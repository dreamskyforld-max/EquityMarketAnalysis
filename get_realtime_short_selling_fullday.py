#!/usr/bin/env python3
"""
港股全日沽空数据 — 港交所官方全日快照（全市场全量采集版）

数据源：https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm
发布时间：每个交易日约 18:00-19:00
一次请求抓取全市场页面，解析全部港股沽空数据并全量入库 daily_short_selling。

常驻调用：run(codes, ctx)。
- codes 为 None/空 → 全量入库全部港股（全局任务用法）
- codes 给定 → 仅按代码过滤入库（兼容 wecom 手动单票触发）
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

def parse_fullday_page(html, debug=False):
    """从港交所全日沽空页面 HTML 解析全市场沽空记录。

    返回 [{code, name, volume, amount, date}, ...]；页面解析失败返回 None。
    - code: 页面原始数字代码（如 "700"）
    - amount: 亿港元
    - 排除人民币柜台（5位以 8 开头，如 80700）
    """
    pre_match = re.search(r'<pre>(.*?)</pre>', html, re.DOTALL)
    if not pre_match:
        if debug:
            print("[调试] 未找到 <pre> 标签")
        return None

    text = pre_match.group(1)
    text = text.replace('\u3000', ' ')

    date_match = re.search(r'日期\s*:\s*(\d{1,2}\s+\w+\s+\d{4})', text)
    data_date = date_match.group(1) if date_match else date.today().strftime('%d %b %Y')

    pattern = r'^\s*(\d{1,5})\s{2,}(.+?)\s{2,}([\d,]+)\s+([\d,]+)$'
    lines = text.split('\n')

    records = []
    for line in lines:
        # 跳过人民币柜台（行首带 %）
        if line.lstrip().startswith('%'):
            continue

        m = re.match(pattern, line.strip())
        if not m:
            continue

        code = m.group(1)
        # 排除 5 位以 8 开头的人民币柜台代码（如 80700）
        if code.startswith('8'):
            continue

        name = m.group(2).strip()
        volume = int(m.group(3).replace(',', ''))
        amount = float(m.group(4).replace(',', '')) / 1e8   # 亿港元
        records.append({
            "code": code,
            "name": name,
            "volume": volume,
            "amount": amount,
            "date": data_date,
        })

    return records

def get_fullday_short_selling_all(debug=False):
    """抓取港交所全日沽空快照页，返回全市场沽空记录列表；失败返回 None。"""
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

    return parse_fullday_page(html, debug)

def _norm_symbol(sym):
    """把带前导零的代码（00700）归一化为页面格式（700）。"""
    try:
        return str(int(sym))
    except ValueError:
        return sym

def run(codes=None, ctx=None, debug=False):
    """采集入口（常驻调用）。

    codes: 股票代码列表；None/空 = 全量采集全港股并入库；给定 = 仅按代码过滤入库。
    ctx 未使用（数据源为港交所）。
    """
    records = get_fullday_short_selling_all(debug=debug)
    if not records:
        print("全日沽空数据: 获取失败或页面无数据")
        return

    # 过滤：codes 给定则只保留这些票（兼容 wecom 手动单票触发）
    want = {_norm_symbol(c.partition(".")[2]) for c in (codes or [])
            if c.partition(".")[0].upper() == "HK"}
    if want:
        records = [r for r in records if r["code"] in want]
        if not records:
            print("全日沽空数据: 无匹配股票")
            return

    # 全量入库
    inserted = 0
    failed = 0
    with get_conn() as conn:
        for r in records:
            full_code = f"HK.{int(r['code']):05d}"
            try:
                upsert(conn, "daily_short_selling", {
                    "stock_code": full_code,
                    "trade_date": date.today(),
                    "stock_name": r["name"],
                    "data_date": r["date"],
                    "short_selling_vol": r["volume"],
                    "short_selling_amt": r["amount"],
                }, conflict_cols=["stock_code", "trade_date"])
                inserted += 1
            except Exception as e:
                failed += 1
                print(f"[DB] 全日沽空入库失败 {full_code}: {e}")

    # 输出：单票保持原格式（兼容 wecom 渲染），全量输出汇总
    if len(records) == 1:
        r = records[0]
        full_code = f"HK.{int(r['code']):05d}"
        print(f"全日沽空数据 ({full_code})")
        print(f"股票名称: {r['name']}")
        print(f"数据日期: {r['date']}")
        print(f"更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"全日沽空股数: {r['volume']:,}")
        print(f"全日沽空金额: {r['amount']:.2f} 亿港元")
    else:
        total_amt = sum(r["amount"] for r in records)
        print(f"全日沽空数据: 全市场入库 {inserted}/{len(records)} 只股票"
              f"（失败 {failed}），数据日期 {records[0]['date']}，"
              f"全市场沽空金额 {total_amt:.2f} 亿港元")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default=None,
                        help="股票代码（如 HK.00700）；不填 = 全量采集全港股")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run([args.code] if args.code else None, debug=args.debug)
