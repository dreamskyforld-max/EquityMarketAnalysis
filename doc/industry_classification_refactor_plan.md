# 行业分类体系统一整改计划

> **文档性质**：整改计划（决策已定，待执行）。目标是把当前"三套行业分类并存、口径别扭"的现状，改为 **A 股统一申万、港股统一 HSICS**，各自三级映射、**一级/二级并存**。
> **决策依据**：2026-11-19~20 系列讨论与定量分析（见 §1 背景）；全部决策见 §2（已定稿）。
> **相关文档**：`doc/industry_allocation_feasibility.md`（第④层设计，C2 A/H 分离约束）、`doc/stock_profile_l2_l3.md`（画像标签与 L2/L3 契约）、`AGENTS.md`（DB schema 与计算任务资源铁律）。
> **状态**：**决策已定稿，可开工**。标签 code/name 清单（§6）与行业特征编码方案（§3.2）已获确认。按阶段 A→B→C→D→E 执行。

---

## 0. 一页摘要

| 项 | 内容 |
|---|---|
| 问题 | 画像用 GICS 一级/二级（13/25 类）、行业层 HK 用 HSICS 三级（113）、CN 用申万一级（31），三套分类并存，无法对齐 |
| 目标 | A 股统一 **申万**、港股统一 **HSICS**；各自三级映射，**画像与行业特征均一级/二级并存**（两套标签/指标同时计算） |
| 已验证的数据前提 | 申万三级指数有 1999 起长历史（实测 850111 6494 行）；AKShare 可拉申万三级 336 行业/5223 只个股映射；HSICS 官方 PDF 可解析 114 个 subsector 层级（修正后）；港股官方码走"两跳方案"（无免费直连源） |
| 定量结论 | 二级 = 样本量与同质性的最佳平衡（HK.00700 三级/二级/一级分位 27.78/28.57/31.68；SH.688981 83.33/77.86/83.29）；但**一级同样保留**作宏观对照，二者并存 |
| 五阶段 | A 行业信息采集 → B 个股-行业映射采集 → C 画像标签重构（一/二级并存）→ D 行业特征重构（一/二级并存）→ E 切换与验收 |
| 最大风险 | L2/L3 结论暂不计算（待行业改造完成后优化）；计算资源红线（申万三级 336 行业 × 6500 行 ≈ 220 万行） |

---

## 1. 背景与现状

### 1.1 三套分类并存（问题根源）

| 体系 | 用在哪 | 分类单位 | 数量 | 来源 |
|---|---|---|---|---|
| GICS 一级/二级 | 个股画像所有行业相关标签（13+ 个） | `sector_hierarchy.parent_code` / `_l2.group_code` | 13 / 25 | 人工语义映射（富途板块/证监会行业 → GICS） |
| HSICS 三级 | 港股行业特征（`sector_layer_hk.py`） | `stock_sector.sector_code`（HK.LIST*） | 113 | 富途采集，恒生三级子行业 |
| 申万一级 | A 股行业特征（`sector_layer.py`） | `regime.sector_valuation_snapshot.sw_code`（改造后 → `regime.sw_industry_valuation_snapshot.sw_code`） | 31 | 申万官方 |

"别扭"的具体表现：同为"行业 PE 分位"，画像的 `val_pe_ind_pct` 按 GICS 13 类、行业层的 `SEC.VAL.PE_XSEC_PCT` 按申万 31 或恒生 113；两者分母不同、无法互相印证，且 A 股画像的 GICS 映射由"证监会首字母→GICS"粗暴推出（2139 只 A 股挤在 INDUSTRIALS 一组）。

### 1.2 已验证的数据前提（本轮实测）

| 验证项 | 结果 |
|---|---|
| 申万三级行业数 | `ak.sw_index_third_info()` = **336** 个；二级 131；一级 31 |
| 申万三级指数历史 | `ak.index_hist_sw("850111")` = **1999-12-30 起 6494 行**；部分行业（如 851921）中途停止（分类调整） |
| 申万成分股接口 | `ak.index_component_sw(symbol="850111")` = 8 只，含**计入日期**；336 行业合计 5223 只映射 |
| 坏接口 | `ak.sw_index_third_cons()` 在 akshare 1.18.60 报 `Length mismatch`（上游列数变了），**禁用**，统一用 `index_component_sw` |
| HSICS 层级 | 官方 PDF 解析出 **114 个 subsector**（12 industry / 31 sector / 114 subsector，2024 版；原解析器漏 1 码已修）；`702015 Digital Solution Services` 归属 `70 Information Technology → 7020 Software & Services` |
| 港股富途板块 | 113 个 HK.LIST* 板块，名称即 HSICS 子行业英文名；其中 48 个自动匹配、65 个转人工；用户复核后 46 auto + 67 人工确认（confirmed），113 板块占用 113 个唯一官方码 |

### 1.3 定量甄选（as_of 2026-11-19，正 PE）

| 股票 | 三级 | 二级 | 一级 | 三级 n / 一级 CV |
|---|---|---|---|---|
| HK.00700（HSICS） | 702015: 27.78 | 7020: 28.57 | 70: 31.68 | 18 / 1.72 |
| SH.688981（申万） | 850816: 83.33 | 半导体: 77.86 | 电子: 83.29 | 6 / 2.08 |

**结论**：三级样本不足（中芯仅 6 只，分位步长 16.7pp），一级异质性高（组内 PE 6~2500）；**二级是平衡点**，定为主口径。

---

## 2. 决策清单（全部已定稿，2026-11-20）

| # | 决策项 | 结论 |
|---|---|---|
| D0 | A/H 体系 | A 股与港股**彻底分成两套**，不跨市场混排（延续 C2 约束） |
| D1 | 行业层级 | **一级/二级并存**：画像标签与行业特征都**同时计算一级和二级**两套，不是二选一 |
| D2 | GICS 标签 | **废弃、原地替换**：原 `val_pe_ind_pct` 等 GICS 标签废弃，新标签用申万/HSICS 口径；code/name 同步更换为可识别层级（见 §6） |
| D3 | L2/L3 结论 | **本期暂不计算**，待行业改造完成后再优化。原 L2/L3 依赖 `val_pe_ind_pct`（已被废弃替换），改造完成前停止产出（见 §4 阶段 C 注） |
| D4 | 港股 code | **用 HSICS 官方 6 位码**，走**两跳方案**：富途 `HK.LISTxxxx` → 富途英文名 ↔ HSICS 官方英文名对照（最终落库 46 auto + 67 人工 confirmed）→ 官方码；无免费直连源（AKShare 无接口、理杏仁为自有码、恒生云不公布三级） |
| D5 | 画像标签命名 | 一级/二级拆两个 tag，code 含 `ind1`/`ind2`、name 含"一级行业/二级行业"（见 §6，已逐项确认） |
| D6 | 行业特征命名 | code 加 `_L1`/`_L2` 后缀、name 加"一级行业/二级行业"前缀（见 §3.2，已确认）；两市场仍靠 `market` 列区分 |
| D7 | 行业特征表结构 | 采用 **X 方案**：值表（`indicator_value`/`dimension_value`/`business_value`）**不加** `classification` 列，靠 `sector_code` 取值 + `indicator_code` 的 `_L1/_L2` 后缀区分层级 |
| D8 | 字典表 `industry.indicator_dict` | **新增 `industry_level` 字段**（数值 1/2/3），标识该指标对应的行业层级，便于后续按层级快速定位指标。⚠ 与 D7 不冲突：D7 针对**值表**，D8 针对**字典表**。改造后每个指标字典行带 `_L1`/`_L2` 后缀并填 `industry_level=1/2`；值 `3` 预留（三级行业指标将来启用） |
| D9 | 阶段 A 脚本命名 | **新建**：`get_sw_industry_classification.py`、`get_hsics_industry_classification.py`；**改名**：`backfill_sw_industry.py` → `get_sw_industry_quotes.py`、`get_sector_valuation.py` → `get_sw_industry_valuation.py`。名实匹配：分类/行情/估值命名族 |
| D10 | 全量回填拆分 + 表名同步 | ① `backfill_sw_industry.py` 的 `--full` 全量回填功能**去掉**，新建 **`backfill_sw_industry_classification.py`** 承载（独立 unit 手工跑）；② **数据库表名同步更改**：`regime.sector_valuation_snapshot` → `regime.sw_industry_valuation_snapshot`（脚本名与表名一致） |
| D11 | 层级表命名 + 码映射拆表 | ① 两张层级表去掉泛泛的 `info`：`sw_industry_info` → **`sw_industry_classification`**、`hsics_industry_info` → **`hsics_industry_classification`**（与脚本同名族）；② 富途码↔HSICS 码对照**独立成表** `regime.futu_hsics_code_mapping`，不与分类层级混存（单一职责） |
| D12 | A 股个股-行业映射收口 | 统一到 `regime.stock_sw_industry_mapping`（`get_stock_sw_industry_mapping.py` 唯一维护），**废除** `industry.constituent`/`constituent_change`/`get_sector_constituent.py` 及全部引用 |
| D13 | 映射表结构 | 一行一股票（PK `stock_code`），三层级横向展开 `sw_code_l1/l2/l3` + `weight_l1/l2/l3` + `as_of`；**不存** `eff_from/eff_to` 历史版本 |
| D14 | 行业指标 PIT 取舍 | 仍逐日算历史序列（2021-12-13 起），但成分用**最新名单**贴历史（放弃 PIT，接受幸存者偏差）；`pit_capable=false` 标注 |
| D15 | 档位型标签加连续百分位 —— **已撤销** | 评估后决定 `num_value` 保持原值语义（PS倍数/ROE%等），不新增百分位存储：加列要改 engine.py 核心写入与版本判断（pct 日变会触发大量新版本行），且 conclusion_detail 是 L2/L3 专用不可复用 |

**历史说明**：`val_pe_ind2_pct` 在 2026-11-19 曾作为"GICS 二级"标签创建并落库（`eff_from=2026-11-19`）。按 D2/D5，该 code **复用但改口径**（GICS 二级 → 申万/HSICS 二级），当日已产出的值全部作废重算。

---

## 3. 总体架构（目标态）

```
                      A 股                              港股
行业信息        申万 一级31/二级131/三级336        HSICS 一级12/二级31/三级114
  │                    │                                  │
个股映射    stock_sw_industry（三级+二级+一级）    stock_hsics_industry（三级+二级+一级，官方码）
  │                    │                                  │
画像标签    market|sw_L1 + market|sw_L2          market|hsics_L1 + market|hsics_L2
  │         （每标签两个：_ind1 / _ind2，成对计算）  （同一批标签，按市场分支）
  │
L2/L3 结论   本期暂停产出（D3），行业改造完成后再优化

行业特征    sector_layer.py 申万 L1+L2            sector_layer_hk.py HSICS L1+L2
            SEC.*_L1 / SEC.*_L2 成对               SEC.*_L1 / SEC.*_L2 成对
```

**铁律不变**：A/H 物理分表、不混合分位/排名（C2）；所有分位 as-of 计算、禁事后刷写（C4）。

### 3.2 行业特征编码/名称方案（D6/D7 已确认）

**命名规则**：

- **code**：`SEC.<域>.<指标>` → `SEC.<域>.<指标>_L1`（一级）/ `SEC.<域>.<指标>_L2`（二级）
- **name**：原中文名前加"一级行业"/"二级行业"（如 `行业PE-TTM` → `一级行业PE-TTM` / `二级行业PE-TTM`）
- 两市场（CN/HK）仍靠 `market` 列区分，不另建 code 体系

**覆盖范围**（全部指标成对产出，原 41 CN + 25 HK code）：

| 域 | code 变换示例 | name 变换示例 |
|---|---|---|
| TREND | `SEC.TREND.RET_20D` → `...RET_20D_L1` / `_L2` | `行业20日收益` → `一级行业20日收益` / `二级行业20日收益` |
| VAL | `SEC.VAL.PE_TTM` → `...PE_TTM_L1` / `_L2` | `行业PE-TTM` → `一级行业PE-TTM` / `二级行业PE-TTM` |
| FLOW | `SEC.FLOW.NET_1D` → `...NET_1D_L1` / `_L2` | `行业资金净流入` → `一级行业资金净流入` / `二级行业资金净流入` |
| CROWD | `SEC.CROWD.GATE` → `...GATE_L1` / `_L2` | `拥挤档位` → `一级行业拥挤档位` / `二级行业拥挤档位` |
| BREADTH | `SEC.BREADTH.UP_RATIO` → `...UP_RATIO_L1` / `_L2` | `行业内上涨占比` → `一级行业内上涨占比` / `二级行业内上涨占比` |
| FUND（CN） | `SEC.FUND.ROE_MEDIAN` → `...ROE_MEDIAN_L1` / `_L2` | `行业ROE中位数` → `一级行业ROE中位数` / `二级行业ROE中位数` |

**表结构**：

- **值表（D7 X 方案）**：`industry.indicator_value` / `dimension_value` / `business_value` **不加 `classification` 列**。一级/二级靠两点区分：
  1. `sector_code` 取值域不同（申万一级 `801010` vs 二级 `801011`；HSICS 一级 `70` vs 二级 `7020`）；
  2. `indicator_code` 的 `_L1` / `_L2` 后缀（维度/业务层无后缀，靠 `sector_code` 区分）。
  查询侧按需用 `indicator_code LIKE '%\_L1'` 等过滤（注意 SQL 下划线转义）。

- **字典表（D8 新增字段）**：`industry.indicator_dict` 增加 `industry_level SMALLINT`（值域 1/2/3，可为 NULL 表示非行业层级指标）。
  - 现状：42 行 / 42 个唯一 `indicator_code`，主键即 `indicator_code`，`market_scope='CN+HK'`（同 code 描述两市场）。
  - 改造后：字典行随 `_L1`/`_L2` code 拆分，每行填 `industry_level=1` 或 `2`（对应后缀），`3` 预留给将来的三级行业指标。
  - 用途：按 `industry_level` 快速过滤某层级全部指标，不再靠解析 code 后缀。
  - 落地三处（已核实）：
    1. `regime_schema.py`：`_DDL["sector_indicator_dict"]` 增列 + 增量迁移 `ALTER ... ADD COLUMN IF NOT EXISTS industry_level SMALLINT`（幂等，照 v2.1/v2.1.1 增量列写法）+ `_SECTOR_COMMENTS` 加 COMMENT；
    2. `sector_layer.py` / `sector_layer_hk.py` 的 `D()` 构造与 `sync_dict()` 写入新增字段（值取 `1`/`2`，随 `_L1`/`_L2` code）；
    3. `sql/schema.sql` 第④层字典表定义逐字同步（AGENTS.md 强制），并跑 `_schema_diff.py` / `_code_schema_diff.py` 确认无漏写。

---

## 4. 分阶段实施计划

### 阶段 A：行业信息采集

**A1 申万行业分类层级表（新建）**

- 数据源：`ak.sw_index_first_info()`（31）/`sw_index_second_info()`（131）/`sw_index_third_info()`（336），三者自带"上级行业"字段可串成层级链。
- 新表 `regime.sw_industry_classification`：`sw_code / sw_name / level(1/2/3) / parent_code / as_of`。存**纯分类层级**，不含行情/估值/映射数据（单一职责）。
- 与现有指数采集脚本（改名为 `get_sw_industry_quotes.py`）的 `SW_L1` 常量（写死 31 个）对接：改为读层级表，不破坏 `--only` / `--list` CLI。

**A2 申万行业指数历史（扩容，脚本改名+回填拆分）**

- 现状：`daily_benchmark` 已有 31 个 `CN.SW*`（1999 起）；采集脚本 `backfill_sw_industry.py`（D9 改为 **`get_sw_industry_quotes.py`**）。
- 扩容：三级 336 个行业指数，每个 ~6500 行 ≈ **220 万行**。
- **D10 拆分**：`get_sw_industry_quotes.py` 只做**日常增量**（每工作日 18:10，去掉 `--full`）；新脚本 **`backfill_sw_industry_classification.py`** 承载**首次全量回填**（独立 unit 手工跑，17 批 × 20 行业）。
- ⚠ **计算资源红线**（AGENTS.md）：全量回填必须独立 systemd unit（`oneshot` + `MemoryMax=512M` + `MemorySwapMax=0` + `RuntimeMaxSec=3600` + `Nice=10` + `IOSchedulingPriority=7` + `OOMScoreAdjust=500`），分批（每批 ≤20 行业），单事务写 ≤5 万行；禁止塞进 `market_scheduler`。上线前 `/usr/bin/time -v` 实测峰值内存并留 2 倍余量。
- 部分三级行业中途停止（如 851921 到 2021），需记录 `last_trade_date` 并标 BREAK 闸门。

**A3 HSICS 行业分类层级表（新建）**

- 数据源：恒生官方 PDF（`B_HSICS.pdf`，可 `pymupdf` 解析，已验证可提取 114 条 subsector + 父级 sector/industry（含合并行/跨行解析修复））。英文名取 `en_hk` 版；中文名取 `zh_hk` 官方中文版，繁体经 NFKC 归一化 + OpenCC `t2s` 转简体（2026-11-24 新增，按官方码直接对齐，不意译）。
- 新表 `regime.hsics_industry_classification`：`subsector_code / subsector_name / subsector_name_cn / sector_code / sector_name / sector_name_cn / industry_code / industry_name / industry_name_cn / as_of`（与 `sql/schema.sql` 一致）。存**纯 HSICS 官方分类层级**，不含富途码、不含映射（单一职责）。

**A3b 富途码↔HSICS 官方码映射表（新建，D11 拆表）**

- 新表 `regime.futu_hsics_code_mapping`：`futu_code / futu_name / hsics_code / match_method(auto/manual) / confirmed / as_of`（与 `sql/schema.sql` 一致）。
- 用途：D4 两跳方案的中间层——富途 `HK.LISTxxxx` → HSICS 官方 6 位码；48 个按英文名自动匹配，65 个转人工；用户已复核确认 67 项，最终落库 46 auto + 67 manual（全部 confirmed=true）（不被每日自动刷新覆盖）。
- 与 A3 分工：A3 存官方分类层级、A3b 存码映射，**两类主数据分离**，避免分类刷新污染人工核对结果；测试钉住关键样本（HK.00700=702015）。

**A4 A 股行业估值快照扩容（一/二级并存，脚本+表名同步改名）**

- 脚本 `get_sector_valuation.py` → **`get_sw_industry_valuation.py`**（D9）；表名 `regime.sector_valuation_snapshot` → **`regime.sw_industry_valuation_snapshot`**（D10）。
- 新增 `ak.sw_index_second_info()` 逐日快照（131 二级行业/日），与现有一级 31 行业快照并存。
- ⚠ **表名改动影响面**（已核实 20+ 处引用，必须全同步）：`regime_schema.py`（DDL/索引/COMMENT）、`sector_layer.py`（`_D_SNAP` 及 6 处 `source=`）、`monitor_collector.py`（表监控登记 2 处）、`tests/test_sector_indicators.py`（4 处 SQL）、`tools/validate_sector.py`、`tools/probe_sector_constituent.py`、`sector_layer_hk.py`（remark 文案）、`market_scheduler.py`（任务名引用）、相关文档。迁移用 `ALTER TABLE ... RENAME TO`（保留数据），不用重建。

**A5 阶段 A 脚本清单（改名 / 改造 / 新建，D9/D10）**

| 脚本 | 动作 | 用途 | 触发方式 |
|---|---|---|---|
| `get_sw_industry_classification.py` | **新建** | A1：三级/二级/一级行业层级表采集（AKShare 三个 info 接口） | 每日 08:10（见 A6） |
| `get_sw_industry_quotes.py`（原 `backfill_sw_industry.py`） | **改名+改造** | A2：行业指数**日常增量**采集；从"一级 31 写死常量"改为读层级表；扩容到三级 336 | 每工作日 18:10（现有时点不变） |
| `backfill_sw_industry_classification.py` | **新建** | A2/D10：承载原 `--full` **首次全量回填**（336 行业 × 6500 行，独立 unit 分批） | 手工触发（一次性/大修后） |
| `get_hsics_industry_classification.py` | **新建** | A3+A3b：HSICS 官方 PDF 解析写分类层级表 + 富途板块↔官方码映射（46 auto + 67 人工 confirmed）写映射表 | 每日 08:15（见 A6） |
| `get_sw_industry_valuation.py`（原 `get_sector_valuation.py`） | **改名+改造** | A4：估值快照从一级 31 扩到一级+二级（131），写新表 `sw_industry_valuation_snapshot` | 每工作日 18:15（现有时点不变） |
| `regime_schema.py` | **改造** | 新增三张表（`sw_industry_classification`/`hsics_industry_classification`/`futu_hsics_code_mapping`）的 DDL/COMMENT/迁移 key + 估值表改名 + `indicator_dict.industry_level`（D8） | 建表/迁移时手工执行，非周期任务 |

> 说明：A1/A3 拆成独立采集脚本而非塞进行情脚本，是因为层级表是**低频参考数据**（月度/年度），与指数日频采集的频率、资源、失败影响面都不同；分开后层级表坏了不影响日频指数采集。全量回填再独立成 `backfill_*`（D10），避免日常采集脚本背负历史回填的资源与运维复杂度。

**A6 新建/改名脚本的采集时间计划**

| 脚本 | 频率/时点 | 调度载体 | 实测核心耗时 / 错峰 |
|---|---|---|---|
| `get_sw_industry_classification.py` | **每日 08:10**（改为日频） | `market_scheduler.GLOBAL_TASKS` 新条目（进程内） | **实测 AKShare 三接口 ≈20.4s**（31/131/336 行），远低于 300s 阈值；08:20 有「行业映射重算」，08:10 前置取其输入 |
| `get_sw_industry_quotes.py` | 每工作日 18:10（日常增量，**已去掉 `--full`**） | `market_scheduler` 现有「申万行业指数」条目改名 | 现有时点不变 |
| `backfill_sw_industry_classification.py` | 手工触发（首次全量/大修后一次性） | **独立 oneshot unit**（不挂 `market_scheduler`） | 错峰手工执行，不占日常调度 |
| `get_hsics_industry_classification.py` | **每日 08:15**（改为日频） | `market_scheduler.GLOBAL_TASKS` 新条目（进程内） | **实测 PDF 下载+解析+匹配 ≈0.9s**（114 subsector）；08:10 申万层级表之后顺序执行，无冲突 |
| `get_sw_industry_valuation.py` | 每工作日 18:15（现有时点不变） | `market_scheduler` 现有「行业估值快照」条目改名 | 增二级 131 行/日，耗时仍 <300s 阈值 |

> **耗时实测（2026-11-20，本机）**：`sw_index_first_info` 11.1s / `sw_index_second_info` 4.9s / `sw_index_third_info` 4.5s → 合计 **20.4s**（含网络延迟）；HSICS PDF 下载 0.4s + 解析 114 subsector 0.4s + 名称匹配 0.07s → **约 0.9s**。两者均属轻量，按用户决策改为**每日**执行（不再月度/季度），HSICS 官方分类调整时靠 confirmed 行保护人工核对结果不被覆盖。

**⏸ 首次全量回填的运维约束**：`backfill_sw_industry_classification.py` 处理 336 行业 × 6500 行 ≈ 220 万行，走**独立 oneshot unit**（`MemoryMax=512M`/`MemorySwapMax=0`/`RuntimeMaxSec=3600`/`Nice=10`/`IOSchedulingPriority=7`/`OOMScoreAdjust=500`），分 17 批（20 行业/批）、单事务 ≤5 万行；禁止在 `market_scheduler` 进程内跑（R2/R5）。实测峰值写入提交说明。

**阶段 A 验收**：三张表齐全——`sw_industry_classification`（336 行）、`hsics_industry_classification`（114 行）、`futu_hsics_code_mapping`（113 行，67 项人工 confirmed）；申万指数历史行数与官方一致，`sw_industry_valuation_snapshot` 迁移后数据完整、20+ 引用点全同步，资源实测报告归档；新调度条目（`get_sw_industry_classification`/`get_hsics_industry_classification`/`get_sw_industry_quotes`/`get_sw_industry_valuation`）与独立回填 unit 在 `market_scheduler` 与 `system/` 中登记。

---

### 阶段 B：个股-行业映射关系采集（统一收口，2026-11-20 定稿）

**架构决策（D12~D14）**：

| # | 决策 |
|---|---|
| D12 | **A 股个股↔申万行业映射统一收口到一张表** `regime.stock_sw_industry_mapping`，由 `get_stock_sw_industry_mapping.py` 唯一维护；**废除** `industry.constituent` / `industry.constituent_change` / `get_sector_constituent.py` 及全部引用（14 文件） |
| D13 | 表采用**一行一股票**、三层级横向展开：`stock_code` PK + `sw_code_l3` + `weight_l1/l2/l3` + `as_of`；**不存** `eff_from/eff_to` 历史版本（用户决策） |
| D14 | **行业指标仍逐日算历史序列**（2021-12-13 起），但成分用**最新名单**贴历史（放弃 PIT 精度，接受幸存者偏差）；`pit_capable=false` 写入所有受影响指标 remark |

**B1 申万映射（新建 `regime.stock_sw_industry_mapping`）**

- 脚本 `get_stock_sw_industry_mapping.py`：遍历 498 个申万行业（一级31/二级131/三级336）调 `ak.index_component_sw`，**按股票聚合**成一行（三码 + 三权重，实测同股三层 weight 不同：中芯国际 3.39%/5.97%/60.33%）；幂等 upsert（同 stock_code 覆盖）。
- 字段：`stock_code(PK) / sw_code_l3 / weight_l1 / weight_l2 / weight_l3 / as_of / updated_at`。
- ⚠ 实测耗时：498 行业 ≈ **138s**（15669 成分行），调度阈值内。
- 每日 **08:20** 调度（用户定；实测可接受）。

**B2 港股映射（新建 `regime.stock_hsics_industry_mapping`）**

- 脚本 `get_stock_hsics_industry_mapping.py`：从 `stock_sector`(113 HK.INDUSTRY 板块) + `futu_hsics_code_mapping` + `hsics_industry_classification` 关联，写 `stock_code / hsics_code_l3 / futu_sector_code / as_of`。
- 2773 只港股、113 板块（已核对覆盖）。幸存者偏差同 D14。
- 每日 **08:17** 调度（HSICS 分类 08:15 之后）。

**B3 废除与迁移（关键）**

- `industry.constituent`（7238 行）+ `industry.constituent_change`（7238 行）：**DROP**（用户明确"历史不保留"）；
- `get_sector_constituent.py`：废除，`market_scheduler.py:199` 任务脚本名改 `get_stock_sw_industry_mapping.py`；
- `sector_stage2_agg.py:64-68` `load_constituent_map()`：改读 `regime.stock_sw_industry_mapping`，取 `sw_code_l1 AS sector_code / weight_l1 AS weight`；`pit_join()` 退化为简单 join（按 stock_code 贴最新行业，删 eff 区间判断）——**仍逐日算历史，但成分用最新**；
- `regime_schema.py` / `sql/schema.sql`：删旧表 DDL/COMMENT/索引、加两张新表 DDL；
- `monitor_collector.py`、`tests/test_sector_stage2.py`、`tools/probe_*.py`、4 份文档：全部同步新表名；
- 所有受影响指标 remark 标注 `pit_capable=false` + 幸存者偏差。

**阶段 B 验收**：A 股 ≥5000 只、港股 ≥2500 只有三级映射；抽查 SH.688981=850816 / weight_l1/l2/l3 三层不同、HK.00700=702015；`industry.constituent` 旧名零残留；行业特征 `sector_layer.py --verify` 通过（历史序列仍产出）。

---

### 阶段 C：个股画像行业标签重构（一级/二级并存）

**C1 新增统一映射加载函数**

- `profiling/quantile.py` 新增 `load_industry_map(conn, market, level)`：按市场返回 `stock_code → industry_code`，`level=1/2/3` 必填。
  - A 股：`regime.stock_sw_industry_mapping`（存 sw_code_l3）JOIN `regime.sw_industry_classification`（parent_code 链）——level=1 取三级→二级→一级，level=2 取三级→二级，level=3 直接取 sw_code_l3；
  - 港股：`regime.stock_hsics_industry_mapping`（存 hsics_code_l3）JOIN `regime.hsics_industry_classification`（sector_code/industry_code 列）——level=1/2/3 同理。
  - ⚠ 层级关系只在分类表一处维护（用户原则），映射表只存三级原子归属，一/二级靠 join 推导。
- 现有 `load_gics_map` / `load_gics_group_map` 随 GICS 标签废弃而移除（D2）。

**C2 标签重构范围（17 个标签 × 2 层级，完整 code/name 见 §6）**

⚠ 分组数据源随 D12 变更：画像的行业归属改读阶段 B 的统一映射表
`regime.stock_sw_industry_mapping`（A股）/ `stock_hsics_industry_mapping`（港股），
一/二级归属从分类表 parent 链 join，**不再读 GICS 的 `sector_hierarchy`/`sector_hierarchy_l2`**。

| 域 | 原标签 code | 原中文名 | 拆分后（新 code / 新中文名见 §6） | 分组 |
|---|---|---|---|---|
| ③估值 | `val_pe_ind_pct` | PE行业分位 | `val_pe_ind1_pct` + `val_pe_ind2_pct`（复用改口径） | 市场+一级 / 市场+二级 |
| ③估值 | `val_pb_ind_pct` | PB行业分位 | `val_pb_ind1_pct` + `val_pb_ind2_pct` | 同上 |
| ③估值 | `val_ps_ttm_tier` | PS(TTM)档 | `val_ps_ttm_ind1_tier` + `val_ps_ttm_ind2_tier` | 同上 |
| ③估值 | `val_pcf_ttm_tier` | PCF(TTM)档 | `val_pcf_ttm_ind1_tier` + `val_pcf_ttm_ind2_tier` | 同上 |
| ③估值 | `val_peg_tier` | PEG(TTM)档 | `val_peg_ind1_tier` + `val_peg_ind2_tier` | 同上 |
| ④质量 | `qal_roe_tier` | ROE档 | `qal_roe_ind1_tier` + `qal_roe_ind2_tier` | 市场+行业+报告期 |
| ④质量 | `qal_gross_margin_tier` | 毛利率档 | `qal_gross_margin_ind1_tier` + `qal_gross_margin_ind2_tier` | 同上 |
| ④质量 | `qal_net_margin_tier` | 净利率档 | `qal_net_margin_ind1_tier` + `qal_net_margin_ind2_tier` | 同上 |
| ④质量 | `qal_leverage_tier` | 杠杆水平档 | `qal_leverage_ind1_tier` + `qal_leverage_ind2_tier` | 同上 |
| ④质量 | `qal_roe_stability` | ROE稳定性 | `qal_roe_stability_ind1` + `qal_roe_stability_ind2` | 同上 |
| ④质量 | `qal_earnings_quality` | 利润现金转换率 | `qal_earnings_quality_ind1` + `qal_earnings_quality_ind2` | 同上 |
| ⑤成长 | `grw_revenue_yoy_tier` | 营收增速档 | `grw_revenue_yoy_ind1_tier` + `grw_revenue_yoy_ind2_tier` | 市场+行业+报告期 |
| ⑤成长 | `grw_profit_yoy_tier` | 净利增速档 | `grw_profit_yoy_ind1_tier` + `grw_profit_yoy_ind2_tier` | 同上 |
| ②规模 | `scl_revenue_tier` | 营收规模档 | `scl_revenue_ind1_tier` + `scl_revenue_ind2_tier` | 市场+行业 |
| ②规模 | `scl_industry_mktcap_rank` | 行业内市值排名 | `scl_mktcap_ind1_rank` + `scl_mktcap_ind2_rank` | 市场+行业 |
| ⑥回报 | `shr_payout_tier` | 分红率档 | `shr_payout_ind1_tier` + `shr_payout_ind2_tier` | 市场+行业 |
| ①属性 | `idt_gics_sector` | GICS一级部门 | 废弃 → `idt_ind1_sector`（一级行业）+ `idt_ind2_sector`（二级行业） | — |

（另：`val_pe_ind2_pct` 现为"PE二级行业分位"，是上一轮 GICS 口径的临时标签，本期复用 code 改为新口径，原值作废重算。）

- 全市场档标签（`val_pe_ttm_tier`/`val_pb_tier` 等）与时间分位 `val_pe_hist_pct` **不拆分、不改**。
- GICS 标签（含 `val_pe_ind_pct`/`val_pe_ind2_pct` 旧口径/`idt_gics_sector`/`idt_industry` 等）全部废弃并从注册表移除。

**C3 ⚠ L2/L3 暂停（D3）**

- `dim_valuation_conclusion` / `dim_quality_conclusion` / `dim_growth_conclusion` / `dim_position_state` 及 `biz_*` 依赖被废弃的 GICS 标签，**本期暂停产出**（`is_active=False` 或计算入口跳过），待行业改造完成后重新设计输入与切点。
- `profiling/conclusions/rules.py` 的 `RULES` 保留代码但暂不参与计算；`doc/stock_profile_l2_l3.md` 标注"阶段改造中暂停"。

**C4 实施约束**

- 所有标签分组键统一走 `load_industry_map`（单一真相源），禁止各模块各自 join。
- 组内有效样本 <15 只时：`confidence` 降级 + 记录 `n_sample`。
- `val_pe_ind2_pct` 的 2026-11-19 旧值（GICS 口径）作废重算。
- **D15 连续百分位 —— 已撤销（2026-11-23）**：曾计划给档位型标签加 `num_value` 连续百分位，评估实施路径后发现：① `profile.tag_value` 无 `extra` 列、`conclusion_detail` 是 L2/L3 专用（L1 恒 NULL，schema.py:100），存 L1 百分位会破坏契约；② 加独立列要改 `engine.py` 核心写入链路的 INSERT/UPDATE 与 `_same_value` 版本判断，pct 每日微变会触发大量新版本行；③ 档位型标签的 `num_value` 现为**原值语义**（PS 倍数/ROE%/市值元等），前端与消费方依赖该量纲。**最终决策：num_value 保持原值语义不变，不新增百分位存储。** 阶段 C 不再引入任何新工具函数。
- **D16 `load_financial_context` 改法（甲）**：一次加载 l1/l2 两套行业 map，产出 `grp_l1`/`grp_l2` 两列（缓存键不变，一次加载两用，避免重复加载财务大表）；quality/growth 8 个标签各算两遍用不同 grp 列。
- **D17 L2/L3 暂停方式**：在 `compute_profile.py` 计算入口跳过 L2/L3（`L2_L3_PAUSED=True`，保留 `RULES` 代码与 `is_active` 契约不动）。
- **D20 L2 估值结论整改（2026-11-23，已实施）**：行业 PE 分位从废弃的 `val_pe_ind_pct` 改为 **`val_pe_ind2_pct`（二级行业分位）**；二级组加分层样本门禁（正 PE ≤5 只不产出、6–14 只 conf=0.5、≥15 只 conf=1.0）；规则表加 `hist_tier`/`ind_tier` 档位派生字段，11 条规则条件从范围比较改为枚举比较（条数不变，可读性提升）；版本 v3→v4。L3 继续暂停（D17），`VAL_CLASS` 不动。
- **D21 L2 质量结论整改（2026-11-23，待实施）**：① 质量指标行业分档**用一级**（`qal_*_ind1_*`）——质量的行业差异主要在一级层面（银行/钢铁/白酒 ROE 结构性差异），二级多丢 60-70 只且差异小；② 杠杆从一票否决（QAL-03 priority 2）改为**降级因素**，不覆盖高质量的正面码；③ `weak` 兜底过宽（约 73%）→ 新增 `moderate` 档（ROE 3-4 档且无负面信号），`weak` 只留 ROE≤2 或有明显短板；④ 增加 ROE 绝对底线：inputs 加 `roe_raw`（`qal_roe_ind1_tier.num_value`），ROE<5%（可配阈值）最高只能到 `moderate`，不能 `solid`。L3 继续暂停（D17），`QAL_CLASS` 不动。

- **D19 GICS 遗留清理（2026-11-23）**：D18 斩断英文名依赖后，`build_sector_hierarchy.py` 已无任何消费方 → git rm 删除；`sector_hierarchy`（196 行）/ `sector_hierarchy_l2`（113 行）两表 DROP；`market_scheduler.py` 周二 18:35「行业层级映射」任务移除；`sql/schema.sql` 两表 DDL/COMMENT 删除、探针/测试/可行性文档同步。至此 GICS 代码残留清零（SCHEMA_FIX 历史归档文件除外）。
- **D18 富途英文名来源（2026-11-23）**：`get_hsics_industry_classification.py` 不再正则解析 `build_sector_hierarchy._SECTOR_MAP` 的行尾注释取富途英文名；改为从**富途 API 英文版** `get_plate_list(Market.HK, Plate.INDUSTRY)` 实测拉取 113 个板块标准英文名，固化为本文件常量 `FUTU_EN_NAME`。至此 HSICS 采集脚本对 GICS 遗留脚本零依赖；富途新增板块时手工往 MAP 补一行。实跑：auto 匹配从 46 升至 50（confirmed 67 行保护后落库仍 46 auto + 67 confirmed）。

**阶段 C 验收**：逐票对比 HK.00700 / SH.688981；分组键无 GICS 残留；一/二级两值成对产出；num_value 保持原值语义（不变）；L2/L3 确认无新产出；`profiling/tags/__init__.py` 注册数量更新；全量测试通过。

---

### 阶段 D：行业特征计算重构（一级/二级并存）

**D0 层级已定（D1）**：一级、二级**都算**，每个指标成对产出 `_L1`/`_L2`（见 §3.2）。

- CN：`sector_layer.py` 现有 41 个指标，每个拆成 `_L1`（申万一级 31 行业）+ `_L2`（申万二级 131 行业）两套；`sector_daily` / `sw_industry_valuation_snapshot`（原 `sector_valuation_snapshot`，D10 改名）的 `sector_code` 取值域扩到含二级。
- HK：`sector_layer_hk.py` 现有 25 个指标，每个拆成 `_L1`（HSICS 一级 12 行业）+ `_L2`（HSICS 二级 31 行业）两套；原三级 113 行业的自算指数（`industry.hk_index_daily` 69 万行）不再作为主输出，可保留作三级参考或下线（**实施时定**）。
- 表结构按 D7 X 方案：不加 `classification` 列，靠 `sector_code` + `indicator_code` 后缀区分。
- 字典 `params_hash` 变化 → 触发全量重算；`CALIB_VERSION` 需 bump（CN s2.1 / HK hk1.0）。
- **字典表加 `industry_level`**（D8）：`regime_schema.py` 增列 + 幂等 ALTER + COMMENT；`sync_dict` 写入 `1`/`2`；`sql/schema.sql` 同步。
- ⚠ `min_periods=250`、横截面分母 `rank_n`、闸门阈值随行业数变化需重新标定（`doc/sector_validation_report.md` 已有先例）；一级 12 行业的 `n_valid<10` 门禁需特别检查。

**阶段 D 验收**：每个指标都有 `_L1`/`_L2` 两版且行业数正确（CN 31/131，HK 12/31）；字典行 `industry_level` 填 1/2 正确、可按层级过滤；前瞻验证脚本（`sector_layer` 的 `--verify`）通过；监控登记更新（`monitor_collector` 表配置）。

---

### 阶段 E：切换与验收

1. **DB schema 同步**：所有新表写入 `sql/schema.sql`（AGENTS.md 强制）；**估值表改名迁移**（D10）：`ALTER TABLE regime.sector_valuation_snapshot RENAME TO sw_industry_valuation_snapshot`（含索引同步改名），全量 20+ 引用点（代码/测试/工具/监控/文档）一次性替换；跑 `_code_schema_diff.py` + `_schema_diff.py` 确认无漏写。
2. **全量重算顺序**：行业信息 → 个股映射 → 画像标签（**跳过 L2/L3**，见 D3）→ 行业特征。
3. **回归验证**：`git worktree` 取旧代码，对同一 as_of 跑新旧对比，确认"非行业标签逐值一致、行业标签按预期变化"。
4. **测试**：`.venv/bin/python3 -m pytest tests/ -v` 全绿；新增测试覆盖映射完整性、标签分组键、层级一致性。
5. **部署**：按 `deploy.sh` 正常流程；新 systemd unit（A2）需 `systemctl enable --now`。

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 申万三级 336 行业 × 6500 行 ≈ 220 万行，内存雪崩（历史事故） | 整机冻结 | 独立 unit + 六项资源参数 + 分批 + `/usr/bin/time -v` 实测（阶段 A2） |
| AKShare 接口变更（`sw_index_third_cons` 已坏一次） | 采集中断 | 只用 `index_component_sw`；加接口健康检查与 dry-run |
| HSICS 官方码与富途 code 映射 67 个人工核对项（已确认） | 分类错误 | 逐条 confirmed + 双人复核 + 测试钉住 HK.00700 等关键样本 |
| L2/L3 依赖被废弃的 GICS 标签（D3） | L2/L3 输入缺失会报错 | 本期暂停 L2/L3 产出（is_active=False / 跳过），待行业改造完成后重设输入与切点 |
| A/H 混排 | 结论错误（C2） | 画像分组键含 market；行业层物理分表；测试静态断言 |
| 幸存者偏差 | 历史分位虚高 | 港股标 `pit_capable=False`；A 股利用 `join_date` 做 PIT，逐步完善 |

---

## 6. 标签 code / name 清单（已确认，2026-11-20）

一级/二级并存，code 含 `ind1`/`ind2`、name 含"一级行业/二级行业"。**以下清单已获用户逐项确认**：

| 域 | 原 code / name | 新一级 code / name | 新二级 code / name |
|---|---|---|---|
| 估值 | `val_pe_ind_pct` PE行业分位 | `val_pe_ind1_pct` / PE一级行业分位 | `val_pe_ind2_pct` / PE二级行业分位（复用改口径） |
| 估值 | `val_pb_ind_pct` PB行业分位 | `val_pb_ind1_pct` / PB一级行业分位 | `val_pb_ind2_pct` / PB二级行业分位 |
| 估值 | `val_ps_ttm_tier` PS(TTM)档 | `val_ps_ttm_ind1_tier` / PS(TTM)一级行业档 | `val_ps_ttm_ind2_tier` / PS(TTM)二级行业档 |
| 估值 | `val_pcf_ttm_tier` PCF(TTM)档 | `val_pcf_ttm_ind1_tier` / PCF(TTM)一级行业档 | `val_pcf_ttm_ind2_tier` / PCF(TTM)二级行业档 |
| 估值 | `val_peg_tier` PEG(TTM)档 | `val_peg_ind1_tier` / PEG(TTM)一级行业档 | `val_peg_ind2_tier` / PEG(TTM)二级行业档 |
| 质量 | `qal_roe_tier` ROE档 | `qal_roe_ind1_tier` / ROE一级行业档 | `qal_roe_ind2_tier` / ROE二级行业档 |
| 质量 | `qal_gross_margin_tier` 毛利率档 | `qal_gross_margin_ind1_tier` / 毛利率一级行业档 | `qal_gross_margin_ind2_tier` / 毛利率二级行业档 |
| 质量 | `qal_net_margin_tier` 净利率档 | `qal_net_margin_ind1_tier` / 净利率一级行业档 | `qal_net_margin_ind2_tier` / 净利率二级行业档 |
| 质量 | `qal_leverage_tier` 杠杆水平档 | `qal_leverage_ind1_tier` / 资产负债率一级行业档 | `qal_leverage_ind2_tier` / 资产负债率二级行业档 |
| 质量 | `qal_roe_stability` ROE稳定性 | `qal_roe_stability_ind1` / ROE波动程度一级行业档 | `qal_roe_stability_ind2` / ROE波动程度二级行业档 |
| 质量 | `qal_earnings_quality` 利润现金转换率 | `qal_earnings_quality_ind1` / 利润现金转换率一级行业档 | `qal_earnings_quality_ind2` / 利润现金转换率二级行业档 |
| 成长 | `grw_revenue_yoy_tier` 营收增速档 | `grw_revenue_yoy_ind1_tier` / 营收增速一级行业档 | `grw_revenue_yoy_ind2_tier` / 营收增速二级行业档 |
| 成长 | `grw_profit_yoy_tier` 净利增速档 | `grw_profit_yoy_ind1_tier` / 净利增速一级行业档 | `grw_profit_yoy_ind2_tier` / 净利增速二级行业档 |
| 规模 | `scl_revenue_tier` 营收规模档 | `scl_revenue_ind1_tier` / 营收一级行业规模档 | `scl_revenue_ind2_tier` / 营收二级行业规模档 |
| 规模 | `scl_industry_mktcap_rank` 行业内市值排名 | `scl_mktcap_ind1_rank` / 一级行业内市值排名 | `scl_mktcap_ind2_rank` / 二级行业内市值排名 |
| 回报 | `shr_payout_tier` 分红率档 | `shr_payout_ind1_tier` / 分红率一级行业档 | `shr_payout_ind2_tier` / 分红率二级行业档 |
| 属性 | `idt_gics_sector` GICS一级部门 | 废弃 → `idt_ind1_sector` / 一级行业 | 新增 `idt_ind2_sector` / 二级行业 |

全市场档（`val_pe_ttm_tier`/`val_pb_tier`）与时间分位 `val_pe_hist_pct` 不拆分、不改。

---

## 7. 工作量与排期（粗估）

| 阶段 | 主要工作 | 依赖 |
|---|---|---|
| A 行业信息采集 | 3 张层级表 + 336 行业指数历史回填（独立 unit）+ HSICS 官方码对照 | 决策已定；1~2 周 |
| B 个股映射采集 | 2 张映射表 + 336 次 API 调用 + 67 项人工核对（已完成） | A 完成；3~5 天 |
| C 画像标签重构 | 17 标签 × 2 层级 code/name 更换 + 映射函数 + L2/L3 暂停 + 测试 | B 完成；1~2 周 |
| D 行业特征重构 | CN/HK 指标 × 2 层级（_L1/_L2）+ 重新标定 | A/B 完成；1~2 周 |
| E 切换验收 | schema 同步 + 全量重算（跳过 L2/L3）+ 回归 + 部署 | C/D 完成；3~5 天 |

**关键路径**：A → B → C/D 可并行 → E。

---

## 8. 开工前检查清单（全部完成）

- [x] D1 行业特征层级确认（一级/二级并存）
- [x] D2 GICS 标签存废确认（废弃、原地替换）
- [x] D3 L2/L3 处理确认（本期暂停产出）
- [x] D4 港股 code 体系确认（官方码，两跳方案）
- [x] §6 中文标签名逐项确认
- [x] §3.2 行业特征编码/名称方案 + D7 表结构 X 方案确认
- [x] D8 `industry.indicator_dict.industry_level` 字段（1/2/3）确认
- [x] D9 脚本改名（4 个 get_* 命名族）确认
- [x] D10 全量回填拆分（新建 `backfill_sw_industry_classification.py`）+ 表名同步改名确认
- [x] D11 层级表去 `info` 改名 + 富途码映射拆表确认
- [x] D12~D14 阶段 B 统一收口方案（一张表/无历史版本/行业指标放弃 PIT）确认
- [x] D15 连续百分位 —— 已撤销（num_value 保持原值语义）
- [ ] 计算任务 unit 模板准备（阶段 A2，开工时执行）

---

## 9. 命名与职责规范（本次迭代的经验教训，强制遵守）

本次行业改造的反复返工，根源是**脚本名、数据库表名、表存储内容三者没有做到规范/一致/明确**，且违反了单一职责。总结为以下强制规范，后续所有模块（不限于本次改造）必须遵守：

### 9.1 三条铁律

1. **名实匹配**：脚本名、表名、字段名必须**准确描述其存储内容或行为**，禁止 `info`/`data`/`temp` 等泛泛之词。
   - 反例（本次踩过）：`sw_industry_info` / `hsics_industry_info` 存的是**分类层级**，却叫 `info`；`backfill_sw_industry.py` 实际同时做全量回填+日常增量。
   - 正例：`sw_industry_classification`（分类层级）、`get_sw_industry_quotes.py`（行情采集）、`backfill_sw_industry_classification.py`（全量回填）。
2. **三名一致**：同一事物的**脚本名 ↔ 表名 ↔ 标签/指标 code 前缀**必须指向同一口径，可互相推导。
   - 正例：`get_sw_industry_classification.py` ↔ `regime.sw_industry_classification`；`get_sw_industry_valuation.py` ↔ `regime.sw_industry_valuation_snapshot`。
   - 反例（本次踩过）：脚本 `get_sector_valuation.py` 写 `sector_valuation_snapshot`，"sector" 口径模糊（是申万还是恒生？），本次一并改成 `sw_*`。
3. **单一职责**：一张表/一个脚本只做一件事；不同类型主数据不得混存。
   - 反例（本次踩过）：HSICS 分类层级与富途码映射想塞同一张表；层级表（低频）与指数日频采集想塞同一脚本。
   - 正例：分类层级 `hsics_industry_classification` / 码映射 `futu_hsics_code_mapping` 拆开；全量回填 `backfill_*` / 日常采集 `get_*` 拆开。

### 9.2 命名约定（本次确立，作为后续模板）

| 对象 | 约定 | 示例 |
|---|---|---|
| 采集脚本（日常） | `get_<市场><域><内容>.py` | `get_sw_industry_quotes.py`、`get_hsics_industry_classification.py` |
| 回填脚本（一次性） | `backfill_<市场><域><内容>.py` | `backfill_sw_industry_classification.py` |
| 分类层级表 | `<市场>_industry_classification` | `sw_industry_classification`、`hsics_industry_classification` |
| 码映射表 | `<源>_<目标>_code_mapping` | `futu_hsics_code_mapping` |
| 快照表 | `<口径>_<内容>_snapshot` | `sw_industry_valuation_snapshot` |
| 画像标签 code | 含层级/口径标识 | `val_pe_ind1_pct` / `val_pe_ind2_pct`、`_L1`/`_L2` |
| 行业指标 code | 域.指标_层级 | `SEC.VAL.PE_TTM_L1` / `_L2` |

### 9.3 变更纪律

- 改脚本名/表名/字段名时，必须**一次性同步全部引用点**（调度器字符串、测试、工具、文档、COMMENT），改完用 `grep <旧名>` 确认零残留。
- 表名改动用 `ALTER TABLE ... RENAME TO` 保留数据，不重建；同步更新 `sql/schema.sql` 并跑 `_schema_diff.py` / `_code_schema_diff.py`。
- 新增表/脚本前，先回答三个问题：**它只存/做一件事吗？名字能准确说出来吗？与相关对象口径一致吗？** 任一答不上来就先不要建。
