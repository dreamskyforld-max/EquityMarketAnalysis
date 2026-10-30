"""分时切分研究 · 带权代价函数 + DP 全局最优（训练模型，当前最好）

Cost(S) = w · φ(S)
  φ = [ΣSSE, 段数 K, 方向翻转数, Σ过短hinge, Σ幅度不足hinge, Σ低效hinge]
  w = [w_sse, w_k, w_flip, w_short, w_amp, w_er]（由人工标注集训练）

为什么是它而不是规则堆（见 docs/02）：
  1. DP 求全局最优 → 一段的去留取决于前后段怎么切，**带上下文**；
     规则堆的局部 if-else 在标注集上互相矛盾（并 A 天就破 B 天）。
  2. 权重是**训练**出来的，不是拍的。
  3. 「V 型反转要保留」不需要特殊规则：把尖锐 V 并成一段会让线性拟合残差爆炸，
     SSE 项天然阻止合并（10-27 ⑦⑧ 实测验证）。

候选边界（cuts）：
  生产推断时 = v3 超切边界（去掉午休切点）；
  训练评估时 = v3 边界 ∪ 标注中出现过的边界（教师强制）。
  ⚠ 实测：候选集放大到「全部局部极值」会掉到 F1 0.35——段数对了但边界位置
  与人工标注不同源，故只能定向增补。
"""
from __future__ import annotations

import numpy as np
from scipy.signal import argrelextrema

from .common import Lin, log_pct, sigma_noise, amp_min_of, er_of, chg_of, anchor_segs

MS = 3   # DP 硬最短段（防退化拟合，m=1 时 den=0）


class DayDP:
    """单日分钟序列的切分问题（候选边界 + 特征）"""

    def __init__(self, p, tt, cands=None):
        self.p = np.asarray(p, float)
        self.tt = list(tt)
        self.n = len(self.p)
        x = log_pct(self.p)
        self.sn2 = sigma_noise(self.p) ** 2
        t = np.arange(self.n, dtype=float)
        self.PT = np.concatenate([[0.0], np.cumsum(t)])
        self.PT2 = np.concatenate([[0.0], np.cumsum(t * t)])
        self.PX = np.concatenate([[0.0], np.cumsum(x)])
        self.PTX = np.concatenate([[0.0], np.cumsum(t * x)])
        self.PX2 = np.concatenate([[0.0], np.cumsum(x * x)])
        self.CUM = np.concatenate([[0.0], np.cumsum(np.abs(np.diff(self.p)))])
        if cands is None:
            c = {0, self.n - 1}
            c |= set(argrelextrema(self.p, np.greater_equal, order=2)[0].tolist())
            c |= set(argrelextrema(self.p, np.less_equal, order=2)[0].tolist())
        else:
            c = {0, self.n - 1} | {int(v) for v in cands}
        self.cand = np.array(sorted(v for v in c if 0 <= v < self.n), dtype=int)
        self.is_cand = np.zeros(self.n, dtype=bool)
        self.is_cand[self.cand] = True

    def seg_arrays(self, e):
        """段 [s, e]（s = 0..e-MS+1）的 (s, m, SSE, 涨跌%, ER, 方向)"""
        s = np.arange(0, max(e - MS + 2, 1))
        m = e - s + 1
        St = self.PT[e + 1] - self.PT[s]
        St2 = self.PT2[e + 1] - self.PT2[s]
        Sx = self.PX[e + 1] - self.PX[s]
        Stx = self.PTX[e + 1] - self.PTX[s]
        Sx2 = self.PX2[e + 1] - self.PX2[s]
        den = np.where(m * St2 - St * St <= 1e-9, np.nan, m * St2 - St * St)
        b = (m * Stx - St * Sx) / den
        a = (Sx - b * St) / m
        sse = np.maximum(Sx2 - a * Sx - b * Stx, 0.0)
        chg = (self.p[e] / self.p[s] - 1) * 100
        path = self.CUM[e] - self.CUM[s]
        er = np.abs(self.p[e] - self.p[s]) / np.where(path > 0, path, 1.0)
        return s, m, sse, chg, er, (b > 0).astype(int)


def dp_segment(day: DayDP, w, thr=(10.0, 0.08, 0.45)):
    """给定权重求全局最优切分。thr = (L_min, amp_ratio, E_min)"""
    L_min, amp_ratio, e_min = thr
    p, n = day.p, day.n
    amp_min = amp_min_of(p, amp_ratio)
    INF = float("inf")
    f = np.full((2, n), INF)
    bk = np.full((2, n, 2), -1, dtype=int)
    for e in range(n):
        s, m, sse, chg, er, _ = day.seg_arrays(e)
        valid = day.is_cand[s] & (m >= MS)
        base = (w[0] * sse + w[1]
                + w[3] * np.maximum(0.0, L_min - m)
                + w[4] * np.maximum(0.0, amp_min - np.abs(chg))
                + w[5] * np.maximum(0.0, e_min - er))
        for d in (0, 1):
            prev = np.full(len(s), INF)
            same = np.full(len(s), INF)
            pos = s > 0
            prev[pos] = f[1 - d][s[pos] - 1] + w[2]
            same[pos] = f[d][s[pos] - 1]
            cand = base + np.where(s == 0, 0.0, np.minimum(prev, same))
            cand = np.where(valid, cand, INF)
            j = int(np.argmin(cand))
            if cand[j] < f[d][e]:
                f[d][e] = cand[j]
                pd = d
                if s[j] > 0:
                    pd = (1 - d) if prev[j] <= same[j] else d
                bk[d][e] = (int(s[j]), pd)
    d = int(np.argmin(f[:, n - 1]))
    segs, e = [], n - 1
    while e >= 0:
        s, pd = bk[d][e]
        segs.append((s, e))
        if s == 0:
            break
        e, d = s - 1, pd
    return segs[::-1]


def phi(day: DayDP, segs, thr=(10.0, 0.08, 0.45)):
    """切分的特征向量（训练用）"""
    L_min, amp_ratio, e_min = thr
    p = day.p
    amp_min = amp_min_of(p, amp_ratio)
    L = Lin(log_pct(p))
    sse = k = flip = hs = ha = he = 0.0
    prev_d = None
    for s, e in segs:
        if e <= s:
            continue
        b, _, v, _ = L.fit(s, e)
        sse += v
        k += 1
        d = 0 if b > 0 else 1
        if prev_d is not None and d != prev_d:
            flip += 1
        prev_d = d
        m = e - s + 1
        hs += max(0.0, L_min - m)
        ha += max(0.0, amp_min - abs(chg_of(p, s, e)))
        he += max(0.0, e_min - er_of(p, s, e))
    return np.array([sse, k, flip, hs, ha, he])


def scale_of(day: DayDP):
    """特征按日归一：SSE/σ² ~ O(n)，hinge/n ~ O(阈值)。
    ⚠ 早期版本把 SSE 除以 σ²·n，导致拟合项被压到 ~0.01 → 训练发散。"""
    return np.array([day.sn2, 1.0, 1.0, day.n, day.n, day.n])
