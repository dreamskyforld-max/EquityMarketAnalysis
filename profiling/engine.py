#!/usr/bin/env python3
"""标签计算引擎：算快照 → 与库中当前值 diff → 版本化写入

统一计算接口
------------
每个标签函数是纯函数：

    fn(as_of: date) -> pd.DataFrame

返回列固定为：

    stock_code   股票代码（如 HK.00700）
    key_value    离散取值（筛选用，必填）
    num_value    连续取值（回测用，可空）
    confidence   置信度（可空，缺省 1.0）

引擎负责其余一切：校验、去重、与库中当前值 diff、关闭旧版本、写运行日志。

版本化 diff 规则
----------------
对 (stock_code, key_value) 这个粒度做比较：

    库中无、本次有   → INSERT 新行（eff_from = as_of, eff_to = 9999-12-31）
    库中有、本次无   → 关闭：eff_to = as_of - 1
    两边都有但值变了 → 关闭旧行 + 插入新行（产生新版本）
    两边都有且值相同 → 不动（不产生无意义的版本）

同一天重复计算（eff_from == as_of）时不关闭旧行，而是原地 UPDATE / DELETE——
同日内的修正是「改错」而非「历史变更」，不该留下 eff_to < eff_from 的无效区间。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

import pandas as pd
from psycopg2 import extras

from . import registry
from .schema import ensure_partition

log = logging.getLogger(__name__)

REQUIRED_COLS = ["stock_code", "key_value"]
OPTIONAL_COLS = {"num_value": None, "confidence": 1.0}
FAR_FUTURE = date(9999, 12, 31)

# 各更新频率的最小重算间隔（天）。
# 没有这个门禁，静态标签每天重算都会因「num_value 随时间微变」（如上市年限 +1 天）
# 而关闭旧行 + 插入新行，导致版本无限膨胀——这正好是存储量失控的常见原因。
# 日频标签（daily）间隔 1 天即每交易日都重算；低频标签只在间隔到达后重算，
# 需要立即刷新时用 force=True。
_FREQ_MIN_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "event": 30,      # 事件驱动：无事件时不重算，用 30 天兜底
    "static": 3650,   # 准静态：基本只算一次，数据补齐后用 force 刷新
}


def _last_eff_from(conn: Any, tag_code: str, as_of: date) -> date | None:
    """该标签在 as_of 之前最近一次写入新版本的日期。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(eff_from) FROM profile.tag_value "
            "WHERE tag_code = %s AND eff_from <= %s",
            (tag_code, as_of),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _normalize(df: pd.DataFrame, meta: Any) -> pd.DataFrame:
    """校验并规整标签函数的返回：补齐缺失列、去空、去重。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["stock_code", "key_value", "num_value", "confidence"])

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"[{meta.code}] 返回缺少必需列: {missing}")

    out = df.copy()
    for col, default in OPTIONAL_COLS.items():
        if col not in out.columns:
            out[col] = default
        else:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    # 先用原始类型判缺失（pd.NA / np.nan / None 一网打尽），再转字符串。
    # 不能反过来：astype(str) 后空值会变成 'nan' / '<NA>' / 'NaN' 等，
    # 具体拼写随 pandas 版本而变，靠字符串黑名单匹配必然漏（实测漏过 'NaN'）。
    before = len(out)
    out = out[out["stock_code"].notna() & out["key_value"].notna()]
    out = out.copy()
    out["stock_code"] = out["stock_code"].astype(str).str.strip()
    out["key_value"] = out["key_value"].astype(str).str.strip()
    out = out[(out["stock_code"] != "") & (out["key_value"] != "")]
    out = pd.DataFrame(out)
    dropped = before - len(out)
    if dropped:
        log.warning("[%s] 丢弃 %d 行空 stock_code/key_value", meta.code, dropped)

    # 同一 (股票, 取值) 只保留一行（多重归属标签本就靠不同 key_value 区分）
    out = pd.DataFrame(out.drop_duplicates(subset=["stock_code", "key_value"], keep="first"))

    if not meta.multi_value:
        dup = int(out["stock_code"].duplicated().sum())
        if dup:
            raise ValueError(
                f"[{meta.code}] 声明为单值标签（multi_value=False），但有 {dup} 只股票返回了多个取值"
            )

    if meta.confidence_req and bool(out["confidence"].isna().any()):
        raise ValueError(f"[{meta.code}] 声明必须带置信度，但存在空值")

    return pd.DataFrame(out[["stock_code", "key_value", "num_value", "confidence"]])


def _load_current(conn: Any, tag_code: str, as_of: date) -> dict[tuple[str, str], dict[str, Any]]:
    """读出 as_of 当天有效的全部行。

    返回 {(stock_code, key_value): {"id":..., "eff_from":..., "num_value":..., "confidence":...}}
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, stock_code, key_value, num_value, confidence, eff_from
            FROM profile.tag_value
            WHERE tag_code = %s AND eff_from <= %s AND eff_to >= %s
            """,
            (tag_code, as_of, as_of),
        )
        return {
            (r[1], r[2]): {
                "id": r[0],
                "key_value": r[2],
                "num_value": r[3],
                "confidence": r[4],
                "eff_from": r[5],
            }
            for r in cur.fetchall()
        }


def _same_value(old: dict[str, Any], new_num: Any, new_conf: Any) -> bool:
    """浮点比较留 1e-9 容差，避免无意义的新版本。"""
    o_num, o_conf = old["num_value"], old["confidence"]
    # 显式分支而非 `not (new_num is None or pd.isna(new_num))`：
    # pd.isna 返回 Any，用它做条件无法让类型检查器收窄 new_num
    if new_num is None or pd.isna(new_num):
        if o_num is not None:
            return False
    else:
        if o_num is None:
            return False
        if abs(float(o_num) - float(new_num)) > 1e-9:
            return False
    old_conf = 1.0 if o_conf is None else float(o_conf)
    new_conf_val = 1.0 if new_conf is None else float(new_conf)
    return abs(old_conf - new_conf_val) <= 1e-9


def compute_tag(conn: Any, tag_code: str, as_of: date | None = None,
                dry_run: bool = False, force: bool = False,
                mode: str = "auto") -> dict[str, Any]:
    """计算单个标签并落库。返回结果统计字典。

    mode 决定「标签值变了怎么写」：
        snapshot  原地 UPDATE，只维护一份当前值（eff_from 保持不变，update_time 刷新）。
                  用于日常跑当天——不产生一天期的历史版本，避免写满全历史。
        version   关闭旧行 + 插入新行，完整保留版本链，支持 as_of 时间旅行。
                  用于补算历史日期或需要留痕的场合。
        auto      as_of 是今天 → snapshot；as_of 是历史日期 → version。

    force=True 时忽略 update_freq 门禁，强制重算（补完依赖数据后刷新标签用）。
    """
    meta = registry.get(tag_code)
    if meta is None:
        raise KeyError(f"未注册的标签: {tag_code}")

    as_of = as_of or date.today()
    result: dict[str, Any] = {
        "tag_code": tag_code,
        "as_of": as_of,
        "rows_total": 0,
        "rows_new": 0,
        "rows_closed": 0,
        "status": "ok",
        "message": "",
    }

    if meta.status != registry.ACTIVE:
        result["status"] = "skipped"
        result["message"] = f"状态={meta.status}：{meta.blocked_reason or ''}"
        log.info("[%s] 跳过：%s", tag_code, result["message"])
        _write_log(conn, meta, result, 0)
        return result

    # 更新频率门禁：低频标签未到重算间隔则跳过（跳过不写运行日志，避免日志膨胀）
    gap = _FREQ_MIN_DAYS.get(meta.update_freq, 1)
    if not force and not dry_run and gap > 1:
        last = _last_eff_from(conn, tag_code, as_of)
        if last is not None and (as_of - last).days < gap:
            result["status"] = "skipped"
            result["message"] = (
                f"update_freq={meta.update_freq}（间隔 {gap} 天），"
                f"上次版本 {last} 距今 {(as_of - last).days} 天，跳过（用 --force 强制重算）"
            )
            log.info("[%s] 跳过：%s", tag_code, result["message"])
            return result

    if mode not in ("auto", "snapshot", "version"):
        raise ValueError(f"mode 非法: {mode}（可选 auto / snapshot / version）")
    if mode == "auto":
        mode = "snapshot" if as_of == date.today() else "version"
    result["mode"] = mode

    t0 = time.monotonic()
    try:
        df = _normalize(registry.get_func(tag_code)(as_of), meta)
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"计算失败: {type(e).__name__}: {e}"
        log.error("[%s] %s", tag_code, result["message"])
        _write_log(conn, meta, result, int((time.monotonic() - t0) * 1000))
        return result

    result["rows_total"] = len(df)

    # 枚举落库校验：key 必须在声明的值域内，否则字典与数据漂移
    # （实测踩过：att_southbound_trend 字典声明 increase/decrease，函数却落 rise/fall）
    # 引用型值域（table:xxx）来自外部表，无法静态校验，跳过
    if meta.enum_values:
        valid_keys = set(meta.enum_values.keys())
        bad = sorted(set(df["key_value"]) - valid_keys)
        if bad:
            result["status"] = "error"
            result["message"] = (
                f"落库 key 不在 enum_values 内: {bad}（合法: {sorted(valid_keys)}）——"
                f"修正计算函数或字典后再算"
            )
            log.error("[%s] %s", tag_code, result["message"])
            _write_log(conn, meta, result, int((time.monotonic() - t0) * 1000))
            return result

    ensure_partition(conn, as_of.year)

    cur_map = _load_current(conn, tag_code, as_of)
    # 用 tolist() 逐列取值再 zip，比 itertuples 更快且类型明确
    new_map = {
        (s, k): (n, c)
        for s, k, n, c in zip(
            df["stock_code"].tolist(),
            df["key_value"].tolist(),
            df["num_value"].tolist(),
            df["confidence"].tolist(),
        )
    }

    def _clean(num, conf):
        """清洗单行的连续值与置信度（None/NaN 归位）。"""
        n = None if num is None or pd.isna(num) else float(num)
        return n, (1.0 if conf is None or pd.isna(conf) else float(conf))

    to_insert: list[tuple[str, str, float | None, float]] = []       # (stock, key, num, conf)
    to_update: list[tuple[int, str, float | None, float]] = []       # (id, key, num, conf) 原地改写
    to_close: list[tuple[int]] = []                                  # 跨天：关闭旧版本
    to_delete: list[tuple[int]] = []                                 # 同一天插入又消失：直接删

    if mode == "snapshot" and not meta.multi_value:
        # ── 单值标签的快照：按 stock 粒度原地改写整行 ──────────────────
        # 关键：档位类标签的 key_value 天天漂移（股票从档 3 漂到档 4），
        # 若按 (stock, key) 匹配，key 一变就匹配不到旧行 → close+insert，
        # 「不产生历史版本」就成了一句空话。单值标签的语义是「该股票当前的
        # 最新状态」，所以 key/num 都直接 UPDATE 原行，eff_from 保持不变。
        cur_by_stock = {s: row for (s, _k), row in cur_map.items()}
        new_by_stock = {s: (k, *_clean(n, c)) for (s, k), (n, c) in new_map.items()}

        for s, (k, n, c) in new_by_stock.items():
            old = cur_by_stock.get(s)
            if old is None:
                to_insert.append((s, k, n, c))
            elif old["key_value"] != k or not _same_value(old, n, c):
                to_update.append((old["id"], k, n, c))
        for s, old in cur_by_stock.items():
            if s not in new_by_stock:
                if old["eff_from"] == as_of:
                    to_delete.append((old["id"],))
                else:
                    to_close.append((old["id"],))
    else:
        # ── 多重归属标签（任何模式）/ 版本模式的单值标签：按 (stock, key) diff ──
        # 多重归属的 key 集合变化（概念增删）本身就是事件，close+insert 是正确语义
        for key, (num, conf) in new_map.items():
            n, c = _clean(num, conf)
            old = cur_map.get(key)
            if old is None:
                to_insert.append((key[0], key[1], n, c))
            elif not _same_value(old, n, c):
                if old["eff_from"] == as_of:
                    to_update.append((old["id"], key[1], n, c))
                else:
                    to_close.append((old["id"],))
                    to_insert.append((key[0], key[1], n, c))
        for key, old in cur_map.items():
            if key not in new_map:
                if old["eff_from"] == as_of:
                    to_delete.append((old["id"],))
                else:
                    to_close.append((old["id"],))

    close_day = as_of - timedelta(days=1)
    result["rows_new"] = len(to_insert)
    result["rows_closed"] = len(to_close) + len(to_delete)

    log.info(
        "[%s] as_of=%s 计算 %d 行 → 新增 %d，关闭 %d，同日修正 %d，删除 %d",
        tag_code, as_of, len(df), len(to_insert), len(to_close), len(to_update), len(to_delete),
    )

    if dry_run:
        result["message"] = "dry-run，未写入"
        result["duration_ms"] = int((time.monotonic() - t0) * 1000)
        result["_to_insert"] = to_insert
        result["_to_close"] = to_close
        result["_to_update"] = to_update
        result["_to_delete"] = to_delete
        return result

    with conn.cursor() as cur:
        if to_insert:
            extras.execute_values(
                cur,
                """
                INSERT INTO profile.tag_value
                    (stock_code, tag_code, key_value, num_value, confidence, eff_from, eff_to, version)
                VALUES %s
                ON CONFLICT (stock_code, tag_code, key_value, eff_from) DO UPDATE SET
                    num_value = EXCLUDED.num_value,
                    confidence = EXCLUDED.confidence,
                    version = EXCLUDED.version
                """,
                [
                    (s, tag_code, k, n, c, as_of, FAR_FUTURE, meta.version)
                    for s, k, n, c in to_insert
                ],
            )
        if to_update:
            # 原地改写（快照模式 / 同日修正）：key_value/num_value/confidence 一并更新，
            # eff_from 保持不变 → 不产生新版本。
            # num/conf 必须显式 cast：VALUES 里的 NULL 会被 PG 推断为 text，直接赋给
            # double precision 列报 DatatypeMismatch
            extras.execute_values(
                cur,
                "UPDATE profile.tag_value AS t "
                "SET key_value = v.kv, num_value = v.num::double precision, "
                "    confidence = v.conf::real, update_time = NOW() "
                "FROM (VALUES %s) AS v(id, kv, num, conf) WHERE t.id = v.id",
                to_update,
            )
        if to_close:
            # 跨天：关闭旧版本，保留可回溯的历史
            extras.execute_values(
                cur,
                "UPDATE profile.tag_value AS t SET eff_to = v.eff_to, update_time = NOW() "
                "FROM (VALUES %s) AS v(id, eff_to) WHERE t.id = v.id",
                [(i[0], close_day) for i in to_close],
            )
        if to_delete:
            # 同一天插入又消失：直接删除，不留 eff_to < eff_from 的无效区间
            extras.execute_values(
                cur,
                "DELETE FROM profile.tag_value AS t USING (VALUES %s) AS v(id) WHERE t.id = v.id",
                to_delete,
            )

    result["duration_ms"] = int((time.monotonic() - t0) * 1000)
    _write_log(conn, meta, result, result["duration_ms"])
    return result


def _write_log(conn: Any, meta: Any, result: dict[str, Any], duration_ms: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO profile.tag_run_log
                (tag_code, as_of, rows_total, rows_new, rows_closed, duration_ms, version, status, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                meta.code, result["as_of"], result["rows_total"], result["rows_new"],
                result["rows_closed"], duration_ms, meta.version, result["status"],
                result.get("message", ""),
            ),
        )


def compute_domain(conn: Any, domain_prefix: str, as_of: date | None = None,
                   dry_run: bool = False, force: bool = False,
                   mode: str = "auto") -> list[dict[str, Any]]:
    """计算某个域（按域名称匹配，如 '规模属性'）下的全部标签。"""
    metas = registry.by_domain(domain_prefix)
    if not metas:
        log.warning("域前缀 %r 下没有已注册标签", domain_prefix)
    return [
        compute_tag(conn, m.code, as_of=as_of, dry_run=dry_run, force=force, mode=mode)
        for m in metas
    ]


def compute_all(conn: Any, as_of: date | None = None,
                dry_run: bool = False, force: bool = False,
                mode: str = "auto") -> list[dict[str, Any]]:
    return [
        compute_tag(conn, m.code, as_of=as_of, dry_run=dry_run, force=force, mode=mode)
        for m in registry.all_tags()
    ]
