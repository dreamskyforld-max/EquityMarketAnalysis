#!/usr/bin/env python3
"""宏观月/季频包采集 —— 写入 regime.macro_series。

覆盖（第①层 宏观 + 第②层 资产配置输入）：
    货币信用  CN.SHRZGM_INC(社融增量) / CN.SHRZGM_RMB_LOAN(社融-人民币贷款)
              CN.M2_YOY / CN.M1_YOY / CN.M1_M2_SCISSOR(派生)
              CN.NEW_LOAN(新增人民币贷款) / CN.LPR_1Y / CN.LPR_5Y
    通胀价格  CN.CPI_MOM / CN.CPI_YOY / CN.CORE_CPI_YOY(核心CPI)
              / CN.PPI_YOY / CN.PPI_CPI_SCISSOR(派生)
    经济周期  CN.PMI_MFG / CN.PMI_NONMFG / CN.IP_YOY(工业增加值) / CN.GDP_YOY / CN.GDP
              / OECD CLI（CN/US/JP/DE/KR + G7/G20，源：OECD SDMX）
    市场/实体 CN.MARKET_CAP(沪深市价总值，巴菲特指标分子) / CN.ELECTRICITY_YOY / CN.CONSUMER_CONFIDENCE
    情绪      CN.ACCOUNT_NEW(新增投资者；⚠ 源已停更于 2023-08)

口径与 PIT：
  · period_date = 统计期首日（月=当月 1 日 / 季=当季首月 1 日），对齐确定、无歧义；
  · release_time = 源带「发布时间」列时用真值（如工业增加值），否则用 period_date + lag 天
    （保守上限：月频 45 天 / 季频 100 天）——宁可偏晚，绝不前视；
  · freq: month / quarter；unit 见各序列定义；`source` 记录具体接口，便于溯源。

用法:
    python3 get_macro_monthly.py            # 全量刷新（月频数据量小，直接整表 upsert 幂等）
    python3 get_macro_monthly.py --dry-run  # 只打印不写库
"""
import argparse
import datetime
import re
import sys
import time

from db import get_conn, bulk_upsert

TABLE = "regime.macro_series"
CONFLICT = ["series_code", "period_date", "revision"]
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

# ── NBS「国家数据」直连（新版 /dg/website API）──────────────────────────────
# 背景（2026-10-08 实测更正）：此前记「核心 CPI 免费源不可得」是**错的**。错因是只用过
#   akshare `macro_china_nbs_nation` → 它走**老接口** easyquery.htm，实测 403 URLACL
#   （WAF 拦截；在页面内同源 fetch 也一样被挡，非脚本问题）。
#   国家统计局新版前端「国家数据」（data.stats.gov.cn/dg/website）改走
#   `/publicrelease/web/external/**` 一套新接口，**纯 requests 可直连**：
#     ① GET page.html 取 JSESSIONID → ② queryIndexTreeAsync 下钻目录
#     → ③ queryIndicatorsByCid 列指标 → ④ POST stream/esData 取序列。
#   核心 CPI 的官方口径名是「不包括食品和能源居民消费价格指数 (上年同月=100)」，
#   源为**指数型**（100=持平），入库统一换算为同比（指数 − 100）。
NBS_API = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
NBS_PAGE = "https://data.stats.gov.cn/dg/website/page.html"
NBS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_NBS_SESSION = None
_NBS_MIN_GAP = 1.2       # 两次请求最小间隔（秒）：防触发反爬限流（实测高频遍历会被持续拦）
_NBS_LAST_CALL = 0.0


def _nbs_session():
    """建会话并**主动握手**一次。

    实测（2026-10-08）该站前置 WAF 的行为：**每个新会话的第一次 API 调用会被拦**，
    返回 200 + HTML 反爬页（约 39KB，「Please enable JavaScript…」，非 JSON），
    同时下发一个 cookie；**同一会话的下一次调用即正常返回 JSON**。
    故：建会话时先打一次目录树接口把「挑战页」消耗掉，避免污染真实采集。
    """
    global _NBS_SESSION
    if _NBS_SESSION is None:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": NBS_UA, "Referer": NBS_PAGE})
        s.get(NBS_PAGE, timeout=30, verify=False)
        _NBS_SESSION = s
        try:                                    # 握手：吸收 WAF 首次挑战，失败不影响后续
            s.get(f"{NBS_API}/new/queryIndexTreeAsync",
                  params={"pid": "", "code": "1"}, timeout=30, verify=False)
        except Exception:                       # noqa: BLE001
            pass
    return _NBS_SESSION


def _nbs_json(method: str, url: str, tries: int = 3, **kw):
    """调 NBS 接口并解析 JSON；**带限速与重试**。

    ⚠ 重试策略的关键：解析失败（= WAF 挑战页/空体）时 **必须沿用同一会话**——
    挑战页下发的 cookie 正是下次放行的凭据，一旦重建会话就等于把「已通关」状态丢掉，
    会永远卡在第一次（实测踩过：每次都重建 → 连续 3 次全拿挑战页而误判为「接口挂了」）。
    仅当出现 HTTP 层错误（403/5xx）才重建会话重新握手。

    ⚠ 另需**限速**：实测连续高频请求（如整棵目录树遍历，数百次）会触发反爬，
    之后**每次**都返回挑战页且持续数分钟（本次调试即因此被限）。
    正常采集一轮仅 ~8 次请求，加最小间隔即可安全。
    """
    global _NBS_SESSION, _NBS_LAST_CALL
    last = None
    timeout = kw.pop("timeout", 60)
    gap = _NBS_MIN_GAP - (time.time() - _NBS_LAST_CALL)
    if gap > 0:
        time.sleep(gap)
    for i in range(tries):
        try:
            _NBS_LAST_CALL = time.time()
            r = _nbs_session().request(method, url, timeout=timeout, verify=False, **kw)
            r.raise_for_status()
            return r.json()
        except ValueError as e:                     # 含 requests.JSONDecodeError＝拿到挑战页
            last = e
        except Exception as e:                      # noqa: BLE001 —— HTTP/网络层
            last = e
            _NBS_SESSION = None                     # 会话可能已废 → 下次重建并重新握手
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"NBS {url.rsplit('/', 1)[-1]} 连续 {tries} 次失败：{type(last).__name__}: {last}")


def _nbs_tree(pid: str, code: str = "1") -> list:
    """目录树一级子节点（code=1 月度数据）；pid='' 取顶层。"""
    j = _nbs_json("GET", f"{NBS_API}/new/queryIndexTreeAsync", params={"pid": pid, "code": code})
    return j.get("data") or []


def _nbs_indicators(cid: str) -> list:
    """某目录叶子下的指标清单（含官方名 i_showname 与指标 id _id）。"""
    j = _nbs_json("GET", f"{NBS_API}/new/queryIndicatorsByCid",
                  params={"cid": cid, "dt": "", "name": ""})
    return (j.get("data") or {}).get("list") or []


def _nbs_esdata(cid: str, indicator_ids: list, root_id: str, dts: list) -> list:
    """取序列（POST stream/esData）→ [{code: '202608MM', values: [{value: '101.0'}]}]。"""
    body = {"cid": cid, "indicatorIds": indicator_ids, "daCatalogId": "",
            "das": [{"text": "全国", "value": "000000000000"}],
            "showType": "1", "dts": dts, "rootId": root_id}
    return (_nbs_json("POST", f"{NBS_API}/stream/esData", json=body) or {}).get("data") or []


def _nbs_period(code):
    """期间码「202608MM」→ 当月 1 日（MM=月度；年度码 YY 返回 None）。"""
    m = re.fullmatch(r"(\d{4})(\d{2})MM", str(code or ""))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return datetime.date(y, mo, 1) if 1 <= mo <= 12 else None


def _nbs_dts(spec: dict) -> list:
    """期间区间 —— **必须显式给**：`dts=""` 时接口只返回默认的最近 9 个月
    （实测核心 CPI 只回来 17 条、首段被截断）；给区间则一次返全（实测 27 年跨度的请求
    可正常返回 324 个月，无分页上限）。
    """
    start = spec.get("dts_from", "200001")
    end = spec.get("dts_to") or datetime.date.today().strftime("%Y%m")
    return [f"{start}MM-{end}MM"]


def _nbs_fetch(spec: dict) -> dict:
    """按 spec 定位目录 → 取序列 → {period_date: value}。

    NBS 把同一指标按年份切成**多个目录叶子**（(2026-)/(2021-2025)/(2016-2020)/(-2015)），
    且各段指标 id 不同、越早的段越可能**没有该指标**（如核心 CPI 仅 2021 起）→
    必须遍历全部匹配叶子并合并，缺指标的段直接跳过。
    """
    code = spec.get("code", "1")
    cur, root_id, node = _nbs_tree("", code), None, ""
    for name in spec["path"]:
        hit = [c for c in cur if str(c.get("name") or "").strip() == name.strip()]
        if not hit:
            raise RuntimeError(f"NBS 目录未找到「{name}」（现有：{[c.get('name') for c in cur][:6]}）")
        node = hit[0]["_id"]
        root_id = root_id or node          # 首级（月度数据）的 id 即 esData 的 rootId
        cur = _nbs_tree(node, code)
    leaves = [c for c in cur if str(c.get("name") or "").startswith(spec["leaf_prefix"])]
    if not leaves:
        raise RuntimeError(f"NBS 目录下无「{spec['leaf_prefix']}」叶子："
                           f"{[c.get('name') for c in cur][:6]}")
    out, hit_seg = {}, 0
    minus = spec.get("minus", 0)
    dts = spec.get("dts") or _nbs_dts(spec)
    for leaf in leaves:
        ids = [x["_id"] for x in _nbs_indicators(leaf["_id"])
               if spec["indicator"] in str(x.get("i_showname") or "")]
        if not ids:
            continue                        # 该时段库无此指标（如 2016-2020 无核心 CPI）
        hit_seg += 1
        for d in _nbs_esdata(leaf["_id"], ids, root_id, dts):
            pd_ = _nbs_period(d.get("code"))
            if pd_ is None:
                continue
            vals = d.get("values") or [{}]
            try:
                v = float(vals[0].get("value"))
            except (TypeError, ValueError):
                continue                    # 未公布月份为空串
            out[pd_] = round(v - minus, 4)
    print(f"    · NBS「{spec['indicator']}」：命中 {hit_seg}/{len(leaves)} 个时段目录")
    return dict(sorted(out.items()))

# ── OECD SDMX 直连（复合领先指标 CLI）────────────────────────────────────────
# 直连 OECD 官方 SDMX REST（sdmx.oecd.org）：**无鉴权、无 WAF、纯 requests 可取**。
# 数据流 = `OECD.SDD.STES,DSD_STES@DF_CLI`（Composite leading indicators），
# 对应 Data Explorer 页面 https://data-explorer.oecd.org/ （df[id]=DSD_STES@DF_CLI）。
# 键 `.M.LI...AA...H` 的语义（9 维）：FREQ=M 月度 / MEASURE=LI 领先指标 /
#   ADJUSTMENT=AA 振幅调整 / METHODOLOGY=H；返回值 UNIT_MEASURE=IX（指数，100=长期趋势）。
# 实测 2026-10-08：**一次请求即可取全部经济体**——中国 CLI 1992-05 起 413 个月（无缺口）、
#   美/日/德/英 1980-01 起、G7/G20/G4E 聚合齐全，且**最新到 2026-09（当月即得）**。
SDMX_CLI_URL = ("https://sdmx.oecd.org/public/rest/data/"
                "OECD.SDD.STES,DSD_STES@DF_CLI,4.1/{areas}.M.LI...AA...H")
_SDMX_KEY = "oecd:cli"


def _sdmx_cli(spec: dict, cache: dict) -> dict:
    """取 CLI 序列 → {ref_area: {period_date: value}}。

    多个经济体共用**同一次请求**（结果存进 cache）：SERIES 里每个 CLI 序列都带
    `sdmx={"ref_area": ...}`，第一个触发的负责下载，其余直接复用。
    """
    if _SDMX_KEY in cache:
        data = cache[_SDMX_KEY]
    else:
        import csv
        import io
        import requests
        url = SDMX_CLI_URL.format(areas="+".join(spec.get("areas") or _SDMX_AREAS))
        r = requests.get(url, params={"startPeriod": spec.get("start", "1980-01"),
                                      "format": "csvfile"},
                         headers={"User-Agent": NBS_UA}, timeout=90)
        r.raise_for_status()
        data: dict = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            d = _parse_period(row.get("TIME_PERIOD"), "month")
            try:
                v = float(row.get("OBS_VALUE"))
            except (TypeError, ValueError):
                continue                    # 空值/缺失（OBS_STATUS=M）
            if d is None:
                continue
            data.setdefault(row["REF_AREA"], {})[d] = round(v, 4)
        cache[_SDMX_KEY] = data
        print(f"    · OECD SDMX：{len(data)} 个经济体 / "
              f"{sum(len(v) for v in data.values())} 个月度观测（一次请求）")
    return data.get(spec["ref_area"], {})


# ── 序列定义：code / 名称 / 接口 / 取值列 / 单位 / 频率 / 发布滞后(天)
SERIES = [
    # 货币与信用
    # ⚠ 社能源侧停更（实测 2026-10-08）：akshare `macro_china_shrzgm` 上游是**商务部镜像**
    #   （data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery），最新只到 2026-04；
    #   akshare 全库无其它新鲜替代（已枚举 85 个 macro_china_* 接口核对）。
    #   → 历史可用、更新停摆；新鲜度替代用 CN.NEW_LOAN（新增人民币贷款，更新至 2026-08）。
    #   换源（央行官网直连）列为 P1。
    dict(code="CN.SHRZGM_INC", name="社会融资规模增量(月)", func="macro_china_shrzgm",
         col="社会融资规模增量", unit="yi_yuan", freq="month", lag=45,
         remark="⚠ 源（商务部镜像）停更于 2026-04；替代口径见 CN.NEW_LOAN"),
    dict(code="CN.SHRZGM_RMB_LOAN", name="社融-人民币贷款(月)", func="macro_china_shrzgm",
         col="其中-人民币贷款", unit="yi_yuan", freq="month", lag=45,
         remark="⚠ 源停更于 2026-04"),
    dict(code="CN.NEW_LOAN", name="新增人民币贷款(月)", func="macro_china_new_financial_credit",
         col="当月", unit="yi_yuan", freq="month", lag=45),
    dict(code="CN.M2_YOY", name="M2 同比", func="macro_china_money_supply",
         col="货币和准货币(M2)-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.M1_YOY", name="M1 同比", func="macro_china_money_supply",
         col="货币(M1)-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.LPR_1Y", name="LPR 1年期", func="macro_china_lpr",
         col="LPR1Y", unit="pct", freq="month", date_col="TRADE_DATE", lag=1,
         release_from_date=True, remark="源为日频重复报价 → 按月去重保留当月最后一次发布"),
    dict(code="CN.LPR_5Y", name="LPR 5年期", func="macro_china_lpr",
         col="LPR5Y", unit="pct", freq="month", date_col="TRADE_DATE", lag=1,
         release_from_date=True, remark="同上"),
    # 通胀与价格
    # 【更正 2026-10-08】此前记为「中国核心 CPI 无免费可得源」——**该结论错误**（详见文首 NBS_* 说明）。
    #   原判断只基于 akshare `macro_china_nbs_nation`（走**老接口** easyquery.htm，实测被 WAF 403）
    #   与 `macro_china_cpi`（确实无核心分项），未试国家统计局新版「国家数据」接口。
    #   实测：新版接口可直连，核心 CPI 2021-01 起共 68 个月可得 → 已落库 CN.CORE_CPI_YOY。
    dict(code="CN.CPI_MOM", name="CPI 环比", func="macro_china_cpi",
         col="全国-环比增长", unit="pct", freq="month", lag=45,
         remark="通胀动能（月度环比）；核心 CPI 见 CN.CORE_CPI_YOY"),
    dict(code="CN.CPI_YOY", name="CPI 同比", func="macro_china_cpi",
         col="全国-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.CORE_CPI_YOY", name="核心CPI同比(剔除食品能源)", unit="pct", freq="month", lag=45,
         source="nbs:stream/esData",
         remark="⚠ 国家数据月度库仅 2021-01 起有该指标（更早年份只有旧八大类分类指数、无核心口径）"
                "→ 分位样本约 5 年；源为指数型（上年同月=100），此处已换算为同比（指数−100）",
         nbs=dict(path=["月度数据", "价格指数", "居民消费价格分类指数 (上年同月=100)"],
                  leaf_prefix="全国居民消费价格分类指数",
                  indicator="不包括食品和能源", minus=100)),
    dict(code="CN.PPI_YOY", name="PPI 同比", func="macro_china_ppi",
         col="当月同比增长", unit="pct", freq="month", lag=45),
    # 经济周期
    dict(code="CN.PMI_MFG", name="制造业 PMI", func="macro_china_pmi",
         col="制造业-指数", unit="index", freq="month", lag=45),
    dict(code="CN.PMI_NONMFG", name="非制造业 PMI", func="macro_china_pmi",
         col="非制造业-指数", unit="index", freq="month", lag=45),
    dict(code="CN.IP_YOY", name="工业增加值同比", func="macro_china_gyzjz",
         col="同比增长", unit="pct", freq="month", lag=45, use_release_col="发布时间"),
    # OECD 复合领先指标 CLI（跨国可比；见文首 SDMX 说明）。
    # PIT 口径：**沿用月频默认 lag=45**。注意 lag 以「**月初**（period_date）」为基准，
    #   而 OECD 上月 CLI 的实际发布在**次月初**（实测：2026-09 读数在 2026-10-08 已可得，
    #   即 ≈ 月初+37 天）→ lag=45（= 月末+15）既覆盖实际发布又留足余量。
    #   ⚠ 切勿按「月末+8 天」误设为 lag=15：那等于把数据提前 3 周视为可得，构成前视。
    # G7/G20 用 GLOBAL 前缀（聚合体，非单一市场）；其余用市场前缀。
    dict(code="CN.CLI", name="OECD 综合领先指标(中国)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="CN",
         remark="振幅调整口径（AA），100=长期趋势；1992-05 起 413 个月无缺口；"
                "拐点领先经济约 3-6 个月",
         sdmx=dict(ref_area="CHN")),
    dict(code="US.CLI", name="OECD 综合领先指标(美国)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="US",
         remark="1980-01 起；全球需求与美元流动性的领先读数",
         sdmx=dict(ref_area="USA")),
    dict(code="JP.CLI", name="OECD 综合领先指标(日本)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="JP", sdmx=dict(ref_area="JPN")),
    dict(code="DE.CLI", name="OECD 综合领先指标(德国)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="DE",
         remark="欧洲制造业/出口周期的代表", sdmx=dict(ref_area="DEU")),
    dict(code="KR.CLI", name="OECD 综合领先指标(韩国)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="KR",
         remark="半导体/科技周期 → 对 A股科技板块有领先性", sdmx=dict(ref_area="KOR")),
    dict(code="GLOBAL.CLI_G7", name="OECD 综合领先指标(G7)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="GLOBAL",
         remark="发达经济体整体周期", sdmx=dict(ref_area="G7")),
    dict(code="GLOBAL.CLI_G20", name="OECD 综合领先指标(G20)", unit="index", freq="month", lag=45,
         source="oecd-sdmx:DSD_STES@DF_CLI", market="GLOBAL",
         remark="含新兴市场，全球周期更宽口径", sdmx=dict(ref_area="G20")),
    # GDP 口径（重要）：源按**年内累计**披露（第1季度 / 第1-2季度 / 第1-3季度 / 第1-4季度），
    #   period_date = 该累计区间的**末季度起始月**（Q1→01-01、H1→04-01、9M→07-01、FY→10-01），
    #   value 也是**累计值**（非单季）→ 消费方做同比/环比时勿直接当单季用。
    dict(code="CN.GDP_YOY", name="GDP 同比(累计口径)", func="macro_china_gdp",
         col="国内生产总值-同比增长", unit="pct", freq="quarter", lag=100,
         remark="年内累计同比；period_date=末季度起始月（H1 记 04-01）"),
    dict(code="CN.GDP", name="GDP 绝对值(季, 年内累计)", func="macro_china_gdp",
         col="国内生产总值-绝对值", unit="yi_yuan", freq="quarter", lag=100,
         remark="年内累计绝对值（非单季）；period_date=末季度起始月"),
    # 市场与实体
    dict(code="CN.MARKET_CAP_SH", name="沪深市价总值-上海", func="macro_china_stock_market_cap",
         col="市价总值-上海", unit="yi_yuan", freq="month", date_col="数据日期", lag=45),
    dict(code="CN.MARKET_CAP_SZ", name="沪深市价总值-深圳", func="macro_china_stock_market_cap",
         col="市价总值-深圳", unit="yi_yuan", freq="month", date_col="数据日期", lag=45),
    dict(code="CN.ELECTRICITY_YOY", name="全社会用电量同比", func="macro_china_society_electricity",
         col="全社会用电量同比", unit="pct", freq="month", date_col="统计时间", lag=45,
         remark="源「统计时间」为点分隔（如 2026.8）；历史曾因解析失败被压成年度，已修"),
    # 货运量（实体经济的另一高频验证；源按「统计对象」分口径，必须显式取「合计」）
    dict(code="CN.FREIGHT_YOY", name="货运量同比(合计)", func="macro_china_society_traffic_volume",
         col="货运量同比增长", unit="pct", freq="month", date_col="统计时间", lag=45,
         row_filter={"统计对象": "合计"}),
    dict(code="CN.CONSUMER_CONFIDENCE", name="消费者信心指数", func="macro_china_xfzxx",
         col="消费者信心指数-指数值", unit="index", freq="month", lag=45),
    # 情绪（源已停更，仅作历史序列）
    dict(code="CN.ACCOUNT_NEW", name="新增投资者数量(万户)", func="stock_account_statistics_em",
         col="新增投资者-数量", unit="count", freq="month", date_col="数据日期", lag=45,
         remark="⚠ 东方财富源已停更于 2023-08，历史序列可用"),
]

# OECD CLI 的取数范围 = 所有 CLI 序列的 ref_area 并集（一次请求全取，见 _sdmx_cli）
_SDMX_AREAS = [d["sdmx"]["ref_area"] for d in SERIES if d.get("sdmx")]

# 派生序列（由上面已入库的原始序列做差得出）
DERIVED = [
    dict(code="CN.M1_M2_SCISSOR", name="M1−M2 剪刀差(pp)", expr=("CN.M1_YOY", "CN.M2_YOY"),
         unit="pp", freq="month", remark="M1 同比 − M2 同比，扩大=资金活化"),
    dict(code="CN.PPI_CPI_SCISSOR", name="PPI−CPI 剪刀差(pp)", expr=("CN.PPI_YOY", "CN.CPI_YOY"),
         unit="pp", freq="month", remark="反映利润在上下游间的分配"),
]


def _parse_period(raw, freq_hint: str):
    """把 '2026年08月份' / '2026-08' / '2026年第2季度' / '202608' 解析为统计期首日。"""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t or t.lower() in ("nan", "none"):
        return None
    y = re.search(r"(\d{4})", t)
    if not y:
        return None
    year = int(y.group(1))
    # 季度
    q = re.search(r"(?:第)?\s*([1-4])\s*(?:季度|季|Q)", t, re.I)
    if q or freq_hint == "quarter":
        qq = int(q.group(1)) if q else None
        if qq is None:
            m = re.search(r"[-\s](\d{1,2})", t)
            qq = min(4, max(1, (int(m.group(1)) - 1) // 3 + 1)) if m else 1
        return datetime.date(year, (qq - 1) * 3 + 1, 1)
    # 点分隔「2026.8」/「2003.12」（**必须放在「年-月」兜底之前**）：
    #   源侧用点分隔的序列（全社会用电量/货运量）曾因此解析失败 → 全部落到「年初」，
    #   整表被压成「一年一条」（实测用电量只剩 24 行，看起来像年度数据）。
    md0 = re.fullmatch(r"(\d{4})\.(\d{1,2})", t)
    if md0 and 1 <= int(md0.group(2)) <= 12:
        return datetime.date(int(md0.group(1)), int(md0.group(2)), 1)
    # 月
    m = re.search(r"[年\-/\s](\d{1,2})(?!\d)", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return datetime.date(year, int(m.group(1)), 1)
    md = re.search(r"(\d{4})(\d{2})", t)
    if md and 1 <= int(md.group(2)) <= 12:
        return datetime.date(year, int(md.group(2)), 1)
    return datetime.date(year, 1, 1)


def _mk_row(defn: dict, d, v, rel=None, basis=None) -> dict:
    """macro_series 行模板（akshare 与 NBS 两条采集路径共用）。"""
    if rel is None:
        # 源无发布时间 → period + lag 天（保守上限，宁可偏晚绝不前视）
        rel = datetime.datetime.combine(
            d + datetime.timedelta(days=defn["lag"]), datetime.time(18, 0), tzinfo=TZ_CN)
        basis = basis or f"period+{defn['lag']}d"
    return {
        "series_code": defn["code"], "series_name": defn["name"],
        "period_date": d, "value": round(float(v), 4),
        "unit": defn["unit"], "freq": defn["freq"],
        "market": defn.get("market") or defn["code"].split(".")[0],
        "source": defn.get("source") or f"akshare:{defn.get('func')}",
        "release_time": rel, "revision": 0,
        "extra": {"release_basis": basis, "remark": defn.get("remark")},
        "updated_at": datetime.datetime.now(TZ_CN),
    }


def _collect_one(defn: dict, cache: dict) -> list:
    """采集单个序列 → macro_series 记录列表。"""
    if defn.get("nbs"):
        # 国家统计局「国家数据」直连（子函数自行做目录下钻与多段合并）
        return [_mk_row(defn, d, v) for d, v in _nbs_fetch(defn["nbs"]).items()]
    if defn.get("sdmx"):
        # OECD SDMX 直连（多个 CLI 序列共用一次请求，结果经 cache 复用）
        return [_mk_row(defn, d, v) for d, v in _sdmx_cli(defn["sdmx"], cache).items()]
    fn = defn["func"]
    if fn not in cache:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        fnobj = getattr(ak, fn, None)
        cache[fn] = fnobj() if fnobj else None
    df = cache[fn]
    if df is None or len(df) == 0:
        return []
    if defn["col"] not in df.columns:
        print(f"    ⚠️ {defn['code']}: 接口 {fn} 无列「{defn['col']}」（现有列：{list(df.columns)[:6]}）")
        return []
    # 行过滤：同一接口常含多个口径（如货运量按「统计对象」= 合计/铁路/公路/水运/民航），
    # 必须显式取总计口径，否则会被最后一行覆盖成单一子口径。
    rf = defn.get("row_filter")
    if rf:
        for k, v in rf.items():
            if k in df.columns:
                df = df[df[k] == v]
            else:
                print(f"    ⚠️ {defn['code']}: 无过滤列「{k}」，跳过该序列")
                return []

    import pandas as pd
    dcol = defn.get("date_col", "月份")
    if dcol not in df.columns:
        dcol = df.columns[0]
    rel_col = defn.get("use_release_col")

    rows, skipped = [], 0
    for _, r in df.iterrows():
        v = pd.to_numeric(r.get(defn["col"]), errors="coerce")
        if pd.isna(v):
            skipped += 1
            continue
        d = _parse_period(r.get(dcol), defn["freq"])
        if d is None:
            skipped += 1
            continue
        rel = None
        if defn.get("release_from_date"):
            # 真实发布时间就是数据日期本身（如 LPR 报价日）→ 直接用作 release_time
            rel = datetime.datetime.combine(d, datetime.time(9, 0), tzinfo=TZ_CN)
        if rel is None and rel_col and r.get(rel_col) is not None:
            rd = _parse_period(r.get(rel_col), "day")
            if rd:
                rel = datetime.datetime.combine(rd, datetime.time(18, 0), tzinfo=TZ_CN)
        if rel is None:
            rel = datetime.datetime.combine(
                d + datetime.timedelta(days=defn["lag"]), datetime.time(18, 0), tzinfo=TZ_CN)
        rows.append(_mk_row(defn, d, v, rel,
                            "source" if (rel_col and rel) else f"period+{defn['lag']}d"))
    if skipped:
        print(f"    · {defn['code']}: {len(rows)} 条（跳过空值 {skipped}）")
    return rows


def _build_derived(conn) -> list:
    """派生序列：从库里已入库的原始序列做差（同 period_date 对齐）。"""
    import pandas as pd
    rows = []
    for d in DERIVED:
        a, b = d["expr"]
        df = pd.read_sql_query(
            """SELECT a.period_date, a.value - b.value AS v
               FROM regime.macro_series a JOIN regime.macro_series b
                 ON a.period_date = b.period_date AND a.revision=0 AND b.revision=0
               WHERE a.series_code=%s AND b.series_code=%s ORDER BY 1""",
            conn, params=[a, b])
        for _, r in df.iterrows():
            if r["v"] is None or pd.isna(r["v"]):
                continue
            dd = r["period_date"]
            rows.append({
                "series_code": d["code"], "series_name": d["name"], "period_date": dd,
                "value": round(float(r["v"]), 4), "unit": d["unit"], "freq": d["freq"],
                "market": "CN", "source": "derived:get_macro_monthly",
                "release_time": datetime.datetime.combine(dd + datetime.timedelta(days=45),
                                                          datetime.time(18, 0), tzinfo=TZ_CN),
                "revision": 0, "extra": {"expr": f"{a} - {b}", "remark": d.get("remark")},
                "updated_at": datetime.datetime.now(TZ_CN),
            })
        print(f"    · {d['code']}: {len(rows)} 条累计（派生）")
    return rows


def _dedupe(rows: list) -> list:
    """同一 (series_code, period_date) 只保留 release_time 最新的一条。

    场景：LPR 等日频重复报价接口按月聚合后会同月多值（且源多为倒序），
    不去重会触发 `ON CONFLICT DO UPDATE cannot affect row a second time`。
    """
    best: dict = {}
    for i, r in enumerate(rows):
        key = (r["series_code"], r["period_date"])
        cur = best.get(key)
        if cur is None or (r["release_time"], i) > (cur["release_time"], cur["_idx"]):
            r = {**r, "_idx": i}
            best[key] = r
    out = [{k: v for k, v in r.items() if k != "_idx"} for r in best.values()]
    return sorted(out, key=lambda r: (r["series_code"], r["period_date"]))


def run(codes=None, ctx=None, dry_run: bool = False):
    """采集入口：全量刷新（幂等；月频数据量小）。"""
    cache: dict = {}
    all_rows = []
    print("  ── 原始序列 ──")
    for defn in SERIES:
        try:
            rows = _collect_one(defn, cache)
        except Exception as e:
            print(f"    ❌ {defn['code']}: {type(e).__name__}: {str(e)[:100]}")
            continue
        all_rows += rows
        if rows:
            ds = sorted(r["period_date"] for r in rows)
            print(f"    · {defn['code']}: {len(rows)} 条  {ds[0]} ~ {ds[-1]}")
    before = len(all_rows)
    all_rows = _dedupe(all_rows)
    print(f"  原始合计 {before} 条 → 去重后 {len(all_rows)} 条")

    with get_conn() as conn:
        if dry_run:
            print("  --dry-run：不写库")
            return len(all_rows)
        bulk_upsert(conn, TABLE, all_rows, conflict_cols=CONFLICT, skip_null_updates=True)
        print(f"  ── 派生序列 ──")
        drows = _build_derived(conn)
        if drows:
            bulk_upsert(conn, TABLE, drows, conflict_cols=CONFLICT, skip_null_updates=True)
        print(f"  ✅ 写入 {len(all_rows)} + 派生 {len(drows)} 条")
        with conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT series_code), count(*), max(updated_at)
                           FROM regime.macro_series WHERE source LIKE 'akshare:macro%'
                              OR source LIKE 'derived:get_macro_monthly'""")
            n, cnt, upd = cur.fetchone()
            print(f"  宏观包序列 {n} 个 / {cnt:,} 行，最后更新 {upd}")
    return len(all_rows) + len(drows)


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print("=" * 60)
    print("宏观月/季频包 → regime.macro_series")
    print("=" * 60)
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
