"""财务报告期维度 P1 防回退测试（doc/financial_period_basis_refactor.md §8 P1）。

纯源码 + 纯函数断言为主； annotate_group_level 用合成 DataFrame 验证 D7 分组回退。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestSchemaView(unittest.TestCase):
    def test_schema_declares_analysis_period_view(self):
        src = _src("sql/schema.sql")
        self.assertIn("CREATE OR REPLACE VIEW v_financial_analysis_period AS", src)
        self.assertIn("COMMENT ON VIEW v_financial_analysis_period", src)

    def test_view_exposes_both_column_families(self):
        src = _src("sql/schema.sql")
        start = src.index("CREATE OR REPLACE VIEW v_financial_analysis_period AS")
        end = src.index("COMMENT ON VIEW v_financial_analysis_period", start)
        ddl = src[start:end]
        for col in ("revenue_ytd", "np_ytd", "ocf_ytd",
                    "revenue_q", "np_q", "ocf_q",
                    "report_basis", "period_months", "roe",
                    "prev_comparable_date"):
            with self.subTest(col=col):
                self.assertIn(col, ddl)

    def test_view_per_period_not_per_stock(self):
        """视图必须是每票每期一行，D8 选期在加载函数做。"""
        src = _src("sql/schema.sql")
        start = src.index("CREATE OR REPLACE VIEW v_financial_analysis_period AS")
        end = src.index("COMMENT ON VIEW v_financial_analysis_period", start)
        ddl = src[start:end]
        self.assertNotIn("DISTINCT ON (stock_code)", ddl)
        self.assertIn("FROM tagged", ddl)
        self.assertIn("q.period_type AS quarter_type", ddl)

    def test_roe_not_annualized_in_view(self):
        ddl = _src("sql/schema.sql")
        start = ddl.index("CREATE OR REPLACE VIEW v_financial_analysis_period AS")
        end = ddl.index("COMMENT ON VIEW v_financial_analysis_period", start)
        body = ddl[start:end]
        self.assertIn("f.roe::float8              AS roe", body)
        self.assertNotIn("roe_ann", body)


class TestLoaderContract(unittest.TestCase):
    def test_loader_exists_and_uses_view_with_bounds(self):
        src = _src("profiling/quantile.py")
        self.assertIn("def load_financial_period(conn, as_of: date)", src)
        self.assertIn("FROM v_financial_analysis_period", src)
        self.assertIn("WHERE report_date <= %s AND report_date >= %s", src)
        self.assertIn("_FIN_PERIOD_MIN_GROUP = 20", src)
        self.assertIn("_FIN_PERIOD_STALE_DAYS = 15 * 30", src)

    def test_prior_period_join_not_shifted(self):
        """去年同期对齐：prev 期末日直接等于 df 的 prev_comparable_date，不再平移。"""
        src = _src("profiling/quantile.py")
        self.assertIn("on=[\"stock_code\", \"report_type\", \"report_basis\",", src)
        self.assertNotIn("+ pd.DateOffset(years=1)", src)

    def test_cache_cleared(self):
        src = _src("profiling/quantile.py")
        self.assertIn("_FIN_PERIOD_CACHE.clear()", src)
        # clear_caches 内必须也清
        tail = src[src.index("def clear_caches()"):]
        self.assertIn("_FIN_PERIOD_CACHE.clear()", tail)


class TestP2TagHelpers(unittest.TestCase):
    def test_period_yoy_quarterly_vs_accumulated(self):
        from profiling.quantile import period_yoy
        df = pd.DataFrame({
            "report_basis": ["quarterly", "semi_annual", "quarterly"],
            "revenue_q": [2073.36, np.nan, 100.0],
            "py_revenue_q": [1928.69, np.nan, -5.0],  # 第三行基期<=0 → NaN
            "revenue_ytd": [6085.79, 4012.43, 1000.0],
            "py_revenue_ytd": [5573.95, 3645.26, 900.0],
        })
        y = period_yoy(df, "revenue")
        self.assertAlmostEqual(y.iloc[0], (2073.36 / 1928.69 - 1) * 100, places=4)
        self.assertAlmostEqual(y.iloc[1], (4012.43 / 3645.26 - 1) * 100, places=4)
        self.assertTrue(pd.isna(y.iloc[2]))  # 基期<=0 不打标

    def test_cash_conversion_dual_caliber(self):
        from profiling.quantile import cash_conversion
        df = pd.DataFrame({
            "np_q": [636.12, -10.0, 100.0],
            "ocf_q": [26.52, 5.0, np.nan],
            "np_ytd": [1777.27, 100.0, 500.0],
            "ocf_ytd": [1567.13, 80.0, 400.0],
        })
        sq, ac = cash_conversion(df)
        self.assertAlmostEqual(sq.iloc[0], 26.52 / 636.12 * 100, places=4)
        self.assertAlmostEqual(ac.iloc[0], 1567.13 / 1777.27 * 100, places=4)
        self.assertTrue(pd.isna(sq.iloc[2]))  # ocf_q 缺失

    def test_bumped_versions(self):
        from profiling import registry
        import profiling.tags.quality  # noqa: F401  注册标签（导入即生效）
        import profiling.tags.growth   # noqa: F401
        v3 = ("qal_roe_ind1_tier", "qal_leverage_ind1_tier",
              "qal_earnings_quality_ind1", "grw_revenue_yoy_ind1_tier",
              "grw_profit_yoy_ind1_tier", "grw_growth_accel")
        for code in v3:
            with self.subTest(code=code):
                self.assertEqual(registry.get(code).version, "v3")
        for code in ("qal_profit_years", "grw_growth_persistence"):
            self.assertEqual(registry.get(code).version, "v1")
        for code in ("qal_roe_stability_ind1", "qal_gross_margin_ind1_tier"):
            self.assertEqual(registry.get(code).version, "v2")

    def test_detail_has_top_level_report_basis(self):
        from datetime import date
        from profiling.conclusions import producer as P
        d = P._detail("X", {"a": 1}, 1.0, [], {"G1": True}, date(2026, 11, 25),
                      with_run=False,
                      primary_report_period={"report_type": "Q3",
                                             "report_date": "2026-09-30",
                                             "report_basis": "quarterly"})
        self.assertEqual(d["report_basis"], "quarterly")
        self.assertEqual(d["primary_report_period"]["report_basis"], "quarterly")

    def test_roe_floor_by_report_type(self):
        from profiling.conclusions import producer as P, rules as R
        spec = R.RULES["dim_quality_conclusion"]
        self.assertEqual(spec["params"]["roe_floor_pct_by_report_type"],
                         {"Q1": 0.43, "H1": 2.23, "Q3": 3.27, "annual": 1.36})
        self.assertNotIn("roe_floor_pct", spec["params"])
        self.assertIn("roe_floor_ok", {d["field"] for d in spec["derived_fields"]})
        # 派生：Q1 的 0.5% 过线（floor 0.43），Q3 的 2.0% 不过线（floor 3.27）
        vals = {"report_type": "Q1", "roe_raw": 0.5}
        P._derive(spec, vals)
        self.assertTrue(vals["roe_floor_ok"])
        vals2 = {"report_type": "Q3", "roe_raw": 2.0}
        P._derive(spec, vals2)
        self.assertFalse(vals2["roe_floor_ok"])

    def test_aux_not_in_g1_denominator(self):
        from profiling.conclusions import producer as P, rules as R
        spec = R.RULES["dim_quality_conclusion"]
        # 构造缺 report_type 但其余齐全的快照：coverage 仍应为 1.0
        snap = {"T": {("qal_roe_ind1_tier", "key_value"): "4",
                      ("qal_roe_ind1_tier", "num_value"): 10.0,
                      ("qal_roe_stability_ind1", "key_value"): "2",
                      ("qal_earnings_quality_ind1", "key_value"): "4",
                      ("qal_leverage_ind1_tier", "key_value"): "2"}}
        extras = {"period_auto": {}, "period_annual": {}, "fcf": {}, "ma": {}}
        values, cov, missing, _p, _pr = P._build_values(spec, "T", snap, extras, {})
        self.assertEqual(cov, 1.0)
        self.assertIn("report_type", missing)

    def test_view_marks_q2_as_quarterly(self):
        """Q2（H1 拆解）必须归 quarterly，否则加速度会跳过 Q2。"""
        src = _src("sql/schema.sql")
        self.assertIn("q.period_type IN ('Q1','Q2','Q3','Q4')", src)
        self.assertIn("q.period_type AS quarter_type", src)


class TestGroupFallback(unittest.TestCase):
    def _frame(self, groups: dict[str, int]) -> pd.DataFrame:
        rows = []
        for grp, n in groups.items():
            mkt, ind, basis = grp.split("|")
            for i in range(n):
                rows.append({"stock_code": f"{mkt}.{grp}{i:04d}",
                             "market": mkt, "ind1": ind, "report_basis": basis})
        return pd.DataFrame(rows)

    def test_industry_level_when_group_ge_20(self):
        from profiling.quantile import assign_group_level
        df = self._frame({"A|IND1|quarterly": 25})
        out = assign_group_level(df)
        self.assertEqual(set(out["peer_level"]), {"industry_l1"})
        self.assertTrue(out["grp_l1"].notna().all())
        self.assertTrue((out["grp_l1"] == "A|IND1|quarterly").all())

    def test_fallback_to_market_when_group_lt_20(self):
        from profiling.quantile import assign_group_level
        df = self._frame({"A|IND1|quarterly": 8, "A|IND2|quarterly": 30})
        out = assign_group_level(df)
        small = out[out["ind1"] == "IND1"]
        large = out[out["ind1"] == "IND2"]
        self.assertEqual(set(small["peer_level"]), {"market"})
        self.assertTrue((small["grp_l1"] == "A|quarterly").all())
        self.assertEqual(set(large["peer_level"]), {"industry_l1"})

    def test_no_publish_when_market_group_lt_5(self):
        from profiling.quantile import assign_group_level
        df = self._frame({"HK|IND1|annual": 3})
        out = assign_group_level(df)
        self.assertTrue(out["grp_l1"].isna().all())

    def test_secondary_industry_when_available(self):
        from profiling.quantile import assign_group_level
        df = self._frame({"A|IND1|quarterly": 25})
        df["ind2"] = "SUB1"
        out = assign_group_level(df)
        self.assertTrue((out["grp_l2"] == "A|SUB1|quarterly").all())


if __name__ == "__main__":
    unittest.main()
