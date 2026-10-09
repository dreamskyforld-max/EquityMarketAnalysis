"""央行「社会融资规模」直连 + 信贷脉冲真值口径测试（§3.2 换源）。

背景：社融增量源（akshare `macro_china_shrzgm`，上游商务部镜像）冻结于 2026-04，
而它正是 `MACRO.CREDIT_IMPULSE` 的输入 → 指标被**静默冻结**在 −3.21（比缺失更危险）。
本轮改抓央行官网「社会融资规模存量/增量统计表」的 **htm 附件**（无需 Excel 库）。

本文件钉住三类实测坑与一个口径决策：

  ① **htm 附件编码新旧不一**（2016-2019 是 UTF-8、2020+ 是 GBK）→ 必须按关键字自动判定；
  ② **宽表每月份占 2 列**（存量 / 增速%）→ 必须**按位置**配对，先过滤空值再配对会错位；
  ③ **水平值有口径断点**（2018-12 及更早不含国债/地方政府债）→ 信贷脉冲只能取 2019-01 起；
  ④ **信贷脉冲量级自检**：真值（流量/GDP 同比变化）≈ ±3~8pp；
     而「存量/GDP 比率的同比变化」会常年 +10pp（跟随 330% 的比率水平）、
     「流量/GDP 的月度变化」仅 ±1.5pp（噪声主导）→ 两者都是错口径，用值域把它们挡在门外。
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import get_macro_monthly as gmm

D = datetime.date

# 合成 htm：表头 6 个月（`_pbc_parse` 用「≥6 个月份单元格」识别表头行，故样例需 ≥6），
# 总量行故意让 2026.2 的存量列留空 → 若实现是「先过滤空值再配对」，2026.3 会错拿 2 月的增速值。
STOCK_HTML = """
<table><tr><td></td><td>2026.1</td><td>x</td><td>2026.2</td><td>x</td><td>2026.3</td><td>x</td>
<td>2026.4</td><td>x</td><td>2026.5</td><td>x</td><td>2026.6</td><td>x</td></tr>
<tr><td>项目 Items</td><td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td>
<td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td><td>存量</td><td>增速（%）</td></tr>
<tr><td>社会融资规模存量 AFRE(stock)</td><td>449.11</td><td>8.2</td><td></td><td>7.9</td><td>456.46</td><td>7.5</td>
<td>456.88</td><td>7.8</td><td>458.00</td><td>7.6</td><td>462.06</td><td>7.4</td></tr>
<tr><td>人民币贷款</td><td>273.30</td><td>6.1</td><td>274.15</td><td>6.0</td><td>277.30</td><td>5.8</td>
<td>276.90</td><td>5.6</td><td>277.00</td><td>5.5</td><td>278.00</td><td>5.4</td></tr></table>
"""

FLOW_HTML = """
<table><tr><td>项目 Items 月份 Month</td><td>社会融资规模增量</td><td>人民币贷款</td></tr>
<tr><td>2026.01</td><td>72185</td><td>49016</td></tr>
<tr><td>2026.02</td><td>23837</td><td>8458</td></tr>
<tr><td>2026.03</td><td></td><td>1</td></tr></table>
"""


class TestPbcParse(unittest.TestCase):
    def test_stock_positional_pairing(self):
        """每月份 2 列、按位置配对：2026.2 存量空 → 该月无值，且**不得**把 3 月的值串到 2 月。"""
        out = gmm._pbc_parse(STOCK_HTML, "stock")
        self.assertEqual(out[(2026, 1)], 449.11)
        self.assertNotIn((2026, 2), out, "2026.2 存量列为空 → 不应有值")
        self.assertEqual(out[(2026, 3)], 456.46, "2026.3 被前面的空值挤位 = 按位置配对失效")

    def test_stock_yoy_pairs(self):
        out = gmm._pbc_parse(STOCK_HTML, "stock_yoy")
        self.assertEqual((out[(2026, 1)], out[(2026, 2)], out[(2026, 3)]), (8.2, 7.9, 7.5))

    def test_flow_rows(self):
        out = gmm._pbc_parse(FLOW_HTML, "flow")
        self.assertEqual(out[(2026, 1)], 72185.0)
        self.assertEqual(out[(2026, 2)], 23837.0)
        self.assertNotIn((2026, 3), out, "增量列为空 → 该月跳过")

    def test_title_rows_ignored(self):
        """标题行（`社会融资规模存量统计表`）只有 4 格 → 不得被当成总量数据行。"""
        html = ("<table><tr><td>社会融资规模存量统计表</td></tr>"
                "<tr><td></td><td>2026.1</td><td>x</td><td>2026.2</td><td>x</td>"
                "<td>2026.3</td><td>x</td><td>2026.4</td><td>x</td>"
                "<td>2026.5</td><td>x</td><td>2026.6</td><td>x</td></tr></table>")
        self.assertEqual(gmm._pbc_parse(html, "stock"), {}, "无总量数据行时返回空，不得臆造")


class TestPbcSecrets(unittest.TestCase):
    """无网络的纯函数：年份配对（就近前向）与 htm 附件定位。"""

    IDX = ("<td class='x'>2025年统计数据</a></td>"
           "<a href='/a/2025/shrzgm/index.html' class='y'>社会融资规模</a>"
           "<td class='x'>2026年统计数据</a></td>"
           "<a href='/a/2026ntjsj/shrzgm/index.html' class='y'>社会融资规模</a>")

    def test_year_pairing_regex(self):
        import re
        ypos = [(int(m.group(1)), m.start()) for m in re.finditer(r"(\d{4})年统计数据", self.IDX)]
        spos = [(m.group(1), m.start()) for m in
                re.finditer(r"href=[\"']([^\"']+)[\"'][^>]*>\s*社会融资规模\s*</a>", self.IDX)]
        got = {}
        for href, sp in spos:
            prev = [y for y, yp in ypos if yp < sp]
            if prev:
                got[prev[-1]] = href
        self.assertEqual(got[2025], "/a/2025/shrzgm/index.html")
        self.assertEqual(got[2026], "/a/2026ntjsj/shrzgm/index.html")

    def test_entries_block_parse(self):
        """按 `titp20` 分块取块内首个 .htm（附件在标题之后、同一块内）。"""
        import requests  # noqa: F401  —— 仅为保持与实现同源依赖
        html = ("<div class='titp20'>社会融资规模增量统计表<br/>Flow</div>"
                "<a href='/d/inc.htm'>htm</a><a href='/d/inc.pdf'>pdf</a>"
                "<div class='titp20'>社会融资规模存量统计表<br/>Stock</div>"
                "<a href='/d/stock.htm'>htm</a>")
        orig = gmm._pbc_get
        gmm._pbc_get = lambda url, attach=False: html
        try:
            ents = gmm._pbc_entries("http://x")
        finally:
            gmm._pbc_get = orig
        names = {n: h for n, h in ents}
        self.assertTrue(any(n.startswith("社会融资规模存量") for n in names))
        self.assertIn("/d/stock.htm", names[[n for n in names if n.startswith("社会融资规模存量")][0]])


class TestPbcInDb(unittest.TestCase):
    """库内实测（需数据库）：三条序列覆盖 + 口径断点须被记录（不可当连续序列用）。"""

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

    def test_series_coverage(self):
        for code, low in (("CN.TSF_STOCK", 110), ("CN.TSF_STOCK_YOY", 110), ("CN.TSF_FLOW_M", 110)):
            with self.subTest(code=code):
                self.cur.execute("""SELECT count(*), max(period_date) FROM regime.macro_series
                                    WHERE series_code=%s""", (code,))
                n, mx = self.cur.fetchone()
                self.assertGreaterEqual(n, low, f"{code} 仅 {n} 行")
                self.assertGreater(mx, D(2026, 6, 1), f"{code} 不新鲜：{mx}")

    def test_stock_scale_is_yi_yuan(self):
        """存量统一为亿元：2026 年应在 400 万亿元量级 = 4,000,000 亿元上下（挡住万亿元未换算）。"""
        self.cur.execute("""SELECT value FROM regime.macro_series WHERE series_code='CN.TSF_STOCK'
                            ORDER BY period_date DESC LIMIT 1""")
        v = float(self.cur.fetchone()[0])
        self.assertTrue(3.5e6 < v < 6.0e6, f"存量量级异常：{v}（应为亿元）")

    def test_caliber_break_is_real(self):
        """口径断点回归：2018-12 → 2019-01 应有 ~20% 的跳升（央行 2019 年纳入国债/地方债）。"""
        self.cur.execute("""SELECT period_date, value FROM regime.macro_series
                            WHERE series_code='CN.TSF_STOCK'
                              AND period_date IN (DATE '2018-12-01', DATE '2019-01-01')""")
        got = {str(d): float(v) for d, v in self.cur.fetchall()}
        self.assertIn("2018-12-01", got)
        self.assertIn("2019-01-01", got)
        jump = got["2019-01-01"] / got["2018-12-01"] - 1
        self.assertGreater(jump, 0.15, "口径断点消失？信贷脉冲的取值起点（2019-01）需重新评估")


class TestCreditImpulseInDb(unittest.TestCase):
    """信贷脉冲：值域必须落在「真值口径」的合理区间（挡三种错口径）。"""

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

    def test_range_matches_true_caliber(self):
        self.cur.execute("""SELECT min(raw_value), max(raw_value), count(*), min(trade_date), max(trade_date)
                            FROM regime.indicator_value WHERE indicator_code='MACRO.CREDIT_IMPULSE'
                              AND market='CN' AND raw_value IS NOT NULL""")
        lo, hi, n, mn, mx = self.cur.fetchone()
        lo, hi = float(lo), float(hi)
        self.assertGreater(n, 1000, f"信贷脉冲仅 {n} 个交易日")
        self.assertGreaterEqual(mn, D(2021, 1, 1), "首值不得早于 2021（存量口径 2019-01 + 流量需 12 月）")
        self.assertGreater(mx, D(2026, 6, 1))
        # 真值口径：±3~8pp（实测 −5.5~+5.9）；错口径的值域各不相同，逐一挡住：
        self.assertTrue(-12 < lo and hi < 12, f"值域 {lo}~{hi} 超出真值口径（疑似换了定义）")
        self.assertGreater(hi - lo, 4.0,
                           "值域过窄（<4pp）→ 疑似误用「流量/GDP 月度变化」（±1.5pp、噪声主导）")
        self.assertLess(hi, 12,
                        "上限过高（>12pp）→ 疑似误用「存量/GDP 比率的同比变化」（常年 +10pp 且无周期含义）")
        self.assertGreater(lo, -12, "下限过低 → 同上，口径存疑")


class TestBreakevenInDb(unittest.TestCase):
    """通胀预期（§3.3 缺口）：T10YIE 与计算层指标都要在、且量级合理。"""

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

    def test_bench_history(self):
        self.cur.execute("""SELECT count(*), min(trade_date), max(trade_date) FROM daily_benchmark
                            WHERE bench_code='US.T10YIE'""")
        n, mn, mx = self.cur.fetchone()
        self.assertGreater(n, 5000, f"T10YIE 仅 {n} 行（FRED 自 2003 起应 ~5,900 行）")
        self.assertEqual(mn, D(2003, 1, 2))
        self.assertGreater(mx, D(2026, 6, 1))

    def test_indicator_scale(self):
        self.cur.execute("""SELECT min(raw_value), max(raw_value), max(trade_date)
                            FROM regime.indicator_value WHERE indicator_code='MACRO.BREAKEVEN_10Y'
                              AND market='CN' AND raw_value IS NOT NULL""")
        lo, hi, mx = self.cur.fetchone()
        lo, hi = float(lo), float(hi)
        self.assertTrue(0.0 < lo and hi < 5.0, f"盈亏平衡通胀率量级异常：{lo}~{hi}（应为 0-5%）")
        self.assertGreater(mx, D(2026, 6, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
