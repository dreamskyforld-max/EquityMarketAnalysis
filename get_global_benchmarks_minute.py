#!/usr/bin/env python3
"""
全球基准指数分钟级采集 — 独立脚本（不改动现有 get_global_benchmarks.py）。

数据源:
  - 富途 OpenAPI K_1M:     恒生指数 / 恒生科技
  - 腾讯分钟接口:           上证指数 / 深证成指
  - yfinance:              日经225 / KOSPI
  - 东财直调 klt=1:        DAX / 道琼斯 / 纳斯达克 / 标普500

说明:
  - 美债收益率 / 离岸人民币 / VIX / 美元指数 无公开分钟源，不采集。
  - ts 统一转 UTC 存储；mkt_time 保留市场本地时间，便于核对。
  - 自包含建表 benchmark_minute（首次运行自动创建，无需改 schema.sql）。
  - 东财有反爬限流，已内置重试 + 递增退避 + 慢速。

用法:
  python3 get_global_benchmarks_minute.py                  # 全采(默认1分钟, 近1天)
  python3 get_global_benchmarks_minute.py --klt 5          # 5分钟
  python3 get_global_benchmarks_minute.py --days 5         # 近5天
  python3 get_global_benchmarks_minute.py --source futu    # 只采富途
  python3 get_global_benchmarks_minute.py --source eastmoney
"""
import os
import sys
import logging
from datetime import datetime, timedelta, date, timezone
from typing import List, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── 自动加载 .env ──
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if not os.environ.get(k):
                os.environ[k] = v


_load_dotenv()


# ── 指数定义（仅能采到分钟级的）──
# code: 统一代码(市场.代码)  source: futu/tencent_a/yfinance/eastmoney
# tz: 市场时区(用于转UTC)  yf_ticker: yfinance ticker  em_secid: 东财 secid
MINUTE_BENCHMARKS: List[Dict] = [
    # 富途（港股）
    {"code": "HK.800000", "name": "恒生指数",     "source": "futu",     "tz": "Asia/Hong_Kong"},
    {"code": "HK.800700", "name": "恒生科技指数", "source": "futu",     "tz": "Asia/Hong_Kong"},
    # 腾讯分钟接口（A股指数）
    {"code": "SH.000001", "name": "上证指数",     "source": "tencent_a", "tz": "Asia/Hong_Kong"},
    {"code": "SZ.399001", "name": "深证成指",     "source": "tencent_a", "tz": "Asia/Hong_Kong"},
    # yfinance（日经 / KOSPI）
    {"code": "JP.N225",   "name": "日经225指数",   "source": "yfinance", "yf_ticker": "^N225", "tz": "Asia/Tokyo"},
    {"code": "KR.KS11",   "name": "韩国KOSPI指数", "source": "yfinance", "yf_ticker": "^KS11", "tz": "Asia/Seoul"},
    # 东财全球指数 — 反爬拦截严重，暂不采集
    # {"code": "DE.GDAXI",  "name": "德国DAX指数",   "source": "eastmoney", "em_secid": "100.GDAXI",  "tz": "Europe/Berlin"},
    # {"code": "US.DJIA",   "name": "道琼斯工业指数", "source": "eastmoney", "em_secid": "100.DJIA",  "tz": "America/New_York"},
    # {"code": "US.NDX",    "name": "纳斯达克综合指数", "source": "eastmoney", "em_secid": "100.NDX", "tz": "America/New_York"},
    # {"code": "US.SPX",    "name": "标普500指数",   "source": "eastmoney", "em_secid": "100.SPX",   "tz": "America/New_York"},
]


# ── 工具 ──
def _r(v, nd: int = 4):
    try:
        return round(float(v), nd) if v is not None else None
    except Exception:
        return None


def _to_utc(local_str: str, tz_name: str):
    """把 'YYYY-MM-DD HH:MM:SS' 本地时间转 UTC；无 zoneinfo 时返回 None"""
    if ZoneInfo is None:
        return None
    try:
        dt = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(tz_name))
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logger.warning("时区转换失败 %s/%s: %s", local_str, tz_name, e)
        return None


def _ensure_table():
    """自建 benchmark_minute 表（幂等）"""
    from db import get_conn
    ddl = """
    CREATE TABLE IF NOT EXISTS benchmark_minute (
        id          BIGSERIAL       PRIMARY KEY,
        bench_code  VARCHAR(20)     NOT NULL,
        bench_name  VARCHAR(50),
        ts          TIMESTAMPTZ     NOT NULL,   -- 已转 UTC
        mkt_time    TIMESTAMPTZ,                -- 市场本地时间(便于核对)
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
        conn.commit()


def _save(records: list) -> int:
    if not records:
        return 0
    from db import get_conn, upsert
    saved = 0
    for rec in records:
        try:
            with get_conn() as conn:
                upsert(conn, "benchmark_minute", rec, conflict_cols=["bench_code", "ts"])
            saved += 1
        except Exception as e:
            logger.warning("写入失败 %s: %s", rec.get("bench_code"), e)
    return saved


# ── 富途分钟采集 ──
def _collect_futu(items: list, klt: str = "1", days: int = 1) -> list:
    from futu import OpenQuoteContext, RET_OK, KLType, AuType

    ktype = {"1": KLType.K_1M, "5": KLType.K_5M, "15": KLType.K_15M,
             "60": KLType.K_60M}.get(klt, KLType.K_1M)
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    records = []
    try:
        end_day = date.today()
        for d in range(days):
            day = end_day - timedelta(days=d)
            start_str = f"{day} 00:00:00"
            end_str = f"{(day + timedelta(days=1))} 00:00:00"
            for it in items:
                try:
                    ret, kl, _ = ctx.request_history_kline(
                        it["code"], ktype=ktype, autype=AuType.NONE,
                        start=start_str, end=end_str, max_count=2000)
                    if ret != RET_OK or len(kl) == 0:
                        print(f"  [富途] ⚠️ {it['name']} {day} 无数据")
                        continue
                    for _, row in kl.iterrows():
                        tk = str(row['time_key'])
                        utc = _to_utc(tk, it["tz"])
                        records.append({
                            "bench_code": it["code"], "bench_name": it["name"],
                            "ts": utc, "mkt_time": tk,
                            "open": _r(row['open']), "high": _r(row['high']),
                            "low": _r(row['low']), "close": _r(row['close']),
                            "source": "futu",
                        })
                    print(f"  [富途] ✅ {it['name']} {day} {len(kl)}根")
                except Exception as e:
                    print(f"  [富途] ❌ {it['name']} {day} {type(e).__name__}: {e}")
    finally:
        ctx.close()
    return records


# ── 东财分钟采集 ──
def _collect_eastmoney(items: list, klt: str = "1", days: int = 1) -> list:
    import requests as req
    import time as _t

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    base = {
        "klt": klt, "fqt": "1", "lmt": str(min(days * 400, 50000)),
        "end": "20500000", "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "f057cbcbce2a86e2866ab8877db1d059", "forcect": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    records = []
    _t.sleep(2)  # 反爬缓冲
    for it in items:
        secid = it.get("em_secid")
        if not secid:
            continue
        last_err = None
        for attempt in range(4):
            try:
                if attempt > 0:
                    _t.sleep(4 * attempt)  # 递增退避 4s / 8s / 12s
                r = req.get(url, params={**base, "secid": secid}, headers=headers, timeout=20)
                data = r.json()
                kls = data.get("data", {}).get("klines")
                if not kls:
                    last_err = "无数据"
                    continue
                kept = 0
                for line in kls:
                    row = line.split(",")
                    mkt = row[0]  # 市场本地时间
                    dt = datetime.strptime(mkt[:16], "%Y-%m-%d %H:%M:%S")
                    if (date.today() - dt.date()).days > days:
                        continue
                    utc = _to_utc(mkt[:16], it["tz"])
                    # 东财字段: f51时间 f52开 f53收 f54高 f55低
                    records.append({
                        "bench_code": it["code"], "bench_name": it["name"],
                        "ts": utc, "mkt_time": mkt[:16],
                        "open": _r(row[1]), "high": _r(row[3]),
                        "low": _r(row[4]), "close": _r(row[2]),
                        "source": "eastmoney",
                    })
                    kept += 1
                print(f"  [东财] ✅ {it['name']}({secid}) {kept}根")
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                _t.sleep(0.5)
        else:
            print(f"  [东财] ❌ {it['name']}({secid}) {last_err}")
        _t.sleep(1.5)
    return records


# ── 腾讯分钟采集（A股指数）──
def _collect_tencent_a(items: list, klt: str = "1", days: int = 1) -> list:
    import requests as req
    import time as _t

    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://gu.qq.com/",
    }
    code_map = {
        "SH.000001": "sh000001",
        "SZ.399001": "sz399001",
    }
    records = []
    for it in items:
        qcode = code_map.get(it["code"])
        if not qcode:
            continue
        try:
            r = req.get(url, params={"code": qcode}, headers=headers, timeout=20)
            data = r.json()
            node = (data.get("data", {}) or {}).get(qcode, {})
            minute = node.get("data", {}).get("data") if isinstance(node, dict) else None
            if not minute:
                print(f"  [腾讯] ⚠️ {it['name']} 无分钟数据")
                continue
            kept = 0
            for line in minute:
                # 腾讯格式: "HHMM  price  avg_price  volume  "
                parts = line.split()
                if len(parts) < 2:
                    continue
                hm = parts[0]
                price = _r(parts[1])
                if price is None:
                    continue
                # 拼成当日本地时间
                today = date.today()
                dt = datetime.strptime(f"{today} {hm[:2]}:{hm[2:]}", "%Y-%m-%d %H:%M")
                utc = _to_utc(dt.strftime("%Y-%m-%d %H:%M:%S"), it["tz"])
                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "ts": utc, "mkt_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": price, "high": price, "low": price, "close": price,
                    "source": "tencent_a",
                })
                kept += 1
            print(f"  [腾讯] ✅ {it['name']} {kept}根")
        except Exception as e:
            print(f"  [腾讯] ❌ {it['name']} {type(e).__name__}: {e}")
        _t.sleep(0.5)
    return records


# ── yfinance 分钟采集（日经 / KOSPI）──
def _collect_yfinance(items: list, klt: str = "1", days: int = 1) -> list:
    import time as _t

    try:
        import yfinance as yf
    except Exception as e:
        print(f"  [yfinance] ❌ 库未安装: {e}")
        return []

    records = []
    for it in items:
        ticker = it.get("yf_ticker")
        if not ticker:
            continue
        last_err = None
        for attempt in range(3):  # 3 次重试，应对 429 限流
            try:
                if attempt > 0:
                    _t.sleep(10 * attempt)  # 10s / 20s 递增退避
                t = yf.Ticker(ticker)
                df = t.history(period="1d", interval="1m", auto_adjust=False)
                if df is None or len(df) == 0:
                    last_err = "无数据"
                    continue
                kept = 0
                for idx, row in df.iterrows():
                    # yfinance 索引为 tz-aware (市场时区)
                    local = idx.tz_convert(it["tz"]) if idx.tzinfo else idx
                    utc = idx.astimezone(timezone.utc) if idx.tzinfo else _to_utc(
                        local.strftime("%Y-%m-%d %H:%M:%S"), it["tz"])
                    records.append({
                        "bench_code": it["code"], "bench_name": it["name"],
                        "ts": utc, "mkt_time": local.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": _r(row.get("Open")), "high": _r(row.get("High")),
                        "low": _r(row.get("Low")), "close": _r(row.get("Close")),
                        "source": "yfinance",
                    })
                    kept += 1
                print(f"  [yfinance] ✅ {it['name']}({ticker}) {kept}根")
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                _t.sleep(0.5)
        else:
            print(f"  [yfinance] ❌ {it['name']}({ticker}) {last_err}")
        _t.sleep(0.5)
    return records


# ── 调度入口 ──
def collect_all(sources: Optional[set] = None, klt: str = "1", days: int = 1):
    by_source = {}
    for b in MINUTE_BENCHMARKS:
        if sources and b["source"] not in sources:
            continue
        by_source.setdefault(b["source"], []).append(b)

    collectors = {
        "futu": lambda its: _collect_futu(its, klt, days),
        "eastmoney": lambda its: _collect_eastmoney(its, klt, days),
        "tencent_a": lambda its: _collect_tencent_a(its, klt, days),
        "yfinance": lambda its: _collect_yfinance(its, klt, days),
    }

    _ensure_table()
    all_rec = []
    for src, items in by_source.items():
        fn = collectors.get(src)
        if not fn:
            print(f"  ❌ 未知数据源: {src}")
            continue
        print(f"\n── {src.upper()} ({len(items)}个) ──")
        all_rec.extend(fn(items))

    saved = _save(all_rec)
    print(f"\n总计: 采集 {len(all_rec)} 条, 写入 {saved} 条")
    return saved, len(all_rec)


# ── 常驻调用入口（供 market_scheduler.run_module 调用）──
def run(codes: Optional[List[str]] = None, ctx=None, source: Optional[str] = None):
    """分钟级全球基准指数采集入口。

    codes 未使用（全量采集）；source 可指定数据源子集如 {'futu'}；
    klt/days 默认 1 分钟 / 近 1 天（调度场景只需当日增量，无需回溯）。
    """
    s = {x.strip() for x in source.split(",")} if source else None
    return collect_all(s, klt="1", days=1)


# ── CLI ──
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="全球基准指数分钟级采集")
    ap.add_argument("--source", default=None, help="futu / tencent_a / yfinance / eastmoney")
    ap.add_argument("--klt", default="1", help="1/5/15/60 分钟")
    ap.add_argument("--days", type=int, default=1, help="回溯天数")
    ns = ap.parse_args()
    src = {s.strip() for s in ns.source.split(",")} if ns.source else None
    collect_all(src, ns.klt, ns.days)
