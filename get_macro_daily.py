#!/usr/bin/env python3
"""宏观日频包采集 —— 写入 regime.macro_series（HIBOR / Shibor / 商品 + 利差派生）。

覆盖：
    港币流动性  HK.HIBOR_ON / HK.HIBOR_1W / HK.HIBOR_1M / HK.HIBOR_3M（1996 起）
    离岸人民币  HK.HIBOR_CNH_1M
    境内流动性  CN.SHIBOR_ON / CN.SHIBOR_1W / CN.SHIBOR_1M / CN.SHIBOR_3M
    商品        CMDTY.COPPER(沪铜) / CMDTY.GOLD(沪金) / CMDTY.OIL(原油，2018 起)
    利差派生    CN.CREDIT_SPREAD_MTN_AAA_10Y / _5Y（中短期票据 AAA − 国债）
                CN.BANK_SPREAD_AAA_10Y（商业银行普通债 AAA − 国债）
                CN.TERM_SPREAD_10Y_1Y（国债 10Y − 1Y）
    ——利差均由 `get_cn_bond.py` 已入库的中债曲线同日做差得到，无额外数据源。

口径与 PIT：
  · 全部为「当日收盘即得」的市场数据 → release_time = 当日 18:00(+08)（保守 EOD）；
  · 拆借利率单位 %；商品为连续合约收盘价（元/吨、元/克、元/桶，见 extra.contract）；
  · 源接口一次返回全历史 → 直接整段幂等 upsert（数据量小，无需增量窗口）。

用法:
    python3 get_macro_daily.py            # 全量刷新（幂等）
    python3 get_macro_daily.py --dry-run
"""
import argparse
import datetime
import sys

from db import get_conn, bulk_upsert

TABLE = "regime.macro_series"
CONFLICT = ["series_code", "period_date", "revision"]
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# 拆借利率序列：(code, 名称, market, symbol, indicator)
RATES = [
    ("HK.HIBOR_ON", "HIBOR 隔夜", "香港银行同业拆借市场", "Hibor港币", "隔夜"),
    ("HK.HIBOR_1W", "HIBOR 1周", "香港银行同业拆借市场", "Hibor港币", "1周"),
    ("HK.HIBOR_1M", "HIBOR 1月", "香港银行同业拆借市场", "Hibor港币", "1月"),
    ("HK.HIBOR_3M", "HIBOR 3月", "香港银行同业拆借市场", "Hibor港币", "3月"),
    ("HK.HIBOR_CNH_1M", "HIBOR 离岸人民币 1月", "香港银行同业拆借市场", "Hibor人民币", "1月"),
    ("CN.SHIBOR_ON", "Shibor 隔夜", "上海银行同业拆借市场", "Shibor人民币", "隔夜"),
    ("CN.SHIBOR_1W", "Shibor 1周", "上海银行同业拆借市场", "Shibor人民币", "1周"),
    ("CN.SHIBOR_1M", "Shibor 1月", "上海银行同业拆借市场", "Shibor人民币", "1月"),
    ("CN.SHIBOR_3M", "Shibor 3月", "上海银行同业拆借市场", "Shibor人民币", "3月"),
]

# 商品连续合约：(code, 名称, 新浪代码, 单位)
COMMODITIES = [
    ("CMDTY.COPPER", "沪铜连续", "CU0", "cny_per_ton"),
    ("CMDTY.GOLD", "沪金连续", "AU0", "cny_per_gram"),
    ("CMDTY.OIL", "原油连续(上期能源)", "SC0", "cny_per_barrel"),
]

# 利差派生：(code, 名称, 被减项, 减项, 单位, 说明)
DERIVED = [
    ("CN.CREDIT_SPREAD_MTN_AAA_10Y", "信用利差-中短期票据AAA 10Y", "CN.MTN_AAA_10Y", "CN.BOND_10Y",
     "pp", "中短期票据(AAA) 10Y 收益率 − 国债 10Y（风险偏好/违约预期）"),
    ("CN.CREDIT_SPREAD_MTN_AAA_5Y", "信用利差-中短期票据AAA 5Y", "CN.MTN_AAA_5Y", "CN.BOND_5Y",
     "pp", "同上，5 年期"),
    ("CN.BANK_SPREAD_AAA_10Y", "信用利差-商业银行普通债AAA 10Y", "CN.BANK_AAA_10Y", "CN.BOND_10Y",
     "pp", "商业银行普通债(AAA) 10Y − 国债 10Y"),
    ("CN.TERM_SPREAD_10Y_1Y", "期限利差-国债 10Y−1Y", "CN.BOND_10Y", "CN.BOND_1Y",
     "pp", "国债收益率曲线陡峭度，倒挂为衰退预警"),
]


def _rate_rows() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    rows = []
    for code, name, market, symbol, indicator in RATES:
        try:
            df = ak.rate_interbank(market=market, symbol=symbol, indicator=indicator)
        except Exception as e:
            print(f"    ❌ {code}: {type(e).__name__}: {str(e)[:80]}")
            continue
        dcol = "报告日" if "报告日" in df.columns else df.columns[0]
        vcol = "利率" if "利率" in df.columns else df.columns[1]
        n, sub = 0, []
        for _, r in df.iterrows():
            v = pd.to_numeric(r.get(vcol), errors="coerce")
            d = pd.to_datetime(r.get(dcol), errors="coerce")
            if pd.isna(v) or pd.isna(d):
                continue
            dd = d.date()
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(v), 4), "unit": "pct", "freq": "day", "market": code[:2],
                "source": f"akshare:rate_interbank:{market}/{symbol}/{indicator}",
                "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"indicator": indicator}, "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
            sub.append(dd)
        if sub:
            print(f"    · {code}: {n} 条  {min(sub)} ~ {max(sub)}")
    return rows


def _commodity_rows() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    rows = []
    for code, name, sina, unit in COMMODITIES:
        try:
            df = ak.futures_zh_daily_sina(symbol=sina)
        except Exception as e:
            print(f"    ❌ {code}: {type(e).__name__}: {str(e)[:80]}")
            continue
        n = 0
        for _, r in df.iterrows():
            v = pd.to_numeric(r.get("close"), errors="coerce")
            d = pd.to_datetime(r.get("date"), errors="coerce")
            if pd.isna(v) or pd.isna(d):
                continue
            dd = d.date()
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(v), 4), "unit": unit, "freq": "day", "market": "CMDTY",
                "source": f"akshare:futures_zh_daily_sina:{sina}",
                "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"contract": sina, "field": "close"},
                "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
        if rows:
            print(f"    · {code}: {n} 条  {rows[0]['period_date']} ~ {rows[-1]['period_date']}")
    return rows


def _derived_rows(conn) -> list:
    """利差派生：从已入库的中债曲线同日做差。"""
    import pandas as pd

    rows = []
    for code, name, a, b, unit, remark in DERIVED:
        df = pd.read_sql_query(
            """SELECT x.period_date, (x.value - y.value) AS v
               FROM regime.macro_series x JOIN regime.macro_series y
                 ON x.period_date = y.period_date AND x.revision=0 AND y.revision=0
               WHERE x.series_code=%s AND y.series_code=%s ORDER BY 1""",
            conn, params=[a, b])
        n = 0
        for _, r in df.iterrows():
            if r["v"] is None or pd.isna(r["v"]):
                continue
            dd = r["period_date"]
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(r["v"]), 4), "unit": unit, "freq": "day", "market": "CN",
                "source": "derived:get_macro_daily",
                "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"expr": f"{a} - {b}", "remark": remark},
                "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
        print(f"    · {code}: {n} 条（派生）")
    return rows


def run(codes=None, ctx=None, dry_run: bool = False):
    """采集入口：全量幂等刷新。"""
    print("  ── 拆借利率 ──")
    rows = _rate_rows()
    print("  ── 商品 ──")
    rows += _commodity_rows()

    with get_conn() as conn:
        print("  ── 利差派生 ──")
        drows = _derived_rows(conn)
        if dry_run:
            print("  --dry-run：不写库")
            return len(rows) + len(drows)
        rows += drows
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT, skip_null_updates=True)
        print(f"  ✅ 写入 {len(rows)} 条")
        with conn.cursor() as cur:
            cur.execute("""SELECT split_part(series_code,'.',1) ns, count(DISTINCT series_code), count(*)
                           FROM regime.macro_series
                           WHERE source LIKE 'akshare:rate_interbank%'
                              OR source LIKE 'akshare:futures_zh_daily_sina%'
                              OR source = 'derived:get_macro_daily'
                           GROUP BY 1 ORDER BY 1""")
            for r in cur.fetchall():
                print(f"    {r[0]:6s} 序列 {r[1]} 个 / {r[2]:,} 行")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("=" * 60)
    print("宏观日频包（HIBOR/Shibor/商品/利差）→ regime.macro_series")
    print("=" * 60)
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
