#!/usr/bin/env python3
"""分档方式评估 v2：等频 5 档是否合适？是否该换切法？

⚠️ 为什么不用「KMeans inertia 对比等频」：
   KMeans 的目标函数就是最小化 inertia，直接比较等于让它跟自己比，必然"赢"；
   且这些指标 skew 高达 15~75，总方差由少数极端值主导，档内方差占比会失真到 80%+。
   v1 那份"聚类更优 60~98%"是方法论假象，已废弃。

本版判据（都围绕「分档是否把相似的样本放一起、不同的样本分开」）：
  1. 档宽均衡度 CV：等频 5 档下，中间 3 档的**实际数值跨度**是否接近。
     CV 越大 → 各档"含义厚度"差得越远（第 5 档可能跨度是第 3 档的几百倍）
  2. 对数变换后的 CV：长尾指标在 log 尺度上重新等频分档，CV 是否显著改善。
     改善明显 → 该指标应按**倍数**分档（log 等频），而不是按**绝对值**分档
  3. 自然间隙检测：排序后相邻样本间距是否存在显著断层（gap ≫ 中位间距）。
     存在 → 分布自带分组，等频切点可能把断层两侧硬塞进同一档
  4. 分位数快照：给出 P5/P25/P50/P75/P95，供人工判断阈值语义

用法：.venv/bin/python3 _binning_analysis.py
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from db import get_conn  # noqa: E402

N_TIERS = 5
NAME = {
    "val_pe_ttm_tier": "PE(TTM)", "val_pb_tier": "PB", "val_ps_ttm_tier": "PS(TTM)",
    "val_pcf_ttm_tier": "PCF(TTM)", "val_fcf_yield_tier": "FCF收益率",
    "scl_mktcap_tier": "总市值", "scl_float_mktcap_tier": "流通市值", "scl_revenue_tier": "营收",
    "qal_roe_tier": "ROE", "qal_gross_margin_tier": "毛利率", "qal_net_margin_tier": "净利率",
    "qal_leverage_tier": "资产负债率", "qal_roe_stability": "ROE稳定性", "qal_earnings_quality": "盈利真实性",
    "grw_revenue_yoy_tier": "营收增速", "grw_profit_yoy_tier": "净利增速",
    "shr_dividend_yield_tier": "股息率", "shr_payout_tier": "派息率", "shr_buyback_tier": "回购率",
    "shr_total_yield_tier": "股东回报率", "trd_volatility_annual": "年化波动率", "trd_beta": "Beta",
    "trd_downside_vol": "下行波动", "trd_residual_vol": "残差波动", "trd_turnover": "换手率",
    "trd_max_drawdown": "最大回撤", "trd_momentum_12_1": "动量12-1", "trd_reversal_1m": "1月反转",
    "trd_liquidity": "日均成交额", "idt_listing_age_tier": "上市年限",
}


def signed_log(v: np.ndarray) -> np.ndarray:
    """保号对数变换：适用于含负值的指标（增速、ROE、动量等）。"""
    return np.sign(v) * np.log1p(np.abs(v))


def tier_width_cv(x: np.ndarray) -> float:
    """等频 5 档下中间 3 档的数值跨度变异系数（CV 越小=各档厚度越接近）。"""
    if len(x) < N_TIERS * 3:
        return np.nan
    edges = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
    inner = edges[1:]                      # P40/P60/P80 三条内部切点
    widths = np.diff(inner)                # 中间两档（P40~P60、P60~P80）的跨度
    return float(np.std(widths) / np.mean(widths)) if np.mean(widths) > 0 else np.nan


def gap_stats(x: np.ndarray) -> tuple[float, int]:
    """自然间隙：排序后相邻样本间距是否出现「断层」。

    阈值用**分布跨度的比例**而非中位间距的倍数——密集区（如保留 2 位小数的
    财务比率）中位间距趋近 0，用倍数判据会把正常间距全判成断层（v2 的坑）。
    """
    s = np.sort(x)
    gaps = np.diff(s)
    gaps = gaps[gaps > 0]
    if len(gaps) < 50:
        return np.nan, 0
    span = float(np.quantile(x, 0.99) - np.quantile(x, 0.01))
    if span <= 0:
        return np.nan, 0
    thr = 0.01 * span                     # 间隔超过分布跨度的 1% 才算断层
    return float(gaps.max() / thr), int((gaps > thr).sum())


def main():
    q = """
    SELECT v.tag_code, v.num_value::float8 AS val, si.market
    FROM profile.tag_value v
    JOIN profile.tag_registry r ON r.tag_code = v.tag_code
    JOIN stock_info si ON si.stock_code = v.stock_code
    WHERE v.eff_to = '9999-12-31' AND v.num_value IS NOT NULL
      AND r.value_type = 'tier'
      AND COALESCE(r.num_unit, '') NOT IN ('percentile_0_100', 'count', 'rank')
    """
    with get_conn() as conn:
        df = pd.read_sql(q, conn)

    rows = []
    for code, g in df.groupby("tag_code", sort=True):
        sub = g.dropna(subset=["val"])
        v = sub["val"].to_numpy()
        if len(v) < 300:
            continue
        sl = signed_log(v)

        cv_raw, cv_log = tier_width_cv(v), tier_width_cv(sl)
        gap_ratio, n_gap = gap_stats(sl)

        # 建议
        if not np.isnan(cv_log) and not np.isnan(cv_raw) and cv_raw > 0 and cv_log < 0.6 * cv_raw:
            advice = "改对数分档（长尾，按倍数切更均衡）"
        elif n_gap >= 3 and (gap_ratio or 0) > 50:
            advice = "分布有自然断层，等频可能切开断层两侧"
        elif not np.isnan(cv_raw) and cv_raw < 0.3:
            advice = "等频 5 档合适（各档跨度均衡）"
        else:
            advice = "等频 5 档可接受"

        rows.append({
            "指标": NAME.get(code, code), "n": len(v),
            "P25": round(float(np.quantile(v, .25)), 2),
            "P50": round(float(np.median(v)), 2),
            "P75": round(float(np.quantile(v, .75)), 2),
            "P95": round(float(np.quantile(v, .95)), 2),
            "原始CV": round(cv_raw, 2) if not np.isnan(cv_raw) else None,
            "对数CV": round(cv_log, 2) if not np.isnan(cv_log) else None,
            "断层数": n_gap,
            "建议": advice,
        })

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 220, "display.max_rows", 100, "display.max_colwidth", 40)
    print(out.to_string(index=False))
    print("\n建议分布：")
    print(out["建议"].value_counts().to_string())


if __name__ == "__main__":
    main()
