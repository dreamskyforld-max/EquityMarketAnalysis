#!/usr/bin/env python3
"""
盘中实时主力净流入 — 特大单+大单（富途 OpenAPI LV1）
用法：
    python3 get_realtime_main_inflow.py HK.00700
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
    if val is None:
        return "N/A"
    e = float(val) / 1e8
    return f"{e:.2f} 亿{currency}"

def get_main_inflow(full_code, quote_ctx):
    ret1, snapshot = quote_ctx.get_market_snapshot([full_code])
    if ret1 != RET_OK or snapshot.empty:
        return None
    row = snapshot.iloc[0]
    price = row.get('last_price', None)
    update_time = row.get('update_time', None)

    ret2, flow = quote_ctx.get_capital_flow(full_code)
    if ret2 != RET_OK or flow.empty:
        return None

    flow_row = flow.iloc[0]
    # 安全提取各字段
    super_in = flow_row.get('super_in_flow', None)
    big_in   = flow_row.get('big_in_flow', None)
    main_raw = flow_row.get('main_in_flow', None)

    # 降级策略：如果 main_in_flow 是 'N/A' 或 None，则用特大+大单计算
    if main_raw in (None, 'N/A'):
        if super_in is not None and big_in is not None:
            main_in = float(super_in) + float(big_in)
        else:
            main_in = None
    else:
        try:
            main_in = float(main_raw)
        except (ValueError, TypeError):
            main_in = None

    return {
        "update_time": str(update_time)[:19] if update_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "price": float(price) if price else None,
        "main_in": main_in,
        "super_in": float(super_in) if super_in is not None else None,
        "big_in": float(big_in) if big_in is not None else None,
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
        data = get_main_inflow(full_code, quote_ctx)
    except Exception as e:
        print(f"获取主力净流入数据失败: {e}")
        sys.exit(0)
    finally:
        quote_ctx.close()

    if not data:
        print(f"实时主力净流入 ({full_code}): 暂无数据（可能非交易时段）")
        sys.exit(0)

    print(f"实时主力净流入 ({full_code})")
    print(f"更新时间: {data['update_time']}")
    print(f"最新价: {fmt_price(data['price'])} {currency}")
    print(f"主力净流入: {fmt_amount(data['main_in'], currency)}")
    print(f"  特大单净流入: {fmt_amount(data['super_in'], currency)}")
    print(f"  大单净流入: {fmt_amount(data['big_in'], currency)}")