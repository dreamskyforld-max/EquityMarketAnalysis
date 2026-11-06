#!/usr/bin/env python3
"""`DIM.TREND` 前置：3 个 `TREND.*` 原子的全历史 raw_value + expanding 分位回填。

为什么单独一个工具（而不是 `market_state_daily.py --backfill`）：
  · `run()` 会遍历全部 126 个指标并按 4 年分块重算全历史，本次只新增 3 个原子，
    全量回填会无谓触碰其余 123 个指标的 `updated_at` 与写库压力；
  · 按族重算是项目既有范式（`tools/recalc_flow_percentile.py` /
    `tools/recalc_breadth_percentile.py`）：锁定 codes → DELETE 旧行 → 写新行 →
    `_ensure_dict()` 登记字典，破坏性范围锁在这 3 个编码内。
  · 日频增量由 `market_state_daily.calc_cn / calc_hk` 里的 `_trend_atoms()` 自动接入，
    `_write_market()` 会把全历史 raw 与新窗口拼接后重算分位（PIT 安全）。

口径（§5.6 v1.19）：
  · raw_value 由 `daily_benchmark` 指数收盘价现算，护栏 TREND_ASOF_START 之前置 NULL；
  · percentile 走 `_expanding_percentile(s)`（默认 expanding、min_periods=0，
    与其余非宽度指标同款），护栏前的失真样本不进参考分布；
  · `model_version` = `_params_hash(meta, rolling=False)`，只挂在 TREND_CALIB_CODES 上。

用法：
    .venv/bin/python3 tools/recalc_trend_percentile.py --probe   # 只读试算
    .venv/bin/python3 tools/recalc_trend_percentile.py --write   # 写库
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import bulk_upsert, get_conn  # noqa: E402
from market_state_daily import (  # noqa: E402
    INDICATORS, TREND_CALIB_CODES, _ensure_dict, _expanding_percentile,
    _params_hash, _trend_atoms,
)

TABLE = "regime.indicator_value"
CODES = sorted(TREND_CALIB_CODES)
META = {m["code"]: m for m in INDICATORS}


def build_rows(conn, market: str, end: datetime.date) -> list[dict]:
    """全历史算 3 个原子的 raw_value 与 expanding 分位，返回待写行。

    `_trend_atoms(index=None)` 返回全历史序列（CN ~8757 / HK ~3253 行），
    远低于资源规范 50 万行红线；写库按市场分批，单事务 <5 万行。
    """
    atoms = _trend_atoms(conn, market, end)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows: list[dict] = []
    for code in CODES:
        raw = atoms[code]
        pct = _expanding_percentile(raw)
        model_version = _params_hash(META[code], False)
        for d in raw.index:
            v, p = raw.loc[d], pct.loc[d]
            if pd.isna(v) and pd.isna(p):
                continue
            rows.append({
                "indicator_code": code, "market": market, "trade_date": d,
                "raw_value": None if pd.isna(v) else round(float(v), 6),
                "percentile": None if pd.isna(p) else round(float(p), 2),
                "extra": None,
                "model_version": model_version,
                "updated_at": now,
            })
    return rows


def _report(rows: list[dict], market: str) -> None:
    for code in CODES:
        part = [r for r in rows if r["indicator_code"] == code]
        dates = sorted(r["trade_date"] for r in part)
        valid = [r for r in part if r["raw_value"] is not None]
        pct = [r for r in part if r["percentile"] is not None]
        print(f"  {code:<22} rows={len(part):<6} raw={len(valid):<6} "
              f"pct={len(pct):<6} {dates[0] if dates else '-'}~{dates[-1] if dates else '-'}"
              f"  model_version={part[0]['model_version'] if part else '-'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只读试算，不写库")
    ap.add_argument("--write", action="store_true", help="写 regime.indicator_value + 字典登记")
    ap.add_argument("--end", type=datetime.date.fromisoformat,
                    default=datetime.date.today(), help="计算截止日")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe 或 --write")

    with get_conn() as conn:
        all_rows = {m: build_rows(conn, m, args.end) for m in ("CN", "HK")}
        for market, rows in all_rows.items():
            print(f"===== {market} =====")
            _report(rows, market)
        if args.write:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {TABLE} WHERE indicator_code=ANY(%s)", (CODES,))
            print(f"🗑  清理 {CODES} 旧行")
            total = 0
            for market, rows in all_rows.items():
                if not rows:
                    continue
                bulk_upsert(conn, TABLE, rows,
                            conflict_cols=["indicator_code", "market", "trade_date"],
                            skip_null_updates=True, only_if_changed=True)
                conn.commit()
                total += len(rows)
                print(f"✅ {market} 写入 {len(rows)} 行")
            _ensure_dict(conn)
            conn.commit()
            print(f"✅ 字典登记完成（_ensure_dict）；共 {total} 行")
        else:
            print("[probe] 未写库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
