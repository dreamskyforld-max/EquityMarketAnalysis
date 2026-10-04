#!/usr/bin/env python3
"""中债收益率曲线采集 —— 写入 regime.macro_series。

数据源：AKShare `bond_china_yield`（中国债券信息网 中债收益率曲线，官方口径）
  · 一次调用返回 **3 条曲线** × 8 个期限（3月/6月/1年/3年/5年/7年/10年/30年）：
      中债国债收益率曲线                 → CN.BOND_*
      中债中短期票据收益率曲线(AAA)       → CN.MTN_AAA_*      （信用债，算信用利差用）
      中债商业银行普通债收益率曲线(AAA)   → CN.BANK_AAA_*     （信用债，对照用）
  · 单次查询跨度须 < 1 年 → 全历史回填按「自然年」分段（2010 起实测有数据，2002 窗口为空）。
  · 用途：A 股 ERP 分母（CN.BOND_10Y）、期限利差（10Y-1Y）、信用利差（MTN_AAA - BOND，计算层算差值）。

PIT 约定：本表为当日收盘即得的市场数据，release_time 记为当日 18:00(+08)（保守 EOD，防同日前视）。

用法:
    python3 get_cn_bond.py                          # 增量（最近 30 天）
    python3 get_cn_bond.py --backfill               # 全历史回填（2005 起逐年，跳过空年份）
    python3 get_cn_bond.py --backfill --start 2005-01-01
"""
import argparse
import datetime
import sys
import time

from db import get_conn, bulk_upsert

SOURCE = "akshare:bond_china_yield"
TABLE = "regime.macro_series"
CONFLICT = ["series_code", "period_date", "revision"]

# 曲线名称 → series_code 前缀
CURVES = {
    "中债国债收益率曲线": ("CN.BOND", "国债"),
    "中债中短期票据收益率曲线(AAA)": ("CN.MTN_AAA", "中短期票据(AAA)"),
    "中债商业银行普通债收益率曲线(AAA)": ("CN.BANK_AAA", "商业银行普通债(AAA)"),
}
# 期限列 → 短码
TENORS = {"3月": "3M", "6月": "6M", "1年": "1Y", "3年": "3Y",
          "5年": "5Y", "7年": "7Y", "10年": "10Y", "30年": "30Y"}

TZ_CN = datetime.timezone(datetime.timedelta(hours=8))


def _to_rows(df) -> list:
    """DataFrame → regime.macro_series 记录列表（跳过 NaN 值）。"""
    import pandas as pd

    rows = []
    for _, r in df.iterrows():
        curve = str(r.get("曲线名称", "")).strip()
        prefix_info = CURVES.get(curve)
        if not prefix_info:
            continue
        prefix, label = prefix_info
        d = r.get("日期")
        if not isinstance(d, datetime.date):
            d = datetime.date.fromisoformat(str(d)[:10])
        release = datetime.datetime.combine(d, datetime.time(18, 0), tzinfo=TZ_CN)
        for ten_col, ten_code in TENORS.items():
            if ten_col not in df.columns:
                continue
            v = r.get(ten_col)
            if v is None or pd.isna(v):
                continue
            rows.append({
                "series_code": f"{prefix}_{ten_code}",
                "series_name": f"中债{label}收益率曲线-{ten_col}",
                "period_date": d,
                "value": round(float(v), 4),
                "unit": "pct",
                "freq": "day",
                "market": "CN",
                "source": SOURCE,
                "release_time": release,
                "revision": 0,
                # 显式刷新 updated_at：ON CONFLICT DO UPDATE 不会重放列默认值，
                # 监控（monitor_table_config.time_column=updated_at）依赖它反映「采集在跑」。
                "updated_at": datetime.datetime.now(TZ_CN),
            })
    return rows


def _fetch(start: datetime.date, end: datetime.date) -> list:
    """拉取 [start, end] 区间（调用方保证跨度 < 1 年）。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    df = ak.bond_china_yield(start_date=start.strftime("%Y%m%d"),
                             end_date=end.strftime("%Y%m%d"))
    if df is None or len(df) == 0:
        return []
    return _to_rows(df)


def _save(rows: list) -> int:
    if not rows:
        return 0
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT, skip_null_updates=True)
    return len(rows)


def run(codes=None, ctx=None, days: int = 30):
    """采集入口：增量拉取最近 days 天（默认 30，覆盖长假 + 源延迟）。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    rows = _fetch(start, end)
    n = _save(rows)
    print(f"  ✅ 增量 {start} ~ {end}: 写入 {n} 条")
    return n


def backfill(start_date: datetime.date, end_date: datetime.date = None) -> int:
    """全历史回填：按自然年分段（单次跨度<1年），空年份自动跳过。"""
    end_date = end_date or datetime.date.today()
    total, year = 0, start_date.year
    while year <= end_date.year:
        s = max(start_date, datetime.date(year, 1, 1))
        e = min(end_date, datetime.date(year, 12, 31))
        try:
            rows = _fetch(s, e)
        except Exception as exc:
            print(f"  ❌ {year}: {type(exc).__name__}: {exc}")
            rows = []
        if rows:
            total += _save(rows)
            print(f"  ✅ {year}: {len(rows)} 条（{rows[0]['period_date']} ~ {rows[-1]['period_date']}）")
        else:
            print(f"  ⚠️ {year}: 无数据（源未覆盖）")
        time.sleep(0.5)
        year += 1
    print(f"\n  回填完成，累计 {total} 条")
    return total


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="全历史回填（按年分段）")
    ap.add_argument("--start", default="2005-01-01", help="回填起始日（默认 2005-01-01，实测源最早约 2010）")
    args = ap.parse_args()

    print("=" * 60)
    print("中债收益率曲线采集 → regime.macro_series")
    print("=" * 60)
    if args.backfill:
        backfill(datetime.date.fromisoformat(args.start))
    else:
        run()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT split_part(series_code, '_', 1) AS ns, count(*),
                                  min(period_date), max(period_date),
                                  count(DISTINCT series_code)
                           FROM regime.macro_series
                           WHERE source = %s GROUP BY 1 ORDER BY 1""", (SOURCE,))
            print("\n── 库内覆盖核对 ──")
            for ns, cnt, mn, mx, nser in cur.fetchall():
                print(f"  {ns:14s} rows={cnt:>6,}  series={nser}  {mn} ~ {mx}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
