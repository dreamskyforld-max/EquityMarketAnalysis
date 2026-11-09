"""BIZ.REVERSAL_RISK 一期裁剪版落地防回退测试（不连库）。

锁住 v1.34 的九件事：
  ① 判读档显式 1-based（1..3），不再混用 0/1-based；
  ② 判读单位是族去重计数，每族 1 票，score = 命中族数；
  ③ 「当日 5 族齐全 + 不早于冻结起点」双门禁，缺一即整行不发布；
  ④ 顶底冲突各降一档，写 signal_conflict；level 只承载顶部、恒当日 raw；
  ⑤ 撤销 SWITCH_DAYS 状态机，稳定性由游程标注承载；
  ⑥ 族清单版本 / 族切点 / 判读阈值 / 发布日历 / 上游原子指纹全部进 params_hash；
  ⑦ 字典 inputs 为 10 条 L1 atom（一期不落 signal_value）；
  ⑧ 四处接线（BUSINESS_ORDER / SEEDABLE / seed 分支 / 分派分支）齐全；
  ⑨ 标定脚本与 --seed 走同一入口，且生产者不写信号表。
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
FAMS = bb.REVERSAL_FAMILIES
ATOMS = bb.REVERSAL_FAMILY_ATOMS["CN"]


def _params(market="CN"):
    fam_cuts = {f: {"n": 1000, "top": 90.0, "bottom": 10.0} for f in FAMS}
    return {
        "family_list_version": bb.REVERSAL_FAMILY_LIST_VERSION,
        "families": list(FAMS),
        "family_labels": copy.deepcopy(bb.REVERSAL_FAMILY_LABELS),
        "family_atoms": copy.deepcopy(bb.REVERSAL_FAMILY_ATOMS),
        "family_cuts": {m: copy.deepcopy(fam_cuts) for m in bb.BUSINESS_MARKETS},
        "market_bands": {
            "CN": {"warn_from": 1, "high_from": 3, "calib_n": 1691,
                   "high_share": 0.0668},
            "HK": {"warn_from": 1, "high_from": 2, "calib_n": 3029,
                   "high_share": 0.0885},
        },
        "publish_from": dict(bb.REVERSAL_PUBLISH_FROM),
        "min_available_families": 5,
        "top_q": 0.90, "bottom_q": 0.10, "target_high_share": 0.10,
        "confirm_days": 3, "conflict_downgrade": True,
        "calib_end": "2025-12-31", "calib_n_obs": 47515,
        "excluded_families": copy.deepcopy(bb.REVERSAL_EXCLUDED_FAMILIES),
        "correlated_families": copy.deepcopy(bb.REVERSAL_CORRELATED_FAMILIES),
        "state_labels": list(bb.REVERSAL_STATE_LABELS),
        "bottom_state_labels": list(bb.REVERSAL_BOTTOM_STATE_LABELS),
        "upstream_atom_params_hashes": {c: "h" + c
                                        for c in bb.reversal_atom_codes()},
        "publish_policy": "all_families_present",
        "state_rule": "top_state_only_no_state_machine",
        "score_rule": "top_family_cnt",
    }


def _frame(days):
    """days: [(date, {fam: percentile or None}), ...] → 生产者输入宽表。"""
    out = []
    for day, vals in days:
        rec = {"trade_date": day}
        for fam in FAMS:
            rec[ATOMS[fam]] = vals.get(fam, 50.0)
        out.append(rec)
    return pd.DataFrame(out)


def _compute(days, market="CN", params=None):
    p = params or _params(market)
    return bb.reversal_compute(_frame(days), market, p, "hash16")


class TestBandAndFamilyCount(unittest.TestCase):

    def test_01_band_of_is_explicit_1_based(self):
        defn = {"warn_from": 1, "high_from": 3}
        self.assertEqual([bb.reversal_band_of(c, defn) for c in (0, 1, 2, 3, 5)],
                         [1, 2, 2, 3, 3])

    def test_02_score_is_deduplicated_family_count(self):
        days = [(D(2020, 1, 2), {"width": 95.0, "valuation": 97.0})]
        rows, stats = _compute(days)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 2)
        self.assertEqual(rows[0]["detail"]["top_cnt"], 2)
        self.assertEqual(rows[0]["detail"]["top_families"], ["宽度", "估值"])
        self.assertEqual(stats["top_cnt_dist"], {2: 1})

    def test_03_missing_family_blocks_publication(self):
        days = [(D(2020, 1, 2), {"width": 95.0, "flow": None}),
                (D(2020, 1, 3), {f: 95.0 for f in FAMS})]
        rows, stats = _compute(days)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(rows[0]["trade_date"], D(2020, 1, 3))
        self.assertEqual(rows[0]["detail"]["available_family_cnt"], 5)

    def test_04_early_date_before_frozen_start_is_dropped(self):
        days = [(D(2018, 12, 28), {f: 95.0 for f in FAMS})]
        rows, _ = _compute(days)
        self.assertEqual(rows, [])


class TestJudgementAndNoStateMachine(unittest.TestCase):

    def test_05_conflict_downgrades_both_sides(self):
        # 5 族不可能同时顶/底命中，用低 high_from 制造「两侧各达警惕档以上」
        p = _params()
        p["market_bands"]["CN"] = {"warn_from": 1, "high_from": 2}
        # 顶部 1 族 + 底部 1 族 + 3 族中性 → 两侧各达警惕/关注档
        days = [(D(2020, 1, 2), {"width": 95.0, "valuation": 5.0,
                                 "sentiment": 50.0, "stress": 50.0,
                                 "flow": 50.0})]
        rows, _ = _compute(days, params=p)
        det = rows[0]["detail"]
        self.assertTrue(det["signal_conflict"])
        self.assertEqual((det["top_state"], det["bottom_state"]), (1, 1))
        self.assertEqual(rows[0]["level"], 1)
        self.assertEqual(rows[0]["state"], "正常")

    def test_06_level_only_carries_top_risk(self):
        days = [(D(2020, 1, 2), {f: 5.0 for f in FAMS})]
        rows, _ = _compute(days)
        self.assertEqual(rows[0]["score"], 0)
        self.assertEqual(rows[0]["level"], 1)
        self.assertEqual(rows[0]["state"], "正常")
        self.assertEqual(rows[0]["detail"]["bottom_state"], 3)
        self.assertEqual(rows[0]["detail"]["bottom_state_label"], "高价值区")

    def test_07_no_state_machine_state_is_daily_raw(self):
        hi = {f: 95.0 for f in FAMS}
        lo = {f: 50.0 for f in FAMS}
        days = [(D(2020, 1, 2), hi), (D(2020, 1, 3), lo),
                (D(2020, 1, 6), hi)]
        rows, _ = _compute(days)
        self.assertEqual([r["level"] for r in rows], [3, 1, 3])
        self.assertEqual([r["state"] for r in rows],
                         ["高风险", "正常", "高风险"])
        self.assertEqual([r["dwell_days"] for r in rows], [1, 1, 1])

    def test_08_run_length_annotation(self):
        lo = {f: 50.0 for f in FAMS}
        days = [(D(2020, 1, d), lo) for d in (2, 3, 6, 7)]
        rows, _ = _compute(days)
        self.assertEqual([r["dwell_days"] for r in rows], [1, 2, 3, 4])
        self.assertEqual([r["status"] for r in rows],
                         ["待确认", "待确认", "已确认", "已确认"])
        self.assertTrue(all(r["since_date"] == D(2020, 1, 2) for r in rows))
        self.assertTrue(all(r["detail"]["dwell_days"] == r["dwell_days"]
                            for r in rows))


class TestContractAndWiring(unittest.TestCase):

    def test_09_params_hash_covers_family_cuts_and_bands_and_calendar(self):
        calib = {"calib_end": "2025-12-31", "n_obs": 47515,
                 "family_cuts": _params()["family_cuts"],
                 "market_bands": _params()["market_bands"]}
        hashes = {c: "h" + c for c in bb.reversal_atom_codes()}
        base = bb.reversal_build_dict_row(copy.deepcopy(calib), hashes)
        self.assertEqual(len(base["params_hash"]), 16)
        for key, value in (
                ("family_cuts", {**calib["family_cuts"],
                                 "CN": {**calib["family_cuts"]["CN"],
                                        "width": {"n": 1, "top": 1.0,
                                                  "bottom": 0.0}}}),
                ("market_bands", {**calib["market_bands"],
                                  "CN": {"warn_from": 1, "high_from": 4,
                                         "calib_n": 1, "high_share": 0.0}})):
            c = copy.deepcopy(calib)
            c[key] = value
            self.assertNotEqual(
                bb.reversal_build_dict_row(c, hashes)["params_hash"],
                base["params_hash"])
        c = copy.deepcopy(calib)
        self.assertNotEqual(
            bb.reversal_build_dict_row(c, {**hashes,
                                           bb.reversal_atom_codes()[0]: "changed"})
            ["params_hash"], base["params_hash"])
        self.assertIn("publish_from", base["params"])
        self.assertIn("family_list_version", base["params"])

    def test_10_inputs_are_ten_l1_atoms_and_schema_is_explicit(self):
        calib = {"calib_end": "2025-12-31", "n_obs": 47515,
                 "family_cuts": _params()["family_cuts"],
                 "market_bands": _params()["market_bands"]}
        row = bb.reversal_build_dict_row(
            calib, {c: "h" + c for c in bb.reversal_atom_codes()})
        self.assertEqual(row["kind"], "rule_set")
        self.assertEqual(row["is_active"], True)
        self.assertEqual(len(row["inputs"]), 10)
        self.assertTrue(all(x["kind"] == "atom" and
                            x["role"] == "family_atom" for x in row["inputs"]))
        schema = row["output_schema"]
        self.assertIn("level_semantics", schema)
        self.assertIn("score_semantics", schema)
        self.assertEqual(len(schema["detail_keys"]), 10)
        self.assertTrue(schema["no_state_machine"])

    def test_11_four_wiring_points_present(self):
        self.assertIn(bb.REVERSAL_BIZ_CODE, bb.BUSINESS_ORDER)
        self.assertIn(bb.REVERSAL_BIZ_CODE, bb.SEEDABLE_BUSINESS_CODES)
        with open(os.path.join(ROOT, "build_business_value.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("reversal_build_dict_row(reversal_calibrate(conn)",
                      src)
        self.assertIn("build_reversal_business(conn, config, market, write)",
                      src)
        # 一期偏离：生产者不得写信号层
        self.assertNotIn("INSERT INTO regime.signal_value", src)
        self.assertNotIn("INSERT INTO regime.signal_dict", src)

    def test_12_calibrator_uses_the_same_entry_as_seed(self):
        path = os.path.join(ROOT, "tools", "calibrate_reversal.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("bb.reversal_calibrate(conn)", src)
        self.assertIn("bb.reversal_build_dict_row(", src)
        self.assertIn("bb.reversal_upstream_hashes(conn)", src)

    def test_13_excluded_families_semantics_is_no_source_not_available(self):
        days = [(D(2020, 1, 2), {f: 50.0 for f in FAMS})]
        rows, _ = _compute(days)
        det = rows[0]["detail"]
        self.assertEqual(det["unavailable_families"],
                         list(bb.REVERSAL_EXCLUDED_FAMILIES))
        self.assertEqual(det["available_family_cnt"], 5)
        self.assertIsNone(det["missing"])
        self.assertEqual(det["correlated_families"],
                         bb.REVERSAL_CORRELATED_FAMILIES["CN"])


if __name__ == "__main__":
    unittest.main()
