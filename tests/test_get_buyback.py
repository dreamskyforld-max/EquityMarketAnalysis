"""get_buyback.py 全量解析逻辑单元测试（纯函数，不联网）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from get_buyback import _records_to_db_rows


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
