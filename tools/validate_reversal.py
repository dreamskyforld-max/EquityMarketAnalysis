#!/usr/bin/env python3
"""`BIZ.REVERSAL_RISK` 落库验收（只读）：契约 / 独立复算 / 分布 / 增量信息 / PIT。

判定项对应 `doc/market_state_data_architecture.md` §6.4 v1.34 上线门禁：
  ① 字典（rule_set / 多态列语义 / 族清单版本）与读数口径一致；
  ② score 为 0..5 整数族计数、level 1..3、state 三选一、detail 十键齐全；
  ③ 独立复算（不调用生产者计算函数）逐值一致；
  ④ 三档时间占比（无长期 0%）、各族命中率（无长期 0% / 100%）、冲突场景占比；
  ⑤ 发布日历与「当日 5 族齐全」门禁，早于冻结起点不产行；
  ⑥ 与已落地 §6.2 形态 / §6.3 背离的区分度（TVD，披露项，不设 FAIL）；
  ⑦ 「仅用截至 t 样本标切点」的 PIT 敏感性一致率（参数层前视披露）。
任一 FAIL → 退出码 1。

用法：
    .venv/bin/python3 tools/validate_reversal.py
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

BIZ = bb.REVERSAL_BIZ_CODE
PIT_ENDS = (datetime.date(2022, 12, 31), datetime.date(2023, 12, 31),
            datetime.date(2024, 12, 31))
DETAIL_KEYS = ("top_state", "bottom_state", "top_families", "bottom_families",
               "top_cnt", "bottom_cnt", "unavailable_families",
               "signal_conflict", "dwell_days", "missing")
FAILS: list[str] = []


def j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def _band(cnt: int, defn: dict) -> int:
    """独立实现的判读档（不调用 bb.reversal_band_of）。"""
    if cnt >= defn["high_from"]:
        return 3
    if cnt >= defn["warn_from"]:
        return 2
    return 1


def _load_atoms(conn, market: str, params: dict):
    atoms = params["family_atoms"][market]
    codes = sorted(set(atoms.values()))
    df = read_sql(conn, """
        SELECT trade_date, indicator_code, percentile
        FROM regime.indicator_value
        WHERE market=%s AND indicator_code=ANY(%s)
          AND trade_date>=%s AND percentile IS NOT NULL
    """, (market, codes, bb.REVERSAL_DATA_START))
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="trade_date", columns="indicator_code",
                          values="percentile", aggfunc="first")


def _recompute(piv, market: str, params: dict, cuts=None, bands=None) -> dict:
    """从 L1 原子分位独立复算；返回 {trade_date: 期望读数}。"""
    cuts = cuts or params["family_cuts"][market]
    bands = bands or params["market_bands"][market]
    atoms = params["family_atoms"][market]
    publish_from = datetime.date.fromisoformat(params["publish_from"][market])
    fams = params["families"]
    if any(atoms[f] not in piv.columns for f in fams):
        return {}
    out = {}
    for d, row in piv.iterrows():
        date = d.date() if hasattr(d, "date") else d
        if date < publish_from:
            continue
        vals = [row[atoms[f]] for f in fams]
        if any(v is None or pd.isna(v) for v in vals):
            continue                      # 5 族齐全门禁
        v = {f: float(row[atoms[f]]) for f in fams}
        top_f = [f for f in fams if v[f] >= cuts[f]["top"]]
        bot_f = [f for f in fams if v[f] <= cuts[f]["bottom"]]
        tfc, bfc = len(top_f), len(bot_f)
        ts, bs = _band(tfc, bands), _band(bfc, bands)
        conflict = bool(params["conflict_downgrade"] and ts >= 2 and bs >= 2)
        if conflict:
            ts, bs = ts - 1, bs - 1
        out[date] = {
            "score": tfc, "level": ts,
            "state": params["state_labels"][ts - 1],
            "top_state": ts, "bottom_state": bs, "signal_conflict": conflict,
            "top_families": [params["family_labels"][f] for f in top_f],
            "bottom_families": [params["family_labels"][f] for f in bot_f],
            "top_cnt": tfc, "bottom_cnt": bfc,
        }
    return out


def _tvd(a: Counter, b: Counter) -> float:
    """两离散分布的全变差距离，用于衡量高档是否与全样本同形。"""
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return 0.0
    keys = set(a) | set(b)
    return round(0.5 * sum(abs(a[k] / na - b[k] / nb) for k in keys), 4)


def main() -> int:
    with get_conn() as conn:
        d = read_sql(conn, """
            SELECT biz_code, kind, inputs, params, output_schema, params_hash,
                   is_active, remark FROM regime.business_dict WHERE biz_code=%s
        """, (BIZ,))
        check(not d.empty and bool(d.iloc[0]["is_active"]),
              "字典行存在且启用", BIZ)
        if d.empty:
            print("\n结论：FAIL — 无字典行")
            return 1
        rec = d.iloc[0]
        params, schema = j(rec["params"]), j(rec["output_schema"])
        phash = rec["params_hash"]
        check(rec["kind"] == "rule_set", "kind = rule_set")
        check(bool(schema.get("level_semantics")) and
              bool(schema.get("score_semantics")),
              "output_schema 含多态列语义")
        check(set(DETAIL_KEYS) == set(schema.get("detail_keys", [])),
              "output_schema.detail_keys 与十键一致")
        check(bool(params.get("family_list_version")) and
              len(params.get("families", [])) == 5 and
              bool(params.get("upstream_atom_params_hashes")),
              "族清单版本 / 5 族 / 原子指纹已冻结")
        print(f"  params_hash = {phash}｜族清单 {params['family_list_version']}")

        for market in bb.BUSINESS_MARKETS:
            print(f"\n── {market} ──")
            rows = read_sql(conn, """
                SELECT trade_date, score, state, level, since_date, dwell_days,
                       status, confidence, detail, model_version
                FROM regime.business_value
                WHERE biz_code=%s AND market=%s ORDER BY trade_date
            """, (BIZ, market))
            check(not rows.empty, f"{market} 有读数", f"{len(rows)} 行")
            if rows.empty:
                continue
            check((rows["model_version"] == phash).all(),
                  f"{market} model_version == params_hash")
            check(rows["score"].between(0, 5).all() and
                  (rows["score"] % 1 == 0).all(),
                  f"{market} score 为 0..5 整数族计数")
            check(rows["level"].between(1, 3).all() and
                  rows["state"].isin(list(bb.REVERSAL_STATE_LABELS)).all(),
                  f"{market} level 1..3 / state 三选一")

            details = [j(x) for x in rows["detail"]]
            check(all(set(DETAIL_KEYS) <= set(x) for x in details),
                  f"{market} detail 十键齐全")
            check(all(int(x["top_cnt"]) == int(s) and
                      int(x["available_family_cnt"]) ==
                      params["min_available_families"] and
                      x["missing"] is None
                      for x, s in zip(details, rows["score"])),
                  f"{market} top_cnt / 5 族齐全 / missing 自洽")

            # ③ 独立复算逐值一致
            piv = _load_atoms(conn, market, params)
            exp = _recompute(piv, market, params)
            check(len(exp) == len(rows),
                  f"{market} 发布日历与独立复算行数一致",
                  f"库内 {len(rows)} / 复算 {len(exp)}")
            diffs = []
            for r, det in zip(rows.to_dict("records"), details):
                d = r["trade_date"]
                d = d.date() if hasattr(d, "date") else d
                e = exp.get(d)
                if e is None:
                    diffs.append((d, "缺复算行"))
                    continue
                got = {"score": int(r["score"]), "level": int(r["level"]),
                       "state": r["state"], "top_state": det["top_state"],
                       "bottom_state": det["bottom_state"],
                       "signal_conflict": det["signal_conflict"],
                       "top_families": det["top_families"],
                       "bottom_families": det["bottom_families"],
                       "top_cnt": det["top_cnt"], "bottom_cnt": det["bottom_cnt"]}
                if got != e:
                    diffs.append((d, f"{got} != {e}"))
            check(not diffs, f"{market} 独立复算逐值一致",
                  f"{len(diffs)} 处不符" + (f" 例：{diffs[:2]}" if diffs else ""))

            # ④ 分布与门禁
            share = rows["state"].value_counts(normalize=True).round(4).to_dict()
            print(f"  三档占比：{share}")
            check(all(share.get(s, 0) > 0 for s in bb.REVERSAL_STATE_LABELS),
                  f"{market} 无长期 0% 档（§8.5）")
            fam_hits = Counter()
            for det in details:
                fam_hits.update(det["top_families"])
                fam_hits.update(det["bottom_families"])
            n = len(rows)
            rates = {f: round(fam_hits[f] / n, 4) for f in params["family_labels"].values()}
            print(f"  各族命中率：{rates}")
            check(all(0 < v < 1 for v in rates.values()),
                  f"{market} 无长期 0% / 100% 命中族")
            conflicts = sum(1 for x in details if x["signal_conflict"])
            print(f"  顶底冲突降档 {conflicts} 日（{conflicts / n:.2%}）")

            # ⑤ 发布日历
            start = rows["trade_date"].min()
            start = start.date() if hasattr(start, "date") else start
            frozen = datetime.date.fromisoformat(params["publish_from"][market])
            check(start >= frozen, f"{market} 无早于冻结起点的行",
                  f"实际 {start} / 冻结 {frozen}")

            # ⑥ 与已落地 L3 的区分度（披露项）
            for peer, col in (("BIZ.MARKET_PATTERN", "state"),
                              ("BIZ.MARKET_DIVERGENCE", "level")):
                p = read_sql(conn, f"""
                    SELECT trade_date, {col} AS v FROM regime.business_value
                    WHERE biz_code=%s AND market=%s AND {col} IS NOT NULL
                """, (peer, market))
                if p.empty:
                    continue
                pmap = {}
                for x in p.to_dict("records"):
                    d = x["trade_date"]
                    pmap[d.date() if hasattr(d, "date") else d] = str(x["v"])
                allc = Counter(pmap.values())
                highc = Counter()
                for r in rows.to_dict("records"):
                    if r["state"] == "高风险":
                        d = r["trade_date"]
                        d = d.date() if hasattr(d, "date") else d
                        if d in pmap:
                            highc[pmap[d]] += 1
                print(f"  vs {peer}：高档分布 TVD = {_tvd(highc, allc)}"
                      f"（高档可交叉 {sum(highc.values())} 日）")

            # ⑦ PIT 敏感性（参数层前视披露）
            for end in PIT_ENDS:
                calib = bb.reversal_calibrate(conn, calib_end=end)
                pit = _recompute(piv, market, params,
                                 cuts=calib["family_cuts"][market],
                                 bands=calib["market_bands"][market])
                common = set(pit) & set(exp)
                if not common:
                    continue
                same = sum(1 for d in common
                           if pit[d]["state"] == exp[d]["state"])
                print(f"  PIT 截至 {end}：state 一致率 "
                      f"{same / len(common):.2%}（{len(common)} 日）")

    print("\n结论：" + ("PASS（无 FAIL）" if not FAILS else
                        f"FAIL — {len(FAILS)} 项：" + "；".join(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
