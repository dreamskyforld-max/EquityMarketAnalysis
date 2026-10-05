#!/usr/bin/env python3
"""⑦ 交易特征标签域

域定义：描述股票在**交易行为层面**的统计特征与风险暴露，回答「它怎么被交易、波动特性如何」。
判定标准：由**行情数据的统计特征**计算，**不含任何财务数据**；采用**横截面分位**。

与 ⑩ 趋势状态的分工（重要）：
    ⑦ 是「要素测量」——横截面统计暴露，不互斥、确定性、无需验证（温度/湿度/风速）
    ⑩ 是「状态判断」——个股时序状态机，互斥、判断性、需验证闭环（晴/雨/多云）
    ⑩ 的判定不读取 ⑦ 的分档结果，两者交叉组合产生增量筛选价值。

口径约定：
    · 复权价：a_daily_quote / hk_daily_quote 的 close 已是复权价，可直接计算收益率
    · 窗口按**交易日**计：252 交易日≈1 年，21≈1 月，60≈1 季度
    · 动量用 12-1（t-252→t-21）而非 12 个月：剔除最近 1 月可规避短期反转污染
    · 停牌股在窗口内行数偏少，按实际行数计算并设最小样本门槛（波动类≥60 个收益观测）
    · 分组：全部按市场（A/港股分开）——两市的波动与换手中枢差异显著
    · Beta / 残差波动需要基准：SH→上证 SH.000001，SZ→深证 SZ.399001，HK→恒指 HK.800000；
      北交所无对应基准指数，不打这两个标签

数据来源：a_daily_quote / hk_daily_quote（close / amount / turnover_rate）、daily_benchmark
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..registry import tag, TIER
from ..quantile import assign_tier
from ._base import (_conn, _read_sql, _read_sql_stream, _distinct_codes,
                    _frame, require_fresh)

DOMAIN = "交易特征"

# 窗口（交易日）
W_1Y, W_1M, W_1Q = 252, 21, 60
MIN_OBS_VOL = 60          # 波动类最小收益观测数
_TRADING_DAYS = 250       # 年化因子

# ── 大表取数参数 ─────────────────────────────────────────────────
# 全市场行情窗口 ~220 万行（A股 ~5300 只 + 港股 ~2200 只 × 262 交易日），
# 必须走 _read_sql_stream 分批 + dtype 瘦身；一次性 fetchall + object 列
# 的峰值内存 ~1GB+，是 2026-09-29/30 整机内存-IO 雪崩的直接来源。
_STREAM_CHUNK = 50_000
# 逐批计算的股票数/批：先按此粒度切股票清单、逐批加载行情并算完指标后立即
# 释放 —— 任意时刻只驻留单批数据（约 500 只 × 262 天 ≈ 13 万行），峰值内存
# 与总行数彻底脱钩（配合 _STREAM_CHUNK 的分批取数）。
_BATCH_CODES = 500
_QUOTE_DTYPES = {
    # stock_code / trade_date 是大头：object 字符串/日期对象 ~68B/行，220 万行
    # 每列约 150MB；category / datetime64 各降到 ~10-18MB。
    "stock_code": "category",
    "trade_date": "datetime64[ns]",
    # close 保持 float64：动量、收益、回撤、Beta 全部由它派生，不冒精度风险；
    # amount / turnover_rate 仅用于 60 日窗口均值（流动性/换手率），float32 够用。
    "close": "float64",
    "amount": "float32",
    "turnover_rate": "float32",
}

# 个股 → 基准指数（北交所无对应指数，置空即不打 Beta）
BENCH_BY_PREFIX = {"SH": "SH.000001", "SZ": "SZ.399001", "HK": "HK.800000"}

_TIER_RANGE = {
    "1": "最低 20%", "2": "次低 20%", "3": "中间 20%", "4": "次高 20%", "5": "最高 20%",
}


def _bench_code(stock_code: str) -> str | None:
    return BENCH_BY_PREFIX.get(stock_code.split(".")[0])


_QUOTE_TABLES = (("A", "a_daily_quote"), ("HK", "hk_daily_quote"))


def _window_start(conn, as_of: date, window: int = W_1Y + 10) -> date | None:
    """窗口起点：as_of 往前数 window 个交易日中的首个日期（日历从 a_daily_quote 推）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(d) FROM (SELECT DISTINCT trade_date d FROM a_daily_quote "
            "WHERE trade_date <= %s ORDER BY d DESC LIMIT %s) t",
            (as_of, window),
        )
        return cur.fetchone()[0]


def _load_bench(conn, start: date, as_of: date) -> pd.DataFrame:
    """窗口内的基准收益率（3 个指数 × 262 天，结果集小，走普通取数）。"""
    b = _read_sql(
        conn,
        "SELECT bench_code, trade_date, last_price::float8 AS px FROM daily_benchmark "
        "WHERE trade_date BETWEEN %s AND %s AND bench_code = ANY(%s)",
        (start, as_of, list(BENCH_BY_PREFIX.values())),
    )
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    b = b.sort_values(["bench_code", "trade_date"])
    b["bm_ret"] = b.groupby("bench_code")["px"].pct_change()
    return b[["bench_code", "trade_date", "bm_ret"]]


def _load_batch(conn, table: str, codes: list[str], start: date, as_of: date,
                bench: pd.DataFrame) -> pd.DataFrame:
    """按股票批次加载窗口行情（含基准收益列）。

    取数走 _read_sql_stream（服务端游标分批）+ _QUOTE_DTYPES 瘦身。
    WHERE 用 stock_code = ANY(...) 限定批内股票 + ORDER BY stock_code, trade_date：
    两张表都有 (stock_code, trade_date) 索引 → 索引扫描天然有序、PG 无需排序，
    下游 groupby(sort=False) 也不必再做 sort_values（省整表副本 + 排序开销）。
    """
    f = _read_sql_stream(
        conn,
        f"SELECT stock_code, trade_date, close::float8 AS close, amount::float8 AS amount, "
        f"turnover_rate::float8 AS turnover_rate FROM {table} "
        f"WHERE stock_code = ANY(%s) AND trade_date BETWEEN %s AND %s "
        f"ORDER BY stock_code, trade_date",
        (list(codes), start, as_of), chunk_size=_STREAM_CHUNK, dtypes=_QUOTE_DTYPES,
    )
    if f.empty:
        return f
    # merge 键统一为 object：stock_code 是 category，直接 map 会得到 category 型
    # 结果，与 bench 的 object 型 bench_code 混用易踩 dtype 不匹配（且 trade_date
    # 两边必须同为 datetime64）。bench_code 在 merge 后再转 category（仅列存，
    # 不参与计算）。
    f["bench_code"] = f["stock_code"].astype(str).map(_bench_code)
    f = f.merge(bench, on=["bench_code", "trade_date"], how="left")
    f["bench_code"] = f["bench_code"].astype("category")
    return f


def _metrics(s: pd.DataFrame) -> dict:
    """单只股票在窗口内的统计量（s 已按 trade_date 升序）。"""
    close = s["close"].to_numpy(dtype=float)
    out = {"mom": np.nan, "rev": np.nan, "vol": np.nan,
           "beta": np.nan, "rvol": np.nan, "dvol": np.nan,
           "liq": np.nan, "turn": np.nan, "mdd": np.nan}

    # 脏行情防御（实测：a_daily_quote 窗口内 5.6% 行 close IS NULL，多为北交所整行 NULL；
    # hk_daily_quote 有少量 close=0 的退市/无成交代码）：
    #   · close<=0 作分母 → inf 收益率，而 inf 能通过 notna() 混进分档、污染整组排序；
    #   · NaN 参与除法 → 每票每次计算刷 RuntimeWarning，淹没真正的异常。
    # 注意**不能删行**：r 与下方 bm（基准收益）按下标对齐，删行会让两者长度错位
    # （实测报错 operands could not be broadcast together with shapes (7,) (6,)）。
    # 故只做「非有限值归 NaN」，再在各自统计量内剔除 NaN —— 脏行不参与，但也不
    # 传染整只股票。对无脏数据的股票结果完全不变。
    n = len(close)
    if n < 2:
        return out

    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(close) / close[:-1]
    r = np.where(np.isfinite(r), r, np.nan)

    # 动量 12-1 与 1 月反转（注意索引：close[-22] 是 21 个交易日前）
    if n > W_1Y:
        p_1m, p_12m = close[-1 - W_1M], close[-1 - W_1Y]
        if p_12m > 0 and p_1m > 0:
            out["mom"] = (p_1m / p_12m - 1) * 100
            out["rev"] = (close[-1] / p_1m - 1) * 100

    rr = r[-W_1Y:]
    rr = rr[np.isfinite(rr)]    # 脏价产生的无效收益不参与统计（否则整只被 NaN 传染）
    if len(rr) >= MIN_OBS_VOL:
        out["vol"] = np.std(rr, ddof=1) * np.sqrt(_TRADING_DAYS) * 100
        neg = rr[rr < 0]
        if len(neg) >= MIN_OBS_VOL // 3:
            out["dvol"] = np.std(neg, ddof=1) * np.sqrt(_TRADING_DAYS) * 100

    # Beta 与残差波动：个股收益 vs 基准收益（日期已对齐）
    bm = s["bm_ret"].to_numpy(dtype=float)[1:]     # 与 r 对齐（首个收益为 NaN）
    mask = ~np.isnan(bm) & ~np.isnan(r)
    if mask.sum() >= MIN_OBS_VOL:
        x, y = bm[mask], r[mask]
        var_x = np.var(x, ddof=1)
        if var_x > 0:
            beta = np.cov(x, y, ddof=1)[0, 1] / var_x
            resid = y - (y.mean() - beta * x.mean() + beta * x)   # y − (α + βx)
            out["beta"] = beta
            out["rvol"] = np.std(resid, ddof=1) * np.sqrt(_TRADING_DAYS) * 100

    # 最大回撤（窗口内 1 − close/cummax）
    w = close[-W_1Y - 1:]
    w = w[np.isfinite(w) & (w > 0)]
    if len(w) > 1:
        out["mdd"] = float((1 - w / np.maximum.accumulate(w)).max() * 100)

    amt = s["amount"].to_numpy(dtype=float)[-W_1Q:]
    amt = amt[np.isfinite(amt)]
    out["liq"] = float(amt.mean()) if len(amt) else np.nan
    to = s["turnover_rate"].to_numpy(dtype=float)[-W_1Q:]
    to = to[np.isfinite(to)]
    out["turn"] = float(to.mean()) if len(to) else np.nan
    return out


_METRICS_CACHE: dict[date, pd.DataFrame] = {}


def clear_caches() -> None:
    """释放本域的窗口统计缓存。

    常驻进程（调度器）里模块级缓存不会随函数返回释放，上一轮的大对象会一直
    驻留到下一轮覆盖 —— 每轮计算结束后调用一次，把内存交还给系统。
    """
    _METRICS_CACHE.clear()


def _compute_all(as_of: date) -> pd.DataFrame:
    """窗口统计全量计算（本域 9 个标签共用的中间结果，按 as_of 缓存一次）。

    不缓存的话 9 个标签会把同一份 ~220 万行行情各算一遍，实测从 17s ×9 ≈ 2.5 分钟
    降到一次约 20 秒。

    按股票分批「加载 → 逐票计算 → 释放」：先取窗口内有行情的股票清单，再按
    _BATCH_CODES 切成小批逐批处理，任意时刻只驻留单批数据（2026-09-29/30
    事故的内存背景见 _STREAM_CHUNK 注释）。
    """
    if as_of in _METRICS_CACHE:
        return _METRICS_CACHE[as_of]

    with _conn() as conn:
        # 本域 9 个标签共用同一份行情窗口，数据可得性在这里一次性判定。
        # 判据由本域按自身口径定：全部指标都是「截至当日收盘」的横截面统计，
        # 日线必须当日到货。任一市场日线未到 → 该市场股票不会出现在结果集里，
        # 若照常 diff 会被判「标签失效」而关闭历史版本（2026-09-15 事故形态：
        # 单日关闭 55,035 行），故整体弃权，让现有版本原样保留。
        require_fresh(conn, "a_daily_quote", as_of, max_lag=0)
        require_fresh(conn, "hk_daily_quote", as_of, max_lag=0)
        start = _window_start(conn, as_of)
        if start is None:
            return pd.DataFrame()
        bench = _load_bench(conn, start, as_of)
        rows = []
        for mkt, table in _QUOTE_TABLES:
            codes = _distinct_codes(conn, table, start, as_of)
            for i in range(0, len(codes), _BATCH_CODES):
                batch = codes[i:i + _BATCH_CODES]
                f = _load_batch(conn, table, batch, start, as_of, bench)
                if f.empty:
                    continue
                # 行序由 _load_batch 的 SQL ORDER BY 保证（同股票连续 + trade_date 升序）。
                # observed=True 必须显式给：stock_code 是 category，默认 observed=False 会为
                # 「字典中有、本批无行」的类别产出空组 → _metrics 收到空帧返回全 NaN 垃圾行。
                for code, s in f.groupby("stock_code", sort=False, observed=True):
                    m = _metrics(s)
                    m["stock_code"] = code
                    m["market"] = mkt
                    rows.append(m)
                del f   # 单批算完立即释放，峰值与总行数脱钩
    out = pd.DataFrame(rows)

    _METRICS_CACHE.clear()
    _METRICS_CACHE[as_of] = out
    return out


def _emit(df: pd.DataFrame, col: str, unit: str | None = "pct") -> pd.DataFrame:
    """按市场分组分档并构造返回帧。"""
    sub = df[df[col].notna()]
    tier = assign_tier(sub[col], 5, by=sub["market"])
    return _frame(sub["stock_code"], tier, sub[col].round(4))


def require_benchmark(as_of: date) -> None:
    """Beta / 残差波动专属判据：基准日线未到 → 弃权。

    基准缺货不会报错，只会让 bm_ret 全为 NaN → beta/rvol 全 NaN → 结果集为空。
    若照常返回空帧，这两个标签会被 diff 判成「无此标签」而关闭全部历史版本，
    所以要显式弃权。本域其余 7 个标签不依赖基准，不受影响——判据按标签各异，
    这正是「每个标签对自己负责」而非全域一刀切的地方。
    """
    with _conn() as conn:
        require_fresh(conn, "daily_benchmark", as_of, max_lag=0)


# ═══════════════════════════════════════════════════════════════════════════
# 趋势性
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="trd_momentum_12_1", name="动量档(12-1月)", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="动量 = t-252 到 t-21 交易日的复权价涨幅（剔除最近 1 月，规避短期反转污染），"
                  "市场内五等分：1=最弱，5=最强。需窗口内 >252 个交易日",
    pit_capable=True, owner="profiling",
)
def trd_momentum_12_1(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "mom")


@tag(
    code="trd_reversal_1m", name="反转档(1月)", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={**_TIER_RANGE, "1": "近 1 月跌幅最大 20%（超跌）", "5": "近 1 月涨幅最大 20%（超涨）"},
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="近 21 个交易日涨跌幅，市场内五等分。与动量方向相反使用："
                  "档 1 是超跌组（反转策略的买入侧），档 5 是超涨组",
    pit_capable=True, owner="profiling",
)
def trd_reversal_1m(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "rev")


# ═══════════════════════════════════════════════════════════════════════════
# 波动性
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="trd_volatility_annual", name="年化波动率档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="近 252 个交易日日收益标准差 × √250 ×100，市场内五等分：1=最平稳，5=波动最大。"
                  "需 ≥60 个收益观测",
    pit_capable=True, owner="profiling",
)
def trd_volatility_annual(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "vol")


@tag(
    code="trd_beta", name="Beta档", domain=DOMAIN,
    num_unit="x",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "daily_benchmark"],
    compute_logic="近 252 个交易日个股收益对基准指数收益的回归斜率（cov/var），市场内五等分："
                  "1=低 Beta（防御），5=高 Beta（进攻）。基准：SH→上证，SZ→深证，HK→恒指；"
                  "北交所无对应基准指数不打标",
    pit_capable=True, owner="profiling",
)
def trd_beta(as_of: date) -> pd.DataFrame:
    require_benchmark(as_of)
    return _emit(_compute_all(as_of), "beta")


@tag(
    code="trd_residual_vol", name="残差波动档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote", "daily_benchmark"],
    compute_logic="剔除市场因子后的特质波动：回归残差标准差 × √250 ×100，市场内五等分。"
                  "高残差波动 = 个股特有的不确定性高（与高 Beta 是两种不同风险）",
    pit_capable=True, owner="profiling",
)
def trd_residual_vol(as_of: date) -> pd.DataFrame:
    require_benchmark(as_of)
    return _emit(_compute_all(as_of), "rvol")


@tag(
    code="trd_downside_vol", name="下行波动档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="近 252 个交易日中**下跌日**收益的标准差 × √250 ×100（上行波动不计入），"
                  "市场内五等分。相比全样波动率更贴合「亏损风险」的直觉",
    pit_capable=True, owner="profiling",
)
def trd_downside_vol(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "dvol")


# ═══════════════════════════════════════════════════════════════════════════
# 活跃度
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="trd_liquidity", name="流动性档", domain=DOMAIN,
    num_unit="CNY|HKD",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="近 60 个交易日日均成交额（原币：A 股元 / 港股港元），市场内五等分："
                  "1=流动性最差（冲击成本高），5=最好。num_value 存日均成交额原值",
    pit_capable=True, owner="profiling",
)
def trd_liquidity(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "liq")


@tag(
    code="trd_turnover", name="换手率档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER, value_range=dict(_TIER_RANGE),
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="近 60 个交易日日均换手率(%)，市场内五等分：1=最冷清，5=筹码交换最活跃。"
                  "换手率的行业与市值属性强，跨行业比较谨慎",
    pit_capable=True, owner="profiling",
)
def trd_turnover(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "turn")


# ═══════════════════════════════════════════════════════════════════════════
# 风险
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="trd_max_drawdown", name="最大回撤档", domain=DOMAIN,
    num_unit="pct",
    value_type=TIER,
    value_range={"1": "回撤最小 20%", "2": "次小 20%", "3": "中间 20%", "4": "次大 20%", "5": "回撤最大 20%"},
    source_type="stat", update_freq="daily",
    data_sources=["a_daily_quote", "hk_daily_quote"],
    compute_logic="近 252 个交易日内的最大回撤 = max(1 − close/历史峰值)×100，市场内五等分。"
                  "衡量极端下跌风险，与波动率互补（波动率看日常抖动，回撤看最坏情形）",
    pit_capable=True, owner="profiling",
)
def trd_max_drawdown(as_of: date) -> pd.DataFrame:
    return _emit(_compute_all(as_of), "mdd")
