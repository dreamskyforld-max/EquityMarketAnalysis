#!/usr/bin/env python3
"""报告期维度口径 · P3 切点标定（只读，不写库、不改 RULES）。

与 `calibrate_conclusion_cutpoints.py` 的差别：本脚本直接调用 P2 改造后的
标签函数拿**内存中新口径 L1 值**（不读 profile.tag_value——旧值要到 P4 才重建），
按新口径模拟 quality / growth 两个 L2 结论的分档分布与熵比，为冻结切点提供依据。

覆盖 §8 P3：
  - `roe_floor_pct` 按 report_type 分设（§11 裁定 1），在正值子样本上取 P25/P30/P35；
  - `accel` ±5pp 重标（单季同比方差更大）；
  - 其余切点（quality_weak / roe_high / stable / leverage / growth gap / high_growth /
    persistence）按新口径复核；
  - 按 basis 分组报覆盖与分布，检查 §10.1「无 0% 档 / 单档 ≤80%」。

资源约定：单次加载 <50 万行，不分批；查询带时间下界（R7）；结束打印峰值 RSS。
用法：APP_DIR=$(pwd) .venv/bin/python3 tools/calibrate_period_basis_cutpoints.py
"""
from __future__ import annotations

import platform
import resource
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_conn
from profiling.quantile import (
    clear_caches, load_financial_period, load_financial_period_yoy_series,
)
import profiling.tags.growth as G
import profiling.tags.quality as Q

AS_OF = date.today()
CANDIDATE_FLOOR_PCTLS = (25, 30, 35)
ACCEL_GRID = (3, 4, 5, 6, 8, 10)


def _hdr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def build_snapshot(as_of: date) -> pd.DataFrame:
    """调用 P2 标签函数，组装每票一行的判定快照。"""
    with get_conn() as conn:
        p = load_financial_period(conn, as_of)
        h = load_financial_period_yoy_series(conn, as_of, periods=2)
    clear_caches()

    base = p.set_index("stock_code")[["report_basis", "report_type", "roe",
                                      "debt_ratio", "report_date"]].copy()
    base = base.rename(columns={"roe": "roe_raw"})

    l1 = {
        "roe_tier": Q._qal_roe(as_of, 1),
        "cash_tier": Q._qal_earnings_quality(as_of, 1),
        "lev_tier": Q._qal_leverage(as_of, 1),
        "stab_tier": Q._qal_roe_stability(as_of, 1),
        "rev_tier": G._grw_revenue_yoy(as_of, 1),
        "np_tier": G._grw_profit_yoy(as_of, 1),
        "accel": G.grw_growth_accel(as_of),
        "persistence": G.grw_growth_persistence(as_of),
    }
    clear_caches()
    for name, df in l1.items():
        base = base.join(df.set_index("stock_code")["key_value"].rename(name), how="left")
    for name, df in (("rev_yoy", l1["rev_tier"]), ("np_yoy", l1["np_tier"])):
        base = base.join(df.set_index("stock_code")["num_value"].rename(name), how="left")

    # accel 的 Δpp 原值（用于阈值网格）
    h0 = h[h["rn"] == 0].set_index("stock_code")["rev_yoy"].rename("yoy0")
    h1 = h[h["rn"] == 1].set_index("stock_code")["rev_yoy"].rename("yoy1")
    base = base.join(h0, how="left").join(h1, how="left")
    base["accel_delta"] = base["yoy0"] - base["yoy1"]
    return base


def entropy_ratio(counter: Counter, domain_size: int) -> float:
    """分布熵比：观察熵 / 均匀熵（1=全均匀，0=全挤一档）。"""
    n = sum(counter.values())
    if n == 0:
        return 0.0
    h = -sum((c / n) * np.log(c / n) for c in counter.values() if c > 0)
    return h / np.log(domain_size) if domain_size > 1 else 0.0


def main() -> None:
    snap = build_snapshot(AS_OF)
    print(f"snapshot: {len(snap)} stocks, report_type={snap['report_type'].value_counts().to_dict()}")

    # ---------- 1. roe_floor_pct 按 report_type ----------
    _hdr("1. roe_floor_pct 按 report_type（正值子样本，§11 裁定 1）")
    roe_pos = snap[snap["roe_raw"] > 0]
    rows = []
    for rt, g in roe_pos.groupby("report_type"):
        rows.append({
            "report_type": rt, "n": len(g),
            **{f"P{p}": round(float(g["roe_raw"].quantile(p / 100)), 3)
               for p in CANDIDATE_FLOOR_PCTLS},
            "P50": round(float(g["roe_raw"].median()), 3),
        })
    for r in rows:
        print(f"  {r['report_type']:<8} n={r['n']:>5} "
              + " ".join(f"P{p}={r[f'P{p}']:>7}" for p in CANDIDATE_FLOOR_PCTLS)
              + f" P50={r['P50']:>7}")
    print("  注：取 P30 作初始候选（D21『保险丝而非区分度来源』定位），网格结果供 P3 冻结。")

    # ---------- 2. 质量结论分布（现值 P=5 vs 分设 P30）----------
    _hdr("2. 质量结论分档分布（现行单一 5% vs 分设 P30）")
    QUAL_CODES = ("deteriorating", "paper_profit", "solid", "high_but_volatile",
                  "leveraged", "moderate", "weak")

    def judge_quality(row, floor: float) -> str | None:
        s, q, r, l, rr = (row["stab_tier"], row["cash_tier"], row["roe_tier"],
                          row["lev_tier"], row["roe_raw"])
        try:
            s, q, r, l = (int(s) if pd.notna(s) else None,
                          int(q) if pd.notna(q) else None,
                          int(r) if pd.notna(r) else None,
                          int(l) if pd.notna(l) else None)
        except (TypeError, ValueError):
            return None
        if r is None and q is None:
            return None
        if s is not None and q is not None and s >= 4 and q <= 2:
            return "deteriorating"
        if q is not None and q <= 2:
            return "paper_profit"
        if r is not None and rr is not None and r >= 4 and rr >= floor:
            if s is not None and s <= 2 and q is not None and q >= 3:
                return "solid"
            return "high_but_volatile"
        if l is not None and l >= 4 and r is not None and r < 4:
            return "leveraged"
        if r is not None and 3 <= r <= 4:
            return "moderate"
        return "weak"

    # 与 RULES 冻结值对齐（P30 四舍五入）
    floors_p30 = {r["report_type"]: r["P30"] for r in rows}
    frozen = {"Q1": 0.43, "H1": 2.23, "Q3": 3.27, "annual": 1.36}
    print(f"  RULES 冻结值: {frozen}")
    for label, fn in (
        ("现行 floor=5", lambda row: judge_quality(row, 5.0)),
        ("分设 P30", lambda row: judge_quality(
            row, floors_p30.get(row["report_type"], 5.0))),
    ):
        dist = Counter(fn(row) for _, row in snap.iterrows())
        dist = {k: v for k, v in dist.items() if k}
        print(f"  {label:<14} n={sum(dist.values()):>5} 熵比={entropy_ratio(Counter(dist), 7):.3f} "
              + " ".join(f"{c}:{dist.get(c,0)}" for c in QUAL_CODES))
    print("  注：分设后 solid / high_but_volatile 的含义在各 report_type 内一致；"
          "仍按 report_type 组内分档。")

    # ---------- 3. accel 阈值网格 ----------
    _hdr("3. accel ±pp 阈值网格（单季同比方差更大，D10）")
    GROW_CODES = ("fast_sustained", "fast_accelerating", "fast_decelerating",
                  "slow_steady", "contracting", "revenue_profit_divergent")

    def judge_growth(row, acc_thr: float) -> str | None:
        try:
            rt = int(row["rev_tier"]) if pd.notna(row["rev_tier"]) else None
            nt = int(row["np_tier"]) if pd.notna(row["np_tier"]) else None
            pe = int(row["persistence"]) if pd.notna(row["persistence"]) else None
        except (TypeError, ValueError):
            return None
        if rt is None or nt is None:
            return None
        if abs(rt - nt) >= 2:
            return "revenue_profit_divergent"
        ry, ny = row["rev_yoy"], row["np_yoy"]
        if (pd.notna(ry) and ry < 0) or (pd.notna(ny) and ny < 0):
            return "contracting"
        d = row["accel_delta"]
        if rt >= 4 and nt >= 4 and pe is not None and pe >= 2:
            return "fast_sustained"
        if rt >= 4 and pd.notna(d) and d >= acc_thr:
            return "fast_accelerating"
        if rt >= 4 and pd.notna(d) and d <= -acc_thr:
            return "fast_decelerating"
        return "slow_steady"

    for thr in ACCEL_GRID:
        dist = Counter(judge_growth(row, thr) for _, row in snap.iterrows())
        dist = {k: v for k, v in dist.items() if k}
        er = entropy_ratio(Counter(dist), 6)
        print(f"  ±{thr:>2}pp  n={sum(dist.values()):>5} 熵比={er:.3f} "
              + " ".join(f"{c}:{dist.get(c,0)}" for c in GROW_CODES))

    # ---------- 4. 分 basis 覆盖 ----------
    _hdr("4. 分 basis 判定覆盖（G1/G2 后出档）")
    for basis, g in snap.groupby("report_basis"):
        okq = g["cash_tier"].notna().sum()
        okrev = g["rev_tier"].notna().sum()
        okacc = g["accel_delta"].notna().sum()
        print(f"  {basis:<13} n={len(g):>5} 现金档={okq:>5} 营收增速档={okrev:>5} "
              f"加速度可比={okacc:>5}")

    print(f"\n[{platform.system()}] 峰值 RSS: "
          f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if platform.system()=='Darwin' else 1024):.1f} MB")
    print("P3 标定探查完成（只读，未写库、未改 RULES）。")


if __name__ == "__main__":
    main()
