#!/usr/bin/env python3
"""个股画像 L2/L3 结论的**唯一可执行规则真源**（doc/stock_profile_l2_l3.md v1.0）。

本模块是纯数据 + 纯函数，零 IO（除 sync_conclusion_dict 的写字典动作）。
- RULES：6 个结论的 inputs / derived_fields / params / rules / output_schema，
  已按 §4.3.1「规则表 → RULES 投影约定」展开为**字面量条件**，不含参数名或表达式。
- conclusion_dict 是 RULES 的**只读发布物**：由 sync_conclusion_dict() 写进
  profile.tag_registry.conclusion_dict，并校验 version 一致。
- 判定只读内存 RULES，绝不读 JSONB，避免双真相源。

切点数值见 doc §12.3.2（2026-11-12 本机实测标定后冻结，无需调整）。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

_DAILY = "daily"
_QUARTERLY = "quarterly"

VAL_CLASS = {
    "rel_cheap": "cheap", "hist_cheap_ind_expensive": "cheap",
    "rel_fair": "neutral", "hist_expensive_ind_cheap": "neutral",
    "rel_expensive": "expensive",
    "loss": "no_anchor", "negative": "no_anchor",
}
QAL_CLASS = {
    "solid": "solid", "high_but_volatile": "volatile",
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
        "version": "v1",
        "update_freq": _DAILY,
        "inputs": [
            {"field": "pe_hist_pct", "tag_code": "val_pe_hist_pct",
             "source_col": "num_value", "dtype": "float", "role": "anchor"},
            {"field": "pe_ind_pct", "tag_code": "val_pe_ind_pct",
             "source_col": "num_value", "dtype": "float", "role": "anchor"},
            {"field": "loss_flag", "tag_code": "val_pe_ttm_tier",
             "source_col": "key_value", "dtype": "bool", "match": "LOSS", "role": "flag"},
            {"field": "negative_flag", "tag_code": "val_pb_tier",
             "source_col": "key_value", "dtype": "bool", "match": "NEGATIVE", "role": "flag"},
        ],
        "derived_fields": [
            {"field": "pe_gap", "expr": "abs(pe_hist_pct - pe_ind_pct)", "dtype": "float"},
            {"field": "pe_gap_dir", "expr": "hist_lower if pe_hist_pct < pe_ind_pct else hist_higher",
             "dtype": "enum", "codes": ["hist_lower", "hist_higher"]},
        ],
        "params": {**_COMMON, "cheap_pctl": 20, "expensive_pctl": 80,
                   "divergence_gap_pctl": 30},
        "g4_exempt": True,  # loss_flag / negative_flag 命中即发布，豁免 G1/G2
        "rules": [
            {"rule_id": "VAL-00", "priority": 0,
             "conditions": _all(_cond("loss_flag", "==", True)), "result": "loss"},
            {"rule_id": "VAL-01", "priority": 1,
             "conditions": _all(_cond("negative_flag", "==", True)), "result": "negative"},
            {"rule_id": "VAL-02", "priority": 10,
             "conditions": _all(_cond("pe_gap", ">=", 30),
                                _cond("pe_gap_dir", "==", "hist_lower")),
             "result": "hist_cheap_ind_expensive"},
            {"rule_id": "VAL-03", "priority": 11,
             "conditions": _all(_cond("pe_gap", ">=", 30),
                                _cond("pe_gap_dir", "==", "hist_higher")),
             "result": "hist_expensive_ind_cheap"},
            {"rule_id": "VAL-04", "priority": 20,
             "conditions": _all(_cond("pe_hist_pct", "<=", 20),
                                _cond("pe_ind_pct", "<=", 20)), "result": "rel_cheap"},
            {"rule_id": "VAL-05", "priority": 21,
             "conditions": _all(_cond("pe_hist_pct", ">=", 80),
                                _cond("pe_ind_pct", ">=", 80)), "result": "rel_expensive"},
            {"rule_id": "VAL-06", "priority": 99, "conditions": _all(), "result": "rel_fair"},
        ],
        "output_schema": {
            "key_value_semantics": "相对估值位置的枚举类型；不是绝对贵贱、不是预测",
            "value_codes": {"rel_cheap": "相对偏便宜", "loss": "亏损（无 PE 估值锚）"},
        },
    },

    # ── §5.2 盈利质量 ─────────────────────────────────────────────
    "dim_quality_conclusion": {
        "version": "v1",
        "update_freq": _QUARTERLY,
        "inputs": [
            {"field": "roe_tier", "tag_code": "qal_roe_tier",
             "source_col": "key_value", "dtype": "int", "invalid": ["LOSS"], "role": "anchor"},
            {"field": "roe_stability", "tag_code": "qal_roe_stability",
             "source_col": "key_value", "dtype": "int"},
            {"field": "earnings_quality_tier", "tag_code": "qal_earnings_quality",
             "source_col": "key_value", "dtype": "int", "invalid": ["LOSS"], "role": "anchor"},
            {"field": "leverage_tier", "tag_code": "qal_leverage_tier",
             "source_col": "key_value", "dtype": "int"},
        ],
        "derived_fields": [],
        "params": {**_COMMON, "stable_max": 2, "volatile_min": 4,
                   "quality_weak_max": 2, "leverage_high": 4, "roe_high": 4},
        "rules": [
            {"rule_id": "QAL-01", "priority": 0,
             "conditions": _all(_cond("roe_stability", ">=", 4),
                                _cond("earnings_quality_tier", "<=", 2)),
             "result": "deteriorating"},
            {"rule_id": "QAL-02", "priority": 1,
             "conditions": _all(_cond("earnings_quality_tier", "<=", 2)),
             "result": "paper_profit"},
            {"rule_id": "QAL-03", "priority": 2,
             "conditions": _all(_cond("leverage_tier", ">=", 4)), "result": "leveraged"},
            {"rule_id": "QAL-04", "priority": 10,
             "conditions": _all(_cond("roe_tier", ">=", 4),
                                _cond("roe_stability", "<=", 2)), "result": "solid"},
            {"rule_id": "QAL-05", "priority": 11,
             "conditions": _all(_cond("roe_tier", ">=", 4)), "result": "high_but_volatile"},
            {"rule_id": "QAL-06", "priority": 99, "conditions": _all(), "result": "weak"},
        ],
        "output_schema": {
            "key_value_semantics": "盈利水平/稳定性/现金转化/杠杆的财报特征，不评价证券好坏",
            "value_codes": {"solid": "盈利稳且现金实", "paper_profit": "利润高但现金转化弱"},
        },
    },

    # ── §5.3 成长 ────────────────────────────────────────────────
    "dim_growth_conclusion": {
        "version": "v1",
        "update_freq": _QUARTERLY,
        "inputs": [
            {"field": "rev_tier", "tag_code": "grw_revenue_yoy_tier",
             "source_col": "key_value", "dtype": "int", "role": "anchor"},
            {"field": "np_tier", "tag_code": "grw_profit_yoy_tier",
             "source_col": "key_value", "dtype": "int"},
            {"field": "rev_yoy", "tag_code": "grw_revenue_yoy_tier",
             "source_col": "num_value", "dtype": "float"},
            {"field": "np_yoy", "tag_code": "grw_profit_yoy_tier",
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
             "conditions": _all(_cond("rev_tier", ">=", 4), _cond("np_tier", ">=", 4),
                                _cond("persistence", ">=", 2),
                                _cond("accel", "==", "steady")),
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
        "version": "v1",
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
             "conditions": _all(_cond("position_tier", "<=", 2),
                                _cond("volume_ratio_60", ">=", 2.0),
                                _cond("ma_alignment", "==", "bullish")),
             "result": "low_volume_start"},
            {"rule_id": "POS-02", "priority": 2,
             "conditions": _all(_cond("position_tier", ">=", 4),
                                _cond("volume_ratio_60", ">=", 2.0)), "result": "high_overheat"},
            {"rule_id": "POS-03", "priority": 3,
             "conditions": _all(_cond("ma_alignment", "==", "bearish"),
                                _cond("momentum_tier", "<=", 2)),
             "result": "downtrend_unresolved"},
            {"rule_id": "POS-04", "priority": 4,
             "conditions": _all(_cond("ma_alignment", "==", "bullish"),
                                _cond("position_tier", "==", 3)),
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
        "version": "v1",
        "update_freq": _DAILY,
        "inputs": [
            {"field": "valuation_class", "source": "derived", "compute": "val_class",
             "dtype": "enum", "codes": ["cheap", "neutral", "expensive", "no_anchor"]},
            {"field": "quality_class", "source": "derived", "compute": "qal_class",
             "dtype": "enum", "codes": ["solid", "volatile", "weak"]},
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
            {"rule_id": "VQ-C2", "priority": 11,
             "conditions": _all(_cond("valuation_class", "==", "cheap"),
                                _cond("quality_class", "==", "volatile")),
             "result": "value_trap_risk"},
            {"rule_id": "VQ-C3", "priority": 12,
             "conditions": _all(_cond("valuation_class", "==", "cheap"),
                                _cond("quality_class", "==", "weak")),
             "result": "value_trap_risk"},
            {"rule_id": "VQ-C4", "priority": 20,
             "conditions": _all(_cond("valuation_class", "==", "neutral"),
                                _cond("quality_class", "==", "solid")),
             "result": "quality_rel_fair"},
            {"rule_id": "VQ-C6", "priority": 30,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "solid")),
             "result": "quality_rel_expensive"},
            {"rule_id": "VQ-C8", "priority": 31,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "volatile")),
             "result": "weak_and_expensive"},
            {"rule_id": "VQ-C9", "priority": 32,
             "conditions": _all(_cond("valuation_class", "==", "expensive"),
                                _cond("quality_class", "==", "weak")),
             "result": "weak_and_expensive"},
            # neutral × (volatile|weak) 及所有未列 cell：宁可不出档
            {"rule_id": "VQ-99", "priority": 99, "conditions": _all(), "result": None},
        ],
        "output_schema": {
            "key_value_semantics": "估值 × 盈利质量的交叉类型；result=None 表示整行不发布，不是 enum 码",
            "value_codes": {"value_trap_risk": "价值陷阱嫌疑"},
        },
    },

    # ── §6.2 增长质量 ─────────────────────────────────────────────
    "biz_growth_quality_type": {
        "version": "v1",
        "update_freq": _QUARTERLY,
        "inputs": [
            # 主锚：L2 未出档时整行不发布，否则 GQ-04 兜底会把数据不足的票
            # 误判成 sustained_cash_backed（违反 §4.2「宁可不出档」）
            {"field": "growth_code", "source": "derived", "compute": "l2_growth",
             "dtype": "enum", "role": "anchor",
             "codes": ["fast_sustained", "fast_accelerating", "fast_decelerating",
                       "slow_steady", "contracting", "revenue_profit_divergent"]},
            {"field": "earnings_quality_tier", "tag_code": "qal_earnings_quality",
             "source_col": "key_value", "dtype": "int", "invalid": ["LOSS"]},
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
            {"rule_id": "GQ-04", "priority": 99, "conditions": _all(),
             "result": "sustained_cash_backed"},
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
