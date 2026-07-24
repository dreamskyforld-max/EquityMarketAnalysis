#!/usr/bin/env python3
"""
北向资金 —— 东方财富版（市场整体净流入，近5个有效交易日）
"""
import sys, re, urllib.request
from datetime import date, timedelta
from db import get_conn, bulk_upsert

def get_eastmoney_north_data(date_str):
    """从东方财富页面抓取指定日期的北向净买入"""
    url = f"https://data.eastmoney.com/hsgt/index/{date_str}.html"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        # 东方财富页面的北向净流入通常在某个class下
        match = re.search(r'净买入[：:]\s*</span>\s*<span[^>]*>([0-9\.\-]+)亿', html)
        if not match:
            match = re.search(r'净流入[：:]\s*</span>\s*<span[^>]*>([0-9\.\-]+)亿', html)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None

# ---------- 主程序 ----------
stock_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"  # 参数仅用于兼容原有调用
print("=" * 50)
print("【北向资金 - 市场整体净流入（东方财富）】")
print("注意：以下为市场总量数据，非个股数据。无效数据已自动过滤。")
print()

records = []
today = date.today()
for i in range(7):
    d = today - timedelta(days=i)
    date_str = d.strftime('%Y%m%d')
    net = get_eastmoney_north_data(date_str)
    if net is not None and net != 0.0:
        records.insert(0, {'日期': date_str, '北向合计净流入(亿)': round(net, 2)})

recent = records[-5:] if len(records) > 5 else records
if not recent:
    print("最近5个交易日暂无有效北向资金数据")
else:
    print(f"{'日期':<12} {'北向合计净流入(亿)':>18}")
    for rec in recent:
        d = rec['日期']
        if len(d) == 8:
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        print(f"{d:<12} {rec['北向合计净流入(亿)']:>18.2f}")

# 写入数据库
if records:
    try:
        db_data = []
        for rec in records:
            d = rec['日期']
            trade_date = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
            db_data.append({
                "trade_date": trade_date,
                "net_inflow": rec['北向合计净流入(亿)'],
            })
        with get_conn() as conn:
            bulk_upsert(conn, "daily_northbound_flow", db_data, conflict_cols=["trade_date"])
    except Exception as e:
        print(f"[DB] 北向资金入库失败: {e}")