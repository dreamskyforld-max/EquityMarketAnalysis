"""个股画像 L2/L3 结论层防回退测试（doc/stock_profile_l2_l3.md §10.3 / §12.3）。

以纯函数 + 元数据 + 源码断言为主，不依赖数据库运行，便于随发版推到服务器执行。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from profiling import registry, tags  # noqa: F401  tags 触发注册
from profiling.conclusions import producer, rules as R

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestRulesSchema(unittest.TestCase):
    def test_validate_rules_passes(self):
        R.validate_rules()

    def test_six_derived_and_order(self):
        self.assertEqual(len(R.DERIVED_TAGS), 6)
        self.assertEqual(R.L2_TAGS + R.L3_TAGS, R.DERIVED_TAGS)
        self.assertEqual(len(R.RULES), 6)

    def test_every_table_has_priority_99_fallback(self):
        for code, spec in R.RULES.items():
            priorities = [r["priority"] for r in spec["rules"]]
            self.assertIn(99, priorities, code)
            self.assertEqual(len(priorities), len(set(priorities)), code)
            self.assertEqual(
                next(r for r in spec["rules"] if r["priority"] == 99)["conditions"],
                {"all": []}, code,
            )

    def test_conditions_are_literal_only(self):
        allowed = {"<=", "<", ">=", ">", "==", "in"}
        for code, spec in R.RULES.items():
            fields = {i["field"] for i in spec["inputs"]}
            fields |= {d["field"] for d in spec["derived_fields"]}
            for rule in spec["rules"]:
                stack = [rule["conditions"]]
                while stack:
                    node = stack.pop()
                    if "all" in node:
                        stack.extend(node["all"])
                    elif "any" in node:
                        stack.extend(node["any"])
                    elif "not" in node:
                        stack.append(node["not"])
                    else:
                        self.assertIn(node["field"], fields, f"{code}/{rule['rule_id']}")
                        self.assertIn(node["op"], allowed, f"{code}/{rule['rule_id']}")
                        self.assertNotIsInstance(node["value"], dict)
                        if node["op"] in ("<=", "<", ">=", ">"):
                            self.assertIsInstance(node["value"], (int, float))

    def test_first_match_and_eval_condition(self):
        spec = R.RULES["dim_valuation_conclusion"]
        rid, result = R.first_match(spec, {"loss_flag": True})
        self.assertEqual((rid, result), ("VAL-00", "loss"))
        vals = {"loss_flag": False, "negative_flag": False,
                "pe_hist_pct": 18.0, "pe_ind_pct": 19.0}
        producer._derive(spec, vals)   # D20：规则读档位，须先派生
        rid, result = R.first_match(spec, vals)
        self.assertEqual((rid, result), ("VAL-04", "rel_cheap"))
        # 空合取恒真 = priority 99 兜底
        self.assertTrue(R.eval_condition({"all": []}, {}))
        self.assertFalse(R.eval_condition(
            {"field": "x", "op": "<=", "value": 1}, {"x": None}))
        self.assertTrue(R.eval_condition(
            {"any": [{"field": "a", "op": "==", "value": 1},
                     {"field": "b", "op": "==", "value": 2}]}, {"b": 2}))

    def test_g4_exempt_declared(self):
        self.assertTrue(R.RULES["dim_valuation_conclusion"].get("g4_exempt"))

    def test_valuation_3x3_matrix_first_match(self):
        """§5.1 v2：双分位 3×3 全展开，每格都落到自己那一码，不被点差门槛抢走。"""
        spec = R.RULES["dim_valuation_conclusion"]
        cases = [
            # (历史分位, 行业分位, 期望 rule_id, 期望结果码)
            (1.19, 34.13, "VAL-07", "hist_cheap_ind_neutral"),   # HK.06181 第一轮反例
            (10.0, 85.0, "VAL-02", "hist_cheap_ind_expensive"),
            (85.0, 10.0, "VAL-03", "hist_expensive_ind_cheap"),
            (10.0, 10.0, "VAL-04", "rel_cheap"),
            (85.0, 85.0, "VAL-05", "rel_expensive"),
            (85.0, 50.0, "VAL-08", "hist_expensive_ind_neutral"),
            (50.0, 10.0, "VAL-09", "hist_neutral_ind_cheap"),
            (50.0, 85.0, "VAL-10", "hist_neutral_ind_expensive"),
            (50.0, 50.0, "VAL-06", "rel_fair"),
        ]
        for hist, ind, rule_id, result in cases:
            with self.subTest(hist=hist, ind=ind):
                vals = {"loss_flag": False, "negative_flag": False,
                        "pe_hist_pct": hist, "pe_ind_pct": ind}
                producer._derive(spec, vals)   # D20：先切档，再按档位枚举匹配
                self.assertEqual(R.first_match(spec, vals), (rule_id, result))

    def test_valuation_has_no_gap_derived_fields(self):
        """v4（D20）：只有 hist_tier/ind_tier 两个档位派生；仍不得有点差派生字段。"""
        spec = R.RULES["dim_valuation_conclusion"]
        self.assertEqual([d["field"] for d in spec["derived_fields"]],
                         ["hist_tier", "ind_tier"])
        self.assertNotIn("divergence_gap_pctl", spec["params"])
        self.assertEqual(spec["params"]["cheap_pctl"], 20)
        self.assertEqual(spec["params"]["expensive_pctl"], 80)
        values = {"pe_hist_pct": 1.19, "pe_ind_pct": 34.13}
        original = dict(values)
        producer._derive(spec, values)
        self.assertEqual(values["pe_hist_pct"], original["pe_hist_pct"])
        self.assertEqual(values["pe_ind_pct"], original["pe_ind_pct"])
        self.assertEqual(values["hist_tier"], "cheap")
        self.assertEqual(values["ind_tier"], "mid")


class TestEnumContract(unittest.TestCase):
    def test_43_codes_exactly_cover_all_results(self):
        codes = set()
        for m in registry.all_tags():
            if m.source_type == "derived":
                codes |= set(m.enum_values)
        results = {r["result"] for spec in R.RULES.values()
                   for r in spec["rules"] if r["result"] is not None}
        self.assertEqual(len(codes), 43)   # D21 质量 moderate + D22 价值质量 3 码
        self.assertEqual(codes, results)  # 无越界码、无孤儿码

    def test_all_short_labels_nonempty(self):
        for m in registry.all_tags():
            if m.source_type == "derived":
                for code, names in m.enum_values.items():
                    self.assertGreaterEqual(len(names), 2, f"{m.code}/{code}")
                    self.assertTrue(names[0] and names[1], f"{m.code}/{code}")

    def test_derived_tag_metadata(self):
        metas = [m for m in registry.all_tags() if m.source_type == "derived"]
        self.assertEqual(len(metas), 6)
        for m in metas:
            self.assertEqual(m.value_type, "enum")
            self.assertTrue(m.value_ref and m.value_ref.startswith("enum:"))
            self.assertIsNone(m.value_range)   # §3.4 R4：不污染 _tag_pct
            self.assertIsNone(m.num_unit)
            # D21 质量结论 v2→v3（一级行业口径+7 码），估值 v4，其余维持 v2；
            # 与 RULES.version 必须逐码一致（sync_conclusion_dict 会硬校验）
            expected = {"dim_valuation_conclusion": "v4",
                        "dim_quality_conclusion": "v3",
                        "dim_growth_conclusion": "v4",
                        "dim_position_state": "v3",
                        "biz_value_quality_type": "v3",
                        "biz_growth_quality_type": "v3"}.get(m.code, "v2")
            self.assertEqual(m.version, expected, m.code)
            self.assertEqual(m.version, R.RULES[m.code]["version"], m.code)
            self.assertEqual(m.status, "active")
            self.assertFalse(m.is_exclusive)
            self.assertFalse(m.confidence_req)
            self.assertIn("不构成投资建议", m.compute_logic)
        domains = {m.domain for m in metas}
        self.assertIn("综合研判", domains)

    def test_enum_uniqueness_allows_same_label_and_short(self):
        # 附录 D 有「label == short_label」的码（如 low_quiet），不得被误判为概念冲突
        registry.validate_enum_uniqueness()


class TestProducerPureFunctions(unittest.TestCase):
    def test_cast_tiers_and_invalid_flags(self):
        item = {"dtype": "int", "invalid": ["LOSS"]}
        self.assertEqual(producer._cast("4", item), 4)
        self.assertIsNone(producer._cast("LOSS", item))
        self.assertIsNone(producer._cast("NEGATIVE", item))
        self.assertIsNone(producer._cast(None, item))
        self.assertEqual(producer._cast("LOSS", {"dtype": "bool", "match": "LOSS"}), True)
        self.assertEqual(producer._cast("3", {"dtype": "bool", "match": "LOSS"}), False)
        self.assertEqual(producer._cast("18.4", {"dtype": "float"}), 18.4)

    def test_derive_fields(self):
        spec = R.RULES["dim_growth_conclusion"]
        values = {"rev_tier": 1, "np_tier": 4}
        producer._derive(spec, values)
        self.assertEqual(values["growth_gap"], 3)

    def test_g4_exempt_beats_g1(self):
        spec = R.RULES["dim_valuation_conclusion"]
        values = {"loss_flag": True, "negative_flag": False,
                  "pe_hist_pct": None, "pe_ind_pct": None}
        rid, result, gates = producer._judge(
            spec, "dim_valuation_conclusion", values, 0.5)
        self.assertEqual((rid, result), ("VAL-00", "loss"))
        self.assertTrue(gates["G4"])

    def test_g1_gate_blocks_low_coverage(self):
        spec = R.RULES["dim_quality_conclusion"]
        values = {"roe_tier": 4, "roe_stability": None,
                  "earnings_quality_tier": None, "leverage_tier": None}
        _rid, result, gates = producer._judge(spec, "dim_quality_conclusion", values, 0.25)
        self.assertIsNone(result)
        self.assertFalse(gates["G1"])

    def test_detail_keys(self):
        detail = producer._detail("VAL-04", {"pe_hist_pct": 10.0}, 1.0, [],
                                  {"G1": True, "G2": True, "G3": True, "G4": False},
                                  __import__("datetime").date(2026, 11, 12),
                                  with_run=True)
        self.assertIn("rule_id", detail)
        self.assertIn("evidence", detail)
        self.assertIn("coverage", detail)
        self.assertIn("as_of", detail)
        for k in ("state_raw", "since_date", "dwell_days", "status", "candidate"):
            self.assertIn(k, detail)

    def test_evidence_carries_per_input_report_period(self):
        """财报派生输入逐项标 report_type/report_date；年报口径标签用 annual 期次。"""
        detail = producer._detail(
            "GRW-02", {"rev_tier": 4, "np_tier": 4}, 1.0, [],
            {"G1": True, "G2": True, "G3": True, "G4": False},
            __import__("datetime").date(2026, 11, 19), with_run=False,
            period_by_field={
                "rev_tier": {"report_type": "Q3", "report_date": "2026-09-30"},
                "persistence": {"report_type": "annual", "report_date": "2025-12-31"},
            },
        )
        rp = detail["evidence"]["report_period"]
        self.assertEqual(rp["rev_tier"]["report_type"], "Q3")
        self.assertEqual(rp["persistence"]["report_type"], "annual")

    def test_detail_without_period_no_extra_key(self):
        detail = producer._detail(
            "VAL-06", {}, 1.0, [],
            {"G1": True, "G2": True, "G3": True, "G4": False},
            __import__("datetime").date(2026, 11, 19), with_run=False)
        self.assertNotIn("report_period", detail["evidence"])
        self.assertNotIn("primary_report_period", detail)

    def test_detail_primary_report_period(self):
        primary = {"report_type": "Q3", "report_date": "2026-09-30"}
        detail = producer._detail(
            "GRW-02", {}, 1.0, [],
            {"G1": True, "G2": True, "G3": True, "G4": False},
            __import__("datetime").date(2026, 11, 19), with_run=False,
            primary_report_period=primary)
        self.assertEqual(detail["primary_report_period"], primary)

    def test_primary_period_only_for_financial_l2(self):
        """主报告期只由 qal_*/grw_* 输入产生；估值/量价 L2 不写该键。"""
        stock = "SH.600519"
        snap = {
            (stock, "qal_roe_ind1_tier", "key_value"): "4",
            (stock, "grw_revenue_yoy_ind1_tier", "key_value"): "4",
            (stock, "val_pe_hist_pct", "num_value"): 18.0,
            (stock, "tst_price_position_52w", "key_value"): "3",
        }
        auto = {stock: {"report_type": "Q3", "report_date": "2026-09-30"}}
        annual = {stock: {"report_type": "annual", "report_date": "2025-12-31"}}
        extras = {"period_auto": auto, "period_annual": annual, "ma": {}}
        for code in ("dim_quality_conclusion", "dim_growth_conclusion"):
            with self.subTest(code=code):
                _v, _c, _m, periods, primary = producer._build_values(
                    R.RULES[code], stock, snap, extras, {})
                self.assertEqual(primary, auto[stock])
                # annual-only 字段逐字段标注，但不覆盖主期次
                if code == "dim_growth_conclusion":
                    self.assertEqual(periods["persistence"], annual[stock])
        for code in ("dim_valuation_conclusion", "dim_position_state"):
            with self.subTest(code=code):
                _v, _c, _m, _p, primary = producer._build_values(
                    R.RULES[code], stock, snap, extras, {})
                self.assertIsNone(primary)


class TestGrowthConclusionIndustry(unittest.TestCase):
    """§5.3：增速档输入切一级行业口径（旧 grw_*_yoy_tier 已拆 ind1/ind2 下线）。"""

    def test_inputs_use_industry_level_1_tags(self):
        spec = R.RULES["dim_growth_conclusion"]
        codes = {i["tag_code"] for i in spec["inputs"] if "tag_code" in i}
        self.assertEqual(len(codes), 4)
        self.assertIn("grw_revenue_yoy_ind1_tier", codes)
        self.assertIn("grw_profit_yoy_ind1_tier", codes)
        self.assertNotIn("grw_revenue_yoy_tier", codes)
        self.assertNotIn("grw_profit_yoy_tier", codes)

    def test_rules_and_params_unchanged(self):
        # 6 码 / 6 规则 / 参数均不动（仅 GRW-02 条件与输入口径变过）
        spec = R.RULES["dim_growth_conclusion"]
        self.assertEqual([r["rule_id"] for r in spec["rules"]],
                         ["GRW-00", "GRW-01", "GRW-02", "GRW-03", "GRW-04", "GRW-05"])
        self.assertEqual({r["result"] for r in spec["rules"]},
                         {"revenue_profit_divergent", "contracting", "fast_sustained",
                          "fast_accelerating", "fast_decelerating", "slow_steady"})
        self.assertEqual((spec["params"]["divergence_gap_tier"],
                          spec["params"]["high_growth_min"],
                          spec["params"]["persistence_min"]), (2, 4, 2))

    def test_grw02_ignores_accel(self):
        """v4：持续高增长不以当期加速度为必要条件，accel 缺失也不能误落 slow_steady。"""
        spec = R.RULES["dim_growth_conclusion"]
        for ac in ("accelerating", "steady", "decelerating", None):
            with self.subTest(accel=ac):
                v = {"rev_tier": 4, "np_tier": 4, "persistence": 2, "accel": ac,
                     "rev_yoy": 20.0, "np_yoy": 20.0, "growth_gap": 0}
                self.assertEqual(R.first_match(spec, v), ("GRW-02", "fast_sustained"))
        # 加速度仍参与 GRW-03/04
        v = {"rev_tier": 4, "np_tier": 3, "persistence": 2,
             "accel": "accelerating", "rev_yoy": 20.0, "np_yoy": 10.0, "growth_gap": 1}
        self.assertEqual(R.first_match(spec, v), ("GRW-03", "fast_accelerating"))
        v["accel"] = "decelerating"
        self.assertEqual(R.first_match(spec, v), ("GRW-04", "fast_decelerating"))


class TestValueQualityV3(unittest.TestCase):
    """§6.1 D22：四档质量矩阵 + 单侧估值归中性 + moderate 三码。"""

    def setUp(self):
        self.spec = R.RULES["biz_value_quality_type"]

    def test_class_maps(self):
        self.assertEqual(R.VAL_CLASS["hist_cheap_ind_expensive"], "neutral")
        self.assertEqual(R.VAL_CLASS["hist_expensive_ind_cheap"], "neutral")
        self.assertEqual(R.QAL_CLASS["moderate"], "moderate")

    def test_moderate_cells(self):
        cases = [
            ("cheap", "moderate", "VQ-C1M", "moderate_rel_cheap"),
            ("neutral", "moderate", "VQ-C4M", "moderate_rel_fair"),
            ("expensive", "moderate", "VQ-C6M", "moderate_rel_expensive"),
        ]
        for vc, qc, rid, result in cases:
            with self.subTest(cell=(vc, qc)):
                v = {"valuation_class": vc, "quality_class": qc}
                self.assertEqual(R.first_match(self.spec, v), (rid, result))

    def test_priorities_unique(self):
        ps = [r["priority"] for r in self.spec["rules"]]
        self.assertEqual(len(ps), len(set(ps)))


class TestGrowthQualityV3(unittest.TestCase):
    """§6.2 v3：FCF 精细化门禁 + 一级行业现金转化档。"""

    def setUp(self):
        self.spec = R.RULES["biz_growth_quality_type"]

    def test_inputs(self):
        codes = {i["tag_code"] for i in self.spec["inputs"] if "tag_code" in i}
        self.assertIn("qal_earnings_quality_ind1", codes)
        self.assertNotIn("qal_earnings_quality", codes)
        # FCF 不设 anchor：GQ-00/01/03 不能被门禁无差别拦截
        fcf = next(i for i in self.spec["inputs"] if i["field"] == "fcf_sign")
        self.assertIsNone(fcf.get("role"))

    def test_fcf_gate_only_blocks_positive_code(self):
        base = {"growth_code": "slow_steady", "earnings_quality_tier": 3,
                "persistence": 2, "accel": "steady"}
        for sign in (0, 1):
            self.assertEqual(R.first_match(self.spec, {**base, "fcf_sign": sign}),
                             ("GQ-04", "sustained_cash_backed"))
        for sign in (None, -1):
            self.assertEqual(R.first_match(self.spec, {**base, "fcf_sign": sign})[1], None)
        # 负面码不依赖 FCF，缺 FCF 仍发布
        self.assertEqual(R.first_match(self.spec, {**base, "growth_code": "contracting",
                                                  "fcf_sign": None}),
                         ("GQ-00", "contraction"))
        self.assertEqual(R.first_match(self.spec, {**base, "earnings_quality_tier": 2,
                                                  "fcf_sign": None}),
                         ("GQ-01", "paper_growth"))


class TestPositionStateV3(unittest.TestCase):
    """§5.4 v3：POS-01 去 bullish、POS-04 去 position==3，修复两个近死档。"""

    def setUp(self):
        self.spec = R.RULES["dim_position_state"]

    def test_pos01_low_volume_without_ma(self):
        for ma in ("bearish", "neutral", "bullish", None):
            with self.subTest(ma=ma):
                v = {"position_tier": 2, "volume_ratio_60": 2.5, "ma_alignment": ma,
                     "momentum_tier": 3, "volume_surge": "surge"}
                self.assertEqual(R.first_match(self.spec, v),
                                 ("POS-01", "low_volume_start"))

    def test_pos04_any_bullish_position(self):
        for pos in (1, 2, 3, 4, 5):
            with self.subTest(position=pos):
                v = {"position_tier": pos, "volume_ratio_60": 1.0, "ma_alignment": "bullish",
                     "momentum_tier": 3, "volume_surge": "normal"}
                # pos<=2 且 vr>=2 才走 POS-01；vr=1.0 不抢，落到 POS-04
                self.assertEqual(R.first_match(self.spec, v),
                                 ("POS-04", "trend_continuation"))

    def test_thresholds_unchanged(self):
        p = self.spec["params"]
        self.assertEqual((p["low_pos_max"], p["high_pos_min"], p["vol_quiet_max"],
                          p["vol_active_min"], p["weak_momentum_max"]), (2, 4, 0.5, 2.0, 2))


class TestQualityConclusionD21(unittest.TestCase):
    """§5.2 D21/v3：一级行业输入 + 7 码 + 杠杆降级 + ROE 绝对底线 + anchor_any 门禁。"""

    def setUp(self):
        self.spec = R.RULES["dim_quality_conclusion"]

    def test_inputs_use_industry_level_1_tags(self):
        codes = {i["tag_code"] for i in self.spec["inputs"] if "tag_code" in i}
        self.assertEqual(len(codes), 4)
        self.assertTrue(all("_ind1" in c for c in codes), codes)
        self.assertEqual(self.spec["inputs"][-1]["source_col"], "num_value")

    def test_seven_results_literal_params(self):
        results = {r["result"] for r in self.spec["rules"]}
        self.assertEqual(results, {"deteriorating", "paper_profit", "solid",
                                   "high_but_volatile", "leveraged", "moderate", "weak"})
        p = self.spec["params"]
        self.assertEqual((p["stable_max"], p["volatile_min"], p["quality_weak_max"],
                          p["quality_ok_min"], p["leverage_high"], p["roe_high"],
                          p["moderate_min"], p["moderate_max"], p["roe_floor_pct"]),
                         (2, 4, 2, 3, 4, 4, 3, 4, 5))

    def test_all_seven_rules_first_match(self):
        cases = [
            ({"roe_stability": 4, "earnings_quality_tier": 2},
             "QAL-01", "deteriorating"),
            ({"roe_stability": 2, "earnings_quality_tier": 2},
             "QAL-02", "paper_profit"),
            ({"roe_tier": 4, "roe_stability": 2, "earnings_quality_tier": 3, "roe_raw": 6.0},
             "QAL-04", "solid"),
            ({"roe_tier": 4, "roe_stability": 4, "earnings_quality_tier": 3, "roe_raw": 6.0},
             "QAL-05", "high_but_volatile"),
            ({"roe_tier": 2, "leverage_tier": 4, "earnings_quality_tier": 3},
             "QAL-03", "leveraged"),
            ({"roe_tier": 3, "earnings_quality_tier": 3},
             "QAL-07", "moderate"),
            ({"roe_tier": 2, "earnings_quality_tier": 3},
             "QAL-06", "weak"),
        ]
        for values, rule_id, result in cases:
            with self.subTest(rule_id=rule_id):
                self.assertEqual(R.first_match(self.spec, values), (rule_id, result))

    def test_leverage_is_downgrade_not_veto(self):
        # 高 ROE 且现金实：即使杠杆档=4 也不被 QAL-03 一票否决
        values = {"roe_tier": 4, "roe_stability": 2, "earnings_quality_tier": 3,
                  "roe_raw": 6.0, "leverage_tier": 4}
        self.assertEqual(R.first_match(self.spec, values), ("QAL-04", "solid"))

    def test_roe_floor_falls_to_moderate(self):
        # §5.2 中文写法：3-4 档归中等。ROE 绝对值 <5% 的 4 档票落 moderate 而非 weak，
        # 兑现「低于 5% 最高只能评盈利中等」
        values = {"roe_tier": 4, "roe_stability": 2, "earnings_quality_tier": 3,
                  "roe_raw": 3.0, "leverage_tier": 1}
        self.assertEqual(R.first_match(self.spec, values), ("QAL-07", "moderate"))
        # 5 档同样不得评两个正面码（保险丝）
        self.assertNotIn(R.first_match(self.spec, {**values, "roe_tier": 5})[1],
                         ("solid", "high_but_volatile"))

    def test_anchor_any_gate(self):
        values = {"roe_tier": 2, "earnings_quality_tier": None}
        _rid, result, gates = producer._judge(
            self.spec, "dim_quality_conclusion", values, 0.8)
        self.assertTrue(gates["G2"])
        self.assertIsNotNone(result)
        values = {"roe_tier": None, "earnings_quality_tier": None}
        _rid, result, gates = producer._judge(
            self.spec, "dim_quality_conclusion", values, 0.6)
        self.assertFalse(gates["G2"])
        self.assertIsNone(result)

    def test_moderate_in_l3_class_map(self):
        # D22 裁决：单开 moderate 类（D21 时暂未纳入）
        self.assertEqual(R.QAL_CLASS["moderate"], "moderate")


class TestReportPeriodAdaptation(unittest.TestCase):
    """2026-11-19 报告期自适应：同期次分组 + 年报口径标签不动。"""

    def test_auto_std_filter_binds_type_to_period_end(self):
        from profiling import quantile
        src = _src("profiling/quantile.py")
        self.assertIn('def _financial_std_filter_any(', src)
        self.assertIn("report_type='auto'", src)
        # 四类标准期末日都在同一 OR 中，且每类按 report_type 绑定其标准期末日。
        # 检查运行时生成的 SQL（源码是 f-string 动态拼的，不能字面搜）。
        clause = quantile._financial_std_filter_any()
        self.assertTrue(clause.startswith("AND ("))
        for rt, months in (("Q1", 3), ("H1", 6), ("Q3", 9), ("annual", 12)):
            with self.subTest(rt=rt):
                self.assertIn(f"report_type = '{rt}'", clause)
                self.assertIn(f"INTERVAL '{months} months'", clause)

    def test_context_grp_includes_report_type_only_for_auto(self):
        """D16 甲：load_financial_context 一次产出 grp_l1/grp_l2，报告期后缀只 auto 时附加。"""
        src = _src("profiling/quantile.py")
        self.assertIn('report_type: str = "annual"', src)
        self.assertIn('df["grp_l1"]', src)
        self.assertIn('df["grp_l2"]', src)
        self.assertIn('if report_type == "auto" else ""', src)

    def test_quality_growth_use_auto_except_years_tags(self):
        q = _src("profiling/tags/quality.py")
        g = _src("profiling/tags/growth.py")
        self.assertIn('load_financial_context(conn, as_of, "auto")', q)
        self.assertIn('load_financial_context(conn, as_of, "auto")', g)
        # 年报口径的两个标签必须走 _hist_annual
        self.assertIn("streak = _streak(_hist_annual(as_of, 15),", q)
        self.assertIn("hist = _hist_annual(as_of, 15)", g)
        # 序列类走同类型 auto 历史
        self.assertIn("g = _hist(as_of, 3)", q)
        self.assertIn("hist = _hist(as_of, 2)", g)

    def test_years_tags_keep_v1_and_semantics(self):
        q = registry.get("qal_profit_years")
        g = registry.get("grw_growth_persistence")
        self.assertEqual(q.version, "v1")
        self.assertEqual(g.version, "v1")
        self.assertEqual(q.name, "连续盈利年数")
        self.assertIn("年", q.compute_logic)

    def test_changed_l1_tags_bumped_to_v2(self):
        # 阶段 C：行业标签已拆成一级/二级并存（新 code），仍须保持 v2 版本指纹
        for code in ("qal_roe_ind1_tier", "qal_roe_ind2_tier",
                     "qal_gross_margin_ind1_tier", "qal_net_margin_ind1_tier",
                     "qal_leverage_ind1_tier", "qal_roe_stability_ind1",
                     "qal_earnings_quality_ind1",
                     "grw_revenue_yoy_ind1_tier", "grw_profit_yoy_ind1_tier",
                     "grw_growth_accel"):
            with self.subTest(code=code):
                self.assertIsNotNone(registry.get(code), code)
                self.assertEqual(registry.get(code).version, "v2", code)


class TestWiringAndSchema(unittest.TestCase):
    def test_compute_profile_skips_derived_and_calls_producer(self):
        src = _src("compute_profile.py")
        self.assertIn('meta.source_type == "derived"', src)
        self.assertIn("conclusion_producer.produce(", src)
        self.assertIn("conclusion_rules.sync_conclusion_dict(", src)
        # sync_conclusion_dict 必须在 try/except（字典同步）之外，version 不一致硬失败
        self.assertGreater(src.index("sync_conclusion_dict(conn_sync)"),
                           src.index("标签字典同步失败"))

    def test_run_py_filters_derived(self):
        self.assertIn('m.source_type != "derived"', _src("profiling/run.py"))

    def test_jsonb_refresh_has_explicit_cast(self):
        # UPDATE ... FROM (VALUES) 的派生列被推断为 text，必须显式 ::jsonb
        self.assertIn("v.detail::jsonb", _src("profiling/conclusions/producer.py"))

    def test_schema_sources_have_jsonb_columns_and_migration(self):
        for rel in ("sql/schema.sql", "profiling/schema.py"):
            src = _src(rel)
            self.assertIn("conclusion_dict JSONB", src, rel)
            self.assertIn("conclusion_detail JSONB", src, rel)
            self.assertIn("ADD COLUMN IF NOT EXISTS conclusion_dict", src, rel)
            self.assertIn("ADD COLUMN IF NOT EXISTS conclusion_detail", src, rel)
            self.assertIn("/ derived", src, rel)

    def test_tag_meta_has_no_conclusion_dict_field(self):
        # 不给 TagMeta 加字段，避免 sync_registry 全列 upsert 覆盖规则契约
        src = _src("profiling/registry.py")
        self.assertNotIn("conclusion_dict:", src)

    def test_rebuild_is_manual_script_not_service(self):
        # 用户决定：rebuild 不注册 systemd 服务，只提供手动脚本
        self.assertFalse((ROOT / "system/compute-conclusions-rebuild.service").exists())
        self.assertNotIn("compute-conclusions-rebuild", _src("bootstrap.sh"))
        self.assertNotIn("compute-conclusions-rebuild", _src("deploy.sh"))
        src = _src("compute_conclusions_rebuild.py")
        self.assertIn("--yes", src)             # 破坏性操作默认预演
        self.assertIn("log.info", src)          # 执行过程有日志
        self.assertIn("peak", src.lower())      # 实测峰值内存
        self.assertIn("producer.rebuild(", src)
        self.assertIn("IS DISTINCT FROM", src)  # 重建后检查版本残留

    def test_producer_rebuild_logs_deletions(self):
        src = _src("profiling/conclusions/producer.py")
        self.assertIn("重建：已删除", src)
        self.assertIn("手动脚本 compute_conclusions_rebuild.py", src)


if __name__ == "__main__":
    unittest.main()
