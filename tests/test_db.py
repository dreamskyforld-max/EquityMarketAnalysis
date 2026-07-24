"""
db.py 测试
覆盖：get_conn 连接、upsert 写入/冲突更新、bulk_upsert 批量写入
（需要本地数据库可用）
"""
import sys, os, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


if __name__ == '__main__':
    unittest.main()
