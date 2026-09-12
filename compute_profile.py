#!/usr/bin/env python3
"""全量画像每日计算任务（供 market_scheduler.py 以 run(codes, ctx) 形式调用）。

每个工作日收盘后（默认 16:30，见 market_scheduler.py 的 GLOBAL_TASKS 注册）
计算当日全量画像，等价于手动执行：
    python3 -m profiling.run compute --as-of <今日> --force

行为：
- 写入 profile.tag_value（版本化：值变化才产生新版本，未变标签沿用上一有效版本）。
- 依赖的当日行情快照由同调度器的「收盘采集」任务（A 股 15:10 / 港股 16:20）先行入库，
  故 16:30 计算已能拿到当日收盘价。
- force=True 跳过各标签的 update_freq 更新门禁，确保全量重算（未变标签不产生冗余版本）。
- codes / ctx 由调度器传入，但本任务为全局全量，忽略二者。
"""
from datetime import date
from db import get_conn
from profiling import engine
from log_utils import setup_logger

log = setup_logger("compute_profile")


def run(codes=None, ctx=None):
    """计算当日全量画像。异常直接向上冒泡，由调度器记录为执行失败。"""
    as_of = date.today()
    log.info("【全量画像】开始计算 as_of=%s", as_of)
    with get_conn() as conn:
        results = engine.compute_all(conn, as_of=as_of, force=True, mode="auto")
    n = len(results) if results else 0
    log.info("【全量画像】完成 as_of=%s, 标签数=%d", as_of, n)
    return True
