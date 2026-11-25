# 个股画像财务报告期口径重构设计（年报 / 半年报 / 季报分维度）

| 项目 | 内容 |
| --- | --- |
| 文档版本 | **v1.0**（2026-11-24） |
| 文档性质 | **设计 / 口径规格**。不改代码、不改库、不执行任何变更 |
| 上位文档 | `doc/stock_profile_l2_l3.md`（数据契约）、`doc/industry_classification_refactor_plan.md`（阶段 C 分组）、`AGENTS.md`（数据库结构 / 计算资源 / 命名三条强制约定）、`doc/compute_task_resource_standard.md` |
| 适用范围 | ④ 盈利质量 / ⑤ 成长特征 L1 原子标签（`qal_*` / `grw_*`）、`dim_quality_conclusion` / `dim_growth_conclusion`、两个 L3、`conclusion_detail` 数据契约 |
| 不在范围 | ③ 估值（TTM 链路）、⑩ 位置状态、采集端 `get_financial.py`、消费端 `stock-realtime`、**TTM 体系**、**任何上屏中文文案** |
| 真源优先级 | 本文档为「报告期口径」的规格真源；与 `stock_profile_l2_l3.md` 冲突时，以本文档 §4 决策为准，落地后回写该文档 |

> **上屏文案硬约束**：本文档不定义、不改写任何将上屏的中文字符串（标签名、维度名、脚注、tooltip、空态文案）。若后续需要在前端展示「年报 / 半年报 / 季报」维度、期次或降级原因，**必须先把落地字符串逐项原样列出并经确认**，才允许改文件。本文档只定义机器字段（`report_basis` 等 code）与计算口径。

---

## 0 结论摘要（决策一览）

用户已裁决五条：① 季报维度单季化；② 只有低频数据的公司降级到低频维度；③ 暂不引入 TTM；④ **现金转化率必须支持单季计算**（目的是凸显最新突变、支撑归因，见 D5）；⑤ **ROE 用 `financial_indicator.roe` 对应报告期原值**，不年化、不单季化（见 D4）。在此基础上，本文档把剩余决策项**逐项给出结论**。

| # | 决策项 | 结论 |
| --- | --- | --- |
| D1 | 维度的表达方式 | 三个维度是**对比分层（report_basis）**，不是每票三套结论；每票只取一个「满足可比条件的最高维」 |
| D2 | 单季化算法 | 复用 `v_financial_quarterly` 的财年内累减法；新增独立视图 `v_financial_analysis_period` 承载 basis，不改原视图语义 |
| D3 | 同比口径 | 单季同比（同季 vs 去年同季）/ 半年同比（H1 vs 去年 H1）/ 年度同比（FY vs 去年 FY）；基期 ≤0 → 增速无定义 |
| D4 | ROE | **用对应报告期原值**（H1 半年累计 / Q3 前三季累计 / annual 全年），不年化；分档按 `report_type` 细分，不跨期长混排 |
| D5 | 现金转化率 | **支持单季计算**：单季 OCF ÷ 单季净利，凸显最新突变（腾讯 Q3 净现比 4.2%）；不可得时降级为累计口径并标注 |
| D6 | 波动 / 持续性 / 加速度 | ROE 稳定性保持同类型报告期原值序列；持续性年报序列；加速度改为同 basis 同比之差 |
| D7 | 横截面分档 | basis 固定不混；组内样本 <20 时**行业层级回退**到「市场 × basis」，<5 不发布 |
| D8 | 降级阶梯 | `quarterly → semi_annual → annual → 不发布`，以「是否存在可比期」为准；加陈旧门禁 |
| D9 | PIT | 现有披露日护栏不动；分解用到的前序期、去年同季同样叠加护栏；`pit_capable` 保持 `False` |
| D10 | 结论切点 | **全部重标定**（单季同比与累计同比中位差 8pp）；绝对阈值按 basis 复核 |
| D11 | 数据契约 | `conclusion_detail` 增 `report_basis` / `period_months` / `peer_level` / `basis_reason`；JSONB 无需加列 |
| D12 | 版本与迁移 | 相关标签与 `RULES` 全部 bump、全量重建、补防回退测试、视图 DDL 同步 `sql/schema.sql` |

原 2 项待裁决事项已于 2026-11-25 全部裁定，记录见 §11。

---

## 1 问题定义：为什么必须改

### 1.1 观测事实（腾讯，本机库 2026-11-24 实测）

| 来源 | 2026-09-30 收入 |
| --- | --- |
| 腾讯官方业绩公告（单季） | **2073.36 亿** |
| 东财 `stock_financial_hk_analysis_indicator_em` → `financial_indicator.revenue` | **6085.79 亿**（1–9 月累计） |
| `v_financial_quarterly` 单季视图 | **2073.36 亿** ✅ |

`6085.79 − 4012.43(H1) = 2073.36`。数据没丢，但**落地表存的是披露格式（累计），不是分析口径（单季）**。

### 1.2 现状链路

```
get_financial.py（东财/新浪 → 原样落库累计值）
  → financial_indicator（report_type: Q1/H1/Q3/annual，全为累计或比率）
  → load_financial_latest(..., 'auto')：每票取「最近一期」标准报告期
  → load_financial_context：grp = 市场|行业|report_type（四组分开）
  → quality.py / growth.py：用源字段 revenue_yoy / roe / OCF 分档
  → RULES → dim_quality / dim_growth → L3
```

### 1.3 四个具体缺陷

| 缺陷 | 说明 | 后果 |
| --- | --- | --- |
| **A. 披露格式当分析口径** | 三季报的 `revenue_yoy = +9.18%` 是「1–9 月 vs 去年 1–9 月」的累计同比，不是 Q3 单季同比 `+7.50%` | 与公司公告、券商研报口径不一致；无法回答「这个季度怎么样」 |
| **B. 「季报」被拆成四类且量纲不齐** | 现在按 `Q1/H1/Q3/annual` 四组分开：Q1 是 3 个月，Q3 是 9 个月累计，annual 是 12 个月。Q1 与 Q3 都叫「季报」却不能同组，Q3 与 annual 又因「最近一期」混杂 | 用户提出的「三个维度、同维度对比」在现有分组里并不成立 |
| **C. ROE 原值跨期长不可比 + 绝对阈值共用** | `roe` 是报告期加权 ROE，Q1 约 5%、Q3 约 15%、年报约 21%；`RULES` 里 `roe_floor_pct=5` 拿这三者直接与同一个 5% 比较 | 同一阈值在 Q1 票上是「极优秀」、在年报票上是「平庸」，绝对底线跨期次失效（腾讯四期原值见 §6.4，故按 `report_type` 分设阈值，D4/D10） |
| **D. 加速度的二阶导数不成立** | `grw_growth_accel` = 最新一期 `revenue_yoy` − 上一期同类型 `revenue_yoy`，实际是「9 个月累计同比 − 6 个月累计同比」，分母期长不同 | 度量的是累计口径切换，不是增速拐点 |

### 1.4 本次目标

按用户思路落地：**年报 / 半年报 / 季报三个维度，公司只算自己有的维度，同维度对比**；季报维度单季化；低频公司降级；不引入 TTM。同时修复 A–D 四个缺陷，且不破坏 `stock_profile_l2_l3.md` 已冻结的「描述不预测 / 出档门禁 / 事件行 / 版本即口径」契约。

---

## 2 金融行业成熟方法（先立标准，再谈设计）

### 2.1 披露制度层：三个市场的规则不同

| 市场 | 强制披露 | 期间口径 | 依据 |
| --- | --- | --- | --- |
| A 股 | 一季报、半年报、三季报、年报 | **累计（YTD）**：3 / 6 / 9 / 12 个月 | 证监会《公开发行证券的公司信息披露内容与格式准则》第 2、3 号 |
| 港股主板 | 半年报、年报 | 半年累计 / 全年；**季报为自愿披露**（腾讯属持续披露的少数） | HKEX 主板上市规则附录十六 |
| 美股 | 10-Q ×3、10-K | 10-Q 同时给 three months 与 nine months 两组 | SEC Regulation S-X |

**关键推论**：港股大量公司**根本没有季报**，所以「单季化」对它们不是算法选择问题，而是数据可得性问题 —— 这正是用户「降级到低频维度」的现实依据。

### 2.2 数据商层：三套字段并存，绝不二选一

Wind / Choice / Bloomberg / Compustat / Refinitiv 的标准做法：

| 字段族 | 含义 | 主要用途 |
| --- | --- | --- |
| As-reported / YTD | 报告期累计原值（法律披露口径） | 年度进度，唯一真相源 |
| Single period / discrete quarter | 单季流量（财年内累减还原） | 增长趋势、拐点 |
| TTM / LTM | 滚动 12 个月 | 估值、比率平滑 |

配套三件事：**PIT（point-in-time，按首次披露口径 + filing date 取数）**、**restatement 追踪**、**fiscal period 标签**（`period_end` + `fiscal_year` + `period_type` + `filing_date`）。

> 本项目取舍：保留 YTD 为唯一真相源 + 新增单季还原；**本期不做 TTM 新链路**（③ 估值已有的 `load_financial_ttm` 保持原样，不复用、不扩张）。

### 2.3 分析层：四类指标必须区别处理（本次设计的核心）

金融分析里「单季化」从来不是对所有字段一刀切，而是按分子分母性质分：

| 指标类 | 例子 | 成熟做法 | 原因 |
| --- | --- | --- | --- |
| **流量** | 营收、净利、OCF | 可累减成单季；同比用**同季 vs 去年同季** | 期长一致 + 季节位置一致 |
| **流量 ÷ 流量** | 毛利率、净利率、OCF/净利 | 分子分母**同期同长**，期长自动抵消；为避免季节性噪声，行业偏好 TTM 或全年滚动 | 单季净现比「容易受备货、预收、季节性结算扰动」，全年滚动「最常用、最稳定」 |
| **流量 ÷ 存量** | ROE、ROIC、周转率 | **行业通用做法**是年化（`YTD ROE × 12 / 月数`、单季 `×4`）或 TTM ROE，**本项目裁决例外**：用报告期原值 + 按 `report_type` 分组（D4） | 年化只在跨期长比较时必需；本项目只做组内横截面分档，原值在组内可比，且年化假设对季节性行业失真 |
| **存量** | 资产负债率、净资产 | 直接取报告期末值，不拆不年化 | 与期间长度无关 |

**行业实证（作为对照，非本项目采用）**：券商三季报研报同时列示「单 Q3 年化加权 ROE 9.6%」与「前三季度年化加权 ROE 13.6%」—— 两者都年化到 12 个月基准后才可比较。本项目在 §4 D4 明确不采用此做法，改用「原值 + 同 `report_type` 组内分档」，理由见 D4「行业方法对照」。

### 2.4 可比性三原则

1. **期长一致**：3 个月只和 3 个月比，6 个月只和 6 个月比，12 个月只和 12 个月比。
2. **季节位置一致**：用 YoY（同季 vs 去年同季）把季节性差分掉。**这带来一个重要简化** —— 一旦判定用的是同比，Q1 与 Q3 的单季同比已消除各自季节性，可在「季报维度」内直接横比，不必再按季度序号（Q1/Q2/Q3/Q4）二次切组，否则样本进一步碎裂。
3. **PIT 一致**：参与比较的本期、去年同季、以及用于累减还原的前序期，都必须满足「截至 as_of 已可获知」。

### 2.5 横截面分档的样本量纪律

等频五分位（本项目 `assign_tier`）要求组内有足够样本；GICS / 申万等分类体系的通行做法是**层级回退**：子行业样本不足 → 升到行业 → 升到板块 → 升到全市场，并把实际使用的层级随结果一起记录。本项目 `quantile.py:571-575` 已有「样本 <5 不打档」的弱护栏，但没有层级回退、没有最小稳定样本线。

### 2.6 本节来源

- 券商三季报点评中的「单季度年化加权 ROE」「累计年化加权 ROE」并列口径（中信证券 / 华泰证券 / 开源证券 2026 三季报研报）
- 净现比「单季度 vs 全年滚动」口径对比与适用性说明
- TTM 定义与用途（Stockopedia / Longbridge / Sharpely 数据方法说明）
- ROE 分子（期间流量）分母（时点存量）不同时钟的标准表述

---

## 3 决策依赖链（问题之间的关系）

问题不是并列的，前一项的结论会锁死后一项的可选空间：

```
D1 维度定义（basis 是分层，不是三套结论）
  ├─→ D2 单季化算法与边界 ──→ D3 同比口径 ──→ D6 加速度（依赖 D3 的同比序列）
  │                              └─→ D10 切点重标定（同比分布变了）
  ├─→ D8 降级阶梯（依赖 D2 能否拆出单季 + 是否存在可比期）
  ├─→ D7 分组与最小样本（basis 是分组键的一部分；basis 越细，样本越碎）
  └─→ D11 evidence 字段（basis 必须逐字段可追溯）
D4 ROE 用原值 ──→ D10 的 roe_floor_pct 必须按 report_type 分设（Q1/年报票上同一阈值含义不同）
D5 现金转化率 ──→ D7（比率类不进单季分组，但仍按 basis 分层）
D9 PIT ──→ 贯穿 D2/D3/D8（还原用到的前序期、去年同季同样要护栏）
D12 版本迁移 ──→ 由 D2~D11 的改动面决定 bump 范围
```

**三条依赖要点**：

1. **D2 决定 D8**：能否降级不是「看公司披露了什么报告」，而是「能不能算出可比的单季同比」。港股实测仅约 14.6% 的公司同时具备单季与去年同季（§6.2），所以降级规则必须是**数据驱动**的，不能是名单式的。
2. **D4 决定 D10**：ROE 不年化、用原值后，`roe_floor_pct=5` 在 Q1 / 年报票上含义完全不同，必须按 `report_type` 分设阈值并重标定（§11 已裁定 1）。
3. **D1 决定 D7**：如果把三个维度做成三套并行结论，分档样本会被切成三份且同一票出现三个冲突结论（违反 §4.5 单一真相源与 §6.0 判据③）；做成「一个结论 + basis 分层」才能既保留同维度对比，又维持单一结论。

---

## 4 逐项决策分析

### D1 维度如何定义与落地

**问题**：用户说「算画像也分年报、半年报、季报三个维度」，是三套结论还是一个结论分三组比？

**行业方法**：数据商不发布三套「成长结论」，只发布一个标准化后的值 + 期间标签；投资结论必须可比较、可检索、单一值。对低频公司，标注 stale / low-frequency，而不是另造一个指标。

**本项目约束**：`stock_profile_l2_l3.md` §4.5「同一构念在数据层只给一个结论」、§6.0 判据③、§4.1「event row + enum」；消费端按 `tag_code + key_value` 检索，多套结论无法表达。

**结论**：三个维度 = **对比分层 `report_basis`**。每票只取一个「满足可比条件的最高维」作为当期判定口径，产出一个 enum；较低频的数值算出来只放 `evidence` 供核对，不单独发布。分组键 `市场|行业|report_basis` 保证同维度对比。**这不改变用户「同维度对比」的实质**，只是把维度落实为分层而非并行结论。

**代价**：低频公司无法与高频公司直接横比（用户已接受）；需接受分 basis 报覆盖率。

### D2 单季化算法与边界

**问题**：单季数据怎么来，哪些字段能拆、哪些不能？

**行业方法**：同财年内累减 —— `Q1=Q1累计`、`Q2=H1−Q1`、`Q3=9M−H1`、`Q4=FY−9M`；仅在期次严格连续且前序有值时相减，否则保留 as-reported 并标注，不臆造。比率与存量字段不参与相减。

**本项目现状**：`sql/schema.sql:975-1043` 的 `v_financial_quarterly` 已完整实现，且已按 `fiscal_year_start` 分组修复了非自然年财年（阿里 4 月财年）跨年误减问题。**不重复造轮子**。

**结论**：
1. `v_financial_quarterly` 保持原样（职责单一：累减还原）。
2. 新增视图 **`v_financial_analysis_period`**（名实匹配：每行一个「分析期」观测；单一职责：产出 basis 口径），叠加在 `financial_indicator` + `v_financial_quarterly` 之上，输出：
   `stock_code, period_end, report_basis, period_months, quarter_index, fiscal_year_start, revenue, net_profit, operating_cash_flow`（累计与单季两列族并存）、`roe`（报告期原值，不年化）、各比率、`report_date` / `prev_comparable` 对齐键。
3. 累减结果为异常值（如营收 ≤ 0）时置 NULL 并记 `data_quality_flag`，不以负数进入分档。
4. 视图 DDL 必须同步 `sql/schema.sql`（AGENTS.md 最高优先级约束）；`deploy.sh` 的 `rsync --exclude '*.md'` 不影响 DDL 同步。

**代价**：新增一个视图对象，需走 `_dump_schema_from_db.py` / `_schema_diff.py` / `_code_schema_diff.py` 三件自检；查询须带时间下界 + 按股票分批（AGENTS.md R1/R7）。

### D3 同比口径

**问题**：三种维度各自怎么算同比？

**行业方法**：单季同比 = 本期单季 ÷ 去年同季单季 − 1；半年同比 = 本期 H1 ÷ 去年 H1 − 1；年度同比 = 本期 FY ÷ 去年 FY − 1。**绝不混用**。基期 ≤ 0（亏损/扭亏）时同比无定义，数据商标 NA 或 TURNAROUND，不填极端值。

**本项目实测**（§6.3）：A 股 Q3 单季同比与累计同比平均绝对差 **21.3pp**、中位 **8.0pp**、P90 **32.6pp**，**64.3%** 的票差 >5pp；6352 个最新单季中 354 个缺去年同季、4 个基期 ≤0、108 个超 ±200%。

**结论**：
1. 三个 basis 各自算同比，`quarterly` 只用单季流量比单季流量。
2. **基期 ≤ 0 或缺失 → `yoy = NULL`**，不进分档、不参与 `GRW-01` 负增长判定（避免扭亏票被算成 −99% 或 +3000%）。
3. 分档用 rank（现有 `assign_tier` 已对异常值免疫），`num_value` 保留原值供复核；规则中的 `rev_yoy < 0` 只能用有效同比。
4. A/H 口径差异：`financial_indicator.revenue_yoy` 只在 `semi_annual` / `annual` 维度可沿用源字段；`quarterly` 必须自算。

**代价**：`revenue_yoy` / `net_profit_yoy` 在单季维度不再是直接取列，取数层要重写；354 只票单季同比缺失 → 由 D8 降级。

### D4 ROE 用对应报告期原值

**用户裁决**：季报 ROE 直接用 `financial_indicator.roe` 的对应报告期原值 —— H1 显示半年累计 ROE、Q3 显示前三季累计 ROE、Q1 显示单季 ROE、annual 显示全年 ROE。**不做年化、不做单季还原**。

**数据层可行性**：✅ 完全支持。`roe` 列本身已是「报告期加权 ROE」，源端（东财 `ROE_AVG` / 新浪「净资产收益率_平均」）按报告期直接给出；无需净资产列、无需改采集端。这是用户裁决相对于「单季 ROE 需补净资产」的关键优势 —— **用原值零采集改动**。

**行业方法对照**：券商研报通常把 ROE 年化后再横比（§2.3）。本项目**主动不采用年化**，理由：① 画像结论是**描述性横截面分档**，不是跨期时序比较；只要分档组内报告期类型一致，原值即可比；② 避免引入年化假设（单季 ×4 对季节性行业严重失真）；③ 用户裁决要求「显示对应报告期原值」，与消费端展示口径一致。

**结论**：
1. `qal_roe_ind1/2_tier` 的 `num_value` = 对应报告期 `roe` 原值；`key_value` 档位在**同 `report_type` 组内**五等分（Q1 只和 Q1 比、H1 只和 H1 比、Q3 只和 Q3 比、annual 只和 annual 比），**绝不跨期长混排**。
2. `evidence.report_period` 标注 `report_type` / `report_date`，`roe_raw` 即原值；`roe_floor_pct` 按 `report_type` 分设阈值（见 D10 与 §11 已裁定 1）。
3. `tier_or_flag` 的 `LOSS` 标记仍按原始符号判定（负值不进分档）。
4. 报告期分组与 D1 的 `report_basis` 是**两个正交维度**：`quarterly` basis 下仍可能出现 Q1 / Q3 两种报告期，ROE 分档用更细的 `report_type` 键；增速与现金转化用 `report_basis` 键。

**代价**：不同 `report_type` 的 ROE 数值不可比（腾讯 Q1 5.09% vs Q3 15.03% vs 年报 21.13%），跨期长比较被禁止；`roe_floor_pct` 绝对值不能全期次共用。

### D5 现金转化率（OCF ÷ 净利）支持单季计算

**用户裁决**：现金转化率**必须支持单季计算**，目的是凸显最新期的突变、支撑归因 —— 例：腾讯 2026Q3 单季 OCF 仅 26.52 亿、单季净利 636.12 亿，单季净现比 **4.17%**，而累计口径仍显示 88.18%，无法凸显现金流骤降，系统因此「迟钝」。

**数据层可行性**：✅ 完全支持。`v_financial_quarterly` 已直接提供单季 `operating_cash_flow` 与单季 `net_profit`（腾讯 2026Q3 = 26.52 / 636.12 亿）；无需采集端改动、无需净资产列。

**行业方法对照**：TTM / 全年滚动净现比更稳定（§2.3），但**稳定性与及时性是一对权衡**：TTM 会滞后 1–3 个季度才反映突变。用户明确选择及时性优先（归因需要），本设计接受这一取舍。

**结论**：
1. `qal_earnings_quality_ind1/2` 改用**单季 OCF ÷ 单季净利**，分档键 = `市场|行业|report_basis`（同 basis 内）；`num_value` 存单季比率原值。
2. 单季流量不可得时（港股最近一期仅 323/2830 ≈ 11.4% 可拆单季，即约 88.6% 不可得，§6.2）→ 按 D8 降级为**最近可得累计口径**，并在 `evidence` 标注 `cash_basis = 'accumulated'` + `basis_reason = 'no_single_quarter_flow'`；**降级票与单季票不混排分档**（前者落 `semi_annual` / `annual` basis）。
3. 单季净利 ≤ 0 → 打 `LOSS`，不进分档（与现有 `tier_or_flag` 一致）。
4. 单季 OCF 为负、净利为正 → 比率取负值，正常进第 1 档（现金转化最差），这是**期望的信号**，不做裁剪。
5. `evidence` 同时写入 `cash_conversion_single_q`（单季值）与 `cash_conversion_accumulated`（累计值）供归因对照 —— 腾讯 Q3 的两个值（4.17% / 88.18%）并列，直接支持「为何股价下挫」的归因。

**代价（已知且接受）**：单季净现比噪声显著高于累计（腾讯 Q1 174.5% → Q3 4.2%；A 股单季 P10–P90 = −3.12~8.29，累计 = −3.08~5.21，约 25% 单季 OCF 为负）。档位翻转率可能上升，§10.1 的「日翻转率 <5%」验收需用**分 basis 的周频口径**复核；这是及时性的必要代价，不因该验收指标而回退口径。

### D6 波动 / 持续性 / 加速度的序列

**问题**：二阶特征用什么序列？

**行业方法**：盈利波动用 3–5 年**年度**（或 TTM）ROE 标准差，不用单季 —— 单季 ROE 标准差主要反映季节性而非盈利稳定性。增长持续性按「年」定义。加速度是同比的二阶差分：Δ(本季单季同比 − 上季单季同比)。

**结论**：
| 标签 | 现状 | 目标 |
| --- | --- | --- |
| `qal_roe_stability_ind1/2` | 最近 3 期同类型（auto）报告期 ROE 标准差 | 保持同类型报告期**原值**序列（按 `report_type` 分组）；不用年化、不改年报序列（D4 裁决） |
| `qal_profit_years` | 年报序列 | 不变 |
| `grw_growth_persistence` | 年报序列 `revenue_yoy>0` 连续年数 | 不变（years 语义） |
| `grw_growth_accel` | 相邻两期**累计同比**之差（期长不同） | `quarterly`：本季单季同比 − 上季单季同比；`semi_annual`：本期 H1 同比 − 上年 H1 同比；`annual`：本期 FY 同比 − 上年 FY 同比。均同 basis |
| `accel` 阈值 ±5pp | 冻结值 | 因单季同比方差显著大于累计同比，**必须重标定**（并入 D10） |

**代价**：`semi_annual` / `annual` 维度的加速度是「年频二阶差分」，信息时效性低；需在 evidence 标注 `accel_basis`，且 L2 规则 `GRW-03/04` 对低频维度应考虑降级为不参与判定（标定后定，不预设）。

### D7 横截面分档的分组与最小样本

**问题**：切成三维度后行业组样本不足，怎么办？

**本项目实测**（§6.5，市场 × **一级行业** × basis；2026-11-25 修正 —— 早期版本误用三级行业码 `sw_code_l3` 当一级，虚增组数，已按 `load_industry_map(level=1)` 的真实口径重算）：

| 市场 | basis | 股票数 | 一级行业组 | <5 | 5–9 | 10–19 | ≥20 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | quarterly | 5578 | 32 | 0 | 0 | 0 | **32 全部达标** |
| A | semi_annual | 6 | 3 | 3 | 0 | 0 | 0 |
| HK | annual | 539 | 13 | 1 | 2 | 2 | 8 |
| HK | quarterly | 285 | 12 | 1 | 2 | 3 | 6 |
| HK | semi_annual | 2006 | 13 | 0 | 0 | 2 | 11 |

若严格「一级行业内 + 同 basis」且只在组内五等分，A 股完全无压力；港股 `quarterly` 有 6/12 组、`annual` 有 8/13 组达标，其余需回退。

**行业方法**：期长/期间**不混**（硬约束），但**同伴组层级允许回退**（GICS/申万惯例，§2.5）。

**阈值选择（用户 2026-11-25 裁定：保持 20，不调到 10）**：
- 阈值 20 是**分组质量开关**，不是覆盖率闸门——低于阈值的组回退到 `市场|basis`，不发布；所以 20→10 不改变覆盖率，只改变约 **87 只票（1.0%）** 用行业级还是市场级分档。
- 这 87 只落在 10–19 人的行业组，五等分后每档仅 **2–4 只**，统计噪声主导；回退市场级则每档 40–400 只。降到 10 会让这批票的分档更不可靠。
- 五等分描述性分档的保守下限是 20 人（每档 4 只）；`assign_tier` 的 `<5 不打档`（`quantile.py:573`）只是绝对兜底，不是推荐值。

**结论**：
1. `report_basis` 是硬分组键，**绝不跨 basis 混排**。
2. 三级层级回退：`市场|一级行业|basis`（有效样本 ≥20）→ `市场|basis`（≥20）→ 不发布（<5 或回退后仍不足）。**阈值保持 20**（用户裁定）。
3. `peer_level` 写进 `evidence`，消费端可据此判断分档可信度。
4. 二级行业（`_ind2_`）标签保持产出，但 L2 判定仍走一级口径（与 §5.2 D21 一致）；低频维度不额外切二级。
5. **不按 quarter_index 二次切组**：判定用同比已消除季节性（§2.4 原则 2），再切会让港股样本进一步归零。

**代价**：少数低样本行业的「行业内相对位置」退化为「市场内相对位置」，语义需在 evidence 明示。

### D8 降级阶梯与陈旧门禁

**问题**：「只有低频数据的公司降级到低频维度」具体怎么判？

**行业方法**：数据商用 "latest available comparable period" —— 若计算所需的上年可比期缺失，则该频率不可用，回退到下一个可用频率并标注；数据陈旧（stale）时不参与横截面。

**结论**（每票独立判定，优先级从高到低）：

| 阶梯 | 准入条件（全部满足） |
| --- | --- |
| `quarterly` | 最近一期可拆出单季（财年内期次连续）**且** 去年同季单季可得 **且** 两期流量 > 0 **且** 数据不陈旧 |
| `semi_annual` | 最近 H1 累计可得 **且** 去年 H1 累计可得 **且** 基期 > 0 **且** 数据不陈旧 |
| `annual` | 最近年报可得 **且** 上年年报可得 **且** 基期 > 0 **且** 数据不陈旧 |
| 不发布 | 以上皆不满足 → 按 §4.2「宁可不出档」，关闭当前有效行、不插新行 |

- 陈旧门禁：`as_of − period_end > 15 个月` 视为陈旧（约一年 + 披露滞后），不发布并在 `basis_reason` 记录。**该阈值需用 §8 P0 探查脚本实测分布后确认**，不预设为冻结值。
- `basis_reason` 记录降级原因码：`no_quarterly` / `no_comparable_prior_year` / `nonpositive_base` / `stale`。
- 降级只影响「用哪个维度算」，不影响该票仍可查到的低频 evidence 值。

**约束**：每票当期只有一个 `report_basis`；切换维度属于值/依据变化，按 §4.6 事件行处理（关闭旧行 + 插新行），并在 `conclusion_detail` 记 `basis_changed=true`。

### D9 PIT 与披露日

**问题**：单季化引入「前序期」「去年同季」取数，会不会放大前视偏差？

**行业方法**：PIT 要求所有参与计算的行都是「截至 as_of 首次可获知」的版本；restatement 不回溯覆盖已发布结论。

**本项目约束**：A 股 `announce_date` 覆盖 96.5% 且有历史回填污染（`quantile.py:281-287` 已有可信度护栏）；**港股恒 NULL → 放行 → 仍有前视**（§8.5）。

**结论**：
1. 累减用到的前序期、同比用到的去年同季，**全部叠加同一套披露日护栏**，不因新链路放宽。
2. ④ ⑤ 及两个 L3 的 `pit_capable` **保持 `False`**，`update_freq` 保持 `quarterly`；不宣称可严格回测。
3. 单季 YoY 的基期若被后续 restate，不做回溯修订（与 §4.4.4「不保留复盘当时结论」一致）。

### D10 结论规则与切点重标定

**问题**：口径换了，`RULES` 里冻结的阈值还能用吗？

**实测**：单季同比 vs 累计同比中位差 8.0pp、64.3% 的票差 >5pp（§6.3）。

**结论**：**所有涉及增速、加速度、ROE 绝对值的切点一律重标定**，不得沿用：

| 参数 | 现状 | 处置 |
| --- | --- | --- |
| `coverage_min=0.6` | 冻结 | 保留，但**分 basis 报出档率** |
| `divergence_gap_tier=2` | 冻结 | 按 basis 分别复核（分档本身免疫） |
| `high_growth_min=4` / `persistence_min=2` | 冻结 | 保留，复核各 basis 分布 |
| `accel` ±5pp | 冻结 | **必须重标**（单季同比方差更大） |
| `roe_floor_pct=5` | 冻结 | **按 `report_type` 分设阈值**（Q1 / H1 / Q3 / annual 的 ROE 原值不可比，同一个 5% 在 Q1 是优秀、在年报是平庸）；用户已同意，取各组 P25–P35 标定，见 §11 已裁定 1 |
| `quality_weak_max=2` / `quality_ok_min=3` / `stable_max=2` / `volatile_min=4` | D21 标定 | 现金转化率改单季后需重跑熵比标定；ROE 稳定性保持同类型序列（D6）故其切点可沿用，但复核 |
| `leverage_high=4` / `moderate_min=3` / `moderate_max=4` | D21 标定 | ROE 分档组内口径不变（仅键从 report_type 细分），复核即可 |

工具：`tools/calibrate_conclusion_cutpoints.py` + §5.2/§5.3 的分布熵比法；标定结果为**描述性标定**，不以未来收益为门槛（§10.2 不变）。

### D11 数据契约扩展

**结论**：`conclusion_detail`（JSONB，无需加列）新增：

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `report_basis` | enum | `quarterly` / `semi_annual` / `annual` |
| `period_months` | int | 3 / 6 / 12 |
| `peer_level` | enum | `industry_l1` / `market` |
| `basis_reason` | enum | D8 的降级原因码 |
| `basis_changed` | bool | 本行是否因维度切换产生 |
| `evidence.report_period` | dict | 逐字段增加 `basis` / `period_months`；ROE 标 `value='as_reported'`（不年化） |
| `evidence.cash_conversion` | dict | `single_q` / `accumulated` 双口径并列（D5） |
| `primary_report_period` | dict | 增加 `basis` |

`tag_registry.conclusion_dict` 的 `inputs[]` / `params` 同步声明新口径；`sync_conclusion_dict()` 的 version 一致性校验不变。

**约束**：消费端 `stock-realtime/db.py` 读取这些键属 §12.1 第 1 项「本轮不实施」，本次只保证生产侧写出、不要求消费端改造。

### D12 版本、迁移与合规

| 动作 | 内容 |
| --- | --- |
| version bump | 全部 `qal_*` / `grw_*` 标签（v2→v3）；`RULES`：`dim_quality_conclusion` v3→v4、`dim_growth_conclusion` v4→v5、两个 L3 同步 bump |
| 全量重建 | `compute_conclusions_rebuild.py --yes`（DELETE + INSERT、按 as_of 逐日、事件行压缩），`tag_run_log(status='ok', version)`，残留查询须 0 行 |
| 单标签重算 | L1 因 version bump 触发 `engine.compute_tag()` 全量重算；注意 `compute_profile.py` 的 `L2_L3_PAUSED` 当前为 `False`（已恢复产出） |
| schema.sql | 新增视图 DDL 同步；`_dump_schema_from_db.py` / `_schema_diff.py` / `_code_schema_diff.py` 三件自检 |
| 命名合规 | `v_financial_analysis_period`（名实匹配、单一职责）；不复用含糊的 `info`/`data`/`sector`；三名一致（视图 ↔ `report_basis` ↔ 标签 `compute_logic`） |
| 资源红线 | `financial_indicator` 约 36.5 万行：查询带时间下界、按 500 只/批、单事务 ≤5 万行、实测峰值 RSS、登记监控埋点；`AGENTS.md` R1/R3/R6/R7 + `doc/compute_task_resource_standard.md` §6 清单 |
| 依赖 | 不引入任何新第三方包，`requirements.txt` 无需改动 |
| 测试 | 更新 `tests/test_conclusions.py` 中对 `load_financial_context(conn, as_of, "auto")` 的断言；新增 basis 阶梯、单季同比、单季现金转化率（腾讯 Q3 对账）、ROE 原值不年化、基期≤0、分组回退、PIT 护栏、版本一致性的防回退测试；全量 `pytest tests/ -v` |

---

## 5 目标架构与数据流

```
financial_indicator（累计原值，唯一真相源，不改表）
   │
   ├── v_financial_quarterly（已有：财年内累减还原单季，不改语义）
   │
   └── v_financial_analysis_period（新增：分析期视图）
         · 每票每期一行，带 report_basis / period_months / quarter_index
         · 累计列族 + 单季列族并存；roe（报告期原值）；同期可比期对齐键
         · 全部行叠加 PIT 披露日护栏
              │
              ▼
       profiling/quantile.py
         · 新增 load_financial_period(as_of, basis)（按 500 只/批、带时间下界）
         · load_financial_context 分组键 市场|行业|report_basis + 层级回退
         · 保留 load_financial_ttm 原样（③ 估值专用，不扩张）
              │
              ▼
       quality.py / growth.py
         · 增速类：按 basis 自算同比（单季 / 半年 / 年度）
         · 水平类：ROE 报告期原值（按 report_type 分组）、单季现金转化率（降级时累计）、存量比率
         · 序列类：年报序列（波动、持续性）、同 basis 同比差分（加速度）
              │
              ▼
       RULES（重标定）→ 4 个 L2 + 2 个 L3 → tag_value（事件行）
         · conclusion_detail 带 report_basis / peer_level / basis_reason
```

`v_financial_analysis_period` 关键字段契约：

| 字段 | 说明 |
| --- | --- |
| `stock_code`, `period_end`, `fiscal_year_start` | 主键语义；非自然年财年靠 `fiscal_year_start` |
| `report_basis` | `quarterly` / `semi_annual` / `annual` |
| `period_months` | 3 / 6 / 12 |
| `quarter_index` | 财年内 1–4（仅 quarterly 有） |
| `revenue_ytd` / `np_ytd` / `ocf_ytd` | 累计原值 |
| `revenue_q` / `np_q` / `ocf_q` | 单季还原值（不可拆时 NULL） |
| `roe`（报告期原值）/ `debt_ratio` / 利润率 | 比率，不年化；标 `report_type` |
| `comparable_period_end` | 去年同 basis 可比期的期末日（供自算同比） |
| `data_quality_flag` | 累减异常 / 非正基期 / 陈旧 |

---

## 6 本机实测数据（2026-11-24，`localhost/market_db`）

> 全部为**只读查询**，未写库。以下数字是设计依据，不是冻结阈值。

### 6.1 最近一期报告期类型分布

| 市场 | Q3 | H1 | Q1 | annual | 合计 |
| --- | --- | --- | --- | --- | --- |
| A | 5577 | 6 | 1 | 0 | 5584 |
| HK | 275 | 2006 | 10 | 539 | 2830 |

**港股 71% 的公司最近一期是半年报，19% 只有年报** —— 说明「降级」是港股的主路径，不是边缘情况。

### 6.2 单季化可行性

| 市场 | 类型 | 可拆单季 / 总数 | 可拆率 |
| --- | --- | --- | --- |
| A | Q3 | 5575 / 5575 | 100% |
| A | H1 | 6 / 6 | 100% |
| HK | Q3 | 273 / 273 | 100% |
| HK | H1 | 47 / 1990 | **2.4%** |
| HK | annual | 3 / 546 | **0.5%** |

港股视图中仅 768 只票存在单季行，其中 **414 只**有去年同季可比 → 全港股约 **14.6%** 可直接算单季同比。

### 6.3 单季同比 vs 累计同比（A 股 Q3，n=5578）

| 统计量 | 值 |
| --- | --- |
| 平均绝对差 | 21.32 pp |
| 中位绝对差 | 7.99 pp |
| P90 | 32.62 pp |
| 差 >5pp 的票数 | 3586（64.3%） |

最新单季 6352 行中：缺去年同季 354 行、基期 ≤0 共 4 行、同比 >200% 或 <−90% 共 108 行、|同比| >50% 共 867 行。

腾讯 2026Q3：累计同比 **+9.18%**，单季同比 **+7.50%**（2073.36 / 1928.69 − 1），差 1.68pp。

### 6.4 ROE 报告期原值（腾讯）—— 为何必须按 `report_type` 分组

| 期次 | `roe` 原值 | 若年化（仅作对照，本项目不采用） |
| --- | --- | --- |
| 2025 Q1 | 4.749 | 18.996 |
| 2025 H1 | 9.908 | 19.816 |
| 2025 Q3 | 15.528 | 20.703 |
| 2025 annual | 21.135 | 21.135 |
| 2026 Q1 | 5.092 | 20.367 |
| 2026 H1 | 9.966 | 19.933 |
| 2026 Q3 | 15.025 | 20.033 |

原值在 3 个月 / 6 个月 / 9 个月 / 12 个月之间相差 **3–4 倍**（Q1 5.09% vs 年报 21.14%），因此：
1. **绝不跨 `report_type` 混排分档**（D4）；同类型报告期内横比才有效。
2. `roe_floor_pct` 单一 5% 在 Q1 与年报票上含义不同，按 `report_type` 分设（D10 / §11 已裁定 1）。
3. 年化列仅说明「原值差异来自期长」这一事实，不是本项目采用的计算口径（用户裁决 D4）。

### 6.5 分 basis 分组样本量

见 §D7 表（已按 `load_industry_map(level=1)` 的真实一级行业口径重算）。若回退到 `市场|basis` 级：A/quarterly 5578、HK/semi_annual 2006、HK/annual 539、HK/quarterly 285 —— 样本充足，回退方案可行。

### 6.6 P0 脚本实测结果（2026-11-25，`tools/probe_financial_period_basis.py`）

> 全程只读、未写库；峰值 RSS 20.4MB。六项探查结论如下，供 P1/P3 使用。

**① 最近一期分布**：A 股 5577 Q3 / 6 H1 / 1 Q1；港股 2006 H1 / 539 annual / 275 Q3 / 10 Q1。港股 71% 落 semi_annual，季度维度样本小。

**② 单季化可行性与陈旧度**：

| 市场 | 票数 | 可拆单季 | 且去年同季可比 | 报告期中位龄 | P90 | 超 15 月 |
| --- | --- | --- | --- | --- | --- | --- |
| A | 5584 | 5582 | 5576 | 56 天 | 56 天 | 0 |
| HK | 2830 | 332 | 283 | 148 天 | 239 天 | 62 |

- **D8 陈旧阈值 15 月通过**：A 股零命中、港股仅 62 只（2.2%）超线，阈值不误伤正常票且能拦住真正陈旧数据。
- 港股 `quarterly` 实际可用（可拆 + 去年同季）仅 283 只，降级路径是主路径，与 D8 一致。

**③ 单季同比 vs 累计同比差异**：A 股 n=5579，平均绝对差 21.31%、中位 7.99%、P90 32.60%、>5pp 3586 只、极端值 86；港股 n=394，平均 43.37%、中位 6.68%、>5pp 233。**印证 D10：切点必须全部重标定。**

**④ 分组回退比例（阈值 20）**：

| 市场 | basis | 行业组 | 行业级 | 回退市场级 | 不发布 |
| --- | --- | --- | --- | --- | --- |
| A | quarterly | 32 | 5578 | 0 | 0 |
| A | semi_annual | 3 | 0 | 0 | 6 |
| HK | annual | 13 | 504 | 32 | 3 |
| HK | quarterly | 12 | 222 | 60 | 3 |
| HK | semi_annual | 13 | 1981 | 25 | 0 |

- A 股 quarterly 全部行业级；港股 quarterly 222/285 行业级、60 回退；低频组样本充足，回退只在小行业发生。
- **闸门通过**：无需调整阈值 20，与 §11 裁定 2 一致。

**⑤ `roe_floor_pct` 分设候选**（各组近 3 年全部报告期原值分布）：

| report_type | n | P25 | P30 | P35 | P50 | 现行 ≥5% |
| --- | --- | --- | --- | --- | --- | --- |
| annual | 8236 | 2.633 | 3.215 | 3.850 | 6.086 | 4684 |
| H1 | 7932 | 1.340 | 1.663 | 1.993 | 3.170 | 2523 |
| Q3 | 5898 | 1.840 | 2.290 | 2.770 | 4.390 | 2655 |
| Q1 | 5756 | 0.620 | 0.760 | 0.920 | 1.440 | 489 |

- 现行单一 5% 在 Q1 只有 489/5756（8.5%）达标、annual 有 4684/8236（56.9%）达标 —— **同一阈值跨期次严重失衡**，印证 §11 裁定 1。
- P1 阶段取各组 **P30** 作为初始候选（Q1 0.76 / H1 1.66 / Q3 2.29 / annual 3.22），P3 用熵比标定后冻结。

**⑥ 单季现金转化率**：

| 口径 | n | P10 | P50 | P90 | OCF<0 |
| --- | --- | --- | --- | --- | --- |
| 单季 | 46903 | -3.758 | 1.195 | 7.607 | 12729 |
| 累计 | 55704 | -3.680 | 0.962 | 4.944 | 15748 |

- 腾讯 HK.00700 已对账：2026Q3 单季净现比 **4.17%** vs 累计 **88.18%**，与 §6.4 一致；双口径并列可直接支撑归因（D5）。
- 单季 OCF<0 占 27.1%（12729/46903），量级与 D5「约 25%」的预判一致。

### 6.7 P1 实施验证记录（2026-11-25）

**产出**
- `sql/schema.sql`：新增 `v_financial_analysis_period` 视图 DDL + COMMENT（每票每期一行，累计/单季双列族，`report_basis` / `period_months` / `prev_comparable_date`）。
- `profiling/quantile.py`：新增 `load_financial_period(conn, as_of)`（D8 阶梯选期 + 去年同期真值对齐 + 缓存）、`assign_group_level(df)`（D7 分组回退纯函数）、`_FIN_PERIOD_CACHE` 接入 `clear_caches()`。
- `tests/test_financial_period_basis.py`：11 项防回退测试（视图契约 / 加载函数 / 分组回退）。

**实测验证**
| 项 | 结果 |
| --- | --- |
| 视图本机库 | 373,149 行 / 8,414 票；basis 分布 quarterly 155,152 / annual 121,120 / semi_annual 96,877 |
| `load_financial_period(as_of=2026-11-25)` | 6,647 票；quarterly 5,101 / semi_annual 1,330 / annual 216；peer_level 行业级 6,245 / 市场级 436 |
| 腾讯 HK.00700 | Q3 / quarterly；`revenue_q=2073.36 亿`、`np_q=636.12 亿`、`ocf_q=26.52 亿`、`roe=15.0251`（原值，不年化）；单季同比 = (2073.36/1928.69−1) = **+7.50%**，与 P0 一致 |
| schema 自检 | `_code_schema_diff.py`：新视图已登记，无漏写；`_schema_diff.py`：新视图不在差异列表（DDL 与库一致） |
| 测试 | 新测 11 passed / 10 subtests；全量 895 passed / 1 failed；排除已知无关的 `test_sector_indicators.py` 后 881 passed 全绿 |

**实施中发现并修复的三个问题**（均为实现细节，未改变已裁定口径）
1. 视图初版用 `DISTINCT ON (stock_code)` 只留最近一期，无法支撑 D8 降级到低频维度 → 改为每票每期一行，选期交给加载函数（与 §5 字段契约一致）。
2. 去年同期对齐初版把 prev 期末日 `+1 年` 后再 join，取成了前年数据（腾讯 py_revenue 4878.11 亿而非 5573.95 亿）→ 改为 prev 期末日直接等于 `report_date − 1 年`。
3. 闰日 `date.replace(year+1)` 对 02-29 抛 `ValueError`；因修复 2 已移除平移逻辑，问题自然消解。

**已知无关失败**：`tests/test_sector_indicators.py::test_market_indicator_matches_snapshot_median`（PE 中位 21.595 vs 20.45），属 2026-11-24 sector 改造的数据快照差异，与本次改动无文件依赖，未修。

### 6.8 P2 实施验证记录（2026-11-25）

**产出**
- `profiling/quantile.py`：新增 `period_yoy(df, field)`（按 basis 自算同比，基期≤0→NaN）、`cash_conversion(df)`（单季/累计双口径）、`load_financial_period_yoy_series(conn, as_of, periods)`（同 basis 两期同比序列，供 D6 加速度）；`assign_group_level` 增 `period_col` 参数（ROE 传 `report_type`）。
- `profiling/tags/quality.py`：`qal_roe`（原值 + report_type 细分分档）、`qal_leverage`（分组换 basis）、`qal_earnings_quality`（单季优先、累计降级）改走 `load_financial_period`；`qal_gross_margin` / `qal_net_margin` / `qal_roe_stability` / `qal_profit_years` 口径不变。
- `profiling/tags/growth.py`：`grw_revenue_yoy` / `grw_profit_yoy` 按 basis 自算同比；`grw_growth_accel` 改同 basis 两期同比之差；`grw_growth_persistence` 不变。
- **版本**：口径变更的 11 个标签 bump v3；未变口径的 `qal_roe_stability` 等保持 v2；`qal_profit_years` / `grw_growth_persistence` 保持 v1。遵循「版本即口径」，未按 §7 表格笼统全 bump。

**实测验证**（as_of=2026-11-25）
| 标签 | 腾讯值 | 说明 |
| --- | --- | --- |
| `qal_roe_ind1_tier` | 档 4 / num 15.0251 | 报告期原值，不年化 |
| `qal_earnings_quality_ind1` | 档 **1** / num **4.17%** | 单季 Q3 现金流骤降被正确凸显（累计口径 88.18% 会落在高档） |
| `qal_leverage_ind1_tier` | 档 4 / num 44.69 | 存量比率 |
| `grw_revenue_yoy_ind1_tier` | 档 2 / num **+7.50%** | 单季同比，与 P0 对账一致 |
| `grw_growth_accel` | steady / **−3.49pp** | Q3 单季同比 7.50% − Q2 单季同比 10.99% |
| `grw_profit_yoy_ind1_tier` | 档 2 / num +0.76% | 单季净利同比 |

**实施中发现并修复的三个问题**
1. 视图初版只把 Q1/Q3 标 `quarterly`，H1 拆解出的 Q2 / annual 拆解出的 Q4 仍标累计维度且单季列为 NULL → 加速度会跳过 Q2（腾讯上期取到 Q1 而非 Q2）。改为按 `v_financial_quarterly.period_type ∈ {Q1,Q2,Q3,Q4}` 判定 basis，并新增 `quarter_type` 列；视图 JOIN 只按 (stock_code, report_date)，因为 Q2 的 `period_type` 与原始 `report_type=H1` 不同。
2. 现金转化率初版 `LOSS` 判定用单季净利、比率分母可能来自累计，导致 405 只票同时进 `ok` 与 `LOSS`（总数 7008 > 6644）→ 改为比率与分母同源，修复后 6602 行唯一、无重复。
3. `load_financial_period_yoy_series` 初版把 prev 期末日 `+1 年` 再 join（P1 已踩过同类坑），本次直接按 `report_date − 1 年` 对齐，未复现。

**测试**：新测 15 passed / 16 subtests；P1+P2 相关 64 passed；全量 897 passed / 1 failed（同一已知无关 sector 失败）。

### 6.9 P3 实施验证记录（2026-11-25）

**产出**
- `tools/calibrate_period_basis_cutpoints.py`：只读标定脚本，直接调用 P2 标签函数拿新口径 L1 值（旧脚本读库里的旧值，P4 才重建）。
- `profiling/conclusions/rules.py`：`dim_quality_conclusion` v3→**v4**；新增 `report_type` 输入（`role=aux`，不进 G1 分母）+ 派生字段 `roe_floor_ok`；QAL-04/QAL-05 条件由 `roe_raw >= 5` 改为 `roe_floor_ok == True`；params 由单一 `roe_floor_pct:5` 改为 `roe_floor_pct_by_report_type`。
- `profiling/conclusions/producer.py`：`compute="report_type"` 分支（取 `extras["period_auto"]`）；G1 分母排除 aux；`_derive` 计算 `roe_floor_ok`；`_load_financial_period(...,"auto")` 改走 P1 `load_financial_period`，与 P2 L1 同源，evidence 的 `primary_report_period` 增 `report_basis`。
- `profiling/tags/conclusion.py`：结论标签元数据版本同步 v4。
- 测试：新增 17 项 P3 相关测试（P3 文件累计 17 passed）。

**冻结切点（P30，正值子样本）**

| report_type | Q1 | H1 | Q3 | annual |
| --- | --- | --- | --- | --- |
| `roe_floor_pct` | **0.43** | **2.23** | **3.27** | **1.36** |

- `accel` 阈值：网格 ±3/±5/±10pp 熵比 0.815/0.814/0.807，差异可忽略 → **保持 ±5pp，不重标**。
- 其余切点沿用 D21 冻结值（`stable_max=2` / `quality_weak_max=2` 等）。

**验证**
| 项 | 结果 |
| --- | --- |
| 质量分档熵比 | 现行单一 5%：0.947；分设 P30：0.945（几乎不变，印证「保险丝非区分度来源」） |
| 分 basis 覆盖 | quarterly 5196 / semi_annual 1303 / annual 145；现金档与增速档覆盖 >99% |
| 生产路径 | 腾讯/SH.600519/SZ.000001 三只抽样：`report_type=Q3`、`roe_floor_ok=True`、`coverage=1.00`、结论正确 |
| `validate_rules()` | 通过；`sync_conclusion_dict` version 校验机制不变 |
| 测试 | P3 相关 17 passed；相关套件 68 passed；全量 903 passed / 1 failed（同一已知无关 sector 失败） |
| 峰值 RSS | 标定脚本 351.8MB（手动工具、非调度任务；R3 的 512M unit 红线不适用于本脚本） |

**实施中修复的问题**：元数据版本与 RULES 版本不一致（`conclusion.py` 仍 v3）导致 `sync_conclusion_dict` 会硬失败 → 已同步 v4。

### 6.10 P4/P5 实施与验收记录（2026-11-25）

**P4 执行顺序（两步，缺一不可）**
1. `compute_profile.py`：重算全部 L1（P2 新口径）——94 标签、0 失败、耗时 329s、峰值 329MB（<512M 红线）。
2. `compute_conclusions_rebuild.py --yes`：删除 6 个 derived 标签共 48,578 行历史 → 重建 34,516 行 → 残留旧版本 **0 行**、耗时 26.8s、峰值 267MB。

> 为何不能只跑第 2 步：重建脚本只读 `profile.tag_value` 里的 L1 值；P2 改了标签函数但不重算，库里仍是旧 L1。第一次增量 `compute_profile` 后残留 5,334 行旧 v3 结论（版本链保留已关闭行），全量重建后才归零。

**P5 本机验收**
| 项 | 结果 |
| --- | --- |
| 版本一致性 | 6 个 derived 标签全部 `tag_value.version == tag_registry.version`；残留查询 0 行 |
| 腾讯质量结论 | `paper_profit`（QAL-02）— ROE 15.03 达标但单季现金转化档 1（净现比 4.17%）触发「现金转化弱」，正是 D5 要凸显的突变信号 |
| 腾讯成长结论 | `slow_steady`（GRW-05）— 单季营收同比 +7.50% 未达高档（rank 档 2） |
| 证据链 | `report_basis=quarterly`、`primary_report_period={Q3, 2026-09-30, quarterly}` 均写入 |
| 分 basis 覆盖 | growth: quarterly 5195 / semi_annual 1303 / annual 145；quality: 4374 / 1086 / 57 |
| 测试 | 全量 904 passed / 1 failed（同一已知无关 sector 失败）；相关套件 71 passed |

**P5 实施中修复的问题**：`conclusion_detail` 顶层 `report_basis` 初版缺失（仅存在于 `primary_report_period` 嵌套内），与 §D11 数据契约不符 → 已在 `_detail()` 补顶层键并再次全量重建（第二次残留仍为 0）。

**待办（未在本机实施）**
- 回写 `doc/stock_profile_l2_l3.md` §5.2/§5.3/§8/§12 的口径与版本（生产文档同步）。
- 生产库部署与调度（用户侧运维，§12.1 第 2 项）；本机数据不代表生产。

---

## 7 标签与规则改造清单

| 对象 | 现状口径 | 目标口径 | version |
| --- | --- | --- | --- |
| `qal_roe_ind1/2_tier` | 最近一期原始 ROE 分档（grp=市场\|行业\|report_type） | 原值不变（D4 裁决）；grp 加 basis 层级回退，ROE 分档仍按 `report_type` 细分 | v2→v3 |
| `qal_roe_stability_ind1/2` | 3 期同类型(auto) ROE 标准差 | 保持同类型报告期原值序列（不改年报、不年化） | v2→v3 |
| `qal_earnings_quality_ind1/2` | 同期累计 OCF / 净利 | **单季** OCF / 单季净利（D5 裁决）；不可得时降级累计并标注；并列写两口径 | v2→v3 |
| `qal_leverage_ind1/2_tier` | 最近一期 `debt_ratio` | 不变，分组键换 basis | v2→v3 |
| `qal_profit_years` | 年报序列 | 不变 | v2→v3 |
| `grw_revenue_yoy_ind1/2_tier` | 源 `revenue_yoy`（累计同比） | 按 basis 自算同比 | v2→v3 |
| `grw_profit_yoy_ind1/2_tier` | 源 `net_profit_yoy` | 同上 | v2→v3 |
| `grw_growth_persistence` | 年报序列 | 不变 | v2→v3 |
| `grw_growth_accel` | 相邻两期累计同比之差 | 同 basis 同比之差 | v2→v3 |
| `dim_quality_conclusion` | `RULES` v3 | inputs/params 重标定 + basis 契约 | v3→v4 |
| `dim_growth_conclusion` | `RULES` v4 | 同上 | v4→v5 |
| `biz_value_quality_type` / `biz_growth_quality_type` | v3 | FCF 取数逻辑不动；inputs 声明同步 | v3→v4 |
| `evidence.report_period` | report_type / report_date | + basis / period_months；ROE 标 `value='as_reported'`（不年化） | — |
| `evidence.cash_conversion` | — | `single_q` / `accumulated` 双口径并列（D5），供归因 | — |

**不动**：③ 估值全部标签（`load_financial_ttm`）、⑩ 位置状态、`scale.py` / `shareholder.py` 的年报口径取数（另行评估，见 §10）。

---

## 8 实施顺序

| 阶段 | 内容 | 产出 | 闸门 |
| --- | --- | --- | --- |
| **P0 只读探查** ✅ **已完成 2026-11-25** | `tools/probe_financial_period_basis.py`（只读、不写库）；六项探查全部跑通，结果见 §6.6 | 数字报告（峰值 RSS 20.4MB） | 已确认 D8 陈旧阈值 15 月与 D10 标定范围，闸门通过 |
| **P1 数据层** ✅ **已完成 2026-11-25** | 新增 `v_financial_analysis_period` 视图并同步 `sql/schema.sql`；`quantile.py` 新增 `load_financial_period` / `assign_group_level` / 缓存接入 | 视图（本机库 373,149 行 / 8,414 票）+ 加载函数（选期后 6,647 票）+ 11 项单测 | schema 两件自检通过；腾讯单季同比 +7.50% 对账通过；新测全绿（详见 §6.7） |
| **P2 标签层** ✅ **已完成 2026-11-25** | `quality.py` / `growth.py` 9 个标签接新底表；新增 `period_yoy` / `cash_conversion` / `load_financial_period_yoy_series`；按口径变更 bump 版本 | 标签新口径 + 15 项单测 | 9 个标签端到端跑通；腾讯值对账通过；全量测试仅剩 1 项已知无关失败（详见 §6.8） |
| **P3 标定** ✅ **已完成 2026-11-25** | `tools/calibrate_period_basis_cutpoints.py`（读 P2 内存 L1）；ROE 底线按 report_type 分设并改规则引擎；accel 保持 ±5pp | 冻结切点 + 规则引擎改造 + 17 项单测 | 熵比无死档、最大单档 <80%；`validate_rules` 通过（详见 §6.9） |
| **P4 全量重建** ✅ **已完成 2026-11-25** | 先 `compute_profile.py`（94 标签重算，329s，峰值 329MB），再 `compute_conclusions_rebuild.py --yes`（删 48,578 行、重建 34,516 行） | 6 个结论事件行 + tag_run_log | 残留查询 **0 行**；重建峰值 267MB |
| **P5 验收** 🔄 **本机验收通过，文档回写待定** | `pytest tests/ -q`；本机库分 basis 覆盖与腾讯结论对账 | 904 passed / 1 failed（已知无关）；腾讯结论对账通过 | §9 验收表 |

> P0 是纯只读脚本，不写库、不建表、不动代码；按用户约定，AI 侧完成改代码后不重启服务、不做浏览器验证，由用户自行重启验收。

---

## 9 验收标准

| 项 | 标准 |
| --- | --- |
| 口径一致性 | 同一 enum 在同一 basis 内语义一致；`conclusion_detail.report_basis` 与 L1 取数 basis 可复算一致 |
| 单季对账 | 腾讯 2026Q3 单季营收 = 2073.36 亿、单季同比 = +7.50%、单季净现比 = 4.17%（累计口径 88.18% 并列）；A/H 抽样 30 只与公司公告/研报对齐 |
| ROE 原值 | 腾讯 Q1/H1/Q3/annual 的 `roe_raw` 等于 `financial_indicator.roe` 对应报告期原值（不年化）；分档组内 `report_type` 一致 |
| 覆盖率 | **分市场 × 分 basis** 报出档率；不得用全样本合并掩盖低频维度缺口 |
| 降级正确性 | 无可比期 / 非正基期 / 陈旧 → 正确降级或不发布，`basis_reason` 非空 |
| 分组回退 | `peer_level` 与实际分档组一致；<5 样本组不产出 |
| PIT | 前序期与去年同季均叠加披露日护栏；`pit_capable` 仍为 `False` |
| 版本一致性 | 全量重建后 `tag_value.version = tag_registry.version`，残留查询 0 行 |
| 稳定性 | 日频结论单日翻转率 <5%（§10.1 不变） |
| 可解释 | `rule_id + evidence` 经结构化条件可复算同一 enum |
| 资源 | 峰值 RSS 实测并留 ≥2 倍余量；单事务 ≤5 万行；监控埋点登记 |

---

## 10 风险与已知局限

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 港股去年同季单季缺失约 46%（有单季行的票中）/ 全市场仅 14.6% 可用 | quarterly 维度在港股样本极小 | D8 降级；分 basis 报覆盖率；不强行凑数 |
| 只披露 H1 + 年报的港股无法拆单季 | 永远无法进入 quarterly 维度 | 半年报维度按 H1 累计同比计算，属设计内正常路径 |
| 非自然年财年（阿里 4 月、新东方 5 月） | 期间错位 | 沿用 `fiscal_year_start` 分组，不按日历月硬映射 |
| 港股 `announce_date` 恒 NULL | 前视偏差仍存在 | `pit_capable=False` 不放宽，文档明示 |
| 基期 ≤0 / 扭亏导致同比无定义 | 覆盖率下降 | 置 NULL、不打档、不判负增长；`basis_reason` 记录 |
| 切点全部需重标定 | 工作量与过拟合风险 | 描述性标定（分布熵比），不以未来收益为门槛 |
| 单季 OCF 噪声大（约 25% 单季 OCF 为负、Q1→Q3 可从 174% 掉到 4%） | 现金转化档位跳变、翻转率上升 | 用户裁决接受（及时性优先）；`evidence` 双口径并列供归因；稳定性验收改分 basis 周频复核 |
| ROE 原值跨期长不可比（Q1 5% vs 年报 21%） | 同阈值误判（Q1 优秀被当成平庸） | 按 `report_type` 分设 `roe_floor_pct`（§11 已裁定 1）；分档严格同 `report_type` 组内 |
| FCF 单季不可还原（数据源缺 capex 明细） | `biz_growth_quality_type` 的 `fcf_sign` 仍为累计/最近可得口径 | 本期不动，evidence 标注期次不一致（沿用 §6.2 已接受的口径） |
| `scale.py` / `shareholder.py` 仍用 `load_financial_latest(..., "annual")` | 与本次分维度口径不一致 | 本期不扩散范围；列入后续评估，不改本次方案 |
| 新增视图 DDL 未同步 `schema.sql` | 部署到新服务器缺视图 | §D12 三件自检强制执行 |

---

## 11 决策裁定记录（全部已确认）

> 原待确认项 1（ROE 年化 + 现金转化率累计口径）已被用户**否决**：改为 ROE 用原值、现金转化率用单季（D4/D5 已重写）。下列两项已于 2026-11-25 由用户裁定。

**1. `roe_floor_pct` 按 `report_type` 分设阈值（D4 + D10）—— 已同意**
- 裁定：**按 `report_type` 分设 `roe_floor_pct`**，不沿用单一 5%。
- 落地：取各自组内约 P25–P35（与 D21「保险丝而非区分度来源」的定位一致），用熵比标定确认不产生死档后冻结。Q1 / H1 / Q3 / annual 四个阈值独立写入 `RULES[code].params` 并 bump version。

**2. 组内样本阈值保持 20（D7）—— 已裁定**
- 裁定：**保持 20，不调到 10**。
- 理由：阈值是分组质量开关（不是覆盖率闸门，低样本组回退到市场级）；20→10 仅让约 87 只票（1.0%）改用行业级分档，而这批票每档仅 2–4 只，统计噪声主导，分档更不可靠。
- 附带修正：早期分组样本表误用三级行业码当一级，已按 `load_industry_map(level=1)` 真实口径重算（§D7 / §6.5）。

> 至此全部决策项已确认，无待确认项。后续若改动已裁定口径，须重新走确认流程。

---

*本文档为口径设计规格，不含实现代码；涉及数据库结构的改动必须同步 `sql/schema.sql`（AGENTS.md 最高优先级约束），涉及「读库→计算→写库」的改动须过 `doc/compute_task_resource_standard.md` §6 检查清单。所有将上屏的中文字符串另行确认后再落地。*
