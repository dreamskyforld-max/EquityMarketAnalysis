#!/usr/bin/env python3
"""
股票-板块归属采集 — 反向建表（已知板块/指数 → 全成分）

数据源：
  港股（尽量全）：FutuOpenD
    1) 宽基/行业指数成分（恒生指数/恒生科技/恒生国企等）
       —— 用 get_plate_stock(描述串或指数代码)，指数成分不走 get_plate_list 板块分类。
    2) 全市场板块分类（行业 / 概念 / 地域）
       —— 先用 get_plate_list(Market.HK, Plate.{INDUSTRY,CONCEPT,REGION}) 枚举全部
          板块 code，再逐个 get_plate_stock(板块code) 拉成分。返回 DataFrame 无板块类型列，
          故类型由枚举时的 plate_class 决定，单独记一列 sector_type。
  A 股指数：AKShare index_stock_cons_csindex / index_stock_cons_sina（本机可直接跑）
  A 股行业板块(INDUSTRY)：BaoStock 证监会行业分类（免费/不限流/稳定）
  A 股概念板块(CONCEPT)：AKShare 东方财富(em) stock_board_concept_*（偶发连接重置已加退避重试）

用法：
  python3 get_stock_sector.py                 # 默认跑全部（港股 + A股）
  python3 get_stock_sector.py --market A      # 仅跑 A股（含指数/行业/概念）
  python3 get_stock_sector.py --market HK     # 仅跑港股（需 futu OpenD）
  python3 get_stock_sector.py --list-hk       # 调试：枚举并打印港股全部板块

写入表：stock_sector（PK stock_code + sector_code，每只成分股一行；sector_type 区分 INDEX/INDUSTRY/CONCEPT/REGION）
  查询「腾讯属于哪些板块」：
      SELECT sector_code, sector_name, sector_type FROM stock_sector WHERE stock_code='HK.00700'
  统计板块数 / 每个板块成分数：
      SELECT sector_type, sector_code, sector_name, COUNT(*) AS stock_count
      FROM stock_sector GROUP BY sector_type, sector_code, sector_name
      ORDER BY sector_type, stock_count DESC

刷新策略：纯参考数据，低频刷新（scheduler 每周二 18:00）。
  每次刷新会先 DELETE 该 sector_code 旧行，再 bulk 写入，保证调出成分股被移除。

用法（独立运行兼容）：
    python3 get_stock_sector.py              # 采集全部（港股板块+指数 + A股指数）
    python3 get_stock_sector.py --list-hk    # 仅打印港股全部板块(code+name+type)，用于核对

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用。
"""
import sys
import os
import time
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("stock_sector")

# ── 港股宽基/行业指数白名单（指数成分，走 get_plate_stock 描述串/代码）──────
#   sector_code = 规范存储值；query = 实际传给 get_plate_stock 的参数
HK_INDEX_TARGETS = [
    {"name": "恒生指数",     "sector_code": "HK.800000", "query": "HK.HSI Constituent Stocks"},
    {"name": "恒生科技",     "sector_code": "HK.800700", "query": "HK.800700"},
    # 注意：HSCEI 必须用指数代码 HK.800100，不能用描述串 "HK.HSCEI Stock"——
    # 后者会返回过期的旧成分（缺美团/阿里/京东/网易等新纳入成分，47只而非50只）。
    {"name": "恒生中国企业", "sector_code": "HK.800100", "query": "HK.800100"},
]
A_INDEX_TARGETS = [
    {"name": "沪深300",  "sector_code": "000300", "source": "csindex"},
    {"name": "中证500",  "sector_code": "000905", "source": "csindex"},
    {"name": "上证50",   "sector_code": "000016", "source": "csindex"},
    {"name": "中证1000", "sector_code": "000852", "source": "csindex"},
    # 创业板指是深交所指数，中证源(csindex)不覆盖，改用 sina 源
    {"name": "创业板指", "sector_code": "399006", "source": "sina"},
]
# ── 港股全市场板块分类：分别枚举 行业/概念（尽量全）────────────────────────
#   key = 传给 get_plate_list 的 Plate 枚举名；value = 记入 stock_sector.sector_type 的值
#   注意：富途港股无 REGION（地域）板块分类，get_plate_list(HK,REGION) 返回空，故不枚举。
HK_PLATE_CLASSES = {
    "INDUSTRY": "INDUSTRY",   # 行业板块
    "CONCEPT":  "CONCEPT",     # 概念板块
}

_EXCH_PREFIX = {
    "上海证券交易所": "SH",
    "深圳证券交易所": "SZ",
    "北京证券交易所": "BJ",
}

# ── A股代码前缀（纯数字代码 → 交易所前缀）─────────────────────────────────
#   60xxxx/68xxxx(科创板) → SH；00xxxx/30xxxx(创业板)/20xxxx(B股) → SZ；
#   8xxxxx/92xxxx(北交所)/4xxxxx/9xxxxx → BJ
def _a_exch_prefix(raw: str) -> str | None:
    if not raw:
        return None
    if raw.startswith(("60", "68", "90", "5", "11", "113", "110")):  # 沪市（含沪市转债/B股）
        return "SH"
    if raw.startswith(("00", "30", "20", "12", "15", "16", "123", "127", "128")):  # 深市
        return "SZ"
    if raw.startswith(("8", "92", "4", "9")):  # 北交所 / 老三板
        return "BJ"
    return None


# ── A股：东方财富(em) 行业/概念板块（含成分）──────────────────────────────
#   本机网络对 em 源偶发 RemoteDisconnected，需退避重试。ths 源虽稳定但无成分股明细，
#   故行业/概念成分统一用 em 源 + 重试。
def _ak_retry(fn, n=5, wait=4, label="akshare", max_total=None):
    """AKShare em 源偶发连接重置，退避重试。

    max_total: 重试累计耗时上限（秒）；超过则放弃并抛出最后一次异常，避免限流时无限阻塞。
    """
    import time as _time
    last: Exception | None = None
    start = _time.time()
    for i in range(n):
        try:
            return fn()
        except Exception as e:  # RemoteDisconnected / ConnectionError 等
            last = e
            log.warning("%s 第 %d/%d 次失败: %s", label, i + 1, n, repr(e)[:120])
            if max_total is not None and (_time.time() - start) >= max_total:
                log.warning("%s 重试累计超 %.0fs，放弃", label, max_total)
                break
            if i < n - 1:
                _time.sleep(wait)
    if last is None:
        raise RuntimeError(f"{label} 重试循环未执行（n={n}）")
    raise last


def _fetch_a_board(plate_type):
    """采集 A股 行业(INDUSTRY)/概念(CONCEPT) 全市场板块及成分。

    返回 [(stock_code, sector_code, sector_name, sector_type), ...]
    sector_code 用东财板块代码（如 BK1616），sector_type = 'INDUSTRY' / 'CONCEPT'。
    """
    import akshare as ak
    if plate_type == "INDUSTRY":
        list_fn = lambda: ak.stock_board_industry_name_em()
        cons_fn = lambda sym: ak.stock_board_industry_cons_em(symbol=sym)
        stype = "INDUSTRY"
    else:
        list_fn = lambda: ak.stock_board_concept_name_em()
        cons_fn = lambda sym: ak.stock_board_concept_cons_em(symbol=sym)
        stype = "CONCEPT"

    try:
        board_list = _ak_retry(list_fn, label=f"a_{plate_type}_list", max_total=90)
    except Exception as e:
        log.warning("A股 %s 板块列表采集失败: %s", plate_type, repr(e)[:200])
        return []
    if board_list is None or board_list.empty:
        return []

    rows = []
    names = board_list.get("板块名称")
    codes = board_list.get("板块代码")
    if names is None or codes is None:
        log.warning("A股 %s 板块列表字段异常: %s", plate_type, list(board_list.columns))
        return []
    total = len(board_list)
    for i, (bname, bcode) in enumerate(zip(names, codes), 1):
        try:
            # 东财偶发限流：单板块重试累计不超过 60s 即跳过，避免整体阻塞
            cons = _ak_retry(lambda: cons_fn(bname), label=f"a_{plate_type}_cons:{bname}", max_total=60)
        except Exception as e:
            log.warning("A股 %s 成分(%s)失败: %s", plate_type, bname, repr(e)[:120])
            continue
        if cons is None or cons.empty:
            continue
        for _, r in cons.iterrows():
            raw = str(r.get("代码", "")).strip()
            prefix = _a_exch_prefix(raw)
            if not raw or not prefix:
                continue
            rows.append((f"{prefix}.{raw}", str(bcode), str(bname), stype))
        if i % 25 == 0 or i == total:
            log.info("A股 %s 板块进度 %d/%d", plate_type, i, total)
    log.info("A股 %s 板块共 %d 个，成分 %d 只", plate_type, total, len(rows))
    return rows


# ── A股：证监会行业分类（BaoStock，免费、不限流、稳定）──────────────────────
#   返回每只股票的证监会细分行业（如 C36汽车制造业）。代码已是 sh./sz./bj. 标准格式，
#   无需补前缀。行业分类体系 = 证监会行业分类（19 个一级大类字母 A-S）。
#   注：BaoStock 仅提供行业分类，不提供概念板块；概念板块仍走东财(em) _fetch_a_board。
def _fetch_a_industry_baostock():
    """采集 A股 证监会行业分类（股票 → 细分行业）。

    返回 [(stock_code, sector_code, sector_name, 'INDUSTRY'), ...]
    sector_code = 证监会行业代码（如 C36），sector_name = 行业名称（如 汽车制造业）。
    无行业标注的股票（B股/退市等）跳过。
    """
    import baostock as bs
    import re
    import contextlib

    rows = []
    # BaoStock 的 login/logout 用 print 直接打 stdout，重定向到 devnull 避免干扰进度日志
    _null = open(os.devnull, "w")
    try:
        lg = None
        for _attempt in range(3):
            with contextlib.redirect_stdout(_null):
                lg = bs.login()
            if lg.error_code == "0":
                break
            log.warning("[A股行业] BaoStock 登录第 %d/3 次失败: %s %s，重试…",
                        _attempt + 1, lg.error_code, lg.error_msg)
            time.sleep(2)
        if lg is None or lg.error_code != "0":
            log.warning("BaoStock 登录失败（已重试）: %s %s",
                        getattr(lg, "error_code", "?"), getattr(lg, "error_msg", ""))
            return []
        log.info("[A股行业] BaoStock 登录成功，开始拉取证监会行业分类…")
        with contextlib.redirect_stdout(_null):
            rs = bs.query_stock_industry()
        if rs.error_code != "0":
            log.warning("BaoStock 行业查询失败: %s %s", rs.error_code, rs.error_msg)
            return []
        total_rows = 0
        while (rs.error_code == "0") and rs.next():
            code, name, industry = rs.get_row_data()[1:4]  # code, code_name, industry
            total_rows += 1
            if not industry:  # 无行业标注（B股等）
                continue
            # industry 格式为「代码+中文」连写，如 'C37铁路、船舶、航空航天...制造业'
            # sector_code 取开头字母数字段（证监会行业代码，如 C37）；sector_name 用全称
            m = re.match(r"^([A-Za-z]\d+)", industry)
            sec_code = m.group(1) if m else industry[:10]
            # BaoStock 返回的 code 形如 sh.600000 / sz.000001（本身小写 + 自带 sh/sz 前缀）。
            # 统一转大写(UPPER)，对齐港股 HK.00700 及其它 A 股路径(SH./SZ./BJ.)，
            # 避免 stock_code 主键因大小写敏感出现 sh.600000 / SH.600000 两套、下游漏匹配。
            rows.append((code.upper(), sec_code, industry, "INDUSTRY"))
            if len(rows) % 1000 == 0:
                log.info("[A股行业] 已解析 %d 只…", len(rows))
        with contextlib.redirect_stdout(_null):
            bs.logout()
    finally:
        try:
            _null.close()
        except Exception:
            pass
    log.info("[A股行业] 采集完成：共 %d 只 A股，其中 %d 只有证监会行业标注", total_rows, len(rows))
    return rows


# ── 富途限速：每 30 秒最多 10 次 get_plate_stock / get_plate_list 调用 ──────
#   用固定间隔节流：每次调用保证距上一次 ≥ _RATE_INTERVAL 秒（留余量，不攒满再等）。
_RATE_INTERVAL = 3.2   # 10 次/30秒 → 至少 3 秒一次，取 3.2 留余量
_rate_last = [0.0]     # 模块级：上次调用时间戳（用 list 便于函数内改）


def _rate_limit():
    """富途接口限速：保证两次调用间隔 ≥ 3.2 秒（10 次/30秒 留余量）。"""
    elapsed = time.time() - _rate_last[0]
    if elapsed < _RATE_INTERVAL:
        time.sleep(_RATE_INTERVAL - elapsed)
    _rate_last[0] = time.time()


# ── 港股：富途板块/指数成分 ─────────────────────────────────────────────────
def _call_plate_stock(ctx, query, max_retry=4):
    """带限速 + 限频重试的 get_plate_stock 调用，返回 (ret, data)。"""
    from futu import RET_OK
    last_err = None
    ret = -1
    for attempt in range(max_retry):
        _rate_limit()
        ret, data = ctx.get_plate_stock(query)
        if ret == RET_OK:
            return ret, data
        last_err = data
        if isinstance(data, str) and "high frequency" in data.lower():
            # 触发限频：多等一轮再重试
            time.sleep(_RATE_INTERVAL * 2)
            continue
        break
    return ret, last_err


def _fetch_hk_index(ctx, target):
    """返回 [(stock_code, sector_code, sector_name), ...]

    用 get_plate_stock(query) 直接取指数成分（文档 v10.9：指数成分不走
    get_plate_list 板块分类，plate_code 传描述串或指数代码）。
    """
    from futu import RET_OK
    query = target["query"]
    ret, data = _call_plate_stock(ctx, query)
    if ret != RET_OK or data is None or data.empty:
        log.warning(f"get_plate_stock({query}) 失败: {data}")
        return []
    rows = []
    for _, r in data.iterrows():
        code = r.get("code")
        if code:
            rows.append((str(code), target["sector_code"], target["name"]))
    log.info(f"港股指数 {target['name']}({query}) 成分 {len(rows)} 只")
    return rows


# ── 港股：全市场板块枚举 + 成分 ──────────────────────────────────────────────
def _enum_hk_plates(ctx):
    """枚举港股全部板块（行业/概念/地域），返回 [(plate_code, plate_name, plate_type), ...]。

    get_plate_list 返回 DataFrame 仅有 code / plate_name / plate_id 三列，
    不含板块类型，故类型由本次请求的 plate_class 决定。逐类枚举并合并。
    """
    from futu import Market, Plate, RET_OK
    out = []
    for enum_name, plate_type in HK_PLATE_CLASSES.items():
        _rate_limit()
        try:
            plate_cls = getattr(Plate, enum_name)
        except AttributeError:
            log.warning(f"未知 Plate 枚举 {enum_name}，跳过")
            continue
        ret, data = ctx.get_plate_list(Market.HK, plate_cls)
        if ret != RET_OK or data is None or data.empty:
            log.warning(f"get_plate_list(HK,{enum_name}) 失败: {data}")
            continue
        for _, r in data.iterrows():
            code = r.get("code")
            name = r.get("plate_name")
            if code:
                out.append((str(code), str(name) if name is not None else "", plate_type))
        log.info(f"港股板块[{enum_name}] 枚举 {len(data)} 个")
    return out


def _fetch_hk_plate(ctx, plate_code, plate_name, plate_type):
    """返回 [(stock_code, sector_code, sector_name, sector_type), ...]

    用 get_plate_stock(板块code) 拉该板块全部成分股。板块类型由调用方传入。
    """
    from futu import RET_OK
    ret, data = _call_plate_stock(ctx, plate_code)
    if ret != RET_OK or data is None or data.empty:
        log.warning(f"get_plate_stock({plate_code}) 失败: {data}")
        return []
    rows = []
    for _, r in data.iterrows():
        code = r.get("code")
        if code:
            rows.append((str(code), plate_code, plate_name, plate_type))
    log.info(f"港股板块 {plate_name}({plate_code},{plate_type}) 成分 {len(rows)} 只")
    return rows


# ── A股：AKShare 中证成分 ───────────────────────────────────────────────────
def _fetch_a_index(target):
    """返回 [(stock_code, sector_code, sector_name), ...]

    source='csindex' 用 AKShare index_stock_cons_csindex（中证系列，含沪深300/中证500等）；
    source='sina'    用 index_stock_cons_sina（深交所等 csindex 不覆盖的指数，如创业板指），
                    其 symbol 列自带 sh/sz/bj 前缀，直接解析。
    """
    import time
    import akshare as ak
    src = target.get("source", "csindex")
    time.sleep(1)
    try:
        if src == "sina":
            df = ak.index_stock_cons_sina(symbol=target["sector_code"])
        else:
            df = ak.index_stock_cons_csindex(symbol=target["sector_code"])
    except Exception as e:
        log.warning(f"AKShare 指数成分({src},{target['sector_code']}) 失败: {e}")
        return []
    if df is None or df.empty:
        return []
    rows = []
    if src == "sina":
        for _, r in df.iterrows():
            sym = str(r.get("symbol", "")).lower()
            if len(sym) < 2:
                continue
            pre = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2])
            code = sym[2:]
            if pre and code:
                rows.append((f"{pre}.{code}", target["sector_code"], target["name"]))
    else:
        for _, r in df.iterrows():
            raw = str(r.get("成分券代码", "")).strip()
            exch = str(r.get("交易所", "")).strip()
            prefix = _EXCH_PREFIX.get(exch)
            if not raw or not prefix:
                continue
            rows.append((f"{prefix}.{raw}", target["sector_code"], r.get("指数名称", target["name"])))
    log.info(f"A股指数 {target['name']}({target['sector_code']},{src}) 成分 {len(rows)} 只")
    return rows


# ── 建表 ───────────────────────────────────────────────────────────────────
#   与 sql/schema.sql 中的 stock_sector 定义保持一致；缺失时先建表再写入。
_SECTOR_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_sector (
    stock_code      VARCHAR(20)     NOT NULL,        -- 股票完整代码，如 HK.00700 / SH.600000
    sector_code     VARCHAR(20)     NOT NULL,        -- 指数代码：港股用富途 plate code(如 HK.800000) / A股用中证代码(如 000300)
    sector_name     VARCHAR(100),                       -- 指数中文名，如 恒生科技 / 沪深300
    sector_type     VARCHAR(20)     DEFAULT 'INDEX',
    weight          NUMERIC(10,4),
    source          VARCHAR(20),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (stock_code, sector_code)
)
"""
def _ensure_sector_table():
    """确保 stock_sector 表存在（CREATE TABLE IF NOT EXISTS，幂等）。"""
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SECTOR_TABLE_SQL)
    log.info("stock_sector 表已确认存在")


def _normalize_lowercase_codes():
    """订正历史脏数据：stock_code 前缀 sh./sz./bj. 小写 → 大写 SH./SZ./BJ.。

    根因：旧版 _fetch_a_industry_baostock 用 code.lower() 入库，导致 A 股证监会行业
    分类行的 stock_code 为小写前缀，与港股 HK.00700 及其它 A 股路径(SH./SZ./BJ.) 不一致。
    stock_code 是大小写敏感字符串主键，会造成同一只 A 股出现两套主键、下游按 stock_code
    精确 join/聚合时漏匹配。本函数幂等，可每次 run 前安全调用。
    """
    from db import get_conn
    _MAP = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
    fixed = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for low, up in _MAP.items():
                cur.execute(
                    "UPDATE stock_sector "
                    "SET stock_code = %s || SUBSTRING(stock_code FROM 3), "
                    "    updated_at = updated_at "
                    "WHERE stock_code LIKE %s || '.%%'",
                    (up, low),
                )
                fixed += cur.rowcount
    if fixed:
        log.info("订正历史小写前缀 stock_code 共 %d 行（sh/sz/bj → SH/SZ/BJ）", fixed)
    return fixed


# ── 写入 ───────────────────────────────────────────────────────────────────
def _save_sector_rows(rows, source, default_type="INDEX"):
    """先按 sector_code 删除旧行，再 bulk 写入（保证调出成分被清理）。

    rows 元素可为：
      - 三元组 (stock_code, sector_code, sector_name)          → 用 default_type
      - 四元组 (stock_code, sector_code, sector_name, sector_type)
    """
    if not rows:
        return 0
    from db import get_conn, bulk_upsert
    sector_codes = sorted({r[1] for r in rows})
    data = []
    for r in rows:
        if len(r) == 4:
            sc, se, sn, st = r
        else:
            sc, se, sn = r
            st = default_type
        data.append({
            "stock_code": sc,
            "sector_code": se,
            "sector_name": sn,
            "sector_type": st,
            "weight": None,
            "source": source,
            "updated_at": datetime.now(),
        })
    with get_conn() as conn:
        with conn.cursor() as cur:
            for sc in sector_codes:
                cur.execute("DELETE FROM stock_sector WHERE sector_code = %s", (sc,))
        bulk_upsert(conn, "stock_sector", data, conflict_cols=["stock_code", "sector_code"])
    return len(data)


def _futu_available():
    try:
        import futu  # noqa
        return True
    except Exception:
        return False


def run(codes=None, ctx=None, market="ALL"):
    """采集入口（常驻调用）。

    codes  未使用；ctx 为共享行情上下文（港股部分需要）。
    market 'ALL'(默认) 跑港股+A股；'HK' 仅港股；'A' 仅 A股。
    """
    from futu import OpenQuoteContext
    market = (market or "ALL").upper()
    if market not in ("ALL", "HK", "A"):
        raise ValueError(f"未知 market={market!r}，仅支持 ALL/HK/A")
    _ensure_sector_table()
    # 幂等订正历史小写前缀脏数据（sh./sz./bj. → SH./SZ./BJ.）
    _normalize_lowercase_codes()
    own_ctx = False
    if ctx is None and _futu_available():
        try:
            ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
            own_ctx = True
        except Exception as e:
            log.warning(f"创建 futu 上下文失败，跳过港股指数: {e}")
            ctx = None

    total = 0
    # 港股（需要 futu）
    if market in ("ALL", "HK"):
        if ctx is not None:
            # 1) 宽基/行业指数成分（恒生指数/恒生科技/恒生国企等）
            log.info("开始采集 %d 个港股宽基指数…", len(HK_INDEX_TARGETS))
            for t in HK_INDEX_TARGETS:
                rows = _fetch_hk_index(ctx, t)
                total += _save_sector_rows(rows, "futu")
            # 2) 全市场板块（行业/概念/地域）——枚举后逐板块拉成分
            plates = _enum_hk_plates(ctx)
            log.info("港股共枚举到 %d 个板块，开始逐板块拉成分…", len(plates))
            for i, (pcode, pname, ptype) in enumerate(plates, 1):
                rows = _fetch_hk_plate(ctx, pcode, pname, ptype)
                total += _save_sector_rows(rows, "futu")
                if i % 25 == 0 or i == len(plates):
                    log.info("港股板块进度 %d/%d，累计写入 %d 行", i, len(plates), total)
        else:
            log.warning("未提供 futu ctx，跳过港股采集")
    else:
        log.info("market=%s，跳过港股采集", market)

    # A股（akshare / baostock，本机可跑）
    if market in ("ALL", "A"):
        log.info("开始采集 %d 个 A股指数…", len(A_INDEX_TARGETS))
        for t in A_INDEX_TARGETS:
            rows = _fetch_a_index(t)
            total += _save_sector_rows(rows, "akshare")

        # A股 行业板块（INDUSTRY）：证监会行业分类，BaoStock（免费/不限流/稳定）
        log.info("开始采集 A股 证监会行业分类(INDUSTRY)…")
        try:
            a_ind = _fetch_a_industry_baostock()
            if a_ind:
                total += _save_sector_rows(a_ind, "baostock", default_type="INDUSTRY")
            else:
                log.warning("A股 证监会行业分类采集为空（BaoStock 不可用或无数据），跳过该段")
        except Exception as e:
            log.warning("A股 证监会行业分类采集异常，跳过该段: %s", e)
        # A股 全市场概念板块（CONCEPT）：东财(em)，偶发限流已带退避重试
        log.info("开始采集 A股 概念板块(CONCEPT)…")
        a_con = _fetch_a_board("CONCEPT")
        total += _save_sector_rows(a_con, "akshare", default_type="CONCEPT")
    else:
        log.info("market=%s，跳过 A股采集", market)

    if own_ctx and ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
    log.info("完成（market=%s）：本次写入/更新 %d 行", market, total)


def _list_hk_plates():
    """调试：枚举并打印港股全部板块（按类型分页），用于确认板块代码与归属。"""
    from futu import OpenQuoteContext, Market, Plate, RET_OK
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        for enum_name, plate_type in HK_PLATE_CLASSES.items():
            try:
                plate_cls = getattr(Plate, enum_name)
            except AttributeError:
                print(f"未知 Plate 枚举 {enum_name}")
                continue
            _rate_limit()
            ret, data = ctx.get_plate_list(Market.HK, plate_cls)
            if ret != RET_OK or data is None or data.empty:
                print(f"[{enum_name}] 枚举失败:", data)
                continue
            print(f"\n=== {enum_name}（{plate_type}）共 {len(data)} 个 ===")
            print(data[["code", "plate_name"]].to_string(index=False))
    finally:
        ctx.close()


if __name__ == "__main__":
    import argparse

    _parser = argparse.ArgumentParser(description="股票-板块归属采集")
    _parser.add_argument(
        "--market", "-m", default="ALL", choices=["ALL", "HK", "A"],
        help="指定市场：ALL(默认)=港股+A股；HK=仅港股；A=仅 A股",
    )
    _parser.add_argument(
        "--list-hk", action="store_true", help="调试：枚举并打印港股全部板块",
    )
    _args = _parser.parse_args()

    if _args.list_hk:
        _list_hk_plates()
    else:
        run(market=_args.market)
