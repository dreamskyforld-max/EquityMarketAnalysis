#!/usr/bin/env python3
"""资金族 4 个 L1 原子口径升级后的专项全量重算（§5.4 / §2.2 两类触发）。

背景：DIM.FLOW 落地时把 FLOW.SOUTHBOUND_NET / FLOW.NORTHBOUND_NET 从「单日净额」
改成「20 交易日累计 ÷ 同期成交额 ×100」，并新增 FLOW.MARGIN_NET_20D /
FLOW.BUYBACK_NET_20D。改 `formula` 同时触发 §2.2 第一类（params_hash）与第二类
（参考样本集）全量重算，且旧口径行必须显式删除 —— `indicator_value` 的写入是
`skip_null_updates=True`，新口径在窗口不足 20 日的头部为 NULL，不删就会留下
「同指标新旧两套值混存」。

⚠ 为什么不直接跑 `market_state_daily.py --backfill`：
   ① 它会重写 123 个指标 × 全历史，还会连带触发 L2 三个既有维度的全量重算；
   ② 其中 119 个指标口径未变，属于无意义的大范围写库；
   ③ 本工具把重算范围严格锁在 FLOW_CALIB_CODES 的 4 个编码上。

分位仍走 `market_state_daily._compute_percentiles`（单一真相源，expanding / 无
min_periods）；`model_version` 取 `_params_hash`（含 FLOW_CALIB_VERSION）。
护栏由 `_flow_atoms_*` 的 `_asof_guard` 在产出层裁剪，护栏前不进参考分布。

资源规范（doc/compute_task_resource_standard.md §6）：
   · 所有 SQL 带 [start, end] 下界、GROUP BY 在库侧完成（R1/R7）；
   · 单事务 upsert ≤ 10000 行（R6）；计算任务由本工具独立进程执行（R4/R5）；
   · 表级监控沿用 regime.indicator_value 既有登记，任务级沿用 scheduler 埋点。

用法：
    .venv/bin/python3 tools/recalc_flow_percentile.py            # dry-run，只报数
    .venv/bin/python3 tools/recalc_flow_percentile.py --write    # 删旧口径 + 全量重算
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import bulk_upsert, get_conn  # noqa: E402
import market_state_daily as msd  # noqa: E402

DEFAULT_START = datetime.date(2009, 11, 1)   # 护栏前留足 20 交易日窗口预热
BATCH = 10_000

MARKET_SPEC = {
    "CN": {
        "turnover_table": "a_daily_market_turnover",
        "atoms": msd._flow_atoms_cn,
        "codes": ["FLOW.MARGIN_NET_20D", "FLOW.NORTHBOUND_NET"],
    },
    "HK": {
        "turnover_table": "daily_market_turnover",
        "atoms": msd._flow_atoms_hk,
        "codes": ["FLOW.BUYBACK_NET_20D", "FLOW.SOUTHBOUND_NET"],
    },
}


def _trading_days(conn, table: str, start: datetime.date, end: datetime.date):
    """成交额表交易日 = 本市场唯一对齐基准（trading_calendar 的 HK 段会滞后）。"""
    df = msd._q(conn, f"""
        SELECT trade_date FROM {table}
        WHERE trade_date BETWEEN %s AND %s AND total_turnover > 0 ORDER BY 1
    """, [start, end])
    return pd.Index(pd.to_datetime(df["trade_date"]).dt.date, name="trade_date")


def _build_rows(conn, start, end, write: bool) -> dict:
    now = datetime.datetime.now(msd.TZ_CN)
    meta = {m["code"]: m for m in msd.INDICATORS}
    out_rows, summary = [], {}
    for market, spec in MARKET_SPEC.items():
        idx = _trading_days(conn, spec["turnover_table"], start, end)
        atoms = spec["atoms"](conn, start, end, idx)
        wide = pd.DataFrame({c: atoms[c].reindex(idx) for c in spec["codes"]})
        pct, _n = msd._compute_percentiles(wide)
        for code in spec["codes"]:
            version = msd._params_hash(meta[code], False)
            s_raw, s_pct = wide[code], pct[code]
            valid_idx = s_raw.dropna().index
            for d in idx:
                v, p = s_raw.get(d), s_pct.get(d)
                if (v is None or pd.isna(v)) and (p is None or pd.isna(p)):
                    continue
                has_v = not (v is None or pd.isna(v))
                has_p = not (p is None or pd.isna(p))
                out_rows.append({
                    "indicator_code": code, "market": market, "trade_date": d,
                    "raw_value": round(float(v), 6) if has_v else None,
                    "percentile": round(float(p), 2) if has_p else None,
                    "extra": None,
                    "model_version": version, "updated_at": now,
                })
            summary[f"{market}:{code}"] = {
                "raw": len(valid_idx), "pct": int(s_pct.notna().sum()),
                "version": version,
                "first": str(valid_idx.min()) if len(valid_idx) else "-",
                "last": str(valid_idx.max()) if len(valid_idx) else "-",
            }
    if write:
        codes = sorted(msd.FLOW_CALIB_CODES)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM regime.indicator_value "
                        "WHERE indicator_code = ANY(%s)", (codes,))
            deleted = cur.rowcount
        print(f"  已删旧口径残留 {deleted} 行（{', '.join(codes)}）")
        for i in range(0, len(out_rows), BATCH):
            bulk_upsert(conn, msd.IV_TABLE, out_rows[i:i + BATCH],
                        conflict_cols=["indicator_code", "market", "trade_date"],
                        skip_null_updates=False, only_if_changed=True)
            conn.commit()
        n_dict = msd._ensure_dict(conn)
        print(f"  字典同步 {n_dict} 条（新口径 params_hash）")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（默认 2009-11-01）")
    ap.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--write", action="store_true", help="真正写库；缺省为 dry-run")
    args = ap.parse_args()
    start = datetime.date.fromisoformat(args.start) if args.start else DEFAULT_START
    end = datetime.date.fromisoformat(args.end) if args.end else datetime.date.today()

    print("=" * 72)
    print(f"资金族 L1 专项重算  {start} ~ {end}  {'[WRITE]' if args.write else '[DRY-RUN]'}")
    print("=" * 72)
    with get_conn() as conn:
        summary = _build_rows(conn, start, end, args.write)
    for key, v in sorted(summary.items()):
        print(f"  {key:<36} raw {v['raw']:>5} / pct {v['pct']:>5}"
              f"  {v['first']} ~ {v['last']}  mv={v['version']}")
    print(f"  合计 {sum(v['raw'] for v in summary.values())} 个有效 raw 观测")
    return 0


if __name__ == "__main__":
    sys.exit(main())
