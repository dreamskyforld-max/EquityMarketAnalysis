"""`DIM.FLOW` 生产计算回归测试（不连库）。

锁住按市场单锚方案（2026-11-06）的十一件事：
  ① CN / HK 各取自己的唯一核心成员，score = 对齐分位
  ② 6 个 flags_only 诊断成员不进 score
  ③ core_coverage < 1.0 或核心缺失 → 整行不产出
  ④ member_valid_from 冻结日期之前不产行（PIT 安全，不依赖 L1 extra.n_sample）
  ⑤ state 恒 NULL（state_bands_calibrated=false）；单锚 dispersion 恒 NULL
  ⑥ confidence 最高只到「中」；短样本写 flags.short_sample
  ⑦ flags 含 single_block_proxy / flow_single_anchor / block_coverage=1.0 /
     regime_shift_2016(HK) / northbound_discontinued(CN) / margin_ratio_source_discontinued
  ⑧ bulk_upsert 契约与 model_version == params_hash
  ⑨ build_all_dimensions() 按 dimension_code 分派到新生产者，不触碰现有三维
  ⑩ 生产者 8 参数签名与其它三维一致
  ⑪ 单锚配置被破坏（core 与 proxy 不符）时整维不产行，不静默出分
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
LABELS = ["极流出", "偏流出", "中性", "偏流入", "极流入"]

SCOPES = {
    "FLOW.MARGIN_NET_20D": "CN",
    "FLOW.BUYBACK_NET_20D": "HK",
    "FLOW.NORTHBOUND_NET": "CN",
    "FLOW.MARGIN_RATIO": "CN",
    "SENT.INSIDER_NET_SELL_60D": "CN",
    "SENT.UNLOCK_PAST_30D_VALUE": "CN",
    "FLOW.SOUTHBOUND_NET": "HK",
    "FLOW.SHORT_SELLING_RATIO": "HK",
}

MEMBERS = [
    {"code": "FLOW.MARGIN_NET_20D",  "sign": 1, "weight": 1.0, "block": "杠杆资金净流"},
    {"code": "FLOW.BUYBACK_NET_20D", "sign": 1, "weight": 1.0, "block": "公司净买入/股份供给"},
    {"code": "FLOW.NORTHBOUND_NET", "sign": 1, "weight": 0.0, "block": "诊断", "flags_only": True},
    {"code": "FLOW.MARGIN_RATIO", "sign": 1, "weight": 0.0, "block": "诊断", "flags_only": True},
    {"code": "SENT.INSIDER_NET_SELL_60D", "sign": -1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "SENT.UNLOCK_PAST_30D_VALUE", "sign": -1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "FLOW.SOUTHBOUND_NET", "sign": 1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "FLOW.SHORT_SELLING_RATIO", "sign": -1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
]


def _dim():
    return {
        "members": MEMBERS,
        "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
        "state_labels": LABELS,
        "params": {
            "coverage_min": 0.6, "core_coverage_min": 1.0,
            "dispersion_core_max": 30.0, "state_bands_calibrated": False,
            "availability_tier": "flow_single_anchor",
            "proxy_member_by_market": {"CN": "FLOW.MARGIN_NET_20D",
                                       "HK": "FLOW.BUYBACK_NET_20D"},
            "flow_asof_start": {"CN": "2019-01-02", "HK": "2010-01-04"},
            "regime_shift_2016": True,
            "northbound_discontinued": "2024-08-16",
            "margin_ratio_source_discontinued": True,
            "member_valid_from": {
                "CN": {"FLOW.MARGIN_NET_20D": "2020-01-10"},
                "HK": {"FLOW.BUYBACK_NET_20D": "2011-02-01"}},
            "member_confident_from": {
                "CN": {"FLOW.MARGIN_NET_20D": "2022-01-20"},
                "HK": {"FLOW.BUYBACK_NET_20D": "2013-02-14"}},
        },
    }


def _pct(market, index=None, **overrides):
    if market == "CN":
        data = {"FLOW.MARGIN_NET_20D": [80.0],
                "FLOW.NORTHBOUND_NET": [30.0],
                "FLOW.MARGIN_RATIO": [55.0],
                "SENT.INSIDER_NET_SELL_60D": [60.0],
                "SENT.UNLOCK_PAST_30D_VALUE": [45.0]}
    else:
        data = {"FLOW.BUYBACK_NET_20D": [70.0],
                "FLOW.SOUTHBOUND_NET": [40.0],
                "FLOW.SHORT_SELLING_RATIO": [30.0]}
    data.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(data, index=pd.Index(index or [DAY], name="trade_date"))


class _Harness:
    """拦截 DB 边界，返回 `build_flow_dimension` 本应写入的行。"""

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
        return rd.build_flow_dimension(
            None, dim or _dim(), "hash123", market, D(1990, 1, 1), DAY, write)


class TestSingleAnchor(unittest.TestCase):
    def test_cn_score_is_anchor_percentile(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        self.assertEqual(h.rows[0]["score"], 80.0)
        self.assertEqual(h.rows[0]["blocks"]["杠杆资金净流"]["score"], 80.0)
        self.assertIsNone(h.rows[0]["dispersion"])

    def test_hk_score_is_anchor_percentile(self):
        with _Harness(_pct("HK")) as h:
            h.run("HK")
        self.assertEqual(h.rows[0]["score"], 70.0)
        self.assertEqual(h.rows[0]["blocks"]["公司净买入/股份供给"]["score"], 70.0)

    def test_diagnostic_members_do_not_enter_score(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        changed = _pct("CN", **{"FLOW.NORTHBOUND_NET": 1.0,
                               "FLOW.MARGIN_RATIO": 2.0,
                               "SENT.INSIDER_NET_SELL_60D": 99.0,
                               "SENT.UNLOCK_PAST_30D_VALUE": 98.0})
        with _Harness(changed) as h2:
            h2.run("CN")
        self.assertEqual(h.rows[0]["score"], 80.0)
        self.assertEqual(h2.rows[0]["score"], 80.0)

    def test_core_missing_produces_no_row(self):
        with _Harness(_pct("CN", **{"FLOW.MARGIN_NET_20D": np.nan})) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_frozen_valid_from_blocks_early_rows(self):
        with _Harness(_pct("CN", index=[D(2019, 6, 3)])) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_misconfigured_proxy_produces_no_row(self):
        dim = _dim()
        dim["params"]["proxy_member_by_market"]["CN"] = "FLOW.BUYBACK_NET_20D"
        with _Harness(_pct("CN")) as h:
            s = h.run("CN", dim=dim)
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)


class TestStateAndFlags(unittest.TestCase):
    def test_state_always_null_and_cn_flags(self):
        with _Harness(_pct("CN")) as h:
            s = h.run("CN")
        row = h.rows[0]
        self.assertIsNone(row["state"])
        self.assertEqual(s["state_rows"], 0)
        self.assertEqual(row["confidence"], "中")     # 单锚不出「高」
        f = row["flags"]
        self.assertEqual(f["availability_tier"], "flow_single_anchor")
        self.assertTrue(f["single_block_proxy"])
        self.assertEqual(f["core_coverage"], 1.0)
        self.assertEqual(f["block_coverage"], 1.0)
        self.assertEqual(f["dimension_completeness"], round(5 / 8, 4))
        self.assertEqual(f["flow_asof_start"], "2019-01-02")
        self.assertEqual(f["northbound_discontinued"], "2024-08-16")
        self.assertTrue(f["margin_ratio_source_discontinued"])
        self.assertNotIn("regime_shift_2016", f)
        self.assertEqual(f["member_valid_from"]["FLOW.MARGIN_NET_20D"], "2020-01-10")

    def test_hk_regime_shift_flag(self):
        with _Harness(_pct("HK")) as h:
            h.run("HK")
        f = h.rows[0]["flags"]
        self.assertTrue(f["regime_shift_2016"])
        self.assertEqual(f["flow_asof_start"], "2010-01-04")
        self.assertNotIn("northbound_discontinued", f)

    def test_short_sample_lowers_confidence_only(self):
        day = D(2021, 6, 1)      # 2020-01-10 之后 / 2022-01-20 之前
        with _Harness(_pct("CN", index=[day])) as h:
            h.run("CN")
        row = h.rows[0]
        self.assertEqual(row["score"], 80.0)
        self.assertEqual(row["confidence"], "低")
        self.assertIsNone(row["state"])
        self.assertTrue(row["flags"]["short_sample"])


class TestContractAndDispatch(unittest.TestCase):
    def test_bulk_upsert_contract(self):
        with _Harness(_pct("CN")) as h:
            h.run("CN")
        self.assertEqual(h.kw["conflict_cols"],
                         ["dimension_code", "market", "trade_date"])
        self.assertFalse(h.kw["skip_null_updates"])
        self.assertTrue(h.kw["only_if_changed"])
        self.assertEqual(h.rows[0]["model_version"], "hash123")
        self.assertEqual(h.rows[0]["dimension_code"], rd.DIM_FLOW)

    def test_dispatch_includes_flow_without_touching_existing_dims(self):
        with mock.patch.object(rd, "_active_dimensions",
                               return_value=[rd.DIM_FLOW]), \
             mock.patch.object(rd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(rd, "build_breadth_dimension") as bm, \
             mock.patch.object(rd, "build_valuation_dimension") as vm, \
             mock.patch.object(rd, "build_sentiment_dimension") as sm, \
             mock.patch.object(rd, "build_flow_dimension",
                               return_value={"rows": 7, "dates": 7,
                                             "state_rows": 0}) as fm:
            st = rd.build_all_dimensions(None, start=D(2020, 1, 1), end=DAY,
                                         write=False, markets=("CN",))
        self.assertEqual(fm.call_count, 1)
        bm.assert_not_called()
        vm.assert_not_called()
        sm.assert_not_called()
        self.assertEqual(st["CN"]["rows"], 7)
        self.assertEqual(st["CN"]["dims"][rd.DIM_FLOW]["rows"], 7)

    def test_producers_share_one_signature(self):
        import inspect
        a = list(inspect.signature(rd.build_sentiment_dimension).parameters)
        b = list(inspect.signature(rd.build_flow_dimension).parameters)
        self.assertEqual(a, b)
        self.assertEqual(a, ["conn", "dimension_config", "params_hash", "market",
                             "start", "end", "write", "dim_code"])


if __name__ == "__main__":
    unittest.main()
