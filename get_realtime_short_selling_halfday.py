#!/usr/bin/env python3
"""
港股半日沽空数据 — 港交所官方半日快照
数据源：https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/mshtmain_c.htm
发布时间：每个交易日约 12:00-12:30
用法：
    python3 get_realtime_short_selling_halfday.py HK.00700
"""
import sys, re, urllib.request
from datetime import datetime, date

# ---------- 工具函数 ----------
def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

# ---------- 数据获取 ----------
def get_halfday_short_selling(symbol, debug=False):
    url = "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/mshtmain_c.htm"
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

    # 替换全角空格为半角空格，避免正则\s不认
    text = text.replace('\u3000', ' ')

    # 提取日期
    date_match = re.search(r'日期\s*:\s*(\d{1,2}\s+\w+\s+\d{4})', text)
    data_date = date_match.group(1) if date_match else date.today().strftime('%d %b %Y')

    # 将 symbol 归一化为无前导零的数字字符串（如 "700"）
    try:
        normalized_symbol = str(int(symbol))
    except ValueError:
        normalized_symbol = symbol

    if debug:
        print(f"[调试] 查找代码: {normalized_symbol}")

    # ★ 修正后的正则：代码为1-5位数字（右对齐），字段间至少两个空格
    pattern = r'^\s*(\d{1,5})\s{2,}(.+?)\s{2,}([\d,]+)\s+([\d,]+)$'
    lines = text.split('\n')

    for line in lines:
        # 跳过人民币柜台（行首带 %）
        if line.lstrip().startswith('%'):
            continue

        m = re.match(pattern, line.strip())
        if not m:
            continue

        code = m.group(1)
        name = m.group(2).strip()
        vol_str = m.group(3)
        amount_str = m.group(4)

        # 匹配目标代码（排除以8开头的5位代码，即人民币柜台，如80700）
        if code == normalized_symbol and not code.startswith('8'):
            volume = int(vol_str.replace(',', ''))
            amount = float(amount_str.replace(',', '')) / 1e8   # 亿港元
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

# ---------- 主程序 ----------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default="HK.00700")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    full_code = args.code
    if "." in full_code: market_prefix, symbol = full_code.split(".", 1)
    else: market_prefix, symbol = "HK", full_code

    if market_prefix.upper() != "HK":
        print(f"半日沽空数据 ({full_code}): 仅支持港股")
        sys.exit(0)

    data = get_halfday_short_selling(symbol, debug=args.debug)
    currency = get_currency(full_code)

    if data:
        print(f"半日沽空数据 ({full_code})")
        print(f"股票名称: {data['name']}")
        print(f"数据日期: {data['date']}")
        print(f"更新时间: {data['update_time']}")
        print(f"上午沽空股数: {data['volume']:,}")
        print(f"上午沽空金额: {data['amount']:.2f} 亿{currency}")
    else:
        print(f"半日沽空数据 ({full_code}): 暂无数据（今日半日沽空尚未发布或无此标的沽空记录）")