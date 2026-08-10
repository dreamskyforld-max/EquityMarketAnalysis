#!/usr/bin/env python3
"""
国际指数历史回填 — AKShare 新浪源（日经225 / KOSPI / DAX）。

背景: 东方财富 push2 接口对当前出口 IP 不稳定（RemoteDisconnected），
      改用 AKShare 的 index_global_hist_sina（新浪源）稳定拉取全历史日线。

数据写入: daily_benchmark 表（与 get_global_benchmarks.py 同表，幂等 upsert）。

用法:
  python3 collect_benchmark_sina.py                 # 全量回填三个指数
  python3 collect_benchmark_sina.py --only JP.N225  # 只回填单个
  python3 collect_benchmark_sina.py --days 1095     # 只取最近 N 天（默认全量）
"""
import sys
import time
import warnings
from datetime import date, datetime
from typing import Dict, List

warnings.filterwarnings("ignore")

# 新浪源 symbol 映射（键为 AKShare index_global_hist_sina 的中文全名）
SINA_TARGETS: List[Dict] = [
    {"code": "JP.N225", "name": "日经225指数", "sina_name": "日经225指数"},
    {"code": "KR.KS11", "name": "韩国KOSPI指数", "sina_name": "首尔综合指数"},
    {"code": "DE.GDAXI", "name": "德国DAX指数", "sina_name": "德国DAX 30种股价指数"},
]


def _r(v, ndigits=4):
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def collect_one(it: dict, days: int = 0) -> List[dict]:
    """拉取单个指数的全历史（或最近 N 天）日线，映射为 daily_benchmark 记录。"""
    import akshare as ak

    records: List[dict] = []
    try:
        df = ak.index_global_hist_sina(symbol=it["sina_name"])
    except Exception as e:
        print(f"  ❌ {it['name']}({it['sina_name']}) 拉取失败: {type(e).__name__}: {e}")
        return records

    if df is None or len(df) < 2:
        print(f"  ⚠️ {it['name']}({it['sina_name']}) 数据不足 ({len(df) if df is not None else 0}行)")
        return records

    df = df.dropna().reset_index(drop=True)
    if days and len(df) > days:
        df = df.tail(days)

    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        td = row["date"]
        if not isinstance(td, date):
            td = date.fromisoformat(str(td)[:10])
        close = _r(row["close"])
        prev = _r(df.iloc[i - 1]["close"]) if i > 0 else close
        change = (close / prev - 1) * 100 if (prev not in (None, 0)) else 0.0
        # 20 个交易日前的收盘（用于中期趋势）
        c20 = _r(df.iloc[i - 21]["close"]) if i >= 21 else None
        records.append({
            "bench_code": it["code"],
            "bench_name": it["name"],
            "trade_date": td,
            "update_time": datetime.combine(td, datetime.min.time()),
            "last_price": close,
            "prev_close": prev,
            "change_pct": _r(change),
            "close_20d_ago": c20,
        })
    return records


def main():
    only = None
    days = 0
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
        elif a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass

    targets = [t for t in SINA_TARGETS if (only is None or t["code"] == only)]
    if not targets:
        print(f"未找到匹配的指数: {only}")
        return

    from db import get_conn, bulk_upsert

    total = 0
    for idx, it in enumerate(targets):
        print(f"\n── {it['name']}({it['code']}) [{it['sina_name']}] ──")
        recs = collect_one(it, days=days)
        if recs:
            try:
                with get_conn() as conn:
                    bulk_upsert(conn, "daily_benchmark", recs,
                                conflict_cols=["bench_code", "trade_date"])
                print(f"  ✅ 写入 {len(recs)} 条  ({recs[0]['trade_date']} ~ {recs[-1]['trade_date']})")
                total += len(recs)
            except Exception as e:
                print(f"  ❌ 写入失败: {type(e).__name__}: {e}")
        else:
            print("  (无数据)")
        if idx < len(targets) - 1:
            time.sleep(1)  # 新浪源轻量限流

    print(f"\n全部完成，累计写入 {total} 条历史记录")


if __name__ == "__main__":
    main()
