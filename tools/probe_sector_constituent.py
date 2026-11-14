#!/usr/bin/env python3
"""第④层「行业配置」阶段 0 数据闸门探测（**只读**：不写库、不建表、不改配置）。

对应 doc/industry_allocation_feasibility.md §4.2 阶段 0 的三项验收：
  ① 数据可用性盘点（附录 A 的 SQL，拿到各表真实起点 / 有效日数 / 行数）；
  ② D1 行业成分主数据源探测 —— 重点判定「能否拿到 PIT 历史成分」；
  ③ D2 行业成交额口径标定 —— 判定 sector_daily.turnover 是否可直接用作拥挤度分母。

用法（项目根目录执行）：
    python3 tools/probe_sector_constituent.py            # 只跑本地库（快，无需外网）
    python3 tools/probe_sector_constituent.py --sources  # 额外联网探测 AKShare 候选接口

说明：
  · 退出码恒为 0。「源不通 / 接口报错」本身就是有效探测结论，不阻塞发布。
  · 联网部分用 signal.alarm 做单接口超时（macOS 无 timeout 命令）。
  · 本脚本 SQL 一律 cur.execute(sql) 不传 params，故 LIKE 'CN.SW%' 里的 %
    不会被 psycopg2 当格式符；若日后改成参数化执行，字面 % 必须双写成 %%。
  · 结论不硬编码在脚本里：脚本只输出事实，判定与决策点写回设计文档。
"""
import os
import signal
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── ① 数据可用性盘点 ───────────────────────────────────────────────────────
# 字段：(标签, SQL)。所有 SQL 只读且带聚合，不触发资源红线 R1/R7。
DB_QUERIES = [
    ("daily_benchmark CN.SW*",
     "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date), "
     "count(DISTINCT bench_code), count(*) FROM daily_benchmark "
     "WHERE bench_code LIKE 'CN.SW%'"),
    ("regime.sector_daily",
     "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date), "
     "count(DISTINCT sw_code), count(*) FROM regime.sector_daily"),
    ("regime.sector_valuation_snapshot",
     "SELECT min(snapshot_date), max(snapshot_date), count(DISTINCT snapshot_date), "
     "count(*) FROM regime.sector_valuation_snapshot"),
    ("regime.sector_fund_flow",
     "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date), "
     "count(*) FROM regime.sector_fund_flow"),
    ("regime.sector_mapping",
     "SELECT count(*), count(*) FILTER (WHERE confirmed), min(as_of), max(as_of) "
     "FROM regime.sector_mapping"),
    ("regime.analyst_forecast_snapshot",
     "SELECT min(snapshot_date), max(snapshot_date), count(DISTINCT snapshot_date), "
     "count(*) FROM regime.analyst_forecast_snapshot"),
    ("financial_indicator",
     "SELECT min(report_date), max(report_date), count(DISTINCT stock_code), "
     "count(*) FROM financial_indicator"),
    ("daily_ggt_hold",
     "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date) "
     "FROM daily_ggt_hold"),
    ("stock_sector",
     "SELECT sector_type, count(*), count(DISTINCT stock_code), "
     "count(DISTINCT sector_code) FROM stock_sector GROUP BY 1 ORDER BY 1"),
    ("stock_sector.weight 非空",
     "SELECT count(*) FILTER (WHERE weight IS NOT NULL), count(*) FROM stock_sector"),
]


# ── ③ 成交额口径标定 ───────────────────────────────────────────────────────
# 若 sector_daily.turnover 单位是「亿元」且 31 行业完备覆盖全 A，
# 则 sum(turnover) / (a_daily_market_turnover.total_turnover / 1e8) 应 ≈ 1。
TURNOVER_SQL = """
SELECT d.trade_date, sum(d.turnover) AS sw_sum_yi,
       m.total_turnover / 1e8 AS market_yi,
       round(sum(d.turnover) / (m.total_turnover / 1e8), 6) AS ratio
FROM regime.sector_daily d
JOIN a_daily_market_turnover m ON m.trade_date = d.trade_date
WHERE d.trade_date >= %s AND d.turnover IS NOT NULL
GROUP BY d.trade_date, m.total_turnover
ORDER BY d.trade_date
"""

CROSS_SECTION_SQL = """
SELECT %s AS d, count(DISTINCT sw_code) FROM regime.sector_daily WHERE trade_date = %s
"""


def probe_db():
    print("=" * 78)
    print("① 数据可用性盘点（附录 A）")
    print("=" * 78)
    from db import get_conn
    cm = get_conn()
    conn = cm.__enter__()          # get_conn() 是 contextmanager；必须持有引用，
    try:                           # 否则裸 __enter__ 的连接会被 GC 关掉
        cur = conn.cursor()
        for label, sql in DB_QUERIES:
            try:
                cur.execute(sql)
                rows = cur.fetchall()
                print(f"  {label:<42} {rows if len(rows) > 1 else rows[0]}")
            except Exception as exc:
                print(f"  {label:<42} ERROR {type(exc).__name__}: {str(exc)[:120]}")

        print("\n  横截面分母（rank_n 的历史变化）")
        for d in ("2005-06-01", "2010-06-01", "2014-02-20", "2014-02-21",
                  "2018-06-01", "2021-12-13", "2026-11-11"):
            cur.execute(CROSS_SECTION_SQL, (d, d))
            print(f"    {d}  -> {cur.fetchone()[1]} 个行业")

        print("\n③ D2 成交额口径标定：sum(31行业 turnover) / (全A成交额 / 1e8)")
        cur.execute(TURNOVER_SQL, ("2026-11-01",))
        rows = cur.fetchall()
        for d, sw, mkt, ratio in rows[-8:]:
            print(f"    {d}  sw={sw}  mkt={round(float(mkt), 1)}  ratio={ratio}")
        if rows:
            rs = [float(r[3]) for r in rows]
            print(f"    ratio 区间 {min(rs):.6f} ~ {max(rs):.6f}  (n={len(rs)})")
    finally:
        cm.__exit__(None, None, None)


# ── ② D1 行业成分主数据源探测 ──────────────────────────────────────────────
class _Timeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _Timeout()


def _call(label, fn, timeout=35, extra=None):
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout)
    try:
        df = fn()
        cols = list(df.columns)
        info = f"shape={df.shape} cols={cols[:8]}"
        if extra:
            info += "  " + extra(df)
        print(f"  {label:<46} OK  {info}")
        return df
    except Exception as exc:
        print(f"  {label:<46} FAIL  {type(exc).__name__}: {str(exc)[:150]}")
        return None
    finally:
        signal.alarm(0)


def _weight_date(df):
    import pandas as pd
    out = []
    if "最新权重" in df.columns:
        w = pd.to_numeric(df["最新权重"], errors="coerce").sum()
        out.append(f"weight_sum={round(float(w), 2)}")
    for col in ("计入日期", "纳入日期", "变更日期"):
        if col in df.columns:
            s = df[col].astype(str)
            out.append(f"{col}={s.min()}~{s.max()}")
            break
    return "  ".join(out)


def probe_sources():
    print()
    print("=" * 78)
    print("② D1 行业成分主数据源探测（联网，单接口超时 35s / 90s）")
    print("=" * 78)
    try:
        import akshare as ak
    except ImportError as exc:
        print(f"  akshare 不可用: {exc}")
        return

    print(f"  akshare version = {getattr(ak, '__version__', '?')}")

    # A. 申万官方「个股行业分类变动历史」—— 唯一可能提供完整 eff_from/eff_to 的源
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(90)
    try:
        df = ak.stock_industry_clf_hist_sw()
        print(f"  {'stock_industry_clf_hist_sw (完整PIT候选)':<46} OK  "
              f"shape={df.shape} cols={list(df.columns)[:10]}")
    except Exception as exc:
        print(f"  {'stock_industry_clf_hist_sw (完整PIT候选)':<46} FAIL  "
              f"{type(exc).__name__}: {str(exc)[:150]}")
    finally:
        signal.alarm(0)

    # A-2. 绕开 AKShare 的 SSL 校验直连源站，判断是「证书问题」还是「源站问题」
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        r = requests.get(
            "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls",
            verify=False, timeout=35)
        print(f"  {'  直连 swsresearch xls (verify=False)':<46} "
              f"HTTP {r.status_code}  bytes={len(r.content)}")
    except Exception as exc:
        print(f"  {'  直连 swsresearch xls (verify=False)':<46} FAIL  "
              f"{type(exc).__name__}: {str(exc)[:120]}")

    # B. 当前成分 + 权重 + 计入日期（申万官方指数成分）
    for code in ("801010", "801780", "801950", "801980"):
        _call(f"index_component_sw({code})",
              lambda c=code: ak.index_component_sw(symbol=c), extra=_weight_date)

    # C. 其它候选（东财 / 新浪 / 巨潮 / 乐咕）
    _call("index_stock_cons(801010)",
          lambda: ak.index_stock_cons(symbol="801010"), extra=_weight_date)
    _call("stock_board_industry_name_em",
          lambda: ak.stock_board_industry_name_em())
    _call("sw_index_third_cons(801120.SI)",
          lambda: ak.sw_index_third_cons(symbol="801120.SI"))
    _call("stock_industry_change_cninfo(002594)",
          lambda: ak.stock_industry_change_cninfo(
              symbol="002594", start_date="20091227", end_date="20220713"),
          extra=_weight_date)
    _call("stock_industry_category_cninfo(申银万国)",
          lambda: ak.stock_industry_category_cninfo(symbol="申银万国行业分类标准"))

    print("\n  判定要点（详见设计文档 §2.2 / 阶段 0 实施记录）：")
    print("    · 有「计入日期」≠ 有 PIT：无剔除记录时，当前成分回算历史仍有幸存者偏差。")
    print("    · 权重合计 ≈100 才能用于市值/权重加权；stock_sector.weight 若全 NULL 不可依赖。")
    print("    · 逐股接口（stock_industry_change_cninfo）不能当全市场主源，只作增量校验。")


def main():
    probe_db()
    if "--sources" in sys.argv:
        probe_sources()
    else:
        print("\n  （未加 --sources，跳过联网探测；需要 D1 结论时请补上该参数）")
    print("\n探测结束。本脚本只读，未写入任何数据。")


if __name__ == "__main__":
    main()
