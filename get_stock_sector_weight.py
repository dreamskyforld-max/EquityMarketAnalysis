#!/usr/bin/env python3
"""
港股指数成分权重采集 —— 从恒生指数公司官网 factsheet PDF 提取 WEIGHTING 列

背景：stock_sector.weight 此前恒为 NULL（富途 get_plate_stock / AKShare 均不提供
港股指数成分权重）。恒生指数公司官网的指数单张（factsheet PDF）内含完整成分权重表，
可免费下载、无需登录，故用 pymupdf 解析 PDF 补全 weight。

数据链路：
  1. GET /data/{lang}/download/factsheets.json → 拿到各指数 factsheetFile 路径
     （用 indexShortName 定位，避免硬编码 URL）
  2. 下载 PDF → pymupdf 提取文本
  3. 定位 "CONSTITUENTS" 段 → 按 6 列解析（Stock Code / ISIN / Company / Industry /
     Share Type / Weighting），Stock Code 补 "HK." 前缀
  4. UPDATE stock_sector.weight（按 stock_code + sector_code 精确匹配）

刷新策略：权重随指数季度审核变化，低频；脚本每次运行覆盖该 sector_code 下已采集到的
成分权重（仅更新 weight 列，不动 sector_name / source 等，也不删除未在 PDF 中出现的行）。

用法：
    python3 get_stock_sector_weight.py            # 采集全部目标港股指数
    python3 get_stock_sector_weight.py HSI        # 只采集指定 indexShortName
"""
import sys
import logging
import re
import urllib.request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stock_sector_weight")

# ── 目标港股指数（indexShortName → sector_code，须与 get_stock_sector.py 一致）──
#   sector_code 是 stock_sector 表的规范存储值
HK_INDEX_MAP = {
    "HSI":    "HK.800000",  # 恒生指数
    "HSTECH": "HK.800700",  # 恒生科技
    "HSCEI":  "HK.800100",  # 恒生中国企业
}

_BASE = "https://www.hsi.com.hk"
_FACTSHEETS_JSON = "/data/eng/download/factsheets.json"
_UA = {"User-Agent": "Mozilla/5.0"}

# 成分表表头（用于校验解析正确性）
_HEADER_TOKENS = ["STOCK CODE", "WEIGHTING"]


def _http_get(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _find_factsheet_path(short_name):
    """从 factsheets.json 元数据里定位某指数的 factsheet PDF 相对路径。"""
    import json
    data = json.loads(_http_get(_BASE + _FACTSHEETS_JSON))

    def walk(items):
        for it in items:
            if it.get("indexShortName") == short_name and it.get("factsheetFile"):
                return it["factsheetFile"]
            for key in ("indexList", "subIndexList"):
                child = it.get(key) or []
                found = walk(child)
                if found:
                    return found
        return None

    for series in data.get("indexSeriesList", []):
        found = walk(series.get("indexList", []))
        if found:
            return found
    return None


def _parse_weight_table(pdf_bytes):
    """解析 factsheet PDF，返回 {stock_code4: weight_float}。

    依赖 pymupdf（项目 venv 已安装），能正确处理 CID 字体子集的文本提取。
    """
    import pymupdf
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        full_text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    idx = full_text.find("CONSTITUENTS")
    if idx < 0:
        log.warning("PDF 未找到 CONSTITUENTS 段")
        return {}

    section = full_text[idx:]
    lines = [ln.strip() for ln in section.splitlines()]

    # 表头：Stock Code / ISIN CODE / Company Name / Industry Classification /
    #        [Share Type] / Weighting (%)。
    # 不同指数列数不同（HSI/HSTECH 6 列，HSCEI 无 Share Type 5 列），
    # 故用「Stock Code 到 Weighting 之间的行数」自适应确定每条记录行数 N。
    header_i = next(
        (i for i, ln in enumerate(lines) if ln.upper() == "STOCK CODE"),
        None,
    )
    if header_i is None:
        log.warning("未找到成分表表头（Stock Code）")
        return {}

    weight_header_i = next(
        (i for i in range(header_i, min(header_i + 8, len(lines)))
         if lines[i].upper().startswith("WEIGHTING")),
        None,
    )
    if weight_header_i is None:
        log.warning("未找到 Weighting 表头列")
        return {}

    n_cols = weight_header_i - header_i + 1  # 每条记录的行数（列数）

    result = {}
    i = weight_header_i + 1  # 数据从 Weighting 表头之后开始
    while i < len(lines):
        ln = lines[i]
        # 遇到 Total / 尾注则结束
        if ln.upper().startswith("TOTAL") or "may not add" in ln.lower():
            break
        if re.fullmatch(r"\d{1,4}", ln) and i + n_cols - 1 < len(lines):
            w_line = lines[i + n_cols - 1]
            w = re.fullmatch(r"(\d+(?:\.\d+)?)", w_line)
            if w:
                result[ln] = float(w.group(1))
                i += n_cols
                continue
        i += 1

    log.info(f"解析到 {len(result)} 只成分股权重")
    return result


def _update_weights(sector_code, weight_map):
    """按 stock_code + sector_code 精确更新 weight（只更新，不删不插）。"""
    if not weight_map:
        return 0
    from db import get_conn
    updated = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for code4, w in weight_map.items():
                # 恒生 factsheet 用 4 位代码，库内 stock_code 统一 5 位（左边补 0）
                stock_code = f"HK.{code4.zfill(5)}"
                cur.execute(
                    "UPDATE stock_sector SET weight=%s, updated_at=NOW() "
                    "WHERE stock_code=%s AND sector_code=%s",
                    (w, stock_code, sector_code),
                )
                updated += cur.rowcount
    return updated


def run(codes=None, ctx=None):
    """采集入口（常驻调用约定：run(codes, ctx)，均未使用）。

    独立运行时（CLI）也可通过位置参数指定要采集的 indexShortName。
    """
    if codes:
        short_names = [c for c in codes if c in HK_INDEX_MAP]
    else:
        short_names = list(HK_INDEX_MAP.keys())

    total = 0
    for short in short_names:
        sector_code = HK_INDEX_MAP.get(short)
        if sector_code is None:
            log.warning(f"未知指数简称 {short}，跳过")
            continue
        log.info(f"处理 {short} (sector_code={sector_code})")
        path = _find_factsheet_path(short)
        if not path:
            log.warning(f"{short} 未在 factsheets.json 找到 factsheet 路径")
            continue
        url = _BASE + path
        try:
            pdf_bytes = _http_get(url)
        except Exception as e:
            log.warning(f"下载 {url} 失败: {e}")
            continue
        weight_map = _parse_weight_table(pdf_bytes)
        n = _update_weights(sector_code, weight_map)
        total += n
        log.info(f"{short}: 更新 {n} 行 weight")

    print(f"\n[stock_sector_weight] 本次更新 {total} 行")
    return total


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    run(codes=args or None)
