#!/usr/bin/env python3
# ============================================================================
# _schema_diff.py — 数据库结构差异检测 + 生成修复 SQL（只读，绝不修改数据库）
# ----------------------------------------------------------------------------
# 设计原则（安全优先）:
#   ★ 本脚本【只读取】数据库结构，【绝不执行任何 DDL】。
#   ★ 检测到差异后，仅【生成修复用的 SQL 语句】并打印 / 写入文件。
#   ★ 所有实际修改由用户拿生成的 SQL 自行手动执行（可先 review 再跑）。
#   ★ 即便用户误操作，脚本本身也不会动数据库一根汗毛。
#
# 职责:
#   1. 读取 $APP_DIR/sql/schema.sql（由 deploy.sh 同步过来的最新版）得到"期望结构"
#   2. 只读连接本机 PostgreSQL，读出"实际结构"（SELECT 查询，不改库）
#   3. 语义化 diff，分类:
#        - 新增表 / 新增列 / 新增索引 / 视图变更   → 低风险
#        - 删除列 / 删除索引 / 修改列类型          → 高风险（会丢数据 / 改结构）
#   4. 把所有修复 SQL 收集起来，输出到屏幕并写入带时间戳的文件，供用户手动执行。
#
# 依赖: 仅标准库 + 服务器上的 psql 客户端（只读查询用，PGPASSWORD 连 localhost）
# 调用方: sql/sync_schema.sh（本机 ssh -t 到此脚本）
# ============================================================================
import configparser
import os
import re
import subprocess
import sys

APP_DIR = os.environ.get("APP_DIR", "/home/mkt/EquityMarketAnalysis")
SCHEMA_PATH = os.path.join(APP_DIR, "sql", "schema.sql")


# ── 读取 config.conf 取连接参数 ──────────────────────────────────────────────
def load_db_conf():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(APP_DIR, "config.conf"))
    sec = "database"
    return {
        "host": cfg.get(sec, "host", fallback="localhost"),
        "port": cfg.get(sec, "port", fallback="5432"),
        "dbname": cfg.get(sec, "dbname", fallback="market_db"),
        "user": cfg.get(sec, "user", fallback="market_user"),
        "password": cfg.get(sec, "password", fallback=""),
    }


# ── psql 查询工具 ────────────────────────────────────────────────────────────
def psql_query(db, sql):
    env = dict(os.environ)
    env["PGPASSWORD"] = db["password"]
    cmd = [
        "psql", "-h", db["host"], "-p", str(db["port"]),
        "-U", db["user"], "-d", db["dbname"],
        "-tA", "-F|", "-c", sql,
    ]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        sys.exit("[x] 服务器未找到 psql 客户端，无法比对数据库")
    if out.returncode != 0:
        sys.exit(f"[x] psql 查询失败:\n{out.stderr.strip()}")
    return out.stdout


# ── 类型规范化：让 schema.sql 的类型写法与 information_schema 对齐 ─────────────
def parse_index_signature(def_text):
    """从 CREATE INDEX / UNIQUE 约束文本提取内容指纹，用于"同名不同名但内容一致则不算差异"。

    返回 (tablename_lower, [(col_lower, direction), ...], is_unique)。
    direction 为 'asc' / 'desc'，保留方向是因为 PG 中 (a, b DESC) 与 (a, b) 是不同索引。
    解析失败返回 None。
    """
    def_text = def_text.strip()
    if not def_text:
        return None
    is_unique = bool(re.search(r"\bUNIQUE\b", def_text, re.I))
    # 表名: ON [schema.]tbl （后面可能跟 USING btree 等，不直接是 '('）
    mon = re.search(r"\bON\s+(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)", def_text, re.I)
    if not mon:
        return None
    tbl = mon.group(1).lower()
    # 列定义: 取文本中第一个 '(' 到匹配的右括号
    open_p = def_text.find("(")
    if open_p < 0:
        return None
    depth = 0
    close_p = -1
    for k in range(open_p, len(def_text)):
        if def_text[k] == "(":
            depth += 1
        elif def_text[k] == ")":
            depth -= 1
            if depth == 0:
                close_p = k
                break
    if close_p < 0:
        return None
    cols_body = def_text[open_p + 1:close_p]
    cols = []
    for part in cols_body.split(","):
        part = part.strip()
        if not part or part.upper().startswith("INCLUDE"):
            continue
        # 取列名（第一个 token），方向取 DESC（若有）
        cm = re.match(r"([A-Za-z_][\w]*)\s*(DESC|ASC)?", part, re.I)
        if not cm:
            continue
        col = cm.group(1).lower()
        direction = "desc" if cm.group(2) and cm.group(2).upper() == "DESC" else "asc"
        cols.append((col, direction))
    if not cols:
        return None
    return (tbl, tuple(cols), is_unique)


def norm_type(t):
    if not t:
        return ""
    t = t.strip().lower()
    t = t.replace("varchar", "character varying")
    t = t.replace("timestamptz", "timestamp with time zone")
    t = t.replace("datetime", "timestamp")
    # 去空格前先把 "timestamp without time zone" 归一为 "timestamp"，
    # 否则期望侧 TIMESTAMP -> timestamp，实际侧 timestamp without time zone
    # -> timestampwithouttimezone，两侧不一致会误报列类型变更。
    t = t.replace("timestamp without time zone", "timestamp")
    t = t.replace("float8", "double precision")
    # serial 系列 -> 底层整数类型（必须在 int/integer 处理之前）
    t = t.replace("bigserial", "bigint")
    t = t.replace("smallserial", "smallint")
    t = t.replace("serial", "integer")
    t = t.replace("int4", "integer").replace("int8", "bigint")
    # int -> integer / char -> character：用词边界正则，避免把 bigint 变成 biginteger
    t = re.sub(r"\bint\b", "integer", t)
    t = re.sub(r"\bchar\b", "character", t)
    t = t.replace(" ", "")
    t = re.sub(r",\s*", ",", t)          # numeric(12, 4) -> numeric(12,4)
    t = re.sub(r"\(\s*", "(", t)
    t = re.sub(r"\s*\)", ")", t)
    return t


# ── 解析本地 schema.sql → 期望结构 ───────────────────────────────────────────
def parse_schema(text):
    tables = {}      # name -> {cols: {col: type}, order: [col,...]}
    indexes = {}     # indexname -> tablename
    views = set()
    cur_table = None

    # 预处理：去掉注释行
    lines = []
    for line in text.splitlines():
        # 去掉 -- 注释（不处理字符串内的，schema.sql 注释都在行首或行尾独立）
        line = re.sub(r"\s*--.*$", "", line)
        lines.append(line)

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        # 视图
        m = re.match(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+([A-Za-z_][\w]*)", line, re.I)
        if m:
            views.add(m.group(1).lower())
            i += 1
            continue
        # 建表开始
        m = re.match(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", line, re.I)
        if m:
            tname = m.group(1).lower()
            tables.setdefault(tname, {"cols": {}, "order": []})
            cur_table = tname
            # 可能 CREATE TABLE x ( ... ) 同行
            rest = line[m.end():]
            if "(" in rest:
                i = parse_columns(lines, i, rest, tables, indexes, tname)
                cur_table = None
                continue
            else:
                i += 1
                continue
        # 索引（CREATE INDEX 可能跨行：名字在一行，ON 在下一行，或带 INCLUDE 子句）
        m = re.match(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", line, re.I)
        if m:
            # 若本行没有 ON，向后拼接后续行直到出现 ON（或语句结束）
            buf = line
            j = i
            while " ON " not in re.sub(r"\([^)]*\)", "", buf) and j + 1 < len(lines):
                j += 1
                buf = buf + " " + lines[j]
            m2 = re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s+ON\s+(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)", buf, re.I)
            if m2:
                name = m2.group(1).lower()
                tbl = m2.group(2).lower()
                indexes[name] = {"tbl": tbl, "sig": parse_index_signature(buf)}
            i = j + 1
            continue
        # 在建表块内解析列
        if cur_table and line:
            cm = parse_column_line(line, cur_table, tables)
            if cm == "TABLE_END":
                cur_table = None
                i += 1
                continue
        i += 1
    return tables, indexes, views


def parse_columns(lines, idx, first_rest, tables, indexes, tname):
    # first_rest 是 " ( col type, col2 type, ... )" 剩余部分
    buf = first_rest
    depth = buf.count("(") - buf.count(")")
    i = idx
    # 处理第一行剩余
    while True:
        if ")" in buf and depth <= 0:
            # 表定义结束（取 ) 之前部分）
            buf = buf[:buf.index(")")]
            if buf.strip():
                parse_column_line(buf.strip(), tname, tables)
                colm = re.match(r"^([A-Za-z_][\w]*)", buf.strip())
                collect_inline_indexes(buf.strip(), indexes, tname,
                                       colm.group(1) if colm else None)
            return i + 1
        # 还没结束，继续读行
        parse_column_line(buf.strip(), tname, tables)
        colm = re.match(r"^([A-Za-z_][\w]*)", buf.strip())
        collect_inline_indexes(buf.strip(), indexes, tname,
                               colm.group(1) if colm else None)
        i += 1
        if i >= len(lines):
            return i
        buf = lines[i]
        depth += buf.count("(") - buf.count(")")


# 从建表块的一行中，提取内联 PRIMARY KEY / UNIQUE 约束对应的"物理索引名"，
# 补进期望索引集合。PG 对约束会自动建物理索引：
#   - PRIMARY KEY                 -> {table}_pkey
#   - CONSTRAINT name UNIQUE/PRIMARY KEY -> name
#   - 匿名 UNIQUE (a, b)          -> {table}_{首列}_key （PG 实际命名规则）
# 这样期望侧能和实际侧 pg_indexes 返回的主键/唯一索引对齐，避免把它们误判为"要删"。
def collect_inline_indexes(line, indexes, tname, current_col=None):
    line = line.strip()
    if not line:
        return
    # 显式命名约束: CONSTRAINT xxx PRIMARY KEY / UNIQUE (...)
    m = re.match(r"CONSTRAINT\s+([A-Za-z_][\w]*)\s+(PRIMARY\s+KEY|UNIQUE)", line, re.I)
    if m:
        name = m.group(1).lower()
        unique = bool(re.search(r"UNIQUE", m.group(2), re.I))
        cols = _extract_constraint_cols(line, current_col)
        indexes[name] = {"tbl": tname, "sig": parse_index_signature(
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {tname} ({cols})")}
        return
    # 内联 PRIMARY KEY（表级带括号，或列级无括号）
    if re.search(r"\bPRIMARY\s+KEY\b", line, re.I):
        name = f"{tname}_pkey"
        cols = _extract_constraint_cols(line, current_col, primary=True)
        indexes[name] = {"tbl": tname, "sig": parse_index_signature(
            f"CREATE UNIQUE INDEX {name} ON {tname} ({cols})")}
        return
    # 匿名 UNIQUE (a, b, ...)  -> {table}_{首列}_key
    m = re.match(r"UNIQUE\s*\(\s*([A-Za-z_][\w]*)", line, re.I)
    if m:
        name = f"{tname}_{m.group(1).lower()}_key"
        cols = _extract_constraint_cols(line, current_col)
        indexes[name] = {"tbl": tname, "sig": parse_index_signature(
            f"CREATE UNIQUE INDEX {name} ON {tname} ({cols})")}
        return


def _extract_constraint_cols(line, current_col, primary=False):
    """从约束行提取列名列表（逗号分隔）。列级约束（PRIMARY KEY/UNIQUE 后无括号）
    回退到当前正在解析的列名 current_col。"""
    if primary:
        colm = re.search(r"PRIMARY\s+KEY\s*\((.*?)\)", line, re.I)
    else:
        colm = re.search(r"\(\s*(.*?)\s*\)", line, re.I)
    if colm:
        cols = [c.strip().split()[0] for c in colm.group(1).split(",") if c.strip()]
        if cols:
            return ",".join(cols)
    # 列级约束（无括号）：用当前列名
    if current_col:
        return current_col
    return ""


# 匹配"表级约束行"（非列定义），用于在解析建表块时跳过。
# 注意: 末尾不能留空分支 '|'，否则会零宽匹配任意行，把所有列都误判为约束跳过。
_COL_KW = re.compile(
    r"^\s*(PRIMARY\s+KEY|UNIQUE|CONSTRAINT|CHECK|FOREIGN|EXCLUDE|LIKE)\b", re.I)


def parse_column_line(line, tname, tables):
    line = line.rstrip(",").strip()
    if not line:
        return
    if line in (")",):
        return "TABLE_END"
    if ")" in line and line.endswith(")"):
        # 可能是表尾 ) 带分号
        if re.match(r"^[\);]+$", line):
            return "TABLE_END"
    # 跳过约束行
    if _COL_KW.match(line):
        if line.startswith(")"):
            return "TABLE_END"
        return
    if line.startswith(")"):
        return "TABLE_END"
    # 列定义: name TYPE [约束...]
    m = re.match(r"^([A-Za-z_][\w]*)\s+([A-Za-z][\w()\s,]*?)(?:\s+(?:NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|UNIQUE|REFERENCES|CHECK|GENERATED|COLLATE|ON\s+UPDATE|ON\s+DELETE).*)?$", line, re.I)
    if not m:
        # 可能是 GENERATED ALWAYS 等复杂列，尝试宽松匹配 name + 到第一个空格后的类型块
        m2 = re.match(r"^([A-Za-z_][\w]*)\s+([A-Za-z][\w()\s,]*)$", line)
        if not m2:
            return
        cname, ctype = m2.group(1), m2.group(2)
    else:
        cname, ctype = m.group(1), m.group(2)
    cname = cname.lower()
    ctype = norm_type(ctype.strip())
    tables[tname]["cols"][cname] = ctype
    if cname not in tables[tname]["order"]:
        tables[tname]["order"].append(cname)


# ── 读取实际结构 ──────────────────────────────────────────────────────────────
def read_actual(db):
    tables = {}
    # 列（含类型）
    sql = (
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) "
        "FROM pg_class c "
        "JOIN pg_attribute a ON a.attrelid = c.oid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY c.relname, a.attnum;"
    )
    out = psql_query(db, sql)
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) < 3:
            continue
        t, c, typ = parts[0], parts[1], parts[2]
        tables.setdefault(t, {"cols": {}, "order": []})
        tables[t]["cols"][c] = norm_type(typ)
        tables[t]["order"].append(c)

    # 索引（含 indexdef，用于按"内容指纹"比对，避免同名不同名误报）
    # 返回结构: indexes[name] = {tbl, sig}
    indexes = {}
    sql = ("SELECT indexname, tablename, indexdef "
           "FROM pg_indexes WHERE schemaname = 'public';")
    out = psql_query(db, sql)
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) < 3:
            continue
        iname, tbl, indexdef = parts[0], parts[1], parts[2]
        sig = parse_index_signature(indexdef)
        indexes[iname] = {"tbl": tbl, "sig": sig}

    # 视图
    views = set()
    sql = "SELECT table_name FROM information_schema.views WHERE table_schema = 'public';"
    out = psql_query(db, sql)
    for row in out.splitlines():
        if row.strip():
            views.add(row.strip().lower())

    return tables, indexes, views


# ── 生成修复 SQL（仅拼接字符串，绝不执行）─────────────────────────────────────
# 说明: 以下所有函数只负责产出一条 SQL 文本，交给用户自行 review / 手动执行。
# 脚本本身不调用 psql 执行任何写操作。

def gen_ddl_create_table(t, cols):
    col_defs = ", ".join(f'"{c}" {ty}' for c, ty in cols.items())
    return f'CREATE TABLE IF NOT EXISTS "{t}" ({col_defs});'


def gen_ddl_add_column(t, col, typ):
    return (f'DO $$ BEGIN IF NOT EXISTS ('
            f'SELECT 1 FROM information_schema.columns '
            f"WHERE table_name='{t}' AND column_name='{col}'"
            f') THEN ALTER TABLE "{t}" ADD COLUMN "{col}" {typ}; END IF; END $$;')


def gen_ddl_drop_column(t, col):
    # 高风险: 会丢失该列数据。生成的 SQL 默认注释掉，需用户手动取消注释才执行。
    return f'ALTER TABLE "{t}" DROP COLUMN IF EXISTS "{col}";'


def gen_ddl_drop_index(idx):
    # 高风险: 删索引。生成的 SQL 默认注释掉，需用户手动取消注释才执行。
    return f'DROP INDEX IF EXISTS "{idx}";'


def gen_ddl_alter_type(t, col, new):
    # 高风险: 改列类型可能丢精度 / 破坏自增。默认注释掉，需用户手动取消注释才执行。
    return f'ALTER TABLE "{t}" ALTER COLUMN "{col}" TYPE {new};'


def main():
    print("=" * 64)
    print("  数据库增量同步 (schema.sql ↔ 目标库)")
    print("=" * 64)
    if not os.path.exists(SCHEMA_PATH):
        sys.exit(f"[x] 找不到 {SCHEMA_PATH}")

    db = load_db_conf()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_text = f.read()

    exp_tables, exp_indexes, exp_views = parse_schema(schema_text)
    act_tables, act_indexes, act_views = read_actual(db)

    # 计算差异
    # 注意: 目标库可能是多项目共用。本项目只拥有"schema.sql 声明范围内"的对象，
    # 绝不触碰未知表/索引（那些属于其他项目）。
    new_tables = [t for t in exp_tables if t not in act_tables]

    # other_tables: 库里有、但本项目未声明的表。在共用库里这绝大多数是其他
    # 项目的表，不能自动提议 DROP。仅记录、提示，不进入任何危险操作列表。
    other_tables = [t for t in act_tables if t not in exp_tables and t not in act_views]

    add_cols = []     # (table, col, type)
    alter_cols = []   # (table, col, old, new)
    drop_cols = []    # (table, col) —— 仅限本项目已声明表内
    for t in exp_tables:
        if t not in act_tables:
            continue
        exp_c = exp_tables[t]["cols"]
        act_c = act_tables[t]["cols"]
        for col, typ in exp_c.items():
            if col not in act_c:
                add_cols.append((t, col, typ))
            elif act_c[col] != typ:
                alter_cols.append((t, col, act_c[col], typ))
        for col in act_c:
            if col not in exp_c:
                drop_cols.append((t, col))

    # 索引按"内容指纹"比对：同名不同名只要列/方向/唯一性一致就互相抵消，
    # 不算差异（例如旧约束索引 daily_quote_stock_code_trade_date_key 与
    # 新索引 idx_daily_quote_stock_date 内容一致，仅名字不同，无需改动）。
    # exp_indexes / act_indexes 的值为 {tbl, sig}
    from collections import defaultdict
    act_sigs = defaultdict(set)   # tbl -> set(sig)
    for info in act_indexes.values():
        if info.get("sig"):
            act_sigs[info["tbl"]].add(info["sig"])
    exp_sigs = defaultdict(set)
    for info in exp_indexes.values():
        if info.get("sig"):
            exp_sigs[info["tbl"]].add(info["sig"])

    new_indexes = []   # 期望有、实际没有相同指纹 -> 真新增
    for idx, info in exp_indexes.items():
        tbl = info["tbl"]
        if tbl not in exp_tables:
            continue
        if info.get("sig") and info["sig"] in act_sigs.get(tbl, set()):
            continue  # 实际库已有相同内容的索引（可能名字不同），跳过
        new_indexes.append(idx)

    drop_indexes = []  # 实际有、期望没有相同指纹 -> 真多余（仍限定本项目表）
    for idx, info in act_indexes.items():
        tbl = info["tbl"]
        if tbl not in exp_tables:
            continue  # 其他项目的表/索引，不提议删除
        if info.get("sig") and info["sig"] in exp_sigs.get(tbl, set()):
            continue  # 期望侧已有相同内容的索引（可能名字不同），跳过
        drop_indexes.append(idx)
    new_views = [v for v in exp_views if v not in act_views]
    changed_views = [v for v in exp_views if v in act_views]

    total = (len(new_tables) + len(add_cols) + len(alter_cols)
             + len(drop_cols) + len(new_indexes) + len(drop_indexes)
             + len(new_views) + len(changed_views))

    if total == 0:
        print("\n[i] 数据库结构与 schema.sql 一致，无需变更。")
        return

    print(f"\n检测到 {total} 项差异:\n")

    # 低风险：新增
    print("── 新增（低风险）──")
    for t in new_tables:
        print(f"  + 表      {t}  ({len(exp_tables[t]['cols'])} 列)")
    for t, col, typ in add_cols:
        print(f"  + 列      {t}.{col}  {typ}")
    for idx in new_indexes:
        t = exp_indexes[idx]["tbl"]
        print(f"  + 索引    {idx} ON {t}")
    for v in new_views:
        print(f"  + 视图    {v}")
    for v in changed_views:
        print(f"  ~ 视图    {v} (CREATE OR REPLACE)")

    # 多项目共用库提示: 跳过非本项目的表/索引，绝不提议删除
    if other_tables:
        print(f"\n[i] 跳过 {len(other_tables)} 张不在本项目 schema 中的表"
              f"（可能是其他项目，已忽略，不会删除）:")
        for t in other_tables[:50]:
            print(f"    · {t}")
        if len(other_tables) > 50:
            print(f"    · … 及另外 {len(other_tables) - 50} 张")

    # 高风险：删除 / 修改（仅限本项目已声明对象）
    if drop_cols or drop_indexes or alter_cols:
        print("\n── 破坏性 / 高风险 ──")
        for t, col in drop_cols:
            print(f"  - 列(删)  {t}.{col}  ← 将丢失该列数据!")
        for idx in drop_indexes:
            t = act_indexes[idx]["tbl"]
            print(f"  - 索引(删){idx} ON {t}")
        for t, col, old, new in alter_cols:
            print(f"  ~ 列(改)  {t}.{col}: {old} → {new}")

    # ── 只生成 SQL，绝不执行 ───────────────────────────────────────────────
    # 低风险 SQL 直接生成（可放心执行）；高风险 SQL 默认注释掉，必须用户手动
    # 取消注释确认后才执行，避免误删 / 误改生产数据。
    print("\n正在生成修复 SQL ...")
    sql_lines = []
    sql_lines.append("-- ===========================================================")
    sql_lines.append("-- 数据库结构修复 SQL（由 _schema_diff.py 自动生成）")
    sql_lines.append("-- 生成时间: " + _now())
    sql_lines.append("-- 来源 schema.sql: " + SCHEMA_PATH)
    sql_lines.append("--")
    sql_lines.append("-- ⚠ 安全须知:")
    sql_lines.append("--   1. 本文件由脚本【自动生成】，脚本本身【未】连接数据库执行。")
    sql_lines.append("--   2. 请先仔细 review 以下 SQL，确认无误后再手动执行。")
    sql_lines.append("--   3. 被注释掉的【高风险】语句需你确认后取消注释才生效。")
    sql_lines.append("--   4. 建议执行前先备份: pg_dump --schema-only -f before.sql <db>")
    sql_lines.append("-- ===========================================================")
    sql_lines.append("")

    # 低风险: 新增
    if new_tables or add_cols or new_indexes or new_views or changed_views:
        sql_lines.append("-- ── 低风险：新增表 / 列 / 索引 / 视图 ──")
        for t in new_tables:
            sql_lines.append(gen_ddl_create_table(t, exp_tables[t]["cols"]))
        for t, col, typ in add_cols:
            sql_lines.append(gen_ddl_add_column(t, col, typ))
        for idx in new_indexes:
            ddl = extract_index_ddl(schema_text, idx)
            if ddl:
                sql_lines.append(ddl)
        for v in new_views + changed_views:
            ddl = extract_view_ddl(schema_text, v)
            if ddl:
                sql_lines.append(ddl)
        sql_lines.append("")

    # 高风险: 删列 / 删索引 / 改类型 —— 默认注释，需手工确认
    if drop_cols or drop_indexes or alter_cols:
        sql_lines.append("-- ── 高风险：删除列 / 删除索引 / 修改列类型 ──")
        sql_lines.append("-- ⚠ 以下语句默认被注释。请逐条确认无误后，删除行首 '-- ' 再执行。")
        sql_lines.append("--    删除列会丢失该列所有数据；删除索引会移除约束/性能；")
        sql_lines.append("--    修改列类型可能丢精度或破坏自增序列。")
        for t, col in drop_cols:
            sql_lines.append("-- " + gen_ddl_drop_column(t, col))
        for idx in drop_indexes:
            sql_lines.append("-- " + gen_ddl_drop_index(idx))
        for t, col, old, new in alter_cols:
            sql_lines.append("-- " + gen_ddl_alter_type(t, col, new))
        sql_lines.append("")

    sql_text = "\n".join(sql_lines)

    # 打印到屏幕
    print("\n" + "=" * 64)
    print("  修复 SQL（脚本未执行，请 review 后手动执行）")
    print("=" * 64)
    print(sql_text)

    # 写入带时间戳的文件，方便下载/复核
    out_path = os.path.join(APP_DIR, "sql",
                            "schema_fix_" + _stamp() + ".sql")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(sql_text + "\n")
        print(f"\n[+] 修复 SQL 已写入: {out_path}")
    except OSError as e:
        print(f"\n[!] 写入文件失败（不影响上方已打印的 SQL）: {e}")

    print("\n[i] 脚本仅做检测，未对数据库做任何修改。请复制上方 SQL 或打开生成的文件，"
          "确认无误后手动执行。")


def extract_index_ddl(text, idx):
    # 找到 CREATE ... INDEX idx ON ... 的完整语句（到 ; 或行尾）
    pat = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        + re.escape(idx) + r"\s+ON\s+.*?;",
        re.I | re.S)
    m = pat.search(text)
    return m.group(0).strip() if m else None


def extract_view_ddl(text, v):
    pat = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+" + re.escape(v) + r"\s+.*?;",
        re.I | re.S)
    m = pat.search(text)
    return m.group(0).strip() if m else None


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp():
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


if __name__ == "__main__":
    main()
