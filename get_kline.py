#!/usr/bin/env python3
"""
历史K线 - 支持港股/A股/ETF，统一去零精度
用法：
    python3 get_kline.py HK.00700
    python3 get_kline.py SH.600519
    python3 get_kline.py SZ.159636
"""
import sys, urllib.request, json, time

def safe_float(v):
    try: return float(v)
    except: return None

def safe_int(v):
    try: return int(float(v))
    except: return None

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

def fetch_kline(market, symbol, days=10):
    if market == 0:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk{symbol},day,,,{days+10},qfq"
    elif market == 1:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh{symbol},day,,,{days+10},qfq"
    else:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz{symbol},day,,,{days+10},qfq"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        prefix_map = {0: "hk", 1: "sh", 2: "sz"}
        stock_key = f"{prefix_map[market]}{symbol}"
        stock_info = data.get("data", {}).get(stock_key, {})
        klines = stock_info.get("qfqday") or stock_info.get("day")
        if not klines:
            return None
        klines = klines[-days:]
        result = []
        for item in klines:
            result.append({
                "日期": item[0],
                "开盘": safe_float(item[1]),
                "收盘": safe_float(item[2]),
                "最高": safe_float(item[3]),
                "最低": safe_float(item[4]),
                "成交量": safe_int(item[5]),
            })
        return result
    except Exception as e:
        print(f"获取失败: {e}")
        return None

arg = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
if "." in arg:
    market_str, symbol = arg.split(".")
    market_str = market_str.upper()
else:
    market_str = "HK"
    symbol = arg

mk_map = {"HK":0,"SH":1,"SZ":2}
market = mk_map.get(market_str,0)

print(f"历史K线 - 最近10个交易日 ({arg})")
kline = fetch_kline(market, symbol, 10)
if not kline:
    print("获取失败")
else:
    for item in kline:
        print(f"  日期: {item['日期']}, 开盘: {fmt_price(item['开盘'])}, 收盘: {fmt_price(item['收盘'])}, 最高: {fmt_price(item['最高'])}, 最低: {fmt_price(item['最低'])}, 成交量: {item['成交量']:,}")