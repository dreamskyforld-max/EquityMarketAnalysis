"""``BIZ.MARKET_REGIME`` v2 + L3 编排层回归测试（不连库）。

锁住 §6.1 v2 与 2026-11-06 扩展化整改的十四件事：
  ① 方向 = sign(TSMOM)、强度门槛 = DIM.TREND.score>30，不重算 σ / 不复用 DEADBAND
  ② score>gate 才出上行/下行；gate 内、缺输入一律不发行或落震荡（A8，不填 0/50）
  ③ params_hash 覆盖 inputs/params/output_schema 与上游 DIM.TREND 口径指纹
  ④ 状态机初始化 dwell=1（含当日）、满 N 日转已确认（off-by-one 已修）
  ⑤ streak 只累计「连续指向同一候选态」，换态 / 回到当前态归 1
  ⑥ MIN_DWELL 只阻断切换、不阻断计数；切换后 dwell=1、status=已确认
  ⑦ 起点由传入 frame 决定（指标层 inner join DIM.TREND，不在本层补历史）
  ⑧ detail 必带 trend_state / turn_risk / candidate / evidence / breadth_confirmation
  ⑨ turn_risk = MA200 与动量符号背离（零参数）
  ⑩ score 直接引用上游、model_version == params_hash
  ⑪ compute 纯函数、两次结果逐值相等（全量重算幂等，排除 updated_at）
  ⑫ **命名纪律**：指标专属模块不得暴露通用名（compute / raw_state / state_machine / PARAMS ...）
  ⑬ **参数真源**：阈值从 dict_row.params 读，改字典里的 score_gate 会改变判定
  ⑭ **单文件编排**：显式执行顺序 / if-elif 分派 / 通用公共函数存在，且不存在注册表、purge 等过度封装；标定后 params 不被拒绝
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
DAY0 = D(2026, 1, 5)
HASH = "hash1234567890ab"


def _params(**overrides):
    p = dict(bb.REGIME_PARAMS)
    p.update(overrides)
    return p


def _frame(n=12, tsmom=0.1, ma=0.02, score=80.0, bw="偏广"):
    """列名也与指标专属前缀对齐，避免与其它指标的 frame 混淆。"""
    return pd.DataFrame({
        "trade_date": [DAY0 + datetime.timedelta(days=i) for i in range(n)],
        "regime_trend_score": [score] * n,
        "regime_tsmom": [tsmom] * n,
        "regime_ma": [ma] * n,
        "regime_bw_state": [bw] * n,
    })


class _FakeCursor:
    def __init__(self, one=None, all_rows=None, dict_row=None):
        self._one, self._all, self._dict_row = one, all_rows, dict_row
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a):
        self._sql = sql
        return None

    def fetchone(self):
        if "business_dict" in self._sql and self._dict_row is not None:
            return self._dict_row
        return self._one

    def fetchall(self):
        return self._all


class _FakeConn:
    def __init__(self, one=None, all_rows=None, dict_row=None):
        self._one, self._all, self._dict_row = one, all_rows, dict_row

    def cursor(self):
        return _FakeCursor(self._one, self._all, self._dict_row)


class TestRawAndDict(unittest.TestCase):

    def test_01_direction_sign_and_gate(self):
        self.assertEqual(bb.regime_raw_state(0.10, 80.0, 30), "上行")
        self.assertEqual(bb.regime_raw_state(-0.10, 80.0, 30), "下行")
        self.assertEqual(bb.regime_raw_state(0.10, 30.0, 30), "震荡")   # 边界含等号
        self.assertEqual(bb.regime_raw_state(0.10, 10.0, 30), "震荡")
        self.assertEqual(bb.regime_raw_state(0.0, 80.0, 30), "震荡")

    def test_02_missing_input_no_row(self):
        self.assertIsNone(bb.regime_raw_state(None, 80.0, 30))
        self.assertIsNone(bb.regime_raw_state(float("nan"), 80.0, 30))
        self.assertIsNone(bb.regime_raw_state(0.10, None, 30))

    def test_03_params_hash_stable_and_upstream_coupled(self):
        a = bb.regime_build_dict_row({"DIM.TREND": "abc123"})
        b = bb.regime_build_dict_row({"DIM.TREND": "abc123"})
        c = bb.regime_build_dict_row({"DIM.TREND": "other999"})
        self.assertEqual(a["params_hash"], b["params_hash"])
        self.assertEqual(len(a["params_hash"]), 16)
        self.assertNotEqual(a["params_hash"], c["params_hash"],
                            "§5.6 口径变更必须换指纹并触发全量重算")
        self.assertEqual(a["params"]["score_gate"], 30)
        self.assertEqual(a["params"]["upstream_dim_trend_params_hash"], "abc123")
        self.assertNotIn("deadband", a["params"])
        self.assertNotIn("tsmom_deadband_ratio", a["params"])
        self.assertTrue(a["is_active"])

    def test_04_output_schema_and_inputs(self):
        row = bb.regime_build_dict_row({"DIM.TREND": "abc123"})
        self.assertIn("level_semantics", row["output_schema"])
        self.assertIn("score_semantics", row["output_schema"])
        kinds = {i["kind"] for i in row["inputs"]}
        self.assertEqual(kinds, {"atom", "dimension"})
        self.assertEqual(row["kind"], "state_machine")


class TestAuxiliary(unittest.TestCase):

    def test_05_turn_risk(self):
        self.assertTrue(bb.regime_turn_risk(0.02, -0.10))
        self.assertTrue(bb.regime_turn_risk(-0.02, 0.10))
        self.assertFalse(bb.regime_turn_risk(0.02, 0.10))
        self.assertFalse(bb.regime_turn_risk(0.0, 0.10))
        self.assertFalse(bb.regime_turn_risk(None, 0.10))

    def test_06_breadth_confirmation(self):
        self.assertEqual(bb.regime_breadth_confirmation("偏广", "上行"), "一致")
        self.assertEqual(bb.regime_breadth_confirmation("极窄", "上行"), "背离")
        self.assertEqual(bb.regime_breadth_confirmation("中性", "下行"), "中性")
        self.assertEqual(bb.regime_breadth_confirmation("偏广", "震荡"), "中性")
        self.assertEqual(bb.regime_breadth_confirmation(None, "上行"), "不可用")
        self.assertEqual(bb.regime_breadth_confirmation(float("nan"), "上行"), "不可用")


class TestStateMachine(unittest.TestCase):

    def test_07_init_dwell_and_confirm_offbyone(self):
        v = bb.regime_state_machine(
            ["上行"] * 8, [DAY0 + datetime.timedelta(days=i) for i in range(8)], 5, 20)
        self.assertEqual(v[0]["dwell"], 1, "初始化日即含当日")
        self.assertEqual(v[0]["status"], "待确认")
        self.assertEqual(v[0]["since"], DAY0)
        self.assertEqual([x["dwell"] for x in v[:5]], [1, 2, 3, 4, 5])
        self.assertEqual(v[4]["status"], "已确认", "满 N=5 日即转已确认（勿 N+1）")

    def test_08_streak_only_same_candidate(self):
        raws = ["上行"] * 22 + ["下行"] * 3 + ["震荡"] * 2 + ["下行"] * 5
        dates = [DAY0 + datetime.timedelta(days=i) for i in range(len(raws))]
        v = bb.regime_state_machine(raws, dates, 5, 20)
        tail = v[22:]
        self.assertEqual([x["streak"] for x in tail[:3]], [1, 2, 3])
        self.assertEqual(tail[3]["streak"], 1, "换态后归 1，不得沿用旧 streak")
        self.assertEqual(tail[5]["streak"], 1)
        self.assertEqual(tail[9]["state"], "下行", "连续 N 日后切换")
        self.assertNotEqual(tail[4]["state"], "震荡",
                            "震荡只成立 2 天，不得被误切（v1 的 streak 语义 bug）")

    def test_09_min_dwell_blocks_switch_not_count(self):
        raws = ["上行"] * 10 + ["下行"] * 20
        dates = [DAY0 + datetime.timedelta(days=i) for i in range(30)]
        v = bb.regime_state_machine(raws, dates, 5, 20)
        self.assertEqual(v[14]["state"], "上行", "dwell<20 期间不切换")
        self.assertEqual(v[14]["streak"], 5, "但候选计数继续累计")
        self.assertEqual(v[19]["state"], "下行", "停留期一到且 streak>=N 立即切换")
        self.assertEqual(v[19]["dwell"], 1)
        self.assertEqual(v[19]["status"], "已确认")
        self.assertEqual(v[19]["candidate"], None)


class TestCompute(unittest.TestCase):

    def test_10_detail_shape_and_score_reference(self):
        f = _frame(n=8)
        rows, stats = bb.regime_compute(f, "CN", _params(), HASH)
        self.assertEqual(stats["rows"], 8)
        self.assertEqual(stats["skipped"], 0)
        r = rows[0]
        self.assertEqual(r["biz_code"], bb.REGIME_BIZ_CODE)
        self.assertEqual(r["state"], "上行")
        self.assertEqual(r["level"], 3)
        self.assertEqual(r["score"], 80.0, "score 直接引用上游，不重算")
        self.assertEqual(r["model_version"], HASH)
        for k in ("trend_state", "turn_risk", "candidate", "evidence",
                  "breadth_confirmation"):
            self.assertIn(k, r["detail"])
        self.assertEqual(r["detail"]["trend_state"], "trend")
        self.assertEqual(r["detail"]["breadth_confirmation"], "一致")
        self.assertNotIn("DEADBAND", r["detail"]["evidence"])

    def test_11_missing_atom_skips_row_and_idempotent(self):
        f = _frame(n=8)
        f.loc[3, "regime_tsmom"] = float("nan")
        tail = f.iloc[4:].reset_index(drop=True)
        rows, stats = bb.regime_compute(tail, "HK", _params(), HASH)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(len(rows), 4)
        again, _ = bb.regime_compute(tail, "HK", _params(), HASH)
        strip = lambda xs: [{k: v for k, v in x.items() if k != "updated_at"} for x in xs]
        self.assertEqual(strip(rows), strip(again), "全量重算幂等")

    def test_12_range_state_and_turn_risk_flag(self):
        f = _frame(n=6, tsmom=0.1, ma=-0.02, score=10.0)
        rows, _ = bb.regime_compute(f, "CN", _params(), HASH)
        self.assertEqual(rows[0]["state"], "震荡")
        self.assertEqual(rows[0]["level"], 2)
        self.assertEqual(rows[0]["detail"]["trend_state"], "range")
        self.assertTrue(rows[0]["detail"]["turn_risk"])
        self.assertEqual(rows[0]["confidence"], "低")

    def test_13_params_come_from_dict_not_module_constant(self):
        f = _frame(n=6, score=80.0)
        default_rows, _ = bb.regime_compute(f, "CN", _params(), HASH)
        strict_rows, _ = bb.regime_compute(f, "CN", _params(score_gate=90), HASH)
        self.assertEqual(default_rows[0]["state"], "上行")
        self.assertEqual(strict_rows[0]["state"], "震荡",
                         "改字典 params 必须改变判定，不得读模块常量")
        self.assertEqual(strict_rows[0]["detail"]["evidence"]["score_gate"], 90)


class TestNamingDiscipline(unittest.TestCase):

    def test_14_naming_split_in_single_file(self):
        """单文件里通用函数用通用名、指标专属一律前缀，互不混淆。"""
        for name in ("compute", "raw_state", "state_machine", "turn_risk",
                     "breadth_confirmation", "load_frame", "build_dict_row",
                     "PARAMS", "INPUTS", "OUTPUT_SCHEMA", "REMARK", "LEVEL",
                     "BIZ", "DIM_TREND", "ATOM_MOM"):
            self.assertFalse(hasattr(bb, name), f"不应暴露无归属通用名 {name}")
        for name in ("regime_compute", "regime_raw_state", "regime_state_machine",
                     "regime_turn_risk", "regime_breadth_confirmation",
                     "regime_load_frame", "regime_build_dict_row",
                     "REGIME_PARAMS", "REGIME_INPUTS", "REGIME_LEVEL",
                     "REGIME_BIZ_CODE", "build_regime_business"):
            self.assertTrue(hasattr(bb, name), f"缺少带前缀的专属名 {name}")
        for name in ("load_business_dict", "active_business_codes",
                     "seed_business_dict", "write_business_rows",
                     "build_all_business"):
            self.assertTrue(callable(getattr(bb, name)), f"缺少通用公共函数 {name}")
        for name in ("BUSINESS_PRODUCERS", "BusinessProducer",
                     "purge_stale_business_rows", "expected_business_dict_row",
                     "load_business_dict_row"):
            self.assertFalse(hasattr(bb, name), f"已撤销的过度封装 {name} 不应存在")


class TestOrchestration(unittest.TestCase):

    def test_15_explicit_execution_order(self):
        """L3 四个对象按显式顺序表遍历；风险预算已整体移出，不能由 biz_code 字母序决定。"""
        self.assertEqual(bb.BUSINESS_ORDER[0], bb.REGIME_BIZ_CODE)
        self.assertEqual(bb.BUSINESS_ORDER[-1], "BIZ.REVERSAL_RISK")
        self.assertEqual(len(bb.BUSINESS_ORDER), 4)
        for code in ("BIZ.MARKET_PATTERN", "BIZ.MARKET_DIVERGENCE",
                     "BIZ.REVERSAL_RISK"):
            self.assertIn(code, bb.BUSINESS_ORDER)
        for code in ("BIZ.RISK_BUDGET", "BIZ.MARKET_TEMPERATURE",
                     "BIZ.SYSTEMIC_STRESS"):
            self.assertNotIn(code, bb.BUSINESS_ORDER)
        fake = _FakeConn(all_rows=[("BIZ.MARKET_PATTERN",),
                                  (bb.REGIME_BIZ_CODE,), ("BIZ.UNKNOWN",)])
        self.assertEqual(bb.active_business_codes(fake),
                         [bb.REGIME_BIZ_CODE, "BIZ.MARKET_PATTERN"])
        self.assertEqual(bb.active_business_codes(fake, only_biz=["BIZ.OTHER"]), [])

    def test_16_seed_hash_matches_production(self):
        conn = _FakeConn(one=("5038af6aebc31c43",))
        self.assertEqual(bb.regime_upstream_hashes(conn),
                         {"DIM.TREND": "5038af6aebc31c43"})
        row = bb.regime_build_dict_row(bb.regime_upstream_hashes(conn))
        self.assertEqual(row["params_hash"], "0ccb0176f74177e5")

    def test_17_input_codes_driven_by_dict(self):
        row = bb.regime_build_dict_row({"DIM.TREND": "abc123"})
        codes = bb.regime_input_codes(row)
        self.assertEqual(codes["direction"], "TREND.TSMOM_12M")
        self.assertEqual(codes["strength_gate_and_score"], "DIM.TREND")
        self.assertEqual(codes["detail_only"], "DIM.BREADTH")

    def test_18_load_business_dict_keeps_inputs_array(self):
        """`inputs` 是 JSON 数组、不是对象；解析器不得把 list 吞成 {}。"""
        expected = bb.regime_build_dict_row({"DIM.TREND": "abc123"})
        db_row = (expected["inputs"], expected["params"],
                  expected["output_schema"], expected["params_hash"])
        loaded = bb.load_business_dict(_FakeConn(dict_row=db_row), bb.REGIME_BIZ_CODE)
        self.assertIsInstance(loaded["inputs"], list)
        self.assertEqual(loaded["inputs"], expected["inputs"])
        self.assertEqual(loaded["params"], expected["params"])
        self.assertEqual(bb.regime_input_codes(loaded)["direction"],
                         "TREND.TSMOM_12M")

    def test_19_calibrated_params_not_rejected(self):
        """标定工具改写字典 params / params_hash 后，编排层不得拒绝运行。"""
        expected = bb.regime_build_dict_row({"DIM.TREND": "abc123"})
        calibrated = dict(expected["params"])
        calibrated["score_gate"] = 42
        db_row = (expected["inputs"], calibrated,
                  expected["output_schema"], "calibrated-hash")
        loaded = bb.load_business_dict(_FakeConn(dict_row=db_row), bb.REGIME_BIZ_CODE)
        self.assertEqual(loaded["params"]["score_gate"], 42)
        self.assertEqual(loaded["params_hash"], "calibrated-hash")


if __name__ == "__main__":
    unittest.main()
