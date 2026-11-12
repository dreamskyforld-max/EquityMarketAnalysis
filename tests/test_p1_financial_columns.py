"""P1 补数第一批防回退测试（doc/stock_profile_l2_l3.md §8 / §13）。

纯源码 + 纯函数断言，不依赖数据库；指标名以 ak.stock_financial_abstract
实测为准（get_financial.fetch_a 用精确字符串匹配，写错会静默恒 NULL）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import get_financial as gf

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


NEW_COLS = (
    "net_profit_excl_nonrec", "goodwill", "roic",
    "inventory_turnover", "receivable_turnover", "total_asset_turnover",
    "capex_fixed", "capex_intangible", "acquisition_cash_flow", "rd_expense",
)

A_MAP_EXPECTED = {
    "扣非净利润": "net_profit_excl_nonrec",
    "商誉": "goodwill",
    "投入资本回报率": "roic",
    "存货周转率": "inventory_turnover",
    "应收账款周转率": "receivable_turnover",
    "总资产周转率": "total_asset_turnover",
}


class TestSchemaRegistration(unittest.TestCase):
    def test_schema_sql_declares_all_new_columns(self):
        src = _src("sql/schema.sql")
        for col in NEW_COLS:
            self.assertIn(col, src, col)

    def test_idempotent_alter_for_every_new_column(self):
        src = _src("sql/schema.sql")
        for col in NEW_COLS:
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col}", src, col)

    def test_ratio_columns_use_numeric_12_4(self):
        # AGENTS.md：百分比/比率列统一 NUMERIC(12,4)，8,4 会让整批 INSERT 溢出
        src = _src("sql/schema.sql")
        expected = {"roic", "inventory_turnover", "receivable_turnover",
                    "total_asset_turnover"}
        checked = set()
        for line in src.splitlines():
            stripped = line.strip()
            for col in expected:
                if stripped.startswith(col + " ") or stripped.startswith(col + "NUMERIC"):
                    self.assertIn("NUMERIC(12,4)", line, line)
                    self.assertNotIn("NUMERIC(8,4)", line, line)
                    checked.add(col)
        self.assertEqual(checked, expected)

    def test_stale_fcf_comment_removed(self):
        src = _src("sql/schema.sql")
        self.assertNotIn("暂留空 NULL，后续补充 CAPEX", src)
        self.assertIn("已落库", src)


class TestCollector(unittest.TestCase):
    def test_a_indicator_map_exact_names(self):
        for cn, db_field in A_MAP_EXPECTED.items():
            self.assertEqual(gf.A_INDICATOR_MAP.get(cn), db_field, cn)

    def test_hk_cash_flow_targets_include_acquisition(self):
        src = _src("get_financial.py")
        self.assertIn('"acquisition_cash":', src)
        self.assertIn("收购附属公司", src)

    def test_hk_record_exposes_new_fields(self):
        row = {
            "REPORT_DATE": "2025-12-31 00:00:00",
            "OPERATE_INCOME": 1000.0, "OCF_SALES": 50.0,
            "HOLDER_PROFIT": 200.0, "GROSS_PROFIT_RATIO": 40.0,
            "NET_PROFIT_RATIO": 20.0, "ROE_AVG": 15.0,
            "DEBT_ASSET_RATIO": 30.0, "OPERATE_INCOME_YOY": 10.0,
            "HOLDER_PROFIT_YOY": 12.0, "DATE_TYPE_CODE": "001",
            "START_DATE": "2025-01-01", "ROIC_YEARLY": 18.5,
        }
        cf = {"2025-12-31": {"ocf": 500.0, "capex_fixed": 100.0,
                             "capex_intangible": 20.0, "acquisition_cash": 30.0}}
        rec = gf._build_hk_record(row, cf)
        self.assertEqual(rec["roic"], 18.5)
        self.assertEqual(rec["capex_fixed"], 100.0)
        self.assertEqual(rec["capex_intangible"], 20.0)
        self.assertEqual(rec["acquisition_cash_flow"], 30.0)
        self.assertEqual(rec["free_cash_flow"], 380.0)  # 500 - 100 - 20

    def test_hk_record_missing_cf_is_none_not_error(self):
        row = {
            "REPORT_DATE": "2025-12-31 00:00:00",
            "OPERATE_INCOME": 1000.0, "OCF_SALES": 50.0,
            "HOLDER_PROFIT": 200.0, "GROSS_PROFIT_RATIO": 40.0,
            "NET_PROFIT_RATIO": 20.0, "ROE_AVG": 15.0,
            "DEBT_ASSET_RATIO": 30.0, "OPERATE_INCOME_YOY": 10.0,
            "HOLDER_PROFIT_YOY": 12.0, "DATE_TYPE_CODE": "001",
            "START_DATE": "2025-01-01",
        }
        rec = gf._build_hk_record(row, {})
        for key in ("roic", "capex_fixed", "capex_intangible", "acquisition_cash_flow"):
            self.assertIsNone(rec[key], key)


class TestQuantileSelect(unittest.TestCase):
    def test_load_financial_latest_selects_new_columns(self):
        src = _src("profiling/quantile.py")
        for col in NEW_COLS:
            self.assertIn(col, src, col)


if __name__ == "__main__":
    unittest.main()
