#!/usr/bin/env python3
"""
盘中实时大单/小单资金流向分化（富途 OpenAPI LV1，基于 get_capital_distribution，常驻调用版）

常驻调用：run(codes, ctx) 返回数据 dict 并写入 realtime_order_size 表。
__main__ 保留独立运行打印。不再创建/关闭共享上下文。
"""
import sys
from datetime import datetime
from futu import OpenQuoteContext, RET_OK
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

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

def get_order_size_flow(full_code, quote_ctx):
    # 1) 快照拿最新价
    ret1, snapshot = quote_ctx.get_market_snapshot([full_code])
    if ret1 != RET_OK or snapshot.empty:
        return None
    row = snapshot.iloc[0]
    price = row.get('last_price', None)
    update_time = row.get('update_time', None)

    # 2) 资金分布（盘中实时更新）
    ret2, dist = quote_ctx.get_capital_distribution(full_code)
    if ret2 != RET_OK or dist.empty:
        return None

    row_dist = dist.iloc[0]
    super_in = row_dist.get('capital_in_super', None)
    big_in   = row_dist.get('capital_in_big', None)
    mid_in   = row_dist.get('capital_in_mid', None)
    small_in = row_dist.get('capital_in_small', None)
    super_out = row_dist.get('capital_out_super', None)
    big_out   = row_dist.get('capital_out_big', None)
    mid_out   = row_dist.get('capital_out_mid', None)
    small_out = row_dist.get('capital_out_small', None)

    def net_inflow(in_val, out_val):
        if in_val is None or out_val is None:
            return None
        return float(in_val) - float(out_val)

    super_net = net_inflow(super_in, super_out)
    big_net   = net_inflow(big_in, big_out)
    mid_net   = net_inflow(mid_in, mid_out)
    small_net = net_inflow(small_in, small_out)

    large_in = None
    if super_net is not None and big_net is not None:
        large_in = super_net + big_net
    elif super_net is not None:
        large_in = super_net
    elif big_net is not None:
        large_in = big_net

    small_total = None
    if mid_net is not None and small_net is not None:
        small_total = mid_net + small_net
    elif mid_net is not None:
        small_total = mid_net
    elif small_net is not None:
        small_total = small_net

    direction = "N/A"
    if large_in is not None and small_total is not None:
        if large_in > 0 and small_total > 0:
            direction = "大单与中小单均为净流入"
        elif large_in < 0 and small_total < 0:
            direction = "大单与中小单均为净流出"
        elif large_in > 0 and small_total < 0:
            direction = "大单净流入，中小单净流出"
        elif large_in < 0 and small_total > 0:
            direction = "大单净流出，中小单净流入"
        else:
            direction = "资金流向持平"

    def raw_or_none(val):
        return float(val) if val is not None else None

    return {
        "update_time": str(update_time)[:19] if update_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "price": float(price) if price else None,
        "large_in": large_in,
        "small_in": small_total,
        "direction": direction,
        "super_in_flow": raw_or_none(super_in),
        "big_in_flow":   raw_or_none(big_in),
        "mid_in_flow":   raw_or_none(mid_in),
        "small_in_flow": raw_or_none(small_in),
        "super_out_flow": raw_or_none(super_out),
        "big_out_flow":   raw_or_none(big_out),
        "mid_out_flow":   raw_or_none(mid_out),
        "small_out_flow": raw_or_none(small_out),
        "super_net": super_net,
        "big_net": big_net,
        "mid_net": mid_net,
        "small_net": small_net,
    }

def _save_to_db(data, full_code):
    try:
        snapshot_time = datetime.strptime(data['update_time'], '%Y-%m-%d %H:%M:%S')
        with get_conn() as conn:
            upsert(conn, "realtime_order_size", {
                "stock_code": full_code,
                "snapshot_time": snapshot_time,
                "last_price": data['price'],
                "large_in_flow": data['large_in'],
                "super_in_flow": data['super_in_flow'],
                "super_out_flow": data['super_out_flow'],
                "super_net": data['super_net'],
                "big_in_flow": data['big_in_flow'],
                "big_out_flow": data['big_out_flow'],
                "big_net": data['big_net'],
                "small_total": data['small_in'],
                "mid_in_flow": data['mid_in_flow'],
                "mid_out_flow": data['mid_out_flow'],
                "mid_net": data['mid_net'],
                "small_in_flow": data['small_in_flow'],
                "small_out_flow": data['small_out_flow'],
                "small_net": data['small_net'],
                "direction": data['direction'],
            }, conflict_cols=["stock_code", "snapshot_time"])
    except Exception as e:
        print(f"[DB] 大小单资金入库失败: {e}")

def format_text(data, full_code, currency):
    if not data:
        return f"实时大小单资金 ({full_code}): 暂无数据（可能非交易时段）"
    lines = [
        f"实时大小单资金 ({full_code})",
        f"更新时间: {data['update_time']}",
        f"最新价: {fmt_price(data['price'])} {currency}",
        f"大单净流入(特大+大): {fmt_amount(data['large_in'], currency)}",
        f"  特大单: 流入 {fmt_amount(data['super_in_flow'], currency)}  流出 {fmt_amount(data['super_out_flow'], currency)}  净流入 {fmt_amount(data['super_net'], currency)}",
        f"  大单:   流入 {fmt_amount(data['big_in_flow'], currency)}  流出 {fmt_amount(data['big_out_flow'], currency)}  净流入 {fmt_amount(data['big_net'], currency)}",
        f"中小单净流入(中+小): {fmt_amount(data['small_in'], currency)}",
        f"  中单:   流入 {fmt_amount(data['mid_in_flow'], currency)}  流出 {fmt_amount(data['mid_out_flow'], currency)}  净流入 {fmt_amount(data['mid_net'], currency)}",
        f"  小单:   流入 {fmt_amount(data['small_in_flow'], currency)}  流出 {fmt_amount(data['small_out_flow'], currency)}  净流入 {fmt_amount(data['small_net'], currency)}",
        f"资金方向: {data['direction']}",
    ]
    return "\n".join(lines)

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享上下文（可空）。返回单 dict 或 list。"""
    ctx = ctx or get_shared_ctx()
    results = []
    for code in (codes or []):
        data = get_order_size_flow(code, ctx)
        if data:
            _save_to_db(data, code)
        results.append(data)
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
        print(f"获取大小单资金数据失败: {e}")
        sys.exit(0)
    if not data:
        print(format_text(None, full_code, currency))
        sys.exit(0)
    print(format_text(data, full_code, currency))
