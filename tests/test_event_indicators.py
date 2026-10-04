"""情绪事件指标窗口聚合测试（纯函数，无需数据库）。

重点验证 **PIT 安全**：交易日 t 的指标值只能用到 t 之前（或已公告）的事件——
窗口边界、forward 语义、源覆盖前置空，任一错位都会变成回测前视。
"""
import datetime
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_state_daily import _win_agg, _mask_before

D = datetime.date


class TestWinAggTrailing(unittest.TestCase):
    """回看窗口 (t−days, t]：左开右闭，含当日、不含 days 天前那一日。"""

    def setUp(self):
        self.idx = [D(2026, 9, 15), D(2026, 9, 30)]
        self.dates = [D(2026, 9, 1), D(2026, 9, 20)]
        self.vals = [10.0, 5.0]

    def test_sum(self):
        s = _win_agg(self.dates, self.vals, self.idx, 30, "sum")
        # t=09-15: (08-16, 09-15] → 仅 09-01 → 10
        # t=09-30: (08-31, 09-30] → 09-01 + 09-20 → 15
        self.assertEqual(s.iloc[0], 10.0)
        self.assertEqual(s.iloc[1], 15.0)

    def test_count(self):
        s = _win_agg(self.dates, None, self.idx, 30, "count")
        self.assertEqual(list(s.values), [1.0, 2.0])

    def test_max(self):
        s = _win_agg(self.dates, self.vals, self.idx, 30, "max")
        self.assertEqual(s.iloc[0], 10.0)
        self.assertEqual(s.iloc[1], 10.0)

    def test_left_open_right_closed(self):
        """边界：恰好落在 t 当日的事件计入；恰好落在 t−days 的事件不计。"""
        idx = [D(2026, 9, 30)]
        # 09-30 当日事件 → 计入（右闭）
        s1 = _win_agg([D(2026, 9, 30)], [1.0], idx, 30, "sum")
        self.assertEqual(s1.iloc[0], 1.0)
        # 08-31 = t−30 → 不计（左开）
        s2 = _win_agg([D(2026, 8, 31)], [1.0], idx, 30, "sum")
        self.assertEqual(s2.iloc[0], 0.0)
        # 09-01 = t−29 → 计入
        s3 = _win_agg([D(2026, 9, 1)], [1.0], idx, 30, "sum")
        self.assertEqual(s3.iloc[0], 1.0)

    def test_empty_window_returns_zero(self):
        s = _win_agg([D(2020, 1, 2)], [1.0], self.idx, 30, "sum")
        self.assertEqual(list(s.values), [0.0, 0.0])

    def test_no_events_returns_nan(self):
        s = _win_agg([], None, self.idx, 30, "count")
        self.assertTrue(s.isna().all())


class TestWinAggForward(unittest.TestCase):
    """前瞻窗口 (t, t+days]：只含**未来**已公告事件（用于解禁排期）。"""

    def test_forward_excludes_past_and_same_day(self):
        dates = [D(2026, 10, 10)]
        vals = [7.0]
        idx = [D(2026, 9, 30), D(2026, 10, 10), D(2026, 10, 11)]
        s = _win_agg(dates, vals, idx, 30, "sum", forward=True)
        self.assertEqual(s.iloc[0], 7.0)    # 09-30 → (09-30, 10-30] 含 10-10
        self.assertEqual(s.iloc[1], 0.0)    # 10-10 当日不计（左开）
        self.assertEqual(s.iloc[2], 0.0)    # 10-11 已过期，不计

    def test_forward_beyond_window(self):
        s = _win_agg([D(2026, 11, 15)], [1.0], [D(2026, 9, 30)], 30, "sum", forward=True)
        self.assertEqual(s.iloc[0], 0.0)    # 11-15 在 (09-30, 10-30] 之外


class TestPIT(unittest.TestCase):
    """PIT：未来新发生的事件不得改变历史日的取值。"""

    def test_history_unchanged_by_future_events(self):
        idx = [D(2026, 3, 31), D(2026, 6, 30), D(2026, 9, 30)]
        past = [D(2026, 3, 1), D(2026, 5, 1)]
        future = [D(2026, 12, 1)]
        v = [3.0, 4.0]
        base = _win_agg(past, v, idx, 30, "sum")
        with_future = _win_agg(past + future, v + [100.0], idx, 30, "sum")
        pd.testing.assert_series_equal(base, with_future)

    def test_forward_window_only_uses_announced(self):
        """前瞻窗口同样 PIT：解禁排期是公告先行的已知信息。"""
        idx = [D(2026, 9, 30)]
        s = _win_agg([D(2026, 10, 15), D(2027, 1, 1)], [5.0, 9.0], idx, 30, "sum", forward=True)
        self.assertEqual(s.iloc[0], 5.0)    # 仅窗口内那笔，远期排期不计


class TestMaskBefore(unittest.TestCase):
    """源覆盖前的日期必须置空——否则「源无数据」会被读成 0（家数/金额口径尤其致命）。"""

    def test_mask(self):
        idx = [D(2012, 6, 1), D(2013, 8, 20), D(2013, 9, 1)]
        s = pd.Series([0.0, 0.0, 12.0], index=idx)
        out = _mask_before(s, D(2013, 8, 20))
        self.assertTrue(np.isnan(out.iloc[0]))
        self.assertEqual(out.iloc[1], 0.0)   # 覆盖首日本身保留（窗口含首日事件）
        self.assertEqual(out.iloc[2], 12.0)

    def test_mask_none_first(self):
        idx = [D(2013, 8, 20)]
        s = pd.Series([1.0], index=idx)
        self.assertEqual(_mask_before(s, None).iloc[0], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
