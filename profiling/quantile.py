#!/usr/bin/env python3
"""分档与分位工具（②③④⑤⑥⑦ 域共用）

标签体系里大量标签是「连续值 → 横截面分档」，这个转换必须口径统一：
分档用**横截面分位**（同一天全市场/行业内比较），而不是绝对阈值——
因为绝对阈值会随时间漂移（今天 100 亿是大盘，2019 年是巨无霸）。

分档规则（全项目统一）：
    · tier 取值 1..N，1 = 最小/最低，N = 最大/最高
    · 并列值用 average 排名，避免因大量并列导致某档为空
    · 样本不足（< N 个有效值）时整组不打分，返回 NaN
    · 负值/无效值不参与分档，单独走枚举值（如 PE 为负 → LOSS）

数据源说明：
    · 行情/估值快照来自 a_daily_quote（A股）与 hk_daily_quote（港股），两表字段一致
    · A股已补 ps_ttm_ratio / pcf_ttm_ratio；港股两列为空（接口未提供），相关标签仅覆盖 A 股
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

# 市场 → 行情表
QUOTE_TABLES = {"A": "a_daily_quote", "HK": "hk_daily_quote"}

_SNAPSHOT_CACHE: dict[date, pd.DataFrame] = {}


def _read_sql(conn, sql: str, params=None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def latest_trade_date(conn, table: str, as_of: date) -> date | None:
    """取 <= as_of 的最近交易日（as_of 可能是非交易日）。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT MAX(trade_date) FROM {table} WHERE trade_date <= %s", (as_of,))
        row = cur.fetchone()
        return row[0] if row else None


def load_quote_snapshot(conn, as_of: date, use_cache: bool = True) -> pd.DataFrame:
    """加载某交易日全市场的市值 / 估值 / 价格快照（②③ 域共用底层）。

    返回列：stock_code, market, trade_date, total_market_val, circular_market_val,
            pe_ttm, pb, ps_ttm, pcf_ttm, dividend_yield, close

    注：A股与港股可能落在不同的最近交易日（节假日不同），trade_date 逐行保留。
    """
    if use_cache and as_of in _SNAPSHOT_CACHE:
        return _SNAPSHOT_CACHE[as_of]

    frames = []
    for mkt, table in QUOTE_TABLES.items():
        d = latest_trade_date(conn, table, as_of)
        if d is None:
            continue
        df = _read_sql(
            conn,
            f"""
            SELECT stock_code, %s::text AS market, %s::date AS trade_date,
                   total_market_val, circular_market_val,
                   pe_ttm_ratio AS pe_ttm, pb_ratio AS pb,
                   ps_ttm_ratio AS ps_ttm, pcf_ttm_ratio AS pcf_ttm,
                   dividend_ratio_ttm AS dividend_yield, close,
                   volume::float8 AS volume, turnover_rate::float8 AS turnover_rate
            FROM {table}
            WHERE trade_date = %s
            """,
            (mkt, d, d),
        )
        frames.append(df)

    out = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(
            columns=["stock_code", "market", "trade_date", "total_market_val",
                     "circular_market_val", "pe_ttm", "pb", "ps_ttm", "pcf_ttm",
                     "dividend_yield", "close", "volume", "turnover_rate"]
        )
    )
    for c in ("total_market_val", "circular_market_val", "pe_ttm", "pb",
              "ps_ttm", "pcf_ttm", "dividend_yield", "close", "volume", "turnover_rate"):
        out[c] = pd.to_numeric(out[c], errors="coerce")

    # 只缓存最近一个 as_of：常驻进程里长期累积会吃内存
    _SNAPSHOT_CACHE.clear()
    _SNAPSHOT_CACHE[as_of] = out
    return out


def load_gics_map(conn) -> pd.DataFrame:
    """stock_code → GICS 一级部门（官方英文标识）。

    行业内分位/排名的分组依据。用一级部门（13 类）而非细粒度行业，
    保证每组样本量足够（细粒度行业里很多组不足 5 只，无法分五档）。
    """
    return _read_sql(
        conn,
        """
        SELECT ss.stock_code, sh.parent_code AS gics
        FROM stock_sector ss
        JOIN sector_hierarchy sh ON sh.sector_code = ss.sector_code
        WHERE ss.sector_type = 'INDUSTRY' AND sh.parent_code IS NOT NULL
        """,
    )


def load_stock_connect(conn) -> set:
    """港股通标的（南向可交易股票集合）。

    口径说明：取自 daily_ggt_hold（有南向持股记录的股票）。该表由南向持股明细
    采集而来，覆盖数少于港股通名单全量（未持股的标的不会出现），因此是
    「有南向持股」的近似口径，不等于官方港股通名单。
    """
    df = _read_sql(conn, "SELECT DISTINCT stock_code FROM daily_ggt_hold")
    return set(df["stock_code"]) if not df.empty else set()


# 各报告类型的标准期末日（月, 日）。financial_indicator 里混有非标准报告期的
# 业绩快报（如 report_date=2026-05-31 的年报仅 6 条），不过滤会污染「最新一期」取数
_STD_REPORT_MD = {
    "annual": ("12", "31"),
    "Q1": ("03", "31"),
    "H1": ("06", "30"),
    "Q3": ("09", "30"),
}


def _financial_std_filter(report_type: str) -> tuple[str, list]:
    """财报取数的标准报告期过滤条件与参数。"""
    md = _STD_REPORT_MD.get(report_type)
    if md:
        return ("AND EXTRACT(MONTH FROM report_date) = %s AND EXTRACT(DAY FROM report_date) = %s",
                [md[0], md[1]])
    return "", []


def load_financial_latest(conn, as_of: date, report_type: str = "annual") -> pd.DataFrame:
    """取每只股票 as_of 之前最近一期**标准报告期**财报。

    ⚠ 前视风险：financial_indicator 只有 report_date（报告期），没有实际披露日，
    所以「2025 年报」在 2025-12-31 之后即可被取到，而真实披露可能晚至次年 4 月。
    在补采 announce_date 之前，所有财报派生标签的 pit_capable 均为 False。
    """
    std, std_params = _financial_std_filter(report_type)
    return _read_sql(
        conn,
        f"""
        SELECT DISTINCT ON (stock_code)
               stock_code, report_date, revenue, net_profit, roe,
               gross_profit_rate, net_profit_rate, debt_ratio,
               operating_cash_flow, free_cash_flow, revenue_yoy, net_profit_yoy
        FROM financial_indicator
        WHERE report_type = %s AND report_date <= %s {std}
        ORDER BY stock_code, report_date DESC
        """,
        [report_type, as_of] + std_params,
    )


def load_financial_history(conn, as_of: date, report_type: str = "annual",
                           periods: int = 4) -> pd.DataFrame:
    """取每只股票 as_of 之前最近 N 期**标准报告期**财报（带序号 rn：0=最新）。

    序列类标签（ROE 稳定性、连续盈利年数、成长加速度）的底层取数。
    rn 是「期数序号」而非自然年——个别年份报告缺失时，连续性判断按期数近似。
    """
    std, std_params = _financial_std_filter(report_type)
    return _read_sql(
        conn,
        f"""
        SELECT * FROM (
            SELECT stock_code, report_date, revenue, net_profit, roe,
                   gross_profit_rate, net_profit_rate, debt_ratio,
                   operating_cash_flow, revenue_yoy, net_profit_yoy,
                   ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) - 1 AS rn
            FROM financial_indicator
            WHERE report_type = %s AND report_date <= %s {std}
        ) t
        WHERE rn <= %s
        """,
        [report_type, as_of] + std_params + [periods],
    )


def tier_or_flag(values: pd.Series, by: pd.Series, flag: str,
                 n_tiers: int = 5) -> pd.Series:
    """正值参与横截面分档，负值打 flag（LOSS/NEGATIVE），其余不打标。

    财务比率为负的含义是「亏损/资不抵债/现金流出」而非「数值最小」，
    混进分档会让「最低档」全是异常样本，必须分离。
    """
    v = pd.to_numeric(values, errors="coerce")
    out = pd.Series(pd.NA, index=v.index, dtype="object")
    pos = v > 0
    neg = v < 0
    out[neg] = flag
    if pos.any():
        out[pos] = assign_tier(v.where(pos), n_tiers, by=by)
    return out


_FIN_CTX_CACHE: dict[date, pd.DataFrame] = {}


def load_financial_context(conn, as_of: date) -> pd.DataFrame:
    """④⑤ 域共用底表：最新标准年报 × 行情市场 × GICS 部门（一次加载多处复用）。

    grp 列 = 市场|GICS，是所有「行业内分位」的分组键（同时规避跨市场币种与跨行业不可比）。
    """
    if as_of in _FIN_CTX_CACHE:
        return _FIN_CTX_CACHE[as_of]

    snap = load_quote_snapshot(conn, as_of)
    fin = load_financial_latest(conn, as_of, "annual")
    gics_map = load_gics_map(conn)

    df = (
        snap[["stock_code", "market", "total_market_val", "close"]]
        .merge(fin, on="stock_code", how="inner")
        .merge(gics_map, on="stock_code", how="left")
    )
    df["grp"] = df["market"].astype(str) + "|" + df["gics"].astype(str)

    _FIN_CTX_CACHE.clear()
    _FIN_CTX_CACHE[as_of] = df
    return df


def assign_tier(values: pd.Series, n_tiers: int = 5, by: pd.Series | None = None) -> pd.Series:
    """横截面分档：把连续值切成 1..n_tiers 档（1=最小，n=最大）。

    by: 可选的分组序列（如行业、市场），分档在组内进行（行业内分位）。
    返回字符串档位，无效值返回 NaN。
    """
    v = pd.to_numeric(values, errors="coerce")
    valid = v.notna()
    if not valid.any():
        return pd.Series(pd.NA, index=values.index, dtype="object")

    out = pd.Series(pd.NA, index=values.index, dtype="object")
    groups = pd.Series("_all", index=values.index) if by is None else by

    for _, idx in valid.groupby(groups[valid]).groups.items():
        sub = v.loc[idx]
        if len(sub) < n_tiers:
            continue  # 样本不足，不打档
        # average 排名：并列值取平均名次，避免大量并列导致某些档为空
        pct = sub.rank(pct=True, method="average")
        tier = np.ceil(pct * n_tiers).clip(1, n_tiers).astype(int)
        out.loc[idx] = tier.astype(str)

    return out


def rank_within_group(values: pd.Series, by: pd.Series, ascending: bool = False) -> pd.Series:
    """组内排名（1 起），默认大的排前面。无效值返回 NaN。"""
    v = pd.to_numeric(values, errors="coerce")
    return v.groupby(by).rank(ascending=ascending, method="min")


def historical_percentile(conn, table: str, value_col: str, as_of: date,
                          window_days: int = 750, positive_only: bool = False) -> pd.Series:
    """个股时序分位：当前值在过去 window_days 个交易日区间中的百分位（0-100）。

    positive_only=True 时排除非正值——估值比率（PE/PB/PS）为负意味着亏损或资不抵债，
    「PE=-5」在数值上比「PE=10」低，但含义是更贵而非更便宜，参与分位会反向误导。

    用 SQL 窗口函数算，避免把全历史拉进 Python。
    返回 Series: index=stock_code, value=百分位(0-100)；样本不足为 NaN。
    """
    pos_filter = f"AND {value_col} > 0" if positive_only else ""
    sql = f"""
    WITH hist AS (
        SELECT stock_code, trade_date, val,
               COUNT(*) OVER (PARTITION BY stock_code) AS n_obs
        FROM (
            SELECT stock_code, trade_date, {value_col} AS val,
                   ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY trade_date DESC) AS rn
            FROM {table}
            WHERE trade_date <= %s AND {value_col} IS NOT NULL {pos_filter}
        ) t
        WHERE rn <= %s
    ),
    cur AS (
        SELECT stock_code, val AS cur_val
        FROM hist
        WHERE trade_date = (SELECT MAX(trade_date) FROM hist)
    )
    SELECT c.stock_code,
           ROUND(100.0 * COUNT(CASE WHEN h.val <= c.cur_val THEN 1 END)
                 / NULLIF(COUNT(h.val), 0), 2) AS pct,
           MAX(h.n_obs) AS n_obs
    FROM cur c
    JOIN hist h USING (stock_code)
    GROUP BY c.stock_code, c.cur_val
    HAVING COUNT(h.val) >= 60
    """
    df = _read_sql(conn, sql, (as_of, window_days))
    if df.empty:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df.set_index("stock_code")["pct"], errors="coerce")
