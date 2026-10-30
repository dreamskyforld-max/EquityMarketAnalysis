"""分时切分研究 · 权重训练 + 留一股票交叉验证

用法（项目根目录）：
    python3 -m research.force_segmentation.seg.train            # 全量拟合
    python3 -m research.force_segmentation.seg.train --loo      # 含留一股 CV（较慢）
"""
from __future__ import annotations

import argparse

import numpy as np

from .common import anchor_segs
from .dp import DayDP, dp_segment, phi, scale_of
from .labels import LABELS, VALID, items
from .panels import load_panel
from .rules import segment_v3

TOL = 3               # 边界命中容差（分钟）
THR = (10.0, 0.08, 0.45)   # (L_min, amp_ratio, E_min)
RANGES = (("w_k", np.log(2), np.log(200)), ("w_flip", 0, 40), ("w_short", 0, 20),
          ("w_amp", 0, 60), ("w_er", 0, 60))


def build(verbose=True):
    """构建全部标注样本的 (DayDP, 目标索引)；候选 = v3 边界（去午休）∪ 标注边界"""
    DD, TGT, TT = {}, {}, {}
    for code, d, segs in items():
        tt, p = load_panel(code, d)
        if len(tt) < 60:
            continue
        key = (code, d)
        TT[key] = tt
        TGT[key] = [(tt.index(a), tt.index(z)) for a, z in segs]
        v3 = [(s, e) for s, e, _ in segment_v3(p, tt)]
        n = len(p)
        cut = [0] + [e + 1 for _, e in v3[:-1]]
        lunch = tt.index("13:00") if "13:00" in tt else -1
        cut = [c for c in cut if c != lunch]
        extra = [s for s, _ in TGT[key]] + [e + 1 for _, e in TGT[key]]
        DD[key] = DayDP(p, tt, cands=[c for c in cut + extra if 0 <= c < n])
    if verbose:
        print(f"标注样本 {len(DD)} 个（股票×日），窗口合计 {sum(len(v) for v in TGT.values())}")
    return DD, TGT, TT


def predict(DD, TGT, k, w):
    sc = np.array([DD[k].sn2, 1.0, 1.0, DD[k].n, DD[k].n, DD[k].n])
    return anchor_segs(DD[k].p, dp_segment(DD[k], w / sc, THR))


def f1(TGT, k, segs):
    got = {(s, e) for s, e in segs}
    hit = sum(1 for s, e in TGT[k]
              if any(abs(gs - s) <= TOL and abs(ge - e) <= TOL for gs, ge in got))
    pr = hit / max(len(got), 1)
    rc = hit / len(TGT[k])
    return 2 * pr * rc / (pr + rc) if pr + rc else 0.0


def mean_f1(DD, TGT, keys, w):
    return float(np.mean([f1(TGT, k, predict(DD, TGT, k, w)) for k in keys]))


def fit(DD, TGT, keys, n_rand=150, seed=5, sweeps=6):
    """随机搜索 + 坐标下降"""
    rng = np.random.default_rng(seed)
    best, bw = -1, None
    for _ in range(n_rand):
        w = np.array([1.0,
                      float(np.exp(rng.uniform(np.log(2), np.log(200)))),
                      float(rng.uniform(0, 40)), float(rng.uniform(0, 20)),
                      float(rng.uniform(0, 60)), float(rng.uniform(0, 60))])
        m = mean_f1(DD, TGT, keys, w)
        if m > best:
            best, bw = m, w.copy()
    for _ in range(sweeps):
        imp = False
        for j in range(1, 6):
            for st in (0.6, 0.8, 1.25, 1.6):
                w = bw.copy()
                w[j] = max(0.0, w[j] * st)
                m = mean_f1(DD, TGT, keys, w)
                if m > best + 1e-6:
                    best, bw, imp = m, w.copy(), True
        if not imp:
            break
    return best, bw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loo", action="store_true", help="跑留一股票交叉验证（较慢）")
    ap.add_argument("--rand", type=int, default=150)
    args = ap.parse_args()

    DD, TGT, _ = build()
    keys = list(DD)

    # 基线：规则堆 v3
    base = {}
    for k in keys:
        code, d = k
        tt, p = load_panel(code, d)
        base[k] = f1(TGT, k, [(s, e) for s, e, _ in segment_v3(p, tt)])
    print(f"\n基线 规则堆 v3：全体 {np.mean(list(base.values())):.3f}")
    for c in LABELS:
        ks = [k for k in keys if k[0] == c]
        if ks:
            print(f"  {c}: {np.mean([base[k] for k in ks]):.3f}")

    best, w = fit(DD, TGT, keys, n_rand=args.rand)
    print(f"\n训练模型（{len(keys)} 样本）：F1 = {best:.3f}")
    print("权重 [SSE, K, 翻转, 过短, 幅度, 低效] =", np.round(w, 3))
    for c in LABELS:
        ks = [k for k in keys if k[0] == c]
        tr = [k for k in ks if k not in VALID]
        va = [k for k in ks if k in VALID]
        line = f"  {c}: 全体 {np.mean([f1(TGT, k, predict(DD, TGT, k, w)) for k in ks]):.3f}"
        if tr:
            line += f"｜训练日 {np.mean([f1(TGT, k, predict(DD, TGT, k, w)) for k in tr]):.3f}"
        if va:
            line += f"｜验证日 {np.mean([f1(TGT, k, predict(DD, TGT, k, w)) for k in va]):.3f}"
        print(line)

    if args.loo:
        print("\n留一股票交叉验证：")
        print("股票        全局权重  留一权重    差")
        res = {}
        for c in LABELS:
            others = [k for k in keys if k[0] != c]
            hold = [k for k in keys if k[0] == c]
            if not others or not hold:
                continue
            _, w_loo = fit(DD, TGT, others, n_rand=60, seed=17)
            g = np.mean([f1(TGT, k, predict(DD, TGT, k, w)) for k in hold])
            lo = np.mean([f1(TGT, k, predict(DD, TGT, k, w_loo)) for k in hold])
            res[c] = (g, lo)
            print(f"{c:<12} {g:.3f}    {lo:.3f}   {lo - g:+.3f}")
        print(f"\n留一均值：全局 {np.mean([v[0] for v in res.values()]):.3f}  "
              f"留一 {np.mean([v[1] for v in res.values()]):.3f}")


if __name__ == "__main__":
    main()
