"""L2 宽度维度 `DIM.BREADTH` 与 §5.1 派生量回归测试（不连库）。

锁住：
  ① A/D Line / Summation Index 的 cumsum 语义与 SI_0=0 固定；
  ② 双高点 A/D 背离能触发顶/底背离、幅度为 z 单位、持续天数递增；
  ③ 族标定与构念效度判据常量（防回退）；
  ④ `build_market` 五件套 + blocks/contrib/flags/confidence + 出档门禁。

背景见 doc/market_profile.md §5.1 / §5.7 L2-c / §8.1.1。
"""
import datetime
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import regime_dimensions as rd
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import calibrate_breadth as cb

D = datetime.date


class TestDerived(unittest.TestCase):
    def test_ad_line_is_cumsum(self):
        s = pd.Series([10.0, -20.0, 5.0])
        pd.testing.assert_series_equal(rd.ad_line(s), pd.Series([10.0, -10.0, -5.0]))

    def test_summation_index_starts_at_zero(self):
        s = pd.Series([1.0, 2.0, -1.0])
        pd.testing.assert_series_equal(rd.summation_index(s), pd.Series([1.0, 3.0, 2.0]))
        # SI_0 = 0 固定：不存在「起点任选」的自由度
        self.assertEqual(float(rd.summation_index(pd.Series([], dtype=float)).sum()), 0.0)

    def _two_peaks(self, up=True):
        """构造两个依次抬高的 N 日高点（间隔 >= gap），A/D 依次降低。"""
        close_vals = list(range(1, 11)) + [5] * 5 + [6, 7, 8, 9, 10, 11]
        idx = [D(2020, 1, 1) + datetime.timedelta(days=i) for i in range(len(close_vals))]
        close = pd.Series([float(v) for v in close_vals], index=idx)
        ad = pd.Series(np.linspace(100.0, 50.0, len(idx)), index=idx)
        return close, ad

    def test_top_divergence_fires(self):
        close, ad = self._two_peaks()
        out = rd.ad_divergence(close, ad, n=10, gap=3, sigma_win=20, detrend=False)
        self.assertIn("顶背离", set(out["direction"]))
        hit = out[out["direction"] == "顶背离"]
        self.assertGreaterEqual(int(hit["days"].min()), 1)
        self.assertEqual(list(out.columns), ["direction", "amplitude_z", "days"])
        self.assertNotIn("距高点百分比", str(out.columns))

    def test_bottom_divergence_fires(self):
        close, ad = self._two_peaks()
        # 镜像：两个依次降低的低点 + A/D 依次抬高
        close = 22.0 - close
        ad = 150.0 - ad
        out = rd.ad_divergence(close, ad, n=10, gap=3, sigma_win=20, detrend=False)
        self.assertIn("底背离", set(out["direction"]))

    def test_streak_and_reset(self):
        close, ad = self._two_peaks()
        extra = [11.0] * 8            # 触发后横盘，让 streak 累积到 max_streak 并复位
        idx = [close.index[-1] + datetime.timedelta(days=i + 1) for i in range(len(extra))]
        close = pd.concat([close, pd.Series(extra, index=idx)])
        ad = pd.concat([ad, pd.Series(np.linspace(48.0, 40.0, len(extra)), index=idx)])
        out = rd.ad_divergence(close, ad, n=10, gap=3, sigma_win=20, detrend=False, max_streak=2)
        self.assertIn("顶背离", set(out["direction"]))
        self.assertIn("无", set(out["direction"]))
        self.assertTrue((out["days"] >= 0).all())

    def test_detrend_default_uses_relative_position(self):
        """默认口径去趋势；输出契约不变，且相对位置比较不引入 NaN 方向。"""
        close, ad = self._two_peaks()
        out = rd.ad_divergence(close, ad, n=10, gap=3, sigma_win=20)
        self.assertEqual(list(out.columns), ["direction", "amplitude_z", "days"])
        self.assertTrue(set(out["direction"]) <= {"无", "顶背离", "底背离"})
        self.assertTrue((out["days"] >= 0).all())
        self.assertTrue(out["direction"].notna().all())

    def test_no_lookahead(self):
        """前缀判定不受后续观测影响。"""
        close, ad = self._two_peaks()
        a = rd.ad_divergence(close.iloc[:15], ad.iloc[:15], n=10, gap=3, sigma_win=20, detrend=False)
        b = rd.ad_divergence(close, ad, n=10, gap=3, sigma_win=20, detrend=False).iloc[:15]
        # 前缀与全序列前 15 行的 amplitude_z 全为空时，dtype 会被 pandas 推断成
        # object / float64 两种；数值语义一致即可，不比较 dtype。
        pd.testing.assert_frame_equal(a.reset_index(drop=True),
                                      b.reset_index(drop=True), check_dtype=False)


class TestCalibrationContract(unittest.TestCase):
    def test_thresholds_pinned(self):
        self.assertEqual(cb.MIN_INTRA_CORR, 0.50)
        self.assertEqual(cb.MIN_PC1, 0.60)
        self.assertEqual(cb.MIN_DECADE_CORR, 0.30)
        self.assertEqual(cb.MIN_CONSTRUCT_CORR, 0.40)
        self.assertEqual(cb.CALIB_START, D(1996, 1, 1))
        self.assertEqual(cb.CALIB_END, D(2025, 12, 31))

    def test_four_candidate_blocks_cover_11_members(self):
        self.assertEqual(len(cb.CANDIDATES), 4)
        all_members = [c for v in cb.CANDIDATES.values() for c in v]
        self.assertEqual(len(all_members), 11)
        self.assertEqual(len(set(all_members)), 11)
        self.assertEqual(len(cb.STRUCT_CODES), 4)
        self.assertEqual(len(cb.CORE_REPS), 3)

    def test_build_dict_row_keeps_flags_only_members(self):
        """flags_only 成员不进 score，但必须留在 members[] 里计入 coverage。"""
        meta = {c: {"direction": "high_good", "market_scope": "CN+HK"}
                for c in ("A", "B", "C")}
        results = {
            "CN": {
                "state_bands": [20, 40, 60, 80],
                "effective_blocks": [
                    {"block": "族1", "members": ["A", "B"], "representative": "A"}],
                "members_meta": meta,
            },
            "HK": {
                "state_bands": [20, 40, 60, 80],
                "effective_blocks": [
                    {"block": "族1", "members": ["A", "B"], "representative": "A"}],
                "members_meta": meta,
            },
        }
        row = cb.build_dict_row(results, {"C"})
        by = {m["code"]: m for m in row["members"]}
        self.assertEqual(set(by), {"A", "B", "C"})
        self.assertTrue(by["A"]["representative"])
        self.assertFalse(by["B"]["representative"])
        self.assertTrue(by["C"]["flags_only"])
        self.assertEqual(by["C"]["weight"], 0.0)
        self.assertEqual(by["C"]["block"], "诊断")
        self.assertEqual(set(row["state_bands"]), {"CN", "HK"})
        self.assertEqual(len(row["params_hash"]), 16)
        self.assertIn("coverage 分母", row["remark"])


DIM_FIX = {
    "members": [
        dict(code="BREADTH.ADV_RATIO", sign=1, block="涨跌家数",
             representative=True, flags_only=False, weight=0.5),
        dict(code="BREADTH.ADV_MINUS_DEC", sign=1, block="涨跌家数",
             representative=False, flags_only=False, weight=0.0),
        dict(code="BREADTH.NEW_HIGH_LOW_20", sign=1, block="新高新低",
             representative=True, flags_only=False, weight=0.5),
        dict(code="BREADTH.NEW_HIGH_LOW_60", sign=1, block="新高新低",
             representative=False, flags_only=False, weight=0.0),
        dict(code="BREADTH.EQ_VS_CAP_20D", sign=1, block="诊断",
             representative=False, flags_only=True, weight=0.0),
    ],
    "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
    "state_labels": ["极窄", "偏窄", "中性", "偏广", "极广"],
    "params": {"coverage_min": 0.6, "dispersion_max": 30.0, "weight_cap": 0.25},
}


class _FakeConn:
    pass


def _fake_read(scopes: pd.DataFrame, pct_long: pd.DataFrame):
    def f(sql, conn, params=None):
        return scopes.copy() if "indicator_dict" in sql else pct_long.copy()
    return f


class TestBuildMarket(unittest.TestCase):
    def _run(self, pct_long):
        scopes = pd.DataFrame({
            "indicator_code": [m["code"] for m in DIM_FIX["members"]],
            "market_scope": ["CN+HK"] * 5,
        })
        with mock.patch.object(rd, "_read", side_effect=_fake_read(scopes, pct_long)), \
             mock.patch.object(rd, "bulk_upsert") as up:
            st = rd.build_market(_FakeConn(), DIM_FIX, "hash16", "CN",
                                 D(2020, 1, 1), D(2020, 1, 5), True)
            rows = up.call_args.args[2]
        return st, rows

    @staticmethod
    def _pct(values_by_day):
        recs = []
        for d, vals in values_by_day.items():
            for i, m in enumerate(DIM_FIX["members"]):
                v = vals[i]
                if v is not None:
                    recs.append({"trade_date": d, "indicator_code": m["code"],
                                 "percentile": float(v)})
        return pd.DataFrame(recs)

    def test_five_piece_output_and_confidence(self):
        pct = self._pct({D(2020, 1, d): [50, 50, 50, 50, 50] for d in range(1, 6)})
        st, rows = self._run(pct)
        self.assertEqual(st["rows"], 5)
        self.assertEqual(st["state_rows"], 5)
        r = rows[0]
        for k in ("score", "state", "coverage", "dispersion", "contrib", "flags",
                  "blocks", "confidence", "is_final", "model_version"):
            self.assertIn(k, r)
        self.assertAlmostEqual(float(r["score"]), 50.0, places=2)
        self.assertEqual(r["state"], "中性")
        self.assertEqual(r["confidence"], "高")
        self.assertTrue(r["is_final"])
        self.assertEqual(r["model_version"], "hash16")
        self.assertEqual(set(r["contrib"]), {"pos", "neg"})
        self.assertIn("涨跌家数", r["blocks"])
        self.assertEqual(r["blocks"]["涨跌家数"]["representative"], "BREADTH.ADV_RATIO")

    def test_coverage_gate_blocks_state(self):
        """只剩 1/5 成员 → coverage=0.2<0.6 → 只出 score + 低置信，不出档。"""
        pct = self._pct({D(2020, 1, d): [50, None, None, None, None] for d in range(1, 6)})
        st, rows = self._run(pct)
        self.assertEqual(st["state_rows"], 0)
        self.assertTrue(all(r["state"] is None for r in rows))
        self.assertTrue(all(r["confidence"] == "低" for r in rows))
        self.assertTrue(all(r["score"] is not None for r in rows))

    def test_dispersion_gate_blocks_state(self):
        """成员分位严重背离 → dispersion_norm>30 → 不出档。"""
        pct = self._pct({D(2020, 1, d): [95, 5, 95, 5, 50] for d in range(1, 6)})
        st, rows = self._run(pct)
        self.assertEqual(st["state_rows"], 0)
        self.assertTrue(all(r["state"] is None for r in rows))

    def test_high_risk_member_is_flipped(self):
        """AMT_CONC_TOP10 为 high_risk：sign=-1 → 取 100−分位。"""
        dim = dict(DIM_FIX)
        dim["members"] = [dict(m) for m in DIM_FIX["members"]]
        dim["members"][2]["sign"] = -1
        pct = self._pct({D(2020, 1, d): [50, 50, 10, 50, 50] for d in range(1, 6)})
        scopes = pd.DataFrame({
            "indicator_code": [m["code"] for m in dim["members"]],
            "market_scope": ["CN+HK"] * 5})
        with mock.patch.object(rd, "_read", side_effect=_fake_read(scopes, pct)), \
             mock.patch.object(rd, "bulk_upsert") as up:
            rd.build_market(_FakeConn(), dim, "h", "CN", D(2020, 1, 1), D(2020, 1, 5), True)
            rows = up.call_args.args[2]
        # 代表 ADV_RATIO=50、NHL20 翻向后=90 → score=70
        self.assertAlmostEqual(float(rows[0]["score"]), 70.0, places=2)

    def test_flags_structural_divergence(self):
        """短窗新高低 < 长窗 − 15 → flags 命中，且不被平均分抹掉。"""
        pct = self._pct({D(2020, 1, d): [50, 50, 10, 80, 50] for d in range(1, 6)})
        _, rows = self._run(pct)
        self.assertTrue(any("短窗动能衰竭" in f for f in rows[0]["flags"]))


if __name__ == "__main__":
    unittest.main()
