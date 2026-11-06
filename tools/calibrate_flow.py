#!/usr/bin/env python3
"""`DIM.FLOW`（资金）字典标定：按市场冻结单锚有效起点 + 写 `regime.dimension_dict`。

落地口径（2026-11-06 定案，见 `doc/market_state_data_architecture.md` §5.4）：

  · 数据现实：4 个原成员里北向 2024-08-16 断供、南向仅 2026-04 起 130 天、
    产业资本仅 262 行且分位退化、MARGIN_RATIO 分母（流通市值）2026-10 塌陷。
    改用库内两条最长、同源日更的有符号净额序列作**按市场各自的单锚**：
      CN  FLOW.MARGIN_NET_20D   Δ20 两融余额 / 同期成交额 ×100（2019-01-02 起）
      HK  FLOW.BUYBACK_NET_20D  Σ20 公司回购 / 同期成交额 ×100（2010-01-04 起）
  · 其余 6 个成员降为 flags_only 诊断，不进 score / dispersion，只写 contrib /
    coverage / flags。score = 该市场单锚的对齐分位，两侧都标 single_block_proxy，
    **禁止 CN / HK 横比或拼接成一条曲线**。
  · member_valid_from / member_confident_from 按 (market, code) 冻结第 250 / 750
    个有效 percentile 日期，写进 params 并纳入 params_hash。生产者只按字典日期
    判定，不依赖 L1 extra.n_sample（资金族未落地），PIT 安全、不触发 L1 重算。
  · state_bands_calibrated = False → 全时段 state 恒 NULL；本维是解释维度，
    score 不进 L3、不驱动任何 cap、不作预测依据。

⚠ 前置：必须先跑 `tools/recalc_flow_percentile.py --write` 产出新口径 L1，
   否则核心成员没有 ≥250 个有效分位 → 冻结日期为 None → 生产者整维不产行。

用法：
    .venv/bin/python3 tools/calibrate_flow.py --probe    # 只读报告
    .venv/bin/python3 tools/calibrate_flow.py --write    # 写字典一行
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

DIM = "DIM.FLOW"
MIN_OBS = 250          # §2.4 规则 1
CONFIDENT_OBS = 750    # §2.4 规则 6：3 × min_periods
DATA_START = datetime.date(1990, 1, 1)

PROXY_BY_MARKET = {
    "CN": "FLOW.MARGIN_NET_20D",
    "HK": "FLOW.BUYBACK_NET_20D",
}
ALL_CODES = list(PROXY_BY_MARKET.values()) + [
    "FLOW.NORTHBOUND_NET", "FLOW.MARGIN_RATIO",
    "SENT.INSIDER_NET_SELL_60D", "SENT.UNLOCK_PAST_30D_VALUE",
    "FLOW.SOUTHBOUND_NET", "FLOW.SHORT_SELLING_RATIO",
]

MEMBERS = [
    {"code": "FLOW.MARGIN_NET_20D",  "sign":  1, "weight": 1.0, "block": "杠杆资金净流"},
    {"code": "FLOW.BUYBACK_NET_20D", "sign":  1, "weight": 1.0, "block": "公司净买入/股份供给"},
    {"code": "FLOW.NORTHBOUND_NET",         "sign":  1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "FLOW.MARGIN_RATIO",           "sign":  1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "SENT.INSIDER_NET_SELL_60D",   "sign": -1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "SENT.UNLOCK_PAST_30D_VALUE",  "sign": -1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "FLOW.SOUTHBOUND_NET",         "sign":  1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
    {"code": "FLOW.SHORT_SELLING_RATIO",    "sign": -1, "weight": 0.0,
     "block": "诊断", "flags_only": True},
]
BLOCK_AGG = {"杠杆资金净流": "mean", "公司净买入/股份供给": "mean"}
STATE_BANDS = {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]}  # 占位，未标定
LABELS = ["极流出", "偏流出", "中性", "偏流入", "极流入"]


def _frozen_dates(conn, market: str) -> dict:
    """按 (market, code) 冻结「第 250 / 750 个有效 percentile」的日期。

    带实体过滤 + 时间下界（资源规范 R7）；结果最多 8 列 × 6600 行，内存可忽略。
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
        "dispersion_core_max": 30,    # 单锚无 dispersion，仅登记
        "dispersion_scope": "core_only",
        "weight_cap": 0.25,           # 单锚下不生效，仅登记
        "min_periods": MIN_OBS,
        "quality_sample": CONFIDENT_OBS,
        "winsor": [2, 98],            # 预留键，不参与计算
        "core_coverage_min": 1.0,     # 单锚必须有效才产 score
        "state_bands_calibrated": False,
        "flags_shape": "dict",
        "proxy_member_by_market": PROXY_BY_MARKET,
        "availability_tier": "flow_single_anchor",
        "flow_asof_start": {"CN": "2019-01-02", "HK": "2010-01-04"},
        "regime_shift_2016": True,
        "northbound_discontinued": "2024-08-16",
        "margin_ratio_source_discontinued": True,
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
        "按市场单锚（2026-11-06）：CN 用 FLOW.MARGIN_NET_20D（Δ20 两融余额/同期成交额），"
        "HK 用 FLOW.BUYBACK_NET_20D（Σ20 公司回购/同期成交额）；二者都是有符号净额且已在 L1 "
        "标准化，分位窗口按 §2.4 默认 expanding。其余 6 个成员为 flags_only 诊断，不进 "
        "score / dispersion，只写 contrib / coverage / flags。"
        "原 4 成员模型不可落地：北向 2024-08-16 断供、南向仅 130 天、产业资本 262 行且分位退化、"
        "MARGIN_RATIO 分母流通市值 2026-10 覆盖塌陷（raw 2.58→6.99、分位顶格）。"
        "member_valid_from / member_confident_from 按 (market, code) 冻结第 250 / 750 个有效分位"
        "日期，PIT 安全、不依赖 L1 extra.n_sample、纳入 params_hash。"
        "⚠ HK 回购是「公司部门净买入 / 股份供给收缩」，不是外部资金流入，语义边界不得与跨境净流混同。"
        "⚠ HK 冻结 2010-01-04；2016 后回购常态化（20 日比率 0.1067→0.3394，3.2 倍制度抬升），"
        "expanding 分位会把 2016 后系统性排到高位，只作相对变化读数、不做跨年代水平比较，"
        "写 flags.regime_shift_2016。"
        "⚠ CN 两融表虽已回补齐 2010 起，但 2015-2018 去杠杆段波动与标的池扩张会污染 expanding "
        "分布，L1 护栏 FLOW_ASOF_START=2019-01-02 才进分位，补齐段只作离线对照。"
        "coverage 按全体 members[] 联 market_scope 计（仅描述量），产分门禁改用 "
        "flags.core_coverage（单锚有效）。state_bands_calibrated=false → 全时段 state 恒 NULL；"
        "两侧均标 single_block_proxy / 恒低置信，禁止 CN 与 HK 横比或拼接成一条曲线。"
        "⚠ 本维是解释维度：score 不进 L3、不驱动任何 cap、不作强弱判断或预测依据；sign 为理论推定。"
        "⚠ members[].weight 是记录值，生产者按 proxy_member_by_market 取单锚，不按 weight 加权。"
    )
    return {
        "dimension_code": DIM, "dimension_name": "资金",
        "question": "钱实际进出多少（有符号净额；高分=净流入侧，描述型内部读数）",
        "polarity": "high_risk", "members": MEMBERS,
        "block_agg": BLOCK_AGG,
        "state_bands": STATE_BANDS, "state_labels": LABELS,
        "params": params, "params_hash": ph,
        "is_active": True, "remark": remark,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }


def _report(results: dict) -> None:
    for market, core in PROXY_BY_MARKET.items():
        print(f"\n===== {market} =====")
        for code, v in sorted(results["detail"][market].items()):
            mark = "核心" if code == core else "诊断"
            print(f"  {code:<32} {mark}  n={v['n']:<5} {v['first']}~{v['last']} "
                  f"250日={v['valid'] or '-':<12} 750日={v['confident'] or '-'}")
        valid = results["member_valid_from"][market].get(core)
        conf = results["member_confident_from"][market].get(core)
        print(f"  可发布起点: {valid or '未达标（不产行）'}"
              f"   confidence 可升级起点: {conf or '未达标'}")


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
