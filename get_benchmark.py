#!/usr/bin/env python3
"""
基准指数行情采集 — 自动根据股票市场选择对应基准指数（常驻调用版）

数据源（2026-11-18 起统一为 AKShare 新浪源）：
    HK.* → 新浪港股指数（HSI）
    SH.* → 新浪 A股指数（sh000001）
    SZ.* → 新浪 A股指数（sz399001）

历史沿革：港股原走富途快照，但富途对指数快照/日K 的 volume 恒返回 0.0（实测），
        代码判假后写 NULL，且整行覆盖会把库内已有成交量冲掉 → 改为新浪源，
        与 A 股指数口径统一（新浪同时提供 volume + amount）。代价：新浪为收盘后
        更新，盘中拿到的是上一交易日——与 A 股指数现状一致。

写入表：daily_benchmark（按 bench_code + trade_date 去重）

用法（独立运行兼容）：
    python3 get_benchmark.py              # 默认 HK.00700 → 恒生指数
    python3 get_benchmark.py SH.600900    # → 上证指数
    python3 get_benchmark.py SZ.000001    # → 深证成指

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用。
"""
import sys
import logging
logging.basicConfig(level=logging.WARNING)
from db import get_conn, upsert, clean_ohlc


def fmt_price(val, max_dec=4):
    if val is None:
        return "N/A"
    try:
        v = round(float(val), max_dec)
    except (ValueError, TypeError):
        return str(val)
    s = f"{v:.{max_dec}f}"
    s = s.rstrip('0').rstrip('.')
    if '.' not in s:
        s += ".0"
    return s

def _run_sina_a(bench_code, bench_name):
    """A股指数基准采集（新浪 API，无 akshare 依赖）。

    数据源：新浪财经 K 线 JSON API
    https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
    """
    import requests as req
    import datetime as _dt

    sina_map = {"SH.000001": "sh000001", "SZ.399001": "sz399001"}
    sina_code = sina_map.get(bench_code)
    if not sina_code:
        print(f"未配置新浪 A股代码: {bench_code}")
        return
    try:
        r = req.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": sina_code, "scale": "240", "ma": "no", "datalen": "25"},
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        print(f"获取{bench_name}失败: {type(e).__name__}: {e}")
        return
    if not data or len(data) < 2:
        print(f"获取{bench_name}失败: 数据不足")
        return
    last = data[-1]; prev = data[-2]
    td = _dt.date.fromisoformat(last["day"])
    close = float(last["close"])
    prev_close = float(prev["close"])
    change_pct = (close / prev_close - 1) * 100 if prev_close else 0.0
    close_20d_ago = float(data[-21]["close"]) if len(data) >= 21 else None
    volume = int(float(last["volume"])) if last.get("volume") else None
    o, h, lo = clean_ohlc(last.get("open"), last.get("high"), last.get("low"))

    print(f"{bench_name} ({bench_code}) [新浪源]")
    print(f"最新价: {fmt_price(close)}")
    print(f"昨收: {fmt_price(prev_close)}")
    print(f"涨跌幅: {change_pct:.2f}%")
    if close_20d_ago:
        print(f"20日前收盘价: {fmt_price(close_20d_ago)}")

    try:
        with get_conn() as conn:
            data = {
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": _dt.datetime.combine(td, _dt.datetime.min.time()),
                "last_price": close, "prev_close": prev_close,
                "change_pct": round(change_pct, 4),
                "close_20d_ago": close_20d_ago,
                "volume": volume,
            }
            if o is not None:
                data["open_price"], data["high_price"], data["low_price"] = o, h, lo
            upsert(conn, "daily_benchmark", data, conflict_cols=["bench_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 基准指数入库失败 ({bench_code}): {e}")


def _run_sina_hk(bench_code, bench_name):
    """港股指数基准采集（新浪港股源，与 A 股指数同口径）。

    2026-11-18 由 futu 快照切换：富途对指数快照/日K 的 volume 恒返回 0.0（实测），
    新浪源同时提供 volume + amount（amount 与富途 turnover 同口径，已逐日核对）。
    代价：新浪收盘后更新，盘中拿到的是上一交易日——与 A 股指数现状一致。
    """
    import warnings; warnings.filterwarnings("ignore")
    import akshare as ak
    import datetime as _dt

    symbol = {"HK.800000": "HSI", "HK.800700": "HSTECH"}.get(bench_code)
    if not symbol:
        print(f"未配置新浪港股符号: {bench_code}")
        return
    try:
        df = ak.stock_hk_index_daily_sina(symbol=symbol)
    except Exception as e:
        print(f"获取{bench_name}失败: {type(e).__name__}: {e}")
        return
    if df is None or len(df) < 2:
        print(f"获取{bench_name}失败: 数据不足")
        return
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    last = df.iloc[-1]; prev = df.iloc[-2]
    td = last["date"]
    if not isinstance(td, _dt.date):
        td = _dt.date.fromisoformat(str(td)[:10])
    close = float(last["close"])
    prev_close = float(prev["close"])
    change_pct = (close / prev_close - 1) * 100 if prev_close else 0.0
    close_20d_ago = float(df.iloc[-21]["close"]) if len(df) >= 21 else None
    o, h, lo = clean_ohlc(last.get("open"), last.get("high"), last.get("low"))
    vol = last.get("volume")
    amt = last.get("amount")

    print(f"{bench_name} ({bench_code}) [新浪港股源]")
    print(f"最新价: {fmt_price(close)}")
    print(f"昨收: {fmt_price(prev_close)}")
    print(f"涨跌幅: {change_pct:.2f}%")
    if close_20d_ago:
        print(f"20日前收盘价: {fmt_price(close_20d_ago)}")

    try:
        with get_conn() as conn:
            data = {
                "bench_code": bench_code, "bench_name": bench_name,
                "trade_date": td,
                "update_time": _dt.datetime.combine(td, _dt.datetime.min.time()),
                "last_price": close, "prev_close": prev_close,
                "change_pct": round(change_pct, 4),
                "close_20d_ago": close_20d_ago,
                "volume": int(vol) if vol and vol == vol else None,
                "turnover": float(amt) if amt and amt == amt else None,
            }
            if o is not None:
                data["open_price"], data["high_price"], data["low_price"] = o, h, lo
            upsert(conn, "daily_benchmark", data, conflict_cols=["bench_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 基准指数入库失败 ({bench_code}): {e}")


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: [股票代码]；ctx 保留仅为签名兼容（新浪源不需要行情上下文）。"""
    stock_code = codes[0] if (codes and len(codes) > 0) else "HK.00700"

    # --- 根据股票代码自动选择基准指数 ---
    if stock_code.startswith("SH."):
        bench_code = "SH.000001"
        bench_name = "上证指数"
    elif stock_code.startswith("SZ."):
        bench_code = "SZ.399001"
        bench_name = "深证成指"
    else:
        bench_code = "HK.800000"
        bench_name = "恒生指数"

    # A股 / 港股指数统一走 AKShare 新浪源
    # 2026-11-18：港股由 futu 快照切换（富途对指数 volume 恒 0，整行覆盖会把库内成交量冲成 NULL）
    if bench_code in ("SH.000001", "SZ.399001"):
        _run_sina_a(bench_code, bench_name)
        return
    _run_sina_hk(bench_code, bench_name)


if __name__ == "__main__":
    stock_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([stock_code])
