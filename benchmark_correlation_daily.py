#!/usr/bin/env python3
"""全球基准指数与指定股票日收益率全方位相关性分析。

用法:
    python3 benchmark_correlation_daily.py                  # 默认 HK.00700
    python3 benchmark_correlation_daily.py HK.00700
    python3 benchmark_correlation_daily.py SH.600900

数据源:
  - daily_quote         日行情 (change_pct, volume, turnover, last_price, prev_close)
  - daily_benchmark     日基准指数 (change_pct, volume, last_price)

分析维度:
  §1 同期相关性排名 (Pearson + Spearman)
  §2 领先/滞后交叉相关 (lead-lag ±2天)
  §3 隔夜跳空分析 (T-1外盘 → T日股票跳空)
  §4 多窗口累计效应 (外盘N日累计 → 股票未来N日)
  §5 虹吸效应 (外盘相对跑赢 → 股票未来走弱 + 成交量验证)
  §6 趋势区间分段 (50日均线上方/下方时相关是否分裂)
  §7 阈值条件效应 (美债利率分段 / 单日剧烈波动 / 极端涨跌日)
"""

import argparse
from typing import List

import pandas as pd

from db import get_conn

MIN_DAYS = 30  # 最少公共天数才纳入分析

# ── 相关性阈值（基于 R² 经济意义，非统计显著性）──
# 1450 天数据下 r>0.05 即统计显著(p<0.05)，但 R²=0.25% 无意义
# 阈值按 R² 分级：1% → r≈0.1, 5% → r≈0.22, 10% → r≈0.32, 25% → r=0.5
R_WEAK = 0.15    # R²≈2.3%，"注意线"——有微弱关系但不值得交易
R_MEANINGFUL = 0.30  # R²≈9%，"有意义"——开始有弱预测/解释力
R_STRONG = 0.50      # R²=25%，"较强"——可靠的相关性
DELTA_NOTABLE = 0.15  # 子区间 Δr 显著差异（趋势分岔/波动日增强）


def load_data(stock_code: str) -> pd.DataFrame:
    """加载股票日行情与所有指数日行情，按 trade_date 对齐。"""
    sql = """
    WITH stock AS (
        SELECT trade_date, change_pct AS stock_ret,
               last_price AS stock_price, prev_close AS stock_prev_close,
               volume AS stock_vol, turnover AS stock_to
        FROM daily_quote
        WHERE stock_code = %s AND change_pct IS NOT NULL
    ),
    bench AS (
        SELECT trade_date, bench_code, bench_name, change_pct AS bench_ret,
               last_price AS bench_price, prev_close AS bench_prev_close,
               volume AS bench_vol
        FROM daily_benchmark
        WHERE change_pct IS NOT NULL
    )
    SELECT s.trade_date, b.bench_code, b.bench_name,
           s.stock_ret, b.bench_ret,
           s.stock_price, s.stock_prev_close,
           b.bench_price, b.bench_prev_close,
           s.stock_vol, s.stock_to, b.bench_vol
    FROM stock s
    JOIN bench b ON s.trade_date = b.trade_date
    ORDER BY b.bench_code, s.trade_date
    """
    with get_conn() as c:
        return pd.read_sql(sql, c, params=(stock_code,))


def analyze_correlation(df: pd.DataFrame) -> pd.DataFrame:
    """计算每个指数与 HK.00700 的同期 Pearson / Spearman 相关性。"""
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        n = len(grp)
        if n < MIN_DAYS:
            continue
        pe = grp["stock_ret"].corr(grp["bench_ret"])
        sp = grp["stock_ret"].corr(grp["bench_ret"], method="spearman")
        rows.append({
            "bench_code": code, "bench_name": name, "days": n,
            "pearson": round(pe, 4), "spearman": round(sp, 4),
        })
    return pd.DataFrame(rows).sort_values("pearson", ascending=False)


def analyze_lead_lag(df: pd.DataFrame, max_lag: int = 2) -> pd.DataFrame:
    """计算交叉相关：lag=-k = 指数领先(指数_T-k → 股票_T)；lag=+k = 股票领先。"""
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        stock = grp["stock_ret"]
        bench = grp["bench_ret"]
        for k in range(1, max_lag + 1):
            # lag=-k: bench 领先 — pair (bench_{T-k}, stock_T)
            r_neg = stock.corr(bench.shift(k))
            # lag=+k: stock 领先 — pair (stock_{T-k}, bench_T)
            r_pos = stock.shift(k).corr(bench)
            if not pd.isna(r_neg):
                rows.append({
                    "bench_code": code, "bench_name": name,
                    "lag": -k, "pearson": round(r_neg, 4),
                })
            if not pd.isna(r_pos):
                rows.append({
                    "bench_code": code, "bench_name": name,
                    "lag": k, "pearson": round(r_pos, 4),
                })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════
#  阶段1：多窗口累计效应
#  考察"指数 N 日累计变化" vs "股票未来 N 日累计收益"的关系
# ═══════════════════════════════════════════════════════════════════════

def analyze_window_effect(df: pd.DataFrame, windows=(5, 10, 20)) -> pd.DataFrame:
    """多窗口累计效应：bench_T-n..T 累计涨跌 → stock_T..T+n 累计涨跌。

    对每个指数，固定 N=窗口，计算：
      bench_cum = bench 过去 N 日累计收益率
      stock_fwd = 股票未来 N 日累计收益率
      r = corr(bench_cum, stock_fwd)
    """
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        stock_ret = grp["stock_ret"]
        bench_ret = grp["bench_ret"]
        s = pd.Series(stock_ret.values, index=stock_ret.index)
        for w in windows:
            # bench 过去 w 天累计
            bench_cum = bench_ret.rolling(w).sum()
            # 股票未来 w 天累计
            stock_fwd = pd.Series(
                [s.iloc[i:i + w].sum() if i + w <= len(s) else None
                 for i in range(len(s))],
                index=s.index,
            )
            r = bench_cum.corr(stock_fwd)
            if not pd.isna(r):
                rows.append({
                    "bench_code": code, "bench_name": name,
                    "window": w, "pearson": round(r, 4),
                })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════
#  阶段2：趋势区间分段（Regime）
#  benchmark 的 50 日均线上方/下方时，相关性是否截然不同
# ═══════════════════════════════════════════════════════════════════════

def analyze_trend_regime(df: pd.DataFrame, ma_window: int = 50) -> pd.DataFrame:
    """对每个指数，按其 50 日均线方向拆分为上升/下降两段，
    分别计算与股票的同期相关性。"""
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        prices = grp["bench_price"]
        if prices.isna().all():
            continue
        ma = prices.rolling(ma_window, min_periods=ma_window // 2).mean()
        regime = (prices > ma).astype(int)  # 1=上升, 0=下降
        stock = grp["stock_ret"]
        bench = grp["bench_ret"]

        for label, mask in [("上升", regime == 1), ("下降", regime == 0)]:
            n = mask.sum()
            if n < MIN_DAYS:
                continue
            r = stock[mask].corr(bench[mask])
            if pd.isna(r):
                continue
            rows.append({
                "bench_code": code, "bench_name": name,
                "regime": label, "n": n,
                "pearson": round(r, 4),
            })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════
#  阶段3：阈值条件效应
#  a) 美债按利率水平分段  b) 所有指数按单日剧烈波动过滤
# ═══════════════════════════════════════════════════════════════════════

def analyze_threshold_effect(df: pd.DataFrame) -> dict:
    """阈值条件效应，返回两个子结果字典。"""
    # ── a) 美债利率水平分段 ──
    rate_bins = []
    for code in ["US.DGS2", "US.DGS10"]:
        grp = df[df["bench_code"] == code]
        if grp.empty:
            continue
        grp = grp.set_index("trade_date").sort_index()
        prices = grp["bench_price"]
        stock = grp["stock_ret"]
        bench = grp["bench_ret"]
        bins = [0, 2, 4, 5, float("inf")]
        labels = ["<2%", "2~4%", "4~5%", ">5%"]
        name = grp["bench_name"].iloc[0]
        for lo, hi, lbl in zip(bins[:-1], bins[1:], labels):
            mask = (prices >= lo) & (prices < hi)
            n = mask.sum()
            if n < MIN_DAYS:
                continue
            r = stock[mask].corr(bench[mask])
            if pd.isna(r):
                continue
            rate_bins.append({
                "bench_code": code, "bench_name": name,
                "rate_range": lbl, "n": n, "pearson": round(r, 4),
            })

    # ── b) 单日剧烈波动（|bench_ret| > 2σ）vs 正常日 ──
    shock_rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        bench_ret = grp["bench_ret"]
        stock = grp["stock_ret"]
        if len(bench_ret) < MIN_DAYS:
            continue
        sigma = bench_ret.std()
        if sigma is None or sigma == 0:
            continue
        shock_mask = bench_ret.abs() > 2 * sigma
        normal_mask = ~shock_mask
        for label, mask in [("剧烈波动(|ret|>2σ)", shock_mask), ("正常", normal_mask)]:
            n = mask.sum()
            if n < 10:
                continue
            r = stock[mask].corr(bench_ret[mask])
            if pd.isna(r):
                continue
            shock_rows.append({
                "bench_code": code, "bench_name": name,
                "condition": label, "n": n,
                "pearson": round(r, 4),
            })

    return {"rate_bins": pd.DataFrame(rate_bins),
            "shock_days": pd.DataFrame(shock_rows)}


# ═══════════════════════════════════════════════════════════════════════
#  阶段4：隔夜跳空分析
#  外盘 T-1 日表现 → 股票 T 日开盘跳空
# ═══════════════════════════════════════════════════════════════════════

def analyze_overnight_gap(df: pd.DataFrame) -> pd.DataFrame:
    """T-1 日外盘收益率 → T 日股票隔夜跳空 (open−prev_close)/prev_close。"""
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        if grp["stock_price"].isna().all() or grp["stock_prev_close"].isna().all():
            continue
        gap = (grp["stock_price"] - grp["stock_prev_close"]) / grp["stock_prev_close"] * 100
        bench = grp["bench_ret"]
        # T-1 外盘 → T 日跳空
        r = gap.corr(bench.shift(1))
        if pd.isna(r):
            continue
        rows.append({
            "bench_code": code, "bench_name": name,
            "pearson": round(r, 4),
        })
    return pd.DataFrame(rows).sort_values("pearson", ascending=False)


# ═══════════════════════════════════════════════════════════════════════
#  阶段5：虹吸效应
#  外盘 N 日跑赢股票 → 股票未来 N 日走弱 + 成交量变化
# ═══════════════════════════════════════════════════════════════════════

def analyze_siphon_effect(df: pd.DataFrame, windows=(5, 10, 20)) -> pd.DataFrame:
    """虹吸 = 外盘相对跑赢股票后，股票未来表现是否走弱（负相关=虹吸成立）。
    同时检验成交量变化。"""
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        stock_ret = grp["stock_ret"]
        bench_ret = grp["bench_ret"]
        stock_vol = grp["stock_vol"]
        bench_vol = grp["bench_vol"]
        s = pd.Series(stock_ret.values, index=stock_ret.index)
        for w in windows:
            # 外盘 N 日累计
            bench_cum = bench_ret.rolling(w).sum()
            stock_cum = stock_ret.rolling(w).sum()
            # 相对强弱差 = 外盘跑赢幅度（正=虹吸压力）
            diff = bench_cum - stock_cum
            # 股票未来 N 日
            stock_fwd = pd.Series(
                [s.iloc[i:i + w].sum() if i + w <= len(s) else None
                 for i in range(len(s))],
                index=s.index,
            )
            r = diff.corr(stock_fwd)
            # 成交量验证：虹吸压力 → 股票缩量（应为负相关）
            r_vol = None
            if stock_vol.notna().sum() > 30:
                vol_chg = stock_vol.pct_change().rolling(w).mean()
                r_vol = diff.corr(vol_chg)
            rows.append({
                "bench_code": code, "bench_name": name,
                "window": w,
                "pearson": round(r, 4) if not pd.isna(r) else None,
                "vol_r": round(r_vol, 4) if r_vol is not None and not pd.isna(r_vol) else None,
            })
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════
#  阶段6：极端涨跌日
#  外盘涨/跌超过 N% 时，股票平均表现
# ═══════════════════════════════════════════════════════════════════════

def analyze_extreme_days(df: pd.DataFrame, thresholds=(1, 2)) -> pd.DataFrame:
    """外盘单日涨/跌超过阈值时，股票同日的平均收益。"""
    rows: List[dict] = []
    for (code, name), grp in df.groupby(["bench_code", "bench_name"]):
        grp = grp.set_index("trade_date").sort_index()
        stock = grp["stock_ret"]
        bench = grp["bench_ret"]
        for t in thresholds:
            for direction, label in [(1, "涨"), (-1, "跌")]:
                mask = (bench * direction) > t
                n = mask.sum()
                if n < 10:
                    continue
                avg = stock[mask].mean()
                rows.append({
                    "bench_code": code, "bench_name": name,
                    "condition": f"{label}>{t}%", "n": n,
                    "stock_avg_ret": round(avg, 4),
                })
    return pd.DataFrame(rows)


# ── 输出 ──

def _fmt(val: float) -> str:
    """格式化相关系数：≥0.5 或 ≤-0.3 加标记。"""
    s = f"{val:.4f}"
    if abs(val) >= R_STRONG:
        s += " ★"
    elif abs(val) >= R_MEANINGFUL:
        s += " *"
    else:
        s += "  "
    return s


def main(stock_code: str = "HK.00700"):
    print("=" * 64)
    print(f"  全球指数 vs {stock_code} 日收益率全方位分析")
    print("=" * 64)

    df = load_data(stock_code)
    if df.empty:
        print(f"❌ {stock_code} 无数据，请确认 daily_quote / daily_benchmark 表是否就绪")
        return

    data_start = df["trade_date"].min()
    data_end = df["trade_date"].max()

    # ═══ §1 同期相关性排名 ═══
    corr = analyze_correlation(df)
    if corr.empty:
        print(f"❌ 公共交易日少于 {MIN_DAYS} 天")
        return
    print(f"\n{'─' * 64}")
    print(f"  §1 同期相关性排名  (Pearson 降序 | ★: r≥0.5 | *: r≥0.3)")
    print(f"{'─' * 64}")
    print(f"  {'指数':<25s} {'Pearson':>8s} {'Spearman':>8s}  {'天数':>5s}")
    print(f"  {'-' * 50}")
    for _, r in corr.iterrows():
        print(f"  {r['bench_name']:<23s} {_fmt(r['pearson']):>10s} "
              f"{r['spearman']:>8.4f}  {r['days']:>5d}")

    # ═══ §2 领先/滞后 ═══
    lags = analyze_lead_lag(df)
    print(f"\n{'─' * 64}")
    print(f"  §2 领先/滞后交叉相关  (lag<0=指数领先, lag>0=指数滞后)")
    print(f"{'─' * 64}")
    print(f"  {'指数':<25s} {'lag=-2':>8s} {'lag=-1':>8s} {'同期(0)':>8s} {'lag=+1':>8s} {'lag=+2':>8s}")
    print(f"  {'-' * 72}")
    for _, r in corr.iterrows():
        code, name = r["bench_code"], r["bench_name"]
        parts = []
        for k in [-2, -1, 0, 1, 2]:
            if k == 0:
                v = r["pearson"]
            else:
                sub = lags[(lags["bench_code"] == code) & (lags["lag"] == k)]
                v = sub["pearson"].values[0] if len(sub) else float("nan")
            parts.append(_fmt(v))
        print(f"  {name:<23s} " + " ".join(parts))

    # ═══ §3 隔夜跳空 ═══
    gap = analyze_overnight_gap(df)
    if not gap.empty:
        print(f"\n{'─' * 64}")
        print(f"  §3 隔夜跳空  (T-1外盘 → T日{stock_code}跳空)")
        print(f"{'─' * 64}")
        print(f"  {'指数':<25s} {'跳空 r':>10s}")
        print(f"  {'-' * 38}")
        for _, r in gap.iterrows():
            print(f"  {r['bench_name']:<23s} {_fmt(r['pearson']):>10s}")

    # ═══ §4 多窗口累计 ═══
    win = analyze_window_effect(df)
    if not win.empty:
        print(f"\n{'─' * 64}")
        print(f"  §4 多窗口累计效应  (外盘N日累计 → {stock_code}未来N日)")
        print(f"{'─' * 64}")
        for w in [5, 10, 20]:
            sub = win[win["window"] == w].sort_values("pearson", ascending=False).head(5)
            print(f"\n  ── 窗口={w}日 ──")
            for _, r in sub.iterrows():
                tag = " ★" if r["pearson"] is not None and abs(r["pearson"]) >= R_MEANINGFUL else ""
                print(f"  {r['bench_name']:<23s} {r['pearson']:>10.4f}{tag}")

    # ═══ §5 虹吸效应 ═══
    siphon = analyze_siphon_effect(df)
    if not siphon.empty:
        print(f"\n{'─' * 64}")
        print(f"  §5 虹吸效应  (外盘跑赢 → {stock_code}未来走弱? 负相关=虹吸)")
        print(f"{'─' * 64}")
        for w in [5, 10, 20]:
            sub = siphon[siphon["window"] == w].sort_values("pearson")
            print(f"\n  ── 窗口={w}日 ──")
            print(f"  {'指数':<20s} {'虹吸 r':>8s} {'量变 r':>8s}")
            for _, r in sub.head(5).iterrows():
                r_s = f'{r["pearson"]:.4f}' if r["pearson"] is not None else '  None'
                r_v = f'{r["vol_r"]:.4f}' if r["vol_r"] is not None else '  None'
                tag = " ★" if r["pearson"] is not None and r["pearson"] < -R_WEAK else ""
                print(f"  {r['bench_name']:<18s} {r_s:>8s}{tag} {r_v:>8s}")

    # ═══ §6 趋势区间 + 阈值 ═══
    regime = analyze_trend_regime(df)
    if not regime.empty:
        print(f"\n{'─' * 64}")
        print(f"  §6a 趋势区间分段  (50日均线上方/下方)")
        print(f"{'─' * 64}")
        print(f"  {'指数':<23s} {'上升 r':>8s} {'下降 r':>8s}  {'Δr':>6s}")
        print(f"  {'-' * 50}")
        for (code, name), sub in regime.groupby(["bench_code", "bench_name"]):
            if len(sub) < 2:
                continue
            up = sub[sub["regime"] == "上升"]
            dn = sub[sub["regime"] == "下降"]
            r_up = up["pearson"].values[0] if len(up) else float("nan")
            r_dn = dn["pearson"].values[0] if len(dn) else float("nan")
            diff = abs(r_up - r_dn) if not pd.isna(r_up) and not pd.isna(r_dn) else 0
            marker = " ⚡" if diff >= 0.2 else ""
            print(f"  {name:<21s} {_fmt(r_up):>10s} {_fmt(r_dn):>10s} {diff:>6.3f}{marker}")

    thresh = analyze_threshold_effect(df)

    if not thresh["rate_bins"].empty:
        print(f"\n{'─' * 64}")
        print(f"  §6b 美债利率水平分段")
        print(f"{'─' * 64}")
        for (code, name), sub in thresh["rate_bins"].groupby(["bench_code", "bench_name"]):
            print(f"\n  {name}:")
            for _, r in sub.iterrows():
                tag = " ★" if r["pearson"] is not None and abs(r["pearson"]) >= R_MEANINGFUL else ""
                print(f"    {r['rate_range']:<10s} r={r['pearson']:.4f}{tag}  n={r['n']}")

    if not thresh["shock_days"].empty:
        print(f"\n{'─' * 64}")
        print(f"  §6c 单日剧烈波动 (|ret|>2σ vs 正常日)")
        print(f"{'─' * 64}")
        shock_summary = []
        for (code, name), sub in thresh["shock_days"].groupby(["bench_code", "bench_name"]):
            normal = sub[sub["condition"] == "正常"]
            shock = sub[sub["condition"] == "剧烈波动(|ret|>2σ)"]
            r_n = normal["pearson"].values[0] if len(normal) else float("nan")
            r_s = shock["pearson"].values[0] if len(shock) else float("nan")
            shock_summary.append((name, r_n, r_s, abs(r_s - r_n) if not pd.isna(r_s) and not pd.isna(r_n) else 0))
        shock_summary.sort(key=lambda x: x[3], reverse=True)
        print(f"  {'指数':<23s} {'正常日':>8s} {'波动日':>8s}  {'Δr':>6s}")
        for name, r_n, r_s, diff in shock_summary[:8]:
            marker = " ⚡" if diff >= DELTA_NOTABLE else ""
            print(f"  {name:<21s} {r_n:>8.4f} {r_s:>8.4f} {diff:>6.3f}{marker}")

    # ═══ §7 极端涨跌日 ═══
    extreme = analyze_extreme_days(df)
    if not extreme.empty:
        print(f"\n{'─' * 64}")
        print(f"  §7 极端涨跌日  (外盘涨/跌>N%时 {stock_code}平均收益)")
        print(f"{'─' * 64}")
        # 只展示主要指数
        focus = ["纳斯达克综合指数", "标普500指数", "恒生科技指数",
                 "日经225指数", "上证指数", "VIX恐慌指数"]
        for name in focus:
            sub = extreme[extreme["bench_name"] == name]
            if sub.empty:
                continue
            parts = []
            for _, r in sub.iterrows():
                parts.append(f'{r["condition"]}: {r["stock_avg_ret"]:+.2f}%')
            print(f"  {name:<21s} {', '.join(parts)}")

    # ═══ §8 综合评分 ═══
    print(f"\n{'─' * 64}")
    print(f"  §8 综合评分  (每指数一句话结论)")
    print(f"{'─' * 64}")

    def _get_r(df_outer, code, col="pearson"):
        """从分析结果 DataFrame 中提取某个 bench_code 的值。"""
        if df_outer is None or df_outer.empty:
            return None
        sub = df_outer[df_outer["bench_code"] == code]
        return sub[col].values[0] if len(sub) else None

    for _, r in corr.iterrows():
        code, name = r["bench_code"], r["bench_name"]
        sync = r["pearson"]       # §1 同期
        lag1 = _get_r(lags[lambda x: x["lag"] == -1], code)  # §2 T-1领先
        gap_r = _get_r(gap, code)  # §3 隔夜跳空
        siphon_5 = None
        if not siphon.empty:
            s5 = siphon[(siphon["bench_code"] == code) & (siphon["window"] == 5)]
            siphon_5 = s5["pearson"].values[0] if len(s5) else None

        # 构建评价
        signals = []
        verdict = "无关"

        # 同市场/高度同步
        if sync is not None and sync >= 0.7:
            verdict = "同市场锚定"
            signals.append(f"同步r={sync:.2f}")
        elif sync is not None and abs(sync) >= R_MEANINGFUL:
            verdict = "弱同步"
            direction = "正" if sync > 0 else "负"
            signals.append(f"{direction}相关r={sync:+.2f}")
        elif sync is not None and abs(sync) >= R_WEAK:
            verdict = "微弱相关"
            direction = "正" if sync > 0 else "负"
            signals.append(f"{direction}相关r={sync:+.2f}")

        # 隔夜跳空信号
        if gap_r is not None and abs(gap_r) >= R_WEAK:
            direction = "正向" if gap_r > 0 else "反向"
            signals.append(f"隔夜跳空({direction} {gap_r:+.2f})")

        # 极端涨跌日信号（汇率等低波动变量不靠同步 r 捕获）
        # 对低波动指数（σ<0.5%），用自身 σ 阈值而非固定 1%/2%
        extreme_signal = None
        grp = df[df["bench_code"] == code]
        bench_sigma = grp["bench_ret"].std() if len(grp) > 0 else None
        is_low_vol = bench_sigma is not None and bench_sigma < 0.5  # 日波动<0.5%

        if not extreme.empty:
            ext = extreme[extreme["bench_code"] == code]
            if not ext.empty:
                # 先试固定阈值 2%/1%
                for t in [2, 1]:
                    up = ext[(ext["condition"] == f"涨>{t}%")]
                    dn = ext[(ext["condition"] == f"跌>{t}%")]
                    if len(up) and len(dn):
                        up_avg = up["stock_avg_ret"].values[0]
                        dn_avg = dn["stock_avg_ret"].values[0]
                        if up_avg is not None and dn_avg is not None:
                            if (up_avg < 0 and dn_avg > 0) or (up_avg > 0 and dn_avg < 0):
                                impact = max(abs(up_avg), abs(dn_avg))
                                if impact >= 0.005:
                                    direction = "反向" if up_avg < 0 else "正向"
                                    extreme_signal = f"极端日{direction}影响{impact*100:.1f}bp"
                                    break

        # 低波动指数降阈值重试（独立于 ext 是否为空）
        if extreme_signal is None and is_low_vol and bench_sigma:
            grp2 = grp.set_index("trade_date").sort_index()
            stock2 = grp2["stock_ret"]
            bench2 = grp2["bench_ret"]
            for sigma_mult in [1.5, 2.0]:  # 先试 1.5σ，再试 2σ
                threshold = sigma_mult * bench_sigma
                for direction, label in [(1, "涨"), (-1, "跌")]:
                    mask = (bench2 * direction) > threshold
                    n = mask.sum()
                    if n < 10:
                        continue
                    avg = stock2[mask].mean()
                    opp_mask = (bench2 * (-direction)) > threshold
                    opp_n = opp_mask.sum()
                    opp_avg = stock2[opp_mask].mean() if opp_n >= 10 else None
                    if opp_avg is not None and ((avg < 0 and opp_avg > 0) or (avg > 0 and opp_avg < 0)):
                        impact = max(abs(avg), abs(opp_avg))
                        if impact >= 0.005:
                            direction_str = "反向" if avg < 0 else "正向"
                            extreme_signal = f"{sigma_mult}σ极端日{direction_str}影响{impact*100:.1f}bp"
                            break
                if extreme_signal:
                    break

        if extreme_signal and verdict == "无关":
            verdict = "微弱相关"
            signals.insert(0, f"极端日信号({extreme_signal})")

        # 虹吸
        if siphon_5 is not None and siphon_5 < -R_WEAK:
            signals.append(f"弱虹吸({siphon_5:+.2f})")

        # 趋势分岔（从 regime 查）
        if not regime.empty:
            reg = regime[regime["bench_code"] == code]
            if len(reg) == 2:
                up = reg[reg["regime"] == "上升"]
                dn = reg[reg["regime"] == "下降"]
                r_up = up["pearson"].values[0] if len(up) else None
                r_dn = dn["pearson"].values[0] if len(dn) else None
                if r_up is not None and r_dn is not None and abs(r_up - r_dn) >= DELTA_NOTABLE:
                    signals.append(f"趋势分岔Δ={abs(r_up-r_dn):.2f}")

        # 领先信号
        if lag1 is not None and abs(lag1) >= R_WEAK:
            direction = "正向领先" if lag1 > 0 else "反向领先"
            signals.append(f"{direction}({lag1:+.2f})")

        # 波动日增强
        if not thresh["shock_days"].empty:
            shock = thresh["shock_days"][thresh["shock_days"]["bench_code"] == code]
            if len(shock) == 2:
                normal = shock[shock["condition"] == "正常"]
                shock_day = shock[shock["condition"] == "剧烈波动(|ret|>2σ)"]
                r_n = normal["pearson"].values[0] if len(normal) else None
                r_s = shock_day["pearson"].values[0] if len(shock_day) else None
                if r_n is not None and r_s is not None and abs(r_s - r_n) >= DELTA_NOTABLE:
                    signals.append(f"波动日增强Δ={abs(r_s-r_n):.2f}")

        # 综合判定（仅当上述规则都未命中时走 fallback）
        if verdict == "无关":
            if any("隔夜" in s for s in signals):
                verdict = "隔夜传导"
            elif any("虹吸" in s for s in signals):
                verdict = "弱虹吸"

        signal_str = " | ".join(signals) if signals else "无显著信号"
        print(f"  [{verdict:<6s}] {name:<21s} {signal_str}")

    # ═══ 汇总 ═══
    print(f"\n{'═' * 64}")
    print(f"  数据区间: {data_start} ~ {data_end}")
    print(f"  r≥0.5 ★较强  r≥0.3 *有意义  Δr≥0.15 ⚡显著差异")
    print(f"{'═' * 64}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="股票 vs 全球指数日收益率相关性分析")
    ap.add_argument("stock", nargs="?", default="HK.00700",
                    help="股票代码，默认 HK.00700")
    args = ap.parse_args()
    main(args.stock)
