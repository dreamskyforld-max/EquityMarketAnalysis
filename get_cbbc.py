#!/usr/bin/env python3
"""
牛熊证（CBBC）街货分布采集（常驻调用版）

数据源：港交所 CBBC 完整列表 CSV（sc.hkex.com.hk）
写入表：daily_cbbc（按 stock_code + trade_date 去重）
常驻调用：run(codes, ctx)。__main__ 保留独立运行。

注意：仅适用于港股，A股无牛熊证。
"""
import sys, csv, urllib.request
from datetime import date
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

# 港交所 CBBC 完整列表 CSV 下载地址
CSV_URL = "https://sc.hkex.com.hk/TuniS/www.hkex.com.hk/eng/cbbc/search/cbbcFullList.csv"

def get_cbbc(symbol="00700"):
    try:
        req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        print(f"下载失败: {e}")
        return None

    for enc in ['utf-16', 'utf-8', 'latin-1']:
        try:
            decoded = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        print("编码失败")
        return None

    lines = decoded.splitlines()
    reader = csv.DictReader(lines[1:], delimiter='\t')
    reader.fieldnames = [f.strip() for f in reader.fieldnames]

    bulls, bears = [], []
    for row in reader:
        ul = (row.get("UL") or "").strip()
        if ul != symbol:
            continue
        try:
            os_pct = float(row.get("O/S (%)", "0") or "0")
        except ValueError:
            continue
        if os_pct <= 0:
            continue
        try:
            total = int(row.get("Total Issue Size", "0").replace(",", ""))
        except ValueError:
            continue
        street_vol = int(total * os_pct / 100)
        entry = {"回收价": float(row.get("Call Level", 0) or 0), "街货量(张)": street_vol}
        bear_bull = (row.get("Bull/Bear") or "").strip()
        if bear_bull.startswith("Bull"):
            bulls.append(entry)
        elif bear_bull.startswith("Bear"):
            bears.append(entry)

    if not bulls or not bears:
        return "未找到牛熊证数据"

    top_bull = max(bulls, key=lambda x: x["街货量(张)"])
    top_bear = max(bears, key=lambda x: x["街货量(张)"])

    return {
        "牛证回收价": f"{top_bull['回收价']:.2f} 港元",
        "牛证街货量(张)": top_bull["街货量(张)"],
        "熊证回收价": f"{top_bear['回收价']:.2f} 港元",
        "熊证街货量(张)": top_bear["街货量(张)"],
    }

def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: [股票代码]；ctx 未使用（数据源为港交所）。"""
    code = codes[0] if (codes and len(codes) > 0) else "HK.00700"
    if "." in code:
        market_prefix, symbol = code.split(".")
    else:
        market_prefix, symbol = "HK", code
    full_code = f"{market_prefix}.{symbol}"

    print(f"牛熊证街货分布 ({full_code})")
    data = get_cbbc(symbol)
    if isinstance(data, str):
        print(data)
        return

    for k, v in data.items():
        print(f"{k}: {v}")

    # 写入 daily_cbbc 表
    try:
        with get_conn() as conn:
            upsert(conn, "daily_cbbc", {
                "stock_code": full_code,
                "trade_date": date.today(),
                "bull_call_level": float(data["牛证回收价"].replace(" 港元", "")),
                "bull_street_volume": data["牛证街货量(张)"],
                "bear_call_level": float(data["熊证回收价"].replace(" 港元", "")),
                "bear_street_volume": data["熊证街货量(张)"],
            }, conflict_cols=["stock_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 牛熊证入库失败: {e}")


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([code])
