#!/usr/bin/env python3
"""
A股全市场总成交额采集（富途批量快照版）

数据源：富途 OpenQuoteContext.get_market_snapshot()（本地 OpenD，端口 11111）
口径：全 A 股正股（沪 SH + 深 SZ），分批（400/批）批量快照，SUM(turnover) → 全市场总成交额
落表（均独立于港股表，避免破坏现网数据）：
  - a_daily_market_turnover  —— A股全市场总成交额（按 trade_date 唯一）
  - a_daily_quote            —— A股个股日线数据池（含估值/换手率，富途口径）

设计要点：
  - 富途快照的 turnover 单位为「元」（非港元），打印按 /1e8 亿元。
  - 代码清单来自富途 get_stock_basicinfo(SH/SZ, STOCK)（不依赖 akshare 外网源，
    实测 akshare 新浪/东财源在部分网络下不稳定）。清单带 DB 缓存（7 天有效）。
  - 盘后调用（如 15:30）拿到的就是当天全天完整值。
  - 历史回溯由 backfill_a_market_turnover.py 负责（富途 request_history_kline，分批）。
"""
import sys
import os
import logging
from datetime import date, datetime, timezone

from futu import RET_OK
from db import get_conn, bulk_upsert
from collector_runtime import get_shared_ctx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("a_market_turnover")

BATCH_SIZE = 400  # 富途 get_market_snapshot 单次上限 400 只


def _as_float(v):
    """安全转 float：非数字 / NaN / None 返回 None，否则返回数值。

    用于过滤占位行：富途对停牌或异常日可能返回 NaN、空串或其他非数值，
    直接 float() 会抛错，pd.notna 又识别不了非数值字符串。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN 不等于自身
        return None
    return f


def _parse_date(update_time):
    """从 update_time 提取日期。"""
    if not update_time or update_time == "N/A":
        return date.today()
    try:
        return date.fromisoformat(str(update_time)[:10])
    except (ValueError, TypeError):
        return date.today()


# ----------------------------------------------------------------------------
# A股采集清单（读 v_quote_scope）
# ----------------------------------------------------------------------------
def _a_code_list():
    """全 A 股采集清单：读 v_quote_scope（quote_universe.is_collectable ∪ stock_info.is_active）。

    替代原「富途 get_stock_basicinfo + 段号白名单 + 7 天 DB 缓存」实现：
      · 清单由 sync_quote_universe.py 每日 08:30 统一维护（含计数护栏 + 30 天软删）；
      · 段号白名单上移为 quote_universe.is_primary（口径列），采集侧不再硬编码，
        因此这里会采到 B股/REIT/CDR 等非正股品种（口径过滤交给读方用 is_primary）；
      · 并集 stock_info.is_active，以覆盖 SH.520900 这类「深采有、富途 STOCK 全集无」的品种。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stock_code FROM v_quote_scope "
                        "WHERE market IN ('SH','SZ') ORDER BY stock_code")
            codes = [r[0] for r in cur.fetchall()]
    if not codes:
        log.warning("v_quote_scope 返回空（清单未初始化？），本次不采集")
    else:
        log.info(f"A股采集清单（v_quote_scope）: {len(codes)} 只")
    return codes


# ----------------------------------------------------------------------------
# 批量快照
# ----------------------------------------------------------------------------
def fetch_market_snapshot_batch(codes, ctx=None):
    """富途批量快照：返回 (trade_date, total_turnover, total_volume, stock_count, quote_rows)。"""
    ctx = ctx or get_shared_ctx()
    targets = list(codes)  # 已是完整 SH./SZ. 代码

    total_turnover = 0.0
    total_volume = 0
    stock_count = 0
    trade_date = None
    quote_rows = []

    for i in range(0, len(targets), BATCH_SIZE):
        batch = targets[i:i + BATCH_SIZE]
        ret, data = ctx.get_market_snapshot(batch)
        if ret != RET_OK:
            log.warning(f"批次 {i // BATCH_SIZE + 1} 失败: {data}")
            continue
        for _, row in data.iterrows():
            code = row["code"]
            td = _parse_date(row.get("update_time"))
            if trade_date is None:
                trade_date = td
            # 直接判断「是否为数字且 >0」：富途异常日快照 turnover 可能是 NaN、空串
            # 或非数值，pd.notna 识别不了非数值字符串，NaN>0 又恒为 False，会把空行
            # 写进 a_daily_quote。统一用安全转换：非数字/NaN/None 视为无成交跳过。
            turnover = _as_float(row.get("turnover")) or 0.0
            volume = int(_as_float(row.get("volume")) or 0)
            if turnover > 0:
                total_turnover += turnover
                total_volume += volume
                stock_count += 1
                quote_rows.append(_map_quote_row(code, td, row))

    return {
        "trade_date": trade_date,
        "total_turnover": total_turnover,
        "total_volume": total_volume,
        "stock_count": stock_count,
        "snapshot_time": datetime.now(timezone.utc),
        "quote_rows": quote_rows,
    }


def _map_quote_row(code, trade_date, row):
    """富途快照行 → a_daily_quote 字段。"""
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    last_price = _f(row.get("last_price"))
    prev_close = _f(row.get("prev_close_price"))  # 富途官方昨收，当"前交易日收盘价"最权威
    change_pct = None
    if last_price is not None and prev_close:
        change_pct = round((last_price - prev_close) / prev_close * 100, 2)

    ut = row.get("update_time")
    update_time = None
    if ut and str(ut) != "N/A":
        try:
            update_time = str(ut)
        except (TypeError, ValueError):
            update_time = None

    return {
        "stock_code": code,
        "trade_date": trade_date,
        "open": _f(row.get("open_price")),
        "high": _f(row.get("high_price")),
        "low": _f(row.get("low_price")),
        "close": last_price,
        "volume": int(row["volume"]) if row.get("volume") is not None else 0,
        "amount": _f(row.get("turnover")),
        "turnover_rate": _f(row.get("turnover_rate")),
        "volume_ratio": _f(row.get("volume_ratio")) if (row.get("volume_ratio") or 0) > 0 else None,
        "high_52w": _f(row.get("highest52weeks_price")) if (row.get("highest52weeks_price") or 0) > 0 else None,
        "low_52w": _f(row.get("lowest52weeks_price")) if (row.get("lowest52weeks_price") or 0) > 0 else None,
        "total_market_val": _f(row.get("total_market_val")),
        "circular_market_val": _f(row.get("circular_market_val")),
        "pe_ratio": _f(row.get("pe_ratio")),
        "pe_ttm_ratio": _f(row.get("pe_ttm_ratio")),
        "pb_ratio": _f(row.get("pb_ratio")),
        "dividend_ratio_ttm": _f(row.get("dividend_ratio_ttm")),
        "prev_close": prev_close,
        "change_pct": change_pct,
        "update_time": update_time,
    }


# ----------------------------------------------------------------------------
# 落表
# ----------------------------------------------------------------------------
_DDL = {
    "a_daily_market_turnover": """
        CREATE TABLE IF NOT EXISTS a_daily_market_turnover (
            id BIGSERIAL PRIMARY KEY,
            trade_date DATE NOT NULL,
            snapshot_time TIMESTAMPTZ NOT NULL,
            total_turnover NUMERIC(22,2),
            total_volume BIGINT,
            stock_count INT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (trade_date)
        )
    """,
    "a_daily_quote": """
        CREATE TABLE IF NOT EXISTS a_daily_quote (
            id BIGSERIAL PRIMARY KEY,
            stock_code VARCHAR(20) NOT NULL,
            trade_date DATE NOT NULL,
            open NUMERIC(12,4),
            high NUMERIC(12,4),
            low NUMERIC(12,4),
            close NUMERIC(12,4),
            prev_close NUMERIC(12,4),
            change_pct NUMERIC(12,4),
            volume BIGINT,
            amount NUMERIC(22,2),
            turnover_rate NUMERIC(8,4),
            volume_ratio NUMERIC(8,4),
            high_52w NUMERIC(12,4),
            low_52w NUMERIC(12,4),
            total_market_val NUMERIC(22,2),
            circular_market_val NUMERIC(22,2),
            pe_ratio NUMERIC(12,4),
            pe_ttm_ratio NUMERIC(12,4),
            pb_ratio NUMERIC(12,4),
            dividend_ratio_ttm NUMERIC(8,4),
            update_time TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (stock_code, trade_date)
        )
    """,
}


def _ensure_table(table):
    ddl = _DDL.get(table)
    if not ddl:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            # 补齐 prev_close / change_pct（旧库可能缺；CREATE TABLE IF NOT EXISTS 不补列）
            if table in ("a_daily_quote",):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS prev_close NUMERIC(12,4);")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS change_pct NUMERIC(12,4);")
        conn.commit()


def save_to_db(rec):
    if not rec or not rec.get("trade_date"):
        return
    _ensure_table("a_daily_market_turnover")
    data = [{
        "trade_date": rec["trade_date"],
        "snapshot_time": rec["snapshot_time"],
        "total_turnover": rec["total_turnover"],
        "total_volume": rec["total_volume"],
        "stock_count": rec["stock_count"],
    }]
    with get_conn() as conn:
        bulk_upsert(conn, "a_daily_market_turnover", data, conflict_cols=["trade_date"])
    log.info(f"落库: {rec['trade_date']} 总成交额 {rec['total_turnover']/1e8:.2f} 亿元, 股票 {rec['stock_count']} 只")


def _save_quotes(quote_rows):
    if not quote_rows:
        return
    _ensure_table("a_daily_quote")
    batch = 2000
    with get_conn() as conn:
        for i in range(0, len(quote_rows), batch):
            bulk_upsert(conn, "a_daily_quote", quote_rows[i:i + batch],
                        conflict_cols=["stock_code", "trade_date"])
    log.info(f"个股日线落库 {len(quote_rows)} 条（a_daily_quote）")


def _print(rec):
    if not rec or not rec.get("trade_date"):
        print("采集失败（数据为空）")
        return
    print("=" * 60)
    print("A股全市场总成交额（富途批量快照）")
    print("=" * 60)
    print(f"数据日期 : {rec['trade_date']}")
    print(f"总成交额 : {rec['total_turnover']/1e8:,.2f} 亿元")
    print(f"总成交量 : {rec['total_volume']:,} 股")
    print(f"标的数量 : {rec['stock_count']} 只")


def _fill_today_valuation(rec, dry=False):
    """当日 A 股估值（市值/PE/PB/PS/PCF）顺带回填，失败不影响成交额主流程。

    复用 backfill_a_valuation.run_valuation_one_day（东财 RPT_VALUEANALYSIS_DET
    当日快照 + QFQ 换算）。依赖 a_daily_quote 当日行已由本脚本写就（close 用于复权因子）。
    """
    if not rec or not rec.get("trade_date"):
        return
    try:
        from backfill_a_valuation import run_valuation_one_day
        run_valuation_one_day(rec["trade_date"], dry=dry)
    except Exception as e:
        log.warning(f"当日 A 股估值回填失败（不影响成交额主流程）: {e}")


def run(codes=None, ctx=None):
    """采集入口（常驻调用兼容）。codes 可为代码列表，空则全 A 股。"""
    if not codes:
        codes = _a_code_list()
    rec = fetch_market_snapshot_batch(codes, ctx)
    if rec.get("trade_date"):
        save_to_db(rec)
        _save_quotes(rec.get("quote_rows", []))
        _fill_today_valuation(rec)
    _print(rec)
    return rec


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    codes = _a_code_list()
    rec = fetch_market_snapshot_batch(codes)
    _print(rec)
    if rec.get("trade_date") and not dry:
        save_to_db(rec)
        _save_quotes(rec.get("quote_rows", []))
        _fill_today_valuation(rec, dry=dry)
    os._exit(0)
