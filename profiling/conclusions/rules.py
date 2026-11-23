#!/usr/bin/env python3
"""个股画像 L2/L3 结论的**唯一可执行规则真源**（doc/stock_profile_l2_l3.md v1.1）。

本模块是纯数据 + 纯函数，零 IO（除 sync_conclusion_dict 的写字典动作）。
- RULES：6 个结论的 inputs / derived_fields / params / rules / output_schema，
  已按 §4.3.1「规则表 → RULES 投影约定」展开为**字面量条件**，不含参数名或表达式。
- conclusion_dict 是 RULES 的**只读发布物**：由 sync_conclusion_dict() 写进
  profile.tag_registry.conclusion_dict，并校验 version 一致。
- 判定只读内存 RULES，绝不读 JSONB，避免双真相源。

切点数值见 doc §5.1（20/80 沿用 L1 等频五分档档界）。v2 起估值结论
改为「双分位各自切三档 → 3×3 交叉」，废弃点差门槛。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

_DAILY = "daily"
_QUARTERLY = "quarterly"

VAL_CLASS = {
    "rel_cheap": "cheap",
    # D22：原 hist_cheap_ind_expensive（自身历史便宜但同行贵）由 cheap 改归 neutral，
    # 与其余 4 个单侧格一致——单侧信号不足以支撑 L3 的贵贱/陷阱判断（§5.1/§6.1）
    "hist_cheap_ind_expensive": "neutral",
    "rel_fair": "neutral", "hist_expensive_ind_cheap": "neutral",
    # 3×3 矩阵的 4 个单侧格统一归中性类：单侧信号不足以支撑 L3 的贵贱判断，
    # 且 §6.1 中「中性类 × 质量偏弱」整行不发布，避免误产出「盈利弱价格低」的交叉判断
    "hist_cheap_ind_neutral": "neutral",
    "hist_expensive_ind_neutral": "neutral",
    "hist_neutral_ind_cheap": "neutral",
    "hist_neutral_ind_expensive": "neutral",
    "rel_expensive": "expensive",
    "loss": "no_anchor", "negative": "no_anchor",
}
QAL_CLASS = {
    "solid": "solid", "high_but_volatile": "volatile",
    "moderate": "moderate",   # D22：单开中等档，不再挤进 weak
    "paper_profit": "weak", "leveraged": "weak",
    "weak": "weak", "deteriorating": "weak",
}

# ── 通用参数（各结论可覆盖）──────────────────────────────────────────
_COMMON = {"coverage_min": 0.6}


def _cond(field: str, op: str, value: Any) -> dict:
    return {"field": field, "op": op, "value": value}


def _all(*items) -> dict:
    return {"all": list(items)}


RULES: dict[str, dict] = {
    # ── §5.1 估值 ────────────────────────────────────────────────
    "dim_valuation_conclusion": {
        "version": "v4",   # D20：行业分位换二级 + 档位派生；v3=报告期自适应
        "update_freq": _DAILY,
        "inputs": [
            {"field": "pe_hist_pct", "tag_code": "val_pe_hist_pct",
             "source_col": "num_value", "dtype": "float", "role": "anchor"},
            # D20：行业分位改用二级（val_pe_ind2_pct），原 val_pe_ind_pct 已废弃
            {"field": "pe_ind_pct", "tag_code": "val_pe_ind2_pct",
             "source_col": "num_value", "dtype": "float", "role": "anchor"},
            {"field": "loss_flag", "tag_code": "val_pe_ttm_tier",
             "source_col": "key_value", "dtype": "bool", "match": "LOSS", "role": "flag"},
            {"field": "negative_flag", "tag_code": "val_pb_tier",
             "source_col": "key_value", "dtype": "bool", "match": "NEGATIVE", "role": "flag"},
        ],
        # v4（D20）：加两个档位派生字段，规则条件从范围比较改为枚举比较（条数不变）。
        # 两个百分位是序数排名、相减无经济含义，故仍然没有点差派生字段。
        "derived_fields": [
            {"field": "hist_tier", "expr": "tier_of(pe_hist_pct, 20, 80)",
             "source": "derived:tier", "values": ["cheap", "mid", "expensive"]},
            {"field": "ind_tier", "expr": "tier_of(pe_ind_pct, 20, 80)",
             "source": "derived:tier", "values": ["cheap", "mid", "expensive"]},
        ],
        "params": {**_COMMON, "cheap_pctl": 20, "expensive_pctl": 80},
        "g4_exempt": True,  # loss_flag / negative_flag 命中即发布，豁免 G1/G2
        "rules": [
            {"rule_id": "VAL-00", "priority": 0,
             "conditions": _all(_cond("loss_flag", "==", True)), "result": "loss"},
            {"rule_id": "VAL-01", "priority": 1,
             "conditions": _all(_cond("negative_flag", "==", True)), "result": "negative"},
            # D20：条件改为档位枚举比较（hist_tier/ind_tier ∈ cheap/mid/expensive），
            # 由 _derive() 按 cheap_pctl/expensive_pctl 切档；条数与结果码不变。
            # 对角两格：两个参照系方向相反
            {"rule_id": "VAL-02", "priority": 10,
             "conditions": _all(_cond("hist_tier", "==", "cheap"),
                                _cond("ind_tier", "==", "expensive")),
             "result": "hist_cheap_ind_expensive"},
            {"rule_id": "VAL-03", "priority": 11,
             "conditions": _all(_cond("hist_tier", "==", "expensive"),
                                _cond("ind_tier", "==", "cheap")),
             "result": "hist_expensive_ind_cheap"},
            # 同向两格：双分位共振
            {"rule_id": "VAL-04", "priority": 20,
             "conditions": _all(_cond("hist_tier", "==", "cheap"),
                                _cond("ind_tier", "==", "cheap")), "result": "rel_cheap"},
            {"rule_id": "VAL-05", "priority": 21,
             "conditions": _all(_cond("hist_tier", "==", "expensive"),
                                _cond("ind_tier", "==", "expensive")), "result": "rel_expensive"},
            # 单侧四格：一侧有明确信号、另一侧中性，各自单列，不压成「相对中性」
            {"rule_id": "VAL-07", "priority": 30,
             "conditions": _all(_cond("hist_tier", "==", "cheap"),
                                _cond("ind_tier", "==", "mid")),
             "result": "hist_cheap_ind_neutral"},
            {"rule_id": "VAL-08", "priority": 31,
             "conditions": _all(_cond("hist_tier", "==", "expensive"),
                                _cond("ind_tier", "==", "mid")),
             "result": "hist_expensive_ind_neutral"},
            {"rule_id": "VAL-09", "priority": 32,
             "conditions": _all(_cond("hist_tier", "==", "mid"),
                                _cond("ind_tier", "==", "cheap")),
             "result": "hist_neutral_ind_cheap"},
            {"rule_id": "VAL-10", "priority": 33,
             "conditions": _all(_cond("hist_tier", "==", "mid"),
                                _cond("ind_tier", "==", "expensive")),
             "result": "hist_neutral_ind_expensive"},
            {"rule_id": "VAL-06", "priority": 99, "conditions": _all(), "result": "rel_fair"},
        ],
        "output_schema": {
            "key_value_semantics": "相对估值位置的枚举类型；不是绝对贵贱、不是预测",
            "value_codes": {"rel_cheap": "相对偏便宜", "loss": "亏损（无 PE 估值锚）"},
        },
    },

    # ── §5.2 盈利质量 ─────────────────────────────────────────────
    "dim_quality_conclusion": {
        "version": "v3",   # D21：一级行业口径 + 7 码 + 杠杆降级 + ROE 绝对底线
        "update_freq": _QUARTERLY,
        "inputs": [
            {"field": "roe_tier", "tag_code": "qal_roe_ind1_tier",
             "source_col": "key_value", "dtype": "int", "invalid": ["LOSS"], "role": "anchor"},
            {"field": "roe_stability", "tag_code": "qal_roe_stability_ind1",
             "source_col": "key_value", "dtype": "int"},
            {"field": "earnings_quality_tier", "tag_code": "qal_earnings_quality_ind1",
             "source_col": "key_value", "dtype": "int", "invalid": ["LOSS"], "role": "anchor"},
            {"field": "leverage_tier", "tag_code": "qal_leverage_ind1_tier",
             "source_col": "key_value", "dtype": "int"},
            {"field": "roe_raw", "tag_code": "qal_roe_ind1_tier",
             "source_col": "num_value", "dtype": "float"},
        ],
        # D21 §5.2：两个主锚（ROE 档 / 现金转化档）至少一个有效即可判定
        "anchor_any": True,
        "derived_fields": [],
        "params": {**_COMMON, "stable_max": 2, "volatile_min": 4,
                   "quality_weak_max": 2, "quality_ok_min": 3,
                   "leverage_high": 4, "roe_high": 4,
                   "moderate_min": 3, "moderate_max": 4, "roe_floor_pct": 5},
        "rules": [
            {"rule_id": "QAL-01", "priority": 0,
             "conditions": _all(_cond("roe_stability", ">=", 4),
                                _cond("earnings_quality_tier", "<=", 2)),
             "result": "deteriorating"},
            {"rule_id": "QAL-02", "priority": 1,
             "conditions": _all(_cond("earnings_quality_tier", "<=", 2)),
             "result": "paper_profit"},
            {"rule_id": "QAL-04", "priority": 10,
             "conditions": _all(_cond("roe_tier", ">=", 4),
                                _cond("roe_stability", "<=", 2),
                                _cond("earnings_quality_tier", ">=", 3),
                                _cond("roe_raw", ">=", 5)),
             "result": "solid"},
            {"rule_id": "QAL-05", "priority": 11,
             "conditions": _all(_cond("roe_tier", ">=", 4),
                                _cond("roe_raw", ">=", 5)),
             "result": "high_but_volatile"},
            {"rule_id": "QAL-03", "priority": 12,
             "conditions": _all(_cond("leverage_tier", ">=", 4),
                                _cond("roe_tier", "<", 4)),
             "result": "leveraged"},
            {"rule_id": "QAL-07", "priority": 20,
             # §5.2 中文写「3-4 档」：上限含 4 档，使 ROE 绝对值未达底线的高档票
             # 落 moderate 而非 weak（满足「低于 5% 最高只能评盈利中等」）
             "conditions": _all(_cond("roe_tier", ">=", 3),
                                _cond("roe_tier", "<=", 4)),
             "result": "moderate"},
            {"rule_id": "QAL-06", "priority": 99, "conditions": _all(), "result": "weak"},
        ],
        "output_schema": {
            "key_value_semantics": "盈利水平/ROE 波动/现金转化/杠杆的财报特征，不评价证券好坏",
            "value_codes": {"solid": "盈利稳且现金实", "moderate": "盈利水平中等"},
        },
    },

    # ── §5.3 成长 ────────────────────────────────────────────────
    "dim_growth_conclusion": {
        "version": "v4",   # 输入切一级行业口径(v3) + GRW-02 去掉 accel=='steady'(v4)
        "update_freq": _QUARTERLY,
        "inputs": [
            {"field": "rev_tier", "tag_code": "grw_revenue_yoy_ind1_tier",
             "source_col": "key_value", "dtype": "int", "role": "anchor"},
            {"field": "np_tier", "tag_code": "grw_profit_yoy_ind1_tier",
             "source_col": "key_value", "dtype": "int"},
            {"field": "rev_yoy", "tag_code": "grw_revenue_yoy_ind1_tier",
             "source_col": "num_value", "dtype": "float"},
            {"field": "np_yoy", "tag_code": "grw_profit_yoy_ind1_tier",
             "source_col": "num_value", "dtype": "float"},
            {"field": "persistence", "tag_code": "grw_growth_persistence",
             "source_col": "key_value", "dtype": "int"},
            {"field": "accel", "tag_code": "grw_growth_accel",
             "source_col": "key_value", "dtype": "enum",
             "codes": ["accelerating", "steady", "decelerating"]},
        ],
        "derived_fields": [
            {"field": "growth_gap", "expr": "abs(rev_tier - np_tier)", "dtype": "int"},
        ],
        "params": {**_COMMON, "divergence_gap_tier": 2,
                   "high_growth_min": 4, "persistence_min": 2},
        "rules": [
            {"rule_id": "GRW-00", "priority": 0,
             "conditions": _all(_cond("growth_gap", ">=", 2)),
             "result": "revenue_profit_divergent"},
            {"rule_id": "GRW-01", "priority": 1,
             "conditions": {"any": [_cond("rev_yoy", "<", 0), _cond("np_yoy", "<", 0)]},
             "result": "contracting"},
            {"rule_id": "GRW-02", "priority": 10,
             # 加速度不是「持续高增长」的必要条件：加速/放缓中的高增长公司仍属此类。
             # 真实数据下 steady 仅 19%，把它设为必要条件会让本档只占 1.7%。
             "conditions": _all(_cond("rev_tier", ">=", 4), _cond("np_tier", ">=", 4),
                                _cond("persistence", ">=", 2)),
             "result": "fast_sustained"},
            {"rule_id": "GRW-03", "priority": 11,
             "conditions": _all(_cond("rev_tier", ">=", 4),
                                _cond("accel", "==", "accelerating")),
             "result": "fast_accelerating"},
            {"rule_id": "GRW-04", "priority": 12,
             "conditions": _all(_cond("rev_tier", ">=", 4),
                                _cond("accel", "==", "decelerating")),
             "result": "fast_decelerating"},
            {"rule_id": "GRW-05", "priority": 99, "conditions": _all(), "result": "slow_steady"},
        ],
        "output_schema": {
            "key_value_semantics": "增速/持续性/加速度的现状判断，非未来增速预测",
            "value_codes": {"fast_sustained": "高增速且持续", "contracting": "负增长"},
        },
    },

    # ── §5.4 当前位置与状态 ────────────────────────────────────────
    "dim_position_state": {
        "version": "v3",   # 六档区分度整改：POS-01 去 bullish 三腿合取、POS-04 去 position==3
        "update_freq": _DAILY,
        "inputs": [
            {"field": "position_tier", "tag_code": "tst_price_position_52w",
             "source_col": "key_value", "dtype": "int", "role": "anchor"},
            {"field": "momentum_tier", "tag_code": "trd_momentum_12_1",
             "source_col": "key_value", "dtype": "int"},
            {"field": "volume_surge", "tag_code": "att_volume_surge",
             "source_col": "key_value", "dtype": "enum",
             "codes": ["surge", "normal", "shrink"]},
            # 复用 L1 已算好的「当日量 / 近 60 日均量」倍数，不另起现算
            {"field": "volume_ratio_60", "tag_code": "att_volume_surge",
             "source_col": "num_value", "dtype": "float"},
            # 由 v_daily_quote.close 的 rolling(20)/rolling(60) 现算（视图无 MA 列）
            {"field": "ma_alignment", "source": "v_daily_quote",
             "compute": "ma_alignment", "dtype": "enum",
             "codes": ["bullish", "neutral", "bearish"]},
        ],
        "derived_fields": [],
        "params": {**_COMMON, "low_pos_max": 2, "high_pos_min": 4,
                   "vol_quiet_max": 0.5, "vol_active_min": 2.0, "weak_momentum_max": 2},
        "rules": [
            {"rule_id": "POS-00", "priority": 0,
             "conditions": _all(_cond("position_tier", "<=", 2),
                                _cond("volume_ratio_60", "<=", 0.5)), "result": "low_quiet"},
            {"rule_id": "POS-01", "priority": 1,
             # 52 周低位与均线多头天然互斥（实测 (pos=1,bullish) 仅 49/6044），
             # 三腿合取让本档只有 0.27%。去掉 bullish，label「低位放量」本身无方向暗示。
             "conditions": _all(_cond("position_tier", "<=", 2),
                                _cond("volume_ratio_60", ">=", 2.0)),
             "result": "low_volume_start"},
            {"rule_id": "POS-02", "priority": 2,
             "conditions": _all(_cond("position_tier", ">=", 4),
                                _cond("volume_ratio_60", ">=", 2.0)), "result": "high_overheat"},
            {"rule_id": "POS-03", "priority": 3,
             "conditions": _all(_cond("ma_alignment", "==", "bearish"),
                                _cond("momentum_tier", "<=", 2)),
             "result": "downtrend_unresolved"},
            {"rule_id": "POS-04", "priority": 4,
             # 原 position_tier==3 精确单档过严（仅 1.14%），均线多头排列本身即可作为
             # 滞后描述；去掉后 5.18%，仍不预测方向。
             "conditions": _all(_cond("ma_alignment", "==", "bullish")),
             "result": "trend_continuation"},
            {"rule_id": "POS-05", "priority": 99, "conditions": _all(), "result": "range_bound"},
        ],
        "output_schema": {
            "key_value_semantics": "描述性滞后状态，不是方向预测、不构成买卖建议",
            "value_codes": {"low_quiet": "低位缩量", "range_bound": "区间震荡"},
        },
    },

    # ── §6.1 好公司 × 好价格 ───────────────────────────────────────
    "biz_value_quality_type": {
        "version": "v3",   # D22：质量侧四档 + 单侧估值归中性 + 3 个 moderate cell
        "update_freq": _DAILY,
        "inputs": [
            {"field": "valuation_class", "source": "derived", "compute": "val_class",
             "dtype": "enum", "codes": ["cheap", "neutral", "expensive", "no_anchor"]},
            {"field": "quality_class", "source": "derived", "compute": "qal_class",
             "dtype": "enum", "codes": ["solid", "volatile", "moderate", "weak"]},
        ],
        "derived_fields": [],
        "params": {**_COMMON},
        "rules": [
            {"rule_id": "VQ-00", "priority": 0,
             "conditions": _all(_cond("valuation_class", "==", "no_anchor")), "result": None},
            {"rule_id": "VQ-C1", "priority": 10,
             "conditions": _all(_cond("valuation_class", "==", "cheap"),
                                _cond("quality_class", "==", "solid")),
             "result": "quality_rel_cheap"},
            {"rule_id": "VQ-C1M", "priority": 11,
             "conditions": _all(_cond("valuation_class", "==", "cheap"),
                                _cond("quality_class", "==", "moderate")),
             "result": "moderate_rel_cheap"},
            {"rule_id": "VQ-C2", "priority": 12,
             "conditions": _all(_cond("valuation_class", "==", "cheap"),
                                _cond("quality_class", "==", "volatile")),
             "result": "value_trap_risk"},
            {"rule_id": "VQ-C3", "priority": 13,
             "conditions": _all(_cond("valuation_class", "==", "cheap"),
                                _cond("quality_class", "==", "weak")),
             "result": "value_trap_risk"},
            {"rule_id": "VQ-C4", "priority": 20,
             "conditions": _all(_cond("valuation_class", "==", "neutral"),
                                _cond("quality_class", "==", "solid")),
             "result": "quality_rel_fair"},
            {"rule_id": "VQ-C4M", "priority": 21,
             "conditions": _all(_cond("valuation_class", "==", "neutral"),
                                _cond("quality_class", "==", "moderate")),
             "result": "moderate_rel_fair"},
            {"rule_id": "VQ-C6", "priority": 30,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "solid")),
             "result": "quality_rel_expensive"},
            {"rule_id": "VQ-C6M", "priority": 31,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "moderate")),
             "result": "moderate_rel_expensive"},
            {"rule_id": "VQ-C8", "priority": 32,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "volatile")),
             "result": "weak_and_expensive"},
            {"rule_id": "VQ-C9", "priority": 33,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "weak")),
             "result": "weak_and_expensive"},
            # neutral × (volatile|weak) 及所有未列 cell：宁可不出档
            {"rule_id": "VQ-99", "priority": 99, "conditions": _all(), "result": None},
        ],
        "output_schema": {
            "key_value_semantics": "估值 × 盈利质量的交叉类型；result=None 表示整行不发布，不是 enum 码",
            "value_codes": {"value_trap_risk": "盈利弱价格低"},
        },
    },

    # ── §6.2 增长质量 ─────────────────────────────────────────────
    "biz_growth_quality_type": {
        "version": "v3",   # 修 FCF 取数 + 现金转化档切 ind1 + GQ-04 精细化门禁
        "update_freq": _QUARTERLY,
        "inputs": [
            # 主锚：L2 未出档时整行不发布，否则 GQ-04 兜底会把数据不足的票
            # 误判成 sustained_cash_backed（违反 §4.2「宁可不出档」）
            {"field": "growth_code", "source": "derived", "compute": "l2_growth",
             "dtype": "enum", "role": "anchor",
             "codes": ["fast_sustained", "fast_accelerating", "fast_decelerating",
                       "slow_steady", "contracting", "revenue_profit_divergent"]},
            # D22：切一级行业口径，与 §5.2 质量结论同源（旧 qal_earnings_quality 已停更）
            {"field": "earnings_quality_tier", "tag_code": "qal_earnings_quality_ind1",
             "source_col": "key_value", "dtype": "int", "invalid": ["LOSS"]},
            # 不设 anchor：GQ-00/01/03 不依赖 FCF，不能被门禁无差别拦掉。
            # 精细化门禁只放在 GQ-04 兜底前（GQ-99），见下方规则。
            {"field": "fcf_sign", "source": "financial_indicator",
             "column": "free_cash_flow", "dtype": "int", "codes": [-1, 0, 1]},
            {"field": "persistence", "tag_code": "grw_growth_persistence",
             "source_col": "key_value", "dtype": "int"},
            {"field": "accel", "tag_code": "grw_growth_accel",
             "source_col": "key_value", "dtype": "enum",
             "codes": ["accelerating", "steady", "decelerating"]},
        ],
        "derived_fields": [],
        "params": {**_COMMON, "quality_weak_max": 2},
        "rules": [
            {"rule_id": "GQ-00", "priority": 0,
             "conditions": _all(_cond("growth_code", "==", "contracting")),
             "result": "contraction"},
            {"rule_id": "GQ-01", "priority": 1,
             "conditions": _all(_cond("earnings_quality_tier", "<=", 2)),
             "result": "paper_growth"},
            {"rule_id": "GQ-02", "priority": 2,
             "conditions": _all(_cond("fcf_sign", "<", 0),
                                _cond("growth_code", "in",
                                      ["fast_sustained", "fast_accelerating"])),
             "result": "cash_burning_expansion"},
            {"rule_id": "GQ-03", "priority": 3,
             "conditions": _all(_cond("persistence", "<=", 1),
                                _cond("accel", "==", "accelerating"),
                                _cond("growth_code", "in",
                                      ["fast_accelerating", "fast_sustained"])),
             "result": "one_off_like"},
            # 精细化门禁：正面码必须有非负 FCF 实证（_compare 对 None 返回 False，
            # 故 FCF 缺失同样落不进 GQ-04）。GQ-00/01/02/03 优先级更高，不受影响。
            {"rule_id": "GQ-04", "priority": 98,
             "conditions": _all(_cond("fcf_sign", ">=", 0)),
             "result": "sustained_cash_backed"},
            {"rule_id": "GQ-99", "priority": 99, "conditions": _all(),
             "result": None},
        ],
        "output_schema": {
            "key_value_semantics": "增长 × 现金转化的交叉类型，现状判断而非可持续性预测",
            "value_codes": {"sustained_cash_backed": "增长持续且现金支撑"},
        },
    },
}

DERIVED_TAGS = tuple(RULES)
L2_TAGS = ("dim_valuation_conclusion", "dim_quality_conclusion",
           "dim_growth_conclusion", "dim_position_state")
L3_TAGS = ("biz_value_quality_type", "biz_growth_quality_type")

_OPS = {"<=", "<", ">=", ">", "==", "in"}


def _compare(left: Any, op: str, right: Any) -> bool:
    if left is None:
        return False
    if op == "==":
        return left == right
    if op == "in":
        return left in right
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    raise ValueError(f"不支持的算子: {op}")


def eval_condition(cond: dict, values: dict) -> bool:
    """结构化条件求值：组合子 all/any/not + 叶子 {field, op, value}（value 只能是字面量）。"""
    if "all" in cond:
        return all(eval_condition(c, values) for c in cond["all"])
    if "any" in cond:
        return any(eval_condition(c, values) for c in cond["any"])
    if "not" in cond:
        return not eval_condition(cond["not"], values)
    return _compare(values.get(cond["field"]), cond["op"], cond["value"])


def first_match(spec: dict, values: dict) -> tuple[str, Any]:
    """按 priority 升序取首个全匹配规则，返回 (rule_id, result)。"""
    for rule in sorted(spec["rules"], key=lambda r: r["priority"]):
        if eval_condition(rule["conditions"], values):
            return rule["rule_id"], rule["result"]
    raise RuntimeError("规则表缺少 priority=99 兜底规则")


def validate_rules() -> None:
    """冻结 schema 自检（供 sync / 测试调用）：field 白名单、算子、字面量、兜底。"""
    for code, spec in RULES.items():
        fields = {i["field"] for i in spec["inputs"]}
        fields |= {d["field"] for d in spec["derived_fields"]}
        priorities = [r["priority"] for r in spec["rules"]]
        assert 99 in priorities, f"{code} 缺 priority=99 兜底"
        assert len(priorities) == len(set(priorities)), f"{code} priority 重复"
        for rule in spec["rules"]:
            stack = [rule["conditions"]]
            while stack:
                node = stack.pop()
                if "all" in node:
                    stack.extend(node["all"])
                elif "any" in node:
                    stack.extend(node["any"])
                elif "not" in node:
                    stack.append(node["not"])
                else:
                    assert node["field"] in fields, f"{code}.{rule['rule_id']} 未知 field"
                    assert node["op"] in _OPS, f"{code}.{rule['rule_id']} 非法算子"
                    assert not isinstance(node["value"], dict), "条件 value 必须是字面量"
        # 字面值必须与 params 一致（防双真相源）
        if "params" in spec:
            assert spec["params"]["coverage_min"] == _COMMON["coverage_min"]


def sync_conclusion_dict(conn) -> int:
    """把 RULES 序列化写入 tag_registry.conclusion_dict（只读发布物）。

    - 必须在 sync_registry() 之后调用（结论标签需已在字典中）；
    - version 不一致直接抛错，避免「标签 active 但规则契约是旧版」；
    - 不给 TagMeta 加字段，故 sync_registry() 的全列 upsert 不会覆盖本列。
    """
    validate_rules()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tag_code, version FROM profile.tag_registry WHERE tag_code = ANY(%s)",
            (list(RULES),),
        )
        versions = dict(cur.fetchall())
        missing = [c for c in RULES if c not in versions]
        if missing:
            raise RuntimeError(f"结论标签未注册，请先执行 sync_registry(): {missing}")
        for code, spec in RULES.items():
            if versions[code] != spec["version"]:
                raise RuntimeError(
                    f"口径版本不一致 {code}: tag_registry={versions[code]} RULES={spec['version']}；"
                    "请 bump @tag(version) 并全量重建历史结论"
                )
            payload = {k: v for k, v in spec.items() if k != "version"}
            cur.execute(
                "UPDATE profile.tag_registry SET conclusion_dict = %s, updated_at = NOW() "
                "WHERE tag_code = %s",
                (Json(payload), code),
            )
    return len(RULES)
