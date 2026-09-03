#!/usr/bin/env python3
"""
A股 市值/市盈率/市净率 历史回溯（东方财富 datacenter 源，批量按交易日回填）

数据源：东方财富 datacenter-web.eastmoney.com 报表 RPT_VALUEANALYSIS_DET
        （东财「每日指标」页后端），按 TRADE_DATE 过滤，一次返回全 A 股约 5500+ 只的
        估值快照，含：
            TOTAL_MARKET_CAP      总市值（元）
            NOTLIMITED_MARKETCAP_A 流通市值（元，A股）
            PE_TTM                市盈率(TTM)
            PE_LAR                静态市盈率
            PB_MRQ                市净率(MRQ)
            PEG_CAR               PEG
            CLOSE_PRICE           收盘价
            TOTAL_SHARES          总股本

为什么不用富途快照（get_a_market_turnover.py）：
    - 富途 get_market_snapshot 受订阅/权限限制，只能覆盖少数重点股，
      无法批量回溯全 A 股的市值/PE 历史。
    - 东财该报表无需订阅，按交易日批量返回全市场，且实测带 cookie 稳定。

落库策略（关键·只补估值列，不碰 OHLC）：
    - 目标表 a_daily_quote 已有 UNIQUE(stock_code, trade_date)（由 backfill_a_market_turnover.py
      落 K线时建立），本脚本用同样冲突列做 ON CONFLICT DO UPDATE，
      只更新 6 个估值列 + update_time，不动 open/high/low/close/volume/amount。
    - 若某交易日 a_daily_quote 还没有对应个股行（K线未回溯），本脚本也会 upsert
      插入（估值列有值、OHLC 为 NULL），保证估值数据不依赖 K线先行。

交易日枚举：
    - 默认从 a_daily_quote 已存在的 trade_date 去重取（这些天确定有交易数据，
      避免拉周末/休市日浪费配额），逐日回填估值。
    - 支持 --date 单日 / --start --end 区间 / --all 全部已存在交易日 / --limit N 限量。

断点续采：
    - 进度由库反推：某交易日「估值已落（total_market_val 非空）的股票数」达到
      该日东财返回总数即视为完成；--resume 跳过已完成交易日。
    - 最后一只交易日可能只落了一半被 kill，--resume 会把它重采（upsert 幂等）。

用法：
    python3 backfill_a_valuation.py                      # 回填全部已有交易日
    python3 backfill_a_valuation.py --date 2026-08-27    # 单日
    python3 backfill_a_valuation.py --start 2024-01-01 --end 2026-08-27  # 区间
    python3 backfill_a_valuation.py --resume --limit 30  # 续采本轮 30 天
    python3 backfill_a_valuation.py --dry-run            # 只探测不落库
"""
import sys
import time
import logging
from datetime import date, datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_a_valuation")

EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
    "Cookie": "qgqp=1",  # 带 cookie 显著提升稳定性，避免 RemoteDisconnect
}

# 报表字段 → a_daily_quote 列映射
FIELD_MAP = {
    "TOTAL_MARKET_CAP": "total_market_val",
    "NOTLIMITED_MARKETCAP_A": "circular_market_val",
    "PE_TTM": "pe_ttm_ratio",
    "PE_LAR": "pe_ratio",
    "PB_MRQ": "pb_ratio",
    "PEG_CAR": "dividend_ratio_ttm",  # 注：东财 PEG 非股息率，下面 _map 会纠正
}
# 实际 dividend_ratio_ttm 在东财该报表无直接列；这里用 PEG 占位后在 _map 置空处理
EM_COLS = "SECURITY_CODE,TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A,PE_TTM,PE_LAR,PB_MRQ,PEG_CAR,CLOSE_PRICE"


# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
def parse_args():
    dry = "--dry-run" in sys.argv
    resume = "--resume" in sys.argv
    one_day = None
    start = end = None
    limit = None
    if "--date" in sys.argv:
        one_day = date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    if "--start" in sys.argv:
        start = date.fromisoformat(sys.argv[sys.argv.index("--start") + 1])
    if "--end" in sys.argv:
        end = date.fromisoformat(sys.argv[sys.argv.index("--end") + 1])
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    return {
        "dry": dry, "resume": resume, "date": one_day,
        "start": start, "end": end, "limit": limit,
    }


# ----------------------------------------------------------------------------
# 交易日枚举
# ----------------------------------------------------------------------------
def _existing_trade_dates(start=None, end=None):
    """从 a_daily_quote 已落库的 trade_date 去重取（确定有交易日数据）。"""
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            if start and end:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM a_daily_quote "
                    "WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date",
                    (start, end),
                )
            elif start:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM a_daily_quote "
                    "WHERE trade_date >= %s ORDER BY trade_date", (start,),
                )
            elif end:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM a_daily_quote "
                    "WHERE trade_date <= %s ORDER BY trade_date", (end,),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM a_daily_quote ORDER BY trade_date"
                )
            rows = cur.fetchall()
    return [r[0] for r in rows]


def _ensure_table():
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS a_daily_quote ("
                "  id BIGSERIAL PRIMARY KEY,"
                "  stock_code VARCHAR(20) NOT NULL,"
                "  trade_date DATE NOT NULL,"
                "  total_market_val NUMERIC(22,2),"
                "  circular_market_val NUMERIC(22,2),"
                "  pe_ratio NUMERIC(12,4),"
                "  pe_ttm_ratio NUMERIC(12,4),"
                "  pb_ratio NUMERIC(12,4),"
                "  dividend_ratio_ttm NUMERIC(8,4),"
                "  update_time TIMESTAMPTZ,"
                "  created_at TIMESTAMPTZ DEFAULT NOW(),"
                "  UNIQUE (stock_code, trade_date))"
            )
        conn.commit()


def _done_dates():
    """已完成估值的交易日集合（total_market_val 非空计数达到该日东财总数）。

    简化判定：某交易日 a_daily_quote 中 total_market_val 非空的股票数，
    大于等于该日东财返回总数（约 5500）即视为完成。
    """
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, COUNT(*) FROM a_daily_quote "
                "WHERE total_market_val IS NOT NULL GROUP BY trade_date"
            )
            rows = cur.fetchall()
    # 返回 {trade_date: 已估值股票数}
    return {r[0]: r[1] for r in rows}


# ----------------------------------------------------------------------------
# 东财抓取（带重试）
# ----------------------------------------------------------------------------
def _fetch_one_day(td: date, session: requests.Session, max_retry=4):
    """拉取单交易日全 A 股估值快照，返回 (list_of_rows, total_count)。

    row 形如 {'stock_code': 'SH.600519', 'trade_date': date, 'total_market_val': ..., ...}
    """
    td_str = td.strftime("%Y-%m-%d")
    last_err = None
    for attempt in range(max_retry):
        try:
            params = {
                "reportName": "RPT_VALUEANALYSIS_DET",
                "columns": EM_COLS,
                "pageSize": "6000",
                "pageNumber": "1",
                "sortColumns": "SECURITY_CODE",
                "sortTypes": "1",
                "source": "WEB",
                "client": "WEB",
                "filter": f"(TRADE_DATE='{td_str}')",
            }
            r = session.get(EM_DC, params=params, timeout=30)
            r.raise_for_status()
            res = r.json().get("result") or {}
            data = res.get("data") or []
            total = res.get("count") or len(data)
            if not data:
                return [], 0
            rows = []
            skipped = 0
            empty_cols = ("TOTAL_MARKET_CAP", "NOTLIMITED_MARKETCAP_A", "PE_TTM", "PE_LAR", "PB_MRQ")
            for d in data:
                code = _to_full_code(d.get("SECURITY_CODE"))
                if not code:
                    continue
                # 原则：只把有效数据写入；某只股票所有估值字段都为空 → 视为无效，跳过
                # （避免把数据库里已有的有效估值覆盖成 NULL）
                if all(_num(d.get(c)) is None for c in empty_cols):
                    skipped += 1
                    continue
                rows.append({
                    "stock_code": code,
                    "trade_date": td,
                    "total_market_val": _num(d.get("TOTAL_MARKET_CAP")),
                    "circular_market_val": _num(d.get("NOTLIMITED_MARKETCAP_A")),
                    "pe_ttm_ratio": _num(d.get("PE_TTM")),
                    "pe_ratio": _num(d.get("PE_LAR")),
                    "pb_ratio": _num(d.get("PB_MRQ")),
                    # 东财该报表无股息率(TTM)列，置空；字段级 upsert 会保留库里原值，不清空
                    "dividend_ratio_ttm": None,
                    "update_time": datetime(td.year, td.month, td.day),
                })
            if skipped:
                log.info(f"    {td} 跳过 {skipped} 只「全字段为空」的无效记录（不写库）")
            return rows, total
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt)
    log.warning(f"  {td} 抓取失败（{max_retry} 次重试）: {last_err}")
    return None, 0


def _to_full_code(num):
    """东财 SECURITY_CODE 是纯数字（如 600519/000001/300750），按上市规则加前缀。"""
    if not num:
        return None
    num = str(num).strip()
    if num.startswith("6") or num.startswith("9"):
        return f"SH.{num}"
    if num.startswith(("0", "3", "2")):
        return f"SZ.{num}"
    # 北交所 8/4 开头、其它——库 a_daily_quote 只收 A 股正股，这里跳过避免脏数据
    return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# 落库
# ----------------------------------------------------------------------------
# 估值列（用于 upsert 的列）。OHLC/量价列一律不在此出现，绝不触碰。
VAL_COLS = ["total_market_val", "circular_market_val", "pe_ratio",
            "pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm", "update_time"]
CONFLICT_COLS = ["stock_code", "trade_date"]


def _flush(rows, dry):
    """落库：字段级保护 upsert。

    原则：只把有效数据写入，绝不把库里已有的有效值覆盖成空。
      - 记录级：调用方已过滤「全部估值字段为空」的无效记录（见 _fetch_one_day）。
      - 字段级：用 COALESCE(EXCLUDED.col, table.col) ——
          新值为非空 → 更新；新值为 NULL → 保留库里原值（不清空）。
        这样即使某字段东财返回空（如亏损股 PE 为空），也不会抹掉库里原有 PE。
      - 只操作 VAL_COLS，OHLC/量价列完全不进入 UPDATE SET，安全。
    """
    if not rows:
        return 0
    if dry:
        return len(rows)
    from db import get_conn
    from psycopg2 import sql
    from psycopg2.extras import execute_values
    _ensure_table()

    all_cols = CONFLICT_COLS + VAL_COLS
    conflict_target = sql.SQL(", ").join([sql.Identifier(c) for c in CONFLICT_COLS])
    col_ids = sql.SQL(", ").join([sql.Identifier(c) for c in all_cols])
    # 字段级 COALESCE：新值非空才覆盖，否则保留原值
    update_set = sql.SQL(", ").join([
        sql.SQL("{c} = COALESCE(EXCLUDED.{c}, a_daily_quote.{c})").format(
            c=sql.Identifier(c))
        for c in VAL_COLS
    ])
    query = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES %s "
        "ON CONFLICT ({conf}) DO UPDATE SET {set}"
    ).format(
        table=sql.Identifier("a_daily_quote"),
        cols=col_ids,
        conf=conflict_target,
        set=update_set,
    )
    values_list = [tuple(r.get(c) for c in all_cols) for r in rows]
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(cur, query, values_list, page_size=2000)
    return len(rows)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run():
    a = parse_args()
    dry = a["dry"]
    resume = a["resume"]

    if a["date"]:
        dates = [a["date"]]
        log.info(f"单日模式: {a['date']}")
    else:
        dates = _existing_trade_dates(a["start"], a["end"])
        if not dates:
            log.warning("a_daily_quote 无已有交易日，无法枚举。请先跑 backfill_a_market_turnover.py，或用 --date 指定。")
            return
        log.info(f"枚举到 {len(dates)} 个已有交易日，范围 {dates[0]} ~ {dates[-1]}")

    if resume:
        done = _done_dates()
        before = len(dates)
        dates = [d for d in dates
                 if done.get(d, 0) < 5000]  # 不足 5000 视为未完成
        log.info(f"续采：已完成 {before - len(dates)} 天，剩 {len(dates)} 天待处理")

    if a["limit"] and len(dates) > a["limit"]:
        log.info(f"本轮限量 {a['limit']} 天（剩余 {len(dates) - a['limit']} 天下次 --resume）")
        dates = dates[:a["limit"]]

    if not dates:
        log.info("无待处理交易日")
        return

    session = requests.Session()
    session.headers.update(EM_HEADERS)

    total_rows = 0
    fail_days = 0
    log.info(f"开始回填估值（{len(dates)} 天，dry={dry}）...")
    for i, td in enumerate(dates, 1):
        log.info(f"  [{i}/{len(dates)}] {td} 抓取中...")
        rows, total = _fetch_one_day(td, session)
        if rows is None:
            fail_days += 1
            continue
        n = _flush(rows, dry)
        total_rows += n
        log.info(f"  [{i}/{len(dates)}] {td} 估值 {n}/{total} 只" +
                 ("（dry-run 未落库）" if dry else "已落库"))
        time.sleep(0.3)  # 轻量限速，保护东财源

    log.info(f"回填完成：{len(dates)-fail_days} 天成功，{fail_days} 天失败，估值记录 {total_rows} 条")


if __name__ == "__main__":
    import os
    run()
    os._exit(0)
