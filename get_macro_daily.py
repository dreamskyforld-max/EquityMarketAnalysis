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

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

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
    ("CN.CREDIT_SPREAD_MTN_AA_5Y", "信用利差-中短期票据AA 5Y", "CN.MTN_AA_5Y", "CN.BOND_5Y",
     "pp", "AA 级中短票 5Y − 国债 5Y（低等级信用风险的绝对定价）"),
    ("CN.GRADE_SPREAD_AA_AAA_5Y", "等级利差-AA−AAA 5Y", "CN.MTN_AA_5Y", "CN.MTN_AAA_5Y",
     "pp", "**等级利差**（AA 中票 − AAA 中票）：同期限、仅信用等级不同 → 纯风险偏好读数，"
           "走阔=风险偏好收缩（比绝对信用利差更少受无风险利率干扰）"),
]

# 通胀高频（日频）：CPI 的先行/验证序列
#   猪价源 `spot_hog_year_trend_soozhu`：**约 7.5 个月滚动窗口** → 逐日累积；
#   菜篮子源 `macro_china_vegetable_basket`：**2005-09 起的完整日频指数**（含源侧算好的同比）
PRICES = [
    ("CN.PORK_PRICE", "生猪价格(瘦肉型, 元/公斤)", "spot_hog_year_trend_soozhu", "价格"),
    ("CN.VEG_BASKET", "菜篮子批发价格指数", "macro_china_vegetable_basket", "最新值"),
    ("CN.VEG_BASKET_YOY", "菜篮子批发价格指数-近1年涨跌幅", "macro_china_vegetable_basket", "近1年涨跌幅"),
]

# 猪价源未标注单位（数值量级=元/公斤）；源窗口约 7.5 个月 → 登记进 meta.remark（不逐行重复）
PORK_REMARK = "猪价源未标注单位（数值量级=元/公斤）；源窗口约 7.5 个月"

# 跨表派生：(code, 名称, 左序列, 右序列, 单位, 说明)
#   序列写法 "macro:CODE"（regime.macro_series）/ "bench:CODE"（daily_benchmark）。
#   为什么单独一类：中美利差需要**中债曲线（macro_series）− 美债（daily_benchmark，FRED 源）**
#   跨两张表；而美债期限利差虽同表，但归属宏观口径（不进 daily_benchmark 的行情命名空间）。
CROSS_DERIVED = [
    ("CN.US_SPREAD_10Y", "中美利差-中债10Y−美债10Y", "macro:CN.BOND_10Y", "bench:US.DGS10",
     "pp", "中美利差（外资配置中国资产的机会成本）；走阔通常伴随人民币企稳/外资流入改善"),
    ("US.TERM_SPREAD_10Y_2Y", "美债期限利差 10Y−2Y", "bench:US.DGS10", "bench:US.DGS2",
     "pp", "美债曲线陡峭度；倒挂为经典衰退预警（全球流动性与风险偏好的领先信号）"),
]


def _specs() -> list:
    """五张清单 → 完整序列清单（**采集契约**），登记进 macro_series_meta。"""
    specs = []
    for code, name, market, symbol, indicator in RATES:
        specs.append(dict(series_code=code, series_name=name, unit="pct", freq="day",
                          market=code[:2],
                          source=f"akshare:rate_interbank:{market}/{symbol}/{indicator}",
                          lag_days=0,
                          collect_params={"kind": "rate", "market": market,
                                          "symbol": symbol, "indicator": indicator}))
    for code, name, sina, unit in COMMODITIES:
        specs.append(dict(series_code=code, series_name=name, unit=unit, freq="day",
                          market="CMDTY", source=f"akshare:futures_zh_daily_sina:{sina}",
                          lag_days=0,
                          collect_params={"kind": "commodity", "symbol": sina, "field": "close"}))
    for code, name, func, col in PRICES:
        specs.append(dict(series_code=code, series_name=name, freq="day", market="CN",
                          unit="index" if "指数" in name else "cny_per_kg",
                          source=f"akshare:{func}", lag_days=0,
                          # 口径备注上提到 meta（原来逐行写进 extra，8 千多行各存一份）
                          remark=(PORK_REMARK if code == "CN.PORK_PRICE" else None),
                          collect_params={"kind": "price", "func": func, "col": col}))
    for code, name, a, b, unit, remark in DERIVED:
        specs.append(dict(series_code=code, series_name=name, unit=unit, freq="day",
                          market="CN", source="derived:get_macro_daily", lag_days=0,
                          remark=remark,
                          collect_params={"kind": "derived", "left": a, "right": b}))
    for code, name, a, b, unit, remark in CROSS_DERIVED:
        specs.append(dict(series_code=code, series_name=name, unit=unit, freq="day",
                          market=a.split(":", 1)[1][:2],
                          source="derived:get_macro_daily:cross", lag_days=0, remark=remark,
                          collect_params={"kind": "cross", "left": a, "right": b}))
    return specs


def _defs() -> dict:
    """播种序列契约 → 读回「库里生效的定义」（库里的值覆盖代码，改库即改采集）。"""
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _rate_rows(defs: dict) -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    rows = []
    for code, name, market, symbol, indicator in RATES:
        p = (defs.get(code) or {}).get("params") or {}
        market = p.get("market", market)
        symbol = p.get("symbol", symbol)
        indicator = p.get("indicator", indicator)
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


def _commodity_rows(defs: dict) -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    rows = []
    for code, name, sina, unit in COMMODITIES:
        p = (defs.get(code) or {}).get("params") or {}
        sina = p.get("symbol", sina)
        field = p.get("field", "close")
        try:
            df = ak.futures_zh_daily_sina(symbol=sina)
        except Exception as e:
            print(f"    ❌ {code}: {type(e).__name__}: {str(e)[:80]}")
            continue
        n = 0
        for _, r in df.iterrows():
            v = pd.to_numeric(r.get(field), errors="coerce")
            d = pd.to_datetime(r.get("date"), errors="coerce")
            if pd.isna(v) or pd.isna(d):
                continue
            dd = d.date()
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(v), 4), "unit": unit, "freq": "day", "market": "CMDTY",
                "source": f"akshare:futures_zh_daily_sina:{sina}",
                "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"contract": sina, "field": field},
                "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
        if rows:
            print(f"    · {code}: {n} 条  {rows[0]['period_date']} ~ {rows[-1]['period_date']}")
    return rows


def _derived_rows(conn, defs: dict) -> list:
    """利差派生：从已入库的中债曲线同日做差。"""
    import pandas as pd

    rows = []
    for code, name, a, b, unit, remark in DERIVED:
        p = (defs.get(code) or {}).get("params") or {}
        a, b = p.get("left", a), p.get("right", b)
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
                "revision": 0, "extra": {"expr": f"{a} - {b}"},   # remark 已上提到 meta
                "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
        print(f"    · {code}: {n} 条（派生）")
    return rows


def _price_rows(defs: dict) -> list:
    """通胀高频价格序列（猪价 / 菜篮子指数 + 同比）：日频，幂等刷新。

    口径注意：猪价源**未标注单位**（数值量级符合元/公斤）→ 该口径备注已**上提到
    macro_series_meta.remark**（不再逐行写进 extra）；猪价源只有约 7.5 个月滚动窗口
    → 历史靠逐日累积（不像菜篮子有 2005 起的完整历史）。
    """
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    cache: dict = {}
    rows = []
    for code, name, func, col in PRICES:
        p = (defs.get(code) or {}).get("params") or {}
        func = p.get("func", func)
        col = p.get("col", col)
        if func not in cache:
            try:
                cache[func] = getattr(ak, func)()
            except Exception as e:
                print(f"    ❌ {code}: 接口 {func} 失败 {type(e).__name__}: {str(e)[:60]}")
                cache[func] = None
        df = cache[func]
        if df is None or len(df) == 0 or col not in df.columns:
            print(f"    ⚠️ {code}: 无数据或无列「{col}」")
            continue
        n = 0
        for _, r in df.iterrows():
            v = pd.to_numeric(r.get(col), errors="coerce")
            dt = pd.to_datetime(r.get("日期"), errors="coerce")
            if pd.isna(v) or pd.isna(dt):
                continue
            dd = dt.date()
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(v), 4),
                "unit": "index" if "指数" in name else "cny_per_kg",
                "freq": "day", "market": "CN", "source": f"akshare:{func}",
                "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"col": col},
                "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
        print(f"    · {code}: {n} 条")
    return rows


def _cross_derived_rows(conn, defs: dict) -> list:
    """跨表派生（macro_series ↔ daily_benchmark）：中美利差、美债期限利差。

    口径：按**同日**做差（两源都是交易日序列，取交集；任一缺失则该日不产出，
    不做 ffill —— 利差是当日市场定价，用陈旧值拼接会造假信号）。
    """
    import pandas as pd

    def _load(spec: str) -> dict:
        kind, code = spec.split(":", 1)
        if kind == "macro":
            df = pd.read_sql_query(
                """SELECT period_date, value FROM regime.macro_series
                   WHERE series_code=%s AND revision=0 ORDER BY period_date""", conn, params=[code])
        else:
            df = pd.read_sql_query(
                """SELECT trade_date AS period_date, last_price AS value FROM daily_benchmark
                   WHERE bench_code=%s ORDER BY trade_date""", conn, params=[code])
        return {r["period_date"]: float(r["value"]) for _, r in df.iterrows()
                if r["value"] is not None and not pd.isna(r["value"])}

    rows = []
    for code, name, left, right, unit, remark in CROSS_DERIVED:
        p = (defs.get(code) or {}).get("params") or {}
        left, right = p.get("left", left), p.get("right", right)
        la, rb = _load(left), _load(right)
        n = 0
        for dd in sorted(set(la) & set(rb)):
            v = la[dd] - rb[dd]
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(v), 4), "unit": unit, "freq": "day",
                "market": code.split(".")[0],
                "source": "derived:get_macro_daily:cross",
                "release_time": datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"expr": f"{left} - {right}"},   # remark 已上提到 meta
                "updated_at": datetime.datetime.now(TZ_CN),
            })
            n += 1
        print(f"    · {code}: {n} 条（跨表派生 {left} − {right}）")
    return rows


def run(codes=None, ctx=None, dry_run: bool = False):
    """采集入口：全量幂等刷新。"""
    defs = _defs()                        # 一次加载「库里生效的定义」，全部子模块共用
    print("  ── 拆借利率 ──")
    rows = _rate_rows(defs)
    print("  ── 商品 ──")
    rows += _commodity_rows(defs)
    print("  ── 通胀高频（猪价/菜篮子）──")
    rows += _price_rows(defs)

    with get_conn() as conn:
        print("  ── 利差派生 ──")
        drows = _derived_rows(conn, defs)
        drows += _cross_derived_rows(conn, defs)
        if dry_run:
            print("  --dry-run：不写库")
            return len(rows) + len(drows)
        rows += drows
        apply_series_defs(rows, defs)     # 生效的名称/单位/契约/备注贴到行 → 拆进 meta
        upsert_macro_series(conn, rows)
        print(f"  ✅ 写入 {len(rows)} 条")
        with conn.cursor() as cur:
            cur.execute("""SELECT split_part(series_code,'.',1) ns, count(DISTINCT series_code), count(*)
                           FROM regime.v_macro_series
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
