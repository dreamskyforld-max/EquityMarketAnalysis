#!/usr/bin/env python3
"""
行业层级映射构建 — 把细粒度行业板块(INDUSTRY)聚合到 GICS 一级部门。

背景：
  港股：富途 get_plate_list(Plate.INDUSTRY) 返回 ~111 个细粒度行业板块（粒度接近 GICS 三级/四级），
       stock_sector.sector_code 形如 HK.xxxx，按 _SECTOR_MAP 静态映射。
  A股：东方财富(em) 行业板块（代码形如 BKxxxx，名称如「半导体」「白酒」），
       sector_code 非 HK.* 前缀，按板块【名称】经 _A_INDUSTRY_MAP / _A_INDUSTRY_PREFIX 映射。
  直接平铺做资金流板块分析会非常凌乱。业界标准做法是聚合到 GICS 一级部门（11 类）后再分析。
   本脚本对两类板块统一聚合到 GICS 一级部门。

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

常驻调用：run(codes=None, ctx=None) —— 阶段 3 起由 market_scheduler 在每周二 18:30
    （get_stock_sector.py 18:00 之后）通过 collector_runtime 调用。
    ⚠ 本表只作**展示层 GICS 标签**，不是第④层行业配置的计算口径
    （阶段 3 用 HS_L3 的 113 个恒生三级行业，见 hk_sector_index.py）。

【GICS 二级映射（2026-11-19 新增）】
  另建 sector_hierarchy_l2 表，专门存【板块 → GICS 二级行业组】映射，供画像标签
  val_pe_ind2_pct（【市场+二级组】内 PE 横截面分位）使用。一级表 sector_hierarchy
  保持原样不动。要点：
    · 码表 LEVEL2 固化 25 个二级组（GICS 2023 口径，四位数字码 + 标准简体中文名）；
    · 二级归属由 _SECTOR_GROUP_MAP 人工逐条判定（富途板块无官方 GICS 字段），属语义近似；
    · 首期只做港股 INDUSTRY 板块（原 111 个 + 2026-11 新增 2 个 = 当前 113 个）；
      A 股无二级映射 → 不打 val_pe_ind2_pct；
    · 综合企业 HK.LIST1061 在 GICS 2023 无对应二级组 → group_code='NONE'（未映射）。
  ⚠ 二级组之间粒度不均（能源/材料/公用事业仅 1 组 = 与一级等价），分位可比性有限。
"""
import sys
from datetime import date, datetime

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
    "CONSUMER_DISCRETIONARY": "可选消费",
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

# ── GICS 二级行业组码表（2023 口径：25 组，四位数字码 + 标准简体中文名）──────
#   中文名取自对齐 GICS 2023 的开源对照（skysaint/gics-data，四级齐全），
#   与 S&P Global 中文站口径一致；⚠ GICS 官方仅发布英文结构表，无官方简体版。
#   NONE = 综合企业等 GICS 无对应二级组的兜底码（非官方 GICS code）。
LEVEL2 = {
    "1010": "能源",
    "1510": "材料",
    "2010": "资本品",
    "2020": "商业和专业服务",
    "2030": "运输",
    "2510": "汽车及零部件",
    "2520": "耐用消费品和服装",
    "2530": "消费者服务",
    "2550": "非必需消费品分销与零售",
    "3010": "必需消费品分销与零售",
    "3020": "食品、饮料和烟草",
    "3030": "家庭和个人用品",
    "3510": "医疗保健设备与服务",
    "3520": "制药、生物技术和生命科学",
    "4010": "银行",
    "4020": "金融服务",
    "4030": "保险",
    "4510": "软件与服务",
    "4520": "技术性硬件和设备",
    "4530": "半导体与半导体设备",
    "5010": "通讯服务",
    "5020": "媒体与娱乐",
    "5510": "公共事业",
    "6010": "权益房地产投资信托（REITs）",
    "6020": "房地产管理与开发",
    "NONE": "未映射",
}

# ── 建表 ───────────────────────────────────────────────────────────────────
_SECTOR_HIERARCHY_SQL = """
CREATE TABLE IF NOT EXISTS sector_hierarchy (
    sector_code   VARCHAR(20)   NOT NULL,        -- 细粒度行业板块 code（= stock_sector.sector_code，如 HK.LIST1019）
    sector_name   VARCHAR(100),                   -- 细粒度行业板块英文名（冗余自 stock_sector，方便查询）
    parent_code   VARCHAR(30)   NOT NULL,        -- 一级部门 code（GICS：ENERGY/MATERIALS/...）
    parent_name   VARCHAR(50),                    -- 一级部门中文名（如 能源/金融）
    sector_type   VARCHAR(20)   DEFAULT 'INDUSTRY',
    as_of         DATE,                           -- 本次映射快照归属日（阶段3 D4；不是 PIT 生效日）
    updated_at    TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (sector_code)
)
"""
_COMMENTS = [
    ("TABLE", "sector_hierarchy",
     "行业层级映射：富途细粒度 INDUSTRY → GICS 一级部门（参考数据，低频刷新）；阶段3 起由 market_scheduler 每周二 18:30 刷新并写 as_of"),
    ("COLUMN", "sector_hierarchy.parent_code",
     "一级部门 code：GICS 11 部门 + CONGLOMERATES(综合企业) + OTHER(兜底)"),
    ("COLUMN", "sector_hierarchy.parent_name",
     "一级部门中文名"),
    ("COLUMN", "sector_hierarchy.as_of",
     "映射快照归属日（build_sector_hierarchy 每次运行写当天）。⚠ 只标快照日，不是 PIT 版本链；GICS 仅作展示层标签，不作行业配置计算口径（阶段3 用 HS_L3 113 行业）"),
]

# 二级映射独立表：一级表命名已固定为「行业层级映射」，二级关系另表存放，避免混义。
_SECTOR_HIERARCHY_L2_SQL = """
CREATE TABLE IF NOT EXISTS sector_hierarchy_l2 (
    sector_code   VARCHAR(20)   NOT NULL,        -- 细粒度行业板块 code（= stock_sector.sector_code）
    sector_name   VARCHAR(100),                   -- 板块名（冗余自 stock_sector）
    group_code    VARCHAR(30)   NOT NULL,        -- GICS 二级行业组四位码；NONE=无对应
    group_name    VARCHAR(50),                    -- 二级组标准简体中文名
    sector_type   VARCHAR(20)   DEFAULT 'INDUSTRY',
    as_of         DATE,                           -- 映射快照归属日（不是 PIT 生效日）
    updated_at    TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (sector_code)
)
"""
_L2_COMMENTS = [
    ("TABLE", "sector_hierarchy_l2",
     "行业二级映射：富途细粒度 INDUSTRY 板块 → GICS 二级行业组（2023 口径 25 组）。⚠ 人工语义近似映射，非官方 GICS 个股分类；供画像 val_pe_ind2_pct 分位使用"),
    ("COLUMN", "sector_hierarchy_l2.group_code",
     "GICS 2023 二级行业组四位码（1010~6020）；NONE=综合企业等无对应二级组的兜底码（非官方 GICS code）"),
    ("COLUMN", "sector_hierarchy_l2.group_name",
     "二级行业组标准简体中文名（对齐 GICS 2023 开源中文对照，非 MSCI 官方发布）"),
    ("COLUMN", "sector_hierarchy_l2.as_of",
     "映射快照归属日。⚠ 只标快照日，不是 PIT 版本链；stock_sector 当前成分含幸存者偏差"),
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
    "HK.LIST1040": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Automobiles
    "HK.LIST1041": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Auto Parts
    "HK.LIST1269": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Auto Retailers
    "HK.LIST23847": ("CONSUMER_DISCRETIONARY", "可选消费"),  # Motorcycles and others
    "HK.LIST1277": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Apparel Manufacturing
    "HK.LIST1270": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Apparel Retailers
    "HK.LIST1268": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Other Clothing Accessories
    "HK.LIST1275": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Footwear
    "HK.LIST1035": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Textiles & Fabrics
    "HK.LIST1049": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Jewelry & Watches
    "HK.LIST1021": ("CONSUMER_DISCRETIONARY", "可选消费"),   # furniture
    "HK.LIST1022": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Home Appliances
    "HK.LIST1047": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Toys & Leisure
    "HK.LIST1278": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Home Improvement Retail
    "HK.LIST1071": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Hotels & Resorts
    "HK.LIST1069": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Resorts & Casinos
    "HK.LIST1032": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Sports & Recreation Facilities
    "HK.LIST1083": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Catering
    "HK.LIST1029": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Entertainment
    "HK.LIST1034": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Travel & Sightseeing
    "HK.LIST1091": ("CONSUMER_DISCRETIONARY", "可选消费"),   # education
    "HK.LIST1056": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Diversified Retailers
    "HK.LIST1276": ("CONSUMER_DISCRETIONARY", "可选消费"),   # Other Retailers
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
    "HK.LIST23361": ("INFORMATION_TECHNOLOGY", "信息技术"),     # Online Retailers
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
    # ── 2026-11 新增板块（数据源扩容；用户 2026-11-19 确认归属）──
    "HK.LIST24340": ("HEALTH_CARE", "医疗保健"),               # 医药及生物科技合约服务 CRO/CDMO
    "HK.LIST24341": ("INDUSTRIALS", "工业"),                   # 机器人系统及解决方案
}

# ── GICS 二级映射：港股板块 code → 二级组 code（113 个，人工语义判定）────────
#   ⚠ 富途板块无官方 GICS 字段；下列归属为语义近似，不是官方 GICS 个股分类。
#   已验证：覆盖 _SECTOR_MAP 全部港股板块（2026-11 起 113 个），
#   层级一致性 112/113（唯一例外 HK.LIST1061 综合企业：GICS 无对应二级组 → NONE）。
#   5 个低置信项已由用户 2026-11-19 确认按此落：
#     HK.LIST23361 网上零售→4510（一级 IT 与 GICS 2550 冲突，一级错传导）
#     HK.LIST23849 膳食补充剂→3020  HK.LIST1357 药品分销→3510
#     HK.LIST1020 地产投资→6020     HK.LIST1061 综合企业→NONE
_SECTOR_GROUP_MAP = {
    # ── 能源 → 1010 ──
    "HK.LIST1042": "1010", "HK.LIST1043": "1010", "HK.LIST1044": "1010",
    "HK.LIST1016": "1010", "HK.LIST1358": "1010", "HK.LIST1354": "1010",
    "HK.LIST1033": "1010",
    # ── 原材料 → 1510 ──
    "HK.LIST1078": "1510", "HK.LIST1077": "1510", "HK.LIST1084": "1510",
    "HK.LIST1006": "1510", "HK.LIST1075": "1510", "HK.LIST1046": "1510",
    "HK.LIST1028": "1510", "HK.LIST1059": "1510", "HK.LIST1015": "1510",
    "HK.LIST1037": "1510",
    # ── 工业 → 2010 资本品 / 2020 商业和专业服务 / 2030 运输 ──
    "HK.LIST1063": "2010", "HK.LIST1017": "2010", "HK.LIST1095": "2010",
    "HK.LIST1073": "2010", "HK.LIST1074": "2010", "HK.LIST1025": "2010",
    "HK.LIST23846": "2010",
    "HK.LIST1031": "2020", "HK.LIST1002": "2020", "HK.LIST1271": "2020",
    "HK.LIST1065": "2030", "HK.LIST1064": "2030", "HK.LIST1005": "2030",
    "HK.LIST1076": "2030", "HK.LIST1066": "2030", "HK.LIST1355": "2030",
    # ── 可选消费 → 2510 汽车 / 2520 耐用品服装 / 2530 消费者服务 / 2550 分销零售 ──
    "HK.LIST1040": "2510", "HK.LIST1041": "2510", "HK.LIST23847": "2510",
    "HK.LIST1277": "2520", "HK.LIST1268": "2520", "HK.LIST1275": "2520",
    "HK.LIST1035": "2520", "HK.LIST1049": "2520", "HK.LIST1021": "2520",
    "HK.LIST1022": "2520", "HK.LIST1047": "2520",
    "HK.LIST1071": "2530", "HK.LIST1069": "2530", "HK.LIST1032": "2530",
    "HK.LIST1083": "2530", "HK.LIST1029": "2530", "HK.LIST1034": "2530",
    "HK.LIST1091": "2530",
    "HK.LIST1269": "2550", "HK.LIST1270": "2550", "HK.LIST1278": "2550",
    "HK.LIST1056": "2550", "HK.LIST1276": "2550",
    # ── 必需消费 → 3010 分销零售 / 3020 食品饮料烟草 / 3030 家庭个人用品 ──
    "HK.LIST1070": "3010",
    "HK.LIST1001": "3020", "HK.LIST1010": "3020", "HK.LIST1273": "3020",
    "HK.LIST1272": "3020", "HK.LIST1082": "3020", "HK.LIST1011": "3020",
    "HK.LIST1008": "3020", "HK.LIST1072": "3020", "HK.LIST1080": "3020",
    "HK.LIST1356": "3020", "HK.LIST23849": "3020",
    "HK.LIST1062": "3030", "HK.LIST23850": "3030", "HK.LIST23848": "3030",
    # ── 医疗保健 → 3510 设备与服务 / 3520 制药生技 ──
    "HK.LIST1012": "3510", "HK.LIST1086": "3510", "HK.LIST1357": "3510",
    "HK.LIST1050": "3520", "HK.LIST1067": "3520", "HK.LIST1284": "3520",
    # ── 金融 → 4010 银行 / 4020 金融服务 / 4030 保险 ──
    "HK.LIST1079": "4010", "HK.LIST1003": "4030",
    "HK.LIST1004": "4020", "HK.LIST1068": "4020", "HK.LIST1030": "4020",
    "HK.LIST1007": "4020", "HK.LIST23362": "4020",
    # ── 信息技术 → 4510 软件服务 / 4520 硬件设备 / 4530 半导体 ──
    "HK.LIST1100": "4510", "HK.LIST23363": "4510", "HK.LIST23364": "4510",
    "HK.LIST23361": "4510",
    "HK.LIST1053": "4520", "HK.LIST1052": "4520", "HK.LIST1274": "4520",
    "HK.LIST1055": "4520",
    "HK.LIST1013": "4530", "HK.LIST1360": "4530",
    # ── 通信服务 → 5010 通讯服务 / 5020 媒体娱乐 ──
    "HK.LIST1054": "5010", "HK.LIST23851": "5010", "HK.LIST1014": "5010",
    "HK.LIST1026": "5020", "HK.LIST1027": "5020", "HK.LIST23360": "5020",
    "HK.LIST1359": "5020", "HK.LIST1009": "5020",
    # ── 公用事业 → 5510 ──
    "HK.LIST1051": "5510", "HK.LIST1045": "5510", "HK.LIST1039": "5510",
    # ── 房地产 → 6010 REITs / 6020 管理开发 ──
    "HK.LIST1311": "6010",
    "HK.LIST1019": "6020", "HK.LIST1089": "6020", "HK.LIST1090": "6020",
    "HK.LIST1020": "6020",
    # ── 综合企业 → NONE（GICS 无对应二级组）──
    "HK.LIST1061": "NONE",
    # ── 2026-11 新增板块（用户 2026-11-19 确认）──
    "HK.LIST24340": "3520",   # 医药及生物科技合约服务 CRO/CDMO → 制药生技
    "HK.LIST24341": "2010",   # 机器人系统及解决方案 → 资本品
}

_OTHER = ("OTHER", "其他")

# ── A股东财行业名 → GICS 一级部门（名称映射，因东财板块 code 无稳定含义）────
#   覆盖东财一级行业主类；未列出的细分行业按名称模糊命中，仍缺失归 OTHER。
#   仅对 sector_type='INDUSTRY' 且 sector_code 非 HK.* 前缀的板块生效。
_A_INDUSTRY_MAP = {
    "煤炭": "MATERIALS", "石油": "ENERGY", "石油石化": "ENERGY", "石化": "ENERGY",
    "有色金属": "MATERIALS", "钢铁": "MATERIALS", "化工": "MATERIALS", "化学制品": "MATERIALS",
    "建筑材料": "MATERIALS", "建筑装饰": "INDUSTRIALS", "建筑": "INDUSTRIALS",
    "机械设备": "INDUSTRIALS", "通用设备": "INDUSTRIALS", "专用设备": "INDUSTRIALS",
    "电力设备": "INDUSTRIALS", "新能源": "ENERGY", "电气设备": "INDUSTRIALS",
    "国防军工": "INDUSTRIALS", "航空航天": "INDUSTRIALS", "汽车": "CONSUMER_DISCRETIONARY",
    "汽车零部件": "CONSUMER_DISCRETIONARY", "商贸零售": "CONSUMER_DISCRETIONARY",
    "零售": "CONSUMER_DISCRETIONARY", "社会服务": "CONSUMER_DISCRETIONARY", "旅游": "CONSUMER_DISCRETIONARY",
    "酒店": "CONSUMER_DISCRETIONARY", "餐饮": "CONSUMER_DISCRETIONARY",
    "纺织服饰": "CONSUMER_DISCRETIONARY", "服装": "CONSUMER_DISCRETIONARY",
    "轻工制造": "CONSUMER_DISCRETIONARY", "家居": "CONSUMER_DISCRETIONARY",
    "家电": "CONSUMER_DISCRETIONARY", "家用电器": "CONSUMER_DISCRETIONARY",
    "农林牧渔": "CONSUMER_STAPLES", "食品饮料": "CONSUMER_STAPLES", "食品": "CONSUMER_STAPLES",
    "酿酒": "CONSUMER_STAPLES", "白酒": "CONSUMER_STAPLES", "医药生物": "HEALTH_CARE",
    "医药": "HEALTH_CARE", "医疗器械": "HEALTH_CARE", "生物制品": "HEALTH_CARE",
    "中药": "HEALTH_CARE", "化学制药": "HEALTH_CARE", "银行": "FINANCIALS",
    "非银金融": "FINANCIALS", "证券": "FINANCIALS", "保险": "FINANCIALS",
    "多元金融": "FINANCIALS", "房地产": "REAL_ESTATE", "电子": "INFORMATION_TECHNOLOGY",
    "半导体": "INFORMATION_TECHNOLOGY", "计算机": "INFORMATION_TECHNOLOGY",
    "通信设备": "INFORMATION_TECHNOLOGY", "通信": "COMMUNICATION", "传媒": "COMMUNICATION",
    "互联网": "COMMUNICATION", "公用事业": "UTILITIES", "电力": "UTILITIES",
    "燃气": "UTILITIES", "环保": "INDUSTRIALS", "综合": "CONGLOMERATES",
    "交通运输": "INDUSTRIALS", "物流": "INDUSTRIALS", "港口": "INDUSTRIALS",
}

# A股行业名 → GICS 前缀模糊匹配（处理带修饰的行业名，如「半导体及元件」）
_A_INDUSTRY_PREFIX = [
    ("煤炭", "MATERIALS"), ("石油", "ENERGY"), ("有色", "MATERIALS"), ("钢铁", "MATERIALS"),
    ("化工", "MATERIALS"), ("建材", "MATERIALS"), ("建筑", "INDUSTRIALS"), ("机械", "INDUSTRIALS"),
    ("电力设备", "INDUSTRIALS"), ("军工", "INDUSTRIALS"), ("汽车", "CONSUMER_DISCRETIONARY"),
    ("零售", "CONSUMER_DISCRETIONARY"), ("社服", "CONSUMER_DISCRETIONARY"), ("纺织", "CONSUMER_DISCRETIONARY"),
    ("轻工", "CONSUMER_DISCRETIONARY"), ("家电", "CONSUMER_DISCRETIONARY"), ("农林", "CONSUMER_STAPLES"),
    ("食品", "CONSUMER_STAPLES"), ("酿酒", "CONSUMER_STAPLES"), ("医药", "HEALTH_CARE"),
    ("银行", "FINANCIALS"), ("证券", "FINANCIALS"), ("保险", "FINANCIALS"), ("金融", "FINANCIALS"),
    ("房地产", "REAL_ESTATE"), ("电子", "INFORMATION_TECHNOLOGY"), ("半导体", "INFORMATION_TECHNOLOGY"),
    ("计算机", "INFORMATION_TECHNOLOGY"), ("通信", "COMMUNICATION"), ("传媒", "COMMUNICATION"),
    ("公用事业", "UTILITIES"), ("环保", "INDUSTRIALS"), ("综合", "CONGLOMERATES"),
    ("交通", "INDUSTRIALS"), ("物流", "INDUSTRIALS"),
]

# A股行业名 → GICS 子串包含匹配（兼容证监会全称，如「酒、饮料和精制茶制造业」含「饮料/酒」）
#   顺序敏感：更具体的子串放前面（如「饮料」「白酒」优先于泛「酒」）。
_A_INDUSTRY_SUBSTR = [
    ("饮料", "CONSUMER_STAPLES"), ("白酒", "CONSUMER_STAPLES"), ("酒", "CONSUMER_STAPLES"),
    ("食品", "CONSUMER_STAPLES"), ("农", "CONSUMER_STAPLES"), ("牧", "CONSUMER_STAPLES"),
    ("汽车", "CONSUMER_DISCRETIONARY"), ("家电", "CONSUMER_DISCRETIONARY"),
    ("服装", "CONSUMER_DISCRETIONARY"), ("纺织", "CONSUMER_DISCRETIONARY"),
    ("零售", "CONSUMER_DISCRETIONARY"), ("商贸", "CONSUMER_DISCRETIONARY"),
    ("家居", "CONSUMER_DISCRETIONARY"), ("家具", "CONSUMER_DISCRETIONARY"),
    ("化学制药", "HEALTH_CARE"), ("生物制品", "HEALTH_CARE"), ("医疗器械", "HEALTH_CARE"),
    ("医药", "HEALTH_CARE"), ("中药", "HEALTH_CARE"), ("医疗", "HEALTH_CARE"),
    ("半导体", "INFORMATION_TECHNOLOGY"), ("电子", "INFORMATION_TECHNOLOGY"),
    ("计算机", "INFORMATION_TECHNOLOGY"), ("软件", "INFORMATION_TECHNOLOGY"),
    ("通信", "COMMUNICATION"), ("传媒", "COMMUNICATION"), ("互联网", "COMMUNICATION"),
    ("银行", "FINANCIALS"), ("证券", "FINANCIALS"), ("保险", "FINANCIALS"),
    ("房地产", "REAL_ESTATE"), ("电力", "UTILITIES"), ("燃气", "UTILITIES"),
    ("煤炭", "MATERIALS"), ("钢铁", "MATERIALS"), ("有色", "MATERIALS"), ("化工", "MATERIALS"),
    ("建筑", "INDUSTRIALS"), ("机械", "INDUSTRIALS"), ("设备", "INDUSTRIALS"),
    ("军工", "INDUSTRIALS"), ("交通", "INDUSTRIALS"), ("运输", "INDUSTRIALS"),
    ("物流", "INDUSTRIALS"), ("金属", "MATERIALS"), ("石油", "ENERGY"),
]


def _classify_a_industry(sector_name):
    """A股行业板块名 → (parent_code, parent_name)。

    匹配顺序：精确 → 前缀 → 子串包含（兼容证监会全称如「酒、饮料和精制茶制造业」含「饮料」）。
    仍缺失归 OTHER。
    """
    name = (sector_name or "").strip()
    if name in _A_INDUSTRY_MAP:
        pc = _A_INDUSTRY_MAP[name]
        return pc, LEVEL1[pc]
    for pref, pc in _A_INDUSTRY_PREFIX:
        if name.startswith(pref):
            return pc, LEVEL1[pc]
    for key, pc in _A_INDUSTRY_SUBSTR:
        if key in name:
            return pc, LEVEL1[pc]
    log.warning("A股行业板块 %s 无 GICS 映射，归入 OTHER", name)
    return _OTHER


def _ensure_table():
    """确保 sector_hierarchy / sector_hierarchy_l2 两表存在（幂等）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SECTOR_HIERARCHY_SQL)
            # 阶段 3：存量库没有 as_of 列，CREATE TABLE IF NOT EXISTS 不会补 → 显式幂等 ALTER
            cur.execute("ALTER TABLE sector_hierarchy ADD COLUMN IF NOT EXISTS as_of DATE")
            cur.execute("SELECT to_regclass('sector_hierarchy')")
            if cur.fetchone():
                for obj_type, obj_name, comment in _COMMENTS:
                    cur.execute(
                        f"COMMENT ON {obj_type} {obj_name} IS %s",
                        (comment,),
                    )
            # 二级映射独立表（2026-11-19 新增）
            cur.execute(_SECTOR_HIERARCHY_L2_SQL)
            cur.execute("SELECT to_regclass('sector_hierarchy_l2')")
            if cur.fetchone():
                for obj_type, obj_name, comment in _L2_COMMENTS:
                    cur.execute(
                        f"COMMENT ON {obj_type} {obj_name} IS %s",
                        (comment,),
                    )
    log.info("sector_hierarchy / sector_hierarchy_l2 两表已确认存在")


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


# ── 证监会行业代码首字母 → GICS 一级部门（BaoStock 行业分类用）─────────────
#   证监会一级大类（A-S）与 GICS 一级部门对应；制造业 C 整体偏 INDUSTRIALS，
#   其下的汽车/食品等 CONSUMER 子类由 _classify_a_industry(名称) 二次修正。
_CSRC_LETTER_MAP = {
    "A": "CONSUMER_STAPLES",    # 农林牧渔
    "B": "MATERIALS",           # 采矿业
    "C": "INDUSTRIALS",         # 制造业（整体偏工业，子类见名称修正）
    "D": "UTILITIES",           # 电力、热力、燃气及水生产供应
    "E": "INDUSTRIALS",         # 建筑业
    "F": "CONSUMER_DISCRETIONARY",  # 批发和零售业
    "G": "INDUSTRIALS",         # 交通运输、仓储和邮政
    "H": "CONSUMER_DISCRETIONARY",  # 住宿和餐饮业
    "I": "INFORMATION_TECHNOLOGY",  # 信息传输、软件和信息技术服务
    "J": "FINANCIALS",          # 金融业
    "K": "REAL_ESTATE",         # 房地产业
    "L": "INDUSTRIALS",         # 租赁和商务服务业
    "M": "INDUSTRIALS",         # 科学研究和技术服务业
    "N": "UTILITIES",           # 水利、环境和公共设施管理
    "O": "CONGLOMERATES",       # 居民服务、修理和其他服务
    "P": "CONSUMER_DISCRETIONARY",  # 教育
    "Q": "HEALTH_CARE",         # 卫生和社会工作
    "R": "COMMUNICATION",       # 文化、体育和娱乐
    "S": "OTHER",               # 公共管理、社会保障和社会组织
}


def _classify_csrc(sector_code, sector_name):
    """证监会行业代码（如 C36）→ GICS 一级部门。

    优先按代码首字母映射；对制造业 C 下偏消费/材料的子类（汽车/食品/酒等）
    再用名称经 _A_INDUSTRY_MAP 二次修正，提升 GICS 聚合精度。
    """
    code = str(sector_code).strip()
    letter = code[0].upper() if code else ""
    base = _CSRC_LETTER_MAP.get(letter)
    if base is None:
        log.warning("证监会行业代码 %s 首字母 %s 无映射，归入 OTHER", sector_code, letter)
        return _OTHER
    # 制造业子类名称修正（汽车/食品饮料/酒等归消费；化工/金属等归材料）
    if letter == "C" and sector_name:
        refined = _classify_a_industry(sector_name)
        if refined != _OTHER:
            return refined
    return base, LEVEL1[base]


def _classify(sector_code, sector_name):
    """返回 (parent_code, parent_name)。

    港股富途板块：sector_code 形如 HK.xxxx，按 _SECTOR_MAP 查。
    A股 BaoStock 证监会行业：sector_code 为纯字母数字（如 C36），按首字母经 _CSRC_LETTER_MAP。
    A股东财行业板块：sector_code 形如 BKxxxx，按名称经 _A_INDUSTRY_MAP。
    均缺失时归 OTHER 并打 warning。
    """
    code = str(sector_code)
    if code.startswith("HK."):
        mapped = _SECTOR_MAP.get(code)
        if mapped is None:
            log.warning("港股板块 %s(%s) 无映射，归入 OTHER", sector_name, sector_code)
            return _OTHER
        return mapped
    if code[:1].isalpha() and not code.startswith("BK"):
        # 证监会行业代码（字母开头，非东财 BK）：BaoStock 来源
        return _classify_csrc(code, sector_name)
    # A股东财行业板块（code 形如 BKxxxx）
    return _classify_a_industry(sector_name)


def _build_rows():
    """读 INDUSTRY 板块 → 归类 → 构造一级写库行。返回 (rows, stats)。"""
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
            "as_of": date.today(),
            "updated_at": datetime.now(),
        })
        stats[parent_code] = stats.get(parent_code, 0) + 1
    return rows, stats


def _build_l2_rows():
    """构造二级写库行：仅港股 INDUSTRY 板块（首期范围），返回 (rows, stats, unmapped)。

    ⚠ A 股板块无二级映射（方案确认首期只做港股），不写入本表 →
    画像 val_pe_ind2_pct 对 A 股不打标（inner join 落到港股板块才返回）。
    """
    sectors = _load_industry_sectors()
    rows, stats, unmapped = [], {}, []
    for code, name in sectors:
        if not code.startswith("HK."):
            continue
        group_code = _SECTOR_GROUP_MAP.get(code)
        if group_code is None:
            unmapped.append(code)
            continue
        rows.append({
            "sector_code": code,
            "sector_name": name,
            "group_code": group_code,
            "group_name": LEVEL2[group_code],
            "sector_type": "INDUSTRY",
            "as_of": date.today(),
            "updated_at": datetime.now(),
        })
        stats[group_code] = stats.get(group_code, 0) + 1
    if unmapped:
        log.warning("港股板块无二级映射（不打 val_pe_ind2_pct）：%s", unmapped)
    return rows, stats, unmapped


def run(codes=None, ctx=None):
    """构建入口：建表 + 采集 + 写入 sector_hierarchy / sector_hierarchy_l2。

    参数签名兼容 collector_runtime.run_module 的 run(codes, ctx=...) 调用；
    本任务只处理板块级映射（~194 行 + 111 二级行），忽略 codes。
    """
    _ensure_table()
    rows, stats = _build_rows()
    l2_rows, l2_stats, _ = _build_l2_rows()
    if not rows:
        log.warning("stock_sector 无 INDUSTRY 板块，跳过写入")
        return 0
    with get_conn() as conn:
        bulk_upsert(conn, "sector_hierarchy", rows, conflict_cols=["sector_code"])
        bulk_upsert(conn, "sector_hierarchy_l2", l2_rows, conflict_cols=["sector_code"])
    log.info("完成：写入/更新 %d 个一级板块映射，%d 个二级板块映射", len(rows), len(l2_rows))
    for pc in sorted(stats, key=lambda x: -stats[x]):
        log.info("  一级 %-24s %s：%d 个板块", pc, LEVEL1.get(pc, pc), stats[pc])
    for gc in sorted(l2_stats, key=lambda x: -l2_stats[x]):
        log.info("  二级 %-8s %s：%d 个板块", gc, LEVEL2.get(gc, gc), l2_stats[gc])
    return len(rows) + len(l2_rows)


def _print_mapping():
    """--list：打印每个板块 → 一级部门 / 二级组 的完整映射（不写库）。"""
    sectors = _load_industry_sectors()
    for code, name in sectors:
        pc, pn = _classify(code, name)
        gc = _SECTOR_GROUP_MAP.get(code, "-") if code.startswith("HK.") else "-"
        gname = LEVEL2.get(gc, "-") if gc != "-" else "-"
        print(f"{pc}\t{pn}\t{gc}\t{gname}\t{name}\t{code}")
    print(f"\n共 {len(sectors)} 个 INDUSTRY 板块")


if __name__ == "__main__":
    if "--list" in sys.argv:
        _print_mapping()
    elif "--dry-run" in sys.argv:
        rows, stats = _build_rows()
        print(f"dry-run：共 {len(rows)} 个板块，按一级部门分布：")
        for pc in sorted(stats, key=lambda x: -stats[x]):
            print(f"  {pc:<26} {LEVEL1.get(pc, pc)}：{stats[pc]} 个板块")
        l2_rows, l2_stats, unmapped = _build_l2_rows()
        print(f"dry-run：港股二级映射 {len(l2_rows)} 个板块，按二级组分布：")
        for gc in sorted(l2_stats, key=lambda x: -l2_stats[x]):
            print(f"  {gc:<8} {LEVEL2.get(gc, gc)}：{l2_stats[gc]} 个板块")
        if unmapped:
            print(f"  未映射（跳过）：{unmapped}")
    else:
        run()
