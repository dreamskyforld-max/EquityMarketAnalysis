#!/usr/bin/env python3
"""L2 维度指标 `DIM.BREADTH` 的计算与落库 + §5.1 三项派生量（v2.1.9）。

上游：`tools/calibrate_breadth.py --write` 产出 `regime.dimension_dict`；
      `--backfill` 后的 `regime.indicator_value.percentile`（v2.1.8 口径）。
下游：`regime.dimension_value`（一市场一日一维一行）。

按 `doc/market_profile.md` §5.7 L2-c 产出五件套：
  score / state(5 档) / coverage / dispersion / contrib，外加 blocks / flags / confidence。
门禁：`coverage < coverage_min` 或 `dispersion_norm > dispersion_max`
      → **只输出 score + 低置信，不输出 state**（宁可说不确定，不给确定的错结论）。

派生量按 §5.1「不落表，由消费方按固定 model_version 重算」：A/D Line、McClellan
Summation Index、A/D 与指数双高点背离。它们不写库，由 `--derived` 输出。

用法：
    .venv/bin/python3 regime_dimensions.py --write            # 增量（最近 45 天）
    .venv/bin/python3 regime_dimensions.py --write --backfill # 全历史
    .venv/bin/python3 regime_dimensions.py --derived --market CN   # 打印派生量
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys

import numpy as np
import pandas as pd

from db import get_conn, bulk_upsert, read_sql

DIM = "DIM.BREADTH"
DV_TABLE = "regime.dimension_value"
DATA_START = {"CN": datetime.date(1990, 1, 1), "HK": datetime.date(2000, 1, 1)}
BENCH_CODE = {"CN": "SH.000001", "HK": "HK.800000"}
HK_BENCH_START = datetime.date(2013, 8, 20)   # daily_benchmark.HK.800000 实际起点

# 结构性背离判据（§5.7 L2-c flags 表）
FLAG_GAP = 15.0          # 快慢成员分位差
FLAG_EXTREME = 20.0      # 极端分位


def _read(sql, conn, params=None):
    """统一走 `db.read_sql`（全项目唯一读库入口，避免 pandas DBAPI2 警告刷屏）。

    保持 `pd.read_sql_query` 的调用形态，便于既有 SQL 与 mock 直接复用。
    """
    return read_sql(conn, sql, params=params)


# ── §5.1 派生量（纯函数，不落表）──────────────────────────────────────────────
def ad_line(adv_net: pd.Series) -> pd.Series:
    """A/D Line = 归一化净广度增量的 cumsum（只存增量，累计由消费方算）。"""
    return pd.Series(adv_net, dtype=float).cumsum()


def summation_index(mcc: pd.Series) -> pd.Series:
    """McClellan Summation Index，`SI_0 = 0` **固定**（v2.1.7 删除「起点任选」）。"""
    return pd.Series(mcc, dtype=float).cumsum()


def ad_divergence(index_close: pd.Series, ad: pd.Series,
                  n: int = 60, gap: int = 20, sigma_win: int = 60,
                  max_streak: int = 60, detrend: bool = True) -> pd.DataFrame:
    """A/D 与指数的**双高点**背离（§5.1 v2.1.7 重做的口径）。

    顶背离 = 指数出现两个依次抬高的 N 日新高（间隔 ≥ gap 交易日），
             且 A/D 在对应两个高点处依次降低；底背离对称。
    幅度  = 两个 A/D 高点之差 / A/D 的 sigma_win 日差分标准差（**z 单位**），
            不用「距高点百分比」——累计量接近 0 时分母不稳定。
    返回逐日 [direction, amplitude_z, days]；days = 背离成立以来的连续交易日数。
    """
    close = pd.Series(index_close, dtype=float).dropna()
    a = pd.Series(ad, dtype=float).reindex(close.index)
    hi = close.rolling(n, min_periods=n).max()
    lo = close.rolling(n, min_periods=n).min()
    # ⚠ A/D Line 是 cumsum 累计量、带长期趋势：直接比较两点的**绝对水平**会被
    #   趋势支配（实测 HK 底背离恒 0、CN 顶背离曾停在 2007 年）。故默认比较
    #   「相对自身 N 日均线的标准化偏离」，即用相对位置而非绝对水位（§5.1）。
    if detrend:
        mu = a.rolling(n, min_periods=max(n // 2, 5)).mean()
        sd = a.rolling(n, min_periods=max(n // 2, 5)).std()
        a_cmp = (a - mu) / sd.replace(0, np.nan)
    else:
        a_cmp = a
    sigma = a_cmp.rolling(sigma_win, min_periods=min(20, sigma_win)).std()

    last_hi = None      # (pos, close, ad_relative)
    last_lo = None
    direction, amplitude, streak = "无", np.nan, 0
    rows = []
    for i, d in enumerate(close.index):
        c, av, sig = close.iloc[i], a_cmp.iloc[i], sigma.iloc[i]
        # ⚠ 高点对必须是**相近时期**的局部结构（文档的 N 日窗口语义）。超过 2N 日
        #   未刷新就重置，否则一次历史大顶会把之后的比较永久锁死：实测 CN 在
        #   2007-03-19 后再无顶背离、底背离一次都不触发。取 2N 是为了给
        #   「间隔 >= gap」留出余量，同时仍限定在半年量级内。
        if last_hi is not None and i - last_hi[0] > 2 * n:
            last_hi = None
        if last_lo is not None and i - last_lo[0] > 2 * n:
            last_lo = None
        hit = None
        # ⚠ 只有「间隔 >= gap 且**依次抬高**」才构成新的高点对并刷新基准。
        #   间隔不足或高度未抬高的「伪新高」必须忽略——否则同一波上涨里的连续
        #   新高会不断刷新时间戳，把真正的高点对挤出 gap 窗口（实测踩过）。
        if not pd.isna(hi.iloc[i]) and c >= hi.iloc[i]:
            if last_hi is None:
                last_hi = (i, c, av)
            elif i - last_hi[0] >= gap and c > last_hi[1]:
                if not pd.isna(av) and not pd.isna(last_hi[2]) and av < last_hi[2]:
                    z = ((av - last_hi[2]) / sig
                         if not pd.isna(sig) and sig > 0 else np.nan)
                    hit = ("顶背离", z)
                last_hi = (i, c, av)
        if not pd.isna(lo.iloc[i]) and c <= lo.iloc[i]:
            if last_lo is None:
                last_lo = (i, c, av)
            elif i - last_lo[0] >= gap and c < last_lo[1]:
                if not pd.isna(av) and not pd.isna(last_lo[2]) and av > last_lo[2]:
                    z = ((av - last_lo[2]) / sig
                         if not pd.isna(sig) and sig > 0 else np.nan)
                    hit = ("底背离", z)
                last_lo = (i, c, av)
        if hit:
            direction, amplitude, streak = hit[0], hit[1], 1
        elif direction != "无":
            streak += 1
            if streak > max_streak:
                direction, amplitude, streak = "无", np.nan, 0
        else:
            streak = 0
        rows.append((direction, amplitude, streak if direction != "无" else 0))
    return pd.DataFrame(rows, index=close.index,
                        columns=["direction", "amplitude_z", "days"])


def derived_frame(conn, market: str, start: datetime.date,
                  end: datetime.date) -> pd.DataFrame:
    """读库组装三项派生量（不落表）。HK 恒指仅 2013-08-20 起 → 之前不输出背离。"""
    codes = ["BREADTH.ADV_MINUS_DEC", "BREADTH.MCCLELLAN"]
    raw = _read(
        "SELECT trade_date, indicator_code, raw_value FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date BETWEEN %s AND %s "
        "  AND raw_value IS NOT NULL ORDER BY trade_date",
        conn, params=[market, codes, start, end])
    wide = raw.pivot_table(index="trade_date", columns="indicator_code",
                           values="raw_value", aggfunc="last").sort_index()
    if wide.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=wide.index)
    out["ad_line"] = ad_line(wide.get("BREADTH.ADV_MINUS_DEC", pd.Series(dtype=float)))
    out["summation_index"] = summation_index(
        wide.get("BREADTH.MCCLELLAN", pd.Series(dtype=float)))
    bench = _read(
        "SELECT trade_date, last_price FROM daily_benchmark "
        "WHERE bench_code=%s AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        conn, params=[BENCH_CODE[market], start, end])
    close = pd.Series(bench["last_price"].to_numpy(dtype=float),
                      index=pd.to_datetime(bench["trade_date"]).dt.date)
    close = close[close.index >= (HK_BENCH_START if market == "HK" else start)]
    dv = ad_divergence(close, out["ad_line"].reindex(close.index))
    out = out.join(dv)
    out["benchmark_mismatch"] = True    # 上证/恒指均为窄基，与全市场宽度池不同构
    out["sample_start"] = str(close.index.min()) if len(close) else str(start)
    # dv 只覆盖指数可得区间；join 后缺失日（含 HK 2013-08 前、指数晚一日）一律「无背离」
    out["direction"] = out["direction"].fillna("无")
    out["days"] = pd.to_numeric(out["days"], errors="coerce").fillna(0).astype(int)
    return out


# ── L2 维度指标 ───────────────────────────────────────────────────────────────
def _json(v):
    return json.loads(v) if isinstance(v, str) else v


def load_dimension(conn):
    df = _read(
        "SELECT members, agg_method, state_bands, state_labels, params, params_hash "
        "FROM regime.dimension_dict WHERE dimension_code=%s AND is_active=TRUE",
        conn, params=[DIM])
    if df.empty:
        raise RuntimeError(
            "regime.dimension_dict 无 DIM.BREADTH；请先跑 "
            "tools/calibrate_breadth.py --write（族标定 + 切点标定）")
    r = df.iloc[0]
    dim = {k: _json(r[k]) for k in ("members", "state_bands", "state_labels", "params")}
    dim["agg_method"] = r["agg_method"]
    return dim, r["params_hash"]


def _load_scopes(conn, codes):
    df = _read(
        "SELECT indicator_code, market_scope FROM regime.indicator_dict "
        "WHERE indicator_code = ANY(%s)", conn, params=[codes])
    return dict(zip(df["indicator_code"], df["market_scope"]))


def _load_pct(conn, market, codes, start, end):
    """as-of 分位（上游已落库即终值）。带实体过滤 + 时间下界（资源规范 R7）。"""
    df = _read(
        "SELECT trade_date, indicator_code, percentile FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date BETWEEN %s AND %s "
        "  AND percentile IS NOT NULL ORDER BY trade_date",
        conn, params=[market, codes, start, end])
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="trade_date", columns="indicator_code",
                          values="percentile", aggfunc="last").sort_index()


def build_market(conn, dim: dict, params_hash: str, market: str,
                 start: datetime.date, end: datetime.date, write: bool) -> dict:
    members = dim["members"]
    bands = list(dim["state_bands"][market])
    labels = list(dim["state_labels"])
    p = dim["params"]
    coverage_min = float(p["coverage_min"])
    dispersion_max = float(p["dispersion_max"])
    weight_cap = float(p["weight_cap"])

    all_codes = [m["code"] for m in members]
    scopes = _load_scopes(conn, all_codes)
    ok_scope = lambda c: scopes.get(c) in (market, "CN+HK", "GLOBAL")
    eligible = [c for c in all_codes if ok_scope(c)]          # coverage 分母（§5.7 L2-c）
    pct = _load_pct(conn, market, eligible, start, end)
    if pct.empty:
        return {"rows": 0, "dates": 0}
    aligned = pct.copy()
    for c in eligible:
        if c not in aligned.columns:
            aligned[c] = np.nan
    sign = {m["code"]: int(m.get("sign", 1)) for m in members}
    for c in eligible:
        if sign.get(c, 1) == -1:
            aligned[c] = 100.0 - aligned[c]                    # 极性统一（high_risk 翻向）

    # 族 → 每族固定 1 个代表 → 族间等权；flags_only 成员不进 score
    blocks = {}
    for m in members:
        if m.get("flags_only") or not ok_scope(m["code"]):
            continue
        blocks.setdefault(m["block"], []).append(m)
    reps = []
    for ms in blocks.values():
        r = next((x["code"] for x in ms if x.get("representative")), None)
        if r and r in aligned.columns:
            reps.append(r)
    n_eff = len(reps)
    w = min(1.0 / n_eff, weight_cap) if n_eff else 0.0       # 单成员权重上限 25%
    wsum = w * n_eff
    weights = pd.Series({r: (w / wsum if wsum else 0.0) for r in reps})
    rep_frame = aligned[reps] if reps else pd.DataFrame(index=aligned.index)
    num = rep_frame.mul(weights, axis=1).sum(axis=1, min_count=1)
    den = rep_frame.notna().mul(weights, axis=1).sum(axis=1)
    score = (num / den.replace(0, np.nan)).clip(0, 100) if reps else pd.Series(
        np.nan, index=aligned.index)

    valid = aligned[eligible].notna().sum(axis=1)
    coverage = valid / max(len(eligible), 1)
    dispersion = aligned[eligible].std(axis=1, ddof=1)
    dispersion_norm = dispersion * np.sqrt(valid / 10.0)

    lv = np.digitize(score.fillna(-1).to_numpy(), bands)      # 等值归较高档（§8.1.1）
    state = pd.Series([labels[int(i)] for i in lv], index=score.index)
    ok = (coverage >= coverage_min) & (dispersion_norm <= dispersion_max) & score.notna()
    state = state.where(ok, None)
    confidence = pd.Series("低", index=score.index)
    high = ok & (coverage >= 0.8) & (dispersion_norm <= 20.0)
    confidence[high] = "高"
    confidence[ok & ~high] = "中"

    def _contrib(d):
        dev = (aligned.loc[d, eligible] - score.loc[d]).dropna()
        def _lst(s):
            return [{"code": k, "delta": round(float(v), 2)} for k, v in s.items()]
        return {"pos": _lst(dev.nlargest(3)), "neg": _lst(dev.nsmallest(3))}

    def _flags(d):
        f = []
        def g(c):
            v = aligned.loc[d, c] if c in aligned.columns else np.nan
            return None if pd.isna(v) else float(v)
        a, b = g("BREADTH.NEW_HIGH_LOW_20"), g("BREADTH.NEW_HIGH_LOW_60")
        if a is not None and b is not None and a < b - FLAG_GAP:
            f.append("短窗动能衰竭（中期趋势未破）")
        a, b = g("BREADTH.PCT_ABOVE_MA60"), g("BREADTH.PCT_ABOVE_MA200")
        if a is not None and b is not None and a > b + FLAG_GAP:
            f.append("短期反弹但中期未修复")
        if (a := g("BREADTH.EQ_VS_CAP_20D")) is not None and a < FLAG_EXTREME:
            f.append("权重股掩盖多数股票下跌")
        if (a := g("BREADTH.AMT_CONC_TOP10")) is not None and a < FLAG_EXTREME:
            f.append("成交额集中度偏高")
        return f

    def _block_scores(d):
        out = {}
        for name, ms in blocks.items():
            cs = [m["code"] for m in ms if ok_scope(m["code"])]
            rep = next((m["code"] for m in ms if m.get("representative")), None)
            vals = [aligned.loc[d, c] for c in cs if c in aligned.columns]
            nv = sum(1 for v in vals if not pd.isna(v))
            sv = aligned.loc[d, rep] if rep in aligned.columns else np.nan
            out[name] = {
                "score": None if pd.isna(sv) else round(float(sv), 2),
                "coverage": round(nv / len(cs), 4) if cs else None,
                "representative": rep,
            }
        return out

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = []
    for d in score.index:
        if pd.isna(score.loc[d]) and valid.loc[d] == 0:
            continue
        rows.append({
            "dimension_code": DIM, "market": market, "trade_date": d,
            "score": None if pd.isna(score.loc[d]) else round(float(score.loc[d]), 2),
            # Series.where(..., None) 在 object dtype 下仍会是 NaN；显式转 None，
            # 保证「不出档」语义在入库前就是明确的 NULL，不依赖写入层兜底。
            "state": None if pd.isna(state.loc[d]) else str(state.loc[d]),
            "coverage": round(float(coverage.loc[d]), 4),
            "dispersion": None if pd.isna(dispersion.loc[d]) else round(float(dispersion.loc[d]), 2),
            "blocks": _block_scores(d),
            "contrib": _contrib(d),
            "flags": _flags(d),
            "confidence": confidence.loc[d],
            "is_final": True,                 # 纯派生量，收盘即终值
            "model_version": params_hash,
            "updated_at": now,
        })
    if write and rows:
        # skip_null_updates=False：门禁不通过时 state=NULL 必须能覆盖旧档（否则旧结论残留）
        bulk_upsert(conn, DV_TABLE, rows,
                    conflict_cols=["dimension_code", "market", "trade_date"],
                    skip_null_updates=False, only_if_changed=True)
    return {"rows": len(rows), "dates": len(score.index),
            "state_rows": int(ok.sum()), "reps": reps}


def build(conn, start=None, end=None, backfill=False, write=True, markets=("CN", "HK")):
    """供 `market_state_daily.run()` 接线调用。"""
    dim, ph = load_dimension(conn)
    end = end or datetime.date.today()
    stats = {}
    for mk in markets:
        s0 = start or (DATA_START[mk] if backfill else end - datetime.timedelta(days=45))
        s0 = max(s0, DATA_START[mk])
        stats[mk] = build_market(conn, dim, ph, mk, s0, end, write)
    return stats


def run(codes=None, ctx=None, market=None, start=None, end=None,
        backfill=False, write=True) -> dict:
    """兼容 `market_scheduler` 的 run_module 调用约定。"""
    markets = [market] if market else ["CN", "HK"]
    with get_conn() as conn:
        return build(conn, start=start, end=end, backfill=backfill,
                     write=write, markets=markets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写 regime.dimension_value")
    ap.add_argument("--backfill", action="store_true", help="全历史")
    ap.add_argument("--derived", action="store_true", help="打印派生量（不落表）")
    ap.add_argument("--market", choices=["CN", "HK"])
    ap.add_argument("--start", type=datetime.date.fromisoformat)
    ap.add_argument("--end", type=datetime.date.fromisoformat)
    args = ap.parse_args()
    markets = [args.market] if args.market else ["CN", "HK"]
    end = args.end or datetime.date.today()
    with get_conn() as conn:
        if args.derived:
            for mk in markets:
                s0 = args.start or DATA_START[mk]
                df = derived_frame(conn, mk, s0, end)
                print(f"===== {mk} 派生量（{len(df)} 日）=====")
                print(df.tail(10).to_string())
            return
        st = build(conn, start=args.start, end=end, backfill=args.backfill,
                   write=args.write, markets=markets)
        for mk, s in st.items():
            print(f"{mk}: {s['dates']} 日 / {s['rows']} 行 / 出档 {s['state_rows']} 日 / "
                  f"代表 {s['reps']}")


if __name__ == "__main__":
    main()
