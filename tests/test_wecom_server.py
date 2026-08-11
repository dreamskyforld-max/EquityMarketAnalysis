"""
wecom_server_collector 测试
覆盖：parse_stock_code、ALL_MODULES 结构、collect_all 结果格式、import 完整性
"""
import sys, os, json, asyncio, tempfile, unittest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestParseStockCode(unittest.TestCase):
    """股票代码解析 —— 纯函数，最容易测试"""

    def setUp(self):
        from wecom_server_collector import parse_stock_code
        self.parse = parse_stock_code

    def test_hk_with_prefix(self):
        self.assertEqual(self.parse("全量 HK.00700"), "HK.00700")

    def test_hk_digits_only(self):
        self.assertEqual(self.parse("全量 00700"), "HK.00700")

    def test_sh_with_prefix(self):
        self.assertEqual(self.parse("SH.600519"), "SH.600519")

    def test_sh_digits_only(self):
        self.assertEqual(self.parse("实时 600519"), "SH.600519")

    def test_sz_with_prefix(self):
        self.assertEqual(self.parse("SZ.000001"), "SZ.000001")

    def test_sz_digits_only(self):
        self.assertEqual(self.parse("趋势 000001"), "SZ.000001")

    def test_no_code_returns_default(self):
        self.assertEqual(self.parse("全量"), "HK.00700")
        self.assertEqual(self.parse(""), "HK.00700")
        self.assertEqual(self.parse("趋势数据"), "HK.00700")


class TestAllModules(unittest.TestCase):
    """ALL_MODULES 结构正确性 —— 加了新模块不破坏格式"""

    def setUp(self):
        from wecom_server_collector import ALL_MODULES
        self.modules = ALL_MODULES

    def test_each_entry_has_4_fields(self):
        for entry in self.modules:
            self.assertEqual(len(entry), 4, f"条目字段数不对: {entry}")

    def test_name_is_string(self):
        for name, script, need_param, is_rt in self.modules:
            self.assertIsInstance(name, str)
            self.assertIsInstance(script, str)
            self.assertIsInstance(need_param, bool)
            self.assertIsInstance(is_rt, bool)

    def test_scripts_end_with_py(self):
        for name, script, need_param, is_rt in self.modules:
            self.assertTrue(script.endswith('.py'), f"{script} 不是 .py")

    def test_no_duplicate_names_within_same_type(self):
        """同名但在不同 is_rt 组是合理的（盘后+实时各一份），同组内才不允许重名"""
        batch_names = [e[0] for e in self.modules if not e[3]]
        rt_names = [e[0] for e in self.modules if e[3]]
        self.assertEqual(len(batch_names), len(set(batch_names)), "盘后模块名重复")
        self.assertEqual(len(rt_names), len(set(rt_names)), "实时模块名重复")

    def test_at_least_one_realtime(self):
        rt = [entry for entry in self.modules if entry[3]]
        self.assertGreater(len(rt), 0, "至少需要一个实时模块")

    def test_at_least_one_batch(self):
        batch = [entry for entry in self.modules if not entry[3]]
        self.assertGreater(len(batch), 0, "至少需要一个盘后模块")


class TestImportIntegrity(unittest.TestCase):
    """import 完整性 —— 防 NameError 回潮"""

    def test_generate_req_id_imported(self):
        """上次 bug: generate_req_id 只在 main() 局部导入，handler 拿不到"""
        from wecom_server_collector import generate_req_id
        self.assertTrue(callable(generate_req_id))

    def test_critical_functions_exist(self):
        import wecom_server_collector as wsc
        self.assertTrue(callable(wsc.collect_all))
        self.assertTrue(callable(wsc.handle_collect))
        self.assertTrue(callable(wsc.handle_realtime))
        self.assertTrue(callable(wsc.handle_trend))
        self.assertTrue(callable(wsc.parse_stock_code))

    def test_constants_defined(self):
        import wecom_server_collector as wsc
        self.assertIsInstance(wsc.SCRIPTS_DIR, str)
        self.assertIsInstance(wsc.CACHE_DIR, str)


class TestCollectAll(unittest.TestCase):
    """collect_all 内核 —— mock 掉子进程调用"""

    @patch('wecom_server_collector.run_module_and_get_output')
    def test_returns_dict_with_all_module_names(self, mock_run):
        mock_run.return_value = "mock output"

        from wecom_server_collector import collect_all, ALL_MODULES
        results = asyncio.run(collect_all("HK.00700"))

        batch_names = [name for name, *_ in ALL_MODULES if not _[2]]
        self.assertIsInstance(results, dict)
        for name in batch_names:
            self.assertIn(name, results)

    @patch('wecom_server_collector.run_module_and_get_output')
    def test_result_values_match_module_names(self, mock_run):
        mock_run.return_value = "mock output"

        from wecom_server_collector import collect_all
        results = asyncio.run(collect_all("HK.00700"))

        for name, output in results.items():
            self.assertIsInstance(name, str)
            self.assertIsInstance(output, str)


class TestHandleTrendFormatting(unittest.TestCase):
    """趋势数据处理逻辑 —— 不依赖 WS 连接"""

    def test_am_pm_split(self):
        """上/下午数据分割"""
        records = [
            {"time": "10:30", "price": 460.0, "super_in": 1.5, "big_in": 2.0,
             "small_in": 0.5, "ratio": 0.75, "excess": 2.1,
             "volume": 12340000, "turnover": 56.78,
             "buy_str": "1,2,3,4,5", "sell_str": "1,2,3,4,5"},
            {"time": "11:59", "price": 462.0, "super_in": 1.6, "big_in": 2.1,
             "small_in": 0.4, "ratio": 0.80, "excess": 2.2,
             "volume": 23400000, "turnover": 67.89,
             "buy_str": "1,2,3,4,5", "sell_str": "1,2,3,4,5"},
            {"time": "13:01", "price": 461.0, "super_in": 1.4, "big_in": 2.2,
             "small_in": 0.3, "ratio": 0.82, "excess": 2.0,
             "volume": 34500000, "turnover": 78.90,
             "buy_str": "1,2,3,4,5", "sell_str": "1,2,3,4,5"},
            {"time": "15:30", "price": 465.0, "super_in": 1.8, "big_in": 2.5,
             "small_in": 0.2, "ratio": 0.90, "excess": 3.0,
             "volume": 45600000, "turnover": 89.01,
             "buy_str": "1,2,3,4,5", "sell_str": "1,2,3,4,5"},
        ]

        am = [r for r in records if r['time'] < '13:00']
        pm = [r for r in records if r['time'] >= '13:00']

        self.assertEqual(len(am), 2)
        self.assertEqual(len(pm), 2)
        self.assertEqual(am[0]['time'], '10:30')
        self.assertEqual(pm[0]['time'], '13:01')

    def test_format_records_structure(self):
        """format_trend_records 输出包含所有关键字段"""
        from wecom_server_collector import format_trend_records

        records = [{
            "time": "10:30", "price": 460.0,
            "super_in": 1.5, "big_in": 2.0, "mid_in": 0.8, "small_in": 0.5,
            "ratio": 0.75, "excess": 2.1,
            "volume": 12340000, "turnover": 56.78,
            "buy_str": "1,2,3,4,5", "sell_str": "1,2,3,4,5",
        }]

        output = format_trend_records(records, "HK.00700", "Test")

        self.assertIn("[10:30]", output)
        self.assertIn("股价 460.0", output)
        self.assertIn("特大单 1.50", output)
        self.assertIn("大单 2.00", output)
        self.assertIn("中单 0.80", output)
        self.assertIn("小单 0.50", output)
        self.assertIn("买比 0.75", output)
        self.assertIn("超额 2.10", output)
        self.assertIn("量 1234万", output)
        self.assertIn("成交 56.78亿", output)
        self.assertIn("盘口 买5-1: 1,2,3,4,5 | 卖1-5: 1,2,3,4,5", output)

    def test_format_records_missing_fields(self):
        """缺失字段时显示 N/A"""
        from wecom_server_collector import format_trend_records

        records = [{"time": "14:00"}]
        output = format_trend_records(records, "HK.00700", "PM")

        self.assertIn("[14:00]", output)
        self.assertIn("股价 N/A", output)
        self.assertIn("特大单 N/A", output)
        self.assertIn("中单 N/A", output)
        self.assertIn("买比 N/A", output)
        self.assertIn("超额 N/A", output)
        self.assertIn("量 N/A", output)
        self.assertIn("成交 N/A", output)


class TestFetchTrendFromDb(unittest.TestCase):
    """fetch_trend_from_db 映射逻辑 —— mock 掉 DB 连接"""

    def _mock_conn(self, rows):
        """构造一个返回 rows 的 get_conn 上下文管理器（psycopg2 风格）"""
        class FakeCursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self): return rows
        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return FakeCursor()
        return FakeConn()

    def test_empty_returns_not_found(self):
        from unittest.mock import patch
        from wecom_server_collector import fetch_trend_from_db
        with patch('db.get_conn', return_value=self._mock_conn([])):
            res = fetch_trend_from_db("HK.00700", "20260811")
        self.assertFalse(res["found"])
        self.assertEqual(res["am"], [])
        self.assertEqual(res["pm"], [])

    def test_am_pm_split_and_mapping(self):
        """验证 record 字段映射 + 上午(hour<13)/下午(hour>=13) 分段"""
        from unittest.mock import patch
        from datetime import datetime
        from wecom_server_collector import fetch_trend_from_db

        am_time = datetime(2026, 8, 11, 10, 30, 0)
        pm_time = datetime(2026, 8, 11, 14, 5, 0)
        rows = [
            (am_time, 460.0, 1.5, 2.0, 0.8, 0.5, 0.75, 2.1, 12340000, 56.78, "b1,b2", "s1,s2"),
            (pm_time, 465.0, 1.8, 2.5, 0.9, 0.2, 0.90, 3.0, 45600000, 89.01, "b3", "s3"),
        ]
        with patch('db.get_conn', return_value=self._mock_conn(rows)):
            res = fetch_trend_from_db("HK.00700", "20260811")

        self.assertTrue(res["found"])
        self.assertEqual(len(res["am"]), 1)
        self.assertEqual(len(res["pm"]), 1)
        self.assertEqual(res["am"][0]["time"], "10:30")
        self.assertEqual(res["pm"][0]["time"], "14:05")
        self.assertEqual(res["am"][0]["price"], 460.0)
        self.assertEqual(res["am"][0]["super_in"], 1.5)
        self.assertEqual(res["am"][0]["buy_str"], "b1,b2")
        self.assertEqual(res["pm"][0]["turnover"], 89.01)

    def test_none_levels_str_defaults_to_na(self):
        from unittest.mock import patch
        from datetime import datetime
        from wecom_server_collector import fetch_trend_from_db
        rows = [(datetime(2026, 8, 11, 11, 0, 0), 460.0, None, None, None, None, None, None, None, None, None, None)]
        with patch('db.get_conn', return_value=self._mock_conn(rows)):
            res = fetch_trend_from_db("HK.00700", "20260811")
        self.assertEqual(res["am"][0]["buy_str"], "N/A")
        self.assertEqual(res["am"][0]["super_in"], None)


class TestRegexMatching(unittest.TestCase):
    """消息路由正则 —— 防止关键词匹配错误"""

    def test_collect_keywords(self):
        import re
        self.assertTrue(re.search(r"全量", "全量 HK.00700"))
        self.assertTrue(re.search(r"全量", "全量 HK.00700", re.IGNORECASE))
        self.assertTrue(re.search(r"全量", "全量采集"))

    def test_realtime_keywords(self):
        import re
        self.assertTrue(re.search(r"实时", "实时 HK.00700"))

    def test_trend_keywords(self):
        import re
        self.assertTrue(re.search(r"趋势", "趋势 HK.00700"))


class TestOrderSizeFormat(unittest.TestCase):
    """get_realtime_order_size.format_text 复现大小单资金文本（wecom 趋势渲染依赖）"""

    def _data(self):
        return {
            "update_time": "2026-06-11 13:08:15",
            "price": 458.8,
            "large_in": -5.68e8,
            "small_in": 3.21e8,
            "direction": "大单净流出，中小单净流入",
            "super_in_flow": 1.23e8, "super_out_flow": 6.91e8, "super_net": -5.68e8,
            "big_in_flow": 5.00e8, "big_out_flow": 5.00e8, "big_net": 0.0,
            "mid_in_flow": 2.50e8, "mid_out_flow": 1.00e8, "mid_net": 1.50e8,
            "small_in_flow": 3.21e8, "small_out_flow": 1.50e8, "small_net": 1.71e8,
        }

    def test_format_all_values(self):
        from get_realtime_order_size import format_text
        out = format_text(self._data(), "HK.00700", "港元")
        self.assertIn("实时大小单资金 (HK.00700)", out)
        self.assertIn("大单净流入(特大+大): -5.68 亿港元", out)
        self.assertIn("特大单: 流入 1.23 亿港元  流出 6.91 亿港元  净流入 -5.68 亿港元", out)
        self.assertIn("资金方向: 大单净流出，中小单净流入", out)

    def test_format_none(self):
        from get_realtime_order_size import format_text
        out = format_text(None, "HK.00700", "港元")
        self.assertIn("暂无数据", out)


class TestTradeDirectionFormat(unittest.TestCase):
    """get_realtime_trade_direction.format_text 复现主动性买卖盘文本"""

    def _data(self):
        return {
            "update_time": "2026-06-12 13:08:15",
            "price": 458.8,
            "bid_vol": 1234567,
            "ask_vol": 987654,
            "volume": 12345678,
            "turnover": 56.78,
        }

    def test_format_with_volume_turnover(self):
        from get_realtime_trade_direction import format_text
        out = format_text(self._data(), "HK.00700", "港元")
        self.assertIn("实时主动性买卖盘 (HK.00700)", out)
        self.assertIn("最新价: 458.8 港元", out)
        self.assertIn("主动性买盘: 1,234,567 股", out)
        self.assertIn("主动性卖盘: 987,654 股", out)
        self.assertIn("主动买卖比: 1.25", out)
        self.assertIn("成交量: 12,345,678 股", out)
        self.assertIn("成交额: 56.78 亿港元", out)

    def test_format_none(self):
        from get_realtime_trade_direction import format_text
        out = format_text(None, "HK.00700", "港元")
        self.assertIn("暂无数据", out)


if __name__ == '__main__':
    unittest.main()
