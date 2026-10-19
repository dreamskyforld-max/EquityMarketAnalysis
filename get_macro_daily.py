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
    # 股息利差（§4.1 股债性价比）：股息是股东**实得现金流**，与债券票息同质 → 可比
    ("CN.DIV_SPREAD", "股息利差-中证全指股息率−国债10Y", "CN.IDX_DIV_YIELD", "CN.BOND_10Y",
     "pp", "中证全指股息率 − 国债 10Y：保守型估值锚；为负=「买股不如买债」。"
           "⚠ 源仅回最近 20 个交易日 → 本序列只有逐日累积的短历史，"
           "**长历史口径见 CN.DIV_SPREAD_LG**"),
    ("CN.DIV_SPREAD_LG", "股息利差-沪深等权股息率(乐咕)−国债10Y", "CN.DIV_YIELD_LG", "CN.BOND_10Y",
     "pp", "乐咕乐股沪/深 A 股股息率等权合成 − 国债 10Y：**长历史口径**（2006-03 起，受中债10Y "
           "起点限制）。⚠ 与中证全指口径的 CN.DIV_SPREAD **不可拼接**（两源水平差 0.4~0.7pp）——"
           "采集层两口径并存，指标层选其一"),
    ("CN.GRADE_SPREAD_AA_AAA_5Y", "等级利差-AA−AAA 5Y", "CN.MTN_AA_5Y", "CN.MTN_AAA_5Y",
     "pp", "**等级利差**（AA 中票 − AAA 中票）：同期限、仅信用等级不同 → 纯风险偏好读数，"
           "走阔=风险偏好收缩（比绝对信用利差更少受无风险利率干扰）"),
    # —— 低评级档（AA−，4.2「高收益债利差」的可得近似；源仅回最近 3 个交易日 → 逐日累积）——
    ("CN.CREDIT_SPREAD_MTN_AA_MINUS_5Y", "信用利差-中短期票据AA− 5Y",
     "CN.MTN_AA_MINUS_5Y", "CN.BOND_5Y",
     "pp", "AA− 级中短票 5Y − 国债 5Y：**本表最低评级** → 最接近「高收益债利差」，"
           "对违约担忧与流动性收缩最敏感"),
    ("CN.GRADE_SPREAD_AA_MINUS_AA_5Y", "等级利差-AA−−AA 5Y",
     "CN.MTN_AA_MINUS_5Y", "CN.MTN_AA_5Y",
     "pp", "AA− 中票 − AA 中票：同期限仅差半档评级 → 低评级段的**纯风险偏好读数**；"
           "走阔=资金向高等级避险（比绝对利差更少受无风险利率干扰）"),
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
    # §4.1 股息利差（HK 侧）：恒指股息率是**月频快照**，美债 10Y 是日频 → 右腿用 @asof
    ("HK.DIV_SPREAD", "股息利差-恒指股息率−美债10Y", "macro:HK.DIV_YIELD_HSI",
     "bench@asof:US.DGS10",
     "pp", "恒生指数股息率（乐咕，月频）− 美债 10Y（取该月末或之前最近值）：港股相对美债的"
           "现金回报，为负=「买港股不如买美债」。⚠ 美债腿用 **asof** 而非严格同日——月末常落在"
           "周末/美国假期，严格同日会丢掉约 30% 的月份（实测 346 → 242）"),
]

# ── 美股标的的可得时点（PIT）───────────────────────────────────────────────
# 美东交易日 T 的收盘，在北京时间 **T+1 上午**才可得：
#   夏令时 16:00 ET = 04:00 北京(T+1)；冬令时 = 05:00 北京(T+1)；FRED 再滞后约 15 分钟。
#   取 06:00 保守（宁可晚一小时，不可早一小时）。
# ⚠ 只适用于 `US.*` 基准；A 股/港股/中债/在岸汇率都是本地市场、当日盘后即可得，
#   若误用于它们会把整段 PIT 无端推后一天。
# ⚠ 这是**口径修正**：修前 `CN.US_SPREAD_10Y` / `US.TERM_SPREAD_10Y_2Y` 的 100% 行都记成
#   同日可得（前视 1 天）。修正后其「可见日」整体后移 1 天 —— 依赖它们的
#   `MACRO.CN_US_SPREAD` / `MACRO.US_TERM_SPREAD` 历史值会相应平移。
US_BENCH_PREFIX = "US."
US_BENCH_LAG_DAYS = 1
US_BENCH_AVAIL_HOUR = 6

# 派生序列的 freq / lag_days 覆盖（默认 day / 0）。以下两种情况须显式声明：
#   · 驱动腿是**月频**（freq=month）；
#   · 两腿中较晚的可得时点**晚于当日**（lag_days>0，即 release_time 落在 period_date 之后）。
#   ⚠ 单一真相源：`_specs()`（播种 meta）与派生函数（写 fact）都读它 —— 且
#     `apply_series_defs` 会用 **meta** 覆盖行上的 freq/lag_days，故不能只在一处声明
#     （否则 fact 被 meta 的旧值改回去，实测踩过）。
#   ⚠ `_specs()` 无 DB 连接、查不到两腿的实际可得时点，这正是必须人工声明的原因；
#     若将来忘了声明，fact 的 release_time 仍是对的（真值在 fact 上），只是 meta 的
#     lag_days 会失真 —— 它当前**只作文档**（派生序列不会被采集器重采）。
DERIVED_OVERRIDE = {
    # 中证全指口径（股息腿 T+1、债腿 T）→ GREATEST 后利差也是 T+1 才可见
    "CN.DIV_SPREAD": dict(lag_days=1),
    "CN.DIV_SPREAD_LG": dict(lag_days=1),             # 同上（乐咕股息腿 T+1）
    "HK.DIV_SPREAD": dict(freq="month", lag_days=1),  # 恒指股息率月频 + T+1
}


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
        ov = DERIVED_OVERRIDE.get(code, {})
        specs.append(dict(series_code=code, series_name=name, unit=unit,
                          freq=ov.get("freq", "day"), market="CN",
                          source="derived:get_macro_daily",
                          lag_days=ov.get("lag_days", 0), remark=remark,
                          collect_params={"kind": "derived", "left": a, "right": b}))
    for code, name, a, b, unit, remark in CROSS_DERIVED:
        ov = DERIVED_OVERRIDE.get(code, {})
        specs.append(dict(series_code=code, series_name=name, unit=unit,
                          freq=ov.get("freq", "day"),
                          market=a.split(":", 1)[1][:2],
                          source="derived:get_macro_daily:cross",
                          lag_days=ov.get("lag_days", 0), remark=remark,
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
            """SELECT x.period_date, (x.value - y.value) AS v,
                      GREATEST(x.release_time, y.release_time) AS rel
               FROM regime.macro_series x JOIN regime.macro_series y
                 ON x.period_date = y.period_date AND x.revision=0 AND y.revision=0
               WHERE x.series_code=%s AND y.series_code=%s ORDER BY 1""",
            conn, params=[a, b])
        n = 0
        for _, r in df.iterrows():
            if r["v"] is None or pd.isna(r["v"]):
                continue
            dd = r["period_date"]
            # release_time = **双腿中较晚的可得时点** —— 利差只有在两条腿都拿得到时才算得出来。
            # ⚠ 不能一律记 period_date 18:00：旧派生的两条腿都是当日中债曲线（同一天 18:00），
            #   记当日尚可；但 CN.DIV_SPREAD 的股息腿是 T+1 发布 → 记 T 日即**前视 1 天**。
            #   GREATEST 对旧派生是恒等变换，故该修正只影响跨发布时点的派生。
            rel = r["rel"]
            if rel is None or pd.isna(rel):
                rel = datetime.datetime.combine(dd, datetime.time(18, 0), tzinfo=TZ_CN)
            elif hasattr(rel, "to_pydatetime"):
                rel = rel.to_pydatetime()
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(r["v"]), 4), "unit": unit, "freq": "day", "market": "CN",
                "source": "derived:get_macro_daily",
                "release_time": rel,
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
    """跨表派生（macro_series ↔ daily_benchmark）：中美利差、美债期限利差、恒指股息利差。

    口径（默认）：按**同日**做差（两源都是交易日序列，取交集；任一缺失则该日不产出，
    不做 ffill —— 利差是当日市场定价，用陈旧值拼接会造假信号）。

    **asof 模式**（spec 形如 `bench@asof:US.DGS10`）：带 `@asof` 的腿按「≤ 另一腿日期 的最近
    可得值」取值，另一腿的日期为驱动。用于「一条腿是**周期性快照**」的派生 —— 月频快照常落在
    周末/他国假期，严格同日会丢掉约 30% 的月份（实测 HK 股息率 346 → 242）。日频×日频**必须**
    保持严格同日，不得滥用。

    `release_time` = **两腿中较晚的可得时点**（利差只有在两条腿都拿得到时才算得出来；
    宏观腿取 macro_series.release_time，基准腿按其收盘日 18:00 —— 后者对美股标的是乐观的，
    详见下面 ⚠）。`freq` 取驱动腿的频率（月频驱动 → 月频派生）。
    """
    import bisect
    import pandas as pd

    def _load(spec: str):
        """spec → ({(date): (value, release_time)}, asof)。kind 可带 @asof 后缀。"""
        head, code = spec.split(":", 1)
        kind, _, flag = head.partition("@")
        if kind == "macro":
            df = pd.read_sql_query(
                """SELECT period_date, value, release_time FROM regime.macro_series
                   WHERE series_code=%s AND revision=0 ORDER BY period_date""", conn, params=[code])
        else:
            df = pd.read_sql_query(
                """SELECT trade_date AS period_date, last_price AS value FROM daily_benchmark
                   WHERE bench_code=%s ORDER BY trade_date""", conn, params=[code])
        out = {}
        for _, r in df.iterrows():
            if r["value"] is None or pd.isna(r["value"]):
                continue
            # 基准腿的可得时点：本地市场（A 股/港股/中债/在岸汇率）当日盘后 18:00 可得；
            #   **美股标的**的收盘在北京时间次日凌晨才可得 → 按 T+1 06:00 记（见常量注释）。
            rel = r["release_time"] if "release_time" in df.columns else None
            if rel is None or pd.isna(rel):
                if kind == "bench" and code.startswith(US_BENCH_PREFIX):
                    rel = datetime.datetime.combine(
                        r["period_date"] + datetime.timedelta(days=US_BENCH_LAG_DAYS),
                        datetime.time(US_BENCH_AVAIL_HOUR), tzinfo=TZ_CN)
                else:
                    rel = datetime.datetime.combine(r["period_date"], datetime.time(18, 0), tzinfo=TZ_CN)
            elif hasattr(rel, "to_pydatetime"):
                rel = rel.to_pydatetime()
            out[r["period_date"]] = (float(r["value"]), rel)
        return out, flag == "asof"

    def _asof(keys: list, d):
        i = bisect.bisect_right(keys, d) - 1
        return keys[i] if i >= 0 else None

    rows = []
    for code, name, left, right, unit, remark in CROSS_DERIVED:
        p = (defs.get(code) or {}).get("params") or {}
        left, right = p.get("left", left), p.get("right", right)
        la, la_asof = _load(left)
        rb, rb_asof = _load(right)
        if la_asof and rb_asof:
            raise ValueError(f"{code}: 两腿不能同时标 @asof（无驱动腿）")
        pairs = []
        if la_asof or rb_asof:
            keys_l, keys_r = sorted(la), sorted(rb)
            for dd in (keys_r if la_asof else keys_l):
                dl = _asof(keys_l, dd) if la_asof else dd
                dr = _asof(keys_r, dd) if rb_asof else dd
                if dl is None or dr is None:
                    continue
                pairs.append((dd, la[dl][0] - rb[dr][0], max(la[dl][1], rb[dr][1])))
        else:
            for dd in sorted(set(la) & set(rb)):
                pairs.append((dd, la[dd][0] - rb[dd][0], max(la[dd][1], rb[dd][1])))
        freq = DERIVED_OVERRIDE.get(code, {}).get("freq", "day")   # 见常量注释
        for dd, v, rel in pairs:
            rows.append({
                "series_code": code, "series_name": name, "period_date": dd,
                "value": round(float(v), 4), "unit": unit, "freq": freq,
                "market": code.split(".")[0],
                "source": "derived:get_macro_daily:cross",
                "release_time": rel,
                "revision": 0, "extra": {"expr": f"{left} - {right}"},   # remark 已上提到 meta
                "updated_at": datetime.datetime.now(TZ_CN),
            })
        print(f"    · {code}: {len(pairs)} 条（跨表派生 {left} − {right}）")
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
