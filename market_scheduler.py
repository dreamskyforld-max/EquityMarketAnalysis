#!/usr/bin/env python3
"""
市场数据定时调度服务
- 以股票为中心配置，自动生成盘中（分钟级）和收盘（全量）采集任务
- 支持港股和A股，通过 MARKET_PRESETS 定义各市场采集模块
- 添加新股：在 STOCKS 中加一行即可

用法：
    python3 market_scheduler.py          # 前台运行
    nohup python3 market_scheduler.py & # 后台运行
"""
import os
import sys
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedThreadPool

# ── 日志配置 ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "scheduler.log")),
    ],
)
log = logging.getLogger("scheduler")

# ── 路径配置（与 wecom_server_collector 保持一致）───────────
# 服务器部署时用 /home/hermes-agent/hermes-skills
# 本地调试时自动切换到脚本所在目录
_SERVER_PATH = "/home/hermes-agent/hermes-skills"
if os.path.exists(_SERVER_PATH):
    SCRIPTS_DIR = _SERVER_PATH
else:
    SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
    print(f"[调试模式] 使用本地路径: {SCRIPTS_DIR}")

# 注册共享运行时：惰性模块加载 + 进程级共享 FutuOpenD 上下文（消除每分钟 subprocess 冷启动）
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
import collector_runtime  # noqa: E402  (导入即把 SCRIPTS_DIR 加入 sys.path)


# ── 市场预设 ────────────────────────────────────────────────
# 不同市场默认采集的模块集合，股票自动继承
# 添加 A 股：在 STOCKS 中加一行，继承 "A" 市场的预设模块
MARKET_PRESETS = {
    "HK": {
        "intraday": [  # 盘中每分钟
            "get_quote.py",      # 行情快照（富途API）→ daily_quote
            "get_benchmark.py",  # 恒生等基准指数 → benchmark_daily
            "record_trend.py",   # 盘中趋势记录（分钟快照+缓存）→ trend_snapshot + trend_cache/
        ],
        "daily": {     # 收盘后全量采集
            "time": {"hour": 16, "minute": 20},
            "modules": [
                "get_quote.py",                          # 行情快照（富途API）→ daily_quote
                "get_benchmark.py",                      # 恒生等基准指数 → benchmark_daily
                "get_excess_return.py",                  # 超额收益计算 → excess_return_daily
                "get_south_flow.py",                     # 南向资金（港股通流向）
                "get_cbbc.py",                           # 牛熊证街货分布（港交所）→ cbbc_daily
                "get_short_selling.py",                  # 沽空数据（实时）
                "get_trend.py",                          # 全日趋势数据
                "get_realtime_short_selling_fullday.py", # 全日沽空数据
                "get_buyback.py",                         # 公司回购（港交所）→ buyback_daily
            ],
        },
        "extras": [    # 市场特有定时任务
            ("财务指标", "get_financial.py", {"hour": 9, "minute": 0}),                                # 财务指标（AKShare年度+季度）→ financial_indicator
            ("全日沽空数据-1", "get_realtime_short_selling_fullday.py", {"hour": 16, "minute": 10}),  # 全日沽空 第1次补采
            ("全日沽空数据-2", "get_realtime_short_selling_fullday.py", {"hour": 16, "minute": 30}),  # 全日沽空 第2次补采
            ("全日沽空数据-3", "get_realtime_short_selling_fullday.py", {"hour": 17, "minute": 0}),   # 全日沽空 第3次补采
            ("公司回购", "get_buyback.py", {"hour": 9, "minute": 0}),                                  # 公司回购（港交所）→ buyback_daily
            ("南向资金", "get_south_flow.py", {"hour": 9, "minute": 0}),                               # 南向资金（港股通流向）
        ],
    },
    "A": {
        "intraday": [
            "get_quote.py",      # 行情快照（富途API）→ daily_quote
            "get_benchmark.py",  # 上证等基准指数 → benchmark_daily
            "record_trend.py",   # 盘中趋势记录（分钟快照+缓存）→ trend_snapshot + trend_cache/
        ],
        "daily": {
            "time": {"hour": 15, "minute": 10},  # A股 15:00 收盘
            "modules": [
                "get_quote.py",          # 行情快照（富途API）→ daily_quote
                "get_benchmark.py",      # 上证等基准指数 → benchmark_daily
                "get_excess_return.py",  # 超额收益计算 → excess_return_daily
                "get_trend.py",          # 全日趋势数据
                "get_margin_balance.py", # 融资融券余额 → margin_balance
            ],
        },
        "extras": [
            ("财务指标", "get_financial.py", {"hour": 9, "minute": 0}),       # 财务指标（AKShare年度+季度）→ financial_indicator
            ("融资余额-早盘", "get_margin_balance.py", {"hour": 9, "minute": 10}),   # 上交所次日8:00后公布
            ("融资余额-晚间", "get_margin_balance.py", {"hour": 20, "minute": 10}),   # 深交所18:30-20:00公布
        ],
    },
}

# ── 股票列表（添加新股只需加一行）─────────────────────────
STOCKS = [
    {"code": "HK.00700", "market": "HK"},
    {"code": "SH.600900", "market": "A"},
    {"code": "SH.520900", "market": "A"},
    {"code": "SH.600941", "market": "A"},
    {"code": "HK.09660", "market": "HK"},
    {"code": "HK.00857", "market": "HK"},
    {"code": "HK.01088", "market": "HK"},
    {"code": "HK.00883", "market": "HK"},
    {"code": "HK.00941", "market": "HK"},
    {"code": "HK.00386", "market": "HK"},
    {"code": "HK.01919", "market": "HK"},
    {"code": "HK.06869", "market": "HK"},
    {"code": "HK.00728", "market": "HK"},
    {"code": "HK.03328", "market": "HK"},
    {"code": "HK.03968", "market": "HK"},
]

# ── 全局任务（与具体股票无关）──────────────────────────────
GLOBAL_TASKS = [
    ("数据清理", "cleanup_old_data.py", {"hour": 4, "minute": 0}),            # 清理36个月前盘中实时数据+逐笔成交数据
    ("全球指数采集(每小时)", "get_global_benchmarks.py", {"hour": "*", "minute": 0}),  # 每小时刷新: 富途系盘中实时, FRED/债券/汇率为日频(T-1)
    # 全球指数分钟级采集：覆盖亚太(08:00–16:00 HK)+欧美盘至凌晨，避开低频 04:00–08:00
    # 每5分钟一次，仅交易日；run 忽略 codes，全局只跑一次
    ("全球指数分钟采集", "get_global_benchmarks_minute.py",
     {"minute": "*/5", "hour": "8-11,13-16,17-23,0-3", "day_of_week": "mon-fri"}),
    # 股票-指数成分归属（参考数据，季度刷新即可；run 忽略 codes，全局只跑一次）
    ("指数成分归属", "get_stock_sector.py", {"day_of_week": 2, "hour": 18, "minute": 0}),
    # 股票 vs 全球指数日收益率相关性分析（读 daily_quote/daily_benchmark 日频数据，
    # 盘后跑即可，盘中重算结果不变；run 循环 STOCKS，全局只跑一次）
    ("指数相关性分析", "benchmark_correlation_daily.py",
     {"hour": 17, "minute": 30, "day_of_week": "mon-fri"}),
    # 宏观环境三维评分（股/债/汇；读 daily_benchmark，盘后跑；run 忽略 codes，全局只跑一次）
    ("宏观环境评分", "macro_environment_score.py",
     {"hour": 18, "minute": 0, "day_of_week": "mon-fri"}),
]


# ── 执行器 ──────────────────────────────────────────────────

def run_script(script_name: str, stock_code: str | None, timeout: int = 180) -> bool:
    """运行采集模块（常驻进程内调用，无 subprocess 冷启动）。

    通过 collector_runtime.run_module 惰性导入模块并调用其 run(codes, ctx)，
    import 整个进程仅发生一次，FutuOpenD 连接由共享上下文复用。
    用线程+Event 实现超时：超时后线程被标记为 daemon 自动随进程退出，
    不阻塞 scheduler 主循环。
    """
    codes = [stock_code] if stock_code else None
    result = {"ok": False, "err": None}

    def _target():
        try:
            from collector_runtime import run_module
            run_module(script_name, codes)
            result["ok"] = True
        except BaseException as e:
            result["err"] = e

    import threading
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        log.warning(f"  [{script_name}] 执行超时 (>{timeout}s)")
        return False

    if result["err"] is not None:
        e = result["err"]
        import traceback
        log.warning(f"  [{script_name}] 执行异常: {type(e).__name__}: {e}")
        log.warning(traceback.format_exc())
        return False

    return result["ok"]


def execute_task(name: str, modules: list, stock_code: str | None,
                 force: bool = False, market: str | None = None, timeout: int = 180):
    """执行一组采集模块（顺序执行）

    Args:
        name: 任务名称（日志用）
        modules: 脚本名列表，空列表跳过
        stock_code: 股票代码，None 表示全局任务
        force: 是否跳过交易时段检查
        market: 市场标识（"HK"/"A"），用于交易时段判断；None 时不检查
        timeout: 单个脚本超时秒数，默认 180
    """
    if not force and market and not is_trading_hours(market):
        return

    if not modules:
        return

    start = datetime.now()
    log.info(f"[{name}] 开始 ...")

    fail = 0
    for script in modules:
        ok = run_script(script, stock_code, timeout)
        if not ok:
            fail += 1

    elapsed = (datetime.now() - start).total_seconds()
    total = len(modules)
    status = "成功" if fail == 0 else f"部分失败({fail}/{total})"
    log.info(f"[{name}] 完成 ({elapsed:.1f}s) — {status}")


# ── 调度器 ───────────────────────────────────────────────────

def is_trading_hours(market: str = "HK") -> bool:
    """判断当前是否在交易时段"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute

    if market == "A":
        # A股：周一至五，9:30-11:30 / 13:00-15:00
        if (h == 9 and m >= 30) or (h == 10) or (h == 11 and m <= 30):
            return True
        if 13 <= h < 15 or (h == 15 and m == 0):
            return True
    else:
        # 港股：周一至五，9:30-12:00 / 13:00-16:00
        if (h == 9 and m >= 30) or (10 <= h < 12) or (h == 12 and m == 0):
            return True
        if 13 <= h < 16 or (h == 16 and m == 0):
            return True
    return False


def build_scheduler():
    """根据 STOCKS + MARKET_PRESETS 自动生成调度任务"""
    sched = BackgroundScheduler(timezone="Asia/Hong_Kong", daemon=True)
    # 默认线程池只有 10 个线程，盘中每分钟同时触发 15+ 个任务会排队延迟
    sched.add_executor(APSchedThreadPool(max_workers=20), "default")
    registered = 0
    _fin_offset = {}  # 按市场错开财务指标任务的分钟偏移

    for stock in STOCKS:
        code = stock["code"]
        market = stock["market"]
        preset = MARKET_PRESETS.get(market)
        if not preset:
            log.warning(f"未知市场 [{market}]，跳过 {code}")
            continue

        # 1. 盘中每分钟采集组
        intraday = preset.get("intraday")
        if intraday:
            name = f"盘中采集-{code}"
            sched.add_job(
                execute_task,
                trigger=CronTrigger(second=0),
                args=[name, intraday, code, False, market],
                id=name,
                replace_existing=True,
                misfire_grace_time=60,
            )
            registered += 1
            log.info(f"已注册: [{name}] 每分钟 × {len(intraday)} 模块")

        # 2. 收盘全量采集
        daily = preset.get("daily")
        if daily:
            name = f"收盘采集-{code}"
            sched.add_job(
                execute_task,
                trigger=CronTrigger(**daily["time"]),
                args=[name, daily["modules"], code, True],
                id=name,
                replace_existing=True,
                misfire_grace_time=600,
            )
            registered += 1
            t = daily["time"]
            log.info(f"已注册: [{name}] {t['hour']:02d}:{t['minute']:02d} × {len(daily['modules'])} 模块")

        # 3. 市场特有任务
        for extra_name, script, cron_args in preset.get("extras", []):
            job_name = f"{extra_name}-{code}"

            # 财务指标采集：错开执行时间（每只股票间隔2分钟）+ 延长超时（600s）
            if script == "get_financial.py":
                idx = _fin_offset.get(market, 0)
                _fin_offset[market] = idx + 1
                cron = {**cron_args, "minute": cron_args.get("minute", 0) + idx * 2}
                timeout = 600
            else:
                cron = cron_args
                timeout = 180

            sched.add_job(
                execute_task,
                trigger=CronTrigger(**cron),
                args=[job_name, [script], code, True, None, timeout],
                id=job_name,
                replace_existing=True,
                misfire_grace_time=300,
            )
            registered += 1
            log.info(f"已注册: [{job_name}] {cron.get('hour',0):02d}:{cron.get('minute',0):02d}")

    # 4. 全局任务
    for g_name, g_script, g_cron in GLOBAL_TASKS:
        sched.add_job(
            execute_task,
            trigger=CronTrigger(**g_cron),
            args=[g_name, [g_script], None, True],
            id=g_name,
            replace_existing=True,
            misfire_grace_time=300,
        )
        registered += 1
        _h = g_cron.get('hour', 0)
        _m = g_cron.get('minute', 0)
        _h = f"{_h:02d}" if isinstance(_h, int) else str(_h)
        _m = f"{_m:02d}" if isinstance(_m, int) else str(_m)
        log.info(f"已注册: [{g_name}] (全局) {_h}:{_m}")

    # 5. Linux 端：每天早上 5:00 重启 FutuOpenD（缓解长时间运行 CPU/内存泄漏）
    if sys.platform == "linux":
        def _restart_futuopend():
            import subprocess
            try:
                subprocess.run(["sudo", "systemctl", "restart", "FutuOpenD.service"],
                               capture_output=True, text=True, timeout=30, check=True)
                log.info("[FutuOpenD 重启] 执行成功")
            except subprocess.CalledProcessError as e:
                log.warning(f"[FutuOpenD 重启] 失败: {e.stderr.strip()}")
            except Exception as e:
                log.warning(f"[FutuOpenD 重启] 异常: {e}")

        sched.add_job(
            _restart_futuopend,
            trigger=CronTrigger(hour=5, minute=0),
            id="重启FutuOpenD",
            replace_existing=True,
            misfire_grace_time=300,
        )
        registered += 1
        log.info("已注册: [重启FutuOpenD] (仅Linux) 05:00")

    log.info(f"共计注册 {registered} 个任务")
    return sched


def main():
    log.info("=" * 50)
    log.info("市场数据定时调度服务启动")
    log.info("=" * 50)

    sched = build_scheduler()
    sched.start()

    # 保持进程运行
    try:
        while True:
            import time
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号，关闭调度器 ...")
        sched.shutdown(wait=False)
        log.info("调度服务已停止")


if __name__ == "__main__":
    main()
