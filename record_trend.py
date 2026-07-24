#!/usr/bin/env python3
"""
盘中趋势记录 — 每1分钟采集实时数据，缓存至 JSON + 写入 trend_snapshot（常驻调用版）

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用。
不再用 subprocess，直接调用各 get_realtime_* 模块的 run() 拿结构化数据。
"""
import sys, os, json, glob
from datetime import datetime, date, timedelta
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

import get_realtime_trade_direction as m_dir
import get_realtime_order_size as m_flow
import get_realtime_excess_return as m_excess
import get_realtime_order_book as m_book

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPTS_DIR, "trend_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def get_latest_trading_day():
    today = date.today()
    d = today
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def clean_old_cache(prefix):
    today = date.today()
    if today.weekday() >= 5:
        latest = get_latest_trading_day()
    else:
        latest = today
    pattern = os.path.join(CACHE_DIR, f"{prefix}_*.json")
    for fpath in glob.glob(pattern):
        fname = os.path.basename(fpath)
        try:
            date_str = fname.replace(prefix, "").replace(".json", "").strip("_")
            file_date = datetime.strptime(date_str, "%Y%m%d").date()
            if file_date < latest:
                os.remove(fpath)
                print(f"[清理] 已删除旧缓存: {fpath}")
        except Exception:
            continue


def _fmt_order(order_list):
    if not order_list:
        return "N/A"
    return " ".join([f"{p:.1f}({v//1000}K)" for p, v in [(b[0], b[1]) for b in order_list]])


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享行情上下文（可空）。"""
    ctx = ctx or get_shared_ctx()
    for full_code in (codes or []):
        market_prefix, symbol = full_code.split(".") if "." in full_code else ("HK", full_code)
        currency = "港元" if full_code.startswith("HK.") else "元"

        file_prefix = f"trend_{full_code.replace('.', '_')}"
        clean_old_cache(file_prefix)

        now = datetime.now()
        timestamp = now.strftime('%H:%M')
        date_str = now.strftime('%Y%m%d')

        # 直接调用各实时模块 run() 拿结构化数据（共享 ctx，无 subprocess 冷启动）
        d_dir = m_dir.run([full_code], ctx)
        d_flow = m_flow.run([full_code], ctx)
        d_excess = m_excess.run([full_code], ctx)
        d_book = m_book.run([full_code], ctx)

        # 提取数值
        price = d_dir.get('price') if d_dir else None
        bid = d_dir.get('bid_vol') if d_dir else None
        ask = d_dir.get('ask_vol') if d_dir else None
        ratio = (bid / ask) if (d_dir and bid and ask and ask > 0) else None
        volume = d_dir.get('volume') if d_dir else None
        turnover = d_dir.get('turnover') if d_dir else None
        super_in = d_flow.get('super_net') if d_flow else None
        big_in = d_flow.get('big_net') if d_flow else None
        mid_in = d_flow.get('mid_net') if d_flow else None
        small_in = d_flow.get('small_net') if d_flow else None
        excess = d_excess.get('excess') if d_excess else None
        bids = d_book.get('bids', []) if d_book else []
        asks = d_book.get('asks', []) if d_book else []

        buy_str = _fmt_order(bids)
        sell_str = _fmt_order(asks)

        record = {
            "time": timestamp,
            "price": price,
            "super_in": super_in,
            "big_in": big_in,
            "mid_in": mid_in,
            "small_in": small_in,
            "ratio": ratio,
            "excess": excess,
            "volume": volume,
            "turnover": turnover,
            "buy_str": buy_str,
            "sell_str": sell_str,
            "raw": {
                "trade_direction": m_dir.format_text(d_dir, full_code, currency) if d_dir else None,
                "order_size": m_flow.format_text(d_flow, full_code, currency) if d_flow else None,
                "excess_return": m_excess.format_text(d_excess, full_code, currency) if d_excess else None,
                "order_book": m_book.format_text(d_book, full_code, currency) if d_book else None,
            }
        }

        # 写入 JSON 缓存（按时间戳去重）
        file_name = f"{file_prefix}_{date_str}.json"
        file_path = os.path.join(CACHE_DIR, file_name)
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = []
        else:
            data = []

        replaced = False
        for i, item in enumerate(data):
            if item.get("time") == timestamp:
                data[i] = record
                replaced = True
                break
        if not replaced:
            data.append(record)

        with open(file_path, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"[记录] 已写入 {file_path}，当前记录总数: {len(data)}")

        # 写入数据库
        try:
            with get_conn() as conn:
                upsert(conn, "trend_snapshot", {
                    "stock_code": full_code,
                    "snapshot_time": now,
                    "price": price,
                    "super_in_net": super_in,
                    "big_in_net": big_in,
                    "mid_in_net": mid_in,
                    "small_in_net": small_in,
                    "buy_sell_ratio": ratio,
                    "excess_return_pct": excess,
                    "volume": volume,
                    "turnover": turnover,
                    "buy_levels_str": buy_str,
                    "sell_levels_str": sell_str,
                }, conflict_cols=["stock_code", "snapshot_time"])
        except Exception as e:
            print(f"[记录] DB入库失败: {e}")


if __name__ == "__main__":
    full_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([full_code])
