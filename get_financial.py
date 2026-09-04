#!/usr/bin/env python3
"""
财务指标采集 — AKShare API（常驻调用版，支持港股+A股全量）

数据源：
  - 港股：stock_financial_hk_analysis_indicator_em（东方财富港股财务分析指标，年度+报告期）
          + stock_financial_hk_report_em（现金流量表，算自由现金流 FCF）
  - A 股：stock_financial_abstract（新浪财经财务摘要，年度+季度）

采集范围（两种模式）：
  - 全量模式 run(codes=None)：拉取港股+A股「全部代码」的财务指标。代码清单通过
    _list_all_codes() 获取，带本地文件缓存（stock_list_cache.json，TTL 7 天），
    避免每次都打 AKShare。单只异常隔离，不中断整批。这是调度器全局任务的用法。
  - 指定模式 run([code,...])：仅采集给定代码（wecom 手动触发 / 调试用）。

落库：bulk_upsert → financial_indicator，唯一键 (stock_code, report_date)。
常驻调用：run(codes, ctx=None)。
__main__ 默认全量：`python get_financial.py`（不加参数即采集港股+A股全部代码）；
给定 code 才单只：`python get_financial.py HK.00700`；`--all` 显式全量（等价于默认）。
"""
import sys
import os
import json
from datetime import date, datetime, timezone
import akshare as ak
from db import get_conn, bulk_upsert


# ---------- 港股采集 ----------

def _build_hk_record(row, cf_data: dict) -> dict | None:
    report_date_str = str(row["REPORT_DATE"])[:10]
    revenue = row["OPERATE_INCOME"]
    ocf_sales = row["OCF_SALES"]
    operating_cash_flow = (ocf_sales / 100) * revenue if ocf_sales is not None and revenue is not None else None
    free_cash_flow = _calc_hk_fcf(cf_data.get(report_date_str))

    return {
        "report_date": date.fromisoformat(report_date_str),
        "report_type": _report_type(report_date_str),
        "revenue": _safe_float(revenue),
        "net_profit": _safe_float(row["HOLDER_PROFIT"]),
        "gross_profit_rate": _safe_float(row["GROSS_PROFIT_RATIO"]),
        "net_profit_rate": _safe_float(row["NET_PROFIT_RATIO"]),
        "roe": _safe_float(row["ROE_AVG"]),
        "debt_ratio": _safe_float(row["DEBT_ASSET_RATIO"]),
        "revenue_yoy": _safe_float(row["OPERATE_INCOME_YOY"]),
        "net_profit_yoy": _safe_float(row["HOLDER_PROFIT_YOY"]),
        "operating_cash_flow": _safe_float(operating_cash_flow),
        "free_cash_flow": _safe_float(free_cash_flow),
    }


def fetch_hk(symbol: str) -> list[dict]:
    df_annual = ak.stock_financial_hk_analysis_indicator_em(symbol=symbol, indicator="年度")
    df_quarterly = ak.stock_financial_hk_analysis_indicator_em(symbol=symbol, indicator="报告期")
    cf_data = _fetch_hk_cash_flow(symbol)

    seen = set()
    records = []
    for _, row in df_annual.iterrows():
        r = _build_hk_record(row, cf_data)
        k = (r["report_date"], r["report_type"])
        if k not in seen:
            seen.add(k)
            records.append(r)

    for _, row in df_quarterly.iterrows():
        r = _build_hk_record(row, cf_data)
        k = (r["report_date"], r["report_type"])
        if k not in seen:
            seen.add(k)
            records.append(r)

    records.sort(key=lambda x: x["report_date"])
    return records


# ---------- A股采集 ----------

A_INDICATOR_MAP = {
    "营业总收入":            "revenue",
    "归母净利润":             "net_profit",
    "毛利率":                "gross_profit_rate",
    "销售净利率":             "net_profit_rate",
    "净资产收益率(ROE)":      "roe",
    "资产负债率":             "debt_ratio",
    "营业总收入增长率":        "revenue_yoy",
    "归属母公司净利润增长率":   "net_profit_yoy",
    "经营现金流量净额":        "operating_cash_flow",
}


def fetch_a(symbol: str) -> list[dict]:
    df = ak.stock_financial_abstract(symbol=symbol)
    period_cols = [c for c in df.columns if c not in ("选项", "指标")]

    indicator_rows = {}
    for _, row in df.iterrows():
        indicator_rows[row["指标"]] = row

    records = []
    for period_col in period_cols:
        report_date = _parse_period_date(period_col)
        if report_date is None:
            continue

        record = {"report_date": report_date, "report_type": _report_type(str(report_date))}
        has_data = False

        for cn_name, db_field in A_INDICATOR_MAP.items():
            if cn_name in indicator_rows:
                val = indicator_rows[cn_name].get(period_col)
                record[db_field] = _safe_float(val)
                if val is not None:
                    has_data = True

        record["free_cash_flow"] = _calc_fcff(indicator_rows, period_col, record.get("revenue"))
        if has_data:
            records.append(record)

    records.sort(key=lambda x: x["report_date"])
    return records


# ---------- A股披露日（业绩报表「最新公告日期」）----------

def _parse_date(val) -> date | None:
    """把 '2026-03-19' / '20260319' 等解析为 date；非法返回 None。"""
    if val is None:
        return None
    s = str(val).strip().replace("-", "").replace("/", "")
    if len(s) >= 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except (ValueError, KeyError):
            return None
    return None


def fetch_a_announce_dates(period: str) -> dict[str, date]:
    """拉取指定报告期(date='YYYYMMDD')全市场 A 股业绩报表，返回 {完整代码: 最新公告日期}。

    数据源：AKShare stock_yjbb_em（东方财富业绩报表），含「最新公告日期」列。
    该接口按报告期返回全市场（非按单只），故用于批量回填披露日；港股无对应列，不在此处理。

    健壮性：东方财富对极早期报告期（如 1994 年）可能返回 None 或内部解析报错
    （TypeError: 'NoneType' object is not subscriptable）。两种情况都视为「该期无披露数据」，
    安全跳过，不中断整批回填。
    """
    try:
        df = ak.stock_yjbb_em(date=period)
    except Exception as e:
        # 接口内部报错（极早期/无数据期常见）→ 该期跳过，不影响其他期
        print(f"[A股披露日] stock_yjbb_em({period}) 接口异常，跳过该期: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return {}
    if df is None or len(df) == 0:
        # 接口返回空（无数据）→ 该期跳过，不刷 error
        return {}
    result: dict[str, date] = {}
    for _, row in df.iterrows():
        code6 = str(row.get("股票代码", "")).strip()
        if not code6.isdigit():
            continue
        full = f"{_guess_market(code6)}.{code6}"
        ad = _parse_date(row.get("最新公告日期"))
        if ad is not None:
            result[full] = ad
    return result


def run_announce_date(codes=None, ctx=None):
    """回填 financial_indicator.announce_date（仅 A 股；港股无数据源，恒留 NULL）。

    流程：取库内已有的 A 股 (stock_code, report_date) → 按报告期逐一调 stock_yjbb_em
    拿全市场「最新公告日期」→ 仅更新已存在行的 announce_date（skip_null_updates，
    既不把已有值覆盖成 NULL，也不会插入财务指标尚未存在的脏行）。

    调用：python get_financial.py --announce-date
    """
    from collections import defaultdict

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, report_date FROM financial_indicator "
                "WHERE stock_code LIKE 'SH.%%' OR stock_code LIKE 'SZ.%%'"
            )
            existing = {(r[0], r[1]) for r in cur.fetchall()}
    if not existing:
        print("[A股披露日] financial_indicator 内无 A 股行，跳过")
        return

    by_period: dict[date, list] = defaultdict(list)
    for c, rd in existing:
        by_period[rd].append(c)

    periods = sorted(by_period.keys())
    print(f"[A股披露日] 需回填 {len(periods)} 个报告期，覆盖 {len(existing)} 行 A 股财务记录")
    total_updated = 0
    for rd in periods:
        period = rd.strftime("%Y%m%d")
        mapping = fetch_a_announce_dates(period)
        if not mapping:
            continue
        rows = [
            {"stock_code": c, "report_date": rd, "announce_date": mapping[c]}
            for c in by_period[rd] if c in mapping
        ]
        if not rows:
            continue
        try:
            with get_conn() as conn:
                bulk_upsert(conn, "financial_indicator", rows,
                            conflict_cols=["stock_code", "report_date"],
                            skip_null_updates=True)
            total_updated += len(rows)
            print(f"[A股披露日] {period} 更新 {len(rows)} 行")
        except Exception as e:
            print(f"[A股披露日] {period} 写入失败: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"[A股披露日] 回填完成，共更新 {total_updated} 行")


# ---------- 工具函数 ----------

def _fetch_hk_cash_flow(symbol: str) -> dict[str, dict]:
    try:
        df = ak.stock_financial_hk_report_em(stock=symbol, symbol="现金流量表", indicator="报告期")
    except Exception:
        return {}

    result = {}
    targets = {
        "ocf":               "经营业务现金净额",
        "capex_fixed":       "购建固定资产",
        "capex_intangible":  "购建无形资产及其他资产",
    }
    for _, row in df.iterrows():
        item_name = row.get("STD_ITEM_NAME", "")
        for key, cn_name in targets.items():
            if item_name == cn_name:
                rds = str(row["REPORT_DATE"])[:10]
                if rds not in result:
                    result[rds] = {}
                result[rds][key] = _safe_float(row.get("AMOUNT"))
                break
    return result


def _calc_hk_fcf(cf_period: dict | None) -> float | None:
    if cf_period is None:
        return None
    ocf = cf_period.get("ocf")
    capex_fixed = cf_period.get("capex_fixed")
    capex_intangible = cf_period.get("capex_intangible")
    if ocf is None:
        return None
    fcf = ocf
    if capex_fixed is not None:
        fcf -= capex_fixed
    if capex_intangible is not None:
        fcf -= capex_intangible
    return fcf


def _calc_fcff(indicator_rows: dict, period_col: str, revenue: float | None) -> float | None:
    fcff_ps_row = indicator_rows.get("每股企业自由现金流量")
    rev_ps_row = indicator_rows.get("每股营业总收入")
    if fcff_ps_row is None or rev_ps_row is None or revenue is None:
        return None
    fcff_ps = _safe_float(fcff_ps_row.get(period_col))
    rev_ps = _safe_float(rev_ps_row.get(period_col))
    if fcff_ps is None or rev_ps is None or rev_ps == 0:
        return None
    total_shares = revenue / rev_ps
    return fcff_ps * total_shares


def _report_type(date_str: str) -> str:
    mmdd = date_str[5:10] if len(date_str) >= 10 else ""
    mapping = {"12-31": "annual", "03-31": "Q1", "06-30": "H1", "09-30": "Q3"}
    return mapping.get(mmdd, "annual")


def _safe_float(val):
    if val is None:
        return None
    try:
        v = float(val)
        if v != v:
            return None
        return v
    except (ValueError, TypeError):
        return None


def _parse_period_date(raw: str) -> date | None:
    raw = str(raw).strip().replace("-", "").replace("/", "")
    if len(raw) >= 8:
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except (ValueError, KeyError):
            pass
    return None


def _guess_market(stock_code: str) -> str:
    if "." in stock_code:
        return stock_code.split(".")[0].upper()
    if stock_code.startswith("6"):
        return "SH"
    if stock_code.startswith(("0", "3")):
        return "SZ"
    return "HK"


# ---------- 全市场代码清单（全局任务用，本地文件缓存）----------

# 缓存用项目根目录下的本地 JSON 文件（不经共用数据库，避免污染业务表）。
# 与 get_analyst_targets.py 用 analyst_targets.json 的惯例一致：以本文件目录定位项目根。
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_PROJECT_ROOT, "stock_list_cache.json")
CACHE_TTL_DAYS = 7  # 缓存有效期（天），与 get_hk_market_turnover 一致


def _load_cached_codes() -> tuple[list[str], datetime | None]:
    """从本地缓存文件读代码清单。返回 (codes, cached_at)；文件不存在/损坏返回 ([], None)。"""
    if not os.path.exists(CACHE_FILE):
        return [], None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        codes = data.get("codes", [])
        cached_at = datetime.fromisoformat(data["cached_at"]) if data.get("cached_at") else None
        return codes, cached_at
    except Exception as e:
        print(f"[全市场代码] 缓存文件读取失败({CACHE_FILE}): {type(e).__name__}: {e}", file=sys.stderr)
        return [], None


def _save_cache(codes: list[str]):
    """将完整代码清单写入本地缓存文件（含写入时间戳）。"""
    now = datetime.now(timezone.utc)
    payload = {"cached_at": now.isoformat(), "count": len(codes), "codes": codes}
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[全市场代码] 更新缓存（{now.date()}，{len(codes)} 只）→ {CACHE_FILE}")
    except Exception as e:
        print(f"[全市场代码] 缓存文件写入失败({CACHE_FILE}): {type(e).__name__}: {e}", file=sys.stderr)


def _fetch_all_codes_from_akshare() -> list[str]:
    """直连 AKShare 拉全市场代码（港股 + A 股）。单市场失败不影响另一市场。"""
    codes: list[str] = []
    # 港股
    try:
        df_hk = ak.stock_hk_spot()
        for c in df_hk.get("代码", []):
            s = str(c).strip()
            if s.isdigit():
                codes.append(f"HK.{int(s):05d}")
    except Exception as e:
        print(f"[全市场代码] 港股 stock_hk_spot 失败: {type(e).__name__}: {e}", file=sys.stderr)
    # A 股（沪 60/68/9 开头，深其余）
    try:
        df_a = ak.stock_info_a_code_name()
        for c in df_a.get("code", []):
            s = str(c).strip()
            if not s.isdigit():
                continue
            prefix = "SH" if s[0] in ("6", "9") else "SZ"
            codes.append(f"{prefix}.{s}")
    except Exception as e:
        print(f"[全市场代码] A股 stock_info_a_code_name 失败: {type(e).__name__}: {e}", file=sys.stderr)
    return codes


def _list_all_codes() -> list[str]:
    """全市场代码清单（港股 + A 股，带本地文件缓存，TTL 7 天）。

    优先读本地缓存文件（未过期直接返回，避免每次全量采集都打 AKShare）；
    过期/为空才调 AKShare 重新拉取并刷新缓存文件。单市场接口失败不影响整体——
    若缓存过期且拉取失败，兜底用旧缓存文件（为空则跳过）。
    """
    cached, cached_at = _load_cached_codes()
    now = datetime.now(timezone.utc)
    if cached_at is not None and cached:
        from datetime import timedelta
        if (now - cached_at) < timedelta(days=CACHE_TTL_DAYS):
            print(f"[全市场代码] 命中缓存（{cached_at.date()}，{len(cached)} 只），跳过 AKShare 拉取")
            return cached
    # 缓存过期/为空 → 拉 AKShare
    codes = _fetch_all_codes_from_akshare()
    if codes:
        _save_cache(codes)
    else:
        # 拉取失败，兜底用旧缓存（可能为空）
        print("[全市场代码] AKShare 拉取为空/失败，尝试用旧缓存兜底")
        codes = cached
    return codes


# ---------- 采集入口（常驻调用）----------

def _fetch_one(full_code: str) -> list[dict]:
    """单只股票拉取财务指标记录（复用 fetch_hk/fetch_a），返回 record 列表。"""
    if "." in full_code:
        market, symbol = full_code.split(".", 1)
        market = market.upper()
    else:
        market = _guess_market(full_code)
        symbol = full_code
    if market == "HK":
        return fetch_hk(symbol)
    return fetch_a(symbol)


def run(codes=None, ctx=None, verbose=False):
    """采集入口（常驻调用）。

    codes:
      - None / 空列表 = 全量模式：通过 _list_all_codes()（港股+A股全部代码，本地7天缓存）
        逐只拉取财务指标并批量入库 financial_indicator。单只异常隔离，不中断整批。
        调度器全局任务（GLOBAL_TASKS 中的「财务指标全量」）即走此模式。
      - [单只/多只]，如 ["HK.00700"] / ["SH.600519","SZ.000001"] = 指定代码采集，
        兼容 wecom 手动触发 / 调试。代码可带或不带市场前缀（不带则按首位数字猜测）。

    本地缓存：全市场代码清单存于项目根 stock_list_cache.json，TTL 7 天；
    未过期直接复用，过期才重新拉 AKShare（hk_stock_list / stock_info_a_code_name）。
    ctx 未使用（数据源为 AKShare）。
    """
    if not codes:
        target_codes = _list_all_codes()
        if not target_codes:
            print("财务指标全量采集: 无法获取全市场代码清单，跳过")
            return
        print(f"财务指标全量采集: 共 {len(target_codes)} 只（港股+A股），开始逐只拉取...")

        total_rows = 0
        ok = 0
        fail = 0
        skip = 0          # 有数据但接口返回空（未入库）
        _t0 = datetime.now()
        _total = len(target_codes)
        for _i, full_code in enumerate(target_codes, 1):
            try:
                records = _fetch_one(full_code)
            except Exception as e:
                fail += 1
                print(f"[{_i}/{_total}] {full_code} 拉取失败: {type(e).__name__}: {e}",
                      file=sys.stderr)
                # 每 25 只或失败发生时也刷新一次进度，避免只看得到 stderr
                if _i % 25 == 0:
                    _elapsed = (datetime.now() - _t0).total_seconds()
                    print(f"财务指标全量采集进度: {_i}/{_total} (成功 {ok}/失败 {fail}/跳过 {skip}/已入库 {total_rows} 条, 用时 {_elapsed:.0f}s)")
                continue
            if not records:
                skip += 1
                print(f"[{_i}/{_total}] {full_code} 无财务数据(跳过)")
                continue
            db_data = [{"stock_code": full_code, **r} for r in records]
            try:
                with get_conn() as conn:
                    bulk_upsert(conn, "financial_indicator", db_data,
                                conflict_cols=["stock_code", "report_date"])
                total_rows += len(db_data)
                ok += 1
                print(f"[{_i}/{_total}] {full_code} 入库 {len(db_data)} 期 (累计成功 {ok}/失败 {fail}/跳过 {skip})")
            except Exception as e:
                fail += 1
                print(f"[{_i}/{_total}] {full_code} 数据库写入失败: {type(e).__name__}: {e}",
                      file=sys.stderr)
        _elapsed = (datetime.now() - _t0).total_seconds()
        print(f"财务指标全量采集完成: 成功 {ok} 只 / 失败 {fail} 只 / 跳过 {skip} 只，"
              f"入库 {total_rows} 条报告期，总用时 {_elapsed:.0f}s")
        return

    # 指定代码模式（单只或多只）
    for raw_code in codes:
        raw_code = raw_code.strip()
        full_code = raw_code if "." in raw_code else f"{_guess_market(raw_code)}.{raw_code}"
        print(f"[{full_code}] 正在获取财务指标...")
        try:
            records = _fetch_one(full_code)
        except Exception as e:
            print(f"[{full_code}] API 调用失败: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        if not records:
            print(f"[{full_code}] 未获取到财务数据")
            continue

        print(f"[{full_code}] 获取到 {len(records)} 个报告期:")
        for r in records:
            rd = r["report_date"]
            rt = r.get("report_type", "")
            rev = f"{(r['revenue']/1e8):.0f}亿" if r["revenue"] else "N/A"
            np_ = f"{(r['net_profit']/1e8):.0f}亿" if r["net_profit"] else "N/A"
            ocf = f"{(r['operating_cash_flow']/1e8):.0f}亿" if r['operating_cash_flow'] else "N/A"
            fcf = f"{(r['free_cash_flow']/1e8):.0f}亿" if r['free_cash_flow'] else "N/A"
            roe = f"{r['roe']:.1f}%" if r['roe'] is not None else "N/A"
            print(f"  {rd} [{rt}]  营收:{rev}  净利:{np_}  经营CF:{ocf}  自由CF:{fcf}  ROE:{roe}")

        if verbose:
            for r in records:
                print(f"\n  --- {r['report_date']} ---")
                for k, v in r.items():
                    print(f"    {k}: {v}")

        db_data = [{"stock_code": full_code, **r} for r in records]
        try:
            with get_conn() as conn:
                bulk_upsert(conn, "financial_indicator", db_data,
                            conflict_cols=["stock_code", "report_date"])
            print(f"[{full_code}] 已写入数据库 ({len(db_data)} 条)")
        except Exception as e:
            print(f"[{full_code}] 数据库写入失败: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="采集财务指标（港股+A股）。默认全市场全量采集；给定 code 才只采集该只。")
    parser.add_argument("code", nargs="?", default=None,
                        help="股票代码，如 HK.00700 / SH.600519。不传则默认全量采集港股+A股全部代码")
    parser.add_argument("--all", action="store_true",
                        help="显式全量模式：采集港股+A股全部代码（约8300只）。代码清单走本地7天缓存，"
                             "过期才重新拉 AKShare。等价于调度器全局任务。")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示详细输出")
    parser.add_argument("--announce-date", action="store_true",
                        help="回填 A 股财报披露日 announce_date（港股无数据源留空）；"
                             "配合 --all 仍走默认财务指标全量，本开关单独触发披露日回填")
    args = parser.parse_args()
    if args.announce_date:
        run_announce_date()                       # 回填 A 股披露日
    elif args.code and not args.all:
        run([args.code], verbose=args.verbose)   # 指定单只（调试/wecom）
    else:
        run(None, verbose=args.verbose)          # 默认全量模式
