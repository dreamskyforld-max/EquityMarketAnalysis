#!/usr/bin/env python3
"""
融资融券全量日度明细采集（A股沪深两市专用，常驻调用版）

数据源：东方财富 datacenter-web 接口 RPTA_WEB_RZRQ_GGMX（一次请求拉全市场两融明细，
       服务端强制每页最多 500 条，故按 pageNumber 翻页直到空页）。
       覆盖沪市 + 深市全标的、融资 + 融券双向全字段，且沪市 RQYE（融券余额金额）、
       深市 RZCHE/RZJME（融资偿还额/融资净买入）均有值 —— 补齐原 AKShare 源的两处缺口。

采集范围：最近一个交易日（自动向前回溯，跳过周末/休市/空数据日）
           全市场所有标的（约 4400+ 只/日），含融资 + 融券双向全字段。

入库表：daily_margin_balance
    (stock_code, trade_date) 唯一约束，upsert 幂等，每日定时执行可重复运行。

调用方式：
    - 常驻进程：run_module("get_margin_balance.py")  → run(None, ctx)
    - 独立运行：python3 get_margin_balance.py [--date YYYY-MM-DD] [--dry-run]
                --date 指定交易日（默认自动取最近一个已披露数据的交易日）

注意：本脚本仅支持 A 股（SH/SZ）。港股（HK）无融资融券公开逐日明细，不在此覆盖。
"""
from datetime import date, datetime, timedelta
from typing import Any, Optional

import sys
import logging
import time
import argparse

import requests
import pandas as pd

sys.path.insert(0, "/Users/fredhan/mkt/EquityMarketAnalysis")
from db import get_conn, bulk_upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("get_margin_balance")

_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
}

# 东财字段 → 内部规范字段
_EM_COLS = {
    "SCODE": "stock_code",
    "SECNAME": "stock_name",
    "RZYE": "rz_balance",       # 融资余额
    "RZMRE": "rz_buy",          # 融资买入额
    "RZCHE": "rz_repay",        # 融资偿还额
    "RZJME": "rz_net",          # 融资净买入
    "RQYE": "rq_balance",       # 融券余额（金额，沪市约 13% 标的无融券→NULL）
    "RQMCL": "rq_sell",         # 融券卖出量
    "RQCHL": "rq_repay",        # 融券偿还量
    "RZRQYE": "rzrq_balance",   # 融资融券余额
}

_TABLE = "daily_margin_balance"
_COLS = [
    "stock_code", "trade_date", "rz_balance", "rz_buy", "rz_repay", "rz_net",
    "rq_balance", "rq_sell", "rq_repay", "rzrq_balance",
]
_NUMERIC = [c for c in _COLS if c not in ("stock_code", "trade_date")]
_CONFLICT = ("stock_code", "trade_date")

_PAGE_SIZE = 500          # 东财服务端硬限每页最多 500
_PAGE_TIMEOUT = 30
_PAGE_RETRIES = 3


def _prefix(code: str) -> Optional[str]:
    """6 位纯数字代码 → SH./SZ. 前缀；非法返回 None。"""
    code = str(code).strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("60", "68", "90")):   # 上交所（含科创板 688/689、B股 900）
        return "SH." + code
    if code.startswith(("00", "30", "20", "15", "11", "12")):  # 深交所
        return "SZ." + code
    return None


def fetch_day(trade_date: date, retries: int = _PAGE_RETRIES) -> pd.DataFrame:
    """拉取某交易日全市场融资融券明细（自动翻页），返回规范化 DataFrame。

    翻页：东财强制每页最多 500 条，需 pageNumber 递增直到空页。
    单页失败按 retries 退避重试；整体为空（周末/休市/未披露）返回空 DataFrame。
    """
    ds = trade_date.strftime("%Y-%m-%d")
    rows: list[dict] = []
    for attempt in range(retries):
        try:
            page = 1
            while True:
                params = {
                    "reportName": "RPTA_WEB_RZRQ_GGMX",
                    "columns": "ALL",
                    "filter": f"(DATE='{ds}')",
                    "pageSize": str(_PAGE_SIZE),
                    "pageNumber": str(page),
                    "sortColumns": "DATE",
                    "sortTypes": "-1",
                    "source": "WEB",
                    "client": "WEB",
                }
                r = requests.get(_EM_URL, params=params, headers=_EM_HEADERS, timeout=_PAGE_TIMEOUT)
                r.raise_for_status()
                res = r.json().get("result") or {}
                data = res.get("data") or []
                if not data:
                    break
                rows.extend(data)
                page += 1
                time.sleep(0.05)
            break
        except Exception as e:  # noqa: BLE001
            wait = 1.5 * (attempt + 1)
            log.warning(f"  {trade_date} 东财拉取失败(第{attempt+1}次): {e}，{wait:.1f}s 后重试")
            time.sleep(wait)
            rows = []
            continue
    if not rows:
        log.info(f"  {trade_date} 东财无数据（可能非交易日/未披露）")
        return pd.DataFrame()

    raw = pd.DataFrame(rows)
    norm = pd.DataFrame()
    for src, dst in _EM_COLS.items():
        if src in raw.columns:
            norm[dst] = raw[src]
    if "stock_code" not in norm.columns:
        return pd.DataFrame()
    # 代码前缀
    norm["stock_code"] = norm["stock_code"].map(_prefix)
    norm = norm.dropna(subset=["stock_code"])
    # 数值清洗
    for c in _NUMERIC:
        if c in norm.columns:
            norm[c] = pd.to_numeric(norm[c], errors="coerce")
    # 融资净买入兜底（若源无 RZJME）
    if "rz_net" not in norm.columns and "rz_buy" in norm.columns and "rz_repay" in norm.columns:
        norm["rz_net"] = norm["rz_buy"] - norm["rz_repay"]
    log.info(f"  {trade_date} 东财解析 {len(norm)} 条")
    return norm


def find_last_trading_day(target: Optional[date] = None, max_lookback: int = 15) -> Optional[date]:
    """从 target（默认今天）向前回溯，找到第一个有数据的交易日。"""
    base = target or date.today()
    for i in range(max_lookback):
        d = base - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        df = fetch_day(d)
        if len(df) > 0:
            return d
    return None


def collect(trade_date: Optional[date] = None, dry: bool = False) -> int:
    """
    采集指定交易日的全市场融资融券明细，upsert 入库。
    返回入库行数（dry-run 时返回待入库行数）。
    """
    if trade_date is None:
        trade_date = find_last_trading_day()
    if trade_date is None:
        log.warning("未找到最近一个交易日（回看 15 天均无数据），跳过")
        return 0
    if trade_date.weekday() >= 5:
        log.info(f"{trade_date} 为周末，跳过")
        return 0
    log.info(f"融资融券全量采集（东财）：交易日 {trade_date}")

    df = fetch_day(trade_date)
    if len(df) == 0:
        log.warning(f"{trade_date} 沪深均无数据，跳过（可能当日未披露）")
        return 0

    df["trade_date"] = trade_date
    keep = [c for c in _COLS if c in df.columns]
    df = df[keep]
    for c in _COLS:
        if c not in df.columns:
            df[c] = float("nan")

    df = df.drop_duplicates(subset=["stock_code"], keep="first")

    rows = []
    for _, r in df.iterrows():
        row: dict[str, Any] = {}
        for c in _COLS:
            v = r.get(c, None)
            if c in _NUMERIC:
                try:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        row[c] = None
                    else:
                        row[c] = float(v)
                except (ValueError, TypeError):
                    row[c] = None
            else:
                row[c] = None if v is None else str(v)
        rows.append(row)

    if dry:
        log.info(f"[dry-run] 待入库 {len(rows)} 条")
        return len(rows)

    with get_conn() as conn:
        bulk_upsert(conn, _TABLE, rows, _CONFLICT)
    log.info(f"融资融券全量采集完成：入库 {len(rows)} 条（交易日 {trade_date}）")
    return len(rows)


def collect_recent(days: int = 7, dry: bool = False) -> int:
    """回填最近 days 个自然日内所有缺失的交易日数据（默认 7 天）。

    不依赖交易日历：查库得到该窗口内已有数据的日期，对其余每个日期直接调
    collect()；周末（weekday>=5）由 collect 内部跳过，非交易日/当日未披露
    （东财返回空）由 fetch_day 返回空 DataFrame 自然跳过。这样即使某次调度
    misfire 漏采，后续任一轮（09:20/18:20）都能把窗口内的空洞补回。

    只扫描自然日、不做交易日判断，是为了避免引入 trading_calendar 等多数据源
    依赖。代价是每天多探查若干本就无数据的日期（东财空返回开销很小）。

    返回窗口内新入库行数合计。
    """
    today = date.today()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT trade_date FROM daily_margin_balance "
                "WHERE trade_date >= %s AND trade_date <= %s",
                (today - timedelta(days=days - 1), today))
            have = {r[0] for r in cur.fetchall()}

    total = 0
    for i in range(days):
        d = today - timedelta(days=i)
        if d in have:
            continue
        total += collect(trade_date=d, dry=dry)
    return total


def run(codes=None, ctx=None, **kwargs) -> int:
    """常驻进程入口，供 collector_runtime.run_module 调用。

    每轮回填最近 7 个自然日内的缺失交易日（防漏采）。kwargs 的 trade_date
    由独立运行/手动补录使用，走原来的单日 collect 逻辑。
    """
    cli_date = kwargs.get("trade_date")
    if cli_date:
        td = datetime.strptime(cli_date, "%Y-%m-%d").date()
        return collect(trade_date=td)
    return collect_recent(days=7)


def main():
    p = argparse.ArgumentParser(description="融资融券全量日度明细采集（东财，A股沪深两市）")
    p.add_argument("--date", help="指定交易日 YYYY-MM-DD（默认自动取最近一个有数据的交易日）")
    p.add_argument("--dry-run", action="store_true", help="只解析不落库")
    args = p.parse_args()

    td: Optional[date] = None
    if args.date:
        td = datetime.strptime(args.date, "%Y-%m-%d").date()
    n = collect(trade_date=td, dry=args.dry_run)
    log.info(f"完成，处理 {n} 条" + ("（dry-run）" if args.dry_run else ""))


if __name__ == "__main__":
    main()
