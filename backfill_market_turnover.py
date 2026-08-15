#!/usr/bin/env python3
"""
港股全市场总成交额 · 历史回溯（新浪 stock_hk_daily 版，一次性全量）

数据源：AKShare stock_hk_daily()（新浪财经港股个股日线，完整历史含 amount）
用途：遍历全港股，逐只拉历史日线成交额，按日 SUM → 全市场历史总成交额，
      回溯写入 daily_market_turnover；同时个股日线写入 hk_daily_quote。

为什么用新浪而不是富途历史 K 线：
  - 富途 request_history_kline 有「历史 K 线额度」限制（100 只/7天），
    全港股 2800+ 只需 28 周才能采完，无法做全量回溯。
  - 新浪 stock_hk_daily 无额度限制，能一次拿完整历史（仅耗时较长）。

字段说明：新浪仅返回基础量价（date/open/high/low/close/volume/amount），
  估值字段（市值/PE/PB/换手率等）新浪不提供，统一置空（NULL），
  这些字段由日常采集（get_hk_market_turnover.py 富途快照）当天补上。

用法：
    python3 backfill_market_turnover.py --years 3                 # 全港股回溯近 3 年
    python3 backfill_market_turnover.py --start 2023-01-01        # 全港股，指定起始日期
    python3 backfill_market_turnover.py --code HK.01857           # 仅采集单只股票（可省略 HK. 前缀）
    python3 backfill_market_turnover.py --code 01857 --start 2023-01-01  # 单只 + 起始日期
    python3 backfill_market_turnover.py --code HK.01857 --dry-run # 只看不落库

耗时：全港股 2800+ 只 × 逐只拉历史，约 40-60 分钟。
"""
import sys
import time
import logging
from datetime import datetime, date

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill_market_turnover")


def parse_args():
    dry = "--dry-run" in sys.argv
    start = None
    code = None
    if "--code" in sys.argv:
        i = sys.argv.index("--code")
        code = sys.argv[i + 1]
        if not code.startswith("HK."):
            code = f"HK.{code}"
    if "--years" in sys.argv:
        i = sys.argv.index("--years")
        years = int(sys.argv[i + 1])
        start = date(datetime.now().year - years, 1, 1)
    elif "--start" in sys.argv:
        i = sys.argv.index("--start")
        start = date.fromisoformat(sys.argv[i + 1])
    if start is None:
        start = date(datetime.now().year - 3, 1, 1)
    return dry, start, code


def _hk_code_list():
    # 复用 get_hk_market_turnover 的缓存版（带 7 天数据库缓存）
    from get_hk_market_turnover import _hk_code_list as _cached
    return _cached()


def _fetch_daily(sym):
    """新浪 stock_hk_daily 拉单只完整历史，返回 DataFrame（date 已转 date 类型）。"""
    import akshare as ak
    df = ak.stock_hk_daily(symbol=sym, adjust="")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    return df


def fetch_history(start: date, code: str | None = None):
    """逐只拉历史日线，返回 (总成交额 DataFrame, 个股日线列表)。

    code: 指定单只股票（如 HK.01857），None=全港股。
    """
    if code:
        codes = [code]
    else:
        codes = _hk_code_list()
    if not codes:
        log.warning("获取港股代码列表失败")
        return pd.DataFrame(), []

    agg = {}
    quote_rows = []
    fail = 0

    log.info(f"开始回溯 {len(codes)} 只港股历史日线（起始 {start}）...")
    for i, raw in enumerate(codes, 1):
        full = raw if raw.startswith("HK.") else f"HK.{raw}"  # 统一带 HK. 前缀
        sym = full.split(".")[-1]  # 纯数字，如 01857
        log.info(f"  [{i}/{len(codes)}] {full} 开始采集...")
        try:
            df = _fetch_daily(sym)
            if df.empty:
                fail += 1
                log.info(f"  [{i}/{len(codes)}] {full} 无数据")
                continue
            df = df[df["trade_date"] >= start]
            for _, r in df.iterrows():
                td = r["trade_date"]
                amt = float(r["amount"]) if r.get("amount") is not None else 0.0
                vol = int(r["volume"]) if r.get("volume") is not None else 0
                a = agg.setdefault(td, {"turnover": 0.0, "volume": 0, "stocks": set()})
                a["turnover"] += amt
                a["volume"] += vol
                a["stocks"].add(sym)
                quote_rows.append(_map_quote_row(full, td, r))
            log.info(f"  [{i}/{len(codes)}] {full} 完成，{len(df)} 条日线")
        except Exception as e:
            fail += 1
            if fail <= 5:
                log.warning(f"  [{i}/{len(codes)}] {full} 失败: {e}")
        time.sleep(0.1)

    log.info(f"回溯完成：成功 {len(codes)-fail} 只，失败 {fail} 只，交易日 {len(agg)} 个")

    rows = []
    for td in sorted(agg):
        a = agg[td]
        rows.append({
            "trade_date": td,
            "total_turnover": a["turnover"],
            "total_volume": a["volume"],
            "stock_count": len(a["stocks"]),
        })
    return pd.DataFrame(rows), quote_rows


def _map_quote_row(code, td, row):
    """新浪 stock_hk_daily 行 → hk_daily_quote 字段（估值字段置空）。"""
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "stock_code": code,
        "trade_date": td,
        "open": _f(row.get("open")),
        "high": _f(row.get("high")),
        "low": _f(row.get("low")),
        "close": _f(row.get("close")),
        "volume": int(row["volume"]) if row.get("volume") is not None else 0,
        "amount": _f(row.get("amount")),
        # 新浪不提供估值/换手率/量比，置空；由日常富途快照补上
        "turnover_rate": None,
        "volume_ratio": None,
        "high_52w": None,
        "low_52w": None,
        "total_market_val": None,
        "circular_market_val": None,
        "pe_ratio": None,
        "pe_ttm_ratio": None,
        "pb_ratio": None,
        "dividend_ratio_ttm": None,
        # 历史日线无实时更新时间，用交易日作为数据更新时间
        "update_time": str(td),
    }


def save_to_db(df: pd.DataFrame):
    from db import get_conn, bulk_upsert
    if df.empty:
        return
    now = datetime.now()
    data = []
    for _, r in df.iterrows():
        data.append({
            "trade_date": r["trade_date"],
            "snapshot_time": now,
            "total_turnover": r["total_turnover"],
            "total_volume": int(r["total_volume"]),
            "stock_count": int(r["stock_count"]),
        })
    # 表不存在时自动创建（复用 get_hk_market_turnover 的 DDL）
    from get_hk_market_turnover import _ensure_table
    _ensure_table("daily_market_turnover")
    with get_conn() as conn:
        bulk_upsert(conn, "daily_market_turnover", data, conflict_cols=["trade_date"])
    log.info(f"落库 {len(data)} 个交易日")


def _save_quotes(quote_rows):
    if not quote_rows:
        return
    from db import get_conn, bulk_upsert
    from get_hk_market_turnover import _ensure_table
    _ensure_table("hk_daily_quote")
    batch = 5000
    with get_conn() as conn:
        for i in range(0, len(quote_rows), batch):
            bulk_upsert(conn, "hk_daily_quote", quote_rows[i:i + batch],
                        conflict_cols=["stock_code", "trade_date"])
    log.info(f"个股日线落库 {len(quote_rows)} 条（hk_daily_quote）")


def run():
    dry, start, code = parse_args()
    log.info(f"回溯起始日期: {start}，dry_run={dry}，code={code or '全港股'}")
    df, quote_rows = fetch_history(start, code)
    if df.empty:
        log.warning("无数据")
        return
    print(df.sort_values("trade_date").tail(10).to_string(index=False))
    if not dry:
        save_to_db(df)
        _save_quotes(quote_rows)
    log.info("完成")


if __name__ == "__main__":
    import os
    run()
    # 独立运行：新浪/akshare 可能残留非 daemon 线程，显式退出。
    os._exit(0)
