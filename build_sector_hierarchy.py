#!/usr/bin/env python3
"""
行业层级映射构建 — 把富途细粒度行业板块(INDUSTRY)聚合到 GICS 一级部门。

背景：
  富途 get_plate_list(Plate.INDUSTRY) 返回 ~111 个细粒度行业板块（粒度接近 GICS 三级/四级），
  直接平铺做资金流板块分析会非常凌乱。业界标准做法是聚合到 GICS 一级部门（11 类）或
  恒生行业分类（12 类）后再分析。本脚本建立「细粒度板块 → 一级部门」的静态映射。

流程：
  1) 建表 sector_hierarchy（缺失时 CREATE TABLE IF NOT EXISTS，幂等）；
  2) 从 stock_sector 读取全部 INDUSTRY 板块（DISTINCT sector_code, sector_name）；
  3) 按映射表归类到 GICS 一级部门；映射缺失的板块归 OTHER（并打 warning）；
  4) bulk upsert 写入 sector_hierarchy。

映射口径说明（GICS 一级部门，11 类 + 综合企业）：
  ENERGY 能源 / MATERIALS 原材料 / INDUSTRIALS 工业 / CONSUMER_DISCRETIONARY 非必需消费 /
  CONSUMER_STAPLES 必需消费 / HEALTH_CARE 医疗保健 / FINANCIALS 金融 /
  INFORMATION_TECHNOLOGY 信息技术 / COMMUNICATION 通信服务(含电信与传媒) /
  UTILITIES 公用事业 / REAL_ESTATE 房地产 / CONGLOMERATES 综合企业 / OTHER 其他(兜底)

用法：
    python3 build_sector_hierarchy.py            # 建表 + 采集 + 写入
    python3 build_sector_hierarchy.py --dry-run  # 只打印归类统计，不写库
    python3 build_sector_hierarchy.py --list     # 打印每个板块 → 一级部门 的完整映射

常驻调用：run() —— 由 market_scheduler 通过 collector_runtime 调用（低频，每周一次即可）。
"""
import sys
from datetime import datetime

from db import get_conn, bulk_upsert

log_name = "sector_hierarchy"
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger(log_name)

# ── 一级部门定义（GICS 11 部门 + 综合企业 + 其他兜底）───────────────────────
LEVEL1 = {
    "ENERGY": "能源",
    "MATERIALS": "原材料",
    "INDUSTRIALS": "工业",
    "CONSUMER_DISCRETIONARY": "非必需消费",
    "CONSUMER_STAPLES": "必需消费",
    "HEALTH_CARE": "医疗保健",
    "FINANCIALS": "金融",
    "INFORMATION_TECHNOLOGY": "信息技术",
    "COMMUNICATION": "通信服务",
    "UTILITIES": "公用事业",
    "REAL_ESTATE": "房地产",
    "CONGLOMERATES": "综合企业",
    "OTHER": "其他",
}

# ── 建表 ───────────────────────────────────────────────────────────────────
_SECTOR_HIERARCHY_SQL = """
CREATE TABLE IF NOT EXISTS sector_hierarchy (
    sector_code   VARCHAR(20)   NOT NULL,        -- 细粒度行业板块 code（= stock_sector.sector_code，如 HK.LIST1019）
    sector_name   VARCHAR(100),                   -- 细粒度行业板块英文名（冗余自 stock_sector，方便查询）
    parent_code   VARCHAR(30)   NOT NULL,        -- 一级部门 code（GICS：ENERGY/MATERIALS/...）
    parent_name   VARCHAR(50),                    -- 一级部门中文名（如 能源/金融）
    sector_type   VARCHAR(20)   DEFAULT 'INDUSTRY',
    updated_at    TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (sector_code)
)
"""
_COMMENTS = [
    ("TABLE", "sector_hierarchy",
     "行业层级映射：富途细粒度 INDUSTRY → GICS 一级部门（参考数据，低频刷新）"),
    ("COLUMN", "sector_hierarchy.parent_code",
     "一级部门 code：GICS 11 部门 + CONGLOMERATES(综合企业) + OTHER(兜底)"),
    ("COLUMN", "sector_hierarchy.parent_name",
     "一级部门中文名"),
]

# ── 映射表：富途板块 code → (parent_code, parent_name) ─────────────────────
#   111 个 INDUSTRY 板块全部显式映射；未覆盖的（未来新增板块）由代码兜底到 OTHER。
_SECTOR_MAP = {
    # ── 能源 ENERGY ──
    "HK.LIST1042": ("ENERGY", "能源"),            # Oil & Gas Producers
    "HK.LIST1043": ("ENERGY", "能源"),            # Oil & Gas Services
    "HK.LIST1044": ("ENERGY", "能源"),            # Coal
    "HK.LIST1016": ("ENERGY", "能源"),            # Alternative/Renewable Energy
    "HK.LIST1358": ("ENERGY", "能源"),            # nuclear
    "HK.LIST1354": ("ENERGY", "能源"),            # Energy Storage Systems
    "HK.LIST1033": ("ENERGY", "能源"),            # New Energy Materials
    # ── 原材料 MATERIALS ──
    "HK.LIST1078": ("MATERIALS", "原材料"),       # Aluminum
    "HK.LIST1077": ("MATERIALS", "原材料"),       # Copper
    "HK.LIST1084": ("MATERIALS", "原材料"),       # Gold & Precious Metals
    "HK.LIST1006": ("MATERIALS", "原材料"),       # Other Metals & Minerals
    "HK.LIST1075": ("MATERIALS", "原材料"),       # Steel
    "HK.LIST1046": ("MATERIALS", "原材料"),       # Speciality Chemicals
    "HK.LIST1028": ("MATERIALS", "原材料"),       # Construction Materials
    "HK.LIST1059": ("MATERIALS", "原材料"),       # Paper & Packaging
    "HK.LIST1015": ("MATERIALS", "原材料"),       # Printing & Packaging
    "HK.LIST1037": ("MATERIALS", "原材料"),       # Forestry & Timber
    # ── 工业 INDUSTRIALS ──
    "HK.LIST1063": ("INDUSTRIALS", "工业"),       # Aerospace & Defense
    "HK.LIST1065": ("INDUSTRIALS", "工业"),       # Air Cargo & Logistics
    "HK.LIST1064": ("INDUSTRIALS", "工业"),       # Airlines
    "HK.LIST1017": ("INDUSTRIALS", "工业"),       # Commercial Vehicles
    "HK.LIST1095": ("INDUSTRIALS", "工业"),       # Engineering & Construction
    "HK.LIST1073": ("INDUSTRIALS", "工业"),       # Heavy Infrastructure
    "HK.LIST1074": ("INDUSTRIALS", "工业"),       # Heavy Machinery
    "HK.LIST1025": ("INDUSTRIALS", "工业"),       # Industrial Parts & Equipment
    "HK.LIST1031": ("INDUSTRIALS", "工业"),       # Other Support Services
    "HK.LIST1002": ("INDUSTRIALS", "工业"),       # Procurement & Supply Chain Management
    "HK.LIST1005": ("INDUSTRIALS", "工业"),       # Public Transport
    "HK.LIST1076": ("INDUSTRIALS", "工业"),       # Railroads & Highways
    "HK.LIST1066": ("INDUSTRIALS", "工业"),       # Shipping & Ports
    "HK.LIST23846": ("INDUSTRIALS", "工业"),      # Track and train equipment
    "HK.LIST1355": ("INDUSTRIALS", "工业"),       # Transport & Logistics
    "HK.LIST1271": ("INDUSTRIALS", "工业"),       # Environmental Services
    # ── 非必需消费 CONSUMER_DISCRETIONARY ──
    "HK.LIST1040": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Automobiles
    "HK.LIST1041": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Auto Parts
    "HK.LIST1269": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Auto Retailers
    "HK.LIST23847": ("CONSUMER_DISCRETIONARY", "非必需消费"),  # Motorcycles and others
    "HK.LIST1277": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Apparel Manufacturing
    "HK.LIST1270": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Apparel Retailers
    "HK.LIST1268": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Other Clothing Accessories
    "HK.LIST1275": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Footwear
    "HK.LIST1035": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Textiles & Fabrics
    "HK.LIST1049": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Jewelry & Watches
    "HK.LIST1021": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # furniture
    "HK.LIST1022": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Home Appliances
    "HK.LIST1047": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Toys & Leisure
    "HK.LIST1278": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Home Improvement Retail
    "HK.LIST1071": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Hotels & Resorts
    "HK.LIST1069": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Resorts & Casinos
    "HK.LIST1032": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Sports & Recreation Facilities
    "HK.LIST1083": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Catering
    "HK.LIST1029": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Entertainment
    "HK.LIST1034": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Travel & Sightseeing
    "HK.LIST1091": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # education
    "HK.LIST23361": ("CONSUMER_DISCRETIONARY", "非必需消费"),  # Online Retailers
    "HK.LIST1056": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Diversified Retailers
    "HK.LIST1276": ("CONSUMER_DISCRETIONARY", "非必需消费"),   # Other Retailers
    # ── 必需消费 CONSUMER_STAPLES ──
    "HK.LIST1001": ("CONSUMER_STAPLES", "必需消费"),           # Dairy
    "HK.LIST1010": ("CONSUMER_STAPLES", "必需消费"),           # packaged food
    "HK.LIST1273": ("CONSUMER_STAPLES", "必需消费"),           # Meat & Poultry
    "HK.LIST1272": ("CONSUMER_STAPLES", "必需消费"),           # Livestock Feed
    "HK.LIST1082": ("CONSUMER_STAPLES", "必需消费"),           # Food Additives
    "HK.LIST1011": ("CONSUMER_STAPLES", "必需消费"),           # Agricultural Inputs
    "HK.LIST1008": ("CONSUMER_STAPLES", "必需消费"),           # Agriculture
    "HK.LIST1072": ("CONSUMER_STAPLES", "必需消费"),           # Alcoholic Beverages
    "HK.LIST1080": ("CONSUMER_STAPLES", "必需消费"),           # Non-Alcoholic Beverages
    "HK.LIST1356": ("CONSUMER_STAPLES", "必需消费"),           # Tobacco
    "HK.LIST1062": ("CONSUMER_STAPLES", "必需消费"),           # Personal Care
    "HK.LIST23850": ("CONSUMER_STAPLES", "必需消费"),          # Skincare and cosmetics
    "HK.LIST23848": ("CONSUMER_STAPLES", "必需消费"),          # household consumables
    "HK.LIST23849": ("CONSUMER_STAPLES", "必需消费"),          # dietary supplements
    "HK.LIST1070": ("CONSUMER_STAPLES", "必需消费"),           # Supermarkets & Convenience Stores
    # ── 医疗保健 HEALTH_CARE ──
    "HK.LIST1050": ("HEALTH_CARE", "医疗保健"),                # Biotechnology
    "HK.LIST1012": ("HEALTH_CARE", "医疗保健"),                # Medical Equipment & Supplies
    "HK.LIST1086": ("HEALTH_CARE", "医疗保健"),                # Medical Services
    "HK.LIST1067": ("HEALTH_CARE", "医疗保健"),                # Pharmaceuticals
    "HK.LIST1357": ("HEALTH_CARE", "医疗保健"),                # Pharmaceutical Distribution
    "HK.LIST1284": ("HEALTH_CARE", "医疗保健"),                # Traditional Chinese Medicine
    # ── 金融 FINANCIALS ──
    "HK.LIST1079": ("FINANCIALS", "金融"),                      # Banks
    "HK.LIST1003": ("FINANCIALS", "金融"),                      # Insurance
    "HK.LIST1004": ("FINANCIALS", "金融"),                      # Credit Services
    "HK.LIST1068": ("FINANCIALS", "金融"),                      # Securities & Brokerage
    "HK.LIST1030": ("FINANCIALS", "金融"),                      # Investment & Asset Management
    "HK.LIST1007": ("FINANCIALS", "金融"),                      # Other Financial Services
    "HK.LIST23362": ("FINANCIALS", "金融"),                     # Payment services
    # ── 信息技术 INFORMATION_TECHNOLOGY ──
    "HK.LIST1100": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Application Software
    "HK.LIST1053": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Computers & Equipment
    "HK.LIST1052": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Consumer Electronics
    "HK.LIST1274": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Electronic Components
    "HK.LIST1013": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Semiconductors
    "HK.LIST1360": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Semiconductor Equipment & Materials
    "HK.LIST23363": ("INFORMATION_TECHNOLOGY", "信息技术"),     # Digital Solution Services
    "HK.LIST23364": ("INFORMATION_TECHNOLOGY", "信息技术"),     # Internet services and infrastructure
    "HK.LIST1055": ("INFORMATION_TECHNOLOGY", "信息技术"),      # Consumer Telecommunication Equipment
    # ── 通信服务 COMMUNICATION（电信 + 传媒/游戏/广告/出版，GICS 口径）──
    "HK.LIST1054": ("COMMUNICATION", "通信服务"),               # Telecom Services
    "HK.LIST23851": ("COMMUNICATION", "通信服务"),              # Telecommunication network infrastructure
    "HK.LIST1014": ("COMMUNICATION", "通信服务"),               # Satellite & Wireless Services
    "HK.LIST1026": ("COMMUNICATION", "通信服务"),               # Advertising Agencies
    "HK.LIST1027": ("COMMUNICATION", "通信服务"),               # Broadcasting
    "HK.LIST23360": ("COMMUNICATION", "通信服务"),              # Interactive media and services
    "HK.LIST1359": ("COMMUNICATION", "通信服务"),               # Gaming
    "HK.LIST1009": ("COMMUNICATION", "通信服务"),               # publishing
    # ── 公用事业 UTILITIES ──
    "HK.LIST1051": ("UTILITIES", "公用事业"),                   # Electric Utilities
    "HK.LIST1045": ("UTILITIES", "公用事业"),                   # Gas Utilities
    "HK.LIST1039": ("UTILITIES", "公用事业"),                   # Water Utilities
    # ── 房地产 REAL_ESTATE ──
    "HK.LIST1019": ("REAL_ESTATE", "房地产"),                   # Real Estate Developers
    "HK.LIST1020": ("REAL_ESTATE", "房地产"),                   # Real Estate Investment
    "HK.LIST1089": ("REAL_ESTATE", "房地产"),                   # Real Estate Services
    "HK.LIST1090": ("REAL_ESTATE", "房地产"),                   # Property Services & Management
    "HK.LIST1311": ("REAL_ESTATE", "房地产"),                   # REITs
    # ── 综合企业 CONGLOMERATES ──
    "HK.LIST1061": ("CONGLOMERATES", "综合企业"),               # Conglomerates
}

_OTHER = ("OTHER", "其他")


def _ensure_table():
    """确保 sector_hierarchy 表存在（CREATE TABLE IF NOT EXISTS，幂等）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SECTOR_HIERARCHY_SQL)
            cur.execute("SELECT to_regclass('sector_hierarchy')")
            if cur.fetchone():
                for obj_type, obj_name, comment in _COMMENTS:
                    cur.execute(
                        f"COMMENT ON {obj_type} {obj_name} IS %s",
                        (comment,),
                    )
    log.info("sector_hierarchy 表已确认存在")


def _load_industry_sectors():
    """从 stock_sector 读取全部 INDUSTRY 板块，返回 [(sector_code, sector_name), ...]"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT sector_code, sector_name FROM stock_sector "
                "WHERE sector_type = 'INDUSTRY' AND sector_name IS NOT NULL "
                "ORDER BY sector_name"
            )
            return cur.fetchall()


def _classify(sector_code, sector_name):
    """返回 (parent_code, parent_name)；映射缺失时归 OTHER 并打 warning。"""
    mapped = _SECTOR_MAP.get(sector_code)
    if mapped is None:
        log.warning("板块 %s(%s) 无映射，归入 OTHER", sector_name, sector_code)
        return _OTHER
    return mapped


def _build_rows():
    """读 INDUSTRY 板块 → 归类 → 构造写库行。返回 (rows, stats)。"""
    sectors = _load_industry_sectors()
    rows = []
    stats = {}
    for code, name in sectors:
        parent_code, parent_name = _classify(code, name)
        rows.append({
            "sector_code": code,
            "sector_name": name,
            "parent_code": parent_code,
            "parent_name": parent_name,
            "sector_type": "INDUSTRY",
            "updated_at": datetime.now(),
        })
        stats[parent_code] = stats.get(parent_code, 0) + 1
    return rows, stats


def run():
    """构建入口：建表 + 采集 + 写入 sector_hierarchy。"""
    _ensure_table()
    rows, stats = _build_rows()
    if not rows:
        log.warning("stock_sector 无 INDUSTRY 板块，跳过写入")
        return 0
    with get_conn() as conn:
        bulk_upsert(conn, "sector_hierarchy", rows, conflict_cols=["sector_code"])
    log.info("完成：写入/更新 %d 个行业板块映射", len(rows))
    for pc in sorted(stats, key=lambda x: -stats[x]):
        log.info("  %-24s %s：%d 个板块", pc, LEVEL1.get(pc, pc), stats[pc])
    return len(rows)


def _print_mapping():
    """--list：打印每个板块 → 一级部门 的完整映射（不写库）。"""
    sectors = _load_industry_sectors()
    for code, name in sectors:
        pc, pn = _classify(code, name)
        print(f"{pc}\t{pn}\t{name}\t{code}")
    print(f"\n共 {len(sectors)} 个 INDUSTRY 板块")


if __name__ == "__main__":
    if "--list" in sys.argv:
        _print_mapping()
    elif "--dry-run" in sys.argv:
        rows, stats = _build_rows()
        print(f"dry-run：共 {len(rows)} 个板块，按一级部门分布：")
        for pc in sorted(stats, key=lambda x: -stats[x]):
            print(f"  {pc:<26} {LEVEL1.get(pc, pc)}：{stats[pc]} 个板块")
    else:
        run()
