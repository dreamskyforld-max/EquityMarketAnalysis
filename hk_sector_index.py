#!/usr/bin/env python3
"""第④层 阶段 3 · HK 行业层数据底座（D4）：自算行业指数 + 行业日聚合。

对应 doc/industry_allocation_feasibility.md §2.2 D4 / §3.2 M8 / §4.2 阶段 3。

HK 没有官方行业指数序列（CN 侧是 31 个申万一级 `daily_benchmark` / `regime.sector_daily`），
本模块从 `hk_daily_quote` × `stock_sector`（113 个恒生三级 `HK.LIST*` 行业）自下而上合成，
输出 `industry.hk_index_daily`（第 27 号表），供 `sector_layer_hk.py` 计算 L1/L2/L3。

【2026-11-14 实测驱动的三条口径变更，均已写进字典 remark / 表注释】
  ① M8 原文「流通市值加权」不可得：`circular_market_val` 2000-2023 全 NULL、
     2024-05-14 起仅 65 个交易日有值；`turnover_rate` 同样只有 75 日。
     → 加权版改用 `total_market_val`（2002 起覆盖率 97%+），等权版 2000-01-03 起。
  ② 成分只有 `stock_sector` 当前快照（无 as_of / 无 PIT / weight 全 NULL），
     → 全部自下而上聚合含**幸存者偏差**，消费端必须透出 pit_capable=false / confidence=low。
  ③ 113 个恒生三级行业 vs CN 31 个申万一级，粒度细 3.6 倍 → 不同构，
     只各自看分位，禁止混合分位 / 混合排名。

【已知限制 · 两版指数极端差异（2026-11-14 实测，勿当作 bug 静默改口径）】
  `--verify` 的两版差异报告显示：等权与市值加权日收益相关系数约 0.63、
  约 23% 的行业日方向相反；最大差异出现在**成分数很少**的行业
  （如 HK.LIST1089 仅 3 只成分，等权 +96.52% / 加权 −0.15%），
  小成分股的极端 change_pct（疑似除权/合股未复权或新股首日）会主导等权版。
  本期**不静默截尾、不改值**：`n_constituent` 随行透出，消费方按成分数自行门禁；
  是否对个股收益做 winsorize 或加最小成分数门槛，列为阶段 4 待排查项。

【资源规范 R1/R3/R6/R7】
  · `hk_daily_quote` 全史 844 万行，绝不整段入进程：按月 × 按股票(1000 只/批)双层分批，
    服务端游标 + dtype 瘦身 + 充分统计量（sum / 平方和 / count）跨批无损合并；
  · 生成即写，单事务 upsert ≤2000 行、逐批 commit；
  · 独立 systemd oneshot + timer（`sector-layer-hk`），**不挂 market_scheduler**（R4/R5）。

CLI:
    python3 hk_sector_index.py --dry-run                 # 小窗口试算（不写库）
    python3 hk_sector_index.py --start 2026-10-01 --end 2026-11-13
    python3 hk_sector_index.py --backfill --dry-run      # 全量 2000 起预演
    python3 hk_sector_index.py --verify                  # 与 SECTOR.UP_RATIO HK 等权合成一致性报告
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import sys
import time

import numpy as np
import pandas as pd

from db import bulk_upsert, get_conn, read_sql
from log_utils import setup_logger
from profiling.tags._base import _read_sql_stream
from sector_layer import _clean_rows   # 复用 NaN/Inf → None 护栏（§7.6 事故）

log = setup_logger("hk_sector_index")

# 口径标识（消费端按此组合过滤，禁止与 CN / SW_L1 混排）
MARKET = "HK"
EQ_START = dt.date(2000, 1, 3)          # hk_daily_quote 实测起点
CAP_START = dt.date(2002, 1, 1)         # total_market_val 2002 起覆盖率 97.5%+
BACKFILL_START = EQ_START
MODEL_VERSION = "hki1.0"
BASE_INDEX = 1000.0

MIN_AMOUNT = 100_000                    # 与 market_state_daily.HK_MIN_AMOUNT 一致（仙股门槛）
WARMUP_DAYS = 120                       # rolling(60) + shift(20) 预热
MIN_COVERAGE = 0.6                      # 复用 market_state_daily._coverage_gate 实测标定
BATCH_CODES = 1000                      # R3：峰值与 844 万行总规模脱钩
STREAM_CHUNK = 50_000                   # 服务端游标每批行数
WRITE_BATCH = 2000                      # R6：单事务 ≤2000 行
INCREMENTAL_DAYS = 45
REWIND_DAYS = 3

# 批内 dtype 瘦身（资源规范 §3）：object 56~68B/单元格 → category ~4B
# ⚠ amount / total_market_val 保持 float64：金额可达 1e11，float32 只有 7 位有效数字
_QUOTE_DTYPES = {"stock_code": "category", "trade_date": "datetime64[ns]",
                 "close": "float32", "prev_close": "float64", "change_pct": "float32",
                 "amount": "float64", "total_market_val": "float64",
                 "pe_ttm_ratio": "float32", "pb_ratio": "float32",
                 "dividend_ratio_ttm": "float32"}

# 全部 SQL 均带时间下界或实体过滤（R7）。
# ⚠ 刻意不用 LIKE 通配符判 HK 前缀：字面 % 在 psycopg2 参数化 SQL 里会被误判为
#   格式符（项目已多次踩中 IndexError），改用 left(col,3)='HK.' 从根上规避。
_CONSTITUENT_SQL = """
    SELECT DISTINCT sector_code, sector_name, stock_code
    FROM stock_sector
    WHERE sector_type = 'INDUSTRY'
      AND left(stock_code, 3) = 'HK.'
      AND left(sector_code, 3) = 'HK.'
"""
_QUOTE_SQL = """
    SELECT trade_date, stock_code, close, prev_close, change_pct, amount,
           total_market_val, pe_ttm_ratio, pb_ratio, dividend_ratio_ttm
    FROM hk_daily_quote
    WHERE trade_date BETWEEN %s AND %s AND stock_code = ANY(%s)
"""
_GGT_SQL = """
    SELECT trade_date, stock_code, est_net_inflow
    FROM daily_ggt_hold
    WHERE trade_date BETWEEN %s AND %s AND est_net_inflow IS NOT NULL
"""

# 可跨批相加的充分统计量（sum / 平方和 / count），保证「整批 == 分批合并」逐值相等
_SS_COLS = ["n_all", "n_eq", "s_eq", "s_wret", "s_w", "n_cap", "s_amount",
            "s_up", "c_up", "s_above", "c_above", "s_high", "s_low",
            "s_ret", "s_ret2", "c_ret",
            "s_pe_w", "s_wpe", "c_pe", "s_pb_w", "s_wpb", "c_pb",
            "s_dy_w", "s_dyw", "c_dy"]


def _as_dt(s: pd.Series) -> pd.Series:
    """统一 datetime64[ns]：pandas 3.0 不同来源可能解析成 s/us 精度。"""
    return pd.to_datetime(s, errors="coerce").astype("datetime64[ns]")


def _month_ranges(start: dt.date, end: dt.date):
    s = dt.date(start.year, start.month, 1)
    while s <= end:
        e = (dt.date(s.year + 1, 1, 1) - dt.timedelta(days=1)
             if s.month == 12 else
             dt.date(s.year, s.month + 1, 1) - dt.timedelta(days=1))
        yield max(s, start), min(e, end)
        s = e + dt.timedelta(days=1)


def load_constituents(conn) -> pd.DataFrame:
    """当前成分快照（2,771 行 / 113 行业）。⚠ 无 PIT，历史回算含幸存者偏差。"""
    df = read_sql(conn, _CONSTITUENT_SQL)
    if df.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "stock_code"])
    df["stock_code"] = df["stock_code"].astype(str)
    return df


def _quote_partial(q: pd.DataFrame, cmap: pd.DataFrame,
                   month_start: dt.date, month_end: dt.date) -> pd.DataFrame:
    """单个股票批次 → 按 (trade_date, sector_code) 的充分统计量（纯函数，可单测）。

    等权收益 = avg(成分 change_pct)；市值加权收益 = Σ(change_pct×tmv)/Σtmv（M8）。
    行业估值用**市值加权调和均值** Σw/Σ(w/pe)（等价「行业总市值/行业总盈利」），
    因为调和均值可用充分统计量跨批合并，而中位数不能；且对负 PE 天然稳健（只取 pe>0）。
    """
    if q.empty:
        return pd.DataFrame()
    q = q.copy()
    q["stock_code"] = q["stock_code"].astype(str)
    q["trade_date"] = _as_dt(q["trade_date"])
    q = q.merge(cmap[["stock_code", "sector_code"]], on="stock_code", how="inner")
    if q.empty:
        return pd.DataFrame()
    # 仙股门槛，与 market_state_daily._hk_sector_up_ratio 完全一致（一致性验收前提）
    q["amount"] = pd.to_numeric(q["amount"], errors="coerce")
    q = q[q["amount"] >= MIN_AMOUNT]
    if q.empty:
        return pd.DataFrame()
    for c in ("close", "prev_close", "change_pct", "total_market_val",
              "pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm"):
        q[c] = pd.to_numeric(q[c], errors="coerce")

    q = q.sort_values(["stock_code", "trade_date"])
    g = q.groupby("stock_code", observed=True)["close"]
    q["ma60"] = g.transform(lambda s: s.rolling(60, min_periods=60).mean())
    q["hh60"] = g.transform(lambda s: s.rolling(60, min_periods=60).max())
    q["ll60"] = g.transform(lambda s: s.rolling(60, min_periods=60).min())
    q["ret20"] = g.transform(lambda s: s / s.shift(20) - 1)
    # warmup 段只用于 rolling 预热，不产出读数
    q = q[(q["trade_date"] >= pd.Timestamp(month_start))
          & (q["trade_date"] <= pd.Timestamp(month_end))]
    if q.empty:
        return pd.DataFrame()

    d = pd.DataFrame({"trade_date": q["trade_date"], "sector_code": q["sector_code"]})
    chg = q["change_pct"]
    w = q["total_market_val"].where(q["total_market_val"] > 0).fillna(0.0)
    w_eff = w.where(chg.notna(), 0.0)

    d["n_all"] = 1.0
    d["n_eq"] = chg.notna().astype(float)
    d["s_eq"] = chg.fillna(0.0)
    d["s_wret"] = (chg.fillna(0.0) * w_eff)
    d["s_w"] = w_eff
    d["n_cap"] = (w_eff > 0).astype(float)
    d["s_amount"] = q["amount"].fillna(0.0)

    d["s_up"] = (chg > 0).where(chg.notna()).fillna(0.0)
    d["c_up"] = chg.notna().astype(float)
    d["s_above"] = (q["close"] > q["ma60"]).where(q["ma60"].notna()).fillna(0.0)
    d["c_above"] = q["ma60"].notna().astype(float)
    d["s_high"] = (q["close"] >= q["hh60"]).where(q["hh60"].notna()).fillna(0.0)
    d["s_low"] = (q["close"] <= q["ll60"]).where(q["ll60"].notna()).fillna(0.0)
    r = q["ret20"]
    d["s_ret"] = r.fillna(0.0)
    d["s_ret2"] = (r ** 2).fillna(0.0)
    d["c_ret"] = r.notna().astype(float)

    # 估值：只取 pe/pb > 0 且市值 > 0；调和均值 Σw / Σ(w/pe)
    pe = q["pe_ttm_ratio"].where(q["pe_ttm_ratio"] > 0)
    pb = q["pb_ratio"].where(q["pb_ratio"] > 0)
    dy = q["dividend_ratio_ttm"]
    wpe = w.where(pe.notna(), 0.0)
    wpb = w.where(pb.notna(), 0.0)
    wdy = w.where(dy.notna(), 0.0)
    d["s_pe_w"] = wpe
    d["s_wpe"] = (wpe / pe).fillna(0.0)
    d["c_pe"] = (wpe > 0).astype(float)
    d["s_pb_w"] = wpb
    d["s_wpb"] = (wpb / pb).fillna(0.0)
    d["c_pb"] = (wpb > 0).astype(float)
    d["s_dy_w"] = wdy
    d["s_dyw"] = (wdy * dy.fillna(0.0))
    d["c_dy"] = (wdy > 0).astype(float)

    return d.groupby(["trade_date", "sector_code"], observed=True,
                     as_index=False)[_SS_COLS].sum()


def _finalize(ss: pd.DataFrame, names: dict) -> pd.DataFrame:
    """充分统计量 → 表 27 的行（纯函数）。覆盖度 <0.6 的列一律作废。"""
    m = ss.copy()
    div = lambda s: s.replace(0, np.nan)  # noqa: E731
    m["ret_eq"] = m["s_eq"] / div(m["n_eq"])
    cap_ok = (m["n_cap"] / div(m["n_all"])) >= MIN_COVERAGE
    m["ret_cap"] = (m["s_wret"] / div(m["s_w"])).where(cap_ok)
    m["amount"] = m["s_amount"]
    m["n_constituent"] = m["n_all"].astype(int)
    m["coverage"] = (m["n_eq"] / div(m["n_all"])).clip(0, 1)

    # 估值覆盖门禁：有效成分 <60% → NULL（复用 _coverage_gate 的 0.6 实测标定）
    for key, s_w, s_inv, cnt in (("pe_agg", "s_pe_w", "s_wpe", "c_pe"),
                                 ("pb_agg", "s_pb_w", "s_wpb", "c_pb")):
        ok = (m[cnt] / div(m["n_all"])) >= MIN_COVERAGE
        m[key] = (m[s_w] / div(m[s_inv])).where(ok)
    dy_ok = (m["c_dy"] / div(m["n_all"])) >= MIN_COVERAGE
    m["div_yield_agg"] = (m["s_dyw"] / div(m["s_dy_w"])).where(dy_ok)

    m["up_ratio"] = 100 * m["s_up"] / div(m["c_up"])
    m["above_ma60"] = 100 * m["s_above"] / div(m["c_above"])
    m["new_high_low"] = 100 * (m["s_high"] - m["s_low"]) / div(m["s_high"] + m["s_low"])
    cr = div(m["c_ret"])
    m["ret_dispersion"] = np.sqrt((m["s_ret2"] / cr - (m["s_ret"] / cr) ** 2).clip(lower=0))

    m["sector_name"] = m["sector_code"].map(names)
    # 市值加权版起点：2002 之前 total_market_val 覆盖率只有 0~5.7%，不得出加权读数
    cap_start_ts = pd.Timestamp(CAP_START)
    m.loc[m["trade_date"] < cap_start_ts, "ret_cap"] = np.nan
    cols = ["trade_date", "sector_code", "sector_name", "ret_eq", "ret_cap", "amount",
            "n_constituent", "coverage", "pe_agg", "pb_agg", "div_yield_agg",
            "up_ratio", "above_ma60", "new_high_low", "ret_dispersion"]
    return m[cols]


def _southbound(conn, cmap: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    """南向资金按行业求和（亿港元）。⚠ 仅 2026-04-20 起、est_ 估算、当前成分偏差。"""
    df = read_sql(conn, _GGT_SQL, params=[start, end])
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "sector_code", "southbound_net"])
    df["stock_code"] = df["stock_code"].astype(str)
    df["trade_date"] = _as_dt(df["trade_date"])
    df["est_net_inflow"] = pd.to_numeric(df["est_net_inflow"], errors="coerce")
    df = df.merge(cmap[["stock_code", "sector_code"]], on="stock_code", how="inner")
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "sector_code", "southbound_net"])
    return (df.groupby(["trade_date", "sector_code"], observed=True, as_index=False)
              ["est_net_inflow"].sum()
              .rename(columns={"est_net_inflow": "southbound_net"}))


def _refresh_index_points(conn, start: dt.date, end: dt.date, write: bool) -> int:
    """把窗口内的日收益累成连续指数点位（基期 1000）。

    增量时取窗口前一日每个行业已落的 idx 作基数接续，因此**不需要重算全史**；
    全量回填时 base 为空 → 从 BASE_INDEX 起累积。
    """
    from psycopg2.extras import execute_values

    base = read_sql(conn, """
        SELECT DISTINCT ON (sector_code) sector_code, idx_eq, idx_cap
        FROM industry.hk_index_daily
        WHERE trade_date < %s AND idx_eq IS NOT NULL
        ORDER BY sector_code, trade_date DESC
    """, params=[start])
    bmap = {r["sector_code"]: (r["idx_eq"], r["idx_cap"])
            for r in base.to_dict("records")} if not base.empty else {}

    rows_df = read_sql(conn, """
        SELECT trade_date, sector_code, ret_eq, ret_cap
        FROM industry.hk_index_daily
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY sector_code, trade_date
    """, params=[start, end])
    if rows_df.empty:
        return 0
    rows_df["trade_date"] = _as_dt(rows_df["trade_date"])
    for c in ("ret_eq", "ret_cap"):
        rows_df[c] = pd.to_numeric(rows_df[c], errors="coerce")

    out: list[tuple] = []
    for sec, g in rows_df.groupby("sector_code", sort=False):
        b_eq, b_cap = bmap.get(sec, (BASE_INDEX, BASE_INDEX))
        b_eq = float(b_eq) if b_eq is not None else BASE_INDEX
        b_cap = float(b_cap) if b_cap is not None else BASE_INDEX
        eq = BASE_INDEX * (1.0 + g["ret_eq"].fillna(0.0) / 100.0).cumprod()
        cap = BASE_INDEX * (1.0 + g["ret_cap"].fillna(0.0) / 100.0).cumprod()
        # 用窗口前最后一日的点位接续（cumprod 的相对形状不变，只平移基期）
        if sec in bmap:
            first_eq = float(eq.iloc[0]) or BASE_INDEX
            first_cap = float(cap.iloc[0]) or BASE_INDEX
            eq = eq * (b_eq / first_eq)
            cap = cap * (b_cap / first_cap)
        # 加权版起点之前不出点位
        cap = cap.where(g["trade_date"] >= pd.Timestamp(CAP_START))
        for d, e, cval in zip(g["trade_date"], eq, cap):
            out.append((d.date().isoformat(), sec,
                        None if pd.isna(e) else round(float(e), 6),
                        None if pd.isna(cval) else round(float(cval), 6)))
    if not write:
        return len(out)
    with conn.cursor() as cur:
        sql = """
            UPDATE industry.hk_index_daily AS t
               SET idx_eq = v.idx_eq, idx_cap = v.idx_cap
              FROM (VALUES %s) AS v(trade_date, sector_code, idx_eq, idx_cap)
             WHERE t.trade_date = v.trade_date::date
               AND t.sector_code = v.sector_code
        """
        for i in range(0, len(out), WRITE_BATCH):
            execute_values(cur, sql, out[i:i + WRITE_BATCH], page_size=WRITE_BATCH)
            conn.commit()          # R6：逐批 commit
    return len(out)


def build_index_daily(conn, start: dt.date, end: dt.date, write: bool = True) -> dict:
    """按月 × 按股票分批聚合 → 写 industry.hk_index_daily（表 27）。"""
    t0 = time.monotonic()
    cmap = load_constituents(conn)
    if cmap.empty:
        raise RuntimeError("stock_sector 无港股 INDUSTRY 成分 —— 上游 get_stock_sector 未采集，拒绝执行")
    codes = sorted(cmap["stock_code"].unique().tolist())
    names = cmap.drop_duplicates("sector_code").set_index("sector_code")["sector_name"].to_dict()
    per_code = cmap[["stock_code", "sector_code"]].drop_duplicates()
    stats = dict(start=str(start), end=str(end), sectors=len(names), stocks=len(codes),
                 rows=0, months=0, idx_points=0, write=write)

    for ms, me in _month_ranges(start, end):
        qs = ms - dt.timedelta(days=WARMUP_DAYS)
        parts = []
        for i in range(0, len(codes), BATCH_CODES):
            batch = codes[i:i + BATCH_CODES]
            q = _read_sql_stream(conn, _QUOTE_SQL, params=[qs, me, batch],
                                 chunk_size=STREAM_CHUNK, dtypes=_QUOTE_DTYPES)
            if not q.empty:
                p = _quote_partial(q, per_code, ms, me)
                if not p.empty:
                    parts.append(p)
                del p
            del q
            gc.collect()
        if not parts:
            continue
        ss = (pd.concat(parts, ignore_index=True)
                .groupby(["trade_date", "sector_code"], as_index=False)[_SS_COLS].sum())
        del parts
        monthly = _finalize(ss, names)
        del ss
        sb = _southbound(conn, cmap, ms, me)
        if not sb.empty:
            monthly = monthly.merge(sb, on=["trade_date", "sector_code"], how="left")
        monthly["southbound_net"] = (monthly["southbound_net"]
                                     if "southbound_net" in monthly.columns else np.nan)
        monthly["model_version"] = MODEL_VERSION
        monthly["trade_date"] = monthly["trade_date"].dt.date
        rows = _clean_rows(monthly.to_dict("records"))
        if write:
            for i in range(0, len(rows), WRITE_BATCH):
                bulk_upsert(conn, "industry.hk_index_daily", rows[i:i + WRITE_BATCH],
                            conflict_cols=["trade_date", "sector_code"])
                conn.commit()
        stats["rows"] += len(rows)
        stats["months"] += 1
        del monthly, rows
        gc.collect()
    if write:
        stats["idx_points"] = _refresh_index_points(conn, start, end, write)
    stats["elapsed_s"] = round(time.monotonic() - t0, 2)
    log.info("HK 行业日聚合完成 %s", stats)
    return stats


def report_consistency(conn, start: dt.date, end: dt.date) -> dict:
    """与 market_state_daily._hk_sector_up_ratio 的 HK 等权合成做一致性验收（只读）。

    两处都用 `amount >= 100000` 门槛 + `avg(change_pct)` 等权；
    差异仅可能来自「按 sector_code 分组」vs「按 sector_name 分组」以及行业数口径。
    """
    ref = read_sql(conn, """
        WITH s AS (
            SELECT v.trade_date, ss.sector_name, v.change_pct
            FROM hk_daily_quote v
            JOIN stock_sector ss ON ss.stock_code = v.stock_code
              AND ss.sector_type = 'INDUSTRY'
            WHERE v.trade_date BETWEEN %s AND %s
              AND v.change_pct IS NOT NULL AND v.amount >= %s
        ), g AS (
            SELECT trade_date, sector_name, avg(change_pct) AS r
            FROM s GROUP BY trade_date, sector_name
        )
        SELECT trade_date,
               count(*) FILTER (WHERE r > 0) * 100.0 / nullif(count(*), 0) AS up_ref
        FROM g GROUP BY trade_date ORDER BY trade_date
    """, params=[start, end, MIN_AMOUNT])
    new = read_sql(conn, """
        SELECT trade_date,
               count(*) FILTER (WHERE ret_eq > 0) * 100.0 / nullif(count(*), 0) AS up_new,
               count(*) AS rank_n
        FROM industry.hk_index_daily
        WHERE trade_date BETWEEN %s AND %s
        GROUP BY trade_date ORDER BY trade_date
    """, params=[start, end])
    if ref.empty or new.empty:
        return {"error": "参考或新表无数据", "ref_days": len(ref), "new_days": len(new)}
    m = ref.merge(new, on="trade_date", how="inner")
    m["diff"] = (pd.to_numeric(m["up_new"], errors="coerce")
                 - pd.to_numeric(m["up_ref"], errors="coerce")).abs()
    n = pd.to_numeric(m["rank_n"], errors="coerce").dropna()
    # 两版指数差异报告（M8 要求「两版并列以标注方法论差异」，可复跑）
    ver = read_sql(conn, """
        SELECT count(*) AS n,
               avg(abs(ret_eq - ret_cap)) AS mean_abs,
               max(abs(ret_eq - ret_cap)) AS max_abs,
               corr(ret_eq, ret_cap) AS corr,
               count(*) FILTER (WHERE sign(ret_eq) <> sign(ret_cap)) AS sign_flips,
               min(n_constituent) FILTER (WHERE abs(ret_eq - ret_cap) > 20) AS min_n_large_gap
        FROM industry.hk_index_daily
        WHERE trade_date BETWEEN %s AND %s
          AND ret_eq IS NOT NULL AND ret_cap IS NOT NULL
    """, params=[start, end])
    versions = {}
    if not ver.empty:
        r = ver.iloc[0]
        versions = dict(sector_days=int(r["n"]),
                        mean_abs_diff_pp=_rnd(r["mean_abs"]),
                        max_abs_diff_pp=_rnd(r["max_abs"]),
                        corr=_rnd(r["corr"], 4),
                        sign_flips=int(r["sign_flips"]),
                        min_constituents_on_large_gap=(
                            int(r["min_n_large_gap"]) if pd.notna(r["min_n_large_gap"]) else None))
    return dict(days=len(m), max_abs_diff=round(float(m["diff"].max()), 6),
                mean_abs_diff=round(float(m["diff"].mean()), 6),
                exact_days=int((m["diff"] < 1e-9).sum()),
                rank_n_min=int(n.min()) if len(n) else None,
                rank_n_max=int(n.max()) if len(n) else None,
                index_versions=versions)


def _rnd(v, digits=4):
    return None if v is None or pd.isna(v) else round(float(v), digits)


def _default_window(conn) -> tuple[dt.date, dt.date]:
    end = dt.date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM industry.hk_index_daily")
        last = cur.fetchone()[0]
    start = (last - dt.timedelta(days=REWIND_DAYS)) if last else \
            (end - dt.timedelta(days=INCREMENTAL_DAYS))
    return start, end


def run(codes=None, ctx=None, start: dt.date = None, end: dt.date = None,
        backfill: bool = False, write: bool = True, verify: bool = False) -> dict:
    """CLI / 独立运行入口（不挂 market_scheduler，R5）。"""
    with get_conn() as conn:
        if verify:
            # 验收路径保持**严格只读**：不跑建表 / 主键迁移等 DDL
            s = start or (dt.date.today() - dt.timedelta(days=60))
            e = end or dt.date.today()
            return {"consistency": report_consistency(conn, s, e)}
        from regime_schema import ensure_sector_schema
        ensure_sector_schema(conn)
        if backfill:
            start, end = BACKFILL_START, (end or dt.date.today())
        elif start is None:
            start, end = _default_window(conn)
        else:
            end = end or dt.date.today()
        # 输入新鲜度闸门：不越过 hk_daily_quote 的最后交易日
        with conn.cursor() as cur:
            cur.execute("SELECT max(trade_date) FROM hk_daily_quote")
            upstream = cur.fetchone()[0]
        if upstream is None:
            raise RuntimeError("hk_daily_quote 为空 —— 上游行情未采集，拒绝执行")
        end = min(end, upstream)
        if start > end:
            return {"skipped": "start > end"}
        return build_index_daily(conn, start, end, write=write)


def _main() -> int:
    ap = argparse.ArgumentParser(description="第④层 HK 行业层数据底座（D4 / M8）")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--backfill", action="store_true", help="全量回填 2000-01-03 起")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--verify", action="store_true", help="与 SECTOR.UP_RATIO HK 等权合成一致性报告")
    a = ap.parse_args()
    out = run(start=a.start, end=a.end, backfill=a.backfill,
              write=not a.dry_run, verify=a.verify)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
