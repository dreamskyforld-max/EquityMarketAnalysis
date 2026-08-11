#!/usr/bin/env python3
"""
基准指数行情采集 — 自动根据股票市场选择对应基准指数（常驻调用版）

数据源：FutuOpenD get_market_snapshot()（实时快照）+ request_history_kline()（20日前收盘价）
写入表：daily_benchmark（按 bench_code + trade_date 去重）

基准选择规则：
    HK.* → 恒生指数 (HK.800000)
    SH.* → 上证指数 (SH.000001)
    SZ.* → 深证成指 (SZ.399001)

用法（独立运行兼容）：
    python3 get_benchmark.py              # 默认 HK.00700 → 恒生指数
    python3 get_benchmark.py SH.600900    # → 上证指数
    python3 get_benchmark.py SZ.000001    # → 深证成指

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用。
"""
import sys
import logging
from datetime import date
logging.basicConfig(level=logging.WARNING)
from futu import RET_OK, KLType, AuType
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

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
            upsert(conn, "daily_benchmark", data, conflict_cols=["bench_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 基准指数入库失败 ({bench_code}): {e}")


# 20日前收盘价缓存：盘中同一交易日内不会变化，避免每分钟重复拉 K线
_close_20d_cache = {}


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes: [股票代码]；ctx: 共享行情上下文（可空）。"""
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

    # A股指数：服务器端 futu 无权限，改用 AKShare 新浪 A股指数源
    if bench_code in ("SH.000001", "SZ.399001"):
        _run_sina_a(bench_code, bench_name)
        return

    # 1. 获取快照
    ret_snap, snap_data = ctx.get_market_snapshot([bench_code])
    if ret_snap != RET_OK:
        print(f"获取{bench_name}快照失败: {snap_data}")
        return

    row = snap_data.iloc[0]
    name = row['name']
    close = row['last_price']
    prev_close = row.get('prev_close_price', close)
    change_pct = (float(close) / float(prev_close) - 1) * 100 if prev_close and float(prev_close) != 0 else 0
    update_time = row.get('update_time', 'N/A')

    # 2. 拉取历史K线（盘中缓存：同一交易日只拉一次）
    today = date.today()
    cache_key = f"{bench_code}_{today.isoformat()}"
    close_20d_ago = _close_20d_cache.get(cache_key)
    if close_20d_ago is None:
        ret_kl, kl_data, _ = ctx.request_history_kline(
            bench_code,
            ktype=KLType.K_DAY,
            autype=AuType.QFQ,
            max_count=40,
            extended_time=False
        )
        if ret_kl == RET_OK and len(kl_data) >= 20:
            close_20d_ago = kl_data.iloc[-20]['close']
            _close_20d_cache[cache_key] = close_20d_ago
        else:
            close_20d_ago = None

    # 输出
    print(f"{name} ({bench_code})")
    print(f"更新时间: {update_time}")
    print(f"最新价: {fmt_price(close)}")
    print(f"昨收: {fmt_price(prev_close)}")
    print(f"涨跌幅: {change_pct:.2f}%")
    if close_20d_ago:
        print(f"20日前收盘价: {fmt_price(close_20d_ago)}")
    else:
        print("20日前收盘价: 获取失败")

    # 写入 daily_benchmark 表
    try:
        trade_date = date.fromisoformat(str(update_time)[:10]) if update_time and update_time != 'N/A' else date.today()
        data = {
            "bench_code": bench_code,
            "bench_name": bench_name,
            "trade_date": trade_date,
            "update_time": update_time if update_time != 'N/A' else None,
            "last_price": float(close) if close is not None else None,
            "prev_close": float(prev_close) if prev_close else None,
            "change_pct": round(float(change_pct), 4),
            "close_20d_ago": float(close_20d_ago) if close_20d_ago else None,
        }
        with get_conn() as conn:
            upsert(conn, "daily_benchmark", data, conflict_cols=["bench_code", "trade_date"])
    except Exception as e:
        print(f"[DB] 基准指数入库失败 ({bench_code}): {e}")


if __name__ == "__main__":
    stock_code = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run([stock_code])
