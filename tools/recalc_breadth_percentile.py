#!/usr/bin/env python3
"""L1 宽度指标族「按族全量重算分位」（§2.4 规则 4/5 的第二类合法触发）。

用途：口径变更（v2.1.11）后重算 `regime.indicator_value` 里 11 个 `BREADTH.*`
成员的 `percentile` / `extra` / `model_version`，**不重跑 calc_cn / calc_hk 采集**：
  ① 补 `min_periods=250`（前 250 个有效观测分位置 NULL）；
  ② `extra.n_sample` 落库（供 L2 短样本降置信）；
  ③ 清 `BREADTH.LIMIT_NET` 1996-12-16 制度起点前的护栏残留行；
  ④ `model_version` 递增到新 `BREADTH_CALIB_VERSION` 指纹。

分位口径唯一真相源是 `market_state_daily._compute_percentiles` —— 本脚本与
`_write_market`（日频增量 / --backfill）共用同一函数，杜绝两处口径漂移。

资源约定：只按 market + 11 个 code 过滤读历史（约 15 万行，无大表全量加载），
每批 10,000 行 commit（单事务远低于 5 万行红线）。

⚠ **破坏性**：会 DELETE 护栏残留行并 UPDATE 全历史宽度分位。执行前必须备份
  （`/tmp/breadth_iv_backup_20261105.csv.gz` 为本次执行前快照）。

用法：
    .venv/bin/python3 tools/recalc_breadth_percentile.py            # dry-run，只报告
    .venv/bin/python3 tools/recalc_breadth_percentile.py --write    # 执行
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn, bulk_upsert, read_sql
import market_state_daily as msd

BATCH = 10_000
LOWER_BOUND = datetime.date(1990, 1, 1)


def _read_breadth_raw(conn, market: str) -> tuple[pd.DataFrame, dict]:
    """读宽度族历史 raw_value + 旧 extra（带实体过滤与时间下界）。"""
    codes = sorted(msd.BREADTH_ROLLING_CODES)
    df = read_sql(
        conn,
        "SELECT indicator_code, trade_date, raw_value, extra "
        "FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date >= %s "
        "ORDER BY trade_date, indicator_code",
        params=[market, codes, LOWER_BOUND])
    if df.empty:
        return pd.DataFrame(), {}
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    extra = {(r["indicator_code"], r["trade_date"]): (r["extra"] or {})
             for _, r in df.iterrows()}
    wide = df.pivot_table(index="trade_date", columns="indicator_code",
                          values="raw_value", aggfunc="last").sort_index()
    return wide, extra


def _apply_limit_guard(wide: pd.DataFrame, market: str) -> int:
    """制度起点前的 LIMIT_NET 值在生产端已置 NaN；这里是库内残留，直接抹掉。"""
    code = "BREADTH.LIMIT_NET"
    if code not in wide.columns:
        return 0
    before = wide.index < msd.LIMIT_EFFECTIVE_FROM
    n = int(wide.loc[before, code].notna().sum())
    wide.loc[before, code] = np.nan
    return n


def build_rows(conn, market: str) -> tuple[list[dict], dict]:
    wide, extra = _read_breadth_raw(conn, market)
    if wide.empty:
        return [], {"market": market, "rows": 0}
    n_guard = _apply_limit_guard(wide, market)
    pct, n_samples = msd._compute_percentiles(wide)
    meta = {m["code"]: m for m in msd.INDICATORS}
    now = datetime.datetime.now(msd.TZ_CN)
    rows = []
    for code in wide.columns:
        ver = msd._params_hash(meta[code], True)
        for d in wide.index:
            raw = wide.loc[d, code]
            if pd.isna(raw):
                continue                       # 无原始值 → 不写行（护栏段由 DELETE 清理）
            p = pct.loc[d, code]
            ns = n_samples[code].get(d)
            ex = msd._breadth_extra(code, d, None, n_sample=ns) or {}
            old = extra.get((code, d), {})
            for k in ("adv", "dec", "n", "unchanged"):
                if k in old:
                    ex[k] = old[k]
            rows.append({
                "indicator_code": code, "market": market, "trade_date": d,
                "percentile": None if pd.isna(p) else round(float(p), 2),
                "extra": ex or None,
                "model_version": ver,
                "updated_at": now,
            })
    stat = {
        "market": market, "dates": len(wide.index), "rows": len(rows),
        "guard_rows": n_guard,
        "null_pct": sum(1 for r in rows if r["percentile"] is None),
        "versions": sorted({r["model_version"] for r in rows}),
    }
    return rows, stat


def purge_guard_residue(conn) -> int:
    """删除制度起点前 LIMIT_NET 残留行（bulk_upsert 不会用 NULL 覆盖旧值，必须显式删）。"""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM regime.indicator_value "
            "WHERE indicator_code=%s AND market=%s AND trade_date < %s",
            ("BREADTH.LIMIT_NET", "CN", msd.LIMIT_EFFECTIVE_FROM))
        return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="实际写库（默认 dry-run）")
    ap.add_argument("--market", choices=["CN", "HK"])
    args = ap.parse_args()
    markets = [args.market] if args.market else ["CN", "HK"]

    with get_conn() as conn:
        for market in markets:
            rows, stat = build_rows(conn, market)
            print(f"{market}: {stat['dates']} 日 / 待写 {stat['rows']} 行 / "
                  f"护栏残留 {stat['guard_rows']} 行 / "
                  f"短样本置 NULL 分位 {stat['null_pct']} 行 / 版本 {stat['versions']}")
            if not args.write:
                continue
            deleted = purge_guard_residue(conn)
            print(f"  · 删除 LIMIT_NET 制度起点前残留 {deleted} 行")
            for i in range(0, len(rows), BATCH):
                bulk_upsert(conn, "regime.indicator_value", rows[i:i + BATCH],
                            conflict_cols=["indicator_code", "market", "trade_date"],
                            skip_null_updates=False, only_if_changed=True)
                conn.commit()                  # 单事务 ≤ BATCH 行（资源规范）
                print(f"  · 写入 {min(i + BATCH, len(rows))}/{len(rows)} 行")
        if args.write:
            n = msd._ensure_dict(conn)
            conn.commit()
            print(f"✅ indicator_dict 登记/更新 {n} 条（宽度族 11 个 params_hash 已递增）")
        else:
            print("（dry-run，未写库；加 --write 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
