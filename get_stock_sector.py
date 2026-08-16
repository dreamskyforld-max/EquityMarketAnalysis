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


def run(codes=None, ctx=None):
    """采集入口（常驻调用）。codes 未使用；ctx 为共享行情上下文（港股部分需要）。"""
    from futu import OpenQuoteContext
    _ensure_sector_table()
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
    if ctx is not None:
        # 1) 宽基/行业指数成分（恒生指数/恒生科技/恒生国企等）
        print(f"[stock_sector] 开始采集 {len(HK_INDEX_TARGETS)} 个港股宽基指数…")
        for t in HK_INDEX_TARGETS:
            rows = _fetch_hk_index(ctx, t)
            total += _save_sector_rows(rows, "futu")
        # 2) 全市场板块（行业/概念/地域）——枚举后逐板块拉成分
        plates = _enum_hk_plates(ctx)
        print(f"[stock_sector] 港股共枚举到 {len(plates)} 个板块，开始逐板块拉成分…")
        for i, (pcode, pname, ptype) in enumerate(plates, 1):
            rows = _fetch_hk_plate(ctx, pcode, pname, ptype)
            total += _save_sector_rows(rows, "futu")
            if i % 25 == 0 or i == len(plates):
                print(f"[stock_sector] 板块进度 {i}/{len(plates)}，累计写入 {total} 行")
    else:
        log.warning("未提供 futu ctx，跳过港股采集（A股指数仍会采集）")

    # A股（akshare，本机可跑）
    print(f"[stock_sector] 开始采集 {len(A_INDEX_TARGETS)} 个 A股指数…")
    for t in A_INDEX_TARGETS:
        rows = _fetch_a_index(t)
        total += _save_sector_rows(rows, "akshare")

    if own_ctx and ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
    print(f"[stock_sector] 完成：本次写入/更新 {total} 行")


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
    if "--list-hk" in sys.argv:
        _list_hk_plates()
    else:
        run()
