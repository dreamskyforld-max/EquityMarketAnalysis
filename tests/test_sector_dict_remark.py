"""第④层 `industry.indicator_dict.remark` 三段式规范测试（防回退）。

参照 `regime.indicator_dict` 的事实标准（market_state_daily.py:211/215/707/255）：
单行 `定义：…　·　判读：…　·　数据：…`，可扩展 口径：/⚠/已知限制：，无换行、
无【】方括号；含实测数字、口径变更理由、跨市场可比性与样本门禁。

规范出处：sql/schema.sql:1710 / doc/industry_allocation_feasibility.md §3.1。
⚠ 关键边界：remark **不进 params_hash**，改文案不得触发历史重算。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sector_layer as sl  # noqa: E402
import sector_layer_hk as slh  # noqa: E402


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


ALL_DEFS = ([("CN", d) for d in sl.INDICATOR_DEFS] +
            [("HK", d) for d in slh.HK_INDICATOR_DEFS])


class TestThreePartFormat(unittest.TestCase):
    """66 条 remark 逐条对齐参考表格式"""

    def test_all_66_defs(self):
        self.assertEqual(len(sl.INDICATOR_DEFS), 41)
        self.assertEqual(len(slh.HK_INDICATOR_DEFS), 25)
        self.assertEqual(len(ALL_DEFS), 66)

    def test_every_remark_is_single_line_three_part(self):
        for market, d in ALL_DEFS:
            r = d["remark"]
            with self.subTest(code=market + "/" + d["code"]):
                self.assertTrue(r.startswith("定义："))
                self.assertIn("判读：", r)
                self.assertIn("数据：", r)
                self.assertEqual(r.count("数据："), 1)   # 参考表单数据段，扩展用 口径：/⚠
                self.assertEqual(r.count("定义："), 1)
                self.assertEqual(r.count("判读："), 1)
                self.assertIn(sl._SEP, r)             # 全角空格 + · + 全角空格
                self.assertNotIn("\n", r)
                self.assertNotIn("【", r)
                self.assertNotIn("】", r)

    def test_length_in_reference_range(self):
        """参考表 128 条实测 75~566 字、均值 180；本层因口径声明更密，允许放宽上限。"""
        for market, d in ALL_DEFS:
            with self.subTest(code=market + "/" + d["code"]):
                self.assertGreaterEqual(len(d["remark"]), 120)
                self.assertLessEqual(len(d["remark"]), 600)

    def test_no_development_phase_fragments(self):
        """旧版「阶段1」「阶段2」碎片式 remark 不得回流。"""
        for market, d in ALL_DEFS:
            with self.subTest(code=market + "/" + d["code"]):
                r = d["remark"]
                self.assertFalse(r.startswith("阶段"))
                self.assertNotIn("；阶段1", r)
                self.assertNotIn("；阶段2", r)
        self.assertNotIn('remark="阶段2"', _read("sector_layer.py"))

    def test_hk_declares_survivorship_bias(self):
        """HK 成分只有当前快照，每条必须自带 PIT / 不可比声明（§2.4 铁律）。"""
        for d in slh.HK_INDICATOR_DEFS:
            with self.subTest(code=d["code"]):
                self.assertIn("幸存者偏差", d["remark"])
                self.assertIn("不同构", d["remark"])

    def test_separator_is_ideographic_space_middle_dot(self):
        self.assertEqual(sl._SEP, "\u3000\u00b7\u3000")
        self.assertEqual(slh._SEP, sl._SEP)
        reference = _read("market_state_daily.py")
        self.assertIn("\u3000\u00b7\u3000", reference)   # 与参考表同一分隔符


class TestHashBoundary(unittest.TestCase):
    """改 remark 不得换 params_hash、不得触发历史重算"""

    def test_remark_not_in_cn_hash(self):
        a = dict(sl.INDICATOR_DEFS[0])
        b = dict(a, remark="任意改写后的三段式文案")
        self.assertEqual(sl._params_hash(a), sl._params_hash(b))

    def test_remark_not_in_hk_hash(self):
        a = dict(slh.HK_INDICATOR_DEFS[0])
        b = dict(a, remark="任意改写后的三段式文案")
        self.assertEqual(slh._hk_params_hash(a), slh._hk_params_hash(b))

    def test_hash_payload_fields_unchanged(self):
        """payload 只含 code/unit/direction/formula/model_key/版本（+HK 的 market）。"""
        src = _read("sector_layer.py")
        payload = src.split("def _params_hash")[1].split("return")[0]
        self.assertNotIn("remark", payload)
        hk = _read("sector_layer_hk.py")
        payload_hk = hk.split("def _hk_params_hash")[1].split("return")[0]
        self.assertNotIn("remark", payload_hk)


class TestDocAndDDLSync(unittest.TestCase):
    """三段式要求必须写进 DDL、设计与代码，且两处逐字一致"""

    def test_ddl_dual_source_inline_comment(self):
        for rel in ("sql/schema.sql", "regime_schema.py"):
            with self.subTest(rel=rel):
                self.assertIn("规范化三段式：【定义】是什么 /【判读】怎么看 /【数据】样本与断点",
                              _read(rel))

    def test_column_comment_dual_source(self):
        schema = _read("sql/schema.sql")
        code = _read("regime_schema.py")
        key = "规范化三段式（与 regime.indicator_dict 事实标准一致）"
        self.assertIn("COMMENT ON COLUMN industry.indicator_dict.remark IS '" + key, schema)
        self.assertIn(key, code)
        self.assertIn("库内手改会被幂等 upsert 覆盖", schema)
        self.assertIn("库内手改会被幂等 upsert 覆盖", code)

    def test_design_doc_requires_three_part(self):
        self.assertIn("（三段式：定义/判读/数据）",
                      _read("doc/industry_allocation_feasibility.md"))

    def test_code_is_dictionary(self):
        """库内手改无效：两个 sync 函数都是幂等 upsert，且带复合冲突键。"""
        for rel in ("sector_layer.py", "sector_layer_hk.py"):
            with self.subTest(rel=rel):
                src = _read(rel)
                self.assertIn("def sync_dict", src)
                self.assertIn("sync_dict", src)
                self.assertIn("bulk_upsert", src)


if __name__ == "__main__":
    unittest.main()
