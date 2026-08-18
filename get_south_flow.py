#!/usr/bin/env python3
"""
南向资金采集 — AKShare 港股通持股（批量全市场版），写入 daily_ggt_hold

数据源：东方财富 → AKShare stock_hsgt_stock_statistics_em(symbol="南向持股")
批量能力：一次返回全市场（约 1200+ 只港股通标的）按日期区间的持股明细，
          无需逐只调用，规避并发限流。

字段映射（接口 → daily_ggt_hold）：
  股票代码 → stock_code（补 HK. 前缀）
  持股日期 → trade_date
  持股数量 → hold_num
  持股数量占发行股百分比 → hold_ratio
  当日收盘价 → close_price
  当日涨跌幅 → change_pct
  持股市值 → hold_value（新增）
  持股市值变化-1日/-5日/-10日 → hold_value_change_1d/5d/10d（新增）
  hold_num_change / hold_ratio_change / est_net_inflow：入库后由 recalc_from_db()
  基于表内全量数据窗口重算（单一真相源，防批次缺失）

常驻调用：run(codes, ctx)，codes 忽略（全局一次）；__main__ 保留独立运行。
"""
import sys
import logging
from datetime import date, datetime, timedelta

import akshare as ak

from db import get_conn, bulk_upsert

# 采集最近几个交易日的持股数据（日期区间）
FETCH_DAYS = 10

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("south_flow")
VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv


def log(msg):
    if VERBOSE:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _to_date_str(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _num(v):
    """转 float，None/NaN/- → None"""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN → None
    except (TypeError, ValueError):
        return None


def fetch_market_south_flow(start_date=None, end_date=None, fetch_days=FETCH_DAYS):
    """批量拉取全市场南向持股，返回按 (stock_code, trade_date) 的明细列表。

    start_date/end_date: date 或 'YYYY-MM-DD' 字符串；None 时默认最近 fetch_days 个自然日。
    """
    if end_date is None:
        end = date.today()
    elif hasattr(end_date, "isoformat"):
        end = end_date
    else:
        end = date.fromisoformat(str(end_date))
    if start_date is None:
        start = end - timedelta(days=fetch_days + 15)  # 留足自然日余量覆盖交易日
    elif hasattr(start_date, "isoformat"):
        start = start_date
    else:
        start = date.fromisoformat(str(start_date))
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    try:
        df = ak.stock_hsgt_stock_statistics_em(
            symbol="南向持股", start_date=start_s, end_date=end_s
        )
    except Exception as e:
        log(f"AKShare 批量接口异常: {e}")
        return []

    if df is None or df.empty:
        log("AKShare 批量接口返回空")
        return []

    rows = []
    for _, r in df.iterrows():
        code = str(r.get("股票代码", "")).strip()
        if not code:
            continue
        rows.append({
            "stock_code": f"HK.{code}",
            "trade_date": _to_date_str(r.get("持股日期")),
            "hold_num": _num(r.get("持股数量")),
            "hold_ratio": _num(r.get("持股数量占发行股百分比")),
            "close_price": _num(r.get("当日收盘价")),
            "change_pct": _num(r.get("当日涨跌幅")),
            "hold_value": _num(r.get("持股市值")),
            "hold_value_change_1d": _num(r.get("持股市值变化-1日")),
            "hold_value_change_5d": _num(r.get("持股市值变化-5日")),
            "hold_value_change_10d": _num(r.get("持股市值变化-10日")),
        })
    return rows


def save_to_db(rows):
    if not rows:
        return 0
    db_data = []
    for r in rows:
        try:
            td = date.fromisoformat(r["trade_date"])
        except (ValueError, TypeError):
            continue
        db_data.append({
            "stock_code": r["stock_code"],
            "trade_date": td,
            "hold_num": r.get("hold_num"),
            "hold_ratio": r.get("hold_ratio"),
            "hold_num_change": r.get("hold_num_change"),
            "hold_ratio_change": None,
            "close_price": r.get("close_price"),
            "change_pct": r.get("change_pct"),
            "est_net_inflow": r.get("est_net_inflow"),
            "hold_value": r.get("hold_value"),
            "hold_value_change_1d": r.get("hold_value_change_1d"),
            "hold_value_change_5d": r.get("hold_value_change_5d"),
            "hold_value_change_10d": r.get("hold_value_change_10d"),
        })
    with get_conn() as conn:
        bulk_upsert(conn, "daily_ggt_hold", db_data,
                    conflict_cols=["stock_code", "trade_date"])
    return len(db_data)


def recalc_from_db(table="daily_ggt_hold"):
    """入库后基于表内全量数据窗口重算差值（单一真相源，防复发）。

    相比 Python 侧跨日计算（只算同批次内相邻日期），窗口函数按
    (stock_code, trade_date) 全表排序取 LAG，不受「本次批次只覆盖部分日期」
    影响 —— 历史批次起点、缺日、回填补采后的差值都会自动补齐。
    table 参数仅供测试注入临时表。
    """
    sql = f"""
        WITH ranked AS (
            SELECT id, hold_num, hold_ratio, close_price,
                   LAG(hold_num)  OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_hold,
                   LAG(hold_ratio) OVER (PARTITION BY stock_code ORDER BY trade_date) AS prev_ratio
            FROM {table}
        )
        UPDATE {table} g
        SET hold_num_change = CASE WHEN g.hold_num IS NOT NULL
                                   THEN ranked.hold_num - ranked.prev_hold ELSE NULL END,
            hold_ratio_change = CASE WHEN g.hold_ratio IS NOT NULL AND ranked.prev_ratio IS NOT NULL
                                     THEN round(g.hold_ratio - ranked.prev_ratio, 4) ELSE NULL END,
            est_net_inflow = CASE WHEN ranked.prev_hold IS NOT NULL AND g.hold_num IS NOT NULL
                                       AND g.close_price IS NOT NULL
                                  THEN round((g.hold_num - ranked.prev_hold) * g.close_price / 1e8, 2)
                                  ELSE NULL END
        FROM ranked
        WHERE g.id = ranked.id AND ranked.prev_hold IS NOT NULL
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            n = cur.rowcount
    log(f"窗口重算差值: 更新 {n} 行（hold_num_change/hold_ratio_change/est_net_inflow）")
    return n


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。批量全市场一次，codes/ctx 忽略。"""
    log("通过 AKShare 批量获取全市场南向持股...")
    rows = fetch_market_south_flow()
    if not rows:
        print("南向资金数据获取失败（批量接口无数据）")
        return []

    n = save_to_db(rows)
    m = recalc_from_db()
    stocks = len({r["stock_code"] for r in rows})
    log(f"入库 {n} 条，覆盖 {stocks} 只港股通标的，窗口重算 {m} 行差值")
    print(f"南向资金批量采集完成: 入库 {n} 条，覆盖 {stocks} 只标的，窗口重算 {m} 行差值")
    return rows


if __name__ == "__main__":
    run()
