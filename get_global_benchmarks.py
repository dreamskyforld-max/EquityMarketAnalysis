#!/usr/bin/env python3
"""
全球基准指数采集 — 多数据源统一采集到 daily_benchmark 表。

数据源:
  - 富途 OpenAPI:       恒生 / 恒生科技 / 上证
  - FRED (美联储):      标普500 / 道琼斯 / 纳斯达克 / VIX / 美元指数
  - AKShare:           美债10Y / 美债2Y / 离岸人民币
  - 待定 (yfinance):    日经225 / KOSPI / DAX

前置: export FRED_API_KEY=xxx
用法:
  python3 get_global_benchmarks.py                  # 采集全部
  python3 get_global_benchmarks.py --source futu     # 只采富途
  python3 get_global_benchmarks.py --source fred,akshare
"""
import os
import sys
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 自动加载 .env ──
def _load_dotenv():
    """手动加载 .env 文件（无需 python-dotenv 依赖）"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if not os.environ.get(key):  # 优先保留已设的环境变量
                os.environ[key] = val
_load_dotenv()

# ── 指数定义 ──
# 格式: (bench_code, bench_name, source, note)
# bench_code 规则: 市场.代码  (与现有 daily_benchmark 富途格式 "HK.800000" 对齐)
# change_pct 含义: 指数=涨跌幅%, 利率(bp)=收益率变化×100(基点)

ALL_BENCHMARKS: List[Dict] = [
    # ── 富途 OpenAPI ──
    {"code": "HK.800000",  "name": "恒生指数",               "source": "futu"},
    {"code": "HK.800700",  "name": "恒生科技指数",           "source": "futu"},
    {"code": "SH.000001",  "name": "上证指数",               "source": "futu"},
    # ── FRED 美联储 ──
    {"code": "US.SP500",       "name": "标普500指数",        "source": "fred", "fred_ticker": "SP500"},
    {"code": "US.DJIA",        "name": "道琼斯工业指数",     "source": "fred", "fred_ticker": "DJIA"},
    {"code": "US.NASDAQCOM",   "name": "纳斯达克综合指数",   "source": "fred", "fred_ticker": "NASDAQCOM"},
    {"code": "US.VIXCLS",      "name": "VIX恐慌指数",        "source": "fred", "fred_ticker": "VIXCLS"},
    {"code": "US.DTWEXBGS",    "name": "美元指数(贸易加权)", "source": "fred", "fred_ticker": "DTWEXBGS"},
    # ── AKShare ──
    {"code": "US.DGS10",       "name": "美国10年期国债收益率", "source": "akshare", "ak_func": "bond"},
    {"code": "US.DGS2",        "name": "美国2年期国债收益率",  "source": "akshare", "ak_func": "bond"},
    {"code": "FX.USDCNY",      "name": "离岸人民币(USD/CNY)", "source": "akshare", "ak_func": "currency"},
    # ── 东财全球指数（直调东方财富 API）──
    {"code": "JP.N225",       "name": "日经225指数",       "source": "eastmoney", "em_secid": "100.N225"},
    {"code": "KR.KS11",       "name": "韩国KOSPI指数",     "source": "eastmoney", "em_secid": "100.KS11"},
    {"code": "DE.GDAXI",      "name": "德国DAX指数",       "source": "eastmoney", "em_secid": "100.GDAXI"},
]


# ── 工具 ──
def _r(v, ndigits=4):
    """round 并处理 None"""
    return round(float(v), ndigits) if v is not None else None


def _save(records: list):
    """批量写入 daily_benchmark"""
    if not records:
        return 0
    from db import get_conn, upsert
    saved, skipped = 0, 0
    for rec in records:
        try:
            with get_conn() as conn:
                upsert(conn, "daily_benchmark", rec, conflict_cols=["bench_code", "trade_date"])
            saved += 1
        except Exception:
            skipped += 1
    return saved


# ── 富途采集 ──
def _collect_futu(items: list) -> list:
    """采集富途指数：snapshot + 20日历史K线"""
    from futu import RET_OK, KLType, AuType
    from collector_runtime import get_shared_ctx

    codes = [it["code"] for it in items]
    ctx = get_shared_ctx()  # 复用共享上下文（常驻进程内不关闭）
    records = []

    try:
        ret, snap = ctx.get_market_snapshot(codes)
        if ret != RET_OK:
            print(f"  [富途] ❌ 快照失败: {snap}")
            return []
        for i, it in enumerate(items):
            try:
                row = snap.iloc[i]
                close = float(row['last_price'])
                prev = float(row.get('prev_close_price', close) or close)
                change = (close / prev - 1) * 100 if prev != 0 else 0.0
                ut = row.get('update_time', 'N/A')
                td = date.fromisoformat(str(ut)[:10]) if ut and str(ut) != 'N/A' else date.today()

                # 20日前收盘
                ret_k, kl, _ = ctx.request_history_kline(
                    it["code"], ktype=KLType.K_DAY, autype=AuType.QFQ,
                    max_count=25, extended_time=False)
                close_20d = _r(float(kl.iloc[-21]['close'])) if ret_k == RET_OK and len(kl) >= 21 else None

                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "trade_date": td,
                    "update_time": ut if ut != 'N/A' else None,
                    "last_price": close, "prev_close": _r(prev),
                    "change_pct": _r(change), "close_20d_ago": close_20d,
                })
                print(f"  [富途] ✅ {it['name']}({it['code']})  {close:.2f}  ({change:+.2f}%)")
            except Exception as e:
                print(f"  [富途] ❌ {it['name']}({it['code']})  {type(e).__name__}: {e}")
    except Exception as e:
        print(f"  [富途] ❌ 批量快照异常: {e}")
    return records


# ── FRED 采集 ──
def _collect_fred(items: list) -> list:
    """采集 FRED 指数（需环境变量 FRED_API_KEY）"""
    import pandas_datareader as pdr

    from config import val
    api_key = os.environ.get("FRED_API_KEY") or val("fred", "api_key")
    if not api_key:
        print("  [FRED] ❌ 缺少环境变量 FRED_API_KEY")
        return []

    # 拉取日期范围：最近 80 个自然日
    end = date.today()
    start = end - timedelta(days=80)

    records = []
    for it in items:
        ticker = it.get("fred_ticker", it["code"].split(".")[-1])
        try:
            df = pdr.data.DataReader(ticker, "fred", start=start.isoformat(), end=end.isoformat())
            if len(df) < 2:
                print(f"  [FRED] ⚠️ {it['name']}({ticker})  数据不足 ({len(df)}行)")
                continue
            df = df.dropna()
            if len(df) < 2:
                print(f"  [FRED] ⚠️ {it['name']}({ticker})  去NaN后不足")
                continue

            last = df.iloc[-1]
            prev = df.iloc[-2]
            last_val = float(last.iloc[0])
            prev_val = float(prev.iloc[0])
            td = df.index[-1].date()
            change = (last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0

            # 20日前
            close_20d = _r(float(df.iloc[-21].iloc[0])) if len(df) >= 21 else None

            records.append({
                "bench_code": it["code"], "bench_name": it["name"],
                "trade_date": td,
                "update_time": datetime.combine(td, datetime.min.time()),
                "last_price": last_val, "prev_close": _r(prev_val),
                "change_pct": _r(change), "close_20d_ago": close_20d,
            })
            print(f"  [FRED] ✅ {it['name']}({ticker})  {last_val:.4f}  ({change:+.2f}%)  {td}")
        except Exception as e:
            print(f"  [FRED] ❌ {it['name']}({ticker})  {type(e).__name__}: {e}")
    return records


# ── AKShare 采集 ──
def _collect_akshare(items: list) -> list:
    """采集 AKShare 数据（债券/汇率）"""
    import akshare as ak
    import warnings; warnings.filterwarnings("ignore")

    records = []

    # 区分债券类和汇率类
    bond_items = [it for it in items if it.get("ak_func") == "bond"]
    fx_items = [it for it in items if it.get("ak_func") == "currency"]

    # ── 债券：bond_zh_us_rate（中美利差总表）──
    if bond_items:
        try:
            df = ak.bond_zh_us_rate()
            # 列: ['日期', '中国国债收益率2年',..., '美国国债收益率10年', '美国国债收益率2年', ...]
            bond_map = {"US.DGS10": "美国国债收益率10年", "US.DGS2": "美国国债收益率2年"}
            for it in bond_items:
                col = bond_map.get(it["code"])
                if col and col in df.columns:
                    sub = df[["日期", col]].dropna()
                    if len(sub) < 2:
                        print(f"  [AKShare] ⚠️ {it['name']} 数据不足")
                        continue
                    last = sub.iloc[-1]
                    prev = sub.iloc[-2]
                    td = date.fromisoformat(str(last['日期'])[:10])
                    last_val = float(last[col])
                    prev_val = float(prev[col])
                    # 收益率变化：存百分比（如 4.49→4.50 变化 0.01pp）
                    change = round(last_val - prev_val, 4)  # 直接差值
                    close_20d = _r(float(sub.iloc[-21][col])) if len(sub) >= 21 else None
                    records.append({
                        "bench_code": it["code"], "bench_name": it["name"],
                        "trade_date": td,
                        "update_time": datetime.combine(td, datetime.min.time()),
                        "last_price": _r(last_val), "prev_close": _r(prev_val),
                        "change_pct": _r(change), "close_20d_ago": close_20d,
                    })
                    print(f"  [AKShare] ✅ {it['name']}  {last_val:.4f}%  ({change:+.4f}pp)  {td}")
                else:
                    print(f"  [AKShare] ❌ {it['name']}  列 '{col}' 不存在")
        except Exception as e:
            print(f"  [AKShare] ❌ bond_zh_us_rate: {type(e).__name__}: {e}")

    # ── 汇率：currency_boc_safe ──
    if fx_items:
        try:
            df = ak.currency_boc_safe()
            # 列: ['日期', '美元', '欧元', ...] 美元值为 100美元兑人民币(分)，如 680.54 = 6.8054
            col = "美元"
            if col in df.columns:
                sub = df[["日期", col]].dropna()
                for it in fx_items:
                    if len(sub) < 2:
                        print(f"  [AKShare] ⚠️ {it['name']} 数据不足")
                        continue
                    last = sub.iloc[-1]
                    prev = sub.iloc[-2]
                    td = date.fromisoformat(str(last['日期'])[:10])
                    last_val = float(last[col]) / 100.0   # 分 → 元
                    prev_val = float(prev[col]) / 100.0
                    change = (last_val / prev_val - 1) * 100
                    close_20d = _r(float(sub.iloc[-21][col]) / 100.0) if len(sub) >= 21 else None
                    records.append({
                        "bench_code": it["code"], "bench_name": it["name"],
                        "trade_date": td,
                        "update_time": datetime.combine(td, datetime.min.time()),
                        "last_price": _r(last_val), "prev_close": _r(prev_val),
                        "change_pct": _r(change), "close_20d_ago": close_20d,
                    })
                    print(f"  [AKShare] ✅ {it['name']}  {last_val:.4f}  ({change:+.4f}%)  {td}")
            else:
                print(f"  [AKShare] ❌ currency 列 '{col}' 不存在")
        except Exception as e:
            print(f"  [AKShare] ❌ currency_boc_safe: {type(e).__name__}: {e}")

    return records


# ── 东财全球指数 ──
def _collect_eastmoney(items: list) -> list:
    """直调东方财富全球指数 API（无需 AKShare 封装）"""
    import requests as req
    import time as _time
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    base = {
        "klt": "101", "fqt": "1", "lmt": "50000", "end": "20500000",
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "f057cbcbce2a86e2866ab8877db1d059", "forcect": "1",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    records = []
    # 东财有轻度反爬，开头留缓冲时间
    _time.sleep(2)
    for it in items:
        secid = it.get("em_secid")
        if not secid:
            continue
        last_err = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    _time.sleep(4 * attempt)  # 重试间隔递增：4s, 8s
                r = req.get(url, params={**base, "secid": secid}, headers=headers, timeout=15)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                data = r.json()
                kls = data.get("data", {}).get("klines")
                if not kls or len(kls) < 2:
                    last_err = "数据不足"
                    continue
                rows = [l.split(",") for l in kls]
                last = rows[-1]; prev = rows[-2]
                td = last[0][:10]
                last_val = float(last[2]); prev_val = float(prev[2])
                change = (last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0
                c20 = float(rows[-21][2]) if len(rows) >= 21 else None
                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "trade_date": date.fromisoformat(td),
                    "update_time": datetime.combine(date.fromisoformat(td), datetime.min.time()),
                    "last_price": last_val, "prev_close": _r(prev_val),
                    "change_pct": _r(change), "close_20d_ago": _r(c20) if c20 else None,
                })
                print(f"  [东财] ✅ {it['name']}({secid})  {last_val:,.2f}  ({change:+.2f}%)  {td}")
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                _time.sleep(0.5)
        else:
            print(f"  [东财] ❌ {it['name']}({secid})  {last_err}")
    return records


# ── 调度入口 ──
def collect_all(sources: Optional[set] = None):
    """采集全部或指定数据源的指数。
    sources: None=全部, 或 {'futu','fred','akshare','yfinance'} 子集
    返回 (成功写入数, 总指数数)
    """
    # 按 source 分组
    by_source = {}
    for b in ALL_BENCHMARKS:
        s = b["source"]
        if sources and s not in sources:
            continue
        by_source.setdefault(s, []).append(b)

    collectors = {
        "futu":      _collect_futu,
        "fred":      _collect_fred,
        "akshare":   _collect_akshare,
        "eastmoney": _collect_eastmoney,
    }

    total_records = []
    for source, items in by_source.items():
        fn = collectors.get(source)
        if fn is None:
            print(f"  ❌ 未知数据源: {source}")
            continue
        print(f"\n── {source.upper()} ({len(items)}个) ──")
        recs = fn(items)
        total_records.extend(recs)

    saved = _save(total_records)
    print(f"\n总计: 采集 {len(total_records)} 条, 写入 {saved} 条, "
          f"跳过/失败 {len(total_records)-saved} 条")
    return saved, len(total_records)


# ── 常驻调用入口 ──
def run(codes=None, ctx=None, source=None):
    """采集入口（常驻调用）。codes 未使用（全量采集）；source 可指定数据源子集如 {'futu'}。"""
    collect_all(source)


# ── CLI ──
if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.startswith("--source="):
            src = arg.split("=", 1)[1]
            allowed = {s.strip() for s in src.split(",")}
        else:
            allowed = None
    else:
        allowed = None

    collect_all(allowed)
