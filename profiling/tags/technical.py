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
from ._base import _conn, _read_sql, _frame

DOMAIN = "交易特征"

# 窗口（交易日）
W_1Y, W_1M, W_1Q = 252, 21, 60
MIN_OBS_VOL = 60          # 波动类最小收益观测数
_TRADING_DAYS = 250       # 年化因子

# 个股 → 基准指数（北交所无对应指数，置空即不打 Beta）
BENCH_BY_PREFIX = {"SH": "SH.000001", "SZ": "SZ.399001", "HK": "HK.800000"}

_TIER_RANGE = {
    "1": "最低 20%", "2": "次低 20%", "3": "中间 20%", "4": "次高 20%", "5": "最高 20%",
}


def _bench_code(stock_code: str) -> str | None:
    return BENCH_BY_PREFIX.get(stock_code.split(".")[0])


def _load_history(conn, as_of: date, window: int = W_1Y + 10) -> pd.DataFrame:
    """加载窗口内的个股行情（含基准收益率）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(d) FROM (SELECT DISTINCT trade_date d FROM a_daily_quote "
            "WHERE trade_date <= %s ORDER BY d DESC LIMIT %s) t",
            (as_of, window),
        )
        start = cur.fetchone()[0]
    if start is None:
        return pd.DataFrame()

    frames = []
    for mkt, table in (("A", "a_daily_quote"), ("HK", "hk_daily_quote")):
        f = _read_sql(
            conn,
            f"SELECT stock_code, trade_date, close::float8 AS close, amount::float8 AS amount, "
            f"turnover_rate::float8 AS turnover_rate FROM {table} "
            f"WHERE trade_date BETWEEN %s AND %s",
            (start, as_of),
        )
        f["market"] = mkt
        frames.append(f)
    q = pd.concat(frames, ignore_index=True)

    # 基准收益率：按市场取对应指数
    bench_codes = list(BENCH_BY_PREFIX.values())
    b = _read_sql(
        conn,
        "SELECT bench_code, trade_date, last_price::float8 AS px FROM daily_benchmark "
        "WHERE trade_date BETWEEN %s AND %s AND bench_code = ANY(%s)",
        (start, as_of, bench_codes),
    )
    b = b.sort_values(["bench_code", "trade_date"])
    b["bm_ret"] = b.groupby("bench_code")["px"].pct_change()

    q["bench_code"] = q["stock_code"].map(_bench_code)
    q = q.merge(b[["bench_code", "trade_date", "bm_ret"]], on=["bench_code", "trade_date"], how="left")
    return q


def _metrics(s: pd.DataFrame) -> dict:
    """单只股票在窗口内的统计量（s 已按 trade_date 升序）。"""
    close = s["close"].to_numpy(dtype=float)
    n = len(close)
    out = {"mom": np.nan, "rev": np.nan, "vol": np.nan,
           "beta": np.nan, "rvol": np.nan, "dvol": np.nan,
           "liq": np.nan, "turn": np.nan, "mdd": np.nan}
    if n < 2:
        return out

    r = np.diff(close) / close[:-1]

    # 动量 12-1 与 1 月反转（注意索引：close[-22] 是 21 个交易日前）
    if n > W_1Y:
        p_1m, p_12m = close[-1 - W_1M], close[-1 - W_1Y]
        if p_12m > 0 and p_1m > 0:
            out["mom"] = (p_1m / p_12m - 1) * 100
            out["rev"] = (close[-1] / p_1m - 1) * 100

    rr = r[-W_1Y:]
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
    if len(w) > 1:
        out["mdd"] = float((1 - w / np.maximum.accumulate(w)).max() * 100)

    amt = s["amount"].to_numpy(dtype=float)[-W_1Q:]
    out["liq"] = float(np.nanmean(amt)) if len(amt) else np.nan
    to = s["turnover_rate"].to_numpy(dtype=float)[-W_1Q:]
    out["turn"] = float(np.nanmean(to)) if len(to) else np.nan
    return out


_METRICS_CACHE: dict[date, pd.DataFrame] = {}


def _compute_all(as_of: date) -> pd.DataFrame:
    """窗口统计全量计算（本域 9 个标签共用的中间结果，按 as_of 缓存一次）。

    不缓存的话 9 个标签会把同一份 ~220 万行行情各算一遍，实测从 17s ×9 ≈ 2.5 分钟
    降到一次约 20 秒。
    """
    if as_of in _METRICS_CACHE:
        return _METRICS_CACHE[as_of]

    with _conn() as conn:
        q = _load_history(conn, as_of)
    if q.empty:
        return pd.DataFrame()

    q = q.sort_values(["stock_code", "trade_date"])
    rows = []
    for code, s in q.groupby("stock_code", sort=False):
        m = _metrics(s)
        m["stock_code"] = code
        m["market"] = s["market"].iloc[0]
        rows.append(m)
    out = pd.DataFrame(rows)

    _METRICS_CACHE.clear()
    _METRICS_CACHE[as_of] = out
    return out


def _emit(df: pd.DataFrame, col: str, unit: str | None = "pct") -> pd.DataFrame:
    """按市场分组分档并构造返回帧。"""
    sub = df[df[col].notna()]
    tier = assign_tier(sub[col], 5, by=sub["market"])
    return _frame(sub["stock_code"], tier, sub[col].round(4))


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
