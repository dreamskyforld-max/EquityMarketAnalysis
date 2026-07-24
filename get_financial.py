#!/usr/bin/env python3
"""
财务指标采集 — AKShare API（常驻调用版）

数据源：
  - 港股：stock_financial_hk_analysis_indicator_em（东方财富港股财务分析指标）
  - A 股：stock_financial_abstract（新浪财经财务摘要）
覆盖年度+季度报告期。
常驻调用：run(codes, ctx)。__main__ 保留独立运行。
"""
import sys
from datetime import date
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


# ---------- 采集入口（常驻调用）----------

def run(codes=None, ctx=None, verbose=False):
    """codes: [股票代码]，如 HK.00700 / SH.600519；ctx 未使用（数据源为 AKShare）。"""
    raw_code = (codes[0] if (codes and len(codes) > 0) else "HK.00700").strip()
    if "." in raw_code:
        market, symbol = raw_code.split(".", 1)
        market = market.upper()
    else:
        market = _guess_market(raw_code)
        symbol = raw_code

    full_code = f"{market}.{symbol}"

    print(f"[{full_code}] 正在获取财务指标...")
    try:
        if market == "HK":
            records = fetch_hk(symbol)
        else:
            records = fetch_a(symbol)
    except Exception as e:
        print(f"[{full_code}] API 调用失败: {type(e).__name__}: {e}", file=sys.stderr)
        return

    if not records:
        print(f"[{full_code}] 未获取到财务数据")
        return

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

    db_data = []
    for r in records:
        row = {"stock_code": full_code}
        row.update(r)
        db_data.append(row)

    try:
        with get_conn() as conn:
            bulk_upsert(conn, "financial_indicator", db_data,
                        conflict_cols=["stock_code", "report_date"])
        print(f"[{full_code}] 已写入数据库 ({len(db_data)} 条)")
    except Exception as e:
        print(f"[{full_code}] 数据库写入失败: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="采集年度财务指标")
    parser.add_argument("code", nargs="?", default="HK.00700", help="股票代码，如 HK.00700 / SH.600519")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示详细输出")
    args = parser.parse_args()
    run([args.code], verbose=args.verbose)
