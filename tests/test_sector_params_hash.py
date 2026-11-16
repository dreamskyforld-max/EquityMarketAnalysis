#!/usr/bin/env python3
"""第④层 L2 / L3 口径指纹回归（2026-11-16）。

背景：旧实现把 L2 / L3 的 `params_hash` 只绑在 `structure` 字面量上，
改锚 / 改成员 / 翻 sign / 改 L1 定义 / 改闸门阈值都不换指纹，
历史行会混存两套口径却显示同一版本号。

本组测试钉住：
  ① 影响数值的变更必须换指纹；
  ② 文案类变更（indicator_name / remark）不得换指纹；
  ③ L1 原子指纹保持不变（避免误触发 L1 全量重算）；
  ④ CN / HK 两条版本线互相隔离；
  ⑤ 真实产出行的 params_hash 与预计算一致（防调用点漏改）。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import unittest
from unittest import mock

import pandas as pd

import sector_layer as sl
import sector_layer_hk as slh

DAY = dt.date(2026, 11, 11)


class TestDimSpecContent(unittest.TestCase):
    """L2 spec 必须真的包含结构参数与上游 L1 定义。"""

    def test_01_six_dims_and_key_fields(self):
        spec = sl._dim_spec()
        self.assertEqual(set(spec), set(sl.DIMENSION_DEFS))
        self.assertEqual(len(spec), 6)
        for name, s in spec.items():
            with self.subTest(dim=name):
                self.assertEqual(s["layer"], "L2")
                self.assertEqual(s["code"], "SEC.DIM." + name)
                self.assertEqual(s["version"], sl.CALIB_VERSION)
                self.assertIn("structure", s)
                self.assertIn("anchor", s)
                self.assertIn("members", s)
                self.assertIn("upstream", s)

    def test_02_anchor_and_sign_are_captured(self):
        spec = sl._dim_spec()
        self.assertEqual(spec["TREND"]["anchor"], "SEC.TREND.RS_XSEC_PCT")
        self.assertIn("SEC.TREND.RS_XSEC_PCT", spec["TREND"]["upstream"])
        signs = {m["code"]: m["sign"] for m in spec["BREADTH"]["members"]}
        self.assertEqual(signs["SEC.BREADTH.RET_DISPERSION"], -1)
        self.assertEqual(signs["SEC.BREADTH.UP_RATIO"], 1)

    def test_03_crowding_captures_three_factors(self):
        spec = sl._dim_spec()["CROWDING"]
        self.assertEqual(sl.DIMENSION_DEFS["CROWDING"]["anchor"], None)
        self.assertEqual(set(spec["upstream"]), set(sl._CROWD_FACTORS))

    def test_04_upstream_contains_l1_hash_and_numeric_def(self):
        atom = sl._dim_spec()["TREND"]["upstream"]["SEC.TREND.RS_XSEC_PCT"]
        self.assertEqual(len(atom["l1_hash"]), 16)
        self.assertEqual(atom["def"]["model_key"], "M1")
        self.assertIn("pct_x_col", atom["def"])
        # 文案 / 启用开关不得进入 spec
        for banned in ("remark", "indicator_name", "is_active"):
            self.assertNotIn(banned, json.dumps(atom, ensure_ascii=False))


class TestDimHashSensitivity(unittest.TestCase):
    """任何影响数值的口径变更都必须换指纹。"""

    def setUp(self):
        self.spec = sl._dim_spec()

    def test_05_change_anchor(self):
        s = copy.deepcopy(self.spec["TREND"])
        s["anchor"] = "SEC.TREND.MOM_RANK"
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec["TREND"]))

    def test_06_change_sign(self):
        s = copy.deepcopy(self.spec["BREADTH"])
        s["members"][-1]["sign"] = 1
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec["BREADTH"]))

    def test_07_change_member_list(self):
        s = copy.deepcopy(self.spec["FUNDAMENTAL"])
        s["members"] = s["members"][:-1]
        self.assertNotEqual(sl._hash_spec(s),
                            sl._hash_spec(self.spec["FUNDAMENTAL"]))

    def test_08_change_structure_or_side(self):
        s = copy.deepcopy(self.spec["TREND"])
        s["structure"] = "equal"
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec["TREND"]))

    def test_09_change_l1_definition(self):
        s = copy.deepcopy(self.spec["TREND"])
        s["upstream"]["SEC.TREND.RS_XSEC_PCT"]["def"]["pct_x_col"] = "other_col"
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec["TREND"]))

    def test_10_change_l1_fingerprint(self):
        s = copy.deepcopy(self.spec["TREND"])
        s["upstream"]["SEC.TREND.RS_XSEC_PCT"]["l1_hash"] = "0000000000000000"
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec["TREND"]))

    def test_11_version_change(self):
        s = copy.deepcopy(self.spec["TREND"])
        s["version"] = "s9.9"
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec["TREND"]))

    def test_12_copywriting_does_not_change_hash(self):
        """改中文名 / remark 不触发重算（与既有 remark 纪律一致）。"""
        before = sl._dim_hashes()
        d = next(x for x in sl.INDICATOR_DEFS
                 if x["code"] == "SEC.TREND.RS_XSEC_PCT")
        old_name, old_remark = d["indicator_name"], d["remark"]
        try:
            d["indicator_name"] = "改过的名字"
            d["remark"] = "改过的说明"
            self.assertEqual(sl._dim_hashes(), before)
        finally:
            d["indicator_name"], d["remark"] = old_name, old_remark
        self.assertEqual(sl._dim_hashes(), before)

    def test_13_hashes_unique_stable_16(self):
        a, b = sl._dim_hashes(), sl._dim_hashes()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 6)
        self.assertEqual(len(set(a.values())), 6)
        self.assertTrue(all(len(x) == 16 for x in a.values()))


BIZ_SAMPLE = "SEC.BIZ.STATE_SIDE"   # 取一个 active biz_code 做指纹敏感性样本


class TestBizSpecSensitivity(unittest.TestCase):
    """L3 spec 必须包含六维指纹、闸门阈值与本指标的独立输出契约。"""

    def setUp(self):
        self.hashes = sl._dim_hashes()
        self.spec = sl._biz_spec(BIZ_SAMPLE, self.hashes)

    def test_14_contains_dims_and_thresholds(self):
        self.assertEqual(self.spec["upstream_dims"], self.hashes)
        p = self.spec["params"]
        for k in ("coverage_min", "valid_days_20_min", "valid_days_window",
                  "crowd_pct_high", "short_sample", "min_periods",
                  "pit_start", "enabled_dimensions"):
            self.assertIn(k, p)
        self.assertEqual(list(p["enabled_dimensions"]),
                         list(sl.ENABLED_DIMENSIONS))

    def test_15_threshold_change_changes_hash(self):
        for key in ("coverage_min", "valid_days_20_min", "valid_days_window",
                    "crowd_pct_high", "short_sample", "min_periods"):
            with self.subTest(key=key):
                s = copy.deepcopy(self.spec)
                s["params"][key] = 999
                self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec))

    def test_16_upstream_dim_change_changes_hash(self):
        s = copy.deepcopy(self.spec)
        s["upstream_dims"]["TREND"] = "0000000000000000"
        self.assertNotEqual(sl._hash_spec(s), sl._hash_spec(self.spec))

    def test_17_constants_really_wired(self):
        base = sl._biz_params_hash(BIZ_SAMPLE)
        with mock.patch.object(sl, "COVERAGE_MIN", 0.7):
            self.assertNotEqual(sl._biz_params_hash(BIZ_SAMPLE), base)
        with mock.patch.object(sl, "VALID_DAYS_20_MIN", 16):
            self.assertNotEqual(sl._biz_params_hash(BIZ_SAMPLE), base)
        with mock.patch.object(sl, "CALIB_VERSION", "s9.9"):
            self.assertNotEqual(sl._biz_params_hash(BIZ_SAMPLE), base)
        self.assertEqual(sl._biz_params_hash(BIZ_SAMPLE), base)

    def test_17b_each_biz_code_has_independent_hash(self):
        hs = sl._biz_hashes()
        self.assertEqual(set(hs), set(sl.BIZ_DEFS))
        self.assertEqual(len(set(hs.values())), len(hs))
        # 改单个业务指标的输出契约，只有它自己的指纹变
        spec = copy.deepcopy(self.spec)
        spec["output_schema"]["score"] = "改过的语义"
        self.assertNotEqual(sl._hash_spec(spec), sl._hash_spec(self.spec))


class TestL1Unchanged(unittest.TestCase):
    """L1 指纹必须保持原样，否则历史 L1 行会被误判旧口径。"""

    def test_18_l1_still_six_field_payload(self):
        d = dict(code="X", unit="pct", direction="high_good",
                 formula="f", model_key="M1")
        self.assertEqual(sl._params_hash(d), sl._params_hash(dict(d)))
        self.assertEqual(len(sl._params_hash(d)), 16)
        self.assertNotEqual(sl._params_hash(d),
                            sl._params_hash(dict(d, formula="g")))
        # 新增的 spec 字段不得泄漏进 L1 payload
        self.assertEqual(sl._params_hash(dict(d, anchor="whatever")),
                         sl._params_hash(d))

    def test_19_l1_remark_ignored(self):
        a = dict(code="X", unit="pct", direction="high_good",
                 formula="f", model_key="M1", remark="甲")
        b = dict(a, remark="乙")
        self.assertEqual(sl._params_hash(a), sl._params_hash(b))


class TestHKIsolation(unittest.TestCase):
    """HK 独立口径线：spec 带市场 / 分类 / hk1.0 / no_rank。"""

    def test_20_spec_metadata(self):
        spec = slh._hk_dim_spec()
        self.assertEqual(len(spec), 6)
        for s in spec.values():
            self.assertEqual(s["market"], "HK")
            self.assertEqual(s["classification"], "HS_L3")
            self.assertEqual(s["version"], "hk1.0")
        biz = slh._hk_biz_spec(BIZ_SAMPLE, slh._hk_dim_hashes())
        self.assertTrue(biz["params"]["no_rank"])
        self.assertEqual(biz["params"]["cap_start"], "2002-01-01")
        self.assertEqual(biz["market"], "HK")
        # HK 本期不排名：字典登记 RANK 但值表不产出
        self.assertFalse(slh.HK_BIZ_DEFS["SEC.BIZ.RANK"]["is_active"])
        self.assertNotIn("SEC.BIZ.RANK", slh._hk_biz_hashes())

    def test_21_differs_from_cn(self):
        cn, hk = sl._dim_hashes(), slh._hk_dim_hashes()
        self.assertTrue(all(cn[k] != hk[k] for k in cn))
        self.assertNotEqual(sl._biz_params_hash(BIZ_SAMPLE),
                            slh._hk_biz_params_hash(BIZ_SAMPLE))

    def test_22_hk_sensitivity(self):
        base_dim = slh._hk_dim_hashes()
        s = copy.deepcopy(slh._hk_dim_spec()["TREND"])
        s["anchor"] = "SEC.TREND.MOM_RANK"
        self.assertNotEqual(sl._hash_spec(s), base_dim["TREND"])
        self.assertEqual(slh._hk_dim_hashes(), base_dim)

        base_biz = slh._hk_biz_params_hash(BIZ_SAMPLE)
        with mock.patch.object(slh, "COVERAGE_MIN", 0.7):
            self.assertNotEqual(slh._hk_biz_params_hash(BIZ_SAMPLE), base_biz)
        with mock.patch.object(slh, "CAP_START", dt.date(2003, 1, 1)):
            self.assertNotEqual(slh._hk_biz_params_hash(BIZ_SAMPLE), base_biz)
        self.assertEqual(slh._hk_biz_params_hash(BIZ_SAMPLE), base_biz)


def _cn_wide():
    return pd.DataFrame([{
        "trade_date": DAY, "sw_code": "801780",
        "dim_TREND": 60.0, "dim_FLOW": 55.0, "dim_VALUATION": 63.0,
        "dim_FUNDAMENTAL": 50.0, "dim_BREADTH": 52.0,
        "amt_share_n": 1000, "n_flow": 1000, "pe_n_sample": 1000,
        "revision_n": 1000,
        "_crowd": {"gate": "NORMAL", "k": 3, "partial": False, "hit": 0},
    }])


def _cn_biz_wide(date=DAY):
    return pd.DataFrame([{
        "trade_date": date, "sw_code": "801780", "coverage": 1.0,
        "state_n": 4,
        "state_members": ["dim_BREADTH", "dim_FLOW", "dim_FUNDAMENTAL", "dim_TREND"],
        "valid_days_20": 20, "pe_n_sample": 1000,
        "state_side": 55.0, "risk_side": 63.0, "quadrant": "强势·未拥挤",
        "rank": 3, "rank_n": 31,
        "_crowd": {"gate": "NORMAL", "k": 3, "partial": False},
    }])


class TestProducedRows(unittest.TestCase):
    """真实产出行必须用新指纹，且闸门行为符合设计意图。"""

    def test_24_score_rows_use_new_hashes(self):
        rows = sl._score_rows(_cn_wide(), DAY, DAY)
        expected = sl._dim_hashes()
        self.assertEqual(len(rows), 6)
        for r in rows:
            self.assertEqual(r["params_hash"], expected[r["dimension_code"]])
        # CROWDING 仍是规则判定：score 恒 NULL、members_used 为空
        cw = next(r for r in rows if r["dimension_code"] == "CROWDING")
        self.assertIsNone(cw["score"])
        self.assertEqual(cw["members_used"], [])

    def test_25_biz_rows_are_long_table_with_independent_hashes(self):
        rows = sl._biz_rows(_cn_biz_wide(), DAY, DAY)
        expected = sl._biz_hashes()
        self.assertEqual(len(rows), 5)              # CN 5 个 active 业务指标
        self.assertEqual({r["biz_code"] for r in rows}, set(expected))
        by_code = {r["biz_code"]: r for r in rows}
        for code, r in by_code.items():
            self.assertEqual(r["params_hash"], expected[code])
            self.assertIn("biz_code", r)   # L3 长表行带 biz_code（BIZ_PK_COLS 首位）
        # 闸门在 GATE 行的 detail 里；短样本不产出闸门
        gate = by_code["SEC.BIZ.GATE"]["detail"]["gate_flags"]
        self.assertNotIn("SHORT_SAMPLE", gate)
        self.assertNotIn("BREAK", gate)             # BREAK 仅 HK 侧写入
        self.assertTrue(all(r["applicable"] for r in rows))
        self.assertIsNotNone(by_code["SEC.BIZ.STATE_SIDE"]["score"])
        self.assertIsNone(by_code["SEC.BIZ.QUADRANT"]["score"])
        self.assertIsNotNone(by_code["SEC.BIZ.RANK"]["level"])

    def test_26_hk_rows_use_new_hashes(self):
        wide = pd.DataFrame([{
            "trade_date": DAY, "sector_code": "HK.LIST1019",
            "dim_TREND": 60.0, "dim_VALUATION": 63.0, "dim_BREADTH": 52.0,
            "amt_share_n": 1000, "pe_n_sample": 1000,
            "_crowd": {"gate": "NORMAL", "k": 2, "partial": True},
        }])
        rows = slh._score_rows(wide, DAY, DAY)
        expected = slh._hk_dim_hashes()
        for r in rows:
            self.assertEqual(r["params_hash"], expected[r["dimension_code"]])

    def test_27_hk_break_gate_before_2002(self):
        wide = pd.DataFrame([{
            "trade_date": dt.date(2001, 6, 1), "sector_code": "HK.LIST1019",
            "biz_coverage": 1.0, "state_n": 2,
            "state_members": ["dim_BREADTH", "dim_TREND"],
            "valid_days_20": 20, "pe_n_sample": 1000,
            "state_side": 55.0, "risk_side": 63.0, "quadrant": "强势·未拥挤",
            "rank_n": 110,
            "_crowd": {"gate": "NORMAL", "k": 2, "partial": True},
        }])
        rows = slh._biz_rows(wide, dt.date(2001, 6, 1), dt.date(2001, 6, 1))
        self.assertEqual(len(rows), 4)              # HK RANK 不启用
        self.assertNotIn("SEC.BIZ.RANK", {r["biz_code"] for r in rows})
        by_code = {r["biz_code"]: r for r in rows}
        for code, r in by_code.items():
            self.assertEqual(r["params_hash"], slh._hk_biz_hashes()[code])
        gate = by_code["SEC.BIZ.GATE"]["detail"]["gate_flags"]
        self.assertIn("BREAK", gate)
        self.assertIn("PARTIAL_STATE", gate)
        self.assertTrue(all(not r["applicable"] for r in rows))


class TestSourceGuard(unittest.TestCase):
    """防回退：L2/L3 不得再用旧的六字段拼接构造指纹。"""

    def test_28_no_legacy_hash_construction(self):
        for path in ("sector_layer.py", "sector_layer_hk.py"):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            with self.subTest(file=path):
                self.assertNotIn('code="SEC.DIM." + name', src)
                self.assertNotIn('code="SEC.BIZ.DAILY"', src)


if __name__ == "__main__":
    unittest.main()
