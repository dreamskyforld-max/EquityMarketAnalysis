#!/usr/bin/env python3
"""
一次性采集全部 15 个全球基准指数的完整历史数据到 daily_benchmark 表。

与 backfill_benchmark.py 的区别：
  - 本脚本只追求「全部历史」，不接收天数参数（自动拉到各数据源上限）。
  - FRED 拉 1990 年以来全部（SP500 等起始更早）；富途按 max_count 上限；
    国际指数(日韩德)用 AKShare 新浪源；A股指数(上证/深证)用新浪A股源。
  - 成交量/额一并采集（富途 K线 + 新浪源 + 东财源均支持）。
  - write_records 为单连接批量 upsert。

依赖：
  - 富途源: 本地 OpenD 在线 (127.0.0.1:11111)
  - FRED源: FRED_API_KEY (.env 或 config.conf [fred] api_key)
  - 国际指数(sina): AKShare（无需 OpenD / key）
  - 数据库: PG 连接正常
"""
import os
import sys
import datetime
import logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 自动加载 .env ──
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

# ── 指数定义（与 backfill_benchmark 保持一致）──
BENCHMARKS = {
    # 富途 K线（港股指数）
    "HK.800000":  ("恒生指数",               "futu"),
    "HK.800700":  ("恒生科技指数",           "futu"),
    # A股指数（新浪源，服务器端 futu 无 A 股权限）
    "SH.000001":  ("上证指数",               "sina_a"),
    "SZ.399001":  ("深证成指",               "sina_a"),
    # FRED
    "US.SP500":       ("标普500指数",        "fred"),
    "US.DJIA":        ("道琼斯工业指数",     "fred"),
    "US.NASDAQCOM":   ("纳斯达克综合指数",   "fred"),
    "US.VIXCLS":      ("VIX恐慌指数",        "fred"),
    "US.DTWEXBGS":    ("美元指数(贸易加权)", "fred"),
    # 国际指数（AKShare 新浪源）
    "JP.N225":        ("日经225指数",        "sina"),
    "KR.KS11":        ("韩国KOSPI指数",      "sina"),
    "DE.GDAXI":       ("德国DAX指数",        "sina"),
    # 离岸人民币兑美元中间价（AKShare 外汇局全历史）
    "FX.USDCNY":      ("离岸人民币(USD/CNY)", "currency"),
    # 美债收益率（AKShare bond_zh_us_rate，中美利率总表）
    "US.DGS10":       ("美国10年期国债收益率", "bond"),
    "US.DGS2":        ("美国2年期国债收益率",  "bond"),
}

SINA_NAME = {
    "JP.N225": "日经225指数", "KR.KS11": "首尔综合指数", "DE.GDAXI": "德国DAX 30种股价指数",
}

SINA_A_CODE = {
    "SH.000001": "sh000001", "SZ.399001": "sz399001",
}

EM_SECID = {
    "JP.N225": "100.N225", "KR.KS11": "100.KS11", "DE.GDAXI": "100.GDAXI",
}

FRED_TICKER = {
    "US.SP500": "SP500", "US.DJIA": "DJIA", "US.NASDAQCOM": "NASDAQCOM",
    "US.VIXCLS": "VIXCLS", "US.DTWEXBGS": "DTWEXBGS",
}

# 采集窗口：最近 3 年
_LOOKBACK_DAYS = 1095
# 富途单页 K 线条数上限（3 年约 750 交易日，1000 足够）
_FUTU_MAX = 1000
# 东财 lmt（3 年约 750 交易日，留余量）
_EM_MAX = 1200


def write_records(records):
    """单连接批量 upsert 到 daily_benchmark（优化版，替代逐条开连接）。"""
    if not records:
        return 0, 0
    from db import get_conn, bulk_upsert
    try:
        with get_conn() as conn:
            bulk_upsert(conn, "daily_benchmark", records,
                        conflict_cols=["bench_code", "trade_date"])
        return len(records), 0
    except Exception as e:
        logger.error("批量写入失败: %s", e)
        print(f"  ❌ 批量写入失败: {type(e).__name__}: {e}")
        return 0, len(records)


def build_records(kl_data, bench_code, bench_name):
    """从 K 线 DataFrame 构建记录列表（含成交量/额）"""
    closes = kl_data['close'].tolist()
    times = kl_data['time_key'].tolist()
    # 富途 K 线可能包含 volume / turnover 列
    has_vol = 'volume' in kl_data.columns
    has_to = 'turnover' in kl_data.columns
    vols = kl_data['volume'].tolist() if has_vol else None
    tos = kl_data['turnover'].tolist() if has_to else None
    records = []
    for i in range(len(closes)):
        trade_date = str(times[i])[:10]
        last_price = float(closes[i])
        prev_close = float(closes[i - 1]) if i > 0 else None
        change_pct = round((last_price / prev_close - 1) * 100, 4) if prev_close and prev_close != 0 else None
        close_20d_ago = float(closes[i - 20]) if i >= 20 else None
        rec = {
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": trade_date, "update_time": trade_date,
            "last_price": last_price, "prev_close": prev_close,
            "change_pct": change_pct, "close_20d_ago": close_20d_ago,
        }
        if vols and vols[i] is not None:
            rec["volume"] = int(vols[i])
        if tos and tos[i] is not None:
            rec["turnover"] = float(tos[i])
        records.append(rec)
    return records


def collect_futu(bench_code, bench_name):
    """富途 K线：拉取最近 1 年日 K（单页足够）"""
    from futu import OpenQuoteContext, RET_OK, KLType, AuType
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=_LOOKBACK_DAYS)
        ret, kl_data, _ = quote_ctx.request_history_kline(
            bench_code,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=_FUTU_MAX,
            extended_time=False,
        )
        if ret != RET_OK or kl_data is None or len(kl_data) < 2:
            print(f"  ❌ 获取失败: {kl_data if ret != RET_OK else '数据不足'}")
            return 0
        print(f"  ✅ 获取 {len(kl_data)} 根日 K 线 ({str(kl_data['time_key'].iloc[0])[:10]} ~ {str(kl_data['time_key'].iloc[-1])[:10]})")
        recs = build_records(kl_data, bench_code, bench_name)
        ins, skip = write_records(recs)
        print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
        return ins
    finally:
        quote_ctx.close()


def collect_fred(bench_code, bench_name):
    """FRED：拉取最近 1 年"""
    from config import val
    api_key = os.environ.get("FRED_API_KEY") or val("fred", "api_key")
    if not api_key:
        print(f"  ❌ 缺少 FRED_API_KEY，跳过")
        return 0
    import pandas_datareader as pdr

    ticker = FRED_TICKER.get(bench_code, bench_code.split(".")[-1])
    end = datetime.date.today()
    start = end - datetime.timedelta(days=_LOOKBACK_DAYS)

    try:
        df = pdr.data.DataReader(ticker, "fred", start=start.isoformat(), end=end.isoformat())
        df = df.dropna()
        if len(df) < 2:
            print(f"  ⚠️ 数据不足 ({len(df)}行)")
            return 0
        print(f"  ✅ 获取 {len(df)} 行 ({df.index[0].date()} ~ {df.index[-1].date()})")

        recs = []
        for i in range(len(df)):
            td = df.index[i].date()
            v = float(df.iloc[i].iloc[0])
            prev = float(df.iloc[i-1].iloc[0]) if i > 0 else None
            chg = round((v / prev - 1) * 100, 4) if prev and prev != 0 else None
            c20 = float(df.iloc[i-20].iloc[0]) if i >= 20 else None
            recs.append({
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": datetime.datetime.combine(td, datetime.time.min),
                "last_price": v, "prev_close": prev,
                "change_pct": chg, "close_20d_ago": c20,
            })
        ins, skip = write_records(recs)
        print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
        return ins
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return 0


def collect_eastmoney(bench_code, bench_name):
    """东财全球指数：拉取全部历史（lmt=_EM_MAX）"""
    import requests as req
    secid = EM_SECID.get(bench_code)
    if not secid:
        print(f"  ❌ 未知东财 secid")
        return 0
    # 注意: push2his.eastmoney.com 在当前网络不可达，改用 push2.eastmoney.com（同款 kline 接口）
    url = "https://push2.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "klt": "101", "fqt": "1", "lmt": str(_EM_MAX),
        "end": "20500000", "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "f057cbcbce2a86e2866ab8877db1d059", "forcect": "1",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        rows = None
        last_err = None
        for attempt in range(5):
            if attempt > 0:
                import time as _t
                _t.sleep(3 * attempt)  # 退避: 3s, 6s, 9s, 12s
                print(f"  · 东财重试第{attempt}次 ...")
            try:
                r = req.get(url, params=params, headers=headers, timeout=15)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    continue
                data = r.json()
                kls = data.get("data", {}).get("klines")
                if not kls or len(kls) < 2:
                    last_err = "数据不足"
                    continue
                rows = [l.split(",") for l in kls]
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        if rows is None:
            print(f"  ❌ 失败: {last_err}")
            return 0
        print(f"  ✅ 获取 {len(rows)} 行 ({rows[0][0][:10]} ~ {rows[-1][0][:10]})")

        recs = []
        for i in range(len(rows)):
            td = rows[i][0][:10]
            # 东财 klines: f51=日期 f52=开 f53=收 f54=高 f55=低 f56=量 f57=额 ...
            v = float(rows[i][2])
            prev = float(rows[i-1][2]) if i > 0 else None
            chg = round((v / prev - 1) * 100, 4) if prev and prev != 0 else None
            c20 = float(rows[i-20][2]) if i >= 20 else None
            vol = int(float(rows[i][5])) if len(rows[i]) > 5 and rows[i][5] and rows[i][5] != '-' else None
            to = float(rows[i][6]) if len(rows[i]) > 6 and rows[i][6] and rows[i][6] != '-' else None
            rec = {
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": datetime.datetime.combine(
                    datetime.date.fromisoformat(td), datetime.time.min),
                "last_price": v, "prev_close": prev,
                "change_pct": chg, "close_20d_ago": c20,
            }
            if vol is not None:
                rec["volume"] = vol
            if to is not None:
                rec["turnover"] = to
            recs.append(rec)
        ins, skip = write_records(recs)
        print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
        return ins
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return 0


def collect_sina(bench_code, bench_name):
    """AKShare 新浪源国际指数（日经225 / KOSPI / DAX）。

    东财 push2 接口对当前网络不稳（RemoteDisconnected），改用 AKShare 的
    index_global_hist_sina（新浪源），稳定返回日线 OHLC（约 1000 条/指数，约4年）。
    """
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    sina_name = SINA_NAME.get(bench_code)
    if not sina_name:
        print(f"  ❌ 未配置新浪源名称")
        return 0
    try:
        df = ak.index_global_hist_sina(symbol=sina_name)
    except Exception as e:
        print(f"  ❌ 拉取失败: {type(e).__name__}: {e}")
        return 0
    if df is None or len(df) < 2:
        print(f"  ⚠️ 数据不足 ({len(df) if df is not None else 0}行)")
        return 0

    df = df.dropna().reset_index(drop=True)
    has_vol = "volume" in df.columns
    recs = []
    for i in range(len(df)):
        td = df.iloc[i]["date"]
        if not isinstance(td, datetime.date):
            td = datetime.date.fromisoformat(str(td)[:10])
        v = float(df.iloc[i]["close"])
        prev = float(df.iloc[i - 1]["close"]) if i > 0 else None
        chg = round((v / prev - 1) * 100, 4) if (prev and prev != 0) else None
        c20 = float(df.iloc[i - 20]["close"]) if i >= 20 else None
        rec = {
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": td,
            "update_time": datetime.datetime.combine(td, datetime.time.min),
            "last_price": v, "prev_close": prev,
            "change_pct": chg, "close_20d_ago": c20,
        }
        if has_vol:
            vol = df.iloc[i].get("volume", 0)
            rec["volume"] = int(vol) if vol and vol != 0 else None
        recs.append(rec)
    print(f"  ✅ 获取 {len(recs)} 行 ({recs[0]['trade_date']} ~ {recs[-1]['trade_date']})")
    ins, skip = write_records(recs)
    print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
    return ins


def collect_sina_a(bench_code, bench_name):
    """AKShare 新浪源 A股指数（上证/深证），含成交量。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    sina_code = SINA_A_CODE.get(bench_code)
    if not sina_code:
        print(f"  ❌ 未配置新浪A股代码")
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
    has_vol = "volume" in df.columns
    recs = []
    for i in range(len(df)):
        td = df.iloc[i]["date"]
        if not isinstance(td, datetime.date):
            td = datetime.date.fromisoformat(str(td)[:10])
        v = float(df.iloc[i]["close"])
        prev = float(df.iloc[i - 1]["close"]) if i > 0 else None
        chg = round((v / prev - 1) * 100, 4) if (prev and prev != 0) else None
        c20 = float(df.iloc[i - 20]["close"]) if i >= 20 else None
        rec = {
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": td,
            "update_time": datetime.datetime.combine(td, datetime.time.min),
            "last_price": v, "prev_close": prev,
            "change_pct": chg, "close_20d_ago": c20,
        }
        if has_vol:
            vol = df.iloc[i].get("volume", 0)
            rec["volume"] = int(vol) if vol and vol != 0 else None
        recs.append(rec)
    print(f"  ✅ 获取 {len(recs)} 行 ({recs[0]['trade_date']} ~ {recs[-1]['trade_date']})")
    ins, skip = write_records(recs)
    print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
    return ins


def collect(bench_code):
    """根据 bench_code 路由到对应数据源采集最近 _LOOKBACK_DAYS 天"""
    info = BENCHMARKS.get(bench_code)
    if info is None:
        print(f"未知指数: {bench_code}")
        return 0
    name, source = info
    print(f"\n── {name} ({bench_code}) [{source}] ──")
    if source == "futu":
        return collect_futu(bench_code, name)
    elif source == "fred":
        return collect_fred(bench_code, name)
    elif source == "sina":
        return collect_sina(bench_code, name)
    elif source == "sina_a":
        return collect_sina_a(bench_code, name)
    elif source == "currency":
        return collect_currency(bench_code, name)
    elif source == "bond":
        return collect_bond(bench_code, name)
    elif source == "eastmoney":
        return collect_eastmoney(bench_code, name)
    return 0


def collect_bond(bench_code, bench_name):
    """美债收益率（AKShare bond_zh_us_rate，中美利率总表）。

    该接口一次性返回全历史（可追溯到 ~2002 年），列名形如
    '美国国债收益率10年' / '美国国债收益率2年'。收益率为百分比（如 4.50 表示 4.50%），
    change_pct 存「收益率变化」百分比点数差值（last - prev，单位 pp），与增量采集
    get_global_benchmarks.py 的 akshare 口径一致。
    """
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    bond_map = {"US.DGS10": "美国国债收益率10年", "US.DGS2": "美国国债收益率2年"}
    col = bond_map.get(bench_code)
    if not col:
        print(f"  ❌ 未配置美债映射列")
        return 0
    try:
        df = ak.bond_zh_us_rate()
    except Exception as e:
        print(f"  ❌ 拉取失败: {type(e).__name__}: {e}")
        return 0
    if df is None or col not in df.columns:
        print(f"  ❌ 列 '{col}' 不存在")
        return 0

    sub = df[["日期", col]].dropna().reset_index(drop=True)
    if len(sub) < 2:
        print(f"  ⚠️ 数据不足 ({len(sub)}行)")
        return 0

    recs = []
    for i in range(len(sub)):
        td = sub.iloc[i]["日期"]
        if not isinstance(td, datetime.date):
            td = datetime.date.fromisoformat(str(td)[:10])
        v = float(sub.iloc[i][col])
        prev = float(sub.iloc[i - 1][col]) if i > 0 else v
        # 收益率变化：存百分比点数差值（如 4.49→4.50 变化 0.01pp）
        chg = round(v - prev, 4)
        c20 = float(sub.iloc[i - 20][col]) if i >= 20 else None
        recs.append({
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": td,
            "update_time": datetime.datetime.combine(td, datetime.time.min),
            "last_price": v, "prev_close": prev,
            "change_pct": chg, "close_20d_ago": c20,
        })
    print(f"  ✅ 获取 {len(recs)} 行 ({recs[0]['trade_date']} ~ {recs[-1]['trade_date']})")
    ins, skip = write_records(recs)
    print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
    return ins


def collect_currency(bench_code, bench_name):
    """离岸人民币兑美元中间价（AKShare currency_boc_safe，国家外汇管理局全历史）。

    返回 8022+ 行全历史中间价，"美元"列即 USD/CNY。注意这是外汇管理局在岸中间价，
    非严格离岸 CNH，但作为人民币兑美元基准足够。
    """
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    try:
        df = ak.currency_boc_safe()
    except Exception as e:
        print(f"  ❌ 拉取失败: {type(e).__name__}: {e}")
        return 0
    if df is None or len(df) == 0:
        print(f"  ⚠️ 数据为空")
        return 0

    df = df.dropna(subset=["日期", "美元"]).reset_index(drop=True)
    recs = []
    for i in range(len(df)):
        td = df.iloc[i]["日期"]
        if not isinstance(td, datetime.date):
            td = datetime.date.fromisoformat(str(td)[:10])
        mid = float(df.iloc[i]["美元"]) / 100.0  # 外汇局单位为「每100外币兑人民币」
        prev = float(df.iloc[i - 1]["美元"]) / 100.0 if i > 0 else mid
        chg = round((mid / prev - 1) * 100, 4) if (prev and prev != 0) else None
        c20 = (float(df.iloc[i - 20]["美元"]) / 100.0) if i >= 20 else None
        recs.append({
            "bench_code": bench_code, "bench_name": bench_name,
            "trade_date": td,
            "update_time": datetime.datetime.combine(td, datetime.time.min),
            "last_price": mid, "prev_close": prev,
            "change_pct": chg, "close_20d_ago": c20,
        })
    print(f"  ✅ 获取 {len(recs)} 行 ({recs[0]['trade_date']} ~ {recs[-1]['trade_date']})")
    ins, skip = write_records(recs)
    print(f"  ✅ 写入 {ins} 条, 跳过 {skip} 条")
    return ins


if __name__ == "__main__":
    # 支持可选参数：指定单个指数代码，否则采集全部指数
    only = sys.argv[1] if len(sys.argv) > 1 else None
    targets = [only] if only else list(BENCHMARKS.keys())

    if not only:
        print("=" * 60)
        print(f"开始采集全部 {len(targets)} 个基准指数最近 {_LOOKBACK_DAYS} 天的数据")
        print("=" * 60)

    import time as _time
    total = 0
    for idx, code in enumerate(targets):
        total += collect(code)
        src = BENCHMARKS.get(code, ("", ""))[1]
        # 每个指数间留间隔，避免触发东财频率限制；东财源额外加长
        gap = 5 if src == "eastmoney" else 1.5
        if idx < len(targets) - 1:
            print(f"  (间隔 {gap}s ...)")
            _time.sleep(gap)

    print("\n" + "=" * 60)
    print(f"全部完成，累计写入 {total} 条历史记录")
    print("=" * 60)
