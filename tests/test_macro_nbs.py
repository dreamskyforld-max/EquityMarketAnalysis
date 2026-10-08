"""国家统计局「国家数据」直连（核心 CPI）解析与采集测试。

背景：此前项目记录过「核心 CPI 免费源不可得」——**该结论是错的**。错因是只试过 akshare
`macro_china_nbs_nation`（走老接口 easyquery.htm，被 WAF 403），未试官网新版
`/dg/website/**` 新接口。本文件锁住三个实测踩到的坑：

  ① **期间区间必须显式给**：`dts=""` 时接口只返回默认最近 9 个月（核心 CPI 只回来 17 条、
     首段被截断）→ 必须传 `["YYYYMM MM-YYYYMM MM"]` 形式的显式区间（实测 27 年跨度可一次返回）；
  ② **同一指标被切成多个时段目录**：(2026-)/(2021-2025)/(2016-2020)/(-2015)，各段指标 id 不同、
     早期段可能**没有该指标**（核心 CPI 只有 2021 起的两段有）→ 必须遍历全部匹配叶子并合并；
  ③ **口径换算**：源为指数型（上年同月=100），入库统一为同比（指数 − 100），
     否则与 CN.CPI_YOY（已是 %）混用会得出荒谬的差值。
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import get_macro_monthly as gmm

D = datetime.date


class TestNbsPeriod(unittest.TestCase):
    def test_month_code(self):
        self.assertEqual(gmm._nbs_period("202608MM"), D(2026, 8, 1))
        self.assertEqual(gmm._nbs_period("202101MM"), D(2021, 1, 1))

    def test_invalid(self):
        for bad in (None, "", "2026MM", "202613MM", "202608YY", "abc"):
            self.assertIsNone(gmm._nbs_period(bad), f"{bad!r} 应解析失败")


class TestNbsDts(unittest.TestCase):
    """坑 ①：区间必须显式给定，否则只返回默认最近 9 个月。"""

    def test_explicit_range(self):
        v = gmm._nbs_dts({})
        self.assertEqual(len(v), 1)
        self.assertRegex(v[0], r"^20\d{4}MM-20\d{4}MM$")

    def test_range_override(self):
        v = gmm._nbs_dts({"dts_from": "202101"})[0]
        self.assertTrue(v.startswith("202101MM-"))
        self.assertTrue(v.endswith(datetime.date.today().strftime("%Y%m") + "MM"))


class TestNbsFetch(unittest.TestCase):
    """用假接口替换四个 HTTP 函数，验证目录下钻 / 多段合并 / 换算 / 空值处理。"""

    SPEC = {
        "path": ["月度数据", "价格指数", "居民消费价格分类指数 (上年同月=100)"],
        "leaf_prefix": "全国居民消费价格分类指数",
        "indicator": "不包括食品和能源", "minus": 100,
    }

    def setUp(self):
        self._orig = (gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata)
        self.calls = []

        def tree(pid, code="1"):
            if pid == "":
                return [{"name": "月度数据", "_id": "ROOT"}]
            if pid == "ROOT":
                return [{"name": "价格指数", "_id": "P1"}]
            if pid == "P1":
                return [{"name": "居民消费价格分类指数 (上年同月=100)", "_id": "P2"}]
            if pid == "P2":   # 4 个时段叶子 + 1 个不匹配的叶子
                return [{"name": "全国居民消费价格分类指数 (上年同月=100) (2026-)", "_id": "SEG26"},
                        {"name": "全国居民消费价格分类指数 (上年同月=100) (2021-2025)", "_id": "SEG21"},
                        {"name": "全国居民消费价格分类指数 (上年同月=100) (2016-2020)", "_id": "SEG16"},
                        {"name": "全国食品类居民消费价格指数 (上年同月=100)", "_id": "OTHER"}]
            return []

        def indicators(cid):
            self.calls.append(("ind", cid))
            if cid == "SEG16":      # 早期段无核心 CPI（源库结构如此）
                return [{"i_showname": "居民消费价格指数 (上年同月=100)", "_id": "x1"}]
            return [{"i_showname": "居民消费价格指数 (上年同月=100)", "_id": "x0"},
                    {"i_showname": "不包括食品和能源居民消费价格指数 (上年同月=100)", "_id": "CORE_" + cid}]

        def esdata(cid, ids, root_id, dts):
            self.calls.append(("es", cid, ids, root_id, dts))
            if cid == "SEG26":
                return [{"code": "202608MM", "values": [{"value": "101.0"}]},
                        {"code": "202607MM", "values": [{"value": ""}]}]        # 未公布 → 跳过
            return [{"code": "202112MM", "values": [{"value": "101.2"}]},
                    {"code": "202101MM", "values": [{"value": "99.7"}]}]

        gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata = tree, indicators, esdata

    def tearDown(self):
        gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata = self._orig

    def test_merge_segments_and_convert(self):
        out = gmm._nbs_fetch(self.SPEC)
        # 两段合并 + 指数−100 换算 + 空值剔除
        self.assertEqual(out, {D(2021, 1, 1): -0.3, D(2021, 12, 1): 1.2, D(2026, 8, 1): 1.0})
        # 不匹配的叶子（食品类）与无该指标的时段（2016-2020）都不得触发取数
        es_cids = [c[1] for c in self.calls if c[0] == "es"]
        self.assertEqual(sorted(es_cids), ["SEG21", "SEG26"])

    def test_root_id_taken_from_first_level(self):
        gmm._nbs_fetch(self.SPEC)
        root = {c[3] for c in self.calls if c[0] == "es"}
        self.assertEqual(root, {"ROOT"}, "esData 的 rootId 应取「月度数据」节点 id，不能硬编码")

    def test_dts_is_explicit_range(self):
        gmm._nbs_fetch(self.SPEC)
        for c in [c for c in self.calls if c[0] == "es"]:
            self.assertTrue(c[4] and c[4][0].endswith("MM"), "必须传显式期间区间（空串只有 9 个月）")

    def test_missing_path_raises(self):
        bad = dict(self.SPEC, path=["月度数据", "不存在的目录"])
        with self.assertRaises(RuntimeError):
            gmm._nbs_fetch(bad)


class TestCoreCpiInDb(unittest.TestCase):
    """库内覆盖（需数据库）：核心 CPI 必须真有数据且量纲为同比 %。"""

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

    def test_coverage(self):
        self.cur.execute("""SELECT count(*), min(period_date), max(period_date)
                            FROM regime.macro_series WHERE series_code='CN.CORE_CPI_YOY'""")
        n, mn, mx = self.cur.fetchone()
        self.assertGreaterEqual(n, 60, f"核心 CPI 仅 {n} 条 → 时段目录漏采（应 2021 起 ~68 条）")
        self.assertEqual(mn, D(2021, 1, 1), "起点应为 2021-01（更早时段库无该口径）")
        self.assertGreater(mx, D(2026, 6, 1))

    def test_unit_is_yoy_pct(self):
        """量纲回归：源是指数型（上年同月=100），入库必须是同比 %（−100），否则与 CN.CPI_YOY 不可比。"""
        self.cur.execute("""SELECT value FROM regime.macro_series
                            WHERE series_code='CN.CORE_CPI_YOY' ORDER BY period_date""")
        vals = [float(r[0]) for r in self.cur.fetchall()]
        self.assertTrue(all(-10 < v < 20 for v in vals), "比值域超出常识 → 疑似忘了 −100")
        self.assertAlmostEqual(vals[0], -0.3, places=1)      # 2021-01 核心 CPI 同比 99.7 → −0.3%

    def test_no_month_gap(self):
        """逐月连续：行数应等于月份跨度（防「时段目录只取到一段」的静默缺段）。"""
        self.cur.execute("""SELECT count(*), min(period_date), max(period_date)
                            FROM regime.macro_series WHERE series_code='CN.CORE_CPI_YOY'""")
        n, mn, mx = self.cur.fetchone()
        months = (mx.year - mn.year) * 12 + (mx.month - mn.month) + 1
        self.assertEqual(n, months, f"行数 {n} ≠ 月份跨度 {months} → 中间缺段")

    def test_pit_not_before_release(self):
        """PIT：release_time 不得早于统计期（本源无发布时间字段，按 period+lag 保守估计）。"""
        self.cur.execute("""SELECT count(*) FROM regime.macro_series
                            WHERE series_code='CN.CORE_CPI_YOY' AND release_time <= period_date""")
        self.assertEqual(self.cur.fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
