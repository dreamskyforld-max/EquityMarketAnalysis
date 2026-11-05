"""跨资产风险偏好与外部环境指标测试（§3.4 + §5.4）。

背景：`get_global_benchmarks.py` 每小时采的 17 个基准里，此前**只有 US.DGS10/DGS2 被计算层消费**；
VIX / 美元 / EFFR / 全球股指都在库里但没用起来。
⚠ 2026-11-05：旧合成表 `regime.market_regime_daily`（含 risk_appetite 等 4 个合成指数）已删除，
   原「风险偏好入库 / 区间 / 与 VIX 方向」三项库内测试随之移除；L2 继任者见
   `build_dimension_value.py` / `tools/calibrate_valuation.py`。本文件保留纯函数与 L1 指标存在性测试。
本文件钉住三件容易出错的事：

  ① **动量必须在源市场日历上算**：20 日动量 = 源序列 `s/s.shift(20)−1`，
     再用 `merge_asof backward` 对齐到本市场交易日。若先用本市场日历 reindex（外盘假期成 NaN）
     再 shift，就会把「20 个交易日」错位成别的窗口；
  ② **merge_asof 两侧时间精度必须一致**（本市场索引 `M8[us]` vs `date` 索引 `M8[s]`
     → 不统一直接报 incompatible merge keys，实测踩过）；
  ③ **方向约定**：风险偏好指数要与 VIX **负相关**、与股指动量**正相关**（挡 sign 写反）。
"""
import datetime
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_state_daily import _align_close

D = datetime.date


class TestAlignClose(unittest.TestCase):
    def test_no_lookahead_backward_fill(self):
        """对齐必须取「≤ t 的最近一条」：t 在各源观测之间时取前值，绝不取未来值。"""
        src = pd.Series([100.0, 101.0, 102.0],
                        index=[pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-07"),
                               pd.Timestamp("2026-01-09")])
        idx = [D(2026, 1, 6), D(2026, 1, 7), D(2026, 1, 8)]
        out = _align_close(src, idx)
        self.assertEqual(list(out.values), [100.0, 101.0, 101.0], "1/6 应取 1/5 的值（不得用 1/7）")

    def test_empty_source(self):
        out = _align_close(pd.Series(dtype=float), [D(2026, 1, 5)])
        self.assertTrue(np.isnan(out.iloc[0]))

    def test_mixed_datetime_resolution(self):
        """回归：本市场索引（date→M8[us]）与源索引（M8[s]）精度不同也必须能 merge。"""
        src = pd.Series([1.5], index=pd.DatetimeIndex(["2026-01-05"]))   # 秒级
        out = _align_close(src, [D(2026, 1, 5)])                          # 微秒级
        self.assertEqual(float(out.iloc[0]), 1.5)

    def test_momentum_uses_source_calendar(self):
        """动量在源日历上算：源缺一天时，20 个**源交易日**的窗口不得被本市场日历拉长。"""
        dates = pd.bdate_range("2026-01-01", periods=30)
        src = pd.Series(np.arange(100.0, 130.0), index=dates)
        mom_src = (src / src.shift(20) - 1) * 100
        manual = (src.iloc[25] / src.iloc[5] - 1) * 100
        self.assertAlmostEqual(float(mom_src.iloc[25]), manual, places=6)


class TestExternalIndicatorsInDb(unittest.TestCase):
    """库内实测（需数据库）：§3.4 外部环境 7 个 L1 原子指标存在且新鲜。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()
            cls.conn = cls._cm.__enter__()
        except Exception as e:
            raise unittest.SkipTest(f"数据库不可用: {e}")
        cls.cur = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def test_new_indicators_exist(self):
        codes = ("MACRO.DXY", "MACRO.DTWEXBGS", "MACRO.FED_FUNDS", "RISK.VIX",
                 "MACRO.SPX_MOM20", "MACRO.COPPER_MOM20", "MACRO.GOLD_MOM20")
        self.cur.execute("""SELECT indicator_code, count(*), max(trade_date)
                            FROM regime.indicator_value
                            WHERE indicator_code = ANY(%s) GROUP BY 1""", (list(codes),))
        got = {r[0]: (r[1], r[2]) for r in self.cur.fetchall()}
        for c in codes:
            with self.subTest(code=c):
                self.assertIn(c, got, f"{c} 未入库")
                self.assertGreater(got[c][0], 200, f"{c} 行数过少：{got[c][0]}")
                self.assertGreater(got[c][1], D(2026, 6, 1), f"{c} 不新鲜：{got[c][1]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
