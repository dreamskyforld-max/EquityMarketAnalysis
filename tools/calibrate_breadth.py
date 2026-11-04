#!/usr/bin/env python3
"""宽度维度 `DIM.BREADTH` 的族划分与档位切点标定（v2.1.9）。

读 `regime.indicator_value.percentile`（`--backfill` 后的 v2.1.8 口径），按
`doc/market_profile.md` §5.1 / §5.7 L2-b / §8.1.1 产出：

  ① **族划分**：一阶差分相关矩阵（水平值相关会被共同趋势污染）+ PCA 第一主成分
     解释度 + 年代分层稳定性；三条判据全过才保留该族，否则拆成单成员族。
  ② **族代表**：历史长度 × PC1 载荷打分，每族固定取 1 个，结果写入字典并纳入
     `params_hash`（§5.1「每族取 1 个代表」）。
  ③ **档位切点**：按各市场历史 score 分布取 P20/P40/P60/P80（等频、按市场冻结），
     等值归较高档（§8.1.1）。
  ④ **验收报告**：标定样本内每档占比（应约 20%）、按年代分层占比（检查幸存者
     偏差传导）、样本外（2026）占比漂移。

⚠ **参数层前视**：冻结切点由全历史标定，用它回算历史档位时历史标签并非当时
可知，报告须注明（§8.1.1 / §8.5）。

用法：
    .venv/bin/python3 tools/calibrate_breadth.py --probe     # 只读打印报告
    .venv/bin/python3 tools/calibrate_breadth.py --write     # 写 regime.dimension_dict
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from db import get_conn, bulk_upsert, read_sql
import market_state_daily as msd


def _read(sql, conn, params=None):
    """统一走 db.read_sql（全项目唯一读库入口）。"""
    return read_sql(conn, sql, params=params)

DIM = "DIM.BREADTH"
LABELS = ["极窄", "偏窄", "中性", "偏广", "极广"]
CALIB_START = datetime.date(1996, 1, 1)
CALIB_END = datetime.date(2025, 12, 31)

# 族判据（可判定、由 tests/test_breadth_dimension.py 钉住）
MIN_INTRA_CORR = 0.50      # 族内一阶差分相关中位数
MIN_PC1 = 0.60             # 族内 PC1 解释度
MIN_DECADE_CORR = 0.30     # 每个年代分层的族内相关中位数下限
MIN_CONSTRUCT_CORR = 0.40  # §8.5 构念效度：结构族成员与「核心宽度分」相关（取两市场最小值）

# 三个通过族的代表构成核心宽度分；结构族成员与之相关过低 → 移出 score，仅作 flags
CORE_REPS = ["BREADTH.ADV_RATIO", "BREADTH.PCT_ABOVE_MA60", "BREADTH.NEW_HIGH_LOW_20"]
STRUCT_CODES = ["BREADTH.EQ_VS_CAP_20D", "BREADTH.AMT_CONC_TOP10",
                "BREADTH.UP_DOWN_STRENGTH", "BREADTH.LIMIT_NET"]

COVERAGE_MIN = 0.6
DISPERSION_MAX = 30.0
WEIGHT_CAP = 0.25

CANDIDATES = {
    "涨跌家数": ["BREADTH.ADV_RATIO", "BREADTH.ADV_MINUS_DEC", "BREADTH.MCCLELLAN"],
    "均线": ["BREADTH.PCT_ABOVE_MA60", "BREADTH.PCT_ABOVE_MA200"],
    "新高新低": ["BREADTH.NEW_HIGH_LOW_20", "BREADTH.NEW_HIGH_LOW_60"],
    "结构": ["BREADTH.EQ_VS_CAP_20D", "BREADTH.AMT_CONC_TOP10",
             "BREADTH.UP_DOWN_STRENGTH", "BREADTH.LIMIT_NET"],
}


def _load(conn, market: str):
    """读该市场宽度成员的 as-of 分位，按 indicator_dict.direction 统一极性。"""
    codes = sorted(msd.BREADTH_ROLLING_CODES)
    sql = """
        SELECT iv.trade_date, iv.indicator_code, iv.percentile,
               d.direction, d.market_scope
        FROM regime.indicator_value iv
        JOIN regime.indicator_dict d ON d.indicator_code = iv.indicator_code
        WHERE iv.market = %s AND iv.indicator_code = ANY(%s)
          AND iv.trade_date >= %s AND iv.percentile IS NOT NULL
        ORDER BY iv.trade_date
    """
    df = _read(sql, conn, params=[market, codes, CALIB_START])
    meta = {c: (g["direction"].iloc[0], g["market_scope"].iloc[0])
            for c, g in df.groupby("indicator_code")}
    wide = df.pivot_table(index="trade_date", columns="indicator_code",
                          values="percentile", aggfunc="last").sort_index()
    for c in wide.columns:
        if meta[c][0] == "high_risk":
            wide[c] = 100.0 - wide[c]        # 集中度：高 = 窄 → 翻向
    return wide, meta


def _corr_stats(diff: pd.DataFrame, members: list[str]):
    """族内一阶差分相关中位数 / PC1 解释度 / PC1 载荷。"""
    sub = diff[members].corr(min_periods=250).values.astype(float)
    n = len(members)
    off = sub[~np.eye(n, dtype=bool)]
    med = float(np.nanmedian(off)) if n > 1 else 1.0
    m = np.nan_to_num(sub, nan=0.0)
    np.fill_diagonal(m, 1.0)
    eig, vec = np.linalg.eigh(m)
    pc1 = float(eig[-1] / eig.sum()) if eig.sum() > 0 else 1.0
    load = {c: float(abs(v)) for c, v in zip(members, vec[:, -1])}
    return med, pc1, load


def _decade_corr(diff: pd.DataFrame, members: list[str]) -> dict:
    out = {}
    years = pd.to_datetime(diff.index).year
    for dec, g in diff.groupby((years // 10) * 10):
        if len(g) < 250 or len(members) < 2:
            continue
        c = g[members].corr(min_periods=100).values.astype(float)
        off = c[~np.eye(len(members), dtype=bool)]
        out[str(int(dec))] = round(float(np.nanmedian(off)), 3)
    return out


def calibrate(wide, meta, market: str, flags_only: set) -> dict:
    diff = wide.diff()
    cal = wide[(wide.index >= CALIB_START) & (wide.index <= CALIB_END)]

    # §8.5 构念效度：三个通过族的代表构成「核心宽度分」，结构族成员与它的相关
    # 用来判定是否只是「同一构念的另一种读数」（过低 → 移出 score，仅作 flags）。
    core = cal[[c for c in CORE_REPS if c in cal.columns]].mean(axis=1, skipna=True)
    construct_corr = {c: round(float(cal[c].corr(core)), 3)
                      for c in STRUCT_CODES if c in cal.columns}

    blocks, effective = {}, []
    for name, members in CANDIDATES.items():
        mem = [c for c in members if c in wide.columns]
        if not mem:
            continue
        med, pc1, load = _corr_stats(diff, mem)
        dec = _decade_corr(diff, mem)
        stable = all(v >= MIN_DECADE_CORR for v in dec.values()) if dec else True
        passed = len(mem) < 2 or (med >= MIN_INTRA_CORR and pc1 >= MIN_PC1 and stable)
        eligible = [c for c in mem if c not in flags_only]
        nobs = wide[mem].notna().sum()
        rep = max(eligible or mem,
                  key=lambda c: 0.6 * nobs[c] / max(nobs.max(), 1) + 0.4 * load[c])
        blocks[name] = {
            "members": mem, "eligible": eligible, "intra_corr": round(med, 3),
            "pc1": round(pc1, 3), "decade_corr": dec, "passed": bool(passed),
            "representative": rep, "n_obs": {c: int(nobs[c]) for c in mem},
        }
        if passed:
            effective.append({"block": name, "members": eligible, "representative": rep})
        else:
            # §5.1：结构族标定不通过 → 拆分（每个成员自成一族），不得当同质族取代表
            for c in eligible:
                effective.append({"block": f"{name}·{c.split('.')[-1]}",
                                  "members": [c], "representative": c})

    rep_codes = [b["representative"] for b in effective]
    score = cal[rep_codes].mean(axis=1, skipna=True).dropna()
    score_full = wide[rep_codes].mean(axis=1, skipna=True).dropna()
    bands = [round(float(np.percentile(score, q)), 2) for q in (20, 40, 60, 80)]

    def _shares(s: pd.Series) -> dict:
        lv = np.digitize(s.values, bands)          # 等值归较高档，见 §8.1.1
        return {LABELS[i]: round(float((lv == i).mean() * 100), 1) for i in range(5)}

    decade_share = {}
    for dec, g in score.groupby(pd.to_datetime(score.index).year // 10 * 10):
        if len(g) >= 250:
            decade_share[f"{int(dec) * 10}s"] = _shares(g)
    oos = score_full[score_full.index > CALIB_END]

    return {
        "market": market,
        "sample": {"start": str(score.index.min()), "end": str(score.index.max()),
                   "n": int(len(score))},
        "blocks": blocks, "effective_blocks": effective,
        "construct_corr": construct_corr, "flags_only": sorted(flags_only),
        "state_bands": bands,
        "score_stats": {"p05": round(float(score.quantile(.05)), 2),
                        "p50": round(float(score.median()), 2),
                        "p95": round(float(score.quantile(.95)), 2),
                        "std": round(float(score.std()), 2)},
        "band_share_in_sample": _shares(score),
        "band_share_by_decade": decade_share,
        "band_share_oos": _shares(oos) if len(oos) else {},
        "oos_n": int(len(oos)),
        "members_meta": {c: {"direction": meta[c][0], "market_scope": meta[c][1]}
                         for c in wide.columns},
    }


def build_dict_row(results: dict, flags_only: set) -> dict:
    """两个市场的标定结果 → regime.dimension_dict 一行。"""
    markets = sorted(results)
    bands = {m: results[m]["state_bands"] for m in markets}
    cn = results["CN"]            # 成员清单用 CN（最全，含 LIMIT_NET）；HK 由 coverage 分母排除
    rep_by_code, block_by_code = {}, {}
    for b in cn["effective_blocks"]:
        for c in b["members"]:
            block_by_code[c] = b["block"]
            rep_by_code[c] = (c == b["representative"])
    # 成员清单覆盖**全部**宽度原子（含 flags_only）：它们不进 score，
    # 但仍计入 coverage / dispersion / contrib / flags，不可静默消失。
    members = []
    for c in sorted(cn["members_meta"]):
        m = cn["members_meta"][c]
        is_rep = bool(rep_by_code.get(c, False)) and c not in flags_only
        members.append({
            "code": c,
            "sign": -1 if m.get("direction") == "high_risk" else 1,
            "block": block_by_code.get(c, "诊断"),
            "representative": is_rep,
            "flags_only": c in flags_only,
            "weight": round(1.0 / len(cn["effective_blocks"]), 4) if is_rep else 0.0,
        })
    params = {
        "winsor": [2, 98], "coverage_min": COVERAGE_MIN,
        "dispersion_max": DISPERSION_MAX, "weight_cap": WEIGHT_CAP,
        "pct_window": msd.BREADTH_PCT_WINDOW,
        "calib_start": str(CALIB_START), "calib_end": str(CALIB_END),
        "min_intra_corr": MIN_INTRA_CORR, "min_pc1": MIN_PC1,
        "min_decade_corr": MIN_DECADE_CORR,
        "min_construct_corr": MIN_CONSTRUCT_CORR,
        "flags_only": sorted(flags_only),
        "calib_version": "v2.1.9",
    }
    payload = {"members": members, "state_bands": bands, "params": params,
               "labels": LABELS, "agg_method": "block_equal"}
    ph = hashlib.md5(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    block_agg = {b["block"]: "mean" for b in cn["effective_blocks"]}
    remark = (
        "成员归属唯一真源为 indicator_dict.domain；members[].sign 统一极性（-1=取 100−分位）。"
        "coverage 分母按 market 判定：members[].code 联 indicator_dict，"
        "market_scope ∈ {当前市场, CN+HK, GLOBAL} 的计数（§5.7 L2-c），HK 无 LIMIT_NET。"
        "agg=block_equal：每族固定取 1 个代表 → 族间等权；非代表成员不进 score，"
        "但仍计入 coverage / dispersion / contrib / flags。"
        "族标定实证：三个同质族一阶差分相关 0.59~0.93 / PC1 0.79~0.96 通过；"
        "「结构族」相关约 0.10 / PC1 约 0.45 不通过 → 拆为单成员族；"
        "其中与核心宽度分构念相关过低者（flags_only）移出 score、仅作 flags。"
        "⚠ 代表由 CN 标定、**两市场共用**（dimension_dict 一行无法按市场存不同代表）："
        "HK 单独标定显示涨跌家数族最优代表为 ADV_MINUS_DEC（CN 为 ADV_RATIO），"
        "两者同族且一阶差分相关 >0.69；统一代表以保两市场读数可比。"
        "⚠ 参数层前视：state_bands 由全历史 score 分布标定并冻结，"
        "回算历史档位时标签非当时可知（§8.1.1），离线验证报告必须注明；"
        "标定时 PIT 成分池尚未重建，宽度分位带 extra.survivorship_bias=true / quality='low'，"
        "按年代分层的档位占比见 tools/calibrate_breadth.py --probe 报告。"
    )
    return {
        "dimension_code": DIM, "dimension_name": "宽度",
        "question": "上涨的参与面有多广（描述型，高分=越广，非'越好'）",
        "polarity": "high_good", "members": members,
        "agg_method": "block_equal", "block_agg": block_agg,
        "state_bands": bands, "state_labels": LABELS,
        "params": params, "params_hash": ph,
        "is_active": True, "remark": remark,
        "updated_at": datetime.datetime.now(msd.TZ_CN),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只读打印标定报告")
    ap.add_argument("--write", action="store_true", help="写 regime.dimension_dict")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe 或 --write")

    with get_conn() as conn:
        data = {mk: _load(conn, mk) for mk in ("CN", "HK")}
    # §8.5 构念效度：取**两市场相关的最小值**，避免单市场侥幸通过
    corr_by_code = {}
    for wide, _ in data.values():
        core = wide[[c for c in CORE_REPS if c in wide.columns]].mean(axis=1, skipna=True)
        for c in STRUCT_CODES:
            if c in wide.columns:
                corr_by_code.setdefault(c, []).append(float(wide[c].corr(core)))
    flags_only = {c for c, vs in corr_by_code.items() if min(vs) < MIN_CONSTRUCT_CORR}
    results = {mk: calibrate(wide, meta, mk, flags_only)
               for mk, (wide, meta) in data.items()}

    with get_conn() as conn:
        if args.write:
            row = build_dict_row(results, flags_only)
            bulk_upsert(conn, "regime.dimension_dict", [row],
                        conflict_cols=["dimension_code"], skip_null_updates=True)
            print(f"✅ 写入 regime.dimension_dict {DIM}，params_hash={row['params_hash']}")

    if args.probe:
        print(f"构念效度（两市场最小相关，阈值 {MIN_CONSTRUCT_CORR}）: "
              f"{ {c.split('.')[-1]: round(min(v), 3) for c, v in corr_by_code.items()} }")
        print(f"flags_only（不进 score，仅作 flags）: {sorted(flags_only)}")
        for mk, r in results.items():
            print(f"\n===== {mk} | 标定样本 {r['sample']['start']}~{r['sample']['end']} "
                  f"({r['sample']['n']} 日) =====")
            print(f"{'族':<14}{'成员数':>6}{'相关中位':>10}{'PC1':>8}{'通过':>6}  代表")
            for name, b in r["blocks"].items():
                print(f"{name:<14}{len(b['members']):>6}{b['intra_corr']:>10.3f}"
                      f"{b['pc1']:>8.3f}{'是' if b['passed'] else '否':>6}  {b['representative']}")
            print(f"年代分层相关: {r['blocks']['涨跌家数']['decade_corr']}")
            print(f"有效族数 {len(r['effective_blocks'])}: "
                  f"{[b['block'] for b in r['effective_blocks']]}")
            print(f"score 统计 {r['score_stats']}")
            print(f"state_bands P20/40/60/80 = {r['state_bands']}")
            print(f"样本内档位占比 {r['band_share_in_sample']}")
            print(f"样本外占比（{r['oos_n']} 日） {r['band_share_oos']}")
            for dec, sh in r["band_share_by_decade"].items():
                print(f"  {dec}: {sh}")


if __name__ == "__main__":
    main()
