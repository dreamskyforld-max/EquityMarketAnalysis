#!/usr/bin/env python3
"""
探测结论（2026-09-04）：分红明细数据源已验证可用。

  1. A股：东方财富 datacenter RPT_SHAREBONUS_DET
     - PRETAX_BONUS_RMB = 每10股税前派息（元），/10 得每股
     - EX_DIVIDEND_DATE 除净日 / ASSIGN_PROGRESS=实施分配（可过滤实施口径）
     - 附带 BASIC_EPS / TOTAL_SHARES → 单表即可算分红率
  2. 港股：同花顺 F10（akshare stock_hk_fhpx_detail_ths）
     - HTML 表格：公告日期/方案("每股4.5港元"或"不分红")/除净日/派息日/进度("实施完成")
     - 含"不分红"预案行 → 连续分红年数可直接从序列判定
     - 方案文本需解析出每股派息（港元）

本脚本保留两个已验证源的直连调用，供回填脚本开发前复核。
"""
import akshare as ak
import requests

EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}


def probe_a_share(code: str = "600900"):
    params = {
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageSize": "3",
        "pageNumber": "1",
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    r = requests.get(EM_DC, params=params, headers=DC_HEADERS, timeout=15)
    rows = (r.json().get("result") or {}).get("data") or []
    print(f"A股 RPT_SHAREBONUS_DET {code}: {len(rows)} 行")
    for row in rows[:2]:
        print(f"  {row['EX_DIVIDEND_DATE'][:10]} 每10股派{row['PRETAX_BONUS_RMB']}元 "
              f"EPS={row.get('BASIC_EPS')} 进度={row.get('ASSIGN_PROGRESS')} "
              f"报告期={row.get('REPORT_DATE','')[:10]}")


def probe_hk_ths(symbol: str = "0700"):
    df = ak.stock_hk_fhpx_detail_ths(symbol=symbol)
    print(f"港股 同花顺F10 HK{symbol}: {len(df)} 行")
    ok = df[df["除净日"].notna()].tail(3)
    for _, row in ok.iterrows():
        print(f"  除净{row['除净日']} 方案={row['方案']} 进度={row['进度']} 类型={row['类型']}")


if __name__ == "__main__":
    probe_a_share()
    print()
    probe_hk_ths()
