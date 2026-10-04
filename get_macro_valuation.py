#!/usr/bin/env python3
"""全A 市场级估值采集（乐咕乐股）—— 写入 regime.macro_series。

数据源：AKShare 乐咕乐股（legulegu）全A估值序列
  · `stock_a_ttm_lyr()`  全A PE-TTM（中位数/等权平均 + 自带的两个分位）
  · `stock_a_all_pb()`   全A PB（中位数/等权平均 + 两个分位）
  · 实测深度：**2005-01 起，月频 261 行**（当月的点位随月内推进滚动更新）。
  · 用途：解决"个股 PE/PB 仅 2018 起"的历史缺口 —— 市场级估值分位可回溯到 2005，
    覆盖 2015/2018 两轮完整牛熊；个股级估值分位仍从 2018 起（口径分开标注）。

落库序列（freq=month，unit: x=倍数 / index=点位）：
    CN.VAL_PE_MEDIAN  全A PE-TTM 中位数
    CN.VAL_PE_AVG     全A PE-TTM 等权平均
    CN.VAL_PB_MEDIAN  全A PB 中位数
    CN.VAL_PB_AVG     全A PB 等权平均
    CN.VAL_INDEX_CLOSE 全A 收盘点位（乐咕乐股口径，仅作参考上下文）
  源自带分位（全历史 / 近10年）写入 extra JSONB，避免重复造轮子。

PIT 约定：release_time = 观测日 18:00(+08)。注意「当月点」为月内滚动值，月频统计时
应剔除未结束月份（计算层标注），否则当月分位会被部分月数据污染。

用法:
    python3 get_macro_valuation.py            # 全量刷新（月频序列，仅 261 行，直接整表 upsert）
"""
import datetime
import sys

from db import get_conn, bulk_upsert

SOURCE = "akshare:legulegu"
TABLE = "regime.macro_series"
CONFLICT = ["series_code", "period_date", "revision"]
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))


def _mk(code: str, name: str, d, value, unit: str, extra: dict = None) -> dict:
    return {
        "series_code": code,
        "series_name": name,
        "period_date": d,
        "value": round(float(value), 4),
        "unit": unit,
        "freq": "month",
        "market": "CN",
        "source": SOURCE,
        "release_time": datetime.datetime.combine(d, datetime.time(18, 0), tzinfo=TZ_CN),
        "revision": 0,
        "extra": extra,
        # 显式刷新 updated_at（同 get_cn_bond.py：冲突更新不重放默认值，监控依赖此列）
        "updated_at": datetime.datetime.now(TZ_CN),
    }


def _pe_rows() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    df = ak.stock_a_ttm_lyr()
    rows = []
    for _, r in df.iterrows():
        d = r["date"]
        d = d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
        common = {k: (None if pd.isna(r.get(k)) else float(r.get(k)))
                  for k in ("quantileInAllHistoryMiddlePeTtm", "quantileInRecent10YearsMiddlePeTtm")}
        if not pd.isna(r.get("middlePETTM")):
            rows.append(_mk("CN.VAL_PE_MEDIAN", "全A PE-TTM 中位数", d, r["middlePETTM"], "x", common))
        if not pd.isna(r.get("averagePETTM")):
            rows.append(_mk("CN.VAL_PE_AVG", "全A PE-TTM 等权平均", d, r["averagePETTM"], "x", common))
        if not pd.isna(r.get("close")):
            rows.append(_mk("CN.VAL_INDEX_CLOSE", "全A 收盘点位(乐咕乐股口径)", d, r["close"], "index"))
    return rows


def _pb_rows() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    df = ak.stock_a_all_pb()
    rows = []
    for _, r in df.iterrows():
        d = r["date"]
        d = d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
        common = {k: (None if pd.isna(r.get(k)) else float(r.get(k)))
                  for k in ("quantileInAllHistoryMiddlePB", "quantileInRecent10YearsMiddlePB")}
        if not pd.isna(r.get("middlePB")):
            rows.append(_mk("CN.VAL_PB_MEDIAN", "全A PB 中位数", d, r["middlePB"], "x", common))
        if not pd.isna(r.get("equalWeightAveragePB")):
            rows.append(_mk("CN.VAL_PB_AVG", "全A PB 等权平均", d, r["equalWeightAveragePB"], "x", common))
    return rows


def run(codes=None, ctx=None):
    """采集入口：整表刷新（月频序列，量极小，直接全量 upsert 幂等）。"""
    pe = _pe_rows()
    pb = _pb_rows()
    rows = pe + pb
    if not rows:
        print("  ⚠️ 未取到数据")
        return 0
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT, skip_null_updates=True)
    print(f"  ✅ PE {len(pe)} 条 + PB {len(pb)} 条 = {len(rows)} 条")
    print(f"     区间 {rows[0]['period_date']} ~ {max(r['period_date'] for r in rows)}")
    return len(rows)


def _main() -> int:
    print("=" * 60)
    print("全A 市场级估值（乐咕乐股）→ regime.macro_series")
    print("=" * 60)
    run()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT series_code, count(*), min(period_date), max(period_date)
                           FROM regime.macro_series
                           WHERE source = %s GROUP BY 1 ORDER BY 1""", (SOURCE,))
            print("\n── 库内覆盖核对 ──")
            for code, cnt, mn, mx in cur.fetchall():
                print(f"  {code:22s} rows={cnt:>5,}  {mn} ~ {mx}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
