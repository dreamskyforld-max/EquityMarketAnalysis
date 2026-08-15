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
import get_realtime_excess_return as m_excess

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


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
        # 富途 SDK 失败时不抛异常，而是返回 (ret=-1, 错误字符串)。
        # 必须打印 snap 本体，否则真实错误原因被静默丢弃，无法定位 ret=-1 根因。
        if ret == RET_OK and not snap.empty:
            batch_snapshot = snap
            print(f"[记录] get_market_snapshot 返回 ret={ret}, batch_snapshot=有({len(snap)}行)")
        else:
            print(f"[记录] get_market_snapshot 返回 ret={ret}, 错误: {snap!r}")

    # 批量调用各实时模块 run() 拿结构化数据（共享 ctx，无 subprocess 冷启动）
    # trade_direction / excess_return 只需要 snapshot，复用批量快照避免重复调用
    # 注：get_capital_distribution(m_flow) 与 get_order_book(m_book) 已停用——
    # 其产出字段(super/big/mid/small_in_net、buy/sell_levels_str)本项目未使用，
    # 且为逐票调用、无法复用批量快照，是加股票时的主要性能瓶颈，故跳过。
    # 一次性传入 all_codes，模块内部按 code 逐行筛值，不再逐票调用（API 仍为 1 次）。
    print(f"[记录] -> m_dir.run (trade_direction, 批量 {len(all_codes)} 只) ...")
    d_dir_list = m_dir.run(all_codes, ctx, snapshot=batch_snapshot)
    if not isinstance(d_dir_list, list):
        d_dir_list = [d_dir_list]
    print(f"[记录] <- m_dir.run 完成: {len(d_dir_list)} 条")
    print(f"[记录] -> m_excess.run (excess_return, 批量 {len(all_codes)} 只) ...")
    d_excess_list = m_excess.run(all_codes, ctx, snapshot=batch_snapshot)
    if not isinstance(d_excess_list, list):
        d_excess_list = [d_excess_list]
    print(f"[记录] <- m_excess.run 完成: {len(d_excess_list)} 条")

    # 按 stock_code 建立索引，便于与 all_codes 对齐（m_excess 对非支持市场会跳过）
    dir_by_code = {d.get('stock_code'): d for d in d_dir_list if isinstance(d, dict)}
    excess_by_code = {d.get('stock_code'): d for d in d_excess_list if isinstance(d, dict)}

    for full_code in all_codes:
        now = datetime.now()
        print(f"[记录] 处理 {full_code} 开始")

        d_dir = dir_by_code.get(full_code)
        d_excess = excess_by_code.get(full_code)

        # 提取数值
        price = d_dir.get('price') if d_dir else None
        bid = d_dir.get('bid_vol') if d_dir else None
        ask = d_dir.get('ask_vol') if d_dir else None
        ratio = (bid / ask) if (d_dir and bid and ask and ask > 0) else None
        volume = d_dir.get('volume') if d_dir else None
        turnover = d_dir.get('turnover') if d_dir else None
        excess = d_excess.get('excess') if d_excess else None

        # 写入数据库（trend_snapshot 表是唯一落盘目标；企业微信趋势渲染也读此表）
        # 四档 net 与盘口 str 列保留但置 None（历史数据已禁用，新采集不再填）
        try:
            with get_conn() as conn:
                upsert(conn, "trend_snapshot", {
                    "stock_code": full_code,
                    "snapshot_time": now,
                    "price": price,
                    "super_in_net": None,
                    "big_in_net": None,
                    "mid_in_net": None,
                    "small_in_net": None,
                    "buy_sell_ratio": ratio,
                    "excess_return_pct": excess,
                    "volume": volume,
                    "turnover": turnover,
                    "buy_levels_str": None,
                    "sell_levels_str": None,
                }, conflict_cols=["stock_code", "snapshot_time"])
            print(f"[记录] {full_code} 已写入 trend_snapshot (price={price})")
        except Exception as e:
            print(f"[记录] DB入库失败: {e}")


if __name__ == "__main__":
    full_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([full_code])
