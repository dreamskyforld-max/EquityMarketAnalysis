#!/usr/bin/env python3
"""申万一级行业成分主数据采集 —— 写入 regime.sector_constituent（PIT 版本链）。

第④层「行业配置」阶段 2 的数据层，口径见 doc/industry_allocation_feasibility.md
§2.2.1 / §4.2。

为什么是版本链而不是当前快照：
    源 `akshare.index_component_sw(symbol=<sw_code>)` 只返回**当前成分** +
    「计入日期」，**没有剔除记录**。主报告 §9.4 把「用今日成分股算历史」列为最高等级
    风险。因此本表按 (sector_code, stock_code, eff_from) 存版本链，每交易日快照与
    active 行对比，消失的成分把 eff_to 关在快照日，逐日累积出可 PIT 取用的历史。

阶段 0 探测结论（2026-11-14，tools/probe_sector_constituent.py）：
    · 31 个申万一级行业接口全部可用，合计 5,222 行 / 10.3s；
    · `计入日期` 最小一律 2021-12-13（申万 2021 版分类启用日）→ **有效起点下限**；
    · 完整历史变动源 `stock_industry_clf_hist_sw` 返回 HTTP 508，不可得；
    · 决策 A：自下而上指标只出 2021-12-13 起读数；决策 B：权重只落本表，
      **不回写 public.stock_sector**（后者是证监会/富途分类，口径不同且无生效日）。

源字段（AKShare 1.18.60）：序号 / 证券代码（纯 6 位无前缀）/ 证券名称 /
    最新权重（**百分数**，单行业合计≈100）/ 计入日期（datetime.date）。

用法:
    python3 get_sector_constituent.py                 # 采集（调度用，交易日 18:05）
    python3 get_sector_constituent.py --dry-run
    python3 get_sector_constituent.py --date 2026-11-13
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from db import bulk_upsert, get_conn

TABLE = "regime.sector_constituent"
CHANGE_TABLE = "regime.sector_constituent_change"
SOURCE = "akshare:index_component_sw"
MARKET = "CN"
CLASSIFICATION = "SW_L1"
PIT_START = dt.date(2021, 12, 13)      # 决策 A：2021 版申万分类启用日
WEIGHT_EPS = 0.01                      # 权重变化 < 0.01 个百分点视为噪声
MIN_SECTORS = 25                       # 31 个行业至少成功 25 个
MIN_ROWS = 3000                        # 实测 5,222 行

# 6 位纯数字 → 项目股票代码前缀。规则照 get_analyst_forecast.py:51-57
# （必须含 BJ 92/83/87/88/43 段；get_margin_balance 的旧写法会静默丢北交所）。
_BJ_PREFIX = ("92", "83", "87", "88", "43")
_SH_PREFIX = ("60", "68", "90", "11", "13", "50", "51", "58")


def to_stock_code(raw) -> str | None:
    """6 位纯数字 → `SH.600000` / `SZ.000001` / `BJ.920001`。

    ⚠ 必须 zfill(6)：源若把 `000505` 推断成数值 505，直接 str() 会得到 '505'。
    """
    s = str(raw).strip()
    if s.endswith(".0"):          # pandas 把整列推断成 float 时的痕迹
        s = s[:-2]
    if not s.isdigit():
        return None
    s = s.zfill(6)
    if len(s) != 6:
        return None
    p = s[:2]
    if p in _BJ_PREFIX:
        return "BJ." + s
    if p in _SH_PREFIX:
        return "SH." + s
    return "SZ." + s


def _recent_cn_trading_date() -> dt.date:
    """最近 A 股交易日（trading_calendar）；查不到回落今天。"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT max(cal_date) FROM public.trading_calendar "
                            "WHERE market='CN' AND is_open AND cal_date <= CURRENT_DATE")
                d = cur.fetchone()[0]
                if d:
                    return d
    except Exception as e:
        print(f"  ⚠️ 读取 A 股交易日失败（回落今天）: {e}")
    return dt.date.today()


def _sw_codes(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT sw_code FROM regime.sector_daily ORDER BY 1")
        return [r[0] for r in cur.fetchall()]


def _fetch(sw_codes: list[str]) -> tuple[list[dict], list[str]]:
    """拉取 31 个行业成分。返回 (行列表, 失败行业代码)。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    rows, failed = [], []
    for code in sw_codes:
        try:
            df = ak.index_component_sw(symbol=code)
        except Exception as e:
            print(f"  ⚠️ {code} 拉取失败: {type(e).__name__}: {str(e)[:100]}")
            failed.append(code)
            continue
        if df is None or df.empty:
            failed.append(code)
            continue
        n_ok = 0
        for _, r in df.iterrows():
            sc = to_stock_code(r.get("证券代码"))
            if sc is None:
                continue
            w = pd.to_numeric(r.get("最新权重"), errors="coerce")
            d = r.get("计入日期")
            if pd.isna(d):
                eff = PIT_START
            else:
                eff = d.date() if isinstance(d, pd.Timestamp) else d
                eff = max(eff, PIT_START)      # 决策 A：不得早于 2021-12-13
            rows.append(dict(sector_code=str(code), stock_code=sc,
                             eff_from=eff,
                             weight=None if pd.isna(w) else round(float(w), 4)))
            n_ok += 1
        print(f"    {code}: {n_ok} 只")
    return rows, failed


def _load_active(conn) -> dict:
    """当前有效成分 {(sector_code, stock_code): (eff_from, weight)}。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sector_code, stock_code, eff_from, weight FROM regime.sector_constituent "
            "WHERE market=%s AND classification=%s AND eff_to IS NULL",
            (MARKET, CLASSIFICATION))
        return {(r[0], r[1]): (r[2], float(r[3]) if r[3] is not None else None)
                for r in cur.fetchall()}


def diff_snapshot(active: dict, snap: list[dict], snapshot_date: dt.date) -> dict:
    """对比当前有效行与本次快照，生成 ADD / REMOVE / WEIGHT 三类变更（纯函数，可单测）。

    权重微小波动（< WEIGHT_EPS）只更新当前行，不关旧开新 —— 否则每次快照都会
    因浮点噪声产生数千行版本，把版本链淹没。
    """
    cur = {}
    for r in snap:
        k = (r["sector_code"], r["stock_code"])
        cur[k] = (r["eff_from"], r["weight"])

    add = [dict(sector_code=k[0], stock_code=k[1], eff_from=v[0], weight=v[1],
                change_type="ADD", old_weight=None, new_weight=v[1])
           for k, v in cur.items() if k not in active]
    remove = [dict(sector_code=k[0], stock_code=k[1], eff_from=None, weight=None,
                   change_type="REMOVE", old_weight=v[1], new_weight=None)
              for k, v in active.items() if k not in cur]
    weight_chg, tiny = [], []
    for k, (eff, w) in cur.items():
        if k not in active:
            continue
        old_eff, old_w = active[k]
        if w is None or old_w is None:
            continue
        item = dict(sector_code=k[0], stock_code=k[1], eff_from=eff, weight=w,
                    change_type="WEIGHT", old_weight=old_w, new_weight=w)
        if abs(w - old_w) >= WEIGHT_EPS:
            weight_chg.append(item)
        elif w != old_w:
            tiny.append((k, w))
    return dict(add=add, remove=remove, weight=weight_chg, tiny=tiny,
                snapshot_date=snapshot_date, current=cur)


def _apply(conn, diff: dict, dry_run: bool = False) -> dict:
    """写版本链 + 变更日志。返回计数。"""
    snap = diff["snapshot_date"]
    counts = {k: len(diff[k]) for k in ("add", "remove", "weight", "tiny")}
    if dry_run:
        return counts

    # ① REMOVE：关闭 eff_to
    for r in diff["remove"]:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE regime.sector_constituent SET eff_to=%s "
                "WHERE market=%s AND classification=%s AND sector_code=%s "
                "AND stock_code=%s AND eff_to IS NULL",
                (snap, MARKET, CLASSIFICATION, r["sector_code"], r["stock_code"]))

    # ② WEIGHT（显著）：关旧行 + 开新行，生效日 = 快照日
    for r in diff["weight"]:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE regime.sector_constituent SET eff_to=%s "
                "WHERE market=%s AND classification=%s AND sector_code=%s "
                "AND stock_code=%s AND eff_to IS NULL",
                (snap, MARKET, CLASSIFICATION, r["sector_code"], r["stock_code"]))
    new_rows = diff["add"] + [
        dict(r, eff_from=snap) for r in diff["weight"]]

    # ③ 微小权重波动：只更新当前行，不动版本链
    for (sec, stock), w in diff["tiny"]:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE regime.sector_constituent SET weight=%s "
                "WHERE market=%s AND classification=%s AND sector_code=%s "
                "AND stock_code=%s AND eff_to IS NULL",
                (w, MARKET, CLASSIFICATION, sec, stock))

    if new_rows:
        rows = [dict(market=MARKET, classification=CLASSIFICATION,
                     sector_code=r["sector_code"], stock_code=r["stock_code"],
                     eff_from=r["eff_from"], eff_to=None, weight=r["weight"],
                     source=SOURCE) for r in new_rows]
        bulk_upsert(conn, TABLE, rows,
                    conflict_cols=["market", "classification", "sector_code",
                                   "stock_code", "eff_from"],
                    skip_null_updates=True)

    # ④ 变更日志（幂等：同一快照日重跑 ON CONFLICT 覆盖）
    log_rows = []
    for kind in ("add", "remove", "weight"):
        for r in diff[kind]:
            log_rows.append(dict(
                snapshot_date=snap, market=MARKET, classification=CLASSIFICATION,
                sector_code=r["sector_code"], stock_code=r["stock_code"],
                change_type=r["change_type"], old_weight=r["old_weight"],
                new_weight=r["new_weight"], eff_from=r["eff_from"]))
    if log_rows:
        bulk_upsert(conn, CHANGE_TABLE, log_rows,
                    conflict_cols=["snapshot_date", "market", "classification",
                                   "sector_code", "stock_code", "change_type"])
    conn.commit()
    return counts


def run(codes=None, ctx=None, snapshot_date: dt.date = None, dry_run: bool = False):
    """采集入口。签名兼容 collector_runtime.run_module 的 run(codes, ctx=...)。"""
    from regime_schema import ensure_sector_schema

    with get_conn() as conn:
        ensure_sector_schema(conn)          # 幂等自举（新服务器兜底）
        snap = snapshot_date or _recent_cn_trading_date()
        sw_codes = _sw_codes(conn)
        print("=" * 68)
        print(f"申万一级行业成分主数据（{SOURCE}）→ {TABLE}")
        print(f"归属交易日: {snap}　行业数: {len(sw_codes)}　PIT 起点: {PIT_START}")
        print("=" * 68)
        rows, failed = _fetch(sw_codes)

        # ── 质量闸门：源异常 / 大面积失败时拒绝写库（避免把清单塌陷成"成分调整"）──
        sectors_ok = len({r["sector_code"] for r in rows})
        print(f"  源返回 {len(rows)} 行 / {sectors_ok} 个行业；失败 {len(failed)} 个")
        if sectors_ok < MIN_SECTORS or len(rows) < MIN_ROWS:
            raise RuntimeError(
                f"成分清单疑似塌陷：{sectors_ok} 个行业 / {len(rows)} 行 "
                f"（闸门 {MIN_SECTORS} / {MIN_ROWS}），拒绝写库")
        dup = len(rows) - len({(r["sector_code"], r["stock_code"]) for r in rows})
        if dup:
            print(f"  ⚠️ 同一行业内重复代码 {dup} 行（bulk_upsert 会按业务键合并）")
        wsum = {}
        for r in rows:
            if r["weight"] is not None:
                wsum[r["sector_code"]] = wsum.get(r["sector_code"], 0.0) + r["weight"]
        bad_w = {k: round(v, 2) for k, v in wsum.items() if not (90 <= v <= 110)}
        if bad_w:
            print(f"  ⚠️ 权重合计异常的行业: {bad_w}")

        active = _load_active(conn)
        diff = diff_snapshot(active, rows, snap)
        print(f"  库内 active {len(active)} 行 → ADD {len(diff['add'])} / "
              f"REMOVE {len(diff['remove'])} / WEIGHT {len(diff['weight'])} / "
              f"微小波动 {len(diff['tiny'])}")
        counts = _apply(conn, diff, dry_run=dry_run)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE eff_to IS NULL), "
                        "min(eff_from), max(eff_from) FROM regime.sector_constituent "
                        "WHERE market=%s AND classification=%s",
                        (MARKET, CLASSIFICATION))
            total, n_active, mn, mx = cur.fetchone()
        print(f"  {'--dry-run：未写库' if dry_run else '✅ 写入完成'}；"
              f"库内版本链 {total:,} 行 / active {n_active:,} 行（{mn} ~ {mx}）")
        return dict(snapshot_date=str(snap), sectors=sectors_ok, rows=len(rows),
                    failed=failed, active_in_db=n_active, **counts)


def _main() -> int:
    ap = argparse.ArgumentParser(description="申万一级行业成分主数据采集（PIT 版本链）")
    ap.add_argument("--date", default=None, help="快照日 YYYY-MM-DD（默认最近 A 股交易日）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    out = run(snapshot_date=dt.date.fromisoformat(a.date) if a.date else None,
              dry_run=a.dry_run)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
