#!/usr/bin/env python3
"""
指数分钟采集 · 快源（恒生指数 / 恒生科技指数 / 上证指数 / 深证成指）

调度：market_scheduler「指数分钟-快源」，每 1 分钟、仅交易日 9-11 / 13-16 时。
数据源：
  - 富途 OpenAPI K_1M：HK.800000 恒生指数 / HK.800700 恒生科技指数（只拉最近 15 分钟窗口）
  - 腾讯分钟接口：     SH.000001 上证指数 / SZ.399001 深证成指（接口返回当天全部分钟，本地按增量过滤）
落表：benchmark_minute（UNIQUE(bench_code, ts)，bulk_upsert 单事务写入）

设计要点：
  - 富途 / 腾讯均不支持「一次调用多代码」（实测：富途逗号串报未知股票、list 报类型错误；
    腾讯多 code 报 code param error）→ 按指数循环，每批共 4 次请求，正常单批 < 5s。
  - 增量写入：只写「库内 MAX(ts) 回退 2 分钟」之后的 bar。回退 2 分钟用于重写最后
    一根尚未定格的分钟 bar（分钟走完才最终确定），既省 IO 又不丢修正。
  - 腾讯开盘前会返回上一交易日全天序列 → 用返回体 date 字段校验归属日，非当天丢弃，
    避免把旧数据拼成"今天"。
  - ts 统一转 UTC 存储；mkt_time 保留市场本地时间便于核对。

手动补采：历史回溯请用 get_global_benchmarks_minute.py（保留的 CLI 全量版）。
    python3 get_index_minute_fast.py     # 单次采集（当日增量）
"""
import logging
from datetime import datetime, timedelta, date, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from db import get_conn, bulk_upsert

logger = logging.getLogger(__name__)

# ── 指数定义（快源）──
HK_TZ = "Asia/Hong_Kong"
FAST_BENCHMARKS: List[Dict] = [
    {"code": "HK.800000", "name": "恒生指数",     "source": "futu",      "tz": HK_TZ},
    {"code": "HK.800700", "name": "恒生科技指数", "source": "futu",      "tz": HK_TZ},
    {"code": "SH.000001", "name": "上证指数",     "source": "tencent_a", "tz": HK_TZ},
    {"code": "SZ.399001", "name": "深证成指",     "source": "tencent_a", "tz": HK_TZ},
]

TABLE = "benchmark_minute"
REWRITE_MINUTES = 2    # 增量写库回退窗口（分钟）：重写最后一根未定格的 bar
FUTU_WINDOW_MIN = 15   # 富途每批拉取窗口（分钟）

_TENCENT_CODE_MAP = {"SH.000001": "sh000001", "SZ.399001": "sz399001"}


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


# ── 富途采集（恒指 / 恒生科技）──
def _collect_futu(items: list, ctx, window_min: int = FUTU_WINDOW_MIN) -> list:
    """拉最近 window_min 分钟的 1 分钟 K 线。ctx 为进程级共享上下文（常驻复用）。"""
    from futu import RET_OK, KLType, AuType

    now = datetime.now()
    start = (now - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    end = (now + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for it in items:
        try:
            ret, kl, _ = ctx.request_history_kline(
                it["code"], ktype=KLType.K_1M, autype=AuType.NONE,
                start=start, end=end, max_count=200)
            if ret != RET_OK:
                print(f"  [富途] ⚠️ {it['name']} {str(kl)[:80]}")
                continue
            if len(kl) == 0:
                continue  # 非交易时段（如开盘前），静默跳过
            for _, row in kl.iterrows():
                tk = str(row["time_key"])
                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "ts": _to_utc(tk, it["tz"]), "mkt_time": tk,
                    "open": _r(row["open"]), "high": _r(row["high"]),
                    "low": _r(row["low"]), "close": _r(row["close"]),
                    "source": "futu",
                })
            print(f"  [富途] {it['name']} {len(kl)}根")
        except Exception as e:
            print(f"  [富途] ❌ {it['name']} {type(e).__name__}: {e}")
    return records


# ── 腾讯采集（上证 / 深证成指）──
def _collect_tencent(items: list) -> list:
    """腾讯分钟接口：返回当天全部分钟序列（增量过滤在 run 内统一做）。"""
    import requests as req

    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gu.qq.com/",
    }
    records = []
    for it in items:
        qcode = _TENCENT_CODE_MAP.get(it["code"])
        if not qcode:
            continue
        try:
            r = req.get(url, params={"code": qcode}, headers=headers, timeout=10)
            data = r.json()
            node = (data.get("data", {}) or {}).get(qcode, {})
            inner = node.get("data", {}) if isinstance(node, dict) else {}
            minute = inner.get("data") if isinstance(inner, dict) else None
            date_str = inner.get("date") if isinstance(inner, dict) else None
            if not minute or not date_str:
                continue
            try:
                bar_date = datetime.strptime(str(date_str), "%Y%m%d").date()
            except Exception:
                continue
            if bar_date != date.today():
                continue  # 开盘前返回上一交易日全天，丢弃避免误写
            kept = 0
            for line in minute:
                parts = line.split()
                if len(parts) < 2:
                    continue
                hm, price = parts[0], _r(parts[1])
                if price is None:
                    continue
                mkt = f"{bar_date} {hm[:2]}:{hm[2:]}:00"
                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "ts": _to_utc(mkt, it["tz"]), "mkt_time": mkt,
                    "open": price, "high": price, "low": price, "close": price,
                    "source": "tencent_a",
                })
                kept += 1
            print(f"  [腾讯] {it['name']} {kept}根")
        except Exception as e:
            print(f"  [腾讯] ❌ {it['name']} {type(e).__name__}: {e}")
    return records


# ── 调度入口 ──
def run(codes=None, ctx=None):
    """采集入口（调度常驻调用）。codes 未使用：固定采集 4 个指数的当日增量。"""
    if ctx is None:
        from collector_runtime import get_shared_ctx
        ctx = get_shared_ctx()

    futu_items = [b for b in FAST_BENCHMARKS if b["source"] == "futu"]
    tx_items = [b for b in FAST_BENCHMARKS if b["source"] == "tencent_a"]

    records = _collect_futu(futu_items, ctx)
    records += _collect_tencent(tx_items)

    last = _last_ts([b["code"] for b in FAST_BENCHMARKS])
    recs = _filter_incremental(records, last)
    saved = _save(recs)
    print(f"  [快源] 采集 {len(records)} 条 → 写入 {saved} 条")
    return saved


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    from collector_runtime import get_shared_ctx, close_shared_ctx
    try:
        run(ctx=get_shared_ctx())
    finally:
        close_shared_ctx()  # CLI 独立运行收尾（常驻进程勿调用）
