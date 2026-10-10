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

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

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


# AA 档中短期票据：**必须走独立接口** `bond_china_close_return` ——
# `bond_china_yield` 默认只返回 国债 + 中票(AAA) + 商业银行(AAA) 三条曲线，**没有 AA**。
# ⚠ 实测（2026-10-08）：该接口只返回**最近 3 个交易日**的滚动窗口
#   （传 2026-08 月窗口 → 只回 08-27~08-31；传 2025 年窗口 → 直接报错 newDateValue）
#   → **无法回填历史**，只能逐日累积；断档即永久缺失（与行业资金流/一致预期快照同类约束）。
AA_SYMBOL = "中短期票据(AA)"
AA_SOURCE = "akshare:bond_china_close_return"
AA_TENORS = {0.25: "3M", 0.5: "6M", 1.0: "1Y", 3.0: "3Y", 5.0: "5Y", 10.0: "10Y"}


def _fetch_aa(today: datetime.date) -> list:
    """AA 档中短票到期收益率（长表：日期/期限/到期收益率）→ macro_series 行。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    # 窗口必须覆盖「源最后有数据的那天」：源只保留最近 3 个交易日，且**无数据窗口会直接报错**
    # （实测长假后：窗口 10-01~10-08 → KeyError: 'newDateValue'）→ 逐步放宽回看天数重试。
    df, last_err = None, None
    for lookback in (7, 14, 30, 45, 60):
        start = today - datetime.timedelta(days=lookback)
        try:
            df = ak.bond_china_close_return(symbol=AA_SYMBOL,
                                            start_date=start.strftime("%Y%m%d"),
                                            end_date=today.strftime("%Y%m%d"))
        except Exception as e:
            last_err = e
            continue
        if df is not None and len(df):
            break
    if df is None or len(df) == 0:
        print(f"    ⚠️ AA 中票曲线无数据（回看 60 天内均失败）: "
              f"{type(last_err).__name__ if last_err else 'Empty'}: {str(last_err)[:60]}")
        return []
    rows = []
    for _, r in df.iterrows():
        ten = pd.to_numeric(r.get("期限"), errors="coerce")
        if pd.isna(ten):
            continue
        code = AA_TENORS.get(round(float(ten), 3))
        if not code:
            continue
        v = pd.to_numeric(r.get("到期收益率"), errors="coerce")
        dt = pd.to_datetime(r.get("日期"), errors="coerce")
        if pd.isna(v) or pd.isna(dt):
            continue
        dd = dt.date()
        rows.append({
            "series_code": f"CN.MTN_AA_{code}",
            "series_name": f"中债中短期票据(AA)收益率曲线-{code}",
            "period_date": dd, "value": round(float(v), 4), "unit": "pct", "freq": "day",
            "market": "CN", "source": AA_SOURCE,
            "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
            "revision": 0, "extra": {"tenor_year": round(float(ten), 3)},
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


def _specs() -> list:
    """曲线 × 期限 → 完整序列清单（**采集契约**），登记进 macro_series_meta。"""
    specs = [dict(series_code=f"{prefix}_{ten_code}",
                  series_name=f"中债{label}收益率曲线-{ten_col}",
                  unit="pct", freq="day", market="CN", source=SOURCE, lag_days=0,
                  collect_params={"curve": curve, "tenor": ten_col})
             for curve, (prefix, label) in CURVES.items()
             for ten_col, ten_code in TENORS.items()]
    specs += [dict(series_code=f"CN.MTN_AA_{code}",
                   series_name=f"中债中短期票据(AA)收益率曲线-{code}",
                   unit="pct", freq="day", market="CN", source=AA_SOURCE, lag_days=0,
                   collect_params={"curve": AA_SYMBOL, "tenor_year": yr})
              for yr, code in AA_TENORS.items()]
    return specs


def _defs() -> dict:
    """播种序列契约 → 读回「库里生效的定义」（库里的值可覆盖代码，改库即改采集）。"""
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _save(rows: list, defs: dict = None) -> int:
    if not rows:
        return 0
    # 行是循环生成的，这里统一用库登记值覆盖（名称/单位/契约/备注）
    apply_series_defs(rows, defs if defs is not None else _defs())
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
    return len(rows)


def run(codes=None, ctx=None, days: int = 30):
    """采集入口：增量拉取最近 days 天（默认 30，覆盖长假 + 源延迟）+ AA 档（源仅 3 日窗）。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    defs = _defs()                       # 一次加载生效定义，两处写入共用
    rows = _fetch(start, end)
    n = _save(rows, defs)
    print(f"  ✅ 增量 {start} ~ {end}: 写入 {n} 条")
    aa_rows = _fetch_aa(end)
    if aa_rows:
        n_aa = _save(aa_rows, defs)
        days_aa = sorted({r["period_date"] for r in aa_rows})
        print(f"  ✅ AA 中票曲线: {n_aa} 条（源仅最近 3 个交易日）{' ~ '.join(str(d) for d in days_aa[-1:])}")
    return n + (len(aa_rows) if aa_rows else 0)


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
                           FROM regime.v_macro_series
                           WHERE source = %s GROUP BY 1 ORDER BY 1""", (SOURCE,))
            print("\n── 库内覆盖核对 ──")
            for ns, cnt, mn, mx, nser in cur.fetchall():
                print(f"  {ns:14s} rows={cnt:>6,}  series={nser}  {mn} ~ {mx}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
