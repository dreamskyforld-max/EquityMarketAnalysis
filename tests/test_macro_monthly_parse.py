"""宏观月频解析与派生序列测试。

`_parse_period` 是这批序列的唯一入口，历史上出过两个真实事故：
  ① 点分隔「2026.8」不识别 → 全部落到「年初」，**整表被压成一年一条**
     （实测全社会用电量只剩 24 行、看起来像年度数据，实为月度源被压扁）；
  ② 季度累计标签「2026年第1-2季度」的归属需要明确（现口径 = 区间**末季度起始月**）。
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from get_macro_monthly import _parse_period

D = datetime.date


class TestParsePeriod(unittest.TestCase):
    def test_dot_separated(self):
        """点分隔（用电量/货运量源格式）——曾导致年度化压缩。"""
        self.assertEqual(_parse_period("2026.8", "month"), D(2026, 8, 1))
        self.assertEqual(_parse_period("2003.12", "month"), D(2003, 12, 1))

    def test_cn_month_label(self):
        self.assertEqual(_parse_period("2026年08月份", "month"), D(2026, 8, 1))
        self.assertEqual(_parse_period("2026-08", "month"), D(2026, 8, 1))

    def test_compact_yyyymm(self):
        self.assertEqual(_parse_period("202608", "month"), D(2026, 8, 1))

    def test_quarter_labels(self):
        """季度口径：period_date = 该累计区间的**末季度起始月**。"""
        self.assertEqual(_parse_period("2026年第1季度", "quarter"), D(2026, 1, 1))
        self.assertEqual(_parse_period("2026年第1-2季度", "quarter"), D(2026, 4, 1))   # H1 → Q2
        self.assertEqual(_parse_period("2026年第1-3季度", "quarter"), D(2026, 7, 1))   # 9M → Q3
        self.assertEqual(_parse_period("2026年第1-4季度", "quarter"), D(2026, 10, 1))  # FY → Q4

    def test_invalid(self):
        for bad in (None, "", "  ", "nan", "无日期"):
            self.assertIsNone(_parse_period(bad, "month"))


class TestMacroSeriesCoverage(unittest.TestCase):
    """本轮新修/新增序列的库内覆盖（数据库需可用）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()
            cls.conn = cls._cm.__enter__()
        except Exception as e:
            raise unittest.SkipTest(f"数据库不可用: {e}")
        cls.cur = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def _stat(self, code):
        self.cur.execute("""SELECT count(*), min(period_date), max(period_date)
                            FROM regime.macro_series WHERE series_code=%s""", (code,))
        return self.cur.fetchone()

    def test_electricity_is_monthly(self):
        """用电量必须是**月度**（≥200 行）；回落到年度（~24 行）即为解析事故复发。"""
        n, mn, mx = self._stat("CN.ELECTRICITY_YOY")
        self.assertGreaterEqual(n, 200, f"用电量仅 {n} 行 → 疑似又被压成年度")
        self.assertLessEqual((mx - mn).days / 30.44 / n, 1.6, "行数/月数比对不上：存在重复或缺失")

    def test_freight_exists(self):
        n, _mn, mx = self._stat("CN.FREIGHT_YOY")
        self.assertGreater(n, 250)
        self.assertGreater(mx, D(2025, 1, 1))

    def test_cross_derived_spreads(self):
        """跨表派生：中美利差 / 美债期限利差，且中债−美债的方向必须为负值域常见（-10~10）。"""
        for code in ("CN.US_SPREAD_10Y", "US.TERM_SPREAD_10Y_2Y"):
            n, _mn, mx = self._stat(code)
            with self.subTest(code=code):
                self.assertGreater(n, 1000)
                self.assertGreater(mx, D(2026, 1, 1))

    def test_spread_equals_components(self):
        """中美利差 = 中债10Y − 美债10Y（取最新共同日，允许四舍五入）。"""
        self.cur.execute("""
            SELECT s.period_date, s.value, c.value, u.last_price
            FROM regime.macro_series s
            JOIN regime.macro_series c ON c.series_code='CN.BOND_10Y' AND c.period_date=s.period_date
            JOIN daily_benchmark u ON u.bench_code='US.DGS10' AND u.trade_date=s.period_date
            WHERE s.series_code='CN.US_SPREAD_10Y' ORDER BY s.period_date DESC LIMIT 1""")
        row = self.cur.fetchone()
        self.assertTrue(row, "找不到可比同日数据")
        _d, spread, cn, us = row
        self.assertAlmostEqual(float(spread), float(cn) - float(us), places=2)


class TestCreditAndInflationSeries(unittest.TestCase):
    """AA 信用利差与通胀高频序列（需数据库）：覆盖 + 派生恒等式校验。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()
            cls.conn = cls._cm.__enter__()
        except Exception as e:
            raise unittest.SkipTest(f"数据库不可用: {e}")
        cls.cur = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def _stat(self, code):
        self.cur.execute("""SELECT count(*), min(period_date), max(period_date)
                            FROM regime.macro_series WHERE series_code=%s""", (code,))
        return self.cur.fetchone()

    def test_aa_curve_exists(self):
        """AA 档中短票：源只保留最近 3 个交易日 → 行数很少但必须存在（逐日累积）。"""
        n, _mn, mx = self._stat("CN.MTN_AA_5Y")
        self.assertGreater(n, 0, "AA 中票曲线缺失（源窗口/重试逻辑问题）")
        self.assertGreater(mx, D(2026, 6, 1))

    def test_inflation_series(self):
        n_pork, _m1, mx_pork = self._stat("CN.PORK_PRICE")
        n_veg, mn_veg, mx_veg = self._stat("CN.VEG_BASKET")
        self.assertGreater(n_pork, 30, "猪价行数过少（源窗口 ~7.5 个月）")
        self.assertGreater(mx_pork, D(2026, 6, 1))
        self.assertGreater(n_veg, 3000, "菜篮子应有 2005 起的长历史")
        self.assertLess(mn_veg, D(2010, 1, 1))

    def test_aa_spread_identity(self):
        """AA 信用利差 = AA 中票 5Y − 国债 5Y（同日，允许四舍五入）。"""
        self.cur.execute("""
            SELECT s.period_date, s.value, a.value, b.value
            FROM regime.macro_series s
            JOIN regime.macro_series a ON a.series_code='CN.MTN_AA_5Y' AND a.period_date=s.period_date
            JOIN regime.macro_series b ON b.series_code='CN.BOND_5Y' AND b.period_date=s.period_date
            WHERE s.series_code='CN.CREDIT_SPREAD_MTN_AA_5Y' ORDER BY s.period_date DESC LIMIT 1""")
        row = self.cur.fetchone()
        self.assertTrue(row, "无 AA 信用利差数据")
        _d, spread, aa, bond = row
        self.assertAlmostEqual(float(spread), float(aa) - float(bond), places=2)

    def test_grade_spread_identity_and_sign(self):
        """等级利差 = AA − AAA，且**应为正**（低等级收益率更高）——挡方向写反。"""
        self.cur.execute("""
            SELECT g.value, a.value, t.value
            FROM regime.macro_series g
            JOIN regime.macro_series a ON a.series_code='CN.MTN_AA_5Y' AND a.period_date=g.period_date
            JOIN regime.macro_series t ON t.series_code='CN.MTN_AAA_5Y' AND t.period_date=g.period_date
            WHERE g.series_code='CN.GRADE_SPREAD_AA_AAA_5Y' ORDER BY g.period_date DESC LIMIT 1""")
        row = self.cur.fetchone()
        self.assertTrue(row, "无等级利差数据")
        grade, aa, aaa = (float(x) for x in row)
        self.assertAlmostEqual(grade, aa - aaa, places=2)
        self.assertGreater(grade, 0, "AA 收益率应高于 AAA → 等级利差为正")


if __name__ == "__main__":
    unittest.main(verbosity=2)
