#!/usr/bin/env python3
"""
定时清理过期数据（常驻调用版）

清理规则：每张表独立保留期
  - 行情/资金流/趋势类（3 年）：
      tick_data, realtime_order_size, trend_snapshot
  - 采集运行日志（3 年）：
      collection_run_log
  - 监控明细（30 天滚动，不需要长期保留）：
      collection_task_log, collection_api_log

常驻调用：run(codes, ctx)。__main__ 保留独立运行。
"""
from db import get_conn

# (table, time_col, keep_interval) —— keep_interval 是 Postgres INTERVAL 字面量
RULES = [
    ("tick_data",              "tick_time",     "36 months"),
    ("realtime_order_size",    "snapshot_time", "36 months"),
    ("trend_snapshot",         "snapshot_time", "36 months"),
    ("collection_run_log",     "run_time",      "36 months"),
    # 监控明细 30 天滚动
    ("collection_api_log",     "called_at",     "30 days"),
    ("collection_task_log",    "started_at",    "30 days"),
]


def run(codes=None, ctx=None):
    """清理过期数据（codes/ctx 未使用）。"""
    total_deleted = 0
    with get_conn() as conn:
        cur = conn.cursor()
        for table, time_col, keep in RULES:
            cur.execute(
                f"DELETE FROM {table} WHERE {time_col} < NOW() - INTERVAL '{keep}'"
            )
            deleted = cur.rowcount
            total_deleted += deleted
            print(f"[清理] {table}: 保留 {keep}，删除 {deleted} 条")
    print(f"[清理] 完成，共删除 {total_deleted} 条记录")


if __name__ == "__main__":
    run()
