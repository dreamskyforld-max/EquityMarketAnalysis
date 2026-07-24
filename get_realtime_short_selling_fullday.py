#!/usr/bin/env python3
"""
港股全日沽空数据 — 港交所官方全日快照（常驻调用版）

数据源：https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm
发布时间：每个交易日约 18:00-19:00
常驻调用：run(codes, ctx)。__main__ 保留独立运行。
仅支持港股（HK）。
"""
import sys, re, urllib.request
from datetime import datetime, date
from db import get_conn, upsert

def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def get_fullday_short_selling(symbol, debug=False):
    url = "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("big5", errors="replace")
    except Exception as e:
        if debug:
            print(f"[调试] 请求页面失败: {e}")
        return None

    pre_match = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL)
    if not pre_match:
        if debug:
            print("[调试] 未找到 <pre> 标签")
        return None

    text = pre_match.group(1)
    text = text.replace('\u3000', ' ')

    date_match = re.search(r'日期\s*:\s*(\d{1,2}\s+\w+\s+\d{4})', text)
    data_date = date_match.group(1) if date_match else date.today().strftime('%d %b %Y')

    try:
        normalized_symbol = str(int(symbol))
    except ValueError:
        normalized_symbol = symbol

    if debug:
        print(f"[调试] 查找代码: {normalized_symbol}")

    pattern = r'^\s*(\d{1,5})\s{2,}(.+?)\s{2,}([\d,]+)\s+([\d,]+)$'
    lines = text.split('\n')

    for line in lines:
        if line.lstrip().startswith('%'):
            continue

        m = re.match(pattern, line.strip())
        if not m:
            continue

        code = m.group(1)
        name = m.group(2).strip()
        vol_str = m.group(3)
        amount_str = m.group(4)

        if code == normalized_symbol and not code.startswith('8'):
            volume = int(vol_str.replace(',', ''))
            amount = float(amount_str.replace(',', '')) / 1e8
            return {
                "date": data_date,
                "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "volume": volume,
                "amount": amount,
                "name": name,
            }

    if debug:
        print(f"[调试] 未找到代码 {normalized_symbol} 的数据")
    return None

def run(codes=None, ctx=None, debug=False):
    """采集入口（常驻调用）。codes: [股票代码]；ctx 未使用（数据源为港交所）。"""
    full_code = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    if "." in full_code:
        market_prefix, symbol = full_code.split(".", 1)
    else:
        market_prefix, symbol = "HK", full_code

    if market_prefix.upper() != "HK":
        print(f"全日沽空数据 ({full_code}): 仅支持港股")
        return

    data = get_fullday_short_selling(symbol, debug=debug)
    currency = get_currency(full_code)

    if data:
        print(f"全日沽空数据 ({full_code})")
        print(f"股票名称: {data['name']}")
        print(f"数据日期: {data['date']}")
        print(f"更新时间: {data['update_time']}")
        print(f"全日沽空股数: {data['volume']:,}")
        print(f"全日沽空金额: {data['amount']:.2f} 亿{currency}")

        try:
            with get_conn() as conn:
                upsert(conn, "daily_short_selling", {
                    "stock_code": full_code,
                    "trade_date": date.today(),
                    "stock_name": data['name'],
                    "data_date": data['date'],
                    "short_selling_vol": data['volume'],
                    "short_selling_amt": data['amount'],
                }, conflict_cols=["stock_code", "trade_date"])
        except Exception as e:
            print(f"[DB] 全日沽空入库失败: {e}")
    else:
        print(f"全日沽空数据 ({full_code}): 暂无数据（今日全日沽空尚未发布或无此标的沽空记录）")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run([args.code], debug=args.debug)
