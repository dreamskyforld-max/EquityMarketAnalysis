#!/usr/bin/env python3
"""验证国际指数/宏观指标各数据源可用性。

逐个测试：富途 yfinance AKShare 对每个标的的获取能力。
输出：✅/⚠️/❌ + 最新数据日期 + 最新值 + 耗时 + 错误信息。
"""
import time
import sys
import traceback

# ── 工具函数 ──
def test_one(name, func):
    """运行一次测试，返回 (status, date, value, ms, err)"""
    t0 = time.time()
    try:
        result = func()
        ms = round((time.time() - t0) * 1000)
        dt, val = result if isinstance(result, tuple) else (None, result)
        ok = "✅" if dt is not None and val is not None else "⚠️"
        return ok, str(dt) if dt else "N/A", str(val) if val is not None else "N/A", f"{ms}ms", ""
    except Exception as e:
        ms = round((time.time() - t0) * 1000)
        return "❌", "N/A", "N/A", f"{ms}ms", f"{type(e).__name__}: {e}"

# ────────────────────────────────────────────────
# 1. 富途 — 恒生科技指数
# ────────────────────────────────────────────────
def test_futu_hstech():
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        ret, snap = ctx.get_market_snapshot(['HK.800700'])
        if ret != RET_OK:
            return None, str(snap)
        r = snap.iloc[0]
        return str(r.get('update_time', 'N/A'))[:10], float(r['last_price'])
    finally:
        ctx.close()

# 额外测试：也测一下已经接入的恒生指数确保富途正常
def test_futu_hsi():
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        ret, snap = ctx.get_market_snapshot(['HK.800000'])
        if ret != RET_OK:
            return None, str(snap)
        r = snap.iloc[0]
        return str(r.get('update_time', 'N/A'))[:10], float(r['last_price'])
    finally:
        ctx.close()

# ────────────────────────────────────────────────
# 2. yfinance — 批量下载
# ────────────────────────────────────────────────
YF_TICKERS = {
    "道琼斯 (^DJI)":           "^DJI",
    "纳斯达克 (^IXIC)":        "^IXIC",
    "标普500 (^GSPC)":         "^GSPC",
    "日经225 (^N225)":         "^N225",
    "韩国KOSPI (^KS11)":       "^KS11",
    "德国DAX (^GDAXI)":        "^GDAXI",
    "VIX恐慌 (^VIX)":          "^VIX",
    "美债10Y (^TNX)":          "^TNX",
    "美元指数 (DX-Y.NYB)":     "DX-Y.NYB",
    "离岸人民币 (CNH=X)":      "CNH=X",
}

def test_yfinance_all():
    import yfinance as yf
    results = {}
    tickers = list(YF_TICKERS.values())
    names = list(YF_TICKERS.keys())
    # 分批下载（yfinance 多 ticker 一起下更稳）
    for i, (name, ticker) in enumerate(zip(names, tickers)):
        try:
            tk = yf.Ticker(ticker)
            info = tk.info
            prev = info.get('regularMarketPreviousClose') or info.get('previousClose')
            price = info.get('regularMarketPrice') or info.get('currentPrice')
            dt = info.get('regularMarketTime')
            if dt:
                from datetime import datetime
                dt = datetime.fromtimestamp(dt).strftime('%Y-%m-%d')
            results[name] = (dt, float(price) if price else None, None)
        except Exception as e:
            results[name] = (None, None, f"{type(e).__name__}: {e}")
    return results

# ────────────────────────────────────────────────
# 3. AKShare — 美债收益率 + 离岸人民币
# ────────────────────────────────────────────────
def test_akshare():
    import akshare as ak
    tests = {}
    # 美债收益率
    for label, func_name in [("美债10Y收益率", "bond_zh_us_rate"), ("美债2Y收益率", "bond_zh_us_rate")]:
        try:
            fn = getattr(ak, func_name, None)
            if fn is None:
                tests[label] = (None, None, f"函数 {func_name} 不存在")
            else:
                df = fn()
                if df is None or df.empty:
                    tests[label] = (None, None, "返回空 DataFrame")
                else:
                    tests[label] = (str(df.iloc[-1, 0])[:10], float(df.iloc[-1, 1]), None)
        except Exception as e:
            tests[label] = (None, None, f"{type(e).__name__}: {str(e)[:80]}")
    # 离岸人民币
    for label, func_name in [("离岸人民币", "currency_boc_safe")]:
        try:
            fn = getattr(ak, func_name, None)
            if fn is None:
                tests[label] = (None, None, f"函数 {func_name} 不存在")
            else:
                df = fn()
                tests[label] = (str(df.iloc[-1, 0])[:10], float(df.iloc[-1, 1]), None)
        except Exception as e:
            tests[label] = (None, None, f"{type(e).__name__}: {str(e)[:80]}")
    return tests

# ────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────
def main():
    print("=" * 78)
    print("国际指数 / 宏观指标 数据源可用性验证")
    print("=" * 78)

    # ── 富途 ──
    print("\n── 1. 富途 OpenAPI ──")
    print(f"{'标的':<25} {'状态':<4} {'最新日期':<10} {'最新值':<15} {'耗时':<8} 错误")
    print("-" * 78)
    for name, fn in [("恒生指数 HK.800000", test_futu_hsi), ("恒生科技 HK.800700", test_futu_hstech)]:
        s, d, v, t, e = test_one(name, fn)
        print(f"{name:<25} {s:<4} {d:<10} {v:<15} {t:<8} {e}")

    # ── yfinance ──
    print("\n── 2. yfinance ──")
    print(f"{'标的':<25} {'状态':<4} {'最新日期':<12} {'最新值':<15} {'耗时':<8} 错误")
    print("-" * 78)
    yf_results = test_yfinance_all()
    for name in YF_TICKERS:
        res = yf_results.get(name)
        if res is None:
            print(f"{name:<25} {'❌':<4} {'N/A':<12} {'N/A':<15} {'N/A':<8} 无结果")
        elif res[2]:  # 有错误
            print(f"{name:<25} {'❌':<4} {'N/A':<12} {'N/A':<15} {'N/A':<8} {res[2]}")
        else:
            d, v, _ = res
            print(f"{name:<25} {'✅':<4} {str(d):<12} {str(v):<15} {'N/A':<8}")

    # ── AKShare ──
    print("\n── 3. AKShare ──")
    print(f"{'标的':<25} {'状态':<4} {'最新日期':<12} {'最新值':<15} {'耗时':<8} 错误")
    print("-" * 78)
    ak_results = test_akshare()
    for name, (d, v, e) in ak_results.items():
        s = "✅" if d and v else "❌"
        print(f"{name:<25} {s:<4} {str(d):<12} {str(v):<15} {'N/A':<8} {e or ''}")

    # ── 额外：探索 AKShare 可用函数 ──
    print("\n── AKShare 相关函数探测（列出可调用的 API 名）──")
    import akshare as ak
    candidates = [
        "bond_zh_us_rate", "bond_us_yield", "currency_boc_sina",
        "currency_boc_safe", "currency_boc_sina_df", "currency_pair_map",
        "macro_china_lpr", "macro_china_market_money_supply",
        "index_global_hist", "index_vix", "index_us_hist",
    ]
    for c in candidates:
        exists = "✅" if hasattr(ak, c) else "❌"
        print(f"  {exists} akshare.{c}")

    print("\n" + "=" * 78)
    print("验证完成。请根据 ✅/⚠️/❌ 决定每个指标的数据源。")
    print("=" * 78)


if __name__ == "__main__":
    main()
