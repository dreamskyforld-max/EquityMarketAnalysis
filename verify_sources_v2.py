#!/usr/bin/env python3
"""第二轮验证：修正 yfinance 限流 + 确认 AKShare 返回字段含义。"""
import time

# ── yfinance 批量下载（避免限流）──
print("=" * 60)
print("yfinance 批量下载（yf.download，一次请求）")
print("=" * 60)
import yfinance as yf
tickers = [
    ("道琼斯", "^DJI"), ("纳斯达克", "^IXIC"), ("标普500", "^GSPC"),
    ("日经225", "^N225"), ("KOSPI", "^KS11"), ("DAX", "^GDAXI"),
    ("VIX", "^VIX"), ("美债10Y", "^TNX"), ("美元指数", "DX-Y.NYB"),
    ("离岸人民币", "CNH=X"),
]
print("拉取 5 天历史...")
try:
    df = yf.download([t[1] for t in tickers], period="5d", group_by="ticker", progress=False)
    print(f"下载完成，columns: {list(df.columns)[:3]}...")
    print()
    for name, tk in tickers:
        try:
            if tk in df.columns.get_level_values(0):
                sub = df[tk].dropna()
                if len(sub) > 0:
                    last = sub.iloc[-1]
                    dt = str(sub.index[-1].date())
                    close = float(last['Close'])
                    print(f"  ✅ {name:<20} ({tk:<15})  {dt}  Close={close}")
                else:
                    print(f"  ❌ {name:<20} ({tk:<15})  无数据")
            else:
                print(f"  ❌ {name:<20} ({tk:<15})  不在返回列中")
        except Exception as e:
            print(f"  ❌ {name:<20} ({tk:<15})  {type(e).__name__}: {e}")
except Exception as e:
    print(f" ❌ 批量下载失败: {type(e).__name__}: {e}")

# ── AKShare 数据字段确认 ──
print()
print("=" * 60)
print("AKShare 数据字段确认")
print("=" * 60)
import akshare as ak

# bond_zh_us_rate — 美债收益率字段
print("\n── bond_zh_us_rate —— 列名 + 前3行 + 末3行 ──")
try:
    df = ak.bond_zh_us_rate()
    print(f"  columns: {list(df.columns)}")
    print(f"  shape: {df.shape}")
    print(f"  前3行:\n{df.head(3).to_string()}")
    print(f"  末3行:\n{df.tail(3).to_string()}")
except Exception as e:
    print(f"  ❌ {e}")

# currency_boc_safe — 离岸人民币字段
print("\n── currency_boc_safe —— 列名 + 前3行 + 末3行 ──")
try:
    df = ak.currency_boc_safe()
    print(f"  columns: {list(df.columns)}")
    print(f"  shape: {df.shape}")
    print(f"  前3行:\n{df.head(3).to_string()}")
    print(f"  末3行:\n{df.tail(3).to_string()}")
except Exception as e:
    print(f"  ❌ {e}")

# currency_boc_sina — 备选
print("\n── currency_boc_sina —— 列名 + 前3行 + 末3行 ──")
try:
    df = ak.currency_boc_sina(symbol="美元")
    print(f"  columns: {list(df.columns)}")
    print(f"  shape: {df.shape}")
    print(f"  前3行:\n{df.head(3).to_string()}")
    print(f"  末3行:\n{df.tail(3).to_string()}")
except Exception as e:
    print(f"  ❌ {e}")

# 尝试单 ticker yfinance 有限速保护
print()
print("=" * 60)
print("yfinance 单 ticker（加 1s 间隔，验证限流后恢复）")
print("=" * 60)
for name, tk in [("道琼斯", "^DJI"), ("VIX", "^VIX"), ("美债10Y", "^TNX"), ("美元指数", "DX-Y.NYB")]:
    time.sleep(1.5)  # 控制频率
    try:
        t = yf.Ticker(tk)
        h = t.history(period="5d")
        if len(h) > 0:
            last = h.iloc[-1]
            print(f"  ✅ {name} ({tk})  {str(h.index[-1].date())}  Close={float(last['Close'])}")
        else:
            print(f"  ⚠️ {name} ({tk})  返回空")
    except Exception as e:
        print(f"  ❌ {name} ({tk})  {type(e).__name__}: {e}")
