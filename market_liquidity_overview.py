"""
港股市场流动性全景分析（干流-支流模型，基于现有数据，无需补采）

=== 模型映射 ===
干流(水位) = 恒生指数(HK.800000) + 恒生科技(HK.800700) 的 turnover(成交额, 港元)
            -> 反映"整个港股主板旗舰河道的流量/活跃度"，2023-08 起有数据
干流(方向) = 南向资金(港股通)整体净流入 = SUM(daily_ggt_hold.est_net_inflow)
            -> 真正的跨境净买入(亿港元)，2026-04-30 起有数据
支流(个股) = 12 只港股通标的的 est_net_inflow + 占干流比重 + 比重迁移(抢流量)

=== 数据源(全部已落库) ===
  daily_benchmark  : 指数点位 + turnover(成交额)   [水位]
  daily_ggt_hold   : 港股通个股持股 + est_net_inflow(亿港元)  [方向 + 支流]
  daily_cbbc       : 牛熊证街货(杠杆多空, 可选参考)

=== 重要说明(避免误读) ===
  1. 干流水位用指数成交额代理，不是全港股2500+只总成交额，但覆盖主板旗舰，比南向覆盖更"市场级"。
  2. 指数 turnover 无内外盘拆分，故"方向"用南向净流入代理(仅内地->港股一路)。
  3. est_net_inflow = 持股变动 × 收盘价，是真实跨境净买入估算，方向可靠。
  4. volume 字段对指数为0/None，不可用，一律用 turnover。

运行: .venv/bin/python3 market_liquidity_overview.py
"""
from collections import defaultdict
from datetime import date
from db import get_conn


def _fetch(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------- 干流水位：恒指+科指 成交额(日频) ----------
def river_main_daily():
    sql = """
        SELECT trade_date,
               SUM(CASE WHEN bench_code='HK.800000' THEN turnover END) AS hsi_turn,
               SUM(CASE WHEN bench_code='HK.800700' THEN turnover END) AS hstech_turn
        FROM daily_benchmark
        WHERE bench_code IN ('HK.800000','HK.800700') AND turnover IS NOT NULL
        GROUP BY trade_date ORDER BY trade_date
    """
    rows = _fetch(sql)
    for r in rows:
        r['total_turn'] = (r['hsi_turn'] or 0) + (r['hstech_turn'] or 0)
    return rows


# ---------- 干流方向 + 支流：南向净流入(日频, 含个股) ----------
def south_daily():
    sql = """
        SELECT trade_date, stock_code, est_net_inflow
        FROM daily_ggt_hold
        WHERE est_net_inflow IS NOT NULL
        ORDER BY trade_date, stock_code
    """
    rows = _fetch(sql)
    by_day = defaultdict(lambda: defaultdict(float))
    for r in rows:
        by_day[r['trade_date']][r['stock_code']] += float(r['est_net_inflow'])
    out = []
    for d, stocks in sorted(by_day.items()):
        total = sum(stocks.values())
        rec = {'trade_date': d, 'total_net': round(total, 2)}
        rec.update({k: round(v, 2) for k, v in stocks.items()})
        out.append(rec)
    return out


# ---------- 聚合到 周/月/季/年 ----------
def _aggregate(daily_vals, freq):
    """daily_vals: list of (date, value). 返回 list of (label, sum, n)."""
    buckets = defaultdict(list)
    for d, v in daily_vals:
        if freq == 'week':
            key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        elif freq == 'month':
            key = f"{d.year}-{d.month:02d}"
        elif freq == 'quarter':
            q = (d.month - 1) // 3 + 1
            key = f"{d.year}-Q{q}"
        elif freq == 'year':
            key = str(d.year)
        else:
            key = str(d)
        buckets[key].append(v)
    return [(k, round(sum(v), 2), len(v)) for k, v in sorted(buckets.items())]


def _print_agg(title, agg, unit='亿港元'):
    print(f"\n=== {title} ===")
    print(f"{'区间':<12}{'净额('+unit+')':>18}{'交易日':>8}")
    for k, s, n in agg:
        print(f"{k:<12}{s:>18.2f}{n:>8}")


def main():
    # ---- 干流水位(成交额, 港元) ----
    main_daily = river_main_daily()
    turn_daily = [(r['trade_date'], float(r['total_turn'])) for r in main_daily]
    print("=" * 70)
    print("【干流·水位】恒指+科指 成交总额(港元) — 2023-08 起")
    print("=" * 70)
    for freq in ['year', 'quarter', 'month']:
        _print_agg(f"干流水位·{freq}聚合", _aggregate(turn_daily, freq), unit='港元')

    # 日内/日频最近趋势
    print("\n=== 干流水位·最近10个交易日 ===")
    print(f"{'日期':<12}{'恒指成交':>16}{'科指成交':>16}{'合计':>16}")
    for r in main_daily[-10:]:
        print(f"{str(r['trade_date']):<12}{float(r['hsi_turn'] or 0):>16,.0f}"
              f"{float(r['hstech_turn'] or 0):>16,.0f}{float(r['total_turn']):>16,.0f}")

    # ---- 干流方向 + 支流(南向) ----
    south = south_daily()
    net_daily = [(r['trade_date'], r['total_net']) for r in south]
    print("\n" + "=" * 70)
    print("【干流·方向】南向资金净流入(亿港元) — 2026-04-30 起")
    print("=" * 70)
    for freq in ['quarter', 'month', 'week']:
        _print_agg(f"干流方向·{freq}聚合", _aggregate(net_daily, freq))

    # ---- 支流：个股贡献 + 抢流量 ----
    print("\n" + "=" * 70)
    print("【支流】港股通12只个股 净流入汇总 + 占干流比重(全期累计)")
    print("=" * 70)
    stock_total = defaultdict(float)
    day_total = defaultdict(float)
    for r in south:
        for k, v in r.items():
            if k in ('trade_date', 'total_net'):
                continue
            stock_total[k] += v
            day_total[r['trade_date']] += 0  # placeholder
    grand = sum(stock_total.values())
    print(f"{'个股':<12}{'累计净流(亿)':>16}{'占干流%':>12}")
    for k, v in sorted(stock_total.items(), key=lambda x: -x[1]):
        share = (v / grand * 100) if grand else 0
        print(f"{k:<12}{v:>16.2f}{share:>11.1f}%")

    # 抢流量：最近一日 vs 前一交易日，各股占比变化
    print("\n=== 支流抢流量·最近两日 占比变化 ===")
    if len(south) >= 2:
        d1, d0 = south[-1], south[-2]
        t1 = d1['total_net'] or 1
        t0 = d0['total_net'] or 1
        stocks = [k for k in d1 if k not in ('trade_date', 'total_net')]
        print(f"{'个股':<12}{'昨日净流':>14}{'今日净流':>14}{'占比Δ(pp)':>12}")
        for k in stocks:
            v1 = d1.get(k, 0) or 0
            v0 = d0.get(k, 0) or 0
            share1 = v1 / t1 * 100 if t1 else 0
            share0 = v0 / t0 * 100 if t0 else 0
            print(f"{k:<12}{v0:>14.2f}{v1:>14.2f}{share1 - share0:>11.1f}")

    # ---- 杠杆多空参考(牛熊证) ----
    print("\n" + "=" * 70)
    print("【参考】牛熊证街货 多空(最近交易日)")
    print("=" * 70)
    cbbc = _fetch("""
        SELECT trade_date, stock_code, bull_street_volume, bear_street_volume
        FROM daily_cbbc
        WHERE bull_street_volume IS NOT NULL OR bear_street_volume IS NOT NULL
        ORDER BY trade_date DESC LIMIT 12
    """)
    if cbbc:
        r = cbbc[0]
        tot_bull = sum((x['bull_street_volume'] or 0) for x in cbbc)
        tot_bear = sum((x['bear_street_volume'] or 0) for x in cbbc)
        print(f"最近交易日={r['trade_date']}  样本股牛证街货={tot_bull:,}  "
              f"熊证街货={tot_bear:,}  牛熊比={(tot_bull/tot_bear):.2f}" if tot_bear else "熊证为0")
        print("  (注: daily_cbbc 为个股级牛熊证街货, 非全市场汇总)")
    else:
        print("(无 daily_cbbc 数据)")


if __name__ == '__main__':
    main()
