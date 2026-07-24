#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_backfill.py — 从本机 PostgreSQL 导出指定日期的表数据为「幂等回补 SQL」。

产物： INSERT ... ON CONFLICT DO NOTHING 形态的 .sql 文件，
       复制到服务器后用 `psql -h localhost -U market_user -d market_db -f <file>` 执行。

固化的防坑点（来自 2026-07-08 / 2026-07-20 两次回补经验）：
  1. 去重键自动从 pg_constraint 读取表的 UNIQUE 约束，不手动写（避免写错/漏写）。
  2. 自动排除 serial 自增主键 id（服务器侧自增，跨库带值会冲突或乱序）。
  3. 时间戳导出原始值（带秒），绝不 date_trunc，否则去重键对不上 → 静默重复插入。
  4. 列与类型自适应：从 information_schema 读列；timestamp 加 ::timestamptz，
     字符串转义，NULL 处理；表加列后无需改本脚本。

用法：
  python3 gen_backfill.py --date 2026-07-20
  python3 gen_backfill.py --date 2026-07-20 --tables tick_data
  python3 gen_backfill.py --date 2026-07-20 --stocks HK.00700,SH.600900
  python3 gen_backfill.py --date 2026-07-20 --dry-run      # 只打印各表行数，不落文件
  python3 gen_backfill.py --date 2026-07-20 --outdir /tmp   # 指定输出目录

服务器端执行（生成 .sql 后）：
  PGPASSWORD='<pwd>' psql -h localhost -U market_user -d market_db -f tick_data_backfill_YYYYMMDD.sql
  PGPASSWORD='<pwd>' psql -h localhost -U market_user -d market_db -f trend_snapshot_backfill_YYYYMMDD.sql

验证（服务器侧）：
  SELECT 'tick' t, stock_code, COUNT(*) FROM tick_data
    WHERE tick_time::DATE='YYYY-MM-DD' GROUP BY stock_code
  UNION ALL
  SELECT 'trend', stock_code, COUNT(*) FROM trend_snapshot
    WHERE snapshot_time::DATE='YYYY-MM-DD' GROUP BY stock_code;
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))


def load_conn_params():
    from config import val
    return dict(
        host=val("database", "host", "DB_HOST", "localhost"),
        port=int(val("database", "port", "DB_PORT", "5432")),
        dbname=val("database", "dbname", "DB_NAME", "market_db"),
        user=val("database", "user", "DB_USER", "market_user"),
        password=val("database", "password", "DB_PASSWORD", ""),
    )


def get_columns(cur, table):
    """返回 (列名列表, {列名: data_type})。

    自动排除以下列，避免插入时报错：
      - id：serial 自增主键，服务器侧自增
      - 生成列（attgenerated='s'，GENERATED ALWAYS AS ... STORED）
      - 派生列：DEFAULT 表达式引用了同表其他列（如 snapshot_date 由 snapshot_time 派生）
        这类列服务器不允许手动插入，留空由数据库自动计算。
    """
    cur.execute(
        """
        SELECT a.attname,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attgenerated,
               COALESCE(pg_get_expr(d.adbin, d.adrelid), '') AS def
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %s::regclass
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (table,),
    )
    rows = cur.fetchall()
    all_names = {r[0] for r in rows}
    # 易变函数：这类 DEFAULT 的审计列由服务器自动填充，且旧服务器 schema 可能没有该列，
    # 导出时跳过以避免 "column does not exist" 或插入非 DEFAULT 值报错。
    volatile = ("now()", "current_timestamp", "current_date",
                "clock_timestamp()", "timeofday()", "transaction_timestamp()",
                "statement_timestamp()")
    cols = []
    types = {}
    for name, dtype, attgen, default in rows:
        if name == "id":
            continue
        if attgen == "s":  # GENERATED ALWAYS AS (...) STORED
            continue
        def_low = (default or "").lower()
        # 派生列：DEFAULT 表达式引用了其他列 → 由数据库自动计算，跳过
        if def_low and any(n.lower() in def_low for n in all_names if n != name):
            continue
        # 审计列：DEFAULT 是易变函数（now() 等）→ 跳过，服务器自动填充
        if def_low and any(v in def_low for v in volatile):
            continue
        cols.append(name)
        types[name] = dtype
    return cols, types


def get_conflict_cols(cur, table):
    """返回该表第一个「不含 id 列」的 UNIQUE 约束列列表，作为 ON CONFLICT 去重键。
    优先选纯 UNIQUE（非主键）；若无合适 UNIQUE 则返回空列表（调用方报错）。"""
    cur.execute(
        """
        SELECT conkey
        FROM pg_constraint
        WHERE conrelid = %s::regclass
          AND contype = 'u'          -- UNIQUE（不含主键 p）
        ORDER BY array_length(conkey, 1) ASC
        LIMIT 1
        """,
        (table,),
    )
    row = cur.fetchone()
    if not row:
        return []
    # conkey 是 int2vector（attnum 数组），需转成列名
    attnums = row[0]
    cur.execute(
        """
        SELECT attname FROM pg_attribute
        WHERE attrelid = %s::regclass AND attnum = ANY(%s)
        """,
        (table, list(attnums)),
    )
    return [r[0] for r in cur.fetchall()]


def get_time_column(cols, types):
    """推断用作日期过滤的时间列：优先名字含 time 的 timestamptz 列，
    否则名字含 date 的列。"""
    for c in cols:
        if "time" in c.lower() and "timestamp" in types[c].lower():
            return c
    for c in cols:
        if "date" in c.lower():
            return c
    raise RuntimeError("无法推断时间列，请检查表结构")


def fmt_value(val, dtype):
    """按列类型把 Python 值格式化为 SQL 字面量。"""
    if val is None:
        return "NULL"
    dl = dtype.lower()
    if "timestamp" in dl:
        # psycopg2 返回 datetime，str 形如 '2026-07-20 09:30:12.722355+08:00'
        return "'{}'::timestamptz".format(str(val))
    if "date" in dl and "timestamp" not in dl:
        return "'{}'::date".format(str(val))
    if any(k in dl for k in ("numeric", "integer", "bigint", "real", "double")):
        return str(val)
    if "boolean" in dl:
        return "TRUE" if val else "FALSE"
    # 文本类：单引号包裹，内部单引号翻倍
    s = str(val).replace("'", "''")
    return "'{}'".format(s)


def gen_table(cur, table, day, stocks, batch, outdir, dry_run):
    cols, types = get_columns(cur, table)
    if not cols:
        print(f"[跳过] {table}: 无可用列（可能只有 id）")
        return
    conflict = get_conflict_cols(cur, table)
    if not conflict:
        print(f"[跳过] {table}: 找不到不含 id 的 UNIQUE 约束，无法安全去重，请手动处理")
        return
    time_col = get_time_column(cols, types)

    where = [f"{time_col}::DATE = %s"]
    params = [day]
    if stocks:
        placeholders = ",".join(["%s"] * len(stocks))
        where.append(f"stock_code IN ({placeholders})")
        params.extend(stocks)

    col_sql = ", ".join(cols)
    conflict_sql = ", ".join(conflict)

    # 先取总行数用于 dry-run / 报告
    cur.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {' AND '.join(where)}", params
    )
    total = cur.fetchone()[0]

    if dry_run:
        print(f"[dry-run] {table}: {total} 行, 去重键=({conflict_sql}), 时间列={time_col}")
        return

    # 流式读取，分批写 INSERT
    out_path = os.path.join(outdir, f"{table}_backfill_{day.strftime('%Y%m%d')}.sql")
    select_sql = (
        f"SELECT {col_sql} FROM {table} WHERE {' AND '.join(where)} "
        f"ORDER BY {conflict_sql}"
    )
    cur.execute(select_sql, params)

    header = (
        f"-- backfill {table} for {day}\n"
        f"-- conflict key: ({conflict_sql})\n"
        f"-- time filter: {time_col}::DATE = '{day}'\n"
        f"-- generated by gen_backfill.py\n\n"
    )

    written = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        rows_buf = []
        while True:
            row = cur.fetchone()
            if row is None:
                break
            vals = "(" + ", ".join(fmt_value(v, types[c]) for v, c in zip(row, cols)) + ")"
            rows_buf.append(vals)
            if len(rows_buf) >= batch:
                fh.write(
                    f"INSERT INTO {table} ({col_sql}) VALUES\n"
                    + ",\n".join(rows_buf)
                    + f"\nON CONFLICT ({conflict_sql}) DO NOTHING;\n\n"
                )
                written += len(rows_buf)
                rows_buf = []
        if rows_buf:
            fh.write(
                f"INSERT INTO {table} ({col_sql}) VALUES\n"
                + ",\n".join(rows_buf)
                + f"\nON CONFLICT ({conflict_sql}) DO NOTHING;\n\n"
            )
            written += len(rows_buf)

    print(f"[生成] {out_path}: 导出 {written} 行 (总计 {total} 行), 去重键=({conflict_sql})")


def main():
    ap = argparse.ArgumentParser(description="生成本机库指定日期的幂等回补 SQL")
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"),
                    help="回补日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--tables", default="tick_data,trend_snapshot",
                    help="逗号分隔的表名（默认 tick_data,trend_snapshot）")
    ap.add_argument("--stocks", default="",
                    help="逗号分隔的股票代码过滤（默认全部）")
    ap.add_argument("--batch", type=int, default=2000, help="每批 INSERT 行数（默认 2000）")
    ap.add_argument("--outdir", default=HERE, help="输出目录（默认脚本所在目录）")
    ap.add_argument("--dry-run", action="store_true", help="只打印各表行数，不落文件")
    args = ap.parse_args()

    try:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"日期格式错误: {args.date!r}，应为 YYYY-MM-DD")
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]

    conn = psycopg2.connect(**load_conn_params())
    try:
        cur = conn.cursor()
        for table in tables:
            gen_table(cur, table, day, stocks, args.batch, args.outdir, args.dry_run)
    finally:
        conn.close()

    if not args.dry_run:
        print("\n完成。将生成的 .sql 复制到服务器后执行：")
        print(f"  PGPASSWORD='<pwd>' psql -h localhost -U market_user -d market_db -f <table>_backfill_{day.strftime('%Y%m%d')}.sql")


if __name__ == "__main__":
    main()
