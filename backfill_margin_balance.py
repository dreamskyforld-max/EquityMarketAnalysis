#!/usr/bin/env python3
"""
融资融券全量历史补录 — 按日期区间批量采集沪深两市融资融券明细分日入库

数据源：AKShare 沪深交易所融资融券明细（与 get_margin_balance.py 同源，复用其
        fetch_market / collect 底层逻辑）。

用途：补齐 daily_margin_balance 历史段。交易所接口按单日披露，需逐日拉取；
      跨度太大/限流时按 --chunk 天分段（默认 5 天一段）。upsert 幂等
      （UNIQUE(stock_code, trade_date)），可重复执行。

用法：
    python3 backfill_margin_balance.py --start 2024-01-01 --end 2024-12-31
    python3 backfill_margin_balance.py --start 2024-01-01            # end 默认今天
    python3 backfill_margin_balance.py --days 365                   # 最近 365 天
    python3 backfill_margin_balance.py --start 2024-01-01 --chunk 5 # 每段拉取天数
    python3 backfill_margin_balance.py --start 2024-01-01 --dry-run # 只看不落库

说明：每个交易日两侧市场分别拉取并合并入库；遇空数据日（周末/休市）自动跳过；
      --date 单日模式可用 --date YYYY-MM-DD 直接指定。
"""
import sys
import time
import logging
from datetime import date, timedelta
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_margin_balance")


def parse_args(argv=None) -> dict[str, Any]:
    argv = list(sys.argv if argv is None else argv)
    dry = "--dry-run" in argv

    def _val(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    start_s = _val("--start")
    end_s = _val("--end")
    days_s = _val("--days")
    date_s = _val("--date")
    chunk_s = _val("--chunk")
    return {
        "dry": dry,
        "date": date.fromisoformat(date_s) if date_s else None,
        "start": date.fromisoformat(start_s) if start_s else None,
        "end": date.fromisoformat(end_s) if end_s else None,
        "days": int(days_s) if days_s else None,
        "chunk": int(chunk_s) if chunk_s else 5,
    }


def run():
    a: dict[str, Any] = parse_args()

    # 单日模式
    if a["date"]:
        from get_margin_balance import collect
        n = collect(trade_date=a["date"], dry=bool(a["dry"]))
        log.info(f"单日补录 {a['date']} 完成，处理 {n} 条" + ("（dry-run）" if a["dry"] else ""))
        return

    dry: bool = bool(a["dry"])
    end: date = a["end"] or date.today()
    days: Optional[int] = a["days"]
    start: Optional[date] = a["start"]
    if days:
        start = end - timedelta(days=days)
    if start is None:
        log.error("需要 --start / --days / --date 指定回填范围")
        sys.exit(1)
    if start > end:
        log.error(f"start({start}) 晚于 end({end})，请检查参数")
        sys.exit(1)

    from get_margin_balance import collect

    chunk = max(1, a["chunk"])
    seg = start
    got = 0
    failed_days: list[str] = []
    while seg <= end:
        seg_end = min(seg + timedelta(days=chunk - 1), end)
        log.info(f"拉取 {seg} ~ {seg_end} ...")
        n_day = 0
        d = seg
        while d <= seg_end:
            if d.weekday() >= 5:  # 周末：collect 已跳过，这里不计入失败列表
                d += timedelta(days=1)
                continue
            n = collect(trade_date=d, dry=dry)
            if n == 0:
                # 0 条可能是休市/接口失败（collect 内部已告警），记录待补
                failed_days.append(d.isoformat())
            else:
                n_day += n
            d += timedelta(days=1)
        got += n_day
        log.info(f"  本轮入库 {n_day} 条，累计 {got}")
        seg = seg_end + timedelta(days=1)
        time.sleep(0.2)  # 段间轻量节流，避免 AKShare 限流

    if failed_days:
        log.warning(f"以下 {len(failed_days)} 个交易日采集为 0 条（多为周末/休市/接口异常）：{failed_days[:20]}{'...' if len(failed_days) > 20 else ''}")
    log.info(f"历史补录结束：本次入库 {got} 条" + ("（dry-run，未落库）" if dry else ""))


if __name__ == "__main__":
    run()
