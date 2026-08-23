#!/usr/bin/env python3
"""
采集层故障监控系统
================================
目标：监控「数据采集系统是否运行正常」，而非校验数据逻辑正确性。

监控维度（本次设计范围）：
  1. 任务执行层  —— 定时器/调度是否卡住、任务是否执行失败/超时
  2. 接口调用层  —— 富途等外部接口调用成功率、延迟
  3. 数据落地层  —— 数据表是否长时间未更新（采集到了却没落库 / 落库后停滞）

数据流：
  - 在统一入口埋点（market_scheduler.run_script / collector_runtime._LockedCtx）
    写入 collection_task_log / collection_api_log（原始明细，滚动保留）。
  - 检查器（check_health）周期性读取明细 + 关键数据表，
    评估是否存在故障，结果写入 collection_alert（故障表，前端 UI 读这张）。
  - 前端只读 collection_alert，不感知明细表。

用法：
  - 埋点：在采集入口调用 record_api_call / record_task_start / record_task_end
  - 常驻服务：python3 monitor_collector.py  （独立进程，自带循环，不依赖调度服务）
  - 单次巡检：python3 monitor_collector.py --once
  - 手动排查：python3 monitor_collector.py --check（打印当前告警与明细）
  - 也可被 scheduler 调用 run() 作为补充，但主用独立服务。
"""
import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, cast

from db import get_conn
from log_utils import setup_logger

log = setup_logger("monitor_collector")   # 统一日志: ./log 目录 + 按天滚动 + 保留 10 天

# ── 可调阈值 ──────────────────────────────────────────────
# 接口成功率告警阈值（滚动窗口内的失败率，0~1）
API_FAIL_RATE_WARN = 0.10      # >10% 警告
API_FAIL_RATE_CRIT = 0.30      # >30% 严重
# 单接口延迟告警阈值（秒）
API_SLOW_THRESHOLD = 5.0
# 数据表「最长允许未更新」阈值（分钟）。超过则判定采集停滞。
STALE_WARN_MIN = 15
STALE_CRIT_MIN = 40
# 任务失败率告警阈值（滚动窗口）
TASK_FAIL_RATE_WARN = 0.10
TASK_FAIL_RATE_CRIT = 0.30
# 滚动窗口大小（分钟）
WINDOW_MIN = 30

# 项目时区：所有盘中时间戳列（mkt_time / tick_time / snapshot_time / update_time /
# realtime_order_size.snapshot_time 等）均按 HKT(UTC+8) 写入。监控窗口起点必须用
# HKT 对齐，否则 09:00 UTC(=17:00 HKT) 会落在收盘后，误判"窗口内无数据"。
HKT = timezone(timedelta(hours=8))

_DDL = {
    "collection_task_log": """
        CREATE TABLE IF NOT EXISTS collection_task_log (
            id            BIGSERIAL PRIMARY KEY,
            task_name     VARCHAR(80)  NOT NULL,
            market        VARCHAR(8),
            started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            finished_at   TIMESTAMPTZ,
            status        VARCHAR(16)  NOT NULL,   -- running/ok/timeout/error
            duration_s    NUMERIC(8,2),
            error_msg     TEXT,
            created_at    TIMESTAMPTZ  DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_tasklog_name_time
            ON collection_task_log (task_name, started_at DESC);
    """,
    "collection_api_log": """
        CREATE TABLE IF NOT EXISTS collection_api_log (
            id            BIGSERIAL PRIMARY KEY,
            api_name      VARCHAR(40)  NOT NULL,   -- get_market_snapshot/request_history_kline/...
            success       BOOLEAN      NOT NULL,
            latency_s     NUMERIC(8,3),
            error_msg     TEXT,
            called_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            created_at    TIMESTAMPTZ  DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_apilog_name_time
            ON collection_api_log (api_name, called_at DESC);
    """,
    "collection_alert": """
        CREATE TABLE IF NOT EXISTS collection_alert (
            id            BIGSERIAL PRIMARY KEY,
            category      VARCHAR(24)  NOT NULL,   -- task_stall/task_fail/api_fail/api_slow/data_stale
            severity      VARCHAR(8)   NOT NULL,   -- warn/crit
            source        VARCHAR(80),            -- 关联任务名 / 表名 / 接口名
            message       TEXT         NOT NULL,
            first_seen    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            last_seen     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            occurrences   INT          NOT NULL DEFAULT 1,
            resolved      BOOLEAN      NOT NULL DEFAULT FALSE,
            resolved_at   TIMESTAMPTZ,
            created_at    TIMESTAMPTZ  DEFAULT NOW(),
            UNIQUE (category, source, severity, resolved)
        );
        CREATE INDEX IF NOT EXISTS idx_alert_open
            ON collection_alert (resolved, last_seen DESC);
    """,
    # 监控元数据表：配置「需要监控的表」
    # 后续扩展只需改这张表（增/删行、调 period/expect_lag），无需改代码、无需重启服务。
    # period 枚举: minute(盘中高频) / day(日更) / week(周更) / lowfreq(低频维度)
    # expect_lag 单位随 period: minute→分钟, day/week/lowfreq→天
    # active=false 可临时关停某表监控
    "monitor_table_config": """
        CREATE TABLE IF NOT EXISTS monitor_table_config (
            id           BIGSERIAL   PRIMARY KEY,
            db_name      VARCHAR(32) NOT NULL DEFAULT 'public',
            table_name   VARCHAR(64) NOT NULL,
            time_column  VARCHAR(32) NOT NULL,
            period       VARCHAR(12) NOT NULL DEFAULT 'day',
            expect_lag   INT         NOT NULL DEFAULT 0,
            refresh_weekday INT      NOT NULL DEFAULT 0,  -- period='week' 时生效：刷新日星期几(0=Mon..6=Sun)
            active       BOOLEAN     NOT NULL DEFAULT TRUE,
            remark       VARCHAR(120),
            UNIQUE (db_name, table_name)
        );
    """,
    # 任务级 stall 监控配置：只纳入"应持续运行"的高频任务，
    # 低频/一次性任务（日/周级）不配置，避免非刷新日被误报停摆。
    # max_interval_min: 该任务两次触发的最大允许间隔（分钟）
    "monitor_task_config": """
        CREATE TABLE IF NOT EXISTS monitor_task_config (
            task_name         VARCHAR(80) PRIMARY KEY,
            max_interval_min  INT NOT NULL,
            active            BOOLEAN NOT NULL DEFAULT TRUE,
            remark            VARCHAR(120)
        );
    """,
}


# ── 建表 ──────────────────────────────────────────────────
def ensure_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ddl in _DDL.values():
                cur.execute(ddl)
        conn.commit()
    _seed_table_config()
    _seed_task_config()


# 当前真正在采集的表的种子配置（仅在配置表为空时写入）。
# 来源：scheduler 注册脚本 + ticker_collector 独立服务 + 企业微信常驻服务落库的表，
# 经逐一核对 upsert 目标与真实时间列，已排除废弃表（daily_south_flow /
# fund_flow_daily / volume_daily / phase_label 等无人写入的表）。
#
# 周期与列的配对原则（关键：必须按表的真实更新节奏配）：
#   period='minute' 的表，time_column 必须用「盘中高频刷新的时间戳列」
#     （如 update_time / snapshot_time / mkt_time），绝不能填 trade_date
#     —— trade_date 一天不变，无法体现盘中是否还在更新。
#   period='day' 的表，time_column 用 trade_date（看是否有当天数据即可，天级容忍）。
#   period='lowfreq' 的表，time_column 用 updated_at / created_at（低频维度）。
# expect_lag 单位随 period: minute→分钟, day/week/lowfreq→天。
# refresh_weekday 单位: 0=Mon..6=Sun, 仅 period='week' 生效（指定每周几刷新）。
#
# 元组: (table_name, time_column, period, expect_lag, refresh_weekday, remark)
_SEED_TABLE_CONFIG = [
    # —— 日更型（trade_date 判天级，盘后全量）——
    ("daily_ggt_hold",          "trade_date",   "day",    1, 0, "get_south_flow(南向资金T-1)"),
    ("daily_short_selling",     "trade_date",   "day",    0, 0, "get_realtime_short_selling_fullday"),
    ("daily_cbbc",              "trade_date",   "day",    0, 0, "get_cbbc"),
    ("daily_buyback_event",     "buyback_date", "day",    1, 0, "get_buyback"),
    ("daily_margin_balance",    "trade_date",   "day",    1, 0, "get_margin_balance"),
    ("macro_environment_score", "trade_date",   "day",    0, 0, "macro_environment_score"),
    ("daily_trend",             "trade_date",   "day",    0, 0, "get_trend"),
    ("daily_northbound_flow",   "trade_date",   "day",    0, 0, "get_northbound"),
    ("benchmark_correlation",   "updated_at",   "lowfreq", 3, 0, "benchmark_correlation_daily"),
    ("stock_sector",            "updated_at",   "week",   0, 1, "get_stock_sector(每周二刷新)"),
    # —— 盘中高频型（必须用盘中时间戳列，才能抓「超 N 分钟未更新」）——
    ("daily_quote",             "update_time",  "minute", 0, 0, "get_quote(盘中每5分钟批量刷新)"),
    ("hk_daily_quote",          "update_time",  "minute", 0, 0, "get_hk_market_turnover(盘中刷新update_time)"),
    ("daily_market_turnover",   "snapshot_time","minute", 0, 0, "get_hk_market_turnover(盘中每5分钟)"),
    ("daily_benchmark",         "trade_date",   "day",    0, 0, "get_global_benchmarks(每小时/盘后全量)"),
    ("benchmark_minute",        "mkt_time",     "minute", 0, 0, "get_global_benchmarks_minute"),
    ("trend_snapshot",          "snapshot_time","minute", 0, 0, "record_trend(盘中批量)"),
    ("tick_data",               "tick_time",    "minute", 0, 0, "ticker_collector(独立服务, tick_time有索引)"),
    ("realtime_order_size",     "snapshot_time","minute", 0, 0, "企业微信常驻服务"),
]


# 任务级 stall 监控 seed：只纳入"应持续运行"的高频任务。
# 低频/一次性任务（日/周级）不进表，避免非刷新日被误报停摆。
# 元组: (task_name, max_interval_min, remark)
# 暂不 seed 任何任务：现有高频任务（盘中批量采集 / 港股成交额 / 全球指数分钟）
# 的「是否在跑」已被 data_stale 覆盖（对应数据表都在监控），task_stall 对它们
# 是冗余，且会因盘后不写 task_log 而误报（任务盘后不跑，但 stall 检查窗口仍开）。
# monitor_task_config 表与 _check_task_stall 逻辑保留，供未来「成功与否不反映在
# 数据表上」的任务（如纯清理类）使用。
_SEED_TASK_CONFIG = []


def _seed_table_config():
    """配置表为空时写入种子数据（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM monitor_table_config")
            if cur.fetchone()[0] > 0:
                return
            for name, col, period, lag, wd, remark in _SEED_TABLE_CONFIG:
                cur.execute(
                    "INSERT INTO monitor_table_config "
                    "(db_name, table_name, time_column, period, expect_lag, refresh_weekday, remark) "
                    "VALUES ('public', %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (db_name, table_name) DO NOTHING",
                    (name, col, period, lag, wd, remark),
                )
        conn.commit()


def _seed_task_config():
    """任务级配置表为空时写入 seed（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM monitor_task_config")
            if cur.fetchone()[0] > 0:
                return
            for name, max_min, remark in _SEED_TASK_CONFIG:
                cur.execute(
                    "INSERT INTO monitor_task_config (task_name, max_interval_min, remark) "
                    "VALUES (%s, %s, %s) ON CONFLICT (task_name) DO NOTHING",
                    (name, max_min, remark),
                )
        conn.commit()


# ── 埋点：任务执行 ───────────────────────────────────────
def record_task_start(task_name: str, market: str | None = None) -> int:
    """写入一条 running 记录，返回 id（供结束埋点关联）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO collection_task_log (task_name, market, status) "
                "VALUES (%s, %s, 'running') RETURNING id",
                (task_name, market),
            )
            rid = cur.fetchone()[0]
        conn.commit()
    return rid


def record_task_end(rid: int, status: str, error_msg: str | None = None,
                    duration_s: float | None = None):
    """更新任务执行结果。status: ok/timeout/error"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE collection_task_log SET finished_at=NOW(), status=%s, "
                "duration_s=%s, error_msg=%s WHERE id=%s",
                (status, duration_s, error_msg, rid),
            )
        conn.commit()


# ── 埋点：接口调用 ───────────────────────────────────────
def record_api_call(api_name: str, success: bool, latency_s: float | None = None,
                    error_msg: str | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO collection_api_log (api_name, success, latency_s, error_msg) "
                "VALUES (%s, %s, %s, %s)",
                (api_name, success, latency_s, error_msg),
            )
        conn.commit()


# ── 故障表写入/更新（去重合并同类未解决告警）──────────────
def _raise_alert(category, severity, source, message):
    """已存在同 (category, source) 的未解决告警则升级/续计：UPDATE severity +
    occurrences+1 + 刷新 last_seen/message；否则 INSERT 新行。

    关键：按 (category, source) 而非 (category, source, severity) 去重，使
    严重度升级（warn→crit）时复用同一行而非新建，避免 warn/crit 两条告警同时
    悬挂。UNIQUE(category, source, severity, resolved) 仍兼容：同一 source 任意
    时刻至多一条 resolved=false 行，UPDATE severity 不与之冲突。
    """
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, occurrences FROM collection_alert "
                "WHERE category=%s AND source=%s AND resolved=false "
                "ORDER BY id DESC LIMIT 1",
                (category, source),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE collection_alert SET severity=%s, last_seen=%s, "
                    "occurrences=%s, message=%s WHERE id=%s",
                    (severity, now, row[1] + 1, message, row[0]),
                )
            else:
                cur.execute(
                    "INSERT INTO collection_alert "
                    "(category, severity, source, message, first_seen, last_seen) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (category, severity, source, message, now, now),
                )
        conn.commit()


def _resolve_alert(category, source):
    """把某 source 下该类未解决告警标记为已解决（检查器确认正常时调用）。"""
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE collection_alert SET resolved=true, resolved_at=%s "
                "WHERE category=%s AND source=%s AND resolved=false",
                (now, category, source),
            )
        conn.commit()


# ── 评估器 ───────────────────────────────────────────────
# 交易日状态：每日只调一次富途，批量拉取未来一段时间的交易日，
# 结果存入模块级变量 _TRADING_DAY_STATUS（date_str -> bool）。
# 巡检时 is_trading_day 只读变量，绝不调用富途接口。
_TRADE_DAY_FETCH_DAYS = 35  # 一次拉取未来约5周，覆盖 week/lowfreq 判定所需回溯
_trading_day_status: Dict[str, set[str]] = {}   # {"HK": {date_str}, "US": {...}}，存交易日集合
_trading_day_fetched_at: str = ""           # 拉取对应的"今天"，跨天触发重新拉取


def _refresh_trading_days():
    """每日只调一次富途，批量刷新交易日状态到模块级变量。

    设计要点（避免巡检热路径调接口）：
      - 进程启动立即调一次；之后每天 00:00 由定时器再调一次。
      - 接口不可用时降级为 weekday<5，不阻断监控。
    返回拉取是否成功（失败则保留旧值/降级值）。
    """
    global _trading_day_status, _trading_day_fetched_at
    today_d = datetime.now().date()
    _trading_day_fetched_at = today_d.isoformat()
    markets = ["HK", "US"]
    new_status: Dict[str, set[str]] = {}
    try:
        from futu import OpenQuoteContext, Market
        end = (today_d + timedelta(days=_TRADE_DAY_FETCH_DAYS)).isoformat()
        start = (today_d - timedelta(days=_TRADE_DAY_FETCH_DAYS)).isoformat()
        with OpenQuoteContext(host="127.0.0.1", port=11111) as ctx:
            for mkt in markets:
                m = getattr(Market, mkt, Market.HK)
                ret, data = ctx.request_trading_days(m, start, end)
                s: set[str] = set()
                if ret == 0 and data:
                    for row in cast(list[dict[str, object]], data):
                        day_str = str(row.get("time", ""))
                        if day_str:
                            s.add(day_str)
                new_status[mkt] = s
        _trading_day_status = {k: v for k, v in new_status.items()}
        log.info(f"交易日状态刷新成功: 覆盖 {start}~{end}, HK={len(_trading_day_status.get('HK', set()))}天")
        return True
    except Exception as e:
        log.warning(f"富途交易日批量查询失败，降级 weekday: {e}")
        # 降级：用 weekday<5 填充未来窗口
        s: set[str] = set()
        for i in range(-_TRADE_DAY_FETCH_DAYS, _TRADE_DAY_FETCH_DAYS + 1):
            d = today_d + timedelta(days=i)
            if d.weekday() < 5:
                s.add(d.isoformat())
        _trading_day_status = {"HK": s, "US": s}
        return False


def is_trading_day(d, market="HK") -> bool:
    """判断 d(date) 是否为交易日。

    只读模块级变量 _trading_day_status（由 _refresh_trading_days 每日刷新），
    巡检热路径完全不调用富途接口。
    """
    if not _trading_day_status:  # 尚未初始化（极端情况），立即拉一次
        _refresh_trading_days()
    s = _trading_day_status.get(market) or _trading_day_status.get("HK") or set()
    return d.isoformat() in s


def is_collection_active() -> bool:
    """判断当前是否处于「采集应活跃」的时段。

    用于避免非交易时段误报 data_stale / task_stall：
    非交易日（周末/节假日）、以及盘中/盘后窗口之外，数据采集本就稀疏或停滞，不应告警。
    逻辑对齐 market_scheduler.is_trading_hours（港股+ A 股窗口并集，
    并覆盖盘后 16:00-19:00 的补采活跃期），且新增「真实交易日」判断
    （读每日刷新的交易日状态变量，自动排除港股/A 股节假日，不再仅依赖 weekday）。
    """
    now = datetime.now()  # 系统时区 = Asia/Shanghai，与 HK 同处 +8
    if now.weekday() >= 5:
        return False
    if not is_trading_day(now.date(), "HK"):  # 节假日（如圣诞/佛诞）排除
        return False
    h, m = now.hour, now.minute
    # 港股 9:30-12:00 / 13:00-16:10
    hk = ((h == 9 and m >= 30) or (10 <= h < 12) or (h == 12 and m == 0)
          or (13 <= h < 16) or (h == 16 and m <= 10))
    # A股 9:30-11:30 / 13:00-15:00
    a = ((h == 9 and m >= 30) or (h == 10) or (h == 11 and m <= 30)
         or (13 <= h < 15) or (h == 15 and m == 0))
    # 盘后补采活跃期（南向资金 19:00、沽空 16:50-18:30 等）
    after = (16 <= h < 20)
    return hk or a or after


def is_intraday_active() -> bool:
    """严格交易时段（仅用于分钟级 data_stale 判断）。

    与 is_collection_active 的关键区别：**不含盘后补采窗口 16:00-20:00**。
    原因：分钟级表（daily_quote / tick_data / trend_snapshot / benchmark_minute /
    hk_daily_quote 等）在收盘后本就该冻结，盘后继续判 idle 会持续误报 CRIT
    （16:40-20:00 全程假告警）。盘后补采任务（南向/沽空）写的是日级表，
    日级表 data_stale 本就不受 active 门控，所以盘后窗口对它们也无意义。

    覆盖：港股 9:30-12:00 / 13:00-16:10 + 缓冲到 16:40（收盘竞价 16:00-16:10
    + 16:30 盘后补采快照）；A 股 9:30-11:30 / 13:00-15:00。
    16:41 之后分钟级表冻结属正常，不告警（次日 9:30 数据刷新后由活跃期 resolve）。
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if not is_trading_day(now.date(), "HK"):
        return False
    h, m = now.hour, now.minute
    hk = ((h == 9 and m >= 30) or (10 <= h < 12) or (h == 12 and m == 0)
          or (13 <= h < 16) or (h == 16 and m <= 40))   # 含收盘竞价 + 16:30 盘后补采缓冲
    a = ((h == 9 and m >= 30) or (h == 10) or (h == 11 and m <= 30)
         or (13 <= h < 15) or (h == 15 and m == 0))
    return hk or a


def _window_start(minutes: int = WINDOW_MIN):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _check_api_failures():
    """接口调用成功率 / 延迟监控。"""
    ws = _window_start()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 整体成功率
            cur.execute(
                "SELECT COUNT(*), SUM(CASE WHEN success THEN 1 ELSE 0 END) "
                "FROM collection_api_log WHERE called_at >= %s", (ws,))
            total, ok = cur.fetchone()
            total = total or 0
            ok = ok or 0
            # 慢调用数
            cur.execute(
                "SELECT COUNT(*) FROM collection_api_log "
                "WHERE called_at >= %s AND latency_s >= %s", (ws, API_SLOW_THRESHOLD))
            slow = cur.fetchone()[0] or 0
    if total == 0:
        return  # 窗口内无接口调用（可能非交易时段），不告警
    fail_rate = (total - ok) / total
    if fail_rate >= API_FAIL_RATE_CRIT:
        _raise_alert("api_fail", "crit", "futu",
                     f"富途接口失败率 {fail_rate*100:.1f}%（{total} 次调用，{total-ok} 失败）")
    elif fail_rate >= API_FAIL_RATE_WARN:
        _raise_alert("api_fail", "warn", "futu",
                     f"富途接口失败率 {fail_rate*100:.1f}%（{total} 次调用，{total-ok} 失败）")
    else:
        _resolve_alert("api_fail", "futu")

    if slow > 0:
        _raise_alert("api_slow", "warn", "futu",
                     f"窗口内 {slow} 次富途接口调用耗时 ≥ {API_SLOW_THRESHOLD}s")
    else:
        _resolve_alert("api_slow", "futu")


def _check_task_failures():
    """任务执行失败率监控。

    仅统计已结束的任务（finished_at IS NOT NULL）：
    in-flight（status='running'）不算成功也不算失败，避免窗口叠加 in-flight
    抬高失败率产生误报。长时间未结束的 'running' 由 task_stall（高频任务）
    单独兜底。
    """
    ws = _window_start()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT "
                "COUNT(*) FILTER (WHERE finished_at IS NOT NULL) AS completed, "
                "SUM(CASE WHEN status='ok' AND finished_at IS NOT NULL "
                "         THEN 1 ELSE 0 END) AS ok_cnt "
                "FROM collection_task_log WHERE started_at >= %s", (ws,))
            completed, ok_cnt = cur.fetchone()
            completed = completed or 0
            ok_cnt = ok_cnt or 0
    if completed == 0:
        return
    fail_rate = (completed - ok_cnt) / completed
    if fail_rate >= TASK_FAIL_RATE_CRIT:
        _raise_alert("task_fail", "crit", "all",
                     f"采集任务失败率 {fail_rate*100:.1f}%"
                     f"（{completed} 次已完成，{completed-ok_cnt} 失败/超时）")
    elif fail_rate >= TASK_FAIL_RATE_WARN:
        _raise_alert("task_fail", "warn", "all",
                     f"采集任务失败率 {fail_rate*100:.1f}%"
                     f"（{completed} 次已完成，{completed-ok_cnt} 失败/超时）")
    else:
        _resolve_alert("task_fail", "all")


def _check_task_stall():
    """任务停摆监控（仅监控 monitor_task_config 中配置的「应持续运行」任务）。

    读 monitor_task_config(活跃=true) LEFT JOIN collection_task_log 取最近
    started_at。对每个配置任务按其 max_interval_min 计算阈值：
      - idle >= max * 2     → crit
      - idle >= max * 1.5   → warn
      - 否则 resolve
    未配置的任务不做 stall 检查（日/周级一次性任务本就允许长时间空闲）。
    LEFT JOIN 保留"从未触发"的配置任务，但首次巡检不告警（避免部署噪声），
    等其正常跑过一次后即进入正常节拍。
    """
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.task_name, c.max_interval_min, MAX(l.started_at) "
                "FROM monitor_task_config c "
                "LEFT JOIN collection_task_log l ON l.task_name = c.task_name "
                "WHERE c.active = true "
                "GROUP BY c.task_name, c.max_interval_min"
            )
            rows = cur.fetchall()
    for task_name, max_min, last in rows:
        if last is None:
            # 从未触发：可能是部署未启动/任务时间窗未到，不告警（避免噪声）
            continue
        idle = (now - last).total_seconds() / 60.0
        crit_th = max_min * 2
        warn_th = max_min * 1.5
        if idle >= crit_th:
            _raise_alert("task_stall", "crit", task_name,
                         f"任务「{task_name}」已 {idle:.0f} 分钟未触发"
                         f"（最大允许 {max_min} 分钟）")
        elif idle >= warn_th:
            _raise_alert("task_stall", "warn", task_name,
                         f"任务「{task_name}」已 {idle:.0f} 分钟未触发"
                         f"（最大允许 {max_min} 分钟）")
        else:
            _resolve_alert("task_stall", task_name)


# 全业务表监控配置：覆盖所有采集落库的表（不含监控自身表 collection_*）
def _last_trading_date(reference):
    """返回 reference 之前（含）最近的一个真实交易日。

    读每日刷新的交易日状态变量（自动含周末+节假日），
    接口不可用时降级值为仅跳过周六日的近似。
    """
    d = reference.date() if hasattr(reference, "date") else reference
    for _ in range(14):  # 最多往前找两周，避免死循环
        if is_trading_day(d, "HK"):
            return d
        d = d - timedelta(days=1)
    # 降级：仅跳过周末
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


def _check_data_stale():
    """数据落地停滞监控：根据 monitor_table_config 配置表驱动。

    读取 monitor_table_config（active=true）逐行监控，按 period 分支：
      - minute : 盘中高频，活跃期内 MAX(col) >= now - STALE_*_MIN
      - day    : 日更，MAX(col) >= 最近交易日 - expect_lag 天
      - week   : 周更，MAX(col) >= 最近周一 - expect_lag 周
      - lowfreq: 低频维度，MAX(col) >= now - expect_lag 天
    同时检测「表无数据」和「无法读取」两种情况。
    扩展新表只需往 monitor_table_config 插一行，无需改代码/重启。
    """
    now = datetime.now(timezone.utc)
    today = _last_trading_date(now)
    # 分钟级表用「严格交易时段」（不含盘后窗口），避免收盘后冻结被误判 stale；
    # 日/周/低频分支不使用此变量。
    active = is_intraday_active()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, time_column, period, expect_lag, refresh_weekday "
                    "FROM monitor_table_config WHERE active=true ORDER BY table_name"
                )
                rows = cur.fetchall()
    except Exception as e:
        log.warning(f"读取 monitor_table_config 失败: {e}")
        return

    # 清理孤儿告警：配置已不再监控的 source，其遗留未解决告警直接收尾，
    # 避免前端读到已失效的监控目标告警（配置驱动：改配置即生效）。
    monitored = {r[0] for r in rows}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE collection_alert SET resolved=true, resolved_at=%s "
                    "WHERE category='data_stale' AND resolved=false "
                    "AND source <> ALL(%s)",
                    (now, list(monitored)),
                )
    except Exception as e:
        log.warning(f"清理孤儿告警失败: {e}")

    # 单连接串行查所有表，避免逐表反复建连。
    # 关键优化：不查 MAX(col) 全表扫描，而是带「时间下界」窗口查询，
    #   只扫最近一段数据（利用时间列索引做 range scan，无索引表也只扫窗口内行）。
    # 下界按 period 推算，确保覆盖判定所需的最长 idle 窗口。
    window_start = _compute_window_start(now, today, rows)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for table, col, period, lag, wd in rows:
                    start = window_start.get((period, lag, wd))
                    try:
                        if start is not None:
                            cur.execute(
                                f"SELECT MAX({col}) FROM {table} WHERE {col} >= %s",
                                (start,),
                            )
                        else:
                            cur.execute(f"SELECT MAX({col}) FROM {table}")
                        last = cur.fetchone()[0]
                    except Exception as e:
                        _raise_alert("data_stale", "warn", table, f"无法读取 {table}.{col}: {e}")
                        continue
                    _evaluate_stale(table, col, period, lag, wd, last, now, today, active,
                                    window_start=start)
    except Exception as e:
        log.warning(f"data_stale 巡检连接失败: {e}")


def _compute_window_start(now, today, rows):
    """为每张表计算 MAX(col) 查询的时间下界（窗口起点）。

    目的：避免对大表做无下界的 MAX() 全表扫描。窗口只需覆盖
    「判定 stale 所需的最长 idle 区间 + 余量」即可，且下界对齐「真实交易日」
    （用富途接口判断），避免节假日/周末把窗口落在无数据的非交易日导致误报。
    minute 表: 最近真实交易日 09:00 HKT（覆盖当天盘中 + 盘后补采窗口）
    day 表   : 最近交易日 - (lag + 2) 天
    week 表  : 最近刷新日 - (lag + 2) 周
    lowfreq  : now - (lag + 2) 天
    """
    starts = {}
    for table, col, period, lag, wd in rows:
        if period == "minute":
            # 最近真实交易日开盘前：确保窗口落在「有数据的交易日」，
            # 节假日/周末当天无数据也不会误判（窗口回溯到上一交易日）。
            # 用 HKT 09:00（= UTC 01:00），与数据列时区对齐。
            tday = _last_trading_date(now)
            starts[(period, lag, wd)] = datetime(tday.year, tday.month, tday.day,
                                                 9, 0, tzinfo=HKT)
        elif period == "day":
            starts[(period, lag, wd)] = today - timedelta(days=lag + 2)
        elif period == "week":
            # 找「最近一个 refresh_weekday」：从 today 倒退到首个 weekday==wd
            d = today
            while d.weekday() != wd:
                d = d - timedelta(days=1)
            starts[(period, lag, wd)] = d - timedelta(weeks=lag + 2)
        elif period == "lowfreq":
            starts[(period, lag, wd)] = now - timedelta(days=lag + 2)
    return starts


def _evaluate_stale(table, col, period, lag, wd, last, now, today, active, window_start=None):
    """单表停滞判定（供 _check_data_stale 循环调用）。

    last 来自「带时间下界的窗口查询」MAX(col)：
      - 若 window_start 有下界且 last is None → 窗口内无数据（近期未更新）→ 判 stale（非全表空）
      - 若 window_start 为 None（兜底全表）且 last is None → 全表无数据 → 从未采集
    """
    if last is None:
        if window_start is None:
            _raise_alert("data_stale", "crit", table, f"{table} 无任何数据（采集从未成功？）")
        else:
            _raise_alert("data_stale", "crit", table,
                         f"{table} 最近时间窗口内无数据（{col} >= {window_start} 为空），采集可能停滞")
        return

    if period == "minute":
        # 盘中高频：仅活跃期判断。
        # 周末/节假日直接 return（不新发告警也不 resolve）——交易日判断逻辑
        # （_last_trading_date 锚定窗口 + is_collection_active 门控）已保证：
        #   1) 窗口落在"上一交易日"而非周末当天，不会因周末无数据而误报
        #   2) 活跃期外的告警状态保守保持，周一开盘后由活跃期检查重新确认，
        #      避免"周末 resolve → 周一又 raise" 的假恢复 blip。
        if not active:
            return
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        idle = (now - last).total_seconds() / 60.0
        if idle >= STALE_CRIT_MIN:
            _raise_alert("data_stale", "crit", table,
                         f"{table} 最新记录 {last}，已 {idle:.0f} 分钟未更新（盘中采集停滞？）")
        elif idle >= STALE_WARN_MIN:
            _raise_alert("data_stale", "warn", table,
                         f"{table} 最新记录 {last}，已 {idle:.0f} 分钟未更新")
        else:
            _resolve_alert("data_stale", table)

    elif period == "day":
        last_date = last.date() if hasattr(last, "date") else last
        expected = today - timedelta(days=lag)
        if last_date < expected:
            gap = (today - last_date).days
            _raise_alert("data_stale", "crit", table,
                         f"{table} 最新交易日 {last_date}，已落后 {gap} 天"
                         f"（期望 ≤ {lag} 天前），采集可能停滞")
        else:
            _resolve_alert("data_stale", table)

    elif period == "week":
        # 每周固定星期几(refresh_weekday)刷新：找「最近的应刷新日」——
        # 若本周该星期几已过去→取本周；否则取上周（本周还没到刷新时间，不应报）。
        # 允许落后 expect_lag 周。
        d = today
        while d.weekday() != wd:
            d = d - timedelta(days=1)
        if d > today:  # 本周刷新日还没到，基准回退到上周
            d = d - timedelta(weeks=1)
        expected = d - timedelta(weeks=lag)
        last_date = last.date() if hasattr(last, "date") else last
        if last_date < expected:
            gap_weeks = (today - last_date).days // 7
            _raise_alert("data_stale", "crit", table,
                         f"{table} 最新数据 {last_date}，已约 {gap_weeks} 周未更新"
                         f"（期望 ≤ {lag} 周前），采集可能停滞")
        else:
            _resolve_alert("data_stale", table)

    elif period == "lowfreq":
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        idle_days = (now - last).days
        if idle_days >= lag:
            _raise_alert("data_stale", "warn", table,
                         f"{table} 最新记录 {last}，已 {idle_days} 天未更新（低频表也可能停采）")
        else:
            _resolve_alert("data_stale", table)
    else:
        log.warning(f"未知 period={period} 于表 {table}，跳过")


def run(codes=None, ctx=None):
    """单次巡检（供循环调用 / scheduler 调用 / 手动触发）。

    设计为幂等：每次读取最新指标并 upsert 到 collection_alert，
    正常情况自动 resolved，异常才持续 open。
    """
    try:
        ensure_tables()
        _check_api_failures()
        _check_task_failures()
        # task_stall 仅采集活跃期检查（非活跃期任务本就稀少，跳过避免误报）
        if is_collection_active():
            _check_task_stall()
        else:
            log.info("当前非采集活跃时段，跳过 task_stall 检查")
        # data_stale 始终检查：日更/低频表是否停采不受盘中活跃期限制，
        # 仅 intraday 表在 _check_data_stale 内部自行跳过非活跃期
        _check_data_stale()
    except Exception as e:
        log.exception(f"巡检异常: {e}")
    log.info("采集层健康巡检完成")


def _schedule_daily_trading_day_refresh():
    """每天 00:00 刷新一次交易日状态（后台线程，独立于巡检循环）。

    确保富途接口每天只调一次，结果存模块变量，巡检热路径完全不调用接口。
    """
    _refresh_trading_days()
    # 计算到明天 00:00 的秒数，递归调度
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    wait = (tomorrow - now).total_seconds()
    t = threading.Timer(wait, _schedule_daily_trading_day_refresh)
    t.daemon = True
    t.start()


def monitor_loop(interval: int = 60):
    """常驻主循环：独立于调度服务，自身定时巡检。

    不依赖 APScheduler / market_scheduler，即使调度器挂掉也能持续监控。
    interval: 巡检间隔秒数，默认 60（比调度器 5 分钟更密，停摆发现更快）。
    """
    log.info(f"采集层监控服务启动，巡检间隔 {interval}s（独立进程，不依赖调度服务）")
    ensure_tables()
    # 启动即刷新交易日状态，并起每日定时刷新（巡检热路径不再调富途接口）
    _refresh_trading_days()
    _schedule_daily_trading_day_refresh()
    while True:
        try:
            run()
        except Exception as e:
            log.exception(f"巡检循环异常: {e}")
        time.sleep(interval)


def print_status():
    """手动排查：打印当前未解决告警 + 关键指标。"""
    ensure_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            print("=== 未解决告警 ===")
            cur.execute(
                "SELECT severity, category, source, message, occurrences, last_seen "
                "FROM collection_alert WHERE resolved=false ORDER BY severity, last_seen DESC")
            for r in cur.fetchall():
                print(f"  [{r[0]}] {r[1]}/{r[2]} (x{r[4]}) {r[3]} @ {r[5]}")
            print("=== 最近接口调用(5) ===")
            cur.execute(
                "SELECT api_name, success, latency_s, called_at FROM collection_api_log "
                "ORDER BY called_at DESC LIMIT 5")
            for r in cur.fetchall():
                print(f"  {r[0]} ok={r[1]} {r[2]}s @ {r[3]}")
            print("=== 最近任务执行(5) ===")
            cur.execute(
                "SELECT task_name, status, duration_s, started_at FROM collection_task_log "
                "ORDER BY started_at DESC LIMIT 5")
            for r in cur.fetchall():
                print(f"  {r[0]} {r[1]} {r[2]}s @ {r[3]}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        print_status()
    elif "--once" in sys.argv:
        run()
    else:
        # 常驻服务：自带循环，独立于调度服务
        monitor_loop(interval=int(os.environ.get("MONITOR_INTERVAL", "60")))
