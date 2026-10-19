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
    dict(code="BREADTH.ADV_RATIO", name="上涨家数占比", scope="MARKET", dimension="宽度",
         remark="定义：当日收涨家数占有行情家数的比例，市场参与广度最直接的读数　·　判读：>50 多数上涨、<50 多数下跌；指数不跌而本值持续走低=宽度背离（顶部信号），持续 >70=普涨过热　·　数据：已剔除停牌与 B 股/仙股；CN+HK",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="上涨家数 / 有行情家数（剔除停牌与 B 股 / 仙股）"),
    dict(code="BREADTH.MCCLELLAN", name="麦克莱林振荡", scope="MARKET", dimension="宽度",
         remark="定义：涨跌家数净差的快慢 EMA 差（EMA19−EMA39），A/D 线的动量读数　·　判读：>0 上涨动能占优、<0 下跌动能占优；零轴上下穿为短期拐点，宜与 ADV_RATIO 同用　·　数据：零轴附近震荡频繁，单独使用噪声大",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="EMA19(涨-跌) − EMA39(涨-跌)"),
    dict(code="BREADTH.ADV_MINUS_DEC", name="涨跌家数净差", scope="MARKET", dimension="宽度",
         remark="定义：当日涨家数减跌家数的绝对家数（腾落指数的每日增量）　·　判读：>0 涨多于跌；应看其 20 日均值方向而非单日极值　·　数据：A/D Line 累计值由消费方自行累加，本指标只给增量，避免增量重算时口径漂移",
         market_scope="CN+HK", unit="count", direction="high_good", freq="day",
         formula="涨家数 − 跌家数（腾落指数的每日增量；AD Line 累计值由消费方自行累加，避免累计序列在增量重算时口径漂移）"),
    dict(code="BREADTH.NEW_HIGH_LOW_60", name="60日新高−新低家数差(占比)", scope="MARKET", dimension="宽度",
         remark="定义：创 60 日新高与创新低的家数净差占有行情家数比例　·　判读：>0 创新高者多于创新低者=趋势健康；持续为负而指数走高=内在动能衰竭　·　数据：需 60 日以上历史，新上市股不计入",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="(60日新高家数 − 60日新低家数) / 有效样本家数 * 100"),
    dict(code="BREADTH.PCT_ABOVE_MA60", name="站上 MA60 占比", scope="MARKET", dimension="宽度",
         remark="定义：收盘价站上 60 日均线的家数占比（中期趋势的参与广度）　·　判读：>50 中期趋势偏多；>80 偏过热、<20 偏超跌，须配合趋势方向使用　·　数据：仅统计有 60 日以上历史的股票",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="收盘 > MA60 的家数 / 有 60 日以上历史的家数 * 100"),
    dict(code="BREADTH.PCT_ABOVE_MA200", name="站上 MA200 占比", scope="MARKET", dimension="宽度",
         remark="定义：收盘价站上 200 日均线的家数占比（长期牛熊分界的参与广度）　·　判读：>50 长期趋势偏多；从 <15 回升并上穿 15 是底部信号清单的构成项之一　·　数据：仅统计有 200 日以上历史的股票",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="收盘 > MA200 的家数 / 有 200 日以上历史的家数 * 100"),
    dict(code="BREADTH.EQ_VS_CAP_20D", name="等权−市值加权 20日累计收益差", scope="MARKET", dimension="宽度",
         remark="定义：等权组合与市值加权组合近 20 日累计收益之差　·　判读：>0 小票/等权跑赢（普涨、散户活跃），<0 权重股主导（结构性行情）　·　数据：依赖总市值字段 → 2018 起；仅 CN",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         formula="Σ20(等权日收益 − 市值加权日收益)；依赖市值字段 → 2018 起"),
    dict(code="BREADTH.LIMIT_NET", name="涨跌停家数差(占比)", scope="MARKET", dimension="宽度",
         remark="定义：涨停与跌停家数净差占有行情家数比例，极端情绪强度　·　判读：大幅为正=情绪亢奋（涨停潮），大幅为负=恐慌；极值后常伴反转　·　数据：涨跌停按板块阈值判定（主板±9.8 / 双创±19.6），ST 未单独处理会被误判；仅 CN",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         formula="(涨停家数 − 跌停家数) / 有行情家数 * 100；涨跌停按板块阈值判定（主板±9.8 / 双创±19.6，ST 未单独处理）"),
    # —— 估值 Valuation ——
    dict(code="VAL.PE_TTM_MEDIAN", name="全市场 PE-TTM 中位数", scope="MARKET", dimension="估值",
         remark="定义：全市场个股 PE-TTM 的**中位数**（剔除 PE<=0），估值水位最稳健口径　·　判读：历史分位 >80=高估、<20=低估；中位数不受少数巨亏/巨盈股扭曲，比均值口径可靠　·　数据：个股级字段起点 CN 2018 / HK 2019",
         market_scope="CN+HK", unit="x", direction="high_risk", freq="day",
         formula="中位数(PE-TTM)，剔除 PE<=0；个股级字段起点 CN 2018 / HK 2019"),
    dict(code="VAL.PB_MEDIAN", name="全市场 PB 中位数", scope="MARKET", dimension="估值",
         remark="定义：全市场个股 PB 的**中位数**（剔除 PB<=0）　·　判读：分位 >80=高估、<20=低估；周期股/重资产行业 PB 比 PE 更稳，两者宜交叉验证　·　数据：个股级字段起点 CN 2018 / HK 2019",
         market_scope="CN+HK", unit="x", direction="high_risk", freq="day",
         formula="中位数(PB)，剔除 PB<=0"),
    dict(code="VAL.PCT_BELOW_PB_1", name="破净股占比", scope="MARKET", dimension="估值",
         remark="定义：破净股（PB<1）占有正 PB 家数的比例　·　判读：占比越高越接近市场底部，>80 是底部信号清单构成项之一；但熊市中可持续高位，须配合宽度趋势确认　·　数据：分母为 PB>0 家数（剔除负资产）",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="PB < 1 的家数 / PB > 0 的家数 * 100（底部信号）"),
    dict(code="VAL.ERP", name="股权风险溢价 ERP", scope="ALLOC", dimension="估值",
         remark="定义：股权风险溢价 = 盈利收益率 − 无风险利率（1/PE中位数 − 10Y 国债），股相对债的性价比　·　判读：ERP 越高=股票相对债券越有吸引力，分位 >80 对应历史性底部区域；CN 用中债 10Y、HK 用美债 10Y，两市场不可直接比数值　·　数据：日频口径，依赖个股聚合 PE 中位数 → CN 2018 / HK 2019 起；**长周期（2006 起）位置请看 VAL.ERP_LG**（乐咕乐股口径，两者刻意不拼接，原因见该指标）",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="day",
         formula="1/PE中位数*100 − 无风险利率（CN 用中债国债10Y；HK 用美债10Y）"),
    dict(code="VAL.ERP_LG", name="股权风险溢价 ERP(乐咕乐股口径,月频)", scope="ALLOC", dimension="估值",
         remark="定义：同 VAL.ERP（E/P − 10Y 国债），但分子用**乐咕乐股全A PE-TTM 中位数**（月频，2006-03 起）——提供**口径一致的 20 年长历史**，覆盖 2008/2015/2018 三轮周期　·　判读：与 VAL.ERP 同向；**看长周期位置用它**（样本约 250 个月），看当下日频变化用 VAL.ERP　·　⚠ **刻意不与 VAL.ERP 拼接**：两口径 PE 的比值**不稳定**（实测 2018→2026 从 0.73 单调漂到 1.08，8 年漂移 48%），ERP 之差达均值 0.44pp / 标准差 0.40pp —— 已接近 VAL.ERP 自身波动（σ=0.50pp）；拼接会同时引入水平跳变与**伪趋势**，且用重叠段定校准系数会前视　·　数据：仅 CN",
         market_scope="CN", unit="pct", direction="high_good", freq="month",
         formula="100/CN.VAL_PE_MEDIAN − CN.BOND_10Y（乐咕乐股全A PE-TTM 中位数，月频；债券取当日或之前最近值）"),
    dict(code="VAL.ERP_Z", name="ERP 标准化偏离(z-score)", scope="ALLOC", dimension="估值",
         remark="定义：(ERP − ERP 自身历史均值) / ERP 自身历史标准差 —— Fed Model 的**「与历史均值比较」**口径（ERP = E/P − 无风险利率）　·　判读：>0 = 股债性价比优于自身历史中枢（越高越有利于股票），>+2 为历史极值区　·　**与 VAL.ERP 的分位互补**：分位看排位（对分布形状不敏感），z-score 看偏离幅度（对极值敏感），两者显著背离时提示分布偏斜　·　数据：均值/标准差用**扩展窗口**（只用当日及之前，无前视），min_periods=250 → 自 ~2019-01 起有值；⚠ **不做历史分位**（二次标准化无意义）；⚠ 只基于日频口径的 VAL.ERP（不含乐咕乐股段）",
         market_scope="CN+HK", unit="sigma", direction="high_good", freq="day",
         formula="(VAL.ERP − expanding_mean(VAL.ERP)) / expanding_std(VAL.ERP)，min_periods=250"),
    # —— 资金 / 情绪 Flow ——
    dict(code="FLOW.TURNOVER_RATIO", name="市场换手率", scope="MARKET", dimension="资金",
         remark="定义：成交额占流通市值（CN）/总市值（HK）的比例，即市场换手率　·　判读：升温=资金活跃、情绪上行（温度计成分 sign +1）；分位 >80 偏过热、<20 缩量地量　·　数据：依赖市值字段 → CN 2018 起",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="Σ成交额 / Σ流通市值(CN) 或 Σ总市值(HK) * 100；CN 依赖市值字段 → 2018 起"),
    dict(code="FLOW.MARGIN_RATIO", name="两融余额占流通市值比", scope="MARKET", dimension="资金",
         remark="定义：两融余额占流通市值比例，杠杆资金水位　·　判读：持续攀升=杠杆加码（助涨也助跌）；高位叠加宽度恶化是典型风险信号　·　数据：依赖流通市值字段 → 2018 起；仅 CN",
         market_scope="CN", unit="pct", direction="high_risk", freq="day",
         formula="Σ两融余额 / Σ流通市值 * 100（杠杆水位；2018 起）"),
    dict(code="FLOW.SOUTHBOUND_NET", name="南向净流入(亿港元)", scope="MARKET", dimension="资金",
         remark="定义：港股通南向资金当日净流入（亿港元），内地资金定价港股的边际力量　·　判读：持续净流入=内地资金定价权增强、利好港股流动性；单日大额须辨别是否为成分股调整所致　·　数据：数据 2026-04 起，样本短，分位需长窗口累积才有意义",
         market_scope="HK", unit="yi_hkd", direction="high_good", freq="day",
         formula="Σ est_net_inflow（daily_ggt_hold）；数据 2026-04 起，分位需长窗口累积"),
    dict(code="FLOW.NORTHBOUND_NET", name="北向净流入(亿元)", scope="MARKET", dimension="资金",
         remark="定义：北向资金当日净流入（亿元），外资配置 A 股的边际力量　·　判读：持续净流入=外资风险偏好回升，与人民币汇率、中美利差同向确认　·　数据：⚠ 官方 2024-08-16 后停止披露，**序列在断点终止无新值**，勿用于当期判断",
         market_scope="CN", unit="yi_cny", direction="high_good", freq="day",
         formula="daily_northbound_flow.net_inflow；⚠ 官方 2024-08-16 后停披露，本指标序列在断点终止"),
    dict(code="FLOW.SHORT_SELLING_RATIO", name="港股卖空成交占比", scope="MARKET", dimension="资金",
         remark="定义：港股卖空金额占全市场成交额比例，做空/对冲力量强度　·　判读：异常升高=看空或对冲需求上升，常伴波动放大；持续低位=缺乏做空制衡　·　数据：数据 2026-06 起，样本短；仅 HK",
         market_scope="HK", unit="pct", direction="high_risk", freq="day",
         formula="Σ沽空金额 / 全市场成交额 * 100；数据 2026-06 起"),
    # —— 风险 Risk ——
    dict(code="RISK.RV20", name="指数20日已实现波动率(年化)", scope="MARKET", dimension="风险",
         remark="定义：指数近 20 日已实现波动率（对数收益标准差年化）　·　判读：越高越危险；分位 >80=高波动环境，仓位与止损需相应调整；是温度计与 FSI 双料成分　·　数据：基准指数 CN=SH.000001、HK=HK.800000",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="std(log收益, 20) * sqrt(252) * 100（基准：CN=SH.000001，HK=HK.800000）"),
    dict(code="RISK.RV60", name="指数60日已实现波动率(年化)", scope="MARKET", dimension="风险",
         remark="定义：指数近 60 日已实现波动率（年化）　·　判读：同 RV20 但更平滑，反映中期波动 regime；RV20 明显高于 RV60=波动正在放大　·　数据：基准同 RV20；需 60 日以上历史",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="std(log收益, 60) * sqrt(252) * 100"),
    dict(code="RISK.MAXDD_250", name="近250日最大回撤", scope="MARKET", dimension="风险",
         remark="定义：近 250 个交易日内的最大回撤（负值）　·　判读：越接近 0 越健康；深度回撤（如 <-25%）=趋势受损，修复需要时间　·　数据：需 250 日历史；风险指数成分",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="min(close / rolling_max(close,250) − 1) * 100（负值）"),
    dict(code="RISK.EXTREME_DOWN_PCT", name="单日跌幅超3%家数占比", scope="MARKET", dimension="风险",
         remark="定义：单日跌幅超 3% 的家数占比，普跌/恐慌的横截面读数　·　判读：骤升=恐慌性抛售，往往对应短期超卖；与宽度指标同看更准　·　数据：阈值固定 3%，未按板块波动率分层（双创天然更易触发）",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="change_pct <= -3 的家数 / 有行情家数 * 100"),
    dict(code="RISK.SYNC_DOWN_20D", name="下跌家数占比20日均值", scope="MARKET", dimension="风险",
         remark="定义：下跌家数占比的 20 日均值，系统性同步下跌的代理　·　判读：持续高位=个股相关性上升、分散化失效，是真正的风险来源　·　数据：v1 用占比代理真实两两相关性（全量相关性计算成本高），非严格相关性指标",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         formula="mean20(下跌家数 / 有行情家数 * 100)；系统性同步下跌代理（真实两两相关性成本高，v1 用占比代理）"),
    # —— 宏观接入（来源 regime.macro_series，月度/日度值前向填充到交易日）——
    dict(code="MACRO.CREDIT_IMPULSE", name="信贷脉冲(社融存量/GDP 同比变化)", scope="MACRO", dimension="宏观",
         market_scope="CN+HK", unit="pp", direction="high_good", freq="month",
         remark="定义：信用流量/GDP 比率的同比变化（pp），即信用扩张的边际速度　·　判读：>0 信用加速扩张（利好风险资产），<0 信用收缩；领先经济与股市约 6-12 个月，是美林时钟春季判定的必要条件　·　数据：自 2021-02 起有值（存量口径 2019-01 + 流量需 12 个月）；旧「12M 滚动增量同比」代理的源已停更、值被冻结在 −3.21，本轮已换央行直连真值口径",
         formula="F_t/GDP_ttm_t − F_{t−12}/GDP_ttm_{t−12}，F = CN.TSF_STOCK 的 12 个月差分（亿元）；"
                 "GDP_ttm = CN.GDP 差分回单季后的滚动 4 季合计（PIT：只用当时已发布数据）"),
    dict(code="MACRO.TSF_STOCK_YOY", name="社会融资规模存量同比", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="month",
         remark="定义：社会融资规模存量的同比增长（%），中国信用扩张的总量读数　·　判读：持续上行=信用扩张周期；与信贷脉冲同向但更平滑（一个是存量增速、一个是边际变化），脉冲拐点通常领先存量同比　·　数据：2016-01 起；源按当期口径重算上年同期，跨 2019 口径断点的可比性优于水平值",
         formula="CN.TSF_STOCK_YOY（源：中国人民银行 统计数据 → 社会融资规模，htm 附件直连）"),
    dict(code="RISK.CREDIT_SPREAD", name="信用利差(中票AAA 10Y−国债10Y)", scope="MACRO", dimension="风险",
         remark="定义：AAA 级中短期票据与同期限国债的收益率差，信用风险的绝对定价　·　判读：走阔=违约担忧上升/风险偏好收缩，收窄=信用环境改善；与等级利差对照可分离「整体信用环境」与「纯风险偏好」　·　数据：长历史且稳定，是风险指数成分；**CN+HK**——HK 侧同样接入（§5.4 风险偏好指数的成分以 GLOBAL 为主，若 HK 缺信用成分会与 CN 侧口径不一致），但读数本身是中国信用环境",
         market_scope="CN+HK", unit="pp", direction="high_risk", freq="day",
         formula="CN.CREDIT_SPREAD_MTN_AAA_10Y；利差走阔=违约担忧/风险偏好下降（源：get_macro_daily 派生）"),
    dict(code="RISK.CREDIT_SPREAD_CHG20", name="信用利差 20日变化", scope="MACRO", dimension="风险",
         market_scope="CN+HK", unit="pp", direction="high_risk", freq="day",
         remark="定义：AAA 中票信用利差近 20 个交易日的变化（**pp，不是百分比**——利差本身以 % 计价，百分比变化在低位时会失真，见 MACRO.BOND_10Y_CHG20）　·　判读：**边际方向比水平更重要**——水平分位低说明「宽松已久」，而变化说明「是否仍在改善」；§4.2.1 要求投票一律用边际变化，此即信用组的投票依据　·　与水平指标的分工：水平用于「是否已进入危险区」（阈值判定），变化用于「方向」（投票）　·　数据：2011 起（受利差序列限制）；前 20 个交易日无值",
         formula="CN.CREDIT_SPREAD_MTN_AAA_10Y − 同序列(t−20)，单位 pp"),
    dict(code="RISK.TERM_SPREAD", name="期限利差(国债 10Y−1Y)", scope="MACRO", dimension="风险",
         remark="定义：国债 10Y 与 1Y 收益率之差（曲线陡峭度）　·　判读：**倒挂（低/负）为经典衰退预警**；走阔=复苏预期升温；风险指数中取反　·　数据：仅 CN",
         market_scope="CN", unit="pp", direction="high_good", freq="day",
         formula="CN.TERM_SPREAD_10Y_1Y；倒挂（低/负）为衰退预警"),
    dict(code="RISK.CREDIT_SPREAD_AA", name="信用利差(中票AA 5Y−国债5Y)", scope="MACRO", dimension="风险",
         market_scope="CN", unit="pp", direction="high_risk", freq="day",
         remark="定义：AA 级中短期票据与同期限国债收益率差，低等级绝对信用利差　·　判读：走阔=低等级信用风险定价上升；比 AAA 口径更敏感但更嘈杂　·　数据：⚠ 源只保留最近 3 个交易日，历史靠逐日累积，早期分位无意义；仅 CN",
         formula="CN.MTN_AA_5Y − CN.BOND_5Y（源：get_cn_bond AA 档 + get_macro_daily 派生）"),
    # —— 低评级档 AA−（4.2「高收益债利差」的可得近似；源为 bond_china_close_return 的最低档）——
    dict(code="VAL.BUFFETT", name="巴菲特指标(总市值/GDP)", scope="ALLOC", dimension="估值",
         market_scope="CN", unit="pct", direction="high_risk", freq="day",
         remark="定义：沪深股票总市值 / GDP_ttm × 100（%）——**极长期估值锚**，衡量股市相对实体经济的体量　·　判读：>100% 为警戒区（股市相对经济过热），<50% 为深度低估区；**慢变量，只用于定位长期位置，不做择时信号**　·　数据：⚠ 受沪市源限制自 2022-02 起（深市单独可到 2010），且**历史段为周频降采样**（两所接口无批量历史、逐日回填易被限流挂起），2026-10 起为日频；仅 CN",
         formula="CN.MKT_CAP_TOTAL / GDP_ttm × 100（GDP_ttm = CN.GDP 差分回单季后滚动 4 季合计，PIT）"),
    dict(code="VAL.DIV_SPREAD", name="股息利差(股息率−无风险利率)", scope="ALLOC", dimension="估值",
         market_scope="CN+HK", unit="pp", direction="high_good", freq="day",
         remark="定义：股票股息率 − 同市场无风险利率（pp）——**保守型估值锚**，股息是股东**实得现金流**、与债券票息同质，故两者可比　·　判读：利差走高=股票相对债券的现金回报更有吸引力（高股息/红利策略的配置窗口）；利差为负即「买股不如买债」　·　⚠ 与 VAL.ERP 同属股债性价比但口径不同（ERP 用**盈利**收益率、本项用**已实现股息**），二者背离时说明分红率在变　·　两市场口径不同**不可直接比数值**：**CN** = 沪深A股股息率（乐咕沪/深等权，日频）− 中债10Y，自 **2006-03** 起（受中债10Y 起点限制）；**HK** = 恒指股息率（乐咕，**月频**）− 美债10Y，自 2019 起（受 HK 指标索引起点限制，约 85 个月频点）　·　数据：中证全指口径的 `CN.DIV_SPREAD` 仍在采集层留存（仅 20 日窗口累积），与乐咕口径水平差 0.4~0.7pp **不可拼接**",
         formula="CN：CN.DIV_YIELD_LG − CN.BOND_10Y；HK：HK.DIV_YIELD_HSI − US.DGS10（美债腿 asof；源 get_dividend_yield_lg + get_macro_daily 派生）"),
    dict(code="RISK.CREDIT_SPREAD_AA_MINUS", name="信用利差(中票AA− 5Y−国债5Y)", scope="MACRO", dimension="风险",
         market_scope="CN", unit="pp", direction="high_risk", freq="day",
         remark="定义：AA− 级中短期票据与同期限国债收益率差——**本表可采的最低评级**，最接近「高收益债利差」　·　判读：走阔=违约担忧与流动性收缩（领先股票调整）；比 AA 口径更早反映信用压力，但更嘈杂、样本更短　·　数据：⚠ 源只保留最近 3 个交易日，历史靠逐日累积，**早期分位无意义**；仅 CN",
         formula="CN.MTN_AA_MINUS_5Y − CN.BOND_5Y（源：get_cn_bond AA− 档 + get_macro_daily 派生）"),
    dict(code="RISK.GRADE_SPREAD_AA_MINUS", name="信用等级利差(AA− 5Y−AA 5Y)", scope="MACRO", dimension="风险",
         market_scope="CN", unit="pp", direction="high_risk", freq="day",
         remark="定义：同期限 AA− 与 AA 中票收益率差（低评级段等级利差）　·　判读：**低评级段的纯风险偏好读数**——同期限仅差半档评级，走阔=资金向高等级避险；比绝对利差更少受无风险利率干扰，是信用收缩的最早读数之一　·　数据：⚠ 源只保留最近 3 个交易日，历史逐日累积；仅 CN",
         formula="CN.MTN_AA_MINUS_5Y − CN.MTN_AA_5Y"),
    dict(code="MACRO.STOCK_BOND_CORR", name="股债相关性(60日)", scope="MACRO", dimension="风险",
         market_scope="CN+HK", unit="corr", direction="high_risk", freq="day",
         remark="定义：60 日滚动 corr(本市场股指日收益, −本市场无风险利率日变化)；取 −Δyield 是为对齐「债券**价格**」口径（库里存的是收益率，上行=债价跌）　·　判读：**负值=常态**（避险主导，股跌债涨、股债对冲有效）；**转正/持续上行=宏观主导**（股债同跌，增长或流动性冲击 → 对冲失效，应提高现金与防守仓位）　·　两市场口径不同**不可直接比数值**：**CN** = 上证 × 中债10Y（2006 起）；**HK** = 恒指 × 美债10Y（2019 起，香港无本地长端利率，经 HKD 联系汇率以美债为无风险利率）　·　数据：窗口 60 交易日，序列前 60 日无值",
         formula="CN：rolling_corr(100·Δln(SH.000001), −Δ(CN.BOND_10Y), 60)；HK：同一算法换成 HK.800000 × US.DGS10"),
    dict(code="RISK.GRADE_SPREAD", name="信用等级利差(AA−AAA 5Y)", scope="MACRO", dimension="风险",
         market_scope="CN+HK", unit="pp", direction="high_risk", freq="day",
         remark="定义：同期限 AA 与 AAA 中票收益率差（等级利差）　·　判读：**纯风险偏好读数**——同期限同信用环境仅等级不同，走阔=风险偏好收缩；比绝对信用利差更少受无风险利率干扰　·　数据：源只保留最近 3 个交易日，历史逐日累积；已纳入 FSI；**CN+HK**（与 RISK.CREDIT_SPREAD 同理，HK 侧也接入以保持 §5.4 风险偏好指数两侧口径一致）",
         formula="CN.MTN_AA_5Y − CN.MTN_AAA_5Y"),
    dict(code="MACRO.VEG_BASKET_YOY", name="菜篮子批发价同比(CPI 高频)", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="day",
         remark="定义：农业农村部菜篮子批发价格指数的近 1 年涨跌幅　·　判读：CPI 食品项的**日频先行验证**——菜篮子同比走高往往领先 CPI 食品项数月　·　数据：源侧已算好同比；日频但源窗口有限",
         formula="源 macro_china_vegetable_basket「近1年涨跌幅」"),
    dict(code="MACRO.PORK_PRICE", name="生猪价格(瘦肉型)", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="cny_per_kg", direction="high_risk", freq="day",
         remark="定义：瘦肉型生猪现货价（元/公斤）　·　判读：猪价是 CPI 的最大单一驱动项，猪周期领先 CPI 约 3-6 个月　·　数据：⚠ 源约 7.5 个月滚动窗口 + 未标注单位（量级=元/公斤），历史靠逐日累积，早期分位无意义",
         formula="源 spot_hog_year_trend_soozhu「价格」"),
    dict(code="MACRO.CORE_CPI_YOY", name="核心CPI同比(剔除食品能源)", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：剔除食品与能源的 CPI 同比（核心通胀），需求侧通胀黏性的读数　·　判读：持续上行=内生通胀压力，货币宽松空间受压；与菜篮子/猪价（食品项）对照可看结构性通胀　·　数据：⚠ 源库仅 2021-01 起有该口径，分位样本约 5 年",
         formula="CN.CORE_CPI_YOY（源：国家统计局「国家数据」月度库，官方名「不包括食品和能源"
                 "居民消费价格指数(上年同月=100)」，指数−100 → 同比%）"),
    dict(code="MACRO.CLI_CN", name="OECD 综合领先指标(中国)", scope="MACRO", dimension="景气",
         market_scope="CN", unit="index", direction="high_good", freq="month",
         remark="定义：OECD 中国综合领先指标（振幅调整口径，100=长期趋势）　·　判读：**>100 扩张、<100 收缩**；领先经济拐点约 3-6 个月，用于周期定位而非择时　·　数据：1992-05 起 413 个月无缺口；月频；发布滞后按月初+45 天保守估",
         formula="CN.CLI（OECD Composite leading indicators，振幅调整口径）"),
    dict(code="MACRO.CLI_GLOBAL", name="OECD 综合领先指标(G20)", scope="MACRO", dimension="景气",
         market_scope="GLOBAL", unit="index", direction="high_good", freq="month",
         remark="定义：OECD G20 综合领先指标（100=长期趋势）　·　判读：**>100 全球扩张**；与 CLI_CN 对照可分离国内 vs 外部驱动力——全球向上而中国向下=外需强内需弱　·　数据：口径同 CLI_CN；月频",
         formula="GLOBAL.CLI_G20（OECD SDMX）"),
    # —— CN 宏观月频补接（2026-10-10）：此前只在采集层 regime.macro_series，未进指标层 ——
    #   全部**不入合成**（独立读数）；方向判据写在各自 remark 里。
    dict(code="MACRO.CPI_YOY", name="CPI 同比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：居民消费价格指数同比（%），整体通胀水平　·　判读：上行=通胀压力→紧缩风险；持续为负（通缩）=需求不足；2-3% 附近通常被视为政策舒适区　·　数据：2008-01 起；月频（PIT 填充到交易日）",
         formula="CN.CPI_YOY（akshare macro_china_cpi「全国-同比增长」）"),
    dict(code="MACRO.CPI_MOM", name="CPI 环比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：CPI 月度环比（%），通胀的短期动能　·　判读：比同比更早反映拐点——环比连续走高往往预示同比见底回升；春节错位会造成单月大幅波动，宜看 3 月移动平均　·　数据：2008-01 起；月频",
         formula="CN.CPI_MOM（akshare macro_china_cpi「全国-环比增长」）"),
    dict(code="MACRO.PPI_YOY", name="PPI 同比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_good", freq="month",
         remark="定义：工业生产者出厂价格同比（%）　·　判读：**顺周期盈利口径**——PPI 回升对应名义增长与上游企业盈利改善；PPI 持续为负=工业品通缩、企业盈利承压　·　数据：2006-01 起；月频",
         formula="CN.PPI_YOY（akshare macro_china_ppi「当月同比增长」）"),
    dict(code="MACRO.PPI_CPI_SCISSOR", name="PPI−CPI 剪刀差", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pp", direction="neutral", freq="month",
         remark="定义：PPI 同比 − CPI 同比（pp）　·　判读：刻画利润在上下游的分配——走阔=上游（资源/材料）相对占优，收敛=中下游成本压力缓解；对整体市场无单一方向，故 direction=neutral　·　数据：由 CPI_YOY 与 PPI_YOY 派生",
         formula="CN.PPI_CPI_SCISSOR（derived:get_macro_monthly，PPI_YOY − CPI_YOY）"),
    # —— 央行企业商品价格指数 CGPI（§3.3 通胀维度；先于 CPI/PPI 变动的早期读数）——
    dict(code="MACRO.CGPI_YOY", name="企业商品价格指数-总指数同比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：企业间交易商品价格总指数同比（**不含服务与消费品零售**，央行 CGPI）　·　"
                "判读：上行=企业成本端通胀压力累积，通常**领先 CPI/PPI 数月**；持续为负=工业品通缩、"
                "企业毛利受压　·　数据：2009-01 起 212 个月；源为央行 CGPI 年度 htm 附件"
                "（源表「上年同月=100」已减 100 转同比）",
         formula="CN.CGPI_YOY（pbc:cgpi 总指数列，值−100）"),
    dict(code="MACRO.CGPI_AGRI_YOY", name="企业商品价格指数-农产品同比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：CGPI 农产品分项同比，企业购入的初级农产品价格　·　"
                "判读：**食品类成本的先行读数**，领先 CPI 食品项；与菜篮子批发价（MACRO.VEG_BASKET_YOY）"
                "互为验证　·　数据：2009-01 起；实测近年持续为负（农产品价格低迷）",
         formula="CN.CGPI_AGRI_YOY（pbc:cgpi 农产品列，值−100）"),
    dict(code="MACRO.CGPI_MINING_YOY", name="企业商品价格指数-矿产品同比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：CGPI 矿产品分项同比，上游资源品价格　·　"
                "判读：与 PPI 采掘工业高度相关，是**上游涨价压力的最早读数**；走阔通常先于 PPI 回升　·　"
                "数据：2009-01 起；近年明显高于总指数（上游强于中下游）",
         formula="CN.CGPI_MINING_YOY（pbc:cgpi 矿产品列，值−100）"),
    dict(code="MACRO.CGPI_ENERGY_YOY", name="企业商品价格指数-煤油电同比", scope="MACRO", dimension="通胀",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：CGPI 煤油电分项同比，能源购进价格　·　"
                "判读：**双重性**——对上游能源企业是收入端利好，对中下游制造业是成本冲击；"
                "读它要区分站在产业链哪一环　·　数据：2009-01 起",
         formula="CN.CGPI_ENERGY_YOY（pbc:cgpi 煤油电列，值−100）"),
    dict(code="MACRO.M1_YOY", name="M1 同比", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="month",
         remark="定义：狭义货币 M1 同比（%）　·　判读：**资金活化**读数——M1 高增=企业与居民交易性存款活跃，历史上领先企业盈利与股市　·　数据：2008-01 起；月频；CN/HK 双市场接入",
         formula="CN.M1_YOY（akshare macro_china_money_supply「货币(M1)-同比增长」）"),
    dict(code="MACRO.M2_YOY", name="M2 同比", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pct", direction="high_good", freq="month",
         remark="定义：广义货币 M2 同比（%），流动性总量　·　判读：单独读数信息量低，须与 M1 对照看结构；M2 增速持续高于名义 GDP 增速=流动性宽松环境　·　数据：2008-01 起；月频",
         formula="CN.M2_YOY（akshare macro_china_money_supply）"),
    dict(code="MACRO.M1_M2_SCISSOR", name="M1−M2 剪刀差", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pp", direction="high_good", freq="month",
         remark="定义：M1 同比 − M2 同比（pp）　·　判读：**资金活化程度**——走阔=存款活期化、实体交易与投资意愿回升，A 股顺周期行情的经典领先读数；收窄=资金定期化淤积、经济活力下降　·　数据：2008-01 起；月频",
         formula="CN.M1_M2_SCISSOR（derived:get_macro_monthly，M1_YOY − M2_YOY）"),
    dict(code="MACRO.LPR_1Y", name="LPR 1年期", scope="MACRO", dimension="资金",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：贷款市场报价利率 1 年期（%），实体贷款定价基准　·　判读：**读数下行=宽松**（direction=high_risk，低反而是宽松）；LPR 下调通常利好地产链与整体估值　·　数据：2013-10 起（2019-08 前为贷款基准利率，口径已切换，长周期分位需注意）",
         formula="CN.LPR_1Y（akshare macro_china_lpr，日频重复报价按月取当月最后一次）"),
    dict(code="MACRO.LPR_5Y", name="LPR 5年期", scope="MACRO", dimension="资金",
         market_scope="CN", unit="pct", direction="high_risk", freq="month",
         remark="定义：贷款市场报价利率 5 年期（%），房贷利率锚　·　判读：读数下行=地产政策放松信号，利好地产链与居民中长贷　·　数据：2019-08 起 86 个月，分位样本约 6 年",
         formula="CN.LPR_5Y（akshare macro_china_lpr）"),
    dict(code="MACRO.BOND_10Y", name="10年期国债收益率", scope="MACRO", dimension="资金",
         market_scope="CN", unit="pct", direction="neutral", freq="day",
         remark="定义：中债国债收益率曲线 10 年期（%），中国无风险利率　·　判读：**无单一方向**——既是贴现率（上行压估值）也是增长/通胀预期（上行反映景气），实务中与 ERP 联读：收益率上行而 ERP 同步走高=盈利驱动，反之则纯估值压制　·　数据：2006-03 起，全库最长样本之一；亦是 VAL.ERP 与 RISK.TERM_SPREAD 的输入",
         formula="CN.BOND_10Y（akshare bond_china_yield 中债国债收益率曲线-10年）"),
    dict(code="MACRO.BOND_10Y_CHG20", name="中债10Y 20日变化", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pp", direction="high_risk", freq="day",
         remark="定义：10 年期国债收益率近 20 个交易日的变化（**pp，非百分比**——收益率本身以 % 计价且可能贴近 0，百分比变化会严重失真：1.65%→1.70% 的「3% 涨幅」实为 +5bp）　·　判读：按 §4.2 的方向性读法——**下行 + 信用利差收窄 = 风险偏好改善**，上行则警惕；⚠ 上行也可能是增长预期驱动（对股票未必坏），须与 MACRO.BREAKEVEN_10Y / 信用利差联读以区分「通胀或紧缩驱动」与「增长驱动」；水平值的双重性质见 MACRO.BOND_10Y　·　数据：2006-03 起（受 CN.BOND_10Y 限制）；前 20 个交易日无值；CN+HK（中国利率是港股分母端的重要外生变量，同 RISK.CREDIT_SPREAD 先例）",
         formula="CN.BOND_10Y − CN.BOND_10Y(t−20)，单位 pp（源：get_cn_bond，MOM_MAP diff 口径）"),
    dict(code="MACRO.PMI_MFG", name="制造业 PMI", scope="MACRO", dimension="景气",
         market_scope="CN+HK", unit="index", direction="high_good", freq="month",
         remark="定义：制造业采购经理指数，由新订单/生产/就业/库存等分项扩散指数加权而成　·　判读：**50 为荣枯线：>50 扩张、<50 收缩**；连续 3 个月同向穿越荣枯线才算趋势，单月波动勿过度解读　·　数据：2008-01 起；月频",
         formula="CN.PMI_MFG（akshare macro_china_pmi「制造业-指数」）"),
    dict(code="MACRO.PMI_NONMFG", name="非制造业 PMI", scope="MACRO", dimension="景气",
         market_scope="CN", unit="index", direction="high_good", freq="month",
         remark="定义：非制造业（服务业+建筑业）采购经理指数，编制方法同制造业 PMI　·　判读：**50 为荣枯线**；中国服务业占比已过半，与制造业 PMI 常分化——两者背离时更能定位内需强弱（制造弱服务强=外需/投资弱、消费韧性）　·　数据：2008-01 起；月频",
         formula="CN.PMI_NONMFG（akshare macro_china_pmi「非制造业-指数」）"),
    dict(code="MACRO.PMI_NEW_ORDERS", name="制造业 PMI 新订单", scope="MACRO", dimension="景气",
         market_scope="CN+HK", unit="index", direction="high_good", freq="month",
         remark="定义：制造业 PMI 的新订单分项指数，反映订单、生产、就业、库存等经营活动的前端需求　·　判读：**50 为荣枯线：>50 订单扩张、<50 订单收缩**；新订单领先生产与出厂价约 1-2 个季度，是 PMI 体系中**前瞻性最强**的读数，与总指数互为验证　·　数据：2005-01 起 261 个月，历史长于制造业 PMI 总指数；走 NBS 月度库（akshare 接口无分项）",
         formula="CN.PMI_NEW_ORDERS（nbs:stream/esData PMI 新订单指数）"),
    dict(code="MACRO.INDUSTRIAL_PROFIT_YOY", name="工业企业利润累计同比", scope="MACRO", dimension="景气",
         market_scope="CN", unit="pct", direction="high_good", freq="month",
         remark="定义：规模以上工业企业利润总额的年初至今累计同比（%）　·　判读：A 股盈利的宏观代理——与上市公司利润同向、略领先；持续为负=盈利下行周期　·　数据：⚠ **累计口径非单月**，勿与单月序列直接相减；1 月每年缺（1-2 月合并发布）",
         formula="CN.INDUSTRIAL_PROFIT_YOY（nbs:stream/esData）"),
    dict(code="MACRO.POWER_GEN_YOY", name="发电量同比", scope="MACRO", dimension="景气",
         market_scope="CN", unit="pct", direction="high_good", freq="month",
         remark="定义：规模以上工业发电量同比（%）　·　判读：**克强指数成分之一**，实体经济活动量的硬指标，不受价格因素干扰；与货运量交叉验证可排除单指标噪声　·　数据：⚠ 口径为规上工业发电量，不含规模以下/分布式光伏与自备电厂，与全社会用电量不可互替",
         formula="CN.POWER_GEN_YOY（nbs:stream/esData）"),
    dict(code="MACRO.FREIGHT_YOY", name="货运量同比", scope="MACRO", dimension="景气",
         market_scope="CN", unit="pct", direction="high_good", freq="month",
         remark="定义：全社会货运量合计同比（%）　·　判读：**克强指数成分之一**，实物量指标，与发电量互为印证——两者同向走高=实体需求确认　·　数据：2000-01 起，全库最长宏观序列之一",
         formula="CN.FREIGHT_YOY（akshare macro_china_society_traffic_volume，row_filter 合计）"),
    dict(code="MACRO.OIL_MOM20", name="原油 20日动量", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="pct", direction="neutral", freq="day",
         remark="定义：INE 原油连续近 20 个交易日动量（%）　·　判读：**双重身份**——既是需求代理（正）也是成本冲击（对中下游为负），故无单一方向；与铜动量对照可分离需求与供给冲击（油强铜弱=供给驱动）　·　数据：2018-03 起",
         formula="CMDTY.OIL 的 s/s.shift(20)−1（在源市场交易日上算，再对齐本市场）"),
    # —— §3.4 外部与汇率 + §5.4 跨资产成分（**复用 get_global_benchmarks 已采序列，零新增采集**）——
    dict(code="MACRO.DXY", name="美元指数(ICE DXY)", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="index", direction="high_risk", freq="day",
         remark="定义：ICE 美元指数（对一篮子主要货币）　·　判读：**全球流动性总闸门**——强美元压制新兴市场（A股/港股）与大宗商品；风险偏好指数中取反　·　数据：2016-09 起（分位样本约 10 年）；与 DTWEXBGS 互为口径校验",
         formula="daily_benchmark US.DXY 收盘（get_global_benchmarks 每小时采）"),
    dict(code="MACRO.DXY_CHG20", name="美元指数 20日变化", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="pct", direction="high_risk", freq="day",
         remark="定义：ICE 美元指数近 20 个交易日的变化（%）　·　判读：对应 §4.2「**强美元 → 新兴市场承压**」——上行=A 股/港股的资金面逆风与大宗商品压制（风险偏好指数对美元是取反的）　·　数据：2016-09 起（受 US.DXY 限制）；长历史美元周期看 MACRO.DTWEXBGS（2006 起），两者水平口径不同不可直接比",
         formula="(US.DXY / US.DXY(t−20) − 1) × 100（bench，MOM_MAP pct 口径）"),
    dict(code="MACRO.DTWEXBGS", name="美元指数(Fed 贸易加权)", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="index", direction="high_risk", freq="day",
         remark="定义：美联储广义贸易加权美元指数　·　判读：同 DXY 逻辑但篮子更宽、更贴近美国贸易结构；**样本长于 ICE DXY（2006 起）**，做长周期分位优先用本序列　·　数据：两口径同向但幅值不同，勿混用",
         formula="daily_benchmark US.DTWEXBGS（FRED）"),
    dict(code="MACRO.USDCNY", name="美元兑人民币(在岸)", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="cny_per_usd", direction="high_risk", freq="day",
         remark="定义：美元兑人民币**在岸**汇率（中国银行折算价）　·　判读：**上行=人民币贬值** → 外资流出压力与风险偏好下降；与中美利差同看（利差走阔叠加汇率贬值=资本外流组合）　·　数据：1994 起长样本；⚠ 离岸 CNH 无稳定免费源未采，勿把本序列当 CNH 用",
         formula="daily_benchmark FX.USDCNY（akshare currency_boc_safe「美元」列÷100，"
                 "get_global_benchmarks 采；口径订正见该文件 §3.4 注释）"),
    dict(code="MACRO.USDCNY_CHG20", name="人民币 20日变化(在岸)", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pct", direction="high_risk", freq="day",
         remark="定义：USDCNY（在岸）近 20 个交易日的变化（%）　·　判读：**上行 = 人民币贬值 → 资本外流压力、北向承压**，故高分=风险；⚠ 方向与「人民币强弱」相反（这里是「美元兑人民币」的涨跌）　·　与 MACRO.CN_US_SPREAD 联读：**利差走阔 + 汇率贬值 = 典型资本外流组合**（§4.2 汇率维度的核心确认）　·　数据：1994 起；CN+HK",
         formula="(FX.USDCNY / FX.USDCNY(t−20) − 1) × 100（bench，MOM_MAP pct 口径）"),
    # —— 避险货币（4.2 汇率：避险货币走强 → 新兴市场承压）——
    dict(code="MACRO.JPYCNY", name="日元兑人民币(百日元)", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="cny_per_100jpy", direction="high_risk", freq="day",
         remark="定义：100 日元兑人民币（中国银行折算价，中行挂牌惯例口径）　·　判读：**上行=日元升值（避险资金流入）→ risk-off**，与 VIX、黄金同向；⚠ 是**兑人民币**的交叉盘，同时含人民币自身强弱 → 须与 MACRO.USDCNY 联读（日元升值 + 人民币贬值 = 避险主导）　·　数据：1994 起长样本",
         formula="daily_benchmark FX.JPYCNY（akshare currency_boc_safe「日元」列÷100×100）"),
    dict(code="MACRO.CHFCNY", name="瑞郎兑人民币", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="cny_per_chf", direction="high_risk", freq="day",
         remark="定义：1 瑞郎兑人民币（中国银行折算价）　·　判读：**上行=瑞郎升值（避险/欧元区风险外溢）→ risk-off**；瑞郎对欧债危机等欧洲风险事件最敏感，是欧元区压力的早期读数　·　数据：⚠ 2015-11 起（中行表 2015 年才纳入瑞郎），约 11 年样本",
         formula="daily_benchmark FX.CHFCNY（akshare currency_boc_safe「瑞士法郎」列÷100）"),
    dict(code="MACRO.FED_FUNDS", name="有效联邦基金利率(EFFR)", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="pct", direction="high_risk", freq="day",
         remark="定义：有效联邦基金利率（EFFR），美联储政策利率的实际成交口径　·　判读：高位=全球紧缩环境，压制风险资产估值与新兴市场流动性；政策转向（首次降息）往往是全球流动性拐点信号　·　数据：2001-12 起；日频但变动不频繁（政策会议驱动）",
         formula="daily_benchmark US.EFFR（FRED）"),
    dict(code="MACRO.BREAKEVEN_10Y", name="通胀预期(10Y盈亏平衡)", scope="MACRO", dimension="通胀",
         market_scope="GLOBAL", unit="pct", direction="high_risk", freq="day",
         remark="定义：10 年期盈亏平衡通胀率 = 10Y 名义国债 − 10Y TIPS，即市场隐含通胀预期　·　判读：上行=市场通胀预期升温（紧缩预期）；与已实现核心 CPI 对照可分离预期 vs 现实——预期显著高于现实往往预示后续 CPI 上行　·　数据：2003-01 起长样本；⚠ 为美债口径，作全球锚而非中国通胀预期（中债隐含通胀预期无免费源）",
         formula="daily_benchmark US.T10YIE（FRED，get_global_benchmarks 每小时采）"),
    dict(code="RISK.VIX", name="VIX 恐慌指数", scope="MACRO", dimension="风险",
         market_scope="GLOBAL", unit="index", direction="high_risk", freq="day",
         remark="定义：标普 500 期权隐含波动率（恐慌指数）　·　判读：>20 进入高波动/恐慌区，>30 为极端恐慌（常对应阶段性底部）；风险偏好指数中取反　·　数据：1990 起长样本",
         formula="daily_benchmark US.VIXCLS（FRED）"),
    dict(code="RISK.VIX_CHG20", name="VIX 20日变化", scope="MACRO", dimension="风险",
         market_scope="GLOBAL", unit="index", direction="high_risk", freq="day",
         remark="定义：标普 500 期权隐含波动率近 20 个交易日的变化（**按「点」读**——波动率惯例以点数而非百分比计，如「VIX 上升 5 点」）　·　判读：**波动率抬头先于风险事件**，且能捕捉水平分位滞后的问题——典型如 VIX 水平分位仍中性（45）而 20 日已 +25%，正是 2020-03 / 2024-08 的形态　·　与水平指标的分工：水平用于「是否已在高波动区」（>20 / >30 阈值），变化用于「方向」（§4.2.1 的投票）　·　⚠ 同步性提示：VIX 由标普期权定价，与股票**基本同步**，故它更适合作为「状态描述」而非「独立的领先确认」（见 §4.2.1 第九节）　·　数据：1990 起；前 20 个交易日无值",
         formula="US.VIXCLS − US.VIXCLS(t−20)，单位波动率点数"),
    dict(code="MACRO.SPX_MOM20", name="标普500 20日动量", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="pct", direction="high_good", freq="day",
         remark="定义：标普 500 近 20 个**美股**交易日的动量（%）　·　判读：全球权益风险偏好的代理——正动量=risk-on；风险偏好指数成分（sign +1）　·　数据：动量在源市场交易日上算后再对齐本市场，避免假期错位",
         formula="US.SP500 收盘 20 日变动 %"),
    dict(code="MACRO.COPPER_MOM20", name="铜价 20日动量", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="pct", direction="high_good", freq="day",
         remark="定义：沪铜连续近 20 个交易日动量（%）　·　判读：铜=全球需求侧最纯的商品，正动量=再通胀/需求改善预期；与黄金动量背离（铜强金弱）是典型 risk-on 组合　·　数据：2018-03 起",
         formula="CMDTY.COPPER 收盘 20 日变动 %"),
    dict(code="MACRO.GOLD_MOM20", name="黄金 20日动量", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="pct", direction="high_risk", freq="day",
         remark="定义：沪金连续近 20 个交易日动量（%）　·　判读：黄金=避险资产，**金强=risk-off**，风险偏好指数中取反　·　数据：2018-03 起",
         formula="CMDTY.GOLD 收盘 20 日变动 %"),
    # —— 商品**价格水平**（4.2 跨资产：此前只登记了 20 日动量，水平值虽在库却取不到分位）——
    # 方向与其动量口径对齐（铜 high_good / 金 high_risk / 油 neutral），便于水平与方向联读。
    dict(code="MACRO.COPPER", name="沪铜连续价格", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="cny_per_ton", direction="high_good", freq="day",
         remark="定义：沪铜连续期货收盘价（元/吨），全球需求侧最纯的商品　·　判读：**高分位=再通胀交易已打得较满**——价格水平反映「需求预期兑现程度」，方向性看 MACRO.COPPER_MOM20；与 MACRO.GOLD 的比值可看「铜强金弱」的 risk-on 结构　·　数据：2005-01 起长样本",
         formula="CMDTY.COPPER 收盘价（akshare futures_zh_daily_sina:CU0）"),
    dict(code="MACRO.GOLD", name="沪金连续价格", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="cny_per_gram", direction="high_risk", freq="day",
         remark="定义：沪金连续期货收盘价（元/克）　·　判读：**黄金与实际利率互为反面**——价格高分位常对应避险需求高企/实际利率下行，对股票是 risk-off 信号（方向取反）；与美债 10Y、美元指数联读可分离「避险」与「美元弱」两种驱动　·　数据：2008-01 起",
         formula="CMDTY.GOLD 收盘价（akshare futures_zh_daily_sina:AU0）"),
    dict(code="MACRO.OIL", name="原油连续价格", scope="MACRO", dimension="跨资产",
         market_scope="GLOBAL", unit="cny_per_barrel", direction="neutral", freq="day",
         remark="定义：上期能源原油连续期货收盘价（元/桶）　·　判读：**无单一方向**——既是通胀源头（高位→紧缩压力）也是需求读数（上行→景气改善）；须与 MACRO.OIL_MOM20、CPI/PPI 联读　·　数据：⚠ 2018-03 起，样本约 8 年，长周期分位参考价值有限",
         formula="CMDTY.OIL 收盘价（akshare futures_zh_daily_sina:SC0）"),
    dict(code="MACRO.OUTPUT_GAP", name="产出缺口(实际−潜在 GDP)", scope="MACRO", dimension="景气",
         market_scope="CN", unit="pct", direction="high_good", freq="quarter",
         remark="定义：(实际 GDP − 潜在 GDP)/潜在 GDP，经济冷热的水平读数　·　判读：>0 实际产出高于潜在（过热、通胀压力），<0 产出不足（衰退、失业压力）；是美林时钟的增长轴　·　数据：⚠ 2014Q1 起有值（源链自 2011Q1 + 前 12 季滤波暖机），约 50 个季度；季频",
         formula="(lv − HP单边(lv, λ=1600))/HP × 100；lv = 100·Π(1+CN.GDP_QOQ/100)"),
    dict(code="MACRO.CLOCK_QUADRANT", name="美林投资时钟象限", scope="MACRO", dimension="景气",
         market_scope="CN", unit="code", direction="neutral", freq="quarter",
         remark="定义：产出缺口方向 × CPI 同比 6 个月方向组合出的美林时钟象限（分类标签，非连续值）　·　判读：**1=复苏（增长↑通胀↓）→股 / 2=过热（↑↑）→商品 / 3=滞胀（↓/通胀↑）→现金 / 4=衰退（↓↓）→债**；增长轴取产出缺口环比方向，通胀轴取 CPI 同比 6 个月方向（去单月噪声）　·　数据：⚠ **分类值不做历史分位**（percentile 恒为空），勿当连续指标用；季频",
         formula="quadrant(sign(ΔMACRO.OUTPUT_GAP), sign(CPI_YOY(t) − CPI_YOY(t−6)))"),
    dict(code="MACRO.US_10Y", name="美债10年期收益率", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="pct", direction="neutral", freq="day",
         remark="定义：美国 10 年期国债收益率（%），全球资产定价的无风险利率锚　·　判读：**无单一方向**（同 MACRO.BOND_10Y 的双重性质）——既是全球贴现率（上行压估值）也是增长/通胀预期（上行反映景气）；须与美元指数、VIX 联读以辨识驱动来源　·　数据：1990-12 起长样本；⚠ 美债休市日（如哥伦布日）源无报价，属正常非缺采；亦是 HK VAL.ERP 的无风险利率输入",
         formula="daily_benchmark US.DGS10（akshare 美国国债收益率10年）"),
    dict(code="MACRO.US_2Y", name="美债2年期收益率", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="pct", direction="neutral", freq="day",
         remark="定义：美国 2 年期国债收益率（%），对美联储政策路径最敏感的期限　·　判读：**无单一方向**——更贴政策预期（上行=市场计入更紧的紧缩路径），但也会因增长预期改善而上行；带方向含义的曲线信号看 MACRO.US_TERM_SPREAD　·　数据：1990-12 起长样本，与美债10Y 同日成交",
         formula="daily_benchmark US.DGS2（akshare 美国国债收益率2年）"),
    dict(code="MACRO.US_10Y_CHG20", name="美债10Y 20日变化", scope="MACRO", dimension="资金",
         market_scope="GLOBAL", unit="pp", direction="high_risk", freq="day",
         remark="定义：美债 10 年期收益率近 20 个交易日的变化（**pp，非百分比**，理由同 MACRO.BOND_10Y_CHG20）　·　判读：**全球折现率的方向**——上行=估值端逆风（尤其是港股等外资定价市场），下行=分母端顺风；⚠ 与 MACRO.BOND_10Y_CHG20 的**方向差**是重要读数（美债上而中债下=中美利差走阔，对应资本外流与人民币压力）　·　数据：1990-12 起（受 US.DGS10 限制）；美债休市日无值，属正常",
         formula="US.DGS10 − US.DGS10(t−20)，单位 pp（bench，MOM_MAP diff 口径）"),
    dict(code="MACRO.CN_US_SPREAD", name="中美利差(中债10Y−美债10Y)", scope="MACRO", dimension="资金",
         market_scope="CN+HK", unit="pp", direction="neutral", freq="day",
         remark="定义：中债 10Y − 美债 10Y（pp），中美利差　·　判读：倒挂=利差为负，人民币承压、外资流出压力；走阔通常利好人民币与外资流入——但若因美债风险溢价飙升而走阔则相反，须结合美元指数与汇率解读，故无单一方向　·　数据：日频，双源同日对齐",
         formula="CN.BOND_10Y − US.DGS10（同日，源：get_macro_daily 跨表派生）"),
    dict(code="MACRO.US_TERM_SPREAD", name="美债期限利差(10Y−2Y)", scope="MACRO", dimension="风险",
         market_scope="GLOBAL", unit="pp", direction="high_good", freq="day",
         remark="定义：美债 10Y − 2Y（pp），美债曲线陡峭度　·　判读：**倒挂为经典衰退预警**（全球流动性与风险偏好的领先信号）；走阔=复苏预期；CN/HK 双侧均接入　·　数据：1990-12 起长样本（与美债 10Y / 2Y 同覆盖）",
         formula="US.DGS10 − US.DGS2（同日，源：get_macro_daily 跨表派生）"),
    dict(code="FLOW.HIBOR_3M", name="HIBOR 3月(港币流动性)", scope="MACRO", dimension="资金",
         remark="定义：香港银行同业 3 个月港元拆借利率　·　判读：**上行=港元资金面收紧** → 港股估值与流动性承压（温度计中取反）；HIBOR 急升常伴套息交易平仓与港元弱方兑换保证触发　·　数据：仅 HK；日频",
         market_scope="HK", unit="pct", direction="high_risk", freq="day",
         formula="HK.HIBOR_3M；港元资金面收紧→港股估值与流动性承压"),
    # —— 分析师一致预期（依赖 regime.analyst_forecast_snapshot 逐日快照）——
    dict(code="FORECAST.REVISION_BREADTH", name="一致预期修正宽度(30快照滚动)", scope="MARKET", dimension="景气",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         remark="定义：同一财年 FY1 EPS 被上修与下修的家数净占比（30 个快照滚动）　·　判读：>0 盈利预期上修占优（利好），<0 下修占优；领先股价约 1-2 个季度，是温度计成分　·　数据：需 ≥2 个快照日才有值；同日只算 FY1 且财年标签须一致，±0.5% 以内视为噪声不计",
         formula="(Σ30 快照上调家数 − 下调家数) / Σ30 有可比样本家数 × 100；上调=同一财年 FY1 EPS 上调 >0.5%"),
    dict(code="FORECAST.RATING_BULL_PCT", name="评级乐观度(买入+增持占比)", scope="MARKET", dimension="情绪",
         market_scope="CN", unit="pct", direction="neutral", freq="day",
         remark="定义：买入+增持评级占五档评级合计的比例　·　判读：⚠ **判别力弱**——卖方评级结构性集中在买入/增持（实测 2026-09-30 = 99.3%），横截面几乎不变；仅作极端背离（如骤降）时的提示，勿用于常规分位择时　·　数据：仅 CN",
         formula="(买入+增持家数) / 五档评级合计 × 100"),
    # —— 情绪事件（依赖 regime.ipo_event / fund_issuance_event / unlock_schedule / insider_trade）——
    dict(code="SENT.IPO_COUNT_30D", name="近30日 IPO 上市家数", scope="MARKET", dimension="情绪",
         market_scope="CN", unit="count", direction="high_risk", freq="day",
         remark="定义：近 30 日 IPO 上市家数　·　判读：密集发行=新股供给压力，同时是牛市温度信号（温度计成分 sign +1）　·　数据：窗口内无 IPO 记 0；源覆盖前为空；仅 CN",
         formula="count(list_date ∈ (t−30d, t])"),
    dict(code="SENT.IPO_BREAK_RATE_90D", name="近90日 IPO 首日破发率", scope="MARKET", dimension="情绪",
         market_scope="CN", unit="pct", direction="high_risk", freq="day",
         remark="定义：近 90 日新股上市首日破发（收盘价<发行价）的比例　·　判读：**破发=情绪低迷的直接读数**——破发率高时打新无利可图，往往对应市场底部区域　·　数据：取 90 日而非 30 日窗口（30 日样本量常年为 0，无判别力）；仅 CN",
         formula="count(首日收盘价<发行价) / count(上市) × 100，(t−90d, t]"),
    dict(code="SENT.FUND_ISSUANCE_60D", name="近60日权益类基金成立份额", scope="MARKET", dimension="情绪",
         market_scope="CN", unit="yi_fen", direction="neutral", freq="day",
         remark="定义：近 60 日权益类基金成立份额合计（亿份）　·　判读：基金发行放量=增量资金入场 + 散户热度上行（温度计成分）；发行冰点往往对应市场底部　·　数据：权益类=基金类型不含债/固收/稳健/货币；仅 CN",
         formula="Σ issue_share(亿份)，setup_date ∈ (t−60d, t]"),
    dict(code="SENT.FUND_MAX_60D", name="近60日最大单只基金成立份额(爆款)", scope="MARKET", dimension="情绪",
         market_scope="CN", unit="yi_fen", direction="neutral", freq="day",
         remark="定义：近 60 日单只权益基金的最大成立份额（爆款基金）　·　判读：爆款=散户入场的强信号，历史上常出现在阶段性顶部附近——需结合估值水位看（高估值+爆款=警惕）　·　数据：单只极值噪声大，宜与 FUND_ISSUANCE_60D 同看；仅 CN",
         formula="max(issue_share)，权益类，(t−60d, t]"),
    dict(code="SENT.UNLOCK_NEXT_30D_SHARES", name="未来30日解禁股数", scope="MARKET", dimension="资金",
         market_scope="CN", unit="yi_gu", direction="high_risk", freq="day",
         remark="定义：未来 30 日已公告的解禁股数合计（亿股）　·　判读：解禁=潜在供给压力，规模异常放大时压制股价（风险指数成分）；解禁前 30 日内已知，属 PIT 安全的前瞻压力读数　·　数据：⚠ 用**股数**口径保证 PIT 安全（市值口径需当日价格估计，待升级）；仅 CN",
         formula="Σ unlock_shares(亿股)，unlock_date ∈ (t, t+30d]"),
    dict(code="SENT.UNLOCK_PAST_30D_VALUE", name="近30日已解禁市值", scope="MARKET", dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="neutral", freq="day",
         remark="定义：近 30 日已实际解禁的市值（亿元）　·　判读：已实现的供给冲击——与未来 30 日压力对照可看供给节奏的边际变化　·　数据：实际解禁市值仅解禁后可算，故只看过去；仅 CN",
         formula="Σ unlock_value(亿元)，unlock_date ∈ (t−30d, t]"),
    dict(code="SENT.INSIDER_NET_SELL_60D", name="近60日董监高净减持金额", scope="MARKET", dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="high_risk", freq="day",
         remark="定义：近 60 日董监高净减持金额（卖出−买入，亿元）　·　判读：内部人净减持=最了解公司的人在卖出，风险信号（风险指数成分）；净增持为正面信号　·　数据：仅事件级临时公告（含成交均价）；源仅滚动近 1 年，历史靠逐日累积；北交所已排除（源侧数量量纲失真）；仅 CN",
         formula="(Σ SELL − Σ BUY) event_value(亿元)，ann_date ∈ (t−60d, t]"),
    # —— 行业层（第④层，独立于市场合成；不做温度计/风险成员）——
    dict(code="SECTOR.UP_RATIO", name="行业上涨占比(申万一级)", scope="SECTOR", dimension="宽度",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         remark="定义：31 个申万一级行业中当日收涨的占比，行业层宽度　·　判读：>50 多数行业上涨=广度健康；与市场宽度（BREADTH）同向确认，背离时提示指数由少数权重行业拉动　·　数据：分母为当日有行情的行业数；仅 CN",
         formula="count(行业当日涨幅>0) / count(有行情行业) × 100"),
    dict(code="SECTOR.MOM_20D_MEDIAN", name="行业20日动量中位数", scope="SECTOR", dimension="动量",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         remark="定义：31 个申万一级行业 20 日收益的横截面中位数　·　判读：趋势跟随读数——>0 多数行业中期趋势向上；与估值分位配合防过热（高动量+高估值分位=警惕）　·　数据：用中位数抗单行业极端值；仅 CN",
         formula="median(行业指数 20 日收益 %)，31 个申万一级行业"),
    dict(code="SECTOR.RS_DISPERSION", name="行业相对强度离散度(轮动强度)", scope="SECTOR", dimension="风格",
         market_scope="CN", unit="pct", direction="neutral", freq="day",
         remark="定义：行业 20 日收益相对等权均值的横截面标准差，即轮动强度　·　判读：高=结构分化/主线切换剧烈（利好行业轮动策略），低=同涨同跌（β 主导）；**无方向优劣**，须配合行业 RS 方向解读　·　数据：仅 CN",
         formula="std(行业 20 日收益 − 行业等权均值)，横截面"),
    dict(code="SECTOR.PE_TTM_MEDIAN", name="行业 PE-TTM 中位数(申万一级)", scope="SECTOR", dimension="估值",
         market_scope="CN", unit="x", direction="high_risk", freq="day",
         remark="定义：31 个申万一级行业 TTM 市盈率的中位数，行业层估值水位　·　判读：分位 >80=行业整体偏贵；与个股估值（VAL）分层看可定位是龙头贵还是全行业贵　·　数据：⚠ 源**只有当前横截面**（无历史），分位靠逐日累积，早期分位无意义；仅 CN",
         formula="median(行业 PE-TTM)，31 个申万一级行业"),
    dict(code="SECTOR.PB_MEDIAN", name="行业 PB 中位数(申万一级)", scope="SECTOR", dimension="估值",
         market_scope="CN", unit="x", direction="high_risk", freq="day",
         remark="定义：31 个申万一级行业 PB 的中位数　·　判读：同 PE 口径；重资产行业占比高时 PB 比 PE 更可靠　·　数据：源只有当前横截面，分位逐日累积；逐行业明细见 regime.sector_valuation_snapshot；仅 CN",
         formula="median(行业 PB)，31 个申万一级行业"),
    dict(code="SECTOR.FLOW_NET", name="行业资金净流入合计(同花顺)", scope="SECTOR", dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="high_good", freq="day",
         remark="定义：同花顺约 90 个细分行业的资金净流入合计（亿元）　·　判读：>0 资金净流入行业整体占优；与行业涨跌同向确认，背离时提示资金在轮动而非离场　·　数据：⚠ 分类为同花顺口径，**≠ 申万一级**，跨表 join 需映射；源只给当前快照；仅 CN",
         formula="Σ net_inflow(亿元)，当日横截面"),
    dict(code="SECTOR.FLOW_UP_RATIO", name="净流入行业占比(同花顺)", scope="SECTOR", dimension="资金",
         remark="定义：同花顺细分行业中资金净流入为正的占比，资金面广度　·　判读：>50 资金面广度健康；与 FLOW_NET 同看（净额大但占比低=少数行业吸金）　·　数据：分母为有效行业数；源只给当前快照；仅 CN",
         market_scope="CN", unit="pct", direction="high_good", freq="day",
         formula="count(净额>0) / count(有效行业) × 100"),
    dict(code="SECTOR.FLOW_NET_5D", name="近5日行业资金净流入", scope="SECTOR", dimension="资金",
         market_scope="CN", unit="yi_yuan", direction="high_good", freq="day",
         remark="定义：近 5 个交易日行业资金净流入合计　·　判读：平滑单日噪声，>0 中期资金面偏暖　·　数据：需 ≥5 个交易日数据（min_periods=5），不足则空——避免把部分窗口当完整窗口；仅 CN",
         formula="Σ 近5个交易日 SECTOR.FLOW_NET"),
]

# 不做历史分位的指标：
#   · 分类/编码类（分位对类别标签无意义）
#   · VAL.ERP_Z 是 ERP 的**二次标准化**（z-score）→ 再取其分位无信息量
NO_PERCENTILE = {"MACRO.CLOCK_QUADRANT", "VAL.ERP_Z"}

ERP_Z_MIN_PERIODS = 250
"""ERP 标准化偏离（VAL.ERP_Z）的扩展窗口最小样本数。

取 250 ≈ 1 年交易日：z-score 的均值/标准差需要足够样本才稳定。VAL.ERP 自 2018-01 起（日频），
故 ERP_Z 自 ~2019-01 起有值（约 1,880 个交易日样本）。
（原方案曾取 60 以便让「2018 前的月频段」也出值；改为两口径独立成序列后该动机消失，
  60 对日频序列的统计意义不足，故提到 250。）
"""

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
        ("MACRO.US_TERM_SPREAD", -1),         # 美债曲线倒挂 → 全球压力↑（§3.4 外部维度）
        ("RISK.GRADE_SPREAD", 1),             # 等级利差走阔 → 信用风险偏好收缩（§3.2 信用维度）
        ("FLOW.HIBOR_3M", 1),                 # 港币资金面（HK 专属）
    ]),
}

DICT_COLS = ["indicator_code", "indicator_name", "scope", "dimension", "market_scope",
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


# 逐股滚动的分批大小（股票数/批）。每批约 500 只 × 445 天 ≈ 15 万行（峰值 ~40MB），
# 取代原实现的一次性全市场加载（~200 万行 / 峰值 888MB，见下）。
_WINDOW_BATCH_CODES = 500


def _per_stock_windows(conn, market: str, w_start: datetime.date, end: datetime.date) -> pd.DataFrame:
    """逐股滚动窗口：60 日新高/新低、MA60/MA200 上下占比。

    为什么不全在 SQL 里做（性能实测）：PostgreSQL 的 max()/min() 移动窗口**没有逆函数**，
    1.4M 行 × 60 帧 ≈ 33s；改成「SQL 只取原始列 + pandas rolling（C 实现）」后 <3s。

    ⚠ 内存（2026-10-05 修，勿改回一次性加载）：原实现一次 read_sql_query 就把
    全市场 ~200 万行（A 股 5300 只 × 445 天窗口）拉进 pandas，实测峰值 888MB
    （HK 354MB）——在 1.9GB 内存的小机上直接打穿可用余量，与画像计算 09-29/30
    的整机内存-IO 雪崩同款（会连带遗留 futu 锁死、全量采集停摆）。
    改为**按股票分批**：每批 500 只的完整窗口单独 rolling、按 trade_date 聚合累加。
    逐股窗口互不跨股（rolling 的 groupby stem 是 stock_code）→ 数学等价；
    峰值只与单批相关（~40MB），与窗口总行数脱钩。

    返回：按 trade_date 聚合的家数（ok60/ok200 为有效样本数，nh60/nl60/ab60/ab200 为命中数）。
    """
    if market == "CN":
        tbl = "a_daily_quote"
        flt = "close IS NOT NULL AND close <> 'NaN'::numeric AND left(stock_code,5) <> ALL(%s)"
        fparams = [list(CN_EXCLUDE_PREFIX)]
    else:
        tbl = "hk_daily_quote"
        flt = "close IS NOT NULL AND close <> 'NaN'::numeric AND amount >= %s"
        fparams = [HK_MIN_AMOUNT]

    # 窗口内有数据的股票清单（DISTINCT / ORDER 交给 PG，客户端只收几千行）
    codes = _q(conn,
               f"SELECT DISTINCT stock_code FROM {tbl} "
               f"WHERE trade_date BETWEEN %s AND %s AND {flt} ORDER BY stock_code",
               [w_start, end] + fparams)["stock_code"].tolist()
    if not codes:
        return pd.DataFrame()

    agg = None
    for i in range(0, len(codes), _WINDOW_BATCH_CODES):
        batch = codes[i:i + _WINDOW_BATCH_CODES]
        df = _q(conn,
                f"SELECT trade_date, stock_code, close FROM {tbl} "
                f"WHERE stock_code = ANY(%s) AND trade_date BETWEEN %s AND %s AND {flt} "
                f"ORDER BY stock_code, trade_date",
                [batch, w_start, end] + fparams)
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.sort_values(["stock_code", "trade_date"], kind="mergesort").reset_index(drop=True)

        # 批内逐股滚动（算法与参数和旧版完全一致，仅数据范围缩小到本批）
        roll = df.groupby("stock_code", sort=False)["close"]
        n60 = roll.rolling(60, min_periods=1).count().reset_index(level=0, drop=True)
        n200 = roll.rolling(200, min_periods=1).count().reset_index(level=0, drop=True)
        h60 = roll.rolling(60, min_periods=1).max().reset_index(level=0, drop=True)
        l60 = roll.rolling(60, min_periods=1).min().reset_index(level=0, drop=True)
        ma60 = roll.rolling(60, min_periods=1).mean().reset_index(level=0, drop=True)
        ma200 = roll.rolling(200, min_periods=1).mean().reset_index(level=0, drop=True)

        ok60, ok200 = n60 >= 60, n200 >= 200
        part = pd.DataFrame({
            "trade_date": df["trade_date"],
            "ok60": ok60.astype(int), "ok200": ok200.astype(int),
            "nh60": (ok60 & (df["close"] >= h60)).astype(int),
            "nl60": (ok60 & (df["close"] <= l60)).astype(int),
            "ab60": (ok60 & (df["close"] > ma60)).astype(int),
            "ab200": (ok200 & (df["close"] > ma200)).astype(int),
        }).groupby("trade_date")[["ok60", "ok200", "nh60", "nl60", "ab60", "ab200"]].sum()
        # 逐批累加（同一天跨批相加；本批缺失的日期按 0 补 —— 该批当天无有效样本，正确）
        agg = part if agg is None else agg.add(part, fill_value=0)
        del df
    return agg.astype(int) if agg is not None else pd.DataFrame()


# ── 指标数据来源映射（**代码即真相源**）────────────────────────────────────────
# 指标层有两条入湖通路，映射一律在此声明（提为模块常量，供自检与 indicator_dict.source
# 派生使用）：
#   ① PIT_MAP_CN / PIT_MAP_HK：regime.macro_series → 指标（月/季频按 release_time PIT 填充）
#   ② BENCH_MAP / MOM_MAP    ：public.daily_benchmark → 指标（市场报价，同日对齐，无发布滞后）
#
# ⚠ 改 value（源编码）**必须**确认该编码已注册：macro 侧查 macro_series_meta、bench 侧查
#   daily_benchmark.bench_code —— 写错的后果是**静默产出空指标**（不抛错、只是没数据）。
#   该风险由 tests/test_indicator_sources.py 兜住（注册校验 + 端到端覆盖）。
# ⚠ 映射留在代码而非入库：它是「信号怎么算」的计算逻辑，与 formula 同源；入库的是它的
#   **投影**（indicator_dict.source，每轮由 _ensure_dict 全量重写，不会漂移）。

PIT_MAP_CN = {
    "RISK.CREDIT_SPREAD": "CN.CREDIT_SPREAD_MTN_AAA_10Y",
    "RISK.TERM_SPREAD": "CN.TERM_SPREAD_10Y_1Y",
    "MACRO.CN_US_SPREAD": "CN.US_SPREAD_10Y",      # 中美利差（外资机会成本）
    "MACRO.US_TERM_SPREAD": "US.TERM_SPREAD_10Y_2Y",   # 美债期限结构（全球风险偏好）
    "RISK.CREDIT_SPREAD_AA": "CN.CREDIT_SPREAD_MTN_AA_5Y",   # AA 绝对信用利差
    "RISK.GRADE_SPREAD": "CN.GRADE_SPREAD_AA_AAA_5Y",        # 等级利差（风险偏好）
    "RISK.CREDIT_SPREAD_AA_MINUS": "CN.CREDIT_SPREAD_MTN_AA_MINUS_5Y",  # AA− 低评级绝对利差
    "RISK.GRADE_SPREAD_AA_MINUS": "CN.GRADE_SPREAD_AA_MINUS_AA_5Y",     # AA− 低评级等级利差
    # 股息利差（§4.1 股债性价比）：源为**乐咕口径**（长历史，2006-03 起）—— 原中证全指口径
    #   仅 20 日窗口、逐日累积，历史太短无法支撑分位；两口径序列都在采集层保留
    #   （CN.DIV_SPREAD / CN.DIV_SPREAD_LG），指标层只用后者，应用层无需感知。
    "VAL.DIV_SPREAD": "CN.DIV_SPREAD_LG",
    "MACRO.VEG_BASKET_YOY": "CN.VEG_BASKET_YOY",   # CPI 食品项高频
    "MACRO.PORK_PRICE": "CN.PORK_PRICE",           # 猪价（通胀高频）
    "MACRO.CORE_CPI_YOY": "CN.CORE_CPI_YOY",       # 核心CPI（剔除食品能源；源仅 2021 起）
    "MACRO.TSF_STOCK_YOY": "CN.TSF_STOCK_YOY",     # 社融存量同比（央行直连）
    "MACRO.CLI_CN": "CN.CLI",                      # OECD 领先指标（中国）
    "MACRO.CLI_GLOBAL": "GLOBAL.CLI_G20",          # OECD 领先指标（G20，全球周期）
    # —— CN 宏观月频补接（2026-10-10；不入合成，作独立读数）——
    "MACRO.CPI_YOY": "CN.CPI_YOY",                 # 通胀（紧缩风险口径）
    "MACRO.CPI_MOM": "CN.CPI_MOM",                 # 通胀动能（环比）
    "MACRO.PPI_YOY": "CN.PPI_YOY",                 # 顺周期盈利口径
    "MACRO.PPI_CPI_SCISSOR": "CN.PPI_CPI_SCISSOR",  # 上下游利润分配（neutral）
    "MACRO.CGPI_YOY": "CN.CGPI_YOY",                # 企业商品价格总指数（先于CPI/PPI）
    "MACRO.CGPI_AGRI_YOY": "CN.CGPI_AGRI_YOY",      # CGPI 农产品分项
    "MACRO.CGPI_MINING_YOY": "CN.CGPI_MINING_YOY",  # CGPI 矿产品分项（上游）
    "MACRO.CGPI_ENERGY_YOY": "CN.CGPI_ENERGY_YOY",  # CGPI 煤油电分项（能源成本）
    "MACRO.M1_YOY": "CN.M1_YOY",                   # 资金活化
    "MACRO.M2_YOY": "CN.M2_YOY",                   # 流动性总量
    "MACRO.M1_M2_SCISSOR": "CN.M1_M2_SCISSOR",     # 资金活化程度（顺周期领先）
    "MACRO.LPR_1Y": "CN.LPR_1Y",                   # 政策利率锚
    "MACRO.LPR_5Y": "CN.LPR_5Y",                   # 房贷利率锚（地产链）
    "MACRO.BOND_10Y": "CN.BOND_10Y",               # 10Y 国债（neutral；ERP/期限利差输入）
    "MACRO.PMI_MFG": "CN.PMI_MFG",                 # 制造业景气
    "MACRO.PMI_NONMFG": "CN.PMI_NONMFG",           # 非制造业景气
    "MACRO.PMI_NEW_ORDERS": "CN.PMI_NEW_ORDERS",   # PMI 最领先分项
    "MACRO.INDUSTRIAL_PROFIT_YOY": "CN.INDUSTRIAL_PROFIT_YOY",   # A 股盈利宏观代理
    "MACRO.POWER_GEN_YOY": "CN.POWER_GEN_YOY",     # 实体活动量（克强口径）
    "MACRO.FREIGHT_YOY": "CN.FREIGHT_YOY",         # 实体活动量（克强口径）
}

PIT_MAP_HK = {
    "FLOW.HIBOR_3M": "HK.HIBOR_3M",
    # 外部变量对港股解释力更高（market_profile §3.4 港股特殊性）→ 同样接入
    "MACRO.CN_US_SPREAD": "CN.US_SPREAD_10Y",
    "MACRO.US_TERM_SPREAD": "US.TERM_SPREAD_10Y_2Y",
    # 中国信用（绝对利差 + 等级利差）：既是中国信用周期的读数，也是 §5.4 风险偏好指数的成分
    #   —— 该指数成分以 GLOBAL 为主，若 HK 侧缺信用成分会与 CN 侧口径不一致
    "RISK.CREDIT_SPREAD": "CN.CREDIT_SPREAD_MTN_AAA_10Y",
    "RISK.GRADE_SPREAD": "CN.GRADE_SPREAD_AA_AAA_5Y",
    "VAL.DIV_SPREAD": "HK.DIV_SPREAD",             # 股息利差（港股：恒指股息率 − 美债10Y，月频）
    "MACRO.TSF_STOCK_YOY": "CN.TSF_STOCK_YOY",     # 社融存量同比（中国信用扩张读数）
    # —— CN 宏观月频补接（market_scope=CN+HK 者；不入合成，作独立读数）——
    "MACRO.M1_YOY": "CN.M1_YOY",                   # 资金活化
    "MACRO.M2_YOY": "CN.M2_YOY",                   # 流动性总量
    "MACRO.M1_M2_SCISSOR": "CN.M1_M2_SCISSOR",     # 资金活化程度（顺周期领先）
    "MACRO.PMI_MFG": "CN.PMI_MFG",                 # 制造业景气
    "MACRO.PMI_NEW_ORDERS": "CN.PMI_NEW_ORDERS",   # PMI 最领先分项
}

# 水平值直连：(指标, 源编码, 源所在层)。取**水平值**，同日对齐到本市场交易日 —— 因源都是
#   市场报价（无发布滞后）故不走 PIT 填充。kind 决定从哪层读：
#     "bench" → public.daily_benchmark（bench_code）
#     "macro" → regime.macro_series（series_code，商品期货收盘价等）
LEVEL_MAP = (
    ("MACRO.DXY", "US.DXY", "bench"),
    ("MACRO.DTWEXBGS", "US.DTWEXBGS", "bench"),
    ("MACRO.FED_FUNDS", "US.EFFR", "bench"),
    ("RISK.VIX", "US.VIXCLS", "bench"),
    ("MACRO.BREAKEVEN_10Y", "US.T10YIE", "bench"),
    # 美债 10Y/2Y：与 MACRO.BOND_10Y（中债 10Y）对称，使美债曲线全段可分位化。
    # 此前只有利差（MACRO.US_TERM_SPREAD）入库，两腿水平值仅存于 daily_benchmark。
    ("MACRO.US_10Y", "US.DGS10", "bench"),
    ("MACRO.US_2Y", "US.DGS2", "bench"),
    ("MACRO.USDCNY", "FX.USDCNY", "bench"),
    # 避险货币（§4.2 汇率：避险货币走强 → 新兴市场承压）。⚠ 兑人民币交叉盘 → 含人民币
    #   自身强弱，判读须与 MACRO.USDCNY 联读（见各自的 remark）。
    ("MACRO.JPYCNY", "FX.JPYCNY", "bench"),
    ("MACRO.CHFCNY", "FX.CHFCNY", "bench"),
    # 商品**价格水平**（4.2 跨资产）：此前只登记了 20 日动量（MOM_MAP），水平值虽已在库却
    #   取不到分位 —— 「铜价历史高位」这类读数缺了就没法判断。
    ("MACRO.COPPER", "CMDTY.COPPER", "macro"),
    ("MACRO.GOLD", "CMDTY.GOLD", "macro"),
    ("MACRO.OIL", "CMDTY.OIL", "macro"),
)

# 动量/变化类：(指标, 源编码, 源所在层, 变化口径)。窗口统一 MOM_WINDOW 个交易日。
#   口径两种：
#     · "pct"  百分比变化 —— 适用于价格/指数/汇率水平（s/s.shift(N)−1 ×100）；
#     · "diff" 绝对变化   —— **适用于收益率**：它本身以 % 计价且可能贴近 0，「百分比变化」
#       会严重失真（1.65%→1.70% 的 "3% 涨幅" 实为 +5bp；若收益率从 0.2%→0.3% 更是 "50% 涨幅"）
#       → 一律记 **pp**。
#   ⚠ 变化必须在**源市场交易日**上算，再对齐本市场：直接用本市场日历 shift 一个含外盘假期
#     NaN 的序列会错位（美/港/内地假期不同）。
MOM_WINDOW = 20
MOM_MAP = (
    ("MACRO.SPX_MOM20", "US.SP500", "bench", "pct"),
    ("MACRO.COPPER_MOM20", "CMDTY.COPPER", "macro", "pct"),
    ("MACRO.GOLD_MOM20", "CMDTY.GOLD", "macro", "pct"),
    ("MACRO.OIL_MOM20", "CMDTY.OIL", "macro", "pct"),
    # ── §4.2「10Y 收益率方向」与「强美元 / 人民币」：此前只有水平值，方向需消费方自算 ──
    ("MACRO.BOND_10Y_CHG20", "CN.BOND_10Y", "macro", "diff"),   # pp（中国利率）
    ("MACRO.US_10Y_CHG20", "US.DGS10", "bench", "diff"),        # pp（全球无风险利率）
    ("MACRO.DXY_CHG20", "US.DXY", "bench", "pct"),              # %（强美元 → EM 承压）
    ("MACRO.USDCNY_CHG20", "FX.USDCNY", "bench", "pct"),        # %（上行 = 人民币贬值）
    # ── §4.2.1 要求「投票一律用边际变化」：信用与波动率两组此前**只有水平序列**，
    #    按规格须降级标注；补这两条变化序列后即可按变化口径投票（它们是经济学上
    #    预测力最强的两组 —— 信用利差领先股票调整、波动率抬头先于跳空风险）。
    ("RISK.CREDIT_SPREAD_CHG20", "CN.CREDIT_SPREAD_MTN_AAA_10Y", "macro", "diff"),  # pp
    ("RISK.VIX_CHG20", "US.VIXCLS", "bench", "diff"),           # 波动率点数（VIX 惯例按"点"读）
)


def _build_indicator_sources() -> dict:
    """指标 → 原始源编码（INDICATOR_SOURCES 的构造；供 indicator_dict.source 填充）。

    跨市场差异：同一指标在 CN/HK 两侧源不同时以 " / " 并列（当前无此情况）。
    `bench:` 前缀标明源在 public.daily_benchmark；无前缀者在 regime.macro_series。
    """
    out: dict = {}
    for mp in (PIT_MAP_CN, PIT_MAP_HK):
        for ind, src in mp.items():
            if ind in out and out[ind] != src:
                out[ind] = f"{out[ind]} / {src}"
            else:
                out.setdefault(ind, src)
    for ind, src, kind in LEVEL_MAP:
        out[ind] = f"bench:{src}" if kind == "bench" else src
    for ind, src, kind, _mode in MOM_MAP:
        out[ind] = src if kind == "macro" else f"bench:{src}"
    return out


INDICATOR_SOURCES = _build_indicator_sources()
"""代码侧声明的「指标 → 源序列」映射（45 条；其余指标由公式从多源计算，见 formula）。"""


# ── 覆盖度门禁（2026-10-14 事故驱动）─────────────────────────────────────────
# 事故：hk_daily_quote 当日只落了监控池 77 只（全市场采集因单只复牌股量比溢出
#   整批失败，见 get_hk_market_turnover._save_quotes），calc_hk 不设门禁照算
#   → 港股 PE 13.6 / PB 2.72 / 破净 14.3% / 涨跌家数 27.3% 等被写进
#   regime.indicator_value，还进了分位与合成；且 only_if_changed 不会自己覆盖，
#   要等下一次重算才可能修好 → 静默错一整天。
# 判据：当日样本数 < 近 _COVERAGE_WINDOW 日中位数 × _COVERAGE_MIN_RATIO。
# 阈值实测（2025-06 起 337 个交易日，A/HK 双市场）：正常日 p05 = 0.95、最低 0.79
#   （半日市）；塌陷日为 0.20~0.25（10-02 / 10-06 / 10-07 / 10-14），
#   隔壁 09-25 = 0.75（轻微缺口）。取 0.6 落在 0.25 与 0.75 的空隙正中：
#   **只作废灾难日，不误伤半日市与轻微缺口**；后者由 monitor_collector 的
#   data_coverage 告警提醒人工回补（见该文件 _check_coverage）。
_COVERAGE_WINDOW = 20            # 中位数窗口（交易日）
_COVERAGE_MIN_PERIODS = 5        # 窗口最小样本（冷启动/分块首段）
_COVERAGE_MIN_RATIO = 0.6        # 低于此比值 → 该日横截面整体作废
_COVERAGE_WARN_RATIO = 0.9       # 低于此比值 → 仅打印告警（不作废）

# 口径依赖「当日全市场横截面」的指标：覆盖度塌陷日必须整体作废（值置 NaN 且清库）。
# 不在列的指标（基准指数波动/宏观 PIT/资金流外部源等）与当日样本数无关，照常计算。
_CROSS_SECTIONAL_CODES = (
    "BREADTH.ADV_RATIO", "BREADTH.MCCLELLAN", "BREADTH.ADV_MINUS_DEC", "BREADTH.LIMIT_NET",
    "BREADTH.NEW_HIGH_LOW_60", "BREADTH.PCT_ABOVE_MA60", "BREADTH.PCT_ABOVE_MA200",
    "BREADTH.EQ_VS_CAP_20D",
    "VAL.PE_TTM_MEDIAN", "VAL.PB_MEDIAN", "VAL.PCT_BELOW_PB_1", "VAL.ERP",
    "RISK.EXTREME_DOWN_PCT", "RISK.SYNC_DOWN_20D",
    "FLOW.TURNOVER_RATIO", "FLOW.MARGIN_RATIO",
)

_INVALID_DATES_KEY = "__invalid_dates__"
"""calc_cn / calc_hk → _write_market 的**带外通道**：覆盖度塌陷的交易日集合。

放在返回 dict 里（而不是改函数签名/新增返回值）以保持既有调用与测试接口不变；
_write_market 会先 pop 掉这个键，它不会进入指标 DataFrame。
"""


def _coverage_gate(df: pd.DataFrame, label: str, count_col: str = "n") -> set:
    """把「横截面样本塌陷」的交易日在 df 上整行置 NaN，返回这些日期。

    置 NaN 而不是只把输出序列改掉：滚动/EMA 派生序列（RISK.SYNC_DOWN_20D、
    BREADTH.MCCLELLAN）必须在**输入层**就看不到脏日，否则污染会顺延 ~20 个交易日。
    """
    if df is None or df.empty or count_col not in df.columns:
        return set()
    n = pd.to_numeric(df[count_col], errors="coerce")
    med = n.rolling(_COVERAGE_WINDOW, min_periods=_COVERAGE_MIN_PERIODS).median()
    ratio = n / med.replace(0, np.nan)
    bad = (ratio.notna() & (ratio < _COVERAGE_MIN_RATIO))
    warn = (ratio.notna() & (ratio < _COVERAGE_WARN_RATIO) & ~bad)
    for d in df.index[warn]:
        print(f"    ⚠ {label} {d} 横截面样本偏低：{int(n[d])} 只"
              f"（近{_COVERAGE_WINDOW}日中位 {med[d]:.0f}，比 {ratio[d]:.2f}）→ 不作废，但建议回补")
    if not bad.any():
        return set()
    for d in df.index[bad]:
        print(f"    ❌ {label} {d} 横截面样本塌陷：{int(n[d])} 只"
              f"（近{_COVERAGE_WINDOW}日中位 {med[d]:.0f}，比 {ratio[d]:.2f}）"
              f"→ 该日横截面指标与合成整体作废，并清除库内旧值")
    df.loc[bad, :] = np.nan
    return set(df.index[bad])


def _mask_days(s, days: set):
    """把 days（date 集合）对应的点置 NaN（供 wdf 派生、无滚动传播的序列用）。"""
    if not days or s is None or len(s) == 0:
        return s
    return s.mask(s.index.isin(days))


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

    # 覆盖度门禁：样本塌陷日整行置 NaN（在建序列之前 → 滚动/EMA 派生也看不到脏日）
    bad_days = _coverage_gate(df, "CN")

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
        out["BREADTH.NEW_HIGH_LOW_60"] = _mask_days(
            (wdf["nh60"] - wdf["nl60"]) / wdf["ok60"].replace(0, np.nan) * 100, bad_days)
        out["BREADTH.PCT_ABOVE_MA60"] = _mask_days(
            wdf["ab60"] / wdf["ok60"].replace(0, np.nan) * 100, bad_days)
        out["BREADTH.PCT_ABOVE_MA200"] = _mask_days(
            wdf["ab200"] / wdf["ok200"].replace(0, np.nan) * 100, bad_days)

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

    # ⑥ ERP = 1/PE − 中国10Y国债（日频口径：个股聚合 PE 中位数，自 2018）
    #    债券腿取全历史（~5000 行，开销可忽略），以便同时服务 VAL.ERP 与 VAL.ERP_LG。
    b10 = _q(conn, """SELECT period_date, value FROM regime.macro_series
                      WHERE series_code='CN.BOND_10Y' ORDER BY period_date""")
    b10_all = _series_from_df(b10, "period_date", "value")
    b10s = b10_all.reindex(out["VAL.PE_TTM_MEDIAN"].index).ffill()
    out["VAL.ERP"] = 1.0 / out["VAL.PE_TTM_MEDIAN"].replace(0, np.nan) * 100 - b10s

    # ⑥b ERP_LG（乐咕乐股口径，月频，2006-03 起）：提供**口径一致的长历史**。
    #    ⚠ **刻意不与 VAL.ERP 拼接** —— 实测（2026-10-16）两口径 PE 的比值并不稳定：
    #      2018→2026 从 0.73 单调漂移到 1.08（8 年漂移 48%），ERP 之差 = 均值 0.44pp /
    #      标准差 0.40pp，已接近 VAL.ERP 自身波动（σ=0.50pp）。拼接会同时引入**水平跳变**
    #      与**伪趋势**（单一校准常数无法吸收单调漂移）；而用重叠段定校准系数又会前视。
    #      → 两口径各自独立成序列，跨口径比较由使用方自行判断。
    pe_lg = _q(conn, """SELECT period_date, value FROM regime.macro_series
                        WHERE series_code='CN.VAL_PE_MEDIAN' ORDER BY period_date""")
    if not pe_lg.empty:
        lg = _series_from_df(pe_lg, "period_date", "value")
        # 月频观测日常落在非交易日 → reindex 后 ffill 取「当日或之前最近」的收益率
        out["VAL.ERP_LG"] = 1.0 / lg.replace(0, np.nan) * 100 - b10_all.reindex(lg.index).ffill()

    # ⑦ 宏观接入（PIT 填充到交易日）：信用利差 / 期限利差 / 信贷脉冲代理（映射见 PIT_MAP_CN）
    out.update(_macro_pit_map(conn, out["BREADTH.ADV_RATIO"].index, PIT_MAP_CN))
    out["MACRO.CREDIT_IMPULSE"] = _credit_impulse(conn, out["BREADTH.ADV_RATIO"].index)

    # ⑧ 分析师一致预期（修正宽度 / 评级乐观度）
    out.update(_forecast_indicators(conn, out["BREADTH.ADV_RATIO"].index))

    # ⑨ 情绪事件（IPO / 基金发行 / 解禁 / 董监高增减持）
    out.update(_event_indicators(conn, out["BREADTH.ADV_RATIO"].index))

    # ⑩ 行业层（申万一级行业宽度/动量分化 + 行业资金流；不入合成）
    out.update(_sector_indicators(conn, out["BREADTH.ADV_RATIO"].index))

    # ⑪ 产出缺口 + 美林时钟（季度输入 + 月度通胀方向；逐期单边滤波 → 无前视；不入合成）
    gap_pts = _output_gap_points(conn)
    out["MACRO.OUTPUT_GAP"] = _pit_fill(out["BREADTH.ADV_RATIO"].index, gap_pts)
    out["MACRO.CLOCK_QUADRANT"] = _pit_fill(out["BREADTH.ADV_RATIO"].index,
                                            _clock_points(conn, gap_pts))

    # ⑫ §3.4 外部环境 + §5.4 跨资产成分（复用已采序列；供风险偏好指数用）
    out.update(_global_block(conn, out["BREADTH.ADV_RATIO"].index))

    # ⑬ 股债相关性（§4.2 相关性异常）：60 日滚动 corr(上证日收益, −中债10Y 日变化)
    #    —— 负值=避险主导（常态），转正=宏观主导（股债对冲失效）
    out["MACRO.STOCK_BOND_CORR"] = _stock_bond_corr(conn, out["BREADTH.ADV_RATIO"].index)

    # ⑭ 巴菲特指标（§4.1 极长期估值锚）：沪深总市值 / GDP_ttm
    out["VAL.BUFFETT"] = _buffett_ratio(conn, out["BREADTH.ADV_RATIO"].index)

    clipped = {k: _clip(v, start, end) for k, v in out.items()}
    if bad_days:
        clipped[_INVALID_DATES_KEY] = bad_days
    return clipped


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

    # 覆盖度门禁：样本塌陷日整行置 NaN（2026-10-14：全市场落库失败，只剩监控池 77 只）
    bad_days = _coverage_gate(df, "HK")

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
        out["BREADTH.NEW_HIGH_LOW_60"] = _mask_days(
            (wdf["nh60"] - wdf["nl60"]) / wdf["ok60"].replace(0, np.nan) * 100, bad_days)
        out["BREADTH.PCT_ABOVE_MA60"] = _mask_days(
            wdf["ab60"] / wdf["ok60"].replace(0, np.nan) * 100, bad_days)
        out["BREADTH.PCT_ABOVE_MA200"] = _mask_days(
            wdf["ab200"] / wdf["ok200"].replace(0, np.nan) * 100, bad_days)

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
    #   ⚠ **前视修正**：美债（美股标的）T 日收盘在北京时间 T+1 上午才可得。原实现用
    #     `reindex(港股交易日).ffill()`（同日对齐）——实时运行时因库里还没有当天美债行而
    #     自然退化到前值（碰巧正确），但**回填时**当天行已在库里 → 港股 T 日用上了 T 日
    #     美债收盘（实为 T+1 凌晨才发生）→ 前视 1 天，且回填历史与实盘口径不一致。
    #     改为：把美债索引平移到「可得日」(T+1)，再用 merge_asof 向后对齐——周末/假期
    #     的近似值由 asof 自然落到前一个可得日，不需要逐日 reindex。
    dgs = _q(conn, """SELECT trade_date, last_price FROM daily_benchmark
                      WHERE bench_code='US.DGS10' AND trade_date BETWEEN %s AND %s ORDER BY 1""",
             [w_start, end])
    dgs_s = _series_from_df(dgs, "trade_date", "last_price")
    if not dgs_s.empty:
        dgs_s.index = [d + datetime.timedelta(days=US_BENCH_LAG_DAYS) for d in dgs_s.index]
    dgs_s = _align_close(dgs_s, out["VAL.PE_TTM_MEDIAN"].index)
    out["VAL.ERP"] = 1.0 / out["VAL.PE_TTM_MEDIAN"].replace(0, np.nan) * 100 - dgs_s

    # 宏观接入：HIBOR 3M（港元流动性）+ 中国信用脉冲（HK 用其作为边际驱动代理），PIT 填充
    #   （映射见 PIT_MAP_HK）
    out.update(_macro_pit_map(conn, out["VAL.PE_TTM_MEDIAN"].index, PIT_MAP_HK))
    out["MACRO.CREDIT_IMPULSE"] = _credit_impulse(conn, out["VAL.PE_TTM_MEDIAN"].index)

    # §3.4 外部环境 + §5.4 跨资产成分（GLOBAL 口径 → 双市场同算，风险偏好指数才能各自落地）
    out.update(_global_block(conn, out["VAL.PE_TTM_MEDIAN"].index))

    # §4.2 相关性异常（HK）：60 日滚动 corr(恒指日收益, −美债10Y 日变化)。
    #   香港无本地长端利率，美债 10Y（经 HKD 联系汇率）才是港股的实际无风险利率
    #   → 与 CN 侧「上证 × 中债10Y」同构，但债券腿在基准层。
    out["MACRO.STOCK_BOND_CORR"] = _stock_bond_corr(
        conn, out["VAL.PE_TTM_MEDIAN"].index,
        stock_code="HK.800000", bond_series="US.DGS10", bond_kind="bench")

    clipped = {k: _clip(v, start, end) for k, v in out.items()}
    if bad_days:
        clipped[_INVALID_DATES_KEY] = bad_days
    return clipped


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


def _pit_fill(idx, points: list) -> pd.Series:
    """[(release_time, value)] → 按 PIT 前向填充到交易日（口径同 `_macro_pit_map`）。

    用于**计算得出**的序列（产出缺口、美林时钟）：这些序列不在 macro_series 里，
    但同样必须按「源观测的 release_time」推进，否则季频输入会以 period_date 提前可见（前视）。
    """
    if not points:
        return pd.Series(np.nan, index=idx)
    left = pd.DataFrame({"t": [pd.Timestamp(datetime.datetime.combine(d, datetime.time(23, 59)))
                               for d in idx]}).sort_values("t").reset_index(drop=True)
    rel = _naive_local(pd.Series([p[0] for p in points]))
    right = pd.DataFrame({"rel": rel.to_numpy(), "v": [float(p[1]) for p in points]})
    right = right.dropna(subset=["rel"]).sort_values("rel")
    if right.empty:
        return pd.Series(np.nan, index=idx)
    merged = pd.merge_asof(left, right, left_on="t", right_on="rel", direction="backward")
    return pd.Series(merged["v"].to_numpy(), index=idx)


_HP_LAMBDA = 1600.0      # HP 滤波 λ：标准季频「水平」序列取值
_GAP_MIN_Q = 12          # 产出缺口前 12 个季度不出值（HP 在极短样本上不稳）


def _hp_trend(y: np.ndarray, lam: float = _HP_LAMBDA) -> np.ndarray:
    """Hodrick-Prescott 趋势（scipy 稀疏三对角解；本环境未装 statsmodels）。"""
    n = len(y)
    if n < 3:
        return np.asarray(y, dtype=float).copy()
    from scipy import sparse
    from scipy.sparse.linalg import spsolve
    eye = sparse.eye(n, format="csc")
    d = sparse.diags([1.0, -2.0, 1.0], [0, 1, 2], shape=(n - 2, n), format="csc")
    return np.asarray(spsolve((eye + lam * (d.T @ d)).tocsc(),
                              np.asarray(y, dtype=float))).ravel()


def _output_gap_points(conn) -> list:
    """产出缺口（水平口径 %）→ [(release_time, gap)]。

    · 实际水平：用 NBS **已季调**的单季环比（`CN.GDP_QOQ`）链成水平指数
      （基期任取 —— 缺口是「相对潜在水平的百分比」，与基期无关）；
    · 潜在水平：**单边** HP（λ=1600）—— 对第 i 期只用 `lv[:i+1]` 重算并取末点。
      ⚠ HP 默认是**双边**滤波（用到未来期），全样本跑一遍取趋势 = 前视，必须逐期重算；
    · 样本不足 12 个季度不出值（季频 → 因此缺口自 2014Q1 起有值）。
    """
    df = _q(conn, """SELECT release_time, value FROM regime.macro_series
                     WHERE series_code='CN.GDP_QOQ' AND revision=0 ORDER BY release_time""")
    if df.empty:
        return []
    qoq = pd.to_numeric(df["value"], errors="coerce").to_numpy(dtype=float)
    qoq = np.where(np.isnan(qoq), 0.0, qoq)
    lv = 100.0 * np.cumprod(1.0 + qoq / 100.0)
    out = []
    for i in range(_GAP_MIN_Q - 1, len(lv)):
        pot = float(_hp_trend(lv[: i + 1])[-1])
        if pot:
            out.append((df["release_time"].iloc[i], (lv[i] - pot) / pot * 100.0))
    return out


def _clock_map(growth_up: bool, infl_up: bool) -> int:
    """美林投资时钟四象限：1=复苏(增长↑/通胀↓) 2=过热(↑/↑) 3=滞胀(↓/↑) 4=衰退(↓/↓)。"""
    if growth_up:
        return 1 if not infl_up else 2
    return 3 if infl_up else 4


def _clock_points(conn, gap_pts: list) -> list:
    """美林四象限 → [(release_time, 1..4)]。

    增长轴 = 产出缺口的方向（本季 vs 上季）；通胀轴 = CPI 同比的**6 个月**方向（去掉单月噪声）。
    两条轴各按自己的源 release_time 推进，任一轴更新时重算象限 → 无前视。
    """
    if len(gap_pts) < 2:
        return []
    cpi = _q(conn, """SELECT release_time, value FROM regime.macro_series
                      WHERE series_code='CN.CPI_YOY' AND revision=0 ORDER BY release_time""")
    if len(cpi) < 7:
        return []
    cvals = pd.to_numeric(cpi["value"], errors="coerce").to_numpy(dtype=float)
    events = [(gap_pts[i][0], "g", gap_pts[i][1] - gap_pts[i - 1][1] > 0)
              for i in range(1, len(gap_pts))]
    events += [(cpi["release_time"].iloc[k], "i", cvals[k] - cvals[k - 6] > 0)
               for k in range(6, len(cvals))]
    events.sort(key=lambda x: x[0])
    cur_g = cur_i = None
    out = []
    for rel, kind, up in events:
        if kind == "g":
            cur_g = up
        else:
            cur_i = up
        if cur_g is not None and cur_i is not None:
            out.append((rel, float(_clock_map(cur_g, cur_i))))
    return out


def _align_close(src: pd.Series, idx) -> pd.Series:
    """源日历上的收盘序列 → 对齐到本市场交易日（merge_asof backward，无前视）。

    注意：两侧时间列必须**统一精度**（`merge_asof` 要求 dtype 完全一致；本市场索引建出来是
    `M8[us]`、`pd.to_datetime(date 索引)` 是 `M8[s]` → 不统一会报 incompatible merge keys）。
    """
    if src.empty:
        return pd.Series(np.nan, index=idx)
    left = pd.DataFrame({"t": pd.to_datetime(
        [pd.Timestamp(datetime.datetime.combine(d, datetime.time(23, 59))) for d in idx]
    ).astype("datetime64[ns]")}).sort_values("t").reset_index(drop=True)
    right = pd.DataFrame({"rel": pd.to_datetime(src.index).astype("datetime64[ns]"),
                          "v": src.to_numpy(dtype=float)})
    right = right.dropna(subset=["rel"]).sort_values("rel")
    merged = pd.merge_asof(left, right, left_on="t", right_on="rel", direction="backward")
    return pd.Series(merged["v"].to_numpy(), index=idx)


def _expanding_zscore(s: pd.Series, min_periods: int) -> pd.Series:
    """扩展窗口 z-score = (x − 累计均值) / 累计标准差（只用 t 及之前，无前视）。

    与 `_expanding_percentile` 对称：都必须在**全历史**上算（增量运行时只看窗口会算错
    均值/标准差），故调用点在 `_write_market` 的 combined 上，而非 calc_* 内部。
    样本不足 min_periods 个有效观测时为 NaN；标准差为 0 时也返回 NaN（避免除零爆值）。
    """
    m = s.expanding(min_periods=min_periods).mean()
    sd = s.expanding(min_periods=min_periods).std()
    return (s - m) / sd.replace(0, np.nan)


US_BENCH_LAG_DAYS = 1
"""美股标的（`US.*` 基准）的可得滞后（自然日）。

美东交易日 T 的收盘在北京时间 **T+1 上午**才可得（夏令时约 04:00、冬令时约 05:00，
FRED 再滞后约 15 分钟）。凡把美股基准当作「T 日本市场可得」的地方都必须 +1 天，
否则回填历史时会把「T+1 凌晨才发生的事」算进 T 日的指标 —— 即前视。

⚠ 与 `get_macro_daily.US_BENCH_LAG_DAYS` **必须一致**（两处各自定义，因为两个文件
   不互相 import；改一处务必改另一处）。
"""

_CORR_WINDOW = 60          # 股债相关性的滚动窗口（交易日）


def _stock_bond_corr(conn, idx, stock_code: str = "SH.000001",
                     bond_series: str = "CN.BOND_10Y",
                     bond_kind: str = "macro") -> pd.Series:
    """60 日滚动「股债相关性」= corr(股指日收益, −债券收益率日变化)（§4.2 相关性异常）。

    为什么取 −Δyield：文献里的「股债相关性」指**价格**相关性，而库里存的是**收益率**
    （yield 上行 = 债券价格下跌）→ 只有取负号才是价格口径。于是：
      · 负值（常态）= 避险主导：股跌债涨（flight to quality）；
      · 转正/持续上行 = 宏观主导：股债同跌（增长或流动性冲击），股债对冲失效、须警惕。
    ⚠ 两腿日历不同（股指交易日 vs 国债收益率交易日）→ 各自算日变化后仅在**共同**日期上
      算相关，缺失日不进窗口（不做 ffill —— 用陈旧收益率拼接会伪造相关性）。
    `bond_kind`: 债券腿来源 —— "macro"（regime.macro_series，如 CN.BOND_10Y）或
      "bench"（public.daily_benchmark，如 US.DGS10）。HK 侧用「恒指 × 美债10Y」：
      香港无本地长端利率，美债 10Y 才是港股的实际无风险利率（HKD 联系汇率）。
      ⚠ 美股标的的可得时点实为北京次日凌晨，本函数按同日取用 → 60 日滚动相关受影响很小
        （仅一条腿单日错位），但与「严格 PIT」有细微偏差，跨市场比较时注意。
    """
    px = _q(conn, """SELECT trade_date, last_price FROM daily_benchmark
                     WHERE bench_code = %s ORDER BY trade_date""", [stock_code])
    if bond_kind == "bench":
        yl = _q(conn, """SELECT trade_date AS period_date, last_price AS value
                         FROM daily_benchmark WHERE bench_code = %s ORDER BY trade_date""",
                [bond_series])
    else:
        yl = _q(conn, """SELECT period_date, value FROM regime.macro_series
                         WHERE series_code = %s AND revision = 0 ORDER BY period_date""",
                [bond_series])
    if px.empty or yl.empty:
        return pd.Series(np.nan, index=idx)
    sp = pd.Series(pd.to_numeric(px["last_price"], errors="coerce").to_numpy(),
                   index=pd.to_datetime(px["trade_date"])).dropna()
    yv = pd.Series(pd.to_numeric(yl["value"], errors="coerce").to_numpy(),
                   index=pd.to_datetime(yl["period_date"])).dropna()
    # DataFrame 构造即按索引对齐 → dropna 后只剩两腿**共同**的日期
    pair = pd.DataFrame({"ret": sp.pct_change() * 100.0, "dneg": -yv.diff()}).dropna()
    if len(pair) < _CORR_WINDOW:
        return pd.Series(np.nan, index=idx)
    corr = pair["ret"].rolling(_CORR_WINDOW).corr(pair["dneg"]).dropna()
    return _align_close(corr, idx)


def _global_block(conn, idx) -> dict:
    """§3.4 外部环境 + §5.4 跨资产成分（**零新增采集**：daily_benchmark 每小时已在采、
    CMDTY.* 日频行情已在库）。

    · 水平类：美元指数双口径（ICE DXY / Fed 贸易加权）、VIX、有效联邦基金利率、
      美债 10Y·2Y、盈亏平衡通胀、在岸人民币、商品价格水平（铜/金/油）（映射见 LEVEL_MAP）；
    · 动量类：标普 500 / 铜 / 黄金 / 原油的 **20 个交易日**动量（映射见 MOM_MAP）。
      ⚠ 动量必须在**各自源市场的交易日**上算（20 个交易日 = s/s.shift(20)−1），再对齐到本市场：
      直接用本市场日历 shift 一个含外盘假期 NaN 的序列会错位（美/港/内地假期不同）。

    ⚠ **已知口径（未修，勿误认为已 PIT 正确）**：本节全部按「同日对齐」（`_align_close`）——
      对**A 股/港股/中债/在岸汇率**成立（本地市场，当日盘后即掌握）；但对 `US.*` 基准
      **不成立**：其 T 日收盘在北京时间 T+1 上午才可得（见 `US_BENCH_LAG_DAYS`），
      故 `MACRO.US_10Y` / `MACRO.VIX` / `MACRO.DXY` 等在 CN/HK 的 T 日读数实际上是
      「T+1 凌晨才发生的事」→ 回填历史时前视 1 天。
      本轮只修了**宣称 PIT 的三处**（派生层三条跨表派生 + HK 的 `VAL.ERP`）；
      本节之所以暂不修：会平移约 15 个指标的双侧历史与全部合成分（温度计/风险/FSI/风险偏好），
      属独立变更，需单独评估影响面后再动。
    """
    out: dict = {}

    def _bench(code):
        df = _q(conn, """SELECT trade_date, last_price FROM daily_benchmark
                         WHERE bench_code = %s ORDER BY trade_date""", [code])
        if df.empty:
            return pd.Series(dtype=float)
        s = pd.Series(pd.to_numeric(df["last_price"], errors="coerce").to_numpy(),
                      index=pd.to_datetime(df["trade_date"]))
        return s[~s.index.duplicated(keep="last")].sort_index()

    def _macro(src):
        df = _q(conn, """SELECT period_date, value FROM regime.macro_series
                         WHERE series_code = %s AND revision = 0 ORDER BY period_date""", [src])
        if df.empty:
            return pd.Series(dtype=float)
        s = pd.Series(pd.to_numeric(df["value"], errors="coerce").to_numpy(),
                      index=pd.to_datetime(df["period_date"]))
        return s[~s.index.duplicated(keep="last")].sort_index()

    loader = {"bench": _bench, "macro": _macro}
    for code, src, kind in LEVEL_MAP:
        out[code] = _align_close(loader[kind](src), idx)

    for code, src, kind, mode in MOM_MAP:
        s = loader[kind](src)
        if len(s) <= MOM_WINDOW:
            out[code] = pd.Series(dtype=float)
            continue
        # diff 用于收益率（pp）：百分比变化在低利率/贴近 0 时失真，见 MOM_MAP 注释
        chg = (s - s.shift(MOM_WINDOW)) if mode == "diff" else (s / s.shift(MOM_WINDOW) - 1) * 100
        out[code] = _align_close(chg, idx)
    return out


def _gdp_single_quarters(conn) -> list:
    """`CN.GDP`（年内累计名义值）→ 逐季**单季值**，返回 [(period_date, 单季值, 可得时间)]。

    ⚠ 差分基线必须是**前一条的累计值**，不能是「前一条的单季值」——后者会让同一年内第 3 个
      及以后的累计点重复扣除（实测 2025-07 被算成 672,573 亿，而正确值 = 1,013,968 − 659,862
      = 354,106 亿，多扣了 Q2 的单季值 341,396）→ GDP_ttm 被放大约 1.9 倍。
      该 bug 曾同时存在于 `_credit_impulse` 与 `_buffett_ratio`，故抽为本函数统一修正。
    · `period_date` 语义 = **末季度起始月**（见 macro_series_meta.CN.GDP.remark）：
      2025-10 = Q4 起始月 → 该行是 Q1~Q4 的全年累计。
    """
    gdp = _q(conn, """SELECT period_date, release_time, value FROM regime.macro_series
                      WHERE series_code='CN.GDP' AND revision=0 ORDER BY period_date""")
    if gdp.empty:
        return []
    gdp = gdp.sort_values("period_date")
    rel = _naive_local(gdp["release_time"]).to_numpy()
    out, prev_year, prev_cum = [], None, 0.0
    for i, r in enumerate(gdp.itertuples()):
        d = pd.Timestamp(r.period_date).date()
        v = float(r.value)
        base = prev_cum if d.year == prev_year else 0.0     # 年初首个点即 Q1 自身
        out.append((d, v - base, rel[i]))
        prev_year, prev_cum = d.year, v
    return out


def _credit_impulse(conn, idx) -> pd.Series:
    """信贷脉冲（Biggs/Mayer 口径）：**信用流量/GDP 比率的同比变化**（pp of GDP）。

    impulse_t = F_t/GDP_ttm_t − F_{t−12}/GDP_ttm_{t−12}，其中 F_t = 存量_t − 存量_{t−12}
    （滚动 12 个月的新增信用流量）。即「新增信用占 GDP 的比重」相对**一年前**的变化。

    量级自检（口径选择的关键）：中国年度信用流量约占 GDP 的 23-30%，
    · 若用「比率同比变化」（本实现）→ 典型区间 **±3~8pp**，2020 放水大幅转正、2021 转负 ✓ 有周期含义；
    · 若用「存量/GDP 比率的同比变化」→ 常年 +10pp（跟随 330% 的比率水平）✗ 无周期含义；
    · 若用「流量/GDP 比率的月度变化」→ 仅 ±1.5pp、噪声主导 ✗ 量级过小。

    · 存量：央行直连 `CN.TSF_STOCK`，**仅取 2019-01 起**——2018 及更早为旧口径（不含国债/
      地方政府债），跨断点差分会出现假跳变（实测 2018-12 → 2019-01 水平跳升 20%）；
    · GDP_ttm：`CN.GDP`（年内累计名义值）**差分回单季**后取滚动 4 季合计；
    · PIT：逐月按存量 release_time 推进；每个时点只取「截至该时点已发布」的 GDP 单季，
      且 t 与 t−1 两个比率用**同一 GDP vintage** 计算（不使用未来修订）。
    · 旧实现为「12M 滚动社融增量的同比变化率」代理（只有增量时的近似，且增量源已停更）。
    """
    st = _q(conn, """SELECT period_date, release_time, value FROM regime.macro_series
                     WHERE series_code='CN.TSF_STOCK' AND revision=0
                       AND period_date >= DATE '2019-01-01' ORDER BY period_date""")
    gd = _gdp_single_quarters(conn)                 # 见该函数：差分基线必须是**累计值**
    if st.empty or len(gd) < 8:
        return pd.Series(np.nan, index=idx)

    def _prev_month(d):
        return (d.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)

    def _ratio(dd, flow12, avail):
        """F/GDP_ttm × 100 —— 取「截至 dd 的最近 4 个已发布单季」为分母。"""
        q = [x for x in avail if x[0] <= dd]
        if len(q) < 4:
            return None
        ttm = sum(x[1] for x in q[-4:])
        return flow12 / ttm * 100 if ttm > 0 else None

    # ② 逐月推进：t 与 t−12 两个比率都用「截至该时点已发布」的 GDP vintage（同口径比较）
    stock = [(pd.Timestamp(r.period_date).date(), r.release_time, float(r.value))
             for r in st.itertuples()]
    by_month = {d: v for d, _, v in stock}
    points = []
    for d, rel, v in stock:
        rel_n = _naive_local(pd.Series([rel])).iloc[0]
        avail = [(dd, sv) for dd, sv, rl in gd if rl <= rel_n]
        d12 = d.replace(year=d.year - 1)
        d24 = d12.replace(year=d12.year - 1)
        v12, v24 = by_month.get(d12), by_month.get(d24)
        if None in (v12, v24):           # 需要 24 个月历史 → 存量自 2019-01 起 → 脉冲自 2021-01 起
            continue
        r_now, r_prev = _ratio(d, v - v12, avail), _ratio(d12, v12 - v24, avail)
        if r_now is None or r_prev is None:
            continue
        points.append((rel, r_now - r_prev))
    if not points:
        return pd.Series(np.nan, index=idx)
    return _pit_fill(idx, points)


def _buffett_ratio(conn, idx) -> pd.Series:
    """巴菲特指标 = 沪深股票总市值 / GDP_ttm × 100（%，§4.1 极长期估值锚）。

    · 分子：`CN.MKT_CAP_TOTAL`（沪深两市**股票**总市值，亿元；交易所每日公布）
    · 分母：`CN.GDP`（年内累计名义 GDP，亿元）→ 差分回单季 → 滚动 4 季合计（TTM）
    · PIT：分母只取「截至该市值观测**可得时点**已发布」的最近 4 个单季（与 _credit_impulse
      同一纪律，不用未来修订）；市值本身按 release_time 前向填充到交易日。
    · 量级自检：A 股总市值/GDP 长期约 60~100%（2015 牛市峰值逾 100%、2018 底约 50%），
      与「>100% 警戒」的通行判读一致。
    """
    cap = _q(conn, """SELECT period_date, release_time, value FROM regime.macro_series
                      WHERE series_code='CN.MKT_CAP_TOTAL' AND revision=0 ORDER BY period_date""")
    quarters = _gdp_single_quarters(conn)           # 见该函数：差分基线必须是**累计值**
    if cap.empty or len(quarters) < 4:
        return pd.Series(np.nan, index=idx)

    # ② 逐「市值观测」算比率：分母用与分子同一可得时点（同口径 vintage）
    points = []
    for r in cap.itertuples():
        rel_n = _naive_local(pd.Series([r.release_time])).iloc[0]
        q = [x for x in quarters if x[2] <= rel_n]
        if len(q) < 4:
            continue
        ttm = sum(x[1] for x in q[-4:])
        if ttm <= 0:
            continue
        points.append((r.release_time, float(r.value) / ttm * 100))
    if not points:
        return pd.Series(np.nan, index=idx)
    return _pit_fill(idx, points)


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

    # 跨资产风险偏好指数（§5.4）：成分各自分位化、方向统一为「高分 = 风险偏好高」，再等权。
    #   VIX(−) / 美元 DXY(−) / AAA 信用利差(−) / 信用等级利差 AA−AAA(−)
    #   / 标普动量(+) / 铜动量(+) / 黄金(−) → 0-100：>80 极度 Risk-On · <20 极度 Risk-Off。
    #   ⚠ 文档 §5.4 原列 7 项，本实现 7 项（含信用两个口径），未落两项：
    #     ①「股债相对表现」缺债券**总回报**序列（库里只有国债收益率）；
    #     ②「EM vs DM 相对表现」缺 MSCI EM 等 EM 指数。
    #   ⚠ 信用用**两个口径**：AAA 绝对利差（长历史、稳）+ AA−AAA 等级利差
    #     （更纯的风险偏好读数，但源只保留最近 3 个交易日 → 历史仅逐日累积的少数点，
    #      故只作近期补充，不能只靠它）。
    #   ⚠ 分位按**各市场自身历史**展开（与其他合成一致：thermometer/fsi 亦然）→
    #     同一时点 CN/HK 数值**可能略有差异**（CN 样本 1990 起、HK 2019 起）；同市场内可比。
    out["risk_appetite"] = _score([
        ("RISK.VIX", -1), ("MACRO.DXY", -1),
        ("RISK.CREDIT_SPREAD", -1), ("RISK.GRADE_SPREAD", -1),
        ("MACRO.SPX_MOM20", 1), ("MACRO.COPPER_MOM20", 1), ("MACRO.GOLD_MOM20", -1),
    ])

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
    # 列宽上限（indicator_dict）：name 80 / dimension 24 / unit 16 / direction 12 /
    #   freq 8 / source 64 → 超长会直接抛 StringDataRightTruncation 打断整轮计算，
    #   故在写库前**截断**（宁截断也不让采集轮次失败）。
    # ⚠ remark 已放宽为 TEXT，**不参与截断**——它是规范化三段式文档，截断即丢信息。
    lim = {"indicator_name": 80, "dimension": 24, "market_scope": 16, "unit": 16,
           "direction": 12, "freq": 8, "source": 64}

    def _cut(key, val):
        if isinstance(val, str) and len(val) > lim.get(key, 10 ** 9):
            return val[:lim[key]]
        return val

    rows = []
    for meta in INDICATORS:
        rows.append({
            "indicator_code": meta["code"], "indicator_name": _cut("indicator_name", meta["name"]),
            "scope": meta["scope"], "dimension": _cut("dimension", meta["dimension"]),
            "market_scope": _cut("market_scope", meta["market_scope"]),
            "unit": _cut("unit", meta["unit"]),
            "direction": _cut("direction", meta["direction"]),
            "freq": _cut("freq", meta["freq"]),
            # source = 「本指标引用的原始源编码」（bench: 前缀=基准层）。**由接线映射派生**，
            #   不手工维护、每轮全量重写 → 库内是代码的投影，不会漂移。由多源计算的指标
            #   （如 VAL.ERP = PE + 中债10Y）不在此列，其来源见 formula。
            "source": _cut("source", meta.get("source") or INDICATOR_SOURCES.get(meta["code"], "")),
            "formula": meta["formula"],
            "is_active": meta.get("active", True), "remark": _cut("remark", meta.get("remark")),
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

    覆盖度塌陷日（_INVALID_DATES_KEY）：横截面指标值已是 NaN → 不会写；但库内**旧值
    必须显式删**（bulk_upsert 是 skip_null_updates，NULL 不覆盖旧值），合成行整行作废
    （成分置 NaN 后 build_composites 会退化成「剩余成分平均」，仍是错的）。
    """
    invalid = series_map.pop(_INVALID_DATES_KEY, None) or set()
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

    # VAL.ERP_Z（Fed Model 的「与历史均值比较」口径）：**必须建立在全历史上** ——
    #   与分位同理，增量运行时只看窗口会算错均值/标准差，故与分位同处计算。放在 pct 构造
    #   **之前**，使其进入 pct 的列集合（并在 NO_PERCENTILE 里置空分位）。
    if "VAL.ERP" in combined.columns:
        combined["VAL.ERP_Z"] = _expanding_zscore(
            combined["VAL.ERP"].astype(float), ERP_Z_MIN_PERIODS)

    # 分类/编码类指标不做分位（如美林象限 1-4 是**类别标签**，分位没有意义）
    pct = pd.DataFrame({c: (_expanding_percentile(combined[c]) if c not in NO_PERCENTILE
                            else pd.Series(np.nan, index=combined.index))
                        for c in combined.columns}, index=combined.index)
    comp = build_composites(pct, combined, market)

    write_idx = [d for d in combined.index if isinstance(d, datetime.date) and d >= start]
    if not write:
        last = write_idx[-1] if write_idx else None
        return {"rows": 0, "dates": len(write_idx),
                "preview": (last, comp.loc[last].to_dict() if last else None)}

    # 覆盖度塌陷日清库：bulk_upsert(skip_null_updates=True) 不会用 NULL 覆盖旧值，
    # 不显式删就会把上一轮算错的垃圾永久留下（2026-10-14 的事故值即此机制留下）。
    bad_dates = sorted(d for d in invalid
                       if isinstance(d, datetime.date) and d >= start)
    if bad_dates:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM regime.indicator_value "
                        "WHERE market=%s AND trade_date = ANY(%s) AND indicator_code = ANY(%s)",
                        (market, bad_dates, list(_CROSS_SECTIONAL_CODES)))
            n_iv = cur.rowcount
            cur.execute("DELETE FROM regime.market_regime_daily "
                        "WHERE market=%s AND trade_date = ANY(%s)", (market, bad_dates))
            n_mr = cur.rowcount
        print(f"    ⚠ {market} 覆盖度塌陷日 {[str(d) for d in bad_dates]}："
              f"作废横截面指标 {n_iv} 行 + 合成 {n_mr} 行（数据补齐后重跑即恢复）")

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
    # only_if_changed=True：月/季频指标被 PIT 填充到每个交易日，值没变的行**不写**
    #   → 不再反复重写同一行；updated_at 恢复「值最后变化时间」语义（配合 indicator_dict.freq
    #     即可判断「月频指标一个月没变」是否异常）。⚠ 仅计算层可用，采集层禁用（见 bulk_upsert）
    bulk_upsert(conn, IV_TABLE, iv_rows,
                conflict_cols=["indicator_code", "market", "trade_date"], skip_null_updates=True,
                only_if_changed=True)

    mr_rows = []
    for d in write_idx:
        if d in invalid:
            continue        # 覆盖度塌陷日：合成整体作废（见函数头说明）
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
            "fsi": _r2(r["fsi"]), "risk_appetite": _r2(r.get("risk_appetite")),
            "top_signal_cnt": int(r["top_signal_cnt"]), "bottom_signal_cnt": int(r["bottom_signal_cnt"]),
            "detail": detail, "updated_at": now,
        })
    # 同 IV_TABLE：合成值未变则不重写（保守起见合成层也走同一语义）
    bulk_upsert(conn, MR_TABLE, mr_rows, conflict_cols=["trade_date", "market"],
                only_if_changed=True)
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
