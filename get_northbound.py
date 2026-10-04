#!/usr/bin/env python3
"""北向资金（东方财富 stock_hsgt_hist_em）—— 写入 daily_northbound_flow。

⚠️ 口径断点（2026-09-30 实测，务必知悉）：
  官方自 2024-08-19 起停止披露北向「净买入 / 买入成交额 / 卖出成交额」；
  东财该接口连「持股市值」也一并置 0 → **2024-08-16 之后全部资金字段不可得**
  （最后一个有效日 = 2024-08-16）。
  因此本脚本定位：
    ① 入库 2014-11-17 ~ 2024-08-16 的历史净流入（幂等，可重复跑）；
    ② 每日运行时若源无有效值 → 打印「断流」提示并返回 0，**不写脏数据**；
    ③ 2024-08 之后的北向替代指标（成交额 / 持股）需另找源（港交所 / 沪深交易所官网），
       属 P1 待办，见 doc/market_profile_collection_plan.md §4.4-B。
  消费方注意：读 daily_northbound_flow 做指标时**必须按 2024-08-16 分段**，
  禁止跨断点做连续序列比较。

用法:
    python3 get_northbound.py             # 增量/幂等刷新（断流后为无操作）
    python3 get_northbound.py --backfill  # 全历史（2014-11-17 起）
"""
import argparse
import sys

from db import get_conn, bulk_upsert

SOURCE = "akshare:stock_hsgt_hist_em"
TABLE = "daily_northbound_flow"
CUTOFF = "2024-08-16"   # 最后一个有效交易日（实测）


def _fetch_records() -> tuple:
    """返回 (records, meta)：records 为有效净流入行；meta 含断流统计。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    df = ak.stock_hsgt_hist_em(symbol="北向资金")
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    net = pd.to_numeric(df["当日成交净买额"], errors="coerce")
    hold = pd.to_numeric(df.get("持股市值"), errors="coerce")

    valid = df[net.notna() & (net != 0)]
    records = [{"trade_date": r["日期"].date(), "net_inflow": round(float(net[i]), 2)}
               for i, r in valid.iterrows()]

    meta = {
        "rows_total": len(df),
        "rows_valid": len(records),
        "first_valid": str(df.loc[net.notna(), "日期"].min())[:10] if net.notna().any() else "-",
        "last_valid": str(df.loc[net.notna(), "日期"].max())[:10] if net.notna().any() else "-",
        "hold_last_positive": str(df.loc[(hold.notna()) & (hold > 0), "日期"].max())[:10]
        if (hold.notna() & (hold > 0)).any() else "-",
        "tail_date": str(df["日期"].max())[:10],
    }
    return records, meta


def run(codes=None, ctx=None):
    """采集入口（幂等）：把有效历史段 upsert 进 daily_northbound_flow。"""
    records, meta = _fetch_records()
    print(f"  源：{SOURCE}  总行数 {meta['rows_total']}")
    print(f"  有效净流入：{meta['rows_valid']} 行  {meta['first_valid']} ~ {meta['last_valid']}")
    print(f"  持股市值最后一个正值：{meta['hold_last_positive']}（之后源侧全为 0）")

    if not records:
        print("  ⚠️ 断流：源已无有效北向资金字段，本次不写库（口径断点见模块 docstring）")
        return 0

    with get_conn() as conn:
        bulk_upsert(conn, TABLE, records, conflict_cols=["trade_date"])
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), min(trade_date), max(trade_date) FROM daily_northbound_flow")
            cnt, mn, mx = cur.fetchone()
    print(f"  ✅ upsert {len(records)} 行；库内共 {cnt} 行  {mn} ~ {mx}")
    if str(mx) > CUTOFF:
        print(f"  ⚠️ 库内存在 {CUTOFF} 之后的行，请核对来源（正常不应有）")
    return len(records)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="全历史上库（与增量等价，本脚本天然全量幂等）")
    args = ap.parse_args()

    print("=" * 60)
    print("北向资金（东财历史接口）→ daily_northbound_flow")
    print("=" * 60)
    run()
    print("\n提醒：北向指标计算须按 %s 分段（断点后无数据）" % CUTOFF)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
