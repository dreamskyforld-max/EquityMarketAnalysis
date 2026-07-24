#!/usr/bin/env python3
"""
盘中实时超额收益（个股 vs 基准指数，常驻调用版）

常驻调用：run(codes, ctx) 返回数据 dict。
__main__ 保留独立运行打印。不再创建/关闭共享上下文。
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

def calc_change_rate(last_price, prev_close):
    if last_price is None or prev_close is None or prev_close == 0:
        return None
    return (float(last_price) - float(prev_close)) / float(prev_close) * 100

def get_realtime_excess_return(stock_code, bench_code, quote_ctx):
    ret, snap = quote_ctx.get_market_snapshot([stock_code, bench_code])
    if ret != RET_OK or snap.empty:
        return None

    stock_row = snap[snap['code'] == stock_code]
    bench_row = snap[snap['code'] == bench_code]
    if stock_row.empty or bench_row.empty:
        return None

    stock = stock_row.iloc[0]
    bench = bench_row.iloc[0]

    stock_price = stock.get('last_price', None)
    stock_prev = stock.get('prev_close_price', None)
    bench_price = bench.get('last_price', None)
    bench_prev = bench.get('prev_close_price', None)
    update_time = stock.get('update_time', None)

    stock_change = calc_change_rate(stock_price, stock_prev)
    bench_change = calc_change_rate(bench_price, bench_prev)

    excess = None
    if stock_change is not None and bench_change is not None:
        excess = stock_change - bench_change

    return {
        "update_time": str(update_time)[:19] if update_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "stock_code": stock_code,
        "bench_code": bench_code,
        "stock_price": float(stock_price) if stock_price else None,
        "stock_prev": float(stock_prev) if stock_prev else None,
        "bench_price": float(bench_price) if bench_price else None,
        "bench_prev": float(bench_prev) if bench_prev else None,
        "stock_change": stock_change,
        "bench_change": bench_change,
        "excess": excess,
    }

BENCH_BY_MARKET = {"HK": "HK.800000", "SH": "SH.000001", "SZ": "SZ.399001"}

def format_text(data, full_code, currency):
    if not data:
        return f"实时超额收益 ({full_code}): 暂无数据"
    lines = [
        f"实时超额收益 ({data['stock_code']} vs {data['bench_code']})",
        f"更新时间: {data['update_time']}",
        f"个股最新价: {fmt_price(data['stock_price'])} {currency}",
        f"个股昨收:   {fmt_price(data['stock_prev'])} {currency}",
        f"基准最新价: {fmt_price(data['bench_price'])} {currency}",
        f"基准昨收:   {fmt_price(data['bench_prev'])} {currency}",
        f"个股涨跌幅: {fmt_price(data['stock_change'], 2)}%",
        f"基准涨跌幅: {fmt_price(data['bench_change'], 2)}%",
    ]
    excess = data['excess']
    lines.append(f"实时超额收益: {fmt_price(excess, 2)}%" if excess is not None else "实时超额收益: N/A")
    return "\n".join(lines)

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享上下文（可空）。返回单 dict 或 list。"""
    ctx = ctx or get_shared_ctx()
    results = []
    for code in (codes or []):
        prefix = code.split(".", 1)[0].upper() if "." in code else "HK"
        bench_code = BENCH_BY_MARKET.get(prefix)
        if not bench_code:
            continue
        results.append(get_realtime_excess_return(code, bench_code, ctx))
    return results[0] if len(results) == 1 else results

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    args = parser.parse_args()
    full_code = args.code
    prefix = full_code.split(".", 1)[0].upper() if "." in full_code else "HK"
    bench_code = BENCH_BY_MARKET.get(prefix)
    if not bench_code:
        print(f"实时超额收益 ({full_code}): 不支持的市场")
        sys.exit(0)
    currency = get_currency(full_code)
    try:
        data = run([full_code])
    except Exception as e:
        print(f"获取实时超额收益失败: {e}")
        sys.exit(0)
    if not data:
        print(format_text(None, full_code, currency))
        sys.exit(0)
    print(format_text(data, full_code, currency))
