"""BIZ.MARKET_DIVERGENCE 落地防回退测试（不连库）。

锁住 v1.33 的六件事：
  ① 六维 score 先按各维冻结 P30/P70 转 L/M/H，**绝不跨维分位相减**；
  ② 成对判据是序数档位差 delta∈{0,1,2}，结构对只有 0/2；
  ③ 输入缺失 strict 退出（记 missing_pairs），不填中性、不判「一致」；
  ④ state 按归一强度 Σdelta/(2×n_valid) 分档，score 保留命中计数；
  ⑤ min_pairs / min_distinct_dims 双门禁，切点 / 对清单 / 六维指纹进 params_hash；
  ⑥ 专属命名纪律与「标定脚本 == --seed」同一入口。
"""
import copy
import datetime
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_business_value as bb  # noqa: E402

D = datetime.date
DIMS = bb.DIVERGENCE_DIMS


def _cuts(p30=30.0, p70=70.0):
    return {"p30": p30, "p70": p70, "n": 1000}


def _params():
    return {
        "band_low_u": 0.30, "band_high_u": 0.70,
        "min_pairs": 4, "min_distinct_dims": 3,
        "publish_policy": "pair_level_degrade",
        "state_rule": "normalized_strength", "score_rule": "hit_count",
        "calib_end": "2025-12-31",
        "band_cuts": {m: {d: _cuts() for d in DIMS}
                      for m in bb.BUSINESS_MARKETS},
        "strength_bands": [0.15, 0.30, 0.50],
        "state_labels": list(bb.DIVERGENCE_STATE_LABELS),
        "eq_vs_cap_extreme": {"low": 20.0, "high": 80.0},
        "structural_flag_texts": bb.DIVERGENCE_FLAG_TEXTS,
        "pair_rules": copy.deepcopy(bb.DIVERGENCE_PAIRS),
        "upstream_dim_params_hashes": {d: "h" + d for d in DIMS},
    }


_SHORT_TO_DIM = {"b": DIMS[0], "v": DIMS[1], "s": DIMS[2],
                 "f": DIMS[3], "p": DIMS[4], "t": DIMS[5]}


def _frame(rows):
    out = []
    for r in rows:
        rec = {"trade_date": r["date"], "div_eq_pctl": r.get("eq", 50.0),
               "div_regime": r.get("regime", "震荡"),
               "div_flags": r.get("flags", {"short_fade": False,
                                            "mid_unrepaired": False})}
        for short, dim_code in _SHORT_TO_DIM.items():
            rec[dim_code] = r.get(short, 50.0)
        out.append(rec)
    return pd.DataFrame(out)


def _scores(b=50., v=50., s=50., f=50., p=50., t=50.):
    return {DIMS[0]: b, DIMS[1]: v, DIMS[2]: s,
            DIMS[3]: f, DIMS[4]: p, DIMS[5]: t}


class TestBandAndPairs(unittest.TestCase):

    def test_01_band_uses_per_dimension_cuts(self):
        self.assertEqual(bb.divergence_band(_cuts(), 80.0), "H")
        self.assertEqual(bb.divergence_band(_cuts(), 20.0), "L")
        self.assertEqual(bb.divergence_band(_cuts(), 50.0), "M")
        self.assertEqual(bb.divergence_band(_cuts(p30=85., p70=95.), 80.0), "L")
        self.assertIsNone(bb.divergence_band(_cuts(), None))

    def test_02_delta_is_ordinal_gap_not_percentile_difference(self):
        p = _params()
        cuts = p["band_cuts"]["CN"]
        scores = _scores(s=80., f=20., b=50., t=50.)
        bands = {d: bb.divergence_band(cuts[d], scores[d]) for d in DIMS}
        pairs, missing = bb.divergence_evaluate_pairs(
            bands, scores, 50.0, "震荡",
            {"short_fade": False, "mid_unrepaired": False}, p)
        got = {x["id"]: x["delta"] for x in pairs}
        self.assertEqual(missing, [])
        self.assertEqual(len(pairs), 6)
        self.assertEqual(got["D2"], 2)          # S=H(80) vs F=L(20)
        self.assertEqual(got["D6"], 1)          # B=M vs S=H
        self.assertEqual(got["D3"], 0)
        self.assertTrue(all(isinstance(x, int) and 0 <= x <= 2
                            for x in got.values()))
        # 原始分位差 60 分位，序数差只能是 2 —— 不复用绝对阈值
        self.assertNotEqual(got["D2"], abs(80. - 20.))

    def test_03_structural_pairs_and_missing_are_strict(self):
        p = _params()
        cuts = p["band_cuts"]["CN"]
        scores = _scores(b=20., p=80.)
        bands = {d: bb.divergence_band(cuts[d], scores[d]) for d in DIMS}
        pairs, _ = bb.divergence_evaluate_pairs(
            bands, scores, 10.0, "上行",
            {"short_fade": True, "mid_unrepaired": False}, p)
        got = {x["id"]: x for x in pairs}
        self.assertTrue(got["D1"]["hit"])       # eq<=20 且 BREADTH=L
        self.assertTrue(got["D4"]["hit"])       # STRESS=H 且 上行
        self.assertTrue(got["D5"]["hit"])       # 短窗衰竭文案
        self.assertEqual(got["D1"]["delta"], 2)

        # 缺 FLOW → D2 退出；缺方向 → D4 退出；缺 flags → D5 退出
        scores2 = dict(scores)
        scores2[DIMS[3]] = None
        bands2 = {d: bb.divergence_band(cuts[d], scores2[d]) for d in DIMS}
        pairs2, missing2 = bb.divergence_evaluate_pairs(
            bands2, scores2, 50.0, None, None, p)
        self.assertIn("D2", missing2)
        self.assertIn("D4", missing2)
        self.assertIn("D5", missing2)
        self.assertEqual(len(pairs2), 3)
        self.assertNotIn("D2", {x["id"] for x in pairs2})

    def test_04_dominant_pair_has_no_none_comparison(self):
        """结构型对 delta 也是整数，max(delta) 不再出现 None 比较 TypeError。"""
        p = _params()
        rows, _ = bb.divergence_compute(
            _frame([dict(date=D(2026, 1, 5))]), "CN", p, "abc123")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["detail"]["dominant_pair"])  # 无命中
        self.assertTrue(all(isinstance(x["delta"], int)
                            for x in rows[0]["detail"]["pairs"]))


class TestComputeContract(unittest.TestCase):

    def test_05_normalized_strength_and_count_score(self):
        p = _params()
        rows, stats = bb.divergence_compute(_frame([
            dict(date=D(2026, 1, 5), s=80., f=20.),   # D2=2, D6=1 → Σ=3
            dict(date=D(2026, 1, 6)),                 # 全档位一致 → Σ=0
        ]), "CN", p, "h1")
        self.assertEqual(len(rows), 2)
        first = rows[0]["detail"]
        self.assertEqual(first["n_valid_pairs"], 6)
        self.assertEqual(rows[0]["score"], 2)         # D2 + D6 命中
        self.assertAlmostEqual(first["strength"], 3 / 12)
        self.assertEqual((rows[0]["state"], rows[0]["level"]),
                         ("轻度背离", 2))
        self.assertEqual(rows[0]["detail"]["dominant_pair"], "D2")
        self.assertEqual((rows[1]["state"], rows[1]["level"]), ("一致", 1))
        self.assertEqual(rows[1]["score"], 0)
        for r in rows:
            self.assertEqual(set(r["detail"]),
                             set(bb.DIVERGENCE_OUTPUT_SCHEMA["detail_keys"]))
            self.assertEqual(r["model_version"], "h1")
            self.assertIsNone(r["since_date"])
            self.assertIsNone(r["dwell_days"])
        self.assertEqual(stats["rows"], 2)
        self.assertEqual(stats["zero_bands"], ["显著背离", "多重背离"])

    def test_06_min_pairs_gate(self):
        p = _params()
        row = dict(date=D(2026, 1, 5), f=None, t=None)
        rows, stats = bb.divergence_compute(_frame([row]), "CN", p, "h")
        # D2 / D3 退出；D1、D4、D5、D6 仍有效 → 4 对，恰好达标
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detail"]["n_valid_pairs"], 4)
        row2 = dict(date=D(2026, 1, 6), f=None, t=None, p=None, v=None)
        rows2, stats2 = bb.divergence_compute(_frame([row2]), "CN", p, "h")
        self.assertEqual(rows2, [])                   # 有效对数 < 4 → 整行不发布
        self.assertEqual(stats2["rows"], 0)

    def test_07_min_distinct_dims_gate(self):
        p = _params()
        p["min_pairs"] = 3
        row = dict(date=D(2026, 1, 5), f=None, t=None, p=None, v=None)
        # 仅 D1(B) / D5(B) / D6(B,S) 有效 → 3 对，但只涉及 2 个维度
        rows, _ = bb.divergence_compute(_frame([row]), "CN", p, "h")
        self.assertEqual(rows, [])

    def test_08_degraded_strength_is_normalized_by_valid_pairs(self):
        """归一：退出对既不进分子也不进分母，不得按固定 6 对稀释。"""
        p = _params()
        rows, _ = bb.divergence_compute(_frame([
            dict(date=D(2026, 1, 5), f=None, t=None, s=80.),
        ]), "CN", p, "h")
        d = rows[0]["detail"]
        self.assertEqual(d["n_valid_pairs"], 4)
        self.assertAlmostEqual(d["strength"],
                               sum(x["delta"] for x in d["pairs"]) / 8)
        self.assertAlmostEqual(d["hit_ratio"], rows[0]["score"] / 4)


class TestDictRowAndWiring(unittest.TestCase):

    def _calib(self):
        return {"calib_end": "2025-12-31", "n_obs": 123,
                "band_cuts": {m: {d: _cuts() for d in DIMS}
                              for m in bb.BUSINESS_MARKETS}}

    def test_09_seed_deterministic_and_hash_sensitive(self):
        calib, hashes = self._calib(), {d: "h" + d for d in DIMS}
        a = bb.divergence_build_dict_row(calib, hashes)
        b = bb.divergence_build_dict_row(calib, hashes)
        self.assertEqual(a["params_hash"], b["params_hash"])
        self.assertEqual(a["kind"], "rule_set")
        self.assertTrue(a["is_active"])
        self.assertEqual(len(a["params"]["band_cuts"]["CN"]), 6)
        self.assertEqual(a["params"]["pair_rules"], bb.DIVERGENCE_PAIRS)
        c2 = copy.deepcopy(calib)
        c2["band_cuts"]["CN"][DIMS[0]] = _cuts(p30=31.)
        self.assertNotEqual(
            bb.divergence_build_dict_row(c2, hashes)["params_hash"],
            a["params_hash"], "改切点必须换指纹")
        p2 = copy.deepcopy(a["params"])
        p2["pair_rules"] = p2["pair_rules"][:-1]
        import hashlib
        import json
        payload = {"biz_code": a["biz_code"], "kind": a["kind"],
                   "inputs": a["inputs"], "params": p2,
                   "output_schema": a["output_schema"]}
        h2 = hashlib.md5(json.dumps(payload, sort_keys=True,
                                    ensure_ascii=False).encode("utf-8")
                         ).hexdigest()[:16]
        self.assertNotEqual(h2, a["params_hash"], "改对清单必须换指纹")

    def test_10_input_roles(self):
        row = bb.divergence_build_dict_row(self._calib(),
                                           {d: "h" for d in DIMS})
        roles = bb.divergence_input_roles(row)
        self.assertEqual(len(roles["bands"]), 6)
        self.assertEqual(roles["eq_extreme"], bb.DIVERGENCE_FLAG_ATOM)
        self.assertEqual(roles["direction_axis"], bb.DIVERGENCE_DIRECTION_BIZ)

    def test_11_naming_and_wiring(self):
        for name in ("divergence_calibrate", "divergence_build_dict_row",
                     "divergence_load_frame", "divergence_compute",
                     "build_divergence_business", "divergence_band",
                     "divergence_evaluate_pairs"):
            self.assertTrue(callable(getattr(bb, name, None)), f"缺少 {name}")
        self.assertIn(bb.DIVERGENCE_BIZ_CODE, bb.SEEDABLE_BUSINESS_CODES)
        with open(os.path.join(ROOT, "build_business_value.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("elif biz_code == DIVERGENCE_BIZ_CODE:", src)
        self.assertIn("build_divergence_business(conn, config, market, write)", src)
        with open(os.path.join(ROOT, "tools", "calibrate_divergence.py"),
                  encoding="utf-8") as f:
            cal = f.read()
        self.assertIn("divergence_build_dict_row", cal)
        self.assertIn("divergence_calibrate", cal)

    def test_12_no_absolute_pair_threshold_in_params(self):
        """撤销原规格的 pair_thresholds / divergence_bands 计数切档。"""
        row = bb.divergence_build_dict_row(self._calib(),
                                           {d: "h" for d in DIMS})
        self.assertNotIn("pair_thresholds", row["params"])
        self.assertNotIn("divergence_bands", row["params"])
        self.assertIn("strength_bands", row["params"])
        self.assertEqual(row["output_schema"]["detail_keys"][-1], "strength")


if __name__ == "__main__":
    unittest.main()
