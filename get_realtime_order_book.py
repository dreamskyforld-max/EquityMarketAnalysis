#!/usr/bin/env python3
"""
盘中实时买卖盘口（富途 OpenAPI，需 LV2，常驻调用版）

注意：LV2 权限仅 Mac 端可用，服务器（LV1）无法运行此脚本（get_order_book 返回 None）。
常驻调用：run(codes, ctx) 返回数据 dict（bids/asks）。__main__ 保留独立运行打印。
"""
import sys
from datetime import datetime
from futu import OpenQuoteContext, RET_OK
from collector_runtime import get_shared_ctx

def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def get_order_book(full_code, quote_ctx):
    # 必须先订阅才能获取摆盘
    ret_sub, _ = quote_ctx.subscribe([full_code], ['ORDER_BOOK'])
    if ret_sub != RET_OK:
        return None

    ret, order_book = quote_ctx.get_order_book(full_code)
    if ret != RET_OK:
        return None

    bid_list = order_book.get('Bid', [])
    ask_list = order_book.get('Ask', [])

    top_bids = bid_list[:5] if bid_list else []
    top_asks = ask_list[:5] if ask_list else []

    return {
        "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "bids": [(float(b[0]), int(b[1]), int(b[2])) for b in top_bids],
        "asks": [(float(a[0]), int(a[1]), int(a[2])) for a in top_asks],
    }

def format_text(data, full_code, currency):
    if not data:
        return f"实时盘口 ({full_code}): 暂无数据（可能非交易时段或LV2权限不足）"
    lines = [f"实时盘口 ({full_code})", f"更新时间: {data['update_time']}", "\n卖盘 (Ask):"]
    for price, vol, orders in data['asks']:
        lines.append(f"  {fmt_price(price)} {currency}  挂单: {vol:,} 股  订单数: {orders}")
    lines.append("\n买盘 (Bid):")
    for price, vol, orders in data['bids']:
        lines.append(f"  {fmt_price(price)} {currency}  挂单: {vol:,} 股  订单数: {orders}")
    return "\n".join(lines)

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享上下文（可空）。返回单 dict 或 list。"""
    ctx = ctx or get_shared_ctx()
    results = []
    for code in (codes or []):
        results.append(get_order_book(code, ctx))
    return results[0] if len(results) == 1 else results

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    args = parser.parse_args()
    full_code = args.code
    currency = get_currency(full_code)
    try:
        data = run([full_code])
    except Exception as e:
        print(f"获取盘口数据失败: {e}")
        sys.exit(0)
    if not data:
        print(format_text(None, full_code, currency))
        sys.exit(0)
    print(format_text(data, full_code, currency))
