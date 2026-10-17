#!/usr/bin/env python3
"""
港股日线估值/市值历史回填（百度股市通 gushitong.baidu.com 源）

为什么用百度：
    hk_daily_quote 的历史行由 backfill_hk_market_turnover.py 从新浪/akshare 回填，
    只含 OHLCV，不含市值/PE/PB（其文档明写"市值由富途快照当天补"）。
    富途 get_market_snapshot 只返回当天快照，故 hk_daily_quote.total_market_val 历史全 NULL。
    注：本脚本只负责把历史市值铺满；PS/PCF 列已废弃，由画像层用 load_revenue_ttm + 总市值现算。

    百度股市通（gushitong.baidu.com/opendata，resource_id=51171）按个股返回估值时间序列：
      - 总市值      （单位：亿，需 ×1e8 转元，与富途快照/财务表同尺度）
      - 市盈率(静)  → pe_ratio
      - 市盈率(TTM) → pe_ttm_ratio
      - 市净率      → pb_ratio
    均为日频（近三年），更早（2004~约3年前）为约双周频降采样，落库时前向填充到各交易日。

    注：百度不提供 流通市值 / 股息率 / 换手率 / 量比，这些列本脚本不动
    （仍只能靠富途实时快照）。high_52w/low_52w 百度也无，但可由本表已有 OHLC
    滚动 252 交易日推导，故一并在本脚本内补全。

落库策略：
    仅 upsert 本脚本负责的列（total_market_val / pe_ratio / pe_ttm_ratio / pb_ratio /
    high_52w / low_52w），沿用 bulk_upsert(skip_null_updates=True) 字段级防护：
    新值为空保留库中原值，不误伤其他列（close/amount/富途快照填的 circular 等）。

性能设计（2026-10-17 优化；旧版全量 2833 只串行跑 8~12 小时起步，百度限流后更慢）：
    - 抓取层：requests.Session 复用 TCP+TLS 连接；单只 8 个请求
      （4 指标 × 2 period）线程池并发（max_workers=4），单只耗时从
      ≈ 8×RTT 降到 ≈ 2×RTT。
    - 限流重试：只有 ResultCode!=0 / Result 缺失 / body 截断成 <=2 点 /
      解析异常才退避重试（固定 2s、最多 3 次）；点数少但结构完整的响应
      直接采纳——旧版 min_pts=30 把上市历史短的股票误判为限流，白跑
      5 次指数退避（单请求最坏 ~120s）。
    - 连接：每只股票 1 次连接（旧版 3 次，含循环内冗余的 _ensure_table）。
    - 52 周高低：单调队列 O(n)（旧版每交易日重建 252 元素列表，
      全量约 18 亿次元素操作）。

两阶段自动串联（现仅剩阶段一）：
    阶段一：采集 total_market_val / pe / pe_ttm / pb（百度）→ 落 hk_daily_quote；
            high_52w / low_52w 由本表 OHLC 滚动 252 交易日推导补全。
    阶段二（已废弃）：曾用 total_market_val ÷ TTM财务 现算 PS_TTM/PCF_TTM 落表。
            现 PS/PCF 不再落行情表，两市统一由画像层 load_revenue_ttm + 总市值现算。
    --no-valuation / --force-valuation 参数保留仅为兼容旧命令行，不再触发任何计算。

断点续采：
    --resume 跳过已完成的股票（total_market_val 非空行数 / 总行数 >= 99%）。
    --code 单只 / --limit N 限量 / --dry-run 只探测不落库（dry 不触发阶段二）。
    --no-valuation      跳过阶段二（只采市值）。
    --force-valuation   市值未铺满也强制计算阶段二（仅在你确认无遗漏时使用）。

用法：
    python3 backfill_hk_market_val.py                      # 一次跑完市值+估值
    python3 backfill_hk_market_val.py --code HK.00700      # 单只验证
    python3 backfill_hk_market_val.py --limit 50           # 先跑 50 只试水
    python3 backfill_hk_market_val.py --resume --limit 200 # 续采本轮 200 只
    python3 backfill_hk_market_val.py --dry-run            # 只探测不落库
"""
import sys
import time
import logging
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from db import get_conn, bulk_upsert

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill_hk_market_val")

BAIDU = "https://gushitong.baidu.com/opendata"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gushitong.baidu.com/"}
# 连接复用：requests.get 每次新建 TCP+TLS 握手，全量 2.3 万次请求光握手就占大头；
# Session 在抓取线程间共享（GET 走 urllib3 连接池，线程安全）
_SESSION = requests.Session()

# 百度指标名 -> (目标列, 数值缩放)
#   总市值百度给"亿"，×1e8 转"元"（与富途快照/财务表同尺度，PS/PCF 才正确）
#   PE/PB 百度直接给倍数，缩放 1.0
INDICATORS = {
    "总市值":     ("total_market_val", 1e8),
    "市盈率(静)": ("pe_ratio", 1.0),
    "市盈率(TTM)": ("pe_ttm_ratio", 1.0),
    "市净率":     ("pb_ratio", 1.0),
}
# 优先"近三年"拿日频，再用"全部"补更早（降采样）的空缺
PERIODS = ["近三年", "全部"]
WIN_52W = 252  # 52 周约 252 个交易日

TARGET_COLS = ["total_market_val", "pe_ratio", "pe_ttm_ratio", "pb_ratio", "high_52w", "low_52w"]


# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
def parse_args():
    dry = "--dry-run" in sys.argv
    resume = "--resume" in sys.argv
    no_val = "--no-valuation" in sys.argv
    force_val = "--force-valuation" in sys.argv
    limit = None
    one = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--code" in sys.argv:
        one = sys.argv[sys.argv.index("--code") + 1]
    return {"dry": dry, "resume": resume, "limit": limit, "code": one,
            "no_valuation": no_val, "force_valuation": force_val}


# ----------------------------------------------------------------------------
# 建表/补列（幂等）
# ----------------------------------------------------------------------------
def _ensure_table():
    cols = {
        "total_market_val": "NUMERIC(20,2)",
        "pe_ratio": "NUMERIC(12,4)",
        "pe_ttm_ratio": "NUMERIC(12,4)",
        "pb_ratio": "NUMERIC(12,4)",
        "high_52w": "NUMERIC(12,4)",
        "low_52w": "NUMERIC(12,4)",
    }
    with get_conn() as conn:
        with conn.cursor() as cur:
            for c, typ in cols.items():
                cur.execute(
                    f"ALTER TABLE hk_daily_quote ADD COLUMN IF NOT EXISTS {c} {typ}")
        conn.commit()


# ----------------------------------------------------------------------------
# 百度抓取
# ----------------------------------------------------------------------------
def _code5(stock_code: str) -> str:
    return stock_code.split(".")[-1]  # HK.00700 -> 00700


def _fetch_baidu(indicator: str, code5: str, period: str, retries: int = 3):
    """返回 [(date, value), ...]；[] 表示确认无数据；None 表示限流/失败。

    百度在高频访问时会限流：HTTP 仍 200，但 ResultCode!=0 或 body 被截断成
    仅 1~2 个点（只给最新一日）。直接当"成功"会导致该股票只回填 1 天，
    故这类响应必须退避重试。

    与旧版（慢的根因之一）的区别：
      - 旧版以 min_pts=30 判定限流，把"上市历史短、点数本就少"的正常响应
        也误判为限流，白跑 5 次指数退避（1.5→7.5s，单请求最坏 ~120s）。
        现在只有 ResultCode!=0 / Result 缺失 / body 截断成 <=2 点 / 解析
        异常才重试；点数少但结构完整的响应直接采纳。
      - ResultCode==0 但无数据 = 确认无数据（返回 []），重试无意义。
      - 退避从指数 1.5/3/4.5/6/7.5s 改为固定 2s、最多 3 次。
    """
    params = {
        "openapi": "1", "dspName": "iphone", "tn": "tangram", "client": "app",
        "query": indicator, "code": code5, "word": "", "resource_id": "51171",
        "market": "hk", "tag": indicator, "chart_select": period,
        "industry_select": "", "skip_industry": "1", "finClientType": "pc",
    }
    url = BAIDU + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, headers=HEADERS, timeout=20)
            j = r.json()
        except Exception:
            time.sleep(2.0)
            continue
        if j.get("ResultCode") != 0:
            time.sleep(2.0)  # 限流特征：HTTP 200 但 ResultCode!=0
            continue
        if not j.get("Result"):
            return []  # ResultCode==0 且无 Result：确认无数据，重试无意义
        try:
            body = (j["Result"][0]["DisplayData"]["resultData"]["tplData"]
                    ["result"]["chartInfo"][0]["body"])
        except (KeyError, IndexError, TypeError):
            time.sleep(2.0)
            continue
        if not body:
            return []
        if len(body) <= 2:
            # body 被截断成 1~2 个点（只给最新一日）= 限流特征，退避重试
            time.sleep(2.0)
            continue
        out = []
        for row in body:
            if not row or len(row) < 2:
                continue
            dstr, vstr = row[0], row[1]
            if not dstr or vstr in (None, "", "0", 0):
                continue
            try:
                out.append((date.fromisoformat(str(dstr)[:10]), float(vstr)))
            except (ValueError, TypeError):
                continue
        if not out:
            return []  # 结构完整但无有效点：确认无数据
        return out
    return None


def _build_col_series(code5: str):
    """col -> {date: value}。近三年(日频)优先，全部(降采样)补更早空缺。

    8 个请求（4 指标 × 2 period）经线程池并发发起（max_workers=4）：
    串行版单只 ≈ 8×RTT + 8×0.15s sleep，并发后 ≈ 2×RTT。合并仍严格按
    INDICATORS × PERIODS 的固定顺序进行（近三年先、全部补缺），
    结果与串行版完全一致。并发度刻意压在 4，避免加剧百度限流。
    """
    jobs = [(ind, col, scale, per)
            for ind, (col, scale) in INDICATORS.items() for per in PERIODS]
    with ThreadPoolExecutor(max_workers=4) as ex:
        pts_by_job = list(ex.map(
            lambda job: _fetch_baidu(job[0], code5, job[3]), jobs))

    col_series = {col: {} for col, _ in INDICATORS.values()}
    for (ind, col, scale, _per), pts in zip(jobs, pts_by_job):
        if not pts:  # None=限流失败 / []=确认无数据，均无点可合并
            continue
        merged = col_series[col]
        for d, v in pts:
            if d not in merged:  # 近三年已填充的日期不被降采样值覆盖
                merged[d] = round(v * scale, 2 if scale == 1e8 else 4)
    return col_series


# ----------------------------------------------------------------------------
# 前向填充 + 52周高低
# ----------------------------------------------------------------------------
def _ffill(dates, sdates, svals):
    """dates / sdates 升序；对每日期取 <= 该日的最新 sval，无则 None。"""
    out, j, last = [], 0, None
    for d in dates:
        while j < len(sdates) and sdates[j] <= d:
            last = svals[j]
            j += 1
        out.append(last)
    return out


def _rolling_52w(highs, lows):
    """滚动 252 交易日高低。highs/lows 为与 dates 对齐的 float/None 列表。

    单调队列 O(n)：旧版对每个交易日重建两个 <=252 元素列表求 max/min，
    全量 2833 只约 18 亿次元素操作；队列维护窗口内递增值/递减值的下标，
    均摊 O(1)/元素。窗口定义与旧版一致：[i-251, i]（含当日；早期不足
    252 个交易日时窗口自然变短，与旧版 max(0, i-WIN+1) 等价）。
    """
    h52, l52 = [], []
    dq_h, dq_l = deque(), deque()  # 下标队列：highs 值递减 / lows 值递增
    for i in range(len(highs)):
        h, l = highs[i], lows[i]
        if h is not None:
            while dq_h and highs[dq_h[-1]] <= h:
                dq_h.pop()
            dq_h.append(i)
        if l is not None:
            while dq_l and lows[dq_l[-1]] >= l:
                dq_l.pop()
            dq_l.append(i)
        lo = i - WIN_52W + 1
        while dq_h and dq_h[0] < lo:
            dq_h.popleft()
        while dq_l and dq_l[0] < lo:
            dq_l.popleft()
        h52.append(highs[dq_h[0]] if dq_h else None)
        l52.append(lows[dq_l[0]] if dq_l else None)
    return h52, l52


# ----------------------------------------------------------------------------
# 单股票处理
# ----------------------------------------------------------------------------
def _process_stock(code, dry):
    # 先抓取、后开连接：百度限流重试期间（最坏单只几分钟）不占数据库连接；
    # 读+写共用同一个连接（旧版每只股票开 3 次连接，其中 _ensure_table 在
    # run() 已全局执行过一次，循环内重复调用纯属冗余，已移除）。
    col_series = _build_col_series(_code5(code))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trade_date, high, low FROM hk_daily_quote "
                "WHERE stock_code = %s ORDER BY trade_date", (code,))
            rows = cur.fetchall()
        if not rows:
            return 0
        dates = [r[0] for r in rows]
        highs = [float(r[1]) if r[1] is not None else None for r in rows]
        lows = [float(r[2]) if r[2] is not None else None for r in rows]

        cols_data = {}
        for col, sdict in col_series.items():
            sd = sorted(sdict.items())
            cols_data[col] = _ffill(dates, [d for d, _ in sd], [v for _, v in sd])

        h52, l52 = _rolling_52w(highs, lows)

        out_rows = []
        for i, d in enumerate(dates):
            r = {"stock_code": code, "trade_date": d}
            if cols_data.get("total_market_val") and cols_data["total_market_val"][i] is not None:
                r["total_market_val"] = cols_data["total_market_val"][i]
            if cols_data.get("pe_ratio") and cols_data["pe_ratio"][i] is not None:
                r["pe_ratio"] = cols_data["pe_ratio"][i]
            if cols_data.get("pe_ttm_ratio") and cols_data["pe_ttm_ratio"][i] is not None:
                r["pe_ttm_ratio"] = cols_data["pe_ttm_ratio"][i]
            if cols_data.get("pb_ratio") and cols_data["pb_ratio"][i] is not None:
                r["pb_ratio"] = cols_data["pb_ratio"][i]
            if h52[i] is not None:
                r["high_52w"] = h52[i]
            if l52[i] is not None:
                r["low_52w"] = l52[i]
            if len(r) > 2:
                out_rows.append(r)

        if not out_rows or dry:
            return len(out_rows)
        bulk_upsert(conn, "hk_daily_quote", out_rows,
                    conflict_cols=["stock_code", "trade_date"],
                    skip_null_updates=True)
    return len(out_rows)


# ----------------------------------------------------------------------------
# 进度辅助
# ----------------------------------------------------------------------------
def _remaining_market_codes():
    """尚未铺满市值的港股只数（覆盖率 < 99%）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT stock_code FROM hk_daily_quote WHERE stock_code LIKE 'HK.%%' "
                "GROUP BY stock_code "
                "HAVING COUNT(*) FILTER (WHERE total_market_val IS NOT NULL)::float "
                "/ COUNT(*) < 0.99) t")
            return cur.fetchone()[0]


# ----------------------------------------------------------------------------
# 主流程（两阶段自动串联：先采市值 → 再算 PS/PCF）
# ----------------------------------------------------------------------------
def run():
    a = parse_args()
    dry = a["dry"]
    _ensure_table()

    with get_conn() as conn:
        with conn.cursor() as cur:
            if a["code"]:
                codes = [a["code"]]
            else:
                cur.execute("SELECT DISTINCT stock_code FROM hk_daily_quote "
                            "WHERE stock_code LIKE 'HK.%%' ORDER BY stock_code")
                codes = [r[0] for r in cur.fetchall()]
            if a["resume"]:
                cur.execute(
                    "SELECT stock_code, COUNT(*), "
                    "COUNT(*) FILTER (WHERE total_market_val IS NOT NULL) "
                    "FROM hk_daily_quote WHERE stock_code LIKE 'HK.%%' "
                    "GROUP BY stock_code")
                # 覆盖率>=99% 视为已完成（允许百度最新日滞后于库末日造成的少数空缺）
                done = {r[0] for r in cur.fetchall()
                        if r[1] > 0 and r[2] * 1.0 / r[1] >= 0.99}
                codes = [c for c in codes if c not in done]

    if a["limit"] and len(codes) > a["limit"]:
        log.info(f"本轮限量 {a['limit']} 只（剩余 {len(codes) - a['limit']} 只下次 --resume）")
        codes = codes[:a["limit"]]

    if not codes:
        log.info("无待处理股票")
        return

    # ---- 阶段一：采集市值 / PE / PB / 52周高低 ----
    total = 0
    fail = 0
    log.info(f"【阶段一】回填港股市值/估值字段（{len(codes)} 只，dry={dry}）...")
    for i, code in enumerate(codes, 1):
        try:
            n = _process_stock(code, dry)
            total += n
            log.info(f"  [{i}/{len(codes)}] {code} -> {n} 行"
                     f"{'（dry-run）' if dry else ''}")
        except Exception as e:
            fail += 1
            log.warning(f"  [{i}/{len(codes)}] {code} 失败: {type(e).__name__}: {e}")
        time.sleep(0.2)
    log.info(f"【阶段一】完成：{len(codes) - fail} 只成功，{fail} 只失败，写入 {total} 行")

    # dry-run 不落库，市值未进库无法供阶段二使用，跳过
    if dry:
        log.info("dry-run 模式：仅预览市值采集，跳过 PS/PCF 计算。")
        return
    # 显式跳过估值计算
    if a["no_valuation"]:
        log.info("已指定 --no-valuation，跳过 PS/PCF 计算。")
        return

    # ---- 阶段二：计算 PS/PCF（依赖阶段一的总市值）----
    # 仅当市值已全部铺满（或强制）时自动进入，避免增量分轮跑导致部分交易日漏算。
    remaining = _remaining_market_codes()
    if remaining > 0 and not a["force_valuation"]:
        log.info(f"市值尚有 {remaining} 只未铺满（可能限流/无数据），本次先不计算 PS/PCF；"
                 f"请用 --resume 续采，全部铺满后将自动计算。"
                 f"（确认无遗漏要强制计算可加 --force-valuation）")
        return

    # PS/PCF 列已废弃：两市统一由画像层 quantile.load_revenue_ttm + 行情表总市值现算，
    # 不再由本回填链路写表（原第二阶段调用的 backfill_hk_valuation 已退役）。
    log.info("市值已铺满（阶段二 PS/PCF 落表已废弃，无需计算）。")


if __name__ == "__main__":
    import os
    run()
    os._exit(0)
