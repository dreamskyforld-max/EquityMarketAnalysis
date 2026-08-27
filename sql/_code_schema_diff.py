#!/usr/bin/env python3
# ============================================================================
# _code_schema_diff.py — 代码侧建表 vs sql/schema.sql 漏写检测（只读，绝不改库）
# ----------------------------------------------------------------------------
# 背景:
#   sql/schema.sql 是部署 / 更新服务器时用来更新【目标服务器数据库】的"标准"。
#   如果代码里用 CREATE TABLE / CREATE INDEX / CREATE VIEW 建立了对象，但没
#   同步写进 schema.sql，那么:
#     - deploy 到新服务器时，这个表不会被创建 → 服务启动/运行异常；
#     - 更新已有服务器时，目标库按 schema.sql 为准 → 该表被当作"其他项目表"
#       跳过（_schema_diff.py 的 other_tables 逻辑），或干脆不被纳入管理。
#   关键点: 现成的 sql/_schema_diff.py 比对的是"schema.sql ↔ 目标库"，
#   它【不会读代码】，因此【查不出】"代码建了表但 schema.sql 漏写"。
#   本脚本专门补这个盲区: 扫代码里的建表语句，反查 schema.sql 是否已声明。
#
# 职责:
#   1. 扫描 $APP_DIR 下所有 .py（排除本脚本、_schema_diff.py、tests/ 下测试表），
#      提取 CREATE TABLE [IF NOT EXISTS] <name> 的字面量表名。
#   2. 解析 sql/schema.sql，得到"已声明"的表/视图集合。
#   3. 反查: 代码里建了、但 schema.sql 没声明的表 → 漏写（高风险，必须补）。
#   4. 反向: schema.sql 声明了、但代码里没有任何建表语句的表 → 孤儿（提示，
#      可能建在别处 / 已废弃，需人工确认是否死表）。
#   5. 仅打印报告，绝不连接数据库、绝不执行任何 DDL。
#
# 说明:
#   - 动态表名（f-string 拼出来的，如 CREATE TABLE IF NOT EXISTS {CACHE_TABLE}）
#     无法静态提取，会被列在"动态表名（需人工核对）"清单，不计入漏写判定。
#   - tests/ 下的 CREATE TABLE {TEST_TABLE} 是 pytest 临时表，默认排除。
# ============================================================================
import os
import re

# 默认 APP_DIR 取脚本所在目录的上一级（项目根），不依赖外部环境变量，
# 避免在本机/服务器不同调用方式下因未设 APP_DIR 而找不到 schema.sql。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get("APP_DIR", os.path.dirname(_THIS_DIR))
SCHEMA_PATH = os.path.join(APP_DIR, "sql", "schema.sql")
ROOT = APP_DIR

# 排除扫描的目录 / 文件
_EXCLUDE_DIRS = {"tests", ".git", ".venv", "venv", "__pycache__", "node_modules"}
_EXCLUDE_FILES = {"_schema_diff.py", "_code_schema_diff.py"}

# tests 下的测试表名（pytest 临时表，不计入漏写）
_TEST_TABLE_RE = re.compile(r"\{TEST_TABLE\}|test_\w+", re.I)


def iter_py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 原地修改 dirnames 以剪枝
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if fn in _EXCLUDE_FILES:
                continue
            yield os.path.join(dirpath, fn)


# 匹配 CREATE TABLE [IF NOT EXISTS] <name> 或 CREATE TABLE <name> (
# name 支持带引号「"xxx"」「`xxx`」「[xxx]」或裸标识符。
# 注意: 裸标识符分支必须排除 SQL 关键字 IF/NOT/EXISTS，否则
# "CREATE TABLE IF NOT EXISTS x" 会把 "if" 当成表名。
_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?P<q>[\"'`\[])(?P<qn>.+?)(?P=q)|(?P<bn>(?!IF\b|NOT\b|EXISTS\b)[A-Za-z_][\w]*))",
    re.I,
)


def extract_code_tables():
    """返回 (literal_tables, dynamic_tables)
    literal_tables: dict[name_lower] = [(file, lineno), ...]
    dynamic_tables: list[(file, lineno, raw)]
    """
    literal = {}
    dynamic = []
    for path in iter_py_files():
        rel = os.path.relpath(path, ROOT)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = _TABLE_RE.search(line)
            if not m:
                continue
            quoted = m.group("qn")
            bare = m.group("bn")
            name = (quoted or bare or "").strip()
            if not name:
                continue
            # 动态表名: 含 { 或 % 或 .format / f-string 痕迹
            if "{" in name or "}" in name or "%" in name or "$" in name:
                dynamic.append((rel, i, name))
                continue
            # 过滤测试表
            if _TEST_TABLE_RE.search(name):
                continue
            literal.setdefault(name.lower(), []).append((rel, i))
    return literal, dynamic


def parse_schema_declared(text):
    """从 schema.sql 提取已声明的表 / 视图名（lower）。"""
    declared = set()
    for line in text.splitlines():
        line = re.sub(r"--.*$", "", line)  # 去行注释
        # 建表
        m = re.match(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"(?:(?P<q>[\"'`\[])(?P<qn>.+?)(?P=q)|(?P<bn>(?!IF\b|NOT\b|EXISTS\b)[A-Za-z_][\w]*))",
            line, re.I)
        if m:
            nm = (m.group("qn") or m.group("bn") or "").strip().lower()
            if nm:
                declared.add(nm)
            continue
        # 视图
        m = re.match(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(?:(?P<q>[\"'`\[])(?P<qn>.+?)(?P=q)|(?P<bn>(?!IF\b|NOT\b|EXISTS\b)[A-Za-z_][\w]*))", line, re.I)
        if m:
            nm = (m.group("qn") or m.group("bn") or "").strip().lower()
            if nm:
                declared.add(nm)
    return declared


def main():
    print("=" * 70)
    print("  代码侧建表 vs sql/schema.sql 漏写检测 (只读，不连库、不改库)")
    print("=" * 70)
    if not os.path.exists(SCHEMA_PATH):
        sys_exit(f"[x] 找不到 {SCHEMA_PATH}")

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_text = f.read()
    declared = parse_schema_declared(schema_text)

    literal, dynamic = extract_code_tables()

    missing = {t: locs for t, locs in literal.items() if t not in declared}
    orphans = sorted(declared - set(literal.keys()))

    print(f"\n代码扫描: 提取字面量表 {len(literal)} 个，动态表 {len(dynamic)} 个")
    print(f"schema.sql 声明对象: {len(declared)} 个")

    if missing:
        print(f"\n⚠ 漏写（代码建表但 schema.sql 未声明，必须补）: {len(missing)} 个")
        for t in sorted(missing):
            locs = ", ".join(f"{f}:{ln}" for f, ln in missing[t])
            print(f"  - {t}   ← 代码位置: {locs}")
    else:
        print("\n[i] 未发现代码建表但 schema.sql 漏写的情况。")

    if orphans:
        print(f"\n[i] 孤儿表（schema.sql 声明但代码无建表语句，需人工确认是否废弃/建在别处）: "
              f"{len(orphans)} 个")
        for t in orphans:
            print(f"    · {t}")

    if dynamic:
        print(f"\n[i] 动态表名（无法静态提取，需人工核对是否已写入 schema.sql）: {len(dynamic)} 个")
        for f, ln, name in dynamic:
            print(f"    · {f}:{ln}  {name}")

    if not missing and not orphans and not dynamic:
        print("\n[i] 代码与 schema.sql 对齐，无需处理。")
    elif not missing:
        print("\n[i] 无漏写风险。其余提示项请按需人工核对。")


def sys_exit(msg):
    import sys
    sys.exit(msg)


if __name__ == "__main__":
    main()
