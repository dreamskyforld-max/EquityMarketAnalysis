#!/usr/bin/env python3
"""标签域实现的基础工具（各 tags/*.py 共用）

统一约定：
    · 每个标签函数是纯函数 fn(as_of: date) -> DataFrame
    · 返回列固定为 [stock_code, key_value, num_value, confidence]
    · 数据库连接由标签函数自己开（with _conn()），不跨标签共享，避免长事务
    · 数据不可用 → raise SkipTag（本次不产出），由标签自己判断，没有统一检查
"""
from __future__ import annotations

import pandas as pd

from ..errors import SkipTag   # re-export：标签侧统一从 _base 引用

RESULT_COLS = ["stock_code", "key_value", "num_value", "confidence"]


def _conn():
    """开一个数据库连接（上下文管理器）。"""
    from db import get_conn

    return get_conn()


def _read_sql(conn, sql: str, params=None) -> pd.DataFrame:
    """用 psycopg2 原生游标取数（pandas 官方不支持 psycopg2 连接，会刷警告）。"""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def _frame(codes, values, nums=None, confs=None) -> pd.DataFrame:
    """构造标准返回帧。

    codes   股票代码
    values  离散取值（key_value）：enum 存规范 code，tier 存 "1".."5"，bool 存 "true"/"false"
    nums    连续取值（num_value）：保留分档损失的信息量，供回测与重新标定
    confs   置信度，缺省 1.0
    """
    return pd.DataFrame(
        {
            "stock_code": list(codes),
            "key_value": pd.Series(list(values), dtype="object"),
            "num_value": list(nums) if nums is not None else None,
            "confidence": list(confs) if confs is not None else 1.0,
        }
    )


def last_arrival(conn, table: str, time_col: str = "trade_date"):
    """源表最新到货日期。纯事实查询，不做任何健康/充分性判断。

    time_col 由调用方（标签）指定：各表的时间列本就不同（trade_date / update_time /
    buyback_date ...），不该由工具层猜测或硬编码。
    """
    from psycopg2 import sql as pgsql

    with conn.cursor() as cur:
        cur.execute(
            pgsql.SQL("SELECT MAX({})::date FROM {}").format(
                pgsql.Identifier(time_col), pgsql.Identifier(table)
            )
        )
        row = cur.fetchone()
    return row[0] if row else None


def require_fresh(conn, table: str, as_of, max_lag: int = 0,
                  time_col: str = "trade_date"):
    """要求源表数据滞后不超过 max_lag 天，否则 raise SkipTag。返回实际到货日。

    max_lag 由标签自己传：技术面要求 T 日就位（0），财报容忍 T+3，各标签语义不同。
    注意本函数只判「滞后」，不判「够不够多」——数量是否充分由标签按其自身口径决定。
    """
    latest = last_arrival(conn, table, time_col=time_col)
    if latest is None:
        raise SkipTag(f"{table} 无任何数据")
    lag = (as_of - latest).days
    if lag > max_lag:
        raise SkipTag(
            f"{table} 最新到货 {latest}，相对 as_of={as_of} 滞后 {lag} 天（允许 {max_lag} 天）"
        )
    return latest
