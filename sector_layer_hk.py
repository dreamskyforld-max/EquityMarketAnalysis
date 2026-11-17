#!/usr/bin/env python3
"""第④层 阶段 3 · HK 行业配置指标层（L1/L2/L3）。

对应 doc/industry_allocation_feasibility.md §3 / §4.2 阶段 3。
⚠ **与 CN 侧独立口径**：不 import / 不修改 `sector_layer.py` 的计算主流程，
   只复用其纯函数（`_xsec_percentile` / `crowd_gate` / `quadrant_of` / 写库护栏）。
   CN 的 `CALIB_VERSION=s2.1` / `params_hash` 一律不动，HK 用 `hk1.0`。

【本期范围（2026-11-14 用户决策）】
   A 相对强弱与趋势 / B 估值 / C 组仅 `SOUTHBOUND_NET` / D 拥挤(k=2 部分模式) / F 行业宽度。
   · E 组不做：`financial_indicator` 港股 38,619 行的 `announce_date` **全 NULL**，
     M6 财务 PIT 无法套用；一致预期仅 A 股。
   · `TURNOVER_PCT` 不做：`hk_daily_quote.turnover_rate` 仅 75 个交易日零星有值。
   · **L3 不排名**：`rank_no` 恒 NULL、`extra.no_rank=true`。C 组只有 136 日南向、
     E 组不可得，若照搬 CN 六维口径会让 coverage 恒 <0.6，进而硬凑 applicable 出排名；
     本期只出「状态读数 + 2×2 状态矩阵 + 闸门」，排序待阶段 4 前瞻验证后再议。

【纪律】
   · 主键含 market（'HK'），横截面按 (market, trade_date) 分组 —— 本模块只加载
     HK 数据，物理上
     不可能混入 CN；测试另有静态断言钉住。
   · 全部读数 `pit_capable=false` / `confidence=low`（成分是 stock_sector 当前快照）。
   · 113 个恒生三级行业 vs CN 31 个申万一级，字典 remark 与 extra 双处标注粒度差异。
   · 与 CN 共用 `sector-layer.service`（工作日 19:10），CN 算完再串行算 HK；
     不挂 market_scheduler（R4/R5）。

CLI:
    python3 sector_layer_hk.py --dry-run
    python3 sector_layer_hk.py --start 2026-10-01 --end 2026-11-13 --skip-index
    python3 sector_layer_hk.py --backfill --dry-run
    python3 sector_layer_hk.py --init-schema
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import json
import sys
import time

import numpy as np
import pandas as pd

from db import bulk_upsert, get_conn, read_sql
from log_utils import setup_logger
from sector_layer import (BIZ_DEFS as _CN_BIZ_DEFS,
                          BIZ_PK_COLS, CROWD_GATE_CODES, CROWD_PCT_HIGH, PK_COLS, RRG_LABELS,
                          _atom_spec, _clean_rows, _dict_hash, _get, _hash_spec,
                          _reverse_percentile, _write_batched,
                          _xsec_percentile, confidence_of, crowd_gate, quadrant_of)

log = setup_logger("sector_layer_hk")

MARKET = "HK"
CALIB_VERSION = "hk1.0"             # 独立于 CN 的 s2.1；换版本会换掉 HK 全部 params_hash
EQ_START = dt.date(2000, 1, 3)
CAP_START = dt.date(2002, 1, 1)     # 总市值加权版起点（口径断点：BREAK 闸门）
BACKFILL_START = EQ_START
CHUNK_YEARS = 2                     # HK 113 行业，块比 CN(4年) 小，控 R3 峰值
WARMUP_DAYS = 400
ROLL_WINDOW = 2520
MIN_PERIODS = 250
SHORT_SAMPLE = 750
INCREMENTAL_DAYS = 45
REWIND_DAYS = 3
SLICE_DAYS = 10                     # 输出行按 10 天切片（CN 按月；HK 行业数 3.6 倍）
WRITE_BATCH = 2000

# 本期启用的维度（FLOW / FUNDAMENTAL 无可靠数据源，写 NULL 行并标 reason）
ENABLED_DIMENSIONS = ("TREND", "VALUATION", "CROWDING", "BREADTH")

# L3 业务读数阈值：抽成常量以便纳入口径指纹（数值与既有实现完全一致）。
# ⚠ 改这些值会换掉 HK 的 L2/L3 params_hash，必须全量重算。
COVERAGE_MIN = 0.6
VALID_DAYS_20_MIN = 15
VALID_DAYS_WINDOW = 20

# 拥挤度规则判定所用因子（HK TURNOVER_PCT 登记为 inactive，但定义仍在
# HK_INDICATOR_DEFS 中，纳入指纹可让「启用该因子」这一口径变更被感知）。
_HK_CROWD_FACTORS = ("SEC.CROWD.AMT_SHARE_PCT", "SEC.CROWD.TURNOVER_PCT",
                     "SEC.CROWD.VAL_PCT")


# ── remark 三段式（与 regime.indicator_dict 的事实标准对齐）──────────────────
# 单行 `定义：…　·　判读：…　·　数据：…`，可扩展 ⚠/口径：；无换行、无【】。
# ⚠ remark 不进 _hk_params_hash 的 payload —— 只改 remark 不换指纹、不触发历史重算。
_SEP = "　·　"
_HK_DATA = ("数据：源 = HK 自算行业日聚合（表 27）；成分取 stock_sector 当前快照，"
            "非 PIT、含幸存者偏差，全期 pit_capable=false；"
            "113 个恒生三级行业与 CN 31 个申万一级不同构，不得混排；"
            "等权 2000-01-03 起、总市值加权 2002-01-01 起")


def D(code, name, domain, side, unit, direction, formula, model_key,
       value_col=None, pct_x_col=None, pct_t_col=None, n_col=None,
       freq="day", remark="", active=True, extra_kind=None):
    # 每条字典行都必须自带 PIT / A-H 不可比声明（§2.4 数据可用性铁律）。
    # 守卫只作兜底：漏拼 _HK_DATA 时在**末尾**补齐，不破坏「定义：」开头的三段式。
    if "幸存者偏差" not in remark:
        remark = remark + _SEP + _HK_DATA
    return dict(code=code, indicator_name=name, domain=domain, side=side,
                unit=unit, direction=direction, formula=formula, model_key=model_key,
                value_col=value_col, pct_x_col=pct_x_col, pct_t_col=pct_t_col,
                n_col=n_col, source="industry.hk_index_daily", freq=freq,
                remark=remark, is_active=active, extra_kind=extra_kind)

HK_INDICATOR_DEFS = [
    # ── A 相对强弱与趋势（状态侧）──────────────────────────────────────────
    D("SEC.TREND.RET_20D", "行业20日收益(等权指数)", "TREND", "state", "pct", "neutral",
      "idx_eq_t/idx_eq_{t-20}-1（等权行业指数）", "M8", value_col="ret_20d",
      remark="定义：等权行业指数近 20 交易日收益 idx_eq_t/idx_eq_{t-20}−1（%）" + _SEP +
             "判读：>0 该行业过去一个月上涨，**只是历史动量不是预测**；与 RS_20D 联读区分「普涨」与「真相对强」" + _SEP +
             "⚠ 两版指数方向翻转率约 24%（5,331 行业日实测），等权版易被小成分股极端收益主导"),
    D("SEC.TREND.RET_60D", "行业60日收益(等权指数)", "TREND", "state", "pct", "neutral",
      "idx_eq_t/idx_eq_{t-60}-1", "M8", value_col="ret_60d",
      remark="定义：等权行业指数近 60 交易日收益（%）" + _SEP +
             "判读：>0 中期趋势向上；与 RET_20D 背离提示动能拐点" + _SEP +
             "⚠ 指数点位在 2 年块内按行业 cumprod 生成，基期不影响 20/60 日收益（比值与基期无关）"),
    D("SEC.TREND.RS_20D", "20日相对强度", "TREND", "state", "pct", "neutral",
      "ret_20d − 当日 113 行业等权均值（M3；横截面均值恒 0）", "M3",
      value_col="rs_20d",
      remark="定义：20 日相对强度 = ret_20d − 当日 113 行业等权均值（M3，%），横截面均值恒 0" + _SEP +
             "判读：>0 跑赢同侪、<0 跑输；**基准是同日 HK 行业等权均值，不是恒生指数**（库内无官方行业指数序列）"),
    D("SEC.TREND.RS_60D", "60日相对强度", "TREND", "state", "pct", "neutral",
      "ret_60d − 当日 113 行业等权均值", "M3", value_col="rs_60d",
      remark="定义：60 日相对强度 = ret_60d − 当日 113 行业等权均值（%），横截面均值恒 0" + _SEP +
             "判读：>0 中期跑赢；与 RS_20D 同向确认趋势，背离提示相对强弱换挡"),
    D("SEC.TREND.RS_XSEC_PCT", "相对强度横截面分位", "TREND", "state", "score_0_100",
      "high_good", "M1 中位秩，n_valid<10 不给分位", "M1", pct_x_col="rs_xsec_pct",
      remark="定义：RS_20D 的当日横截面中位秩分位（M1，0–100），**TREND 维度单锚**、RRG 横轴" + _SEP +
             "判读：>50 相对强度位于同侪上半区；113 行业分母随当日有效行业数变化" + _SEP +
             "⚠ n_valid<10 不给分位；阶段 3 决策 L3 不排名，只作状态描述"),
    D("SEC.TREND.RS_MOM_20D", "RS 分位20日变化", "TREND", "state", "score_delta",
      "neutral", "rs_xsec_pct(t) − rs_xsec_pct(t−20)", "M0",
      value_col="rs_mom_20d",
      remark="定义：RS 横截面分位的 20 交易日变化 p(t)−p(t−20)（RRG 纵轴，−100~100）" + _SEP +
             "判读：>0 相对位置改善（走强/领先），<0 恶化（走弱/落后）；与 RS_XSEC_PCT 组合成四象限" + _SEP +
             "⚠ 不足 21 交易日为 NULL"),
    D("SEC.TREND.RRG_QUADRANT", "四象限状态", "TREND", "state", "class", "neutral",
      "M5：rs_pct≥50 & Δ≥0 领先 / <50 & Δ≥0 走强 / ≥50 & Δ<0 走弱 / 否则落后",
      "M5", value_col="rrg_code", extra_kind="rrg",
      remark="定义：以 RS 分位 50 与分位 20 日变化 0 划四象限，1领先/2走强/3走弱/4落后（分类值）" + _SEP +
             "判读：领先=强且更强、走强=弱但改善、走弱=强但转弱、落后=弱且更弱；**只描述当前状态，不含超配/买卖含义**" + _SEP +
             "⚠ raw_value 存 1–4 编码、中文 label 写 extra.label；**横截面分位近似 RRG，非标准 JdK RS-Ratio/Momentum**"),
    D("SEC.TREND.MOM_RANK", "20日动量排名分位", "TREND", "state", "score_0_100",
      "high_good", "M1(rank(method=min)) 反向：rank 越小分位越高", "M1",
      pct_x_col="mom_rank_pct",
      remark="定义：当日 113 行业 ret_20d 降序排名的反向分位（rank(method=min) → M1，0–100）" + _SEP +
             "判读：分位高 = 动量排名靠前；已做反向翻转，消费端不得二次翻转" + _SEP +
             "⚠ 与 RS_XSEC_PCT 同源，仅作校验不重复计权"),

    # ── B 估值（风险侧；高分 = 更贵）─────────────────────────────────────
    D("SEC.VAL.PE_TTM", "行业PE(市值加权调和)", "VAL", "risk", "x", "high_risk",
      "Σw/Σ(w/pe)，仅 pe>0；成分覆盖<60% 置 NULL", "M6", value_col="pe_agg",
      remark="定义：行业内成分股市值加权调和 PE = Σw/Σ(w/pe)，仅取 pe>0，等价「行业总市值 / 行业总盈利」（x）" + _SEP +
             "判读：**高 = 更贵**（风险侧）；只作估值水位读数，不得与拥挤度相加抵消（§3.5 不产出单一综合分）" + _SEP +
             "⚠ **不是官方行业指数 PE**：由个股 pe_ttm_ratio 聚合，与 CN sector_valuation_snapshot 的申万口径不同构" + _SEP +
             "⚠ M8 口径变更：文档原文「流通市值加权」，实测 circular_market_val 2000-2023 全 NULL、2024-05 起仅 65 日有值 → 改用 total_market_val；成分覆盖 <60% 置 NULL"),
    D("SEC.VAL.PB", "行业PB(市值加权调和)", "VAL", "risk", "x", "high_risk",
      "Σw/Σ(w/pb)，仅 pb>0", "M6", value_col="pb_agg",
      remark="定义：行业内成分股市值加权调和 PB = Σw/Σ(w/pb)，仅取 pb>0（x）" + _SEP +
             "判读：高 = 更贵；与 PE_TTM 联读分辨「盈利口径」与「净资产口径」差异" + _SEP +
             "⚠ 非官方行业 PB；成分覆盖 <60% 置 NULL"),
    D("SEC.VAL.DIV_YIELD", "行业股息率(市值加权)", "VAL", "risk", "pct", "high_good",
      "Σ(dy×w)/Σw", "M6", value_col="div_yield_agg", pct_x_col="dy_xsec_pct",
      remark="定义：行业成分股股息率按市值加权 Σ(dy×w)/Σw（%）" + _SEP +
             "判读：高 = 现金回报高、估值相对不贵；percentile_xsec 取 100−p，使高分位仍统一代表「更贵的一侧」" + _SEP +
             "⚠ 覆盖成分 <60% 置 NULL"),
    D("SEC.VAL.PE_XSEC_PCT", "PE 横截面分位", "VAL", "risk", "score_0_100",
      "high_risk", "M1(pe_agg)", "M1", pct_x_col="pe_xsec_pct",
      remark="定义：pe_agg 当日横截面中位秩分位（M1，0–100），**VALUATION 维度单锚**、L3 risk_side" + _SEP +
             "判读：>50 比同日多数 HK 行业贵；首期主用口径" + _SEP +
             "⚠ 2002-01-01 前 total_market_val 覆盖率 <6%，该段无估值读数"),
    D("SEC.VAL.PB_XSEC_PCT", "PB 横截面分位", "VAL", "risk", "score_0_100",
      "high_risk", "M1(pb_agg)", "M1", pct_x_col="pb_xsec_pct",
      remark="定义：pb_agg 当日横截面分位（M1，0–100）" + _SEP +
             "判读：>50 比同日多数 HK 行业贵；与 PE_XSEC_PCT 同源，仅作校验不进维度分"),
    D("SEC.VAL.REL_TO_MARKET", "相对全行业估值", "VAL", "risk", "ratio", "high_risk",
      "pe_agg / 当日 113 行业 pe_agg 中位数", "M0", value_col="rel_to_market",
      remark="定义：pe_agg / 当日 113 行业 pe_agg 中位数（ratio）" + _SEP +
             "判读：>1 贵于中位行业；与 PE_XSEC_PCT 高度冗余，**只作校验项，不进维度分**（§7.2 去重）"),
    D("SEC.VAL.PE_TS_PCT", "PE 自身历史分位", "VAL", "risk", "score_0_100",
      "high_risk", "M2 expanding/10年滚动，min_periods=250，as-of", "M2",
      pct_t_col="pe_ts_pct", n_col="pe_n_sample",
      remark="定义：pe_agg 自身历史的 as-of expanding 时序分位（M2，0–100，min_periods=250）" + _SEP +
             "判读：>50 贵于自身历史多数时候；与横截面分位互补（一个比同侪、一个比自己）" + _SEP +
             "⚠ HK 估值由个股聚合、2002 起约 6,000 交易日，可出时序分位（与 CN 仅 27 日快照不同）" + _SEP +
             "⚠ 2002-01-01 前 total_market_val 覆盖率 <6%，该段置 NULL 并在 L3 加 BREAK 闸门"),

    # ── C 资金（状态侧；HK 无同花顺行业资金流，只剩南向）────────────────
    D("SEC.FLOW.SOUTHBOUND_NET", "南向资金按行业净流入", "FLOW", "state", "yi_hkd",
      "high_good", "Σ daily_ggt_hold.est_net_inflow（M6 求和）", "M6",
      value_col="southbound_net",
      remark="定义：南向资金按行业净流入 = Σ daily_ggt_hold.est_net_inflow（亿港元）" + _SEP +
             "判读：>0 南向净买入该行业；HK 侧唯一资金类读数，**不作预测信号**" + _SEP +
             "⚠ 源 2026-04-20 起仅 136 个交易日、691 只标的（686 只可映射行业，99.3%）；"
             "est_ 为「持股变动×收盘价」的估算值，非交易所披露净买额" + _SEP +
             "⚠ HK 侧无 NET_1D/5D/20D/NORM/PRICE_FLOW_GAP 数据源，C 组仅此一项"),

    # ── D 拥挤度（风险侧；k=2 部分模式，不开 EXTREME）───────────────────
    D("SEC.CROWD.AMT_SHARE_PCT", "成交额占比时序分位", "CROWD", "risk",
      "score_0_100", "high_risk", "成交额/当日有效行业合计 → M2(10年滚动,250)",
      "M2", pct_t_col="amt_share_pct", n_col="amt_share_n",
      remark="定义：行业成交额占当日有效行业合计的比例，再取 10 年滚动 as-of 时序分位（M2，0–100，min_periods=250）" + _SEP +
             "判读：高 = 成交占比处于自身历史高位，是 HK 拥挤两因子之一；单一因子 >80 不判极高" + _SEP +
             "⚠ 同源占比量纲自消；2000 起长历史，是 HK 侧唯一可出时序分位的拥挤因子" + _SEP +
             "⚠ rank_n 实测 109~112（非恒定 113），早期段不与后期比"),
    D("SEC.CROWD.TURNOVER_PCT", "换手率分位(本期不可用)", "CROWD", "risk",
      "score_0_100", "high_risk", "M6+M2", "M2", active=False,
      remark="定义：行业换手率分位（M6+M2），本期**登记但不产出**" + _SEP +
             "判读：原设计作拥挤第三因子；本期不可用，故 CROWDING 降为 k=2 部分模式、不开 EXTREME" + _SEP +
             "🔴 实测：hk_daily_quote.turnover_rate 2000-2023 全 NULL、2024-05-14 起仅 75 个交易日零星有值" + _SEP +
             "⚠ is_active=false，不写入 sector_indicator_value；列覆盖恢复后须换 params_hash 再启用"),
    D("SEC.CROWD.VAL_PCT", "估值因子分位(复用)", "CROWD", "risk", "score_0_100",
      "high_risk", "直取 PE_XSEC_PCT，不重复计权", "M1", pct_x_col="pe_xsec_pct",
      remark="定义：拥挤度估值因子，直取 SEC.VAL.PE_XSEC_PCT（0–100，复用不重复计权）" + _SEP +
             "判读：>80 计一票命中；与 SEC.DIM.VALUATION 读同一批 PE 原子，语义不同（风险共振一票 vs 估值水位程度）" + _SEP +
             "⚠ 跨层重复计权已登记（§3.4 D 组注），消费端不得当作两条互相印证的独立证据"),
    D("SEC.CROWD.HIT_CNT", "拥挤因子命中数", "CROWD", "risk", "count", "high_risk",
      "M5 Σ1{p_i>80}，缺失因子不进分子也不进分母", "M5", value_col="crowd_hit",
      extra_kind="crowd",
      remark="定义：两因子中 >80 分位的命中数 Σ1{p_i>80}（0..k），k = 当日可用因子数" + _SEP +
             "判读：命中越多越拥挤；**缺失因子不进分子也不进分母**，k<2 → NULL，不得把缺失按 0 计" + _SEP +
             "⚠ 本期 k≤2（缺 TURNOVER_PCT），extra.k / hit / members 随行透出"),
    D("SEC.CROWD.GATE", "拥挤档位", "CROWD", "risk", "class", "high_risk",
      "M5：k=2 时 2/2→HIGH / 1→WATCH / 0→NORMAL；**不出 EXTREME**", "M5",
      value_col="crowd_gate_code", extra_kind="crowd",
      remark="定义：拥挤档位 NORMAL/WATCH/HIGH/NA（M5，k=2 部分模式分类值）" + _SEP +
             "判读：2/2→HIGH、1→WATCH、0→NORMAL、k<2→NA；**本期不出 EXTREME**（缺一因子无权判极高）" + _SEP +
             "⚠ raw_value 存 0–4 编码、label/k/partial 在 extra，partial=true 恒开" + _SEP +
             "⚠ 与阶段 2 CN 的 k=3 分母不同，两期闸门不可跨期比较"),

    # ── F 行业宽度（状态侧）──────────────────────────────────────────────
    D("SEC.BREADTH.UP_RATIO", "成分股收涨占比", "BREADTH", "state", "pct",
      "high_good", "M6 count-if/n", "M6", value_col="up_ratio",
      pct_x_col="up_ratio_xsec",
      remark="定义：行业内当日收涨成分股占比（%，M6 count-if/n）" + _SEP +
             "判读：高 = 行业内部参与广度健康；**与市场级 SECTOR.UP_RATIO 区分**——后者是「多少个行业在涨」，本项是「行业内多少股票在涨」" + _SEP +
             "⚠ 与 SECTOR.UP_RATIO HK 等权合成做过一致性验收：52 日逐日完全一致（max_abs_diff=0.0）"),
    D("SEC.BREADTH.ABOVE_MA60", "成分股站上MA60占比", "BREADTH", "state", "pct",
      "high_good", "M6 + rolling MA60（60 日暖机）", "M6", value_col="above_ma60",
      pct_x_col="above_ma60_xsec",
      remark="定义：行业内 close > MA60 的成分股占比（%，需 60 交易日暖机）" + _SEP +
             "判读：>50 中期广度健康；极高位偏过热，须与趋势方向同用" + _SEP +
             "⚠ 不足 60 交易日为 NULL"),
    D("SEC.BREADTH.NEW_HIGH_LOW", "60日新高−新低归一净占比", "BREADTH", "state",
      "pct", "high_good", "100×(n_high−n_low)/(n_high+n_low)", "M6",
      value_col="new_high_low", pct_x_col="new_high_low_xsec",
      remark="定义：60 日新高−新低**归一净占比** = 100×(n_high−n_low)/(n_high+n_low)（%，−100~100）" + _SEP +
             "判读：>0 创新高者占优 = 中期动能健康；持续为负而行业指数走高 = 内在动能衰竭" + _SEP +
             "⚠ 计数类净额必须归一（§5.7 L2-a ③），不得用家数差——股票池扩缩会读成「年份」的函数"),
    D("SEC.BREADTH.RET_DISPERSION", "成分20日收益离散度", "BREADTH", "state",
      "pct", "high_risk", "M6 + 横截面 std", "M6", value_col="ret_dispersion",
      pct_x_col="ret_dispersion_xsec",
      remark="定义：行业内成分股 20 日收益的横截面标准差（M6 + std，%）" + _SEP +
             "判读：direction=high_risk（**高 = 分化大**）；BREADTH 维度内 sign=-1 翻转，高分位代表更集中" + _SEP +
             "⚠ 与 A 组 DISPERSION_INTRA 同源，同维度内去重只留本项"),
]
ACTIVE_DEFS = [d for d in HK_INDICATOR_DEFS if d["is_active"]]

# 维度：单锚 / 规则判定 / 分块等权；unavailable 组写 NULL 行并标 reason
HK_DIMENSION_DEFS = {
    "TREND": dict(side="state", structure="single_anchor",
                  anchor="SEC.TREND.RS_XSEC_PCT"),
    "FLOW": dict(side="state", structure="unavailable", anchor=None,
                 reason="HK 侧只有 SOUTHBOUND_NET（2026-04 起 136 日），无行业资金流序列；单锚不成立"),
    "VALUATION": dict(side="risk", structure="single_anchor",
                      anchor="SEC.VAL.PE_XSEC_PCT"),
    "CROWDING": dict(side="risk", structure="rule", anchor=None),
    "FUNDAMENTAL": dict(side="state", structure="unavailable", anchor=None,
                        reason="financial_indicator 港股 announce_date 全 NULL（M6 财务 PIT 不可用）+ 一致预期仅 A 股"),
    "BREADTH": dict(side="state", structure="equal", anchor=None,
                    members=[("SEC.BREADTH.UP_RATIO", 1),
                             ("SEC.BREADTH.ABOVE_MA60", 1),
                             ("SEC.BREADTH.NEW_HIGH_LOW", 1),
                             ("SEC.BREADTH.RET_DISPERSION", -1)]),
}

# HK 业务指标集合：与 CN 共用同一组 biz_code（开放集合），但按市场覆盖
# 输入清单与启用状态。⚠ SEC.BIZ.RANK 本期 is_active=False（阶段 3 用户决策
# 「HK 不排名」）—— 字典仍登记该行，值表不产出，前端显示「本期不提供」。
HK_BIZ_DEFS = {code: dict(cfg, inputs=list(cfg["inputs"]))
               for code, cfg in _CN_BIZ_DEFS.items()}
HK_BIZ_DEFS["SEC.BIZ.STATE_SIDE"]["inputs"] = [
    {"kind": "dimension", "code": "SEC.DIM." + n}
    for n, c in HK_DIMENSION_DEFS.items()
    if c["side"] == "state" and c["structure"] in ("single_anchor", "equal")]
HK_BIZ_DEFS["SEC.BIZ.RANK"]["is_active"] = False
HK_BIZ_DEFS["SEC.BIZ.RANK"]["output_schema"] = dict(
    _CN_BIZ_DEFS["SEC.BIZ.RANK"]["output_schema"],
    level="恒 NULL：阶段 3 用户决策 HK 首期不产出跨行业排名")


# ── 旧 L1 原子口径指纹（仅供旧版 _hk_dim_spec / 回归测试使用）─────────────────
def _hk_params_hash(d: dict) -> str:
    """HK L1 原子口径指纹（旧版 payload，不含市场/分类）。

    值表 model_version 与字典 params_hash 已统一走 sector_layer._dict_hash；
    本函数只供 _hk_dim_spec 的 upstream 摘要与 test_sector_params_hash 的
    L1 payload 敏感性断言使用，不再参与任何写库。
    """
    payload = "|".join([d["code"], d["unit"],
                        d["direction"], d["formula"], d["model_key"], CALIB_VERSION])
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


# ── HK L2 / L3 口径指纹（统一线，spec 纳入两市场实现差异）──
def _hk_dim_spec() -> dict:
    """HK 六维口径规格：结构 + 锚/成员/sign + 上游 L1 定义 + hk1.0。"""
    out = {}
    for name, cfg in HK_DIMENSION_DEFS.items():
        codes = ([cfg["anchor"]] if cfg["anchor"] else
                 [c for c, _ in cfg.get("members", [])])
        if name == "CROWDING":
            codes = list(_HK_CROWD_FACTORS)
        out[name] = {
            "layer": "L2", "code": "SEC.DIM." + name,
                        "version": CALIB_VERSION,
            "structure": cfg["structure"], "side": cfg["side"],
            "anchor": cfg["anchor"],
            "members": [{"code": c, "sign": s}
                        for c, s in cfg.get("members", [])],
            "upstream": {c: _atom_spec(HK_INDICATOR_DEFS, c, _hk_params_hash)
                         for c in codes},
        }
    return out


def _hk_dim_hashes() -> dict:
    """循环外预计算一次，避免每个行业日重复 json.dumps（热路径护栏）。"""
    return {n: _hash_spec(s) for n, s in _hk_dim_spec().items()}


def _hk_biz_spec(biz_code: str, dim_hashes: dict) -> dict:
    """HK L3 单指标口径规格：自身定义 + 六维指纹 + 闸门阈值 + 不排名 + hk1.0。"""
    cfg = HK_BIZ_DEFS[biz_code]
    return {
        "layer": "L3", "code": biz_code,
                "version": CALIB_VERSION,
        "kind": cfg["kind"], "side": cfg["side"],
        "inputs": cfg["inputs"], "output_schema": cfg["output_schema"],
        "upstream_dims": dim_hashes,
        "params": {
            "enabled_dimensions": list(ENABLED_DIMENSIONS),
            "coverage_min": COVERAGE_MIN,
            "valid_days_20_min": VALID_DAYS_20_MIN,
            "valid_days_window": VALID_DAYS_WINDOW,
            "crowd_pct_high": CROWD_PCT_HIGH,
            "short_sample": SHORT_SAMPLE,
            "min_periods": MIN_PERIODS,
            "cap_start": str(CAP_START),
            "no_rank": True,
        },
    }


def _hk_biz_hashes() -> dict:
    """循环外预计算一次；只产出 is_active=True 的业务指标。"""
    dh = _hk_dim_hashes()
    return {c: _hash_spec(_hk_biz_spec(c, dh))
            for c, cfg in HK_BIZ_DEFS.items() if cfg["is_active"]}


def _hk_biz_params_hash(biz_code: str) -> str:
    return _hash_spec(_hk_biz_spec(biz_code, _hk_dim_hashes()))


def _hk_biz_dict_rows() -> list:
    """兼容保留：字典已统一由 sector_layer.sync_dict 生成。"""
    now = dt.datetime.now().astimezone()
    dh = _hk_dim_hashes()
    return [dict(market_scope=MARKET,
                 biz_code=code, biz_name=cfg["biz_name"],
                 question=cfg["question"], side=cfg["side"], kind=cfg["kind"],
                 inputs=cfg["inputs"], params={"version": CALIB_VERSION},
                 output_schema=cfg["output_schema"],
                 params_hash=_hash_spec(_hk_biz_spec(code, dh)),
                 is_active=cfg["is_active"],
                 remark="定义：" + cfg["biz_name"] + "　·　判读：" +
                        (cfg["question"] or "") + "　·　数据：HK 全期非 PIT / 置信度低",
                 updated_at=now)
            for code, cfg in HK_BIZ_DEFS.items()]


# ── 数据加载（R1/R7：全部带时间下界）────────────────────────────────────────
_INDEX_COLS = ["trade_date", "sector_code", "sector_name", "ret_eq", "ret_cap",
               "amount", "pe_agg", "pb_agg", "div_yield_agg", "up_ratio",
               "above_ma60", "new_high_low", "ret_dispersion", "southbound_net",
               "coverage", "n_constituent"]


def _load_index_daily(conn, start: dt.date, end: dt.date) -> pd.DataFrame:
    w = start - dt.timedelta(days=WARMUP_DAYS)
    sql = """
        SELECT trade_date, sector_code, sector_name, ret_eq, ret_cap, amount,
               pe_agg, pb_agg, div_yield_agg, up_ratio, above_ma60,
               new_high_low, ret_dispersion, southbound_net, coverage, n_constituent
        FROM industry.hk_index_daily
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY sector_code, trade_date
    """
    return read_sql(conn, sql, params=[w, end])


def _precompute_amt_share(conn, start: dt.date, end: dt.date) -> pd.DataFrame:
    """3 列全史 → 成交额占比 + 10 年 as-of 时序分位（与 CN 同款，独立数据源）。

    ⚠ R3（2026-11-17）：原实现一次读 69 万行 × 3 列再 groupby.transform，
      `expanding` 的有序 buf 副本把峰值推到 368MB（服务上限 512M、目标 200M）。
      分位与占比都可按行业独立计算（分母是「当日全部行业」需一次全局和），
      改为：① 一次读当日分母映射（轻量）；② 按行业分批读 + 批内算 + **只保留
      trade_date >= start 的结果行**（历史仅用于计算分位，不需返回给增量窗口）。
      逐值语义不变（每行业的分位只依赖本行业历史），峰值与总行数脱钩。

      `start` 由调用方传增量窗口起点；全量回填时 start=2000-01-03 会保留全史。
    """
    from market_state_daily import _expanding_percentile

    cols = ["trade_date", "sector_code", "amt_share", "amt_share_pct", "amt_share_n"]
    # ① 全史当日分母：只取两列（69 万行 × 2），聚合成 ~6,500 行字典后释放
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_date, sum(amount) AS total
            FROM industry.hk_index_daily
            WHERE trade_date <= %s AND amount IS NOT NULL
            GROUP BY trade_date
        """, (end,))
        denom = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}
    if not denom:
        return pd.DataFrame(columns=cols)

    # ② 按行业分批：每批只持有 10 个行业的全史（约 6 万行），算完即释放
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT sector_code FROM industry.hk_index_daily
            WHERE trade_date <= %s AND amount IS NOT NULL
            ORDER BY sector_code
        """, (end,))
        sectors = [r[0] for r in cur.fetchall()]

    parts = []
    for i in range(0, len(sectors), 10):
        batch = sectors[i:i + 10]
        sql = """
            SELECT trade_date, sector_code, amount
            FROM industry.hk_index_daily
            WHERE trade_date <= %s AND amount IS NOT NULL
              AND sector_code = ANY(%s)
            ORDER BY sector_code, trade_date
        """
        df = read_sql(conn, sql, params=[end, batch])
        if df.empty:
            continue
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        df["amt_share"] = df["amount"].astype(float) / df["trade_date"].map(denom)
        # ⚠ 必须按行业分组算：批内 10 个行业混在一起传会让滚动窗口跨行业串样本
        # （实测分位差 21.8，口径错误）。原实现的 groupby.transform 天然按行业隔离。
        g = df.groupby("sector_code", sort=False)["amt_share"]
        df["amt_share_pct"] = g.transform(
            lambda s: _expanding_percentile(s, window=ROLL_WINDOW, min_periods=MIN_PERIODS))
        df["amt_share_n"] = g.transform(
            lambda s: s.rolling(ROLL_WINDOW, min_periods=1).count())
        # 只保留窗口内结果；历史行只参与本行业分位计算，不再驻留
        parts.append(df.loc[df["trade_date"] >= start, cols])
        del df
    if not parts:
        return pd.DataFrame(columns=cols)
    return pd.concat(parts, ignore_index=True)


# ── 计算主体 ────────────────────────────────────────────────────────────────
def _compute_hk_frame(df: pd.DataFrame, amt: pd.DataFrame) -> pd.DataFrame:
    """一行 = 一个 HK 行业日。所有横截面 groupby 只按 trade_date —— 本模块
    的输入 SQL 只取表 27（纯 HK / HS_L3），不存在 CN 行，物理上不会混排。
    """
    if df.empty:
        return pd.DataFrame()
    num = ["ret_eq", "ret_cap", "amount", "pe_agg", "pb_agg", "div_yield_agg",
           "up_ratio", "above_ma60", "new_high_low", "ret_dispersion",
           "southbound_net", "coverage"]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 统一为 datetime.date：与 amt 表 merge、与 CLI 窗口比较时类型一致
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df = df.sort_values(["sector_code", "trade_date"]).reset_index(drop=True)

    # 块内 cumprod 生成等权指数点位：基期不影响 20/60 日收益（比值与基期无关），
    # WARMUP_DAYS=400 保证块边界的 shift(60) 有足够预热。
    df["idx_eq"] = df.groupby("sector_code", sort=False)["ret_eq"].transform(
        lambda s: (1.0 + s.fillna(0.0) / 100.0).cumprod())
    gsec = df.groupby("sector_code", sort=False)["idx_eq"]
    df["ret_20d"] = 100 * (df["idx_eq"] / gsec.shift(20) - 1)
    df["ret_60d"] = 100 * (df["idx_eq"] / gsec.shift(60) - 1)

    gd = df.groupby("trade_date", sort=False)
    df["n_valid"] = df.groupby("trade_date")["ret_20d"].transform("count")
    df["rs_20d"] = df["ret_20d"] - df.groupby("trade_date")["ret_20d"].transform("mean")
    df["rs_60d"] = df["ret_60d"] - df.groupby("trade_date")["ret_60d"].transform("mean")
    df["rs_xsec_pct"] = df.groupby("trade_date")["rs_20d"].transform(_xsec_percentile)
    df["mom_rank"] = df.groupby("trade_date")["ret_20d"].rank(
        ascending=False, method="min")
    df["mom_rank_pct"] = df.groupby("trade_date")["mom_rank"].transform(
        _reverse_percentile)
    df["rs_mom_20d"] = df.groupby("sector_code")["rs_xsec_pct"].diff(20)

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

    # 估值：横截面分位（首期主用）+ 2002 起可出的时序分位
    df["pe_xsec_pct"] = gd["pe_agg"].transform(_xsec_percentile)
    df["pb_xsec_pct"] = gd["pb_agg"].transform(_xsec_percentile)
    df["dy_xsec_pct"] = gd["div_yield_agg"].transform(_xsec_percentile).rsub(100)
    df["rel_to_market"] = df["pe_agg"] / gd["pe_agg"].transform("median").replace(0, np.nan)
    from market_state_daily import _expanding_percentile
    df["pe_ts_pct"] = df.groupby("sector_code")["pe_agg"].transform(
        lambda s: _expanding_percentile(s, window=ROLL_WINDOW, min_periods=MIN_PERIODS))
    df["pe_n_sample"] = df.groupby("sector_code")["pe_agg"].transform(
        lambda s: s.notna().expanding().count())

    # 宽度：横截面分位（RET_DISPERSION 为 high_risk，在维度合成时 sign=-1 翻转）
    for src, dst in (("up_ratio", "up_ratio_xsec"), ("above_ma60", "above_ma60_xsec"),
                     ("new_high_low", "new_high_low_xsec"),
                     ("ret_dispersion", "ret_dispersion_xsec")):
        df[dst] = gd[src].transform(_xsec_percentile)

    if not amt.empty:
        a = amt.copy()
        a["trade_date"] = pd.to_datetime(a["trade_date"]).dt.date
        df = df.merge(a, on=["trade_date", "sector_code"], how="left")
    else:
        for c in ("amt_share", "amt_share_pct", "amt_share_n"):
            df[c] = np.nan

    # 拥挤度：HK 只有 AMT_SHARE_PCT + VAL_PCT 两因子（TURNOVER_PCT 不可用）
    # → k=2 部分模式，crowd_gate 内部保证不开 EXTREME。
    crowd = [crowd_gate({"AMT_SHARE_PCT": a, "VAL_PCT": v})
             for a, v in zip(df["amt_share_pct"], df["pe_xsec_pct"])]
    df["crowd_hit"] = [None if c["gate"] == "NA" else c["hit"] for c in crowd]
    df["crowd_gate_code"] = [CROWD_GATE_CODES[c["gate"]] for c in crowd]
    df["_crowd"] = crowd

    df["_valid"] = df["rs_20d"].notna().astype(int)
    df["valid_days_20"] = df.groupby("sector_code")["_valid"].transform(
        lambda s: s.rolling(VALID_DAYS_WINDOW, min_periods=1).sum())

    # 维度分
    def _pct_col(code):
        d = next(x for x in ACTIVE_DEFS if x["code"] == code)
        col = d["pct_x_col"]
        return df[col] if col and col in df.columns else pd.Series(np.nan, index=df.index)

    for name, cfg in HK_DIMENSION_DEFS.items():
        if cfg["structure"] == "single_anchor":
            df["dim_" + name] = _pct_col(cfg["anchor"])
        elif cfg["structure"] == "equal":
            keys = []
            for code, sign in cfg["members"]:
                key = "_eq_" + code
                df[key] = (100.0 - _pct_col(code) if sign < 0 else _pct_col(code))
                keys.append(key)
            v = df[keys]
            df["dim_" + name] = v.mean(axis=1, skipna=True)
            df.loc[v.notna().sum(axis=1) < 2, "dim_" + name] = np.nan
        else:
            df["dim_" + name] = np.nan

    # 业务读数：状态侧只聚「本期有分的状态维度」（TREND + BREADTH）
    state_cols = [f"dim_{n}" for n, c in HK_DIMENSION_DEFS.items()
                  if c["side"] == "state" and c["structure"] in ("single_anchor", "equal")]
    df["state_side"] = df[state_cols].mean(axis=1, skipna=True)
    df.loc[df[state_cols].notna().sum(axis=1) < 2, "state_side"] = np.nan
    df["risk_side"] = df["dim_VALUATION"]
    df["state_members"] = [sorted(state_cols)] * len(df)
    df["state_n"] = df[state_cols].notna().sum(axis=1)

    # coverage 分母 = 本期启用维度（4），不可用维度不进分母也进不了分子
    ok = sum(df[f"dim_{n}"].notna().astype(int)
             for n in ENABLED_DIMENSIONS if n != "CROWDING")
    ok = ok + (df["crowd_gate_code"] != CROWD_GATE_CODES["NA"]).astype(int)
    # ⚠ 命名为 biz_coverage：输入表本身有 coverage 列（成分覆盖度），
    #   若同名覆盖会让 L1 行的 coverage 误写成「维度覆盖度」。
    df["biz_coverage"] = ok / float(len(ENABLED_DIMENSIONS))
    df["rank_n"] = df.groupby("trade_date")["state_side"].transform("count")
    df["state_med"] = df.groupby("trade_date")["state_side"].transform("median")
    df["risk_med"] = df.groupby("trade_date")["risk_side"].transform("median")
    df["quadrant"] = [quadrant_of(s, r, sm, rm) for s, r, sm, rm in
                      zip(df["state_side"], df["risk_side"],
                          df["state_med"], df["risk_med"])]
    return df


# ── 输出行构造 ──────────────────────────────────────────────────────────────
def _indicator_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    rows, now = [], dt.datetime.now().astimezone()
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        for meta in ACTIVE_DEFS:
            raw, px = _get(rec, meta["value_col"]), _get(rec, meta["pct_x_col"])
            pt, ns = _get(rec, meta["pct_t_col"]), _get(rec, meta["n_col"])
            if raw is None and px is None and pt is None and not (ns and ns > 0):
                continue
            extra = dict(rank_n=_clean_int(rec.get("n_valid")),
                         pit_capable=False, survivorship_bias=True,
                         granularity="HK 113 恒生三级 vs CN 31 申万一级，不可混排")
            if meta["extra_kind"] == "rrg" and raw is not None:
                extra["label"] = RRG_LABELS.get(int(raw))
            if meta["extra_kind"] == "crowd":
                c = rec.get("_crowd") or {}
                extra.update(k=c.get("k"), partial=c.get("partial"),
                             members=c.get("members", []))
                if meta["code"] == "SEC.CROWD.GATE" and raw is not None:
                    extra["label"] = {v: k for k, v in CROWD_GATE_CODES.items()}.get(int(raw))
            rows.append(dict(
                market=MARKET,
                sector_code=rec["sector_code"], trade_date=d,
                indicator_code=meta["code"], raw_value=raw,
                percentile_xsec=px, percentile_ts=pt, n_sample=ns,
                coverage=_get(rec, "coverage"),
                quality=("short_sample" if ns is not None and
                         meta["model_key"] == "M2" and ns < SHORT_SAMPLE else "ok"),
                model_version=_dict_hash("L1", meta["code"]), extra=extra, updated_at=now))
    return rows


def _clean_int(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _score_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    rows, now = [], dt.datetime.now().astimezone()
    dim_hashes = {n: _dict_hash("L2", n) for n in HK_DIMENSION_DEFS}  # 统一口径指纹
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        for name, cfg in HK_DIMENSION_DEFS.items():
            score = (_get(rec, "dim_" + name)
                     if cfg["structure"] in ("single_anchor", "equal") else None)
            c = rec.get("_crowd") or {}
            cov = 1.0 if score is not None else 0.0
            if name == "CROWDING":
                cov = 1.0 if c.get("gate") != "NA" else 0.0
            members = ([cfg["anchor"]] if cfg["anchor"] else
                       [code for code, _ in cfg.get("members", [])])
            extra = dict(structure=cfg["structure"], anchor=cfg["anchor"],
                         members=members, pit_capable=False)

            if cfg["structure"] == "unavailable":
                extra["reason"] = cfg["reason"]
            if cfg["structure"] == "equal":
                extra["weight"] = round(1.0 / max(len(members), 1), 4)
            if name == "CROWDING":
                extra.update(crowd_gate=c.get("gate"), k=c.get("k"),
                             partial=c.get("partial"))
            rows.append(dict(
                market=MARKET,
                sector_code=rec["sector_code"], trade_date=d, dimension_code=name,
                side=cfg["side"], score=score, coverage=cov, dispersion=0.0,
                # HK 全期非 PIT → 置信度一律 low
                confidence=confidence_of(_dim_n_sample(rec, name), cov,
                                         pit_capable=False),
                members_used=members,
                params_hash=dim_hashes[name],
                extra=extra, updated_at=now))
    return rows


def _dim_n_sample(rec: dict, name: str):
    if name in ("TREND", "CROWDING"):
        return _get(rec, "amt_share_n")
    if name == "VALUATION":
        return _get(rec, "pe_n_sample")
    return None


def _biz_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    """HK L3 长表：按 HK_BIZ_DEFS 中 is_active=True 的业务指标产出多行。

    SEC.BIZ.RANK 本期不启用 → 该 biz_code 无值表行（business_dict 仍登记原因）。
    """
    rows, now = [], dt.datetime.now().astimezone()
    hashes = {c: _dict_hash("L3", c)
              for c, cfg in HK_BIZ_DEFS.items() if cfg["is_active"]}  # 统一口径指纹；RANK 不产出
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        cov = float(_get(rec, "biz_coverage") or 0.0)
        c = rec.get("_crowd") or {}
        flags = []
        if cov < COVERAGE_MIN:
            flags.append("DATA_COVERAGE")
        if d < CAP_START:
            flags.append("BREAK")       # 2002 前 total_market_val 覆盖率 <6%
        if c.get("gate") in ("HIGH", "EXTREME"):
            flags.append("CROWDING")
        state_n = int(_get(rec, "state_n") or 0)
        # HK 状态侧只有 TREND + BREADTH（FLOW/FUNDAMENTAL 无可靠源），
        # 相对完整体系的 4 组恒不齐 → PARTIAL_STATE 闸门恒开。
        # SHORT_SAMPLE 不是闸门：短样本由 L1 quality + L2/L3 confidence 承载
        # （与 CN 同源纪律，见 sector_layer._biz_rows 注释）。
        flags.append("PARTIAL_STATE")
        applicable = (cov >= COVERAGE_MIN
                      and int(_get(rec, "valid_days_20") or 0) >= VALID_DAYS_20_MIN
                      and "DATA_COVERAGE" not in flags and "BREAK" not in flags)
        extra = dict(state_members=rec["state_members"], state_n=state_n,
                     state_dims_full=4, partial=True,
                     enabled_dimensions=list(ENABLED_DIMENSIONS),
                     calib_version=CALIB_VERSION, pit_capable=False,
                     survivorship_bias=True,
                     no_rank=True,
                     no_rank_reason="阶段3 HK 首期不产出跨行业排名（C 组仅南向短样本、E 组不可得）；"
                                    "排序待阶段4 前瞻验证后再议",
                     crowd_k=c.get("k"), crowd_partial=c.get("partial"),
                     rank_n=_clean_int(rec.get("rank_n")),
                     break_notes="2002-01-01 前 total_market_val 覆盖率 <6%，加权版指数与估值不可用",
                     granularity="HK 113 恒生三级 vs CN 31 申万一级，不可混排")
        state_side = _get(rec, "state_side")
        risk_side = _get(rec, "risk_side")
        quadrant = rec.get("quadrant") or "NA"
        rank_n = _clean_int(rec.get("rank_n"))
        common = dict(
            market=MARKET,
            sector_code=rec["sector_code"], trade_date=d,
            coverage=cov, applicable=bool(applicable),
            # HK 全期非 PIT（stock_sector 当前成分）→ 置信度不高于 low
            confidence=confidence_of(_get(rec, "pe_n_sample"), cov,
                                     pit_capable=False),
            extra=extra, updated_at=now)
        for biz_code, h in hashes.items():
            score = state = level = None
            detail = {}
            if biz_code == "SEC.BIZ.STATE_SIDE":
                score = state_side
                detail = dict(state_members=rec["state_members"],
                              state_n=state_n, partial=True,
                              state_dims_full=4)
            elif biz_code == "SEC.BIZ.RISK_SIDE":
                score = risk_side
            elif biz_code == "SEC.BIZ.QUADRANT":
                state = quadrant
                detail = dict(state_side=state_side, risk_side=risk_side)
            elif biz_code == "SEC.BIZ.GATE":
                detail = dict(gate_flags=flags, crowd_gate=c.get("gate"),
                              crowd_k=c.get("k"), crowd_partial=c.get("partial"),
                              no_rank=True, rank_n=rank_n,
                              no_rank_reason="阶段3 HK 首期不产出跨行业排名（C 组仅南向短样本、E 组不可得）；"
                                             "排序待阶段4 前瞻验证后再议")
            rows.append(dict(common, biz_code=biz_code, score=score,
                             state=state, level=level, detail=detail,
                             params_hash=h))
    return rows


# ── 字典同步 ────────────────────────────────────────────────────────────────
def sync_dict_hk(conn) -> int:
    """兼容入口：字典表已统一，转交 sector_layer.sync_dict 写同一批行。"""
    from sector_layer import sync_dict
    return sync_dict(conn)


def _default_window(conn) -> tuple[dt.date, dt.date]:
    end = dt.date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM industry.business_value "
                    "WHERE market=%s", (MARKET,))
        last = cur.fetchone()[0]
    start = (last - dt.timedelta(days=REWIND_DAYS)) if last else \
            (end - dt.timedelta(days=INCREMENTAL_DAYS))
    return start, end


def _chunks(start: dt.date, end: dt.date, years: int = CHUNK_YEARS):
    out, s = [], start
    while s <= end:
        e = min(dt.date(s.year + years - 1, 12, 31), end)
        out.append((s, e))
        s = e + dt.timedelta(days=1)
    return out


def _iter_slice_frames(wide: pd.DataFrame, start: dt.date, end: dt.date,
                       days: int = SLICE_DAYS):
    """按 N 天切片生成输出行（HK 113 行业 → 比 CN 按月切片更细，控 R3 峰值）。"""
    if wide.empty:
        return
    d = pd.to_datetime(wide["trade_date"])
    mask = (d.dt.date >= start) & (d.dt.date <= end)
    w = wide.loc[mask]
    if w.empty:
        return
    dd = d.loc[mask]
    bucket = ((dd - dd.min()).dt.days // days)
    for _, idx in w.groupby(bucket).groups.items():
        yield w.loc[idx]


# ── 主流程 ──────────────────────────────────────────────────────────────────
def run(codes=None, ctx=None, start: dt.date = None, end: dt.date = None,
        backfill: bool = False, write: bool = True, init_schema: bool = False,
        skip_index: bool = False) -> dict:
    """计算入口。参数签名兼容 run_module，但**本模块不得挂进 market_scheduler**（R5）。"""
    t0 = time.monotonic()
    rid = None
    try:
        from monitor_collector import record_task_start
        rid = record_task_start("行业配置计算HK", market=MARKET)
    except Exception:
        log.warning("任务埋点起点失败（不影响计算）", exc_info=True)
    status = "ok"
    try:
        from regime_schema import ensure_sector_schema
        with get_conn() as conn:
            ensure_sector_schema(conn)      # 含第 27 号表 + 字典复合主键迁移
            if init_schema:
                return {"init_schema": True, "dict_rows": sync_dict_hk(conn)}
            if backfill:
                start, end = BACKFILL_START, (end or dt.date.today())
            elif start is None:
                start, end = _default_window(conn)
            else:
                end = end or dt.date.today()

            stats = dict(start=str(start), end=str(end), market=MARKET,
                         calib_version=CALIB_VERSION)
            # ① 数据层 D4：自算指数 + 行业日聚合（内部按月×按股票分批）
            if not skip_index:
                from hk_sector_index import build_index_daily
                stats["index"] = build_index_daily(conn, start, end, write=write)
            # 输入新鲜度闸门：数据层未产出时不制造空洞读数
            with conn.cursor() as cur:
                cur.execute("SELECT max(trade_date) FROM industry.hk_index_daily")
                upstream = cur.fetchone()[0]
            if upstream is None:
                raise RuntimeError("industry.hk_index_daily 为空 —— 数据层 D4 未产出，拒绝执行")
            end = min(end, upstream)
            if start > end:
                return {"skipped": "start > end（上游数据未就绪）"}

            stats["dict_rows"] = sync_dict_hk(conn)
            amt = _precompute_amt_share(conn, start, end)
            stats.update(iv=0, score=0, biz=0, chunks=0)
            for cs, ce in _chunks(start, end):
                raw = _load_index_daily(conn, cs, ce)
                if raw.empty:
                    continue
                wide = _compute_hk_frame(raw, amt)
                del raw
                for sub in _iter_slice_frames(wide, cs, ce):
                    iv = _indicator_rows(sub, cs, ce)
                    sc = _score_rows(sub, cs, ce)
                    bz = _biz_rows(sub, cs, ce)
                    if write:
                        stats["iv"] += _write_batched(
                            conn, "industry.indicator_value", iv,
                            PK_COLS + ["indicator_code"])
                        stats["score"] += _write_batched(
                            conn, "industry.dimension_value", sc,
                            PK_COLS + ["dimension_code"])
                        stats["biz"] += _write_batched(
                            conn, "industry.business_value", bz, BIZ_PK_COLS)
                    else:
                        stats["iv"] += len(iv)
                        stats["score"] += len(sc)
                        stats["biz"] += len(bz)
                    del iv, sc, bz, sub
                del wide
                gc.collect()
                stats["chunks"] += 1
                log.info("HK 块 %s~%s：iv=%d score=%d biz=%d", cs, ce,
                         stats["iv"], stats["score"], stats["biz"])
            stats["elapsed_s"] = round(time.monotonic() - t0, 2)
            stats["write"] = write
            log.info("HK 行业层计算完成 %s", stats)
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


def _main() -> int:
    ap = argparse.ArgumentParser(description="第④层 HK 行业配置计算（阶段3）")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--backfill", action="store_true", help="全量回填 2000-01-03 起")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--init-schema", action="store_true", help="只建表 + 同步 HK 字典")
    ap.add_argument("--skip-index", action="store_true", help="跳过数据层 D4（表 27 已就绪）")
    a = ap.parse_args()
    out = run(start=a.start, end=a.end, backfill=a.backfill,
              write=not a.dry_run, init_schema=a.init_schema,
              skip_index=a.skip_index)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
