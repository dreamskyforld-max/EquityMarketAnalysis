#!/usr/bin/env python3
"""② 规模属性标签域

域定义：描述公司在**资本市场的体量**，回答「有多大」。
判定标准：由市值、营收、资产规模派生；**时变量**（随股价与财报变化），
故与 ① 证券属性（准静态身份）分离。

分档口径（与全项目一致）：
    · 用横截面分位而非绝对阈值——绝对阈值会随时间漂移（今天 100 亿是大盘，2019 年是巨无霸）
    · tier 取值 1..5，1 = 最小/最低，5 = 最大/最高
    · 按市场分组分档：A 股用人民币、港股用港元，跨市场直接比较会混入汇率因素
    · 行业内比较时同样叠加市场分组（避免同行业里 A/H 公司因币种不同被错排）

未在本域重复实现的标签：
    · 「指数成分」→ 复用 ① 域的 idt_index_member（一个概念一个标签，不重复建）

数据来源：
    a_daily_quote / hk_daily_quote  市值（total_market_val / circular_market_val）
    financial_indicator             营收（annual 年报，revenue）
    daily_ggt_hold                  南向持股（港股通标的的近似口径）
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..registry import tag, BOOL, TIER, PLANNED_NO_DATA
from ..quantile import (
    load_quote_snapshot, load_financial_latest, load_gics_map,
    load_stock_connect, assign_tier, rank_within_group,
)
from ._base import _conn, _frame

DOMAIN = "规模属性"

_TIER_RANGE = {
    "1": "最小 20%", "2": "次小 20%", "3": "中间 20%", "4": "次大 20%", "5": "最大 20%",
}


def _market_industry_key(snap: pd.DataFrame, gics: pd.Series) -> pd.Series:
    """分组键 = 市场 + GICS 部门（同时规避跨市场汇率与跨行业不可比）。"""
    return snap["market"].astype(str) + "|" + gics.astype(str)


# ═══════════════════════════════════════════════════════════════════════════
# 市值类（日频，随股价变化）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="scl_mktcap_tier", name="总市值档", domain=DOMAIN,
    num_unit="CNY|HKD",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="按市场分组（A股/港股分开）对总市值做横截面五等分：1=最小20%，5=最大20%。"
                  "num_value 存总市值原值（A股元/港股港元，按市场分组故不受汇率影响）",
    pit_capable=True, owner="profiling",
)
def scl_mktcap_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
    tier = assign_tier(snap["total_market_val"], 5, by=snap["market"])
    return _frame(snap["stock_code"], tier, snap["total_market_val"])


@tag(
    code="scl_float_mktcap_tier", name="流通市值档", domain=DOMAIN,
    num_unit="CNY|HKD",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="按市场分组对流通市值做横截面五等分。"
                  "口径说明：源字段 circular_market_val 是【流通市值】（剔除限售股），"
                  "严格意义的【自由流通市值】还需再剔除控股股东与管理层长期持股，本标签未做该剔除",
    pit_capable=True, owner="profiling",
)
def scl_float_mktcap_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
    tier = assign_tier(snap["circular_market_val"], 5, by=snap["market"])
    return _frame(snap["stock_code"], tier, snap["circular_market_val"])


# ═══════════════════════════════════════════════════════════════════════════
# 财报派生的体量（季频，财报驱动）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="scl_revenue_tier", name="营收规模档", domain=DOMAIN,
    num_unit="CNY|HKD",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly",
    data_sources=["financial_indicator", "stock_sector", "sector_hierarchy"],
    compute_logic="取最近一期年报 revenue，按【市场+GICS 部门】分组做横截面五等分；"
                  "营收 <= 0 或缺失不打档。num_value 存营收原值。",
    pit_capable=False,  # 财报无披露日，见 load_financial_latest 的前视风险说明
    owner="profiling",
)
def scl_revenue_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        fin = load_financial_latest(conn, as_of, "annual")
        gics_map = load_gics_map(conn)

    df = snap[["stock_code", "market"]].merge(
        fin[["stock_code", "revenue"]], on="stock_code", how="inner"
    )
    df = df.merge(gics_map, on="stock_code", how="left")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df = df[df["revenue"] > 0]

    tier = assign_tier(df["revenue"], 5, by=_market_industry_key(df, df["gics"]))
    return _frame(df["stock_code"], tier, df["revenue"])


@tag(
    code="scl_industry_mktcap_rank", name="行业内市值排名", domain=DOMAIN,
    num_unit="rank",
    value_type=TIER,
    value_range={
        "1": "行业内规模最小 20%", "2": "次小 20%", "3": "中间 20%",
        "4": "次大 20%", "5": "行业内规模最大 20%（龙头组）",
    },
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "stock_sector", "sector_hierarchy"],
    compute_logic="在【市场 + GICS 部门】内按总市值排名；key_value 为组内规模档（5=龙头组），"
                  "num_value 为行业内绝对排名（1=组内市值最大）。用于龙头识别与小市值组内比较",
    pit_capable=True, owner="profiling",
)
def scl_industry_mktcap_rank(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        gics_map = load_gics_map(conn)

    df = snap[["stock_code", "market", "total_market_val"]].merge(
        gics_map, on="stock_code", how="inner"
    )
    df["total_market_val"] = pd.to_numeric(df["total_market_val"], errors="coerce")
    df = df[df["total_market_val"] > 0]

    grp_key = _market_industry_key(df, df["gics"])
    rank = rank_within_group(df["total_market_val"], by=grp_key, ascending=False)
    tier = assign_tier(df["total_market_val"], 5, by=grp_key)
    return _frame(df["stock_code"], tier, rank)


# ═══════════════════════════════════════════════════════════════════════════
# 港股通标的
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="scl_stock_connect", name="港股通标的", domain=DOMAIN,
    value_type=BOOL,
    value_range={"true": "有南向持股记录（港股通可交易）", "false": "无南向持股记录"},
    source_type="external", update_freq="event", data_sources=["daily_ggt_hold"],
    compute_logic="取 daily_ggt_hold 中出现过的港股。仅对港股打标。"
                  "覆盖说明：该口径是「有南向持股记录」，未持股的港股通标的不会出现，"
                  "因此 false 不等于「非港股通标的」，严格名单需外采交易所官方清单",
    pit_capable=False, owner="profiling",
)
def scl_stock_connect(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        connect = load_stock_connect(conn)
        snap = load_quote_snapshot(conn, as_of)

    hk = snap[snap["market"] == "HK"]
    flag = hk["stock_code"].isin(connect)
    return _frame(
        hk["stock_code"],
        flag.map({True: "true", False: "false"}),
        flag.astype(float),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 数据源缺失（等补资产负债表后启用）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="scl_total_assets_tier", name="总资产规模档", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="取最近一期年报总资产，按【市场+行业】分组五等分",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：financial_indicator 只有利润表/现金流量表字段（revenue/net_profit/ocf/fcf/roe），"
                   "无资产负债表科目（总资产、净资产、有息负债），需扩展财报采集接口",
    pit_capable=False, owner="profiling",
)
def scl_total_assets_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")
