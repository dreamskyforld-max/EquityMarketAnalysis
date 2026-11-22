#!/usr/bin/env python3
"""HSICS 行业分类层级 + 富途码映射采集 —— 写入两张表（单一职责各一张）。

数据源：恒生官方 B_HSICS.pdf（HKEX/HSI 官网，含 12 industry / 31 sector / 114 subsector）。
  PDF URL 固定为实测可用的官方地址；如需更换直接改本脚本 PDF_URL 常量。

两张表（各司其职，见 AGENTS.md 单一职责）：
  1) regime.hsics_industry_classification —— 纯 HSICS 官方分类层级（6位 subsector + 父级）；
  2) regime.futu_hsics_code_mapping       —— 富途 HK.LISTxxxx → HSICS 官方码映射（D4 两跳中间层）。

映射方法：
  · 富途英文名 ↔ HSICS subsector 英文名规范化后精确匹配 → auto；
  · 匹配不上的（同义词差异如 Aluminum↔Aluminium）→ 留 hsics_code=NULL, match_method=manual,
    confirmed=FALSE，等人工核对（不自动猜测，避免错误映射污染画像）；
  · confirmed=TRUE 的行（人工已核对）在重算时保留，不被覆盖。

落库：UNIQUE(subsector_code) / UNIQUE(futu_code) 幂等 upsert；每日 08:15 调度。
实测耗时：PDF 下载+解析+匹配 ≈0.9s。

用法：
    python3 get_hsics_industry_classification.py            # 采集写库
    python3 get_hsics_industry_classification.py --dry-run  # 只打印（含未匹配清单）
"""
import argparse
import datetime
import io
import re
import sys
import urllib.request

from db import bulk_upsert, get_conn

PDF_URL = "https://www.hsi.com.hk/content/dam/wpb/hsil/index_review/en_hk/B_HSICS.pdf"
T_CLASS = "regime.hsics_industry_classification"
T_MAP = "regime.futu_hsics_code_mapping"


# 代码侧修复（2026-11-20 用户复核发现）：原解析器只认独立纯数字行，漏掉合并行
# 「Commercial & Professional Services 103010」「Telecommunications 3500」，并把
# 350010/350020 错挂到医疗父级；名称还会混入描述文字或被换行截断。
_DESC_START = re.compile(
    r"^(Companies|Company|Providers?|Producers?|Manufacturers?|Owners?|Operators?|"
    r"Establishments?|Engaged|Involved|Includes?|Holders|Investment companies|Companies that)\b")
# 描述句在名称尾部接续的分隔词（如 "Iron & Steel Producers, processors and traders ..."）
_DESC_TAIL = re.compile(
    r"\s+(?:Producers?|Manufacturers?|Providers?|Distributors?|Retailers?|Owners?|"
    r"Operators?|Companies|Company|Establishments?|Engaged|Involved|Includes?)\b[, ]")
# 名称里混入的描述句起点（出现在名称中间，如 "Supermarkets ... Stores Owners and operators of..."）
_DESC_INLINE = re.compile(r"\s+(?:Owners and operators|Companies providing|Providers of|Companies that|Establishments)\b")


def _name_from_line(line: str) -> str:
    """从一行文本提取纯名称：若形如「名称 + 码」合并行则只取名称前缀。"""
    m = re.match(r"^(.*?\S)\s+(\d{2}|\d{4}|\d{6})$", line)
    return (m.group(1) if m else line).strip()


def _parse_pdf() -> list:
    """解析官方 PDF，返回 [(industry_code, industry_name, sector_code, sector_name,
    subsector_code, sub_name), ...]（6 元组，含父级名称）。

    支持三种行形态：
      a) 纯码行 `703020` + 下一行名称（可能跨行拼接）；
      b) 合并行 `Commercial & Professional Services 103010`（名称+码同行）→ 拆出码；
      c) 名称被换行截断（如 `Semiconductor Equipment &` + `Materials`）→ 拼接。
    父级归属在归一化后的 (code, name) 序列上回溯，避免错挂；
    sector_name / industry_name 从 entries 的 code→name 字典查得。
    """
    import pymupdf
    raw = urllib.request.urlopen(PDF_URL, timeout=30).read()
    doc = pymupdf.open(stream=raw, filetype="pdf")
    lines = [l.strip() for l in text_lines(doc) if l.strip()]

    # ① 归一化：每行 → (code, name_hint)；先切「名称 码」合并行，再收纯码行
    # ⚠ 合并行有两种语义，必须区分（2026-11-20 复核修正）：
    #   · 2/4 位码合并行（如 "Telecommunications 3500"）：行首前缀 = 该级名称；
    #   · 6 位码合并行（如 "Commercial & Professional Services 103010"）：行首前缀是
    #     **二级 sector 名**，真正的 subsector 名在下一行（"Sourcing & Supply Chain
    #     Service"）。旧实现误把行首当前子行业名 → 103010 写成 sector 名，故此处取下一行。
    entries = []
    for i, line in enumerate(lines):
        m = re.match(r"^(.*?\S)\s+(\d{2}|\d{4}|\d{6})$", line)
        if m and not line.isdigit():
            code, prefix = m.group(2), m.group(1)
            if len(code) == 6:
                # 6 位合并行：名称取下一行（并截掉描述句），行首前缀仅作 sector hint 不入名称
                name = lines[i + 1] if i + 1 < len(lines) else prefix
                if name.endswith("&") and i + 2 < len(lines):
                    name = (name + " " + _name_from_line(lines[i + 2])).strip()
                name = _DESC_TAIL.split(name)[0].strip()
                name = _DESC_INLINE.split(name)[0].strip()
                entries.append((i, code, name or prefix))
            else:
                entries.append((i, code, prefix))               # 2/4 位合并行：行首即本级名
        elif re.fullmatch(r"\d{2}|\d{4}|\d{6}", line):
            if len(line) == 6:
                # 6 位 subsector：名称取紧随的一行（其后即描述句）；
                # 仅当以 & 结尾（跨行截断，如 Semiconductor Equipment & / Materials）才拼下一行。
                raw = lines[i + 1] if i + 1 < len(lines) else ""
                name = _name_from_line(raw)
                if name.endswith("&") and i + 2 < len(lines):
                    name = (name + " " + _name_from_line(lines[i + 2])).strip()
                name = _DESC_TAIL.split(name)[0].strip()
            else:
                # 2/4 位 industry/sector：名称可能跨行（Information + Technology），
                # 或下一行是「名称+码」合并行（Commercial & Professional Services 103010）
                name = ""
                for j in range(i + 1, min(i + 3, len(lines))):
                    nxt = lines[j]
                    if re.fullmatch(r"\d{2}|\d{4}|\d{6}", nxt) or _DESC_START.match(nxt):
                        break
                    name = (name + " " + _name_from_line(nxt)).strip()
                    if re.search(r"\s+\d{2}|\d{4}|\d{6}$", nxt):   # 合并行已取前缀，到此为止
                        break
            name = _DESC_INLINE.split(name)[0].strip()
            entries.append((i, line, name))                    # 纯码行

    # ② 码 → 名称字典（2/4/6 位都有），供 subsector 行回填父级名称
    name_by_code = {}
    for _, code, name in entries:
        if name and code not in name_by_code:
            name_by_code[code] = name

    # ③ 建立 subsector → (sector, industry) 映射，在归一化条目上回溯
    rows = []
    seen_subs = set()
    for idx, code, name in entries:
        if len(code) != 6 or code in seen_subs or not name:
            continue
        # 官方码前 4 位 = sector、前 2 位 = industry（已实测全部满足，无需回溯）
        sector = code[:4]
        industry = code[:2]
        rows.append((industry, name_by_code.get(industry),
                     sector, name_by_code.get(sector),
                     code, name))
        seen_subs.add(code)

    # ④ 硬校验：官方 2024 结构 = 12 industry / 31 sector / 114 subsector
    # rows 为 6 元组 (ind_code, ind_name, sec_code, sec_name, sub_code, sub_name)
    n_ind = len({r[0] for r in rows})
    n_sec = len({r[2] for r in rows})
    if n_ind != 12 or n_sec != 31 or len(rows) != 114:
        raise RuntimeError(f"HSICS 官方结构校验失败：industry={n_ind}(期望12) "
                           f"sector={n_sec}(期望31) subsector={len(rows)}(期望114)，拒绝写库")
    return rows


def text_lines(doc):
    return "".join(p.get_text() for p in doc).split("\n")


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[&/\-.,'()]", " ", s)
    s = re.sub(r"\b(and|the|of)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _futu_boards() -> list:
    """港股 INDUSTRY 板块（富途 code + 英文名）。

    code 来自 build_sector_hierarchy._SECTOR_MAP 的键；英文名来自该文件**行尾注释**
    （_SECTOR_MAP 的 value 是 (GICS一级, 中文名)，不含英文名）。单一真相源在
    build_sector_hierarchy.py，本脚本只解析不复制，避免两处英文名漂移。
    """
    import pathlib
    import build_sector_hierarchy as bsh
    src = pathlib.Path(bsh.__file__).read_text()
    en_by_code = dict(re.findall(r'"(HK\.LIST\d+)":\s*\([^)]*\),\s*#\s*(.+)$', src, re.M))
    codes = [c for c in bsh._SECTOR_MAP if c.startswith("HK.")]
    missing = [c for c in codes if c not in en_by_code]
    if missing:
        raise RuntimeError(f"富途板块缺英文名注释（无法匹配 HSICS）：{missing}")
    return [(c, en_by_code[c]) for c in codes]


def _build() -> tuple[list, list, list]:
    as_of = datetime.date.today()
    hsics = _parse_pdf()   # 内部已硬校验 12/31/114

    class_rows = [{
        "industry_code": ind, "industry_name": ind_name,
        "sector_code": sec, "sector_name": sec_name,
        "subsector_code": sub, "subsector_name": sub_name,
        "as_of": as_of,
    } for ind, ind_name, sec, sec_name, sub, sub_name in hsics]

    # 英文名 → 官方码
    by_norm = {}
    for ind, ind_name, sec, sec_name, sub, sub_name in hsics:
        by_norm.setdefault(_norm(sub_name), sub)

    futu = _futu_boards()
    map_rows = []
    unmatched = []
    used_codes = {}   # 一码多配检测：同一官方码被多个富途板块命中 → 全部转 manual
    usage = {}
    for code, en in futu:
        sub = by_norm.get(_norm(en))
        if sub:
            usage[sub] = usage.get(sub, 0) + 1
            used_codes[code] = sub
    for code, en in futu:
        sub = used_codes.get(code)
        if sub and usage[sub] > 1:
            sub = None   # 冲突码不自动采信，转人工
        if sub:
            method, confirmed = "auto", False
        else:
            sub, method, confirmed = None, "manual", False
            unmatched.append((code, en))
        map_rows.append({
            "futu_code": code, "futu_name": en, "hsics_code": sub,
            "match_method": method, "confirmed": confirmed, "as_of": as_of,
        })
    return class_rows, map_rows, unmatched


def _preserve_confirmed(conn, new_rows: list):
    """保留已人工确认（confirmed=true）的行，不被自动刷新覆盖。"""
    with conn.cursor() as cur:
        cur.execute("SELECT futu_code, hsics_code FROM " + T_MAP + " WHERE confirmed=TRUE")
        confirmed = dict(cur.fetchall())
    for r in new_rows:
        if r["futu_code"] in confirmed:
            r["hsics_code"] = confirmed[r["futu_code"]]
            r["match_method"] = "manual"
            r["confirmed"] = True


def run(codes=None, ctx=None) -> dict:
    class_rows, map_rows, unmatched = _build()
    with get_conn() as conn:
        _preserve_confirmed(conn, map_rows)
        bulk_upsert(conn, T_CLASS, class_rows, conflict_cols=["subsector_code"])
        bulk_upsert(conn, T_MAP, map_rows, conflict_cols=["futu_code"])
    print(f"写入 {T_CLASS}: {len(class_rows)} 行；{T_MAP}: {len(map_rows)} 行；"
          f"未自动匹配（待人工核对）: {len(unmatched)}")
    for c, e in unmatched:
        print(f"  ⚠ 待核对 {c}\t{e}")
    return {"class_rows": len(class_rows), "map_rows": len(map_rows), "unmatched": len(unmatched)}


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = ap.parse_args()
    class_rows, map_rows, unmatched = _build()
    from collections import Counter
    print(f"分类层级 {len(class_rows)} 行；码映射 {len(map_rows)} 行 "
          f"(auto {sum(1 for r in map_rows if r['match_method']=='auto')} / "
          f"manual {len(unmatched)})")
    print("\n=== 未自动匹配清单（待人工核对）===")
    for c, e in unmatched:
        print(f"  {c}\t{e}")
    if not args.dry_run:
        with get_conn() as conn:
            _preserve_confirmed(conn, map_rows)
            bulk_upsert(conn, T_CLASS, class_rows, conflict_cols=["subsector_code"])
            bulk_upsert(conn, T_MAP, map_rows, conflict_cols=["futu_code"])
        print("已写入两张表")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
