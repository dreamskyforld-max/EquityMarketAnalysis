#!/usr/bin/env python3
"""`DIM.SENTIMENT`（情绪）字典标定：冻结成员有效起点 + 写 `regime.dimension_dict`。

方案 A（2026-11-06 定案，见 `doc/market_state_data_architecture.md` §5.3）：
  内容忍数据覆盖率的现实边界，**只用一个跨期固定的核心成员集算分**，避免成员
  逐个加入（2018 换手 / 2023 基金 / 2026 评级）造成同一条 score 曲线跨期不可比。

  · 进 score 的核心成员固定 3 个（2018-01-02 起全程有值）：
      FLOW.TURNOVER_RATIO（交易行为块）+ SENT.IPO_COUNT_30D / IPO_BREAK_RATE_90D（一级供给块）
  · 诊断成员（flags_only，不进 score / dispersion，只写 contrib / flags）：
      SENT.FUND_ISSUANCE_60D / FUND_MAX_60D（2023 起）、
      FORECAST.RATING_BULL_PCT（2026 起，raw 99.23~99.35 近似常数）、
      FLOW.SHORT_SELLING_RATIO（HK 2026-06 起，仅 103 行）
  · `member_valid_from` / `member_confident_from`：按 (market, code) 从库内
    `indicator_value.percentile` 非 NULL 观测数取第 250 / 750 个日期冻结，写进
    `params` 并纳入 `params_hash`。生产者只按字典日期判定，**不动态读 L1
    extra.n_sample**（情绪族该键未落地），因此 PIT 安全、不触发 L1 重算。
  · `state_bands_calibrated = false` → 全时段不出档（不给确定结论）。
  · HK 保留「单成分代理分」：核心集在 HK 只有换手一个成员，score = 其对齐分位，
    标 `single_block_proxy=true`、恒不出档、confidence 低，禁止与 CN 横比。

限制（写进 remark）：离线预检显示核心成员一阶差分相关中位约 0.045、PC1 解释率
0.298，低于 §5.1 的同质块判据（0.50 / 0.60）；本项目无「情绪亢奋」外部效标，
该维度分只能作内部描述量，不得作强弱判断或预测依据。

用法：
    .venv/bin/python3 tools/calibrate_sentiment.py --probe    # 只读报告
    .venv/bin/python3 tools/calibrate_sentiment.py --write    # 写字典一行
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

DIM = "DIM.SENTIMENT"
MIN_OBS = 250          # §2.4 规则 1：短样本分位不可信
CONFIDENT_OBS = 750    # §2.4 规则 6：3 × min_periods
DATA_START = datetime.date(1990, 1, 1)

CORE_CODES = [
    "FLOW.TURNOVER_RATIO",
    "SENT.IPO_COUNT_30D",
    "SENT.IPO_BREAK_RATE_90D",
]
HK_PROXY = "FLOW.TURNOVER_RATIO"
ALL_CODES = CORE_CODES + [
    "SENT.FUND_ISSUANCE_60D", "SENT.FUND_MAX_60D",
    "FORECAST.RATING_BULL_PCT", "FLOW.SHORT_SELLING_RATIO",
]

MEMBERS = [
    {"code": "FLOW.TURNOVER_RATIO",      "sign":  1, "weight": 0.50, "block": "交易行为"},
    {"code": "SENT.IPO_COUNT_30D",       "sign":  1, "weight": 0.25, "block": "一级供给"},
    {"code": "SENT.IPO_BREAK_RATE_90D",  "sign": -1, "weight": 0.25, "block": "一级供给"},
    {"code": "SENT.FUND_ISSUANCE_60D",   "sign":  1, "weight": 0.00, "block": "诊断", "flags_only": True},
    {"code": "SENT.FUND_MAX_60D",        "sign":  1, "weight": 0.00, "block": "诊断", "flags_only": True},
    {"code": "FORECAST.RATING_BULL_PCT", "sign":  1, "weight": 0.00, "block": "诊断", "flags_only": True},
    {"code": "FLOW.SHORT_SELLING_RATIO", "sign": -1, "weight": 0.00, "block": "诊断", "flags_only": True},
]
BLOCK_AGG = {"交易行为": "mean", "一级供给": "mean"}
STATE_BANDS = {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]}  # 占位，未标定
LABELS = ["极低迷", "偏低迷", "中性", "偏亢奋", "极亢奋"]
LOW_COMMON_FACTOR_PC1 = 0.298  # 只读预检实测（923 日重叠样本）


def _frozen_dates(conn, market: str) -> dict:
    """按 (market, code) 冻结「第 250 / 750 个有效 percentile」的日期。

    带实体过滤 + 时间下界（资源规范 R7）；结果最多 7 列 × 6600 行，内存可忽略。
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
        "coverage_min": 0.6,          # §2.4 通用参数；本维只作记录，不作门禁
        "dispersion_max": 30,         # §2.4 通用参数（种子物化形态）
        "dispersion_core_max": 30,    # 核心 3 成员原始 std 阈值，不做 sqrt(n/10) 归一化
        "dispersion_scope": "core_only",
        "weight_cap": 0.25,           # 两级块平均下不生效，仅登记
        "min_periods": MIN_OBS,
        "quality_sample": CONFIDENT_OBS,
        "winsor": [2, 98],            # 预留键，不参与计算
        "core_coverage_min": 1.0,     # 核心集必须全有效才产 score
        "flag_gap": 15.0,             # 块间结构性背离判据
        "state_bands_calibrated": False,
        "flags_shape": "dict",
        "hk_proxy_member": HK_PROXY,
        "availability_tier_core": "core3_fixed",
        "availability_tier_hk": "hk_single_proxy",
        "low_common_factor_pc1": LOW_COMMON_FACTOR_PC1,
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
        "方案 A（2026-11-06）：固定核心 3 成员（换手 + IPO 数 + 破发率）→ 2 块两级等权；"
        "基金 2 / 评级乐观度 / 港股卖空为 flags_only 诊断成员，不进 score / dispersion。"
        "member_valid_from / member_confident_from 按 (market, code) 冻结第 250 / 750 个有效分位日期，"
        "生产者只按字典判定、不依赖 L1 extra.n_sample（情绪族未落地），PIT 安全且纳入 params_hash。"
        "coverage 仍按全体 members[] 联 market_scope 计（CN 6 / HK 2，仅描述量）；"
        "产分门禁改用 flags.core_coverage（核心集全有效）与 dispersion_core（原始 std，不归一化）。"
        "state_bands_calibrated=false → 全时段 state 恒 NULL；confidence 按冻结的 750 达标日分档。"
        "HK 核心集只有换手 → 单成分代理分，flags.single_block_proxy=true、恒不出档、confidence 低，"
        "禁止与 CN 横比或拼接成一条曲线。"
        "⚠ 限制：核心成员一阶差分相关中位约 0.045、PC1 解释率 0.298（低于 §5.1 的 0.50 / 0.60 同质块判据），"
        "且本项目无情绪外部效标；该 score 只能作内部描述量，不得作强弱判断或预测依据。"
        "⚠ members[].weight 是两级块平均的记录值，生产者按 block 两级平均，不得按 weight 单层加权。"
    )
    return {
        "dimension_code": DIM, "dimension_name": "情绪",
        "question": "人在追还是怕（意愿；高分=越亢奋，描述型，内部读数）",
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
        print(f"\n===== {market} 成员有效起点（冻结）=====")
        for code, v in sorted(results["detail"][market].items()):
            mark = "核心" if code in CORE_CODES else "诊断"
            print(f"  {code:<32} {mark}  n={v['n']:<5} {v['first']}~{v['last']} "
                  f"250日={v['valid'] or '-':<12} 750日={v['confident'] or '-'}")
    cn = results["member_valid_from"]["CN"]
    cc = results["member_confident_from"]["CN"]
    core_valid = [cn[c] for c in CORE_CODES if c in cn]
    core_conf = [cc[c] for c in CORE_CODES if c in cc]
    if len(core_valid) == len(CORE_CODES):
        print(f"\nCN 可发布起点（核心 3 全部达标）: {max(core_valid)}")
    else:
        print("\nCN 核心成员未全部达到 250 观测 → 不产出维度行")
    if len(core_conf) == len(CORE_CODES):
        print(f"CN confidence 可升级起点（核心 3 全部 750 达标）: {max(core_conf)}")
    hk = results["member_valid_from"]["HK"].get(HK_PROXY)
    print(f"HK 代理分起点（{HK_PROXY}）: {hk or '不可产出'}")


def main():
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


if __name__ == "__main__":
    main()
