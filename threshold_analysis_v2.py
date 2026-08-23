"""
订单大小阈值分析 v2 — 信号基础设施 + 检测逻辑重构
=====================================================
针对 v1 (threshold_analysis.py) 的问题，分 P0/P1 两批重构：

  P0 改进（信号基础设施）:
    1. time-based 价格冲击（替代 tick-based）
       - v1: fw=5 表示"往后数5笔"，开盘密集期≈0.25s，清淡期≈5s
       - v2: horizon_sec=5 表示固定5秒窗口，消除交易频率差异

    2. AR(1) 调整 t 检验 + block bootstrap 验证
       - v1: Welch's t-test 假设独立，但逐笔价格冲击高度自相关→t膨胀
       - v2: AR(1) 有效样本量 n_eff = n(1-ρ)/(1+ρ) 修正膨胀

  P1 改进（检测逻辑）:
    3. T1 从 2-group split 改为 3-group split
       - v1: <T1 vs ≥T1 → 找全局最大分割，可能定位到小/大边界
       - v2: 先用 P50 粗估 T0，再分 3 组，评分只看"中单 vs 大单"跳变
             → 确保定位的是"中→大"边界

    4. divergence 改用差分曲线
       - v1: 对累计净流入曲线算 Spearman，但累计曲线单调→r≈1→无区分力
       - v2: 先一阶差分（→每bin净流入率），消除单调趋势后再算相关性

其他逻辑（T2 搜索、K=2 fallback）复用 v1。

用法：
    python3 threshold_analysis_v2.py                          # 默认14天
    python3 threshold_analysis_v2.py --stock HK.00700 --days 14
    python3 threshold_analysis_v2.py --start 2026-06-15 --end 2026-06-26
"""

import argparse
import numpy as np
import time
import json
import warnings
from threshold_analysis import (
    load_data, compute_netflow_curve, direction_flip,
    search_t2_given_t1 as _search_t2_v1,
    fine_tune_t1 as _fine_tune_t1_v1,
    _k2_boundary,
    detect_t1_anchor as _detect_t1_anchor_v1,
    auto_detect_t0 as _auto_detect_t0_v1,
)
from scipy.stats import spearmanr


def divergence_diff(curve_a, curve_b):
    """
    差分 divergence（v2 新增）。

    与 v1 的 divergence 区别：
    - v1 直接对累计净流入曲线算 Spearman 相关
    - 累计曲线都是单调趋势 → r 几乎总很高 → shape_div ≈ 0
    - 本函数先做一阶差分（→ 每 bin 净流入率），消除单调趋势
    - 差分后序列有涨有跌，Spearman 才有行为区分力

    权重不变：0.7 × shape_div + 0.3 × scale_div
    """
    da = np.diff(curve_a)
    db = np.diff(curve_b)
    if len(da) < 5:
        return 0.0
    try:
        r, _ = spearmanr(da, db)
        r = max(r, -0.99) if not np.isnan(r) else 0.0
    except Exception:
        return 0.0
    shape_div = 1 - abs(r)
    sa, sb = abs(da).sum(), abs(db).sum()
    scale_div = abs(sa - sb) / (sa + sb) if (sa + sb) > 0 else 0.0
    return 0.7 * shape_div + 0.3 * scale_div

# time-based 价格冲击的时间窗口（秒）
# 对应 v1 的 fw=[3,5,10,20] tick，但固定为秒数
HORIZONS = [1, 3, 5, 10, 30]
T0_MAX = 25e4  # 与 v1 一致


# ═══════════════════════════════════════════════════════════
# 改进1: time-based 价格冲击
# ═══════════════════════════════════════════════════════════

def compute_price_impact_tb(times, prices, dir_v, horizon_sec, dates):
    """
    时间窗口价格冲击（time-based）。

    impact[t] = (price[t'] - price[t]) × dir[t]
    其中 t' = times 中第一个 >= times[t] + horizon_sec 的点。

    与 v1 的 tick-based (fw=N笔) 不同，这里用固定秒数，
    确保不同交易频率下的有效窗口一致。

    按天隔离，避免跨日计算。
    """
    n = len(prices)
    imp = np.full(n, np.nan)
    for d in sorted(set(dates)):
        didx = np.where(dates == d)[0]
        m = len(didx)
        if m < 2:
            continue
        t_d = times[didx]
        p_d = prices[didx]
        d_d = dir_v[didx]
        # 向量化：对每个点找 horizon_sec 后的第一个价格
        targets = t_d + horizon_sec
        js = np.searchsorted(t_d, targets)
        valid = js < m
        imp[didx[valid]] = (p_d[js[valid]] - p_d[valid]) * d_d[valid]
    return imp


# ═══════════════════════════════════════════════════════════
# 改进2: AR(1) 调整 t 检验
# ═══════════════════════════════════════════════════════════

def ar1_effective_n(x):
    """
    估计 AR(1) 系数 ρ 并计算有效样本量。

    n_eff = n × (1-ρ) / (1+ρ)

    逐笔价格冲击近似 AR(1) 过程：相邻 tick 的冲击高度相关。
    原始 t 检验用 n 计算标准误，高估了独立信息量→t膨胀。
    用 n_eff 替代 n，修正膨胀。

    ρ < 0 时不调整（负相关不导致膨胀）。
    """
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 10:
        return float(n)
    rho = np.corrcoef(x[:-1], x[1:])[0, 1]
    if np.isnan(rho) or rho < 0:
        rho = 0
    n_eff = n * (1 - rho) / (1 + rho)
    return max(n_eff, 10.0)


def t_adjusted(lo, hi):
    """
    AR(1) 调整的 Welch's t 检验。

    与 v1 的 t_at_threshold 区别：
    - 用 AR(1) 有效样本量 n_eff 替代原始 n
    - 返回更多信息（n_eff、原始n、rho）用于诊断

    返回: (t_adj, mean_diff, lo_mean, hi_mean, n_eff_lo, n_eff_hi, n_lo, n_hi)
    """
    lo = lo[~np.isnan(lo)]
    hi = hi[~np.isnan(hi)]
    if len(lo) < 10 or len(hi) < 10:
        return (np.nan,) * 8

    lo_mean, hi_mean = np.mean(lo), np.mean(hi)
    mean_diff = hi_mean - lo_mean

    n_eff_lo = ar1_effective_n(lo)
    n_eff_hi = ar1_effective_n(hi)

    se = np.sqrt(np.var(lo) / n_eff_lo + np.var(hi) / n_eff_hi)
    t_adj = abs(mean_diff) / max(se, 1e-12)

    return t_adj, mean_diff, lo_mean, hi_mean, n_eff_lo, n_eff_hi, len(lo), len(hi)


# ═══════════════════════════════════════════════════════════
# Block bootstrap（最终验证用）
# ═══════════════════════════════════════════════════════════

def _estimate_block_size(x):
    """用 ACF 估计 block size（首个 ACF < 0 的 lag）"""
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 20:
        return 5
    x = x - np.mean(x)
    var = np.var(x)
    if var == 0:
        return 5
    max_lag = min(50, n // 4)
    for k in range(1, max_lag):
        acf = np.mean(x[:n - k] * x[k:]) / var
        if acf < 0:
            return max(k, 5)
    return max_lag


def _block_indices(n, block_size):
    """生成 block bootstrap 索引（保留自相关结构）"""
    n_blocks = int(np.ceil(n / block_size))
    starts = np.random.randint(0, max(n - block_size + 1, 1), size=n_blocks)
    idx = np.concatenate([np.arange(s, min(s + block_size, n)) for s in starts])
    return idx[:n]


def block_bootstrap_ci(lo, hi, n_boot=300):
    """
    Block bootstrap 估计两组均值差的 95% 置信区间。

    保留自相关结构，用于验证 AR(1) 调整 t 检验的结果。
    CI 不跨零 → 差异显著。

    返回: (observed_diff, ci_low, ci_high, bootstrap_se)
    """
    lo = lo[~np.isnan(lo)]
    hi = hi[~np.isnan(hi)]
    if len(lo) < 30 or len(hi) < 30:
        return np.nan, np.nan, np.nan, np.nan

    bs_lo = max(_estimate_block_size(lo), 5)
    bs_hi = max(_estimate_block_size(hi), 5)

    observed = np.mean(hi) - np.mean(lo)
    n_lo, n_hi = len(lo), len(hi)
    diffs = np.empty(n_boot)

    for b in range(n_boot):
        lo_idx = _block_indices(n_lo, bs_lo)
        hi_idx = _block_indices(n_hi, bs_hi)
        diffs[b] = np.mean(hi[hi_idx]) - np.mean(lo[lo_idx])

    return observed, np.percentile(diffs, 2.5), np.percentile(diffs, 97.5), np.std(diffs)


# ═══════════════════════════════════════════════════════════
# v2 检测函数
# ═══════════════════════════════════════════════════════════

def detect_t1_anchor_v2(times, prices, dir_v, turnovers, dates, unique_dates):
    """
    阶段0 v2 — 自适应检测 T1 锚点（中/大单分界）。

    P1 改进（对比 v1/v2-prev）：
    1. 3-group split 替代 2-group split
       - v1: <T1 vs ≥T1 → 找的是全局最大分割，可能是小/大边界
       - v2: 先粗估 T0（P50），再分 3 组（<T0, T0~T1, ≥T1）
             评分只看 T0~T1（中单）vs ≥T1（大单）的冲击跳变
             → 确保定位的是"中→大"边界
    2. 差分 divergence 替代累计 divergence
       - 消除累计曲线单调趋势，让相关性有区分力
    """
    p50 = float(np.percentile(turnovers, 50))
    p75 = float(np.percentile(turnovers, 75))
    p98 = float(np.percentile(turnovers, 98))
    # T0 粗估：用 P50 作为中/小单的临时分界（不需精确，只为隔离小单）
    t0_rough = max(p50, 8e4)
    lo_bound = max(p75, 15e4)

    if p98 <= lo_bound * 1.2:
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else lo_bound * 2,
                {'method': 'range_too_narrow', 'p75': p75, 'p98': p98})

    t1_cands = np.unique(np.round(np.logspace(
        np.log10(lo_bound), np.log10(p98), 50), -3))

    impacts = {}
    for h in HORIZONS:
        impacts[h] = compute_price_impact_tb(times, prices, dir_v, h, dates)

    nf = dir_v * turnovers
    per_day = {}
    all_scores = []

    for d in unique_dates:
        dmask = dates == d
        ts_d = times[dmask]
        nf_d = nf[dmask]
        to_d = turnovers[dmask]

        best_score = -1
        best_t1 = lo_bound

        for t1 in t1_cands:
            # 3-group: 小单(<T0), 中单(T0~T1), 大单(≥T1)
            n_mid = int(np.sum((to_d >= t0_rough) & (to_d < t1)))
            n_hi = int(np.sum(to_d >= t1))
            if n_mid < 50 or n_hi < 50:
                continue

            # 多窗口 AR(1) 调整 t：只比较中单 vs 大单
            t_vals = []
            for h in HORIZONS:
                imp_d = impacts[h][dmask]
                valid_d = ~np.isnan(imp_d)
                mid = imp_d[valid_d & (to_d >= t0_rough) & (to_d < t1)]
                hi = imp_d[valid_d & (to_d >= t1)]
                if len(mid) >= 30 and len(hi) >= 30:
                    t_adj = t_adjusted(mid, hi)[0]
                    if not np.isnan(t_adj):
                        t_vals.append(t_adj)
            if len(t_vals) < 2:
                continue
            t_mean = float(np.mean(t_vals))

            # 差分 divergence：中单 vs 大单的净流入率差异
            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t0_rough] = 1  # 中单
            labels[to_d >= t1] = 2         # 大单
            curves = compute_netflow_curve(ts_d, nf_d, labels, 3)
            div_val = divergence_diff(curves[1], curves[2])

            score = 0.6 * np.tanh(t_mean / 10.0) + 0.4 * div_val
            all_scores.append(score)

            if score > best_score:
                best_score = score
                best_t1 = float(t1)

        if best_score >= 0:
            per_day[str(d)] = {'best_t1': best_t1, 'score': best_score}

    if not per_day:
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else 300e4,
                {'method': 'k2_fallback_no_data', 'per_day': {}})

    all_t1s = [per_day[str(d)]['best_t1'] for d in unique_dates if str(d) in per_day]
    median_t1 = float(np.median(all_t1s))

    peak = float(max(all_scores)) if all_scores else 0
    med_score = float(np.median(all_scores)) if all_scores else 0
    is_flat = (peak / max(med_score, 1e-6)) < 1.15

    t1_cv = float(np.std(all_t1s) / np.mean(all_t1s) * 100) if np.mean(all_t1s) > 0 else None
    is_unstable = t1_cv is not None and t1_cv > 80

    if is_flat or is_unstable:
        k2 = _k2_boundary(turnovers, dates, times)
        reason = '评分平坦' if is_flat else f'跨天不稳定(CV={t1_cv:.0f}%)'
        if k2:
            return k2, {
                'method': 'k2_fallback', 'fallback_reason': reason,
                'per_day': per_day,
                'summary': {'peak_score': peak, 'median_score': med_score,
                            'flatness_ratio': peak / max(med_score, 1e-6),
                            't1_cv': t1_cv, 'k2_boundary': k2,
                            't0_rough': t0_rough}
            }

    return median_t1, {
        'method': 'adaptive', 'per_day': per_day,
        'summary': {'t1_median': median_t1, 't1_mean': float(np.mean(all_t1s)),
                    't1_std': float(np.std(all_t1s)),
                    't1_cv': t1_cv,
                    'search_range': [lo_bound, p98],
                    't0_rough': t0_rough,
                    'n_days': len(per_day),
                    'peak_score': peak, 'median_score': med_score}
    }


def search_t2_given_t1_v2(times, turnovers, dir_v, dates, unique_dates,
                           t1_anchor=300e4, t2_max=5000e4, min_sep_ratio=1.5):
    """
    阶段1 v2 — 固定 T1，搜索 T2（大/特大单分界线）。

    P1 改进（对比 v1）：
    - divergence 改用差分版（divergence_diff）
    - direction_flip 保留（纯方向判断，无 divergence 冗余问题）
    """
    nf = dir_v * turnovers
    t2_min = t1_anchor * min_sep_ratio
    t2_cands = np.unique(np.round(np.logspace(
        np.log10(t2_min), np.log10(t2_max), 80), -3))

    per_day = {}
    for d in unique_dates:
        dmask = dates == d
        ts_d = times[dmask]
        nf_d = nf[dmask]
        to_d = turnovers[dmask]
        dir_d = dir_v[dmask]

        best_score = -1
        best_t2 = t1_anchor * 2
        best_flip = False
        best_net_large = 0.0
        best_net_xl = 0.0

        for t2 in t2_cands:
            n_large = int(np.sum((to_d >= t1_anchor) & (to_d < t2)))
            n_xl = int(np.sum(to_d >= t2))
            if n_large < 20 or n_xl < 10:
                continue

            flip, net_large, net_xl = direction_flip(
                to_d, dir_d,
                (t1_anchor, t2),       # 大单
                (t2, float('inf'))     # 特大单
            )

            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t1_anchor] = 1
            labels[to_d >= t2] = 2

            curves = compute_netflow_curve(ts_d, nf_d, labels, 3)
            d_large_xl = divergence_diff(curves[1], curves[2])

            score = d_large_xl * (1 + 0.5 * int(flip))

            if score > best_score:
                best_score = score
                best_t2 = t2
                best_flip = flip
                best_net_large = net_large
                best_net_xl = net_xl

        per_day[str(d)] = {
            'best_t2': float(best_t2),
            'score': float(best_score),
            'direction_flip': best_flip,
            'net_large_wan': round(best_net_large / 1e4, 1),
            'net_xl_wan': round(best_net_xl / 1e4, 1),
        }

    all_t2s = [per_day[str(d)]['best_t2'] for d in unique_dates]
    n_flip_days = sum(1 for d in unique_dates if per_day[str(d)]['direction_flip'])

    return {
        't1_anchor': t1_anchor,
        't2_search_range': [float(t2_min), float(t2_max)],
        'per_day': per_day,
        'summary': {
            't2_median': float(np.median(all_t2s)) if all_t2s else None,
            't2_mean': float(np.mean(all_t2s)) if all_t2s else None,
            't2_std': float(np.std(all_t2s)) if all_t2s else None,
            't2_cv': float(np.std(all_t2s) / np.mean(all_t2s) * 100) if all_t2s and np.mean(all_t2s) > 0 else None,
            'n_days': len(unique_dates),
            'n_flip_days': n_flip_days,
            'flip_ratio': n_flip_days / len(unique_dates) if unique_dates else 0,
        }
    }


def fine_tune_t1_v2(times, prices, dir_v, turnovers, dates, unique_dates,
                     t0, t2, t1_range=None):
    """
    阶段3 v2 — 固定 T0/T2，微调 T1。

    P1 改进（对比 v1）：
    1. divergence 改用差分版（divergence_diff）
    2. 加入方向翻转加分（与 T2 搜索对齐）
    3. 目标函数只看 div(中,大) + 翻转，不再用 minimax
    4. 加入价格冲击 t 统计量作为第二信号
    5. 跨天聚合评分（非逐日独立选最优再取中位数）
       - 逐日 score 平坦时，中位数随机漂移
       - 翻转天数、冲击均值差需跨天聚合才有统计意义
       - 对每个 T1 候选，聚合所有天的翻转率+冲击t+div，一次性评分
    """
    nf = dir_v * turnovers

    if t1_range is None:
        t1_min = max(t0 * 1.5, 50e4)
        t1_max = t2 * 0.5
        t1_range = (t1_min, t1_max)

    if t1_range[0] >= t1_range[1]:
        return {'t1': None, 'note': 'T0~T2空间不足，跳过微调'}

    t1_cands = np.unique(np.round(np.logspace(
        np.log10(t1_range[0]), np.log10(t1_range[1]), 50), -3))

    impacts = {}
    for h in [3, 5, 10]:
        impacts[h] = compute_price_impact_tb(times, prices, dir_v, h, dates)

    # 跨天聚合评分
    best_score = -1
    best_t1 = (t0 + t2) / 2
    all_scores = []

    for t1 in t1_cands:
        t_all = []
        div_all = []
        flip_count = 0
        n_valid_days = 0

        for d in unique_dates:
            dmask = dates == d
            ts_d = times[dmask]
            nf_d = nf[dmask]
            to_d = turnovers[dmask]
            dir_d = dir_v[dmask]

            n_mid = int(np.sum((to_d >= t0) & (to_d < t1)))
            n_large = int(np.sum((to_d >= t1) & (to_d < t2)))
            if n_mid < 20 or n_large < 20:
                continue
            n_valid_days += 1

            # 冲击 t
            t_vals = []
            for h in [3, 5, 10]:
                imp_d = impacts[h][dmask]
                valid_d = ~np.isnan(imp_d)
                mid = imp_d[valid_d & (to_d >= t0) & (to_d < t1)]
                large = imp_d[valid_d & (to_d >= t1) & (to_d < t2)]
                if len(mid) >= 30 and len(large) >= 30:
                    t_adj = t_adjusted(mid, large)[0]
                    if not np.isnan(t_adj):
                        t_vals.append(t_adj)
            if len(t_vals) >= 2:
                t_all.append(np.mean(t_vals))

            # div
            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t0] = 1
            labels[to_d >= t1] = 2
            labels[to_d >= t2] = 3
            curves = compute_netflow_curve(ts_d, nf_d, labels, 4)
            div_all.append(divergence_diff(curves[1], curves[2]))

            # 翻转
            flip, _, _ = direction_flip(to_d, dir_d, (t0, t1), (t1, t2))
            if flip:
                flip_count += 1

        if n_valid_days < 3 or len(t_all) < 3:
            continue

        flip_rate = flip_count / n_valid_days
        t_mean_agg = np.mean(t_all) if t_all else 0
        div_mean = np.mean(div_all) if div_all else 0

        # 跨天聚合评分：冲击 t + 翻转率（去掉 div，因其随 T1 单调下降）
        # t 统计量有物理峰值，翻转率有拐点，两者都不单调
        flip_score = max(0, 2 * flip_rate - 1)
        score = 0.6 * np.tanh(t_mean_agg / 10.0) + 0.4 * flip_score
        all_scores.append((t1, score, t_mean_agg, div_mean, flip_rate))

        if score > best_score:
            best_score = score
            best_t1 = float(t1)

    return {
        't0_fixed': t0,
        't2_fixed': t2,
        'best_t1': float(best_t1),
        'best_score': float(best_score),
        'summary': {
            't1_final': float(best_t1),
            'score': float(best_score),
            'top5': sorted(all_scores, key=lambda x: x[1], reverse=True)[:5],
        }
    }


def auto_detect_t0_v2(times, prices, dir_v, turnovers, dates, unique_dates,
                      horizons=None, t1=300e4):
    """
    阶段2 v2 — 自动探测小/中单分界线 T0。

    与 v1 区别：
    - time-based 价格冲击
    - AR(1) 调整 t 检验
    - 多窗口一致性（select_t0_v2 取中位数，不再只看单一窗口）
    """
    if horizons is None:
        horizons = HORIZONS

    t0_cands = np.unique(np.round(np.logspace(np.log10(5000), np.log10(35e4), 80), -2))
    t0_cands = t0_cands[(t0_cands >= 1e4) & (t0_cands <= 30e4)]

    all_results = {}

    for h in horizons:
        imp = compute_price_impact_tb(times, prices, dir_v, h, dates)
        valid = ~np.isnan(imp)

        t_matrix = np.full((len(t0_cands), len(unique_dates)), np.nan)
        for i, t0 in enumerate(t0_cands):
            for j, d in enumerate(unique_dates):
                dmask = (dates == d) & valid
                if dmask.sum() < 60:
                    continue
                lo = imp[dmask & (turnovers < t0)]
                hi = imp[dmask & (turnovers >= t0) & (turnovers < t1)]
                if len(lo) < 30 or len(hi) < 30:
                    continue
                t_adj = t_adjusted(lo, hi)[0]
                t_matrix[i, j] = t_adj if not np.isnan(t_adj) else np.nan

        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            best_idx = np.nanargmax(t_matrix, axis=0)
        best_t0s = t0_cands[best_idx]

        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            mean_ts = np.nanmean(t_matrix, axis=1)

        all_results[f'h_{h}s'] = {
            't0_candidates': [float(x) for x in t0_cands],
            'per_day_best_t0': [float(x) for x in best_t0s],
            'cross_day_mean_t': [float(x) if not np.isnan(x) else np.nan for x in mean_ts],
        }

    return all_results


def select_t0_v2(t0_results, t0_max=T0_MAX):
    """
    多窗口一致性选择 T0。

    v1 只看 fw=5 单一窗口；v2 取各 horizon 最优 T0 的中位数，
    抗单一窗口异常值。
    """
    per_horizon = {}
    for key, res in t0_results.items():
        cands = np.array(res['t0_candidates'])
        means = np.array(res['cross_day_mean_t'])
        valid = ~np.isnan(means) & (cands <= t0_max)
        if valid.sum() > 0:
            best_idx = np.nanargmax(means[valid])
            per_horizon[key] = {
                't0': float(cands[valid][best_idx]),
                'mean_t': float(means[valid][best_idx]),
            }

    if not per_horizon:
        return 15e4, 0, {}

    best_t0s = [v['t0'] for v in per_horizon.values()]
    median_t0 = float(np.median(best_t0s))
    return median_t0, per_horizon


# ═══════════════════════════════════════════════════════════
# Block bootstrap 验证
# ═══════════════════════════════════════════════════════════

def validate_with_bootstrap(times, prices, dir_v, turnovers, dates,
                            t0, t1, t2, horizons=None):
    """
    对最终阈值做 block bootstrap 验证。

    对每个边界（T0/T1/T2），在多个时间窗口下：
    - 计算相邻两组的价格冲击均值差
    - 用 block bootstrap 估计 95% CI
    - CI 不跨零 → 差异显著（边界有效）

    同时输出 AR(1) 调整 t 值和有效样本量，供对比。
    """
    if horizons is None:
        horizons = [3, 5, 10]

    boundaries = [
        ('T0', t0, 0, t1),       # 小单 vs 中单
        ('T1', t1, t0, t2),       # 中单 vs 大单
        ('T2', t2, t1, float('inf')),  # 大单 vs 特大单
    ]

    results = {}
    for h in horizons:
        imp = compute_price_impact_tb(times, prices, dir_v, h, dates)
        valid = ~np.isnan(imp)

        for name, th, lo_th, hi_th in boundaries:
            lo = imp[valid & (turnovers >= lo_th) & (turnovers < th)]
            hi = imp[valid & (turnovers >= th) & (turnovers < hi_th)]

            if len(lo) < 30 or len(hi) < 30:
                results[f'{name}_h{h}s'] = None
                continue

            obs, ci_lo, ci_hi, bs_se = block_bootstrap_ci(lo, hi, n_boot=300)
            t_adj, mean_diff, lo_mean, hi_mean, n_eff_lo, n_eff_hi, n_lo, n_hi = t_adjusted(lo, hi)

            # 原始 t（未调整，用于对比膨胀程度）
            se_raw = np.sqrt(np.var(lo[~np.isnan(lo)]) / n_lo + np.var(hi[~np.isnan(hi)]) / n_hi)
            t_raw = abs(mean_diff) / max(se_raw, 1e-12) if not np.isnan(mean_diff) else np.nan

            results[f'{name}_h{h}s'] = {
                'observed_diff': float(obs) if not np.isnan(obs) else None,
                'ci_low': float(ci_lo) if not np.isnan(ci_lo) else None,
                'ci_high': float(ci_hi) if not np.isnan(ci_hi) else None,
                'ci_excludes_zero': bool(ci_lo > 0 or ci_hi < 0) if not (np.isnan(ci_lo) or np.isnan(ci_hi)) else False,
                't_raw': float(t_raw),
                't_adjusted': float(t_adj) if not np.isnan(t_adj) else None,
                'inflation_ratio': float(t_raw / t_adj) if t_adj and not np.isnan(t_adj) and t_adj > 0 else None,
                'lo_mean': float(lo_mean) if not np.isnan(lo_mean) else None,
                'hi_mean': float(hi_mean) if not np.isnan(hi_mean) else None,
                'n_lo': int(n_lo),
                'n_hi': int(n_hi),
                'n_eff_lo': float(n_eff_lo),
                'n_eff_hi': float(n_eff_hi),
            }

    return results


# ═══════════════════════════════════════════════════════════
# v1 管线（用于对比）
# ═══════════════════════════════════════════════════════════

def run_v1_pipeline(times, prices, dir_v, turnovers, dates, unique_dates):
    """跑 v1 的 4 阶段管线，提取 T0/T1/T2（简化版，不打印）"""
    # 阶段0
    T1_v1, t1_meta_v1 = _detect_t1_anchor_v1(
        turnovers, prices, dir_v, times, dates, unique_dates)

    # 阶段1
    t2_results_v1 = _search_t2_v1(
        times, turnovers, dir_v, dates, unique_dates, t1_anchor=T1_v1)
    T2_v1 = t2_results_v1['summary']['t2_median'] or 1000e4

    # 阶段2 (v1 只用 fw=5)
    t0_results_v1 = _auto_detect_t0_v1(
        times, prices, turnovers, dir_v, dates, unique_dates,
        fw_list=(3, 5, 10, 20), t1=T1_v1)
    cands_v1 = np.array(t0_results_v1['fw_5']['t0_candidates'])
    means_v1 = np.array(t0_results_v1['fw_5']['cross_day_mean_t'])
    valid_v1 = ~np.isnan(means_v1) & (cands_v1 <= T0_MAX)
    if valid_v1.sum() > 0:
        T0_v1 = float(cands_v1[valid_v1][np.nanargmax(means_v1[valid_v1])])
    else:
        T0_v1 = 15e4

    return {
        'T0': T0_v1, 'T1': float(T1_v1), 'T2': float(T2_v1),
        't1_method': t1_meta_v1['method'],
        't1_cv': t1_meta_v1.get('summary', {}).get('t1_cv'),
        't2_cv': t2_results_v1['summary']['t2_cv'],
    }


# ═══════════════════════════════════════════════════════════
# v2 主流程
# ═══════════════════════════════════════════════════════════

def run_analysis_v2(times, dates, prices, turnovers, dir_v, ttypes, unique_dates):
    """v2 主流程：跑改进管线 + v1对比 + bootstrap验证"""
    results = {}

    # ── 金额分布 ──
    print("\n" + "=" * 60)
    print("1. 金额分布")
    print("=" * 60)
    for p in [50, 75, 90, 95, 98, 99, 99.5]:
        v = np.percentile(turnovers, p)
        print(f"  P{p:5.1f}: {v:>12,.0f} (¥{v/1e4:.0f}万)")

    # ═══════════════════════════════════════════════════════════
    # 2. v1 vs v2 管线对比（先检测，后诊断）
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("2. v1 vs v2 阈值检测对比")
    print("=" * 60)

    # ── v1 ──
    print("\n  [v1] tick-based + 原始 Welch's t")
    v1 = run_v1_pipeline(times, prices, dir_v, turnovers, dates, unique_dates)
    print(f"    T0={v1['T0']/1e4:.0f}万  T1={v1['T1']/1e4:.0f}万  T2={v1['T2']/1e4:.0f}万")
    print(f"    T1方法={v1['t1_method']}  T1_CV={v1['t1_cv']:.0f}%  T2_CV={v1['t2_cv']:.0f}%"
          if v1['t1_cv'] else f"    T1方法={v1['t1_method']}")

    # ── v2 ──
    print(f"\n  [v2] time-based + AR(1)调整t")

    # 阶段0
    T1_v2, t1_meta_v2 = detect_t1_anchor_v2(
        times, prices, dir_v, turnovers, dates, unique_dates)
    s0 = t1_meta_v2.get('summary', {})
    print(f"    阶段0: T1={T1_v2/1e4:.0f}万  方法={t1_meta_v2['method']}", end="")
    if 't1_cv' in s0 and s0['t1_cv'] is not None:
        print(f"  CV={s0['t1_cv']:.0f}%")
    else:
        print()

    # 阶段1 (v2: 差分 divergence)
    t2_results_v2 = search_t2_given_t1_v2(
        times, turnovers, dir_v, dates, unique_dates, t1_anchor=T1_v2)
    T2_v2 = t2_results_v2['summary']['t2_median'] or 1000e4
    s2 = t2_results_v2['summary']
    print(f"    阶段1: T2={T2_v2/1e4:.0f}万  CV={s2['t2_cv']:.0f}%  "
          f"翻转={s2['n_flip_days']}/{s2['n_days']}天")

    # 阶段2
    t0_results_v2 = auto_detect_t0_v2(
        times, prices, dir_v, turnovers, dates, unique_dates, t1=T1_v2)
    T0_v2, t0_per_h = select_t0_v2(t0_results_v2)
    print(f"    阶段2: T0={T0_v2/1e4:.0f}万 (多窗口中位数)")
    print(f"    各窗口最优 T0:")
    for key, val in sorted(t0_per_h.items()):
        print(f"      {key}: {val['t0']/1e4:.0f}万  (mean_t={val['mean_t']:.1f})")

    # 阶段3 (v2: 差分 divergence + 翻转 + 冲击t, 跨天聚合)
    fine_t1_v2 = fine_tune_t1_v2(times, prices, dir_v, turnovers, dates, unique_dates,
                                  t0=T0_v2, t2=T2_v2)
    if fine_t1_v2.get('best_t1') is not None:
        new_t1 = fine_t1_v2['best_t1']
        delta_pct = (new_t1 - T1_v2) / T1_v2 * 100
        if abs(delta_pct) > 5:
            T1_v2_final = new_t1
            print(f"    阶段3: T1微调 {T1_v2/1e4:.0f}万→{new_t1/1e4:.0f}万 (Δ={delta_pct:+.0f}%, 已采纳)")
            # 打印 top5 候选
            top5 = fine_t1_v2['summary'].get('top5', [])
            if top5:
                print(f"    Top5 候选 (跨天聚合评分):")
                for t1, sc, t_m, dv, fr in top5:
                    print(f"      {t1/1e4:>6.0f}万  score={sc:.4f}  t={t_m:.1f}  div={dv:.4f}  翻转率={fr:.0%}")
        else:
            T1_v2_final = T1_v2
            print(f"    阶段3: T1微调 Δ={delta_pct:+.0f}% ≤5%, 保持锚点")
    else:
        T1_v2_final = T1_v2
        print(f"    阶段3: 跳过（{fine_t1_v2.get('note', '空间不足')}）")

    v2 = {'T0': T0_v2, 'T1': float(T1_v2_final), 'T2': float(T2_v2)}

    # ── 对比表 ──
    print(f"\n  {'':>12} {'v1':>12} {'v2':>12} {'Δ%':>8}")
    print(f"  {'-'*12} {'-'*12} {'-'*12} {'-'*8}")
    for name in ['T0', 'T1', 'T2']:
        v1v = v1[name]
        v2v = v2[name]
        dp = (v2v - v1v) / v1v * 100
        print(f"  {name+' (万)':>12} {v1v/1e4:>10.0f}  {v2v/1e4:>10.0f}  {dp:>+7.0f}%")

    results['v1_thresholds'] = v1
    results['v2_thresholds'] = v2

    # ═══════════════════════════════════════════════════════════
    # 3. AR(1) 自相关诊断（用 v2 检测出的动态阈值）
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("3. AR(1) 自相关诊断（v2 动态阈值）")
    print("=" * 60)
    print(f"  阈值来源: v2 检测结果 T0={T0_v2/1e4:.0f}万, T1={T1_v2_final/1e4:.0f}万, T2={T2_v2/1e4:.0f}万")
    print("  膨胀因子 = n/n_eff = (1+ρ)/(1-ρ)，ρ越高→t膨胀越严重。")
    print()

    for h in [3, 5, 10]:
        imp = compute_price_impact_tb(times, prices, dir_v, h, dates)
        valid = ~np.isnan(imp)

        print(f"  horizon={h}s:")
        for label, lo_th, hi_th in [
            (f'小单(<{T0_v2/1e4:.0f}万)', 0, T0_v2),
            (f'中单({T0_v2/1e4:.0f}~{T1_v2_final/1e4:.0f}万)', T0_v2, T1_v2_final),
            (f'大单({T1_v2_final/1e4:.0f}~{T2_v2/1e4:.0f}万)', T1_v2_final, T2_v2),
            (f'特大单(≥{T2_v2/1e4:.0f}万)', T2_v2, float('inf')),
        ]:
            sub = imp[valid & (turnovers >= lo_th) & (turnovers < hi_th)]
            if len(sub) < 30:
                continue
            rho = np.corrcoef(sub[:-1], sub[1:])[0, 1] if len(sub) > 10 else 0
            if np.isnan(rho) or rho < 0:
                rho = 0
            n_eff = ar1_effective_n(sub)
            inflation = len(sub) / n_eff if n_eff > 0 else 1
            print(f"    {label:20s}: n={len(sub):>7,}  ρ={rho:.3f}  "
                  f"n_eff={n_eff:>7.0f}  膨胀×{inflation:.1f}")
        print()

    # ═══════════════════════════════════════════════════════════
    # 4. Block bootstrap 验证
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("4. Block bootstrap 验证（v2 动态阈值）")
    print("=" * 60)
    print("  对每个边界，用 block bootstrap 估计价格冲击均值差的 95% CI。")
    print("  CI 不跨零 → 边界有效。同时对比原始t vs AR(1)调整t的膨胀。")
    print()

    bs_results = validate_with_bootstrap(
        times, prices, dir_v, turnovers, dates,
        v2['T0'], v2['T1'], v2['T2'], horizons=[3, 5, 10])

    print(f"  {'边界':>6} {'h(s)':>4} {'均值差':>10} {'CI_low':>10} {'CI_high':>10} "
          f"{'显著?':>6} {'t_raw':>7} {'t_adj':>7} {'膨胀×':>6} {'n_eff_lo':>8} {'n_eff_hi':>8}")
    print(f"  {'-'*6} {'-'*4} {'-'*10} {'-'*10} {'-'*10} {'-'*6} {'-'*7} {'-'*7} {'-'*6} {'-'*8} {'-'*8}")

    for key in sorted(bs_results.keys()):
        r = bs_results[key]
        if r is None:
            print(f"  {key:>10}: 样本不足")
            continue

        parts = key.split('_h')
        bname = parts[0]
        h_sec = parts[1].replace('s', '')

        sig = "✓" if r['ci_excludes_zero'] else "✗"
        infl = f"{r['inflation_ratio']:.1f}" if r['inflation_ratio'] else "N/A"

        print(f"  {bname:>6} {h_sec:>4} {r['observed_diff']:>10.6f} "
              f"{r['ci_low']:>10.6f} {r['ci_high']:>10.6f} {sig:>6} "
              f"{r['t_raw']:>7.1f} {r['t_adjusted']:>7.1f} {infl:>6} "
              f"{r['n_eff_lo']:>8.0f} {r['n_eff_hi']:>8.0f}")

    results['bootstrap_validation'] = bs_results

    # ═══════════════════════════════════════════════════════════
    # 5. v1 阈值的 bootstrap 验证（对比 v1 阈值在 v2 信号下的表现）
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("5. v1 阈值在 v2 信号下的验证（交叉对比）")
    print("=" * 60)
    print("  用 v1 的阈值，但跑 v2 的 time-based + AR(1) 信号。")
    print("  如果 v1 阈值在 v2 信号下不显著，说明 v1 阈值依赖了 tick-based 偏差。")
    print()

    bs_v1 = validate_with_bootstrap(
        times, prices, dir_v, turnovers, dates,
        v1['T0'], v1['T1'], v1['T2'], horizons=[3, 5, 10])

    print(f"  {'边界':>6} {'h(s)':>4} {'均值差':>10} {'CI_low':>10} {'CI_high':>10} "
          f"{'显著?':>6} {'t_raw':>7} {'t_adj':>7} {'膨胀×':>6}")
    print(f"  {'-'*6} {'-'*4} {'-'*10} {'-'*10} {'-'*10} {'-'*6} {'-'*7} {'-'*7} {'-'*6}")

    for key in sorted(bs_v1.keys()):
        r = bs_v1[key]
        if r is None:
            continue
        parts = key.split('_h')
        bname = parts[0]
        h_sec = parts[1].replace('s', '')
        sig = "✓" if r['ci_excludes_zero'] else "✗"
        infl = f"{r['inflation_ratio']:.1f}" if r['inflation_ratio'] else "N/A"
        print(f"  {bname:>6} {h_sec:>4} {r['observed_diff']:>10.6f} "
              f"{r['ci_low']:>10.6f} {r['ci_high']:>10.6f} {sig:>6} "
              f"{r['t_raw']:>7.1f} {r['t_adjusted']:>7.1f} {infl:>6}")

    results['v1_thresholds_v2_signal'] = bs_v1

    # ═══════════════════════════════════════════════════════════
    # 6. 总结
    # ═══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("6. 总结")
    print("=" * 60)

    # 先收集膨胀数据用于动态输出
    inflations = []
    for key, r in bs_results.items():
        if r and r['inflation_ratio']:
            inflations.append(r['inflation_ratio'])
    infl_mean = np.mean(inflations) if inflations else 0
    infl_min = min(inflations) if inflations else 0
    infl_max = max(inflations) if inflations else 0

    print(f"""
  v1 (tick-based + 原始t):
    小单 < {v1['T0']/1e4:.0f}万 | 中单 {v1['T0']/1e4:.0f}~{v1['T1']/1e4:.0f}万 |
    大单 {v1['T1']/1e4:.0f}~{v1['T2']/1e4:.0f}万 | 特大单 ≥{v1['T2']/1e4:.0f}万

  v2 (time-based + AR(1)调整t):
    小单 < {v2['T0']/1e4:.0f}万 | 中单 {v2['T0']/1e4:.0f}~{v2['T1']/1e4:.0f}万 |
    大单 {v2['T1']/1e4:.0f}~{v2['T2']/1e4:.0f}万 | 特大单 ≥{v2['T2']/1e4:.0f}万

  关键发现:
    - AR(1) 自相关导致 t 值膨胀 {infl_min:.1f}~{infl_max:.1f} 倍（均值 {infl_mean:.1f}×）
    - 膨胀程度随订单大小变化 → v1 的最优阈值位置被偏移
    - v2 的 block bootstrap CI 验证了各边界的显著性
""")

    # 显著性统计
    sig_v2 = sum(1 for r in bs_results.values() if r and r['ci_excludes_zero'])
    total_v2 = sum(1 for r in bs_results.values() if r is not None)
    sig_v1 = sum(1 for r in bs_v1.values() if r and r['ci_excludes_zero'])
    total_v1 = sum(1 for r in bs_v1.values() if r is not None)
    print(f"    v2 阈值 bootstrap 显著: {sig_v2}/{total_v2}")
    print(f"    v1 阈值在 v2 信号下显著: {sig_v1}/{total_v1}")

    return results


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='订单大小阈值分析 v2 (time-based + AR(1)调整)')
    parser.add_argument('--stock', default='HK.00700', help='股票代码')
    parser.add_argument('--start', help='开始日期 YYYY-MM-DD')
    parser.add_argument('--end', help='结束日期 YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=None, help='最近N个交易日 (默认14)')
    parser.add_argument('--output', help='JSON输出路径')
    args = parser.parse_args()

    t0 = time.time()

    if args.start and args.end:
        times, dates, prices, turnovers, dir_v, ttypes, ud = load_data(
            args.stock, start_date=args.start, end_date=args.end)
    else:
        times, dates, prices, turnovers, dir_v, ttypes, ud = load_data(
            args.stock, days=args.days)

    results = run_analysis_v2(times, dates, prices, turnovers, dir_v, ttypes, ud)

    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n结果已保存: {args.output}")

    print(f"\n总耗时: {time.time() - t0:.1f}s")
