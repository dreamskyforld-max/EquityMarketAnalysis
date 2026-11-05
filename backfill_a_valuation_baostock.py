#!/usr/bin/env python3
"""A股 2018 年前 pe_ttm_ratio 回补（baostock 源，与东财同口径）

为什么需要这个脚本：
    东财 RPT_VALUEANALYSIS_DET（backfill_a_valuation.py 的源）**硬起点就是
    2018-01-02** —— 按个股过滤该报表实测 count=2143，与全市场查询完全一致，
    换成 F10 报表（RPT_F10_VALUATION 等）则一律「9501 报表配置不存在」。
    所以 2018 前的估值列在 a_daily_quote 里是空的，无源可补。

    唯一实测可行的源是 baostock（`query_history_k_data_plus` 的 peTTM）：
      · 深度：到个股上市首日（sh.600601 起 1990-12-19，sz.000001 起 1991-04-03）
      · 口径：与东财 PE_TTM **完全一致** —— 5 只票（600519/000001/002415/601398/
        688981）973 个重叠交易日，stored_pe / (bao_pe × f) 的中位、p05、p95
        全部 = 1.0000（f = 库内 QFQ close / baostock 不复权 close）。
        即两边都是「总市值 / 归母净利润TTM」，只差复权基准。

落库口径（关键）：
    a_daily_quote.pe_ttm_ratio 在 2018 后是 **QFQ 基准**值（backfill_a_valuation.py
    的 _apply_qfq_factor 把东财未复权 PE 乘过 f）。为保持同一列语义连续，
    本脚本同样换算：  pe_qfq = pe_baostock × (a_daily_quote.close / baostock.close)
    不换算直接写原值会与 2018 后差 20%~50%（实测 600519 f=0.79~0.86、
    格力 f=0.54~0.62），列内口径断裂。

    ⚠ 因此本列是「QFQ 缩放后的 PE」，不是「当时的真实 PE」。要真实 PE 需另立列，
      那是会动全表的口径变更，不在本脚本范围。

写入范围（保守）：
    · 只 UPDATE，不 INSERT —— 不产生 OHLC 全空的脏行（2018 前行情表已完整，
      1990-12-19 起 3233 只 / 747 万行）。
    · 只填 pe_ttm_ratio IS NULL 且 close IS NOT NULL 的行；已有值（东财 2018+）绝不覆盖。
    · 不碰 open/high/low/close/volume/amount/update_time —— update_time 是前端
      「按更新时间取最新快照」的排序键，回填时改成 NOW() 会让 2017 的行变成"最新"。
    · pb_ratio **不补**：实测 baostock pbMRQ 与东财 PB_MRQ 在财报披露窗口
      （1-4 月 / 7 月 / 10 月）有 5%~9% 系统性偏差（按报告期切 vs 按公告日切），
      混用会污染列。PE_TTM 无此问题（973 日实测 1.0000）。

断点续采：
    进度由库反推 —— 每轮重新统计「仍有 pe_ttm_ratio IS NULL 的股票」，
    已补齐的股票自动跳过，无需额外进度表。中途被 kill 直接重跑即可。

限速与重试（实测必须）：
    baostock 是裸 socket 协议（public-api.baostock.com:10030），连续查询十几次后
    会出现 `接收数据异常` / `Broken pipe` / rc=10002007，甚至 TCP connect 超时，
    静置数分钟后自愈。故三层防护：
      ① socket 全局超时 60s（否则 rs.next() 可永久挂住）；
      ② 单只失败退避 30/120/300s + 强制重登重试；
      ③ 连续 3 只失败 → 冷却 600s 后重登继续（最多 3 次），之后才中止。
    因此可以 nohup 无人值守长跑，被限流会自己等；中止后重跑同命令即续采。

用法：
    python3 backfill_a_valuation_baostock.py --probe              # 只测速/看是否被限流
    python3 backfill_a_valuation_baostock.py --dry-run            # 只取不写，看覆盖
    python3 backfill_a_valuation_baostock.py --limit 5            # 先跑 5 只验证
    python3 backfill_a_valuation_baostock.py                      # 全量（约 3200 只）
    python3 backfill_a_valuation_baostock.py --start 2006-01-01   # 只补 2006 起
    python3 backfill_a_valuation_baostock.py --codes SH.600519,SZ.000001
    nohup python3 backfill_a_valuation_baostock.py > backfill_pe.log 2>&1 &   # 长跑

    退出/被限流后直接重跑同一条命令即可（进度由库反推，已补齐的股票自动跳过）。
"""
import os
import sys
import time
import socket
import logging
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_a_valuation_baostock")

# 东财估值报表硬起点：>= 此日的行由 backfill_a_valuation.py 负责，本脚本不碰
CUTOFF = date(2018, 1, 2)
DEFAULT_START = date(1990, 1, 1)      # 行情表起点 1990-12-19，留余量
DEFAULT_END = date(2017, 12, 31)

SOCKET_TIMEOUT = 60                   # baostock 裸 socket，不设超时会永久挂住
MAX_ABS_PE = 99_999_999.0             # pe_ttm_ratio NUMERIC(12,4) 的整数位上限
UPDATE_PAGE = 5000                    # execute_values 单批行数（≤5 万行/事务红线）

# 单只股票查询失败后的退避梯度（秒）。baostock 会在连续请求后「协议层静默挂起」
# （实测：TCP 通、login 通、query 卡住无响应，静置数分钟后自愈），必须狠等一下。
BACKOFF = (30, 120, 300, 600)

PRE2018_ROWS = 7_476_549              # 2018 前待补行总量（仅用于 --probe 估时）


# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
def parse_args():
    a = {
        "dry": "--dry-run" in sys.argv,
        "probe": "--probe" in sys.argv,
        "start": DEFAULT_START,
        "end": DEFAULT_END,
        "limit": None,
        "codes": None,
        "sleep": 0.5,          # 默认轻量限速，保护源站（实测连续猛查会被挂起）
        "max_retry": 3,
        "break_after": 3,      # 连续失败 N 只 → 触发冷却
        "cooldown": 600,       # 冷却时长（秒），服务端限流一般几分钟自愈
        "max_cooldown": 3,     # 最多冷却次数，用完则中止
    }
    if "--break-after" in sys.argv:
        a["break_after"] = int(sys.argv[sys.argv.index("--break-after") + 1])
    if "--cooldown" in sys.argv:
        a["cooldown"] = int(sys.argv[sys.argv.index("--cooldown") + 1])
    if "--max-cooldown" in sys.argv:
        a["max_cooldown"] = int(sys.argv[sys.argv.index("--max-cooldown") + 1])
    if "--start" in sys.argv:
        a["start"] = date.fromisoformat(sys.argv[sys.argv.index("--start") + 1])
    if "--end" in sys.argv:
        a["end"] = date.fromisoformat(sys.argv[sys.argv.index("--end") + 1])
    if "--limit" in sys.argv:
        a["limit"] = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--codes" in sys.argv:
        raw = sys.argv[sys.argv.index("--codes") + 1]
        a["codes"] = [c.strip().upper() for c in raw.split(",") if c.strip()]
    if "--sleep" in sys.argv:
        a["sleep"] = float(sys.argv[sys.argv.index("--sleep") + 1])
    if "--max-retry" in sys.argv:
        a["max_retry"] = int(sys.argv[sys.argv.index("--max-retry") + 1])
    # 越界保护：end 不允许跨进东财负责的 2018+ 区间
    if a["end"] >= CUTOFF:
        a["end"] = CUTOFF - timedelta(days=1)
    return a


# ----------------------------------------------------------------------------
# 代码映射
# ----------------------------------------------------------------------------
def _bs_symbol(code):
    """SH.600519 -> sh.600519 / SZ.000001 -> sz.000001；其余（北交所等）返回 None。"""
    if code.startswith("SH."):
        return "sh." + code[3:]
    if code.startswith("SZ."):
        return "sz." + code[3:]
    return None


# ----------------------------------------------------------------------------
# 待补目标（断点续采依据）
# ----------------------------------------------------------------------------
def _targets(start, end, codes=None):
    """返回 [(stock_code, 待补行数), ...]，按待补行数降序。"""
    from db import get_conn
    sql = ("SELECT stock_code, COUNT(*) AS n FROM a_daily_quote "
           "WHERE trade_date >= %s AND trade_date <= %s "
           "  AND close IS NOT NULL AND pe_ttm_ratio IS NULL")
    params = [start, end]
    if codes:
        sql += " AND stock_code = ANY(%s)"
        params.append(codes)
    sql += " GROUP BY stock_code ORDER BY n DESC, stock_code"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [(r[0], int(r[1])) for r in cur.fetchall()]


def _load_pending_closes(code, start, end):
    """该股票待补行的 {trade_date: Decimal close}（只取需要填的日期，省内存）。"""
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, close FROM a_daily_quote "
                "WHERE stock_code = %s AND trade_date >= %s AND trade_date <= %s "
                "  AND close IS NOT NULL AND pe_ttm_ratio IS NULL",
                (code, start, end))
            return {r[0]: r[1] for r in cur.fetchall()}


# ----------------------------------------------------------------------------
# baostock 会话（自带重连）
# ----------------------------------------------------------------------------
class Bao:
    """baostock 连接封装：懒登录 + 失败重登 + 超时保护。

    baostock 用模块级 socket（baostock.common.context），并发不安全 → 单线程串行。
    """

    def __init__(self, max_retry=3):
        self.max_retry = max_retry
        self.bs = None
        self._logged_in = False

    def login(self):
        import baostock as bs
        self.bs = bs
        socket.setdefaulttimeout(SOCKET_TIMEOUT)   # 必须：否则 rs.next() 可永久阻塞
        for i in range(self.max_retry):
            r = bs.login()
            if r.error_code == "0":
                self._logged_in = True
                return
            log.warning("  baostock 登录失败(%s/%s): %s %s", i + 1, self.max_retry,
                        r.error_code, r.error_msg)
            time.sleep(5 * (i + 1))
        raise RuntimeError("baostock 登录失败，服务端可能限流，请稍后重试")

    def relogin(self):
        try:
            if self._logged_in:
                self.bs.logout()
        except Exception:
            pass
        self._logged_in = False
        time.sleep(10)
        self.login()

    def query_pe(self, bsym, start, end):
        """取个股 [start, end] 的 {date: (pe_raw, close_raw)}；peTTM 为 0/空 视为无效。

        失败按 BACKOFF 递增退避 + 重登后重试；耗尽抛异常（由调用方记 failed 并触发熔断）。
        """
        last = None
        for attempt in range(self.max_retry):
            try:
                rs = self.bs.query_history_k_data_plus(
                    bsym, "date,close,peTTM",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency="d", adjustflag="3")   # 3 = 不复权，与库内 close 换算用
                if rs.error_code != "0":
                    raise RuntimeError(f"rc={rs.error_code} {rs.error_msg}")
                out = {}
                bad = 0
                while rs.next():
                    d, c, pe = rs.get_row_data()
                    try:
                        pe_f = float(pe) if pe else None
                        c_f = float(c) if c else None
                    except ValueError:
                        bad += 1
                        continue
                    # peTTM=0 是「无数据」占位（实测 600601 1991-01 全是 0），不是真实值
                    if not pe_f or pe_f == 0 or not c_f or c_f <= 0:
                        bad += 1
                        continue
                    out[date.fromisoformat(d)] = (pe_f, c_f)
                return out, bad
            except Exception as e:      # 网络/协议异常统一重试
                last = e
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                log.warning("  %s 查询失败(%s/%s): %s: %s —— 等待 %ss 后重试", bsym,
                            attempt + 1, self.max_retry, type(e).__name__,
                            str(e)[:100], wait)
                if attempt + 1 < self.max_retry:
                    time.sleep(wait)
                    self.relogin()
        raise RuntimeError(f"{bsym} 重试耗尽: {last}")

    def close(self):
        try:
            if self._logged_in:
                self.bs.logout()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 落库
# ----------------------------------------------------------------------------
def _flush(rows):
    """单股票一次 UPDATE（VALUES 连接），只写 pe_ttm_ratio NULL 的行。

    rows: [(stock_code, trade_date, pe_qfq), ...]
    返回实际更新行数（WHERE 会再挡一次已有值/缺 close 的行）。
    """
    if not rows:
        return 0
    from db import get_conn
    from psycopg2.extras import execute_values
    sql = ("UPDATE a_daily_quote a SET pe_ttm_ratio = v.pe "
           "FROM (VALUES %s) AS v(stock_code, trade_date, pe) "
           "WHERE a.stock_code = v.stock_code "
           "  AND a.trade_date = v.trade_date::date "
           "  AND a.pe_ttm_ratio IS NULL AND a.close IS NOT NULL")
    n = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), UPDATE_PAGE):
                chunk = rows[i:i + UPDATE_PAGE]
                execute_values(cur, sql, chunk, page_size=UPDATE_PAGE)
                n += cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
    return n


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def _process_one(bao, code, start, end, dry):
    """回补单只股票。返回 (写入行数, 源无数据日期数, 缺复权因子数)。"""
    bsym = _bs_symbol(code)
    if not bsym:
        log.warning("  %s 非沪深代码，跳过", code)
        return 0, 0, 0
    closes = _load_pending_closes(code, start, end)
    if not closes:
        return 0, 0, 0
    src, bad = bao.query_pe(bsym, start, end)
    if bad:
        log.info("  %s 源端无效/空缺 %s 个交易日（peTTM=0 或空）", code, bad)

    rows = []
    no_factor = 0
    for d, c_qfq in closes.items():
        hit = src.get(d)
        if not hit:
            no_factor += 1          # 源无当日（停牌/未上市/未覆盖）
            continue
        pe_raw, c_raw = hit
        f = float(c_qfq) / c_raw     # 复权因子：QFQ 基准 / 源未复权
        pe = round(pe_raw * f, 4)
        if abs(pe) >= MAX_ABS_PE:    # NUMERIC(12,4) 溢出保护
            no_factor += 1
            continue
        rows.append((code, d, pe))

    if dry:
        log.info("  %s 待写 %s 行（源 %s 行 / 库待补 %s 行 / 缺源 %s）",
                 code, len(rows), len(src), len(closes), no_factor)
        return len(rows), no_factor, 0
    n = _flush(rows)
    return n, no_factor, len(rows) - n


def _probe(a):
    """连通性/限流探测：只查一只票并报速。长跑前先跑它，确认服务端没在挂起。"""
    code = (a["codes"] or ["SH.600519"])[0]
    bsym = _bs_symbol(code)
    if not bsym:
        log.error("探测代码 %s 非沪深，无法探测", code)
        return
    bao = Bao(max_retry=1)
    t0 = time.time()
    bao.login()
    t_login = time.time() - t0
    t0 = time.time()
    try:
        src, bad = bao.query_pe(bsym, a["start"], a["end"])
    except Exception as e:
        log.error("探测失败（服务端可能在限流/挂起，可稍后重试）: %s: %s",
                  type(e).__name__, str(e)[:160])
        bao.close()
        return
    dt = time.time() - t0
    bao.close()
    rps = len(src) / dt if dt > 0 else 0
    est = PRE2018_ROWS / rps / 3600 if rps > 0 else float("inf")
    log.info("探测 %s：登录 %.1fs / 查询 %.1fs / 源 %s 行（无效 %s）"
             " → %.0f 行/秒，全量 %s 行粗估 %.1f 小时",
             bsym, t_login, dt, len(src), bad, rps, PRE2018_ROWS, est)


def run():
    a = parse_args()
    start, end, dry = a["start"], a["end"], a["dry"]
    if a["probe"]:
        _probe(a)
        return
    log.info("回补区间 %s ~ %s（东财负责区从 %s 起，不越界）dry=%s",
             start, end, CUTOFF, dry)

    targets = _targets(start, end, a["codes"])
    if not targets:
        log.info("无待补数据（该区间 pe_ttm_ratio 已全部有值）")
        return
    total_rows = sum(n for _, n in targets)
    log.info("待补 %s 只股票 / %s 行（断点续采：已补齐的股票不在列表内）",
             len(targets), total_rows)
    if a["limit"] and len(targets) > a["limit"]:
        rest = len(targets) - a["limit"]
        targets = targets[:a["limit"]]
        log.info("本轮限量 %s 只（剩余 %s 只下次重跑继续）", a["limit"], rest)

    bao = Bao(max_retry=a["max_retry"])
    bao.login()
    t0 = time.time()
    done_rows = done_stocks = fail_stocks = no_data_stocks = mismatch = 0
    consec_fail = cooldowns = 0
    try:
        for i, (code, n_missing) in enumerate(targets, 1):
            try:
                n, no_src, race = _process_one(bao, code, start, end, dry)
                consec_fail = 0
            except Exception as e:
                fail_stocks += 1
                consec_fail += 1
                log.error("  [%s/%s] %s 失败（连续第 %s 次）: %s: %s",
                          i, len(targets), code, consec_fail,
                          type(e).__name__, str(e)[:140])
                # 熔断：连续失败说明是服务端整体限流/挂起 → 冷却后重登继续，
                # 冷却次数用完才中止（这样无人值守长跑也能自愈）
                if consec_fail >= a["break_after"]:
                    cooldowns += 1
                    if cooldowns > a["max_cooldown"]:
                        log.error("连续失败且已冷却 %s 次，判定服务端长时间不可用 → 中止。"
                                  "已写入数据保留，稍后重跑同命令即可断点续采。",
                                  cooldowns - 1)
                        break
                    log.warning("连续 %s 只失败，判定 baostock 限流/挂起 → 冷却 %ss 后继续"
                                "（第 %s/%s 次）", consec_fail, a["cooldown"],
                                cooldowns, a["max_cooldown"])
                    time.sleep(a["cooldown"])
                    bao.relogin()
                    consec_fail = 0
                continue
            done_rows += n
            mismatch += race
            if n == 0 and no_src >= n_missing:
                no_data_stocks += 1
            elif n > 0:
                done_stocks += 1
            elapsed = time.time() - t0
            if i % 20 == 0 or i == len(targets):
                eta = elapsed / i * (len(targets) - i)
                log.info("[%s/%s] 已写 %s 行  耗时 %.0fs  预计剩余 %.0fs",
                         i, len(targets), done_rows, elapsed, eta)
            if a["sleep"]:
                time.sleep(a["sleep"])
    finally:
        bao.close()

    log.info("完成：处理 %s 只（有效 %s / 源无数据 %s / 失败 %s），写入 %s 行%s",
             len(targets), done_stocks, no_data_stocks, fail_stocks, done_rows,
             "（dry-run 未落库）" if dry else "")
    if mismatch:
        log.warning("有 %s 行因并发改动未能更新（重跑会自动补齐）", mismatch)
    if fail_stocks:
        log.warning("有 %s 只失败，直接重跑本脚本即可断点续采", fail_stocks)


if __name__ == "__main__":
    run()
    os._exit(0)   # baostock 的 socket 线程会拖住退出
