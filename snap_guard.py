#!/usr/bin/env python3
"""快照守卫（snap_guard）：批量快照「未知股票」自愈 + 黑名单。

背景（2026-09-21 定位）：
  富途 get_market_snapshot 是整批原子接口——批内任一代码被服务端判「未知股票」，
  整批 400 只全部取不到数据。清单来自 get_stock_basicinfo 证券全集，混有快照查
  不到的品种：退市/更名残留（HK.02900 驴迹科技(旧)）、供股权（HK.02987 马可数字
  科技股权）、6 位临时代码（752 只，如 HK.810951 罗博特科(临时代码)）。港股 3,787
  只分 10 批，3 个坏码就能让 3 个批次全废（HK.03888 金山软件因此连续多日无日线）。

机制（2026-09-28 定稿：发现即拉黑、永久有效）：
  1) 采集前：清单读 v_quote_scope.snap_ok 列，跳过已拉黑代码（零额外调用、零数据损失）；
  2) 采集时：批次失败且错误为「未知股票」→ 从文案提取坏码 → **立即写黑名单**
     （quote_universe.snap_bad_at/reason/cnt，不再等"剔除后重试成功"）→ 剔除该码重试，
     让同批其余股票**当期**拿到数据（保留逐轮重试：每轮都在缩小坏码范围，
     是"发现坏码"的唯一途径，去掉它则每周期只能学 1 个坏码）；
  3) 黑名单**永久有效**：采集流程不再自动复位（旧的「当日有效、每日 08:30 清零」已废弃）。
     坏码终身只报错一次，之后由清单层直接跳过、不再重复请求；
     需要恢复某代码时人工解除：`python3 snap_guard.py list` / `unblock HK.02903`。

为什么"报错即拉黑"（2026-09-28 用户拍板）：
  · 富途「未知股票」是确定性的参数校验错误 → 相信 API 结果；万一误判，人工用 CLI 解除；
  · 旧策略"重试成功才拉黑"的后果：失败批次（批内坏码 ≥4，超出 max_rounds）的坏码
    永远学不到 → 同一批每 5 分钟重学一遍（实测单日 368 次无效重试、整批 400 只全天无数据）。

安全阀（宁可漏拉黑，不可误伤）：
  · 只有「未知股票」错误允许拉黑；限流/断线/超时一律不拉黑（退避重试或放弃）；
  · 提取出的坏码必须与当前批次求交集（防错误文案里其它数字误伤正常股票）；
  · 修复调用有预算（RepairBudget，默认 20 次/run），防限流雪崩。
"""
import re
import time
import logging
from typing import Any

from db import get_conn

log = logging.getLogger("snap_guard")

DEFAULT_REPAIR_BUDGET = 20   # 每次 run 允许的额外快照调用次数（修复重试 + 限流退避）
RATE_LIMIT_SLEEP = 1.5       # 撞限流后的退避秒数
_MAX_REASON_LEN = 500        # snap_bad_reason 入库截断长度

# 「未知股票」后的连续代码 token 序列（如 "HK.00001" / "02900,02905" / "02900、02905"）
# 序列遇非「分隔符+代码」即终止，避免把文案里其它数字（错误码等）误当股票代码。
_UNKNOWN_STOCK_RE = re.compile(
    r"(?:未知股票|unknown\s+stock)\s*[:：]?\s*"
    r"((?:(?:HK|SH|SZ)\.)?\d{4,6}(?:\s*[,，、]\s*(?:(?:HK|SH|SZ)\.)?\d{4,6})*)",
    re.I,
)
_CODE_RE = re.compile(r"(?:(HK|SH|SZ)\.)?(\d{4,6})")
_RATE_LIMIT_RE = re.compile(r"频率太高|too\s+frequent|频率限制", re.I)

_RET_ERR = -1        # snapshot_batch 自造的失败码（非 futu RET_OK）


# ── 错误识别与代码提取（纯函数，无 IO） ──────────────────────────────

def is_unknown_stock_err(err) -> bool:
    """是否「未知股票」类错误（唯一允许拉黑的错误类型）。"""
    return bool(_UNKNOWN_STOCK_RE.search(str(err)))


def is_rate_limit_err(err) -> bool:
    """是否限流类错误（绝不拉黑；退避重试或放弃）。"""
    return bool(_RATE_LIMIT_RE.search(str(err)))


def extract_bad_codes(err, market=None) -> list[str]:
    """从富途错误文案提取坏码 → 完整代码列表（如 HK.02900）。

    实测文案格式："未知股票 HK.00001"（重复前缀场景）/ "未知股票 02900"。
    裸数字按 market 补前缀；调用方**必须**再与本批次代码求交集后才可使用。
    """
    out = []
    for seg in _UNKNOWN_STOCK_RE.findall(str(err)):
        for mk, num in _CODE_RE.findall(seg):
            code = f"{mk}.{num}" if mk else (f"{market}.{num}" if market else num)
            if code not in out:
                out.append(code)
    return out


# ── 黑名单读写（quote_universe.snap_bad_*） ──────────────────────────

def mark_snap_bad(codes, reason="", market=None) -> int:
    """写黑名单：snap_bad_at=NOW()、snap_bad_reason、snap_bad_cnt+1。返回命中行数。

    仅允许由「未知股票」路径调用（错误类型已确认 + 坏码与批内求过交集）。
    黑名单永久有效，解除只能人工（CLI unblock / clear_snap_bad）。
    quote_universe 无对应行的代码（如仅在 realtime_collect_target 的人工池品种）
    无法落账 → 记 warning 供人工处理。
    """
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quote_universe SET snap_bad_at = NOW(), snap_bad_reason = %s, "
                "snap_bad_cnt = COALESCE(snap_bad_cnt, 0) + 1, updated_at = NOW() "
                "WHERE stock_code = ANY(%s)",
                (str(reason)[:_MAX_REASON_LEN], codes))
            n = cur.rowcount
    if n < len(codes):
        log.warning(f"[{market or '-'}] 黑名单写入 {n}/{len(codes)} 只（部分代码不在 quote_universe，"
                    f"需人工确认）: {codes} | 原因: {str(reason)[:120]}")
    else:
        log.warning(f"[{market or '-'}] 快照黑名单 +{n}: {codes} | 原因: {str(reason)[:120]}")
    return n


def get_snap_bad_codes(market=None) -> set[str]:
    """读当前黑名单代码集合（供运维/测试/审计用；采集端走 v_quote_scope.snap_ok）。"""
    sql = "SELECT stock_code FROM quote_universe WHERE snap_bad_at IS NOT NULL"
    params = None
    if market:
        sql += " AND market = %s"
        params = (market,)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {r[0] for r in cur.fetchall()}


def clear_snap_bad(market=None) -> int:
    """人工复位黑名单（**采集流程不再自动调用**——黑名单 2026-09-28 起永久有效）。

    只清 snap_bad_at/reason，**保留 snap_bad_cnt**（累计命中次数，供审计）。
    单票解除优先用 CLI：`python3 snap_guard.py unblock HK.02903`。
    """
    sql = "UPDATE quote_universe SET snap_bad_at = NULL, snap_bad_reason = NULL, updated_at = NOW() " \
          "WHERE snap_bad_at IS NOT NULL"
    params = None
    if market:
        sql += " AND market = %s"
        params = (market,)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


# ── 采集清单读取（v_quote_scope.snap_ok） ────────────────────────────

def read_scope_codes(markets=None):
    """读采集清单 → (可采代码列表, 跳过的黑名单数)。

    snap_ok=false 的代码（快照黑名单）在此过滤——采集端在分批**之前**就排除坏码，
    稳态下批次内零坏码、零额外调用、零数据损失。
    旧库视图未迁移（缺 snap_ok 列）时回退旧查询并告警（黑名单本次不生效，
    跑一次 sync_quote_universe.py 会自动补列+重建视图）。
    """
    params = (list(markets),) if markets else None
    where = " WHERE market = ANY(%s)" if markets else ""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT stock_code, snap_ok FROM v_quote_scope{where} ORDER BY stock_code", params)
                rows = cur.fetchall()
        codes = [r[0] for r in rows if r[1]]
        return codes, len(rows) - len(codes)
    except Exception as e:
        log.warning(f"v_quote_scope 缺 snap_ok 列或读取失败（{type(e).__name__}: {e}）→ 回退旧查询："
                    f"本次黑名单过滤不生效；执行一次 sync_quote_universe.py 可自动迁移视图")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT stock_code FROM v_quote_scope{where} ORDER BY stock_code", params)
                codes = [r[0] for r in cur.fetchall()]
        return codes, 0


# ── 带自愈的批量快照 ────────────────────────────────────────────────

class RepairBudget:
    """单次 run 的额外快照调用预算（可变容器，跨批次共享）。"""

    def __init__(self, n=DEFAULT_REPAIR_BUDGET):
        self.left = int(n)

    def take(self, n=1) -> bool:
        if self.left < n:
            return False
        self.left -= n
        return True


def snapshot_batch(ctx, codes, market, budget=None, max_rounds=3) -> tuple[int, Any, list]:
    """带坏码自愈的批量快照。

    返回 (ret, data, removed)：
      · 正常 / 修复成功：ret == futu.RET_OK，data 为 DataFrame，removed 为本批剔除的坏码；
      · 不可修复失败：ret != RET_OK，data 为服务端错误（或异常描述），removed 为本批已剔除的坏码
        （未及剔除的坏码也已随发现落黑名单，下个采集周期由清单层自动跳过）。

    坏码在**发现当轮即写黑名单**（不等重试成功），所以失败批次也不会白学。

    max_rounds: 单批最多剔除几轮坏码（防"错误文案漏提导致无限循环"）。
    """
    from futu import RET_OK

    budget = budget if budget is not None else RepairBudget()
    pending = list(codes)
    removed = []
    rounds = 0
    attempts = 0

    while True:
        if not pending:
            return _RET_ERR, f"整批代码均判「未知股票」被剔除: {removed}", removed
        try:
            ret, data = ctx.get_market_snapshot(pending)
        except Exception as e:
            # 断线/超时等（_LockedCtx 已内部重连重试一次）：不拉黑、不二次重试
            return _RET_ERR, f"{type(e).__name__}: {e}", removed
        if ret == RET_OK:
            if removed:
                # 坏码在剔除当轮已落黑名单（见下方「未知股票」分支），此处不再重复写库
                log.info(f"[{market}] 快照剔除 {len(removed)} 只未知股票后恢复: {removed} "
                         f"（预算剩余 {budget.left}）")
            return ret, data, removed

        err = str(data)
        attempts += 1

        if is_rate_limit_err(err):
            if attempts <= 1 and budget.take():
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            log.warning(f"[{market}] 快照限流且重试无果（预算剩余 {budget.left}），本批放弃"
                        f"（未拉黑）: {err[:120]}")
            return ret, data, removed

        if not is_unknown_stock_err(err):
            return ret, data, removed        # 其它错误：不识别、不拉黑

        known = set(pending)
        bad = [c for c in extract_bad_codes(err, market) if c in known]
        if not bad:
            log.warning(f"[{market}] 「未知股票」文案未能提取到批内代码（放弃修复、未拉黑）: {err[:160]}")
            return ret, data, removed

        # 证据成立（错误=「未知股票」且坏码在本批内）→ **立即落黑名单**（永久有效）。
        # 刻意放在轮次/预算判断之前：即使本批随后因轮次/预算耗尽而放弃，
        # 坏码也已沉淀，下个采集周期清单层直接跳过 → 不再"同一坏码每 5 分钟重学一遍"。
        try:
            mark_snap_bad(bad, reason=err, market=market)
        except Exception as e:
            log.warning(f"[{market}] 黑名单写入失败（不影响本批采集）: {type(e).__name__}: {e}")

        if rounds >= max_rounds:
            log.warning(f"[{market}] 单批修复轮次达上限 {max_rounds}，本批放弃"
                        f"（坏码 {bad} 已入黑名单，下周期起自动跳过）")
            return ret, data, removed
        if not budget.take():
            log.warning(f"[{market}] 修复预算耗尽（剩 {budget.left}），本批放弃（坏码已入黑名单）")
            return ret, data, removed

        removed.extend(bad)
        pending = [c for c in pending if c not in set(bad)]
        rounds += 1
        log.info(f"[{market}] 批次含未知股票 {bad} → 剔除后重试（第 {rounds} 轮，剩余 {len(pending)} 只）")


# ── 人工管理入口（黑名单永久有效，只能手动解除） ──────────────────────

def _cli_list():
    """打印当前黑名单（永久有效，供人工审计/解除）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, market, snap_bad_at, snap_bad_cnt, "
                "left(COALESCE(snap_bad_reason, ''), 60) "
                "FROM quote_universe WHERE snap_bad_at IS NOT NULL "
                "ORDER BY market, stock_code")
            rows = cur.fetchall()
    if not rows:
        print("快照黑名单为空。")
        return
    print(f"快照黑名单（永久有效，共 {len(rows)} 只）：")
    print(f"{'stock_code':<12} {'market':<6} {'最近命中':<20} {'cnt':<4} reason")
    for code, mkt, at, cnt, reason in rows:
        print(f"{code:<12} {str(mkt or '-'):<6} {str(at)[:19]:<20} {cnt or 0:<4} {reason}")
    print("\n解除某只：python3 snap_guard.py unblock <stock_code> [stock_code ...]")


def _cli_unblock(codes):
    """人工解除黑名单（按代码）。返回 shell 退出码。"""
    codes = [c.strip().upper() for c in codes if c.strip()]
    if not codes:
        print("用法：python3 snap_guard.py unblock <stock_code> [stock_code ...]")
        return 1
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quote_universe SET snap_bad_at = NULL, snap_bad_reason = NULL, "
                "updated_at = NOW() WHERE stock_code = ANY(%s)", (codes,))
            n = cur.rowcount
    print(f"已解除 {n}/{len(codes)} 只：{codes}")
    if n < len(codes):
        print("（未命中 = 代码拼写有误，或本就不在黑名单/不在 quote_universe）")
    return 0


if __name__ == "__main__":
    import sys

    _args = sys.argv[1:]
    if not _args or _args[0] in ("list", "-l", "--list"):
        _cli_list()
    elif _args[0] == "unblock":
        raise SystemExit(_cli_unblock(_args[1:]))
    else:
        print("用法：\n"
              "  python3 snap_guard.py list                       # 查看黑名单\n"
              "  python3 snap_guard.py unblock HK.02903 [HK.xxx]  # 解除（黑名单永久有效，只能手动解除）")
        raise SystemExit(1)
