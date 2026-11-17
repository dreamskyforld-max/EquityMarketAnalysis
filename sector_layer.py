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
import importlib
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
MARKETS = ("CN", "HK")
DICT_VERSION = "v2.0"           # 统一字典版本：一行一概念，口径变更触发全市场重算
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

# L3 业务读数阈值：抽成常量以便纳入口径指纹（数值与既有实现完全一致）。
# ⚠ 改这些值会换掉 L2/L3 的 params_hash，必须全量重算，不得只写新日期。
COVERAGE_MIN = 0.6              # applicable / DATA_COVERAGE / confidence_of
VALID_DAYS_20_MIN = 15          # 近 20 交易日有效读数门槛
VALID_DAYS_WINDOW = 20          # valid_days_20 的滚动窗口


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


# ── remark 三段式（与 regime.indicator_dict 的事实标准对齐）──────────────────
# sql/schema.sql:1710 / doc/industry_allocation_feasibility.md §3.1 要求：
#   单行文本，`定义：…　·　判读：…　·　数据：…`，可扩展 口径：/⚠/已知限制：，
#   无换行、无【】方括号；强调用 **…** 与 ⚠/🔴；引用优先写中文章节名。
# ⚠ remark 不进 _params_hash 的 payload —— 只改 remark 不换指纹、不触发历史重算；
#   但 formula / unit / direction / model_key / CALIB_VERSION 任一改动都会换指纹。
_SEP = "　·　"
_D_PRICE = ("数据：CN 申万一级 31 个官方行业指数（daily_benchmark / regime.sector_daily），"
            "1999-12-30 起 6,498 交易日；三批起点不同，早期行业数不足 31")
_D_XSEC = ("数据：同一 (CN, SW_L1, trade_date) 内按 M1 中位秩取横截面分位，"
           "n_valid<10 不给值；rank_n 实测 16(≤2010)→28(2014)→31(2018 起) 时变，"
           "早期分母不可与后期直接比")
_D_SNAP = ("数据：regime.sector_valuation_snapshot 只给当前横截面、无历史序列，"
           "2026-09-30 起逐日累积（实测 27 个快照日），漏采一天即永久缺失")
_D_FLOW = ("数据：同花顺约 90 个细分行业资金流经 regime.sector_mapping 映射到申万，"
           "只纳入 confirmed=true 或 corr≥0.6；源只给即时快照、2026-09-30 起")
_D_PIT = ("数据：成分按 industry.constituent 的 [eff_from, eff_to) 取用，"
          "有效起点 2021-12-13（决策 A：申万 2021 版分类启用日），更早不得用当前成分回算；"
          "按月 × 按股票分批聚合，pit_capable=false 段须降置信")
_D_FC = ("数据：regime.analyst_forecast_snapshot 2026-09-30 起、仅 A 股、实测 25 个快照日；"
         "30 快照窗口尚未满，按实际 n 出值并写 revision_n")


INDICATOR_DEFS = [
    # A 相对强弱与趋势（状态侧）—— 基准是 31 行业等权均值，不是大盘指数（§7.11 决策 3）
    D("SEC.TREND.RET_20D", "行业20日收益", "TREND", "state", "pct", "neutral",
      "close_t/close_{t-20}-1（直接取 sector_daily.ret_20d）", "M0",
      value_col="ret_20d",
      remark="定义：行业指数近 20 交易日收益，close_t/close_{t-20}−1（%）" + _SEP +
             "判读：>0 该行业过去一个月上涨，**只是历史动量不是预测**；与 RS_20D 联读区分「普涨」与「真相对强」" + _SEP +
             _D_PRICE + _SEP + "⚠ 三批起点前（行业数不足 31）为 NULL"),
    D("SEC.TREND.RET_60D", "行业60日收益", "TREND", "state", "pct", "neutral",
      "close_t/close_{t-60}-1（直接取 sector_daily.ret_60d）", "M0",
      value_col="ret_60d",
      remark="定义：行业指数近 60 交易日收益（%）" + _SEP +
             "判读：>0 中期趋势向上；与 RET_20D 背离时提示动能拐点（20 日转负而 60 日仍正 = 中期未破、短期转弱）" + _SEP +
             _D_PRICE),
    D("SEC.TREND.RS_20D", "20日相对强度", "TREND", "state", "pct", "high_good",
      "ret_20d - 当日有效行业 ret_20d 等权均值（M3，横截面均值恒0）", "M3",
      value_col="rs_20d",
      remark="定义：20 日相对强度 = 行业 ret_20d − 当日有效行业 ret_20d 等权均值（M3，%），横截面均值恒 0" + _SEP +
             "判读：>0 跑赢同侪、<0 跑输；**基准是 31 行业等权均值，不是沪深300或大盘指数**（§7.11 决策 3，库内无沪深300序列）" + _SEP +
             _D_PRICE),
    D("SEC.TREND.RS_60D", "60日相对强度", "TREND", "state", "pct", "high_good",
      "ret_60d - 当日有效行业 ret_60d 等权均值（M3）", "M3",
      value_col="rs_60d",
      remark="定义：60 日相对强度 = ret_60d − 当日有效行业等权均值（%），横截面均值恒 0" + _SEP +
             "判读：>0 中期跑赢；与 RS_20D 同向确认趋势，背离提示相对强弱换挡" + _SEP +
             _D_PRICE + _SEP + "⚠ 计算层新增列，非 sector_daily 原生字段"),
    D("SEC.TREND.RS_XSEC_PCT", "相对强度横截面分位", "TREND", "state", "score_0_100", "high_good",
      "M1 中位秩：100*(n_less+0.5*n_equal)/n_valid", "M1",
      pct_x_col="rs_xsec_pct", n_col="n_valid",
      remark="定义：RS_20D 的当日横截面中位秩分位（M1，0–100），**TREND 维度单锚**、RRG 横轴" + _SEP +
             "判读：>50 相对强度位于同侪上半区；阶段 4 前瞻 IC 检验不通过，只作状态描述、不得用于配置" + _SEP +
             _D_XSEC),
    D("SEC.TREND.RS_MOM_20D", "RS分位20日变化", "TREND", "state", "score_0_100", "high_good",
      "rs_xsec_pct(t) - rs_xsec_pct(t-20)，按行业 shift 20 个交易日", "M4",
      value_col="rs_mom_20d", pct_x_col="rs_mom_xsec_pct",
      remark="定义：RS 横截面分位的 20 交易日变化 p(t)−p(t−20)（RRG 纵轴，−100~100）" + _SEP +
             "判读：>0 相对位置改善（走强/领先），<0 恶化（走弱/落后）；与 RS_XSEC_PCT 组合成四象限" + _SEP +
             "数据：按行业 shift 20 个交易日，不足 21 日为 NULL；另有 rs_mom_xsec_pct 作当日横截面分位"),
    D("SEC.TREND.RRG_QUADRANT", "RRG四象限", "TREND", "state", "enum", "neutral",
      "rs_xsec_pct>=50 & Δ>=0→1领先; <50 & Δ>=0→2走强; >=50 & Δ<0→3走弱; <50 & Δ<0→4落后", "M5",
      value_col="rrg_code",
      remark="定义：以 RS 分位 50 与 RS 分位 20 日变化 0 划四象限，1领先/2走强/3走弱/4落后（分类值，不分位）" + _SEP +
             "判读：领先=强且更强、走强=弱但改善、走弱=强但转弱、落后=弱且更弱；**只描述当前状态，不含超配/买卖含义**" + _SEP +
             "数据：raw_value 存 1–4 编码，中文 label 写 extra.label" + _SEP +
             "⚠ 横截面分位近似 RRG，非标准 JdK RS-Ratio/Momentum",
      extra_kind="rrg"),
    D("SEC.TREND.MOM_RANK", "动量排名", "TREND", "state", "rank", "high_good",
      "当日 ret_20d 横截面降序 rank(method=min)，直接取 sector_daily.mom_rank", "M0",
      value_col="mom_rank", pct_x_col="mom_rank_pct", n_col="n_valid",
      remark="定义：当日 31 行业 ret_20d 降序排名（method=min，1..rank_n）" + _SEP +
             "判读：名次越小越强；percentile_xsec 已做反向翻转（rank 小 = 高分位），消费端不得二次翻转" + _SEP +
             _D_XSEC),
    # B 估值（风险侧，高分=更贵）
    D("SEC.VAL.PE_TTM", "行业PE-TTM", "VAL", "risk", "x", "high_risk",
      "sector_valuation_snapshot.pe_ttm 原值按 snapshot_date 对齐", "M0",
      value_col="pe_ttm", source="regime.sector_valuation_snapshot",
      remark="定义：申万一级行业 PE-TTM 官方快照原值（x）" + _SEP +
             "判读：**高 = 行业更贵**（风险侧）；只作估值水位读数，不得用「便宜」抵消拥挤度（§3.5 不产出单一综合分）" + _SEP +
             _D_SNAP + _SEP + "⚠ 首期只有横截面分位可用，时序分位待满 250 观测"),
    D("SEC.VAL.PB", "行业PB", "VAL", "risk", "x", "high_risk",
      "sector_valuation_snapshot.pb 原值", "M0",
      value_col="pb", source="regime.sector_valuation_snapshot",
      remark="定义：申万一级行业 PB 官方快照原值（x）" + _SEP +
             "判读：高 = 更贵；与 PE_TTM 联读可分辨「盈利下滑推高 PE」与「资产重估推高 PB」" + _SEP +
             _D_SNAP),
    D("SEC.VAL.DIV_YIELD", "行业股息率", "VAL", "risk", "pct", "high_good",
      "sector_valuation_snapshot.div_yield 原值", "M0",
      value_col="div_yield", source="regime.sector_valuation_snapshot",
      remark="定义：申万一级行业股息率官方快照原值（%）" + _SEP +
             "判读：高 = 现金回报高、估值相对不贵；与 PE_TTM 反向，是风险侧的缓冲读数" + _SEP +
             _D_SNAP),
    D("SEC.VAL.PE_XSEC_PCT", "PE横截面分位", "VAL", "risk", "score_0_100", "high_risk",
      "M1(pe_ttm)，越高越贵", "M1",
      pct_x_col="pe_xsec_pct", n_col="n_val", source="regime.sector_valuation_snapshot",
      remark="定义：PE_TTM 当日横截面中位秩分位（M1，0–100），**VALUATION 维度单锚**" + _SEP +
             "判读：>50 比同日多数行业贵；**首期主用口径**，不依赖历史深度；同时是 CROWDING 的 VAL_PCT 一票（跨层重复计权已登记）" + _SEP +
             _D_XSEC),
    D("SEC.VAL.PB_XSEC_PCT", "PB横截面分位", "VAL", "risk", "score_0_100", "high_risk",
      "M1(pb)", "M1", pct_x_col="pb_xsec_pct", n_col="n_val",
      source="regime.sector_valuation_snapshot",
      remark="定义：PB 当日横截面分位（M1，0–100）" + _SEP +
             "判读：>50 比同日多数行业贵；与 PE_XSEC_PCT 概念同源，按 §7.2 去重后**降级为校验项，不进维度分**" + _SEP +
             _D_XSEC),
    D("SEC.VAL.REL_TO_MARKET", "行业PE/市场PE中位数", "VAL", "risk", "ratio", "high_risk",
      "pe_ttm / 当日 31 行业 pe_ttm 中位数", "M0",
      value_col="rel_to_market", source="regime.sector_valuation_snapshot",
      remark="定义：行业 PE_TTM / 当日 31 行业 PE_TTM 中位数（ratio）" + _SEP +
             "判读：>1 贵于中位行业；与 PE_XSEC_PCT 高度冗余，**仅作校验项，不进维度分**（§7.2 去重）" + _SEP +
             _D_SNAP),
    D("SEC.VAL.PE_TS_PCT", "PE时序分位", "VAL", "risk", "score_0_100", "high_risk",
      "M2 expanding，min_periods=250", "M2",
      pct_t_col="pe_ts_pct", n_col="pe_n_sample", source="regime.sector_valuation_snapshot",
      remark="定义：行业 PE_TTM 自身历史的 as-of expanding 时序分位（M2，0–100，min_periods=250）" + _SEP +
             "判读：>50 贵于自身历史多数时候；与横截面分位互补（一个比同侪、一个比自己）" + _SEP +
             "数据：🔴 阶段 1 恒不可用——实测仅 27 个快照日 « 250，按 §5.7 一律 NULL 并写 pe_n_sample，禁止用小样本分位填充" + _SEP +
             "⚠ 满 250 观测后须换 params_hash + breaks 登记并全历史重算才启用"),
    # C 资金（状态侧）
    D("SEC.FLOW.NET_1D", "行业资金净流入", "FLOW", "state", "yi_yuan", "high_good",
      "sector_daily.fund_flow_net（同花顺→申万映射汇总，confirmed 准入）", "M0",
      value_col="fund_flow_net",
      remark="定义：行业当日资金净流入（亿元），同花顺细分行业经映射汇总到申万一级" + _SEP +
             "判读：>0 净流入；守恒校验 31 行业合计 = 市场级 SECTOR.FLOW_NET（§7.13 已实测）" + _SEP +
             _D_FLOW + _SEP + "⚠ 多条 THS 映射到同一 SW 须求和，mapped_ths_count 随读数透出"),
    D("SEC.FLOW.NET_5D", "近5日资金净流入", "FLOW", "state", "yi_yuan", "high_good",
      "rolling_sum(5, min_periods=5)，窗口不足不给值（§7.11）", "M4",
      value_col="fund_flow_net_5d",
      remark="定义：近 5 交易日资金净流入累计（rolling_sum(5, min_periods=5)，亿元）" + _SEP +
             "判读：>0 近一周持续流入；与 NET_1D 背离提示单日脉冲而非趋势" + _SEP +
             _D_FLOW + _SEP + "⚠ 窗口不足 5 日一律 NULL，防部分窗口当完整窗口（§7.11 教训）"),
    D("SEC.FLOW.NET_20D", "近20日资金净流入", "FLOW", "state", "yi_yuan", "high_good",
      "rolling_sum(20, min_periods=20)", "M4",
      value_col="net_20d",
      remark="定义：近 20 交易日资金净流入累计（rolling_sum(20, min_periods=20)，亿元）" + _SEP +
             "判读：>0 月度资金面偏强；与 NET_5D 背离提示短期资金反复" + _SEP +
             "数据：需 ≥20 个有资金流的交易日，按 2026-09-30 起累积约 2026-10 下旬才有值" + _SEP +
             "⚠ 计算层新增列，非 sector_daily 原生字段"),
    D("SEC.FLOW.NET_NORM", "归一资金净额", "FLOW", "state", "ratio", "high_good",
      "net_1d / 行业成交额（同为亿元；阶段0已标定 turnover 口径）", "M4",
      value_col="net_norm",
      remark="定义：归一资金净额 = net_1d / 行业成交额（同为亿元，ratio）" + _SEP +
             "判读：绝对值大 = 相对该行业体量的资金异动；**必须先归一才能取时序分位**" + _SEP +
             "数据：阶段 0 实测 sector_daily.turnover 单位亿元、31 行业合计覆盖全 A 99.8%+" + _SEP +
             "⚠ 净额随市场规模漂移，未归一的原始值取时序分位会把「年份」读进分位（§5.7 L2-a ②）"),
    D("SEC.FLOW.XSEC_PCT", "资金净额横截面分位", "FLOW", "state", "score_0_100", "high_good",
      "M1(net_norm)", "M1", pct_x_col="flow_xsec_pct", n_col="n_flow",
      remark="定义：NET_NORM 当日横截面分位（M1，0–100），**FLOW 维度单锚**" + _SEP +
             "判读：>50 资金面相对同侪更强；阶段 4 前瞻 IC 未通过，只作状态描述" + _SEP +
             _D_XSEC + _SEP +
             "⚠ 资金源：同花顺约 90 个细分行业经 regime.sector_mapping 映射到申万，"
             "只纳入 confirmed=true 或 corr≥0.6；源只给即时快照、2026-09-30 起"),
    D("SEC.FLOW.FLOW_RANK", "资金流排名", "FLOW", "state", "rank", "high_good",
      "直接取 sector_daily.flow_rank，pct 做反向翻转", "M0",
      value_col="flow_rank", pct_x_col="flow_rank_pct", n_col="n_flow",
      remark="定义：行业资金净流当日排名（1..n）" + _SEP +
             "判读：名次越小资金越强；percentile_xsec 已做反向翻转，消费端不得二次翻转" + _SEP +
             _D_FLOW + _SEP + "⚠ 与 XSEC_PCT 同源，仅作校验不重复计权"),
    D("SEC.FLOW.PRICE_FLOW_GAP", "量价-资金背离", "FLOW", "state", "rank", "neutral",
      "rs_rank - flow_rank；正=资金排名领先于价格排名", "M0",
      value_col="price_flow_gap",
      remark="定义：量价-资金背离 = rs_rank − flow_rank（−n..n）" + _SEP +
             "判读：正 = 价格排名落后于资金排名（资金领先），负 = 价格领先而资金未跟" + _SEP +
             _D_FLOW + _SEP +
             "⚠ **纯描述性读数，direction=neutral、不进维度分、不作预测信号**（与项目既有「指标无预测力」结论一致）"),
    # D 拥挤度（风险侧；阶段1 两因子部分模式）
    D("SEC.CROWD.AMT_SHARE_PCT", "成交额占比时序分位", "CROWD", "risk", "score_0_100", "high_risk",
      "share=turnover/当日有效行业合计 → M2 滚动10年(min_periods=250)", "M2",
      value_col="amt_share", pct_t_col="amt_share_pct", n_col="amt_share_n",
      remark="定义：行业成交额占当日有效行业合计的比例，再取 10 年滚动 as-of 时序分位（M2，0–100，min_periods=250）" + _SEP +
             "判读：**高 = 成交占比处于自身历史高位**（资金聚集、可能拥挤）；只是拥挤三因子中的一票，须与 TURNOVER_PCT、VAL_PCT 联读，单一因子 >80 不判极高" + _SEP +
             "数据：sector_daily.turnover 单位亿元，阶段 0 实测 31 行业合计/全 A 成交额 = 0.998088~0.999661（完备覆盖）；1999-12-30 起 6,498 日长历史，是快照期唯一可出时序分位的因子" + _SEP +
             "⚠ 早期 rank_n 16→28，同源占比分母随行业数变化，不可与 2018 后 31 行业直接比"),
    D("SEC.CROWD.VAL_PCT", "拥挤·估值因子", "CROWD", "risk", "score_0_100", "high_risk",
      "直取 SEC.VAL.PE_XSEC_PCT（复用，跨层重复计权已登记）", "M1",
      pct_x_col="pe_xsec_pct", n_col="n_val", source="regime.sector_valuation_snapshot",
      remark="定义：拥挤度估值因子，直取 SEC.VAL.PE_XSEC_PCT（0–100，复用不重复计权）" + _SEP +
             "判读：>80 计一票命中；与 SEC.DIM.VALUATION 读同一批 PE 原子但语义不同（风险共振一票 vs 估值水位程度）" + _SEP +
             _D_SNAP + _SEP +
             "⚠ 跨层重复计权已登记（§3.4 D 组注），消费端不得当作两条互相印证的独立证据"),
    D("SEC.CROWD.HIT_CNT", "拥挤因子命中数", "CROWD", "risk", "count", "high_risk",
      "Σ 1{p_i>80}，k=当日可用因子数", "M5",
      value_col="crowd_hit",
      remark="定义：三因子中 >80 分位的命中数 Σ1{p_i>80}（0..k），k = 当日可用因子数" + _SEP +
             "判读：命中越多越拥挤；**缺失因子既不进分子也不进分母**（k 减 1），k<2 → NULL，不得把缺失按 0 计" + _SEP +
             "数据：阶段 1 只有 2 因子（k=2 部分模式），阶段 2 成分表上线后 k=3；extra.hit / extra.k / extra.members 随行透出",
      extra_kind="crowd"),
    D("SEC.CROWD.GATE", "拥挤档位", "CROWD", "risk", "enum", "high_risk",
      "k=3: 3/3→EXTREME,>=2→HIGH,1→WATCH; k=2: 2/2→HIGH(不出EXTREME),1→WATCH; 0→NORMAL", "M5",
      value_col="crowd_gate_code",
      remark="定义：拥挤档位 NORMAL/WATCH/HIGH/EXTREME/NA（M5 条件命中，分类值）" + _SEP +
             "判读：k=3 时 3/3→EXTREME、≥2→HIGH、1→WATCH；k=2 部分模式最高只到 HIGH（**缺一因子无权判极高**）、0→NORMAL、k<2→NA" + _SEP +
             "数据：raw_value 存 0–4 编码，label/k/partial/members 在 extra" + _SEP +
             "⚠ 阶段 1(k=2) 与阶段 2(k=3) 分母不同，两期闸门不可跨期比较；阶段 4 因 EXTREME 仅 45 行判 NOT_EVALUABLE，验证完成前只作纯展示标记",
      extra_kind="crowd"),
    # ── 阶段 2：依赖 industry.constituent 的自下而上指标 ──────────────
    # 有效起点 2021-12-13（决策 A）；全部由 sector_stage2_agg 按月 × 按股票分批聚合。
    D("SEC.CROWD.TURNOVER_PCT", "行业换手率横截面分位", "CROWD", "risk", "score_0_100", "high_risk",
      "成分股 turnover_rate 按流通市值加权 → 当日横截面分位(M1)", "M6",
      value_col="turnover_w", pct_x_col="turnover_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + industry.constituent",
      remark="定义：成分股 turnover_rate 按流通市值加权的行业换手率，再取当日横截面分位（M1，0–100）" + _SEP +
             "判读：高 = 该行业交投相对同侪更热，是拥挤三因子的第三票；阶段 1 该因子恒缺，闸门按 k=2 部分模式" + _SEP +
             _D_PIT + _SEP +
             "⚠ 首期用**横截面分位**而非 M2 时序分位：成分快照起点 2021-12-13，样本不足以出稳定时序分位；满 250 观测后按字典切换并登记口径变更"),
    D("SEC.VAL.INTRA_DISPERSION", "行业内个股PE离散度", "VAL", "risk", "score_0_100", "high_risk",
      "成分股 pe_ttm_ratio 横截面 std → 当日横截面分位(M1)", "M6",
      value_col="pe_dispersion", pct_x_col="pe_disp_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + industry.constituent",
      remark="定义：行业内成分股 PE-TTM 的横截面标准差，再取当日横截面分位（M1，0–100）" + _SEP +
             "判读：高 = 行业内估值分化大（龙头与尾部定价撕裂）；与 PE_XSEC_PCT 高冗余，**仅作校验不进维度分**" + _SEP +
             _D_PIT + _SEP + "⚠ 个股 pe_ttm_ratio 2018 起才有，早期为 NULL"),
    D("SEC.BREADTH.UP_RATIO", "行业内上涨占比", "BREADTH", "state", "pct", "high_good",
      "M6：成分股 change_pct>0 占比", "M6",
      value_col="up_ratio", pct_x_col="up_ratio_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + industry.constituent",
      remark="定义：行业内当日收涨成分股占有行情成分数比例（%，M6 count-if/n）" + _SEP +
             "判读：**高 = 行业内部参与广度健康**；与市场级 SECTOR.UP_RATIO 区分——后者是「多少个行业在涨」，本项是「行业内多少股票在涨」" + _SEP +
             _D_PIT),
    D("SEC.BREADTH.ABOVE_MA60", "行业内站上MA60占比", "BREADTH", "state", "pct", "high_good",
      "M6：成分股 close > MA60 占比（rolling 60，min_periods=60）", "M6",
      value_col="above_ma60", pct_x_col="above_ma60_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + industry.constituent",
      remark="定义：行业内 close > MA60 的成分股占比（%，rolling 60 / min_periods=60）" + _SEP +
             "判读：>50 多数成分站上中期均线 = 中期广度健康；极高位偏过热，须与趋势方向同用" + _SEP +
             _D_PIT + _SEP + "⚠ 需 60 交易日暖机，起点后首 60 日为 NULL"),
    D("SEC.BREADTH.NEW_HIGH_LOW", "行业内新高−新低归一净占比", "BREADTH", "state", "pct", "high_good",
      "100×(n_high−n_low)/(n_high+n_low)，60日窗口；计数类必须归一（§5.7）", "M6",
      value_col="new_high_low", pct_x_col="nhl_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + industry.constituent",
      remark="定义：60 日新高−新低**归一净占比** = 100×(n_high−n_low)/(n_high+n_low)（%，−100~100）" + _SEP +
             "判读：>0 创新高者占优 = 中期动能健康；持续为负而行业指数走高 = 内在动能衰竭" + _SEP +
             _D_PIT + _SEP +
             "⚠ 计数类净额必须归一（§5.7 L2-a ③），不得用家数差——股票池扩缩会读成「年份」的函数"),
    D("SEC.BREADTH.RET_DISPERSION", "行业内收益离散度", "BREADTH", "state", "pct", "high_risk",
      "成分股 20 日收益横截面 std → 横截面分位(M1)", "M6",
      value_col="ret_dispersion", pct_x_col="ret_disp_xsec_pct", n_col="constituent_n",
      source="a_daily_quote + industry.constituent",
      remark="定义：行业内成分股 20 日收益的横截面标准差（M6 + std）" + _SEP +
             "判读：direction=high_risk（**高 = 分化大**）；BREADTH 维度内 sign=-1 翻转，高分位代表更集中" + _SEP +
             _D_PIT + _SEP +
             "⚠ 阶段 4 构念效度实测与维度分相关仅 0.160 <0.3，已登记为待移出成员（见 sector_validation_report.md §3）"),
    D("SEC.FLOW.MARGIN_NET", "行业两融净买入", "FLOW", "state", "yi_yuan", "high_good",
      "M6：成分股 rz_net 求和 / 1e8", "M6",
      value_col="margin_net", source="daily_margin_balance + industry.constituent",
      remark="定义：行业成分股两融净买入额求和 / 1e8（亿元，M6）" + _SEP +
             "判读：>0 杠杆资金净流入该行业；只作描述性读数，不作预测信号" + _SEP +
             "⚠ 源 daily_margin_balance.rz_net（元→亿元），按 PIT 成分映射" + _SEP +
             _D_PIT),
    D("SEC.FUND.ROE_MEDIAN", "行业ROE中位数", "FUND", "state", "pct", "high_good",
      "M6：按 announce_date ≤ t 取最新报告期（PIT），行业内取中位数", "M6",
      value_col="roe_median", pct_x_col="roe_xsec_pct", n_col="constituent_n",
      source="financial_indicator + industry.constituent",
      remark="定义：行业成分股 ROE 的 PIT 中位数（%，按 announce_date ≤ t 取最新报告期）" + _SEP +
             "判读：高 = 行业盈利能力相对强；FUNDAMENTAL 维度 4 成员之一（每成员 25% 权重）" + _SEP +
             _D_PIT + _SEP +
             "⚠ 财务为季频台阶，采用「月批次 + 次月 1 日生效」防前视，读数最多滞后一个月；fund_coverage<0.6 作废"),
    D("SEC.FUND.NP_YOY_MEDIAN", "行业净利同比中位数", "FUND", "state", "pct", "high_good",
      "PIT 最新报告期 net_profit_yoy 中位数", "M6",
      value_col="np_yoy_median", pct_x_col="npyoy_xsec_pct", n_col="constituent_n",
      source="financial_indicator + industry.constituent",
      remark="定义：行业成分股净利同比的 PIT 中位数（%）" + _SEP +
             "判读：>0 行业盈利整体扩张；与 REV_YOY_MEDIAN 联读区分「增收不增利」" + _SEP +
             _D_PIT + _SEP +
             "⚠ 极端值 winsorize [2,98] 只用于稳健估计参考分布，不对最终读数截尾"),
    D("SEC.FUND.REV_YOY_MEDIAN", "行业营收同比中位数", "FUND", "state", "pct", "high_good",
      "PIT 最新报告期 revenue_yoy 中位数", "M6",
      value_col="rev_yoy_median", pct_x_col="revyoy_xsec_pct", n_col="constituent_n",
      source="financial_indicator + industry.constituent",
      remark="定义：行业成分股营收同比的 PIT 中位数（%）" + _SEP +
             "判读：>0 行业需求端扩张；与 NP_YOY_MEDIAN 背离时提示利润率变化" + _SEP +
             _D_PIT),
    D("SEC.FUND.IMPROVEMENT_BREADTH", "行业盈利改善宽度", "FUND", "state", "pct", "high_good",
      "PIT 最新报告期 net_profit_yoy>0 的成分占比", "M6",
      value_col="improvement_breadth", pct_x_col="improve_xsec_pct", n_col="constituent_n",
      source="financial_indicator + industry.constituent",
      remark="定义：行业内净利同比为正的成分占比（0–100%，M6 count-if/n）" + _SEP +
             "判读：高 = 盈利改善是**普遍性**而非少数龙头拉动；与 NP_YOY_MEDIAN 联读区分「中位数被拉高」与「多数改善」" + _SEP +
             _D_PIT + _SEP + "⚠ 归一比率天然平稳（§5.7 L2-a ③），不随股票池扩缩漂移"),
    D("SEC.FUND.COVERAGE", "财务覆盖度", "FUND", "state", "ratio", "high_good",
      "有 ROE 的成分数 / 成分数", "M6",
      value_col="fund_coverage", source="financial_indicator + industry.constituent",
      remark="定义：有 ROE 的成分数 / PIT 有效成分数（0–1）" + _SEP +
             "判读：**门禁**——<0.6 时 FUNDAMENTAL 组全部读数作废并清库，不得用稀疏样本出中位数" + _SEP +
             _D_PIT + _SEP +
             "⚠ 0.6 沿用 market_state_daily._coverage_gate 的实测标定，不得随手调"),
    D("SEC.FUND.REVISION_BREADTH", "行业盈利修正宽度", "FUND", "state", "pct", "high_good",
      "M7：同财年 FY1 EPS 相邻快照差分（±0.5% 噪声），30快照滚动，按成分下沉到行业", "M7",
      value_col="revision_breadth", pct_x_col="revision_xsec_pct", n_col="revision_n",
      source="regime.analyst_forecast_snapshot + industry.constituent",
      remark="定义：行业盈利修正宽度 = (Σ上调−Σ下调)/Σ可比样本×100（M7，同财年 FY1 EPS 相邻快照差分 ±0.5% 噪声，30 快照滚动）" + _SEP +
             "判读：>0 分析师预期在上修；FUNDAMENTAL 维度 4 成员之一" + _SEP +
             _D_FC + _SEP +
             "⚠ 30 快照窗口尚未满时按实际 n 出值并写 revision_n + confidence=low；行业覆盖 <30% 或有效样本 <5 家 → NULL" + _SEP +
             "⚠ 阶段 4 构念效度实测载荷 0.156 <0.3，已登记为待移出成员（见 sector_validation_report.md §3）"),
    D("SEC.FUND.RATING_BULL_PCT", "行业评级乐观度", "FUND", "state", "pct", "high_good",
      "(rating_buy+rating_overweight)/五档合计，按成分汇总", "M0",
      value_col="rating_bull_pct", source="regime.analyst_forecast_snapshot + industry.constituent",
      remark="定义：(买入+增持)/评级五档合计，按 PIT 成分汇总（%）" + _SEP +
             "判读：🔴 判别力弱——§7.8 实测 99.3% 结构性集中在买入/增持，**仅作极端背离提示，不进维度分**" + _SEP +
             _D_FC),
    D("SEC.FUND.FORECAST_COVERAGE", "预期覆盖度", "FUND", "state", "ratio", "high_good",
      "有预期数据的成分数 / PIT 有效成分数", "M6",
      value_col="forecast_coverage", source="regime.analyst_forecast_snapshot + industry.constituent",
      remark="定义：有预期数据的成分数 / PIT 有效成分数（0–1）" + _SEP +
             "判读：**门禁**——<0.3 时 REVISION_BREADTH 不出值，避免用少数覆盖样本冒充行业一致预期" + _SEP +
             _D_FC),
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

# ── L3 业务指标字典（代码即字典；**开放集合**）────────────────────────────────
# 取代旧宽表：旧设计把 5 类业务读数压成一行的多个列，没有指标编码，无法各自
# 版本化 / 重算 / 增减。现按 biz_code 分行写 industry.business_value，定义同步
# 落 industry.business_dict。**新增或下线业务指标只改本字典，不加表字段。**
# ⚠ question / biz_name 是面向前端的展示文案，与 remark 一样不进 params_hash 之外
#   的口径计算（但 inputs / output_schema / kind / side 进口径指纹）。
BIZ_DEFS = {
    "SEC.BIZ.STATE_SIDE": dict(
        biz_name="状态侧热度",
        question="这个行业当前整体状态在同日所有行业中偏强还是偏弱？",
        side="state", kind="continuous",
        inputs=[{"kind": "dimension", "code": "SEC.DIM." + n}
                for n, c in DIMENSION_DEFS.items()
                if c["side"] == "state"
                and c["structure"] in ("single_anchor", "equal")],
        output_schema={"score": "0-100 状态侧热度 = 当期有分的状态侧维度等权均值；有效维度<2 为 NULL",
                       "state": "恒 NULL", "level": "恒 NULL",
                       "detail": "state_members / state_n / partial"},
        is_active=True),
    "SEC.BIZ.RISK_SIDE": dict(
        biz_name="风险侧水平",
        question="这个行业当前估值风险高不高？",
        side="risk", kind="continuous",
        inputs=[{"kind": "dimension", "code": "SEC.DIM.VALUATION"}],
        output_schema={"score": "0-100 风险侧水平 = VALUATION.score；高分 = 更贵",
                       "state": "恒 NULL", "level": "恒 NULL", "detail": "{}"},
        is_active=True),
    "SEC.BIZ.QUADRANT": dict(
        biz_name="状态象限",
        question="把状态与风险组合起来，当前属于哪一类局面？",
        side="neutral", kind="enum",
        inputs=[{"kind": "biz", "code": "SEC.BIZ.STATE_SIDE"},
                {"kind": "biz", "code": "SEC.BIZ.RISK_SIDE"}],
        output_schema={"score": "恒 NULL",
                       "state": "强势·未拥挤 / 强势·偏贵拥挤 / 冷门·未拥挤 / 弱势·偏贵拥挤 / NA；只描述当前状态，不含超配/低配/买卖含义",
                       "level": "恒 NULL", "detail": "state_side / risk_side"},
        is_active=True),
    "SEC.BIZ.RANK": dict(
        biz_name="当日状态排名",
        question="今天所有行业按状态排序，它排第几？共几个可比行业？",
        side="state", kind="rank",
        inputs=[{"kind": "biz", "code": "SEC.BIZ.STATE_SIDE"}],
        output_schema={"score": "恒 NULL", "state": "恒 NULL",
                       "level": "当日排名名次 1..rank_n（仅 applicable=true 有值）",
                       "detail": "rank_no / rank_n"},
        is_active=True),
    "SEC.BIZ.GATE": dict(
        biz_name="风险与可用性闸门",
        question="哪些原因让今天的结论需要打折看？",
        side="risk", kind="gate",
        inputs=[{"kind": "atom", "code": "SEC.CROWD.GATE"},
                {"kind": "dimension", "code": "SEC.DIM.CROWDING"}],
        output_schema={"score": "恒 NULL", "state": "恒 NULL", "level": "恒 NULL",
                       "detail": "gate_flags[] / crowd_gate / crowd_k / crowd_partial"},
        is_active=True),
}

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
    """L1 原子口径指纹：公式/单位/极性/模型/版本任一变化都会换指纹（§7.2 PIT 铁律）。

    ⚠ 本函数只用于 L1 原子。L2 / L3 用下面的 _dim_spec / _biz_spec，
      因为它们的口径还依赖「锚、成员、sign、L1 定义、闸门阈值」，旧实现把这些
      漏在 payload 之外，改了不换指纹会让历史行混存两套口径却显示同一版本号。
    """
    payload = "|".join([d["code"], d["unit"], d["direction"], d["formula"],
                        d["model_key"], CALIB_VERSION])
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


# ── L2 / L3 口径指纹（结构 + 上游 L1 定义 + 闸门阈值，全量纳入）────────────────
# 只纳入「影响数值」的字段。indicator_name / remark / is_active 属文案或启用开关，
# 不进指纹（与 remark 不触发重算的既有纪律一致）；成员启用状态变化时必然伴随
# formula / source / model_key 调整，会经 upstream 自动传导。
_NUMERIC_DEF_KEYS = ("code", "unit", "direction", "formula", "model_key",
                     "source", "freq", "value_col", "pct_x_col", "pct_t_col",
                     "n_col", "extra_kind")
_CROWD_FACTORS = ("SEC.CROWD.AMT_SHARE_PCT", "SEC.CROWD.TURNOVER_PCT",
                  "SEC.CROWD.VAL_PCT")


def _hash_spec(spec: dict) -> str:
    """规范化序列化后取 md5 前 16 位。纯函数，HK 侧复用。"""
    return hashlib.md5(json.dumps(spec, sort_keys=True, ensure_ascii=False,
                                  default=str).encode("utf-8")).hexdigest()[:16]


def _atom_spec(defs: list, code: str, hash_fn) -> dict:
    """某 L1 原子的口径规格：自身指纹 + 影响数值的定义字段。纯函数，HK 侧复用。"""
    d = next(x for x in defs if x["code"] == code)
    return {"l1_hash": hash_fn(d),
            "def": {k: d.get(k) for k in _NUMERIC_DEF_KEYS}}


def _dim_spec() -> dict:
    """CN 六维口径规格（纯函数，供单测验证「改锚 / 改成员 / 改 sign」的敏感性）。"""
    out = {}
    for name, cfg in DIMENSION_DEFS.items():
        codes = ([cfg["anchor"]] if cfg["anchor"] else
                 [c for c, _ in cfg.get("members", [])])
        if name == "CROWDING":
            codes = list(_CROWD_FACTORS)     # 规则判定维度无 anchor/members
        out[name] = {
            "layer": "L2", "code": "SEC.DIM." + name,
            "market": MARKET,
            "version": CALIB_VERSION,
            "structure": cfg["structure"], "side": cfg["side"],
            "anchor": cfg["anchor"],
            "members": [{"code": c, "sign": s}
                        for c, s in cfg.get("members", [])],
            "upstream": {c: _atom_spec(INDICATOR_DEFS, c, _params_hash)
                         for c in codes},
        }
    return out


def _dim_hashes() -> dict:
    """循环外预计算一次，避免每个行业日重复 json.dumps（热路径护栏）。"""
    return {n: _hash_spec(s) for n, s in _dim_spec().items()}


def _biz_spec(biz_code: str, dim_hashes: dict) -> dict:
    """CN L3 单指标口径规格：自身定义 + 六维指纹 + 闸门阈值 + 版本。

    ⚠ 每个 biz_code 独立出指纹 —— 改某一个业务指标的输入 / 输出契约 / 阈值，
      只换它自己的 params_hash，不牵连其它业务指标的历史行。
    """
    cfg = BIZ_DEFS[biz_code]
    return {
        "layer": "L3", "code": biz_code,
        "version": DICT_VERSION,
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
            "pit_start": str(PIT_START),
        },
    }


def _biz_hashes() -> dict:
    """循环外预计算一次：每个 active biz_code 一个独立指纹。"""
    dh = _dim_hashes()
    return {c: _hash_spec(_biz_spec(c, dh))
            for c, cfg in BIZ_DEFS.items() if cfg["is_active"]}


def _biz_params_hash(biz_code: str) -> str:
    return _hash_spec(_biz_spec(biz_code, _dim_hashes()))


def _hk_impl() -> dict:
    """HK 侧实现定义（延迟导入，避免与 sector_layer_hk 顶层 import 形成循环）。

    字典表只登记领域概念；两市场的公式 / 数据源 / 模型 / 启用状态 / 版本等
    实现差异全部落 params.per_market，由本函数提供 HK 部分。
    """
    import importlib
    hk = importlib.import_module("sector_layer_hk")
    return {
        "version": hk.CALIB_VERSION,
        "enabled_dimensions": list(hk.ENABLED_DIMENSIONS),
        "no_rank": True,
        "cap_start": str(hk.CAP_START),
        "pit_capable": False,
        "coverage_min": hk.COVERAGE_MIN,
        "valid_days_20_min": hk.VALID_DAYS_20_MIN,
        "valid_days_window": hk.VALID_DAYS_WINDOW,
        "crowd_pct_high": hk.CROWD_PCT_HIGH,
        "short_sample": hk.SHORT_SAMPLE,
        "min_periods": hk.MIN_PERIODS,
        "dimension_defs": {
            n: {k: c.get(k) for k in ("side", "structure", "anchor", "reason")}
               | {"members": [{"code": a, "sign": s} for a, s in c.get("members", [])]}
            for n, c in hk.HK_DIMENSION_DEFS.items()
        },
        "indicator_defs": {
            d["code"]: {k: d.get(k) for k in _NUMERIC_DEF_KEYS}
            for d in hk.HK_INDICATOR_DEFS
        },
        "biz_defs": {
            code: {"inputs": cfg["inputs"], "output_schema": cfg["output_schema"],
                   "is_active": cfg["is_active"]}
            for code, cfg in hk.HK_BIZ_DEFS.items()
        },
    }


def _cn_impl() -> dict:
    return {
        "version": CALIB_VERSION,
        "enabled_dimensions": list(ENABLED_DIMENSIONS),
        "no_rank": False,
        "pit_start": str(PIT_START),
        "pit_capable": True,
        "coverage_min": COVERAGE_MIN,
        "valid_days_20_min": VALID_DAYS_20_MIN,
        "valid_days_window": VALID_DAYS_WINDOW,
        "crowd_pct_high": CROWD_PCT_HIGH,
        "short_sample": SHORT_SAMPLE,
        "min_periods": MIN_PERIODS,
        "dimension_defs": {
            n: {k: c.get(k) for k in ("side", "structure", "anchor", "sign")}
               | {"members": [{"code": a, "sign": s} for a, s in c.get("members", [])]}
            for n, c in DIMENSION_DEFS.items()
        },
        "indicator_defs": {
            d["code"]: {k: d.get(k) for k in _NUMERIC_DEF_KEYS}
            for d in INDICATOR_DEFS
        },
        "biz_defs": {
            code: {"inputs": cfg["inputs"], "output_schema": cfg["output_schema"],
                   "is_active": cfg["is_active"]}
            for code, cfg in BIZ_DEFS.items()
        },
    }


def _per_market() -> dict:
    """一行一概念字典的 params.per_market：两市场实现差异的唯一登记处。"""
    return {"CN": _cn_impl(), "HK": _hk_impl()}


def _dict_spec(layer: str, code: str) -> dict:
    """统一口径指纹：概念定义 + 两市场实现差异，全市场共用一个 hash。

    L2 维度没有字典表（industry 层六维只存在于代码），但仍需统一指纹供
    `dimension_value.params_hash` 使用：concept 取 CN 侧维度定义，
    HK 侧结构差异由 per_market.HK.dimension_defs 纳入。
    """
    pm = _per_market()
    if layer == "L1":
        concept = next((d for d in INDICATOR_DEFS if d["code"] == code), None)
    elif layer == "L2":
        concept = DIMENSION_DEFS.get(code)
    else:
        concept = BIZ_DEFS.get(code)
    return {"layer": layer, "code": code, "version": DICT_VERSION,
            "concept": concept, "per_market": pm}


def _dict_hash(layer: str, code: str) -> str:
    return _hash_spec(_dict_spec(layer, code))


def _split_remark(remark: str) -> dict:
    """按 `_SEP` 拆三段式：定义 / 判读 / 其余（数据 / ⚠ / 口径）。"""
    out = {"定义": "", "判读": "", "其余": []}
    for part in (remark or "").split(_SEP):
        if part.startswith("定义："):
            out["定义"] = part[len("定义："):]
        elif part.startswith("判读："):
            out["判读"] = part[len("判读："):]
        elif part:
            out["其余"].append(part.removeprefix("数据："))
    return out


def _merged_indicator_remark(cn: dict | None, hk: dict | None) -> str:
    """同名 code 的 remark：定义 / 数据 / ⚠ 段并列两市场原文，判读段保留一份。

    措辞全部来自两市场现有字符串；`⚠ **两市场构造不同、数值不可直接比**`
    与 `**CN** = …；**HK** = …` 沿用 regime.SECTOR.UP_RATIO 的先例，不新造词。
    """
    if not cn:
        return hk["remark"]
    if not hk:
        return cn["remark"]
    c, h = _split_remark(cn["remark"]), _split_remark(hk["remark"])
    seg = ["⚠ **两市场构造不同、数值不可直接比**：**CN** = " + c["定义"] +
           "；**HK** = " + h["定义"]]
    if c["判读"] == h["判读"]:
        seg.append("判读：" + c["判读"])
    else:
        seg.append("判读：**CN** = " + c["判读"] + "；**HK** = " + h["判读"])
    seg.append("数据：**CN** = " + "；".join(c["其余"]) +
               "；**HK** = " + "；".join(h["其余"]))
    return _SEP.join(seg)


def _merged_formula(cn: dict | None, hk: dict | None) -> str:
    """同名 code 的 formula：`CN：…；HK：…`，照 regime.SECTOR.UP_RATIO 格式。"""
    if not cn:
        return hk["formula"]
    if not hk:
        return cn["formula"]
    return "CN：" + cn["formula"] + "；HK：" + hk["formula"]


def _indicator_dict_rows() -> list:
    """L1 字典：一行一个领域概念（CN 全集 ∪ HK 独有）。"""
    now = dt.datetime.now().astimezone()
    pm = _per_market()
    hk_defs = {d["code"]: d for d in
               importlib.import_module("sector_layer_hk").HK_INDICATOR_DEFS}
    codes = [d["code"] for d in INDICATOR_DEFS] + \
            [c for c in hk_defs if c not in {d["code"] for d in INDICATOR_DEFS}]
    rows = []
    for code in codes:
        cn = next((d for d in INDICATOR_DEFS if d["code"] == code), None)
        hk = hk_defs.get(code)
        base = cn or hk
        scope = "CN+HK" if cn and hk else ("CN" if cn else "HK")
        rows.append(dict(
            indicator_code=code, indicator_name=base["indicator_name"],
            domain=base["domain"], side=base["side"], market_scope=scope,
            unit=base["unit"], direction=base["direction"], freq=base["freq"],
            source=base["source"], formula=_merged_formula(cn, hk),
            model_key=base["model_key"],
            params={"version": DICT_VERSION, "per_market": {
                "CN": pm["CN"]["indicator_defs"].get(code),
                "HK": pm["HK"]["indicator_defs"].get(code)}},
            params_hash=_dict_hash("L1", code),
            min_periods=MIN_PERIODS if base["model_key"] == "M2" else 0,
            pit_capable=bool(cn), is_active=bool(cn and cn["is_active"]) or bool(hk and hk["is_active"]),
            remark=_merged_indicator_remark(cn, hk), updated_at=now))
    return rows


def _biz_dict_rows() -> list:
    """business_dict 种子：一行一个业务指标，市场差异落 params.per_market。"""
    now = dt.datetime.now().astimezone()
    pm = _per_market()
    return [dict(market_scope="CN+HK",
                 biz_code=code, biz_name=cfg["biz_name"],
                 question=cfg["question"], side=cfg["side"], kind=cfg["kind"],
                 inputs=cfg["inputs"],
                 params={"version": DICT_VERSION, "per_market": {
                     "CN": pm["CN"]["biz_defs"].get(code),
                     "HK": pm["HK"]["biz_defs"].get(code)}},
                 output_schema=cfg["output_schema"],
                 params_hash=_dict_hash("L3", code),
                 is_active=True,
                 remark="定义：" + cfg["biz_name"] + "　·　判读：" +
                        (cfg["question"] or "") +
                        "　·　⚠ **两市场构造不同、数值不可直接比**："
                        "**CN** = 见 industry_hotspot_data_architecture.md §6；"
                        "**HK** = HK 全期非 PIT / 置信度低",
                 updated_at=now)
            for code, cfg in BIZ_DEFS.items()]


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
    if coverage < COVERAGE_MIN or not pit_capable:
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
        lambda s: s.rolling(VALID_DAYS_WINDOW, min_periods=1).sum())

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
            extra["pit_capable"] = (d >= PIT_START) if "constituent" in meta["source"] else True
            rows.append(dict(
                market=MARKET,
                sector_code=rec["sw_code"], trade_date=d,
                indicator_code=meta["code"], raw_value=raw,
                percentile_xsec=px, percentile_ts=pt, n_sample=ns,
                coverage=None, quality=("short_sample" if ns is not None and
                                        meta["model_key"] == "M2" and ns < SHORT_SAMPLE
                                        else "ok"),
                model_version=_dict_hash("L1", meta["code"]), extra=extra,
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
    dim_hashes = {n: _dict_hash("L2", n) for n in DIMENSION_DEFS}  # 统一口径指纹
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
                market=MARKET,
                sector_code=rec["sw_code"], trade_date=d, dimension_code=name,
                side=cfg["side"], score=score, coverage=cov, dispersion=0.0,
                confidence=confidence_of(_dim_n_sample(rec, name), cov),
                members_used=members,
                params_hash=dim_hashes[name],
                extra=extra, updated_at=now))
    return rows


def _biz_rows(wide: pd.DataFrame, start: dt.date, end: dt.date) -> list:
    """L3 长表：一个行业日按 BIZ_DEFS 产出多行，每行一个 biz_code。

    公共质量元数据（coverage / applicable / confidence / extra）每行都带；
    多态列 score / state / level / detail 按 biz_code 各填各的，其余恒 NULL。
    """
    rows, now = [], dt.datetime.now().astimezone()
    hashes = {c: _dict_hash("L3", c) for c in BIZ_DEFS}  # 统一口径指纹
    for rec in wide.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        if not (start <= d <= end):
            continue
        cov = float(_get(rec, "coverage") or 0.0)
        c = rec.get("_crowd") or {}
        flags = []
        if cov < COVERAGE_MIN:
            flags.append("DATA_COVERAGE")
        # SHORT_SAMPLE 不是闸门：按设计稿 §3.6 实施期细化 ①，短样本只按
        # 「参与维度分的锚指标」判定；当前锚全部是横截面分位（不依赖时序样本），
        # 故不触发。短样本信息由 L1 quality='short_sample' 与 L2/L3
        # confidence='low' 承载，若写成闸门会让 applicable 恒为 false。
        if c.get("gate") in ("HIGH", "EXTREME"):
            flags.append("CROWDING")
        state_dims_n = sum(1 for cfg in DIMENSION_DEFS.values()
                           if cfg["side"] == "state"
                           and cfg["structure"] in ("single_anchor", "equal"))
        state_n = int(_get(rec, "state_n") or 0)
        if state_n < state_dims_n:
            flags.append("PARTIAL_STATE")   # 阶段 2 之前 / 成分数据缺失时状态侧不齐
        applicable = (cov >= COVERAGE_MIN
                      and int(_get(rec, "valid_days_20") or 0) >= VALID_DAYS_20_MIN
                      and "DATA_COVERAGE" not in flags)
        partial = "PARTIAL_STATE" in flags
        state_side = _get(rec, "state_side")
        risk_side = _get(rec, "risk_side")
        quadrant = rec.get("quadrant") or "NA"
        rank_no = _get(rec, "rank") if applicable else None
        rank_n = _clean_value(rec.get("rank_n"))
        common = dict(
            market=MARKET,
            sector_code=rec["sw_code"], trade_date=d,
            coverage=cov, applicable=bool(applicable),
            confidence=confidence_of(_get(rec, "pe_n_sample"), cov),
            extra=dict(enabled_dimensions=list(ENABLED_DIMENSIONS),
                       calib_version=CALIB_VERSION,
                       pit_capable=(d >= PIT_START),
                       rank_n=rank_n),
            updated_at=now)
        for biz_code, h in hashes.items():
            score = state = level = None
            detail = {}
            if biz_code == "SEC.BIZ.STATE_SIDE":
                score = state_side
                detail = dict(state_members=rec["state_members"],
                              state_n=state_n, partial=partial)
            elif biz_code == "SEC.BIZ.RISK_SIDE":
                score = risk_side
            elif biz_code == "SEC.BIZ.QUADRANT":
                state = quadrant
                detail = dict(state_side=state_side, risk_side=risk_side)
            elif biz_code == "SEC.BIZ.RANK":
                level = int(rank_no) if rank_no is not None else None
                detail = dict(rank_no=level, rank_n=rank_n)
            elif biz_code == "SEC.BIZ.GATE":
                detail = dict(gate_flags=flags, crowd_gate=c.get("gate"),
                              crowd_k=c.get("k"), crowd_partial=c.get("partial"))
            rows.append(dict(common, biz_code=biz_code, score=score,
                             state=state, level=level, detail=detail,
                             params_hash=h))
    return rows


# ── 写库 / 字典同步 ────────────────────────────────────────────────────────
# L1/L2 的实体主键（不含 biz_code）；L3 长表主键另加 biz_code。
# ⚠ 不要混用：给 L1/L2 的 conflict_cols 加 biz_code 会因列不存在直接报错。
PK_COLS = ["market", "sector_code", "trade_date"]
BIZ_PK_COLS = ["biz_code"] + PK_COLS


def _write_batched(conn, table: str, rows: list, conflict_cols: list) -> int:
    """R6：单事务 ≤2000 行，逐批 commit。"""
    rows = _clean_rows(rows)
    for k in range(0, len(rows), WRITE_BATCH):
        bulk_upsert(conn, table, rows[k:k + WRITE_BATCH], conflict_cols=conflict_cols)
        conn.commit()
    return len(rows)


def sync_dict(conn) -> int:
    """两张字典表幂等 upsert：一行一个领域概念，不区分 CN / HK。

    口径唯一真源在 INDICATOR_DEFS / BIZ_DEFS；两市场实现差异由
    `_per_market()` 落 params.per_market，由 `_dict_hash()` 统一指纹。
    CN / HK 两侧 run() 调本函数写成同一批行（幂等，不互相覆盖）。
    """
    rows = _indicator_dict_rows()
    bulk_upsert(conn, "industry.indicator_dict", _clean_rows(rows),
                conflict_cols=["indicator_code"])
    bulk_upsert(conn, "industry.business_dict", _clean_rows(_biz_dict_rows()),
                conflict_cols=["biz_code"])
    conn.commit()
    return len(rows)


def _default_window(conn) -> tuple[dt.date, dt.date]:
    """增量窗口：取输出表最新日 -3 天（迟到/修订自愈）；无输出则最近 45 天。"""
    end = dt.date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM industry.business_value WHERE market=%s",
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
                            conn, "industry.indicator_value", iv,
                            PK_COLS + ["indicator_code"])
                        n_sc += _write_batched(
                            conn, "industry.dimension_value", sc,
                            PK_COLS + ["dimension_code"])
                        n_bz += _write_batched(
                            conn, "industry.business_value", bz, BIZ_PK_COLS)
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
