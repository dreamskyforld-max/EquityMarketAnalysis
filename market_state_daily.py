#!/usr/bin/env python3
"""市场状态计算层 —— 读原始表算指标，写 regime 三张表。

定位（doc/market_profile_collection_plan.md §5.1 的「计算层」）：
    原始表（a_daily_quote / hk_daily_quote / daily_benchmark / daily_margin_balance /
           daily_ggt_hold / daily_northbound_flow / daily_short_selling / regime.macro_series）
      → 本模块（CN / HK 双市场）
      → regime.indicator_dict   （指标字典：口径/方向/频率/来源，代码即字典）
        regime.indicator_value  （指标时序：raw_value + 历史分位）
        regime.market_regime_daily（合成：温度计 / 季节 / 风险指数 / FSI / 顶底信号计数）

关键约定：
  · **分位口径**：`indicator_value.percentile` = 原始值的历史分位（0-100，expanding，
    只用 t 及之前的数据，无前视）；**不做方向调整**，方向由 indicator_dict.direction 声明，
    消费方（合成/展示）自行取反。
  · **无前视**：所有滚动窗口只用截至当日的数据；分位用 expanding。
  · **矩阵**：CN / HK 双市场；A 股口径剔除 B 股（SH.90*/SZ.20*）；港股口径设
    最低成交额门槛（HK_MIN_AMOUNT，默认 10 万港元，剔除仙股对宽度的污染）。
  · 数据起点不同（CN 宽度 1990 / 估值 2018；HK 2019），指标各自在可用区间起算。

用法:
    python3 market_state_daily.py                     # 增量（最近 30 个交易日，默认两市场）
    python3 market_state_daily.py --backfill          # 全历史回填（CN 1990 起 / HK 2019 起）
    python3 market_state_daily.py --market CN --start 2026-01-01
    python3 market_state_daily.py --no-write          # 只打印不写库（调试）
"""
import argparse
import bisect
import datetime
import sys

import numpy as np
import pandas as pd

from db import get_conn, bulk_upsert

TZ_CN = datetime.timezone(datetime.timedelta(hours=8))
IV_TABLE = "regime.indicator_value"
MR_TABLE = "regime.market_regime_daily"

# A 股口径常量
CN_EXCLUDE_PREFIX = ("SH.90", "SZ.20")          # B 股
CN_MAIN_PREFIX = ("SH.60", "SZ.00")             # 主板（±10%）
CN_GEM_PREFIX = ("SZ.30", "SH.68")              # 创业板 / 科创板（±20%）
HK_MIN_AMOUNT = 100000                          # 港股最低日成交额（港元），仙股门槛

# ── 指标字典（代码即字典：改这里 = 改口径）────────────────────────────────
# direction: high_risk=越高越危险 / high_good=越高越好 / neutral
INDICATORS: list[dict] = [
    # —— 宽度 Breadth ——
    dict(code="BREADTH.ADV_RATIO", name="上涨家数占比", layer=3, dimension="宽度",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="上涨家数 / 有行情家数（剔除停牌与 B 股 / 仙股）"),
    dict(code="BREADTH.MCCLELLAN", name="麦克莱林振荡", layer=3, dimension="宽度",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="EMA19(涨-跌) − EMA39(涨-跌)"),
    dict(code="BREADTH.ADV_MINUS_DEC", name="涨跌家数净差", layer=3, dimension="宽度",
         market_scope="CN+HK", unit="count", direction="high_good", freq="day",
         formula="涨家数 − 跌家数（腾落指数的每日增量；AD Line 累计值由消费方自行累加，避免累计序列在增量重算时口径漂移）"),
    dict(code="BREADTH.NEW_HIGH_LOW_60", name="60日新高−新低家数差(占比)", layer=3, dimension="宽度",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="(60日新高家数 − 60日新低家数) / 有效样本家数 * 100"),
    dict(code="BREADTH.PCT_ABOVE_MA60", name="站上 MA60 占比", layer=3, dimension="宽度",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="收盘 > MA60 的家数 / 有 60 日以上历史的家数 * 100"),
    dict(code="BREADTH.PCT_ABOVE_MA200", name="站上 MA200 占比", layer=3, dimension="宽度",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="收盘 > MA200 的家数 / 有 200 日以上历史的家数 * 100"),
    dict(code="BREADTH.EQ_VS_CAP_20D", name="等权−市值加权 20日累计收益差", layer=3, dimension="宽度",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         formula="Σ20(等权日收益 − 市值加权日收益)；依赖市值字段 → 2018 起"),
    dict(code="BREADTH.LIMIT_NET", name="涨跌停家数差(占比)", layer=3, dimension="宽度",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         formula="(涨停家数 − 跌停家数) / 有行情家数 * 100；涨跌停按板块阈值判定（主板±9.8 / 双创±19.6，ST 未单独处理）"),
    # —— 估值 Valuation ——
    dict(code="VAL.PE_TTM_MEDIAN", name="全市场 PE-TTM 中位数", layer=3, dimension="估值",
         market_scope="CN+HK", unit="x", direction="high_risk", freq="day",
         formula="中位数(PE-TTM)，剔除 PE<=0；个股级字段起点 CN 2018 / HK 2019"),
    dict(code="VAL.PB_MEDIAN", name="全市场 PB 中位数", layer=3, dimension="估值",
         market_scope="CN+HK", unit="x", direction="high_risk", freq="day",
         formula="中位数(PB)，剔除 PB<=0"),
    dict(code="VAL.PCT_BELOW_PB_1", name="破净股占比", layer=3, dimension="估值",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="PB < 1 的家数 / PB > 0 的家数 * 100（底部信号）"),
    dict(code="VAL.ERP", name="股权风险溢价 ERP", layer=2, dimension="估值",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="1/PE中位数*100 − 无风险利率（CN 用中债国债10Y；HK 用美债10Y）"),
    # —— 资金 / 情绪 Flow ——
    dict(code="FLOW.TURNOVER_RATIO", name="市场换手率", layer=3, dimension="资金",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="Σ成交额 / Σ流通市值(CN) 或 Σ总市值(HK) * 100；CN 依赖市值字段 → 2018 起"),
    dict(code="FLOW.MARGIN_RATIO", name="两融余额占流通市值比", layer=3, dimension="资金",
         market_scope="CN", unit="pct", direction="high_risk", freq="day",
         formula="Σ两融余额 / Σ流通市值 * 100（杠杆水位；2018 起）"),
    dict(code="FLOW.SOUTHBOUND_NET", name="南向净流入(亿港元)", layer=3, dimension="资金",
         market_scope="HK", unit="yi_hkd", direction="high_good", freq="day",
         formula="Σ est_net_inflow（daily_ggt_hold）；数据 2026-04 起，分位需长窗口累积"),
    dict(code="FLOW.NORTHBOUND_NET", name="北向净流入(亿元)", layer=3, dimension="资金",
         market_scope="CN", unit="yi_cny", direction="high_good", freq="day",
         formula="daily_northbound_flow.net_inflow；⚠ 官方 2024-08-16 后停披露，本指标序列在断点终止"),
    dict(code="FLOW.SHORT_SELLING_RATIO", name="港股卖空成交占比", layer=3, dimension="资金",
         market_scope="HK", unit="pct", direction="high_risk", freq="day",
         formula="Σ沽空金额 / 全市场成交额 * 100；数据 2026-06 起"),
    # —— 风险 Risk ——
    dict(code="RISK.RV20", name="指数20日已实现波动率(年化)", layer=3, dimension="风险",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="std(log收益, 20) * sqrt(252) * 100（基准：CN=SH.000001，HK=HK.800000）"),
    dict(code="RISK.RV60", name="指数60日已实现波动率(年化)", layer=3, dimension="风险",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="std(log收益, 60) * sqrt(252) * 100"),
    dict(code="RISK.MAXDD_250", name="近250日最大回撤", layer=3, dimension="风险",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="min(close / rolling_max(close,250) − 1) * 100（负值）"),
    dict(code="RISK.EXTREME_DOWN_PCT", name="单日跌幅超3%家数占比", layer=3, dimension="风险",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="change_pct <= -3 的家数 / 有行情家数 * 100"),
    dict(code="RISK.SYNC_DOWN_20D", name="下跌家数占比20日均值", layer=3, dimension="风险",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="mean20(下跌家数 / 有行情家数 * 100)；系统性同步下跌代理（真实两两相关性成本高，v1 用占比代理）"),
    # —— 宏观接入（来源 regime.macro_series，月度/日度值前向填充到交易日）——
    dict(code="MACRO.CREDIT_IMPULSE", name="信贷脉冲代理(社融12M滚动同比)", layer=1, dimension="宏观",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="month",
         remark="HK 侧用中国信用周期作代理（港股边际驱动：中国信用 + 美元流动性）",
         formula="12M 滚动社融增量 的同比变化率(%)（二阶导近似；真值口径需社融存量/GDP，待外采）"),
    dict(code="RISK.CREDIT_SPREAD", name="信用利差(中票AAA 10Y−国债10Y)", layer=1, dimension="风险",
         market_scope="CN", unit="pp", direction="high_risk", freq="day",
         formula="CN.CREDIT_SPREAD_MTN_AAA_10Y；利差走阔=违约担忧/风险偏好下降（源：get_macro_daily 派生）"),
    dict(code="RISK.TERM_SPREAD", name="期限利差(国债 10Y−1Y)", layer=1, dimension="风险",
         market_scope="CN", unit="pp", direction="high_good", freq="day",
         formula="CN.TERM_SPREAD_10Y_1Y；倒挂（低/负）为衰退预警"),
    dict(code="FLOW.HIBOR_3M", name="HIBOR 3月(港币流动性)", layer=1, dimension="资金",
         market_scope="HK", unit="pct", direction="high_risk", freq="day",
         formula="HK.HIBOR_3M；港元资金面收紧→港股估值与流动性承压"),
    # —— 分析师一致预期（依赖 regime.analyst_forecast_snapshot 逐日快照）——
    dict(code="FORECAST.REVISION_BREADTH", name="一致预期修正宽度(30快照滚动)", layer=3, dimension="景气",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         remark="需 ≥2 个快照日才有值；同日只算 FY1 且财年标签须一致，±0.5% 以内视为噪声不计",
         formula="(Σ30 快照上调家数 − 下调家数) / Σ30 有可比样本家数 × 100；上调=同一财年 FY1 EPS 上调 >0.5%"),
    dict(code="FORECAST.RATING_BULL_PCT", name="评级乐观度(买入+增持占比)", layer=3, dimension="情绪",
         market_scope="CN", unit="pct", direction="neutral", freq="day",
         remark="⚠ 判别力弱：卖方评级结构性集中在买入/增持（实测 2026-09-30 = 99.3%），"
                "横截面几乎不变，仅作极端背离（如骤降）时的提示，勿用于常规分位择时",
         formula="(买入+增持家数) / 五档评级合计 × 100"),
    # —— 情绪事件（依赖 regime.ipo_event / fund_issuance_event / unlock_schedule / insider_trade）——
    dict(code="SENT.IPO_COUNT_30D", name="近30日 IPO 上市家数", layer=3, dimension="情绪",
         market_scope="CN", unit="count", direction="high_risk", freq="day",
         remark="新股供给压力 + 牛市温度；窗口内无 IPO 记 0，源覆盖前为空",
         formula="count(list_date ∈ (t−30d, t])"),
    dict(code="SENT.IPO_BREAK_RATE_90D", name="近90日 IPO 首日破发率", layer=3, dimension="情绪",
         market_scope="CN", unit="pct", direction="high_risk", freq="day",
         remark="破发=情绪低迷的直接读数；90 日窗口取样本量（30 日窗口常年为 0 无判别力）",
         formula="count(首日收盘价<发行价) / count(上市) × 100，(t−90d, t]"),
    dict(code="SENT.FUND_ISSUANCE_60D", name="近60日权益类基金成立份额", layer=3, dimension="情绪",
         market_scope="CN", unit="yi_fen", direction="neutral", freq="day",
         remark="权益类=基金类型不含「债/固收/稳健/货币」；既是增量资金也是散户入场热度",
         formula="Σ issue_share(亿份)，setup_date ∈ (t−60d, t]"),
    dict(code="SENT.FUND_MAX_60D", name="近60日最大单只基金成立份额(爆款)", layer=3, dimension="情绪",
         market_scope="CN", unit="yi_fen", direction="neutral", freq="day",
         remark="爆款基金=散户入场的强信号，亦常出现在阶段性顶部附近（需结合估值水位看）",
         formula="max(issue_share)，权益类，(t−60d, t]"),
    dict(code="SENT.UNLOCK_NEXT_30D_SHARES", name="未来30日解禁股数", layer=3, dimension="资金",
         market_scope="CN", unit="yi_gu", direction="high_risk", freq="day",
         remark="公告先行的供给压力；用**股数**口径保证 PIT 安全"
                "（市值口径需当日价格估计，待升级），解禁前 30 日内已知",
         formula="Σ unlock_shares(亿股)，unlock_date ∈ (t, t+30d]"),
    dict(code="SENT.UNLOCK_PAST_30D_VALUE", name="近30日已解禁市值", layer=3, dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="neutral", freq="day",
         remark="已实现口径（实际解禁市值仅解禁后可算，故只看过去）；与未来压力对照",
         formula="Σ unlock_value(亿元)，unlock_date ∈ (t−30d, t]"),
    dict(code="SENT.INSIDER_NET_SELL_60D", name="近60日董监高净减持金额", layer=3, dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="high_risk", freq="day",
         remark="仅事件级「临时公告」（含成交均价）；源仅滚动近 1 年、历史靠逐日累积；"
                "北交所已排除（源侧变动数量量纲失真）",
         formula="(Σ SELL − Σ BUY) event_value(亿元)，ann_date ∈ (t−60d, t]"),
    # —— 行业层（第④层，独立于市场合成；不做温度计/风险成员）——
    dict(code="SECTOR.UP_RATIO", name="行业上涨占比(申万一级)", layer=4, dimension="宽度",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         remark="31 个申万一级行业中当日收涨的占比；行业层宽度（分母为当日有行情的行业数）",
         formula="count(行业当日涨幅>0) / count(有行情行业) × 100"),
    dict(code="SECTOR.MOM_20D_MEDIAN", name="行业20日动量中位数", layer=4, dimension="动量",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         remark="横截面中位数（抗单行业极端值）；趋势跟随读法，与估值分位配合防过热",
         formula="median(行业指数 20 日收益 %)，31 个申万一级行业"),
    dict(code="SECTOR.RS_DISPERSION", name="行业相对强度离散度(轮动强度)", layer=4, dimension="风格",
         market_scope="CN", unit="pct", direction="neutral", freq="day",
         remark="高=结构分化/主线切换剧烈，低=同涨同跌；**无方向优劣**，须配合行业 RS 方向解读",
         formula="std(行业 20 日收益 − 行业等权均值)，横截面"),
    dict(code="SECTOR.PE_TTM_MEDIAN", name="行业 PE-TTM 中位数(申万一级)", layer=4, dimension="估值",
         market_scope="CN", unit="x", direction="high_risk", freq="day",
         remark="31 个申万一级行业 TTM 市盈率的中位数；源**只有当前横截面**（无历史）→ "
                "分位靠逐日累积，早期分位无意义",
         formula="median(行业 PE-TTM)，31 个申万一级行业"),
    dict(code="SECTOR.PB_MEDIAN", name="行业 PB 中位数(申万一级)", layer=4, dimension="估值",
         market_scope="CN", unit="x", direction="high_risk", freq="day",
         remark="同 PE 口径；逐行业估值明细见 regime.sector_valuation_snapshot",
         formula="median(行业 PB)，31 个申万一级行业"),
    dict(code="SECTOR.FLOW_NET", name="行业资金净流入合计(同花顺)", layer=4, dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="high_good", freq="day",
         remark="约 90 个同花顺细分行业净额合计（≠ 申万一级分类，跨表 join 需映射）；源只给当前快照",
         formula="Σ net_inflow(亿元)，当日横截面"),
    dict(code="SECTOR.FLOW_UP_RATIO", name="净流入行业占比(同花顺)", layer=4, dimension="资金",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         formula="count(净额>0) / count(有效行业) × 100"),
    dict(code="SECTOR.FLOW_NET_5D", name="近5日行业资金净流入", layer=4, dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="high_good", freq="day",
         remark="需 ≥5 个交易日数据（min_periods=5），不足则空——避免把部分窗口当完整窗口",
         formula="Σ 近5个交易日 SECTOR.FLOW_NET"),
]

# 合成指数：成员 = (indicator_code, sign)；sign=+1 高分贡献高，-1 取 100−分位
COMPOSITES = {
    "thermometer": dict(name="市场温度计", members=[
        ("BREADTH.ADV_RATIO", 1), ("BREADTH.MCCLELLAN", 1), ("BREADTH.PCT_ABOVE_MA60", 1),
        ("FLOW.TURNOVER_RATIO", 1), ("RISK.RV20", 1), ("FLOW.MARGIN_RATIO", 1),
        ("MACRO.CREDIT_IMPULSE", 1),          # 信用扩张 → 热度上行
        ("FLOW.HIBOR_3M", -1),                # 港元资金面收紧 → 降温（HK 专属，CN 侧为空跳过）
        ("FORECAST.REVISION_BREADTH", 1),     # 盈利预期上修 → 热度上行（CN 专属）
        ("FORECAST.RATING_BULL_PCT", 1),      # 卖方一致性乐观 → 热度上行（CN 专属）
        ("SENT.IPO_COUNT_30D", 1),            # IPO 密集 → 供给与温度同步上行（CN 专属）
        ("SENT.FUND_ISSUANCE_60D", 1),        # 基金发行放量 → 增量资金/散户热度（CN 专属）
    ]),
    "risk_score": dict(name="风险指数", members=[
        ("VAL.PE_TTM_MEDIAN", 1), ("VAL.PB_MEDIAN", 1), ("VAL.ERP", -1),
        ("RISK.MAXDD_250", 1), ("RISK.EXTREME_DOWN_PCT", 1), ("RISK.RV60", 1),
        ("VAL.PCT_BELOW_PB_1", -1),
        ("RISK.CREDIT_SPREAD", 1),            # 信用利差走阔 → 风险↑
        ("RISK.TERM_SPREAD", -1),             # 期限利差倒挂 → 风险↑
        ("SENT.UNLOCK_NEXT_30D_SHARES", 1),   # 未来解禁压力 → 供给风险↑（CN 专属）
        ("SENT.INSIDER_NET_SELL_60D", 1),     # 董监高净减持 → 内部人风险↑（CN 专属）
    ]),
    "fsi": dict(name="金融压力指数", members=[
        ("RISK.RV20", 1), ("RISK.RV60", 1), ("RISK.EXTREME_DOWN_PCT", 1),
        ("RISK.SYNC_DOWN_20D", 1), ("FLOW.TURNOVER_RATIO", -1),
        ("RISK.CREDIT_SPREAD", 1),            # 信用维度（原计划缺口，已用中债曲线补齐）
        ("RISK.TERM_SPREAD", -1),             # 期限结构
        ("MACRO.CREDIT_IMPULSE", -1),         # 信用扩张 → 压力↓
        ("FLOW.HIBOR_3M", 1),                 # 港币资金面（HK 专属）
    ]),
}

DICT_COLS = ["indicator_code", "indicator_name", "layer", "dimension", "market_scope",
             "unit", "direction", "freq", "source", "formula", "is_active", "remark"]


# ── 工具 ────────────────────────────────────────────────────────────────────
def _q(conn, sql, params=None) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def _expanding_percentile(s: pd.Series) -> pd.Series:
    """逐点历史分位（只用 t 及之前的数据，无前视）。"""
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    buf: list = []
    for i, v in enumerate(vals):
        if np.isnan(v):
            continue
        lo = bisect.bisect_left(buf, v)
        hi = bisect.bisect_right(buf, v)
        avg_rank = (lo + hi) / 2.0
        out[i] = 100.0 * (avg_rank + 0.5) / (len(buf) + 1)
        bisect.insort(buf, v)
    return pd.Series(out, index=s.index)


def _series_from_df(df: pd.DataFrame, date_col: str, value_col: str) -> pd.Series:
    """DataFrame → 以 trade_date 为索引的 Series（去重、排序）。"""
    if df.empty:
        return pd.Series(dtype=float)
    s = df.set_index(pd.to_datetime(df[date_col]).dt.date)[value_col].astype(float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _clip(s, start: datetime.date, end: datetime.date) -> pd.Series:
    """按日期裁剪（对空序列 / 非日期索引安全：空序列直接返回空）。

    坑：空 Series 的默认索引是 RangeIndex，用 s[start:end] 切会抛
    TypeError: cannot do slice indexing on RangeIndex（北向断点后查询即为空）。
    """
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    # 索引统一成 datetime.date 再比较：Timestamp 是 datetime 的子类，
    # isinstance(d, datetime.date) 为真但 date 与 Timestamp 直接比较会抛 TypeError
    # （曾因某指标返回 Timestamp 索引导致整块计算失败）。
    def _as_date(d):
        if isinstance(d, pd.Timestamp):
            return d.date()
        return d if isinstance(d, datetime.date) else None

    idx_dates = [_as_date(d) for d in s.index]
    mask = [d is not None and start <= d <= end for d in idx_dates]
    return s[mask]


def _per_stock_windows(conn, market: str, w_start: datetime.date, end: datetime.date) -> pd.DataFrame:
    """逐股滚动窗口：60 日新高/新低、MA60/MA200 上下占比。

    为什么不全在 SQL 里做（性能实测）：PostgreSQL 的 max()/min() 移动窗口**没有逆函数**，
    1.4M 行 × 60 帧 ≈ 33s；改成「SQL 只取原始列 + pandas rolling（C 实现）」后 <3s。
    返回：按 trade_date 聚合的家数（ok60/ok200 为有效样本数，nh60/nl60/ab60/ab200 为命中数）。
    """
    if market == "CN":
        sql = """SELECT trade_date, stock_code, close FROM a_daily_quote
                 WHERE trade_date BETWEEN %s AND %s AND close IS NOT NULL
                   AND close <> 'NaN'::numeric AND left(stock_code,5) <> ALL(%s)"""
        params = [w_start, end, list(CN_EXCLUDE_PREFIX)]
    else:
        sql = """SELECT trade_date, stock_code, close FROM hk_daily_quote
                 WHERE trade_date BETWEEN %s AND %s AND close IS NOT NULL
                   AND close <> 'NaN'::numeric AND amount >= %s"""
        params = [w_start, end, HK_MIN_AMOUNT]

    df = _q(conn, sql, params)
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df = df.sort_values(["stock_code", "trade_date"], kind="mergesort").reset_index(drop=True)

    roll = df.groupby("stock_code", sort=False)["close"]
    n60 = roll.rolling(60, min_periods=1).count().reset_index(level=0, drop=True)
    n200 = roll.rolling(200, min_periods=1).count().reset_index(level=0, drop=True)
    h60 = roll.rolling(60, min_periods=1).max().reset_index(level=0, drop=True)
    l60 = roll.rolling(60, min_periods=1).min().reset_index(level=0, drop=True)
    ma60 = roll.rolling(60, min_periods=1).mean().reset_index(level=0, drop=True)
    ma200 = roll.rolling(200, min_periods=1).mean().reset_index(level=0, drop=True)

    ok60, ok200 = n60 >= 60, n200 >= 200
    agg = pd.DataFrame({
        "trade_date": df["trade_date"],
        "ok60": ok60.astype(int), "ok200": ok200.astype(int),
        "nh60": (ok60 & (df["close"] >= h60)).astype(int),
        "nl60": (ok60 & (df["close"] <= l60)).astype(int),
        "ab60": (ok60 & (df["close"] > ma60)).astype(int),
        "ab200": (ok200 & (df["close"] > ma200)).astype(int),
    })
    return agg.groupby("trade_date")[["ok60", "ok200", "nh60", "nl60", "ab60", "ab200"]].sum()


# ── 计算：CN ────────────────────────────────────────────────────────────────
def calc_cn(conn, start: datetime.date, end: datetime.date, warmup_days: int = 400) -> dict:
    """A 股指标计算（宽度/估值/资金/风险），返回 {code: Series}。"""
    w_start = start - datetime.timedelta(days=warmup_days)
    ex = list(CN_EXCLUDE_PREFIX)
    out: dict[str, pd.Series] = {}

    # ① 单日横截面（涨跌家数 / 涨跌停 / PE-PB / 换手 / 极端跌幅）
    # 注：NULLIF(col,'NaN'::numeric) 防御 PostgreSQL numeric NaN（写入层已修，存量已清）；
    #     left(stock_code,5) 与 5 字符前缀比较（旧写法 left(...,4) 与 'SH.90' 永不相等 → B 股未排除）。
    df = _q(conn, """
        WITH src AS (
            SELECT trade_date, stock_code, change_pct, amount,
                   NULLIF(pe_ttm_ratio, 'NaN'::numeric)         AS pe,
                   NULLIF(pb_ratio, 'NaN'::numeric)             AS pb,
                   NULLIF(circular_market_val, 'NaN'::numeric)  AS cmv
            FROM a_daily_quote
            WHERE trade_date BETWEEN %s AND %s
              AND change_pct IS NOT NULL
              AND left(stock_code,5) <> ALL(%s)
        )
        SELECT trade_date,
               count(*) FILTER (WHERE change_pct > 0)                       AS adv,
               count(*) FILTER (WHERE change_pct < 0)                       AS dec,
               count(*)                                                     AS n,
               count(*) FILTER (WHERE change_pct <= -3)                     AS extdown,
               count(*) FILTER (WHERE left(stock_code,5) = ANY(%s)
                                  AND change_pct >= 9.8)                     AS lu_main,
               count(*) FILTER (WHERE left(stock_code,5) = ANY(%s)
                                  AND change_pct >= 19.6)                    AS lu_gem,
               count(*) FILTER (WHERE left(stock_code,5) = ANY(%s)
                                  AND change_pct <= -9.8)                    AS ld_main,
               count(*) FILTER (WHERE left(stock_code,5) = ANY(%s)
                                  AND change_pct <= -19.6)                   AS ld_gem,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY pe)
                   FILTER (WHERE pe > 0)                                     AS pe_med,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY pb)
                   FILTER (WHERE pb > 0)                                     AS pb_med,
               count(*) FILTER (WHERE pb > 0)                                AS pb_n,
               count(*) FILTER (WHERE pb > 0 AND pb < 1)                     AS below1,
               sum(amount)                                                   AS amt,
               sum(cmv)                                                      AS cmv,
               avg(change_pct)                                               AS eq_ret,
               sum(change_pct * cmv) / nullif(sum(cmv), 0)                   AS cap_ret
        FROM src
        GROUP BY trade_date ORDER BY trade_date
    """, [w_start, end, ex, list(CN_MAIN_PREFIX), list(CN_GEM_PREFIX),
          list(CN_MAIN_PREFIX), list(CN_GEM_PREFIX)])
    idx = pd.to_datetime(df["trade_date"]).dt.date
    df = df.set_index(idx)

    out["BREADTH.ADV_RATIO"] = df["adv"] / df["n"] * 100
    adv_minus_dec = df["adv"] - df["dec"]
    out["BREADTH.MCCLELLAN"] = (adv_minus_dec.ewm(span=19, adjust=False).mean()
                                - adv_minus_dec.ewm(span=39, adjust=False).mean())
    out["BREADTH.ADV_MINUS_DEC"] = adv_minus_dec
    out["BREADTH.LIMIT_NET"] = (df["lu_main"] + df["lu_gem"] - df["ld_main"] - df["ld_gem"]) / df["n"] * 100
    out["VAL.PE_TTM_MEDIAN"] = df["pe_med"]
    out["VAL.PB_MEDIAN"] = df["pb_med"]
    out["VAL.PCT_BELOW_PB_1"] = df["below1"] / df["pb_n"].replace(0, np.nan) * 100
    out["RISK.EXTREME_DOWN_PCT"] = df["extdown"] / df["n"] * 100
    out["RISK.SYNC_DOWN_20D"] = (df["dec"] / df["n"] * 100).rolling(20, min_periods=10).mean()
    out["FLOW.TURNOVER_RATIO"] = df["amt"] / df["cmv"].replace(0, np.nan) * 100
    out["BREADTH.EQ_VS_CAP_20D"] = (df["eq_ret"] - df["cap_ret"]).rolling(20, min_periods=10).sum()

    # ② 逐股窗口（60/200 日新高新低、MA 上下占比）—— 见 _per_stock_windows 的性能说明
    wdf = _per_stock_windows(conn, "CN", w_start, end)
    if not wdf.empty:
        out["BREADTH.NEW_HIGH_LOW_60"] = (wdf["nh60"] - wdf["nl60"]) / wdf["ok60"].replace(0, np.nan) * 100
        out["BREADTH.PCT_ABOVE_MA60"] = wdf["ab60"] / wdf["ok60"].replace(0, np.nan) * 100
        out["BREADTH.PCT_ABOVE_MA200"] = wdf["ab200"] / wdf["ok200"].replace(0, np.nan) * 100

    # ③ 两融占比（余额 / 流通市值）
    mdf = _q(conn, """
        SELECT m.trade_date, sum(m.rzrq_balance) AS margin
        FROM daily_margin_balance m
        WHERE m.trade_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 1
    """, [w_start, end])
    margin = _series_from_df(mdf, "trade_date", "margin")
    cmv = df["cmv"].astype(float)
    out["FLOW.MARGIN_RATIO"] = (margin.reindex(cmv.index) / cmv.replace(0, np.nan) * 100)

    # ④ 北向净流入（断点 2024-08-16 之后自然为空）
    ndf = _q(conn, """SELECT trade_date, net_inflow FROM daily_northbound_flow
                      WHERE trade_date BETWEEN %s AND %s ORDER BY 1""", [w_start, end])
    out["FLOW.NORTHBOUND_NET"] = _series_from_df(ndf, "trade_date", "net_inflow")

    # ⑤ 指数波动率 / 回撤（上证指数）
    bdf = _q(conn, """SELECT trade_date, last_price FROM daily_benchmark
                      WHERE bench_code='SH.000001' AND trade_date BETWEEN %s AND %s ORDER BY 1""",
             [w_start, end])
    out.update(_risk_from_index(bdf, "trade_date", "last_price", "CN"))

    # ⑥ ERP = 1/PE − 中国10Y国债（国债收益率日频，取同日/最近前值）
    b10 = _q(conn, """SELECT period_date, value FROM regime.macro_series
                      WHERE series_code='CN.BOND_10Y' AND period_date BETWEEN %s AND %s
                      ORDER BY period_date""", [w_start, end])
    b10s = _series_from_df(b10, "period_date", "value").reindex(out["VAL.PE_TTM_MEDIAN"].index).ffill()
    out["VAL.ERP"] = 1.0 / out["VAL.PE_TTM_MEDIAN"].replace(0, np.nan) * 100 - b10s

    # ⑦ 宏观接入（PIT 填充到交易日）：信用利差 / 期限利差 / 信贷脉冲代理
    out.update(_macro_pit_map(conn, out["BREADTH.ADV_RATIO"].index, {
        "RISK.CREDIT_SPREAD": "CN.CREDIT_SPREAD_MTN_AAA_10Y",
        "RISK.TERM_SPREAD": "CN.TERM_SPREAD_10Y_1Y",
    }))
    out["MACRO.CREDIT_IMPULSE"] = _credit_impulse(conn, out["BREADTH.ADV_RATIO"].index)

    # ⑧ 分析师一致预期（修正宽度 / 评级乐观度）
    out.update(_forecast_indicators(conn, out["BREADTH.ADV_RATIO"].index))

    # ⑨ 情绪事件（IPO / 基金发行 / 解禁 / 董监高增减持）
    out.update(_event_indicators(conn, out["BREADTH.ADV_RATIO"].index))

    # ⑩ 行业层（申万一级行业宽度/动量分化 + 行业资金流；不入合成）
    out.update(_sector_indicators(conn, out["BREADTH.ADV_RATIO"].index))

    return {k: _clip(v, start, end) for k, v in out.items()}


# ── 计算：HK ────────────────────────────────────────────────────────────────
def calc_hk(conn, start: datetime.date, end: datetime.date, warmup_days: int = 400) -> dict:
    """港股指标计算（宽度/估值/资金/风险）。"""
    w_start = start - datetime.timedelta(days=warmup_days)
    out: dict[str, pd.Series] = {}

    df = _q(conn, """
        WITH src AS (
            SELECT trade_date, close, prev_close, amount,
                   NULLIF(pe_ttm_ratio, 'NaN'::numeric)     AS pe,
                   NULLIF(pb_ratio, 'NaN'::numeric)         AS pb,
                   NULLIF(total_market_val, 'NaN'::numeric) AS tmv
            FROM hk_daily_quote
            WHERE trade_date BETWEEN %s AND %s
              AND close IS NOT NULL AND prev_close IS NOT NULL
              AND close <> 'NaN'::numeric AND amount >= %s
        )
        SELECT trade_date,
               count(*) FILTER (WHERE close > prev_close)                 AS adv,
               count(*) FILTER (WHERE close < prev_close)                 AS dec,
               count(*)                                                   AS n,
               count(*) FILTER (WHERE close <= prev_close * 0.97)         AS extdown,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY pe)
                   FILTER (WHERE pe > 0)                                   AS pe_med,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY pb)
                   FILTER (WHERE pb > 0)                                   AS pb_med,
               count(*) FILTER (WHERE pb > 0)                              AS pb_n,
               count(*) FILTER (WHERE pb > 0 AND pb < 1)                   AS below1,
               sum(amount)                                                 AS amt,
               sum(tmv)                                                    AS tmv
        FROM src
        GROUP BY trade_date ORDER BY trade_date
    """, [w_start, end, HK_MIN_AMOUNT])
    if df.empty:
        return {}
    df = df.set_index(pd.to_datetime(df["trade_date"]).dt.date)

    out["BREADTH.ADV_RATIO"] = df["adv"] / df["n"] * 100
    adv_minus_dec = df["adv"] - df["dec"]
    out["BREADTH.MCCLELLAN"] = (adv_minus_dec.ewm(span=19, adjust=False).mean()
                                - adv_minus_dec.ewm(span=39, adjust=False).mean())
    out["BREADTH.ADV_MINUS_DEC"] = adv_minus_dec
    out["VAL.PE_TTM_MEDIAN"] = df["pe_med"]
    out["VAL.PB_MEDIAN"] = df["pb_med"]
    out["VAL.PCT_BELOW_PB_1"] = df["below1"] / df["pb_n"].replace(0, np.nan) * 100
    out["RISK.EXTREME_DOWN_PCT"] = df["extdown"] / df["n"] * 100
    out["RISK.SYNC_DOWN_20D"] = (df["dec"] / df["n"] * 100).rolling(20, min_periods=10).mean()
    out["FLOW.TURNOVER_RATIO"] = df["amt"] / df["tmv"].replace(0, np.nan) * 100

    # 逐股窗口（60/200 日）
    wdf = _per_stock_windows(conn, "HK", w_start, end)
    if not wdf.empty:
        out["BREADTH.NEW_HIGH_LOW_60"] = (wdf["nh60"] - wdf["nl60"]) / wdf["ok60"].replace(0, np.nan) * 100
        out["BREADTH.PCT_ABOVE_MA60"] = wdf["ab60"] / wdf["ok60"].replace(0, np.nan) * 100
        out["BREADTH.PCT_ABOVE_MA200"] = wdf["ab200"] / wdf["ok200"].replace(0, np.nan) * 100

    # 南向净流入（亿港元）
    sdf = _q(conn, """SELECT trade_date, sum(est_net_inflow) AS net FROM daily_ggt_hold
                      WHERE trade_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 1""", [w_start, end])
    out["FLOW.SOUTHBOUND_NET"] = _series_from_df(sdf, "trade_date", "net")

    # 卖空成交占比 = 沽空金额(亿港元) / 全市场成交额
    shdf = _q(conn, """SELECT trade_date, sum(short_selling_amt) AS short_amt
                       FROM daily_short_selling WHERE trade_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 1""",
              [w_start, end])
    tdf = _q(conn, """SELECT trade_date, total_turnover FROM daily_market_turnover
                      WHERE trade_date BETWEEN %s AND %s ORDER BY 1""", [w_start, end])
    short_amt = _series_from_df(shdf, "trade_date", "short_amt")
    turnover = _series_from_df(tdf, "trade_date", "total_turnover")
    unit = 1e8 if (turnover.dropna().tail(5) > 1e8).all() else 1.0   # 成交额单位自适应（元 / 亿）
    out["FLOW.SHORT_SELLING_RATIO"] = short_amt / (turnover / unit) * 100

    # 指数波动率 / 回撤（恒生指数）
    bdf = _q(conn, """SELECT trade_date, last_price FROM daily_benchmark
                      WHERE bench_code='HK.800000' AND trade_date BETWEEN %s AND %s ORDER BY 1""",
             [w_start, end])
    out.update(_risk_from_index(bdf, "trade_date", "last_price", "HK"))

    # ERP = 1/PE − 美债10Y
    dgs = _q(conn, """SELECT trade_date, last_price FROM daily_benchmark
                      WHERE bench_code='US.DGS10' AND trade_date BETWEEN %s AND %s ORDER BY 1""",
             [w_start, end])
    dgs_s = _series_from_df(dgs, "trade_date", "last_price").reindex(out["VAL.PE_TTM_MEDIAN"].index).ffill()
    out["VAL.ERP"] = 1.0 / out["VAL.PE_TTM_MEDIAN"].replace(0, np.nan) * 100 - dgs_s

    # 宏观接入：HIBOR 3M（港元流动性）+ 中国信用脉冲（HK 用其作为边际驱动代理），PIT 填充
    out.update(_macro_pit_map(conn, out["VAL.PE_TTM_MEDIAN"].index,
                              {"FLOW.HIBOR_3M": "HK.HIBOR_3M"}))
    out["MACRO.CREDIT_IMPULSE"] = _credit_impulse(conn, out["VAL.PE_TTM_MEDIAN"].index)

    return {k: _clip(v, start, end) for k, v in out.items()}


def _naive_local(series) -> pd.Series:
    """TIMESTAMPTZ → 上海本地朴素时间（供 merge_asof 与交易日对齐）。"""
    s = pd.to_datetime(series, errors="coerce", utc=True)
    return s.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)


def _macro_pit_map(conn, idx, mapping: dict) -> dict:
    """把 regime.macro_series 的序列按 **PIT 规则** 前向填充到交易日。

    PIT 规则：交易日 t 上只能用 release_time <= t 23:59 的最近一条观测
    （月频数据次年 10 日才发布，若按 period_date 直接对齐就是前视）。
    用 merge_asof 实现（O(n log n)），避免逐日布尔筛选。
    """
    out = {}
    left = pd.DataFrame({"t": [pd.Timestamp(datetime.datetime.combine(d, datetime.time(23, 59)))
                               for d in idx]})
    left = left.sort_values("t").reset_index(drop=True)
    for code, src in mapping.items():
        df = _q(conn, """SELECT period_date, release_time, value FROM regime.macro_series
                         WHERE series_code = %s AND revision = 0 ORDER BY period_date""", [src])
        if df.empty:
            out[code] = pd.Series(np.nan, index=idx)
            continue
        # 统一为「上海本地朴素时间」：release_time 是 TIMESTAMPTZ（带时区），
        # 直接与本地朴素时间做 merge_asof 会报 dtype 不兼容。
        right = pd.DataFrame({
            "rel": _naive_local(df["release_time"]),
            "v": pd.to_numeric(df["value"], errors="coerce"),
        }).dropna(subset=["rel"]).sort_values("rel")
        if right.empty:
            out[code] = pd.Series(np.nan, index=idx)
            continue
        merged = pd.merge_asof(left, right, left_on="t", right_on="rel", direction="backward")
        out[code] = pd.Series(merged["v"].to_numpy(), index=idx)
    return out


def _credit_impulse(conn, idx) -> pd.Series:
    """信贷脉冲代理：12M 滚动社融增量的同比变化率(%)，按 PIT 填充到交易日。

    真值口径（社融存量/GDP 的二阶导）需存量数据，当前源只有月度增量 → 用滚动和近似，
    并在指标字典 formula/remark 注明。
    """
    df = _q(conn, """SELECT period_date, release_time, value FROM regime.macro_series
                     WHERE series_code='CN.SHRZGM_INC' AND revision=0 ORDER BY period_date""")
    if df.empty:
        return pd.Series(np.nan, index=idx)
    s = _series_from_df(df, "period_date", "value")
    roll = s.rolling(12, min_periods=12).sum()
    imp = (roll / roll.shift(12) - 1) * 100
    if imp.dropna().empty:
        return pd.Series(np.nan, index=idx)
    right = pd.DataFrame({
        "rel": _naive_local(df["release_time"]).to_numpy(),
        "v": imp.to_numpy(),
    }).dropna(subset=["rel"]).sort_values("rel")
    left = pd.DataFrame({"t": [pd.Timestamp(datetime.datetime.combine(d, datetime.time(23, 59)))
                               for d in idx]}).sort_values("t").reset_index(drop=True)
    merged = pd.merge_asof(left, right, left_on="t", right_on="rel", direction="backward")
    return pd.Series(merged["v"].to_numpy(), index=idx)


def _forecast_indicators(conn, idx, lookback_days: int = 400) -> dict:
    """分析师一致预期指标：修正宽度 + 评级乐观度（CN）。

    修正宽度（Revision Breadth）：把「同一财年 FY1 EPS」的相邻两次快照做 diff，
    上调(+1)/下调(-1)/无变化或无样本(0)，再取 30 个快照的滚动净占比。
    快照日本身即 PIT（当日采集、当日可得），因此按 ffill 对齐到交易日不产生前视。
    """
    empty = {"FORECAST.REVISION_BREADTH": pd.Series(np.nan, index=idx),
             "FORECAST.RATING_BULL_PCT": pd.Series(np.nan, index=idx)}
    since = min(idx) - datetime.timedelta(days=lookback_days) if len(idx) else datetime.date(2020, 1, 1)
    df = _q(conn, """
        SELECT snapshot_date, stock_code, fy1_year, fy1_eps,
               rating_buy, rating_overweight, rating_neutral, rating_reduce, rating_sell
        FROM regime.analyst_forecast_snapshot
        WHERE snapshot_date >= %s ORDER BY snapshot_date, stock_code
    """, [since])
    if df.empty:
        return empty
    df = df.sort_values(["stock_code", "snapshot_date"]).reset_index(drop=True)

    # ① 评级乐观度（按快照日横截面）
    rt = df[["rating_buy", "rating_overweight", "rating_neutral",
             "rating_reduce", "rating_sell"]].apply(pd.to_numeric, errors="coerce")
    total = rt.sum(axis=1, skipna=True)
    bull = (rt["rating_buy"].fillna(0) + rt["rating_overweight"].fillna(0))
    opt = (bull / total.replace(0, np.nan) * 100)
    opt_s = opt.groupby(df["snapshot_date"]).mean()

    # ② 修正宽度
    g = df.groupby("stock_code", sort=False)
    prev_eps, prev_year = g["fy1_eps"].shift(1), g["fy1_year"].shift(1)
    cur_eps = pd.to_numeric(df["fy1_eps"], errors="coerce")
    prev_eps = pd.to_numeric(prev_eps, errors="coerce")
    comparable = (cur_eps.notna() & prev_eps.notna()
                  & (df["fy1_year"] == prev_year) & (prev_eps.abs() > 0))
    chg = (cur_eps - prev_eps) / prev_eps.abs()
    upd = (comparable & (chg > 0.005)).astype(int)
    dwn = (comparable & (chg < -0.005)).astype(int)
    agg = pd.DataFrame({"d": df["snapshot_date"], "up": upd, "dn": dwn,
                        "n": comparable.astype(int)}).groupby("d").sum().sort_index()
    roll = agg.rolling(30, min_periods=2).sum()
    breadth = (roll["up"] - roll["dn"]) / roll["n"].replace(0, np.nan) * 100

    def _ffill_to_trading(s: pd.Series) -> pd.Series:
        """快照日序列 → 交易日序列（交易日 t 取 <= t 的最近快照值；无前视）。"""
        if s.dropna().empty:
            return pd.Series(np.nan, index=idx)
        m = s.dropna().copy()
        m.index = pd.DatetimeIndex([pd.Timestamp(d) for d in m.index])
        tgt = pd.DatetimeIndex([pd.Timestamp(d) for d in idx])
        combined = m.reindex(m.index.union(tgt)).ffill()
        return pd.Series(combined.reindex(tgt).to_numpy(), index=idx)

    return {"FORECAST.REVISION_BREADTH": _ffill_to_trading(breadth),
            "FORECAST.RATING_BULL_PCT": _ffill_to_trading(opt_s)}


def _win_agg(dates, values, idx, days: int, how: str = "sum", forward: bool = False) -> pd.Series:
    """事件窗口聚合（PIT 安全）：对每个交易日 t 聚合 (t−days, t] 内的事件；
    forward=True 时聚合 (t, t+days]（仅用于「已公告的未来排期」，无前视）。

    向量化：事件按日期排序 → searchsorted 定位窗口边界 + 前缀和，
    避免「23k 事件 × 8.7k 交易日」的双重循环。
    窗口内无事件返回 0（对「家数/金额」是合法值）；源覆盖前的日期由 `_mask_before` 置空。
    """
    if dates is None or len(dates) == 0:
        return pd.Series(np.nan, index=idx)
    d = np.asarray(pd.to_datetime(pd.Series(list(dates))).values, dtype="datetime64[D]")
    tgt = np.asarray(pd.to_datetime(pd.Series(list(idx))).values, dtype="datetime64[D]")
    order = np.argsort(d, kind="mergesort")
    d = d[order]
    span = np.timedelta64(int(days), "D")
    if forward:
        lo = np.searchsorted(d, tgt, side="right")
        hi = np.searchsorted(d, tgt + span, side="right")
    else:
        lo = np.searchsorted(d, tgt - span, side="right")
        hi = np.searchsorted(d, tgt, side="right")
    if how == "count":
        res = (hi - lo).astype(float)
    else:
        v = np.nan_to_num(
            np.asarray(pd.to_numeric(pd.Series(list(values)), errors="coerce"), dtype=float)[order],
            nan=0.0)
        if how == "sum":
            c = np.concatenate([[0.0], np.cumsum(v)])
            res = c[hi] - c[lo]
        elif how == "max":
            res = np.array([v[i:j].max() if j > i else np.nan for i, j in zip(lo, hi)])
        else:
            raise ValueError(f"未知聚合方式: {how}")
    return pd.Series(res, index=idx, dtype=float)


def _mask_before(s: pd.Series, first) -> pd.Series:
    """源覆盖日之前置空：避免把「源尚未有数据」误读成 0（对家数/金额口径尤其致命）。"""
    if first is None or (isinstance(first, float) and np.isnan(first)):
        return s
    f = pd.Timestamp(first)
    return s.where(pd.DatetimeIndex([pd.Timestamp(d) for d in s.index]) >= f)


def _event_indicators(conn, idx) -> dict:
    """情绪事件指标（CN）：IPO / 基金发行 / 解禁 / 董监高增减持。

    全部为「公告先行」或「已实现」口径，无前视：
      · IPO 家数/破发率  → list_date 已知；
      · 基金成立份额     → setup_date 已知（权益类=剔除债/固收/稳健/货币）；
      · 未来解禁**股数** → 排期公告先行（市值口径在解禁前不可知，故不用未来市值）；
      · 已解禁市值       → 解禁后才可算，只看过去窗口；
      · 董监高净减持     → 公告日已知（仅事件级临时公告含成交均价）。
    """
    out: dict[str, pd.Series] = {}

    # ① IPO
    ipo = _q(conn, """SELECT list_date, is_break_issue FROM regime.ipo_event
                      WHERE list_date IS NOT NULL""")
    if not ipo.empty:
        d = pd.to_datetime(ipo["list_date"])
        out["SENT.IPO_COUNT_30D"] = _mask_before(
            _win_agg(d, None, idx, 30, "count"), d.min())
        n90 = _win_agg(d, None, idx, 90, "count")
        b90 = _win_agg(d, ipo["is_break_issue"].fillna(False).astype(float), idx, 90, "sum")
        out["SENT.IPO_BREAK_RATE_90D"] = _mask_before(
            b90 / n90.replace(0, np.nan) * 100, d.min())

    # ② 基金发行（权益类）
    fund = _q(conn, """SELECT setup_date, issue_share, fund_type
                       FROM regime.fund_issuance_event
                       WHERE setup_date IS NOT NULL AND issue_share IS NOT NULL""")
    if not fund.empty:
        eq = fund[~fund["fund_type"].fillna("").str.contains("债|固收|稳健|货币")]
        if not eq.empty:
            fd = pd.to_datetime(eq["setup_date"])
            out["SENT.FUND_ISSUANCE_60D"] = _mask_before(
                _win_agg(fd, eq["issue_share"], idx, 60, "sum"), fd.min())
            out["SENT.FUND_MAX_60D"] = _mask_before(
                _win_agg(fd, eq["issue_share"], idx, 60, "max"), fd.min())

    # ③ 解禁（未来股数 / 过去已实现市值）
    ul = _q(conn, """SELECT unlock_date, unlock_shares, unlock_value
                     FROM regime.unlock_schedule WHERE unlock_date IS NOT NULL""")
    if not ul.empty:
        ud = pd.to_datetime(ul["unlock_date"])
        shares_yi = pd.to_numeric(ul["unlock_shares"], errors="coerce").fillna(0) / 1e8
        value_yi = pd.to_numeric(ul["unlock_value"], errors="coerce").fillna(0) / 1e8
        out["SENT.UNLOCK_NEXT_30D_SHARES"] = _mask_before(
            _win_agg(ud, shares_yi, idx, 30, "sum", forward=True), ud.min())
        out["SENT.UNLOCK_PAST_30D_VALUE"] = _mask_before(
            _win_agg(ud, value_yi, idx, 30, "sum"), ud.min())

    # ④ 董监高净减持（事件级）
    ins = _q(conn, """SELECT ann_date, direction, event_value FROM regime.insider_trade
                      WHERE data_kind = '临时公告' AND ann_date IS NOT NULL
                        AND event_value IS NOT NULL""")
    if not ins.empty:
        idt = pd.to_datetime(ins["ann_date"])
        m_sell, m_buy = ins["direction"] == "SELL", ins["direction"] == "BUY"
        sell = _win_agg(idt[m_sell], ins.loc[m_sell, "event_value"], idx, 60, "sum")
        buy = _win_agg(idt[m_buy], ins.loc[m_buy, "event_value"], idx, 60, "sum")
        out["SENT.INSIDER_NET_SELL_60D"] = _mask_before((sell - buy) / 1e8, idt.min())

    return out


def _sector_indicators(conn, idx) -> dict:
    """行业层指标（CN，第④层）：申万一级行业指数宽度/动量分化 + 同花顺行业资金流。

    数据源
      · `daily_benchmark` 的 `CN.SW*`（31 个申万一级行业，1999-12 起，见 backfill_sw_industry.py）
        —— 本层**只用价格类字段**：源侧成交量/成交额单位与个股口径不同（亿量级），
           跨表与个股混算会失真（脚本 docstring 已标注）。
      · `regime.sector_fund_flow`（同花顺，历史从上线起逐日累积）。

    相对基准的选择：库内**无沪深300**（只有上证/深证成指），且行业轮动分析的标准做法是
    比行业等权均值 —— 故 RS_DISPERSION 用「各行业 20 日收益 − 行业等权均值」的横截面标准差，
    衡量轮动/分化强度，不引入外部基准。
    """
    out: dict[str, pd.Series] = {}
    if not len(idx):
        return out
    tgt = pd.DatetimeIndex([pd.Timestamp(d) for d in idx])
    start, end = min(idx), max(idx)

    def _dated(s: pd.Series) -> pd.Series:
        """对齐到交易日并**转回 datetime.date 索引**：全模块系列统一 date 索引
        （曾误用 Timestamp 索引 → `_clip` 里 date 与 Timestamp 比较抛 TypeError）。"""
        return pd.Series(s.reindex(tgt).to_numpy(dtype=float), index=idx)

    # ① 申万一级行业指数（多取 90 天以支持 20 日动量与当日涨跌幅的窗口）
    sw = _q(conn, """SELECT bench_code, trade_date, last_price FROM daily_benchmark
                     WHERE bench_code LIKE 'CN.SW%%' AND trade_date <= %s AND trade_date >= %s
                     ORDER BY trade_date""",
            [end, start - datetime.timedelta(days=90)])
    if not sw.empty:
        piv = sw.pivot_table(index="trade_date", columns="bench_code", values="last_price")
        piv.index = pd.DatetimeIndex(piv.index)
        piv = piv.sort_index()
        ret1 = (piv / piv.shift(1) - 1) * 100
        ret20 = (piv / piv.shift(20) - 1) * 100
        n_valid = piv.notna().sum(axis=1).replace(0, np.nan)
        out["SECTOR.UP_RATIO"] = _dated((ret1 > 0).sum(axis=1) / n_valid * 100)
        out["SECTOR.MOM_20D_MEDIAN"] = _dated(ret20.median(axis=1))
        excess = ret20.sub(ret20.mean(axis=1), axis=0)      # 相对行业等权均值
        out["SECTOR.RS_DISPERSION"] = _dated(excess.std(axis=1))

    # ② 行业估值（快照表；源无历史 → 逐日累积，此处只做市场级中位数）
    val = _q(conn, """SELECT snapshot_date, pe_ttm, pb FROM regime.sector_valuation_snapshot
                      WHERE snapshot_date <= %s""", [end])
    if not val.empty:
        val["snapshot_date"] = pd.to_datetime(val["snapshot_date"])
        out["SECTOR.PE_TTM_MEDIAN"] = _dated(val.groupby("snapshot_date")["pe_ttm"].median())
        out["SECTOR.PB_MEDIAN"] = _dated(val.groupby("snapshot_date")["pb"].median())

    # ③ 行业资金流（同花顺，亿元）
    flow = _q(conn, """SELECT trade_date, net_inflow FROM regime.sector_fund_flow
                       WHERE trade_date <= %s ORDER BY trade_date""", [end])
    if not flow.empty:
        flow["trade_date"] = pd.to_datetime(flow["trade_date"])
        net = _dated(flow.groupby("trade_date")["net_inflow"].sum())
        up = _dated(flow.assign(pos=(flow["net_inflow"] > 0))
                    .groupby("trade_date")["pos"].sum()
                    / flow.groupby("trade_date")["net_inflow"].count().replace(0, np.nan)
                    * 100)
        out["SECTOR.FLOW_NET"] = net
        out["SECTOR.FLOW_UP_RATIO"] = up
        # 近 5 日累计：按交易日位置滚动；min_periods=5 —— 数据不足 5 日不给值，
        # 避免把部分窗口当完整窗口用（源无历史，历史逐日累积）
        out["SECTOR.FLOW_NET_5D"] = net.rolling(5, min_periods=5).sum()

    return out


SECTOR_FLOW_MIN_CORR = 0.6    # 资金流汇总只纳入：confirmed 映射 或 corr ≥ 此阈值


def calc_sector_daily(conn, start: datetime.date, end: datetime.date,
                      warmup_days: int = 150) -> list:
    """逐行业日频横截面（第④层）→ `regime.sector_daily` 行列表。

    内容：申万一级指数价格/涨跌/20·60 日收益 + **20 日相对强度与排名**
          + 映射汇总的同花顺行业资金流（净额、近5日、排名、参与汇总的行业数）。
    相对强度的口径：rs_20d = 行业 20 日收益 − 31 行业等权均值 → **横截面均值恒为 0**，
    只看相对排序、不看绝对水平（避免把「全市场普涨」误读成行业强势）。

    资金流汇总的准入：只纳入 `sector_mapping.confirmed=true`（规则/人工确认）
    或 corr ≥ SECTOR_FLOW_MIN_CORR 的映射 —— 相关性已被证明会被风格因子误导，
    未确认的低置信映射宁可不汇总（`mapped_ths_count` 会显示实际参与数）。
    """
    w_start = start - datetime.timedelta(days=warmup_days)
    px = _q(conn, """SELECT bench_code, trade_date, last_price, turnover FROM daily_benchmark
                     WHERE bench_code LIKE 'CN.SW%%' AND trade_date BETWEEN %s AND %s
                     ORDER BY trade_date""", [w_start, end])
    if px.empty:
        return []
    px = px.assign(sw_code=px["bench_code"].str.replace("CN.SW", "", regex=False))
    close = px.pivot_table(index="trade_date", columns="sw_code", values="last_price").sort_index()
    turn = px.pivot_table(index="trade_date", columns="sw_code", values="turnover").sort_index()
    close.index = pd.DatetimeIndex(close.index)
    turn.index = pd.DatetimeIndex(turn.index)
    ret1 = (close / close.shift(1) - 1) * 100
    ret20 = (close / close.shift(20) - 1) * 100
    ret60 = (close / close.shift(60) - 1) * 100
    rs20 = ret20.sub(ret20.mean(axis=1), axis=0)          # 横截面均值恒为 0
    rs_rank = rs20.rank(axis=1, ascending=False, method="min")

    names = {}
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT bench_code, bench_name FROM daily_benchmark "
                    "WHERE bench_code LIKE 'CN.SW%'")
        names = {k.replace("CN.SW", ""): (v or "").replace("申万-", "") for k, v in cur.fetchall()}

    # 资金流：映射汇总（同一条 THS 行业只算一次；多条 THS → 同一 SW 则求和）
    flow = _q(conn, """SELECT f.trade_date, m.sw_code, sum(f.net_inflow) AS net, count(*) AS n
                       FROM regime.sector_fund_flow f
                       JOIN regime.sector_mapping m ON m.ths_name = f.sector_name
                       WHERE f.trade_date <= %s AND (m.confirmed OR m.corr >= %s)
                       GROUP BY 1, 2""", [end, SECTOR_FLOW_MIN_CORR])
    net_w = flow_5 = n_w = None
    if not flow.empty:
        flow["trade_date"] = pd.to_datetime(flow["trade_date"])
        net_w = flow.pivot_table(index="trade_date", columns="sw_code", values="net").sort_index()
        n_w = flow.pivot_table(index="trade_date", columns="sw_code", values="n").sort_index()
        flow_5 = net_w.rolling(5, min_periods=5).sum()     # 不足 5 日不给值

    dates = [d for d in close.index if start <= d.date() <= end]
    rows = []
    for dt in dates:
        d = dt.date()
        r20, rs_row, rk = ret20.loc[dt], rs20.loc[dt], rs_rank.loc[dt]
        mrk = r20.rank(ascending=False, method="min")
        frk = (net_w.loc[dt].rank(ascending=False, method="min")
               if net_w is not None and dt in net_w.index else None)
        for code in close.columns:
            v = lambda s: None if s is None or pd.isna(s) else float(s)
            rows.append({
                "trade_date": d, "sw_code": code, "sw_name": names.get(code),
                "close": v(close.loc[dt, code]), "change_pct": v(ret1.loc[dt, code]),
                "ret_20d": v(r20[code]), "ret_60d": v(ret60.loc[dt, code]),
                "rs_20d": v(rs_row[code]), "rs_rank": v(rk[code]), "mom_rank": v(mrk[code]),
                "turnover": v(turn.loc[dt, code]) if dt in turn.index else None,
                "fund_flow_net": v(net_w.loc[dt, code]) if net_w is not None and dt in net_w.index and code in net_w.columns else None,
                "fund_flow_net_5d": v(flow_5.loc[dt, code]) if flow_5 is not None and dt in flow_5.index and code in flow_5.columns else None,
                "flow_rank": v(frk[code]) if frk is not None and code in frk.index else None,
                "mapped_ths_count": int(n_w.loc[dt, code]) if n_w is not None and dt in n_w.index and code in n_w.columns and not pd.isna(n_w.loc[dt, code]) else 0,
            })
    return rows


def _write_sector_daily(conn, start: datetime.date, end: datetime.date,
                        write: bool = True) -> int:
    """计算并写入 `regime.sector_daily`（仅 CN）。返回写入行数。"""
    rows = calc_sector_daily(conn, start, end)
    if not write or not rows:
        return len(rows)
    now = datetime.datetime.now(TZ_CN)
    for r in rows:
        r["updated_at"] = now
    bulk_upsert(conn, "regime.sector_daily", rows, conflict_cols=["trade_date", "sw_code"])
    conn.commit()
    return len(rows)


def _risk_from_index(bdf: pd.DataFrame, date_col: str, price_col: str, market: str) -> dict:
    """从指数日线算 RV20 / RV60 / MAXDD_250。"""
    s = _series_from_df(bdf, date_col, price_col)
    if s.empty:
        return {}
    ret = np.log(s / s.shift(1))
    return {
        "RISK.RV20": ret.rolling(20, min_periods=15).std() * np.sqrt(252) * 100,
        "RISK.RV60": ret.rolling(60, min_periods=40).std() * np.sqrt(252) * 100,
        "RISK.MAXDD_250": (s / s.rolling(250, min_periods=60).max() - 1) * 100,
    }


# ── 合成 ────────────────────────────────────────────────────────────────────
def build_composites(pct: pd.DataFrame, raw: pd.DataFrame, market: str) -> pd.DataFrame:
    """温度计 / 风险指数 / FSI / 季节 + 顶底信号计数（全部向量化）。"""
    out = pd.DataFrame(index=pct.index)

    def _score(members):
        cols = []
        for code, sign in members:
            if code in pct.columns:
                cols.append(pct[code] if sign > 0 else 100 - pct[code])
        return pd.concat(cols, axis=1).mean(axis=1, skipna=True) if cols else pd.Series(np.nan, index=pct.index)

    out["thermometer"] = _score(COMPOSITES["thermometer"]["members"])
    out["risk_score"] = _score(COMPOSITES["risk_score"]["members"])
    out["fsi"] = _score(COMPOSITES["fsi"]["members"])

    # 季节（v2 规则版：估值水位 + 宽度水位/趋势 + 信用脉冲；ERP 分位维度待接入）
    # 规则来自 market_profile.md §8.1：冲突时以宽度趋势为准；「春」必须信用脉冲转正。
    v = pct.get("VAL.PE_TTM_MEDIAN", pd.Series(np.nan, index=pct.index))
    w = pct.get("BREADTH.PCT_ABOVE_MA200", pd.Series(np.nan, index=pct.index))
    ci_pct = pct.get("MACRO.CREDIT_IMPULSE", pd.Series(np.nan, index=pct.index))
    ci_raw = raw.get("MACRO.CREDIT_IMPULSE", pd.Series(np.nan, index=pct.index))
    w_chg = w - w.shift(20)
    ci_up = ci_raw > 0                                       # 信用脉冲为正（扩张）
    ci_fall = (ci_raw < ci_raw.shift(6)) & (ci_raw.shift(6) > 0)   # 信用脉冲见顶回落
    spring = (w < 25) & (v < 25) & (w_chg > 0) & ci_up
    autumn = ((v >= 75) & (w_chg < 0)) | ((v >= 60) & (w_chg < 0) & ci_fall)
    season = pd.Series(index=pct.index, dtype=object)
    season[(w >= 25)] = "夏"
    season[(w < 25)] = "冬"
    season[spring] = "春"
    season[autumn] = "秋"
    out["season"] = season
    out["season_score"] = pd.concat([100 - v, w, ci_pct], axis=1).mean(axis=1, skipna=True)

    # 顶/底信号（market_profile.md §8 的可算子集）
    def _pct_c(code, default=np.nan):
        return pct.get(code, pd.Series(default, index=pct.index))

    def _raw_c(code, default=np.nan):
        return raw.get(code, pd.Series(default, index=pct.index))

    ma200 = _raw_c("BREADTH.PCT_ABOVE_MA200")
    rv20 = _pct_c("RISK.RV20")
    top = {
        "VAL_PE_GT80": _pct_c("VAL.PE_TTM_MEDIAN") > 80,
        "ERP_LT20": _pct_c("VAL.ERP") < 20,
        "TURNOVER_GT90": _pct_c("FLOW.TURNOVER_RATIO") > 90,
        "MARGIN_GT90": _pct_c("FLOW.MARGIN_RATIO") > 90,
        "MA200_ROLLOVER": (ma200.shift(20) > 60) & (ma200 < ma200.shift(20) - 10),
        "RV_JUMP": (rv20.shift(20) < 30) & (rv20 > 60),
    }
    bottom = {
        "VAL_PE_LT20": _pct_c("VAL.PE_TTM_MEDIAN") < 20,
        "ERP_GT80": _pct_c("VAL.ERP") > 80,
        "TURNOVER_LT20": _pct_c("FLOW.TURNOVER_RATIO") < 20,
        "BELOW_PB_GT80": _pct_c("VAL.PCT_BELOW_PB_1") > 80,
        "MA200_RECOVER": (ma200 < 15) & (ma200 > ma200.shift(20)),
        "RV_FALL": (rv20.shift(20) > 80) & (rv20 < rv20.shift(20) - 20),
    }
    out["top_signal_cnt"] = pd.DataFrame(top).sum(axis=1)
    out["bottom_signal_cnt"] = pd.DataFrame(bottom).sum(axis=1)
    out["_top_hits"] = pd.DataFrame(top).apply(lambda r: ",".join(r.index[r.values]), axis=1)
    out["_bottom_hits"] = pd.DataFrame(bottom).apply(lambda r: ",".join(r.index[r.values]), axis=1)
    return out


# ── 落库 ────────────────────────────────────────────────────────────────────
def _ensure_dict(conn):
    rows = []
    for meta in INDICATORS:
        rows.append({
            "indicator_code": meta["code"], "indicator_name": meta["name"],
            "layer": meta["layer"], "dimension": meta["dimension"],
            "market_scope": meta["market_scope"], "unit": meta["unit"],
            "direction": meta["direction"], "freq": meta["freq"],
            "source": meta.get("source", ""), "formula": meta["formula"],
            "is_active": meta.get("active", True), "remark": meta.get("remark"),
            "updated_at": datetime.datetime.now(TZ_CN),
        })
    bulk_upsert(conn, "regime.indicator_dict", rows, conflict_cols=["indicator_code"],
                skip_null_updates=True)
    return len(rows)


def _load_stored_raw(conn, market: str) -> pd.DataFrame:
    """读已入库的 raw_value 历史（增量场景下分位必须建立在全历史上）。"""
    df = pd.read_sql_query(
        "SELECT indicator_code, trade_date, raw_value FROM regime.indicator_value "
        "WHERE market = %s ORDER BY trade_date", conn, params=[market])
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df.pivot_table(index="trade_date", columns="indicator_code",
                          values="raw_value", aggfunc="last")


def _write_market(conn, market: str, series_map: dict, start: datetime.date,
                  write: bool) -> dict:
    """分位化（全历史 expanding）+ 合成 + 写库，返回统计。

    增量正确性：分位与需要前 20/250 日窗口的合成（季节趋势、顶底信号）都必须建立在
    **全历史**上，因此把已入库 raw_value 与本次新算值拼接后再算，只写 [start, end] 段。
    """
    new_raw = pd.DataFrame({c: s for c, s in series_map.items() if s is not None and len(s)})
    if new_raw.empty:
        return {"rows": 0, "dates": 0}
    new_raw = new_raw.sort_index()
    codes = [m["code"] for m in INDICATORS]

    hist = _load_stored_raw(conn, market)
    if not hist.empty:
        hist = hist[hist.index < new_raw.index.min()]          # 与本次窗口重叠段用新算值
        hist = hist[[c for c in hist.columns if c in codes]]
        combined = pd.concat([hist, new_raw])
    else:
        combined = new_raw
    for c in codes:
        if c not in combined.columns:
            combined[c] = np.nan
    combined = combined.sort_index()

    pct = pd.DataFrame({c: _expanding_percentile(combined[c]) for c in combined.columns},
                       index=combined.index)
    comp = build_composites(pct, combined, market)

    write_idx = [d for d in combined.index if isinstance(d, datetime.date) and d >= start]
    if not write:
        last = write_idx[-1] if write_idx else None
        return {"rows": 0, "dates": len(write_idx),
                "preview": (last, comp.loc[last].to_dict() if last else None)}

    now = datetime.datetime.now(TZ_CN)
    iv_rows = []
    for code in codes:
        if code not in combined.columns:
            continue
        s_raw, s_pct = combined[code], pct[code]
        for d in write_idx:
            v, p = s_raw.get(d), s_pct.get(d)
            if (v is None or pd.isna(v)) and (p is None or pd.isna(p)):
                continue
            iv_rows.append({
                "indicator_code": code, "market": market, "trade_date": d,
                "raw_value": None if v is None or pd.isna(v) else round(float(v), 6),
                "percentile": None if p is None or pd.isna(p) else round(float(p), 2),
                "updated_at": now,
            })
    bulk_upsert(conn, IV_TABLE, iv_rows,
                conflict_cols=["indicator_code", "market", "trade_date"], skip_null_updates=True)

    mr_rows = []
    for d in write_idx:
        r = comp.loc[d]
        detail = {
            "top": r["_top_hits"], "bottom": r["_bottom_hits"],
            "members": {k: (None if pd.isna(r[k]) else round(float(r[k]), 2))
                        for k in ("thermometer", "risk_score", "fsi")},
        }
        mr_rows.append({
            "trade_date": d, "market": market,
            "thermometer": _r2(r["thermometer"]), "season": r["season"],
            "season_score": _r2(r["season_score"]), "risk_score": _r2(r["risk_score"]),
            "fsi": _r2(r["fsi"]), "risk_appetite": None,
            "top_signal_cnt": int(r["top_signal_cnt"]), "bottom_signal_cnt": int(r["bottom_signal_cnt"]),
            "detail": detail, "updated_at": now,
        })
    bulk_upsert(conn, MR_TABLE, mr_rows, conflict_cols=["trade_date", "market"])
    return {"rows": len(iv_rows), "dates": len(write_idx), "composite_rows": len(mr_rows)}


def _r2(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), 2)


# ── 入口 ────────────────────────────────────────────────────────────────────
def run(codes=None, ctx=None, market: str = None, start: datetime.date = None,
        end: datetime.date = None, backfill: bool = False, write: bool = True) -> dict:
    """计算入口：默认增量（最近 45 天）；backfill=True 时按市场数据起点分块全量。

    分块原因：逐股滚动（pandas）与横截面聚合在千万行级别内存/耗时都会放大，
    按 4 年一块处理（每块自带 400 天预热），单块内存可控、失败可定位、可中断续跑。
    """
    end = end or datetime.date.today()
    markets = [market] if market else ["CN", "HK"]
    data_start = {"CN": datetime.date(1990, 1, 1), "HK": datetime.date(2019, 1, 1)}

    stats = {}
    with get_conn() as conn:
        n_dict = _ensure_dict(conn)
        print(f"  指标字典登记/更新 {n_dict} 条")
        for mkt in markets:
            s0 = start or (data_start[mkt] if backfill else end - datetime.timedelta(days=45))
            s0 = max(s0, data_start[mkt])
            if s0 > end:
                continue
            chunks = _chunks(s0, end, years=4) if (end - s0).days > 730 else [(s0, end)]
            t0 = datetime.datetime.now()
            tot = {"rows": 0, "dates": 0, "composite_rows": 0, "sector_rows": 0}
            print(f"  ── {mkt}: {s0} ~ {end}（{len(chunks)} 块）──")
            for cs, ce in chunks:
                try:
                    series = calc_cn(conn, cs, ce) if mkt == "CN" else calc_hk(conn, cs, ce)
                except Exception as e:
                    print(f"    ❌ {cs}~{ce} 计算失败: {type(e).__name__}: {e}")
                    continue
                if not series:
                    continue
                st = _write_market(conn, mkt, series, cs, write)
                for k in tot:
                    tot[k] += st.get(k, 0)
                line = f"    · {cs}~{ce}: {st['dates']} 日 / {st['rows']} 行"
                # 逐行业横截面（第④层，仅 CN）：一市场 31 行业 × 交易日
                if mkt == "CN":
                    n_sec = _write_sector_daily(conn, cs, ce, write)
                    tot["sector_rows"] += n_sec
                    line += f" / 行业 {n_sec} 行"
                print(f"{line}（累计 {round((datetime.datetime.now() - t0).total_seconds(), 1)}s）")
            tot["elapsed_s"] = round((datetime.datetime.now() - t0).total_seconds(), 1)
            stats[mkt] = tot
            print(f"  ✅ {mkt}: 合计 {tot['dates']} 个交易日，indicator_value {tot['rows']} 行"
                  f"，合成 {tot['composite_rows']} 行，用时 {tot['elapsed_s']}s")
    return stats


def _chunks(start: datetime.date, end: datetime.date, years: int = 4) -> list:
    """按 N 年切块（闭区间）。"""
    out, s = [], start
    while s <= end:
        e = min(datetime.date(s.year + years - 1, 12, 31), end)
        out.append((s, e))
        s = e + datetime.timedelta(days=1)
    return out


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["CN", "HK"], default=None)
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", default=None)
    ap.add_argument("--backfill", action="store_true", help="全历史回填")
    ap.add_argument("--no-write", action="store_true", help="只算不写（调试）")
    args = ap.parse_args()

    print("=" * 60)
    print("市场状态计算层 → regime.indicator_value / market_regime_daily")
    print("=" * 60)
    run(market=args.market,
        start=datetime.date.fromisoformat(args.start) if args.start else None,
        end=datetime.date.fromisoformat(args.end) if args.end else None,
        backfill=args.backfill,
        write=not args.no_write)

    if not args.no_write:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT market, count(*), count(percentile),
                                      min(trade_date), max(trade_date)
                               FROM regime.indicator_value GROUP BY 1 ORDER BY 1""")
                print("\n── indicator_value ──")
                for r in cur.fetchall():
                    print(f"  {r[0]}: {r[1]:,} 行（有分位 {r[2]:,}）  {r[3]} ~ {r[4]}")
                cur.execute("""SELECT market, count(*), max(trade_date) FROM regime.market_regime_daily
                               GROUP BY 1 ORDER BY 1""")
                print("── market_regime_daily ──")
                for r in cur.fetchall():
                    print(f"  {r[0]}: {r[1]:,} 行，最新 {r[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
