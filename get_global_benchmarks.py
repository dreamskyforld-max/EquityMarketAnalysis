#!/usr/bin/env python3
"""
全球基准指数采集 — 多数据源统一采集到 daily_benchmark 表。

数据源:
  - 富途 OpenAPI:       恒生 / 恒生科技 / 上证
  - FRED (美联储):      标普500 / 道琼斯 / 纳斯达克 / VIX / 美元指数 / 有效联邦基金利率(EFFR)
  - AKShare:           美债10Y / 美债2Y / 离岸人民币
  - AKShare(新浪源):    日经225 / KOSPI / DAX  （东财接口不稳，改用新浪源）

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
    # ── A股指数（服务器端 futu 权限不足，改用 AKShare 新浪 A 股指数源）──
    # sina_code 为 stock_zh_index_daily 的带市场前缀代码（小写）
    {"code": "SH.000001",  "name": "上证指数",               "source": "sina_a", "sina_code": "sh000001"},
    {"code": "SZ.399001",  "name": "深证成指",               "source": "sina_a", "sina_code": "sz399001"},
    # ── FRED 美联储 ──
    {"code": "US.SP500",       "name": "标普500指数",        "source": "fred", "fred_ticker": "SP500"},
    {"code": "US.DJIA",        "name": "道琼斯工业指数",     "source": "fred", "fred_ticker": "DJIA"},
    {"code": "US.NASDAQCOM",   "name": "纳斯达克综合指数",   "source": "fred", "fred_ticker": "NASDAQCOM"},
    {"code": "US.VIXCLS",      "name": "VIX恐慌指数",        "source": "fred", "fred_ticker": "VIXCLS"},
    {"code": "US.DTWEXBGS",    "name": "美元指数(贸易加权)", "source": "fred", "fred_ticker": "DTWEXBGS"},
    {"code": "US.EFFR",         "name": "有效联邦基金利率",   "source": "fred", "fred_ticker": "EFFR", "is_rate": True},
    # ── Yahoo Finance（ICE DXY；大陆 IP 被风控需代理，服务器香港直连，见 _yahoo_proxy）──
    {"code": "US.DXY",         "name": "美元指数(ICE DXY)", "source": "yfinance", "yf_code": "DX-Y.NYB"},
    # ── AKShare ──
    {"code": "US.DGS10",       "name": "美国10年期国债收益率", "source": "akshare", "ak_func": "bond"},
    {"code": "US.DGS2",        "name": "美国2年期国债收益率",  "source": "akshare", "ak_func": "bond"},
    {"code": "FX.USDCNY",      "name": "离岸人民币(USD/CNY)", "source": "akshare", "ak_func": "currency"},
    # ── 国际指数（AKShare 新浪源，东财接口不稳故改用新浪）──
    # sina_name 为 AKShare index_global_hist_sina 的键（中文全名）
    {"code": "JP.N225",       "name": "日经225指数",       "source": "sina", "sina_name": "日经225指数"},
    {"code": "KR.KS11",       "name": "韩国KOSPI指数",     "source": "sina", "sina_name": "首尔综合指数"},
    {"code": "DE.GDAXI",      "name": "德国DAX指数",       "source": "sina", "sina_name": "德国DAX 30种股价指数"},
]


# ── 网络环境探测 ──
_SERVER_PATH = "/home/hermes-agent/hermes-skills"


def _is_server_env() -> bool:
    """是否部署在服务器（路径约定与 market_scheduler.py / wecom_server_collector.py 一致）。

    服务器在香港 → Yahoo 直连；本机(macOS 大陆 IP) 被 Yahoo 风控 → 需 socks5 代理。
    """
    return os.path.exists(_SERVER_PATH)


def _yahoo_proxy() -> Optional[str]:
    """Yahoo 数据源代理配置。

    优先级: 环境变量 YAHOO_PROXY（显式覆盖，空字符串=强制直连）> 环境探测。
    本机返回 socks5h://127.0.0.1:1080，服务器(香港)返回 None(直连)。
    """
    env = os.environ.get("YAHOO_PROXY")
    if env is not None:
        return env or None
    if _is_server_env():
        return None
    return "socks5h://127.0.0.1:1080"


# ── 工具 ──
def _r(v, ndigits=4):
    """round 并处理 None"""
    return round(float(v), ndigits) if v is not None else None


def _save(records: list):
    """批量写入 daily_benchmark（单连接 execute_values upsert）"""
    if not records:
        return 0
    from db import get_conn, bulk_upsert
    try:
        with get_conn() as conn:
            bulk_upsert(conn, "daily_benchmark", records, conflict_cols=["bench_code", "trade_date"])
        return len(records)
    except Exception as e:
        print(f"  ❌ 批量写入失败: {type(e).__name__}: {e}")
        return 0


# ── 富途采集 ──
def _collect_futu(items: list) -> list:
    """采集富途指数：snapshot（价格） + K线（20日前收盘 + 成交量/额）"""
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
        # 按 code 建索引，避免 snapshot 返回顺序与传入 codes 不一致导致错位
        snap_map = {str(r['code']): r for _, r in snap.iterrows()}
        for it in items:
            try:
                row = snap_map.get(it["code"])
                if row is None:
                    print(f"  [富途] ⚠️ {it['name']}({it['code']}) 快照未返回，跳过")
                    continue
                close = float(row['last_price'])
                prev = float(row.get('prev_close_price', close) or close)
                change = (close / prev - 1) * 100 if prev != 0 else 0.0
                ut = row.get('update_time', 'N/A')
                td = date.fromisoformat(str(ut)[:10]) if ut and str(ut) != 'N/A' else date.today()
                print(f"  [富途] {it['name']}({it['code']}) snapshot update_time={ut} → trade_date={td}")

                # 当日成交量/额（snapshot 自带）
                vol = int(row.get('volume', 0)) if row.get('volume') else None
                to = float(row.get('turnover', 0)) if row.get('turnover') else None

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
                    "volume": vol, "turnover": _r(to) if to else None,
                })
                print(f"  [富途] ✅ {it['name']}({it['code']})  {close:.2f}  ({change:+.2f}%)  "
                      f"量:{vol or '-'}")
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
            # 利率/收益率序列用基点(bp)，其余用相对涨跌幅%
            change = (last_val - prev_val) * 100 if it.get("is_rate") else \
                ((last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0)

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
    # 注意: push2his.eastmoney.com 在当前网络不可达，改用 push2.eastmoney.com（同款 kline 接口）
    url = "https://push2.eastmoney.com/api/qt/stock/kline/get"
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
                # 东财 klines: f51=日期 f52=开 f53=收 f54=高 f55=低 f56=量 f57=额 ...
                last_val = float(last[2]); prev_val = float(prev[2])
                change = (last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0
                c20 = float(rows[-21][2]) if len(rows) >= 21 else None
                vol = int(float(last[5])) if len(last) > 5 and last[5] and last[5] != '-' else None
                to = _r(float(last[6])) if len(last) > 6 and last[6] and last[6] != '-' else None
                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "trade_date": date.fromisoformat(td),
                    "update_time": datetime.combine(date.fromisoformat(td), datetime.min.time()),
                    "last_price": last_val, "prev_close": _r(prev_val),
                    "change_pct": _r(change), "close_20d_ago": _r(c20) if c20 else None,
                    "volume": vol, "turnover": to,
                })
                print(f"  [东财] ✅ {it['name']}({secid})  {last_val:,.2f}  ({change:+.2f}%)  {td}  "
                      f"量:{vol or '-'}")
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                _time.sleep(0.5)
        else:
            print(f"  [东财] ❌ {it['name']}({secid})  {last_err}")
    return records


# ── AKShare 新浪源国际指数 ──
def _collect_sina(items: list) -> list:
    """采集 AKShare 新浪源国际指数（日经225 / KOSPI / DAX）。

    东财 push2 接口对当前 IP 不稳定（RemoteDisconnected），改用 AKShare 的
    index_global_hist_sina（新浪源），稳定返回日线 OHLC。此处只取最新一天做增量写入，
    全历史回填请见 collect_benchmark_sina.py。
    """
    import akshare as ak
    import warnings; warnings.filterwarnings("ignore")

    # 接口不支持区间参数，拉全量后在本地截断到最近约 20 个交易日
    # 保留 22 行：last(当天) + prev(昨) + 20 日前(iloc[-21]) 需 21 行窗口，多留 1 行余量
    # 避免 close_20d_ago 取 iloc[-21] 时越界，同时避免拉取全历史（数千行）的浪费
    _KEEP = 22

    records = []
    for it in items:
        sina_name = it.get("sina_name")
        if not sina_name:
            print(f"  [新浪] ❌ {it['name']} 缺少 sina_name")
            continue
        try:
            df = ak.index_global_hist_sina(symbol=sina_name)
            if df is None or len(df) < 2:
                print(f"  [新浪] ⚠️ {it['name']}({sina_name}) 数据不足")
                continue
            df = df.dropna().tail(_KEEP)
            last = df.iloc[-1]
            prev = df.iloc[-2]
            td = last["date"]
            if not isinstance(td, date):
                td = date.fromisoformat(str(td)[:10])
            last_val = float(last["close"])
            prev_val = float(prev["close"])
            change = (last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0

            records.append({
                "bench_code": it["code"], "bench_name": it["name"],
                "trade_date": td,
                "update_time": datetime.combine(td, datetime.min.time()),
                "last_price": _r(last_val), "prev_close": _r(prev_val),
                "change_pct": _r(change),
                "close_20d_ago": _r(float(df.iloc[-21]["close"])) if len(df) >= 21 else None,
                "volume": int(last["volume"]) if "volume" in last.index and last["volume"] else None,
            })
            print(f"  [新浪] ✅ {it['name']}({sina_name})  {last_val:,.2f}  ({change:+.2f}%)  {td}  "
                  f"量:{last.get('volume', '-')}")
        except Exception as e:
            print(f"  [新浪] ❌ {it['name']}({sina_name})  {type(e).__name__}: {e}")
    return records


# ── AKShare 新浪源 A股指数（服务器端 futu 无 A股权限，改用此源）──
def _collect_sina_a(items: list) -> list:
    """采集 AKShare 新浪源 A股指数（上证 / 深证成指）。

    使用 stock_zh_index_daily（新浪源），返回日线 OHLCV，代码格式为带市场前缀
    小写（如 sh000001 / sz399001）。此处只取最新一天做增量写入，全历史回填请见
    backfill_benchmark.py（同源）。
    """
    import akshare as ak
    import warnings; warnings.filterwarnings("ignore")

    # 接口不支持区间参数，拉全量后在本地截断到最近约 20 个交易日
    # 保留 22 行：last(当天) + prev(昨) + 20 日前(iloc[-21]) 需 21 行窗口，多留 1 行余量
    # 避免 close_20d_ago 取 iloc[-21] 时越界，同时避免拉取全历史（数千行）的浪费
    _KEEP = 22

    records = []
    for it in items:
        sina_code = it.get("sina_code")
        if not sina_code:
            print(f"  [新浪A股] ❌ {it['name']} 缺少 sina_code")
            continue
        try:
            df = ak.stock_zh_index_daily(symbol=sina_code)
            if df is None or len(df) < 2:
                print(f"  [新浪A股] ⚠️ {it['name']}({sina_code}) 数据不足")
                continue
            df = df.dropna().tail(_KEEP)
            last = df.iloc[-1]
            prev = df.iloc[-2]
            td = last["date"]
            if not isinstance(td, date):
                td = date.fromisoformat(str(td)[:10])
            last_val = float(last["close"])
            prev_val = float(prev["close"])
            change = (last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0

            records.append({
                "bench_code": it["code"], "bench_name": it["name"],
                "trade_date": td,
                "update_time": datetime.combine(td, datetime.min.time()),
                "last_price": _r(last_val), "prev_close": _r(prev_val),
                "change_pct": _r(change),
                "close_20d_ago": _r(float(df.iloc[-21]["close"])) if len(df) >= 21 else None,
                "volume": int(last["volume"]) if "volume" in last.index and last["volume"] else None,
            })
            print(f"  [新浪A股] ✅ {it['name']}({sina_code})  {last_val:,.2f}  ({change:+.2f}%)  {td}  "
                  f"量:{last.get('volume', '-')}")
        except Exception as e:
            print(f"  [新浪A股] ❌ {it['name']}({sina_code})  {type(e).__name__}: {e}")
    return records


# ── Yahoo Finance 采集 ──
def _collect_yahoo(items: list) -> list:
    """采集 Yahoo Finance 指数（当前为 ICE 美元指数 DX-Y.NYB）。

    大陆 IP 被 Yahoo 风控（403/429），本机需走 socks5 代理（见 _yahoo_proxy），
    服务器（香港）直连。yfinance 1.5.1 的 proxy 参数与 curl_cffi 不兼容
    （报 Could not resolve proxy），故直接构造 curl_cffi.Session 调 v8 chart API。
    此处只取最新一天做增量写入，全历史回填见 backfill_benchmark.py（US.DXY 分支）。
    """
    from curl_cffi import requests as creq
    from datetime import timezone
    import time as _time

    proxy = _yahoo_proxy()
    sess_kwargs = {"impersonate": "chrome"}
    if proxy:
        sess_kwargs["proxies"] = {"http": proxy, "https": proxy}

    records = []
    for it in items:
        ycode = it.get("yf_code", it["code"].split(".")[-1])
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ycode}?range=3mo&interval=1d"
        last_err = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    _time.sleep(3 * attempt)
                sess = creq.Session(**sess_kwargs)
                r = sess.get(url, timeout=25)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    if r.status_code in (403, 429):
                        _time.sleep(5 * (attempt + 1))  # 风控/限流，退避更长
                    continue
                d = r.json()["chart"]["result"][0]
                meta = d["meta"]
                ts = d["timestamp"]
                quote = d["indicators"]["quote"][0]
                closes = quote["close"]
                if not ts or len(ts) < 2:
                    last_err = "数据不足"
                    continue
                td = datetime.fromtimestamp(ts[-1], timezone.utc).date()
                # 盘中最新 bar 的 close 可能为 None，用 regularMarketPrice 兜底
                last_val = closes[-1] if closes[-1] is not None else meta.get("regularMarketPrice")
                prev_val = None
                for c in reversed(closes[:-1]):
                    if c is not None:
                        prev_val = c
                        break
                if last_val is None or prev_val is None:
                    last_err = "close 缺失"
                    continue
                change = (last_val / prev_val - 1) * 100 if prev_val != 0 else 0.0
                valid = [c for c in closes if c is not None]
                c20 = valid[-21] if len(valid) >= 21 else None
                records.append({
                    "bench_code": it["code"], "bench_name": it["name"],
                    "trade_date": td,
                    "update_time": datetime.combine(td, datetime.min.time()),
                    "last_price": _r(last_val), "prev_close": _r(prev_val),
                    "change_pct": _r(change), "close_20d_ago": _r(c20) if c20 else None,
                })
                print(f"  [Yahoo] ✅ {it['name']}({ycode})  {last_val:.2f}  ({change:+.2f}%)  {td}")
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                _time.sleep(0.5)
        else:
            print(f"  [Yahoo] ❌ {it['name']}({ycode})  {last_err}")
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
        "sina":      _collect_sina,
        "sina_a":    _collect_sina_a,
        "eastmoney": _collect_eastmoney,
        "yfinance":  _collect_yahoo,
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
