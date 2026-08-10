#!/usr/bin/env python3
"""
预测因子系统扫描：IC + 条件概率 + 区制分析
目标：在现有回测基础上，从「没被系统测试过」的角度寻找预测信号

核心方法论（与 direction_backtest.py 的差异）：
1. 条件概率框架（替代线性相关）：指标极端值时，次日涨跌概率是否显著偏离 50%？
2. 多周期累计效应：5/10/20 日累计，而非单日
3. 非价格维度：牛熊证、回购、资金流日级物化表、缺口
4. 区制条件化：VIX 高低、趋势上下、缺口大小 等子集分析
5. 交互特征：两指标联合，而非只看边际

已经测过的（不重复）：
- 20 特征 walk-forward logistic（AUC 0.44-0.54）
- OFI 分钟级（AUC ~0.5）
- 全球基准同期/领先/滞后相关（r<0.2 除港股外）
- 12 特征次日收益 Ridge 回归
"""
import sys
import numpy as np
from datetime import date
from collections import defaultdict
from db import get_conn

STOCK = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"

# ============ 1. 数据加载 ============

def load_price_data(stock):
    """daily_quote 全量"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, open_price, high_price, low_price, last_price,
                      prev_close, volume, turnover, volume_ratio, change_pct,
                      high_52w, low_52w, pe_ttm_ratio
               FROM daily_quote WHERE stock_code=%s ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    cols = ["date","open","high","low","close","prev_close","volume","turnover",
            "vol_ratio","change_pct","high52","low52","pe"]
    return [{c: (r[i] if r[i] is not None else None) for i,c in enumerate(cols)} for r in rows]

def load_fund_flow(stock):
    """fund_flow_daily 物化表"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, super_in, super_out, big_in, big_out, mid_in, mid_out,
                      small_in, small_out, total_buy, total_sell, tick_count, turnover_sum
               FROM fund_flow_daily WHERE stock_code=%s ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    return {r[0]: {
        "super_in": float(r[1] or 0), "super_out": float(r[2] or 0),
        "big_in": float(r[3] or 0), "big_out": float(r[4] or 0),
        "mid_in": float(r[5] or 0), "mid_out": float(r[6] or 0),
        "small_in": float(r[7] or 0), "small_out": float(r[8] or 0),
        "total_buy": float(r[9] or 0), "total_sell": float(r[10] or 0),
        "tick_count": int(r[11] or 0), "turnover_sum": float(r[12] or 0),
    } for r in rows}

def load_ggt(stock):
    """港股通持仓"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, hold_ratio, est_net_inflow, hold_num_change,
                      hold_ratio_change
               FROM daily_ggt_hold WHERE stock_code=%s ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    return {r[0]: {
        "hold_ratio": float(r[1]) if r[1] else None,
        "net_inflow": float(r[2]) if r[2] else None,
        "hold_num_chg": float(r[3]) if r[3] else None,
        "hold_ratio_chg": float(r[4]) if r[4] else None,
    } for r in rows}

def load_short_selling(stock):
    """沽空"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, short_selling_vol, short_selling_amt
               FROM daily_short_selling WHERE stock_code=%s ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    return {r[0]: {"vol": float(r[1] or 0), "amt": float(r[2] or 0)} for r in rows}

def load_cbbc(stock):
    """牛熊证街货"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, bull_call_level, bull_street_volume,
                      bear_call_level, bear_street_volume
               FROM daily_cbbc WHERE stock_code=%s ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    return {r[0]: {
        "bull_level": float(r[1]) if r[1] else None,
        "bull_vol": float(r[2]) if r[2] else None,
        "bear_level": float(r[3]) if r[3] else None,
        "bear_vol": float(r[4]) if r[4] else None,
    } for r in rows}

def load_buyback(stock):
    """公司回购"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT buyback_date, volume, amount, avg_price
               FROM daily_buyback_event WHERE stock_code=%s ORDER BY buyback_date""",
            (stock,),
        )
        rows = cur.fetchall()
    return {r[0]: {"vol": float(r[1] or 0), "amt": float(r[2] or 0),
                   "avg_price": float(r[3]) if r[3] else None} for r in rows}


# ============ 2. 特征工程 ============

def rsi(prices, period):
    if len(prices) < period + 1:
        return None
    d = np.diff(prices)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + avg_gain / avg_loss)

def sma(arr, n):
    if len(arr) < n:
        return None
    return float(np.mean(arr[-n:]))

def rolling_std(arr, n):
    if len(arr) < n:
        return None
    return float(np.std(arr[-n:]))


def build_dataset(price_rows, fund_flow, ggt, short_sell, cbbc, buyback):
    """
    构造 (date, features_dict, label_1d, label_5d, label_10d, label_20d) 序列
    所有特征在 t 日只使用 <=t 的信息
    """
    dates = [r["date"] for r in price_rows]
    closes = np.array([r["close"] for r in price_rows], dtype=float)
    opens = np.array([r["open"] for r in price_rows], dtype=float)
    highs = np.array([r["high"] for r in price_rows], dtype=float)
    lows = np.array([r["low"] for r in price_rows], dtype=float)
    prevs = np.array([r["prev_close"] for r in price_rows], dtype=float)
    volumes = np.array([r["volume"] for r in price_rows], dtype=float)
    turnovers = np.array([r["turnover"] for r in price_rows], dtype=float)
    vol_ratios = np.array([r["vol_ratio"] for r in price_rows], dtype=float)
    change_pcts = np.array([r["change_pct"] for r in price_rows], dtype=float)
    high52s = np.array([r["high52"] for r in price_rows], dtype=float)
    low52s = np.array([r["low52"] for r in price_rows], dtype=float)

    T = len(dates)
    feat_list = []

    for i in range(T):
        f = {}
        c = closes[i]
        if c is None or np.isnan(c):
            feat_list.append(None)
            continue

        # ---- 价格动量 ----
        for n in [1, 3, 5, 10, 20]:
            if i >= n:
                f[f"ret_{n}d"] = c / closes[i-n] - 1
            else:
                f[f"ret_{n}d"] = np.nan

        # ---- MA 偏离 ----
        for n in [5, 20, 60]:
            mv = sma(closes[:i+1], n)
            f[f"ma{n}_dev"] = c / mv - 1 if mv else np.nan

        # ---- MA 交叉 ----
        m5 = sma(closes[:i+1], 5)
        m20 = sma(closes[:i+1], 20)
        f["ma_cross"] = m5 / m20 - 1 if (m5 and m20) else np.nan

        # ---- RSI ----
        for p in [6, 14, 24]:
            r = rsi(closes[:i+1], p)
            f[f"rsi{p}"] = r if r is not None else np.nan

        # ---- 波动率 ----
        if i >= 5:
            rets = np.diff(closes[max(0,i-4):i+1]) / closes[max(0,i-4):i]
            f["vol_5d"] = np.std(rets) if len(rets) > 1 else np.nan
        else:
            f["vol_5d"] = np.nan

        if i >= 20:
            rets = np.diff(closes[max(0,i-19):i+1]) / closes[max(0,i-19):i]
            f["vol_20d"] = np.std(rets) if len(rets) > 1 else np.nan
        else:
            f["vol_20d"] = np.nan

        # ---- 振幅 ----
        if highs[i] and lows[i] and c:
            f["amp"] = (highs[i] - lows[i]) / c
        else:
            f["amp"] = np.nan

        # ---- 量比偏离 ----
        f["vol_ratio_dev"] = vol_ratios[i] - 1.0 if vol_ratios[i] is not None else np.nan

        # ---- 放量 ----
        mv20 = sma(volumes[:i+1], 20)
        f["vol_surprise"] = volumes[i] / mv20 - 1 if mv20 else np.nan

        # ---- 量价关系（滚动 5 日量价相关性）----
        if i >= 4:
            p_slice = closes[i-4:i+1]
            v_slice = volumes[i-4:i+1]
            if np.std(p_slice) > 0 and np.std(v_slice) > 0:
                f["pv_corr_5d"] = np.corrcoef(p_slice, v_slice)[0, 1]
            else:
                f["pv_corr_5d"] = np.nan
        else:
            f["pv_corr_5d"] = np.nan

        # ---- 量价背离（当日）----
        f["pv_div"] = np.sign(change_pcts[i]) * (-np.sign(f.get("vol_surprise", 0) or 0))

        # ---- 缺口 ----
        if prevs[i] and opens[i]:
            f["overnight_gap"] = opens[i] / prevs[i] - 1  # T日开盘相对T-1收盘
        else:
            f["overnight_gap"] = np.nan

        # 日内填充幅度：(close-open)/(open-prev_close)，正缺口后是否继续走高
        if f.get("overnight_gap") is not None and abs(f["overnight_gap"]) > 0.001:
            f["gap_fill_ratio"] = (c - opens[i]) / (opens[i] - prevs[i]) if abs(opens[i]-prevs[i]) > 0.01 else 0
        else:
            f["gap_fill_ratio"] = np.nan

        # ---- 52 周位置（指数衰减）----
        if high52s[i] and low52s[i] and high52s[i] > low52s[i] and c:
            k = 14.0
            rg = high52s[i] - low52s[i]
            f["w52"] = np.exp(-k*(c-low52s[i])/rg) - np.exp(-k*(high52s[i]-c)/rg)
        else:
            f["w52"] = np.nan

        # ---- 港股通 ----
        d = dates[i]
        if d in ggt:
            g = ggt[d]
            f["ggt_inflow"] = g["net_inflow"]
            f["ggt_hold_chg"] = g["hold_ratio_chg"]
            # 5/10 日累计
            for w in [5, 10, 20]:
                cum = 0.0
                cnt = 0
                for j in range(max(0, i-w+1), i+1):
                    prev_d = dates[j]
                    if prev_d in ggt and ggt[prev_d]["net_inflow"] is not None:
                        cum += ggt[prev_d]["net_inflow"]
                        cnt += 1
                f[f"ggt_cum{w}d"] = cum if cnt else np.nan
        else:
            f["ggt_inflow"] = np.nan
            f["ggt_hold_chg"] = np.nan
            for w in [5, 10, 20]:
                f[f"ggt_cum{w}d"] = np.nan

        # ---- 沽空 ----
        if d in short_sell and turnovers[i]:
            sr = short_sell[d]["amt"] / (turnovers[i] / 1e8)  # 沽空比例
            f["short_ratio"] = sr
        else:
            f["short_ratio"] = np.nan

        # 沽空变化
        if d in short_sell and i >= 1 and dates[i-1] in short_sell and turnovers[i] and turnovers[i-1]:
            prev_sr = short_sell[dates[i-1]]["amt"] / (turnovers[i-1] / 1e8)
            curr_sr = short_sell[d]["amt"] / (turnovers[i] / 1e8)
            f["short_chg"] = curr_sr - prev_sr
        else:
            f["short_chg"] = np.nan

        # ---- 资金流（从 fund_flow_daily 物化表）----
        if d in fund_flow:
            ff = fund_flow[d]
            # 各档净额/总额
            for tier, cn in [("super","特大"),("big","大"),("mid","中"),("small","小")]:
                buy = ff[f"{tier}_in"]
                sell = ff[f"{tier}_out"]
                total = buy + sell
                f[f"{tier}_net"] = (buy - sell) / total if total > 0 else 0

            # 总主动买卖比
            tb, ts = ff["total_buy"], ff["total_sell"]
            f["total_bsr"] = tb / ts if ts > 0 else 1.0

            # 大资金净占比（特大+大）
            sup_net = (ff["super_in"]-ff["super_out"] + ff["big_in"]-ff["big_out"])
            sup_total = (ff["super_in"]+ff["super_out"] + ff["big_in"]+ff["big_out"])
            f["large_net"] = sup_net / sup_total if sup_total > 0 else 0

            # tick 密度（每分钟 tick 数，代理活跃度）
            f["tick_density"] = ff["tick_count"] / 330.0 if ff["tick_count"] else 0  # 港股 ~330 min/day
        else:
            for k in ["super_net","big_net","mid_net","small_net","total_bsr","large_net","tick_density"]:
                f[k] = np.nan

        # ---- 牛熊证 ----
        if d in cbbc:
            cb = cbbc[d]
            if cb["bull_vol"] and cb["bear_vol"] and (cb["bull_vol"]+cb["bear_vol"]) > 0:
                f["cbbc_bull_ratio"] = cb["bull_vol"] / (cb["bull_vol"] + cb["bear_vol"])
            else:
                f["cbbc_bull_ratio"] = np.nan

            # 牛证距现价距离
            if cb["bull_level"] and c:
                f["bull_dist_pct"] = (c - cb["bull_level"]) / c * 100
            else:
                f["bull_dist_pct"] = np.nan

            # 熊证距现价距离
            if cb["bear_level"] and c:
                f["bear_dist_pct"] = (cb["bear_level"] - c) / c * 100
            else:
                f["bear_dist_pct"] = np.nan
        else:
            for k in ["cbbc_bull_ratio","bull_dist_pct","bear_dist_pct"]:
                f[k] = np.nan

        # ---- 回购 ----
        if d in buyback:
            f["buyback_amt"] = buyback[d]["amt"]
            # 5 日累计回购
            b_cum = 0.0
            for j in range(max(0, i-4), i+1):
                if dates[j] in buyback:
                    b_cum += buyback[dates[j]]["amt"]
            f["buyback_cum5d"] = b_cum
        else:
            f["buyback_amt"] = 0.0
            f["buyback_cum5d"] = 0.0

        # ---- 趋势强度（ADX 简化版：|MA5偏离| / 5日波动）----
        if f.get("ma5_dev") is not None and f.get("vol_5d") is not None and f["vol_5d"] > 0:
            f["trend_strength"] = abs(f["ma5_dev"]) / f["vol_5d"]
        else:
            f["trend_strength"] = np.nan

        feat_list.append(f)

    # ---- 计算 label ----
    labels = {}
    y_1d = np.full(T, np.nan)
    y_5d = np.full(T, np.nan)
    y_10d = np.full(T, np.nan)
    y_20d = np.full(T, np.nan)

    for i in range(T):
        c = closes[i]
        if c is None or np.isnan(c):
            continue
        for n, arr in [(1, y_1d), (5, y_5d), (10, y_10d), (20, y_20d)]:
            if i + n < T and closes[i+n] is not None and not np.isnan(closes[i+n]):
                arr[i] = closes[i+n] / c - 1

    return dates, feat_list, y_1d, y_5d, y_10d, y_20d


# ============ 3. 分析核心 ============

def spearman_ic(feature_vals, labels):
    """Spearman 秩相关系数（IC）"""
    mask = ~np.isnan(feature_vals) & ~np.isnan(labels)
    if mask.sum() < 20:
        return np.nan
    from scipy.stats import spearmanr
    r, _ = spearmanr(feature_vals[mask], labels[mask])
    return r

def quintile_analysis(feature_vals, labels):
    """
    按特征值分 5 组，返回各组的：
    - 平均未来收益
    - 上涨概率
    - 样本数
    """
    if len(feature_vals) < 25:
        return None
    mask = ~np.isnan(feature_vals) & ~np.isnan(labels)
    fv = feature_vals[mask]
    lb = labels[mask]
    if len(fv) < 25:
        return None

    order = np.argsort(fv)
    n = len(order)
    q_size = n // 5
    result = []
    for q in range(5):
        start = q * q_size
        end = (q + 1) * q_size if q < 4 else n
        idx = order[start:end]
        q_labels = lb[idx]
        result.append({
            "q": q + 1,
            "n": len(q_labels),
            "mean_ret": float(np.mean(q_labels)),
            "up_prob": float(np.mean(q_labels > 0)),
            "feature_range": (float(fv[idx[0]]), float(fv[idx[-1]])),
        })
    return result

def conditional_hit_rate(feature_vals, labels, direction="top", pct=20):
    """
    条件胜率：特征值在 top/bottom pct% 时，次日方向匹配概率
    direction="top": 最高 pct% → 应该涨？（正向因子）还是跌？（反向因子）
    """
    if len(feature_vals) < max(10, int(100/pct)):
        return np.nan, 0
    mask = ~np.isnan(feature_vals) & ~np.isnan(labels)
    fv = feature_vals[mask]
    lb = labels[mask]
    if len(fv) < max(10, int(100/pct)):
        return np.nan, 0

    n = len(fv)
    k = max(1, n * pct // 100)
    threshold_high = np.sort(fv)[-k]
    threshold_low = np.sort(fv)[k-1]

    if direction == "top":
        subset = lb[fv >= threshold_high]
    else:
        subset = lb[fv <= threshold_low]

    if len(subset) < 5:
        return np.nan, len(subset)

    # 正向：特征高 → 未来涨 → 胜率
    up_rate = float(np.mean(subset > 0))
    return up_rate, len(subset)


# ============ 4. 主分析流程 ============

def run_scan(stock):
    print(f"\n{'='*80}")
    print(f"  预测因子系统扫描 — {stock}")
    print(f"{'='*80}")

    # 加载
    print("\n[1/5] 加载数据...")
    price_rows = load_price_data(stock)
    fund_flow = load_fund_flow(stock)
    ggt = load_ggt(stock)
    short_sell = load_short_selling(stock)
    cbbc = load_cbbc(stock)
    buyback = load_buyback(stock)

    print(f"  daily_quote: {len(price_rows)} 天")
    print(f"  fund_flow_daily: {len(fund_flow)} 天")
    print(f"  ggt: {len(ggt)} 天")
    print(f"  short_selling: {len(short_sell)} 天")
    print(f"  cbbc: {len(cbbc)} 天")
    print(f"  buyback: {len(buyback)} 天")

    # 特征工程
    print("\n[2/5] 构造特征...")
    dates, feat_list, y_1d, y_5d, y_10d, y_20d = build_dataset(
        price_rows, fund_flow, ggt, short_sell, cbbc, buyback
    )

    # 提取特征矩阵
    all_feat_names = sorted(feat_list[0].keys()) if feat_list[0] else []
    T = len(dates)

    print(f"  特征数: {len(all_feat_names)}")
    print(f"  总天数: {T}")

    # ============ IC 扫描 ============
    print(f"\n[3/5] IC 扫描 (Spearman 秩相关)")
    print(f"  {'-'*70}")
    print(f"  {'特征':24s} {'1D':>8s} {'5D':>8s} {'10D':>8s} {'20D':>8s}  {'覆盖(天)':>8s}")
    print(f"  {'-'*70}")

    ic_results = []
    for fname in all_feat_names:
        fvals = np.array([(fl[fname] if fl else np.nan) for fl in feat_list], dtype=float)
        coverage = int((~np.isnan(fvals)).sum())

        ic1 = spearman_ic(fvals, y_1d)
        ic5 = spearman_ic(fvals, y_5d)
        ic10 = spearman_ic(fvals, y_10d)
        ic20 = spearman_ic(fvals, y_20d)

        best_abs = max(abs(x) for x in [ic1, ic5, ic10, ic20] if not np.isnan(x)) if any(
            not np.isnan(x) for x in [ic1, ic5, ic10, ic20]) else 0

        ic_results.append((fname, ic1, ic5, ic10, ic20, coverage, best_abs))

        def fmt_ic(v):
            if np.isnan(v): return "     N/A"
            return f"{v:+7.3f}"

        mark = ""
        if best_abs >= 0.10:
            mark = " ★"
        elif best_abs >= 0.06:
            mark = " ·"

        print(f"  {fname:24s} {fmt_ic(ic1)} {fmt_ic(ic5)} {fmt_ic(ic10)} {fmt_ic(ic20)}  {coverage:8d}{mark}")

    ic_results.sort(key=lambda x: -x[6])

    # Top 10 IC
    print(f"\n  Top 10 |IC| (任何 horizon):")
    for fname, ic1, ic5, ic10, ic20, cov, ba in ic_results[:10]:
        best_h = max(
            [(1, ic1), (5, ic5), (10, ic10), (20, ic20)],
            key=lambda x: abs(x[1]) if not np.isnan(x[1]) else 0
        )
        print(f"    {fname:24s}  |IC|={ba:.3f}  best@{best_h[0]}D={best_h[1]:+.3f}  n={cov}")

    if ic_results[0][6] < 0.06:
        print(f"\n  ⚠ 所有特征 |IC| < 0.06，单变量线性预测力极弱")

    # ============ 条件概率分析 ============
    print(f"\n[4/5] 条件概率分析（极端分位胜率）")
    print(f"  方法：将特征按值排序，看 top 20% 和 bottom 20% 时次日上涨概率")
    print(f"  基线 = 次日上涨概率（随机 ~50%）")
    print(f"  有意义信号: top 20% 上涨率 > 60% 或 < 40%（即 10pp 以上偏离随机）")
    print(f"  {'-'*70}")
    print(f"  {'特征':24s} {'top20%↑率':>10s} {'bot20%↑率':>10s} {'top_n':>6s} {'bot_n':>6s}  信号")
    print(f"  {'-'*70}")

    cond_results = []
    for fname, ic1, ic5, ic10, ic20, cov, ba in ic_results:
        fvals = np.array([(fl[fname] if fl else np.nan) for fl in feat_list], dtype=float)
        top_up, top_n = conditional_hit_rate(fvals, y_1d, "top", 20)
        bot_up, bot_n = conditional_hit_rate(fvals, y_1d, "bottom", 20)

        signal = ""
        if not np.isnan(top_up) and top_up > 0.60:
            signal += "TOP↑强看涨 "
        elif not np.isnan(top_up) and top_up < 0.40:
            signal += "TOP↓反向看跌 "
        if not np.isnan(bot_up) and bot_up > 0.60:
            signal += "BOT↑看涨 "
        elif not np.isnan(bot_up) and bot_up < 0.40:
            signal += "BOT↓看跌 "

        if signal:
            cond_results.append((fname, top_up, bot_up, top_n, bot_n, signal))

        def fmt_pct(v):
            if np.isnan(v): return "      N/A"
            return f"{v:9.1%}"
        print(f"  {fname:24s} {fmt_pct(top_up)} {fmt_pct(bot_up)} {top_n:6d} {bot_n:6d}  {signal}")

    if not cond_results:
        print(f"\n  ⚠ 没有任何特征在极端分位产生 >60% 或 <40% 的方向信号")

    # ============ 交互特征探测 ============
    print(f"\n[5/5] 交互特征探测（两指标联合条件）")
    print(f"  方法：取 IC 前 8 的特征，两两组合，看联合极端条件下的胜率")

    top8 = [name for name, *_ in ic_results[:8]]
    interaction_hits = []

    for a in top8:
        for b in top8:
            if a >= b:
                continue
            fv_a = np.array([(fl[a] if fl else np.nan) for fl in feat_list], dtype=float)
            fv_b = np.array([(fl[b] if fl else np.nan) for fl in feat_list], dtype=float)

            # 联合 top 20%：两指标同时在 top 20%
            mask = ~np.isnan(fv_a) & ~np.isnan(fv_b) & ~np.isnan(y_1d)
            if mask.sum() < 30:
                continue

            a_top_th = np.sort(fv_a[mask])[-max(1, mask.sum()*20//100)]
            b_top_th = np.sort(fv_b[mask])[-max(1, mask.sum()*20//100)]
            joint_top = mask & (fv_a >= a_top_th) & (fv_b >= b_top_th)
            joint_bot = mask & (fv_a <= np.sort(fv_a[mask])[max(1, mask.sum()*20//100)-1]) & (fv_b <= np.sort(fv_b[mask])[max(1, mask.sum()*20//100)-1])

            top_up = float(np.mean(y_1d[joint_top] > 0)) if joint_top.sum() >= 5 else np.nan
            bot_up = float(np.mean(y_1d[joint_bot] > 0)) if joint_bot.sum() >= 5 else np.nan

            if not np.isnan(top_up) and (top_up > 0.60 or top_up < 0.40):
                interaction_hits.append((a, b, "TOP", top_up, int(joint_top.sum())))
            if not np.isnan(bot_up) and (bot_up > 0.60 or bot_up < 0.40):
                interaction_hits.append((a, b, "BOT", bot_up, int(joint_bot.sum())))

    if interaction_hits:
        interaction_hits.sort(key=lambda x: -abs(x[3]-0.5))
        print(f"  发现 {len(interaction_hits)} 个联合信号:")
        for a, b, side, prob, n in interaction_hits:
            print(f"    {a} × {b} : {side} 联合 → 涨率={prob:.1%} (n={n})")
    else:
        print(f"  未发现显著的联合信号（所有组合极端条件下涨率仍在 40-60% 之间）")

    # ============ 最终诊断 ============
    print(f"\n{'='*80}")
    print(f"  综合诊断 — {stock}")
    print(f"{'='*80}")

    # 1. IC 诊断
    max_ic = ic_results[0][6] if ic_results else 0
    print(f"\n  IC 诊断：最强单因子 |IC| = {max_ic:.3f}")
    if max_ic < 0.05:
        print(f"    → 极弱（任何线性预测模型在此数据上几乎不可能超过随机）")
    elif max_ic < 0.10:
        print(f"    → 弱（需极强组合/非线性方法才可能有用）")
    else:
        print(f"    → 有一定信号，值得深入")

    # 2. 条件概率诊断
    if cond_results:
        print(f"\n  条件概率信号: {len(cond_results)} 个")
        for fname, top_up, bot_up, top_n, bot_n, signal in cond_results[:5]:
            print(f"    {fname}: {signal.strip()} (n_top={top_n}, n_bot={bot_n})")
    else:
        print(f"\n  条件概率信号: 0 个 — 极端值也无法提高方向判断")

    # 3. 交互特征诊断
    if interaction_hits:
        print(f"\n  交互特征信号: {len(interaction_hits)} 个")
    else:
        print(f"\n  交互特征信号: 0 个")

    # 4. Horizon 效应
    print(f"\n  多周期表现（以最强 IC 因子为例）:")
    for fname, ic1, ic5, ic10, ic20, cov, ba in ic_results[:3]:
        print(f"    {fname:24s}: 1D={ic1:+.3f} 5D={ic5:+.3f} 10D={ic10:+.3f} 20D={ic20:+.3f}")

    # 5. 总体结论
    print(f"\n  {'─'*60}")
    if max_ic < 0.06 and not cond_results and not interaction_hits:
        print(f"  结论: 在 {stock} 的日线数据上，没有任何单一指标或其简单组合")
        print(f"        能在统计上显著预测未来 1~20 日收益方向。")
        print(f"        这与弱式有效市场假说一致。")
        print(f"  ")
        print(f"  建议方向:")
        print(f"    1. 转向更微观数据（分钟级 tick flow 不平衡）")
        print(f"    2. 引入外部事件（财报日、政策公告、大宗交易）")
        print(f"    3. 区制切换模型（只在特定市场状态下预测）")
        print(f"    4. 放弃方向预测，转向波动率/尾部风险预测")
        print(f"    5. 跨品种套利（相对价值，而非绝对方向）")
    elif max_ic < 0.10:
        print(f"  结论: 存在微弱但真实的预测信号，需要:")
        print(f"    1. 非线性模型（GBDT/XGBoost）而非线性回归")
        print(f"    2. 更长的训练窗口 + walk-forward 严谨验证")
        print(f"    3. 只在信号极端时出手（<30% 覆盖率换取 >60% 精度）")
    else:
        print(f"  结论: 存在可用的预测信号，建议进一步开发交易策略")


if __name__ == "__main__":
    run_scan(STOCK)
