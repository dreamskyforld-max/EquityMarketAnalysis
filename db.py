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


def bulk_upsert(conn, table, data_list, conflict_cols):
    """
    批量 INSERT ... ON CONFLICT DO UPDATE
    data_list: dict 列表
    """
    if not data_list:
        return

    columns = list(data_list[0].keys())
    update_cols = [c for c in columns if c not in conflict_cols and c != "id"]

    col_identifiers = sql.SQL(", ").join([sql.Identifier(c) for c in columns])
    conflict_target = sql.SQL(", ").join([sql.Identifier(c) for c in conflict_cols])

    if not update_cols:
        query = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES %s ON CONFLICT ({conflict}) DO NOTHING"
        ).format(
            table=sql.Identifier(table),
            cols=col_identifiers,
            conflict=conflict_target,
        )
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
