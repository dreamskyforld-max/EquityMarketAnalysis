"""§5.1 宽度口径 v2.1.11 防回退测试（不连库）。

锁住 2026-11-05 阶段 1 / 2 整改：
  ① L1：宽度/非宽度口径版本拆分（宽度升级不得波及其余 112 个指标）；
  ② L1：`min_periods=250` 只作用于宽度族，`extra.n_sample` 落库；
  ③ L1：分位计算收敛到 `_compute_percentiles` 单一真相源；
  ④ L2：`flags` 为 §2.3.3 的 dict 形状 + 可用性标记；
  ⑤ L2：`contrib_sign_split` 按 delta 符号分组、`quality_sample` 短样本降置信；
  ⑥ 重算脚本：LIMIT_NET 制度起点护栏。
"""
import datetime
import inspect
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_dimension_value as bd
import market_state_daily as msd
import calibrate_breadth as cb
import recalc_breadth_percentile as rp

D = datetime.date
DAY = D(2026, 1, 5)


class TestL1VersionSplit(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(msd.BREADTH_CALIB_VERSION, "v2.1.11")
        self.assertEqual(msd.EXPANDING_CALIB_VERSION, "v2.1.7")
        self.assertEqual(msd.BREADTH_PCT_MIN_PERIODS, 250)
        self.assertEqual(msd.BREADTH_PCT_WINDOW, 2520)

    def test_width_version_does_not_leak_to_expanding(self):
        """关键：递增宽度版本不能让其余指标的 params_hash / model_version 集体漂移。"""
        meta = dict(code="X", unit="u", direction="high_good", formula="f")
        exp_base = msd._params_hash(meta, False)
        roll_base = msd._params_hash(meta, True)
        with mock.patch.object(msd, "BREADTH_CALIB_VERSION", "v9"):
            self.assertEqual(msd._params_hash(meta, False), exp_base)
            self.assertNotEqual(msd._params_hash(meta, True), roll_base)


class TestMinPeriods(unittest.TestCase):
    def test_default_is_backwards_compatible(self):
        s = pd.Series([3.0, 1.0, 2.0, 5.0, 4.0])
        pd.testing.assert_series_equal(
            msd._expanding_percentile(s), msd._expanding_percentile(s, min_periods=0))

    def test_short_sample_suppressed(self):
        s = pd.Series(np.arange(300, dtype=float))
        got = msd._expanding_percentile(s, window=2520, min_periods=250)
        self.assertTrue(got.iloc[:249].isna().all())
        self.assertFalse(pd.isna(got.iloc[249]))

    def test_nan_does_not_consume_quota(self):
        s = pd.Series([1.0] * 249 + [np.nan] * 10 + [2.0])
        got = msd._expanding_percentile(s, min_periods=250)
        self.assertTrue(got.iloc[:-1].isna().all())
        self.assertFalse(pd.isna(got.iloc[-1]))

    def test_compute_percentiles_only_applies_to_breadth(self):
        idx = [D(2000, 1, 1) + datetime.timedelta(days=i) for i in range(300)]
        combined = pd.DataFrame({"BREADTH.ADV_RATIO": np.arange(300, dtype=float),
                                 "VAL.PE_TTM_MEDIAN": np.arange(300, dtype=float)}, index=idx)
        pct, ns = msd._compute_percentiles(combined)
        self.assertTrue(pct["BREADTH.ADV_RATIO"].iloc[:249].isna().all())
        self.assertTrue(pct["VAL.PE_TTM_MEDIAN"].iloc[:5].notna().all())
        self.assertEqual(int(ns["BREADTH.ADV_RATIO"].iloc[-1]), 300)

    def test_write_market_uses_single_source(self):
        self.assertIn("_compute_percentiles(combined)", inspect.getsource(msd._write_market))


class TestExtraNSample(unittest.TestCase):
    def test_written_when_supplied(self):
        ex = msd._breadth_extra("BREADTH.ADV_RATIO", DAY, None, n_sample=2143)
        self.assertEqual(ex["n_sample"], 2143)
        self.assertTrue(ex["survivorship_bias"])

    def test_absent_by_default(self):
        ex = msd._breadth_extra("BREADTH.ADV_RATIO", DAY, None) or {}
        self.assertNotIn("n_sample", ex)


class TestLimitGuard(unittest.TestCase):
    def test_residue_is_masked(self):
        wide = pd.DataFrame(
            {"BREADTH.LIMIT_NET": [1.0, 2.0, 3.0]},
            index=[D(1990, 1, 1), D(1996, 12, 15), D(1996, 12, 16)])
        n = rp._apply_limit_guard(wide, "CN")
        self.assertEqual(n, 2)
        self.assertTrue(pd.isna(wide.loc[D(1990, 1, 1), "BREADTH.LIMIT_NET"]))
        self.assertEqual(wide.loc[D(1996, 12, 16), "BREADTH.LIMIT_NET"], 3.0)


DIM = {
    "members": [
        dict(code="A", sign=1, block="g1", representative=True, flags_only=False),
        dict(code="B", sign=1, block="g1", representative=False, flags_only=False),
        dict(code="C", sign=1, block="g2", representative=True, flags_only=False),
    ],
    "state_bands": {"CN": [20, 40, 60, 80]},
    "state_labels": ["a", "b", "c", "d", "e"],
    "params": {"coverage_min": 0.6, "dispersion_max": 30, "weight_cap": 0.25,
               "rep_coverage_min": 1.0, "quality_sample": 750,
               "contrib_sign_split": True},
}


def _run(values, params=None, n_sample=None):
    pct = pd.DataFrame([
        {"trade_date": DAY, "indicator_code": c, "percentile": float(v)}
        for c, v in zip(("A", "B", "C"), values)
    ])
    scopes = pd.DataFrame({"indicator_code": ["A", "B", "C"],
                           "market_scope": ["CN+HK"] * 3})
    ns_long = pd.DataFrame()
    if n_sample is not None:
        ns_long = pd.DataFrame({"trade_date": [DAY] * 3,
                                "indicator_code": ["A", "B", "C"],
                                "n_sample": [n_sample] * 3})
    dim = dict(DIM)
    dim["params"] = dict(DIM["params"])
    if params:
        dim["params"].update(params)

    def fake(sql, conn, params=None):
        if "indicator_dict" in sql:
            return scopes.copy()
        if "n_sample" in sql:
            return ns_long.copy()
        return pct.copy()

    cap = {}
    with mock.patch.object(bd, "_read_sql", side_effect=fake), \
         mock.patch.object(bd, "bulk_upsert",
                           side_effect=lambda c, t, rows, **kw: cap.update(rows=rows)):
        bd.build_breadth_dimension(None, dim, "ph", "CN", DAY, DAY, True)
    return cap["rows"][0]


class TestL2FlagsDict(unittest.TestCase):
    def test_shape_is_dict_with_structural(self):
        row = _run([50, 50, 50], n_sample=1000)
        self.assertIsInstance(row["flags"], dict)
        self.assertIn("structural", row["flags"])
        self.assertIsInstance(row["flags"]["structural"], list)
        self.assertEqual(row["flags"]["n_sample"], 1000)

    def test_high_dispersion_is_flagged(self):
        row = _run([100, 0, 100], n_sample=1000)
        self.assertIsNone(row["state"])
        self.assertTrue(row["flags"]["high_dispersion"])
        self.assertEqual(row["confidence"], "低")

    def test_short_sample_lowers_confidence_only(self):
        row = _run([50, 50, 50], n_sample=100)
        self.assertTrue(row["flags"]["short_sample"])
        self.assertEqual(row["confidence"], "低")
        self.assertIsNotNone(row["state"])       # 规则 6：只降置信，不改 state / score
        self.assertAlmostEqual(float(row["score"]), 50.0, places=2)

    def test_quality_sample_off_by_default(self):
        row = _run([50, 50, 50], params={"quality_sample": 0}, n_sample=100)
        self.assertNotIn("short_sample", row["flags"])
        self.assertEqual(row["confidence"], "高")

    def test_contrib_sign_split(self):
        row = _run([80, 80, 80], n_sample=1000)
        self.assertEqual(row["contrib"]["pos"], [])
        self.assertEqual(row["contrib"]["neg"], [])

    def test_contrib_legacy_when_disabled(self):
        row = _run([80, 80, 80], params={"contrib_sign_split": False}, n_sample=1000)
        self.assertTrue(row["contrib"]["pos"])


class TestRecalcSafety(unittest.TestCase):
    def test_nsample_sql_placeholders_match_params(self):
        """回归：SQL 里 6 个 %s 曾只传 5 个参数，pandas/psycopg2 报 IndexError。"""
        cap = {}

        def fake(sql, conn, params=None):
            cap.update(sql=sql, params=params)
            return pd.DataFrame()

        with mock.patch.object(bd, "_read_sql", side_effect=fake):
            bd._load_n_samples(None, "CN", ["A"], DAY, DAY)
        self.assertEqual(cap["sql"].count("%s"), len(cap["params"]))

    def test_short_sample_uses_representatives(self):
        """回归：用全体成员取 min 会被晚起点的 flags_only 诊断成员拖成短样本。"""
        self.assertIn("columns=reps", inspect.getsource(bd.build_breadth_dimension))

    def test_backfill_purges_stale_rows(self):
        """回归：全量重算必须删掉不再产出的旧口径行（UPSERT 不会删行）。"""
        self.assertIn("_purge_stale_dimension_rows(",
                      inspect.getsource(bd.build_all_dimensions))


DIM_C = {
    "members": [
        dict(code="A", sign=1, block="g1", representative=True, flags_only=False),
        dict(code="B", sign=1, block="g1", representative=False, flags_only=False),
        dict(code="C", sign=1, block="g2", representative=True, flags_only=False),
        dict(code="D", sign=1, block="诊断", representative=False, flags_only=True),
        dict(code="E", sign=1, block="诊断", representative=False, flags_only=True),
    ],
    "state_bands": {"CN": [20, 40, 60, 80]},
    "state_labels": ["a", "b", "c", "d", "e"],
    "params": {"coverage_min": 0.6, "dispersion_max": 30, "weight_cap": 0.25,
               "rep_coverage_min": 1.0, "quality_sample": 0, "contrib_sign_split": True},
}


def _run_scope(scope=None):
    """代表族一致(90)，两个 flags_only 诊断成员极端(0) —— 能把两种 dispersion 口径分开。"""
    values = {"A": 90.0, "B": 90.0, "C": 90.0, "D": 0.0, "E": 0.0}
    pct = pd.DataFrame([
        {"trade_date": DAY, "indicator_code": c, "percentile": v}
        for c, v in values.items()])
    scopes = pd.DataFrame({"indicator_code": list(values),
                           "market_scope": ["CN+HK"] * len(values)})
    dim = dict(DIM_C)
    dim["params"] = dict(DIM_C["params"])
    if scope is not None:
        dim["params"]["dispersion_scope"] = scope

    def fake(sql, conn, params=None):
        if "indicator_dict" in sql:
            return scopes.copy()
        if "n_sample" in sql:
            return pd.DataFrame(columns=["trade_date", "indicator_code", "n_sample"])
        return pct.copy()

    cap = {}
    with mock.patch.object(bd, "_read_sql", side_effect=fake), \
         mock.patch.object(bd, "bulk_upsert",
                           side_effect=lambda c, t, rows, **kw: cap.update(rows=rows)):
        bd.build_breadth_dimension(None, dim, "ph", "CN", DAY, DAY, True)
    return cap["rows"][0]


class TestDispersionScopeC(unittest.TestCase):
    def test_default_scope_keeps_legacy_dispersion(self):
        row = _run_scope()
        self.assertAlmostEqual(float(row["dispersion"]), 49.30, places=1)
        self.assertTrue(row["flags"]["high_dispersion"])
        self.assertIsNone(row["state"])

    def test_non_flags_only_scope_enables_c(self):
        row = _run_scope("non_flags_only")
        self.assertAlmostEqual(float(row["dispersion"]), 0.0, places=2)
        self.assertEqual(row["flags"]["dispersion_norm"], 0.0)
        self.assertNotIn("high_dispersion", row["flags"])
        self.assertIsNotNone(row["state"])
        self.assertAlmostEqual(float(row["score"]), 90.0, places=2)  # score 不变

    def test_score_is_identical_across_scopes(self):
        self.assertEqual(_run_scope()["score"], _run_scope("non_flags_only")["score"])


class TestIsFinalRemoved(unittest.TestCase):
    def test_producers_do_not_write_is_final(self):
        src = (inspect.getsource(bd.build_breadth_dimension)
               + inspect.getsource(bd.build_valuation_dimension))
        self.assertNotIn('"is_final"', src)

    def test_schema_sources_have_no_is_final(self):
        for rel in ("sql/schema.sql", "regime_schema.py"):
            with self.subTest(rel=rel):
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    self.assertNotIn("is_final", f.read())


class TestParamRegistration(unittest.TestCase):
    def test_updates_registered(self):
        self.assertEqual(cb.PARAM_UPDATES["flags_shape"], "dict")
        self.assertTrue(cb.PARAM_UPDATES["contrib_sign_split"])
        self.assertEqual(cb.PARAM_UPDATES["quality_sample"], 750)
        self.assertEqual(cb.PARAM_UPDATES["min_periods"], 250)
        self.assertEqual(cb.PARAM_UPDATES["calib_version"], "v2.1.12")
        self.assertEqual(cb.PARAM_UPDATES["dispersion_scope"], "non_flags_only")
        self.assertIn("flag_gap", cb.PARAM_UPDATES)
        self.assertIn("flag_extreme", cb.PARAM_UPDATES)

    def test_flags_remark_pinned(self):
        self.assertIn("structural", cb.FLAGS_REMARK)
        self.assertIn("不得按 list 遍历", cb.FLAGS_REMARK)


if __name__ == "__main__":
    unittest.main()
