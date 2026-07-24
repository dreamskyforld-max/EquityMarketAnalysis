#!/usr/bin/env python3
"""OFI（订单流不平衡）→ 短期收益 验证。

验证 tick_data 的主动买卖压力是否对未来分钟级收益有领先预测力。
- 特征：每分钟 OFI = Σ(BUY turnover) − Σ(SELL turnover)，归一化到 [-1,1]
- 标签：未来 k 分钟收益 ret_k = close[i+k]/close[i] − 1，方向 y = sign(ret_k)
- 方法：
  1) IC（Spearman）：OFI(t) vs ret_k(t)，按交易日分层平均（抗单日异常）
  2) walk-forward 分类：pooled_logit 滚动训练，无未来泄漏，看 AUC / 双向精度 / 仅做空精度
- 关键判据：OFI 对未来 k>0 收益的 IC 是否显著 > 0，且随 k 增大而衰减（=领先信号而非同步噪声）

单标的（默认 HK.00700）。复用 direction_backtest.pooled_logit。
"""
import sys
sys.argv = sys.argv[:1]  # 防止 direction_backtest 顶层误读本脚本的 argv
import json
import numpy as np
import psycopg2
from collections import defaultdict
from scipy.stats import spearmanr
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
import direction_backtest as M

import config
DB = dict(
    host=config.val("database", "host", "DB_HOST", "localhost"),
    port=int(config.val("database", "port", "DB_PORT", "5432")),
    dbname=config.val("database", "dbname", "DB_NAME", "market_db"),
    user=config.val("database", "user", "DB_USER", "market_user"),
    password=config.val("database", "password", "DB_PASSWORD", ""),
)


def load_ticks(stock="HK.00700"):
    conn = psycopg2.connect(host=DB["host"], port=DB["port"],
                             dbname=DB["dbname"], user=DB["user"], password=DB["password"])
    cur = conn.cursor()
    cur.execute("""SELECT tick_time, price, turnover, ticker_direction
                   FROM tick_data WHERE stock_code=%s AND ticker_direction IN ('BUY','SELL')
                   ORDER BY tick_time""", (stock,))
    rows = cur.fetchall()
    conn.close()
    return rows


def build_minute_bars(rows):
    """按分钟聚合，返回 (mks, ofi_norm, ofi_raw, close)。"""
    bars = {}
    for t, price, turnover, d in rows:
        mk = t.replace(second=0, microsecond=0)
        buy, sell, _ = bars.get(mk, (0.0, 0.0, 0.0))
        if d == 'BUY':
            buy += float(turnover)
        else:
            sell += float(turnover)
        bars[mk] = (buy, sell, float(price))  # 最后一笔 price 作收盘
    mks = sorted(bars)
    buy = np.array([bars[m][0] for m in mks])
    sell = np.array([bars[m][1] for m in mks])
    close = np.array([bars[m][2] for m in mks])
    ofi_raw = buy - sell
    tot = buy + sell
    ofi_norm = np.where(tot > 0, ofi_raw / tot, 0.0)
    return mks, ofi_norm, ofi_raw, close


def ic_by_day(mks, feat, ret, k):
    """按交易日分层算 OFI(t) vs ret_k(t) 的 Spearman，返回 (均值IC, 有效天数)。"""
    by_day = defaultdict(list)
    for i in range(len(mks) - k):
        by_day[mks[i].date()].append((feat[i], ret[i]))
    ics = []
    for pairs in by_day.values():
        f = np.array([p[0] for p in pairs])
        r = np.array([p[1] for p in pairs])
        if len(f) > 5 and np.std(f) > 0 and np.std(r) > 0:
            rho, _ = spearmanr(f, r)
            ics.append(rho)
    return (float(np.nanmean(ics)) if ics else np.nan, len(ics))


def walk_forward(samples, min_train=800):
    """复用 pooled_logit 做滚动 walk-forward，返回指标。"""
    preds, trues, proba, _, _ = M.pooled_logit(samples, min_train, 1.0, [0])
    if len(trues) < 50:
        return None
    trues = np.array(trues)
    proba = np.array(proba)
    y01 = (trues > 0).astype(int)
    auc = roc_auc_score(y01, proba)
    pred = (proba > 0.5).astype(int) * 2 - 1
    acc = float((pred == trues).mean())
    # 仅做空精度：p_up < 0.4 时实际跌的比例
    mask = proba < 0.4
    if mask.sum() > 0:
        dp = float((trues[mask] < 0).mean())
        dn = int(mask.sum())
    else:
        dp, dn = float('nan'), 0
    return auc, acc, dp, dn, len(trues)


def main(stock="HK.00700"):
    rows = load_ticks(stock)
    mks, ofi_norm, ofi_raw, close = build_minute_bars(rows)
    print(f"{stock}: {len(rows):,} ticks -> {len(mks):,} 分钟bars, "
          f"{mks[0]} ~ {mks[-1]}")
    print(f"  OFI_norm 均值={ofi_norm.mean():+.4f}  std={ofi_norm.std():.4f}  "
          f"min={ofi_norm.min():.3f} max={ofi_norm.max():.3f}")
    print()

    ks = [0, 1, 5, 15, 30]
    print("=" * 70)
    print("IC（Spearman）: OFI vs 未来 k 分钟收益（按交易日分层平均）")
    print("=" * 70)
    print(f"{'k':>3} {'ofi_norm_IC':>12} {'ofi_raw_IC':>12} {'有效天数':>8}  说明")
    for k in ks:
        ret = close[k:] / close[:-k] - 1 if k > 0 else close[1:] / close[:-1] - 1
        rn, nd = ic_by_day(mks, ofi_norm, ret, k if k > 0 else 1)
        rr, _ = ic_by_day(mks, ofi_raw, ret, k if k > 0 else 1)
        note = "（同步，仅作对照）" if k == 0 else ""
        print(f"{k:3d} {rn:12.4f} {rr:12.4f} {nd:8d}  {note}")

    print()
    print("=" * 70)
    print("walk-forward 分类：OFI_norm -> 未来 k 分钟涨跌方向")
    print("=" * 70)
    print(f"{'k':>3} {'AUC':>7} {'acc':>7} {'down_prec':>10} {'down_n':>7} {'n_test':>7}")
    for k in ks:
        if k == 0:
            continue
        ret = close[k:] / close[:-k] - 1
        y = ((ret > 0).astype(int) * 2 - 1)  # +1 涨 / -1 跌
        samples = [(mks[i], i, np.array([ofi_norm[i]]), int(y[i]))
                   for i in range(len(mks) - k)]
        res = walk_forward(samples, min_train=800)
        if res is None:
            print(f"{k:3d} 样本不足")
            continue
        auc, acc, dp, dn, nt = res
        print(f"{k:3d} {auc:7.3f} {acc:7.3f} {dp:10.3f} {dn:7d} {nt:7d}")

    print()
    print("解读：")
    print("  - IC/AUC 接近 0.5 → OFI 无领先预测力（与日线特征同命运）")
    print("  - IC>0 且随 k 增大衰减 → OFI 是真正的短期领先信号")
    print("  - AUC>0.55 + down_prec 稳定 > 跌基率 → 值得进指标验证工作台")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "HK.00700")
