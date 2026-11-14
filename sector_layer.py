#!/usr/bin/env python3
"""第④层「行业配置」计算层 · 阶段 1（CN 申万一级，独立于 market_scheduler）。

设计文档：doc/industry_allocation_feasibility.md（§3 指标体系 / §4.2 阶段 1）。

定位（与主报告 §0 一致）：**只输出状态读数与风险闸门，不输出超配/低配建议**；
不产出单一综合分，状态侧与风险侧分离。

阶段 1 范围：A 相对强弱与趋势、B 估值（横截面）、C 资金、D 拥挤度**两因子部分模式**
（k=2，最高档 HIGH，不开 EXTREME）、L2 六组维度读数、L3 状态矩阵/排名/闸门。
依赖 D1 行业成分主数据的指标（行业内宽度/离散度、财务聚合、盈利修正下沉、
换手率因子、南向/两融按行业）留到阶段 2，字典里以 is_active=false 登记。

工程红线（doc/compute_task_resource_standard.md）：
  · R1/R7：所有大表查询带时间下界；输入按 4 年分块，另用「轻量 3 列」序列算
    10 年时序分位，避免把 15 万行 × 15 列一次性拉进进程。
  · R3：独立单元 MemoryMax=512M，实测峰值须 ≤256MB。
  · R4/R5：本模块由 system/sector-layer.timer 触发，**不得**挂进 market_scheduler。
  · R6：单事务 upsert ≤2000 行，逐批 commit。
  · 监控：任务级 collection_task_log 埋点 + 表级 monitor_table_config 登记。

用法：
    python3 sector_layer.py                       # 增量（默认最近 45 天，回退 3 天自愈）
    python3 sector_layer.py --backfill            # 全量回填（1999-12-30 起，按 4 年分块）
    python3 sector_layer.py --start 2026-01-01 --end 2026-11-11
    python3 sector_layer.py --dry-run             # 只算不写（内存/口径实测用）
    python3 sector_layer.py --init-schema         # 只建表 + 同步指标字典
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import json
import math
import sys
import time

import numpy as np
import pandas as pd

from db import bulk_upsert, get_conn, read_sql
from log_utils import setup_logger

log = setup_logger("sector_layer")

MARKET = "CN"
CLASSIFICATION = "SW_L1"
CALIB_VERSION = "s2.1"          # 口径版本；阶段1→2 变更（六维启用/分母变化）触发历史重算
PIT_START = dt.date(2021, 12, 13)   # 决策A：自下而上指标有效起点（sector_stage2_agg.PIT_START）

MIN_PERIODS = 250               # §7.1 时序分位最小样本
SHORT_SAMPLE = 750              # §5.7 n_sample < 750 降置信
XSEC_MIN_N = 10                 # 横截面分位最小有效行业数
CROWD_PCT_HIGH = 80.0           # 拥挤因子命中阈值
ROLL_WINDOW = 2520              # 时序分位滚动 10 年（交易日）
CHUNK_YEARS = 4
WRITE_BATCH = 2000              # R6：单事务 ≤5 万行，取更保守的 2000
INCREMENTAL_DAYS = 45
REWIND_DAYS = 3                 # 保留迟到/修订自愈（同 backfill_sw_industry.py）
WARMUP_DAYS = 400               # RS shift(20) / rolling(20) 预热
BACKFILL_START = dt.date(1999, 12, 30)

# 阶段 2 六维全部启用。⚠ 阶段 1 分母是 3、阶段 2 是 6 —— coverage 口径断点，
# 已通过 CALIB_VERSION s1.1→s2.1 换掉全部 params_hash，两期读数不可直接比。
ENABLED_DIMENSIONS = ("TREND", "FLOW", "VALUATION", "CROWDING",
                      "FUNDAMENTAL", "BREADTH")


# ── 指标字典（代码即字典；新增指标须先在此登记）──────────────────────────────
# value_col / pct_x_col / pct_t_col / n_col 指向 _compute_frame() 产出的宽表列。
# side: state=状态侧 / risk=风险侧。direction 只在字典定义一次，消费端不得二次翻转。
def D(code, name, domain, side, unit, direction, formula, model_key,
       value_col=None, pct_x_col=None, pct_t_col=None, n_col=None,
       source="regime.sector_daily", freq="day", remark="", active=True,
       extra_kind=None):
    return dict(code=code, indicator_name=name, domain=domain, side=side,
                unit=unit, direction=direction, formula=formula, model_key=model_key,
                value_col=value_col, pct_x_col=pct_x_col, pct_t_col=pct_t_col,
                n_col=n_col, source=source, freq=freq, remark=remark,
                is_active=active, extra_kind=extra_kind)


INDICATOR_DEFS = [
    # A 相对强弱与趋势（状态侧）—— 基准是 31 行业等权均值，不是大盘指数（§7.11 决策 3）
    D("SEC.TREND.RET_20D", "行业20日收益", "TREND", "state", "pct", "neutral",
      "close_t/close_{t-20}-1（直接取 sector_daily.ret_20d）", "M0",
      value_col="ret_20d", remark="阶段1；1999-12-30 起，早期行业数不足 31"),
    D("SEC.TREND.RET_60D", "行业60日收益", "TREND", "state", "pct", "neutral",
      "close_t/close_{t-60}-1（直接取 sector_daily.ret_60d）", "M0",
      value_col="ret_60d", remark="阶段1"),
    D("SEC.TREND.RS_20D", "20日相对强度", "TREND", "state", "pct", "high_good",
      "ret_20d - 当日有效行业 ret_20d 等权均值（M3，横截面均值恒0）", "M3",
      value_col="rs_20d", remark="阶段1；**不是行业指数/大盘指数**"),
    D("SEC.TREND.RS_60D", "60日相对强度", "TREND", "state", "pct", "high_good",
      "ret_60d - 当日有效行业 ret_60d 等权均值（M3）", "M3",
      value_col="rs_60d", remark="阶段1新增；横截面均值恒0"),
    D("SEC.TREND.RS_XSEC_PCT", "相对强度横截面分位", "TREND", "state", "score_0_100", "high_good",
      "M1 中位秩：100*(n_less+0.5*n_equal)/n_valid", "M1",
      pct_x_col="rs_xsec_pct", n_col="n_valid", remark="阶段1；RRG 横轴；n_valid 随 rank_n 变化"),
    D("SEC.TREND.RS_MOM_20D", "RS分位20日变化", "TREND", "state", "score_0_100", "high_good",
      "rs_xsec_pct(t) - rs_xsec_pct(t-20)，按行业 shift 20 个交易日", "M4",
      value_col="rs_mom_20d", pct_x_col="rs_mom_xsec_pct", remark="阶段1；RRG 纵轴；不足21交易日为NULL"),
    D("SEC.TREND.RRG_QUADRANT", "RRG四象限", "TREND", "state", "enum", "neutral",
      "rs_xsec_pct>=50 & Δ>=0→1领先; <50 & Δ>=0→2走强; >=50 & Δ<0→3走弱; <50 & Δ<0→4落后", "M5",
      value_col="rrg_code", remark="阶段1；**横截面分位近似 RRG，非标准 JdK**；raw_value 存1-4编码，label 在 extra",
      extra_kind="rrg"),
    D("SEC.TREND.MOM_RANK", "动量排名", "TREND", "state", "rank", "high_good",
      "当日 ret_20d 横截面降序 rank(method=min)，直接取 sector_daily.mom_rank", "M0",
      value_col="mom_rank", pct_x_col="mom_rank_pct", n_col="n_valid",
      remark="阶段1；pct 已做反向翻转（rank 小 = 高分位）"),
    # B 估值（风险侧，高分=更贵）
    D("SEC.VAL.PE_TTM", "行业PE-TTM", "VAL", "risk", "x", "high_risk",
      "sector_valuation_snapshot.pe_ttm 原值按 snapshot_date 对齐", "M0",
      value_col="pe_ttm", source="regime.sector_valuation_snapshot",
      remark="阶段1；源只有当前横截面，2026-09-30 起逐日累积，早期 NULL"),
    D("SEC.VAL.PB", "行业PB", "VAL", "risk", "x", "high_risk",
      "sector_valuation_snapshot.pb 原值", "M0",
      value_col="pb", source="regime.sector_valuation_snapshot", remark="阶段1"),
    D("SEC.VAL.DIV_YIELD", "行业股息率", "VAL", "risk", "pct", "high_good",
      "sector_valuation_snapshot.div_yield 原值", "M0",
      value_col="div_yield", source="regime.sector_valuation_snapshot", remark="阶段1"),
    D("SEC.VAL.PE_XSEC_PCT", "PE横截面分位", "VAL", "risk", "score_0_100", "high_risk",
      "M1(pe_ttm)，越高越贵", "M1",
      pct_x_col="pe_xsec_pct", n_col="n_val", source="regime.sector_valuation_snapshot",
      remark="阶段1；**估值维度单锚**；不依赖历史深度"),
    D("SEC.VAL.PB_XSEC_PCT", "PB横截面分位", "VAL", "risk", "score_0_100", "high_risk",
      "M1(pb)", "M1", pct_x_col="pb_xsec_pct", n_col="n_val",
      source="regime.sector_valuation_snapshot", remark="阶段1；概念同源，降级为校验项不重复计权"),
    D("SEC.VAL.REL_TO_MARKET", "行业PE/市场PE中位数", "VAL", "risk", "ratio", "high_risk",
      "pe_ttm / 当日 31 行业 pe_ttm 中位数", "M0",
      value_col="rel_to_market", source="regime.sector_valuation_snapshot",
      remark="阶段1；与 PE_XSEC_PCT 高冗余，仅作校验（§7.2 去重）"),
    D("SEC.VAL.PE_TS_PCT", "PE时序分位", "VAL", "risk", "score_0_100", "high_risk",
      "M2 expanding，min_periods=250", "M2",
      pct_t_col="pe_ts_pct", n_col="pe_n_sample", source="regime.sector_valuation_snapshot",
      remark="🔴 阶段1恒不可用：实测仅27个快照日 <250；只写 n_sample，不产出分位"),
    # C 资金（状态侧）
    D("SEC.FLOW.NET_1D", "行业资金净流入", "FLOW", "state", "yi_yuan", "high_good",
      "sector_daily.fund_flow_net（同花顺→申万映射汇总，confirmed 准入）", "M0",
      value_col="fund_flow_net", remark="阶段1；源无历史，2026-09-30 起；31行业合计=市场级 SECTOR.FLOW_NET"),
    D("SEC.FLOW.NET_5D", "近5日资金净流入", "FLOW", "state", "yi_yuan", "high_good",
      "rolling_sum(5, min_periods=5)，窗口不足不给值（§7.11）", "M4",
      value_col="fund_flow_net_5d", remark="阶段1；直接取 sector_daily.fund_flow_net_5d"),
    D("SEC.FLOW.NET_20D", "近20日资金净流入", "FLOW", "state", "yi_yuan", "high_good",
      "rolling_sum(20, min_periods=20)", "M4",
      value_col="net_20d", remark="阶段1新增；需≥20个有资金流的交易日"),
    D("SEC.FLOW.NET_NORM", "归一资金净额", "FLOW", "state", "ratio", "high_good",
      "net_1d / 行业成交额（同为亿元；阶段0已标定 turnover 口径）", "M4",
      value_col="net_norm", remark="阶段1；净额随市场规模漂移，未归一不得取时序分位（§5.7）"),
    D("SEC.FLOW.XSEC_PCT", "资金净额横截面分位", "FLOW", "state", "score_0_100", "high_good",
      "M1(net_norm)", "M1", pct_x_col="flow_xsec_pct", n_col="n_flow",
      remark="阶段1；**资金维度单锚**"),
    D("SEC.FLOW.FLOW_RANK", "资金流排名", "FLOW", "state", "rank", "high_good",
      "直接取 sector_daily.flow_rank，pct 做反向翻转", "M0",
      value_col="flow_rank", pct_x_col="flow_rank_pct", n_col="n_flow", remark="阶段1"),
    D("SEC.FLOW.PRICE_FLOW_GAP", "量价-资金背离", "FLOW", "state", "rank", "neutral",
      "rs_rank - flow_rank；正=资金排名领先于价格排名", "M0",
      value_col="price_flow_gap",
      remark="阶段1；**纯描述性读数，不作预测信号**（direction=neutral，不进维度分）"),
    # D 拥挤度（风险侧；阶段1 两因子部分模式）
    D("SEC.CROWD.AMT_SHARE_PCT", "成交额占比时序分位", "CROWD", "risk", "score_0_100", "high_risk",
      "share=turnover/当日有效行业合计 → M2 滚动10年(min_periods=250)", "M2",
      value_col="amt_share", pct_t_col="amt_share_pct", n_col="amt_share_n",
      remark="阶段1；阶段0实测 turnover 为亿元且覆盖全A 99.8%+；6,498日长历史，唯一可出时序分位的因子"),
    D("SEC.CROWD.VAL_PCT", "拥挤·估值因子", "CROWD", "risk", "score_0_100", "high_risk",
      "直取 SEC.VAL.PE_XSEC_PCT（复用，跨层重复计权已登记）", "M1",
      pct_x_col="pe_xsec_pct", n_col="n_val", source="regime.sector_valuation_snapshot",
      remark="阶段1；首期用横截面分位"),
    D("SEC.CROWD.HIT_CNT", "拥挤因子命中数", "CROWD", "risk", "count", "high_risk",
      "Σ 1{p_i>80}，k=当日可用因子数", "M5",
      value_col="crowd_hit", remark="阶段2 k=3（阶段1 为 k=2 部分模式）；k<2 → NULL，缺失不得按0计",
      extra_kind="crowd"),
    D("SEC.CROWD.GATE", "拥挤档位", "CROWD", "risk", "enum", "high_risk",
      "k=3: 3/3→EXTREME,>=2→HIGH,1→WATCH; k=2: 2/2→HIGH(不出EXTREME),1→WATCH; 0→NORMAL", "M5",
      value_col="crowd_gate_code", remark="阶段2 k=3 开放 EXTREME；raw_value 存0-4编码，label/k/partial 在 extra",
      extra_kind="crowd"),
    # ── 阶段 2：依赖 regime.sector_constituent 的自下而上指标 ──────────────
    # 有效起点 2021-12-13（决策 A）；全部由 sector_stage2_agg 按月 × 按股票分批聚合。
    D("SEC.CROWD.TURNOVER_PCT", "行业换手率横截面分位", "CROWD", "risk", "score_0_100", "high_risk",
      "成分股 turnover_rate 按流通市值加权 → 当日横截面分位(M1)", "M6",
      value_col="turnover_w", pct_x_col="turnover_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + regime.sector_constituent",
      remark="⚠ 首期用**横截面分位**而非 M2 时序分位：成分快照起点 2021-12-13，样本不足以出稳定时序分位；满 250 日后按字典切换"),
    D("SEC.VAL.INTRA_DISPERSION", "行业内个股PE离散度", "VAL", "risk", "score_0_100", "high_risk",
      "成分股 pe_ttm_ratio 横截面 std → 当日横截面分位(M1)", "M6",
      value_col="pe_dispersion", pct_x_col="pe_disp_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + regime.sector_constituent",
      remark="阶段2；与 PE_XSEC_PCT 高冗余，仅作校验不进维度分"),
    D("SEC.BREADTH.UP_RATIO", "行业内上涨占比", "BREADTH", "state", "pct", "high_good",
      "M6：成分股 change_pct>0 占比", "M6",
      value_col="up_ratio", pct_x_col="up_ratio_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + regime.sector_constituent",
      remark="阶段2；有效起点 2021-12-13"),
    D("SEC.BREADTH.ABOVE_MA60", "行业内站上MA60占比", "BREADTH", "state", "pct", "high_good",
      "M6：成分股 close > MA60 占比（rolling 60，min_periods=60）", "M6",
      value_col="above_ma60", pct_x_col="above_ma60_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + regime.sector_constituent", remark="阶段2"),
    D("SEC.BREADTH.NEW_HIGH_LOW", "行业内新高−新低归一净占比", "BREADTH", "state", "pct", "high_good",
      "100×(n_high−n_low)/(n_high+n_low)，60日窗口；计数类必须归一（§5.7）", "M6",
      value_col="new_high_low", pct_x_col="nhl_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + regime.sector_constituent", remark="阶段2"),
    D("SEC.BREADTH.RET_DISPERSION", "行业内收益离散度", "BREADTH", "state", "pct", "high_risk",
      "成分股 20 日收益横截面 std → 横截面分位(M1)", "M6",
      value_col="ret_dispersion", pct_x_col="ret_disp_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + regime.sector_constituent",
      remark="阶段2；direction=high_risk（高=分化大），维度内 sign=-1 翻转"),
    D("SEC.FLOW.MARGIN_NET", "行业两融净买入", "FLOW", "state", "yi_yuan", "high_good",
      "M6：成分股 rz_net 求和 / 1e8", "M6",
      value_col="margin_net", source="daily_margin_balance + regime.sector_constituent",
      remark="阶段2；元→亿元"),
    D("SEC.FUND.ROE_MEDIAN", "行业ROE中位数", "FUND", "state", "pct", "high_good",
      "M6：按 announce_date ≤ t 取最新报告期（PIT），行业内取中位数", "M6",
      value_col="roe_median", pct_x_col="roe_xsec_pct", n_col="constituent_n",
      source="financial_indicator + regime.sector_constituent",
      remark="阶段2；月批次次月1日生效，无前视；fund_coverage<0.6 作废"),
    D("SEC.FUND.NP_YOY_MEDIAN", "行业净利同比中位数", "FUND", "state", "pct", "high_good",
      "PIT 最新报告期 net_profit_yoy 中位数", "M6",
      value_col="np_yoy_median", pct_x_col="npyoy_xsec_pct", n_col="constituent_n",
      source="financial_indicator + regime.sector_constituent", remark="阶段2"),
    D("SEC.FUND.REV_YOY_MEDIAN", "行业营收同比中位数", "FUND", "state", "pct", "high_good",
      "PIT 最新报告期 revenue_yoy 中位数", "M6",
      value_col="rev_yoy_median", pct_x_col="revyoy_xsec_pct", n_col="constituent_n",
      source="financial_indicator + regime.sector_constituent", remark="阶段2"),
    D("SEC.FUND.IMPROVEMENT_BREADTH", "行业盈利改善宽度", "FUND", "state", "pct", "high_good",
      "PIT 最新报告期 net_profit_yoy>0 的成分占比", "M6",
      value_col="improvement_breadth", pct_x_col="improve_xsec_pct", n_col="constituent_n",
      source="financial_indicator + regime.sector_constituent",
      remark="阶段2；归一比率天然平稳（§5.7 L2-a ③）"),
    D("SEC.FUND.COVERAGE", "财务覆盖度", "FUND", "state", "ratio", "high_good",
      "有 ROE 的成分数 / 成分数", "M6",
      value_col="fund_coverage", source="financial_indicator + regime.sector_constituent",
      remark="阶段2；**门禁**：<0.6 时本组读数作废并清库"),
    D("SEC.FUND.REVISION_BREADTH", "行业盈利修正宽度", "FUND", "state", "pct", "high_good",
      "M7：同财年 FY1 EPS 相邻快照差分（±0.5% 噪声），30快照滚动，按成分下沉到行业", "M7",
      value_col="revision_breadth", pct_x_col="revision_xsec_pct", n_col="revision_n",
      source="regime.analyst_forecast_snapshot + regime.sector_constituent",
      remark="阶段2；阶段0实测仅25个快照日，30快照窗口尚未满，按实际 n 出值并写 revision_n；覆盖<30% 或样本<5家 → NULL"),
    D("SEC.FUND.RATING_BULL_PCT", "行业评级乐观度", "FUND", "state", "pct", "high_good",
      "(rating_buy+rating_overweight)/五档合计，按成分汇总", "M0",
      value_col="rating_bull_pct", source="regime.analyst_forecast_snapshot + regime.sector_constituent",
      remark="阶段2；⚠ 判别力弱（§7.8 实测 99.3% 结构性集中），**仅作极端背离提示，不进维度分**"),
    D("SEC.FUND.FORECAST_COVERAGE", "预期覆盖度", "FUND", "state", "ratio", "high_good",
      "有预期数据的成分数 / PIT 有效成分数", "M6",
      value_col="forecast_coverage", source="regime.analyst_forecast_snapshot + regime.sector_constituent",
      remark="阶段2；<0.3 时 REVISION_BREADTH 不出值"),
]

ACTIVE_DEFS = [d for d in INDICATOR_DEFS if d["is_active"]]

# 维度定义（阶段 2）：成员 <4 走「单锚」（等权会让单成员 33% > §7.2 的 25% 上限）；
# 成员恰好 4 个的 FUNDAMENTAL / BREADTH 走「分块等权」，每成员 25% 合规。
# sign 是**唯一**方向翻转点：+1 顺向、-1 取 100−percentile（§7.1）。
DIMENSION_DEFS = {
    "TREND": dict(side="state", structure="single_anchor",
                  anchor="SEC.TREND.RS_XSEC_PCT", sign=1, members=[]),
    "FLOW": dict(side="state", structure="single_anchor",
                 anchor="SEC.FLOW.XSEC_PCT", sign=1, members=[]),
    "VALUATION": dict(side="risk", structure="single_anchor",
                      anchor="SEC.VAL.PE_XSEC_PCT", sign=1, members=[]),
    "CROWDING": dict(side="risk", structure="rule", anchor=None, sign=1, members=[]),
    "FUNDAMENTAL": dict(side="state", structure="equal", anchor=None, sign=1,
                        members=[("SEC.FUND.ROE_MEDIAN", 1),
                                 ("SEC.FUND.NP_YOY_MEDIAN", 1),
                                 ("SEC.FUND.IMPROVEMENT_BREADTH", 1),
                                 ("SEC.FUND.REVISION_BREADTH", 1)]),
    "BREADTH": dict(side="state", structure="equal", anchor=None, sign=1,
                    members=[("SEC.BREADTH.UP_RATIO", 1),
                             ("SEC.BREADTH.ABOVE_MA60", 1),
                             ("SEC.BREADTH.NEW_HIGH_LOW", 1),
                             ("SEC.BREADTH.RET_DISPERSION", -1)]),
}

RRG_LABELS = {1: "领先", 2: "走强", 3: "走弱", 4: "落后"}
CROWD_GATE_CODES = {"NORMAL": 0, "WATCH": 1, "HIGH": 2, "EXTREME": 3, "NA": 4}

# 阶段 2 聚合输出的列（sector_stage2_agg.build_stage2_agg）
_STAGE2_AGG_COLS = [
    "up_ratio", "above_ma60", "new_high_low", "ret_dispersion", "pe_dispersion",
    "turnover_w", "breadth_coverage", "constituent_n", "margin_net",
    "roe_median", "np_yoy_median", "rev_yoy_median", "improvement_breadth",
    "fund_coverage", "revision_breadth", "rating_bull_pct", "revision_n",
    "forecast_coverage",
]
# 阶段 2 数值 → 当日横截面分位（短历史期主用 M1，不用时序分位）
_STAGE2_XSEC = (
    ("turnover_w", "turnover_xsec_pct"), ("pe_dispersion", "pe_disp_xsec_pct"),
    ("up_ratio", "up_ratio_xsec_pct"), ("above_ma60", "above_ma60_xsec_pct"),
    ("new_high_low", "nhl_xsec_pct"), ("ret_dispersion", "ret_disp_xsec_pct"),
    ("roe_median", "roe_xsec_pct"), ("np_yoy_median", "npyoy_xsec_pct"),
    ("rev_yoy_median", "revyoy_xsec_pct"),
    ("improvement_breadth", "improve_xsec_pct"),
    ("revision_breadth", "revision_xsec_pct"),
)


# ── 纯函数（可单测）─────────────────────────────────────────────────────────
def _xsec_percentile(s: pd.Series) -> pd.Series:
    """M1 横截面分位：中位秩 100*(n_less + 0.5*n_equal)/n（§7.1 平均秩，不用上界）。

    rank(method='average') 给并列组平均秩 = n_less + (n_equal+1)/2，
    故 (rank-0.5)/n 恰好等于上式。n_valid < XSEC_MIN_N 时整列置 NaN。
    """
    v = pd.to_numeric(s, errors="coerce")
    n = int(v.notna().sum())
    if n < XSEC_MIN_N:
        return pd.Series(np.nan, index=s.index, dtype=float)
    r = v.rank(method="average", ascending=True)
    return (r - 0.5) / n * 100.0


def _reverse_percentile(s: pd.Series) -> pd.Series:
    """rank 类指标反向翻转：rank 越小（越强）→ 分位越高。方向只在此定义一次。"""
    p = _xsec_percentile(s)
    return 100.0 - p


def _params_hash(d: dict) -> str:
    """口径指纹：公式/单位/极性/模型/参数/版本任一变化都会换指纹（§7.2 PIT 铁律）。"""
    payload = "|".join([d["code"], d["unit"], d["direction"], d["formula"],
                        d["model_key"], CALIB_VERSION])
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def _clean_value(v):
    """NaN/Inf → None。PostgreSQL numeric NaN 会污染整批聚合（§7.6 事故，必防）。"""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 6)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return str(v)[:10]
    return v


def _clean_rows(rows: list) -> list:
    return [{k: _clean_value(v) for k, v in r.items()} for r in rows]


def crowd_gate(hits: dict) -> dict:
    """M5 条件命中 → 档位。hits: {因子名: 分位值 or None}。

    单因子缺失时既不进分子也不进分母（k 减 1），**不得把缺失按 0 计**。
    k<2 判 NA；k=2 部分模式不开 EXTREME。
    """
    valid = {k: float(v) for k, v in hits.items()
             if v is not None and not (isinstance(v, float) and math.isnan(v))}
    k = len(valid)
    if k < 2:
        return dict(gate="NA", hit=0, k=k, partial=True, members=[])
    hit = sum(1 for v in valid.values() if v > CROWD_PCT_HIGH)
    partial = k < 3
    if hit >= 3 and not partial:
        gate = "EXTREME"
    elif hit >= 2:
        gate = "HIGH"
    elif hit == 1:
        gate = "WATCH"
    else:
        gate = "NORMAL"
    return dict(gate=gate, hit=hit, k=k, partial=partial,
                members=sorted(f for f, v in valid.items() if v > CROWD_PCT_HIGH))


def quadrant_of(state: float, risk: float, state_med: float, risk_med: float) -> str:
    """2×2 状态矩阵。象限名只描述当前状态，不含超配/低配含义（§3.6）。"""
    if state is None or risk is None or (isinstance(state, float) and math.isnan(state)) \
            or (isinstance(risk, float) and math.isnan(risk)):
        return "NA"
    hi_s, hi_r = state >= state_med, risk >= risk_med
    if hi_s and not hi_r:
        return "强势·未拥挤"
    if hi_s and hi_r:
        return "强势·偏贵拥挤"
    if not hi_s and not hi_r:
        return "冷门·未拥挤"
    return "弱势·偏贵拥挤"


def confidence_of(n_sample: int | None, coverage: float, pit_capable: bool = True) -> str:
    """置信度：短样本 / 低覆盖 / 非 PIT 段一律降级。"""
    if coverage < 0.6 or not pit_capable:
        return "low"
    if n_sample is not None and n_sample < SHORT_SAMPLE:
        return "low"
    return "normal"


# ── 数据加载（R1/R7：全部带时间下界）────────────────────────────────────────
def _load_sector_daily(conn, start: dt.date, end: dt.date) -> pd.DataFrame:
    """按块加载逐行业横截面（含 WARMUP_DAYS 预热，供 shift/rolling 使用）。"""
    w = start - dt.timedelta(days=WARMUP_DAYS)
    sql = """
        SELECT trade_date, sw_code, sw_name, close, ret_20d, ret_60d, rs_20d,
               rs_rank, mom_rank, turnover, fund_flow_net, fund_flow_net_5d,
               flow_rank, mapped_ths_count
        FROM regime.sector_daily
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY trade_date, sw_code
    """
    return read_sql(conn, sql, params=[w, end])


def _load_valuation(conn, start: dt.date, end: dt.date) -> pd.DataFrame:
    w = start - dt.timedelta(days=WARMUP_DAYS)
    sql = """
        SELECT snapshot_date AS trade_date, sw_code, pe_ttm, pb, div_yield
        FROM regime.sector_valuation_snapshot
        WHERE snapshot_date BETWEEN %s AND %s
    """
    return read_sql(conn, sql, params=[w, end])


def _precompute_amt_share(conn, hist_start: dt.date, end: dt.date) -> pd.DataFrame:
    """轻量 3 列全史序列 → 成交额占比 + 10 年 as-of 时序分位。

    单独加载的原因：主表 15 万行 × 15 列若整段进进程会推高峰值（R1/R3）；
    这里只取 (trade_date, sw_code, turnover)，约 14 万行 × 3 列 ≈ 5MB。
    """
    from market_state_daily import _expanding_percentile  # 口径唯一真源（§7.1 as-of）

    sql = """
        SELECT trade_date, sw_code, turnover
        FROM regime.sector_daily
        WHERE trade_date BETWEEN %s AND %s AND turnover IS NOT NULL
        ORDER BY sw_code, trade_date
    """
    df = read_sql(conn, sql, params=[hist_start, end])
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "sw_code", "amt_share",
                                     "amt_share_pct", "amt_share_n"])
    df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
    # 同源占比：分母取「当日有效行业合计」，rank_n<31 的早期段不与后期比
    df["amt_share"] = df["turnover"] / df.groupby("trade_date")["turnover"].transform("sum")
    g = df.groupby("sw_code")["amt_share"]
    df["amt_share_pct"] = g.transform(
        lambda s: _expanding_percentile(s, window=ROLL_WINDOW, min_periods=MIN_PERIODS))
    df["amt_share_n"] = g.transform(
        lambda s: s.rolling(ROLL_WINDOW, min_periods=1).count())
    return df[["trade_date", "sw_code", "amt_share", "amt_share_pct", "amt_share_n"]]


# ── 计算主体 ───────────────────────────────────────────────────────────────
def _compute_frame(df: pd.DataFrame, val: pd.DataFrame, amt: pd.DataFrame,
                   agg: pd.DataFrame | None = None) -> pd.DataFrame:
    """把原始横截面加工成「一行 = 一个行业日」的宽表，列名对应字典的 *_col。

    agg = sector_stage2_agg.build_stage2_agg() 的自下而上聚合（阶段 2），
    只覆盖 2021-12-13 起；为空时阶段 2 列全 NaN，阶段 1 行为保持不变。
    """
    if df.empty:
        return pd.DataFrame()
    for c in ("ret_20d", "ret_60d", "rs_20d", "rs_rank", "mom_rank", "turnover",
              "fund_flow_net", "fund_flow_net_5d", "flow_rank"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # 每股时序类先按 (sw_code, trade_date) 排序；横截面类用 groupby 不依赖顺序
    df = df.sort_values(["sw_code", "trade_date"]).reset_index(drop=True)
    g = df.groupby("trade_date", sort=False)

    df["n_valid"] = g["ret_20d"].transform("count")
    df["rs_60d"] = df["ret_60d"] - g["ret_60d"].transform("mean")
    df["rs_xsec_pct"] = g["rs_20d"].transform(_xsec_percentile)
    df["mom_rank_pct"] = g["mom_rank"].transform(_reverse_percentile)
    df["rs_mom_20d"] = df.groupby("sw_code")["rs_xsec_pct"].transform(lambda s: s.shift(20))
    df["rs_mom_xsec_pct"] = df.groupby("trade_date")["rs_mom_20d"].transform(_xsec_percentile)

    # 资金：rolling 必须在「行业内按日期有序」的前提下做
    df["net_20d"] = df.groupby("sw_code")["fund_flow_net"].transform(
        lambda s: s.rolling(20, min_periods=20).sum())
    df["net_norm"] = df["fund_flow_net"] / df["turnover"].replace(0, np.nan)
    df["n_flow"] = df.groupby("trade_date")["net_norm"].transform("count")
    df["flow_xsec_pct"] = df.groupby("trade_date")["net_norm"].transform(_xsec_percentile)
    df["flow_rank_pct"] = df.groupby("trade_date")["flow_rank"].transform(_reverse_percentile)
    df["price_flow_gap"] = df["rs_rank"] - df["flow_rank"]
    df["n_val"] = df.groupby("trade_date")["ret_20d"].transform("count")  # 占位，估值 join 后覆盖

    # 估值快照按 (trade_date, sw_code) 左连接；不回填历史（源无历史序列）
    if not val.empty:
        val = val.copy()
        for c in ("pe_ttm", "pb", "div_yield"):
            val[c] = pd.to_numeric(val[c], errors="coerce")
        df = df.merge(val, on=["trade_date", "sw_code"], how="left")
    else:
        for c in ("pe_ttm", "pb", "div_yield"):
            df[c] = np.nan
    gv = df.groupby("trade_date", sort=False)
    df["n_val"] = gv["pe_ttm"].transform("count")
    df["pe_xsec_pct"] = gv["pe_ttm"].transform(_xsec_percentile)
    df["pb_xsec_pct"] = gv["pb"].transform(_xsec_percentile)
    df["rel_to_market"] = df["pe_ttm"] / gv["pe_ttm"].transform("median").replace(0, np.nan)
    # M2 时序分位：阶段1 实测 27 个快照日 << min_periods=250 → 一律 NULL
    df["pe_ts_pct"] = np.nan
    df["pe_n_sample"] = df.groupby("sw_code")["pe_ttm"].transform(
        lambda s: s.notna().expanding().count())

    # 成交额占比时序分位（预计算结果按日期 join，不重复算）
    if not amt.empty:
        df = df.merge(amt, on=["trade_date", "sw_code"], how="left")
    else:
        for c in ("amt_share", "amt_share_pct", "amt_share_n"):
            df[c] = np.nan

    # 阶段 2 聚合按 (trade_date, sw_code) 左连接；agg 只覆盖 PIT_START 起（决策 A）
    if agg is not None and not agg.empty:
        a = agg.copy()
        a["trade_date"] = pd.to_datetime(a["trade_date"], errors="coerce").dt.date
        cols = [c for c in _STAGE2_AGG_COLS if c in a.columns]
        df = df.merge(a[["trade_date", "sw_code"] + cols],
                      on=["trade_date", "sw_code"], how="left")
        del a
    for c in _STAGE2_AGG_COLS:
        if c not in df.columns:
            df[c] = np.nan
    for src, dst in _STAGE2_XSEC:
        df[dst] = (df.groupby("trade_date")[src].transform(_xsec_percentile)
                   if src in df.columns else np.nan)

    # RRG 象限（分类编码 + label）
    def _rrg(row):
        p, m = row["rs_xsec_pct"], row["rs_mom_20d"]
        if pd.isna(p) or pd.isna(m):
            return np.nan
        if p >= 50 and m >= 0:
            return 1
        if p < 50 and m >= 0:
            return 2
        if p >= 50 and m < 0:
            return 3
        return 4
    df["rrg_code"] = df.apply(_rrg, axis=1)

    # 拥挤度三因子（阶段2：AMT_SHARE_PCT + TURNOVER_PCT + VAL_PCT → k=3）
    crowd = [crowd_gate({"AMT_SHARE_PCT": a, "TURNOVER_PCT": t, "VAL_PCT": v})
             for a, t, v in zip(df["amt_share_pct"],
                                df["turnover_xsec_pct"], df["pe_xsec_pct"])]
    df["crowd_hit"] = [None if c["gate"] == "NA" else c["hit"] for c in crowd]
    df["crowd_gate_code"] = [CROWD_GATE_CODES[c["gate"]] for c in crowd]
    df["_crowd"] = crowd

    # applicable：近 20 交易日有效读数 ≥15 日（直接从输入算，不依赖输出表）
    df["_valid"] = df["rs_20d"].notna().astype(int)
    df["valid_days_20"] = df.groupby("sw_code")["_valid"].transform(
        lambda s: s.rolling(20, min_periods=1).sum())

    # 维度分：单锚 / 分块等权 / 规则判定（§7.2 三种标准结构）
    def _anchor_pct(code):
        d = next(x for x in ACTIVE_DEFS if x["code"] == code)
        col = d["pct_x_col"]
        return df[col] if col and col in df.columns else pd.Series(np.nan, index=df.index)

    for name, cfg in DIMENSION_DEFS.items():
        if cfg["structure"] == "single_anchor":
            df["dim_" + name] = _anchor_pct(cfg["anchor"])
        elif cfg["structure"] == "equal":
            cols = []
            for code, sign in cfg["members"]:
                d = next(x for x in ACTIVE_DEFS if x["code"] == code)
                col = d["pct_x_col"]
                p = (df[col] if col and col in df.columns
                     else pd.Series(np.nan, index=df.index))
                key = f"_eq_{code}"
                df[key] = 100.0 - p if sign < 0 else p
                cols.append(key)
            v = df[cols]
            df["dim_" + name] = v.mean(axis=1, skipna=True)
            # 可用成员 <2 时不出维度分（扁平平均会退化成单成员定档）
            df.loc[v.notna().sum(axis=1) < 2, "dim_" + name] = np.nan
        else:
            df["dim_" + name] = np.nan

    # 业务读数：状态侧 = 当期已启用的所有状态维度（阶段 2 = 4 组）
    state_cols = [f"dim_{n}" for n, c in DIMENSION_DEFS.items()
                  if c["side"] == "state"
                  and c["structure"] in ("single_anchor", "equal")]
    df["state_side"] = df[state_cols].mean(axis=1, skipna=True)
    df.loc[df[state_cols].notna().sum(axis=1) < 2, "state_side"] = np.nan
    df["risk_side"] = df["dim_VALUATION"]
    df["state_members"] = [sorted(state_cols)] * len(df)
    df["state_n"] = df[state_cols].notna().sum(axis=1)

    # coverage = 可用维度数 / 当期已启用维度数（阶段 2 分母 = 6）
    # CROWDING 走规则判定，用 gate != NA 代替其 score 是否有值。
    ok = sum(df[f"dim_{n}"].notna().astype(int)
             for n in ENABLED_DIMENSIONS if n != "CROWDING")
    ok = ok + (df["crowd_gate_code"] != CROWD_GATE_CODES["NA"]).astype(int)
    df["coverage"] = ok / float(len(ENABLED_DIMENSIONS))
    df["rank_n"] = df.groupby("trade_date")["state_side"].transform("count")
    df["rank"] = df.groupby("trade_date")["state_side"].transform(
        lambda s: s.rank(ascending=False, method="min"))
    df["state_med"] = df.groupby("trade_date")["state_side"].transform("median")
    df["risk_med"] = df.groupby("trade_date")["risk_side"].transform("median")
    df["quadrant"] = [quadrant_of(s, r, sm, rm) for s, r, sm, rm in
                      zip(df["state_side"], df["risk_side"], df["state_med"], df["risk_med"])]
    return df


# ── 输出行构造 ─────────────────────────────────────────────────────────────
def _get(row, col):
    if not col or col not in row:
        return None
    v = row[col]
    return None if v is None or (isinstance(v, (float, np.floating)) and
                                (math.isnan(float(v)) or math.isinf(float(v)))) else v


def _indicator_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    rows = []
    now = dt.datetime.now().astimezone()
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        for meta in ACTIVE_DEFS:
            raw = _get(rec, meta["value_col"])
            px = _get(rec, meta["pct_x_col"])
            pt = _get(rec, meta["pct_t_col"])
            ns = _get(rec, meta["n_col"])
            if raw is None and px is None and pt is None and not (ns and ns > 0):
                continue        # 全空且无样本信息 → 不写行（全量回填省 ~70% 行数）
            extra = {}
            if meta["extra_kind"] == "rrg" and raw is not None:
                extra["label"] = RRG_LABELS.get(int(raw))
            if meta["extra_kind"] == "crowd":
                c = rec.get("_crowd") or {}
                extra.update(k=c.get("k"), partial=c.get("partial"),
                             members=c.get("members", []))
                if meta["code"] == "SEC.CROWD.GATE" and raw is not None:
                    extra["label"] = {v: k for k, v in CROWD_GATE_CODES.items()}.get(int(raw))
            extra["rank_n"] = _clean_value(rec.get("rank_n"))
            # 依赖成分主数据的指标，在 PIT_START 之前无有效成分 → 标注非 PIT。
            extra["pit_capable"] = (d >= PIT_START) if "sector_constituent" in meta["source"] else True
            rows.append(dict(
                market=MARKET, classification=CLASSIFICATION,
                sector_code=rec["sw_code"], trade_date=d,
                indicator_code=meta["code"], raw_value=raw,
                percentile_xsec=px, percentile_ts=pt, n_sample=ns,
                coverage=None, quality=("short_sample" if ns is not None and
                                        meta["model_key"] == "M2" and ns < SHORT_SAMPLE
                                        else "ok"),
                model_version=_params_hash(meta), extra=extra,
                updated_at=now))
    return rows


def _dim_n_sample(rec: dict, name: str):
    """各维度按**自身**样本长度判置信：TREND/CROWDING 用长历史成交额序列，
    FLOW/VALUATION 用快照类短样本 —— 不许让估值快照的 27 日把趋势维度一起判 low。
    """
    if name in ("TREND", "CROWDING"):
        return _get(rec, "amt_share_n")
    if name == "FLOW":
        return _get(rec, "n_flow")
    if name == "VALUATION":
        return _get(rec, "pe_n_sample")
    if name == "FUNDAMENTAL":
        return _get(rec, "revision_n")       # 阶段2 实测 25 个快照日 << 750 → low
    return None


def _score_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    rows, now = [], dt.datetime.now().astimezone()
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        for name, cfg in DIMENSION_DEFS.items():
            score = (_get(rec, "dim_" + name)
                     if cfg["structure"] in ("single_anchor", "equal") else None)
            c = rec.get("_crowd") or {}
            cov = 1.0 if score is not None else 0.0
            if name == "CROWDING":
                cov = 1.0 if c.get("gate") != "NA" else 0.0
            members = ([cfg["anchor"]] if cfg["anchor"] else
                       [code for code, _ in cfg.get("members", [])])
            extra = dict(structure=cfg["structure"], anchor=cfg["anchor"],
                         members=members)
            if cfg["structure"] == "equal":
                extra["weight"] = round(1.0 / max(len(members), 1), 4)
                extra["calib_break"] = "s1.1→s2.1 单锚/未启用 → 分块等权"
            if name == "CROWDING":
                extra.update(crowd_gate=c.get("gate"), k=c.get("k"),
                             partial=c.get("partial"))
            rows.append(dict(
                market=MARKET, classification=CLASSIFICATION,
                sector_code=rec["sw_code"], trade_date=d, dimension_code=name,
                side=cfg["side"], score=score, coverage=cov, dispersion=0.0,
                confidence=confidence_of(_dim_n_sample(rec, name), cov),
                members_used=members,
                params_hash=_params_hash(dict(code="SEC.DIM." + name, unit="score_0_100",
                                              direction="high_good", formula=cfg["structure"],
                                              model_key=cfg["structure"])),
                extra=extra, updated_at=now))
    return rows


def _biz_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    rows, now = [], dt.datetime.now().astimezone()
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        cov = float(_get(rec, "coverage") or 0.0)
        c = rec.get("_crowd") or {}
        flags = []
        if cov < 0.6:
            flags.append("DATA_COVERAGE")
        # SHORT_SAMPLE 只在「参与维度分的锚指标」短样本时触发；阶段1 锚均为横截面
        # 分位（不依赖时序样本），故不触发 —— 否则 applicable 会恒为 false。
        if c.get("gate") in ("HIGH", "EXTREME"):
            flags.append("CROWDING")
        state_dims_n = sum(1 for cfg in DIMENSION_DEFS.values()
                           if cfg["side"] == "state"
                           and cfg["structure"] in ("single_anchor", "equal"))
        state_n = int(_get(rec, "state_n") or 0)
        if state_n < state_dims_n:
            flags.append("PARTIAL_STATE")   # 阶段 2 之前 / 成分数据缺失时状态侧不齐
        applicable = (cov >= 0.6 and int(_get(rec, "valid_days_20") or 0) >= 15
                      and "DATA_COVERAGE" not in flags)
        partial = "PARTIAL_STATE" in flags
        extra = dict(state_members=rec["state_members"], state_n=state_n,
                     partial=partial, enabled_dimensions=list(ENABLED_DIMENSIONS),
                     calib_version=CALIB_VERSION, pit_capable=(d >= PIT_START),
                     crowd_k=c.get("k"), crowd_partial=c.get("partial"),
                     rank_n=_clean_value(rec.get("rank_n")))
        rows.append(dict(
            market=MARKET, classification=CLASSIFICATION,
            sector_code=rec["sw_code"], trade_date=d,
            state_side=_get(rec, "state_side"), risk_side=_get(rec, "risk_side"),
            quadrant=rec.get("quadrant") or "NA",
            rank_no=_get(rec, "rank") if applicable else None,
            rank_n=_clean_value(rec.get("rank_n")),
            coverage=cov, gate_flags=flags,
            applicable=bool(applicable),
            confidence=confidence_of(_get(rec, "pe_n_sample"), cov),
            params_hash=_params_hash(dict(code="SEC.BIZ.DAILY", unit="score_0_100",
                                          direction="neutral", formula="state/risk matrix",
                                          model_key="L3")),
            extra=extra, updated_at=now))
    return rows


# ── 写库 / 字典同步 ────────────────────────────────────────────────────────
PK_COLS = ["market", "classification", "sector_code", "trade_date"]


def _write_batched(conn, table: str, rows: list, conflict_cols: list) -> int:
    """R6：单事务 ≤2000 行，逐批 commit。"""
    rows = _clean_rows(rows)
    for k in range(0, len(rows), WRITE_BATCH):
        bulk_upsert(conn, table, rows[k:k + WRITE_BATCH], conflict_cols=conflict_cols)
        conn.commit()
    return len(rows)


def sync_dict(conn) -> int:
    """指标字典幂等 upsert（口径唯一真源在 INDICATOR_DEFS）。"""
    rows = [dict(indicator_code=d["code"], indicator_name=d["indicator_name"],
                 domain=d["domain"], side=d["side"], market_scope=MARKET,
                 classification=CLASSIFICATION, unit=d["unit"], direction=d["direction"],
                 freq=d["freq"], source=d["source"], formula=d["formula"],
                 model_key=d["model_key"],
                 params={"version": CALIB_VERSION},
                 params_hash=_params_hash(d),
                 min_periods=MIN_PERIODS if d["model_key"] == "M2" else 0,
                 pit_capable=True, is_active=d["is_active"], remark=d["remark"],
                 updated_at=dt.datetime.now().astimezone()) for d in INDICATOR_DEFS]
    bulk_upsert(conn, "regime.sector_indicator_dict", _clean_rows(rows),
                conflict_cols=["indicator_code"])
    conn.commit()
    return len(rows)


def _default_window(conn) -> tuple[dt.date, dt.date]:
    """增量窗口：取输出表最新日 -3 天（迟到/修订自愈）；无输出则最近 45 天。"""
    end = dt.date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM regime.sector_biz_daily WHERE market=%s",
                    (MARKET,))
        last = cur.fetchone()[0]
    start = (last - dt.timedelta(days=REWIND_DAYS)) if last else \
            (end - dt.timedelta(days=INCREMENTAL_DAYS))
    return start, end


def run(codes=None, ctx=None, start: dt.date = None, end: dt.date = None,
        backfill: bool = False, write: bool = True, init_schema: bool = False) -> dict:
    """计算入口。参数签名兼容 run_module，但**本模块不得挂进 market_scheduler**（R5）。"""
    t0 = time.monotonic()
    rid = None
    try:
        from monitor_collector import record_task_start
        rid = record_task_start("行业配置计算", market=MARKET)
    except Exception:
        log.warning("任务埋点起点失败（不影响计算）", exc_info=True)
    status = "ok"
    try:
        if init_schema:
            from regime_schema import ensure_sector_schema
            with get_conn() as conn:
                ensure_sector_schema(conn)
                n = sync_dict(conn)
            return {"init_schema": True, "dict_rows": n}

        with get_conn() as conn:
            # 自举：新服务器上 sql/schema.sql 的增量同步是部署主路径，这里再做一层
            # 幂等兜底（只执行本层 7 条 DDL + 注释），避免首次运行因表不存在直接失败。
            from regime_schema import ensure_sector_schema
            ensure_sector_schema(conn)
            if backfill:
                start, end = BACKFILL_START, (end or dt.date.today())
            elif start is None:
                start, end = _default_window(conn)
            else:
                end = end or dt.date.today()

            # 输入新鲜度闸门：上游 market_state_daily 18:45 未跑完时，
            # 只算到 sector_daily 已有数据的最后一天，不制造空洞读数。
            with conn.cursor() as cur:
                cur.execute("SELECT max(trade_date) FROM regime.sector_daily")
                upstream = cur.fetchone()[0]
            if upstream is None:
                raise RuntimeError("regime.sector_daily 为空 —— 上游市场状态计算未产出，拒绝执行")
            end = min(end, upstream)
            if start > end:
                return {"skipped": "start > end（上游数据未就绪）"}

            n_dict = sync_dict(conn)
            hist_start = min(start, end - dt.timedelta(days=365 * 11))
            amt = _precompute_amt_share(conn, hist_start, end)

            stats = dict(start=str(start), end=str(end), dict_rows=n_dict,
                         iv=0, score=0, biz=0, chunks=0)
            from sector_stage2_agg import build_stage2_agg

            for cs, ce in _chunks(start, end):
                raw = _load_sector_daily(conn, cs, ce)
                if raw.empty:
                    continue
                val = _load_valuation(conn, cs, ce)
                # 阶段 2 自下而上聚合（内部按月 × 按股票分批，R1/R3）；
                # cs < PIT_START 的早期年块返回空表 → 阶段 2 列全 NaN（决策 A）。
                agg = build_stage2_agg(conn, cs, ce)
                wide = _compute_frame(raw, val, amt, agg)
                del raw, val, agg
                n_iv = n_sc = n_bz = n_sub = 0
                # R3：按月子片生成即写，禁止把整年块的输出行一次性堆在内存
                for sub in _iter_month_frames(wide, cs, ce):
                    iv = _indicator_rows(sub, cs, ce)
                    sc = _score_rows(sub, cs, ce)
                    bz = _biz_rows(sub, cs, ce)
                    if write:
                        n_iv += _write_batched(
                            conn, "regime.sector_indicator_value", iv,
                            PK_COLS + ["indicator_code"])
                        n_sc += _write_batched(
                            conn, "regime.sector_score_daily", sc,
                            PK_COLS + ["dimension_code"])
                        n_bz += _write_batched(
                            conn, "regime.sector_biz_daily", bz, PK_COLS)
                    else:
                        n_iv += len(iv)
                        n_sc += len(sc)
                        n_bz += len(bz)
                    n_sub += 1
                    del iv, sc, bz, sub
                stats["iv"] += n_iv
                stats["score"] += n_sc
                stats["biz"] += n_bz
                del wide
                gc.collect()
                stats["chunks"] += 1
                log.info("块 %s~%s（%d 月片）：iv=%d score=%d biz=%d", cs, ce,
                         n_sub, n_iv, n_sc, n_bz)
            stats["elapsed_s"] = round(time.monotonic() - t0, 2)
            stats["write"] = write
            log.info("行业层计算完成 %s", stats)
            return stats
    except BaseException:
        status = "error"
        raise
    finally:
        if rid is not None:
            try:
                from monitor_collector import record_task_end
                record_task_end(rid, status, duration_s=round(time.monotonic() - t0, 2))
            except Exception:
                log.warning("任务埋点终点失败（不影响计算）", exc_info=True)


def _chunks(start: dt.date, end: dt.date, years: int = CHUNK_YEARS):
    out, s = [], start
    while s <= end:
        e = min(dt.date(s.year + years - 1, 12, 31), end)
        out.append((s, e))
        s = e + dt.timedelta(days=1)
    return out


def _iter_month_frames(wide: pd.DataFrame, start: dt.date, end: dt.date):
    """把年块的宽表再按**月**切片后逐片构造输出行。

    ⚠ 这是 R3 的关键：一个 4 年块 ≈ 31,000 个「行业日」× ~25 个指标
    ≈ 77 万行 dict，一次性堆在内存里实测峰值 **892MB**（会被 MemoryMax=512M
    的 cgroup OOM kill）。按月切片后每片约 650 行业日 × 25 ≈ 1.6 万行，
    生成即写、写完 del + gc.collect()，峰值降到 ~150MB。

    切片只影响**输出行构造**，不影响计算：RS shift / rolling / 时序分位
    已在 _compute_frame / _precompute_amt_share 里用整块 warmup 算完。
    """
    if wide.empty:
        return
    d = pd.to_datetime(wide["trade_date"])
    mask = (d.dt.date >= start) & (d.dt.date <= end)
    w = wide.loc[mask]
    if w.empty:
        return
    period = d.loc[mask].dt.to_period("M")
    for _, idx in w.groupby(period).groups.items():
        yield w.loc[idx]


def _main() -> int:
    ap = argparse.ArgumentParser(description="第④层行业配置计算（阶段1，CN 申万一级）")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--backfill", action="store_true", help="全量回填 1999-12-30 起")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--init-schema", action="store_true", help="只建表 + 同步字典")
    a = ap.parse_args()
    out = run(start=a.start, end=a.end, backfill=a.backfill,
              write=not a.dry_run, init_schema=a.init_schema)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
