"""get_cbbc.py 全量解析逻辑单元测试（纯文本解析，不联网）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from get_cbbc import _norm_symbol, parse_cbbc_full


# 构造港交所 CBBC 完整列表 CSV 文本（第一行标题，第二行字段名，tab 分隔）
SAMPLE_CSV = """HKEX CBBC Full List
UL\tBull/Bear\tCall Level\tTotal Issue Size\tO/S (%)
00700\tBull\t500.00\t100,000,000\t25
00700\tBull\t480.00\t200,000,000\t10
00700\tBear\t600.00\t50,000,000\t40
09660\tBull\t30.00\t10,000,000\t0
09660\tBear\t35.00\t20,000,000\t50
ABCD\tBull\t10.00\t1,000,000\t30
"""


def test_norm_symbol():
    assert _norm_symbol("00700") == "700"
    assert _norm_symbol("700") == "700"
    assert _norm_symbol("ABCD") == "ABCD"
    assert _norm_symbol(None) is None


def test_parse_cbbc_full_multi_issue_take_max_street_vol():
    result = parse_cbbc_full(SAMPLE_CSV)
    # 00700 两只牛证：500@25万（街货 25,000,000） vs 480@10%（街货 20,000,000）→ 取前者
    assert result["700"]["bull_call_level"] == 500.0
    assert result["700"]["bull_street_volume"] == 25000000
    # 熊证 600@40%（街货 20,000,000）
    assert result["700"]["bear_call_level"] == 600.0
    assert result["700"]["bear_street_volume"] == 20000000


def test_parse_cbbc_full_skip_zero_os():
    result = parse_cbbc_full(SAMPLE_CSV)
    # 09660 牛证 O/S=0 被跳过 → 无牛证字段为 None；熊证 35@50% → 街货 10,000,000
    assert result["9660"]["bull_call_level"] is None
    assert result["9660"]["bull_street_volume"] is None
    assert result["9660"]["bear_call_level"] == 35.0
    assert result["9660"]["bear_street_volume"] == 10000000


def test_parse_cbbc_full_skip_non_numeric_ul():
    result = parse_cbbc_full(SAMPLE_CSV)
    # ABCD 非数字代码跳过
    assert "ABCD" not in result
    assert "abcd" not in result
