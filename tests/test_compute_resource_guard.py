"""防回退钉子：计算任务的内存/资源红线载体必须存在。

背景（2026-09 ~ 10 的事故链，细则见 doc/compute_task_resource_standard.md）：
画像计算与市场状态计算都曾因"一次性把百万行拉进进程"把整机拖死
（09-29 5.5h、09-30 8.4h 内存-IO 雪崩；10-05 在 market_state_daily 拦下同款
888MB 峰值）。本测试锁住修复点的关键特征——分批参数、dtype 瘦身、独立单元的
资源上限、规范文档——防止后续重构无声地改回一次性加载。

它不测业务正确性（那是各处自己的测试），只测"红线结构还在"。
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestBatchLoadingGuards(unittest.TestCase):
    """分批取数 / dtype 瘦身（规范 §4.1~§4.3）"""

    def test_stream_tool_uses_server_side_cursor(self):
        src = _read("profiling/tags/_base.py")
        self.assertIn("def _read_sql_stream", src)
        self.assertIn("conn.cursor(name=", src)   # 服务端游标（不是 fetchall）
        self.assertIn("fetchmany", src)
        self.assertIn("def _distinct_codes", src)

    def test_technical_batched_and_slimmed(self):
        src = _read("profiling/tags/technical.py")
        self.assertIn("_BATCH_CODES", src)
        self.assertIn("_read_sql_stream(", src)
        self.assertIn('"category"', src)
        self.assertIn('"float32"', src)
        self.assertRegex(src, r"for i in range\(0, len\(codes\), _BATCH_CODES\)")

    def test_trend_batched(self):
        src = _read("profiling/tags/trend.py")
        self.assertIn("_BATCH_CODES", src)
        self.assertRegex(src, r"for i in range\(0, len\(codes\), _BATCH_CODES\)")
        self.assertIn("ORDER BY stock_code, trade_date", src)

    def test_market_state_windows_batched(self):
        """_per_stock_windows 曾一次加载 ~200 万行（实测 888MB），必须保持分批"""
        src = _read("market_state_daily.py")
        self.assertIn("_WINDOW_BATCH_CODES", src)
        self.assertRegex(src, r"for i in range\(0, len\(codes\), _WINDOW_BATCH_CODES\)")
        self.assertIn("stock_code = ANY(%s)", src)   # 批次查询按股票限定


class TestSchedulerIsolation(unittest.TestCase):
    """重计算任务不得回到常驻调度进程（规范 §2 R4/R5）"""

    def test_profile_task_not_in_scheduler(self):
        src = _read("market_scheduler.py")
        self.assertNotIn('("全量画像计算", "compute_profile.py"', src)

    def test_oneshot_unit_has_resource_limits(self):
        src = _read("system/compute-profile.service")
        for key in ("MemoryMax=", "MemorySwapMax=0", "RuntimeMaxSec=",
                    "Nice=", "IOSchedulingPriority=", "OOMScoreAdjust="):
            with self.subTest(key=key):
                self.assertIn(key, src)


class TestStandardCarriersExist(unittest.TestCase):
    """规范载体（文档 + AGENTS.md 挂钩 + 自查工具）"""

    def test_standard_doc_exists_and_hooked_in_agents(self):
        self.assertTrue(
            os.path.exists(os.path.join(ROOT, "doc", "compute_task_resource_standard.md")),
            "计算任务资源规范文档缺失")
        self.assertIn("计算任务资源约定", _read("AGENTS.md"))

    def test_scan_tool_exists(self):
        self.assertTrue(
            os.path.exists(os.path.join(ROOT, "tools", "scan_big_table_loads.py")),
            "大表全量加载自查工具缺失")


if __name__ == "__main__":
    unittest.main()
