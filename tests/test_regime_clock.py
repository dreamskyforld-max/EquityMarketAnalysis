"""产出缺口与美林投资时钟测试（§3.1 的「方法类」指标）。

三个必须钉死的点：

  ① **HP 滤波不能直接全样本跑**：HP 默认是**双边**滤波（趋势点用到未来期），
     全样本跑一遍再把趋势对齐到历史 = **前视**。本实现是「单边」——对第 i 期只用 `lv[:i+1]`
     重算并取末点，成本是 O(n) 次滤波（本数据量下毫秒级）。
  ② **象限是分类值，不能有分位**：1-4 是类别标签，`percentile` 必须为 None
     （否则会被当成"历史分位 25/50/75"误用）。
  ③ **HP 自检**：线性序列的 HP 趋势必须等于自身（滤波不引入虚假波动）。
"""
import datetime
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_state_daily import (_clock_map, _clock_points, _hp_trend,
                                _output_gap_points, NO_PERCENTILE)

D = datetime.date


class TestHpFilter(unittest.TestCase):
    def test_linear_series_is_preserved(self):
        """线性输入的 HP 趋势＝自身（HP 只惩罚趋势的二阶差分，线性项不受罚）。"""
        lin = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        np.testing.assert_allclose(_hp_trend(lin), lin, atol=1e-6)

    def test_length_and_short_input(self):
        self.assertEqual(len(_hp_trend(np.arange(20.0))), 20)
        short = np.array([3.0, 4.0])
        np.testing.assert_allclose(_hp_trend(short), short,
                                   err_msg="长度 <3 时原样返回，不得抛错")

    def test_trend_is_smoother_than_input(self):
        rng = np.random.default_rng(7)
        y = np.cumsum(rng.normal(size=80)) + 100.0
        t = _hp_trend(y)
        self.assertLess(np.std(np.diff(t)), np.std(np.diff(y)), "趋势应比原序列更平滑")


class TestClockMap(unittest.TestCase):
    """四象限映射（纯函数）：1=复苏 2=过热 3=滞胀 4=衰退。"""

    def test_all_quadrants(self):
        self.assertEqual(_clock_map(True, False), 1)    # 增长↑ 通胀↓ → 复苏
        self.assertEqual(_clock_map(True, True), 2)     # 增长↑ 通胀↑ → 过热
        self.assertEqual(_clock_map(False, True), 3)    # 增长↓ 通胀↑ → 滞胀
        self.assertEqual(_clock_map(False, False), 4)   # 增长↓ 通胀↓ → 衰退

    def test_covers_exactly_four_codes(self):
        combos = {_clock_map(g, i) for g in (True, False) for i in (True, False)}
        self.assertEqual(combos, {1, 2, 3, 4})


class TestOutputGapPoints(unittest.TestCase):
    """库内实测（需数据库）：缺口必须从 2014 起、量级合理、且极值落在真实危机期。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()
            cls.conn = cls._cm.__enter__()
        except Exception as e:
            raise unittest.SkipTest(f"数据库不可用: {e}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def test_points_range_and_sign(self):
        pts = _output_gap_points(self.conn)
        self.assertGreaterEqual(len(pts), 45, f"产出缺口仅 {len(pts)} 期（源链 2011Q1 + 12 季暖机）")
        vals = [v for _, v in pts]
        self.assertTrue(all(-20 < v < 10 for v in vals), f"缺口量级异常: {min(vals)}~{max(vals)}")
        worst = min(pts, key=lambda x: x[1])
        self.assertEqual(worst[0].year, 2020, "最低点应落在 2020 疫情期")
        self.assertLess(worst[1], -3.0, "2020Q1 产出缺口应明显为负")

    def test_no_lookahead_ordering(self):
        """逐期推进：每一期的 release_time 必须严格递增（否则就是用了未来数据）。"""
        pts = _output_gap_points(self.conn)
        rels = [r for r, _ in pts]
        self.assertEqual(rels, sorted(rels), "release_time 未按时间推进 → 存在前视风险")


class TestClockInDb(unittest.TestCase):
    """库内实测（需数据库）：象限取值域 + **分类值不得有分位**。"""

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

    def test_quadrant_values(self):
        self.cur.execute("""SELECT DISTINCT raw_value FROM regime.indicator_value
                            WHERE indicator_code='MACRO.CLOCK_QUADRANT' AND raw_value IS NOT NULL
                            ORDER BY 1""")
        vals = {float(r[0]) for r in self.cur.fetchall()}
        self.assertTrue(vals and vals <= {1.0, 2.0, 3.0, 4.0}, f"象限取值越界: {vals}")

    def test_quadrant_has_no_percentile(self):
        """分类值不得写分位（NO_PERCENTILE 契约）。"""
        self.assertIn("MACRO.CLOCK_QUADRANT", NO_PERCENTILE)
        self.cur.execute("""SELECT count(*) FROM regime.indicator_value
                            WHERE indicator_code='MACRO.CLOCK_QUADRANT'
                              AND percentile IS NOT NULL""")
        self.assertEqual(self.cur.fetchone()[0], 0, "美林象限是分类值，percentile 必须为空")

    def test_output_gap_written(self):
        self.cur.execute("""SELECT count(*), min(trade_date), max(trade_date)
                            FROM regime.indicator_value
                            WHERE indicator_code='MACRO.OUTPUT_GAP' AND raw_value IS NOT NULL""")
        n, mn, mx = self.cur.fetchone()
        self.assertGreater(n, 2000, f"产出缺口日频行数过少: {n}")
        self.assertGreaterEqual(mn, D(2014, 1, 1), "首值不得早于 2014（源链 2011Q1 + 12 季暖机）")
        self.assertGreater(mx, D(2026, 6, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
