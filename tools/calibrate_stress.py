#!/usr/bin/env python3
"""`DIM.STRESS`（压力）字典标定：固定核心成员集 + 冻结有效起点 + 写 `regime.dimension_dict`。

落地口径（2026-11-06 定案，见 `doc/market_state_data_architecture.md` §5.5 v1.17）：

  · 数据现实（本机 market_db 实测）：原 21 成员中 4 个 AA 系信用利差只有 19~27 行
    （2026-09-28 起，`RISK.GRADE_SPREAD_AA_MINUS` 分位恒 50.0），`RISK.TERM_SPREAD`
    仅 CN、`FLOW.HIBOR_3M` 仅 HK；HK 本地 RV / 回撤起点是 2013 而非研究稿的 2019。
    信用利差 / HIBOR / VIX 等**水平型**序列的 expanding 分位随年代漂移
    （CREDIT_SPREAD 分位均值 30.3(2011-15) → 5.0(2021-23)；HIBOR 32.1 → 76.6），
    差分型 `*_CHG20` 各时期稳定在 48~53。
  · 聚合实测：原 5 块 21 成员块内相关极低（波动中位 0.18、相关性块 −0.18、
    信用 −0.04、尾部 −0.02），块均值把压力信号压成 ≈50；6 个危机窗口中
    CN 在 2022-10 / 2024-01 几乎无反应。改为**固定核心 9 成员 / 4 块 / 块内与块间等权**
    （CN 8 核心、HK 7 核心 + 1 诊断），危机日 > 全期 P90 的占比由 36%/39% 升到 48%/41%。
  · `RISK.MAXDD_250`（raw 为负回撤，高分位＝浅回撤）与 `RISK.TERM_SPREAD`
    （direction=high_good，倒挂低＝危险）都取 `sign = −1`，修研究稿全 +1 的读反。
  · 其余 12 个成员**移出 `members[]`**（不计 coverage 分母）——否则 AA 系长期无值
    会恒定压低 coverage（`VAL.ERP_Z` 同款，§5.2 v1.2）。`FLOW.HIBOR_3M` 保留为
    HK 的 `flags_only` 诊断，只写 contrib / coverage / flags，不进 score / dispersion。
  · `member_valid_from` / `member_confident_from` 按 (market, code) 冻结第 250 / 750
    个有效 percentile 日期，写进 params 并纳入 params_hash。生产者只按字典日期判定，
    不依赖 L1 `extra.n_sample`（压力族未落地），PIT 安全、不触发 L1 重算。
  · `dispersion` 只作描述量、**不作门禁**（块内成员低相关是设计事实），`state` 恒 NULL
    （`state_bands_calibrated=False`），`confidence` 最高「中」。首版是内部连续读数，
    不进资产配置约束 / `cap`、禁 CN / HK 横比。

零新增 L1：只用现有 `indicator_value.percentile`，不改 `market_state_daily.py`、
不改表结构、不回补历史数据。M2 候选（Amihud / 利率 CHG20 / 5Y 信用利差）与
「已有 L1 的宏观风险量」均不进本次成员集，留作离线效标与加入实验。

用法：
    .venv/bin/python3 tools/calibrate_stress.py --probe    # 只读报告
    .venv/bin/python3 tools/calibrate_stress.py --write    # 写字典一行
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from db import bulk_upsert, get_conn, read_sql  # noqa: E402

DIM = "DIM.STRESS"
MIN_OBS = 250          # §2.4 规则 1
CONFIDENT_OBS = 750    # §2.4 规则 6：3 × min_periods
DATA_START = datetime.date(1990, 1, 1)
RESONANCE_FLOOR = 80   # flags.resonance_cnt：对齐分位 >= 80 的核心成员数

# block="诊断" 的成员为 flags_only，不进 score / dispersion
MEMBERS = [
    {"code": "RISK.RV20",                  "sign":  1, "weight": 0.0, "block": "波动"},
    {"code": "RISK.VIX_CHG20",            "sign":  1, "weight": 0.0, "block": "波动"},
    {"code": "RISK.SYNC_DOWN_20D",        "sign":  1, "weight": 0.0, "block": "相关性"},
    {"code": "BREADTH.AVG_CORR_VW",       "sign":  1, "weight": 0.0, "block": "相关性"},
    {"code": "RISK.CREDIT_SPREAD_CHG20",  "sign":  1, "weight": 0.0, "block": "信用"},
    {"code": "RISK.TERM_SPREAD",          "sign": -1, "weight": 0.0, "block": "信用"},
    {"code": "RISK.EXTREME_DOWN_PCT",     "sign":  1, "weight": 0.0, "block": "尾部"},
    {"code": "RISK.MAXDD_250",            "sign": -1, "weight": 0.0, "block": "尾部"},
    {"code": "FLOW.HIBOR_3M",             "sign":  1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
]
BLOCK_AGG = {"波动": "mean", "相关性": "mean", "信用": "mean", "尾部": "mean"}
STATE_BANDS = {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]}  # 占位，未标定
LABELS = ["极稳", "稳健", "正常", "紧张", "危机"]

ALL_CODES = [m["code"] for m in MEMBERS]
CORE_CODES = [m["code"] for m in MEMBERS if not m.get("flags_only")]

# 移出 members[] 的原成员：短样本 / 水平型漂移 / 跨资产冗余，仅作离线诊断
EXCLUDED_MEMBERS = [
    "RISK.RV60", "RISK.VIX", "RISK.BOND_RV60", "RISK.COPPER_RV60",
    "RISK.GOLD_RV60", "RISK.OIL_RV60", "RISK.SPX_RV60",
    "RISK.CREDIT_SPREAD", "RISK.CREDIT_SPREAD_AA", "RISK.CREDIT_SPREAD_AA_MINUS",
    "RISK.GRADE_SPREAD", "RISK.GRADE_SPREAD_AA_MINUS",
]


def _frozen_dates(conn, market: str) -> dict:
    """按 (market, code) 冻结「第 250 / 750 个有效 percentile」的日期。

    带实体过滤 + 时间下界（资源规范 R7）；结果最多 9 列 × 8800 行，内存可忽略。
    """
    df = read_sql(
        conn,
        "SELECT trade_date, indicator_code FROM regime.indicator_value "
        "WHERE market=%s AND indicator_code=ANY(%s) AND trade_date>=%s "
        "  AND percentile IS NOT NULL ORDER BY indicator_code, trade_date",
        [market, ALL_CODES, DATA_START])
    out = {}
    for code, g in df.groupby("indicator_code"):
        dates = g["trade_date"].tolist()
        out[code] = {
            "valid": dates[MIN_OBS - 1].isoformat() if len(dates) >= MIN_OBS else None,
            "confident": (dates[CONFIDENT_OBS - 1].isoformat()
                          if len(dates) >= CONFIDENT_OBS else None),
            "n": len(dates),
            "first": dates[0].isoformat() if dates else None,
            "last": dates[-1].isoformat() if dates else None,
        }
    return out


def calibrate(conn) -> dict:
    by_market = {m: _frozen_dates(conn, m) for m in ("CN", "HK")}
    return {
        "member_valid_from": {
            m: {c: v["valid"] for c, v in by_market[m].items() if v["valid"]}
            for m in ("CN", "HK")},
        "member_confident_from": {
            m: {c: v["confident"] for c, v in by_market[m].items() if v["confident"]}
            for m in ("CN", "HK")},
        "detail": by_market,
    }


def build_params(results: dict) -> dict:
    return {
        "coverage_min": 0.6,           # §2.4 通用参数；本维只作记录，不作产分门禁
        "dispersion_max": 30,          # §2.4 通用参数（种子物化形态）
        "dispersion_scope": "core_only",
        "dispersion_no_gate": True,    # 块内成员低相关是设计事实，dispersion 只描述
        "weight_cap": 0.25,            # 分块结构不生效，仅登记
        "min_periods": MIN_OBS,
        "quality_sample": CONFIDENT_OBS,
        "winsor": [2, 98],             # 预留键，不参与计算
        "core_coverage_min": 1.0,      # 全部核心成员有效才产行
        "state_bands_calibrated": False,
        "flags_shape": "dict",
        "availability_tier": "stress_core_fixed",
        "empty_block_policy": "drop_from_denominator",
        "resonance_floor": RESONANCE_FLOOR,
        "excluded_members": EXCLUDED_MEMBERS,
        "member_valid_from": results["member_valid_from"],
        "member_confident_from": results["member_confident_from"],
    }


def build_dict_row(results: dict) -> dict:
    params = build_params(results)
    payload = {"members": MEMBERS, "block_agg": BLOCK_AGG,
               "state_bands": STATE_BANDS, "labels": LABELS, "params": params}
    ph = hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    remark = (
        "固定核心集（2026-11-06，§5.5 v1.17）：4 块（波动/相关性/信用/尾部）块内与块间等权，"
        "CN 8 核心 / HK 7 核心 + FLOW.HIBOR_3M(HK) 为 flags_only 诊断。"
        "原 21 成员 5 块模型不可落地：AA 系 4 项仅 19~27 行（GRADE_SPREAD_AA_MINUS 分位恒 50）、"
        "块内相关中位 0.18（相关性块 −0.18）、危机日 > 全期 P90 占比仅 36%/39%；"
        "水平型 CREDIT_SPREAD / HIBOR / VIX 的 expanding 分位随年代漂移。"
        "sign 修正两处：RISK.MAXDD_250 raw 为负回撤（高分位＝浅回撤）、"
        "RISK.TERM_SPREAD direction=high_good（倒挂低＝危险），均取 −1。"
        "其余 12 个成员移出 members[]（不计 coverage 分母，避免 VAL.ERP_Z 同款稀释），"
        "编码登记在 params.excluded_members，只作离线诊断。"
        "member_valid_from / member_confident_from 按 (market, code) 冻结第 250 / 750 个有效分位"
        "日期，PIT 安全、不依赖 L1 extra.n_sample（压力族未落地）、纳入 params_hash。"
        "dispersion 只写描述量不作门禁（块内成员本就低相关）；state_bands_calibrated=false → "
        "全时段 state 恒 NULL；confidence 最高「中」。"
        "⚠ 首版是内部连续读数，不进 BIZ.RISK_BUDGET、不作预测依据；CN / HK 分位基数不同，"
        "禁止横比或拼接成一条曲线。⚠ members[].weight 是记录值，生产者按块两级等权，不按 weight 加权。"
        "⚠ 零新增 L1：只用现有 indicator_value.percentile；M2 候选（市场级 Amihud / HIBOR_CHG20 / "
        "SHIBOR_CHG20 / 5Y 信用利差）与已有 L1 的宏观风险量均不进本次成员集，须先有本版基线再做加入实验。"
    )
    return {
        "dimension_code": DIM, "dimension_name": "压力",
        "question": "系统稳不稳（共振型；高分=压力越大，内部连续读数）",
        "polarity": "high_risk", "members": MEMBERS,
        "block_agg": BLOCK_AGG,
        "state_bands": STATE_BANDS, "state_labels": LABELS,
        "params": params, "params_hash": ph,
        "is_active": True, "remark": remark,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }


def _report(results: dict) -> None:
    for market in ("CN", "HK"):
        print(f"\n===== {market} =====")
        for code, v in sorted(results["detail"][market].items()):
            mark = "核心" if code in CORE_CODES else "诊断"
            print(f"  {code:<32} {mark}  n={v['n']:<5} {v['first']}~{v['last']} "
                  f"250日={v['valid'] or '-':<12} 750日={v['confident'] or '-'}")
        # scope 过滤：TERM_SPREAD 仅 CN、HIBOR_3M 仅 HK，缺源的市场不计入冻结判定
        core = [c for c in CORE_CODES if c in results["detail"][market]]
        valid = [results["member_valid_from"][market].get(c) for c in core]
        conf = [results["member_confident_from"][market].get(c) for c in core]
        print("  可发布起点:", max(valid) if all(valid) else "未达标（不产行）",
              "  confidence 可升级起点:", max(conf) if all(conf) else "未达标")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只读打印标定报告")
    ap.add_argument("--write", action="store_true", help="写 regime.dimension_dict")
    args = ap.parse_args()
    if not (args.probe or args.write):
        ap.error("需指定 --probe 或 --write")

    with get_conn() as conn:
        results = calibrate(conn)
        row = build_dict_row(results)
        if args.write:
            bulk_upsert(conn, "regime.dimension_dict", [row],
                        conflict_cols=["dimension_code"], skip_null_updates=True)
            print(f"✅ 写入 regime.dimension_dict {DIM}，params_hash={row['params_hash']}")
        else:
            print(f"[probe] params_hash 应为 {row['params_hash']}")
    if args.probe:
        _report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
