#!/usr/bin/env python3
"""
获取股票 vs 基准指数的近20日超额收益（自动选择基准，去零精度，常驻调用版）

常驻调用：run(codes, ctx)。__main__ 保留独立运行。
不再创建/关闭共享上下文（ctx 由 collector_runtime 共享）。
"""
import sys
from datetime import date, timedelta
import logging
logging.basicConfig(level=logging.WARNING)
from futu import RET_OK, KLType, AuType
from collector_runtime import get_shared_ctx

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

def _bench_for(stock_code):
    if stock_code.startswith("HK."):
        return "HK.800000"
    elif stock_code.startswith("SH."):
        return "SH.000001"
    elif stock_code.startswith("SZ."):
        return "SZ.399001"
    return "HK.800000"

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: [股票代码]；ctx: 共享行情上下文（可空）。"""
    ctx = ctx or get_shared_ctx()
    stock_code = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    bench_code = _bench_for(stock_code)

    end_date = date.today().strftime('%Y-%m-%d')
    start_date = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')

    def get_daily_prices(code):
        ret, data, _ = ctx.request_history_kline(
            code,
            start=start_date,
            end=end_date,
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=40,
            extended_time=False
        )
        if ret != RET_OK:
            print(f"获取 {code} K线失败: {data}")
            return None
        return data

    stock_data = get_daily_prices(stock_code)
    bench_data = get_daily_prices(bench_code)

    if stock_data is None or bench_data is None:
        return

    # ---------- 日期对齐 ----------
    stock_data['date'] = stock_data['time_key'].str[:10]
    bench_data['date'] = bench_data['time_key'].str[:10]
    common_dates = sorted(set(stock_data['date']) & set(bench_data['date']))[-20:]

    if len(common_dates) < 2:
        print("共同交易日不足，无法计算。")
        return

    stock_prices = dict(zip(stock_data['date'], stock_data['close']))
    bench_prices = dict(zip(bench_data['date'], bench_data['close']))

    stock_prev = None
    bench_prev = None
    cumulative_excess = 0.0

    print(f"近{len(common_dates)}个交易日超额收益：{stock_code} vs {bench_code}")
    print(f"{'日期':<12} {'股票涨跌幅':>10} {'基准涨跌幅':>10} {'超额收益':>10}")

    for d in common_dates:
        s_close = stock_prices[d]
        b_close = bench_prices[d]
        if stock_prev is not None and bench_prev is not None:
            s_ret = (float(s_close) / float(stock_prev) - 1) * 100
            b_ret = (float(b_close) / float(bench_prev) - 1) * 100
            excess = s_ret - b_ret
            cumulative_excess += excess
            print(f"{d:<12} {s_ret:>9.2f}% {b_ret:>9.2f}% {excess:>9.2f}%")
        stock_prev = s_close
        bench_prev = b_close

    print(f"\n近{len(common_dates)}日累计超额收益: {cumulative_excess:.2f}%")


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([code])
