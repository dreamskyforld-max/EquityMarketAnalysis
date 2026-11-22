"""第④层「行业配置」阶段 2 测试：映射代码转换、聚合口径、工程接线。

纯函数 / 静态文本断言为主，任何环境都能跑；需要真实库的契约另见
tests/test_sector_layer_db.py（本文件不连库）。

⚠ D12/D14（2026-11-20）：成分 PIT 版本链已废除（industry.constituent →
  regime.stock_sw_industry_mapping），diff_snapshot 相关测试已删除；
  pit_join 语义改为"最新成分贴历史"。历史序列起点 PIT_START 仍保留。
"""
import datetime as dt
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import get_stock_sw_industry_mapping as swmap  # noqa: E402
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
                self.assertEqual(swmap.to_stock_code(raw), expect)

    def test_zfill_and_numeric_coercion(self):
        self.assertEqual(swmap.to_stock_code(505), "SZ.000505")
        self.assertEqual(swmap.to_stock_code("505"), "SZ.000505")
        self.assertEqual(swmap.to_stock_code("505.0"), "SZ.000505")

    def test_invalid(self):
        for bad in (None, "", "abc", "SH.600000", "12345678"):
            with self.subTest(bad=bad):
                self.assertIsNone(swmap.to_stock_code(bad))


class TestHistoryStart(unittest.TestCase):
    """历史序列起点（D14：成分无 PIT，但历史序列仍从 2021-12-13 起算）"""

    def test_history_start_is_2021_12_13(self):
        self.assertEqual(ag.PIT_START, dt.date(2021, 12, 13))
        self.assertEqual(sl.PIT_START, dt.date(2021, 12, 13))


class TestPitJoin(unittest.TestCase):
    """最新成分贴历史（D14：无 PIT 区间过滤，函数名保留兼容调用点）"""

    def test_latest_component_all_dates(self):
        """一只股票的最新映射贴到所有历史日期（无 eff 区间；接受幸存者偏差）。"""
        cmap = pd.DataFrame({
            "sector_code": ["A", "B", "A"],
            "stock_code": ["SZ.2", "SZ.1", "SZ.3"],   # 一行一股票（新表 PK 结构）
            "weight": [1.0, 1.0, 1.0],
        })
        q = pd.DataFrame({
            "stock_code": ["SZ.1", "SZ.1", "SZ.2", "SZ.1"],
            "trade_date": ["2022-06-01", "2024-06-01", "2025-01-01", "2023-12-31"],
            "close": [1.0, 2.0, 3.0, 4.0],
        })
        out = ag.pit_join(q, cmap)
        self.assertEqual(len(out), 4)   # 所有历史日期都贴最新成分，不再按区间过滤
        got = {(r.stock_code, str(r.trade_date.date()), r.sector_code)
               for r in out.itertuples()}
        self.assertIn(("SZ.1", "2022-06-01", "B"), got)   # 2022 年也贴最新的 B
        self.assertIn(("SZ.1", "2024-06-01", "B"), got)
        self.assertIn(("SZ.2", "2025-01-01", "A"), got)

    def test_no_interval_columns_needed(self):
        """cmap 无 eff_from/eff_to 列也能工作（新表结构）。"""
        cmap = pd.DataFrame({
            "sector_code": ["A"], "stock_code": ["SZ.1"], "weight": [1.0],
        })
        q = pd.DataFrame({"stock_code": ["SZ.1", "SZ.1"],
                          "trade_date": ["2023-12-31", "2024-01-01"],
                          "close": [1.0, 2.0]})
        out = ag.pit_join(q, cmap)
        self.assertEqual(len(out), 2)   # 两天都贴 A（最新成分）
        self.assertEqual(set(out["sector_code"]), {"A"})


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
        entities = {(r["sector_code"], r["trade_date"]) for r in rows}
        self.assertEqual(len(rows), len(entities) * 5)
        gates = [r for r in rows if r["biz_code"] == "SEC.BIZ.GATE"]
        self.assertTrue(all("PARTIAL_STATE" not in r["detail"]["gate_flags"] for r in gates))

    def test_stage2_indicator_rows_emitted(self):
        rows = sl._indicator_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        codes = {r["indicator_code"] for r in rows}
        for c in ("SEC.CROWD.TURNOVER_PCT", "SEC.BREADTH.UP_RATIO",
                  "SEC.FUND.REVISION_BREADTH", "SEC.FLOW.MARGIN_NET"):
            self.assertIn(c, codes)

    def test_pit_capable_flag_for_stage2(self):
        rows = sl._indicator_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        stage2 = [r for r in rows if "constituent" in
                  next(d["source"] for d in sl.ACTIVE_DEFS
                       if d["code"] == r["indicator_code"])]
        self.assertTrue(all(r["extra"]["pit_capable"] for r in stage2))


class TestEngineeringGuards(unittest.TestCase):
    """双同步 / 调度 / 监控 / 资源红线，防止阶段 2 接线静默回退"""

    def test_ddl_dual_source(self):
        """D12 收口：新映射表在 schema.sql / regime_schema.py 双源同步。"""
        schema = _read("sql/schema.sql")
        code = _read("regime_schema.py")
        for tbl in ("regime.stock_sw_industry_mapping", "regime.stock_hsics_industry_mapping"):
            with self.subTest(tbl=tbl):
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, schema)
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, code)
        self.assertIn("sw_code_l3", schema)
        self.assertNotIn("sw_code_l1", schema)   # 层级关系不冗余（只在分类表）
        self.assertNotIn("eff_from", schema.split("stock_sw_industry_mapping")[1][:400])
        self.assertIn("_SECTOR_DDL_KEYS", code)

    def test_scheduler_registration(self):
        src = _read("market_scheduler.py")
        self.assertIn('"get_stock_sw_industry_mapping.py"', src)
        self.assertIn('"get_stock_hsics_industry_mapping.py"', src)
        self.assertNotIn('"get_sector_constituent.py"', src)
        # 计算层仍禁止挂进调度器（R4/R5）
        self.assertNotIn('"sector_layer.py"', src)

    def test_monitor_registration(self):
        src = _read("monitor_collector.py")
        for tbl in ("stock_sw_industry_mapping", "stock_hsics_industry_mapping"):
            with self.subTest(tbl=tbl):
                self.assertIn('("regime", "' + tbl + '"', src)
        self.assertNotIn('"constituent_change"', src)

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
