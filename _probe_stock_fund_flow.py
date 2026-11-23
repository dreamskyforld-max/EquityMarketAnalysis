#!/usr/bin/env python3
"""探测个股资金流源：可用性 / 覆盖率 / 历史深度（2026-11-23 实测）。

背景：同花顺行业资金流（约 90 细分）映射到申万 31 有口径损失；若个股级资金流
可用，则可用 regime.stock_sw_industry_mapping（申万 498 行业成分，5223 只）自算。

实测结论：
  1. 同花顺 stock_fund_flow_individual(symbol="即时") —— 可用、覆盖率 99.8%。
     当日 5212 只，与申万成分映射表 5223 只交集 5211（缺 12 只），耗时约 9s。
     字段：股票代码/简称/最新价/涨跌幅/换手率/流入资金/流出资金/净额/成交额。
     但只有「即时 / 3日 / 5日 / 10日 / 20日排行」快照，**无历史序列**，与行业
     资金流同源同约束（历史只能逐日累积、漏采即永久缺失）。金额是带单位的
     字符串（如「7618.30万」「1.35亿」），需解析。
  2. 东财 stock_individual_fund_flow(stock, market) —— 首次调用可读历史，但不稳定。
     600519 / 000001 均返回 121 行，日期 2026-05-28~2026-11-23（约 6 个月）。
     字段：日期/收盘价/涨跌幅/主力·超大单·大单·中单·小单 净额与净占比（13 列）。
     源码 lmt="0" 即取全部 → 121 行就是源给的全部，**无法拉更早历史**。
     复测（含直连 push2his）连续 ConnectionError（RemoteDisconnected），疑似反爬/限流，
     与项目既有「东财 push2 系两轮探测均断连」结论一致，不宜作为生产源。
  3. 东财 stock_individual_fund_flow_rank(今日) —— ConnectionError（push2 断连）。
  4. 基准：stock_info_a_code_name 全市场 5581 只；申万成分映射表 5223 只。

结论：个股资金流「当日快照」可由同花顺稳定获取且几乎全覆盖申万成分；但两源都
无历史（同花顺）或只有约半年且不稳定（东财），无法回填 2026-09 之前的申万行业
资金流。若要自算申万口径，只能从上线日起逐日累积。

用法: python3 _probe_stock_fund_flow.py [--step list|ths|em|all]
"""
import argparse
import socket
import time
import warnings

import akshare as ak
import pandas as pd

warnings.filterwarnings("ignore")
socket.setdefaulttimeout(20)


def _call(label, fn):
    t = time.monotonic()
    try:
        df = fn()
        print(f"[OK] {label}: {time.monotonic() - t:.1f}s shape={df.shape}")
        print(f"  cols: {list(df.columns)[:24]}")
        print(df.head(2).to_string(index=False, max_colwidth=26))
        return df
    except Exception as exc:
        print(f"[FAIL] {label}: {time.monotonic() - t:.1f}s "
              f"{type(exc).__name__}: {exc}")
        return None


def _norm_code(c):
    c = str(c).strip()
    if c.startswith(("SH.", "SZ.", "BJ.")):
        c = c[3:]
    return c.replace(".0", "").zfill(6)


def step_list():
    print(f"akshare={ak.__version__}")
    for f in sorted(x for x in dir(ak) if "fund_flow" in x.lower()):
        print("  ", f)


def step_ths():
    """同花顺当日个股资金流 —— 可用性与对申万成分的覆盖率。"""
    df = _call("同花顺 stock_fund_flow_individual(即时)",
               lambda: ak.stock_fund_flow_individual(symbol="即时"))
    if df is None or df.empty:
        return
    ths = {_norm_code(c) for c in df["股票代码"]}
    print(f"  同花顺覆盖股票数（补零后）: {len(ths)}")
    try:
        from db import get_conn
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM regime.stock_sw_industry_mapping")
            sw = {_norm_code(r[0]) for r in cur.fetchall()}
        inter = ths & sw
        print(f"  申万成分映射表股票数: {len(sw)}；交集: {len(inter)} "
              f"({100 * len(inter) / len(sw):.1f}%)")
        print(f"  申万成分缺资金流: {len(sw - ths)} 示例: {sorted(sw - ths)[:12]}")
    except Exception as exc:
        print(f"  覆盖率计算失败: {type(exc).__name__}: {exc}")


def step_em():
    """东财个股资金流 —— 当日排名（push2）与历史（push2his）。"""
    _call("东财 stock_individual_fund_flow_rank(今日)",
          lambda: ak.stock_individual_fund_flow_rank(indicator="今日"))
    for code, market in [("600519", "sh"), ("000001", "sz")]:
        df = _call(f"东财 stock_individual_fund_flow({code},{market})",
                   lambda c=code, m=market: ak.stock_individual_fund_flow(stock=c, market=m))
        if df is not None and not df.empty:
            print(f"  历史: {df['日期'].min()} ~ {df['日期'].max()} ({len(df)} 行)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default="all", choices=["list", "ths", "em", "all"])
    args = ap.parse_args()
    if args.step in ("list", "all"):
        step_list()
    if args.step in ("ths", "all"):
        step_ths()
    if args.step in ("em", "all"):
        step_em()
