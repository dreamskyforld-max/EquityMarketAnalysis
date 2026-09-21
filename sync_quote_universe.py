#!/usr/bin/env python3
"""
全市场采集清单刷新器（quote_universe）

做三件事：
  1) 从富途 get_stock_basicinfo(market, sec_type) 全量拉取 → upsert 进 quote_universe
  2) 软删：last_seen 超 N 天（默认 30）未在源出现 → is_collectable=FALSE（再出现自动恢复）
  3) 联动：新增代码 INSERT 进 stock_info（仅补名称字典，绝不写 realtime_collect_target、
     不自动进入实时采集池），并补齐 stock_name / list_date / exchange_type / delisting；
     已存在行只更新这些「源权威字段」与 currency

刷新机制（2026-09-14 定稿）：
  · 频率：每日 08:30（market_scheduler.GLOBAL_TASKS，force=True 不受交易时段门控）
  · 三市场各自拉取/重试/落库，单市场失败不影响其它市场
  · 护栏：单市场新拉数量偏离当前值 >±10% 或低于绝对下限 → 该市场拒绝写入并告警
    （替代旧实现的「非空即存」，防「源半返回/接口变更 → 残缺清单被落库并沿用」）
  · 只增不删：软删只置 is_collectable=FALSE，不物理删除；stock_info 更是只增不删

为什么用富途而不是 akshare 现货快照（旧实现）：
  · 旧实现（akshare stock_hk_spot）只返回「当日有报价」的股票（2,800 只），
    富途 get_stock_basicinfo 返回全部 STOCK（3,787 只，含 GEM/停牌），且带上市日/板块类型；
  · 「100 只/7 天」额度限制只针对 request_history_kline，与本接口无关。

用法：
    python3 sync_quote_universe.py              # 刷新（幂等）
    python3 sync_quote_universe.py --dry-run    # 只拉取+比对+报告，不写库
    python3 sync_quote_universe.py --force      # 忽略护栏强制写入（人工确认后用）
    python3 sync_quote_universe.py --market HK  # 仅刷新指定市场（HK/SH/SZ）
"""
import os
import sys
import time
import logging
from datetime import date, datetime, timezone

from db import get_conn
from collector_runtime import get_shared_ctx
from snap_guard import clear_snap_bad

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sync_quote_universe")
if logging.getLogger().handlers and not log.handlers:
    # 常驻进程（scheduler）已有日志配置时（如 log_utils 写入 ./log/），不再叠加格式，避免同一行打印两遍
    log.propagate = True

# ── 参数 ─────────────────────────────────────────────────────
MARKETS = ("HK", "SH", "SZ")
# 绝对值下限：低于此数一律判为源异常（当前实测 HK≈3787 / SH≈2380 / SZ≈2969）
_ABS_FLOOR = {"HK": 3000, "SH": 2000, "SZ": 2000}
# 相对护栏：新数量偏离当前值超过该比例 → 拒绝写入
_DEV_GUARD = 0.10
# 软删窗口：last_seen 超过 N 天未在源出现 → is_collectable=FALSE
_DEFAULT_SOFT_DELETE_DAYS = 30
# A 股「正股」段号（计入全市场成交额口径）；港股不做段号排除（GEM 也计入港股全市场）
_A_PRIMARY_SEGS = ("600", "601", "603", "605", "688",
                   "000", "001", "002", "003", "300", "301")


def _cfg(key, env, default):
    from config import val
    return str(val("quote", key, env, default)).strip()


def _universe_types():
    return [t.strip().upper() for t in _cfg("universe_types", "UNIVERSE_TYPES", "STOCK").split(",") if t.strip()]


def _soft_delete_days():
    try:
        return int(_cfg("universe_soft_delete_days", "UNIVERSE_SOFT_DELETE_DAYS", str(_DEFAULT_SOFT_DELETE_DAYS)))
    except ValueError:
        return _DEFAULT_SOFT_DELETE_DAYS


def _parse_listing_date(v):
    """富途 listing_date → date；'N/A'/空/1970-01-01 占位 → None（与 stock_info 注释一致）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "N/A":
        return None
    try:
        d = date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None
    return None if d == date(1970, 1, 1) else d


def _is_primary(market, seg):
    if market == "HK":
        return True                      # 港股：全部 STOCK 计入（含 GEM）
    return seg in _A_PRIMARY_SEGS        # A 股：仅正股段


def _ensure_table():
    """确保 quote_universe 表/索引/快照黑名单列 + v_quote_scope 视图就绪。

    DDL 从 sql/schema.sql 提取，避免两处维护漂移；CREATE TABLE IF NOT EXISTS 不补列，
    故对旧库显式 ADD COLUMN（幂等）；视图 CREATE OR REPLACE 同步 snap_ok 列（采集端据此过滤黑名单）。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql", "schema.sql")
    txt = open(path, encoding="utf-8").read()
    t0 = txt.find("CREATE TABLE IF NOT EXISTS quote_universe")
    if t0 < 0:
        raise RuntimeError(f"{path} 未找到 quote_universe 定义（应有 1.1 段）")
    i0 = txt.find("CREATE INDEX IF NOT EXISTS idx_quote_universe_scope", t0)
    if i0 < 0:
        raise RuntimeError(f"{path} 未找到 idx_quote_universe_scope 定义")
    end = txt.find(";", i0)
    stmts = [s.strip() for s in txt[t0:end + 1].split(";") if s.strip()]
    # 旧库补列（CREATE TABLE IF NOT EXISTS 不补列）：快照黑名单三列（snap_guard.py 读写）
    stmts += [
        "ALTER TABLE quote_universe ADD COLUMN IF NOT EXISTS snap_bad_at TIMESTAMPTZ",
        "ALTER TABLE quote_universe ADD COLUMN IF NOT EXISTS snap_bad_reason TEXT",
        "ALTER TABLE quote_universe ADD COLUMN IF NOT EXISTS snap_bad_cnt INTEGER DEFAULT 0",
    ]
    # 视图随 schema.sql 重建（v_quote_scope 增 snap_ok 列）
    v0 = txt.find("CREATE OR REPLACE VIEW v_quote_scope")
    if v0 < 0:
        raise RuntimeError(f"{path} 未找到 v_quote_scope 定义")
    stmts.append(txt[v0:txt.find(";", v0) + 1].strip())
    with get_conn() as conn:
        with conn.cursor() as cur:
            for s in stmts:
                cur.execute(s)
    return len(stmts)


def _cur_state():
    """读当前库内状态 → (各市场可采数 dict, 已知代码集合)。表不存在时返回空（dry-run 友好）。"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT market, COUNT(*) FROM quote_universe WHERE is_collectable GROUP BY market")
                active = dict(cur.fetchall())
                cur.execute("SELECT stock_code FROM quote_universe")
                known = {r[0] for r in cur.fetchall()}
        return active, known
    except Exception as e:
        log.info(f"quote_universe 尚不可读（首次运行？）: {type(e).__name__} → 按空清单处理")
        return {}, set()


def fetch_market(ctx, market, sec_type, max_retry=3):
    """拉单市场全量清单 → list[dict]；失败返回 None。"""
    from futu import RET_OK
    for attempt in range(max_retry):
        try:
            ret, data = ctx.get_stock_basicinfo(market, sec_type)
            if ret != RET_OK:
                raise RuntimeError(str(data))
            now = datetime.now(timezone.utc)
            rows = []
            for _, r in data.iterrows():
                code = str(r.get("code") or "")
                if not code.startswith(f"{market}."):
                    continue
                seg = code.split(".", 1)[1][:3]
                nz = r.get("name")
                name = None if nz is None or str(nz).strip() in ("", "N/A") else str(nz).strip()
                rows.append({
                    "stock_code": code,
                    "stock_name": name,
                    "market": market,
                    "seg": seg,
                    "sec_type": sec_type,
                    "is_primary": _is_primary(market, seg),
                    "is_collectable": True,      # 源里出现 = 可采（自动恢复）
                    "listing_date": _parse_listing_date(r.get("listing_date")),
                    "exchange_type": None if str(r.get("exchange_type", "N/A")) == "N/A" else str(r.get("exchange_type")),
                    "source": "futu_basicinfo",
                    "last_seen": now,
                    "updated_at": now,
                })
            return rows
        except Exception as e:
            log.warning(f"[{market}/{sec_type}] 拉取失败（第 {attempt + 1} 次）: {e}")
            time.sleep(1 + attempt)
    return None


def guard_check(market, new_n, cur_n):
    """计数护栏：返回 (是否放行, 原因)。"""
    floor = _ABS_FLOOR.get(market, 0)
    if new_n < floor:
        return False, f"数量 {new_n} 低于绝对下限 {floor}"
    if cur_n and cur_n > 0:
        dev = abs(new_n - cur_n) / cur_n
        if dev > _DEV_GUARD:
            return False, f"数量 {new_n} 偏离当前值 {cur_n} 达 {dev * 100:.1f}%（>±{_DEV_GUARD * 100:.0f}%）"
    return True, ""


def _upsert_universe(conn, rows):
    """upsert 进 quote_universe（first_seen 不覆盖）。"""
    from psycopg2.extras import execute_values
    cols = ["stock_code", "stock_name", "market", "seg", "sec_type", "is_primary",
            "is_collectable", "listing_date", "exchange_type", "source", "last_seen", "updated_at"]
    data = [tuple(r.get(c) for c in cols) for r in rows]
    sql = f"""
        INSERT INTO quote_universe ({", ".join(cols)})
        VALUES %s
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name     = EXCLUDED.stock_name,
            market         = EXCLUDED.market,
            seg            = EXCLUDED.seg,
            sec_type       = EXCLUDED.sec_type,
            is_primary     = EXCLUDED.is_primary,
            is_collectable = EXCLUDED.is_collectable,
            listing_date   = COALESCE(EXCLUDED.listing_date, quote_universe.listing_date),
            exchange_type  = EXCLUDED.exchange_type,
            source         = EXCLUDED.source,
            last_seen      = EXCLUDED.last_seen,
            updated_at     = EXCLUDED.updated_at
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, data, page_size=2000)
    return len(data)


def _sync_stock_info(conn, rows):
    """联动：新增代码 → INSERT 进 stock_info（仅补名称字典）；已存在 → 只更新「源权威字段」，且仅在有变更时写。

    绝不写 realtime_collect_target（采集池由前端维护）与 currency（历史两套约定并存，不掺和）。
    返回 (新增数, 有变更数)；rows 为空/None 时返回 (0, 0)。
    """
    from psycopg2.extras import execute_values
    if not rows:
        return 0, 0
    data = []
    for r in rows:
        market = r["market"]
        data.append((
            r["stock_code"], r["stock_name"], market,
            r["stock_code"].split(".", 1)[1],        # symbol：纯数字
            "HKD" if market == "HK" else "CNY",      # currency：与主流约定一致（不覆盖旧值）
            r["listing_date"], r["exchange_type"],
            False,                                   # delisting：富途当前对所有 STOCK 返回 False
        ))
    # 只写名称字典，不写 realtime_collect_target（绝不自动纳入采集池）
    sql = """
        INSERT INTO stock_info (stock_code, stock_name, market, symbol, currency,
                                list_date, exchange_type, delisting)
        VALUES %s
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name    = COALESCE(EXCLUDED.stock_name, stock_info.stock_name),
            list_date     = COALESCE(EXCLUDED.list_date, stock_info.list_date),
            exchange_type = COALESCE(EXCLUDED.exchange_type, stock_info.exchange_type),
            delisting     = COALESCE(EXCLUDED.delisting, stock_info.delisting),
            updated_at    = NOW()
        WHERE stock_info.stock_name    IS DISTINCT FROM COALESCE(EXCLUDED.stock_name, stock_info.stock_name)
           OR stock_info.list_date     IS DISTINCT FROM COALESCE(EXCLUDED.list_date, stock_info.list_date)
           OR stock_info.exchange_type IS DISTINCT FROM COALESCE(EXCLUDED.exchange_type, stock_info.exchange_type)
           OR stock_info.delisting     IS DISTINCT FROM COALESCE(EXCLUDED.delisting, stock_info.delisting)
    """
    codes = [d[0] for d in data]
    with conn.cursor() as cur:
        cur.execute("SELECT stock_code FROM stock_info WHERE stock_code = ANY(%s)", (codes,))
        existing = {r[0] for r in cur.fetchall()}
        execute_values(cur, sql, data, page_size=2000)
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    inserted = len([c for c in codes if c not in existing])
    return inserted, changed


def run(codes=None, ctx=None):
    """刷新入口（常驻调用兼容；codes 忽略）。"""
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    only = None
    if "--market" in sys.argv:
        i = sys.argv.index("--market")
        only = sys.argv[i + 1].upper()
    markets = [m for m in MARKETS if (only is None or m == only)]
    types = _universe_types()
    soft_days = _soft_delete_days()

    log.info(f"刷新开始：markets={markets} types={types} dry_run={dry} force={force} 软删窗口={soft_days} 天")
    ctx = ctx or get_shared_ctx()

    if not dry:
        n_ddl = _ensure_table()
        log.info(f"quote_universe 表/索引就绪（执行 {n_ddl} 条 DDL）")

    # 当前库内状态（护栏基线 + 新增判定）
    cur_active, known = _cur_state()

    total_up = 0
    for market in markets:
        fetched = None
        for sec_type in types:
            rows = fetch_market(ctx, market, sec_type)
            if rows is None:
                log.warning(f"[{market}/{sec_type}] 拉取失败（重试耗尽），本市场跳过（保留旧清单）")
                fetched = None
                break
            fetched = (fetched or []) + rows
        if fetched is None:
            continue

        n_new = len(fetched)
        cur_n = cur_active.get(market, 0)
        ok, reason = guard_check(market, n_new, cur_n)
        added = sorted({r["stock_code"] for r in fetched} - known)
        log.info(f"[{market}] 源返回 {n_new} 只（库内可采 {cur_n} 只）| 新增 {len(added)} 只 | 护栏: "
                 + ("放行" if ok or force else f"拒绝（{reason}）"))
        if added:
            log.info(f"[{market}] 新增清单（前 20）: {added[:20]}")
        if not ok and not force:
            log.warning(f"[{market}] 护栏拦截，未写入（可用 --force 人工确认后强制写入）")
            continue

        if dry:
            continue

        with get_conn() as conn:
            n_up = _upsert_universe(conn, fetched)
            total_up += n_up
            # 联动 stock_info：新增 → INSERT（仅名称字典）；存量 → 只刷新源权威字段（有变更才写）
            ins_n, chg_n = _sync_stock_info(conn, fetched)
            log.info(f"[{market}] quote_universe 写入 {n_up} 只；stock_info 联动：新增 {ins_n} 只、"
                     f"存量字段更新 {chg_n} 只")

    # 软删（全局，不区分市场）
    if not dry:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE quote_universe SET is_collectable = FALSE, updated_at = NOW() "
                    "WHERE is_collectable AND (last_seen IS NULL OR last_seen < NOW() - make_interval(days => %s))",
                    (soft_days,))
                n_off = cur.rowcount
        log.info(f"软删：{n_off} 只超 {soft_days} 天未在源出现 → is_collectable=FALSE")
        # 快照黑名单复位（当日有效策略）：清单刷新 = 新一天的学习起点。
        # 清 snap_bad_at/reason 让当天采集重新验证一遍（代码复活自动回归，防永久误伤）；
        # snap_bad_cnt 跨日累计保留，用于识别连续多日命中的顽固坏码（退市残留/供股权/临时代码）。
        n_reset = clear_snap_bad()
        log.info(f"快照黑名单复位：{n_reset} 只（当日有效；snap_bad_cnt 累计保留）")
    else:
        log.info("dry-run：不写库")

    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""SELECT market, COUNT(*), COUNT(*) FILTER (WHERE is_collectable),
                                      COUNT(*) FILTER (WHERE is_primary) FROM quote_universe
                               GROUP BY market ORDER BY market""")
                log.info("刷新后 quote_universe：")
                for m, n, c, p in cur.fetchall():
                    log.info(f"   {m}: 共 {n} 只 / 可采 {c} / 正股口径 {p}")
            except Exception as e:
                log.info(f"（quote_universe 统计跳过：{type(e).__name__}）")
            conn.rollback()
            cur.execute("SELECT (SELECT COUNT(*) FROM stock_info), "
                        "(SELECT COUNT(*) FROM realtime_collect_target)")
            t, a = cur.fetchone()
            log.info(f"stock_info：共 {t} 只；（实时采集池：{a} 只）")
    log.info(f"刷新完成（写入 {total_up} 只）")


if __name__ == "__main__":
    run()
    os._exit(0)
