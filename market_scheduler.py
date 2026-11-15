#!/usr/bin/env python3
"""
市场数据定时调度服务
- 以股票为中心配置，自动生成盘中（分钟级）和收盘（全量）采集任务
- 支持港股和A股，通过 MARKET_PRESETS 定义各市场采集模块
- 股票列表从 realtime_collect_target 配置表加载（行存在=在池；三开关控实时细分），
  新股接入/停用由前端或 setup_new_stock(s).py 管理，改表后重启本服务生效

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
                "get_trend.py",          # 全日趋势数据
            ],
        },
        "extras": [    # 财务指标已改为全局全量任务，见 GLOBAL_TASKS
        ],
    },
}

# ── 股票列表（从 realtime_collect_target 配置表读取）──────────
def load_stocks():
    """从 realtime_collect_target 读取在池股票，返回 [{'code':..., 'market':...}, ...]。

    口径：行存在 = 在池（等价旧 stock_info.is_active=TRUE）→ 日频任务
    （收盘采集 / 相关性 / 宏观）按此全量注册；盘中实时任务另按
    collect_trend 筛选（见 load_intraday_by_market）。
    读表失败直接抛异常 → 进程退出，由 systemd（Restart=on-failure）重启重试。
    """
    from db import get_realtime_targets
    targets = get_realtime_targets()
    stocks = [{"code": c, "market": targets["market"].get(c)} for c in targets["all"]]
    log.info(f"从 realtime_collect_target 加载在池股票 {len(stocks)} 只")
    return stocks


def load_intraday_by_market():
    """盘中批量采集清单：collect_trend=TRUE 的票按市场分组 → {'HK': [...], 'A': [...]}。"""
    from db import get_realtime_targets
    targets = get_realtime_targets()
    by_market = {}
    for code in targets["trend"]:
        mkt = targets["market"].get(code)
        mkt = "A" if mkt in ("SH", "SZ") else mkt
        by_market.setdefault(mkt, []).append(code)
    log.info("盘中批量采集（collect_trend）: %s",
             {m: len(v) for m, v in sorted(by_market.items())} or "无")
    return by_market


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
# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL_TASKS 按 P0→P5 排列、段内按时间升序；新增 / 改时间前先读三条铁律：
#  ① 依赖：T 日口径且被 18:45「市场状态计算」消费的采集必须排在 18:45 前；
#     排在其后的必须写明 T+1 / 滞后口径与「供次日 45 天窗口重算」。
#  ② 盘中：P1 全部冻结（前端分钟级窗口聚合依赖 0 秒起采），禁止改秒位 / 分层。
#  ③ 资源：两个 512M cgroup（sector-layer / -hk）间隔 ≥10min；20 线程池；
#     共享 FutuOpenD 锁的任务不可并发。
# 新增任务先答：写哪张表 / 谁消费 · 源发布时点与 PIT 口径 · 漏跑后果 ·
#               周期与实测耗时 · 属于哪一段。
# ═══════════════════════════════════════════════════════════════════════════
GLOBAL_TASKS: list[GlobalTask] = [  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]
    # ══ P0 盘前 07:00–09:20 · 清单 / 日历 / 参考数据 / 昨日兜底 ══════════════
    # 公司回购 07:00 补昨日 / 17:00 当日盘后（后者属 P2）。
    ("公司回购", "get_buyback.py",
     {"hour": "7,17", "minute": 0, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 南向资金 → daily_ggt_hold；08:00 昨日兜底 / 19:00 盘后终值（后者属 P4，
    # 是 sector-layer-hk.timer 19:20 的直接上游，不可后移）。
    ("南向资金", "get_south_flow.py",
     {"hour": "8,19", "minute": 0, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 行业映射月度复核：confirmed=true 的行不被自动结果覆盖。
    ("行业映射重算", "build_sector_mapping.py",
     {"day": 1, "hour": 8, "minute": 20}, None, True, None, 900),

    # 全市场清单刷新：唯一进 _CATCH_UP_NAMES 白名单的任务；清单塌陷会连累
    # hk/a_daily_quote 采集范围（soft_delete_scope 已按市场门控）。
    ("全市场清单刷新", "sync_quote_universe.py", {"hour": 8, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 300),

    ("交易日历维护", "init_trading_calendar.py", {"hour": 8, "minute": 45, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 融资融券 → daily_margin_balance；09:20 昨日兜底 / 18:20 当日（后者属 P2）。
    ("融资融券全量", "get_margin_balance.py",
     {"hour": "9,18", "minute": 20, "day_of_week": "mon-fri"}, None, True, None, 300),

    # ══ P1 盘中 09:30–16:10 ═══
    ("全球指数采集(每小时)", "get_global_benchmarks.py", {"hour": "*", "minute": 0}, None, True, None, 180),
    ("指数分钟-快源", "get_index_minute_fast.py",
     {"minute": "*", "hour": "9-11,13-16", "day_of_week": "mon-fri"}, None, True, None, 120),
    # 国际源东财反爬敏感，保持 5 分钟；失败一批无损（成功即补齐当天全部分钟）。
    ("指数分钟-国际", "get_index_minute_intl.py",
     {"minute": "*/5", "hour": "8-14", "day_of_week": "mon-fri"}, None, True, None, 120),
    ("港股全市场成交额", "get_hk_market_turnover.py",
     {"minute": "*/5", "second": 0}, None, False, "HK", 300),
    ("A股全市场成交额", "get_a_market_turnover.py",
     {"minute": "*/5", "second": 0}, None, False, "A", 120),
    # 多空力量回合：每分钟只重算当天（days_back=1，单轮 20~25s），
    # 历史补数用 `build_force_rounds.py --days N`；:05 秒位冻结。
    ("多空力量回合", "build_force_rounds.py",
     {"minute": "*", "second": 5, "hour": "9-16", "day_of_week": "mon-fri"}, None, True, None, 900),
    # 行业资金流：源只给当前快照，漏采即永久缺失，必须 A 股收盘后当日采到。
    ("行业资金流", "get_sector_fund_flow.py",
     {"hour": 15, "minute": 35, "day_of_week": "mon-fri"}, None, True, None, 300),

    # ══ P2 盘后采集 16:10–18:40 · 全部早于 18:45 计算闸门 ══════════════════
    ("A股全市场成交额-盘后补采", "get_a_market_turnover.py",
     {"hour": 16, "minute": 10, "day_of_week": "mon-fri"}, None, True, None, 120),
    ("牛熊证街货分布", "get_cbbc.py",
     {"hour": 16, "minute": 25, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("港股全市场成交额-盘后补采", "get_hk_market_turnover.py",
     {"hour": 16, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 300),
    # 沽空 16:50 主采 + 17:40 / 18:30 补采；18:45 计算层依赖当日终值。
    # 17:40 避开 compute-profile.timer（17:30，512M）。
    ("全日沽空数据", "get_realtime_short_selling_fullday.py",
     {"hour": 16, "minute": 50, "day_of_week": "mon-fri"}, None, True, None, 300),
    # 回合收口：清 ongoing 兜底，必须放在分钟轮（9-16 时）覆盖窗口之外。
    ("多空力量回合-盘后收口", "build_force_rounds.py",
     {"hour": 17, "minute": 5, "day_of_week": "mon-fri"}, None, True, None, 900),
    ("全日沽空数据-补采1", "get_realtime_short_selling_fullday.py",
     {"hour": 17, "minute": 40, "day_of_week": "mon-fri"}, None, True, None, 300),

    # 周二：成分归属 18:00 → 展示标签 18:35（须在其后取刚刷新的 INDUSTRY 清单）。
    ("指数成分归属", "get_stock_sector.py", {"day_of_week": 2, "hour": 18, "minute": 0}, None, True, None, 180),
    # PIT 版本链：逐日必跑，eff_to 靠快照对比累积，漏采一天即永久断链。
    ("行业成分主数据", "get_sector_constituent.py",
     {"hour": 18, "minute": 5, "day_of_week": "mon-fri"}, None, True, None, 600),
    ("申万行业指数", "backfill_sw_industry.py",
     {"hour": 18, "minute": 10, "day_of_week": "mon-fri"}, None, True, None, 900),
    # 行业估值快照：源无历史，漏采即永久缺失。
    ("行业估值快照", "get_sector_valuation.py",
     {"hour": 18, "minute": 15, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("宏观月频采集", "get_macro_monthly.py",
     {"hour": 18, "minute": 20, "day_of_week": "mon-fri"}, None, True, None, 600),

    # ── T 日 18:00 PIT 的 5 条宏观序列：被 18:45 计算层当日消费，2026-11-15
    ("中债收益率曲线", "get_cn_bond.py",
     {"hour": 18, "minute": 22, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("中债指数", "get_bond_index_cbond.py",
     {"hour": 18, "minute": 24, "day_of_week": "mon-fri"}, None, True, None, 300),
    # 美债代理：FRED 停机期间的久期近似，只可用于相关性口径，不接 VOL 指标。
    ("美债总回报代理", "get_us_bond_tr_proxy.py",
     {"hour": 18, "minute": 26, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("全A市场估值", "get_macro_valuation.py",
     {"hour": 18, "minute": 28, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("宏观日频采集", "get_macro_daily.py",
     {"hour": 18, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 600),
    ("全日沽空数据-补采2", "get_realtime_short_selling_fullday.py",
     {"hour": 18, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("沪深总市值", "get_market_cap.py",
     {"hour": 18, "minute": 32, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("行业层级映射", "build_sector_hierarchy.py",
     {"day_of_week": 2, "hour": 18, "minute": 35}, None, True, None, 180),
    # 一致预期快照：逐日差分，漏采一天即永久断档。
    ("一致预期快照", "get_analyst_forecast.py",
     {"hour": 18, "minute": 40, "day_of_week": "mon-fri"}, None, True, None, 600),

    # ══ P3 盘后计算 18:45–19:30 ═══════════════════════════════════════════
    # 计算闸门：所有 T 日口径且被当日消费的采集必须排在 18:45 之前。
    # 45 天增量窗口能补齐数据，但不能替代正确排序（顺序错时当日读数慢一天且任务仍记 ok）。
    ("市场状态计算", "market_state_daily.py",
     {"hour": 18, "minute": 45, "day_of_week": "mon-fri"}, None, True, None, 1800),
    # 19:10 sector-layer.timer / 19:20 sector-layer-hk.timer：独立 systemd unit，
    # 不在此列表；全量画像 compute-profile.timer 17:30 亦已迁出（资源规范 R4/R5）。

    # ══ P4 夜间 T+1 19:30–20:30 · 不参与当日计算，供次日重算 ═══════════════
    # 事件采集：滞后一日口径，晚于计算层无碍（豁免范本，注释须写明口径理由）。
    ("市场事件采集", "get_market_events.py",
     {"hour": 19, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 900),
    # 两条股息率 release_time = T+1 18:00，后移不改 PIT 语义。
    # ⚠ 中证源只回最近 20 个交易日 → 每日必采，漏采即永久缺失，不可降优先级。
    ("中证指数股息率", "get_cn_index_dividend.py",
     {"hour": 20, "minute": 0, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("乐咕股息率", "get_dividend_yield_lg.py",
     {"hour": 20, "minute": 5, "day_of_week": "mon-fri"}, None, True, None, 300),
    ("指数成分权重", "get_stock_sector_weight.py", {"day_of_week": 2, "hour": 20, "minute": 30}, None, True, None, 180),

    # ══ P5 低频 · 凌晨 / 周末 ═════════════════════════════════════════════
    # 全量任务超时后 daemon 线程会续跑至自然完成，数据照常入库。
    ("财务指标全量", "get_financial.py",
     {"hour": 2, "minute": 0, "day_of_week": "mon"}, None, True, None, 7200),
    # 公司资料：富途限流 30 次/30s（脚本按 26 次节流），全市场 ≈2.7h。
    ("公司资料全量", "get_company_profile.py",
     {"hour": 3, "minute": 0, "day_of_week": "sun"}, None, True, None, 14400),
    ("分红明细全量", "get_dividend_history.py",
     {"hour": 9, "minute": 0, "day_of_week": "sat"}, None, True, None, 7200),

    # ══ 已停用（保留备查）════════════════════════════════════════════════
    #("数据清理", "cleanup_old_data.py", {"hour": 4, "minute": 0}, None, True, None, 180),
    #("指数相关性分析", "benchmark_correlation_daily.py",
    # {"hour": 17, "minute": 30, "day_of_week": "mon-fri"}, [s["code"] for s in STOCKS], True, None, 180),
    #("宏观环境评分", "macro_environment_score.py",
    # {"hour": 18, "minute": 0, "day_of_week": "mon-fri"}, [s["code"] for s in STOCKS], True, None, 180),

    # 注意：采集层故障监控已由独立服务 monitor_collector.py（常驻进程，
    # systemd: monitor-collector.service）负责，不再挂在调度器里，
    # 以免「调度器挂掉→监控也失效」的同源单点故障。
]

# 需要向模块 run() 传额外关键字参数的全局任务（任务名 → run() kwargs）。
# 注册循环把这些 kwargs 透传给 execute_task → run_script → run_module。
GLOBAL_TASK_KWARGS: dict[str, dict] = {
    # 每分钟一轮只重算「当天」（5 天全量重算单轮 100~135s > 60s cron 间隔，实际被
    # max_instances=1 合并成约 2 分钟一轮；见该任务条目的注释）。
    "多空力量回合": {"days_back": 1},
    # 盘后收口同理只算当天（run() 默认 only_missing=False → 覆盖当天行、清掉 ongoing）。
    "多空力量回合-盘后收口": {"days_back": 1},
}

# ── 盘中批量采集（按市场拆分）────────────────────────────────
# 「每个市场每分钟一个全局任务」，codes=该市场 collect_trend=TRUE 的全量股票，
# 一次批量快照分发（get_quote 写 daily_quote + record_trend 写 trend_snapshot）。
# 作为 GLOBAL_TASKS 项追加，与其他全局任务共用同一条注册循环。
INTRADAY_GLOBAL_MODULES = ["get_quote.py", "record_trend.py"]
_intraday_by_market = load_intraday_by_market()  
for _mkt, _mkt_codes in sorted(_intraday_by_market.items()):  
    GLOBAL_TASKS.append(
        (f"盘中批量采集-{_mkt}", INTRADAY_GLOBAL_MODULES,
         {"second": 0}, _mkt_codes, False, _mkt, 180)
    )


# ── 执行器 ──────────────────────────────────────────────────

def run_script(script_name: str, codes=None, timeout: int = 180,
               kwargs: dict | None = None) -> bool:
    """运行采集模块（常驻进程内调用，无 subprocess 冷启动）。

    通过 collector_runtime.run_module 惰性导入模块并调用其 run(codes, ctx)，
    import 整个进程仅发生一次，FutuOpenD 连接由共享上下文复用。
    用线程+Event 实现超时：超时后线程被标记为 daemon 自动随进程退出，
    不阻塞 scheduler 主循环。

    Args:
        codes: 股票代码列表；可为 None（全局任务，由模块自行决定范围）、
               str（单票，自动包成 [str]）或 list（多票全局任务）。
        kwargs: 额外传给模块 run() 的关键字参数（如 build_force_rounds 的
                days_back）；None 表示用模块自身默认值。
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
            run_module(script_name, codes, **(kwargs or {}))
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
                 force: bool = False, market: str | None = None, timeout: int = 180,
                 kwargs: dict | None = None):
    """执行一组采集模块（顺序执行）

    Args:
        name: 任务名称（日志用）
        modules: 脚本名列表，空列表跳过
        codes: 股票代码列表；None 表示全局任务（由模块自行决定范围）、
               str 表示单票任务、list 表示多票全局任务（如全票相关性分析）
        force: 是否跳过交易时段检查
        market: 市场标识（"HK"/"A"），用于交易时段判断；None 时不检查
        timeout: 单个脚本超时秒数，默认 180
        kwargs: 额外传给各模块 run() 的关键字参数（见 GLOBAL_TASK_KWARGS）
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
        ok = run_script(script, codes, timeout, kwargs)
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


# ── 启动补跑（2026-10-26 事故）────────────────────────────────
# APScheduler 的 CronTrigger 只在进程存活期间触发：进程若在计划时点"之后"启动
# （10-27 / 10-28 / 10-29 分别 09:09 / 10:46 / 10:37 启动），当天 08:30 的清单刷新
# 整天不执行 → 清单塌陷状态无人修复，断供从 1 天拖成 4 个交易日。
# 下面在启动时对白名单任务补跑一次（仅「当天尚无 ok 记录」时），复用 execute_task
# 的超时 / 日志 / collection_task_log 埋点，因此补跑与正常触发监控口径一致。
_CATCH_UP_NAMES = ("全市场清单刷新",)


def _catch_up_specs():
    """白名单任务的执行参数 → [(名字, 脚本, hour, minute, timeout)]。

    时点从 GLOBAL_TASKS 读（单一真相源，避免两处 cron 漂移）；高频/区间任务
    （hour="*" 或 "9-16"）不参与补跑——它们下一次触发就在几分钟内，无需补。
    """
    specs = []
    for name in _CATCH_UP_NAMES:
        for g_name, g_script, g_cron, _codes, _force, _mkt, g_timeout in GLOBAL_TASKS:
            if g_name != name:
                continue
            hour, minute = g_cron.get("hour"), g_cron.get("minute")
            if not isinstance(hour, int) or not isinstance(minute, int):
                break
            specs.append((name, g_script if isinstance(g_script, str) else g_script[0],
                          hour, minute, g_timeout))
            break
    return specs


def _has_ok_today(task_name: str) -> bool:
    """当天是否已有该任务的 ok 记录。读库失败一律按「有」处理（不补跑，避免启动期雪崩）。"""
    try:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 按香港时区比日期（::date 依赖会话时区，服务器 PG 若为 UTC 会在凌晨错日）
                cur.execute(
                    "SELECT 1 FROM collection_task_log "
                    "WHERE task_name = %s AND status = 'ok' "
                    "AND (started_at AT TIME ZONE 'Asia/Hong_Kong')::date "
                    "  = (NOW() AT TIME ZONE 'Asia/Hong_Kong')::date LIMIT 1", (task_name,))
                return cur.fetchone() is not None
    except Exception as e:
        log.warning(f"[启动补跑] 查询 {task_name} 执行记录失败"
                    f"（{type(e).__name__}: {e}）→ 本次不补跑")
        return True


def missed_catch_up_tasks(now=None, has_ok_today=None):
    """启动时需补跑的任务 → [(名字, 脚本, 超时)]；空 = 无需补跑。

    判定：工作日 + 已过计划时点 + 当天无 ok 记录。has_ok_today 可注入（供测试）。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return []
    check = has_ok_today or _has_ok_today
    out = []
    for name, script, hour, minute, timeout in _catch_up_specs():
        if (now.hour, now.minute) < (hour, minute):
            continue
        if check(name):
            continue
        out.append((name, script, timeout))
    return out


def schedule_catch_up(sched, now=None):
    """把补跑任务挂到调度器（延迟 10s，走 default 线程池，由 scheduler 统一执行）。"""
    from datetime import timedelta
    for name, script, timeout in missed_catch_up_tasks(now):
        log.warning(f"[启动补跑] 今天 {name} 尚未成功（进程在计划时点之后启动）→ 补跑一次")
        sched.add_job(
            execute_task,
            "date",
            run_date=datetime.now() + timedelta(seconds=10),
            args=[name, [script], None, True, None, timeout],
            id=f"{name}-启动补跑",
            replace_existing=True,
        )


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
        # 需要显式传 run() 参数的任务（如 build_force_rounds 的 days_back=1）
        g_kwargs = GLOBAL_TASK_KWARGS.get(g_name)
        sched.add_job(
            execute_task,
            trigger=CronTrigger(**g_cron),
            args=[g_name, g_modules, g_codes, g_force, g_market, g_timeout, g_kwargs],
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

    # 启动补跑：弥补「进程在计划时点之后启动 → 当天任务整日不触发」（2026-10-26 事故）
    try:
        schedule_catch_up(sched)
    except Exception as e:
        log.warning(f"[启动补跑] 挂载失败: {type(e).__name__}: {e}")

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
