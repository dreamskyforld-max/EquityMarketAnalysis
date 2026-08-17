"""
market_scheduler 测试
覆盖：is_trading_hours（多市场）、MARKET_PRESETS 配置、build_scheduler、import 完整性
"""
import sys, os, unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIsTradingHours(unittest.TestCase):
    """交易时段判断 —— 纯函数 + 边界值"""

    def test_function_exists_and_returns_bool(self):
        from market_scheduler import is_trading_hours
        result = is_trading_hours()
        self.assertIsInstance(result, bool)

    def test_market_param_accepted(self):
        """接受 market 参数"""
        from market_scheduler import is_trading_hours
        r1 = is_trading_hours("HK")
        r2 = is_trading_hours("A")
        self.assertIsInstance(r1, bool)
        self.assertIsInstance(r2, bool)

    def test_current_state_consistent(self):
        """连续调用两次应返回相同结果"""
        from market_scheduler import is_trading_hours
        r1 = is_trading_hours()
        r2 = is_trading_hours()
        self.assertEqual(r1, r2)

    def test_weekday_check_exists(self):
        """确认函数包含核心判断逻辑"""
        import inspect
        from market_scheduler import is_trading_hours
        source = inspect.getsource(is_trading_hours)
        self.assertIn('weekday', source)
        self.assertIn('m >= 30', source)
        # HK 午盘
        self.assertIn('13 <= h', source)
        self.assertIn('h < 16', source)
        # A 股市场分支
        self.assertIn('"A"', source)


class TestConfigStructure(unittest.TestCase):
    """MARKET_PRESETS / STOCKS / GLOBAL_TASKS 配置结构正确性"""

    def setUp(self):
        from market_scheduler import MARKET_PRESETS, STOCKS, GLOBAL_TASKS
        self.presets = MARKET_PRESETS
        self.stocks = STOCKS
        self.globals = GLOBAL_TASKS

    def test_stocks_list_not_empty(self):
        self.assertIsInstance(self.stocks, list)
        self.assertGreater(len(self.stocks), 0, "STOCKS 至少需要有1只股票")

    def test_each_stock_has_code_and_market(self):
        # stock_info.market 存交易所代码(SH/SZ/HK)，MARKET_PRESETS 用 A/HK，
        # 需先归一化再判断（与 build_scheduler 一致）
        for s in self.stocks:
            self.assertIn('code', s, f"缺少 code: {s}")
            self.assertIn('market', s, f"缺少 market: {s}")
            norm = "A" if s['market'] in ("SH", "SZ") else s['market']
            self.assertIn(norm, self.presets,
                          f"market={s['market']} 归一化后 {norm} 不在 MARKET_PRESETS 中")

    def test_hk_preset_has_all_sections(self):
        hk = self.presets.get("HK", {})
        self.assertIsInstance(hk.get("intraday"), list, "HK 缺少 intraday")
        daily = hk.get("daily")
        self.assertIsNotNone(daily, "HK 缺少 daily")
        self.assertIn("time", daily or {}, "HK daily 缺少 time")
        self.assertIsInstance((daily or {}).get("modules"), list, "HK daily 缺少 modules")
        self.assertIsInstance(hk.get("extras"), list, "HK 缺少 extras")

    def test_a_preset_has_all_sections(self):
        a = self.presets.get("A", {})
        self.assertIsInstance(a.get("intraday"), list, "A 缺少 intraday")
        daily = a.get("daily")
        self.assertIsNotNone(daily, "A 缺少 daily")
        self.assertIn("time", daily or {}, "A daily 缺少 time")
        self.assertIsInstance(a.get("extras"), list, "A 缺少 extras")

    def test_short_selling_retry_times(self):
        """全日沽空数据应为全局任务且至少采集 2 次（18:30 主采 + 19:30 补采）"""
        short_names = [n for n, *_ in self.globals if "沽空" in n]
        self.assertGreaterEqual(len(short_names), 2,
                                f"全日沽空全局任务只找到 {len(short_names)} 次，期望至少2次")
        # 且不应再作为逐票任务挂在 HK extras 中
        hk = self.presets.get("HK", {})
        extras = hk.get("extras", [])
        extra_short = [n for n, *_ in extras if "沽空" in n]
        self.assertEqual(extra_short, [], f"全日沽空不应再出现在 HK extras: {extra_short}")

    def test_daily_time_field_valid(self):
        """各市场 daily.time 的 hour/minute 合法"""
        for market, preset in self.presets.items():
            daily = preset.get("daily")
            if not daily:
                continue
            t = daily.get("time", {})
            self.assertIn('hour', t, f"{market} daily.time 缺少 hour")
            self.assertIn('minute', t, f"{market} daily.time 缺少 minute")
            self.assertTrue(0 <= t['hour'] <= 23,
                            f"{market} hour={t['hour']} 越界")
            self.assertTrue(0 <= t['minute'] <= 59,
                            f"{market} minute={t['minute']} 越界")

    def test_intraday_modules_are_py_scripts(self):
        """intraday 模块以 .py 结尾"""
        for market, preset in self.presets.items():
            for m in preset.get("intraday", []):
                self.assertTrue(m.endswith('.py'),
                                f"{market} intraday 模块 [{m}] 不以 .py 结尾")

    def test_daily_modules_are_py_scripts(self):
        """daily.modules 以 .py 结尾"""
        for market, preset in self.presets.items():
            daily = preset.get("daily", {})
            for m in daily.get("modules", []):
                self.assertTrue(m.endswith('.py'),
                                f"{market} daily 模块 [{m}] 不以 .py 结尾")

    def test_no_duplicate_extra_names_hk(self):
        """HK extras 无重名"""
        hk = self.presets.get("HK", {})
        extras = hk.get("extras", [])
        names = [e[0] for e in extras]
        self.assertEqual(len(names), len(set(names)),
                         f"HK extras 有重复名称: {names}")

    def test_global_tasks_is_list(self):
        self.assertIsInstance(self.globals, list)


class TestBuildScheduler(unittest.TestCase):
    """调度器构建 —— 验证任务注册"""

    def test_build_returns_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from market_scheduler import build_scheduler

        sched = build_scheduler()
        self.assertIsInstance(sched, BackgroundScheduler)

        sched.start()
        import time; time.sleep(0.1)
        jobs = sched.get_jobs()
        self.assertGreaterEqual(len(jobs), 1, "至少注册了1个任务")

        sched.shutdown(wait=False)

    def test_all_tasks_registered(self):
        """注册任务数 = 盘中批量全局任务(按市场) + 收盘组 + extras + 全局

        record_trend/get_quote 已从「每只股票每分钟一个任务」改为「每个市场
        每分钟一个全局批量任务」，故盘中任务数不再按股票逐只累加，而按市场去重。
        """
        from market_scheduler import build_scheduler, STOCKS, MARKET_PRESETS, GLOBAL_TASKS

        # 归一化市场：stock_info.market 存 SH/SZ/HK，MARKET_PRESETS 用 A/HK
        def _norm(mkt):
            return "A" if mkt in ("SH", "SZ") else mkt

        # 计算期望任务数
        # 盘中批量采集任务已作为 GLOBAL_TASKS 项追加（见 market_scheduler.py
        # INTRADAY_GLOBAL_MODULES 那段），已计入 len(GLOBAL_TASKS)，无需再按市场去重累加。
        expected = len(GLOBAL_TASKS)
        for stock in STOCKS:
            preset = MARKET_PRESETS.get(_norm(stock["market"]), {})
            if preset.get("daily"):
                expected += 1
            expected += len(preset.get("extras", []))

        sched = build_scheduler()
        sched.start()
        import time; time.sleep(0.2)

        jobs = sched.get_jobs()
        self.assertEqual(len(jobs), expected,
                         f"注册任务数 {len(jobs)} != 期望 {expected}")

        sched.shutdown(wait=False)


class TestImportIntegrity(unittest.TestCase):
    """import 完整性"""

    def test_all_exports_available(self):
        import market_scheduler as ms
        self.assertTrue(callable(ms.is_trading_hours))
        self.assertTrue(callable(ms.build_scheduler))
        self.assertTrue(callable(ms.execute_task))
        self.assertIsInstance(ms.MARKET_PRESETS, dict)
        self.assertIsInstance(ms.STOCKS, list)
        self.assertIsInstance(ms.GLOBAL_TASKS, list)
        self.assertIsInstance(ms.SCRIPTS_DIR, str)


if __name__ == '__main__':
    unittest.main()
