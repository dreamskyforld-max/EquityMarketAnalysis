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
from db import get_conn, bulk_upsert

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
    # 东财全球指数
    "JP.N225":        ("日经225指数",        "eastmoney"),
    "KR.KS11":        ("韩国KOSPI指数",      "eastmoney"),
    "DE.GDAXI":       ("德国DAX指数",        "eastmoney"),
}

EM_SECID = {
    "JP.N225": "100.N225", "KR.KS11": "100.KS11", "DE.GDAXI": "100.GDAXI",
}

SINA_A_CODE = {
    "SH.000001": "sh000001", "SZ.399001": "sz399001",
}

FRED_TICKER = {
    "US.SP500": "SP500", "US.DJIA": "DJIA", "US.NASDAQCOM": "NASDAQCOM",
    "US.VIXCLS": "VIXCLS", "US.DTWEXBGS": "DTWEXBGS",
}


def build_records(kl_data, bench_code, bench_name):
    """从 K 线 DataFrame 构建记录列表"""
    closes = kl_data['close'].tolist()
    times = kl_data['time_key'].tolist()
    records = []
    for i in range(len(closes)):
        trade_date = str(times[i])[:10]
        last_price = float(closes[i])
        prev_close = float(closes[i - 1]) if i > 0 else None
        change_pct = round((last_price / prev_close - 1) * 100, 4) if prev_close and prev_close != 0 else None
        close_20d_ago = float(closes[i - 20]) if i >= 20 else None
        records.append({
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": trade_date, "update_time": trade_date,
            "last_price": last_price, "prev_close": prev_close,
            "change_pct": change_pct, "close_20d_ago": close_20d_ago,
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


def backfill_futu(bench_code, bench_name, days):
    """富途 K线回补"""
    from futu import OpenQuoteContext, RET_OK, KLType, AuType
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days + 25)  # 多拉 25 天保证 20d 可算

        ret, kl_data, _ = quote_ctx.request_history_kline(
            bench_code,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=days + 30,
            extended_time=False,
        )
        if ret != RET_OK or kl_data is None or len(kl_data) < 2:
            print(f"  获取失败: {kl_data if ret != RET_OK else '数据不足'}")
            return 0
        print(f"  获取 {len(kl_data)} 根日 K 线")
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
        recs.append({
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": td,
            "update_time": datetime.datetime.combine(td, datetime.time.min),
            "last_price": last_val, "prev_close": prev_val,
            "change_pct": chg, "close_20d_ago": c20,
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


def backfill_eastmoney(bench_code, bench_name, days):
    """东财全球指数回补"""
    import requests as req
    secid = EM_SECID.get(bench_code)
    if not secid:
        print(f"  未知东财 secid")
        return 0
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
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
            recs.append({
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": datetime.datetime.combine(
                    datetime.date.fromisoformat(td), datetime.time.min),
                "last_price": val, "prev_close": prev,
                "change_pct": chg, "close_20d_ago": c20,
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
