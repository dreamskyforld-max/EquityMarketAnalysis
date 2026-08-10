#!/usr/bin/env python3
"""
bear_dist_pct 信号验证：排除过拟合 + 前瞻偏差
三个检验：
1. 信号稳定性（滚动 IC + 逐段分析）
2. 去趋势：残差是否有独立预测力
3. 前瞻偏差排查（数据时间戳对齐）
"""
import sys
import numpy as np
from datetime import date
from db import get_conn

STOCK = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"

# ========== 加载 ==========

def load_quote(stock):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, last_price, prev_close, change_pct FROM daily_quote WHERE stock_code=%s ORDER BY trade_date",
            (stock,),
        )
        rows = cur.fetchall()
    return [(r[0], float(r[1]), float(r[2]) if r[2] else None,
             float(r[3]) if r[3] is not None else None) for r in rows]

def load_cbbc(stock):
    with get_conn() as conn:
        cur = conn.cursor()
        # 先探测有哪些列
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='daily_cbbc' ORDER BY ordinal_position"
        )
        cols = [r[0] for r in cur.fetchall()]
        # 动态查询
        sel_cols = ["trade_date","bull_call_level","bull_street_volume","bear_call_level","bear_street_volume"]
        has_update = "update_time" in cols
        has_created = "created_at" in cols
        if has_update:
            sel_cols.append("update_time")
        elif has_created:
            sel_cols.append("created_at")
        cur.execute(
            f"""SELECT {', '.join(sel_cols)}
               FROM daily_cbbc WHERE stock_code=%s ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    result = {}
    for r in rows:
        d = {
            "bull_level": float(r[1]) if r[1] else None,
            "bull_vol": float(r[2]) if r[2] else None,
            "bear_level": float(r[3]) if r[3] else None,
            "bear_vol": float(r[4]) if r[4] else None,
        }
        if len(r) > 5:
            d["ts_col"] = r[5]
        result[r[0]] = d
    return result


# ========== 检验 1: 信号稳定性（滚动 IC）==========

def rolling_ic(dates, close, bear_dist_pct, y_fwd, window=20):
    """在 window 天内滚动计算 Spearman IC"""
    from scipy.stats import spearmanr
    n = len(dates)
    results = []
    for i in range(window - 1, n):
        start = i - window + 1
        end = i + 1
        segment_dates = dates[start:end]
        segment_x = np.array(bear_dist_pct[start:end])
        segment_y = np.array(y_fwd[start:end])
        mask = ~np.isnan(segment_x) & ~np.isnan(segment_y)
        if mask.sum() < 10:
            results.append((dates[i], np.nan, 0))
            continue
        r, _ = spearmanr(segment_x[mask], segment_y[mask])
        results.append((dates[i], r, mask.sum()))
    return results


# ========== 检验 2: 去趋势（残差预测力）==========

def detrend_test(dates, close, bear_dist_pct, y_fwd, lookbacks=[5,10,20]):
    """
    对每个 lookback：
    1. bear_dist_pct ~ past_N_day_return 做线性回归
    2. 取残差（排除趋势共线性后的纯信号）
    3. 残差与未来收益的 IC
    """
    from scipy.stats import spearmanr
    print(f"\n  {'─'*60}")
    print(f"  去趋势检验：bear_dist_pct 在控制历史涨幅后是否仍有预测力")
    print(f"  {'─'*60}")

    n = len(dates)

    for lb in lookbacks:
        # 计算过去 N 日收益
        past_ret = np.full(n, np.nan)
        for i in range(lb, n):
            past_ret[i] = close[i] / close[i - lb] - 1

        # 有效样本
        mask = ~np.isnan(bear_dist_pct) & ~np.isnan(past_ret) & ~np.isnan(y_fwd)
        if mask.sum() < 15:
            print(f"  N={lb}: 样本不足 ({mask.sum()} 天)")
            continue

        x_raw = bear_dist_pct[mask]
        x_past = past_ret[mask]
        y = y_fwd[mask]

        # 线性回归：bear_dist_pct = α + β * past_ret + ε
        A = np.column_stack([np.ones_like(x_past), x_past])
        beta, _, _, _ = np.linalg.lstsq(A, x_raw, rcond=None)
        predicted = A @ beta
        residual = x_raw - predicted

        # 原始 IC
        r_raw, _ = spearmanr(x_raw, y)
        # 残差 IC
        r_res, _ = spearmanr(residual, y)

        # 共享方差：corr(bear_dist_pct, past_ret)
        r_share, _ = spearmanr(x_raw, x_past)

        print(f"  N={lb:2d} 天   原始 IC = {r_raw:+.3f}    残差 IC = {r_res:+.3f}    bear_dist ~ past_ret r = {r_share:+.3f}    n = {mask.sum()}")

        decline = (r_raw - r_res) / abs(r_raw) * 100 if abs(r_raw) > 0.01 else 0
        if decline > 50:
            print(f"           ⚠ 残差 IC 衰减 {decline:.0f}% → 信号可能只是趋势代理")
        elif decline > 20:
            print(f"           · 残差 IC 有一定衰减 ({decline:.0f}%)，但仍有独立预测力")
        else:
            print(f"           ✓ 残差 IC 基本不变 → 信号独立于历史趋势")


# ========== 检验 3: 前瞻偏差 ==========

def lookahead_check(dates, cbbc, close):
    """
    排查两种前瞻偏差：
    1. CBBC 数据的 update_time 是否晚于 trade_date？（收盘后才更新）
    2. bear_call_level 是否已反映当日收盘价？（行权价根据当日收盘调整？）
    """
    print(f"\n  {'─'*60}")
    print(f"  前瞻偏差排查")
    print(f"  {'─'*60}")

    # 1. 时间戳对齐
    cbbc_dates = sorted(cbbc.keys())
    if cbbc_dates:
        first = cbbc_dates[0]
        last = cbbc_dates[-1]
        print(f"  CBBC 数据范围: {first} ~ {last} ({len(cbbc_dates)} 天)")
        ts_col = cbbc[cbbc_dates[0]].get("ts_col")
        if ts_col:
            print(f"  时间戳列: {ts_col}（含时间说明数据采集时刻）")
        else:
            print(f"  无时间戳列 — 数据按 trade_date 对齐，同一天内无更细精度")

    # 2. 检查 bear_level 是否与当日收盘同步变化
    # 如果 bear_dist_pct 的变动完全由 close 变动解释（bear_level 不变），
    # 那 bear_dist_pct 只是 close 的数学变换，不是独立信号
    bear_levels = np.array([cbbc[d]["bear_level"] if d in cbbc and cbbc[d]["bear_level"] else np.nan for d in dates])
    bear_dist = np.array([(close[i] - bear_levels[i]) / close[i] * 100
                          if not (np.isnan(close[i]) or np.isnan(bear_levels[i]))
                          else np.nan for i in range(len(dates))])

    # bear_level 的日变动 vs close 的日变动
    mask = ~np.isnan(bear_levels) & ~np.isnan(close)
    if mask.sum() > 2:
        d_bear = np.diff(bear_levels[mask])
        d_close = np.diff(close[mask])
        mask2 = ~np.isnan(d_bear) & ~np.isnan(d_close)
        if mask2.sum() > 2:
            from scipy.stats import spearmanr
            r_level_price, _ = spearmanr(d_bear[mask2], d_close[mask2])
            print(f"  bear_level 日变动 vs close 日变动: r = {r_level_price:+.3f}")

            # bear_level 变动频率
            changes = np.sum(np.abs(d_bear[mask2]) > 0.01)
            total = len(d_bear[mask2])
            print(f"  bear_level 变动天数: {changes}/{total} ({changes/total*100:.0f}%)")

            if r_level_price > 0.8:
                print(f"  ⚠ bear_level 与 close 高度同步变化 → bear_dist_pct 的变动可能只是 price 变动的代数后果")
            elif r_level_price < 0.3:
                print(f"  ✓ bear_level 相对独立于 close 变动 → bear_dist_pct 含有独立信息")


# ========== 检验 4: 收益分布（是否是几个极端日驱动的）==========

def extreme_day_analysis(dates, close, bear_dist_pct, y_fwd):
    """检查 bear_dist_pct 的信号是否由少数极端日驱动"""
    print(f"\n  {'─'*60}")
    print(f"  极端日驱动分析")
    print(f"  {'─'*60}")

    from scipy.stats import spearmanr

    mask = ~np.isnan(bear_dist_pct) & ~np.isnan(y_fwd)
    x = bear_dist_pct[mask]
    y = y_fwd[mask]
    n = len(x)

    if n < 10:
        print("  样本太少，跳过")
        return

    # 全样本 IC
    r_full, _ = spearmanr(x, y)
    print(f"  全样本 IC = {r_full:+.3f} (n={n})")

    # 去掉 top 5% 和 bottom 5%
    k = max(1, n // 20)
    order = np.argsort(x)
    keep = np.ones(n, dtype=bool)
    keep[order[:k]] = False
    keep[order[-k:]] = False
    if keep.sum() > 10:
        r_trim, _ = spearmanr(x[keep], y[keep])
        print(f"  去掉两端 5% 后 IC = {r_trim:+.3f} (n={keep.sum()})")
        if abs(r_trim - r_full) > 0.15:
            print(f"  ⚠ IC 剧烈变化 → 信号可能由极端日驱动")
        else:
            print(f"  ✓ IC 稳定 → 信号非极端值依赖")

    # 逐日贡献：每次去掉一天，看 IC 变化
    max_impact = 0
    for i in range(n):
        mask_leave = np.ones(n, dtype=bool)
        mask_leave[i] = False
        r_leave, _ = spearmanr(x[mask_leave], y[mask_leave])
        impact = abs(r_leave - r_full)
        if impact > max_impact:
            max_impact = impact

    if max_impact > 0.1:
        print(f"  ⚠ 单日最大影响 = {max_impact:.3f} → 信号对个别日敏感")
    else:
        print(f"  ✓ 单日最大影响 = {max_impact:.3f} → 信号稳健")


# ========== 检验 5: 同类横向验证（bull_dist_pct 也应该有效？）==========

def symmetry_check(dates, close, cbbc, y_fwd):
    """如果 bear_dist_pct 有效，bull_dist_pct 的 IC 应该对称（符号相反）"""
    print(f"\n  {'─'*60}")
    print(f"  对称性验证（bull vs bear）")
    print(f"  {'─'*60}")

    from scipy.stats import spearmanr

    bull_dist = np.full(len(dates), np.nan)
    bear_dist = np.full(len(dates), np.nan)

    for i, d in enumerate(dates):
        if d in cbbc and close[i] is not None and not np.isnan(close[i]):
            cb = cbbc[d]
            if cb["bull_level"]:
                bull_dist[i] = (close[i] - cb["bull_level"]) / close[i] * 100
            if cb["bear_level"]:
                bear_dist[i] = (cb["bear_level"] - close[i]) / close[i] * 100

    for name, arr in [("bull_dist_pct", bull_dist), ("bear_dist_pct", bear_dist)]:
        mask = ~np.isnan(arr) & ~np.isnan(y_fwd)
        if mask.sum() < 10:
            print(f"  {name}: 样本不足")
            continue
        r, _ = spearmanr(arr[mask], y_fwd[mask])

        # bull_dist 越大 → 牛证行权价越远 → 看跌？ → IC 应为负
        print(f"  {name}: IC = {r:+.3f} (n={mask.sum()})")

    # 检查对称性
    mask_both = ~np.isnan(bull_dist) & ~np.isnan(bear_dist) & ~np.isnan(y_fwd)
    if mask_both.sum() > 10:
        r_cross, _ = spearmanr(bull_dist[mask_both], bear_dist[mask_both])
        print(f"  bull_dist ~ bear_dist: r = {r_cross:+.3f}")
        if r_cross < -0.5:
            print(f"  ✓ 两者高度负相关 → 可能是同一信息的两个方向")
        else:
            print(f"  · 两者低相关 → 可能含有互补信息")


# ========== 主流程 ==========

def main():
    stock = STOCK
    print(f"\n{'='*80}")
    print(f"  bear_dist_pct 信号验证 — {stock}")
    print(f"{'='*80}")

    print("\n[加载]")
    quotes = load_quote(stock)  # [(date, close, prev_close, change_pct), ...]
    cbbc = load_cbbc(stock)
    print(f"  daily_quote: {len(quotes)} 天")
    print(f"  daily_cbbc: {len(cbbc)} 天")

    dates = [q[0] for q in quotes]
    close = np.array([q[1] for q in quotes], dtype=float)

    # 构造 bear_dist_pct
    bear_dist_pct = np.full(len(dates), np.nan)
    for i, d in enumerate(dates):
        if d in cbbc and cbbc[d]["bear_level"] and close[i] is not None and not np.isnan(close[i]):
            bear_dist_pct[i] = (cbbc[d]["bear_level"] - close[i]) / close[i] * 100

    # 构造 y_fwd (10 日收益，最强的 horizon)
    n = len(dates)
    y_fwd_10d = np.full(n, np.nan)
    for i in range(n - 10):
        y_fwd_10d[i] = close[i + 10] / close[i] - 1

    # 有效样本
    mask = ~np.isnan(bear_dist_pct) & ~np.isnan(y_fwd_10d)
    print(f"  有效样本: {mask.sum()} 天（bear_dist_pct × 10日收益）")

    # 检验 3: 前瞻偏差
    lookahead_check(dates, cbbc, close)

    # 检验 4: 稳健性
    extreme_day_analysis(dates, close, bear_dist_pct, y_fwd_10d)

    # 检验 2: 去趋势
    detrend_test(dates, close, bear_dist_pct, y_fwd_10d)

    # 检验 5: 对称性
    symmetry_check(dates, close, cbbc, y_fwd_10d)

    # 检验 1: 滚动 IC
    print(f"\n  {'─'*60}")
    print(f"  滚动 IC (20 天窗口)")
    print(f"  {'─'*60}")
    rolling = rolling_ic(dates, close, bear_dist_pct, y_fwd_10d, window=20)
    if rolling:
        ics = [r for _, r, n in rolling if not np.isnan(r) and n >= 10]
        if ics:
            print(f"  窗口数: {len(ics)}")
            print(f"  IC 均值: {np.mean(ics):+.3f}")
            print(f"  IC 标准差: {np.std(ics):.3f}")
            print(f"  IC > 0 的比例: {np.mean(np.array(ics) > 0):.0%}")
            positive_run = 0
            max_run = 0
            for ic in ics:
                if ic > 0:
                    positive_run += 1
                    max_run = max(max_run, positive_run)
                else:
                    positive_run = 0
            print(f"  最长连续正值: {max_run} 窗")

            # 显示滚动 IC 序列
            print(f"\n  最近 {min(10, len(rolling))} 个窗口:")
            for d, ic, n in rolling[-10:]:
                print(f"    {d}: IC={ic:+.3f} (n={n})")

    # 总结
    print(f"\n{'='*80}")
    print(f"  验证总结")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
