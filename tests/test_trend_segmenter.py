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


# —— L1.5 大加速切分（split_big_accel）——

def test_split_big_accel_slow_then_fast_gets_split():
    # 缓跌 120 分钟（-0.005%/min）→ 急跌 20 分钟（-0.05%/min，10 倍），无方向反转，应切成 2 段
    slow = [100.0 - i * 0.005 for i in range(120)]
    fast_start = slow[-1]
    fast = [fast_start - i * 0.05 for i in range(1, 21)]
    prices = slow + fast
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1 and segs[0].direction == "下跌"
    splitted = ts.split_big_accel(prices, segs)
    assert len(splitted) == 2
    assert splitted[0].e == 119              # 缓侧止于索引 119
    assert splitted[1].s == 120              # 急侧从索引 120 起
    assert splitted[1].direction == "下跌"
    assert splitted[1].minutes == 20


def test_split_big_accel_relative_threshold_triggers_low_vol():
    # 低波动票：急侧 -0.02%/min（绝对 < 旧阈值 0.03），相对自身缓侧 -0.004%/min 是 5 倍，
    # 且超过「当日 P85 斜率 + 下限」→ 旧绝对口径会漏切，相对口径应能切出。
    slow = [20.0 - i * 0.0008 for i in range(120)]          # -0.004%/min
    fast_start = slow[-1]
    fast = [fast_start - i * 0.004 for i in range(1, 21)]   # -0.02%/min
    prices = slow + fast
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1 and segs[0].direction == "下跌"
    splitted = ts.split_big_accel(prices, segs)
    assert len(splitted) == 2
    assert splitted[1].s == 120


def test_split_big_accel_floor_blocks_crawl():
    # 急侧 < 绝对下限 0.01 的「3×爬行」不应切（即使比值 ≥3，绝对太慢不算「大加速」）
    slow = [100.0 - i * 0.002 for i in range(120)]          # -0.002%/min
    fast_start = slow[-1]
    fast = [fast_start - i * 0.009 for i in range(1, 21)]   # -0.009%/min（< 0.01 下限）
    prices = slow + fast
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1 and segs[0].direction == "下跌"
    assert len(ts.split_big_accel(prices, segs)) == 1       # 不切


def test_split_big_accel_constant_slope_not_split():
    # 恒定斜率下跌：无加速，不应切
    prices = [100.0 - i * 0.01 for i in range(120)]
    segs = ts.layer1_segments(prices)
    assert len(segs) == 1
    assert len(ts.split_big_accel(prices, segs)) == 1


def test_split_big_accel_preserves_continuity():
    rng = np.random.default_rng(23)
    prices = 100 + np.cumsum(rng.normal(0, 0.3, 330))
    segs = ts.layer1_segments(prices)
    splitted = ts.split_big_accel(prices, segs)
    assert splitted[0].s == 0
    assert splitted[-1].e == len(prices) - 1
    assert sum(s.minutes for s in splitted) == len(prices)
    for prev, cur in zip(splitted, splitted[1:]):
        assert cur.s == prev.e + 1           # 无缝、不重叠


def test_split_big_accel_flat_segments_untouched():
    # 横盘段不参与切分，原样保留
    flat = [10.0 + (0.001 if i % 2 else -0.001) for i in range(60)]
    segs = ts.layer1_segments(flat)
    assert segs[0].direction == "横盘"
    splitted = ts.split_big_accel(flat, segs)
    assert len(splitted) == 1
    assert splitted[0].direction == "横盘"
