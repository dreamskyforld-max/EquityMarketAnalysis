#!/usr/bin/env python3
"""
机构目标价 - 从本地 JSON 配置文件读取（手动维护），统一去零精度
用法：python3 get_analyst_targets.py [代码]  默认 HK.00700
"""
import sys, json, os

def fmt_price(val, max_dec=4):
    """最多 max_dec 位小数，自动去除末尾无效的 0，至少保留一位小数"""
    if val is None:
        return "N/A"
    try:
        v = round(float(val), max_dec)
    except (ValueError, TypeError):
        return str(val)
    s = f"{v:.{max_dec}f}"
    s = s.rstrip('0').rstrip('.')
    if '.' not in s:
        s += ".0"
    return s

full_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
if "." in full_code:
    _, symbol = full_code.split(".")
else:
    symbol = full_code

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyst_targets.json")

def load_targets_from_file(stock_code):
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(stock_code)

info = load_targets_from_file(symbol)

if info is None:
    print(f"机构目标价：{full_code} 暂无数据（请在 analyst_targets.json 中补充）")
else:
    print(f"机构目标价 ({full_code})")
    print(f"平均目标价: {fmt_price(info['avg'])}")
    print(f"最高目标价: {fmt_price(info['high'])}")
    print(f"最低目标价: {fmt_price(info['low'])}")
    if info.get('count'):
        print(f"统计机构数: {info['count']}")
    if info.get('updated'):
        print(f"数据更新日期: {info['updated']}")