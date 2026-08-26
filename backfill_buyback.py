#!/usr/bin/env python3
"""
回购数据历史回填 — 按市场批量拉取 → 对应表

数据源（与 get_buyback.py 同源，复用其 fetch / 转换函数）：
- 港股：东财 datacenter RPT_HK_BUYBACK（历史可至 1991 年，逐日明细）→ daily_buyback_event
- A股：AKShare stock_repurchase_em（全市场回购方案，方案维度）→ a_stock_repurchase_plan
      A股不强制每日披露，无逐日明细，与港股表隔离存储。

用途：补齐历史段。港股日常调度只采最近 90 天，会漏掉历史；A股为全量方案快照，
      重跑即可刷新进度。入库走 get_buyback 的转换函数，bulk_upsert 按去重键幂等可重跑。

用法：
    python3 backfill_buyback.py                  # 默认回填 hk + a 全历史
    python3 backfill_buyback.py --market a       # 仅 A股（方案表）
    python3 backfill_buyback.py --market hk      # 仅港股（逐日明细）
    python3 backfill_buyback.py --limit 1000     # 只入库前 1000 条（试跑）
    python3 backfill_buyback.py --dry-run        # 只看不落库

说明：
- 港股历史量大（10万+），单页 500 条会翻 ~200 页，按页拉取并逐页 upsert，
  中途失败可从断点续跑（upsert 幂等，重跑不重复）。
- A股重跑前会清掉旧口径（SH./SZ. 前缀）的残缺数据，避免与方案表混淆。
"""
import sys
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_buyback")


def parse_args(argv=None):
    argv = list(sys.argv if argv is None else argv)
    dry = "--dry-run" in argv

    def _val(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    market = (_val("--market") or "all").lower()
    limit_s = _val("--limit")
    return {
        "dry": dry,
        "market": market,
        "limit": int(limit_s) if limit_s else None,
    }


def _backfill_hk(limit, dry):
    """港股全历史回填。返回入库条数。"""
    from get_buyback import fetch_full_buyback, _records_to_db_rows
    from db import get_conn, bulk_upsert
    log.info("拉取港股全历史回购（RPT_HK_BUYBACK）...")
    records = fetch_full_buyback(days=None)
    if not records:
        log.warning("港股全历史获取失败或无数据")
        return 0
    log.info(f"  港股原始记录 {len(records)} 条，转换为入库行...")
    rows = _records_to_db_rows(records)
    log.info(f"  有效入库行 {len(rows)} 条")
    if limit:
        rows = rows[:limit]
        log.info(f"  --limit 截取前 {len(rows)} 条")
    if dry or not rows:
        return 0
    n = 0
    step = 500
    for i in range(0, len(rows), step):
        batch = rows[i:i + step]
        with get_conn() as conn:
            bulk_upsert(conn, "daily_buyback_event", batch, conflict_cols=["stock_code", "buyback_date"])
        n += len(batch)
        log.info(f"  港股进度 {min(i + step, len(rows))}/{len(rows)}")
        time.sleep(0.2)
    log.info(f"  港股入库 {n} 条")
    return n


def _backfill_a(limit, dry):
    """A股回购方案回填 → a_stock_repurchase_plan。返回入库条数。"""
    from get_buyback import fetch_a_repurchase, _a_repurchase_to_db_rows
    from db import get_conn, bulk_upsert
    log.info("拉取 A股全市场回购方案（AKShare stock_repurchase_em）...")
    df = fetch_a_repurchase()
    if df is None:
        log.warning("A股回购方案获取失败或无数据")
        return 0
    rows = _a_repurchase_to_db_rows(df)
    log.info(f"  A股方案行 {len(rows)} 条")
    if limit:
        rows = rows[:limit]
        log.info(f"  --limit 截取前 {len(rows)} 条")
    if dry or not rows:
        return 0
    # 清掉旧口径残缺数据（之前误写入 daily_buyback_event 的 SH./SZ. 行）
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM daily_buyback_event WHERE stock_code LIKE 'SH.%' OR stock_code LIKE 'SZ.%'")
        log.info(f"  已清理 daily_buyback_event 中旧 A股残缺数据 {cur.rowcount} 条")
    # 分批 upsert 到方案表
    n = 0
    step = 500
    for i in range(0, len(rows), step):
        batch = rows[i:i + step]
        with get_conn() as conn:
            bulk_upsert(conn, "a_stock_repurchase_plan", batch, conflict_cols=["stock_code", "plan_id"])
        n += len(batch)
        log.info(f"  A股进度 {min(i + step, len(rows))}/{len(rows)}")
        time.sleep(0.2)
    log.info(f"  A股方案入库 {n} 条")
    return n


def run():
    a = parse_args()
    dry = a["dry"]
    market = a["market"]
    if market not in ("hk", "a", "all"):
        log.error(f"未知 --market={market}，仅支持 hk/a/all")
        sys.exit(1)

    total = 0
    if market in ("hk", "all"):
        total += _backfill_hk(a["limit"], dry)
    if market in ("a", "all"):
        total += _backfill_a(a["limit"], dry)

    if dry:
        log.info(f"dry-run 完成：本应入库 {total} 条（未落库）")
    else:
        log.info(f"回填结束：累计入库 {total} 条")


if __name__ == "__main__":
    run()
