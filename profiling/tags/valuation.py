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
    a_daily_quote / hk_daily_quote  pe_ttm_ratio / pb_ratio / 市值（外部源直供，日采必写）
    financial_indicator             free_cash_flow（年报）、revenue / operating_cash_flow（滚动12个月）

PS/PCF 为什么不在行情表取：
    两列曾作为派生值落行情表，由多个生产者（东财直采 + 本地推算 + 采集钩子）分别写入，
    港股那一路把累计口径的 Q1/H1/Q3/annual 直接 SUM（分母虚高约 2.4 倍），且日常链路填不满。
    现在统一：分子 = 行情表 total_market_val，分母 = quantile.load_revenue_ttm（TTM 唯一实现），
    两市同源、无需区分，也不再依赖任何回填作业。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..registry import tag, TIER, PLANNED_NO_DATA
from ..quantile import (
    QUOTE_TABLES, load_quote_snapshot, load_financial_latest, load_industry_map,
    assign_tier, historical_percentile, tier_or_flag, load_financial_ttm,
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


def _val_ps_ttm(as_of: date, level: int) -> pd.DataFrame:
    """PS(TTM) 行业分档内部实现（level=1 一级 / 2 二级，D2 一/二级并存）。"""
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        ind_map = load_industry_map(conn, level)
        ttm = load_financial_ttm(conn, as_of)

    df = snap[["stock_code", "market", "total_market_val"]].copy()
    df["total_market_val"] = pd.to_numeric(df["total_market_val"], errors="coerce")
    df = df.merge(ttm[["stock_code", "rev_ttm"]], on="stock_code", how="inner")
    df = df.merge(ind_map[["stock_code", "industry_code"]], on="stock_code", how="inner")
    df = df[(df["total_market_val"] > 0) & df["rev_ttm"].notna() & (df["rev_ttm"] != 0)]

    ps = df["total_market_val"] / df["rev_ttm"]
    grp = df["market"].astype(str) + "|" + df["industry_code"].astype(str)
    tier = tier_or_flag(ps, by=grp, flag="NEGATIVE")
    return _frame(df["stock_code"], tier, ps.round(4))


_PS_COMMON = dict(
    domain=DOMAIN, num_unit="x", value_type=TIER,
    value_range={**_TIER_RANGE, "NEGATIVE": "营收为负或无效，不参与分档"},
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "financial_indicator",
                  "regime.stock_sw_industry_mapping", "regime.stock_hsics_industry_mapping"],
    pit_capable=False, owner="profiling",
)


@tag(code="val_ps_ttm_ind1_tier", name="PS(TTM)一级行业档", compute_logic=
     "PS=总市值/滚动12月营收，在【市场+申万/HSICS 一级行业】内横截面五等分；非正值 NEGATIVE。"
     "num_value 存 PS 原值。分组来自阶段 B 统一映射表，层级关系从分类表 parent 链 join",
     **_PS_COMMON)
def val_ps_ttm_ind1_tier(as_of: date) -> pd.DataFrame:
    return _val_ps_ttm(as_of, 1)


@tag(code="val_ps_ttm_ind2_tier", name="PS(TTM)二级行业档", compute_logic=
     "PS=总市值/滚动12月营收，在【市场+申万/HSICS 二级行业】内横截面五等分；非正值 NEGATIVE。"
     "num_value 存 PS 原值。分组来自阶段 B 统一映射表，层级关系从分类表 parent 链 join",
     **_PS_COMMON)
def val_ps_ttm_ind2_tier(as_of: date) -> pd.DataFrame:
    return _val_ps_ttm(as_of, 2)


def _val_pcf_ttm(as_of: date, level: int) -> pd.DataFrame:
    """PCF(TTM) 行业分档内部实现（level=1/2）。"""
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        ind_map = load_industry_map(conn, level)
        ttm = load_financial_ttm(conn, as_of)

    df = snap[["stock_code", "market", "total_market_val"]].copy()
    df["total_market_val"] = pd.to_numeric(df["total_market_val"], errors="coerce")
    df = df.merge(ttm[["stock_code", "ocf_ttm"]], on="stock_code", how="inner")
    df = df.merge(ind_map[["stock_code", "industry_code"]], on="stock_code", how="inner")
    df = df[(df["total_market_val"] > 0) & df["ocf_ttm"].notna() & (df["ocf_ttm"] != 0)]

    pcf = df["total_market_val"] / df["ocf_ttm"]
    grp = df["market"].astype(str) + "|" + df["industry_code"].astype(str)
    tier = tier_or_flag(pcf, by=grp, flag="NEGATIVE")
    return _frame(df["stock_code"], tier, pcf.round(4))


_PCF_COMMON = dict(
    domain=DOMAIN, num_unit="x", value_type=TIER,
    value_range={**_TIER_RANGE, "NEGATIVE": "经营现金流为负，不参与分档"},
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "financial_indicator",
                  "regime.stock_sw_industry_mapping", "regime.stock_hsics_industry_mapping"],
    pit_capable=False, owner="profiling",
)


@tag(code="val_pcf_ttm_ind1_tier", name="PCF(TTM)一级行业档", compute_logic=
     "PCF=总市值/滚动12月经营现金流，在【市场+申万/HSICS 一级行业】内横截面五等分；"
     "非正值 NEGATIVE。num_value 存 PCF 原值。分组来自阶段 B 统一映射表",
     **_PCF_COMMON)
def val_pcf_ttm_ind1_tier(as_of: date) -> pd.DataFrame:
    return _val_pcf_ttm(as_of, 1)


@tag(code="val_pcf_ttm_ind2_tier", name="PCF(TTM)二级行业档", compute_logic=
     "PCF=总市值/滚动12月经营现金流，在【市场+申万/HSICS 二级行业】内横截面五等分；"
     "非正值 NEGATIVE。num_value 存 PCF 原值。分组来自阶段 B 统一映射表",
     **_PCF_COMMON)
def val_pcf_ttm_ind2_tier(as_of: date) -> pd.DataFrame:
    return _val_pcf_ttm(as_of, 2)


def _val_peg(as_of: date, level: int) -> pd.DataFrame:
    """PEG(TTM) 行业分档内部实现（level=1/2）。"""
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        ind_map = load_industry_map(conn, level)
        ttm = load_financial_ttm(conn, as_of)

    df = snap[["stock_code", "market", "pe_ttm"]].copy()
    df["pe_ttm"] = pd.to_numeric(df["pe_ttm"], errors="coerce")
    df = df.merge(ttm[["stock_code", "np_ttm", "np_ttm_prev", "np_ttm_yoy"]],
                  on="stock_code", how="inner")
    df = df.merge(ind_map[["stock_code", "industry_code"]], on="stock_code", how="inner")
    df = df[df["pe_ttm"].notna()]

    pe = df["pe_ttm"]
    yoy = df["np_ttm_yoy"]
    prev_np = pd.to_numeric(df["np_ttm_prev"], errors="coerce")
    np_now = pd.to_numeric(df["np_ttm"], errors="coerce")

    peg = pe / yoy.where(yoy != 0)

    key = pd.Series(pd.NA, index=df.index, dtype="object")
    loss = pe <= 0
    turn = (~loss) & (prev_np <= 0) & (np_now > 0)
    decline = (~loss) & (~turn) & (yoy <= 0)
    key[loss] = "LOSS"
    key[turn] = "TURNAROUND"
    key[decline] = "DECLINE"

    pos = (~loss) & (~turn) & (yoy > 0)
    if pos.any():
        grp = df.loc[pos, "market"].astype(str) + "|" + df.loc[pos, "industry_code"].astype(str)
        key[pos] = assign_tier(peg[pos], 5, by=grp)

    return _frame(df["stock_code"], key, peg.round(4))


_PEG_COMMON = dict(
    domain=DOMAIN, num_unit="x", value_type=TIER,
    value_range={
        **_TIER_RANGE,
        "LOSS": "PE≤0（亏损），PEG 无意义",
        "DECLINE": "净利 TTM 同比 ≤0（盈利下滑），PEG 为负/无意义",
        "TURNAROUND": "扭亏（上年同期 TTM 净利≤0），增速无定义",
    },
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "financial_indicator",
                  "regime.stock_sw_industry_mapping", "regime.stock_hsics_industry_mapping"],
    pit_capable=False, owner="profiling",
)


@tag(code="val_peg_ind1_tier", name="PEG(TTM)一级行业档", compute_logic=
     "PEG=PE(TTM)÷净利TTM同比增速(%)，在【市场+申万/HSICS 一级行业】内横截面五等分；"
     "增速按百分数数值代入；PE≤0 打 LOSS、增速≤0 打 DECLINE、扭亏打 TURNAROUND。"
     "num_value 存 PEG 原值。历史(trailing)口径，非一致预期",
     **_PEG_COMMON)
def val_peg_ind1_tier(as_of: date) -> pd.DataFrame:
    return _val_peg(as_of, 1)


@tag(code="val_peg_ind2_tier", name="PEG(TTM)二级行业档", compute_logic=
     "PEG=PE(TTM)÷净利TTM同比增速(%)，在【市场+申万/HSICS 二级行业】内横截面五等分；"
     "增速按百分数数值代入；PE≤0 打 LOSS、增速≤0 打 DECLINE、扭亏打 TURNAROUND。"
     "num_value 存 PEG 原值。历史(trailing)口径，非一致预期",
     **_PEG_COMMON)
def val_peg_ind2_tier(as_of: date) -> pd.DataFrame:
    return _val_peg(as_of, 2)


# ═══════════════════════════════════════════════════════════════════════════
# 双分位：行业分位 + 历史分位
# ═══════════════════════════════════════════════════════════════════════════


def _ind_pct(as_of: date, level: int, value_col: str) -> pd.DataFrame:
    """行业横截面百分位分档内部实现（PE/PB 共用；level=1/2）。

    分组来自阶段 B 统一映射表（申万/HSICS），一/二级从分类表 parent 链 join。
    num_value 为百分位 0-100（这类标签本就是分位标签，与 PS/PCF/PEG 的原值语义不同）。
    """
    with _conn() as conn:
        snap = load_quote_snapshot(conn, as_of)
        ind_map = load_industry_map(conn, level)

    df = snap[["stock_code", "market", value_col]].merge(
        ind_map[["stock_code", "industry_code"]], on="stock_code", how="inner")
    df = df[df[value_col] > 0]
    if df.empty:
        return _frame([], [])

    grp = df["market"].astype(str) + "|" + df["industry_code"].astype(str)
    # D20 分层样本门禁（仅二级：申万二级 41% 组正 PE<15，一级样本充足不启用）
    #   ≤5 只不产出（1 只股票自己排 100% 无意义）；6-14 只 conf=0.5；≥15 只 conf=1.0
    confs = None
    if level == 2:
        grp_size = df.groupby(grp)[value_col].transform("size")
        df = df[grp_size > 5]
        if df.empty:
            return _frame([], [])
        grp = grp.loc[df.index]
        confs = grp_size.loc[df.index].ge(15).map({True: 1.0, False: 0.5})

    pct = df[value_col].groupby(grp).rank(pct=True, method="average") * 100.0
    return _frame(df["stock_code"], _pct_to_tier(pct), pct.round(2), confs=confs)


_PCT_COMMON = dict(
    domain=DOMAIN, num_unit="percentile_0_100",
    value_type=TIER, value_range=dict(_PCT_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote",
                  "regime.stock_sw_industry_mapping", "regime.stock_hsics_industry_mapping"],
    pit_capable=True, owner="profiling",
)


@tag(code="val_pe_ind1_pct", name="PE一级行业分位", compute_logic=
     "在【市场+申万/HSICS 一级行业】内对 PE(TTM) 算横截面百分位并切五档；"
     "num_value 存百分位；仅正 PE 参与。分组来自阶段 B 统一映射表",
     **_PCT_COMMON)
def val_pe_ind1_pct(as_of: date) -> pd.DataFrame:
    return _ind_pct(as_of, 1, "pe_ttm")


@tag(code="val_pe_ind2_pct", name="PE二级行业分位", compute_logic=
     "在【市场+申万/HSICS 二级行业】内对 PE(TTM) 算横截面百分位并切五档；"
     "num_value 存百分位；仅正 PE 参与。分组来自阶段 B 统一映射表（本 code 由 GICS 口径复用改口径）",
     **_PCT_COMMON)
def val_pe_ind2_pct(as_of: date) -> pd.DataFrame:
    return _ind_pct(as_of, 2, "pe_ttm")


@tag(code="val_pb_ind1_pct", name="PB一级行业分位", compute_logic=
     "在【市场+申万/HSICS 一级行业】内对 PB 算横截面百分位并切五档；"
     "num_value 存百分位；仅正 PB 参与。分组来自阶段 B 统一映射表",
     **_PCT_COMMON)
def val_pb_ind1_pct(as_of: date) -> pd.DataFrame:
    return _ind_pct(as_of, 1, "pb")


@tag(code="val_pb_ind2_pct", name="PB二级行业分位", compute_logic=
     "在【市场+申万/HSICS 二级行业】内对 PB 算横截面百分位并切五档；"
     "num_value 存百分位；仅正 PB 参与。分组来自阶段 B 统一映射表",
     **_PCT_COMMON)
def val_pb_ind2_pct(as_of: date) -> pd.DataFrame:
    return _ind_pct(as_of, 2, "pb")


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
