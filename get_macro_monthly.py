#!/usr/bin/env python3
"""宏观月/季频包采集 —— 写入 regime.macro_series。

覆盖（第①层 宏观 + 第②层 资产配置输入）：
    货币信用  CN.SHRZGM_INC(社融增量) / CN.SHRZGM_RMB_LOAN(社融-人民币贷款)
              CN.M2_YOY / CN.M1_YOY / CN.M1_M2_SCISSOR(派生)
              CN.NEW_LOAN(新增人民币贷款) / CN.LPR_1Y / CN.LPR_5Y
    通胀价格  CN.CPI_MOM / CN.CPI_YOY / CN.CORE_CPI_YOY(核心CPI)
              / CN.PPI_YOY / CN.PPI_CPI_SCISSOR(派生)
    经济周期  CN.PMI_MFG / CN.PMI_NONMFG / CN.PMI_NEW_ORDERS(新订单分项) / CN.IP_YOY(工业增加值)
              / CN.INDUSTRIAL_PROFIT_YOY(工业企业利润，累计口径) / CN.GDP_YOY / CN.GDP
              / OECD CLI（CN/US/JP/DE/KR + G7/G20，源：OECD SDMX）
    市场/实体 CN.MARKET_CAP(沪深市价总值，巴菲特指标分子) / CN.ELECTRICITY_YOY(用电量)
              / CN.POWER_GEN_YOY(发电量) / CN.FREIGHT_YOY(货运量) / CN.CONSUMER_CONFIDENCE
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

from db import get_conn
from regime_schema import upsert_macro_series, seed_series_meta, load_series_defs

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
_NBS_TREE_CACHE: dict = {}   # (pid, code) → 子节点；多序列共用一条路径时不重复下钻
_NBS_IND_CACHE: dict = {}    # cid → 指标清单；同叶子的多个指标复用一次请求


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
    """目录树一级子节点（code=1 月度数据）；pid='' 取顶层。

    带进程内缓存：CLI/核心CPI/发电量/利润/PMI… 多个序列各自下钻同一条路径，
    不缓存就会「每个序列重走一遍树」（5 个序列 ≈ 20 次请求）→ 既慢又更容易触发反爬限流。
    """
    key = (pid, code)
    if key not in _NBS_TREE_CACHE:
        j = _nbs_json("GET", f"{NBS_API}/new/queryIndexTreeAsync",
                      params={"pid": pid, "code": code})
        _NBS_TREE_CACHE[key] = j.get("data") or []
    return _NBS_TREE_CACHE[key]


def _nbs_indicators(cid: str) -> list:
    """某目录叶子下的指标清单（含官方名 i_showname 与指标 id _id）。同样带缓存。"""
    if cid not in _NBS_IND_CACHE:
        j = _nbs_json("GET", f"{NBS_API}/new/queryIndicatorsByCid",
                      params={"cid": cid, "dt": "", "name": ""})
        _NBS_IND_CACHE[cid] = (j.get("data") or {}).get("list") or []
    return _NBS_IND_CACHE[cid]


def _nbs_esdata(cid: str, indicator_ids: list, root_id: str, dts: list) -> list:
    """取序列（POST stream/esData）→ [{code: '202608MM', values: [{value: '101.0'}]}]。"""
    body = {"cid": cid, "indicatorIds": indicator_ids, "daCatalogId": "",
            "das": [{"text": "全国", "value": "000000000000"}],
            "showType": "1", "dts": dts, "rootId": root_id}
    return (_nbs_json("POST", f"{NBS_API}/stream/esData", json=body) or {}).get("data") or []


def _nbs_period(code):
    """期间码 → period_date（月首日）。

    实测两种编码：月度 `202608MM`、季度 `202603SS`（季度码用 **2 位季度号**）。
    季度按「该季度**首月**」落库（Q1→01-01、Q2→04-01、Q3→07-01、Q4→10-01），
    与项目既有季度口径（`_parse_period` 的「末季度起始月」）一致。
    """
    t = str(code or "")
    m = re.fullmatch(r"(\d{4})(\d{2})MM", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return datetime.date(y, mo, 1) if 1 <= mo <= 12 else None
    q = re.fullmatch(r"(\d{4})(\d{2})SS", t)
    if q:
        y, qq = int(q.group(1)), int(q.group(2))
        return datetime.date(y, (qq - 1) * 3 + 1, 1) if 1 <= qq <= 4 else None
    return None


def _nbs_dts(spec: dict) -> list:
    """期间参数 —— **必须显式给**：`dts=""` 时接口只返回默认窗口
    （月度只回最近 9 个月，实测核心 CPI 因此只回来 17 条、首段被截断）。

    ⚠ 月度与季度的传法**不同**（实测）：
      · 月度 → 区间串 `["199001MM-202608MM"]`（27 年跨度可一次返回 324 个月，无分页上限）；
      · 季度 → **必须逐期枚举** `["201101SS","201102SS",…]`：区间串（含 `SS/QQ` 各种写法）一律 500。
    """
    today = datetime.date.today()
    if spec.get("quarterly"):
        y0 = int(spec.get("dts_from", "1990"))
        return [f"{y}{q:02d}SS" for y in range(y0, today.year + 1) for q in (1, 2, 3, 4)]
    start = spec.get("dts_from", "200001")
    end = spec.get("dts_to") or today.strftime("%Y%m")
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
        # 指标名用**前匹配**（startswith）：实测「货运量_同比增长」正是「铁路货运量_同比增长」
        #   的子串，用 `in` 会把铁路/公路/水运/民航分品种一并命中并混算成总量（发电量目录同理）。
        ids = [x["_id"] for x in _nbs_indicators(leaf["_id"])
               if str(x.get("i_showname") or "").strip().startswith(spec["indicator"])]
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
    if hit_seg == 0:
        # 全时段都没匹配到 → 明确报错，避免「静默空序列」被误读成「今天没新数据」
        raise RuntimeError(f"NBS「{spec['indicator']}」在 {len(leaves)} 个叶子中均未匹配"
                           f"（官方名可能已变，请核对 i_showname 前缀）")
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


# ── 央行「社会融资规模」直连（PBoC 官网，替代已停更的 akshare 商务部镜像）─────────
# 背景：`macro_china_shrzgm`（社融增量）上游是商务部镜像，**冻结于 2026-04**，
#   而它正是 MACRO.CREDIT_IMPULSE 的输入 → 指标值被冻结在 −3.21（「静默失效」，最危险的一类）。
# 央行官网路径（2026-10-09 实测）：
#   ① 统计数据索引页 `/diaochatongjisi/116219/116319/index.html`（UTF-8）列出各年目录，
#      每年下挂「社会融资规模」子页 → 页内条目含 **htm/xls/pdf 附件**；
#   ② 抓 **htm** 附件即可解析（无需 Excel 库）；每月发布一份、**每份覆盖当年 1-12 月**；
#   ③ 年份子页 URL **除当年外是随机 ID**（`/116219/116319/5570903/5570885/`），当年页才是
#      `/{年}ntjsj/shrzgm/` → 故一律从索引页**就近配对**解析，勿硬编码。
# ⚠ 三个实测坑：
#   · 附件编码**新旧不一**（2016-2019 是 UTF-8、2020+ 是 GBK）→ 需自动判定；
#   · 表格是「项目行 × 月份列」宽表，每月份占 **2 列**（存量 / 增速%）→ 按**位置**配对，
#     不可先过滤空值再配对（会错位）；
#   · **口径断点**：2018-12 及更早为旧口径（不含国债/地方政府债），2019-01 起新口径
#     → 水平值跨 2019 差分会出现假跳变（实测 2018-12 192.37 → 2019-01 231.55 万亿元）。
PBC_BASE = "http://www.pbc.gov.cn"
PBC_INDEX = f"{PBC_BASE}/diaochatongjisi/116219/116319/index.html"
_PBC_CACHE: dict = {}


def _pbc_get(url: str, attach: bool = False) -> str:
    """取页面/附件文本。页面是 UTF-8；附件编码新旧不一 → 用关键字自动判定。"""
    import requests
    r = requests.get(url, timeout=40, verify=False, headers={
        "User-Agent": NBS_UA, "Accept": "text/html,application/xhtml+xml,*/*"})
    r.raise_for_status()
    if not attach:
        return r.content.decode("utf-8", "ignore")
    for enc in ("gb18030", "utf-8"):
        t = r.content.decode(enc, "ignore")
        if "社会融资规模存量" in t or "社会融资规模增量" in t:
            return t
    return r.content.decode("utf-8", "ignore")


def _pbc_year_map() -> dict:
    """{年份: 社融子页 URL} —— 从索引页把「社会融资规模」链接配到**最近的**年份标签上。"""
    if "years" in _PBC_CACHE:
        return _PBC_CACHE["years"]
    idx = _pbc_get(PBC_INDEX)
    ypos = [(int(m.group(1)), m.start()) for m in re.finditer(r"(\d{4})年统计数据", idx)]
    spos = [(m.group(1), m.start()) for m in
            re.finditer(r"href=[\"']([^\"']+)[\"'][^>]*>\s*社会融资规模\s*</a>", idx)]
    out, seen = {}, set()
    for href, sp in spos:
        prev = [y for y, yp in ypos if yp < sp]
        if prev and prev[-1] not in seen:
            seen.add(prev[-1])
            out[prev[-1]] = href
    _PBC_CACHE["years"] = out
    return out


def _pbc_entries(url: str) -> list:
    """子页条目 → [(条目名, htm 附件 URL)]（按 `titp20` 分块，块内取首个 .htm）。"""
    t = _pbc_get(url)
    out = []
    # 标题块的分隔属性**单双引号都可能出现**（老页面 style 不同）→ 两者都接受
    for part in re.split(r"class=[\"']titp20[\"']", t)[1:]:
        name = re.sub(r"[^\u4e00-\u9fa5]", "", part.split("<")[0].split(">")[-1])
        m = re.search(r"href=[\"']([^\"']+\.htm)[\"']", part)
        out.append((name, m.group(1) if m else None))
    return out


def _pbc_rows(txt: str) -> list:
    """htm 表 → [[单元格文本, ...], ...]（去掉标签与 &nbsp;，**保留空单元格占位**）。"""
    rows = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", txt, re.S):
        cells = [re.sub(r"<[^>]+>|&nbsp;|\s+", "", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        rows.append(cells)
    return rows


def _pbc_parse(txt: str, kind: str) -> dict:
    """社融 htm → {(年, 月): 值}。

    · 存量/存量同比 = 「项目行 × 月份列」宽表：总量行 = 首格以「社会融资规模存量」开头，
      表头行 = 含 ≥6 个 `YYYY.M` 的单元格；每月份占 2 列（存量、增速）→ **按位置**取。
    · 增量 = 「月份行 × 项目列」：首格为 `YYYY.M`，第一个数值列即社会融资规模增量合计（亿元）。
    """
    out = {}
    rows = _pbc_rows(txt)
    if kind == "flow":
        for cells in rows:
            m = re.fullmatch(r"(\d{4})\.(\d{1,2})", cells[0] if cells else "")
            if m and len(cells) > 1 and cells[1]:
                try:
                    out[(int(m.group(1)), int(m.group(2)))] = float(cells[1])
                except ValueError:
                    pass
        return out
    hdr, tot = None, None
    for cells in rows:
        months = [c for c in cells if re.fullmatch(r"\d{4}\.\d{1,2}", c)]
        if len(months) >= 6:
            hdr = months
        if cells and cells[0].startswith("社会融资规模存量") and len(cells) > 10:
            tot = cells[1:]                       # 保留空位 → 按位置配对不错位
    if not hdr or not tot:
        return out
    idx = 0 if kind == "stock" else 1             # 0=存量 1=增速
    for i, mk in enumerate(hdr):
        if 2 * i + 1 >= len(tot) or not tot[2 * i + idx]:
            continue
        try:
            y, mo = (int(v) for v in mk.split("."))
            out[(y, mo)] = float(tot[2 * i + idx])
        except ValueError:
            continue
    return out


def _pbc_fetch(spec: dict) -> dict:
    """央行社融序列 → {period_date: value}。按年份升序抓取，**后年文件覆盖前年**（源会修订）。"""
    kind = spec["kind"]
    target = "存量统计表" if kind.startswith("stock") else "增量统计表"
    out = {}
    ymap = _pbc_year_map()
    used = 0
    for y in sorted(ymap):
        if y < spec.get("from_year", 2016):
            continue
        htm = [h for nm, h in _pbc_entries(PBC_BASE + ymap[y])
               if nm.startswith("社会融资规模" + target) and h]
        if not htm:
            continue
        pts = _pbc_parse(_pbc_get(PBC_BASE + htm[0], attach=True), kind)
        used += 1
        scale = spec.get("scale", 1.0)          # 存量源为万亿元 → ×10000 统一为亿元
        for (yy, mm), v in pts.items():
            out[datetime.date(yy, mm, 1)] = round(v * scale, 4)
    print(f"    · 央行社融「{target}」：解析 {used} 个年度文件，共 {len(out)} 个月")
    return dict(sorted(out.items()))


# ── 序列定义：code / 名称 / 接口 / 取值列 / 单位 / 频率 / 发布滞后(天)
SERIES = [
    # 货币与信用
    # ⚠ 社能源侧停更（实测 2026-10-08）：akshare `macro_china_shrzgm` 上游是**商务部镜像**
    #   （data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery），最新只到 2026-04；
    #   akshare 全库无其它新鲜替代（已枚举 85 个 macro_china_* 接口核对）。
    #   → 历史可用、更新停摆；新鲜度替代用 CN.NEW_LOAN（新增人民币贷款，更新至 2026-08）。
    #   换源（央行官网直连）列为 P1。
    dict(code="CN.SHRZGM_INC", name="社会融资规模增量(月,旧源)", func="macro_china_shrzgm",
         col="社会融资规模增量", unit="yi_yuan", freq="month", lag=45,
         remark="⚠ 源（商务部镜像）停更于 2026-04 → **已被央行直连的 CN.TSF_FLOW_M 取代**"
                "（见 §7.24）；本序列仅保留历史，勿再用于新计算"),
    dict(code="CN.SHRZGM_RMB_LOAN", name="社融-人民币贷款(月,旧源)", func="macro_china_shrzgm",
         col="其中-人民币贷款", unit="yi_yuan", freq="month", lag=45,
         remark="⚠ 源停更于 2026-04；分项可由 CN.TSF_* 派生，暂保留历史"),
    # —— 央行官网直连（换源落地，见文首 PBC_* 说明）——
    # 社融**存量**是本轮换源的核心：MACRO.CREDIT_IMPULSE 原来只能拿「增量」做代理，
    # 且增量源已停更 → 现用存量/GDP 的 BIS 口径重算（见 market_state_daily._credit_impulse）。
    dict(code="CN.TSF_STOCK", name="社会融资规模存量(央行)", unit="yi_yuan", freq="month", lag=45,
         source="pbc:social-financing",
         remark="央行「社会融资规模存量统计表」（源单位万亿元 → ×10000 统一为亿元）。"
                "⚠ **口径断点**：2018-12 及更早为旧口径（不含国债/地方政府债），2019-01 起新口径"
                "→ 水平值**不可跨 2019 直接差分**（实测 2018-12 = 192.37 → 2019-01 = 231.55 万亿元）；"
                "实测 2016-01 起可得（其中 2018 年仅 7-12 月、2015 年页无 htm）",
         pbc=dict(kind="stock", from_year=2016, scale=10000)),
    dict(code="CN.TSF_STOCK_YOY", name="社会融资规模存量同比(央行)", unit="pct", freq="month", lag=45,
         source="pbc:social-financing",
         remark="同表「增速（%）」列；源按**当期口径**重算上年同期 → 同比序列跨口径断点的可比性"
                "好于水平值（水平值仍不可跨断点差分）",
         pbc=dict(kind="stock_yoy", from_year=2016)),
    dict(code="CN.TSF_FLOW_M", name="社会融资规模增量-当月(央行)", unit="yi_yuan", freq="month", lag=45,
         source="pbc:social-financing",
         remark="央行「社会融资规模增量统计表」当月合计（亿元）；**替换已停更的 CN.SHRZGM_INC**"
                "（其源为商务部镜像、冻结于 2026-04）",
         pbc=dict(kind="flow", from_year=2016)),
    dict(code="CN.NEW_LOAN", name="新增人民币贷款(月)", func="macro_china_new_financial_credit",
         col="当月", unit="yi_yuan", freq="month", lag=45),
    dict(code="CN.M2_YOY", name="M2 同比", func="macro_china_money_supply",
         col="货币和准货币(M2)-同比增长", unit="pct", freq="month", lag=45),
    dict(code="CN.M1_YOY", name="M1 同比", func="macro_china_money_supply",
         col="货币(M1)-同比增长", unit="pct", freq="month", lag=45),
    # M0（现金）：同一接口已有该列，无需另找源。
    # 注：NBS「金融 → 货币供应量」也有 M0 同比，但实测其表**比本接口晚一个月**
    #   （2026-10-09 时 NBS 只到 2026-07，本接口已到 2026-08），故 M0 一并走本接口；
    #   两者重叠月逐月一致（2026-07 = 11.6% ✓ 已交叉验证）。
    dict(code="CN.M0_YOY", name="M0 同比", func="macro_china_money_supply",
         col="流通中的现金(M0)-同比增长", unit="pct", freq="month", lag=45,
         remark="M0=流通中现金；与 M1/M2 同源同表（akshare macro_china_money_supply）；"
                "M0 高增常伴随后续 M1 回升（资金活化前奏）"),
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
    # PMI 新订单（最领先的分项）：akshare 的 macro_china_pmi 只有总指数（列：月份/制造业-指数/
    # 制造业-同比增长/非制造业-指数/非制造业-同比增长），**无分项** → 走 NBS 月度库
    # 「采购经理指数 → 制造业采购经理指数 → 新订单指数 (%)」。
    dict(code="CN.PMI_NEW_ORDERS", name="制造业 PMI 新订单指数", unit="index", freq="month", lag=45,
         source="nbs:stream/esData",
         remark="PMI 中最领先的成分（订单先于生产）；50=荣枯线。实测 2005-01 起 261 个月、"
                "最新 **2026-09**；与 CN.PMI_MFG（总指数）互为验证",
         nbs=dict(path=["月度数据", "采购经理指数"],
                  leaf_prefix="制造业采购经理指数", indicator="新订单指数")),
    dict(code="CN.IP_YOY", name="工业增加值同比", func="macro_china_gyzjz",
         col="同比增长", unit="pct", freq="month", lag=45, use_release_col="发布时间"),
    # 工业企业利润（A 股盈利的同步指标）。原判「源不可得」→ 2026-10-09 经 NBS 月度库落地。
    # ⚠ **累计口径**：NBS 该表只有累计值/累计增长（无单月），故 value = **年初至今累计同比**，
    #   与 CN.IP_YOY（单月同比）**不可直接相减**；月度变化里含累计基数效应。
    # ⚠ 缺口：1 月每年都缺（1-2 月合并发布）；2007-2010 仅 2/5/8/11 月有值（该期为准季度发布）。
    dict(code="CN.INDUSTRIAL_PROFIT_YOY", name="工业企业利润总额累计同比", unit="pct",
         freq="month", lag=45, source="nbs:stream/esData",
         remark="⚠ **累计口径**（年初至今累计同比，非单月）→ 勿与 CN.IP_YOY（单月同比）直接相减；"
                "缺口：**1 月每年缺**（1-2 月合并发布），2007-2010 仅 2/5/8/11 月有值；"
                "实测 2000-02 起 265 个月（最新 2026-08 = 15.7%）；A 股盈利的同步验证",
         nbs=dict(path=["月度数据", "工业"],
                  leaf_prefix="工业企业主要经济指标", indicator="利润总额累计增长")),
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
    # ⚠ lag 由 100 调为 **110**（2026-10-09）：实测 NBS 季度 GDP 于**季后次月 15-20 日**发布
    #   （Q3 数据 ≈ 10-18），距 period_date（季度首月 1 日）约 105-110 天 → lag=100 会提前 5-10 天
    #   视为可得（轻微前视）。属 PIT 收紧，历史 release_time 一并前移。
    dict(code="CN.GDP_YOY", name="GDP 同比(累计口径)", func="macro_china_gdp",
         col="国内生产总值-同比增长", unit="pct", freq="quarter", lag=110,
         remark="年内累计同比；period_date=末季度起始月（H1 记 04-01）"),
    dict(code="CN.GDP", name="GDP 绝对值(季, 年内累计)", func="macro_china_gdp",
         col="国内生产总值-绝对值", unit="yi_yuan", freq="quarter", lag=110,
         remark="年内累计绝对值（非单季）；period_date=末季度起始月"),
    # NBS 季度库补充（产出缺口 / 美林时钟的输入；2026-10-09 落地）：
    #   · 环比增长速度 = **已季调**的单季环比 %（NBS 自算 SA）→ 可直接链成 SA 实际水平；
    #   · 单季同比    = 「指数(上年同期=100) 当季值」− 100 → **单季**口径，比 CN.GDP_YOY 的
    #     年内累计口径更干净，且历史长得多（1993Q1 起）。
    dict(code="CN.GDP_QOQ", name="GDP 环比增速(季调, 单季)", unit="pct", freq="quarter", lag=110,
         source="nbs:stream/esData",
         remark="NBS 已季调的单季环比（官方口径）；2011Q1 起 62 个季度。"
                "→ 链成 SA 实际水平后做产出缺口（MACRO.OUTPUT_GAP）",
         nbs=dict(path=["季度数据", "国民经济核算"], leaf_prefix="国内生产总值环比增长速度",
                  indicator="国内生产总值环比增长速度 (%)", code="2", quarterly=True)),
    dict(code="CN.GDP_YOY_Q", name="GDP 单季实际同比", unit="pct", freq="quarter", lag=110,
         source="nbs:stream/esData",
         remark="NBS「国内生产总值指数(上年同期=100) 当季值」− 100；**单季**实际同比"
                "（区别于 CN.GDP_YOY 的年内累计口径）；1993Q1 起 134 个季度",
         nbs=dict(path=["季度数据", "国民经济核算"], leaf_prefix="国内生产总值指数",
                  indicator="国内生产总值指数 (上年同期=100) 当季值", minus=100,
                  code="2", quarterly=True)),
    # 实际 GDP 水平（不变价）：用户指出的「国民经济核算 → 国内生产总值」节点。
    # ⚠ **未季调**水平 → 不能直接做产出缺口（见 remark 的实测证据）；产出缺口仍用已季调的环比链。
    dict(code="CN.GDP_REAL_Q", name="GDP 实际水平(不变价, 单季)", unit="yi_yuan", freq="quarter",
         lag=110, source="nbs:stream/esData",
         remark="NBS「国内生产总值(不变价) 当季值」；2007Q1 起 78 季；**未季调** → 直接 HP 会把季节性"
                "当周期，而 4 季滚动和会在危机时**摊平冲击**（实测 2020 疫情低点：已季调环比链 −9.55% "
                "vs 本序列 4 季滚动和 −3.53%）→ 产出缺口口径**继续用 CN.GDP_QOQ 链**，本序列作水平参考/敏感性对照",
         nbs=dict(path=["季度数据", "国民经济核算"], leaf_prefix="国内生产总值 (不变价)",
                  indicator="国内生产总值 (不变价) 当季值", code="2", quarterly=True)),
    # 市场与实体
    dict(code="CN.MARKET_CAP_SH", name="沪深市价总值-上海", func="macro_china_stock_market_cap",
         col="市价总值-上海", unit="yi_yuan", freq="month", date_col="数据日期", lag=45),
    dict(code="CN.MARKET_CAP_SZ", name="沪深市价总值-深圳", func="macro_china_stock_market_cap",
         col="市价总值-深圳", unit="yi_yuan", freq="month", date_col="数据日期", lag=45),
    dict(code="CN.ELECTRICITY_YOY", name="全社会用电量同比", func="macro_china_society_electricity",
         col="全社会用电量同比", unit="pct", freq="month", date_col="统计时间", lag=45,
         remark="源「统计时间」为点分隔（如 2026.8）；历史曾因解析失败被压成年度，已修；"
                "⚠ 与发电量同属 NBS 月度口径 → 存在**1–2 月合并发布**造成的固有缺口"
                "（240 行/273 个月跨度），滚动窗口须按缺失处理"),
    # 货运量（实体经济的另一高频验证；源按「统计对象」分口径，必须显式取「合计」）
    dict(code="CN.FREIGHT_YOY", name="货运量同比(合计)", func="macro_china_society_traffic_volume",
         col="货运量同比增长", unit="pct", freq="month", date_col="统计时间", lag=45,
         row_filter={"统计对象": "合计"}),
    dict(code="CN.CONSUMER_CONFIDENCE", name="消费者信心指数", func="macro_china_xfzxx",
         col="消费者信心指数-指数值", unit="index", freq="month", lag=45),
    # 发电量（NBS「能源 → 能源主要产品产量 → 发电量 → 发电量同比增长」；2026-10-09 实测 2000-02 起 280 个月）
    # 【口径要点】该目录下同时有「发电量」「火力发电量」「水力/核能/风力/太阳能发电量」共 6 个平级叶子，
    #   故 leaf_prefix 必须用**前匹配**（startswith）而非包含匹配——否则「火力发电量」等会被一并取回并混算。
    dict(code="CN.POWER_GEN_YOY", name="发电量同比", unit="pct", freq="month", lag=45,
         source="nbs:stream/esData",
         remark="⚠ 与 CN.ELECTRICITY_YOY（全社会用电量）**口径不同、不可互替**：本序列是「规模以上"
                "工业发电量」，**不含规模以下/分布式光伏与自备电厂**；实测 2026-08 二者背离 5.1pp"
                "（发电量 −0.8% vs 用电量 +4.3%），全样本符号一致率 88%、相关 0.63。"
                "**实体活跃度优先看用电量**（需求侧、覆盖全社会），发电量作供给侧对照；"
                "⚠ 另有**1–2 月合并发布**的固有缺口（实测缺 1 月 26 个 / 2 月 12 个，另有 2012-06 "
                "一次源侧缺失）：280 行/319 个月跨度 → 做滚动窗口/环比时须按缺失处理，**勿补 0**",
         nbs=dict(path=["月度数据", "能源", "能源主要产品产量"],
                  leaf_prefix="发电量", indicator="发电量同比增长")),
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


_CONTRACT_KEYS = ("func", "col", "date_col", "nbs", "sdmx", "pbc",
                  "use_release_col", "row_filter", "release_from_date")
"""序列定义中属于「采集契约」的键 → 存 macro_series_meta.collect_params。

存下来是为了让 `macro_series_meta` 成为序列注册表的唯一真相源：从库里就能重建出完整
的采集定义（见 _load_defs），改契约不必翻代码。"""


def _contract_params(defn: dict):
    """序列定义 → 采集契约参数（JSONB）。无契约键时返回 None。"""
    p = {k: defn[k] for k in _CONTRACT_KEYS if defn.get(k) is not None}
    return p or None


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
        # 采集契约 + 口径备注 → 维度表 macro_series_meta（不逐行重复）
        "lag_days": defn["lag"], "collect_params": _contract_params(defn),
        "remark": defn.get("remark"),
        "release_time": rel, "revision": 0,
        "extra": {"release_basis": basis},
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
    if defn.get("pbc"):
        # 央行「社会融资规模」直连（htm 附件解析；见文首 PBC_* 说明）
        return [_mk_row(defn, d, v) for d, v in _pbc_fetch(defn["pbc"]).items()]
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
                "lag_days": d.get("lag", 45), "collect_params": {"expr": f"{a} - {b}"},
                "remark": d.get("remark"),
                "revision": 0, "extra": {"release_basis": "period+45d"},
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


def _seed_series_meta(conn) -> int:
    """把代码 SERIES 的定义播种进 macro_series_meta（只补空缺，不覆盖；见 seed_series_meta）。"""
    return seed_series_meta(conn, [{
        "series_code": d["code"], "series_name": d["name"], "unit": d["unit"],
        "freq": d["freq"], "market": d.get("market") or d["code"].split(".")[0],
        "source": d.get("source") or f"akshare:{d.get('func')}",
        "lag_days": d["lag"], "collect_params": _contract_params(d),
        "remark": d.get("remark"),
    } for d in SERIES])


def _load_defs(conn) -> list:
    """从 macro_series_meta 重建采集定义（**DB 为唯一真相源**）。

    · 代码 SERIES 只决定「有哪些序列 + 顺序」；
    · 库里有该序列 → 库的值覆盖代码（契约/单位/备注/滞后）；库里没有 → 用代码兜底。
    """
    db = load_series_defs(conn, codes=[d["code"] for d in SERIES])
    out = []
    for d in SERIES:
        m = db.get(d["code"])
        if not m:
            out.append(d)
            continue
        merged = dict(d)
        for k, v in (("name", m["name"]), ("unit", m["unit"]), ("freq", m["freq"]),
                     ("market", m["market"]), ("source", m["source"]),
                     ("lag", m["lag_days"]), ("remark", m["remark"])):
            if v is not None:                     # 库里为空的字段不覆盖代码
                merged[k] = v
        merged.update(m["params"])                # 采集契约参数（func/col/nbs/sdmx/pbc...）
        out.append(merged)
    return out


def run(codes=None, ctx=None, dry_run: bool = False):
    """采集入口：全量刷新（幂等；月频数据量小）。"""
    global _SDMX_AREAS
    with get_conn() as conn:
        _seed_series_meta(conn)
        defs = _load_defs(conn)
    # OECD CLI 一次请求取全部经济体 → 区域并集须按**最终生效的定义**重算（库可覆盖 ref_area）
    _SDMX_AREAS = [d["sdmx"]["ref_area"] for d in defs if d.get("sdmx")]
    cache: dict = {}
    all_rows = []
    print("  ── 原始序列 ──")
    for defn in defs:
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
        upsert_macro_series(conn, all_rows)
        print(f"  ── 派生序列 ──")
        drows = _build_derived(conn)
        if drows:
            upsert_macro_series(conn, drows)
        print(f"  ✅ 写入 {len(all_rows)} + 派生 {len(drows)} 条")
        with conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT series_code), count(*), max(updated_at)
                           FROM regime.v_macro_series WHERE source LIKE 'akshare:macro%'
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
