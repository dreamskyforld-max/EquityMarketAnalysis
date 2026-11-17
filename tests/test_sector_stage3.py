"""第④层「行业配置」阶段 3（HK 行业层）测试：口径（纯函数）+ 工程红线载体。

不连库：全部为纯函数 / 静态文本断言，任何环境都能跑。
背景：doc/industry_allocation_feasibility.md §2.2 D4 / §3.2 M8 / §4.2 阶段 3。

三项用户决策（2026-11-14）在测试里钉住，防静默回退：
  ① 沿用现有 4 张输出表，字典回归一行一概念（主键仅 code）；
  ② M8 加权版用 total_market_val（2002 起），等权版 2000 起；
  ③ HK 本期 A/B/D/F + 南向，L3 不排名。
"""
import datetime as dt
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import hk_sector_index as hki          # noqa: E402
import sector_layer as sl              # noqa: E402
import sector_layer_hk as slh          # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _quotes(rows):
    """rows: [(date, code, close, change_pct, amount, tmv, pe, pb, dy), ...]"""
    return pd.DataFrame(rows, columns=[
        "trade_date", "stock_code", "close", "change_pct", "amount",
        "total_market_val", "pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm"
    ]).assign(prev_close=np.nan)


class TestM8IndexMath(unittest.TestCase):
    """M8 两版收益 + 市值加权调和估值 + 覆盖度门禁"""

    def setUp(self):
        self.day = dt.date(2026, 11, 13)
        self.cmap = pd.DataFrame({
            "stock_code": ["HK.00001", "HK.00002"],
            "sector_code": ["HK.LIST1001", "HK.LIST1001"],
            "sector_name": ["Banks", "Banks"]})
        self.q = _quotes([
            (self.day, "HK.00001", 10.0, 1.0, 5e6, 100.0, 10.0, 1.0, 3.0),
            (self.day, "HK.00002", 20.0, 3.0, 8e6, 300.0, 20.0, 2.0, 5.0),
        ])
        self.q["trade_date"] = pd.to_datetime(self.q["trade_date"])

    def _ss(self, q):
        return hki._quote_partial(q, self.cmap, self.day, self.day)

    def test_equal_and_cap_weighted_returns(self):
        ss = self._ss(self.q)
        self.assertFalse(ss.empty)
        self.assertEqual(float(ss["n_eq"].iloc[0]), 2.0)
        self.assertAlmostEqual(float(ss["s_eq"].iloc[0]), 4.0)       # 1 + 3
        self.assertAlmostEqual(float(ss["s_wret"].iloc[0]), 1000.0)  # 1*100 + 3*300
        self.assertAlmostEqual(float(ss["s_w"].iloc[0]), 400.0)
        out = hki._finalize(ss, {"HK.LIST1001": "Banks"})
        self.assertAlmostEqual(float(out["ret_eq"].iloc[0]), 2.0)
        self.assertAlmostEqual(float(out["ret_cap"].iloc[0]), 2.5)    # 1000/400
        self.assertAlmostEqual(float(out["amount"].iloc[0]), 13e6)
        self.assertEqual(int(out["n_constituent"].iloc[0]), 2)

    def test_harmonic_pe_and_weighted_dividend(self):
        out = hki._finalize(self._ss(self.q), {"HK.LIST1001": "Banks"})
        # Σw / Σ(w/pe) = 400 / (100/10 + 300/20) = 400/25 = 16
        self.assertAlmostEqual(float(out["pe_agg"].iloc[0]), 16.0)
        self.assertAlmostEqual(float(out["pb_agg"].iloc[0]), 400 / (100 / 1 + 300 / 2))
        self.assertAlmostEqual(float(out["div_yield_agg"].iloc[0]),
                               (100 * 3 + 300 * 5) / 400)

    def test_liquidity_gate_filters_penny_stocks(self):
        q = self.q.copy()
        q.loc[1, "amount"] = 1000.0        # < MIN_AMOUNT
        out = hki._finalize(self._ss(q), {"HK.LIST1001": "Banks"})
        self.assertAlmostEqual(float(out["ret_eq"].iloc[0]), 1.0)
        self.assertEqual(int(out["n_constituent"].iloc[0]), 1)

    def test_coverage_gate_voids_cap_and_valuation(self):
        q = self.q.copy()
        q["total_market_val"] = np.nan    # 0/2 覆盖 < 0.6
        out = hki._finalize(self._ss(q), {"HK.LIST1001": "Banks"})
        for col in ("ret_cap", "pe_agg", "pb_agg", "div_yield_agg"):
            with self.subTest(col=col):
                self.assertTrue(pd.isna(out[col].iloc[0]))
        self.assertAlmostEqual(float(out["ret_eq"].iloc[0]), 2.0)  # 等权版不受影响

    def test_cap_weighted_not_available_before_2002(self):
        q = self.q.copy()
        q["trade_date"] = pd.Timestamp("2001-06-01")
        ss = hki._quote_partial(q, self.cmap, dt.date(2001, 6, 1), dt.date(2001, 6, 1))
        out = hki._finalize(ss, {"HK.LIST1001": "Banks"})
        self.assertTrue(pd.isna(out["ret_cap"].iloc[0]))
        self.assertAlmostEqual(float(out["ret_eq"].iloc[0]), 2.0)

    def test_negative_pe_excluded_from_harmonic_mean(self):
        # 4 只成分、3 只 pe>0（覆盖 0.75 ≥ 0.6 门禁），负 PE 不进调和均值
        day = self.day
        cmap = pd.DataFrame({"stock_code": [f"HK.{i:05d}" for i in range(4)],
                             "sector_code": ["HK.LIST1001"] * 4})
        rows = [(day, f"HK.{i:05d}", 10.0 + i, 1.0, 5e6, 100.0 * (i + 1),
                 10.0 + i if i < 3 else -5.0, 1.0, 2.0) for i in range(4)]
        q = _quotes(rows)
        q["trade_date"] = pd.to_datetime(q["trade_date"])
        out = hki._finalize(hki._quote_partial(q, cmap, day, day),
                            {"HK.LIST1001": "Banks"})
        expected = 600.0 / (100 / 10 + 200 / 11 + 300 / 12)
        self.assertAlmostEqual(float(out["pe_agg"].iloc[0]), expected, places=5)


class TestSufficientStatistics(unittest.TestCase):
    """按股票分批后的 sum 合并必须与整批逐值相等（R3 无损合并）"""

    def test_split_batches_equal_whole(self):
        day = dt.date(2026, 11, 13)
        cmap = pd.DataFrame({"stock_code": [f"HK.{i:05d}" for i in range(4)],
                             "sector_code": ["HK.LIST1001"] * 4})
        rows = [(day, f"HK.{i:05d}", 10.0 + i, 1.0 + i, 5e6, 100.0 * (i + 1),
                 10.0 + i, 1.0 + i * 0.1, 2.0 + i) for i in range(4)]
        q = _quotes(rows)
        q["trade_date"] = pd.to_datetime(q["trade_date"])
        whole = hki._quote_partial(q, cmap, day, day)
        parts = [hki._quote_partial(q.iloc[[i]].copy(), cmap.iloc[[i]].copy(), day, day)
                 for i in range(4)]
        merged = (pd.concat(parts, ignore_index=True)
                    .groupby(["trade_date", "sector_code"], as_index=False)[hki._SS_COLS].sum())
        pd.testing.assert_frame_equal(whole.reset_index(drop=True),
                                      merged.reset_index(drop=True),
                                      check_exact=False, rtol=1e-6)


class TestHKIndicatorDict(unittest.TestCase):
    """指标字典：口径登记、不可用项、params_hash 隔离"""

    def test_codes_unique(self):
        codes = [d["code"] for d in slh.HK_INDICATOR_DEFS]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(slh.HK_INDICATOR_DEFS), 25)
        self.assertEqual(len(slh.ACTIVE_DEFS), 24)

    def test_turnover_pct_registered_but_inactive(self):
        inactive = [d for d in slh.HK_INDICATOR_DEFS if not d["is_active"]]
        self.assertEqual([d["code"] for d in inactive], ["SEC.CROWD.TURNOVER_PCT"])
        self.assertIn("75", inactive[0]["remark"])

    def test_m8_weight_change_documented(self):
        all_remark = " ".join(d["remark"] for d in slh.HK_INDICATOR_DEFS)
        src = _read("hk_sector_index.py")
        for token in ("total_market_val", "circular_market_val", "65"):
            with self.subTest(token=token):
                self.assertIn(token, src)
        self.assertIn("幸存者偏差", src)
        for d in slh.HK_INDICATOR_DEFS:
            with self.subTest(code=d["code"]):
                self.assertIn("幸存者偏差", d["remark"])

    def test_params_hash_differs_from_cn(self):
        cn = {d["code"]: sl._params_hash(d) for d in sl.INDICATOR_DEFS}
        for d in slh.ACTIVE_DEFS:
            with self.subTest(code=d["code"]):
                self.assertNotEqual(slh._hk_params_hash(d), cn.get(d["code"]))
        # 同 code 的 HK 指纹稳定且彼此不同
        hashes = [slh._hk_params_hash(d) for d in slh.HK_INDICATOR_DEFS]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_enabled_dimensions_exclude_unavailable(self):
        self.assertEqual(slh.ENABLED_DIMENSIONS,
                         ("TREND", "VALUATION", "CROWDING", "BREADTH"))
        self.assertEqual(slh.HK_DIMENSION_DEFS["FLOW"]["structure"], "unavailable")
        self.assertEqual(slh.HK_DIMENSION_DEFS["FUNDAMENTAL"]["structure"], "unavailable")
        self.assertIn("announce_date", slh.HK_DIMENSION_DEFS["FUNDAMENTAL"]["reason"])

    def test_breadth_equal_weights_within_25pct_cap(self):
        members = slh.HK_DIMENSION_DEFS["BREADTH"]["members"]
        self.assertEqual(len(members), 4)
        self.assertLessEqual(1.0 / len(members), 0.25)
        self.assertIn(-1, [sign for _, sign in members])  # RET_DISPERSION 翻转


class TestCrowdingPartialMode(unittest.TestCase):
    """HK 只有 2 因子：k=2、partial=true、绝不判 EXTREME"""

    def test_two_factor_gate_never_extreme(self):
        for a in (10, 50, 85, 95, None):
            for v in (10, 50, 85, 95, None):
                with self.subTest(a=a, v=v):
                    c = sl.crowd_gate({"AMT_SHARE_PCT": a, "VAL_PCT": v})
                    self.assertNotEqual(c["gate"], "EXTREME")
                    self.assertLessEqual(c["k"], 2)
                    if c["k"] == 2:
                        self.assertTrue(c["partial"])

    def test_missing_factor_not_counted_as_zero(self):
        c = sl.crowd_gate({"AMT_SHARE_PCT": None, "VAL_PCT": 95})
        self.assertEqual(c["k"], 1)
        self.assertEqual(c["gate"], "NA")


class TestHKBusinessContract(unittest.TestCase):
    """L3：不排名、闸门恒开、非 PIT 低置信、A/H 不混排"""

    def _wide(self, date=dt.date(2026, 11, 13)):
        rec = dict(trade_date=date, sector_code="HK.LIST1001",
                   biz_coverage=0.75, state_n=2, state_members=["dim_TREND", "dim_BREADTH"],
                   valid_days_20=18, state_side=60.0, risk_side=55.0,
                   quadrant="强势·未拥挤", rank_n=113, pe_n_sample=5000,
                   _crowd=dict(k=2, partial=True, gate="NORMAL", hit=0, members=[]),
                   n_valid=113, coverage=0.95, rs_20d=1.0)
        for d in slh.ACTIVE_DEFS:
            for key in ("value_col", "pct_x_col", "pct_t_col", "n_col"):
                col = d[key]
                if col:
                    rec.setdefault(col, 1.0)
        return pd.DataFrame([rec])

    def test_no_rank_and_no_rank_reason(self):
        rows = slh._biz_rows(self._wide(), dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertEqual(len(rows), 4)   # RANK 字典登记但本期不产出值表行
        codes = {r["biz_code"] for r in rows}
        self.assertNotIn("SEC.BIZ.RANK", codes)
        gate = next(r for r in rows if r["biz_code"] == "SEC.BIZ.GATE")
        self.assertTrue(gate["detail"]["no_rank"])
        self.assertIn("不产出跨行业排名", gate["detail"]["no_rank_reason"])
        # 不排名决策同时留在共有 extra 与 GATE 行 detail，前端两条路径都能取到
        self.assertTrue(any(r["extra"].get("no_rank") for r in rows))

    def test_partial_state_and_break_gate(self):
        rows = slh._biz_rows(self._wide(), dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        r = rows[0]
        gate = next(x for x in rows if x["biz_code"] == "SEC.BIZ.GATE")
        self.assertIn("PARTIAL_STATE", gate["detail"]["gate_flags"])
        self.assertEqual(r["confidence"], "low")
        self.assertFalse(r["extra"]["pit_capable"])
        self.assertTrue(r["extra"]["survivorship_bias"])
        self.assertEqual(r["detail"].get("state_dims_full") or
                         next(x for x in rows if x["biz_code"] == "SEC.BIZ.STATE_SIDE")["detail"]["state_dims_full"], 4)
        early = slh._biz_rows(self._wide(dt.date(2001, 6, 1)),
                              dt.date(2000, 1, 1), dt.date(2001, 12, 31))
        early_gate = next(x for x in early if x["biz_code"] == "SEC.BIZ.GATE")
        self.assertIn("BREAK", early_gate["detail"]["gate_flags"])
        self.assertTrue(all(not x["applicable"] for x in early))

    def test_quadrant_names_contain_no_advice(self):
        for q in ("强势·未拥挤", "强势·偏贵拥挤", "冷门·未拥挤", "弱势·偏贵拥挤", "NA"):
            for banned in ("超配", "低配", "标配", "买入", "卖出"):
                with self.subTest(q=q, word=banned):
                    self.assertNotIn(banned, q)

    def test_indicator_rows_are_hk_only(self):
        rows = slh._indicator_rows(self._wide(), dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertEqual(r["market"], "HK")
            self.assertNotIn("classification", r)
            self.assertFalse(r["extra"]["pit_capable"])
            self.assertTrue(r["extra"]["survivorship_bias"])
        # 不可用指标不产出行
        self.assertNotIn("SEC.CROWD.TURNOVER_PCT", [r["indicator_code"] for r in rows])

    def test_score_rows_mark_unavailable_dimensions(self):
        rows = slh._score_rows(self._wide(), dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        by = {r["dimension_code"]: r for r in rows}
        self.assertEqual(set(by), set(slh.HK_DIMENSION_DEFS))
        for name in ("FLOW", "FUNDAMENTAL"):
            with self.subTest(dim=name):
                self.assertIsNone(by[name]["score"])
                self.assertIn("reason", by[name]["extra"])
        for r in rows:
            self.assertEqual(r["confidence"], "low")


class TestCrossSectionIsolation(unittest.TestCase):
    """HK 横截面分位只在 HK 数据集内计算，绝不混入 CN"""

    def test_hk_frame_math(self):
        rng = np.random.default_rng(7)
        days = pd.bdate_range("2026-01-05", periods=90).date
        rows = []
        for d in days:
            for i in range(15):            # 15 > XSEC_MIN_N=10
                rows.append(dict(trade_date=d, sector_code=f"HK.LIST{1000+i}",
                                 sector_name="X", ret_eq=float(rng.normal(0, 1)),
                                 ret_cap=float(rng.normal(0, 1)), amount=1e7 + i,
                                 pe_agg=10 + i / 2, pb_agg=1 + i / 20,
                                 div_yield_agg=2 + i / 10,
                                 up_ratio=40 + i, above_ma60=30 + i,
                                 new_high_low=i - 7, ret_dispersion=0.02 + i / 1000,
                                 southbound_net=float(i), coverage=0.95,
                                 n_constituent=20 + i))
        df = pd.DataFrame(rows)
        out = slh._compute_hk_frame(df, pd.DataFrame())
        # RS 横截面均值恒 0（M3）
        for d, g in out.groupby("trade_date"):
            with self.subTest(date=d):
                if g["rs_20d"].notna().any():
                    self.assertAlmostEqual(float(g["rs_20d"].mean()), 0.0, places=6)
        self.assertIn("rs_xsec_pct", out.columns)
        self.assertLessEqual(float(out["biz_coverage"].max()), 1.0)
        # k=2 部分模式，永不 EXTREME
        codes = set(out["crowd_gate_code"].dropna().astype(int))
        self.assertNotIn(sl.CROWD_GATE_CODES["EXTREME"], codes)
        self.assertTrue(all(c["k"] <= 2 for c in out["_crowd"]))

    def test_hk_sources_never_touch_cn_tables(self):
        src = _read("sector_layer_hk.py")
        self.assertIn('MARKET = "HK"', src)
        self.assertIn("FROM industry.hk_index_daily", src)
        self.assertNotIn("FROM regime.sector_daily", src)
        self.assertNotIn("FROM a_daily_quote", src)
        self.assertNotIn("FROM regime.sector_valuation_snapshot", src)

    def test_dict_upsert_is_single_code(self):
        src = _read("sector_layer_hk.py")
        self.assertIn("def sync_dict_hk", src)
        self.assertIn("from sector_layer import sync_dict", src)
        cn = _read("sector_layer.py")
        self.assertIn('conflict_cols=["indicator_code"]', cn)


class TestEngineeringGuards(unittest.TestCase):
    """R1/R3/R4/R5/R6/R7 + 双源 DDL + 部署接线，防止静默回退"""

    def test_cn_side_unchanged(self):
        """阶段 3 只允许改 CN 的字典冲突键，口径版本与指纹算法一律不动。"""
        self.assertEqual(sl.MARKET, "CN")
        self.assertEqual(sl.CALIB_VERSION, "s2.1")
        self.assertEqual(len(sl.INDICATOR_DEFS), 41)
        self.assertNotEqual(slh.CALIB_VERSION, sl.CALIB_VERSION)
        payload = _read("sector_layer.py").split("def _params_hash")[1].split("return")[0]
        self.assertIn("CALIB_VERSION", payload)
        self.assertNotIn("MARKET", payload)

    def test_ddl_dual_source_and_pk_migration(self):
        schema, code = _read("sql/schema.sql"), _read("regime_schema.py")
        for tbl in ("industry.hk_index_daily", "industry.indicator_dict"):
            with self.subTest(tbl=tbl):
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, schema)
                self.assertIn("CREATE TABLE IF NOT EXISTS " + tbl, code)
        for text in (schema, code):
            self.assertIn("PRIMARY KEY (trade_date, sector_code)", text)
            self.assertIn("indicator_code  VARCHAR(64)     PRIMARY KEY", text)
        self.assertIn("def _migrate_sector_schema_v2", code)
        self.assertIn("_migrate_sector_schema_v2(conn", code)
        self.assertIn('"hk_sector_index_daily", "hk_sector_index_daily_idx"', code)
        self.assertIn("ret_cap", schema)
        self.assertIn("southbound_net", schema)

    def test_sector_hierarchy_as_of_three_sources(self):
        for rel in ("sql/schema.sql", "build_sector_hierarchy.py"):
            with self.subTest(rel=rel):
                self.assertIn("as_of", _read(rel))
        src = _read("build_sector_hierarchy.py")
        self.assertIn("ADD COLUMN IF NOT EXISTS as_of DATE", src)
        self.assertIn("def run(codes=None, ctx=None)", src)
        self.assertIn('"as_of": date.today()', src)
        self.assertIn('"build_sector_hierarchy.py"', _read("market_scheduler.py"))
        self.assertIn("18, \"minute\": 30", _read("market_scheduler.py"))

    def test_hk_oneshot_unit_has_six_limits(self):
        src = _read("system/sector-layer-hk.service")
        for key in ("MemoryMax=512M", "MemorySwapMax=0", "RuntimeMaxSec=",
                    "Nice=", "IOSchedulingPriority=", "OOMScoreAdjust="):
            with self.subTest(key=key):
                self.assertIn(key, src)
        self.assertIn("Type=oneshot", src)
        self.assertIn("sector_layer_hk.py", src)
        self.assertNotIn("\n[Install]", src)

    def test_timer_wired_and_staggered_from_cn(self):
        t = _read("system/sector-layer-hk.timer")
        self.assertIn("OnCalendar=Mon-Fri 19:20:00", t)
        self.assertIn("Unit=sector-layer-hk.service", t)
        self.assertIn("sector-layer-hk.timer", _read("deploy.sh"))
        b = _read("bootstrap.sh")
        self.assertIn("sector-layer-hk.service sector-layer-hk.timer", b)
        self.assertIn("systemctl enable --now sector-layer-hk.timer", b)

    def test_not_in_scheduler(self):
        src = _read("market_scheduler.py")
        self.assertNotIn("sector_layer_hk", src)
        self.assertNotIn("hk_sector_index", src.split("行业层级映射")[0])

    def test_monitor_registered(self):
        src = _read("monitor_collector.py")
        self.assertIn('("行业配置计算HK", 1440', src)
        self.assertIn('("industry", "hk_index_daily"', src)
        self.assertIn("已知残差", src)

    def test_batch_and_stream_bounds(self):
        src = _read("hk_sector_index.py")
        self.assertIn("BATCH_CODES = 1000", src)
        self.assertIn("STREAM_CHUNK = 50_000", src)
        self.assertIn("WRITE_BATCH = 2000", src)
        self.assertIn("conn.commit()", src)
        self.assertIn("gc.collect()", src)
        self.assertIn("WHERE trade_date BETWEEN %s AND %s", src)
        self.assertIn("stock_code = ANY(%s)", src)
        self.assertLessEqual(hki.WRITE_BATCH, 50_000)
        self.assertLessEqual(slh.WRITE_BATCH, 50_000)

    def test_no_like_percent_literal_in_sql(self):
        """psycopg2 参数化 SQL 里的字面 % 必须双写；本模块改用 left() 规避。"""
        src = _read("hk_sector_index.py")
        self.assertNotIn("LIKE 'HK.%'", src)
        self.assertIn("left(stock_code, 3) = 'HK.'", src)

    def test_slice_guard_for_r3(self):
        src = _read("sector_layer_hk.py")
        self.assertIn("def _iter_slice_frames", src)
        self.assertIn("SLICE_DAYS = 10", src)
        self.assertIn("CHUNK_YEARS = 2", src)
        self.assertIn("del iv, sc, bz, sub", src)
        self.assertIn("gc.collect()", src)

    def test_index_points_incremental_continuation(self):
        src = _read("hk_sector_index.py")
        self.assertIn("def _refresh_index_points", src)
        self.assertIn("DISTINCT ON (sector_code)", src)
        self.assertIn("execute_values", src)
        self.assertIn("BASE_INDEX", src)

    def test_consistency_report_exists(self):
        src = _read("hk_sector_index.py")
        self.assertIn("def report_consistency", src)
        self.assertIn("up_ref", src)
        self.assertIn("--verify", src)


if __name__ == "__main__":
    unittest.main()
