#!/usr/bin/env python3
"""
股票-指数成分归属采集 — 反向建表（已知指数 → 全成分）

数据源：
  - 港股指数：FutuOpenD get_plate_stock（部署机经 collector_runtime 调用）
      恒生指数 HSI = HK.800000（已知 benchmark 代码）
      恒生科技 / 恒生国企 等按名称模糊匹配 plate code，避免硬编码
  - A 股指数：AKShare index_stock_cons_csindex（中证指数公司，本机可直接跑）

写入表：stock_sector（PK stock_code + sector_code，每只成分股一行）
  查询「腾讯属于哪些指数」：SELECT sector_code, sector_name FROM stock_sector WHERE stock_code='HK.00700'

刷新策略：纯参考数据，仅在指数定期审核(季度/半年)后刷新一次即可。
  每次刷新会先 DELETE 该 sector_code 旧行，再 bulk 写入，保证调出成分股被移除。

用法（独立运行兼容）：
    python3 get_stock_sector.py              # 采集全部目标指数
    python3 get_stock_sector.py --list-hk    # 仅打印港股全部板块(含 code)，用于确认恒生科技等 plate code

常驻调用：run(codes, ctx) —— 由 market_scheduler 通过 collector_runtime 调用。
"""
import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("stock_sector")

# ── 目标指数白名单（按需增删）──────────────────────────────────────────────
# 港股：用富途 get_plate_stock，plate_code 传「描述串」或「指数代码」
#   （指数成分不走 get_plate_list 板块分类；参考 Futu 文档 v10.9）
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

_EXCH_PREFIX = {
    "上海证券交易所": "SH",
    "深圳证券交易所": "SZ",
    "北京证券交易所": "BJ",
}


# ── 港股：富途板块/指数成分 ─────────────────────────────────────────────────
def _fetch_hk_index(ctx, target):
    """返回 [(stock_code, sector_code, sector_name), ...]

    用 get_plate_stock(query) 直接取指数成分（文档 v10.9：指数成分不走
    get_plate_list 板块分类，plate_code 传描述串或指数代码）。
    注意频率限制：每 30 秒最多 10 次，故调用间 sleep 1s。
    """
    import time
    from futu import RET_OK
    query = target["query"]
    time.sleep(1)
    ret, data = ctx.get_plate_stock(query)
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


# ── 写入 ───────────────────────────────────────────────────────────────────
def _save_sector_rows(rows, source):
    """先按 sector_code 删除旧行，再 bulk 写入（保证调出成分被清理）。"""
    if not rows:
        return 0
    from db import get_conn, bulk_upsert
    sector_codes = sorted({s for _, s, _ in rows})
    data = [{
        "stock_code": sc,
        "sector_code": se,
        "sector_name": sn,
        "sector_type": "INDEX",
        "weight": None,
        "source": source,
        "updated_at": datetime.now(),
    } for sc, se, sn in rows]
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
        for t in HK_INDEX_TARGETS:
            rows = _fetch_hk_index(ctx, t)
            total += _save_sector_rows(rows, "futu")
    else:
        log.warning("未提供 futu ctx，跳过港股指数采集（A股指数仍会采集）")

    # A股（akshare，本机可跑）
    for t in A_INDEX_TARGETS:
        rows = _fetch_a_index(t)
        total += _save_sector_rows(rows, "akshare")

    if own_ctx and ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
    print(f"\n[stock_sector] 本次写入/更新 {total} 行")


def _list_hk_plates():
    """调试：打印港股全部板块 code+plate_name，用于确认指数/板块代码。"""
    from futu import OpenQuoteContext, Market, Plate, RET_OK
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, data = ctx.get_plate_list(Market.HK, Plate.ALL)
    ctx.close()
    if ret != RET_OK or data is None or data.empty:
        print("枚举港股板块失败:", data)
        return
    print(data[["code", "plate_name"]].to_string())


if __name__ == "__main__":
    if "--list-hk" in sys.argv:
        _list_hk_plates()
    else:
        run()
