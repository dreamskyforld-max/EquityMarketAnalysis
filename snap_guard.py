#!/usr/bin/env python3
"""快照守卫（snap_guard）：批量快照「未知股票」自愈 + 黑名单。

背景（2026-09-21 定位）：
  富途 get_market_snapshot 是整批原子接口——批内任一代码被服务端判「未知股票」，
  整批 400 只全部取不到数据。清单来自 get_stock_basicinfo 证券全集，混有快照查
  不到的品种：退市/更名残留（HK.02900 驴迹科技(旧)）、供股权（HK.02987 马可数字
  科技股权）、6 位临时代码（752 只，如 HK.810951 罗博特科(临时代码)）。港股 3,787
  只分 10 批，3 个坏码就能让 3 个批次全废（HK.03888 金山软件因此连续多日无日线）。

机制（当日有效策略，2026-09-21 用户拍板）：
  1) 采集前：清单读 v_quote_scope.snap_ok 列，跳过已确认坏码（零额外调用、零数据损失）；
  2) 采集时：批次失败且错误为「未知股票」→ 从错误文案提取坏码 → 剔除重试
     → 重试成功才写黑名单（quote_universe.snap_bad_at/reason/cnt）；
  3) 每日 08:30 sync_quote_universe 跑完复位：清空 snap_bad_at/reason、保留 snap_bad_cnt
     （跨日累计识别顽固坏码），当天重新学习一次，代码复活自动重新纳入。

安全阀（宁可漏拉黑，不可误伤）：
  · 只有「未知股票」错误允许拉黑；限流/断线/超时一律不拉黑（退避重试或放弃）；
  · 提取出的坏码必须与当前批次求交集（防错误文案里其它数字误伤正常股票）；
  · 剔除后重试仍失败（限流/断网）→ 不写黑名单（宁下次重学，不误判）；
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

    仅允许由「未知股票」路径调用（且须“剔除后重试成功”确认）。
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
    """复位黑名单（当日有效策略：每日 08:30 sync_quote_universe 跑完后调用）。

    只清 snap_bad_at/reason，**保留 snap_bad_cnt**（跨日累计识别顽固坏码）。
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
      · 不可修复失败：ret != RET_OK，data 为服务端错误（或异常描述），removed 为已剔除但
        **未写入黑名单**的代码（剔除后重试仍失败 → 不拉黑，下次重学）。

    max_rounds: 单批最多剔除几轮坏码（防"错误文案漏提导致无限循环"）。
    """
    from futu import RET_OK

    budget = budget if budget is not None else RepairBudget()
    pending = list(codes)
    removed = []
    rounds = 0
    attempts = 0
    last_err = ""

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
                # 剔除后同批成功 → 确认这些码是坏码，才落黑名单
                try:
                    mark_snap_bad(removed, reason=last_err, market=market)
                except Exception as e:
                    log.warning(f"[{market}] 黑名单写入失败（不影响本批采集）: {type(e).__name__}: {e}")
                log.info(f"[{market}] 快照剔除 {len(removed)} 只未知股票后恢复: {removed} "
                         f"（预算剩余 {budget.left}）")
            return ret, data, removed

        err = str(data)
        last_err = err
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
        if rounds >= max_rounds:
            log.warning(f"[{market}] 单批修复轮次达上限 {max_rounds}，本批放弃（余下坏码下次学习）")
            return ret, data, removed
        if not budget.take():
            log.warning(f"[{market}] 修复预算耗尽（剩 {budget.left}），本批放弃（未拉黑）")
            return ret, data, removed

        removed.extend(bad)
        pending = [c for c in pending if c not in set(bad)]
        rounds += 1
        log.info(f"[{market}] 批次含未知股票 {bad} → 剔除后重试（第 {rounds} 轮，剩余 {len(pending)} 只）")
