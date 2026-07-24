#!/usr/bin/env python3
"""
定时清理过期数据（常驻调用版）

清理规则：删除 3 年前的记录
涉及表：
  - tick_data              (tick_time)
  - realtime_order_size   (snapshot_time)
  - trend_snapshot        (snapshot_time)
  - collection_run_log    (run_time)

常驻调用：run(codes, ctx)。__main__ 保留独立运行。
"""
from db import get_conn

KEEP_MONTHS = 36
TABLES = [
    ("tick_data", "tick_time"),
    ("realtime_order_size", "snapshot_time"),
    ("trend_snapshot", "snapshot_time"),
    ("collection_run_log", "run_time"),
]

def run(codes=None, ctx=None):
    """清理过期数据（codes/ctx 未使用）。"""
    total_deleted = 0
    with get_conn() as conn:
        cur = conn.cursor()
        for table, time_col in TABLES:
            sql = f"DELETE FROM {table} WHERE {time_col} < NOW() - INTERVAL '{KEEP_MONTHS} months'"
            cur.execute(sql)
            deleted = cur.rowcount
            total_deleted += deleted
            print(f"[清理] {table}: 删除 {deleted} 条记录")
    print(f"[清理] 完成，共删除 {total_deleted} 条记录")


if __name__ == "__main__":
    run()
