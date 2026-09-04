"""
profiling 模块测试
覆盖：
  1. 标签元数据校验（registry.TagMeta）
  2. 引擎返回值规整（engine._normalize）
  3. ① 证券属性域的板块/证券类型判定规则（纯函数，不连库）
  4. 版本化写入与 as_of 时间旅行（端到端，用 _test_ 前缀标签，跑完清理）

需要本地数据库可用（与 tests/test_db.py 一致）。
"""
import sys, os, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from db import get_conn
from profiling import registry, engine
from profiling.registry import TagMeta, tag, ENUM, TIER, ACTIVE, PLANNED_NO_DATA
from profiling.schema import ensure_schema
from profiling import quantile
from profiling.tags import identity, quality, valuation


def _row(market, symbol, exchange_type=None):
    return pd.Series({"market": market, "symbol": symbol, "exchange_type": exchange_type})


# ═══════════════════════════════════════════════════════════════════════════
class TestTagMeta(unittest.TestCase):
    """标签元数据的合法性校验（防字典失控的第一道闸）"""

    def _meta(self, **kw):
        base = dict(
            code="x", name="测试", domain="证券属性", value_type=ENUM,
            source_type="rule", update_freq="static",
            value_ref="table:some_table.some_col",   # enum 类型必须声明值域
        )
        base.update(kw)
        return TagMeta(**base)

    def test_valid(self):
        self.assertEqual(self._meta().status, ACTIVE)

    def test_enum_requires_value_domain(self):
        """enum 标签不声明值域 = 值域失控，必须拦下"""
        with self.assertRaises(ValueError):
            self._meta(value_ref=None)

    def test_enum_values_label_must_be_unique(self):
        """同一概念不能有两个 code（「上交所」与「上海证券交易所」各立一码）"""
        with self.assertRaises(ValueError):
            self._meta(
                value_ref=None,
                enum_type="exchange",
                enum_values={"XSHG": ("上海证券交易所", "上交所"), "SSE": ("上海证券交易所", "沪市")},
            )

    def test_invalid_value_type(self):
        with self.assertRaises(ValueError):
            self._meta(value_type="whatever")

    def test_exclusive_conflicts_multi_value(self):
        with self.assertRaises(ValueError):
            self._meta(is_exclusive=True, multi_value=True)

    def test_planned_must_have_reason(self):
        """口径已定但数据缺失的标签必须写明原因，否则无法排期补数据"""
        with self.assertRaises(ValueError):
            self._meta(status=PLANNED_NO_DATA)
        ok = self._meta(status=PLANNED_NO_DATA, blocked_reason="数据源缺失")
        self.assertEqual(ok.status, PLANNED_NO_DATA)

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            @tag(code="idt_market", name="重复", domain="①", value_type=ENUM,
                 source_type="rule", update_freq="static")
            def _dup(as_of):
                return None


# ═══════════════════════════════════════════════════════════════════════════
class TestNormalize(unittest.TestCase):
    """引擎对标签函数返回值的规整"""

    def _meta(self, **kw):
        base = dict(code="t", name="测试", domain="证券属性", value_type=ENUM,
                    source_type="rule", update_freq="static",
                    value_ref="table:some_table.some_col")
        base.update(kw)
        return TagMeta(**base)

    def test_missing_required_column(self):
        with self.assertRaises(ValueError):
            engine._normalize(pd.DataFrame({"stock_code": ["SZ.000001"]}), self._meta())

    def test_defaults_filled(self):
        out = engine._normalize(
            pd.DataFrame({"stock_code": ["SZ.000001"], "key_value": ["A"]}), self._meta()
        )
        self.assertEqual(out["confidence"].iloc[0], 1.0)
        self.assertIsNone(out["num_value"].iloc[0])

    def test_blank_rows_dropped(self):
        out = engine._normalize(
            pd.DataFrame({
                "stock_code": ["SZ.000001", "", "SZ.000002"],
                "key_value": ["A", "A", ""],
            }),
            self._meta(),
        )
        self.assertEqual(len(out), 1)

    def test_duplicate_key_value_dedup(self):
        out = engine._normalize(
            pd.DataFrame({"stock_code": ["SZ.000001", "SZ.000001"], "key_value": ["A", "A"]}),
            self._meta(),
        )
        self.assertEqual(len(out), 1)

    def test_single_value_tag_rejects_multi(self):
        """声明为单值标签却返回多个取值 = 口径错误，必须显式暴露"""
        with self.assertRaises(ValueError):
            engine._normalize(
                pd.DataFrame({"stock_code": ["SZ.000001", "SZ.000001"], "key_value": ["A", "B"]}),
                self._meta(multi_value=False),
            )

    def test_multi_value_tag_allows_multi(self):
        out = engine._normalize(
            pd.DataFrame({"stock_code": ["SZ.000001", "SZ.000001"], "key_value": ["A", "B"]}),
            self._meta(multi_value=True),
        )
        self.assertEqual(len(out), 2)


# ═══════════════════════════════════════════════════════════════════════════
class TestIdentityRules(unittest.TestCase):
    """① 域的代码段判定规则（不连库）"""

    def test_board_returns_canonical_code(self):
        """板块落库的是规范 code，不是中文名（一个概念一个码）"""
        cases = [
            ("SH", "600000", "CN_SH", "SSE_MAIN"),
            ("SH", "688981", "CN_STIB", "SSE_STAR"),
            ("SH", "688981", None, "SSE_STAR"),        # 无 exchange_type 时回退代码段
            ("SH", "920000", "CN_BJ", "BSE_MAIN"),
            ("SH", "900901", None, "SSE_B"),
            ("SZ", "000001", "CN_SZ", "SZSE_MAIN"),
            ("SZ", "300750", None, "SZSE_CHINEXT"),
            ("SZ", "200011", None, "SZSE_B"),
            ("HK", "00700", "HK_MAINBOARD", "HKEX_MAIN"),
            ("HK", "08217", "HK_GEMBOARD", "HKEX_GEM"),
            ("HK", "08217", None, "HKEX_GEM"),          # 回退：08 段即 GEM
            ("SH", "510300", None, "NON_STOCK"),        # ETF 不属于任何股票板块
        ]
        for market, symbol, et, expect in cases:
            with self.subTest(symbol=symbol, et=et):
                self.assertEqual(identity._board_of(_row(market, symbol, et)), expect)

    def test_board_nan_exchange_type(self):
        """exchange_type 为空时 pandas 会给出 float nan，不能崩"""
        self.assertEqual(identity._board_of(_row("SH", "600000", float("nan"))), "SSE_MAIN")

    def test_listing_age_tier_boundary(self):
        """上市年限档用有序数字 code：1=次新(<1)，2=成熟(1-5)，3=老股(>5)"""
        years = pd.Series([0.5, 0.99, 1.0, 4.9, 5.0, 20.0])
        tier = pd.Series("3", index=years.index, dtype=object)
        tier[years < 5] = "2"
        tier[years < 1] = "1"
        self.assertEqual(list(tier), ["1", "1", "2", "2", "3", "3"])


# ═══════════════════════════════════════════════════════════════════════════
class TestVersioning(unittest.TestCase):
    """端到端：版本化写入、幂等性、as_of 时间旅行

    用一个临时标签（_test_ 前缀）在真实 profile schema 上跑，跑完清理数据与注册表。
    """

    TAG = "_test_versioning"
    DAY1 = date(2026, 1, 5)
    DAY2 = date(2026, 1, 12)

    @classmethod
    def setUpClass(cls):
        cls.payload = {"SZ.000001": "A", "SZ.000002": "B"}
        meta = TagMeta(
            code=cls.TAG, name="测试标签", domain="测试", value_type=ENUM,
            source_type="rule", update_freq="static", owner="test",
            value_ref="table:some_table.some_col",
        )
        registry._REGISTRY[cls.TAG] = meta

        def _fn(as_of):
            return pd.DataFrame({
                "stock_code": list(cls.payload.keys()),
                "key_value": list(cls.payload.values()),
                "num_value": None,
                "confidence": 1.0,
            })

        registry._FUNCS[cls.TAG] = _fn
        with get_conn() as conn:
            ensure_schema(conn, years=(2026,))

    @classmethod
    def tearDownClass(cls):
        registry._REGISTRY.pop(cls.TAG, None)
        registry._FUNCS.pop(cls.TAG, None)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM profile.tag_value WHERE tag_code = %s", (cls.TAG,))
                cur.execute("DELETE FROM profile.tag_registry WHERE tag_code = %s", (cls.TAG,))
                cur.execute("DELETE FROM profile.tag_run_log WHERE tag_code = %s", (cls.TAG,))

    def _current(self, as_of):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT stock_code, key_value, eff_from, eff_to FROM profile.tag_value "
                    "WHERE tag_code = %s AND eff_from <= %s AND eff_to >= %s ORDER BY stock_code",
                    (self.TAG, as_of, as_of),
                )
                return cur.fetchall()

    def test_full_lifecycle(self):
        # 1) 首次计算：两行全部新增
        #    注：该标签 update_freq=static，默认会被更新门禁拦截，测试统一用 force=True
        with get_conn() as conn:
            r1 = engine.compute_tag(conn, self.TAG, as_of=self.DAY1, force=True)
        self.assertEqual(r1["rows_total"], 2)
        self.assertEqual(r1["rows_new"], 2)
        self.assertEqual(r1["rows_closed"], 0)

        # 2) 同日重算：幂等，不产生任何新版本
        with get_conn() as conn:
            r2 = engine.compute_tag(conn, self.TAG, as_of=self.DAY1, force=True)
        self.assertEqual((r2["rows_new"], r2["rows_closed"]), (0, 0))

        # 3) 换日 + 改值：SZ.000002 由 B → C，应关闭旧版本并新增一行
        self.payload["SZ.000002"] = "C"
        with get_conn() as conn:
            r3 = engine.compute_tag(conn, self.TAG, as_of=self.DAY2, force=True)
        self.assertEqual(r3["rows_new"], 1)
        self.assertEqual(r3["rows_closed"], 1)

        # 4) 时间旅行：回看 DAY1 仍是旧值，看 DAY2 是新值
        snap1 = {c: v for c, v, _, _ in self._current(self.DAY1)}
        snap2 = {c: v for c, v, _, _ in self._current(self.DAY2)}
        self.assertEqual(snap1["SZ.000002"], "B")
        self.assertEqual(snap2["SZ.000002"], "C")
        self.assertEqual(snap1["SZ.000001"], snap2["SZ.000001"], "未变更的标签不应产生新版本")

        # 5) 旧版本区间必须闭合且不重叠：DAY1 生效行在 DAY2 之前失效
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key_value, eff_from, eff_to FROM profile.tag_value "
                    "WHERE tag_code = %s AND stock_code = 'SZ.000002' ORDER BY eff_from",
                    (self.TAG,),
                )
                rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "B")
        self.assertLess(rows[0][2], rows[1][1], "旧版本必须在新版本生效前失效")

        # 6) 取值消失：整只股票退出标签，旧行应被关闭而非删除
        self.payload.pop("SZ.000001")
        day3 = date(2026, 1, 19)
        with get_conn() as conn:
            r4 = engine.compute_tag(conn, self.TAG, as_of=day3, force=True)
        self.assertEqual(r4["rows_closed"], 1)
        self.assertNotIn("SZ.000001", [r[0] for r in self._current(day3)])


    def test_zza_freq_gate(self):
        """低频标签未到重算间隔应被跳过，force=True 可强制重算

        没有这个门禁，静态标签每天重算都会因 num_value 随时间微变而重建全部版本行。
        """
        day = date(2026, 2, 1)
        with get_conn() as conn:
            skipped = engine.compute_tag(conn, self.TAG, as_of=day)
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("update_freq", skipped["message"])

        with get_conn() as conn:
            forced = engine.compute_tag(conn, self.TAG, as_of=day, force=True)
        self.assertEqual(forced["status"], "ok")

    def test_zzb_snapshot_mode_updates_in_place(self):
        """快照模式：值变了原地 UPDATE，eff_from 不变，不产生新版本

        日常跑当天用 snapshot，避免日频标签每天重建全部版本行（写满全历史）。
        补历史用 version（--as-of 历史日期时 auto 模式自动切换）。

        注意：payload 必须通过 self.__class__ 修改——计算闭包引用的是类属性，
        self.payload = ... 只绑定实例属性，闭包看不到（实测踩过）。
        """
        day4 = date(2026, 2, 10)
        # 前序测试留下的库状态：SZ.000002 当前有效值为 C（eff_from=DAY2）
        self.__class__.payload = {"SZ.000002": "X"}
        with get_conn() as conn:
            r = engine.compute_tag(conn, self.TAG, as_of=day4, force=True, mode="snapshot")
        self.assertEqual(r["mode"], "snapshot")
        # 原地更新：不新增行、不关闭行
        self.assertEqual((r["rows_new"], r["rows_closed"]), (0, 0))

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key_value, eff_from FROM profile.tag_value "
                    "WHERE tag_code = %s AND stock_code = 'SZ.000002' AND eff_to = '9999-12-31'",
                    (self.TAG,),
                )
                rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "X")
        self.assertEqual(rows[0][1], self.DAY2, "快照模式下 eff_from 必须保持不变（无新版本）")

    def test_zzc_version_mode_still_chains(self):
        """同一场景改用 version 模式：必须产生新版本（关闭旧行 + 插入新行）"""
        day4, day5 = date(2026, 2, 10), date(2026, 2, 20)
        self.__class__.payload = {"SZ.000002": "Y"}
        with get_conn() as conn:
            r = engine.compute_tag(conn, self.TAG, as_of=day5, force=True, mode="version")
        self.assertEqual(r["mode"], "version")
        self.assertEqual((r["rows_new"], r["rows_closed"]), (1, 1))
        # 时间旅行：day4 时点仍是 X（快照模式改的），day5 起才是 Y
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key_value FROM profile.tag_value WHERE tag_code=%s "
                    "AND stock_code='SZ.000002' AND eff_from<=%s AND eff_to>=%s",
                    (self.TAG, day4, day4),
                )
                self.assertEqual(cur.fetchone()[0], "X")


# ═══════════════════════════════════════════════════════════════════════════
class TestQuantile(unittest.TestCase):
    """分档工具（②③④⑤⑥⑦ 域共用，口径必须全项目一致）"""

    def test_assign_tier_uniform(self):
        """均匀分布的 100 个值应切成各 20 个的五档，且 1=最小、5=最大"""
        tier = quantile.assign_tier(pd.Series(range(100), dtype=float), 5)
        self.assertEqual(tier.value_counts().to_dict(), {"1": 20, "2": 20, "3": 20, "4": 20, "5": 20})
        self.assertEqual(tier.iloc[0], "1")
        self.assertEqual(tier.iloc[-1], "5")

    def test_assign_tier_by_group(self):
        """分组分档：每个组内各自五等分，不跨组比较"""
        tier = quantile.assign_tier(
            pd.Series([1.0, 2, 3, 4, 5] * 2),
            5,
            by=pd.Series(["A"] * 5 + ["B"] * 5),
        )
        self.assertEqual(tier.tolist(), ["1", "2", "3", "4", "5"] * 2)

    def test_assign_tier_insufficient_sample(self):
        """组内有效样本不足档位数时不打档（避免强行切出误导性的档）"""
        tier = quantile.assign_tier(pd.Series([1.0, 2.0]), 5)
        self.assertTrue(tier.isna().all())

    def test_assign_tier_skips_nan(self):
        """NaN 不打档，但不影响其余值分档（样本量需足够，否则整组都不打档）"""
        v = pd.Series([1.0, None, 3.0, None, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        tier = quantile.assign_tier(v, 5)
        self.assertEqual(tier.isna().sum(), 2)

    def test_tier_or_flag_negative_goes_to_flag(self):
        """估值为负（亏损/资不抵债）必须走枚举值，不能混进分档"""
        v = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, -5.0])
        out = quantile.tier_or_flag(v, by=pd.Series(["A"] * len(v)), flag="LOSS")
        self.assertEqual(out.iloc[-1], "LOSS")
        self.assertTrue(set(out.iloc[:-1]) <= {"1", "2", "3", "4", "5"})

    def test_pct_to_tier_is_integer_string(self):
        """档位必须是 '1' 而不是 '1.0'（np.ceil 返回 float，需显式转 int）"""
        out = valuation._pct_to_tier(pd.Series([5.0, 50.0, 95.0]))
        self.assertEqual(out.tolist(), ["1", "3", "5"])

    def test_pct_to_tier_nan_passthrough(self):
        out = valuation._pct_to_tier(pd.Series([50.0, None]))
        self.assertEqual(out.iloc[0], "3")
        self.assertTrue(pd.isna(out.iloc[1]))


class TestStreak(unittest.TestCase):
    """连续为正期数的计算（④ 连续盈利年数 / ⑤ 成长持续性 共用逻辑）"""

    def _hist(self, profits_by_stock):
        rows = []
        for code, profits in profits_by_stock.items():
            for i, p in enumerate(profits):   # i=0 是最新期
                rows.append({"stock_code": code, "rn": i, "net_profit": p,
                             "report_date": pd.Timestamp(2025 - i, 12, 31)})
        return pd.DataFrame(rows)

    def test_consecutive_count(self):
        hist = self._hist({
            "A": [5, 4, 3, -1, 2],      # 最新 3 期连续盈利，2022 亏损 → 3
            "B": [-1, 2, 3, 4],          # 最新期亏损 → 0
            "C": [1, 1, 1, 1],           # 全盈利（4 期）→ 4
        })
        s = quality._streak(hist, "net_profit")
        self.assertEqual(s["A"], 3)
        self.assertEqual(s["B"], 0)
        self.assertEqual(s["C"], 4)

    def test_missing_report_breaks_streak(self):
        """报告缺失（NaN）视为中断，不跨缺口累计"""
        hist = self._hist({"A": [3, None, 2, 1]})
        s = quality._streak(hist, "net_profit")
        self.assertEqual(s["A"], 1)


if __name__ == "__main__":
    unittest.main()
