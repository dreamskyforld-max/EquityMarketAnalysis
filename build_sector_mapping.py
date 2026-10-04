#!/usr/bin/env python3
"""同花顺行业 → 申万一级 分类映射（**数据驱动**，写入 regime.sector_mapping）。

为什么需要映射：
    行业资金流源用的是**同花顺细分行业（90 个）**，而行业指数/动量/相对强度用的是
    **申万一级（31 个）** —— 两套分类不能按名称模糊匹配（「半导体」「元件」「光学光电子」
    都属申万「电子」），必须先建映射才能做「逐行业 RS × 资金流」。

映射方法（客观、可复现、可复核）：
    两组行业指数在同一批个股上的成分重叠，会体现为日收益高度相关。但**直接用原始收益
    会失败**——所有 A 股行业都受共同市场因子支配（实测 raw corr 普遍 0.6~0.95，
    物流被判给纺织服饰、白色家电被判给轻工制造）。故必须先剔除市场因子：
      · 市场代理 = 31 个申万一级行业的**等权平均**（同一代理同时剔两边，保证可比）
      · 相关性用**超额收益**（行业日收益 − 市场代理）计算 → 反映「相对市场同涨同跌」的成分共振
    取超额收益相关性最高者为映射，并记录次优（判别裕度 = corr − runner_up）：
    裕度小 = 分类本身歧义（跨行业主题），需人工 confirmed 后不再被自动覆盖。

**实测结论（决定了两层设计的分工）**：
  1. 剔除市场因子后，残余相关主要反映**风格因子**（成长/价值）——
     实测「塑料制品/消费电子/金属新材料」都会被高相关地拉向**机械设备**（成长风格共振），
     故相关性**不能作为映射依据**，只能作审计线索；
  2. 两套指数**编制方法不同**（权重/成分口径）：实测 THS 白色家电与申万家用电器
     一年累计收益差 **46 个百分点**（+40.4% vs −6.1%）、超额相关性甚至为负，
     但同日原始相关性最高（0.64，错位后仅 0.02）证明**不存在数据错位**，纯属编制差异。
  → 因此：**RULES（行业公认对应）为主、相关性仅作交叉校验/异常提示**（corr_suggests / low_corr_with_rule）。

用法:
    python3 build_sector_mapping.py                    # 全量重算（90 个同花顺行业）
    python3 build_sector_mapping.py --days 250         # 相关性窗口（默认 250 交易日）
    python3 build_sector_mapping.py --dry-run          # 只算不写
    python3 build_sector_mapping.py --report           # 只打印当前映射表
    python3 build_sector_mapping.py --only 半导体       # 单个行业

调度（market_scheduler.GLOBAL_TASKS「行业映射重算」每月 1 日 08:20）：
    分类会随指数公司调整而变，定期用最新相关性复核；confirmed=true 的行不会被覆盖。
"""
import argparse
import datetime
import sys
import time

import numpy as np
import pandas as pd

from db import get_conn, bulk_upsert

TABLE = "regime.sector_mapping"
CONFLICT = ["ths_name"]
SOURCE = "akshare:corr_return"
MIN_OVERLAP = 60          # 相关系数所需最少重叠交易日
LOW_CONF_CORR = 0.6       # 低于此相关系数 → 标 low_conf 提示人工确认
AMBIGUOUS_MARGIN = 0.05   # 与次优的裕度低于此值 → 标 ambiguous

# 分类对照表（同花顺行业 → 申万一级代码）：**行业公认对应**，逐条可审计。
# 为什么需要它：同花顺细分行业（90 个）多数是申万一级的细分/子集，名称不重合时
# 纯统计方法会出错（实测：物流→纺织服饰、白色家电→轻工制造、橡胶制品→轻工制造）。
# 规则为主（confirmed=true）、相关性为**交叉校验**：冲突时把 corr 的建议记进 remark 供复核。
RULES = {
    # —— 农林牧渔 ——
    "农产品加工": "801010", "养殖业": "801010", "种植业与林业": "801010",
    # —— 基础化工 ——
    "化学原料": "801030", "化学制品": "801030", "化学纤维": "801030",
    "农化制品": "801030", "橡胶制品": "801030", "塑料制品": "801030",
    "非金属材料": "801030",
    # —— 钢铁 / 有色金属 ——
    "钢铁": "801040",
    "工业金属": "801050", "贵金属": "801050", "小金属": "801050",
    "能源金属": "801050", "金属新材料": "801050",
    # —— 电子 ——
    "半导体": "801080", "元件": "801080", "光学光电子": "801080",
    "消费电子": "801080", "电子化学品": "801080", "其他电子": "801080",
    # —— 家用电器 ——
    "白色家电": "801110", "黑色家电": "801110", "厨卫电器": "801110", "小家电": "801110",
    # —— 食品饮料 ——
    "白酒": "801120", "饮料制造": "801120", "食品加工制造": "801120",
    # —— 纺织服饰 / 轻工制造 ——
    "纺织制造": "801130", "服装家纺": "801130",
    "家居用品": "801140", "造纸": "801140", "包装印刷": "801140",
    # —— 医药生物 ——
    "化学制药": "801150", "中药": "801150", "生物制品": "801150",
    "医药商业": "801150", "医疗器械": "801150", "医疗服务": "801150",
    # —— 公用事业 / 交通运输 ——
    "电力": "801160", "燃气": "801160",
    "物流": "801170", "港口航运": "801170", "公路铁路运输": "801170", "机场航运": "801170",
    # —— 房地产 / 商贸零售 / 社会服务 / 综合 ——
    "房地产": "801180",
    "零售": "801200", "贸易": "801200", "互联网电商": "801200",
    "旅游及酒店": "801210", "教育": "801210", "其他社会服务": "801210",
    "综合": "801230",
    # —— 建材 / 建筑装饰 ——
    "建筑材料": "801710", "建筑装饰": "801720",
    # —— 电力设备 / 国防军工 ——
    "电池": "801730", "电机": "801730", "电网设备": "801730",
    "光伏设备": "801730", "风电设备": "801730", "其他电源设备": "801730",
    "军工电子": "801740", "军工装备": "801740",
    # —— 计算机 / 传媒 / 通信 ——
    "软件开发": "801750", "IT服务": "801750", "计算机设备": "801750",
    "游戏": "801760", "文化传媒": "801760", "影视院线": "801760",
    "通信服务": "801770", "通信设备": "801770",
    # —— 银行 / 非银金融 ——
    "银行": "801780",
    "证券": "801790", "保险": "801790", "多元金融": "801790",
    # —— 汽车 / 机械设备 ——
    "汽车整车": "801880", "汽车零部件": "801880", "汽车服务及其他": "801880",
    "通用设备": "801890", "专用设备": "801890", "工程机械": "801890",
    "自动化设备": "801890", "轨交设备": "801890",
    # —— 煤炭 / 石油石化 / 环保 / 美容护理 ——
    "煤炭开采加工": "801950",
    "石油加工贸易": "801960", "油气开采及服务": "801960",
    "环保设备": "801970", "环境治理": "801970",
    "美容护理": "801980",
}


def _sw_returns(conn, days: int) -> pd.DataFrame:
    """申万一级行业日收益（宽表：index=日期, columns=sw_code）。"""
    df = pd.read_sql_query(
        """SELECT bench_code, trade_date, last_price FROM daily_benchmark
           WHERE bench_code LIKE 'CN.SW%%' ORDER BY trade_date""", conn)
    if df.empty:
        return pd.DataFrame()
    piv = df.pivot_table(index="trade_date", columns="bench_code", values="last_price")
    piv.index = pd.DatetimeIndex(piv.index)
    piv = piv.sort_index().tail(days + 1)
    return (piv / piv.shift(1) - 1).dropna(how="all")


def _ths_returns(industries: list, start: str, end: str) -> dict:
    """同花顺行业日收益（逐行业请求；源无批量接口）。返回 {行业名: Series}。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    out = {}
    for i, (name, code) in enumerate(industries, 1):
        try:
            df = ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)
            if df is None or len(df) < MIN_OVERLAP:
                print(f"  ⚠️ [{i}/{len(industries)}] {name}: 数据不足（{0 if df is None else len(df)} 行）")
                continue
            s = pd.Series(
                pd.to_numeric(df["收盘价"], errors="coerce").to_numpy(),
                index=pd.DatetimeIndex(pd.to_datetime(df["日期"])))
            close = s.sort_index()
            out[name] = (close / close.shift(1) - 1).dropna()
            print(f"  · [{i}/{len(industries)}] {name}（{code}）: {len(out[name])} 个交易日")
        except Exception as e:
            print(f"  ❌ [{i}/{len(industries)}] {name}: {type(e).__name__}: {str(e)[:80]}")
        time.sleep(0.2)
    return out


def build(days: int = 250, only: str = None, dry_run: bool = False):
    """重算映射：同花顺行业 → 相关系数最高的申万一级行业。"""
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    with get_conn() as conn:
        sw_ret = _sw_returns(conn, days)
        if sw_ret.empty:
            print("  ❌ 库内无申万行业指数（daily_benchmark CN.SW*），先跑 backfill_sw_industry.py")
            return 0
        print(f"  申万一级收益矩阵: {sw_ret.shape[0]} 日 × {sw_ret.shape[1]} 行业")
        # 已人工确认的行：重算时跳过（人工判断优先于自动结果）
        cur = conn.cursor()
        cur.execute("SELECT ths_name, sw_code FROM regime.sector_mapping WHERE confirmed=true")
        locked = dict(cur.fetchall())
        if locked:
            print(f"  已人工确认 {len(locked)} 行（本次不覆盖）")

        names = ak.stock_board_industry_name_ths()
        industries = [(str(r["name"]), str(r["code"])) for _, r in names.iterrows()]
        if only:
            industries = [x for x in industries if x[0] == only]
        print(f"  同花顺行业: {len(industries)} 个（相关性窗口 {days} 交易日）")

        start = (sw_ret.index.min() - datetime.timedelta(days=5)).strftime("%Y%m%d")
        end = sw_ret.index.max().strftime("%Y%m%d")
        ths_ret = _ths_returns(industries, start, end)

        # 市场代理：31 个申万一级行业的等权平均（同一天同一值，两边共享 → 可比）
        market = sw_ret.mean(axis=1)
        sw_ex = sw_ret.sub(market, axis=0)          # 申万各行业超额收益
        rows, skipped = [], 0
        for name, code in industries:
            if name in locked:
                skipped += 1
                continue
            r = ths_ret.get(name)
            if r is None or r.dropna().empty:
                continue
            joined = pd.concat([r.rename("ths"), market.rename("mkt")], axis=1, join="inner").dropna()
            if len(joined) < MIN_OVERLAP:
                print(f"  ⚠️ {name}: 重叠交易日仅 {len(joined)} < {MIN_OVERLAP}，跳过")
                continue
            ths_ex = (joined["ths"] - joined["mkt"]).rename("ths")     # 同一市场代理
            both = pd.concat([ths_ex, sw_ex.reindex(joined.index)], axis=1).dropna()
            if len(both) < MIN_OVERLAP:
                print(f"  ⚠️ {name}: 超额收益重叠仅 {len(both)} < {MIN_OVERLAP}，跳过")
                continue
            corrs = both.corr()["ths"].drop("ths").sort_values(ascending=False)
            # 原始收益相关性（仅供审计：高 raw corr 只说明同受市场驱动，不构成映射依据）
            raw_joined = pd.concat([r.rename("ths"), sw_ret], axis=1, join="inner").dropna()
            raw_corrs = raw_joined.corr()["ths"].drop("ths") if not raw_joined.empty else pd.Series(dtype=float)

            rule_sw = RULES.get(name)                     # 分类对照表（行业公认对应）
            tags = []
            if rule_sw:
                target = "CN.SW" + rule_sw
                sw_code = rule_sw
                corr = float(corrs.get(target, np.nan))
                alt = corrs.drop(target, errors="ignore")     # 次优 = 除规则目标外的最高
                second, corr2 = (alt.index[0], float(alt.iloc[0])) if len(alt) else ("", np.nan)
                method, confirmed = "rule+corr", True
                # 交叉校验：相关性最高的行业与规则目标不一致且相关性够强 → 记进 remark 供复核
                if len(corrs) and corrs.index[0] != target and float(corrs.iloc[0]) >= LOW_CONF_CORR:
                    tags.append(f"corr_suggests={corrs.index[0].replace('CN.SW', '')}"
                                f"({float(corrs.iloc[0]):.3f})")
                if not np.isnan(corr) and corr < 0.3:
                    tags.append(f"low_corr_with_rule={corr:.3f}")
            else:
                sw_code = corrs.index[0].replace("CN.SW", "")
                corr, second, corr2 = float(corrs.iloc[0]), corrs.index[1], float(corrs.iloc[1])
                method, confirmed = "excess_corr", False
                tags.append("no_rule")
                if corr < LOW_CONF_CORR:
                    tags.append("low_conf")
                if corr - corr2 < AMBIGUOUS_MARGIN:
                    tags.append("ambiguous")
            raw_c = raw_corrs.get("CN.SW" + sw_code, np.nan)
            if not np.isnan(raw_c) and raw_c < 0.5:
                tags.append(f"raw_corr={raw_c:.3f}")
            rows.append({
                "ths_name": name, "ths_code": code,
                "sw_code": sw_code, "sw_name": None,
                "method": method, "corr": None if np.isnan(corr) else round(corr, 4),
                "corr_runner_up": None if (corr2 is None or np.isnan(corr2)) else round(corr2, 4),
                "runner_up_code": (second or "").replace("CN.SW", "") or None,
                "overlap_days": len(both), "as_of": datetime.date.today(),
                "confirmed": confirmed, "remark": ",".join(tags) or None, "source": SOURCE,
            })
        # 补 sw_name（从 daily_benchmark 的 bench_name）
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT bench_code, bench_name FROM daily_benchmark "
                            "WHERE bench_code LIKE 'CN.SW%'")
                sw_names = {k.replace("CN.SW", ""): v.replace("申万-", "") for k, v in cur.fetchall()}
        for r in rows:
            r["sw_name"] = sw_names.get(r["sw_code"])

        print()
        print(f"  ── 映射结果（{len(rows)} 行，跳过已确认 {skipped}）──")
        for r in sorted(rows, key=lambda x: x["sw_code"]):
            tag = "规则" if r["method"] == "rule+corr" else "相关"
            flag = f"  ⚠️ {r['remark']}" if r["remark"] else ""
            c = "  n/a " if r["corr"] is None else f"{r['corr']:.3f}"
            c2 = " n/a " if r["corr_runner_up"] is None else f"{r['corr_runner_up']:.3f}"
            print(f"    [{tag}] {r['ths_name']:10s} → SW{r['sw_code']} {r['sw_name'] or '':6s}"
                  f" corr={c} 次优={r['runner_up_code'] or '-':>6s}({c2}){flag}")

        if dry_run:
            print("\n  --dry-run：不写库")
            return len(rows)
        if not rows:
            print("  ⚠️ 无映射结果，未写库")
            return 0
        now = datetime.datetime.now()
        for r in rows:
            r["updated_at"] = now
        with get_conn() as conn:
            bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), count(*) FILTER (WHERE confirmed), "
                            f"count(DISTINCT sw_code) FROM {TABLE}")
                cnt, conf, n_sw = cur.fetchone()
        print(f"\n  ✅ 写入 {len(rows)} 行；库内 {cnt} 行（人工确认 {conf}，覆盖申万 {n_sw} 个一级行业）")
        return len(rows)


def run(codes=None, ctx=None, days: int = 250, only: str = None, dry_run: bool = False):
    """调度入口（`collector_runtime.run_module` 在进程内调用，只传 codes/ctx）。"""
    return build(days=days, only=only, dry_run=dry_run)


def report():
    with get_conn() as conn:
        df = pd.read_sql_query(
            """SELECT m.ths_name, m.sw_code, m.sw_name, m.corr, m.corr_runner_up, m.remark,
                      m.confirmed,
                      (SELECT count(*) FROM regime.sector_fund_flow f WHERE f.sector_name=m.ths_name) AS flow_rows
               FROM regime.sector_mapping m ORDER BY m.sw_code, m.ths_name""", conn)
    if df.empty:
        print("  映射表为空，先跑 build_sector_mapping.py")
        return
    print(f"共 {len(df)} 条映射，覆盖 {df['sw_code'].nunique()} 个申万一级行业")
    print(df.to_string(index=False))
    print()
    by_sw = df.groupby(["sw_code", "sw_name"]).size().sort_values(ascending=False)
    print("每申万行业对口同花顺行业数（前 10）:")
    print(by_sw.head(10).to_string())
    unmatched = [n for n in _ths_names() if n not in set(df["ths_name"])]
    if unmatched:
        print(f"\n未映射的同花顺行业（{len(unmatched)}）: {unmatched}")


def _ths_names() -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak
    return [str(r["name"]) for _, r in ak.stock_board_industry_name_ths().iterrows()]


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250, help="相关性窗口（交易日）")
    ap.add_argument("--only", default=None, help="单个同花顺行业名")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true", help="只打印当前映射表")
    args = ap.parse_args()

    if args.report:
        report()
        return 0
    print("=" * 62)
    print("同花顺行业 → 申万一级 映射（日收益相关性推断）")
    print("=" * 62)
    build(days=args.days, only=args.only, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
