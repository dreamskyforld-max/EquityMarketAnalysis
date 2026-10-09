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

    def test_quarter_code(self):
        """季度码 `202603SS`（2 位季度号）→ 该季度**首月** 1 日（与项目季度口径一致）。"""
        self.assertEqual(gmm._nbs_period("202603SS"), D(2026, 7, 1))   # 2026Q3 → 07-01
        self.assertEqual(gmm._nbs_period("201101SS"), D(2011, 1, 1))   # 2011Q1 → 01-01
        self.assertEqual(gmm._nbs_period("202604SS"), D(2026, 10, 1))  # 2026Q4 → 10-01

    def test_invalid(self):
        for bad in (None, "", "2026MM", "202613MM", "202608YY", "abc", "202605SS", "202600SS"):
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

    def test_quarter_must_be_enumerated(self):
        """季度**必须逐期枚举**：实测区间串（`SS/QQ`/对象等写法）一律 500，只有枚举可用。"""
        v = gmm._nbs_dts({"quarterly": True, "dts_from": "2011"})
        self.assertTrue(all(x.endswith("SS") and "-" not in x for x in v),
                        "季度不得用区间串，必须逐期 SS 码")
        self.assertEqual(v[0], "201101SS")
        self.assertEqual(v[3], "201104SS")
        self.assertEqual(len(v) % 4, 0)
        self.assertEqual(v[-1][:4], datetime.date.today().strftime("%Y"))


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


class TestPowerGenLeafSelection(unittest.TestCase):
    """发电量目录下有 6 个**平级**叶子：发电量 / 火力发电量 / 水力发电量 / 核能 / 风力 / 太阳能。

    `leaf_prefix` 必须按**前匹配**（startswith）取「发电量」总量叶子；若用包含匹配（in），
    火力/水力/核能/风力/太阳能 5 个叶子会一起命中 → 把分品种发电量混算成总量。
    """

    SPEC = dict(path=["月度数据", "能源", "能源主要产品产量"],
                leaf_prefix="发电量", indicator="发电量同比增长")

    def setUp(self):
        self.es_cids = []
        orig = (gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata)

        def tree(pid, code="1"):
            return {"": [{"name": "月度数据", "_id": "ROOT"}],
                    "ROOT": [{"name": "能源", "_id": "EN"}],
                    "EN": [{"name": "能源主要产品产量", "_id": "PROD"}],
                    "PROD": [{"name": "原煤", "_id": "COAL"},
                             {"name": "发电量", "_id": "TOTAL"},
                             {"name": "火力发电量", "_id": "THERMAL"},
                             {"name": "水力发电量", "_id": "HYDRO"},
                             {"name": "核能发电量", "_id": "NUCLEAR"},
                             {"name": "太阳能发电量", "_id": "SOLAR"}]}.get(pid, [])

        def indicators(cid):
            return [{"i_showname": "发电量同比增长 (%)", "_id": "Y_" + cid}]

        def esdata(cid, ids, root_id, dts):
            self.es_cids.append(cid)
            return [{"code": "202608MM", "values": [{"value": "-0.8"}]}]

        self._orig = orig
        gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata = tree, indicators, esdata

    def tearDown(self):
        gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata = self._orig

    def test_only_total_leaf(self):
        out = gmm._nbs_fetch(self.SPEC)
        self.assertEqual(self.es_cids, ["TOTAL"],
                         "只能取「发电量」总量叶子，混入火电/水电/风光即为口径事故")
        self.assertEqual(out, {D(2026, 8, 1): -0.8})


class TestIndicatorPrefixStrictness(unittest.TestCase):
    """**指标级**前匹配：同叶子内「货运量_同比增长」是「铁路货运量_同比增长」的子串。

    若用包含匹配（in），铁路/公路/水运/民航 4 个分品种会一起命中 → 被并成一条序列
    （取值随查询顺序变化，静默错数）。实测于「交通运输 → 货物运输量」（20 个指标）与
    「能源 → 能源主要产品产量 → 发电量」同构，故用前匹配并加此测试。
    """

    SPEC = dict(path=["月度数据", "交通运输"], leaf_prefix="货物运输量",
                indicator="货运量_同比增长")

    def setUp(self):
        self.es_ids = []
        self._orig = (gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata)

        def tree(pid, code="1"):
            return {"": [{"name": "月度数据", "_id": "ROOT"}],
                    "ROOT": [{"name": "交通运输", "_id": "TR"}],
                    "TR": [{"name": "货物运输量", "_id": "FRT"},
                           {"name": "全国港口货物吞吐量", "_id": "PORT"}]}.get(pid, [])

        def indicators(cid):
            return [{"i_showname": "货运量_当期值 (万吨)", "_id": "TOTAL_V"},
                    {"i_showname": "货运量_同比增长 (%)", "_id": "TOTAL_YOY"},
                    {"i_showname": "铁路货运量_同比增长 (%)", "_id": "RAIL_YOY"},
                    {"i_showname": "公路货运量_同比增长 (%)", "_id": "ROAD_YOY"},
                    {"i_showname": "水运货运量_同比增长 (%)", "_id": "WATER_YOY"}]

        def esdata(cid, ids, root_id, dts):
            self.es_ids.append(ids)
            return [{"code": "202608MM", "values": [{"value": "0.0"}]}]

        gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata = tree, indicators, esdata

    def tearDown(self):
        gmm._nbs_tree, gmm._nbs_indicators, gmm._nbs_esdata = self._orig

    def test_only_total_indicator(self):
        gmm._nbs_fetch(self.SPEC)
        self.assertEqual(self.es_ids, [["TOTAL_YOY"]],
                         "只能命中「货运量_同比增长」，铁路/公路/水运/民航不得混入")

    def test_unmatched_name_raises(self):
        """指标名前缀写错时必须**报错**，而不是静默返回空序列。"""
        with self.assertRaises(RuntimeError):
            gmm._nbs_fetch(dict(self.SPEC, indicator="不存在的指标名"))


class TestSection31GapsInDb(unittest.TestCase):
    """§3.1 三个缺口的库内校验（需数据库）：PMI 新订单 / 工业企业利润 / M0。"""

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

    def test_pmi_new_orders(self):
        n, mn, mx = self._stat("CN.PMI_NEW_ORDERS")
        self.assertGreaterEqual(n, 250, f"PMI 新订单仅 {n} 条（源 2005-01 起应 ~261 条）")
        self.assertEqual(mn, D(2005, 1, 1))
        self.assertGreater(mx, D(2026, 6, 1))
        # 跨源互证：NBS 新订单分项 vs akshare 总指数（同属 NBS PMI，应高度同步）
        self.cur.execute("""SELECT p.value, m.value FROM regime.macro_series p
                            JOIN regime.macro_series m ON m.period_date = p.period_date
                            WHERE p.series_code='CN.PMI_NEW_ORDERS'
                              AND m.series_code='CN.PMI_MFG'""")
        rows = self.cur.fetchall()
        self.assertGreater(len(rows), 200)
        import statistics
        x = [float(a) for a, _ in rows]
        y = [float(b) for _, b in rows]
        r = statistics.correlation(x, y)
        self.assertGreater(r, 0.90, f"与 PMI 总指数相关仅 {r:.2f}，疑似取错指标")
        self.assertTrue(all(20 < float(a) < 80 for a in x), "PMI 分项值域异常（应围绕 50）")

    def test_industrial_profit_cumulative(self):
        """工业企业利润是**累计口径**：1 月每年缺（1-2 月合并发布）→ 必须按缺失处理。"""
        n, mn, mx = self._stat("CN.INDUSTRIAL_PROFIT_YOY")
        self.assertGreaterEqual(n, 250)
        self.assertEqual(mn, D(2000, 2, 1))
        self.cur.execute("""SELECT count(*) FROM regime.macro_series
                            WHERE series_code='CN.INDUSTRIAL_PROFIT_YOY'
                              AND date_part('month', period_date) = 1""")
        self.assertEqual(self.cur.fetchone()[0], 0, "1 月不应有值（源为 1-2 月合并发布）")

    def test_m0_exists_and_scale(self):
        n, mn, mx = self._stat("CN.M0_YOY")
        self.assertGreaterEqual(n, 200, f"M0 仅 {n} 条（同表 M1/M2 应为 2008-01 起）")
        self.assertGreater(mx, D(2026, 6, 1))
        self.cur.execute("""SELECT min(value), max(value) FROM regime.macro_series
                            WHERE series_code='CN.M0_YOY'""")
        lo, hi = (float(x) for x in self.cur.fetchone())
        self.assertTrue(-30 < lo and hi < 60, f"M0 同比值域异常（含 2009 刺激期高增）：{lo}~{hi}")


class TestPowerGenInDb(unittest.TestCase):
    """库内覆盖（需数据库）：发电量同比必须入库、量纲正确、PIT 不前视。"""

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
                            FROM regime.macro_series WHERE series_code='CN.POWER_GEN_YOY'""")
        n, mn, mx = self.cur.fetchone()
        self.assertGreaterEqual(n, 270, f"发电量仅 {n} 条（源 2000-02 起应 ~280 条）")
        self.assertEqual(mn, D(2000, 2, 1))
        self.assertGreater(mx, D(2026, 6, 1))

    def test_yoy_scale(self):
        """同比 % 口径：实测值域 −9.6 ~ +26.9（2000 起含 2020 疫情坑与 2004 高增）。"""
        self.cur.execute("""SELECT min(value), max(value) FROM regime.macro_series
                            WHERE series_code='CN.POWER_GEN_YOY'""")
        lo, hi = (float(x) for x in self.cur.fetchone())
        self.assertTrue(-40 < lo and hi < 50, f"量纲疑似写错（应为同比%）：{lo}~{hi}")

    def test_pit_not_before_release(self):
        self.cur.execute("""SELECT count(*) FROM regime.macro_series
                            WHERE series_code='CN.POWER_GEN_YOY'
                              AND release_time < period_date + INTERVAL '45 days'""")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_missing_months_are_jan_feb(self):
        """缺口模式回归：NBS 月度口径**1–2 月合并发布**，故缺失月份应几乎全是 1/2 月。

        实测 39 个缺失月 = 1 月 26 + 2 月 12 + 2012-06 一次源侧缺失（97% 为 1/2 月）。
        若未来出现「整年缺失」或「3-12 月成片缺失」，说明采集/解析出了问题，此测试会先报出来。
        """
        import pandas as _pd
        self.cur.execute("""SELECT period_date FROM regime.macro_series
                            WHERE series_code='CN.POWER_GEN_YOY' ORDER BY 1""")
        have = {_pd.Timestamp(r[0]) for r in self.cur.fetchall()}
        full = _pd.date_range(min(have), max(have), freq="MS")
        miss = [d for d in full if d not in have]
        self.assertGreater(len(miss), 20, "缺口数异常偏少 → 可能取到的是累计/别的口径")
        jan_feb = sum(1 for d in miss if d.month in (1, 2))
        self.assertGreaterEqual(jan_feb / len(miss), 0.90,
                                f"缺失月份中 1/2 月占比仅 {jan_feb}/{len(miss)}，疑似成片缺月")

    def test_sign_agreement_with_electricity(self):
        """跨源交叉验证：发电量（规上、供给侧）与用电量（全社会、需求侧）**符号一致率 ≥ 80%**
        （实测 226 个重叠月为 88%）。

        不校验相等/高相关：二者口径不同（规上发电量不含分布式光伏与自备电厂），
        实测 2026-08 背离 5.1pp、近 60 月相关仅 0.47 —— 若强行要求高相关会把**真实口径差**
        误判为数据错误。故只挡「方向性错乱」。
        """
        self.cur.execute("""SELECT p.value > 0, e.value > 0
                            FROM regime.macro_series p
                            JOIN regime.macro_series e ON e.period_date = p.period_date
                            WHERE p.series_code='CN.POWER_GEN_YOY'
                              AND e.series_code='CN.ELECTRICITY_YOY'""")
        rows = self.cur.fetchall()
        self.assertGreater(len(rows), 200, "两序列重叠月份过少")
        agree = sum(1 for a, b in rows if a == b) / len(rows)
        self.assertGreaterEqual(agree, 0.80, f"与用电量的符号一致率仅 {agree:.2f}，疑似取错叶子或量纲")


if __name__ == "__main__":
    unittest.main(verbosity=2)
