#!/usr/bin/env python3
"""
探测：除富途外，能否从其他数据源批量拿到「总市值/流通市值/市盈率/市净率」。

验证候选源：
  1. 东方财富 push2 实时行情接口（A股 + 港股，单只可批量循环）
  2. AKShare 港股 spot（stock_hk_spot，含市值/PE 列？）
  3. AKShare A股 spot（stock_zh_a_spot，含市值/PE 列？）

只探测、不入库。输出每个源能否拿到目标字段、样例值、批量可行性。
"""
import sys
import time
import json
from datetime import date

# ---------------------------------------------------------------------------
# 候选 1：东方财富 push2 实时行情（A股 + 港股）
# ---------------------------------------------------------------------------
EM_PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}

# 东方财富 secid 编码：市场前缀 + 代码
#   A股沪市 1.SH600519 / 深市 0.SZ000001
#   港股      r_hk.00700  (注意港股用 r_hk 前缀，且代码不带 HK.)
_EM_SECID = {
    "SH.600519": "1.600519",
    "SZ.000001": "0.000001",
    "HK.00700": "116.00700",
    "HK.09660": "116.09660",
}
# push2 返回的字段名（f 参数）
_EM_FIELDS = "f57,f58,f43,f116,f117,f162,f167,f164,f168,f163"  # 代码,名称,最新价,总市值,流通市值,PE,PE_TTM,PB,股息率,总股本


def probe_em_push2(secid: str):
    params = {
        "secid": secid,
        "fields": _EM_FIELDS,
        "invt": "2",
        "fltt": "2",  # 数值格式化（亿/万自动）
    }
    try:
        r = __import__("requests").get(EM_PUSH2, params=params, headers=EM_HEADERS, timeout=15)
        r.raise_for_status()
        j = r.json()
        return j.get("data")
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def probe_em_batch():
    print("=" * 70)
    print("候选 1：东方财富 push2 实时行情（A股 + 港股）")
    print("=" * 70)
    for code, secid in _EM_SECID.items():
        t0 = time.time()
        d = probe_em_push2(secid)
        dt = time.time() - t0
        if not d or d.get("__error__"):
            print(f"  {code} ({secid}) ERR {(d or {}).get('__error__','空')} ({dt:.1f}s)")
            continue
        print(f"  {code} ({secid}) {dt:.1f}s")
        for k in ("f57", "f58", "f43", "f116", "f117", "f162", "f167", "f164", "f168", "f163"):
            print(f"      {k}={d.get(k)}")
    # 字段含义参考 push2 文档：
    # f116=总市值 f117=流通市值 f162=市盈率(静) f167=市盈率(TTM) f164=市净率
    # f168=股息率(TTM) f43=最新价 f58=名称 f57=代码


# ---------------------------------------------------------------------------
# 候选 2：AKShare 港股 spot（看列里是否有市值/PE）
# ---------------------------------------------------------------------------
def probe_ak_hk_spot():
    print()
    print("=" * 70)
    print("候选 2：AKShare stock_hk_spot（港股实时快照）")
    print("=" * 70)
    try:
        import akshare as ak
        df = ak.stock_hk_spot()
        cols = list(df.columns)
        print(f"  列({len(cols)}): {cols}")
        # 找含市值/PE 的列
        hit = [c for c in cols if any(k in str(c) for k in ("市值", "市盈", "市净", "PE", "PB", "MV", "总", "流通"))]
        print(f"  命中市值/PE 相关列: {hit}")
        if "代码" in cols and hit:
            sample = df.head(3)
            for _, row in sample.iterrows():
                print(f"    {row.get('代码')} {row.get('名称', '')} -> " +
                      ", ".join(f"{c}={row.get(c)}" for c in hit))
    except Exception as e:
        print(f"  ERR {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 候选 3：AKShare A股 spot（stock_zh_a_spot，含市值/PE 列？）
# ---------------------------------------------------------------------------
def probe_ak_a_spot():
    print()
    print("=" * 70)
    print("候选 3：AKShare stock_zh_a_spot（A股实时快照）")
    print("=" * 70)
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot()
        cols = list(df.columns)
        print(f"  列({len(cols)}): {cols}")
        hit = [c for c in cols if any(k in str(c) for k in ("市值", "市盈", "市净", "PE", "PB", "MV", "总", "流通"))]
        print(f"  命中市值/PE 相关列: {hit}")
        if hit:
            sample = df.head(3)
            for _, row in sample.iterrows():
                code = row.get("代码", row.get("symbol", ""))
                print(f"    {code} -> " + ", ".join(f"{c}={row.get(c)}" for c in hit))
    except Exception as e:
        print(f"  ERR {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 候选 4：东方财富 datacenter 港股估值（批量，历史PE/PB）
#   接口 RPT_VALUEANALYSIS_DET 等——这里只验证能否拿到港股 pe/pb/总市值
# ---------------------------------------------------------------------------
EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}


def probe_em_dc_hk(code: str):
    """港股估值快照：用 datacenter 的 RPT_HKF10_VALUATION 报告（东方财富）"""
    params = {
        "reportName": "RPT_HKF10_VALUATION",
        "columns": "ALL",
        "filter": f"(SECUCODE=\"{code}.HK\")",
        "pageSize": "5",
        "pageNumber": "1",
        "sortColumns": "REPORTDATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    try:
        r = __import__("requests").get(EM_DC, params=params, headers=DC_HEADERS, timeout=15)
        r.raise_for_status()
        res = r.json().get("result") or {}
        return res.get("data") or []
    except Exception as e:
        return [{"__error__": f"{type(e).__name__}: {e}"}]


def probe_em_dc_hk_batch():
    print()
    print("=" * 70)
    print("候选 4：东方财富 datacenter RPT_HKF10_VALUATION（港股估值，批量）")
    print("=" * 70)
    for code in ("HK.00700", "HK.09660"):
        d = probe_em_dc_hk(code.split(".")[1])
        if not d or d[0].get("__error__"):
            print(f"  {code} ERR {d[0].get('__error__','空') if d else '空'}")
            continue
        row = d[0]
        print(f"  {code} date={row.get('REPORTDATE')} "
              f"PE={row.get('PE')} PE_TTM={row.get('PETTM')} "
              f"PB={row.get('PB')} 总市值={row.get('TOTALMV')} 流通市值={row.get('CIRC_MV')}")


if __name__ == "__main__":
    probe_em_push2_secids = False
    probe_em_batch()
    probe_ak_hk_spot()
    probe_ak_a_spot()
    probe_em_dc_hk_batch()
    print()
    print("探测完成。")
