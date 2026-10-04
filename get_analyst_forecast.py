#!/usr/bin/env python3
"""分析师一致预期每日快照采集 —— 写入 regime.analyst_forecast_snapshot。

为什么必须逐日快照：
    一致预期只有「当时值」没有历史（免费源不给时间序列），而**修正宽度（Revision Breadth）
    依赖跨日 diff** —— 漏采一天就永久断档，无法事后补算。因此本脚本：
      · 每个交易日跑一次，落「一股一行」的当日横截面（幂等：同日重跑覆盖）；
      · 财年标签（fyN_year）随值一起存 —— 源列名「2026预测每股收益」逐年滚动，
        跨年比较必须按标签对齐，否则会把不同财年当成同一财年 diff；
      · 不做任何计算（修正宽度由 `market_state_daily.py` 读取本表后算）。

数据源：AKShare `stock_profit_forecast_em()`（东财 盈利预测，实测 2930 只）
    列：研报数 / 机构评级(近六个月)-买入·增持·中性·减持·卖出 / YYYY预测每股收益 × 4 个财年。
    局限：无净利润/营收均值、无目标价 → 与方案 §5.2 草案的长表字段有差异，
    表结构按真实可得字段设计（见 sql/schema.sql 第 5 节形态说明）。

用法:
    python3 get_analyst_forecast.py              # 当日快照（幂等，调度用）
    python3 get_analyst_forecast.py --date 2026-09-30
    python3 get_analyst_forecast.py --dry-run
"""
import argparse
import datetime
import re
import sys

from db import get_conn, bulk_upsert

TABLE = "regime.analyst_forecast_snapshot"
CONFLICT = ["snapshot_date", "stock_code", "source"]
SOURCE = "akshare:stock_profit_forecast_em"
RATING_COLS = {
    "rating_buy": "机构投资评级(近六个月)-买入",
    "rating_overweight": "机构投资评级(近六个月)-增持",
    "rating_neutral": "机构投资评级(近六个月)-中性",
    "rating_reduce": "机构投资评级(近六个月)-减持",
    "rating_sell": "机构投资评级(近六个月)-卖出",
}


def _to_int(v):
    import pandas as pd
    if v is None or pd.isna(v):
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _prefix(code: str) -> str:
    """A 股代码 → 交易所前缀（实测源含北交所 92xxxx，不能只按 6/9→SH 二分）。"""
    if code.startswith(("92", "83", "87", "88", "43")):
        return "BJ"          # 北交所（含原新三板精选层代码段）
    if code.startswith(("60", "68", "90", "11", "13", "50", "51", "58")):
        return "SH"          # 沪市主板/科创板/B股/沪市基金
    return "SZ"              # 深市（00 主板 / 30 创业板 / 20 B股 / 15 基金）


def _fetch(snapshot_date: datetime.date) -> list:
    """拉取当日横截面 → 记录列表（一股一行，含 4 个财年的标签与 EPS）。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    df = ak.stock_profit_forecast_em()
    if df is None or len(df) == 0:
        return []

    # 财年列名有形如「2026预测每股收益」→ 解析出年份，按年份升序映射到 fy1..fy4
    fy_cols = []
    for c in df.columns:
        m = re.search(r"(\d{4})\s*预测每股收益", str(c))
        if m:
            fy_cols.append((int(m.group(1)), c))
    fy_cols.sort()

    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码", "")).strip()
        if not code or code.lower() == "nan":
            continue
        # 交易所前缀：6/9 开头 → SH，其余（0/3/2）→ SZ
        rec = {
            "snapshot_date": snapshot_date,
            "stock_code": f"{_prefix(code)}.{code}",
            "stock_name": str(r.get("名称", "")).strip() or None,
            "report_count": _to_int(r.get("研报数")),
            "source": SOURCE,
        }
        for col, src_col in RATING_COLS.items():
            rec[col] = _to_int(r.get(src_col))
        for i, (year, col) in enumerate(fy_cols[:4], start=1):
            v = pd.to_numeric(r.get(col), errors="coerce")
            rec[f"fy{i}_year"] = year
            rec[f"fy{i}_eps"] = None if pd.isna(v) else round(float(v), 4)
        rows.append(rec)
    if fy_cols:
        print(f"  财年列: {[y for y, _ in fy_cols[:4]]}")
    # 显式按 stock_code 去重：实测源对 492 个代码**完全重复返回**（同为 984 行）。
    # 不显式去重虽然会被 ON CONFLICT 静默合并，但行数对不上会掩盖源侧异常。
    uniq = {}
    for r in rows:
        uniq.setdefault(r["stock_code"], r)
    if len(uniq) != len(rows):
        print(f"  ⚠️ 源侧重复 {len(rows) - len(uniq)} 行（{len(rows)} → {len(uniq)}），已按代码去重")
    return list(uniq.values())


def run(codes=None, ctx=None, snapshot_date: datetime.date = None, dry_run: bool = False):
    """采集入口：当日快照（幂等）。"""
    snapshot_date = snapshot_date or datetime.date.today()
    rows = _fetch(snapshot_date)
    if not rows:
        print("  ⚠️ 源返回空，本次不写库")
        return 0

    n_eps = sum(1 for r in rows if r.get("fy1_eps") is not None)
    n_rating = sum(1 for r in rows if r.get("rating_buy") is not None)
    print(f"  快照 {snapshot_date}: {len(rows)} 只（有 FY1 EPS {n_eps}，有评级分布 {n_rating}）")
    print(f"  样例: {rows[0]['stock_code']} {rows[0]['stock_name']} "
          f"研报数={rows[0]['report_count']} fy1={rows[0]['fy1_year']}:{rows[0]['fy1_eps']}")

    if dry_run:
        print("  --dry-run：不写库")
        return len(rows)

    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT)
        with conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT snapshot_date), count(*),
                                  min(snapshot_date), max(snapshot_date)
                           FROM regime.analyst_forecast_snapshot""")
            n_days, cnt, mn, mx = cur.fetchone()
        print(f"  ✅ upsert {len(rows)} 行；库内累计 {n_days} 个快照日 / {cnt:,} 行（{mn} ~ {mx}）")
        if n_days < 2:
            print("  ℹ️ 仅 1 个快照日 → 修正宽度（跨日 diff）需第 2 个快照日后才有值")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="快照日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("=" * 60)
    print("分析师一致预期每日快照 → regime.analyst_forecast_snapshot")
    print("=" * 60)
    run(snapshot_date=datetime.date.fromisoformat(args.date) if args.date else None,
        dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
