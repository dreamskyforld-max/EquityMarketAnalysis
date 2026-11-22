#!/usr/bin/env python3
"""A股个股↔申万行业统一映射采集 —— 写入 regime.stock_sw_industry_mapping。

阶段 B / D12~D14（2026-11-20）：所有 A 股个股↔申万行业映射**唯一收口**到本表，
废除 industry.constituent / constituent_change / get_sector_constituent.py。

设计（一行一股票，只存原子归属）：
  · 遍历 498 个申万行业（一级31/二级131/三级336）调 AKShare index_component_sw；
  · 按股票聚合：一只股票一行，只存 sw_code_l3（原子归属）+ weight_l1/l2/l3（采集权重，
    三层分母不同，实测中芯国际 3.39% / 5.97% / 60.33%，各自独立）；
  · ⚠ 层级关系不冗余：一/二级 code 从 regime.sw_industry_classification.parent_code
    链按需 join 得出——层级关系唯一真相源在分类表；
  · 覆盖式 upsert（同 stock_code 直接覆盖），**不存 eff_from/eff_to 历史版本**。

⚠ PIT 取舍（D14）：行业指标仍逐日算历史序列，但成分用最新名单贴历史（放弃 PIT，
  接受幸存者偏差）；所有下游指标 pit_capable=false。

实测：498 行业 ≈138s / 15669 成分行。

用法：
    python3 get_stock_sw_industry_mapping.py            # 采集（调度用，每日 08:20）
    python3 get_stock_sw_industry_mapping.py --dry-run
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict

from db import bulk_upsert, get_conn

TABLE = "regime.stock_sw_industry_mapping"
SOURCE = "akshare:index_component_sw"


def _load_hierarchy() -> tuple[dict, dict]:
    """读 sw_industry_classification，返回 {code: level}, {code: parent_code}。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sw_code, level, parent_code FROM regime.sw_industry_classification")
            rows = cur.fetchall()
    if not rows:
        raise RuntimeError("sw_industry_classification 为空：请先跑 get_sw_industry_classification.py")
    return ({r[0]: r[1] for r in rows}, {r[0]: r[2] for r in rows})


def to_stock_code(raw) -> str | None:
    """6 位纯数字 → SH./SZ./BJ. 前缀（照 get_sector_constituent.py:53-71）。"""
    s = str(raw).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit():
        return None
    s = s.zfill(6)
    if len(s) != 6:
        return None
    p = s[:2]
    if p in ("92", "83", "87", "88", "43"):
        return "BJ." + s
    if p in ("60", "68", "90", "11", "13", "50", "51", "58"):
        return "SH." + s
    return "SZ." + s


def _fetch() -> tuple[dict, int, list]:
    """遍历全部行业，按股票聚合。返回 (stock_code -> 行dict, 行业成功数, 失败code)。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    levels, parents = _load_hierarchy()
    codes = sorted(levels, key=lambda c: (levels[c], c))
    acc = defaultdict(dict)   # stock_code -> {level: (code, weight)}
    ok, failed = 0, []
    for code in codes:
        try:
            df = ak.index_component_sw(symbol=code)
        except Exception as e:
            print(f"  ⚠ {code}: {type(e).__name__}: {str(e)[:80]}")
            failed.append(code)
            continue
        if df is None or df.empty:
            failed.append(code)
            continue
        lvl = levels[code]
        for _, r in df.iterrows():
            sc = to_stock_code(r.get("证券代码"))
            if sc is None:
                continue
            w = pd.to_numeric(r.get("最新权重"), errors="coerce")
            acc[sc][lvl] = (code, None if pd.isna(w) else round(float(w), 4))
        ok += 1
    return acc, ok, failed


def _build_rows(acc: dict, as_of: dt.date) -> tuple[list, dict]:
    """把按股票聚合的字典转成写库行。

    ⚠ 只存 sw_code_l3（原子归属）；一/二级 code 不冗余——层级关系唯一真相源是
    regime.sw_industry_classification.parent_code，下游按需 join。weight_l1/l2/l3 是
    采集来的权重数据（非层级关系），三层各自保留。
    """
    rows = []
    stats = defaultdict(int)
    for stock, by_lvl in acc.items():
        l3 = by_lvl[3][0] if 3 in by_lvl else None
        if not l3:
            stats["no_l3"] += 1
            continue
        rows.append({
            "stock_code": stock,
            "sw_code_l3": l3,
            "weight_l1": by_lvl[1][1] if 1 in by_lvl else None,
            "weight_l2": by_lvl[2][1] if 2 in by_lvl else None,
            "weight_l3": by_lvl[3][1] if 3 in by_lvl else None,
            "as_of": as_of,
        })
        stats["total"] += 1
    return rows, dict(stats)


def run(codes=None, ctx=None, dry_run: bool = False) -> dict:
    """采集入口（调度进程内调用 run(codes, ctx=...)）。"""
    as_of = dt.date.today()
    acc, ok, failed = _fetch()
    if ok < 25:
        raise RuntimeError(f"申万行业成功数 {ok} < 25，拒绝写入（避免清空映射表）")
    rows, stats = _build_rows(acc, as_of)
    print(f"申万行业成功 {ok} 个，失败 {len(failed)} 个；聚合股票 {len(rows)} 只（{stats}）")
    if failed:
        print(f"  失败 code: {failed[:20]}")
    if dry_run:
        print("  --dry-run：不写库")
        return {"rows": len(rows), "ok": ok, "failed": len(failed)}
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=["stock_code"])
        conn.commit()
    print(f"  ✅ 写入 {TABLE}: {len(rows)} 行（覆盖式，无历史版本）")
    return {"rows": len(rows), "ok": ok, "failed": len(failed)}


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
