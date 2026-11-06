#!/usr/bin/env python3
"""``BIZ.MARKET_REGIME`` 只读验收报告（§8.1 第 1/3/5/7 条的可复跑落点）。

不写库。输出：落库完整性、三态/两态前瞻收益、转折风险子型、状态游程与
状态机增量滞后、公认区间对照、两态收益差的 block bootstrap 区间。

⚠ 前瞻收益是**样本内、重叠窗口**，未扣交易成本；bootstrap 只修正重叠样本的
标准误，不替代样本外检验。结论仅作描述性证据，不构成可交易信号。

用法： python3 tools/validate_regime.py [--bootstrap 500]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import get_conn, read_sql  # noqa: E402

BIZ = "BIZ.MARKET_REGIME"
ANCHORS = {
    "CN": ["2015-06-12", "2018-12-28", "2021-02-18"],
    "HK": ["2020-03-19", "2021-02-18", "2022-10-31"],
}


def _jget(v, key):
    if isinstance(v, dict):
        return v.get(key)
    if isinstance(v, str):
        try:
            return json.loads(v).get(key)
        except ValueError:
            return None
    return None


def _fwd_stats(g, col):
    out = {}
    for k, x in g.groupby(col):
        vals = {}
        for h in (60, 120, 250):
            f = x["f" + str(h)]
            vals["f%d" % h] = (round(float(f.mean()), 4), round(float(f.median()), 4),
                               round(float((f > 0).mean()), 4))
        out[k] = vals
    return out


def _bootstrap(g, n_iter, rng):
    """按 120 交易日整块重采样，估计「趋势 - 震荡转折」f120 均值差的 95% 区间。"""
    v = g.dropna(subset=["f120"])
    blocks = [v.iloc[i:i + 120] for i in range(0, len(v), 120)]
    if len(blocks) < 3:
        return None
    diffs = []
    for _ in range(n_iter):
        pick = rng.integers(0, len(blocks), len(blocks))
        s = pd.concat([blocks[int(j)] for j in pick], ignore_index=True)
        a = s.loc[s["trend_state"] == "trend", "f120"]
        b = s.loc[s["trend_state"] == "range", "f120"]
        if len(a) and len(b):
            diffs.append(float(a.mean() - b.mean()))
    if not diffs:
        return None
    return (round(float(np.percentile(diffs, 2.5)), 4),
            round(float(np.percentile(diffs, 97.5)), 4))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", type=int, default=500)
    args = ap.parse_args()
    rng = np.random.default_rng(42)

    with get_conn() as conn:
        bv = read_sql(conn, """
            SELECT market, trade_date, score, state, level, since_date, dwell_days,
                   status, confidence, detail, model_version
            FROM regime.business_value WHERE biz_code=%s
            ORDER BY market, trade_date
        """, (BIZ,))
        with conn.cursor() as cur:
            cur.execute("SELECT params_hash FROM regime.business_dict WHERE biz_code=%s", (BIZ,))
            rec = cur.fetchone()
        ph = rec[0] if rec else None
        px = read_sql(conn, """
            SELECT bench_code, trade_date, last_price FROM public.daily_benchmark
            WHERE bench_code IN ('SH.000001','HK.800000') AND last_price>0
            ORDER BY bench_code, trade_date
        """)

    if bv.empty:
        print("❌ business_value 无数据，请先跑 build_business_value.py --write")
        return 1

    px["market"] = px["bench_code"].map({"SH.000001": "CN", "HK.800000": "HK"})
    bv["trend_state"] = bv["detail"].apply(lambda x: _jget(x, "trend_state"))
    bv["turn_risk"] = bv["detail"].apply(lambda x: bool(_jget(x, "turn_risk")))
    bv["raw_state"] = bv["detail"].apply(lambda x: _jget(_jget(x, "evidence"), "raw_state")
                                         if isinstance(x, dict) else None)
    d = bv.merge(px[["market", "trade_date", "last_price"]],
                 on=["market", "trade_date"], how="left")
    for h in (60, 120, 250):
        d["f" + str(h)] = d.groupby("market")["last_price"].shift(-h) / d["last_price"] - 1

    print("=== 落库完整性 ===")
    print("business_dict.params_hash =", ph)
    print("model_version 全部一致 =", bool((bv["model_version"] == ph).all()),
          "| 行数 =", len(bv), "| score 空值 =", int(bv["score"].isna().sum()))
    print("detail 必带键齐全 =", bool(bv["trend_state"].notna().all() and
                                     bv["raw_state"].notna().all()))

    for m, g in d.groupby("market"):
        g = g.reset_index(drop=True).copy()
        print(f"\n========== {m} ==========")
        print("区间", g["trade_date"].min(), "~", g["trade_date"].max(), "行数", len(g))
        print("state 占比", g["state"].value_counts(normalize=True).round(3).to_dict())
        print("confidence", g["confidence"].value_counts(normalize=True).round(3).to_dict())

        seg, cur, n = [], g.loc[0, "state"], 0
        for x in g["state"]:
            if x == cur:
                n += 1
            else:
                seg.append(n); cur, n = x, 1
        seg.append(n)
        print("切换次数", len(seg) - 1, "| 游程 中位/P95/max",
              int(np.median(seg)), int(np.percentile(seg, 95)), max(seg))

        lag = []
        cur_state, cand_since = g.loc[0, "state"], None
        for i, x in g.iterrows():
            if x["raw_state"] != cur_state:
                if cand_since is None or (i > 0 and g.loc[i - 1, "raw_state"] != x["raw_state"]):
                    cand_since = i
            if x["state"] != cur_state:
                if cand_since is not None:
                    lag.append(i - cand_since)
                cand_since = None
                cur_state = x["state"]
        if lag:
            print("状态机增量滞后 中位/P95/max", int(np.median(lag)),
                  int(np.percentile(lag, 95)), max(lag),
                  "| >30 日占比", round(float(np.mean(np.array(lag) > 30)), 3))

        print("三态前瞻收益 (均值, 中位, 胜率)")
        for k, v in _fwd_stats(g, "state").items():
            print("  ", k, v)
        print("两态（趋势 / 震荡转折）前瞻收益")
        for k, v in _fwd_stats(g, "trend_state").items():
            print("  ", k, v)
        tr = g.loc[g["turn_risk"], "f120"]
        print("turn_risk 子型 f120 均值/中位/n/占比",
              round(float(tr.mean()), 4), round(float(tr.median()), 4), len(tr),
              round(len(tr) / len(g), 3))
        ci = _bootstrap(g, args.bootstrap, rng)
        print("两态 f120 均值差 block-bootstrap 95% CI =", ci,
              "(n_iter=%d, block=120)" % args.bootstrap)

        print("公认区间附近 state（锚点当日 → +60 交易日）")
        for a in ANCHORS.get(m, []):
            w = g[g["trade_date"] >= pd.Timestamp(a).date()]
            w = w[w["trade_date"] <= pd.Timestamp(a).date() + pd.Timedelta(days=95)]
            if len(w):
                print("  ", a, w.iloc[0]["state"], "->", w.iloc[-1]["state"],
                      "| raw@anchor", w.iloc[0]["raw_state"])

    print("\n⚠ 以上为样本内重叠窗口的描述性统计，未扣成本、未做样本外检验；"
          "不得据此启用方向性 cap（§6.1 v2 下游契约）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
