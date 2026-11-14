"""第④层「行业配置」阶段 1 测试：指标口径（纯函数）+ 工程红线载体。

不测业务正确性之外的东西；需要真实库的契约测试单独放在 test_sector_layer_db.py
（本文件全部为纯函数 / 静态文本断言，任何环境都能跑）。

背景：doc/industry_allocation_feasibility.md §3 指标体系 / §4.2 阶段 1。
"""
import datetime as dt
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sector_layer as sl  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestXsecPercentile(unittest.TestCase):
    """M1：中位秩，不用上界（主报告 §7.1）"""

    def test_no_ties(self):
        s = pd.Series([10.0, 20.0, 30.0, 40.0])
        out = sl._xsec_percentile(s)
        # XSEC_MIN_N=10 时应返回全 NaN；这里临时降低门槛验证数学
        old = sl.XSEC_MIN_N
        sl.XSEC_MIN_N = 1
        try:
            out = sl._xsec_percentile(s)
            self.assertEqual(list(out.round(2)), [12.5, 37.5, 62.5, 87.5])
            self.assertAlmostEqual(out.mean(), 50.0)
        finally:
            sl.XSEC_MIN_N = old

    def test_ties_use_midrank(self):
        old = sl.XSEC_MIN_N
        sl.XSEC_MIN_N = 1
        try:
            # 4 个观测，两个并列最小值：平均秩 1.5 → (1.5-0.5)/4*100 = 25
            out = sl._xsec_percentile(pd.Series([5.0, 5.0, 9.0, 11.0]))
            self.assertAlmostEqual(out.iloc[0], 25.0)
            self.assertAlmostEqual(out.iloc[1], 25.0)
            self.assertAlmostEqual(out.iloc[2], 62.5)
        finally:
            sl.XSEC_MIN_N = old

    def test_min_sample_gate(self):
        s = pd.Series([float(i) for i in range(9)])
        self.assertTrue(sl._xsec_percentile(s).isna().all())

    def test_reverse_percentile(self):
        old = sl.XSEC_MIN_N
        sl.XSEC_MIN_N = 1
        try:
            s = pd.Series([1.0, 2.0, 3.0, 4.0])
            p, r = sl._xsec_percentile(s), sl._reverse_percentile(s)
            self.assertTrue(np.allclose((p + r).to_numpy(), 100.0))
        finally:
            sl.XSEC_MIN_N = old


class TestCrowdGate(unittest.TestCase):
    """M5：阶段 1 两因子部分模式不得冒充三因子共振"""

    def test_k_less_than_2_is_na(self):
        self.assertEqual(sl.crowd_gate({"A": 95.0, "B": None})["gate"], "NA")
        self.assertEqual(sl.crowd_gate({"A": None, "B": None})["gate"], "NA")

    def test_k2_never_extreme(self):
        c = sl.crowd_gate({"A": 95.0, "B": 91.0})
        self.assertEqual(c["gate"], "HIGH")
        self.assertEqual(c["k"], 2)
        self.assertTrue(c["partial"])
        self.assertEqual(c["hit"], 2)

    def test_k3_can_be_extreme(self):
        c = sl.crowd_gate({"A": 95.0, "B": 91.0, "C": 88.0})
        self.assertEqual(c["gate"], "EXTREME")
        self.assertFalse(c["partial"])

    def test_missing_not_counted_as_zero(self):
        # 缺失因子既不进分子也不进分母
        c = sl.crowd_gate({"A": 95.0, "B": 10.0, "C": None})
        self.assertEqual(c["k"], 2)
        self.assertEqual(c["hit"], 1)
        self.assertEqual(c["gate"], "WATCH")

    def test_nan_is_missing(self):
        c = sl.crowd_gate({"A": 95.0, "B": float("nan"), "C": 91.0})
        self.assertEqual(c["k"], 2)
        self.assertEqual(c["gate"], "HIGH")


class TestBizPureFunctions(unittest.TestCase):
    def test_quadrant_four_cases(self):
        cases = [(80, 20, "强势·未拥挤"), (80, 80, "强势·偏贵拥挤"),
                 (20, 20, "冷门·未拥挤"), (20, 80, "弱势·偏贵拥挤")]
        for s, r, expect in cases:
            with self.subTest(s=s, r=r):
                self.assertEqual(sl.quadrant_of(s, r, 50, 50), expect)

    def test_quadrant_na_on_missing(self):
        self.assertEqual(sl.quadrant_of(float("nan"), 50, 50, 50), "NA")
        self.assertEqual(sl.quadrant_of(60, None, 50, 50), "NA")

    def test_confidence(self):
        self.assertEqual(sl.confidence_of(1000, 0.9), "normal")
        self.assertEqual(sl.confidence_of(27, 0.9), "low")
        self.assertEqual(sl.confidence_of(1000, 0.5), "low")
        self.assertEqual(sl.confidence_of(1000, 1.0, pit_capable=False), "low")

    def test_params_hash_stable_and_sensitive(self):
        a = dict(code="X", unit="pct", direction="high_good",
                 formula="f", model_key="M1")
        b = dict(a, formula="g")
        self.assertEqual(sl._params_hash(a), sl._params_hash(a))
        self.assertNotEqual(sl._params_hash(a), sl._params_hash(b))
        self.assertEqual(len(sl._params_hash(a)), 16)

    def test_clean_value(self):
        self.assertIsNone(sl._clean_value(float("nan")))
        self.assertIsNone(sl._clean_value(float("inf")))
        self.assertIsNone(sl._clean_value(np.float64("nan")))
        self.assertEqual(sl._clean_value(np.int64(3)), 3)
        self.assertEqual(sl._clean_value(1.23456789), 1.234568)
        # dict/list 必须原样保留：db._adapt_value 负责转 extras.Json
        self.assertEqual(sl._clean_value({"a": 1}), {"a": 1})
        self.assertEqual(sl._clean_value([1, 2]), [1, 2])


class TestIndicatorDict(unittest.TestCase):
    def test_all_defs_have_required_fields(self):
        keys = {"code", "indicator_name", "domain", "side", "unit", "direction",
                "formula", "model_key", "source", "freq", "remark"}
        for d in sl.INDICATOR_DEFS:
            with self.subTest(code=d["code"]):
                self.assertTrue(keys.issubset(d.keys()))
                self.assertTrue(d["code"].startswith("SEC."))
                self.assertIn(d["side"], ("state", "risk"))
                self.assertIn(d["direction"], ("high_good", "high_risk", "neutral"))
                self.assertIn(d["domain"],
                              {"TREND", "VAL", "FLOW", "CROWD", "FUND", "BREADTH"})

    def test_unique_codes_and_unique_hash(self):
        codes = [d["code"] for d in sl.INDICATOR_DEFS]
        hashes = [sl._params_hash(d) for d in sl.INDICATOR_DEFS]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_stage2_defs_active_and_wired(self):
        """阶段 2 已落地：原占位指标必须激活且接到聚合表列上。"""
        codes = ("SEC.CROWD.TURNOVER_PCT", "SEC.FUND.REVISION_BREADTH",
                 "SEC.BREADTH.UP_RATIO")
        for code in codes:
            d = next(x for x in sl.INDICATOR_DEFS if x["code"] == code)
            with self.subTest(code=code):
                self.assertTrue(d["is_active"])
                self.assertIn(d, sl.ACTIVE_DEFS)
                self.assertIn("sector_constituent", d["source"])
                self.assertTrue(d["value_col"] or d["pct_x_col"])
        self.assertEqual(sl.CALIB_VERSION, "s2.1")   # 口径断点必须换指纹
        self.assertEqual(len(sl.ENABLED_DIMENSIONS), 6)
        self.assertEqual(sl.PIT_START, dt.date(2021, 12, 13))

    def test_pe_ts_pct_never_emits_percentile(self):
        d = next(x for x in sl.ACTIVE_DEFS if x["code"] == "SEC.VAL.PE_TS_PCT")
        self.assertEqual(d["pct_t_col"], "pe_ts_pct")
        self.assertEqual(d["n_col"], "pe_n_sample")
        self.assertEqual(d["min_periods"] if "min_periods" in d else 0, 0)
        self.assertEqual(sl.MIN_PERIODS, 250)

    def test_dimension_structure(self):
        # 成员 <4 走单锚（等权会让单成员 33% > 25% 上限）；恰好 4 成员走分块等权
        for name, cfg in sl.DIMENSION_DEFS.items():
            with self.subTest(dim=name):
                self.assertIn(cfg["structure"],
                              ("single_anchor", "rule", "equal", "unavailable_stage2"))
                self.assertIn(cfg["side"], ("state", "risk"))
        for name in ("FUNDAMENTAL", "BREADTH"):
            cfg = sl.DIMENSION_DEFS[name]
            self.assertEqual(cfg["structure"], "equal")
            self.assertEqual(len(cfg["members"]), 4)
            self.assertAlmostEqual(1.0 / len(cfg["members"]), 0.25)
            for code, sign in cfg["members"]:
                self.assertIn(sign, (1, -1))
                self.assertTrue(any(d["code"] == code for d in sl.ACTIVE_DEFS))


class TestComputeFrameContract(unittest.TestCase):
    """构造 12 行业 × 30 日假数据，验证宽表输出契约（不需要数据库）。"""

    @classmethod
    def setUpClass(cls):
        codes = [f"8010{i:02d}" for i in range(12)]
        dates = pd.date_range("2026-01-01", periods=30, freq="B")
        rows = []
        rng = np.random.default_rng(42)
        for j, d in enumerate(dates):
            for i, c in enumerate(codes):
                rows.append(dict(trade_date=d.date(), sw_code=c, sw_name=f"行业{i}",
                                 close=100 + i + j * 0.1,
                                 ret_20d=float(i) - 5.5 + j * 0.01,
                                 ret_60d=float(i) - 5.5 + j * 0.02,
                                 rs_20d=float(i) - 5.5, rs_rank=float(12 - i),
                                 mom_rank=float(12 - i), turnover=100.0 + i,
                                 fund_flow_net=float(i) - 5.5,
                                 fund_flow_net_5d=float(i), flow_rank=float(12 - i),
                                 mapped_ths_count=1))
        cls.raw = pd.DataFrame(rows)
        cls.wide = sl._compute_frame(cls.raw.copy(), pd.DataFrame(), pd.DataFrame())

    def test_rs60_cross_section_mean_zero(self):
        g = self.wide.groupby("trade_date")["rs_60d"].mean()
        self.assertTrue(np.allclose(g.to_numpy(), 0.0))

    def test_xsec_percentile_mean_50(self):
        g = self.wide.groupby("trade_date")["rs_xsec_pct"].mean()
        self.assertTrue(np.allclose(g.to_numpy(), 50.0, atol=1e-6))

    def test_rolling_min_periods(self):
        first = self.wide[self.wide.sw_code == "801000"].sort_values("trade_date")
        self.assertTrue(first["net_20d"].head(19).isna().all())
        self.assertTrue(pd.notna(first["net_20d"].iloc[19]))

    def test_valuation_missing_is_not_fabricated(self):
        self.assertTrue(self.wide["pe_ttm"].isna().all())
        self.assertTrue(self.wide["pe_xsec_pct"].isna().all())
        self.assertTrue(self.wide["pe_ts_pct"].isna().all())

    def test_crowding_k_less_than_2_when_amt_missing(self):
        # 空 amt 序列 → 只有估值因子（也缺）→ k<2 → NA
        self.assertEqual(set(self.wide["crowd_gate_code"]),
                         {sl.CROWD_GATE_CODES["NA"]})

    def test_state_side_is_mean_of_two_available_anchors(self):
        # 构造数据里 TREND + FLOW 两个单锚都有值 → state_side 必须等于两者均值；
        # 若只有 1 个维度可用，_compute_frame 会把 state_side 置 NaN（见代码守卫）。
        w = self.wide
        self.assertTrue(w["state_side"].notna().all())
        expect = (w["dim_TREND"] + w["dim_FLOW"]) / 2
        self.assertTrue(np.allclose(w["state_side"].to_numpy(), expect.to_numpy()))

    def test_rank_and_rank_n(self):
        # 12 个行业同日横截面：rank_n=12，rank 从 1 起（method=min 允许并列）
        self.assertTrue((self.wide["rank_n"] == 12).all())
        self.assertEqual(int(self.wide.groupby("trade_date")["rank"].min().min()), 1)

    def test_rows_have_no_nan_numeric(self):
        rows = sl._indicator_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertGreater(len(rows), 0)
        for r in rows:
            for k in ("raw_value", "percentile_xsec", "percentile_ts"):
                v = r[k]
                if isinstance(v, (float, np.floating)):
                    self.assertFalse(math.isnan(float(v)) or math.isinf(float(v)))

    def test_biz_rows_flags(self):
        rows = sl._biz_rows(self.wide, dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertTrue(all("PARTIAL_STATE" in r["gate_flags"] for r in rows))
        self.assertTrue(all("rank_no" in r for r in rows))


class TestEngineeringGuards(unittest.TestCase):
    """R4/R5 + 双同步 + 部署接线，防止静默回退"""

    def test_not_in_scheduler(self):
        src = _read("market_scheduler.py")
        self.assertNotIn("sector_layer", src)

    def test_oneshot_unit_has_six_resource_limits(self):
        src = _read("system/sector-layer.service")
        for key in ("MemoryMax=512M", "MemorySwapMax=0", "RuntimeMaxSec=",
                    "Nice=", "IOSchedulingPriority=", "OOMScoreAdjust="):
            with self.subTest(key=key):
                self.assertIn(key, src)
        self.assertIn("Type=oneshot", src)
        # unit 文件注释里会提到「无 [Install]」，只断言没有真正的 [Install] 段落
        self.assertNotIn("\n[Install]", src)

    def test_timer_wired(self):
        t = _read("system/sector-layer.timer")
        self.assertIn("OnCalendar=Mon-Fri 18:50:00", t)
        self.assertIn("Unit=sector-layer.service", t)
        self.assertIn("sector-layer.timer", _read("deploy.sh"))
        b = _read("bootstrap.sh")
        self.assertIn("sector-layer.service sector-layer.timer", b)
        self.assertIn("systemctl enable --now sector-layer.timer", b)

    def test_ddl_dual_source(self):
        schema = _read("sql/schema.sql")
        code = _read("regime_schema.py")
        for tbl in ("regime.sector_indicator_dict", "regime.sector_indicator_value",
                    "regime.sector_score_daily", "regime.sector_biz_daily"):
            with self.subTest(tbl=tbl):
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, schema)
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, code)
        self.assertIn("rank_no", schema)
        self.assertIn("rank_no", code)
        self.assertIn("def ensure_sector_schema", code)

    def test_ddl_primary_keys_include_market_and_classification(self):
        schema = _read("sql/schema.sql")
        self.assertIn("PRIMARY KEY (market, classification, sector_code, trade_date, indicator_code)", schema)
        self.assertIn("PRIMARY KEY (market, classification, sector_code, trade_date, dimension_code)", schema)
        self.assertIn("PRIMARY KEY (market, classification, sector_code, trade_date)", schema)

    def test_monitor_registered(self):
        src = _read("monitor_collector.py")
        self.assertIn('("行业配置计算", 1440', src)
        for tbl in ("sector_indicator_value", "sector_score_daily", "sector_biz_daily"):
            with self.subTest(tbl=tbl):
                self.assertIn('("regime", "' + tbl + '"', src)
                self.assertIn('("regime", "' + tbl + '")', src)  # CN 日历集合

    def test_write_batch_under_r6(self):
        self.assertLessEqual(sl.WRITE_BATCH, 50_000)
        self.assertIn("for k in range(0, len(rows), WRITE_BATCH)", _read("sector_layer.py"))
        self.assertIn("conn.commit()", _read("sector_layer.py"))

    def test_month_slicing_guard_for_r3(self):
        """全量回填曾因整年块输出行堆积达 892MB（> MemoryMax=512M）；
        按月切片 + 生成即写是 R3 的硬护栏，禁止重构回退。
        """
        src = _read("sector_layer.py")
        self.assertIn("def _iter_month_frames", src)
        self.assertIn("892MB", src)               # 事故数字留痕在注释里
        self.assertIn("gc.collect()", src)
        self.assertIn("del iv, sc, bz, sub", src)

    def test_queries_have_lower_bound(self):
        src = _read("sector_layer.py")
        self.assertIn("WHERE trade_date BETWEEN %s AND %s", src)
        self.assertNotIn("SELECT * FROM regime.sector_daily", src)

    def test_chunks_are_bounded(self):
        chunks = sl._chunks(dt.date(1999, 12, 30), dt.date(2026, 11, 11))
        self.assertGreater(len(chunks), 1)
        for s, e in chunks:
            self.assertLessEqual((e - s).days, 366 * 4)

    def test_no_allocation_advice_in_output(self):
        # 输出值（象限名 / RRG 标签）不得含任何配置建议措辞
        labels = [sl.quadrant_of(s, r, 50, 50)
                  for s in (80, 20) for r in (80, 20)] + list(sl.RRG_LABELS.values())
        for label in labels:
            for banned in ("超配", "低配", "标配", "买入", "卖出"):
                with self.subTest(label=label, word=banned):
                    self.assertNotIn(banned, label)
        # DDL 注释必须显式声明「不含配置建议」，防止后来者把象限名改成建议
        self.assertIn("不含超配/低配/买卖含义", _read("sql/schema.sql"))


if __name__ == "__main__":
    unittest.main()
