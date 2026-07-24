#!/usr/bin/env python3
"""
趋势技术指标 — 均线、MACD、RSI（纯数据版，前复权，常驻调用版）

数据源：富途 OpenAPI request_history_kline（LV1）
常驻调用：run(codes, ctx)。__main__ 保留独立运行。
"""
import sys
from datetime import date, timedelta
from futu import RET_OK, AuType
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

# ---------- 工具函数 ----------
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

# ---------- 技术指标计算 ----------
def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def calc_ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for val in data[period:]:
        ema = val * k + ema * (1 - k)
    return ema

def calc_macd(closes):
    if len(closes) < 26:
        return (None, None, None)
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return (None, None, None)
    dif = ema12 - ema26
    dif_list = []
    for i in range(26, len(closes) + 1):
        e12 = calc_ema(closes[:i], 12)
        e26 = calc_ema(closes[:i], 26)
        dif_list.append(e12 - e26)
    dea = calc_ema(dif_list, 9)
    if dea is None:
        return (dif, None, None)
    macd_hist = 2 * (dif - dea)
    return (dif, dea, macd_hist)

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = 0, 0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

# ---------- 数据获取 ----------
def get_trend_data(full_code, quote_ctx):
    market_prefix, symbol = full_code.split(".") if "." in full_code else ("HK", full_code)
    ret, df, page_req_key = quote_ctx.request_history_kline(
        code=full_code,
        start=(date.today() - timedelta(days=200)).strftime('%Y-%m-%d'),
        end=date.today().strftime('%Y-%m-%d'),
        ktype='K_DAY',
        max_count=200,
        autype=AuType.QFQ
    )
    if ret != RET_OK or df.empty:
        return None

    closes = list(df['close'])
    if len(closes) < 60:
        return None

    if 'time_key' in df.columns:
        last_key = df['time_key'].iloc[-1]
        if hasattr(last_key, 'strftime'):
            last_date = last_key.strftime('%Y-%m-%d')
        else:
            last_date = str(last_key)[:10]
    else:
        last_date = date.today().strftime('%Y-%m-%d')

    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    dif, dea, macd_hist = calc_macd(closes)
    rsi14 = calc_rsi(closes, 14)

    return {
        "last_date": last_date,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "dif": dif, "dea": dea, "macd_hist": macd_hist, "rsi14": rsi14,
    }

# ---------- 采集入口（常驻调用）----------
def run(codes=None, ctx=None):
    ctx = ctx or get_shared_ctx()
    full_code = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    currency = get_currency(full_code)

    data = get_trend_data(full_code, ctx)
    if not data:
        print(f"趋势指标 ({full_code}): 数据不足，无法计算")
        return

    print(f"趋势指标 ({full_code})")
    print(f"数据日期: {data['last_date']}")
    print(f"MA5:   {fmt_price(data['ma5'])}")
    print(f"MA10:  {fmt_price(data['ma10'])}")
    print(f"MA20:  {fmt_price(data['ma20'])}")
    print(f"MA60:  {fmt_price(data['ma60'])}")
    print(f"MACD DIF:  {fmt_price(data['dif'], 4)}")
    print(f"MACD DEA:  {fmt_price(data['dea'], 4)}")
    macd_hist = data['macd_hist']
    if macd_hist is not None:
        print(f"MACD 柱:   {fmt_price(macd_hist, 4)}")
    else:
        print("MACD 柱:   N/A")
    print(f"RSI(14):   {fmt_price(data['rsi14'], 2)}")

    # 写入数据库
    try:
        with get_conn() as conn:
            upsert(conn, "daily_trend", {
                "stock_code": full_code,
                "trade_date": date.fromisoformat(data['last_date']),
                "ma5": data['ma5'], "ma10": data['ma10'], "ma20": data['ma20'], "ma60": data['ma60'],
                "macd_dif": data['dif'], "macd_dea": data['dea'], "macd_hist": data['macd_hist'],
                "rsi14": data['rsi14'],
            }, conflict_cols=["stock_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 趋势指标入库失败: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    args = parser.parse_args()
    run([args.code])
