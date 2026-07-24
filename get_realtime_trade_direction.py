#!/usr/bin/env python3
"""
盘中实时主动性买卖盘比率（富途 OpenAPI LV1，常驻调用版）

常驻调用：run(codes, ctx) 返回数据 dict（含 bid_vol/ask_vol/volume/turnover/price）。
__main__ 保留独立运行打印。不再创建/关闭共享上下文。
"""
import sys
from datetime import datetime
from futu import RET_OK
from collector_runtime import get_shared_ctx

def fmt_price(val, max_dec=4):
    if val is None:
        return "N/A"
    try:
        v = round(float(val), max_dec)
    except:
        return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def get_trade_direction(full_code, quote_ctx):
    ret, snapshot = quote_ctx.get_market_snapshot([full_code])
    if ret != RET_OK or snapshot.empty:
        return None

    row = snapshot.iloc[0]
    price = row.get('last_price', None)
    update_time = row.get('update_time', None)
    bid_vol = row.get('bid_vol', None)   # 主动性买盘
    ask_vol = row.get('ask_vol', None)   # 主动性卖盘

    return {
        "update_time": str(update_time)[:19] if update_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "price": float(price) if price else None,
        "bid_vol": int(bid_vol) if bid_vol else None,
        "ask_vol": int(ask_vol) if ask_vol else None,
        "volume": int(row['volume']) if row.get('volume') is not None else None,
        "turnover": float(row['turnover'])/1e8 if row.get('turnover', 0) > 0 else None,  # 转为亿元，与 trend_snapshot.turnover 字段语义一致
    }

def format_text(data, full_code, currency):
    """复现原独立运行的打印内容，供 trend_cache raw 字段与手动调用使用。"""
    if not data:
        return f"实时主动性买卖盘 ({full_code}): 暂无数据（可能非交易时段）"
    lines = [
        f"实时主动性买卖盘 ({full_code})",
        f"更新时间: {data['update_time']}",
        f"最新价: {fmt_price(data['price'])} {currency}",
    ]
    bid = data['bid_vol']
    ask = data['ask_vol']
    if bid is not None and ask is not None:
        lines.append(f"主动性买盘: {bid:,} 股")
        lines.append(f"主动性卖盘: {ask:,} 股")
        lines.append(f"主动买卖比: {bid/ask:.2f}" if ask > 0 else "主动买卖比: N/A (卖盘为0)")
    else:
        lines.append("主动性买卖盘: N/A")
    vol = data.get('volume')
    turnover = data.get('turnover')
    lines.append(f"成交量: {vol:,} 股" if vol is not None else "成交量: N/A")
    lines.append(f"成交额: {turnover:.2f} 亿{currency}" if turnover is not None else "成交额: N/A")
    return "\n".join(lines)

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享行情上下文（可空）。返回单 dict 或 list。"""
    ctx = ctx or get_shared_ctx()
    results = []
    for code in (codes or []):
        results.append(get_trade_direction(code, ctx))
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
        print(f"获取主动性买卖盘数据失败: {e}")
        sys.exit(0)
    if not data:
        print(format_text(None, full_code, currency))
        sys.exit(0)
    print(format_text(data, full_code, currency))
