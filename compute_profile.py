#!/usr/bin/env python3
"""全量画像每日计算任务。

执行方式（2026-10-05 起）：独立 systemd 单元 compute-profile.service + .timer
（Mon-Fri 17:30，模板见 system/ 目录）。原 market_scheduler 内的「全量画像计算」
任务已移除：本任务的大内存计算（全市场行情窗口 ~220 万行）曾两次把整机拖入
内存-IO 雪崩（2026-09-29、09-30 各 5.5h / 8.4h），并遗留调度器进程内的 futu
锁死（10-01~10-05 全量超时）——迁出后用 cgroup 内存上限（MemoryMax）把故障
限制在画像自身。手动执行：
    .venv/bin/python3 compute_profile.py
    systemctl start compute-profile.service

行为：
- 先同步 profile.tag_registry（标签字典）+ profile.enum_value（值域），再计算标签。
  字典是「代码即元数据」（@tag 装饰器）upsert 进去的，唯一写入口是 registry.sync_registry，
  而 deploy.sh 不做、engine.compute_all 也不写 → 新环境只跑调度会导致 tag_registry 空表
  （2026-09-21 线上即此情况），故在每日计算里补这一步自愈。
- 写入 profile.tag_value（版本化：值变化才产生新版本，未变标签沿用上一有效版本）。
- 依赖的当日行情快照由同调度器的「收盘采集」任务（A 股 15:10 / 港股 16:20）先行入库。
- force=True 跳过各标签的 update_freq 更新门禁，确保全量重算（未变标签不产生冗余版本）。
- codes / ctx 由调度器传入，但本任务为全局全量，忽略二者。

⚠ 必须 import profiling.tags：标签是「import 时注册」的（@tag 装饰器），
   只 import engine 会让注册表为空 → compute_all 静默返回 []、日志出现「标签数=0」。
   （2026-09-12 上线后连续 3 个交易日空跑，即此原因；下方加了空注册表硬校验防复发。）
"""
import sys
import time
from datetime import date

from db import get_conn
from profiling import engine, registry
from profiling import tags  # noqa: F401  触发全部标签注册（不可删！）
from log_utils import setup_logger

log = setup_logger("compute_profile")


def run(codes=None, ctx=None):
    """计算当日全量画像。异常直接向上冒泡，让调用方（systemd / 调度器）记为失败。

    codes / ctx 仅为兼容原调度器 run_module 的调用签名而保留，本任务全局全量、
    忽略二者。附任务级埋点（collection_task_log）：monitor_collector 的 task_stall
    依赖它判定「全量画像计算」是否停摆（monitor_task_config 已登记该任务）。
    仍被 market_scheduler 进程内调用时不写埋点 —— 调度器 execute_task 已自带
    同名埋点，重复写会产生两条记录（迁移过渡期用，迁到独立服务后自然只走本路径）。
    """
    _rid = None if _in_scheduler() else _task_start()
    _t0 = time.monotonic()
    status = "ok"
    try:
        return _compute()
    except BaseException:
        status = "error"
        raise
    finally:
        _task_end(_rid, status, time.monotonic() - _t0)


def _in_scheduler() -> bool:
    """当前是否运行在 market_scheduler 进程内（其 execute_task 已有任务埋点）。"""
    return "market_scheduler" in sys.modules


def _task_start():
    """任务级埋点起点（best-effort：监控不可用不得影响画像主流程）。"""
    try:
        from monitor_collector import record_task_start
        return record_task_start("全量画像计算")
    except Exception:
        log.warning("任务埋点 record_task_start 失败（不影响计算）", exc_info=True)
        return None


def _task_end(rid, status, elapsed_s):
    """任务级埋点终点（best-effort，同 _task_start）。"""
    if rid is None:
        return
    try:
        from monitor_collector import record_task_end
        record_task_end(rid, status, duration_s=round(elapsed_s, 2))
    except Exception:
        log.warning("任务埋点 record_task_end 失败（不影响计算）", exc_info=True)


def _compute():
    """画像计算主体（原 run 的全部逻辑；埋点包裹在 run 里）。"""
    as_of = date.today()
    n_tags = len(registry.all_tags())
    if n_tags == 0:
        raise RuntimeError(
            "标签注册表为空（profiling.tags 未导入？）——拒绝执行，避免静默产出 0 标签"
        )
    log.info("【全量画像】开始计算 as_of=%s（注册标签 %d 个）", as_of, n_tags)

    # 同步标签字典：单独一个连接先行提交，避免被后续计算异常回滚。
    # 字典不参与计算本身（compute_all 用内存注册表），同步失败不应让当日画像整体缺数，
    # 故只记 error 不中断 —— 但需人工跟进，否则新标签的中文名/值域/单位查不到。
    try:
        with get_conn() as conn_sync:
            ins, upd = registry.sync_registry(conn_sync)
        log.info("【全量画像】标签字典同步：新增 %d / 更新 %d", ins, upd)
    except Exception:
        log.exception("【全量画像】标签字典同步失败（不影响本次计算，需人工检查）")

    # 逐标签独立事务（不要改回「一个连接跑完所有标签」）：
    #   · 77 个标签共用一个事务时，写入会全憋到最后一次 commit 集中爆发。实测写峰值
    #     88k 块/s（tag_value 单行插入要同步维护 4 个索引），叠加 autovacuum /
    #     checkpoint / 收盘采集任务会把磁盘队列打满，导致整机僵死（2026-09-22 16:45）。
    #   · 逐个提交把写峰值摊平到整个执行过程，同时隔离单个标签的失败。
    results = []
    t0 = time.monotonic()
    for meta in registry.all_tags():
        try:
            with get_conn() as conn:
                results.append(engine.compute_tag(
                    conn, meta.code, as_of=as_of, force=True, mode="auto"))
        except Exception:
            log.exception("【全量画像】标签 %s 执行异常（继续后续标签）", meta.code)
            results.append({"tag_code": meta.code, "status": "error",
                            "message": "执行异常，详见日志"})
    n_err = sum(1 for r in results if r.get("status") == "error")
    log.info("【全量画像】完成 as_of=%s, 标签数=%d, 失败=%d, 耗时=%.0fs",
             as_of, len(results), n_err, time.monotonic() - t0)

    # 释放各域缓存：大对象（窗口行情等）在模块级缓存里不会随函数返回释放，
    # 会驻留到下一轮覆盖。独立进程退出时本可自然归还，保留调用是为兼容
    # 「同进程内连续多次计算」（回填/测试）场景。
    engine.clear_caches()
    return True


if __name__ == "__main__":
    # 独立运行入口：由 systemd 单元 compute-profile.service 调用
    # （compute-profile.timer 每日 17:30 触发；模板见 system/ 目录）。
    # 异常自然冒泡 → 非零退出码 → systemd 记为 failed。
    run()
