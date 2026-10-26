"""构建多空力量回合日表（force_round_daily）· 每日盘后跑批

范围：采集管理中打开「实时交易流水」（realtime_collect_target.collect_tick=TRUE）的股票。
时机：港股收市竞价 16:00-16:10 结束后 16:40 跑（market_scheduler.py 注册），
      留 30 分钟缓冲——回合的「静默收口」需连续 5 分钟不活跃才判定，盘中跑会切错边界。

依赖：只依赖本工程的 db / force_dynamics，**不 import stock-realtime**。
      四档阈值读 fund_flow_settings.thresholds_json（线上生效口径，与分时图同源）。
      表结构见 sql/schema.sql；口径定义见 stock-realtime/docs/force_dynamics_design.md。

用法：
    python3 build_force_rounds.py                # 增量（只补缺失/变更日）
    python3 build_force_rounds.py --days 94      # 回填最近 N 个交易日
    python3 build_force_rounds.py --code HK.00700 --days 20   # 单票调试
    python3 build_force_rounds.py --quiet        # 长跑只留每票汇总（少刷屏）

日志（一次全池 5 日约 30 分钟、94 日回填约 8 小时，故必须能看出「在跑还是卡死」）：
    每项一行进度（写入/跳过原因 + 回合数 + 耗时 + 预计剩余）
    → 每票汇总 → 结尾统计（含跳过明细）。
    单项超过 60s 无输出时有心跳提示。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import psycopg2.extras

import db
import force_dynamics as fd

log = logging.getLogger("build_force_rounds")

TIERS = fd.TIERS

# 兜底四档阈值（股）：与 stock-realtime config_store._DEFAULTS 完全一致。
# 仅在 fund_flow_settings 缺该票行时使用（2026-10-25 已把 20 只缺配置票补进该表）。
_DEFAULT_THRESHOLDS = {"xlarge": 25_000, "large": 7_000, "medium": 300, "small": 100}

# ── 进度输出 ──────────────────────────────────────────────────
# 单票单日要扫一遍 tick_data（实测 HK.00700 约 5s/日），全池 5 日 ≈ 27 分钟、
# 94 日回填 ≈ 8 小时。原实现只在首尾各打一行，长跑时完全分不清「在跑」「卡死」。
_HEARTBEAT_SEC = 60.0
_SKIP_REASONS = {"exists": "已存在", "no_th": "无四档阈值", "thin": "分钟面板不足"}


def _fmt_dur(sec: float) -> str:
    """秒 → m:ss（超 1 小时用 h:mm:ss）。"""
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_eta(t0: float, idx: int, total: int) -> str:
    """按已完成项的平均耗时估算剩余时间。"""
    if idx <= 0:
        return ""
    left = (time.time() - t0) / idx * (total - idx)
    return f"｜剩 {_fmt_dur(left)}"


def _fmt_item(idx: int, total: int, code: str, day: dt.date, st: dict[str, Any]) -> str:
    """单项进度行：写入项带回合统计，跳过项带原因。"""
    head = f"[{idx}/{total} {idx * 100.0 / total:>3.0f}%] {code} {day}"
    if st["status"] != "ok":
        body = f"跳过·{_SKIP_REASONS[st['reason']]}"
        if st.get("minutes") is not None:
            body += f"（{st['minutes']} 分钟）"
    else:
        lam = st.get("lam")
        lam_txt = "-" if lam is None else f"{lam:.3f}"
        body = (f"{st['minutes']} 分钟 {st['rounds']} 回合"
                f" λ_pct {lam_txt} {'适用' if st['applicable'] else '不适用'}")
    return f"{head}｜{body}｜{st['elapsed']:.1f}s"


class _Heartbeat:
    """长查询存活提示：超过 interval 秒没有输出就打一行，避免被误判为卡死。"""

    def __init__(self, interval: float = _HEARTBEAT_SEC):
        self.interval = interval
        self._cur = "启动中"
        self._last = time.time()
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def touch(self, current: str | None = None) -> None:
        if current:
            self._cur = current
        self._last = time.time()

    def start(self) -> None:
        self._th = threading.Thread(target=self._loop, name="bfr-heartbeat", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        poll = max(0.05, min(5.0, self.interval / 4))
        while not self._stop.wait(poll):
            idle = time.time() - self._last
            if idle < self.interval:
                continue
            self._last = time.time()
            try:
                log.info("…… 仍在计算：%s（已 %.1fs 无输出）", self._cur, idle)
            except Exception:  # noqa: BLE001  日志故障不能影响主流程
                return


def _thresholds(cur, code: str) -> dict | None:
    """读线上生效阈值（fund_flow_settings）。

    缺行时返回 None（不静默用默认值）——阈值口径必须与分时图四档一致，
    而分时图那边缺配置时用的是 config_store 的默认阈值，故这里也用同一组默认值兜底，
    保证「页面上看到的四档」与「跑批算出的四档」永远是同一套。
    """
    cur.execute("SELECT thresholds_json FROM fund_flow_settings WHERE stock_code=%s", (code,))
    row = cur.fetchone()
    if not row or not row[0]:
        return dict(_DEFAULT_THRESHOLDS)
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def load_panel(cur, code: str, day: dt.date, th: dict) -> list[dict]:
    """分钟面板：价格取 trend_snapshot，四档主动买卖由 tick_data 实时分类聚合。

    与 stock-realtime 的 get_intraday_fund_flow 同口径：ticker_direction IN ('BUY','SELL')
    按分钟聚合，volume 落在阈值区间归档，成交额 = price × volume。
    分类与聚合全部在 SQL 内完成（单票单日 tick 数万行，Python 逐行分类会慢一个量级）。
    """
    cur.execute(
        """
        SELECT snapshot_time, price FROM trend_snapshot
        WHERE stock_code=%s AND snapshot_date=%s AND price IS NOT NULL
        ORDER BY snapshot_time
        """, (code, day))
    pmap = {r[0].strftime("%H:%M"): float(r[1]) for r in cur.fetchall()}
    if not pmap:
        return []

    # 注意：用 tick_time 范围而不是 tick_time::date=day —— 后者对每行调函数、
    # 无法走 (stock_code, tick_time) 索引，单票单日从 <1s 劣化到 9s+。
    cur.execute(
        """
        SELECT to_char(tick_time, 'HH24:MI') AS t,
               CASE WHEN volume >= %(xl)s THEN 'super'
                    WHEN volume >= %(lg)s THEN 'big'
                    WHEN volume >= %(md)s THEN 'mid'
                    ELSE 'small' END AS tier,
               ticker_direction,
               SUM(price * volume) AS amt
        FROM tick_data
        WHERE stock_code=%(code)s AND tick_time >= %(day)s AND tick_time < %(day)s + interval '1 day'
          AND ticker_direction IN ('BUY', 'SELL') AND volume > 0
        GROUP BY 1, 2, 3
        """, {"code": code, "day": day, "xl": th["xlarge"], "lg": th["large"], "md": th["medium"]})

    agg: dict[str, dict] = {}
    for t, tier, direction, amt in cur.fetchall():
        if t not in pmap or amt is None:
            continue
        a = agg.setdefault(t, {k: [0.0, 0.0] for k in TIERS})
        a[tier][0 if direction == "BUY" else 1] += float(amt)

    rows = []
    for t in sorted(agg):
        buy = sum(agg[t][k][0] for k in TIERS)
        sell = sum(agg[t][k][1] for k in TIERS)
        rows.append(dict(
            t=t, price=pmap[t], buy=buy, sell=sell, net=buy - sell, g=buy + sell,
            main_share=0.0, tiers={k: tuple(agg[t][k]) for k in TIERS},
        ))
    return rows


def upsert(cur, code: str, day: dt.date, rounds: list[dict], applicable: bool, lambda_median) -> None:
    """写一行 force_round_daily（rounds 为 jsonb，NaN/Inf 需转 null 否则 jsonb 报错）。"""
    def clean(o):
        if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
            return None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o

    cur.execute(
        """
        INSERT INTO force_round_daily (stock_code, trade_date, rounds, lambda_median, applicable, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (stock_code, trade_date) DO UPDATE SET
            rounds=EXCLUDED.rounds, lambda_median=EXCLUDED.lambda_median,
            applicable=EXCLUDED.applicable, updated_at=now()
        """, (code, day, psycopg2.extras.Json(clean(rounds), dumps=json.dumps),
              lambda_median, applicable))


def recent_round_count(cur, code: str, day: dt.date, window: int = 20) -> int:
    """近 window 个已落库交易日的回合数合计（适用性 = 样本充足性检查）。

    2026-10-26 起 `is_main` 已移除（窗口改由股价趋势段决定，一天 6~10 段，
    「主回合」这一细分不再有意义），判据从「主回合数」改为「回合数」。
    """
    cur.execute(
        """
        SELECT COALESCE(SUM(jsonb_array_length(rounds)), 0) FROM (
            SELECT rounds FROM force_round_daily
            WHERE stock_code=%s AND trade_date < %s
            ORDER BY trade_date DESC LIMIT %s
        ) t
        """, (code, day, window))
    return int(cur.fetchone()[0] or 0)


def _is_live(day: dt.date) -> bool:
    """day 是否为「当日且尚未收市」——用于把未收口的最后回合标为进行中。

    港股收市竞价 16:00-16:10 结束，故 16:15 之后视为已收盘（数据完整）。
    """
    if day != dt.date.today():
        return False
    return dt.datetime.now().time() < dt.time(16, 15)


def resolve_scope(codes: list[str] | None = None, days: list[dt.date] | None = None,
                  days_back: int = 5) -> tuple[list[str], list[dt.date]]:
    """解析跑批范围：codes 缺省 = collect_tick 全池；days 缺省 = 最近 days_back 个交易日。

    days 一律返回**日期正序**——适用性依赖历史回合逐日累计，乱序会算错。
    """
    with db.get_conn() as conn, conn.cursor() as cur:
        if not codes:
            cur.execute("SELECT stock_code FROM realtime_collect_target WHERE collect_tick ORDER BY 1")
            codes = [r[0] for r in cur.fetchall()]
        if not codes:
            return [], []
        if days is None:
            cur.execute(
                """
                SELECT DISTINCT snapshot_date FROM trend_snapshot
                WHERE stock_code = ANY(%s) ORDER BY 1 DESC LIMIT %s
                """, (list(codes), days_back))
            days = sorted(r[0] for r in cur.fetchall())
    # 始终正序（含调用方显式传入的 days）：适用性靠逐日累计回合数，乱序会算错
    return list(codes), sorted(days)


def process_one(code: str, day: dt.date, th: dict[str, Any],
                only_missing: bool) -> dict[str, Any]:
    """计算并落库一票一日，返回状态描述（供进度行与统计用）。

    自建连接（EMA 的 db.get_conn() 每次新建，线程安全）——run() 天内并行调用本函数。
    """
    t0 = time.time()
    try:
        with db.get_conn() as conn, conn.cursor() as cur:
            if only_missing:
                cur.execute(
                    "SELECT 1 FROM force_round_daily WHERE stock_code=%s AND trade_date=%s",
                    (code, day))
                if cur.fetchone():
                    return dict(status="skip", reason="exists", elapsed=time.time() - t0)
            rows = load_panel(cur, code, day, th)
            if len(rows) < 60:
                return dict(status="skip", reason="thin", minutes=len(rows),
                            elapsed=time.time() - t0)
            # 回合窗口 = 股价趋势段（上涨/下跌/横盘），见 force_dynamics.rounds_by_trend。
            # 2026-10-26 从 detect_rounds（资金相位切分）切换：原口径一天几十个碎块。
            rounds = fd.rounds_by_trend(rows)
            # 盘中（当日且未过收市竞价）：最后一个趋势段其实还在走，标 ongoing，
            # 前端据此显示「进行中」且不画结果，避免把未收口的走势误示为已结束。
            # 注：趋势段窗口下 end 是「趋势」而不再是「收盘」，故不再用 end 判断。
            if _is_live(day) and rounds:
                rounds[-1]["ongoing"] = True
            # λ_pct 中位数 = 全部回合的 eff_pct 中位（横盘段为 NaN，已被过滤）
            lams = [r["eff_pct"] for r in rounds if r["eff_pct"] == r["eff_pct"]]
            lam_med = float(np.median(lams)) if lams else None
            # 适用性：近 20 个已落库交易日的回合数合计 ≥20（样本充足才输出结论）。
            # 依赖按日期正序跑（resolve_scope 已排序），历史回填时逐日累计才准确。
            prev = recent_round_count(cur, code, day)
            applicable = prev + len(rounds) >= 20
            upsert(cur, code, day, rounds, applicable, lam_med)
            conn.commit()
            return dict(status="ok", minutes=len(rows), rounds=len(rounds),
                        lam=lam_med, applicable=applicable, elapsed=time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.debug("%s %s 失败堆栈", code, day, exc_info=True)
        return dict(status="fail", error=f"{type(e).__name__}: {e}", elapsed=time.time() - t0)


def run(codes: list[str] | None = None, days: list[dt.date] | None = None,
        only_missing: bool = False, quiet: bool = False, days_back: int = 5,
        workers: int = 8, ctx=None) -> None:
    """跑批入口。

    codes / days 为 None 时自行解析范围（见 resolve_scope）。
    ctx 仅为兼容 collector_runtime.run_module(script, codes) 的调用约定——本模块只用
    数据库，不需要富途行情上下文，收到即忽略。

    并行结构（2026-10-25）：**天级串行 + 天内并行**。
    - 天级串行：适用性（近 20 日主回合累计）依赖历史逐日累计，乱序会算错，
      故 days 必须正序逐天推进。
    - 天内并行：81 只票彼此独立（各自查各自的数据），瓶颈是 tick_data 聚合
      （单票单日约 2.9s，SQL 内 CASE WHEN + GROUP BY），8 线程后单轮 233s → ~29s，
      可支撑「盘中每分钟一轮」。
    """
    del ctx
    codes, days = resolve_scope(codes, days, days_back)
    if not codes or not days:
        log.warning("范围为空（%d 只票 × %d 个交易日），无需计算", len(codes), len(days))
        return

    total = len(codes) * len(days)
    log.info("开始：%d 只票 × %d 个交易日（%s ~ %s），共 %d 项，模式=%s，并行 %d 线程",
             len(codes), len(days), days[0], days[-1], total,
             "增量（跳过已存在）" if only_missing else "全量重算", workers)

    t0 = time.time()
    idx = done = failed = 0
    n_rounds = 0
    skips = {k: 0 for k in _SKIP_REASONS}
    hb = _Heartbeat()
    hb.start()
    try:
        # 阈值按票配置：一次连接取全部票的阈值（省掉每票重开连接）
        ths: dict[str, dict[str, Any]] = {}
        with db.get_conn() as conn, conn.cursor() as cur:
            for code in codes:
                th = _thresholds(cur, code)
                if th:
                    ths[code] = th
                else:
                    skips["no_th"] += len(days)
                    idx += len(days)
                    log.warning("%s 无四档阈值（fund_flow_settings 无该票行），跳过 %d 个交易日",
                                code, len(days))

        def _task(item: tuple[str, dt.date]) -> dict[str, Any]:
            code, day = item
            return process_one(code, day, ths[code], only_missing)

        for day in days:                      # 天级串行：保 applicable 逐日累计正确
            tasks = [(c, day) for c in ths]
            if not tasks:
                continue
            day_t0 = time.time()
            d_done = d_skip = d_fail = d_rounds = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for (code, d), st in zip(tasks, ex.map(_task, tasks)):
                    idx += 1
                    hb.touch(f"{day} 并行 {len(tasks)} 只")
                    if st["status"] == "ok":
                        done += 1
                        d_done += 1
                        d_rounds += st["rounds"]
                        n_rounds += st["rounds"]
                    elif st["status"] == "fail":
                        failed += 1
                        d_fail += 1
                        log.warning("[%d/%d] %s %s 失败: %s", idx, total, code, d,
                                    st.get("error"))
                    else:
                        skips[st["reason"]] += 1
                        d_skip += 1
                    if not quiet:
                        log.info("%s%s", _fmt_item(idx, total, code, d, st),
                                 _fmt_eta(t0, idx, total))
            log.info("%s 汇总：写入 %d / 跳过 %d / 失败 %d｜回合 %d｜%.1fs（%d 线程）",
                     day, d_done, d_skip, d_fail, d_rounds,
                     time.time() - day_t0, workers)
    finally:
        hb.stop()

    dur = time.time() - t0
    log.info("完成：写入 %d / 跳过 %d / 失败 %d（共 %d 项）｜回合 %d｜"
             "耗时 %s｜平均 %.1fs/项",
             done, sum(skips.values()), failed, total, n_rounds,
             _fmt_dur(dur), dur / max(1, idx))
    detail = "、".join(f"{_SKIP_REASONS[k]} {v}" for k, v in skips.items() if v)
    if detail:
        log.info("跳过明细：%s", detail)


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 force_round_daily（多空力量回合日表）")
    ap.add_argument("--code", help="单票调试；缺省跑 collect_tick=TRUE 全池")
    ap.add_argument("--days", type=int, default=1,
                    help="处理最近 N 个交易日（默认 1，供 scheduler 每分钟轮用；回填用 --days 94）")
    ap.add_argument("--all", action="store_true", help="忽略已存在行，全量重算")
    ap.add_argument("--quiet", action="store_true", help="只打每日汇总与最终统计，不逐项打印")
    ap.add_argument("--workers", type=int, default=8, help="天内并行线程数（默认 8）")
    args = ap.parse_args()

    # 显式 stream=sys.stdout：basicConfig 默认写 stderr，
    # `python3 build_force_rounds.py > run.log 2>&1` 之外的 `> run.log` 会一个字都看不到。
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")

    run([args.code] if args.code else None, only_missing=not args.all,
        quiet=args.quiet, days_back=args.days, workers=args.workers)


if __name__ == "__main__":
    main()
