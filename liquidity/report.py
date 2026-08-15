"""
编排层：汇总三层流动性分析结果。

用法：
    from liquidity.report import liquidity_panorama
    result = liquidity_panorama()
    result 是一个 dict，含 market / sectors / stocks 三个键。
"""
import pandas as pd

from . import market, sector, stock


def liquidity_panorama() -> dict:
    """三层流动性全景：市场总体 + 板块 + 个股。"""
    return {
        "market": market.market_summary(),
        "sectors": sector.sector_latest(),
        "stocks": stock.stock_latest(),
    }


def print_panorama(result: dict | None = None):
    """打印三层全景的可读摘要。"""
    if result is None:
        result = liquidity_panorama()

    m = result["market"]
    print("=" * 64)
    print("【第 1 层 · 市场总体】")
    print("=" * 64)
    if m.get("turnover_latest"):
        t = m["turnover_latest"]
        print(f"  全港股总成交额: {t['total_turnover_hkd']/1e8:,.2f} 亿港元 "
              f"({t['stock_count']} 只)")
    if m.get("southbound_latest"):
        s = m["southbound_latest"]
        net = s["net_inflow_yi"]
        print(f"  南向净流入: {net:+,.2f} 亿港元" if net is not None else "  南向净流入: N/A")
    for n in m.get("notes", []):
        print(f"  ⚠ {n}")

    sec = result["sectors"]
    print("\n" + "=" * 64)
    print("【第 2 层 · 板块/指数】")
    print("=" * 64)
    if sec is None or sec.empty:
        print("  （无板块数据）")
    else:
        cols = ["sector_name", "stock_count", "turnover", "net_inflow"]
        show = sec[[c for c in cols if c in sec.columns]].copy()
        if "turnover" in show:
            show["turnover"] = show["turnover"] / 1e8
        print(show.to_string(index=False))

    stk = result["stocks"]
    print("\n" + "=" * 64)
    print("【第 3 层 · 个股】（最近交易日，按成交额降序 Top 20）")
    print("=" * 64)
    if stk is None or stk.empty:
        print("  （无个股数据）")
    else:
        show = stk.sort_values("turnover", ascending=False).head(20).copy()
        show["turnover"] = show["turnover"] / 1e8
        cols = ["stock_code", "stock_name", "turnover", "turnover_rate", "volume_ratio",
                "amihud", "est_net_inflow"]
        show = show[[c for c in cols if c in show.columns]].copy()
        if "amihud" in show:
            show["amihud"] = show["amihud"].round(4)
        if "est_net_inflow" in show:
            show["est_net_inflow"] = show["est_net_inflow"].round(2)
        print(show.to_string(index=False))

    return result


if __name__ == "__main__":
    print_panorama()
