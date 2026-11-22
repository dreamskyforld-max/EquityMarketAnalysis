#!/usr/bin/env python3
"""申万行业指数行情采集（日常增量）—— 写入 daily_benchmark（复用现有基准表，不建新表）。

数据源：AKShare `index_hist_sw(symbol, period='day')`（申万宏源指数，官方口径）
  · 覆盖：从 regime.sw_industry_classification 读全部行业代码（一级31/二级131/三级336）；
  · 用途（第④层行业配置）：行业 RS / RRG / 行业动量 / 行业内宽度基准 / 行业估值对照。

职责（单一职责）：只做**日常增量**行情采集；首次全量回填在
  `backfill_sw_industry_classification.py`（独立 unit 手工跑，本脚本不含 --full）。

命名与落库约定：
  · bench_code = `CN.SW801010` 形式（daily_benchmark 命名空间扩展，不建新表）；
  · bench_name = `申万-农林牧渔`；
  · last_price=收盘, volume=成交量, turnover=成交额（申万源单位：成交量/额均为「亿」量级，
    与 A 股个股口径不同，行业层内部纵向比较可用，跨表与个股混算需注意）。
  · UNIQUE(bench_code, trade_date) 幂等 upsert，可重复跑。

用法:
    python3 get_sw_industry_quotes.py                 # 增量更新全部行业（日常/调度默认）
    python3 get_sw_industry_quotes.py --only 801010   # 单个行业
    python3 get_sw_industry_quotes.py --list          # 只打印行业清单

调度（market_scheduler.GLOBAL_TASKS「申万行业指数」18:10）：调度器在进程内调用
`run(codes, ctx=...)`，**不传命令行参数** → incremental 恒为 True（见 run 文档）。
"""
import argparse
import datetime
import sys
import time

from db import get_conn, bulk_upsert, clean_ohlc

SOURCE = "akshare:index_hist_sw"
TABLE = "daily_benchmark"
CONFLICT = ["bench_code", "trade_date"]

def _load_sw_codes() -> dict:
    """从 regime.sw_industry_classification 读全部行业 {code: name}（一/二/三级）。

    ⚠ 不再写死 SW_L1 常量（名实不符）：分类清单以层级表为单一真相源。
    若层级表为空，说明 get_sw_industry_classification.py 未跑，直接报错而不静默降级。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sw_code, sw_name FROM regime.sw_industry_classification ORDER BY level, sw_code")
            rows = cur.fetchall()
    if not rows:
        raise RuntimeError("regime.sw_industry_classification 为空：请先跑 get_sw_industry_classification.py")
    return dict(rows)


def _rows_for(code: str, name: str) -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    df = ak.index_hist_sw(symbol=code, period="day")
    if df is None or len(df) == 0:
        return []

    df = df.dropna(subset=["收盘"]).reset_index(drop=True)
    closes = pd.to_numeric(df["收盘"], errors="coerce").tolist()
    rows = []
    for i, r in df.iterrows():
        d = r["日期"]
        d = d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
        close = closes[i]
        if close is None or pd.isna(close):
            continue
        prev = closes[i - 1] if i > 0 else None
        rec = {
            "bench_code": f"CN.SW{code}",
            "bench_name": f"申万-{name}",
            "trade_date": d,
            "update_time": datetime.datetime.combine(d, datetime.time.min),
            "last_price": round(float(close), 4),
            "prev_close": round(float(prev), 4) if prev and not pd.isna(prev) else None,
            "change_pct": round((float(close) / float(prev) - 1) * 100, 4)
            if prev and not pd.isna(prev) and float(prev) != 0 else None,
            "close_20d_ago": round(float(closes[i - 20]), 4)
            if i >= 20 and not pd.isna(closes[i - 20]) else None,
        }
        # 源表含「开盘/最高/最低」列（index_hist_sw 官方口径）；缺失或自相矛盾则写 NULL
        rec["open_price"], rec["high_price"], rec["low_price"] = clean_ohlc(
            r.get("开盘"), r.get("最高"), r.get("最低"))
        vol, to = r.get("成交量"), r.get("成交额")
        if vol is not None and not pd.isna(vol):
            rec["volume"] = int(float(vol))
        if to is not None and not pd.isna(to):
            rec["turnover"] = float(to)
        rows.append(rec)
    return rows


def _last_dates() -> dict:
    """各申万系列的库内最新交易日：{bench_code: date}（增量模式用）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bench_code, max(trade_date) FROM daily_benchmark "
                        "WHERE bench_code LIKE 'CN.SW%' GROUP BY 1")
            return {r[0]: r[1] for r in cur.fetchall()}


INCREMENTAL_LOOKBACK = 3   # 增量模式回看天数：容错源侧迟到/修订（幂等 upsert，重复写无害）


def run(codes=None, ctx=None, only: str = None):
    """采集入口：行业指数**日常增量**更新（幂等）。codes 非空时按行业代码过滤。

    调度器用 `collector_runtime.run_module(script, codes)` 进程内调用
    `run(codes, ctx=...)`，传不了命令行参数，故只做增量（全量回填在独立脚本）。

    增量语义：源 `index_hist_sw` 不支持日期参数、只能整段拉回，故仍全量请求，
    但**只 upsert 库内最新日 −3 天之后的尾部** —— 每个行业全量拉回、只写尾部，
    保留源侧迟到/修订的自愈能力。
    """
    sw_map = _load_sw_codes()
    targets = [only] if only else list(sw_map.keys())
    last_map = _last_dates()
    total = 0
    for code in targets:
        name = sw_map.get(code)
        if not name:
            print(f"  ⚠️ {code}: 不在申万分类层级表，跳过")
            continue
        try:
            rows = _rows_for(code, name)
        except Exception as e:
            print(f"  ❌ {code} {name}: {type(e).__name__}: {e}")
            continue
        if not rows:
            print(f"  ⚠️ {code} {name}: 空数据")
            continue
        last = last_map.get(f"CN.SW{code}")
        if last:
            floor = last - datetime.timedelta(days=INCREMENTAL_LOOKBACK)
            rows = [r for r in rows if r["trade_date"] >= floor]
        with get_conn() as conn:
            bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT)
        total += len(rows)
        tail = f"  {rows[0]['trade_date']} ~ {rows[-1]['trade_date']}" if rows else ""
        print(f"  ✅ {code} {name}: {len(rows)} 行{tail}")
        time.sleep(0.3)
    print(f"\n  累计写入 {total} 行")
    return total


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="单个行业代码，如 801010")
    ap.add_argument("--list", action="store_true", help="只打印行业清单")
    args = ap.parse_args()

    sw_map = _load_sw_codes()
    if args.list:
        for c, n in sw_map.items():
            print(f"  {c}  {n}")
        return 0

    print("=" * 60)
    print(f"申万行业指数增量更新（{len(sw_map)} 个，来自分类层级表）→ daily_benchmark")
    print("=" * 60)
    run(only=args.only)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT bench_code), count(*),
                                  min(trade_date), max(trade_date)
                           FROM daily_benchmark WHERE bench_code LIKE 'CN.SW%'""")
            n_series, cnt, mn, mx = cur.fetchone()
            print(f"\n── 库内覆盖核对 ──\n  CN.SW* 序列 {n_series} 个，共 {cnt:,} 行  {mn} ~ {mx}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
