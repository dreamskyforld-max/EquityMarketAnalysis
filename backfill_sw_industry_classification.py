#!/usr/bin/env python3
"""申万行业指数**首次全量回填** —— 写入 daily_benchmark（一次性/大修后手工跑）。

职责（单一职责）：只做全量回填；日常增量在 `get_sw_industry_quotes.py`。
数据源：AKShare `index_hist_sw(symbol, period='day')`，从 regime.sw_industry_classification
  读全部 498 个行业代码（一级31/二级131/三级336），每个 ~6500 行 ≈ 220 万行。

⚠ 计算资源红线（AGENTS.md）：本脚本必须走**独立 oneshot systemd unit**（不挂 market_scheduler），
  分批（BATCH=20 行业）、单事务写 ≤5 万行；上线前 `/usr/bin/time -v` 实测峰值内存并留 2 倍余量。

用法:
    python3 backfill_sw_industry_classification.py --dry-run      # 只打印计划，不写库
    python3 backfill_sw_industry_classification.py                # 全量回填（分批写库）
    python3 backfill_sw_industry_classification.py --start-batch 0 --batches 1  # 只跑第1批（unit 分片用）
"""
import argparse
import sys
import time

from db import bulk_upsert, get_conn
from get_sw_industry_quotes import _rows_for, _load_sw_codes, TABLE, CONFLICT

BATCH_SIZE = 20   # 每批行业数：20 × 6500 行 = 13 万行 < 单事务 5 万行 → 内部再按 5 万切


def _write_batched(conn, rows: list, batch_size: int = 50000):
    """单事务 ≤5 万行，逐批 commit。"""
    n = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        bulk_upsert(conn, TABLE, chunk, conflict_cols=CONFLICT)
        conn.commit()
        n += len(chunk)
    return n


def run(start_batch: int = 0, batches: int = None, dry_run: bool = False) -> dict:
    sw_map = _load_sw_codes()
    codes = list(sw_map.keys())
    total_batches = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
    end = total_batches if batches is None else min(total_batches, start_batch + batches)
    print(f"全量回填：{len(codes)} 个行业，分 {total_batches} 批 ×{BATCH_SIZE}；"
          f"本次跑第 {start_batch}~{end-1} 批" + ("（dry-run）" if dry_run else ""))

    written = 0
    with get_conn() as conn:
        for bi in range(start_batch, end):
            batch = codes[bi * BATCH_SIZE:(bi + 1) * BATCH_SIZE]
            rows = []
            for code in batch:
                try:
                    rows.extend(_rows_for(code, sw_map[code]))
                except Exception as e:
                    print(f"  ❌ {code} {sw_map[code]}: {type(e).__name__}: {e}")
            print(f"  批 {bi}: {len(batch)} 行业 → {len(rows)} 行"
                  + (f"  ({rows[0]['trade_date']}~{rows[-1]['trade_date']})" if rows else ""))
            if not dry_run:
                written += _write_batched(conn, rows)
            del rows
    print(f"完成：写入 {written} 行" + ("（dry-run 未写库）" if dry_run else ""))
    return {"industries": len(codes), "batches": end - start_batch, "written": written}


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start-batch", type=int, default=0)
    ap.add_argument("--batches", type=int, default=None)
    args = ap.parse_args()
    run(start_batch=args.start_batch, batches=args.batches, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
