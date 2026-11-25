#!/usr/bin/env python3
"""个股画像 L2/L3 结论标签元数据（doc/stock_profile_l2_l3.md v1.1）。

本模块**只注册元数据，不实现计算**：6 个结论标签 source_type='derived'，
由 profiling.conclusions.producer 统一生产，不走 engine.compute_tag()。
compute_profile.py / profiling/run.py 的逐标签循环必须按 source_type 跳过它们。

值域（D21 实施后 40 个 code / label / short_label）以文档附录 D 与 §5.2 规则表为唯一真相源；
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
    "hist_cheap_ind_neutral": ("相对自身历史偏低、相对同行中性", "历史低同行中"),
    "hist_expensive_ind_neutral": ("相对自身历史偏高、相对同行中性", "历史高同行中"),
    "hist_neutral_ind_cheap": ("相对自身历史中性、相对同行偏低", "历史中同行低"),
    "hist_neutral_ind_expensive": ("相对自身历史中性、相对同行偏高", "历史中同行高"),
    "loss": ("亏损（无 PE 估值锚）", "亏损无锚"),
    "negative": ("净资产为负（无 PB 估值锚）", "净资产为负"),
}

QUALITY_ENUM = {
    "solid": ("盈利稳且现金实", "稳且现金实"),
    "high_but_volatile": ("盈利高但波动大", "高但波动"),
    "paper_profit": ("利润高但现金转化弱", "现金转化弱"),
    "leveraged": ("高负债率支撑", "高负债率"),
    "moderate": ("盈利水平中等", "盈利中等"),   # D21 新增：承接原 weak 中的中游公司
    "weak": ("盈利水平偏弱", "盈利偏弱"),
    "deteriorating": ("稳定性与真实性均偏弱", "质量均偏弱"),
}

GROWTH_ENUM = {
    "fast_sustained": ("高增速且持续", "高增速且持续"),
    "fast_accelerating": ("增速抬升", "增速抬升"),
    "fast_decelerating": ("增速放缓", "增速放缓"),
    "slow_steady": ("低速但稳", "低增速但稳"),
    "contracting": ("负增长", "负增长"),
    "revenue_profit_divergent": ("增收不增利或反之", "增收增利背离"),
}

POSITION_ENUM = {
    "low_quiet": ("低位缩量", "低位缩量"),
    "low_volume_start": ("低位放量", "低位放量"),
    "trend_continuation": ("均线多头排列", "均线多头"),
    "high_overheat": ("高位过热", "高位过热"),
    "downtrend_unresolved": ("下跌未止", "下跌未止"),
    "range_bound": ("区间震荡", "区间震荡"),
}

VALUE_QUALITY_ENUM = {
    "quality_rel_cheap": ("盈利稳且价格相对偏低", "盈利稳 · 价格低"),
    "quality_rel_fair": ("盈利稳价格中性", "盈利稳 · 价格中"),
    "quality_rel_expensive": ("盈利稳但价格相对偏贵", "盈利稳 · 价格高"),
    "moderate_rel_cheap": ("盈利中等且价格相对偏低", "盈利中 · 价格低"),
    "moderate_rel_fair": ("盈利中等且价格中性", "盈利中 · 价格中"),
    "moderate_rel_expensive": ("盈利中等但价格相对偏贵", "盈利中 · 价格高"),
    "value_trap_risk": ("盈利质量弱且价格相对偏低", "盈利弱 · 价格低"),
    "weak_and_expensive": ("盈利质量弱且价格相对偏贵", "盈利弱 · 价格高"),
}

GROWTH_QUALITY_ENUM = {
    "high_solid": ("高成长且盈利稳", "高成长 · 盈利稳"),
    "high_moderate": ("高成长且盈利中等", "高成长 · 盈利中"),
    "high_volatile": ("高成长但盈利波动大", "高成长 · 盈利波动"),
    "high_weak": ("高成长但盈利质量弱", "高成长 · 盈利弱"),
    "steady_solid": ("成长平稳且盈利稳", "成长平稳 · 盈利稳"),
    "steady_moderate": ("成长平稳且盈利中等", "成长平稳 · 盈利中"),
    "steady_volatile": ("成长平稳但盈利波动大", "成长平稳 · 盈利波动"),
    "steady_weak": ("成长平稳但盈利质量弱", "成长平稳 · 盈利弱"),
    "divergent_solid": ("增收增利背离但盈利稳", "增收背离 · 盈利稳"),
    "divergent_moderate": ("增收增利背离且盈利中等", "增收背离 · 盈利中"),
    "divergent_volatile": ("增收增利背离且盈利波动大", "增收背离 · 盈利波动"),
    "divergent_weak": ("增收增利背离且盈利质量弱", "增收背离 · 盈利弱"),
    "negative_solid": ("衰退但盈利稳", "衰退 · 盈利稳"),
    "negative_moderate": ("衰退且盈利中等", "衰退 · 盈利中"),
    "negative_volatile": ("衰退且盈利波动大", "衰退 · 盈利波动"),
    "negative_weak": ("衰退且盈利质量弱", "衰退 · 盈利弱"),
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
    compute_logic="双分位各自按 20/80 切偏低、中性、偏高三档：同向且同档判相对档；"
                  "历史分位≤20 且行业分位≥80、或历史分位≥80 且行业分位≤20 时单列背离码，"
                  "背离码仅描述两个参照系分歧，不构成贵贱判断；"
                  "单侧四格按各自位置单列中性码，双中性格判相对中性。"
                  "LOSS/NEGATIVE 业务码豁免 G1 直接发布。横截面分位不等于绝对贵贱。"
                  + _DISCLAIMER,
    parent_tag="val_pe_hist_pct", version="v4", pit_capable=True,   # D20：行业分位换二级 + 档位派生
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_valuation_conclusion(as_of: date):
    _no_direct_compute("dim_valuation_conclusion")


@tag(
    code="dim_quality_conclusion", name="盈利质量结论", domain="盈利质量",
    value_type=ENUM, enum_type="quality_conclusion", enum_values=QUALITY_ENUM,
    source_type="derived", update_freq="quarterly",
    data_sources=["qal_roe_ind1_tier", "qal_roe_stability_ind1",
                  "qal_earnings_quality_ind1", "qal_leverage_ind1_tier"],
    compute_logic="五项输入全取一级行业口径：ROE 档 × ROE 波动档 × 现金转化档三腿合取；"
                  "杠杆仅在没有高 ROE 时降级；ROE 绝对值低于底线最高只能评盈利中等。"
                  "描述的是财报特征，不评价证券好坏。" + _DISCLAIMER,
    parent_tag="qal_roe_ind1_tier", version="v4", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_quality_conclusion(as_of: date):
    _no_direct_compute("dim_quality_conclusion")


@tag(
    code="dim_growth_conclusion", name="成长结论", domain="成长特征",
    value_type=ENUM, enum_type="growth_conclusion", enum_values=GROWTH_ENUM,
    source_type="derived", update_freq="quarterly",
    data_sources=["grw_revenue_yoy_ind1_tier", "grw_profit_yoy_ind1_tier",
                  "grw_growth_persistence", "grw_growth_accel"],
    compute_logic="营收/净利增速档取一级行业口径；增速档 × 持续性 × 加速度 → 状态；"
                  "营收档与净利档背离优先单列。现状判断，非未来增速预测。" + _DISCLAIMER,
    parent_tag="grw_revenue_yoy_ind1_tier", version="v4", pit_capable=False,
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
    parent_tag="tst_price_position_52w", version="v3", pit_capable=True,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def dim_position_state(as_of: date):
    _no_direct_compute("dim_position_state")


@tag(
    code="biz_value_quality_type", name="好公司×好价格交叉类型", domain="综合研判",
    value_type=ENUM, enum_type="value_quality_type", enum_values=VALUE_QUALITY_ENUM,
    source_type="derived", update_freq="daily",
    data_sources=["dim_valuation_conclusion", "dim_quality_conclusion"],
    compute_logic="估值三类 × 盈利质量四类（solid/volatile/moderate/weak）交叉矩阵；"
                  "无估值锚或无明确交叉特征的 cell 整行不发布。"
                  + _DISCLAIMER,
    parent_tag="dim_valuation_conclusion", version="v3", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def biz_value_quality_type(as_of: date):
    _no_direct_compute("biz_value_quality_type")


@tag(
    code="biz_growth_quality_type", name="增长质量", domain="综合研判",
    value_type=ENUM, enum_type="growth_quality_type", enum_values=GROWTH_QUALITY_ENUM,
    source_type="derived", update_freq="quarterly",
    data_sources=["dim_growth_conclusion", "dim_quality_conclusion"],
    compute_logic="成长性四类（高成长/成长平稳/增收增利背离/衰退）× 盈利质量四类"
                  "（稳/中等/波动大/弱）→ 4×4 交叉类型；两侧 L2 任一未出档整行不发布。"
                  "FCF 等补数字段只作 cross_check 旁证，不进判定。" + _DISCLAIMER,
    parent_tag="dim_growth_conclusion", version="v4", pit_capable=False,
    is_exclusive=False, confidence_req=False, status=ACTIVE, owner="profiling",
)
def biz_growth_quality_type(as_of: date):
    _no_direct_compute("biz_growth_quality_type")


DERIVED_TAGS = (
    "dim_valuation_conclusion", "dim_quality_conclusion",
    "dim_growth_conclusion", "dim_position_state",
    "biz_value_quality_type", "biz_growth_quality_type",
)
