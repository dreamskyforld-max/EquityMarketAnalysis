"""db.clean_ohlc —— OHLC 有效性护栏测试（不连库）。

背景（2026-10-27，勿删）：daily_benchmark 的 OHLC 回填完成后发现 11 行源端脏数据：
  · 新浪 HSI 早期（2013-12-20/23/24）开盘缺失被填 0 → open=0；
  · 日韩节假日「假交易日」或字段错位 → high < low（JP.N225 2024-08-12 等）；
  · HK.800000 2014-09-01 low > close；
  · KR.KS11 2025-10-06 open < low。
一次性 UPDATE 清洗后，护栏必须留在**写入侧**——否则重跑全量回填会原样写回。

本文件锁住：db.clean_ohlc 的合法/非法判定、舍入容差、以及上述真实脏行的回归。
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


class TestCleanOhlcValid(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(db.clean_ohlc(100.0, 110.0, 95.0), (100.0, 110.0, 95.0))

    def test_flat_day(self):
        """一字线（open=high=low=close）合法。"""
        self.assertEqual(db.clean_ohlc(33.5, 33.5, 33.5), (33.5, 33.5, 33.5))

    def test_string_numbers(self):
        """新浪 K 线 JSON 字段是字符串，必须能接受。"""
        self.assertEqual(db.clean_ohlc("3010.5", "3050", "2999.25"),
                         (3010.5, 3050.0, 2999.25))

    def test_round_to_4dp(self):
        o, h, lo = db.clean_ohlc(1.23456789, 1.3456789, 1.1234567)
        self.assertEqual((o, h, lo), (1.2346, 1.3457, 1.1235))

    def test_rounding_tolerance_accepted(self):
        """close 4 位小数 vs high/low 2 位小数产生的亚分级差异不应误杀。"""
        # open 比 high 高 0.005（实测最大 0.001）
        self.assertEqual(db.clean_ohlc(25878.9902, 25878.99, 25700.0)[0], 25878.9902)
        # open 比 low 低 0.005
        self.assertEqual(db.clean_ohlc(21008.3395, 21100.0, 21008.34)[0], 21008.3395)


class TestCleanOhlcInvalid(unittest.TestCase):
    def test_none_any(self):
        for args in ((None, 110.0, 95.0), (100.0, None, 95.0), (100.0, 110.0, None)):
            self.assertEqual(db.clean_ohlc(*args), (None, None, None), args)

    def test_nan_and_inf(self):
        for bad in (float("nan"), math.inf, -math.inf):
            self.assertEqual(db.clean_ohlc(bad, 110.0, 95.0), (None, None, None), bad)

    def test_non_numeric(self):
        self.assertEqual(db.clean_ohlc("", 110.0, 95.0), (None, None, None))
        self.assertEqual(db.clean_ohlc("--", 110.0, 95.0), (None, None, None))

    def test_zero_or_negative(self):
        self.assertEqual(db.clean_ohlc(0, 22936.92, 22713.699), (None, None, None))
        self.assertEqual(db.clean_ohlc(-1.0, 110.0, 95.0), (None, None, None))

    def test_high_below_low(self):
        self.assertEqual(db.clean_ohlc(35002.23, 34422.88, 35648.40), (None, None, None))

    def test_open_above_high(self):
        self.assertEqual(db.clean_ohlc(200.0, 110.0, 95.0), (None, None, None))

    def test_open_below_low(self):
        self.assertEqual(db.clean_ohlc(90.0, 110.0, 95.0), (None, None, None))

    def test_tolerance_exceeded(self):
        """差 0.02 > eps=0.01 → 判为脏数据（不是舍入）。"""
        self.assertEqual(db.clean_ohlc(110.02, 110.0, 95.0), (None, None, None))


class TestRealDirtyRows(unittest.TestCase):
    """2026-10-27 实际清洗掉的 11 行中的代表值（回归锚点）。"""

    def test_hsi_zero_open_20131220(self):
        self.assertEqual(db.clean_ohlc(0, 22936.92, 22713.699), (None, None, None))

    def test_n225_holiday_20240812(self):
        """山之日休市假交易日，源把高低字段写反。"""
        self.assertEqual(db.clean_ohlc(35249.63, 34422.88, 35648.40), (None, None, None))

    def test_ks11_open_below_low_20251006(self):
        self.assertEqual(db.clean_ohlc(3525.48, 3549.21, 3549.21), (None, None, None))

    def test_hsi_low_above_close_20140901(self):
        """open 24703.74 < low 24752.09 —— OHL 与 close(24696.18) 互不自洽。"""
        self.assertEqual(db.clean_ohlc(24703.74, 24891.27, 24752.09), (None, None, None))

    def test_clean_row_survives(self):
        """同期正常行必须原样保留（护栏不能误杀历史数据）。"""
        self.assertEqual(db.clean_ohlc(24819.44, 24985.62, 24769.79),
                         (24819.44, 24985.62, 24769.79))


if __name__ == "__main__":
    unittest.main()
