"""PIT 收紧（首次观测时间）防回退测试（2026-11-13）。

锁住 `get_macro_monthly._tighten_release_time` 的四条不变量：
  ① 实时抓到的新期 → release_time 收紧到首次观测时间（不再白等 period+45d）；
  ② 历史回灌 / 白名单外 / 源自带真值 → 逐字不变（历史 PIT 零回归、无前视）；
  ③ 首次观测早于 period+30d 的异常行 → 不采纳（防源提前放表）；
  ④ 重算幂等（first_seen_at 落 extra，第二天跑批结果一致）。
另锁住 `_build_derived`：派生序列 release_time = max(两条输入)，否则上游收紧后
剪刀差会滞后一期，与 CPI/PPI 错配。

全部用 fake conn / mock read_sql，不连数据库、不发网络请求。
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import get_macro_monthly as gmm

D = dt.date
TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 13, 18, 20, tzinfo=TZ)


def _row(code, period, est_lag=45, basis="period+45d", freq="month"):
    d = D.fromisoformat(period)
    return {
        "series_code": code, "period_date": d, "revision": 0, "freq": freq,
        "release_time": dt.datetime.combine(
            d + dt.timedelta(days=est_lag), dt.time(18, 0), tzinfo=TZ),
        "extra": {"release_basis": basis},
    }


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, rows=()):
        self.cur = _FakeCursor(list(rows))

    def cursor(self):
        return self.cur


def _seen(code, period, created, first_seen_at=None):
    return (code, D.fromisoformat(period), 0, created, first_seen_at)


class TestTightenReleaseTime(unittest.TestCase):
    def test_realtime_point_tightened(self):
        """实时抓到（period+39d）→ 收紧到 created_at，并显式落 first_seen_at。"""
        ca = dt.datetime(2026, 11, 9, 18, 21, 31, tzinfo=TZ)
        conn = _FakeConn([_seen("CN.CPI_YOY", "2026-10-01", ca)])
        rows = [_row("CN.CPI_YOY", "2026-10-01")]
        changes, n = gmm._tighten_release_time(conn, rows, now=NOW)
        self.assertEqual((n, len(changes)), (1, 1))
        self.assertEqual(rows[0]["release_time"], ca)
        self.assertEqual(rows[0]["extra"]["first_seen_at"], ca.isoformat())
        self.assertEqual(rows[0]["extra"]["release_basis"],
                         "min(first_seen, period+45d)")
        self.assertIn("ANY", conn.cur.sql)          # SQL 按白名单收窄，不扫全表语义
        self.assertIn("CN.CPI_YOY", conn.cur.params[0])

    def test_backfilled_history_unchanged(self):
        """历史回灌行 SQL 不返回 → first_seen=now 晚于估算 → 保持 period+45d。"""
        conn = _FakeConn([])                        # SQL 双边界已把回灌行排除
        rows = [_row("CN.CPI_YOY", "2025-01-01")]
        before = dict(rows[0])
        before_extra = dict(rows[0]["extra"])
        changes, n = gmm._tighten_release_time(conn, rows, now=NOW)
        self.assertEqual((n, len(changes)), (1, 0))
        self.assertEqual(rows[0], before)
        self.assertEqual(rows[0]["extra"], before_extra)

    def test_non_whitelist_and_source_basis_untouched(self):
        """CLI（有预估/未定稿风险）与源自带真值（source_date）均不参与收紧。"""
        ca = dt.datetime(2026, 11, 9, 18, 21, tzinfo=TZ)
        conn = _FakeConn([_seen("CN.CPI_YOY", "2026-10-01", ca)])
        rows = [_row("GLOBAL.CLI_G20", "2026-10-01"),
                _row("CN.CPI_YOY", "2026-10-01", basis="source_date")]
        changes, n = gmm._tighten_release_time(conn, rows, now=NOW)
        self.assertEqual((n, len(changes)), (0, 0))
        self.assertEqual(rows[0]["extra"]["release_basis"], "period+45d")
        self.assertEqual(rows[1]["extra"]["release_basis"], "source_date")

    def test_floor_blocks_abnormally_early_observation(self):
        """first_seen 早于 period+30d（源提前放表 / 脏数据）→ 不采纳。"""
        ca = dt.datetime(2026, 10, 10, 18, 21, tzinfo=TZ)   # period+9d
        conn = _FakeConn([_seen("CN.CPI_YOY", "2026-10-01", ca)])
        rows = [_row("CN.CPI_YOY", "2026-10-01")]
        changes, _ = gmm._tighten_release_time(conn, rows, now=NOW)
        self.assertEqual(len(changes), 0)
        self.assertEqual(rows[0]["release_time"].date(), D(2026, 11, 15))

    def test_idempotent_recompute(self):
        """第二轮：extra.first_seen_at 已在库 → 结果与首轮逐字一致（无版本抖动）。"""
        ca = dt.datetime(2026, 11, 9, 18, 21, 31, tzinfo=TZ)
        conn1 = _FakeConn([_seen("CN.CPI_YOY", "2026-10-01", ca)])
        r1 = _row("CN.CPI_YOY", "2026-10-01")
        gmm._tighten_release_time(conn1, [r1], now=NOW)
        conn2 = _FakeConn([_seen("CN.CPI_YOY", "2026-10-01", ca,
                                 first_seen_at=ca.isoformat())])
        r2 = _row("CN.CPI_YOY", "2026-10-01")
        changes, _ = gmm._tighten_release_time(conn2, [r2], now=NOW)
        self.assertEqual(len(changes), 1)          # 本轮重算仍会写同一值
        self.assertEqual(r2, r1)

    def test_monetary_credit_whitelist_tightened(self):
        """货币信用序列（2026-11-14 扩充）在官方发布当晚即收紧，不再白等到 period+45d。

        实测场景：2026-10 M2 于 11-13 21:56 首次入库，估算 release_time=11-15 18:00，
        宏观页在 11-14 因此读到 9 月值。加入白名单后应收紧到 11-13。
        """
        ca = dt.datetime(2026, 11, 13, 21, 56, 8, tzinfo=TZ)
        for code in ("CN.M1_YOY", "CN.M2_YOY", "CN.M0_YOY", "CN.NEW_LOAN",
                     "CN.TSF_STOCK", "CN.TSF_STOCK_YOY", "CN.TSF_FLOW_M"):
            with self.subTest(code=code):
                self.assertIn(code, gmm._PIT_FIRST_SEEN_CODES)
                conn = _FakeConn([_seen(code, "2026-10-01", ca)])
                rows = [_row(code, "2026-10-01")]
                changes, n = gmm._tighten_release_time(conn, rows, now=NOW)
                self.assertEqual((n, len(changes)), (1, 1))
                self.assertEqual(rows[0]["release_time"], ca)
                self.assertEqual(rows[0]["extra"]["first_seen_at"], ca.isoformat())

    def test_cli_still_excluded(self):
        """扩充白名单不得把 OECD CLI（有预估/未定稿风险）带进来。"""
        self.assertNotIn("GLOBAL.CLI_G20", gmm._PIT_FIRST_SEEN_CODES)
        self.assertNotIn("CN.CLI_CN", gmm._PIT_FIRST_SEEN_CODES)

    def test_late_collection_keeps_estimate(self):
        """采集断供导致首次观测晚于估算 → 保持估算（不前视、不倒挂）。"""
        ca = dt.datetime(2026, 11, 20, 18, 21, tzinfo=TZ)   # period+50d
        conn = _FakeConn([_seen("CN.CPI_YOY", "2026-10-01", ca)])
        rows = [_row("CN.CPI_YOY", "2026-10-01")]
        changes, _ = gmm._tighten_release_time(conn, rows, now=NOW)
        self.assertEqual(len(changes), 0)
        self.assertEqual(rows[0]["release_time"].date(), D(2026, 11, 15))


class TestDerivedReleaseTime(unittest.TestCase):
    def test_derived_follows_max_input(self):
        """剪刀差 release_time = max(两条输入)，输入收紧后不再滞后一期。"""
        import pandas as pd
        rel_a = dt.datetime(2026, 11, 9, 18, 21, tzinfo=TZ)
        rel_b = dt.datetime(2026, 11, 11, 18, 21, tzinfo=TZ)
        df = pd.DataFrame({
            "period_date": [D(2026, 10, 1)],
            "v": [5.0],
            "rel": [pd.Timestamp(max(rel_a, rel_b))],
        })
        empty = pd.DataFrame(columns=["period_date", "v", "rel"])
        # DERIVED 顺序：M1_M2_SCISSOR 在先、PPI_CPI_SCISSOR 在后
        with mock.patch.object(gmm, "read_sql", side_effect=[empty, df]) as rd:
            rows = gmm._build_derived(_FakeConn())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["series_code"], "CN.PPI_CPI_SCISSOR")
        self.assertEqual(rows[0]["release_time"], max(rel_a, rel_b))
        self.assertEqual(rows[0]["extra"]["release_basis"], "max(inputs)")
        self.assertIn("GREATEST", rd.call_args_list[0].args[1])

    def test_missing_input_release_falls_back(self):
        """输入缺 release_time → 回退 period+45d 保守估算（不把派生值提前可见）。"""
        import pandas as pd
        df = pd.DataFrame({"period_date": [D(2026, 10, 1)],
                           "v": [5.0], "rel": [None]})
        empty = pd.DataFrame(columns=["period_date", "v", "rel"])
        with mock.patch.object(gmm, "read_sql", side_effect=[empty, df]):
            rows = gmm._build_derived(_FakeConn())
        self.assertEqual(rows[0]["release_time"].date(), D(2026, 11, 15))
        self.assertEqual(rows[0]["extra"]["release_basis"], "period+45d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
