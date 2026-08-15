"""
共享数据读取层：统一用 pandas 从 PostgreSQL 读表，供三层模块复用。

约定：
  - 所有函数返回 pandas.DataFrame（空表时返回空 DataFrame）。
  - 只读，不做业务计算（指标计算在各层模块内）。
  - 表名/字段名集中在常量，便于将来迁移。
"""
import pandas as pd

from db import get_conn

# 表名常量
T_MARKET_TURNOVER = "daily_market_turnover"
T_GGT_HOLD = "daily_ggt_hold"
T_DAILY_QUOTE = "hk_daily_quote"   # 港股全量简版日线数据池（字段见 load_daily_quote）
T_BENCHMARK = "daily_benchmark"
T_TICK = "tick_data"
T_SECTOR = "stock_sector"
T_STOCK_INFO = "stock_info"


def read_sql(sql: str, params=None) -> pd.DataFrame:
    """执行只读 SQL，返回 DataFrame。"""
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_market_turnover() -> pd.DataFrame:
    """全港股总成交额快照。

    注意：snapshot_time 为采集时间戳（历史行已被错误写入同一天，不可用），
    业务日期应以 trade_date 为准。此处两者都返回，按 trade_date 排序。
    """
    return read_sql(f"""
        SELECT trade_date, snapshot_time, total_turnover, total_volume, stock_count
        FROM {T_MARKET_TURNOVER}
        ORDER BY trade_date
    """)


def load_ggt_hold() -> pd.DataFrame:
    """南向资金（港股通持股）明细。"""
    return read_sql(f"""
        SELECT stock_code, trade_date, hold_num, hold_ratio,
               hold_num_change, hold_ratio_change, close_price, change_pct,
               est_net_inflow, hold_value,
               hold_value_change_1d, hold_value_change_5d, hold_value_change_10d
        FROM {T_GGT_HOLD}
        ORDER BY stock_code, trade_date
    """)


def load_daily_quote() -> pd.DataFrame:
    """个股日频行情（量价流动性基础，全市场口径）。

    数据源：hk_daily_quote（港股全量简版日线池）。
    字段重命名到旧口径，供上层分析层无感复用：
      open/high/low/close → open_price/high_price/low_price/last_price
      amount → turnover；change_pct 由 last_price 前移重算（无 prev_close 字段）。
    """
    df = read_sql(f"""
        SELECT stock_code, trade_date, open, high, low, close,
               volume, amount, turnover_rate, volume_ratio
        FROM {T_DAILY_QUOTE}
        ORDER BY stock_code, trade_date
    """)
    if df.empty:
        return df
    df = df.rename(columns={
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "last_price",
        "amount": "turnover",
    })
    return df


def load_benchmark() -> pd.DataFrame:
    """指数基准（HSI 等）。"""
    return read_sql(f"""
        SELECT bench_code, bench_name, trade_date, last_price, prev_close,
               change_pct, turnover, volume
        FROM {T_BENCHMARK}
        ORDER BY bench_code, trade_date
    """)


def load_tick() -> pd.DataFrame:
    """逐笔成交（微观结构）。"""
    return read_sql(f"""
        SELECT stock_code, tick_time, price, volume, turnover,
               ticker_direction, ticker_type
        FROM {T_TICK}
        WHERE ticker_direction IN ('BUY', 'SELL')
        ORDER BY stock_code, tick_time
    """)


def load_sector(market: str | None = "HK") -> pd.DataFrame:
    """股票-指数成分归属（板块划分）。

    market: 板块市场过滤。'HK'=只取港股指数（sector_code 前缀 HK.）；
            None=全部。默认 HK。
    """
    if market == "HK":
        where = "WHERE sector_code LIKE 'HK.%'"
    else:
        where = ""
    return read_sql(f"""
        SELECT stock_code, sector_code, sector_name, weight
        FROM {T_SECTOR}
        {where}
        ORDER BY sector_code, stock_code
    """)


def load_stock_info(market: str | None = "HK") -> pd.DataFrame:
    """活跃股票列表（含市场、名称）。

    market: 市场过滤。'HK'=仅港股；None=全部。默认 HK。
    """
    if market:
        where = "WHERE is_active = TRUE AND market = %s"
        params = (market,)
    else:
        where = "WHERE is_active = TRUE"
        params = ()
    return read_sql(f"""
        SELECT stock_code, stock_name, market, currency
        FROM {T_STOCK_INFO}
        {where}
        ORDER BY stock_code
    """, params)
