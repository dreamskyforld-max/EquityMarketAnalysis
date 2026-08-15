"""
第 1 层 · 市场总体流动性
==========================

量价水位：全港股总成交额（daily_market_turnover）
资金方向：全市场南向净流入（daily_ggt_hold 聚合）
"""
import pandas as pd

from . import _data


def market_turnover() -> pd.DataFrame:
    """全港股总成交额时序。

    返回列：snapshot_time, total_turnover, total_volume, stock_count
    注意：daily_market_turnover 为分钟/日快照，需先按日聚合。
    """
    df = _data.load_market_turnover()
    if df.empty:
        return df
    # 日频聚合：取每日最后一条快照（收盘水位）
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["snapshot_time"]).dt.date
    daily = (
        df.sort_values("snapshot_time")
        .groupby("trade_date")
        .tail(1)
        .sort_values("trade_date")
    )
    return daily


def southbound_net_inflow() -> pd.DataFrame:
    """全市场南向净流入（按日）。

    返回列：trade_date, net_inflow(亿港元), hold_value(持股市值, 港元), stock_count
    """
    df = _data.load_ggt_hold()
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "net_inflow", "hold_value", "stock_count"])
    g = (
        df.groupby("trade_date")
        .agg(
            net_inflow=("est_net_inflow", "sum"),
            hold_value=("hold_value", "sum"),
            stock_count=("stock_code", "nunique"),
        )
        .reset_index()
    )
    return g


def hsi_turnover() -> pd.DataFrame:
    """恒指(HSI)成交额时序 —— 市场量价水位的 3 年历史代理。

    返回列：trade_date, hsi_close, hsi_turnover(港元)
    恒指 = HK.800000（旗舰指数，成交额能代表市场总体活跃度）
    """
    df = _data.load_benchmark()
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "hsi_close", "hsi_turnover"])
    hsi = df[df["bench_code"] == "HK.800000"].copy()
    hsi = hsi[["trade_date", "last_price", "turnover"]].rename(
        columns={"last_price": "hsi_close", "turnover": "hsi_turnover"}
    )
    return hsi.sort_values("trade_date").reset_index(drop=True)


def southbound_trend() -> dict:
    """南向资金市场级趋势画像。

    返回：净流入时序、累计净流入、流入/流出天数、持股市值总量走势。
    """
    sf = southbound_net_inflow()
    if sf.empty:
        return {"note": "无南向资金数据", "daily": pd.DataFrame()}

    d = sf.copy()
    d = d.sort_values("trade_date").reset_index(drop=True)
    # 累计净流入
    d["cum_net_inflow"] = d["net_inflow"].cumsum()

    inflow_days = int((d["net_inflow"] > 0).sum())
    outflow_days = int((d["net_inflow"] < 0).sum())
    total_net = float(d["net_inflow"].sum())
    latest_hold_value = float(d["hold_value"].iloc[-1]) if pd.notna(d["hold_value"].iloc[-1]) else None

    return {
        "daily": d,
        "start_date": str(d["trade_date"].iloc[0]),
        "end_date": str(d["trade_date"].iloc[-1]),
        "inflow_days": inflow_days,
        "outflow_days": outflow_days,
        "total_net_inflow": total_net,          # 累计净流入（亿港元）
        "latest_hold_value": latest_hold_value,  # 最新持股市值（港元）
    }


def southbound_anomaly(threshold: float = 20.0) -> pd.DataFrame:
    """南向异常资金日识别。

    threshold: 单日净流入绝对值超过该阈值（亿港元）视为异常。
    返回列：trade_date, net_inflow, 方向标记。
    """
    sf = southbound_net_inflow()
    if sf.empty:
        return pd.DataFrame(columns=["trade_date", "net_inflow", "direction"])
    d = sf.copy()
    d["direction"] = d["net_inflow"].apply(
        lambda x: "大幅流入" if x > threshold else ("大幅流出" if x < -threshold else None)
    )
    out = d[d["direction"].notna()].sort_values("net_inflow").reset_index(drop=True)
    return out[["trade_date", "net_inflow", "direction"]]


def market_summary() -> dict:
    """市场总体概览：最近一个交易日的量价水位 + 资金方向。"""
    mv = market_turnover()
    sf = southbound_net_inflow()
    hsi = hsi_turnover()

    out = {"latest_trade_date": None, "turnover_latest": None,
           "hsi_latest": None, "southbound_latest": None, "notes": []}

    if not mv.empty:
        last = mv.iloc[-1]
        out["latest_trade_date"] = str(last["trade_date"])
        out["turnover_latest"] = {
            "total_turnover_hkd": float(last["total_turnover"]),
            "total_volume": int(last["total_volume"]),
            "stock_count": int(last["stock_count"]),
        }
    else:
        out["notes"].append("daily_market_turnover 无数据（新表，待积累）")

    if not hsi.empty:
        last = hsi.iloc[-1]
        out["hsi_latest"] = {
            "trade_date": str(last["trade_date"]),
            "hsi_close": float(last["hsi_close"]) if pd.notna(last["hsi_close"]) else None,
            "hsi_turnover_hkd": float(last["hsi_turnover"]) if pd.notna(last["hsi_turnover"]) else None,
        }

    if not sf.empty:
        last = sf.iloc[-1]
        out["southbound_latest"] = {
            "trade_date": str(last["trade_date"]),
            "net_inflow_yi": float(last["net_inflow"]) if pd.notna(last["net_inflow"]) else None,
            "hold_value_hkd": float(last["hold_value"]) if pd.notna(last["hold_value"]) else None,
            "stock_count": int(last["stock_count"]),
        }
    else:
        out["notes"].append("daily_ggt_hold 无南向资金数据")

    return out
