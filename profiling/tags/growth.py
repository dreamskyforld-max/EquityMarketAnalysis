#!/usr/bin/env python3
"""⑤ 成长特征标签域

域定义：描述公司**规模扩张的速度与方向**，回答「长得快不快、增长是否在加速」。
判定标准：来自财报的**变化率**（增速）及其二阶变化；**不含价格、不含水平值**
——水平值归 ④ 盈利质量，价格相关归 ③ 估值，这是三域的分界。

口径约定：
    · 数据统一用最近两期**标准年报**（revenue_yoy / net_profit_yoy 为财报现成字段）
    · 增速分档在【市场 + GICS 部门】内做（行业的增速中枢差异巨大：消费 5% vs 新能源 40%）
    · yoy 的极端值（小基数 +3000%）不影响分档——rank 分位对异常值免疫，num_value 保留原值
    · 全部标签 pit_capable=False（财报无披露日）

数据来源：financial_indicator（年报：revenue_yoy / net_profit_yoy / revenue）
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..registry import tag, TIER, PLANNED_NO_DATA
from ..quantile import assign_tier, load_financial_context, load_financial_history
from ._base import _conn, _frame

DOMAIN = "成长特征"

_TIER_RANGE = {
    "1": "最低 20%（组内负增长/萎缩最重）", "2": "次低 20%", "3": "中间 20%",
    "4": "次高 20%", "5": "最高 20%（组内增长最快）",
}

_PERSIST_RANGE = {
    "0": "最新一期负增长/停滞", "1": "连续 1-2 年正增长", "2": "连续 3-4 年正增长",
    "3": "连续 ≥5 年正增长（增长韧性最强）",
}

_ACCEL_RANGE = {
    "accelerating": "增速环比提升 ≥5pp",
    "steady": "增速环比变化 ±5pp 内",
    "decelerating": "增速环比放缓 ≥5pp",
}


def _load_ctx(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        return load_financial_context(conn, as_of)


def _hist(as_of: date, periods: int) -> pd.DataFrame:
    with _conn() as conn:
        return load_financial_history(conn, as_of, "annual", periods=periods)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


# ═══════════════════════════════════════════════════════════════════════════
# 增速档（一阶）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="grw_revenue_yoy_tier", name="营收增速档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly",
    data_sources=["financial_indicator", "stock_sector", "sector_hierarchy"],
    compute_logic="最近一期标准年报营收同比增速，在【市场+GICS】内五等分：1=增长最慢/萎缩最重，"
                  "5=增长最快。负增长是正常状态（与估值域不同），不单独打标。num_value 存 yoy 原值(%)",
    pit_capable=False, owner="profiling",
)
def grw_revenue_yoy_tier(as_of: date) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    df = ctx[ctx["revenue_yoy"].notna()]
    tier = assign_tier(df["revenue_yoy"], 5, by=df["grp"])
    return _frame(df["stock_code"], tier, df["revenue_yoy"])


@tag(
    code="grw_profit_yoy_tier", name="净利增速档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly",
    data_sources=["financial_indicator", "stock_sector", "sector_hierarchy"],
    compute_logic="最近一期标准年报归母净利同比增速，在【市场+GICS】内五等分。"
                  "注意：扭亏为盈公司的 yoy 可能出现 +3000% 级极端值，rank 分位对此免疫，"
                  "但筛选时建议结合 num_value 排序复核",
    pit_capable=False, owner="profiling",
)
def grw_profit_yoy_tier(as_of: date) -> pd.DataFrame:
    ctx = _load_ctx(as_of)
    df = ctx[ctx["net_profit_yoy"].notna()]
    tier = assign_tier(df["net_profit_yoy"], 5, by=df["grp"])
    return _frame(df["stock_code"], tier, df["net_profit_yoy"])


# ═══════════════════════════════════════════════════════════════════════════
# 增长的二阶特征：持续性 / 加速度
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="grw_growth_persistence", name="成长持续性", domain=DOMAIN,
    num_unit="count",
    value_type=TIER, value_range=dict(_PERSIST_RANGE),
    source_type="rule", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="从最近一期年报往前数营收同比>0 的连续期数（最多看 15 期），按期数分档。"
                  "连续 5 年正增长的公司远比单年高增长稀有",
    pit_capable=False, owner="profiling",
)
def grw_growth_persistence(as_of: date) -> pd.DataFrame:
    hist = _hist(as_of, 15)
    g = hist.sort_values(["stock_code", "report_date"], ascending=[True, False]).copy()
    g["hit"] = (_num(g["revenue_yoy"]) > 0).astype(int)   # yoy 缺失视为中断
    g["prod"] = g.groupby("stock_code")["hit"].cumprod()
    # 连续期数 = cumprod 序列的和（latest 行的 cumprod 只等于它自己，直接取会永远 ≤1）
    g["consec"] = g.groupby("stock_code")["prod"].transform("sum")
    latest = g[g["rn"] == 0][["stock_code", "consec", "revenue_yoy"]]
    latest = latest[latest["revenue_yoy"].notna()]        # 最新期 yoy 缺失 ≠ 负增长，不打标

    def bucket(n):
        if n <= 0:
            return "0"
        if n <= 2:
            return "1"
        if n <= 4:
            return "2"
        return "3"

    latest = latest.copy()
    latest["key_value"] = latest["consec"].map(bucket)
    return _frame(latest["stock_code"], latest["key_value"], latest["consec"])


@tag(
    code="grw_growth_accel", name="成长加速度", domain=DOMAIN,
    num_unit="pp",
    value_type=TIER, value_range=dict(_ACCEL_RANGE),
    source_type="rule", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="加速度 = 最新年报营收yoy − 上一年报营收yoy（单位 pp）。"
                  "≥+5pp 记 accelerating，≤−5pp 记 decelerating，其余 steady。"
                  "增速的拐点往往比增速本身更有信息量",
    pit_capable=False, owner="profiling",
)
def grw_growth_accel(as_of: date) -> pd.DataFrame:
    hist = _hist(as_of, 2)
    cur = hist[hist["rn"] == 0][["stock_code", "revenue_yoy"]].rename(columns={"revenue_yoy": "cur"})
    prev = hist[hist["rn"] == 1][["stock_code", "revenue_yoy"]].rename(columns={"revenue_yoy": "prev"})
    df = cur.merge(prev, on="stock_code")
    df = df[df["cur"].notna() & df["prev"].notna()]
    df["delta"] = _num(df["cur"]) - _num(df["prev"])

    def key(d):
        if d >= 5:
            return "accelerating"
        if d <= -5:
            return "decelerating"
        return "steady"

    df = df.copy()
    df["key_value"] = df["delta"].map(key)
    return _frame(df["stock_code"], df["key_value"], df["delta"].round(2))


# ═══════════════════════════════════════════════════════════════════════════
# 数据源缺失
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="grw_non_gaap_yoy_tier", name="扣非增速档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="扣非净利润同比增速。与净利增速并存：两者背离说明利润含大量非经常性损益（卖资产/补贴）",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：financial_indicator 无扣非净利润科目，"
                   "需扩展财报采集（AKShare 扣非净利润字段或东财财务摘要）",
    pit_capable=False, owner="profiling",
)
def grw_non_gaap_yoy_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="grw_rd_intensity_tier", name="研发强度档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="研发费用率 = 研发费用 / 营收，衡量增长的质量与可持续性（内生 vs 堆资源）",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：financial_indicator 无研发费用科目，需扩展财报采集",
    pit_capable=False, owner="profiling",
)
def grw_rd_intensity_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="grw_capex_intensity_tier", name="资本开支强度档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="资本开支强度 = 购建固定资产等支付的现金 / 营收，高资本开支=重资产扩张模式",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：无购建固定资产支付的现金科目（现金流量表投资活动明细），"
                   "当前 FCF 由 OCF 推算不含 CAPEX 分解",
    pit_capable=False, owner="profiling",
)
def grw_capex_intensity_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="grw_source", name="成长来源", domain=DOMAIN,
    value_type=TIER, value_range={"organic": "内生增长", "ma": "并购驱动", "mixed": "混合"},
    source_type="rule", update_freq="quarterly", data_sources=["financial_indicator"],
    compute_logic="内生增长 = 收入增长主要由现有业务驱动；并购驱动 = 收入跳变与并购公告同步",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：需要并购/商誉变动数据（商誉科目缺失），"
                   "且判定逻辑需公告流解析，建议与 ⑪ 事件风险域一并建设",
    pit_capable=False, owner="profiling",
)
def grw_source(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")
