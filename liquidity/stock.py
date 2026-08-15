"""
第 3 层 · 个股流动性
======================

三类指标：
  1. 量价流动性：成交额、换手率、量比、Amihud 非流动性（|收益|/成交额）
  2. 资金流：南向净流入、持股市值变化
  3. 微观结构：tick 主动买卖净额、大单分档（近期）

Amihud 非流动性 = |日收益率| / 成交额（单位：%/亿港元），
值越大表示「单位成交额对价格的冲击越大」，即流动性越差。
"""
import numpy as np
import pandas as pd

from . import _data

# 大单分档阈值（按单笔成交量，股）——与项目 4 档口径一致
TICK_TIER_THRESHOLDS = {
    "small": 0,          # 小单：< 15万
    "mid": 150_000,      # 中单：15万 ~ 300万
    "big": 3_000_000,    # 大单：300万 ~ 1000万
    "super": 10_000_000,  # 特大单：>= 1000万
}


def _prep_daily_quote():
    """加载并补充日收益率（用于 Amihud）。"""
    df = _data.load_daily_quote()
    if df.empty:
        return df
    df = df.copy()
    df = df.sort_values(["stock_code", "trade_date"])
    # 日收益率（基于前收，优先用表内 change_pct，缺则用 last_price 算）
    df["ret"] = df.groupby("stock_code")["last_price"].pct_change()
    return df


def amihud() -> pd.DataFrame:
    """个股 Amihud 非流动性（日频）。

    返回列：stock_code, trade_date, amihud, ret, turnover, last_price
    amihud = |ret| / turnover（turnover 单位换算为亿港元）
    """
    df = _prep_daily_quote()
    if df.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "amihud", "ret", "turnover", "last_price"])
    # turnover 原单位：港元 → 亿港元
    df["turnover_yi"] = df["turnover"] / 1e8
    df["amihud"] = np.where(df["turnover_yi"] > 0, df["ret"].abs() / df["turnover_yi"], np.nan)
    out = df[["stock_code", "trade_date", "amihud", "ret", "turnover", "last_price"]].copy()
    return out


def liquidity_metrics() -> pd.DataFrame:
    """个股量价流动性指标（成交额、换手率、量比、Amihud）。

    量比 = 当日成交量 / 近20日平均成交量（优先用表内现成 volume_ratio，
    缺则自行计算）。
    """
    df = _prep_daily_quote()
    if df.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "turnover", "turnover_rate",
                                     "volume_ratio", "amihud", "ret", "last_price"])
    df = df.sort_values(["stock_code", "trade_date"])
    # 表内 volume_ratio 可能为 NULL，缺则按近20日均量补算
    if df["volume_ratio"].isna().any():
        vol_ma20 = df.groupby("stock_code")["volume"].transform(
            lambda x: x.shift(1).rolling(20, min_periods=5).mean()
        )
        calc = np.where(vol_ma20 > 0, df["volume"] / vol_ma20, np.nan)
        df["volume_ratio"] = df["volume_ratio"].fillna(pd.Series(calc, index=df.index))
    df["turnover_yi"] = df["turnover"] / 1e8
    df["amihud"] = np.where(df["turnover_yi"] > 0, df["ret"].abs() / df["turnover_yi"], np.nan)
    out = df[["stock_code", "trade_date", "turnover", "turnover_rate", "volume_ratio",
              "amihud", "ret", "last_price"]].copy()
    return out


def southbound_flow() -> pd.DataFrame:
    """个股南向资金流（净流入 + 持股市值变化）。"""
    df = _data.load_ggt_hold()
    if df.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "est_net_inflow",
                                     "hold_value", "hold_value_change_1d", "hold_value_change_5d",
                                     "hold_value_change_10d"])
    out = df[["stock_code", "trade_date", "est_net_inflow", "hold_value",
              "hold_value_change_1d", "hold_value_change_5d", "hold_value_change_10d"]].copy()
    return out


def tick_active_flow() -> pd.DataFrame:
    """个股 tick 主动买卖净额（近期，按日聚合）。

    BUY(外盘)=主动买入，SELL(内盘)=主动卖出；净额 = BUY金额 - SELL金额。
    """
    df = _data.load_tick()
    if df.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "buy_turnover",
                                     "sell_turnover", "net_turnover"])
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["tick_time"]).dt.date
    # 按方向拆分金额
    df["buy_amt"] = np.where(df["ticker_direction"] == "BUY", df["turnover"], 0)
    df["sell_amt"] = np.where(df["ticker_direction"] == "SELL", df["turnover"], 0)
    g = (
        df.groupby(["stock_code", "trade_date"])
        .agg(buy_turnover=("buy_amt", "sum"), sell_turnover=("sell_amt", "sum"))
        .reset_index()
    )
    g["net_turnover"] = g["buy_turnover"] - g["sell_turnover"]
    return g


def tick_tier_flow() -> pd.DataFrame:
    """个股 tick 大单分档净流入（近期，按日聚合）。

    按单笔 volume 分 4 档（小/中/大/特大），返回各档主动净买入金额。
    """
    df = _data.load_tick()
    if df.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "tier", "net_turnover"])
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["tick_time"]).dt.date

    def tier_of(v):
        if v >= TICK_TIER_THRESHOLDS["super"]:
            return "super"
        if v >= TICK_TIER_THRESHOLDS["big"]:
            return "big"
        if v >= TICK_TIER_THRESHOLDS["mid"]:
            return "mid"
        return "small"

    df["tier"] = df["volume"].map(tier_of)
    df["signed_amt"] = np.where(df["ticker_direction"] == "SELL", -df["turnover"], df["turnover"])
    g = (
        df.groupby(["stock_code", "trade_date", "tier"])
        .agg(net_turnover=("signed_amt", "sum"))
        .reset_index()
    )
    return g


def stock_latest() -> pd.DataFrame:
    """个股最近一个交易日的流动性快照（量价 + 南向）。"""
    lm = liquidity_metrics()
    sf = southbound_flow()
    if lm.empty:
        return pd.DataFrame()

    latest = lm["trade_date"].max()
    out = lm[lm["trade_date"] == latest].copy()
    # 仅保留港股（默认市场范围 HK）
    info = _data.load_stock_info(market="HK")
    if not info.empty:
        out = out.merge(info[["stock_code", "stock_name"]], on="stock_code", how="inner")
    if not sf.empty:
        sf_latest = sf[sf["trade_date"] == sf["trade_date"].max()].copy()
        out = out.merge(
            sf_latest[["stock_code", "est_net_inflow", "hold_value",
                       "hold_value_change_1d", "hold_value_change_5d"]],
            on="stock_code", how="left",
        )
    return out
