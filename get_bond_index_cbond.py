#!/usr/bin/env python3
"""中债指数采集（**债券资产的总回报序列**）—— 写入 regime.macro_series。

数据源：AKShare `bond_composite_index_cbond` / `bond_treasury_index_cbond`
      （中国债券信息网 · 中债指数族系，官方口径）
  · 中债-综合指数     → CN.BOND_INDEX          2002-01-03 起 · 6,200 点
  · 中债-国债指数     → CN.BOND_TREASURY_INDEX 2008-01-02 起 · 4,697 点
  · **一次调用返回全历史** → 全量刷新、幂等，漏跑一天不丢数据。

**为什么需要它**（market_profile §4.3.1 / §4.4.1 的三处硬缺口）：
  1. §4.3.1 的 `R₁ = w₁ × σ` 把非股票部分当无风险资产 → 需要债券自身的波动率 σ_b；
  2. §4.3.1 第七节点名的「完整风险平价不做」其根因就是**债券端无价格序列**；
  3. §4.4.1 的 60/40 对照基准缺债腿收益、全天候缺债腿风险贡献。
  → 一个序列同时解锁三处。

⚠ **这是「总回报（财富/全价）指数」，不是净价指数** —— 实测特征：
      中债综合  年化 +4.03% / 最大回撤 −3.55% / 年化波动 1.71%
      中债国债  年化 +4.16% / 最大回撤 −4.74% / 年化波动 1.86%
   净价指数应围绕 100 震荡、回撤深；此处持续增长、回撤极浅 → 属全价/财富指数。
   **而总回报正是我们要的**：要算的是"买债券实际赚到多少"，不是报价。
   （若确需净价口径，须另找源；本项目三处用途都不需要净价。）

⚠ **与收益率曲线（CN.BOND_10Y）不是同一东西**：曲线是**到期收益率**（%），本序列是
   **指数点位**。二者换算需久期假设，故不互换、不拼接；指标层分别登记。

PIT 约定：release_time = 观测日 **当日 18:00**(+08)。与 get_cn_bond 同源同约定
  （同为中国债券信息网 EOD 发布）。⚠ 若实测发现发布晚于 18:00，把
  RELEASE_LAG_DAYS 改 1 即可（常数已参数化，勿在散落处硬改）。

用法:
    python3 get_bond_index_cbond.py
"""
import argparse
import datetime
import sys

from db import get_conn
from regime_schema import (upsert_macro_series, seed_series_meta,
                           load_series_defs, apply_series_defs)

SOURCE = "akshare:chinabond_index"
RELEASE_LAG_DAYS = 0          # 当日 EOD 即得（见文首 PIT 约定）
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# akshare 函数名 → (series_code, 名称, market, freq, remark)
SPECS = [
    dict(fn="bond_composite_index_cbond", code="CN.BOND_INDEX",
         name="中债-综合指数(总回报)", market="CN", freq="day",
         remark="中债-综合指数，**总回报（全价/财富）口径**，非净价 —— 年化约 4.0%、"
                "年化波动 1.7%、最大回撤 −3.6%（实测）。覆盖全市场（国债+政金+信用）"
                "→ 代表「买入债券资产」的实际回报。用途：§4.3.1 的 σ_b 与组合波动率完整式、"
                "§4.4.1 的 60/40 债腿与全天候债腿。2002-01 起；一次取全历史无断档风险"),
    dict(fn="bond_treasury_index_cbond", code="CN.BOND_TREASURY_INDEX",
         name="中债-国债指数(总回报)", market="CN", freq="day",
         remark="中债-国债指数，**总回报口径**（年化约 4.2%、年化波动 1.9%）。"
                "与 CN.BOND_INDEX 的差别是**只含国债** → 无风险资产的总回报读数，"
                "可与 CN.BOND_10Y（到期收益率）对照看「carry 实现程度」。2008-01 起"),
]


def _specs() -> list:
    return [dict(series_code=s["code"], series_name=s["name"], unit="index",
                 freq=s["freq"], market=s["market"], source=SOURCE,
                 lag_days=RELEASE_LAG_DAYS,
                 collect_params={"provider": "chinabond", "fn": s["fn"],
                                 "kind": "total_return"},
                 remark=s["remark"])
            for s in SPECS]


def _defs() -> dict:
    """播种序列契约 → 读回「库里生效的定义」（库里的值可覆盖代码）。"""
    specs = _specs()
    with get_conn() as conn:
        seed_series_meta(conn, specs)
        return load_series_defs(conn, codes=[s["series_code"] for s in specs])


def _rows_for(df, code: str, freq: str, market: str) -> list:
    """源 DataFrame（date/value）→ macro_series 行（过滤 NaN）。"""
    import pandas as pd

    out = []
    for _, r in df.iterrows():
        d = pd.to_datetime(r["date"], errors="coerce")
        v = pd.to_numeric(r["value"], errors="coerce")
        if pd.isna(d) or pd.isna(v):
            continue
        dd = d.date()
        rel = datetime.datetime.combine(
            dd + datetime.timedelta(days=RELEASE_LAG_DAYS), datetime.time(18, 0), tzinfo=TZ_CN)
        out.append({
            "series_code": code, "period_date": dd, "value": round(float(v), 4),
            "freq": freq, "market": market,
            "release_time": rel, "revision": 0,
            # 显式刷新 updated_at：ON CONFLICT DO UPDATE 不会重放列默认值，
            # 监控（monitor_table_config.time_column=updated_at）依赖它反映「采集在跑」。
            "updated_at": datetime.datetime.now(TZ_CN),
        })
    return out


def _fetch() -> list:
    """拉取中债综合/国债指数（一次返回全历史）。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    rows = []
    for s in SPECS:
        try:
            df = getattr(ak, s["fn"])()
        except Exception as e:
            print(f"    ⚠️ {s['code']} 拉取失败: {type(e).__name__}: {str(e)[:70]}")
            continue
        if df is None or not len(df):
            print(f"    ⚠️ {s['code']}: 源返回空")
            continue
        n = _rows_for(df, s["code"], s["freq"], s["market"])
        rows += n
        print(f"    · {s['code']:24s} {len(n):>5} 条  "
              f"{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
    return rows


def _save(rows: list, defs: dict = None) -> int:
    if not rows:
        return 0
    apply_series_defs(rows, defs if defs is not None else _defs())
    with get_conn() as conn:
        upsert_macro_series(conn, rows)
    return len(rows)


def run(codes=None, ctx=None) -> int:
    """采集入口：全量刷新（源一次返回全历史，幂等；漏跑一天不丢数据）。"""
    defs = _defs()
    rows = _fetch()
    print(f"  ✅ 中债指数: 写入 {_save(rows, defs)} 条")
    return len(rows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    print("=" * 60)
    print("中债指数采集（债券总回报序列）→ regime.macro_series")
    print("=" * 60)
    run()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT series_code, count(*), min(period_date), max(period_date)
                           FROM regime.macro_series
                           WHERE series_code LIKE 'CN.BOND_INDEX%'
                              OR series_code='CN.BOND_TREASURY_INDEX'
                           GROUP BY 1 ORDER BY 1""")
            print("\n── regime.macro_series（中债指数）──")
            for r in cur.fetchall():
                print(f"  {r[0]:24s} {r[1]:>5} 行   {r[2]} ~ {r[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
