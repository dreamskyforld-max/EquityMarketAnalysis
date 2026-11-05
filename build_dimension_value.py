#!/usr/bin/env python3
"""L2 维度指标的计算与落库（按 `dimension_code` 分派）+ §5.1 派生量。

上游：`tools/calibrate_*.py --write` 产出 `regime.dimension_dict`；
      `--backfill` 后的 `regime.indicator_value.percentile`。
下游：`regime.dimension_value`（一市场一日一维一行）。

已落地聚合结构：`block_equal`（`DIM.BREADTH`，分族取代表 → 族间等权）与
`single_anchor`（`DIM.VALUATION`，锚定档、校验项只调置信）；`build_all_dimensions()` 遍历所有
`is_active` 维度，`--dim` 可单独重算某一维度。

**新增维度（刻意不做插件发现，保持简单）**：只需 ① 写一个独立的
`build_xxx_dimension(...)` 函数（签名与返回结构照抄 `build_valuation_dimension`）→ ② 在
`build_all_dimensions()` 的分派 if/elif 加一个分支 → ③ 写 `dimension_dict` 一行
→ ④ 补独立测试。不得修改现有两个生产者（各含维度专属逻辑）。
完整步骤见 `build_all_dimensions()` 内注释，
「新维度不影响现有维度」由 `tests/test_dimension_isolation.py` 回归钉住。
阈值（`flag_gap` / `flag_extreme` / `min_periods` 等）从各自的
`dimension_dict.params` 读取，缺省回退模块常量，故现有维度逐值不变。

按 `doc/market_profile.md` §5.7 L2-c 产出五件套：
  score / state(5 档) / coverage / dispersion / contrib，外加 blocks / flags / confidence。
门禁：`coverage < coverage_min` 或 `dispersion_norm > dispersion_max`
      → **只输出 score + 低置信，不输出 state**（宁可说不确定，不给确定的错结论）。

派生量按 §5.1「不落表，由消费方按固定 model_version 重算」：A/D Line、McClellan
Summation Index、A/D 与指数双高点背离。它们不写库，由 `--derived` 输出。

用法：
    .venv/bin/python3 build_dimension_value.py --write            # 增量（最近 45 天）
    .venv/bin/python3 build_dimension_value.py --write --backfill # 全历史
    .venv/bin/python3 build_dimension_value.py --derived --market CN   # 打印派生量
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys

import numpy as np
import pandas as pd

from db import get_conn, bulk_upsert, read_sql

DIM_BREADTH = "DIM.BREADTH"
DIM_VALUATION = "DIM.VALUATION"
DIMENSION_VALUE_TABLE = "regime.dimension_value"
MIN_PERIODS = 250          # §2.4 规则 1：有效观测不足 250 不计算分位 / 不出档
DATA_START = {"CN": datetime.date(1990, 1, 1), "HK": datetime.date(2000, 1, 1)}
BENCH_CODE = {"CN": "SH.000001", "HK": "HK.800000"}
HK_BENCH_START = datetime.date(2013, 8, 20)   # daily_benchmark.HK.800000 实际起点

# 结构性背离判据（§5.7 L2-c flags 表）
FLAG_GAP = 15.0          # 快慢成员分位差
FLAG_EXTREME = 20.0      # 极端分位


def _read_sql(sql, conn, params=None):
    """统一走 `db.read_sql`（全项目唯一读库入口，避免 pandas DBAPI2 警告刷屏）。

    保持 `pd.read_sql_query` 的调用形态，便于既有 SQL 与 mock 直接复用。
    """
    return read_sql(conn, sql, params=params)


# ── §5.1 派生量（纯函数，不落表）──────────────────────────────────────────────
def ad_line(adv_net: pd.Series) -> pd.Series:
    """A/D Line = 归一化净广度增量的 cumsum（只存增量，累计由消费方算）。"""
    return pd.Series(adv_net, dtype=float).cumsum()


def summation_index(mcc: pd.Series) -> pd.Series:
    """McClellan Summation Index，`SI_0 = 0` **固定**（v2.1.7 删除「起点任选」）。"""
    return pd.Series(mcc, dtype=float).cumsum()


def ad_divergence(index_close: pd.Series, ad: pd.Series,
                  n: int = 60, gap: int = 20, sigma_win: int = 60,
                  max_streak: int = 60, detrend: bool = True) -> pd.DataFrame:
    """A/D 与指数的**双高点**背离（§5.1 v2.1.7 重做的口径）。

    顶背离 = 指数出现两个依次抬高的 N 日新高（间隔 ≥ gap 交易日），
             且 A/D 在对应两个高点处依次降低；底背离对称。
    幅度  = 两个 A/D 高点之差 / A/D 的 sigma_win 日差分标准差（**z 单位**），
            不用「距高点百分比」——累计量接近 0 时分母不稳定。
    返回逐日 [direction, amplitude_z, days]；days = 背离成立以来的连续交易日数。
    """
    close = pd.Series(index_close, dtype=float).dropna()
    a = pd.Series(ad, dtype=float).reindex(close.index)
    hi = close.rolling(n, min_periods=n).max()
    lo = close.rolling(n, min_periods=n).min()
    # ⚠ A/D Line 是 cumsum 累计量、带长期趋势：直接比较两点的**绝对水平**会被
    #   趋势支配（实测 HK 底背离恒 0、CN 顶背离曾停在 2007 年）。故默认比较
    #   「相对自身 N 日均线的标准化偏离」，即用相对位置而非绝对水位（§5.1）。
    if detrend:
        mu = a.rolling(n, min_periods=max(n // 2, 5)).mean()
        sd = a.rolling(n, min_periods=max(n // 2, 5)).std()
        a_cmp = (a - mu) / sd.replace(0, np.nan)
    else:
        a_cmp = a
    sigma = a_cmp.rolling(sigma_win, min_periods=min(20, sigma_win)).std()

    last_hi = None      # (pos, close, ad_relative)
    last_lo = None
    direction, amplitude, streak = "无", np.nan, 0
    rows = []
    for i, d in enumerate(close.index):
        c, av, sig = close.iloc[i], a_cmp.iloc[i], sigma.iloc[i]
        # ⚠ 高点对必须是**相近时期**的局部结构（文档的 N 日窗口语义）。超过 2N 日
        #   未刷新就重置，否则一次历史大顶会把之后的比较永久锁死：实测 CN 在
        #   2007-03-19 后再无顶背离、底背离一次都不触发。取 2N 是为了给
        #   「间隔 >= gap」留出余量，同时仍限定在半年量级内。
        if last_hi is not None and i - last_hi[0] > 2 * n:
            last_hi = None
        if last_lo is not None and i - last_lo[0] > 2 * n:
            last_lo = None
        hit = None
        # ⚠ 只有「间隔 >= gap 且**依次抬高**」才构成新的高点对并刷新基准。
        #   间隔不足或高度未抬高的「伪新高」必须忽略——否则同一波上涨里的连续
        #   新高会不断刷新时间戳，把真正的高点对挤出 gap 窗口（实测踩过）。
        if not pd.isna(hi.iloc[i]) and c >= hi.iloc[i]:
            if last_hi is None:
                last_hi = (i, c, av)
            elif i - last_hi[0] >= gap and c > last_hi[1]:
                if not pd.isna(av) and not pd.isna(last_hi[2]) and av < last_hi[2]:
                    z = ((av - last_hi[2]) / sig
                         if not pd.isna(sig) and sig > 0 else np.nan)
                    hit = ("顶背离", z)
                last_hi = (i, c, av)
        if not pd.isna(lo.iloc[i]) and c <= lo.iloc[i]:
            if last_lo is None:
                last_lo = (i, c, av)
            elif i - last_lo[0] >= gap and c < last_lo[1]:
                if not pd.isna(av) and not pd.isna(last_lo[2]) and av > last_lo[2]:
                    z = ((av - last_lo[2]) / sig
                         if not pd.isna(sig) and sig > 0 else np.nan)
                    hit = ("底背离", z)
                last_lo = (i, c, av)
        if hit:
            direction, amplitude, streak = hit[0], hit[1], 1
        elif direction != "无":
            streak += 1
            if streak > max_streak:
                direction, amplitude, streak = "无", np.nan, 0
        else:
            streak = 0
        rows.append((direction, amplitude, streak if direction != "无" else 0))
    return pd.DataFrame(rows, index=close.index,
                        columns=["direction", "amplitude_z", "days"])


def build_breadth_derived_frame(conn, market: str, start: datetime.date,
                  end: datetime.date) -> pd.DataFrame:
    """读库组装三项派生量（不落表）。HK 恒指仅 2013-08-20 起 → 之前不输出背离。"""
    codes = ["BREADTH.ADV_MINUS_DEC", "BREADTH.MCCLELLAN"]
    raw = _read_sql(
        "SELECT trade_date, indicator_code, raw_value FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date BETWEEN %s AND %s "
        "  AND raw_value IS NOT NULL ORDER BY trade_date",
        conn, params=[market, codes, start, end])
    wide = raw.pivot_table(index="trade_date", columns="indicator_code",
                           values="raw_value", aggfunc="last").sort_index()
    if wide.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=wide.index)
    out["ad_line"] = ad_line(wide.get("BREADTH.ADV_MINUS_DEC", pd.Series(dtype=float)))
    out["summation_index"] = summation_index(
        wide.get("BREADTH.MCCLELLAN", pd.Series(dtype=float)))
    bench = _read_sql(
        "SELECT trade_date, last_price FROM daily_benchmark "
        "WHERE bench_code=%s AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        conn, params=[BENCH_CODE[market], start, end])
    close = pd.Series(bench["last_price"].to_numpy(dtype=float),
                      index=pd.to_datetime(bench["trade_date"]).dt.date)
    close = close[close.index >= (HK_BENCH_START if market == "HK" else start)]
    dv = ad_divergence(close, out["ad_line"].reindex(close.index))
    out = out.join(dv)
    out["benchmark_mismatch"] = True    # 上证/恒指均为窄基，与全市场宽度池不同构
    out["sample_start"] = str(close.index.min()) if len(close) else str(start)
    # dv 只覆盖指数可得区间；join 后缺失日（含 HK 2013-08 前、指数晚一日）一律「无背离」
    out["direction"] = out["direction"].fillna("无")
    out["days"] = pd.to_numeric(out["days"], errors="coerce").fillna(0).astype(int)
    return out


# ── L2 维度指标 ───────────────────────────────────────────────────────────────
def _parse_jsonb(raw_jsonb_value):
    return (json.loads(raw_jsonb_value)
            if isinstance(raw_jsonb_value, str) else raw_jsonb_value)


def load_dimension_dict(conn, dim_code: str = DIM_BREADTH):
    """按 dimension_code 读字典；默认仍是宽度（保持既有单维度调用兼容）。"""
    df = _read_sql(
        "SELECT members, state_bands, state_labels, params, params_hash "
        "FROM regime.dimension_dict WHERE dimension_code=%s AND is_active=TRUE",
        conn, params=[dim_code])
    if df.empty:
        raise RuntimeError(
            f"regime.dimension_dict 无 {dim_code}；请先跑对应的 "
            "tools/calibrate_*.py --write（标定 + 切点冻结）")
    r = df.iloc[0]
    dimension_config = {k: _parse_jsonb(r[k]) for k in ("members", "state_bands", "state_labels", "params")}
    return dimension_config, r["params_hash"]


def _load_market_scopes(conn, codes):
    df = _read_sql(
        "SELECT indicator_code, market_scope FROM regime.indicator_dict "
        "WHERE indicator_code = ANY(%s)", conn, params=[codes])
    return dict(zip(df["indicator_code"], df["market_scope"]))


def _load_percentiles(conn, market, codes, start, end):
    """as-of 分位（上游已落库即终值）。带实体过滤 + 时间下界（资源规范 R7）。"""
    df = _read_sql(
        "SELECT trade_date, indicator_code, percentile FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date BETWEEN %s AND %s "
        "  AND percentile IS NOT NULL ORDER BY trade_date",
        conn, params=[market, codes, start, end])
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="trade_date", columns="indicator_code",
                          values="percentile", aggfunc="last").sort_index()


def _breadth_flags(aligned: pd.DataFrame, trade_date, flag_gap: float,
                   flag_extreme: float) -> list[str]:
    """`DIM.BREADTH` 专属的结构性背离 flags（§5.7 L2-c，v2.1.9）。

    ⚠ **宽度专属**：列名硬编码 `BREADTH.*`，其它维度不要复用。新增维度应在自己的
    生产者里实现 flags（或返回空列表），避免像此前那样静默产出空数组而不报错。
    判据阈值来自 `dimension_dict.params.flag_gap` / `flag_extreme`。
    """
    flags = []

    def aligned_value_at(indicator_code):
        if indicator_code not in aligned.columns:
            return None
        aligned_value = aligned.loc[trade_date, indicator_code]
        return None if pd.isna(aligned_value) else float(aligned_value)

    short_window_value = aligned_value_at("BREADTH.NEW_HIGH_LOW_20")
    long_window_value = aligned_value_at("BREADTH.NEW_HIGH_LOW_60")
    if (short_window_value is not None and long_window_value is not None
            and short_window_value < long_window_value - flag_gap):
        flags.append("短窗动能衰竭（中期趋势未破）")

    short_window_value = aligned_value_at("BREADTH.PCT_ABOVE_MA60")
    long_window_value = aligned_value_at("BREADTH.PCT_ABOVE_MA200")
    if (short_window_value is not None and long_window_value is not None
            and short_window_value > long_window_value + flag_gap):
        flags.append("短期反弹但中期未修复")

    equity_vs_cap_value = aligned_value_at("BREADTH.EQ_VS_CAP_20D")
    if equity_vs_cap_value is not None and equity_vs_cap_value < flag_extreme:
        flags.append("权重股掩盖多数股票下跌")

    turnover_concentration = aligned_value_at("BREADTH.AMT_CONC_TOP10")
    if turnover_concentration is not None and turnover_concentration < flag_extreme:
        flags.append("成交额集中度偏高")
    return flags


def build_breadth_dimension(
        conn, dimension_config: dict, params_hash: str, market: str,
        start: datetime.date, end: datetime.date, write: bool,
        dim_code: str = DIM_BREADTH) -> dict:
    """`DIM.BREADTH` 生产者：分族取代表 → 族间等权（§5.1 / §5.7 L2-b）。

    ⚠ 宽度专属实现：`flags` 由 `_breadth_flags()` 生成（列名硬编码 `BREADTH.*`），
    `blocks` 带 `representative` 字段。其它维度不要复用，应写自己的生产者。
    """
    members = dimension_config["members"]
    bands = list(dimension_config["state_bands"][market])
    labels = list(dimension_config["state_labels"])
    params = dimension_config["params"]
    coverage_min = float(params["coverage_min"])
    dispersion_max = float(params["dispersion_max"])
    weight_cap = float(params["weight_cap"])
    # 代表级覆盖门禁（§5.1）：字典登记后启用；旧种子未登记该键 → 默认 0.0 保持旧行为，
    # 避免在 params_hash 未变的情况下悄悄改变口径（2026-11-05 修）。
    rep_coverage_min = float(params.get("rep_coverage_min", 0.0))
    # 阈值可由字典覆盖（缺省回退模块常量，保证既有维度逐值不变）；
    # 新维度需要不同判据时只改自己的 dimension_dict.params，无需动本函数。
    flag_gap = float(params.get("flag_gap", FLAG_GAP))
    flag_extreme = float(params.get("flag_extreme", FLAG_EXTREME))

    all_codes = [m["code"] for m in members]
    scopes = _load_market_scopes(conn, all_codes)
    ok_scope = lambda indicator_code: scopes.get(indicator_code) in (market, "CN+HK", "GLOBAL")
    # coverage 分母（§5.7 L2-c）
    eligible = [member_code for member_code in all_codes
                if ok_scope(member_code)]
    percentiles = _load_percentiles(conn, market, eligible, start, end)
    if percentiles.empty:
        return {"rows": 0, "dates": 0}
    aligned = percentiles.copy()
    for indicator_code in eligible:
        if indicator_code not in aligned.columns:
            aligned[indicator_code] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for indicator_code in eligible:
        if sign.get(indicator_code, 1) == -1:
            aligned[indicator_code] = 100.0 - aligned[indicator_code]                    # 极性统一（high_risk 翻向）

    # 族 → 每族固定 1 个代表 → 族间等权；flags_only 成员不进 score
    blocks = {}
    for m in members:
        if m.get("flags_only") or not ok_scope(m["code"]):
            continue
        blocks.setdefault(m["block"], []).append(m)
    reps = []
    for block_members in blocks.values():
        representative_code = next((x["code"] for x in block_members if x.get("representative")), None)
        if representative_code and representative_code in aligned.columns:
            reps.append(representative_code)
    n_eff = len(reps)
    weight_per_representative = min(1.0 / n_eff, weight_cap) if n_eff else 0.0       # 单成员权重上限 25%
    total_weight = weight_per_representative * n_eff
    weights = pd.Series({
        representative_code: (weight_per_representative / total_weight
                             if total_weight else 0.0)
        for representative_code in reps
    })
    rep_frame = aligned[reps] if reps else pd.DataFrame(index=aligned.index)
    weighted_score_sum = rep_frame.mul(weights, axis=1).sum(axis=1, min_count=1)
    present_weight_sum = rep_frame.notna().mul(weights, axis=1).sum(axis=1)
    score = (weighted_score_sum / present_weight_sum.replace(0, np.nan)).clip(0, 100) if reps else pd.Series(
        np.nan, index=aligned.index)

    # 代表级覆盖：score 是全部代表的等权均值，缺任一代表即口径不完整；
    # coverage 的分母含非代表成员，单独用它挡不住「只剩部分代表仍出档」。
    rep_valid = rep_frame.notna().sum(axis=1)
    rep_coverage = rep_valid / max(n_eff, 1)

    valid = aligned[eligible].notna().sum(axis=1)
    coverage = valid / max(len(eligible), 1)
    dispersion = aligned[eligible].std(axis=1, ddof=1)
    dispersion_norm = dispersion * np.sqrt(valid / 10.0)

    band_index = np.digitize(score.fillna(-1).to_numpy(), bands)      # 等值归较高档（§8.1.1）
    state = pd.Series([labels[int(band_level)] for band_level in band_index], index=score.index)
    gate_passed = ((coverage >= coverage_min) & (dispersion_norm <= dispersion_max)
          & score.notna() & (rep_coverage >= rep_coverage_min))
    state = state.where(gate_passed, None)
    confidence = pd.Series("低", index=score.index)
    high = gate_passed & (coverage >= 0.8) & (dispersion_norm <= 20.0)
    confidence[high] = "高"
    confidence[gate_passed & ~high] = "中"

    def _contrib(trade_date):
        deviation = (aligned.loc[trade_date, eligible] - score.loc[trade_date]).dropna()
        def _lst(deviations):
            return [{"code": indicator_code,
                    "delta": round(float(deviation_value), 2)}
                    for indicator_code, deviation_value in deviations.items()]
        return {"pos": _lst(deviation.nlargest(3)), "neg": _lst(deviation.nsmallest(3))}

    def _flags(trade_date):
        # 宽度专属实现已隔离到模块级函数；此处只做参数绑定。
        return _breadth_flags(aligned, trade_date, flag_gap, flag_extreme)

    def _block_scores(trade_date):
        block_score_map = {}
        for block_name, block_members in blocks.items():
            block_codes = [m["code"] for m in block_members if ok_scope(m["code"])]
            representative_code = next((m["code"] for m in block_members if m.get("representative")), None)
            member_values = [
                aligned.loc[trade_date, indicator_code]
                for indicator_code in block_codes
                if indicator_code in aligned.columns
            ]
            valid_member_count = sum(
                1 for aligned_value in member_values
                if not pd.isna(aligned_value))
            representative_score = (
                aligned.loc[trade_date, representative_code]
                if representative_code in aligned.columns else np.nan
            )
            block_score_map[block_name] = {
                "score": None if pd.isna(representative_score) else round(float(representative_score), 2),
                "coverage": round(valid_member_count / len(block_codes), 4) if block_codes else None,
                "representative": representative_code,
            }
        return block_score_map

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for trade_date in score.index:
        if pd.isna(score.loc[trade_date]) and valid.loc[trade_date] == 0:
            continue
        rows.append({
            "dimension_code": dim_code, "market": market, "trade_date": trade_date,
            "score": None if pd.isna(score.loc[trade_date]) else round(float(score.loc[trade_date]), 2),
            # Series.where(..., None) 在 object dtype 下仍会是 NaN；显式转 None，
            # 保证「不出档」语义在入库前就是明确的 NULL，不依赖写入层兜底。
            "state": None if pd.isna(state.loc[trade_date]) else str(state.loc[trade_date]),
            "coverage": round(float(coverage.loc[trade_date]), 4),
            "dispersion": None if pd.isna(dispersion.loc[trade_date]) else round(float(dispersion.loc[trade_date]), 2),
            "blocks": _block_scores(trade_date),
            "contrib": _contrib(trade_date),
            "flags": _flags(trade_date),
            "confidence": confidence.loc[trade_date],
            "is_final": True,                 # 纯派生量，收盘即终值
            "model_version": params_hash,
            "updated_at": now,
        })
    if write and rows:
        # skip_null_updates=False：门禁不通过时 state=NULL 必须能覆盖旧档（否则旧结论残留）
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(score.index),
            "state_rows": int(gate_passed.sum()), "reps": reps}


# ── single_anchor（DIM.VALUATION，§5.2 / market_profile §5.2 v2.1.14）─────────
def _anchor_sample_count(conn, market: str, anchor: str, end) -> pd.Series:
    """锚指标截至 end 的**全历史**有效观测数（as-of，供 n_sample / 短样本降置信）。

    只读单个指标的日期列（<1 万行、带 `trade_date<=end` 下界，资源规范 R7），
    避免增量运行（窗口仅 45 天）把 n_sample 错算成 45。
    """
    df = _read_sql(
        "SELECT trade_date FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=%s AND percentile IS NOT NULL "
        "AND trade_date<=%s ORDER BY trade_date",
        conn, params=[market, anchor, end])
    if df.empty:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(df["trade_date"]).dt.date
    return pd.Series(np.arange(1, len(df) + 1, dtype=float), index=idx)


def build_valuation_dimension(
        conn, dimension_config: dict, params_hash: str, market: str,
        start: datetime.date, end: datetime.date, write: bool,
        dim_code: str = DIM_VALUATION) -> dict:
    """`DIM.VALUATION` 生产者：单锚定档 + 校验（§5.2 计算模型）。

    `score` 取锚分位，校验项 `weight=0` 只影响 `confidence` / `contrib` / `flags`。
    与 `build_breadth_dimension` 刻意分开：两者的 weight 结构、CN 口径约束、
    `blocks` / `flags` 形状都不同，套族代表逻辑会把「写了几个指标」当权重。
    """
    members = dimension_config["members"]
    params = dimension_config["params"]
    bands = list(dimension_config["state_bands"][market])
    labels = list(dimension_config["state_labels"])
    coverage_min = float(params["coverage_min"])
    dispersion_max = float(params["dispersion_max"])
    quality_sample = float(params.get("quality_sample", 750))
    check_divergence = float(params.get("check_divergence", 20))
    min_periods = float(params.get("min_periods", MIN_PERIODS))
    # ⚠ 缺省 **False**：新维度不在字典里写这个键就正常出档，避免静默继承
    #   DIM.VALUATION 的 CN 口径约束。估值字典已显式写 true，行为不变。
    cn_state_disabled = bool(params.get("cn_state_disabled", False))

    all_codes = [m["code"] for m in members]
    scopes = _load_market_scopes(conn, all_codes)
    ok_scope = lambda indicator_code: scopes.get(indicator_code) in (market, "CN+HK", "GLOBAL")
    eligible = [indicator_code for indicator_code in all_codes if ok_scope(indicator_code)]
    # 低频 PIT 偏差标注只列本市场实际参与的成员（HK 无 CN-only 的 VAL.BUFFETT）
    low_freq = sorted(set(params.get("low_freq_members", [])) & set(eligible))
    anchor = next((m["code"] for m in members if m.get("block") == "anchor"), None)
    if anchor is None or anchor not in eligible:
        return {"rows": 0, "dates": 0, "state_rows": 0}

    percentiles = _load_percentiles(conn, market, eligible, start, end)
    if percentiles.empty:
        return {"rows": 0, "dates": 0, "state_rows": 0}
    aligned = percentiles.copy()
    for indicator_code in eligible:
        if indicator_code not in aligned.columns:
            aligned[indicator_code] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for indicator_code in eligible:
        if sign.get(indicator_code, 1) == -1:
            aligned[indicator_code] = 100.0 - aligned[indicator_code]        # §2.4 规则 2：极性统一
    score = aligned[anchor].clip(0, 100)

    valid = aligned[eligible].notna().sum(axis=1)
    coverage = valid / max(len(eligible), 1)
    dispersion = aligned[eligible].std(axis=1, ddof=1)
    dispersion_norm = dispersion * np.sqrt(valid / 10.0)
    n_sample = _anchor_sample_count(conn, market, anchor, end).reindex(
        aligned.index).ffill().fillna(0)

    checks = [indicator_code for indicator_code in eligible if indicator_code != anchor]
    divergent = pd.Series(False, index=aligned.index)
    for indicator_code in checks:
        member_aligned_value = aligned[indicator_code]
        far = (member_aligned_value - score).abs() > check_divergence
        opposite = ((member_aligned_value >= 60) & (score <= 40)) | ((member_aligned_value <= 40) & (score >= 60))
        divergent = divergent | (far & opposite)

    band_index = np.digitize(score.fillna(-1).to_numpy(), bands)
    state = pd.Series([labels[int(band_level)] for band_level in band_index], index=score.index)
    gate_passed = (score.notna() & (n_sample >= min_periods)
          & (coverage >= coverage_min) & (dispersion_norm <= dispersion_max))
    state = state.where(gate_passed, None)

    confidence_level = pd.Series(2, index=score.index)           # 0=低 1=中 2=高
    confidence_level[~gate_passed] = 0
    confidence_level[n_sample < quality_sample] = 0
    confidence_level[divergent] = (confidence_level[divergent] - 1).clip(lower=0)
    confidence = confidence_level.map({0: "低", 1: "中", 2: "高"})

    # CN 口径约束（§5.2 v1.1）：数据缺口从「没有」变成「有但不可比」，
    # state 恒 NULL、confidence 低、flags.short_sample=true；score 照出。
    if market == "CN" and cn_state_disabled:
        state = pd.Series([None] * len(score), index=score.index)
        confidence = pd.Series("低", index=score.index)

    def _contrib(trade_date):
        deviation = (aligned.loc[trade_date, checks] - score.loc[trade_date]).dropna()
        # 按符号分组再取 top3：成员只有 5 个时，直接 nlargest(3)/nsmallest(3)
        # 会让同一个成员同时出现在 pos 与 neg，且 pos 里出现负 delta。
        # 0.005 = 保留两位小数后仍非零的最小幅度，避免写出 delta=0.0 的「正负贡献」
        positive_contributors = deviation[deviation > 0.005].nlargest(3)
        negative_contributors = deviation[deviation < -0.005].nsmallest(3)
        return {
            "pos": [{"code": k, "delta": round(float(v), 2)} for k, v in positive_contributors.items()],
            "neg": [{"code": k, "delta": round(float(v), 2)} for k, v in negative_contributors.items()],
        }

    def _blocks(trade_date):
        ap = aligned.loc[trade_date, anchor]
        return {
            "anchor": {"code": anchor,
                       "percentile": None if pd.isna(ap) else round(float(ap), 2)},
            "check": {"n": len(checks), "divergent": bool(divergent.loc[trade_date])},
        }

    def _flags(trade_date):
        flags = {"n_sample": int(n_sample.loc[trade_date]), "anchor_window": "expanding"}
        if pd.isna(score.loc[trade_date]):
            flags["no_data"] = True
        if market == "CN" and cn_state_disabled:
            flags["short_sample"] = True
        if low_freq:
            flags["low_freq_pit_dup"] = low_freq    # §2.4 规则 1 已知偏差，仅标注不改数值
        return flags

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for trade_date in score.index:
        # 锚缺失即 no_data（原理 6）：不回落其它成员，也不写行。
        # 这同时保证 CN 在 VALUATION_ASOF_START 之前「整体不产出」——即便
        # DIV_SPREAD 等独立口径成员当时已有值，也不该生成一条 score=NULL 的维度行。
        if pd.isna(score.loc[trade_date]):
            continue
        rows.append({
            "dimension_code": dim_code, "market": market, "trade_date": trade_date,
            "score": None if pd.isna(score.loc[trade_date]) else round(float(score.loc[trade_date]), 2),
            "state": None if pd.isna(state.loc[trade_date]) else str(state.loc[trade_date]),
            "coverage": round(float(coverage.loc[trade_date]), 4),
            "dispersion": None if pd.isna(dispersion.loc[trade_date]) else round(float(dispersion.loc[trade_date]), 2),
            "blocks": _blocks(trade_date), "contrib": _contrib(trade_date), "flags": _flags(trade_date),
            "confidence": confidence.loc[trade_date], "is_final": True,
            "model_version": params_hash, "updated_at": now,
        })
    if write and rows:
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(score.index),
            "state_rows": 0 if (market == "CN" and cn_state_disabled) else int(gate_passed.sum())}


def _active_dimensions(conn):
    """所有启用维度 → [dimension_code]；新维度写入字典即自动接入日频计算。"""
    df = _read_sql("SELECT dimension_code FROM regime.dimension_dict "
                   "WHERE is_active=TRUE ORDER BY dimension_code", conn)
    return list(df["dimension_code"]) if not df.empty else []


def build_all_dimensions(conn, start=None, end=None, backfill=False, write=True,
          markets=("CN", "HK"), only_dims=None):
    """供 `market_state_daily.run()` 接线调用；按字典遍历所有启用的 L2 维度。

    `only_dims` 供命令行 `--dim` 单独重算某一维度（避免全量重算时触碰其它维度
    已有的 `updated_at`）；调度器不传 → 全部启用维度。

    返回仍是 `{market: {...}}`，保留 `rows` / `state_rows` / `reps` 键
    （`market_state_daily.py` 与本模块 `main()` 的打印依赖它们），另加 `dims` 明细。
    """
    end = end or datetime.date.today()
    active_dimensions = _active_dimensions(conn)
    if only_dims:
        active_dimensions = [code for code in active_dimensions
                             if code in set(only_dims)]
    stats = {}
    for market_code in markets:
        window_start = start or (DATA_START[market_code] if backfill else end - datetime.timedelta(days=45))
        window_start = max(window_start, DATA_START[market_code])
        market_stats = {"rows": 0, "dates": 0, "state_rows": 0, "dims": {}}
        # ── 新增 L2 维度的最小改动点（仅此一处分派 + 一个新函数）────────────────
        #   ① 在本模块新增 `build_xxx_dimension(conn, dimension_config, params_hash,
        #      market, start, end, write, dim_code)`，照抄 build_valuation_dimension 的签名与返回结构
        #      {rows, dates, state_rows}；自己实现 score / state / flags。
        #      ⚠ **不要改 build_breadth_dimension / build_valuation_dimension** —— 它们含维度专属
        #      逻辑（前者 flags 硬编码 BREADTH.*，后者含估值 CN 口径约束）。
        #   ② 在下方 if/elif 按 dimension_code 加一个分支；未注册维度只跳过本维度。
        #   ③ 写 dimension_dict 一行（state_bands 必须是 {"CN":[..], "HK":[..]}），
        #      is_active=TRUE 即自动接入日频，无需改调度器 / 监控 / 写入层。
        #   ④ 补独立测试文件，不要改现有两维度的测试。
        #   现有维度零影响由 tests/test_dimension_isolation.py 钉住。
        # ────────────────────────────────────────────────────────────────────
        for dimension_code in active_dimensions:
            try:
                dimension_config, dimension_params_hash = load_dimension_dict(conn, dimension_code)
            except RuntimeError as e:
                print(f"  ⚠ L2 {dimension_code} 跳过：{e}")
                continue
            if dimension_code == DIM_BREADTH:
                dimension_stats = build_breadth_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            elif dimension_code == DIM_VALUATION:
                dimension_stats = build_valuation_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            else:
                print(f"  ⚠ L2 {dimension_code} 无生产者，跳过")
                continue
            market_stats["dims"][dimension_code] = dimension_stats
            market_stats["rows"] += dimension_stats["rows"]
            market_stats["state_rows"] += dimension_stats.get("state_rows", 0)
            market_stats["dates"] = max(market_stats["dates"], dimension_stats["dates"])
        # 不绑定某个具体维度编码：取第一个声明了 reps 的维度，供 main() 兼容打印
        market_stats["reps"] = next(
            (stats_for_dimension["reps"]
             for stats_for_dimension in market_stats["dims"].values()
             if stats_for_dimension.get("reps")),
            [])
        stats[market_code] = market_stats
    return stats


def run(codes=None, ctx=None, market=None, start=None, end=None,
        backfill=False, write=True) -> dict:
    """兼容 `market_scheduler` 的 run_module 调用约定。"""
    markets = [market] if market else ["CN", "HK"]
    with get_conn() as conn:
        return build_all_dimensions(
            conn, start=start, end=end, backfill=backfill,
            write=write, markets=markets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写 regime.dimension_value")
    ap.add_argument("--backfill", action="store_true", help="全历史")
    ap.add_argument("--derived", action="store_true", help="打印派生量（不落表）")
    ap.add_argument("--market", choices=["CN", "HK"])
    ap.add_argument("--dim", action="append", dest="dimension_codes",
                    help="只算指定维度（可重复，如 --dim DIM.VALUATION）；默认全部启用维度")
    ap.add_argument("--start", type=datetime.date.fromisoformat)
    ap.add_argument("--end", type=datetime.date.fromisoformat)
    args = ap.parse_args()
    markets = [args.market] if args.market else ["CN", "HK"]
    end = args.end or datetime.date.today()
    with get_conn() as conn:
        if args.derived:
            for market_code in markets:
                window_start = args.start or DATA_START[market_code]
                derived_table = build_breadth_derived_frame(
                    conn, market_code, window_start, end)
                print(f"===== {market_code} 派生量（{len(derived_table)} 日）=====")
                print(derived_table.tail(10).to_string())
            return
        stats = build_all_dimensions(
            conn, start=args.start, end=end, backfill=args.backfill,
                   write=args.write, markets=markets,
                   only_dims=args.dimension_codes)
        for market_code, market_stats in stats.items():
            print(f"{market_code}: {market_stats['dates']} 日 / "
                  f"{market_stats['rows']} 行 / 出档 "
                  f"{market_stats['state_rows']} 日")
            for dimension_code, dimension_stats in market_stats.get("dims", {}).items():
                print(f"    {dimension_code}: {dimension_stats['rows']} 行 / "
                      f"出档 {dimension_stats.get('state_rows', 0)} 日")
            if market_stats.get("reps"):
                print(f"    代表 {market_stats['reps']}")


if __name__ == "__main__":
    main()
