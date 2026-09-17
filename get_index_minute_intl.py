#!/usr/bin/env python3
"""
指数分钟采集 · 国际（日经225 / 韩国KOSPI，东方财富 push2 kline）

调度：market_scheduler「指数分钟-国际」，每 5 分钟、仅交易日 8-14 时
      （日经 8:00-14:30、KOSPI 8:00-14:30，均落在该窗口内）。
数据源：push2.eastmoney.com/api/qt/stock/kline/get（klt=1）。
      实测该接口单次只支持一个 secid；东财 ulist 批量接口只有实时快照、无分钟 K 线。
落表：benchmark_minute（UNIQUE(bench_code, ts)，bulk_upsert 单事务写入）。

东财容错策略（反爬敏感，是本项目最易限流的数据源）：
  - 每指数最多尝试 2 次、退避 2s、超时 8s；失败立即放弃本批，交下一批重试。
  - 不做长退避大重试：接口返回的是「当天全部分钟 bar」，任意一批成功即可补齐当日
    全部缺失分钟；高频重试只会加剧限流（历史教训：4 次重试 + 20s 超时曾把单批拖到
    200s+，触发任务重叠 skip，并让东财连续数小时不可用）。
  - 增量写库：只写「库内 MAX(ts) 回退 2 分钟」之后的 bar，兼顾效率与末根 bar 修正。

手动补采：历史回溯请用 get_global_benchmarks_minute.py（保留的 CLI 全量版）。
    python3 get_index_minute_intl.py     # 单次采集（当日增量）
"""
import logging
import time as _t
from datetime import datetime, timedelta, date, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from db import get_conn, bulk_upsert

logger = logging.getLogger(__name__)

# ── 指数定义（国际源）──
INTL_BENCHMARKS: List[Dict] = [
    {"code": "JP.N225", "name": "日经225指数",   "em_secid": "100.N225", "tz": "Asia/Tokyo"},
    {"code": "KR.KS11", "name": "韩国KOSPI指数", "em_secid": "100.KS11", "tz": "Asia/Seoul"},
]

TABLE = "benchmark_minute"
REWRITE_MINUTES = 2   # 增量写库回退窗口（分钟）：重写最后一根未定格的 bar

_EM_URL = "https://push2.eastmoney.com/api/qt/stock/kline/get"
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
_EM_BASE = {
    "klt": "1", "fqt": "1", "lmt": "1000", "end": "20500000", "iscca": "1",
    "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
    "ut": "f057cbcbce2a86e2866ab8877db1d059", "forcect": "1",
}


# ── 工具 ──
def _r(v, nd: int = 4):
    try:
        return round(float(v), nd) if v is not None else None
    except Exception:
        return None


def _to_utc(local_str: str, tz_name: str):
    """把 'YYYY-MM-DD HH:MM:SS' 本地时间转 UTC；无 zoneinfo 时返回 None。"""
    if ZoneInfo is None:
        return None
    try:
        dt = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(tz_name))
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logger.warning("时区转换失败 %s/%s: %s", local_str, tz_name, e)
        return None


def _ensure_table():
    """自建 benchmark_minute（幂等；与其它指数采集脚本共用同一张表）。"""
    ddl = """
    CREATE TABLE IF NOT EXISTS benchmark_minute (
        id          BIGSERIAL       PRIMARY KEY,
        bench_code  VARCHAR(20)     NOT NULL,
        bench_name  VARCHAR(50),
        ts          TIMESTAMPTZ     NOT NULL,
        mkt_time    TIMESTAMPTZ,
        open        NUMERIC(14,4),
        high        NUMERIC(14,4),
        low         NUMERIC(14,4),
        close       NUMERIC(14,4),
        source      VARCHAR(20),
        created_at  TIMESTAMPTZ     DEFAULT NOW(),
        UNIQUE (bench_code, ts)
    );
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)


def _last_ts(codes) -> Dict[str, Optional[datetime]]:
    """查各指数库内最新 ts（UTC，tz-aware），无数据为 None。"""
    out = {c: None for c in codes}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT bench_code, MAX(ts) FROM {TABLE} "
                    "WHERE bench_code = ANY(%s) GROUP BY bench_code",
                    (list(codes),),
                )
                for code, ts in cur.fetchall():
                    out[code] = ts
    except Exception as e:
        logger.warning("读取 %s 最新 ts 失败: %s", TABLE, e)
    return out


def _filter_incremental(records: list, last_map: Dict[str, Optional[datetime]]) -> list:
    """只保留 ts >= MAX(ts) - REWRITE_MINUTES 的记录（重写最后一根未定格的 bar）。

    ts 为 None（时区转换失败）的记录直接丢弃（表 ts 为 NOT NULL）。
    """
    keep = []
    for rec in records:
        ts = rec.get("ts")
        if ts is None:
            continue
        lt = last_map.get(rec["bench_code"])
        if lt is None or ts >= lt - timedelta(minutes=REWRITE_MINUTES):
            keep.append(rec)
    return keep


def _save(records: list) -> int:
    if not records:
        return 0
    _ensure_table()
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, records, conflict_cols=["bench_code", "ts"])
    return len(records)


# ── 东财采集（日经 / KOSPI）──
def _collect_eastmoney(items: list) -> list:
    """东财 push2 分钟 k 线：单 secid 一次；短重试（2 次 / 退避 2s / 超时 8s）。"""
    import requests as req

    records = []
    today = date.today()
    for it in items:
        secid = it.get("em_secid")
        if not secid:
            continue
        last_err = None
        for attempt in range(2):
            try:
                if attempt > 0:
                    _t.sleep(2)
                r = req.get(_EM_URL, params={**_EM_BASE, "secid": secid},
                            headers=_EM_HEADERS, timeout=8)
                kls = (r.json().get("data") or {}).get("klines")
                if not kls:
                    last_err = "无数据"
                    continue
                kept = 0
                for line in kls:
                    row = line.split(",")
                    mkt = row[0].strip()  # 东财分钟 k 线时间格式 "YYYY-MM-DD HH:MM"
                    if len(mkt) >= 19:
                        mkt_full = mkt[:19]
                    elif len(mkt) >= 16:
                        mkt_full = mkt[:16] + ":00"
                    else:
                        continue
                    try:
                        if datetime.strptime(mkt_full, "%Y-%m-%d %H:%M:%S").date() != today:
                            continue  # 只取当天（调度只关心当日增量）
                    except Exception:
                        continue
                    utc = _to_utc(mkt_full, it["tz"])
                    if utc is None:
                        continue
                    # 东财字段: f51时间 f52开 f53收 f54高 f55低
                    records.append({
                        "bench_code": it["code"], "bench_name": it["name"],
                        "ts": utc, "mkt_time": mkt_full,
                        "open": _r(row[1]), "high": _r(row[3]),
                        "low": _r(row[4]), "close": _r(row[2]),
                        "source": "eastmoney",
                    })
                    kept += 1
                print(f"  [东财] {it['name']} {kept}根")
                last_err = None
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        if last_err:
            print(f"  [东财] ❌ {it['name']}({secid}) {last_err}（交下批重试）")
    return records


# ── 调度入口 ──
def run(codes=None, ctx=None):
    """采集入口（调度常驻调用）。codes/ctx 未使用：固定采集 2 个国际指数的当日增量。"""
    records = _collect_eastmoney(INTL_BENCHMARKS)
    last = _last_ts([b["code"] for b in INTL_BENCHMARKS])
    recs = _filter_incremental(records, last)
    saved = _save(recs)
    print(f"  [国际] 采集 {len(records)} 条 → 写入 {saved} 条")
    return saved


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run()
