#!/usr/bin/env python3
"""乐咕乐股股息率采集（**长历史口径**）—— 写入 regime.macro_series。

数据源：AKShare 乐咕乐股（legulegu）
  · `stock_a_gxl_lg(symbol)`  A 股股息率，symbol ∈ {上证A股, 深证A股, 创业板, 科创板}
    → **2005-01 起日频、一次调用返回全历史**（5288 行）
  · `stock_hk_gxl_lg()`       恒生指数股息率 → 1973-05 起**月频**（642 行）

用途：给 market_profile §4.1「股息率 − 国债收益率（股息利差）」提供**长历史**。
  原中证指数源（见 get_cn_index_dividend）只回**最近 20 个交易日** → 无法回填、断档即
  永久缺失；本源**一次取全历史** → 既补上 2005 年以来的历史，也彻底消除该风险。

落库序列：
    CN.DIV_YIELD_SH    上证A股股息率(乐咕乐股)          pct   day    2005-01 起
    CN.DIV_YIELD_SZ    深证A股股息率(乐咕乐股)          pct   day    2005-01 起
    CN.DIV_YIELD_LG    沪深A股股息率(乐咕乐股,等权合成)  pct   day    2005-01 起 ← 主口径
    HK.DIV_YIELD_HSI   恒生指数股息率(乐咕乐股)         pct   month  1973-05 起

⚠ **与中证指数口径不可拼接**（重叠 15 个交易日实测）：中证全指比乐咕**深证**低 0.07pp、
  比乐咕**上证**低 0.72pp —— 样本/加权不同（上证A股由大盘价值股主导，股息率天然更高）。
  故两口径各自独立成序列；指标层用哪一条由指标层声明，采集层不做取舍。
⚠ **等权合成** (沪+深)/2：因无长历史市值权重（`CN.MKT_CAP_*` 仅 2022 起）。等权与市值
  加权的差约 0.07pp（按当前市值比估算），对慢变估值锚可忽略；`collect_params.expr` 已记录。
⚠ 恒指股息率源早期含 0.0（1973~1980 年代），非真实读数 → **过滤 <= 0**。

PIT 约定：release_time = 观测日 **+1 天** 18:00(+08)。与 get_cn_index_dividend 同口径：
  无法确证乐咕是「T 日盘后」还是「T+1 日」更新 → 按项目「宁晚勿早」取 T+1。

⚠ 全量刷新（幂等）：一次取回全历史整表 upsert，故**漏跑一天不会丢数据**。

用法:
    python3 get_dividend_yield_lg.py
"""
import argparse
import datetime
import sys

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

SOURCE = "akshare:legulegu"
SH_SYMBOL, SZ_SYMBOL = "上证A股", "深证A股"
RELEASE_LAG_DAYS = 1                      # 见文首 PIT 约定（宁晚勿早）
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# field: A 股口径取 ak.stock_a_gxl_lg 的 symbol；"_eq" = 沪/深等权合成；"hk_hsi" = 恒指
SPECS = [
    dict(code="CN.DIV_YIELD_SH", name="上证A股股息率(乐咕乐股)", field=SH_SYMBOL,
         market="CN", freq="day",
         remark="乐咕乐股「上证A股」整体股息率（口径以乐咕官网为准，未硬编码判断）；"
                "2005-01 起日频；一次取全历史 → 无断档风险"),
    dict(code="CN.DIV_YIELD_SZ", name="深证A股股息率(乐咕乐股)", field=SZ_SYMBOL,
         market="CN", freq="day",
         remark="乐咕乐股「深证A股」整体股息率；2005-01 起日频"),
    dict(code="CN.DIV_YIELD_LG", name="沪深A股股息率(乐咕乐股,等权)", field="_eq",
         market="CN", freq="day",
         remark="(上证A股 + 深证A股) / 2 等权合成 —— **长历史主口径**；因无长历史市值权重"
                "（CN.MKT_CAP_* 仅 2022 起）故用等权，与市值加权差约 0.07pp，对慢变估值锚"
                "可忽略。⚠ 与中证全指口径（CN.IDX_DIV_YIELD）水平差 0.4~0.7pp，不可拼接"),
    dict(code="HK.DIV_YIELD_HSI", name="恒生指数股息率(乐咕乐股)", field="hk_hsi",
         market="HK", freq="month",
         remark="乐咕乐股「恒生指数股息率」；1973-05 起**月频**（period_date = 月末）；"
                "源早期含 0.0（非真实读数）已过滤"),
]


def _specs() -> list:
    return [dict(series_code=s["code"], series_name=s["name"], unit="pct",
                 freq=s["freq"], market=s["market"], source=SOURCE,
                 lag_days=RELEASE_LAG_DAYS,
                 collect_params={"provider": "legulegu", "symbol": s["field"]},
                 remark=s["remark"])
            for s in SPECS]


def _defs() -> dict:
    """播种序列契约 → 读回「库里生效的定义」（库里的值可覆盖代码）。"""
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _rows_for(df, code: str, freq: str, market: str) -> list:
    """源 DataFrame（日期/股息率）→ macro_series 行（过滤 <=0，按 release_lag 记可得时点）。"""
    import pandas as pd

    out = []
    for _, r in df.iterrows():
        d = pd.to_datetime(r["日期"], errors="coerce")
        v = pd.to_numeric(r["股息率"], errors="coerce")
        if pd.isna(d) or pd.isna(v) or float(v) <= 0:      # 乐咕早期含 0.0 → 非真实读数
            continue
        dd = d.date()
        rel = datetime.datetime.combine(
            dd + datetime.timedelta(days=RELEASE_LAG_DAYS), datetime.time(18, 0), tzinfo=TZ_CN)
        out.append({
            "series_code": code, "period_date": dd, "value": round(float(v), 4),
            "freq": freq, "market": market,
            "release_time": rel, "revision": 0,
            "updated_at": datetime.datetime.now(TZ_CN),
        })
    return out


def _fetch() -> list:
    """拉取 A 股（沪/深）+ 恒指，并合成等权序列。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    rows = []
    try:
        sh = ak.stock_a_gxl_lg(symbol=SH_SYMBOL)
        sz = ak.stock_a_gxl_lg(symbol=SZ_SYMBOL)
    except Exception as e:
        print(f"    ⚠️ 乐咕 A 股股息率拉取失败: {type(e).__name__}: {str(e)[:70]}")
        sh = sz = None
    if sh is not None and len(sh):
        rows += _rows_for(sh, "CN.DIV_YIELD_SH", "day", "CN")
        print(f"    · 上证A股: {sum(1 for r in rows if r['series_code']=='CN.DIV_YIELD_SH')} 条")
    if sz is not None and len(sz):
        rows += _rows_for(sz, "CN.DIV_YIELD_SZ", "day", "CN")
        print(f"    · 深证A股: {sum(1 for r in rows if r['series_code']=='CN.DIV_YIELD_SZ')} 条")
    if sh is not None and len(sh) and sz is not None and len(sz):
        m = pd.merge(sh, sz, on="日期", suffixes=("_sh", "_sz"))
        m["股息率"] = (pd.to_numeric(m["股息率_sh"], errors="coerce")
                      + pd.to_numeric(m["股息率_sz"], errors="coerce")) / 2
        rows += _rows_for(m[["日期", "股息率"]], "CN.DIV_YIELD_LG", "day", "CN")
        print(f"    · 等权合成: {sum(1 for r in rows if r['series_code']=='CN.DIV_YIELD_LG')} 条")

    try:
        hk = ak.stock_hk_gxl_lg()
        if hk is not None and len(hk):
            rows += _rows_for(hk, "HK.DIV_YIELD_HSI", "month", "HK")
            print(f"    · 恒指: {sum(1 for r in rows if r['series_code']=='HK.DIV_YIELD_HSI')} 条")
    except Exception as e:
        print(f"    ⚠️ 恒指股息率拉取失败: {type(e).__name__}: {str(e)[:70]}")
    return rows


def _save(rows: list, defs: dict = None) -> int:
    if not rows:
        return 0
    apply_series_defs(rows, defs if defs is not None else _defs())
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
    return len(rows)


def run(codes=None, ctx=None) -> int:
    """采集入口：全量刷新（源一次返回全历史，幂等；漏跑一天不丢数据）。"""
    defs = _defs()
    rows = _fetch()
    print(f"  ✅ 乐咕股息率: 写入 {_save(rows, defs)} 条")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    print("=" * 60)
    print("乐咕乐股股息率采集（长历史口径）→ regime.macro_series")
    print("=" * 60)
    run()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT series_code, count(*), min(period_date), max(period_date)
                           FROM regime.macro_series
                           WHERE series_code LIKE '%DIV_YIELD%' GROUP BY 1 ORDER BY 1""")
            print("\n── regime.macro_series（*DIV_YIELD*）──")
            for r in cur.fetchall():
                print(f"  {r[0]:20s} {r[1]:>5} 行   {r[2]} ~ {r[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
