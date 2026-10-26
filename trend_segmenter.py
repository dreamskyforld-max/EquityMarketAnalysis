"""单日分钟级「股价趋势分段器」（纯函数模块，零 IO）

用途
----
把一只票**一天**的分钟级价格序列，切成「上涨 / 下跌 / 横盘」的大段区间，
作为 `force_dynamics.rounds_by_trend` 的**回合窗口**（替代原「资金攻防相位」切分）。

口径来源
--------
算法来自研究脚本 `WorkBuddy/2026-06-19-09-47-07/two_layer_segmentation.py`
（两层：L1 ZigZag 方向切分 + L2 PELT 节奏子切分），参数在其
`trend_segment_test.py` 的网格标定基础上**加粗**（原 0.15%/0.10%/3min 切得太碎，
本模块默认 0.30%/0.25%/5min，目标是一天 5~10 个大段）。

与本项目 `force_dynamics` 的分工
------------------------------
- 本模块只负责**切窗口**（哪几分钟归一段 + 段的方向/涨幅）；
- 段内的 λ / LQ / 冲击留存 / 胜负 等指标仍由 `force_dynamics._close` 计算
  （口径唯一真源不变，两边不重复定义）。
- L2 节奏（急跌/缓跌/反弹…）**只作为标签**输出，不参与窗口划分。

设计约束
--------
- 纯函数、无 IO、无全局状态、可单测；
- 段列表**连续覆盖**整段交易时间（无空洞）：过短的段并入相邻段，避免前端底纹带断口；
- 输入价格允许含 NaN（缺失分钟）——调用方应先用前值填充或过滤。

段结构
------
`Segment(s, e, direction, chg_pct, minutes)`：
  s / e      起点 / 终点索引（**均含**，对应 rows[s:e+1]）
  direction  "上涨" / "下跌" / "横盘"
  chg_pct    段内累计涨跌幅（%）= (prices[e] / prices[s] - 1) * 100
  minutes    e - s + 1
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
from scipy.signal import argrelextrema

# —— L1 方向切分参数（加粗版；原研究脚本 5 / 0.15 / 0.10 / 3）——
SMOOTH_WIN = 5             # 中心对称平滑窗（分钟）
MIN_RETRACE_PCT = 0.30     # 拐点确认：平滑后反向折返 ≥ 该百分比
TREND_THRESHOLD_PCT = 0.25  # 方向判定：段内累计涨跌幅 |chg| < 该值 → 横盘
MIN_SEG_MIN = 5            # 单段最短分钟数（不足则并入相邻段，保持覆盖连续）

# —— L2 节奏子切分参数（仅出标签，不参与窗口）——
L2_MIN_SIZE = 3            # PELT 子段最短分钟数
L2_MAX_SUB = 5             # 目标子段数上限
L2_PENALTIES = (0.5, 1, 2, 3, 5, 8, 12, 20, 30, 50)  # penalty 网格：取首个落在 2~MAX_SUB 的

# —— 节奏标签阈值（每分涨跌幅 %，与研究方法论一致）——
RHYTHM_EPS = 0.003         # |斜率| ≤ 该值 → 停顿
RHYTHM_FAST = 0.03         # ≥ 该值 → 急涨/急跌
RHYTHM_WAVE = 0.02         # 横盘段内 ≥ 该值 → 波动


class Segment(NamedTuple):
    s: int
    e: int
    direction: str
    chg_pct: float
    minutes: int


# ══════════════════════════════════════════════════════════════
# L1：ZigZag 方向切分
# ══════════════════════════════════════════════════════════════

def smooth_center(prices, win: int = SMOOTH_WIN):
    """中心对称滑动平均（两端按实际可用窗口收缩，不引入滞后）。"""
    p = np.asarray(prices, dtype=float)
    n = len(p)
    if n == 0:
        return p
    half = max(1, win // 2)
    out = np.empty(n, dtype=float)
    for i in range(n):
        out[i] = np.mean(p[max(0, i - half):min(n, i + half + 1)])
    return out


def zigzag_turns(smoothed, min_retrace_pct: float = MIN_RETRACE_PCT,
                 min_gap: int = MIN_SEG_MIN):
    """ZigZag 拐点：反向运动 ≥ min_retrace_pct% 才确认转折。

    返回 [(idx, is_peak), ...]（按 idx 升序，含强制补的首尾点）。
    """
    s = np.asarray(smoothed, dtype=float)
    n = len(s)
    if n < 2:
        return [(0, False)] if n == 1 else []

    lmax = set(argrelextrema(s, np.greater_equal, order=2)[0].tolist())
    lmin = set(argrelextrema(s, np.less_equal, order=2)[0].tolist())
    cand = sorted(lmax | lmin)
    if len(cand) < 2:
        return [(0, False), (n - 1, True)]

    confirmed = [(cand[0], cand[0] in lmax, s[cand[0]])]
    for idx in cand[1:]:
        is_peak, price = idx in lmax, s[idx]
        _, last_is_peak, last_price = confirmed[-1]
        if last_is_peak == is_peak:
            # 同类极值只保留更极端的一个
            if (is_peak and price > last_price) or (not is_peak and price < last_price):
                confirmed[-1] = (idx, is_peak, price)
        else:
            if last_price > 0 and abs(price - last_price) / last_price * 100 >= min_retrace_pct:
                confirmed.append((idx, is_peak, price))

    pairs = [(c[0], c[1]) for c in confirmed]
    if pairs[0][0] != 0:
        pairs.insert(0, (0, not pairs[0][1]))
    if pairs[-1][0] != n - 1:
        pairs.append((n - 1, not pairs[-1][1]))

    # 间距过滤：太密的拐点合并（保留更极端的那个）
    out = [pairs[0]]
    for p in pairs[1:]:
        if p[0] - out[-1][0] >= min_gap:
            out.append(p)
        else:
            out[-1] = p if p[0] > out[-1][0] else out[-1]
    if len(out) < 2:
        out = [(0, False), (n - 1, True)]
    return out


def anchor_to_raw(prices, turn_pairs, window: int = 3, min_gap: int = MIN_SEG_MIN):
    """把平滑序列上的拐点锚回**原始价格**的真实极值（峰取最高、谷取最低）。"""
    p = np.asarray(prices, dtype=float)
    n = len(p)
    idxs = []
    for i, (t, is_peak) in enumerate(turn_pairs):
        if i == 0 or i == len(turn_pairs) - 1:
            idxs.append(t)
            continue
        lo, hi = max(0, t - window), min(n - 1, t + window)
        idxs.append(int(lo + (np.argmax(p[lo:hi + 1]) if is_peak else np.argmin(p[lo:hi + 1]))))
    idxs = sorted(set(idxs))
    out = [idxs[0]]
    for t in idxs[1:]:
        if t - out[-1] >= min_gap:
            out.append(t)
        else:
            out[-1] = t
    return out


def layer1_segments(prices, smooth_win: int = SMOOTH_WIN,
                    min_retrace_pct: float = MIN_RETRACE_PCT,
                    trend_threshold_pct: float = TREND_THRESHOLD_PCT,
                    min_seg_min: int = MIN_SEG_MIN) -> list[Segment]:
    """L1 方向切分：返回**连续覆盖** [0, n-1] 的段列表。

    与研究方法论的差异（有意）：
    1. 段区间用**闭区间** [s, e]（原脚本是半开 [s, e)），便于直接喂 `rows[s:e+1]`；
    2. 过短的段**并入前一段**而不是丢弃 —— 丢弃会在时间轴上留下空洞，
       前端的回合底纹带就会出现断口。
    """
    p = np.asarray(prices, dtype=float)
    n = len(p)
    if n == 0:
        return []
    if n < min_seg_min:
        return [Segment(0, n - 1, _direction(p[0], p[-1], trend_threshold_pct),
                        _chg(p[0], p[-1]), n)]

    smoothed = smooth_center(p, smooth_win)
    turns = anchor_to_raw(p, zigzag_turns(smoothed, min_retrace_pct, min_seg_min),
                          window=smooth_win // 2 + 1, min_gap=min_seg_min)

    raw: list[Segment] = []
    for i in range(len(turns) - 1):
        s, e = turns[i], turns[i + 1] - 1   # 转折点归后一段，两段不重叠
        if e < s:
            continue
        raw.append(Segment(s, e, _direction(p[s], p[e], trend_threshold_pct),
                           _chg(p[s], p[e]), e - s + 1))
    # 最后一段补到序列末尾（turns 末尾已含 n-1，这里只兜底）
    if raw and raw[-1].e < n - 1:
        last = raw[-1]
        raw[-1] = Segment(last.s, n - 1, last.direction, _chg(p[last.s], p[n - 1]), n - last.s)
    if not raw:
        return [Segment(0, n - 1, _direction(p[0], p[-1], trend_threshold_pct),
                        _chg(p[0], p[-1]), n)]

    # 过短段并入前一段（方向沿用前段，涨幅按合并后首尾重算）
    merged: list[Segment] = [raw[0]]
    for seg in raw[1:]:
        prev = merged[-1]
        if seg.minutes < min_seg_min:
            merged[-1] = Segment(prev.s, seg.e, prev.direction,
                                 _chg(p[prev.s], p[seg.e]), seg.e - prev.s + 1)
        else:
            merged.append(seg)
    # 首段本身过短 → 并入后一段
    if len(merged) > 1 and merged[0].minutes < min_seg_min:
        first, second = merged[0], merged[1]
        merged[:2] = [Segment(first.s, second.e, second.direction,
                              _chg(p[first.s], p[second.e]), second.e - first.s + 1)]

    # 连续性修补（必须有）：anchor_to_raw 的间距过滤会在首点/密集拐点处**覆盖**
    # 掉边界索引，导致段之间出现 1~3 分钟空洞（实测 10-21 缺 3 分钟）。
    # 这里以游标强制拼接：每段起点 = 上一段终点 + 1，任何空隙并入后一段；
    # 保证 sum(minutes) == n，前端回合底纹带不会有断口。
    out: list[Segment] = []
    cursor = 0
    for seg in merged:
        s = cursor
        e = max(seg.e, s)
        out.append(Segment(s, e, seg.direction, _chg(p[s], p[e]), e - s + 1))
        cursor = e + 1
    if out and out[-1].e < n - 1:
        last = out[-1]
        out[-1] = Segment(last.s, n - 1, last.direction,
                          _chg(p[last.s], p[n - 1]), n - last.s)
    return out


def _direction(p0: float, p1: float, threshold_pct: float) -> str:
    if not p0:
        return "横盘"
    chg = (p1 / p0 - 1) * 100
    if abs(chg) < threshold_pct:
        return "横盘"
    return "上涨" if chg > 0 else "下跌"


def _chg(p0: float, p1: float) -> float:
    return (p1 / p0 - 1) * 100 if p0 else 0.0


# ══════════════════════════════════════════════════════════════
# L2：PELT 节奏子切分（只出标签，不参与窗口）
# ══════════════════════════════════════════════════════════════

def layer2_rhythm(prices, s: int, e: int, direction: str,
                  min_size: int = L2_MIN_SIZE, max_sub: int = L2_MAX_SUB):
    """段内节奏：用 PELT 切成 2~max_sub 个子段，返回 (主标签, 子段数, 子段列表)。

    主标签 = **时长最长**子段的节奏（代表该段的主要运行方式）。
    子段列表元素：dict(start, end, label, slope, chg_pct, minutes)。
    ruptures 缺失时自动降级为「整段一个标签」（不阻塞生产）。
    """
    p = np.asarray(prices, dtype=float)
    e = min(e, len(p) - 1)
    if e - s + 1 < min_size * 2:
        slope, chg = _slope_chg(p, s, e)
        return _rhythm_label(direction, slope, None), 1, [
            dict(start=s, end=e, label=_rhythm_label(direction, slope, None),
                 slope=slope, chg_pct=chg, minutes=e - s + 1)]

    try:
        import ruptures as rpt
    except Exception:  # noqa: BLE001  缺依赖时降级，节奏只是标签
        slope, chg = _slope_chg(p, s, e)
        return _rhythm_label(direction, slope, None), 1, [
            dict(start=s, end=e, label=_rhythm_label(direction, slope, None),
                 slope=slope, chg_pct=chg, minutes=e - s + 1)]

    seg = p[s:e + 1].reshape(-1, 1)
    bkps = _pelt_breakpoints(rpt, seg, min_size, max_sub)
    if not bkps:
        slope, chg = _slope_chg(p, s, e)
        return _rhythm_label(direction, slope, None), 1, [
            dict(start=s, end=e, label=_rhythm_label(direction, slope, None),
                 slope=slope, chg_pct=chg, minutes=e - s + 1)]

    subs, prev_slope, start = [], None, 0
    bounds = list(bkps) + [e - s + 1]
    for b in bounds:
        if b <= start:
            continue
        sub_s, sub_e = s + start, s + b - 1
        slope, chg = _slope_chg(p, sub_s, sub_e)
        label = _rhythm_label(direction, slope, prev_slope)
        subs.append(dict(start=sub_s, end=sub_e, label=label, slope=slope,
                         chg_pct=chg, minutes=sub_e - sub_s + 1))
        prev_slope, start = slope, b
    if not subs:
        slope, chg = _slope_chg(p, s, e)
        return _rhythm_label(direction, slope, None), 1, [
            dict(start=s, end=e, label=_rhythm_label(direction, slope, None),
                 slope=slope, chg_pct=chg, minutes=e - s + 1)]
    main = max(subs, key=lambda d: d["minutes"])
    return main["label"], len(subs), subs


def _pelt_breakpoints(rpt, signal, min_size: int, max_sub: int):
    """在 penalty 网格上取第一个「子段数落在 2~max_sub」的切分；否则取最接近目标的。"""
    fallback, fallback_gap = None, 10 ** 9
    for pen in L2_PENALTIES:
        try:
            algo = rpt.Pelt(model="l2", min_size=min_size).fit(signal)
            bkps = [b for b in algo.predict(pen=pen) if b < len(signal)]
        except Exception:  # noqa: BLE001  个别 penalty 数值失败不影响整体
            continue
        n_sub = len(bkps)
        if 2 <= n_sub <= max_sub:
            return bkps
        gap = abs(n_sub - min(max_sub, 2))
        if gap < fallback_gap:
            fallback, fallback_gap = bkps, gap
    return fallback


def _slope_chg(p, s: int, e: int):
    """段内（每分涨跌幅 %, 累计涨跌幅 %）。"""
    if e <= s:
        return 0.0, 0.0
    chg = _chg(p[s], p[e])
    return chg / max(e - s, 1), chg


def _rhythm_label(parent_direction: str, slope: float, prev_slope) -> str:
    """节奏标签（阈值见模块常量）。

    父段方向只用于决定「反弹 / 回调」；同向子段按斜率分急/缓，并与前一子段比较加速/减速。
    """
    abs_s = abs(slope)
    if parent_direction == "下跌" and slope > RHYTHM_EPS:
        base = "反弹"
    elif parent_direction == "上涨" and slope < -RHYTHM_EPS:
        base = "回调"
    elif abs_s <= RHYTHM_EPS:
        base = "停顿"
    elif parent_direction == "横盘":
        base = "波动" if abs_s >= RHYTHM_WAVE else "横盘"
    elif parent_direction == "下跌":
        base = "急跌" if abs_s >= RHYTHM_FAST else "缓跌"
    else:
        base = "急涨" if abs_s >= RHYTHM_FAST else "缓涨"

    if (prev_slope is not None and base not in ("反弹", "回调", "停顿")
            and abs(prev_slope) > RHYTHM_EPS):
        if abs_s > abs(prev_slope) * 1.8:
            base = "加速跌" if "跌" in base else "加速涨"
        elif abs_s < abs(prev_slope) * 0.5 and abs(prev_slope) > RHYTHM_EPS * 2:
            base = "减速跌" if "跌" in base else "减速涨"
    return base
