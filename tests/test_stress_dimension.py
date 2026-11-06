"""`DIM.STRESS` 生产计算回归测试（不连库）。

锁住固定核心集方案（2026-11-06，§5.5 v1.17）的十五件事：
  ① 4 块两级等权，score = 块间均值（CN 8 核心 / HK 7 核心 + 1 诊断）
  ② `RISK.MAXDD_250` / `RISK.TERM_SPREAD` 的 sign = −1（负回撤与倒挂口径）
  ③ `FLOW.HIBOR_3M` 是 HK 的 flags_only 诊断，不进 score / dispersion
  ④ 核心成员任一缺失 → 整行不产出（core_coverage_min = 1.0）
  ⑤ member_valid_from 冻结日期之前不产行（PIT 安全，不依赖 L1 extra.n_sample）
  ⑥ state 恒 NULL（state_bands_calibrated=false）；confidence 最高「中」
  ⑦ 短样本只降置信，不改 score / state
  ⑧ dispersion 只作描述量、不作门禁（高分歧仍产行）
  ⑨ flags.resonance_cnt = 对齐分位 >= resonance_floor 的核心成员数
  ⑩ blocks 落 4 块 {score, coverage, n}；空块从分母剔除（防御路径）
  ⑪ coverage 按全体 eligible（含诊断成员）计，诊断缺失只改 coverage
  ⑫ flags 形状为 dict，含 availability_tier / frozen_from / dimension_completeness
  ⑬ bulk_upsert 契约与 model_version == params_hash
  ⑭ build_all_dimensions() 按 dimension_code 分派到新生产者，不触碰现有四维
  ⑮ 生产者 8 参数签名与其它四维一致
"""
import datetime
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_dimension_value as rd  # noqa: E402

D = datetime.date
DAY = D(2026, 11, 4)
LABELS = ["极稳", "稳健", "正常", "紧张", "危机"]

SCOPES = {
    "RISK.RV20": "CN+HK",
    "RISK.VIX_CHG20": "GLOBAL",
    "RISK.SYNC_DOWN_20D": "CN+HK",
    "BREADTH.AVG_CORR_VW": "CN+HK",
    "RISK.CREDIT_SPREAD_CHG20": "CN+HK",
    "RISK.TERM_SPREAD": "CN",
    "RISK.EXTREME_DOWN_PCT": "CN+HK",
    "RISK.MAXDD_250": "CN+HK",
    "FLOW.HIBOR_3M": "HK",
}

MEMBERS = [
    {"code": "RISK.RV20",                 "sign":  1, "weight": 0.0, "block": "波动"},
    {"code": "RISK.VIX_CHG20",           "sign":  1, "weight": 0.0, "block": "波动"},
    {"code": "RISK.SYNC_DOWN_20D",       "sign":  1, "weight": 0.0, "block": "相关性"},
    {"code": "BREADTH.AVG_CORR_VW",      "sign":  1, "weight": 0.0, "block": "相关性"},
    {"code": "RISK.CREDIT_SPREAD_CHG20", "sign":  1, "weight": 0.0, "block": "信用"},
    {"code": "RISK.TERM_SPREAD",         "sign": -1, "weight": 0.0, "block": "信用"},
    {"code": "RISK.EXTREME_DOWN_PCT",    "sign":  1, "weight": 0.0, "block": "尾部"},
    {"code": "RISK.MAXDD_250",           "sign": -1, "weight": 0.0, "block": "尾部"},
    {"code": "FLOW.HIBOR_3M",            "sign":  1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
]

FREEZE = {
    "CN": {"valid": "2012-02-14", "confident": "2014-03-11"},
    "HK": {"valid": "2014-11-18", "confident": "2016-11-29"},
}
CORE = [m["code"] for m in MEMBERS if not m.get("flags_only")]


def _dim():
    return {
        "members": MEMBERS,
        "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
        "state_labels": LABELS,
        "params": {
            "coverage_min": 0.6, "core_coverage_min": 1.0,
            "dispersion_max": 30.0, "dispersion_no_gate": True,
            "state_bands_calibrated": False,
            "availability_tier": "stress_core_fixed",
            "resonance_floor": 80.0,
            "excluded_members": ["RISK.RV60", "RISK.CREDIT_SPREAD_AA"],
            "member_valid_from": {
                m: {c: FREEZE[m]["valid"] for c in CORE} for m in ("CN", "HK")},
            "member_confident_from": {
                m: {c: FREEZE[m]["confident"] for c in CORE} for m in ("CN", "HK")},
        },
    }


def _pct(market, index=None, **overrides):
    # 波动 70 / 相关性 50 / 信用 70 / 尾部 30 → score = 55.0（两市场同）
    data = {
        "RISK.RV20": [80.0], "RISK.VIX_CHG20": [60.0],
        "RISK.SYNC_DOWN_20D": [40.0], "BREADTH.AVG_CORR_VW": [60.0],
        "RISK.CREDIT_SPREAD_CHG20": [70.0], "RISK.TERM_SPREAD": [30.0],
        "RISK.EXTREME_DOWN_PCT": [50.0], "RISK.MAXDD_250": [90.0],
    }
    if market == "HK":
        data.pop("RISK.TERM_SPREAD")
        data["FLOW.HIBOR_3M"] = [95.0]
        data["RISK.CREDIT_SPREAD_CHG20"] = [70.0]
    data.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(data, index=pd.Index(index or [DAY], name="trade_date"))


class _Harness:
    """拦截 DB 边界，返回 `build_stress_dimension` 本应写入的行。"""

    def __init__(self, pct):
        self.pct = pct
        self.rows = []
        self.kw = {}

    def __enter__(self):
        def _capture(conn, table, rows, **kw):
            self.rows = rows
            self.kw = kw

        self.patches = [
            mock.patch.object(rd, "_load_market_scopes", return_value=SCOPES),
            mock.patch.object(rd, "_load_percentiles", return_value=self.pct),
            mock.patch.object(rd, "bulk_upsert", side_effect=_capture),
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()

    def run(self, market="CN", write=True, dim=None):
        return rd.build_stress_dimension(
            None, dim or _dim(), "hash123", market, D(1990, 1, 1), DAY, write)


class TestAggregation(unittest.TestCase):
    def test_four_block_two_level_mean(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        row = h.rows[0]
        self.assertEqual(row["score"], 55.0)
        self.assertEqual(row["blocks"]["波动"]["score"], 70.0)
        self.assertEqual(row["blocks"]["相关性"]["score"], 50.0)
        self.assertEqual(row["blocks"]["信用"]["score"], 70.0)
        self.assertEqual(row["blocks"]["尾部"]["score"], 30.0)
        self.assertEqual(len(row["blocks"]), 4)
        for b in ("波动", "相关性", "尾部"):
            self.assertEqual(row["blocks"][b]["n"], 2)

    def test_hk_has_three_block_credit_and_one_diagnostic(self):
        with _Harness(_pct("HK")) as h:
            h.run("HK")
        row = h.rows[0]
        self.assertEqual(row["score"], 55.0)
        self.assertEqual(row["blocks"]["信用"]["n"], 1)
        self.assertNotIn("诊断", row["blocks"])

    def test_sign_flip_of_maxdd_and_term_spread(self):
        with _Harness(_pct("CN", **{"RISK.MAXDD_250": 10.0})) as h:
            h.run("CN")
        self.assertEqual(h.rows[0]["blocks"]["尾部"]["score"], 70.0)  # (50 + 90)/2
        self.assertEqual(h.rows[0]["score"], 65.0)
        with _Harness(_pct("CN", **{"RISK.TERM_SPREAD": 90.0})) as h2:
            h2.run("CN")
        self.assertEqual(h2.rows[0]["blocks"]["信用"]["score"], 40.0)  # (70 + 10)/2
        self.assertEqual(h2.rows[0]["score"], 47.5)

    def test_diagnostic_member_does_not_enter_score(self):
        with _Harness(_pct("HK")) as h:
            h.run("HK")
        with _Harness(_pct("HK", **{"FLOW.HIBOR_3M": 1.0})) as h2:
            h2.run("HK")
        self.assertEqual(h.rows[0]["score"], h2.rows[0]["score"])
        self.assertEqual(h.rows[0]["dispersion"], h2.rows[0]["dispersion"])

    def test_diagnostic_missing_only_changes_coverage(self):
        with _Harness(_pct("HK", **{"FLOW.HIBOR_3M": np.nan})) as h:
            h.run("HK")
        self.assertEqual(h.rows[0]["score"], 55.0)
        self.assertEqual(h.rows[0]["coverage"], round(7 / 8, 4))
        self.assertNotIn("low_coverage", h.rows[0]["flags"])  # 0.875 > 0.6


class TestGates(unittest.TestCase):
    def test_core_missing_produces_no_row(self):
        with _Harness(_pct("CN", **{"RISK.RV20": np.nan})) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_frozen_valid_from_blocks_early_rows(self):
        with _Harness(_pct("CN", index=[D(2011, 6, 3)])) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_state_always_null_and_confidence_max_mid(self):
        with _Harness(_pct("CN")) as h:
            s = h.run("CN")
        self.assertIsNone(h.rows[0]["state"])
        self.assertEqual(s["state_rows"], 0)
        self.assertEqual(h.rows[0]["confidence"], "中")

    def test_short_sample_lowers_confidence_only(self):
        day = D(2013, 6, 3)       # CN: 2012-02-14 之后 / 2014-03-11 之前
        with _Harness(_pct("CN", index=[day])) as h:
            h.run("CN")
        row = h.rows[0]
        self.assertEqual(row["score"], 55.0)
        self.assertEqual(row["confidence"], "低")
        self.assertIsNone(row["state"])
        self.assertTrue(row["flags"]["short_sample"])

    def test_dispersion_is_descriptive_not_gate(self):
        pct = _pct("CN", **{"RISK.RV20": 100.0, "RISK.VIX_CHG20": 0.0,
                            "RISK.SYNC_DOWN_20D": 100.0,
                            "BREADTH.AVG_CORR_VW": 0.0})
        with _Harness(pct) as h:
            h.run("CN")
        self.assertEqual(len(h.rows), 1)
        self.assertGreater(h.rows[0]["dispersion"], 30.0)
        self.assertTrue(h.rows[0]["flags"]["dispersion_desc_only"])
        self.assertIsNone(h.rows[0]["state"])     # 恒不出档，与 dispersion 无关

    def test_empty_block_dropped_from_denominator(self):
        dim = _dim()
        dim["params"]["core_coverage_min"] = 0.5   # 防御路径：放开核心门禁
        pct = _pct("CN", **{"RISK.RV20": np.nan, "RISK.VIX_CHG20": np.nan})
        with _Harness(pct) as h:
            h.run("CN", dim=dim)
        row = h.rows[0]
        self.assertEqual(row["blocks"]["波动"]["score"], None)
        self.assertEqual(row["flags"]["empty_block"], ["波动"])
        self.assertEqual(row["score"], round((50 + 70 + 30) / 3, 2))


class TestFlagsAndContract(unittest.TestCase):
    def test_resonance_count(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        self.assertEqual(h.rows[0]["flags"]["resonance_cnt"], 1)  # RV20=80
        with _Harness(_pct("CN", **{"RISK.MAXDD_250": 10.0})) as h2:
            h2.run("CN")
        self.assertEqual(h2.rows[0]["flags"]["resonance_cnt"], 2)  # +MAXDD 对齐后 90

    def test_flags_shape_and_metadata(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        f = h.rows[0]["flags"]
        self.assertIsInstance(f, dict)
        self.assertEqual(f["availability_tier"], "stress_core_fixed")
        self.assertEqual(f["core_coverage"], 1.0)
        self.assertEqual(f["block_coverage"], 1.0)
        self.assertEqual(f["dimension_completeness"], round(8 / 9, 4))
        self.assertEqual(f["frozen_from"], "2012-02-14")
        self.assertEqual(f["excluded_members_count"], 2)
        self.assertEqual(f["resonance_floor"], 80.0)
        self.assertNotIn("empty_block", f)

    def test_hk_metadata(self):
        with _Harness(_pct("HK")) as h:
            h.run("HK")
        f = h.rows[0]["flags"]
        self.assertEqual(f["frozen_from"], "2014-11-18")
        self.assertEqual(f["dimension_completeness"], round(8 / 9, 4))

    def test_bulk_upsert_contract(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        self.assertEqual(h.kw["conflict_cols"],
                         ["dimension_code", "market", "trade_date"])
        self.assertFalse(h.kw["skip_null_updates"])
        self.assertTrue(h.kw["only_if_changed"])
        self.assertEqual(h.rows[0]["model_version"], "hash123")
        self.assertEqual(h.rows[0]["dimension_code"], rd.DIM_STRESS)

    def test_dispatch_includes_stress_without_touching_existing_dims(self):
        with mock.patch.object(rd, "_active_dimensions",
                               return_value=[rd.DIM_STRESS]), \
             mock.patch.object(rd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(rd, "build_breadth_dimension") as bm, \
             mock.patch.object(rd, "build_valuation_dimension") as vm, \
             mock.patch.object(rd, "build_sentiment_dimension") as sm, \
             mock.patch.object(rd, "build_flow_dimension") as fm, \
             mock.patch.object(rd, "build_stress_dimension",
                               return_value={"rows": 7, "dates": 7,
                                             "state_rows": 0}) as st:
            s = rd.build_all_dimensions(None, start=D(2020, 1, 1), end=DAY,
                                        write=False, markets=("CN",))
        self.assertEqual(st.call_count, 1)
        for m in (bm, vm, sm, fm):
            m.assert_not_called()
        self.assertEqual(s["CN"]["rows"], 7)
        self.assertEqual(s["CN"]["dims"][rd.DIM_STRESS]["rows"], 7)

    def test_producers_share_one_signature(self):
        import inspect
        a = list(inspect.signature(rd.build_sentiment_dimension).parameters)
        b = list(inspect.signature(rd.build_stress_dimension).parameters)
        self.assertEqual(a, b)
        self.assertEqual(a, ["conn", "dimension_config", "params_hash", "market",
                             "start", "end", "write", "dim_code"])


if __name__ == "__main__":
    unittest.main()
