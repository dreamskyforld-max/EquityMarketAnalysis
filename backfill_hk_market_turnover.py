#!/usr/bin/env python3
"""
港股全市场总成交额 · 历史回溯（新浪 stock_hk_daily 版，一次性全量）

数据源：AKShare stock_hk_daily(adjust="qfq")（新浪财经港股个股日线，完整历史含 amount）
        —— 价格列为【QFQ 前复权】，与 a_daily_quote 口径统一（a 池走 stock_zh_a_daily(adjust="qfq")）。
用途：遍历全港股，逐只拉历史日线成交额，按日 SUM → 全市场历史总成交额，
      回溯写入 daily_market_turnover；同时个股日线写入 hk_daily_quote。

为什么用新浪而不是富途历史 K 线：
  - 富途 request_history_kline 有「历史 K 线额度」限制（100 只/7天），
    全港股 2800+ 只需 28 周才能采完，无法做全量回溯。
  - 新浪 stock_hk_daily 无额度限制，能一次拿完整历史（仅耗时较长）。

复权口径（2026-09-14 起统一为 QFQ 前复权）：
  - 价格列（open/high/low/close/prev_close）用 adjust="qfq"，消除送股/配股/派息日除权跳空；
    volume / amount 不受复权影响（已验证 raw 与 qfq 逐行完全一致），总成交额聚合口径不变。
  - QFQ 锚定在「最新交易日」：每次新的除息事件后，需重跑本脚本再锚定历史
    （不加 --resume 即全量重采覆盖；日常采集写入的当日行情在锚点处 raw==QFQ，无需额外处理）。
  - 落库用 skip_null_updates=True：本脚本产出为 NULL 的估值列（市值/PE/PB/PS/PCF/
    换手率/量比/52周）不会把库中已有有效值回写成 NULL。

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

prev_close / change_pct：
  采集时按每只股票的历史日线顺序递推（前交易日 close 即 prev_close），
  change_pct = (close-prev_close)/prev_close*100，与 daily_quote 对齐；首个交易日为 NULL。
  全量跑完后会库内再按 LAG 补齐「之前已落库但缺这两列」的历史行；亦可单独用
  --fill-prev-close 仅做库内补齐（不采集）。change_pct 精度 NUMERIC(12,4)（以真实库为准）：
  个别仙股 合股/拆股 异常日可达上万%（脏数据，源数据 discontinuity）原样落库，下游按需识别。
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
    fill_prev_close = "--fill-prev-close" in sys.argv
    codes_from_db = "--codes-from-db" in sys.argv
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
        "fill_prev_close": fill_prev_close,
        "codes_from_db": codes_from_db,
    }


def _hk_code_list():
    # 复用 get_hk_market_turnover 的缓存版（带 7 天数据库缓存）
    from get_hk_market_turnover import _hk_code_list as _cached
    return _cached()


def _db_code_list():
    """库内已有的全部港股代码（用于 QFQ 全量重写）。

    必要性：akshare 现货清单（stock_hk_spot）只覆盖在市股票，库内另有约 21 只
    不在清单内（如已停牌/退市/换股代码 HK.029xx）；若按现货清单重写，
    这些股票的 hk_daily_quote 历史会残留未复权价 → 表内口径不一致。
    """
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM hk_daily_quote "
                        "WHERE LEFT(stock_code, 2) = 'HK' ORDER BY stock_code")
            return [r[0] for r in cur.fetchall()]


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


# 新浪港股前复权因子接口（与 akshare stock_hk_daily(adjust="qfq") 同源）
_HK_QFQ_URL = "https://finance.sina.com.cn/stock/hkstock/{}/qfq.js"


def _fetch_qfq_factors(sym, max_retry: int = 3):
    """抓新浪港股前复权因子表 → {date: factor}（升序 dict）；失败返回 None。

    因子语义：接口按日期【降序】返回记录，最新一条通常 f=1（前复权锚定最新）。
    某交易日 d 的实际因子 = 「日期 <= d 的最新一条记录」的 f（从该记录日起向后生效）。

    为什么不直接用 ak.stock_hk_daily(adjust="qfq")：其实现有两条【静默回退成 raw】
    的路径（因子接口解析 SyntaxError / 因子表仅 1 行），回退时仍返回未复权数据，
    调用方无法区分——全市场批量跑会静默写入 raw 冒充 QFQ。这里自行取因子，
    拿不到就返回 None，由调用方跳过该股（绝不 raw 冒充 QFQ）。
    """
    import json
    import requests
    last_err = None
    for attempt in range(max_retry):
        try:
            r = requests.get(_HK_QFQ_URL.format(sym), timeout=30)
            r.raise_for_status()
            txt = r.text
            i, j = txt.find("{"), txt.rfind("}")
            if i < 0 or j <= i:
                raise ValueError("响应中未找到 JSON 体")
            payload = json.loads(txt[i:j + 1])
            data = payload.get("data") or []
            if not data:
                raise ValueError(f"因子表为空 (total={payload.get('total')})")
            fac = {}
            for it in data:
                fac[pd.to_datetime(it["d"]).date()] = float(it["f"])
            return dict(sorted(fac.items()))
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    log.warning(f"  {sym} 前复权因子获取失败（{max_retry} 次重试）: {last_err}")
    return None


def _fetch_daily(sym):
    """新浪港股完整历史 → QFQ 前复权 DataFrame（date 已转 date 类型）。

    做法：`stock_hk_daily(adjust="")` 取【原始价】（该分支无静默回退，可靠），
    再自取前复权因子做换算：qfq = raw × factor（factor 按日期后向生效）。
    与 a_daily_quote 口径统一（a 池走 stock_zh_a_daily(adjust="qfq")）。
    已验证：volume/amount 不受复权影响；OHLC 同行同因子缩放，不存在部分调整。

    因子拿不到 → 抛异常，由 fetch_history 计为失败并跳过（绝不 raw 冒充 QFQ）。
    """
    import akshare as ak
    raw = ak.stock_hk_daily(symbol=sym, adjust="")
    if raw is None or raw.empty:
        return pd.DataFrame()
    if "date" not in raw.columns:
        # 新浪偶发返回结构异常（缺 date 列，如 HK.02905）→ 视为无数据，避免 KeyError 逃逸
        log.warning(f"  {sym} 新浪返回缺少 date 列，按无数据处理")
        return pd.DataFrame()
    df = raw.copy()
    df["trade_date"] = pd.to_datetime(df["date"]).dt.date

    fac = _fetch_qfq_factors(sym)
    if fac is None:
        raise RuntimeError(f"{sym} 前复权因子不可用，跳过该股（避免 raw 冒充 QFQ）")

    fdf = pd.DataFrame({"d": pd.to_datetime(list(fac.keys())), "f": list(fac.values())})
    keys = pd.DataFrame({"d": pd.to_datetime(df["trade_date"])}).sort_values("d")
    merged = pd.merge_asof(keys, fdf, on="d", direction="backward")
    # 早于最早因子记录的日期 → 用最早因子（backfill）；仍为空 → 视为无复权(f=1)
    f = merged["f"].bfill().fillna(1.0).to_numpy()
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col].astype(float) * f).round(4)
    return df


def _as_float(v):
    """安全转 float：非数字 / NaN / None 返回 None，否则返回数值。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def fetch_history(start: date, code: str | None = None, quote_sink=None,
                  resume: bool = False, limit: int | None = None,
                  codes_from_db: bool = False):
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
    elif codes_from_db:
        codes = _db_code_list()
        log.info(f"代码清单来源：库内 hk_daily_quote 全量（{len(codes)} 只，含现货清单外代码）")
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
            # 按交易日升序，沿全历史递推 prev_close（不受窗口截断影响）
            df = df.sort_values("trade_date").reset_index(drop=True)
            rows = []
            prev_close = None
            # itertuples 比 iterrows 省内存/更快（不为每行构造 Series）
            for r in df.itertuples(index=False):
                d = r._asdict()
                td = d["trade_date"]
                close = _as_float(d.get("close"))
                # 涨跌幅 = (close - prev_close) / prev_close * 100（prev_close 为前交易日 close）
                if close is not None and prev_close is not None and prev_close != 0:
                    change_pct = round((close - prev_close) / prev_close * 100, 2)
                else:
                    change_pct = None
                # 窗口过滤：仅决定是否落库；窗口外行仍用于递推 prev_close
                if td >= start:
                    amt = _as_float(d.get("amount"))
                    vol = int(d["volume"]) if d.get("volume") is not None else 0
                    a = agg.setdefault(td, {"turnover": 0.0, "volume": 0, "stocks": 0})
                    a["turnover"] += (amt or 0.0)
                    a["volume"] += vol
                    # 同一只股票同一交易日仅一行，直接计数即可（无需 set 去重）
                    a["stocks"] += 1
                    rows.append(_map_quote_row(full, td, d, prev_close, change_pct))
                # 递推 prev_close（用本行 close；close 为 None 时沿用上一交易日）
                if close is not None:
                    prev_close = close
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


def _map_quote_row(code, td, row, prev_close=None, change_pct=None):
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
        # prev_close / change_pct 由采集时按 LAG(close) 同表推算（见 fetch_history 的递推逻辑），
        # 与 daily_quote.change_pct 对齐；首个交易日为 None（无前收盘价）。
        "prev_close": prev_close,
        "change_pct": change_pct,
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


# ----------------------------------------------------------------------------
# prev_close / change_pct 列确保 + 库内 LAG 回填
# ----------------------------------------------------------------------------
# change_pct 精度 NUMERIC(12,4)（以真实库为准，2026-09-14 修正）：
#   港股/A股 仙股「合股/拆股」异常日可达上万%（脏数据）；实测 max|change_pct| 已达 4737.61，
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
    之后任何语句都报 InFailedSqlTransaction —— 2026-09-14 全市场回填收尾时实际踩中。
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


def fill_prev_close_from_db(start: date | None = None):
    """库内按 LAG(close) 回填 prev_close / change_pct 所有仍为 NULL 的行（幂等）。

    用于覆盖「本次/之前已落库但尚缺这两列」的历史数据；采集时按股票递推写入已能覆盖
    新采部分，本函数补齐其余。
    """
    tbl = "hk_daily_quote"
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
    log.info(f"[hk_daily_quote] prev_close/change_pct 回填 {n} 行，剩余 NULL {remain} 行，"
             f"|chg|>100% 脏数据 {dirty} 行（源数据 合股/拆股 discontinuity，原样落库）")


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
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_prev_close_columns(cur, "hk_daily_quote")
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
        # skip_null_updates=True：本脚本只产出量价 + prev_close/change_pct，
        # 估值类列（市值/PE/PB/PS/PCF/换手率/量比/52周）产出为 NULL；
        # 冲突时仅覆盖有值字段，避免把日常采集/估值脚本已填的有效值回写成 NULL
        # （与 backfill_a_market_turnover.py 同款防护）。
        with self._get_conn() as conn:
            self._bulk_upsert(conn, "hk_daily_quote", chunk,
                              conflict_cols=["stock_code", "trade_date"],
                              skip_null_updates=True)
        self.total += len(chunk)
        log.info(f"  个股日线已落库 {self.total} 条")


def run():
    a = parse_args()
    dry: bool = bool(a["dry"])
    start: date = a["start"]  # type: ignore[assignment]
    code: str | None = a["code"]  # type: ignore[assignment]
    resume: bool = bool(a["resume"])
    limit: int | None = a["limit"]  # type: ignore[assignment]

    # 仅库内按 LAG 回填 prev_close / change_pct（不采集），补齐历史已落库但缺这两列的行
    if a["fill_prev_close"]:
        log.info("仅回填 prev_close / change_pct（库内 LAG，不采集）")
        fill_prev_close_from_db(start)
        return

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
                           resume=resume, limit=limit,
                           codes_from_db=bool(a["codes_from_db"]))
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
        # 补齐「之前已落库但缺 prev_close/change_pct」的历史行（采集递推只覆盖本次新采部分）
        if not code:
            fill_prev_close_from_db(start)
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
