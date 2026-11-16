"""第④层「行业配置」阶段 4 验证工作台测试：统计口径（纯函数）+ 工程红线。

不连库：全部为纯函数 / 静态文本断言，任何环境都能跑。
背景：doc/industry_allocation_feasibility.md §1.3 G4 / §3.7 / §4.2 阶段 4。
本期四项用户决策（只做能判定的 5 项 / 只出报告不动生产代码 / tools+doc 落点 /
HK 描述性敏感性）均在测试里钉住，防静默回退。
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
sys.path.insert(0, os.path.join(ROOT, "tools"))

import validate_sector as vs  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _close_wide():
    idx = pd.bdate_range("2026-01-05", periods=12).date
    return pd.DataFrame({
        "A": np.arange(1, 13, dtype=float),
        "B": np.arange(1, 13, dtype=float) * 2,
        "C": np.arange(1, 13, dtype=float) * 3,
    }, index=idx)


class TestForwardExcess(unittest.TestCase):
    """M3：基准是当日有效行业等权均值，不是大盘指数"""

    def test_shift_on_trading_day_grid(self):
        w = _close_wide()
        out = vs.forward_excess(w, 2)
        # A: 3/1-1 = 2；B: 6/2-1 = 2；C: 9/3-1 = 2 → 横截面去均值后全 0
        self.assertAlmostEqual(float(out.iloc[0].abs().max()), 0.0, places=6)

    def test_cross_section_mean_is_zero_with_nans(self):
        w = _close_wide()
        w.loc[w.index[0], "C"] = np.nan       # rank_n 时变：当日只有 2 个有效行业
        out = vs.forward_excess(w, 2)
        self.assertAlmostEqual(float(out.iloc[2].mean()), 0.0, places=6)
        self.assertTrue(pd.isna(out.loc[out.index[-1], "A"]))  # 末 N 日无未来收益

    def test_no_market_index_reference(self):
        src = _read("tools/validate_sector.py")
        self.assertIn("fwd.sub(fwd.mean(axis=1), axis=0)", src)
        self.assertNotIn("SH.000001", src)
        self.assertNotIn("HK.800000", src)


class TestQuintilesAndIC(unittest.TestCase):
    def _panel(self, n=15, days=8, slope=1.0):
        rows = []
        for d in pd.bdate_range("2026-01-05", periods=days).date:
            for i in range(n):
                x = float(i)
                rows.append(dict(trade_date=d, sector_code=f"S{i:02d}",
                                 state_side=x,
                                 fwd_20=slope * x + (i % 3) * 0.01))
        return pd.DataFrame(rows)

    def test_quintiles_are_balanced(self):
        p = self._panel()
        q = vs.assign_quintiles(p)
        self.assertEqual(int(q.min()), 1)
        self.assertEqual(int(q.max()), 5)
        self.assertEqual(q.value_counts().nunique(), 1)  # 15 行业 → 每组 3 个

    def test_quintiles_need_min_xsec(self):
        p = self._panel(n=8)
        self.assertTrue(vs.assign_quintiles(p).isna().all())

    def test_ic_perfect_positive_and_negative(self):
        pos = vs.ic_by_date(self._panel(slope=1.0), "state_side", "fwd_20")
        neg = vs.ic_by_date(self._panel(slope=-1.0), "state_side", "fwd_20")
        self.assertEqual(len(pos), 8)
        self.assertGreater(float(pos.mean()), 0.99)
        self.assertLess(float(neg.mean()), -0.99)

    def test_ic_skips_small_cross_section(self):
        self.assertTrue(vs.ic_by_date(self._panel(n=8), "state_side", "fwd_20").empty)

    def test_fast_ic_matches_scipy(self):
        p = self._panel(slope=0.5)
        p["_x"], p["_y"] = p["state_side"], p["fwd_20"]
        slow = vs.ic_by_date(p, "_x", "_y")
        fast = vs._fast_daily_ic(p[["trade_date", "_x", "_y"]])
        pd.testing.assert_series_equal(slow.sort_index(), fast.sort_index(),
                                       check_names=False, atol=1e-9)


class TestBootstrap(unittest.TestCase):
    def _df(self):
        rng = np.random.default_rng(3)
        rows = []
        for d in pd.bdate_range("2026-01-05", periods=60).date:
            for i in range(20):
                x = rng.normal()
                rows.append(dict(trade_date=d, sector_code=f"S{i:02d}",
                                 _x=x, _y=0.3 * x + rng.normal() * 0.8))
        return pd.DataFrame(rows)

    def test_reproducible_and_ci_brackets_mean(self):
        d = self._df()
        a = vs.two_dim_block_bootstrap(d, 20, np.random.default_rng(42))
        b = vs.two_dim_block_bootstrap(d, 20, np.random.default_rng(42))
        self.assertEqual(a, b)
        self.assertGreater(a["n_blocks"], 3)
        self.assertLessEqual(a["ci_low"], a["mean"])
        self.assertLessEqual(a["mean"], a["ci_high"])

    def test_blocks_are_sector_by_month(self):
        src = _read("tools/validate_sector.py")
        self.assertIn('groupby(["sector_code", "_month"], sort=False).indices', src)
        self.assertIn("np.concatenate([block_idx[int(j)]", src)
        self.assertNotIn("pd.concat([blocks[", src)  # R3：禁止物化上千个 DataFrame 块

    def test_too_few_blocks_returns_none(self):
        # 1 个行业 × 1 个月 = 1 块，低于最少 3 块门槛
        d = self._df()
        d = d[d["sector_code"] == "S00"].head(20)
        self.assertIsNone(vs.two_dim_block_bootstrap(d, 5, np.random.default_rng(1)))


class TestConstruct(unittest.TestCase):
    def test_cronbach_alpha_perfect_and_independent(self):
        n = 200
        base = pd.Series(np.arange(n, dtype=float))
        perfect = pd.DataFrame({"a": base, "b": base + 1, "c": base * 2})
        self.assertAlmostEqual(vs.cronbach_alpha(perfect), 1.0, places=6)
        rng = np.random.default_rng(7)
        indep = pd.DataFrame(rng.normal(size=(n, 4)), columns=list("abcd"))
        self.assertLess(abs(vs.cronbach_alpha(indep)), 0.2)
        self.assertTrue(math.isnan(vs.cronbach_alpha(perfect[["a"]])))
        self.assertTrue(math.isnan(vs.cronbach_alpha(perfect.head(10))))

    def test_pc1_rejects_nan_corr_matrix(self):
        """首版用 nan_to_num(0) 会产出 PC1=1/k 的伪结果，必须判不可估计。"""
        d = pd.DataFrame({"a": [1.0, np.nan, np.nan], "b": [np.nan, 2.0, np.nan]})
        out = vs.pc1_stats(d, ["a", "b"])
        self.assertEqual(out["status"], "INSUFFICIENT_OVERLAP")
        self.assertIsNone(out["pc1_var"])
        self.assertEqual(out["loadings"], {})

    def test_pc1_perfect_correlation(self):
        d = pd.DataFrame({"a": np.arange(300, dtype=float),
                          "b": np.arange(300, dtype=float) * 2})
        out = vs.pc1_stats(d, ["a", "b"])
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["median_corr"], 1.0, places=5)
        self.assertAlmostEqual(out["pc1_var"], 1.0, places=5)

    def test_single_anchor_dimensions_are_not_applicable(self):
        src = _read("tools/validate_sector.py")
        self.assertIn('status="NOT_APPLICABLE"', src)
        self.assertIn("单锚（成员=1）/ 规则判定维度无成员集合", src)


class TestDecisionGate(unittest.TestCase):
    """§3.7 决策闸门：不通过 → 等权 + 状态描述，且不改生产代码"""

    def test_pass_requires_both_conditions(self):
        ok = vs.decision_gate(0.02, 0.9, 0.01)
        self.assertTrue(ok["passed"])
        self.assertIn("有效性已验证的状态排序", ok["action"])
        for args in ((-0.01, 0.9, 0.01), (0.02, -0.5, 0.01),
                     (0.02, 0.9, 0.4), (None, 0.9, 0.01)):
            with self.subTest(args=args):
                self.assertFalse(vs.decision_gate(*args)["passed"])

    def test_fail_action_keeps_equal_weight_and_no_code_change(self):
        act = vs.decision_gate(-0.01, 0.0, 0.9)["action"]
        for token in ("状态描述", "等权", "不得宣称有效", "不改任何生产代码"):
            with self.subTest(token=token):
                self.assertIn(token, act)
        self.assertNotIn("超配", act)
        self.assertNotIn("买入", act)

    def test_not_evaluable_is_explicit(self):
        r = vs.not_evaluable("x", 45, 250, "短样本", "监控")
        self.assertEqual(r["status"], "NOT_EVALUABLE")
        self.assertIn("monitor", r)


class TestEngineeringGuards(unittest.TestCase):
    """只读 / R7 下界 / 报告落点 / 不改生产代码"""

    def test_script_is_read_only(self):
        src = _read("tools/validate_sector.py")
        for banned in ("bulk_upsert", "conn.commit()", "INSERT INTO",
                       "UPDATE regime", "DELETE FROM", "--write"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, src)
        self.assertIn("from db import get_conn, read_sql", src)
        self.assertIn("不写库", src)

    def test_all_queries_have_lower_bound(self):
        src = _read("tools/validate_sector.py")
        self.assertGreaterEqual(src.count("BETWEEN %s AND %s"), 6)
        self.assertIn("trade_date >= %s", src)
        self.assertIn("indicator_code = ANY(%s)", src)

    def test_report_and_design_doc_exist(self):
        rep = _read("doc/sector_validation_report.md")
        design = _read("doc/industry_allocation_feasibility.md")
        for token in ("不通过", "不可判定", "等权", "IC", "bootstrap", "Cronbach"):
            with self.subTest(token=token):
                self.assertIn(token, rep)
        self.assertIn("阶段 4 · 验证闭环", design)
        self.assertIn("阶段 4 实施记录", design)

    def test_no_production_code_changes_declared(self):
        rep = _read("doc/sector_validation_report.md")
        self.assertIn("不改", rep)
        src = _read("tools/validate_sector.py")
        self.assertIn("不改任何生产代码 / 字典 / params_hash / 历史读数", src)

    def test_hk_sensitivity_is_descriptive_only(self):
        src = _read("tools/validate_sector.py")
        self.assertIn("by_min_constituents", src)
        self.assertIn("by_winsorize", src)
        self.assertIn("不改生产口径", src)
        self.assertNotIn("ALTER TABLE industry.hk_index_daily", src)

    def test_data_gates_cover_three_items(self):
        src = _read("tools/validate_sector.py")
        self.assertIn("拥挤闸门有效性", src)
        self.assertIn("快照类时序分位启用", src)
        self.assertIn("HK 排序有效性", src)
        self.assertIn("min_periods=250", src)

    def test_pit_bias_includes_full_history(self):
        src = _read("tools/validate_sector.py")
        self.assertIn("full_history_segments", src)
        self.assertIn("dt.date(1999, 12, 30)", src)


if __name__ == "__main__":
    unittest.main()
