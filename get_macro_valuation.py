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

from db import get_conn
from regime_schema import upsert_macro_series, seed_series_meta, load_series_defs

SOURCE = "akshare:legulegu"
TABLE = "regime.macro_series"
CONFLICT = ["series_code", "period_date", "revision"]
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# 序列定义（**采集契约**）：table=源接口 / field=取值列；登记进 macro_series_meta 后，
# 改「取值列/单位/备注」直接改库即可，不必改代码（见 seed_series_meta / load_series_defs）。
SPECS = [
    dict(code="CN.VAL_PE_MEDIAN", name="全A PE-TTM 中位数", unit="x",
         table="ttm", field="middlePETTM", quantile=True),
    dict(code="CN.VAL_PE_AVG", name="全A PE-TTM 等权平均", unit="x",
         table="ttm", field="averagePETTM", quantile=True),
    dict(code="CN.VAL_INDEX_CLOSE", name="全A 收盘点位(乐咕乐股口径)", unit="index",
         table="ttm", field="close", quantile=False),
    dict(code="CN.VAL_PB_MEDIAN", name="全A PB 中位数", unit="x",
         table="pb", field="middlePB", quantile=True),
    dict(code="CN.VAL_PB_AVG", name="全A PB 等权平均", unit="x",
         table="pb", field="equalWeightAveragePB", quantile=True),
]


def _mk(code: str, d, value, extra, defs: dict) -> dict:
    """按「生效定义」构造行：库里有的用库的值，没有则用 SPECS 兜底。"""
    m = defs.get(code) or {}
    spec = next((s for s in SPECS if s["code"] == code), {})
    p = m.get("params") or spec
    return {
        "series_code": code,
        "series_name": m.get("name") or spec.get("name") or code,
        "period_date": d,
        "value": round(float(value), 4),
        "unit": m.get("unit") or spec.get("unit") or "x",
        "freq": "month",
        "market": "CN",
        "source": SOURCE,
        # 采集契约 → 维度表 macro_series_meta
        "lag_days": m.get("lag_days", 0),
        "collect_params": {"table": p.get("table"), "field": p.get("field")},
        "remark": m.get("remark"),
        "release_time": datetime.datetime.combine(d, datetime.time(18, 0), tzinfo=TZ_CN),
        "revision": 0,
        "extra": extra,
        # 显式刷新 updated_at（同 get_cn_bond.py：冲突更新不重放默认值，监控依赖此列）
        "updated_at": datetime.datetime.now(TZ_CN),
    }


def _rows_from(df, table: str, quantiles: tuple, defs: dict) -> list:
    """按 SPECS（**库里生效的 field 优先**）逐列取值 → fact 行。"""
    import pandas as pd

    rows = []
    for _, r in df.iterrows():
        d = r["date"]
        d = d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
        common = {k: (None if pd.isna(r.get(k)) else float(r.get(k))) for k in quantiles}
        for s in SPECS:
            if s["table"] != table:
                continue
            m = defs.get(s["code"]) or {}
            field = (m.get("params") or {}).get("field") or s["field"]
            v = r.get(field)
            if v is None or pd.isna(v):
                continue
            rows.append(_mk(s["code"], d, v, common if s.get("quantile") else None, defs))
    return rows


def _pe_rows(defs: dict) -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    return _rows_from(ak.stock_a_ttm_lyr(), "ttm",
                      ("quantileInAllHistoryMiddlePeTtm", "quantileInRecent10YearsMiddlePeTtm"), defs)


def _pb_rows(defs: dict) -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    return _rows_from(ak.stock_a_all_pb(), "pb",
                      ("quantileInAllHistoryMiddlePB", "quantileInRecent10YearsMiddlePB"), defs)


def run(codes=None, ctx=None):
    """采集入口：整表刷新（月频序列，量极小，直接全量 upsert 幂等）。"""
    with get_conn() as conn:
        seed_series_meta(conn, [{
            "series_code": s["code"], "series_name": s["name"], "unit": s["unit"],
            "freq": "month", "market": "CN", "source": SOURCE, "lag_days": 0,
            "collect_params": {"table": s["table"], "field": s["field"]},
        } for s in SPECS])
        defs = load_series_defs(conn, codes=[s["code"] for s in SPECS])
    pe = _pe_rows(defs)
    pb = _pb_rows(defs)
    rows = pe + pb
    if not rows:
        print("  ⚠️ 未取到数据")
        return 0
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
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
                           FROM regime.v_macro_series
                           WHERE source = %s GROUP BY 1 ORDER BY 1""", (SOURCE,))
            print("\n── 库内覆盖核对 ──")
            for code, cnt, mn, mx in cur.fetchall():
                print(f"  {code:22s} rows={cnt:>5,}  {mn} ~ {mx}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
