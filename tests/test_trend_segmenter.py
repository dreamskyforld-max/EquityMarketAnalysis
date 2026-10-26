"""trend_segmenter（单日分钟级股价趋势分段）单元测试。

锁定三件事：
1. **连续性**：段列表必须无空洞地覆盖 [0, n-1]（sum(minutes) == n）——
   前端回合底纹带依赖它，缺一分钟就会出现断口；
2. **方向判定**：单边上涨/下跌/横盘按累计涨跌幅 + 阈值划分；
3. **节奏标签**：L2 只在段内出标签，不改变窗口。
"""
import numpy as np
import pytest

import trend_segmenter as ts


# —— 边界输入 ——

def test_empty_prices():
    assert ts.layer1_segments([]) == []


def test_single_point():
    segs = ts.layer1_segments([10.0])
    assert len(segs) == 1
    assert (segs[0].s, segs[0].e) == (0, 0)


def test_short_series_falls_back_to_one_segment():
    segs = ts.layer1_segments([10.0, 10.1, 10.2])
    assert len(segs) == 1
    assert segs[0].minutes == 3


# —— 方向判定 ——

def test_monotonic_rise_is_single_up_segment():
    prices = [10.0 + i * 0.02 for i in range(60)]      # 每分 +0.2%，累计 +11.8%
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1
    assert segs[0].direction == "上涨"
    assert segs[0].chg_pct > 10


def test_monotonic_fall_is_single_down_segment():
    prices = [10.0 - i * 0.02 for i in range(60)]
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1
    assert segs[0].direction == "下跌"


def test_flat_series_is_flat_segment():
    prices = [10.0 + (0.001 if i % 2 else -0.001) for i in range(60)]   # 抖动 ±0.01%
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1
    assert segs[0].direction == "横盘"


def test_v_shape_gives_two_segments():
    up = [10.0 + i * 0.03 for i in range(40)]           # +11.7%
    down = [up[-1] - i * 0.03 for i in range(1, 41)]    # 回落
    segs = ts.layer1_segments(up + down)
    assert len(segs) >= 2
    assert segs[0].direction == "上涨"
    assert segs[-1].direction == "下跌"


# —— 连续性（最关键）——

@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_segments_cover_whole_series(seed):
    rng = np.random.default_rng(seed)
    prices = 100 + np.cumsum(rng.normal(0, 0.25, 330))   # 沪/港股一天 330 分钟
    segs = ts.layer1_segments(prices)
    assert segs[0].s == 0
    assert segs[-1].e == len(prices) - 1
    assert sum(s.minutes for s in segs) == len(prices)
    for prev, cur in zip(segs, segs[1:]):
        assert cur.s == prev.e + 1          # 无缝、不重叠
    for s in segs:
        assert s.minutes == s.e - s.s + 1
        assert s.minutes >= 1


def test_all_segments_meet_min_length_after_merge():
    rng = np.random.default_rng(7)
    prices = 100 + np.cumsum(rng.normal(0, 0.3, 330))
    segs = ts.layer1_segments(prices)
    # 除"整体只剩一段"的极端情况，合并后每段都应 ≥ MIN_SEG_MIN
    if len(segs) > 1:
        assert all(s.minutes >= ts.MIN_SEG_MIN for s in segs)


# —— L2 节奏（只出标签，不改窗口）——

def test_rhythm_single_label_for_short_segment():
    prices = np.array([10.0, 10.01, 10.02, 10.03])
    label, n_sub, subs = ts.layer2_rhythm(prices, 0, 3, "上涨")
    assert n_sub == 1
    assert len(subs) == 1
    assert label in ("急涨", "缓涨", "停顿", "回调", "加速涨", "减速涨")


def test_rhythm_pelt_splits_long_segment():
    rng = np.random.default_rng(11)
    prices = np.concatenate([
        10 + np.cumsum(rng.normal(0.02, 0.02, 40)),      # 缓涨
        10.8 - np.cumsum(rng.normal(0.02, 0.02, 40)),    # 回落
    ])
    label, n_sub, subs = ts.layer2_rhythm(prices, 0, len(prices) - 1, "上涨")
    assert n_sub >= 1
    assert label
    assert sum(s["minutes"] for s in subs) == len(prices)


@pytest.mark.parametrize("direction,slope,expect", [
    ("下跌", 0.05, "反弹"),
    ("上涨", -0.05, "回调"),
    ("上涨", 0.0, "停顿"),
    ("下跌", -0.05, "急跌"),
    ("下跌", -0.01, "缓跌"),
    ("上涨", 0.05, "急涨"),
    ("上涨", 0.01, "缓涨"),
    ("横盘", 0.03, "波动"),
    ("横盘", 0.005, "横盘"),
])
def test_rhythm_label_thresholds(direction, slope, expect):
    assert ts._rhythm_label(direction, slope, None) == expect


def test_rhythm_label_accelerate_decelerate():
    assert ts._rhythm_label("下跌", -0.06, -0.01) == "加速跌"
    assert ts._rhythm_label("上涨", 0.06, 0.01) == "加速涨"
    assert ts._rhythm_label("下跌", -0.004, -0.05) == "减速跌"
    assert ts._rhythm_label("上涨", 0.004, 0.05) == "减速涨"
