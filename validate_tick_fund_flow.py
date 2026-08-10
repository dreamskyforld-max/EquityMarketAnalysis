#!/usr/bin/env python3
"""
从 tick_data 动态聚合日级资金流，用生产环境阈值，做预测力验证

与 fund_flow_daily 的区别：
- fund_flow_daily：物化表，仅 38 天
- 本脚本：直接从 tick_data 聚合，覆盖全部 tick_data 历史（1500+ 天）

阈值：25000/7000/300 股（stock-realtime 生产环境，与 train_buy_sell_index 一致）
"""
import sys
import numpy as np
from collections import defaultdict
from scipy.stats import spearmanr
from datetime import date

from db import get_conn

STOCK = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"

# ── 阈值（生产环境） ──
XL_VOL = 25_000   # 特大单 ≥25000 股
LG_VOL = 7_000    # 大单 ≥7000 股
MD_VOL = 300      # 中单 ≥300 股
# 小单 <300 股

TIER_NAMES = ["特大单", "大单", "中单", "小单"]
TIER_COLS = ["xl_net", "lg_net", "md_net", "sm_net"]


# ════════════════════════════════════════════════════
# 数据加载
# ════════════════════════════════════════════════════

def load_price(stock):
    """加载日线价格"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT trade_date, last_price, volume, turnover
               FROM daily_quote WHERE stock_code=%s
               ORDER BY trade_date""",
            (stock,),
        )
        rows = cur.fetchall()
    return rows  # list of (date, last_price, volume, turnover)


def load_tick_flow_daily(stock):
    """
    从 tick_data 逐日聚合四档资金流（全部历史）
    返回 dict: date -> {xl_in, xl_out, lg_in, lg_out, md_in, md_out, sm_in, sm_out, tick_count, turnover_sum}
    """
    with get_conn() as conn:
        cur = conn.cursor()

        # 查有哪些交易日
        cur.execute(
            """SELECT DISTINCT DATE(tick_time) AS td
               FROM tick_data WHERE stock_code=%s
               AND ticker_direction IN ('BUY','SELL')
               ORDER BY td""",
            (stock,),
        )
        trade_dates = [r[0] for r in cur.fetchall()]
        print(f"  tick_data 覆盖 {len(trade_dates)} 个交易日")

    result = {}
    batch_size = 30  # 逐批聚合

    # 按日期分批聚合（dates 来自 DB 查询，无注入风险；用命名参数传日期列表）
    with get_conn() as conn:
        cur = conn.cursor()
        for i in range(0, len(trade_dates), batch_size):
            batch = trade_dates[i : i + batch_size]
            print(f"  聚合 {batch[0]} ~ {batch[-1]} ...", end=" ", flush=True)

            date_refs = ",".join(["%(" + f"d{j}" + ")s" for j in range(len(batch))])
            params = {"xl": XL_VOL, "lg": LG_VOL, "md": MD_VOL, "code": stock}
            for j, d in enumerate(batch):
                params[f"d{j}"] = d

            cur.execute(
                f"""SELECT DATE(tick_time) AS td,
                          COALESCE(SUM(CASE WHEN volume>=%(xl)s AND ticker_direction='BUY'  THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume>=%(xl)s AND ticker_direction='SELL' THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume>=%(lg)s AND volume<%(xl)s AND ticker_direction='BUY'  THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume>=%(lg)s AND volume<%(xl)s AND ticker_direction='SELL' THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume>=%(md)s AND volume<%(lg)s AND ticker_direction='BUY'  THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume>=%(md)s AND volume<%(lg)s AND ticker_direction='SELL' THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume<%(md)s AND ticker_direction='BUY'  THEN turnover END),0),
                          COALESCE(SUM(CASE WHEN volume<%(md)s AND ticker_direction='SELL' THEN turnover END),0),
                          COUNT(*),
                          SUM(turnover)
                   FROM tick_data
                   WHERE stock_code=%(code)s
                     AND ticker_direction IN ('BUY','SELL')
                     AND DATE(tick_time) IN ({date_refs})
                   GROUP BY td
                   ORDER BY td""",
                params,
            )
            rows = cur.fetchall()

            for r in rows:
                td = r[0]
                result[td] = {
                    "xl_in": float(r[1] or 0), "xl_out": float(r[2] or 0),
                    "lg_in": float(r[3] or 0), "lg_out": float(r[4] or 0),
                    "md_in": float(r[5] or 0), "md_out": float(r[6] or 0),
                    "sm_in": float(r[7] or 0), "sm_out": float(r[8] or 0),
                    "tick_count": int(r[9] or 0),
                    "turnover_sum": float(r[10] or 0),
                }
            print(f"{len(rows)} 天")

    return result


# ════════════════════════════════════════════════════
# 分析函数
# ════════════════════════════════════════════════════

def build_dataset(price_rows, flow):
    """对齐价格和资金流，构建特征矩阵"""
    dates = []
    close_arr = []
    volume_arr = []

    feats = defaultdict(list)

    for i, (td, close, vol, turnover) in enumerate(price_rows):
        if td not in flow:
            continue

        f = flow[td]
        dates.append(td)
        close_arr.append(float(close))
        volume_arr.append(float(vol or 0))

        # 四档净买入（亿元）
        total_turnover = float(turnover or 0)
        if total_turnover > 0:
            feats["xl_net"].append((f["xl_in"] - f["xl_out"]) / 1e8)
            feats["lg_net"].append((f["lg_in"] - f["lg_out"]) / 1e8)
            feats["md_net"].append((f["md_in"] - f["md_out"]) / 1e8)
            feats["sm_net"].append((f["sm_in"] - f["sm_out"]) / 1e8)
        else:
            feats["xl_net"].append(np.nan)
            feats["lg_net"].append(np.nan)
            feats["md_net"].append(np.nan)
            feats["sm_net"].append(np.nan)

        # 四档占比（主动买入 / 总成交）
        if f["turnover_sum"] > 0:
            feats["xl_ratio"].append(f["xl_in"] / f["turnover_sum"])
            feats["lg_ratio"].append(f["lg_in"] / f["turnover_sum"])
            feats["md_ratio"].append(f["md_in"] / f["turnover_sum"])
            feats["sm_ratio"].append(f["sm_in"] / f["turnover_sum"])
        else:
            feats["xl_ratio"].append(np.nan)
            feats["lg_ratio"].append(np.nan)
            feats["md_ratio"].append(np.nan)
            feats["sm_ratio"].append(np.nan)

        # 总主动买卖比
        total_in = f["xl_in"] + f["lg_in"] + f["md_in"] + f["sm_in"]
        total_out = f["xl_out"] + f["lg_out"] + f["md_out"] + f["sm_out"]
        if total_out > 0:
            feats["total_bsr"].append(total_in / total_out)
        else:
            feats["total_bsr"].append(np.nan)

        # 大资金 vs 小资金 差异
        if total_turnover > 0:
            big_net = (f["xl_in"] - f["xl_out"] + f["lg_in"] - f["lg_out"]) / 1e8
            small_net = (f["md_in"] - f["md_out"] + f["sm_in"] - f["sm_out"]) / 1e8
            feats["big_minus_small"].append(big_net - small_net)
        else:
            feats["big_minus_small"].append(np.nan)

        # 主动买入占比（全档 in / 总 turnover）
        if total_turnover > 0:
            feats["active_buy_ratio"].append(total_in / total_turnover)
        else:
            feats["active_buy_ratio"].append(np.nan)

        # tick 密度（笔数）
        feats["tick_count"].append(float(f["tick_count"]))

    # 转 numpy
    close = np.array(close_arr, dtype=float)
    volume = np.array(volume_arr, dtype=float)
    n = len(close)

    feat_arrays = {}
    for k, v in feats.items():
        feat_arrays[k] = np.array(v, dtype=float)

    # 未来 N 日收益标签
    labels = {}
    for horizon in [1, 5, 10, 20]:
        lab = np.full(n, np.nan)
        for i in range(n - horizon):
            lab[i] = close[i + horizon] / close[i] - 1
        labels[horizon] = lab

    return dates, close, volume, feat_arrays, labels


def spearman_ic(fvals, labels):
    """Spearman IC"""
    mask = ~np.isnan(fvals) & ~np.isnan(labels)
    if mask.sum() < 10:
        return np.nan
    r, _ = spearmanr(fvals[mask], labels[mask])
    return float(r)


def detrend_ic(fvals, labels, close, lookback):
    """去趋势：特征对过去 lookback 日收益回归，残差 IC"""
    n = len(close)
    past_ret = np.full(n, np.nan)
    for i in range(lookback, n):
        past_ret[i] = close[i] / close[i - lookback] - 1

    mask = ~np.isnan(fvals) & ~np.isnan(labels) & ~np.isnan(past_ret)
    if mask.sum() < 10:
        return np.nan, np.nan

    x = fvals[mask]
    y = labels[mask]
    p = past_ret[mask]

    # 回归：fvals = alpha + beta * past_ret + residual
    A = np.column_stack([np.ones_like(p), p])
    beta, _, _, _ = np.linalg.lstsq(A, x, rcond=None)
    residual = x - A @ beta

    r_raw, _ = spearmanr(x, y)
    r_res, _ = spearmanr(residual, y)
    return float(r_raw), float(r_res)


def rolling_ic(fvals, labels, window=60):
    """滚动窗口 IC"""
    mask = ~np.isnan(fvals) & ~np.isnan(labels)
    x, y = fvals[mask], labels[mask]
    n = len(x)
    if n < window:
        return np.nan, np.nan, np.nan, 0

    ics = []
    for i in range(window - 1, n):
        sx, sy = x[i - window + 1 : i + 1], y[i - window + 1 : i + 1]
        m = ~np.isnan(sx) & ~np.isnan(sy)
        if m.sum() < 15:
            ics.append(np.nan)
        else:
            r, _ = spearmanr(sx[m], sy[m])
            ics.append(r)

    ics = np.array(ics)
    valid = ics[~np.isnan(ics)]
    if len(valid) < 5:
        return np.nan, np.nan, np.nan, 0
    return float(np.mean(valid)), float(np.std(valid)), float(np.mean(valid > 0)), len(valid)


def walk_forward_check(fvals, labels, close, min_train=120):
    """向前滚动预测：不 look-ahead"""
    mask = ~np.isnan(fvals) & ~np.isnan(labels)
    x, y = fvals[mask], labels[mask]
    n = len(x)
    if n <= min_train:
        return np.nan, np.nan, 0

    preds = []
    actuals = []
    for t in range(min_train, n):
        # 只用 t 之前的数据训练（简单线性回归）
        train_x = x[:t]
        train_y = y[:t]
        m = ~np.isnan(train_y)
        if m.sum() < 10:
            continue
        A = np.column_stack([np.ones_like(train_x[m]), train_x[m]])
        try:
            beta, _, _, _ = np.linalg.lstsq(A, train_y[m], rcond=None)
        except np.linalg.LinAlgError:
            continue
        pred = np.array([1, x[t]]) @ beta
        preds.append(pred)
        actuals.append(y[t])

    if len(preds) < 30:
        return np.nan, np.nan, 0
    preds = np.array(preds)
    actuals = np.array(actuals)
    r, _ = spearmanr(preds, actuals)
    # 准确率（方向正确）
    acc = np.mean((preds > 0) == (actuals > 0))
    return float(r), float(acc), len(preds)


def subsample_stability(fvals, labels, n_boot=200):
    """Bootstrap 稳定性：随机抽 80% 样本算 IC，看分布"""
    mask = ~np.isnan(fvals) & ~np.isnan(labels)
    x, y = fvals[mask], labels[mask]
    n = len(x)
    if n < 20:
        return np.nan, np.nan, np.nan

    ics = []
    for _ in range(min(n_boot, 500)):
        idx = np.random.choice(n, size=int(n * 0.8), replace=True)
        try:
            r, _ = spearmanr(x[idx], y[idx])
            ics.append(r)
        except:
            ics.append(np.nan)

    ics = np.array([i for i in ics if not np.isnan(i)])
    if len(ics) < 10:
        return np.nan, np.nan, np.nan
    return float(np.mean(ics)), float(np.std(ics)), float(np.mean(np.array(ics) > 0))


# ════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════

def main():
    stock = STOCK
    print(f"\n{'='*100}")
    print(f"  从 tick_data 动态聚合资金流 — 预测力系统验证 ({stock})")
    print(f"  阈值: 特大≥{XL_VOL}股 | 大≥{LG_VOL}股 | 中≥{MD_VOL}股 | 小<{MD_VOL}股")
    print(f"{'='*100}")

    # 1. 加载数据
    print("\n[1/3] 加载价格数据...")
    price_rows = load_price(stock)
    print(f"  daily_quote: {len(price_rows)} 天")

    print("\n[2/3] 从 tick_data 动态聚合日级资金流...")
    flow = load_tick_flow_daily(stock)

    # 2. 构建数据集
    print("\n[3/3] 对齐数据...")
    dates, close, volume, feats, labels = build_dataset(price_rows, flow)
    print(f"  对齐后: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")

    # 3. 计算每个特征的各种 IC
    horizons = [1, 5, 10, 20]
    feat_names = sorted(feats.keys())

    # 先打印数据概览
    print(f"\n{'─'*100}")
    print(f"  数据覆盖概览")
    print(f"{'─'*100}")
    for fname in feat_names:
        fv = feats[fname]
        valid = (~np.isnan(fv)).sum()
        print(f"  {fname:24s}  有效 {valid:5d}/{len(fv)} 天  "
              f"均值 {np.nanmean(fv):+.4f}  中位数 {np.nanmedian(fv):+.4f}")

    # 4. IC 表
    print(f"\n{'─'*100}")
    print(f"  Spearman IC（特征 → 未来 N 日收益）")
    print(f"{'─'*100}")
    header = f"  {'特征':24s}"
    for h in horizons:
        header += f"  {'IC@' + str(h) + 'D':>10s}"
    print(header)
    print(f"  {'─'*24}{'  ─'*len(horizons)*5}")

    ic_results = {}
    for fname in feat_names:
        fv = feats[fname]
        row = f"  {fname:24s}"
        best_abs = 0
        best_h = 1
        best_ic = np.nan
        for h in horizons:
            ic = spearman_ic(fv, labels[h])
            row += f"  {ic:+.4f}" if not np.isnan(ic) else "       N/A"
            if not np.isnan(ic) and abs(ic) > best_abs:
                best_abs = abs(ic)
                best_h = h
                best_ic = ic
        print(row)
        ic_results[fname] = {"best_h": best_h, "best_ic": best_ic, "best_abs": best_abs, "fv": fv}

    # 5. 去趋势检验
    print(f"\n{'─'*100}")
    print(f"  去趋势残差 IC（控制过去 N 日价格趋势后，残差的预测力）")
    print(f"{'─'*100}")
    print(f"  {'特征':24s}  {'最佳原始IC':>12s}  {'H':>3s}  {'残差IC(N=3)':>13s}  {'残差IC(N=5)':>13s}  {'残差IC(N=10)':>13s}  {'残差IC(N=20)':>13s}  {'判定':>10s}")
    print(f"  {'─'*24}  {'─'*12}  {'─'*3}  {'─'*13}  {'─'*13}  {'─'*13}  {'─'*13}  {'─'*10}")

    strong_signals = []
    weak_signals = []
    failed_signals = []

    for fname in feat_names:
        info = ic_results[fname]
        if abs(info["best_ic"]) < 0.03:
            continue

        fv = info["fv"]
        best_h = info["best_h"]
        best_ic = info["best_ic"]
        lab = labels[best_h]
        cov = int((~np.isnan(fv)).sum())

        row = f"  {fname:24s}  {best_ic:+.4f}@{best_h}D"

        residual_ics = []
        for lb in [3, 5, 10, 20]:
            r_raw, r_res = detrend_ic(fv, lab, close, lb)
            row += f"  {r_res:+.4f}" if not np.isnan(r_res) else "          N/A"
            if not np.isnan(r_res):
                residual_ics.append(r_res)

        # 判定
        if residual_ics:
            mid_res = sorted(residual_ics, key=lambda x: abs(x))[len(residual_ics) // 2]
            if abs(mid_res) >= 0.15:
                verdict = "★★★ 强"
                strong_signals.append((fname, best_h, best_ic, mid_res, cov, residual_ics))
            elif abs(mid_res) >= 0.08:
                verdict = "★ 有价值"
                weak_signals.append((fname, best_h, best_ic, mid_res, cov, residual_ics))
            elif abs(best_ic) >= 0.08:
                verdict = "✗ 趋势代理"
                failed_signals.append((fname, best_h, best_ic, mid_res, cov))
            else:
                verdict = "— 弱"
        else:
            verdict = "—"

        print(f"  {row}  {verdict:>10s}")

    # 6. 重点特征：滚动 IC + walk-forward + bootstrap
    print(f"\n{'='*100}")
    print(f"  重点特征深度验证（去趋势残差 IC ≥ 0.08 的特征）")
    print(f"{'='*100}")

    candidates = strong_signals + weak_signals
    if not candidates:
        print("\n  无可深度验证的特征。")
        print_summary(strong_signals, weak_signals, failed_signals, dates)
        return

    print(f"\n  {'特征':24s}  {'最佳IC/H':>12s}  {'残差IC':>8s}  "
          f"{'滚动IC(均值±std)':>22s}  {'Bootstrap(均值±std)':>24s}  "
          f"{'向前IC':>8s}  {'向前准确率':>10s}  {'覆盖':>6s}")
    print(f"  {'─'*24}  {'─'*12}  {'─'*8}  {'─'*22}  {'─'*24}  {'─'*8}  {'─'*10}  {'─'*6}")

    for name, best_h, best_ic, mid_res, cov, residual_ics in candidates:
        fv = feats[name]
        lab = labels[best_h]

        # 滚动 IC
        rmean, rstd, rpos, rn = rolling_ic(fv, lab, window=60)

        # Bootstrap
        bmean, bstd, bpos = subsample_stability(fv, lab)

        # Walk-forward
        wf_ic, wf_acc, wf_n = walk_forward_check(fv, lab, close)

        def f_s(v, w=8):
            if np.isnan(v):
                return "     N/A".rjust(w)
            return f"{v:+.4f}".rjust(w)

        def f_s2(v, w=10):
            if np.isnan(v):
                return "     N/A".rjust(w)
            return f"{v:.4f}".rjust(w)

        ic_str = f"{best_ic:+.3f}@{best_h}D".rjust(12)
        res_str = f_s(mid_res, 8)

        if not np.isnan(rmean):
            roll_str = f"{rmean:+.4f}±{rstd:.4f}".rjust(22)
        else:
            roll_str = "       N/A".rjust(22)

        if not np.isnan(bmean):
            boot_str = f"{bmean:+.4f}±{bstd:.4f}({bpos:.0%})".rjust(24)
        else:
            boot_str = "       N/A".rjust(24)

        wf_ic_str = f_s(wf_ic, 8)
        wf_acc_str = f_s2(wf_acc, 10)

        print(f"  {name:24s}  {ic_str}  {res_str}  {roll_str}  {boot_str}  {wf_ic_str}  {wf_acc_str}  {cov:6d}")

    # 7. 各档相关性矩阵
    print(f"\n{'─'*100}")
    print(f"  四档净买入之间的相关性矩阵")
    print(f"{'─'*100}")
    tier_keys = ["xl_net", "lg_net", "md_net", "sm_net"]
    tier_cn = ["特大单", "大单", "中单", "小单"]
    header = "        " + "".join(f"{t:>10s}" for t in tier_cn)
    print(header)
    for i, (ki, ti) in enumerate(zip(tier_keys, tier_cn)):
        row = f"  {ti:4s}  "
        for kj in tier_keys:
            mask = ~np.isnan(feats[ki]) & ~np.isnan(feats[kj])
            if mask.sum() < 10:
                row += "       N/A"
            else:
                r, _ = spearmanr(feats[ki][mask], feats[kj][mask])
                row += f"  {r:+.4f}"
        print(row)

    print_summary(strong_signals, weak_signals, failed_signals, dates)


def print_summary(strong, weak, failed, dates):
    print(f"\n{'='*100}")
    print(f"  总  结")
    print(f"{'='*100}")
    print(f"  数据范围: {dates[0]} ~ {dates[-1]} ({len(dates)} 天)")

    if strong:
        print(f"\n  ★★★ 去趋势后强残差信号 (残差IC≥0.15, {len(strong)} 个):")
        for name, h, raw, res, cov, _ in strong:
            print(f"    {name:24s}  原始IC{raw:+.4f}@{h}D  残差IC{res:+.4f}  n={cov}")

    if weak:
        print(f"\n  ★ 去趋势后有价值的信号 (残差IC≥0.08, {len(weak)} 个):")
        for name, h, raw, res, cov, _ in weak:
            print(f"    {name:24s}  原始IC{raw:+.4f}@{h}D  残差IC{res:+.4f}  n={cov}")

    if failed:
        print(f"\n  ✗ 去趋势后退化为趋势代理 (原始IC≥0.08 但残差<0.08, {len(failed)} 个):")
        for name, h, raw, res, cov in failed:
            print(f"    {name:24s}  原始IC{raw:+.4f}@{h}D  残差IC{res:+.4f}  n={cov}")

    if not strong and not weak:
        print(f"\n  ⚠ 从 tick_data 聚合的 {len(dates)} 天数据中，没有任何资金流特征在去趋势后")
        print(f"    保留 ≥0.08 的残差 IC。")
        print(f"    这意味着即使把样本从 38 天扩展到 {len(dates)} 天，日线资金流对股价方向")
        print(f"    的预测力仍未超过'价格趋势的代理'范畴。")
    elif strong:
        print(f"\n  ✓ 发现去趋势后仍有预测力的信号。建议: 在 stock-realtime 项目前端展示")
        print(f"    对应指标的时序图，让用户可以观察其与股价的联动关系。")


if __name__ == "__main__":
    main()
