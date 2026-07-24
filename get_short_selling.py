#!/usr/bin/env python3
"""
港股沽空数据 — 东方财富 HTML 解析（集成版，常驻调用版）

常驻调用：run(codes, ctx)。__main__ 保留独立运行。
仅支持港股（HK）。
"""
import sys
import re
import urllib.request
from datetime import date, timedelta

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
    if full_code.upper().startswith("HK"):
        return "港元"
    else:
        return "元"

# ---------- 数据获取 ----------
def get_short_selling(symbol, query_date=None, debug=False):
    if query_date is not None:
        return _fetch_for_date(symbol, query_date, debug)

    today = date.today()
    for offset in range(4):
        try_date = (today - timedelta(days=offset)).strftime('%Y-%m-%d')
        if debug:
            print(f"[调试] 尝试日期: {try_date}")
        result = _fetch_for_date(symbol, try_date, debug)
        if result:
            return result
    if debug:
        print("[调试] 最近4天均无沽空数据")
    return None

def _fetch_for_date(symbol, query_date, debug):
    url = f"https://hk.eastmoney.com/sellshort.html?code={symbol}&sdate={query_date}&edate={query_date}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://hk.eastmoney.com/"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        tr_blocks = re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL)
        for block in tr_blocks:
            text = re.sub(r'<[^>]+>', '', block).strip()
            text = re.sub(r'\s+', ' ', text)
            parts = text.split()
            if len(parts) < 10:
                continue
            if parts[1] == symbol:
                price = float(parts[3])
                volume = int(parts[4].replace(',', ''))
                avg_price = float(parts[5])
                amount_wan = float(parts[6].replace(',', '').replace('万', ''))
                total_wan = float(parts[7].replace(',', '').replace('万', ''))
                ratio = float(parts[8].replace('%', ''))
                record_date = parts[9]
                return {
                    "price": price, "volume": volume, "avg_price": avg_price,
                    "amount": amount_wan / 1e4, "total_amount": total_wan / 1e4,
                    "ratio": ratio, "date": record_date
                }
        return None
    except Exception as e:
        if debug:
            print(f"[调试] 异常: {e}")
        print(f"获取沽空数据失败: {e}")
        return None

# ---------- 采集入口（常驻调用）----------
def run(codes=None, ctx=None, debug=False):
    """codes: [股票代码]；ctx 未使用（数据源为东方财富）。"""
    full_code = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    if "." in full_code:
        market_prefix, symbol = full_code.split(".", 1)
    else:
        market_prefix = "HK"
        symbol = full_code

    if market_prefix.upper() != "HK":
        print(f"沽空数据 ({full_code}): 仅支持港股（HK），当前股票不适用")
        return

    data = get_short_selling(symbol, debug=debug)
    currency = get_currency(full_code)

    if data:
        print(f"沽空数据 ({full_code})")
        print(f"日期: {data['date']}")
        print(f"沽空比率: {data['ratio']:.2f}%")
        print(f"沽空金额: {data['amount']:.2f} 亿{currency}")
        print(f"沽空股数: {data['volume']:,}")
        print(f"沽空平均价: {fmt_price(data['avg_price'])}")
        print(f"总成交金额: {data['total_amount']:.2f} 亿{currency}")
    else:
        print(f"沽空数据 ({full_code}): 暂无数据（近4日均无沽空记录）")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run([args.code], debug=args.debug)
