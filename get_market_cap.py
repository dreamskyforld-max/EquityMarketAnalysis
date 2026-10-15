#!/usr/bin/env python3
"""沪深股票总市值采集（上交所/深交所官网统计）—— 写入 regime.macro_series。

数据源：AKShare 两家交易所的每日统计接口
  · 沪：`stock_sse_deal_daily(date)` → 「市价总值 / 股票」列（单位：亿元）
  · 深：`stock_szse_summary(date)`   → 「证券类别=股票 / 总市值」列（单位：元 → ÷1e8 亿元）
  · 用途：补齐 market_profile §4.1「巴菲特指标（股市总市值 / GDP）」的**分子**——
    此前全库无总市值，故该指标不可算。

落库序列（freq=day，unit=yi_yuan）：
    CN.MKT_CAP_SSE    沪市股票总市值
    CN.MKT_CAP_SZSE   深市股票总市值
    CN.MKT_CAP_TOTAL  沪深股票总市值合计（= 前两者之和；**分开存是为了可诊断**——
        单边源失败时合计会静默腰斩，有分项即可用自检发现）

⚠ 历史深度不对称（实测 2026-10-15）：深市 `stock_szse_summary` 可回溯到 **2010**；
  沪市 `stock_sse_deal_daily` 仅到 **2022**（更早日期接口返回格式不同直接报错）。
  → 回填时沪市早于边界的日期会跳过，`CN.MKT_CAP_SSE` 起始日即沪市边界；
    按「宁缺勿错」不推算补值（用深市或指数反推沪市会引入不可控误差）。

PIT 约定：release_time = 观测日 18:00(+08)（交易所盘后发布当日统计）。

用法:
    python3 get_market_cap.py                       # 增量：最近 N 个交易日
    python3 get_market_cap.py --backfill --start 2022-01-01
"""
import argparse
import datetime
import socket
import sys
import time as _time

# ⚠ 实测（2026-10-15）：连续回填时交易所侧会**挂起连接**（不返回也不报错）——异常捕获无效、
#   进程永久卡死。AKShare 内部请求不带 timeout，故在此设全局 socket 超时兜底；超时会被
#   _fetch 的 except 捕获并按「该日无数据」跳过，代价是可能丢掉个别真实交易日。
HTTP_TIMEOUT = 30
PER_DAY_TIMEOUT = 45                     # 单日请求的线程级超时（防挂起，见 _fetch）

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

SOURCE = "akshare:sse_deal_daily+szse_summary"
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))
YI = 1e8                                  # 元 → 亿元

SPECS = [
    dict(code="CN.MKT_CAP_SSE", name="沪市股票总市值", unit="yi_yuan", field="sse",
         remark="上交所每日统计「市价总值-股票」（亿元）；⚠ 源历史仅到 2022（更早接口格式不同）"),
    dict(code="CN.MKT_CAP_SZSE", name="深市股票总市值", unit="yi_yuan", field="szse",
         remark="深交所每日统计「证券类别=股票」总市值（元÷1e8）；源可回溯到 2010"),
    dict(code="CN.MKT_CAP_TOTAL", name="沪深股票总市值合计", unit="yi_yuan", field="total",
         remark="沪 + 深；分开存是为了可诊断——单边源失败时合计会静默腰斩"),
]


def _specs() -> list:
    return [dict(series_code=s["code"], series_name=s["name"], unit=s["unit"],
                 freq="day", market="CN", source=SOURCE, lag_days=0,
                 collect_params={"field": s["field"]},
                 remark=s["remark"])
            for s in SPECS]


def _defs() -> dict:
    """播种序列契约 → 读回「库里生效的定义」（库里的值可覆盖代码）。"""
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _one_day(ak, d: datetime.date, retries: int = 2):
    """取某日的 (沪, 深)（亿元）；任一边不可得则为 None。

    ⚠ 实测（2026-10-15）：连续快速请求会触发交易所侧限流 —— 表现为调用**长时间挂起**而非
      立即报错，回填因而卡死。故每次失败重试 + 失败原因打印（便于区分「限流」与「该日无数据」），
      回填循环另加节流 sleep。
    """
    import time as _time

    sse = szse = None
    ds = d.strftime("%Y%m%d")
    for attempt in range(retries + 1):
        try:
            df = ak.stock_sse_deal_daily(date=ds)
            row = df[df["单日情况"] == "市价总值"]
            if len(row):
                v = float(row["股票"].iloc[0])
                sse = v if v > 0 else None
            break
        except Exception as e:
            if attempt == retries:
                print(f"    ⚠️ {d} 沪市失败: {type(e).__name__}: {str(e)[:50]}")
            else:
                _time.sleep(1.5)
    for attempt in range(retries + 1):
        try:
            df = ak.stock_szse_summary(date=ds)
            row = df[df["证券类别"] == "股票"]
            if len(row):
                v = float(row["总市值"].iloc[0]) / YI
                szse = v if v > 0 else None
            break
        except Exception as e:
            if attempt == retries:
                print(f"    ⚠️ {d} 深市失败: {type(e).__name__}: {str(e)[:50]}")
            else:
                _time.sleep(1.5)
    return sse, szse


def _fetch(days: list, per_day_timeout: int = PER_DAY_TIMEOUT) -> list:
    """按日期列表逐日采集 → macro_series 行。两个源各自独立失败，不互相拖累。

    `per_day_timeout` 不为空时，**每日请求跑在独立线程里并设超时** —— 实测交易所侧偶尔把连接
      挂起（不返回也不报错），`socket.setdefaulttimeout` 会被 urllib3 的显式 `timeout=None`
      覆盖、异常捕获也无效，只有线程级超时能救；超时后**放弃该线程**（`shutdown(wait=False)`）
      并跳过该日，不阻塞后续日期。多留几个 worker，避免个别挂起线程占满。
    """
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    socket.setdefaulttimeout(HTTP_TIMEOUT)
    rows = []
    if per_day_timeout:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
        ex = ThreadPoolExecutor(max_workers=4)
    try:
        for d in days:
            if per_day_timeout:
                fut = ex.submit(_one_day, ak, d)
                try:
                    sse, szse = fut.result(timeout=per_day_timeout)
                except FutTimeout:
                    print(f"    ⚠️ {d} 请求超时（>{per_day_timeout}s）→ 跳过", flush=True)
                    fut.cancel()
                    sse = szse = None
            else:
                sse, szse = _one_day(ak, d)
            if sse is None and szse is None:
                continue
            # ⚠ 只有一边可得时**不写 TOTAL**（会少算一半 → 巴菲特指标失真）
            total = (sse + szse) if (sse is not None and szse is not None) else None
            rel = datetime.datetime.combine(d, datetime.time(18, 0), tzinfo=TZ_CN)
            for s in SPECS:
                v = {"sse": sse, "szse": szse, "total": total}[s["field"]]
                if v is None:
                    continue
                rows.append({
                    "series_code": s["code"], "period_date": d,
                    "value": round(float(v), 4), "release_time": rel, "revision": 0,
                    "updated_at": datetime.datetime.now(TZ_CN),
                })
    finally:
        if per_day_timeout:
            ex.shutdown(wait=False)          # 不等待可能挂起的线程
    return rows


def _save(rows: list, defs: dict = None) -> int:
    if not rows:
        return 0
    apply_series_defs(rows, defs if defs is not None else _defs())
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
    return len(rows)


def run(days: int = 10) -> int:
    """增量：最近 days 个自然日（覆盖长假，幂等）。"""
    end = datetime.date.today()
    dates = [d.date() for d in
             __import__("pandas").bdate_range(end=end, periods=days)]
    defs = _defs()
    rows = _save(_fetch(dates), defs)
    print(f"  ✅ 增量: 写入 {rows} 条（{dates[0]} ~ {dates[-1]}，{len(dates)} 个工作日）")
    return rows


def backfill(start: datetime.date, end: datetime.date = None, step_days: int = 1) -> int:
    """全历史回填：逐工作日拉取（源无批量接口）。沪市早于其边界者自动跳过。

    `step_days > 1` 时按固定间隔抽样（**慢变量的降采样**）：巴菲特指标是极长期估值锚，
      周频样本足以支撑分位，而把请求数降到 1/5 能显著降低被限流挂起的概率。
    """
    import pandas as pd
    end = end or datetime.date.today()
    dates = [d.date() for d in pd.bdate_range(start=start, end=end)]
    if step_days > 1:
        dates = dates[::step_days]
    defs = _defs()
    total, done, started = 0, 0, datetime.datetime.now()
    for i, d in enumerate(dates):
        rows = _fetch([d])
        if rows:
            total += _save(rows, defs)
            done += 1
        if (i + 1) % 50 == 0:
            print(f"    · 已处理 {i + 1}/{len(dates)} 个采样日，写入 {total} 条"
                  f"（已耗时 {int((datetime.datetime.now() - started).total_seconds())}s）",
                  flush=True)
        _time.sleep(0.6)                     # 节流：连续快请求会被交易所侧限流并挂起
    print(f"  ✅ 回填完成: {done}/{len(dates)} 个采样日有数据，累计 {total} 条")
    return total


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="全历史回填（逐工作日或抽样）")
    ap.add_argument("--start", default="2022-01-01", help="回填起始日（沪市边界约 2022-02）")
    ap.add_argument("--end", default=None, help="回填截止日（缺省今日；可分段跑）")
    ap.add_argument("--step-days", type=int, default=1, help="采样间隔（工作日数；>1 = 降采样）")
    ap.add_argument("--days", type=int, default=10, help="增量回看自然日数")
    args = ap.parse_args()
    print("=" * 60)
    print("沪深股票总市值采集 → regime.macro_series")
    print("=" * 60)
    if args.backfill:
        backfill(datetime.date.fromisoformat(args.start),
                 datetime.date.fromisoformat(args.end) if args.end else None,
                 step_days=args.step_days)
    else:
        run(days=args.days)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT series_code, count(*), min(period_date), max(period_date)
                           FROM regime.macro_series
                           WHERE series_code LIKE 'CN.MKT_CAP%' GROUP BY 1 ORDER BY 1""")
            print("\n── regime.macro_series（CN.MKT_CAP*）──")
            for r in cur.fetchall():
                print(f"  {r[0]:18s} {r[1]:>5} 行   {r[2]} ~ {r[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
