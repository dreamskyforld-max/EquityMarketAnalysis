#!/usr/bin/env python3
"""⑨ 资金关注度标签域

域定义：描述**谁在买卖、资金往哪流**，回答「市场关注度与筹码动向」。
判定标准：资金流向、持仓变化、杠杆与卖空行为——与 ⑦ 交易特征（纯量价统计）
的区别在于本域关注**资金主体行为**（融资盘/南向/沽空方），而非价格形态本身。

数据现状（决定了本域能做什么）：
    daily_margin_balance  两融明细，A 股 4,597 只，2010 年起 → 最完整的资金数据，本域主力
    daily_short_selling   港股沽空，869 只，2026-06 起（约 3 个月）→ 覆盖有限，标注限制
    daily_ggt_hold        南向持股，627 只，2026-04 起（约 4.5 个月）→ 覆盖有限，标注限制
    fund_flow_daily       主力资金流，仅 80 只（活跃采集池）→ 覆盖过低，不建标签
    daily_northbound_flow 市场整体北向流水，无个股维度 → 无法建个股标签

覆盖限制统一说明：港股两个标签（沽空/南向）样本量远小于全市场，且历史很短，
**只能反映近 3-5 个月的状态**，不能用于长周期回测；两融标签仅覆盖 A 股。

口径约定：
    · 两融与沽空都用「相对流通市值/成交额」的比率，避免绝对额受市值规模支配
    · 趋势类用固定百分比阈值（±5%）而非分位，因为「上升/下降」有绝对方向语义
    · 窗口：20 交易日≈1 月（资金行为的常用观察窗），60 日用于成交量基准
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ..registry import tag, TIER, ENUM, PLANNED_NO_DATA
from ..quantile import assign_tier, load_quote_snapshot
from ._base import _conn, _read_sql, _frame

DOMAIN = "资金关注度"

W_1M, W_BASE = 20, 60
_TREND_THRESHOLD = 5.0      # 趋势判定阈值（%）
_TIER_RANGE = {
    "1": "最低 20%", "2": "次低 20%", "3": "中间 20%", "4": "次高 20%", "5": "最高 20%",
}
_TREND_RANGE = {"rise": "上升（≥+5%）", "flat": "基本持平（±5% 内）", "fall": "下降（≤−5%）"}


def _trend_key(delta_pct: float) -> str:
    if delta_pct >= _TREND_THRESHOLD:
        return "rise"
    if delta_pct <= -_TREND_THRESHOLD:
        return "fall"
    return "flat"


# ═══════════════════════════════════════════════════════════════════════════
# A 股：两融（融资余额水平 / 净买入强度 / 余额趋势）
# ═══════════════════════════════════════════════════════════════════════════


def _load_margin(conn, as_of: date) -> pd.DataFrame:
    """两融：最新融资余额、近 20 日净买入合计、20 日前余额。"""
    return _read_sql(
        conn,
        f"""
        WITH recent AS (
            SELECT stock_code, trade_date, rz_balance::float8 AS rz_balance, rz_net::float8 AS rz_net,
                   ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
            FROM daily_margin_balance
            WHERE trade_date <= %s AND trade_date > %s
        )
        SELECT stock_code,
               MAX(CASE WHEN rn = 1 THEN rz_balance END)  AS cur_balance,
               SUM(CASE WHEN rn <= {W_1M} THEN rz_net END) AS net_1m,
               MAX(CASE WHEN rn = {W_1M} THEN rz_balance END) AS balance_1m_ago
        FROM recent GROUP BY stock_code
        """,
        (as_of, as_of - timedelta(days=45)),
    )


@tag(
    code="att_margin_balance_tier", name="融资余额水平档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["daily_margin_balance", "a_daily_quote"],
    compute_logic="融资余额 / 流通市值 ×100（杠杆资金占流通盘比重），A 股市场内五等分："
                  "1=杠杆资金占比最低，5=最高（融资盘最密集，波动放大器）。仅覆盖 A 股",
    pit_capable=True, owner="profiling",
)
def att_margin_balance_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        mg = _load_margin(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    df = mg.merge(snap[["stock_code", "market", "circular_market_val"]], on="stock_code", how="inner")
    df["ratio"] = df["cur_balance"] / df["circular_market_val"] * 100.0
    df = df[df["ratio"].notna()]
    tier = assign_tier(df["ratio"], 5, by=df["market"])
    return _frame(df["stock_code"], tier, df["ratio"].round(4))


@tag(
    code="att_margin_net_tier", name="融资净买入档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={
        "1": "近 1 月融资净偿还最多 20%（杠杆资金撤离）", "2": "次低", "3": "中间", "4": "次高",
        "5": "近 1 月融资净买入最多 20%（杠杆资金流入）",
    },
    source_type="stat", update_freq="daily",
    data_sources=["daily_margin_balance", "a_daily_quote"],
    compute_logic="近 20 个交易日融资净买入合计 / 流通市值 ×100，A 股市场内五等分。"
                  "比余额水平更能反映**近期**杠杆资金的态度变化（余额是存量，净买入是流量）",
    pit_capable=True, owner="profiling",
)
def att_margin_net_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        mg = _load_margin(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    df = mg.merge(snap[["stock_code", "market", "circular_market_val"]], on="stock_code", how="inner")
    df["ratio"] = df["net_1m"] / df["circular_market_val"] * 100.0
    df = df[df["ratio"].notna()]
    tier = assign_tier(df["ratio"], 5, by=df["market"])
    return _frame(df["stock_code"], tier, df["ratio"].round(4))


@tag(
    code="att_margin_trend", name="融资余额趋势", domain=DOMAIN,
    num_unit="pct",
    value_type=ENUM,
    enum_type="trend",
    enum_values={
        "rise": ("上升（≥+5%）", None),
        "flat": ("基本持平（±5% 内）", None),
        "fall": ("下降（≤−5%）", None),
    },
    source_type="rule", update_freq="daily", data_sources=["daily_margin_balance"],
    compute_logic="最新融资余额相对 20 个交易日前的变动率：≥+5% 记 rise，≤−5% 记 fall，其余 flat。"
                  "用固定阈值而非分位——「上升/下降」有绝对方向语义。num_value 存变动率(%)",
    pit_capable=True, owner="profiling",
)
def att_margin_trend(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        mg = _load_margin(conn, as_of)
    df = mg[(mg["cur_balance"].notna()) & (mg["balance_1m_ago"].notna()) & (mg["balance_1m_ago"] > 0)]
    chg = (df["cur_balance"] / df["balance_1m_ago"] - 1) * 100.0
    return _frame(df["stock_code"], chg.map(_trend_key), chg.round(3))


# ═══════════════════════════════════════════════════════════════════════════
# 港股：沽空比例 / 南向持股
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="att_short_selling_tier", name="沽空比例档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={
        "1": "沽空占比最低 20%", "2": "次低", "3": "中间", "4": "次高",
        "5": "沽空占比最高 20%（空头压力最大）",
    },
    source_type="stat", update_freq="daily",
    data_sources=["daily_short_selling", "hk_daily_quote"],
    compute_logic="近 20 个交易日沽空金额合计 / 同期成交额合计 ×100，港股市场内五等分。"
                  "高沽空占比既可能是空头押注，也可能含对冲/做市成分，需结合其他标签判断。"
                  "覆盖限制：daily_short_selling 仅 869 只且自 2026-06 起，不可用于长周期回测",
    pit_capable=True, owner="profiling",
)
def att_short_selling_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        # short_selling_amt 单位是「亿港元」（实测：HK.06881 沽空 287.6 万股 ≈ 0.23 亿港元），
        # 而 hk_daily_quote.amount 是「元」，需 ×1e8 对齐，否则沽空占比全部趋近 0
        ss = _read_sql(
            conn,
            "SELECT stock_code, SUM(short_selling_amt)::float8 * 1e8 AS ss_amt "
            "FROM daily_short_selling WHERE trade_date <= %s AND trade_date > %s GROUP BY stock_code",
            (as_of, as_of - timedelta(days=45)),
        )
        q = _read_sql(
            conn,
            "SELECT stock_code, SUM(amount::float8) amt FROM hk_daily_quote "
            "WHERE trade_date <= %s AND trade_date > %s GROUP BY stock_code",
            (as_of, as_of - timedelta(days=45)),
        )
        snap = load_quote_snapshot(conn, as_of)
    df = ss.merge(q, on="stock_code", how="inner").merge(
        snap[["stock_code", "market"]], on="stock_code", how="inner"
    )
    df["ratio"] = df["ss_amt"] / df["amt"] * 100.0
    df = df[df["ratio"].notna()]
    tier = assign_tier(df["ratio"], 5, by=df["market"])
    return _frame(df["stock_code"], tier, df["ratio"].round(4))


@tag(
    code="att_southbound_hold_tier", name="南向持股占比档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily", data_sources=["daily_ggt_hold"],
    compute_logic="最新港股通（南向）持股占总股本比例，港股市场内五等分：1=南向参与度最低，5=最高。"
                  "覆盖限制：daily_ggt_hold 仅 627 只且自 2026-04 起；未出现在该表的港股"
                  "不等于南向零持股，只是无记录，因此本标签不可当作「非南向标的」使用",
    pit_capable=True, owner="profiling",
)
def att_southbound_hold_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        hold = _read_sql(
            conn,
            "SELECT DISTINCT ON (stock_code) stock_code, hold_ratio::float8 AS hold_ratio "
            "FROM daily_ggt_hold WHERE trade_date <= %s ORDER BY stock_code, trade_date DESC",
            (as_of,),
        )
        snap = load_quote_snapshot(conn, as_of)
    df = hold.merge(snap[["stock_code", "market"]], on="stock_code", how="inner")
    df = df[df["hold_ratio"].notna()]
    tier = assign_tier(df["hold_ratio"], 5, by=df["market"])
    return _frame(df["stock_code"], tier, df["hold_ratio"].round(4))


@tag(
    code="att_southbound_trend", name="南向持股变动", domain=DOMAIN,
    num_unit="pct",
    value_type=ENUM,
    enum_type="trend",
    enum_values={
        "rise": ("上升（≥+5%）", None),
        "flat": ("基本持平（±5% 内）", None),
        "fall": ("下降（≤−5%）", None),
    },
    source_type="rule", update_freq="daily", data_sources=["daily_ggt_hold"],
    compute_logic="近 20 个交易日南向持股数量的累计变动 / 期初持股量 ×100：≥+5% 增持，≤−5% 减持，"
                  "其余持平。num_value 存变动率(%)。覆盖限制同 att_southbound_hold_tier",
    pit_capable=True, owner="profiling",
)
def att_southbound_trend(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        rows = _read_sql(
            conn,
            """
            WITH recent AS (
                SELECT stock_code, trade_date, hold_num::float8 AS hold_num,
                       hold_ratio::float8 AS hold_ratio,
                       ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn,
                       COUNT(*) OVER (PARTITION BY stock_code) AS cnt
                FROM daily_ggt_hold WHERE trade_date <= %s AND trade_date > %s
            )
            SELECT stock_code,
                   MAX(CASE WHEN rn = 1 THEN hold_num END)   AS cur_num,
                   MAX(CASE WHEN rn = 1 THEN hold_ratio END) AS cur_ratio,
                   MAX(CASE WHEN rn = cnt THEN hold_num END) AS base_num,
                   MAX(CASE WHEN rn = cnt THEN hold_ratio END) AS base_ratio,
                   MAX(cnt) AS n_obs
            FROM recent GROUP BY stock_code
            """,
            (as_of, as_of - timedelta(days=45)),
        )
    # 低基数排除：期初南向持股占比 <0.5% 的是「建仓期」样本（多为次新 H 股），
    # 几百万股的买入就是几百 % 的变动率，纯噪声——实测圣邦股份 +5548% 即此类
    df = rows[(rows["n_obs"] >= W_1M // 2) & (rows["base_num"] > 0)
              & (rows["base_ratio"] >= 0.5) & rows["cur_num"].notna()]
    chg = (df["cur_num"] / df["base_num"] - 1) * 100.0
    return _frame(df["stock_code"], chg.map(_trend_key), chg.round(3))


# ═══════════════════════════════════════════════════════════════════════════
# 全市场：量能异动（放量 / 缩量）
# ═══════════════════════════════════════════════════════════════════════════


def _load_volume_baseline(conn, as_of: date) -> pd.DataFrame:
    """近 60 日的日均成交量 / 日均换手率（作为量能基准）。"""
    frames = []
    for mkt, table in (("A", "a_daily_quote"), ("HK", "hk_daily_quote")):
        f = _read_sql(
            conn,
            f"""
            SELECT stock_code, AVG(volume::float8) AS base_volume,
                   AVG(turnover_rate::float8) AS base_turnover, COUNT(*) AS n_obs
            FROM (
                SELECT stock_code, volume, turnover_rate FROM {table}
                WHERE trade_date <= %s AND trade_date > %s
            ) t GROUP BY stock_code
            """,
            (as_of, as_of - timedelta(days=95)),
        )
        f["market"] = mkt
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


@tag(
    code="att_volume_surge", name="量能异动", domain=DOMAIN,
    num_unit="x",
    value_type=ENUM,
    enum_type="volume_surge",
    enum_values={
        "surge": ("放量（成交量 ≥ 近 60 日均量的 2 倍）", None),
        "normal": ("量能正常（0.5~2 倍区间）", None),
        "shrink": ("缩量（成交量 ≤ 近 60 日均量的 0.5 倍）", None),
    },
    source_type="rule", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="当日成交量 / 近 60 日日均成交量：≥2 记 surge，≤0.5 记 shrink，其余 normal。"
                  "放量常伴随资金介入或分歧加剧，缩量代表关注度下降。num_value 存倍数",
    pit_capable=True, owner="profiling",
)
def att_volume_surge(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        base = _load_volume_baseline(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    # 当日成交量取最近交易日
    df = snap[["stock_code", "market", "volume"]].merge(
        base[["stock_code", "base_volume", "n_obs"]], on="stock_code", how="inner"
    )
    df = df[(df["n_obs"] >= 30) & (df["base_volume"] > 0) & (df["volume"] > 0)]
    mult = df["volume"] / df["base_volume"]

    def key(m):
        if m >= 2:
            return "surge"
        if m <= 0.5:
            return "shrink"
        return "normal"

    return _frame(df["stock_code"], mult.map(key), mult.round(3))


@tag(
    code="att_turnover_surge_tier", name="换手异动档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER,
    value_range={
        "1": "换手相对自身历史最低 20%（关注度降温）", "2": "次低", "3": "中间", "4": "次高",
        "5": "换手相对自身历史最高 20%（关注度升温）",
    },
    source_type="stat", update_freq="daily", data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="当日换手率 / 近 60 日日均换手率，市场内五等分。"
                  "与 ⑦ 域 trd_turnover（换手的绝对水平）互补：本标签衡量**相对自身历史**的异动，"
                  "可跨市值/行业比较（小盘股换手天然高，但异动倍数可比）",
    pit_capable=True, owner="profiling",
)
def att_turnover_surge_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        base = _load_volume_baseline(conn, as_of)
        snap = load_quote_snapshot(conn, as_of)
    df = snap[["stock_code", "market", "turnover_rate"]].merge(
        base[["stock_code", "base_turnover", "n_obs"]], on="stock_code", how="inner"
    )
    df = df[(df["n_obs"] >= 30) & (df["base_turnover"] > 0) & (df["turnover_rate"] > 0)]
    mult = df["turnover_rate"] / df["base_turnover"]
    tier = assign_tier(mult, 5, by=df["market"])
    return _frame(df["stock_code"], tier, mult.round(3))


# ═══════════════════════════════════════════════════════════════════════════
# 数据源缺失
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="att_main_inflow_tier", name="主力资金净流入档", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily", data_sources=["fund_flow_daily"],
    compute_logic="tick 聚合的四档资金净流入（超大单+大单净额）/ 流通市值，市场内五等分",
    status=PLANNED_NO_DATA,
    blocked_reason="覆盖过低：fund_flow_daily 仅 80 只（活跃采集池），"
                   "全市场建标签会让 99% 的股票无值。若将来对全市场采集逐笔数据可启用",
    pit_capable=True, owner="profiling",
)
def att_main_inflow_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("覆盖过低，见 blocked_reason")


@tag(
    code="att_northbound_hold_tier", name="北向持股档", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="external", update_freq="daily", data_sources=[],
    compute_logic="北向资金（陆股通）持有个股比例及其变化",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：daily_northbound_flow 是市场整体流水（无个股维度），"
                   "库中无北向个股持股明细表，需新增采集（如港交所 CCASS 或东财个股北向持股）",
    pit_capable=False, owner="profiling",
)
def att_northbound_hold_tier(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="att_holder_count_change", name="股东户数变化", domain=DOMAIN,
    value_type=ENUM,
    value_range={"concentrate": "股东户数减少（筹码集中）", "disperse": "股东户数增加（筹码分散）"},
    source_type="external", update_freq="quarterly", data_sources=[],
    compute_logic="股东户数环比变化：减少=筹码集中（主力吸筹信号），增加=筹码分散",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：库中无股东户数表，需新增采集（交易所定期报告或东财股东研究）",
    pit_capable=False, owner="profiling",
)
def att_holder_count_change(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="att_dragon_tiger", name="龙虎榜活跃度", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="external", update_freq="daily", data_sources=[],
    compute_logic="近 N 日上榜次数及席位性质（机构专用/游资营业部），衡量短线资金关注度",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：库中无龙虎榜表，需新增采集（交易所每日龙虎榜或东财接口）",
    pit_capable=False, owner="profiling",
)
def att_dragon_tiger(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="att_research_coverage", name="研报覆盖度", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="external", update_freq="monthly", data_sources=[],
    compute_logic="近 6 个月覆盖机构数、评级分布及评级调整方向（上调/下调）",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：库中无研报/评级表，需新增采集（券商研报聚合源）",
    pit_capable=False, owner="profiling",
)
def att_research_coverage(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="att_inst_holding_change", name="机构持仓变化", domain=DOMAIN,
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="external", update_freq="quarterly", data_sources=[],
    compute_logic="基金/社保/QFII 等机构持仓占流通股比例的环比变化",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：库中无机构持仓表，需新增采集（基金季报十大重仓/十大流通股东）",
    pit_capable=False, owner="profiling",
)
def att_inst_holding_change(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")
