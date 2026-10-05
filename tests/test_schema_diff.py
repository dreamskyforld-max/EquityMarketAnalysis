"""sql/_schema_diff.py 的回归测试（纯解析 + 纯函数，不连库、不碰数据库）。

背景（2026-10-05）: 目标库 trading_calendar 缺主键 + 三列（market/cal_date/
is_open）缺 NOT NULL，脚本能**检出**差异，但产出的"修复 SQL"却是空的 ——
原因是旧版生成侧只认独立 CREATE INDEX 语句（内联 PRIMARY KEY 无此语句 → 静默跳过），
且完全没比对 nullable。

本测试锁住修复后的行为：
  1. schema.sql 的内联 PRIMARY KEY / NOT NULL / SERIAL 能被正确解析；
  2. 「服务器态」（缺主键 + 全 nullable）能产出正确的 ALTER 修复 SQL（不为空）；
  3. 一致态零差异（不误报）；
  4. 库比 schema 更严格（多余 NOT NULL）→ 仅提示、不生成 SQL；
  5. 新建表 DDL 带 NOT NULL 与 PK/UNIQUE 约束；
  6. 无法生成 SQL 的差异必须显式 [!] 提示，绝不静默。
"""
import importlib.util
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 模块用 APP_DIR 定位 schema.sql；指到本仓库，测试才能用真实 schema.sql
os.environ.setdefault("APP_DIR", ROOT)


def _load_mod():
    """加载 sql/_schema_diff.py（下划线开头、不在包内，用 importlib 直载）。"""
    spec = importlib.util.spec_from_file_location(
        "_schema_diff_under_test", os.path.join(ROOT, "sql", "_schema_diff.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load_mod()


def _read_schema():
    with open(os.path.join(ROOT, "sql", "schema.sql"), encoding="utf-8") as f:
        return f.read()


class TestParseInlineConstraints(unittest.TestCase):
    """解析侧: 内联 PRIMARY KEY / NOT NULL / SERIAL"""

    @classmethod
    def setUpClass(cls):
        cls.schema_text = _read_schema()
        cls.tables, cls.indexes, cls.views, cls.views_raw = sd.parse_schema(cls.schema_text)

    def test_pk_constraint_is_parsed_with_cols(self):
        """内联 PRIMARY KEY 必须带出 kind 与列清单（生成 ADD CONSTRAINT 要用）"""
        info = self.indexes["trading_calendar_pkey"]
        self.assertEqual(info["tbl"], "trading_calendar")
        self.assertEqual(info["constraint"]["kind"], "PRIMARY KEY")
        self.assertEqual(info["constraint"]["cols"], ["market", "cal_date"])

    def test_notnull_flags(self):
        nn = self.tables["trading_calendar"]["notnull"]
        self.assertEqual(nn, {"market": True, "cal_date": True, "is_open": True,
                              "src": False, "updated_at": False})

    def test_pk_columns_count_as_not_null(self):
        """表级 PRIMARY KEY (a, b) 的列在 PG 里隐含 NOT NULL"""
        checked = 0
        for info in self.indexes.values():
            con = info.get("constraint")
            if not con or con["kind"] != "PRIMARY KEY":
                continue
            t = info["tbl"]
            if t not in self.tables:
                continue
            for c in con["cols"]:
                checked += 1
                with self.subTest(f"{t}.{c}"):
                    self.assertTrue(self.tables[t]["notnull"].get(c),
                                    f"{t}.{c} 是主键列，期望 notnull=True")
        self.assertGreater(checked, 0, "schema.sql 里应有表级主键可供校验")

    def test_serial_columns_count_as_not_null(self):
        """SERIAL 家族隐含 NOT NULL —— 不识别会把「库中有 NOT NULL」误报成多余约束"""
        checked = 0
        for t, info in self.tables.items():
            for col, ty in (info.get("raw_cols") or {}).items():
                if re.match(r"(?:small|big)?serial\b", ty.strip(), re.I):
                    checked += 1
                    with self.subTest(f"{t}.{col}"):
                        self.assertTrue(info["notnull"].get(col),
                                        f"{t}.{col} 是 SERIAL，期望 notnull=True")
        self.assertGreater(checked, 0, "schema.sql 里应有 SERIAL 列可供校验")


class TestServerStateFixSql(unittest.TestCase):
    """核心回归: 「服务器态」→ 修复 SQL 必须非空且正确（本次事故形态）"""

    @classmethod
    def setUpClass(cls):
        cls.schema_text = _read_schema()
        exp_t, exp_i, _, _ = sd.parse_schema(cls.schema_text)
        # 只取 trading_calendar 一张表，聚焦本次事故场景
        cls.exp_t = {"trading_calendar": exp_t["trading_calendar"]}
        cls.exp_i = {k: v for k, v in exp_i.items() if v["tbl"] == "trading_calendar"}

    def _server_state(self):
        """构造「服务器态」: 表在、列类型一致，但无任何索引/约束、全部列可空。"""
        exp = self.exp_t["trading_calendar"]
        act_t = {"trading_calendar": {
            "cols": dict(exp["cols"]),
            "order": list(exp["order"]),
            "notnull": {c: False for c in exp["cols"]},
        }}
        return act_t, {}

    def test_diff_detects_missing_pk_and_notnull(self):
        act_t, act_i = self._server_state()
        d = sd.diff_schema(self.exp_t, self.exp_i, act_t, act_i)
        self.assertEqual(d["new_tables"], [])
        self.assertEqual(d["new_indexes"], ["trading_calendar_pkey"])
        self.assertEqual(sorted(d["set_not_null"]),
                         [("trading_calendar", "cal_date"),
                          ("trading_calendar", "is_open"),
                          ("trading_calendar", "market")])
        self.assertEqual(d["loosen_not_null"], [])

    def test_fix_sql_is_not_empty(self):
        """关键回归: 修复 SQL 不许为空（旧版在内联主键上静默跳过）"""
        act_t, act_i = self._server_state()
        d = sd.diff_schema(self.exp_t, self.exp_i, act_t, act_i)
        lines = sd.build_low_risk_sql(self.schema_text, self.exp_t, self.exp_i, d)
        sql = "\n".join(lines)
        self.assertTrue(sql.strip(), "修复 SQL 不能为空")
        self.assertIn('ALTER TABLE "trading_calendar" ADD CONSTRAINT '
                      '"trading_calendar_pkey" PRIMARY KEY ("market", "cal_date");', sql)
        for col in ("market", "cal_date", "is_open"):
            self.assertIn(
                f'ALTER TABLE "trading_calendar" ALTER COLUMN "{col}" SET NOT NULL;', sql)
        self.assertNotIn("-- [!]", sql)   # 有约束元信息可还原，不该走"无法生成"提示

    def test_consistent_state_produces_no_diff(self):
        """一致态（有主键、三列 NOT NULL）→ 零差异、零 SQL（不误报）"""
        exp = self.exp_t["trading_calendar"]
        act_t = {"trading_calendar": {
            "cols": dict(exp["cols"]),
            "order": list(exp["order"]),
            "notnull": dict(exp["notnull"]),
        }}
        act_i = {"trading_calendar_pkey": {
            "tbl": "trading_calendar",
            "sig": self.exp_i["trading_calendar_pkey"]["sig"],
        }}
        d = sd.diff_schema(self.exp_t, self.exp_i, act_t, act_i)
        for key in ("new_tables", "add_cols", "alter_cols", "drop_cols",
                    "set_not_null", "loosen_not_null", "new_indexes", "drop_indexes"):
            with self.subTest(key=key):
                self.assertEqual(d[key], [])
        self.assertEqual(
            sd.build_low_risk_sql(self.schema_text, self.exp_t, self.exp_i, d), [])

    def test_looser_than_schema_is_hint_only(self):
        """库比 schema 更严格（多余 NOT NULL）→ 仅提示，不生成 SQL"""
        exp = self.exp_t["trading_calendar"]
        act_t = {"trading_calendar": {
            "cols": dict(exp["cols"]),
            "order": list(exp["order"]),
            "notnull": {c: True for c in exp["cols"]},   # src/updated_at 也 NOT NULL
        }}
        act_i = {"trading_calendar_pkey": {
            "tbl": "trading_calendar",
            "sig": self.exp_i["trading_calendar_pkey"]["sig"],
        }}
        d = sd.diff_schema(self.exp_t, self.exp_i, act_t, act_i)
        self.assertEqual(d["set_not_null"], [])
        self.assertEqual(sorted(d["loosen_not_null"]),
                         [("trading_calendar", "src"),
                          ("trading_calendar", "updated_at")])
        # 放宽约束不得悄悄出现在修复 SQL 里
        sql = "\n".join(sd.build_low_risk_sql(self.schema_text, self.exp_t, self.exp_i, d))
        self.assertNotIn("DROP NOT NULL", sql)


class TestDdlGeneration(unittest.TestCase):
    """建表 DDL 完整性 + 兜底提示"""

    @classmethod
    def setUpClass(cls):
        cls.schema_text = _read_schema()
        cls.tables, cls.indexes, _, _ = sd.parse_schema(cls.schema_text)

    def test_create_table_includes_notnull_and_pk(self):
        """旧版只输出「列名 + 类型」，建出的表仍缺约束"""
        t = "trading_calendar"
        info = self.tables[t]
        cons = [(name, c["constraint"]["kind"], c["constraint"]["cols"])
                for name, c in self.indexes.items()
                if c["tbl"] == t and c.get("constraint")]
        ddl = sd.gen_ddl_create_table(t, info, cons)
        self.assertIn('"market" VARCHAR(8) NOT NULL', ddl)
        self.assertIn('"is_open" BOOLEAN NOT NULL', ddl)
        self.assertIn('"src" VARCHAR(40)', ddl)
        self.assertNotIn('"src" VARCHAR(40) NOT NULL', ddl)
        self.assertIn('CONSTRAINT "trading_calendar_pkey" PRIMARY KEY '
                      '("market", "cal_date")', ddl)

    def test_unresolvable_index_leaves_explicit_hint(self):
        """期望索引既无独立 CREATE INDEX、又无约束元信息 → 必须显式 [!]，绝不静默"""
        exp_i = {"mystery_idx": {"tbl": "trading_calendar", "sig": None}}
        d = {"new_tables": [], "add_cols": [], "set_not_null": [],
             "new_indexes": ["mystery_idx"], "new_views": [], "changed_views": []}
        sql = "\n".join(sd.build_low_risk_sql(
            self.schema_text, {"trading_calendar": self.tables["trading_calendar"]},
            exp_i, d))
        self.assertIn("-- [!]", sql)
        self.assertIn("mystery_idx", sql)

    def test_regular_index_ddl_extracted_from_schema_text(self):
        """普通 CREATE INDEX 仍走原文提取路径（不回归成 ADD CONSTRAINT）"""
        # 挑一个 schema.sql 里真实存在的独立索引
        idx = next((n for n, info in self.indexes.items()
                    if not info.get("constraint")
                    and re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+"
                                  r"(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(n),
                                  self.schema_text, re.I)), None)
        self.assertIsNotNone(idx, "schema.sql 里应有独立 CREATE INDEX 语句可供校验")
        ddl = sd.extract_index_ddl(self.schema_text, idx)
        self.assertTrue(ddl and "CREATE" in ddl.upper())
        d = {"new_tables": [], "add_cols": [], "set_not_null": [],
             "new_indexes": [idx], "new_views": [], "changed_views": []}
        self.assertIn(ddl, sd.build_low_risk_sql(
            self.schema_text, {"trading_calendar": self.tables["trading_calendar"]},
            self.indexes, d))


if __name__ == "__main__":
    unittest.main()
