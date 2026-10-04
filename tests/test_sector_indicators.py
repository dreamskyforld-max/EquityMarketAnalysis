"""行业层指标（第④层）测试。

两部分：
  ① `_clip` 的索引兼容性回归（纯函数）——曾因某指标返回 **Timestamp 索引**，
     `date` 与 `Timestamp` 直接比较抛 TypeError，导致整块历史计算失败；
  ② `_sector_indicators` 的输出契约（需本地数据库）——索引必须是 datetime.date、
     取值域必须合理（占比 0-100）。
"""
import datetime
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_state_daily import _clip, _sector_indicators

D = datetime.date


class TestClipIndexCompat(unittest.TestCase):
    def test_date_index(self):
        s = pd.Series([1.0, 2.0, 3.0], index=[D(2026, 1, 1), D(2026, 6, 1), D(2027, 1, 1)])
        out = _clip(s, D(2026, 1, 1), D(2026, 12, 31))
        self.assertEqual(list(out.values), [1.0, 2.0])

    def test_timestamp_index_regression(self):
        """Timestamp 是 datetime 子类：必须按 date 归一化后再比较，不得抛 TypeError。"""
        s = pd.Series([1.0, 2.0, 3.0],
                      index=pd.DatetimeIndex(["2026-01-01", "2026-06-01", "2027-01-01"]))
        out = _clip(s, D(2026, 1, 1), D(2026, 12, 31))
        self.assertEqual(list(out.values), [1.0, 2.0])

    def test_empty_and_other_index(self):
        self.assertEqual(len(_clip(pd.Series(dtype=float), D(2026, 1, 1), D(2026, 2, 1))), 0)
        # 非日期索引（RangeIndex）不得抛错，返回空
        self.assertEqual(len(_clip(pd.Series([1.0, 2.0]), D(2026, 1, 1), D(2026, 2, 1))), 0)


class TestSectorIndicatorContract(unittest.TestCase):
    """输出契约：date 索引 + 6 个系列 + 占比在 [0,100]（数据库需可用）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()            # 必须保留 contextManager 引用，
            cls.conn = cls._cm.__enter__()  # 否则裸 __enter__ 的连接会被 GC 关掉
        except Exception as e:  # 无数据库环境直接跳过
            raise unittest.SkipTest(f"数据库不可用: {e}")
        cls.idx = [D(2026, 3, 2), D(2026, 6, 1), D(2026, 9, 30)]

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def setUp(self):
        self.out = _sector_indicators(self.conn, self.idx)

    def test_expected_series_present(self):
        self.assertEqual(
            set(self.out),
            {"SECTOR.UP_RATIO", "SECTOR.MOM_20D_MEDIAN", "SECTOR.RS_DISPERSION",
             "SECTOR.PE_TTM_MEDIAN", "SECTOR.PB_MEDIAN",
             "SECTOR.FLOW_NET", "SECTOR.FLOW_UP_RATIO", "SECTOR.FLOW_NET_5D"})

    def test_index_is_date_typed(self):
        """全模块约定：系列索引为 datetime.date（否则 _clip 会炸，见上面的回归）。"""
        for code, s in self.out.items():
            with self.subTest(code=code):
                self.assertTrue(all(isinstance(d, D) for d in s.index))
                self.assertEqual(len(s), len(self.idx))

    def test_ratio_ranges(self):
        for code in ("SECTOR.UP_RATIO", "SECTOR.FLOW_UP_RATIO"):
            s = self.out[code].dropna()
            with self.subTest(code=code):
                self.assertTrue(((s >= 0) & (s <= 100)).all())

    def test_flow_5d_requires_five_days(self):
        """源无历史：数据不足 5 个交易日时 FLOW_NET_5D 必须为空（不得拿部分窗口充完整窗口）。"""
        self.assertEqual(int(self.out["SECTOR.FLOW_NET_5D"].notna().sum()), 0)


class TestSectorMappingAndDaily(unittest.TestCase):
    """映射覆盖 + 逐行业横截面不变量（需数据库）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from db import get_conn
            cls._cm = get_conn()
            cls.conn = cls._cm.__enter__()
        except Exception as e:
            raise unittest.SkipTest(f"数据库不可用: {e}")
        cls.cur = cls.conn.cursor()
        cls.cur.execute("SELECT DISTINCT bench_code FROM daily_benchmark WHERE bench_code LIKE 'CN.SW%'")
        cls.sw_codes = {r[0].replace("CN.SW", "") for r in cls.cur.fetchall()}

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cm.__exit__(None, None, None)
        except Exception:
            pass

    def test_mapping_targets_are_valid_sw(self):
        """映射目标必须是真实存在的申万一级行业（防拼写错误静默丢数据）。"""
        self.cur.execute("SELECT ths_name, sw_code FROM regime.sector_mapping WHERE sw_code IS NOT NULL")
        for ths, sw in self.cur.fetchall():
            with self.subTest(ths=ths):
                self.assertIn(sw, self.sw_codes)

    def test_flow_sectors_all_mapped(self):
        """资金流源里的每个行业都必须有映射，否则该行业资金流被静默丢弃。"""
        self.cur.execute("""SELECT DISTINCT f.sector_name FROM regime.sector_fund_flow f
                            LEFT JOIN regime.sector_mapping m ON m.ths_name = f.sector_name
                            WHERE m.ths_name IS NULL""")
        self.assertEqual([r[0] for r in self.cur.fetchall()], [])

    def test_mapping_covers_all_sw(self):
        """31 个申万一级行业都应有至少一个同花顺行业对口（否则资金流覆盖有洞）。"""
        self.cur.execute("SELECT DISTINCT sw_code FROM regime.sector_mapping")
        self.assertEqual(self.sw_codes - {r[0] for r in self.cur.fetchall()}, set())

    def test_sector_daily_invariants(self):
        """横截面不变量：每日 rs_20d 均值为 0、rs_rank 唯一覆盖 1..N。"""
        self.cur.execute("""SELECT trade_date, count(*) n, avg(rs_20d), max(rs_rank)
                            FROM regime.sector_daily
                            WHERE trade_date >= CURRENT_DATE - 400 AND rs_20d IS NOT NULL
                            GROUP BY 1 ORDER BY 1 DESC LIMIT 20""")
        rows = self.cur.fetchall()
        self.assertTrue(rows, "近 400 日无 sector_daily 数据")
        for d, n, avg_rs, max_rank in rows:
            with self.subTest(date=str(d)):
                # rs_20d 落库为 4 位小数，31 个行业取均值后有 ~1e-6 量级舍入残差 → 用绝对容差
                self.assertAlmostEqual(float(avg_rs), 0.0, delta=1e-3)
                self.assertEqual(int(max_rank), n)

    def test_flow_conservation(self):
        """行业资金流按映射汇总后，合计应等于市场级 SECTOR.FLOW_NET（不得丢也不得重复计）。"""
        self.cur.execute("""SELECT d.trade_date,
                                   sum(d.fund_flow_net) AS by_sector,
                                   (SELECT raw_value FROM regime.indicator_value
                                     WHERE indicator_code='SECTOR.FLOW_NET' AND trade_date=d.trade_date) AS market
                            FROM regime.sector_daily d WHERE d.fund_flow_net IS NOT NULL
                            GROUP BY d.trade_date ORDER BY 1 DESC LIMIT 5""")
        rows = self.cur.fetchall()
        self.assertTrue(rows, "无资金流数据（源只给当日快照，需先采集）")
        for d, by_sector, market in rows:
            with self.subTest(date=str(d)):
                self.assertAlmostEqual(float(by_sector), float(market), places=2)


class TestSectorValuationSnapshot(unittest.TestCase):
    """行业估值快照契约：代码合法性、取值域合理、市场级指标 = 快照中位数（可交叉校验）。"""

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

    def test_codes_are_valid_sw(self):
        self.cur.execute("SELECT DISTINCT bench_code FROM daily_benchmark WHERE bench_code LIKE 'CN.SW%'")
        sw_codes = {r[0].replace("CN.SW", "") for r in self.cur.fetchall()}
        self.cur.execute("SELECT DISTINCT sw_code FROM regime.sector_valuation_snapshot")
        codes = {r[0] for r in self.cur.fetchall()}
        self.assertTrue(codes, "估值快照表为空（先跑 get_sector_valuation.py）")
        self.assertEqual(codes - sw_codes, set())

    def test_value_ranges(self):
        """估值需在合理量级（PE/PB 为正；股息率 0~20%）——挡掉源侧单位/口径漂移。"""
        self.cur.execute("""SELECT count(*) FILTER (WHERE pe_ttm IS NOT NULL AND (pe_ttm <= 0 OR pe_ttm > 300)),
                                   count(*) FILTER (WHERE pb IS NOT NULL AND (pb <= 0 OR pb > 100)),
                                   count(*) FILTER (WHERE div_yield IS NOT NULL AND (div_yield < 0 OR div_yield > 20))
                            FROM regime.sector_valuation_snapshot""")
        bad_pe, bad_pb, bad_dy = self.cur.fetchone()
        self.assertEqual((bad_pe, bad_pb, bad_dy), (0, 0, 0))

    def test_market_indicator_matches_snapshot_median(self):
        """市场级指标必须等于快照的中位数（防口径漂移：改了 SQL 忘改指标口径）。"""
        self.cur.execute("""SELECT s.snapshot_date, median_pe.pe, median_pb.pb
                            FROM (SELECT DISTINCT snapshot_date FROM regime.sector_valuation_snapshot
                                  ORDER BY 1 DESC LIMIT 1) s,
                                 LATERAL (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY pe_ttm) AS pe
                                          FROM regime.sector_valuation_snapshot WHERE snapshot_date = s.snapshot_date) median_pe,
                                 LATERAL (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY pb) AS pb
                                          FROM regime.sector_valuation_snapshot WHERE snapshot_date = s.snapshot_date) median_pb""")
        d, pe, pb = self.cur.fetchone()
        self.cur.execute("""SELECT indicator_code, raw_value FROM regime.indicator_value
                            WHERE trade_date = %s AND indicator_code IN ('SECTOR.PE_TTM_MEDIAN','SECTOR.PB_MEDIAN')""",
                         (d,))
        got = dict(self.cur.fetchall())
        self.assertTrue(got, f"{d} 无行业估值指标（计算层未接入？）")
        self.assertAlmostEqual(float(got["SECTOR.PE_TTM_MEDIAN"]), round(float(pe), 4), places=2)
        self.assertAlmostEqual(float(got["SECTOR.PB_MEDIAN"]), round(float(pb), 4), places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
