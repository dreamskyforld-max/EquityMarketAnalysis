#!/usr/bin/env python3
"""
盘中实时分时成交额与最新价（富途 OpenAPI LV1）
用法：
    python3 get_realtime_volume_price.py HK.00700
"""
import sys
from datetime import datetime
from futu import OpenQuoteContext, RET_OK

def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def fmt_amount(val, currency):
    if val is None: return "N/A"
    e = float(val) / 1e8
    return f"{e:.2f} 亿{currency}"

def get_volume_price(full_code, quote_ctx):
    ret, snapshot = quote_ctx.get_market_snapshot([full_code])
    if ret != RET_OK or snapshot.empty:
        return None

    row = snapshot.iloc[0]
    price = row.get('last_price', None)
    update_time = row.get('update_time', None)
    volume = row.get('volume', None)        # 累计成交量(股)
    turnover = row.get('turnover', None)    # 累计成交额(元)
    turnover_rate = row.get('turnover_rate', None)  # 换手率(%)
    amplitude = row.get('amplitude', None)  # 振幅(%)

    return {
        "update_time": str(update_time)[:19] if update_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "price": float(price) if price else None,
        "volume": int(volume) if volume else None,
        "turnover": float(turnover) if turnover else None,
        "turnover_rate": float(turnover_rate) if turnover_rate else None,
        "amplitude": float(amplitude) if amplitude else None,
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    args = parser.parse_args()

    full_code = args.code
    if "." in full_code: market_prefix, symbol = full_code.split(".", 1)
    else: market_prefix, symbol = "HK", full_code

    currency = get_currency(full_code)
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        data = get_volume_price(full_code, quote_ctx)
    except Exception as e:
        print(f"获取分时成交数据失败: {e}")
        sys.exit(0)
    finally:
        quote_ctx.close()

    if not data:
        print(f"实时分时成交 ({full_code}): 暂无数据（可能非交易时段）")
        sys.exit(0)

    print(f"实时分时成交 ({full_code})")
    print(f"更新时间: {data['update_time']}")
    print(f"最新价: {fmt_price(data['price'])} {currency}")
    print(f"成交量: {data['volume']:,} 股")
    print(f"成交额: {fmt_amount(data['turnover'], currency)}")
    print(f"换手率: {fmt_price(data['turnover_rate'], 2)}%")
    print(f"振幅: {fmt_price(data['amplitude'], 2)}%")