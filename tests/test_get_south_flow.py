"""
get_south_flow.py 测试
覆盖：入库后窗口重算（recalc_from_db）差值逻辑 + backfill_south_flow 参数解析
（recalc_from_db 需要本地 PG 可用，同 test_db.py 模式）
"""
import sys, os, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn
from get_south_flow import recalc_from_db
from backfill_south_flow import parse_args

TEST_TABLE = "_test_ggt_hold"


class TestRecalcFromDb(unittest.TestCase):
    """窗口重算：跨日差值 / 比例差 / 估算净流入"""

    @classmethod
    def setUpClass(cls):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
            cur.execute(f"""
                CREATE TABLE {TEST_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    stock_code VARCHAR(20) NOT NULL,
                    trade_date DATE NOT NULL,
                    hold_num BIGINT,
                    hold_ratio NUMERIC(8,4),
                    hold_num_change BIGINT,
                    hold_ratio_change NUMERIC(8,4),
                    close_price NUMERIC(10,3),
                    est_net_inflow NUMERIC(16,2),
                    UNIQUE (stock_code, trade_date)
                )
            """)
            cur.execute(f"""
                INSERT INTO {TEST_TABLE}
                    (stock_code, trade_date, hold_num, hold_ratio, close_price)
                VALUES
                    ('HK.00001', '2026-07-10', 1000000000, 10.0, 50.0),
                    ('HK.00001', '2026-07-13', 1100000000, 11.0, 52.0),
                    ('HK.00001', '2026-07-14', 1050000000, 10.5, 51.0),
                    ('HK.00002', '2026-07-13', 500000000,  5.0,  20.0)
            """)

    @classmethod
    def tearDownClass(cls):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")

    def _rows(self, code):
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT trade_date, hold_num_change, hold_ratio_change, est_net_inflow "
                f"FROM {TEST_TABLE} WHERE stock_code=%s ORDER BY trade_date", (code,))
            return cur.fetchall()

    def test_recalc_cross_day(self):
        n = recalc_from_db(table=TEST_TABLE)
        self.assertGreaterEqual(n, 2, "应至少重算 2 行")

        rows = self._rows("HK.00001")
        self.assertEqual(len(rows), 3)
        # 首日（无前序）→ 全部 NULL
        self.assertIsNone(rows[0][1])
        self.assertIsNone(rows[0][2])
        self.assertIsNone(rows[0][3])
        # 07-13: 1000M→1100M 股，+100M；比例 +1.0%；inflow=100M*52/1e8=52.0 亿
        self.assertEqual(int(rows[1][1]), 100000000)
        self.assertAlmostEqual(float(rows[1][2]), 1.0, places=2)
        self.assertAlmostEqual(float(rows[1][3]), 52.0, places=2)
        # 07-14: 1100M→1050M 股，-50M；比例 -0.5%；inflow=-50M*51/1e8=-25.5 亿
        self.assertEqual(int(rows[2][1]), -50000000)
        self.assertAlmostEqual(float(rows[2][2]), -0.5, places=2)
        self.assertAlmostEqual(float(rows[2][3]), -25.5, places=2)

        # 单行股票（无前序）保持 NULL
        one = self._rows("HK.00002")
        self.assertEqual(len(one), 1)
        self.assertIsNone(one[0][1])
        self.assertIsNone(one[0][3])


class TestBackfillArgs(unittest.TestCase):
    """backfill_south_flow 参数解析（不依赖网络/DB）"""

    def test_start_end(self):
        a = parse_args(["x.py", "--start", "2026-04-30", "--end", "2026-07-12"])
        self.assertEqual(a["start"].isoformat(), "2026-04-30")
        self.assertEqual(a["end"].isoformat(), "2026-07-12")
        self.assertFalse(a["dry"])

    def test_days_and_dry(self):
        a = parse_args(["x.py", "--days", "60", "--dry-run", "--chunk", "15"])
        self.assertEqual(a["days"], 60)
        self.assertEqual(a["chunk"], 15)
        self.assertTrue(a["dry"])
        self.assertIsNone(a["start"])


if __name__ == '__main__':
    unittest.main()
