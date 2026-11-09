#!/usr/bin/env python3
"""`BIZ.MARKET_PATTERN` 落库验收（只读）：完整性 / 独立复算 / 分布 / PIT 敏感性。

判定项对应 `doc/market_state_data_architecture.md` §6.2 v1.32 上线门禁：
  ① 字典与读数口径一致（model_version == params_hash）；
  ② level / score 恒 NULL、state 八选一、detail 六键齐全；
  ③ 独立复算（不调用生产者函数）逐值一致；
  ④ 各形态时间占比（无长期 0%）、常态占比、切换频率；
  ⑤ 「仅用截至 t 样本标切点」的 PIT 敏感性一致率；
  ⑥ 低置信 / 确认率披露（描述性指标，不作决策依据）。

用法：
    .venv/bin/python3 tools/validate_pattern.py
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

BIZ = bb.PATTERN_BIZ_CODE
DETAIL_KEYS = set(bb.PATTERN_OUTPUT_SCHEMA["detail_keys"])
PIT_ENDS = (datetime.date(2022, 12, 31), datetime.date(2023, 12, 31),
            datetime.date(2024, 12, 31))


def j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def _first_match(rules, bands, regime, flags):
    for rule in rules:
        ok = True
        for c in rule["all"]:
            if "dim" in c:
                cur = bands.get(c["dim"])
                if cur is None or ("band" in c and cur != c["band"]) or \
                   ("band_ne" in c and cur == c["band_ne"]):
                    ok = False
                    break
            elif "direction" in c:
                if regime != c["direction"]:
                    ok = False
                    break
            elif "flag" in c:
                if not flags.get(c["flag"]):
                    ok = False
                    break
        if ok:
            return rule
    return None


def _frame(conn, market, params, ecdf=None):
    """独立复算用的宽表；ecdf 可替换成 PIT 版切点。"""
    ecdf = ecdf or params["ecdf"][market]
    dim = read_sql(conn, """
        SELECT trade_date, dimension_code, score FROM regime.dimension_value
        WHERE market=%s AND dimension_code=ANY(%s) AND score IS NOT NULL
    """, (market, list(bb.PATTERN_DIMS)))
    piv = (dim.pivot_table(index="trade_date", columns="dimension_code",
                           values="score", aggfunc="first")
           .rename(columns=bb.PATTERN_DIM_TO_LETTER))
    regime = dict(read_sql(conn, """
        SELECT trade_date, state FROM regime.business_value
        WHERE biz_code=%s AND market=%s AND state IS NOT NULL
    """, (bb.PATTERN_DIRECTION_BIZ, market))
        [["trade_date", "state"]].to_numpy())
    flag = dict(read_sql(conn, """
        SELECT trade_date, percentile FROM regime.indicator_value
        WHERE indicator_code=%s AND market=%s AND percentile IS NOT NULL
    """, (bb.PATTERN_FLAG_ATOM, market))
        [["trade_date", "percentile"]].to_numpy())
    ext = params["eq_vs_cap_extreme"]
    out = {}
    for dt, row in piv.iterrows():
        scores = {}
        for k in bb.PATTERN_LETTER_TO_DIM:
            v = row.get(k)
            # pivot 出的缺失格是 NaN，必须按「缺行」处理（生产者 strict 语义），
            # 否则会被当成有效值并落到「中」档，复算结果假性不一致。
            scores[k] = None if v is None or pd.isna(v) else float(v)
        if sum(1 for v in scores.values() if v is not None) < params["min_dims"]:
            continue
        r = regime.get(dt)
        if r is None:
            continue
        bands = {}
        for k in bb.PATTERN_BAND_LETTERS:
            cut = ecdf[bb.PATTERN_LETTER_TO_DIM[k]]
            v = scores[k]
            bands[k] = None if v is None else (
                "H" if v >= cut["p70"] else ("L" if v <= cut["p30"] else "M"))
        p = flag.get(dt)
        p = None if p is None else float(p)
        fx = bool(p is not None and (p <= ext["low"] or p >= ext["high"]))
        rule = _first_match(params["pattern_rules"], bands, r,
                            {"eq_vs_cap_extreme": fx})
        out[dt] = {"state": rule["state"] if rule else "常态",
                   "bands": bands,
                   "rule_id": rule["id"] if rule else None}
    return out


def _pit_ecdf(conn, market, end):
    """仅用截至 end 的样本重标 P30/P70（敏感性分析，不写库）。"""
    df = read_sql(conn, """
        SELECT dimension_code,
               percentile_cont(0.30) WITHIN GROUP (ORDER BY score) AS p30,
               percentile_cont(0.70) WITHIN GROUP (ORDER BY score) AS p70
        FROM regime.dimension_value
        WHERE market=%s AND dimension_code=ANY(%s) AND score IS NOT NULL
          AND trade_date<=%s
        GROUP BY dimension_code
    """, (market, list(bb.PATTERN_DIMS), end))
    return {r["dimension_code"]: {"p30": float(r["p30"]), "p70": float(r["p70"])}
            for _, r in df.iterrows()}


def main() -> int:
    failures = []
    with get_conn() as conn:
        d = read_sql(conn, """
            SELECT kind, is_active, params, params_hash, inputs, output_schema
            FROM regime.business_dict WHERE biz_code=%s
        """, (BIZ,))
        if d.empty:
            print(f"❌ {BIZ} 无字典行")
            return 1
        rec = d.iloc[0]
        params = j(rec["params"])
        ph = rec["params_hash"]
        print("=== ① 字典 ===")
        print(f"kind={rec['kind']}  is_active={rec['is_active']}  params_hash={ph}")
        if rec["kind"] != "rule_set" or not rec["is_active"]:
            failures.append("字典 kind / is_active 不符")
        if j(rec["output_schema"]).get("detail_keys") != bb.PATTERN_OUTPUT_SCHEMA["detail_keys"]:
            failures.append("output_schema.detail_keys 不符")

        bv = read_sql(conn, """
            SELECT market, trade_date, score, state, level, since_date, dwell_days,
                   status, confidence, detail, model_version, updated_at
            FROM regime.business_value WHERE biz_code=%s
            ORDER BY market, trade_date
        """, (BIZ,))
        print("\n=== ② 落库完整性 ===")
        print(f"总行数 {len(bv)}｜市场 {sorted(bv['market'].unique())}")
        if bv.empty:
            print("❌ 无读数")
            return 1
        checks = {
            "model_version 全部一致": bool((bv["model_version"] == ph).all()),
            "level 恒 NULL": bool(bv["level"].isna().all()),
            "score 恒 NULL": bool(bv["score"].isna().all()),
            "state 非空且八选一": bool(bv["state"].notna().all() and
                                 set(bv["state"]) <= set(bb.PATTERN_PRIORITY)),
            "detail 六键齐全": all(DETAIL_KEYS <= set(j(x)) for x in bv["detail"]),
            "since_date 非空": bool(bv["since_date"].notna().all()),
            "dwell_days 非空且 >=1": bool(bv["dwell_days"].notna().all() and
                                       (bv["dwell_days"] >= 1).all()),
            "status 合法": bool(set(bv["status"]) <= {"已确认", "待确认"}),
            "confidence 合法": bool(set(bv["confidence"]) <= {"高", "中", "低"}),
        }
        for k, v in checks.items():
            print(f"  {'✅' if v else '❌'} {k}")
            if not v:
                failures.append(k)

        print("\n=== ③ 独立复算逐值一致 ===")
        for market in bb.BUSINESS_MARKETS:
            ref = _frame(conn, market, params)
            sub = bv[bv["market"] == market]
            bad_state = bad_band = bad_rule = 0
            for _, r in sub.iterrows():
                exp = ref.get(r["trade_date"])
                if exp is None:
                    continue
                det = j(r["detail"])
                if r["state"] != exp["state"]:
                    bad_state += 1
                if det.get("evidence", {}).get("bands") != exp["bands"]:
                    bad_band += 1
                if det.get("matched_rules") != ([exp["rule_id"]] if exp["rule_id"] else []):
                    bad_rule += 1
            covered = len(set(sub["trade_date"]) & set(ref))
            print(f"  {market}: 覆盖 {covered}/{len(sub)} 日｜"
                  f"state 不符 {bad_state}｜bands 不符 {bad_band}｜rule_id 不符 {bad_rule}")
            if bad_state or bad_band or bad_rule or covered != len(sub):
                failures.append(f"{market} 独立复算不一致")

        print("\n=== ④ 形态分布与切换 ===")
        for market in bb.BUSINESS_MARKETS:
            sub = bv[bv["market"] == market]
            states = sub["state"].tolist()
            c = Counter(states)
            share = {k: round(100 * v / len(states), 2) for k, v in c.items()}
            raw_sw = sum(1 for a, b in zip(states, states[1:]) if a != b)
            confirmed = sub[sub["status"] == "已确认"]["state"].tolist()
            conf_sw = sum(1 for a, b in zip(confirmed, confirmed[1:]) if a != b)
            years = len(states) / 244
            zero = [p for p in bb.PATTERN_PRIORITY if p != "常态" and c[p] == 0]
            print(f"  {market}: {sub['trade_date'].min()} ~ {sub['trade_date'].max()}｜"
                  f"{len(states)} 日｜raw 切换 {raw_sw}（年均 {raw_sw/years:.1f}）｜"
                  f"已确认序列切换 {conf_sw}（年均 {conf_sw/years:.1f}）｜"
                  f"常态 {share.get('常态', 0):.2f}%｜0% 形态 {zero or '无'}")
            print("    占比 " + " ".join(f"{k}={share.get(k,0):.2f}%"
                                        for k in bb.PATTERN_PRIORITY))
            print(f"    已确认 {int((sub['status']=='已确认').sum())} 日｜"
                  f"低置信 {int((sub['confidence']=='低').sum())} 日")
            if zero:
                failures.append(f"{market} 存在长期 0% 形态：{zero}")
            if share.get("常态", 0) >= 90:
                failures.append(f"{market} 常态占比 >=90%，规则过严")

        print("\n=== ⑤ PIT 敏感性（仅用截至 t 样本重标切点）===")
        frozen = {m: _frame(conn, m, params) for m in bb.BUSINESS_MARKETS}
        for end in PIT_ENDS:
            for market in bb.BUSINESS_MARKETS:
                pit = _frame(conn, market, params, _pit_ecdf(conn, market, end))
                common = set(frozen[market]) & set(pit)
                same = sum(1 for dt in common
                           if frozen[market][dt]["state"] == pit[dt]["state"])
                rate = 100 * same / max(1, len(common))
                print(f"  calib_end={end} {market}: 一致率 {rate:.2f}% "
                      f"({same}/{len(common)})")
                if rate < 85:
                    failures.append(f"{market} PIT@{end} 一致率 {rate:.1f}% < 85%")

        dup = bv.groupby(["market", "trade_date"]).size()
        if (dup > 1).any():
            failures.append("主键重复")
        print(f"\n主键唯一 = {'✅' if not (dup > 1).any() else '❌'}")

    print("\n" + ("=" * 60))
    if failures:
        print("❌ 验收未通过：")
        for f in failures:
            print("   -", f)
        return 1
    print("✅ 全部验收项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
