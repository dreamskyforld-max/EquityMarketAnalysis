#!/usr/bin/env python3
"""
融资融券全量日度明细采集（A股沪深两市专用，常驻调用版）

数据源：AKShare（底层为上交所/深交所官网逐日公布的融资融券明细）
    - 沪市：ak.stock_margin_detail_sse(date=...)
    - 深市：ak.stock_margin_detail_szse(date=...)

采集范围：最近一个交易日（交易日历自动向前回溯，跳过周末/休市/空数据日）
           全市场所有标的（不再按单只代码过滤），含融资 + 融券双向全字段。

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

import akshare as ak
import pandas as pd

sys.path.insert(0, "/Users/fredhan/mkt/EquityMarketAnalysis")
from db import get_conn, bulk_upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("get_margin_balance")

# 字段重命名映射（不同市场列名不同，统一为内部规范名）
# 注意：沪市「融券余量」单位是股（非金额），深市「融券余额」单位是元 —— 量纲不同，
#       沪市无金额的融券余额字段，故 rq_balance 在沪市置 NULL（仅保留股数类字段）。
_SSE_COLS = {
    "标的证券代码": "stock_code",
    "标的证券简称": "stock_name",
    "融资余额": "rz_balance",
    "融资买入额": "rz_buy",
    "融资偿还额": "rz_repay",
    "融券卖出量": "rq_sell",        # 沪市（股）
    "融券余量": "rq_remain",        # 沪市（股，当日余量，仅供参考）
    "融券偿还量": "rq_repay",       # 沪市（股）
    # 沪市无「融资融券余额」「融券余额(金额)」字段 → rq_balance / rzrq_balance 置 NULL
}
_SZSE_COLS = {
    "证券代码": "stock_code",
    "证券简称": "stock_name",
    "融资余额": "rz_balance",
    "融资买入额": "rz_buy",
    # 注意：深市 AKShare 接口【无】融资偿还额 / 融券偿还量 列，
    #       故 rz_repay / rq_repay / rz_net 在深市恒为 NULL（无法从单日明细计算净买入）。
    "融券卖出量": "rq_sell",        # 深市（股）
    "融券余量": "rq_remain",        # 深市（股，当日余量，仅供参考，不入表）
    "融券余额": "rq_balance",       # 深市（元，金额）
    "融资融券余额": "rzrq_balance",  # 深市（元）
    # 深市无「融券偿还量」字段
}

_TABLE = "daily_margin_balance"
_COLS = [
    "stock_code", "trade_date", "rz_balance", "rz_buy", "rz_repay", "rz_net",
    "rq_balance", "rq_sell", "rq_repay", "rzrq_balance",
]
_NUMERIC = [c for c in _COLS if c not in ("stock_code", "trade_date")]
_CONFLICT = ("stock_code", "trade_date")


def _normalize(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """将交易所原始列统一规范化为内部字段，并补齐 stock_code 前缀。"""
    rule = _SSE_COLS if market == "SSE" else _SZSE_COLS
    out = pd.DataFrame()
    for src, dst in rule.items():
        if src in df.columns:
            out[dst] = df[src]
    if "stock_code" not in out.columns:
        return out
    # 代码前缀：交易所返回的是 6 位纯数字，需补 SH./SZ. 前缀
    prefix = "SH." if market == "SSE" else "SZ."
    out["stock_code"] = out["stock_code"].astype(str).str.strip()
    out["stock_code"] = prefix + out["stock_code"]
    # 数值字段清洗（去掉逗号等），非数字置 NULL
    for c in [v for v in rule.values() if v not in ("stock_code", "stock_name")]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c].astype(str).str.replace(",", "", regex=False), errors="coerce")
    # 融资净买入 = 融资买入额 - 融资偿还额（部分交易所明细未直接给出）
    if "rz_net" not in out.columns and "rz_buy" in out.columns and "rz_repay" in out.columns:
        out["rz_net"] = (out["rz_buy"] - out["rz_repay"])
    return out


def _call_akshare(market: str, ds: str):
    """在线程内执行 AKShare 调用（便于加硬超时，规避网络挂死）。"""
    if market == "SSE":
        return ak.stock_margin_detail_sse(date=ds)
    return ak.stock_margin_detail_szse(date=ds)


def fetch_market(market: str, trade_date: date, retries: int = 3, timeout: int = 25) -> pd.DataFrame:
    """拉取单个市场某交易日的全量融资融券明细。

    关键：AKShare 网络请求可能无限挂起（连接卡死不抛异常），因此把调用放进
    线程并加硬超时。超时后必须立即丢弃该线程池（shutdown(wait=False,
    cancel_futures=True)），否则 with 退出会等待卡死线程 → 整体仍卡死。
    卡死的 worker 线程变为孤儿 daemon，由解释器退出时回收，不影响后续重试。
    """
    from concurrent.futures import ThreadPoolExecutor
    ds = trade_date.strftime("%Y%m%d")
    last_err = None
    for attempt in range(retries):
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_call_akshare, market, ds)
            df = fut.result(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 1.5 * (attempt + 1)
            log.warning(f"  {market} {trade_date} 拉取失败(第{attempt+1}次){'超时' if isinstance(e, TimeoutError) else ''}: {e}，{wait:.1f}s 后重试")
            time.sleep(wait)
            continue
        finally:
            # 关键：不等待卡死线程，直接丢弃线程池
            ex.shutdown(wait=False, cancel_futures=True)
        if df is None or len(df) == 0:
            log.info(f"  {market} {trade_date} 无数据（可能非交易日/未披露）")
            return pd.DataFrame()
        norm = _normalize(df, market)
        log.info(f"  {market} {trade_date} 解析 {len(norm)} 条")
        return norm
    log.warning(f"  {market} {trade_date} 重试 {retries} 次仍失败: {last_err}")
    return pd.DataFrame()


def find_last_trading_day(target: Optional[date] = None, max_lookback: int = 15) -> Optional[date]:
    """从 target（默认今天）向前回溯，找到第一个有数据的交易日（两侧市场任一有数据即算）。"""
    base = target or date.today()
    for i in range(max_lookback):
        d = base - timedelta(days=i)
        if d.weekday() >= 5:  # 跳过周末
            continue
        sse = fetch_market("SSE", d)
        szse = fetch_market("SZSE", d)
        if len(sse) > 0 or len(szse) > 0:
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
    # 跳过周六日：交易所周末不披露融资融券明细，避免无效网络请求与重试
    if trade_date.weekday() >= 5:
        log.info(f"{trade_date} 为周末，跳过")
        return 0
    log.info(f"融资融券全量采集：交易日 {trade_date}")

    # 并发拉取沪深两市，缩短当日耗时（网络 IO 并行）
    # 任一侧卡死/异常时，整体超时返回 0（当日记为失败待补，不中断补录）
    from concurrent.futures import ThreadPoolExecutor
    sse: pd.DataFrame = pd.DataFrame()
    szse: pd.DataFrame = pd.DataFrame()
    ex = ThreadPoolExecutor(max_workers=2)
    try:
        sse_f = ex.submit(fetch_market, "SSE", trade_date)
        szse_f = ex.submit(fetch_market, "SZSE", trade_date)
        sse = sse_f.result(timeout=120)   # 单侧最坏 3×25s 重试 + 余量
        szse = szse_f.result(timeout=120)
    except Exception as e:  # noqa: BLE001
        log.warning(f"{trade_date} 并发拉取异常（可能某市场卡死）：{e}，当日跳过待补")
        return 0
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if len(sse) == 0 and len(szse) == 0:
        log.warning(f"{trade_date} 沪市/深市均无数据，跳过（可能当日未披露）")
        return 0

    frames = []
    for df in (sse, szse):
        if len(df) == 0:
            continue
        df["trade_date"] = trade_date
        keep = [c for c in _COLS if c in df.columns]
        frames.append(df[keep])
    if not frames:
        return 0
    all_df = pd.concat(frames, ignore_index=True)

    for c in _COLS:
        if c not in all_df.columns:
            all_df[c] = float("nan")

    all_df = all_df.drop_duplicates(subset=["stock_code"], keep="first")

    rows = []
    for _, r in all_df.iterrows():
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


def run(codes=None, ctx=None, **kwargs) -> int:
    """
    常驻进程入口，供 collector_runtime.run_module 调用。
    每日定时执行：采集最近一个交易日全市场融资融券明细。
    """
    cli_date = kwargs.get("trade_date")
    td: Optional[date] = None
    if cli_date:
        td = datetime.strptime(cli_date, "%Y-%m-%d").date()
    return collect(trade_date=td)


def main():
    p = argparse.ArgumentParser(description="融资融券全量日度明细采集（A股沪深两市）")
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
