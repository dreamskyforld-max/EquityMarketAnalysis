#!/usr/bin/env python3
"""L2 维度指标的计算与落库（按 `dimension_code` 分派）+ §5.1 派生量。

上游：`tools/calibrate_*.py --write` 产出 `regime.dimension_dict`；
      `--backfill` 后的 `regime.indicator_value.percentile`。
下游：`regime.dimension_value`（一市场一日一维一行）。

已落地聚合结构：`block_equal`（`DIM.BREADTH`，分族取代表 → 族间等权）、
`single_anchor`（`DIM.VALUATION`，锚定档、校验项只调置信）与
`fixed_core`（`DIM.SENTIMENT`，固定核心成员集 + 冻结有效起点 + 诊断成员）、
按市场单锚（`DIM.FLOW`）与固定核心集 + 4 块两级等权（`DIM.STRESS`）。
`build_all_dimensions()` 遍历所有 `is_active` 维度，`--dim` 可单独重算某一维度。

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

`--audit` 是 `doc/market_state_data_architecture.md` §5.1 的**只读契约自检**：核验 11 个
宽度原子是否采集 / 分位化、字典与 L2 计算契约是否自洽、最新一日出档门禁为何通过或未通过。
它不写库、不改口径，用于回答「§5.1 的维度指标现在能不能算对」。

⚠ 口径变更纪律（§2.2）：任何改变 `dimension_value` 输出形状的修正都必须由
`dimension_dict.params` 显式登记并触发 `params_hash` 变化 + L2 全量重算，禁止在
`params_hash` 未变时改变输出。`contrib_sign_split` 即按此纪律做成默认关闭的字典开关。

用法：
    .venv/bin/python3 build_dimension_value.py --write            # 增量（最近 45 天）
    .venv/bin/python3 build_dimension_value.py --write --backfill # 全历史
    .venv/bin/python3 build_dimension_value.py --derived --market CN   # 打印派生量
    .venv/bin/python3 build_dimension_value.py --audit            # §5.1 契约自检（只读）
"""
from __future__ import annotations

import argparse
import datetime
import importlib
import json
import sys

import numpy as np
import pandas as pd

from db import get_conn, bulk_upsert, read_sql

DIM_BREADTH = "DIM.BREADTH"
DIM_VALUATION = "DIM.VALUATION"
DIM_SENTIMENT = "DIM.SENTIMENT"
DIM_FLOW = "DIM.FLOW"
DIM_STRESS = "DIM.STRESS"
DIM_TREND = "DIM.TREND"
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


def _load_n_samples(conn, market, codes, start, end):
    """读 L1 `extra.n_sample`（阶段 1 落地），供 §2.4 规则 6 短样本降置信。

    只取本市场参与的成员，带实体过滤 + 时间下界（资源规范 R7）。
    旧行没有该键 → 返回 NaN，**不**触发降置信（缺键 ≠ 短样本）。
    """
    df = _read_sql(
        "SELECT trade_date, indicator_code, (extra ->> %s)::numeric AS n_sample "
        "FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date BETWEEN %s AND %s "
        "  AND (extra ->> %s) IS NOT NULL ORDER BY trade_date",
        conn, params=["n_sample", market, codes, start, end, "n_sample"])
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="trade_date", columns="indicator_code",
                          values="n_sample", aggfunc="last").sort_index()


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
    # §5.1 `contrib` 契约修复（2026-11-05 评估发现，**默认关闭**）：
    #   旧写法 pos=nlargest(3) / neg=nsmallest(3) 未按 delta 符号分组，成员少或
    #   delta 同号时会把非正偏离写进 pos、非负偏离写进 neg，甚至同一成员两边都出现
    #   （库内实测 92,064 个条目中 709 条越界）。估值生产者已按符号分组，宽度遗漏。
    # ⚠ 启用会改变 contrib 的 JSONB 内容 → 必须先在 dimension_dict.params 登记本键
    #   （params_hash 随之变化）并全量重算 L2，否则同一 model_version 混存两套形状。
    contrib_sign_split = bool(params.get("contrib_sign_split", False))
    # §2.4 规则 6：`n_sample < quality_sample` 降置信。默认 0 = 关闭（新维度不写该键
    # 就正常出档，v1.6 扩展隔离约定）；DIM.BREADTH 字典登记 750 后生效。
    quality_sample = float(params.get("quality_sample", 0))

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

    # v2.1.12 / 阶段 3 方案 C：dispersion 只按**参与合成的成员**算，排除 flags_only 诊断成员。
    # 理由：score 由族代表等权算出，flags_only 成员已被标定判为与核心宽度分不同构念、
    # 且不进 score；拿它们判断「平均分是否具代表性」是逻辑错配（HK 实测把未出档率
    # 从 6.27% 抬到 12.61%）。coverage 分母仍按全部 eligible（§5.1 不变）。
    # ⚠ 归一化因子同步改用 core_valid，与 tools/probe_breadth_gate.py 的 C 口径逐值一致。
    flags_only_codes = {m["code"] for m in members if m.get("flags_only")}
    core_codes = [c for c in eligible if c not in flags_only_codes] or eligible
    valid = aligned[eligible].notna().sum(axis=1)
    coverage = valid / max(len(eligible), 1)
    # `dispersion_scope` 由字典驱动：缺省 "all" 保持旧口径（新维度不会静默继承），
    # 登记 "non_flags_only" 才启用方案 C —— 与 rep_coverage_min / contrib_sign_split
    # 同款，保证 params_hash 变化时口径才变。
    use_core = str(params.get("dispersion_scope", "all")) == "non_flags_only"
    scope_codes = core_codes if use_core else eligible
    scope_valid = (aligned[scope_codes].notna().sum(axis=1) if use_core else valid)
    dispersion = (aligned[scope_codes].std(axis=1, ddof=1) if len(scope_codes) > 1
                  else pd.Series(np.nan, index=aligned.index))
    dispersion_norm = dispersion * np.sqrt(scope_valid / 10.0)

    # 短样本（§2.4 规则 6）：只取**进 score 的族代表**里最小的 n_sample。
    # ⚠ 不能用全体 eligible：EQ_VS_CAP_20D 等 flags_only 诊断成员起点晚（CN 2018 起），
    #   它首次出现时 n_sample=1 会把整个维度拖成「短样本」，即使代表们已有 28 年历史。
    #   score 的可靠性由代表决定，故分母只按 reps 计；缺键的旧行 NaN → 不判短样本。
    ns_frame = _load_n_samples(conn, market, eligible, start, end)
    n_sample_min = (ns_frame.reindex(index=aligned.index, columns=reps).min(axis=1)
                    if not ns_frame.empty else pd.Series(np.nan, index=aligned.index))
    short_sample = (pd.Series(False, index=aligned.index) if quality_sample <= 0
                    else (n_sample_min < quality_sample).fillna(False))

    band_index = np.digitize(score.fillna(-1).to_numpy(), bands)      # 等值归较高档（§8.1.1）
    state = pd.Series([labels[int(band_level)] for band_level in band_index], index=score.index)
    gate_passed = ((coverage >= coverage_min) & (dispersion_norm <= dispersion_max)
          & score.notna() & (rep_coverage >= rep_coverage_min))
    state = state.where(gate_passed, None)
    confidence = pd.Series("低", index=score.index)
    high = gate_passed & (coverage >= 0.8) & (dispersion_norm <= 20.0)
    confidence[high] = "高"
    confidence[gate_passed & ~high] = "中"
    confidence[short_sample] = "低"       # §2.4 规则 6：只降置信，不改 score / state

    def _contrib(trade_date):
        deviation = (aligned.loc[trade_date, eligible] - score.loc[trade_date]).dropna()
        def _lst(deviations):
            return [{"code": indicator_code,
                    "delta": round(float(deviation_value), 2)}
                    for indicator_code, deviation_value in deviations.items()]
        if contrib_sign_split:
            # 0.005 = 保留两位小数后仍非零的最小幅度，避免写出 delta=0.0 的「贡献」
            return {"pos": _lst(deviation[deviation > 0.005].nlargest(3)),
                    "neg": _lst(deviation[deviation < -0.005].nsmallest(3))}
        return {"pos": _lst(deviation.nlargest(3)), "neg": _lst(deviation.nsmallest(3))}

    def _flags(trade_date):
        """§2.3.3 dict 形状（2026-11-05 定稿，写入 dimension_dict.remark 并由测试钉住）：

        `structural` 恒存在（list[str]，结构性背离文案）；其余为**仅命中时写入**的
        可用性标记；`n_sample` 为当日有效成员的最小分位样本数（缺键时不写）。
        ⚠ 旧形状是字符串数组，下游解析不得再按 list 遍历本列。
        """
        flags = {"structural": _breadth_flags(aligned, trade_date, flag_gap, flag_extreme)}
        ns = n_sample_min.get(trade_date)
        if ns is not None and not pd.isna(ns):
            flags["n_sample"] = int(ns)
        if pd.isna(score.loc[trade_date]):
            flags["no_data"] = True
        if coverage.loc[trade_date] < coverage_min:
            flags["low_coverage"] = True
        dn = dispersion_norm.loc[trade_date]
        if not pd.isna(dn):
            # 落一份门禁实测值，供消费端与 --audit 精确复算出档判定，不必按 coverage 反推
            flags["dispersion_norm"] = round(float(dn), 2)
            if dn > dispersion_max:
                flags["high_dispersion"] = True
        if rep_coverage.loc[trade_date] < rep_coverage_min:
            flags["missing_representative"] = True
        if bool(short_sample.loc[trade_date]):
            flags["short_sample"] = True
        return flags

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
            "confidence": confidence.loc[trade_date],
            "model_version": params_hash, "updated_at": now,
        })
    if write and rows:
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(score.index),
            "state_rows": 0 if (market == "CN" and cn_state_disabled) else int(gate_passed.sum())}


def build_sentiment_dimension(
        conn, dimension_config: dict, params_hash: str, market: str,
        start: datetime.date, end: datetime.date, write: bool,
        dim_code: str = DIM_SENTIMENT) -> dict:
    """`DIM.SENTIMENT` 生产者：固定核心成员集 + 冻结有效起点（§5.3 方案 A）。

    与宽度 / 估值刻意分开：本维度的难点不是聚合算法，而是**成员逐个加入会造成
    跨期不可比**（2018 换手 / 2023 基金 / 2026 评级）。因此：

    · 进 `score` 的核心集 = 字典中非 `flags_only` 且 `market_scope` 覆盖本市场的成员
      （CN 三个：换手 + IPO 数 + 破发率；HK 一个：换手）。
    · 成员有效性由 `params.member_valid_from[market]` 冻结的日期判定（第 250 个有效
      分位观测日），**不读 L1 `extra.n_sample`**（情绪族未落地），PIT 安全、不触发 L1 重算。
    · `flags.core_coverage = 1.0` 才产 score；否则整行不产出（不用低覆盖分数凑历史）。
    · `coverage` 仍按全体 `members[]` 联 `market_scope` 计（§2.3.3 语义不变，仅描述量）；
      产分门禁改用 `core_coverage` 与 `dispersion_core`（核心原始 std，不做 sqrt(n/10) 归一化）。
    · `state_bands_calibrated = false` → 全时段不出档；`confidence` 按冻结的 750 达标日分档。
    · HK 核心集只有一个成员 → 单成分代理分，恒低置信、恒不出档、禁与 CN 横比。

    `members[].weight` 只是两级块平均的记录值，本函数按 `block` 两级平均，不按 weight 加权。
    """
    members = dimension_config["members"]
    params = dimension_config["params"]
    labels = list(dimension_config["state_labels"])
    bands = list(dimension_config["state_bands"][market])
    calibrated = bool(params.get("state_bands_calibrated", False))
    coverage_min = float(params.get("coverage_min", 0.6))
    dispersion_core_max = float(params.get("dispersion_core_max", 30.0))
    core_coverage_min = float(params.get("core_coverage_min", 1.0))
    flag_gap = float(params.get("flag_gap", FLAG_GAP))
    hk_proxy = str(params.get("hk_proxy_member", "FLOW.TURNOVER_RATIO"))
    low_pc1 = params.get("low_common_factor_pc1")
    tier_core = str(params.get("availability_tier_core", "core3_fixed"))
    tier_hk = str(params.get("availability_tier_hk", "hk_single_proxy"))

    def _freeze(key):
        raw = (params.get(key) or {}).get(market, {}) or {}
        return {c: datetime.date.fromisoformat(d) for c, d in raw.items() if d}

    valid_from = _freeze("member_valid_from")
    confident_from = _freeze("member_confident_from")

    all_codes = [m["code"] for m in members]
    flags_only = {m["code"] for m in members if m.get("flags_only")}
    block_of = {m["code"]: m.get("block", "诊断") for m in members}
    expected_blocks = list(dict.fromkeys(
        m.get("block", "诊断") for m in members if not m.get("flags_only")))
    scopes = _load_market_scopes(conn, all_codes)
    ok_scope = lambda c: scopes.get(c) in (market, "CN+HK", "GLOBAL")
    eligible = [c for c in all_codes if ok_scope(c)]
    core = [c for c in eligible if c not in flags_only]
    if not core:
        return {"rows": 0, "dates": 0, "state_rows": 0}

    percentiles = _load_percentiles(conn, market, eligible, start, end)
    if percentiles.empty:
        return {"rows": 0, "dates": 0, "state_rows": 0}
    aligned = percentiles.copy()
    for c in eligible:
        if c not in aligned.columns:
            aligned[c] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for c in eligible:
        if sign.get(c, 1) == -1:
            aligned[c] = 100.0 - aligned[c]                  # §2.4 规则 2：极性只翻一次

    def _ge(x, threshold):
        d = x.date() if isinstance(x, datetime.datetime) else x
        return d >= threshold

    valid_mask = pd.DataFrame(False, index=aligned.index, columns=eligible)
    for c in eligible:
        d = valid_from.get(c)
        valid_mask[c] = ([_ge(x, d) for x in aligned.index] if d
                         else [False] * len(aligned.index))
    core_valid = aligned[core].notna() & valid_mask[core]
    core_coverage = core_valid.sum(axis=1) / len(core)
    coverage = aligned[eligible].notna().sum(axis=1) / max(len(eligible), 1)
    dispersion_core = (aligned[core].where(core_valid).std(axis=1, ddof=1)
                       if len(core) > 1
                       else pd.Series(np.nan, index=aligned.index))
    dimension_completeness = round(len(eligible) / max(len(all_codes), 1), 4)

    blocks = {b: [c for c in core if block_of[c] == b]
              for b in dict.fromkeys(block_of[c] for c in core)}
    block_score, block_cov = {}, {}
    for b, codes in blocks.items():
        vals = aligned[codes].where(core_valid[codes])
        block_score[b] = vals.mean(axis=1, skipna=True)
        block_cov[b] = vals.notna().sum(axis=1) / len(codes)
    score = (pd.concat(list(block_score.values()), axis=1)
             .mean(axis=1, skipna=True).clip(0, 100)
             .where(core_coverage >= core_coverage_min))
    disp_ok = dispersion_core.isna() | (dispersion_core <= dispersion_core_max)

    is_hk_proxy = (market == "HK" and len(core) == 1 and core[0] == hk_proxy)
    conf_dates = [confident_from[c] for c in core if c in confident_from]
    min_conf = max(conf_dates) if len(conf_dates) == len(core) else None
    short = (pd.Series(True, index=aligned.index) if min_conf is None else
             pd.Series([not _ge(x, min_conf) for x in aligned.index],
                       index=aligned.index))
    gate = score.notna() & disp_ok
    confidence = pd.Series("低", index=aligned.index)
    if not is_hk_proxy:
        confidence[gate & ~short & (coverage >= 0.8) & (dispersion_core <= 20.0)] = "高"
        confidence[gate & ~short & (confidence == "低")] = "中"

    if calibrated:
        state = pd.Series(
            [labels[int(i)] for i in np.digitize(score.fillna(-1).to_numpy(), bands)],
            index=aligned.index).where(gate, None)
    else:
        state = pd.Series([None] * len(aligned.index), index=aligned.index)

    def _contrib(d):
        dev = (aligned.loc[d, eligible] - score.loc[d]).dropna()
        return {
            "pos": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev > 0.005].nlargest(3).items()],
            "neg": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev < -0.005].nsmallest(3).items()],
        }

    def _blocks(d):
        out = {}
        for b in blocks:
            s = block_score[b].loc[d]
            item = {"score": None if pd.isna(s) else round(float(s), 2),
                    "coverage": round(float(block_cov[b].loc[d]), 4)}
            if is_hk_proxy:
                item["proxy"] = True
            out[b] = item
        return out

    def _flags(d):
        live = [b for b in blocks if not pd.isna(block_score[b].loc[d])]
        f = {
            "availability_tier": tier_hk if is_hk_proxy else tier_core,
            "core_coverage": round(float(core_coverage.loc[d]), 4),
            "dimension_completeness": dimension_completeness,
            "block_coverage": round(len(live) / max(len(expected_blocks), 1), 4),
            "member_valid_from": {c: valid_from[c].isoformat()
                                  for c in core if c in valid_from},
        }
        if low_pc1 is not None:
            f["low_common_factor"] = True
        dv = dispersion_core.loc[d]
        if not pd.isna(dv):
            f["dispersion_core"] = round(float(dv), 2)
            if dv > dispersion_core_max:
                f["high_dispersion"] = True
        missing = [b for b in expected_blocks if b not in blocks]
        if missing:
            f["missing_block"] = missing
            f["block_incomplete"] = True
        if is_hk_proxy:
            f["single_block_proxy"] = True
        if coverage.loc[d] < coverage_min:
            f["low_coverage"] = True
        if bool(short.loc[d]):
            f["short_sample"] = True
        bs = list(block_score.values())
        if (len(bs) == 2 and not pd.isna(bs[0].loc[d])
                and not pd.isna(bs[1].loc[d])
                and abs(float(bs[0].loc[d]) - float(bs[1].loc[d])) > flag_gap):
            f["structural"] = ["交易行为与一级供给分位背离"]
        return f

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for d in aligned.index:
        if pd.isna(score.loc[d]):
            continue
        dv = dispersion_core.loc[d]
        rows.append({
            "dimension_code": dim_code, "market": market, "trade_date": d,
            "score": round(float(score.loc[d]), 2),
            "state": None if pd.isna(state.loc[d]) else str(state.loc[d]),
            "coverage": round(float(coverage.loc[d]), 4),
            "dispersion": None if pd.isna(dv) else round(float(dv), 2),
            "blocks": _blocks(d), "contrib": _contrib(d), "flags": _flags(d),
            "confidence": confidence.loc[d],
            "model_version": params_hash, "updated_at": now,
        })
    if write and rows:
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(aligned.index),
            "state_rows": int(state.notna().sum())}


def _purge_stale_dimension_rows(conn, dimension_code, market, params_hash) -> int:
    """全量重算时删除「本轮不再产出」的旧口径行（UPSERT 不会删行）。

    典型场景：min_periods 落地后前 249 日不再有维度行，但库里旧行仍在，
    且 model_version 是上一版 → 消费端会读到同一维度两套口径（§2.4 规则 4/5）。
    ⚠ 只在 `backfill + write` 时调用；与后续写入同事务，生产者异常会整体 rollback。
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM regime.dimension_value WHERE dimension_code=%s AND market=%s "
            "AND model_version IS DISTINCT FROM %s",
            (dimension_code, market, params_hash))
        return cur.rowcount


def build_flow_dimension(
        conn, dimension_config: dict, params_hash: str, market: str,
        start: datetime.date, end: datetime.date, write: bool,
        dim_code: str = DIM_FLOW) -> dict:
    """`DIM.FLOW` 生产者：按市场各自的**单锚**读数（§5.4，2026-11-06 定案）。

    与宽度 / 估值 / 情绪刻意分开：本维度的难点不是聚合算法，而是**原 4 成员
    里 3 个不可用**（北向断供、南向 130 天、产业资本 262 行分位退化）且
    `MARGIN_RATIO` 分母塌陷。可用数据只有两条长历史同源净额序列：

      · CN `FLOW.MARGIN_NET_20D`（Δ20 两融余额 / 同期成交额）
      · HK `FLOW.BUYBACK_NET_20D`（Σ20 公司回购 / 同期成交额）

    故取消「2 块两级等权」，每市场按 `params.proxy_member_by_market` 取唯一
    核心成员，`score` = 该成员对齐分位；其余 6 个成员为 `flags_only` 诊断。
    两侧都写 `single_block_proxy` / `flow_single_anchor`，恒不出档，
    **禁止 CN 与 HK 横比或拼接成一条曲线**（§5.3 HK 代理分同款纪律）。

    `member_valid_from` / `member_confident_from` 由 `tools/calibrate_flow.py`
    按第 250 / 750 个有效分位日冻结在字典中，PIT 安全、不读 L1 `extra.n_sample`。
    单锚无 `dispersion`；`confidence` 最高只到「中」（不伪装成完整维度分）。
    """
    members = dimension_config["members"]
    params = dimension_config["params"]
    labels = list(dimension_config["state_labels"])
    bands = list(dimension_config["state_bands"][market])
    calibrated = bool(params.get("state_bands_calibrated", False))
    coverage_min = float(params.get("coverage_min", 0.6))
    core_coverage_min = float(params.get("core_coverage_min", 1.0))
    proxy = (params.get("proxy_member_by_market") or {}).get(market)
    asof = (params.get("flow_asof_start") or {}).get(market)
    tier = str(params.get("availability_tier", "flow_single_anchor"))

    def _ge(x, threshold):
        d = x.date() if isinstance(x, datetime.datetime) else x
        return d >= threshold

    def _freeze(key):
        raw = (params.get(key) or {}).get(market, {}) or {}
        return {c: datetime.date.fromisoformat(d) for c, d in raw.items() if d}

    valid_from = _freeze("member_valid_from")
    confident_from = _freeze("member_confident_from")

    all_codes = [m["code"] for m in members]
    flags_only = {m["code"] for m in members if m.get("flags_only")}
    block_of = {m["code"]: m.get("block", "诊断") for m in members}
    scopes = _load_market_scopes(conn, all_codes)
    ok_scope = lambda c: scopes.get(c) in (market, "CN+HK", "GLOBAL")
    eligible = [c for c in all_codes if ok_scope(c)]
    core = [c for c in eligible if c not in flags_only]
    if not core or not proxy or len(core) != 1 or core[0] != proxy:
        print(f"  ⚠ L2 {market} {dim_code} 单锚配置不符"
              f"（core={core}, proxy={proxy}），跳过")
        return {"rows": 0, "dates": 0, "state_rows": 0}

    percentiles = _load_percentiles(conn, market, eligible, start, end)
    if percentiles.empty:
        return {"rows": 0, "dates": 0, "state_rows": 0}
    aligned = percentiles.copy()
    for c in eligible:
        if c not in aligned.columns:
            aligned[c] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for c in eligible:
        if sign.get(c, 1) == -1:
            aligned[c] = 100.0 - aligned[c]                  # §2.4 规则 2：极性只翻一次

    valid_mask = pd.DataFrame(False, index=aligned.index, columns=eligible)
    for c in eligible:
        d = valid_from.get(c)
        valid_mask[c] = ([_ge(x, d) for x in aligned.index] if d
                         else [False] * len(aligned.index))
    core_valid = aligned[core].notna() & valid_mask[core]
    core_coverage = core_valid.sum(axis=1) / len(core)
    coverage = aligned[eligible].notna().sum(axis=1) / max(len(eligible), 1)
    score = (aligned[proxy].where(core_valid[proxy])
             .clip(0, 100).where(core_coverage >= core_coverage_min))
    dispersion_core = pd.Series(np.nan, index=aligned.index)   # 单锚 → NULL
    dimension_completeness = round(len(eligible) / max(len(all_codes), 1), 4)

    conf_dates = [confident_from[c] for c in core if c in confident_from]
    min_conf = max(conf_dates) if len(conf_dates) == len(core) else None
    short = (pd.Series(True, index=aligned.index) if min_conf is None else
             pd.Series([not _ge(x, min_conf) for x in aligned.index],
                       index=aligned.index))
    gate = score.notna() & (dispersion_core.isna() |
                            (dispersion_core <= float(params.get("dispersion_core_max", 30))))
    confidence = pd.Series("低", index=aligned.index)
    confidence[gate & ~short] = "中"       # 单锚最高「中」，不出「高」

    if calibrated:
        state = pd.Series(
            [labels[int(i)] for i in np.digitize(score.fillna(-1).to_numpy(), bands)],
            index=aligned.index).where(gate, None)
    else:
        state = pd.Series([None] * len(aligned.index), index=aligned.index)

    shift_date = datetime.date(2016, 1, 1)
    discontinued = str(params.get("northbound_discontinued") or "2024-08-16")

    def _contrib(d):
        dev = (aligned.loc[d, eligible] - score.loc[d]).dropna()
        return {
            "pos": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev > 0.005].nlargest(3).items()],
            "neg": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev < -0.005].nsmallest(3).items()],
        }

    def _blocks(d):
        s = score.loc[d]
        return {block_of[proxy]: {
            "score": None if pd.isna(s) else round(float(s), 2),
            "coverage": round(float(core_coverage.loc[d]), 4),
            "proxy": True,
        }}

    def _flags(d):
        f = {
            "availability_tier": tier,
            "core_coverage": round(float(core_coverage.loc[d]), 4),
            "dimension_completeness": dimension_completeness,
            "block_coverage": 1.0,          # 本市场应有 1 块（按市场计，不按全体 2 块）
            "single_block_proxy": True,
            "member_valid_from": {c: valid_from[c].isoformat()
                                  for c in core if c in valid_from},
        }
        if asof:
            f["flow_asof_start"] = asof
        if market == "HK" and _ge(d, shift_date):
            f["regime_shift_2016"] = True
        if market == "CN":
            f["northbound_discontinued"] = discontinued
            if params.get("margin_ratio_source_discontinued"):
                f["margin_ratio_source_discontinued"] = True
        if coverage.loc[d] < coverage_min:
            f["low_coverage"] = True
        if bool(short.loc[d]):
            f["short_sample"] = True
        return f

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for d in aligned.index:
        if pd.isna(score.loc[d]):
            continue
        rows.append({
            "dimension_code": dim_code, "market": market, "trade_date": d,
            "score": round(float(score.loc[d]), 2),
            "state": None if pd.isna(state.loc[d]) else str(state.loc[d]),
            "coverage": round(float(coverage.loc[d]), 4),
            "dispersion": None,
            "blocks": _blocks(d), "contrib": _contrib(d), "flags": _flags(d),
            "confidence": confidence.loc[d],
            "model_version": params_hash, "updated_at": now,
        })
    if write and rows:
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(aligned.index),
            "state_rows": int(state.notna().sum())}


def build_stress_dimension(
        conn, dimension_config: dict, params_hash: str, market: str,
        start: datetime.date, end: datetime.date, write: bool,
        dim_code: str = DIM_STRESS) -> dict:
    """`DIM.STRESS` 生产者：固定核心成员集 + 4 块两级等权 + 冻结有效起点（§5.5 v1.17）。

    与宽度 / 估值 / 情绪 / 资金刻意分开：原 21 成员 5 块模型不可落地——
    AA 系 4 项仅 19~27 行分位、块内相关中位 0.18（相关性块 −0.18）、
    水平型 CREDIT_SPREAD / HIBOR / VIX 的 expanding 分位随年代漂移，
    块均值把危机信号压成 ≈50（危机日 > 全期 P90 占比仅 36%/39%）。

    本实现只取字典 `members[]` 中非 `flags_only` 且 `market_scope` 覆盖本市场的
    核心成员（CN 8 / HK 7），按 `block` 两级等权（块内 mean → 块间 mean），
    整块为空时由 `mean(skipna=True)` 自动从块间分母剔除、不记 0。
    `FLOW.HIBOR_3M` 是 HK 的 `flags_only` 诊断成员，只写 coverage / contrib / flags。

    · `sign`：只在此翻转一次（§2.4 规则 2）；`RISK.MAXDD_250` 与 `RISK.TERM_SPREAD`
      取 −1（负回撤高分位＝浅回撤；倒挂低＝危险）。
    · `member_valid_from` / `member_confident_from` 由 `tools/calibrate_stress.py`
      按第 250 / 750 个有效分位日冻结在字典中，PIT 安全、不读 L1 `extra.n_sample`。
    · `core_coverage_min = 1.0`：任一核心成员缺失或早于冻结日 → **整行不产出**。
    · `dispersion` 只写描述量、**不作门禁**（块内成员低相关是设计事实，
      用 §2.4 的 `dispersion_norm` 判门禁会几乎每天挡掉）。
    · `state_bands_calibrated = false` → 全时段 `state = NULL`；`confidence` 最高「中」。
    · `flags.resonance_cnt` = 对齐分位 >= `resonance_floor` 的核心成员数，
      承载「几处同时失稳」的共振信息（业务层 `resonance_cnt` 的维度侧输入）。

    `members[].weight` 只是记录值，本函数按块两级等权，不按 weight 加权。
    """
    members = dimension_config["members"]
    params = dimension_config["params"]
    labels = list(dimension_config["state_labels"])
    bands = list(dimension_config["state_bands"][market])
    calibrated = bool(params.get("state_bands_calibrated", False))
    coverage_min = float(params.get("coverage_min", 0.6))
    core_coverage_min = float(params.get("core_coverage_min", 1.0))
    resonance_floor = float(params.get("resonance_floor", 80.0))
    tier = str(params.get("availability_tier", "stress_core_fixed"))
    excluded_count = len(params.get("excluded_members") or [])

    def _ge(x, threshold):
        d = x.date() if isinstance(x, datetime.datetime) else x
        return d >= threshold

    def _freeze(key):
        raw = (params.get(key) or {}).get(market, {}) or {}
        return {c: datetime.date.fromisoformat(d) for c, d in raw.items() if d}

    valid_from = _freeze("member_valid_from")
    confident_from = _freeze("member_confident_from")

    all_codes = [m["code"] for m in members]
    flags_only = {m["code"] for m in members if m.get("flags_only")}
    block_of = {m["code"]: m.get("block", "诊断") for m in members}
    scopes = _load_market_scopes(conn, all_codes)
    ok_scope = lambda c: scopes.get(c) in (market, "CN+HK", "GLOBAL")
    eligible = [c for c in all_codes if ok_scope(c)]
    core = [c for c in eligible if c not in flags_only]
    if not core:
        return {"rows": 0, "dates": 0, "state_rows": 0}

    percentiles = _load_percentiles(conn, market, eligible, start, end)
    if percentiles.empty:
        return {"rows": 0, "dates": 0, "state_rows": 0}
    aligned = percentiles.copy()
    for c in eligible:
        if c not in aligned.columns:
            aligned[c] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for c in eligible:
        if sign.get(c, 1) == -1:
            aligned[c] = 100.0 - aligned[c]                  # §2.4 规则 2：极性只翻一次

    valid_mask = pd.DataFrame(False, index=aligned.index, columns=eligible)
    for c in eligible:
        d = valid_from.get(c)
        valid_mask[c] = ([_ge(x, d) for x in aligned.index] if d
                         else [False] * len(aligned.index))
    core_valid = aligned[core].notna() & valid_mask[core]
    core_coverage = core_valid.sum(axis=1) / len(core)
    coverage = aligned[eligible].notna().sum(axis=1) / max(len(eligible), 1)
    dispersion = (aligned[core].where(core_valid).std(axis=1, ddof=1)
                  if len(core) > 1
                  else pd.Series(np.nan, index=aligned.index))
    resonance = (aligned[core].where(core_valid) >= resonance_floor).sum(axis=1)
    dimension_completeness = round(len(eligible) / max(len(all_codes), 1), 4)

    blocks = {b: [c for c in core if block_of[c] == b]
              for b in dict.fromkeys(block_of[c] for c in core)}
    block_score, block_cov = {}, {}
    for b, codes in blocks.items():
        vals = aligned[codes].where(core_valid[codes])
        block_score[b] = vals.mean(axis=1, skipna=True)
        block_cov[b] = vals.notna().sum(axis=1) / len(codes)
    # 块间等权；空块由 skipna 从分母剔除（empty_block_policy=drop_from_denominator）
    score = (pd.concat(list(block_score.values()), axis=1)
             .mean(axis=1, skipna=True).clip(0, 100)
             .where(core_coverage >= core_coverage_min))

    conf_dates = [confident_from[c] for c in core if c in confident_from]
    min_conf = max(conf_dates) if len(conf_dates) == len(core) else None
    short = (pd.Series(True, index=aligned.index) if min_conf is None else
             pd.Series([not _ge(x, min_conf) for x in aligned.index],
                       index=aligned.index))
    gate = score.notna()
    confidence = pd.Series("低", index=aligned.index)
    confidence[gate & ~short] = "中"       # 首版内部读数不出「高」

    if calibrated:
        state = pd.Series(
            [labels[int(i)] for i in np.digitize(score.fillna(-1).to_numpy(), bands)],
            index=aligned.index).where(gate, None)
    else:
        state = pd.Series([None] * len(aligned.index), index=aligned.index)

    frozen_from = (max(valid_from[c] for c in core).isoformat()
                   if all(c in valid_from for c in core) else None)

    def _contrib(d):
        dev = (aligned.loc[d, eligible] - score.loc[d]).dropna()
        return {
            "pos": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev > 0.005].nlargest(3).items()],
            "neg": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev < -0.005].nsmallest(3).items()],
        }

    def _blocks(d):
        out = {}
        for b, codes in blocks.items():
            s = block_score[b].loc[d]
            out[b] = {
                "score": None if pd.isna(s) else round(float(s), 2),
                "coverage": round(float(block_cov[b].loc[d]), 4),
                "n": int(block_cov[b].loc[d] * len(codes)),
            }
        return out

    def _flags(d):
        live = [b for b in blocks if not pd.isna(block_score[b].loc[d])]
        f = {
            "availability_tier": tier,
            "core_coverage": round(float(core_coverage.loc[d]), 4),
            "dimension_completeness": dimension_completeness,
            "block_coverage": round(len(live) / max(len(blocks), 1), 4),
            "resonance_cnt": int(resonance.loc[d]),
            "resonance_floor": resonance_floor,
            "dispersion_desc_only": True,
            "excluded_members_count": excluded_count,
            "member_valid_from": {c: valid_from[c].isoformat()
                                  for c in core if c in valid_from},
        }
        if frozen_from:
            f["frozen_from"] = frozen_from
        empty = [b for b in blocks if b not in live]
        if empty:
            f["empty_block"] = empty
        if coverage.loc[d] < coverage_min:
            f["low_coverage"] = True
        if bool(short.loc[d]):
            f["short_sample"] = True
        return f

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for d in aligned.index:
        if pd.isna(score.loc[d]):
            continue
        dv = dispersion.loc[d]
        rows.append({
            "dimension_code": dim_code, "market": market, "trade_date": d,
            "score": round(float(score.loc[d]), 2),
            "state": None if pd.isna(state.loc[d]) else str(state.loc[d]),
            "coverage": round(float(coverage.loc[d]), 4),
            "dispersion": None if pd.isna(dv) else round(float(dv), 2),
            "blocks": _blocks(d), "contrib": _contrib(d), "flags": _flags(d),
            "confidence": confidence.loc[d],
            "model_version": params_hash, "updated_at": now,
        })
    if write and rows:
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(aligned.index),
            "state_rows": int(state.notna().sum())}


def _active_dimensions(conn):
    """所有启用维度 → [dimension_code]；新维度写入字典即自动接入日频计算。"""
    df = _read_sql("SELECT dimension_code FROM regime.dimension_dict "
                   "WHERE is_active=TRUE ORDER BY dimension_code", conn)
    return list(df["dimension_code"]) if not df.empty else []


def build_trend_dimension(
        conn, dimension_config: dict, params_hash: str, market: str,
        start: datetime.date, end: datetime.date, write: bool,
        dim_code: str = DIM_TREND) -> dict:
    """`DIM.TREND` 生产者：σ 归一化趋势强度**单锚**（§5.6 v1.19）。

    与其余五维刻意分开，但**共用 §2.4 通用管线**（members[] 读 `percentile`）：
      · `score` = `TREND.STRENGTH_T` 的 expanding 分位。原设计契约用固定 K=3 把
        t 统计量线性压到 0–100 且 `NO_PERCENTILE` 只读 raw_value；实测 CN
        1991/1992/2007/2015 有 100%/75%/100%/41% 的交易日顶格、HK 一半日子
        <20 分 → 绝对刻度跨期不可比。改相对刻度后零标定参数，`raw_value`
        仍保留「几个标准差」的语义。
      · `TREND.TSMOM_12M` / `TREND.MA200_DEV` 是 `weight=0` 的方向腿与校验项，
        只写 `contrib` / `blocks` / `flags`，不进 `score`。L3 方向判定直接读
        这两个原子的 `raw_value`，本维度 `score` 不参与方向判定。
      · `state_bands_calibrated=false` → `state` 恒 NULL（三态方向归
        `BIZ.MARKET_REGIME`）；单锚 `dispersion` 恒 NULL。
      · `member_valid_from` / `member_confident_from` 由 `tools/calibrate_trend.py`
        按第 250 / 750 个有效分位日冻结在字典中，PIT 安全、不读 `extra.n_sample`。

    ⚠ 不读 `state_bands` / `state_labels`（本维恒不出档），避免字典值为 null 时
      `list(...)` 崩溃；`blocks` 按 §2.3.3 标准形状 `{块名:{score,coverage}}` 落库。
    """
    members = dimension_config["members"]
    params = dimension_config["params"]
    coverage_min = float(params.get("coverage_min", 0.6))
    anchor = str(params.get("anchor_code") or "")
    tier = str(params.get("availability_tier", "trend_single_anchor"))
    asof = (params.get("trend_asof_start") or {}).get(market)

    def _ge(x, threshold):
        d = x.date() if isinstance(x, datetime.datetime) else x
        return d >= threshold

    def _freeze(key):
        raw = (params.get(key) or {}).get(market, {}) or {}
        return {c: datetime.date.fromisoformat(d) for c, d in raw.items() if d}

    valid_from = _freeze("member_valid_from")
    confident_from = _freeze("member_confident_from")

    all_codes = [m["code"] for m in members]
    block_of = {m["code"]: m.get("block", "诊断") for m in members}
    scopes = _load_market_scopes(conn, all_codes)
    ok_scope = lambda c: scopes.get(c) in (market, "CN+HK", "GLOBAL")
    eligible = [c for c in all_codes if ok_scope(c)]
    if not anchor or anchor not in eligible:
        print(f"  ⚠ L2 {market} {dim_code} 单锚配置不符"
              f"（anchor={anchor}, eligible={eligible}），跳过")
        return {"rows": 0, "dates": 0, "state_rows": 0}

    percentiles = _load_percentiles(conn, market, eligible, start, end)
    if percentiles.empty:
        return {"rows": 0, "dates": 0, "state_rows": 0}
    aligned = percentiles.copy()
    for c in eligible:
        if c not in aligned.columns:
            aligned[c] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for c in eligible:
        if sign.get(c, 1) == -1:
            aligned[c] = 100.0 - aligned[c]                  # §2.4 规则 2：极性只翻一次

    # 锚门禁：冻结第 250 个有效分位日之前不产分；护栏前的失真样本已被 L1 挡住。
    d_valid = valid_from.get(anchor)
    anchor_ready = (pd.Series([_ge(x, d_valid) for x in aligned.index],
                              index=aligned.index) if d_valid
                    else pd.Series(False, index=aligned.index))
    score = aligned[anchor].where(anchor_ready).clip(0, 100)
    coverage = aligned[eligible].notna().sum(axis=1) / max(len(eligible), 1)
    dispersion = pd.Series(np.nan, index=aligned.index)     # 单锚 → NULL

    conf_dates = [confident_from[c] for c in eligible if c in confident_from]
    min_conf = max(conf_dates) if len(conf_dates) == len(eligible) else None
    short = (pd.Series(True, index=aligned.index) if min_conf is None else
             pd.Series([not _ge(x, min_conf) for x in aligned.index],
                       index=aligned.index))
    gate = score.notna() & (coverage >= coverage_min)
    confidence = pd.Series("低", index=aligned.index)
    confidence[gate & ~short] = "中"                          # 单锚最高「中」，不出「高」
    # 方向状态归 L3，本维规格上永不出档（§5.6）；state_bands_calibrated 保留作将来开关。
    state = pd.Series([None] * len(aligned.index), index=aligned.index)

    checks = [c for c in eligible if c != anchor]

    def _contrib(d):
        dev = (aligned.loc[d, checks] - score.loc[d]).dropna()
        return {
            "pos": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev > 0.005].nlargest(3).items()],
            "neg": [{"code": c, "delta": round(float(v), 2)}
                    for c, v in dev[dev < -0.005].nsmallest(3).items()],
        }

    def _blocks(d):
        """§2.3.3 标准形状：每块写 {score, coverage}，不写原始 t / raw_value。"""
        grouped: dict[str, dict] = {}
        for c in eligible:
            b = grouped.setdefault(block_of[c], {"vals": [], "n": 0})
            b["n"] += 1
            if not pd.isna(aligned.loc[d, c]):
                b["vals"].append(float(aligned.loc[d, c]))
        return {b: {
            "score": round(sum(v["vals"]) / len(v["vals"]), 2) if v["vals"] else None,
            "coverage": round(len(v["vals"]) / v["n"], 4),
        } for b, v in grouped.items()}

    def _flags(d):
        f = {
            "availability_tier": tier,
            "anchor_code": anchor,
            "anchor_window": "expanding",
            "sigma_win": int(params.get("sigma_win", 252)),
            "tsmom_variant": str(params.get("tsmom_variant", "price")),
            "no_state": True,
            "member_valid_from": {c: valid_from[c].isoformat()
                                  for c in eligible if c in valid_from},
        }
        if asof:
            f["trend_asof_start"] = asof
        if coverage.loc[d] < coverage_min:
            f["low_coverage"] = True
        if bool(short.loc[d]):
            f["short_sample"] = True
        if pd.isna(aligned.loc[d, anchor]):
            f["no_data"] = True
        return f

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for d in aligned.index:
        # 锚缺失即 no_data：按缺行处理（与估值 / 资金 / 压力同款），不写 score=NULL 行，
        # 也不得填 0 或 50。L1 护栏与冻结起点已保证下游按缺行处理。
        if pd.isna(score.loc[d]):
            continue
        rows.append({
            "dimension_code": dim_code, "market": market, "trade_date": d,
            "score": round(float(score.loc[d]), 2),
            "state": None,
            "coverage": round(float(coverage.loc[d]), 4),
            "dispersion": None,
            "blocks": _blocks(d), "contrib": _contrib(d), "flags": _flags(d),
            "confidence": confidence.loc[d],
            "model_version": params_hash, "updated_at": now,
        })
    if write and rows:
        bulk_upsert(conn, DIMENSION_VALUE_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(aligned.index),
            "state_rows": int(state.notna().sum())}


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
            if backfill and write:
                n_purged = _purge_stale_dimension_rows(
                    conn, dimension_code, market_code, dimension_params_hash)
                if n_purged:
                    print(f"  ⚠ L2 {market_code} {dimension_code} 清理旧口径残留 {n_purged} 行")
            if dimension_code == DIM_BREADTH:
                dimension_stats = build_breadth_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            elif dimension_code == DIM_VALUATION:
                dimension_stats = build_valuation_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            elif dimension_code == DIM_SENTIMENT:
                dimension_stats = build_sentiment_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            elif dimension_code == DIM_FLOW:
                dimension_stats = build_flow_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            elif dimension_code == DIM_STRESS:
                dimension_stats = build_stress_dimension(
                    conn, dimension_config, dimension_params_hash, market_code,
                    window_start, end, write, dim_code=dimension_code)
            elif dimension_code == DIM_TREND:
                dimension_stats = build_trend_dimension(
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


# ── §5.1 契约自检（只读，不写库、不改口径）────────────────────────────────────
def audit_breadth_contract(conn, markets=("CN", "HK"), end=None) -> dict:
    """按 `doc/market_state_data_architecture.md` §5.1 核验「DIM.BREADTH 能否算对」。

    只读三件事，不改变任何数据 / 口径：
      ① L1：11 个宽度原子是否登记、采集、分位化，版本与新鲜度是否对齐；
      ② L2：字典成员 / sign / state_bands / params / 代表 / 门禁是否自洽；
      ③ 落库：dimension_value 版本是否一致、最新一日四道出档门禁为何通过或未通过。

    L1 侧唯一真相源（成员集合 / 制度起点 / 分位窗口）用 `importlib` **惰性动态导入**
    `market_state_daily`（与 `collector_runtime.run_module` 同款），避免与该文件 run() 里
    的反向 import 形成静态导入环；运行时两处都是函数内导入，不成环。
    所有查询带实体过滤或时间下界（AGENTS.md 计算任务资源约定）。
    """
    msd = importlib.import_module("market_state_daily")

    end = end or datetime.date.today()
    findings: list[dict] = []

    def add(market, code, level, message, **evidence):
        findings.append({"market": market, "code": code, "level": level,
                         "ok": level == "PASS", "message": message,
                         "evidence": evidence})

    try:
        dim, params_hash = load_dimension_dict(conn, DIM_BREADTH)
    except RuntimeError as exc:
        add("ALL", "L2.DICT", "FAIL", f"dimension_dict 不可用：{exc}")
        return {"findings": findings, "summary": {"PASS": 0, "WARN": 0, "FAIL": 1}}

    members = dim["members"]
    codes = [m["code"] for m in members]
    params = dim["params"]
    expected = set(msd.BREADTH_ROLLING_CODES)
    add("ALL", "L2.MEMBERS", "PASS" if set(codes) == expected else "FAIL",
        f"members {len(codes)} 个 / L1 宽度集合 {len(expected)} 个",
        missing=sorted(expected - set(codes)), extra=sorted(set(codes) - expected))

    idf = _read_sql(
        "SELECT indicator_code, market_scope, direction, params_hash, is_active "
        "FROM regime.indicator_dict WHERE indicator_code = ANY(%s)",
        conn, params=[codes])
    idict = {r["indicator_code"]: r for _, r in idf.iterrows()}

    def in_scope(indicator_code, market):
        r = idict.get(indicator_code)
        return r is not None and r["market_scope"] in (market, "CN+HK", "GLOBAL")

    absent = [c for c in codes if c not in idict or not bool(idict[c]["is_active"])]
    add("ALL", "L1.DICT", "PASS" if not absent else "FAIL",
        f"indicator_dict 登记 {len(idict)}/{len(codes)} 个宽度成员", inactive=absent)

    bad_sign = [
        {"code": m["code"], "sign": m.get("sign"), "direction": idict[m["code"]]["direction"]}
        for m in members if m["code"] in idict
        and int(m.get("sign", 1)) != (-1 if idict[m["code"]]["direction"] == "high_risk" else 1)
    ]
    add("ALL", "L2.SIGN", "PASS" if not bad_sign else "FAIL",
        "members[].sign 与 indicator_dict.direction 一致（§5.1 输入段）", mismatch=bad_sign)

    bands_by_market = dim["state_bands"]
    for market in markets:
        b = bands_by_market.get(market) if isinstance(bands_by_market, dict) else None
        ok = bool(b) and len(b) == 4 and all(float(b[i]) < float(b[i + 1]) for i in range(3))
        add(market, "L2.BANDS", "PASS" if ok else "FAIL",
            f"state_bands[{market}] 按市场冻结、4 个升序切点", bands=b)
    add("ALL", "L2.LABELS", "PASS" if len(dim["state_labels"]) == 5 else "FAIL",
        "state_labels 五档", labels=dim["state_labels"])

    required = ("coverage_min", "dispersion_max", "weight_cap", "rep_coverage_min", "pct_window")
    missing_params = [k for k in required if k not in params]
    add("ALL", "L2.PARAMS", "PASS" if not missing_params else "FAIL",
        "门禁与聚合参数齐备", missing=missing_params)
    add("ALL", "L1.PCT_WINDOW",
        "PASS" if int(params.get("pct_window", -1)) == msd.BREADTH_PCT_WINDOW else "FAIL",
        f"分位窗口与 L1 一致（{msd.BREADTH_PCT_WINDOW} 有效观测）",
        dict_value=params.get("pct_window"))
    unregistered = [k for k in ("flag_gap", "flag_extreme") if k not in params]
    add("ALL", "L2.PARAMS_HASH", "PASS" if not unregistered else "WARN",
        "flags 判据阈值已登记字典并纳入 params_hash" if not unregistered
        else "flags 判据阈值未登记字典，改阈值不触发重算（§2.2）",
        using_module_constants=unregistered)
    short_sample = "quality_sample" in params and "min_periods" in params
    add("ALL", "L2.SHORT_SAMPLE", "PASS" if short_sample else "WARN",
        "宽度已接入 n_sample < 750 短样本降置信（§2.4 规则 6）" if short_sample
        else "宽度尚未接入 n_sample < 750 短样本降置信（§2.4 规则 6）",
        dictionary_keys=[k for k in ("quality_sample", "min_periods") if k in params])
    add("ALL", "L1.PIT", "PASS" if msd.PIT_UNIVERSE_READY else "WARN",
        "PIT 成分池未重建：宽度分位带 survivorship_bias / quality=low，禁止跨年代水位比较",
        pit_universe_ready=msd.PIT_UNIVERSE_READY)
    scope = str(params.get("dispersion_scope", "all"))
    c_on = scope == "non_flags_only"
    add("ALL", "L2.DISPERSION_NORM", "PASS" if c_on else "WARN",
        "dispersion 已按方案 C 排除 flags_only 诊断成员" if c_on
        else "dispersion 仍按全体成员计算（阶段 3 方案 C 未启用）",
        dispersion_scope=scope,
        formula="dispersion(core) * sqrt(core_valid/10)" if c_on
                else "dispersion(all) * sqrt(valid/10)")

    for market in markets:
        eligible = [c for c in codes if in_scope(c, market)]
        reps = [m["code"] for m in members
                if m.get("representative") and not m.get("flags_only")
                and in_scope(m["code"], market)]
        flags_only = [m["code"] for m in members if m.get("flags_only")]
        add(market, "L2.REPS", "PASS" if len(reps) == (5 if market == "CN" else 4) else "FAIL",
            f"有效族代表 {len(reps)} 个（HK 自动少 LIMIT_NET 族）", reps=reps,
            expected_members=len(eligible), flags_only=flags_only)

        w = min(1.0 / len(reps), float(params.get("weight_cap", 0.25))) if reps else 0.0
        cap_active = bool(reps) and w < 1.0 / len(reps) - 1e-12
        add(market, "L2.WEIGHT_CAP", "PASS" if cap_active else "WARN",
            "weight_cap 对当前代表数不改变权重（§5.1 称「有名无实的护栏」）"
            if not cap_active else "weight_cap 生效", per_rep_weight=round(w, 4))

        # L1 原子：采集 / 分位化 / 版本
        cov = _read_sql(
            "SELECT indicator_code, count(*) AS n, min(trade_date) AS d0, max(trade_date) AS d1, "
            " count(raw_value) AS n_raw, count(percentile) AS n_pct, "
            " min(model_version) AS v0, max(model_version) AS v1, "
            " count(*) FILTER (WHERE model_version IS NULL) AS n_nullver, "
            " count(*) FILTER (WHERE percentile IS NOT NULL "
            "   AND (extra ->> %s)::numeric < %s) AS n_short_leak, "
            " count(*) FILTER (WHERE percentile IS NULL "
            "   AND (extra ->> %s)::numeric >= %s) AS n_long_gap "
            "FROM regime.indicator_value WHERE market=%s AND indicator_code=ANY(%s) "
            " AND trade_date >= %s GROUP BY 1",
            # ⚠ 占位符按 SQL 文本出现顺序替换：SELECT 里的 4 个在 WHERE 的 3 个之前。
            conn, params=["n_sample", msd.BREADTH_PCT_MIN_PERIODS,
                          "n_sample", msd.BREADTH_PCT_MIN_PERIODS,
                          market, codes, DATA_START[market]])
        by_code = {r["indicator_code"]: r for _, r in cov.iterrows()}
        detail, problems = [], []
        for c in eligible:
            r = by_code.get(c)
            if r is None:
                problems.append(f"{c}: 无 indicator_value 行")
                continue
            ver_ok = (r["v0"] == r["v1"] == idict[c]["params_hash"] and int(r["n_nullver"]) == 0)
            # min_periods 落地后，短样本期 percentile 为 NULL 是**预期**，不是缺口。
            # 真正的不一致：短样本期仍给了分位，或样本够了却没给分位。
            pct_ok = (int(r["n_short_leak"]) == 0 and int(r["n_long_gap"]) == 0)
            detail.append(f"{c}={int(r['n'])}行/{str(r['d0'])[:10]}~{str(r['d1'])[:10]}"
                          f"/分位{int(r['n_pct'])}/NULL{int(r['n']) - int(r['n_pct'])}")
            if not (ver_ok and pct_ok):
                problems.append(
                    f"{c}: min_periods一致={pct_ok}(短样本泄漏{int(r['n_short_leak'])}"
                    f"/长样本缺口{int(r['n_long_gap'])}) 版本一致={ver_ok}")
        add(market, "L1.ATOM", "PASS" if not problems else "FAIL",
            f"{len(eligible) - len(problems)}/{len(eligible)} 个宽度原子已采集并按 min_periods 分位化",
            problems=problems, detail=detail)

        latest_iv = max((r["d1"] for r in by_code.values() if r["d1"] is not None), default=None)
        quote_table = "a_daily_quote" if market == "CN" else "hk_daily_quote"
        latest_quote = _read_sql(
            f"SELECT max(trade_date) AS d FROM {quote_table} WHERE trade_date >= %s",
            conn, params=[end - datetime.timedelta(days=30)]).iloc[0]["d"]
        iv_s = str(latest_iv)[:10] if latest_iv is not None else None
        qt_s = str(latest_quote)[:10] if latest_quote is not None else None
        add(market, "L1.FRESHNESS", "PASS" if iv_s == qt_s else "WARN",
            f"L1 最新 {iv_s} / 行情最新 {qt_s}", l1=iv_s, quote=qt_s)

        # L2 落库：版本 + 最新一日门禁复算
        dv = _read_sql(
            "SELECT count(*) AS n, min(trade_date) AS d0, max(trade_date) AS d1, "
            " count(state) AS n_state, "
            " count(*) FILTER (WHERE model_version IS DISTINCT FROM %s) AS n_ver_bad "
            "FROM regime.dimension_value WHERE dimension_code=%s AND market=%s",
            conn, params=[params_hash, DIM_BREADTH, market]).iloc[0]
        add(market, "L2.VERSION", "PASS" if int(dv["n_ver_bad"]) == 0 else "FAIL",
            f"dimension_value {int(dv['n'])} 行 / 出档 {int(dv['n_state'])} 日，"
            f"model_version 全部=={params_hash}", mismatched=int(dv["n_ver_bad"]))

        last = _read_sql(
            "SELECT trade_date, score, state, coverage, dispersion, confidence, flags, blocks, contrib "
            "FROM regime.dimension_value WHERE dimension_code=%s AND market=%s AND trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT 1",
            conn, params=[DIM_BREADTH, market, end])
        if last.empty:
            add(market, "L2.LATEST_GATE", "FAIL", "dimension_value 无行")
            continue
        lr = last.iloc[0]
        flags = _parse_jsonb(lr["flags"]) or {}
        # 方案 C 后归一化因子改用 core_valid，无法从 coverage 反推；生产者已把实测值
        # 写进 flags.dispersion_norm，自检直接读它，保证门禁复算逐值精确。
        disp_norm = flags.get("dispersion_norm")
        if disp_norm is not None:
            disp_norm = float(disp_norm)
        block_map = _parse_jsonb(lr["blocks"]) or {}
        reps_ok = sum(1 for v in block_map.values()
                      if isinstance(v, dict) and v.get("score") is not None)
        rep_cov = reps_ok / len(reps) if reps else 0.0
        gates = {
            "score_not_null": lr["score"] is not None and not pd.isna(lr["score"]),
            "coverage": float(lr["coverage"]) >= float(params["coverage_min"]),
            "dispersion_norm": disp_norm is not None and disp_norm <= float(params["dispersion_max"]),
            "rep_coverage": rep_cov >= float(params.get("rep_coverage_min", 0.0)),
        }
        consistent = (lr["state"] is not None) == all(gates.values())
        add(market, "L2.LATEST_GATE", "PASS" if consistent else "FAIL",
            f"{str(lr['trade_date'])[:10]} score={lr['score']} state={lr['state']} "
            f"confidence={lr['confidence']}", gates=gates,
            dispersion_norm=None if disp_norm is None else round(disp_norm, 2),
            rep_coverage=round(rep_cov, 4),
            failed=[k for k, v in gates.items() if not v])
        shape_ok = isinstance(flags, dict) and "structural" in flags
        add(market, "L2.FLAGS_SHAPE", "PASS" if shape_ok else "FAIL",
            "flags 为 §2.3.3 的 dict 形状且含 structural 键（下游不得按 list 遍历）",
            flags_type=type(flags).__name__)
        if lr["state"] is None:
            avail = {"low_coverage", "high_dispersion", "short_sample",
                     "no_data", "missing_representative"}
            has_avail = isinstance(flags, dict) and bool(set(flags) & avail)
            add(market, "L2.FLAGS_AVAILABILITY", "PASS" if has_avail else "WARN",
                "未出档行须在 flags 写出被哪道门禁卡住", flags=flags)

        # contrib 符号越界统计（§2.3.3：正负各 top3）
        cdf = _read_sql(
            "SELECT trade_date, contrib FROM regime.dimension_value "
            "WHERE dimension_code=%s AND market=%s AND trade_date >= %s",
            conn, params=[DIM_BREADTH, market, DATA_START[market]])
        total = bad = 0
        for _, r in cdf.iterrows():
            ct = _parse_jsonb(r["contrib"])
            if not isinstance(ct, dict):
                continue
            for side, wrong in (("pos", lambda d: d <= 0), ("neg", lambda d: d >= 0)):
                for item in ct.get(side, []) or []:
                    total += 1
                    if wrong(float(item.get("delta", 0))):
                        bad += 1
        add(market, "L2.CONTRIB_SIGN", "PASS" if bad == 0 else "WARN",
            f"contrib 条目 {total} 个，符号越界 {bad} 个"
            + ("；字典登记 contrib_sign_split=true 并重算 L2 后修复" if bad else ""),
            contrib_sign_split=bool(params.get("contrib_sign_split", False)))

    n_old = _read_sql(
        "SELECT count(*) AS n FROM regime.indicator_value "
        "WHERE indicator_code=%s AND market=%s AND trade_date < %s "
        " AND (raw_value IS NOT NULL OR percentile IS NOT NULL)",
        conn, params=["BREADTH.LIMIT_NET", "CN", msd.LIMIT_EFFECTIVE_FROM]).iloc[0]["n"]
    add("CN", "L1.LIMIT_GUARD", "PASS" if int(n_old) == 0 else "FAIL",
        f"LIMIT_NET 制度起点 {msd.LIMIT_EFFECTIVE_FROM} 前残留非空 {int(n_old)} 行"
        + ("；护栏前值会污染 1996-12-16 起 2520 观测滚动分位" if int(n_old) else ""),
        residual_rows=int(n_old))

    lower = min(DATA_START.values())
    fin = _read_sql(
        "SELECT count(*) FILTER (WHERE extra ->> %s IS NOT NULL) AS n_sample "
        "FROM regime.indicator_value WHERE indicator_code=ANY(%s) AND trade_date >= %s",
        conn, params=["n_sample", codes, lower]).iloc[0]
    add("ALL", "L1.N_SAMPLE", "PASS" if int(fin["n_sample"]) > 0 else "WARN",
        f"indicator_value.extra.n_sample 有值 {int(fin['n_sample'])} 行"
        + ("；宽度为 0 → 短样本降置信缺数据源，无法在 L2 接入"
           if int(fin["n_sample"]) == 0 else ""),
        rows_with_n_sample=int(fin["n_sample"]))

    summary = {k: sum(1 for f in findings if f["level"] == k)
               for k in ("PASS", "WARN", "FAIL")}
    return {"findings": findings, "summary": summary,
            "params_hash": params_hash, "dimension_dict": dim}


def format_breadth_audit(report: dict) -> str:
    """把自检结果排成可读报告（不改数值，仅展示）。"""
    s = report["summary"]
    lines = [f"§5.1 DIM.BREADTH 契约自检：PASS {s['PASS']} / WARN {s['WARN']} / FAIL {s['FAIL']}"
             f"（只读，未写库）"]
    current = None
    for f in report["findings"]:
        if f["market"] != current:
            current = f["market"]
            lines.append(f"\n【{current}】")
        lines.append(f"  [{f['level']:<4}] {f['code']:<24} {f['message']}")
        for key, value in f["evidence"].items():
            if key == "detail" and isinstance(value, list):
                lines.append("         detail:")
                lines.extend(f"           - {x}" for x in value)
                continue
            text = str(value)
            if len(text) > 220:
                text = text[:217] + "..."
            lines.append(f"         {key}: {text}")
    return "\n".join(lines)


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
    ap.add_argument("--audit", action="store_true",
                    help="§5.1 宽度契约自检（只读，不写库）")
    ap.add_argument("--market", choices=["CN", "HK"])
    ap.add_argument("--dim", action="append", dest="dimension_codes",
                    help="只算指定维度（可重复，如 --dim DIM.VALUATION）；默认全部启用维度")
    ap.add_argument("--start", type=datetime.date.fromisoformat)
    ap.add_argument("--end", type=datetime.date.fromisoformat)
    args = ap.parse_args()
    markets = [args.market] if args.market else ["CN", "HK"]
    end = args.end or datetime.date.today()
    with get_conn() as conn:
        if args.audit:
            print(format_breadth_audit(
                audit_breadth_contract(conn, markets=markets, end=end)))
            return
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
