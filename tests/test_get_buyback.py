"""get_buyback.py 全量解析逻辑单元测试（纯函数，不联网）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from get_buyback import _records_to_db_rows, _a_repurchase_to_db_rows


SAMPLE_RECORDS = [
    # 正常记录：00700 2026-08-14
    {"SECURITY_CODE": "00700", "TRADE_DATE": "2026-08-14 00:00:00",
     "AVG_PRICE": 12.29, "REPO_NUM": 1590100, "REPO_AMT": 19543116.1},
    # 同一天多条：00700 2026-08-14 另一公告（应被去重，取先出现的倒序最新一条）
    {"SECURITY_CODE": "00700", "TRADE_DATE": "2026-08-14 00:00:00",
     "AVG_PRICE": 12.5, "REPO_NUM": 100, "REPO_AMT": 1250.0},
    # 另一只票 09660
    {"SECURITY_CODE": "09660", "TRADE_DATE": "2026-08-13 00:00:00",
     "AVG_PRICE": 30.0, "REPO_NUM": 500000, "REPO_AMT": 15000000.0},
    # 非数字 SECURITY_CODE 跳过
    {"SECURITY_CODE": "ABCD", "TRADE_DATE": "2026-08-12 00:00:00",
     "AVG_PRICE": 1.0, "REPO_NUM": 100, "REPO_AMT": 100.0},
    # 非法日期跳过
    {"SECURITY_CODE": "00555", "TRADE_DATE": "not-a-date",
     "AVG_PRICE": 1.0, "REPO_NUM": 100, "REPO_AMT": 100.0},
]


def test_records_to_db_rows_normalization():
    rows = _records_to_db_rows(SAMPLE_RECORDS)
    # 00700 一条（去重后）、09660 一条；ABCD 与非法日期被过滤
    assert len(rows) == 2
    by_code = {r["stock_code"]: r for r in rows}
    assert "HK.00700" in by_code and "HK.09660" in by_code


def test_records_to_db_rows_dedup_takes_first():
    rows = _records_to_db_rows(SAMPLE_RECORDS)
    r700 = next(r for r in rows if r["stock_code"] == "HK.00700")
    # 同一天多条取先出现（倒序最新）：AVG_PRICE=12.29 那条
    assert r700["avg_price"] == 12.29
    assert r700["volume"] == 1590100
    # 新接口无最高/最低价 → None
    assert r700["high_price"] is None
    assert r700["low_price"] is None


def test_records_to_db_rows_fields():
    r9660 = next(r for r in _records_to_db_rows(SAMPLE_RECORDS)
                 if r["stock_code"] == "HK.09660")
    assert r9660["buyback_date"] == "2026-08-13"
    assert r9660["amount"] == 15000000.0


def test_records_to_db_rows_empty():
    assert _records_to_db_rows([]) == []


# --- A股回购方案（AKShare stock_repurchase_em，方案维度）解析 ---
import pandas as pd

A_SAMPLE_DF = pd.DataFrame([
    # 正常方案：600585 茅台(沪) 实施中，已回购 720000 股 / 12661851 元
    {"股票代码": "600585", "股票简称": "海螺水泥", "回购起始时间": "2026-08-20",
     "计划回购金额区间-下限": 10000000, "计划回购金额区间-上限": 20000000,
     "已回购股份数量": 720000, "已回购金额": 12661851,
     "已回购股份价格区间-下限": 17.54, "已回购股份价格区间-上限": 17.62,
     "实施进度": "实施中", "最新公告日期": "2026-08-25"},
    # 深市 00 开头 → SZ
    {"股票代码": "000001", "股票简称": "平安银行", "回购起始时间": "2026-08-19",
     "计划回购金额区间-下限": 5000000, "计划回购金额区间-上限": 8000000,
     "已回购股份数量": 1000, "已回购金额": 10000,
     "已回购股份价格区间-下限": 10.0, "已回购股份价格区间-上限": 10.5,
     "实施进度": "完成实施", "最新公告日期": "2026-08-24"},
    # 非数字代码跳过
    {"股票代码": "ABCD", "股票简称": "X", "回购起始时间": "2026-08-18",
     "计划回购金额区间-下限": 1, "计划回购金额区间-上限": 2,
     "已回购股份数量": 1, "已回购金额": 1,
     "已回购股份价格区间-下限": 1.0, "已回购股份价格区间-上限": 2.0,
     "实施进度": "实施中", "最新公告日期": "2026-08-18"},
    # 空 DataFrame 边界
])


def test_a_repurchase_to_db_rows_basic():
    rows = _a_repurchase_to_db_rows(A_SAMPLE_DF)
    # ABCD 非数字代码被过滤 → 2 行
    assert len(rows) == 2
    by_code = {r["stock_code"]: r for r in rows}
    # 沪市 60 开头 → SH，深市 00 开头 → SZ
    assert "SH.600585" in by_code and "SZ.000001" in by_code
    r = by_code["SH.600585"]
    assert r["stock_name"] == "海螺水泥"
    assert r["start_date"].isoformat() == "2026-08-20"
    assert r["repurchased_qty"] == 720000
    assert r["repurchased_amt"] == 12661851
    assert r["repurchased_price_min"] == 17.54
    assert r["repurchased_price_max"] == 17.62
    assert r["plan_amt_min"] == 10000000
    assert r["plan_amt_max"] == 20000000
    assert r["progress"] == "实施中"
    assert r["latest_ann_date"].isoformat() == "2026-08-25"
    # plan_id 合成键唯一稳定
    assert r["plan_id"] == "SH.600585|2026-08-20|10000000.0|20000000.0"


def test_a_repurchase_to_db_rows_plan_id_unique():
    rows = _a_repurchase_to_db_rows(A_SAMPLE_DF)
    pids = [r["plan_id"] for r in rows]
    assert len(pids) == len(set(pids))  # 无撞键


def test_a_repurchase_to_db_rows_none_df():
    assert _a_repurchase_to_db_rows(None) == []


