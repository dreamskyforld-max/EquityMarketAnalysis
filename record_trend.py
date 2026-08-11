#!/usr/bin/env python3
"""
盘中趋势记录 — 每1分钟采集实时数据，写入 trend_snapshot（常驻调用版）

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用。
不再用 subprocess，直接调用各 get_realtime_* 模块的 run() 拿结构化数据。

注：趋势序列的唯一落盘目标是 trend_snapshot 表。企业微信 Bot 的趋势渲染
也直接读该表（见 wecom_*_collector.fetch_trend_from_db），不再维护本地 JSON
缓存，避免每分钟全量读写随文件增长线性膨胀的 I/O 开销。
"""
import sys, os
from datetime import datetime
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

import get_realtime_trade_direction as m_dir
import get_realtime_order_size as m_flow
import get_realtime_excess_return as m_excess
import get_realtime_order_book as m_book

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _fmt_order(order_list):
    if not order_list:
        return "N/A"
    return " ".join([f"{p:.1f}({v//1000}K)" for p, v in [(b[0], b[1]) for b in order_list]])


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: 代码列表；ctx: 共享行情上下文（可空）。"""
    ctx = ctx or get_shared_ctx()
    from futu import RET_OK
    from get_realtime_excess_return import BENCH_BY_MARKET

    # ── 合并 snapshot 调用 ──
    # 原来每只股票的 trade_direction / excess_return 各调一次 get_market_snapshot，
    # 加上 get_quote 也调一次，同一只股票的 snapshot 被重复调用 3~4 次。
    # 现在一次性批量获取所有 stock_code + bench_code 的 snapshot，分发给各子模块复用。
    all_codes = list(codes or [])
    bench_codes = []
    for full_code in all_codes:
        prefix = full_code.split(".", 1)[0].upper() if "." in full_code else "HK"
        bc = BENCH_BY_MARKET.get(prefix)
        if bc:
            bench_codes.append(bc)
    snap_codes = list(set(all_codes + bench_codes))

    batch_snapshot = None
    if snap_codes:
        print(f"[记录] 调用 get_market_snapshot({snap_codes}) ...")
        ret, snap = ctx.get_market_snapshot(snap_codes)
        if ret == RET_OK and not snap.empty:
            batch_snapshot = snap
        print(f"[记录] get_market_snapshot 返回 ret={ret}, batch_snapshot={'有' if batch_snapshot is not None else '无'}")

    for full_code in all_codes:
        market_prefix, symbol = full_code.split(".") if "." in full_code else ("HK", full_code)
        currency = "港元" if full_code.startswith("HK.") else "元"

        now = datetime.now()
        print(f"[记录] 处理 {full_code} 开始")

        # 直接调用各实时模块 run() 拿结构化数据（共享 ctx，无 subprocess 冷启动）
        # trade_direction / excess_return 只需要 snapshot，复用批量快照避免重复调用
        print(f"[记录] -> m_dir.run (trade_direction) ...")
        d_dir = m_dir.run([full_code], ctx, snapshot=batch_snapshot)
        print(f"[记录] <- m_dir.run 完成: {type(d_dir)}")
        print(f"[记录] -> m_flow.run (order_size) ...")
        d_flow = m_flow.run([full_code], ctx)          # 需 get_capital_distribution，无法复用
        print(f"[记录] <- m_flow.run 完成: {type(d_flow)}")
        print(f"[记录] -> m_excess.run (excess_return) ...")
        d_excess = m_excess.run([full_code], ctx, snapshot=batch_snapshot)
        print(f"[记录] <- m_excess.run 完成: {type(d_excess)}")
        print(f"[记录] -> m_book.run (order_book) ...")
        d_book = m_book.run([full_code], ctx)           # 需 subscribe + get_order_book，无法复用
        print(f"[记录] <- m_book.run 完成: {type(d_book)}")

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

        # 写入数据库（trend_snapshot 表是唯一落盘目标；企业微信趋势渲染也读此表）
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
            print(f"[记录] {full_code} 已写入 trend_snapshot (price={price})")
        except Exception as e:
            print(f"[记录] DB入库失败: {e}")


if __name__ == "__main__":
    full_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([full_code])
