#!/usr/bin/env python3
"""估值维度 `DIM.VALUATION` 的字典种子标定（§5.2 v1.3 / market_profile v2.1.14）。

与 `tools/calibrate_breadth.py` 同形态：离线标定 → 写 `regime.dimension_dict` 一行，
日频生产者 `build_dimension_value.build_valuation_dimension` 只读字典、不重复标定。

标定内容：
  ① `state_bands`：按各市场 `score`（= `VAL.PE_TTM_MEDIAN.percentile`）历史分布取
     P20/P40/P60/P80 等频切点并冻结（§8.1.1）；样本不含 2026（样本外）。
  ② 成员清单固定为 §5.2 的 6 个（`VAL.ERP_Z` / `VAL.ERP_LG` 已移出，见 v1.2）。
  ③ 参数沿用 §2.4 通用口径 + §5.2 本环节占位值，全部写入 `params` 并纳入 `params_hash`。

⚠ **参数层前视**：切点由全历史标定并冻结，回算历史档位时标签并非当时可知
   （§8.1.1 / §8.5），报告须注明。CN 因口径约束恒不出档，但仍冻结一组切点，
   供将来解除约束时使用。

⚠ **低频源 PIT 重复入样**：`VAL.DIV_SPREAD`（HK 月频）/ `VAL.BUFFETT`（周频）被
   `_macro_pit_map` 前向填充到每个交易日，同一观测在 `percentile` 参考样本里重复
   入样（§2.4 规则 1 已登记为当前实现的已知偏差）。本次**不改 L1 分位数值**，
   只在 `params.low_freq_members` 登记，由 L2 写入 `flags.low_freq_pit_dup`；
   修正 L1 属破坏性变更，须单独确认并递增 `params_hash` / `model_version` 全量重算。

用法：
    .venv/bin/python3 tools/calibrate_valuation.py --probe     # 只读打印标定报告
    .venv/bin/python3 tools/calibrate_valuation.py --write     # 写 regime.dimension_dict
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn, bulk_upsert, read_sql  # noqa: E402

DIM = "DIM.VALUATION"
ANCHOR = "VAL.PE_TTM_MEDIAN"
LABELS = ["极便宜", "偏便宜", "中性", "偏贵", "极贵"]
CALIB_END = datetime.date(2025, 12, 31)
# 与 `indicator_value` 首次非空日对齐（2026-11-05 核定）
CALIB_START = {"CN": datetime.date(2018, 1, 2), "HK": datetime.date(2001, 9, 6)}

# §5.2 v1.2 定稿：6 个成员（ERP_Z / ERP_LG 为参考项，不进 members[]）
MEMBERS = [
    {"code": "VAL.PE_TTM_MEDIAN",  "sign":  1, "weight": 1.0, "block": "anchor"},
    {"code": "VAL.PB_MEDIAN",      "sign":  1, "weight": 0.0, "block": "check"},
    {"code": "VAL.PCT_BELOW_PB_1", "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.DIV_SPREAD",     "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.ERP",            "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.BUFFETT",        "sign":  1, "weight": 0.0, "block": "check"},
]
# BUFFETT 是 CN only；HK 的 eligible 由 market_scope 排除后为 5 个
CN_ONLY = {"VAL.BUFFETT"}
LOW_FREQ = ["VAL.DIV_SPREAD", "VAL.BUFFETT"]

PARAMS = {
    "winsor": [2, 98],          # 预留键：L1 分位用经验秩，当前不参与计算（文档 §2.4 规则 1）
    "coverage_min": 0.6,
    "dispersion_max": 30,
    "weight_cap": 0.25,          # 对 single_anchor 不生效（§2.4），仅为参数完整性
    "quality_sample": 750,
    "check_divergence": 20,
    "min_periods": 250,
    "cn_state_disabled": True,   # §5.2 口径约束：CN state 恒 NULL
    "low_freq_members": LOW_FREQ,
    "calib_start": {m: str(d) for m, d in CALIB_START.items()},
    "calib_end": str(CALIB_END),
    "calib_version": "v1.0",
}


def _load(conn, market: str) -> pd.DataFrame:
    """读标定区间内 6 个成员的 as-of 分位（带时间下界，资源规范 R7）。"""
    return read_sql(
        conn,
        "SELECT trade_date, indicator_code, percentile FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) "
        "  AND trade_date BETWEEN %s AND %s AND percentile IS NOT NULL "
        "ORDER BY trade_date",
        params=[market, [m["code"] for m in MEMBERS], CALIB_START[market], CALIB_END])


def calibrate(conn, market: str) -> dict:
    df = _load(conn, market)
    if df.empty:
        return {"market": market, "n": 0}
    wide = df.pivot_table(index="trade_date", columns="indicator_code",
                          values="percentile", aggfunc="last").sort_index()
    score = wide[ANCHOR].dropna() if ANCHOR in wide.columns else pd.Series(dtype=float)
    bands = [round(float(v), 2) for v in np.percentile(score.to_numpy(), [20, 40, 60, 80])] \
        if len(score) >= 50 else [20.0, 40.0, 60.0, 80.0]
    lv = np.digitize(score.to_numpy(), bands)
    shares = [round(float((lv == i).mean()), 4) for i in range(5)]

    eligible = [m["code"] for m in MEMBERS
                if m["code"] not in CN_ONLY or market == "CN"]
    aligned = wide.reindex(columns=eligible)
    valid = aligned.notna().sum(axis=1)
    coverage = valid / max(len(eligible), 1)
    dispersion = aligned.std(axis=1, ddof=1)
    dispersion_norm = dispersion * np.sqrt(valid / 10.0)
    gate = (coverage >= PARAMS["coverage_min"]) & (dispersion_norm <= PARAMS["dispersion_max"])

    return {
        "market": market, "n": int(len(score)),
        "start": str(score.index.min().date() if hasattr(score.index.min(), "date")
                     else score.index.min()),
        "end": str(score.index.max().date() if hasattr(score.index.max(), "date")
                   else score.index.max()),
        "state_bands": bands, "shares": shares,
        "pctl": {str(q): round(float(np.percentile(score.to_numpy(), q)), 2)
                 for q in (0, 20, 40, 60, 80, 100)},
        "eligible": eligible,
        "coverage_mean": round(float(coverage.mean()), 4),
        "gate_pass_ratio": round(float(gate.mean()), 4),
        "cn_state_disabled": market == "CN" and PARAMS["cn_state_disabled"],
    }


def build_dict_row(results: dict) -> dict:
    bands = {m: r["state_bands"] for m, r in results.items() if r.get("n")}
    payload = {"members": MEMBERS, "state_bands": bands, "params": PARAMS,
               "labels": LABELS}
    ph = hashlib.md5(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    remark = (
        "单锚 + 校验（§5.2 v1.2 / market_profile v2.1.14）："
        "score = VAL.PE_TTM_MEDIAN.percentile（sign=+1，高分=贵），其余 5 个成员 "
        "weight=0 只影响 confidence / contrib / flags。sign=-1 取 100−percentile（§2.4 规则 2）。"
        "coverage 分母 = members[].code 联 indicator_dict.market_scope 覆盖本市场的计数"
        "（CN 6 / HK 5，HK 排除 CN-only 的 VAL.BUFFETT）。"
        "⚠ VAL.ERP_Z / VAL.ERP_LG 是参考读数、不在 members[] 内：前者 percentile 恒 NULL"
        "（NO_PERCENTILE，走 raw_value 的 z 幅度通道，须单独读取），后者月频 CN only。"
        "⚠ blocks 形状固定为 {anchor:{code,percentile}, check:{n,divergent}}；"
        "flags 形状固定为 {n_sample, anchor_window, short_sample?, no_data?, low_freq_pit_dup?}"
        "（与 DIM.BREADTH 的 list 型 flags 按维度区分，§2.3.3 两处待固定口径在此关闭）。"
        "⚠ CN 口径约束：回填段是 QFQ 缩放值且成分池断裂，state 恒 NULL + confidence='低' + "
        "flags.short_sample=true；HK 起点已核定 2001-09-06，按标定切点出档。"
        "⚠ 低频源 PIT 重复入样（VAL.DIV_SPREAD / VAL.BUFFETT）仅标注于 flags.low_freq_pit_dup，"
        "本次未改 L1 分位数值；修 L1 属破坏性变更，须递增 params_hash / model_version 全量重算。"
        "⚠ 参数层前视：state_bands 由标定区间 score 分布 P20/P40/P60/P80 冻结（§8.1.1），"
        "回算历史档位时标签非当时可知；样本不含 2026。"
    )
    return {
        "dimension_code": DIM, "dimension_name": "估值",
        "question": "贵不贵（绝对估值，锚 = 全市场 PE-TTM 中位数分位）",
        "polarity": "high_risk", "members": MEMBERS,
        "block_agg": None,
        "state_bands": bands, "state_labels": LABELS,
        "params": PARAMS, "params_hash": ph,
        "is_active": True, "remark": remark,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }


def _report(results: dict) -> None:
    for m, r in results.items():
        if not r.get("n"):
            print(f"[{m}] 无标定样本")
            continue
        print(f"\n[{m}] 样本 {r['n']} 日（{r['start']} ~ {r['end']}）"
              f"  eligible={len(r['eligible'])} 个")
        print(f"  score 分布 P0/20/40/60/80/100 = "
              f"{[r['pctl'][str(q)] for q in (0, 20, 40, 60, 80, 100)]}")
        print(f"  冻结切点 state_bands = {r['state_bands']}  "
              f"样本内各档占比 = {r['shares']}（应各约 0.20）")
        print(f"  coverage 均值 {r['coverage_mean']} / 门禁通过率 {r['gate_pass_ratio']}")
        print(f"  CN 口径约束恒不出档 = {r['cn_state_disabled']}  "
              f"低频 PIT 成员 = {LOW_FREQ}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只读打印标定报告")
    ap.add_argument("--write", action="store_true", help="写 regime.dimension_dict")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe 或 --write")

    with get_conn() as conn:
        results = {m: calibrate(conn, m) for m in ("CN", "HK")}
        if args.write:
            row = build_dict_row(results)
            bulk_upsert(conn, "regime.dimension_dict", [row],
                        conflict_cols=["dimension_code"], skip_null_updates=True)
            print(f"✅ 写入 regime.dimension_dict {DIM}，params_hash={row['params_hash']}")
    if args.probe:
        _report(results)
        print("\n⚠ 参数层前视：切点由全历史标定并冻结，回算历史档位时标签非当时可知（§8.1.1）。")


if __name__ == "__main__":
    main()
