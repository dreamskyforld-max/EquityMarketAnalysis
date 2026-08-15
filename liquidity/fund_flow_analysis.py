"""资金流向分析：整体 → 板块 → 个股 → 情绪佐证。

目的：看清资金「从哪离开、往哪流入」，纯描述性/横截面分析，
不做涨跌预测（方向预测已证伪，见项目记忆）。

四层：
  L0 市场水温   : 全市场成交额趋势 + 南向整体净流入
  L1 板块迁徙   : 3 个港股指数板块的成交额占比漂移 + 南向净流入
  L2 个股排行   : 南向增持 TOP / 减持 TOP（近 N 日累计）
  L3 情绪佐证   : 沽空集中 TOP + 回购活跃（可选，数据较短）

用法：
  python3 -m liquidity.fund_flow_analysis            # 默认近 20 日
  python3 -m liquidity.fund_flow_analysis --days 40  # 近 40 日
  python3 -m liquidity.fund_flow_analysis --top 15   # 各排行取 15 只
"""
from __future__ import annotations

import argparse
import warnings
from datetime import date, timedelta

import pandas as pd

from liquidity import _data
from db import get_conn

warnings.filterwarnings("ignore")


# ---- 本地读取（_data 未提供的两张表，仅本脚本使用） ----
def _load_short_selling() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT stock_code, trade_date, short_selling_amt FROM daily_short_selling "
            "ORDER BY trade_date", conn,
        )


def _load_buyback() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT stock_code, buyback_date, amount FROM daily_buyback_event "
            "ORDER BY buyback_date", conn,
        )


def _trim(df: pd.DataFrame, start: date) -> pd.DataFrame:
    """按 trade_date / buyback_date >= start 过滤（列名自适应）。"""
    if df.empty:
        return df
    for col in ("trade_date", "buyback_date"):
        if col in df.columns:
            df = df[df[col] >= start]
            break
    return df


# ---------------------------------------------------------------------------
# L0 市场水温
# ---------------------------------------------------------------------------
def _daily_market(start: date | None = None) -> pd.DataFrame:
    """全市场日成交额（港元）：读 daily_market_turnover（886天历史）。

    业务日期用 trade_date 列（snapshot_time 为损坏的采集时间戳，不可用）。
    start=None 时不截断（用于板块占比需匹配最早日期）。
    """
    mkt = _data.load_market_turnover()
    if mkt.empty:
        return pd.DataFrame()
    mkt = mkt.copy()
    mkt["trade_date"] = pd.to_datetime(mkt["trade_date"]).dt.date
    if start is not None:
        mkt = mkt[mkt["trade_date"] >= start]
    return mkt[["trade_date", "total_turnover", "stock_count"]].sort_values(
        "trade_date").reset_index(drop=True)


def market_temperature(days: int) -> pd.DataFrame:
    """全市场成交额日趋势 + 南向整体净流入。"""
    end = date.today()
    start = end - timedelta(days=days * 2)  # 多取一段算前期基线
    mkt = _daily_market(start)
    ggt = _trim(_data.load_ggt_hold(), start)
    if mkt.empty:
        return pd.DataFrame()
    if not ggt.empty:
        ggt_day = (
            ggt.groupby("trade_date")["est_net_inflow"].sum().reset_index()
        )
        mkt = mkt.merge(ggt_day, on="trade_date", how="left")
    return mkt


# ---------------------------------------------------------------------------
# L1 板块迁徙
# ---------------------------------------------------------------------------
def sector_migration(days: int) -> pd.DataFrame:
    """各港股板块：成交额占比漂移 + 南向净流入。

    返回：板块、成分数、期初/期末板块成交额、成交额变化%、南向净流入(累计)、
         期初/期末成交额占全市场比、占比漂移(pp)。
    """
    end = date.today()
    start = end - timedelta(days=days)
    quote = _data.load_daily_quote()
    sec = _data.load_sector()
    ggt = _trim(_data.load_ggt_hold(), start)
    mkt = _daily_market(None)  # 全量，匹配板块 first/last 日期算占比
    if quote.empty or sec.empty:
        return pd.DataFrame()

    # 只保留在 hk_daily_quote 有数据的板块（当前仅 3 个港股指数）
    valid = (
        sec.merge(
            quote[["stock_code"]].drop_duplicates(),
            on="stock_code", how="inner",
        )["sector_code"].unique()
    )
    sec = sec[sec["sector_code"].isin(valid)]

    # 个股日成交额 → 板块日成交额
    merged = quote.merge(
        sec[["stock_code", "sector_code", "sector_name"]],
        on="stock_code", how="inner",
    )
    sec_daily = (
        merged.groupby(["sector_code", "sector_name", "trade_date"])["turnover"]
        .sum().reset_index()
    )

    rows = []
    for sc, g in sec_daily.groupby("sector_code"):
        g = g.sort_values("trade_date")
        sname = g["sector_name"].iloc[0]
        first, last = g.iloc[0], g.iloc[-1]
        # 南向累计净流入（该板块成分）
        sub_codes = sec[sec["sector_code"] == sc]["stock_code"].tolist()
        net_in = 0.0
        if not ggt.empty:
            gg = ggt[ggt["stock_code"].isin(sub_codes)]
            net_in = float(gg["est_net_inflow"].sum(skipna=True) or 0.0)
        # 占全市场比
        mkt_sub = mkt[mkt["trade_date"].isin([first["trade_date"], last["trade_date"]])]
        def _mkt_ratio(tdate, tturn):
            mrow = mkt_sub[mkt_sub["trade_date"] == tdate]
            if mrow.empty or mrow["total_turnover"].iloc[0] in (None, 0):
                return None
            return tturn / float(mrow["total_turnover"].iloc[0]) * 100.0
        r0 = _mkt_ratio(first["trade_date"], float(first["turnover"]))
        r1 = _mkt_ratio(last["trade_date"], float(last["turnover"]))
        drift = (r1 - r0) if (r0 is not None and r1 is not None) else None
        chg = (last["turnover"] - first["turnover"]) / first["turnover"] * 100.0 if first["turnover"] else None
        rows.append({
            "sector_code": sc,
            "sector_name": sname,
            "stock_count": len(sub_codes),
            "turnover_first": float(first["turnover"]) / 1e8,
            "turnover_last": float(last["turnover"]) / 1e8,
            "turnover_chg_pct": round(chg, 1) if chg is not None else None,
            "south_net_in_亿": round(net_in, 2),
            "mkt_share_first_%": round(r0, 2) if r0 is not None else None,
            "mkt_share_last_%": round(r1, 2) if r1 is not None else None,
            "share_drift_pp": round(drift, 2) if drift is not None else None,
        })
    out = pd.DataFrame(rows).sort_values("south_net_in_亿", ascending=False)
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# L2 个股排行（南向增持/减持）
# ---------------------------------------------------------------------------
def stock_flow_rank(days: int, top: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """南向近 N 日累计净流入 TOP（增持）/ BOTTOM（减持）。

    用累计净流入 = Σ est_net_inflow（近 N 日），比单日稳。
    同时给出现价/涨跌幅参考（从 hk_daily_quote 最新一日）。
    """
    end = date.today()
    start = end - timedelta(days=days)
    ggt = _trim(_data.load_ggt_hold(), start)
    quote = _data.load_daily_quote()
    if ggt.empty:
        return pd.DataFrame(), pd.DataFrame()

    g_sum = ggt.groupby("stock_code")["est_net_inflow"].sum(skipna=True)
    g_cnt = ggt.groupby("stock_code")["est_net_inflow"].count()  # 非 NaN 数
    cum = g_sum.reset_index()  # 列: stock_code, est_net_inflow
    cum["cum_net_亿"] = cum["est_net_inflow"]
    # 全 NaN 组（count=0）sum 会得 0.0，需还原为 NaN，避免污染减持排序
    cum.loc[g_cnt[g_cnt == 0].index, "cum_net_亿"] = float("nan")

    # 参考：最新一日收盘价与涨跌幅
    if not quote.empty:
        latest = quote.sort_values("trade_date").groupby("stock_code").tail(1)
        latest = latest[["stock_code", "last_price", "turnover"]]
        latest["turnover_亿"] = latest["turnover"] / 1e8
        cum = cum.merge(
            latest[["stock_code", "last_price", "turnover_亿"]],
            on="stock_code", how="left",
        )
    else:
        cum["last_price"] = None
        cum["turnover_亿"] = None

    cum = cum.sort_values("cum_net_亿", ascending=False).reset_index(drop=True)
    buy = cum.head(top).copy()
    # 减持：取 cum_net_亿 最小且非 NaN 的 top 只（先 dropna 再升序取头）
    sell = cum.dropna(subset=["cum_net_亿"]).sort_values("cum_net_亿").head(top).copy()
    return buy, sell


# ---------------------------------------------------------------------------
# L3 情绪佐证
# ---------------------------------------------------------------------------
def sentiment_top(days: int, top: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """沽空集中 TOP（机构撤离信号）+ 回购活跃（公司接盘信号）。"""
    end = date.today()
    start = end - timedelta(days=days)
    short = _trim(_load_short_selling(), start)
    buyback = _trim(_load_buyback(), start)

    short_top = pd.DataFrame()
    if not short.empty:
        s = short.groupby("stock_code")["short_selling_amt"].sum(skipna=True).reset_index()
        s = s.sort_values("short_selling_amt", ascending=False).head(top)
        s["short_amt_亿"] = s["short_selling_amt"].round(2)
        short_top = s[["stock_code", "short_amt_亿"]].reset_index(drop=True)

    buyback_top = pd.DataFrame()
    if not buyback.empty:
        b = buyback.groupby("stock_code")["amount"].sum(skipna=True).reset_index()
        b = b.sort_values("amount", ascending=False).head(top)
        b["buyback_amt_亿"] = (b["amount"] / 1e8).round(2)
        buyback_top = b[["stock_code", "buyback_amt_亿"]].reset_index(drop=True)

    return short_top, buyback_top


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------
def _fmt(df: pd.DataFrame, float_cols=()) -> str:
    if df.empty:
        return "  (无数据)"
    with pd.option_context("display.max_columns", None, "display.width", 200):
        return df.to_string(index=False)


def run_fund_flow_report(days: int = 20, top: int = 10) -> str:
    lines = []
    lines.append(f"=== 资金流向分析（近 {days} 交易日，南向口径） ===")
    lines.append(f"生成日期：{date.today()}")

    # L0
    lines.append("\n--- L0 市场水温（全市场成交额 + 南向整体净流入） ---")
    mkt = market_temperature(days)
    if mkt.empty:
        lines.append("  (无全市场成交额数据)")
    else:
        last = mkt.tail(1).iloc[0]
        first = mkt.head(1).iloc[0]
        lines.append(f"  全市场成交额：期初 {float(first['total_turnover'])/1e8:.1f} 亿 → "
                     f"期末 {float(last['total_turnover'])/1e8:.1f} 亿 港元")
        if "est_net_inflow" in mkt.columns:
            net_sum = float(mkt["est_net_inflow"].sum(skipna=True) or 0)
            lines.append(f"  南向整体累计净流入（区间）：{net_sum:.2f} 亿港元")
        lines.append(f"  区间交易日数：{len(mkt)}（{mkt['trade_date'].min()} ~ {mkt['trade_date'].max()}）")

    # L1
    lines.append("\n--- L1 板块迁徙（仅覆盖有日线数据的港股板块） ---")
    sec = sector_migration(days)
    if sec.empty:
        lines.append("  (无板块数据)")
    else:
        lines.append(_fmt(sec))

    # L2
    lines.append(f"\n--- L2 个股南向增持 TOP {top}（钱往哪走） ---")
    buy, sell = stock_flow_rank(days, top)
    if buy.empty:
        lines.append("  (无南向数据)")
    else:
        lines.append(_fmt(buy[["stock_code", "cum_net_亿", "last_price", "turnover_亿"]]))
    lines.append(f"\n--- L2 个股南向减持 TOP {top}（钱从哪离开） ---")
    if sell.empty:
        lines.append("  (无南向数据)")
    else:
        lines.append(_fmt(sell[["stock_code", "cum_net_亿", "last_price", "turnover_亿"]]))

    # L3
    lines.append(f"\n--- L3 情绪佐证（近 {days} 日） ---")
    short_top, buyback_top = sentiment_top(days, top)
    if short_top.empty:
        lines.append("  沽空：无数据")
    else:
        lines.append("  沽空集中 TOP（机构撤离信号）：")
        lines.append(_fmt(short_top))
    if buyback_top.empty:
        lines.append("  回购：无数据")
    else:
        lines.append("  回购活跃 TOP（公司接盘信号）：")
        lines.append(_fmt(buyback_top))

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="资金流向分析：整体→板块→个股→情绪")
    ap.add_argument("--days", type=int, default=20, help="回看交易日数")
    ap.add_argument("--top", type=int, default=10, help="各排行条数")
    args = ap.parse_args()
    print(run_fund_flow_report(days=args.days, top=args.top))


if __name__ == "__main__":
    main()
