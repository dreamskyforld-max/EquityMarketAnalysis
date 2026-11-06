#!/usr/bin/env python3
"""L3 业务指标生产者 —— 单文件，对齐 `build_dimension_value.py` 的 L2 模式。

扩展性做法（2026-11-06 二次整改，撤销上一版的注册表 / DAG 式封装）：
  · 一个文件，按 `biz_code` 用 `if/elif` 分派，与 L2 `build_all_dimensions` 同款；
  · 只把**真通用**的部分抽成公共函数：`load_business_dict` /
    `active_business_codes` / `seed_business_dict` / `write_business_rows` /
    `build_all_business`；
  · 各指标专属逻辑仍是独立的 `build_<name>_business()` + `<name>_*` / `<NAME>_*`
    前缀的常量与纯函数。取数、公式、重算粒度、参数来源都由指标自己决定，
    编排层**不做任何假设**（因此不阻挡标定类参数 / external 源 / 同层依赖）；
  · `BUSINESS_ORDER` 只是一个显式顺序表（不是 DAG）：L3 固定 4 个指标，
    用 tuple 声明依赖先后即可，无需拓扑排序。

命名纪律：通用公共函数用通用名；指标专属一律 `regime_` / `REGIME_` 前缀，
禁止 `compute` / `raw_state` / `state_machine` / `PARAMS` 等无归属名称。
唯一例外是落库 `rows` 的键必须等于 `regime.business_value` 表列名（DDL 约束）。

新增指标 = 在本文件加 `build_<name>_business()` + `<name>_build_dict_row()` /
`<name>_upstream_hashes()`，并在 `BUSINESS_ORDER`、`seed_business_dict`、
`build_all_business` 三处各加一个分支 / 一行，再写字典种子。

日频接线：`market_state_daily.run()` 在 L2 写出 `dimension_value` **之后**调用
`build_all_business()`（market_scheduler 工作日 18:45 进程内调用）；也可独立
CLI 手动重算。规模 10742 行 / 峰值约 78MB / 约 2s，与 L2 同处常驻 scheduler
进程内；若将来要严格满足 `doc/compute_task_resource_standard.md` R4/R5 的
「重计算任务独立 unit」，需由运维另建 oneshot + timer。写入每 2000 行一批
commit（R6）；各指标查询自行带实体过滤 / 时间下界（R7）。

用法：
    python3 build_business_value.py --seed
    python3 build_business_value.py --probe
    python3 build_business_value.py --write
    python3 build_business_value.py --write --biz BIZ.MARKET_REGIME
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import resource
import sys
from typing import Any

import numpy as np
import pandas as pd

from db import bulk_upsert, get_conn, read_sql

# ── 通用常量 ────────────────────────────────────────────────────────────────
BUSINESS_DICT_TABLE = "regime.business_dict"
BUSINESS_VALUE_TABLE = "regime.business_value"
BUSINESS_CONFLICT_COLS = ["biz_code", "market", "trade_date"]
BUSINESS_MARKETS = ("CN", "HK")
WRITE_BATCH = 2000

# 显式执行顺序：`BIZ.RISK_BUDGET` 读 `MARKET_REGIME.state` 与
# `SYSTEMIC_STRESS.level`，必须排在两者之后。不要改成 ORDER BY biz_code。
BUSINESS_ORDER = ("BIZ.MARKET_REGIME", "BIZ.MARKET_TEMPERATURE",
                  "BIZ.SYSTEMIC_STRESS", "BIZ.RISK_BUDGET")


# ── 通用公共函数（对所有指标一致，稳定不变）────────────────────────────────
def _parse_jsonb(value):
    """JSONB 列 psycopg2 可能返回 str 或已解析对象（L2 `_parse_jsonb` 同款）。"""
    return json.loads(value) if isinstance(value, str) else value


def active_business_codes(conn, only_biz=None) -> list[str]:
    """启用的指标，按 BUSINESS_ORDER 显式顺序返回；未实现者由分派处跳过。"""
    with conn.cursor() as cur:
        cur.execute("SELECT biz_code FROM " + BUSINESS_DICT_TABLE +
                    " WHERE is_active=TRUE")
        enabled = {r[0] for r in cur.fetchall()}
    wanted = set(only_biz) if only_biz else None
    return [c for c in BUSINESS_ORDER
            if c in enabled and (wanted is None or c in wanted)]


def load_business_dict(conn, biz_code: str) -> dict[str, Any]:
    """读字典并做结构校验；**params 以库为准**，不比对代码种子。

    与 L2 `load_dimension_dict` 同款：标定脚本（`tools/calibrate_*.py`）写出的
    params / 切点属于字典真源，编排层不得因「与代码常量不同」而拒绝运行。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT inputs, params, output_schema, params_hash FROM " +
            BUSINESS_DICT_TABLE + " WHERE biz_code=%s AND is_active=TRUE",
            (biz_code,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"{biz_code} 无启用的 business_dict 种子，请先跑 --seed")
    inputs, params, schema, params_hash = (
        _parse_jsonb(row[0]), _parse_jsonb(row[1]), _parse_jsonb(row[2]), row[3])
    if not isinstance(inputs, list) or not isinstance(params, dict) or not params_hash:
        raise RuntimeError(f"{biz_code} 字典 inputs / params / params_hash 结构异常")
    return {"biz_code": biz_code, "inputs": inputs, "params": params,
            "output_schema": schema, "params_hash": params_hash}


def seed_business_dict(conn, biz_code: str) -> dict[str, Any]:
    """写一行字典种子；标定类指标后续由各自的 calibrate 工具维护。"""
    if biz_code == REGIME_BIZ_CODE:
        row = regime_build_dict_row(regime_upstream_hashes(conn))
    else:
        raise RuntimeError(f"{biz_code} 尚无字典种子生成器")
    bulk_upsert(conn, BUSINESS_DICT_TABLE, [row],
                conflict_cols=["biz_code"], skip_null_updates=True)
    conn.commit()
    print(f"✅ 写入 {BUSINESS_DICT_TABLE} {biz_code}，params_hash={row['params_hash']}")
    return row


def write_business_rows(conn, rows: list[dict[str, Any]], write: bool) -> None:
    """通用落库：主键 UPSERT、分批 commit、只写变化行（幂等，R6）。"""
    if not write or not rows:
        return
    for i in range(0, len(rows), WRITE_BATCH):
        bulk_upsert(conn, BUSINESS_VALUE_TABLE, rows[i:i + WRITE_BATCH],
                    conflict_cols=BUSINESS_CONFLICT_COLS,
                    skip_null_updates=False, only_if_changed=True)
        conn.commit()


# ── `BIZ.MARKET_REGIME` 专属常量（仅用于生成种子 / 声明接线）────────────────
REGIME_BIZ_CODE = "BIZ.MARKET_REGIME"
REGIME_DIM_TREND = "DIM.TREND"
REGIME_DIM_BREADTH = "DIM.BREADTH"
REGIME_ATOM_MOM = "TREND.TSMOM_12M"
REGIME_ATOM_MA = "TREND.MA200_DEV"

REGIME_DEFAULT_SCORE_GATE = 30
REGIME_DEFAULT_CONFIRM_DAYS = 5
REGIME_DEFAULT_MIN_DWELL = 20

REGIME_PARAMS = {
    "score_gate": REGIME_DEFAULT_SCORE_GATE,
    "confirm_days": REGIME_DEFAULT_CONFIRM_DAYS,
    "min_dwell": REGIME_DEFAULT_MIN_DWELL,
    "direction_atom": REGIME_ATOM_MOM,
    "strength_dim": REGIME_DIM_TREND,
    "turn_risk_rule": "sign_ma_ne_sign_mom",
    "start_rule": "inner_join_dim_trend",
}

REGIME_INPUTS = [
    {"kind": "atom", "code": REGIME_ATOM_MOM, "role": "direction"},
    {"kind": "atom", "code": REGIME_ATOM_MA, "role": "evidence_only"},
    {"kind": "dimension", "code": REGIME_DIM_TREND, "role": "strength_gate_and_score"},
    {"kind": "dimension", "code": REGIME_DIM_BREADTH, "role": "detail_only"},
]

REGIME_OUTPUT_SCHEMA = {
    "level_semantics": "1=下行 / 2=震荡 / 3=上行（升序=越偏多；仅为序数，不构成方向预测）",
    "score_semantics": "趋势强度 0-100，与方向解耦；直接引用 DIM.TREND.score（expanding 分位读数，非原始 t 值、非方向概率）",
    "state_semantics": "描述性趋势状态（滞后），不是方向预测；下游 direction 约束默认关闭",
    "detail_keys": ["candidate", "evidence", "breadth_confirmation", "trend_state", "turn_risk"],
}

REGIME_REMARK = (
    "v2（2026-11-06 实测整改）：方向=sign(TSMOM_12M)、强度门槛=DIM.TREND.score>30，"
    "零待标定参数、跨市场同定义；起点 inner join DIM.TREND 有效行。"
    "撤销 v1 的 DEADBAND（按市场标定）/ δ×σ 双死区与两腿合取：实测两腿 corr 0.75/0.77、"
    "非零同号 87%/90%，δ=0.25 仅挡 18~23% 交易日。撤销 (DEADBAND,N,M) 三维收益网格标定："
    "HK 实测 3001 交易日（12.2 年，非 v1 假设的 1800 日/7.5 年）、N5M20 下切换 38 次（非 10-30 次），"
    "『切换<20 次沿用 CN 参数』分支不会触发。前瞻收益实测：上行/下行分不开"
    "（CN f120 +7.2%/+7.6%，HK +1.2%/+3.4%，下行反而最高），趋势 vs 震荡转折分离稳定"
    "（CN +7.4%/-0.9%，HK +2.2%/-0.9%）；故 state 不得用作方向性 cap。"
    "⚠ 前瞻收益为样本内重叠窗口、未做 block bootstrap / 扣成本，仅描述性，非可交易信号。"
    "params 纳入 upstream_dim_trend_params_hash：§5.6 口径变化自动触发本指标全量重算。"
)

REGIME_LEVEL = {"下行": 1, "震荡": 2, "上行": 3}
REGIME_BW_DIR = {"极广": 1, "偏广": 1, "中性": 0, "偏窄": -1, "极窄": -1}
REGIME_ST_DIR = {"上行": 1, "震荡": 0, "下行": -1}


# ── `BIZ.MARKET_REGIME` 专属函数 ───────────────────────────────────────────
def regime_upstream_hashes(conn) -> dict[str, str]:
    """本指标依赖的上游口径指纹。"""
    with conn.cursor() as cur:
        cur.execute("SELECT params_hash FROM regime.dimension_dict "
                    "WHERE dimension_code=%s", (REGIME_DIM_TREND,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"{REGIME_DIM_TREND} 字典不存在或 params_hash 为空，请先跑 "
                           "tools/calibrate_trend.py --write")
    return {REGIME_DIM_TREND: row[0]}


def regime_build_dict_row(upstream_hashes: dict[str, str]) -> dict[str, Any]:
    """生成 business_dict 种子；字段取值须保持稳定，否则 params_hash 变化触发全量重算。"""
    params = dict(REGIME_PARAMS)
    params["upstream_dim_trend_params_hash"] = upstream_hashes[REGIME_DIM_TREND]
    row = {
        "biz_code": REGIME_BIZ_CODE,
        "biz_name": "市场方向",
        "kind": "state_machine",
        "inputs": REGIME_INPUTS,
        "params": params,
        "output_schema": REGIME_OUTPUT_SCHEMA,
        "is_active": True,
        "remark": REGIME_REMARK,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }
    payload = {k: row[k] for k in ("biz_code", "kind", "inputs", "params", "output_schema")}
    row["params_hash"] = hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return row


def regime_input_codes(business_config: dict[str, Any]) -> dict[str, str]:
    """按 `inputs[].role` 取接线编码 —— 输入清单是唯一真相源。"""
    return {item["role"]: item["code"] for item in business_config["inputs"]}


def regime_raw_state(regime_tsmom, regime_strength_score, regime_score_gate):
    """当日原始状态：方向 sign(TSMOM) × 强度门槛；缺输入返回 None（不发行）。"""
    if (regime_tsmom is None or regime_strength_score is None
            or pd.isna(regime_tsmom) or pd.isna(regime_strength_score)):
        return None
    if float(regime_strength_score) <= regime_score_gate:
        return "震荡"
    if float(regime_tsmom) > 0:
        return "上行"
    if float(regime_tsmom) < 0:
        return "下行"
    return "震荡"


def regime_turn_risk(regime_ma, regime_tsmom) -> bool:
    """MA200 位置与 12 月动量方向背离（零参数，实测 CN/HK f120 -2.3%/-2.7%）。"""
    if (regime_ma is None or regime_tsmom is None
            or pd.isna(regime_ma) or pd.isna(regime_tsmom)):
        return False
    return bool(np.sign(float(regime_ma)) * np.sign(float(regime_tsmom)) == -1)


def regime_breadth_confirmation(regime_bw, regime_state: str) -> str:
    """宽度确认只写 detail，不调 state / confidence。"""
    if regime_bw is None or (isinstance(regime_bw, float) and bool(np.isnan(regime_bw))):
        return "不可用"
    d = REGIME_BW_DIR.get(str(regime_bw))
    if d is None or d == 0 or REGIME_ST_DIR[regime_state] == 0:
        return "中性"
    return "一致" if d == REGIME_ST_DIR[regime_state] else "背离"


def regime_state_machine(regime_raws, regime_dates, regime_confirm_days,
                         regime_min_dwell):
    """逐日递推的三态状态机（§6.1 v2，B2 交互顺序写死）。"""
    state = since = status = candidate = None
    dwell = streak = 0
    out = []
    for i, raw in enumerate(regime_raws):
        if state is None:
            state, since, dwell, status = raw, regime_dates[i], 1, "待确认"
            streak, candidate = 0, None
        else:
            dwell += 1
            if raw == state:
                streak, candidate = 0, None
                if dwell >= regime_confirm_days:
                    status = "已确认"
            else:
                streak = streak + 1 if raw == candidate else 1
                candidate = raw
                if dwell >= regime_min_dwell and streak >= regime_confirm_days:
                    state, since, dwell, status = raw, regime_dates[i], 1, "已确认"
                    streak, candidate = 0, None
        out.append({
            "state": state, "since": since, "dwell": dwell, "status": status,
            "candidate": candidate, "streak": streak,
        })
    return out


def regime_load_frame(conn, market: str,
                      business_config: dict[str, Any]) -> pd.DataFrame:
    """本指标专属取数；起点由 `strength_gate_and_score` 维度有效行决定。"""
    codes = regime_input_codes(business_config)
    regime_trend = read_sql(conn, """
        SELECT trade_date, score AS regime_trend_score
        FROM regime.dimension_value
        WHERE dimension_code=%s AND market=%s AND score IS NOT NULL
        ORDER BY trade_date
    """, (codes["strength_gate_and_score"], market))
    if regime_trend.empty:
        return regime_trend
    regime_breadth = read_sql(conn, """
        SELECT trade_date, state AS regime_bw_state
        FROM regime.dimension_value
        WHERE dimension_code=%s AND market=%s
        ORDER BY trade_date
    """, (codes["detail_only"], market))
    regime_atoms = read_sql(conn, """
        SELECT indicator_code, trade_date, raw_value
        FROM regime.indicator_value
        WHERE market=%s AND indicator_code IN (%s,%s) AND raw_value IS NOT NULL
        ORDER BY trade_date, indicator_code
    """, (market, codes["direction"], codes["evidence_only"]))
    if not regime_atoms.empty:
        regime_atoms = (regime_atoms
                        .pivot_table(index="trade_date", columns="indicator_code",
                                     values="raw_value", aggfunc="first")
                        .rename(columns={codes["direction"]: "regime_tsmom",
                                         codes["evidence_only"]: "regime_ma"})
                        .reset_index())
    return (regime_trend
            .merge(regime_atoms, on="trade_date", how="left")
            .merge(regime_breadth, on="trade_date", how="left")
            .sort_values("trade_date").reset_index(drop=True))


def regime_compute(regime_frame: pd.DataFrame, market: str,
                   regime_params: dict[str, Any], regime_params_hash: str
                   ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯计算、不连库不写库；所有阈值从 `regime_params` 读。"""
    regime_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    regime_score_gate = regime_params["score_gate"]
    regime_confirm_days = regime_params["confirm_days"]
    regime_min_dwell = regime_params["min_dwell"]

    regime_valid = []
    regime_iter: Any = regime_frame.itertuples(index=False)
    for regime_row in regime_iter:
        regime_raw = regime_raw_state(getattr(regime_row, "regime_tsmom", None),
                                      regime_row.regime_trend_score, regime_score_gate)
        if regime_raw is None:
            continue                       # 缺输入 → 整行不发布（A8）
        regime_valid.append((regime_row, regime_raw))
    if not regime_valid:
        return [], {"rows": 0, "skipped": len(regime_frame)}

    regime_dates = [regime_row.trade_date for regime_row, _ in regime_valid]
    regime_vectors = regime_state_machine(
        [regime_raw for _, regime_raw in regime_valid], regime_dates,
        regime_confirm_days, regime_min_dwell)
    regime_rows: list[dict[str, Any]] = []
    for (regime_row, regime_raw), regime_vector in zip(regime_valid, regime_vectors):
        regime_state = regime_vector["state"]
        regime_turn_risk_flag = regime_turn_risk(
            getattr(regime_row, "regime_ma", None),
            getattr(regime_row, "regime_tsmom", None))
        regime_bw = getattr(regime_row, "regime_bw_state", None)
        regime_ma_raw = getattr(regime_row, "regime_ma", None)
        regime_ma_out = (None if regime_ma_raw is None
                         or bool(np.isnan(float(regime_ma_raw)))
                         else round(float(regime_ma_raw), 6))
        regime_candidate = (
            {"to": regime_vector["candidate"],
             "progress": round(regime_vector["streak"] / regime_confirm_days, 4)}
            if regime_vector["candidate"] else {})
        regime_detail = {
            "trend_state": "trend" if regime_state != "震荡" else "range",
            "turn_risk": regime_turn_risk_flag,
            "candidate": regime_candidate,
            "breadth_confirmation": regime_breadth_confirmation(regime_bw, regime_state),
            "evidence": {
                "tsmom_12m": round(float(regime_row.regime_tsmom), 6),
                "ma200_dev": regime_ma_out,
                "trend_score": round(float(regime_row.regime_trend_score), 2),
                "score_gate": regime_score_gate,
                "raw_state": regime_raw,
            },
        }
        regime_rows.append({
            "biz_code": REGIME_BIZ_CODE, "market": market,
            "trade_date": regime_row.trade_date,
            "score": round(float(regime_row.regime_trend_score), 2),
            "state": regime_state, "level": REGIME_LEVEL[regime_state],
            "since_date": regime_vector["since"], "dwell_days": regime_vector["dwell"],
            "status": regime_vector["status"],
            "confidence": ("低" if (regime_bw is None
                                   or (isinstance(regime_bw, float) and bool(np.isnan(regime_bw)))
                                   or regime_turn_risk_flag) else "中"),
            "detail": regime_detail,
            "model_version": regime_params_hash,
            "updated_at": regime_now,
        })
    regime_share = (pd.Series([x["state"] for x in regime_rows])
                    .value_counts(normalize=True).round(4).to_dict())
    regime_switches = sum(1 for a, b in zip(regime_vectors, regime_vectors[1:])
                          if a["state"] != b["state"])
    regime_stats = {
        "rows": len(regime_rows), "skipped": len(regime_frame) - len(regime_valid),
        "start": regime_dates[0], "end": regime_dates[-1],
        "switches": regime_switches, "share": regime_share,
        "turn_risk_days": sum(1 for x in regime_rows if x["detail"]["turn_risk"]),
    }
    return regime_rows, regime_stats


def build_regime_business(conn, business_config: dict[str, Any],
                          market: str, write: bool) -> dict[str, Any]:
    """`BIZ.MARKET_REGIME` 生产者（对齐 L2 `build_xxx_dimension` 的职责：取数+算+写）。"""
    regime_frame = regime_load_frame(conn, market, business_config)
    regime_rows, regime_stats = regime_compute(
        regime_frame, market, business_config["params"],
        business_config["params_hash"])
    write_business_rows(conn, regime_rows, write)
    del regime_rows                    # 逐市场释放，避免 A+H 同时驻留
    return regime_stats


# ── 分派（与 L2 `build_all_dimensions` 同款的 if/elif）───────────────────────
def build_all_business(conn, markets=BUSINESS_MARKETS, only_biz=None,
                       write=True) -> dict[str, dict[str, Any]]:
    """按 `BUSINESS_ORDER` × markets 分派；`stats` 形状由各指标自定义，只打印不解释。"""
    active = active_business_codes(conn, only_biz)
    stats: dict[str, dict[str, Any]] = {}
    for market in markets:
        for biz_code in active:
            config = load_business_dict(conn, biz_code)
            if biz_code == REGIME_BIZ_CODE:
                business_stats = build_regime_business(conn, config, market, write)
            else:
                print(f"  ⚠ L3 {biz_code} 无生产者，跳过")
                continue
            stats.setdefault(market, {})[biz_code] = business_stats
            print(f"[{'WRITE' if write else 'PROBE'}] {market} {biz_code}: " +
                  " ".join(f"{k}={v}" for k, v in business_stats.items()))
    return stats


def _peak_rss_mb() -> float:
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / (1024 * 1024) if sys.platform == "darwin" else v / 1024


def main() -> int:
    ap = argparse.ArgumentParser(description="L3 业务指标生产者（单文件，L2 风格）")
    ap.add_argument("--seed", action="store_true", help="写 business_dict 种子")
    ap.add_argument("--probe", action="store_true", help="只读计算并打印统计")
    ap.add_argument("--write", action="store_true", help="全量重算并写 business_value")
    ap.add_argument("--biz", nargs="+", default=None, help="只处理指定 biz_code")
    ap.add_argument("--markets", nargs="+", default=list(BUSINESS_MARKETS))
    args = ap.parse_args()
    if not (args.seed or args.probe or args.write):
        ap.error("需指定 --seed / --probe / --write 之一")
    if args.biz:
        unknown = [c for c in args.biz if c not in BUSINESS_ORDER]
        if unknown:
            ap.error("未知的业务指标：" + ", ".join(unknown))

    with get_conn() as conn:
        if args.seed:
            for biz_code in (args.biz or (REGIME_BIZ_CODE,)):
                seed_business_dict(conn, biz_code)
            if not (args.probe or args.write):
                return 0
        if args.probe or args.write:
            build_all_business(conn, markets=args.markets, only_biz=args.biz,
                               write=args.write)
        print(f"峰值 RSS ≈ {_peak_rss_mb():.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
