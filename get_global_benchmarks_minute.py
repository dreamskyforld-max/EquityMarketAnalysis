#!/usr/bin/env python3
"""
全球基准指数分钟级采集 — 独立脚本（不改动现有 get_global_benchmarks.py）。

数据源:
  - 富途 OpenAPI K_1M:     恒生指数 / 恒生科技
  - 腾讯分钟接口:           上证指数 / 深证成指
  - 东方财富全球指数分钟:    日经225 / KOSPI  （2026-09-12 起替代 yfinance：雅虎自 2026-08-21 起持续 429 限流，yfinance 不可靠）
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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    # 东方财富全球指数分钟 k 线（日经 / KOSPI）—— 替代 yfinance（雅虎 2026-08-21 起持续 429 限流，不可靠）
    {"code": "JP.N225",   "name": "日经225指数",   "source": "eastmoney", "em_secid": "100.N225", "tz": "Asia/Tokyo"},
    {"code": "KR.KS11",   "name": "韩国KOSPI指数", "source": "eastmoney", "em_secid": "100.KS11", "tz": "Asia/Seoul"},
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
    from futu import RET_OK, KLType, AuType
    from collector_runtime import get_shared_ctx

    ktype = {"1": KLType.K_1M, "5": KLType.K_5M, "15": KLType.K_15M,
             "60": KLType.K_60M}.get(klt, KLType.K_1M)
    ctx = get_shared_ctx()  # 复用常驻进程共享上下文（不关闭）
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
        pass  # 共享 ctx 由进程持有，此处不关闭
    return records


# ── 东财分钟采集 ──
def _collect_eastmoney(items: list, klt: str = "1", days: int = 1) -> list:
    import requests as req
    import time as _t

    # 注：日线版 get_global_benchmarks.py 实测 push2his 在当前网络不可达，
    # 改用 push2.eastmoney.com（同款 kline 接口）稳定可用。
    url = "https://push2.eastmoney.com/api/qt/stock/kline/get"
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
                    mkt = row[0].strip()  # 东财 1 分钟 k 线时间格式为 "YYYY-MM-DD HH:MM"（无秒）
                    # 统一补齐秒为 "YYYY-MM-DD HH:MM:SS"（与其他源 time_key 格式一致）
                    if len(mkt) >= 19:
                        mkt_full = mkt[:19]
                    elif len(mkt) >= 16:
                        mkt_full = mkt[:16] + ":00"
                    else:
                        continue
                    try:
                        dt = datetime.strptime(mkt_full, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    if (date.today() - dt.date()).days > days:
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
            inner = node.get("data", {}) if isinstance(node, dict) else {}
            minute = inner.get("data") if isinstance(inner, dict) else None
            if not minute:
                print(f"  [腾讯] ⚠️ {it['name']} 无分钟数据")
                continue
            # 校验归属日期：腾讯盘中返回当天序列；开盘前(8:00-9:25)会返回
            # 上一交易日全天，若误拼当天日期会把旧数据写成"今天"，故非当天丢弃。
            date_str = inner.get("date") if isinstance(inner, dict) else None
            if not date_str:
                print(f"  [腾讯] ⚠️ {it['name']} 返回无日期字段，跳过")
                continue
            try:
                bar_date = datetime.strptime(str(date_str), "%Y%m%d").date()
            except Exception:
                print(f"  [腾讯] ⚠️ {it['name']} 日期解析失败: {date_str}，跳过")
                continue
            if bar_date != date.today():
                print(f"  [腾讯] ⚠️ {it['name']} 数据归属 {bar_date}（非今天），跳过避免误写")
                continue
            today = bar_date
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
                # 用归属日期(==今天)拼本地时间
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
# 取当日分钟序列（period="1d"，yfinance 新版已不支持 "1h"），upsert 按 (bench_code,ts) 去重。
# 说明：yfinance 对高频请求会返回 YFRateLimitError(429)，故退避大幅拉长，
# 且 collect_all 已限制 yfinance 仅在整点/半点批次采集（每 30 分钟 1 次），降低限流概率。
def _collect_yfinance_one(it: dict, klt: str = "1") -> list:
    import time as _t
    import yfinance as yf

    ticker = it.get("yf_ticker")
    if not ticker:
        return []
    records = []
    last_err = None
    # 重试策略：仅对非限流类瞬时错误重试(30s/60s 退避)。
    # 429(YFRateLimitError) 是 IP 级限流，短退避救不回；继续重试只会
    # 增加请求量、加剧限流 → 遇 429 立即放弃本批，交给下个整点/半点批次
    # (每 30 分钟 1 次)自然恢复。yfinance period="1d" 一旦成功即回补全天，
    # 单批成功即可补全当天已走过的全部分钟。
    _backoff = [0, 30, 60]
    for attempt in range(3):
        try:
            if attempt > 0:
                _t.sleep(_backoff[min(attempt, len(_backoff) - 1)])
            t = yf.Ticker(ticker)
            # 注：yfinance 新版已移除 period="1h"，最小有效单位为 "1d"。
            # 用 "1d" 取当日分钟序列（盘中到当前时刻 / 收盘后到全天），
            # 由调用方 days=1 控制只取当日；upsert 按 (bench_code,ts) 去重。
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
            # 识别限流类异常，明确提示（便于从日志区分"真无数据"与"被限流"）
            err_name = type(e).__name__
            is_rate = "rate" in err_name.lower() or "429" in str(e).lower()
            if is_rate:
                print(f"  [yfinance] ⚠️ {it['name']}({ticker}) 限流({err_name})，"
                      f"放弃本批重试，交下个整点/半点批次")
                break  # 429: 立即放弃本批，不再发请求加重限流
            last_err = f"{err_name}: {e}"
            _t.sleep(0.5)
    else:
        print(f"  [yfinance] ❌ {it['name']}({ticker}) {last_err}")
    return records


def _collect_yfinance(items: list, klt: str = "1", days: int = 1) -> list:
    """并行拉取多个 yfinance ticker（各 ticker 独立线程），降低总耗时。"""
    try:
        import importlib
        importlib.import_module("yfinance")  # 提前确认库已安装
    except Exception as e:
        print(f"  [yfinance] ❌ 库未安装: {e}")
        return []
    records = []
    if not items:
        return records
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        futs = {ex.submit(_collect_yfinance_one, it, klt): it for it in items}
        for fut in as_completed(futs):
            try:
                records.extend(fut.result())
            except Exception as e:
                it = futs[fut]
                print(f"  [yfinance] ❌ {it['name']} 异常: {type(e).__name__}: {e}")
    return records


# ── 调度入口 ──
def collect_all(sources: Optional[set] = None, klt: str = "1", days: int = 1,
                yf_hourly: bool = True):
    """yf_hourly=True 时，yfinance(日经/KOSPI)仅在整点或半点批次采集（每 30 分钟 1 次），
    降低 yfinance 429 限流概率；其余源(富途/腾讯)保持原频率（每 5 分钟）。
    显式传 sources={'yfinance'} 时忽略此限制（手动补采不受限）。"""
    by_source = {}
    for b in MINUTE_BENCHMARKS:
        if sources and b["source"] not in sources:
            continue
        by_source.setdefault(b["source"], []).append(b)

    # 降频：非整点/半点且未显式指定 yfinance 源时，跳过 yfinance
    if yf_hourly and not (sources and "yfinance" in sources):
        now_min = datetime.now().minute
        if now_min not in (0, 30):
            by_source.pop("yfinance", None)
            print("  [调度] 非整点/半点批次，跳过 yfinance（每 30 分钟才采日经/KOSPI）")

    collectors = {
        "futu": lambda its: _collect_futu(its, klt, days),
        "eastmoney": lambda its: _collect_eastmoney(its, klt, days),
        "tencent_a": lambda its: _collect_tencent_a(its, klt, days),
        "yfinance": lambda its: _collect_yfinance(its, klt, days),
    }

    _ensure_table()
    all_rec = []

    # 各数据源互不依赖，并行执行（数据源头总耗时 ≈ max(各源耗时)，
    # 避免 yfinance 退避叠加在串行链路上逼近 scheduler timeout）。
    with ThreadPoolExecutor(max_workers=max(len(by_source), 1)) as ex:
        fut_map = {}
        for src, items in by_source.items():
            fn = collectors.get(src)
            if not fn:
                continue
            fut_map[ex.submit(fn, items)] = src
        for fut in as_completed(fut_map):
            src = fut_map[fut]
            try:
                recs = fut.result()
                all_rec.extend(recs)
                print(f"  [{src}] 采集 {len(recs)} 条")
            except Exception as e:
                print(f"  [{src}] ❌ 采集异常: {type(e).__name__}: {e}")

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
