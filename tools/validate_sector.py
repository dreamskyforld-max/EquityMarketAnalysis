#!/usr/bin/env python3
"""第④层「行业配置」阶段 4 验证闭环 —— 只读验收工作台（不写库）。

对应 doc/industry_allocation_feasibility.md §1.3 G4 / §3.7 / §4.2 阶段 4，
以及 doc/market_profile.md §8.5（定义不依赖未来、验证使用未来）、§7.2（IC 重估准入）。

【本期范围（2026-11-14 用户决策）】
  ✅ 能判定的 5 项：
     ① 排序有效性：state_side 5 分组 + 未来 20/60/120 日**行业超额收益**
        （基准 = 当日有效行业等权均值，M3 口径，不是大盘指数）
        + 逐日 Spearman IC + 样本外切分 + **按行业×月二维 block bootstrap**；
     ② 构念效度：成员与维度分相关 / Cronbach α / 一阶差分相关 + PC1 载荷；
     ③ 各档样本占比：象限（预期 25%）、q5 分组（预期 20%）、拥挤档位；
     ④ 幸存者偏差：pit_capable=true/false 分段读数对比、早期年份单列；
     ⑤ 口径变更：库内 params_hash 分段 + git worktree 逐值对比命令（阶段 3 已执行）。
  ⛔ 明确「数据不足·不可判定」、不拿短样本硬凑结论：
     · 拥挤闸门有效性（GATE=EXTREME 仅 45 行且全在最近 30 日，后 60 日不可观测）；
     · 快照类时序分位启用（估值 27 / 资金 27 / 预期 25 日 << min_periods=250）；
     · HK 排序有效性（52 日、L3 本期不排名）。
  🔍 附：HK 两版指数 winsorize / 最小成分数门槛**描述性敏感性分析**（阶段 3 待排查项），
     只报告、不改 hk_sector_index.py 生产口径。

【纪律】
  · 纯只读：不调用任何 INSERT/UPDATE/DELETE；所有 SQL 带时间下界或实体白名单（R1/R7）。
  · 前瞻收益是**样本内重叠窗口**、未扣成本；bootstrap 只修正重叠样本标准误，
    **不替代样本外检验**（与 tools/validate_regime.py 同一警示）。
  · 决策闸门按 §3.7：不通过 → 排序维持「当前状态描述」、权重维持等权、不得宣称有效。
    本脚本**只出结论，不改任何生产代码 / 字典 / params_hash / 历史读数**。

用法：
    python3 tools/validate_sector.py                         # stdout 报告
    python3 tools/validate_sector.py --bootstrap 200 --out /tmp/sector_val
    python3 tools/validate_sector.py --start 2021-12-13 --end 2026-11-11
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import get_conn, read_sql  # noqa: E402
from sector_layer import DIMENSION_DEFS  # noqa: E402

MARKET = "CN"
CLASSIFICATION = "SW_L1"
PIT_START = dt.date(2021, 12, 13)
HORIZONS = (20, 60, 120)
MIN_XSEC_N = 10           # 与 XSEC_MIN_N 一致：横截面样本不足不给 IC
MIN_PERIODS = 250         # §5.7 时序分位最小样本
SPLIT_FRACTION = 0.6      # 前 60% 标定 / 后 40% 样本外（按交易日切分，无前视）
BOOTSTRAP_DEFAULT = 200
RANDOM_SEED = 42

_SNAPSHOT_TABLES = {
    "sector_valuation_snapshot": ("snapshot_date", "regime.sector_valuation_snapshot"),
    "sector_fund_flow": ("trade_date", "regime.sector_fund_flow"),
    "analyst_forecast_snapshot": ("snapshot_date", "regime.analyst_forecast_snapshot"),
}

_NOT_EVALUABLE = "NOT_EVALUABLE"


# ── 数据加载（R1/R7：全部带时间下界 / 实体白名单）─────────────────────────────
def load_panel(conn, start: dt.date, end: dt.date) -> pd.DataFrame:
    """L3 业务读数 + 行业指数收盘 → 未来 N 日**行业超额收益**标签。

    ⚠ 基准是「当日有效行业等权均值」而不是大盘指数（§3.2 M3 / §7.11 决策 3）：
       fwd_ret_N(i,t) = close(i,t+N)/close(i,t) − 1
       fwd_excess_N(i,t) = fwd_ret_N(i,t) − mean_{i∈S_t} fwd_ret_N(i,t)
    用 (trade_date × sw_code) 宽表在**交易日网格**上 shift(-N)，避免某行业缺行时
    groupby.shift 按行数位移造成错位；mean(axis=1) 自动适配 rank_n 16→28→31 时变。
    """
    # L3 已改为按 biz_code 分行的长表；这里用条件聚合 pivot 回一行业一日一行，
    # 保持下游分组/IC/占比计算的输入形状不变。
    biz = read_sql(conn, """
        SELECT trade_date, sector_code,
               max(score)  FILTER (WHERE biz_code='SEC.BIZ.STATE_SIDE') AS state_side,
               max(score)  FILTER (WHERE biz_code='SEC.BIZ.RISK_SIDE')  AS risk_side,
               max(state)  FILTER (WHERE biz_code='SEC.BIZ.QUADRANT')   AS quadrant,
               max(level)  FILTER (WHERE biz_code='SEC.BIZ.RANK')       AS rank_no,
               (max(detail->>'rank_n') FILTER (WHERE biz_code='SEC.BIZ.RANK'))::int AS rank_n,
               max(coverage)   AS coverage,
               bool_or(applicable) AS applicable,
               max(confidence) AS confidence,
               max(detail->>'gate_flags') FILTER (WHERE biz_code='SEC.BIZ.GATE') AS gate_flags,
               max(params_hash) AS params_hash,
               max(extra->>'pit_capable')   AS pit_capable,
               max(extra->>'calib_version') AS calib_version
        FROM industry.business_value
        WHERE market=%s AND classification=%s AND trade_date BETWEEN %s AND %s
        GROUP BY trade_date, sector_code
        ORDER BY trade_date, sector_code
    """, params=[MARKET, CLASSIFICATION, start, end])
    px = read_sql(conn, """
        SELECT trade_date, sw_code, close
        FROM regime.sector_daily
        WHERE trade_date BETWEEN %s AND %s AND close IS NOT NULL AND close > 0
        ORDER BY trade_date, sw_code
    """, params=[start, end])
    if biz.empty or px.empty:
        return pd.DataFrame()
    biz["trade_date"] = pd.to_datetime(biz["trade_date"]).dt.date
    px["trade_date"] = pd.to_datetime(px["trade_date"]).dt.date
    wide = px.pivot(index="trade_date", columns="sw_code", values="close").sort_index()
    for n in HORIZONS:
        fwd = wide.shift(-n) / wide - 1.0
        excess = fwd.sub(fwd.mean(axis=1), axis=0)
        long = (excess.stack().rename(f"fwd_{n}").reset_index()
                     .rename(columns={"sw_code": "sector_code"}))
        biz = biz.merge(long, on=["trade_date", "sector_code"], how="left")
    # R3：低基数列 category 化（object 56~68B/单元格 → ~4B）
    for c in ("sector_code", "quadrant", "confidence", "pit_capable",
              "calib_version", "params_hash"):
        if c in biz.columns:
            biz[c] = biz[c].astype("category")
    return biz


def load_dimension_items(conn, start: dt.date, end: dt.date,
                         codes: list[str], dimension: str):
    """构念效度：**按维度分批**加载成员分位 + 该维度 score（R3：不一次性透视全维度）。"""
    iv = read_sql(conn, """
        SELECT trade_date, sector_code, indicator_code, percentile_xsec
        FROM industry.indicator_value
        WHERE market=%s AND classification=%s AND trade_date BETWEEN %s AND %s
          AND indicator_code = ANY(%s)
    """, params=[MARKET, CLASSIFICATION, start, end, sorted(codes)])
    sc = read_sql(conn, """
        SELECT trade_date, sector_code, dimension_code, score
        FROM industry.dimension_value
        WHERE market=%s AND classification=%s AND trade_date BETWEEN %s AND %s
          AND dimension_code = %s
    """, params=[MARKET, CLASSIFICATION, start, end, dimension])
    for d in (iv, sc):
        if not d.empty:
            d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.date
            d["sector_code"] = d["sector_code"].astype("category")
    if not iv.empty:
        iv["indicator_code"] = iv["indicator_code"].astype("category")
    return iv, sc


# ── 纯函数（可单测）─────────────────────────────────────────────────────────
def forward_excess(close_wide: pd.DataFrame, n: int) -> pd.DataFrame:
    """M3 口径的未来 N 日行业超额收益（横截面去均值），在交易日网格上 shift。"""
    fwd = close_wide.sort_index().shift(-n) / close_wide.sort_index() - 1.0
    return fwd.sub(fwd.mean(axis=1), axis=0)


def assign_quintiles(df: pd.DataFrame, value_col: str = "state_side") -> pd.Series:
    """逐交易日横截面 5 分组（先 rank 再 qcut，处理并列）；有效行业 <10 不给分组。"""
    def _one(s):
        if s.notna().sum() < MIN_XSEC_N:
            return pd.Series(np.nan, index=s.index)
        try:
            return pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop") + 1.0
        except ValueError:
            return pd.Series(np.nan, index=s.index)
    return df.groupby("trade_date")[value_col].transform(_one)


def ic_by_date(df: pd.DataFrame, x_col: str, y_col: str) -> pd.Series:
    """逐交易日横截面 Spearman IC 序列（样本 <MIN_XSEC_N 不给值）。"""
    out = {}
    for d, g in df.dropna(subset=[x_col, y_col]).groupby("trade_date"):
        if len(g) < MIN_XSEC_N:
            continue
        rho = spearmanr(g[x_col], g[y_col]).statistic
        if not math.isnan(rho):
            out[d] = float(rho)
    return pd.Series(out, dtype=float).sort_index()


def _fast_daily_ic(df: pd.DataFrame) -> pd.Series:
    """向量化逐日 Spearman（秩 → Pearson）：供 block bootstrap 内循环使用。"""
    z = df.dropna(subset=["_x", "_y"])
    if z.empty:
        return pd.Series(dtype=float)
    ranks = z.groupby("trade_date")[["_x", "_y"]].rank()
    t = pd.DataFrame({"trade_date": z["trade_date"].values,
                      "rx": ranks["_x"].values, "ry": ranks["_y"].values})
    g = t.groupby("trade_date")
    n = g.size()
    sx, sy = g["rx"].sum(), g["ry"].sum()
    sxy = (t["rx"] * t["ry"]).groupby(t["trade_date"]).sum()
    sx2 = (t["rx"] ** 2).groupby(t["trade_date"]).sum()
    sy2 = (t["ry"] ** 2).groupby(t["trade_date"]).sum()
    cov = sxy - sx * sy / n
    den = np.sqrt((sx2 - sx ** 2 / n) * (sy2 - sy ** 2 / n))
    corr = (cov / den.replace(0, np.nan)).where(n >= MIN_XSEC_N).dropna()
    return corr


def two_dim_block_bootstrap(df: pd.DataFrame, n_iter: int, rng) -> dict | None:
    """§3.7 要求的「按行业×月」二维 block bootstrap。

    以 (sector_code, year-month) 为块单位有放回重采样，保留行业内时间依赖与
    月度共同冲击；每次重采样后重算逐日 IC 均值 → 95% CI。
    ⚠ 重采样会改变每日行业集合，IC 为秩相关对此不敏感；报告须注明这一近似。
    """
    # R3：绝不物化上千个 DataFrame 块（首版 1798 块 + 每次 pd.concat 使峰值达 467MB）。
    # 只保存块内**行位置数组**，重采样时用一次 iloc 取行；且只保留 3 列。
    z = (df[["trade_date", "sector_code", "_x", "_y"]]
         .dropna(subset=["_x", "_y"]).reset_index(drop=True))
    if z.empty:
        return None
    month = pd.to_datetime(z["trade_date"]).dt.to_period("M").astype(str)
    block_idx = list(z.assign(_month=month)
                      .groupby(["sector_code", "_month"], sort=False).indices.values())
    if len(block_idx) < 3:
        return None
    z = z[["trade_date", "_x", "_y"]]
    means = []
    for _ in range(n_iter):
        pick = rng.integers(0, len(block_idx), len(block_idx))
        pos = np.concatenate([block_idx[int(j)] for j in pick])
        ic = _fast_daily_ic(z.iloc[pos])
        if len(ic):
            means.append(float(ic.mean()))
        del pos, ic
    if not means:
        return None
    return dict(n_iter=n_iter, n_blocks=len(block_idx),
                mean=round(float(np.mean(means)), 5),
                ci_low=round(float(np.percentile(means, 2.5)), 5),
                ci_high=round(float(np.percentile(means, 97.5)), 5))


def cronbach_alpha(items: pd.DataFrame) -> float:
    """**标准化** Cronbach α：成员先 z-score 再算 k/(k−1)·(1 − ΣVar(item)/Var(Σitem))。

    §8.5 的内部一致性是「基于相关」的 α。直接用原始分位会因各成员方差不同，
    让完全相关的三个成员也只算出 0.9375（试跑踩过）—— 那是未标准化 α，
    对量纲不一致的成员集合不适用。
    """
    x = items.dropna()
    if x.shape[1] < 2 or len(x) < 30:
        return float("nan")
    sd = x.std(axis=0, ddof=1).replace(0, np.nan)
    z = ((x - x.mean()) / sd).dropna(axis=1)
    k = z.shape[1]
    if k < 2:
        return float("nan")
    var_items = float(z.var(axis=0, ddof=1).sum())
    var_total = float(z.sum(axis=1).var(ddof=1))
    if var_total <= 0:
        return float("nan")
    return float(k / (k - 1) * (1 - var_items / var_total))


def pc1_stats(diff: pd.DataFrame, members: list[str]) -> dict:
    """一阶差分相关矩阵 → 中位相关 / PC1 解释度 / 载荷（复用 calibrate_breadth 口径）。

    ⚠ 相关矩阵有任一非对角 NaN 时**直接判不可估计**：`nan_to_num(0)` 会把
    「有效重叠不足」伪装成「零相关」，进而产出 PC1=1/k 的伪结果（试跑踩过）。
    """
    sub = diff[members].corr(min_periods=MIN_PERIODS).values.astype(float)
    off = sub[~np.eye(len(members), dtype=bool)]
    if len(members) < 2 or len(off) == 0 or not np.isfinite(off).all():
        return dict(status="INSUFFICIENT_OVERLAP", median_corr=None, pc1_var=None,
                    loadings={},
                    reason="成员一阶差分序列有效重叠 < min_periods=250，相关矩阵不可估计")
    eig, vec = np.linalg.eigh(sub)
    return dict(status="ok", median_corr=round(float(np.median(off)), 4),
                pc1_var=round(float(eig[-1] / eig.sum()), 4) if eig.sum() > 0 else None,
                loadings={c: round(float(abs(v)), 4)
                          for c, v in zip(members, vec[:, -1])})


def decision_gate(ic_oos_ci_low: float | None, monotone_rho: float | None,
                  monotone_p: float | None) -> dict:
    """§3.7 决策闸门：分组单调 且 样本外 IC 显著 >0（bootstrap CI 下界 >0）。

    不通过时按文档处置：排序维持「当前状态描述」、权重维持等权、不得宣称有效 /
    不得用于配置；**本函数只产出结论，不触发任何写库或权重变更**。
    """
    passed = bool(ic_oos_ci_low is not None and not math.isnan(ic_oos_ci_low)
                  and ic_oos_ci_low > 0
                  and monotone_rho is not None and monotone_rho > 0
                  and monotone_p is not None and monotone_p < 0.05)
    return dict(
        passed=passed,
        criterion="分组单调(Spearman ρ>0 且 p<0.05) 且 样本外 IC 的 95% bootstrap CI 下界 >0",
        action=("通过：排序可标注「有效性已验证的状态排序」，仍不出配置建议；"
                "权重二期按 §7.2 前瞻 IC 重估须单独立项" if passed else
                "不通过：排序保持「当前状态描述」定位，不得宣称有效、不得用于配置、"
                "权重维持等权（现状即如此，不改任何生产代码 / 字典 / params_hash）"))


def not_evaluable(name: str, observed, required, reason: str,
                  monitor: str) -> dict:
    return dict(item=name, status=_NOT_EVALUABLE, observed=observed,
                required=required, reason=reason, monitor=monitor)


# ── 各项检验 ────────────────────────────────────────────────────────────────
def check_ranking(panel: pd.DataFrame, n_iter: int, rng) -> dict:
    out = {"horizons": {}}
    panel = panel.copy()
    panel["q5"] = assign_quintiles(panel)
    q = (panel.groupby("q5").size() / panel["q5"].notna().sum()).round(4).to_dict()
    out["quintile_share"] = {str(int(k)): float(v) for k, v in q.items() if pd.notna(k)}
    out["quintile_note"] = ("31 个行业按横截面秩分 5 组只能是 7/6/6/6/6，"
                            "占比 22.58%/19.35% 属整数效应，不是分布漂移；"
                            "按主报告 §8.5 不得改判据凑数")

    dates = sorted(panel["trade_date"].unique())
    split = dates[int(len(dates) * SPLIT_FRACTION)] if len(dates) > 20 else None
    out["split_date"] = str(split)
    for n in HORIZONS:
        col = f"fwd_{n}"
        # 只取本次检验需要的 5 列，避免每个 horizon 都复制整块 17 列面板（R3）
        d = (panel[["trade_date", "sector_code", "state_side", "q5", col]]
             .dropna(subset=["state_side", col]).copy())
        if d.empty or split is None:
            out["horizons"][str(n)] = {"error": "无有效前瞻样本"}
            continue
        ic = ic_by_date(d, "state_side", col)
        grp = (d.dropna(subset=["q5"]).groupby("q5")[col]
                 .agg(["mean", "count"]).reset_index())
        mono = spearmanr(grp["q5"], grp["mean"]) if len(grp) >= 3 else None
        is_oos = ic.index >= split
        ic_in, ic_oos = ic[~is_oos], ic[is_oos]
        d["_x"], d["_y"] = d["state_side"], d[col]
        boot = two_dim_block_bootstrap(
            d[["trade_date", "sector_code", "_x", "_y"]], n_iter, rng)
        del d
        spread = (float(grp["mean"].iloc[-1] - grp["mean"].iloc[0])
                  if len(grp) >= 2 else None)
        out["horizons"][str(n)] = dict(
            ic_days=int(len(ic)),
            ic_all_mean=_r(ic.mean()), ic_in_mean=_r(ic_in.mean()),
            ic_oos_mean=_r(ic_oos.mean()), ic_oos_days=int(len(ic_oos)),
            ic_oos_bootstrap=boot,
            quintile_mean={str(int(r["q5"])): _r(r["mean"])
                           for _, r in grp.iterrows()},
            quintile_n={str(int(r["q5"])): int(r["count"])
                        for _, r in grp.iterrows()},
            spread_q5_minus_q1=_r(spread),
            monotonicity=dict(rho=_r(mono.statistic if mono else None),
                              p=_r(mono.pvalue if mono else None, 6)),
        )
    # 决策闸门以 20 日为主检验窗口（60/120 作稳健性，重叠更重）
    h = out["horizons"].get("20", {})
    out["decision"] = decision_gate(
        (h.get("ic_oos_bootstrap") or {}).get("ci_low"),
        (h.get("monotonicity") or {}).get("rho"),
        (h.get("monotonicity") or {}).get("p"))
    return out


def _r(v, digits=5):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, digits)


def check_construct(conn, start: dt.date, end: dt.date) -> dict:
    """按维度分批取数 → 计算 → 释放（R3：峰值与「维度数 × 全史明细」脱钩）。"""
    out = {}
    for name, cfg in DIMENSION_DEFS.items():
        # 单锚 / 规则判定维度没有「成员集合」：Cronbach α、成员载荷、PCA 在数学上无定义。
        # 强行计算会得到 α=NaN、载荷=1.0、PC1=1.0 这类伪结果，必须显式标不适用。
        if cfg["structure"] in ("single_anchor", "rule"):
            out[name] = dict(
                status="NOT_APPLICABLE", structure=cfg["structure"],
                anchor=cfg.get("anchor"),
                reason="单锚（成员=1）/ 规则判定维度无成员集合，内部一致性检验不适用；"
                       "§7.2 规定成员<4 走单锚或规则判定，构念效度门禁只对多成员维度适用")
            continue
        members = [c for c, _ in cfg["members"]]
        iv, sc = load_dimension_items(conn, start, end, members, name)
        if iv.empty or sc.empty:
            out[name] = {"error": "无 L1/L2 数据"}
            del iv, sc
            continue
        piv = (iv.dropna(subset=["percentile_xsec"])
                 .pivot_table(index=["trade_date", "sector_code"],
                              columns="indicator_code", values="percentile_xsec",
                              aggfunc="last", observed=True))
        score = (sc.pivot_table(index=["trade_date", "sector_code"],
                                columns="dimension_code", values="score",
                                aggfunc="last", observed=True))
        members = [c for c in members if c in piv.columns]
        if not members or name not in score.columns:
            out[name] = {"error": "无成员分位或维度分"}
        else:
            items = piv[members].copy()
            for c, sign in cfg["members"]:
                if sign < 0 and c in items.columns:
                    items[c] = 100.0 - items[c]
            joined = items.join(score[[name]].rename(columns={name: "_score"}),
                                how="inner").dropna()
            corr = {c: _r(joined[c].corr(joined["_score"]), 4) for c in members}
            # 一阶差分必须按**行业内时序**做：index 是 (date, sector_code)，
            # 直接 diff() 会跨行业相减，制造虚假相关（calibrate_breadth 的教训）。
            diff = items.groupby(level="sector_code", observed=True).diff().dropna()
            failing = [c for c, v in corr.items() if v is None or v < 0.3]
            out[name] = dict(
                status="ok", structure=cfg["structure"],
                n_obs=int(len(joined)), members=members,
                cronbach_alpha=_r(cronbach_alpha(items), 4),
                item_score_corr=corr,
                min_item_score_corr=_r(min((v for v in corr.values() if v is not None),
                                          default=None), 4),
                loadings_ok=not failing, failing_members=failing,
                construct_gate=("未通过：成员载荷 <0.3，按 §8.5 应移出该成员；"
                                "本期按用户决策只登记、不改生产代码" if failing else
                                "通过：成员载荷均 ≥0.3 且无负相关"),
                diff_corr=pc1_stats(diff, members) if len(diff) >= MIN_PERIODS else None)
            del items, joined, diff
        del iv, sc, piv, score
        gc.collect()
    return out


def check_shares(conn, panel: pd.DataFrame, start: dt.date, end: dt.date) -> dict:
    q = panel.loc[panel["quadrant"].notna() & (panel["quadrant"] != "NA"), "quadrant"]
    quad = (q.value_counts(normalize=True).round(4).to_dict() if len(q) else {})
    # 拥挤档位用 SQL 聚合（只回 5 行），不把百万行 L1 明细拉进内存
    g = read_sql(conn, """
        SELECT extra->>'label' AS label, count(*) AS n
        FROM industry.indicator_value
        WHERE market=%s AND classification=%s AND trade_date BETWEEN %s AND %s
          AND indicator_code=%s
        GROUP BY 1
    """, params=[MARKET, CLASSIFICATION, start, end, "SEC.CROWD.GATE"])
    total = float(g["n"].sum()) if not g.empty else 0.0
    gate = {str(r["label"]): round(float(r["n"]) / total, 4)
            for _, r in g.iterrows() if r["label"]} if total else {}
    return dict(
        quadrant_n=int(len(q)), quadrant_expected_each=0.25, quadrant_share=quad,
        gate_n=int(total), gate_share=gate,
        note="按分位切档的档位应近似等频；偏离只提示分布漂移，按主报告 §8.5 不得改判据凑数")


def check_pit_bias(conn, panel: pd.DataFrame) -> dict:
    """有效窗口分段 + **全史分段**（1999 起的 pit_capable=false 早期年份必须单列）。

    只跑 2021-12-13 起的验证窗口会看不到 false 段，等于把「早期年份单列报告」
    这一条悄悄漏掉：故在全史上单独做一次轻量聚合（带时间下界，R7）。
    """
    out = {}
    for pit, g in panel.groupby("pit_capable", dropna=False):
        out[str(pit)] = dict(rows=int(len(g)),
                             date_min=str(g["trade_date"].min()),
                             date_max=str(g["trade_date"].max()),
                             state_side_mean=_r(g["state_side"].mean(), 4),
                             rank_coverage=_r(g["rank_no"].notna().mean(), 4),
                             applicable_ratio=_r(g["applicable"].mean(), 4),
                             coverage_mean=_r(g["coverage"].mean(), 4))
    # 长表下每个行业日有多行，rows 用 DISTINCT 去重，各指标用 FILTER 分别取。
    hist = read_sql(conn, """
        SELECT extra->>'pit_capable' AS pit_capable,
               min(trade_date) AS date_min, max(trade_date) AS date_max,
               count(DISTINCT (trade_date, sector_code)) AS rows,
               avg(score) FILTER (WHERE biz_code='SEC.BIZ.STATE_SIDE') AS state_mean,
               count(level) FILTER (WHERE biz_code='SEC.BIZ.RANK') AS ranked_rows,
               avg(coverage) FILTER (WHERE biz_code='SEC.BIZ.STATE_SIDE') AS coverage_mean
        FROM industry.business_value
        WHERE market=%s AND classification=%s AND trade_date >= %s
        GROUP BY 1 ORDER BY 1
    """, params=[MARKET, CLASSIFICATION, dt.date(1999, 12, 30)])
    full = [dict(pit_capable=str(r["pit_capable"]), rows=int(r["rows"]),
                 date_min=str(r["date_min"]), date_max=str(r["date_max"]),
                 state_side_mean=_r(r["state_mean"], 4),
                 rank_coverage=_r(r["ranked_rows"] / r["rows"], 4) if r["rows"] else None,
                 coverage_mean=_r(r["coverage_mean"], 4))
            for _, r in hist.iterrows()]
    return dict(effective_window_segments=out, full_history_segments=full,
                note="pit_capable=false 段含幸存者偏差，必须单列报告、整体降置信，不静默混入；"
                     "该段因缺 4 组自下而上维度 coverage 最高 0.5 < 0.6 → 不出排名（§3.6 ③）")


def check_calib_versions(panel: pd.DataFrame) -> dict:
    seg = (panel.groupby("params_hash")
                .agg(date_min=("trade_date", "min"), date_max=("trade_date", "max"),
                     rows=("trade_date", "size")).reset_index())
    return dict(segments=[{k: str(v) if isinstance(v, dt.date) else v
                          for k, v in r.items()} for _, r in seg.iterrows()],
                n_segments=int(len(seg)),
                worktree_verification="见 doc/industry_allocation_feasibility.md "
                                      "§4.2 阶段 3 实施记录：git worktree 取改动前代码逐值对比，"
                                      "41 条字典 / 41 个 params_hash / 6 个维度 / "
                                      "CALIB_VERSION=s2.1 全部一致",
                note="库内只有一段 params_hash 时无法做「前后两段」逐值对比；"
                     "口径变更核验走 git worktree 新旧代码对比（阶段 3 已执行并留痕）。"
                     "本次阶段 4 未改动任何生产代码 / 字典 / params_hash，无需重跑")


def check_data_gates(conn, panel: pd.DataFrame, end: dt.date) -> dict:
    """两项明确「不可判定」+ 快照类时序分位启用门槛。"""
    items = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*), min(trade_date), max(trade_date)
            FROM industry.indicator_value
            WHERE market=%s AND classification=%s AND indicator_code=%s
              AND extra->>'label' = %s
        """, (MARKET, CLASSIFICATION, "SEC.CROWD.GATE", "EXTREME"))
        n_ext, d0, d1 = cur.fetchone()
        cur.execute("SELECT max(trade_date) FROM regime.sector_daily")
        px_max = cur.fetchone()[0]
        snapshots = {}
        for name, (col, table) in _SNAPSHOT_TABLES.items():
            cur.execute(f"SELECT count(DISTINCT {col}), max({col}) FROM {table}")
            c, mx = cur.fetchone()
            snapshots[name] = dict(observed_days=int(c), last=str(mx))
        cur.execute("SELECT count(*), min(trade_date), max(trade_date) "
                    "FROM industry.business_value WHERE market='HK'")
        hk_n, hk_d0, hk_d1 = cur.fetchone()

    # EXTREME 后 N 日可观测行数（用 L3 面板的 fwd 标签做保守上界）
    ext_rows = read_sql(conn, """
        SELECT trade_date, sector_code
        FROM industry.indicator_value
        WHERE market=%s AND classification=%s AND indicator_code=%s
          AND extra->>'label'=%s AND trade_date BETWEEN %s AND %s
    """, params=[MARKET, CLASSIFICATION, "SEC.CROWD.GATE", "EXTREME",
                 dt.date(1999, 12, 30), end])
    if not ext_rows.empty:
        ext_rows["trade_date"] = pd.to_datetime(ext_rows["trade_date"]).dt.date
    ext_obs = {}
    for n in (20, 60):
        if ext_rows.empty:
            ext_obs[str(n)] = 0
        else:
            m = ext_rows.merge(panel[["trade_date", "sector_code", f"fwd_{n}"]],
                               on=["trade_date", "sector_code"], how="left")
            ext_obs[str(n)] = int(m[f"fwd_{n}"].notna().sum())
    items.append(not_evaluable(
        "拥挤闸门有效性（GATE=EXTREME 后 20/60 日回撤）",
        dict(extreme_rows=int(n_ext), date_min=str(d0), date_max=str(d1),
             observable_fwd20_rows=ext_obs["20"],
             observable_fwd60_rows=ext_obs["60"], upstream_max=str(px_max)),
        "EXTREME 样本须覆盖 ≥60 个交易日之后的完整前瞻窗口，且行业×日样本足以做显著性检验",
        "45 行全部集中在最近约 30 个交易日，后 60 日收益完全不可观测；"
        "拿 20 日部分窗口出结论属选择性样本，按 §3.7 不通过处置降级为纯展示标记",
        "监控 industry.indicator_value 中 SEC.CROWD.GATE 的 EXTREME 行数与日期跨度；"
        "待 EXTREME 最早日 +60 交易日之后再复跑本脚本"))

    for name, s in snapshots.items():
        need = MIN_PERIODS - s["observed_days"]
        items.append(not_evaluable(
            f"快照类时序分位启用（{name}）",
            s, f"≥ {MIN_PERIODS} 个观测日（§5.7 min_periods）",
            f"仅 {s['observed_days']} 日，物理缺口；按 §5.7 一律不出分位、不得用短样本填充",
            f"该表已登记 monitor_table_config（day/1，断档即告警）；"
            f"按每月约 21 交易日估算还需 {max(need,0)} 日 ≈ "
            f"{max(need,0)/21:.1f} 个月，达标后须换 params_hash + 全历史重算再启用"))

    items.append(not_evaluable(
        "HK 排序有效性",
        dict(hk_biz_rows=int(hk_n), date_min=str(hk_d0), date_max=str(hk_d1)),
        "≥250 观测 + L3 可排名",
        "HK 阶段 3 决策 L3 不排名（rank_no 恒 NULL），且仅 52 日样本",
        "待 HK 全量回填 + 阶段 4 决策闸门对 CN 通过后，单独立项评估 HK 排序"))
    return dict(items=items,
                note="以上项目本期不产出通过/不通过结论；「诚实的空缺优于虚假的精确」")


def check_hk_sensitivity(conn, start: dt.date, end: dt.date) -> dict:
    """阶段 3 待排查项：最小成分数门槛 / 截尾敏感性（描述性，不改生产口径）。"""
    d = read_sql(conn, """
        SELECT trade_date, sector_code, ret_eq, ret_cap, n_constituent
        FROM industry.hk_index_daily
        WHERE trade_date BETWEEN %s AND %s
          AND ret_eq IS NOT NULL AND ret_cap IS NOT NULL
    """, params=[start, end])
    if d.empty:
        return {"error": "表 27 无数据"}
    out = {"n_rows": int(len(d)), "by_min_constituents": {}, "by_winsorize": {}}
    for k in (1, 3, 5, 10, 20):
        g = d[d["n_constituent"] >= k]
        if g.empty:
            continue
        out["by_min_constituents"][str(k)] = dict(
            rows=int(len(g)),
            mean_abs_diff_pp=_r((g["ret_eq"] - g["ret_cap"]).abs().mean(), 4),
            max_abs_diff_pp=_r((g["ret_eq"] - g["ret_cap"]).abs().max(), 4),
            corr=_r(g["ret_eq"].corr(g["ret_cap"]), 4),
            sign_flip_ratio=_r((np.sign(g["ret_eq"]) != np.sign(g["ret_cap"])).mean(), 4))
    lo, hi = d["ret_eq"].quantile(0.01), d["ret_eq"].quantile(0.99)
    for label, clipped in (("raw", d["ret_eq"]),
                           ("p1_p99", d["ret_eq"].clip(lo, hi)),
                           ("p5_p95", d["ret_eq"].clip(d["ret_eq"].quantile(.05),
                                                       d["ret_eq"].quantile(.95)))):
        out["by_winsorize"][label] = dict(
            mean_abs_diff_pp=_r((clipped - d["ret_cap"]).abs().mean(), 4),
            max_abs_diff_pp=_r((clipped - d["ret_cap"]).abs().max(), 4),
            corr=_r(clipped.corr(d["ret_cap"]), 4),
            sign_flip_ratio=_r((np.sign(clipped) != np.sign(d["ret_cap"])).mean(), 4))
    out["note"] = ("⚠ 截尾作用于**行业日收益**而非个股 change_pct，只是描述性近似；"
                   "真正个股级 winsorize 需改 hk_sector_index.py 并重算表 27，"
                   "本期按用户决策不改生产口径")
    return out


# ── 主流程 ──────────────────────────────────────────────────────────────────
def run(start: dt.date, end: dt.date, n_iter: int = BOOTSTRAP_DEFAULT) -> dict:
    rng = np.random.default_rng(RANDOM_SEED)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(trade_date) FROM industry.business_value "
                        "WHERE market=%s AND classification=%s",
                        (MARKET, CLASSIFICATION))
            last = cur.fetchone()[0]
        end = min(end, last or end)
        panel = load_panel(conn, start, end)
        result = dict(
            generated_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            scope=dict(market=MARKET, classification=CLASSIFICATION,
                       start=str(start), end=str(end), bootstrap=n_iter,
                       split_fraction=SPLIT_FRACTION, horizons=list(HORIZONS)),
            sample=dict(rows=int(len(panel)),
                        days=int(panel["trade_date"].nunique()) if not panel.empty else 0,
                        ranked_rows=int(panel["rank_no"].notna().sum()) if not panel.empty else 0))
        if panel.empty:
            result["error"] = "无 L3 读数，请先跑 sector_layer.py 全量/增量"
            return result
        result["ranking"] = check_ranking(panel, n_iter, rng)
        gc.collect()
        result["construct"] = check_construct(conn, start, end)
        result["shares"] = check_shares(conn, panel, start, end)
        result["pit_bias"] = check_pit_bias(conn, panel)
        result["calib_versions"] = check_calib_versions(panel)
        result["data_gates"] = check_data_gates(conn, panel, end)
        result["hk_sensitivity"] = check_hk_sensitivity(conn, start, end)
        result["gate_action"] = decision_gate(
            (result["ranking"]["horizons"].get("20", {}).get("ic_oos_bootstrap") or {}).get("ci_low"),
            (result["ranking"]["horizons"].get("20", {}).get("monotonicity") or {}).get("rho"),
            (result["ranking"]["horizons"].get("20", {}).get("monotonicity") or {}).get("p"))
    return result


def _print_report(r: dict) -> None:
    print("=" * 78)
    print("第④层行业配置 · 阶段 4 验证闭环（只读，不写库）")
    print("=" * 78)
    print("样本:", json.dumps(r.get("sample"), ensure_ascii=False),
          "| 区间:", json.dumps(r.get("scope"), ensure_ascii=False))
    if "error" in r:
        print("❌", r["error"])
        return
    rk = r["ranking"]
    print("\n--- ① 排序有效性（state_side → 未来 N 日行业超额收益）---")
    print("5 分组样本占比:", json.dumps(rk["quintile_share"], ensure_ascii=False),
          "| 样本外切分日:", rk["split_date"])
    print("  ", rk["quintile_note"])
    for h, v in rk["horizons"].items():
        print(f"  N={h}: IC天数={v.get('ic_days')} 全样本IC均值={v.get('ic_all_mean')} "
              f"样本外IC={v.get('ic_oos_mean')} (n={v.get('ic_oos_days')}) "
              f"bootstrap={v.get('ic_oos_bootstrap')}")
        print(f"        分组均值={v.get('quintile_mean')} 单调性={v.get('monotonicity')} "
              f"Q5-Q1={v.get('spread_q5_minus_q1')}")
    print("\n--- 决策闸门（§3.7）---")
    print(json.dumps(rk["decision"], ensure_ascii=False, indent=2))
    print("\n--- ② 构念效度（§8.5 门禁：成员载荷 ≥0.3、无负相关）---")
    for k, v in r["construct"].items():
        if v.get("status") == "NOT_APPLICABLE":
            print(f"  {k}: 不适用（{v['structure']}：{v['reason']}）")
            continue
        print(f"  {k}: α={v.get('cronbach_alpha')} 成员载荷={v.get('item_score_corr')} "
              f"未通过成员={v.get('failing_members')} 门禁={v.get('construct_gate')}")
        print(f"        一阶差分 PC1={v.get('diff_corr')}")
    print("\n--- ③ 各档样本占比 ---")
    print(json.dumps(r["shares"], ensure_ascii=False, indent=2))
    print("\n--- ④ 幸存者偏差分段 ---")
    print(json.dumps(r["pit_bias"], ensure_ascii=False, indent=2))
    print("\n--- ⑤ 口径变更 ---")
    print(json.dumps(r["calib_versions"], ensure_ascii=False, indent=2))
    print("\n--- ⛔ 数据不足·不可判定清单 ---")
    for it in r["data_gates"]["items"]:
        print(f"  [{it['status']}] {it['item']}: {it['reason']}")
        print(f"      observed={it['observed']}")
        print(f"      monitor={it['monitor']}")
    print("\n--- 🔍 HK 两版指数敏感性（描述性）---")
    print(json.dumps(r["hk_sensitivity"], ensure_ascii=False, indent=2))
    print("\n" + "=" * 78)
    print("结论：前瞻收益为样本内重叠窗口、未扣成本；bootstrap 只修正重叠标准误，")
    print("不替代样本外检验。本脚本只出结论，不修改任何生产代码 / 字典 / 历史读数。")
    print("=" * 78)


def _write_out(r: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sector_validation.json"), "w",
              encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2, default=str)
    panels = {
        "ranking_horizons.csv": pd.DataFrame(r.get("ranking", {}).get("horizons", {})).T,
        "share_quadrant.csv": pd.Series(r.get("shares", {}).get("quadrant_share", {}),
                                        name="share").to_frame(),
        "hk_sensitivity_minn.csv": pd.DataFrame(
            r.get("hk_sensitivity", {}).get("by_min_constituents", {})).T,
    }
    for name, df in panels.items():
        if not df.empty:
            df.to_csv(os.path.join(out_dir, name), encoding="utf-8")
    print(f"\n[+] 结果已写入 {out_dir}")


def _main() -> int:
    ap = argparse.ArgumentParser(description="第④层阶段 4 只读验证工作台")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=PIT_START)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--bootstrap", type=int, default=BOOTSTRAP_DEFAULT)
    ap.add_argument("--out", help="可选：落盘 JSON/CSV 到该目录（不写库）")
    a = ap.parse_args()
    r = run(a.start, a.end, a.bootstrap)
    _print_report(r)
    if a.out:
        _write_out(r, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
