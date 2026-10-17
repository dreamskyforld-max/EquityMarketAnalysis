"""写入护栏 + 覆盖度门禁测试（不连库）。

背景（2026-10-14 事故，勿删）：hk_daily_quote 全市场采集因单只复牌股量比
（HK.00129 volume_ratio=49054）超出 NUMERIC(8,4) 上限而整批失败，**当天全市场
2196 只一行未落**；下游 market_state_daily 不设门禁，用仅剩的监控池 77 只算横截面，
港股 PE/PB/破净/涨跌家数占比等指标全部算错并长期留在库里。

本文件锁住三层修复的行为：
  ① db.sanitize_numeric_rows / widen_numeric_columns  —— 写入侧越界净化与列宽自愈
  ② get_{hk,a}_market_turnover._save_quotes            —— 分块事务 + 逐行降级 + 全失败抛错
  ③ market_state_daily._coverage_gate / _mask_days     —— 计算层覆盖度门禁
  ④ monitor_collector._coverage_verdict                —— 监控层覆盖度分级
"""
import datetime
import os
import sys
import unittest
from collections import Counter
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import market_state_daily as msd
import monitor_collector as mc

D = datetime.date


# ── fake 连接（只实现护栏用到的 cursor/事务协议）────────────────────────────
class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        # information_schema 查询 → 返回 conn.limits；其它一律空结果
        if "information_schema.columns" in sql:
            self._rows = [(c, p, s) for c, (p, s) in self.conn.limits.items()]
        else:
            self._rows = []

    def fetchall(self):
        return getattr(self, "_rows", [])

    @property
    def rowcount(self):
        return 0


class _FakeConn:
    """假连接：记录 execute 过的 SQL；limits = {列名: (precision, scale)}。"""

    def __init__(self, limits):
        self.limits = limits
        self.committed = 0
        self.cursor_obj = _FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1


# ── ① db 写入侧护栏 ────────────────────────────────────────────────────────
class TestSanitizeNumericRows(unittest.TestCase):
    LIMITS = {"turnover_rate": (8, 4), "volume_ratio": (8, 4),
              "pe_ttm_ratio": (12, 4), "amount": (20, 2), "trade_date": (None, None)}

    def setUp(self):
        db._NUMERIC_LIMITS_CACHE.clear()
        self.addCleanup(db._NUMERIC_LIMITS_CACHE.clear)
        self.conn = _FakeConn(self.LIMITS)
        # 直接喂列宽，绕开 information_schema 查询（信息已在 fake 里）
        db._NUMERIC_LIMITS_CACHE["hk_daily_quote"] = self.LIMITS

    def _run(self, rows):
        return db.sanitize_numeric_rows(self.conn, "hk_daily_quote", rows)

    def test_out_of_range_set_none_and_reported(self):
        """越界值置 None 并回传清单（复牌股量比 49054 是 2026-10-14 事故的元凶）。"""
        rows = [{"stock_code": "HK.00129", "volume_ratio": 49054.375,
                 "turnover_rate": 0.5, "pe_ttm_ratio": 12.0}]
        out, dropped = self._run(rows)
        self.assertIsNone(out[0]["volume_ratio"])
        self.assertEqual(out[0]["turnover_rate"], 0.5)      # 正常列不受影响
        self.assertEqual(out[0]["pe_ttm_ratio"], 12.0)      # 宽列不误伤
        self.assertEqual(dropped, [("HK.00129", "volume_ratio", 49054.375)])

    def test_boundary_inclusive(self):
        """|v| >= 10^(p-s) 一律作废：10000 对 NUMERIC(8,4) 会因进位溢出，9999.9999 可以过。"""
        rows = [{"stock_code": "A", "volume_ratio": 10000.0},
                {"stock_code": "B", "volume_ratio": 9999.9999},
                {"stock_code": "C", "volume_ratio": -10000.0},
                {"stock_code": "E", "volume_ratio": 49054.0}]
        out, _ = self._run(rows)
        self.assertEqual([r["volume_ratio"] for r in out],
                         [None, 9999.9999, None, None])

    def test_non_finite_set_none(self):
        """NaN / inf 也不能进 NUMERIC（NaN 能写进去但会污染下游 NULLIF 口径）。"""
        out, dropped = self._run([{"stock_code": "A", "volume_ratio": float("nan")},
                                  {"stock_code": "B", "volume_ratio": float("inf")}])
        self.assertEqual([r["volume_ratio"] for r in out], [None, None])
        self.assertEqual(len(dropped), 2)

    def test_strings_and_none_untouched(self):
        """字符串 / None / bool 不动（本模块只对数值列做判定，避免误伤）。"""
        rows = [{"stock_code": "HK.00700", "volume_ratio": None, "trade_date": D(2026, 10, 14)}]
        out, dropped = self._run(rows)
        self.assertIsNone(out[0]["volume_ratio"])
        self.assertEqual(dropped, [])
        self.assertEqual(out[0]["trade_date"], D(2026, 10, 14))

    def test_read_limits_failure_degrades_to_noop(self):
        """information_schema 读不到（表不存在等）→ 不净化也不抛错。"""
        db._NUMERIC_LIMITS_CACHE.clear()
        conn = _FakeConn({})
        rows = [{"stock_code": "A", "volume_ratio": 49054.375}]
        out, dropped = db.sanitize_numeric_rows(conn, "no_such_table", rows)
        self.assertEqual(out[0]["volume_ratio"], 49054.375)
        self.assertEqual(dropped, [])

    def test_numeric_limits_cached_per_table(self):
        """列宽按表缓存（同表第二次不发 information_schema 查询）。"""
        db._NUMERIC_LIMITS_CACHE.clear()
        conn = _FakeConn({"volume_ratio": (8, 4)})
        self.assertEqual(db.numeric_limits(conn, "t"), {"volume_ratio": (8, 4)})
        conn.cursor_obj.sql.clear()
        self.assertEqual(db.numeric_limits(conn, "t"), {"volume_ratio": (8, 4)})
        self.assertEqual(conn.cursor_obj.sql, [])   # 命中缓存，没有新查询


class TestWidenNumericColumns(unittest.TestCase):
    def test_only_narrower_columns_are_altered(self):
        """已够宽的列完全不动（避免每次运行都触发表重写）；更窄的才 ALTER。"""
        conn = _FakeConn({"volume_ratio": (8, 4), "turnover_rate": (12, 4),
                          "dividend_ratio_ttm": (8, 4)})
        db._NUMERIC_LIMITS_CACHE.clear()
        changed = db.widen_numeric_columns(
            conn, "hk_daily_quote", {c: (12, 4) for c in db._PERCENT_COLS_12_4})
        self.assertEqual(changed, ["volume_ratio", "dividend_ratio_ttm"])
        alters = [s for s in conn.cursor_obj.sql if "ALTER TABLE" in s]
        self.assertEqual(len(alters), 2)
        self.assertIn("volume_ratio", alters[0])
        # 改完列宽缓存必须失效，否则后续 sanitize 仍按旧列宽判定
        self.assertNotIn("hk_daily_quote", db._NUMERIC_LIMITS_CACHE)

    def test_missing_column_skipped(self):
        """库里没有的列跳过（不生成 ALTER，不会因拼列名炸库）。"""
        conn = _FakeConn({"volume_ratio": (8, 4)})
        db._NUMERIC_LIMITS_CACHE.clear()
        changed = db.widen_numeric_columns(conn, "t", {"turnover_rate": (12, 4)})
        self.assertEqual(changed, [])


# ── ② 池表 _save_quotes 分块容错 ───────────────────────────────────────────
class TestSaveQuotesResilience(unittest.TestCase):
    """_save_quotes：分块事务 + 逐块失败转逐行 + 全 miss 抛错（不再静默断供）。"""

    def _run(self, module, rows, fail_predicate):
        """fail_predicate(chunk) → True 表示整块失败；逐行只对 fail_predicate([r]) 为 True 的行失败。"""
        conn = _FakeConn({})
        calls = []

        def fake_bulk_upsert(c, table, data_list, conflict_cols=None, **kw):
            calls.append([r["stock_code"] for r in data_list])
            if fail_predicate(data_list):
                raise RuntimeError("boom")

        ctx = contextmanager(lambda: iter([conn]))()
        with mock.patch.object(module, "get_conn", lambda: ctx), \
             mock.patch.object(module, "_ensure_table", lambda t: None), \
             mock.patch.object(module, "bulk_upsert", fake_bulk_upsert), \
             mock.patch("db.bulk_upsert", fake_bulk_upsert), \
             mock.patch("db.sanitize_numeric_rows",
                        lambda c, t, rows: (rows, [])):
            module._save_quotes(rows)
        return calls

    def test_one_bad_block_does_not_lose_the_other(self):
        """整块失败只损失该块，其它块照常落库（旧实现是整表一个事务，全丢）。"""
        import get_hk_market_turnover as hk
        rows = ([{"stock_code": f"HK.{i:05d}", "volume_ratio": 1.0} for i in range(4000)])
        calls = self._run(hk, rows, lambda chunk: len(chunk) == 2000 and chunk[0]["stock_code"] == "HK.00000")
        # 调用形态：第一块 2000 行整块失败 1 次 → 转逐行 2000 次 → 第二块 2000 行整体成功 1 次
        self.assertEqual(Counter(len(c) for c in calls), {2000: 2, 1: 2000})
        self.assertEqual(calls[0][0], "HK.00000")                      # 首次就是整块尝试
        self.assertEqual(calls[1], ["HK.00000"])                       # 紧接着逐行降级
        self.assertEqual(calls[-1][0], "HK.02000")                     # 第二块整体落库

    def test_all_blocks_fail_raises(self):
        """全 miss 必须抛错让调度器记 error（2026-09-15「0 数据却记成功」同款防线）。"""
        import get_hk_market_turnover as hk
        rows = [{"stock_code": "HK.00001"}, {"stock_code": "HK.00002"}]
        with self.assertRaises(RuntimeError):
            self._run(hk, rows, lambda chunk: True)

    def test_a_market_same_guard(self):
        """A 股池表同款护栏（同结构同风险，一并锁住）。"""
        import get_a_market_turnover as am
        rows = [{"stock_code": f"SH.{i:06d}"} for i in range(2500)]
        calls = self._run(am, rows, lambda chunk: len(chunk) == 2000)
        # 第一块 2000 行失败 → 逐行 2000 次；第二块 500 行整体成功
        self.assertEqual(Counter(len(c) for c in calls), {1: 2000, 2000: 1, 500: 1})


# ── ③ 计算层覆盖度门禁 ─────────────────────────────────────────────────────
class TestCoverageGate(unittest.TestCase):
    @staticmethod
    def _df(counts, start=D(2026, 9, 1)):
        """按 counts（list[int]）造一个带 n/adv/dec 列的横截面 df（每日一行）。

        返回 (df, 每个 count 对应的日期列表)——测试不硬编码日期（跳过周末）。
        """
        idx, rows, dates = [], [], []
        d = start
        for c in counts:
            while d.weekday() >= 5:      # 跳过周末，模拟交易日
                d += datetime.timedelta(days=1)
            idx.append(d)
            dates.append(d)
            rows.append({"n": c, "adv": c // 2, "dec": c // 3, "pe_med": 11.0})
            d += datetime.timedelta(days=1)
        import pandas as pd
        return pd.DataFrame(rows, index=idx), dates

    def test_collapsed_day_is_voided_before_series(self):
        """样本塌陷日整行置 NaN 且在返回集合里（2026-10-14：77 只 vs 近 20 日中位 ~2000）。"""
        df, dates = self._df([2000] * 25 + [77] + [2000] * 5)
        bad = msd._coverage_gate(df, "HK")
        self.assertEqual(bad, {dates[25]})
        self.assertTrue(df.loc[dates[25]].isna().all())        # 整行 NaN
        self.assertFalse(df.loc[dates[24]].isna().any())       # 邻日不受影响

    def test_mild_shortfall_not_voided(self):
        """轻微缺口（0.75，如 09-25）只告警不作废 —— 阈值 0.6 落在 0.25/0.75 空隙中。"""
        df, dates = self._df([2000] * 25 + [1500] + [2000] * 3)
        bad = msd._coverage_gate(df, "HK")
        self.assertEqual(bad, set())
        self.assertEqual(int(df.loc[dates[25], "n"]), 1500)

    def test_no_median_reference_never_voids(self):
        """窗口内没有可比基线（min_periods 未满）→ 不判（避免冷启动误伤）。"""
        df, _ = self._df([50, 60, 70])
        self.assertEqual(msd._coverage_gate(df, "HK"), set())

    def test_rolling_series_do_not_propagate_pollution(self):
        """派生滚动序列必须**看不到**脏日：输入置 NaN → rolling 自动跳过，不污染顺延日。

        判据：塌陷日 dec/n=0（正常日 =1/3）。若脏日被算进 20 日窗口，顺延日均值会被
        拉低到 ~0.317；被正确排除则应 ≈0.333。
        """
        import pandas as pd
        df, dates = self._df([2000] * 25 + [77] + [2000] * 3)
        msd._coverage_gate(df, "HK")
        ratio = (df["dec"] / df["n"] * 100)
        self.assertTrue(pd.isna(ratio.loc[dates[25]]))            # 脏日本身无值
        roll = ratio.rolling(20, min_periods=10).mean()
        self.assertGreater(roll.loc[dates[26]], 0.33)             # 顺延日未计入脏日

    def test_mask_days(self):
        import pandas as pd
        s = pd.Series([1.0, 2.0, 3.0], index=[D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15)])
        out = msd._mask_days(s, {D(2026, 10, 14)})
        self.assertTrue(pd.isna(out.loc[D(2026, 10, 14)]))
        self.assertEqual(out.loc[D(2026, 10, 13)], 1.0)
        self.assertIs(msd._mask_days(s, set()), s)              # 空集合原样返回

    def test_cross_sectional_codes_cover_both_markets(self):
        """横截面代码清单必须同时覆盖 CN 与 HK 两侧产出（清库/作废都按它）。"""
        self.assertIn("BREADTH.ADV_RATIO", msd._CROSS_SECTIONAL_CODES)
        self.assertIn("VAL.PE_TTM_MEDIAN", msd._CROSS_SECTIONAL_CODES)
        self.assertIn("VAL.PCT_BELOW_PB_1", msd._CROSS_SECTIONAL_CODES)
        self.assertIn("FLOW.MARGIN_RATIO", msd._CROSS_SECTIONAL_CODES)   # 仅 CN
        self.assertNotIn("FLOW.NORTHBOUND_NET", msd._CROSS_SECTIONAL_CODES)  # 外部源不作废


# ── ④ 监控层覆盖度分级 ─────────────────────────────────────────────────────
class TestCoverageVerdict(unittest.TestCase):
    def test_collapse_is_crit(self):
        """塌陷（0.2）→ crit；消息里要能看出是哪天、多少行。"""
        counts = {D(2026, 10, 1) + datetime.timedelta(days=i): 2200 for i in range(25)}
        counts[D(2026, 10, 20)] = 550                     # 25% 基线
        d, r, level = mc._coverage_verdict(counts, 0.75, mc._COVERAGE_CRIT_RATIO, 20)
        self.assertEqual(d, D(2026, 10, 20))
        self.assertAlmostEqual(r, 0.25)
        self.assertEqual(level, "crit")

    def test_mild_shortfall_is_warn(self):
        counts = {D(2026, 10, 1) + datetime.timedelta(days=i): 2200 for i in range(25)}
        counts[D(2026, 10, 20)] = 1600                     # 73% 基线
        d, r, level = mc._coverage_verdict(counts, 0.75, mc._COVERAGE_CRIT_RATIO, 20)
        self.assertEqual(level, "warn")
        self.assertAlmostEqual(r, 0.727, places=2)

    def test_boundary_equal_is_ok(self):
        """比值恰等于阈值不算越界（判定用严格小于）。"""
        counts = {D(2026, 10, 1) + datetime.timedelta(days=i): 2200 for i in range(25)}
        counts[D(2026, 10, 20)] = 1650                     # 恰好 75%
        self.assertEqual(mc._coverage_verdict(counts, 0.75, 0.5, 20)[2], "ok")

    def test_normal_days_ok(self):
        counts = {D(2026, 10, 1) + datetime.timedelta(days=i): 2200 + i for i in range(25)}
        self.assertEqual(mc._coverage_verdict(counts, 0.75, 0.5, 20)[2], "ok")

    def test_insufficient_history_ok(self):
        self.assertEqual(mc._coverage_verdict({D(2026, 10, 1): 5}, 0.75, 0.5, 20)[2], "ok")


# ── ⑤ _write_market 的失效日处理（pop / 清库 / 合成跳过）─────────────────────
class _FakeWriteConn:
    """记录 DELETE 的假连接（_write_market 只用它执行 DELETE）。"""

    def __init__(self):
        self.deletes: list = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                if sql.strip().upper().startswith("DELETE"):
                    conn.deletes.append((sql.split("WHERE")[0].strip(), params))

            rowcount = 7
        return _Cur()


class TestWriteMarketInvalidDays(unittest.TestCase):
    """_write_market：带外键被 pop、塌陷日清库、合成行跳过、非横截面指标仍写。"""

    def test_invalid_days_purged_and_composite_skipped(self):
        import pandas as pd
        idx = [D(2026, 10, 13), D(2026, 10, 14)]
        series = {
            "BREADTH.ADV_RATIO": pd.Series([38.0, 34.0], index=idx),
            "FLOW.NORTHBOUND_NET": pd.Series([1.0, 2.0], index=idx),   # 非横截面：塌陷日照写
            msd._INVALID_DATES_KEY: {D(2026, 10, 14)},
        }
        conn = _FakeWriteConn()
        captured = {}

        def fake_bulk_upsert(c, table, rows, **kw):
            captured.setdefault(table, []).extend(rows)

        comp = pd.DataFrame({
            "thermometer": [28.0, 31.0], "risk_score": [54.0, 59.0], "fsi": [50.0, 50.0],
            "risk_appetite": [40.0, 41.0], "season": ["冬", "冬"], "season_score": [30.0, 31.0],
            "top_signal_cnt": [1, 1], "bottom_signal_cnt": [0, 0],
            "_top_hits": ["a", "b"], "_bottom_hits": ["", ""],
        }, index=idx)
        with mock.patch.object(msd, "_load_stored_raw", lambda c, m: pd.DataFrame()), \
             mock.patch.object(msd, "build_composites", lambda p, r, m: comp), \
             mock.patch("db.bulk_upsert", fake_bulk_upsert), \
             mock.patch.object(msd, "bulk_upsert", fake_bulk_upsert):
            msd._write_market(conn, "HK", series, D(2026, 10, 13), True)

        # ① 带外键被 pop：不会变成一列指标（否则 iv_rows 里会出现它）
        codes_written = {r["indicator_code"] for r in captured["regime.indicator_value"]}
        self.assertNotIn(msd._INVALID_DATES_KEY, codes_written)
        # ② 塌陷日：横截面指标被清库（两条 DELETE：指标 + 合成）
        self.assertEqual(len(conn.deletes), 2)
        for sql, params in conn.deletes:
            self.assertIn(D(2026, 10, 14), params[1])
        # ③ 合成行跳过塌陷日，但保留正常日
        mr_dates = [r["trade_date"] for r in captured["regime.market_regime_daily"]]
        self.assertEqual(mr_dates, [D(2026, 10, 13)])
        # ④ 非横截面指标两天都写（外部资金流与当日样本数无关）
        nb = {(r["trade_date"]) for r in captured["regime.indicator_value"]
              if r["indicator_code"] == "FLOW.NORTHBOUND_NET"}
        self.assertEqual(nb, {D(2026, 10, 13), D(2026, 10, 14)})

    def test_no_invalid_dates_no_delete(self):
        """没有塌陷日时完全不做 DELETE（不误删正常数据）。"""
        import pandas as pd
        idx = [D(2026, 10, 13), D(2026, 10, 14)]
        conn = _FakeWriteConn()
        # 合成结果必须带 mr_rows 用到的列（season/_top_hits 等），否则是测试桩不完整
        cols = {"thermometer": [28.0, 31.0], "risk_score": [54.0, 59.0], "fsi": [50.0, 50.0],
                "risk_appetite": [40.0, 41.0], "season": ["冬", "冬"], "season_score": [30.0, 31.0],
                "top_signal_cnt": [1, 1], "bottom_signal_cnt": [0, 0],
                "_top_hits": ["a", "b"], "_bottom_hits": ["", ""]}
        with mock.patch.object(msd, "_load_stored_raw", lambda c, m: pd.DataFrame()), \
             mock.patch.object(msd, "build_composites",
                               lambda p, r, m: pd.DataFrame(cols, index=idx)), \
             mock.patch("db.bulk_upsert", lambda *a, **k: None), \
             mock.patch.object(msd, "bulk_upsert", lambda *a, **k: None):
            msd._write_market(conn, "HK",
                              {"BREADTH.ADV_RATIO": pd.Series([38.0, 34.0], index=idx)},
                              D(2026, 10, 13), True)
        self.assertEqual(conn.deletes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
