#!/usr/bin/env python3
"""
融资余额日度变化（A股专用，常驻调用版）

常驻调用：run(codes, ctx)。__main__ 保留独立运行。
仅支持A股（SH/SZ）。原模块顶层逻辑整体搬入 run()。
"""
import sys, os, json, logging
logging.basicConfig(level=logging.WARNING)

import akshare as ak
import pandas as pd
from datetime import date, timedelta
from db import get_conn, bulk_upsert

# 模块级状态（由 run() 赋值，供辅助函数引用）
full_code = ""
market = ""
symbol = ""
CACHE_FILE = ""

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return None

def save_cache(records):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False)

# ---------- 统一输出逻辑 ----------
def print_margin_data(records, source=""):
    if not records:
        print(f"未找到 {full_code} 的融资余额数据")
        return
    df = pd.DataFrame(records).tail(10)
    df['融资余额(亿)'] = (df['融资余额'] / 1e8).round(2)
    pct = df['融资余额'].pct_change() * 100
    df['环比变化(%)'] = pct.apply(lambda x: f"{x:+.2f}%" if pd.notna(x) else "N/A")
    print(f"融资余额日度变化 ({full_code}) {source}")
    print(f"{'日期':<12} {'融资余额(亿)':>12} {'环比变化':>10}")
    for _, row in df.iterrows():
        d = row['日期']
        if len(d) == 8:
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        print(f"{d:<12} {row['融资余额(亿)']:>12.2f} {row['环比变化(%)']:>10}")

def save_margin_to_db(records):
    if not records or len(records) < 2:
        return
    try:
        db_data = []
        prev_balance = None
        for r in records:
            d = r['日期']
            balance = r['融资余额'] / 1e8
            change_pct = None
            if prev_balance is not None and prev_balance > 0:
                change_pct = round((balance - prev_balance) / prev_balance * 100, 4)
            prev_balance = balance
            db_data.append({
                "stock_code": full_code,
                "trade_date": date(int(d[:4]), int(d[4:6]), int(d[6:8])),
                "margin_balance": round(balance, 2),
                "change_pct": change_pct,
            })
        with get_conn() as conn:
            bulk_upsert(conn, "daily_margin_balance", db_data, conflict_cols=["stock_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 融资余额入库失败: {e}")

# ---------- 采集入口（常驻调用）----------
def run(codes=None, ctx=None):
    """codes: [股票代码]，如 SH.600900；仅支持A股。"""
    global full_code, market, symbol, CACHE_FILE
    full_code = codes[0] if (codes and len(codes) > 0) else "SH.600519"
    if not (full_code.startswith("SH.") or full_code.startswith("SZ.")):
        print(f"融资余额：{full_code} 当前股票不适用，目前仅支持A股（SH/SZ）")
        return

    market, symbol = full_code.split(".")
    CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              f"margin_cache_{symbol}.json")

    # ---------- 缓存命中 ----------
    cached = load_cache()
    if cached and len(cached) >= 10:
        from datetime import date as dt_date
        latest_date_str = cached[-1].get('日期', '')
        if len(latest_date_str) == 8:
            latest_date = dt_date(int(latest_date_str[:4]), int(latest_date_str[4:6]), int(latest_date_str[6:8]))
            if (dt_date.today() - latest_date).days <= 1:
                print_margin_data(cached, "[缓存]")
                save_margin_to_db(cached)
                return
            else:
                print(f"缓存已过期（最新: {latest_date_str}），重新拉取...")

    # ---------- 自然日循环拉取 ----------
    records = []
    today = date.today()
    for i in range(1, 21):
        d = today - timedelta(days=i)
        if len(records) >= 12:
            break
        date_str = d.strftime('%Y%m%d')
        try:
            if market == "SH":
                df = ak.stock_margin_detail_sse(date=date_str)
            else:
                df = ak.stock_margin_detail_szse(date=date_str)

            if df is None or df.empty:
                continue

            code_col = next((c for c in ['股票代码','标的证券代码','证券代码'] if c in df.columns), None)
            if not code_col:
                continue

            df[code_col] = df[code_col].astype(str).str.strip()
            row = df[df[code_col] == symbol]
            if row.empty:
                continue

            balance_col = next((c for c in ['融资余额','融资资金余额'] if c in row.columns), None)
            if not balance_col:
                continue

            rz_balance = float(row[balance_col].values[0])
            records.append({'日期': date_str, '融资余额': rz_balance})
        except Exception:
            continue

    if not records:
        print_margin_data([], "")
        return

    records.reverse()
    save_cache(records)
    print_margin_data(records, "")
    save_margin_to_db(records)


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "SH.600519"
    run([code])
