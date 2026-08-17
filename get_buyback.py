#!/usr/bin/env python3
"""
港股公司回购数据 — 东方财富（全量版 + 常驻调用）

数据源：
- 全量模式：东财数据中心 datacenter-web API（RPT_HK_BUYBACK，全市场回购明细，
  按 TRADE_DATE 过滤分页拉取最近 FULL_DAYS 天）。
- 单票模式：hk.eastmoney.com/buyback.html 静态页（兼容 wecom 手动单票触发，输出格式不变）。
写入表：daily_buyback_event（按 stock_code + buyback_date 去重，ON CONFLICT DO UPDATE）。
仅支持港股（HK）。
"""
import sys, re, json, urllib.request, urllib.parse
from datetime import date, timedelta
from db import get_conn, bulk_upsert

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_HK_BUYBACK"
FULL_DAYS = 90      # 全量模式拉取最近 N 天回购明细
PAGE_SIZE = 500     # 东财 datacenter 单页上限

_DATACENTER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://data.eastmoney.com/",
}

def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def _http_get_json(url):
    req = urllib.request.Request(url, headers=_DATACENTER_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))

def fetch_full_buyback(days=FULL_DAYS):
    """全市场回购明细分页拉取（最近 days 天，按 TRADE_DATE 倒序）。失败返回 None。"""
    start = (date.today() - timedelta(days=days)).isoformat()
    flt = urllib.parse.quote(f"(TRADE_DATE>='{start}')")
    records = []
    page = 1
    while True:
        url = (f"{DATACENTER_URL}?reportName={REPORT_NAME}&columns=ALL"
               f"&pageNumber={page}&pageSize={PAGE_SIZE}"
               f"&sortColumns=TRADE_DATE&sortTypes=-1&filter={flt}")
        try:
            data = _http_get_json(url)
        except Exception as e:
            print(f"[调试] 全量回购分页失败 p{page}: {e}")
            break
        rows = (data or {}).get("result") or {}
        page_data = rows.get("data") or []
        records.extend(page_data)
        count = rows.get("count") or 0
        if not page_data or page * PAGE_SIZE >= count:
            break
        page += 1
    return records or None

def _records_to_db_rows(records):
    """东财接口原始记录 → daily_buyback_event 行列表（纯函数，可单测）。

    按 (stock_code, buyback_date) 去重：接口按 TRADE_DATE 倒序返回，
    同一天多条回购公告取最新一条（与单票模式一天一条口径一致）。
    注意：接口无最高/最低成交价，high_price/low_price 置 None。
    """
    db_rows = []
    seen = set()
    for r in records:
        sec = str(r.get("SECURITY_CODE") or "").strip()
        if not sec.isdigit():
            continue
        code = f"HK.{int(sec):05d}"
        raw_date = str(r.get("TRADE_DATE") or "")[:10]
        try:
            date.fromisoformat(raw_date)
        except ValueError:
            continue
        key = (code, raw_date)
        if key in seen:
            continue
        seen.add(key)
        db_rows.append({
            "stock_code": code,
            "buyback_date": raw_date,
            "volume": int(r.get("REPO_NUM") or 0),
            "high_price": None,
            "low_price": None,
            "avg_price": r.get("AVG_PRICE"),
            "amount": r.get("REPO_AMT"),
        })
    return db_rows

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

def _run_single(full_code, debug=False):
    """单票模式（wecom 手动触发）：原静态页逻辑，输出格式不变。"""
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

def _run_full(debug=False):
    """全量模式（全局任务）：东财数据中心全市场回购明细分页拉取并全量入库。"""
    records = fetch_full_buyback()
    if not records:
        print("公司回购数据: 全市场获取失败或无数据")
        return
    db_rows = _records_to_db_rows(records)
    if not db_rows:
        print("公司回购数据: 全市场无有效回购记录")
        return
    try:
        with get_conn() as conn:
            bulk_upsert(conn, "daily_buyback_event", db_rows, conflict_cols=["stock_code", "buyback_date"])
    except Exception as e:
        print(f"[DB] 回购数据入库失败: {e}")
        return
    n_stock = len({r["stock_code"] for r in db_rows})
    print(f"公司回购数据: 全市场入库 {len(db_rows)} 条回购记录"
          f"（最近{FULL_DAYS}天，{n_stock} 只标的）")

def run(codes=None, ctx=None, debug=False):
    """采集入口（常驻调用）。

    codes: None/空 = 全量采集全市场回购明细入库（全局任务用法）；
           给定 = 单票采集（兼容 wecom 手动触发，输出格式不变）。
    ctx 未使用（数据源为东方财富）。
    """
    if codes and len(codes) > 0:
        _run_single(codes[0], debug=debug)
    else:
        _run_full(debug=debug)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default=None,
                        help="股票代码（如 HK.00700）；不填 = 全量采集全市场回购明细")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run([args.code] if args.code else None, debug=args.debug)
