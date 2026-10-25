#!/usr/bin/env python3
"""§4.2.1 第九节「验证要求」的首次落地：**确认度是否真的有前瞻预测力**？

文档要求（§4.2.1 九）：
    · 分组预测力：按确认度高低分组，统计各组后续 20/60 日股票收益 —— 若高低组区分不出，
      **该证据组应被剔除而非继续计票**。
    · 时序性质：检验该组信号是否**领先**于股票风险变化。
    · 系数有效性：「按系数执行」vs「始终按中枢」的收益/回撤差异。

本脚本做前两项（第三项依赖仓位模拟，另做）。**只读，不写库。**

实现要点（与规格逐条对应）：
    · 每项证据立场 = sign_i × 标准化(变化量)，标准化 = (历史分位 − 50)/50 ∈ [−1,+1]
      —— 分位直接取指标层已存的 `percentile`（expanding 分位，**无前视**），不重算。
    · 组立场 = mean(组内各项)；确认度 = 50 + 50 × mean(各组立场) × sign(§4.1 档位)
      —— 即按档位取镜像（文档「三、确认度」）。
    · §4.1 档位 = 长周期 ERP 分位查表（≥80 偏多 / 60~80 中性偏多 / 40~60 中性 /
      20~40 中性偏空 / <20 偏空）。中性档不产生方向，故**验证时剔除**。
    · 前瞻收益 = 上证指数未来 20/60 个交易日的收益，再乘 sign(档位) → **带符号收益**。
      高确认度应当对应**更高的带符号收益**；若不然，说明确认度无预测力。

用法:
    python3 validate_421_confirmation.py [--fwd 20 60] [--since 2006-01-01]
"""
import argparse
import sys

import numpy as np
import pandas as pd

from db import get_conn

# ── 证据分组（与 §4.2.1 二、逐条对应）─────────────────────────────────────
# (指标编码, sign, 说明)；sign 为 +1 表示「该指标上行 = 支持超配股票」
GROUPS = {
    "债券": [("MACRO.BOND_10Y_CHG20", -1, "收益率上行=反对"),
             ("__TERM_SPREAD_CHG20__", +1, "曲线陡峭化=支持")],
    "信用": [("RISK.CREDIT_SPREAD_CHG20", -1, "利差走阔=反对")],
    "商品": [("MACRO.COPPER_MOM20", +1, "铜走强=支持"),
             ("MACRO.GOLD_MOM20", -1, "金走强=避险=反对")],
    "汇率": [("MACRO.USDCNY_CHG20", -1, "人民币贬值=反对"),
             ("MACRO.DXY_CHG20", -1, "美元走强=抽水=反对")],
    "波动率": [("RISK.VIX_CHG20", -1, "波动率上行=反对")],
}


def _load_pct(conn) -> pd.DataFrame:
    """取全部所需指标在 CN 侧的 (date, code) → percentile 宽表。"""
    codes = sorted({c for g in GROUPS.values() for c, _, _ in g
                    if not c.startswith("__")})
    df = pd.read_sql_query(
        """SELECT trade_date, indicator_code, percentile
           FROM regime.indicator_value
           WHERE market='CN' AND indicator_code = ANY(%s)""", conn, params=[codes])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    wide = df.pivot_table(index="trade_date", columns="indicator_code",
                          values="percentile", aggfunc="last")
    return wide.sort_index()


def _term_spread_chg20(conn) -> pd.Series:
    """期限结构的 20 日变化及其 expanding 分位（库里无现成指标，现场派生）。

    ⚠ 用 expanding 分位（只用当日及以前的数据）→ 与指标层口径一致、无前视。
    """
    df = pd.read_sql_query(
        """SELECT trade_date, raw_value FROM regime.indicator_value
           WHERE market='CN' AND indicator_code='RISK.TERM_SPREAD'
           ORDER BY trade_date""", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    s = df.set_index("trade_date")["raw_value"].astype(float).sort_index()
    chg = s.diff(20).dropna()
    v = chg.to_numpy()
    # expanding 分位（含自身，最坏情形为 100 → 标准化 +1）
    out = np.empty(len(v))
    for i in range(len(v)):
        out[i] = (v[:i + 1] < v[i]).mean() * 100
    return pd.Series(out, index=chg.index)


def _tier(erp_pct: pd.Series) -> pd.Series:
    """§4.1 档位 → sign：+1 偏多/中性偏多，−1 偏空/中性偏空，0 中性。"""
    return pd.Series(np.select(
        [erp_pct >= 80, erp_pct >= 60, erp_pct >= 40, erp_pct >= 20],
        [1, 1, 0, -1], default=-1), index=erp_pct.index)


def _expanding_pct(s: pd.Series) -> pd.Series:
    """expanding 分位（0~100），用于把任意序列转成「历史排位」。"""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        if not np.isnan(v[i]):
            col = v[:i + 1]
            out[i] = (col[~np.isnan(col)] < v[i]).mean() * 100
    return pd.Series(out, index=s.index)


def build_confirmation(conn) -> pd.DataFrame:
    """重建逐日「组立场 / 确认度 / 档位」。"""
    pct = _load_pct(conn)

    # 期限结构：现场派生 20 日变化 → 再取其 expanding 分位
    ts_chg = _term_spread_chg20(conn)
    pct["__TERM_SPREAD_PCT__"] = _expanding_pct(ts_chg)

    # §4.1 档位：长周期 ERP 分位（月频，向后填充到日频）
    erp = pd.read_sql_query(
        """SELECT trade_date, percentile FROM regime.indicator_value
           WHERE market='CN' AND indicator_code='VAL.ERP_LG' ORDER BY trade_date""", conn)
    erp["trade_date"] = pd.to_datetime(erp["trade_date"])
    erp_pct = erp.set_index("trade_date")["percentile"].astype(float).sort_index()

    idx = pct.index
    tier = _tier(erp_pct.reindex(erp_pct.index.union(idx)).ffill().reindex(idx))

    # 组立场
    rows = {}
    for gname, members in GROUPS.items():
        cols = []
        for code, sign, _ in members:
            key = "__TERM_SPREAD_PCT__" if code == "__TERM_SPREAD_CHG20__" else code
            if key not in pct.columns:
                continue
            pos = sign * (pct[key] - 50) / 50          # 标准化偏离 ∈ [−1, +1]
            cols.append(pos)
        if cols:
            rows[gname] = pd.concat(cols, axis=1).mean(axis=1, skipna=True)
    gdf = pd.DataFrame(rows, index=idx)
    gdf["n_groups"] = gdf.notna().sum(axis=1)

    m = gdf[list(GROUPS)].mean(axis=1, skipna=True)
    # 确认度 = 50 + 50 × mean(组立场) × sign(档位)   ← 文档「三、确认度」的镜像
    gdf["confirm"] = 50 + 50 * m * tier
    gdf["tier"] = tier
    # 覆盖率门禁：可投票组 <3 → 不可用（文档「三」）
    gdf.loc[gdf["n_groups"] < 3, "confirm"] = np.nan
    return gdf.dropna(subset=["confirm"])


def forward_returns(conn, horizons) -> pd.DataFrame:
    """上证指数未来 h 个交易日收益（%）。"""
    df = pd.read_sql_query(
        """SELECT trade_date, last_price FROM daily_benchmark
           WHERE bench_code='SH.000001' ORDER BY trade_date""", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    s = df.set_index("trade_date")["last_price"].astype(float).sort_index()
    out = pd.DataFrame(index=s.index)
    for h in horizons:
        out[f"fwd{h}"] = (s.shift(-h) / s - 1) * 100
    return out


def _bucket(c: pd.Series) -> pd.Series:
    return pd.cut(c, bins=[-np.inf, 50, 65, np.inf],
                  labels=["<50 未确认", "50~65 部分", "≥65 确认"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fwd", type=int, nargs="+", default=[20, 60])
    ap.add_argument("--since", default="2006-01-01")
    args = ap.parse_args()

    with get_conn() as conn:
        conf = build_confirmation(conn)
        fwd = forward_returns(conn, args.fwd)

    df = conf.join(fwd, how="inner").dropna(subset=["fwd20"])
    df = df[df.index >= pd.Timestamp(args.since)]
    # 带符号收益：sign(档位) × 前瞻收益 → 高确认度应为正
    for h in args.fwd:
        df[f"sfwd{h}"] = df["tier"] * df[f"fwd{h}"]

    n_dir = int((df["tier"] != 0).sum())
    print("=" * 78)
    print("§4.2.1 第九节验证：确认度是否具备前瞻预测力")
    print("=" * 78)
    print(f"样本      {df.index.min().date()} ~ {df.index.max().date()}   共 {len(df)} 个交易日")
    print(f"其中有方向（档位≠中性）：{n_dir} 日（{(df['tier'] != 0).mean():.1%}）")
    print(f"档位分布  偏多/中性偏多 {(df['tier'] == 1).sum()} · 中性 {(df['tier'] == 0).sum()}"
          f" · 偏空/中性偏空 {(df['tier'] == -1).sum()}")
    print()

    d = df[df["tier"] != 0].copy()
    d["bk"] = _bucket(d["confirm"])
    print("─" * 78)
    print("【检验一】分组预测力：按确认度分组，看**带符号**前瞻收益（%）")
    print("  · 带符号 = sign(档位) × 前瞻收益 → 确认度高则应为正、且逐档递增")
    print("─" * 78)
    g = d.groupby("bk", observed=True)
    tab = pd.DataFrame({
        "日数": g.size(),
        **{f"fwd{h}均值": g[f"sfwd{h}"].mean() for h in args.fwd},
        **{f"fwd{h}胜率": g[f"sfwd{h}"].apply(lambda x: (x > 0).mean() * 100) for h in args.fwd},
    })
    print(tab.round(2).to_string())
    print()

    print("─" * 78)
    print("【检验二】单调性 / 区分度：高分组 − 低分组 的带符号收益差（%）")
    print("─" * 78)
    lo, hi = "&lt;50 未确认".replace("&lt;", "<"), "≥65 确认"
    for h in args.fwd:
        a = d.loc[d["bk"] == lo, f"sfwd{h}"].mean()
        b = d.loc[d["bk"] == hi, f"sfwd{h}"].mean()
        ic = d["confirm"].corr(d[f"sfwd{h}"], method="spearman")
        print(f"  fwd{h:<3d}  未确认 {a:+6.2f}%   确认 {b:+6.2f}%   差 {b - a:+6.2f}pp"
              f"   Spearman IC {ic:+.4f}")
    print()

    print("─" * 78)
    print("【检验三】各组单独的预测力（组立场 vs 带符号前瞻收益，Spearman IC）")
    print("  · 文档：「若高低组区分不出，该证据组应被剔除而非继续计票」")
    print("─" * 78)
    for gname in GROUPS:
        if gname not in d.columns:
            continue
        ic20 = d[gname].corr(d["sfwd20"], method="spearman")
        ic60 = d[gname].corr(d["sfwd60"], method="spearman") if 60 in args.fwd else np.nan
        flag = "" if abs(ic20) >= 0.02 else "   ← 预测力接近 0"
        print(f"  {gname:5s}  IC20 {ic20:+.4f}  IC60 {ic60:+.4f}{flag}")
    print()

    print("─" * 78)
    print("【检验四】时序性质：各组的 20 日变化与**同期**上证收益的相关（>0 即同步）")
    print("  · 同步性高 → 该组只能作状态描述，不能作独立领先确认（文档「九」要求标注）")
    print("─" * 78)
    with get_conn() as conn:
        sh = pd.read_sql_query(
            """SELECT trade_date, last_price FROM daily_benchmark
               WHERE bench_code='SH.000001' ORDER BY trade_date""", conn)
    sh["trade_date"] = pd.to_datetime(sh["trade_date"])
    sh_s = sh.set_index("trade_date")["last_price"].astype(float).sort_index()
    ret20 = (sh_s / sh_s.shift(20) - 1) * 100
    for gname in GROUPS:
        if gname not in d.columns:
            continue
        j = pd.concat([d[gname], ret20.rename("r20")], axis=1).dropna()
        if len(j) > 50:
            print(f"  {gname:5s}  corr(组立场, 同期20日收益) = {j[gname].corr(j['r20']):+.4f}"
                  f"   （n={len(j)}）")
    print()
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
