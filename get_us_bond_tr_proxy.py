#!/usr/bin/env python3
"""美债总回报**代理指数**（HK 侧债腿）—— 写入 regime.macro_series。

**为什么需要它**：market_profile §4.4.1 的 HK 资产集缺债腿——恒指之外要有"美债总回报"
  才能算 HK 的股债相关性矩阵（§4.3.1 的 HK 风险预算同样需要 σ_bond）。

**为什么是"代理"而不是真实指数**：真实的美债总回报指数（ICE BofA US Treasury /
  US Corporate Total Return Index）在 FRED 上，而 **FRED 当前停机维护**
  （2026-10-22 实测：api.stlouisfed.org 返回 Site Maintenance 页面，
  连带 pandas_datareader 全部报 ParserError）；yfinance 备选源被限流
  （YFRateLimitError）。故先用久期近似顶上，**待 FRED 恢复后必须替换为真实指数**。

**构造方法**（对 `US.DGS10` 做久期近似）：

    日总回报 r_t = (−D × Δy_t + y_t / 252) / 100
        D = 8.5   （10Y 国债修正久期近似，恒定、不含 roll-down）
        Δy_t      = 10Y 收益率日变化（pp）
        y_t/252   = 当日 carry（票息）
    指数点位 P_t = 100 × Π(1 + r_s)，起点 = US.DGS10 首个交易日

**已知偏差（必须知道再用）**：

| 偏差 | 说明 |
|---|---|
| **波动率高估约 30%** | 实测年化波动 **7.79%**，真实 10Y 国债指数约 5~6% —— 恒定久期不含 roll-down（收益率上行时久期会缩短） |
| **回撤偏深** | 实测最大回撤 −26.88%；真实指数因 roll-down 会浅一些 |
| 回报基本正确 | 实测年化 **4.83%**，与 DGS10 历史均值相当（carry 主导，偏差相互抵消） |
| 对 D 敏感 | D 从 7.0 到 9.5，年化波动从 6.42% 到 8.71% |

→ **结论：只可用于「相关性」口径（尺度无关，代理的收益序列形状正确），
   不可用于「波动率」口径（高估约 30%，会让 §4.3.1 过度压缩仓位）。**
   故本序列**不接入任何 VOL/波动率指标**，只作水平值供前端算相关性。

**替代方案备忘**（FRED 恢复后）：
    FRED `BAMLCC0A0CMTRIV`（ICE BofA US Corporate Index TR，1996-12 起）
    或 `BAMLC0A0CMTRIV`（US Corporate Master TR）—— 都是投资级公司债口径；
    若要纯国债口径需另找源。接上后把 `US.BOND_TR_PROXY` 的来源改为真实指数，
    并重算 HK 侧依赖它的指标。

PIT 约定：release_time = 观测日 **+1 天** 06:00(+08)。DGS10 是美股口径（T 日收盘
  在北京时间 T+1 上午才可得），与 `get_macro_daily.US_BENCH_LAG_DAYS` 同约定。

用法:
    python3 get_us_bond_tr_proxy.py
"""
import argparse
import datetime
import sys

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

SOURCE = "derived:get_us_bond_tr_proxy"
SERIES = "US.BOND_TR_PROXY"
DURATION = 8.5                  # 10Y 国债修正久期近似（恒定）
US_BENCH_LAG_DAYS = 1           # 与 get_macro_daily 的约定一致（美股 T+1 上午才可得）
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

REMARK = (
    "美债总回报**代理指数**（非真实指数）：对 US.DGS10 做久期近似构造 —— "
    "日回报 = (−8.5×Δy + y/252)/100，指数 base 100。"
    "**为什么是代理**：真实的美债总回报指数在 FRED（ICE BofA），而 FRED 当前停机维护、"
    "yfinance 被限流 → 先用久期近似顶上，待源恢复后必须替换。"
    "**已知偏差**：年化波动 7.79%（真实约 5~6%，恒定久期不含 roll-down → **高估约 30%**）、"
    "最大回撤 −26.88%（偏深）、年化回报 4.83%（基本正确，carry 主导）、对久期假设敏感"
    "（D 7→9.5 使波动 6.4%→8.7%）。"
    "⚠ **只可用于「相关性」口径**（尺度无关、收益序列形状正确）；"
    "**不可用于「波动率」口径**（高估约 30%，会让 §4.3.1 过度压缩仓位）→ 故不接入任何 VOL 指标。"
    "用途：HK 侧股债相关性矩阵的债腿（§4.4.1 3.1）。数据：1990-12 起（受 US.DGS10 限制）"
)


def _specs() -> list:
    return [dict(series_code=SERIES, series_name="美债总回报代理指数(久期近似)",
                 unit="index", freq="day", market="US", source=SOURCE,
                 lag_days=US_BENCH_LAG_DAYS,
                 collect_params={"kind": "derived", "method": "duration_approx",
                                 "source_series": "US.DGS10", "duration": DURATION,
                                 "base": 100},
                 remark=REMARK)]


def _defs() -> dict:
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _rows() -> list:
    """从 daily_benchmark.US.DGS10 构造代理指数 → macro_series 行。"""
    import pandas as pd

    with get_conn() as conn:
        df = pd.read_sql_query(
            """SELECT trade_date, last_price FROM daily_benchmark
               WHERE bench_code = 'US.DGS10' ORDER BY trade_date""", conn)
    if df.empty:
        return []
    s = pd.Series(pd.to_numeric(df["last_price"], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df["trade_date"])).dropna().sort_index()
    dy = s.diff()
    ret = (-DURATION * dy + s / 252) / 100          # 日总回报（小数）
    ret = ret.iloc[1:]                              # 首日无 Δy
    level = 100.0 * (1.0 + ret).cumprod()

    out = []
    for d, v in level.items():
        dd = d.date()
        rel = datetime.datetime.combine(
            dd + datetime.timedelta(days=US_BENCH_LAG_DAYS),
            datetime.time(6, 0), tzinfo=TZ_CN)
        out.append({
            "series_code": SERIES, "period_date": dd, "value": round(float(v), 4),
            "freq": "day", "market": "US", "release_time": rel, "revision": 0,
            "updated_at": datetime.datetime.now(TZ_CN),
        })
    return out


def _save(rows: list, defs: dict = None) -> int:
    if not rows:
        return 0
    apply_series_defs(rows, defs if defs is not None else _defs())
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
    return len(rows)


def run(codes=None, ctx=None) -> int:
    defs = _defs()
    rows = _rows()
    print(f"  ✅ 美债总回报代理指数: 写入 {_save(rows, defs)} 条")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    print("=" * 60)
    print("美债总回报代理指数（久期近似，HK 侧债腿）→ regime.macro_series")
    print("=" * 60)
    run()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*), min(period_date), max(period_date),
                                  round(max(value)::numeric,2)
                           FROM regime.macro_series WHERE series_code = %s""", [SERIES])
            r = cur.fetchone()
            print(f"\n── regime.macro_series（{SERIES}）──")
            print(f"  {r[0]} 行   {r[1]} ~ {r[2]}   最新点位 {r[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
