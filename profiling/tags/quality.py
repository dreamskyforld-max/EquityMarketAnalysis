#!/usr/bin/env python3
"""④ 盈利质量标签域

域定义：描述**公司本身的经营品质**，回答「这是不是一门好生意」。
判定标准：**全部来自财报三表，不含价格**，且描述的是**水平/比率**（非变化率）
——这是它与 ③ 估值（含价格）、⑤ 成长（变化率）的双重分界。

口径约定：
    · 水平/比率类取每只票**最近一期标准报告期**（有季报用季报、无则年报，非标报告期已过滤）；
      分档在【市场 + 申万/HSICS 行业 + 报告期类型】内做（阶段 C 一级/二级并存）——Q3 累计 9 个月与 annual 全年不混排
    · `qal_profit_years`（连续盈利年数）保持年报口径，语义是「年」不改成「期」
    · 档位统一规则 1=数值最小 5=数值最大，好坏方向看语义（label 已注明）
    · 全部标签 pit_capable=False：财报无实际披露日，存在前视（见 load_financial_latest）

数据来源：financial_indicator（自适应标准报告期：ROE/毛利率/净利率/负债率/OCF/净利润）
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..registry import tag, TIER, PLANNED_NO_DATA
from ..quantile import (
    assign_tier, load_financial_context, load_financial_history, tier_or_flag,
)
from ._base import _conn, _frame

DOMAIN = "盈利质量"

_TIER_RANGE = {
    "1": "最低 20%", "2": "次低 20%", "3": "中间 20%", "4": "次高 20%", "5": "最高 20%",
}

_PROFIT_YEARS_RANGE = {
    "0": "最近一期年报亏损", "1": "连续盈利 <3 年", "2": "连续盈利 3-5 年",
    "3": "连续盈利 6-10 年", "4": "连续盈利 >10 年（盈利韧性最强）",
}


def _load_ctx(as_of: date) -> pd.DataFrame:
    """④⑤ 水平/比率类底表：自适应最近一期，grp 已含报告期类型。"""
    with _conn() as conn:
        return load_financial_context(conn, as_of, "auto")


def _hist(as_of: date, periods: int) -> pd.DataFrame:
    """同类型报告期序列：ROE 稳定性用（近 3 期都已是非年报或都是年报）。"""
    with _conn() as conn:
        return load_financial_history(conn, as_of, "auto", periods=periods)


def _hist_annual(as_of: date, periods: int) -> pd.DataFrame:
    """年报序列：连续盈利年数专用，保持 years 语义。"""
    with _conn() as conn:
        return load_financial_history(conn, as_of, "annual", periods=periods)


def _streak(hist: pd.DataFrame, field: str) -> pd.Series:
    """从最新期往前数连续为正的期数。

    hit∈{0,1}，组内前缀乘积 cumprod 一旦遇到 0 后面全归零；
    **cumprod 序列的和** = 第一个 0 之前连续 1 的个数 = 连续期数。
    （latest 行的 cumprod 只等于它自己，直接取会永远 ≤1——实测踩过）

    报告缺失（NaN>0 为 False）视为中断。
    """
    g = hist.sort_values(["stock_code", "report_date"], ascending=[True, False]).copy()
    g["hit"] = (pd.to_numeric(g[field], errors="coerce") > 0).astype(int)
    g["prod"] = g.groupby("stock_code")["hit"].cumprod()
    g["consec"] = g.groupby("stock_code")["prod"].transform("sum")
    latest = g[g["rn"] == 0]
    return latest.set_index("stock_code")["consec"]


def _bucket_years(n: int) -> str:
    if n == 0:
        return "0"
    if n < 3:
        return "1"
    if n <= 5:
        return "2"
    if n <= 10:
        return "3"
    return "4"


# ═══════════════════════════════════════════════════════════════════════════
# 行业标签（阶段 C：一级/二级并存，D2/D16）
#   内部函数统一签名 (as_of, level)，分组取 load_financial_context 的 grp_l1/grp_l2；
#   公开标签由 _register_industry_pairs 工厂注册，避免 6×2 份重复实现。
# ═══════════════════════════════════════════════════════════════════════════


def _grp(df: pd.DataFrame, level: int) -> pd.Series:
    """指定层级的分组键（D16 甲：load_financial_context 一次产出 grp_l1/grp_l2）。"""
    return df[f"grp_l{level}"]


def _qal_roe(as_of: date, level: int) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    df = ctx[ctx["roe"].notna()]
    return _frame(df["stock_code"],
                  tier_or_flag(df["roe"], by=_grp(df, level), flag="LOSS"),
                  df["roe"])


def _qal_gross_margin(as_of: date, level: int) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    df = ctx[ctx["gross_profit_rate"].notna()]
    return _frame(df["stock_code"],
                  tier_or_flag(df["gross_profit_rate"], by=_grp(df, level), flag="LOSS"),
                  df["gross_profit_rate"])


def _qal_net_margin(as_of: date, level: int) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    df = ctx[ctx["net_profit_rate"].notna()]
    return _frame(df["stock_code"],
                  tier_or_flag(df["net_profit_rate"], by=_grp(df, level), flag="LOSS"),
                  df["net_profit_rate"])


def _qal_leverage(as_of: date, level: int) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    df = ctx[ctx["debt_ratio"].notna()]
    tier = assign_tier(df["debt_ratio"], 5, by=_grp(df, level))
    return _frame(df["stock_code"], tier, df["debt_ratio"])


def _qal_roe_stability(as_of: date, level: int) -> pd.DataFrame:
    g = _hist(as_of, 3)
    g = g[g["rn"] <= 2]
    agg = g.groupby("stock_code")["roe"].agg(cnt="count", std="std").reset_index()
    agg = agg[agg["cnt"] == 3]
    agg["std"] = pd.to_numeric(agg["std"], errors="coerce")
    ctx = _load_ctx(as_of)[["stock_code", f"grp_l{level}"]].rename(
        columns={f"grp_l{level}": "grp"})
    df = agg.merge(ctx, on="stock_code", how="inner")
    tier = assign_tier(df["std"], 5, by=df["grp"])
    return _frame(df["stock_code"], tier, df["std"].round(4))


def _qal_earnings_quality(as_of: date, level: int) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    np_ = pd.to_numeric(ctx["net_profit"], errors="coerce")
    ocf = pd.to_numeric(ctx["operating_cash_flow"], errors="coerce")
    ok = ctx[(np_ > 0) & ocf.notna()]
    ratio = (pd.to_numeric(ok["operating_cash_flow"], errors="coerce")
             / pd.to_numeric(ok["net_profit"], errors="coerce") * 100.0)
    tier_ok = assign_tier(ratio, 5, by=_grp(ok, level))
    loss = ctx[(np_ <= 0) & np_.notna()]
    tier = pd.concat([
        pd.DataFrame({"stock_code": ok["stock_code"], "key_value": tier_ok.values,
                      "num_value": ratio.values, "confidence": 1.0}),
        pd.DataFrame({"stock_code": loss["stock_code"], "key_value": "LOSS",
                      "num_value": None, "confidence": 1.0}),
    ], ignore_index=True)
    return _frame(tier["stock_code"], tier["key_value"], tier["num_value"])


_QAL_DATA = ["financial_indicator", "regime.stock_sw_industry_mapping",
             "regime.stock_hsics_industry_mapping"]
_QAL_IND_SPECS = (
    # (内部函数, 一级code, 一级中文名, 二级code, 二级中文名, num_unit, value_range, compute_logic)
    (_qal_roe, "qal_roe_ind1_tier", "ROE一级行业档", "qal_roe_ind2_tier", "ROE二级行业档",
     "pct", {**_TIER_RANGE, "LOSS": "ROE 为负（当年亏损），不参与分档"},
     "最近一期标准报告期加权 ROE，在【市场+申万/HSICS 行业+报告期类型】内五等分；num_value 存 ROE 原值(%)"),
    (_qal_gross_margin, "qal_gross_margin_ind1_tier", "毛利率一级行业档",
     "qal_gross_margin_ind2_tier", "毛利率二级行业档",
     "pct", {**_TIER_RANGE, "LOSS": "毛利率为负（毛亏），不参与分档"},
     "最近一期毛利率，在【市场+行业+报告期类型】内五等分；毛利率行业属性极强，必须行业内比较"),
    (_qal_net_margin, "qal_net_margin_ind1_tier", "净利率一级行业档",
     "qal_net_margin_ind2_tier", "净利率二级行业档",
     "pct", {**_TIER_RANGE, "LOSS": "净利率为负（亏损），不参与分档"},
     "最近一期净利率，在【市场+行业+报告期类型】内五等分；num_value 存净利率原值(%)"),
    (_qal_leverage, "qal_leverage_ind1_tier", "资产负债率一级行业档",
     "qal_leverage_ind2_tier", "资产负债率二级行业档",
     "pct", {**_TIER_RANGE, "1": "负债率最低 20%（组内最稳健）", "5": "负债率最高 20%（组内负债最重）"},
     "最近一期资产负债率，在【市场+行业+报告期类型】内五等分；1=负债率最低、5=最高"),
    (_qal_roe_stability, "qal_roe_stability_ind1", "ROE波动程度一级行业档",
     "qal_roe_stability_ind2", "ROE波动程度二级行业档",
     "pct", {"1": "波动最小 20%（盈利最稳定）", "2": "次稳 20%", "3": "中间 20%",
             "4": "次不稳定 20%", "5": "波动最大 20%"},
     "最近 3 期同类型报告期 ROE 标准差，在【市场+行业+报告期类型】内五等分；1=波动最小、5=最大"),
    (_qal_earnings_quality, "qal_earnings_quality_ind1", "利润现金转换率一级行业档",
     "qal_earnings_quality_ind2", "利润现金转换率二级行业档",
     "pct", {**_TIER_RANGE, "1": "OCF/净利 最低 20%（纸面利润风险最高）",
             "5": "最高 20%（利润有充足现金流支撑）", "LOSS": "当年亏损，无法计算利润现金转换率"},
     "经营现金流/净利润×100，在【市场+行业+报告期类型】内五等分；净利>0 才有意义"),
)


def _register_industry_pairs() -> None:
    """工厂注册：每个内部函数按 level=1/2 各注册一个标签（导入即生效）。"""
    def _make(fn, level):
        def wrapper(as_of: date) -> pd.DataFrame:
            return fn(as_of, level)
        wrapper.__name__ = f"{fn.__name__}_l{level}"
        return wrapper

    for fn, c1, n1, c2, n2, unit, vrange, logic in _QAL_IND_SPECS:
        for code, name, level in ((c1, n1, 1), (c2, n2, 2)):
            tag(code=code, name=name, domain=DOMAIN, num_unit=unit,
                value_type=TIER, value_range=dict(vrange),
                source_type="stat", update_freq="quarterly",
                data_sources=list(_QAL_DATA),
                compute_logic=logic + f"（{'一级' if level == 1 else '二级'}行业分组，阶段 B 统一映射表）",
                version="v2", pit_capable=False, owner="profiling")(_make(fn, level))


_register_industry_pairs()


@tag(
    code="qal_profit_years", name="连续盈利年数", domain=DOMAIN,
    num_unit="count",
    value_type=TIER, value_range=dict(_PROFIT_YEARS_RANGE),
    source_type="rule", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="从最近一期年报往前数净利润>0 的连续期数（最多看 15 期年报），按期数分档。"
                  "注：按「期数」而非自然年计——个别年份报告缺失时，连续性判断按期近似",
    pit_capable=False, owner="profiling",
)
def qal_profit_years(as_of: date) -> pd.DataFrame:
    streak = _streak(_hist_annual(as_of, 15), "net_profit")
    latest = streak.rename("streak").reset_index()
    return _frame(latest["stock_code"], latest["streak"].map(_bucket_years), latest["streak"])


# ═══════════════════════════════════════════════════════════════════════════
# 数据源缺失
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="qal_roic_tier", name="ROIC档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="ROIC = NOPAT / 投入资本（有息负债+净资产），比 ROE 更能剔除杠杆的粉饰",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：ROIC 需要 EBIT（利息/税）与投入资本（有息负债+净资产），"
                   "financial_indicator 无利润表利息科目、无资产负债表，需扩展财报采集",
    pit_capable=False, owner="profiling",
)
def qal_roic_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="qal_asset_efficiency_tier", name="资产效率档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="总资产周转率 = 营收 / 平均总资产，衡量资产的变现效率",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：无资产负债表（总资产），无法计算周转率",
    pit_capable=False, owner="profiling",
)
def qal_asset_efficiency_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="qal_wc_anomaly", name="营运资本异常", domain=DOMAIN,
    value_type=TIER, value_range={"abnormal": "存货/应收周转显著恶化", "normal": "正常"},
    source_type="rule", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="存货/应收账款周转天数连续恶化 + 与营收增长背离 → 财务粉饰的常见前兆信号",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：无存货、应收账款科目（资产负债表），无法计算周转恶化",
    pit_capable=False, owner="profiling",
)
def qal_wc_anomaly(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")
