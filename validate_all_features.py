#!/usr/bin/env python3
"""
对所有高 IC 特征做系统验证（去趋势 + 滚动 IC + 稳健性）
"""
import sys
import numpy as np
from scipy.stats import spearmanr

from predictor_scan import (
    load_price_data, load_fund_flow, load_ggt, load_short_selling,
    load_cbbc, load_buyback, build_dataset, spearman_ic
)

STOCK = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"

# 要验证的特征 + 对应的去趋势 lookback（特征本身可能包含过去 N 日信息）
FEATURES = [
    ("bear_dist_pct", [5, 10, 20]),
    ("bull_dist_pct", [5, 10, 20]),
    ("cbbc_bull_ratio", [5, 10]),
    ("short_ratio", [5, 10]),
    ("short_chg", [3, 5]),
    ("ggt_inflow", [5, 10, 20]),
    ("ggt_hold_chg", [5, 10]),
    ("ggt_cum5d", [10, 20]),
    ("ggt_cum10d", [15, 20]),
    ("ggt_cum20d", [25, 30]),
    ("big_net", [3, 5]),
    ("mid_net", [3, 5]),
    ("small_net", [3, 5]),
    ("large_net", [3, 5]),
    ("total_bsr", [3, 5]),
    ("tick_density", [3, 5]),
    ("amp", [5, 10]),
    ("overnight_gap", [3, 5]),
    ("gap_fill_ratio", [3, 5]),
    ("pv_corr_5d", [5, 10]),
    ("vol_surprise", [5, 10]),
]


def rolling_ic_analysis(fvals, labels, window=20):
    """滚动 IC"""
    mask = ~np.isnan(fvals) & ~np.isnan(labels)
    x, y = fvals[mask], labels[mask]
    n = len(x)
    results = []
    for i in range(window - 1, n):
        start = i - window + 1
        end = i + 1
        sx, sy = x[start:end], y[start:end]
        m = ~np.isnan(sx) & ~np.isnan(sy)
        if m.sum() < 8:
            results.append(np.nan)
        else:
            r, _ = spearmanr(sx[m], sy[m])
            results.append(r)
    results = np.array(results)
    valid = results[~np.isnan(results)]
    if len(valid) < 3:
        return np.nan, np.nan, np.nan, 0
    return float(np.mean(valid)), float(np.std(valid)), float(np.mean(valid > 0)), len(valid)


def extreme_sensitivity(fvals, labels):
    """去掉两端 5% 观察 IC 变化 + 单日最大影响"""
    mask = ~np.isnan(fvals) & ~np.isnan(labels)
    x, y = fvals[mask], labels[mask]
    n = len(x)
    if n < 15:
        return np.nan, np.nan, 0

    r_full, _ = spearmanr(x, y)
    k = max(1, n // 20)
    order = np.argsort(x)
    keep = np.ones(n, dtype=bool)
    keep[order[:k]] = False
    keep[order[-k:]] = False
    r_trim, _ = spearmanr(x[keep], y[keep]) if keep.sum() > 5 else (np.nan, None)

    max_impact = 0.0
    for i in range(min(n, 100)):  # 抽样检查避免太慢
        mask_leave = np.ones(n, dtype=bool)
        idx = np.random.randint(0, n)
        mask_leave[idx] = False
        try:
            r_leave, _ = spearmanr(x[mask_leave], y[mask_leave])
            max_impact = max(max_impact, abs(r_leave - r_full))
        except:
            pass

    return float(r_full), float(r_trim) if not np.isnan(r_trim) else np.nan, n


def detrend_check(fvals, labels, close, idx, lookback):
    """特征对过去 lookback 日收益回归，残差 vs 未来收益 IC"""
    n = len(close)
    past_ret = np.full(n, np.nan)
    for i in range(lookback, n):
        past_ret[i] = close[i] / close[i - lookback] - 1

    mask = ~np.isnan(fvals) & ~np.isnan(labels) & ~np.isnan(past_ret)
    if mask.sum() < 10:
        return np.nan, np.nan

    x = fvals[mask]
    lp = past_ret[mask]
    y = labels[mask]

    A = np.column_stack([np.ones_like(lp), lp])
    beta, _, _, _ = np.linalg.lstsq(A, x, rcond=None)
    residual = x - A @ beta

    r_raw, _ = spearmanr(x, y)
    r_res, _ = spearmanr(residual, y)

    return float(r_raw), float(r_res)


def main():
    stock = STOCK
    print(f"\n{'='*90}")
    print(f"  高 IC 特征系统验证 — {stock}")
    print(f"  验证项: IC | 去趋势残差IC | 滚动IC均值±std | 稳健性")
    print(f"{'='*90}")

    # 加载数据
    price_rows = load_price_data(stock)
    fund_flow = load_fund_flow(stock)
    ggt = load_ggt(stock)
    short_sell = load_short_selling(stock)
    cbbc = load_cbbc(stock)
    buyback = load_buyback(stock)

    dates, feat_list, y_1d, y_5d, y_10d, y_20d = build_dataset(
        price_rows, fund_flow, ggt, short_sell, cbbc, buyback
    )

    if not feat_list or feat_list[0] is None:
        print("ERROR: 特征为空")
        return

    close = np.array([r["close"] for r in price_rows], dtype=float)
    T = len(dates)

    # ---- 先算所有特征的 IC ----
    all_feat_names = sorted(feat_list[0].keys())
    ic_by_feat = {}
    for fname in all_feat_names:
        fvals = np.array([(fl[fname] if fl else np.nan) for fl in feat_list], dtype=float)
        cov = int((~np.isnan(fvals)).sum())
        if cov < 10:
            continue
        ic1 = spearman_ic(fvals, y_1d)
        ic5 = spearman_ic(fvals, y_5d)
        ic10 = spearman_ic(fvals, y_10d)
        ic20 = spearman_ic(fvals, y_20d)
        best_abs = max(abs(x) for x in [ic1, ic5, ic10, ic20] if not np.isnan(x))
        best_h = max([(1, ic1), (5, ic5), (10, ic10), (20, ic20)],
                     key=lambda x: abs(x[1]) if not np.isnan(x[1]) else 0)
        ic_by_feat[fname] = {
            "vals": fvals, "cov": cov,
            "ic1": ic1, "ic5": ic5, "ic10": ic10, "ic20": ic20,
            "best_abs": best_abs, "best_h": best_h[0], "best_ic": best_h[1],
        }

    # ---- 对重点特征做验证 ----
    print(f"\n{'特征':26s} {'最佳IC':>7s} {'H':>2s}  {'去趋势残差IC':>16s}  {'滚动IC(均值±std)':>22s}  {'全IC→去端':>14s}  {'覆盖':>5s}  {'判定':>10s}")
    print(f"{'─'*26} {'─'*7} {'─'*2}  {'─'*16}  {'─'*22}  {'─'*14}  {'─'*5}  {'─'*10}")

    for fname, lookbacks in FEATURES:
        if fname not in ic_by_feat:
            continue

        info = ic_by_feat[fname]
        fvals = info["vals"]
        best_h = info["best_h"]
        best_ic = info["best_ic"]
        cov = info["cov"]

        if cov < 10:
            continue

        # 选择 label（取 IC 最强的 horizon）
        label_map = {1: y_1d, 5: y_5d, 10: y_10d, 20: y_20d}
        labels = label_map[best_h]

        # 去趋势（取中间 lookback 的结果）
        det_results = []
        for lb in lookbacks:
            r_raw, r_res = detrend_check(fvals, labels, close, None, lb)
            if not np.isnan(r_res):
                det_results.append((lb, r_raw, r_res))

        # 滚动 IC
        roll_mean, roll_std, roll_pos, roll_n = rolling_ic_analysis(fvals, labels, window=20)

        # 极端日
        r_full, r_trim, es_n = extreme_sensitivity(fvals, labels)

        # 格式化
        def f_ic(v):
            if np.isnan(v): return "   N/A"
            return f"{v:+6.3f}"

        ic_str = f"{f_ic(best_ic)}"
        h_str = f"{best_h}D"

        if det_results:
            best_det = min(det_results, key=lambda x: abs(x[2]))
            det_str = f"{f_ic(best_det[2])} (N={best_det[0]})"
        else:
            det_str = "      N/A       "

        if not np.isnan(roll_mean):
            roll_str = f"{roll_mean:+.3f}±{roll_std:.3f}"
        else:
            roll_str = "        N/A         "

        if not np.isnan(r_full) and not np.isnan(r_trim):
            trim_str = f"{f_ic(r_full)}→{f_ic(r_trim)}"
        else:
            trim_str = "     N/A      "

        # 判定
        verdict = ""
        if det_results:
            residual_ic = abs(best_det[2])
            if residual_ic >= 0.15:
                verdict = "★ 有残差信号"
                if residual_ic >= 0.3:
                    verdict = "★★★ 强残差"
            elif abs(best_ic) >= 0.15 and residual_ic < 0.10:
                verdict = "✗ 趋势代理"
            elif abs(best_ic) < 0.10:
                verdict = "— 弱信号"
            else:
                verdict = "? 待确认"
        else:
            verdict = "— 样本不足" if cov < 15 else "? 待确认"

        print(f"{fname:26s} {ic_str} {h_str:>2s}  {det_str:>16s}  {roll_str:>22s}  {trim_str:>14s}  {cov:5d}  {verdict:>10s}")

    # ---- 总结 ----
    print(f"\n{'='*90}")
    print(f"  总结")
    print(f"{'='*90}")

    real_signals = []
    proxy_signals = []

    for fname, lookbacks in FEATURES:
        if fname not in ic_by_feat:
            continue
        info = ic_by_feat[fname]
        if info["cov"] < 10:
            continue
        best_ic = info["best_ic"]
        fvals = info["vals"]
        best_h = info["best_h"]
        label_map = {1: y_1d, 5: y_5d, 10: y_10d, 20: y_20d}
        labels = label_map[best_h]

        det_results = []
        for lb in lookbacks:
            r_raw, r_res = detrend_check(fvals, labels, close, None, lb)
            if not np.isnan(r_res):
                det_results.append((lb, r_res))

        if not det_results:
            continue

        mid_residual = sorted(det_results, key=lambda x: abs(x[1]))[len(det_results)//2][1]

        if abs(mid_residual) >= 0.15:
            real_signals.append((fname, best_h, best_ic, mid_residual, info["cov"]))
        elif abs(best_ic) >= 0.15:
            proxy_signals.append((fname, best_h, best_ic, mid_residual, info["cov"]))

    if real_signals:
        real_signals.sort(key=lambda x: -abs(x[3]))
        print(f"\n  去趋势后仍有预测力的信号 ({len(real_signals)} 个):")
        for name, h, raw, res, cov in real_signals:
            print(f"    {name:26s} 原始IC{raw:+.3f}@{h}D  残差IC{res:+.3f}  n={cov}")

    if proxy_signals:
        proxy_signals.sort(key=lambda x: -abs(x[2]))
        print(f"\n  去趋势后失效的信号（只是价格趋势的代理, {len(proxy_signals)} 个):")
        for name, h, raw, res, cov in proxy_signals:
            print(f"    {name:26s} 原始IC{raw:+.3f}@{h}D  残差IC{res:+.3f}  n={cov}")

    if not real_signals and not proxy_signals:
        print(f"\n  无显著信号 — 所有特征原始 IC < 0.15")

    if not real_signals:
        print(f"\n  ⚠ 去趋势后没有任何特征残差 IC ≥ 0.15")
        print(f"  → 当前可用的日线数据中，尚未找到独立于价格趋势的预测信号")
        print(f"  → 建议方向：")
        print(f"    1. 分钟级 tick flow 微观结构（OFI 改进版）")
        print(f"    2. 事件驱动（公告/财报/大宗交易）")
        print(f"    3. 跨品种相对价值（非单票方向）")
        print(f"    4. 波动率/尾部风险度量（替代方向预测）")


if __name__ == "__main__":
    main()
