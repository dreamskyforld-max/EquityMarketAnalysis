#!/usr/bin/env python3
"""
南向资金历史回填 — 按日期区间批量拉取全市场港股通持股 → daily_ggt_hold

数据源：AKShare stock_hsgt_stock_statistics_em(symbol="南向持股")
       （与 get_south_flow.py 同源，复用其 fetch/save/recalc）
用途：补齐历史段全市场持股数据（如 2026-04-30~07-12 期间每天仅 1-2 条单票/双票），
      入库后调用 recalc_from_db() 窗口重算差值（est_net_inflow 等自动闭合）。

用法：
    python3 backfill_south_flow.py --start 2026-04-30 --end 2026-07-12
    python3 backfill_south_flow.py --start 2026-04-30            # end 默认今天
    python3 backfill_south_flow.py --days 60                     # 最近 60 天
    python3 backfill_south_flow.py --start 2026-04-30 --chunk 30 # 每段拉取天数（默认 30）
    python3 backfill_south_flow.py --start 2026-04-30 --dry-run  # 只看不落库

说明：接口单次支持日期区间，但跨度太大会超时/限流，默认按 30 天一段分段拉取；
     upsert 幂等（UNIQUE(stock_code, trade_date)），可重复执行。
"""
import sys
import time
import logging
from datetime import date, datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_south_flow")


def parse_args(argv=None):
    argv = list(sys.argv if argv is None else argv)
    dry = "--dry-run" in argv

    def _val(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    start_s = _val("--start")
    end_s = _val("--end")
    days_s = _val("--days")
    chunk_s = _val("--chunk")
    return {
        "dry": dry,
        "start": date.fromisoformat(start_s) if start_s else None,
        "end": date.fromisoformat(end_s) if end_s else None,
        "days": int(days_s) if days_s else None,
        "chunk": int(chunk_s) if chunk_s else 30,
    }


def run():
    a = parse_args()
    dry = a["dry"]
    end = a["end"] or date.today()
    if a["days"]:
        start = end - timedelta(days=a["days"])
    else:
        start = a["start"]
        if start is None:
            log.error("需要 --start 或 --days 指定回填起始日期")
            sys.exit(1)
    if start > end:
        log.error(f"start({start}) 晚于 end({end})，请检查参数")
        sys.exit(1)

    from get_south_flow import fetch_market_south_flow, save_to_db, recalc_from_db

    chunk = max(1, a["chunk"])
    seg = start
    got = 0
    saved = 0
    while seg <= end:
        seg_end = min(seg + timedelta(days=chunk - 1), end)
        log.info(f"拉取 {seg} ~ {seg_end} ...")
        try:
            rows = fetch_market_south_flow(start_date=seg, end_date=seg_end)
        except Exception as e:
            log.warning(f"  {seg} ~ {seg_end} 拉取异常: {e}")
            seg = seg_end + timedelta(days=1)
            continue
        got += len(rows)
        log.info(f"  获取 {len(rows)} 条")
        if rows and not dry:
            n = save_to_db(rows)
            saved += n
            log.info(f"  入库 {n} 条（累计 {saved}）")
        seg = seg_end + timedelta(days=1)
        time.sleep(0.5)

    log.info(f"回填结束：获取 {got} 条" + ("" if dry else f"，入库 {saved} 条"))
    if dry:
        log.info("dry-run：未落库")
        return
    if saved == 0:
        log.info("无新增数据，跳过窗口重算")
        return
    m = recalc_from_db()
    log.info(f"窗口重算差值 {m} 行，完成")


if __name__ == "__main__":
    run()
