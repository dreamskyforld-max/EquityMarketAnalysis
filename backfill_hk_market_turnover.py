#!/usr/bin/env python3
"""
港股全市场总成交额 · 历史回溯（新浪 stock_hk_daily 版，一次性全量）

数据源：AKShare stock_hk_daily()（新浪财经港股个股日线，完整历史含 amount）
用途：遍历全港股，逐只拉历史日线成交额，按日 SUM → 全市场历史总成交额，
      回溯写入 daily_market_turnover；同时个股日线写入 hk_daily_quote。

为什么用新浪而不是富途历史 K 线：
  - 富途 request_history_kline 有「历史 K 线额度」限制（100 只/7天），
    全港股 2800+ 只需 28 周才能采完，无法做全量回溯。
  - 新浪 stock_hk_daily 无额度限制，能一次拿完整历史（仅耗时较长）。

字段说明：新浪仅返回基础量价（date/open/high/low/close/volume/amount），
  估值字段（市值/PE/PB/换手率等）新浪不提供，统一置空（NULL），
  这些字段由日常采集（get_hk_market_turnover.py 富途快照）当天补上。

用法：
    python3 backfill_hk_market_turnover.py --years 3                 # 全港股回溯近 3 年
    python3 backfill_hk_market_turnover.py --start 2023-01-01        # 全港股，指定起始日期
    python3 backfill_hk_market_turnover.py --code HK.01857           # 仅采集单只股票（可省略 HK. 前缀）
    python3 backfill_hk_market_turnover.py --code 01857 --start 2023-01-01  # 单只 + 起始日期
    python3 backfill_hk_market_turnover.py --code HK.01857 --dry-run # 只看不落库

分批采集（小内存服务器推荐）：
    # 每轮只采 300 只，跑完自动记录进度；重复执行直到提示「全部采集完成」
    python3 backfill_hk_market_turnover.py --years 3 --resume --limit 300
    # 查看进度 / 采完后校正总成交额（不采集，秒级）
    python3 backfill_hk_market_turnover.py --years 3 --aggregate-only
    # 只要全市场总额、不要个股日线明细（内存与 PG 写入最省）
    python3 backfill_hk_market_turnover.py --years 3 --no-quotes

断点续采说明：
    --resume 的进度来源是 hk_daily_quote 表本身（不用进度文件），
    因此即使进程被 OOM kill / 断电，下次仍能准确跳过已采完的股票。
    上次中断处的那只股票会被「重新采集」一遍（可能只落了部分批次），
    upsert 幂等，重采不会产生重复行。

耗时：全港股 2800+ 只 × 逐只拉历史，约 40-60 分钟（分批则按轮次摊开）。
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
log = logging.getLogger("backfill_hk_market_turnover")


def parse_args() -> "dict[str, object]":
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
        if not code.startswith("HK."):
            code = f"HK.{code}"
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
        "dry": dry,
        "start": start,
        "code": code,
        "resume": resume,
        "no_quotes": no_quotes,
        "aggregate_only": aggregate_only,
        "limit": limit,
    }


def _hk_code_list():
    # 复用 get_hk_market_turnover 的缓存版（带 7 天数据库缓存）
    from get_hk_market_turnover import _hk_code_list as _cached
    return _cached()


def _done_codes(start: date):
    """已采集完成的股票代码集合（从 hk_daily_quote 反推，无需进度文件）。

    断点续采的「进度」直接由落库结果决定：hk_daily_quote 有
    UNIQUE(stock_code, trade_date)，某只股票在 [start, ~] 区间已有行
    即视为已采完。这样即使进程被 OOM kill（无机会写进度文件），
    下次 --resume 仍能准确跳过。

    注意：最后一只可能只落了部分批次（被 kill 在 flush 中间），
    因此调用方会把「最大 stock_code」排除掉重采一遍（upsert 幂等）。
    """
    from db import get_conn
    from get_hk_market_turnover import _ensure_table
    _ensure_table("hk_daily_quote")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, COUNT(*), MAX(created_at) "
                "FROM hk_daily_quote WHERE trade_date >= %s "
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


def _fetch_daily(sym):
    """新浪 stock_hk_daily 拉单只完整历史，返回 DataFrame（date 已转 date 类型）。"""
    import akshare as ak
    df = ak.stock_hk_daily(symbol=sym, adjust="")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    return df


def fetch_history(start: date, code: str | None = None, quote_sink=None,
                  resume: bool = False, limit: int | None = None):
    """逐只拉历史日线，返回总成交额 DataFrame。

    code: 指定单只股票（如 HK.01857），None=全港股。
    quote_sink: 可选回调 fn(rows: list[dict])，每只股票采完后立即调用以落库个股日线。
                为 None 时不保留个股日线（避免全量驻留内存）。
    resume: 从上次中断处续采（跳过 hk_daily_quote 已有的股票）。
    limit: 本轮最多采集多少只（分批跑完全市场）。

    内存说明：个股日线是流式处理（每只采完即交给 quote_sink 并释放），
    仅 agg 按交易日聚合常驻（≈730 个键，可忽略）。
    """
    if code:
        codes = [code]
    else:
        codes = _hk_code_list()
    if not codes:
        log.warning("获取港股代码列表失败")
        return pd.DataFrame()

    # 断点续采：跳过已完成的股票；中断处那只重采（可能只落了部分批次）
    if resume and not code:
        done, last = _done_codes(start)
        if done:
            retry = ""
            if last and last in done:
                done = done - {last}
                retry = f"，中断处 {last} 重新采集"
            before = len(codes)
            codes = [c for c in codes
                     if (c if c.startswith("HK.") else f"HK.{c}") not in done]
            log.info(f"续采：已完成 {len(done)} 只，跳过后剩 {len(codes)}/{before} 只{retry}")
        else:
            log.info("续采：无已完成记录，从头开始")

    if limit and len(codes) > limit:
        log.info(f"本轮限量 {limit} 只（剩余 {len(codes) - limit} 只下次继续）")
        codes = codes[:limit]

    if not codes:
        log.info("全部股票已采集完成，无需续采")
        return pd.DataFrame()

    agg = {}
    fail = 0
    total_quotes = 0

    log.info(f"开始回溯 {len(codes)} 只港股历史日线（起始 {start}）...")
    for i, raw in enumerate(codes, 1):
        full = raw if raw.startswith("HK.") else f"HK.{raw}"  # 统一带 HK. 前缀
        sym = full.split(".")[-1]  # 纯数字，如 01857
        log.info(f"  [{i}/{len(codes)}] {full} 开始采集...")
        try:
            df = _fetch_daily(sym)
            if df.empty:
                fail += 1
                log.info(f"  [{i}/{len(codes)}] {full} 无数据")
                continue
            df = df[df["trade_date"] >= start]
            rows = []
            # itertuples 比 iterrows 省内存/更快（不为每行构造 Series）
            for r in df.itertuples(index=False):
                d = r._asdict()
                td = d["trade_date"]
                amt = float(d["amount"]) if d.get("amount") is not None else 0.0
                vol = int(d["volume"]) if d.get("volume") is not None else 0
                a = agg.setdefault(td, {"turnover": 0.0, "volume": 0, "stocks": 0})
                a["turnover"] += amt
                a["volume"] += vol
                # 同一只股票同一交易日仅一行，直接计数即可（无需 set 去重）
                a["stocks"] += 1
                rows.append(_map_quote_row(full, td, d))
            n = len(rows)
            total_quotes += n
            if quote_sink and rows:
                quote_sink(rows)
            del rows, df  # 及早释放，个股日线不跨股票累积
            log.info(f"  [{i}/{len(codes)}] {full} 完成，{n} 条日线")
        except Exception as e:
            fail += 1
            if fail <= 5:
                log.warning(f"  [{i}/{len(codes)}] {full} 失败: {e}")
        time.sleep(0.1)

    log.info(
        f"回溯完成：成功 {len(codes)-fail} 只，失败 {fail} 只，"
        f"交易日 {len(agg)} 个，个股日线 {total_quotes} 条"
    )

    rows = []
    for td in sorted(agg):
        a = agg[td]
        rows.append({
            "trade_date": td,
            "total_turnover": a["turnover"],
            "total_volume": a["volume"],
            "stock_count": a["stocks"],
        })
    return pd.DataFrame(rows)


def _map_quote_row(code, td, row):
    """新浪 stock_hk_daily 行 → hk_daily_quote 字段（估值字段置空）。"""
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
        # 新浪不提供估值/换手率/量比，置空；由日常富途快照补上
        "turnover_rate": None,
        "volume_ratio": None,
        "high_52w": None,
        "low_52w": None,
        "total_market_val": None,
        "circular_market_val": None,
        "pe_ratio": None,
        "pe_ttm_ratio": None,
        "pb_ratio": None,
        "dividend_ratio_ttm": None,
        # 历史日线无实时更新时间，用交易日作为数据更新时间
        "update_time": str(td),
    }


def aggregate_from_db(start: date):
    """在 PG 内按 trade_date 重算全市场总成交额 → daily_market_turnover。

    为什么不用内存里的 agg：分批/续采时单轮 agg 只含本轮股票，
    直接落库会写出「残缺的总成交额」（错误值且难以察觉）。改为每轮结束后
    对 hk_daily_quote 做 GROUP BY 重算，结果与「一次跑完」完全一致，
    天然幂等 —— 分几轮跑都不会算错。聚合在 PG 内完成，Python 侧零内存。
    """
    from db import get_conn
    from get_hk_market_turnover import _ensure_table
    _ensure_table("daily_market_turnover")
    sql = """
        INSERT INTO daily_market_turnover
            (trade_date, snapshot_time, total_turnover, total_volume, stock_count)
        SELECT trade_date, NOW(),
               COALESCE(SUM(amount), 0),
               COALESCE(SUM(volume), 0),
               COUNT(DISTINCT stock_code)
        FROM hk_daily_quote
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
    log.info(f"总成交额已在库内重算：{n} 个交易日（daily_market_turnover）")
    return n


def progress_report(start: date):
    """打印续采进度（已完成 / 总数）。"""
    try:
        codes = _hk_code_list()
        done, _ = _done_codes(start)
        total = len(codes)
        n = len({c if c.startswith("HK.") else f"HK.{c}" for c in codes} & done)
        pct = n / total * 100 if total else 0
        log.info(f"进度：{n}/{total} 只已采集（{pct:.1f}%），剩余 {total - n} 只")
    except Exception as e:
        log.warning(f"进度统计失败: {e}")


def save_to_db(df: pd.DataFrame):
    from db import get_conn, bulk_upsert
    if df.empty:
        return
    now = datetime.now()
    data = []
    for r in df.itertuples(index=False):
        data.append({
            "trade_date": r.trade_date,
            "snapshot_time": now,
            "total_turnover": float(r.total_turnover),
            "total_volume": int(r.total_volume),
            "stock_count": int(r.stock_count),
        })
    # 表不存在时自动创建（复用 get_hk_market_turnover 的 DDL）
    from get_hk_market_turnover import _ensure_table
    _ensure_table("daily_market_turnover")
    with get_conn() as conn:
        bulk_upsert(conn, "daily_market_turnover", data, conflict_cols=["trade_date"])
    log.info(f"落库 {len(data)} 个交易日")


class _QuoteWriter:
    """个股日线流式写入器：攒满 batch 条即 upsert，避免全量驻留内存。"""

    BATCH = 5000

    def __init__(self):
        from db import get_conn, bulk_upsert
        from get_hk_market_turnover import _ensure_table
        _ensure_table("hk_daily_quote")
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
        log.info(f"个股日线落库 {self.total} 条（hk_daily_quote）")

    def _flush(self, chunk):
        with self._get_conn() as conn:
            self._bulk_upsert(conn, "hk_daily_quote", chunk,
                              conflict_cols=["stock_code", "trade_date"])
        self.total += len(chunk)
        log.info(f"  个股日线已落库 {self.total} 条")


def run():
    a = parse_args()
    dry: bool = bool(a["dry"])
    start: date = a["start"]  # type: ignore[assignment]
    code: str | None = a["code"]  # type: ignore[assignment]
    resume: bool = bool(a["resume"])
    limit: int | None = a["limit"]  # type: ignore[assignment]

    # 只重算总成交额（不采集）——分批采完后补算，或任何时候校正
    if a["aggregate_only"]:
        log.info(f"仅重算总成交额（起始 {start}）")
        aggregate_from_db(start)
        progress_report(start)
        return

    log.info(
        f"回溯起始日期: {start}，dry_run={dry}，code={code or '全港股'}，"
        f"resume={resume}，limit={limit or '不限'}"
    )
    writer = None if (dry or a["no_quotes"]) else _QuoteWriter()
    interrupted = False
    try:
        df = fetch_history(start, code, quote_sink=writer,
                           resume=resume, limit=limit)
    except KeyboardInterrupt:
        # Ctrl+C：已落库的部分保留，下次 --resume 继续
        interrupted = True
        df = pd.DataFrame()
        log.warning("收到中断信号，已采集部分已落库，可用 --resume 继续")
    finally:
        if writer:
            writer.close()

    if not dry:
        # 总成交额一律在库内重算，不用内存 agg：
        #  - 全市场分批/续采时，单轮 agg 只含本轮股票；
        #  - 单只模式下 agg 只含这一只（原实现会把全市场总额覆盖成单只，是 bug）。
        # 库内 GROUP BY 重算在两种模式下都得到正确的全市场口径。
        aggregate_from_db(start)
        if not code:
            progress_report(start)

    if df.empty and not interrupted:
        log.info("本轮无新增数据")
    elif not df.empty:
        print(df.sort_values("trade_date").tail(10).to_string(index=False))
    log.info("完成")


if __name__ == "__main__":
    import os
    run()
    # 独立运行：新浪/akshare 可能残留非 daemon 线程，显式退出。
    os._exit(0)
