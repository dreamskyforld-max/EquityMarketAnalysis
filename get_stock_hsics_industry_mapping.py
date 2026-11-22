#!/usr/bin/env python3
"""港股个股↔HSICS 行业统一映射采集 —— 写入 regime.stock_hsics_industry_mapping。

阶段 B / D12：港股个股↔HSICS 映射收口到本表（与 A 股 stock_sw_industry_mapping 对称）。

设计（一行一股票，只存原子归属）：
  · 源 = stock_sector（113 个 HK.INDUSTRY 板块成分）+ futu_hsics_code_mapping（富途码→HSICS 官方码）
    + hsics_industry_classification（官方分类层级）；
  · 只存 hsics_code_l3（三级 6 位码）+ futu_sector_code；一/二级从分类表 parent 链 join；
  · ⚠ 幸存者偏差：stock_sector 无历史，pit_capable=false；
  · 覆盖式 upsert（同 stock_code 直接覆盖），不存历史版本。

实测：2773 只港股 / 113 板块；纯 SQL join，秒级。

用法：
    python3 get_stock_hsics_industry_mapping.py            # 采集（调度用，每日 08:17）
    python3 get_stock_hsics_industry_mapping.py --dry-run
"""
import argparse
import datetime as dt
import sys

from db import bulk_upsert, get_conn

TABLE = "regime.stock_hsics_industry_mapping"


def _build(as_of: dt.date) -> tuple[list, dict]:
    """关联出港股个股 → HSICS 三级码，返回 (rows, stats)。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ss.stock_code,
                       ss.sector_code AS futu_code,
                       m.hsics_code
                FROM stock_sector ss
                JOIN regime.futu_hsics_code_mapping m ON m.futu_code = ss.sector_code
                WHERE ss.sector_type='INDUSTRY' AND left(ss.stock_code,3)='HK.'
                  AND m.hsics_code IS NOT NULL
            """)
            hits = cur.fetchall()
    # 一股一板块（已实测 2773 股/2773 唯一），若重复取 hsics_code 非空的那条
    acc = {}
    stats = {"raw": len(hits), "no_code": 0}
    for sc, futu, hsics in hits:
        if not hsics:
            stats["no_code"] += 1
            continue
        acc[sc] = {"stock_code": sc, "hsics_code_l3": hsics,
                   "futu_sector_code": futu, "as_of": as_of}
    stats["total"] = len(acc)
    return list(acc.values()), stats


def run(codes=None, ctx=None, dry_run: bool = False) -> dict:
    as_of = dt.date.today()
    rows, stats = _build(as_of)
    if stats["total"] < 2000:
        raise RuntimeError(f"港股映射仅 {stats['total']} 只（预期 ≥2000），拒绝写入")
    print(f"港股个股↔HSICS 映射：{stats}；写出 {len(rows)} 行")
    if dry_run:
        print("  --dry-run：不写库")
        return stats
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=["stock_code"])
        conn.commit()
    print(f"  ✅ 写入 {TABLE}: {len(rows)} 行（覆盖式，pit_capable=false）")
    return stats


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
