#!/usr/bin/env python3
"""§5.1 dispersion 门禁的**离线对照实验 / 历史对照**（只读，不改口径、不写库）。

⚠ v2.1.12 起生产端已按**方案 C** 落地（`dispersion_scope = non_flags_only`），
   本报告保留为决策依据：A = 整改前口径，C = 当前生效口径。

对照两个疑点，四个组合：
  · 因子方向：现状 `σ×sqrt(valid/10)` vs 标准误方向 `σ×sqrt(10/valid)`
    （后者在 valid=10 处与现口径同尺度，故阈值 30 可沿用；只是成员数依赖反转）
  · σ 的成员集合：全部 eligible（含 2 个 flags_only 诊断成员）
    vs 仅非 flags_only 成员（它们已被标定判为与核心宽度分不同构念）

`score` / `coverage` / `rep_coverage` / 冻结切点四者**保持不变**，只比较出档结果，
故差异可完全归因于 dispersion 门禁。四口径算出的 `score` 完全相同，
`state_bands` 基于 `score` 分布冻结，因此切换口径**无需重标切点**。

读 `regime.indicator_value` 宽度族分位（带 market + code + 时间下界），
复用 `build_dimension_value` 的读取与字典函数，不重跑采集。

用法：
    .venv/bin/python3 tools/probe_breadth_gate.py
"""
from __future__ import annotations

import datetime
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn
import build_dimension_value as bd

DATA_START = {"CN": datetime.date(1990, 1, 1), "HK": datetime.date(2000, 1, 1)}
NO_STATE = "未出档"


def _frame(conn, market: str, end: datetime.date) -> dict:
    dim, _ = bd.load_dimension_dict(conn, bd.DIM_BREADTH)
    members = dim["members"]
    codes = [m["code"] for m in members]
    scopes = bd._load_market_scopes(conn, codes)
    in_scope = lambda c: scopes.get(c) in (market, "CN+HK", "GLOBAL")
    eligible = [c for c in codes if in_scope(c)]
    core = [m["code"] for m in members
            if in_scope(m["code"]) and not m.get("flags_only")]

    raw = bd._load_percentiles(conn, market, eligible, DATA_START[market], end)
    if raw.empty:
        return {}
    aligned = raw.copy()
    for c in eligible:
        if c not in aligned.columns:
            aligned[c] = np.nan
    for m in members:
        c = m["code"]
        if c in eligible and int(m.get("sign", 1)) == -1:
            aligned[c] = 100.0 - aligned[c]

    reps = [m["code"] for m in members
            if m.get("representative") and not m.get("flags_only") and in_scope(m["code"])]
    score = aligned[reps].mean(axis=1, skipna=True).clip(0, 100)
    coverage = aligned[eligible].notna().sum(axis=1) / max(len(eligible), 1)
    rep_cov = aligned[reps].notna().sum(axis=1) / max(len(reps), 1)
    return {
        "aligned": aligned, "eligible": eligible, "core": core, "reps": reps,
        "score": score, "coverage": coverage, "rep_cov": rep_cov,
        "bands": list(dim["state_bands"][market]),
        "labels": list(dim["state_labels"]),
        "params": dim["params"],
    }


def _variants(f: dict) -> dict:
    a = f["aligned"]
    sigma_all = a[f["eligible"]].std(axis=1, ddof=1)
    sigma_core = a[f["core"]].std(axis=1, ddof=1)
    n_all = a[f["eligible"]].notna().sum(axis=1)
    n_core = a[f["core"]].notna().sum(axis=1)
    return {
        "A 现状·全成员·√(n/10)":       (sigma_all * np.sqrt(n_all / 10.0), "全成员", "现因子"),
        "B 标准误·全成员·√(10/n)":     (sigma_all * np.sqrt(10.0 / n_all.replace(0, np.nan)), "全成员", "标准误"),
        "C 现状·排除诊断·√(n/10)":     (sigma_core * np.sqrt(n_core / 10.0), "排除诊断", "现因子"),
        "D 标准误·排除诊断·√(10/n)":   (sigma_core * np.sqrt(10.0 / n_core.replace(0, np.nan)), "排除诊断", "标准误"),
    }


def _states(f: dict, gate: pd.Series) -> pd.Series:
    lv = np.digitize(f["score"].fillna(-1).to_numpy(), f["bands"])
    s = pd.Series([f["labels"][int(i)] for i in lv], index=f["score"].index)
    return s.where(gate, NO_STATE)


def report(conn, market: str, end: datetime.date) -> None:
    f = _frame(conn, market, end)
    if not f:
        print(f"{market}: 无数据")
        return
    p = f["params"]
    print(f"\n===== {market} | {f['aligned'].index.min()} ~ {f['aligned'].index.max()} "
          f"({len(f['aligned'])} 日) | 代表 {len(f['reps'])} / 成员 {len(f['eligible'])} =====")
    print(f"门禁其余三项固定：coverage≥{p['coverage_min']}、"
          f"dispersion_norm≤{p['dispersion_max']}、rep_coverage≥{p.get('rep_coverage_min', 0)}")
    variants = _variants(f)
    base_states = None
    rows = []
    last_day = f["score"].index.max()
    for name, (norm, member_set, factor) in variants.items():
        gate = (f["score"].notna() & (f["coverage"] >= float(p["coverage_min"]))
                & (norm <= float(p["dispersion_max"]))
                & (f["rep_cov"] >= float(p.get("rep_coverage_min", 0.0))))
        st = _states(f, gate)
        if base_states is None:
            base_states = st
        idx = f["score"].index
        years = pd.to_datetime(idx).year
        oos = (years >= 2026)
        rows.append({
            "口径": name, "成员集": member_set, "因子": factor,
            "未出档率%": round(float((~gate).mean() * 100), 2),
            "出档日": int(gate.sum()),
            "档位切换": int((st != st.shift()).sum()),
            "与A差异日": int((st != base_states).sum()),
            "2026未出档%": round(float((~gate[oos]).mean() * 100), 2) if oos.any() else None,
            "norm中位": round(float(norm.median()), 2),
            f"最新日{last_day}": str(st.loc[last_day]),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n最新交易日 {last_day} 的 dispersion_norm（阈值 30）：")
    for name, (norm, _, _) in variants.items():
        v = norm.loc[last_day]
        print(f"  {name}: {v:.2f} → {'出档' if v <= float(p['dispersion_max']) else '未出档'}")

    # 按年代分层的未出档率（幸存者偏差传导检查，§5.1 要求报告）
    print("\n按年代未出档率(%)：")
    layers = []
    for name, (norm, _, _) in variants.items():
        gate = (f["score"].notna() & (f["coverage"] >= float(p["coverage_min"]))
                & (norm <= float(p["dispersion_max"])))
        df = pd.DataFrame({"g": gate, "dec": (pd.to_datetime(f["score"].index).year // 10) * 10})
        by = df.groupby("dec")["g"].agg(lambda s: round((~s).mean() * 100, 1))
        by.name = name
        layers.append(by)
    print(pd.concat(layers, axis=1).to_string())


def main() -> int:
    end = datetime.date.today()
    with get_conn() as conn:
        for market in ("CN", "HK"):
            report(conn, market, end)
    print("\n⚠ 本报告为只读对照，未修改任何口径 / 字典 / 落库数据。")
    print("  v2.1.12 已按 C 落地：A 为整改前口径，C 为当前生效口径。")
    print("  切点无需重标（四口径 score 相同）；口径切换靠 params_hash + 全量重算 L2。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
