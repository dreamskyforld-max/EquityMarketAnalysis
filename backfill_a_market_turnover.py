#!/usr/bin/env python3
"""
A股全市场总成交额 · 历史回溯（akshare stock_zh_a_daily / 新浪源，一次性全量）

数据源：AKShare stock_zh_a_daily()（新浪财经 A股个股日线）。
       返回该代码上市以来的完整历史日线，含 amount/volume/turnover 等字段；
       无富途历史 K 线额度限制，实测稳定，全 A 约 1-2 小时跑完。
       ETF/基金（SH 5xxxxx / SZ 15·16·18xxxx，如 SH.520900）新浪个股接口不覆盖，
       单独走腾讯 newfqkline 前复权分支（见 _fetch_fund_kline）。

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
    python3 backfill_a_market_turnover.py --fill-prev-close          # 仅库内按 LAG 回填 prev_close/change_pct（不采集）

耗时：全 A 5000+ 只 × 逐只拉历史（akshare 新浪），约 1-2 小时；分批则按轮次摊开。

prev_close / change_pct：
  采集时按每只股票的历史日线顺序递推（前交易日 close 即 prev_close），
  change_pct = (close-prev_close)/prev_close*100，与 daily_quote 对齐；首个交易日为 NULL。
  全量跑完后会库内再按 LAG 补齐「之前已落库但缺这两列」的历史行；亦可单独用
  --fill-prev-close 仅做库内补齐（不采集）。change_pct 精度 NUMERIC(12,4)（以真实库为准）：
  个别仙股/重组日涨跌幅异常（脏数据，源数据 discontinuity）原样落库，下游按需识别。
"""
import sys
import time
import socket
import json
import os
import logging
from datetime import datetime, date, timedelta

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
    fill_prev_close = "--fill-prev-close" in sys.argv
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
        "force": force, "fill_prev_close": fill_prev_close,
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
# ETF/基金分支：腾讯 K 线（新浪个股接口不覆盖基金代码）
# ----------------------------------------------------------------------------
# 为什么需要单独分支（2026-09-24 排查 SH.520900 无数据）：
#   新浪个股接口 stock_zh_a_daily 底层取 realstock/company/{sym}/hisdata/klc_kl.js，
#   该文件只对股票存在；基金代码（SH 5 开头 / SZ 15·16·18 开头）返回的不是 JSON，
#   akshare 抛 JSONDecodeError → 重试耗尽后返回空 → 整票被当成「无数据」跳过。
#   腾讯 newfqkline 覆盖基金，且直接给前复权价（锚定最新交易日，与 a_daily_quote 口径一致）。
#   注意不要用它的 fqkline/get 变体：那个接口当日行四个价格都填成前收盘（实测假价）。
#   字段：[date, open, close, high, low, volume(手), {}, 换手率(%), 成交额(万元), '']
TX_KLINE_URL = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
                "?param={sym},day,{start},{end},{count},qfq")
TX_KLINE_COUNT = 320       # 单次请求条数（接口硬上限 640，取 320 留余量交错分页）
TX_KLINE_MAX_PAGES = 40    # 分页安全上限（40 × 320 ≈ 52 年日线，足够任何基金历史）


def _is_fund_code(full: str) -> bool:
    """基金/ETF 代码形态判定：上交所 SH 5xxxxx，深交所 SZ 15/16/18xxxx。

    这些代码段是场内基金（ETF/LOF/封基），不在新浪个股接口覆盖范围内。
    沪市股票段为 600/601/603/605/688/689，深市为 000/001/002/003/300/301，不重叠。
    """
    market, _, num = full.partition(".")
    if market == "SH":
        return num.startswith("5")
    if market == "SZ":
        return num.startswith(("15", "16", "18"))
    return False


def _fetch_fund_kline(full):
    """腾讯 newfqkline 拉 ETF/基金完整历史前复权日线（返回与 _fetch_kline 同构的 DataFrame）。

    · 价格 qfq 前复权，锚定最新交易日 —— 与 stock_zh_a_daily(adjust="qfq") 口径一致
    · volume 接口为「手」→ ×100 换股；amount 为「万元」→ ×1e4 换元（实测与库内既有行一致，
      仅因接口按手/万元取整有 ≤100 股、≤100 元的尾差）
    · turnover 接口为百分数 → ÷100 换小数（_map_quote_row 内部再 ×100 落库）
    · 与股票分支一致：返回【完整上市以来历史】，窗口截断交给 fetch_history 处理，
      以便窗口外的前一交易日仍能推出首条落库行的 prev_close
    分页：接口单次最多回 640 根，按 end 逐段向前翻页、按日期去重合并。
    """
    import requests
    market, num = full.split(".", 1)
    sym = f"sh{num}" if market == "SH" else f"sz{num}"
    headers = {"User-Agent": "Mozilla/5.0"}
    end_s = date.today().isoformat()   # 腾讯 end 为闭区间（含当日）
    rows: dict[str, list] = {}         # 日期 → 原始行（分页去重）
    for _ in range(TX_KLINE_MAX_PAGES):
        url = TX_KLINE_URL.format(sym=sym, start="", end=end_s, count=TX_KLINE_COUNT)
        try:
            payload = requests.get(url, headers=headers, timeout=30).json()
        except Exception as e:
            log.warning(f"  {full} 腾讯 K 线请求失败（end={end_s}）: {e}")
            break
        part = ((payload.get("data") or {}).get(sym) or {}).get("qfqday") or []
        if not part:
            break
        before = len(rows)
        for r in part:
            if len(r) >= 9:            # 跳过字段不全的行
                rows[r[0]] = r
        oldest = min(r[0] for r in part)
        if len(rows) == before:        # 本批没带来新日期 → 已到上市首日
            break
        nxt = date.fromisoformat(oldest) - timedelta(days=1)
        if nxt.isoformat() >= end_s:   # 防御：日期未向前推进
            break
        end_s = nxt.isoformat()

    if not rows:
        return pd.DataFrame()
    recs = []
    for k in sorted(rows):
        r = rows[k]
        vol = _as_float(r[5])   # 手
        tv = _as_float(r[7])    # 换手率（百分数）
        amt = _as_float(r[8])   # 成交额（万元）
        recs.append({
            "date":       date.fromisoformat(r[0]),
            "trade_date": date.fromisoformat(r[0]),
            "open":       _as_float(r[1]),
            "close":      _as_float(r[2]),
            "high":       _as_float(r[3]),
            "low":        _as_float(r[4]),
            "volume":     int(vol * 100) if vol is not None else 0,
            # 上市首日接口给 0.00（份额分母未定，是占位不是真值）→ 置 None 不落假 0%
            "turnover":   (tv / 100) if (tv is not None and tv > 0) else None,
            "amount":     (amt * 1e4) if amt is not None else None,
        })
    log.info(f"  {full} 腾讯 ETF/基金日线 {len(recs)} 条（{recs[0]['trade_date']} ~ {recs[-1]['trade_date']}）")
    return pd.DataFrame(recs)


# ----------------------------------------------------------------------------
# 单只历史 K 线
# ----------------------------------------------------------------------------
def _fetch_kline(full, start=None, end=None):
    """拉单只完整历史日线，返回 DataFrame（trade_date 已转 date）。

    股票 → akshare stock_zh_a_daily（新浪）；ETF/基金 → 腾讯 newfqkline 前复权（见 _fetch_fund_kline）。

    start / end 为日期下界 / 上界（均为闭区间）。end 为 None 表示无截止日。
    数据源返回该代码完整上市以来日线；仅显式传入 start/end 时才按窗口截断，
    不传则保留完整历史（全量采集）。

    full 为带交易所前缀的完整代码（如 SH.600900 / SH.900901 / SZ.200011）。
    新浪源无富途历史 K 线额度限制，可一次拿完整历史，且实测比东财源稳定。
    列名（英文）：date/open/high/low/close/volume/amount/outstanding_share/turnover。
      - amount   = 成交额（元）
      - volume   = 成交量（股）
      - turnover = 换手率（小数，如 0.003 → 0.3%）
    新浪代码前缀按【交易所】映射（SH→sh, SZ→sz），不能用数字开头判断——
    否则 B 股（900/200 开头）、沪伦通 CDR（689/700 等）会被错分到 sz 而查不到。
    注意：数据源对未上市/退市的代码可能返回空，调用方按「无数据」跳过。
    """
    # ETF/基金：新浪个股接口取不到（实测 SH.520900 抛 JSONDecodeError 后空数据），走腾讯分支
    if _is_fund_code(full):
        return _fetch_fund_kline(full)

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
                # 5 次尝试之间都留 2s 间隔（原写法 attempt<2 会让后两次连打）
                if attempt < 4:
                    time.sleep(2)
        else:
            return pd.DataFrame()
    finally:
        socket.setdefaulttimeout(_prev_to)
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
    # 注意：此处返回【完整上市以来历史】，不再按 start/end 截断。
    # 窗口截断改到 fetch_history 落库阶段进行，以便 prev_close（前收盘价）能在
    # 窗口边界外仍正确递推（首条落库行的 prev_close = 窗口前最近一交易日的 close）。
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
            # 按交易日升序，沿全历史递推 prev_close（不受窗口截断影响）
            df = df.sort_values("trade_date").reset_index(drop=True)
            prev_close = None
            for r in df.itertuples(index=False):
                d = r._asdict()
                td = d["trade_date"]
                close = _as_float(d.get("close"))
                # 涨跌幅 = (close - prev_close) / prev_close * 100（prev_close 为前交易日 close）
                if close is not None and prev_close is not None and prev_close != 0:
                    change_pct = round((close - prev_close) / prev_close * 100, 2)
                else:
                    change_pct = None
                # 窗口过滤：仅决定「是否落库」，窗口外的行仍用于递推 prev_close
                in_window = (start is None or td >= start) and (end is None or td <= end)
                if in_window:
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
                    rows.append(_map_quote_row(full, td, d, prev_close, change_pct))
                # 递推 prev_close（用本行 close；close 为 None 时沿用上一交易日）
                if close is not None:
                    prev_close = close
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


def _map_quote_row(code, td, row, prev_close=None, change_pct=None):
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    _tr = _f(row.get("turnover"))
    # 单位统一（2026-09-07 双源实测）：
    #   新浪 turnover 为【小数】—— akshare 1.18.60，sh600900 全天 0.00279 = 0.279%；
    #   富途快照 turnover_rate 为【百分数】—— SH.600900 快照 0.391，
    #   volume/总股本 = 0.00391，比值恰为 100（get_a_market_turnover.py 直采口径）。
    # a_daily_quote.turnover_rate 全表统一为百分数（与富途一致）→ 此处 ×100。
    # 守卫：A股 T+1 日换手不可能 ≥200%，_tr ≥ 2 或负数/NaN 一律置 None
    # （skip_null_updates 保留库内原值，不落脏数据），并保证 ×100 后不超
    # numeric(8,4) 上限 999.9999。注：守卫拦不住 0~2 区间的小数/百分数歧义，
    # 单位漂移靠 requirements.txt 锁定 akshare 版本兜底。
    _turnover_rate = round(_tr * 100, 4) if (_tr is not None and 0 <= _tr < 2) else None

    return {
        "stock_code": code,
        "trade_date": td,
        "open": _f(row.get("open")),
        "high": _f(row.get("high")),
        "low": _f(row.get("low")),
        "close": _f(row.get("close")),
        "volume": int(row["volume"]) if row.get("volume") is not None else 0,
        "amount": _f(row.get("amount")),
        # prev_close / change_pct 由采集时按 LAG(close) 同表推算（见 fetch_history 的递推逻辑），
        # 与 daily_quote.change_pct 对齐；首个交易日为 None（无前收盘价）。
        "prev_close": prev_close,
        "change_pct": change_pct,
        # 新浪小数 ×100 → 百分数（换算与守卫见上方 _turnover_rate），
        # 与日常富途快照（get_a_market_turnover.py）写入的 turnover_rate 同口径。
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
# prev_close / change_pct 列确保 + 库内 LAG 回填
# ----------------------------------------------------------------------------
# change_pct 精度 NUMERIC(12,4)（以真实库为准，2026-09-14 修正）：
#   A股/港股 仙股「合股/拆股」异常日可达上万%（脏数据）；实测 max|change_pct| 已达 4737.61，
#   逼近 8,4 上限 9999.9999；12,4 有 8 位整数余量，最安全。
#   ⚠ 历史上本脚本/schema.sql 写的是 8,2，但真实库两张表实际都是 12,4 —— 按 AGENTS.md
#     「真实库才是结构现状」，统一为 12,4，避免无谓且被视图拦截的 ALTER。
_PREV_CLOSE_TYPE = "NUMERIC(12,4)"
_CHANGE_PCT_TYPE = "NUMERIC(12,4)"
_CHANGE_PCT_TARGET = (12, 4)


def _change_pct_type_now(tbl: str):
    """读信息模式返回 change_pct 当前 (precision, scale)；列不存在返回 None。"""
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s AND column_name='change_pct'",
                (tbl,),
            )
            r = cur.fetchone()
            return (r[0], r[1]) if r else None


def _alter_change_pct_type(tbl: str):
    """在【独立 autocommit 连接】上把 change_pct 转为目标精度（失败仅告警，不污染调用方事务）。

    为什么不复用调用方游标：ALTER COLUMN TYPE 会被依赖该列的视图/规则拦截
    （实测 v_daily_quote → "cannot alter type of a column used by a view or rule"）。
    若在调用方事务内执行且用裸 except 吞掉异常，**事务会停留在 aborted 状态**，
    之后任何语句都报 InFailedSqlTransaction（2026-09-14 港股全市场回填收尾时实际踩中）。
    """
    from db import get_conn
    try:
        with get_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    f"ALTER TABLE {tbl} ALTER COLUMN change_pct TYPE {_CHANGE_PCT_TYPE} "
                    f"USING change_pct::{_CHANGE_PCT_TYPE};"
                )
        log.info(f"[{tbl}] change_pct 精度已转换为 {_CHANGE_PCT_TYPE}")
    except Exception as e:
        log.warning(f"[{tbl}] change_pct 精度转换失败（保持现状，不影响主流程）: {str(e).strip()[:200]}")


def ensure_prev_close_columns(cur, tbl: str):
    """确保 prev_close / change_pct 两列存在且精度为目标（幂等、事务安全）。

    安全要点（2026-09-14 修复 InFailedSqlTransaction）：
      · ADD COLUMN IF NOT EXISTS 是安全 no-op，可留在调用方事务内；
      · ALTER COLUMN TYPE **先读 information_schema 判断**，已是目标精度则完全跳过
        （避免无谓 ALTER 撞上视图依赖而报错）；
      · 确需转换时走 `_alter_change_pct_type` 的独立 autocommit 连接，失败只告警；
      · 不再用裸 `except: pass` —— 那会把调用方事务打成 aborted 却不自知。
    """
    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS prev_close {_PREV_CLOSE_TYPE};")
    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS change_pct {_CHANGE_PCT_TYPE};")
    if _change_pct_type_now(tbl) not in (None, _CHANGE_PCT_TARGET):
        _alter_change_pct_type(tbl)


def fill_prev_close_from_db(start=None, end=None):
    """库内按 LAG(close) 回填 prev_close / change_pct 所有仍为 NULL 的行（幂等）。

    用于覆盖「本次/之前已落库但尚缺这两列」的历史数据；采集时按股票递推写入已能覆盖
    新采部分，本函数补齐其余。窗口参数仅作统计展示用，回填本身不依赖窗口。
    """
    tbl = "a_daily_quote"
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            ensure_prev_close_columns(cur, tbl)
            # 提前判空：若两列已全部就绪，直接跳过（避免重复跑全表 LAG 扫描）
            cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE prev_close IS NULL OR change_pct IS NULL;")
            if cur.fetchone()[0] == 0:
                log.info(f"[{tbl}] prev_close/change_pct 已全部就绪，无需回填，跳过")
                return
            cur.execute("SET LOCAL work_mem = '2GB';")
            cur.execute(
                f"""
                WITH prev AS (
                    SELECT id,
                           LAG(close) OVER (PARTITION BY stock_code ORDER BY trade_date) AS pc
                    FROM {tbl}
                )
                UPDATE {tbl} t
                SET prev_close = p.pc,
                    change_pct = CASE
                        WHEN p.pc IS NOT NULL AND p.pc <> 0
                        THEN round((t.close - p.pc) / p.pc * 100, 2)
                    END
                FROM prev p
                WHERE t.id = p.id
                  AND (t.prev_close IS NULL OR t.change_pct IS NULL);
                """
            )
            n = cur.rowcount
            cur.execute(
                f"SELECT COUNT(*) FROM {tbl} "
                f"WHERE prev_close IS NULL OR change_pct IS NULL;"
            )
            remain = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT COUNT(*) FILTER (WHERE prev_close <> 0
                        AND abs((close - prev_close) / prev_close * 100) > 100)
                FROM {tbl};
                """
            )
            dirty = cur.fetchone()[0]
    log.info(f"[a_daily_quote] prev_close/change_pct 回填 {n} 行，剩余 NULL {remain} 行，"
             f"|chg|>100% 脏数据 {dirty} 行（源数据 合股/拆股 discontinuity，原样落库）")


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
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_prev_close_columns(cur, "a_daily_quote")
        self._get_conn = get_conn
        self._bulk_upsert = bulk_upsert
        self._buf = []
        self.total = 0
        # 本次实际落库的日期区间 (min, max)：run() 单票模式据此收敛全市场总额重算范围
        self.span = None

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
        dts = [r["trade_date"] for r in chunk if r.get("trade_date")]
        if dts:
            lo, hi = min(dts), max(dts)
            self.span = (lo, hi) if self.span is None else (
                min(self.span[0], lo), max(self.span[1], hi))
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

    if a["fill_prev_close"]:
        # 仅库内按 LAG 回填 prev_close / change_pct（不采集），用于补齐历史已落库但缺这两列的行
        log.info("仅回填 prev_close / change_pct（库内 LAG，不采集）")
        fill_prev_close_from_db(start, end)
        return

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
        agg_start, agg_end, skip_agg = start, end, False
        if code:
            # 单票模式：范围收敛到本次实际落库的日期区间。
            # 全表口径（start/end 均为 None）要扫 a_daily_quote 全量 1700+ 万行 / 8700+ 个交易日，
            # 单票也白等约 3 分钟；按本次落库日期的 min/max 重算，对这些交易日的
            # SUM/COUNT 结果与全表重算完全等价（每个交易日的行都在区间内）。
            if writer is not None and writer.span:
                agg_start, agg_end = writer.span
            else:
                # --no-quotes 等不落库的单票场景：本次没有新数据，无需重算
                skip_agg = True
                log.info("本次无日线落库，跳过全市场总额重算")
        if not skip_agg:
            log.info(
                f"开始重算全市场总成交额（区间 {agg_start or '上市以来'} ~ {agg_end or '至今'}，"
                "库内 GROUP BY，日期多时可能持续数分钟）..."
            )
            aggregate_from_db(agg_start, agg_end)
        # 补齐「之前已落库但缺 prev_close/change_pct」的历史行（采集递推只覆盖本次新采部分）
        if not code:
            fill_prev_close_from_db(start, end)
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
