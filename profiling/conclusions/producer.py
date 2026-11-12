#!/usr/bin/env python3
"""个股画像 L2/L3 结论生产者（doc/stock_profile_l2_l3.md §4.6 / §9.1）。

接线：compute_profile._compute() 在 ② 逐标签独立事务全部提交之后调用 produce()，
不进 market_scheduler（R5）；规则升级时才由手动脚本
compute_conclusions_rebuild.py 调用 rebuild()（不注册 systemd 服务）。

落表语义（§4.6 事件行，唯一真相源）：
  - 值变       → 关闭旧行 eff_to + 插新行（含完整 conclusion_detail + version）
  - 值不变     → 只 UPDATE 当前有效行的 conclusion_detail 日变量（不动键列）
  - 不发布     → 有关闭当前有效行，无则跳过（不写行）
  - 规则升级   → rebuild() 删光 6 个 tag_code 全部行后重建，所有行统一新 version

资源红线（doc/compute_task_resource_standard.md §2）：
  R1 按股票 500 只/批读 v_daily_quote；R6 每结论一个事务（<5 万行）；
  R7 tag_code=ANY / eff_to 当前态 / trade_date>= 下界；
  R3 峰值由独立 unit 的 MemoryMax=512M 兜底，实测数字随发布写进提交说明。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, timedelta

from psycopg2.extras import Json, execute_values

from db import get_conn
from profiling.conclusions import rules as R

log = logging.getLogger(__name__)

_FAR_FUTURE = "9999-12-31"
_BATCH_CODES = 500          # R1：按股票分批
_MA_LOOKBACK_DAYS = 120     # R7：约覆盖 60 个交易日
_MA_MIN_OBS = 20


# ── 输入加载 ────────────────────────────────────────────────────────
def _all_l1_tags() -> list[str]:
    tags = set()
    for spec in R.RULES.values():
        for item in spec["inputs"]:
            if "tag_code" in item:
                tags.add(item["tag_code"])
    return sorted(tags)


def _load_l1(conn) -> dict:
    """当前有效 L1 快照：{stock_code: {(tag_code, source_col): raw}}。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, tag_code, key_value, num_value "
            "FROM profile.tag_value WHERE eff_to = %s AND tag_code = ANY(%s)",
            (_FAR_FUTURE, _all_l1_tags()),
        )
        rows = cur.fetchall()
    snap: dict = defaultdict(dict)
    for stock, tag, kv, nv in rows:
        snap[stock][(tag, "key_value")] = kv
        snap[stock][(tag, "num_value")] = nv
    return snap


def _load_fcf_sign(conn) -> dict:
    """financial_indicator.free_cash_flow 的符号（只取符号，A/H 口径不同）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (stock_code) stock_code, free_cash_flow "
            "FROM financial_indicator ORDER BY stock_code, report_date DESC"
        )
        rows = cur.fetchall()
    return {s: (1 if v > 0 else -1 if v < 0 else 0)
            for s, v in rows if v is not None}


def _load_ma_alignment(conn, codes: list[str], as_of: date) -> dict:
    """由 v_daily_quote.close 现算 MA20/MA60（视图无 MA 列，daily_trend 只覆盖采集池）。

    按 500 只/批读取近 120 自然日，Python 侧取最近 60 根算均线，避免一次性拉大表。
    """
    lower = as_of - timedelta(days=_MA_LOOKBACK_DAYS)
    out: dict[str, str] = {}
    for i in range(0, len(codes), _BATCH_CODES):
        batch = codes[i:i + _BATCH_CODES]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, trade_date, close FROM v_daily_quote "
                "WHERE stock_code = ANY(%s) AND trade_date >= %s "
                "ORDER BY stock_code, trade_date DESC",
                (batch, lower),
            )
            rows = cur.fetchall()
        by_stock: dict[str, list[float]] = defaultdict(list)
        for stock, _d, close in rows:
            if close is not None and len(by_stock[stock]) < 60:
                by_stock[stock].append(float(close))
        for stock, closes in by_stock.items():
            if len(closes) < _MA_MIN_OBS:
                continue
            ma20 = sum(closes[:20]) / 20
            ma60 = sum(closes) / len(closes)
            last = closes[0]
            if ma20 > ma60 and last > ma20:
                out[stock] = "bullish"
            elif ma20 < ma60 and last < ma20:
                out[stock] = "bearish"
            else:
                out[stock] = "neutral"
    return out


# ── 归一化与判定 ─────────────────────────────────────────────────────
def _cast(raw, item: dict):
    dtype = item["dtype"]
    if raw is None:
        return None
    if dtype == "bool":
        return raw == item.get("match")
    if dtype == "int":
        if raw in item.get("invalid", []) or raw in ("LOSS", "NEGATIVE"):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if dtype == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return raw  # enum / str


def _build_values(spec: dict, stock: str, snap: dict, extras: dict, l2: dict):
    values: dict = {}
    missing: list[str] = []
    for item in spec["inputs"]:
        field = item["field"]
        compute = item.get("compute")
        if "tag_code" in item:
            raw = snap.get(stock, {}).get((item["tag_code"], item["source_col"]))
            value = _cast(raw, item)
        elif compute == "ma_alignment":
            value = extras["ma"].get(stock)
        elif compute == "l2_growth":
            value = l2.get("dim_growth_conclusion", {}).get(stock)
        elif compute == "val_class":
            code = l2.get("dim_valuation_conclusion", {}).get(stock)
            value = R.VAL_CLASS.get(code)
        elif compute == "qal_class":
            code = l2.get("dim_quality_conclusion", {}).get(stock)
            value = R.QAL_CLASS.get(code)
        elif item.get("source") == "financial_indicator":
            value = extras["fcf"].get(stock)
        else:
            value = None
        values[field] = value
        if value is None:
            missing.append(field)
    coverage = 1.0 - len(missing) / len(spec["inputs"])
    return values, coverage, missing


def _derive(spec: dict, values: dict) -> dict:
    """derived_fields[].expr 仅文档说明，这里显式计算（引擎不解析表达式）。"""
    for item in spec["derived_fields"]:
        field = item["field"]
        if field == "pe_gap":
            a, b = values.get("pe_hist_pct"), values.get("pe_ind_pct")
            values[field] = abs(a - b) if a is not None and b is not None else None
        elif field == "pe_gap_dir":
            a, b = values.get("pe_hist_pct"), values.get("pe_ind_pct")
            values[field] = ("hist_lower" if a < b else "hist_higher") \
                if a is not None and b is not None else None
        elif field == "growth_gap":
            a, b = values.get("rev_tier"), values.get("np_tier")
            values[field] = abs(a - b) if a is not None and b is not None else None
    return values


def _judge(spec: dict, code: str, values: dict, coverage: float):
    """返回 (rule_id, result, gates)。result=None 表示整行不发布。"""
    params = spec["params"]
    gates = {"G1": True, "G2": True, "G3": True, "G4": False}

    # G4 业务码豁免 G1/G2（§4.2；亏损股常同时缺两个 PE 分位）
    if spec.get("g4_exempt"):
        gates["G4"] = True
        if values.get("loss_flag") is True:
            return "VAL-00", "loss", gates
        if values.get("negative_flag") is True:
            return "VAL-01", "negative", gates

    if coverage < params["coverage_min"]:
        gates["G1"] = False
        return None, None, gates

    # G2：主锚必须齐全（缺任一即不发布，与 tools/calibrate_conclusion_cutpoints.py 一致）
    anchors = [i["field"] for i in spec["inputs"] if i.get("role") == "anchor"]
    if any(values.get(a) is None for a in anchors):
        gates["G2"] = False
        return None, None, gates

    rule_id, result = R.first_match(spec, values)
    return rule_id, result, gates


def _detail(rule_id, values, coverage, missing, gates, as_of: date,
            with_run: bool, eff_from: date | None = None) -> dict:
    detail = {
        "rule_id": rule_id,
        "coverage": round(coverage, 4),
        "missing_inputs": missing,
        "evidence": {k: v for k, v in values.items() if v is not None},
        "gates": gates,
        "as_of": str(as_of),
    }
    if with_run:
        since = eff_from or as_of
        detail.update({
            "state_raw": None,  # 由调用方填 result
            "since_date": str(since),
            "dwell_days": (as_of - since).days,
            "status": "confirmed",
            "candidate": {"to": None, "progress": 0},
        })
    return detail


# ── 写入（§4.6 事件行）───────────────────────────────────────────────
def _write(conn, code: str, outcomes: dict, as_of: date, version: str) -> dict:
    """每结论一个事务（R6，8460 行 << 5 万行上限）。"""
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code, key_value, eff_from, conclusion_detail "
            "FROM profile.tag_value WHERE tag_code = %s AND eff_to = %s",
            (code, _FAR_FUTURE),
        )
        current = {r[0]: {"kv": r[1], "eff_from": r[2], "detail": r[3] or {}}
                   for r in cur.fetchall()}

        to_close: list[str] = []
        to_insert: list[tuple] = []
        to_refresh: list[tuple] = []
        closed_prev = as_of - timedelta(days=1)

        for stock, (result, detail) in outcomes.items():
            cur_row = current.get(stock)
            if result is None:
                if cur_row:
                    to_close.append(stock)
                continue
            if cur_row is None:
                to_insert.append((stock, result, detail))
            elif cur_row["kv"] != result:
                to_close.append(stock)
                to_insert.append((stock, result, detail))
            else:
                # 值不变：保留判定快照，只刷新日变量
                merged = {**cur_row["detail"], **detail}
                to_refresh.append((stock, merged))

        if to_close:
            execute_values(
                cur,
                f"UPDATE profile.tag_value AS tv SET eff_to = v.eff_to, update_time = NOW() "
                f"FROM (VALUES %s) AS v(stock_code, eff_to) "
                f"WHERE tv.tag_code = '{code}' AND tv.stock_code = v.stock_code "
                f"AND tv.eff_to = '{_FAR_FUTURE}'",
                [(s, closed_prev) for s in to_close],
            )

        if to_insert:
            execute_values(
                cur,
                "INSERT INTO profile.tag_value "
                "(stock_code, tag_code, key_value, conclusion_detail, confidence, "
                " eff_from, eff_to, version) VALUES %s",
                [(s, code, result, Json(detail), 1.0, as_of, _FAR_FUTURE, version)
                 for s, result, detail in to_insert],
            )

        if to_refresh:
            execute_values(
                cur,
                f"UPDATE profile.tag_value AS tv SET conclusion_detail = v.detail::jsonb, "
                f"update_time = NOW() FROM (VALUES %s) AS v(stock_code, detail) "
                f"WHERE tv.tag_code = '{code}' AND tv.stock_code = v.stock_code "
                f"AND tv.eff_to = '{_FAR_FUTURE}'",
                [(s, Json(detail)) for s, detail in to_refresh],
            )

        # tag_run_log：DATA_SOURCES["profile_tag_value"].version_fn 取
        # max(created_at) where status='ok'，漏写则下游 DAG 不重绘（§3.4 R2）
        cur.execute(
            "INSERT INTO profile.tag_run_log "
            "(tag_code, as_of, rows_total, rows_new, rows_closed, duration_ms, "
            " version, status, message) VALUES (%s,%s,%s,%s,%s,%s,%s,'ok',%s)",
            (code, as_of, len(outcomes), len(to_insert), len(to_close),
             int((time.monotonic() - t0) * 1000), version, "conclusion producer"),
        )
    return {"inserted": len(to_insert), "closed": len(to_close),
            "refreshed": len(to_refresh)}


# ── 对外入口 ────────────────────────────────────────────────────────
def produce(as_of: date | None = None, sync_dict: bool = True) -> dict:
    """日常增量：算 4 个 L2 + 2 个 L3，按事件行写回。

    必须在 compute_profile._compute() 的 ② 全部提交之后调用（§9.1 约束 2/3）。
    """
    as_of = as_of or date.today()
    t0 = time.monotonic()
    with get_conn() as conn:
        snap = _load_l1(conn)
        if not snap:
            # G3 数据新鲜度：当日行情未到货 → 整批不发布
            log.warning("结论生产者跳过：L1 快照为空（as_of=%s）", as_of)
            return {"status": "skipped", "reason": "empty_l1_snapshot"}
        extras = {
            "fcf": _load_fcf_sign(conn),
            "ma": _load_ma_alignment(conn, sorted(snap), as_of),
        }
        if sync_dict:
            R.sync_conclusion_dict(conn)

    outcomes: dict[str, dict] = {}
    l2: dict[str, dict] = {}
    for code in R.L2_TAGS + R.L3_TAGS:
        spec = R.RULES[code]
        bucket: dict = {}
        for stock in snap:
            values, coverage, missing = _build_values(spec, stock, snap, extras, l2)
            _derive(spec, values)
            rule_id, result, gates = _judge(spec, code, values, coverage)
            detail = _detail(rule_id, values, coverage, missing, gates, as_of,
                             with_run=(code == "dim_position_state"))
            if "state_raw" in detail:
                detail["state_raw"] = result
            bucket[stock] = (result, detail)
        outcomes[code] = bucket
        if code in R.L2_TAGS:
            l2[code] = {s: r for s, (r, _d) in bucket.items() if r is not None}

    stats = {}
    for code in R.DERIVED_TAGS:
        with get_conn() as conn:
            stats[code] = _write(conn, code, outcomes[code], as_of, R.RULES[code]["version"])
    log.info("结论生产者完成 as_of=%s 耗时=%.1fs 统计=%s",
             as_of, time.monotonic() - t0, stats)
    return {"status": "ok", "as_of": str(as_of), "stats": stats}


def rebuild(as_of: date | None = None) -> dict:
    """规则升级后的全量重建（由手动脚本 compute_conclusions_rebuild.py 调用）。

    ⚠ 已知 PIT 局限（doc §8.5）：L1 当日走 snapshot 原地 UPDATE，历史日才是版本链，
    严格「按 as_of 取当时版本」不可得。故本期重建的是**当前 L1 有效值下的结论历史**，
    与 §4.4.4「as_of 回放得到的是用当前规则回算的历史值」语义一致。
    """
    as_of = as_of or date.today()
    with get_conn() as conn:
        cur = conn.cursor()
        # R6/R7：按 tag_code 实体过滤逐标签删除，每个标签一个事务（<5 万行）
        for code in R.DERIVED_TAGS:
            cur.execute(
                "SELECT count(*) FROM profile.tag_value WHERE tag_code = %s", (code,)
            )
            n = cur.fetchone()[0]
            cur.execute("DELETE FROM profile.tag_value WHERE tag_code = %s", (code,))
            log.info("重建：已删除 %s 历史行 %d 行", code, n)
    log.info("重建：按 as_of=%s 重新生产 6 个结论", as_of)
    return produce(as_of=as_of, sync_dict=True)
