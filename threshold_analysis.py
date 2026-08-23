"""
订单大小阈值分析工具
=====================
功能：基于逐笔成交数据，通过「价格冲击差异」自动探测最优订单大小分界阈值，
      将订单分为 4 档（小/中/大/特大单），并与富途按比例方案做横向对比。

核心方法论 — 4 阶段自适应流水线：
  阶段0: 自适应检测 T1 锚点（中/大单自然分界）
         - 在 [P75, P98] 内扫描，多窗口 t 检验 + 净流入曲线 divergence 综合评分
         - 评分平坦或跨天 unstable → 自动下滑到 K=2 聚类边界
  阶段1: 固定 T1，搜索 T2（大/特大单边界）
         - 目标: 最大化 divergence(大单净流入曲线, 特大单净流入曲线)
         - 方向翻转（两档净方向相反）加分 50%
  阶段2: 固定 T1，搜索 T0（小/中单边界）
         - 多窗口价格冲击 t 统计量跨天取均值最优
         - T0_MAX=25万 约束避免中单组过小
  阶段3: 固定 T0+T2，微调 T1
         - 目标: 最大化 min(div(中,大), div(大,特大))

四档定义：
  小单：turnover < T0
  中单：T0 ≤ turnover < T1
  大单：T1 ≤ turnover < T2
  特大单：turnover ≥ T2

用法：
    python3 threshold_analysis.py                          # 默认：HK.00700, 最近14个交易日
    python3 threshold_analysis.py --stock HK.00700 --days 10  # 最近10个交易日
    python3 threshold_analysis.py --start 2026-06-15 --end 2026-06-19

输出章节：
    1. 金额分布概览（分位数）
    2. 多窗口价格冲击 t 统计量（固定参考阈值对照）
    3. 自适应扫描：锚点检测 → T2/T0 搜索 → T1 微调
    4. 稳定度验证（train/test split + 留一天交叉验证）
    5. ≥50万订单内部 K=2 聚类（探索"小大单 vs 大-大单"子结构）
    6b. 分档方法对比（绝对金额 vs 富途按比例）
    6. 最终推荐阈值
"""

import argparse
import numpy as np
import time
import json
import warnings
from collections import deque
import psycopg2
from db import DB_CONFIG
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans


# ═══════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════

def _get_trading_dates(conn, stock_code, n_days):
    """查询该股票最近 N 个有数据的交易日"""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT DISTINCT DATE(tick_time) AS d
        FROM tick_data
        WHERE stock_code = '{stock_code}'
          AND ticker_direction IN ('BUY','SELL')
        ORDER BY d DESC
        LIMIT {n_days}
    """)
    return [str(r[0]) for r in cur.fetchall()]


def load_data(stock_code="HK.00700", start_date=None, end_date=None, days=None):
    """
    从 tick_data 表加载逐笔成交数据。

    参数:
        stock_code: 股票代码 (e.g. 'HK.00700')
        start_date/end_date: 日期范围 (YYYY-MM-DD)
        days: 最近 N 个交易日（从 DB 实际查询，不含周末/节假日）
              默认 None → 最近 14 个交易日

    返回:
        times:      float64[], Unix 时间戳
        dates:      object[], 日期 (date 类型)
        prices:     float64[], 成交价
        turnovers:  float64[], 成交额
        dir_v:      int64[], 方向向量 (BUY=+1, SELL=-1)
        ttypes:     object[], 成交类型 (AUTO_MATCH/LATE/NON_AUTO_MATCH/...)
        unique_dates: list[str], 唯一日期列表（升序）

    注: 仅取 ticker_direction IN ('BUY','SELL') 的自动撮合成交，
        排除 NONE/NEUTRAL 方向未知的单子。
    """
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    if start_date and end_date:
        where = f"tick_time >= '{start_date}' AND tick_time < '{end_date}'"
    elif days:
        # 从 DB 中取最近 N 个实际有数据的交易日
        trading_dates = _get_trading_dates(conn, stock_code, days)
        if not trading_dates:
            raise ValueError(f"数据库中无 {stock_code} 数据")
        where = f"DATE(tick_time) IN ({','.join(f"'{d}'" for d in trading_dates)})"
    else:
        # 默认：最近 14 个交易日
        trading_dates = _get_trading_dates(conn, stock_code, 14)
        if not trading_dates:
            raise ValueError(f"数据库中无 {stock_code} 数据")
        where = f"DATE(tick_time) IN ({','.join(f"'{d}'" for d in trading_dates)})"

    sql = f"""
        SELECT tick_time, price, turnover, ticker_direction, tick_type
        FROM tick_data
        WHERE stock_code = '{stock_code}'
          AND {where}
          AND ticker_direction IN ('BUY','SELL')
        ORDER BY tick_time
    """

    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        raise ValueError(f"无数据: {sql}")

    times = np.array([r[0].timestamp() for r in rows])
    dates = np.array([r[0].date() for r in rows])
    prices = np.array([float(r[1]) for r in rows])
    turnovers = np.array([float(r[2]) for r in rows])
    dir_v = np.where(np.array([r[3] for r in rows]) == 'BUY', 1, -1)
    ttypes = np.array([r[4] for r in rows])

    unique_dates = sorted(set(dates))
    print(f"\n数据: {len(turnovers):,} 笔, {len(unique_dates)} 天")
    print(f"日期: {[str(d) for d in unique_dates]}")
    print(f"金额: {turnovers.sum()/1e8:.2f} 亿")
    return times, dates, prices, turnovers, dir_v, ttypes, unique_dates


# ═══════════════════════════════════════════════════════════
# 指标计算
# ═══════════════════════════════════════════════════════════

def compute_price_impact(prices, dir_v, fw):
    """前向价格冲击: (price[t+fw] - price[t]) × direction[t]"""
    n = len(prices)
    imp = np.full(n, np.nan)
    imp[:n - fw] = (prices[fw:] - prices[:n - fw]) * dir_v[:n - fw]
    return imp


def compute_rel_size(dates, turnovers, times, W=1600):
    """
    相对大小: turnover / rolling_mean(turnover, W)

    含义: 一笔订单的成交额是周围 W 笔平均成交额的多少倍。
    > 1: 比近期平均大, < 1: 比近期平均小。

    按日隔离计算，避免跨日边界污染。
    W=1600: 对应约 15~30 分钟窗口（取决于该股交易频率），
            足够大以平滑噪声，又不至于跨时段混淆。
    """
    n = len(turnovers)
    rel = np.full(n, np.nan)
    unique_dates = sorted(set(dates))

    for d in unique_dates:
        dmask = dates == d
        didx = np.where(dmask)[0]
        if len(didx) < 3:
            continue
        damt = turnovers[dmask]
        buf = deque(maxlen=W)
        s = 0.0
        for ii in range(len(didx)):
            gi = didx[ii]
            a = damt[ii]
            buf.append(a)
            s += a
            if len(buf) >= 3:
                nv = min(len(buf), W)
                mean_a = s / nv
                rel[gi] = a / mean_a if mean_a > 0 else 1.0
            if len(buf) == W:
                s -= buf[0]
    return rel


# ═══════════════════════════════════════════════════════════
# 统计工具
# ═══════════════════════════════════════════════════════════

def t_at_threshold(turnovers, impact, th, mask=None):
    """
    在阈值 th 处计算两组均值差异的 Welch's t 统计量。

    分成两组: lo = {turnover < th}, hi = {turnover ≥ th}
    计算 hi 的价格冲击均值是否显著不同于 lo。

    返回: (t, lo_mean, hi_mean, lo_n, hi_n)
    若任一组样本 < 10 则返回 NaN。
    """
    if mask is None:
        mask = np.ones(len(turnovers), dtype=bool)
    valid = mask & ~np.isnan(impact)
    lo = impact[valid & (turnovers < th)]
    hi = impact[valid & (turnovers >= th)]
    if len(lo) < 10 or len(hi) < 10:
        return np.nan, np.nan, np.nan, len(lo), len(hi)
    se = np.sqrt(np.nanvar(lo) / len(lo) + np.nanvar(hi) / len(hi))
    t = abs(np.nanmean(hi) - np.nanmean(lo)) / max(se, 1e-12)
    return t, np.nanmean(lo), np.nanmean(hi), len(lo), len(hi)


def scan_thresholds(turnovers, impact, ths, mask=None):
    """扫描阈值范围，返回 (最优阈值, 最优t值, 均值列表)"""
    best_t, best_th = 0, 0
    for th in ths:
        t, _, _, _, _ = t_at_threshold(turnovers, impact, th, mask)
        if not np.isnan(t) and t > best_t:
            best_t, best_th = t, th
    return best_th, best_t


# ═══════════════════════════════════════════════════════════
# 自动分层阈值探测
# ═══════════════════════════════════════════════════════════

def compute_netflow_curve(ts, nf, labels, n_class, bin_width=30):
    """
    计算每个订单类别的累计净流入曲线（固定时间窗口）
    ts: 时间戳(秒), nf: 净流入=dir×turnover, labels: 类别标签(0..n_class-1)
    """
    t0, t1 = ts[0], ts[-1]
    nb = max(int((t1 - t0) / bin_width) + 1, 2)
    bins = np.clip(((ts - t0) / bin_width).astype(int), 0, nb - 1)
    curves = []
    for c in range(n_class):
        w = (labels == c).astype(float) * nf
        curves.append(np.bincount(bins, weights=w, minlength=nb).cumsum())
    return curves


def divergence(curve_a, curve_b):
    """
    两条累计净流入曲线的差异度：0=完全相同，1=完全不同。

    双维度组合:
      - 形状差异 (70%): 1 - |Spearman r|
        两条曲线如果走势相似（同涨同跌），即使幅度不同，r 也接近 1。
      - 幅度差异 (30%): 末值差距比例
        曲线终点的净流入量差距。比例而非绝对值，避免成交量规模影响。

    若曲线变化点 < 5 直接返回 0（无信息）。
    """
    from scipy.stats import spearmanr
    # 只保留曲线有变化的关键点
    diff_mask = np.concatenate([
        [True],
        (np.diff(curve_a) != 0) | (np.diff(curve_b) != 0)
    ])
    ac, bc = curve_a[diff_mask], curve_b[diff_mask]
    if len(ac) < 5:
        return 0.0
    try:
        r, _ = spearmanr(ac, bc)
        r = max(r, -0.99)
    except Exception:
        return 0.0
    shape_div = 1 - abs(r)
    da, db = abs(ac[-1] - ac[0]), abs(bc[-1] - bc[0])
    denom = da + db
    scale_div = abs(da - db) / denom if denom > 0 else 0.0
    return 0.7 * shape_div + 0.3 * scale_div


def auto_detect_t0(times, prices, turnovers, dir_v, dates, unique_dates,
                   fw_list=(3, 5, 10, 20), t1=300e4):
    """
    阶段2 — 自动探测小/中单分界线 T0。

    方法: 在 [1万, 30万] 范围内对数均匀扫描 80 个候选 T0，
          计算 小单(<T0) vs 中单(T0~T1) 的多窗口价格冲击 t 统计量。
          每天独立计算 → 汇总跨天均值 → 取跨天均值最优的 T0。

    T0_MAX=25万 硬约束: 防止中单组被过度切分（中单需要保留足够样本量）。
    t1: T1 锚点值，来自阶段0检测结果。

    返回: dict{
        fw_N: {
            t0_candidates: [扫描点],
            per_day_best_t0: [每天最优 T0],
            cross_day_mean_t: [跨天均值 t 向量],
            top5: [{t0, mean_t, per_day_t}]
        }
    }
    """
    from scipy.stats import ttest_ind

    t0_cands = np.unique(np.round(np.logspace(np.log10(5000), np.log10(35e4), 80), -2))
    t0_cands = t0_cands[(t0_cands >= 1e4) & (t0_cands <= 30e4)]

    all_results = {}

    for fw in fw_list:
        imp = compute_price_impact(prices, dir_v, fw)
        valid = ~np.isnan(imp)

        # 按天计算 t 矩阵
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
                t_val, _ = ttest_ind(hi, lo, equal_var=False)
                t_matrix[i, j] = abs(t_val)

        # 每天最优
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            best_idx = np.nanargmax(t_matrix, axis=0)
        best_t0s = t0_cands[best_idx]
        best_ts = np.array([t_matrix[best_idx[j], j] for j in range(len(unique_dates))])

        # 跨天均值（轴1全NaN的行产NaN，不警告）
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            mean_ts = np.nanmean(t_matrix, axis=1)
        top5_idx = np.argsort(mean_ts)[-5:][::-1]

        all_results[f'fw_{fw}'] = {
            't0_candidates': [float(x) for x in t0_cands],
            'per_day_best_t0': [float(x) for x in best_t0s],
            'per_day_best_t': [float(x) for x in best_ts],
            'cross_day_mean_t': [float(x) if not np.isnan(x) else np.nan for x in mean_ts],
            'top5': [{'t0': float(t0_cands[i]), 'mean_t': float(mean_ts[i]),
                       'per_day_t': [float(t_matrix[i, j]) if not np.isnan(t_matrix[i, j])
                                     else None for j in range(len(unique_dates))]}
                     for i in top5_idx],
        }

    return all_results


def direction_flip(turnovers, dir_v, lo, hi):
    """
    检测两组订单的净方向是否相反（方向翻转）
    返回: (是否翻转, net_lo, net_hi)
    """
    m_lo = (turnovers >= lo[0]) & (turnovers < lo[1])
    m_hi = (turnovers >= hi[0]) & (turnovers < hi[1])
    net_lo = np.sum(dir_v[m_lo] * turnovers[m_lo])
    net_hi = np.sum(dir_v[m_hi] * turnovers[m_hi])
    return net_lo * net_hi < 0, float(net_lo), float(net_hi)


def search_t2_given_t1(times, turnovers, dir_v, dates, unique_dates,
                        t1_anchor=300e4, t2_max=5000e4, min_sep_ratio=1.5):
    """
    阶段1 — 固定 T1，搜索 T2（大/特大单分界线）

    约束:
      - T2 ≥ T1_anchor × min_sep_ratio（至少1.5倍分离，避免边界挤压）
      - T2 ≤ t2_max

    搜索维度: 仅 T2（1D 扫描），T1 固定为锚点值
    评分: div(大单, 特大单) × (1 + 0.5 × 方向翻转加分)

    每天独立搜索 → 跨天汇总（中位数/均值/CV）
    返回: dict{每天最优 T2, 方向翻转, 跨天汇总}
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

            # 构造标签：0=小+中，1=大，2=特大
            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t1_anchor] = 1
            labels[to_d >= t2] = 2

            curves = compute_netflow_curve(ts_d, nf_d, labels, 3)
            d_large_xl = divergence(curves[1], curves[2])

            # 综合得分: divergence 为基础，方向翻转加分 50%
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


def fine_tune_t1(times, turnovers, dir_v, dates, unique_dates,
                 t0, t2, t1_range=None):
    """
    阶段3（可选）— 固定 T0/T2，微调 T1。

    在 T0~T2 范围内小步长扫描 T1，
    目标: 最大化 min(div(中单,大单), div(大单,特大单))
          — 让两个分界的区分度都不太差，避免偏废一端。

    约束:
      - T1 下界: max(T0×1.5, 50万) — 保证中单组有足够空间
      - T1 上界: T2×0.5           — 保证大单组有足够空间
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

    per_day = {}
    for d in unique_dates:
        dmask = dates == d
        ts_d = times[dmask]
        nf_d = nf[dmask]
        to_d = turnovers[dmask]

        best_mm = -1
        best_t1 = (t0 + t2) / 2

        for t1 in t1_cands:
            n_mid = int(np.sum((to_d >= t0) & (to_d < t1)))
            n_large = int(np.sum((to_d >= t1) & (to_d < t2)))
            if n_mid < 20 or n_large < 20:
                continue

            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t0] = 1  # 中单
            labels[to_d >= t1] = 2  # 大单
            labels[to_d >= t2] = 3  # 特大单

            curves = compute_netflow_curve(ts_d, nf_d, labels, 4)
            d_mid_large = divergence(curves[1], curves[2])
            d_large_xl = divergence(curves[2], curves[3])
            mm = min(d_mid_large, d_large_xl)
            if mm > best_mm:
                best_mm = mm
                best_t1 = t1

        per_day[str(d)] = {
            'best_t1': float(best_t1),
            'minimax': float(best_mm),
        }

    all_t1s = [per_day[str(d)]['best_t1'] for d in unique_dates]
    return {
        't0_fixed': t0,
        't2_fixed': t2,
        'per_day': per_day,
        'summary': {
            't1_median': float(np.median(all_t1s)) if all_t1s else None,
            't1_mean': float(np.mean(all_t1s)) if all_t1s else None,
            't1_std': float(np.std(all_t1s)) if all_t1s else None,
        }
    }


# ═══════════════════════════════════════════════════════════
# 阶段0: 自适应锚点检测
# ═══════════════════════════════════════════════════════════

def _k2_boundary(turnovers, dates, times):
    """
    K=2 聚类边界 — 锚点检测下滑备选方案。

    在 ≥50万 的订单内部，用 (log turnover, 相对大小) 双特征 K=2 聚类，
    返回两簇边界的几何均值 (lo 簇最大 + hi 簇最小) / 2。

    这回答了「≥50万的单子里是否存在两个自然群体」的问题，
    如果自适应扫描找不到有意义的 T1，这个自然边界就是备选。
    """
    big = turnovers >= 5e5
    if big.sum() < 100:
        return None
    rel_size = compute_rel_size(dates, turnovers, times)
    X = np.column_stack([np.log1p(turnovers[big]), rel_size[big]])
    X = np.nan_to_num(X, 0)
    X_s = StandardScaler().fit_transform(X)
    km = KMeans(n_clusters=2, random_state=42, n_init=5, max_iter=100)
    labs = km.fit_predict(X_s)
    c_meds = [np.median(turnovers[big][labs == i]) for i in range(2)]
    if c_meds[0] > c_meds[1]:
        labs = 1 - labs
    lo = turnovers[big][labs == 0]
    hi = turnovers[big][labs == 1]
    return float((lo.max() + hi.min()) / 2)


def detect_t1_anchor(turnovers, prices, dir_v, times, dates, unique_dates):
    """
    阶段0 — 自适应检测 T1 锚点（中单/大单自然分界）。

    核心思路: 在股票自身分布范围 [P75, P98] 内扫描候选 T1，
              同时考虑价格冲击区分度 + 净流入曲线形状差异，
              跨天取中位数作为锚点。

    评分公式: score = 0.6 × tanh(t_mean/10) + 0.4 × divergence
      - t_mean: 多窗口(fw=3,5,10)价格冲击 t 检验均值，tanh 压缩到 [0,1)
      - divergence: 两组 (<T1, ≥T1) 的净流入累计曲线差异
      - 权重 6:4 偏向统计显著性

    自动下滑机制 (两级):
      1. 分布过窄 (P98 ≤ P75×1.2) → K=2 聚类边界
      2. 峰值平坦 (peak/median < 1.15) 或跨天不稳定 (CV > 80%)
         → K=2 聚类边界（≥50万订单内 turnover+相对大小 双特征聚类）

    返回: (anchor_value, meta_dict)
      meta_dict.method:
        'adaptive'          — 自适应扫描成功
        'k2_fallback'       — 下滑到 K=2
        'k2_fallback_no_data' — 无数据，下滑到 K=2
        'range_too_narrow'  — 分布过窄
    """
    from scipy.stats import ttest_ind

    p50 = float(np.percentile(turnovers, 50))
    p75 = float(np.percentile(turnovers, 75))
    p98 = float(np.percentile(turnovers, 98))

    # 搜索下界: P75（分布的中高位），避免低端样本量优势干扰
    lo_bound = max(p75, 15e4)
    if p98 <= lo_bound * 1.2:
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else lo_bound * 2,
                {'method': 'range_too_narrow', 'p50': p50, 'p98': p98})

    t1_cands = np.unique(np.round(np.logspace(
        np.log10(lo_bound), np.log10(p98), 50), -3))

    fw_list = [3, 5, 10]

    # Precompute impacts (full array)
    impacts = {}
    for fw in fw_list:
        impacts[fw] = compute_price_impact(prices, dir_v, fw)

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
            n_lo = int(np.sum(to_d < t1))
            n_hi = int(np.sum(to_d >= t1))
            if n_lo < 50 or n_hi < 50:
                continue

            # Multi-window t-stat
            t_vals = []
            for fw in fw_list:
                imp = impacts[fw]
                imp_d = imp[dmask]           # slice to this day
                valid_d = ~np.isnan(imp_d)    # valid for this day
                lo = imp_d[valid_d & (to_d < t1)]
                hi = imp_d[valid_d & (to_d >= t1)]
                if len(lo) >= 30 and len(hi) >= 30:
                    t_val, _ = ttest_ind(hi, lo, equal_var=False)
                    t_vals.append(abs(t_val))
            if len(t_vals) < 2:
                continue
            t_mean = float(np.mean(t_vals))

            # Netflow divergence
            labels = np.zeros(len(to_d), dtype=int)
            labels[to_d >= t1] = 1
            curves = compute_netflow_curve(ts_d, nf_d, labels, 2)
            div_val = divergence(curves[0], curves[1])

            score = 0.6 * np.tanh(t_mean / 10.0) + 0.4 * div_val
            all_scores.append(score)

            if score > best_score:
                best_score = score
                best_t1 = float(t1)

        if best_score >= 0:
            per_day[str(d)] = {
                'best_t1': best_t1,
                'score': best_score,
            }

    if not per_day:
        k2 = _k2_boundary(turnovers, dates, times)
        return (k2 if k2 else 300e4,
                {'method': 'k2_fallback_no_data', 'per_day': {}})

    all_t1s = [per_day[str(d)]['best_t1'] for d in unique_dates if str(d) in per_day]
    median_t1 = float(np.median(all_t1s))

    # Flatness check + cross-day stability check
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
                'method': 'k2_fallback',
                'fallback_reason': reason,
                'per_day': per_day,
                'summary': {
                    'peak_score': peak,
                    'median_score': med_score,
                    'flatness_ratio': peak / max(med_score, 1e-6),
                    't1_cv': t1_cv,
                    'k2_boundary': k2,
                }
            }

    return median_t1, {
        'method': 'adaptive',
        'per_day': per_day,
        'summary': {
            't1_median': median_t1,
            't1_mean': float(np.mean(all_t1s)),
            't1_std': float(np.std(all_t1s)),
            't1_cv': float(np.std(all_t1s) / np.mean(all_t1s) * 100) if np.mean(all_t1s) > 0 else None,
            'search_range': [lo_bound, p98],
            'n_days': len(per_day),
            'peak_score': peak,
            'median_score': med_score,
        }
    }


# ═══════════════════════════════════════════════════════════
# 主分析流程
# ═══════════════════════════════════════════════════════════

def run_analysis(times, dates, prices, turnovers, dir_v, ttypes, unique_dates):
    """
    主编排函数 — 执行完整的阈值分析流程。

    流程 (6 个章节，详见文件头 docstring):
      1. 金额分布: 分位数概览
      2. 价格冲击 t 统计量: 固定候选阈值对照 (1/5/15/300/1000 万)
      3. 自适应扫描: 阶段0→1→2→3 四阶段管线
      4. 稳定度验证: train/test split + 留一天交叉验证
      5. K=2 聚类: ≥50万订单内部子结构探索
      6b. 分档方法对比: 绝对金额 vs 富途按比例 (交叉矩阵)
      6. 最终推荐阈值

    关键变量:
      recommended_t0: 小/中单边界 (来自阶段2)
      recommended_t1: 中/大单边界 (来自阶段0锚点 + 阶段3微调)
      recommended_t2: 大/特大单边界 (来自阶段1)

    返回:
      results dict，包含各章节的详细数据，可序列化为 JSON。
    """
    results = {}

    # ── 1. 金额分布 ──
    print("\n" + "=" * 60)
    print("1. 金额分布")
    print("=" * 60)
    percentiles = [50, 60, 70, 75, 80, 85, 90, 92, 94, 95, 96, 97, 98, 99, 99.5, 99.9]
    for p in percentiles:
        v = np.percentile(turnovers, p)
        print(f"  P{p:5.1f}: {v:>12,.0f} (¥{v/1e4:.0f}万)")

    results['amount_summary'] = {
        'count': int(len(turnovers)),
        'total_amt': float(turnovers.sum()),
        'mean': float(turnovers.mean()),
        'median': float(np.median(turnovers)),
        'percentiles': {f'P{p}': float(np.percentile(turnovers, p)) for p in percentiles}
    }

    # ── 2. 多窗口价格冲击 t 统计量 ──
    print("\n" + "=" * 60)
    print("2. 价格冲击 t 统计量（候选阈值）")
    print("=" * 60)

    FW_SMALL = [1, 3, 5, 10]
    FW_LARGE = [20, 50, 100, 200, 500, 1000]
    # 固定参考阈值 — 展示常见档位处的 t 统计量，作为自适应探测的对照基准
    CANDIDATE_THS = [1e4, 5e4, 15e4, 300e4, 1000e4]  # 1万, 5万, 15万, 300万, 1000万

    impact_results = {}
    for fw in FW_SMALL + FW_LARGE:
        imp = compute_price_impact(prices, dir_v, fw)
        actual_times = times[fw:] - times[:len(times) - fw]
        med_time = np.median(actual_times) if len(actual_times) > 0 else 0

        print(f"\n  fw={fw:>4} tick | 时间中位: {med_time:.0f}s")
        impact_results[f'fw_{fw}'] = {
            'med_time_s': float(med_time),
            'thresholds': {}
        }

        for th, label in zip(CANDIDATE_THS, ['1万', '5万', '15万', '300万', '1000万']):
            t, lo_mean, hi_mean, lo_n, hi_n = t_at_threshold(turnovers, imp, th)
            if np.isnan(t):
                print(f"    {label:6s}: 样本不足")
                continue
            sign = "⚠" if t < 5 else ("✓" if t >= 10 else "")
            print(f"    {label:6s}: lo={lo_mean:+.5f} hi={hi_mean:+.5f}  "
                  f"t={t:.1f}  (lo_n={lo_n:,} hi_n={hi_n:,})  {sign}")
            impact_results[f'fw_{fw}']['thresholds'][label] = {
                't': float(t), 'lo_mean': float(lo_mean), 'hi_mean': float(hi_mean),
                'lo_n': int(lo_n), 'hi_n': int(hi_n)
            }

    results['impact_analysis'] = impact_results

    # ── 3. 阈值扫描最优 ──
    print("\n" + "=" * 60)
    print("3. 阈值扫描：各窗口最优边界")
    print("=" * 60)
    ths_scan = np.logspace(3.5, 7, 200)

    scan_results = {}
    print(f"  {'fw':>8} {'最优阈值':>12} {'t':>7}  (med时间)")
    for fw in FW_SMALL + FW_LARGE:
        imp = compute_price_impact(prices, dir_v, fw)
        best_th, best_t = scan_thresholds(turnovers, imp, ths_scan)
        med_time = np.median(times[fw:] - times[:len(times) - fw])
        print(f"  {fw:>4}tick {best_th:>12,.0f} {best_t:>7.1f}  {med_time:.0f}s")
        scan_results[f'fw_{fw}'] = {
            'best_th': float(best_th) if not np.isnan(best_th) else None,
            'best_t': float(best_t) if not np.isnan(best_t) else None,
            'med_time_s': float(med_time)
        }
    results['threshold_scan'] = scan_results

    # ═══════════════════════════════════════════════════════════
    # 3b. 分阶段条件搜索
    #  阶段0: 自适应检测 T1 锚点
    #  阶段1: T1固定 → 搜 T2（大/特大边界）
    #  阶段2: T1固定 → 搜 T0（小/中边界）
    #  阶段3: T0+T2固定 → 微调 T1（可选）
    # ═══════════════════════════════════════════════════════════

    # ── 阶段0: 自适应锚点检测 ──
    print("\n" + "=" * 60)
    print("3b-阶段0. 自适应检测 T1 锚点")
    print("=" * 60)

    T1_ANCHOR, t1_meta = detect_t1_anchor(
        turnovers, prices, dir_v, times, dates, unique_dates)
    results['staged_stage0_anchor'] = t1_meta

    method_label = {'adaptive': '自适应扫描', 'k2_fallback_flat': 'K=2聚类(评分平坦)',
                    'k2_fallback_no_data': 'K=2聚类(无数据)', 'range_too_narrow': '分布过窄'}

    if t1_meta['method'] == 'adaptive':
        s0 = t1_meta['summary']
        print(f"  方法: 自适应扫描")
        print(f"  搜索范围: [{s0['search_range'][0]/1e4:.0f}万, {s0['search_range'][1]/1e4:.0f}万] "
              f"(P75~P98)")
        print(f"  峰值/中位比: {s0['peak_score']:.3f} / {s0['median_score']:.3f} = "
              f"{s0['peak_score']/max(s0['median_score'],1e-6):.2f}")
        print(f"  跨天 CV: {s0.get('t1_cv', 0):.0f}% {'⚠ 不稳定' if s0.get('t1_cv', 0) and s0['t1_cv'] > 80 else '✓ 稳定'}")
        print(f"  {'日期':>10}  {'最优 T1':>10}  {'score':>7}")
        print(f"  {'-'*10}  {'-'*10}  {'-'*7}")
        for d in unique_dates:
            pd = t1_meta['per_day'].get(str(d), {})
            if pd:
                print(f"  {str(d):>10}  {pd['best_t1']/1e4:>8.0f}万  {pd['score']:>7.3f}")
        print(f"\n  → T1 锚点 = {T1_ANCHOR/1e4:.0f}万 "
              f"(中位数, CV={s0['t1_cv']:.0f}%)" if s0.get('t1_cv') is not None
              else f"\n  → T1 锚点 = {T1_ANCHOR/1e4:.0f}万")
    else:
        print(f"  方法: K=2聚类下滑 ({t1_meta.get('fallback_reason', '')})")
        s0 = t1_meta.get('summary', {})
        if s0.get('flatness_ratio'):
            print(f"  峰值/中位比: {s0['flatness_ratio']:.2f} < 1.15 → 评分平坦")
        if s0.get('t1_cv'):
            print(f"  跨天 CV: {s0['t1_cv']:.0f}% > 80% → 不稳定")
        if s0.get('k2_boundary'):
            print(f"  K2 聚类边界: {s0['k2_boundary']/1e4:.0f}万")
        print(f"  → T1 锚点 = {T1_ANCHOR/1e4:.0f}万")

    # ── 阶段1: T1 固定 → 搜索 T2 ──
    print("\n" + "=" * 60)
    print("3b-阶段1. 搜索 T2（大/特大单分界线）")
    print("=" * 60)
    print(f"  锚点: T1={T1_ANCHOR/1e4:.0f}万（固定）")
    print(f"  搜索: T2 ∈ [{T1_ANCHOR*1.5/1e4:.0f}万, 5000万]，1D 扫描")
    print(f"  评分: divergence + 方向翻转加分")

    t2_results = search_t2_given_t1(times, turnovers, dir_v, dates,
                                     unique_dates, t1_anchor=T1_ANCHOR)
    results['staged_stage1_t2'] = t2_results

    s2 = t2_results['summary']
    print(f"\n  {'日期':>10}  {'最优 T2':>10}  {'score':>7}  {'方向翻转':>8}  {'大单净':>10}  {'特大净':>10}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*10}")
    for d in unique_dates:
        pd = t2_results['per_day'][str(d)]
        flip_str = "✓ 翻转" if pd['direction_flip'] else "  同向"
        print(f"  {str(d):>10}  {pd['best_t2']/1e4:>8.0f}万  {pd['score']:>7.3f}  "
              f"{flip_str:>8}  {pd['net_large_wan']:>+8.0f}万  {pd['net_xl_wan']:>+8.0f}万")

    print(f"\n  跨天汇总:")
    cv_flag = " ⚠ 不稳定" if (s2['t2_cv'] and s2['t2_cv'] > 30) else " ✓ 稳定"
    print(f"    T2: 中位={s2['t2_median']/1e4:.0f}万  均值={s2['t2_mean']/1e4:.0f}万  "
          f"±{s2['t2_std']/1e4:.0f}万  CV={s2['t2_cv']:.0f}%{cv_flag}")
    print(f"    方向翻转: {s2['n_flip_days']}/{s2['n_days']}天 ({s2['flip_ratio']:.0%})")

    recommended_t2 = s2['t2_median'] if s2['t2_median'] else 1000e4

    # ── 阶段2: T1 固定 → 搜索 T0 ──
    print("\n" + "=" * 60)
    print("3b-阶段2. 搜索 T0（小/中单分界线）")
    print("=" * 60)
    print(f"  锚点: T1={T1_ANCHOR/1e4:.0f}万（固定）")
    print(f"  搜索: T0 ∈ [1万, 25万]，1D 扫描")
    print(f"  评分: 小单(<T0) vs 中单(T0~T1) 价格冲击 t 统计量")

    t0_results = auto_detect_t0(times, prices, turnovers, dir_v, dates,
                                unique_dates, fw_list=(3, 5, 10, 20), t1=T1_ANCHOR)
    results['staged_stage2_t0'] = t0_results

    key_t0s = [1e4, 2e4, 3e4, 5e4, 8e4, 10e4, 12e4, 15e4, 20e4, 25e4]
    t0_cands_arr = np.array(t0_results['fw_5']['t0_candidates'])
    mean_ts_arr = np.array(t0_results['fw_5']['cross_day_mean_t'])

    print(f"\n  fw=5 tick 参考:")
    print(f"  {'T0':>7}  {'跨天均值t':>10}")
    print(f"  {'-'*7}  {'-'*10}")
    for kt in key_t0s:
        idx = np.argmin(np.abs(t0_cands_arr - kt))
        if idx >= 0 and idx < len(mean_ts_arr):
            mt = mean_ts_arr[idx]
            if mt is None or np.isnan(mt):
                continue
            star = " ✓" if (mt > 5) else (" ⚠" if mt < 3 else "")
            print(f"  {t0_cands_arr[idx]/1e4:>6.0f}万  {mt:>10.2f}{star}")

    # T0_MAX=25万: 硬约束上限，防止中单组被过度切分
    # 无约束最优可能在 30~50万（高 t 但只剩少量中单），需截断
    T0_MAX = 25e4
    valid = ~np.isnan(mean_ts_arr) & (t0_cands_arr <= T0_MAX)
    if valid.sum() == 0:
        best_t0 = 15e4  # fallback
        best_t0_mean = 0
    else:
        best_t0_idx = np.nanargmax(mean_ts_arr[valid])
        best_t0 = t0_cands_arr[valid][best_t0_idx]
        best_t0_mean = mean_ts_arr[valid][best_t0_idx]
    # 无约束最优（用于对比报告，不采纳）
    uc_best = t0_cands_arr[np.nanargmax(mean_ts_arr)] if np.any(~np.isnan(mean_ts_arr)) else best_t0
    print(f"\n  → 推荐 T0 = {best_t0/1e4:.0f}万 (跨天均值 t={best_t0_mean:.1f})")
    if uc_best != best_t0 and uc_best > T0_MAX:
        print(f"     (无约束最优={uc_best/1e4:.0f}万, "
              f"但 >{T0_MAX/1e4:.0f}万会导致中单组过小，已截断)")

    recommended_t0 = best_t0

    # ── 阶段3（可选）: T0/T2 固定 → 微调 T1 ──
    print("\n" + "=" * 60)
    print("3b-阶段3. 微调 T1（中/大单边界）")
    print("=" * 60)
    print(f"  锚点: T0={recommended_t0/1e4:.0f}万, T2={recommended_t2/1e4:.0f}万（固定）")
    print(f"  搜索: T1 ∈ [{max(recommended_t0*1.5, 50e4)/1e4:.0f}万, "
          f"{recommended_t2*0.5/1e4:.0f}万]")

    fine_t1 = fine_tune_t1(times, turnovers, dir_v, dates, unique_dates,
                           t0=recommended_t0, t2=recommended_t2)
    results['staged_stage3_t1'] = fine_t1

    f1s = fine_t1.get('summary', {})
    if fine_t1.get('t1') is None and fine_t1.get('note'):
        print(f"  {fine_t1['note']}")
        print(f"  → T1 保持锚点值 = {T1_ANCHOR/1e4:.0f}万")
        recommended_t1 = T1_ANCHOR
    elif f1s.get('t1_median') is None:
        print(f"  T0~T2空间内无有效T1候选")
        print(f"  → T1 保持锚点值 = {T1_ANCHOR/1e4:.0f}万")
        recommended_t1 = T1_ANCHOR
    else:
        has_adjustment = abs(f1s['t1_median'] - T1_ANCHOR) / T1_ANCHOR > 0.10
        print(f"\n  {'日期':>10}  {'微调 T1':>10}  {'minimax':>8}")
        print(f"  {'-'*10}  {'-'*10}  {'-'*8}")
        for d in unique_dates:
            pd = fine_t1['per_day'][str(d)]
            print(f"  {str(d):>10}  {pd['best_t1']/1e4:>8.0f}万  {pd['minimax']:>8.3f}")

        new_t1 = f1s['t1_median']
        delta_pct = (new_t1 - T1_ANCHOR) / T1_ANCHOR * 100
        print(f"\n  T1 微调: {T1_ANCHOR/1e4:.0f}万 → {new_t1/1e4:.0f}万 (Δ={delta_pct:+.0f}%)")
        if has_adjustment:
            print(f"    调整>10%，已采纳")
            recommended_t1 = new_t1
        else:
            print(f"    调整≤10%，保持锚点值")
            recommended_t1 = T1_ANCHOR

    # ── 汇总 ──
    print("\n" + "-" * 40)
    print(f"  分阶段搜索结果:")
    print(f"    小单:  < {recommended_t0/1e4:.0f}万")
    print(f"    中单:  {recommended_t0/1e4:.0f}万 ~ {recommended_t1/1e4:.0f}万")
    print(f"    大单:  {recommended_t1/1e4:.0f}万 ~ {recommended_t2/1e4:.0f}万")
    print(f"    特大单: ≥ {recommended_t2/1e4:.0f}万")
    print("-" * 40)

    # ── 4. 稳定度验证（如果天数 ≥ 4）──
    # 分两步:
    #   4a: 前 n-1 天训练，最后 1 天测试 — 模拟"新一天来了阈值还管用吗"
    #   4b: 留一天法交叉验证 — 每天轮流当测试集，看波动和 CV
    if len(unique_dates) >= 4:
        print("\n" + "=" * 60)
        print("4. 稳定度验证 (前n-1天训练 / 最后1天测试)")
        print("=" * 60)

        train_mask = np.isin(dates, unique_dates[:-1])
        test_mask = dates == unique_dates[-1]

        print(f"  训练: {[str(d) for d in unique_dates[:-1]]} ({train_mask.sum():,}笔)")
        print(f"  测试: {unique_dates[-1]} ({test_mask.sum():,}笔)")

        th_specs = [
            (f'{recommended_t0/1e4:.0f}万', recommended_t0),
            (f'{recommended_t1/1e4:.0f}万', recommended_t1),
            (f'{recommended_t2/1e4:.0f}万', recommended_t2),
        ]
        stability = {}
        for label_part, th_ref in th_specs:
            stability[label_part] = {}
            for fw in [1, 5, 10, 50, 100]:
                imp = compute_price_impact(prices, dir_v, fw)
                for set_name, msk in [('train', train_mask), ('test', test_mask)]:
                    t, lo_mean, hi_mean, lo_n, hi_n = t_at_threshold(turnovers, imp, th_ref, msk)
                    stability[label_part][f'fw{fw}_{set_name}'] = {
                        't': float(t) if not np.isnan(t) else None,
                        'lo_mean': float(lo_mean) if not np.isnan(lo_mean) else None,
                        'hi_mean': float(hi_mean) if not np.isnan(hi_mean) else None
                    }

        # 打印
        print(f"\n  {'阈值':>8} {'fw':>5} {'train_t':>8} {'test_t':>8} {'Δ%':>7}")
        for label_part, th_ref in th_specs:
            for fw in [1, 5, 10, 50, 100]:
                tt = stability[label_part][f'fw{fw}_train']['t']
                st = stability[label_part][f'fw{fw}_test']['t']
                if tt is None or st is None:
                    print(f"  {label_part:>8} {fw:>5}  N/A")
                    continue
                delta = abs(tt - st) / max(abs(tt), 1e-6) * 100
                flag = "⚠" if delta > 50 else "✓"
                print(f"  {label_part:>8} {fw:>5} {tt:>8.1f} {st:>8.1f} {delta:>6.0f}% {flag}")
        results['stability'] = stability

        # ── 4b. 轮换测试日：每天轮流做测试集 ──
        print("\n" + "-" * 40)
        print("4b. 轮换测试日 (留一天法交叉验证)")
        print("-" * 40)

        rotate = {}
        fw_list = [1, 5, 10]
        th_list = [
            (f'{recommended_t0/1e4:.0f}万', recommended_t0),
            (f'{recommended_t1/1e4:.0f}万', recommended_t1),
            (f'{recommended_t2/1e4:.0f}万', recommended_t2),
        ]

        for label, th_ref in th_list:
            rotate[label] = {}
            for fw in fw_list:
                imp = compute_price_impact(prices, dir_v, fw)
                test_ts = []
                for di in range(len(unique_dates)):
                    test_d = unique_dates[di]
                    train_d = unique_dates[:di] + unique_dates[di+1:]
                    train_m = np.isin(dates, train_d)
                    test_m = dates == test_d
                    tt, _, _, _, _ = t_at_threshold(turnovers, imp, th_ref, train_m)
                    st, _, _, _, _ = t_at_threshold(turnovers, imp, th_ref, test_m)
                    if not np.isnan(tt) and not np.isnan(st):
                        test_ts.append((test_d, tt, st))
                    else:
                        test_ts.append((test_d, np.nan, np.nan))
                rotate[label][f'fw{fw}'] = {
                    'splits': [{'test_day': str(d), 'train_t': float(tt) if not np.isnan(tt) else None,
                                'test_t': float(st) if not np.isnan(st) else None}
                               for d, tt, st in test_ts]
                }

        # 打印
        print(f"\n  {'阈值':>8} {'fw':>4}", end="")
        for di in range(len(unique_dates)):
            print(f" {'D'+str(di+1)+'_train':>8} {'D'+str(di+1)+'_test':>8}", end="")
        print(f" {'test_range':>10} {'cv':>6}")
        for label, th_ref in th_list:
            for fw in fw_list:
                splits = rotate[label][f'fw{fw}']['splits']
                test_vals = [s['test_t'] for s in splits if s['test_t'] is not None]
                train_vals = [s['train_t'] for s in splits if s['train_t'] is not None]
                if len(test_vals) < 2:
                    continue
                t_min, t_max = min(test_vals), max(test_vals)
                t_range = t_max - t_min
                t_cv = np.std(test_vals) / np.mean(test_vals) * 100 if np.mean(test_vals) > 0 else np.nan
                print(f"  {label:>8} {fw:>4}", end="")
                for s in splits:
                    tt = s['train_t']; st = s['test_t']
                    if tt is not None and st is not None:
                        print(f" {tt:>8.1f} {st:>8.1f}", end="")
                    else:
                        print(f" {'N/A':>8} {'N/A':>8}", end="")
                print(f" {t_range:>10.1f} {t_cv:>5.0f}%")

        results['stability_rotate'] = rotate

    # ── 5. ≥50万大单内部 K=2 聚类 ──
    # 用 (log turnover, 相对大小) 双特征，探索"小-大单"vs"大-大单"自然子结构
    # 边界可能接近 T1 或 T2，用于验证/校准阶段0的检测结果
    print("\n" + "=" * 60)
    print("5. ≥50万大单内部 K=2 聚类")
    print("=" * 60)

    rel_size = compute_rel_size(dates, turnovers, times)

    for set_name, msk in [('全量', np.ones(len(turnovers), bool))] + \
                         ([('训练', train_mask), ('测试', test_mask)] if len(unique_dates) >= 4 else []):
        big = msk & (turnovers >= 5e5)
        if big.sum() < 100:
            print(f"  {set_name}: 大单样本不足 ({big.sum()})")
            continue

        X = np.column_stack([
            np.log1p(turnovers[big]),
            rel_size[big],
        ])
        X = np.nan_to_num(X, 0)
        X_s = StandardScaler().fit_transform(X)
        km = KMeans(n_clusters=2, random_state=42, n_init=5, max_iter=100)
        labs = km.fit_predict(X_s)

        # 按 turnover 排序
        c_meds = [np.median(turnovers[big][labs == i]) for i in range(2)]
        if c_meds[0] > c_meds[1]:
            labs = 1 - labs

        for ci in [0, 1]:
            mask_c = labs == ci
            amt_c = turnovers[big][mask_c]
            rel_c = rel_size[big][mask_c]
            pct_amt = amt_c.sum() / turnovers[big].sum() * 100
            print(f"  {set_name:4s} 簇{ci}: {mask_c.sum():,}笔 ({100*mask_c.sum()/big.sum():.1f}%)  "
                  f"med_turnover={np.median(amt_c)/1e4:.0f}万  "
                  f"med_rel={np.median(rel_c[~np.isnan(rel_c)]):.1f}×  "
                  f"金额占比={pct_amt:.1f}%")

        lo_grp = turnovers[big][labs == 0]
        hi_grp = turnovers[big][labs == 1]
        boundary = (lo_grp.max() + hi_grp.min()) / 2
        print(f"  {set_name:4s} 边界 ≈ {boundary/1e4:.0f}万")
        results[f'k2_big_{set_name}'] = {
            'cluster_lo_n': int(len(lo_grp)),
            'cluster_hi_n': int(len(hi_grp)),
            'cluster_lo_med': float(np.median(lo_grp)),
            'cluster_hi_med': float(np.median(hi_grp)),
            'boundary': float(boundary)
        }

    # ── 6b. 分档方法对比：绝对金额 vs 富途按比例 ──
    # 绝对方案: 使用自适应探测的 T0/T1/T2（行为驱动，固定金额）
    # 富途方案: 按全天总成交金额累计切分（特大10% / 大10~30% / 中30~55% / 小55~100%）
    #          阈值每天随成交分布浮动，跨股票口径统一
    print("\n" + "=" * 60)
    print("6b. 分档方法对比：绝对金额 vs 富途按比例")
    print("=" * 60)

    # 富途按比例分档（对成交金额累计切分：前10%特大, 10~30%大, 30~55%中, 55~100%小）
    sorted_idx = np.argsort(turnovers)[::-1]  # 从大到小
    sorted_amt = turnovers[sorted_idx]
    cumsum = np.cumsum(sorted_amt)
    total = cumsum[-1]

    cut_10 = np.searchsorted(cumsum, total * 0.10, side='right')
    cut_30 = np.searchsorted(cumsum, total * 0.30, side='right')
    cut_55 = np.searchsorted(cumsum, total * 0.55, side='right')

    # 每个归入区间的金额下界（排序后第 cut_xx 笔的 turnover）
    th_10 = sorted_amt[min(cut_10, len(sorted_amt) - 1)]
    th_30 = sorted_amt[min(cut_30, len(sorted_amt) - 1)]
    th_55 = sorted_amt[min(cut_55, len(sorted_amt) - 1)]

    futu_labels = np.full(len(turnovers), '小单', dtype=object)
    futu_labels[sorted_idx[:cut_10]] = '特大单'
    futu_labels[sorted_idx[cut_10:cut_30]] = '大单'
    futu_labels[sorted_idx[cut_30:cut_55]] = '中单'
    futu_bounds = {
        '特大单': (th_10, float('inf')),
        '大单':   (th_30, th_10),
        '中单':   (th_55, th_30),
        '小单':   (0, th_55),
    }

    # 绝对金额分档（使用自适应探测结果）
    abs_bounds_list = [
        ("小单",   0,               recommended_t0),
        ("中单",   recommended_t0,   recommended_t1),
        ("大单",   recommended_t1,   recommended_t2),
        ("特大单", recommended_t2,   float("inf")),
    ]
    abs_labels = np.full(len(turnovers), '', dtype=object)
    for label, lo, hi in abs_bounds_list:
        abs_labels[(turnovers >= lo) & (turnovers < hi)] = label
    abs_bounds = {l: (lo, hi) for l, lo, hi in abs_bounds_list}

    categories = ['小单', '中单', '大单', '特大单']
    total_amt = turnovers.sum() / 1e8

    # 表1: 并列对比
    print(f"\n{'分类':>8} | {'绝对金额方案':>44} | {'富途按比例方案':>44}")
    print(f"{'':>8} | {'笔数':>7} {'金额(亿)':>9} {'占比':>6} {'阈值':>15} | "
          f"{'笔数':>7} {'金额(亿)':>9} {'占比':>6} {'阈值':>15}")
    print("-" * 100)

    for cat in categories:
        a_n = (abs_labels == cat).sum()
        a_amt = turnovers[abs_labels == cat].sum() / 1e8
        a_pct = a_amt / total_amt * 100
        a_lo, a_hi = abs_bounds[cat]
        a_th = f"≥{a_lo/1e4:.0f}万" if np.isinf(a_hi) else f"{a_lo/1e4:.0f}~{a_hi/1e4:.0f}万"

        f_n = (futu_labels == cat).sum()
        f_amt = turnovers[futu_labels == cat].sum() / 1e8
        f_pct = f_amt / total_amt * 100
        f_lo, f_hi = futu_bounds[cat]
        f_th = f"≥{f_lo/1e4:.0f}万" if np.isinf(f_hi) else f"{f_lo/1e4:.0f}~{f_hi/1e4:.0f}万"

        print(f"{cat:>8} | {a_n:>7,} {a_amt:>9.2f} {a_pct:>5.1f}% {a_th:>15} | "
              f"{f_n:>7,} {f_amt:>9.2f} {f_pct:>5.1f}% {f_th:>15}")

    # 表2: 交叉分布矩阵
    print(f"\n交叉分布（行=绝对金额, 列=富途按比例）:")
    print(f"{'':>10}", end="")
    for cat in categories:
        print(f" {cat:>8}", end="")
    print(f" {'合计':>8}")
    print("-" * (10 + 9 * (len(categories) + 1)))

    for a_cat in categories:
        print(f"{a_cat:>10}", end="")
        row_sum = 0
        for f_cat in categories:
            n = ((abs_labels == a_cat) & (futu_labels == f_cat)).sum()
            row_sum += n
            print(f" {n:>8,}", end="")
        print(f" {row_sum:>8,}")

    # 表3: 差异汇总
    print(f"\n差异最大的分类:")
    for cat in categories:
        a_pct = (turnovers[abs_labels == cat].sum() / 1e8) / total_amt * 100
        f_pct = (turnovers[futu_labels == cat].sum() / 1e8) / total_amt * 100
        delta = a_pct - f_pct
        flag = "← 绝对方案更多" if delta > 5 else ("← 富途方案更多" if delta < -5 else "")
        print(f"  {cat:>6}: 绝对{a_pct:5.1f}% vs 富途{f_pct:5.1f}%  Δ={delta:+5.1f}% {flag}")

    results['classification_comparison'] = {
        'futu_cutoffs': {'th_10%': float(th_10), 'th_30%': float(th_30), 'th_55%': float(th_55)},
        'futu_bounds': {k: (float(v[0]), float(v[1])) for k, v in futu_bounds.items()},
        'absolute_bounds': {k: (float(v[0]), float(v[1])) for k, v in abs_bounds.items()},
    }

    # ── 6. 最终推荐 ──
    print("\n" + "=" * 60)
    print("6. 推荐阈值（自动探测）")
    print("=" * 60)
    print(f"""
  小单:    turnover <  {recommended_t0/1e4:.0f}万      (T0自动探测，价格冲击 t={best_t0_mean:.1f})
  中单:   {recommended_t0/1e4:.0f}万 ≤ turnover < {recommended_t1/1e4:.0f}万  (T1自动探测)
  大单:   {recommended_t1/1e4:.0f}万 ≤ turnover < {recommended_t2/1e4:.0f}万  (T2自动探测)
  特大单: turnover ≥ {recommended_t2/1e4:.0f}万     (T2自动探测)
""")

    return results


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='订单大小阈值分析')
    parser.add_argument('--stock', default='HK.00700', help='股票代码')
    parser.add_argument('--start', help='开始日期 YYYY-MM-DD')
    parser.add_argument('--end', help='结束日期 YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=None, help='最近N个交易日 (默认14, 与start/end互斥)')
    parser.add_argument('--output', help='JSON输出路径')
    args = parser.parse_args()

    t0 = time.time()

    if args.start and args.end:
        times, dates, prices, turnovers, dir_v, ttypes, ud = load_data(
            args.stock, start_date=args.start, end_date=args.end)
    else:
        times, dates, prices, turnovers, dir_v, ttypes, ud = load_data(
            args.stock, days=args.days)

    results = run_analysis(times, dates, prices, turnovers, dir_v, ttypes, ud)

    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n结果已保存: {args.output}")

    print(f"\n总耗时: {time.time() - t0:.1f}s")
