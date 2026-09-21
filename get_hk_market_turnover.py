#!/usr/bin/env python3
"""
港股全市场总成交额采集（富途批量快照版）

数据源：富途 OpenQuoteContext.get_market_snapshot()（本地 OpenD，端口 11111）
口径：全港股正股，分批（400/批）批量快照，SUM(turnover) → 全市场总成交额
落表：
  - daily_market_turnover  —— 全市场总成交额（按 trade_date 唯一）
  - hk_daily_quote         —— 个股日线数据池（含估值/换手率，富途口径）

设计要点：
  - 富途快照的 turnover 与新浪 stock_hk_daily 的 amount 逐字段一致（已验证），
    且批量 400 只/次，全市场 2800 只仅需约 7 次调用，远快于逐只拉取。
  - 盘后调用（如 17:00）拿到的就是当天全天完整值。
  - 历史回溯由 backfill_hk_market_turnover.py 负责（同样走富途批量）。
  - 分层：全市场 → hk_daily_quote；重点深采 → daily_quote（不变）。

字段映射（富途快照 → hk_daily_quote）：
  参考 get_quote.py save_quote_to_db，字段名含 prev_close_price / highest52weeks_price 等。
"""
import sys
import logging
from datetime import date, datetime, timezone

from futu import RET_OK
from db import get_conn, bulk_upsert
from collector_runtime import get_shared_ctx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hk_market_turnover")

BATCH_SIZE = 400  # 富途 get_market_snapshot 单次上限 400 只


def _parse_date(update_time):
    """从 update_time 提取日期。"""
    if not update_time or update_time == "N/A":
        return date.today()
    try:
        return date.fromisoformat(str(update_time)[:10])
    except (ValueError, TypeError):
        return date.today()


# ----------------------------------------------------------------------------
# 港股采集清单（读 v_quote_scope）
# ----------------------------------------------------------------------------
def _hk_code_list():
    """全港股采集清单：读 v_quote_scope（quote_universe.is_collectable ∪ realtime_collect_target）。

    替代原「akshare stock_hk_spot 现货快照 + 7 天 DB 缓存」实现：
      · 现货快照只返回「当日有报价」的股票（约 2,800 只），会漏掉停牌/长期无成交/GEM 等
        （阶段 0 就踩过：21 只库内 HK 代码不在清单内）；
      · 现由 sync_quote_universe.py 用富途 get_stock_basicinfo 维护全集（3,787 只），
        「僵尸股」由入库侧「只落成交额>0 的行」自然过滤，无需清单侧裁剪；
      · 并集 realtime_collect_target（在池全集），覆盖深采池品种。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stock_code FROM v_quote_scope WHERE market = 'HK' ORDER BY stock_code")
            codes = [r[0] for r in cur.fetchall()]
    if not codes:
        log.warning("v_quote_scope 返回空（清单未初始化？），本次不采集")
    else:
        log.info(f"港股采集清单（v_quote_scope）: {len(codes)} 只")
    return codes


def fetch_market_snapshot_batch(codes, ctx=None):
    """富途批量快照：返回 (trade_date, total_turnover, total_volume, stock_count, quote_rows)。"""
    ctx = ctx or get_shared_ctx()
    targets = [f"HK.{c}" for c in codes]

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
            # 确定交易日期（以第一只为准）
            td = _parse_date(row.get("update_time"))
            if trade_date is None:
                trade_date = td
            # 成交额/成交量
            turnover = float(row["turnover"]) if row.get("turnover") is not None else 0.0
            volume = int(row["volume"]) if row.get("volume") is not None else 0
            if turnover > 0:
                total_turnover += turnover
                total_volume += volume
                stock_count += 1
                # 只落「有成交」的个股日线（对齐 A 股口径）：
                # 全集里的僵尸/停牌代码 turnover<=0（含 NaN，NaN>0 为 False）→ 不落行，
                # 既不膨胀行数、不影响 daily_market_turnover 合计口径；
                # 复活/新上市一旦有成交即自动开始落行（天然自愈，无 bootstrap 问题）。
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
    """富途快照行 → hk_daily_quote 字段（参考 get_quote.py 映射）。"""
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

    # 数据更新时间：富途快照的 update_time（"N/A" 或空则置 None）
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


_DDL = {
    "daily_market_turnover": """
        CREATE TABLE IF NOT EXISTS daily_market_turnover (
            id BIGSERIAL PRIMARY KEY,
            trade_date DATE NOT NULL,
            snapshot_time TIMESTAMPTZ NOT NULL,
            total_turnover NUMERIC(20,2),
            total_volume BIGINT,
            stock_count INT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (trade_date)
        )
    """,
    "hk_daily_quote": """
        CREATE TABLE IF NOT EXISTS hk_daily_quote (
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
            amount NUMERIC(20,2),
            turnover_rate NUMERIC(8,4),
            volume_ratio NUMERIC(8,4),
            high_52w NUMERIC(12,4),
            low_52w NUMERIC(12,4),
            total_market_val NUMERIC(20,2),
            circular_market_val NUMERIC(20,2),
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
    """表不存在时自动创建（服务器部署免手动建表）。"""
    ddl = _DDL.get(table)
    if not ddl:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            # 补齐 prev_close / change_pct（旧库可能缺；CREATE TABLE IF NOT EXISTS 不补列）
            if table in ("hk_daily_quote",):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS prev_close NUMERIC(12,4);")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS change_pct NUMERIC(12,4);")
        conn.commit()


def save_to_db(rec):
    from db import bulk_upsert
    if not rec or not rec.get("trade_date"):
        return
    _ensure_table("daily_market_turnover")
    data = [{
        "trade_date": rec["trade_date"],
        "snapshot_time": rec["snapshot_time"],
        "total_turnover": rec["total_turnover"],
        "total_volume": rec["total_volume"],
        "stock_count": rec["stock_count"],
    }]
    with get_conn() as conn:
        bulk_upsert(conn, "daily_market_turnover", data, conflict_cols=["trade_date"])
    log.info(f"落库: {rec['trade_date']} 总成交额 {rec['total_turnover']/1e8:.2f} 亿港元, 股票 {rec['stock_count']} 只")


def _save_quotes(quote_rows):
    if not quote_rows:
        return
    from db import bulk_upsert
    _ensure_table("hk_daily_quote")
    batch = 2000
    with get_conn() as conn:
        for i in range(0, len(quote_rows), batch):
            bulk_upsert(conn, "hk_daily_quote", quote_rows[i:i + batch],
                        conflict_cols=["stock_code", "trade_date"])
    log.info(f"个股日线落库 {len(quote_rows)} 条（hk_daily_quote）")


def _print(rec):
    if not rec or not rec.get("trade_date"):
        print("采集失败（数据为空）")
        return
    print("=" * 60)
    print("港股全市场总成交额（富途批量快照）")
    print("=" * 60)
    print(f"数据日期 : {rec['trade_date']}")
    print(f"总成交额 : {rec['total_turnover']/1e8:,.2f} 亿港元")
    print(f"总成交量 : {rec['total_volume']:,} 股")
    print(f"标的数量 : {rec['stock_count']} 只")


def run(codes=None, ctx=None):
    """采集入口（常驻调用兼容）。codes 可为代码列表，空则全港股。"""
    if not codes:
        codes = _hk_code_list()
    rec = fetch_market_snapshot_batch(codes, ctx)
    if rec.get("trade_date"):
        save_to_db(rec)
        _save_quotes(rec.get("quote_rows", []))
    _print(rec)
    return rec


if __name__ == "__main__":
    import os
    dry = "--dry-run" in sys.argv
    codes = _hk_code_list()
    rec = fetch_market_snapshot_batch(codes)
    _print(rec)
    if rec.get("trade_date") and not dry:
        save_to_db(rec)
        _save_quotes(rec.get("quote_rows", []))
    # 独立运行：共享上下文由 collector_runtime 管理（常驻不关闭），
    # 富途 SDK 非 daemon 线程会导致进程挂住，这里显式退出。
    os._exit(0)
