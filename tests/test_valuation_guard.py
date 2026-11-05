"""A 股历史估值回填后的 L1 估值锚护栏回归测试（不连库）。

背景（2026-11-05，doc/market_state_data_architecture.md §5.2 评审）：
`backfill_a_valuation_baostock.py` 把 `a_daily_quote.pe_ttm_ratio` 从 2018 回填到
1991，但该列是 **QFQ 缩放值**（复权因子随除权累积单调衰减，年度中位 1991=0.12 /
1995=3.40 / 2000=19.08 / 2018=29.70），同列水平值跨期不可比；且字段覆盖率从
1995 的 97.4% 单调衰减到 2017 的 11.8%，成分池在 2018-01-02 由 302 只跳到 2984 只。
直接跑 `--backfill` 会把 300 只票的中位数写进 L1，并污染 expanding 分位与
`VAL.ERP_Z` 的扩展均值/标准差。

锁住五件事，任何一条被改坏都说明估值锚回到了「把回填段当长历史用」的状态：
  ① `VALUATION_ASOF_START` / `QUOTE_VALUATION_CODES` 存在且取值正确
  ② `_valuation_asof_guard` 截断行为、2018+ 逐值零影响、混合 index 安全
  ③ `calc_cn` 同时有字段级覆盖门禁（`pe_n` / `_half`）与日期硬截断
  ④ `_write_market` 在分位输入层再挡一次（库内历史 raw_value 也不进参考分布）
  ⑤ 估值横截面指标带 `survivorship_bias` / `quality='low'`；字典 `formula` 不因
     本次改动变化（改 formula 会换 params_hash，而护栏对 2018+ 数值零影响，不该触发重算）
"""
import datetime
import inspect
import os
import re
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import market_state_daily as msd

D = datetime.date
ASOF = D(2018, 1, 2)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _indicator(code):
    return next(m for m in msd.INDICATORS if m["code"] == code)


class TestAsOfGuard(unittest.TestCase):
    """护栏常量与纯函数行为。"""

    def test_constants(self):
        self.assertEqual(msd.VALUATION_ASOF_START, ASOF)
        self.assertEqual(msd.QUOTE_VALUATION_CODES, frozenset({
            "VAL.PE_TTM_MEDIAN", "VAL.PB_MEDIAN",
            "VAL.PCT_BELOW_PB_1", "VAL.ERP",
        }))
        # ERP_Z 由 ERP 派生，DIV_SPREAD / ERP_LG / BUFFETT 是独立口径，均不受护栏
        for c in ("VAL.ERP_Z", "VAL.ERP_LG", "VAL.DIV_SPREAD", "VAL.BUFFETT"):
            self.assertNotIn(c, msd.QUOTE_VALUATION_CODES)

    def test_cuts_history_keeps_asof_and_after(self):
        s = pd.Series([3.40, 27.48, 29.70, 30.0],
                      index=[D(1995, 6, 1), D(2017, 12, 29), ASOF, D(2026, 11, 4)])
        g = msd._valuation_asof_guard(s)
        self.assertTrue(pd.isna(g.iloc[0]))
        self.assertTrue(pd.isna(g.iloc[1]))
        self.assertEqual(g.iloc[2], 29.70)     # 起点当天必须保留
        self.assertEqual(g.iloc[3], 30.0)

    def test_no_effect_on_current_regime(self):
        """核心性质：护栏对 2018+ 逐值零影响 → 不触发 params_hash / 全量重算。"""
        idx = [D(2018, 1, 2), D(2020, 3, 20), D(2026, 11, 4)]
        s = pd.Series([29.70, 24.10, 31.55], index=idx)
        pd.testing.assert_series_equal(s, msd._valuation_asof_guard(s))

    def test_custom_start(self):
        s = pd.Series([1.0, 2.0], index=[D(2010, 1, 4), D(2020, 1, 2)])
        g = msd._valuation_asof_guard(s, start_date=D(2015, 1, 1))
        self.assertTrue(pd.isna(g.iloc[0]))
        self.assertEqual(g.iloc[1], 2.0)

    def test_empty_and_all_nan(self):
        self.assertTrue(msd._valuation_asof_guard(pd.Series(dtype=float)).empty)
        self.assertIsNone(msd._valuation_asof_guard(None))
        s = pd.Series([float("nan"), float("nan")], index=[D(1995, 1, 3), D(2020, 1, 2)])
        self.assertTrue(msd._valuation_asof_guard(s).isna().all())

    def test_mixed_index_types(self):
        """concat(hist, new_raw) 可能把 index 提升为 Timestamp（与 date 比较会抛 TypeError）。"""
        for past in (pd.Timestamp("2017-01-03"), datetime.datetime(2017, 1, 3), D(2017, 1, 3)):
            g = msd._valuation_asof_guard(pd.Series([1.0], index=[past]))
            self.assertTrue(pd.isna(g.iloc[0]), past)
        s = pd.Series([1.0, 2.0],
                      index=[pd.Timestamp("2017-01-03"), pd.Timestamp("2020-01-03")])
        g = msd._valuation_asof_guard(s)
        self.assertTrue(pd.isna(g.iloc[0]))
        self.assertEqual(g.iloc[1], 2.0)

    def test_before_mask_shared_by_guard_and_gate(self):
        """掩码函数被护栏与字段级门禁共用，起点当天必须为 False。"""
        idx = pd.Index([pd.Timestamp("2017-01-03"), ASOF, D(2020, 1, 3)])
        self.assertEqual(msd._valuation_before_mask(idx).tolist(), [True, False, False])


class TestCalcCnGuards(unittest.TestCase):
    """字段级覆盖门禁 + 产出层日期硬截断（源码钉子，不连库）。"""

    def setUp(self):
        self.src = inspect.getsource(msd.calc_cn)

    def test_pe_n_aggregate_exists(self):
        self.assertIn("AS pe_n", self.src)
        self.assertIn("count(*) FILTER (WHERE pe > 0)", self.src)

    def test_field_level_coverage_gate(self):
        """与 calc_hk 同款 _half 判据，作为回填段的第二道防御。"""
        self.assertIn("_half = df[\"n\"] * 0.5", self.src)
        self.assertIn("_hist = _valuation_before_mask(df.index)", self.src)
        self.assertIn("~_hist | (df[\"pe_n\"] >= _half)", self.src)
        self.assertIn("df[\"pe_med\"].where(_pe_ok)", self.src)
        self.assertIn("df[\"pb_med\"].where(_pb_ok)", self.src)

    def test_gate_limited_to_backfill_segment(self):
        """门禁只在 2018 前生效：2018+ 的偶发字段塌陷是既有独立缺口，不能顺带改掉。

        若对全时段生效，2018+ 某些日会被置 NaN，而库内旧值因 `skip_null_updates`
        不会被清除 → 新旧混存，比不改更糟（实测 2026-10-26~29 pe 覆盖仅 0.37~0.38）。
        """
        self.assertIn("刻意不作用于 2018+", self.src)
        hist = pd.Series([True, True, False, False])       # 是否在回填段
        covered = pd.Series([False, True, False, True])    # 是否过 _half
        self.assertEqual((~hist | covered).tolist(), [False, True, True, True])

    def test_asof_guard_in_output(self):
        """字段级门禁挡不住 QFQ 缩放（1994-96 覆盖 87-97% 却只有 3~5 倍 PE）→ 硬截断必须还在。"""
        self.assertIn("_valuation_asof_guard", self.src)
        self.assertIn("for c in QUOTE_VALUATION_CODES", self.src)


class TestWriteMarketGuard(unittest.TestCase):
    """分位输入层：库内历史 raw_value 也不能进 expanding 参考分布。"""

    def test_combined_guarded_before_percentile(self):
        src = inspect.getsource(msd._write_market)
        self.assertIn('if market == "CN"', src)
        self.assertIn("combined[c] = _valuation_asof_guard(combined[c])", src)
        # 必须在 ERP_Z 与分位计算之前
        self.assertLess(src.index("combined[c] = _valuation_asof_guard(combined[c])"),
                        src.index("_expanding_zscore("))
        self.assertLess(src.index("combined[c] = _valuation_asof_guard(combined[c])"),
                        src.index("_expanding_percentile("))


class TestExtraAndDict(unittest.TestCase):
    """extra 质量标注与字典起点同步（formula 不得变，否则误触发 params_hash）。"""

    def test_survivorship_flag_for_cross_sectional_valuation(self):
        for c in sorted(msd.QUOTE_VALUATION_CODES):
            self.assertEqual(msd._breadth_extra(c, D(2026, 11, 4), None),
                             {"survivorship_bias": True, "quality": "low"}, c)
        # 外部指数 / 独立口径不是个股横截面，不标
        for c in ("VAL.ERP_LG", "VAL.ERP_Z", "VAL.DIV_SPREAD", "VAL.BUFFETT"):
            self.assertIsNone(msd._breadth_extra(c, D(2026, 11, 4), None), c)

    def test_dict_remark_updated_without_changing_formula(self):
        pe = _indicator("VAL.PE_TTM_MEDIAN")
        self.assertIn("baostock", pe["remark"])
        self.assertIn("QFQ", pe["remark"])
        self.assertNotIn("baostock", pe["formula"])   # params_hash 依赖 formula
        self.assertIn("2018-01-02", _indicator("VAL.ERP")["remark"])
        self.assertIn("baostock", _indicator("VAL.ERP_Z")["remark"])
        self.assertIn("未回填 PB", _indicator("VAL.PB_MEDIAN")["remark"])


class TestDocumentation(unittest.TestCase):
    """文档 5.2 / §2.4 / 附录 B 与代码护栏保持一致。"""

    def setUp(self):
        self.arch = _read("doc/market_state_data_architecture.md")

    def test_asof_and_reason_documented(self):
        self.assertIn("VALUATION_ASOF_START", self.arch)
        self.assertIn("QFQ 缩放", self.arch)
        self.assertIn("302", self.arch)           # 成分池断裂实测
        self.assertIn("baostock", self.arch)

    def test_recalc_trigger_documented(self):
        self.assertIn("参考样本集变更", self.arch)
        self.assertIn("全量重算", self.arch)

    def test_cn_constraint_reason_rewritten(self):
        self.assertIn("口径断裂", self.arch)
        self.assertIn("物理缺口", self.arch)  # 保留被推翻的旧结论作为历史说明


def _valuation_members(text):
    """取出估值维度的 members[] 种子（文档里宽度也有 members，按锚指标识别）。"""
    for m in re.finditer(r'"members"\s*:\s*\[(.*?)\]', text, re.S):
        if "VAL.PE_TTM_MEDIAN" in m.group(1):
            return m.group(1)
    return ""


class TestValuationMembership(unittest.TestCase):
    """`VAL.ERP_Z` 移出 members[] 的契约钉子（§5.2 v1.2 / market_profile v2.1.13）。

    ERP_Z 是 z-score、percentile 恒 NULL，而 members[] 只读 percentile → 留在数组里
    不改 score，却恒定压低 coverage、让 sign/校验判据失效。两份规格必须同步，
    否则按旧稿落地会算错 coverage 分母。
    """

    def setUp(self):
        self.arch = _read("doc/market_state_data_architecture.md")
        self.profile = _read("doc/market_profile.md")

    def test_both_specs_drop_erp_z_from_members(self):
        for name, text in (("arch", self.arch), ("market_profile", self.profile)):
            with self.subTest(doc=name):
                block = _valuation_members(text)
                self.assertTrue(block, "%s 未找到估值 members[]" % name)
                self.assertNotIn("VAL.ERP_Z", block)
                self.assertEqual(block.count('"code"'), 6,
                                 "%s 估值成员应为 6 个" % name)

    def test_member_counts_and_coverage_sample(self):
        self.assertIn("6（HK 5）", self.arch)
        self.assertIn("6（HK 5）", self.profile)
        # 输出样例：HK 5 个成员全有效 → coverage 1.0、4 个校验项
        self.assertIn('"coverage": 1.0000', self.arch)
        self.assertIn('"check": {"n": 4', self.arch)
        self.assertIn('"coverage": 1.0000', self.profile)
        self.assertIn('"n": 4, "divergent"', self.profile)

    def test_no_percentile_rule_forbids_membership(self):
        """通用纪律：NO_PERCENTILE 命中项不得进任何 dimension_dict.members[]。"""
        src = _read("market_state_daily.py")
        for name, text in (("arch", self.arch), ("market_profile", self.profile),
                           ("market_state_daily.py", src)):
            with self.subTest(doc=name):
                self.assertIn("不得列入任何 `dimension_dict.members[]`", text)

    def test_dict_remark_documented_without_changing_formula(self):
        """remark 可改、formula 不可改（后者进 params_hash，改了会误触发全量重算）。"""
        erpz = _indicator("VAL.ERP_Z")
        self.assertIn("不进 `DIM.VALUATION.members[]`", erpz["remark"])
        self.assertNotIn("members", erpz["formula"])
        self.assertIn("expanding_mean(VAL.ERP)", erpz["formula"])


if __name__ == "__main__":
    unittest.main()
