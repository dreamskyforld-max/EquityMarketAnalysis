#!/usr/bin/env python3
"""
公司回购数据 — 港股（逐日明细）+ A股（方案维度），全量版 + 常驻调用

数据源：
- 港股全量：东财数据中心 datacenter-web API（RPT_HK_BUYBACK，全市场逐日回购明细，
  按 TRADE_DATE 过滤分页拉取最近 FULL_DAYS 天）。港股强制每日披露回购，故有逐日粒度。
- 港股单票：hk.eastmoney.com/buyback.html 静态页（兼容 wecom 手动单票触发）。
- A股：AKShare stock_repurchase_em（全市场回购方案进度，2922+ 家、万亿级）。
  A股不强制每日披露，仅有「回购方案 + 累计已回购」口径，无逐日明细，故单独成表。

写入表：
- 港股 → daily_buyback_event（逐日明细，按 stock_code + buyback_date 去重）。
- A股 → a_stock_repurchase_plan（方案维度，按 stock_code + plan_id 去重）。
支持港股（HK）与 A股（SH/SZ）。
"""
import sys, re, json, urllib.request, urllib.parse
from datetime import date, timedelta
from db import get_conn, bulk_upsert

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME_HK = "RPT_HK_BUYBACK"      # 港股全市场逐日回购明细
FULL_DAYS = 90      # 全量模式拉取最近 N 天回购明细（港股）
PAGE_SIZE = 500     # 东财 datacenter 单页上限

_DATACENTER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://data.eastmoney.com/",
}

def fmt_price(val, max_dec=4):
    if val is None: return "N/A"
    try: v = round(float(val), max_dec)
    except: return str(val)
    s = f"{v:.{max_dec}f}".rstrip('0').rstrip('.')
    return s if '.' in s else s + ".0"

def get_currency(full_code):
    return "港元" if full_code.upper().startswith("HK") else "元"

def _http_get_json(url):
    req = urllib.request.Request(url, headers=_DATACENTER_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))

def fetch_full_buyback(days=FULL_DAYS):
    """港股全市场回购明细分页拉取（按 TRADE_DATE 倒序）。

    days: 拉取最近 N 天（默认 FULL_DAYS）；传 None = 不限时间，拉取报表全历史。
    失败返回 None。
    """
    flt = ""
    if days is not None:
        start = (date.today() - timedelta(days=days)).isoformat()
        flt = urllib.parse.quote(f"(TRADE_DATE>='{start}')")
    records = []
    page = 1
    while True:
        url = (f"{DATACENTER_URL}?reportName={REPORT_NAME_HK}&columns=ALL"
               f"&pageNumber={page}&pageSize={PAGE_SIZE}"
               f"&sortColumns=TRADE_DATE&sortTypes=-1"
               + (f"&filter={flt}" if flt else ""))
        try:
            data = _http_get_json(url)
        except Exception as e:
            print(f"[调试] 全量回购分页失败 p{page}: {e}")
            break
        rows = (data or {}).get("result") or {}
        page_data = rows.get("data") or []
        records.extend(page_data)
        count = rows.get("count") or 0
        if not page_data or page * PAGE_SIZE >= count:
            break
        page += 1
    return records or None

def _records_to_db_rows(records):
    """东财接口原始记录 → daily_buyback_event 行列表（纯函数，可单测）。

    按 (stock_code, buyback_date) 去重：接口按 TRADE_DATE 倒序返回，
    同一天多条回购公告取最新一条（与单票模式一天一条口径一致）。
    注意：接口无最高/最低成交价，high_price/low_price 置 None。
    """
    db_rows = []
    seen = set()
    for r in records:
        sec = str(r.get("SECURITY_CODE") or "").strip()
        if not sec.isdigit():
            continue
        code = f"HK.{int(sec):05d}"
        raw_date = str(r.get("TRADE_DATE") or "")[:10]
        try:
            date.fromisoformat(raw_date)
        except ValueError:
            continue
        key = (code, raw_date)
        if key in seen:
            continue
        seen.add(key)
        db_rows.append({
            "stock_code": code,
            "buyback_date": raw_date,
            "volume": int(r.get("REPO_NUM") or 0),
            "high_price": None,
            "low_price": None,
            "avg_price": r.get("AVG_PRICE"),
            "amount": r.get("REPO_AMT"),
        })
    return db_rows

# ---------------------------------------------------------------------------
# A 股回购（AKShare stock_repurchase_em，方案维度，无逐日明细）
# ---------------------------------------------------------------------------
def _a_market_prefix(sec):
    """6位 A股代码 → SH./SZ. 前缀（60/68/9 开头=沪，其余=深）。"""
    return "SH" if sec[0] in ("6", "9") else "SZ"

def _a_parse_num(v):
    """把 AKShare 可能带逗号/单位的字符串解析为浮点数值（None/NaN 透传）。"""
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return f

def _a_parse_int(v):
    """把 AKShare 数量字段解析为 int（BIGINT 列用，避免 float 写入报错）。"""
    f = _a_parse_num(v)
    if f is None:
        return None
    return int(round(f))

def _a_parse_date(v):
    """解析 AKShare 日期字符串为 date；失败返回 None。"""
    if v is None or v == "":
        return None
    s = str(v).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None

def _a_make_plan_id(code, start_date, amt_lo, amt_hi):
    """合成方案业务键（稳定唯一）：code|start_date|plan_amt_lo|plan_amt_hi。"""
    return f"{code}|{start_date or ''}|{amt_lo or ''}|{amt_hi or ''}"

def fetch_a_repurchase():
    """A股全市场回购方案（AKShare stock_repurchase_em）。失败返回 None。"""
    try:
        import akshare as ak
        df = ak.stock_repurchase_em()
    except Exception as e:
        print(f"[调试] A股回购方案拉取失败: {e}")
        return None
    return df

def _a_repurchase_to_db_rows(df):
    """AKShare DataFrame → a_stock_repurchase_plan 行列表（纯函数，可单测）。

    每行 = 一个回购方案最新进度快照。plan_id 由 (code, 起始时间, 计划金额区间) 合成，
    同一方案跨时间重新拉取时 plan_id 不变，可 ON CONFLICT DO UPDATE 滚动刷新。
    字段缺失（预案阶段尚未执行）存 None。
    """
    if df is None:
        return []
    db_rows = []
    for _, r in df.iterrows():
        sec = str(r.get("股票代码") or "").strip()
        if not sec.isdigit():
            continue
        code = f"{_a_market_prefix(sec)}.{sec}"
        start_date = _a_parse_date(r.get("回购起始时间"))
        amt_lo = _a_parse_num(r.get("计划回购金额区间-下限"))
        amt_hi = _a_parse_num(r.get("计划回购金额区间-上限"))
        plan_id = _a_make_plan_id(code, start_date, amt_lo, amt_hi)
        db_rows.append({
            "stock_code": code,
            "stock_name": str(r.get("股票简称") or "").strip() or None,
            "plan_id": plan_id,
            "progress": str(r.get("实施进度") or "").strip() or None,
            "plan_price_min": _a_parse_num(r.get("计划回购价格区间")),
            "plan_price_max": _a_parse_num(r.get("计划回购价格区间")),
            "plan_qty_min": _a_parse_int(r.get("计划回购数量区间-下限")),
            "plan_qty_max": _a_parse_int(r.get("计划回购数量区间-上限")),
            "plan_amt_min": amt_lo,
            "plan_amt_max": amt_hi,
            "start_date": start_date,
            "repurchased_price_min": _a_parse_num(r.get("已回购股份价格区间-下限")),
            "repurchased_price_max": _a_parse_num(r.get("已回购股份价格区间-上限")),
            "repurchased_qty": _a_parse_int(r.get("已回购股份数量")),
            "repurchased_amt": _a_parse_num(r.get("已回购金额")),
            "latest_ann_date": _a_parse_date(r.get("最新公告日期")),
        })
    return db_rows

def fetch_a_repurchase_by_code(symbol):
    """单票 A股回购方案（本地按 6位代码过滤）。失败返回空列表。"""
    df = fetch_a_repurchase()
    if df is None:
        return []
    sec = str(symbol).strip()
    sub = df[df["股票代码"].astype(str).str.strip() == sec]
    return _a_repurchase_to_db_rows(sub)

def get_buyback(symbol, debug=False):
    today = date.today()
    start_date = f"{today.year}-01-01"
    end_date = today.strftime('%Y-%m-%d')

    url = f"https://hk.eastmoney.com/buyback.html?code={symbol}&sdate={start_date}&edate={end_date}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://hk.eastmoney.com/"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        if debug:
            print(f"[调试] 请求失败: {e}")
        return None

    tr_blocks = re.findall(r'<tr>\s*(.*?)\s*</tr>', html, re.DOTALL)
    records = []

    for block in tr_blocks:
        spans = re.findall(r'<span>(.*?)</span>', block)
        if len(spans) < 7:
            continue
        try:
            seq = spans[0].strip()
            if not seq.isdigit():
                continue
            volume_raw = spans[1].strip()
            high_price = float(spans[2])
            low_price = float(spans[3])
            avg_price = float(spans[4])
            amount_raw = spans[5].strip()
            record_date = spans[6].strip()

            volume = int(float(volume_raw.replace('万', '')) * 1e4)
            amount = float(amount_raw.replace('万', '')) * 1e4

            records.append({
                "date": record_date, "volume": volume,
                "high_price": high_price, "low_price": low_price,
                "avg_price": avg_price, "amount": amount,
            })
        except (ValueError, IndexError):
            continue

    if not records:
        if debug:
            print(f"[调试] 未找到 {symbol} 的回购记录")
        return None

    latest = records[0]
    total_volume = sum(r['volume'] for r in records)
    total_amount = sum(r['amount'] for r in records)

    return {
        "latest": latest, "recent": records[:5],
        "total_records": len(records),
        "total_volume": total_volume, "total_amount": total_amount,
    }

def _run_single(full_code, debug=False):
    """单票模式（wecom 手动触发）：原静态页逻辑，输出格式不变。"""
    if "." in full_code:
        market_prefix, symbol = full_code.split(".", 1)
    else:
        market_prefix, symbol = "HK", full_code

    market_prefix = market_prefix.upper()
    if market_prefix == "HK":
        _run_single_hk(full_code, symbol, debug=debug)
    elif market_prefix in ("SH", "SZ"):
        _run_single_a(full_code, symbol, debug=debug)
    else:
        print(f"公司回购数据 ({full_code}): 仅支持港股(HK)与A股(SH/SZ)")

def _run_single_hk(full_code, symbol, debug=False):
    """单票模式（港股）：原 hk.eastmoney.com 静态页逻辑，输出格式不变。"""
    data = get_buyback(symbol, debug=debug)
    currency = get_currency(full_code)

    if data:
        latest = data['latest']
        print(f"公司回购数据 ({full_code})")
        print(f"数据日期: {latest['date']}")
        print(f"最新回购:")
        print(f"  回购数量: {latest['volume']:,} 股")
        print(f"  回购均价: {fmt_price(latest['avg_price'])} {currency}")
        print(f"  回购总额: {latest['amount']/1e8:.2f} 亿{currency}")
        print(f"  回购价格区间: {fmt_price(latest['low_price'])} - {fmt_price(latest['high_price'])} {currency}")
        print(f"年内累计回购 {data['total_records']} 次")
        print(f"  累计数量: {data['total_volume']:,} 股")
        print(f"  累计金额: {data['total_amount']/1e8:.2f} 亿{currency}")

        print(f"\n最近 5 次回购明细:")
        for i, r in enumerate(data['recent'], 1):
            print(f"  {i}. {r['date']}  {r['volume']:,}股  均价{fmt_price(r['avg_price'])}  总额{r['amount']/1e8:.2f}亿{currency}")

        try:
            all_records = {r['date']: r for r in data['recent']}
            all_records[data['latest']['date']] = data['latest']
            db_data = []
            for r in all_records.values():
                db_data.append({
                    "stock_code": full_code,
                    "buyback_date": date.fromisoformat(r['date']),
                    "volume": r['volume'], "high_price": r['high_price'],
                    "low_price": r['low_price'], "avg_price": r['avg_price'],
                    "amount": r['amount'],
                })
            with get_conn() as conn:
                bulk_upsert(conn, "daily_buyback_event", db_data, conflict_cols=["stock_code", "buyback_date"])
        except Exception as e:
            print(f"[DB] 回购数据入库失败: {e}")
    else:
        print(f"公司回购数据 ({full_code}): 暂无数据（该股票年内无回购记录）")

def _run_single_a(full_code, symbol, debug=False):
    """单票模式（A股）：AKShare stock_repurchase_em，按方案维度输出并写 a_stock_repurchase_plan。"""
    rows = fetch_a_repurchase_by_code(symbol)
    if not rows:
        print(f"公司回购数据 ({full_code}): 暂无回购方案（该股票无公开回购计划）")
        return
    currency = get_currency(full_code)
    # 按最新公告日期倒序展示
    rows.sort(key=lambda r: r.get("latest_ann_date") or date.min, reverse=True)

    print(f"公司回购数据 ({full_code})  —  A股回购方案（方案维度，无逐日明细）")
    print(f"回购方案数: {len(rows)}")
    total_plan = sum((r.get("plan_amt_max") or 0) for r in rows)
    total_done = sum((r.get("repurchased_amt") or 0) for r in rows)
    print(f"  计划回购上限合计: {total_plan/1e8:.2f} 亿{currency}")
    print(f"  已回购金额合计:   {total_done/1e8:.2f} 亿{currency}")

    print(f"\n最近 5 个方案:")
    for i, r in enumerate(rows[:5], 1):
        ann = r.get("latest_ann_date")
        pa = r.get("plan_amt_max")
        ra = r.get("repurchased_amt")
        print(f"  {i}. 公告{ann} [{r.get('progress')}] "
              f"计划≤{pa/1e8:.2f}亿 已回购{ra/1e8:.2f}亿 {currency}" if pa else
              f"  {i}. 公告{ann} [{r.get('progress')}] 已回购{(ra or 0)/1e8:.2f}亿 {currency}")

    try:
        with get_conn() as conn:
            bulk_upsert(conn, "a_stock_repurchase_plan", rows,
                        conflict_cols=["stock_code", "plan_id"])
    except Exception as e:
        print(f"[DB] A股回购方案入库失败: {e}")

def _run_full(debug=False):
    """全量模式（全局任务）：东财数据中心全市场回购明细（港股 + A股）分页拉取并全量入库。"""
    total_rows = 0
    total_stock = 0
    # 港股
    hk_records = fetch_full_buyback()
    if hk_records:
        hk_rows = _records_to_db_rows(hk_records)
        if hk_rows:
            try:
                with get_conn() as conn:
                    bulk_upsert(conn, "daily_buyback_event", hk_rows, conflict_cols=["stock_code", "buyback_date"])
                total_rows += len(hk_rows)
                total_stock += len({r["stock_code"] for r in hk_rows})
            except Exception as e:
                print(f"[DB] 港股回购入库失败: {e}")
    else:
        print("公司回购数据: 港股全市场获取失败或无数据")
    # A股（方案维度，独立表）
    a_rows = _a_repurchase_to_db_rows(fetch_a_repurchase())
    if a_rows:
        try:
            with get_conn() as conn:
                bulk_upsert(conn, "a_stock_repurchase_plan", a_rows,
                            conflict_cols=["stock_code", "plan_id"])
            total_rows += len(a_rows)
            total_stock += len({r["stock_code"] for r in a_rows})
        except Exception as e:
            print(f"[DB] A股回购方案入库失败: {e}")
    else:
        print("公司回购数据: A股全市场获取失败或无数据")
    if total_rows:
        print(f"公司回购数据: 入库 {total_rows} 条"
              f"（港股=daily_buyback_event 逐日明细；A股=a_stock_repurchase_plan 方案，共 {total_stock} 只标的）")
    else:
        print("公司回购数据: 全市场无有效回购记录")

def run(codes=None, ctx=None, debug=False):
    """采集入口（常驻调用）。

    codes: None/空 = 全量采集全市场回购明细入库（港股 + A股，全局任务用法）；
           给定 = 单票采集（按代码前缀分流 HK/A股，兼容 wecom 手动触发）。
    ctx 未使用（数据源为东方财富）。
    """
    if codes and len(codes) > 0:
        _run_single(codes[0], debug=debug)
    else:
        _run_full(debug=debug)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default=None,
                        help="股票代码（如 HK.00700 / SH.600585）；不填 = 全量采集港股+A股回购明细")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run([args.code] if args.code else None, debug=args.debug)
