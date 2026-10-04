#!/usr/bin/env python3
"""退市股名单回补 —— 写入 stock_info（delisting / delist_date）。

背景（doc/market_profile_collection_plan.md §2.1 / §9 幸存者偏差）：
  现有 a_daily_quote 只含「当前在市 + 近期停牌」的票（A 股 2019–2025 退市股仅留存 19 只），
  历史退市股整批缺失 → 用当前样本回溯历史宽度会系统性偏乐观。
  本脚本先把**退市名单**（代码 / 简称 / 退市日期）补齐，作为：
    ① 宽度指标的敏感性分析口径（含/不含退市股）；
    ② 后续逐票试补历史行情（`backfill_delisted_quotes.py`，另立）的输入清单。

数据源（实测可用）：
  · 上交所 `stock_info_sh_delist(symbol='全部')`      → 公司代码 / 公司简称 / 上市日期 / 暂停上市日期
  · 深交所 `stock_info_sz_delist(symbol='终止上市公司')` → 证券代码 / 证券简称 / 上市日期 / 终止上市日期
  注：上交所侧字段名为「暂停上市日期」，对已退市公司即其停止交易日期，统一记为 delist_date
      （口径差异写在 remark/输出中，不做静默统一）。

写入语义：stock_info upsert（conflict=stock_code，skip_null_updates=True）
  · 已存在的票 → 只补 delisting/delist_date，不动 list_date/name 等既有字段；
  · 不在 stock_info 的票 → 新建行（market=SH/SZ，symbol=纯数字码）。
  · 不写入 quote_universe / realtime_collect_target（退市股不进采集池）。

用法:
    python3 backfill_delisted.py            # 回补（幂等，可重复跑）
    python3 backfill_delisted.py --dry-run  # 只打印不写库
"""
import argparse
import datetime
import sys

from db import get_conn, bulk_upsert

TABLE = "stock_info"
CONFLICT = ["stock_code"]


def _norm_date(v):
    if v is None:
        return None
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _fetch_sh() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    out = []
    try:
        df = ak.stock_info_sh_delist(symbol="全部")
    except Exception as e:
        print(f"  ❌ 上交所名单拉取失败: {type(e).__name__}: {e}")
        return out
    for _, r in df.iterrows():
        code = str(r.get("公司代码", "")).strip()
        if not code or pd.isna(r.get("公司代码")):
            continue
        out.append({
            "stock_code": f"SH.{code.zfill(6)}",
            "stock_name": str(r.get("公司简称", "")).strip() or None,
            "market": "SH",
            "symbol": code.zfill(6),
            "list_date": _norm_date(r.get("上市日期")),
            "delist_date": _norm_date(r.get("暂停上市日期")),
            "delisting": True,
        })
    print(f"  上交所：{len(out)} 条（含「暂停上市日期」口径）")
    return out


def _fetch_sz() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    import pandas as pd

    out = []
    for sym in ("终止上市公司", "暂停上市公司"):
        try:
            df = ak.stock_info_sz_delist(symbol=sym)
        except Exception as e:
            print(f"  ⚠️ 深交所[{sym}]拉取失败: {type(e).__name__}: {e}")
            continue
        for _, r in df.iterrows():
            code = r.get("证券代码")
            if code is None or pd.isna(code):
                continue
            code = str(code).strip().split(".")[0].zfill(6)
            out.append({
                "stock_code": f"SZ.{code}",
                "stock_name": str(r.get("证券简称", "")).strip() or None,
                "market": "SZ",
                "symbol": code,
                "list_date": _norm_date(r.get("上市日期")),
                "delist_date": _norm_date(r.get("终止上市日期") or r.get("暂停上市日期")),
                "delisting": True,
            })
        print(f"  深交所[{sym}]：{len(out)} 条累计")
    return out


def ensure_columns(conn):
    """存量库补列（幂等）：delist_date 由本脚本首次运行自动补齐。"""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS delist_date DATE")
        cur.execute("COMMENT ON COLUMN stock_info.delist_date IS "
                    "'退市日期（NULL=未退市）；上交所侧源字段为「暂停上市日期」口径；"
                    "用于幸存者偏差敏感性分析'")
    conn.commit()


def run(codes=None, ctx=None, dry_run: bool = False):
    """采集入口：回补退市名单（幂等）。"""
    rows = _fetch_sh() + _fetch_sz()
    if not rows:
        print("  ⚠️ 未取到退市名单")
        return 0

    # 去重（两所名单可能有重叠/重复行）
    uniq = {r["stock_code"]: r for r in rows}
    rows = list(uniq.values())
    with_date = [r for r in rows if r["delist_date"]]
    print(f"  去重后 {len(rows)} 条，其中 {len(with_date)} 条有退市日期")

    if dry_run:
        print("  --dry-run：不写库。样例：")
        for r in rows[:5]:
            print("   ", r)
        return len(rows)

    with get_conn() as conn:
        ensure_columns(conn)
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT, skip_null_updates=True)
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM stock_info WHERE delisting IS TRUE""")
            n_flag = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM stock_info WHERE delist_date IS NOT NULL""")
            n_date = cur.fetchone()[0]
            cur.execute("""SELECT to_char(delist_date,'YYYY') yr, count(*)
                           FROM stock_info WHERE delist_date IS NOT NULL
                           GROUP BY 1 ORDER BY 1 DESC LIMIT 6""")
            by_year = cur.fetchall()
        print(f"  ✅ upsert {len(rows)} 行；stock_info 中 delisting=TRUE {n_flag} 条，"
              f"delist_date 非空 {n_date} 条")
        print("  最近年份分布：" + ", ".join(f"{y}:{c}" for y, c in by_year))
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 60)
    print("退市股名单回补 → stock_info(delisting / delist_date)")
    print("=" * 60)
    run(dry_run=args.dry_run)
    print("\n下一步（另立脚本）：用名单逐票试补历史行情（东财对退市股部分可得），"
          "\n补不到的记入「已知缺失清单」，宽度指标做含/不含敏感性标注。")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
