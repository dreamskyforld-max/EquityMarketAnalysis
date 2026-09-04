#!/usr/bin/env python3
"""标签域实现的基础工具（各 tags/*.py 共用）

统一约定：
    · 每个标签函数是纯函数 fn(as_of: date) -> DataFrame
    · 返回列固定为 [stock_code, key_value, num_value, confidence]
    · 数据库连接由标签函数自己开（with _conn()），不跨标签共享，避免长事务
"""
from __future__ import annotations

import pandas as pd

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
