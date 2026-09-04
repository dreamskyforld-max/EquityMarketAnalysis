#!/usr/bin/env python3
"""
A股全市场总成交额 · 历史回溯（akshare stock_zh_a_daily / 新浪源，一次性全量）

数据源：AKShare stock_zh_a_daily()（新浪财经 A股个股日线）。
       返回该代码上市以来的完整历史日线，含 amount/volume/turnover 等字段；
       无富途历史 K 线额度限制，实测稳定，全 A 约 1-2 小时跑完。

用途：遍历全 A 股（SH+SZ，代码清单取自富途 get_stock_basicinfo 当前上市列表），
       逐只拉历史日线成交额，按日 SUM → 全市场历史总成交额，写入 a_daily_market_turnover；
       同时个股日线写入 a_daily_quote。

为什么用新浪而不是富途 / 东财：
  - 富途 request_history_kline 有「历史 K 线额度」限制（约 100 只/7天），
    全 A 5000+ 需 50+ 轮 × 7天 ≈ 1 年才能采完，无法做全量回溯。
  - akshare stock_zh_a_hist（东财源）实测网络不稳定（ConnectionError 频发），不采用。
  - akshare stock_zh_a_daily（新浪源）无该额度限制、实测稳定，能一次拿完整历史，
    与港股版 backfill_hk_market_turnover.py 用新浪（stock_hk_daily）思路一致。

采集范围：
  - 默认「全量、无时间窗」：每只股票直接取新浪返回的完整上市以来日线（多多益善）。
  - 可选 --start / --years / --end 仅作调试用窗口裁剪，平时不传。
  - 估值字段（市值/PE/PB/52w 等）新浪不提供，统一置 NULL，由日常采集
    （get_a_market_turnover.py 富途快照）当天补上。

断点续采：
  - 进度记录在 a_backfill_progress 表（与采集窗口无关），status ∈ done / empty / error。
  - --resume 时跳过 status∈(done, empty) 的代码；error 状态下次重试。
  - 即使进程被 OOM kill / 断电，下次 --resume 仍能准确跳过已采完的股票；
    upsert 幂等，重采不会产生重复行。

用法：
    python3 backfill_a_market_turnover.py                       # 全 A 全历史（无时间窗）
    python3 backfill_a_market_turnover.py --start 2023-01-01    # 仅采集该日及之后（窗口裁剪）
    python3 backfill_a_market_turnover.py --years 3             # 今年往前推 3 年
    python3 backfill_a_market_turnover.py --start 2023-01-01 --end 2023-12-31  # 仅 2023 全年
    python3 backfill_a_market_turnover.py --code SH.600900      # 仅单只（可省略 SH. 前缀）
    python3 backfill_a_market_turnover.py --code 600900 --dry-run   # 只看不落库
    python3 backfill_a_market_turnover.py --resume --limit 300      # 续采本轮 300 只
    python3 backfill_a_market_turnover.py --aggregate-only          # 仅库内重算总额，不采集
    python3 backfill_a_market_turnover.py --no-quotes               # 只要总额，不落个股日线

耗时：全 A 5000+ 只 × 逐只拉历史（akshare 新浪），约 1-2 小时；分批则按轮次摊开。
"""
import sys
import time
import socket
import json
import os
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
    force = "--force" in sys.argv
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
        start = None  # 不传 --start/--years：采集每只股票完整历史（无时间窗，多多益善）
    end = None
    if "--end" in sys.argv:
        i = sys.argv.index("--end")
        end = date.fromisoformat(sys.argv[i + 1])
    return {
        "dry": dry, "start": start, "end": end, "code": code, "resume": resume,
        "no_quotes": no_quotes, "aggregate_only": aggregate_only, "limit": limit,
        "force": force,
    }


# ----------------------------------------------------------------------------
# 代码清单（富途 get_stock_basicinfo，带缓存）
# ----------------------------------------------------------------------------
def _a_code_list():
    from get_a_market_turnover import _a_code_list as _cached
    return _cached()


# 进度记录改为本地临时文件（不建库表）：{stock_code: 'done'|'empty'|'error'}
_PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "a_backfill_progress.json")


def _load_progress():
    """读取进度文件；文件缺失/损坏时返回空 dict（视为无进度）。"""
    if not os.path.exists(_PROGRESS_FILE):
        return {}
    try:
        with open(_PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"读进度文件失败（视为空）: {e}")
        return {}


def _save_progress(progress):
    try:
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"写进度文件失败: {e}")


def _mark_progress(code, status):
    """写入/更新单只代码的回补结果到本地进度文件（done / empty / error）。"""
    p = _load_progress()
    p[code] = status
    _save_progress(p)


def _done_codes():
    """已采集完成 / 确认无数据的代码集合（来自进度文件，与窗口无关）。

    断点续采时跳过这些代码；error 状态的代码不在跳过之列，下次 --resume 会重试。
    """
    p = _load_progress()
    return {c for c, s in p.items() if s in ("done", "empty")}


# ----------------------------------------------------------------------------
# 单只历史 K 线
# ----------------------------------------------------------------------------
def _fetch_kline(full, start=None, end=None):
    """akshare stock_zh_a_daily（新浪）拉单只完整历史日线，返回 DataFrame（trade_date 已转 date）。

    start / end 为日期下界 / 上界（均为闭区间）。end 为 None 表示无截止日。
    新浪源返回该代码完整上市以来日线；仅显式传入 start/end 时才按窗口截断，
    不传则保留完整历史（全量采集）。

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
    # akshare 走 urllib（requests），底层继承全局 socket 默认超时。
    # 设 30s：单只请求 30s 不返回即抛 timeout，被 fetch_history 的 try/except 捕获并记失败跳过，
    # 避免某只股票的新浪连接半开导致整进程永久冻结（见 2026-08-29 卡死排查）。
    # 仅在此调用内临时覆盖，结束即还原，避免误伤 PG 连接（bulk_upsert / aggregate_from_db）。
    _prev_to = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30)
    try:
        for attempt in range(5):
            try:
                # adjust="qfq"：前复权价，消除送股/配股/派息日的除权跳空
                # （原 adjust="" 为未复权价，是 a_daily_quote.close 除权跳变的根因）
                df = ak.stock_zh_a_daily(symbol=sina_sym, adjust="qfq")
                if df is not None and not df.empty:
                    break
            except Exception:
                if attempt < 2:
                    time.sleep(2)
        else:
            return pd.DataFrame()
    finally:
        socket.setdefaulttimeout(_prev_to)
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    # 全量采集：不传 start/end 时保留完整历史（无时间窗）；仅显式传入时才按窗口截断。
    if start is not None or end is not None:
        mask = pd.Series(True, index=df.index)
        if start is not None:
            mask &= df["trade_date"] >= start
        if end is not None:
            mask &= df["trade_date"] <= end
        df = df[mask]
    return df


# ----------------------------------------------------------------------------
# 主回溯
# ----------------------------------------------------------------------------
def _as_float(v):
    """安全转 float：非数字 / NaN / None 返回 None，否则返回数值。

    用于过滤占位行：akshare/富途对停牌或异常日可能返回 NaN、空串或其他
    非数值，直接 float() 会抛错，pd.notna 又识别不了非数值字符串。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN 不等于自身
        return None
    return f


def fetch_history(start, code=None, quote_sink=None, resume=False, limit=None, end=None, force=False):
    if code:
        codes = [code]
    else:
        codes = _a_code_list()
    if not codes:
        log.warning("获取 A 股代码列表失败")
        return pd.DataFrame()

    if not code and (resume or force):
        if force:
            # --force：忽略 a_backfill_progress，全部代码重跑并覆盖已有数据
            # （含未复权→QFQ 的 close 覆盖；估值列因本脚本产出为 NULL，
            #  bulk_upsert(skip_null_updates=True) 不会回写清空 valuation 脚本的值）
            log.info("强制重采（--force）：忽略已完成进度，全部代码重跑并覆盖")
        else:
            # 进度来自 a_backfill_progress（与窗口无关）：已 done/empty 的代码直接跳过。
            done = _done_codes()
            if done:
                before = len(codes)
                codes = [c for c in codes if c not in done]
                log.info(f"续采：已完成/跳过 {len(done)} 只，剩 {len(codes)}/{before} 只")
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
            df = _fetch_kline(full, start, end)
            if df.empty:
                fail += 1
                _mark_progress(full, "empty")
                log.info(f"  [{i}/{len(codes)}] {full} 无数据")
                continue
            rows = []
            for r in df.itertuples(index=False):
                d = r._asdict()
                td = d["trade_date"]
                # 直接判断「是否为数字且 >0」：akshare 对停牌/异常日常返回 NaN、
                # 空串或其他非数值，pd.notna 识别不了非数值字符串，NaN<=0 又恒为
                # False，都会漏过过滤把全空行写进 a_daily_quote。这里统一安全转换：
                # 非数字/NaN/None 一律视为无成交（amt=None）跳过。
                amt = _as_float(d.get("amount"))
                if amt is None or amt <= 0:
                    log.info(f"  [{i}/{len(codes)}] {full} {td} amount 非数字或<=0，跳过（无成交占位行）")
                    continue
                a = agg.setdefault(td, {"stocks": 0})
                a["stocks"] += 1
                rows.append(_map_quote_row(full, td, d))
            n = len(rows)
            total_quotes += n
            if quote_sink and rows:
                # 流式写入：每只股票采完立即交给 writer，攒满批次即 upsert 落库，
                # 个股日线不跨股票累积在内存（避免全 A 全历史 5000+ 只 OOM）。
                quote_sink(rows)
            del rows, df
            if n > 0:
                _mark_progress(full, "done")
            log.info(f"  [{i}/{len(codes)}] {full} 完成，{n} 条日线")
        except Exception as e:
            fail += 1
            _mark_progress(full, "error")
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

    _tr = _f(row.get("turnover"))
    _turnover_rate = round(_tr, 4) if (_tr is not None and 0 <= _tr < 10000) else None

    return {
        "stock_code": code,
        "trade_date": td,
        "open": _f(row.get("open")),
        "high": _f(row.get("high")),
        "low": _f(row.get("low")),
        "close": _f(row.get("close")),
        "volume": int(row["volume"]) if row.get("volume") is not None else 0,
        "amount": _f(row.get("amount")),
        # akshare turnover 已是换手率百分比数值（如 148.32 表示 148.32%），直接采信。
        # 切勿再 ×100：旧逻辑放大 100 倍会超出 numeric(8,4) 上限 → numeric overflow。
        "turnover_rate": _turnover_rate,
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
def aggregate_from_db(start, end=None):
    from db import get_conn
    from get_a_market_turnover import _ensure_table
    _ensure_table("a_daily_market_turnover")
    conds, params = [], []
    if start is not None:
        conds.append("trade_date >= %s"); params.append(start)
    if end is not None:
        conds.append("trade_date <= %s"); params.append(end)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = f"""
        INSERT INTO a_daily_market_turnover
            (trade_date, snapshot_time, total_turnover, total_volume, stock_count)
        SELECT trade_date, NOW(),
               COALESCE(SUM(amount), 0),
               COALESCE(SUM(volume), 0),
               COUNT(DISTINCT stock_code)
        FROM a_daily_quote
        {where}
        GROUP BY trade_date
        ON CONFLICT (trade_date) DO UPDATE SET
            snapshot_time  = EXCLUDED.snapshot_time,
            total_turnover = EXCLUDED.total_turnover,
            total_volume   = EXCLUDED.total_volume,
            stock_count    = EXCLUDED.stock_count
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            n = cur.rowcount
    log.info(f"总成交额已在库内重算：{n} 个交易日（a_daily_market_turnover）")
    return n


def progress_report(start, end=None):
    try:
        codes = _a_code_list()
        done = _done_codes()
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
                              conflict_cols=["stock_code", "trade_date"],
                              skip_null_updates=True)
        self.total += len(chunk)
        log.info(f"  个股日线已落库 {self.total} 条")


def run():
    a = parse_args()
    dry = bool(a["dry"])
    start = a["start"]
    end = a["end"]
    code = a["code"]
    resume = bool(a["resume"])
    force = bool(a.get("force"))
    limit = a["limit"]

    if a["aggregate_only"]:
        log.info(f"仅重算总成交额（区间 {start} ~ {end or '至今'}）")
        aggregate_from_db(start, end)
        progress_report(start, end)
        _print_recent_totals(start, end)
        return

    log.info(
        f"回溯区间: {start} ~ {end or '至今'}，dry_run={dry}，code={code or '全 A 股'}，"
        f"resume={resume}，limit={limit or '不限'}"
    )
    writer = None if (dry or a["no_quotes"]) else _QuoteWriter()
    interrupted = False
    try:
        fetch_history(start, code, quote_sink=writer, resume=resume, limit=limit, end=end, force=force)
    except KeyboardInterrupt:
        interrupted = True
        log.warning("收到中断信号，已采集部分已落库，可用 --resume 继续")
    finally:
        if writer:
            writer.close()

    if not dry:
        # 总成交额一律在库内重算，不用内存 agg（单轮/单只都不完整）
        aggregate_from_db(start, end)
        if not code:
            progress_report(start, end)
            _print_recent_totals(start, end)

    log.info("完成" if not interrupted else "已中断，可 --resume 续采")


def _print_recent_totals(start, end=None):
    """从库查询并展示最近 10 个交易日的总成交额（避免内存持有完整聚合）。"""
    from db import get_conn
    conds, params = [], []
    if start is not None:
        conds.append("trade_date >= %s"); params.append(start)
    if end is not None:
        conds.append("trade_date <= %s"); params.append(end)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trade_date, total_turnover, stock_count "
                    f"FROM a_daily_market_turnover {where} "
                    "ORDER BY trade_date DESC LIMIT 10",
                    tuple(params),
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
