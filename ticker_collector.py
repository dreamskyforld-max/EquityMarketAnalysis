#!/usr/bin/env python3
"""
逐笔成交数据采集服务（独立常驻进程，基于富途推送回调）

架构：
  FutuOpenD TICKER 推送 → TickerCollector.on_recv_rsp() 
    → 内存缓冲 (deque, 线程安全)
    → 后台定时刷新线程 (每 N 秒 / 每 M 条)
    → bulk_upsert → tick_data 表 (使用 sequence 字段去重)

特性：
  - 开机自启（systemd），常驻运行，无需停止
  - 配置文件 config.conf 的 [ticker]/[futu] 段管理订阅股票列表与富途地址
  - 支持多市场混合订阅：HK.*（港股）/ SH.* / SZ.*（A股）
  - 交易时段由 FutuOpenD 按各自市场管理，无需自行判断
  - 推送→缓冲→批量入库，不丢失数据
  - sequence 去重：断线重连推送的历史数据自动跳过
  - 重连机制：FutuOpenD 未就绪时自动等待重试

用法：
    python3 ticker_collector.py              # 前台运行
    systemctl start ticker-collector         # systemd 管理
"""

import os
import time
import signal
import logging
import threading
from collections import deque
from datetime import datetime

from futu import OpenQuoteContext, SubType, RET_OK, TickerHandlerBase, StockQuoteHandlerBase
from db import get_conn, bulk_upsert


# ── 路径 ──────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

log = logging.getLogger("ticker_collector")


def load_stocks_from_db():
    """从 stock_info 表读取活跃股票代码，返回 [code, ...]（is_active 过滤）。

    与 market_scheduler 共用同一张表，避免逐笔订阅列表与分钟级调度列表不一致。
    读取失败或为空时返回空列表（由调用方回退到 config.conf）。
    """
    from db import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT stock_code FROM stock_info "
                    "WHERE is_active = TRUE ORDER BY stock_code"
                )
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"从 stock_info 读取股票列表失败，回退到 config: {e}")
        return []


def load_config():
    """从统一 config.conf 加载配置（[ticker] 段 + [futu] 段）"""
    from config import val
    # 股票列表优先从 stock_info 表(is_active)读取；表为空或读取失败才回退 config.conf
    db_stocks = load_stocks_from_db()
    if db_stocks:
        stocks = db_stocks
        stocks_source = "stock_info"
    else:
        stocks_raw = val("ticker", "stocks", fallback="HK.00700")
        stocks = [s.strip() for s in stocks_raw.split(",") if s.strip()]
        stocks_source = "config.conf"
    # QUOTE(LV1 实时报价) 订阅标的：富途免费 LV1 额度通常有限（远小于 100），
    # 不能像 TICKER 那样一次性订阅全部股票。改为从配置显式指定少量有权限的代码。
    # 缺省为空 → 不订阅 QUOTE（逐笔主链路不受影响，仅缺失实时报价刷新）。
    quote_raw = val("ticker", "quote_stocks", fallback="")
    quote_stocks = [s.strip() for s in quote_raw.split(",") if s.strip()]
    return {
        "stocks": stocks,
        "stocks_source": stocks_source,
        "quote_stocks": quote_stocks,
        "buffer_size": int(val("ticker", "buffer_size", fallback="500")),
        "flush_interval_seconds": int(val("ticker", "flush_interval_seconds", fallback="10")),
        "futu_host": val("futu", "host", fallback="127.0.0.1"),
        "futu_port": int(val("futu", "port", fallback="11111")),
        "reconnect_delay_seconds": int(val("ticker", "reconnect_delay_seconds", fallback="30")),
    }


# ── 数据库写入 ────────────────────────────────────────────────

def _write_batch_to_db(batch: list) -> int:
    """将一批逐笔数据写入数据库，返回成功写入条数"""
    if not batch:
        return 0
    try:
        with get_conn() as conn:
            bulk_upsert(
                conn,
                "tick_data",
                batch,
                conflict_cols=["sequence"],
            )
        return len(batch)
    except Exception as e:
        log.error(f"数据库写入失败 ({len(batch)}条): {e}")
        return 0


# ── 逐笔回调处理器 ────────────────────────────────────────────

class TickerCollector(TickerHandlerBase):
    """
    富途逐笔成交回调处理器
    - on_recv_rsp: 接收推送数据，追加到内存缓冲
    - _flush: 从缓冲取出一批数据，写入数据库
    """

    def __init__(self, buffer_size: int = 500, flush_interval: float = 5.0):
        super().__init__()
        self.buffer = deque()
        self.buffer_lock = threading.Lock()
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.last_flush = time.time()
        self.total_received = 0
        self.total_written = 0

    def on_recv_rsp(self, rsp_str):
        ret_code, data = super().on_recv_rsp(rsp_str)

        if ret_code != RET_OK or data is None or len(data) == 0:
            return ret_code, data

        rows_appended = 0
        with self.buffer_lock:
            for _, row in data.iterrows():
                direction = row.get("ticker_direction", "")
                self.buffer.append({
                    "stock_code": str(row.get("code", "")),
                    "tick_time": _safe_timestamp(row.get("time")),
                    "price": _safe_numeric(row.get("price")),
                    "volume": _safe_int(row.get("volume")),
                    "turnover": _safe_numeric(row.get("turnover")),
                    "ticker_direction": direction if direction else None,
                    "sequence": _safe_int(row.get("sequence")),
                    "tick_type": str(row.get("type", "")) or None,
                })
                rows_appended += 1

        self.total_received += rows_appended
        return ret_code, data

    def should_flush(self) -> bool:
        """判断是否应该刷新缓冲区"""
        with self.buffer_lock:
            return (len(self.buffer) >= self.buffer_size or
                    (len(self.buffer) > 0 and
                     time.time() - self.last_flush >= self.flush_interval))

    def flush(self):
        """从缓冲区取出一批数据并写入数据库"""
        with self.buffer_lock:
            if not self.buffer:
                return
            batch = list(self.buffer)
            self.buffer.clear()
            self.last_flush = time.time()

        written = _write_batch_to_db(batch)
        self.total_written += written
        if len(batch) > 10:  # 只对较大批次打印日志
            log.info(f"批量入库: {len(batch)}条 → 成功 {written}条 "
                     f"(累计: 接收 {self.total_received}, 写入 {self.total_written})")

    def stats(self) -> dict:
        with self.buffer_lock:
            buf_len = len(self.buffer)
        return {
            "buf_len": buf_len,
            "total_received": self.total_received,
            "total_written": self.total_written,
        }


# ── 辅助函数 ──────────────────────────────────────────────────

def _safe_timestamp(val):
    """将时间值转为 ISO 格式字符串，解析失败返回 None"""
    if val is None:
        return None
    try:
        if isinstance(val, (datetime,)):
            return val.isoformat()
        s = str(val)
        if s and s != "NaT" and s != "nan":
            return s
    except Exception:
        pass
    return None


def _safe_numeric(val):
    """安全转换为 float，失败返回 None"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val):
    """安全转换为 int，失败返回 None"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ── 实时报价回调处理器（QUOTE 推送，只更新已存在行）────────────

class QuoteRefresher(StockQuoteHandlerBase):
    """
    富途实时报价(QUOTE)回调处理器
    - on_recv_rsp: 接收推送，仅保留每只股票最新一条报价到内存缓冲
    - 由后台线程 drain 后做 UPDATE（只刷新已存在的行，行不存在则跳过，不新增）

    与 cron 的分工：
      · record_trend 每一分钟 upsert trend_snapshot 整行（权威写入，含 buy_sell_ratio 等）
      · get_quote   每一分钟 upsert daily_quote 当日行（权威写入）
      · 本处理器仅用 QUOTE 推送刷新「行情类列」，绝不 INSERT
    """

    def __init__(self):
        super().__init__()
        self.buffer = {}          # stock_code -> 最新报价 dict
        self.lock = threading.Lock()

    def on_recv_rsp(self, rsp_str):
        ret_code, data = super().on_recv_rsp(rsp_str)
        if ret_code != RET_OK or data is None or len(data) == 0:
            return ret_code, data
        with self.lock:
            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                if not code:
                    continue
                self.buffer[code] = {
                    "stock_code": code,
                    "last_price": _safe_numeric(row.get("last_price")),
                    "high_price": _safe_numeric(row.get("high_price")),
                    "low_price": _safe_numeric(row.get("low_price")),
                    "volume": _safe_int(row.get("volume")),
                    "turnover": _safe_numeric(row.get("turnover")),
                    "update_time": _safe_timestamp(row.get("update_time")),
                    "prev_close": _safe_numeric(row.get("prev_close_price")),
                }
        return ret_code, data

    def drain(self):
        with self.lock:
            batch = list(self.buffer.values())
            self.buffer.clear()
        return batch


def _refresh_quote_batch(batch: list) -> int:
    """将一批实时报价刷新进 DB（只 UPDATE 已存在行，0 行即跳过）"""
    if not batch:
        return 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            for q in batch:
                code = q["stock_code"]
                last_price = q["last_price"]
                prev_close = q["prev_close"]
                change_pct = None
                if last_price is not None and prev_close not in (None, 0):
                    change_pct = round((last_price - prev_close) / prev_close * 100, 4)

                # trend_snapshot.turnover 约定为「亿元」，富途 QUOTE 原始为「元」，需 ÷1e8 对齐
                # （daily_quote.turnover 约定为「元」，其 UPDATE 仍用原始 q["turnover"]，不除）
                ts_turnover = q["turnover"] / 1e8 if q["turnover"] is not None else None

                # trend_snapshot：当前分钟行（行情列）。updated_at 用服务器时间作为刷新信号；
                # 当前分钟无行（cron 未建）→ 0 行命中 → 自动跳过，不碰表。
                # 注意：buy_sell_ratio 来自逐笔(tick)分析，QUOTE 不提供，仍由 cron 独占。
                cur.execute(
                    """UPDATE trend_snapshot
                       SET price = %(price)s,
                           volume = %(volume)s,
                           turnover = %(turnover)s,
                           updated_at = now()
                       WHERE stock_code = %(code)s
                         AND date_trunc('minute', snapshot_time) = date_trunc('minute', now())""",
                    {"code": code, "price": last_price,
                     "volume": q["volume"], "turnover": ts_turnover},
                )

                # daily_quote：当日行。update_time 用富途原始值（缺失时回退 now()）。
                cur.execute(
                    """UPDATE daily_quote
                       SET last_price = %(last_price)s,
                           high_price = %(high_price)s,
                           low_price  = %(low_price)s,
                           volume = %(volume)s,
                           turnover = %(turnover)s,
                           change_pct = %(change_pct)s,
                           update_time = %(update_time)s
                       WHERE stock_code = %(code)s
                         AND trade_date = CURRENT_DATE""",
                    {"code": code, "last_price": last_price,
                     "high_price": q["high_price"], "low_price": q["low_price"],
                     "volume": q["volume"], "turnover": q["turnover"],
                     "change_pct": change_pct,
                     "update_time": q["update_time"] or datetime.now()},
                )
        return len(batch)
    except Exception as e:
        log.error(f"实时报价刷新失败 ({len(batch)}条): {e}")
        return 0


def _quote_flush_loop(refresher: QuoteRefresher, stop_event: threading.Event,
                      interval: float = 3.0):
    """后台线程：定期把缓冲的实时报价刷新进 DB"""
    while not stop_event.is_set():
        stop_event.wait(timeout=interval)
        batch = refresher.drain()
        if batch:
            _refresh_quote_batch(batch)
    # 退出前最后一次刷新
    batch = refresher.drain()
    if batch:
        _refresh_quote_batch(batch)


# ── 刷新后台线程 ──────────────────────────────────────────────

def _flush_loop(collector: TickerCollector, stop_event: threading.Event):
    """后台线程：定期检查并刷新缓冲区"""
    while not stop_event.is_set():
        if collector.should_flush():
            collector.flush()
        stop_event.wait(timeout=0.5)  # 每 500ms 检查一次

    # 退出前最后一次刷新
    collector.flush()
    log.info(f"退出刷新, 最终统计: {collector.stats()}")


# ── 统计日志线程 ──────────────────────────────────────────────

def _stats_loop(collector: TickerCollector, stop_event: threading.Event):
    """定期打印统计信息（每小时一次）"""
    while not stop_event.is_set():
        stop_event.wait(timeout=3600)
        if not stop_event.is_set():
            stats = collector.stats()
            log.info(f"运行统计 — 缓冲区: {stats['buf_len']}条, "
                     f"累计接收: {stats['total_received']}, "
                     f"累计写入: {stats['total_written']}")


# ── 主循环 ────────────────────────────────────────────────────

def connect_and_subscribe(cfg: dict) -> OpenQuoteContext | None:
    """连接 FutuOpenD 并订阅逐笔数据，返回上下文"""
    stocks = cfg.get("stocks", ["HK.00700"])
    quote_stocks = cfg.get("quote_stocks", [])
    futu_host = cfg.get("futu_host", "127.0.0.1")
    futu_port = cfg.get("futu_port", 11111)

    log.info(f"连接 FutuOpenD ({futu_host}:{futu_port}) ...")
    try:
        quote_ctx = OpenQuoteContext(host=futu_host, port=futu_port)
    except Exception as e:
        log.error(f"连接 FutuOpenD 失败: {e}")
        return None

    # 1) TICKER(逐笔成交, LV2) 订阅全部股票 —— 主链路，失败则整体失败
    ret, msg = quote_ctx.subscribe(stocks, [SubType.TICKER], subscribe_push=True)
    if ret != RET_OK:
        log.error(f"TICKER 订阅失败: {msg}")
        quote_ctx.close()
        return None
    log.info(f"TICKER 订阅成功: 共 {len(stocks)} 只")

    # 2) QUOTE(实时报价, LV1) 订阅配置指定的子集 —— 富途 LV1 额度有限，
    #    不能一次性订阅全部。失败仅告警、不阻断主链路（缺失实时报价刷新而已）。
    if quote_stocks:
        ret_q, msg_q = quote_ctx.subscribe(quote_stocks, [SubType.QUOTE], subscribe_push=True)
        if ret_q != RET_OK:
            log.warning(f"QUOTE 订阅失败（不影响逐笔主链路）: {msg_q}")
        else:
            log.info(f"QUOTE 订阅成功: 共 {len(quote_stocks)} 只")
    else:
        log.info("未配置 quote_stocks，跳过 QUOTE 订阅（实时报价刷新将不可用）")

    return quote_ctx


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(
                os.path.join(_SCRIPT_DIR, "ticker_collector.log")
            ),
        ],
    )

    log.info("=" * 50)
    log.info("逐笔成交采集服务启动")
    log.info("=" * 50)

    cfg = load_config()
    log.info(f"配置: stocks={cfg.get('stocks')} (来源: {cfg.get('stocks_source')}), "
             f"buffer_size={cfg.get('buffer_size')}, "
             f"flush_interval={cfg.get('flush_interval_seconds')}s")

    collector = TickerCollector(
        buffer_size=cfg.get("buffer_size", 500),
        flush_interval=cfg.get("flush_interval_seconds", 5.0),
    )
    refresher = QuoteRefresher()

    stop_event = threading.Event()

    # 注册信号处理
    def _shutdown(signum, frame):
        log.info(f"收到信号 {signum}，准备退出 ...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # 启动后台刷新线程
    flush_thread = threading.Thread(
        target=_flush_loop, args=(collector, stop_event), daemon=True
    )
    flush_thread.start()

    # 启动统计线程
    stats_thread = threading.Thread(
        target=_stats_loop, args=(collector, stop_event), daemon=True
    )
    stats_thread.start()

    # 启动实时报价刷新线程（QUOTE 推送 → 只 UPDATE 已存在行）
    quote_flush_thread = threading.Thread(
        target=_quote_flush_loop, args=(refresher, stop_event), daemon=True
    )
    quote_flush_thread.start()

    reconnect_delay = cfg.get("reconnect_delay_seconds", 30)

    while not stop_event.is_set():
        quote_ctx = connect_and_subscribe(cfg)

        if quote_ctx is None:
            log.warning(f"将在 {reconnect_delay}s 后重试连接 ...")
            stop_event.wait(timeout=reconnect_delay)
            continue

        # 设置回调处理器
        quote_ctx.set_handler(collector)
        quote_ctx.set_handler(refresher)

        log.info("逐笔 + 实时报价采集已就绪，等待推送数据 ...")

        # 保持连接，等待退出信号
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)

        # 退出
        quote_ctx.close()
        log.info("连接已关闭")

    log.info("逐笔成交采集服务已停止")


if __name__ == "__main__":
    main()
