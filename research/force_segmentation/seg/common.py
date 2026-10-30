"""分时切分研究 · 公共工具（纯函数，无 IO、无副作用）

研究目录 `research/force_segmentation/` 专用；**不参与生产路径**。
生产口径仍以 `force_dynamics.py` + `trend_segmenter.py` 为准。

提供：
  Lin            闭区间线性拟合（前缀和 O(1)）
  sigma_noise    分钟噪声 σ（‰/min，对数价格二阶差分）
  amp_min_of     效应量门槛（相对当日振幅）
  er_of          段内路径效率 ER
  dir_label      方向标签（含效应量护栏）
  flag_of        段标记（低效 / 波动）
  sessions       按时钟缺口切交易时段（午休）
  lunch_jump     午休跳变（元 / σ 倍数）
  anchor_segs    边界锚回原始价格极值
"""
from __future__ import annotations

import numpy as np

PRE = lambda v: np.concatenate([[0.0], np.cumsum(v)])

AMP_RATIO = 0.08      # 效应量 = 当日振幅 × 该比例
AMP_FLOOR = 0.10      # 效应量绝对下限（%）
ER_Z = 1.3            # 低效判据：方向性位移 ÷ 随机游走期望 < 该值 → 低效
SIG_Z = 2.0           # 方向显著性：|slope| ÷ SE < 该值 → 横盘


class Lin:
    """[s, e] 闭区间的线性拟合，前缀和 O(1)。

    fit(s, e) → (slope ‰/min, intercept, SSE, den)
    """

    def __init__(self, x):
        self.x = np.asarray(x, float)
        self.n = len(self.x)
        t = np.arange(self.n, dtype=float)
        self.PT, self.PT2 = PRE(t), PRE(t * t)
        self.PX, self.PTX, self.PX2 = PRE(self.x), PRE(t * self.x), PRE(self.x * self.x)

    def fit(self, s, e):
        m = e - s + 1
        St = self.PT[e + 1] - self.PT[s]
        St2 = self.PT2[e + 1] - self.PT2[s]
        Sx = self.PX[e + 1] - self.PX[s]
        Stx = self.PTX[e + 1] - self.PTX[s]
        Sx2 = self.PX2[e + 1] - self.PX2[s]
        den = m * St2 - St * St
        if den <= 0:
            a = Sx / m
            return 0.0, a, max(Sx2 - a * Sx, 0.0), 0.0
        b = (m * Stx - St * Sx) / den
        a = (Sx - b * St) / m
        return b, a, max(Sx2 - a * Sx - b * Stx, 0.0), den

    def se_slope(self, s, e, sigma2):
        """斜率标准误（用于方向显著性 z）"""
        _, _, _, den = self.fit(s, e)
        m = e - s + 1
        return float(np.sqrt(sigma2 * m / den)) if den > 0 else float("inf")


def log_pct(p):
    """价格 → 相对开盘的对数千分比（尺度归一，跨票可比）"""
    p = np.asarray(p, float)
    return (np.log(p) - np.log(p[0])) * 1000.0


def sigma_noise(p):
    """分钟噪声 σ（‰/min）：对数价格二阶差分，去趋势"""
    x = log_pct(p)
    d2 = x[2:] - 2 * x[1:-1] + x[:-2]
    return float(np.sqrt(np.var(d2) / 6.0))


def amp_min_of(p, ratio=AMP_RATIO, floor=AMP_FLOOR):
    """效应量门槛（%）：幅度低于此值不配单独成窗"""
    p = np.asarray(p, float)
    return max(floor, (p.max() - p.min()) / p.min() * 100 * ratio)


def er_of(p, s, e):
    """段内路径效率 = |净位移| ÷ 路径长度（1 = 一路不回头）"""
    seg = np.asarray(p, float)[s:e + 1]
    path = float(np.sum(np.abs(np.diff(seg))))
    return abs(seg[-1] - seg[0]) / path if path > 0 else 1.0


def chg_of(p, s, e):
    p = np.asarray(p, float)
    return (p[e] / p[s] - 1) * 100


def slope_dir(p, s, e, sigma, z=SIG_Z):
    """仅按拟合斜率显著性的方向（不含效应量护栏）

    ⚠ 极值锚定必须用这个版本：带效应量护栏的版本会把小幅段也判成横盘，
    锚点会偏几分钟，连带后续合并结果改变（对齐全原型时的实测坑）。
    """
    L = Lin(log_pct(p))
    b = L.fit(s, e)[0]
    se = L.se_slope(s, e, sigma ** 2)
    if abs(b) <= z * se:
        return "横盘"
    return "上涨" if b > 0 else "下跌"


def dir_label(p, s, e, sigma, ratio=AMP_RATIO, z=SIG_Z):
    """对外方向标签 = 斜率方向 + 效应量护栏：|涨跌| < 效应量门槛 → 横盘"""
    if abs(chg_of(p, s, e)) < amp_min_of(p, ratio):
        return "横盘"
    return slope_dir(p, s, e, sigma, z)


def flag_of(p, s, e, sigma, d, ratio=AMP_RATIO, erz=ER_Z):
    """段标记：**低效** / **波动** / None

    低效用「段内方向性最大位移」而不是首尾净位移——首尾净位移会把
    "推了但没守住"的真实推动误判成低效（10-29 13:00-13:30 实测踩到）。
    """
    p = np.asarray(p, float)
    seg = p[s:e + 1]
    amp = (seg.max() - seg.min()) / seg.min() * 100
    chg = abs(chg_of(p, s, e))
    if d == "横盘":
        return "波动" if amp >= amp_min_of(p, ratio) else None
    m = e - s + 1
    sd = float(np.std(np.diff(p), ddof=1)) if len(p) > 1 else 0.0
    if sd <= 0:
        return None
    exc = float(seg.max() - seg[0]) if d == "上涨" else float(seg[0] - seg.min())
    if exc / (0.8 * sd * np.sqrt(m)) < erz:
        return "低效"
    return "波动" if (amp >= amp_min_of(p, ratio) and amp >= 2 * chg) else None


def _mins(tt):
    return [int(t[:2]) * 60 + int(t[3:]) for t in tt]


def sessions(tt):
    """按时钟缺口切交易时段（午休 11:59→13:00 / A股 11:30→13:00）"""
    mm = _mins(tt)
    out, s = [], 0
    for i in range(1, len(mm)):
        if mm[i] - mm[i - 1] > 1:
            out.append((s, i - 1))
            s = i
    out.append((s, len(mm) - 1))
    return out


def lunch_jump(p, tt):
    """午休缺口跳变 → [(缺口索引, 跳变元, 跳变/分钟噪声 σ)]"""
    p = np.asarray(p, float)
    sd = float(np.std(np.diff(p), ddof=1))
    out = []
    for i in range(1, len(p)):
        if _mins(tt)[i] - _mins(tt)[i - 1] > 1:
            out.append((i, float(abs(p[i] - p[i - 1])),
                        float(abs(p[i] - p[i - 1]) / sd) if sd else 0.0))
    return out


def anchor_segs(p, segs, w=3, ms=5, end=None, dirs=None, sigma=None, amp_ratio=0.08):
    """边界锚回 ±w 分钟内的原始价格极值（后段涨锚谷 / 跌锚峰）

    end：本次锚定范围的末尾索引（**分场处理时必须传该场末尾**，
    否则会把上午最后一段一直拉到全天末尾，产生跨午休的重叠段）。
    dirs：每段方向；缺省时用「带显著性的方向标签」，**不能用首尾价格涨跌**——
    横盘段的锚定规则不同，用首尾会让锚点偏几分钟，连带后续合并结果改变。
    """
    p = np.asarray(p, float)
    end = len(p) - 1 if end is None else min(end, len(p) - 1)
    if len(segs) < 2:
        return list(segs)
    if dirs is None:
        if sigma is None:
            sigma = sigma_noise(p)
        dirs = [dir_label(p, s, e, sigma, amp_ratio) for s, e in segs]
    bounds = []
    for i in range(1, len(segs)):
        s_prev, e_prev = segs[i - 1]
        s_cur, e_cur = segs[i]
        b = s_cur
        lo = max(s_prev + ms, b - w)
        hi = min(e_cur - ms + 1, b + w)
        if lo <= hi:
            win = p[lo:hi + 1]
            if dirs[i] == "上涨":
                b = lo + int(np.argmin(win))
            elif dirs[i] == "下跌":
                b = lo + int(np.argmax(win))
            elif dirs[i - 1] == "上涨":
                b = lo + int(np.argmax(win))
            elif dirs[i - 1] == "下跌":
                b = lo + int(np.argmin(win))
        bounds.append(max(bounds[-1] + ms if bounds else 0, min(b, e_cur - ms + 1)))
    out, start = [], segs[0][0]
    for b in bounds:
        b = min(b, end)
        if b > start:
            out.append((start, b - 1))
            start = b
    if start <= end:
        out.append((start, end))
    return [(s, e) for s, e in out if e > s]
