#!/usr/bin/env python3
"""get_dividend_history.py 纯函数单测（不发网络请求、不连库）。"""
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from get_dividend_history import (
    em_records_to_db_rows,
    hk_df_to_db_rows,
    parse_hk_plan,
    ths_symbol,
)


# ── ths_symbol: 同花顺 URL 代码（去掉一个前导零，实测规律）──────────────
def test_ths_symbol():
    assert ths_symbol("00700") == "0700"
    assert ths_symbol("00005") == "0005"
    assert ths_symbol("09660") == "9660"
    assert ths_symbol("00381") == "0381"


# ── parse_hk_plan: 方案文本解析 ─────────────────────────────────────────
def test_parse_hk_plan_hkd():
    assert parse_hk_plan("每股4.5港元") == (4.5, "HKD")
    assert parse_hk_plan("每股0.98港元") == (0.98, "HKD")


def test_parse_hk_plan_usd_cents():
    dps, cur = parse_hk_plan("每股25美分")
    assert cur == "USD" and math.isclose(dps, 0.25)


def test_parse_hk_plan_no_div():
    assert parse_hk_plan("不分红") == (None, None)
    assert parse_hk_plan("") == (None, None)
    assert parse_hk_plan(None) == (None, None)


# ── em_records_to_db_rows: 东财原始记录 → DB 行 ─────────────────────────
def test_em_records_to_db_rows():
    records = [{
        "SECUCODE": "600900.SH",
        "PRETAX_BONUS_RMB": 7.9,           # 每10股 → 每股 0.79
        "PLAN_NOTICE_DATE": "2026-04-30 00:00:00",
        "EQUITY_RECORD_DATE": "2026-07-16 00:00:00",
        "EX_DIVIDEND_DATE": "2026-07-17 00:00:00",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "ASSIGN_PROGRESS": "实施分配",
    }, {
        "SECUCODE": "830799.BJ",           # 北交所代码也能正确映射
        "PRETAX_BONUS_RMB": None,
        "PLAN_NOTICE_DATE": None,
        "REPORT_DATE": None,
    }]
    rows = em_records_to_db_rows(records)
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["stock_code"] == "SH.600900"
    assert math.isclose(r0["dps"], 0.79)
    assert r0["ex_date"] == date(2026, 7, 17)
    assert r0["currency"] == "CNY"
    assert r0["is_dividend"] is True
    assert r0["source"] == "em"
    assert r0["dedup_key"] == "2026-04-30|2025-12-31|0.79"
    # dps 为空 → 非分红行，键含占位符不炸
    r1 = rows[1]
    assert r1["stock_code"] == "BJ.830799"
    assert r1["dps"] is None and r1["is_dividend"] is False
    assert r1["dedup_key"] == "||x"


# ── hk_df_to_db_rows: 同花顺 DataFrame → DB 行 ──────────────────────────
def _make_hk_df():
    return pd.DataFrame([
        # 分红行
        {"公告日期": "2026-03-18", "方案": "每股5.3港元", "除净日": "2026-05-15",
         "派息日": "2026-06-01", "过户日期起止日-起始": "2026-05-19",
         "过户日期起止日-截止": "2026-05-20", "类型": "年报", "进度": "实施完成",
         "以股代息": "否"},
        # 不分红行（NaT 日期）
        {"公告日期": "2026-08-12", "方案": "不分红", "除净日": pd.NaT,
         "派息日": pd.NaT, "过户日期起止日-起始": pd.NaT,
         "过户日期起止日-截止": pd.NaT, "类型": "中报", "进度": "预案",
         "以股代息": "否"},
    ])


def test_hk_df_to_db_rows():
    rows = hk_df_to_db_rows(_make_hk_df(), "00700")
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["stock_code"] == "HK.00700"
    assert r0["dps"] == 5.3 and r0["currency"] == "HKD"
    assert r0["ex_date"] == date(2026, 5, 15)
    assert r0["pay_date"] == date(2026, 6, 1)
    assert r0["record_date"] == date(2026, 5, 19)
    assert r0["is_dividend"] is True
    assert r0["source"] == "ths"
    assert r0["dedup_key"] == "2026-03-18|年报|5.3"
    # 不分级行：显式预案，is_dividend=False，日期/金额为 None
    r1 = rows[1]
    assert r1["is_dividend"] is False
    assert r1["dps"] is None and r1["ex_date"] is None and r1["currency"] is None
    assert r1["dedup_key"] == "2026-08-12|中报|x"


def test_hk_df_same_day_two_plans():
    """同日公告两条不同派息额（拆分方案）→ 两行各自保留，不触发 upsert 键冲突。"""
    df = pd.DataFrame([
        {"公告日期": "2009-03-18", "方案": "每股0.1港元", "除净日": "2009-05-06",
         "派息日": "2009-05-27", "过户日期起止日-起始": "2009-05-08",
         "过户日期起止日-截止": "2009-05-13", "类型": "年报", "进度": "实施完成",
         "以股代息": "否"},
        {"公告日期": "2009-03-18", "方案": "每股0.25港元", "除净日": "2009-05-06",
         "派息日": "2009-05-27", "过户日期起止日-起始": "2009-05-08",
         "过户日期起止日-截止": "2009-05-13", "类型": "年报", "进度": "实施完成",
         "以股代息": "否"},
    ])
    rows = hk_df_to_db_rows(df, "00700")
    assert len(rows) == 2
    keys = {r["dedup_key"] for r in rows}
    assert keys == {"2009-03-18|年报|0.1", "2009-03-18|年报|0.25"}


def test_hk_df_exact_dup_dedup():
    """完全重复行（以股代息伴随行）→ 组内去重为一条。"""
    row = {"公告日期": "2020-03-18", "方案": "每股1.2港元", "除净日": "2020-05-06",
           "派息日": "2020-05-27", "过户日期起止日-起始": "2020-05-08",
           "过户日期起止日-截止": "2020-05-13", "类型": "年报", "进度": "实施完成",
           "以股代息": "否"}
    df = pd.DataFrame([row, dict(row)])
    rows = hk_df_to_db_rows(df, "00700")
    assert len(rows) == 1


def test_hk_df_empty():
    assert hk_df_to_db_rows(None, "00700") == []
    assert hk_df_to_db_rows(pd.DataFrame(), "00700") == []
