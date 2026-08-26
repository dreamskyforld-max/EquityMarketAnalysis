#!/usr/bin/env python3
"""东财融资融券接口全量稳定性批量测试（不入库，仅统计）"""
import requests, time, json
from datetime import date, timedelta

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}


def fetch_day(d: date):
    """拉取某交易日全市场两融明细，自动翻页直到空页。返回 list[dict]。"""
    ds = d.strftime("%Y-%m-%d")
    out = []
    page = 1
    while True:
        params = {
            "reportName": "RPTA_WEB_RZRQ_GGMX",
            "columns": "ALL",
            "filter": f"(DATE='{ds}')",
            "pageSize": "500",
            "pageNumber": str(page),
            "sortColumns": "DATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        }
        r = requests.get(URL, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        res = r.json().get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        out.extend(data)
        page += 1
        time.sleep(0.05)
    return out


def main():
    end = date(2026, 8, 25)
    days = []
    d = end
    while len(days) < 20:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.sort()

    print(f"测试 {len(days)} 个交易日全量，从 {days[0]} 到 {days[-1]}")
    ok = 0
    total_rows = 0
    sh_rqmiss = []
    sz_nomiss = []
    for d in days:
        t0 = time.time()
        try:
            rows = fetch_day(d)
            dt = time.time() - t0
            if rows:
                ok += 1
                total_rows += len(rows)
                sh = [r for r in rows if str(r.get("SCODE", "")).startswith("6")]
                sh_rq = sum(1 for r in sh if r.get("RQYE") not in (None, "", 0))
                sh_rqmiss.append(len(sh) - sh_rq)
                print(f"  {d} OK  {len(rows):>5}只 {dt:5.1f}s  沪市RQYE缺失 {len(sh)-sh_rq}/{len(sh)}")
            else:
                print(f"  {d} 空数据(可能休市)")
        except Exception as e:
            dt = time.time() - t0
            print(f"  {d} ERR {type(e).__name__}: {str(e)[:60]} ({dt:.1f}s)")

    print(f"\n成功率: {ok}/{len(days)}  总样本行: {total_rows}")
    if sh_rqmiss:
        print(f"沪市RQYE日均缺失: {sum(sh_rqmiss)/len(sh_rqmiss):.1f} 只 (样本日均)")


if __name__ == "__main__":
    main()
