#!/usr/bin/env python3
"""
市场数据定时调度服务
- 以股票为中心配置，自动生成盘中（分钟级）和收盘（全量）采集任务
- 支持港股和A股，通过 MARKET_PRESETS 定义各市场采集模块
- 股票列表从 stock_info 表(is_active)动态加载，新股接入/停用由 setup_new_stock(s).py 管理

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

# ── 日志配置 (统一: ./log 目录 + 按天滚动 + 保留 10 天) ──────
from log_utils import setup_logger
log = setup_logger("scheduler")

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
# stock_info.market 填交易所代码(HK/SH/SZ) 即可，SH/SZ 会在 build_scheduler 归一化为 "A" 预设
MARKET_PRESETS = {
    "HK": {
        # 注意：盘中采集（get_quote.py / record_trend.py）已从「每只股票每分钟一个任务」
        # 改为「按市场全局批量任务」（见下方 INTRADAY_GLOBAL_MODULES + GLOBAL_TASKS 动态追加），
        # 避免 90 只股票 × 每分钟各打一次 get_market_snapshot 触发富途限频
        # （get_market_snapshot 上限 60 次/30秒）。此处 intraday 置空。
        "intraday": [],
        "daily": {     # 收盘后全量采集
            "time": {"hour": 16, "minute": 20},
            "modules": [
                "get_quote.py",                          # 行情快照（富途API）→ daily_quote
                "get_benchmark.py",                      # 恒生等基准指数 → benchmark_daily
                "get_excess_return.py",                  # 超额收益计算 → excess_return_daily
                "get_short_selling.py",                  # 沽空数据（实时，东方财富）
                "get_trend.py",                          # 全日趋势数据
            ],
        },
        "extras": [    # 市场特有定时任务（财务指标已改为全局全量任务，见 GLOBAL_TASKS）
        ],
    },
    "A": {
        "intraday": [],
        "daily": {
            "time": {"hour": 15, "minute": 10},  # A股 15:00 收盘
            "modules": [
                "get_quote.py",          # 行情快照（富途API）→ daily_quote
                "get_benchmark.py",      # 上证等基准指数 → benchmark_daily
                "get_excess_return.py",  # 超额收益计算 → excess_return_daily
                "get_trend.py",          # 全日趋势数据
            ],
        },
        "extras": [    # 财务指标已改为全局全量任务，见 GLOBAL_TASKS
        ],
    },
}

# ── 股票列表（从 stock_info 表读取 is_active 的股票）──────────
def load_stocks():
    """从 stock_info 表读取活跃股票，返回 [{'code':..., 'market':...}, ...]"""
    from db import get_conn
    stocks = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT stock_code, market FROM stock_info "
                    "WHERE is_active = TRUE ORDER BY stock_code"
                )
                for code, market in cur.fetchall():
                    stocks.append({"code": code, "market": market})
        log.info(f"从 stock_info 加载活跃股票 {len(stocks)} 只")
    except Exception as e:
        log.warning(f"加载 stock_info 失败，回退到空列表: {e}")
    return stocks


STOCKS = load_stocks()

# ── 全局任务（与具体股票无关）──────────────────────────────
# 每条任务: (name, script, cron, codes=None, force=True, market=None, timeout=180)
#   name    : 任务名（也是 job id）
#   script  : 采集脚本，单个用 str，多个用 list[str]（如盘中批量采集）
#   cron    : CronTrigger 参数字典
#   codes   : 全局股票列表（None=由模块自行决定范围；list=循环全票）
#   force   : True=跳过交易时段检查（默认）；False=需配合 market 做交易时段门控
#   market  : 交易时段判断用的市场标识（"HK"/"A"），仅当 force=False 时生效
#   timeout : 单脚本超时秒数（默认 180）
GlobalTask = tuple[str, str | list[str], dict[str, int | str], list[str] | None, bool, str | None, int]
GLOBAL_TASKS: list[GlobalTask] = [
    # 删除 3 年前过期数据（tick_data / trend_snapshot / realtime_order_size / collection_run_log 共 4 表）
    ("数据清理", "cleanup_old_data.py", {"hour": 4, "minute": 0}, None, True, None, 180),

    # 每小时刷新: 富途系盘中实时, FRED/债券/汇率为日频(T-1)
    ("全球指数采集(每小时)", "get_global_benchmarks.py", {"hour": "*", "minute": 0}, None, True, None, 180),

    # 全球指数分钟级采集：覆盖亚太(08:00–16:00 HK)+欧美盘至凌晨，避开低频 04:00–08:00
    # 每5分钟一次，仅交易日；run 忽略 codes，全局只跑一次
    ("全球指数分钟采集", "get_global_benchmarks_minute.py",
     {"minute": "*/5", "hour": "8-11,13-16,17-23,0-3", "day_of_week": "mon-fri"}, None, True, None, 300),

    # 股票-指数成分归属（参考数据，每周二 18:00 刷新；run 忽略 codes，全局只跑一次）
    ("指数成分归属", "get_stock_sector.py", {"day_of_week": 2, "hour": 18, "minute": 0}, None, True, None, 180),

    # 港股指数成分权重（恒生官网 factsheet 解析，每周二 19:00 刷新；
    ("指数成分权重", "get_stock_sector_weight.py", {"day_of_week": 2, "hour": 19, "minute": 0}, None, True, None, 180),

    # 股票 vs 全球指数日收益率相关性分析（读 daily_quote/daily_benchmark 日频数据，
    # 盘后跑即可，盘中重算结果不变；单 job 内循环全部 STOCKS 逐票执行）
    ("指数相关性分析", "benchmark_correlation_daily.py",
     {"hour": 17, "minute": 30, "day_of_week": "mon-fri"}, [s["code"] for s in STOCKS], True, None, 180),

    # 宏观环境三维评分（股/债/汇；读 daily_benchmark，盘后跑；单 job 内循环全部 STOCKS 逐票执行）
    ("宏观环境评分", "macro_environment_score.py",
     {"hour": 18, "minute": 0, "day_of_week": "mon-fri"}, [s["code"] for s in STOCKS], True, None, 180),

    # 港股全市场总成交额：富途 get_market_snapshot 批量（~30秒）聚合全港股成交额，
    # 落 daily_market_turnover 作为「市场总体流动性水位」分母。
    # 富途快照盘中实时更新，交易时段内每 5 分钟跑一次（force=False + market="HK"），
    # 盘中拿实时累计；收市竞价结束后盘后再补采全天完整终值（下一条）。
    ("港股全市场成交额", "get_hk_market_turnover.py",
     {"minute": "*/5", "second": 0}, None, False, "HK", 300),

    # 港股全市场成交额-盘后补采：收市竞价（16:00–16:10）结束后，富途快照结算出
    # 当天全天完整值，16:30 盘后固定补采一次落库（force=True 不受交易时段门控限制）。
    ("港股全市场成交额-盘后补采", "get_hk_market_turnover.py",
     {"hour": 16, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 300),

    # A股全市场成交额：富途 get_market_snapshot 批量（~3秒）聚合全 A 股（SH+SZ）成交额，
    # 落 a_daily_market_turnover + a_daily_quote，作为 A 股市场总体流动性水位分母。
    # 盘中实时累计：交易时段内每 5 分钟跑一次（force=False + market="A"，受 A 股时段门控）。
    ("A股全市场成交额", "get_a_market_turnover.py",
     {"minute": "*/5", "second": 0}, None, False, "A", 120),

    # A股全市场成交额-盘后补采：A股收市竞价（15:00–15:30）结束后，富途快照结算出
    # 当天全天完整值，16:10 盘后固定补采一次落库（force=True 不受交易时段门控限制）。
    ("A股全市场成交额-盘后补采", "get_a_market_turnover.py",
     {"hour": 16, "minute": 10, "day_of_week": "mon-fri"}, None, True, None, 120),
    
    # 南向资金（港股通持股）：AKShare 批量接口一次拉全市场 ~1200 只港股通标的，
    # 落 daily_ggt_hold。盘前 8:00、盘后 19:00 各跑一次（run 忽略 codes，全局批量，force=True）。
    ("南向资金", "get_south_flow.py",
     {"hour": "8,19", "minute": 0, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 融资融券全量明细（A股沪深两市）：AKShare 拉取交易所逐日公布的融资融券明细，
    # 全市场全标的 + 融资融券双向全字段，落 daily_margin_balance（全局任务，run 忽略 codes）。
    # 交易所约盘后披露当日数据，9 + 18 兜底补采两次（周一至周五，force=True）。
    ("融资融券全量", "get_margin_balance.py",
     {"hour": "9,18", "minute": 20, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 港股全日沽空（全市场全量）：一次抓取港交所全日沽空快照页，解析全部港股
    # 并全量入库 daily_short_selling（全局任务，不依赖股票列表，run 忽略 codes）。
    # 港交所约 16:50 发布，17:30、18:30 兜底补采。
    ("全日沽空数据", "get_realtime_short_selling_fullday.py",
     {"hour": 16, "minute": 50, "day_of_week": "mon-fri"}, None, True, None, 300),
     ("全日沽空数据-补采1", "get_realtime_short_selling_fullday.py",
     {"hour": 17, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("全日沽空数据-补采2", "get_realtime_short_selling_fullday.py",
     {"hour": 18, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 港股牛熊证街货分布（全市场全量）：一次抓取港交所 CBBC 完整列表 CSV，
    # 解析全部有牛熊证的标的并全量入库 daily_cbbc（全局任务，run 忽略 codes）。
    # 日更数据，盘后 16:25 跑一次（避开 16:20 收盘批量高峰）。
    ("牛熊证街货分布", "get_cbbc.py",
     {"hour": 16, "minute": 25, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 港股公司回购（全市场全量）：东财数据中心 RPT_HK_BUYBACK 按 TRADE_DATE 过滤分页，
    # 全量入库 daily_buyback_event（全局任务，run 忽略 codes）。
    # 回购公告盘后陆续披露，21:00 跑一次覆盖当日全部。
    ("公司回购", "get_buyback.py",
     {"hour": "7,17", "minute": 0, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 财务指标全量采集（港股 + A 股全部代码）：run(codes=None) 时忽略 codes，
    # 内部通过 AKShare 全量代码接口拉港股+全部A股代码，逐只 fetch 后批量入库
    # financial_indicator（单只异常隔离，不中断整批）。财务数据为低频（年报/季报），
    # 财务数据为低频（年报/季报），每周一凌晨 2 点跑一次全量即可。全市场约 8300+ 只、
    # 逐只 HTTP 调用耗时较长；超时设为 2 小时，且 execute_task 超时后后台线程仍会
    # 续跑直至自然完成（数据照常入库）。
    ("财务指标全量", "get_financial.py",
     {"hour": 2, "minute": 0, "day_of_week": "mon"}, None, True, None, 7200),

    # 注意：采集层故障监控已由独立服务 monitor_collector.py（常驻进程，
    # systemd: monitor-collector.service）负责，不再挂在调度器里，
    # 以免「调度器挂掉→监控也失效」的同源单点故障。
]

# ── 盘中批量采集（按市场拆分）────────────────────────────────
# 「每个市场每分钟一个全局任务」，codes=该市场全量股票，一次批量快照分发。
# 作为 GLOBAL_TASKS 项追加，与其他全局任务共用同一条注册循环。
INTRADAY_GLOBAL_MODULES = ["get_quote.py", "record_trend.py"]
_intraday_by_market = {}
for _s in STOCKS:
    _mkt = "A" if _s["market"] in ("SH", "SZ") else _s["market"]
    _intraday_by_market.setdefault(_mkt, []).append(_s["code"])
for _mkt, _mkt_codes in sorted(_intraday_by_market.items()):
    GLOBAL_TASKS.append(
        (f"盘中批量采集-{_mkt}", INTRADAY_GLOBAL_MODULES,
         {"second": 0}, _mkt_codes, False, _mkt, 180)
    )


# ── 执行器 ──────────────────────────────────────────────────

def run_script(script_name: str, codes=None, timeout: int = 180) -> bool:
    """运行采集模块（常驻进程内调用，无 subprocess 冷启动）。

    通过 collector_runtime.run_module 惰性导入模块并调用其 run(codes, ctx)，
    import 整个进程仅发生一次，FutuOpenD 连接由共享上下文复用。
    用线程+Event 实现超时：超时后线程被标记为 daemon 自动随进程退出，
    不阻塞 scheduler 主循环。

    Args:
        codes: 股票代码列表；可为 None（全局任务，由模块自行决定范围）、
               str（单票，自动包成 [str]）或 list（多票全局任务）。
    """
    if codes is None:
        pass  # 保持 None，交给模块 run() 决定默认范围
    elif isinstance(codes, str):
        codes = [codes]
    # list 则原样传入
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


def execute_task(name: str, modules: list, codes=None,
                 force: bool = False, market: str | None = None, timeout: int = 180):
    """执行一组采集模块（顺序执行）

    Args:
        name: 任务名称（日志用）
        modules: 脚本名列表，空列表跳过
        codes: 股票代码列表；None 表示全局任务（由模块自行决定范围）、
               str 表示单票任务、list 表示多票全局任务（如全票相关性分析）
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
    _rid: int | None = None
    try:
        from monitor_collector import record_task_start, record_task_end
        _rid = record_task_start(name, market)
    except Exception:
        _rid = None

    fail = 0
    for script in modules:
        ok = run_script(script, codes, timeout)
        if not ok:
            fail += 1

    elapsed = (datetime.now() - start).total_seconds()
    total = len(modules)
    status = "成功" if fail == 0 else f"部分失败({fail}/{total})"
    log.info(f"[{name}] 完成 ({elapsed:.1f}s) — {status}")

    if _rid is not None:
        try:
            if fail == 0:
                _st = "ok"
            else:
                # 部分失败：若有超时才算 timeout，否则 error
                _st = "timeout" if elapsed >= timeout else "error"
            record_task_end(_rid, _st, duration_s=round(elapsed, 2))
        except Exception:
            pass


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
        # 港股：周一至五，9:30-12:00 / 13:00-16:00（含 16:00-16:10 收市竞价时段）
        if (h == 9 and m >= 30) or (10 <= h < 12) or (h == 12 and m == 0):
            return True
        if 13 <= h < 16 or (h == 16 and m <= 10):
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
        # 归一化：stock_info.market 存的是交易所代码(SH/SZ/HK)，
        # 而 MARKET_PRESETS 用 HK/A 分类，这里把 A 股交易所统一映射到 "A"
        norm_market = "A" if market in ("SH", "SZ") else market
        preset = MARKET_PRESETS.get(norm_market)
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
            # 注意：偏移可能跨小时，必须同时进位 hour 并对 minute 取模（0-59），
            # 否则股票数 > 30 时 minute 会超过 59 触发 CronTrigger ValueError → 进程崩溃重启循环。
            if script == "get_financial.py":
                idx = _fin_offset.get(market, 0)
                _fin_offset[market] = idx + 1
                base_min = cron_args.get("minute", 0) + idx * 2
                cron = {
                    **cron_args,
                    "hour": cron_args.get("hour", 9) + base_min // 60,
                    "minute": base_min % 60,
                }
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
    for g_name, g_script, g_cron, g_codes, g_force, g_market, g_timeout in GLOBAL_TASKS:
        # 脚本字段：单个脚本用字符串，多个脚本用列表（如盘中批量采集）
        g_modules = g_script if isinstance(g_script, list) else [g_script]
        sched.add_job(
            execute_task,
            trigger=CronTrigger(**g_cron),
            args=[g_name, g_modules, g_codes, g_force, g_market, g_timeout],
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
