# HSICS 码映射复核结论与修正清单

> 核对基准：恒生官方 `B_HSICS.pdf`（与 `get_hsics_industry_classification.py` 同一数据源），实时解析得到 
> **12 个一级行业 / 31 个二级 sector / 114 个三级 subsector（6 位码）**。
> 本文件只读生成，不改动原 `hsics_code_mapping_review.md`；原文档「判定」列请按下表填写。

## 一、总结论

| 部分 | 条数 | 结论 |
|---|---|---|
| 自动匹配表 | 46 | ✅ **全部正确**。与脚本 auto 结果逐条一致，码 ↔ 官方名 ↔ 富途名三方一致，无一码多配，可直接采信 |
| 待核对候选建议 | 67 | ❌ **不能照抄**。仅 31 条正确，36 条需改正（占 54%） |
| 全量可选码参照表 | 113 | ❌ **表头与内容整体错位**，且无 6 位码，无法用于核对；另有漏码与父级错挂 |

67 条错误的构成：候选码**根本不存在于官方码表** 9 条；码存在但**张冠李戴** 25 条（其中 12 条与第一部分已占用的码直接冲突）；标「—」无对应但官方**其实有对应码** 2 条。

**根因**：候选建议码不是查官方 6 位码表得到的，而是按名称/序号想当然编的（典型如 `202010`、`352010`、`201010` 这类 GICS 式编号，以及 `235060`/`235070`/`251070`/`103050`/`103040` 这类顺推码）。按官方码表重新对齐后，46 条 auto + 67 条修正 = 113 个富途板块恰好占用 113 个互不重复的官方码，官方 114 码中仅剩 `232050` 无对应板块 —— 这个一一对应关系反证了修正方案的正确性。

## 二、逐条修正判定表（67 项，可直接填入原文档「判定」列）

| 富途 code | 富途英文名 | 原候选码 | 建议 HSICS 6 位码 | 官方英文名 | 处理 |
|---|---|---|---|---|---|
| HK.LIST1001 | Dairy | 251020 | **251020** | Dairy Products | ✅ 保持 |
| HK.LIST1002 | Procurement & Supply Chain Management | 202010 | **103010** | Sourcing & Supply Chain Service | ❌ 原码不存在 |
| HK.LIST1004 | Credit Services | 502010 | **503030** | Financing | ❌ 与 auto 冲突 |
| HK.LIST1005 | Public Transport | 103050 | **102040** | Road Transportation | ❌ 原码不存在 ⚠️ |
| HK.LIST1007 | Other Financial Services | 503040 | **503040** | Other Financials | ✅ 保持 |
| HK.LIST1008 | Agriculture | 251070 | **252020** | Agricultural Products | ❌ 原码不存在 |
| HK.LIST1010 | packaged food | 251010 | **251010** | Packaged Foods | ✅ 保持 |
| HK.LIST1011 | Agricultural Inputs | 251060 | **053010** | Fertilisers & Agricultural Chemicals | ❌ 原码不存在 |
| HK.LIST1012 | Medical Equipment & Supplies | 281010 | **282010** | Medical Devices & Supplies | ❌ 与 auto 冲突 |
| HK.LIST1014 | Satellite & Wireless Services | 350010 | **350010** | Satellite & Wireless Communication | ✅ 保持 |
| HK.LIST1017 | Commercial Vehicles | 101010 | **101010** | Commercial Vehicles & Trucks | ✅ 保持 |
| HK.LIST1019 | Real Estate Developers | 601020 | **601020** | Property Development | ✅ 保持 |
| HK.LIST1020 | Real Estate Investment | — | **601030** | Property Investment | ➕ 补对应码 |
| HK.LIST1021 | furniture | 232030 | **232040** | Household Goods | ❌ 改码 |
| HK.LIST1025 | Industrial Parts & Equipment | 101020 | **101020** | Industrial Components & Equipment | ✅ 保持 |
| HK.LIST1026 | Advertising Agencies | 352010 | **235010** | Advertising & Marketing | ❌ 原码不存在 |
| HK.LIST1029 | Entertainment | 235030 | **235030** | Movies & Entertainment | ✅ 保持 |
| HK.LIST1032 | Sports & Recreation Facilities | 235060 | **234070** | Leisure & Recreational Facilities | ❌ 原码不存在 |
| HK.LIST1034 | Travel & Sightseeing | 235070 | **234050** | Travel & Tourism | ❌ 原码不存在 |
| HK.LIST1039 | Water Utilities | 400030 | **400030** | Water | ✅ 保持 |
| HK.LIST1043 | Oil & Gas Services | 101020 | **001020** | Oil & Gas Equipment & Services | ❌ 改码 |
| HK.LIST1045 | Gas Utilities | 400020 | **400020** | Gas Distribution | ✅ 保持 |
| HK.LIST1046 | Speciality Chemicals | 053040 | **053040** | Specialty Chemicals | ✅ 保持 |
| HK.LIST1047 | Toys & Leisure | 232040 | **232030** | Toys & Leisure Products | ❌ 改码 |
| HK.LIST1049 | Jewelry & Watches | 232050 | **233040** | Jewellery & Timepieces | ❌ 改码 |
| HK.LIST1051 | Electric Utilities | 400010 | **400010** | Conventional Electricity | ✅ 保持 |
| HK.LIST1053 | Computers & Equipment | 701020 | **701020** | Computers & Peripherals | ✅ 保持 |
| HK.LIST1054 | Telecom Services | 350020 | **350020** | Telecommunication Services | ✅ 保持 |
| HK.LIST1055 | Consumer Telecommunication Equipment | 701010 | **701010** | Consumer Telecommunication | ✅ 保持 |
| HK.LIST1059 | Paper & Packaging | 053030 | **053030** | Paper | ✅ 保持 |
| HK.LIST1063 | Aerospace & Defense | 101060 | **101060** | Aerospace & Defence | ✅ 保持 |
| HK.LIST1065 | Air Cargo & Logistics | 103010 | **102030** | Air Freight & Logistics | ❌ 改码 |
| HK.LIST1066 | Shipping & Ports | 103020 | **102010** | Shipping & Port Operation | ❌ 与 auto 冲突 |
| HK.LIST1069 | Resorts & Casinos | 235040 | **234030** | Casinos & Gaming | ❌ 与 auto 冲突 ⚠️ |
| HK.LIST1070 | Supermarkets & Convenience Stores | 253010 | **253010** | Supermarkets & Convenience Stores | ✅ 保持 |
| HK.LIST1072 | Alcoholic Beverages | 251030 | **251040** | Alcohols | ❌ 与 auto 冲突 |
| HK.LIST1073 | Heavy Infrastructure | 201010 | **602030** | Heavy Construction & Engineering | ❌ 原码不存在 ⚠️ |
| HK.LIST1074 | Heavy Machinery | 101050 | **101040** | Heavy Industrial Machinery | ❌ 与 auto 冲突 |
| HK.LIST1075 | Steel | 052010 | **052010** | Iron & Steel | ✅ 保持 |
| HK.LIST1076 | Railroads & Highways | 103040 | **102020** | Railway & Tollroad Operation | ❌ 原码不存在 |
| HK.LIST1078 | Aluminum | 052030 | **052030** | Aluminium | ✅ 保持 |
| HK.LIST1083 | Catering | 234060 | **234060** | Restaurants | ✅ 保持 |
| HK.LIST1086 | Medical Services | 281020 | **282020** | Medical & Aesthetic Services | ❌ 与 auto 冲突 |
| HK.LIST1089 | Real Estate Services | — | **601010** | Real Estate Agencies | ➕ 补对应码 |
| HK.LIST1090 | Property Services & Management | 601050 | **601050** | Property Service & Management | ✅ 保持 |
| HK.LIST1095 | Engineering & Construction | 101040 | **602020** | Building Construction | ❌ 改码 ⚠️ |
| HK.LIST1268 | Other Clothing Accessories | 232010 | **233050** | Other Accessories | ❌ 与 auto 冲突 |
| HK.LIST1269 | Auto Retailers | 237010 | **237010** | Automotive Retailers | ✅ 保持 |
| HK.LIST1271 | Environmental Services | 101030 | **101030** | Environmental Engineering | ✅ 保持 |
| HK.LIST1272 | Livestock Feed | 251050 | **252030** | Animal Feeds | ❌ 与 auto 冲突 |
| HK.LIST1273 | Meat & Poultry | 251040 | **252010** | Poultry & Meat | ❌ 改码 |
| HK.LIST1274 | Electronic Components | 101025 | **101025** | Electrical & Electronic Components | ✅ 保持 |
| HK.LIST1277 | Apparel Manufacturing | 233020 | **233020** | Apparel | ✅ 保持 |
| HK.LIST1278 | Home Improvement Retail | 237030 | **237030** | Home Improvement Retailers | ✅ 保持 |
| HK.LIST1284 | Traditional Chinese Medicine | 282020 | **281030** | Chinese Medicines | ❌ 改码 |
| HK.LIST1311 | REITs | 601010 | **601040** | Real Estate Investment Trusts | ❌ 改码 |
| HK.LIST1354 | Energy Storage Systems | 101070 | **101070** | Energy Storage Units | ✅ 保持 |
| HK.LIST1355 | Transport & Logistics | 103010 | **234010** | Transportation | ❌ 改码 ⚠️ |
| HK.LIST1357 | Pharmaceutical Distribution | 282010 | **281040** | Medicine Distribution | ❌ 改码 |
| HK.LIST1358 | nuclear | 400050 | **400050** | Nuclear Energy | ✅ 保持 |
| HK.LIST1359 | Gaming | 234030 | **702040** | Game Software | ❌ 改码 ⚠️ |
| HK.LIST1360 | Semiconductor Equipment & Materials | 703020 | **703020** | Semiconductor Equipment & Materials | ✅ 保持 |
| HK.LIST23846 | Track and train equipment | 101050 | **101015** | Rail & Train Equipment | ❌ 与 auto 冲突 |
| HK.LIST23848 | household consumables | 253020 | **253030** | Non-durable Household Goods | ❌ 与 auto 冲突 |
| HK.LIST23851 | Telecommunication network infrastructure | 701015 | **701015** | Telecommunication Network | ✅ 保持 |
| HK.LIST24340 | 医药及生物科技合约服务 CRO/CDMO | 282010 | **281050** | Pharma & Biotech Contract Services | ❌ 改码 |
| HK.LIST24341 | 机器人系统及解决方案 | 101050 | **701030** | Robotic Systems & Solutions | ❌ 与 auto 冲突 |

⚠️ 标记的 6 项属语义边界，建议用成分股二次确认：

- `HK.LIST1005`：Public Transport 更接近 102040 Road Transportation；若富途口径含轨道/公交综合运输，可考虑 234010 Transportation
- `HK.LIST1069`：Resorts & Casinos 兼含度假村与博彩；234030 Casinos & Gaming 为主，也可判 234040 Hotels & Resorts，需看成分股
- `HK.LIST1073`：Heavy Infrastructure ↔ 602030 Heavy Construction & Engineering
- `HK.LIST1095`：Engineering & Construction ↔ 602020 Building Construction；与上项可对调，建议用成分股判定
- `HK.LIST1355`：Transport & Logistics 为综合物流，234010 Transportation 为兜底；若实为空运物流则 102030
- `HK.LIST1359`：Gaming 在富途 GICS 归 COMMUNICATION/媒体娱乐，判 702040 Game Software；若实为博彩则 234030（与 HK.LIST1069 二选一）

### 最危险的 12 条（会把两个不同板块塞进同一个官方码）

| 富途 code | 原候选码 | 该码官方真实含义 | 已占用它的板块 | 应改为 |
|---|---|---|---|---|
| HK.LIST1004 | 502010 | Insurance | HK.LIST1003 | **503030** Financing |
| HK.LIST1012 | 281010 | Pharmaceuticals | HK.LIST1067 | **282010** Medical Devices & Supplies |
| HK.LIST1066 | 103020 | Printing & Packaging | HK.LIST1015 | **102010** Shipping & Port Operation |
| HK.LIST1069 | 235040 | Publishing | HK.LIST1009 | **234030** Casinos & Gaming |
| HK.LIST1072 | 251030 | Non-alcoholic Beverages | HK.LIST1080 | **251040** Alcohols |
| HK.LIST1074 | 101050 | New Energy Materials | HK.LIST1033 | **101040** Heavy Industrial Machinery |
| HK.LIST1086 | 281020 | Biotechnology | HK.LIST1050 | **282020** Medical & Aesthetic Services |
| HK.LIST1268 | 232010 | Home Appliances | HK.LIST1022 | **233050** Other Accessories |
| HK.LIST1272 | 251050 | Food Additives | HK.LIST1082 | **252030** Animal Feeds |
| HK.LIST23846 | 101050 | New Energy Materials | HK.LIST1033 | **101015** Rail & Train Equipment |
| HK.LIST23848 | 253020 | Personal Care | HK.LIST1062 | **253030** Non-durable Household Goods |
| HK.LIST24341 | 101050 | New Energy Materials | HK.LIST1033 | **701030** Robotic Systems & Solutions |

## 三、参照表的问题与修正

原第三部分表头为「二级 sector | 三级码 | 三级英文名」，实际三列内容是「**三级子行业英文名 | 二级 4 位码 | 一级 2 位码**」—— 列名与内容整体错位，且**完全没有 6 位码**，而待核对表要填的正是 6 位码，因此该表无法承担核对参照的作用。另有 3 处数据缺陷：

1. **漏 1 个官方码**：`103010 Sourcing & Supply Chain Service`（属 1030 Commercial & Professional Services）。PDF 里该行是 `Commercial & Professional Services 103010`（名称与码同行），解析脚本只认纯数字行因而漏掉。它恰好是 `HK.LIST1002 Procurement & Supply Chain Management` 的正确对应码。
2. **漏 1 个一级 / 1 个二级**：PDF 里 `Telecommunications 3500` 同样是合并行，导致 `35` Telecommunications 与 `3500` 未被识别；`350010/350020` 被错挂到 `28 / 2820`（医疗保健）。正确父级应为 `35 / 3500`。
3. **名称污染**：`253010`、`281050` 混入整段描述文字，`703020` 被截断成 `Semiconductor Equipment &`，会拉低英文名自动匹配率。

修正后的权威参照表（114 行，按 6 位码排序）：

| 三级 6 位码 | 三级子行业官方名 | 二级码 | 一级码 |
|---|---|---|---|
| 001010 | Oil & Gas Producers | 0010 | 00 |
| 001020 | Oil & Gas Equipment & Services | 0010 | 00 |
| 002010 | Coal | 0020 | 00 |
| 051010 | Gold & Precious Metals | 0510 | 05 |
| 052010 | Iron & Steel | 0520 | 05 |
| 052020 | Copper | 0520 | 05 |
| 052030 | Aluminium | 0520 | 05 |
| 052040 | Other Metals & Minerals | 0520 | 05 |
| 053010 | Fertilisers & Agricultural Chemicals | 0530 | 05 |
| 053020 | Forestry & Timber | 0530 | 05 |
| 053030 | Paper | 0530 | 05 |
| 053040 | Specialty Chemicals | 0530 | 05 |
| 101010 | Commercial Vehicles & Trucks | 1010 | 10 |
| 101015 | Rail & Train Equipment | 1010 | 10 |
| 101020 | Industrial Components & Equipment | 1010 | 10 |
| 101025 | Electrical & Electronic Components | 1010 | 10 |
| 101030 | Environmental Engineering | 1010 | 10 |
| 101040 | Heavy Industrial Machinery | 1010 | 10 |
| 101050 | New Energy Materials | 1010 | 10 |
| 101060 | Aerospace & Defence | 1010 | 10 |
| 101070 | Energy Storage Units | 1010 | 10 |
| 102010 | Shipping & Port Operation | 1020 | 10 |
| 102020 | Railway & Tollroad Operation | 1020 | 10 |
| 102030 | Air Freight & Logistics | 1020 | 10 |
| 102040 | Road Transportation | 1020 | 10 |
| 103010 | Sourcing & Supply Chain Service | 1030 | 10 |
| 103020 | Printing & Packaging | 1030 | 10 |
| 231010 | Automobiles | 2310 | 23 |
| 231020 | Auto Parts | 2310 | 23 |
| 231030 | Motorcycles and Others | 2310 | 23 |
| 232010 | Home Appliances | 2320 | 23 |
| 232020 | Consumer Electronics | 2320 | 23 |
| 232030 | Toys & Leisure Products | 2320 | 23 |
| 232040 | Household Goods | 2320 | 23 |
| 232050 | Pet Products | 2320 | 23 |
| 233010 | Textiles & Fabrics | 2330 | 23 |
| 233020 | Apparel | 2330 | 23 |
| 233030 | Footwear | 2330 | 23 |
| 233040 | Jewellery & Timepieces | 2330 | 23 |
| 233050 | Other Accessories | 2330 | 23 |
| 234010 | Transportation | 2340 | 23 |
| 234020 | Airlines | 2340 | 23 |
| 234030 | Casinos & Gaming | 2340 | 23 |
| 234040 | Hotels & Resorts | 2340 | 23 |
| 234050 | Travel & Tourism | 2340 | 23 |
| 234060 | Restaurants | 2340 | 23 |
| 234070 | Leisure & Recreational Facilities | 2340 | 23 |
| 235010 | Advertising & Marketing | 2350 | 23 |
| 235020 | Broadcasting | 2350 | 23 |
| 235030 | Movies & Entertainment | 2350 | 23 |
| 235040 | Publishing | 2350 | 23 |
| 235050 | Interactive Media & Services | 2350 | 23 |
| 236010 | Education | 2360 | 23 |
| 236020 | Other Support Services | 2360 | 23 |
| 237010 | Automotive Retailers | 2370 | 23 |
| 237020 | Apparel Retailers | 2370 | 23 |
| 237030 | Home Improvement Retailers | 2370 | 23 |
| 237040 | Diversified Retailers | 2370 | 23 |
| 237050 | Other Retailers | 2370 | 23 |
| 237060 | Online Retailers | 2370 | 23 |
| 251010 | Packaged Foods | 2510 | 25 |
| 251020 | Dairy Products | 2510 | 25 |
| 251030 | Non-alcoholic Beverages | 2510 | 25 |
| 251040 | Alcohols | 2510 | 25 |
| 251045 | Tobacco | 2510 | 25 |
| 251050 | Food Additives | 2510 | 25 |
| 252010 | Poultry & Meat | 2520 | 25 |
| 252020 | Agricultural Products | 2520 | 25 |
| 252030 | Animal Feeds | 2520 | 25 |
| 253010 | Supermarkets & Convenience Stores | 2530 | 25 |
| 253020 | Personal Care | 2530 | 25 |
| 253030 | Non-durable Household Goods | 2530 | 25 |
| 281010 | Pharmaceuticals | 2810 | 28 |
| 281020 | Biotechnology | 2810 | 28 |
| 281030 | Chinese Medicines | 2810 | 28 |
| 281040 | Medicine Distribution | 2810 | 28 |
| 281050 | Pharma & Biotech Contract Services | 2810 | 28 |
| 282010 | Medical Devices & Supplies | 2820 | 28 |
| 282020 | Medical & Aesthetic Services | 2820 | 28 |
| 282030 | Dietary Supplements | 2820 | 28 |
| 282040 | Skincare and Cosmetics | 2820 | 28 |
| 350010 | Satellite & Wireless Communication | 3500 | 35 |
| 350020 | Telecommunication Services | 3500 | 35 |
| 400010 | Conventional Electricity | 4000 | 40 |
| 400020 | Gas Distribution | 4000 | 40 |
| 400030 | Water | 4000 | 40 |
| 400040 | Alternative/ Renewable Energy | 4000 | 40 |
| 400050 | Nuclear Energy | 4000 | 40 |
| 501010 | Banks | 5010 | 50 |
| 502010 | Insurance | 5020 | 50 |
| 503010 | Securities & Brokerage | 5030 | 50 |
| 503020 | Investment & Asset Management | 5030 | 50 |
| 503030 | Financing | 5030 | 50 |
| 503040 | Other Financials | 5030 | 50 |
| 503050 | Payment Services | 5030 | 50 |
| 601010 | Real Estate Agencies | 6010 | 60 |
| 601020 | Property Development | 6010 | 60 |
| 601030 | Property Investment | 6010 | 60 |
| 601040 | Real Estate Investment Trusts | 6010 | 60 |
| 601050 | Property Service & Management | 6010 | 60 |
| 602010 | Construction Materials | 6020 | 60 |
| 602020 | Building Construction | 6020 | 60 |
| 602030 | Heavy Construction & Engineering | 6020 | 60 |
| 701010 | Consumer Telecommunication | 7010 | 70 |
| 701015 | Telecommunication Network | 7010 | 70 |
| 701020 | Computers & Peripherals | 7010 | 70 |
| 701030 | Robotic Systems & Solutions | 7010 | 70 |
| 702015 | Digital Solution Services | 7020 | 70 |
| 702025 | Internet Services and Infrastructure | 7020 | 70 |
| 702030 | Application Software | 7020 | 70 |
| 702040 | Game Software | 7020 | 70 |
| 703010 | Semiconductors | 7030 | 70 |
| 703020 | Semiconductor Equipment & Materials | 7030 | 70 |
| 800010 | Conglomerates | 8000 | 80 |

## 四、代码侧修改建议（否则下次采集仍会复现）

`get_hsics_industry_classification.py::_parse_pdf()` 当前逻辑是「只把纯数字独立行当作码，6 位码后一行当作名称，向上找最近的 4 位 / 2 位独立行当父级」，这个假设在官方 PDF 的合并行处失效。建议：

1. 先把每行归一化为 `(码, 名称)`：用宽松正则识别「名称 + 码」同行，例如 `^(?:.*?\s)?(\d{2}|\d{4}|\d{6})$`，把 `Telecommunications 3500` 拆成 sector 码 `3500`、把 `Commercial & Professional Services 103010` 拆出 subsector 码 `103010`；名称取紧随其后的文本行。
2. 父级归属改用在归一化后的码序列上向前回溯，而不是在原始文本行上回溯，避免 `350010/350020` 挂到 `2820`。
3. 名称清洗：截掉描述句（以 `Companies`/`Owners and operators`/`Providers` 等开头的后续文本），并补回被换行截断的 `703020 Semiconductor Equipment & Materials`。
4. 把现有的 `if len(hsics) < 100` 弱断言换成硬校验：`industry == 12 / sector == 31 / subsector == 114`，不符直接拒绝写库（当前 113/11/30 就是靠弱断言放过去的）。
5. auto 匹配加「一码多配」检测：同一 6 位码被多个富途板块命中时告警并转 manual，避免人工核对时把两个板块写进同一个官方码。

## 五、文档口径同步

- `doc/industry_classification_refactor_plan.md` 多处写「70 自动 + 43 人工核对」，实测为 **46 自动 + 67 人工**（合计 113 个富途板块），验收清单、风险表、阶段 B 描述需一并更新。
- 官方层级数按 **12 / 31 / 114** 表述（现文档写 12 / 31 / 113）。
- 原 `hsics_code_mapping_review.md` 第三部分的列名与内容需按上文第 3 节重生成，或直接以本文件第 3 节的 114 行表替换。
