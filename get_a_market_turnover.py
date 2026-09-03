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
from datetime import date, datetime, timezone, timedelta

from futu import RET_OK
from db import get_conn, bulk_upsert
from collector_runtime import get_shared_ctx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("a_market_turnover")

BATCH_SIZE = 400  # 富途 get_market_snapshot 单次上限 400 只

CACHE_TABLE = "a_stock_list_cache"
CACHE_TTL_DAYS = 7  # 缓存有效期（天）


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
# A股代码清单（富途 get_stock_basicinfo，带 DB 缓存）
# ----------------------------------------------------------------------------
def _ensure_cache_table():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
                    stock_code  VARCHAR(20) NOT NULL,
                    cache_date  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (stock_code)
                )
            """)
        conn.commit()


def _load_cached_codes():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT stock_code FROM {CACHE_TABLE} ORDER BY stock_code")
            return [r[0] for r in cur.fetchall()]


def _save_cache(codes, now):
    data = [{"stock_code": c, "cache_date": now} for c in codes]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {CACHE_TABLE}")
            for d in data:
                cur.execute(
                    f"INSERT INTO {CACHE_TABLE} (stock_code, cache_date) VALUES (%s, %s)",
                    (d["stock_code"], d["cache_date"]),
                )
        conn.commit()


def _a_code_list():
    """全 A 股正股代码列表（SH + SZ，带数据库缓存，7 天有效）。

    优先读缓存（未过期直接用），过期则调富途 get_stock_basicinfo 重新拉取并更新缓存。
    """
    _ensure_cache_table()
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(cache_date) FROM {CACHE_TABLE}")
            row = cur.fetchone()
    cache_date = row[0] if row and row[0] else None
    if cache_date is not None:
        if now - cache_date < timedelta(days=CACHE_TTL_DAYS):
            codes = _load_cached_codes()
            if codes:
                log.info(f"命中股票列表缓存（{cache_date.date()}，{len(codes)} 只）")
                return codes

    # 缓存过期/为空，调富途接口拉取（SH + SZ 两次）
    ctx = get_shared_ctx()
    codes = []
    for market in ("SH", "SZ"):
        try:
            ret, data = ctx.get_stock_basicinfo(market, "STOCK")
            if ret != RET_OK:
                log.warning(f"get_stock_basicinfo({market}) 失败: {data}")
                continue
            for _, r in data.iterrows():
                code = r.get("code")
                if not code or not code.startswith(f"{market}."):
                    continue
                num = code.split(".", 1)[1]
                # 仅保留标准 A 股正股号码段，排除非 A 股品种：
                #  - B 股（沪 900 / 深 200 开头）：独立市场，不计入 A 股成交额
                #  - 沪伦通 CDR / 公募 REITs 等（689/700/742/760 等开头）：
                #    新浪 stock_zh_a_daily 无数据，且非标准 A 股正股，排除
                # 保留段：沪市 600/601/603/605/688，深市 000/001/002/003/300/301
                keep_sh = num[:3] in ("600", "601", "603", "605", "688")
                keep_sz = num[:3] in ("000", "001", "002", "003", "300", "301")
                if not (keep_sh or keep_sz):
                    continue
                codes.append(code)
        except Exception as e:
            log.warning(f"get_stock_basicinfo({market}) 异常: {e}")
    if codes:
        _save_cache(codes, now)
        log.info(f"更新股票列表缓存（{now.date()}，{len(codes)} 只）")
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
    prev_close = _f(row.get("prev_close_price"))
    change_pct = None
    if last_price is not None and prev_close:
        change_pct = round((last_price - prev_close) / prev_close * 100, 4)

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


def run(codes=None, ctx=None):
    """采集入口（常驻调用兼容）。codes 可为代码列表，空则全 A 股。"""
    if not codes:
        codes = _a_code_list()
    rec = fetch_market_snapshot_batch(codes, ctx)
    if rec.get("trade_date"):
        save_to_db(rec)
        _save_quotes(rec.get("quote_rows", []))
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
    os._exit(0)
