"""产出缺口与美林投资时钟测试（§3.1 的「方法类」指标）。

五个必须钉死的点：

  ① **HP 滤波不能直接全样本跑**：HP 默认是**双边**滤波（趋势点用到未来期），
     全样本跑一遍再把趋势对齐到历史 = **前视**。本实现是「单边」——对第 i 期只用 `lv[:i+1]`
     重算并取末点，成本是 O(n) 次滤波（本数据量下毫秒级）。
  ② **象限是分类值，不能有分位**：1-4 是类别标签，`percentile` 必须为 None
     （否则会被当成"历史分位 25/50/75"误用）。
  ③ **HP 自检**：线性序列的 HP 趋势必须等于自身（滤波不引入虚假波动）。
  ④ **v2 纯季频 + 双轴死区 + 增长轴双口径**（2026-11-11）：v1 让月度 CPI 单独
     触发象限（54 次切换里 52% 由月度 CPI 驱动）、零阈值硬判 sign，实测 ±0.1pp
     扰动就改写 24% 的交易日标签。v2 只在季度 GDP 发布时点重算、死区内滞回、
     增长轴需 Δgap 与 Δ单季同比一致才更新。测试锁住事件数、切换率、参数扰动。
  ⑤ **参数扰动回归**：口径参数小幅变化不得大幅改写标签，防止回退到 v1 的
     边界噪声主导状态（阈值取自 §7.21 v2 评测实测值）。
"""
import datetime
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_state_daily as msd
from market_state_daily import (_clock_map, _clock_points, _gap_frame, _hp_trend,
                                _infl_flag, _output_gap_points, _pit_fill,
                                _sign_eps, NO_PERCENTILE)

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


class TestClockV2Pure(unittest.TestCase):
    """v2 死区与通胀水平门控（纯函数，不连库）。"""

    def test_sign_eps_deadband(self):
        self.assertIsNone(_sign_eps(0.05, 0.20), "死区内不判方向（保持上一状态）")
        self.assertIsNone(_sign_eps(-0.20, 0.20), "边界严格大于，等于 eps 不算翻转")
        self.assertTrue(_sign_eps(0.30, 0.20))
        self.assertFalse(_sign_eps(-0.30, 0.20))
        self.assertIsNone(_sign_eps(float("nan"), 0.20), "NaN 不得被当成「↓」")
        self.assertIsNone(_sign_eps(None, 0.20))

    def test_infl_flag_deflation_gate(self):
        """通缩期「跌幅收窄」不等于通胀上行（v1 会把 4 个通缩月打进过热/滞胀）。"""
        self.assertIsNone(_infl_flag(-0.2, -0.6, 0.20))
        self.assertFalse(_infl_flag(-0.6, -0.2, 0.20), "通缩加深仍应判通胀↓")
        self.assertTrue(_infl_flag(2.0, 1.5, 0.20))
        self.assertIsNone(_infl_flag(1.0, 0.95, 0.20), "正常期死区内也不翻转")


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


class TestClockV2InDb(unittest.TestCase):
    """v2 计算层库内契约（需数据库）：纯季频、PIT 时点、增长轴有效性。"""

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

    def test_pure_quarterly_and_pit_timestamps(self):
        gap = _output_gap_points(self.conn)
        pts = _clock_points(self.conn)
        self.assertGreater(len(pts), 40)
        self.assertEqual(len(pts), len(gap) - 1,
                         "v2 必须纯季频：每季度事件恰好 1 条，首期只用于算方向")
        self.assertEqual([r for r, _ in pts], [r for r, _ in gap][1:],
                         "事件时点必须就是季度 GDP 的 release_time → 无 CPI 月度触发、无前视")
        self.assertEqual([r for r, _ in pts], sorted(r for r, _ in pts))
        self.assertTrue({int(v) for _, v in pts} <= {1, 2, 3, 4})

    def test_gap_frame_order_and_coverage(self):
        f = _gap_frame(self.conn)
        months = np.array([d.year * 12 + d.month for d in f["period_date"]])
        self.assertTrue(np.all(np.diff(months) == 3), "季度序列必须连续（链式水平的前提）")
        self.assertEqual(int(f["yoy"].isna().sum()), 0, "单季同比必须齐（增长轴第二票）")
        self.assertTrue(f["gap"].iloc[:msd._GAP_MIN_Q - 1].isna().all())

    def test_growth_axis_vs_yoy(self):
        f = _gap_frame(self.conn).dropna(subset=["gap"]).reset_index(drop=True)
        pts = _clock_points(self.conn)
        ref = (np.diff(f["yoy"].to_numpy()) > 0).astype(int)
        axis = np.isin([int(v) for _, v in pts], [1, 2]).astype(int)
        self.assertEqual(len(axis), len(ref))
        self.assertGreaterEqual(np.mean(axis == ref), 0.68,
                                "v1 增长轴与单季同比一致率仅 0.647，v2 实测 0.725")

    def test_switch_rate(self):
        q = np.array([int(v) for _, v in _clock_points(self.conn)])
        self.assertLessEqual(int(np.sum(q[1:] != q[:-1])), 30,
                             "v1 同期 54 次切换（4.4 次/年）；v2 实测 23 次（1.9 次/年）")


class TestClockV2Robustness(unittest.TestCase):
    """参数微扰不得大幅改写标签（v1 加 ±0.1pp 死区后仅 75.9% 一致）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()
            cls.conn = cls._cm.__enter__()
        except Exception as e:
            raise unittest.SkipTest(f"数据库不可用: {e}")
        cur = cls.conn.cursor()
        cur.execute("""SELECT DISTINCT trade_date FROM regime.indicator_value
                       WHERE indicator_code = 'MACRO.CLOCK_QUADRANT'
                         AND raw_value IS NOT NULL ORDER BY 1""")
        cls.idx = [r[0] for r in cur.fetchall()]
        if not cls.idx:
            raise unittest.SkipTest("库内无时钟历史行，跳过日频标签扰动对比")

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def _labels(self, **kw):
        old = {k: getattr(msd, k) for k in kw}
        try:
            for k, v in kw.items():
                setattr(msd, k, v)
            return _pit_fill(self.idx, _clock_points(self.conn)).to_numpy()
        finally:
            for k, v in old.items():
                setattr(msd, k, v)

    def test_deadband_perturbation(self):
        base = self._labels()
        m = np.isfinite(base)
        hi = self._labels(CLOCK_GAP_EPS=0.25, CLOCK_CPI_EPS=0.25)
        lo = self._labels(CLOCK_GAP_EPS=0.10, CLOCK_CPI_EPS=0.10)
        self.assertGreaterEqual(np.mean(base[m] == hi[m]), 0.88,   # 实测 0.92
                                "死区上调 0.05pp 不应大幅改写标签")
        self.assertGreaterEqual(np.mean(base[m] == lo[m]), 0.78,   # 实测 0.82
                                "死区下调 0.10pp 的一致率应显著高于 v1 的 0.759")

    def test_cpi_window_perturbation(self):
        base = self._labels()
        for w, floor in ((7, 0.85), (9, 0.82)):
            alt = self._labels(CLOCK_CPI_WINDOW=w)
            m = np.isfinite(base) & np.isfinite(alt)
            self.assertGreaterEqual(np.mean(base[m] == alt[m]), floor,
                                    f"窗口 {w} 月标签一致率过低（v1 的 6→9 仅 0.766）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
