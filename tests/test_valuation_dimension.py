"""`DIM.VALUATION` 生产计算回归测试（不连库）。

锁住七件事，任何一条被改坏都说明估值维度回到了「按宽度族代表算」或
「把 CN 约束期当成可出档」的状态：
  ① `single_anchor`：score = 锚分位，校验项 weight=0 不进分数
  ② 极性翻转用 `100−p`（§2.4 规则 2），不是 `p * sign`
  ③ CN 口径约束：state 恒 NULL / confidence 低 / flags.short_sample
  ④ HK 按 state_bands[market] 出档，coverage 分母按 market_scope（CN 6 / HK 5）
  ⑤ 锚缺失即 no_data、不写行（护栏前整体不产出）
  ⑥ blocks / flags / contrib 形状固定，低频 PIT 偏差按市场标注
  ⑦ build_all_dimensions() 按 dimension_code 分派所有启用维度，宽度路径逐值不变、only_dims 可隔离
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

import build_dimension_value as rd

D = datetime.date
DAY = D(2026, 11, 4)
LABELS = ["极便宜", "偏便宜", "中性", "偏贵", "极贵"]

SCOPES = {
    "VAL.PE_TTM_MEDIAN": "CN+HK", "VAL.PB_MEDIAN": "CN+HK",
    "VAL.PCT_BELOW_PB_1": "CN+HK", "VAL.DIV_SPREAD": "CN+HK",
    "VAL.ERP": "CN+HK", "VAL.BUFFETT": "CN",
}


def _dim():
    members = [
        {"code": "VAL.PE_TTM_MEDIAN", "sign": 1, "weight": 1.0, "block": "anchor"},
        {"code": "VAL.PB_MEDIAN", "sign": 1, "weight": 0.0, "block": "check"},
        {"code": "VAL.PCT_BELOW_PB_1", "sign": -1, "weight": 0.0, "block": "check"},
        {"code": "VAL.DIV_SPREAD", "sign": -1, "weight": 0.0, "block": "check"},
        {"code": "VAL.ERP", "sign": -1, "weight": 0.0, "block": "check"},
        {"code": "VAL.BUFFETT", "sign": 1, "weight": 0.0, "block": "check"},
    ]
    return {
        "members": members,
        "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
        "state_labels": LABELS,
        "params": {"coverage_min": 0.6, "dispersion_max": 30,
                   "quality_sample": 750, "check_divergence": 20,
                   "cn_state_disabled": True,
                   "low_freq_members": ["VAL.DIV_SPREAD", "VAL.BUFFETT"]},
    }


def _pct(anchor=70.0):
    """一行分位：sign=-1 的三项翻转后才与锚接近（未翻转会让 dispersion 爆掉）。"""
    return pd.DataFrame(
        {"VAL.PE_TTM_MEDIAN": [anchor], "VAL.PB_MEDIAN": [72.0],
         "VAL.PCT_BELOW_PB_1": [30.0], "VAL.DIV_SPREAD": [25.0],
         "VAL.ERP": [28.0], "VAL.BUFFETT": [65.0]},
        index=pd.Index([DAY], name="trade_date"))


class _Harness:
    """拦截所有 DB 边界，返回 build_valuation_dimension 本应写入的行。"""

    def __init__(self, pct=None, n_sample=1000.0):
        self.pct = _pct() if pct is None else pct
        self.n_sample = n_sample
        self.rows = []
        self.kw = {}

    def __enter__(self):
        def _capture(conn, table, rows, **kw):
            self.rows = rows
            self.kw = kw
        self.patches = [
            mock.patch.object(rd, "_load_market_scopes", return_value=SCOPES),
            mock.patch.object(rd, "_load_percentiles", return_value=self.pct),
            mock.patch.object(rd, "_anchor_sample_count",
                              return_value=pd.Series([self.n_sample], index=[DAY])),
            mock.patch.object(rd, "bulk_upsert", side_effect=_capture),
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()

    def run(self, market="CN"):
        return rd.build_valuation_dimension(None, _dim(), "hash123", market,
                                      D(2000, 1, 1), DAY, True)


class TestSingleAnchor(unittest.TestCase):
    def test_score_is_anchor_only_and_sign_flips(self):
        """锚 70；sign=-1 的 30/25/28 必须翻成 70/75/72，dispersion 才不会爆。"""
        with _Harness() as h:
            h.run("CN")
        row = h.rows[0]
        self.assertEqual(row["score"], 70.0)
        self.assertEqual(row["blocks"]["anchor"]["code"], "VAL.PE_TTM_MEDIAN")
        self.assertEqual(row["blocks"]["anchor"]["percentile"], 70.0)
        self.assertLess(row["dispersion"], 10.0)   # 未翻转时会是 20+
        self.assertEqual(row["coverage"], 1.0)

    def test_cn_constraint(self):
        with _Harness() as h:
            s = h.run("CN")
        row = h.rows[0]
        self.assertIsNone(row["state"])
        self.assertEqual(row["confidence"], "低")
        self.assertTrue(row["flags"]["short_sample"])
        self.assertEqual(s["state_rows"], 0)
        self.assertEqual(row["blocks"]["check"]["n"], 5)
        self.assertEqual(row["flags"]["low_freq_pit_dup"],
                         ["VAL.BUFFETT", "VAL.DIV_SPREAD"])

    def test_hk_uses_market_bands_and_scope(self):
        with _Harness() as h:
            s = h.run("HK")
        row = h.rows[0]
        self.assertEqual(row["state"], "偏贵")       # digitize(70,[20,40,60,80])=3
        self.assertEqual(row["confidence"], "高")
        self.assertEqual(row["coverage"], 1.0)      # HK 5/5
        self.assertEqual(row["blocks"]["check"]["n"], 4)
        # BUFFETT 是 CN only，不能出现在 HK 的低频偏差标注里
        self.assertEqual(row["flags"]["low_freq_pit_dup"], ["VAL.DIV_SPREAD"])
        self.assertNotIn("short_sample", row["flags"])
        self.assertEqual(s["state_rows"], 1)

    def test_anchor_missing_writes_no_row(self):
        pct = _pct(np.nan)
        with _Harness(pct) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_contrib_no_overlap_and_correct_sign(self):
        with _Harness() as h:
            h.run("HK")
        c = h.rows[0]["contrib"]
        pos, neg = {x["code"] for x in c["pos"]}, {x["code"] for x in c["neg"]}
        self.assertFalse(pos & neg)
        self.assertTrue(all(x["delta"] > 0 for x in c["pos"]))
        self.assertTrue(all(x["delta"] < 0 for x in c["neg"]))

    def test_short_sample_lowers_confidence(self):
        with _Harness(n_sample=100.0) as h:
            h.run("HK")
        self.assertEqual(h.rows[0]["confidence"], "低")

    def test_write_uses_dimension_pk_and_null_overwrite(self):
        with _Harness() as h:
            h.run("CN")
        self.assertEqual(h.kw["conflict_cols"],
                         ["dimension_code", "market", "trade_date"])
        # state=NULL 必须能覆盖旧档，否则解除约束后残留旧结论
        self.assertFalse(h.kw["skip_null_updates"])
        self.assertEqual(h.rows[0]["dimension_code"], rd.DIM_VALUATION)
        self.assertEqual(h.rows[0]["model_version"], "hash123")
        self.assertTrue(h.rows[0]["is_final"])


class TestMultiDimensionBuild(unittest.TestCase):
    def test_dispatch_by_dimension_code(self):
        dims = [rd.DIM_BREADTH, rd.DIM_VALUATION]
        with mock.patch.object(rd, "_active_dimensions", return_value=dims), \
             mock.patch.object(rd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(rd, "build_breadth_dimension",
                               return_value={"rows": 10, "dates": 10,
                                             "state_rows": 8, "reps": ["r"]}) as bm, \
             mock.patch.object(rd, "build_valuation_dimension",
                               return_value={"rows": 5, "dates": 5,
                                             "state_rows": 4}) as sa:
            st = rd.build_all_dimensions(None, start=D(2020, 1, 1), end=DAY,
                          write=False, markets=("CN",))
        self.assertEqual(bm.call_count, 1)
        self.assertEqual(sa.call_count, 1)
        self.assertEqual(st["CN"]["rows"], 15)
        self.assertEqual(st["CN"]["state_rows"], 12)
        self.assertEqual(st["CN"]["reps"], ["r"])
        self.assertIn("dims", st["CN"])

    def test_only_dims_isolates_valuation(self):
        dims = [rd.DIM_BREADTH, rd.DIM_VALUATION]
        with mock.patch.object(rd, "_active_dimensions", return_value=dims), \
             mock.patch.object(rd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(rd, "build_breadth_dimension") as bm, \
             mock.patch.object(rd, "build_valuation_dimension",
                               return_value={"rows": 5, "dates": 5, "state_rows": 4}):
            rd.build_all_dimensions(None, start=D(2020, 1, 1), end=DAY, write=False,
                     markets=("CN",), only_dims=[rd.DIM_VALUATION])
        bm.assert_not_called()   # 生产计算只跑估值时不触碰宽度既有数据

    def test_breadth_default_dimension_code(self):
        """宽度路径保持 DIM.BREADTH，签名新增的 dim_code 有默认值。"""
        import inspect
        sig = inspect.signature(rd.build_breadth_dimension)
        self.assertEqual(sig.parameters["dim_code"].default, rd.DIM_BREADTH)


if __name__ == "__main__":
    unittest.main()
