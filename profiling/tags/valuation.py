#!/usr/bin/env python3
"""③ 估值水平标签域

域定义：描述**价格相对于公司经营基本面**的贵贱，回答「贵还是便宜」。
判定标准：形如「市值 ÷ 经营基本面指标（盈利/净资产/营收/现金流）」及其倒数。
**分母必须是经营基本面**——这是它与「股东回报」域（分红/回购，属分配行为）的根本分界。

═══ 负值处理（估值标签的关键设计）═══
估值比率为负意味着亏损（PE）或资不抵债（PB），此时「数值低」不代表「便宜」：
PE = -5 在数值上小于 PE = 10，但前者是亏损股、后者是便宜股。
因此本域统一规则：
    · 正值 → 参与横截面分档（1 = 最便宜 20%，5 = 最贵 20%）
    · 负值 → 单独枚举值（LOSS / NEGATIVE），不参与分档，筛选时必须显式排除
    · 缺失 → 不打标

═══ 双分位（文档硬性要求）═══
不同行业估值中枢差异极大（银行 PE 5 倍、科技 50 倍），只给全市场档或只给行业档都会误导，
所以 PE 同时提供三个视角：
    val_pe_ttm_tier  全市场档（按市场分组）
    val_pe_ind_pct   行业分位（横截面行业内百分位）
    val_pe_hist_pct  历史分位（个股自身时序百分位）

数据来源：
    a_daily_quote / hk_daily_quote  pe_ttm_ratio / pb_ratio / ps_ttm_ratio / pcf_ttm_ratio / 市值
    financial_indicator             free_cash_flow（年报）

覆盖限制：
    ps_ttm_ratio / pcf_ttm_ratio 目前仅 A 股有值（港股两列为空），相关标签只覆盖 A 股。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..registry import tag, TIER, PLANNED_NO_DATA
from ..quantile import (
    QUOTE_TABLES, load_quote_snapshot, load_financial_latest, load_gics_map,
    assign_tier, historical_percentile, tier_or_flag,
)
from ._base import _conn, _read_sql, _frame

DOMAIN = "估值水平"

# 1 档 = 最便宜（数值最低），5 档 = 最贵
_TIER_RANGE = {
    "1": "最便宜 20%", "2": "次便宜 20%", "3": "中间 20%", "4": "次贵 20%", "5": "最贵 20%",
}
# 分位类标签：1 = 处于历史/行业最低区间
_PCT_RANGE = {
    "1": "最低 20% 区间", "2": "次低 20% 区间", "3": "中间 20% 区间",
    "4": "次高 20% 区间", "5": "最高 20% 区间",
}


def _pct_to_tier(pct: pd.Series, n_tiers: int = 5) -> pd.Series:
    """百分位(0-100) → 1..n 档字符串（1 = 最低区间）。

    必须显式转 int：np.ceil 返回 float，直接转字符串会得到 '1.0' 这种脏档位。
    """
    p = pd.to_numeric(pct, errors="coerce")
    out = pd.Series(pd.NA, index=p.index, dtype="object")
    valid = p.notna()
    if valid.any():
        tier = np.ceil(p[valid] / (100.0 / n_tiers)).clip(1, n_tiers).astype(int)
        out[valid] = tier.astype(str)
    return out


def _market_industry_key(market: pd.Series, gics: pd.Series) -> pd.Series:
    return market.astype(str) + "|" + gics.astype(str)


# ═══════════════════════════════════════════════════════════════════════════
# 四大估值比率档（日频，随价变化）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="val_pe_ttm_tier", name="PE(TTM)档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER,
    value_range={**_TIER_RANGE, "LOSS": "亏损（PE 为负，不参与分档）"},
    source_type="stat", update_freq="daily", data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="按市场分组对 PE(TTM) 做横截面五等分：1=最便宜20%，5=最贵20%；"
                  "PE<=0（亏损）打 LOSS，不参与分档。num_value 存 PE 原值",
    pit_capable=True, owner="profiling",
)
def val_pe_ttm_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
    tier = tier_or_flag(snap["pe_ttm"], by=snap["market"], flag="LOSS")
    return _frame(snap["stock_code"], tier, snap["pe_ttm"])


@tag(
    code="val_pb_tier", name="PB档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER,
    value_range={**_TIER_RANGE, "NEGATIVE": "资不抵债（净资产为负，不参与分档）"},
    source_type="stat", update_freq="daily", data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="按市场分组对 PB 做横截面五等分；PB<=0（资不抵债）打 NEGATIVE。num_value 存 PB 原值。"
                  "保留全市场口径的理由：PB 是风格因子语言（HML/低市净率策略均为全市场排序），"
                  "且有「破净」硬锚跨行业成立；个股的行业内贵贱看 val_pb_ind_pct",
    pit_capable=True, owner="profiling",
)
def val_pb_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
    tier = tier_or_flag(snap["pb"], by=snap["market"], flag="NEGATIVE")
    return _frame(snap["stock_code"], tier, snap["pb"])


@tag(
    code="val_ps_ttm_tier", name="PS(TTM)档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER,
    value_range={**_TIER_RANGE, "NEGATIVE": "营收为负或无效，不参与分档"},
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "stock_sector", "sector_hierarchy"],
    compute_logic="PS(TTM) 在【市场+GICS】内做横截面五等分；非正值打 NEGATIVE。"
                  "刻意不用全市场分组：PS 由商业模式决定（零售 0.2x vs 软件 10x），"
                  "全市场档测量的主要是行业毛利结构而非贵贱，跨行业比较无意义。num_value 存 PS 原值",
    pit_capable=True, owner="profiling",
)
def val_ps_ttm_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        gics_map = load_gics_map(conn)
    df = snap[snap["ps_ttm"].notna()].merge(gics_map, on="stock_code", how="inner")
    grp = df["market"].astype(str) + "|" + df["gics"].astype(str)
    tier = tier_or_flag(df["ps_ttm"], by=grp, flag="NEGATIVE")
    return _frame(df["stock_code"], tier, df["ps_ttm"])


@tag(
    code="val_pcf_ttm_tier", name="PCF(TTM)档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER,
    value_range={**_TIER_RANGE, "NEGATIVE": "经营现金流为负，不参与分档"},
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "stock_sector", "sector_hierarchy"],
    compute_logic="PCF(TTM)（市值/经营现金流）在【市场+GICS】内做横截面五等分；非正值打 NEGATIVE。"
                  "与 PS 同理：重资产行业的折旧摊销加回会系统性抬高 OCF，全市场比较无意义。"
                  "PCF 比 PE 更难被会计操纵，适合行业内与 PE 交叉验证。num_value 存 PCF 原值",
    pit_capable=True, owner="profiling",
)
def val_pcf_ttm_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        gics_map = load_gics_map(conn)
    df = snap[snap["pcf_ttm"].notna()].merge(gics_map, on="stock_code", how="inner")
    grp = df["market"].astype(str) + "|" + df["gics"].astype(str)
    tier = tier_or_flag(df["pcf_ttm"], by=grp, flag="NEGATIVE")
    return _frame(df["stock_code"], tier, df["pcf_ttm"])


# ═══════════════════════════════════════════════════════════════════════════
# 双分位：行业分位 + 历史分位
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="val_pe_ind_pct", name="PE行业分位", domain=DOMAIN,
    num_unit="percentile_0_100",
    value_type=TIER, value_range=dict(_PCT_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "stock_sector", "sector_hierarchy"],
    compute_logic="在【市场 + GICS 部门】内对 PE(TTM) 算横截面百分位，再切成五档："
                  "1=行业内最便宜20%，5=行业内最贵20%。num_value 存百分位(0-100)。"
                  "仅正 PE 参与，亏损股不打标",
    pit_capable=True, owner="profiling",
)
def val_pe_ind_pct(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        gics_map = load_gics_map(conn)

    df = snap[["stock_code", "market", "pe_ttm"]].merge(gics_map, on="stock_code", how="inner")
    df = df[df["pe_ttm"] > 0]

    grp = _market_industry_key(df["market"], df["gics"])
    pct = df["pe_ttm"].groupby(grp).rank(pct=True, method="average") * 100.0
    return _frame(df["stock_code"], _pct_to_tier(pct), pct.round(2))


@tag(
    code="val_pe_hist_pct", name="PE历史分位", domain=DOMAIN,
    num_unit="percentile_0_100",
    value_type=TIER, value_range=dict(_PCT_RANGE),
    source_type="stat", update_freq="daily", data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="个股自身时序分位：当前 PE(TTM) 在过去 750 个交易日（约 3 年）区间中的百分位，"
                  "再切成五档：1=处于自身历史最低20%区间，5=历史最高20%区间。num_value 存百分位(0-100)。"
                  "要求至少 60 个有效观测；仅正 PE 参与（亏损股的历史分位无意义）",
    pit_capable=True, owner="profiling",
)
def val_pe_hist_pct(as_of: date) -> pd.DataFrame:
    parts = []
    with _conn() as conn:
        for mkt, table in QUOTE_TABLES.items():
            pct = historical_percentile(
                conn, table, "pe_ttm_ratio", as_of, window_days=750, positive_only=True
            )
            if pct.empty:
                continue
            parts.append(pct.rename("pct").reset_index().assign(market=mkt))

    if not parts:
        return _frame([], [])

    df = pd.concat(parts, ignore_index=True).rename(columns={"index": "stock_code"})
    df = df[df["stock_code"].notna()]
    return _frame(df["stock_code"], _pct_to_tier(df["pct"]), df["pct"])


@tag(
    code="val_pb_ind_pct", name="PB行业分位", domain=DOMAIN,
    num_unit="percentile_0_100",
    value_type=TIER, value_range=dict(_PCT_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "stock_sector", "sector_hierarchy"],
    compute_logic="PB 在【市场+GICS】内横截面百分位→五档：1=行业内最低20%，5=行业内最高20%。"
                  "仅正 PB 参与（净资产为负打 NEGATIVE 于全市场档）。与 val_pe_ind_pct 对称，"
                  "回答「同行业内贵贱」；风格筛选（全市场低 PB）用 val_pb_tier",
    pit_capable=True, owner="profiling",
)
def val_pb_ind_pct(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        gics_map = load_gics_map(conn)
    df = snap[["stock_code", "market", "pb"]].merge(gics_map, on="stock_code", how="inner")
    df = df[df["pb"] > 0]
    grp = df["market"].astype(str) + "|" + df["gics"].astype(str)
    pct = df["pb"].groupby(grp).rank(pct=True, method="average") * 100.0
    return _frame(df["stock_code"], _pct_to_tier(pct), pct.round(2))


# ═══════════════════════════════════════════════════════════════════════════
# 自由现金流收益率（季频，财报驱动）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="val_fcf_yield_tier", name="FCF收益率档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={
        "1": "最低 20%（自由现金流回报最差）", "2": "次低 20%", "3": "中间 20%",
        "4": "次高 20%", "5": "最高 20%（自由现金流回报最好）",
        "NEGATIVE": "自由现金流为负（烧钱）",
    },
    source_type="stat", update_freq="quarterly",
    data_sources=["financial_indicator", "a_daily_quote", "hk_daily_quote"],
    compute_logic="FCF收益率 = 最近一期年报 free_cash_flow / 总市值 × 100%，按市场分组五等分。"
                  "FCF 为负打 NEGATIVE。num_value 存收益率百分比。"
                  "注：季报（Q1/Q3）缺 capex 明细导致 FCF 大量缺失，故只用年报",
    pit_capable=False,  # 财报无披露日
    owner="profiling",
)
def val_fcf_yield_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        fin = load_financial_latest(conn, as_of, "annual")

    df = snap[["stock_code", "market", "total_market_val"]].merge(
        fin[["stock_code", "free_cash_flow"]], on="stock_code", how="inner"
    )
    df["free_cash_flow"] = pd.to_numeric(df["free_cash_flow"], errors="coerce")
    df["total_market_val"] = pd.to_numeric(df["total_market_val"], errors="coerce")
    df = df[df["total_market_val"] > 0]

    yield_pct = df["free_cash_flow"] / df["total_market_val"] * 100.0
    tier = tier_or_flag(yield_pct, by=df["market"], flag="NEGATIVE")
    return _frame(df["stock_code"], tier, yield_pct.round(4))


# ═══════════════════════════════════════════════════════════════════════════
# 数据源缺失
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="val_ev_ebitda_tier", name="EV/EBITDA档", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="EV = 市值 + 有息负债 - 货币资金；EBITDA = 净利润 + 利息 + 税 + 折旧摊销。"
                  "含债口径，跨行业与跨杠杆水平可比性优于 PE",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：financial_indicator 无 EBITDA、有息负债、货币资金等科目，"
                   "需扩展财报采集（利润表需补折旧摊销与利息，资产负债表需补有息负债与现金）",
    pit_capable=False, owner="profiling",
)
def val_ev_ebitda_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="val_ah_premium", name="AH溢价率", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily", data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="对同一公司的 A 股与 H 股，溢价率 = A股价 / (H股价 × HKD/CNY汇率) - 1，"
                  "按历史分布分档（1=折价最多，5=溢价最高）",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：库中无 HKD/CNY 汇率序列。没有汇率就无法把 H 股价换算成人民币，"
                   "算出的溢价率会失真（当前汇率约 0.92，误差虽小但不可用于时序比较）",
    pit_capable=True, owner="profiling",
)
def val_ah_premium(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失（缺汇率），见 blocked_reason")
