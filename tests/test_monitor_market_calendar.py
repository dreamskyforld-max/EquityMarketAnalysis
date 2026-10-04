"""监控「按市场取交易日历」测试（无需数据库）。

背景（实测 2026-10-02）：A 股国庆休市、港股开市。监控原本一律用港股日历取
「最近交易日」，导致 A 股专属表被判落后（如一致预期快照报「最新交易日 2026-09-30，
已落后 2 天」）。修复后按表所属市场分别取日历（market='CN' / 默认 'HK'）。
"""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor_collector as mc

D = datetime.date

# 2026 国庆：A 股 10-01 起休市（10-08 复市）；港股 10-01 休、**10-02 开市**、10-03/04 周末
CN_DAYS = {D(2026, 9, 28), D(2026, 9, 29), D(2026, 9, 30), D(2026, 10, 8), D(2026, 10, 9)}
HK_DAYS = {D(2026, 9, 28), D(2026, 9, 29), D(2026, 9, 30), D(2026, 10, 2), D(2026, 10, 5),
           D(2026, 10, 6), D(2026, 10, 7), D(2026, 10, 8), D(2026, 10, 9)}


def _fake_is_trading_day(d, market="HK"):
    """用静态日历替掉真实交易日状态变量（巡检热路径只读变量，故可安全替换）。"""
    return d in (CN_DAYS if market == "CN" else HK_DAYS)


class TestLastTradingDateByMarket(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(mc, "is_trading_day", _fake_is_trading_day)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_cn_vs_hk_on_sunday_after_national_day(self):
        """10-04（周日）：A 股最近交易日 = 09-30，港股 = 10-02（港股 10-02 开市）。"""
        ref = D(2026, 10, 4)
        self.assertEqual(mc._last_trading_date(ref, "CN"), D(2026, 9, 30))
        self.assertEqual(mc._last_trading_date(ref, "HK"), D(2026, 10, 2))

    def test_default_is_hk(self):
        """默认参数保持港股口径（兼容既有告警口径/混合表）。"""
        self.assertEqual(mc._last_trading_date(D(2026, 10, 4)),
                         mc._last_trading_date(D(2026, 10, 4), "HK"))

    def test_same_day_when_trading(self):
        self.assertEqual(mc._last_trading_date(D(2026, 10, 2), "HK"), D(2026, 10, 2))
        self.assertEqual(mc._last_trading_date(D(2026, 10, 2), "CN"), D(2026, 9, 30))


class TestStaleVerdictByMarket(unittest.TestCase):
    """同一份数据（最新 09-30）在 10-04 检查：CN 表应通过、HK 表应告警。"""

    NOW = datetime.datetime(2026, 10, 4, 14, 0, tzinfo=datetime.timezone.utc)

    def setUp(self):
        for name in ("is_trading_day",):
            p = mock.patch.object(mc, name, _fake_is_trading_day)
            p.start()
            self.addCleanup(p.stop)
        self.alerts = []
        self.resolved = []
        p1 = mock.patch.object(mc, "_raise_alert",
                               lambda cat, sev, src, msg: self.alerts.append((src, msg)))
        p2 = mock.patch.object(mc, "_resolve_alert",
                               lambda cat, src: self.resolved.append(src))
        p1.start()
        self.addCleanup(p1.stop)
        p2.start()
        self.addCleanup(p2.stop)

    def _run(self, market):
        today = mc._last_trading_date(self.NOW, market)
        mc._evaluate_stale("t", "c", "day", 1, 0, D(2026, 9, 30), self.NOW, today,
                           active=False)

    def test_cn_table_passes(self):
        """A 股专属表：09-30 已是 A 股最近交易日 → 不应告警。"""
        self._run("CN")
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.resolved, ["t"])

    def test_hk_table_alerts(self):
        """港股类表：港股 10-01/10-02 开市却停更 → 应告警（真实缺口，不是误报）。"""
        self._run("HK")
        self.assertTrue(self.alerts)
        self.assertIn("落后", self.alerts[0][1])


class TestMarketConfigIntegrity(unittest.TestCase):
    """分类清单必须与配置源一致——名字写错会让迁移静默无效（误报照旧）。"""

    def test_every_cn_table_exists_in_config_sources(self):
        public_seed = {t[0] for t in mc._SEED_TABLE_CONFIG}
        regime_seed = {t[1] for t in mc._REGIME_TABLE_CONFIG}
        for db, tbl in mc._CN_MONITOR_TABLES:
            with self.subTest(table=f"{db}.{tbl}"):
                if db == "public":
                    self.assertIn(tbl, public_seed)
                else:
                    self.assertIn(tbl, regime_seed)

    def test_hk_only_tables_not_marked_cn(self):
        """数据实测为港股的表不得被划成 CN（否则港股开市日会漏报告警）。"""
        cn_public = {t for db, t in mc._CN_MONITOR_TABLES if db == "public"}
        for hk_tbl in ("daily_buyback_event", "daily_cbbc", "daily_short_selling",
                       "daily_ggt_hold", "trend_snapshot", "tick_data",
                       "hk_daily_quote", "daily_market_turnover"):
            with self.subTest(table=hk_tbl):
                self.assertNotIn(hk_tbl, cn_public)


if __name__ == "__main__":
    unittest.main(verbosity=2)
