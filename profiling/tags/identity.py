#!/usr/bin/env python3
"""① 证券属性标签域（12 个标签）

域定义：描述证券的**静态登记与归属**——「这是什么证券、在哪交易、属于哪个官方分类」。
判定标准：取值由交易所规则、监管分类、公司注册信息决定，**与股价、成交量、财务表现无关**。
这是它与其余所有域的根本分界（市值档、指数成分属 ② 规模属性，是时变量）。

已实现 10 个，2 个因数据源缺失标记为 planned_no_data（见文件末尾），
等数据补齐后把 status 改回 active 即可自动纳入计算。

═══ 枚举取值规范（重要）═══
本域所有 enum 标签遵循「一个概念 = 一个 code」：
  · key_value 存**规范 code**，中文只作 label（查 profile.enum_value），不参与比较与关联。
    这样「上交所 / 上海证券交易所 / SSE / SH」四种写法被收敛成唯一 XSHG。
  · 有外部权威体系的优先采用官方代码：交易所用 ISO 10383 MIC（XSHG/XSHE/XHKG/XBEI），
    GICS 用官方英文标识（INFORMATION_TECHNOLOGY）。
  · 已有权威表承载的值域（行业/概念/指数）不复制到字典，用 value_ref 声明引用。

数据来源：
    stock_info        股票基本信息（market / symbol / name / list_date / exchange_type）
    stock_sector      板块归属（INDUSTRY 行业 / INDEX 指数成分 / CONCEPT 概念）
    sector_hierarchy  行业 → GICS 一级部门映射（富途板块 HK.LIST* 与证监会行业码 A01/C39 均覆盖）

PIT 说明：stock_sector 只有当前快照（无历史归属版本），因此行业/指数/概念类标签
pit_capable=False——历史 as_of 无法还原当时的归属，重算结果一律以「当前归属」为准，
eff_from 记为计算日。这是刻意暴露的已知限制，不是 bug。
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from ..registry import tag, ENUM, BOOL, TIER, PLANNED_NO_DATA
from ._base import _conn, _read_sql, _frame

DOMAIN = "证券属性"

# ── 代码段规则（富途 exchange_type 缺失时的回退依据）────────────────────────
_SH_MAIN = ("600", "601", "603", "605")     # 沪市主板
_SZ_MAIN = ("000", "001", "002", "003")     # 深市主板（002/003 原中小板已并入）
_SZ_CHINEXT = ("300", "301", "302")         # 创业板
_STAR = ("688", "689")                      # 科创板（689 = 科创板 CDR）
_BJ = ("920",)                              # 北交所（920 代码段，富途前缀为 SH.920xxx）
_SH_B = ("900",)                            # 沪市 B 股
_SZ_B = ("200", "201")                      # 深市 B 股


def _table_columns(conn, table: str, schema: str = "public") -> set:
    """返回表已有列名集合（用于新列未补采时优雅降级）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


def _load_universe(conn) -> pd.DataFrame:
    """加载股票池（含市场/代码/名称，以及已补采的上市日期与交易所类型）。"""
    cols = _table_columns(conn, "stock_info")
    extra = [c for c in ("list_date", "exchange_type", "delisting") if c in cols]

    sql = f"""
        SELECT stock_code, stock_name, market, symbol {"," + ",".join(extra) if extra else ""}
        FROM stock_info
    """
    df = _read_sql(conn, sql)
    for c in ("list_date", "exchange_type", "delisting"):
        if c not in df.columns:
            df[c] = None
    df["symbol"] = df["symbol"].fillna("").astype(str)
    return df


# ═══════════════════════════════════════════════════════════════════════════
# 1-5：交易所规则派生的静态身份（内置枚举，code 写进 profile.enum_value）
#
# enum_values 格式：{code: (label, short_label, parent_code)}
#   code        规范代码，落库的就是它
#   label       中文正式名，同一 enum_type 内全局唯一（含 short_label 一起查重）
#   short_label 中文简称，仅展示用，不参与比较/关联
#   parent_code 层级父项（板块 → 交易所）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="idt_market", name="市场", domain=DOMAIN,
    value_type=ENUM,
    enum_type="market",
    enum_values={
        "CN": ("A股", "沪深北三市"),
        "HK": ("港股", "香港市场"),
    },
    source_type="rule", update_freq="static", data_sources=["stock_info"],
    compute_logic="stock_info.market：SH/SZ → CN（A股，含北交所）；HK → HK（港股）",
    pit_capable=True, owner="profiling",
)
def idt_market(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _load_universe(conn)
    return _frame(df["stock_code"], df["market"].map({"SH": "CN", "SZ": "CN", "HK": "HK"}))


@tag(
    code="idt_exchange", name="交易所", domain=DOMAIN,
    value_type=ENUM,
    enum_type="exchange",
    # ISO 10383 MIC 代码：国际通用、唯一、机器可读
    enum_values={
        "XSHG": ("上海证券交易所", "上交所"),
        "XSHE": ("深圳证券交易所", "深交所"),
        "XBEI": ("北京证券交易所", "北交所"),
        "XHKG": ("香港交易所", "港交所"),
    },
    source_type="rule", update_freq="static", data_sources=["stock_info"],
    compute_logic="股票代码前缀：HK→XHKG；SH 且 920 段→XBEI；SH→XSHG；SZ→XSHE（ISO 10383 MIC）",
    pit_capable=True, owner="profiling",
)
def idt_exchange(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _load_universe(conn)

    def _ex(row):
        mkt, sym = row["market"], row["symbol"]
        if mkt == "HK":
            return "XHKG"
        if mkt == "SH":
            return "XBEI" if sym.startswith(_BJ) else "XSHG"
        return "XSHE"

    return _frame(df["stock_code"], df.apply(_ex, axis=1))


@tag(
    code="idt_board", name="交易所与板块", domain=DOMAIN,
    value_type=ENUM,
    enum_type="board",
    enum_values={
        "SSE_MAIN":     ("沪市主板", None, "XSHG"),
        "SSE_STAR":     ("科创板", None, "XSHG"),
        "SSE_B":        ("沪市B股", None, "XSHG"),
        "SZSE_MAIN":    ("深市主板", None, "XSHE"),
        "SZSE_CHINEXT": ("创业板", None, "XSHE"),
        "SZSE_B":       ("深市B股", None, "XSHE"),
        "BSE_MAIN":     ("北交所", None, "XBEI"),
        "HKEX_MAIN":    ("港股主板", None, "XHKG"),
        "HKEX_GEM":     ("港股创业板", None, "XHKG"),
        "NON_STOCK":    ("非股票标的", None, None),   # ETF 等，不属任何股票板块
    },
    source_type="rule", update_freq="static", data_sources=["stock_info"],
    compute_logic="优先用富途 exchange_type（CN_STIB/CN_BJ/HK_MAINBOARD/HK_GEMBOARD）；"
                  "缺失时按代码段判定。板块与交易所是两级概念，不混在同一枚举里",
    pit_capable=True, owner="profiling",
)
def idt_board(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _load_universe(conn)
    return _frame(df["stock_code"], df.apply(_board_of, axis=1))


def _board_of(row) -> str:
    """板块判定：富途 exchange_type 优先，回退代码段规则。返回规范 code。"""
    sym = row["symbol"] or ""
    # exchange_type 允许为空（未补采到的标的），pandas 会把空值读成 float nan
    et = str(row.get("exchange_type") or "").upper()

    if row["market"] == "HK":
        if "GEM" in et:
            return "HKEX_GEM"
        if "MAIN" in et:
            return "HKEX_MAIN"
        return "HKEX_GEM" if sym.startswith("08") else "HKEX_MAIN"

    # A 股：交易所类型优先（CN_STIB=科创板，CN_BJ=北交所，由补采脚本写入）
    if "STIB" in et:
        return "SSE_STAR"
    if "BJ" in et:
        return "BSE_MAIN"

    # 回退：代码段规则
    if sym.startswith(_STAR):
        return "SSE_STAR"
    if sym.startswith(_BJ):
        return "BSE_MAIN"
    if row["market"] == "SH":
        if sym.startswith(_SH_B):
            return "SSE_B"
        if sym.startswith(_SH_MAIN):
            return "SSE_MAIN"
    else:
        if sym.startswith(_SZ_B):
            return "SZSE_B"
        if sym.startswith(_SZ_CHINEXT):
            return "SZSE_CHINEXT"
        if sym.startswith(_SZ_MAIN):
            return "SZSE_MAIN"
    return "NON_STOCK"


@tag(
    code="idt_security_type", name="证券类型", domain=DOMAIN,
    value_type=ENUM,
    enum_type="security_type",
    enum_values={
        "ORDINARY": ("普通股", None),
        "B_SHARE":  ("B股", None),        # 沪 900 / 深 200-201
        "CDR":      ("存托凭证", None),    # 科创板 CDR（689 段）
    },
    source_type="rule", update_freq="static", data_sources=["stock_info"],
    compute_logic="900/200/201 → B_SHARE；689 段 → CDR；其余 → ORDINARY",
    pit_capable=True, owner="profiling",
)
def idt_security_type(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _load_universe(conn)

    def _t(row):
        sym = row["symbol"]
        if row["market"] == "SH":
            if sym.startswith(_SH_B):
                return "B_SHARE"
            if sym.startswith("689"):
                return "CDR"
        elif sym.startswith(_SZ_B):
            return "B_SHARE"
        return "ORDINARY"

    return _frame(df["stock_code"], df.apply(_t, axis=1))


@tag(
    code="idt_listing_age_tier", name="上市年限档", domain=DOMAIN,
    num_unit="years",
    value_type=TIER,
    # tier 用有序数字 code，便于排序与区间筛选；中文说明放 value_range
    value_range={"1": "次新（上市<1年）", "2": "成熟（1-5年）", "3": "老股（>5年）"},
    source_type="rule", update_freq="static", data_sources=["stock_info"],
    compute_logic="(as_of - list_date)/365.25：<1→1，1-5→2，>5→3；list_date 缺失则不打标签。"
                  "num_value 存实际年限（年）",
    pit_capable=True, owner="profiling",
)
def idt_listing_age_tier(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _load_universe(conn)

    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
    sub = df[df["list_date"].notna()].copy()
    # 富途对老股票会给出 1970-01-01 占位值，视为缺失
    sub = sub[sub["list_date"] > pd.Timestamp("1971-01-01")]

    years = (pd.Timestamp(as_of) - sub["list_date"]).dt.days / 365.25
    tier = pd.Series("3", index=sub.index, dtype=object)
    tier[years < 5] = "2"
    tier[years < 1] = "1"

    return _frame(sub["stock_code"], tier, nums=years.round(3))


# ═══════════════════════════════════════════════════════════════════════════
# 6-7：行业归属（引用已有权威表，值域不复制到字典）
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="idt_gics_sector", name="GICS一级部门", domain=DOMAIN,
    value_type=ENUM,
    # 引用型：值域由 sector_hierarchy 承载，落库存官方英文标识（parent_code），
    # 中文名通过 enum_values 提供展示字典（label），不另立概念
    value_ref="table:sector_hierarchy.parent_code",
    enum_type="gics_sector",
    enum_values={
        "ENERGY":                 ("能源", None),
        "MATERIALS":              ("原材料", None),
        "INDUSTRIALS":            ("工业", None),
        "CONSUMER_DISCRETIONARY": ("可选消费", None),
        "CONSUMER_STAPLES":       ("必需消费", None),
        "HEALTH_CARE":            ("医疗保健", None),
        "FINANCIALS":             ("金融", None),
        "INFORMATION_TECHNOLOGY": ("信息技术", None),
        "COMMUNICATION":          ("通信服务", None),
        "UTILITIES":              ("公用事业", None),
        "REAL_ESTATE":            ("房地产", None),
        "CONGLOMERATES":          ("综合企业", None),
        "OTHER":                  ("其他", None),
    },
    source_type="external", update_freq="event",
    data_sources=["stock_sector", "sector_hierarchy"],
    compute_logic="stock_sector(sector_type=INDUSTRY) JOIN sector_hierarchy 取 parent_code（GICS 官方英文标识）。"
                  "落库存 code，中文显示名查 profile.enum_value",
    pit_capable=False, owner="profiling",
)
def idt_gics_sector(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _read_sql(
            conn,
            """
            SELECT ss.stock_code, sh.parent_code
            FROM stock_sector ss
            JOIN sector_hierarchy sh ON sh.sector_code = ss.sector_code
            WHERE ss.sector_type = 'INDUSTRY' AND sh.parent_code IS NOT NULL
            """,
        )
    return _frame(df["stock_code"], df["parent_code"])


@tag(
    code="idt_industry", name="行业（细粒度）", domain=DOMAIN,
    value_type=ENUM,
    # 引用型：值域完全由 stock_sector 承载（A股=证监会行业，港股=富途行业板块），
    # 两套体系并存是既有数据事实，由枚举类型自身区分，不在此处强行统一
    value_ref="table:stock_sector.sector_name[sector_type=INDUSTRY]",
    source_type="external", update_freq="event", data_sources=["stock_sector"],
    compute_logic="stock_sector 中 sector_type=INDUSTRY 的 sector_name。"
                  "注：A股为证监会行业（baostock，含行业码前缀如 C39），港股为富途行业板块名，"
                  "两者体系不同，跨市场比较请用 idt_gics_sector",
    pit_capable=False, owner="profiling",
)
def idt_industry(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _read_sql(
            conn,
            "SELECT stock_code, sector_name FROM stock_sector "
            "WHERE sector_type = 'INDUSTRY' AND sector_name IS NOT NULL AND sector_name <> ''",
        )
    return _frame(df["stock_code"], df["sector_name"])


# ═══════════════════════════════════════════════════════════════════════════
# 8：多地上市（A+H）
# ═══════════════════════════════════════════════════════════════════════════

_WS = re.compile(r"[\s\-－—Ａ-ＺA-Z]")


def _norm_name(s: str) -> str:
    """公司名归一：去空白与连接符，用于 A/H 同名匹配（如「工商银行」在两地同名）。"""
    return _WS.sub("", str(s or "")).replace("股份有限", "").replace("集团", "")


@tag(
    code="idt_is_ah", name="是否多地上市(A+H)", domain=DOMAIN,
    value_type=BOOL,
    value_range={"true": "同时在 A 股与港股上市", "false": "单一市场"},
    source_type="rule", update_freq="event", data_sources=["stock_info"],
    compute_logic="对 stock_name 做归一（去空白/连接符/「股份有限」「集团」后缀），"
                  "若同一归一名称同时出现在 A 股与港股，则双方均标记 true",
    pit_capable=False, owner="profiling",
)
def idt_is_ah(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _load_universe(conn)

    df["_n"] = df["stock_name"].map(_norm_name)
    df["_is_hk"] = df["market"] == "HK"
    ah_names = {
        n for n, g in df.groupby("_n")
        if g["_is_hk"].any() and (~g["_is_hk"]).any() and len(n) > 1
    }
    flags = df["_n"].isin(ah_names).map({True: "true", False: "false"})
    return _frame(df["stock_code"], flags, nums=df["_n"].isin(ah_names).astype(float))


# ═══════════════════════════════════════════════════════════════════════════
# 9-10：多重归属（指数成分 / 概念板块）—— 值域引用已有表
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="idt_index_member", name="指数成分", domain=DOMAIN,
    value_type=ENUM,
    value_ref="table:stock_sector.sector_name[sector_type=INDEX]",
    source_type="external", update_freq="event", data_sources=["stock_sector"],
    compute_logic="stock_sector 中 sector_type=INDEX 的 sector_name（沪深300/中证500/恒生指数…）。"
                  "多重归属：一只股票可同时属于多个指数",
    multi_value=True, pit_capable=False, owner="profiling",
)
def idt_index_member(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _read_sql(
            conn,
            "SELECT DISTINCT stock_code, sector_name FROM stock_sector "
            "WHERE sector_type = 'INDEX' AND sector_name IS NOT NULL AND sector_name <> ''",
        )
    return _frame(df["stock_code"], df["sector_name"])


@tag(
    code="idt_concept", name="概念板块", domain=DOMAIN,
    value_type=ENUM,
    value_ref="table:stock_sector.sector_name[sector_type=CONCEPT]",
    source_type="external", update_freq="event", data_sources=["stock_sector"],
    compute_logic="stock_sector 中 sector_type=CONCEPT 的 sector_name（富途概念板块）。"
                  "多重归属。注：概念的完整体系属「题材概念」域，此处仅为已有数据的弱版接入；"
                  "概念名存在语义重叠（如「人工智能」与「AI应用」），做严格概念去重需另建同义词表",
    multi_value=True, pit_capable=False, owner="profiling",
)
def idt_concept(as_of: date) -> pd.DataFrame:
    with _conn() as conn:
        df = _read_sql(
            conn,
            "SELECT DISTINCT stock_code, sector_name FROM stock_sector "
            "WHERE sector_type = 'CONCEPT' AND sector_name IS NOT NULL AND sector_name <> ''",
        )
    return _frame(df["stock_code"], df["sector_name"])


# ═══════════════════════════════════════════════════════════════════════════
# 11-12：口径已定但数据源缺失（登记在册，等数据补齐后改 status 即可）
#
# 未实现的 enum 标签可不声明 value_ref（校验只对 active 生效）——
# 值域取决于最终采用哪个数据源，提前写死反而会固化错误口径。
# ═══════════════════════════════════════════════════════════════════════════


@tag(
    code="idt_region", name="地域", domain=DOMAIN,
    value_type=ENUM,
    source_type="external", update_freq="static", data_sources=[],
    compute_logic="取公司注册地与办公地。若采用 AKShare，建议用国家统计局行政区划代码作 code",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：现有库无公司注册地/办公地字段，需外采（AKShare 股票基本信息或富途公司资料）",
    pit_capable=False, owner="profiling",
)
def idt_region(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")


@tag(
    code="idt_ownership", name="所有制", domain=DOMAIN,
    value_type=ENUM,
    source_type="external", update_freq="static", data_sources=[],
    compute_logic="按实际控制人性质划分国资/民营/外资",
    status=PLANNED_NO_DATA,
    blocked_reason="数据源缺失：现有库无股东性质/实际控制人字段，需外采（股权结构或公司资料接口）",
    pit_capable=False, owner="profiling",
)
def idt_ownership(as_of: date) -> pd.DataFrame:
    raise NotImplementedError("数据源缺失，见 blocked_reason")
