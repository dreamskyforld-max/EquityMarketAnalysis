#!/usr/bin/env python3
"""
回补 daily_benchmark 历史数据
用法：
    python3 backfill_benchmark.py                         # 回补全部指数 ~1000 天
    python3 backfill_benchmark.py 120                     # 回补全部 120 天
    python3 backfill_benchmark.py 60 US.SP500             # 指定回补标普500
数据源：
    富途 K线:   HK.* / SH.* / SZ.* 指数
    FRED:      US.* 指数（需环境变量 FRED_API_KEY）
"""
import os
import sys
import datetime
from urllib.parse import quote

from db import get_conn, bulk_upsert, clean_ohlc
from collector_runtime import prefer_ipv4

# futu 改为惰性导入：仅 backfill_futu 内部使用，避免无 futu 环境的服务器
# （如只回补 A股/美债/外汇/国际指数时）在模块加载阶段即崩溃。

# ── 自动加载 .env ──
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

# ── 指数定义 ──
BENCHMARKS = {
    # 富途 K线
    "HK.800000":  ("恒生指数",               "futu"),
    "HK.800700":  ("恒生科技指数",           "futu"),
    # A股指数（服务器端 futu 无权限，改用 AKShare 新浪 A 股指数源）
    "SH.000001":  ("上证指数",               "sina_a"),
    "SZ.399001":  ("深证成指",               "sina_a"),
    # FRED
    "US.SP500":       ("标普500指数",        "fred"),
    "US.DJIA":        ("道琼斯工业指数",     "fred"),
    "US.NASDAQCOM":   ("纳斯达克综合指数",   "fred"),
    "US.VIXCLS":      ("VIX恐慌指数",        "fred"),
    "US.DTWEXBGS":    ("美元指数(贸易加权)", "fred"),
    "US.EFFR":        ("有效联邦基金利率",   "fred"),
    "US.DXY":         ("美元指数(ICE DXY)",  "yfinance"),
    # 国际指数：Yahoo（全历史 OHLC；本机经 socks5 代理，服务器直连）
    # 原走东财 push2，2026-10-27 实测本机被限流（Empty reply / 空 klines），
    # 且 lmt 上限仅约 1000 根，补不了 2014-2022 缺口。
    "JP.N225":        ("日经225指数",        "yfinance"),
    "KR.KS11":        ("韩国KOSPI指数",      "yfinance"),
    "DE.GDAXI":       ("德国DAX指数",        "yfinance"),
}

EM_SECID = {
    "JP.N225": "100.N225", "KR.KS11": "100.KS11", "DE.GDAXI": "100.GDAXI",
}

# Yahoo Finance 代码映射（国际指数；backfill_yahoo 用）
_YF_CODE = {
    "US.DXY": "DX-Y.NYB",
    "JP.N225": "^N225",
    "KR.KS11": "^KS11",
    "DE.GDAXI": "^GDAXI",
}

SINA_A_CODE = {
    "SH.000001": "sh000001", "SZ.399001": "sz399001",
}

FRED_TICKER = {
    "US.SP500": "SP500", "US.DJIA": "DJIA", "US.NASDAQCOM": "NASDAQCOM",
    "US.VIXCLS": "VIXCLS", "US.DTWEXBGS": "DTWEXBGS", "US.EFFR": "EFFR",
}

# FRED 中以"利率/收益率(%)"口径的序列：change_pct 用基点(bp)而非相对涨跌幅
_FRED_RATE_TICKERS = {"US.EFFR"}

# 富途单次 K 线返回上限（超过即静默截断，必须分页，见 fetch_futu_kline_paged）
_FUTU_PAGE = 1000


def build_records(kl_data, bench_code, bench_name):
    """从 K 线 DataFrame 构建记录列表（含 OHLC；源缺 open/high/low 列时置 None）"""
    closes = kl_data['close'].tolist()
    times = kl_data['time_key'].tolist()

    def _col(name):
        return kl_data[name].tolist() if name in kl_data.columns else [None] * len(closes)

    opens, highs, lows = _col('open'), _col('high'), _col('low')
    records = []
    for i in range(len(closes)):
        trade_date = str(times[i])[:10]
        last_price = float(closes[i])
        prev_close = float(closes[i - 1]) if i > 0 else None
        change_pct = round((last_price / prev_close - 1) * 100, 4) if prev_close and prev_close != 0 else None
        close_20d_ago = float(closes[i - 20]) if i >= 20 else None
        o, h, lo = clean_ohlc(opens[i], highs[i], lows[i])
        records.append({
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": trade_date, "update_time": trade_date,
            "last_price": last_price, "prev_close": prev_close,
            "change_pct": change_pct, "close_20d_ago": close_20d_ago,
            "open_price": o, "high_price": h, "low_price": lo,
        })
    return records


def write_records(records):
    """单连接批量 upsert（优化：替代逐条开连接，改用 bulk_upsert）"""
    if not records:
        return 0, 0
    try:
        with get_conn() as conn:
            bulk_upsert(conn, "daily_benchmark", records,
                        conflict_cols=["bench_code", "trade_date"])
        return len(records), 0
    except Exception as e:
        print(f"  ❌ 批量写入失败: {type(e).__name__}: {e}")
        return 0, len(records)


def fetch_futu_kline_paged(quote_ctx, code, start_date, end_date, ktype, autype,
                           max_total=None, max_pages=20):
    """分页拉日 K（富途单次上限 1000 根，超出直接截断）。

    背景（2026-10-27）：原实现单次请求 max_count=days+30，富途只认 ≤1000 根 →
    HK.800700 缺 2024-08 之后的 OHLC（单次只回区间内最早的 1000 根）。

    ⚠ 翻页方向必须**向后**（推 start）：实测富途对 [start, end] 区间返回的是区间内
    **最早的** max_count 根，不是最近 max_count 根。所以每页把 start 设为本页最晚
    time_key 的后一天，直到返回不足一页 / 越过 end_date / 页数用尽。

    返回按 time_key 升序、去重后的 DataFrame；无数据返回 None。
    """
    import pandas as pd
    from futu import RET_OK

    frames = []
    cursor_start = start_date
    for _ in range(max_pages):
        ret, kl, _ = quote_ctx.request_history_kline(
            code,
            start=cursor_start.isoformat(),
            end=end_date.isoformat(),
            ktype=ktype,
            autype=autype,
            max_count=_FUTU_PAGE,
            extended_time=False,
        )
        if ret != RET_OK or kl is None or len(kl) == 0:
            break
        frames.append(kl)
        if len(kl) < _FUTU_PAGE:
            break                       # 本页不满 → 已覆盖到 end_date
        latest = datetime.date.fromisoformat(str(kl.iloc[-1]["time_key"])[:10])
        if latest >= end_date:
            break
        cursor_start = latest + datetime.timedelta(days=1)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df = (df.drop_duplicates(subset=["time_key"])
            .sort_values("time_key")
            .reset_index(drop=True))
    if max_total and len(df) > max_total:
        df = df.iloc[-max_total:].reset_index(drop=True)
    return df


def backfill_futu(bench_code, bench_name, days):
    """富途 K线回补（自动分页，突破单次 1000 根上限）"""
    from futu import OpenQuoteContext, KLType, AuType
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days + 25)  # 多拉 25 天保证 20d 可算

        kl_data = fetch_futu_kline_paged(quote_ctx, bench_code, start, end,
                                         KLType.K_DAY, AuType.QFQ,
                                         max_total=days + 30)
        if kl_data is None or len(kl_data) < 2:
            print(f"  获取失败: 数据不足")
            return 0
        print(f"  获取 {len(kl_data)} 根日 K 线 "
              f"({str(kl_data['time_key'].iloc[0])[:10]} ~ "
              f"{str(kl_data['time_key'].iloc[-1])[:10]})")
        recs = build_records(kl_data, bench_code, bench_name)
        ins, skip = write_records(recs)
        print(f"  写入 {ins} 条, 跳过 {skip} 条")
        return ins
    finally:
        quote_ctx.close()


def backfill_sina_a(bench_code, bench_name, days):
    """AKShare 新浪 A股指数回补（服务器端 futu 无权限时的替代源）"""
    import warnings; warnings.filterwarnings("ignore")
    import akshare as ak

    sina_code = SINA_A_CODE.get(bench_code)
    if not sina_code:
        print(f"  ❌ 未配置新浪 A股代码映射: {bench_code}")
        return 0
    try:
        df = ak.stock_zh_index_daily(symbol=sina_code)
    except Exception as e:
        print(f"  ❌ 拉取失败: {type(e).__name__}: {e}")
        return 0
    if df is None or len(df) < 2:
        print(f"  ⚠️ 数据不足 ({len(df) if df is not None else 0}行)")
        return 0
    df = df.dropna().reset_index(drop=True)
    # 按 days 截断（保留足够算 20d 的前序）
    if days and len(df) > days:
        df = df.iloc[-(days + 20):]
    print(f"  获取 {len(df)} 行 ({df.iloc[0]['date']} ~ {df.iloc[-1]['date']})")

    recs = []
    for i in range(len(df)):
        td = df.iloc[i]["date"]
        if not isinstance(td, datetime.date):
            td = datetime.date.fromisoformat(str(td)[:10])
        last_val = float(df.iloc[i]["close"])
        prev_val = float(df.iloc[i - 1]["close"]) if i > 0 else None
        chg = round((last_val / prev_val - 1) * 100, 4) if prev_val and prev_val != 0 else None
        c20 = float(df.iloc[i - 20]["close"]) if i >= 20 else None
        o, h, lo = clean_ohlc(df.iloc[i].get("open"), df.iloc[i].get("high"),
                              df.iloc[i].get("low"))
        recs.append({
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": td,
            "update_time": datetime.datetime.combine(td, datetime.time.min),
            "last_price": last_val, "prev_close": prev_val,
            "change_pct": chg, "close_20d_ago": c20,
            "open_price": o, "high_price": h, "low_price": lo,
        })
    ins, skip = write_records(recs)
    print(f"  写入 {ins} 条, 跳过 {skip} 条")
    return ins


def backfill_fred(bench_code, bench_name, days):
    """FRED 回补"""
    from config import val
    api_key = os.environ.get("FRED_API_KEY") or val("fred", "api_key")
    if not api_key:
        print(f"  ❌ 缺少 FRED_API_KEY，跳过")
        return 0
    import pandas_datareader as pdr

    ticker = FRED_TICKER.get(bench_code, bench_code.split(".")[-1])
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days + 30)

    try:
        df = pdr.data.DataReader(ticker, "fred", start=start.isoformat(), end=end.isoformat())
        df = df.dropna()
        if len(df) < 2:
            print(f"  数据不足 ({len(df)}行)")
            return 0
        print(f"  获取 {len(df)} 行 ({df.index[0].date()} ~ {df.index[-1].date()})")

        recs = []
        for i in range(len(df)):
            td = df.index[i].date()
            val = float(df.iloc[i].iloc[0])
            prev = float(df.iloc[i-1].iloc[0]) if i > 0 else None
            if bench_code in _FRED_RATE_TICKERS:
                chg = round((val - prev) * 100, 4) if prev is not None else None
            else:
                chg = round((val / prev - 1) * 100, 4) if prev and prev != 0 else None
            c20 = float(df.iloc[i-20].iloc[0]) if i >= 20 else None
            recs.append({
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": datetime.datetime.combine(td, datetime.time.min),
                "last_price": val, "prev_close": prev,
                "change_pct": chg, "close_20d_ago": c20,
            })
        ins, skip = write_records(recs)
        print(f"  写入 {ins} 条, 跳过 {skip} 条")
        return ins
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return 0


def backfill_yahoo(bench_code, bench_name, days):
    """Yahoo Finance 回补（国际指数 ^N225/^KS11/^GDAXI + ICE 美元指数 DX-Y.NYB）。

    大陆 IP 被 Yahoo 风控需 socks5 代理，服务器(香港)直连 —— 复用
    get_global_benchmarks._yahoo_proxy 的环境探测；yfinance 1.5.1 proxy
    参数与 curl_cffi 不兼容，故直接构造 curl_cffi.Session 调 v8 chart API。
    """
    try:
        from get_global_benchmarks import _yahoo_proxy
        from curl_cffi import requests as creq
    except ImportError as e:
        print(f"  ❌ 依赖缺失: {e}")
        return 0

    ycode = _YF_CODE.get(bench_code, bench_code.split(".")[-1])
    proxy = _yahoo_proxy()
    sess_kwargs = {"impersonate": "chrome"}
    if proxy:
        sess_kwargs["proxies"] = {"http": proxy, "https": proxy}

    # ⚠ 不能用 range=max：实测（2026-10-27）Yahoo 对 range=max 会**静默把 interval=1d
    #   降采样成月线**（^N225 只回 169 根、日期全是月末）。改用 period1/period2 显式区间
    #   才稳定返回日线（同区间实测 3396 根）。
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    # days 是「交易日」，×1.6 折算自然日并加余量（周末/假期），再本地截断
    span_days = int(days * 1.6) + 60 if days else 3650
    p1 = now_ts - span_days * 86400
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{quote(ycode, safe='')}?period1={p1}&period2={now_ts}&interval=1d")
    try:
        sess = creq.Session(**sess_kwargs)
        r = sess.get(url, timeout=30)
        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code}")
            return 0
        d = r.json()["chart"]["result"][0]
        ts = d["timestamp"]
        q = d["indicators"]["quote"][0]
        closes = q.get("close") or []
        opens, highs, lows = q.get("open") or [], q.get("high") or [], q.get("low") or []

        def _at(arr, i):
            return arr[i] if i < len(arr) else None

        # 以 close 非空为准过滤（盘中最新的 bar 可能缺 close），OHLC 同步对齐
        bars = [(ts[i], closes[i], _at(opens, i), _at(highs, i), _at(lows, i))
                for i in range(len(ts)) if closes[i] is not None]
        if len(bars) < 2:
            print(f"  数据不足 ({len(bars)}行)")
            return 0
        if days and len(bars) > days + 20:
            bars = bars[-(days + 20):]
        print(f"  获取 {len(bars)} 行 "
              f"({datetime.datetime.fromtimestamp(bars[0][0]).date()} ~ "
              f"{datetime.datetime.fromtimestamp(bars[-1][0]).date()})")

        recs = []
        for i, (t, v, o, h, l) in enumerate(bars):
            td = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date()
            val = float(v)
            prev = float(bars[i-1][1]) if i > 0 else None
            chg = round((val / prev - 1) * 100, 4) if prev and prev != 0 else None
            c20 = float(bars[i-20][1]) if i >= 20 else None
            oo, hh, ll = clean_ohlc(o, h, l)
            recs.append({
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": datetime.datetime.combine(td, datetime.time.min),
                "last_price": val, "prev_close": prev,
                "change_pct": chg, "close_20d_ago": c20,
                "open_price": oo, "high_price": hh, "low_price": ll,
            })
        ins, skip = write_records(recs)
        print(f"  写入 {ins} 条, 跳过 {skip} 条")
        return ins
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return 0


def backfill_eastmoney(bench_code, bench_name, days):
    """东财全球指数回补"""
    prefer_ipv4()      # 本机 IPv6 出口对该站不可用（见 collector_runtime.prefer_ipv4）
    import requests as req
    secid = EM_SECID.get(bench_code)
    if not secid:
        print(f"  未知东财 secid")
        return 0
    # 注意: push2his.eastmoney.com 在当前网络不可达（2026-10-27 实测
    # RemoteDisconnected），改用 push2.eastmoney.com（同款 kline 接口）；
    # 与 collect_benchmark_history / get_global_benchmarks 保持一致。
    url = "https://push2.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "klt": "101", "fqt": "1", "lmt": str(days + 30),
        "end": "20500000", "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "f057cbcbce2a86e2866ab8877db1d059", "forcect": "1",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        r = req.get(url, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}")
            return 0
        data = r.json()
        kls = data.get("data", {}).get("klines")
        if not kls or len(kls) < 2:
            print(f"  数据不足")
            return 0
        rows = [l.split(",") for l in kls]
        print(f"  获取 {len(rows)} 行 ({rows[0][0][:10]} ~ {rows[-1][0][:10]})")

        recs = []
        for i in range(len(rows)):
            td = rows[i][0][:10]
            val = float(rows[i][2])
            prev = float(rows[i-1][2]) if i > 0 else None
            chg = round((val / prev - 1) * 100, 4) if prev and prev != 0 else None
            c20 = float(rows[i-20][2]) if i >= 20 else None
            cols = rows[i]
            # 东财 klines: f52=开 f53=收 f54=高 f55=低
            o, h, lo = clean_ohlc(cols[1] if len(cols) > 4 else None,
                                  cols[3] if len(cols) > 4 else None,
                                  cols[4] if len(cols) > 4 else None)
            recs.append({
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": datetime.datetime.combine(
                    datetime.date.fromisoformat(td), datetime.time.min),
                "last_price": val, "prev_close": prev,
                "change_pct": chg, "close_20d_ago": c20,
                "open_price": o, "high_price": h, "low_price": lo,
            })
        ins, skip = write_records(recs)
        print(f"  写入 {ins} 条, 跳过 {skip} 条")
        return ins
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return 0


def backfill(bench_code, days):
    """总入口：根据 bench_code 路由到对应数据源"""
    info = BENCHMARKS.get(bench_code)
    if info is None:
        print(f"未知指数: {bench_code}")
        print(f"已知: {', '.join(BENCHMARKS)}")
        return
    name, source = info
    print(f"回补 {name} ({bench_code})，最近 {days} 天 ...")
    if source == "futu":
        backfill_futu(bench_code, name, days)
    elif source == "fred":
        backfill_fred(bench_code, name, days)
    elif source == "sina_a":
        backfill_sina_a(bench_code, name, days)
    elif source == "yfinance":
        backfill_yahoo(bench_code, name, days)
    elif source == "eastmoney":
        backfill_eastmoney(bench_code, name, days)


if __name__ == "__main__":
    # 参数兼容两种写法：
    #   python3 backfill_benchmark.py [code] [days]   # 推荐：先代码后天数
    #   python3 backfill_benchmark.py [days] [code]   # 旧写法：先天数后代码
    #   python3 backfill_benchmark.py                 # 全部指数，默认 1000 天
    args = sys.argv[1:]
    code = None
    days = 1000
    for a in args:
        try:
            days = int(a)
        except ValueError:
            code = a  # 非整数即视为指数代码

    if code:
        backfill(code, days)
    else:
        for c in BENCHMARKS:
            backfill(c, days)
            print()
