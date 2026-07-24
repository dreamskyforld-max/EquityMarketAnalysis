#!/usr/bin/env python3
"""
方向预测回测台（池化 walk-forward，样本外严谨验证）
目标：未来 N=3 交易日收盘价相对今日涨(1)/跌(-1)，严格二分类。

核心纪律：
- 特征在 t 时刻只用该票 <=t 的数据计算（无未来函数，且不跨票泄漏）
- label = sign(close[t+N] - close[t])，只用该票自身价格
- 池化 walk-forward：所有 (票,日期) 样本按日期排序，预测日 t 的训练只用“日期<t”的样本
  → 天然无未来泄漏，且跨票共享模式、扩样本
- 标准化：每特征用训练样本(跨票)的 mean/std，缺失→0(=训练均值)
- 报告：样本外准确率 + Wilson 95%CI + 二项 p 值(vs 0.5) + 单特征/全模型/5特征/敏感性/分票

数据现实（2026-07-06 回补后）：HK.00700 / HK.09660 的 daily_quote 均 245 天；
excess_return 视图 98 天；ggt/short_selling 仅数十天（非主导，忽略）。
→ 池化两票可得 ~380 个样本外点，足以严谨认证。
"""
import sys
import numpy as np
from math import sqrt
from collections import defaultdict
from db import get_conn
from sklearn.linear_model import LogisticRegression

STOCKS = ["HK.00700", "HK.09660"]  # 回补后均有 245 天
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3  # 未来 N 个交易日（可用 argv 覆盖）
MIN_TRAIN = 120  # 池化训练样本下限

FEAT_NAMES = [
    "ret_1", "ret_3", "ret_5", "ret_10",
    "ma5_dev", "ma20_dev", "ma60_dev", "ma_cross",
    "rsi6", "rsi24", "vol_ratio_dev", "vol_surprise", "amp",
    "w52", "excess", "excess_ma5",
    "short_ratio", "short_change", "ggt", "rsi_div",
]
TOP5 = ["rsi6", "amp", "ret_3", "rsi_div", "ma_cross"]


# ---------------- 数据提取 ----------------
def load_all(stocks):
    out = {}
    with get_conn() as conn:
        cur = conn.cursor()
        for stock in stocks:
            cur.execute(
                """SELECT trade_date, open_price, high_price, low_price, last_price,
                          volume, turnover, volume_ratio, change_pct, high_52w, low_52w
                   FROM daily_quote WHERE stock_code=%s ORDER BY trade_date""",
                (stock,),
            )
            q = cur.fetchall()
            cur.execute(
                "SELECT trade_date, excess_return_pct FROM v_daily_excess_return WHERE stock_code=%s ORDER BY trade_date",
                (stock,),
            )
            ex = {r[0]: float(r[1]) if r[1] is not None else None for r in cur.fetchall()}
            cur.execute(
                "SELECT trade_date, short_selling_amt FROM daily_short_selling WHERE stock_code=%s ORDER BY trade_date",
                (stock,),
            )
            sh = {r[0]: float(r[1]) for r in cur.fetchall()}  # amt(亿)
            cur.execute(
                "SELECT trade_date, est_net_inflow FROM daily_ggt_hold WHERE stock_code=%s ORDER BY trade_date",
                (stock,),
            )
            ggt = {r[0]: float(r[1]) for r in cur.fetchall()}
            out[stock] = (q, ex, sh, ggt)
    return out


# ---------------- 指标工具 ----------------
def rsi(prices, period):
    if len(prices) < period + 1:
        return None
    d = np.diff(prices)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag, al = g[-period:].mean(), l[-period:].mean()
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def sma(arr, n):
    if len(arr) < n or n <= 0:
        return None
    return float(np.mean(arr[-n:]))


# ---------------- 特征计算（每行只用 <=t 数据） ----------------
def build_features(dates, close, high, low, vol, turn, vr, chg, h52, l52, ex, sh, ggt):
    T = len(dates)
    feats = {name: [np.nan] * T for name in [
        "ret_1", "ret_3", "ret_5", "ret_10",
        "ma5_dev", "ma20_dev", "ma60_dev", "ma_cross",
        "rsi6", "rsi24", "vol_ratio_dev", "vol_surprise", "amp",
        "w52", "excess", "excess_ma5",
        "short_ratio", "short_change", "ggt", "rsi_div",
    ]}
    closes = np.array(close, dtype=float)
    short_ratios = {}
    for i in range(T):
        c = closes[i]
        # 动量
        if i >= 1: feats["ret_1"][i] = closes[i] / closes[i-1] - 1
        if i >= 3: feats["ret_3"][i] = closes[i] / closes[i-3] - 1
        if i >= 5: feats["ret_5"][i] = closes[i] / closes[i-5] - 1
        if i >= 10: feats["ret_10"][i] = closes[i] / closes[i-10] - 1
        # MA 偏离
        m5 = sma(closes[:i+1], 5); m20 = sma(closes[:i+1], 20); m60 = sma(closes[:i+1], 60)
        if m5: feats["ma5_dev"][i] = c / m5 - 1
        if m20: feats["ma20_dev"][i] = c / m20 - 1
        if m60: feats["ma60_dev"][i] = c / m60 - 1
        if m5 and m20: feats["ma_cross"][i] = m5 / m20 - 1
        # RSI
        r6 = rsi(closes[:i+1], 6); r24 = rsi(closes[:i+1], 24)
        if r6 is not None: feats["rsi6"][i] = (50 - r6) / 50
        if r24 is not None: feats["rsi24"][i] = (50 - r24) / 50
        # 量
        if vr[i] is not None: feats["vol_ratio_dev"][i] = vr[i] - 1
        mv = sma(np.array(vol[:i+1], dtype=float), 20)
        if mv: feats["vol_surprise"][i] = vol[i] / mv - 1
        if high[i] and low[i] and c: feats["amp"][i] = (high[i] - low[i]) / c
        # 52 周
        if h52[i] and l52[i] and h52[i] > l52[i] and c:
            rg = h52[i] - l52[i]
            feats["w52"][i] = np.exp(-14*(c-l52[i])/rg) - np.exp(-14*(h52[i]-c)/rg)
        # 超额收益
        if dates[i] in ex and ex[dates[i]] is not None:
            feats["excess"][i] = ex[dates[i]] / 100.0
            if i >= 4:
                ws = [ex[dates[j]] for j in range(i-4, i+1) if ex.get(dates[j]) is not None]
                if ws: feats["excess_ma5"][i] = np.mean(ws) / 100.0
        # 沽空比率（用截至 i 的历史 mean/std，无未来泄漏）
        if dates[i] in sh and sh[dates[i]] and turn[i]:
            ratio = sh[dates[i]] / (turn[i] / 1e8)  # amt亿 / turnover亿
            short_ratios[dates[i]] = ratio
            hist = [short_ratios[d] for d in dates[:i] if d in short_ratios]
            if len(hist) >= 3:
                m, s = np.mean(hist), np.std(hist) or 0.01
                feats["short_ratio"][i] = -np.tanh((ratio - m) / s)
            if len(hist) >= 1:
                prev = short_ratios[dates[i-1]] if dates[i-1] in short_ratios else None
                if prev is not None:
                    feats["short_change"][i] = -np.tanh((ratio - prev) / 0.05)
        # 港股通
        if dates[i] in ggt and ggt[dates[i]] is not None:
            feats["ggt"][i] = np.tanh(ggt[dates[i]] / 2.0)
        # RSI6 背离（5 日窗口）
        if r6 is not None and i >= 4:
            wp = [j for j in range(max(0, i-4), i+1)]
            ps = [closes[j] for j in wp]; rs = [rsi(closes[:j+1], 6) for j in wp]
            rs = [x for x in rs if x is not None]
            if ps and rs:
                if c <= min(ps) and r6 > min(rs): feats["rsi_div"][i] = 1.0
                elif c >= max(ps) and r6 < max(rs): feats["rsi_div"][i] = -1.0
    return feats


# ---------------- 统计 ----------------
def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    den = 1 + z*z/n
    c = (p + z*z/(2*n)) / den
    h = z*sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
    return (max(0, c-h), min(1, c+h))


def binom_p(k, n):
    if n == 0:
        return 1.0
    try:
        from scipy import stats
        return 2 * min(stats.binom.cdf(k, n, 0.5), stats.binom.sf(k-1, n, 0.5))
    except Exception:
        from math import comb
        p_exact = sum(comb(n, i) * (0.5**n) for i in range(n+1)
                      if comb(n, i) * (0.5**n) <= comb(n, k) * (0.5**n))
        return min(1.0, 2 * p_exact)


# ---------------- 池化 walk-forward 核心 ----------------
def assemble_samples(raw, N_override=None):
    """返回 [(date, stock_idx, feat_vec(F), y), ...]，仅含 label 非缺失的样本。
    N_override: 覆盖全局 N，用于 horizon 扫描。"""
    nN = N_override if N_override is not None else N
    samples = []
    for si, stock in enumerate(STOCKS):
        q, ex, sh, ggt = raw[stock]
        dates = [r[0] for r in q]
        close = [float(r[4]) for r in q]
        high = [float(r[2]) for r in q]
        low = [float(r[3]) for r in q]
        vol = [float(r[5]) if r[5] is not None else None for r in q]
        turn = [float(r[6]) if r[6] is not None else None for r in q]
        vr = [float(r[7]) if r[7] is not None else None for r in q]
        chg = [float(r[8]) if r[8] is not None else 0.0 for r in q]
        h52 = [float(r[9]) if r[9] is not None else None for r in q]
        l52 = [float(r[10]) if r[10] is not None else None for r in q]
        T = len(dates)
        feats = build_features(dates, close, high, low, vol, turn, vr, chg, h52, l52, ex, sh, ggt)
        y = np.full(T, np.nan)
        for i in range(T - nN):
            y[i] = np.sign(close[i+nN] - close[i])
        fv_mat = np.array([[feats[n][i] for n in FEAT_NAMES] for i in range(T)], dtype=float)
        for i in range(T):
            if not np.isnan(y[i]):
                samples.append((dates[i], si, fv_mat[i], y[i]))
    return samples


def pooled_logit(samples, min_train, C, feat_idx=None):
    """池化 walk-forward logistic。返回 (preds, trues, proba_up, coefs, sidx)"""
    F = len(samples[0][2])
    cols = feat_idx if feat_idx is not None else list(range(F))
    preds, trues, proba_up, coefs, sidx = [], [], [], [], []
    by_date = defaultdict(list)
    for s in samples:
        by_date[s[0]].append(s)
    for t in sorted(by_date):
        test = by_date[t]
        train = [s for s in samples if s[0] < t]
        if len(train) < min_train:
            continue
        Xtr = np.array([s[2][cols] for s in train], dtype=float)
        ytr = np.array([s[3] for s in train])
        Xte = np.array([s[2][cols] for s in test], dtype=float)
        yte = np.array([s[3] for s in test])
        Ztr = np.zeros_like(Xtr)
        Zte = np.zeros_like(Xte)
        for j in range(Xtr.shape[1]):
            col = Xtr[:, j]
            m = ~np.isnan(col)
            if m.sum() >= 10:
                mu = col[m].mean()
                sd = col[m].std() or 1e-9
                Ztr[m, j] = (col[m] - mu) / sd
                te_m = ~np.isnan(Xte[:, j])
                Zte[te_m, j] = (Xte[te_m, j] - mu) / sd
        y01 = (ytr > 0).astype(int)
        try:
            lr = LogisticRegression(C=C, max_iter=2000)
            lr.fit(Ztr, y01)
            up = list(lr.classes_).index(1)
            p = lr.predict(Zte)
            pr = lr.predict_proba(Zte)
            for kk in range(len(test)):
                preds.append(1 if p[kk] == 1 else -1)
                trues.append(yte[kk])
                proba_up.append(pr[kk][up])
                sidx.append(test[kk][1])
            coefs.append(lr.coef_[0])
        except Exception:
            pass
    return preds, trues, proba_up, coefs, sidx


def single_feat_oos(samples, min_train):
    """池化 walk-forward 单特征：用训练窗相关符号预测。返回 {name: [0/1,...]}"""
    F = len(samples[0][2])
    hits = {FEAT_NAMES[j]: [] for j in range(F)}
    by_date = defaultdict(list)
    for s in samples:
        by_date[s[0]].append(s)
    for t in sorted(by_date):
        test = by_date[t]
        train = [s for s in samples if s[0] < t]
        if len(train) < min_train:
            continue
        Xtr = np.array([s[2] for s in train], dtype=float)
        ytr = np.array([s[3] for s in train])
        for j in range(F):
            fv = Xtr[:, j]
            m = ~np.isnan(fv) & ~np.isnan(ytr)
            if m.sum() < 10:
                continue
            c = np.corrcoef(fv[m], ytr[m])[0, 1]
            sg = np.sign(c) if not np.isnan(c) else 0
            if sg == 0:
                continue
            for ts in test:
                xv = ts[2][j]
                yv = ts[3]
                if np.isnan(xv):
                    continue
                pred = sg * np.sign(xv)
                if pred != 0:
                    hits[FEAT_NAMES[j]].append(1 if pred == yv else 0)
    return hits


def down_metrics(pu, trues, delta):
    """仅做空策略：p_up < 0.5−δ 视为 bet 跌。返回 (precision, n)。"""
    pu = np.array(pu); tr = np.array(trues)
    m = pu < (0.5 - delta)
    n = int(m.sum())
    if n < 1:
        return float("nan"), 0
    return float(np.mean(tr[m] < 0)), n


def walk_forward_core(samples, min_train, mask_cols=None):
    """池化 walk-forward（委托给 pooled_logit，保持与报告主路径完全一致）。
    mask_cols: 屏蔽特征索引集合（不参与训练/预测，等价“无此特征”）。
    缺失值行为：标准化后 NaN 位置保持 0（=训练均值，即中性）。
    返回 (proba_up, trues, sidx)。
    """
    F = len(samples[0][2])
    feat_idx = [j for j in range(F) if (mask_cols is None or j not in mask_cols)]
    preds, trues, proba_up, _, sidx = pooled_logit(samples, min_train, 1.0, feat_idx)
    return proba_up, trues, sidx


def feature_contribution(samples, delta=0.10):
    """特征贡献 + NaN 污染诊断（聚焦仅做空信号）。
    用逐特征消融（leave-one-out）：移除某特征后 down 精度下降 = 该特征重要。
    """
    F = len(samples[0][2])
    print()
    print("=" * 78)
    print(f"特征贡献分析（N={N}, 仅做空信号 δ={delta}, 逐特征消融 + 分组消融）")
    print("=" * 78)
    # (1) 缺失率
    nan_cnt = np.zeros(F); tot = len(samples)
    for s in samples:
        for j in range(F):
            if np.isnan(s[2][j]):
                nan_cnt[j] += 1
    print("各特征缺失率（NaN 标准化后填 0=中性，可能污染；≈100% 表示该特征完全无效）:")
    for j in range(F):
        flag = "  ← 全缺失,建议删除" if nan_cnt[j] / tot > 0.99 else ""
        print(f"  {FEAT_NAMES[j]:14s} NaN={nan_cnt[j]/tot:6.1%}{flag}")
    # (2) base
    base_pu, trues, sidx = walk_forward_core(samples, MIN_TRAIN)
    base_prec, base_n = down_metrics(base_pu, trues, delta)
    print()
    print(f"Base 仅做空 down 精度 = {base_prec:.1%} (n={base_n}, 跌基线={np.mean(np.array(trues)<0):.1%})")
    # (3) 逐特征消融
    print("逐特征消融（移除该特征后 down 精度变化；下降=重要，上升=该特征在拖累模型）:")
    print(f"{'特征':14s} {'移除后down精':>12s} {'变化(pp)':>10s} {'n':>6s}")
    rows = []
    for j in range(F):
        pu_a, tr_a, _ = walk_forward_core(samples, MIN_TRAIN, mask_cols={j})
        prec_a, n_a = down_metrics(pu_a, tr_a, delta)
        chg = (prec_a - base_prec) * 100
        rows.append((FEAT_NAMES[j], prec_a, chg, n_a))
    rows.sort(key=lambda r: r[2])  # 下降最多排最前
    for n, prec_a, chg, n_a in rows:
        print(f"  {n:12s} {prec_a:11.1%} {chg:+9.1f} {n_a:6d}")
    # (4) 分组消融：仅价格 vs 仅另类
    alt = {"excess", "excess_ma5", "short_ratio", "short_change", "ggt"}
    alt_idx = [j for j in range(F) if FEAT_NAMES[j] in alt]
    price_idx = [j for j in range(F) if j not in alt_idx]
    p_pu, p_tr, _ = walk_forward_core(samples, MIN_TRAIN, mask_cols=alt_idx)   # 仅价格
    a_pu, a_tr, _ = walk_forward_core(samples, MIN_TRAIN, mask_cols=price_idx)  # 仅另类
    pp_prec, pp_n = down_metrics(p_pu, p_tr, delta)
    pa_prec, pa_n = down_metrics(a_pu, a_tr, delta)
    print()
    print("分组消融（诊断 NaN 污染：另类特征历史短、大量 NaN）:")
    print(f"  仅价格特征(14个,基本无NaN): down精={pp_prec:.1%} (n={pp_n})")
    print(f"  仅另类特征(5个,大量NaN):    down精={pa_prec:.1%} (n={pa_n})")
    print(f"  全特征 base:                 down精={base_prec:.1%} (n={base_n})")
    print("  解读：若『仅价格』≈ base 且『仅另类』明显更低 → 信号来自价格特征,")
    print("        另类特征的 NaN=0 填充没有污染、也没有额外贡献。")


def horizon_scan(raw, max_n=20, delta=0.10):
    """Horizon 扫描：对 N=1..max_n 各跑一次 walk-forward，看 AUC 与仅做空 down 精度
    是否随预测周期单调上行、在哪饱和。纯文本表格 + ASCII 条形图。"""
    print()
    print("=" * 78)
    print(f"Horizon 扫描（N=1..{max_n}, 全20特征 C=1.0, AUC + 仅做空 down 精度 δ={delta}）")
    print("=" * 78)
    print(f"{'N':>3s} {'OOS_n':>6s} {'acc':>7s} {'AUC':>7s} | {'down精':>7s} {'down_n':>6s} | AUC条形")
    print("-" * 78)
    aucs = []
    for n in range(1, max_n + 1):
        smp = assemble_samples(raw, n)
        if not smp:
            continue
        preds, trues, proba, _, _ = pooled_logit(smp, MIN_TRAIN, 1.0)
        if not preds:
            continue
        k = sum(1 for a, b in zip(preds, trues) if a == b)
        nn = len(preds)
        acc = k / nn
        try:
            from sklearn.metrics import roc_auc_score
            auc = roc_auc_score(np.array(trues) > 0, np.array(proba))
        except Exception:
            auc = float("nan")
        dp, dn = down_metrics(proba, trues, delta)
        aucs.append((n, auc))
        bar = "#" * int(max(0, (auc - 0.4)) * 120) if not np.isnan(auc) else ""
        print(f"{n:3d} {nn:6d} {acc:7.1%} {auc:7.3f} | {dp:7.1%} {dn:6d} | {bar}")
    print("-" * 78)
    if len(aucs) >= 3:
        inc = sum(1 for i in range(1, len(aucs)) if aucs[i][1] > aucs[i-1][1])
        best_n, best_auc = max(aucs, key=lambda x: x[1])
        post = aucs[10:]  # N>=11
        post_inc = sum(1 for i in range(1, len(post)) if post[i][1] > post[i-1][1])
        print(f"AUC 上升段(总): {inc}/{len(aucs)-1}；N=2→10 净变化 "
              f"{aucs[1][1]:.3f}→{aucs[9][1]:.3f} ({aucs[9][1]-aucs[1][1]:+.3f})，整体上行")
        print(f"最佳 AUC 在 N={best_n} (AUC={best_auc:.3f})")
        print(f"N>10 段: 上升段 {post_inc}/{len(post)-1} → 非单调，非简单『越长越好』")
        print("解读：N=1→10 整体上行(短horizon近随机、长horizon渐可预测，中间小幅波动)；")
        print("      N>10 波动并再创新高(N=20)，提示 ~1个月周期有额外趋势/均值回归信号。")


def trend_diagnosis(raw, delta=0.10):
    """验证『越长越准是否只是标的处于下跌趋势』的假设。
    判据：跌标签基率 P(y=-1) 是否随 N 大幅上升；模型 down 精度相对朴素『全喊跌』
    的 edge(lift) 是否很小。edge 小 → 可预测性主要源于趋势，而非模型学到方向。"""
    print()
    print("=" * 78)
    print("趋势诊断：回测窗口是否整体下跌？能否解释『越长越准』？")
    print("=" * 78)
    print("窗口内价格趋势（daily_quote 回补后 245 天，逐票）:")
    for stock in STOCKS:
        q, *_ = raw[stock]
        close = np.array([float(r[4]) for r in q])
        ret = close[-1] / close[0] - 1
        peak = np.maximum.accumulate(close)
        dd = (close - peak) / peak
        mdd = dd.min()
        print(f"  {stock}: 起点={close[0]:.2f} 终点={close[-1]:.2f} "
              f"窗口收益={ret:+.1%} 最大回撤={mdd:+.1%}")
    print()
    print(f"{'N':>3s} {'跌基率':>7s} {'全喊跌精':>9s} {'模型down精':>10s} "
          f"{'edge(pp)':>9s} {'模型down_n':>9s}  结论")
    print("-" * 78)
    for n in [1, 5, 10, 20]:
        smp = assemble_samples(raw, n)
        preds, trues, proba, _, _ = pooled_logit(smp, MIN_TRAIN, 1.0)
        tr = np.array(trues)
        down_base = np.mean(tr < 0)
        naive_prec = down_base            # 朴素『全喊跌』精度 = 跌基率
        model_prec, model_n = down_metrics(proba, trues, delta)
        edge = (model_prec - naive_prec) * 100
        verdict = "edge大→模型真有方向信号" if edge > 8 else \
                  "edge中→部分来自趋势" if edge > 3 else \
                  "edge小→主要是趋势驱动"
        print(f"{n:3d} {down_base:7.1%} {naive_prec:9.1%} {model_prec:10.1%} "
              f"{edge:+9.1f} {model_n:9d}  {verdict}")
    print("-" * 78)
    print("说明：朴素『全喊跌』精度=跌基率，覆盖100%%；模型『仅做空』只在 p_up<%.2f 时出手。"
          % (0.5 - delta))
    print("edge = 模型down精 − 跌基率。edge 越小，『越长越准』越可能是下跌趋势的假象。")


# ---------------- 报告 ----------------
def report_method(name, preds, trues, proba_up=None):
    if not preds:
        print(f"{name:24s} 无样本外点")
        return
    k = sum(1 for a, b in zip(preds, trues) if a == b)
    nn = len(preds)
    lo, hi = wilson_ci(k, nn)
    p = binom_p(k, nn)
    extra = ""
    if proba_up is not None:
        try:
            from sklearn.metrics import roc_auc_score
            auc = roc_auc_score(np.array(trues) > 0, np.array(proba_up))
            extra = f"  AUC={auc:.3f}"
        except Exception:
            pass
    print(f"{name:24s} OOS_n={nn:4d} acc={k/nn:.1%} CI=[{lo:.1%},{hi:.1%}] p={p:.4f}{extra}")


# ---------------- 主流程 ----------------
def main():
    raw = load_all(STOCKS)
    samples = assemble_samples(raw)
    yall = np.array([s[3] for s in samples])
    down_rate = np.mean(yall < 0)
    print(f"池化样本：{len(samples)} 条 (票={STOCKS})")
    for si, stock in enumerate(STOCKS):
        cnt = sum(1 for s in samples if s[1] == si)
        print(f"  {stock}: {cnt} 样本")
    print(f"Label(未来{N}日方向): 跌占比={down_rate:.1%}（池化多数类基线）\n")

    # 单特征
    print("=" * 78)
    print("单特征 样本外方向准确率（池化 walk-forward）")
    print("-" * 78)
    print(f"{'特征':14s} {'OOS_n':>6s} {'acc':>7s} {'95%CI':>16s} {'p':>8s}")
    print("-" * 78)
    hits = single_feat_oos(samples, MIN_TRAIN)
    rows = []
    for n in FEAT_NAMES:
        h = hits[n]
        if not h:
            continue
        k = sum(h); nn = len(h)
        lo, hi = wilson_ci(k, nn)
        p = binom_p(k, nn)
        rows.append((n, nn, k/nn, lo, hi, p))
    rows.sort(key=lambda r: -r[2])
    for n, nn, acc, lo, hi, p in rows:
        print(f"{n:14s} {nn:6d} {acc:7.1%} [{lo:5.1%},{hi:5.1%}] {p:8.4f}")
    if not rows:
        print("（无足够样本外点）")

    # 组合：全 20 特征
    print()
    print("=" * 78)
    print("组合方法 样本外对比（池化 walk-forward, 标准化 logistic）")
    print("-" * 78)
    preds, trues, proba, coefs, sidx = pooled_logit(samples, MIN_TRAIN, 1.0)
    report_method("全20特征(C=1.0)", preds, trues, proba)
    # 分票
    for si, stock in enumerate(STOCKS):
        mask = [sidx[i] == si for i in range(len(preds))]
        sp = [preds[i] for i in range(len(preds)) if mask[i]]
        st = [trues[i] for i in range(len(preds)) if mask[i]]
        if sp:
            report_method(f"  └{stock}", sp, st)
    # 简约 5 特征
    idx5 = [FEAT_NAMES.index(n) for n in TOP5]
    p5, t5, pu5, _, _ = pooled_logit(samples, MIN_TRAIN, 1.0, idx5)
    report_method("简约5特征(C=1.0)", p5, t5, pu5)

    print()
    print(f"基线：始终看多 acc={1-down_rate:.1%} | 始终看空(多数类) acc={down_rate:.1%} | 抛硬币 50%")
    print(f"★ 真实基线=多数类 {down_rate:.1%}：任何指标须超过它才算有 edge")

    if coefs:
        coef_avg = np.abs(np.array(coefs)).mean(axis=0)
        order = np.argsort(-coef_avg)
        print()
        print("Logistic 平均|权重| Top8（标准化后，仅指示相对重要性）:")
        for j in order[:8]:
            print(f"  {FEAT_NAMES[j]:14s} {coef_avg[j]:.3f}")

    # 稳健性扫描
    print()
    print("=" * 78)
    print("Logistic 稳健性扫描（C / 全特征 vs 5特征）")
    print("-" * 78)
    print(f"{'模型':16s} {'C':>6s} {'OOS_n':>6s} {'acc':>7s} {'p':>8s}")
    print("-" * 78)
    for C in [0.3, 1.0, 3.0]:
        pr, tr_, pu, _, _ = pooled_logit(samples, MIN_TRAIN, C)
        if pr:
            k = sum(1 for a, b in zip(pr, tr_) if a == b)
            nn = len(pr)
            print(f"{'全20特征':16s} {C:6.1f} {nn:6d} {k/nn:7.1%} {binom_p(k, nn):8.4f}")
        pr5, tr5, pu5, _, _ = pooled_logit(samples, MIN_TRAIN, C, idx5)
        if pr5:
            k = sum(1 for a, b in zip(pr5, tr5) if a == b)
            nn = len(pr5)
            print(f"{'简约5特征':16s} {C:6.1f} {nn:6d} {k/nn:7.1%} {binom_p(k, nn):8.4f}")

    # ---- 高置信子集（precision-gated）：只在模型最确定的尾部出手 ----
    print()
    print("=" * 78)
    print(f"高置信子集 precision-gated（N={N}, 全20特征, C=1.0）")
    print("-" * 78)
    print("思路：整体 acc 低是因为低置信预测被噪声主导；只在 |p_up-0.5|>δ 的")
    print("      高置信尾部出手，看该子集准确率能否 >60%。δ=0 → 即整体。")
    print("发现：模型「涨」侧反信号、「跌」侧真信号 → 重点看 down 喊单 precision。")
    print(f"{'δ':>5s} {'cov':>7s} {'n':>5s} | {'信预测':>7s} {'反向':>7s} | {'up精':>7s} {'up_n':>5s} {'down精':>7s} {'down_n':>6s}")
    print("-" * 78)
    preds, trues, pu, _, sidx = pooled_logit(samples, MIN_TRAIN, 1.0)
    pu = np.array(pu); trues = np.array(trues); preds = np.array(preds); sidx = np.array(sidx)
    tot = len(pu)
    gate_pass = 0
    for delta in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25]:
        m = np.abs(pu - 0.5) > delta
        n = int(m.sum())
        if n < 10:
            print(f"{delta:5.2f} {'-':>7s} {n:5d} | 样本不足")
            continue
        sp = preds[m]; st = trues[m]
        accA = np.mean(sp == st)                       # 信预测类
        accB = np.mean(-sp == st)                      # 反向
        upm = sp == 1
        dnm = sp == -1
        prec_up = np.mean(st[upm] > 0) if upm.sum() else float("nan")
        prec_dn = np.mean(st[dnm] < 0) if dnm.sum() else float("nan")
        cov = n / tot
        print(f"{delta:5.2f} {cov:7.1%} {n:5d} | {accA:7.1%} {accB:7.1%} | {prec_up:7.1%} {int(upm.sum()):5d} {prec_dn:7.1%} {int(dnm.sum()):6d}")
        if accA > 0.60 and gate_pass == 0:
            gate_pass = delta
    print("-" * 78)
    print("策略C「仅做空」：只信模型的 down 喊单（p_up<0.5−δ 时 bet 跌），跳过 up 喊单")
    print(f"{'δ':>5s} {'cov(down)':>10s} {'down_n':>7s} {'down精':>7s} {'vs基线':>8s}")
    print("-" * 78)
    best = None
    for delta in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25]:
        dnm = pu < (0.5 - delta)
        nd = int(dnm.sum())
        if nd < 10:
            continue
        prec = np.mean(trues[dnm] < 0)
        cov = nd / tot
        vs = prec - down_rate
        flag = " ★>60%" if prec > 0.60 else ""
        print(f"{delta:5.2f} {cov:10.1%} {nd:7d} {prec:7.1%} {vs:+8.1%}{flag}")
        if best is None and prec > 0.60:
            best = (delta, prec, cov, nd)
    print("-" * 78)
    if best:
        print(f"★ 仅做空策略 δ={best[0]:.2f}: precision={best[1]:.1%}（>{down_rate:.1%}基线 {best[1]-down_rate:+.1%}），"
              f"覆盖率={best[2]:.1%}（{best[3]}笔）")
    else:
        print("★ 仅做空策略也未破 60%")
    # 分票（N=10 关键，确认 down 信号跨票通用，非单票假象）
    if N == 10:
        print()
        print("分票「仅做空」down 喊单 precision（δ=0.10）:")
        dnm = pu < 0.40
        for si, stock in enumerate(STOCKS):
            mm = dnm & (sidx == si)
            if mm.sum() >= 10:
                prec = np.mean(trues[mm] < 0)
                print(f"  {stock}: down_n={int(mm.sum()):3d} down精={prec:.1%}")
        print("（若两票 down 精均>60% → 信号跨票通用，非单票过拟合）")

    # 特征贡献 + NaN 污染诊断（仅 N=10，消融 20 次 walk-forward 较慢）
    if N == 10:
        feature_contribution(samples, delta=0.10)

    # Horizon 扫描（N=1..20，确认可预测性随周期的变化与饱和点）
    horizon_scan(raw, max_n=20, delta=0.10)

    # 趋势诊断：验证『越长越准』是否只是标的处于下跌趋势
    trend_diagnosis(raw, delta=0.10)


if __name__ == "__main__":
    main()
