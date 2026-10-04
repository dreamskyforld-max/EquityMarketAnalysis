#!/usr/bin/env python3
"""交易日历初始化/增量维护 —— 写入 public.trading_calendar。

数据源与口径：
  · CN（A股）：AKShare `tool_trade_date_hist_sina`（1990-12-19 起，含当年全年预排）；
    并与 `a_daily_market_turnover` 实际交易日交叉校验（防源缺日）。
  · HK（港股）：由 `daily_market_turnover`（港股全市场成交额，每交易日一行，2019-01 起）派生；
    港股无免费全年预排日历，故 HK 日历随交易日推进天然补齐（历史全 + 当日增量）。
  · 表语义：只落「开市日」行（is_open=TRUE），即「表中存在 = 该日为交易日」。

用法:
    python3 init_trading_calendar.py            # 初始化 + 增量（幂等，可重复执行）
    python3 init_trading_calendar.py --check    # 只校验不写入

依赖: akshare（CN 源）、PG 连接。
"""
import datetime
import sys

from db import get_conn, bulk_upsert

SOURCE_CN = "akshare"
SOURCE_HK = "derived:daily_market_turnover"
_NOW = datetime.datetime.now(datetime.timezone.utc)   # 显式刷新 updated_at（监控依赖此列）


def _cn_from_akshare() -> list:
    """A股交易日历（AKShare 新浪源，1990-12-19 ~ 当年年末预排）。"""
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    dates = [d for d in df["trade_date"].tolist()]
    return [{"market": "CN", "cal_date": d, "is_open": True, "src": SOURCE_CN,
             "updated_at": _NOW} for d in dates]


def _cn_from_turnover(conn) -> set:
    """A股实际交易日（交叉校验用）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT trade_date FROM a_daily_market_turnover")
        return {r[0] for r in cur.fetchall()}


def _hk_from_turnover(conn) -> list:
    """港股交易日（由全市场成交额表派生）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT trade_date FROM daily_market_turnover")
        return [{"market": "HK", "cal_date": r[0], "is_open": True, "src": SOURCE_HK,
                 "updated_at": _NOW} for r in cur.fetchall()]


def run(codes=None, ctx=None):
    """采集入口（调度器约定签名；本任务与 codes/ctx 无关）。"""
    check_only = bool(codes and "--check" in codes)

    with get_conn() as conn:
        cn_rows = _cn_from_akshare()
        hk_rows = _hk_from_turnover(conn)
        cn_actual = _cn_from_turnover(conn)

        # 交叉校验：实际有行情的 A 股交易日是否都覆盖（源预排日历缺日 → 补进去）
        cn_set = {r["cal_date"] for r in cn_rows}
        missing = sorted(d for d in cn_actual if d not in cn_set)
        if missing:
            print(f"  ⚠️ AKShare 日历缺 {len(missing)} 个实际交易日，已补：{missing[:5]}{' ...' if len(missing) > 5 else ''}")
            cn_rows += [{"market": "CN", "cal_date": d, "is_open": True, "src": "derived:a_daily_market_turnover"}
                        for d in missing]

        print(f"  CN: {len(cn_rows)} 行（AKShare 预排 {len(cn_set)} + 补 {len(missing)}）")
        print(f"  HK: {len(hk_rows)} 行（派生自 daily_market_turnover）")

        if check_only:
            print("  --check 模式：不写入")
            return len(cn_rows) + len(hk_rows)

        bulk_upsert(conn, "trading_calendar", cn_rows + hk_rows,
                    conflict_cols=["market", "cal_date"])
        print(f"  ✅ upsert {len(cn_rows) + len(hk_rows)} 行（幂等）")

        with conn.cursor() as cur:
            cur.execute("""SELECT market, count(*), min(cal_date), max(cal_date)
                           FROM trading_calendar GROUP BY market ORDER BY market""")
            for m, cnt, mn, mx in cur.fetchall():
                print(f"  {m}: {cnt} 行  {mn} ~ {mx}")
        return len(cn_rows) + len(hk_rows)


if __name__ == "__main__":
    argv = sys.argv[1:]
    print("=" * 60)
    print("交易日历初始化/增量维护 → public.trading_calendar")
    print("=" * 60)
    run(codes=argv if argv else None)
