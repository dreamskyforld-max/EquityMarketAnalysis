#!/usr/bin/env python3
"""标签域实现的基础工具（各 tags/*.py 共用）

统一约定：
    · 每个标签函数是纯函数 fn(as_of: date) -> DataFrame
    · 返回列固定为 [stock_code, key_value, num_value, confidence]
    · 数据库连接由标签函数自己开（with _conn()），不跨标签共享，避免长事务
    · 数据不可用 → raise SkipTag（本次不产出），由标签自己判断，没有统一检查
"""
from __future__ import annotations

import itertools

import pandas as pd

from ..errors import SkipTag   # re-export：标签侧统一从 _base 引用

RESULT_COLS = ["stock_code", "key_value", "num_value", "confidence"]


def _conn():
    """开一个数据库连接（上下文管理器）。"""
    from db import get_conn

    return get_conn()


def _read_sql(conn, sql: str, params=None) -> pd.DataFrame:
    """用 psycopg2 原生游标取数（pandas 官方不支持 psycopg2 连接，会刷警告）。

    小结果集（万行级以内）用本函数；大表（全市场行情等百万行级）必须用
    `_read_sql_stream` —— fetchall 会在客户端同时驻留全量 Python 对象列表与
    DataFrame 两份，是全市场窗口计算内存雪崩的直接来源（2026-09-29/30 事故）。
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


_STREAM_SEQ = itertools.count(1)   # 命名游标序号（同一连接内名字必须唯一）


def _cast_dtypes(df: pd.DataFrame, dtypes: dict | None) -> pd.DataFrame:
    """按 spec 就地做 dtype 瘦身（大表分批取数专用，见 _read_sql_stream）。

    spec 形如 {"stock_code": "category", "trade_date": "datetime64[ns]"}：
    datetime 前缀走 pd.to_datetime，其余直接 astype（含 category / float32）。
    列可能不存在（SQL 选择集变化）时跳过，不报错。
    """
    if not dtypes:
        return df
    for col, dt in dtypes.items():
        if col not in df.columns:
            continue
        if str(dt).startswith("datetime"):
            df[col] = pd.to_datetime(df[col])
        else:
            df[col] = df[col].astype(dt)
    return df


def _distinct_codes(conn, table: str, start, as_of, time_col: str = "trade_date") -> list[str]:
    """[start, as_of] 窗口内出现过数据的股票清单（升序）。

    供「按股票分批加载」的标签域先取批次划分：DISTINCT + ORDER BY 都由 PG
    完成，客户端只收几千行 —— 配合大表上的 (stock_code, <time_col>) 索引可走
    index-only scan，不产生大结果集（内存背景见 _read_sql_stream）。
    表名/列名为调用方传入的内部常量，不接受外部输入。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT stock_code FROM {table} "
            f"WHERE {time_col} BETWEEN %s AND %s ORDER BY stock_code",
            (start, as_of),
        )
        return [r[0] for r in cur.fetchall()]


def _read_sql_stream(conn, sql: str, params=None, chunk_size: int = 50_000,
                     dtypes: dict | None = None) -> pd.DataFrame:
    """流式取数（PG 服务端游标逐批回传），百万行级大表专用。

    与 `_read_sql` 的差别：fetchall 的「全量 Python 对象列表」与最终 DataFrame
    会同时驻留（全市场行情 ~220 万行、多个 object 列，实测峰值 1GB+），叠加
    concat/merge/sort 的中间副本后把 2GB 内存的机器打穿（2026-09-29/30 两次
    整机内存-IO 雪崩的直接来源）。本函数用 psycopg2 命名游标让 PG 逐批回传，
    客户端每批即时转 DataFrame（并按 dtypes 立即瘦身）后累积 —— 峰值只与
    单批大小相关，与总行数脱钩。

    chunk_size: 每批行数（也是 itersize）。
    dtypes: 传给 _cast_dtypes 的瘦身规格；建议在批次内就转 category/datetime，
            否则拼回终态时 object 列的累积峰值仍会回来。
    """
    if not chunk_size or chunk_size <= 0:
        return _read_sql(conn, sql, params)

    frames: list[pd.DataFrame] = []
    cols: list[str] | None = None
    name = f"_stream_{next(_STREAM_SEQ)}"
    with conn.cursor(name=name) as cur:
        cur.itersize = chunk_size
        cur.execute(sql, params)
        # 注意：命名游标的 description 要等第一次 FETCH 之后才有 ——
        # execute() 只发 DECLARE CURSOR，服务端此时不返回结果集元数据
        # （实测 execute 后 cur.description 为 None，直接取会 TypeError）。
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            if cols is None:
                cols = [d[0] for d in cur.description]
            frames.append(_cast_dtypes(pd.DataFrame(rows, columns=cols), dtypes))
    if not frames:
        # 空结果：命名游标拿不到列名，返回空帧（调用方都以 .empty 判空后再用）
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


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
