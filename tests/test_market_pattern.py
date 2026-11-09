"""`BIZ.MARKET_PATTERN` 落地防回退测试（不连库）。

锁住 v1.32 的六件事：
  ① 六维 score 先按各维冻结切点转 band，绝不共用绝对 70/30；
  ② 缺失 band 一律 strict（含 `band_ne` 不得把 None 当「非高」）；
  ③ 最宽的规则「资金驱动」后置，不得遮蔽含方向的组合；
  ④ 规则表 / ECDF / 六维指纹进 params_hash，字典种子确定性；
  ⑤ state 恒当日 raw、level/score 恒 NULL，稳定性只由 status/dwell_days 标注；
  ⑥ 专属命名纪律与「标定脚本 == --seed」同一入口。
"""
import datetime
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_business_value as bb  # noqa: E402

D = datetime.date


def _cuts(p30=30.0, p70=70.0):
    return {"p30": p30, "p70": p70, "n": 1000,
            "quantiles": [float(i) for i in range(101)]}


def _params():
    ecdf = {m: {d: _cuts() for d in bb.PATTERN_DIMS}
            for m in bb.BUSINESS_MARKETS}
    return {
        "band_high_u": 0.70, "band_low_u": 0.30,
        "min_dims": 5, "confirm_days": 3,
        "dwell_semantics": "publish_day_run_length",
        "missing_semantics": "strict", "direction_policy": "inner_join_regime",
        "ecdf_calib_end": "2025-12-31", "ecdf": ecdf,
        "eq_vs_cap_extreme": {"low": 20.0, "high": 80.0},
        "pattern_priority": list(bb.PATTERN_PRIORITY),
        "pattern_rules": bb.PATTERN_RULES,
        "upstream_dim_params_hashes": {d: "hash" for d in bb.PATTERN_DIMS},
    }


def _frame(rows):
    """rows: list of dict(B,V,S,F,P,T,regime,eq_pctl,date)。"""
    out = []
    for r in rows:
        rec = {"trade_date": r["date"], "pattern_regime": r.get("regime"),
               "pattern_eq_pctl": r.get("eq_pctl")}
        for k in bb.PATTERN_LETTER_TO_DIM:
            rec[k] = r.get(k)
            rec["conf_" + k] = r.get("conf_" + k, "中")
        out.append(rec)
    return pd.DataFrame(out)


class TestBandAndRules(unittest.TestCase):

    def test_01_band_uses_per_dimension_cuts(self):
        """同一 score 在不同维切点下应得不同 band（禁止绝对 70/30）。"""
        self.assertEqual(bb.pattern_band(_cuts(), 80.0), "H")
        self.assertEqual(bb.pattern_band(_cuts(), 20.0), "L")
        self.assertEqual(bb.pattern_band(_cuts(), 50.0), "M")
        self.assertEqual(bb.pattern_band(_cuts(p30=85.0, p70=95.0), 80.0), "L")
        self.assertIsNone(bb.pattern_band(_cuts(), None))

    def test_02_u_of_is_bounded_and_monotone(self):
        q = _cuts()["quantiles"]
        self.assertEqual(bb.pattern_u_of(q, None), None)
        self.assertLessEqual(bb.pattern_u_of(q, -5), 0.01)
        self.assertGreaterEqual(bb.pattern_u_of(q, 500), 0.99)
        self.assertLess(bb.pattern_u_of(q, 30), bb.pattern_u_of(q, 70))

    def test_03_missing_is_strict(self):
        cond_eq = {"dim": "S", "band": "H"}
        cond_ne = {"dim": "F", "band_ne": "H"}
        bands = {"S": None, "F": None}
        self.assertFalse(bb.pattern_eval_condition(cond_eq, bands, "上行", {}))
        self.assertFalse(bb.pattern_eval_condition(cond_ne, bands, "上行", {}),
                         "缺失不得被当成「非高」")
        bands = {"S": "H", "F": "M"}
        self.assertTrue(bb.pattern_eval_condition(cond_eq, bands, "上行", {}))
        self.assertTrue(bb.pattern_eval_condition(cond_ne, bands, "上行", {}))

    def test_04_wide_rule_does_not_shadow_direction_rule(self):
        """F=H 且 S≠H 本会命中「资金驱动」，但「广泛上行」优先级更高。"""
        bands = {"B": "H", "V": "M", "S": "M", "F": "H", "P": "M"}
        hit = bb.pattern_first_match(bb.PATTERN_RULES, bands, "上行", {})
        self.assertEqual(hit["state"], "广泛上行")
        hit2 = bb.pattern_first_match(bb.PATTERN_RULES, bands, "震荡", {})
        self.assertEqual(hit2["state"], "资金驱动")

    def test_05_all_eight_states_covered(self):
        self.assertEqual(len(bb.PATTERN_PRIORITY), 8)
        self.assertEqual(bb.PATTERN_PRIORITY[-1], "常态")
        self.assertEqual([r["id"] for r in bb.PATTERN_RULES], list(range(1, 8)))
        self.assertEqual([r["state"] for r in bb.PATTERN_RULES],
                         list(bb.PATTERN_PRIORITY[:7]))


class TestDictRow(unittest.TestCase):

    def test_06_seed_is_deterministic_and_includes_ecdf(self):
        calib = {"calib_end": "2025-12-31", "n_obs": 123,
                 "ecdf": {m: {d: _cuts() for d in bb.PATTERN_DIMS}
                          for m in bb.BUSINESS_MARKETS}}
        hashes = {d: "h" + d for d in bb.PATTERN_DIMS}
        a = bb.pattern_build_dict_row(calib, hashes)
        b = bb.pattern_build_dict_row(calib, hashes)
        self.assertEqual(a["params_hash"], b["params_hash"])
        self.assertEqual(a["kind"], "rule_set")
        self.assertTrue(a["is_active"])
        self.assertIsInstance(a["inputs"], list)
        self.assertEqual(len(a["params"]["ecdf"]["CN"]), 6)
        self.assertEqual(a["params"]["pattern_rules"], bb.PATTERN_RULES)
        self.assertEqual(a["params"]["upstream_dim_params_hashes"], hashes)
        # 改规则 / 改切点都必须换指纹
        calib2 = dict(calib)
        e2 = {m: dict(v) for m, v in calib["ecdf"].items()}
        e2["CN"] = dict(e2["CN"])
        e2["CN"]["DIM.STRESS"] = _cuts(p30=31.0)
        calib2["ecdf"] = e2
        self.assertNotEqual(
            bb.pattern_build_dict_row(calib2, hashes)["params_hash"],
            a["params_hash"])

    def test_07_input_roles(self):
        row = bb.pattern_build_dict_row(
            {"calib_end": "2025-12-31", "n_obs": 1,
             "ecdf": {m: {d: _cuts() for d in bb.PATTERN_DIMS}
                      for m in bb.BUSINESS_MARKETS}},
            {d: "h" for d in bb.PATTERN_DIMS})
        roles = bb.pattern_input_roles(row)
        self.assertEqual(len(roles["bands"]), 5)
        self.assertEqual(roles["flag"], bb.PATTERN_FLAG_ATOM)
        self.assertEqual(roles["direction_axis"], bb.PATTERN_DIRECTION_BIZ)
        self.assertEqual(roles["evidence_only"], "DIM.TREND")


class TestCompute(unittest.TestCase):

    def test_08_compute_contract(self):
        rows = [
            dict(date=D(2026, 1, 5), B=50., V=80., S=80., F=50., P=80., T=50.,
                 regime="上行", eq_pctl=50.),                       # 高位脆弱
            dict(date=D(2026, 1, 6), B=80., V=50., S=50., F=80., P=50., T=50.,
                 regime="上行", eq_pctl=50.),                       # 广泛上行（非资金驱动）
            dict(date=D(2026, 1, 7), B=50., V=50., S=50., F=50., P=50., T=50.,
                 regime="震荡", eq_pctl=50.),                       # 常态
            dict(date=D(2026, 1, 8), B=50., V=50., S=50., F=50., P=50., T=50.,
                 regime=None, eq_pctl=50.),                         # 方向缺失 → 不发布
            dict(date=D(2026, 1, 9), B=50., V=50., S=50., F=50., P=50., T=None,
                 regime="震荡", eq_pctl=50.),                       # 5 维仍可发布
        ]
        out, stats = bb.pattern_compute(_frame(rows), "CN", _params(), "abc123")
        self.assertEqual(len(out), 4)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual([r["state"] for r in out],
                         ["高位脆弱", "广泛上行", "常态", "常态"])
        self.assertEqual([r["dwell_days"] for r in out], [1, 1, 1, 2])
        self.assertEqual(out[3]["status"], "待确认")
        self.assertEqual(out[3]["since_date"], D(2026, 1, 7))
        for r in out:
            self.assertIsNone(r["level"])
            self.assertIsNone(r["score"])
            self.assertEqual(set(r["detail"]), set(bb.PATTERN_OUTPUT_SCHEMA["detail_keys"]))
            self.assertEqual(r["model_version"], "abc123")
        self.assertIn("DIM.TREND", out[3]["detail"]["missing_dims"])
        self.assertEqual(out[0]["detail"]["matched_rules"], [1])
        self.assertEqual(len(out[0]["detail"]["dominant_driver"]), 2)

    def test_09_min_dims_gate(self):
        rows = [dict(date=D(2026, 1, 5), B=80., V=50., S=None, F=None,
                     P=50., T=50., regime="上行", eq_pctl=50.)]
        out, stats = bb.pattern_compute(_frame(rows), "CN", _params(), "abc")
        self.assertEqual(out, [])
        self.assertEqual(stats["rows"], 0)

    def test_10_confidence_and_low_conf_disclosure(self):
        rows = [dict(date=D(2026, 1, 5), B=80., V=50., S=50., F=50., P=80., T=50.,
                     regime="上行", eq_pctl=50., conf_S="低")]
        out, _ = bb.pattern_compute(_frame(rows), "CN", _params(), "abc")
        self.assertEqual(out[0]["confidence"], "低")
        self.assertIn("DIM.SENTIMENT", out[0]["detail"]["evidence"]["low_conf_dims"])


class TestSourceDiscipline(unittest.TestCase):

    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_11_naming_and_wiring(self):
        for name in ("pattern_calibrate", "pattern_build_dict_row",
                     "pattern_load_frame", "pattern_compute",
                     "build_pattern_business", "pattern_u_of", "pattern_band",
                     "pattern_first_match"):
            self.assertTrue(callable(getattr(bb, name, None)), f"缺少 {name}")
        self.assertEqual(bb.SEEDABLE_BUSINESS_CODES[:2],
                         (bb.REGIME_BIZ_CODE, bb.PATTERN_BIZ_CODE))
        self.assertIn(bb.DIVERGENCE_BIZ_CODE, bb.SEEDABLE_BUSINESS_CODES)
        src = self._read("build_business_value.py")
        self.assertIn("elif biz_code == PATTERN_BIZ_CODE:", src)
        self.assertIn("build_pattern_business(conn, config, market, write)", src)

    def test_12_calibrator_uses_same_dict_builder(self):
        src = self._read("tools/calibrate_pattern.py")
        self.assertIn("pattern_build_dict_row", src)
        self.assertIn("pattern_calibrate", src)


if __name__ == "__main__":
    unittest.main()
