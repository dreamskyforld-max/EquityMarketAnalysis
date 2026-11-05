"""L2 新增维度时的隔离性回归测试（不连库）。

目标不是「自动扩展」，而是保证后续新增维度指标时：
  ① 改动集中在 `build()` 的一处分派 + 一个独立函数；
  ② 现有 `DIM.BREADTH` / `DIM.VALUATION` 的调用与输出完全不变；
  ③ 未注册的 dimension_code 只跳过该维度，不拖垮其它维度；
  ④ 新维度不会静默继承旧维度的专属默认值 / 阈值。
"""
import datetime
import inspect
import os
import sys
import unittest
from unittest import mock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_dimension_value as bd

D = datetime.date
DAY = D(2026, 11, 4)
LABELS = ["极便宜", "偏便宜", "中性", "偏贵", "极贵"]

SCOPES = {
    "VAL.PE_TTM_MEDIAN": "CN+HK", "VAL.PB_MEDIAN": "CN+HK",
    "VAL.PCT_BELOW_PB_1": "CN+HK", "VAL.DIV_SPREAD": "CN+HK",
    "VAL.ERP": "CN+HK", "VAL.BUFFETT": "CN",
}

MEMBERS = [
    {"code": "VAL.PE_TTM_MEDIAN", "sign": 1, "weight": 1.0, "block": "anchor"},
    {"code": "VAL.PB_MEDIAN", "sign": 1, "weight": 0.0, "block": "check"},
    {"code": "VAL.PCT_BELOW_PB_1", "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.DIV_SPREAD", "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.ERP", "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.BUFFETT", "sign": 1, "weight": 0.0, "block": "check"},
]


def _dim(params=None):
    """不带任何维度专属开关的字典——模拟「新维度只写了最小 params」。"""
    p = {"coverage_min": 0.6, "dispersion_max": 30,
         "quality_sample": 750, "check_divergence": 20}
    p.update(params or {})
    return {
        "members": MEMBERS,
        "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
        "state_labels": LABELS, "params": p,
    }


def _pct(anchor=70.0):
    return pd.DataFrame(
        {"VAL.PE_TTM_MEDIAN": [anchor], "VAL.PB_MEDIAN": [72.0],
         "VAL.PCT_BELOW_PB_1": [30.0], "VAL.DIV_SPREAD": [25.0],
         "VAL.ERP": [28.0], "VAL.BUFFETT": [65.0]},
        index=pd.Index([DAY], name="trade_date"))


def _run_single_anchor(params=None, market="CN", n_sample=1000.0):
    """拦截 DB 边界跑 build_valuation_dimension，返回它本应写入的行。"""
    cap = {}
    with mock.patch.object(bd, "_load_market_scopes", return_value=SCOPES), \
         mock.patch.object(bd, "_load_percentiles", return_value=_pct()), \
         mock.patch.object(bd, "_anchor_sample_count",
                           return_value=pd.Series([n_sample], index=[DAY])), \
         mock.patch.object(bd, "bulk_upsert",
                           side_effect=lambda c, t, rows, **kw: cap.update(rows=rows)):
        stat = bd.build_valuation_dimension(None, _dim(params), "ph", market,
                                      D(2000, 1, 1), DAY, True)
    return cap.get("rows", []), stat


def _stats():
    return {"rows": 10, "dates": 10, "state_rows": 8}


class TestDispatchIsolation(unittest.TestCase):
    def test_unknown_dimension_skips_only_that_dimension(self):
        dims = [bd.DIM_BREADTH, "DIM.NEW", bd.DIM_VALUATION]
        with mock.patch.object(bd, "_active_dimensions", return_value=dims), \
             mock.patch.object(bd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(bd, "build_breadth_dimension", return_value=_stats()) as bm, \
             mock.patch.object(bd, "build_valuation_dimension", return_value=_stats()) as sa:
            st = bd.build_all_dimensions(None, start=D(2026, 1, 1), end=DAY, write=False,
                          markets=("CN",))
        self.assertEqual(bm.call_count, 1)
        self.assertEqual(sa.call_count, 1)
        self.assertNotIn("DIM.NEW", st["CN"]["dims"])
        self.assertEqual(st["CN"]["rows"], 20)

    def test_adding_a_third_dimension_does_not_change_existing_calls(self):
        """有无第三维度，传给现有两生产者的参数必须逐项一致。"""
        base = [bd.DIM_BREADTH, bd.DIM_VALUATION]
        extra = base + ["DIM.NEW"]

        def run(dims):
            with mock.patch.object(bd, "_active_dimensions", return_value=dims), \
                 mock.patch.object(bd, "load_dimension_dict", return_value=({}, "ph")), \
                 mock.patch.object(bd, "build_breadth_dimension", return_value=_stats()) as bm, \
                 mock.patch.object(bd, "build_valuation_dimension", return_value=_stats()) as sa:
                bd.build_all_dimensions(None, start=D(2026, 1, 1), end=DAY, write=False,
                         markets=("CN",))
                return (list(bm.call_args_list), list(sa.call_args_list))

        before = run(base)
        # 第三维度注册后（此处用未知 method 模拟「尚未实现」也走同一分派路径），
        # 现有两维度的调用参数不得变化
        after = run(extra)
        self.assertEqual(before, after)

    def test_producers_share_one_signature(self):
        """新函数照抄签名即可接入；两个现有生产者必须同构。"""
        a = list(inspect.signature(bd.build_breadth_dimension).parameters)
        b = list(inspect.signature(bd.build_valuation_dimension).parameters)
        self.assertEqual(a, b)
        self.assertEqual(a, ["conn", "dimension_config", "params_hash", "market",
                             "start", "end", "write", "dim_code"])

    def test_reps_not_bound_to_breadth_code(self):
        """聚合统计不依赖具体维度编码（源码钉子）。"""
        src = inspect.getsource(bd.build_all_dimensions)
        self.assertIn('for stats_for_dimension in market_stats["dims"].values()', src)
        self.assertNotIn('get(DIM,', src)


class TestNoImplicitInheritance(unittest.TestCase):
    def test_new_dimension_is_not_silently_disabled_for_cn(self):
        """不写 cn_state_disabled 的新维度应正常出档（旧默认值 True 会静默禁档）。"""
        rows, stat = _run_single_anchor(market="CN")
        self.assertIsNotNone(rows[0]["state"])
        self.assertEqual(stat["state_rows"], 1)
        self.assertNotIn("short_sample", rows[0]["flags"])

    def test_explicit_cn_constraint_still_works(self):
        """估值字典显式写 true 时，CN 仍恒不出档（现有行为不得被改坏）。"""
        rows, stat = _run_single_anchor({"cn_state_disabled": True}, market="CN")
        self.assertIsNone(rows[0]["state"])
        self.assertEqual(stat["state_rows"], 0)
        self.assertTrue(rows[0]["flags"]["short_sample"])

    def test_min_periods_is_dictionary_driven(self):
        low, _ = _run_single_anchor({"min_periods": 10}, market="HK", n_sample=100.0)
        high, _ = _run_single_anchor({"min_periods": 1000}, market="HK", n_sample=100.0)
        self.assertIsNotNone(low[0]["state"])
        self.assertIsNone(high[0]["state"])


class TestBreadthFlagsIsolated(unittest.TestCase):
    def test_flags_thresholds_are_parameters(self):
        """宽度专属 flags 已隔离为独立函数，阈值由参数传入而非模块常量。"""
        aligned = pd.DataFrame(
            {"BREADTH.NEW_HIGH_LOW_20": [10.0], "BREADTH.NEW_HIGH_LOW_60": [40.0],
             "BREADTH.PCT_ABOVE_MA60": [80.0], "BREADTH.PCT_ABOVE_MA200": [50.0],
             "BREADTH.EQ_VS_CAP_20D": [10.0], "BREADTH.AMT_CONC_TOP10": [10.0]},
            index=[DAY])
        strict = bd._breadth_flags(aligned, DAY, flag_gap=15.0, flag_extreme=20.0)
        loose = bd._breadth_flags(aligned, DAY, flag_gap=100.0, flag_extreme=0.0)
        self.assertEqual(len(strict), 4)
        self.assertEqual(loose, [])

    def test_breadth_flags_is_a_named_isolated_function(self):
        src = inspect.getsource(bd.build_breadth_dimension)
        self.assertIn("_breadth_flags(aligned, trade_date, flag_gap, flag_extreme)", src)
        doc = inspect.getdoc(bd._breadth_flags) or ""
        self.assertIn("宽度专属", doc)


if __name__ == "__main__":
    unittest.main()
