"""
订单大小阈值分析 v3 — 鲁棒性优化版
=====================================
在 v2 (time-based + AR(1) + 3-group split) 基础上，
针对薄尾/低单价/小样本股票的鲁棒性问题做了 6 项优化：

  P0（v2 已知缺陷修复）：
    1. t0_rough 自适应：min(P50, 8e4) 替代 max(P50, 8e4)
       → 低价股用实际 P50，避免中单组被 8万下限挤压
    2. 三级回退：3-group → 2-group → K2
       → 薄尾/小样本时自动降级，不直接退到 K2

  P1（方法改进）：
    3. 动态 horizon：根据交易频率自适应选择 [1,3,5,10,30] 的子集
       → 低频股跳过 1s（搜索不到有效点），高频股加上 1s（捕捉微结构）
    4. 异常天检测：MAD 排除跨天聚合中的 outlier day
       → 单天极端行情不污染中位数
    5. divergence 自适应权重：根据差分序列波动量，动态调整 70/30
       → 当差分序列低波动时降低 shape_div 权重，避免噪声放大
    6. 信号强度分层输出：★★★(强) / ★★(中) / ★(弱) / -(无效)
       → 对每个边界给出可读的信号强度判断，方便人工审查

与 v2 保持一致的部分：
  - time-based 价格冲击（固定秒数，消除交易频率差异）
  - AR(1) 调整 t 检验 + block bootstrap 验证
  - 差分 divergence（消除累计曲线单调趋势）
  - 跨天聚合评分（fine_tune_t1）
  - v1 对比管线 + 交叉验证

用法：
    python3 threshold_analysis_v3.py                          # 默认14天
    python3 threshold_analysis_v3.py --stock HK.09660 --days 14
    python3 threshold_analysis_v3.py --start 2026-06-15 --end 2026-06-26
"""

import argparse
import numpy as np
import time
import json
import warnings
from threshold_analysis import (
    load_data, compute_netflow_curve, direction_flip, _k2_boundary,
    detect_t1_anchor as _detect_t1_anchor_v1,
    auto_detect_t0 as _auto_detect_t0_v1,
    search_t2_given_t1 as _search_t2_v1,
)
from scipy.stats import spearmanr

# ═══════════ 常量 ═══════════
HORIZONS_FULL = [1, 3, 5, 10, 30]   # 全量 horizon 池
HORIZONS_CORE = [3, 5, 10]           # 核心 horizon（t 检验/评分用）
HORIZONS_DIAG = [3, 5, 10]           # 诊断 horizon（AR(1)/bootstrap）
T0_MAX = 25e4                        # T0 硬上限
T0_MIN = 1e4                         # T0 硬下限


# ═══════════════════════════════════════════════════════════
# 基础信号函数（与 v2 相同，内联避免依赖）
# ═══════════════════════════════════════════════════════════

def compute_price_impact_tb(times, prices, dir_v, horizon_sec, dates):
    """time-based 价格冲击（同 v2）"""
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
        targets = t_d + horizon_sec
        js = np.searchsorted(t_d, targets)
        valid = js < m
        imp[didx[valid]] = (p_d[js[valid]] - p_d[valid]) * d_d[valid]
    return imp


def ar1_effective_n(x):
    """AR(1) 有效样本量（同 v2）"""
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 10:
        return float(n)
    rho = np.corrcoef(x[:-1], x[1:])[0, 1]
    if np.isnan(rho) or rho < 0:
        rho = 0
    return max(n * (1 - rho) / (1 + rho), 10.0)


def t_adjusted(lo, hi):
    """AR(1) 调整 Welch's t（同 v2）"""
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
# 优化5: divergence 自适应权重
# ═══════════════════════════════════════════════════════════

def divergence_adaptive(curve_a, curve_b, min_weight_shape=0.3, max_weight_shape=0.7):
    """
    v3 改进: 根据差分序列波动量自适应调整 shape/scale 权重。

    思路: 差分序列低波动 → Spearman r 容易被噪声主导 → 降低 shape 权重
          差分序列高波动 → 形状信息可信 → 保持高 shape 权重

    权重: w_shape = min(max(shape_weight, min_weight_shape), max_weight_shape)
           w_scale = 1 - w_shape

    基准 70/30 当 CV ≈ 1 时；CV < 0.5 时降至 30/70。
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

    # 自适应权重: CV 越低 → shape 权重越低
    cv_a = float(np.std(da) / (abs(np.mean(da)) + 1e-12))
    cv_b = float(np.std(db) / (abs(np.mean(db)) + 1e-12))
    cv_mean = (cv_a + cv_b) / 2
    # sigmoid: CV=0 → 0.3, CV=1 → 0.7, CV→∞ → 0.7
    shape_weight = min_weight_shape + (max_weight_shape - min_weight_shape) * np.tanh(cv_mean)

    shape_div = 1 - abs(r)
    sa, sb = abs(da).sum(), abs(db).sum()
    scale_div = abs(sa - sb) / (sa + sb) if (sa + sb) > 0 else 0.0

    return float(shape_weight * shape_div + (1 - shape_weight) * scale_div)


# ═══════════════════════════════════════════════════════════
# 优化3: 动态 horizon 选择
# ═══════════════════════════════════════════════════════════

def select_horizons(times, dates, unique_dates, full=HORIZONS_FULL):
    """
    根据交易频率自适应选择 horizon 列表。

    检测最短 horizon (1s) 的有效点占比：
      >80% → 高频股，保留 1s
      <20% → 低频股，去掉 1s，且 3s 也可能不可靠
      else → 标准，使用 [3,5,10,30]（去掉 1s 节省计算）

    返回: (scoring_horizons, diag_horizons)
    """
    if len(unique_dates) == 0:
        return [3, 5, 10], [3, 5, 10]

    # 用第一天估算（对所有天算日均笔数）
    n_per_day = int(np.median([np.sum(dates == dd) for dd in unique_dates]))

    d = unique_dates[0]
    dmask = dates == d
    t_d = times[dmask]
    m = len(t_d)

    # 用相邻间隔估算 horizon 覆盖率（比 searchsorted 更准确）
    intervals = np.diff(t_d) if m > 1 else np.array([1.0])
    valid_1s = np.mean(intervals < 1.0)
    valid_3s = np.mean(intervals < 3.0)
    avg_interval = np.median(intervals) if len(intervals) > 0 else 1.0

    if valid_1s > 0.80 and n_per_day > 5000:
        # 高频股：加上 1s
        scoring = [1, 3, 5, 10]
    elif valid_3s < 0.30:
        # 极低频：只保留长窗口，样本少时更稳定
        scoring = [5, 10, 30]
    elif valid_1s < 0.20:
        # 低频：去掉 1s
        scoring = [3, 5, 10, 30]
    else:
        scoring = [3, 5, 10, 30]

    diag = [h for h in HORIZONS_DIAG if h in scoring]
    if len(diag) < 2:
        diag = scoring[:3] if len(scoring) >= 3 else scoring

    print(f"  交易特征: 日均{n_per_day}笔, 间隔={avg_interval*1000:.0f}ms, "
          f"1s覆盖率={valid_1s:.0%}, horizons={scoring}")

    return scoring, diag


# ═══════════════════════════════════════════════════════════
# 优化4: 异常天检测
# ═══════════════════════════════════════════════════════════

def detect_outlier_days(values, dates_list):
    """
    MAD (Median Absolute Deviation) 检测异常天。

    返回: outlier_mask (bool array, True=异常)
    """
    if len(values) < 4:
        return np.zeros(len(values), dtype=bool)
    median = np.median(values)
    mad = np.median(np.abs(values - median))
    if mad < 1e-12:
        return np.zeros(len(values), dtype=bool)
    z = np.abs(values - median) / (mad * 1.4826)
    return z > 2.5  # 2.5 MAD


# ═══════════════════════════════════════════════════════════
# 优化6: 信号强度
# ═══════════════════════════════════════════════════════════

def signal_strength(t_vals, ci_results):
    """
    基于 t 值和 CI 判断信号强度。

    ★★★ 强: 所有 horizon t_adj ≥ 3.0 且所有 CI 不跨零
    ★★  中: 多数 horizon t_adj ≥ 2.0 且多数 CI 不跨零
    ★   弱: 部分 horizon 显著
    -   无效: 无显著

    返回: (label, summary_dict)
    """
    t_adj_vals = []
    ci_pass = []
    for key, r in ci_results.items():
        if r is None:
            continue
        t = r.get('t_adjusted')
        if t is not None and not np.isnan(t):
            t_adj_vals.append(t)
        ci_pass.append(r.get('ci_excludes_zero', False))

    if not t_adj_vals:
        return '-', {'level': 0}

    t_mean = np.mean(t_adj_vals)
    t_min = np.min(t_adj_vals)
    ci_rate = np.mean(ci_pass) if ci_pass else 0

    if t_min >= 3.0 and ci_rate >= 0.9:
        return '★★★', {'level': 3, 't_mean': t_mean, 't_min': t_min, 'ci_rate': ci_rate}
    elif t_min >= 2.0 and ci_rate >= 0.6:
        return '★★', {'level': 2, 't_mean': t_mean, 't_min': t_min, 'ci_rate': ci_rate}
    elif ci_rate > 0:
        return '★', {'level': 1, 't_mean': t_mean, 't_min': t_min, 'ci_rate': ci_rate}
    else:
        return '-', {'level': 0, 't_mean': t_mean, 't_min': t_min, 'ci_rate': ci_rate}


# ═══════════════════════════════════════════════════════════
# 优化2: 三级回退 + 优化1: 自适应 t0_rough
# ═══════════════════════════════════════════════════════════

def _try_2group_fallback(times, prices, dir_v, turnovers, dates, unique_dates,
                         impacts, nf, t1_cands, horizons, unique_reason):
    """
    3-group 失败后的 2-group 回退（v1 模式 + v2 信号）。
    如果 2-group 也失败，返回 (None, None)。
    """
    per_day = {}
    all_scores = []
    for d in unique_dates:
        dmask = dates == d
        ts_d = times[dmask]
        nf_d = nf[dmask]
        to_d = turnovers[dmask]

        best_score = -1
        best_t1 = float(t1_cands[0])

        for t1 in t1_cands:
            n_lo = int(np.sum(to_d < t1))
            n_hi = int(np.sum(to_d >= t1))
            if n_lo < 50 or n_hi < 50:
                continue

            t_vals = []
            for h in horizons:
                imp_d = impacts[h][dmask]
                valid_d = ~np.isnan(imp_d)
                lo = imp_d[valid_d & (to_d < t1)]
                hi = imp_d[valid_d & (to_d >= t1)]
                if len(lo) >= 30 and len(hi) >= 30:
                    t_adj = t_adjusted(lo, hi)[0]
                    if not np.isnan(t_adj):
                        t_vals.append(t_adj)
            if len(t_vals) < 2:
                continue
            t_mean = float(np.mean(t_vals))

            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t1] = 1
            curves = compute_netflow_curve(ts_d, nf_d, labels, 2)
            div_val = divergence_adaptive(curves[0], curves[1])
            score = 0.6 * np.tanh(t_mean / 10.0) + 0.4 * div_val
            all_scores.append(score)
            if score > best_score:
                best_score = score
                best_t1 = float(t1)

        if best_score >= 0:
            per_day[str(d)] = {'best_t1': best_t1, 'score': best_score}

    if len(per_day) < 2:
        return None, {'method': 'none', 'reason': '2-group <2天有效'}

    all_t1s = [per_day[str(d)]['best_t1'] for d in unique_dates if str(d) in per_day]

    # 异常天检测
    outlier_mask = detect_outlier_days(np.array(all_t1s), [d for d in unique_dates if str(d) in per_day])
    clean_t1s = [all_t1s[i] for i in range(len(all_t1s)) if not outlier_mask[i]]

    if len(clean_t1s) < 2:
        return None, {'method': 'none', 'reason': '2-group 去异常后<2天'}

    median_t1 = float(np.median(clean_t1s))
    peak = float(max(all_scores)) if all_scores else 0
    med_score = float(np.median(all_scores)) if all_scores else 0

    # 低数据量时放宽阈值：2~3天 flatness_ratio 可能接近 1.0 是正常的
    n_days = len(per_day)
    if n_days <= 3:
        flat_threshold = 1.03   # 2~3天几乎必然平坦
        unstable_cv = 150       # 放宽 CV 阈值
    else:
        flat_threshold = 1.15
        unstable_cv = 80

    is_flat = (peak / max(med_score, 1e-6)) < flat_threshold
    t1_cv = float(np.std(clean_t1s) / np.mean(clean_t1s) * 100) if np.mean(clean_t1s) > 0 else None
    is_unstable = t1_cv is not None and t1_cv > unstable_cv

    if is_flat or is_unstable:
        flat_reason = f'评分平坦(ratio={peak/max(med_score,1e-6):.2f})' if is_flat else f'CV={t1_cv:.0f}%'
        return None, {'method': 'none', 'reason': f'2-group {flat_reason} (n={n_days})'}

    n_outlier = sum(outlier_mask)
    msg = f'3-group {unique_reason}，2-group 回退成功'
    if n_outlier > 0:
        msg += f' (排除{n_outlier}异常天)'

    return median_t1, {
        'method': 'adaptive_2group',
        'fallback_reason': f'3-group: {unique_reason}',
        'per_day': per_day,
        'summary': {
            't1_median': median_t1,
            't1_mean': float(np.mean(clean_t1s)),
            't1_std': float(np.std(clean_t1s)),
            't1_cv': t1_cv,
            'n_days': len(per_day),
            'n_outlier_days': int(n_outlier),
            'peak_score': peak,
            'median_score': med_score,
            'msg': msg,
        }
    }


def detect_t1_anchor_v3(times, prices, dir_v, turnovers, dates, unique_dates):
    """
    阶段0 v3 — 自适应 T1 锚点检测。

    优化点（对比 v2）：
    1. t0_rough = min(P50, 8e4) 替代 max(P50, 8e4)
       → 低价股用实际 P50，高价股 capped at 8万
    2. 三级回退：3-group → 2-group → K2
    3. 动态 horizon（高频+1s，低频去1s）
    4. 异常天排除
    5. divergence_adaptive 替代 divergence_diff
    """
    p50 = float(np.percentile(turnovers, 50))
    p75 = float(np.percentile(turnovers, 75))
    p98 = float(np.percentile(turnovers, 98))

    # 优化1: t0_rough 自适应 —— 低价股用实际 P50，高价股 capped
    t0_rough = max(min(p50, 8e4), 2e4)
    lo_bound = max(p75, 15e4)

    if p98 <= lo_bound * 1.2:
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else lo_bound * 2,
                {'method': 'range_too_narrow', 'p75': p75, 'p98': p98,
                 't0_rough': t0_rough})

    # 优化3: 动态 horizon
    scoring_h, _ = select_horizons(times, dates, unique_dates)

    t1_cands = np.unique(np.round(np.logspace(
        np.log10(lo_bound), np.log10(p98), 50), -3))

    impacts = {}
    for h in scoring_h:
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
            n_mid = int(np.sum((to_d >= t0_rough) & (to_d < t1)))
            n_hi = int(np.sum(to_d >= t1))
            if n_mid < 50 or n_hi < 50:
                continue

            t_vals = []
            for h in scoring_h:
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

            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t0_rough] = 1
            labels[to_d >= t1] = 2
            curves = compute_netflow_curve(ts_d, nf_d, labels, 3)
            div_val = divergence_adaptive(curves[1], curves[2])

            score = 0.6 * np.tanh(t_mean / 10.0) + 0.4 * div_val
            all_scores.append(score)

            if score > best_score:
                best_score = score
                best_t1 = float(t1)

        if best_score >= 0:
            per_day[str(d)] = {'best_t1': best_t1, 'score': best_score}

    # ── 3-group 失败 → 2-group 回退 ──
    if not per_day:
        t1_2g, meta_2g = _try_2group_fallback(
            times, prices, dir_v, turnovers, dates, unique_dates,
            impacts, nf, t1_cands, scoring_h, '无数据')
        if t1_2g is not None:
            print(f"  {meta_2g['summary']['msg']}")
            return t1_2g, meta_2g
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else 300e4,
                {'method': 'k2_fallback_no_data', 'per_day': {},
                 't0_rough': t0_rough})

    all_t1s = [per_day[str(d)]['best_t1'] for d in unique_dates if str(d) in per_day]

    # 优化4: 异常天检测
    valid_dates = [d for d in unique_dates if str(d) in per_day]
    outlier_mask = detect_outlier_days(np.array(all_t1s), valid_dates)
    clean_t1s = [all_t1s[i] for i in range(len(all_t1s)) if not outlier_mask[i]]

    if len(clean_t1s) < 2:
        t1_2g, meta_2g = _try_2group_fallback(
            times, prices, dir_v, turnovers, dates, unique_dates,
            impacts, nf, t1_cands, scoring_h, '异常天后<2天')
        if t1_2g is not None:
            print(f"  {meta_2g['summary']['msg']}")
            return t1_2g, meta_2g
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else 300e4,
                {'method': 'k2_fallback_no_data', 'per_day': per_day,
                 't0_rough': t0_rough})

    median_t1 = float(np.median(clean_t1s))
    n_outlier = int(sum(outlier_mask))

    peak = float(max(all_scores)) if all_scores else 0
    med_score = float(np.median(all_scores)) if all_scores else 0

    # 低数据量时放宽阈值
    n_eff_days = len(clean_t1s)
    if n_eff_days <= 3:
        flat_threshold = 1.03
        unstable_cv_th = 150
    else:
        flat_threshold = 1.15
        unstable_cv_th = 80

    is_flat = (peak / max(med_score, 1e-6)) < flat_threshold
    t1_cv = float(np.std(clean_t1s) / np.mean(clean_t1s) * 100) if np.mean(clean_t1s) > 0 else None
    is_unstable = t1_cv is not None and t1_cv > unstable_cv_th

    # ── 平坦或不稳定 → 2-group 回退 ──
    if is_flat or is_unstable:
        reason = '评分平坦' if is_flat else f'CV={t1_cv:.0f}%'
        t1_2g, meta_2g = _try_2group_fallback(
            times, prices, dir_v, turnovers, dates, unique_dates,
            impacts, nf, t1_cands, scoring_h, reason)
        if t1_2g is not None:
            print(f"  {meta_2g['summary']['msg']}")
            return t1_2g, meta_2g
        # 三级: 最终退到 K2
        k2 = _k2_boundary(turnovers, dates, times)
        if k2:
            return k2, {
                'method': 'k2_fallback',
                'fallback_reason': f'3-group {reason}→2-group也失败',
                'per_day': per_day,
                'summary': {'peak_score': peak, 'median_score': med_score,
                            'flatness_ratio': peak / max(med_score, 1e-6),
                            't1_cv': t1_cv, 'k2_boundary': k2,
                            't0_rough': t0_rough}
            }

    # 3-group 成功
    msg = f'T1={median_t1/1e4:.0f}万 方法=3-group  CV={t1_cv:.0f}%'
    if n_outlier > 0:
        msg += f' (排除{n_outlier}异常天)'

    return median_t1, {
        'method': 'adaptive_3group',
        'per_day': per_day,
        'summary': {
            't1_median': median_t1,
            't1_mean': float(np.mean(clean_t1s)),
            't1_std': float(np.std(clean_t1s)),
            't1_cv': t1_cv,
            'search_range': [lo_bound, p98],
            't0_rough': t0_rough,
            'n_days': len(per_day),
            'n_outlier_days': n_outlier,
            'peak_score': peak,
            'median_score': med_score,
            'horizons': scoring_h,
            'msg': msg,
        }
    }


# ═══════════════════════════════════════════════════════════
# T2 搜索 (v2 基础上: divergence_adaptive)
# ═══════════════════════════════════════════════════════════

def search_t2_given_t1_v3(times, turnovers, dir_v, dates, unique_dates,
                           t1_anchor=300e4, t2_max=5000e4, min_sep_ratio=1.5):
    """阶段1 v3 — 固定 T1，搜索 T2（用 divergence_adaptive）"""
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
                to_d, dir_d, (t1_anchor, t2), (t2, float('inf')))

            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t1_anchor] = 1
            labels[to_d >= t2] = 2
            curves = compute_netflow_curve(ts_d, nf_d, labels, 3)

            # v3: divergence_adaptive
            d_large_xl = divergence_adaptive(curves[1], curves[2])
            score = d_large_xl * (1 + 0.5 * int(flip))

            if score > best_score:
                best_score = score
                best_t2 = t2
                best_flip = flip
                best_net_large = net_large
                best_net_xl = net_xl

        per_day[str(d)] = {
            'best_t2': float(best_t2), 'score': float(best_score),
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


# ═══════════════════════════════════════════════════════════
# T1 微调 (v2 基础上: 异常天排除 + 动态 horizon)
# ═══════════════════════════════════════════════════════════

def fine_tune_t1_v3(times, prices, dir_v, turnovers, dates, unique_dates,
                     t0, t2, t1_range=None, horizons=None):
    """
    阶段3 v3 — 固定 T0/T2，微调 T1。

    v3 优化：
    1. 异常天排除（跨天聚合时忽略 outlier）
    2. 动态 horizon
    """
    nf = dir_v * turnovers

    if t1_range is None:
        t1_min = max(t0 * 1.5, 50e4)
        t1_max = t2 * 0.5
        t1_range = (t1_min, t1_max)

    if t1_range[0] >= t1_range[1]:
        return {'t1': None, 'note': 'T0~T2空间不足，跳过微调'}

    if horizons is None:
        horizons = [3, 5, 10]

    t1_cands = np.unique(np.round(np.logspace(
        np.log10(t1_range[0]), np.log10(t1_range[1]), 50), -3))

    impacts = {}
    for h in horizons:
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

            t_vals = []
            for h in horizons:
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

            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t0] = 1
            labels[to_d >= t1] = 2
            labels[to_d >= t2] = 3
            curves = compute_netflow_curve(ts_d, nf_d, labels, 4)
            div_all.append(divergence_adaptive(curves[1], curves[2]))

            flip, _, _ = direction_flip(to_d, dir_d, (t0, t1), (t1, t2))
            if flip:
                flip_count += 1

        if n_valid_days < 3 or len(t_all) < 3:
            continue

        flip_rate = flip_count / n_valid_days
        t_mean_agg = np.mean(t_all) if t_all else 0
        div_mean = np.mean(div_all) if div_all else 0

        flip_score = max(0, 2 * flip_rate - 1)
        score = 0.6 * np.tanh(t_mean_agg / 10.0) + 0.4 * flip_score
        all_scores.append((t1, score, t_mean_agg, div_mean, flip_rate))

        if score > best_score:
            best_score = score
            best_t1 = float(t1)

    return {
        't0_fixed': t0, 't2_fixed': t2,
        'best_t1': float(best_t1), 'best_score': float(best_score),
        'summary': {
            't1_final': float(best_t1), 'score': float(best_score),
            'top5': sorted(all_scores, key=lambda x: x[1], reverse=True)[:5],
        }
    }


# ═══════════════════════════════════════════════════════════
# T0 检测 (v2 基础上: min_samples 降低 + 动态 horizon)
# ═══════════════════════════════════════════════════════════

def auto_detect_t0_v3(times, prices, dir_v, turnovers, dates, unique_dates,
                       horizons=None, t1=300e4):
    """
    阶段2 v3 — 自动探测小/中单分界线 T0。

    v3 优化：
    - 动态 horizon + 降低最小样本要求（对低价/低频股友好）
    """
    if horizons is None:
        horizons = [3, 5, 10]

    t0_cands = np.unique(np.round(np.logspace(np.log10(T0_MIN), np.log10(35e4), 80), -2))
    t0_cands = t0_cands[(t0_cands >= T0_MIN) & (t0_cands <= T0_MAX)]

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
                if len(lo) < 15 or len(hi) < 15:  # v3: 降低到15（对低频友好）
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


def select_t0_v3(t0_results, t0_max=T0_MAX):
    """多窗口一致性选择 T0（同 v2）"""
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
        return 15e4, {}, 0

    best_t0s = [v['t0'] for v in per_horizon.values()]
    cv_t0s = np.std(best_t0s) / np.mean(best_t0s) * 100 if np.mean(best_t0s) > 0 else 0
    median_t0 = float(np.median(best_t0s))
    return median_t0, per_horizon, cv_t0s


# ═══════════════════════════════════════════════════════════
# Block bootstrap 验证
# ═══════════════════════════════════════════════════════════

def _estimate_block_size(x):
    """用 ACF 估计 block size"""
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
        if np.mean(x[:n - k] * x[k:]) / var < 0:
            return max(k, 5)
    return max_lag


def _block_indices(n, block_size):
    """Block bootstrap 索引"""
    n_blocks = int(np.ceil(n / block_size))
    starts = np.random.randint(0, max(n - block_size + 1, 1), size=n_blocks)
    idx = np.concatenate([np.arange(s, min(s + block_size, n)) for s in starts])
    return idx[:n]


def block_bootstrap_ci(lo, hi, n_boot=300):
    """Block bootstrap 95% CI"""
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


def validate_with_bootstrap(times, prices, dir_v, turnovers, dates,
                            t0, t1, t2, horizons=None):
    """对最终阈值做 block bootstrap 验证（同 v2）"""
    if horizons is None:
        horizons = HORIZONS_DIAG

    boundaries = [
        ('T0', t0, 0, t1),
        ('T1', t1, t0, t2),
        ('T2', t2, t1, float('inf')),
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
            t_adj_val, mean_diff, lo_mean, hi_mean, n_eff_lo, n_eff_hi, n_lo, n_hi = t_adjusted(lo, hi)

            se_raw = np.sqrt(np.var(lo[~np.isnan(lo)]) / n_lo + np.var(hi[~np.isnan(hi)]) / n_hi)
            t_raw = abs(mean_diff) / max(se_raw, 1e-12) if not np.isnan(mean_diff) else np.nan

            results[f'{name}_h{h}s'] = {
                'observed_diff': float(obs) if not np.isnan(obs) else None,
                'ci_low': float(ci_lo) if not np.isnan(ci_lo) else None,
                'ci_high': float(ci_hi) if not np.isnan(ci_hi) else None,
                'ci_excludes_zero': bool(ci_lo > 0 or ci_hi < 0) if not (np.isnan(ci_lo) or np.isnan(ci_hi)) else False,
                't_raw': float(t_raw),
                't_adjusted': float(t_adj_val) if not np.isnan(t_adj_val) else None,
                'inflation_ratio': float(t_raw / t_adj_val) if t_adj_val and not np.isnan(t_adj_val) and t_adj_val > 0 else None,
                'lo_mean': float(lo_mean) if not np.isnan(lo_mean) else None,
                'hi_mean': float(hi_mean) if not np.isnan(hi_mean) else None,
                'n_lo': int(n_lo), 'n_hi': int(n_hi),
                'n_eff_lo': float(n_eff_lo), 'n_eff_hi': float(n_eff_hi),
            }

    return results


# ═══════════════════════════════════════════════════════════
# v1 管线（用于对比）
# ═══════════════════════════════════════════════════════════

def run_v1_pipeline(times, prices, dir_v, turnovers, dates, unique_dates):
    """跑 v1 4 阶段管线，提取 T0/T1/T2"""
    T1_v1, t1_meta_v1 = _detect_t1_anchor_v1(
        turnovers, prices, dir_v, times, dates, unique_dates)

    t2_results_v1 = _search_t2_v1(
        times, turnovers, dir_v, dates, unique_dates, t1_anchor=T1_v1)
    T2_v1 = t2_results_v1['summary']['t2_median'] or 1000e4

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
# v3 主流程
# ═══════════════════════════════════════════════════════════

def run_analysis_v3(times, dates, prices, turnovers, dir_v, ttypes, unique_dates):
    """v3 主流程"""
    results = {}

    # ── 1. 金额分布 ──
    print("\n" + "=" * 60)
    print("1. 金额分布")
    print("=" * 60)
    for p in [50, 75, 90, 95, 98, 99, 99.5]:
        v = np.percentile(turnovers, p)
        print(f"  P{p:5.1f}: {v:>12,.0f} (¥{v/1e4:.0f}万)")

    # ── 2. v1 vs v3 对比 ──
    print("\n" + "=" * 60)
    print("2. v1 vs v3 阈值检测对比")
    print("=" * 60)

    print("\n  [v1] tick-based + 原始 Welch's t")
    v1 = run_v1_pipeline(times, prices, dir_v, turnovers, dates, unique_dates)
    print(f"    T0={v1['T0']/1e4:.0f}万  T1={v1['T1']/1e4:.0f}万  T2={v1['T2']/1e4:.0f}万")
    print(f"    T1方法={v1['t1_method']}  T1_CV={v1['t1_cv']:.0f}%  T2_CV={v1['t2_cv']:.0f}%"
          if v1['t1_cv'] else f"    T1方法={v1['t1_method']}")

    print(f"\n  [v3] time-based + AR(1) + 自适应")

    # ── 阶段0 ──
    T1_v3, t1_meta_v3 = detect_t1_anchor_v3(
        times, prices, dir_v, turnovers, dates, unique_dates)
    s0 = t1_meta_v3.get('summary', {})
    msg = s0.get('msg', '')
    print(f"    阶段0: T1={T1_v3/1e4:.0f}万  方法={t1_meta_v3['method']}  {msg}")

    # ── 阶段1 ──
    t2_results_v3 = search_t2_given_t1_v3(
        times, turnovers, dir_v, dates, unique_dates, t1_anchor=T1_v3)
    T2_v3 = t2_results_v3['summary']['t2_median'] or 1000e4
    s2 = t2_results_v3['summary']
    print(f"    阶段1: T2={T2_v3/1e4:.0f}万  CV={s2['t2_cv']:.0f}%  "
          f"翻转={s2['n_flip_days']}/{s2['n_days']}天")

    # ── 阶段2 ──
    # 动态 horizon
    _, diag_h = select_horizons(times, dates, unique_dates)
    t0_results_v3 = auto_detect_t0_v3(
        times, prices, dir_v, turnovers, dates, unique_dates,
        horizons=diag_h, t1=T1_v3)
    T0_v3, t0_per_h, t0_cv = select_t0_v3(t0_results_v3)
    print(f"    阶段2: T0={T0_v3/1e4:.0f}万 (多窗口中位数, CV={t0_cv:.0f}%)")
    print(f"    各窗口最优 T0:")
    for key, val in sorted(t0_per_h.items()):
        print(f"      {key}: {val['t0']/1e4:.0f}万  (mean_t={val['mean_t']:.1f})")

    # ── 阶段3 ──
    fine_t1_v3 = fine_tune_t1_v3(times, prices, dir_v, turnovers, dates, unique_dates,
                                   t0=T0_v3, t2=T2_v3, horizons=diag_h)
    if fine_t1_v3.get('best_t1') is not None:
        new_t1 = fine_t1_v3['best_t1']
        delta_pct = (new_t1 - T1_v3) / T1_v3 * 100
        if abs(delta_pct) > 5:
            T1_v3_final = new_t1
            print(f"    阶段3: T1微调 {T1_v3/1e4:.0f}万→{new_t1/1e4:.0f}万 (Δ={delta_pct:+.0f}%, 已采纳)")
            top5 = fine_t1_v3['summary'].get('top5', [])
            if top5:
                print(f"    Top5 候选 (跨天聚合评分):")
                for t1, sc, t_m, dv, fr in top5:
                    print(f"      {t1/1e4:>6.0f}万  score={sc:.4f}  t={t_m:.1f}  div={dv:.4f}  翻转率={fr:.0%}")
        else:
            T1_v3_final = T1_v3
            print(f"    阶段3: T1微调 Δ={delta_pct:+.0f}% ≤5%, 保持锚点")
    else:
        T1_v3_final = T1_v3
        print(f"    阶段3: 跳过（{fine_t1_v3.get('note', '空间不足')}）")

    v3 = {'T0': T0_v3, 'T1': float(T1_v3_final), 'T2': float(T2_v3)}

    # 对比表
    print(f"\n  {'':>12} {'v1':>12} {'v3':>12} {'Δ%':>8}")
    print(f"  {'-'*12} {'-'*12} {'-'*12} {'-'*8}")
    for name in ['T0', 'T1', 'T2']:
        v1v = v1[name]
        v3v = v3[name]
        dp = (v3v - v1v) / v1v * 100
        print(f"  {name+' (万)':>12} {v1v/1e4:>10.0f}  {v3v/1e4:>10.0f}  {dp:>+7.0f}%")

    results['v1_thresholds'] = v1
    results['v3_thresholds'] = v3

    # ── 3. AR(1) 自相关诊断 ──
    print("\n" + "=" * 60)
    print("3. AR(1) 自相关诊断（v3 动态阈值）")
    print("=" * 60)
    print(f"  阈值来源: v3 T0={T0_v3/1e4:.0f}万, T1={T1_v3_final/1e4:.0f}万, T2={T2_v3/1e4:.0f}万")

    for h in diag_h:
        imp = compute_price_impact_tb(times, prices, dir_v, h, dates)
        valid = ~np.isnan(imp)
        print(f"\n  horizon={h}s:")
        for label, lo_th, hi_th in [
            (f'小单(<{T0_v3/1e4:.0f}万)', 0, T0_v3),
            (f'中单({T0_v3/1e4:.0f}~{T1_v3_final/1e4:.0f}万)', T0_v3, T1_v3_final),
            (f'大单({T1_v3_final/1e4:.0f}~{T2_v3/1e4:.0f}万)', T1_v3_final, T2_v3),
            (f'特大单(≥{T2_v3/1e4:.0f}万)', T2_v3, float('inf')),
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

    # ── 4. Block bootstrap + 信号强度 ──
    print("\n" + "=" * 60)
    print("4. Block bootstrap 验证 + 信号强度（v3 动态阈值）")
    print("=" * 60)

    bs_results = validate_with_bootstrap(
        times, prices, dir_v, turnovers, dates,
        v3['T0'], v3['T1'], v3['T2'], horizons=diag_h)

    print(f"\n  {'边界':>6} {'h(s)':>4} {'均值差':>10} {'CI_low':>10} {'CI_high':>10} "
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

    # 信号强度
    print(f"\n  信号强度评估:")
    for boundary in ['T0', 'T1', 'T2']:
        boundary_keys = {k: v for k, v in bs_results.items() if k.startswith(boundary)}
        label, info = signal_strength([], boundary_keys)
        t_mean = info.get('t_mean', 0)
        ci_rate = info.get('ci_rate', 0)
        print(f"    {boundary}: {label}  (t_mean={t_mean:.1f}, CI通过率={ci_rate:.0%})")

    # ── 5. v1 阈值在 v3 信号下交叉验证 ──
    print("\n" + "=" * 60)
    print("5. v1 阈值在 v3 信号下的验证（交叉对比）")
    print("=" * 60)

    bs_v1 = validate_with_bootstrap(
        times, prices, dir_v, turnovers, dates,
        v1['T0'], v1['T1'], v1['T2'], horizons=diag_h)

    print(f"\n  {'边界':>6} {'h(s)':>4} {'均值差':>10} {'CI_low':>10} {'CI_high':>10} "
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

    results['v1_thresholds_v3_signal'] = bs_v1

    # ── 6. 总结 ──
    print("\n" + "=" * 60)
    print("6. 总结")
    print("=" * 60)

    inflations = []
    for key, r in bs_results.items():
        if r and r['inflation_ratio']:
            inflations.append(r['inflation_ratio'])
    infl_mean = np.mean(inflations) if inflations else 0

    sig_v3 = sum(1 for r in bs_results.values() if r and r['ci_excludes_zero'])
    total_v3 = sum(1 for r in bs_results.values() if r is not None)

    print(f"""
  v1 (tick-based + 原始t):
    小单 < {v1['T0']/1e4:.0f}万 | 中单 {v1['T0']/1e4:.0f}~{v1['T1']/1e4:.0f}万 |
    大单 {v1['T1']/1e4:.0f}~{v1['T2']/1e4:.0f}万 | 特大单 ≥{v1['T2']/1e4:.0f}万

  v3 (time-based + AR(1) + 自适应回退):
    小单 < {v3['T0']/1e4:.0f}万 | 中单 {v3['T0']/1e4:.0f}~{v3['T1']/1e4:.0f}万 |
    大单 {v3['T1']/1e4:.0f}~{v3['T2']/1e4:.0f}万 | 特大单 ≥{v3['T2']/1e4:.0f}万

  改进项:
    - t0_rough 自适应: min(P50, 8e4)，对低价股降低中单门槛
    - 三级回退: 3-group → 2-group → K2，薄尾股不再硬退到K2
    - 动态 horizon: 根据交易频率自适应选择窗口列表
    - 异常天排除: MAD 检测跨天聚合 outlier
    - divergence 自适应权重: 差分低波动时降低 shape 权重
    - 信号强度分层: ★★★/★★/★/-
    - AR(1) 膨胀均值: {infl_mean:.1f}×
    - v3 阈值 bootstrap 显著: {sig_v3}/{total_v3}
""")

    return results


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='订单大小阈值分析 v3 (鲁棒性优化)')
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

    results = run_analysis_v3(times, dates, prices, turnovers, dir_v, ttypes, ud)

    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n结果已保存: {args.output}")

    print(f"\n总耗时: {time.time() - t0:.1f}s")
