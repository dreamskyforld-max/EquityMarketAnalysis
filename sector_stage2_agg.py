#!/usr/bin/env python3
"""第④层「行业配置」阶段 2：自下而上聚合（依赖 regime.sector_constituent）。

被 sector_layer.py 调用，产出「一行 = 一个行业日」的阶段 2 原子聚合表，
再 merge 进 _compute_frame 的宽表。

设计约束（doc/industry_allocation_feasibility.md §2.2.1 / §4.2、资源规范 R1/R3/R7）：
  · **PIT**：成分按 trade_date ∈ [eff_from, eff_to) 取用；财务按
    announce_date ≤ trade_date 取最新报告期；一致预期快照日本身即 PIT。
  · **有效起点 2021-12-13**（决策 A）：更早日期一律不产出阶段 2 读数，
    长历史仍由不依赖成分的价格类指标（阶段 1）承担，两段不混排。
  · **分批**：a_daily_quote 自 2021-12-13 起 620 万行、daily_margin_balance 410 万行，
    一律按**月**分块查询（带 BETWEEN 时间下界 + stock_code = ANY(成分清单)），
    算完只保留该月聚合结果并释放原始行；峰值恒定，不把整段历史拉进进程。
  · **覆盖度门禁**：财务/预期覆盖 <0.6 的行业读数作废（复用 _coverage_gate 的 0.6 标定）。

输出列（trade_date, sw_code 为键）：
    up_ratio, above_ma60, new_high_low, ret_dispersion, pe_dispersion,
    turnover_w, breadth_coverage, constituent_n,
    roe_median, np_yoy_median, rev_yoy_median, improvement_breadth, fund_coverage,
    revision_breadth, rating_bull_pct, revision_n, forecast_coverage,
    margin_net
"""
from __future__ import annotations

import datetime as dt
import gc

import numpy as np
import pandas as pd

from db import read_sql
from profiling.tags._base import _read_sql_stream

PIT_START = dt.date(2021, 12, 13)
WARMUP_DAYS = 120            # rolling(60) + shift(20) 预热
MIN_COVERAGE = 0.6           # §5.7 _coverage_gate 实测标定，不得随手调
EPS_NOISE = 0.005            # 一致预期 ±0.5% 视为噪声（沿用 FORECAST.REVISION_BREADTH）
REVISION_WINDOW = 30         # 30 快照滚动
MIN_FORECAST_N = 5           # 行业可比样本下限
STREAM_CHUNK = 50_000        # R1：服务端游标每批行数（峰值与总行数脱钩）

# 批内 dtype 瘦身：object 列 56~68B/单元格，category ~4B、float32 4B（规范 §3）
_QUOTE_DTYPES = {"stock_code": "category", "trade_date": "datetime64[ns]",
                 "close": "float32", "change_pct": "float32",
                 "turnover_rate": "float32", "circular_market_val": "float32",
                 "pe_ttm_ratio": "float32"}
_MARGIN_DTYPES = {"stock_code": "category", "trade_date": "datetime64[ns]",
                  "rz_net": "float32"}
_FIN_DTYPES = {"stock_code": "category", "announce_date": "datetime64[ns]",
               "report_date": "datetime64[ns]", "roe": "float32",
               "net_profit_yoy": "float32", "revenue_yoy": "float32"}


def _as_dt(s: pd.Series) -> pd.Series:
    """统一到 datetime64[ns]：pandas 3.0 会把不同来源解析成 s / us 精度，
    merge_asof 要求两侧 dtype 完全一致，否则抛 MergeError。
    """
    return pd.to_datetime(s, errors="coerce").astype("datetime64[ns]")


def load_constituent_map(conn) -> pd.DataFrame:
    """全部版本链（含已关闭区间），约 5~8 千行，一次加载无内存压力。"""
    sql = """
        SELECT sector_code, stock_code, eff_from, eff_to, weight
        FROM regime.sector_constituent
        WHERE market='CN' AND classification='SW_L1'
    """
    df = read_sql(conn, sql)
    if df.empty:
        return pd.DataFrame(columns=["sector_code", "stock_code", "eff_from", "eff_to", "weight"])
    df["eff_from"] = _as_dt(df["eff_from"])
    df["eff_to"] = _as_dt(df["eff_to"])
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    return df


def pit_join(df: pd.DataFrame, cmap: pd.DataFrame) -> pd.DataFrame:
    """按 PIT 区间把个股行贴到行业（纯函数，可单测）。

    ⚠ 同一股票在同一天若命中多个版本区间，取 eff_from 最大的那条（版本链设计上
    不应重叠；这一步是防御，避免重复计权）。
    """
    if df.empty or cmap.empty:
        return pd.DataFrame()
    d = df.copy()
    d["trade_date"] = _as_dt(d["trade_date"])
    keys = d[["stock_code", "trade_date"]].merge(
        cmap[["sector_code", "stock_code", "eff_from", "eff_to"]],
        on="stock_code", how="inner")
    ok = ((keys["trade_date"] >= keys["eff_from"])
          & (keys["eff_to"].isna() | (keys["trade_date"] < keys["eff_to"])))
    keys = keys.loc[ok]
    if keys.empty:
        return pd.DataFrame()
    keys = (keys.sort_values("eff_from")
                .drop_duplicates(["stock_code", "trade_date"], keep="last")
                [["stock_code", "trade_date", "sector_code"]])
    return d.merge(keys, on=["stock_code", "trade_date"], how="inner")


def _month_ranges(start: dt.date, end: dt.date):
    s = dt.date(start.year, start.month, 1)
    while s <= end:
        if s.month == 12:
            e = dt.date(s.year, 12, 31)
        else:
            e = dt.date(s.year, s.month + 1, 1) - dt.timedelta(days=1)
        yield max(s, start), min(e, end)
        s = e + dt.timedelta(days=1)


def _shift_month(d: dt.date, n: int) -> dt.date:
    """月份加减，返回该月 1 日。"""
    y, m = d.year, d.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return dt.date(y, m, 1)


def _trade_dates(conn, start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    sql = """SELECT DISTINCT trade_date FROM regime.sector_daily
             WHERE trade_date BETWEEN %s AND %s ORDER BY 1"""
    df = read_sql(conn, sql, params=[start, end])
    return pd.DatetimeIndex(_as_dt(df["trade_date"])) if not df.empty else pd.DatetimeIndex([])


def _ffill_to_dates(agg: pd.DataFrame, dates: pd.DatetimeIndex,
                    value_cols: list[str]) -> pd.DataFrame:
    """把月频 / 快照频的聚合值按 as-of 前向填充到交易日网格（不产生前视）。"""
    if agg.empty or len(dates) == 0:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + value_cols)
    grid = pd.DataFrame({"trade_date": pd.DatetimeIndex(dates).astype("datetime64[ns]")})
    out = []
    for sec, g in agg.groupby("sector_code", sort=False):
        g = g.sort_values("trade_date").copy()
        g["trade_date"] = g["trade_date"].astype("datetime64[ns]")
        m = pd.merge_asof(grid, g.drop(columns=["sector_code"]),
                          on="trade_date", direction="backward")
        m["sw_code"] = sec
        out.append(m)
    res = pd.concat(out, ignore_index=True)
    return res[["trade_date", "sw_code"] + value_cols]


# ── ① 个股行情 → 行业内宽度 / 离散度 / 换手率 / 两融 ────────────────────────
_QUOTE_SQL = """
    SELECT trade_date, stock_code, close, change_pct, turnover_rate,
           circular_market_val, pe_ttm_ratio
    FROM a_daily_quote
    WHERE trade_date BETWEEN %s AND %s AND stock_code = ANY(%s)
"""
_MARGIN_SQL = """
    SELECT trade_date, stock_code, rz_net
    FROM daily_margin_balance
    WHERE trade_date BETWEEN %s AND %s AND stock_code = ANY(%s)
"""


# 按股票分批（R1 / 规范 §4.3）：1000 只 × ~140 交易日 ≈ 14 万行/批，
# 单批流式取数 + dtype 瘦身后峰值只与批大小相关，与 620 万行总规模脱钩。
BATCH_CODES = 1000

# 可跨批次相加的**充分统计量**（sum / sum of squares / count）
_SS_COLS = ["n_all", "n_close", "s_up", "c_up", "s_above", "c_above",
            "s_high", "s_low", "s_ret", "s_ret2", "c_ret",
            "s_pe", "s_pe2", "c_pe", "s_tw", "s_w"]


def _quote_partial(q: pd.DataFrame, month_start, month_end) -> pd.DataFrame:
    """单个股票批次 → 按 (trade_date, sector_code) 的充分统计量（纯函数）。

    用 sum / 平方和 / 计数而不是直接算 mean/std，才能把多个批次的结果**无损合并**
    （分布式聚合）。代价是 warmup 段每批都要重算 rolling，但换来内存恒定。
    """
    if q.empty:
        return pd.DataFrame()
    q = q.copy()
    q["trade_date"] = _as_dt(q["trade_date"])
    q["stock_code"] = q["stock_code"].astype("category")
    for c in ("close", "change_pct", "turnover_rate", "circular_market_val", "pe_ttm_ratio"):
        q[c] = pd.to_numeric(q[c], errors="coerce")
    q = q.sort_values(["stock_code", "trade_date"])
    g = q.groupby("stock_code", observed=True)["close"]
    q["ma60"] = g.transform(lambda s: s.rolling(60, min_periods=60).mean())
    q["hh60"] = g.transform(lambda s: s.rolling(60, min_periods=60).max())
    q["ll60"] = g.transform(lambda s: s.rolling(60, min_periods=60).min())
    q["ret20"] = g.transform(lambda s: s / s.shift(20) - 1)
    # warmup 段只用于 rolling 预热，不产出读数
    q = q[(q["trade_date"] >= pd.Timestamp(month_start))
          & (q["trade_date"] <= pd.Timestamp(month_end))]
    if q.empty:
        return pd.DataFrame()

    q["up"] = (q["change_pct"] > 0).where(q["change_pct"].notna()).astype(float)
    q["above"] = (q["close"] > q["ma60"]).where(q["ma60"].notna()).astype(float)
    q["high"] = (q["close"] >= q["hh60"]).where(q["hh60"].notna()).fillna(0)
    q["low"] = (q["close"] <= q["ll60"]).where(q["ll60"].notna()).fillna(0)
    w = q["circular_market_val"].where(q["circular_market_val"] > 0)
    q["w"] = w.fillna(0)
    q["tw"] = (q["turnover_rate"] * w).fillna(0)
    q["ret"] = q["ret20"].fillna(0)
    q["ret2"] = (q["ret20"] ** 2).fillna(0)
    q["pe"] = q["pe_ttm_ratio"].fillna(0)
    q["pe2"] = (q["pe_ttm_ratio"] ** 2).fillna(0)
    q["c_ret"] = q["ret20"].notna().astype(int)
    q["c_pe"] = q["pe_ttm_ratio"].notna().astype(int)

    return q.groupby(["trade_date", "sector_code"], observed=True).agg(
        n_all=("close", "size"), n_close=("close", "count"),
        s_up=("up", "sum"), c_up=("up", "count"),
        s_above=("above", "sum"), c_above=("above", "count"),
        s_high=("high", "sum"), s_low=("low", "sum"),
        s_ret=("ret", "sum"), s_ret2=("ret2", "sum"), c_ret=("c_ret", "sum"),
        s_pe=("pe", "sum"), s_pe2=("pe2", "sum"), c_pe=("c_pe", "sum"),
        s_tw=("tw", "sum"), s_w=("w", "sum"),
    ).reset_index()


def _finalize_quote(ss: pd.DataFrame) -> pd.DataFrame:
    """充分统计量 → 最终行业日指标（纯函数）。"""
    m = ss.copy()
    m["up_ratio"] = 100 * m["s_up"] / m["c_up"].replace(0, np.nan)
    m["above_ma60"] = 100 * m["s_above"] / m["c_above"].replace(0, np.nan)
    m["new_high_low"] = 100 * (m["s_high"] - m["s_low"]) / \
        (m["s_high"] + m["s_low"]).replace(0, np.nan)
    cr = m["c_ret"].replace(0, np.nan)
    cp = m["c_pe"].replace(0, np.nan)
    var_ret = m["s_ret2"] / cr - (m["s_ret"] / cr) ** 2
    var_pe = m["s_pe2"] / cp - (m["s_pe"] / cp) ** 2
    m["ret_dispersion"] = np.sqrt(var_ret.clip(lower=0))
    m["pe_dispersion"] = np.sqrt(var_pe.clip(lower=0))
    m["turnover_w"] = m["s_tw"] / m["s_w"].replace(0, np.nan)
    m["breadth_coverage"] = m["n_close"] / m["n_all"].replace(0, np.nan)
    m["constituent_n"] = m["n_all"]
    return m.rename(columns={"sector_code": "sw_code"})[
        ["trade_date", "sw_code", "up_ratio", "above_ma60", "new_high_low",
         "ret_dispersion", "pe_dispersion", "turnover_w", "breadth_coverage",
         "constituent_n"]]


def build_quote_agg(conn, cmap: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """按月 × 按股票双层分批：行情聚合 + 两融按行业。返回日频表。

    R3 关键：绝不把 620 万行 a_daily_quote / 410 万行 daily_margin_balance
    整段拉进进程；每批只保留聚合后的充分统计量（每月 31×21 行）。
    """
    codes = sorted(cmap["stock_code"].unique().tolist())
    if not codes or start > end:
        return pd.DataFrame()
    frames, margin_frames = [], []
    for ms, me in _month_ranges(start, end):
        qs = ms - dt.timedelta(days=WARMUP_DAYS)
        parts, mparts = [], []
        for i in range(0, len(codes), BATCH_CODES):
            batch = codes[i:i + BATCH_CODES]
            sub = cmap[cmap["stock_code"].isin(batch)]
            q = _read_sql_stream(conn, _QUOTE_SQL, params=[qs, me, batch],
                                 chunk_size=STREAM_CHUNK, dtypes=_QUOTE_DTYPES)
            if not q.empty:
                j = pit_join(q, sub)
                if not j.empty:
                    p = _quote_partial(j, ms, me)
                    if not p.empty:
                        parts.append(p)
                del j
            del q
            m = _read_sql_stream(conn, _MARGIN_SQL, params=[ms, me, batch],
                                 chunk_size=STREAM_CHUNK, dtypes=_MARGIN_DTYPES)
            if not m.empty:
                mj = pit_join(m, sub)
                if not mj.empty:
                    mj["rz_net"] = pd.to_numeric(mj["rz_net"], errors="coerce").fillna(0)
                    mj["trade_date"] = _as_dt(mj["trade_date"])
                    mp = (mj.groupby(["trade_date", "sector_code"], observed=True)["rz_net"]
                            .sum().reset_index()
                            .rename(columns={"sector_code": "sw_code",
                                             "rz_net": "margin_yuan"}))
                    mparts.append(mp)
                del mj
            del m
            gc.collect()
        if parts:
            ss = (pd.concat(parts, ignore_index=True)
                    .groupby(["trade_date", "sector_code"], as_index=False)[_SS_COLS].sum())
            frames.append(_finalize_quote(ss))
        if mparts:
            mg = (pd.concat(mparts, ignore_index=True)
                    .groupby(["trade_date", "sw_code"], as_index=False)["margin_yuan"].sum())
            mg["margin_net"] = mg["margin_yuan"] / 1e8      # 元 → 亿元
            margin_frames.append(mg[["trade_date", "sw_code", "margin_net"]])
    quote = (pd.concat(frames, ignore_index=True) if frames
             else pd.DataFrame(columns=["trade_date", "sw_code"]))
    margin = (pd.concat(margin_frames, ignore_index=True) if margin_frames
              else pd.DataFrame(columns=["trade_date", "sw_code", "margin_net"]))
    if quote.empty:
        return margin
    return quote.merge(margin, on=["trade_date", "sw_code"], how="outer")


# ── ② 财务 PIT 聚合（季频台阶 → as-of ffill）───────────────────────────────
_FIN_SQL = """
    SELECT stock_code, report_date, announce_date, roe, net_profit_yoy, revenue_yoy
    FROM financial_indicator
    WHERE announce_date IS NOT NULL AND report_date >= %s
      AND (stock_code LIKE 'SH.%%' OR stock_code LIKE 'SZ.%%' OR stock_code LIKE 'BJ.%%')
"""


def build_fundamental_agg(conn, cmap: pd.DataFrame, start: dt.date,
                          end: dt.date, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """按月取「截至该月已公告的最新报告期」→ 行业中位数 → as-of ffill。

    announce_date 是 PIT 闸门：只有公告日 ≤ 月末的报告才可用，杜绝前视。
    """
    cols = ["roe_median", "np_yoy_median", "rev_yoy_median",
            "improvement_breadth", "fund_coverage"]
    if cmap.empty or len(dates) == 0:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + cols)
    fin = _read_sql_stream(conn, _FIN_SQL, params=[PIT_START - dt.timedelta(days=400)],
                           chunk_size=STREAM_CHUNK, dtypes=_FIN_DTYPES)
    if fin.empty:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + cols)
    fin["announce_date"] = _as_dt(fin["announce_date"])
    fin["report_date"] = _as_dt(fin["report_date"])
    for c in ("roe", "net_profit_yoy", "revenue_yoy"):
        fin[c] = pd.to_numeric(fin[c], errors="coerce")
    fin = fin.sort_values(["stock_code", "report_date", "announce_date"])

    monthly = []
    # 月批次只需从窗口前 3 个月起（财报是季频，3 个月内必有批次可作 as-of 来源），
    # 避免增量 45 天窗口也重算 2021 年至今的全部 60 个批次。全量回填按年块调用时
    # 各块自带 3 个月预热，同样成立。
    first_month = max(_shift_month(dt.date(start.year, start.month, 1), -3),
                      dt.date(PIT_START.year, PIT_START.month, 1))
    for ms, me in _month_ranges(first_month, end):
        cutoff = pd.Timestamp(me)
        snap = fin[fin["announce_date"] <= cutoff]
        if snap.empty:
            continue
        snap = snap.groupby("stock_code", sort=False).tail(1).copy()   # 最新报告期
        # ⚠ 生效日 = **次月 1 日**，避免把「本月中下旬才公告的财报」在月初就可见
        #    （前视）。代价是财务读数最多滞后一个月，与季频源的更新节奏相称。
        snap["trade_date"] = cutoff + pd.Timedelta(days=1)
        snap = pit_join(snap, cmap)
        if snap.empty:
            continue
        g = snap.groupby(["trade_date", "sector_code"], observed=True)
        out = pd.DataFrame({
            "roe_median": g["roe"].median(),
            "np_yoy_median": g["net_profit_yoy"].median(),
            "rev_yoy_median": g["revenue_yoy"].median(),
            "improvement_breadth": g["net_profit_yoy"].apply(
                lambda s: (s > 0).sum() / s.notna().sum() * 100 if s.notna().sum() else np.nan),
            "fund_coverage": g["roe"].apply(
                lambda s: s.notna().sum() / max(len(s), 1)),
        }).reset_index()
        monthly.append(out)
    if not monthly:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + cols)
    agg = pd.concat(monthly, ignore_index=True)
    # 覆盖度门禁：<0.6 的行业整行作废（不是降权，是清除）
    bad = agg["fund_coverage"] < MIN_COVERAGE
    agg.loc[bad, cols[:-1]] = np.nan
    return _ffill_to_dates(agg, dates, cols)


# ── ③ 一致预期修正宽度下沉（M7）───────────────────────────────────────────
_FC_SQL = """
    SELECT snapshot_date, stock_code, report_count, fy1_year, fy1_eps,
           rating_buy, rating_overweight, rating_neutral, rating_reduce, rating_sell
    FROM regime.analyst_forecast_snapshot
    ORDER BY stock_code, snapshot_date
"""


def build_forecast_agg(conn, cmap: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """把市场级 FORECAST.REVISION_BREADTH 口径按成分下沉到行业。

    沿用 market_state_daily._forecast_indicators 的定义：同财年 FY1 EPS 相邻快照
    差分，±0.5% 内视为噪声，30 快照滚动净占比。⚠ 阶段 0 实测仅 25 个快照日，
    窗口未满时按实际 n 出值并透出 revision_n，不得冒充 30 快照。
    """
    cols = ["revision_breadth", "rating_bull_pct", "revision_n", "forecast_coverage"]
    if cmap.empty or len(dates) == 0:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + cols)
    fc = read_sql(conn, _FC_SQL)
    if fc.empty:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + cols)
    fc["snapshot_date"] = _as_dt(fc["snapshot_date"])
    fc["fy1_eps"] = pd.to_numeric(fc["fy1_eps"], errors="coerce")
    fc = fc.sort_values(["stock_code", "snapshot_date"])
    g = fc.groupby("stock_code", sort=False)
    prev_eps, prev_year = g["fy1_eps"].shift(1), g["fy1_year"].shift(1)
    same_year = prev_year == fc["fy1_year"]
    base = prev_eps.abs().replace(0, np.nan)
    chg = (fc["fy1_eps"] - prev_eps) / base
    fc["dir"] = np.where(same_year & chg.notna() & (chg > EPS_NOISE), 1,
                         np.where(same_year & chg.notna() & (chg < -EPS_NOISE), -1, 0))
    for c in ("rating_buy", "rating_overweight", "rating_neutral", "rating_reduce", "rating_sell"):
        fc[c] = pd.to_numeric(fc[c], errors="coerce").fillna(0)
    fc["bull"] = fc["rating_buy"] + fc["rating_overweight"]
    fc["ratings"] = fc[["rating_buy", "rating_overweight", "rating_neutral",
                        "rating_reduce", "rating_sell"]].sum(axis=1)
    fc["trade_date"] = fc["snapshot_date"]
    fc = pit_join(fc[["trade_date", "stock_code", "dir", "bull", "ratings", "report_count"]],
                  cmap)
    if fc.empty:
        return pd.DataFrame(columns=["trade_date", "sw_code"] + cols)
    grp = fc.groupby(["trade_date", "sector_code"], observed=True)
    per = pd.DataFrame({
        "net": grp["dir"].sum(),
        "n": grp["dir"].size(),
        "bull": grp["bull"].sum(),
        "ratings": grp["ratings"].sum(),
    }).reset_index().sort_values(["sector_code", "trade_date"])
    # 覆盖度：该行业有预期数据的成分数 / 当日 PIT 有效成分数
    snap_dates = pd.DatetimeIndex(sorted(fc["trade_date"].unique()))
    counts = []
    for t in snap_dates:
        a = cmap[(cmap["eff_from"] <= t)
                 & (cmap["eff_to"].isna() | (t < cmap["eff_to"]))]
        cnt = a.groupby("sector_code").size().rename("active_n").reset_index()
        cnt["trade_date"] = t
        counts.append(cnt)
    if counts:
        active = pd.concat(counts, ignore_index=True)
        per = per.merge(active, on=["trade_date", "sector_code"], how="left")
        per["forecast_coverage"] = per["n"] / per["active_n"].replace(0, np.nan)
    else:
        per["forecast_coverage"] = np.nan
    gg = per.groupby("sector_code", sort=False)
    per["net_w"] = gg["net"].transform(
        lambda s: s.rolling(REVISION_WINDOW, min_periods=1).sum())
    per["n_w"] = gg["n"].transform(
        lambda s: s.rolling(REVISION_WINDOW, min_periods=1).sum())
    per["n_snap"] = gg["n"].cumcount() + 1
    per["revision_breadth"] = 100 * per["net_w"] / per["n_w"].replace(0, np.nan)
    per["rating_bull_pct"] = 100 * per["bull"] / per["ratings"].replace(0, np.nan)
    per["revision_n"] = per["n_snap"]
    # 覆盖门禁：可比样本 <5 家、或预期覆盖成分 <30% 时不出值（§3.4 E 组）
    per.loc[(per["n_w"] < MIN_FORECAST_N)
            | (per["forecast_coverage"] < 0.3), "revision_breadth"] = np.nan
    agg = per[["trade_date", "sector_code", "revision_breadth", "rating_bull_pct",
               "revision_n", "forecast_coverage"]]
    return _ffill_to_dates(agg, dates, cols)


# ── ④ 汇总 ────────────────────────────────────────────────────────────────
def build_stage2_agg(conn, start: dt.date, end: dt.date) -> pd.DataFrame:
    """阶段 2 聚合总入口。start < PIT_START 时自动截到 PIT_START（决策 A）。"""
    eff_start = max(start, PIT_START)
    empty = pd.DataFrame(columns=["trade_date", "sw_code"])
    if eff_start > end:
        return empty
    cmap = load_constituent_map(conn)
    if cmap.empty:
        return empty
    dates = _trade_dates(conn, eff_start, end)
    if len(dates) == 0:
        return empty

    quote = build_quote_agg(conn, cmap, eff_start, end)
    fund = build_fundamental_agg(conn, cmap, eff_start, end, dates)
    fcst = build_forecast_agg(conn, cmap, dates)
    out = quote
    for part in (fund, fcst):
        if not part.empty:
            out = (out.merge(part, on=["trade_date", "sw_code"], how="outer")
                   if not out.empty else part)
    if out.empty:
        return empty
    out["trade_date"] = _as_dt(out["trade_date"])
    return out.sort_values(["sw_code", "trade_date"]).reset_index(drop=True)
