"""跨资产风险偏好与外部环境指标测试（§3.4 + §5.4）。

背景：`get_global_benchmarks.py` 每小时采的 17 个基准里，此前**只有 US.DGS10/DGS2 被计算层消费**；
VIX / 美元 / EFFR / 全球股指都在库里但没用起来（`risk_appetite` 列更是**从未写过值**）。
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


class TestRiskAppetiteInDb(unittest.TestCase):
    """库内实测（需数据库）：新指标存在、风险偏好落在 0-100 且方向正确。"""

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

    def test_risk_appetite_written_both_markets(self):
        self.cur.execute("""SELECT market, count(*) FROM regime.market_regime_daily
                            WHERE risk_appetite IS NOT NULL GROUP BY 1""")
        got = dict(self.cur.fetchall())
        self.assertGreater(got.get("CN", 0), 3000, f"CN 风险偏好行数过少：{got}")
        self.assertGreater(got.get("HK", 0), 1000, f"HK 风险偏好行数过少：{got}")

    def test_risk_appetite_range(self):
        self.cur.execute("""SELECT min(risk_appetite), max(risk_appetite),
                                   avg(risk_appetite) FROM regime.market_regime_daily
                            WHERE risk_appetite IS NOT NULL""")
        lo, hi, avg = (float(x) for x in self.cur.fetchone())
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 100.0)
        self.assertTrue(30 < avg < 70, f"长期均值 {avg:.1f} 偏离中性过远，疑似成分方向写反")

    def test_direction_with_vix(self):
        """方向约定：风险偏好高 ⇒ VIX 低（负相关）；与股指动量正相关。"""
        self.cur.execute("""
            SELECT r.trade_date, r.risk_appetite, v.raw_value, s.raw_value
            FROM regime.market_regime_daily r
            JOIN regime.indicator_value v ON v.indicator_code='RISK.VIX'
                 AND v.market='CN' AND v.trade_date=r.trade_date
            JOIN regime.indicator_value s ON s.indicator_code='MACRO.SPX_MOM20'
                 AND s.market='CN' AND s.trade_date=r.trade_date
            WHERE r.market='CN' AND r.risk_appetite IS NOT NULL
            ORDER BY r.trade_date""")
        rows = self.cur.fetchall()
        self.assertGreater(len(rows), 1500, "可比样本过少")
        ra = pd.Series([float(x[1]) for x in rows])
        vix = pd.Series([float(x[2]) for x in rows])
        spx = pd.Series([float(x[3]) for x in rows])
        self.assertLess(ra.corr(vix), -0.3, "风险偏好应与 VIX 负相关（sign 可能写反）")
        self.assertGreater(ra.corr(spx), 0.5, "风险偏好应与标普动量正相关")


if __name__ == "__main__":
    unittest.main(verbosity=2)
