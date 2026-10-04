#!/usr/bin/env python3
"""行业资金流日频采集 —— 写入 regime.sector_fund_flow（第④层行业配置的原始数据）。

数据源：AKShare `stock_fund_flow_industry(symbol="即时")`（**同花顺** 行业资金流）。
    为什么不是东财：`stock_sector_fund_flow_rank`（东财 push2 系）实测持续
    `ConnectionError: Remote end closed connection without response`（反爬/断连，探测两轮均失败），
    同花顺源稳定可用 → 换源（方案 §4.4-B 记录的「需换源」项）。

口径与注意（实测）：
  · 源只提供「当前快照」（即时 / 3日 / 5日 / 10日 / 20日**排行**），**无历史序列**
    → 历史只能逐日累积；本脚本落「即时」＝当日累计，多日口径由计算层滚动求和得出。
  · 因此**必须收盘后采集**（调度 15:35）：盘中取到的即时值是不完整的当日累计。
  · 单位：流入/流出/净额均为**亿元**（源口径）。
  · 行业为**同花顺细分行业（约 90 个）**，与申万一级 31 个**不是同一套分类**
    → 跨表 join 需先建映射表（P2）；本表独立可用（行业资金流横截面 + 市场级聚合）。
  · trade_date 取「最近 A 股交易日」（读 public.trading_calendar）：节假日重跑只会幂等地
    覆盖同一交易日，不会把快照记成非交易日。

用法:
    python3 get_sector_fund_flow.py                 # 采集（调度用）
    python3 get_sector_fund_flow.py --dry-run
    python3 get_sector_fund_flow.py --date 2026-09-30
"""
import argparse
import datetime
import sys

import pandas as pd

from db import get_conn, bulk_upsert

TABLE = "regime.sector_fund_flow"
CONFLICT = ["trade_date", "sector_name"]
SOURCE = "akshare:stock_fund_flow_industry"
SYMBOL = "即时"


def _recent_cn_trading_date() -> datetime.date:
    """最近 A 股交易日（trading_calendar）；查不到回落今天（源快照仍可用）。"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT max(cal_date) FROM public.trading_calendar "
                            "WHERE market='CN' AND is_open AND cal_date <= CURRENT_DATE")
                d = cur.fetchone()[0]
                if d:
                    return d
    except Exception as e:
        print(f"  ⚠️ 读取 A 股交易日失败（回落今天）: {e}")
    return datetime.date.today()


def _fetch() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    df = ak.stock_fund_flow_industry(symbol=SYMBOL)
    if df is None or len(df) == 0:
        return []
    num = lambda v: None if pd.isna(pd.to_numeric(v, errors="coerce")) else \
        round(float(pd.to_numeric(v, errors="coerce")), 4)
    rows = []
    for _, r in df.iterrows():
        name = str(r.get("行业", "")).strip()
        if not name or name.lower() == "nan":
            continue
        rows.append({
            "sector_name": name[:40],
            "sector_index": num(r.get("行业指数")),
            "change_pct": num(r.get("行业-涨跌幅")),
            "inflow": num(r.get("流入资金")),
            "outflow": num(r.get("流出资金")),
            "net_inflow": num(r.get("净额")),
            "company_count": None if pd.isna(pd.to_numeric(r.get("公司家数"), errors="coerce"))
            else int(pd.to_numeric(r.get("公司家数"), errors="coerce")),
            "leader_name": (str(r.get("领涨股")).strip() or None)[:40]
            if not pd.isna(r.get("领涨股")) else None,
            "leader_chg": num(r.get("领涨股-涨跌幅")),
            "source": SOURCE,
        })
    return rows


def run(codes=None, ctx=None, trade_date: datetime.date = None, dry_run: bool = False):
    """采集入口：行业资金流横截面（幂等 upsert）。"""
    trade_date = trade_date or _recent_cn_trading_date()
    print("=" * 62)
    print(f"行业资金流采集（{SOURCE}，symbol={SYMBOL}）→ {TABLE}")
    print(f"归属交易日: {trade_date}")
    print("=" * 62)

    rows = _fetch()
    if not rows:
        print("  ⚠️ 源返回 0 行，本次不写库")
        return 0

    # 源在非交易日/异常时可能返回全 0 快照：全 0 视为可疑，跳过写入避免污染
    nonzero = [r for r in rows if r["net_inflow"] not in (None, 0)]
    if not nonzero:
        print("  ⚠️ 全部行业净额为空/0（非交易日或源异常），跳过写入")
        return 0

    n_in = sum(1 for r in rows if (r["net_inflow"] or 0) > 0)
    total_net = sum(r["net_inflow"] or 0 for r in rows)
    print(f"  源 {len(rows)} 个行业：净流入 {n_in} / 净流出 {len(rows) - n_in}，"
          f"合计净额 {total_net:,.1f} 亿元")
    top = sorted(nonzero, key=lambda r: r["net_inflow"], reverse=True)[:3]
    print("  净流入前三: " + "，".join(f"{r['sector_name']} {r['net_inflow']:.2f}亿" for r in top))

    if dry_run:
        print("  --dry-run：不写库")
        return len(rows)

    now = datetime.datetime.now()
    for r in rows:
        r["trade_date"] = trade_date
        r["updated_at"] = now
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT, skip_null_updates=True)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT trade_date), min(trade_date), max(trade_date) "
                        f"FROM {TABLE}")
            cnt, days, mn, mx = cur.fetchone()
    print(f"  ✅ upsert {len(rows)} 行；库内累计 {days} 个交易日 / {cnt:,} 行（{mn} ~ {mx}）")
    if days < 2:
        print("  ℹ️ 仅 1 个交易日 → 滚动口径指标（如近5日净流入）需第 2 个交易日后才有值")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="归属交易日 YYYY-MM-DD（默认最近 A 股交易日）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(trade_date=datetime.date.fromisoformat(args.date) if args.date else None,
        dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
