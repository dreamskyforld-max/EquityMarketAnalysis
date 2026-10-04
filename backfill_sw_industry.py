#!/usr/bin/env python3
"""申万一级行业指数回填 —— 写入 daily_benchmark（复用现有基准表，不建新表）。

数据源：AKShare `index_hist_sw(symbol, period='day')`（申万宏源指数，官方口径）
  · 实测深度：示例 801010 自 **1999-12-30** 起 6465 行 → 31 个一级行业可全量回填，
    覆盖 2015/2018/2021/2024 全部牛熊（满足"至少 2015 起"要求）。
  · 用途（第④层行业配置）：行业 RS / RRG / 行业动量 / 行业内宽度基准 / 行业估值对照。

命名与落库约定：
  · bench_code = `CN.SW801010` 形式（daily_benchmark 命名空间扩展，不建新表）；
  · bench_name = `申万-农林牧渔`；
  · last_price=收盘, volume=成交量, turnover=成交额（申万源单位：成交量/额均为「亿」量级，
    与 A 股个股口径不同，行业层内部纵向比较可用，跨表与个股混算需注意）。
  · UNIQUE(bench_code, trade_date) 幂等 upsert，可重复跑。

用法:
    python3 backfill_sw_industry.py                 # 增量更新全部 31 个一级行业（日常/调度默认）
    python3 backfill_sw_industry.py --full          # 全量回填（首次建库/大修后）
    python3 backfill_sw_industry.py --only 801010   # 单个行业
    python3 backfill_sw_industry.py --list          # 只打印行业清单

调度（market_scheduler.GLOBAL_TASKS「申万行业指数」18:10）：调度器在进程内调用
`run(codes, ctx=...)`，**不传命令行参数** → incremental 必须默认为 True（见 run 文档）。
"""
import argparse
import datetime
import sys
import time

from db import get_conn, bulk_upsert

SOURCE = "akshare:index_hist_sw"
TABLE = "daily_benchmark"
CONFLICT = ["bench_code", "trade_date"]

# 申万一级行业（2021 版分类，31 个）
SW_L1 = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801110": "家用电器", "801120": "食品饮料", "801130": "纺织服饰",
    "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产", "801200": "商贸零售", "801210": "社会服务", "801230": "综合",
    "801710": "建筑材料", "801720": "建筑装饰", "801730": "电力设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801780": "银行",
    "801790": "非银金融", "801880": "汽车", "801890": "机械设备", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}


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


def run(codes=None, ctx=None, only: str = None, incremental: bool = True):
    """采集入口：回填/更新行业指数（幂等）。codes 非空时按 8010xx 代码过滤。

    **incremental 默认 True**：调度器用 `collector_runtime.run_module(script, codes)`
    在进程内调用 `run(codes, ctx=...)`，**传不了命令行参数**，所以日常默认必须是增量。
    首次全量回填用 CLI 的 `--full`（或直接调 run(incremental=False)）。

    增量语义：源 `index_hist_sw` 不支持日期参数、只能整段拉回，故仍全量请求，
    但**只 upsert 库内最新日 −3 天之后的尾部** —— 31 个行业 × 6465 行
    的全量写入（约 20 万行）压到数十行，同时保留源侧迟到/修订的自愈能力。
    """
    targets = [only] if only else list(SW_L1.keys())
    last_map = _last_dates() if incremental else {}
    total = 0
    for code in targets:
        name = SW_L1.get(code)
        if not name:
            print(f"  ⚠️ {code}: 不在申万一级清单，跳过")
            continue
        try:
            rows = _rows_for(code, name)
        except Exception as e:
            print(f"  ❌ {code} {name}: {type(e).__name__}: {e}")
            continue
        if not rows:
            print(f"  ⚠️ {code} {name}: 空数据")
            continue
        if incremental:
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
    ap.add_argument("--full", action="store_true",
                    help="全量回填（首次建库用）；默认增量：只 upsert 库内最新日−3 天之后的尾部")
    args = ap.parse_args()

    if args.list:
        for c, n in SW_L1.items():
            print(f"  {c}  {n}")
        return 0

    print("=" * 60)
    print(f"申万一级行业指数{'全量回填' if args.full else '增量更新'}（{len(SW_L1)} 个）→ daily_benchmark")
    print("=" * 60)
    run(only=args.only, incremental=not args.full)

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
