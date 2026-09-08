#!/usr/bin/env python3
"""⑥ 股东回报标签域

域定义：描述公司**向股东返还现金的方式与力度**，回答「股东能拿回多少」。
判定标准：分红与回购相关，属**分配行为**而非经营基本面——这是它与 ③ 估值
（分母是经营基本面）、④ 盈利质量（不含价格）的分界。股息率虽含价格，
但回答的是「回报多少」而非「贵不贵」，故独立成域。

数据来源（两表一事件流）：
    dividend_history          分红明细（is_dividend=纯现金分红；ex_date 除权除息日；
                              dps 每股股息；A/北交所 CNY，港股 HKD——与行情价同币种，
                              每股口径无需汇率；USD 派息仅 250 条已排除）
    daily_buyback_event       港股回购逐日金额（1991 起，1,341 只，HKD）
    a_stock_repurchase_plan   A 股回购方案（repurchased_amt=已回购金额累计，元）

口径约定：
    · 事件去重：DISTINCT (stock_code, ex_date, dps)——预案/实施双行只计一次
    · TTM 窗口 = ex_date 落在 (as_of−365, as_of]
    · 分红率 = 最近完整分红年度（ex 年）的分红总额 / 上一年报净利润——
      年报利润的分红通常在次年 5-8 月除权，用 ex 年 = report 年+1 近似配对
    · 股本 = 总市值 / 收盘价（行情表逐日可推，避免引入股本表依赖）
    · 股息率/股东回报率按市场分组（A/H 市场股息中枢不同）；分红率行业内分档
      （派息率正常区间行业差异大：银行 30% vs 成长股 10%）

覆盖限制：
    · 港股分红明细 2022 年起才完整（2018-2021 每年仅 343-472 个事件 vs 2025 的 1,340），
      连续分红年数对港股偏保守——数据缺失按中断处理，不给信用
    · A 股回购分子用「近一年启动方案的已回购金额」近似 TTM（方案表无逐日明细）
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ..registry import tag, TIER, ENUM
from ..quantile import assign_tier, load_quote_snapshot, load_financial_context, load_financial_history, load_gics_map
from ._base import _conn, _read_sql, _frame

DOMAIN = "股东回报"

_YIELD_RANGE = {
    "1": "股息率最低 20%", "2": "次低", "3": "中间", "4": "次高", "5": "股息率最高 20%（高股息组）",
}
_PAYOUT_RANGE = {
    "1": "派息率最低 20%", "2": "次低", "3": "中间", "4": "次高", "5": "派息率最高 20%",
}
_CONSEC_RANGE = {
    "0": "最近完整年度未分红", "1": "连续 1-2 年", "2": "连续 3-5 年",
    "3": "连续 6-10 年", "4": "连续 >10 年（分红纪律最强）",
}
_GROWTH_RANGE = {
    "increasing": "分红同比增长 ≥10%", "steady": "分红同比变化 ±10% 内",
    "decreasing": "分红同比下滑 ≥10%", "new": "上上年未分红、本年恢复分红",
}


def _load_dividends(conn, as_of: date) -> pd.DataFrame:
    """标准现金分红事件（已实施、正 dps、同币种、按事件去重）。"""
    return _read_sql(
        conn,
        """
        SELECT DISTINCT stock_code, ex_date, dps::float8 AS dps, currency
        FROM dividend_history
        WHERE is_dividend = TRUE AND dps > 0
          AND ex_date <= %s AND ex_date >= date '1996-01-01'
          AND progress IN ('实施完成', '实施分配')
          AND currency IN ('CNY', 'HKD')
        """,
        (as_of,),
    )


def _annual_dps(div: pd.DataFrame) -> pd.DataFrame:
    """每股口径：股票 × 自然年（ex_date 年）的 DPS 合计（仅正分红年）。"""
    d = div.copy()
    d["year"] = pd.to_datetime(d["ex_date"]).dt.year
    out = d.groupby(["stock_code", "year"])["dps"].sum().reset_index()
    return out[out["dps"] > 0]


def _market_key(market: pd.Series, gics: pd.Series | None = None) -> pd.Series:
    if gics is None:
        return market.astype(str)
    return market.astype(str) + "|" + gics.astype(str)


# ═══════════════════════════════════════════════════════════════════════════
# 股息率（日频，TTM 口径自算，与行情表 dividend_ratio_ttm 的差异来自窗口定义）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="shr_dividend_yield_tier", name="股息率档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_YIELD_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["dividend_history", "a_daily_quote", "hk_daily_quote"],
    compute_logic="TTM 股息率 = 近 365 天 ex_date 的 DPS 合计 / 收盘价 ×100（每股口径，"
                  "dps 与价格同币种无需汇率），在市场内五等分：1=最低，5=高股息组。"
                  "与行情表 dividend_ratio_ttm 的差异来自 TTM 窗口定义（ex_date 口径）",
    pit_capable=True, owner="profiling",
)
def shr_dividend_yield_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        div = _load_dividends(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    ttm = div[div["ex_date"] > as_of - timedelta(days=365)]
    ttm_dps = ttm.groupby("stock_code")["dps"].sum()

    df = snap[["stock_code", "market", "close"]].merge(
        ttm_dps.rename("ttm_dps"), on="stock_code", how="inner"
    )
    df = df[df["ttm_dps"] > 0]
    yld = df["ttm_dps"] / df["close"] * 100.0
    tier = assign_tier(yld, 5, by=df["market"])
    return _frame(df["stock_code"], tier, yld.round(4))


# ═══════════════════════════════════════════════════════════════════════════
# 分红率 / 连续年数 / 增长性（季频，财报驱动）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="shr_payout_tier", name="分红率档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_PAYOUT_RANGE),
    source_type="stat", update_freq="quarterly",
    data_sources=["dividend_history", "financial_indicator", "a_daily_quote", "hk_daily_quote",
                  "stock_sector", "sector_hierarchy"],
    compute_logic="派息率 = 最近完整分红年度（ex 年=去年）的每股分红合计 × 股本（总市值/收盘价）/ "
                  "上一年报净利润 ×100，在【市场+GICS】内五等分（派息率正常区间行业差异大）。"
                  "近似口径：ex 年与 report 年按 +1 配对，股本按当前值回推",
    pit_capable=False, owner="profiling",
)
def shr_payout_tier(as_of: date) -> pd.DataFrame:
    ex_year = as_of.year - 1
    with _conn() as conn:
        div = _load_dividends(conn, as_of)
        ctx = load_financial_context(conn, as_of)
        hist = load_financial_history(conn, as_of, "annual", periods=2)

    d = div[pd.to_datetime(div["ex_date"]).dt.year == ex_year]
    dps_sum = d.groupby("stock_code")["dps"].sum()
    np_prev = hist[hist["rn"] == 1][["stock_code", "net_profit"]]   # 上一年报（ex_year−1）

    df = ctx[["stock_code", "market", "gics", "total_market_val", "close", "net_profit"]].copy()
    df = df.merge(dps_sum.rename("dps_y"), on="stock_code", how="inner")
    df = df.merge(np_prev.rename(columns={"net_profit": "np_prev"}), on="stock_code", how="inner")
    for c in ("total_market_val", "close", "np_prev"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["close"] > 0) & (df["np_prev"] > 0)]
    # 分红总额 = 每股分红 × 股本（股本=总市值/收盘价），再除以上一年报净利润
    df["payout"] = df["dps_y"] * (df["total_market_val"] / df["close"]) / df["np_prev"] * 100.0

    tier = assign_tier(df["payout"], 5, by=_market_key(df["market"], df["gics"]))
    return _frame(df["stock_code"], tier, df["payout"].round(2))


@tag(
    code="shr_consecutive_years", name="连续分红年数", domain=DOMAIN,
    num_unit="count",
    value_type=TIER, value_range=dict(_CONSEC_RANGE),
    source_type="rule", update_freq="quarterly", data_sources=["dividend_history"],
    compute_logic="从最近完整年度（去年）往前数每年都有现金分红的连续年数（当年已有分红也计入）。"
                  "覆盖限制：港股分红明细 2022 年起才完整，早年缺失按中断处理，港股该值偏保守",
    pit_capable=True, owner="profiling",
)
def shr_consecutive_years(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        div = _load_dividends(conn, as_of)
    ys = _annual_dps(div)
    have = ys.groupby("stock_code")["year"].agg(lambda s: set(s))
    cur_year, prev_year = as_of.year, as_of.year - 1

    def streak(stock: str) -> int:
        years = have.get(stock, set())
        base = cur_year if cur_year in years else prev_year
        n = 0
        while base - n in years:
            n += 1
        return n

    out = pd.DataFrame({"stock_code": have.index})
    out["consec"] = [streak(s) for s in out["stock_code"]]

    def bucket(n):
        if n == 0:
            return "0"
        if n <= 2:
            return "1"
        if n <= 5:
            return "2"
        if n <= 10:
            return "3"
        return "4"

    out["key_value"] = out["consec"].map(bucket)
    return _frame(out["stock_code"], out["key_value"], out["consec"])


@tag(
    code="shr_dividend_growth", name="分红增长性", domain=DOMAIN,
    num_unit=None,
    value_type=ENUM,
    enum_type="dividend_growth",
    enum_values={
        "increasing": ("分红同比增长 ≥10%", None),
        "steady": ("分红同比变化 ±10% 内", None),
        "decreasing": ("分红同比下滑 ≥10%", None),
        "new": ("上上年未分红、本年恢复分红", None),
    },
    source_type="rule", update_freq="quarterly", data_sources=["dividend_history"],
    compute_logic="最近两个完整分红年度的每股分红合计对比：≥+10% 记 increasing，"
                  "≤−10% 记 decreasing，其余 steady；上上年未分红、去年恢复分红记 new。"
                  "任一年度缺分红事件则不打标（连续性见 shr_consecutive_years）",
    pit_capable=True, owner="profiling",
)
def shr_dividend_growth(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        div = _load_dividends(conn, as_of)
    ys = _annual_dps(div)
    y1, y0 = as_of.year - 1, as_of.year - 2
    cur = ys[ys["year"] == y1].set_index("stock_code")["dps"].rename("cur")
    prev = ys[ys["year"] == y0].set_index("stock_code")["dps"].rename("prev")
    df = pd.concat([cur, prev], axis=1, join="inner").reset_index()
    df = df[df["prev"] > 0]

    def key(r):
        delta = (r["cur"] - r["prev"]) / r["prev"]
        if delta >= 0.10:
            return "increasing"
        if delta <= -0.10:
            return "decreasing"
        return "steady"

    df = df.copy()
    df["key_value"] = df.apply(key, axis=1)
    return _frame(df["stock_code"], df["key_value"], None)


# ═══════════════════════════════════════════════════════════════════════════
# 回购力度 / 股东回报率
# ═══════════════════════════════════════════════════════════════════════════


def _buyback_ttm(conn, as_of: date) -> pd.Series:
    """近 12 个月回购金额（原币）。港股=逐日明细精确值；A股=近一年启动方案的累计回购近似值。"""
    start = as_of - timedelta(days=365)
    hk = _read_sql(
        conn,
        "SELECT stock_code, SUM(amount)::float8 AS amt FROM daily_buyback_event "
        "WHERE buyback_date > %s AND buyback_date <= %s GROUP BY 1",
        (start, as_of),
    ).set_index("stock_code")["amt"]
    a = _read_sql(
        conn,
        "SELECT stock_code, SUM(repurchased_amt)::float8 AS amt FROM a_stock_repurchase_plan "
        "WHERE start_date > %s AND start_date <= %s AND repurchased_amt IS NOT NULL GROUP BY 1",
        (start, as_of),
    ).set_index("stock_code")["amt"]
    return pd.concat([hk, a])


@tag(
    code="shr_buyback_tier", name="回购力度档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={
        "1": "无/回购率最低 20%", "2": "次低", "3": "中间", "4": "次高",
        "5": "回购率最高 20%（回购力度最大）",
    },
    source_type="stat", update_freq="daily",
    data_sources=["daily_buyback_event", "a_stock_repurchase_plan", "a_daily_quote", "hk_daily_quote"],
    compute_logic="回购率 = 近 12 个月回购金额 / 总市值 ×100，在市场内五等分。"
                  "港股用逐日回购明细（精确）；A 股用「近一年启动方案的已回购金额累计」近似（方案表无逐日明细）",
    pit_capable=True, owner="profiling",
)
def shr_buyback_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        bb = _buyback_ttm(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    df = snap[["stock_code", "market", "total_market_val"]].merge(
        bb.rename("bb_amt"), on="stock_code", how="inner"
    )
    df = df[df["total_market_val"] > 0]
    df["rate"] = df["bb_amt"] / df["total_market_val"] * 100.0
    tier = assign_tier(df["rate"], 5, by=df["market"])
    return _frame(df["stock_code"], tier, df["rate"].round(4))


@tag(
    code="shr_total_yield_tier", name="股东回报率档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={
        "NONE": "无分红且无回购（零回报），不参与分档",
        "1": "有回报但最低 20%", "2": "次低", "3": "中间", "4": "次高",
        "5": "总回报率最高 20%（分红+回购回报最厚）",
    },
    source_type="stat", update_freq="daily",
    data_sources=["dividend_history", "daily_buyback_event", "a_stock_repurchase_plan",
                  "a_daily_quote", "hk_daily_quote"],
    compute_logic="股东回报率 = TTM 股息率 + TTM 回购率（近一年 ex 的 DPS 合计 + 近一年回购金额）。"
                  "**零回报（既无分红又无回购）单独标 NONE**：实测 ≥25% 的股票为零，"
                  "它们既不是「低回报」也不是「高回报」，混进档 1 会让档 1 失去语义（与回报 0.3% 的股票同档）。"
                  "有回报的股票在市场内五等分。A 股回购部分为方案累计近似（见 shr_buyback_tier），港股两项均精确。"
                  "已知缺陷：分子（近一年派息）用**当前价**折算，股价在派息后暴跌时股息率会虚高——"
                  "实测存在 500%+ 的仙股样本（如派息 0.462 而现价 0.086）。等频分档对极值免疫，"
                  "但筛选高回报档时建议结合 num_value 设上限（如 <20%）剔除噪声",
    pit_capable=True, owner="profiling",
)
def shr_total_yield_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        div = _load_dividends(conn, as_of)
        bb = _buyback_ttm(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    ttm = div[div["ex_date"] > as_of - timedelta(days=365)]
    ttm_dps = ttm.groupby("stock_code")["dps"].sum()

    df = snap[["stock_code", "market", "close", "total_market_val"]].copy()
    df = df.merge(ttm_dps.rename("ttm_dps"), on="stock_code", how="left")
    df = df.merge(bb.rename("bb_amt"), on="stock_code", how="left")
    df[["ttm_dps", "bb_amt"]] = df[["ttm_dps", "bb_amt"]].fillna(0)
    df = df[(df["close"] > 0) & (df["total_market_val"] > 0)]

    div_yld = df["ttm_dps"] / df["close"] * 100.0
    bb_rate = df["bb_amt"] / df["total_market_val"] * 100.0
    total = div_yld + bb_rate

    df = df.assign(total=total)
    tier = pd.Series(pd.NA, index=df.index, dtype="object")
    paying = df["total"] > 0
    if paying.any():
        tier[paying] = assign_tier(df.loc[paying, "total"], 5, by=df.loc[paying, "market"])
    tier[~paying] = "NONE"
    return _frame(df["stock_code"], tier, total.round(4))
