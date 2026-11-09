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

命名纪律：通用公共函数用通用名；指标专属一律 `regime_` / `REGIME_`（方向）、
`pattern_` / `PATTERN_`（形态）前缀，禁止 `compute` / `raw_state` /
`state_machine` / `PARAMS` 等无归属名称。
唯一例外是落库 `rows` 的键必须等于 `regime.business_value` 表列名（DDL 约束）。

新增指标 = 在本文件加 `build_<name>_business()` + `<name>_build_dict_row()` /
`<name>_upstream_hashes()`，并在 `BUSINESS_ORDER`、`seed_business_dict`、
`build_all_business` 三处各加一个分支 / 一行，再写字典种子。

已落地指标：
  · `BIZ.MARKET_REGIME`（state_machine，§6.1 v2）；
  · `BIZ.MARKET_PATTERN`（rule_set，§6.2 v1.32）：六维 `score` 先按
    `(market, dimension)` 冻结 ECDF 转成**自身历史分位**再分档，条件合取判形态；
    `state` 恒为当日 raw（单日幂等），稳定性由 `since_date`/`dwell_days`/`status`
    游程标注承载，不用状态机。字典含 12 组冻结 ECDF 与规则表，由
    `tools/calibrate_pattern.py --write` 生成（`--seed` 亦可）；
  · `BIZ.MARKET_DIVERGENCE`（rule_set，§6.3 v1.33）：六维 score 按
    `(market, dimension)` 冻结 P30/P70 转成 L/M/H 档，成对取**序数档位差**
    `delta∈{0,1,2}`，`state` 按归一强度 `Σdelta/(2×n_valid)` 切四档、`score`
    保留命中计数。不使用跨维分位差（违反「六维分不可加减」硬约束）。字典含
    12 组冻结切点、对清单与阈值，由 `tools/calibrate_divergence.py --write`
    生成（`--seed` 亦可）；
  · `BIZ.REVERSAL_RISK`（rule_set，§6.4 v1.34 一期裁剪版）：固定 5 族 × 每族
    1 个 L1 原子，按 `(market, family)` 冻结 P90/P10 判极端，族去重计数后按市场
    冻结的族数切点判三档 + 顶底冲突降档；`state` 恒当日 raw（无状态机），
    稳定性由游程标注承载。一期族计数在生产者内部算，不落 `signal_value`；
    字典含族清单版本 / 切点 / 阈值 / 发布日历 / 原子指纹，由
    `tools/calibrate_reversal.py --write` 生成（`--seed` 亦可）。

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
    python3 build_business_value.py --write --biz BIZ.MARKET_DIVERGENCE
"""
from __future__ import annotations

import argparse
import bisect
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

# 显式执行顺序：四个 L3 均为独立对象；PATTERN 只读方向指标的**当日读数**（不从
# 分派顺序取依赖，故顺序仅用于稳定遍历），不得改成 ORDER BY biz_code。
# v2.1.21：风险预算 BIZ.RISK_BUDGET 整体移出到资产配置功能（第②层 §4.3），本层不再产出。
# v1.32：MARKET_PATTERN 已落地（rule_set）；v1.33：MARKET_DIVERGENCE 已落地；
# v1.34：REVERSAL_RISK 一期裁剪版已落地。四个 L3 均已有生产者。
BUSINESS_ORDER = ("BIZ.MARKET_REGIME", "BIZ.MARKET_PATTERN",
                  "BIZ.MARKET_DIVERGENCE", "BIZ.REVERSAL_RISK")


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
    elif biz_code == PATTERN_BIZ_CODE:
        row = pattern_build_dict_row(pattern_calibrate(conn),
                                     pattern_upstream_hashes(conn))
    elif biz_code == DIVERGENCE_BIZ_CODE:
        row = divergence_build_dict_row(divergence_calibrate(conn),
                                        divergence_upstream_hashes(conn))
    elif biz_code == REVERSAL_BIZ_CODE:
        row = reversal_build_dict_row(reversal_calibrate(conn),
                                      reversal_upstream_hashes(conn))
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


# ── `BIZ.MARKET_PATTERN` 专属常量（仅用于生成种子 / 声明接线）────────────────
PATTERN_BIZ_CODE = "BIZ.MARKET_PATTERN"
PATTERN_FLAG_ATOM = "BREADTH.EQ_VS_CAP_20D"
PATTERN_DIRECTION_BIZ = "BIZ.MARKET_REGIME"

# 六维 → 规则表字母。TREND 只作 evidence，不进 band / 规则。
PATTERN_LETTER_TO_DIM = {
    "B": "DIM.BREADTH", "V": "DIM.VALUATION", "S": "DIM.SENTIMENT",
    "F": "DIM.FLOW", "P": "DIM.STRESS", "T": "DIM.TREND",
}
PATTERN_DIMS = tuple(PATTERN_LETTER_TO_DIM.values())
PATTERN_BAND_LETTERS = ("B", "V", "S", "F", "P")
PATTERN_DIM_TO_LETTER = {v: k for k, v in PATTERN_LETTER_TO_DIM.items()}

PATTERN_DATA_START = datetime.date(1990, 1, 1)     # R7：查询下界
PATTERN_CALIB_END = datetime.date(2025, 12, 31)    # ECDF 标定窗口终点（2026 起样本外）
PATTERN_QUANTILES = 101                             # P0..P100 冻结分位点
PATTERN_BAND_HIGH_U = 0.70                         # 按各维自身历史分位等频三档
PATTERN_BAND_LOW_U = 0.30
PATTERN_MIN_DIMS = 5
PATTERN_CONFIRM_DAYS = 3                           # dwell_days >= 3 → status=已确认
PATTERN_EQ_EXTREME = {"low": 20.0, "high": 80.0}   # L1 原子分位极端判据

# 规则表整体落 params（§6.2：改规则须重标切点并全量重算）。
# 顺序 = 优先级：最具体的合取在前，最宽的单维触发（资金驱动）后置，避免遮蔽。
# 条件语义 strict：band 为 None 时任何涉及该维的条件均不成立。
PATTERN_RULES = [
    {"id": 1, "state": "高位脆弱",
     "all": [{"dim": "V", "band": "H"}, {"dim": "P", "band": "H"},
             {"dim": "S", "band": "H"}]},
    {"id": 2, "state": "全面收缩",
     "all": [{"dim": "B", "band": "L"}, {"dim": "P", "band": "H"},
             {"direction": "下行"}]},
    {"id": 3, "state": "跌势修复",
     "all": [{"dim": "B", "band_ne": "L"}, {"dim": "V", "band": "L"},
             {"direction": "下行"}]},
    {"id": 4, "state": "广泛上行",
     "all": [{"dim": "B", "band": "H"}, {"direction": "上行"}]},
    {"id": 5, "state": "权重主导",
     "all": [{"dim": "B", "band": "L"}, {"direction": "上行"},
             {"flag": "eq_vs_cap_extreme"}]},
    {"id": 6, "state": "情绪驱动",
     "all": [{"dim": "S", "band": "H"}, {"dim": "F", "band_ne": "H"},
             {"dim": "B", "band": "H"}]},
    {"id": 7, "state": "资金驱动",
     "all": [{"dim": "F", "band": "H"}, {"dim": "S", "band_ne": "H"}]},
]
PATTERN_PRIORITY = tuple(r["state"] for r in PATTERN_RULES) + ("常态",)

PATTERN_INPUTS = [
    {"kind": "dimension", "code": "DIM.BREADTH", "role": "band"},
    {"kind": "dimension", "code": "DIM.VALUATION", "role": "band"},
    {"kind": "dimension", "code": "DIM.SENTIMENT", "role": "band"},
    {"kind": "dimension", "code": "DIM.FLOW", "role": "band"},
    {"kind": "dimension", "code": "DIM.STRESS", "role": "band"},
    {"kind": "dimension", "code": "DIM.TREND", "role": "evidence_only"},
    {"kind": "atom", "code": PATTERN_FLAG_ATOM, "role": "flag"},
    {"kind": "business", "code": PATTERN_DIRECTION_BIZ, "role": "direction_axis"},
]

PATTERN_OUTPUT_SCHEMA = {
    "level_semantics": "恒 NULL（形态无序，不设序数）",
    "score_semantics": "恒 NULL（不使用连续分；不是热度 / 强度 / 危险度）",
    "state_semantics": "离散互斥的市场形态标签（8 选 1，无序）= 当日 raw，恒可读；"
                       "status / dwell_days 仅作游程稳定性标注，**不是读取门禁**"
                       "（实测约半数交易日为待确认，按 status 过滤会让指标半数无值）",
    "detail_keys": ["pattern", "dominant_driver", "evidence", "matched_rules",
                    "missing_dims", "candidate"],
}

PATTERN_REMARK = (
    "v1.32（2026-11-07 按实测整改并落地）：六维 score 分位基数不同（绝对 70/30 下 "
    "CN STRESS 高仅 3.05%、CN VALUATION 57.67%、HK FLOW 56.65%），故先按 "
    "(market, dimension) 冻结 ECDF 转自身历史分位 u_i 再分档，符合 §2.4 规则 7 "
    "「先转自身分位」的合法例外（合取而非平均）。规则 2 判据改读 L1 原子 "
    "BREADTH.EQ_VS_CAP_20D.percentile（dimension_value.flags 无该键，§1.1 铁律②）。"
    "TREND 仅 evidence_only（原 8 条规则零引用却是最晚起点）。state 恒当日 raw、"
    "单日幂等，防抖改由 since_date/dwell_days/status 游程标注承载——实测 "
    "switch_confirm_days>=5 会压死稀有形态（0% 形态 1~5 个）。"
    "上游 §5.3/§5.4/§5.5 下游接口已按 ADR B-13 放行「描述性判定消费」，"
    "HK 恒低置信代理分由 confidence + evidence.low_conf_dims 标注，不作决策依据。"
    "state 恒可读，status 只标游程稳定性、不作读取门禁。已知限制：CN 2019-01-10~"
    "2020-01-09 及 2026-11-06 共 258 日缺 DIM.FLOW（strict 退出判定，常态占比偏高），"
    "由 detail.missing_dims 标注；CN 2015-06 / 2018-12 公认区间早于发布起点不可对照。"
    "⚠ ECDF 用截至 2025-12-31 的全历史冻结，存在参数层前视；PIT 敏感性见 "
    "tools/validate_pattern.py。params 纳入六维 params_hash 与规则表快照。"
)

# `SEEDABLE_BUSINESS_CODES` 在全部专属常量定义完毕后统一声明（见 DIVERGENCE 段末）。


# ── `BIZ.MARKET_PATTERN` 专属函数 ───────────────────────────────────────────
def pattern_upstream_hashes(conn) -> dict[str, str]:
    """六维口径指纹；任一维缺失 / 为空即拒绝生成种子。"""
    with conn.cursor() as cur:
        cur.execute("SELECT dimension_code, params_hash FROM regime.dimension_dict "
                    "WHERE dimension_code=ANY(%s)", (list(PATTERN_DIMS),))
        out = {r[0]: r[1] for r in cur.fetchall()}
    missing = [d for d in PATTERN_DIMS if not out.get(d)]
    if missing:
        raise RuntimeError("以下维度字典缺失或 params_hash 为空：" + ", ".join(missing))
    return out


def pattern_calibrate(conn, calib_end=PATTERN_CALIB_END) -> dict[str, Any]:
    """按 (market, dimension) 冻结各自历史 score 的经验分布。

    只取 `trade_date <= calib_end` 的样本；返回 101 分位点 + P30/P70 切点。
    ECDF 是「自身历史分位」的刻度，不是对 score 再取一次 L1 分位。
    """
    frame = read_sql(conn, """
        SELECT market, dimension_code, score
        FROM regime.dimension_value
        WHERE market=ANY(%s) AND dimension_code=ANY(%s)
          AND trade_date>=%s AND trade_date<=%s AND score IS NOT NULL
    """, (list(BUSINESS_MARKETS), list(PATTERN_DIMS), PATTERN_DATA_START, calib_end))
    if frame.empty:
        raise RuntimeError("dimension_value 无可用 score，无法标定形态 ECDF")
    grid = np.linspace(0, 1, PATTERN_QUANTILES)
    ecdf: dict[str, Any] = {}
    for (market, dim_code), group in frame.groupby(["market", "dimension_code"]):
        arr = np.sort(group["score"].to_numpy(dtype=float))
        ecdf.setdefault(market, {})[dim_code] = {
            "n": int(arr.size),
            "quantiles": [round(float(x), 4) for x in np.quantile(arr, grid)],
            "p30": round(float(np.quantile(arr, PATTERN_BAND_LOW_U)), 4),
            "p70": round(float(np.quantile(arr, PATTERN_BAND_HIGH_U)), 4),
        }
    for market in BUSINESS_MARKETS:
        missing = [d for d in PATTERN_DIMS if d not in ecdf.get(market, {})]
        if missing:
            raise RuntimeError(f"{market} 缺维度 ECDF：" + ", ".join(missing))
    return {"calib_end": calib_end.isoformat(), "ecdf": ecdf,
            "n_obs": int(len(frame))}


def pattern_build_dict_row(calib: dict[str, Any],
                           upstream_hashes: dict[str, str]) -> dict[str, Any]:
    """生成字典行；ECDF / 规则表 / 六维指纹全部进 params_hash。"""
    params = {
        "band_high_u": PATTERN_BAND_HIGH_U,
        "band_low_u": PATTERN_BAND_LOW_U,
        "min_dims": PATTERN_MIN_DIMS,
        "confirm_days": PATTERN_CONFIRM_DAYS,
        "dwell_semantics": "publish_day_run_length",
        "missing_semantics": "strict",
        "direction_policy": "inner_join_regime",
        "ecdf_calib_end": calib["calib_end"],
        "ecdf": calib["ecdf"],
        "eq_vs_cap_extreme": PATTERN_EQ_EXTREME,
        "pattern_priority": list(PATTERN_PRIORITY),
        "pattern_rules": PATTERN_RULES,
        "upstream_dim_params_hashes": upstream_hashes,
    }
    row = {
        "biz_code": PATTERN_BIZ_CODE,
        "biz_name": "市场形态",
        "kind": "rule_set",
        "inputs": PATTERN_INPUTS,
        "params": params,
        "output_schema": PATTERN_OUTPUT_SCHEMA,
        "is_active": True,
        "remark": PATTERN_REMARK,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }
    payload = {k: row[k] for k in ("biz_code", "kind", "inputs", "params", "output_schema")}
    row["params_hash"] = hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return row


def pattern_input_roles(business_config: dict[str, Any]) -> dict[str, Any]:
    """按 `inputs[].role` 取接线；band 有多个，其余各一个。"""
    roles: dict[str, Any] = {"bands": []}
    for item in business_config["inputs"]:
        if item["role"] == "band":
            roles["bands"].append(item["code"])
        else:
            roles[item["role"]] = item["code"]
    return roles


def pattern_u_of(quantiles: list[float], value) -> float | None:
    """冻结 ECDF 的 101 分位点 → 自身历史分位 u_i（并列值取区间中点）。"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    v = float(value)
    lo = bisect.bisect_left(quantiles, v)
    hi = bisect.bisect_right(quantiles, v)
    u = (lo + hi) / (2.0 * (len(quantiles) - 1))
    return round(min(1.0, max(0.0, u)), 6)


def pattern_band(cuts: dict[str, float], score) -> str | None:
    """按该维冻结的 P30/P70 切点分档；缺失返回 None（strict，不按中性填充）。"""
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return None
    v = float(score)
    if v >= cuts["p70"]:
        return "H"
    if v <= cuts["p30"]:
        return "L"
    return "M"


def pattern_eval_condition(cond: dict[str, Any], bands: dict[str, str | None],
                           regime_state: str | None,
                           flags: dict[str, bool]) -> bool:
    """单条规则项的求值；缺失 band 一律不成立（strict）。"""
    if "dim" in cond:
        value = bands.get(cond["dim"])
        if value is None:
            return False
        if "band" in cond:
            return value == cond["band"]
        if "band_ne" in cond:
            return value != cond["band_ne"]
        return False
    if "direction" in cond:
        return regime_state == cond["direction"]
    if "flag" in cond:
        return bool(flags.get(cond["flag"]))
    return False


def pattern_first_match(rules: list[dict[str, Any]], bands, regime_state, flags):
    """按优先级取首个全匹配规则；无匹配返回 None（调用方兜底「常态」）。"""
    for rule in rules:
        if all(pattern_eval_condition(c, bands, regime_state, flags)
               for c in rule["all"]):
            return rule
    return None


def pattern_load_frame(conn, market: str,
                       business_config: dict[str, Any]) -> pd.DataFrame:
    """六维 score/confidence + 方向 + L1 判据原子，按交易日左连接。"""
    roles = pattern_input_roles(business_config)
    dim = read_sql(conn, """
        SELECT trade_date, dimension_code, score, confidence
        FROM regime.dimension_value
        WHERE market=%s AND dimension_code=ANY(%s) AND trade_date>=%s
        ORDER BY trade_date
    """, (market, list(PATTERN_DIMS), PATTERN_DATA_START))
    if dim.empty:
        return dim
    score = dim.pivot_table(index="trade_date", columns="dimension_code",
                            values="score", aggfunc="first")
    conf = dim.pivot_table(index="trade_date", columns="dimension_code",
                           values="confidence", aggfunc="first")
    score = score.rename(columns=PATTERN_DIM_TO_LETTER)
    frame = score.reset_index()
    for col in conf.columns:
        frame["conf_" + PATTERN_DIM_TO_LETTER[col]] = conf[col].reindex(
            score.index).to_numpy()
    regime = read_sql(conn, """
        SELECT trade_date, state AS pattern_regime
        FROM regime.business_value
        WHERE biz_code=%s AND market=%s AND state IS NOT NULL AND trade_date>=%s
        ORDER BY trade_date
    """, (roles["direction_axis"], market, PATTERN_DATA_START))
    flag = read_sql(conn, """
        SELECT trade_date, percentile AS pattern_eq_pctl
        FROM regime.indicator_value
        WHERE indicator_code=%s AND market=%s
          AND percentile IS NOT NULL AND trade_date>=%s
        ORDER BY trade_date
    """, (roles["flag"], market, PATTERN_DATA_START))
    frame = (frame.merge(regime, on="trade_date", how="left")
             if not regime.empty else frame.assign(pattern_regime=None))
    frame = (frame.merge(flag, on="trade_date", how="left")
             if not flag.empty else frame.assign(pattern_eq_pctl=None))
    return frame.sort_values("trade_date").reset_index(drop=True)


def pattern_compute(pattern_frame: pd.DataFrame, market: str,
                    pattern_params: dict[str, Any], pattern_params_hash: str
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯计算：u_i 分档 → 字典规则合取 → 游程标注；单日幂等、不依赖上期 state。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    ecdf = pattern_params["ecdf"][market]
    rules = pattern_params["pattern_rules"]
    priority = pattern_params["pattern_priority"]
    min_dims = pattern_params["min_dims"]
    confirm_days = pattern_params["confirm_days"]
    extreme = pattern_params["eq_vs_cap_extreme"]
    valid: list[dict[str, Any]] = []

    for rec in pattern_frame.to_dict("records"):
        scores = {}
        for letter, dim_code in PATTERN_LETTER_TO_DIM.items():
            v = rec.get(letter)
            scores[letter] = (None if v is None or pd.isna(v) else float(v))
        n_ok = sum(1 for v in scores.values() if v is not None)
        if n_ok < min_dims:
            continue                                  # A8：整行不发布
        regime_state = rec.get("pattern_regime")
        if regime_state is None or pd.isna(regime_state):
            continue                                  # 统一发布日历：inner join 方向
        bands = {k: pattern_band(ecdf[PATTERN_LETTER_TO_DIM[k]], scores[k])
                 for k in PATTERN_BAND_LETTERS}
        u_values = {k: pattern_u_of(ecdf[PATTERN_LETTER_TO_DIM[k]]["quantiles"],
                                    scores[k])
                    for k in PATTERN_BAND_LETTERS}
        eq_pctl = rec.get("pattern_eq_pctl")
        eq_pctl = None if eq_pctl is None or pd.isna(eq_pctl) else float(eq_pctl)
        flag_extreme = bool(eq_pctl is not None and
                            (eq_pctl <= extreme["low"] or eq_pctl >= extreme["high"]))
        rule = pattern_first_match(rules, bands, regime_state,
                                   {"eq_vs_cap_extreme": flag_extreme})
        raw = rule["state"] if rule else "常态"
        missing_dims = [PATTERN_LETTER_TO_DIM[k] for k, v in scores.items()
                        if v is None]
        low_conf_dims = [PATTERN_LETTER_TO_DIM[k] for k in PATTERN_BAND_LETTERS
                         if rec.get("conf_" + k) == "低"]
        ranked = sorted((k for k in PATTERN_BAND_LETTERS
                         if u_values[k] is not None),
                        key=lambda k: abs(u_values[k] - 0.5), reverse=True)
        dominant_driver = [PATTERN_LETTER_TO_DIM[k] for k in ranked[:2]]
        confs = [rec.get("conf_" + k) for k in PATTERN_BAND_LETTERS]
        confidence = ("低" if "低" in confs else
                      ("中" if "中" in confs else "高"))
        evidence = {
            "scores": {k: (None if scores[k] is None else round(scores[k], 2))
                       for k in PATTERN_LETTER_TO_DIM},
            "u": u_values,
            "bands": bands,
            "band_high_u": pattern_params["band_high_u"],
            "band_low_u": pattern_params["band_low_u"],
            "cuts_p30_p70": {k: [ecdf[PATTERN_LETTER_TO_DIM[k]]["p30"],
                                 ecdf[PATTERN_LETTER_TO_DIM[k]]["p70"]]
                             for k in PATTERN_BAND_LETTERS},
            "regime_state": regime_state,
            "eq_vs_cap_pctl": (None if eq_pctl is None else round(eq_pctl, 2)),
            "eq_vs_cap_extreme": flag_extreme,
            "low_conf_dims": low_conf_dims,
        }
        valid.append({
            "trade_date": rec["trade_date"], "raw": raw,
            "detail": {
                "pattern": raw,
                "dominant_driver": dominant_driver,
                "evidence": evidence,
                "matched_rules": [rule["id"]] if rule else [],
                "missing_dims": missing_dims,
                "candidate": {},               # 游程算完后回填
            },
            "confidence": confidence,
        })

    if not valid:
        return [], {"rows": 0, "skipped": len(pattern_frame)}

    rows: list[dict[str, Any]] = []
    since = None
    dwell = 0
    prev_raw = None
    for item in valid:
        if item["raw"] != prev_raw:
            since, dwell = item["trade_date"], 1
        else:
            dwell += 1
        prev_raw = item["raw"]
        status = "已确认" if dwell >= confirm_days else "待确认"
        candidate = ({"to": item["raw"],
                      "progress": round(min(dwell / confirm_days, 1.0), 4)}
                     if status == "待确认" else {})
        detail = dict(item["detail"])
        detail["candidate"] = candidate
        rows.append({
            "biz_code": PATTERN_BIZ_CODE, "market": market,
            "trade_date": item["trade_date"],
            "score": None, "state": item["raw"], "level": None,
            "since_date": since, "dwell_days": dwell, "status": status,
            "confidence": item["confidence"], "detail": detail,
            "model_version": pattern_params_hash, "updated_at": now,
        })

    raws = [r["state"] for r in rows]
    share = pd.Series(raws).value_counts(normalize=True).round(4).to_dict()
    stats = {
        "rows": len(rows), "skipped": len(pattern_frame) - len(valid),
        "start": rows[0]["trade_date"], "end": rows[-1]["trade_date"],
        "share": share,
        "switches": sum(1 for a, b in zip(raws, raws[1:]) if a != b),
        "zero_states": [p for p in priority if p != "常态" and share.get(p, 0) == 0],
        "confirmed_days": sum(1 for r in rows if r["status"] == "已确认"),
        "low_conf_days": sum(1 for r in rows if r["confidence"] == "低"),
    }
    return rows, stats


def build_pattern_business(conn, business_config: dict[str, Any],
                           market: str, write: bool) -> dict[str, Any]:
    """`BIZ.MARKET_PATTERN` 生产者：取数 → 算 → 写（逐市场释放）。"""
    pattern_frame = pattern_load_frame(conn, market, business_config)
    pattern_rows, pattern_stats = pattern_compute(
        pattern_frame, market, business_config["params"],
        business_config["params_hash"])
    write_business_rows(conn, pattern_rows, write)
    del pattern_rows
    return pattern_stats


# ── `BIZ.MARKET_DIVERGENCE` 专属常量（仅用于生成种子 / 声明接线）──────────────
DIVERGENCE_BIZ_CODE = "BIZ.MARKET_DIVERGENCE"
DIVERGENCE_FLAG_ATOM = "BREADTH.EQ_VS_CAP_20D"
DIVERGENCE_DIRECTION_BIZ = "BIZ.MARKET_REGIME"

DIVERGENCE_DIMS = ("DIM.BREADTH", "DIM.VALUATION", "DIM.SENTIMENT",
                   "DIM.FLOW", "DIM.STRESS", "DIM.TREND")
DIVERGENCE_DATA_START = datetime.date(1990, 1, 1)      # R7：查询下界
DIVERGENCE_CALIB_END = datetime.date(2025, 12, 31)     # 冻结切点窗口（2026 起样本外）
DIVERGENCE_BAND_LOW_U = 0.30
DIVERGENCE_BAND_HIGH_U = 0.70
DIVERGENCE_MIN_PAIRS = 4
DIVERGENCE_MIN_DISTINCT_DIMS = 3
DIVERGENCE_EQ_EXTREME = {"low": 20.0, "high": 80.0}
DIVERGENCE_STRENGTH_BANDS = [0.15, 0.30, 0.50]
DIVERGENCE_STATE_LABELS = ("一致", "轻度背离", "显著背离", "多重背离")
DIVERGENCE_BAND_ORD = {"L": 0, "M": 1, "H": 2}
DIVERGENCE_FLAG_TEXTS = {
    "short_fade": "短窗动能衰竭（中期趋势未破）",
    "mid_unrepaired": "短期反弹但中期未修复",
}

# 对清单整体落 params（改任一处即触发 params_hash 与全量重算）。
# ⚠ delta 一律是**各维自身冻结档位的序数差 0/1/2**，不是分位差；结构型对只有 0 / 2。
DIVERGENCE_PAIRS = [
    {"id": "D1", "name": "参与面 vs 指数", "kind": "structural",
     "criterion": "eq_extreme_and_breadth_low", "dims": ["DIM.BREADTH"]},
    {"id": "D2", "name": "意愿 vs 净流", "kind": "amplitude",
     "left": "DIM.SENTIMENT", "right": "DIM.FLOW",
     "dims": ["DIM.SENTIMENT", "DIM.FLOW"]},
    {"id": "D3", "name": "估值 vs 趋势", "kind": "amplitude",
     "left": "DIM.VALUATION", "right": "DIM.TREND",
     "dims": ["DIM.VALUATION", "DIM.TREND"]},
    {"id": "D4", "name": "压力 vs 方向", "kind": "structural",
     "criterion": "stress_high_and_regime_up", "dims": ["DIM.STRESS"]},
    {"id": "D5", "name": "短窗 vs 长窗", "kind": "structural",
     "criterion": "structural_flag", "dims": ["DIM.BREADTH"]},
    {"id": "D6", "name": "参与面 vs 情绪", "kind": "amplitude",
     "left": "DIM.BREADTH", "right": "DIM.SENTIMENT",
     "dims": ["DIM.BREADTH", "DIM.SENTIMENT"]},
]

DIVERGENCE_INPUTS = (
    [{"kind": "dimension", "code": d, "role": "band"} for d in DIVERGENCE_DIMS]
    + [{"kind": "atom", "code": DIVERGENCE_FLAG_ATOM, "role": "eq_extreme"},
       {"kind": "business", "code": DIVERGENCE_DIRECTION_BIZ,
        "role": "direction_axis"}]
)

DIVERGENCE_OUTPUT_SCHEMA = {
    "level_semantics": "1=一致 / 2=轻度背离 / 3=显著背离 / 4=多重背离（升序=归一强度越大；按 biz_code 过滤，禁止跨 biz 横比）",
    "score_semantics": "命中背离对数（0..n_valid 计数，n_valid 见 detail.n_valid_pairs）；非 0-100 程度分、非分位、非预测值",
    "state_semantics": "按归一强度 strength=Σdelta/(2×n_valid) 切四档，不是按命中计数切档；delta 是各维自身冻结档位的序数差 0/1/2，不是分位差。不表达方向、不预测拐点、不产出仓位建议",
    "detail_keys": ["pairs", "dominant_pair", "evidence", "missing_pairs",
                    "n_valid_pairs", "hit_ratio", "strength"],
}

DIVERGENCE_REMARK = (
    "v1.33（2026-11-07 按实测整改并落地）：撤销 §6.3 原规格的 "
    "delta=|score_a−score_b| 分位差 + pair_thresholds=25 + divergence_bands=[0,1,3,5]。"
    "原因：六维 score 是各自历史 expanding 分位，分位基数 / 分布宽度 / 有效起点均不同"
    "（CN VALUATION P30/P70=55/89、STRESS=43/53；HK FLOW=57/87、SENTIMENT=24/61），"
    "直接相减违反 market_profile.md 的「六维分不可跨维加减」硬约束；实测同一阈值 25 下 "
    "D2 命中率 CN 36.8% / HK 64.3%，原四档切点的第 4 档（>=5 命中）全历史 0 天。"
    "新口径：各维按 (market, dim) 冻结 P30/P70 切成 L/M/H，成对取序数档位差 "
    "delta=|ord_a−ord_b|∈{0,1,2}（结构型对 0/2），state 按归一强度 "
    "strength=Σdelta/(2×n_valid) 切 [0.15,0.30,0.50] 四档，score 保留命中计数。"
    "D1 改读 L1 原子 BREADTH.EQ_VS_CAP_20D.percentile（双侧 20/80），移除不落表、"
    "恒 benchmark_mismatch 的 A/D 派生量；D5 改读 dimension_value.flags.structural "
    "的中文文案（L2 只读不重算，映射常量在 params）；D4 用 band(STRESS)=H 定义「高」。"
    "不强制 inner join 方向（方向缺失时 D4 退出），改由 min_pairs=4 + "
    "min_distinct_dims=3 防「四对全靠 BREADTH」的一因多计。TREND.score 是 |t| 的无符号"
    "强度，D3 只报档位互斥、不解读为估值—趋势因果。⚠ 冻结切点有参数层前视；L1 全期 "
    "survivorship_bias=true；CN EQ_VS_CAP_20D 分位 2019-01-23 起才满 min_periods=250。"
    "params 纳入六维 params_hash、对清单与全部阈值快照。"
)


# ── `BIZ.MARKET_DIVERGENCE` 专属函数 ────────────────────────────────────────
def divergence_upstream_hashes(conn) -> dict[str, str]:
    """六维口径指纹；任一维缺失 / 为空即拒绝生成种子。"""
    with conn.cursor() as cur:
        cur.execute("SELECT dimension_code, params_hash FROM regime.dimension_dict "
                    "WHERE dimension_code=ANY(%s)", (list(DIVERGENCE_DIMS),))
        out = {r[0]: r[1] for r in cur.fetchall()}
    missing = [d for d in DIVERGENCE_DIMS if not out.get(d)]
    if missing:
        raise RuntimeError("以下维度字典缺失或 params_hash 为空：" + ", ".join(missing))
    return out


def divergence_calibrate(conn, calib_end=DIVERGENCE_CALIB_END) -> dict[str, Any]:
    """按 (market, dimension) 冻结 P30/P70；只取 calib_end 前的样本。"""
    frame = read_sql(conn, """
        SELECT market, dimension_code, score
        FROM regime.dimension_value
        WHERE market=ANY(%s) AND dimension_code=ANY(%s)
          AND trade_date>=%s AND trade_date<=%s AND score IS NOT NULL
    """, (list(BUSINESS_MARKETS), list(DIVERGENCE_DIMS),
          DIVERGENCE_DATA_START, calib_end))
    if frame.empty:
        raise RuntimeError("dimension_value 无可用 score，无法标定背离档位切点")
    cuts: dict[str, Any] = {}
    for (market, dim_code), group in frame.groupby(["market", "dimension_code"]):
        arr = np.sort(group["score"].to_numpy(dtype=float))
        cuts.setdefault(market, {})[dim_code] = {
            "n": int(arr.size),
            "p30": round(float(np.quantile(arr, DIVERGENCE_BAND_LOW_U)), 4),
            "p70": round(float(np.quantile(arr, DIVERGENCE_BAND_HIGH_U)), 4),
        }
    for market in BUSINESS_MARKETS:
        missing = [d for d in DIVERGENCE_DIMS if d not in cuts.get(market, {})]
        if missing:
            raise RuntimeError(f"{market} 缺维度档位切点：" + ", ".join(missing))
    return {"calib_end": calib_end.isoformat(), "band_cuts": cuts,
            "n_obs": int(len(frame))}


def divergence_build_dict_row(calib: dict[str, Any],
                              upstream_hashes: dict[str, str]) -> dict[str, Any]:
    """生成字典行；切点 / 对清单 / 阈值 / 六维指纹全部进 params_hash。"""
    params = {
        "band_low_u": DIVERGENCE_BAND_LOW_U,
        "band_high_u": DIVERGENCE_BAND_HIGH_U,
        "min_pairs": DIVERGENCE_MIN_PAIRS,
        "min_distinct_dims": DIVERGENCE_MIN_DISTINCT_DIMS,
        "publish_policy": "pair_level_degrade",
        "state_rule": "normalized_strength",
        "score_rule": "hit_count",
        "calib_end": calib["calib_end"],
        "band_cuts": calib["band_cuts"],
        "strength_bands": list(DIVERGENCE_STRENGTH_BANDS),
        "state_labels": list(DIVERGENCE_STATE_LABELS),
        "eq_vs_cap_extreme": DIVERGENCE_EQ_EXTREME,
        "structural_flag_texts": DIVERGENCE_FLAG_TEXTS,
        "pair_rules": DIVERGENCE_PAIRS,
        "upstream_dim_params_hashes": upstream_hashes,
    }
    row = {
        "biz_code": DIVERGENCE_BIZ_CODE,
        "biz_name": "侧面背离",
        "kind": "rule_set",
        "inputs": DIVERGENCE_INPUTS,
        "params": params,
        "output_schema": DIVERGENCE_OUTPUT_SCHEMA,
        "is_active": True,
        "remark": DIVERGENCE_REMARK,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }
    payload = {k: row[k] for k in ("biz_code", "kind", "inputs", "params", "output_schema")}
    row["params_hash"] = hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return row


def divergence_input_roles(business_config: dict[str, Any]) -> dict[str, Any]:
    """按 `inputs[].role` 取接线；band 有多个，其余各一个。"""
    roles: dict[str, Any] = {"bands": []}
    for item in business_config["inputs"]:
        if item["role"] == "band":
            roles["bands"].append(item["code"])
        else:
            roles[item["role"]] = item["code"]
    return roles


def divergence_band(cuts: dict[str, float], score) -> str | None:
    """按该维冻结的 P30/P70 分档；缺失返回 None（strict，不按中性填充）。"""
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return None
    v = float(score)
    if v >= cuts["p70"]:
        return "H"
    if v <= cuts["p30"]:
        return "L"
    return "M"


def divergence_evaluate_pairs(bands: dict[str, str | None], scores: dict,
                              eq_pctl, regime_state, structural_flags,
                              params: dict[str, Any]
                              ) -> tuple[list[dict[str, Any]], list[str]]:
    """逐对判定；任一输入缺失 → 该对退出并记 missing（A8：不填中性）。

    `delta` 恒为整数 0/1/2：`dominant_pair` 用 max(delta) 不会出现 None 比较。
    """
    ext = params["eq_vs_cap_extreme"]
    texts = params["structural_flag_texts"]
    pairs: list[dict[str, Any]] = []
    missing: list[str] = []
    for rule in params["pair_rules"]:
        pid = rule["id"]
        if rule["kind"] == "amplitude":
            lb, rb = bands.get(rule["left"]), bands.get(rule["right"])
            if lb is None or rb is None:
                missing.append(pid)
                continue
            gap = abs(DIVERGENCE_BAND_ORD[lb] - DIVERGENCE_BAND_ORD[rb])
            pairs.append({
                "id": pid, "name": rule["name"], "delta": gap,
                "hit": gap >= 1, "criterion": "amplitude",
                "left": lb, "right": rb,
                "left_score": round(float(scores[rule["left"]]), 2),
                "right_score": round(float(scores[rule["right"]]), 2),
            })
            continue
        crit = rule["criterion"]
        if crit == "eq_extreme_and_breadth_low":
            b = bands.get("DIM.BREADTH")
            if b is None or eq_pctl is None:
                missing.append(pid)
                continue
            hit = bool((eq_pctl <= ext["low"] or eq_pctl >= ext["high"])
                       and b == "L")
            pairs.append({
                "id": pid, "name": rule["name"], "delta": 2 if hit else 0,
                "hit": hit, "criterion": "structural",
                "left": round(float(eq_pctl), 2), "right": b,
                "flag": "eq_vs_cap_extreme",
            })
        elif crit == "stress_high_and_regime_up":
            s = bands.get("DIM.STRESS")
            if s is None or regime_state is None:
                missing.append(pid)
                continue
            hit = bool(s == "H" and regime_state == "上行")
            pairs.append({
                "id": pid, "name": rule["name"], "delta": 2 if hit else 0,
                "hit": hit, "criterion": "structural",
                "left": s, "right": regime_state,
                "left_score": round(float(scores["DIM.STRESS"]), 2),
            })
        elif crit == "structural_flag":
            b = bands.get("DIM.BREADTH")
            if b is None or structural_flags is None:
                missing.append(pid)
                continue
            hit = bool(structural_flags.get("short_fade") or
                       structural_flags.get("mid_unrepaired"))
            pairs.append({
                "id": pid, "name": rule["name"], "delta": 2 if hit else 0,
                "hit": hit, "criterion": "structural",
                "left": (texts["short_fade"] if structural_flags.get("short_fade")
                         else (texts["mid_unrepaired"]
                               if structural_flags.get("mid_unrepaired") else None)),
                "right": b,
            })
        else:
            raise ValueError(f"未知背离判据：{crit}")
    return pairs, missing


def divergence_load_frame(conn, market: str,
                          business_config: dict[str, Any]) -> pd.DataFrame:
    """六维 score + BREADTH.flags 文案 + L1 原子分位 + 方向，按交易日左连接。"""
    roles = divergence_input_roles(business_config)
    texts = business_config["params"]["structural_flag_texts"]
    dim = read_sql(conn, """
        SELECT trade_date, dimension_code, score
        FROM regime.dimension_value
        WHERE market=%s AND dimension_code=ANY(%s) AND trade_date>=%s
        ORDER BY trade_date
    """, (market, roles["bands"], DIVERGENCE_DATA_START))
    if dim.empty:
        return pd.DataFrame()
    score = dim.pivot_table(index="trade_date", columns="dimension_code",
                            values="score", aggfunc="first")
    frame = score.reset_index()

    flag = read_sql(conn, """
        SELECT trade_date, flags FROM regime.dimension_value
        WHERE market=%s AND dimension_code=%s AND trade_date>=%s
    """, (market, "DIM.BREADTH", DIVERGENCE_DATA_START))
    flag_map = {}
    for rec in flag.to_dict("records"):
        obj = _parse_jsonb(rec["flags"]) or {}
        arr = obj.get("structural") or []
        if isinstance(arr, str):
            arr = json.loads(arr)
        txts = set(arr if isinstance(arr, list) else [])
        flag_map[rec["trade_date"]] = {
            "short_fade": texts["short_fade"] in txts,
            "mid_unrepaired": texts["mid_unrepaired"] in txts,
        }

    eq = read_sql(conn, """
        SELECT trade_date, percentile FROM regime.indicator_value
        WHERE market=%s AND indicator_code=%s AND trade_date>=%s
          AND percentile IS NOT NULL
    """, (market, roles["eq_extreme"], DIVERGENCE_DATA_START))
    regime = read_sql(conn, """
        SELECT trade_date, state FROM regime.business_value
        WHERE market=%s AND biz_code=%s AND trade_date>=%s
          AND state IS NOT NULL
    """, (market, roles["direction_axis"], DIVERGENCE_DATA_START))

    frame["div_flags"] = frame["trade_date"].map(flag_map)
    frame = (frame.merge(eq.rename(columns={"percentile": "div_eq_pctl"}),
                         on="trade_date", how="left")
             if not eq.empty else frame.assign(div_eq_pctl=None))
    frame = (frame.merge(regime.rename(columns={"state": "div_regime"}),
                         on="trade_date", how="left")
             if not regime.empty else frame.assign(div_regime=None))
    return frame.sort_values("trade_date").reset_index(drop=True)


def divergence_compute(frame: pd.DataFrame, market: str, params: dict[str, Any],
                       params_hash: str
                       ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯计算：冻结档位 → 逐对序数差 → 归一强度分档；单日幂等、无状态机。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    cuts = params["band_cuts"][market]
    bands_def = params["strength_bands"]
    labels = params["state_labels"]
    if len(labels) != len(bands_def) + 1:
        raise ValueError("state_labels 长度应为 strength_bands 长度 + 1")
    dims_by_id = {r["id"]: r["dims"] for r in params["pair_rules"]}
    pair_hits = {r["id"]: 0 for r in params["pair_rules"]}
    pair_valid = {r["id"]: 0 for r in params["pair_rules"]}
    dominant = {r["id"]: 0 for r in params["pair_rules"]}
    rows: list[dict[str, Any]] = []
    skipped = 0

    for rec in frame.to_dict("records"):
        scores = {}
        for d in DIVERGENCE_DIMS:
            v = rec.get(d)
            scores[d] = None if v is None or pd.isna(v) else float(v)
        bands = {d: divergence_band(cuts[d], scores[d]) for d in DIVERGENCE_DIMS}
        eq = rec.get("div_eq_pctl")
        eq = None if eq is None or pd.isna(eq) else float(eq)
        reg = rec.get("div_regime")
        reg = None if reg is None or (isinstance(reg, float) and pd.isna(reg)) else reg
        fl = rec.get("div_flags")
        fl = fl if isinstance(fl, dict) else None

        pairs, missing = divergence_evaluate_pairs(
            bands, scores, eq, reg, fl, params)
        n_valid = len(pairs)
        distinct_dims: set[str] = set()
        for p in pairs:
            distinct_dims.update(dims_by_id[p["id"]])
        if (n_valid < params["min_pairs"] or
                len(distinct_dims) < params["min_distinct_dims"]):
            skipped += 1
            continue

        strength = sum(p["delta"] for p in pairs) / (2 * n_valid)
        cnt = sum(1 for p in pairs if p["hit"])
        idx = bisect.bisect_right(bands_def, strength)
        state, level = labels[idx], idx + 1
        hit_pairs = [p for p in pairs if p["hit"]]
        dom = (max(hit_pairs, key=lambda p: p["delta"])["id"]
               if hit_pairs else None)
        for p in pairs:
            pair_valid[p["id"]] += 1
            pair_hits[p["id"]] += int(p["hit"])
        if dom:
            dominant[dom] += 1
        confidence = ("低" if n_valid < len(params["pair_rules"])
                      or len(distinct_dims) < 4 else "中")

        rows.append({
            "biz_code": DIVERGENCE_BIZ_CODE, "market": market,
            "trade_date": rec["trade_date"],
            "score": cnt, "state": state, "level": level,
            "since_date": None, "dwell_days": None, "status": None,
            "confidence": confidence,
            "detail": {
                "pairs": pairs,
                "dominant_pair": dom,
                "evidence": {
                    "strength_bands": bands_def,
                    "calib_end": params["calib_end"],
                    "eq_vs_cap_extreme": params["eq_vs_cap_extreme"],
                    "upstream_dim_params_hashes": params["upstream_dim_params_hashes"],
                    "delta_semantics": "序数档位差 0/1/2（非分位差）",
                },
                "missing_pairs": missing,
                "n_valid_pairs": n_valid,
                "hit_ratio": round(cnt / n_valid, 4),
                "strength": round(strength, 4),
            },
            "model_version": params_hash, "updated_at": now,
        })

    if not rows:
        return [], {"rows": 0, "skipped": skipped}
    share = (pd.Series([r["state"] for r in rows])
             .value_counts(normalize=True).round(4).to_dict())
    stats = {
        "rows": len(rows), "skipped": skipped,
        "start": rows[0]["trade_date"], "end": rows[-1]["trade_date"],
        "share": share,
        "zero_bands": [s for s in labels if share.get(s, 0) == 0],
        "avg_strength": round(float(np.mean(
            [r["detail"]["strength"] for r in rows])), 4),
        "avg_cnt": round(float(np.mean([r["score"] for r in rows])), 4),
        "pair_hit_rates": {k: round(pair_hits[k] / pair_valid[k], 4)
                           for k in pair_hits if pair_valid[k]},
        "dominant_pair_dist": dominant,
        "n_valid_dist": {int(k): int(v) for k, v in
                         pd.Series([r["detail"]["n_valid_pairs"] for r in rows])
                         .value_counts().sort_index().items()},
    }
    return rows, stats


def build_divergence_business(conn, business_config: dict[str, Any],
                              market: str, write: bool) -> dict[str, Any]:
    """`BIZ.MARKET_DIVERGENCE` 生产者：取数 → 算 → 写（逐市场释放）。"""
    frame = divergence_load_frame(conn, market, business_config)
    rows, stats = divergence_compute(frame, market, business_config["params"],
                                     business_config["params_hash"])
    write_business_rows(conn, rows, write)
    del rows
    return stats


# ── `BIZ.REVERSAL_RISK` 专属常量（一期裁剪版，§6.4 v1.34）────────────────────
# 与 §6.4 原规格的偏离（已在 doc/market_state_data_architecture.md §6.4 登记）：
#   ① 一期跳过 signal_dict / signal_value / v_signal_summary，族计数由生产者内部
#      直接读 L1 原子分位计算（当前零消费方需要逐条 hit / days 清单）；
#   ② 族清单由「理想 18 顶 + 12 底」收缩为「固定 5 族 × 每族 1 代表原子」，
#      宏观 / 基本面 / 结构因短样本或无源移出分母；
#   ③ 撤销 SWITCH_DAYS 状态机：state 恒当日 raw，稳定性由游程标注承载；
#   ④ 判读阈值按市场用样本内档位占比标定，替代绝对 [0, 2, 4]；
#   ⑤ 族间共变簇只作描述性标注，不宣称「几个独立侧面」。
REVERSAL_BIZ_CODE = "BIZ.REVERSAL_RISK"
REVERSAL_FAMILIES = ("width", "valuation", "sentiment", "stress", "flow")
REVERSAL_FAMILY_LABELS = {
    "width": "宽度", "valuation": "估值", "sentiment": "情绪",
    "stress": "压力", "flow": "资金",
}
REVERSAL_FAMILY_ATOMS = {
    "CN": {
        "width": "BREADTH.PCT_ABOVE_MA200",
        "valuation": "VAL.PE_TTM_MEDIAN",
        "sentiment": "FLOW.TURNOVER_RATIO",
        "stress": "RISK.RV20",
        "flow": "FLOW.MARGIN_NET_20D",
    },
    "HK": {
        "width": "BREADTH.PCT_ABOVE_MA200",
        "valuation": "VAL.PE_TTM_MEDIAN",
        "sentiment": "FLOW.TURNOVER_RATIO",
        "stress": "RISK.RV20",
        "flow": "FLOW.BUYBACK_NET_20D",
    },
}
REVERSAL_DATA_START = datetime.date(1990, 1, 1)       # R7：查询下界
REVERSAL_CALIB_END = datetime.date(2025, 12, 31)      # 冻结切点窗口，2026 起样本外
REVERSAL_PUBLISH_FROM = {"CN": "2019-01-02", "HK": "2013-09-10"}
REVERSAL_MIN_AVAILABLE_FAMILIES = 5
REVERSAL_TOP_Q = 0.90
REVERSAL_BOTTOM_Q = 0.10
REVERSAL_TARGET_HIGH_SHARE = 0.10
REVERSAL_CONFIRM_DAYS = 3
REVERSAL_FAMILY_LIST_VERSION = "v1.34-5fam-20261107"
REVERSAL_STATE_LABELS = ("正常", "警惕", "高风险")
REVERSAL_BOTTOM_STATE_LABELS = ("正常", "关注", "高价值区")
REVERSAL_EXCLUDED_FAMILIES = {
    "宏观": "MACRO.CREDIT_IMPULSE CN 2021-02-18 / HK 2019-01-02 起，短样本",
    "基本面": "FORECAST.REVISION_BREADTH CN 2026-10-08 起仅 22 行，HK 无源",
    "结构": "仅 EQ_VS_CAP_20D 且 CN 2019-01-23 起；行业拥挤度第④层未建",
}
REVERSAL_CORRELATED_FAMILIES = {
    "CN": [["宽度", "估值", "情绪", "资金"]],
    "HK": [["宽度", "估值"]],
}
REVERSAL_INPUTS = [
    {"kind": "atom", "code": code, "role": "family_atom",
     "market": market, "family": fam}
    for market in BUSINESS_MARKETS
    for fam, code in REVERSAL_FAMILY_ATOMS[market].items()
]
REVERSAL_OUTPUT_SCHEMA = {
    "level_semantics": "1=正常 / 2=警惕 / 3=高风险（升序=越危险，仅承载顶部）",
    "score_semantics": ("top_family_cnt，族去重计数（0..5 整数），"
                        "非 0-100 程度分 / 非分位读数"),
    "state_semantics": ("顶部风险离散档，恒当日 raw（无状态机）；底部判读只写 "
                        "detail.bottom_state，不驱动 level / score"),
    "detail_keys": ["top_state", "bottom_state", "top_families", "bottom_families",
                    "top_cnt", "bottom_cnt", "unavailable_families",
                    "signal_conflict", "dwell_days", "missing"],
    "no_state_machine": True,
    "polymorphic": "level / score 为多态列，禁止与 §6.1 / §6.3 横比",
}
REVERSAL_REMARK = (
    "一期裁剪版（§6.4 v1.34）：离线 Go/No-Go 预检通过后落地，零新增数据源、零新增 L1。"
    "① 固定 5 族 + 每族 1 代表原子，族计数在生产者内部由 L1 percentile 计算，"
    "暂不落 signal_value（当前零消费方需要逐条 hit / days）；二期接清单层时须"
    "递增 params_hash 并全量重算。② 宏观 / 基本面 / 结构因短样本或无源移出分母，"
    "写进 detail.unavailable_families；发布日历按市场冻结（CN 2019-01-02 / "
    "HK 2013-09-10），当日须 5 族分位齐全，缺一即整行不发布（A8）。"
    "③ 撤销 SWITCH_DAYS 状态机（§6.2 v1.32 已证伪其对稀有档的压制），"
    "state 恒当日 raw，稳定性由 since_date / dwell_days / status 游程标注承载。"
    "④ 判读阈值按市场用标定窗口内各档占比标定并冻结，替代绝对 [0,2,4]"
    "（实测该初值下 HK 高风险档全历史仅 1 日）。⑤ 族间共变只作描述性标注："
    "CN 宽度 / 估值 / 情绪 / 资金 同期分位相关最高 0.756，HK 宽度 / 估值 0.667，"
    "故不宣称『几个独立侧面』，只表达『几个侧面同时处于极端』；"
    "confidence 按共变程度标 CN 低 / HK 中。⑥ 不驱动仓位 / 配置建议、"
    "不预测拐点时间；CN 无公认顶底历史效标（族起点晚于 2015/2018 区间）。"
    "params 纳入 6 个去重 L1 原子的 indicator_dict.params_hash 快照、族切点、判读阈值、"
    "发布日历与族清单版本，任一变更触发全量重算。"
)


def reversal_atom_codes() -> list[str]:
    """跨市场去重后的 L1 原子编码，供上游指纹与取数使用。"""
    return sorted({c for m in REVERSAL_FAMILY_ATOMS.values() for c in m.values()})


def reversal_upstream_hashes(conn) -> dict[str, str]:
    """L1 原子口径指纹；任一原子缺失或空 hash 即拒绝生成种子。"""
    codes = reversal_atom_codes()
    with conn.cursor() as cur:
        cur.execute("SELECT indicator_code, params_hash FROM regime.indicator_dict "
                    "WHERE indicator_code=ANY(%s)", (codes,))
        out = {r[0]: r[1] for r in cur.fetchall()}
    missing = [c for c in codes if not out.get(c)]
    if missing:
        raise RuntimeError("以下原子字典缺失或 params_hash 为空：" + ", ".join(missing))
    return out


def reversal_calibrate(conn, calib_end=REVERSAL_CALIB_END) -> dict[str, Any]:
    """冻结 `(market, family)` 顶/底切点与按市场的判读阈值。

    切点：标定窗口内该原子 percentile 的 P90 / P10（输入本身是 0-100 as-of 分位，
    切点同尺度），避免 §6.2 证伪过的跨市场绝对切点。判读阈值：在「5 族全齐 +
    发布起点后」的标定样本里，选使高风险档占比最接近 REVERSAL_TARGET_HIGH_SHARE
    的整数族数切点，按市场分别冻结（两市场族代表原子分布宽度不同，禁止横比）。
    """
    codes = reversal_atom_codes()
    frame = read_sql(conn, """
        SELECT market, indicator_code, trade_date, percentile
        FROM regime.indicator_value
        WHERE market=ANY(%s) AND indicator_code=ANY(%s)
          AND trade_date>=%s AND trade_date<=%s AND percentile IS NOT NULL
    """, (list(BUSINESS_MARKETS), codes, REVERSAL_DATA_START, calib_end))
    if frame.empty:
        raise RuntimeError("indicator_value 无可用原子分位，无法标定反转风险族切点")

    family_cuts: dict[str, Any] = {}
    market_bands: dict[str, Any] = {}
    for market in BUSINESS_MARKETS:
        atoms = REVERSAL_FAMILY_ATOMS[market]
        sub = frame[frame["market"] == market]
        family_cuts[market] = {}
        for fam in REVERSAL_FAMILIES:
            arr = np.sort(sub.loc[sub["indicator_code"] == atoms[fam],
                                  "percentile"].to_numpy(dtype=float))
            if arr.size == 0:
                raise RuntimeError(f"{market}/{fam} 标定窗口内无原子分位")
            family_cuts[market][fam] = {
                "n": int(arr.size),
                "top": round(float(np.quantile(arr, REVERSAL_TOP_Q)), 4),
                "bottom": round(float(np.quantile(arr, REVERSAL_BOTTOM_Q)), 4),
            }
        piv = sub.pivot_table(index="trade_date", columns="indicator_code",
                              values="percentile", aggfunc="first")
        cols = [atoms[f] for f in REVERSAL_FAMILIES]
        present = [c for c in cols if c in piv.columns]
        if len(present) < REVERSAL_MIN_AVAILABLE_FAMILIES:
            raise RuntimeError(f"{market} 标定样本缺族，无法标定判读阈值")
        piv = piv.dropna(subset=present)
        piv = piv[piv.index.astype(str) >= REVERSAL_PUBLISH_FROM[market]]
        tfc = sum((piv[atoms[f]] >= family_cuts[market][f]["top"]).astype(int)
                  for f in REVERSAL_FAMILIES)
        if len(tfc) == 0:
            raise RuntimeError(f"{market} 发布起点后无 5 族齐全的标定样本")
        candidates = range(1, len(REVERSAL_FAMILIES) + 1)
        high_from = min(candidates,
                        key=lambda k: abs(float((tfc >= k).mean()) -
                                          REVERSAL_TARGET_HIGH_SHARE))
        market_bands[market] = {
            "warn_from": 1, "high_from": int(high_from),
            "calib_n": int(len(tfc)),
            "high_share": round(float((tfc >= high_from).mean()), 4),
        }
    return {"calib_end": calib_end.isoformat(), "family_cuts": family_cuts,
            "market_bands": market_bands, "n_obs": int(len(frame))}


def reversal_build_dict_row(calib: dict[str, Any],
                            upstream_hashes: dict[str, str]) -> dict[str, Any]:
    """生成字典行；族清单 / 切点 / 阈值 / 发布日历 / 原子指纹全部进 params_hash。"""
    params = {
        "family_list_version": REVERSAL_FAMILY_LIST_VERSION,
        "families": list(REVERSAL_FAMILIES),
        "family_labels": REVERSAL_FAMILY_LABELS,
        "family_atoms": REVERSAL_FAMILY_ATOMS,
        "family_cuts": calib["family_cuts"],
        "market_bands": calib["market_bands"],
        "publish_from": REVERSAL_PUBLISH_FROM,
        "min_available_families": REVERSAL_MIN_AVAILABLE_FAMILIES,
        "top_q": REVERSAL_TOP_Q, "bottom_q": REVERSAL_BOTTOM_Q,
        "target_high_share": REVERSAL_TARGET_HIGH_SHARE,
        "confirm_days": REVERSAL_CONFIRM_DAYS,
        "conflict_downgrade": True,
        "calib_end": calib["calib_end"],
        "calib_n_obs": calib["n_obs"],
        "excluded_families": REVERSAL_EXCLUDED_FAMILIES,
        "correlated_families": REVERSAL_CORRELATED_FAMILIES,
        "state_labels": list(REVERSAL_STATE_LABELS),
        "bottom_state_labels": list(REVERSAL_BOTTOM_STATE_LABELS),
        "upstream_atom_params_hashes": upstream_hashes,
        "publish_policy": "all_families_present",
        "state_rule": "top_state_only_no_state_machine",
        "score_rule": "top_family_cnt",
    }
    row = {
        "biz_code": REVERSAL_BIZ_CODE,
        "biz_name": "反转风险",
        "kind": "rule_set",
        "inputs": REVERSAL_INPUTS,
        "params": params,
        "output_schema": REVERSAL_OUTPUT_SCHEMA,
        "is_active": True,
        "remark": REVERSAL_REMARK,
        "updated_at": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))),
    }
    payload = {k: row[k] for k in ("biz_code", "kind", "inputs", "params", "output_schema")}
    row["params_hash"] = hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return row


def reversal_band_of(cnt: int, defn: dict[str, Any]) -> int:
    """族数 → 1..3 判读档。显式 1-based，规避旧伪代码 0/1-based 混用。"""
    if cnt >= defn["high_from"]:
        return 3
    if cnt >= defn["warn_from"]:
        return 2
    return 1


def reversal_load_frame(conn, market: str,
                        business_config: dict[str, Any]) -> pd.DataFrame:
    """该市场 5 个族代表原子的 L1 分位宽表；查询带实体过滤与时间下界（R7）。"""
    atoms = business_config["params"]["family_atoms"][market]
    codes = sorted(set(atoms.values()))
    frame = read_sql(conn, """
        SELECT trade_date, indicator_code, percentile
        FROM regime.indicator_value
        WHERE market=%s AND indicator_code=ANY(%s)
          AND trade_date>=%s AND percentile IS NOT NULL
        ORDER BY trade_date
    """, (market, codes, REVERSAL_DATA_START))
    if frame.empty:
        return pd.DataFrame()
    piv = frame.pivot_table(index="trade_date", columns="indicator_code",
                            values="percentile", aggfunc="first")
    return piv.reset_index().sort_values("trade_date").reset_index(drop=True)


def reversal_compute(frame: pd.DataFrame, market: str, params: dict[str, Any],
                     params_hash: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯计算：族去重计数 → 判读档 → 冲突降档 → 游程标注。无状态机、全量幂等。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    atoms = params["family_atoms"][market]
    cuts = params["family_cuts"][market]
    bands_def = params["market_bands"][market]
    publish_from = datetime.date.fromisoformat(params["publish_from"][market])
    labels = params["state_labels"]
    bottom_labels = params["bottom_state_labels"]
    excluded = list(params["excluded_families"].keys())
    correlated = params["correlated_families"][market]
    confidence = "低" if market == "CN" else "中"
    rows: list[dict[str, Any]] = []
    skipped = 0
    prev_level: int | None = None
    since: datetime.date | None = None
    dwell = 0
    conflict_days = 0
    top_cnt_dist: dict[int, int] = {}

    for rec in frame.to_dict("records"):
        d = rec["trade_date"]
        if isinstance(d, (pd.Timestamp, datetime.datetime)):
            d = d.date()
        if d < publish_from:
            continue
        vals: dict[str, float | None] = {}
        for fam in params["families"]:
            v = rec.get(atoms[fam])
            vals[fam] = None if v is None or pd.isna(v) else float(v)
        avail = sum(1 for v in vals.values() if v is not None)
        if avail < params["min_available_families"]:
            skipped += 1
            continue

        top_fams = [f for f in params["families"] if vals[f] >= cuts[f]["top"]]
        bottom_fams = [f for f in params["families"] if vals[f] <= cuts[f]["bottom"]]
        tfc, bfc = len(top_fams), len(bottom_fams)
        top_cnt_dist[tfc] = top_cnt_dist.get(tfc, 0) + 1
        top_state = reversal_band_of(tfc, bands_def)
        bottom_state = reversal_band_of(bfc, bands_def)
        conflict = False
        if params["conflict_downgrade"] and top_state >= 2 and bottom_state >= 2:
            top_state, bottom_state = top_state - 1, bottom_state - 1
            conflict = True
        if conflict:
            conflict_days += 1

        level = top_state
        state = labels[level - 1]
        if level != prev_level:
            since, dwell, prev_level = d, 1, level
        else:
            dwell += 1

        rows.append({
            "biz_code": REVERSAL_BIZ_CODE, "market": market, "trade_date": d,
            "score": tfc, "state": state, "level": level,
            "since_date": since, "dwell_days": dwell,
            "status": "已确认" if dwell >= params["confirm_days"] else "待确认",
            "confidence": confidence,
            "detail": {
                "top_state": top_state, "bottom_state": bottom_state,
                "top_families": [params["family_labels"][f] for f in top_fams],
                "bottom_families": [params["family_labels"][f] for f in bottom_fams],
                "top_cnt": tfc, "bottom_cnt": bfc,
                "unavailable_families": excluded,
                "signal_conflict": conflict,
                "dwell_days": dwell, "missing": None,
                "available_family_cnt": avail,
                "correlated_families": correlated,
                "bottom_state_label": bottom_labels[bottom_state - 1],
                "evidence": {
                    "calib_end": params["calib_end"],
                    "family_list_version": params["family_list_version"],
                    "market_bands": bands_def,
                    "upstream_atom_params_hashes":
                        params["upstream_atom_params_hashes"],
                },
            },
            "model_version": params_hash, "updated_at": now,
        })

    if not rows:
        return [], {"rows": 0, "skipped": skipped}
    share = (pd.Series([r["state"] for r in rows])
             .value_counts(normalize=True).round(4).to_dict())
    stats = {
        "rows": len(rows), "skipped": skipped,
        "start": rows[0]["trade_date"], "end": rows[-1]["trade_date"],
        "share": share,
        "zero_bands": [s for s in labels if share.get(s, 0) == 0],
        "avg_top_cnt": round(float(np.mean([r["score"] for r in rows])), 4),
        "top_cnt_dist": {int(k): int(v) for k, v in sorted(top_cnt_dist.items())},
        "conflict_days": conflict_days,
        "high_from": bands_def["high_from"],
    }
    return rows, stats


def build_reversal_business(conn, business_config: dict[str, Any],
                            market: str, write: bool) -> dict[str, Any]:
    """`BIZ.REVERSAL_RISK` 生产者：取数 → 算 → 写（逐市场释放）。"""
    frame = reversal_load_frame(conn, market, business_config)
    rows, stats = reversal_compute(frame, market, business_config["params"],
                                   business_config["params_hash"])
    write_business_rows(conn, rows, write)
    del rows
    return stats


SEEDABLE_BUSINESS_CODES = (REGIME_BIZ_CODE, PATTERN_BIZ_CODE,
                           DIVERGENCE_BIZ_CODE, REVERSAL_BIZ_CODE)


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
            elif biz_code == PATTERN_BIZ_CODE:
                business_stats = build_pattern_business(conn, config, market, write)
            elif biz_code == DIVERGENCE_BIZ_CODE:
                business_stats = build_divergence_business(conn, config, market, write)
            elif biz_code == REVERSAL_BIZ_CODE:
                business_stats = build_reversal_business(conn, config, market, write)
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
            for biz_code in (args.biz or SEEDABLE_BUSINESS_CODES):
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
