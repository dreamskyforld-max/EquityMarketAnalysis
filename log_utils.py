#!/usr/bin/env python3
"""
统一日志配置工具。

所有常驻服务统一使用: 项目根 ./log 目录 + 按天滚动 + 保留 N 天 + 同时输出 stdout。

用法:
    from log_utils import setup_logger
    log = setup_logger("scheduler")          # -> log/scheduler.log
    log = setup_logger("ticker_collector")   # -> log/ticker_collector.log

    # 日志文件按天滚动: 当前写 log/<name>.log, 跨天自动切为 log/<name>.log.YYYY-MM-DD
    # 保留天数默认 10 天, 可用环境变量 LOG_KEEP_DAYS 覆盖。
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler

# 日志目录: 本文件所在目录(项目根)下的 log/
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")

# 保留天数(默认 10 天)
LOG_KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "10"))


def setup_logger(name, filename=None, level=logging.INFO):
    """
    配置根日志器并返回 name 对应的日志器。

    配置的是 root logger, 这样被导入的子模块(collector_runtime/db/get_*.py 等)
    的日志都会 propagate 到 root, 统一输出到同一个文件(与原 basicConfig 行为一致)。

    - 文件: log/{filename or name}.log, 按天滚动(midnight), 保留 LOG_KEEP_DAYS 天
    - stdout: 供 systemd journal 捕获
    - 幂等: 根日志器已配置过则不再重复添加 handler
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    if filename is None:
        filename = f"{name}.log"

    root = logging.getLogger()
    if not getattr(root, "_eq_configured", False):
        root.setLevel(level)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        fh = TimedRotatingFileHandler(
            os.path.join(LOG_DIR, filename),
            when="midnight",
            backupCount=LOG_KEEP_DAYS,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

        root._eq_configured = True

    return logging.getLogger(name)
