"""`DIM.TREND` 落地回归测试（不连库）。

锁住 §5.6 v1.19 模型（t 作 raw_value + expanding 分位作 score）的十一件事：
  ① score = `TREND.STRENGTH_T` 的对齐分位，不是固定 K 的线性刻度
  ② `TSMOM_12M` / `MA200_DEV` 变化不改 score（weight=0 的方向腿与校验项）
  ③ 冻结第 250 有效分位日之前不产行（PIT 安全）
  ④ 锚缺失按缺行处理，不写 score=NULL / 0 / 50
  ⑤ `state` / `dispersion` 恒 NULL；`confidence` 最高「中」
  ⑥ `blocks` 按 §2.3.3 标准形状 `{块名:{score,coverage}}`（三块）
  ⑦ `flags` 含 anchor_code / sigma_win / tsmom_variant / no_state / asof
  ⑧ bulk_upsert 契约与 model_version == params_hash
  ⑨ `build_all_dimensions()` 分派到新生产者，不触碰现有五维
  ⑩ 生产者 8 参数签名与其它维度一致
  ⑪ 单锚配置被破坏时整维不产行，不静默出分
  ⑫ L1：3 个 TREND.* 已登记、`_params_hash` 按族隔离、护栏与 σ=0 生效
"""
import datetime
import inspect
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_dimension_value as rd  # noqa: E402
import market_state_daily as msd  # noqa: E402

D = datetime.date
DAY = D(2026, 11, 4)
ANCHOR = "TREND.STRENGTH_T"
CODES = [ANCHOR, "TREND.TSMOM_12M", "TREND.MA200_DEV"]

SCOPES = {c: "CN+HK" for c in CODES}
MEMBERS = [
    {"code": ANCHOR,             "sign": 1, "weight": 1.0, "block": "强度锚"},
    {"code": "TREND.TSMOM_12M",  "sign": 1, "weight": 0.0, "block": "方向腿"},
    {"code": "TREND.MA200_DEV",  "sign": 1, "weight": 0.0, "block": "校验"},
]


def _freeze(day):
    return {m: {c: day for c in CODES} for m in ("CN", "HK")}


def _dim():
    return {
        "members": MEMBERS,
        "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
        "state_labels": ["极弱", "偏弱", "中性", "偏强", "极强"],
        "params": {
            "coverage_min": 0.6, "core_coverage_min": 1.0,
            "state_bands_calibrated": False, "no_state": True,
            "availability_tier": "trend_single_anchor",
            "anchor_code": ANCHOR, "sigma_win": 252, "tsmom_variant": "price",
            "index_code": {"CN": "SH.000001", "HK": "HK.800000"},
            "trend_asof_start": {"CN": "1993-01-01", "HK": "2013-08-20"},
            "member_valid_from": _freeze("2020-01-01"),
            "member_confident_from": _freeze("2022-01-01"),
        },
    }


def _pct(index=None, **overrides):
    data = {ANCHOR: [63.4], "TREND.TSMOM_12M": [40.0], "TREND.MA200_DEV": [55.0]}
    data.update({k: [v] for k, v in overrides.items()})
    return pd.DataFrame(data, index=pd.Index(index or [DAY], name="trade_date"))


class _Harness:
    """拦截 DB 边界，返回 `build_trend_dimension` 本应写入的行。"""

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
        return rd.build_trend_dimension(
            None, dim or _dim(), "hash123", market, D(1990, 1, 1), DAY, write)


class TestSingleAnchor(unittest.TestCase):
    def test_score_is_anchor_percentile(self):
        with _Harness(_pct()) as h:
            h.run()
        self.assertEqual(len(h.rows), 1)
        self.assertEqual(h.rows[0]["score"], 63.4)
        self.assertEqual(h.rows[0]["dimension_code"], rd.DIM_TREND)

    def test_checks_do_not_change_score(self):
        with _Harness(_pct(**{"TREND.TSMOM_12M": 95.0,
                             "TREND.MA200_DEV": 5.0})) as h:
            h.run()
        self.assertEqual(h.rows[0]["score"], 63.4)

    def test_hk_uses_same_anchor(self):
        with _Harness(_pct()) as h:
            h.run("HK")
        self.assertEqual(h.rows[0]["score"], 63.4)
        self.assertEqual(h.rows[0]["flags"]["trend_asof_start"], "2013-08-20")

    def test_before_frozen_start_no_rows(self):
        with _Harness(_pct(index=[D(2019, 6, 3)])) as h:
            st = h.run()
        self.assertEqual(st["rows"], 0)
        self.assertEqual(h.rows, [])

    def test_missing_anchor_is_no_data_not_zero(self):
        with _Harness(_pct(**{ANCHOR: np.nan})) as h:
            st = h.run()
        self.assertEqual(st["rows"], 0)
        self.assertEqual(h.rows, [])

    def test_broken_anchor_config_skips_dimension(self):
        dim = _dim()
        dim["params"]["anchor_code"] = "TREND.NOT_REGISTERED"
        with _Harness(_pct()) as h:
            st = h.run(dim=dim)
        self.assertEqual(st["rows"], 0)
        self.assertEqual(h.rows, [])


class TestStateAndFlags(unittest.TestCase):
    def test_state_dispersion_null_and_confidence_capped(self):
        with _Harness(_pct()) as h:
            h.run()
        row = h.rows[0]
        self.assertIsNone(row["state"])
        self.assertIsNone(row["dispersion"])
        self.assertEqual(row["confidence"], "中")
        self.assertNotIn("short_sample", row["flags"])

    def test_short_sample_flags_before_confident_date(self):
        with _Harness(_pct(index=[D(2021, 6, 3)])) as h:
            h.run()
        self.assertEqual(h.rows[0]["confidence"], "低")
        self.assertTrue(h.rows[0]["flags"]["short_sample"])

    def test_blocks_use_standard_shape(self):
        with _Harness(_pct()) as h:
            h.run()
        blocks = h.rows[0]["blocks"]
        self.assertEqual(set(blocks), {"强度锚", "方向腿", "校验"})
        for b in blocks.values():
            self.assertIn("score", b)
            self.assertIn("coverage", b)
        self.assertEqual(blocks["强度锚"]["score"], 63.4)
        self.assertEqual(blocks["强度锚"]["coverage"], 1.0)

    def test_flags_carry_model_contract(self):
        with _Harness(_pct()) as h:
            h.run()
        f = h.rows[0]["flags"]
        self.assertEqual(f["anchor_code"], ANCHOR)
        self.assertEqual(f["anchor_window"], "expanding")
        self.assertEqual(f["sigma_win"], 252)
        self.assertEqual(f["tsmom_variant"], "price")
        self.assertTrue(f["no_state"])
        self.assertEqual(f["trend_asof_start"], "1993-01-01")

    def test_low_coverage_flag(self):
        with _Harness(_pct(**{"TREND.MA200_DEV": np.nan,
                             "TREND.TSMOM_12M": np.nan})) as h:
            h.run()
        self.assertTrue(h.rows[0]["flags"]["low_coverage"])
        self.assertEqual(h.rows[0]["coverage"], round(1 / 3, 4))

    def test_sign_minus_one_flips_once(self):
        dim = _dim()
        dim["members"][0]["sign"] = -1
        with _Harness(_pct()) as h:
            h.run(dim=dim)
        self.assertEqual(h.rows[0]["score"], 36.6)


class TestContractAndDispatch(unittest.TestCase):
    def test_bulk_upsert_contract(self):
        with _Harness(_pct()) as h:
            h.run()
        self.assertEqual(h.kw["conflict_cols"],
                         ["dimension_code", "market", "trade_date"])
        self.assertFalse(h.kw["skip_null_updates"])
        self.assertTrue(h.kw["only_if_changed"])
        self.assertEqual(h.rows[0]["model_version"], "hash123")

    def test_dispatch_includes_trend_without_touching_existing_dims(self):
        with mock.patch.object(rd, "_active_dimensions",
                               return_value=[rd.DIM_TREND]), \
             mock.patch.object(rd, "load_dimension_dict", return_value=({}, "ph")), \
             mock.patch.object(rd, "build_breadth_dimension") as bm, \
             mock.patch.object(rd, "build_valuation_dimension") as vm, \
             mock.patch.object(rd, "build_sentiment_dimension") as sm, \
             mock.patch.object(rd, "build_flow_dimension") as fm, \
             mock.patch.object(rd, "build_stress_dimension") as stm, \
             mock.patch.object(rd, "build_trend_dimension",
                               return_value={"rows": 7, "dates": 7,
                                             "state_rows": 0}) as tm:
            st = rd.build_all_dimensions(None, start=D(2020, 1, 1), end=DAY,
                                         write=False, markets=("CN",))
        self.assertEqual(tm.call_count, 1)
        for producer in (bm, vm, sm, fm, stm):
            producer.assert_not_called()
        self.assertEqual(st["CN"]["rows"], 7)
        self.assertEqual(st["CN"]["dims"][rd.DIM_TREND]["rows"], 7)

    def test_producers_share_one_signature(self):
        a = list(inspect.signature(rd.build_flow_dimension).parameters)
        b = list(inspect.signature(rd.build_trend_dimension).parameters)
        self.assertEqual(a, b)
        self.assertEqual(a, ["conn", "dimension_config", "params_hash", "market",
                             "start", "end", "write", "dim_code"])


class TestL1Atoms(unittest.TestCase):
    def test_three_codes_registered_and_version_isolated(self):
        registered = {m["code"] for m in msd.INDICATORS}
        self.assertTrue(msd.TREND_CALIB_CODES <= registered)
        plain = {"code": "VAL.PE_TTM_MEDIAN", "unit": "x",
                 "direction": "high_risk", "formula": "f"}
        original = msd.TREND_CALIB_VERSION
        try:
            h1 = msd._params_hash(plain, False)
            msd.TREND_CALIB_VERSION = "changed"
            h2 = msd._params_hash(plain, False)
            self.assertEqual(h1, h2, "趋势族版本不得波及其余指标指纹")
        finally:
            msd.TREND_CALIB_VERSION = original

    def test_asof_guard_and_sigma_zero(self):
        """构造 300 个交易日的**恒定**价格：σ=0 → STRENGTH_T 全 NULL，不填 0。"""
        dates = [D(2020, 1, 1) + datetime.timedelta(days=i) for i in range(300)]
        df = pd.DataFrame({"trade_date": dates, "last_price": [100.0] * 300})
        with mock.patch.object(msd, "_q", return_value=df):
            out = msd._trend_atoms(None, "CN", dates[-1])
        self.assertEqual(set(out), set(CODES))
        self.assertTrue(out[ANCHOR].isna().all())
        self.assertTrue(out["TREND.TSMOM_12M"].notna().any())

    def test_guard_cn_before_1993(self):
        dates = [D(1991, 1, 1) + datetime.timedelta(days=i) for i in range(900)]
        prices = [100.0 * (1 + 0.0003 * i) for i in range(900)]
        df = pd.DataFrame({"trade_date": dates, "last_price": prices})
        with mock.patch.object(msd, "_q", return_value=df):
            out = msd._trend_atoms(None, "CN", dates[-1])
        early = out[ANCHOR].loc[out[ANCHOR].index < D(1993, 1, 1)]
        self.assertTrue(early.isna().all(), "护栏前 raw_value 必须置 NULL")
        self.assertTrue(out[ANCHOR].loc[out[ANCHOR].index >= D(1993, 1, 1)].notna().any())


if __name__ == "__main__":
    unittest.main()
