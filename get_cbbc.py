#!/usr/bin/env python3
"""
牛熊证（CBBC）街货分布采集（全量版 + 常驻调用）

数据源：港交所 CBBC 完整列表 CSV（sc.hkex.com.hk）
写入表：daily_cbbc（按 stock_code + trade_date 去重）

采集方式：
- 一次下载整份 CBBC 完整列表，解析全部有牛熊证的标的（UL），
  每只标的取街货量最大的牛证/熊证各一只入库 daily_cbbc。
- run(codes, ctx)：codes 为 None/空 = 全量入库全部标的（全局任务用法）；
  codes 给定 = 仅按代码过滤入库（兼容 wecom 手动单票触发）。
- __main__ 保留独立运行。

注意：仅适用于港股，A股无牛熊证。
"""
import sys, csv, urllib.request
from collections import defaultdict
from datetime import date
from db import get_conn, upsert
from collector_runtime import get_shared_ctx

# 港交所 CBBC 完整列表 CSV 下载地址
CSV_URL = "https://sc.hkex.com.hk/TuniS/www.hkex.com.hk/eng/cbbc/search/cbbcFullList.csv"


def _norm_symbol(sym):
    """把带前导零的代码（00700）归一化为无前导零格式（700）；非数字返回原值。"""
    try:
        return str(int(sym))
    except (ValueError, TypeError):
        return sym


def parse_cbbc_full(text):
    """解析港交所 CBBC 完整列表文本，返回 {归一化代码: {...}}。

    每只标的取街货量最大的牛证/熊证各一只；跳过 O/S(%)<=0 或无街货的行。
    返回字段：bull_call_level / bull_street_volume / bear_call_level / bear_street_volume
    （无牛或无熊时对应字段为 None）。
    """
    lines = text.splitlines()
    reader = csv.DictReader(lines[1:], delimiter='\t')
    reader.fieldnames = [f.strip() for f in reader.fieldnames]

    bulls = defaultdict(list)   # code -> [(call_level, street_vol), ...]
    bears = defaultdict(list)

    for row in reader:
        ul = (row.get("UL") or "").strip()
        if not ul:
            continue
        code = _norm_symbol(ul)
        if not code.isdigit():
            continue  # 仅支持数字代码正股/ETF
        try:
            os_pct = float(row.get("O/S (%)", "0") or "0")
        except ValueError:
            continue
        if os_pct <= 0:
            continue
        try:
            total = int(row.get("Total Issue Size", "0").replace(",", "") or "0")
        except ValueError:
            continue
        street_vol = int(total * os_pct / 100)
        if street_vol <= 0:
            continue
        try:
            call_level = float(row.get("Call Level", 0) or 0)
        except (ValueError, TypeError):
            continue
        bear_bull = (row.get("Bull/Bear") or "").strip()
        entry = (call_level, street_vol)
        if bear_bull.startswith("Bull"):
            bulls[code].append(entry)
        elif bear_bull.startswith("Bear"):
            bears[code].append(entry)

    result = {}
    for code in sorted(set(bulls) | set(bears)):
        top_bull = max(bulls.get(code, []), key=lambda x: x[1], default=None)
        top_bear = max(bears.get(code, []), key=lambda x: x[1], default=None)
        result[code] = {
            "bull_call_level": top_bull[0] if top_bull else None,
            "bull_street_volume": top_bull[1] if top_bull else None,
            "bear_call_level": top_bear[0] if top_bear else None,
            "bear_street_volume": top_bear[1] if top_bear else None,
        }
    return result


def get_cbbc():
    """下载并解析港交所 CBBC 完整列表，返回 {归一化代码: {...}}；失败返回 None。"""
    try:
        req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        print(f"下载失败: {e}")
        return None

    for enc in ['utf-16', 'utf-8', 'latin-1']:
        try:
            decoded = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        print("编码失败")
        return None

    return parse_cbbc_full(decoded)


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。

    codes: 股票代码列表；None/空 = 全量采集全部有牛熊证的标的并入库；
           给定 = 仅按代码过滤入库（兼容 wecom 手动单票触发）。
    ctx 未使用（数据源为港交所）。
    """
    data = get_cbbc()
    if not data:
        print("牛熊证街货分布: 获取失败或无数据")
        return

    today = date.today()

    # 过滤：codes 给定则只保留这些票（兼容 wecom 手动单票触发）
    want = {_norm_symbol(c.partition(".")[2]) for c in (codes or [])
            if c.partition(".")[0].upper() == "HK"}
    if want:
        data = {k: v for k, v in data.items() if k in want}
        if not data:
            print("牛熊证街货分布: 无匹配股票")
            return

    # 全量入库
    inserted = 0
    failed = 0
    with get_conn() as conn:
        for code, v in sorted(data.items()):
            full_code = f"HK.{int(code):05d}"
            try:
                upsert(conn, "daily_cbbc", {
                    "stock_code": full_code,
                    "trade_date": today,
                    "bull_call_level": v["bull_call_level"],
                    "bull_street_volume": v["bull_street_volume"],
                    "bear_call_level": v["bear_call_level"],
                    "bear_street_volume": v["bear_street_volume"],
                }, conflict_cols=["stock_code", "trade_date"])
                inserted += 1
            except Exception as e:
                failed += 1
                print(f"[DB] 牛熊证入库失败 {full_code}: {e}")

    # 输出：单票保持原格式（兼容 wecom 渲染），全量输出汇总
    if len(data) == 1:
        code, v = sorted(data.items())[0]
        full_code = f"HK.{int(code):05d}"
        print(f"牛熊证街货分布 ({full_code})")
        if v["bull_call_level"]:
            print(f"牛证回收价: {v['bull_call_level']:.2f} 港元")
        else:
            print("牛证回收价: N/A")
        if v["bull_street_volume"]:
            print(f"牛证街货量(张): {v['bull_street_volume']:,}")
        else:
            print("牛证街货量(张): N/A")
        if v["bear_call_level"]:
            print(f"熊证回收价: {v['bear_call_level']:.2f} 港元")
        else:
            print("熊证回收价: N/A")
        if v["bear_street_volume"]:
            print(f"熊证街货量(张): {v['bear_street_volume']:,}")
        else:
            print("熊证街货量(张): N/A")
    else:
        n_bull = sum(1 for v in data.values() if v["bull_street_volume"])
        n_bear = sum(1 for v in data.values() if v["bear_street_volume"])
        print(f"牛熊证街货分布: 全市场入库 {inserted}/{len(data)} 只标的"
              f"（失败 {failed}），其中牛证 {n_bull} 只、熊证 {n_bear} 只")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("code", nargs="?", default=None,
                        help="股票代码（如 HK.00700）；不填 = 全量采集全部标的")
    args = parser.parse_args()
    run([args.code] if args.code else None)
