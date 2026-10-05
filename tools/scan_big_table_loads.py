#!/usr/bin/env python3
"""大表全量加载自查工具（配合 doc/compute_task_resource_standard.md §2 的 R1/R7）。

用途：新增 / 修改"读库 → 计算 → 写库"逻辑后跑一遍，人工确认大表查询是否都带了
时间下界（BETWEEN / >=）或实体过滤（stock_code = ANY(...)）。没有下界、规模又大的，
必须按规范 §4.1 / §4.3 改成分批（server-side cursor 或按实体分批）——
本项目 09-29 / 09-30 两次整机内存-IO 雪崩（各 5.5h / 8.4h）与 10-05 拦截的 888MB
峰值，都是"无下界大表查询 + 一次性加载"造成的。

判定方式（2026-10-05 改进：按 SQL 字面量块而非行窗口，减少误报）：
  1. 抽出源码里的字符串字面量（三引号 / 单行引号），把相距 ≤2 行的相邻字面量
     合并成块（项目里常见 `"SELECT ... " "WHERE ..."` 的拼接写法）；
  2. 块内含大表名（\\b 词边界，避免误伤备份表）→ 命中；
  3. 命中块内找不到下界 / 聚合 / DISTINCT / LIMIT 等安全信号 → 标"疑似无下界"。

它只做**提示**，不做判定、不阻塞发布。用法:
    python3 tools/scan_big_table_loads.py              # 扫全项目
    python3 tools/scan_big_table_loads.py 某文件.py     # 只扫指定文件
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 大表清单（量级见规范 §1；新增"准大表"时同步更新这里与文档 R2）
BIG_TABLES = [
    "a_daily_quote",
    "hk_daily_quote",
    "tick_data",
    "daily_quote",
    "daily_benchmark",
]

# 扫描时排除的目录（.venv/__pycache__/doc 等没有 SQL；tests 里的查询是构造数据的）
SKIP_DIRS = {".venv", "__pycache__", ".git", "doc", "tests", ".pytest_cache",
             ".workbuddy", "node_modules", "log"}

# 安全信号：块内出现任意一个即认为"可能已带下界/聚合/去重/限额"（宁可少报，避免噪音）
SAFE_PATTERN = re.compile(
    r"BETWEEN|>=\s*%s|<=\s*%s|ANY\s*\(|DISTINCT|LIMIT\s+\d|GROUP\s+BY|ORDER\s+BY"
    r"|count\s*\(|sum\s*\(|avg\s*\(|max\s*\(|min\s*\(|= \s*%s|\bIN\s*\(",
    re.I,
)

TABLE_PATTERN = re.compile(
    r"\bFROM\s+(?:" + "|".join(re.escape(t) for t in BIG_TABLES) + r")\b",
    re.I,
)

# 字符串字面量：三引号（跨行）+ 单行引号；用 groups 取非 None 的那组
STRING_RE = re.compile(
    r'"""(?P<tri_d>.*?)"""'
    r"|'''(?P<tri_s>.*?)'''"
    r'|"(?P<d>[^"\n]*)"'
    r"|'(?P<s>[^'\n]*)'",
    re.S,
)


def _line_of(src, pos):
    return src.count("\n", 0, pos) + 1


def _literal_blocks(src):
    """产出 (start_pos, content)：相邻（≤2 行）字面量合并为一个块。"""
    items = []
    for m in STRING_RE.finditer(src):
        content = next((g for g in m.groups() if g is not None), "")
        items.append((m.start(), m.end(), content))
    blocks, cur = [], None
    for s, e, c in items:
        if cur is not None and _line_of(src, s) - cur["last_line"] <= 2:
            cur["content"].append(c)
            cur["last_line"] = _line_of(src, e)
            continue
        if cur is not None:
            blocks.append((cur["start"], "\n".join(cur["content"])))
        cur = {"start": s, "last_line": _line_of(src, e), "content": [c]}
    if cur is not None:
        blocks.append((cur["start"], "\n".join(cur["content"])))
    return blocks


def scan_file(path):
    """返回 [(行号, 表名, 是否疑似无下界, 片段摘要), ...]"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            src = f.read()
    except OSError:
        return []
    hits = []
    for start, content in _literal_blocks(src):
        m = TABLE_PATTERN.search(content)
        if not m:
            continue
        table = m.group(0).split(None, 1)[-1]
        safe = bool(SAFE_PATTERN.search(content))
        snippet = " ".join(content.split())[:160]
        hits.append((_line_of(src, start), table, not safe, snippet))
    return hits


def iter_py_files(targets):
    if targets:
        for t in targets:
            p = t if os.path.isabs(t) else os.path.join(ROOT, t)
            if os.path.isfile(p) and p.endswith(".py"):
                yield p
        return
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def main(argv):
    targets = [a for a in argv if not a.startswith("-")]
    files = sorted(set(iter_py_files(targets)))
    suspicious, total_hits = [], 0
    for path in files:
        for lineno, table, suspect, snippet in scan_file(path):
            total_hits += 1
            if suspect:
                suspicious.append((os.path.relpath(path, ROOT), lineno, table, snippet))

    print("=" * 72)
    print("  大表全量加载自查（提示性，不阻塞发布）")
    print("=" * 72)
    print(f"  扫描文件: {len(files)}   大表命中: {total_hits}   疑似无下界: {len(suspicious)}")
    if not suspicious:
        print("\n  ✅ 未发现「疑似无下界」的大表查询。")
        return 0
    print("\n  ⚠ 以下位置的大表查询未在同一 SQL 块内发现下界/聚合/去重/限额，请人工确认：")
    print("     （确认要点：是否带 BETWEEN / >= / stock_code = ANY(...)；"
          "结果集是否可能 >50 万行；超限必须分批）\n")
    for rel, lineno, table, snippet in suspicious:
        print(f"  · {rel}:{lineno}  [{table}]")
        print(f"      {snippet}")
    print("\n  规范细则: doc/compute_task_resource_standard.md §2 R1/R7、§4.1/§4.3；")
    print("  实测峰值: /usr/bin/time -v <命令>  （看 Maximum resident set size）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
