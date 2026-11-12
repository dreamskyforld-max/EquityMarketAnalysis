#!/usr/bin/env python3
"""个股画像 L2/L3 结论切点标定（只读，不写库）。

用途：doc/stock_profile_l2_l3.md §12.2 第 2 项「params 实测标定」。按 v1.0 规则表
在本机当前有效 L1 标签快照上模拟 6 个结论，输出各 enum 占比 / 出档率，检查
§10.1 的「无 0% 档、单档 ≤80%」约束，为冻结切点提供依据。

资源约定（AGENTS.md 计算任务资源约定）：
  - 大表查询带 tag_code 实体过滤 + eff_to 当前态条件（R7）；
  - 单次加载 <50 万行，不分批（R1）；
  - 只读、零写入（R3/R6 不涉及）；
  - 结束打印峰值 RSS，便于留档。

用法：.venv/bin/python3 tools/calibrate_conclusion_cutpoints.py
"""
from __future__ import annotations

import platform
import resource
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_conn

# 附录 D 的 6 组值域，用于「0% 档」检查
DOMAIN_CODES = {
    "dim_valuation_conclusion": ["rel_cheap", "rel_fair", "rel_expensive",
                                 "hist_cheap_ind_expensive", "hist_expensive_ind_cheap",
                                 "loss", "negative"],
    "dim_quality_conclusion": ["solid", "high_but_volatile", "paper_profit",
                               "leveraged", "weak", "deteriorating"],
    "dim_growth_conclusion": ["fast_sustained", "fast_accelerating", "fast_decelerating",
                              "slow_steady", "contracting", "revenue_profit_divergent"],
    "dim_position_state": ["low_quiet", "low_volume_start", "trend_continuation",
                           "high_overheat", "downtrend_unresolved", "range_bound"],
    "biz_value_quality_type": ["quality_rel_cheap", "quality_rel_fair", "quality_rel_expensive",
                               "value_trap_risk", "weak_and_expensive"],
    "biz_growth_quality_type": ["sustained_cash_backed", "cash_burning_expansion",
                                "paper_growth", "one_off_like", "contraction"],
}

# 6 个结论判定用输入涉及的 L1 标签（§5 / §6 的 inputs[]）
TAGS = [
    "val_pe_hist_pct", "val_pe_ind_pct", "val_pe_ttm_tier", "val_pb_tier",
    "qal_roe_tier", "qal_roe_stability", "qal_earnings_quality", "qal_leverage_tier",
    "grw_revenue_yoy_tier", "grw_profit_yoy_tier",
    "grw_growth_persistence", "grw_growth_accel",
    "tst_price_position_52w", "trd_momentum_12_1", "att_volume_surge",
]

# v1.0 拟定切点（§12.2 第 2 项）
P = {
    "coverage_min": 0.6,
    "cheap_pctl": 20, "expensive_pctl": 80, "divergence_gap_pctl": 30,
    "stable_max": 2, "volatile_min": 4, "quality_weak_max": 2,
    "leverage_high": 4, "roe_high": 4,
    "divergence_gap_tier": 2, "high_growth_min": 4, "persistence_min": 2,
    "low_pos_max": 2, "high_pos_min": 4, "vol_quiet_max": 0.5,
    "vol_active_min": 2.0, "weak_momentum_max": 2,
}

# 每个结论的判定用 field -> (tag_code, 取 key_value 还是 num_value)
INPUTS = {
    "dim_valuation_conclusion": [
        ("pe_hist_pct", "val_pe_hist_pct", "num"),
        ("pe_ind_pct", "val_pe_ind_pct", "num"),
        ("loss_flag", "val_pe_ttm_tier", "kv"),
        ("negative_flag", "val_pb_tier", "kv"),
    ],
    "dim_quality_conclusion": [
        ("roe_tier", "qal_roe_tier", "kv"),
        ("roe_stability", "qal_roe_stability", "kv"),
        ("earnings_quality_tier", "qal_earnings_quality", "kv"),
        ("leverage_tier", "qal_leverage_tier", "kv"),
    ],
    "dim_growth_conclusion": [
        ("rev_tier", "grw_revenue_yoy_tier", "kv"),
        ("np_tier", "grw_profit_yoy_tier", "kv"),
        ("rev_yoy", "grw_revenue_yoy_tier", "num"),
        ("np_yoy", "grw_profit_yoy_tier", "num"),
        ("persistence", "grw_growth_persistence", "kv"),
        ("accel", "grw_growth_accel", "kv"),
    ],
    "dim_position_state": [
        ("position_tier", "tst_price_position_52w", "kv"),
        ("momentum_tier", "trd_momentum_12_1", "kv"),
        ("volume_surge", "att_volume_surge", "kv"),
        ("volume_ratio_60", "att_volume_surge", "num"),
        # ma_alignment 由 v_daily_quote.close 现算，本次快照无该列 -> 记缺失
        ("ma_alignment", None, "derived"),
    ],
}


def load_snapshot() -> tuple[dict, dict]:
    """当前有效 L1 快照 + free_cash_flow 符号。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT stock_code, tag_code, key_value, num_value
            FROM profile.tag_value
            WHERE eff_to = '9999-12-31' AND tag_code = ANY(%s)
            """,
            (TAGS,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT DISTINCT ON (stock_code) stock_code, free_cash_flow
            FROM financial_indicator
            ORDER BY stock_code, report_date DESC
            """
        )
        fcf = {r[0]: (1 if r[1] > 0 else -1 if r[1] < 0 else 0)
               for r in cur.fetchall() if r[1] is not None}

    snap: dict[str, dict] = defaultdict(dict)
    for code, tag, kv, nv in rows:
        snap[code][(tag, "kv")] = kv
        snap[code][(tag, "num")] = nv
    return snap, fcf


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def values(snap, spec):
    """按 INPUTS 取字段值并算 G1 coverage（无效成员不计分子）。"""
    out, valid = {}, 0
    for field, tag, col in spec:
        if col == "derived":
            out[field] = None
            continue
        raw = snap.get((tag, col))
        if col == "kv" and field.endswith("_flag"):
            out[field] = (raw == "LOSS") if field == "loss_flag" else (raw == "NEGATIVE")
            valid += 1
        elif col == "kv" and raw in ("LOSS", "NEGATIVE"):
            out[field] = None
        elif col == "kv":
            out[field] = _int(raw) if raw not in (None, "surge", "normal", "shrink",
                                                   "accelerating", "steady", "decelerating") else raw
            valid += out[field] is not None
        else:
            out[field] = _num(raw)
            valid += out[field] is not None
    return out, valid / len(spec)


def judge_valuation(v):
    if v["loss_flag"] is True:
        return "loss"
    if v["negative_flag"] is True:
        return "negative"
    a, b = v["pe_hist_pct"], v["pe_ind_pct"]
    if a is None or b is None:
        return None
    if abs(a - b) >= P["divergence_gap_pctl"]:
        return "hist_cheap_ind_expensive" if a < b else "hist_expensive_ind_cheap"
    if a <= P["cheap_pctl"] and b <= P["cheap_pctl"]:
        return "rel_cheap"
    if a >= P["expensive_pctl"] and b >= P["expensive_pctl"]:
        return "rel_expensive"
    return "rel_fair"


def judge_quality(v):
    s, q, r, l = (v["roe_stability"], v["earnings_quality_tier"],
                  v["roe_tier"], v["leverage_tier"])
    if s is None or q is None or r is None or l is None:
        return None
    if s >= P["volatile_min"] and q <= P["quality_weak_max"]:
        return "deteriorating"
    if q <= P["quality_weak_max"]:
        return "paper_profit"
    if l >= P["leverage_high"]:
        return "leveraged"
    if r >= P["roe_high"] and s <= P["stable_max"]:
        return "solid"
    if r >= P["roe_high"]:
        return "high_but_volatile"
    return "weak"


def judge_growth(v):
    rt, nt, ry, ny, pe, ac = (v["rev_tier"], v["np_tier"], v["rev_yoy"],
                              v["np_yoy"], v["persistence"], v["accel"])
    if rt is None or nt is None:
        return None
    if abs(rt - nt) >= P["divergence_gap_tier"]:
        return "revenue_profit_divergent"
    if (ry is not None and ry < 0) or (ny is not None and ny < 0):
        return "contracting"
    if rt >= P["high_growth_min"] and nt >= P["high_growth_min"] \
            and pe is not None and pe >= P["persistence_min"] and ac == "steady":
        return "fast_sustained"
    if rt >= P["high_growth_min"] and ac == "accelerating":
        return "fast_accelerating"
    if rt >= P["high_growth_min"] and ac == "decelerating":
        return "fast_decelerating"
    return "slow_steady"


def judge_position(v):
    pt, mt, vr = v["position_tier"], v["momentum_tier"], v["volume_ratio_60"]
    if pt is None or vr is None:
        return None
    # ma_alignment 本次快照缺失（需 v_daily_quote.close rolling 现算），
    # 故只统计不依赖 ma_alignment 的三条规则，其余落兜底。
    if pt <= P["low_pos_max"] and vr <= P["vol_quiet_max"]:
        return "low_quiet"
    if pt >= P["high_pos_min"] and vr >= P["vol_active_min"]:
        return "high_overheat"
    if mt is not None and mt <= P["weak_momentum_max"]:
        return "downtrend_unresolved"
    return "range_bound"


JUDGES = {
    "dim_valuation_conclusion": judge_valuation,
    "dim_quality_conclusion": judge_quality,
    "dim_growth_conclusion": judge_growth,
    "dim_position_state": judge_position,
}

VAL_CLASS = {"rel_cheap": "cheap", "hist_cheap_ind_expensive": "cheap",
             "rel_fair": "neutral", "hist_expensive_ind_cheap": "neutral",
             "rel_expensive": "expensive", "loss": "no_anchor", "negative": "no_anchor"}
QAL_CLASS = {"solid": "solid", "high_but_volatile": "volatile",
             "paper_profit": "weak", "leveraged": "weak",
             "weak": "weak", "deteriorating": "weak"}


def main():
    snap, fcf = load_snapshot()
    print(f"snapshot: {len(snap)} stocks, {len(snap) and sum(len(x) for x in snap.values()) // 2} tag-rows")

    l2 = {}
    for code, judge in JUDGES.items():
        spec = INPUTS[code]
        dist, total, passed = Counter(), 0, 0
        for stock, data in snap.items():
            v, cov = values(data, spec)
            total += 1
            # G4 业务码豁免 G1：亏损 / 净资产为负必须出档。这类票通常同时缺两个
            # PE 分位（亏损不参与分位），coverage=2/4=0.5 < 0.6，否则会被 G1 挡掉。
            if code == "dim_valuation_conclusion" and (
                    v.get("loss_flag") is True or v.get("negative_flag") is True):
                result = "loss" if v.get("loss_flag") is True else "negative"
            elif cov < P["coverage_min"]:
                continue
            else:
                result = judge(v)
            if result is None:
                continue
            passed += 1
            dist[result] += 1
            l2.setdefault(code, {})[stock] = result
        report(code, dist, total, passed)

    # L3 用模拟出的 L2 结果做链式估算
    vq, gq = Counter(), Counter()
    vq_total = gq_total = 0
    for stock, data in snap.items():
        val = l2.get("dim_valuation_conclusion", {}).get(stock)
        qal = l2.get("dim_quality_conclusion", {}).get(stock)
        if val and qal:
            vq_total += 1
            vc, qc = VAL_CLASS[val], QAL_CLASS[qal]
            if vc == "no_anchor" or (vc == "neutral" and qc != "solid"):
                continue  # 整行不发布
            key = {( "cheap", "solid"): "quality_rel_cheap",
                   ("neutral", "solid"): "quality_rel_fair",
                   ("expensive", "solid"): "quality_rel_expensive"}.get((vc, qc))
            if key is None:
                key = "weak_and_expensive" if vc == "expensive" else "value_trap_risk"
            vq[key] += 1

        grow = l2.get("dim_growth_conclusion", {}).get(stock)
        if grow:
            gq_total += 1
            q = _int(data.get(("qal_earnings_quality", "kv")))
            sign = fcf.get(stock)
            pe = _int(data.get(("grw_growth_persistence", "kv")))
            ac = data.get(("grw_growth_accel", "kv"))
            if grow == "contracting":
                gq["contraction"] += 1
            elif q is not None and q <= P["quality_weak_max"]:
                gq["paper_growth"] += 1
            elif sign is not None and sign < 0 and grow in ("fast_sustained", "fast_accelerating"):
                gq["cash_burning_expansion"] += 1
            elif pe is not None and pe <= 1 and ac == "accelerating" \
                    and grow in ("fast_accelerating", "fast_sustained"):
                gq["one_off_like"] += 1
            else:
                gq["sustained_cash_backed"] += 1
    report("biz_value_quality_type", vq, vq_total, sum(vq.values()))
    report("biz_growth_quality_type", gq, gq_total, sum(gq.values()))

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mb = peak / 1024 / 1024 if platform.system() == "Darwin" else peak / 1024
    print(f"\npeak_rss_mb: {mb:.1f}")


def report(code, dist, total, passed):
    print(f"\n=== {code} ===")
    print(f"  candidates={total}  published={passed}  "
          f"rate={passed / total * 100:.1f}%" if total else "  no data")
    if not passed:
        return
    for k, n in dist.most_common():
        flag = "  <-- >80%" if n / passed > 0.8 else ""
        print(f"  {k:<32} {n:>6}  {n / passed * 100:>5.1f}%{flag}")
    zeros = [k for k in DOMAIN_CODES.get(code, []) if dist.get(k, 0) == 0]
    if zeros:
        print(f"  WARN zero-bucket: {zeros}")


if __name__ == "__main__":
    main()
