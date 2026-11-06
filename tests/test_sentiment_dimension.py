"""`DIM.SENTIMENT` 生产计算回归测试（不连库）。

锁住方案 A（2026-11-06）的九件事：
  ① 固定核心 3 成员 → 2 块两级等权；`IPO_BREAK_RATE_90D` 的 sign=-1 取 100−p
  ② 诊断成员（基金 2 / 评级 / 港股卖空）不进 score
  ③ `core_coverage < 1.0` 不产行（不用低覆盖分数凑历史）
  ④ `member_valid_from` 冻结日期之前不产行（PIT 安全，不依赖 L1 extra.n_sample）
  ⑤ `state_bands_calibrated=false` → state 恒 NULL；confidence 按冻结 750 日分档
  ⑥ HK 单成分代理分：恒低置信、恒不出档、禁止横比的标记齐全
  ⑦ `coverage` 仍按全体 eligible 计；`flags.core_coverage` / `dimension_completeness` / 限制标注齐全
  ⑧ `bulk_upsert` 契约与 `model_version == params_hash`
  ⑨ `build_all_dimensions()` 按 dimension_code 分派到新生产者，且不触碰现有两维
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
LABELS = ["极低迷", "偏低迷", "中性", "偏亢奋", "极亢奋"]

SCOPES = {
    "FLOW.TURNOVER_RATIO": "CN+HK",
    "SENT.IPO_COUNT_30D": "CN",
    "SENT.IPO_BREAK_RATE_90D": "CN",
    "SENT.FUND_ISSUANCE_60D": "CN",
    "SENT.FUND_MAX_60D": "CN",
    "FORECAST.RATING_BULL_PCT": "CN",
    "FLOW.SHORT_SELLING_RATIO": "HK",
}

MEMBERS = [
    {"code": "FLOW.TURNOVER_RATIO", "sign": 1, "weight": 0.50, "block": "交易行为"},
    {"code": "SENT.IPO_COUNT_30D", "sign": 1, "weight": 0.25, "block": "一级供给"},
    {"code": "SENT.IPO_BREAK_RATE_90D", "sign": -1, "weight": 0.25, "block": "一级供给"},
    {"code": "SENT.FUND_ISSUANCE_60D", "sign": 1, "weight": 0.0, "block": "诊断", "flags_only": True},
    {"code": "SENT.FUND_MAX_60D", "sign": 1, "weight": 0.0, "block": "诊断", "flags_only": True},
    {"code": "FORECAST.RATING_BULL_PCT", "sign": 1, "weight": 0.0, "block": "诊断", "flags_only": True},
    {"code": "FLOW.SHORT_SELLING_RATIO", "sign": -1, "weight": 0.0, "block": "诊断", "flags_only": True},
]


def _dim():
    return {
        "members": MEMBERS,
        "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
        "state_labels": LABELS,
        "params": {
            "coverage_min": 0.6, "dispersion_core_max": 30.0,
            "core_coverage_min": 1.0, "flag_gap": 15.0,
            "state_bands_calibrated": False,
            "hk_proxy_member": "FLOW.TURNOVER_RATIO",
            "availability_tier_core": "core3_fixed",
            "availability_tier_hk": "hk_single_proxy",
            "low_common_factor_pc1": 0.298,
            "member_valid_from": {
                "CN": {"FLOW.TURNOVER_RATIO": "2019-01-10",
                       "SENT.IPO_COUNT_30D": "2011-01-24",
                       "SENT.IPO_BREAK_RATE_90D": "2011-01-24"},
                "HK": {"FLOW.TURNOVER_RATIO": "2001-01-18"}},
            "member_confident_from": {
                "CN": {"FLOW.TURNOVER_RATIO": "2021-01-29",
                       "SENT.IPO_COUNT_30D": "2013-02-19",
                       "SENT.IPO_BREAK_RATE_90D": "2014-01-29"},
                "HK": {"FLOW.TURNOVER_RATIO": "2003-02-05"}},
        },
    }


def _pct_cn(index=None, **overrides):
    data = {
        "FLOW.TURNOVER_RATIO": [80.0],
        "SENT.IPO_COUNT_30D": [60.0],
        "SENT.IPO_BREAK_RATE_90D": [60.0],     # sign=-1 → 40.0
        "SENT.FUND_ISSUANCE_60D": [90.0],
        "SENT.FUND_MAX_60D": [85.0],
        "FORECAST.RATING_BULL_PCT": [70.0],
    }
    data.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(data, index=pd.Index(index or [DAY], name="trade_date"))


def _pct_hk(index=None, **overrides):
    data = {"FLOW.TURNOVER_RATIO": [70.0],
            "FLOW.SHORT_SELLING_RATIO": [30.0]}   # sign=-1 → 70.0（诊断，不进 score）
    data.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(data, index=pd.Index(index or [DAY], name="trade_date"))


class _Harness:
    """拦截 DB 边界，返回 `build_sentiment_dimension` 本应写入的行。"""

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

    def run(self, market="CN", write=True):
        return rd.build_sentiment_dimension(
            None, _dim(), "hash123", market, D(1990, 1, 1), DAY, write)


class TestFixedCore(unittest.TestCase):
    def test_two_level_block_equal_and_sign_flip(self):
        with _Harness(_pct_cn()) as h:
            h.run("CN")
        row = h.rows[0]
        # 换手 80；一级供给 (60 + (100-60))/2 = 50 → (80+50)/2 = 65
        self.assertEqual(row["score"], 65.0)
        self.assertEqual(row["blocks"]["交易行为"]["score"], 80.0)
        self.assertEqual(row["blocks"]["一级供给"]["score"], 50.0)
        self.assertEqual(row["dispersion"], 20.0)     # std([80,60,40], ddof=1)
        self.assertEqual(row["coverage"], 1.0)

    def test_diagnostic_members_do_not_enter_score(self):
        base = _pct_cn()
        with _Harness(base) as h:
            h.run("CN")
        changed = _pct_cn(**{"SENT.FUND_ISSUANCE_60D": 1.0,
                            "SENT.FUND_MAX_60D": 2.0,
                            "FORECAST.RATING_BULL_PCT": 3.0})
        with _Harness(changed) as h2:
            h2.run("CN")
        self.assertEqual(h.rows[0]["score"], h2.rows[0]["score"])
        self.assertEqual(h.rows[0]["dispersion"], h2.rows[0]["dispersion"])

    def test_core_member_missing_produces_no_row(self):
        with _Harness(_pct_cn(**{"SENT.IPO_COUNT_30D": np.nan})) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_frozen_valid_from_blocks_early_rows(self):
        early = D(2018, 6, 1)     # 换手尚未达到第 250 个有效观测（2019-01-10）
        with _Harness(_pct_cn(index=[early])) as h:
            s = h.run("CN")
        self.assertEqual(h.rows, [])
        self.assertEqual(s["rows"], 0)

    def test_state_always_null_and_flags_complete(self):
        with _Harness(_pct_cn()) as h:
            s = h.run("CN")
        row = h.rows[0]
        self.assertIsNone(row["state"])
        self.assertEqual(s["state_rows"], 0)
        self.assertEqual(row["confidence"], "高")
        f = row["flags"]
        self.assertEqual(f["availability_tier"], "core3_fixed")
        self.assertEqual(f["core_coverage"], 1.0)
        self.assertEqual(f["dimension_completeness"], round(6 / 7, 4))
        self.assertEqual(f["block_coverage"], 1.0)
        self.assertTrue(f["low_common_factor"])
        self.assertIn("structural", f)             # |80-50| = 30 > flag_gap 15
        self.assertEqual(f["member_valid_from"]["FLOW.TURNOVER_RATIO"], "2019-01-10")

    def test_short_sample_lowers_confidence_only(self):
        short_day = D(2020, 6, 1)     # 2019-01-10 之后 / 2021-01-29 之前
        with _Harness(_pct_cn(index=[short_day])) as h:
            h.run("CN")
        row = h.rows[0]
        self.assertEqual(row["score"], 65.0)
        self.assertEqual(row["confidence"], "低")
        self.assertIsNone(row["state"])
        self.assertTrue(row["flags"]["short_sample"])


class TestHkProxy(unittest.TestCase):
    def test_single_component_proxy(self):
        with _Harness(_pct_hk()) as h:
            s = h.run("HK")
        row = h.rows[0]
        self.assertEqual(row["score"], 70.0)       # 只取换手，卖空是诊断成员
        self.assertIsNone(row["state"])
        self.assertEqual(row["confidence"], "低")
        self.assertIsNone(row["dispersion"])
        self.assertEqual(s["state_rows"], 0)
        f = row["flags"]
        self.assertEqual(f["availability_tier"], "hk_single_proxy")
        self.assertTrue(f["single_block_proxy"])
        self.assertEqual(f["block_coverage"], 0.5)
        self.assertEqual(f["missing_block"], ["一级供给"])
        self.assertTrue(f["block_incomplete"])
        self.assertEqual(f["dimension_completeness"], round(2 / 7, 4))
        self.assertTrue(row["blocks"]["交易行为"]["proxy"])

    def test_hk_proxy_requires_frozen_valid_from(self):
        early = D(2000, 6, 1)     # HK 换手 250 日达标为 2001-01-18
        with _Harness(_pct_hk(index=[early])) as h:
            h.run("HK")
        self.assertEqual(h.rows, [])


class TestContractAndDispatch(unittest.TestCase):
    def test_bulk_upsert_contract(self):
        with _Harness(_pct_cn()) as h:
            h.run("CN")
        self.assertEqual(h.kw["conflict_cols"],
                         ["dimension_code", "market", "trade_date"])
        self.assertFalse(h.kw["skip_null_updates"])
        self.assertTrue(h.kw["only_if_changed"])
        self.assertEqual(h.rows[0]["model_version"], "hash123")
        self.assertEqual(h.rows[0]["dimension_code"], rd.DIM_SENTIMENT)

    def test_dispatch_includes_sentiment_without_touching_existing_dims(self):
        with mock.patch.object(rd, "_active_dimensions",
                               return_value=[rd.DIM_SENTIMENT]), \
             mock.patch.object(rd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(rd, "build_breadth_dimension") as bm, \
             mock.patch.object(rd, "build_valuation_dimension") as vm, \
             mock.patch.object(rd, "build_sentiment_dimension",
                               return_value={"rows": 7, "dates": 7,
                                             "state_rows": 0}) as sm:
            st = rd.build_all_dimensions(None, start=D(2020, 1, 1), end=DAY,
                                         write=False, markets=("CN",))
        self.assertEqual(sm.call_count, 1)
        bm.assert_not_called()
        vm.assert_not_called()
        self.assertEqual(st["CN"]["rows"], 7)
        self.assertEqual(st["CN"]["dims"][rd.DIM_SENTIMENT]["rows"], 7)

    def test_producers_share_one_signature(self):
        import inspect
        a = list(inspect.signature(rd.build_valuation_dimension).parameters)
        b = list(inspect.signature(rd.build_sentiment_dimension).parameters)
        self.assertEqual(a, b)
        self.assertEqual(a, ["conn", "dimension_config", "params_hash", "market",
                             "start", "end", "write", "dim_code"])


if __name__ == "__main__":
    unittest.main()
