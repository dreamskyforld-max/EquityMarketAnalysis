#!/usr/bin/env python3
"""个股画像 L2/L3 结论标签元数据（doc/stock_profile_l2_l3.md v1.0）。

本模块**只注册元数据，不实现计算**：6 个结论标签 source_type='derived'，
由 profiling.conclusions.producer 统一生产，不走 engine.compute_tag()。
compute_profile.py / profiling/run.py 的逐标签循环必须按 source_type 跳过它们。

值域（35 个 code / label / short_label）以文档附录 D 为唯一真相源；
规则契约（inputs/derived_fields/params/rules/output_schema）以
profiling.conclusions.rules.RULES 为唯一可执行真源。

下方每个占位函数都抛 NotImplementedError，用于「漏掉 derived 跳过」时立即暴露，
避免静默产出错误结论。
"""
from __future__ import annotations

from datetime import date

from profiling.registry import ACTIVE, ENUM, tag

_DISCLAIMER = "本结论为描述性判断，不构成投资建议，不预测未来价格走势。"

# ── 附录 D：6 组 enum 值域 ────────────────────────────────────────────
VALUATION_ENUM = {
    "rel_cheap": ("相对偏便宜", "相对偏低"),
    "rel_fair": ("相对中性", "相对中性"),
    "rel_expensive": ("相对偏贵", "相对偏高"),
    "hist_cheap_ind_expensive": ("相对自身历史偏低、相对同行偏高", "历史低同行高"),
    "hist_expensive_ind_cheap": ("相对自身历史偏高、相对同行偏低", "历史高同行低"),
    "loss": ("亏损（无 PE 估值锚）", "亏损无锚"),
    "negative": ("净资产为负（无 PB 估值锚）", "净资产为负"),
}

QUALITY_ENUM = {
    "solid": ("盈利稳且现金实", "稳且现金实"),
    "high_but_volatile": ("盈利高但波动大", "高但波动"),
    "paper_profit": ("利润高但现金转化弱", "现金转化弱"),
    "leveraged": ("高杠杆支撑", "高杠杆"),
    "weak": ("盈利水平偏弱", "盈利偏弱"),
    "deteriorating": ("稳定性与真实性均偏弱", "质量均偏弱"),
}

GROWTH_ENUM = {
    "fast_sustained": ("高增速且持续", "高且持续"),
    "fast_accelerating": ("增速抬升", "增速抬升"),
    "fast_decelerating": ("增速放缓", "增速放缓"),
    "slow_steady": ("低速但稳", "低速稳"),
    "contracting": ("负增长", "负增长"),
    "revenue_profit_divergent": ("增收不增利或反之", "增收增利背离"),
}

POSITION_ENUM = {
    "low_quiet": ("低位缩量", "低位缩量"),
    "low_volume_start": ("低位放量", "低位放量"),
    "trend_continuation": ("趋势段内运行", "趋势段内"),
    "high_overheat": ("高位过热", "高位过热"),
    "downtrend_unresolved": ("下跌未止", "下跌未止"),
    "range_bound": ("区间震荡", "区间震荡"),
}

VALUE_QUALITY_ENUM = {
    "quality_rel_cheap": ("盈利稳且价格相对偏低", "稳且偏低"),
    "quality_rel_fair": ("盈利稳价格中性", "稳且中性"),
    "quality_rel_expensive": ("盈利稳但价格相对偏贵", "稳且偏高"),
    "value_trap_risk": ("相对偏便宜但盈利质量走弱（价值陷阱嫌疑）", "价值陷阱嫌疑"),
    "weak_and_expensive": ("盈利质量弱且价格相对偏贵", "弱且偏高"),
}

GROWTH_QUALITY_ENUM = {
    "sustained_cash_backed": ("增长持续且现金支撑", "现金支撑"),
    "cash_burning_expansion": ("高增长但自由现金流为负", "扩张耗现金"),
    "paper_growth": ("增长但利润现金转化弱", "转化弱"),
    "one_off_like": ("疑似一次性因素（增速跳变、持续性弱）", "疑似一次性"),
    "contraction": ("收缩", "收缩"),
}


def _no_direct_compute(name: str):
    raise NotImplementedError(
        f"{name} 是 source_type='derived' 的结论标签，必须由 "
        "profiling.conclusions.producer 生产；逐标签循环应按 source_type 跳过。"
    )


@tag(
    code="dim_valuation_conclusion", name="估值结论", domain="估值水平",
    value_type=ENUM, enum_type="valuation_conclusion", enum_values=VALUATION_ENUM,
    source_type="derived", update_freq="daily",
    data_sources=["val_pe_hist_pct", "val_pe_ind_pct", "val_pe_ttm_tier", "val_pb_tier"],
    compute_logic="双分位同向且同档 → 相对档；分位背离 → 单列背离码；LOSS/NEGATIVE 业务码豁免 G1 直接发布。"
                  "横截面分位不等于绝对贵贱。" + _DISCLAIMER,
    parent_tag="val_pe_hist_pct", version="v1", pit_capable=True,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_valuation_conclusion(as_of: date):
    _no_direct_compute("dim_valuation_conclusion")


@tag(
    code="dim_quality_conclusion", name="盈利质量结论", domain="盈利质量",
    value_type=ENUM, enum_type="quality_conclusion", enum_values=QUALITY_ENUM,
    source_type="derived", update_freq="quarterly",
    data_sources=["qal_roe_tier", "qal_roe_stability", "qal_earnings_quality", "qal_leverage_tier"],
    compute_logic="盈利水平 × 稳定性 × 现金转化真实性三腿合取；杠杆单独降级。"
                  "描述的是财报特征，不评价证券好坏。" + _DISCLAIMER,
    parent_tag="qal_roe_tier", version="v1", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_quality_conclusion(as_of: date):
    _no_direct_compute("dim_quality_conclusion")


@tag(
    code="dim_growth_conclusion", name="成长结论", domain="成长特征",
    value_type=ENUM, enum_type="growth_conclusion", enum_values=GROWTH_ENUM,
    source_type="derived", update_freq="quarterly",
    data_sources=["grw_revenue_yoy_tier", "grw_profit_yoy_tier",
                  "grw_growth_persistence", "grw_growth_accel"],
    compute_logic="增速档 × 持续性 × 加速度 → 状态；营收档与净利档背离优先单列。"
                  "现状判断，非未来增速预测。" + _DISCLAIMER,
    parent_tag="grw_revenue_yoy_tier", version="v1", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_growth_conclusion(as_of: date):
    _no_direct_compute("dim_growth_conclusion")


@tag(
    code="dim_position_state", name="当前位置与状态", domain="趋势状态",
    value_type=ENUM, enum_type="position_state", enum_values=POSITION_ENUM,
    source_type="derived", update_freq="daily",
    data_sources=["tst_price_position_52w", "trd_momentum_12_1", "att_volume_surge", "v_daily_quote"],
    compute_logic="确定性条件合取 + 游程标注（裸判，不加滞回状态机）。" + _DISCLAIMER,
    parent_tag="tst_price_position_52w", version="v1", pit_capable=True,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_position_state(as_of: date):
    _no_direct_compute("dim_position_state")


@tag(
    code="biz_value_quality_type", name="好公司×好价格交叉类型", domain="综合研判",
    value_type=ENUM, enum_type="value_quality_type", enum_values=VALUE_QUALITY_ENUM,
    source_type="derived", update_freq="daily",
    data_sources=["dim_valuation_conclusion", "dim_quality_conclusion"],
    compute_logic="估值 × 盈利质量 3×3 交叉矩阵；无估值锚或无明确交叉特征的 cell 整行不发布。"
                  + _DISCLAIMER,
    parent_tag="dim_valuation_conclusion", version="v1", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def biz_value_quality_type(as_of: date):
    _no_direct_compute("biz_value_quality_type")


@tag(
    code="biz_growth_quality_type", name="增长质量", domain="综合研判",
    value_type=ENUM, enum_type="growth_quality_type", enum_values=GROWTH_QUALITY_ENUM,
    source_type="derived", update_freq="quarterly",
    data_sources=["dim_growth_conclusion", "qal_earnings_quality", "financial_indicator"],
    compute_logic="增速状态 × 现金转化 × FCF 符号 → 类型码；背离优先于增速。" + _DISCLAIMER,
    parent_tag="dim_growth_conclusion", version="v1", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def biz_growth_quality_type(as_of: date):
    _no_direct_compute("biz_growth_quality_type")


DERIVED_TAGS = (
    "dim_valuation_conclusion", "dim_quality_conclusion",
    "dim_growth_conclusion", "dim_position_state",
    "biz_value_quality_type", "biz_growth_quality_type",
)
