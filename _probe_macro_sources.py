#!/usr/bin/env python3
"""候选数据源探测（只读，不落库，不写业务表）—— 市场状态分析采集层 P0 第 1 步。

用途:
    逐接口实测「可用性 / 返回行数 / 历史起止 / 字段」四要素，人工确认后再开发正式采集。
    方案文档: doc/market_profile_collection_plan.md（§7.1 P0-1、附录 A）

用法:
    python3 _probe_macro_sources.py                       # 探测全部候选
    python3 _probe_macro_sources.py --only CN.BOND10Y,CN.PMI
    python3 _probe_macro_sources.py --layer 3             # 只探测第③层（市场状态）
    python3 _probe_macro_sources.py --write doc/macro_source_probe_report.md
    python3 _probe_macro_sources.py --timeout 90          # 单接口超时秒数（默认 60）

约定:
    · 每个接口独立 try/except + SIGALRM 超时，单点失败不影响整体；
    · 全程只读取，不写数据库（与采集脚本严格区分）；
    · 免费源字段/深度会变，本脚本可保留作为「源变更复核工具」定期重跑。
"""
import argparse
import signal
import sys
import traceback
from datetime import date, datetime

# ── 结果记录 ────────────────────────────────────────────────────────────────
RESULTS: list[dict] = []

# 常见日期列名（优先匹配，避免误判数值列）
DATE_COL_CANDIDATES = [
    "日期", "date", "Date", "时间", "报告期", "统计时间", "交易日", "trade_date",
    "报告日期", "月份", "季度", "公布日期", "发布日期", "上市日期", "成立日期",
]


def _timeout_handler(signum, frame):
    raise TimeoutError("probe timeout")


def _detect_date_range(df) -> tuple:
    """从 DataFrame 里猜测日期列并返回 (起, 止) 字符串。"""
    import pandas as pd

    for col in DATE_COL_CANDIDATES:
        if col in df.columns:
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                return str(s.min())[:10], str(s.max())[:10], col
    # 兜底：逐列尝试解析（非数值列且可解析比例 > 80%）
    for col in df.columns:
        if df[col].dtype == object:
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().mean() > 0.8 and len(df) > 1:
                return str(s.min())[:10], str(s.max())[:10], col
    return "", "", ""


def _run_one(cand: dict, label: str, kwargs: dict, timeout: int) -> dict:
    """执行单个 AKShare 调用并汇总结果。"""
    import akshare as ak

    out = {"ok": False, "label": label, "err": "", "rows": 0,
           "start": "", "end": "", "date_col": "", "cols": [], "sample": ""}
    func = getattr(ak, cand["ak_func"], None)
    if func is None:
        out["err"] = f"AKShare 无此接口: {cand['ak_func']}"
        return out

    signal.alarm(timeout)
    try:
        df = func(**kwargs)
        signal.alarm(0)
        if df is None or len(df) == 0:
            out["err"] = "返回空 DataFrame"
            return out
        out["ok"] = True
        out["rows"] = len(df)
        out["cols"] = [str(c) for c in df.columns]
        start, end, dcol = _detect_date_range(df)
        out["start"], out["end"], out["date_col"] = start, end, dcol
        try:
            out["sample"] = str(df.tail(1).to_dict("records")[0])[:220]
        except Exception:
            pass
    except TimeoutError:
        out["err"] = f"超时(>{timeout}s)"
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        signal.alarm(0)
    return out


def probe_akshare(cand: dict, timeout: int) -> dict:
    """通用 AKShare 探测：支持 kwargs（单次）或 kwargs_list（多次窗口）。"""
    kw_list = cand.get("kwargs_list") or [("默认参数", cand.get("kwargs", {}))]
    subs = [_run_one(cand, label, kw, timeout) for label, kw in kw_list]
    ok = [s for s in subs if s["ok"]]
    return {
        "ok": len(ok) > 0,
        "subs": subs,
        "note": cand.get("note", ""),
    }


def probe_credit_symbols(cand: dict, timeout: int) -> dict:
    """专项：枚举中债收益率曲线品种，并试取信用债（中短期票据 AAA）一个月窗口。"""
    import akshare as ak

    subs = []
    signal.alarm(timeout)
    try:
        m = ak.bond_china_close_return_map()
        signal.alarm(0)
        labels = [str(x) for x in m["cnLabel"].tolist()]
        hits = [x for x in labels if ("票据" in x and "AAA" in x) or ("企业债" in x and "AAA" in x)]
        subs.append({"ok": True, "label": f"品种枚举(共{len(labels)}种)", "err": "",
                     "rows": len(labels), "start": "", "end": "", "date_col": "",
                     "cols": hits[:12], "sample": ""})
        # 试取一个信用品种（优先中短期票据(AAA)，否则企业债(AAA)）
        trial = next((x for x in hits if "中短期票据" in x and "AAA)" in x), None) or (hits[0] if hits else None)
        if trial:
            subs.append(_run_one({**cand, "ak_func": "bond_china_close_return"},
                                 f"试取「{trial}」近1月", 
                                 dict(symbol=trial, period="1",
                                      start_date="20260901", end_date="20260930"),
                                 timeout))
    except TimeoutError:
        subs.append({"ok": False, "label": "品种枚举", "err": f"超时(>{timeout}s)",
                     "rows": 0, "start": "", "end": "", "date_col": "", "cols": [], "sample": ""})
    except Exception as e:
        subs.append({"ok": False, "label": "品种枚举", "err": f"{type(e).__name__}: {str(e)[:200]}",
                     "rows": 0, "start": "", "end": "", "date_col": "", "cols": [], "sample": ""})
    finally:
        signal.alarm(0)
    return {"ok": any(s["ok"] for s in subs), "subs": subs, "note": cand.get("note", "")}


def probe_url(cand: dict, timeout: int) -> dict:
    """专项：网页源可用性（状态码 + 是否疑似有效内容）。"""
    import requests

    subs = []
    for label, url in cand["urls"]:
        s = {"ok": False, "label": label, "err": "", "rows": 0,
             "start": "", "end": "", "date_col": "", "cols": [], "sample": ""}
        signal.alarm(timeout)
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            signal.alarm(0)
            s["ok"] = r.status_code == 200
            s["rows"] = len(r.content)
            s["sample"] = f"HTTP {r.status_code}, {len(r.content)} bytes, ct={r.headers.get('Content-Type','')[:40]}"
            if not s["ok"]:
                s["err"] = f"HTTP {r.status_code}"
        except TimeoutError:
            s["err"] = f"超时(>{timeout}s)"
        except Exception as e:
            s["err"] = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            signal.alarm(0)
        subs.append(s)
    return {"ok": any(s["ok"] for s in subs), "subs": subs, "note": cand.get("note", "")}


def probe_discover(cand: dict, timeout: int) -> dict:
    """专项：列出已安装 AKShare 的函数名清单（用于函数名发现/复核）。"""
    import akshare as ak

    names = sorted(n for n in dir(ak) if n.startswith(cand["prefix"]) and not n.startswith("_"))
    return {"ok": True,
            "subs": [{"ok": True, "label": f"{cand['prefix']}* 共 {len(names)} 个",
                      "err": "", "rows": len(names), "start": "", "end": "",
                      "date_col": "", "cols": names, "sample": ""}],
            "note": cand.get("note", "")}


def probe_northbound_check(cand: dict, timeout: int) -> dict:
    """专项：北向资金历史字段完整性按年统计（定位 2024-08 停披露断点）。"""
    import akshare as ak
    import pandas as pd

    blank = {"ok": False, "label": "", "err": "", "rows": 0,
             "start": "", "end": "", "date_col": "", "cols": [], "sample": ""}
    subs = []
    signal.alarm(timeout)
    try:
        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        signal.alarm(0)
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        cols = [c for c in ["当日成交净买额", "买入成交额", "卖出成交额", "持股市值"] if c in df.columns]
        df["年"] = df["日期"].dt.year
        g = df.groupby("年")[cols].apply(lambda x: x.notna().sum())
        lines = [f"{y}: " + "/".join(f"{c}={int(v)}" for c, v in row.items()) for y, row in g.iterrows()]
        last_net = df[df["当日成交净买额"].notna()]["日期"].max()
        subs.append({**blank, "ok": True, "rows": len(df),
                     "label": f"净买额最后非空日: {str(last_net)[:10]}；逐年非空计数见字段列",
                     "start": str(df["日期"].min())[:10], "end": str(df["日期"].max())[:10],
                     "date_col": "日期", "cols": lines[-5:]})
    except TimeoutError:
        subs.append({**blank, "label": "探测", "err": f"超时(>{timeout}s)"})
    except Exception as e:
        subs.append({**blank, "label": "探测", "err": f"{type(e).__name__}: {str(e)[:200]}"})
    finally:
        signal.alarm(0)
    return {"ok": any(s["ok"] for s in subs), "subs": subs, "note": cand.get("note", "")}


# ── 候选清单 ────────────────────────────────────────────────────────────────
# 接口名与参数均已按本机 akshare 1.18.60 实测签名核对（2026-09-30）。
CANDIDATES: list[dict] = [
    # ══ 第①层 宏观 ══
    dict(series="CN.BOND10Y", layer=1, priority="P0", desc="中债国债收益率曲线(10Y/1Y/3Y/5Y/7Y/30Y)",
         kind="akshare", ak_func="bond_china_yield",
         kwargs_list=[
             ("近3月窗口", dict(start_date="20260701", end_date="20260930")),
             ("2010年窗口", dict(start_date="20100101", end_date="20101231")),
             ("2015年窗口", dict(start_date="20150101", end_date="20151231")),
             ("2018年窗口", dict(start_date="20180101", end_date="20181231")),
             ("2022年窗口", dict(start_date="20220101", end_date="20221231")),
         ],
         note="官方口径，单次查询跨度须<1年 → 全历史回填按年分段；2002年窗口实测返回空，真实起点由本轮多年窗口判定"),
    dict(series="CN.CREDIT_SPREAD", layer=1, priority="P1", desc="信用利差(中债中短期票据/企业债 AAA)",
         kind="special", fn="credit_symbols",
         note="bond_china_close_return 单次窗口<1月且 pageSize=50 → 回填需按月分段"),
    dict(series="CN.SHRZGM", layer=1, priority="P1", desc="社会融资规模存量(月)",
         kind="akshare", ak_func="macro_china_shrzgm", kwargs={}),
    dict(series="CN.MONEY_SUPPLY", layer=1, priority="P1", desc="M0/M1/M2 货币供应(月)",
         kind="akshare", ak_func="macro_china_money_supply", kwargs={}),
    dict(series="CN.PMI", layer=1, priority="P1", desc="制造业 PMI(月)",
         kind="akshare", ak_func="macro_china_pmi", kwargs={}),
    dict(series="CN.CPI", layer=1, priority="P1", desc="CPI(月)",
         kind="akshare", ak_func="macro_china_cpi", kwargs={}),
    dict(series="CN.PPI", layer=1, priority="P1", desc="PPI(月)",
         kind="akshare", ak_func="macro_china_ppi", kwargs={}),
    dict(series="HK.HIBOR", layer=1, priority="P1", desc="HIBOR 港币拆借利率(隔夜/1月)",
         kind="akshare", ak_func="rate_interbank",
         kwargs_list=[
             ("Hibor港币 1月", dict(market="香港银行同业拆借市场", symbol="Hibor港币", indicator="1月")),
             ("Hibor港币 隔夜", dict(market="香港银行同业拆借市场", symbol="Hibor港币", indicator="隔夜")),
         ]),
    dict(series="HK.HIBOR_CNH", layer=1, priority="P2", desc="Hibor 人民币(离岸流动性)",
         kind="akshare", ak_func="rate_interbank",
         kwargs=dict(market="香港银行同业拆借市场", symbol="Hibor人民币", indicator="1月")),
    dict(series="CMDTY.COPPER", layer=1, priority="P1", desc="沪铜连续日线",
         kind="akshare", ak_func="futures_zh_daily_sina", kwargs=dict(symbol="CU0")),
    dict(series="CMDTY.GOLD", layer=1, priority="P1", desc="沪金连续日线",
         kind="akshare", ak_func="futures_zh_daily_sina", kwargs=dict(symbol="AU0")),
    dict(series="CMDTY.OIL", layer=1, priority="P1", desc="原油连续日线(上期能源)",
         kind="akshare", ak_func="futures_zh_daily_sina", kwargs=dict(symbol="SC0")),
    dict(series="discover:macro_china_*", layer=1, priority="—", desc="函数名发现：macro_china_* 全清单",
         kind="discover", prefix="macro_china"),

    # ══ 第②层 资产配置 ══
    dict(series="CN.VAL_PE", layer=2, priority="P0", desc="全A 估值 PE-TTM(长历史)",
         kind="akshare", ak_func="stock_a_ttm_lyr", kwargs={}),
    dict(series="CN.VAL_PB", layer=2, priority="P0", desc="全A 估值 PB(长历史)",
         kind="akshare", ak_func="stock_a_all_pb", kwargs={}),
    dict(series="CN.BELOW_NET_ASSET", layer=2, priority="P1", desc="破净股统计(占比/家数)【已实测不可用】",
         kind="akshare", ak_func="stock_a_below_net_asset_statistics", kwargs=dict(symbol="全部A股"),
         note="实测 KeyError: marketId（乐咕乐股接口变更，akshare 1.18.60 实现失配）→ 降级：破净占比自算（a_daily_quote.pb_ratio < 1，2018 起）"),
    dict(series="CN.HIGH_LOW_STAT", layer=2, priority="P1", desc="新高新低统计(家数)【已实测不可用】",
         kind="akshare", ak_func="stock_a_high_low_statistics", kwargs=dict(symbol="all"),
         note="实测 TypeError（源返回日期格式变更，akshare 实现失配）→ 降级：新高新低自算（全市场日线，历史可回溯）"),

    # ══ 第③层 市场状态 ══
    dict(series="CN.IVIX", layer=3, priority="P0", desc="50ETF 期权波动率指数(QVIX)",
         kind="akshare", ak_func="index_option_50etf_qvix", kwargs={}),
    dict(series="CN.NORTHBOUND_HIST", layer=3, priority="P0", desc="北向资金历史(东财)+字段完整性断点",
         kind="special", fn="northbound_check",
         note="重点看 2024-08 后「成交净买额/买入成交额/持股市值」是否仍有值 → 决定北向指标口径"),
    dict(series="CN.SOUTHBOUND_STAT", layer=3, priority="P0", desc="南向持股统计(测历史深度)",
         kind="akshare", ak_func="stock_hsgt_stock_statistics_em",
         kwargs_list=[
             ("2021-06 窗口", dict(symbol="南向持股", start_date="20210601", end_date="20210607")),
             ("2022-06 窗口", dict(symbol="南向持股", start_date="20220601", end_date="20220607")),
             ("2023-06 窗口", dict(symbol="南向持股", start_date="20230601", end_date="20230607")),
             ("2024-06 窗口", dict(symbol="南向持股", start_date="20240603", end_date="20240607")),
             ("2026-09 窗口", dict(symbol="南向持股", start_date="20260920", end_date="20260930")),
         ],
         note="决定 daily_ggt_hold 能否回填；已实测 2020 窗口返回空，本轮界定真实起点"),
    dict(series="CN.LIMIT_UP_POOL", layer=3, priority="P1", desc="涨停股池(自算涨跌停的校核源)",
         kind="akshare", ak_func="stock_zt_pool_em", kwargs=dict(date="20260929")),
    dict(series="CN.IPO", layer=3, priority="P1", desc="新股上市一览(IPO/破发)",
         kind="akshare", ak_func="stock_xgsglb_em", kwargs=dict(symbol="全部股票")),
    dict(series="CN.FUND_NEW", layer=3, priority="P1", desc="新成立基金(爆款基金信号)",
         kind="akshare", ak_func="fund_new_found_em", kwargs={}),
    dict(series="CN.ACCOUNT", layer=3, priority="P1", desc="新增投资者开户数(月)",
         kind="akshare", ak_func="stock_account_statistics_em", kwargs={}),
    dict(series="CN.UNLOCK_SUMMARY", layer=3, priority="P1", desc="限售解禁汇总(未来90天窗口)",
         kind="akshare", ak_func="stock_restricted_release_summary_em",
         kwargs=dict(symbol="全部股票", start_date="20260930", end_date="20261231"),
         note="akshare 默认参数写死 2022-11~2022-12，必须显式传未来窗口，否则取到陈旧数据"),
    dict(series="CN.UNLOCK_QUEUE", layer=3, priority="P1", desc="个股解禁批次(单票示例)",
         kind="akshare", ak_func="stock_restricted_release_queue_em", kwargs=dict(symbol="600000")),
    dict(series="CN.INSIDER", layer=3, priority="P1", desc="高管/股东增减持明细",
         kind="akshare", ak_func="stock_hold_management_detail_em", kwargs={}),
    dict(series="CN.TRADE_CAL", layer=3, priority="P0", desc="A股交易日历(建 trading_calendar 用)",
         kind="akshare", ak_func="tool_trade_date_hist_sina", kwargs={}),
    dict(series="HK.HSI_HIST", layer=3, priority="P0", desc="恒生指数日线历史深度(恒指回填可行性)",
         kind="akshare", ak_func="stock_hk_index_daily_sina", kwargs=dict(symbol="HSI"),
         note="测回填 2019 前港股行情/恒指的可回溯深度"),
    dict(series="HK.SHORT_HIST", layer=3, priority="P1", desc="港股卖空历史(港交所日报归档)",
         kind="url",
         urls=[
             ("现采集页(chi, 生产在用)", "https://www.hkex.com.hk/chi/stat/smstat/ssturnover/ncms/ashtmain_c.htm"),
             ("现采集页(eng)", "https://www.hkex.com.hk/eng/stat/smstat/ssturnover/ncms/ashtmain_e.htm"),
             ("日报 2026-09-29", "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d260929e.htm"),
             ("日报 2024-01-02", "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d240102e.htm"),
             ("日报 2022-01-04", "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d220104e.htm"),
             ("日报 2018-01-02", "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d180102e.htm"),
         ],
         note="结论：日报归档（dayquot dYYMMDDe.htm）旧日期全部 404 → 卖空历史回填【未解决】，需另找港交所归档（P1 待调研）；现采集页盘中返回 stub（818字节）属正常，判据以生产库 daily_short_selling（2026-06 起有数据）为准"),
    dict(series="HK.STOCKCONNECT_STATS", layer=3, priority="P0", desc="沪深港通每日成交额(港交所页面，北向替代源)",
         kind="url",
         urls=[
             ("Stock Connect 统计入口", "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics?sc_lang=en"),
             ("每日成交(daily turnover)", "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Daily-Turnover?sc_lang=en"),
             ("历史月度成交", "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Monthly-Turnover?sc_lang=en"),
         ],
         note="结论：入口页可访问（373KB），但 daily/monthly 子页 URL 404 → 需人工在入口页定位真实链接或改用港交所 API；北向净流入已确认 2024-08-16 断流"),
    dict(series="CN.HSGT_SUMMARY", layer=3, priority="P1", desc="沪深港通当日资金汇总(东财)",
         kind="akshare", ak_func="stock_hsgt_fund_flow_summary_em", kwargs={}),
    dict(series="HK.HSTECH_HIST", layer=3, priority="P1", desc="恒生科技指数日线历史(em源)",
         kind="akshare", ak_func="stock_hk_index_daily_em", kwargs=dict(symbol="HSTECH"),
         note="symbol 待确认（HSTECH / HSTECH.HI），失败不阻塞 → 可用 sina 源 HSI 同族替代"),
    dict(series="CN.DELIST_SH", layer=3, priority="P1", desc="上交所终止/暂停上市名单",
         kind="akshare", ak_func="stock_info_sh_delist", kwargs=dict(symbol="全部")),
    dict(series="CN.DELIST_SZ", layer=3, priority="P1", desc="深交所终止上市名单",
         kind="akshare", ak_func="stock_info_sz_delist", kwargs=dict(symbol="终止上市公司")),

    # ══ 第④层 行业 ══
    dict(series="CN.SW_INDEX", layer=4, priority="P1", desc="申万一级行业指数日线(示例801010)",
         kind="akshare", ak_func="index_hist_sw", kwargs=dict(symbol="801010", period="day"),
         note="若历史够长，31个一级行业可全量回填 → daily_benchmark"),
    dict(series="CN.INDUSTRY_FLOW", layer=4, priority="P1", desc="行业板块资金流(当日快照)",
         kind="akshare", ak_func="stock_sector_fund_flow_rank",
         kwargs=dict(indicator="今日", sector_type="行业资金流")),
    dict(series="CN.INDUSTRY_FLOW_HIST", layer=4, priority="P2", desc="行业历史资金流(单行业示例)",
         kind="akshare", ak_func="stock_sector_fund_flow_hist", kwargs=dict(symbol="汽车")),
    dict(series="CN.FORECAST", layer=4, priority="P2", desc="东财盈利预测(一致预期近似)",
         kind="akshare", ak_func="stock_profit_forecast_em", kwargs={},
         note="一致预期修正宽度需逐日快照，本接口为免费近似源"),
]


def run_probe(cand: dict, timeout: int) -> dict:
    kind = cand["kind"]
    if kind == "akshare":
        return probe_akshare(cand, timeout)
    if kind == "special" and cand.get("fn") == "credit_symbols":
        return probe_credit_symbols(cand, timeout)
    if kind == "special" and cand.get("fn") == "northbound_check":
        return probe_northbound_check(cand, timeout)
    if kind == "url":
        return probe_url(cand, timeout)
    if kind == "discover":
        return probe_discover(cand, timeout)
    return {"ok": False, "subs": [], "note": f"未知探测类型: {kind}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔 series 过滤（支持前缀，如 CN.,HK.）")
    ap.add_argument("--layer", type=int, default=0, help="只探测指定层（1/2/3/4）")
    ap.add_argument("--timeout", type=int, default=60, help="单接口超时秒数（默认 60）")
    ap.add_argument("--write", default="", help="报告输出路径（markdown）")
    args = ap.parse_args()

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    targets = [
        c for c in CANDIDATES
        if (not args.layer or c.get("layer") == args.layer)
        and (not only or any(c["series"] == o or c["series"].startswith(o) for o in only))
    ]
    if not targets:
        print("没有匹配的候选，检查 --only / --layer")
        return 1

    try:
        import akshare as ak
        print(f"akshare {ak.__version__} | 候选 {len(targets)} 个 | 单接口超时 {args.timeout}s\n")
    except ImportError as e:
        print(f"akshare 不可用: {e}")
        return 1

    md_lines = [
        "# 候选数据源探测报告",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}；"
        f"由 `_probe_macro_sources.py` 输出，仅记录**可用性/行数/历史起止/字段**，不代表已开发。",
        "",
        "| series | 层 | 优先级 | 说明 | 结果 | 行数 | 历史起止 | 关键字段/备注 |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for cand in targets:
        head = f"[{cand['series']}] {cand['desc']}"
        try:
            res = run_probe(cand, args.timeout)
        except Exception:
            res = {"ok": False, "subs": [], "note": traceback.format_exc(limit=2)}

        flag = "✅" if res.get("ok") else "❌"
        print(f"{flag} {head}")
        for s in res.get("subs", []):
            sflag = "  ✅" if s["ok"] else "  ❌"
            rng = f"{s['start']}~{s['end']}" if s["start"] else "-"
            detail = s["err"] if not s["ok"] else str(s["cols"][:6])
            print(f"{sflag} {s['label']}: rows={s['rows']} range={rng}")
            print(f"      {detail}")
            if s.get("sample"):
                print(f"      sample: {s['sample']}")
        if res.get("note"):
            print(f"  note: {res['note']}")

        first = (res.get("subs") or [{}])[0]
        rng = f"{first.get('start','')}~{first.get('end','')}" if first.get("start") else "-"
        detail_md = res.get("note", "") or ""
        unit = "字节" if cand["kind"] == "url" else "行"
        for s in res.get("subs", []):
            if s["ok"]:
                extra = f"{s['rows']} {unit}"
                if s.get("start"):
                    extra += f" | {s['start']}~{s['end']}"
                extra += " | " + ", ".join(str(c) for c in s["cols"][:6])
            else:
                extra = f"❌ {s['err']}"
            detail_md += f"<br>`{s['label']}`: {extra}"
        md_lines.append(
            f"| {cand['series']} | {cand.get('layer','—')} | {cand.get('priority','—')} | {cand['desc']} | "
            f"{flag} | {first.get('rows','-')} | {rng} | {detail_md[:400]} |"
        )
        RESULTS.append({"cand": cand, "res": res})
        print()

    ok_cnt = sum(1 for r in RESULTS if r["res"].get("ok"))
    print(f"===== 汇总: {ok_cnt}/{len(RESULTS)} 可用 =====")
    print("失败清单:")
    for r in RESULTS:
        if not r["res"].get("ok"):
            errs = "; ".join(s["err"][:120] for s in r["res"].get("subs", []) if s["err"])[:200]
            print(f"  ❌ {r['cand']['series']}: {errs}")

    if args.write:
        with open(args.write, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines) + "\n")
        print(f"\n报告已写入 {args.write}")
    return 0


if __name__ == "__main__":
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _timeout_handler)
    sys.exit(main())
