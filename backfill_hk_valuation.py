#!/usr/bin/env python3
"""
港股 市销率(PS)/市现率(PCF) 历史回溯（由 financial_indicator 推算）

为什么是「推算」而非「直接采集」：
    港股没有免费可用的「每日 PS/PCF」时序源——
      - 东财同行比较 stock_hk_valuation_comparison_em 只给当前快照（非历史）；
      - 百度股市通港股只给市现率、不给市销率。
    而本仓 financial_indicator 已逐只采集港股季度财务（营收 revenue / 经营现金流
    operating_cash_flow，含 Q1/H1/Q3/annual 四种 report_type），因此用
    「总市值 ÷ 近 4 季滚动(TTM) 营收 / 经营现金流」现算，可得全历史时序，
    且与 A 股口径统一（A 股走东财 RPT_VALUEANALYSIS_DET 直采 PS_TTM/PCF_OCF_TTM）。

公式：
    PS_TTM  = total_market_val / revenue_TTM
    PCF_TTM = total_market_val / operating_cash_flow_TTM
    revenue_TTM / ocf_TTM = financial_indicator 中该股票「截至 trade_date 最新报告期」及其
    前 3 个季度报告期的 revenue / operating_cash_flow 之和
    （SQL 窗口 ROWS BETWEEN 3 PRECEDING AND CURRENT ROW，恰好覆盖一个完整财年 4 季）。
    注：若某股票仅有年报（无季度），窗口退化为单条年报行 = 全年营收 = TTM，仍成立。

币种假设：financial_indicator 港股营收/经营现金流与 hk_daily_quote.total_market_val 同为港元，
    比值无量纲。若未来发现东财港股财务以其他币种回灌，需在此加币种换算。

落库策略（关键·只补 PS/PCF 两列，不碰 OHLC/PE/PB/市值）：
    hk_daily_quote.ps_ttm_ratio / pcf_ttm_ratio。
    沿用 backfill_a_valuation.py 的 upsert 防护：bulk_upsert(skip_null_updates=True)
    → 新值为空则保留库中原值（COALESCE(EXCLUDED.col, 原值)），不把已有有效值抹成 NULL。
    hk_daily_quote 表本身由 get_hk_market_turnover.py 负责建表与日线/市值落库，本脚本只加两列。

交易日枚举：
    默认从 hk_daily_quote 已存在的 trade_date 去重取（确定有行情数据），逐日回溯 PS/PCF。
    支持 --date 单日 / --start --end 区间 / --all 全部已存在交易日 / --limit N 限量。

断点续采：
    --resume 跳过已完成交易日（该日 ps_ttm_ratio 非空股票数已 ≥ 有市值股票数）。
    进程被 kill 的最后一天可能只算了一半，--resume 会重算（upsert 幂等）。

用法：
    python3 backfill_hk_valuation.py                      # 回填全部已有交易日
    python3 backfill_hk_valuation.py --date 2026-08-27    # 单日
    python3 backfill_hk_valuation.py --start 2024-01-01 --end 2026-08-27
    python3 backfill_hk_valuation.py --resume --limit 30  # 续采本轮 30 天
    python3 backfill_hk_valuation.py --dry-run            # 只探测不落库
"""
import sys
import time
import bisect
import logging
from datetime import date

from db import get_conn, bulk_upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_hk_valuation")

# TTM 滚动覆盖的财务报告类型（季度粒度；年报 12-31 也归于此，4 季恰为一个财年）
_QTYPES = ("Q1", "H1", "Q3", "annual")


# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
def parse_args():
    dry = "--dry-run" in sys.argv
    resume = "--resume" in sys.argv
    one_day = None
    start = end = None
    limit = None
    if "--date" in sys.argv:
        one_day = date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    if "--start" in sys.argv:
        start = date.fromisoformat(sys.argv[sys.argv.index("--start") + 1])
    if "--end" in sys.argv:
        end = date.fromisoformat(sys.argv[sys.argv.index("--end") + 1])
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    return {
        "dry": dry, "resume": resume, "date": one_day,
        "start": start, "end": end, "limit": limit,
    }


# ----------------------------------------------------------------------------
# 建表/补列
# ----------------------------------------------------------------------------
def _ensure_table():
    """确保 hk_daily_quote 含 ps_ttm_ratio / pcf_ttm_ratio 两列（幂等）。

    hk_daily_quote 表结构本身由 get_hk_market_turnover.py 维护，本脚本只负责补这两列。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            for col in ("ps_ttm_ratio", "pcf_ttm_ratio"):
                cur.execute(
                    f"ALTER TABLE hk_daily_quote "
                    f"ADD COLUMN IF NOT EXISTS {col} NUMERIC(12,4)"
                )
        conn.commit()


# ----------------------------------------------------------------------------
# 交易日枚举
# ----------------------------------------------------------------------------
def _existing_trade_dates(start=None, end=None):
    """从 hk_daily_quote 已落库的 trade_date 去重取（确定有行情/市值数据）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if start and end:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM hk_daily_quote "
                    "WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date",
                    (start, end))
            elif start:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM hk_daily_quote "
                    "WHERE trade_date >= %s ORDER BY trade_date", (start,))
            elif end:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM hk_daily_quote "
                    "WHERE trade_date <= %s ORDER BY trade_date", (end,))
            else:
                cur.execute(
                    "SELECT DISTINCT trade_date FROM hk_daily_quote ORDER BY trade_date")
            return [r[0] for r in cur.fetchall()]


def _done_dates():
    """已完成 PS 回溯的交易日：{trade_date: (ps非空数, 有市值数)}。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date,"
                " COUNT(*) FILTER (WHERE ps_ttm_ratio IS NOT NULL),"
                " COUNT(*) FILTER (WHERE total_market_val IS NOT NULL)"
                " FROM hk_daily_quote GROUP BY trade_date")
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


# ----------------------------------------------------------------------------
# TTM 财务预计算（全港股一次，按股票分组存有序列表）
# ----------------------------------------------------------------------------
def _load_ttm():
    """返回 {stock_code: [(report_date, rev_ttm, ocf_ttm), ...]}（按 report_date 升序）。

    rev_ttm/ocf_ttm = 该报告期及其前 3 个季度报告期的 revenue/operating_cash_flow 之和
    （SQL 窗口 ROWS BETWEEN 3 PRECEDING AND CURRENT ROW）。
    仅港股、仅季度粒度、且 revenue 非空（避免分母为空的报告污染 TTM）。
    """
    sql = """
        SELECT stock_code, report_date,
               SUM(revenue) OVER w              AS rev_ttm,
               SUM(operating_cash_flow) OVER w  AS ocf_ttm
        FROM financial_indicator
        WHERE stock_code LIKE 'HK.%%'
          AND report_type IN ('Q1','H1','Q3','annual')
          AND revenue IS NOT NULL
        WINDOW w AS (PARTITION BY stock_code ORDER BY report_date
                     ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
        ORDER BY stock_code, report_date
    """
    ttm = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for code, rd, rev, ocf in cur.fetchall():
                ttm.setdefault(code, []).append((rd, rev, ocf))
    log.info(f"TTM 财务预计算完成：{len(ttm)} 只港股有可用季度财务")
    return ttm


# ----------------------------------------------------------------------------
# 单交易日 PS/PCF 计算
# ----------------------------------------------------------------------------
def _compute_for_date(td, ttm):
    """对单交易日全港股计算 PS/PCF，返回待 upsert 的 record 列表。

    只处理 hk_daily_quote 中 total_market_val 非空的行；对每只股票取
    「截至 td 最新的报告期」对应的 TTM 营收/经营现金流来现算。
    分母为 None 或 0 → 该指标留 NULL（不写 0/Inf），由 upsert 字段级防护保留库中原值。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, total_market_val FROM hk_daily_quote "
                "WHERE trade_date = %s AND total_market_val IS NOT NULL",
                (td,))
            mkt = cur.fetchall()

    rows = []
    for code, mv in mkt:
        series = ttm.get(code)
        if not series:
            continue
        dates = [s[0] for s in series]
        # 最新 report_date <= td 的位置
        idx = bisect.bisect_right(dates, td) - 1
        if idx < 0:
            continue
        rev_ttm, ocf_ttm = series[idx][1], series[idx][2]
        rec = {"stock_code": code, "trade_date": td}
        if rev_ttm is not None and float(rev_ttm) != 0:
            rec["ps_ttm_ratio"] = round(float(mv) / float(rev_ttm), 4)
        else:
            rec["ps_ttm_ratio"] = None
        if ocf_ttm is not None and float(ocf_ttm) != 0:
            rec["pcf_ttm_ratio"] = round(float(mv) / float(ocf_ttm), 4)
        else:
            rec["pcf_ttm_ratio"] = None
        rows.append(rec)
    return rows


# ----------------------------------------------------------------------------
# 落库（字段级防护 upsert，只更新 ps/pcf 两列）
# ----------------------------------------------------------------------------
def _flush(rows, dry):
    if not rows:
        return 0
    if dry:
        return len(rows)
    _ensure_table()
    with get_conn() as conn:
        bulk_upsert(conn, "hk_daily_quote", rows,
                    conflict_cols=["stock_code", "trade_date"],
                    skip_null_updates=True)
    return len(rows)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run_valuation(dry=False, resume=False, date=None, start=None, end=None, limit=None):
    # 先确保 ps/pcf 两列存在（幂等 ALTER），否则后续 _done_dates / upsert 会因缺列报错
    _ensure_table()

    if date:
        dates = [date]
        log.info(f"单日模式: {date}")
    else:
        dates = _existing_trade_dates(start, end)
        if not dates:
            log.warning("hk_daily_quote 无已有交易日，无法枚举。请先跑 get_hk_market_turnover.py，或用 --date 指定。")
            return
        log.info(f"枚举到 {len(dates)} 个已有交易日，范围 {dates[0]} ~ {dates[-1]}")

    if resume:
        done = _done_dates()
        before = len(dates)
        dates = [d for d in dates if done.get(d, (0, 0))[0] < done.get(d, (0, 0))[1]]
        log.info(f"续采：已完成 {before - len(dates)} 天，剩 {len(dates)} 天待处理")

    if limit and len(dates) > limit:
        log.info(f"本轮限量 {limit} 天（剩余 {len(dates) - limit} 天下次 --resume）")
        dates = dates[:limit]

    if not dates:
        log.info("无待处理交易日")
        return

    # TTM 财务全量预计算一次（与交易日数无关，避免每日重复扫财务表）
    ttm = _load_ttm()

    total_rows = 0
    fail_days = 0
    log.info(f"开始回溯港股 PS/PCF（{len(dates)} 天，dry={dry}）...")
    for i, td in enumerate(dates, 1):
        log.info(f"  [{i}/{len(dates)}] {td} 计算中...")
        try:
            rows = _compute_for_date(td, ttm)
        except Exception as e:
            fail_days += 1
            log.warning(f"  {td} 计算失败: {type(e).__name__}: {e}")
            continue
        n = _flush(rows, dry)
        total_rows += n
        log.info(f"  [{i}/{len(dates)}] {td} PS/PCF {n} 只" +
                 ("（dry-run 未落库）" if dry else "已落库"))
        time.sleep(0.1)

    log.info(f"回溯完成：{len(dates) - fail_days} 天成功，{fail_days} 天失败，PS/PCF 记录 {total_rows} 条")


def run():
    """命令行入口：解析参数后调用 run_valuation。"""
    a = parse_args()
    run_valuation(**a)


if __name__ == "__main__":
    import os
    run()
    os._exit(0)
