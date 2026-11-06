"""L3 业务指标日频接线防回退测试（不连库）。

锁住 2026-11-06 的四件事：
  ① `market_state_daily.run()` 在 L2 `build_all_dimensions()` **之后** 调用
     `build_all_business()`（L3 读 DIM.TREND.score / DIM.BREADTH.state，顺序不能反）；
  ② `stats["business"]` 有返回值、`write` / `markets` 正确透传；
  ③ L2 失败时 L3 必须跳过（l2_ok 守卫），不得用上一轮维度读数继续算；
  ④ `monitor_table_config` 里 business_value 用 `trade_date` 判新鲜度且 active=TRUE
     （该表 only_if_changed=True，updated_at 不变，用 updated_at 会误报断供）。
"""
import datetime
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import market_state_daily as msd  # noqa: E402

D = datetime.date


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fake_modules(l2_fails=False):
    l2 = types.ModuleType("build_dimension_value")
    l2.build_all_dimensions = mock.Mock(
        return_value={"CN": {"rows": 1, "state_rows": 0, "dims": {}}})
    if l2_fails:
        l2.build_all_dimensions.side_effect = RuntimeError("L2 boom")
    l3 = types.ModuleType("build_business_value")
    l3.build_all_business = mock.Mock(
        return_value={"CN": {"BIZ.MARKET_REGIME": {"rows": 7990, "switches": 111}}})
    return l2, l3


def _run(l2, l3):
    conn = mock.MagicMock()
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    with mock.patch.object(msd, "get_conn", return_value=ctx), \
            mock.patch.object(msd, "_ensure_dict_schema"), \
            mock.patch.object(msd, "_ensure_dict", return_value=0), \
            mock.patch.object(msd, "calc_cn", return_value={}), \
            mock.patch.dict(sys.modules, {"build_dimension_value": l2,
                                          "build_business_value": l3}):
        return msd.run(market="CN", start=D(2026, 11, 1),
                       end=D(2026, 11, 3), write=False)


class TestSourceWiring(unittest.TestCase):

    def test_01_l3_after_l2_with_guard(self):
        src = _read("market_state_daily.py")
        self.assertIn("import build_business_value", src)
        self.assertIn('stats["business"]', src)
        self.assertIn("l2_ok", src)
        self.assertLess(src.index("build_all_dimensions("),
                        src.index("build_all_business("),
                        "L3 必须在 L2 维度层之后调用")

    def test_04_monitor_time_column(self):
        src = _read("monitor_collector.py")
        self.assertIn('"business_value", "trade_date", "day"', src)
        self.assertIn("'dimension_dict','business_value'", src)


class TestRunBehaviour(unittest.TestCase):

    def test_02_l3_called_after_l2(self):
        l2, l3 = _fake_modules()
        stats = _run(l2, l3)
        self.assertTrue(l2.build_all_dimensions.called)
        self.assertTrue(l3.build_all_business.called)
        self.assertIn("business", stats)
        self.assertEqual(stats["business"]["CN"]["BIZ.MARKET_REGIME"]["rows"], 7990)
        _, kwargs = l3.build_all_business.call_args
        self.assertEqual(kwargs["markets"], ["CN"])
        self.assertFalse(kwargs["write"])

    def test_03_l2_failure_skips_l3(self):
        l2, l3 = _fake_modules(l2_fails=True)
        stats = _run(l2, l3)
        self.assertTrue(l2.build_all_dimensions.called)
        self.assertFalse(l3.build_all_business.called,
                         "L2 失败时不得用陈旧维度读数算 L3")
        self.assertNotIn("business", stats)


if __name__ == "__main__":
    unittest.main()
