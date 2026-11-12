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
        rid, result = R.first_match(spec, {
            "loss_flag": False, "negative_flag": False,
            "pe_gap": 2.0, "pe_gap_dir": "hist_lower",
            "pe_hist_pct": 18.0, "pe_ind_pct": 19.0,
        })
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


class TestEnumContract(unittest.TestCase):
    def test_35_codes_exactly_cover_all_results(self):
        codes = set()
        for m in registry.all_tags():
            if m.source_type == "derived":
                codes |= set(m.enum_values)
        results = {r["result"] for spec in R.RULES.values()
                   for r in spec["rules"] if r["result"] is not None}
        self.assertEqual(len(codes), 35)
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
            self.assertEqual(m.version, "v1")
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
        spec = R.RULES["dim_valuation_conclusion"]
        values = {"pe_hist_pct": 10.0, "pe_ind_pct": 60.0}
        producer._derive(spec, values)
        self.assertEqual(values["pe_gap"], 50.0)
        self.assertEqual(values["pe_gap_dir"], "hist_lower")
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
