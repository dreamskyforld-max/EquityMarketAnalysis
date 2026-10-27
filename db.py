#!/usr/bin/env python3
"""
数据库公共模块 — 所有采集脚本共用
配置文件: 统一从项目根目录的 config.conf 读取（详见 config.py），
         支持环境变量 DB_* 覆盖。
用法:
    from db import get_conn, upsert

    with get_conn() as conn:
        upsert(conn, 'daily_quote', data, conflict_cols=['stock_code', 'trade_date'])
"""
import os
import json
import math
import logging
from contextlib import contextmanager

import psycopg2
from psycopg2 import sql, extras

logger = logging.getLogger(__name__)

# ---------- 数据库配置加载 ----------
def _load_db_config():
    """从统一 config.conf 读取（环境变量优先，兼容 DB_* 覆盖）"""
    from config import val
    return {
        "host": val("database", "host", "DB_HOST", "localhost"),
        "port": val("database", "port", "DB_PORT", "5432"),
        "dbname": val("database", "dbname", "DB_NAME", "market_db"),
        "user": val("database", "user", "DB_USER", "postgres"),
        "password": val("database", "password", "DB_PASSWORD", ""),
    }

DB_CONFIG = _load_db_config()


@contextmanager
def get_conn():
    """数据库连接上下文管理器，自动 commit/rollback"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 标识符工具（支持 schema.table 形态）----------
def _table_ident(table: str):
    """表标识符：兼容 'schema.table'（如 regime.macro_series）。

    坑：psycopg2 的 sql.Identifier('regime.macro_series') 会把整串当成**单个**标识符，
    生成 "regime.macro_series"（一个带点的怪表名）→ relation does not exist。
    必须拆成 schema/table 两段分别加引号。
    """
    parts = [p for p in str(table).split(".") if p]
    if len(parts) == 2:
        return sql.SQL("{}.{}").format(sql.Identifier(parts[0]), sql.Identifier(parts[1]))
    return sql.Identifier(table)


def _table_alias(table: str) -> sql.Identifier:
    """ON CONFLICT DO UPDATE 里引用目标表时用**裸表名**作限定符（SQL 隐式别名规则）。"""
    return sql.Identifier(str(table).split(".")[-1])


def _adapt_value(v):
    """值适配：dict/list → JSONB；NaN/NaT → NULL。

    坑 1：psycopg2 不会自动适配 dict，会报 can't adapt type 'dict'。
    坑 2：pandas 的 NaN 被 psycopg2 写成 **PostgreSQL 的 numeric NaN 字面量**
          （不是 NULL）→ `count(col)` 把它当非空、`sum(col)`/`avg(col)` 整体传播成
          NaN，聚合结果静默失真。实测踩中：a_daily_quote 的 SH.520900（场内基金）
          估值字段为 NaN，导致 `sum(circular_market_val)` 全表按日聚合变 NaN。
    因此统一在写入层把 NaN/NaT 转成 NULL。
    """
    if isinstance(v, (dict, list)):
        return extras.Json(v)
    if v is None:
        return None
    try:
        if v != v:          # NaN / NaT / Decimal('NaN') 自反不等
            return None
    except Exception:
        pass
    return v


# ---------- 实时采集配置（realtime_collect_target）----------
def get_realtime_targets():
    """读取实时采集配置表 —— 采集端读取采集清单的唯一入口（取代旧 stock_info.is_active）。

    返回:
        {
          "all":    [code, ...],   # 在池全集（表内所有行）→ 日频任务/在池判定
          "tick":   [code, ...],   # collect_tick=TRUE  → TICKER 逐笔订阅
          "trend":  [code, ...],   # collect_trend=TRUE → 盘中分钟级（get_quote + record_trend）
          "quote":  [code, ...],   # collect_quote=TRUE → QUOTE(LV1) 推送订阅
          "market": {code: "HK"/"SH"/"SZ"},   # market 映射（stock_info 缺失时按代码前缀兜底）
        }

    语义（与 realtime_collect_target 表注释一致）：
      · 行存在 = 在实时采集池（等价旧 stock_info.is_active=TRUE）；
      · 三开关只控对应实时采集，全 FALSE 的行 = 只采日频、不采实时；
      · 读表失败直接抛异常，调用方不得静默回退旧配置（避免采集口径漂移）。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.stock_code,
                       COALESCE(s.market, split_part(t.stock_code, '.', 1)::char(2)) AS market,
                       t.collect_tick, t.collect_trend, t.collect_quote
                FROM realtime_collect_target t
                LEFT JOIN stock_info s ON s.stock_code = t.stock_code
                ORDER BY t.stock_code
            """)
            rows = cur.fetchall()

    targets = {"all": [], "tick": [], "trend": [], "quote": [], "market": {}}
    for code, market, tick, trend, quote in rows:
        targets["all"].append(code)
        targets["market"][code] = market
        if tick:
            targets["tick"].append(code)
        if trend:
            targets["trend"].append(code)
        if quote:
            targets["quote"].append(code)
    return targets


def upsert(conn, table, data, conflict_cols):
    """
    INSERT ... ON CONFLICT DO UPDATE
    conn:       数据库连接
    table:      表名 (str)
    data:       要插入的数据 (dict)
    conflict_cols: 冲突列列表，如 ['stock_code', 'trade_date']
    """
    columns = list(data.keys())
    values = [_adapt_value(data[c]) for c in columns]

    placeholders = sql.SQL(", ").join([sql.Placeholder()] * len(columns))
    col_identifiers = sql.SQL(", ").join([sql.Identifier(c) for c in columns])
    conflict_target = sql.SQL(", ").join([sql.Identifier(c) for c in conflict_cols])

    # UPDATE SET 部分：排除冲突列
    update_cols = [c for c in columns if c not in conflict_cols and c != "id"]
    if not update_cols:
        # 所有列都是冲突列，使用 DO NOTHING
        query = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) DO NOTHING").format(
            table=_table_ident(table),
            cols=col_identifiers,
            vals=placeholders,
            conflict=conflict_target,
        )
    else:
        update_set = sql.SQL(", ").join([
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c))
            for c in update_cols
        ])
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({vals}) "
            "ON CONFLICT ({conflict}) DO UPDATE SET {update_set}"
        ).format(
            table=_table_ident(table),
            cols=col_identifiers,
            vals=placeholders,
            conflict=conflict_target,
            update_set=update_set,
        )

    with conn.cursor() as cur:
        cur.execute(query, values)


def write_full_tick(conn, data_list, columns):
    """
    旁路全量落盘（诊断用）：富途推送的每一笔逐笔都无去重写入 full_tick_data。
    与 tick_data 不同，本表没有 sequence 唯一约束，重复推送的原样保留，
    用于事后比对"推了什么 / tick_data 实际落了什么"以定位去重或缺失问题。
    columns: 列名列表（与 data_list 中 dict 的 keys 对齐）。
    """
    if not data_list:
        return
    col_identifiers = sql.SQL(", ").join([sql.Identifier(c) for c in columns])
    query = sql.SQL(
        "INSERT INTO full_tick_data ({cols}) VALUES %s"
    ).format(cols=col_identifiers)
    values_list = [tuple(d.get(c) for c in columns) for d in data_list]
    with conn.cursor() as cur:
        extras.execute_values(cur, query.as_string(conn), values_list, page_size=1000)


# 审计/时间戳列：`only_if_changed` 判定「值是否变化」时必须排除——
# 它们恒为 NOW()，纳入判定会让 WHERE 永远成立、优化彻底失效。
_AUDIT_COLS = frozenset({"updated_at", "created_at", "update_time", "snapshot_time"})


def bulk_upsert(conn, table, data_list, conflict_cols, do_nothing=False,
                skip_null_updates=False, only_if_changed=False):
    """
    批量 INSERT ... ON CONFLICT DO UPDATE / DO NOTHING
    data_list: dict 列表
    do_nothing: True → 冲突时跳过(ON CONFLICT DO NOTHING)；
                False(默认) → 冲突时更新(ON CONFLICT DO UPDATE SET ...)
    skip_null_updates: 仅当 do_nothing=False 时生效。
                False(默认) → 冲突时整行覆盖（EXCLUDED.col 无条件覆盖原值）。
                True → 冲突时仅用有值的字段覆盖：col = COALESCE(EXCLUDED.col, 原值)，
                       新值为 NULL 的字段保留库中已有值（不回写成 NULL）。
    only_if_changed: 仅当**数据列**真的变了才执行 UPDATE
                （`DO UPDATE SET ... WHERE tbl.c IS DISTINCT FROM EXCLUDED.c OR ...`）。
                适用于「源是月/季频、但按 PIT 填充成日频网格」的**计算层**表
                （regime.indicator_value / regime.market_regime_daily）：
                  ① 值没变的行**完全不写** → 不再把同一行反复重写（省行版本膨胀 / WAL /
                     autovacuum 负担；实测 indicator_value 全表 31.5% 的行是重复值）；
                  ② updated_at 恢复「值最后变化时间」的语义（原先被刷新成「最后一次被跑到」，
                     导致无法用它判断「月频指标一个月没变」这种本该告警的情形）。
                ⚠ 审计时间列（_AUDIT_COLS）不参与变化判定——它们恒为 NOW()，
                  纳入会让 WHERE 永远成立、优化失效。
                ⚠ **绝不能用于采集层**（如 regime.macro_series）：那里的断供监控正是靠
                  updated_at 被反复刷新判断「采集在跑」（见各 get_*.py 的显式注释），
                  月频序列值不变时也必须刷新。

    自动对齐所有 record 的 keys：不同数据源构造的 record 可能字段不一致
    （如富途带 volume/turnover，FRED 不带），用 data_list[0].keys() 取列名
    会导致后续 record KeyError。这里收集所有 record 的并集 keys，缺失字段补 None。
    """
    if not data_list:
        return

    # 收集所有 record 的 keys 并集，保证每条 record 都有相同的字段
    all_keys = set()
    for d in data_list:
        all_keys.update(d.keys())
    for d in data_list:
        for k in all_keys:
            if k not in d:
                d[k] = None

    columns = list(data_list[0].keys())
    update_cols = [c for c in columns if c not in conflict_cols and c != "id"]

    col_identifiers = sql.SQL(", ").join([sql.Identifier(c) for c in columns])
    conflict_target = sql.SQL(", ").join([sql.Identifier(c) for c in conflict_cols])

    if do_nothing:
        # 仅跳过已存在行：用于 tick_data，冲突键为 (stock_code, sequence) 复合键。
        # 注意富途 sequence 是"同一时刻跨股票共享的包序号"，并非 per-stock 唯一，
        # 故必须用复合键，否则会被其他股票抢键导致本票数据被 DO NOTHING 静默丢弃。
        # 断线重连后 FutuOpenD 重推的历史数据与其余批次同批时会产生重复 sequence，
        # DO NOTHING 直接跳过即可，不会像 DO UPDATE 那样覆盖掉先到的盘前数据。
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s ON CONFLICT ({conflict}) DO NOTHING"
        ).format(
            table=_table_ident(table),
            cols=col_identifiers,
            conflict=conflict_target,
        )
    elif not update_cols:
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s ON CONFLICT ({conflict}) DO NOTHING"
        ).format(
            table=_table_ident(table),
            cols=col_identifiers,
            conflict=conflict_target,
        )
    else:
        if skip_null_updates:
            # 仅覆盖本次有值的字段：新值为 NULL 时保留库中已有值，
            # 避免历史回溯脚本把已存在的估值字段（市值/PE/PB/52w 等）回写为空。
            update_set = sql.SQL(", ").join([
                sql.SQL("{col} = COALESCE(EXCLUDED.{col}, {tbl}.{col})").format(
                    col=sql.Identifier(c), tbl=_table_alias(table)
                )
                for c in update_cols
            ])
        else:
            update_set = sql.SQL(", ").join([
                sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c))
                for c in update_cols
            ])
        # only_if_changed：按**数据列**判定是否真的变了；审计时间列排除（见 _AUDIT_COLS）
        where_sql = sql.SQL("")
        if only_if_changed:
            chg = [c for c in update_cols if c not in _AUDIT_COLS]
            if chg:
                where_sql = sql.SQL(" WHERE {cond}").format(cond=sql.SQL(" OR ").join([
                    sql.SQL("{tbl}.{col} IS DISTINCT FROM EXCLUDED.{col}").format(
                        col=sql.Identifier(c), tbl=_table_alias(table))
                    for c in chg]))
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s "
            "ON CONFLICT ({conflict}) DO UPDATE SET {update_set}{where}"
        ).format(
            table=_table_ident(table),
            cols=col_identifiers,
            conflict=conflict_target,
            update_set=update_set,
            where=where_sql,
        )

    values_list = [tuple(_adapt_value(d[c]) for c in columns) for d in data_list]
    with conn.cursor() as cur:
        extras.execute_values(cur, query.as_string(conn), values_list)


# ============================================================================
# NUMERIC 列范围护栏（2026-10-16，事故驱动，勿删）
# ----------------------------------------------------------------------------
# 事故：富途快照对「复牌/长期无量」股给出的 volume_ratio 可达上万（实测
# HK.00129 = 49054.375），而 hk_daily_quote.volume_ratio 当时是 NUMERIC(8,4)
# （上限 < 10^4）→ 整批 INSERT 抛 NumericValueOutOfRange，异常冒泡出事务，
# **当天全市场 2196 只一行都没落**；随后 market_state_daily 用仅剩的监控池 77 只
# 算横截面，把港股 PE/PB/破净/涨跌家数占比等指标全部算错（2026-10-14）。
#
# 两层防护：
#   ① 结构层：百分比类列放宽到 NUMERIC(12,4)（sql/schema.sql + 各写入脚本 DDL，
#      老库由 widen_numeric_columns 自愈）；
#   ② 写入层：sanitize_numeric_rows —— 按 information_schema 里的**真实列宽**把越界值
#      置 NULL 并回传清单。列宽从库读，所以以后再改列宽不需要动这里。
#      只对显式调用它的写入路径生效：不塞进 bulk_upsert —— 通用写入层静默置空会掩盖
#      别处真实的 bug（如单位写错导致整体放大 10^6），宁可让那些地方自己报错。
# ============================================================================

# 百分比类列的历史事故值域参考：换手率实测 max 85、股息率 max 543、量比 max 49054。
_PERCENT_COLS_12_4 = ("turnover_rate", "volume_ratio", "dividend_ratio_ttm")
"""需要 NUMERIC(12,4) 的「无上界百分比」列（换手率 / 量比 / 股息率）。

量比 = 当日成交量 / 近 5 日均量 —— 长期无量的仙股一旦复牌即上万；
换手率、股息率同理无理论上界（仙股特别股息可上千）。
三者统一 12,4（留 8 位整数余量），与 change_pct 在池表的宽度一致。
"""

_NUMERIC_LIMITS_CACHE: "dict[str, dict[str, tuple[int, int]]]" = {}


def _parse_table(table: str) -> tuple:
    """'regime.macro_series' → ('regime', 'macro_series')；裸表名 → ('public', 表名)。"""
    schema, _, name = table.partition(".")
    if not name:
        return "public", schema
    return schema, name


def _read_numeric_limits(conn, table: str) -> dict:
    """直接读库：{列名: (precision, scale)}，仅 NUMERIC 列。失败返回 {}。"""
    schema, name = _parse_table(table)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND data_type = 'numeric' "
                "AND numeric_precision IS NOT NULL",
                (schema, name),
            )
            return {c: (int(p), int(s)) for c, p, s in cur.fetchall()}
    except Exception as e:
        logger.warning(f"读取 {table} 的 NUMERIC 列宽失败（本次跳过越界净化）: {e}")
        return {}


def numeric_limits(conn, table: str) -> dict:
    """列宽（按表进程内缓存）。配置/DDL 变更后需重启进程才刷新。"""
    if table not in _NUMERIC_LIMITS_CACHE:
        _NUMERIC_LIMITS_CACHE[table] = _read_numeric_limits(conn, table)
    return _NUMERIC_LIMITS_CACHE[table]


def sanitize_numeric_rows(conn, table: str, rows: list) -> tuple:
    """把超出列可表示范围的值**就地**置 None，返回 (rows, dropped)。

    dropped: [(stock_code, 列名, 原值), ...] —— 供调用方告警（哪只票哪个字段被丢弃）。
    范围判定按 PostgreSQL 语义：先按 scale 四舍五入，再要求 |v| < 10^(p-s)。
    （|v| 恰为 10^(p-s) 如 10000 对 NUMERIC(8,4) 会因进位而溢出，必须一起挡住。）
    """
    lim = numeric_limits(conn, table)
    if not lim or not rows:
        return rows, []
    dropped: list = []
    for r in rows:
        for col, (p, s) in lim.items():
            v = r.get(col)
            if v is None or isinstance(v, (str, bool)):
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv) or abs(round(fv, s)) >= 10 ** (p - s):
                r[col] = None
                dropped.append((r.get("stock_code"), col, v))
    return rows, dropped


def clean_ohlc(open_price, high_price, low_price, eps: float = 0.01) -> tuple:
    """OHLC 有效性护栏（2026-10-27）：返回 (open, high, low)，非法则整体 (None, None, None)。

    为什么需要（实测 11 行脏数据，2026-10-27 清洗）：
      · 新浪 HSI 早期（2013-12）开盘缺失被填 0 → open=0；
      · 新浪/东财对日韩节假日「假交易日」或字段错位 → high < low（JP.N225 2024-08-12、
        KR.KS11 2024-09-16~18 等）；
      · 个别行的 low > close（HK.800000 2014-09-01）。
    这些值若原样入库，会把振幅/影线/ATR 类衍生指标算成负数或 0；且**重跑全量回填会
    原样写回**，所以护栏必须在写入侧，而不是靠一次性 UPDATE 清洗。

    判定：任一为 None / 非数值 / NaN / ≤0，或 high < low、open 越界（容差 eps 吸收
    「close 4 位小数 vs high/low 2 位小数」的舍入差，实测最大 0.001）→ 整组作废。

    注意：这是**源数据自洽性**校验，不校验 OHL 与 close 的关系 —— close 来自另一列
    （last_price），跨列比较会把纯舍入差误判为脏数据。
    """
    vals = []
    for v in (open_price, high_price, low_price):
        if v is None or isinstance(v, bool):
            return None, None, None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None, None, None
        if not math.isfinite(x):      # NaN / ±inf
            return None, None, None
        vals.append(x)
    o, h, lo = vals
    if min(o, h, lo) <= 0 or h < lo - eps or o > h + eps or o < lo - eps:
        return None, None, None
    return round(o, 4), round(h, 4), round(lo, 4)


def widen_numeric_columns(conn, table: str, widths: dict) -> list:
    """需要时把 NUMERIC 列放宽到 widths={列: (p, s)}，返回实际改动的列名。

    只在当前列宽**小于**目标时才 ALTER（已是目标宽度则完全不动，避免无谓表重写）。
    用途：老库上的 hk_daily_quote.volume_ratio 仍是 NUMERIC(8,4)，新代码 DDL 已改宽
    → 由各写入脚本的 _ensure_table 调用本函数自愈，不依赖人工迁移。

    ALTER COLUMN TYPE 会被依赖该列的视图/规则硬拦截（实测 v_daily_quote →
    "cannot alter type of a column used by a view or rule"），且 ALTER 无法 CASCADE
    掉视图 → 由 _alter_columns_safe 先摘除依赖视图再还原（见其文档）。
    """
    have_map = _read_numeric_limits(conn, table)
    plan = []  # [(col, have, p, s)] —— 仅确需改动的列
    for col, (p, s) in widths.items():
        have = have_map.get(col)
        if have is None or (have[0] >= p and have[1] >= s):
            continue
        plan.append((col, have, p, s))
    if not plan:
        return []
    changed = _alter_columns_safe(conn, table, plan)
    if changed:
        _NUMERIC_LIMITS_CACHE.pop(table, None)
    for col, have, p, s in plan:
        if col in changed:
            logger.info(f"{table}.{col} 列宽 {have[0]},{have[1]} → {p},{s}（越界值护栏）")
    return changed


# ── 依赖视图保护：ALTER COLUMN TYPE 前的摘除 / 还原 ──────────────────────────
# 背景：PostgreSQL 禁止 ALTER 被视图/规则引用的列的类型，且 ALTER TABLE 的 CASCADE
# 只作用于约束，删不掉视图。唯一可行路径是「先 DROP 依赖视图 → ALTER → 原样重建」。
# 关键安全设计：
#   · 全程在**调用方同一事务**内执行，并用 SAVEPOINT 包裹：任一步失败即
#     ROLLBACK TO SAVEPOINT —— PG 的 DDL 是事务性的，视图自动复原，且调用方
#    事务不会停留在 aborted 状态（2026-09-14 InFailedSqlTransaction 教训）。
#   · 依赖视图按拓扑序摘除/重建（v_daily_excess_return 依赖 v_daily_quote，
#     必须先删前者、后建前者），并还原 relacl 授权与表/列注释，尽量无损。
_VIEW_PRIV_MAP = {"r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE",
                  "D": "TRUNCATE", "x": "REFERENCES", "t": "TRIGGER"}


def _dependent_views(cur, table: str) -> list:
    """返回直接或间接依赖 table 的视图/物化视图（含定义、授权、注释）。"""
    schema, name = _parse_table(table)
    cur.execute(
        """
        WITH RECURSIVE dep AS (
            SELECT DISTINCT r.ev_class AS oid
            FROM pg_depend d
            JOIN pg_rewrite r  ON r.oid = d.objid
            JOIN pg_class t    ON t.oid = d.refobjid
            JOIN pg_namespace tn ON tn.oid = t.relnamespace
            WHERE d.classid = 'pg_rewrite'::regclass
              AND d.refclassid = 'pg_class'::regclass
              AND t.relname = %s AND t.relkind = 'r' AND tn.nspname = %s
            UNION
            SELECT DISTINCT r.ev_class AS oid
            FROM dep
            JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass
                           AND d.refclassid = 'pg_class'::regclass
                           AND d.refobjid = dep.oid
            JOIN pg_rewrite r ON r.oid = d.objid
        )
        SELECT c.oid, n.nspname, c.relname, c.relkind,
               pg_get_viewdef(c.oid, true), c.relacl
        FROM dep
        JOIN pg_class c     ON c.oid = dep.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('v', 'm')
        """,
        (name, schema),
    )
    views = []
    for oid, nsp, relname, kind, vdef, acl in cur.fetchall():
        cur.execute(
            "SELECT objsubid, description FROM pg_description "
            "WHERE objoid = %s AND classoid = 'pg_class'::regclass",
            (oid,),
        )
        comments = cur.fetchall()
        views.append({
            "oid": oid, "schema": nsp, "name": relname, "kind": kind,
            "def": vdef.strip().rstrip(";").strip(), "acl": acl, "comments": comments,
        })
    return views


def _view_topo_order(cur, views: list) -> list:
    """按依赖关系排序：被依赖者在前（重建顺序），其反序即安全摘除顺序。"""
    by_oid = {v["oid"]: v for v in views}
    cur.execute(
        """
        SELECT DISTINCT r.ev_class AS dependent, d.refobjid AS source
        FROM pg_depend d
        JOIN pg_rewrite r ON r.oid = d.objid
        WHERE d.classid = 'pg_rewrite'::regclass
          AND d.refclassid = 'pg_class'::regclass
          AND r.ev_class = ANY(%s) AND d.refobjid = ANY(%s)
        """,
        (list(by_oid), list(by_oid)),
    )
    deps: dict = {oid: set() for oid in by_oid}
    for dependent, source in cur.fetchall():
        if dependent in deps and source in by_oid and source != dependent:
            deps[dependent].add(source)
    order, remaining = [], dict(by_oid)
    while remaining:
        ready = [v for v in remaining.values() if not (deps[v["oid"]] & set(remaining))]
        if not ready:  # 防御：PG 不允许视图循环依赖，真出现则按原序硬处理
            ready = list(remaining.values())
        for v in ready:
            order.append(v)
            remaining.pop(v["oid"])
    return order


def _view_ident(v: dict) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(v["schema"]), sql.Identifier(v["name"]))


def _restore_view_meta(cur, v: dict):
    """重建视图后还原授权（relacl）与表/列注释（pg_description）。"""
    for entry in v.get("acl") or []:
        grantee, _, rest = str(entry).partition("=")
        privs = rest.split("/")[0]
        granted = [_VIEW_PRIV_MAP[ch] for ch in privs if ch in _VIEW_PRIV_MAP]
        if not granted:
            continue
        target = sql.SQL("PUBLIC") if grantee == "" else sql.Identifier(grantee)
        cur.execute(
            sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(", ").join(sql.SQL(p) for p in granted), _view_ident(v), target)
        )
    for subid, desc in v.get("comments") or []:
        if subid == 0:
            cur.execute(
                sql.SQL("COMMENT ON VIEW {} IS {}").format(
                    _view_ident(v), sql.Literal(desc))
            )
        else:
            cur.execute(
                "SELECT a.attname FROM pg_attribute a "
                "WHERE a.attrelid = %s AND a.attnum = %s",
                (v["oid"], subid),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                        _view_ident(v), sql.Identifier(row[0]), sql.Literal(desc))
                )


def _alter_columns_safe(conn, table: str, plan: list) -> list:
    """在 SAVEPOINT 内执行 ALTER COLUMN TYPE，必要时先摘除并还原依赖视图。

    plan: [(col, have, p, s)]。返回成功改动的列名；任何失败都回滚到
    摘除前状态（视图自动复原、列宽不变）并返回 []，调用方事务保持可用。
    """
    sp = "sp_widen_numeric"
    try:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {sp}")
            views = _view_topo_order(cur, _dependent_views(cur, table))
            if views:
                logger.warning(
                    f"{table} 列宽调整被视图依赖，将先摘除后还原（单事务，失败自动回滚）: "
                    + ", ".join(f"{v['schema']}.{v['name']}" for v in views)
                )
                for v in reversed(views):
                    if v["kind"] == "m":
                        logger.warning(
                            f"物化视图 {v['schema']}.{v['name']} 被摘除，"
                            "其数据/索引需重建后人工核对"
                        )
                    cur.execute(
                        sql.SQL("DROP {} IF EXISTS {}").format(
                            sql.SQL("MATERIALIZED VIEW" if v["kind"] == "m" else "VIEW"),
                            _view_ident(v))
                    )
            for col, _have, p, s in plan:
                cur.execute(
                    f"ALTER TABLE {table} ALTER COLUMN {col} TYPE NUMERIC({p},{s})"
                )
            for v in views:
                if v["kind"] == "m":
                    cur.execute(
                        sql.SQL("CREATE MATERIALIZED VIEW {} AS {} WITH DATA").format(
                            _view_ident(v), sql.SQL(v["def"]))
                    )
                else:
                    cur.execute(
                        sql.SQL("CREATE OR REPLACE VIEW {} AS {}").format(
                            _view_ident(v), sql.SQL(v["def"]))
                    )
                _restore_view_meta(cur, v)
            cur.execute(f"RELEASE SAVEPOINT {sp}")
    except Exception as e:
        try:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                cur.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            pass
        logger.warning(
            f"{table} 列宽自愈失败（{str(e).strip()[:200]}），保持现状；"
            "写入侧 sanitize_numeric_rows 仍会兜底置空越界值"
        )
        return []
    return [c for c, *_ in plan]


# ============================================================================
# 写方双写（daily_quote 合并迁移 · 阶段 1）：daily_quote → 池表
# ============================================================================
# 背景：daily_quote 是 active 池的「1 分钟快照 + QUOTE 秒级刷新」表；
#       a_daily_quote / hk_daily_quote 是全市场池表（统一视图 v_daily_quote 的数据源）。
#       阶段 1 让池表也获得 active 池的分钟级/秒级新鲜度，读方才敢切到 v_daily_quote。
# 开关：config.conf 的 [quote] dual_write（env QUOTE_DUAL_WRITE 覆盖），默认 true。
# 回滚：置 false + 重启进程（旧路径完全不受影响）。
#
# 列名映射（daily_quote 命名 → 池表命名；其余同名列透传）：
_QUOTE_COL_MAP = {
    "last_price": "close",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "turnover": "amount",
}

# 允许双写进池表的字段（daily_quote 命名侧）= 池表列 ∩ daily_quote 列。
# 不在白名单的键一律忽略 —— 既不会撞上「列不存在」，
# 也保证 hk/a 池表的派生列永不被本模块写入（PS/PCF 两列已于 2026-09 下线，
# 改由画像层 load_revenue_ttm 现算，详情见 profiling/quantile.py）。
_POOL_QUOTE_FIELDS = frozenset((
    "stock_code", "trade_date", "update_time",
    "last_price", "open_price", "high_price", "low_price",
    "prev_close", "change_pct", "volume", "turnover",
    "turnover_rate", "volume_ratio", "high_52w", "low_52w",
    "total_market_val", "circular_market_val",
    "pe_ratio", "pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm",
))


def dual_write_enabled() -> bool:
    """池表双写开关：config.conf [quote] dual_write / env QUOTE_DUAL_WRITE，默认 true。"""
    from config import val
    v = str(val("quote", "dual_write", "QUOTE_DUAL_WRITE", "true")).strip().lower()
    return v in ("1", "true", "yes", "on")


def quote_pool_table(stock_code: str):
    """按代码前缀路由池表：HK.* → hk_daily_quote；SH./SZ. → a_daily_quote；其它 → None（不写）。"""
    if not stock_code:
        return None
    if stock_code.startswith("HK."):
        return "hk_daily_quote"
    if stock_code.startswith(("SH.", "SZ.")):
        return "a_daily_quote"
    return None


def _build_pool_row(data: dict) -> dict:
    """daily_quote 口径 dict → 池表口径 dict（列名映射 + 白名单过滤）。"""
    return {_QUOTE_COL_MAP.get(k, k): v
            for k, v in data.items() if k in _POOL_QUOTE_FIELDS}


def write_quote_dual(conn, data: dict) -> bool:
    """把一行「daily_quote 口径」行情双写进对应池表（整行 upsert，幂等）。

    安全设计：
      · 内部 SAVEPOINT 隔离 —— 池表写入失败只回滚该子事务并告警，
        绝不影响调用方已完成的 daily_quote 写入；
      · skip_null_updates=True —— 本路径不产出的列（估值/PS/PCF/52周等）保留库中原值；
      · 白名单过滤 —— 不会因多余键撞上「列不存在」。
    返回是否写入成功（未开启双写 / 非股票代码 → False）。
    """
    if not dual_write_enabled():
        return False
    table = quote_pool_table(data.get("stock_code", ""))
    if not table:
        return False
    row = _build_pool_row(data)
    if "stock_code" not in row or "trade_date" not in row:
        return False
    # 越界值净化：单个超出列宽的字段（复牌/无量股的量比可上万）会让整行写入失败，
    # 这里按真实列宽置 NULL 并告警，保住其余字段（与 2026-10-14 事故同源）。
    _, dropped = sanitize_numeric_rows(conn, table, [row])
    if dropped:
        logger.warning(f"[双写] {row.get('stock_code')} → {table} 越界值已置空: {dropped}")
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT dw_pool")
        try:
            bulk_upsert(conn, table, [row],
                        conflict_cols=["stock_code", "trade_date"],
                        skip_null_updates=True)
            cur.execute("RELEASE SAVEPOINT dw_pool")
            return True
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT dw_pool")
            logger.warning(f"[双写] {row.get('stock_code')} → {table} 失败"
                           f"（已回滚子事务，不影响 daily_quote）: {e}")
            return False


def update_quote_pool_intraday(cur, stock_code: str, fields: dict) -> int:
    """用 QUOTE 推送的行情列刷新池表「当日行」（只 UPDATE 已存在行，不 INSERT）。

    与 ticker_collector 对 daily_quote 的语义一致：当日行不存在（如尚未采集）→ 0 行命中、不碰表。
    同样 SAVEPOINT 隔离，失败只回滚子事务并告警。返回命中行数。
    """
    if not dual_write_enabled():
        return 0
    table = quote_pool_table(stock_code)
    fields = {k: v for k, v in (fields or {}).items() if k in _POOL_QUOTE_FIELDS}
    if not table or not fields:
        return 0
    # 表名/列名均来自本模块常量白名单，无注入面；值一律走 psycopg2 参数占位符
    col_sets = ", ".join(f"{_QUOTE_COL_MAP.get(k, k)} = %({k})s" for k in fields)
    query = (f"UPDATE {table} SET {col_sets} "
             f"WHERE stock_code = %(stock_code)s AND trade_date = CURRENT_DATE")
    cur.execute("SAVEPOINT dw_pool")
    try:
        cur.execute(query, {"stock_code": stock_code, **fields})
        n = cur.rowcount
        cur.execute("RELEASE SAVEPOINT dw_pool")
        return n
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT dw_pool")
        logger.warning(f"[双写] {stock_code} → {table} 实时刷新失败"
                       f"（已回滚子事务，不影响 daily_quote）: {e}")
        return 0
