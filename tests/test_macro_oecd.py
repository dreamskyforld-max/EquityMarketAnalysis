"""OECD SDMX（复合领先指标 CLI）采集测试。

用户给的入口 `https://data-explorer.oecd.org/vis?df[id]=DSD_STES%40DF_CLI...` 是可视化页，
真正可编程的是背后的 **OECD SDMX REST**（`sdmx.oecd.org/public/rest/data/...`）：无鉴权、无 WAF。
本文件锁住三件事：

  ① **一次请求取全部经济体**（7 个 ref_area 共用一次 HTTP，靠 cache 复用）——
     否则会退化成「每个序列各发一次请求」；
  ② **PIT 基准是月初**：`lag` 从 period_date（当月 1 日）起算，而 OECD 上月 CLI 的发布在
     **次月初**（≈ 月初+37 天）。曾误设 lag=15（当作「月末+8 天」）→ 等于把数据提前约 3 周
     视为可得，构成**前视**，此处用测试钉死（release_time − period_date ≥ 45 天）；
  ③ 空值/非数值观测（OBS_STATUS=M）必须跳过，不能污染序列。
"""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import get_macro_monthly as gmm

D = datetime.date

CSV = ("DATAFLOW,REF_AREA,FREQ,MEASURE,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
       "x,CHN,M,LI,2026-09,100.4786,A\n"
       "x,CHN,M,LI,2026-08,100.545,A\n"
       "x,CHN,M,LI,2026-07,,M\n"          # 缺失观测 → 必须跳过
       "x,USA,M,LI,2026-09,99.5,A\n"
       "x,G20,M,LI,2026-09,100.1,A\n")


class FakeResp:
    status_code = 200
    text = CSV

    def raise_for_status(self):
        pass


class TestSdmxCli(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_get(url, **kw):
            self.calls.append((url, kw.get("params")))
            return FakeResp()

        self._p = mock.patch("requests.get", side_effect=fake_get)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_one_request_for_all_areas(self):
        """7 个序列共用一次请求（第二次调用直接命中 cache）。"""
        cache = {}
        a = gmm._sdmx_cli({"ref_area": "CHN"}, cache)
        b = gmm._sdmx_cli({"ref_area": "USA"}, cache)
        self.assertEqual(len(self.calls), 1, "同一轮内应只发一次请求")
        self.assertEqual(a, {D(2026, 9, 1): 100.4786, D(2026, 8, 1): 100.545})
        self.assertEqual(b, {D(2026, 9, 1): 99.5})

    def test_area_union_in_url(self):
        cache = {}
        gmm._sdmx_cli({"ref_area": "CHN"}, cache)
        url = self.calls[0][0]
        for area in ("CHN", "USA", "JPN", "DEU", "KOR", "G7", "G20"):
            self.assertIn(area, url.split("/")[-1], "取数范围应覆盖 SERIES 里全部 CLI 经济体")

    def test_missing_obs_skipped(self):
        cache = {}
        gmm._sdmx_cli({"ref_area": "CHN"}, cache)
        self.assertNotIn(D(2026, 7, 1), cache[gmm._SDMX_KEY]["CHN"], "空观测不得入库")

    def test_unknown_area_returns_empty(self):
        self.assertEqual(gmm._sdmx_cli({"ref_area": "ARG"}, {}), {})

    def test_market_from_spec_or_prefix(self):
        """`_mk_row` 的 market 取 defn.market，缺省回落到 code 前缀（CLI 引入了 US/JP/DE 等）。"""
        row = gmm._mk_row({"code": "US.CLI", "name": "x", "unit": "index",
                           "freq": "month", "lag": 45}, D(2026, 9, 1), 99.5)
        self.assertEqual(row["market"], "US")
        row2 = gmm._mk_row({"code": "GLOBAL.CLI_G20", "name": "x", "unit": "index",
                            "freq": "month", "lag": 45}, D(2026, 9, 1), 100.1)
        self.assertEqual(row2["market"], "GLOBAL")
        row3 = gmm._mk_row({"code": "CN.CPI_YOY", "name": "x", "unit": "pct",
                            "freq": "month", "lag": 45}, D(2026, 9, 1), 0.8)
        self.assertEqual(row3["market"], "CN", "既有 CN 序列的 market 必须保持 CN")


class TestCliInDb(unittest.TestCase):
    """库内覆盖（需数据库）：CLI 必须真入库、量纲正确、且 release_time 不许前视。"""

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

    def _stat(self, code):
        self.cur.execute("""SELECT count(*), min(period_date), max(period_date)
                            FROM regime.macro_series WHERE series_code=%s""", (code,))
        return self.cur.fetchone()

    def test_cn_cli_coverage(self):
        n, mn, mx = self._stat("CN.CLI")
        self.assertGreaterEqual(n, 400, f"CN.CLI 仅 {n} 条（源 1992-05 起应 ~413 条）")
        self.assertEqual(mn, D(1992, 5, 1))
        self.assertGreater(mx, D(2026, 6, 1))

    def test_all_areas_present(self):
        self.cur.execute("""SELECT count(*) FROM regime.macro_series
                            WHERE series_code IN ('CN.CLI','US.CLI','JP.CLI','DE.CLI','KR.CLI',
                                                  'GLOBAL.CLI_G7','GLOBAL.CLI_G20')""")
        self.assertGreaterEqual(self.cur.fetchone()[0], 3600, "7 个经济体应共约 3659 个月度观测")

    def test_index_scale(self):
        """CLI 为振幅调整指数（100=长期趋势）：实测 1992 起 85.6~106.4
        （低点 2020 疫情 / 高点 2007 过热）→ 用宽区间挡「量纲写错」（如原始点数/百分数）。"""
        self.cur.execute("""SELECT min(value), max(value) FROM regime.macro_series
                            WHERE series_code='CN.CLI'""")
        lo, hi = (float(x) for x in self.cur.fetchone())
        self.assertTrue(70 < lo < 100 < hi < 130, f"CN.CLI 值域异常: {lo}~{hi}")

    def test_pit_not_before_release(self):
        """PIT 回归：release_time 必须晚于 period_date 至少 45 天（防「次月初发布」被当成月初可得）。"""
        self.cur.execute("""SELECT count(*) FROM regime.macro_series
                            WHERE series_code LIKE '%.CLI%'
                              AND release_time < period_date + INTERVAL '45 days'""")
        self.assertEqual(self.cur.fetchone()[0], 0, "存在前视：lag 基准写错（月初 vs 月末）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
