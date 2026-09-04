#!/usr/bin/env python3
"""
分红送配明细（全市场：A股 + 港股）→ dividend_history 表

数据源：
- A股: 东方财富数据中心 RPT_SHAREBONUS_DET（分红送配明细，全市场全历史，分页拉取；
  PRETAX_BONUS_RMB 为每10股税前派息，SECUCODE 自带市场后缀可直接定位 SH/SZ/BJ）
- 港股: 同花顺 F10 basic.10jqka.com.cn/HKxxxx/bonus.html（HTML 表格解析，
  含「不分红」预案行——显式公告，可直接用于连续分红年数判定）

写入表: dividend_history，唯一键 (stock_code, source, dedup_key)，
ON CONFLICT DO UPDATE 幂等重刷（预案→实施进度推进原地更新）。

口径约定：
- dps = 每股税前现金派息（A股 RMB、港股默认 HKD，按方案文本币种识别）；
  送转股不计入（BONUS_RATIO 不取）。
- 只存「方案/事件」原始明细，不在此做 TTM/LFY 聚合（聚合属计算层）。
- A股报表无发放日列，pay_date 置 None；港股有除净日/派息日/过户起始日。

用法：
    python3 get_dividend_history.py                    # 全市场全历史（回填 = 周刷同入口）
    python3 get_dividend_history.py HK.00700           # 单票
    python3 get_dividend_history.py SH.600900 SZ.000001
"""
import json
import re
import time
import urllib.request
import urllib.parse
from datetime import date
from io import StringIO

import pandas as pd

from db import get_conn, bulk_upsert

# ── 东财 datacenter ──────────────────────────────────────────
EM_DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_REPORT_A = "RPT_SHAREBONUS_DET"
PAGE_SIZE = 500
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
# push2（行情接口）与 datacenter（报表接口）要求不同 Referer
EM_PUSH2_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# ── 同花顺 F10 ───────────────────────────────────────────────
THS_BONUS_URL = "https://basic.10jqka.com.cn/176/HK{sym}/bonus.html"
THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36",
}
THS_SLEEP_SEC = 0.3   # 港股逐票限速（全市场 ~2600 只，一次性回填约 20-30 分钟）
THS_RETRY = 2

TABLE = "dividend_history"
CONFLICT_COLS = ["stock_code", "source", "dedup_key"]


# ═════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════

def _parse_date(v):
    """EM/AKShare 日期字段 → date；失败返回 None。"""
    if v is None or v == "":
        return None
    s = str(v).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _dedup_rows(db_rows: list[dict]) -> list[dict]:
    """按 (stock_code, dedup_key) 组内去重，保留最后一条（页面新数据靠后）。

    防御 ON CONFLICT DO UPDATE "cannot affect row a second time"：
    同一批 upsert 内出现重复唯一键会直接报错中断整批。
    """
    seen = {}
    for r in db_rows:
        seen[(r["stock_code"], r["dedup_key"])] = r
    return list(seen.values())


def ths_symbol(hk_code: str) -> str:
    """HK.00700 的 5 位代码 → 同花顺 URL 代码（去掉一个前导零）。

    00700→0700  00005→0005  00381→0381  09660→9660（实测规律）。
    """
    digits = str(hk_code).strip()
    if digits.startswith("0"):
        digits = digits[1:]
    return digits


def parse_hk_plan(text: str):
    """解析同花顺「方案」文本 → (dps, currency)。

    样例: "每股4.5港元"→(4.5,'HKD')  "每股0.153美元"→(0.153,'USD')
          "不分红"→(None,None)  "每股0.05元人民币"→(0.05,'CNY')
    """
    if not text:
        return None, None
    t = str(text).strip()
    if "不分红" in t or t in ("不分配", "—", "-"):
        return None, None
    m = re.search(r"每股\s*([\d.]+)\s*(港元|港币|美元|美分|人民币|元)", t)
    if not m:
        return None, None
    try:
        dps = float(m.group(1))
    except ValueError:
        return None, None
    unit = m.group(2)
    if unit in ("美元", "美分"):
        cur = "USD"
        if unit == "美分":
            dps = round(dps / 100.0, 6)
    elif unit == "人民币":
        cur = "CNY"
    else:
        cur = "HKD"
    return dps, cur


# ═════════════════════════════════════════════════════════════
# A股：东财 RPT_SHAREBONUS_DET
# ═════════════════════════════════════════════════════════════

def _http_get_json(params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{EM_DC_URL}?{query}", headers=EM_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_a_full() -> list[dict]:
    """A股分红送配明细全量分页拉取（全市场全历史）。失败返回 []。"""
    records = []
    page = 1
    while True:
        params = {
            "reportName": EM_REPORT_A,
            "columns": "ALL",
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            # 双列排序保证翻页稳定性（同日多票按代码二次排序）
            "sortColumns": "EX_DIVIDEND_DATE,SECURITY_CODE",
            "sortTypes": "-1,1",
            "source": "WEB",
            "client": "WEB",
        }
        try:
            data = _http_get_json(params)
        except Exception as e:
            print(f"[EM] A股分红分页失败 p{page}: {type(e).__name__}: {e}")
            break
        rows = (data or {}).get("result") or {}
        page_data = rows.get("data") or []
        records.extend(page_data)
        count = rows.get("count") or 0
        if not page_data or page * PAGE_SIZE >= count:
            break
        page += 1
        time.sleep(0.15)   # 分页限速，避免触发东财风控
    return records


def fetch_a_by_code(sec_code: str) -> list[dict]:
    """单票 A股分红明细（sec_code 为 6 位数字代码）。"""
    params = {
        "reportName": EM_REPORT_A,
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{sec_code}")',
        "pageNumber": 1,
        "pageSize": 500,
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    try:
        data = _http_get_json(params)
    except Exception as e:
        print(f"[EM] A股分红单票 {sec_code} 拉取失败: {type(e).__name__}: {e}")
        return []
    return ((data or {}).get("result") or {}).get("data") or []


def em_records_to_db_rows(records: list[dict]) -> list[dict]:
    """东财原始记录 → dividend_history 行列表（纯函数，可单测）。"""
    db_rows = []
    for r in records:
        secucode = str(r.get("SECUCODE") or "").strip()   # 如 600900.SH / 830799.BJ
        if "." not in secucode:
            continue
        digits, market = secucode.split(".", 1)
        if not digits.isdigit():
            continue
        dps10 = r.get("PRETAX_BONUS_RMB")                  # 每10股税前派息
        dps = round(float(dps10) / 10.0, 6) if dps10 is not None else None
        announce = _parse_date(r.get("PLAN_NOTICE_DATE"))
        report = str(r.get("REPORT_DATE") or "")[:10] or None
        db_rows.append({
            "stock_code": f"{market}.{digits}",
            "announce_date": announce,
            "record_date": _parse_date(r.get("EQUITY_RECORD_DATE")),
            "ex_date": _parse_date(r.get("EX_DIVIDEND_DATE")),
            "pay_date": None,
            "dps": dps,
            "currency": "CNY",
            "progress": str(r.get("ASSIGN_PROGRESS") or "").strip() or None,
            "report_period": report,
            "is_dividend": dps is not None and dps > 0,
            "source": "em",
            # 业务键：预案公告日 + 报告期 + 每股派息额。同方案进度推进时键不变 →
            # 原地更新；同日同报告期两条不同派息额（拆分方案）各自成行
            "dedup_key": f"{announce or ''}|{report or ''}|{dps if dps is not None else 'x'}",
        })
    return _dedup_rows(db_rows)


# ═════════════════════════════════════════════════════════════
# 港股：同花顺 F10
# ═════════════════════════════════════════════════════════════

def _flatten_ths_columns(df: pd.DataFrame) -> pd.DataFrame:
    """同花顺表格列名扁平化：pd.read_html 可能把两行表头解析成 MultiIndex
    （如 ('过户日期起止日','起始')），拼接非空层级为单层字符串列名。"""
    if isinstance(df.columns, pd.MultiIndex):
        flat = []
        for tup in df.columns:
            parts = [str(x).strip() for x in tup if x is not None and str(x) != "nan"]
            flat.append("".join(p for p in parts if p))
        df.columns = flat
    else:
        df.columns = [str(c).strip() for c in df.columns]
    return df


def _resolve_col(df: pd.DataFrame, *keywords: str) -> str | None:
    """模糊列名解析：返回同时包含全部关键词的首个列名（找不到返回 None）。"""
    for c in df.columns:
        if all(k in c for k in keywords):
            return c
    return None


def fetch_hk_by_code(hk_digits: str) -> pd.DataFrame | None:
    """单票港股分红派息表（hk_digits 为 5 位数字代码，如 00700）。失败返回 None。"""
    url = THS_BONUS_URL.format(sym=ths_symbol(hk_digits))
    last_err = None
    for attempt in range(1, THS_RETRY + 1):
        try:
            req = urllib.request.Request(url, headers=THS_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            tables = pd.read_html(StringIO(html))
            if not tables:
                raise ValueError("no tables")
            return _flatten_ths_columns(tables[0])
        except Exception as e:
            last_err = e
            time.sleep(1.0 * attempt)
    print(f"[THS] HK{hk_digits} 分红页拉取失败: {type(last_err).__name__}: {last_err}")
    return None


def hk_df_to_db_rows(df: pd.DataFrame, hk_digits: str) -> list[dict]:
    """同花顺 DataFrame → dividend_history 行列表（纯函数，可单测）。

    列（支持 MultiIndex 扁平化后的模糊匹配）:
    公告日期/方案/除净日/派息日/过户日期起止日+起始/过户日期起止日+截止/类型/进度
    「不分红」行保留（is_dividend=False，占位符 '--' → None），
    这是连续分红年数判定的显式依据。
    """
    if df is None or df.empty:
        return []
    df = _flatten_ths_columns(df)
    col = {
        "announce": _resolve_col(df, "公告日期"),
        "plan": _resolve_col(df, "方案"),
        "ex": _resolve_col(df, "除净日"),
        "pay": _resolve_col(df, "派息日"),
        "record": _resolve_col(df, "过户", "起始"),
        "period": _resolve_col(df, "类型"),
        "progress": _resolve_col(df, "进度"),
    }
    stock_code = f"HK.{int(hk_digits):05d}"
    db_rows = []
    for _, r in df.iterrows():
        plan_text = str(r[col["plan"]]).strip() if col["plan"] else ""
        dps, currency = parse_hk_plan(plan_text)
        announce = _parse_date(r[col["announce"]]) if col["announce"] else None
        period = (str(r[col["period"]]).strip() or None) if col["period"] else None
        db_rows.append({
            "stock_code": stock_code,
            "announce_date": announce,
            "record_date": _parse_date(r[col["record"]]) if col["record"] else None,
            "ex_date": _parse_date(r[col["ex"]]) if col["ex"] else None,
            "pay_date": _parse_date(r[col["pay"]]) if col["pay"] else None,
            "dps": dps,
            "currency": currency if dps is not None else None,
            "progress": (str(r[col["progress"]]).strip() or None) if col["progress"] else None,
            "report_period": period,
            "is_dividend": dps is not None and dps > 0,
            "source": "ths",
            # 业务键：公告日 + 报告类型 + 每股派息额。同日公告多条不同派息额的
            # 真实方案（如派息+特别股息拆两行）各自成行，计算层按 ex_date 聚合
            "dedup_key": f"{announce or ''}|{period or ''}|{dps if dps is not None else 'x'}",
        })
    return _dedup_rows(db_rows)


SINA_HK_LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                    "json_v2.php/Market_Center.getHKStockData")
SINA_HK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def list_hk_codes() -> list[str]:
    """全市场港股正股 5 位数字代码清单（新浪港股列表，~2700 只主板+创业板）。

    注意：不用 ak.stock_hk_spot —— 其底层 urllib 无超时，网络抖动时会无限挂起
    （实测踩坑）。此处直连同源接口并显式 timeout + 页级重试。
    失败返回 []（调用方跳过港股，可重跑；不接受部分清单——漏票比慢更糟）。
    """
    codes: list[str] = []
    page = 1
    while True:
        params = {
            "page": page, "num": 100,
            "sort": "symbol", "asc": 1,
            "node": "qbgg_hk",
        }
        data = None
        for attempt in range(1, 4):     # 页级重试 3 次
            try:
                req = urllib.request.Request(
                    f"{SINA_HK_LIST_URL}?{urllib.parse.urlencode(params)}",
                    headers=SINA_HK_HEADERS)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                break
            except Exception as e:
                print(f"[全市场代码] 新浪港股列表 p{page} 第{attempt}次失败: "
                      f"{type(e).__name__}: {e}")
                time.sleep(1.0 * attempt)
        if data is None:
            return []
        if not data:                    # 空数组 = 翻页结束
            break
        for item in data:
            # 注意：返回字段为 symbol（如 "00001"），无 code 字段；服务端固定 60 条/页
            s = str(item.get("symbol") or "").strip()
            if s.isdigit():
                codes.append(f"{int(s):05d}")
        page += 1
        time.sleep(0.3)                 # 页间限速
    return sorted(set(codes))


# ═════════════════════════════════════════════════════════════
# 入库
# ═════════════════════════════════════════════════════════════

def save_rows(db_rows: list[dict]) -> int:
    if not db_rows:
        return 0
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, db_rows, conflict_cols=CONFLICT_COLS)
    return len(db_rows)


# ═════════════════════════════════════════════════════════════
# run 入口（常驻调用）
# ═════════════════════════════════════════════════════════════

def _run_full(hk_only: bool = False):
    """全量模式：A股分页全量 + 港股全市场逐票全历史。回填与每周刷新同入口。

    hk_only: 跳过 A 股（用于 A 股已入库后单独补跑港股，避免重拉）。
    """
    t0 = time.time()
    total = 0

    # ── A股（东财全市场分页，~200 页）──
    if not hk_only:
        a_rows = em_records_to_db_rows(fetch_a_full())
        if a_rows:
            try:
                total += save_rows(a_rows)
                print(f"[A股] 入库 {len(a_rows)} 条（{len({r['stock_code'] for r in a_rows})} 只）")
            except Exception as e:
                print(f"[DB] A股分红入库失败: {type(e).__name__}: {e}")
        else:
            print("[A股] 全市场拉取失败或无数据")

    # ── 港股（同花顺逐票，全市场 ~2600 只）──
    hk_codes = list_hk_codes()
    if not hk_codes:
        print("[港股] 代码清单获取失败，跳过港股")
    else:
        print(f"[港股] 共 {len(hk_codes)} 只，开始逐票拉取（限速 {THS_SLEEP_SEC}s）...")
        ok = fail = 0
        hk_rows_all = []
        for i, digits in enumerate(hk_codes, 1):
            df = fetch_hk_by_code(digits)
            if df is not None:
                rows = hk_df_to_db_rows(df, digits)
                if rows:
                    hk_rows_all.extend(rows)
                ok += 1
            else:
                fail += 1
            if i % 200 == 0:
                print(f"  进度 {i}/{len(hk_codes)}  ok={ok} fail={fail} 累计行数={len(hk_rows_all)}")
            time.sleep(THS_SLEEP_SEC)
        # 分批入库（每 5000 行一批，避免单事务过大）
        for j in range(0, len(hk_rows_all), 5000):
            try:
                total += save_rows(hk_rows_all[j:j + 5000])
            except Exception as e:
                print(f"[DB] 港股分红入库失败(批次{j//5000 + 1}): {type(e).__name__}: {e}")
        print(f"[港股] 完成 ok={ok} fail={fail} 入库 {len(hk_rows_all)} 条")

    print(f"分红明细全量完成: 入库 {total} 条，耗时 {time.time() - t0:.0f}s")


def _run_codes(codes: list[str]):
    """指定代码模式：按前缀分流（HK→同花顺逐票；SH/SZ/BJ→东财单票）。"""
    total = 0
    for full_code in codes:
        market, _, digits = full_code.partition(".")
        market = market.upper()
        digits = digits.strip()
        try:
            if market == "HK":
                df = fetch_hk_by_code(digits)
                rows = hk_df_to_db_rows(df, digits) if df is not None else []
            elif market in ("SH", "SZ", "BJ"):
                rows = em_records_to_db_rows(fetch_a_by_code(digits))
            else:
                print(f"未知市场 [{market}]，跳过 {full_code}")
                continue
            n = save_rows(rows)
            total += n
            div_rows = [r for r in rows if r["is_dividend"]]
            latest = max((r["ex_date"] or r["announce_date"] or date.min)
                         for r in rows) if rows else None
            print(f"{full_code}: 入库 {n} 条（分红事件 {len(div_rows)} 条，"
                  f"最近除净/公告 {latest or '无'}）")
        except Exception as e:
            print(f"{full_code}: 采集失败 {type(e).__name__}: {e}")
    print(f"指定代码模式完成: 共入库 {total} 条")


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。

    codes: None/空 = 全市场全历史回填/周刷（全局任务用法）；
           list    = 指定代码采集（HK.00700 / SH.600900 等，兼容手动触发）。
    ctx 未使用（数据源为东财/同花顺，不依赖富途上下文）。
    """
    if codes:
        _run_codes(list(codes))
    else:
        _run_full()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="分红送配明细采集（A股东财 + 港股同花顺）")
    parser.add_argument("code", nargs="*", default=[],
                        help="股票代码（如 HK.00700 SH.600900）；不填 = 全市场全历史")
    parser.add_argument("--hk-only", action="store_true",
                        help="全量模式跳过 A 股（A 股已入库后单独补跑港股）")
    args = parser.parse_args()
    if args.code:
        run(args.code)
    else:
        _run_full(hk_only=args.hk_only)
