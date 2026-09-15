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


def upsert(conn, table, data, conflict_cols):
    """
    INSERT ... ON CONFLICT DO UPDATE
    conn:       数据库连接
    table:      表名 (str)
    data:       要插入的数据 (dict)
    conflict_cols: 冲突列列表，如 ['stock_code', 'trade_date']
    """
    columns = list(data.keys())
    values = [data[c] for c in columns]

    placeholders = sql.SQL(", ").join([sql.Placeholder()] * len(columns))
    col_identifiers = sql.SQL(", ").join([sql.Identifier(c) for c in columns])
    conflict_target = sql.SQL(", ").join([sql.Identifier(c) for c in conflict_cols])

    # UPDATE SET 部分：排除冲突列
    update_cols = [c for c in columns if c not in conflict_cols and c != "id"]
    if not update_cols:
        # 所有列都是冲突列，使用 DO NOTHING
        query = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) DO NOTHING").format(
            table=sql.Identifier(table),
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
            table=sql.Identifier(table),
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


def bulk_upsert(conn, table, data_list, conflict_cols, do_nothing=False,
                skip_null_updates=False):
    """
    批量 INSERT ... ON CONFLICT DO UPDATE / DO NOTHING
    data_list: dict 列表
    do_nothing: True → 冲突时跳过(ON CONFLICT DO NOTHING)；
                False(默认) → 冲突时更新(ON CONFLICT DO UPDATE SET ...)
    skip_null_updates: 仅当 do_nothing=False 时生效。
                False(默认) → 冲突时整行覆盖（EXCLUDED.col 无条件覆盖原值）。
                True → 冲突时仅用有值的字段覆盖：col = COALESCE(EXCLUDED.col, 原值)，
                       新值为 NULL 的字段保留库中已有值（不回写成 NULL）。

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
            table=sql.Identifier(table),
            cols=col_identifiers,
            conflict=conflict_target,
        )
    elif not update_cols:
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s ON CONFLICT ({conflict}) DO NOTHING"
        ).format(
            table=sql.Identifier(table),
            cols=col_identifiers,
            conflict=conflict_target,
        )
    else:
        if skip_null_updates:
            # 仅覆盖本次有值的字段：新值为 NULL 时保留库中已有值，
            # 避免历史回溯脚本把已存在的估值字段（市值/PE/PB/52w 等）回写为空。
            update_set = sql.SQL(", ").join([
                sql.SQL("{col} = COALESCE(EXCLUDED.{col}, {tbl}.{col})").format(
                    col=sql.Identifier(c), tbl=sql.Identifier(table)
                )
                for c in update_cols
            ])
        else:
            update_set = sql.SQL(", ").join([
                sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c))
                for c in update_cols
            ])
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s "
            "ON CONFLICT ({conflict}) DO UPDATE SET {update_set}"
        ).format(
            table=sql.Identifier(table),
            cols=col_identifiers,
            conflict=conflict_target,
            update_set=update_set,
        )

    values_list = [tuple(d[c] for c in columns) for d in data_list]
    with conn.cursor() as cur:
        extras.execute_values(cur, query.as_string(conn), values_list)


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
