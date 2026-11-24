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

用户已裁决三条：① 季报维度单季化；② 只有低频数据的公司降级到低频维度；③ 暂不引入 TTM。在此基础上，本文档把剩余决策项**逐项给出结论**，不把选择题抛回给用户。

| # | 决策项 | 结论 |
| --- | --- | --- |
| D1 | 维度的表达方式 | 三个维度是**对比分层（report_basis）**，不是每票三套结论；每票只取一个「满足可比条件的最高维」 |
| D2 | 单季化算法 | 复用 `v_financial_quarterly` 的财年内累减法；新增独立视图 `v_financial_analysis_period` 承载 basis，不改原视图语义 |
| D3 | 同比口径 | 单季同比（同季 vs 去年同季）/ 半年同比（H1 vs 去年 H1）/ 年度同比（FY vs 去年 FY）；基期 ≤0 → 增速无定义 |
| D4 | ROE | **年化**：`roe_ann = roe × 12 / period_months`；不随增速维度单季化 |
| D5 | 现金转化率 | 保持**同期累计** OCF ÷ 净利（分子分母同期、期长抵消）；不用单季 |
| D6 | 波动 / 持续性 / 加速度 | 波动与持续性保持年报序列；加速度改为「相邻两个单季同比之差」 |
| D7 | 横截面分档 | basis 固定不混；组内样本 <20 时**行业层级回退**到「市场 × basis」，<5 不发布 |
| D8 | 降级阶梯 | `quarterly → semi_annual → annual → 不发布`，以「是否存在可比期」为准；加陈旧门禁 |
| D9 | PIT | 现有披露日护栏不动；分解用到的前序期、去年同季同样叠加护栏；`pit_capable` 保持 `False` |
| D10 | 结论切点 | **全部重标定**（单季同比与累计同比中位差 8pp）；绝对阈值按 basis 复核 |
| D11 | 数据契约 | `conclusion_detail` 增 `report_basis` / `period_months` / `peer_level` / `basis_reason`；JSONB 无需加列 |
| D12 | 版本与迁移 | 相关标签与 `RULES` 全部 bump、全量重建、补防回退测试、视图 DDL 同步 `sql/schema.sql` |

需用户裁决的仅 2 项，见 §11（均给出推荐默认值）。

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
| **C. 流量/存量比率未年化** | `roe` 是报告期加权 ROE，Q1 约 5%、Q3 约 15%、年报约 21%；`RULES` 里 `roe_floor_pct=5` 拿这三者直接与同一个 5% 比较 | 同一阈值在 Q1 票上是「极优秀」、在年报票上是「平庸」，绝对底线跨期次失效（腾讯年化后四期都约 20%，见 §6.4） |
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
| **流量 ÷ 存量** | ROE、ROIC、周转率 | **年化**：`YTD ROE × 12 / 月数`；单季 `×4`；或 TTM ROE | 分母是时点存量，不随期间长度增长，不年化则数值不可比 |
| **存量** | 资产负债率、净资产 | 直接取报告期末值，不拆不年化 | 与期间长度无关 |

**行业实证**：券商三季报研报同时列示「单 Q3 年化加权 ROE 9.6%」与「前三季度年化加权 ROE 13.6%」—— 两者都年化到 12 个月基准后才可比较，而不是把 ROE 当成季度流量去累减。

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
D4 ROE 年化 ──→ D10 的 roe_floor_pct 语义修正（原阈值在 Q1/年报票上含义不同）
D5 现金转化率 ──→ D7（比率类不进单季分组，但仍按 basis 分层）
D9 PIT ──→ 贯穿 D2/D3/D8（还原用到的前序期、去年同季同样要护栏）
D12 版本迁移 ──→ 由 D2~D11 的改动面决定 bump 范围
```

**三条依赖要点**：

1. **D2 决定 D8**：能否降级不是「看公司披露了什么报告」，而是「能不能算出可比的单季同比」。港股实测仅约 14.6% 的公司同时具备单季与去年同季（§6.2），所以降级规则必须是**数据驱动**的，不能是名单式的。
2. **D4 决定 D10**：ROE 年化后，`roe_floor_pct=5` 才第一次具备跨期次一致的经济含义；若不年化，切点重标定也无意义。
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
   `stock_code, period_end, report_basis, period_months, quarter_index, fiscal_year_start, revenue, net_profit, operating_cash_flow`（累计与单季两列族并存）、`roe_ann`、各比率、`report_date` / `prev_comparable` 对齐键。
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

### D4 ROE 年化

**问题**：单季化后 ROE 怎么办？

**行业方法**：ROE 是流量÷存量，不能累减；标准做法是年化到 12 个月。券商研报同时发布「单 Q3 年化加权 ROE」与「前三季度年化加权 ROE」，数值口径统一。

**本项目实测**（腾讯，§6.4）：Q1 5.09%→年化 20.37%、H1 9.97%→19.93%、Q3 15.03%→20.03%、2025 年报 21.13% —— **年化后四个期次收敛到 20% 上下**，验证年化有效。

**项目额外约束**：`financial_indicator` **没有净资产列**，无法算「单季净利 ÷ 平均净资产 ×4」；因此单季 ROE 不可得，只能年化 YTD ROE。

**结论**：`roe_ann = roe × 12 / period_months`（Q1 ×4 / H1 ×2 / Q3 ×4/3 / annual ×1）。ROE 作为**水平/慢变量**，不随增速维度单季化；判定用 `roe_ann`，`num_value` 存 `roe_ann`，`evidence` 同时保留原始 `roe` 与 `period_months`。

**副带修复**：`roe_floor_pct=5` 从「跨期次含义漂移」变成一致的经济底线；`tier_or_flag` 的 `LOSS` 判定仍用原始符号。

**代价**：与「全指标单季化」的字面理解有偏差 → 列入 §11 待确认项 1。

### D5 现金转化率（OCF ÷ 净利）

**问题**：单季 OCF ÷ 单季净利可行吗？

**行业方法**：可行但**不推荐**。单季净现比受备货、预收、季节性结算扰动；「全年滚动净现比（TTM）最常用、最稳定，规避淡旺季干扰，是选股核心参考指标」。用户已定本期不引入 TTM。

**结论**：保持**同期累计**口径（当前 `quality.py:144-159` 的做法），分子分母同报告期、同长度，期长在比率中抵消，跨 basis 数值可比；不单季化。在 `evidence` 中标注 `period_months` 与「累计期转化率，非单季」。TTM 版列入未来增强，不在本期。

**代价**：与「季报维度单季化」的字面理解有偏差 → 与 D4 一并列为待确认项 1（同一原则：流量/存量与流量/流量比率按金融惯例处理，不按流量处理）。

### D6 波动 / 持续性 / 加速度的序列

**问题**：二阶特征用什么序列？

**行业方法**：盈利波动用 3–5 年**年度**（或 TTM）ROE 标准差，不用单季 —— 单季 ROE 标准差主要反映季节性而非盈利稳定性。增长持续性按「年」定义。加速度是同比的二阶差分：Δ(本季单季同比 − 上季单季同比)。

**结论**：
| 标签 | 现状 | 目标 |
| --- | --- | --- |
| `qal_roe_stability_ind1/2` | 最近 3 期同类型（auto）报告期 ROE 标准差 | 最近 3 期**年报**、用 `roe_ann`（年报即原值）序列；不随 basis 变 |
| `qal_profit_years` | 年报序列 | 不变 |
| `grw_growth_persistence` | 年报序列 `revenue_yoy>0` 连续年数 | 不变（years 语义） |
| `grw_growth_accel` | 相邻两期**累计同比**之差（期长不同） | `quarterly`：本季单季同比 − 上季单季同比；`semi_annual`：本期 H1 同比 − 上年 H1 同比；`annual`：本期 FY 同比 − 上年 FY 同比。均同 basis |
| `accel` 阈值 ±5pp | 冻结值 | 因单季同比方差显著大于累计同比，**必须重标定**（并入 D10） |

**代价**：`semi_annual` / `annual` 维度的加速度是「年频二阶差分」，信息时效性低；需在 evidence 标注 `accel_basis`，且 L2 规则 `GRW-03/04` 对低频维度应考虑降级为不参与判定（标定后定，不预设）。

### D7 横截面分档的分组与最小样本

**问题**：切成三维度后行业组样本严重不足，怎么办？

**本项目实测**（§6.5，市场 × 一级行业 × basis）：

| 市场 | basis | 股票数 | 行业组数 | 中位组样本 | <20 的组 | <5 的组 |
| --- | --- | --- | --- | --- | --- | --- |
| A | quarterly | 5578 | 337 | 10 | 261 | 74 |
| HK | quarterly | 285 | 79 | 2 | **79（全部）** | 58 |
| HK | semi_annual | 2006 | 112 | 12.5 | 75 | 12 |
| HK | annual | 539 | 97 | 4 | 94 | 59 |

若严格「一级行业内 + 同 basis」且只在组内五等分，港股绝大部分组将因 `len(sub) < 5` 不打档，结论覆盖率会崩。

**行业方法**：期长/期间**不混**（硬约束），但**同伴组层级允许回退**（GICS/申万惯例，§2.5）。

**结论**：
1. `report_basis` 是硬分组键，**绝不跨 basis 混排**。
2. 三级层级回退：`市场|一级行业|basis`（有效样本 ≥20）→ `市场|basis`（≥20）→ 不发布（<5 或回退后仍不足）。
3. `peer_level` 写进 `evidence`，消费端可据此判断分档可信度。
4. 二级行业（`_ind2_`）标签保持产出，但 L2 判定仍走一级口径（与 §5.2 D21 一致）；低频维度不额外切二级。
5. **不按 quarter_index 二次切组**：判定用同比已消除季节性（§2.4 原则 2），再切会让港股样本进一步归零。

**代价**：低覆盖度行业的「行业内相对位置」退化为「市场内相对位置」，语义需在 evidence 明示 → 列入 §11 待确认项 2。

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
| `roe_floor_pct=5` | 冻结 | 改用 `roe_ann` 后重标，此时阈值才跨期次一致 |
| `stable_max=2` / `volatile_min=4` / `quality_weak_max=2` 等 | D21 标定 | 改年报序列后重跑熵比标定 |

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
| `evidence.report_period` | dict | 逐字段增加 `basis` / `period_months` / `annualized` 标记 |
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
| 测试 | 更新 `tests/test_conclusions.py` 中对 `load_financial_context(conn, as_of, "auto")` 的断言；新增 basis 阶梯、单季同比、年化、基期≤0、分组回退、PIT 护栏、版本一致性的防回退测试；全量 `pytest tests/ -v` |

---

## 5 目标架构与数据流

```
financial_indicator（累计原值，唯一真相源，不改表）
   │
   ├── v_financial_quarterly（已有：财年内累减还原单季，不改语义）
   │
   └── v_financial_analysis_period（新增：分析期视图）
         · 每票每期一行，带 report_basis / period_months / quarter_index
         · 累计列族 + 单季列族并存；roe_ann；同期可比期对齐键
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
         · 水平类：roe_ann、同期累计现金转化率、存量比率
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
| `roe` / `roe_ann` / `debt_ratio` / 利润率 | 比率，年化标记 |
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

### 6.4 ROE 年化验证（腾讯）

| 期次 | 原始 ROE | ×12/月数 | 年化 |
| --- | --- | --- | --- |
| 2025 Q1 | 4.749 | ×4 | 18.996 |
| 2025 H1 | 9.908 | ×2 | 19.816 |
| 2025 Q3 | 15.528 | ×4/3 | 20.703 |
| 2025 annual | 21.135 | ×1 | 21.135 |
| 2026 Q1 | 5.092 | ×4 | 20.367 |
| 2026 H1 | 9.966 | ×2 | 19.933 |
| 2026 Q3 | 15.025 | ×4/3 | 20.033 |

年化后跨期次收敛（约 20%），支持 D4。

### 6.5 分 basis 分组样本量

见 §D7 表。若回退到 `市场|basis` 级：A/quarterly 5578、HK/semi_annual 2006、HK/annual 539、HK/quarterly 285 —— 样本充足，回退方案可行。

---

## 7 标签与规则改造清单

| 对象 | 现状口径 | 目标口径 | version |
| --- | --- | --- | --- |
| `qal_roe_ind1/2_tier` | 最近一期原始 ROE 分档（grp=市场\|行业\|report_type） | `roe_ann` 分档（grp=市场\|行业\|basis + 回退） | v2→v3 |
| `qal_roe_stability_ind1/2` | 3 期同类型(auto) ROE 标准差 | 3 期年报 `roe_ann` 标准差 | v2→v3 |
| `qal_earnings_quality_ind1/2` | 同期累计 OCF / 净利 | 不变（累计口径），evidence 标 period_months | v2→v3 |
| `qal_leverage_ind1/2_tier` | 最近一期 `debt_ratio` | 不变，分组键换 basis | v2→v3 |
| `qal_profit_years` | 年报序列 | 不变 | v2→v3 |
| `grw_revenue_yoy_ind1/2_tier` | 源 `revenue_yoy`（累计同比） | 按 basis 自算同比 | v2→v3 |
| `grw_profit_yoy_ind1/2_tier` | 源 `net_profit_yoy` | 同上 | v2→v3 |
| `grw_growth_persistence` | 年报序列 | 不变 | v2→v3 |
| `grw_growth_accel` | 相邻两期累计同比之差 | 同 basis 同比之差 | v2→v3 |
| `dim_quality_conclusion` | `RULES` v3 | inputs/params 重标定 + basis 契约 | v3→v4 |
| `dim_growth_conclusion` | `RULES` v4 | 同上 | v4→v5 |
| `biz_value_quality_type` / `biz_growth_quality_type` | v3 | FCF 取数逻辑不动；inputs 声明同步 | v3→v4 |
| `evidence.report_period` | report_type / report_date | + basis / period_months / annualized | — |

**不动**：③ 估值全部标签（`load_financial_ttm`）、⑩ 位置状态、`scale.py` / `shareholder.py` 的年报口径取数（另行评估，见 §10）。

---

## 8 实施顺序

| 阶段 | 内容 | 产出 | 闸门 |
| --- | --- | --- | --- |
| **P0 只读探查** | 脚本放 `tools/`（只读、不写库）：分市场/basis 的覆盖率、单季同比与累计同比差异分布、降级原因分布、陈旧门禁阈值分布、分组回退比例、切点分布 | 数字报告 | 拿实测确认 D8 陈旧阈值与 D10 标定范围 |
| **P1 数据层** | 新增视图 DDL → 同步 `sql/schema.sql` → `quantile.py` 新增 `load_financial_period` / 分组回退 / 缓存 | 视图 + 加载函数 + 单测 | 三件 schema 自检；峰值 RSS 实测；结果对照 `v_financial_quarterly` 累减一致性 |
| **P2 标签层** | `quality.py` / `growth.py` 按 §7 改造 + version bump | 标签新口径 | 与旧口径逐票对照（差异归因，不要求一致） |
| **P3 标定** | `calibrate_conclusion_cutpoints.py` + 熵比法，分 basis 重标 `RULES` params；`sync_conclusion_dict()` 校验 | 冻结新切点 | 无死档、最大单档 <80% |
| **P4 全量重建** | `compute_conclusions_rebuild.py --yes`；`tag_run_log(status='ok')` | 6 个结论事件行 | 残留查询 0 行 |
| **P5 验收** | `pytest tests/ -v`；回写 `doc/stock_profile_l2_l3.md` §5.2/§5.3/§8/§12 | 全绿 + 文档同步 | §9 验收表 |

> P0 是纯只读脚本，不写库、不建表、不动代码；按用户约定，AI 侧完成改代码后不重启服务、不做浏览器验证，由用户自行重启验收。

---

## 9 验收标准

| 项 | 标准 |
| --- | --- |
| 口径一致性 | 同一 enum 在同一 basis 内语义一致；`conclusion_detail.report_basis` 与 L1 取数 basis 可复算一致 |
| 单季对账 | 腾讯 2026Q3 单季营收 = 2073.36 亿、单季同比 = +7.50%；A/H 抽样 30 只与公司公告/研报对齐 |
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
| FCF 单季不可还原（数据源缺 capex 明细） | `biz_growth_quality_type` 的 `fcf_sign` 仍为累计/最近可得口径 | 本期不动，evidence 标注期次不一致（沿用 §6.2 已接受的口径） |
| `scale.py` / `shareholder.py` 仍用 `load_financial_latest(..., "annual")` | 与本次分维度口径不一致 | 本期不扩散范围；列入后续评估，不改本次方案 |
| 新增视图 DDL 未同步 `schema.sql` | 部署到新服务器缺视图 | §D12 三件自检强制执行 |

---

## 11 待确认项（仅 2 项，均附推荐默认值）

**1. 水平 / 比率类指标不随增速维度单季化（D4 + D5）**
- 推荐：**采纳**。ROE 年化到 12 个月基准、现金转化率保持同期累计，符合金融行业对「流量÷存量」与「流量÷流量」的成熟处理；实测腾讯年化后跨期次收敛。
- 若坚持「所有指标一律单季化」：需要新增净资产等资产负债表字段（`financial_indicator` 现在没有）、并接受单季 OCF 季节性噪声导致档位翻转率显著上升、结论稳定性验收可能不达标。

**2. 组内样本 <20 时行业层级回退到「市场 × basis」（D7）**
- 推荐：**采纳**。期长/期间不混的硬约束不变，只放宽同伴组层级；回退后 A/quarterly 5578、HK/semi_annual 2006 样本充足。
- 若坚持严格一级行业内：实测 HK/quarterly 79 个组全部 <20、58 个组 <5，按 `assign_tier` 现有规则会大面积不打档，港股结论覆盖率将显著低于 §10.1 的 80% 门槛。

> 除以上两项，其余决策本文档已给出结论，无需逐项确认。两项均按推荐默认值编写，用户否决任一项时只需回退对应章节。

---

*本文档为口径设计规格，不含实现代码；涉及数据库结构的改动必须同步 `sql/schema.sql`（AGENTS.md 最高优先级约束），涉及「读库→计算→写库」的改动须过 `doc/compute_task_resource_standard.md` §6 检查清单。所有将上屏的中文字符串另行确认后再落地。*
