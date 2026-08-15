"""
第 2 层 · 板块/指数流动性
==========================

按 stock_sector 自适应分组（不硬编码板块名）：
  - 遍历 stock_sector 中所有 sector_code（指数），聚合板块的量价 + 南向资金流。
  - 后续新增板块只需在 stock_sector 表里加成分归属，本层自动纳入。

口径：板块聚合目前为「等权加总」；weight 字段暂为空，
      加权贡献待 stock_sector.weight 补数据后启用。
"""
import pandas as pd

from . import _data


def list_sectors() -> pd.DataFrame:
    """所有板块（指数）及其成分股数。"""
    df = _data.load_sector()
    if df.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "stock_count"])
    g = (
        df.groupby(["sector_code", "sector_name"], dropna=False)
        .agg(stock_count=("stock_code", "nunique"))
        .reset_index()
    )
    return g.sort_values("stock_count", ascending=False)


def sector_turnover() -> pd.DataFrame:
    """各板块日成交额（等权加总个股成交额 + 权重信息）。

    返回列：sector_code, sector_name, trade_date, turnover, stock_count,
            weight_sum, weight_coverage
      - turnover / stock_count：真实等权加总（不扭曲真实成交额）
      - weight_sum：该板块当日有成分股的权重之和（%）；weight_coverage：
        当日有数据成分股中带 weight 的占比（0~1），用于暴露权重缺口
        （如恒生指数 93 只仅 50 只有权重）。
    """
    sec = _data.load_sector()
    quote = _data.load_daily_quote()
    if sec.empty or quote.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "trade_date",
                                     "turnover", "stock_count", "weight_sum",
                                     "weight_coverage"])

    merged = quote.merge(
        sec[["stock_code", "sector_code", "sector_name", "weight"]],
        on="stock_code", how="inner",
    )
    # weight 只在成分股维度有效（同板块下每只成分股一个 weight），
    # 逐日聚合时取该板块当日有数据的成分股权重之和 + 覆盖率。
    g = (
        merged.groupby(["sector_code", "sector_name", "trade_date"])
        .agg(
            turnover=("turnover", "sum"),
            stock_count=("stock_code", "nunique"),
            weight_sum=("weight", "sum"),
            weight_cnt=("weight", "count"),
        )
        .reset_index()
    )
    g["weight_coverage"] = g["weight_cnt"] / g["stock_count"]
    g = g.drop(columns=["weight_cnt"])
    return g.sort_values(["sector_code", "trade_date"])


def sector_southbound() -> pd.DataFrame:
    """各板块日南向净流入（等权加总个股净流入 + 权重信息）。

    返回列：sector_code, sector_name, trade_date, net_inflow, hold_value,
            weight_sum, weight_coverage
    """
    sec = _data.load_sector()
    ggt = _data.load_ggt_hold()
    if sec.empty or ggt.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "trade_date",
                                     "net_inflow", "hold_value", "weight_sum",
                                     "weight_coverage"])

    merged = ggt.merge(
        sec[["stock_code", "sector_code", "sector_name", "weight"]],
        on="stock_code", how="inner",
    )
    g = (
        merged.groupby(["sector_code", "sector_name", "trade_date"])
        .agg(
            net_inflow=("est_net_inflow", "sum"),
            hold_value=("hold_value", "sum"),
            weight_sum=("weight", "sum"),
            weight_cnt=("weight", "count"),
            stock_count=("stock_code", "nunique"),
        )
        .reset_index()
    )
    g["weight_coverage"] = g["weight_cnt"] / g["stock_count"]
    g = g.drop(columns=["weight_cnt", "stock_count"])
    return g.sort_values(["sector_code", "trade_date"])


def sector_latest() -> pd.DataFrame:
    """各板块最近一个交易日的量价 + 南向资金快照（含权重信息）。"""
    st = sector_turnover()
    sb = sector_southbound()
    if st.empty:
        return pd.DataFrame()

    latest_date = st["trade_date"].max()
    st_latest = st[st["trade_date"] == latest_date].copy()
    if not sb.empty:
        sb_latest = sb[sb["trade_date"] == sb["trade_date"].max()].copy()
        st_latest = st_latest.merge(
            sb_latest[["sector_code", "net_inflow", "hold_value"]],
            on="sector_code", how="left",
        )
    st_latest["trade_date"] = latest_date
    return st_latest
