"""分时切分研究 · 盘中前缀稳定性实测（上线硬门槛 P4）

盘中每分钟重算时，**已收口的段不得重排**。本模块量化这一风险：

  drift(t) = prefix→t 时刻算出的「已收口段」边界集合
            与收盘后全天结果的边界集合中，不一致的处数
            已收口 = 段终点距 prefix 末端 ≥ 3 分钟

对比三种配置：
  v3        规则堆（对照）
  dp        带权 DP（候选 = v3 边界，生产推断的真实设定）
  dp_frozen 带权 DP + 基准冻结（σ / 效应量取**上一交易日**的值，不随当日数据浮动）

用法：
    python3 -m research.force_segmentation.seg.stability
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from .common import sigma_noise, amp_min_of, anchor_segs
from .dp import DayDP, dp_segment, scale_of
from .panels import load_panel
from .rules import segment_v3

TOL = 2                 # 边界一致性容差（分钟）
CLOSE_GAP = 3           # 距 prefix 末端 ≥ 该值才算「已收口」
THR = (10.0, 0.08, 0.45)
W_TRAINED = np.array([1.0, 7.317, 29.383, 19.846, 35.740, 5.025])   # 2026-10-30 训练结果


def cuts_from_v3(p, tt):
    """生产推断时的候选边界 = v3 超切边界（去掉午休切点）"""
    v3 = [(s, e) for s, e, _ in segment_v3(p, tt)]
    n = len(p)
    cut = [0] + [e + 1 for _, e in v3[:-1]]
    lunch = tt.index("13:00") if "13:00" in tt else -1
    return [c for c in cut if c != lunch and 0 <= c < n]


def predict_dp(p, tt, w=W_TRAINED, scale=None):
    dd = DayDP(p, tt, cands=cuts_from_v3(p, tt))
    sc = scale if scale is not None else scale_of(dd)
    return anchor_segs(p, dp_segment(dd, w / sc, THR))


def predict_v3(p, tt):
    return [(s, e) for s, e, _ in segment_v3(p, tt)]


def predict_online(p, tt, frozen_bounds=(), w=W_TRAINED):
    """在线形态：已收口段的边界**强制保持不变**，只对尾部重跑 DP。

    frozen_bounds = 已冻结的段起点索引序列；冻结区内只允许在这些位置切。
    这样「已收口段不重排」是构造性保证（无需事后校验）。
    """
    dd = DayDP(p, tt, cands=cuts_from_v3(p, tt))
    if frozen_bounds:
        fb = set(int(b) for b in frozen_bounds)
        n = len(p)
        cut_off = max(fb) if fb else 0
        mask = dd.is_cand.copy()
        for i in range(min(cut_off, n)):
            mask[i] = (i in fb) or i == 0
        dd.is_cand = mask
        dd.cand = np.flatnonzero(mask)
    return anchor_segs(p, dp_segment(dd, w / scale_of(dd), THR))


def _closed(seqs, idx):
    """已收口段的起点集合（终点距 prefix 末端 ≥ CLOSE_GAP）"""
    return [s for s, e in seqs if e <= idx - CLOSE_GAP]


def simulate_online(p, tt, checkpoints, w=W_TRAINED):
    """按 checkpoint 顺序推进的在线模拟：已收口段冻结，只对尾部重跑 DP。

    返回 [(时刻, 与上一步的漂移, 段数)] —— 这是**生产真正关心的指标**：
    相邻两次计算之间，已收口段不得变化（与收盘定稿的差异可留到盘后定稿解决）。
    """
    frozen = [0]
    prev_closed, prev_idx = None, None
    out = []
    for t in checkpoints:
        idx = tt.index(t)
        pre = predict_online(p[:idx + 1], tt[:idx + 1], frozen, w)
        if prev_closed is None:
            d = 0
        else:
            # 只比对「上一次就已收口」的段：新收口的段不算漂移
            again = [s for s, e in pre if e <= prev_idx - CLOSE_GAP]
            d = len(set(prev_closed) ^ set(again))
        out.append((t, d, len(pre)))
        closed = _closed(pre, idx)
        frozen = [0] + closed
        prev_closed, prev_idx = closed, idx
    return out


def drift_stepwise(p, tt, predict, checkpoints):
    """离线形态的逐步漂移（对照）：相邻两次 prefix 计算之间已收口段的变化数"""
    prev, prev_idx = None, None
    out = []
    for t in checkpoints:
        idx = tt.index(t)
        pre = predict(p[:idx + 1], tt[:idx + 1])
        if prev is None:
            d = 0
        else:
            again = [s for s, e in pre if e <= prev_idx - CLOSE_GAP]
            d = len(set(prev) ^ set(again))
        out.append((t, d, len(pre)))
        prev, prev_idx = _closed(pre, idx), idx
    return out


def drift(p, tt, predict, prefix_times):
    """返回 [(prefix 时刻, 漂移处数, prefix 段数, 全天段数)]"""
    full = predict(p, tt)
    out = []
    for t in prefix_times:
        idx = tt.index(t)
        pre = predict(p[:idx + 1], tt[:idx + 1])
        fb = [s for s, e in full if e <= idx - CLOSE_GAP]
        pb = [s for s, e in pre if e <= idx - CLOSE_GAP]
        diff = sorted(set(fb) ^ set(pb))
        out.append((t, len(diff), len(pre), len(full)))
    return out


def run(prefix_times=("10:30", "11:30", "13:30", "14:30", "15:20")):
    samples = [("HK.00700", dt.date(2026, 10, d)) for d in (27, 28, 29)]
    samples += [("HK.09988", dt.date(2026, 10, 29)), ("HK.01810", dt.date(2026, 10, 29))]
    print(f"{'样本':<20}{'配置':<13}{'逐步漂移':>9}{'段数(末)':>9}   各步漂移")
    total = {"v3": 0, "dp(离线)": 0, "dp(在线冻结)": 0}
    for code, day in samples:
        tt, p = load_panel(code, day)
        if len(tt) < 60:
            continue
        rows = (("v3", drift_stepwise(p, tt, predict_v3, prefix_times)),
                ("dp(离线)", drift_stepwise(p, tt, predict_dp, prefix_times)),
                ("dp(在线冻结)", simulate_online(p, tt, prefix_times)))
        for name, r in rows:
            sc = sum(x[1] for x in r)
            total[name] += sc
            detail = " ".join(f"{t}:{d}" for t, d, *_ in r)
            print(f"{code + ' ' + str(day):<20}{name:<13}{sc:>7}{r[-1][2]:>9}   {detail}")
    print("\n合计**逐步**漂移（相邻两次计算之间已收口段的变化处数，越小越好）：")
    for k, v in total.items():
        print(f"  {k:<14} {v}")


if __name__ == "__main__":
    run()
