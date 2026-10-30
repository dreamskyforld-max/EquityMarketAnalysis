"""分时切分研究 · 规则堆（v1 / v3）

v1 = 分段线性拟合 + 最优划分（MDL 选段数）+ 自底向上合并
v3 = v1 + 三项修正
      A 段内结构切分：段内振幅 ≥ 效应量 且 ≥ 2×|首尾涨跌| → 在段内极值处切开
      B 标记：低效（方向性位移未超过随机游走期望）/ 波动（段内结构未表达）
      C 极值锚定：边界从「斜率变化点」锚回 ±3 分钟窗内原始价格极值
      + 合并规则 R1 相邻横盘 / R2 短横盘并入 / R3 相邻低效（限原始窗口，防级联）

⚠ 实测结论（见 docs/02）：规则堆在人工标注集上只能到 F1 0.80，且
  「低效链式合并」在不同日期的标注上互相矛盾 → 已由 dp.py 的训练模型取代。
  保留本模块仅作对照与研究留档。
"""
from __future__ import annotations

import numpy as np

from .common import (Lin, log_pct, sigma_noise, amp_min_of, er_of, chg_of,
                     dir_label, slope_dir, flag_of, sessions, anchor_segs, lunch_jump)

BETAS = (400, 200, 100, 60, 40, 25, 15, 10, 6, 4, 2.5, 1.5, 1.0, 0.6)
MS = 5              # 最短段（分钟）
FLAT_MIN_MIN = 10   # 横盘窗口单独成窗的最短时间
MIN_PAUSE = 8       # 停顿（横盘）段保留的最短时间
LUNCH_Z = 2.5       # 午休跳变 > 该倍数 σ → 强制切开


def _partition(L: Lin, beta, ms):
    """最优划分 DP：min ΣSSE + β·K"""
    INF = float("inf")
    f = np.full(L.n, INF)
    prev = np.full(L.n, -1)
    for e in range(L.n):
        best, bs = INF, -1
        for s in range(0, e - ms + 2):
            v = (f[s - 1] if s > 0 else 0.0) + L.fit(s, e)[2] + (beta if s > 0 else 0.0)
            if v < best:
                best, bs = v, s
        f[e], prev[e] = best, bs
    segs, e = [], L.n - 1
    while e >= 0:
        segs.append((prev[e], e))
        e = prev[e] - 1
    return segs[::-1]


def segment_v1(p, tt, ms=MS, amp_ratio=0.08, erz=1.3):
    """v1：分段线性 + MDL + 自底向上合并。返回 [(s, e)]"""
    p = np.asarray(p, float)
    x = log_pct(p)
    sn = sigma_noise(p)
    amp_min = amp_min_of(p, amp_ratio)
    day = Lin(x)
    raw = []
    for a, z in sessions(tt):
        m = z - a + 1
        L = Lin(x[a:z + 1])
        best = None
        for beta in BETAS:
            segs = _partition(L, beta, ms)
            sse = sum(L.fit(s, e)[2] for s, e in segs)
            score = sse / (sn ** 2) + 3 * len(segs) * np.log(m)   # MDL（σ 已知）
            if best is None or score < best[0]:
                best = (score, segs)
        raw += [(s + a, e + a) for s, e in best[1]]

    # 自底向上合并（禁止跨交易时段：上午末段与下午首段在列表里相邻，
    # 不拦住会并成跨午休段 → 下游按段分组时整段丢弃，出现覆盖空洞）
    gap_ends = {z for _, z in sessions(tt)[:-1]}

    def meta(s, e):
        # ⚠ 合并判据里的方向必须用「纯斜率方向」(slope_dir)，不能带效应量护栏：
        # 带护栏会把小幅度段判成横盘 → 走"停顿"分支（时长 ≥8 分钟就不并）
        # → 实测 10-28 多出 4 段，整体基线掉到 0.634。
        d = slope_dir(p, s, e, sn)
        return dict(dir=d, m=e - s + 1, z=abs(day.fit(s, e)[0] / day.se_slope(s, e, sn ** 2)),
                    chg=chg_of(p, s, e))

    cur = list(raw)
    while True:
        met = [meta(s, e) for s, e in cur]
        hit = None
        for i in range(len(cur) - 1):
            if cur[i][1] in gap_ends:
                continue
            a, b = met[i], met[i + 1]
            if a["dir"] == b["dir"] and a["dir"] != "横盘":
                if abs(a["z"] - b["z"]) >= 0 or min(abs(a["chg"]), abs(b["chg"])) < amp_min:
                    dz = abs(day.fit(cur[i][0], cur[i][1])[0]
                             - day.fit(cur[i + 1][0], cur[i + 1][1])[0])
                    se = np.hypot(day.se_slope(*cur[i], sn ** 2),
                                  day.se_slope(*cur[i + 1], sn ** 2))
                    if dz <= 2 * se or min(abs(a["chg"]), abs(b["chg"])) < amp_min:
                        hit = i
                        break
            elif "横盘" in (a["dir"], b["dir"]):
                pause = a["m"] if a["dir"] == "横盘" else b["m"]
                if pause < MIN_PAUSE:
                    hit = i
                    break
        if hit is None:
            break
        cur[hit:hit + 2] = [(cur[hit][0], cur[hit + 1][1])]
    return cur


def _split_internal(p, s, e, amp_min, depth=2, ms_split=4):
    """修正 A：段内 V / ∧ 结构切分（切点归右段）"""
    if e - s + 1 < 2 * ms_split or depth <= 0:
        return [(s, e)]
    chg = chg_of(p, s, e)
    amp = (p[s:e + 1].max() - p[s:e + 1].min()) / p[s:e + 1].min() * 100
    if amp < amp_min or amp < 2 * abs(chg):
        return [(s, e)]
    k = (int(np.argmin(p[s:e + 1])) + s if p[s] <= p[e]
         else int(np.argmax(p[s:e + 1])) + s)
    if k - s + 1 < ms_split or e - k + 1 < ms_split:
        return [(s, e)]
    return (_split_internal(p, s, k - 1, amp_min, depth - 1, ms_split)
            + _split_internal(p, k, e, amp_min, depth - 1, ms_split))


def segment_v3(p, tt, flat_min=FLAT_MIN_MIN, erz=1.3, amp_ratio=0.08,
               lunch_z=LUNCH_Z, cross_lunch=False):
    """v3：v1 + A/B/C + 合并规则 R1/R2/R3。返回 [(s, e, flag)]

    cross_lunch=True 时，跳变 ≤ lunch_z×σ 的午休缺口不再强制切开，
    允许窗口跨午休（2026-10-29 用户确认「午休不算边界」）。
    """
    p = np.asarray(p, float)
    sn = sigma_noise(p)
    amp_min = amp_min_of(p, amp_ratio)
    base = segment_v1(p, tt, amp_ratio=amp_ratio, erz=erz)

    def meta(s, e):
        d = dir_label(p, s, e, sn, amp_ratio)
        L = Lin(log_pct(p))
        z = abs(L.fit(s, e)[0] / L.se_slope(s, e, sn ** 2))
        return dict(dir=d, m=e - s + 1, z=z,
                    flag=flag_of(p, s, e, sn, d, amp_ratio, erz))

    def blocked_pair(a, b):
        """a、b 相邻两段之间是否禁止合并（跨不连续的午休缺口）"""
        boundary = a[1] + 1                       # 后一段的起点索引
        for idx, _, mult in lunch_jump_list:
            if boundary == idx:                   # 该处是午休缺口
                if not cross_lunch or mult > lunch_z:
                    return True
        return False

    lunch_jump_list = lunch_jump(p, tt)

    # groups: [(本场段列表, 本场末尾索引)] —— 末尾必须显式带上，
    # 否则 anchor_segs 会把上午最后一段锚到全天末尾（实测产生 11:18-15:59 这类重叠段）
    groups = [(list(base), len(p) - 1)] if cross_lunch else []
    if not cross_lunch:
        for a, z in sessions(tt):
            grp = [(max(s, a), min(e, z)) for s, e in base if not (e < a or s > z)]
            if grp:
                groups.append((grp, z))

    out = []
    for grp, g_end in groups:
        segs = []
        for s, e in grp:
            segs += _split_internal(p, s, e, amp_min)
        segs = anchor_segs(p, segs, end=g_end,
                          dirs=[slope_dir(p, s, e, sn) for s, e in segs])
        orig = [True] * len(segs)
        while True:
            met = [meta(s, e) for s, e in segs]
            hit = None
            # R2 短横盘并入相邻
            for i, mm in enumerate(met):
                if mm["dir"] == "横盘" and mm["m"] < flat_min:
                    tgt = _target(i, met, len(segs), blocked_pair, segs)
                    if tgt is not None:
                        hit = (min(i, tgt), max(i, tgt))
                        break
            # R3 相邻低效（限原始窗口，防级联）
            if hit is None:
                for i in range(len(segs) - 1):
                    if (met[i]["flag"] == "低效" and met[i + 1]["flag"] == "低效"
                            and orig[i] and orig[i + 1]
                            and not blocked_pair(segs[i], segs[i + 1])):
                        hit = (i, i + 1)
                        break
            # R1 相邻横盘
            if hit is None:
                for i in range(len(segs) - 1):
                    if (met[i]["dir"] == "横盘" and met[i + 1]["dir"] == "横盘"
                            and not blocked_pair(segs[i], segs[i + 1])):
                        hit = (i, i + 1)
                        break
            if hit is None:
                break
            a, b = hit
            segs[a:b + 1] = [(segs[a][0], segs[b][1])]
            orig[a:b + 1] = [False]
        for s, e in segs:
            d = dir_label(p, s, e, sn, amp_ratio)
            out.append((s, e, flag_of(p, s, e, sn, d, amp_ratio, erz)))
    return out


def _target(i, met, n, blocked, segs):
    cand = [j for j in (i - 1, i + 1)
            if 0 <= j < n and not blocked(segs[min(i, j)], segs[max(i, j)])]
    if not cand:
        return None
    same = [j for j in cand if met[j]["dir"] == met[i]["dir"]]
    return same[0] if same else min(cand, key=lambda j: met[j]["z"])
