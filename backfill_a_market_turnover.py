#!/usr/bin/env python3
"""
A股全市场总成交额 · 历史回溯（akshare stock_zh_a_hist 版，一次性全量）

数据源：AKShare stock_zh_a_daily()（新浪财经 A股个股日线，完整历史含 amount/volume/turnover）
用途：遍历全 A 股（SH+SZ），逐只拉历史日线成交额，按日 SUM → 全市场历史总成交额，
      回溯写入 a_daily_market_turnover；同时个股日线写入 a_daily_quote。

为什么用新浪而不是富途历史 K 线 / 东财：
  - 富途 request_history_kline 有「历史 K 线额度」限制（约 100 只/7天），
    全 A 5000+ 需 50+ 轮 × 7天 ≈ 1 年才能采完，无法做全量回溯。
  - akshare stock_zh_a_hist（东财源）实测网络不稳定（ConnectionError 频发）。
  - akshare stock_zh_a_daily（新浪源）无该额度限制、实测稳定，能一次拿完整历史
    （含 date/open/high/low/close/volume/amount/turnover 等字段），全 A 约 1-2 小时跑完。
    这与港股版 backfill_market_turnover.py 用新浪（stock_hk_daily）的思路一致。

字段说明：东财仅返回基础量价 + 换手率，估值字段（市值/PE/PB/52w 等）东财不在此接口提供，
  统一置空（NULL）；这些字段由日常采集（get_a_market_turnover.py 富途快照）当天补上。

断点续采说明（与港股版 backfill_market_turnover.py 一致）：
  - --resume 的进度来源是 a_daily_quote 表本身（不用进度文件/进度表），
    因此即使进程被 OOM kill / 断电，下次仍能准确跳过已采完的股票。
  - 上次中断处的那只股票会被「重新采集」一遍（可能只落了部分批次），
    upsert 幂等，重采不会产生重复行。

用法：
    python3 backfill_a_market_turnover.py --years 3                 # 全 A 回溯近 3 年
    python3 backfill_a_market_turnover.py --start 2023-01-01        # 指定起始日期
    python3 backfill_a_market_turnover.py --code SH.600900          # 仅单只（可省略 SH. 前缀）
    python3 backfill_a_market_turnover.py --code 600900 --dry-run   # 只看不落库
    python3 backfill_a_market_turnover.py --resume --limit 300      # 续采本轮 300 只
    python3 backfill_a_market_turnover.py --aggregate-only          # 仅库内重算总额，不采集
    python3 backfill_a_market_turnover.py --no-quotes               # 只要总额，不落个股日线

耗时：全 A 5000+ 只 × 逐只拉历史（akshare 东财），约 1-2 小时（分批则按轮次摊开）。
"""
import sys
import time
import logging
from datetime import datetime, date

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill_a_market_turnover")


# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
def parse_args():
    dry = "--dry-run" in sys.argv
    resume = "--resume" in sys.argv
    no_quotes = "--no-quotes" in sys.argv
    aggregate_only = "--aggregate-only" in sys.argv
    start = None
    code = None
    limit = None
    if "--code" in sys.argv:
        i = sys.argv.index("--code")
        code = sys.argv[i + 1]
        if not code.startswith(("SH.", "SZ.")):
            code = f"SH.{code}" if code.startswith("6") else f"SZ.{code}"
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        limit = int(sys.argv[i + 1])
    if "--years" in sys.argv:
        i = sys.argv.index("--years")
        years = int(sys.argv[i + 1])
        start = date(datetime.now().year - years, 1, 1)
    elif "--start" in sys.argv:
        i = sys.argv.index("--start")
        start = date.fromisoformat(sys.argv[i + 1])
    if start is None:
        start = date(datetime.now().year - 3, 1, 1)
    return {
        "dry": dry, "start": start, "code": code, "resume": resume,
        "no_quotes": no_quotes, "aggregate_only": aggregate_only, "limit": limit,
    }


# ----------------------------------------------------------------------------
# 代码清单（富途 get_stock_basicinfo，带缓存）
# ----------------------------------------------------------------------------
def _a_code_list():
    from get_a_market_turnover import _a_code_list as _cached
    return _cached()


def _ensure_progress_table():
    # 进度由 a_daily_quote 落库结果反推（与港股版一致），无需独立进度表。
    # 保留占位以兼容存量调用，实际不再创建额外表。
    return


def _done_codes(start):
    """已采集完成的股票代码集合（从 a_daily_quote 反推，无需进度文件）。

    断点续采的「进度」直接由落库结果决定：a_daily_quote 有
    UNIQUE(stock_code, trade_date)，某只股票在 [start, ~] 区间已有行
    即视为已采完。这样即使进程被 OOM kill（无机会写进度），
    下次 --resume 仍能准确跳过。

    注意：最后一只可能只落了部分批次（被 kill 在 flush 中间），
    因此调用方会把「最大 stock_code」排除掉重采一遍（upsert 幂等）。
    """
    from db import get_conn
    from get_a_market_turnover import _ensure_table
    _ensure_table("a_daily_quote")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, COUNT(*), MAX(created_at) "
                "FROM a_daily_quote WHERE trade_date >= %s "
                "GROUP BY stock_code",
                (start,),
            )
            rows = cur.fetchall()
    if not rows:
        return set(), None
    # 找出 created_at 最新的那只 —— 它就是上次中断处，可能只落了一半，需重采
    last = max(rows, key=lambda r: (r[2] or datetime.min))
    done = {r[0] for r in rows}
    return done, last[0]


def _mark_done(code):
    # 进度由落库反推，无需显式标记。占位兼容旧调用。
    return


# ----------------------------------------------------------------------------
# 单只历史 K 线
# ----------------------------------------------------------------------------
def _fetch_kline(full, start):
    """akshare stock_zh_a_daily（新浪）拉单只完整历史日线，返回 DataFrame（trade_date 已转 date）。

    full 为带交易所前缀的完整代码（如 SH.600900 / SH.900901 / SZ.200011）。
    新浪源无富途历史 K 线额度限制，可一次拿完整历史，且实测比东财源稳定。
    列名（英文）：date/open/high/low/close/volume/amount/outstanding_share/turnover。
      - amount   = 成交额（元）
      - volume   = 成交量（股）
      - turnover = 换手率（小数，如 0.003 → 0.3%）
    新浪代码前缀按【交易所】映射（SH→sh, SZ→sz），不能用数字开头判断——
    否则 B 股（900/200 开头）、沪伦通 CDR（689/700 等）会被错分到 sz 而查不到。
    注意：新浪接口对未上市/退市的代码可能返回空，调用方按「无数据」跳过。
    """
    import akshare as ak
    market, num = full.split(".", 1)
    sina_sym = f"sh{num}" if market == "SH" else f"sz{num}"
    for attempt in range(5):
        try:
            df = ak.stock_zh_a_daily(symbol=sina_sym, adjust="")
            if df is not None and not df.empty:
                break
        except Exception:
            if attempt < 2:
                time.sleep(2)
    else:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    return df[df["trade_date"] >= start]


# ----------------------------------------------------------------------------
# 主回溯
# ----------------------------------------------------------------------------
def fetch_history(start, code=None, quote_sink=None, resume=False, limit=None):
    if code:
        codes = [code]
    else:
        codes = _a_code_list()
    if not codes:
        log.warning("获取 A 股代码列表失败")
        return pd.DataFrame()

    if resume and not code:
        done, last = _done_codes(start)
        if done:
            retry = ""
            if last and last in done:
                done = done - {last}
                retry = f"，中断处 {last} 重新采集"
            before = len(codes)
            codes = [c for c in codes if c not in done]
            log.info(f"续采：已完成 {len(done)} 只，跳过后剩 {len(codes)}/{before} 只{retry}")
        else:
            log.info("续采：无已完成记录，从头开始")

    if limit and len(codes) > limit:
        log.info(f"本轮限量 {limit} 只（剩余 {len(codes) - limit} 只下次 --resume）")
        codes = codes[:limit]

    if not codes:
        log.info("全部股票已采集完成，无需续采")
        return pd.DataFrame()

    agg = {}  # 仅用于打印交易日数统计，不累积个股明细（个股明细由 quote_sink 流式落库）
    fail = 0
    total_quotes = 0

    log.info(f"开始回溯 {len(codes)} 只 A 股历史日线（起始 {start}）...")
    for i, full in enumerate(codes, 1):
        log.info(f"  [{i}/{len(codes)}] {full} 开始采集...")
        try:
            df = _fetch_kline(full, start)
            if df.empty:
                fail += 1
                log.info(f"  [{i}/{len(codes)}] {full} 无数据")
                continue
            rows = []
            for r in df.itertuples(index=False):
                d = r._asdict()
                td = d["trade_date"]
                amt = float(d["amount"]) if d.get("amount") is not None else 0.0
                vol = int(d["volume"]) if d.get("volume") is not None else 0
                a = agg.setdefault(td, {"stocks": 0})
                a["stocks"] += 1
                rows.append(_map_quote_row(full, td, d))
            n = len(rows)
            total_quotes += n
            if quote_sink and rows:
                # 流式写入：每只股票采完立即交给 writer，攒满批次即 upsert 落库，
                # 个股日线不跨股票累积在内存（避免 3 年全 A 5000+ 只 OOM）。
                quote_sink(rows)
            del rows, df
            log.info(f"  [{i}/{len(codes)}] {full} 完成，{n} 条日线")
        except Exception as e:
            fail += 1
            if fail <= 5:
                log.warning(f"  [{i}/{len(codes)}] {full} 失败: {e}")
        time.sleep(0.1)  # 轻量限速，保护新浪源

    log.info(
        f"回溯完成：成功 {len(codes)-fail} 只，失败 {fail} 只，"
        f"交易日 {len(agg)} 个，个股日线 {total_quotes} 条"
    )
    # 注意：本函数不再返回总成交 DataFrame。
    # 总成交额一律由 aggregate_from_db() 在 PG 内 GROUP BY 重算（run() 中调用），
    # 既避免内存持有完整聚合，又保证分批/续采时总额正确（与港股版一致）。
    return None


def _map_quote_row(code, td, row):
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "stock_code": code,
        "trade_date": td,
        "open": _f(row.get("open")),
        "high": _f(row.get("high")),
        "low": _f(row.get("low")),
        "close": _f(row.get("close")),
        "volume": int(row["volume"]) if row.get("volume") is not None else 0,
        "amount": _f(row.get("amount")),
        # 新浪 turnover 是换手率（小数，如 0.003 → 0.3%）
        "turnover_rate": round(_f(row.get("turnover")) * 100, 4)
                          if _f(row.get("turnover")) is not None else None,
        # 新浪不提供量比/52w/总市值/PE/PB/股息，置空；
        # 这些字段由日常富途快照（get_a_market_turnover.py）当天补上。
        # 注：流通市值可由 close × outstanding_share 估算，但为与港股版字段口径一致，
        # 此处仍置空，避免引入与富途快照不一致的估值口径。
        "volume_ratio": None,
        "high_52w": None,
        "low_52w": None,
        "total_market_val": None,
        "circular_market_val": None,
        "pe_ratio": None,
        "pe_ttm_ratio": None,
        "pb_ratio": None,
        "dividend_ratio_ttm": None,
        "update_time": str(td),
    }


# ----------------------------------------------------------------------------
# 库内重算总成交额（与港股版一致：分批/续采安全，单轮 agg 仅含本轮股票）
# ----------------------------------------------------------------------------
def aggregate_from_db(start):
    from db import get_conn
    from get_a_market_turnover import _ensure_table
    _ensure_table("a_daily_market_turnover")
    sql = """
        INSERT INTO a_daily_market_turnover
            (trade_date, snapshot_time, total_turnover, total_volume, stock_count)
        SELECT trade_date, NOW(),
               COALESCE(SUM(amount), 0),
               COALESCE(SUM(volume), 0),
               COUNT(DISTINCT stock_code)
        FROM a_daily_quote
        WHERE trade_date >= %s
        GROUP BY trade_date
        ON CONFLICT (trade_date) DO UPDATE SET
            snapshot_time  = EXCLUDED.snapshot_time,
            total_turnover = EXCLUDED.total_turnover,
            total_volume   = EXCLUDED.total_volume,
            stock_count    = EXCLUDED.stock_count
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (start,))
            n = cur.rowcount
    log.info(f"总成交额已在库内重算：{n} 个交易日（a_daily_market_turnover）")
    return n


def progress_report(start):
    try:
        codes = _a_code_list()
        done, _ = _done_codes(start)
        total = len(codes)
        n = len(set(codes) & done)
        pct = n / total * 100 if total else 0
        log.info(f"进度：{n}/{total} 只已采集（{pct:.1f}%），剩余 {total - n} 只")
    except Exception as e:
        log.warning(f"进度统计失败: {e}")


def save_to_db(df):
    if df.empty:
        return
    from db import get_conn, bulk_upsert
    from get_a_market_turnover import _ensure_table
    now = datetime.now()
    data = [{
        "trade_date": r.trade_date,
        "snapshot_time": now,
        "total_turnover": float(r.total_turnover),
        "total_volume": int(r.total_volume),
        "stock_count": int(r.stock_count),
    } for r in df.itertuples(index=False)]
    _ensure_table("a_daily_market_turnover")
    with get_conn() as conn:
        bulk_upsert(conn, "a_daily_market_turnover", data, conflict_cols=["trade_date"])
    log.info(f"落库 {len(data)} 个交易日")


class _QuoteWriter:
    BATCH = 5000

    def __init__(self):
        from db import get_conn, bulk_upsert
        from get_a_market_turnover import _ensure_table
        _ensure_table("a_daily_quote")
        self._get_conn = get_conn
        self._bulk_upsert = bulk_upsert
        self._buf = []
        self.total = 0

    def __call__(self, rows):
        self._buf.extend(rows)
        while len(self._buf) >= self.BATCH:
            self._flush(self._buf[:self.BATCH])
            del self._buf[:self.BATCH]

    def close(self):
        if self._buf:
            self._flush(self._buf)
            self._buf = []
        log.info(f"个股日线落库 {self.total} 条（a_daily_quote）")

    def _flush(self, chunk):
        with self._get_conn() as conn:
            self._bulk_upsert(conn, "a_daily_quote", chunk,
                              conflict_cols=["stock_code", "trade_date"])
        self.total += len(chunk)
        log.info(f"  个股日线已落库 {self.total} 条")


def run():
    a = parse_args()
    dry = bool(a["dry"])
    start = a["start"]
    code = a["code"]
    resume = bool(a["resume"])
    limit = a["limit"]

    if a["aggregate_only"]:
        log.info(f"仅重算总成交额（起始 {start}）")
        aggregate_from_db(start)
        progress_report(start)
        _print_recent_totals(start)
        return

    log.info(
        f"回溯起始日期: {start}，dry_run={dry}，code={code or '全 A 股'}，"
        f"resume={resume}，limit={limit or '不限'}"
    )
    writer = None if (dry or a["no_quotes"]) else _QuoteWriter()
    interrupted = False
    try:
        fetch_history(start, code, quote_sink=writer, resume=resume, limit=limit)
    except KeyboardInterrupt:
        interrupted = True
        log.warning("收到中断信号，已采集部分已落库，可用 --resume 继续")
    finally:
        if writer:
            writer.close()

    if not dry:
        # 总成交额一律在库内重算，不用内存 agg（单轮/单只都不完整）
        aggregate_from_db(start)
        if not code:
            progress_report(start)
            _print_recent_totals(start)

    log.info("完成" if not interrupted else "已中断，可 --resume 续采")


def _print_recent_totals(start):
    """从库查询并展示最近 10 个交易日的总成交额（避免内存持有完整聚合）。"""
    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trade_date, total_turnover, stock_count "
                    "FROM a_daily_market_turnover WHERE trade_date >= %s "
                    "ORDER BY trade_date DESC LIMIT 10",
                    (start,),
                )
                rows = cur.fetchall()
        if not rows:
            return
        print("=" * 60)
        print("A股全市场总成交额（最近 10 个交易日，库内重算）")
        print("=" * 60)
        for td, turnover, cnt in sorted(rows, key=lambda x: x[0]):
            t = float(turnover) if turnover is not None else 0.0
            print(f"  {td}  总成交额 {t/1e8:,.2f} 亿元  标的 {cnt} 只")
    except Exception as e:
        log.warning(f"查询最近总成交额失败: {e}")


if __name__ == "__main__":
    import os
    run()
    os._exit(0)
