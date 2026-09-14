"""get_company_profile.py 解析/行构造逻辑单元测试（纯函数，不联网、不连库）。"""
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from get_company_profile import (
    _parse_date,
    _parse_qty,
    _to_float,
    _ts_to_date,
    breakdown_rows,
    profile_row,
)


# ── 基础解析 ─────────────────────────────────────────────────

def test_parse_qty_units():
    assert _parse_qty("4.20亿股") == 420000000
    assert _parse_qty("23.26亿股") == 2326000000
    assert _parse_qty("4831.85万股") == 48318500
    assert _parse_qty("1,234股") == 1234
    assert _parse_qty(None) is None
    assert _parse_qty("") is None
    assert _parse_qty("--") is None


def test_parse_date_formats():
    assert _parse_date("2004/06/16") == date(2004, 6, 16)
    assert _parse_date("1999-11-23") == date(1999, 11, 23)
    assert _parse_date("2004/06/16 00:00:00") == date(2004, 6, 16)
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None


def test_to_float():
    assert _to_float("3.70") == 3.70
    assert _to_float("1,234.5") == 1234.5
    assert _to_float("") is None
    assert _to_float(None) is None


def test_ts_to_date_hkt_zero_hour():
    """富途 screen_date 为北京时间零点，UTC 换算会少一天（2026/H1 → 06-30）。"""
    assert _ts_to_date(1782748800) == date(2026, 6, 30)
    assert _ts_to_date(None) is None


# ── profile_row（港股 / A股两套字段）──────────────────────────

HK_PROFILE = pd.DataFrame([
    {"name": "公司代码", "value": "00700", "field_type": 0},
    {"name": "公司名称", "value": "腾讯控股有限公司", "field_type": 0},
    {"name": "上市日期", "value": "2004/06/16", "field_type": 0},     # stock_info 已有 → 忽略
    {"name": "发行价格", "value": "3.70", "field_type": 0},
    {"name": "发行数量", "value": "4.20亿股", "field_type": 0},
    {"name": "成立日期", "value": "1999/11/23", "field_type": 0},
    {"name": "公司注册地址", "value": "开曼群岛", "field_type": 0},
    {"name": "注册办事处", "value": "Cricket Square", "field_type": 0},
    {"name": "总办事处及主要营业地点", "value": "香港湾仔皇后大道东1号", "field_type": 0},
    {"name": "董事长", "value": "马化腾", "field_type": 0},
    {"name": "公司类别", "value": "境外注册内地个人控制", "field_type": 0},
    {"name": "员工数量", "value": "115927", "field_type": 0},
    {"name": "所属市场", "value": "香港主板", "field_type": 0},        # 忽略
    {"name": "公司业务", "value": "   腾讯主营业务描述", "field_type": 2},  # 带前导空格
    {"name": "公司简介", "value": "腾讯简介长文", "field_type": 2},
    {"name": "公司秘书", "value": None, "field_type": 0},             # 空值不写入
])

A_PROFILE = pd.DataFrame([
    {"name": "A股证券代码", "value": "600900", "field_type": 0},
    {"name": "公司名称", "value": "中国长江电力股份有限公司", "field_type": 0},
    {"name": "法人代表", "value": "刘伟平", "field_type": 0},
    {"name": "总经理", "value": "刘海波", "field_type": 0},
    {"name": "会计师事务所", "value": "信永中和会计师事务所", "field_type": 0},
    {"name": "公司办公地址", "value": "湖北省武汉市江岸区三阳路88号", "field_type": 0},
    {"name": "公司办公地址邮编", "value": "430014", "field_type": 0},
    {"name": "企业法人营业执照注册号", "value": "100000000037300", "field_type": 0},
])


def test_profile_row_hk():
    row = profile_row("HK.00700", HK_PROFILE)
    assert row["stock_code"] == "HK.00700" and row["source"] == "futu"
    assert row["company_name"] == "腾讯控股有限公司"
    assert row["founded_date"] == date(1999, 11, 23)
    assert row["issue_price"] == 3.70
    assert row["issue_qty"] == 420000000
    assert row["office_address"] == "香港湾仔皇后大道东1号"
    assert row["registered_office"] == "Cricket Square"
    assert row["employee_count"] == 115927
    assert row["main_business"] == "腾讯主营业务描述"      # 前导空格已清理
    assert row["company_intro"] == "腾讯简介长文"
    # 空值字段不写入 dict（配合 skip_null_updates 保护已有值）
    assert "company_secretary" not in row
    # stock_info 已有字段不重复落库
    assert "list_date" not in row and "exchange_type" not in row


def test_profile_row_a_share():
    row = profile_row("SH.600900", A_PROFILE)
    assert row["company_name"] == "中国长江电力股份有限公司"
    assert row["legal_rep"] == "刘伟平"
    assert row["general_manager"] == "刘海波"
    assert row["auditor"] == "信永中和会计师事务所"        # A股字段名 → auditor
    assert row["office_address"] == "湖北省武汉市江岸区三阳路88号"
    assert row["office_postcode"] == "430014"
    assert row["business_license_no"] == "100000000037300"
    assert "chairman" not in row                          # A股无董事长字段


def test_profile_row_invalid_without_name():
    """无公司全称视为无效返回（该票计失败，不落空行）。"""
    df = pd.DataFrame([{"name": "员工数量", "value": "100", "field_type": 0}])
    assert profile_row("HK.00001", df) is None


# ── breakdown_rows（主营构成）────────────────────────────────

PAYLOAD_H1 = {
    "period": "2026/H1",
    "currency_code": "CNY",
    "breakdown_list": [
        {"type": "PRODUCT", "item_list": [
            {"name": "增值服务", "main_oper_income": 194524000000.0, "ratio": 48.4803},
            {"name": "其他", "main_oper_income": 4812000000.0, "ratio": 1.1992},
        ]},
        {"type": "BUSINESS", "item_list": [
            {"name": "增值服务", "main_oper_income": 194524000000.0, "ratio": 48.4803},
        ]},
    ],
    "screen_date_list": [
        {"date": 1782748800, "period_text": "2026/H1", "financial_type": "Q6"},
        {"date": 1767110400, "period_text": "2025/FY", "financial_type": "ANNUAL"},
    ],
}


def test_breakdown_rows_basic():
    rows = breakdown_rows("HK.00700", PAYLOAD_H1)
    assert len(rows) == 3
    r = rows[0]
    assert r["stock_code"] == "HK.00700"
    assert r["period"] == "2026/H1"
    assert r["period_end"] == date(2026, 6, 30)
    assert r["breakdown_type"] == "PRODUCT"
    assert r["item_name"] == "增值服务"
    assert r["revenue"] == 194524000000.0
    assert r["ratio"] == 48.4803
    assert r["currency"] == "CNY" and r["source"] == "futu"


def test_breakdown_rows_history_period_uses_date_map():
    """坑：逐期调用返回的 payload 不含 screen_date_list，
    历史期 period_end 必须由首次调用汇总的 date_map 提供，否则恒为 NULL。"""
    payload_fy = {
        "period": "2025/FY",
        "currency_code": "CNY",
        "breakdown_list": [
            {"type": "PRODUCT", "item_list": [
                {"name": "增值服务", "main_oper_income": 369281000000.0, "ratio": 49.1218},
            ]},
        ],
        # 注意：无 screen_date_list
    }
    date_map = {p["period_text"]: p["date"] for p in PAYLOAD_H1["screen_date_list"]}
    rows = breakdown_rows("HK.00700", payload_fy, date_map)
    assert len(rows) == 1
    assert rows[0]["period_end"] == date(2025, 12, 31)


def test_breakdown_rows_empty_and_invalid():
    assert breakdown_rows("HK.00700", {}) == []
    assert breakdown_rows("HK.00700", "error string") == []
    assert breakdown_rows("HK.00700", {"period": "2026/H1", "breakdown_list": []}) == []
