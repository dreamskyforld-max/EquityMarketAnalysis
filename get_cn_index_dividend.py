#!/usr/bin/env python3
"""中证指数股息率采集（中证指数官网）—— 写入 regime.macro_series。

数据源：AKShare `stock_zh_index_value_csindex(symbol)` = 中证指数-指数估值数据。
  · 用途：补齐 market_profile §4.1「股息率 − 国债收益率（股息利差）」的数据基础
    —— 此前全市场股息率**只散落在个股层面**（`a_daily_quote.dividend_ratio_ttm` 覆盖率
    仅 0.86%、`daily_quote` 5.5%），无法聚合出可用的市场级股息率。
  · 取 **000985 中证全指** = 全市场口径（如需沪深300 口径，改 collect_params.index 即可，
    不必改代码：见 seed_series_meta / load_series_defs）。
  · ⚠ 实测（2026-10-15）：该接口只返回**最近 20 个交易日**的滚动窗口 → **无法回填历史**，
    只能逐日累积；断档即永久缺失（与 get_cn_bond 的低评级档 / 行业资金流 / 一致预期
    快照同类约束）。
  · ⚠ 估值表同时给「股息率1」「股息率2」两列，接口未暴露口径说明 → **两个都采**，
    不在代码里硬编码口径判断；派生股息利差默认用股息率1。
  · 同时采「市盈率2」（滚动 PE）：中证口径与乐咕乐股口径互为校验，成本为零。

落库序列（freq=day）：
    CN.IDX_DIV_YIELD    中证全指股息率1         pct
    CN.IDX_DIV_YIELD_2  中证全指股息率2         pct
    CN.IDX_PE_TTM       中证全指市盈率2（滚动）  x

PIT 约定：release_time = 观测日 **+1 天** 18:00(+08)。⚠ 实测（2026-10-15 16:28）当日 15:00
  收盘后源最新仍为前一日 → **无法确证是「T 日盘后」还是「T+1 日」发布**，故按项目「宁晚勿早」
  取 T+1；代价是信号晚 1 天，对慢变的估值锚无实质影响，但不取则可能按 T 日 18:00 记入而前视。

用法:
    python3 get_cn_index_dividend.py            # 增量：覆盖源窗口内全部日期（幂等）
"""
import argparse
import datetime
import sys

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

SOURCE = "akshare:stock_zh_index_value_csindex"
INDEX_CODE = "000985"                     # 中证全指 = 全市场口径
INDEX_LABEL = "中证全指"
RELEASE_LAG_DAYS = 1                      # 见文首 PIT 约定（宁晚勿早）
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# 序列定义（**采集契约**）：field = 源表取值列；登记进 macro_series_meta 后，
# 改「取值列/单位/备注」直接改库即可，不必改代码。
SPECS = [
    dict(code="CN.IDX_DIV_YIELD", name=f"{INDEX_LABEL}股息率1", unit="pct",
         field="股息率1",
         remark=f"{INDEX_LABEL}(000985) 估值表「股息率1」列；⚠ 源仅回最近 20 个交易日 → "
                f"逐日累积，断档即永久缺失；口径以中证官网估值表为准（未硬编码判断）"),
    dict(code="CN.IDX_DIV_YIELD_2", name=f"{INDEX_LABEL}股息率2", unit="pct",
         field="股息率2",
         remark=f"{INDEX_LABEL}(000985) 估值表「股息率2」列；与股息率1 并采以便复核口径，"
                f"派生股息利差默认用股息率1"),
    dict(code="CN.IDX_PE_TTM", name=f"{INDEX_LABEL}市盈率2(滚动)", unit="x",
         field="市盈率2",
         remark=f"{INDEX_LABEL}(000985) 估值表「市盈率2」列（滚动口径）；"
                f"与乐咕乐股 CN.VAL_PE_* 互为校验"),
]


def _specs() -> list:
    """SPECS → macro_series_meta 行（曲线 × 期限式的展开在这里，本采集器是字段式展开）。"""
    return [dict(series_code=s["code"], series_name=s["name"], unit=s["unit"],
                 freq="day", market="CN", source=SOURCE, lag_days=RELEASE_LAG_DAYS,
                 collect_params={"index": INDEX_CODE, "field": s["field"]},
                 remark=s["remark"])
            for s in SPECS]


def _defs() -> dict:
    """播种序列契约 → 读回「库里生效的定义」（库里的值可覆盖代码，改库即改采集）。"""
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _fetch() -> list:
    """拉取源窗口（最近 20 个交易日）→ macro_series 行（每字段一行）。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    try:
        df = ak.stock_zh_index_value_csindex(symbol=INDEX_CODE)
    except Exception as e:
        print(f"    ⚠️ {INDEX_LABEL} 估值拉取失败: {type(e).__name__}: {str(e)[:70]}")
        return []
    if df is None or len(df) == 0:
        print(f"    ⚠️ {INDEX_LABEL} 估值返回空")
        return []

    rows = []
    for _, r in df.iterrows():
        dt = pd.to_datetime(r.get("日期"), errors="coerce")
        if pd.isna(dt):
            continue
        dd = dt.date()
        for s in SPECS:
            v = pd.to_numeric(r.get(s["field"]), errors="coerce")
            if pd.isna(v):
                continue
            rel_d = dd + datetime.timedelta(days=RELEASE_LAG_DAYS)
            rows.append({
                "series_code": s["code"],
                "period_date": dd,
                "value": round(float(v), 4),
                "release_time": datetime.datetime.combine(rel_d, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0,
                "updated_at": datetime.datetime.now(TZ_CN),
            })
    return rows


def _save(rows: list, defs: dict = None) -> int:
    if not rows:
        return 0
    apply_series_defs(rows, defs if defs is not None else _defs())
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
    return len(rows)


def run(codes=None, ctx=None) -> int:
    """采集入口：源只回最近 20 个交易日 → 每次整窗 upsert（幂等），须每日跑。"""
    defs = _defs()
    rows = _fetch()
    n = _save(rows, defs)
    if rows:
        days = sorted({r["period_date"] for r in rows})
        print(f"  ✅ {INDEX_LABEL}: 写入 {n} 条（{len(days)} 个交易日，"
              f"{days[0]} ~ {days[-1]}；源仅最近 20 个交易日）")
    return n


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    print("=" * 60)
    print("中证指数股息率/估值采集 → regime.macro_series")
    print("=" * 60)
    run()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT series_code, count(*), min(period_date), max(period_date)
                           FROM regime.macro_series
                           WHERE series_code LIKE 'CN.IDX_%' GROUP BY 1 ORDER BY 1""")
            print("\n── regime.macro_series（CN.IDX_*）──")
            for r in cur.fetchall():
                print(f"  {r[0]:20s} {r[1]:>4} 行   {r[2]} ~ {r[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
