#!/usr/bin/env python3
"""`BIZ.MARKET_DIVERGENCE` 落库验收（只读）：完整性 / 独立复算 / 分布 / PIT 敏感性。

判定项对应 `doc/market_state_data_architecture.md` §6.3 v1.33 上线门禁：
  ① 字典与读数口径一致（model_version == params_hash）；
  ② score 为 0..n_valid 整数计数、level 1..4、state 四选一、detail 七键齐全；
  ③ 独立复算（不调用生产者计算函数）逐值一致，且 strength / hit_ratio / 档位自洽；
  ④ 四档时间占比（无长期 0%）、各对命中率（无长期 0% / 100%）、主导对分布；
  ⑤ 「仅用截至 t 样本标切点」的 PIT 敏感性一致率（参数层前视披露）；
  ⑥ 发布日历 / 低置信 / 缺对披露；异常项逐条列出，任一 FAIL → 退出码 1。

用法：
    .venv/bin/python3 tools/validate_divergence.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from db import get_conn, read_sql  # noqa: E402
import build_business_value as bb  # noqa: E402

BIZ = bb.DIVERGENCE_BIZ_CODE
PIT_ENDS = (datetime.date(2022, 12, 31), datetime.date(2023, 12, 31),
            datetime.date(2024, 12, 31))

FAILS: list[str] = []


def j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def _band(v, cut):
    """独立实现的档位映射（不调用 bb.divergence_band）。"""
    if v is None or pd.isna(v):
        return None
    v = float(v)
    return 2 if v >= cut["p70"] else (0 if v <= cut["p30"] else 1)


def _eval(bands, eq, reg, fl, params):
    """独立实现的六对判定；返回 (pairs, missing, distinct_dims)。"""
    ext = params["eq_vs_cap_extreme"]
    texts = params["structural_flag_texts"]
    pairs, missing = [], []
    for rule in params["pair_rules"]:
        pid, kind = rule["id"], rule["kind"]
        if kind == "amplitude":
            a, b = bands.get(rule["left"]), bands.get(rule["right"])
            if a is None or b is None:
                missing.append(pid)
                continue
            pairs.append((pid, abs(a - b)))
            continue
        crit = rule["criterion"]
        if crit == "eq_extreme_and_breadth_low":
            if bands.get("DIM.BREADTH") is None or eq is None:
                missing.append(pid)
                continue
            hit = (eq <= ext["low"] or eq >= ext["high"]) and bands["DIM.BREADTH"] == 0
            pairs.append((pid, 2 if hit else 0))
        elif crit == "stress_high_and_regime_up":
            if bands.get("DIM.STRESS") is None or reg is None:
                missing.append(pid)
                continue
            pairs.append((pid, 2 if (bands["DIM.STRESS"] == 2 and reg == "上行") else 0))
        elif crit == "structural_flag":
            if bands.get("DIM.BREADTH") is None or fl is None:
                missing.append(pid)
                continue
            hit = bool(fl.get("short_fade") or fl.get("mid_unrepaired"))
            pairs.append((pid, 2 if hit else 0))
        else:
            raise ValueError(crit)
    # 只按**有效对**累计涉及维度（与生产者 min_distinct_dims 口径一致）
    valid_ids = {p[0] for p in pairs}
    dims = set()
    for rule in params["pair_rules"]:
        if rule["id"] in valid_ids:
            dims.update(rule["dims"])
    return pairs, missing, dims


def _frame(conn, market, params, cuts=None):
    """独立复算用宽表；cuts 可替换为 PIT 版切点。"""
    cuts = cuts or params["band_cuts"][market]
    dim = read_sql(conn, """
        SELECT trade_date, dimension_code, score FROM regime.dimension_value
        WHERE market=%s AND dimension_code=ANY(%s) AND score IS NOT NULL
    """, (market, list(bb.DIVERGENCE_DIMS)))
    piv = dim.pivot_table(index="trade_date", columns="dimension_code",
                          values="score", aggfunc="first")
    flags = read_sql(conn, """
        SELECT trade_date, flags FROM regime.dimension_value
        WHERE market=%s AND dimension_code=%s
    """, (market, "DIM.BREADTH"))
    texts = params["structural_flag_texts"]
    fmap = {}
    for rec in flags.to_dict("records"):
        arr = (j(rec["flags"]) or {}).get("structural") or []
        if isinstance(arr, str):
            arr = json.loads(arr)
        s = set(arr if isinstance(arr, list) else [])
        fmap[rec["trade_date"]] = {
            "short_fade": texts["short_fade"] in s,
            "mid_unrepaired": texts["mid_unrepaired"] in s,
        }
    eq = dict(read_sql(conn, """
        SELECT trade_date, percentile FROM regime.indicator_value
        WHERE market=%s AND indicator_code=%s AND percentile IS NOT NULL
    """, (market, bb.DIVERGENCE_FLAG_ATOM))[["trade_date", "percentile"]].to_numpy())
    reg = dict(read_sql(conn, """
        SELECT trade_date, state FROM regime.business_value
        WHERE market=%s AND biz_code=%s AND state IS NOT NULL
    """, (market, bb.DIVERGENCE_DIRECTION_BIZ))[["trade_date", "state"]].to_numpy())
    return piv, fmap, eq, reg, cuts


def _recompute(conn, market, params, cuts=None):
    """独立复算 → {trade_date: {state, level, score, strength, pairs, missing}}。"""
    import bisect
    piv, fmap, eq, reg, cuts = _frame(conn, market, params, cuts)
    bands_def = params["strength_bands"]
    labels = params["state_labels"]
    out = {}
    for dt, row in piv.iterrows():
        bands = {}
        for d in bb.DIVERGENCE_DIMS:
            v = row.get(d)
            bands[d] = _band(v, cuts[d])
        e = eq.get(dt)
        e = None if e is None or pd.isna(e) else float(e)
        r = reg.get(dt)
        r = None if r is None or (isinstance(r, float) and pd.isna(r)) else r
        f = fmap.get(dt)
        pairs, missing, dims = _eval(bands, e, r, f, params)
        n = len(pairs)
        if n < params["min_pairs"] or len(dims) < params["min_distinct_dims"]:
            continue
        strength = sum(d for _, d in pairs) / (2 * n)
        cnt = sum(1 for _, d in pairs if d >= 1)
        idx = bisect.bisect_right(bands_def, strength)
        out[dt] = {"state": labels[idx], "level": idx + 1, "score": cnt,
                   "strength": round(strength, 4), "pairs": dict(pairs),
                   "missing": missing, "n_valid": n}
    return out


def main() -> int:
    with get_conn() as conn:
        params = bb.load_business_dict(conn, BIZ)
        p = params["params"]
        ph = params["params_hash"]
        rows = read_sql(conn, """
            SELECT market, trade_date, score, state, level, confidence, detail,
                   model_version, updated_at
            FROM regime.business_value WHERE biz_code=%s ORDER BY market, trade_date
        """, (BIZ,))
        print(f"=== {BIZ} 落库验收｜params_hash={ph}｜读数 {len(rows)} 行 ===\n")

        print("【1】口径与列级校验")
        check(not rows.empty, "读数非空")
        check(bool((rows["model_version"] == ph).all()),
              "model_version 全部等于字典 params_hash",
              str(rows["model_version"].drop_duplicates().tolist()))
        labels = list(p["state_labels"])
        check(bool(rows["state"].isin(labels).all()), "state 四选一",
              str(rows["state"].drop_duplicates().tolist()))
        check(bool(rows["level"].between(1, 4).all()), "level 1..4",
              str(sorted(rows["level"].drop_duplicates().tolist())))
        sc = pd.to_numeric(rows["score"])
        check(bool(sc.between(0, 6).all()) and bool((sc % 1 == 0).all()),
              "score 为 0..6 整数计数", f"min={sc.min()} max={sc.max()}")
        check(not rows[["market", "trade_date"]].duplicated().any(), "主键无重复")

        keys = set(bb.DIVERGENCE_OUTPUT_SCHEMA["detail_keys"])
        bad_keys, bad_math, bad_pairs = [], [], []
        for rec in rows.to_dict("records"):
            d = j(rec["detail"])
            if set(d) != keys:
                bad_keys.append(str(rec["trade_date"]))
                continue
            prs = d.get("pairs") or []
            n = d.get("n_valid_pairs")
            deltas = [x["delta"] for x in prs]
            if (n != len(prs) or not (4 <= n <= 6)
                    or any(x not in (0, 1, 2) for x in deltas)
                    or len(d.get("missing_pairs") or []) + len(prs) != len(p["pair_rules"])):
                bad_pairs.append(str(rec["trade_date"]))
            if (abs(d["strength"] - sum(deltas) / (2 * n)) > 1e-4
                    or abs(d["hit_ratio"] - rec["score"] / n) > 1e-4
                    or rec["score"] != sum(1 for x in prs if x["hit"])):
                bad_math.append(str(rec["trade_date"]))
        check(not bad_keys, "detail 七键齐全", f"异常 {len(bad_keys)} 行")
        check(not bad_pairs, "pairs / missing_pairs / delta 自洽", f"异常 {len(bad_pairs)} 行")
        check(not bad_math, "strength / hit_ratio / score 自洽", f"异常 {len(bad_math)} 行")

        print("\n【2】独立复算逐值一致")
        stored = {(r["market"], r["trade_date"]): r for r in rows.to_dict("records")}
        for market in bb.BUSINESS_MARKETS:
            ref = _recompute(conn, market, p)
            got = {k[1]: v for k, v in stored.items() if k[0] == market}
            extra = set(got) - set(ref)
            absent = set(ref) - set(got)
            mism = []
            for dt, exp in ref.items():
                cur = got.get(dt)
                if cur is None:
                    continue
                d = j(cur["detail"])
                if (cur["state"] != exp["state"] or int(cur["level"]) != exp["level"]
                        or int(cur["score"]) != exp["score"]
                        or abs(d["strength"] - exp["strength"]) > 1e-4
                        or {x["id"]: x["delta"] for x in d["pairs"]} != exp["pairs"]):
                    mism.append(str(dt))
            ok = not (extra or absent or mism)
            check(ok, f"{market} 独立复算一致",
                  f"库内 {len(got)} 行 / 复算 {len(ref)} 行 / 多发行 {len(extra)} / "
                  f"漏发行 {len(absent)} / 逐值不符 {len(mism)}")

        print("\n【3】发布日历与分布")
        for market in bb.BUSINESS_MARKETS:
            sub = rows[rows["market"] == market]
            if sub.empty:
                check(False, f"{market} 有读数")
                continue
            share = sub["state"].value_counts(normalize=True).round(4).to_dict()
            print(f"  {market}: {len(sub)} 行 {sub.trade_date.min()} ~ {sub.trade_date.max()}，"
                  f"档位占比 {share}")
            zero = [s for s in labels if share.get(s, 0) == 0]
            check(not zero, f"{market} 四档全部可达", f"0% 档：{zero}")
            check(all(v >= 0.01 for v in share.values()),
                  f"{market} 无 <1% 的稀有档", str(share))
            det = [j(x) for x in sub["detail"]]
            nv = Counter(x["n_valid_pairs"] for x in det)
            print(f"  {market}: 有效对数分布 {dict(sorted(nv.items()))}，"
                  f"平均强度 {sum(x['strength'] for x in det)/len(det):.4f}，"
                  f"平均命中 {sub['score'].mean():.2f}，"
                  f"低置信 {(sub['confidence']=='低').sum()} 行")
            dom = Counter(x["dominant_pair"] for x in det)
            print(f"  {market}: 主导对分布 {dict(dom)}")
            for rule in p["pair_rules"]:
                pid = rule["id"]
                vals = [x for d in det for x in d["pairs"] if x["id"] == pid]
                rate = sum(1 for x in vals if x["hit"]) / len(vals) if vals else 0
                flag = " ⚠" if rate in (0.0, 1.0) else ""
                print(f"    {pid} {rule['name']}: 有效 {len(vals)} 行 / "
                      f"命中率 {rate:.2%}{flag}")
            missing_all = Counter(m for d in det for m in d["missing_pairs"])
            print(f"  {market}: 退出对次数 {dict(missing_all)}")

        print("\n【4】PIT 敏感性（冻结切点参数层前视披露）")
        for end in PIT_ENDS:
            cal = bb.divergence_calibrate(conn, calib_end=end)
            for market in bb.BUSINESS_MARKETS:
                ref = _recompute(conn, market, p, cuts=cal["band_cuts"][market])
                got = {k[1]: v for k, v in stored.items() if k[0] == market}
                common = set(ref) & set(got)
                agree = sum(1 for dt in common
                            if got[dt]["state"] == ref[dt]["state"])
                rate = agree / len(common) if common else 0
                print(f"  {end} {market}: state 一致率 {rate:.2%} "
                      f"({agree}/{len(common)})")
        print("  ⚠ 切点用截至 2025-12-31 全历史冻结，回算历史标签存在参数层前视；"
              "本指标为描述性读数，不作前瞻预测。")

        print("\n【5】异常汇总")
        if FAILS:
            print("  ❌ 未通过：" + "；".join(FAILS))
            return 1
        print("  ✅ 全部通过，无异常")
        return 0


if __name__ == "__main__":
    sys.exit(main())
