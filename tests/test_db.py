"""
db.py 测试
覆盖：get_conn 连接、upsert 写入/冲突更新、bulk_upsert 批量写入
（需要本地数据库可用）；read_sql 统一读库入口（不连库）
"""
import re
import sys, os, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from db import get_conn, upsert, bulk_upsert

TEST_TABLE = "_test_upsert"


class TestDatabaseConnection(unittest.TestCase):
    """数据库连接基础测试"""

    def test_connect_and_ping(self):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            self.assertEqual(row[0], 1)

    def test_autocommit_off(self):
        """验证 connect 返回的连接不会自动提交"""
        with get_conn() as conn:
            self.assertEqual(conn.autocommit, False)


class TestUpsert(unittest.TestCase):
    """upsert 写入逻辑"""

    @classmethod
    def setUpClass(cls):
        """创建测试表（只跑一次）"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TEST_TABLE} (
                    id SERIAL PRIMARY KEY,
                    stock_code VARCHAR(20) NOT NULL,
                    trade_date DATE NOT NULL,
                    last_price NUMERIC,
                    volume BIGINT,
                    UNIQUE(stock_code, trade_date)
                )
            """)

    @classmethod
    def tearDownClass(cls):
        """清理测试数据"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {TEST_TABLE}")

    def test_insert_new_record(self):
        data = {
            "stock_code": "HK.00001",
            "trade_date": "2026-06-10",
            "last_price": 100.5,
            "volume": 1000000,
        }
        with get_conn() as conn:
            upsert(conn, TEST_TABLE, data, conflict_cols=["stock_code", "trade_date"])

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT last_price, volume FROM {TEST_TABLE} WHERE stock_code='HK.00001'")
            row = cur.fetchone()
            self.assertEqual(float(row[0]), 100.5)
            self.assertEqual(int(row[1]), 1000000)

    def test_upsert_updates_existing(self):
        data = {
            "stock_code": "HK.00001",
            "trade_date": "2026-06-10",
            "last_price": 105.0,
            "volume": 2000000,
        }
        with get_conn() as conn:
            upsert(conn, TEST_TABLE, data, conflict_cols=["stock_code", "trade_date"])

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT last_price, volume FROM {TEST_TABLE} WHERE stock_code='HK.00001'")
            row = cur.fetchone()
            self.assertEqual(float(row[0]), 105.0, "upsert 未更新价格")
            self.assertEqual(int(row[1]), 2000000, "upsert 未更新成交量")

    def test_upsert_no_duplicate_rows(self):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {TEST_TABLE} WHERE stock_code='HK.00001' AND trade_date='2026-06-10'")
            cnt = cur.fetchone()[0]
            self.assertEqual(cnt, 1, "upsert 产生了重复行")


class TestBulkUpsert(unittest.TestCase):
    """bulk_upsert 批量写入"""

    @classmethod
    def setUpClass(cls):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TEST_TABLE} (
                    id SERIAL PRIMARY KEY,
                    stock_code VARCHAR(20) NOT NULL,
                    trade_date DATE NOT NULL,
                    last_price NUMERIC,
                    volume BIGINT,
                    UNIQUE(stock_code, trade_date)
                )
            """)

    @classmethod
    def tearDownClass(cls):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {TEST_TABLE}")

    def test_bulk_insert_multiple(self):
        data_list = [
            {"stock_code": "HK.00002", "trade_date": "2026-06-08", "last_price": 50.0, "volume": 500},
            {"stock_code": "HK.00002", "trade_date": "2026-06-09", "last_price": 51.0, "volume": 600},
            {"stock_code": "HK.00002", "trade_date": "2026-06-10", "last_price": 52.0, "volume": 700},
        ]
        with get_conn() as conn:
            bulk_upsert(conn, TEST_TABLE, data_list, conflict_cols=["stock_code", "trade_date"])

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {TEST_TABLE} WHERE stock_code='HK.00002'")
            cnt = cur.fetchone()[0]
            self.assertEqual(cnt, 3, f"预期 3 行，实际 {cnt}")

    def test_bulk_insert_empty_list(self):
        """空列表不应抛异常"""
        try:
            with get_conn() as conn:
                bulk_upsert(conn, TEST_TABLE, [], conflict_cols=["stock_code", "trade_date"])
        except Exception as e:
            self.fail(f"空列表 bulk_upsert 失败: {e}")

    def test_bulk_upsert_updates_existing(self):
        """批量 upsert 应更新已有记录"""
        data_list = [
            {"stock_code": "HK.00002", "trade_date": "2026-06-10", "last_price": 99.0, "volume": 999},
        ]
        with get_conn() as conn:
            bulk_upsert(conn, TEST_TABLE, data_list, conflict_cols=["stock_code", "trade_date"])

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT last_price FROM {TEST_TABLE} "
                        f"WHERE stock_code='HK.00002' AND trade_date='2026-06-10'")
            row = cur.fetchone()
            self.assertEqual(float(row[0]), 99.0)


class TestReadSqlUnified(unittest.TestCase):
    """只读查询统一走 db.read_sql（纯逻辑，不连库）。

    背景（2026-11-04）：pandas 对「非 SQLAlchemy、非 sqlite3 的 DBAPI2 连接」发
    UserWarning，且带 stacklevel 指向每个调用点 → 不会按"同一处只报一次"去重，
    回填时按调用点各刷一条，淹没真实输出。全项目收口到 db.read_sql 统一屏蔽。
    """

    MSG = ("pandas only supports SQLAlchemy connectable (engine/connection) or "
           "database string URI or sqlite3 DBAPI2 connection.")

    def test_suppresses_pandas_connectable_warning(self):
        import warnings as W
        import pandas as pd

        def fake(*a, **k):
            W.warn(self.MSG, UserWarning, stacklevel=2)
            return "ok"

        with mock.patch.object(pd, "read_sql_query", side_effect=fake):
            with W.catch_warnings(record=True) as rec:
                W.simplefilter("always")
                self.assertEqual(db.read_sql(object(), "SELECT 1"), "ok")
        self.assertFalse([w for w in rec if "SQLAlchemy connectable" in str(w.message)])

    def test_other_warnings_still_propagate(self):
        """只屏蔽那一条消息，不能把 pandas / 用户的其它警告一并吞掉。"""
        import warnings as W
        import pandas as pd

        def fake(*a, **k):
            W.warn("some other warning", UserWarning, stacklevel=2)
            return "ok"

        with mock.patch.object(pd, "read_sql_query", side_effect=fake):
            with W.catch_warnings(record=True) as rec:
                W.simplefilter("always")
                db.read_sql(object(), "SELECT 1")
        self.assertTrue([w for w in rec if "some other" in str(w.message)])

    def test_no_direct_pandas_read_sql_outside_db(self):
        """禁止绕过 db.read_sql 直接用 pd.read_sql*（否则警告按调用点刷屏）。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           {".git", ".venv", "__pycache__", "node_modules", ".memory"}]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root)
                if rel == "db.py":
                    continue
                with open(p, encoding="utf-8", errors="ignore") as f:
                    if re.search(r"pd\.read_sql(_query)?\(", f.read()):
                        offenders.append(rel)
        self.assertEqual(offenders, [], f"这些文件绕过了 db.read_sql：{offenders}")


if __name__ == '__main__':
    unittest.main()
