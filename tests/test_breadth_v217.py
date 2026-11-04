"""§5.1 市场宽度 v2.1.7 口径整改回归测试（不连库）。

锁住七件事，任何一条被改坏都说明宽度读数回到了评审前的状态：
  ① 归一化净广度的有界性 / 分母为 0 / **规模不变性**（废弃绝对家数差的核心理由）
  ② ratio-adjusted McClellan 基于归一化净广度、同样规模不变
  ③ 分位函数 window=None 与旧行为逐值一致、window=N 会遗忘窗口外旧值、NaN 不占窗口
  ④ 口径指纹稳定且对公式 / 窗口 / 口径版本敏感
  ⑤ indicator_dict 写入 domain / params_hash / breaks（编码前缀 ≠ 维度归属）
  ⑥ indicator_value.extra 带幸存者偏差标注与原始家数
  ⑦ LIMIT_NET 制度起点与宽度滚动集合的边界

背景：doc/market_profile.md §5.1 v2.1.7 评审整改。旧口径 adv−dec 是绝对家数差，
CN 股票池 1990→2026 扩 2~3 个数量级 → 累计线与分位读成「年份」的函数。
"""
import bisect
import datetime
import inspect
import os
import re
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import market_state_daily as msd

D = datetime.date


class TestAdvNet(unittest.TestCase):
    """归一化净广度 A = 100×(adv−dec)/(adv+dec)。"""

    def test_bounds_and_symmetry(self):
        a = msd._adv_net(pd.Series([100.0, 0.0, 50.0]),
                         pd.Series([0.0, 100.0, 50.0]))
        self.assertAlmostEqual(a.iloc[0], 100.0)
        self.assertAlmostEqual(a.iloc[1], -100.0)
        self.assertAlmostEqual(a.iloc[2], 0.0)
        self.assertTrue(((a >= -100) & (a <= 100)).all())

    def test_zero_denominator_is_nan(self):
        """全平盘 / 无成交日不能除零，也不能记成 0（0 会被当成「多空均衡」）。"""
        a = msd._adv_net(pd.Series([0.0]), pd.Series([0.0]))
        self.assertTrue(pd.isna(a.iloc[0]))

    def test_scale_invariance(self):
        """核心性质：池规模 ×100 而涨跌比例不变 → 读数逐值不变。"""
        adv = pd.Series([30.0, 12.0, 55.0])
        dec = pd.Series([20.0, 28.0, 5.0])
        base = msd._adv_net(adv, dec)
        scaled = msd._adv_net(adv * 100, dec * 100)
        pd.testing.assert_series_equal(base, scaled)
        # 反证：旧口径会随规模线性漂移（这正是被废弃的原因）
        old = adv - dec
        self.assertAlmostEqual(float(((adv * 100 - dec * 100) - old * 100).abs().sum()), 0.0)
        self.assertGreater(float((old * 100).abs().sum()), float(old.abs().sum()))

    def test_unchanged_stocks_excluded_from_denominator(self):
        """平盘股不进分母：与 adv_ratio 用 n 作分母是两套口径，勿混。"""
        self.assertAlmostEqual(
            float(msd._adv_net(pd.Series([30.0]), pd.Series([20.0])).iloc[0]), 20.0)


class TestMcclellan(unittest.TestCase):
    def test_scale_invariant(self):
        adv = pd.Series(np.linspace(20, 80, 120))
        dec = pd.Series(np.linspace(80, 20, 120))
        pd.testing.assert_series_equal(
            msd._mcclellan(msd._adv_net(adv, dec)),
            msd._mcclellan(msd._adv_net(adv * 50, dec * 50)))

    def test_constant_series_is_zero(self):
        a = pd.Series([1.0] * 60)
        self.assertTrue((msd._mcclellan(a).abs() < 1e-12).all())

    def test_nan_days_do_not_break_ema(self):
        a = pd.Series([1.0, np.nan, 3.0, 4.0, np.nan, 6.0])
        out = msd._mcclellan(a)
        self.assertEqual(len(out), len(a))
        self.assertFalse(out.isna().all())


class TestRollingPercentile(unittest.TestCase):
    def test_window_none_matches_legacy_formula(self):
        """window 缺省必须与整改前逐值一致（其余维度仍用 expanding）。"""
        s = pd.Series([3.0, 1.0, 2.0, 5.0, 4.0, np.nan, 0.0])
        vals, buf, expect = s.to_numpy(float), [], []
        for v in vals:
            if np.isnan(v):
                expect.append(np.nan)
                continue
            lo, hi = bisect.bisect_left(buf, v), bisect.bisect_right(buf, v)
            expect.append(100.0 * ((lo + hi) / 2.0 + 0.5) / (len(buf) + 1))
            bisect.insort(buf, v)
        pd.testing.assert_series_equal(
            msd._expanding_percentile(s), pd.Series(expect, index=s.index))

    def test_window_larger_than_sample_equals_expanding(self):
        s = pd.Series([3.0, 1.0, 2.0, 5.0, 4.0, 0.0])
        pd.testing.assert_series_equal(
            msd._expanding_percentile(s, window=10_000),
            msd._expanding_percentile(s))

    def test_rolling_forgets_old_extreme(self):
        """窗口外的极端旧值必须被遗忘 —— 宽度改滚动 10 年的直接理由。"""
        s = pd.Series([100.0] * 10 + [1.0] * 10 + [2.0] * 10)
        exp = msd._expanding_percentile(s)
        roll = msd._expanding_percentile(s, window=10)
        self.assertGreater(roll.iloc[20], exp.iloc[20] + 30)
        self.assertTrue(((roll.dropna() >= 0) & (roll.dropna() <= 100)).all())

    def test_nan_days_do_not_consume_window_quota(self):
        s = pd.Series([100.0] * 10 + [np.nan] * 50 + [1.0] * 10 + [2.0] * 10)
        roll = msd._expanding_percentile(s, window=10)
        self.assertGreater(roll.iloc[-10], 90)

    def test_no_lookahead(self):
        """前缀分位不受后续观测影响（PIT 铁律）。"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        a = msd._expanding_percentile(s.iloc[:3], window=2)
        b = msd._expanding_percentile(s, window=2).iloc[:3]
        pd.testing.assert_series_equal(a.reset_index(drop=True), b.reset_index(drop=True))


class TestParamsHash(unittest.TestCase):
    def test_stable_and_sensitive(self):
        meta = dict(code="BREADTH.ADV_RATIO", unit="pct",
                    direction="high_good", formula="f")
        h = msd._params_hash(meta, True)
        self.assertEqual(len(h), 16)
        self.assertEqual(h, msd._params_hash(dict(meta), True))
        self.assertNotEqual(h, msd._params_hash(meta, False))          # 分位窗口
        self.assertNotEqual(h, msd._params_hash(dict(meta, formula="g"), True))
        self.assertNotEqual(h, msd._params_hash(dict(meta, unit="count"), True))
        with mock.patch.object(msd, "BREADTH_CALIB_VERSION", "v9"):
            self.assertNotEqual(h, msd._params_hash(meta, True))

    def test_tolerates_missing_keys(self):
        self.assertEqual(len(msd._params_hash({"code": "X"}, False)), 16)


class TestIndicatorDict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(msd, "bulk_upsert") as up:
            msd._ensure_dict(object())
            cls.rows = {r["indicator_code"]: r for r in up.call_args.args[2]}

    def test_domain_is_source_of_truth(self):
        """BREADTH. 前缀 ≠ 宽度归属：AVG_CORR_VW 归压力、SECTOR.UP_RATIO 不进六维。"""
        self.assertEqual(self.rows["BREADTH.ADV_RATIO"]["domain"], "宽度")
        self.assertEqual(self.rows["BREADTH.AVG_CORR_VW"]["domain"], "压力")
        self.assertEqual(self.rows["BREADTH.AVG_CORR_VW"]["dimension"], "压力")
        self.assertIsNone(self.rows["SECTOR.UP_RATIO"]["domain"])

    def test_every_row_has_params_hash(self):
        self.assertTrue(all(len(r["params_hash"] or "") == 16 for r in self.rows.values()))

    def test_limit_net_breaks(self):
        br = self.rows["BREADTH.LIMIT_NET"]["breaks"]
        self.assertEqual(len(br), 3)
        self.assertEqual(br[0]["date"], "1996-12-16")

    def test_ad_and_mcc_formula_normalized(self):
        self.assertIn("adv−dec", self.rows["BREADTH.ADV_MINUS_DEC"]["formula"])
        self.assertIn("EMA19(A)", self.rows["BREADTH.MCCLELLAN"]["formula"])
        self.assertEqual(self.rows["BREADTH.ADV_MINUS_DEC"]["unit"], "pct")
        self.assertNotIn("count", self.rows["BREADTH.ADV_MINUS_DEC"]["unit"])

    def test_hk_start_remark_not_stale(self):
        for code in ("BREADTH.NEW_HIGH_LOW_20", "BREADTH.UP_DOWN_STRENGTH",
                     "BREADTH.AMT_CONC_TOP10", "BREADTH.AVG_CORR_VW"):
            self.assertNotIn("HK 2019 起", self.rows[code]["remark"])


class TestDictSchemaSelfHeal(unittest.TestCase):
    """2026-11-04 事故回归：目标库缺 v2.1+ 增量列 → 写字典时 UndefinedColumn 中断。

    事故形态：`python3 market_state_daily.py --backfill` 在 `_ensure_dict` 的
    bulk_upsert 处抛 `column "domain" of relation "indicator_dict" does not exist`，
    整轮回填失败。根因是 schema.sql 加了列、既有库没跑迁移；修复是写库前自愈。
    """

    V21_COLS = {
        "indicator_dict": ["domain", "role", "params_hash", "breaks"],
        "indicator_value": ["is_final", "model_version"],
    }

    def test_ddl_covers_v21_columns(self):
        for tbl, cols in self.V21_COLS.items():
            for col in cols:
                with self.subTest(f"{tbl}.{col}"):
                    self.assertRegex(
                        msd._DICT_SCHEMA_DDL,
                        re.compile(rf"ALTER TABLE regime\.{tbl}\s+"
                                   rf"ADD COLUMN IF NOT EXISTS {col}\b", re.I))

    def test_ddl_is_idempotent_and_additive_only(self):
        """自愈只允许 ADD COLUMN IF NOT EXISTS —— 出现 DROP/DELETE/UPDATE 即事故隐患。"""
        ddl = msd._DICT_SCHEMA_DDL.lower()
        self.assertIn("add column if not exists", ddl)
        for bad in ("drop ", "delete ", "truncate ", "update "):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, ddl)

    def test_heal_runs_before_dict_upsert(self):
        """接线回归：run() 必须先自愈、后 upsert（否则旧库仍在写字典处中断）。"""
        src = inspect.getsource(msd.run)
        self.assertIn("_ensure_dict_schema(conn)", src)
        self.assertLess(src.index("_ensure_dict_schema(conn)"),
                        src.index("_ensure_dict(conn)"))

    def test_written_dict_cols_declared_in_schema_sql(self):
        """代码写入的每一列都必须在 schema.sql 登记（防「代码加列、schema 漏写」）。"""
        path = os.path.join(ROOT, "sql", "schema.sql")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        block = re.search(
            r"CREATE TABLE IF NOT EXISTS regime\.indicator_dict \((.*?)\n\);", text, re.S)
        self.assertIsNotNone(block, "schema.sql 里应有 regime.indicator_dict 建表块")
        declared = set(re.findall(r"^\s{4}([a-z_][\w]*)\s+[A-Z]", block.group(1), re.M))
        declared |= {c.lower() for c in re.findall(
            r"ALTER TABLE regime\.indicator_dict\s+ADD COLUMN IF NOT EXISTS (\w+)", text, re.I)}
        for col in msd.DICT_COLS:
            with self.subTest(col=col):
                self.assertIn(col, declared)


class TestBreadthExtra(unittest.TestCase):
    def test_bias_flag_for_breadth_only(self):
        ex = msd._breadth_extra("BREADTH.ADV_RATIO", D(2026, 1, 5), None)
        self.assertTrue(ex["survivorship_bias"])
        self.assertEqual(ex["quality"], "low")
        self.assertIsNone(msd._breadth_extra("VAL.PE_TTM_MEDIAN", D(2026, 1, 5), None))

    def test_no_flag_when_pit_universe_ready(self):
        with mock.patch.object(msd, "PIT_UNIVERSE_READY", True):
            self.assertIsNone(msd._breadth_extra("BREADTH.ADV_RATIO", D(2026, 1, 5), None))

    def test_raw_counts_kept_in_extra(self):
        detail = pd.DataFrame(
            {"adv": [30, 40], "dec": [20, 10], "n": [55, 55], "unchanged": [5, 5]},
            index=[D(2026, 1, 5), D(2026, 1, 6)])
        ex = msd._breadth_extra("BREADTH.ADV_MINUS_DEC", D(2026, 1, 5), detail)
        self.assertEqual(ex["adv"], 30)
        self.assertEqual(ex["dec"], 20)
        self.assertEqual(ex["n"], 55)
        self.assertEqual(ex["unchanged"], 5)
        self.assertTrue(ex["survivorship_bias"])

    def test_extra_is_json_serializable(self):
        import json
        detail = pd.DataFrame(
            {"adv": [np.int64(30)], "dec": [np.int64(20)],
             "n": [np.int64(55)], "unchanged": [np.int64(5)]},
            index=[D(2026, 1, 5)])
        ex = msd._breadth_extra("BREADTH.ADV_MINUS_DEC", D(2026, 1, 5), detail)
        json.dumps(ex)              # numpy.int64 未转换会在此抛 TypeError

    def test_json_scalar(self):
        self.assertIsNone(msd._json_scalar(np.nan))
        self.assertIsNone(msd._json_scalar(None))
        self.assertEqual(msd._json_scalar(np.int64(7)), 7)
        self.assertIsInstance(msd._json_scalar(np.int64(7)), int)
        self.assertEqual(msd._json_scalar(np.float64(1.5)), 1.5)


class TestConstants(unittest.TestCase):
    def test_rolling_codes(self):
        self.assertEqual(len(msd.BREADTH_ROLLING_CODES), 11)
        self.assertIn("BREADTH.LIMIT_NET", msd.BREADTH_ROLLING_CODES)
        self.assertNotIn("BREADTH.AVG_CORR_VW", msd.BREADTH_ROLLING_CODES)
        self.assertEqual(msd.BREADTH_PCT_WINDOW, 2520)

    def test_limit_effective_from(self):
        self.assertEqual(msd.LIMIT_EFFECTIVE_FROM, D(1996, 12, 16))

    def test_pit_universe_not_ready_yet(self):
        """接含退市股全历史源、重建成分池之前，必须为 False。"""
        self.assertFalse(msd.PIT_UNIVERSE_READY)


if __name__ == "__main__":
    unittest.main()
