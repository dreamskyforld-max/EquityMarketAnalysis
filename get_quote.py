#!/usr/bin/env python3
"""
行情快照采集 — 富途 get_market_snapshot() 实时接口（常驻调用版）

数据源：FutuOpenD（端口 11111）
写入表：daily_quote（ON CONFLICT (stock_code, trade_date) DO UPDATE）

用法（独立运行兼容）：
    python3 get_quote.py                     # 默认 HK.00700 + HK.800000
    python3 get_quote.py SH.600900           # 单只股票
    python3 get_quote.py SH.600900 HK.00700  # 多只股票（空格分隔）

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用，
不创建/不关闭共享上下文。
"""
import sys
import logging
from datetime import date
logging.basicConfig(level=logging.WARNING)
from futu import RET_OK
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

def fmt_price(val, max_dec=4):
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

def get_currency(code):
    """根据股票代码前缀判断币种：HK→港元，其他→元"""
    return "港元" if code.startswith("HK.") else "元"

def format_amount(amount, cur):
    """成交额格式化为亿为单位"""
    if amount == 0:
        return f"0.00 亿{cur}"
    yi = amount / 1e8
    return f"{yi:.2f} 亿{cur}"

def format_ratio(ratio):
    """百分比格式化"""
    return f"{ratio:.2f}%"

# 基准指数：只打印到控制台，不写入 daily_quote
BENCHMARK_CODES = {"HK.800000", "SH.000001", "SZ.399001"}

def _parse_date(update_time):
    """从 update_time 字符串提取日期，如 '2026-06-02 16:00:00' → date(2026,6,2)"""
    if not update_time or update_time == 'N/A':
        return date.today()
    try:
        s = str(update_time)[:10]  # '2026-06-02'
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return date.today()

def save_quote_to_db(stock_code, row):
    """将富途快照数据映射到 daily_quote 表并写入 DB（upsert，按 stock_code+trade_date 去重）"""
    try:
        last_price = float(row['last_price']) if row.get('last_price') is not None else None
        prev_close = row.get('prev_close_price')
        prev_close_f = float(prev_close) if prev_close and prev_close != 0 else None
        change_pct = None
        if last_price is not None and prev_close_f is not None:
            change_pct = round((last_price - prev_close_f) / prev_close_f * 100, 4)

        trade_date = _parse_date(row.get('update_time'))

        # 富途快照字段 → daily_quote 表字段映射
        data = {
            "stock_code": stock_code,
            "trade_date": trade_date,
            "update_time": row.get('update_time'),
            "last_price": last_price,                                 # 最新价
            "open_price": float(row['open_price']) if row.get('open_price') is not None else None,     # 今开
            "high_price": float(row['high_price']) if row.get('high_price') is not None else None,     # 最高
            "low_price": float(row['low_price']) if row.get('low_price') is not None else None,        # 最低
            "prev_close": prev_close_f,                                # 昨收
            "change_pct": change_pct,                                  # 涨跌幅(%)
            "volume": int(row['volume']) if row.get('volume') is not None else None,                   # 成交量(股)
            "turnover": float(row['turnover']) if row.get('turnover', 0) > 0 else None,               # 成交额
            "turnover_rate": float(row['turnover_rate']) if row.get('turnover_rate') is not None else None,  # 换手率
            "volume_ratio": float(row['volume_ratio']) if row.get('volume_ratio', 0) > 0 else None,   # 量比
            "high_52w": float(row['highest52weeks_price']) if row.get('highest52weeks_price', 0) > 0 else None,  # 52周最高
            "low_52w": float(row['lowest52weeks_price']) if row.get('lowest52weeks_price', 0) > 0 else None,     # 52周最低
            "total_market_val": float(row['total_market_val']) if row.get('total_market_val') is not None else None,        # 总市值
            "circular_market_val": float(row['circular_market_val']) if row.get('circular_market_val') is not None else None,  # 流通市值
            "pe_ratio": float(row['pe_ratio']) if row.get('pe_ratio') is not None else None,                    # 静态PE
            "pe_ttm_ratio": float(row['pe_ttm_ratio']) if row.get('pe_ttm_ratio') is not None else None,        # PE(TTM)
            "pb_ratio": float(row['pb_ratio']) if row.get('pb_ratio') is not None else None,                    # PB
            "dividend_ratio_ttm": float(row['dividend_ratio_ttm']) if row.get('dividend_ratio_ttm') is not None else None,  # 股息率(TTM,%)
        }

        with get_conn() as conn:
            upsert(conn, "daily_quote", data, conflict_cols=["stock_code", "trade_date"])
    except Exception as e:
        # 数据库写入失败不中断控制台输出
        print(f"[DB] 行情快照入库失败 ({stock_code}): {e}")

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享行情上下文（可空）。"""
    ctx = ctx or get_shared_ctx()
    targets = list(codes) if codes else ["HK.00700", "HK.800000"]

    # 调用富途行情快照接口（实时数据）
    ret, data = ctx.get_market_snapshot(targets)
    if ret != RET_OK:
        print(f"获取行情快照失败: {data}")
        return

    # 逐只股票打印行情并入库
    for idx, row in data.iterrows():
        code = row['code']
        name = row.get('name', code)
        cur = get_currency(code)
        update_time = row.get('update_time', 'N/A')

        print(f"{name} ({code})")
        print(f"更新时间: {update_time}")
        print(f"最新价: {fmt_price(row['last_price'])} {cur}")
        print(f"今开: {fmt_price(row['open_price'])}")
        print(f"最高: {fmt_price(row['high_price'])}")
        print(f"最低: {fmt_price(row['low_price'])}")
        prev_close = row.get('prev_close_price')
        if prev_close and prev_close != 0:
            change = (float(row['last_price']) - float(prev_close)) / float(prev_close) * 100
            print(f"昨收: {fmt_price(prev_close)}")
            print(f"涨跌幅: {format_ratio(change)}")
        print(f"成交量: {int(row['volume']):,} 股")
        if row['turnover'] > 0:
            print(f"成交额: {format_amount(row['turnover'], cur)}")
        print(f"换手率: {format_ratio(row['turnover_rate'])}")
        if 'volume_ratio' in row and row['volume_ratio'] > 0:
            print(f"量比: {row['volume_ratio']:.2f}")
        if 'highest52weeks_price' in row and row['highest52weeks_price'] > 0:
            print(f"52周最高: {fmt_price(row['highest52weeks_price'])}")
        if 'lowest52weeks_price' in row and row['lowest52weeks_price'] > 0:
            print(f"52周最低: {fmt_price(row['lowest52weeks_price'])}")
        if row.get('total_market_val') is not None:
            print(f"总市值: {format_amount(row['total_market_val'], cur)}")
        if row.get('circular_market_val') is not None:
            print(f"流通市值: {format_amount(row['circular_market_val'], cur)}")
        if row.get('pe_ratio') is not None:
            print(f"静态PE: {fmt_price(row['pe_ratio'])}")
        if row.get('pe_ttm_ratio') is not None:
            print(f"PE(TTM): {fmt_price(row['pe_ttm_ratio'])}")
        if row.get('pb_ratio') is not None:
            print(f"PB: {fmt_price(row['pb_ratio'])}")
        if row.get('dividend_ratio_ttm') is not None:
            print(f"股息率(TTM): {format_ratio(row['dividend_ratio_ttm'])}")
        print()

        # 写入数据库（跳过基准指数）
        if code not in BENCHMARK_CODES:
            save_quote_to_db(code, row)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        targets = ["HK.00700", "HK.800000"]  # 默认：腾讯 + 恒生指数
    else:
        targets = sys.argv[1:]
    run(targets)
