"""第④层「行业配置」阶段 2 测试：成分 PIT、聚合口径、工程接线。

纯函数 / 静态文本断言为主，任何环境都能跑；需要真实库的契约另见
tests/test_sector_layer_db.py（本文件不连库）。

背景：doc/industry_allocation_feasibility.md §2.2.1 / §4.2 阶段 2。
"""
import datetime as dt
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import get_sector_constituent as cons  # noqa: E402
import sector_layer as sl  # noqa: E402
import sector_stage2_agg as ag  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestCodeConversion(unittest.TestCase):
    """6 位纯数字 → 项目代码；必须含 BJ 段且保住前导零"""

    def test_prefixes(self):
        cases = {"000505": "SZ.000505", "300750": "SZ.300750",
                 "600000": "SH.600000", "688981": "SH.688981",
                 "920001": "BJ.920001", "830001": "BJ.830001",
                 "430001": "BJ.430001"}
        for raw, expect in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(cons.to_stock_code(raw), expect)

    def test_zfill_and_numeric_coercion(self):
        self.assertEqual(cons.to_stock_code(505), "SZ.000505")
        self.assertEqual(cons.to_stock_code("505"), "SZ.000505")
        self.assertEqual(cons.to_stock_code("505.0"), "SZ.000505")

    def test_invalid(self):
        for bad in (None, "", "abc", "SH.600000", "12345678"):
            with self.subTest(bad=bad):
                self.assertIsNone(cons.to_stock_code(bad))


class TestDiffSnapshot(unittest.TestCase):
    """PIT 版本链 diff（纯函数）"""

    SNAP = dt.date(2026, 11, 13)
    EFF = dt.date(2021, 12, 13)

    def _row(self, sec, stock, w=1.0):
        return dict(sector_code=sec, stock_code=stock, eff_from=self.EFF, weight=w)

    def test_first_snapshot_all_add(self):
        d = cons.diff_snapshot({}, [self._row("A", "SZ.000001")], self.SNAP)
        self.assertEqual(len(d["add"]), 1)
        self.assertEqual(d["add"][0]["change_type"], "ADD")
        self.assertEqual(d["add"][0]["eff_from"], self.EFF)
        self.assertEqual(d["remove"], [])

    def test_remove(self):
        active = {("A", "SZ.000001"): (self.EFF, 1.0)}
        d = cons.diff_snapshot(active, [], self.SNAP)
        self.assertEqual(len(d["remove"]), 1)
        self.assertEqual(d["remove"][0]["change_type"], "REMOVE")

    def test_weight_significant_vs_tiny(self):
        active = {("A", "SZ.000001"): (self.EFF, 1.0)}
        d = cons.diff_snapshot(active, [self._row("A", "SZ.000001", 1.005)], self.SNAP)
        self.assertEqual(d["weight"], [])
        self.assertEqual(len(d["tiny"]), 1)
        d2 = cons.diff_snapshot(active, [self._row("A", "SZ.000001", 1.05)], self.SNAP)
        self.assertEqual(len(d2["weight"]), 1)
        self.assertEqual(d2["weight"][0]["change_type"], "WEIGHT")

    def test_idempotent_rerun(self):
        rows = [self._row("A", "SZ.000001", 1.0), self._row("A", "SZ.000002", 2.0)]
        active = {(r["sector_code"], r["stock_code"]): (r["eff_from"], r["weight"])
                  for r in rows}
        d = cons.diff_snapshot(active, rows, self.SNAP)
        self.assertEqual(d["add"], [])
        self.assertEqual(d["remove"], [])
        self.assertEqual(d["weight"], [])
        self.assertEqual(d["tiny"], [])

    def test_pit_start_is_2021_12_13(self):
        self.assertEqual(cons.PIT_START, dt.date(2021, 12, 13))
        self.assertEqual(ag.PIT_START, cons.PIT_START)
        self.assertEqual(sl.PIT_START, cons.PIT_START)


class TestPitJoin(unittest.TestCase):
    """成分 PIT 区间过滤（纯函数）"""

    def test_interval_filter(self):
        cmap = pd.DataFrame({
            "sector_code": ["A", "A", "B"],
            "stock_code": ["SZ.1", "SZ.2", "SZ.1"],
            "eff_from": pd.to_datetime(["2021-12-13", "2021-12-13", "2024-01-01"]),
            "eff_to": pd.to_datetime(["2023-12-31", None, None]),
            "weight": [1.0, 1.0, 1.0],
        })
        q = pd.DataFrame({
            "stock_code": ["SZ.1", "SZ.1", "SZ.2", "SZ.1"],
            "trade_date": ["2022-06-01", "2024-06-01", "2025-01-01", "2023-12-31"],
            "close": [1.0, 2.0, 3.0, 4.0],
        })
        out = ag.pit_join(q, cmap)
        self.assertEqual(len(out), 3)
        got = {(r.stock_code, str(r.trade_date.date()), r.sector_code)
               for r in out.itertuples()}
        self.assertIn(("SZ.1", "2022-06-01", "A"), got)
        self.assertNotIn(("SZ.1", "2024-06-01", "A"), got)   # A 区间已关闭
        self.assertIn(("SZ.1", "2024-06-01", "B"), got)

    def test_closed_interval_end_exclusive(self):
        """eff_to 是开区间边界：trade_date == eff_to 时旧版本不再有效。"""
        cmap = pd.DataFrame({
            "sector_code": ["A"], "stock_code": ["SZ.1"],
            "eff_from": pd.to_datetime(["2021-12-13"]),
            "eff_to": pd.to_datetime(["2024-01-01"]), "weight": [1.0],
        })
        q = pd.DataFrame({"stock_code": ["SZ.1", "SZ.1"],
                          "trade_date": ["2023-12-31", "2024-01-01"],
                          "close": [1.0, 2.0]})
        out = ag.pit_join(q, cmap)
        self.assertEqual(len(out), 1)
        self.assertEqual(str(out.iloc[0]["trade_date"].date()), "2023-12-31")


class TestSufficientStatistics(unittest.TestCase):
    """按月 × 按股票分批的充分统计量合并，必须与整批计算逐值相等。"""

    @classmethod
    def setUpClass(cls):
        dates = pd.date_range("2026-01-01", periods=80, freq="B")
        rows = []
        rng = np.random.default_rng(7)
        for code in ["SZ.000001", "SZ.000002", "SH.600000", "SH.600001"]:
            base = 10 + rng.random() * 5
            for i, d in enumerate(dates):
                close = base + i * 0.05 + rng.normal(0, 0.2)
                rows.append(dict(
                    trade_date=d.date(), stock_code=code, sector_code="A",
                    close=close, change_pct=float(rng.normal(0, 2)),
                    turnover_rate=float(abs(rng.normal(2, 0.5))),
                    circular_market_val=1e10 + rng.random() * 1e9,
                    pe_ttm_ratio=float(abs(rng.normal(20, 5)))))
        cls.q = pd.DataFrame(rows)
        cls.ms, cls.me = dt.date(2026, 4, 1), dt.date(2026, 4, 30)

    def test_partial_merge_equals_single_batch(self):
        whole = ag._finalize_quote(ag._quote_partial(self.q, self.ms, self.me))
        parts = [ag._quote_partial(g, self.ms, self.me)
                 for _, g in self.q.groupby("stock_code")]
        merged = (pd.concat(parts, ignore_index=True)
                    .groupby(["trade_date", "sector_code"], as_index=False)[ag._SS_COLS].sum())
        split = ag._finalize_quote(merged)
        self.assertEqual(whole.shape, split.shape)
        for c in ("up_ratio", "above_ma60", "new_high_low", "ret_dispersion",
                  "pe_dispersion", "turnover_w", "breadth_coverage", "constituent_n"):
            with self.subTest(col=c):
                self.assertTrue(np.allclose(whole[c].to_numpy(), split[c].to_numpy(),
                                           equal_nan=True, rtol=1e-9, atol=1e-9), c)

    def test_variance_non_negative(self):
        out = ag._finalize_quote(ag._quote_partial(self.q, self.ms, self.me))
        self.assertTrue((out["ret_dispersion"].dropna() >= 0).all())
        self.assertTrue((out["pe_dispersion"].dropna() >= 0).all())

    def test_warmup_rows_excluded(self):
        out = ag._quote_partial(self.q, self.ms, self.me)
        self.assertTrue((pd.to_datetime(out["trade_date"]).dt.date >= self.ms).all())
        self.assertTrue((pd.to_datetime(out["trade_date"]).dt.date <= self.me).all())

    def test_ratio_ranges(self):
        out = ag._finalize_quote(ag._quote_partial(self.q, self.ms, self.me))
        self.assertTrue(((out["up_ratio"] >= 0) & (out["up_ratio"] <= 100)).all())
        self.assertTrue(((out["above_ma60"] >= 0) & (out["above_ma60"] <= 100)).all())
        self.assertTrue(((out["breadth_coverage"] >= 0) &
                         (out["breadth_coverage"] <= 1)).all())


class TestStage2FrameWiring(unittest.TestCase):
    """阶段 2 聚合接入 _compute_frame 后的口径"""

    @classmethod
    def setUpClass(cls):
        codes = [f"8010{i:02d}" for i in range(12)]
        dates = pd.date_range("2026-11-02", periods=8, freq="B")
        rows = []
        for j, d in enumerate(dates):
            for i, c in enumerate(codes):
                rows.append(dict(trade_date=d.date(), sw_code=c, sw_name=f"行业{i}",
                                 close=100 + i + j * 0.1,
                                 ret_20d=float(i) - 5.5, ret_60d=float(i) - 5.5,
                                 rs_20d=float(i) - 5.5, rs_rank=float(12 - i),
                                 mom_rank=float(12 - i), turnover=100.0 + i,
                                 fund_flow_net=float(i) - 5.5,
                                 fund_flow_net_5d=float(i), flow_rank=float(12 - i),
                                 mapped_ths_count=1))
        cls.raw = pd.DataFrame(rows)
        agg_rows = []
        for d in dates:
            for i, c in enumerate(codes):
                agg_rows.append(dict(trade_date=d.date(), sw_code=c,
                                     turnover_w=1.0 + i * 0.1,
                                     pe_dispersion=10.0 + i,
                                     up_ratio=20.0 + i, above_ma60=30.0 + i,
                                     new_high_low=float(i) - 5.5,
                                     ret_dispersion=0.1 + i / 100,
                                     roe_median=5.0 + i, np_yoy_median=float(i),
                                     rev_yoy_median=float(i),
                                     improvement_breadth=40.0 + i,
                                     revision_breadth=float(i) - 5.5,
                                     constituent_n=100, breadth_coverage=1.0,
                                     margin_net=float(i), fund_coverage=0.9,
                                     rating_bull_pct=99.0, revision_n=25,
                                     forecast_coverage=0.5))
        cls.agg = pd.DataFrame(agg_rows)
        cls.val = pd.DataFrame([dict(trade_date=d.date(), sw_code=c,
                                     pe_ttm=10.0 + i, pb=1.0 + i / 10,
                                     div_yield=2.0)
                                for d in dates for i, c in enumerate(codes)])
        cls.amt = pd.DataFrame([dict(trade_date=d.date(), sw_code=c,
                                     amt_share=1 / 12, amt_share_pct=50.0 + i,
                                     amt_share_n=1000)
                                for d in dates for i, c in enumerate(codes)])
        cls.wide = sl._compute_frame(cls.raw.copy(), cls.val, cls.amt, cls.agg)

    def test_three_crowding_factors(self):
        ks = [c["k"] for c in self.wide["_crowd"]]
        self.assertTrue(all(k == 3 for k in ks))
        self.assertTrue(all(not c["partial"] for c in self.wide["_crowd"]))

    def test_extreme_gate_reachable(self):
        # 阶段 2 k=3 才开放 EXTREME（阶段 1 k=2 部分模式最高只到 HIGH）
        self.assertTrue(set(self.wide["crowd_gate_code"]).issubset(
            set(sl.CROWD_GATE_CODES.values())))
        self.assertEqual(
            sl.crowd_gate({"A": 95.0, "B": 91.0, "C": 88.0})["gate"], "EXTREME")
        self.assertEqual(
            sl.crowd_gate({"A": 95.0, "B": 91.0})["gate"], "HIGH")

    def test_six_dimensions_present(self):
        for name in sl.ENABLED_DIMENSIONS:
            self.assertIn("dim_" + name, self.wide.columns)

    def test_equal_dimensions_have_scores(self):
        for name in ("FUNDAMENTAL", "BREADTH"):
            with self.subTest(dim=name):
                self.assertTrue(self.wide["dim_" + name].notna().all())

    def test_state_side_uses_four_groups(self):
        self.assertEqual(int(self.wide["state_n"].max()), 4)
        self.assertFalse(self.wide["state_side"].isna().all())

    def test_coverage_denominator_is_six(self):
        self.assertLessEqual(float(self.wide["coverage"].max()), 1.0)
        # 六维全有值 → coverage = 1.0
        self.assertAlmostEqual(float(self.wide["coverage"].max()), 1.0, places=6)

    def test_no_partial_state_flag(self):
        rows = sl._biz_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertTrue(all("PARTIAL_STATE" not in r["gate_flags"] for r in rows))

    def test_stage2_indicator_rows_emitted(self):
        rows = sl._indicator_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        codes = {r["indicator_code"] for r in rows}
        for c in ("SEC.CROWD.TURNOVER_PCT", "SEC.BREADTH.UP_RATIO",
                  "SEC.FUND.REVISION_BREADTH", "SEC.FLOW.MARGIN_NET"):
            self.assertIn(c, codes)

    def test_pit_capable_flag_for_stage2(self):
        rows = sl._indicator_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        stage2 = [r for r in rows if "sector_constituent" in
                  next(d["source"] for d in sl.ACTIVE_DEFS
                       if d["code"] == r["indicator_code"])]
        self.assertTrue(all(r["extra"]["pit_capable"] for r in stage2))


class TestEngineeringGuards(unittest.TestCase):
    """双同步 / 调度 / 监控 / 资源红线，防止阶段 2 接线静默回退"""

    def test_ddl_dual_source(self):
        schema = _read("sql/schema.sql")
        code = _read("regime_schema.py")
        for tbl in ("regime.sector_constituent", "regime.sector_constituent_change"):
            with self.subTest(tbl=tbl):
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, schema)
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, code)
        self.assertIn("eff_from", schema)
        self.assertIn("_SECTOR_DDL_KEYS", code)
        self.assertIn("sector_constituent", code)

    def test_scheduler_registration(self):
        src = _read("market_scheduler.py")
        self.assertIn('"get_sector_constituent.py"', src)
        self.assertIn('"hour": 18, "minute": 5', src)
        # 计算层仍禁止挂进调度器（R4/R5）
        self.assertNotIn('"sector_layer.py"', src)

    def test_monitor_registration(self):
        src = _read("monitor_collector.py")
        for tbl in ("sector_constituent", "sector_constituent_change"):
            with self.subTest(tbl=tbl):
                self.assertIn('("regime", "' + tbl + '"', src)
                self.assertIn('("regime", "' + tbl + '")', src)

    def test_resource_guards(self):
        src = _read("sector_stage2_agg.py")
        self.assertIn("BATCH_CODES", src)
        self.assertIn("for i in range(0, len(codes), BATCH_CODES)", src)
        self.assertIn("_read_sql_stream(", src)
        self.assertIn("STREAM_CHUNK", src)
        self.assertIn("gc.collect()", src)
        self.assertGreaterEqual(ag.BATCH_CODES, 100)
        self.assertLessEqual(ag.STREAM_CHUNK, 50_000)

    def test_sql_has_lower_bounds(self):
        src = _read("sector_stage2_agg.py")
        self.assertIn("BETWEEN %s AND %s", src)
        self.assertIn("stock_code = ANY(%s)", src)

    def test_write_batch_unchanged(self):
        self.assertLessEqual(sl.WRITE_BATCH, 50_000)

    def test_calib_break(self):
        self.assertEqual(sl.CALIB_VERSION, "s2.1")
        # 口径版本进入 params_hash，阶段1/2 指纹必然不同
        d = sl.ACTIVE_DEFS[0]
        h2 = sl._params_hash(d)
        self.assertNotEqual(h2, "")
        self.assertEqual(len(h2), 16)


if __name__ == "__main__":
    unittest.main()
