#!/usr/bin/env python3
"""
港股公司回购数据 — 东方财富静态页面（常驻调用版）

数据源：https://hk.eastmoney.com/buyback.html?code=00700&sdate=YYYY-MM-DD&edate=YYYY-MM-DD
常驻调用：run(codes, ctx)。__main__ 保留独立运行。
仅支持港股（HK）。
"""
import sys, re, urllib.request
from datetime import date
from db import get_conn, bulk_upsert

def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def get_buyback(symbol, debug=False):
    today = date.today()
    start_date = f"{today.year}-01-01"
    end_date = today.strftime('%Y-%m-%d')

    url = f"https://hk.eastmoney.com/buyback.html?code={symbol}&sdate={start_date}&edate={end_date}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://hk.eastmoney.com/"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        if debug:
            print(f"[调试] 请求失败: {e}")
        return None

    tr_blocks = re.findall(r'<tr>\s*(.*?)\s*</tr>', html, re.DOTALL)
    records = []

    for block in tr_blocks:
        spans = re.findall(r'<span>(.*?)</span>', block)
        if len(spans) < 7:
            continue
        try:
            seq = spans[0].strip()
            if not seq.isdigit():
                continue
            volume_raw = spans[1].strip()
            high_price = float(spans[2])
            low_price = float(spans[3])
            avg_price = float(spans[4])
            amount_raw = spans[5].strip()
            record_date = spans[6].strip()

            volume = int(float(volume_raw.replace('万', '')) * 1e4)
            amount = float(amount_raw.replace('万', '')) * 1e4

            records.append({
                "date": record_date, "volume": volume,
                "high_price": high_price, "low_price": low_price,
                "avg_price": avg_price, "amount": amount,
            })
        except (ValueError, IndexError):
            continue

    if not records:
        if debug:
            print(f"[调试] 未找到 {symbol} 的回购记录")
        return None

    latest = records[0]
    total_volume = sum(r['volume'] for r in records)
    total_amount = sum(r['amount'] for r in records)

    return {
        "latest": latest, "recent": records[:5],
        "total_records": len(records),
        "total_volume": total_volume, "total_amount": total_amount,
    }

def run(codes=None, ctx=None, debug=False):
    """采集入口（常驻调用）。codes: [股票代码]；ctx 未使用（数据源为东方财富）。"""
    full_code = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    if "." in full_code:
        market_prefix, symbol = full_code.split(".", 1)
    else:
        market_prefix, symbol = "HK", full_code

    if market_prefix.upper() != "HK":
        print(f"公司回购数据 ({full_code}): 仅支持港股")
        return

    data = get_buyback(symbol, debug=debug)
    currency = get_currency(full_code)

    if data:
        latest = data['latest']
        print(f"公司回购数据 ({full_code})")
        print(f"数据日期: {latest['date']}")
        print(f"最新回购:")
        print(f"  回购数量: {latest['volume']:,} 股")
        print(f"  回购均价: {fmt_price(latest['avg_price'])} {currency}")
        print(f"  回购总额: {latest['amount']/1e8:.2f} 亿{currency}")
        print(f"  回购价格区间: {fmt_price(latest['low_price'])} - {fmt_price(latest['high_price'])} {currency}")
        print(f"年内累计回购 {data['total_records']} 次")
        print(f"  累计数量: {data['total_volume']:,} 股")
        print(f"  累计金额: {data['total_amount']/1e8:.2f} 亿{currency}")

        print(f"\n最近 5 次回购明细:")
        for i, r in enumerate(data['recent'], 1):
            print(f"  {i}. {r['date']}  {r['volume']:,}股  均价{fmt_price(r['avg_price'])}  总额{r['amount']/1e8:.2f}亿{currency}")

        try:
            all_records = {r['date']: r for r in data['recent']}
            all_records[data['latest']['date']] = data['latest']
            db_data = []
            for r in all_records.values():
                db_data.append({
                    "stock_code": full_code,
                    "buyback_date": date.fromisoformat(r['date']),
                    "volume": r['volume'], "high_price": r['high_price'],
                    "low_price": r['low_price'], "avg_price": r['avg_price'],
                    "amount": r['amount'],
                })
            with get_conn() as conn:
                bulk_upsert(conn, "daily_buyback_event", db_data, conflict_cols=["stock_code", "buyback_date"])
        except Exception as e:
            print(f"[DB] 回购数据入库失败: {e}")
    else:
        print(f"公司回购数据 ({full_code}): 暂无数据（该股票年内无回购记录）")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run([args.code], debug=args.debug)
