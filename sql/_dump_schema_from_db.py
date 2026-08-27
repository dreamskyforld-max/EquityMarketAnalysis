#!/usr/bin/env python3
# ============================================================================
# _dump_schema_from_db.py — 以【本机/目标数据库真实结构】为基准，反向生成
#                            sql/schema.sql 的"真实结构镜像"（只读库，不连写）
# ----------------------------------------------------------------------------
# 背景 / 为什么需要它:
#   sql/schema.sql 是部署 / 更新服务器时更新【目标库】的标准。但它会"落后于"
#   真实库: 多个项目共用一个数据库（如前端 stock-realtime 项目会在本项目的
#   表上加索引），这些改动只落在真实库、没回写 schema.sql。此时:
#     - sql/_schema_diff.py 方向是 "schema.sql → 库"，查不出 schema 落后于库;
#     - sql/_code_schema_diff.py 只扫本项目代码，看不到前端项目的改动。
#   正确做法: 以真实库为真相源，把 schema.sql 同步成真实库的镜像。
#
# 策略（白名单，防越权）:
#   以【现有 schema.sql 已声明的表/视图】为白名单，只 dump 真实库中这些对象
#   的【完整真实结构】（含库里实际存在的列、索引；视图取真实定义）。
#   这样能补上"前端项目在本项目表上加的索引"，同时【不会】把前端项目的私有
#   表误写进本项目 schema.sql（不会越权建表）。
#   边界: 若前端项目新建了一张"本应属于本项目"的全新表，白名单抓不到 ——
#         用 sql/_code_schema_diff.py + 人工确认兜底。
#
# 安全:
#   ★ 只读连接数据库（SELECT / pg_catalog 查询），绝不执行任何 DDL。
#   ★ 报告后交互询问；用户确认后才把差异【逐项定位插入】到 schema.sql 的对应
#     表位置（列进 CREATE TABLE 块内、索引进表定义后、注释进该表 COMMENT 段），
#     不整体覆盖文件，避免破坏其他已有内容。
#
# 依赖: 标准库 + psql 客户端（与 _schema_diff.py 同）。
# ============================================================================
import configparser
import os
import re
import subprocess
import sys

# APP_DIR 默认取"本脚本所在目录的上一级"（即项目根），不依赖外部环境变量，
# 避免在本机/服务器不同调用方式下因未设 APP_DIR 而找不到 schema.sql。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get("APP_DIR", os.path.dirname(_THIS_DIR))
SCHEMA_PATH = os.path.join(APP_DIR, "sql", "schema.sql")


# ── 复用 _schema_diff 的连接与解析逻辑（保持单一真相）─────────────────────────
def load_db_conf():
    cfg_path = os.path.join(APP_DIR, "config.conf")
    if not os.path.exists(cfg_path):
        sys.exit(f"[x] 找不到 {cfg_path}\n    本脚本需要 config.conf 中的 [database] 段来连接真实库。"
                 f"请确认项目根存在 config.conf（由 config.example.conf 复制并填好）。")
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    sec = "database"
    if not cfg.has_section(sec):
        sys.exit(f"[x] {cfg_path} 缺少 [database] 段，无法连接数据库。")
    return {
        "host": cfg.get(sec, "host", fallback="localhost"),
        "port": cfg.get(sec, "port", fallback="5432"),
        "dbname": cfg.get(sec, "dbname", fallback="market_db"),
        "user": cfg.get(sec, "user", fallback="market_user"),
        "password": cfg.get(sec, "password", fallback=""),
    }


def psql_query(db, sql):
    env = dict(os.environ)
    env["PGPASSWORD"] = db["password"]
    cmd = ["psql", "-h", db["host"], "-p", str(db["port"]),
           "-U", db["user"], "-d", db["dbname"], "-tA", "-F|", "-c", sql]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        sys.exit("[x] 本机未找到 psql 客户端")
    if out.returncode != 0:
        sys.exit(f"[x] psql 查询失败:\n{out.stderr.strip()}")
    return out.stdout


def _read_declared(text):
    """从 schema.sql 提取已声明的表/视图名（保持原顺序，lower 集合 + 顺序列表）。"""
    tables, views = [], []
    seen = set()
    for line in text.splitlines():
        line = __import__("re").sub(r"--.*$", "", line)
        m = __import__("re").match(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"(?:(?P<q>[\"'`\[])(?P<qn>.+?)(?P=q)|(?P<bn>(?!IF\b|NOT\b|EXISTS\b)[A-Za-z_][\w]*))",
            line, __import__("re").I)
        if m:
            nm = (m.group("qn") or m.group("bn") or "").strip()
            if nm and nm.lower() not in seen:
                tables.append(nm)
                seen.add(nm.lower())
            continue
        m = __import__("re").match(
            r"CREATE\s+OR\s+REPLACE\s+VIEW\s+"
            r"(?:(?P<q>[\"'`\[])(?P<qn>.+?)(?P=q)|(?P<bn>(?!IF\b|NOT\b|EXISTS\b)[A-Za-z_][\w]*))",
            line, __import__("re").I)
        if m:
            nm = (m.group("qn") or m.group("bn") or "").strip()
            if nm and nm.lower() not in seen:
                views.append(nm)
                seen.add(nm.lower())
    return tables, views


def dump_columns(db, table):
    safe = table.replace("'", "''")
    sql = (
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
        "pg_get_expr(d.adbin, d.adrelid) AS def, a.attnotnull "
        "FROM pg_class c "
        "JOIN pg_attribute a ON a.attrelid = c.oid "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname='public' AND c.relname='{safe}' "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum;"
    )
    out = psql_query(db, sql)
    cols = []
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("|")
        name = parts[0]
        typ = parts[1] if len(parts) > 1 else ""
        default = parts[2] if len(parts) > 2 else ""
        notnull = parts[3] if len(parts) > 3 else "f"
        line = f'    "{name}" {typ}'
        if default:
            line += f" DEFAULT {default}"
        if notnull == "t":
            line += " NOT NULL"
        cols.append(line)
    return cols


def dump_constraints(db, table):
    """主键 / 唯一约束（作为表内约束行输出）。"""
    safe = table.replace("'", "''")
    sql = (
        "SELECT conname, pg_get_constraintdef(oid) "
        "FROM pg_constraint "
        f"WHERE conrelid = (SELECT oid FROM pg_class WHERE relname='{safe}' "
        "AND relnamespace='public'::regnamespace) "
        "AND contype IN ('p','u');"
    )
    out = psql_query(db, sql)
    cons = []
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("|")
        cname, cdef = parts[0], parts[1] if len(parts) > 1 else ""
        cons.append(f'    CONSTRAINT "{cname}" {cdef}')
    return cons


def dump_indexes(db, table):
    """非唯一 / 非约束自动建的物理索引（独立 CREATE INDEX 语句）。"""
    safe = table.replace("'", "''")
    sql = (
        "SELECT indexname, indexdef FROM pg_indexes "
        f"WHERE schemaname='public' AND tablename='{safe}';"
    )
    out = psql_query(db, sql)
    stmts = []
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) < 2:
            continue
        iname, idef = parts[0], parts[1]
        # 跳过主键/唯一约束自动生成的索引（已在约束里表达），避免重复
        if iname.endswith("_pkey") or iname.endswith("_key"):
            if "UNIQUE" in idef.upper():
                continue
        stmts.append(f"{idef.replace('CREATE INDEX', 'CREATE INDEX IF NOT EXISTS')};")
    return stmts


def dump_comments(db, table):
    """读取真实库中表/列的 COMMENT，生成 COMMENT ON 语句（保留文档）。"""
    safe = table.replace("'", "''")
    sql = (
        "SELECT 'TABLE' AS obj, '' AS col, d.description FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_description d ON d.objoid=c.oid AND d.objsubid=0 "
        "WHERE n.nspname='public' AND c.relname='%s' AND d.description IS NOT NULL "
        "UNION ALL "
        "SELECT 'COLUMN', a.attname, d.description FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid "
        "JOIN pg_description d ON d.objoid=c.oid AND d.objsubid=a.attnum "
        "WHERE n.nspname='public' AND c.relname='%s' "
        "AND a.attnum>0 AND NOT a.attisdropped;"
    ) % (safe, safe)
    out = psql_query(db, sql)
    stmts = []
    for row in out.splitlines():
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) < 3:
            continue
        kind, col, desc = parts[0], parts[1], parts[2]
        esc = desc.replace(chr(39), chr(39) * 2)
        if kind == "TABLE":
            stmts.append((("TABLE", table.lower(), ""),
                          f"COMMENT ON TABLE \"{table}\" IS '{esc}';"))
        else:
            stmts.append((("COLUMN", table.lower(), col.lower()),
                          f"COMMENT ON COLUMN \"{table}\".\"{col}\" IS '{esc}';"))
    return stmts


def dump_view(db, view):
    sql = ("SELECT pg_get_viewdef(to_regclass(%s::text), true);"
           % f"'public.{view}'")
    out = psql_query(db, sql).strip()
    if not out:
        return None
    return f"CREATE OR REPLACE VIEW \"{view}\" AS\n{out.rstrip(';')};"


def parse_schema_comments(text):
    """从原 schema.sql 提取 COMMENT 语句: {(kind,obj,col?): stmt}。
    用于与真实库 COMMENT 取并集（库优先），避免丢失手写业务注释。"""
    import re as _re
    res = {}
    for m in _re.finditer(
        r"COMMENT\s+ON\s+(TABLE|COLUMN|VIEW)\s+"
        r"(?P<obj>[\"'`]?[\w.]+[\"'`]?)(?:\.(?P<col>[\"'`]?[\w]+[\"'`]?))?\s+IS\s+"
        r"(?P<val>'.*?');",
        text, _re.I | _re.S):
        kind = m.group(1).upper()
        obj = m.group("obj").strip('"\'`').lower()
        col = m.group("col").strip('"\'`').lower() if m.group("col") else ""
        key = (kind, obj, col)
        res[key] = m.group(0)
    return res


def parse_schema_indexes(text):
    """从现有 schema.sql 提取索引定义: {table_lower: [stmt, ...]}。
    用于与真实库索引取并集，避免丢失『schema 声明但库里尚未建』的索引
    （这些索引在部署到新服务器时应被创建）。"""
    import re as _re
    res = {}
    for m in _re.finditer(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?P<iname>[A-Za-z_][\w]*)\s+ON\s+(?:public\.)?"
        r"(?P<q>[\"'`\[]?)(?P<tbl>[A-Za-z_][\w]*)(?P=q)?",
        text, _re.I):
        tbl = m.group("tbl").lower()
        # 重建成 IF NOT EXISTS 形式，与库里 dump 出的风格一致
        stmt = _re.sub(r"(?i)CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?",
                       lambda mm: "CREATE " + (mm.group(1) or "") + "INDEX IF NOT EXISTS ",
                       m.group(0).rstrip(";")) + ";"
        res.setdefault(tbl, []).append((m.group("iname").lower(), stmt))
    return res


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="原地覆盖 schema.sql（默认只生成 .generated 供 review）")
    args = ap.parse_args()

    if not os.path.exists(SCHEMA_PATH):
        sys.exit(f"[x] 找不到 {SCHEMA_PATH}")

    db = load_db_conf()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_text = f.read()
    tables, views = _read_declared(schema_text)
    schema_idx = parse_schema_indexes(schema_text)   # {tbl: [(idxname, stmt)]}
    schema_cmt = parse_schema_comments(schema_text)  # {(kind,obj,col): stmt}

    print(f"白名单: 表 {len(tables)} 个, 视图 {len(views)} 个")
    print("正在从真实库读取这些对象的真实结构（只读）...")

    out_parts = []
    out_parts.append("-- ===========================================================")
    out_parts.append("-- schema.sql — 由 _dump_schema_from_db.py 从【真实数据库】反向生成")
    out_parts.append("-- 生成时间: " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out_parts.append("--")
    out_parts.append("-- ⚠ 此文件是数据库结构的唯一真相源 + 部署/更新服务器时更新目标库的标准。")
    out_parts.append("-- ⚠ 改动任何表/列/索引/视图，必须同步更新本文件。")
    out_parts.append("-- ⚠ 本文件由脚本基于真实库结构生成；覆盖前请 diff 确认无误。")
    out_parts.append("-- ===========================================================")
    out_parts.append("")

    # 注释合并: 原 schema.sql 全部 COMMENT 为基底，真实库 dump 的 COMMENT 优先覆盖
    cmt_final = dict(schema_cmt)

    for t in tables:
        cols = dump_columns(db, t)
        cons = dump_constraints(db, t)
        if not cols:
            print(f"  [!] 表 {t} 在真实库中不存在或无列，跳过")
            continue
        body = ",\n".join(cols + cons)
        out_parts.append(f'CREATE TABLE IF NOT EXISTS "{t}" (')
        out_parts.append(body)
        out_parts.append(");")
        # 索引: 真实库索引 ∪ 原 schema.sql 声明的索引（去重 by 索引名），
        # 保证不丢失『schema 规划但库里尚未建』的索引（部署新服务器时需创建）。
        db_idx_stmts = dump_indexes(db, t)
        merged = {}  # idxname_lower -> stmt
        for st in db_idx_stmts:
            nm = re.search(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][\w]*)",
                           st, re.I)
            key = nm.group(1).lower() if nm else st
            merged[key] = st
        for iname, stmt in schema_idx.get(t.lower(), []):
            merged.setdefault(iname, stmt)
        for st in merged.values():
            out_parts.append(st)
        # 注释: 库 dump 覆盖到 cmt_final（库优先）
        for key, stmt in dump_comments(db, t):
            cmt_final[key] = stmt
        out_parts.append("")

    for v in views:
        ddl = dump_view(db, v)
        if ddl:
            out_parts.append(ddl)
            # 视图注释也合并
            for key, stmt in schema_cmt.items():
                if key[1] == v.lower():
                    cmt_final.setdefault(key, stmt)
            out_parts.append("")
        else:
            print(f"  [!] 视图 {v} 在真实库中未找到定义，跳过")

    # 统一追加所有 COMMENT（库优先 + 原 schema 手写注释兜底）
    for stmt in cmt_final.values():
        out_parts.append(stmt)

    generated = "\n".join(out_parts).rstrip() + "\n"

    # ── 打印差异报告（只列有差异的项）──────────────────────────────────────
    _print_diff_report(schema_text, generated, tables, views)

    # ── 计算结构化差异，供交互修复使用 ─────────────────────────────────────
    diff = _compute_diff(schema_text, generated, tables, views)

    if not diff["index"] and not diff["columns"] and not diff["comments"] and not diff["view_missing"]:
        print("\n✓ 无差异，无需修复。")
        return

    # ── 交互询问是否修复 ─────────────────────────────────────────────────
    if args.apply:
        print("\n[--apply] 直接覆盖模式已弃用；请改用默认模式（逐项插入）。")
        print("    本次不执行任何写入。")
        return

    try:
        ans = input("\n是否将以上差异逐项插入 schema.sql？(y/N) ").strip().lower()
    except EOFError:
        print("\n[!] 非交互环境，未确认，不执行写入。")
        return
    if ans not in ("y", "yes"):
        print("已取消，未改动 schema.sql。")
        return

    # ── 逐项定位插入到 schema.sql 对应表位置（不整体覆盖）────────────────
    applied = _apply_insertions(diff, db, schema_text)
    if applied:
        with open(SCHEMA_PATH, "w", encoding="utf-8") as f:
            f.write(applied)
        print(f"\n[+] 已将 {_count_items(diff)} 项差异插入: {SCHEMA_PATH}")
        print("    建议随后运行: python3 sql/_schema_diff.py 或 git diff 复核。")
    else:
        print("\n[!] 无项可插入。")


def _index_set(text):
    """返回 {table_lower: set(index_name_lower)} 与 {table_lower: [stmt,...]}。
    注意: 表内 CONSTRAINT ... UNIQUE (cols) 与独立 CREATE UNIQUE INDEX 语义等价，
          都归一化为索引名，避免「约束形式 vs 索引形式」的虚假差异。"""
    import re as _re
    names, stmts = {}, {}
    # 1) 独立 CREATE [UNIQUE] INDEX
    for m in _re.finditer(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?P<in>[A-Za-z_][\w]*)\s+ON\s+(?:public\.)?"
        r"(?P<q>[\"'`\[]?)(?P<tbl>[A-Za-z_][\w]*)(?P=q)?",
        text, _re.I):
        tbl = m.group("tbl").lower()
        names.setdefault(tbl, set()).add(m.group("in").lower())
        stmts.setdefault(tbl, []).append(m.group(0).strip())
    # 2) 表内 CONSTRAINT <name> UNIQUE (cols) —— 等价唯一索引
    for m in _re.finditer(
        r'CONSTRAINT\s+(?P<cn>[A-Za-z_][\w]*)\s+UNIQUE\s*\(', text, _re.I):
        cn = m.group("cn").lower()
        # 该约束属于哪个表: 向前找最近的 CREATE TABLE ... (
        pos = m.start()
        tbl_m = None
        for tm in _re.finditer(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            r'(?:"?)([A-Za-z_][\w]*)(?:"?)\s*\(', text, _re.I):
            if tm.start() < pos:
                tbl_m = tm.group(1).lower()
        if tbl_m:
            names.setdefault(tbl_m, set()).add(cn)
    return names, stmts


def _table_cols(text):
    """返回 {table_lower: {col_lower: definition_line}}。
    用括号深度配对，确保每张表只解析自己 (...) 内的列定义。"""
    import re as _re
    res = {}
    for m in _re.finditer(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:"?)(?P<tbl>[A-Za-z_][\w]*)(?:"?)\s*\(', text, _re.I):
        tbl = m.group("tbl").lower()
        start = m.end()  # '(' 之后
        depth = 1
        i = start
        n = len(text)
        while i < n and depth > 0:
            ch = text[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            i += 1
        body = text[start:i - 1]  # 去掉最后的 ')'
        cols = {}
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            up = line.upper()
            if up.startswith(("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "FOREIGN KEY",
                              "CHECK", ")")):
                continue
            cm = _re.match(r'(?:"?)([A-Za-z_][\w]*)(?:"?)\s+(.*)', line)
            if cm:
                cols[cm.group(1).lower()] = line
        res[tbl] = cols
    return res


def _comment_set(text):
    """返回 {(kind,obj,col): comment_text}。"""
    import re as _re
    res = {}
    for m in _re.finditer(
        r"COMMENT\s+ON\s+(TABLE|COLUMN|VIEW)\s+"
        r"(?P<obj>[\"'`]?[\w]+[\"'`]?)(?:\.(?P<col>[\"'`]?[\w]+[\"'`]?))?\s+IS\s+"
        r"(?P<val>'.*?');", text, _re.I | _re.S):
        kind = m.group(1).upper()
        obj = m.group("obj").strip('"\'`').lower()
        col = m.group("col").strip('"\'`').lower() if m.group("col") else ""
        # 兼容 "schema"."table" 或被吃进 obj 的退化情况：若 obj 仍含点，拆出最后一段当 col
        if "." in obj and not col:
            obj, col = obj.rsplit(".", 1)
        res[(kind, obj, col)] = m.group("val")
    return res


def _print_diff_report(orig_text, gen_text, tables, views):
    """打印 schema.sql（原） vs 真实库镜像（生成）的差异。
    原则: 只输出【有差异】的项；无差异的段落不列出。"""
    o_idx, _ = _index_set(orig_text)
    g_idx, _ = _index_set(gen_text)
    o_cols = _table_cols(orig_text)
    g_cols = _table_cols(gen_text)
    o_cmt = _comment_set(orig_text)
    g_cmt = _comment_set(gen_text)

    diff_lines = []  # 累积差异行，最后判断是否全空

    # 1) 索引
    sec = []
    for t in sorted(set(o_idx) | set(g_idx)):
        only_gen = g_idx.get(t, set()) - o_idx.get(t, set())
        only_orig = o_idx.get(t, set()) - g_idx.get(t, set())
        if only_gen:
            sec.append(f"  [补索引] {t}: " + ", ".join(sorted(only_gen)))
        if only_orig:
            sec.append(f"  [保留]   {t}: " + ", ".join(sorted(only_orig))
                       + "  (schema声明/库尚未建，部署时会创建)")
    if sec:
        diff_lines.append("\n■ 索引")
        diff_lines.extend(sec)

    # 2) 列
    sec = []
    for t in sorted(set(o_cols) | set(g_cols)):
        if t in o_cols and t not in g_cols:
            sec.append(f"  [!表仅原schema有] {t} (数据库无此表?)")
            continue
        if t not in o_cols and t in g_cols:
            sec.append(f"  [表仅库有] {t} (白名单外/未纳入)")
            continue
        oc, gc = o_cols[t], g_cols[t]
        added = [c for c in gc if c not in oc]
        dropped = [c for c in oc if c not in gc]
        if added:
            sec.append(f"  [+列] {t}: " + ", ".join(added))
        if dropped:
            sec.append(f"  [-列] {t}: " + ", ".join(dropped))
    if sec:
        diff_lines.append("\n■ 表 / 列")
        diff_lines.extend(sec)

    # 3) 视图: 只报真实库找不到定义的异常视图
    g_view_defs = dict(re.findall(
        r'CREATE\s+OR\s+REPLACE\s+VIEW\s+"?([A-Za-z_][\w]*)"?\s+AS\s+(.*?);',
        gen_text, re.I | re.S))
    sec = []
    for v in views:
        if v.lower() not in g_view_defs:
            sec.append(f"  [!视图缺失] {v} 在真实库中未找到定义")
    if sec:
        diff_lines.append("\n■ 视图")
        diff_lines.extend(sec)

    # 4) 注释
    sec = []
    for key in sorted(set(g_cmt) | set(o_cmt)):
        if key in g_cmt and key not in o_cmt:
            sec.append(f"  [+注释] {' '.join(k for k in key if k)}: {g_cmt[key]}")
        elif key in o_cmt and key not in g_cmt:
            sec.append(f"  [保留注释] {' '.join(k for k in key if k)}: {o_cmt[key]}")
    if sec:
        diff_lines.append("\n■ 注释（COMMENT）")
        for s in sec[:40]:
            diff_lines.append(s)
        if len(sec) > 40:
            diff_lines.append(f"  ... 共 {len(sec)} 条注释差异，详见 schema.sql.generated")

    # 输出
    print("\n" + "=" * 64)
    print("  差异报告: 原 schema.sql  ↔  真实库镜像")
    if not diff_lines:
        print("  ✓ 无差异（schema.sql 与真实库结构一致）")
    else:
        print("  （仅列出存在差异的项）")
        print("=" * 64)
        for ln in diff_lines:
            print(ln)
    print("=" * 64)


# ── 差异计算 / 交互修复 ─────────────────────────────────────────────────────
def _compute_diff(orig_text, gen_text, tables, views):
    """返回结构化差异，供报告与交互插入共用。"""
    o_idx, _ = _index_set(orig_text)
    g_idx, _ = _index_set(gen_text)
    o_cols = _table_cols(orig_text)
    g_cols = _table_cols(gen_text)
    o_cmt = _comment_set(orig_text)
    g_cmt = _comment_set(gen_text)
    g_view_defs = dict(re.findall(
        r'CREATE\s+OR\s+REPLACE\s+VIEW\s+"?([A-Za-z_][\w]*)"?\s+AS\s+(.*?);',
        gen_text, re.I | re.S))

    diff = {"index": [], "columns": [], "comments": [], "view_missing": []}
    for t in sorted(set(o_idx) | set(g_idx)):
        for name in sorted(g_idx.get(t, set()) - o_idx.get(t, set())):
            diff["index"].append((t, name))
    for t in sorted(set(o_cols) | set(g_cols)):
        if t in o_cols and t not in g_cols:
            continue
        if t not in o_cols and t in g_cols:
            continue
        added = [c for c in g_cols[t] if c not in o_cols[t]]
        if added:
            diff["columns"].append((t, added))
    for key in sorted(set(g_cmt) | set(o_cmt)):
        if key in g_cmt and key not in o_cmt:
            diff["comments"].append((key, g_cmt[key]))
    for v in views:
        if v.lower() not in g_view_defs:
            diff["view_missing"].append(v)
    return diff


def _count_items(diff):
    return (len(diff["index"]) + len(diff["columns"])
            + len(diff["comments"]) + len(diff["view_missing"]))


def read_column_ddl(db, table, col):
    """从真实库读列的完整定义行（不含前导空格，调用方负责缩进）。"""
    safe_t = table.replace("'", "''")
    safe_c = col.replace("'", "''")
    sql = (
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
        "pg_get_expr(d.adbin, d.adrelid) AS def, a.attnotnull "
        "FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid "
        "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE n.nspname='public' AND c.relname='{safe_t}' AND a.attname='{safe_c}' "
        "AND a.attnum>0 AND NOT a.attisdropped;"
    )
    out = psql_query(db, sql).strip()
    if not out:
        return None
    parts = out.split("|")
    name, typ = parts[0], parts[1] if len(parts) > 1 else ""
    default = parts[2] if len(parts) > 2 else ""
    notnull = parts[3] if len(parts) > 3 else "f"
    line = f'{name} {typ}'  # 不加引号，与本项目 schema.sql 列定义风格一致
    if default:
        line += f" DEFAULT {default}"
    if notnull == "t":
        line += " NOT NULL"
    return line


def read_index_ddl(db, table, idxname):
    """从真实库读独立索引的完整 DDL（统一为 IF NOT EXISTS 形式）。"""
    safe_t = table.replace("'", "''")
    sql = (
        "SELECT indexdef FROM pg_indexes "
        f"WHERE schemaname='public' AND tablename='{safe_t}' AND indexname='{idxname}';"
    )
    out = psql_query(db, sql).strip()
    if not out:
        return None
    return out.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS").rstrip(";") + ";"


def read_comment_ddl(db, table, key):
    """从真实库读 COMMENT 语句。key=(kind,obj,col)。"""
    kind, obj, col = key
    safe_t = table.replace("'", "''")
    if kind == "TABLE":
        sql = (
            "SELECT d.description FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "LEFT JOIN pg_description d ON d.objoid=c.oid AND d.objsubid=0 "
            f"WHERE n.nspname='public' AND c.relname='{safe_t}' AND d.description IS NOT NULL;"
        )
    else:
        safe_c = col.replace("'", "''")
        sql = (
            "SELECT d.description FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid "
            "JOIN pg_description d ON d.objoid=c.oid AND d.objsubid=a.attnum "
            f"WHERE n.nspname='public' AND c.relname='{safe_t}' AND a.attname='{safe_c}';"
        )
    out = psql_query(db, sql).strip()
    if not out:
        return None
    esc = out.replace("'", "''")
    if kind == "TABLE":
        return f'COMMENT ON TABLE "{table}" IS \'{esc}\';'
    return f'COMMENT ON COLUMN "{table}"."{col}" IS \'{esc}\';'


def _apply_insertions(diff, db, schema_text):
    """逐项把差异插入到 schema.sql 对应表的指定位置，返回修改后的全文。
    不整体覆盖；每个差异定位到具体表段插入。"""
    text = schema_text
    applied = 0

    # 1) 补列：插进 CREATE TABLE (...) 括号内部，最后一个真实列之后、约束行之前
    for tbl, cols in diff["columns"]:
        # 找到该表 CREATE TABLE 块
        m = re.search(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            rf'(?:"?){re.escape(tbl)}(?:"?)\s*\(', text, re.I)
        if not m:
            print(f"  [!] 找不到表 {tbl} 定义，跳过补列")
            continue
        start = m.end()
        depth, i, n = 1, start, len(text)
        while i < n and depth > 0:
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
            i += 1
        block = text[start:i - 1]
        lines = block.split("\n")
        # 找最后一个"真实列"行（非约束、非空、非 ) ）
        last_col_idx = -1
        for li in range(len(lines) - 1, -1, -1):
            s = lines[li].strip()
            if not s or s.startswith(")") or s.upper().startswith(
                    ("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")):
                continue
            last_col_idx = li
            break
        if last_col_idx < 0:
            print(f"  [!] 表 {tbl} 无列行，跳过补列")
            continue
        # 构造新列行（4 空格缩进 + 末尾逗号），列名按该表已有列最大宽度对齐
        indent = "    "
        # 已有列名最大宽度（去掉约束行），用于对齐新列
        exist_cols = [ln.strip().split()[0].strip('"\'`')
                      for ln in lines
                      if ln.strip() and not ln.strip().upper().startswith(
                          ("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK", ")"))]
        max_w = max((len(c) for c in exist_cols), default=8)
        new_lines = []
        for c in cols:
            ddl = read_column_ddl(db, tbl, c)
            if not ddl:
                print(f"  [!] 读不到 {tbl}.{c} 定义，跳过")
                continue
            # ddl 形如 "colname TYPE ..."，把列名对齐到 max_w
            parts = ddl.split(" ", 1)
            name_aligned = parts[0].ljust(max_w)
            rest = parts[1] if len(parts) > 1 else ""
            new_lines.append(f"{indent}{name_aligned} {rest},")
        if not new_lines:
            print(f"  [!] 表 {tbl} 无可插入列，跳过")
            continue
        # 给上一行补逗号
        prev = lines[last_col_idx].rstrip()
        if not prev.endswith(","):
            lines[last_col_idx] = prev + ","
        # 去掉 lines 末尾空行，避免插入后产生多余空行
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines[last_col_idx + 1:last_col_idx + 1] = new_lines
        new_block = "\n".join(lines)
        text = text[:start] + new_block + text[i - 1:]
        applied += len(new_lines)
        print(f"  [+] 已插入列 {tbl}: {', '.join(cols)}")

    # 2) 补索引：插在该表 ); 之后、该表 COMMENT 之前
    for tbl, idxname in diff["index"]:
        ddl = read_index_ddl(db, tbl, idxname)
        if not ddl:
            print(f"  [!] 读不到索引 {tbl}.{idxname}，跳过")
            continue
        # 定位该表 ); 行
        m = re.search(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            rf'(?:"?){re.escape(tbl)}(?:"?)\s*\(.*?\);', text, re.I | re.S)
        if not m:
            print(f"  [!] 找不到表 {tbl} 的 );，跳过索引")
            continue
        insert_at = m.end()
        # 插到下一行（确保前后换行）
        text = text[:insert_at] + "\n" + ddl + text[insert_at:]
        applied += 1
        print(f"  [+] 已插入索引 {tbl}.{idxname}")

    # 3) 补注释：插在该表已有 COMMENT 段末尾
    for key, _stmt in diff["comments"]:
        kind, obj, col = key
        # obj 可能是 table 或 table.column（归一后应为 table/col）
        tbl = obj
        ddl = read_comment_ddl(db, tbl, key)
        if not ddl:
            print(f"  [!] 读不到注释 {key}，跳过")
            continue
        # 找该表所有 COMMENT ON 行，插到最后一条之后；若无则插在表 ); 之后
        cmt_m = list(re.finditer(
            r'COMMENT\s+ON\s+(TABLE|COLUMN|VIEW)\s+'
            rf'(?:"?){re.escape(tbl)}(?:"?)(\."{re.escape(col)}")?\b', text, re.I))
        if cmt_m:
            insert_at = cmt_m[-1].end()
            text = text[:insert_at] + "\n" + ddl + text[insert_at:]
        else:
            m = re.search(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                rf'(?:"?){re.escape(tbl)}(?:"?)\s*\(.*?\);', text, re.I | re.S)
            if not m:
                print(f"  [!] 找不到表 {tbl}，跳过注释")
                continue
            insert_at = m.end()
            text = text[:insert_at] + "\n" + ddl + text[insert_at:]
        applied += 1
        print(f"  [+] 已插入注释 {kind} {obj}"
              + (f".{col}" if col else ""))

    return text if applied else None


if __name__ == "__main__":
    main()
