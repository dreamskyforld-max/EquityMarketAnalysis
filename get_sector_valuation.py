#!/usr/bin/env python3
"""申万一级行业估值快照采集 —— 写入 regime.sector_valuation_snapshot。

为什么必须逐日快照：
    源 `sw_index_first_info`（申万宏源官方口径）**只提供当前横截面**（31 个行业的
    PE/PB/股息率），**没有任何历史序列** → 行业估值分位/纵向比较只能靠逐日累积，
    漏采一天即永久缺失（与一致预期快照、行业资金流同类约束）。

来源与用法说明：
    · 行业估值常用于「行业贵不贵」的判断（如 PE_TTM 处于 3 年分位何处），
      本表只落**原始快照**，分位/分位排序由消费方（或计算层的 `SECTOR.PE_TTM_MEDIAN`）计算；
    · 逐行业估值 = 本表；市场级中位数 = `regime.indicator_value` 的 `SECTOR.PE_TTM_MEDIAN`。
    · trade_date 取「最近 A 股交易日」（读 public.trading_calendar）→ 节假日重跑只幂等覆盖，
      不会把快照记成非交易日。

用法:
    python3 get_sector_valuation.py                  # 采集（调度用）
    python3 get_sector_valuation.py --dry-run
    python3 get_sector_valuation.py --date 2026-09-30
"""
import argparse
import datetime
import sys

import pandas as pd

from db import get_conn, bulk_upsert

TABLE = "regime.sector_valuation_snapshot"
CONFLICT = ["snapshot_date", "sw_code"]
SOURCE = "akshare:sw_index_first_info"


def _recent_cn_trading_date() -> datetime.date:
    """最近 A 股交易日（trading_calendar）；查不到回落今天。"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT max(cal_date) FROM public.trading_calendar "
                            "WHERE market='CN' AND is_open AND cal_date <= CURRENT_DATE")
                d = cur.fetchone()[0]
                if d:
                    return d
    except Exception as e:
        print(f"  ⚠️ 读取 A 股交易日失败（回落今天）: {e}")
    return datetime.date.today()


def _fetch() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    df = ak.sw_index_first_info()
    if df is None or len(df) == 0:
        return []

    def num(v):
        x = pd.to_numeric(v, errors="coerce")
        return None if pd.isna(x) else round(float(x), 4)

    rows = []
    for _, r in df.iterrows():
        raw = str(r.get("行业代码", "")).strip()
        code = raw.split(".")[0]                      # 源形如 801010.SI
        if not code.isdigit():
            continue
        rows.append({
            "sw_code": code,
            "sw_name": (str(r.get("行业名称", "")).strip() or None),
            "constituent_count": None if pd.isna(pd.to_numeric(r.get("成份个数"), errors="coerce"))
            else int(pd.to_numeric(r.get("成份个数"), errors="coerce")),
            "pe_static": num(r.get("静态市盈率")),
            "pe_ttm": num(r.get("TTM(滚动)市盈率")),
            "pb": num(r.get("市净率")),
            "div_yield": num(r.get("静态股息率")),
            "source": SOURCE,
        })
    return rows


def run(codes=None, ctx=None, snapshot_date: datetime.date = None, dry_run: bool = False):
    """采集入口：申万一级行业估值横截面（幂等 upsert）。"""
    snapshot_date = snapshot_date or _recent_cn_trading_date()
    print("=" * 62)
    print(f"申万一级行业估值快照（{SOURCE}）→ {TABLE}")
    print(f"归属交易日: {snapshot_date}")
    print("=" * 62)

    rows = _fetch()
    if not rows:
        print("  ⚠️ 源返回 0 行，本次不写库")
        return 0
    n_valid = sum(1 for r in rows if r["pe_ttm"] is not None)
    print(f"  源 {len(rows)} 个行业（有 TTM PE {n_valid}）")
    if n_valid == 0:
        print("  ⚠️ 全部行业 PE 为空（源异常/非交易日），跳过写入")
        return 0
    sample = sorted([r for r in rows if r["pe_ttm"] is not None], key=lambda r: r["pe_ttm"])
    print("  PE_TTM 最低 3:" + "，".join(f"{r['sw_name']} {r['pe_ttm']:.1f}" for r in sample[:3]))
    print("  PE_TTM 最高 3:" + "，".join(f"{r['sw_name']} {r['pe_ttm']:.1f}" for r in sample[-3:]))

    if dry_run:
        print("  --dry-run：不写库")
        return len(rows)

    now = datetime.datetime.now()
    for r in rows:
        r["snapshot_date"] = snapshot_date
        r["updated_at"] = now
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT, skip_null_updates=True)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*), count(DISTINCT snapshot_date), min(snapshot_date), "
                        f"max(snapshot_date) FROM {TABLE}")
            cnt, days, mn, mx = cur.fetchone()
    print(f"  ✅ upsert {len(rows)} 行；库内累计 {days} 个快照日 / {cnt:,} 行（{mn} ~ {mx}）")
    if days < 2:
        print("  ℹ️ 仅 1 个快照日 → 行业估值分位需累积（源无历史，逐日累加越快越值钱）")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="快照日 YYYY-MM-DD（默认最近 A 股交易日）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(snapshot_date=datetime.date.fromisoformat(args.date) if args.date else None,
        dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
