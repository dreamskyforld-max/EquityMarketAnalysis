#!/usr/bin/env python3
"""`DIM.TREND`（趋势）字典标定：冻结单锚有效起点 + 写 `regime.dimension_dict`。

落地口径（2026-11-06 定案，见 `doc/market_state_data_architecture.md` §5.6 v1.19）：

  · 原设计契约用「固定 K=3 把 t 统计量线性压到 0–100」且 `NO_PERCENTILE`
    只读 raw_value。实测证伪：CN 8505 日里 1991/1992/2007/2015 分别有
    100%/75%/100%/41% 的交易日顶格满分，HK 一半交易日 <20 分 → 绝对刻度
    跨期不可比，且 K 需按市场标定（t 的 P95：CN 3.05 / HK 1.92）。
  · 改为 **t 作 raw_value、其 expanding 分位作 score**：保留 σ 归一化
    （CN σ 年均值 0.114~0.832 漂移 7 倍，确实需要自适应），把固定刻度换成
    相对刻度，零标定参数；`raw_value` 仍可读「几个标准差」。
  · 三个 `TREND.*` 都是标准 L1 原子（raw_value + percentile），回到 §2.4
    通用管线：members[] 读 percentile，不再有 NO_PERCENTILE 破例、
    sign=null / state_bands=null / 自定义 blocks 形状等特殊分支。
  · 单锚 = `TREND.STRENGTH_T.percentile`；`TSMOM_12M` / `MA200_DEV` 为
    weight=0 的方向腿与校验项，只写 contrib / blocks / flags，不进 score。
  · `state_bands_calibrated = False` → 全时段 state 恒 NULL（方向状态归
    BIZ.MARKET_REGIME，避免两层各有一套状态）；单锚 dispersion 恒 NULL。
  · member_valid_from / member_confident_from 按 (market, code) 冻结第 250 /
    750 个有效 percentile 日期，写进 params 并纳入 params_hash，PIT 安全。

⚠ 前置：必须先跑 `tools/recalc_trend_percentile.py --write`，否则 3 个原子
   没有 ≥250 个有效分位 → 冻结日期为 None → 生产者整维不产行。

用法：
    .venv/bin/python3 tools/calibrate_trend.py --probe    # 只读报告
    .venv/bin/python3 tools/calibrate_trend.py --write    # 写字典一行
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

DIM = "DIM.TREND"
MIN_OBS = 250          # §2.4 规则 1
CONFIDENT_OBS = 750    # §2.4 规则 6：3 × min_periods
DATA_START = datetime.date(1990, 1, 1)

ANCHOR = "TREND.STRENGTH_T"
ALL_CODES = [ANCHOR, "TREND.TSMOM_12M", "TREND.MA200_DEV"]

MEMBERS = [
    {"code": ANCHOR,             "sign": 1, "weight": 1.0, "block": "强度锚"},
    {"code": "TREND.TSMOM_12M",  "sign": 1, "weight": 0.0, "block": "方向腿"},
    {"code": "TREND.MA200_DEV",  "sign": 1, "weight": 0.0, "block": "校验"},
]
BLOCK_AGG = {"强度锚": "mean", "方向腿": "mean", "校验": "mean"}
STATE_BANDS = {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]}  # 占位，未标定（恒不出档）
LABELS = ["极弱", "偏弱", "中性", "偏强", "极强"]


def _frozen_dates(conn, market: str) -> dict:
    """按 (market, code) 冻结「第 250 / 750 个有效 percentile」的日期。

    带实体过滤 + 时间下界（资源规范 R7）；结果最多 3 列 × 8500 行，内存可忽略。
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
        "dispersion_max": 30,         # 单锚无 dispersion，仅登记
        "weight_cap": 0.25,           # 单锚下不生效，仅登记
        "min_periods": MIN_OBS,
        "quality_sample": CONFIDENT_OBS,
        "winsor": [2, 98],            # 预留键，不参与计算
        "core_coverage_min": 1.0,     # 锚必须有效才产 score
        "state_bands_calibrated": False,
        "no_state": True,
        "flags_shape": "dict",
        "anchor_code": ANCHOR,
        "availability_tier": "trend_single_anchor",
        "sigma_win": 252,
        "tsmom_variant": "price",
        "index_code": {"CN": "SH.000001", "HK": "HK.800000"},
        "trend_asof_start": {"CN": "1993-01-01", "HK": "2013-08-20"},
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
        "趋势强度单锚（2026-11-06 v1.19）：score = TREND.STRENGTH_T 的 expanding 分位，"
        "STRENGTH_T = |TSMOM_12M| / σ_252（252 交易日同窗口配对，价格收益不减 rf）。"
        "撤销原设计契约的固定 K=3 线性刻度与 NO_PERCENTILE 破例：实测 CN 1991/1992/2007/2015 "
        "分别有 100%/75%/100%/41% 的交易日顶格满分、HK 50% 交易日 <20 分，"
        "绝对刻度跨期不可比；K 若按市场标定则为 CN 3 / HK 2（t 的 P95）。"
        "改用相对刻度后零标定参数，raw_value 仍保留「几个标准差」的可读语义。"
        "三个 TREND.* 均为标准 L1 原子（raw + percentile），members[] 读 percentile，"
        "不再有 sign=null / state_bands=null / 自定义 blocks 形状等特殊分支；"
        "blocks 按 §2.3.3 标准形状 {块名:{score,coverage}} 落库。"
        "TSMOM_12M / MA200_DEV 为 weight=0 的方向腿与校验项，只写 contrib / blocks / flags，"
        "不进 score；L3 BIZ.MARKET_REGIME 方向判定直接读这两个原子的 raw_value，"
        "DIM.TREND.score 不参与方向判定、不进温度 / 预算 / 信号。"
        "L1 输入侧护栏 TREND_ASOF_START：CN 1993-01-01（1991-92 上证开市初期 σ 失真、t 中位 10~12），"
        "HK 2013-08-20（恒指实际起点）；护栏前 raw_value 置 NULL，不进 expanding 参考分布。"
        "HK 需 252 交易日预热，实际可发布起点约 2014-08；CN/HK 样本长度不对称（36 年 / 12 年），"
        "禁止横比、禁止拼接成一条曲线。"
        "member_valid_from / member_confident_from 按 (market, code) 冻结第 250 / 750 个有效分位"
        "日期，PIT 安全、不依赖 L1 extra.n_sample、纳入 params_hash。"
        "state_bands_calibrated=false → 全时段 state 恒 NULL；单锚 dispersion / 门禁恒 NULL，"
        "confidence 最高「中」。⚠ 本维 score 是描述型强度读数，不是交易或预测信号。"
        "⚠ members[].weight 是记录值，生产者按 params.anchor_code 取单锚，不按 weight 加权。"
    )
    return {
        "dimension_code": DIM, "dimension_name": "趋势",
        "question": "指数往哪走 / 有多陡（高分=趋势越陡，与多空方向解耦）",
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
            mark = "锚" if code == ANCHOR else "校验"
            print(f"  {code:<22} {mark}  n={v['n']:<5} {v['first']}~{v['last']} "
                  f"250日={v['valid'] or '-':<12} 750日={v['confident'] or '-'}")
        valid = results["member_valid_from"][market].get(ANCHOR)
        conf = results["member_confident_from"][market].get(ANCHOR)
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
