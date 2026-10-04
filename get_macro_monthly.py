#!/usr/bin/env python3
"""宏观月/季频包采集 —— 写入 regime.macro_series。

覆盖（第①层 宏观 + 第②层 资产配置输入）：
    货币信用  CN.SHRZGM_INC(社融增量) / CN.SHRZGM_RMB_LOAN(社融-人民币贷款)
              CN.M2_YOY / CN.M1_YOY / CN.M1_M2_SCISSOR(派生)
              CN.NEW_LOAN(新增人民币贷款) / CN.LPR_1Y / CN.LPR_5Y
    通胀价格  CN.CPI_YOY / CN.PPI_YOY / CN.PPI_CPI_SCISSOR(派生)
    经济周期  CN.PMI_MFG / CN.PMI_NONMFG / CN.IP_YOY(工业增加值) / CN.GDP_YOY / CN.GDP
    市场/实体 CN.MARKET_CAP(沪深市价总值，巴菲特指标分子) / CN.ELECTRICITY_YOY / CN.CONSUMER_CONFIDENCE
    情绪      CN.ACCOUNT_NEW(新增投资者；⚠ 源已停更于 2023-08)

口径与 PIT：
  · period_date = 统计期首日（月=当月 1 日 / 季=当季首月 1 日），对齐确定、无歧义；
  · release_time = 源带「发布时间」列时用真值（如工业增加值），否则用 period_date + lag 天
    （保守上限：月频 45 天 / 季频 100 天）——宁可偏晚，绝不前视；
  · freq: month / quarter；unit 见各序列定义；`source` 记录具体接口，便于溯源。

用法:
    python3 get_macro_monthly.py            # 全量刷新（月频数据量小，直接整表 upsert 幂等）
    python3 get_macro_monthly.py --dry-run  # 只打印不写库
"""
import argparse
import datetime
import re
import sys

from db import get_conn, bulk_upsert

TABLE = "regime.macro_series"
CONFLICT = ["series_code", "period_date", "revision"]
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# ── 序列定义：code / 名称 / 接口 / 取值列 / 单位 / 频率 / 发布滞后(天)
SERIES = [
    # 货币与信用
    dict(code="CN.SHRZGM_INC", name="社会融资规模增量(月)", func="macro_china_shrzgm",
         col="社会融资规模增量", unit="yi_yuan", freq="month", lag=45),
    dict(code="CN.SHRZGM_RMB_LOAN", name="社融-人民币贷款(月)", func="macro_china_shrzgm",
         col="其中-人民币贷款", unit="yi_yuan", freq="month", lag=45),
    dict(code="CN.NEW_LOAN", name="新增人民币贷款(月)", func="macro_china_new_financial_credit",
         col="当月", unit="yi_yuan", freq="month", lag=45),
    dict(code="CN.M2_YOY", name="M2 同比", func="macro_china_money_supply",
         col="货币和准货币(M2)-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.M1_YOY", name="M1 同比", func="macro_china_money_supply",
         col="货币(M1)-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.LPR_1Y", name="LPR 1年期", func="macro_china_lpr",
         col="LPR1Y", unit="pct", freq="month", date_col="TRADE_DATE", lag=1,
         release_from_date=True, remark="源为日频重复报价 → 按月去重保留当月最后一次发布"),
    dict(code="CN.LPR_5Y", name="LPR 5年期", func="macro_china_lpr",
         col="LPR5Y", unit="pct", freq="month", date_col="TRADE_DATE", lag=1,
         release_from_date=True, remark="同上"),
    # 通胀与价格
    dict(code="CN.CPI_YOY", name="CPI 同比", func="macro_china_cpi",
         col="全国-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.PPI_YOY", name="PPI 同比", func="macro_china_ppi",
         col="当月同比增长", unit="pct", freq="month", lag=45),
    # 经济周期
    dict(code="CN.PMI_MFG", name="制造业 PMI", func="macro_china_pmi",
         col="制造业-指数", unit="index", freq="month", lag=45),
    dict(code="CN.PMI_NONMFG", name="非制造业 PMI", func="macro_china_pmi",
         col="非制造业-指数", unit="index", freq="month", lag=45),
    dict(code="CN.IP_YOY", name="工业增加值同比", func="macro_china_gyzjz",
         col="同比增长", unit="pct", freq="month", lag=45, use_release_col="发布时间"),
    dict(code="CN.GDP_YOY", name="GDP 同比", func="macro_china_gdp",
         col="国内生产总值-同比增长", unit="pct", freq="quarter", lag=100),
    dict(code="CN.GDP", name="GDP 绝对值(季)", func="macro_china_gdp",
         col="国内生产总值-绝对值", unit="yi_yuan", freq="quarter", lag=100),
    # 市场与实体
    dict(code="CN.MARKET_CAP_SH", name="沪深市价总值-上海", func="macro_china_stock_market_cap",
         col="市价总值-上海", unit="yi_yuan", freq="month", date_col="数据日期", lag=45),
    dict(code="CN.MARKET_CAP_SZ", name="沪深市价总值-深圳", func="macro_china_stock_market_cap",
         col="市价总值-深圳", unit="yi_yuan", freq="month", date_col="数据日期", lag=45),
    dict(code="CN.ELECTRICITY_YOY", name="全社会用电量同比", func="macro_china_society_electricity",
         col="全社会用电量同比", unit="pct", freq="month", date_col="统计时间", lag=45),
    dict(code="CN.CONSUMER_CONFIDENCE", name="消费者信心指数", func="macro_china_xfzxx",
         col="消费者信心指数-指数值", unit="index", freq="month", lag=45),
    # 情绪（源已停更，仅作历史序列）
    dict(code="CN.ACCOUNT_NEW", name="新增投资者数量(万户)", func="stock_account_statistics_em",
         col="新增投资者-数量", unit="count", freq="month", date_col="数据日期", lag=45,
         remark="⚠ 东方财富源已停更于 2023-08，历史序列可用"),
]

# 派生序列（由上面已入库的原始序列做差得出）
DERIVED = [
    dict(code="CN.M1_M2_SCISSOR", name="M1−M2 剪刀差(pp)", expr=("CN.M1_YOY", "CN.M2_YOY"),
         unit="pp", freq="month", remark="M1 同比 − M2 同比，扩大=资金活化"),
    dict(code="CN.PPI_CPI_SCISSOR", name="PPI−CPI 剪刀差(pp)", expr=("CN.PPI_YOY", "CN.CPI_YOY"),
         unit="pp", freq="month", remark="反映利润在上下游间的分配"),
]


def _parse_period(raw, freq_hint: str):
    """把 '2026年08月份' / '2026-08' / '2026年第2季度' / '202608' 解析为统计期首日。"""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t or t.lower() in ("nan", "none"):
        return None
    y = re.search(r"(\d{4})", t)
    if not y:
        return None
    year = int(y.group(1))
    # 季度
    q = re.search(r"(?:第)?\s*([1-4])\s*(?:季度|季|Q)", t, re.I)
    if q or freq_hint == "quarter":
        qq = int(q.group(1)) if q else None
        if qq is None:
            m = re.search(r"[-\s](\d{1,2})", t)
            qq = min(4, max(1, (int(m.group(1)) - 1) // 3 + 1)) if m else 1
        return datetime.date(year, (qq - 1) * 3 + 1, 1)
    # 月
    m = re.search(r"[年\-/\s](\d{1,2})(?!\d)", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return datetime.date(year, int(m.group(1)), 1)
    md = re.search(r"(\d{4})(\d{2})", t)
    if md and 1 <= int(md.group(2)) <= 12:
        return datetime.date(year, int(md.group(2)), 1)
    return datetime.date(year, 1, 1)


def _collect_one(defn: dict, cache: dict) -> list:
    """采集单个序列 → macro_series 记录列表。"""
    fn = defn["func"]
    if fn not in cache:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        fnobj = getattr(ak, fn, None)
        cache[fn] = fnobj() if fnobj else None
    df = cache[fn]
    if df is None or len(df) == 0:
        return []
    if defn["col"] not in df.columns:
        print(f"    ⚠️ {defn['code']}: 接口 {fn} 无列「{defn['col']}」（现有列：{list(df.columns)[:6]}）")
        return []

    import pandas as pd
    dcol = defn.get("date_col", "月份")
    if dcol not in df.columns:
        dcol = df.columns[0]
    rel_col = defn.get("use_release_col")

    rows, skipped = [], 0
    for _, r in df.iterrows():
        v = pd.to_numeric(r.get(defn["col"]), errors="coerce")
        if pd.isna(v):
            skipped += 1
            continue
        d = _parse_period(r.get(dcol), defn["freq"])
        if d is None:
            skipped += 1
            continue
        rel = None
        if defn.get("release_from_date"):
            # 真实发布时间就是数据日期本身（如 LPR 报价日）→ 直接用作 release_time
            rel = datetime.datetime.combine(d, datetime.time(9, 0), tzinfo=TZ_CN)
        if rel is None and rel_col and r.get(rel_col) is not None:
            rd = _parse_period(r.get(rel_col), "day")
            if rd:
                rel = datetime.datetime.combine(rd, datetime.time(18, 0), tzinfo=TZ_CN)
        if rel is None:
            rel = datetime.datetime.combine(
                d + datetime.timedelta(days=defn["lag"]), datetime.time(18, 0), tzinfo=TZ_CN)
        rows.append({
            "series_code": defn["code"], "series_name": defn["name"],
            "period_date": d, "value": round(float(v), 4),
            "unit": defn["unit"], "freq": defn["freq"], "market": "CN",
            "source": f"akshare:{defn['func']}", "release_time": rel, "revision": 0,
            "extra": {"release_basis": "source" if (rel_col and rel) else f"period+{defn['lag']}d",
                      "remark": defn.get("remark")},
            "updated_at": datetime.datetime.now(TZ_CN),
        })
    if skipped:
        print(f"    · {defn['code']}: {len(rows)} 条（跳过空值 {skipped}）")
    return rows


def _build_derived(conn) -> list:
    """派生序列：从库里已入库的原始序列做差（同 period_date 对齐）。"""
    import pandas as pd
    rows = []
    for d in DERIVED:
        a, b = d["expr"]
        df = pd.read_sql_query(
            """SELECT a.period_date, a.value - b.value AS v
               FROM regime.macro_series a JOIN regime.macro_series b
                 ON a.period_date = b.period_date AND a.revision=0 AND b.revision=0
               WHERE a.series_code=%s AND b.series_code=%s ORDER BY 1""",
            conn, params=[a, b])
        for _, r in df.iterrows():
            if r["v"] is None or pd.isna(r["v"]):
                continue
            dd = r["period_date"]
            rows.append({
                "series_code": d["code"], "series_name": d["name"], "period_date": dd,
                "value": round(float(r["v"]), 4), "unit": d["unit"], "freq": d["freq"],
                "market": "CN", "source": "derived:get_macro_monthly",
                "release_time": datetime.datetime.combine(dd + datetime.timedelta(days=45),
                                                          datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"expr": f"{a} - {b}", "remark": d.get("remark")},
                "updated_at": datetime.datetime.now(TZ_CN),
            })
        print(f"    · {d['code']}: {len(rows)} 条累计（派生）")
    return rows


def _dedupe(rows: list) -> list:
    """同一 (series_code, period_date) 只保留 release_time 最新的一条。

    场景：LPR 等日频重复报价接口按月聚合后会同月多值（且源多为倒序），
    不去重会触发 `ON CONFLICT DO UPDATE cannot affect row a second time`。
    """
    best: dict = {}
    for i, r in enumerate(rows):
        key = (r["series_code"], r["period_date"])
        cur = best.get(key)
        if cur is None or (r["release_time"], i) > (cur["release_time"], cur["_idx"]):
            r = {**r, "_idx": i}
            best[key] = r
    out = [{k: v for k, v in r.items() if k != "_idx"} for r in best.values()]
    return sorted(out, key=lambda r: (r["series_code"], r["period_date"]))


def run(codes=None, ctx=None, dry_run: bool = False):
    """采集入口：全量刷新（幂等；月频数据量小）。"""
    cache: dict = {}
    all_rows = []
    print("  ── 原始序列 ──")
    for defn in SERIES:
        try:
            rows = _collect_one(defn, cache)
        except Exception as e:
            print(f"    ❌ {defn['code']}: {type(e).__name__}: {str(e)[:100]}")
            continue
        all_rows += rows
        if rows:
            ds = sorted(r["period_date"] for r in rows)
            print(f"    · {defn['code']}: {len(rows)} 条  {ds[0]} ~ {ds[-1]}")
    before = len(all_rows)
    all_rows = _dedupe(all_rows)
    print(f"  原始合计 {before} 条 → 去重后 {len(all_rows)} 条")

    with get_conn() as conn:
        if dry_run:
            print("  --dry-run：不写库")
            return len(all_rows)
        bulk_upsert(conn, TABLE, all_rows, conflict_cols=CONFLICT, skip_null_updates=True)
        print(f"  ── 派生序列 ──")
        drows = _build_derived(conn)
        if drows:
            bulk_upsert(conn, TABLE, drows, conflict_cols=CONFLICT, skip_null_updates=True)
        print(f"  ✅ 写入 {len(all_rows)} + 派生 {len(drows)} 条")
        with conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT series_code), count(*), max(updated_at)
                           FROM regime.macro_series WHERE source LIKE 'akshare:macro%'
                              OR source LIKE 'derived:get_macro_monthly'""")
            n, cnt, upd = cur.fetchone()
            print(f"  宏观包序列 {n} 个 / {cnt:,} 行，最后更新 {upd}")
    return len(all_rows) + len(drows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("=" * 60)
    print("宏观月/季频包 → regime.macro_series")
    print("=" * 60)
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
