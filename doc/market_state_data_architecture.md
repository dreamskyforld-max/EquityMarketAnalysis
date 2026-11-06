# 市场状态分析特性 · 数据架构设计文档

| 项 | 内容 |
|---|---|
| 文档性质 | **数据层规格**（当前生效的结构与合约） |
| 版本 | v1.19 |
| 编制日期 | 2026-11-04（v1.19 修订 2026-11-06） |
| 表结构唯一真相源 | `sql/schema.sql`、`regime_schema.py` |
| 适用范围 | 第三层「市场状态」的三层指标体系：原子指标 / 维度指标 / 业务指标 |

---

## 0 阅读契约

| 你要找什么 | 去哪读 |
|---|---|
| 系统里有哪些数据对象、谁依赖谁 | §1 对象地图 |
| 编码、参数、字段类型、通用规则 | §2 全局索引（**每条信息只在这里定义一次**） |
| 某个对象怎么定义、吃什么、怎么算、吐什么 | §3 统一模板 → §4 / §5 / §6 |
| 做成什么样算对 | §7 验收 |
| 数据源清单 | 附录 A |
| 为什么这样定、否了什么 | 附录 B（ADR） |
| 改版编年 / 实现进度 | 附录 C / D |

**规格唯一真相源 = 本文档 + `sql/schema.sql`**；两者冲突时以 DDL 为准。本文档不写 UI / 前端展示方式，不写决策过程，不写项目进度——那些分别在 UI 工作流文档、附录 B、附录 D。

**与 `doc/market_profile.md` 的关系**：该文件是可行性研究报告，保留宏观层（§3）、资产配置层（§4）、方法论依据（§7、§8）与全部变更历史；**数据层规格以本文档为准**。本文档按统一模板完整写出全局骨架与 `DIM.BREADTH`（§5.1）、`DIM.VALUATION`（§5.2）、`DIM.SENTIMENT`（§5.3）、`DIM.FLOW`（§5.4）、`DIM.STRESS`（§5.5）、`DIM.TREND`（§5.6）六个维度对象（**六个维度均已落地**；情绪按方案 A「固定核心集 + 冻结有效起点」产出内部读数，见 §5.3；资金按「按市场单锚 + 冻结有效起点」产出内部读数，见 §5.4 / 附录 D；压力按「固定核心集 + 4 块两级等权 + 冻结有效起点」产出内部连续读数，见 §5.5 / 附录 D；趋势按「σ 归一化强度 t 作 L1 `raw_value` + expanding 分位作 `score`」的单锚产出读数，`state` 恒 NULL、方向归 `BIZ.MARKET_REGIME`，见 §5.6 / 附录 D）；其余对象在对象地图中列出、按模板逐节迁移（进度见附录 D）；§5.x 编号与该文件 §5 对齐，六维总览为章首无编号索引；L3 市场方向已按模板写出并落地（§6.1 v2）。

---

## 1 对象地图

### 1.1 三层依赖

```
外部源表（macro_series / daily_benchmark / a_daily_quote / hk_daily_quote / sector_* / 事件表）
        │
        ▼
L1 原子指标   indicator_value     一个观测量的原始值 + as-of 历史分位
        │  只交 (indicator_code, market, trade_date, percentile)
        ▼
L2 维度指标   dimension_value     六个侧面各自的 0-100 维度分 + 五档
        │  只交 (dimension_code, market, trade_date, score, state, ...)
        ▼
L3 业务指标   business_value      方向 / 温度 / 压力 / 预算，一市场一日一指标一行

横切：SIG.REVERSAL 信号直接读 L1 原子分位，不进任何合成、不改维度分
```

**两条层间铁律**：① 连续型业务指标从维度分算，判定型业务指标从原子算（规则要精确边界，维度分已被平均稀释）；② 维度分是「水平」分位，不是「变化」分位。

### 1.2 对象清单

| 层 | 对象 | 编码规范 | 字典表 | 数据表 | 主键 / 粒度 | 生产者 |
|---|---|---|---|---|---|---|
| L1 | 原子指标（约 123 个，其中 54 个进六维） | `域.名称`，如 `VAL.PE_TTM_MEDIAN` | `regime.indicator_dict` | `regime.indicator_value` | `(indicator_code, market, trade_date)` / 一市场一日一指标一行 | `market_state_daily.py`（已落地） |
| L2 | 维度指标（6 个：宽度 / 估值 / 情绪 / 资金 / 压力 / 趋势） | `DIM.*` | `regime.dimension_dict` | `regime.dimension_value` | `(dimension_code, market, trade_date)` | `build_dimension_value.py`（已落地宽度 / 估值 / 情绪 / 资金四个生产者，按 `dimension_code` 分派） |
| L3 | 业务指标（4 个：市场方向 / 市场温度 / 系统性压力 / 风险预算） | `BIZ.*` | `regime.business_dict` | `regime.business_value` | `(biz_code, market, trade_date)` | 无（仅 DDL） |
| 横切 | 反转信号（约 20 条） | `SIG.REVERSAL.TOP_*` / `BOTTOM_*` | `regime.signal_dict` | `regime.signal_value` | `(signal_code, market, trade_date)` | 无（仅 DDL） |
| 视图 | 信号汇总 | — | — | `regime.v_signal_summary` | 按市场 / 日期 | 无生产者时恒空 |
| 外部输入 | 宏观 / 行情 / 事件 | `MACRO.*` / `bench:` 等 | `regime.macro_series_meta` 等 | `regime.macro_series`、`public.daily_benchmark`、`a_daily_quote`、`hk_daily_quote`、`regime.sector_*`、事件表 | 各自主键 | 采集层（第①②层） |

> ⚠ `market` 是**市场键**（该行由哪个市场的计算产出），不是标的所在地；哪些市场有值由字典的 `market_scope` 声明。
> ✅ 旧合成层 `regime.market_regime_daily` 已于 **2026-11-05 彻底删除**（零生产消费方）：表、DDL、生产者、监控登记、测试与注释全部清理；L2 由 `build_dimension_value.py` 按字典分派产出。

---

## 2 全局索引（唯一真相源）

> 本章每条信息只有一个家。**参数不单独索引**：一律定义在它所绑定的计算环节（L1 分位参数见 §2.4.1、L2 门禁见 §2.4.3、估值专用见 §5.2 输入、L3 与信号见 §6）；后续章节引用参数名，不重复抄数值。

### 2.1 指标代码前缀说明

| 编码 / 前缀 | 名称 | 层级与说明 |
|---|---|---|
| `MACRO.*` / `CN.*` / `US.*` | 宏观 / 国债 / 基准 | 第①②层外部输入 |
| `BREADTH.*` | 宽度 | L1 原子指标族 |
| `VAL.*` | 估值 | L1 原子指标族 |
| `SENT.*` | 情绪 | L1 原子指标族 |
| `FORECAST.*` | 一致预期 | L1 原子指标族；评级乐观度按 `indicator_dict.domain` 归情绪（§5.3） |
| `FLOW.*` | 资金 | L1 原子指标族 |
| `RISK.*` | 风险 / 压力 | L1 原子指标族；压力维现行编码前缀，归属以 `indicator_dict.domain` 为准（§5.5） |
| `STRESS.*` | 压力 | L1 原子指标族（规划前缀；现行压力原子编码为 `RISK.*`，见 §5.5） |
| `TREND.*` | 趋势 | L1 原子指标族 |
| `SECTOR.*` | 行业 | 第④层，不进六维 |
| `DIM.BREADTH` | 宽度 Breadth | L2 维度指标 |
| `DIM.VALUATION` | 估值 Valuation | L2 维度指标 |
| `DIM.SENTIMENT` | 情绪 Sentiment | L2 维度指标 |
| `DIM.FLOW` | 资金 Flow | L2 维度指标 |
| `DIM.STRESS` | 压力 Stress | L2 维度指标 |
| `DIM.TREND` | 趋势 Trend | L2 维度指标 |
| `BIZ.MARKET_REGIME` | 市场方向 | L3 业务指标 |
| `BIZ.MARKET_TEMPERATURE` | 市场温度 | L3 业务指标 |
| `BIZ.SYSTEMIC_STRESS` | 系统性压力 | L3 业务指标 |
| `BIZ.RISK_BUDGET` | 风险预算 | L3 业务指标 |
| `SIG.REVERSAL.TOP_*` / `SIG.REVERSAL.BOTTOM_*` | 反转风险信号（顶部 / 底部） | 横切信号 |

编码表按编码索引（名称紧邻其右）；**其余表格一律名称在前、编码在后**，便于人直接阅读。

新增编码须先登记字典，再写计算。逐指标明细以 `regime.indicator_dict` 为准，本文档不复制 123 行清单。

### 2.2 参数变更的重算范围说明

本节不罗列参数值——**每个参数都定义在它所绑定的计算环节里**：L1 分位参数见 §2.4.1，L2 出档门禁见 §2.4.3，估值维度专用参数见 §5.2 输入，L3 与信号参数见 §6 各对象的 `params`。本节只回答一个跨对象的问题：**参数改了，归哪个口径版本、要重算哪些数据**。

| 字典 / 口径指纹 | 覆盖的参数类别 | 变更后的重算范围 |
|---|---|---|
| `indicator_dict.params_hash` | L1 分位参数：窗口、`min_periods`、`NO_PERCENTILE` 集合（`winsor` 为未生效预留键，见 §2.4 规则 1） | L1 原子分位 → 连带 L2 维度分 |
| `dimension_dict.params_hash` | L2 成员清单、聚合结构、门禁、`state_bands` / `state_labels`、维度级参数 | 仅 L2 `dimension_value` |
| `business_dict.params_hash` | L3 阈值、状态机、切档参数 | 仅 L3 `business_value` |
| `signal_dict.params_hash` | 信号阈值与输入清单 | 该信号历史命中 |

> ⚠ 消费端读到 `model_version` 与字典 `params_hash` 不等，即为旧口径数据。阈值一律登记在字典 `params` 中，**不得写在代码里**——否则调阈值不触发重算。
> ⚠ `winsor` 当前是**预留键、不参与计算**（见 §2.4 规则 1），改动不产生数值影响；若将来实现，它是唯一跨层参数，届时须显式触发 L1 分位与 L2 维度分**全量重算**。
> ⚠ 估值维度的 `quality_sample` / `check_divergence` 是本文件定义的 `dimension_dict.params` 新键（JSONB，无需 DDL 变更）；`state_bands` 默认 `[20,40,60,80]` 只是占位，生效值须标定后冻结。
> ⚠ **参考样本集变更是第二类全量重算触发**（2026-11-05 补）：`expanding` 类分位的参考样本是「截至该日的全部历史有效观测」，**历史数据回补 / 数据起点护栏解除会改变所有后续日的排位**，即使 `params_hash` 一字未改。此类变更须递增 `model_version` 并按指标族全量重算（§2.4 规则 4/5）。反之，若回补段被口径门禁挡在分位输入之外（如 §5.2 的 `VALUATION_ASOF_START`），则起点之后逐值不变，属「无数值影响」、不触发重算——但该判定必须在本文件显式记录，不能靠默认。

### 2.3 指标值存储表字段说明

**指标值存储表**：indicator_value -- 原子指标值、dimension_value -- 维度指标值、business_value -- 业务指标值。  
**字段说明**：字段说明回答的是「读到这一列时，它是什么、拿它可以做什么、什么时候不可用」，不只是类型和值域。

> **拿到任何一行，先做一件事**：
> `model_version` 与对应字典的 `params_hash` 不等 → 旧口径数据（版本映射见 §2.2）。
> ⚠ v2.1.12 起三张值表已删除 `is_final` 列：全项目零消费方、T+1 置终值流程从未实现。
> 「口径新旧」由 `model_version` 判定，「值是否刷新过」由 `updated_at`（`only_if_changed=True`）判定。
>
> 三张值表都是长表，用主键 `(编码, market, trade_date)` 点查。⚠ `market` 是**市场键**（该行由哪个市场的计算产出），不是标的所在地；哪些市场有值，由字典的 `market_scope` 声明。一行完整样例：L1 见 §4、L2 见 §5.2、L3 见本节末尾。

#### 2.3.1 每一列的作用

| 作用说明 | 对应列名 | 注意事项 |
|---|---|---|
| 画趋势 / 显示当前读数 | L1 `raw_value`；L2 / L3 `score` | L3 的 `score` 是多态列，必须先按 `biz_code` 过滤（见 2.3.4） |
| 显示一句定性结论 | L2 / L3 `state` | `NULL` = **未出档**，不是「中性」（§2.4.6） |
| 判断这个数能不能信 | `confidence`、`coverage`、`dispersion`、`flags` | `coverage` 低 = 只有半个维度有数据；`dispersion` 高 = 成员严重背离，此时平均分不具代表性 |
| 知道分数是谁拉高 / 拉低的 | `contrib` | 成员级 top3，**反推不出块级语义**（那是 `blocks` 的职责） |
| 知道是哪一块在紧张 | `blocks` | 只有 L2 有；`SYSTEMIC_STRESS.trigger` 的唯一来源 |
| 知道判定依据 / 被什么卡住 | `detail` | 只有 L3 有，键按 `biz_code` 不同（见 2.3.4） |
| 判断数据新旧 / 口径版本 | `model_version` | 见本节开头；`is_final` 列已删除 |

#### 2.3.2 `regime.indicator_value`（L1 原子读数）

一个原子指标在某市场某天的**原始值**与**它在自身历史中的排位**。主键 `(indicator_code, market, trade_date)`。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 什么时候为空 |
|---|---|---|---|
| `indicator_code`, `market`, `trade_date` | `VARCHAR(48)`, `VARCHAR(8)`, `DATE` | 一行的身份 | 点查、按日期拉序列；`market` 语义见本节开头 |
| `raw_value` | `NUMERIC(20,6)` | 指标原始值，单位见 `indicator_dict.unit` | 展示绝对值、画原始值曲线；量纲各异，**不要跨指标比较原始值** |
| `percentile` | `NUMERIC(6,2)` | 0–100 的 as-of 历史分位 | 跨指标可比的公共刻度，L2 合成与信号族都读它。NULL = 未达 `min_periods`，或该指标在 `NO_PERCENTILE` 集合内（§2.4.1） |
| `extra` | `JSONB` | 质量与明细 | 判断样本是否可信、是否需要标注偏差；形状见下 |
| `model_version` | `VARCHAR(16)` | `= indicator_dict.params_hash` | 判口径新旧 |

`extra` 的实际形状与键含义：

```json
{"n_sample": 2143, "survivorship_bias": true, "quality": "low"}
```

- `n_sample`：截至当日参与分位计算的有效观测数。小于 `750` 时读数应降置信（§2.4.6）。
- `survivorship_bias`：该日成分池缺退市股，早期年份为 `true`，此时估值 / 宽度类分位只能作趋势参考。
- `quality`：数据质量档；`low` 的行在统计与展示中应分层或剔除。

> ⚠ **`percentile` 未做方向调整**：它是观测值的排位，与「这个指标高是好是坏」无关；极性在 L2 由 `members[].sign` 统一（§2.4.2）。直接把 `percentile` 当成"风险高低"会读反。
> ⚠ 低频源（月 / 季频）被 PIT 填成日频后，同一取值重复入样，**月内分位斜率不含信息**，不要据此判趋势（§2.4.1）。

#### 2.3.3 `regime.dimension_value`（L2 维度读数）

一个维度指标在某市场某天的合成结果。主键 `(dimension_code, market, trade_date)`。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 什么时候为空 |
|---|---|---|---|
| `dimension_code`, `market`, `trade_date` | `VARCHAR(24)`, `VARCHAR(8)`, `DATE` | 一行的身份 | 点查；按 `dimension_code` 过滤，禁止跨维度比较（§2.4.7） |
| `score` | `NUMERIC(6,2)` | 0–100，已按该维度 `polarity` 统一方向 | 画维度分曲线、作为 L3 连续型业务指标的输入 |
| `state` | `VARCHAR(12)` | 五档定性，标签见 `dimension_dict.state_labels` | 展示一句结论。**NULL = 未出档**（`coverage` / `dispersion` 门禁未过，或短样本约束期），不是「中性」（§2.4.6） |
| `coverage` | `NUMERIC(5,4)` | 0–1，有效成员 / **本市场应有成员** | 判断本市场成员到齐没；分母按 `market_scope` 计（`members[].code` 联 `indicator_dict.market_scope ∈ {当前市场, CN+HK, GLOBAL}`），**不含其它市场专属成员**。⚠ 它不反映「维度构念是否完整」：某市场天然缺一整个块时 `coverage` 仍可为 1.0，此类缺口由 `flags.dimension_completeness` / `flags.block_coverage` 与块级门禁承担（§5.3） |
| `dispersion` | `NUMERIC(6,2)` | **参与合成成员**（排除 `flags_only`）分位的标准差 | 判断是否内部严重背离；门禁用 `dispersion_norm = dispersion × sqrt(core_valid/10)`（方案 C） |
| `blocks` | `JSONB` | **块级分数**：每个分块各自的分数与覆盖度 | 回答「是哪一块在动」；`SYSTEMIC_STRESS.trigger` 的唯一来源，形状见下 |
| `contrib` | `JSONB` | **成员贡献**：正负各 top3 | 下钻看「谁把分数拉高 / 拉低」，形状见下 |
| `flags` | `JSONB` | 结构性背离标记 + 可用性标记 | 判断这一行能否当结论、哪些信息不能被平均抹掉，形状见下 |
| `confidence` | `VARCHAR(8)` | 高 / 中 / 低 | 低置信时不得按确定结论使用 |
| `model_version` | `VARCHAR(16)` | `= dimension_dict.params_hash` | 判口径新旧 |
| `updated_at` | `TIMESTAMPTZ` | 写入时间 | 排查重算 / 补数 |

**三个 JSONB 的实际形状与作用**（`score` 是压成单个数，这三列保留被压掉的信息）：

`blocks` —— 块级分数。DDL 定义的形状是 `{"块名":{"score":..,"coverage":..}}`：

```json
{"波动": {"score": 78.2, "coverage": 1.0}, "信用": {"score": 61.5, "coverage": 0.5}}
```

六维不是扁平等权，而是分块（宽度 4 族、情绪 2 块 + 1 诊断成员、资金 2 块、压力 4 块 + 1 诊断、趋势 3 块单锚）。单个 `score` 看不出是哪一块在动；`SYSTEMIC_STRESS.trigger` 判「信用在扩」还是「波动在冲」时，**只能用 `blocks`**——成员级 `contrib` 拿不到块级聚合语义，没有它只能用 top3 反推。趋势维（§5.6 v1.19）`blocks` 用的就是本列标准形状 `{块名:{score,coverage}}`，不含原始 t / `raw_value`（那在 L1 `indicator_value.raw_value`）。

`contrib` —— 成员贡献，正负各 top3：

```json
{"pos": [{"code": "VAL.PB_MEDIAN", "delta": 8.2}],
 "neg": [{"code": "VAL.ERP", "delta": -5.1}]}
```

用于下钻与口径排查：`delta` 是该成员对齐极性后与维度分的偏离。单锚结构下即「锚 vs 校验项」的偏离；校验项不参与加权，只写这里。

`flags` —— 两类标记：

| 类别 | 键（示例） | 用途 |
|---|---|---|
| 结构性背离与极端（DDL 原意） | 快慢窗反向（`NEW_HIGH_LOW_20` vs `_60`）、`MA60` 与 `MA200` 分层、等权与市值加权差极端 | 这些对照**不能被平均分抹掉**；`dispersion` 高时必须下钻 `contrib` / `flags` 才能解释 |
| 可用性（本文件扩展） | `short_sample`、`n_sample`、`anchor_window`、`no_data`、`low_coverage`、`high_dispersion` | 判断这一行能否当结论、为什么 `state` 为 NULL；键名由生产端按 §5.2 的门禁与短样本约束约定 |
| 维度完整度（§5.3 扩展，描述量） | `dimension_completeness`（本市场应有成员数 / 全体 `members[]`）、`block_coverage`（有效合成块 / 应有合成块）、`block_incomplete` | 标注「这个市场是否只有半个构念」。⚠ **`dimension_completeness` 不是 `coverage`**，不作出档门禁 |
| 可用性分层（§5.3 方案 A） | `availability_tier`（`core3_fixed` / `hk_single_proxy`）、`core_coverage`、`member_valid_from`、`dispersion_core`、`low_common_factor`、`single_block_proxy`、`short_sample` | 标注「这一行用的是哪套成员集、起点是否达标、构念共同因子是否弱、是不是 HK 单成分代理」。⚠ **产分门禁用 `core_coverage`**（固定核心集全有效），`coverage` 只作描述量；`state` 恒 NULL 时不得当「中性」（§5.3 参数表） |

> ⚠ **本表没有 `quality` / `extra` 列**（它们属于 L1）。可用性一律用 `confidence` + `flags` 承载；写 `quality='low'` 会落库失败。
> ✅ **v2.1.11（2026-11-05）两处口径已固定**（写入 `dimension_dict.remark`，由 `tests/test_breadth_v2111.py` 钉住）：① `DIM.BREADTH.flags` 为 **dict** —— `structural: list[str]` 恒存在（结构性背离文案），`n_sample: int`（族代表中最小的有效观测数），仅命中时写 `low_coverage` / `high_dispersion` / `missing_representative` / `short_sample` / `no_data: true`；旧的字符串数组形状已废止，**下游不得按 list 遍历**。② `blocks` 按维度区分：宽度每族 `{score, coverage, representative}`，估值 `{anchor:{code,percentile}, check:{n,divergent}}`；`DIM.VALUATION.flags` 为 dict（`n_sample` / `anchor_window` / 可选标记）。
> ⚠ `sql/schema.sql` / `regime_schema.py` 的 `flags` 列注释仍是 DDL 原意（仅结构性背离），可用性标记属本文件定义的扩展用法，DDL 注释待同步（不改变表结构）。

#### 2.3.4 `regime.business_value`（L3 业务读数）

一个业务指标在某市场某天给出的结论。主键 `(biz_code, market, trade_date)`。**先按 `biz_code` 过滤再看任何字段**——`score` / `level` / `state` 都是多态列。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 什么时候为空 |
|---|---|---|---|
| `biz_code`, `market`, `trade_date` | `VARCHAR(32)`, `VARCHAR(8)`, `DATE` | 一行的身份 | 必须先按 `biz_code` 过滤，禁止跨 biz 联合查询 |
| `score` | `NUMERIC(6,2)` | **多态**，含义按 `biz_code` 见下表 | 连续读数；纯状态类可空 |
| `state` | `VARCHAR(16)` | **多态**：上行·震荡·下行 / 正常·关注·警戒·危机 / 极低…极高 | 展示状态标签；`RISK_BUDGET` 用 `level` 而非 `state` |
| `level` | `SMALLINT` | **多态**，档位序号（1 起） | 便于比较与画图，但**四个 biz_code 语义互不相容**，禁止跨 biz 比较或同图绘制 |
| `since_date` / `dwell_days` | `DATE` / `SMALLINT` | 进入当前状态的日期 / 已停留交易日数 | 只有状态机类（`MARKET_REGIME`）有；判断状态是否稳定。非状态机行为 NULL |
| `status` | `VARCHAR(12)` | 已确认 / 待确认 | 初始化段为「待确认」，`RISK_BUDGET` 据此保守取值 |
| `confidence` | `VARCHAR(8)` | 高 / 中 / 低 | 宽度确认等外部调节项写这里 |
| `detail` | `JSONB` | 增量信息，键按 `biz_code` 见下表 | 「被什么卡住的」「为什么判定」比数字更有用，见下表 |
| `model_version` | `VARCHAR(16)` | 口径版本 | 见本节开头 |

**`level` 与 `score` 按 `biz_code` 的实际含义**（使用者最容易读错的地方）：

| `biz_code` | 名称 | `level` 含义 | `score` 含义 |
|---|---|---|---|
| `BIZ.MARKET_REGIME` | 市场方向 | 1=下行 / 2=震荡 / 3=上行（升序=越偏多） | 趋势强度，与方向解耦（= `DIM.TREND.score`） |
| `BIZ.SYSTEMIC_STRESS` | 系统性压力 | 1=正常 … 4=危机（升序=越危险） | 压力分（= `DIM.STRESS.score`） |
| `BIZ.MARKET_TEMPERATURE` | 市场温度 | 1=极冷 … 5=极热（升序=越热） | 四维 as-of 分位的等权平均（0–100，分布向 50 集中）；**不是分位读数、不是泡沫程度、不驱动任何 `cap`** |
| `BIZ.RISK_BUDGET` | 风险预算 | 1=20% … 5=100%（升序=敞口越大） | `cap × 100`（敞口百分比） |

**`detail` 按 `biz_code` 必带键**：

| `biz_code` | 键 | 回答什么 |
|---|---|---|
| `MARKET_REGIME` | `candidate`、`evidence`、`breadth_confirmation`、`trend_state`、`turn_risk` | 候选态、证据、宽度确认、趋势/转折二分与转折风险（§6.1 v2） |
| `MARKET_TEMPERATURE` | `components`、`quantiles`（**无条件必输出**）、`quality`、`coverage`、`dispersion`、`low_agreement`、`composition_version` | 四个分量（各维 0–100 的 as-of 分位 `u_i`）、四个冻结切点值、质量与分歧；禁止按「X = 历史 X 分位」解读 `score` |
| `SYSTEMIC_STRESS` | `trigger`、`spike`、`blocks` | 压力是被哪一块触发的（数据来自 `dimension_value.blocks`） |
| `RISK_BUDGET` | `binding_constraint`、`raw_cap`、`inputs`、`constrained_by`、`missing` | 被哪个约束卡住、原始 cap（`second_binding` 由 §6 定义，落库位置由实现确定） |

> ⚠ 消费方读取 `level` / `score` 前，必须按 `biz_code` 过滤并校验 `business_dict.output_schema.level_semantics` / `score_semantics`；建议按 `biz_code` 建独立视图，禁止跨 biz 联合查询。

L3 一行样例（市场温度）：

```json
{
  "biz_code": "BIZ.MARKET_TEMPERATURE",
  "market": "CN",
  "trade_date": "2026-11-04",
  "score": 57.20,
  "state": "较高",
  "level": 4,
  "since_date": null,
  "dwell_days": null,
  "status": null,
  "confidence": "中",
  "detail": {"components": {"DIM.VALUATION": 63.4, "DIM.SENTIMENT": 71.2,
                           "DIM.FLOW": 48.6, "DIM.BREADTH": 45.6},
             "quantiles": [37.5, 46.2, 53.8, 62.5],
             "quality": "low", "composition_version": "v1"},
  "model_version": "e5f6a7b8"
}
```

样例中 `score = mean(components) = 57.20`（可反算）；`state` / `level` 由 `detail.quantiles` 切出，`57.20` 落在 P60（53.8）与 P80（62.5）之间，故为第 4 档「较高」。

#### 2.3.5 `regime.signal_value`（横切信号命中）

主键 `(signal_code, market, trade_date)`。`hit BOOLEAN` = 当日是否命中，`days SMALLINT` = **已连续命中交易日数（必填）**，`strength NUMERIC(12,4)` = 命中强度（可空），`detail JSONB`，`model_version`。

「站上 MA200 比例回落 3 天」和「回落 40 天」是完全不同的信息，所以 `days` 不能省。信号是**条件命中**，不是程度读数，**不得并入任何维度分或业务指标**；判读按族去重后的 `family_cnt`（见 §6）。

#### 2.3.6 原子、维度、业务、信号指标字典表作用说明

| 字典 | 什么时候读它 | 关键列 |
|---|---|---|
| `regime.indicator_dict` | 想知道某原子指标的**单位、方向、频率、来源、属于哪个市场 / 维度** | `unit`、`direction`、`freq`、`source`、`market_scope`、`domain`、`params_hash`、`breaks` |
| `regime.dimension_dict` | 想知道某维度**由哪些成员组成、怎么聚合、五档切点与标签** | `members[]`、`state_bands`、`state_labels`、`params`、`params_hash` |
| `regime.business_dict` | 想确认某业务指标的**输入清单、阈值、多态列语义** | `kind`、`inputs`、`params`、`output_schema`、`params_hash` |
| `regime.signal_dict` | 想确认某信号的**条件、阈值、属于哪个族、哪些市场有源** | `expr`、`inputs`、`params`、`category`、`market_scope` |

字典列定义以 `sql/schema.sql` 为准；`params_hash` 与值表 `model_version` 的对应关系见 §2.2。

### 2.4 指标通用计算规则说明

| # | 规则 | 内容 | 为什么 |
|---|---|---|---|
| 1 | 分位化 | `percentile = 100 × (截至 trade_date 的样本中 ≤ 当前值的比例)`；并列值取中位秩；`percentile` 由经验秩直接算出；`winsor` 为**预留键、当前不参与计算**（详见下方参数表）。涉及的窗口、`min_periods`、`NO_PERCENTILE` 参数见下方「规则涉及的参数」 | 分位是唯一能跨量纲比较的公共刻度；并列值取上界会把整批并列观测记成同一极端分位；winsorize 若作用于结果会截掉尾部、与「极值敏感」的设计目的相反。低频源 PIT 填成日频后重复入样，月内斜率不含信息（当前实现的已知偏差） |
| 2 | 极性统一 | 以 `members[].sign` 为唯一翻转真源（`+1` 顺向 / `−1` 取 `100 − percentile`）；`direction` 只是语义标记；消费端不得二次翻转 | 方向若在多处定义，同一原子指标在不同页面会读出相反含义；目标是让各维 `score` 对齐该维 `polarity` |
| 3 | 出档门禁 | `coverage` 或 `dispersion_norm` 未达门禁 → 只出 `score` + `low_confidence`，**不输出 `state`**；三个参数见下方「规则涉及的参数」 | 覆盖不足说明「半个维度」，成员严重背离说明平均分不具代表性；宁可说不确定，不给确定的错结论 |
| 4 | PIT / 终值 | 分位只用截至该 `trade_date` 可得的样本，落库即终值，禁止刷写历史行；全量重算只有**两类合法触发**：① `params_hash` 变更（口径变了）；② **参考样本集变更**（历史数据回补 / 数据起点护栏解除）。两者都须递增 `model_version`；②的判定与例外见 §2.2 末注 | 否则今天回看历史维度分与当时发布的值不是同一个数，快照不可复现；`model_version` 让新旧口径可区分 |
| 5 | 幂等与重算粒度 | L1 可按单日单指标重算，**但仅限参考样本集未变的情形**；`expanding` 类指标一旦历史回补，须按指标族全量重算并递增 `model_version`，不得只补新日期（口径门禁把回补段挡在输入之外时例外）；L2 可单日重算但某日须六维全部重算；L3 状态机类必须全量重算 | 成员跨维度共享，只算一维会让 `coverage` / `dispersion` 与邻维不一致；`dwell` / `streak` 是累计量，从窗口中间以空状态起步会被重置；**`expanding` 分位的参考样本含全部历史，新增历史观测会改变每一天的排位，只写新日期会让同一序列混存新旧两套分位** |
| 6 | 可用性 | `state IS NULL` 只能读作「未出档」；`model_version` 与字典 `params_hash` 不等即旧口径；`n_sample < 750`（= `3 × min_periods`）降置信 | 短样本分位不是「值小」而是不可信；NULL 若被当成「中性」，会把低置信读数伪装成确定结论 |
| 7 | 不可比 | 六个维度分不可横向比较或求均值；唯一合法例外是连续型业务指标先把各维 `score` 转成自身 as-of 分位 `u_i` 再等权平均；禁止跨维度雷达图 | 分位基数、聚合结构、语义都不同，宽度 60 与估值 60 没有可比性；未归一就平均会因「单锚分位」与「多次平均分位」的方差差异产生隐式权重 |

**规则涉及的参数**（附属于上面的规则，不是独立索引；变更的重算范围见 §2.2）

| 参数 | 值 | 生效步骤 | **作用**（改变了什么输出、不改变什么） | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `BREADTH_PCT_WINDOW` | 2520（滚动 10 年有效观测） | 规则 1，宽度成员计算分位、选取历史样本时 | **决定参与排位的历史样本范围**：只取最近 2520 个有效观测，窗口外的观测不进样本 → 同一个当日值在不同窗口下得到不同 `percentile`。**不改变 `raw_value`** | 宽度分布随上市制度漂移，过长窗口会混入不同年代。变更触发 `indicator_dict.params_hash`，重算 L1 宽度分位并连带 L2 |
| 其余维度窗口 | `expanding`（全样本 as-of） | 规则 1，估值 / 压力等成员计算分位时 | **决定样本范围为全部历史**：观测自起点起一直留在样本内，历史极值不会被移出，`percentile` 随样本累积缓慢变化。**不改变 `raw_value`** | 估值均值回归周期 10–20 年，滚动会截断周期极值。变更同上 |
| `min_periods` | 250 | 规则 1 计算分位之前；规则 6 判置信度时 | **有效观测不足 250 时整步不计算分位**（`percentile` 为 NULL）；另以 `3 × min_periods = 750` 为门槛，`n_sample < 750` 时把 `confidence` 降为低。**不修改 `score` 数值** | 约 1 年有效交易日。变更触发 L1 重算并连带 L2 |
| `winsor` | `[2,98]`（**预留，未生效**） | 无（当前不参与任何计算） | **不改变任何输出**：L1 分位由 `market_state_daily._expanding_percentile` 的经验秩（中位秩）直接算出，既不裁剪参考样本、也不改变 `percentile`；字典保留该键仅为兼容既有种子 | 经核查全项目无计算代码消费该键（2026-11-05）。当前**无数值影响、不触发重算**；若将来实现，须递增 `params_hash` 并连带 L1 + L2 全量重算（§2.2） |
| `NO_PERCENTILE` | `{MACRO.CLOCK_QUADRANT, VAL.ERP_Z}` | 规则 1，进入分位计算之前 | **命中的指标整步跳过**，`percentile` 恒为 NULL；后续合成与信号族不得把它当 0–100 读数使用，`raw_value` 照常产出；⚠ **因此命中项不得列入任何 `dimension_dict.members[]`**（v1.2，估值维度据此移出 `VAL.ERP_Z`） | 分类象限 / 已标准化的 z-score 再取分位不产生信息。变更触发 L1 重算 |
| `coverage_min` | 0.6 | 规则 3，`state` 出档判定时 | **`coverage < 0.6` 时不出 `state`**（写 NULL），只保留 `score` 并标 `low_confidence`。**不修改 `score` 数值** | 超过 1/3 成员缺失时平均分不具代表性。变更触发 `dimension_dict.params_hash`，仅重算 L2 |
| `dispersion_max` | 30 | 规则 3，对 `dispersion_norm = dispersion × sqrt(n/10)` 判定时 | **归一化分歧度 > 30 时不出 `state`**，只保留 `score` + 低置信。**不修改 `score`**；`sqrt(n/10)` 使同一阈值不受成员数影响 | 成员严重背离时平均分不具代表性。⚠ **小 n 维度的已知限制**：`n=2` 时归一化因子 `sqrt(2/10)=0.447` 会把门禁放宽到原始标准差约 67 才触发，须由**块级门禁**兜底（§5.3 `block_coverage_min`），不得只靠本式。变更触发 `dimension_dict.params_hash`，仅重算 L2 |
| `weight_cap` | 0.25 | 等权类聚合计算权重时：`w = min(1/n_eff, 0.25)`（`build_dimension_value.py`） | **限制单个成员的权重上限**：成员很少时（如 `n_eff = 2`）原本每人 50%，被压到 25% 后再按权重和归一化，防止个别成员绑架维度分。**只影响 `score` 的合成权重，不影响 `coverage` / `dispersion`** | 稳健性约束。⚠ 对 `single_anchor`（估值）与分块结构不生效，仅等权类聚合使用。变更触发 L2 重算 |

---

## 3 统一对象模板

所有 L1 / L2 / L3 对象共用同一套四块，不每节另发明骨架：

| 块 | 写什么 | 不写什么 |
|---|---|---|
| **定义** | 编码、层级、业务问题、极性、值域、粒度与主键、市场、边界（不回答什么） | 决策理由、历史版本 |
| **输入** | 上游**接口**：表名、字段、编码清单、频率、`market_scope`；**字典种子**（参数写入数据库的物化形态） | 上游内部怎么算、数据源考据、**参数定义** |
| **计算模型** | ① **原理与背景**：为什么用这个模型、解决什么问题、业务与统计依据、不采用的替代方案及原因；② **指标计算方法（简述）**：用自然语言 + 核心公式把「怎么算」压缩到 2–5 行；③ **本环节参数**：逐个写清名称 / 值 / 生效步骤 / 作用 / 取值依据与变更后果；④ 公式或伪代码（可照抄的落地形态）；⑤ 门禁与落库规则（引用 §2.4） | 变更编年、评审过程、项目排期 |
| **输出** | 目标表 + 一行完整 JSON 样例 + 字段字典引用（§2.3）+ 给下游的一句话接口 | 下游怎么用、UI 怎么展示 |

「原理与背景」写的是**方法本身的依据**（为什么这样算、为什么不用另一种算法），**不是变更的理由**（为什么从上一版改成这一版）——后者归附录 B ADR。伪代码不能替代原理：只给代码，读者能照抄却无法判断模型在边界条件下是否仍成立。

三者的分工：**简述抓要点**（读完能复述算法，不必看代码）、**原理给依据**、**伪代码给实现细节**。简述不得写成伪代码的逐行翻译；伪代码也不得省略简述里的核心公式。

**参数定义写在它生效的计算环节里**，每个参数须交代六件事：**名称、值（含是否待标定）、生效步骤**（公式第几步 / 伪代码哪一行）、**作用**（改变什么、不改变什么）、**取值依据**、**变更后果**（触发哪个 `params_hash`、重算范围见 §2.2）。

⚠ **字典种子不是参数定义**：`dimension_dict` / `business_dict` 的一行 JSON 只给出「键 → 值」，是参数落库的物化形态；只写种子而不写上述六项，等于没有定义。通用参数定义见 §2.4 对应规则，对象章节只引用名字、不重复抄数值。

通用规则的原理集中在 §2.4，逐对象的可用性细则放在「输出」的 NULL 语义里，验收用例集中在 §7。

---

## 4 L1 原子指标

**定义**：一个可观测量的原始值及其 as-of 历史分位。约 123 个，其中 54 个进入六维合成，其余为宏观 / 配置层背景量。粒度一市场一日一指标一行，主键 `(indicator_code, market, trade_date)`。

**输入**：`regime.macro_series`（宏观序列）、`public.daily_benchmark`（国债与基准指数）、`a_daily_quote` / `hk_daily_quote`（行情与估值字段）、`regime.sector_*`（行业快照）、`regime.ipo_event` / `fund_issuance_event` / `unlock_schedule` / `insider_trade` / `analyst_forecast_snapshot`（事件类）。逐指标接线见 `indicator_dict.source` 与 `market_state_daily.py` 的映射表。

**计算模型**

- **原理**：把「原始值」与「它在自身历史中的相对位置」放在同一行，是为了让上层只消费一个可比的 0–100 量。原子指标量纲差异极大（成交额、比率、天数、bp、分类象限），分位是唯一能跨指标比较的公共刻度；若让每个维度各自重算历史，同一天的同一指标会出现多套分位。
- **为什么本层不做方向调整**：`percentile` 是**观测值的排位**，与「这个指标高是好是坏」无关；极性属于维度语义，若在此翻转，同一原子指标被两个维度引用时会产生相反读法。故方向只在 `indicator_dict.direction` 声明、在 L2 由 `members[].sign` 统一（§2.4.2）。
- **为什么设字段级门禁**：个股估值等字段在早期年份覆盖不全，「有值」不等于「样本有效」；有效字段数不足当日有效样本一半时置 NULL，避免用少数存活股票算出看似可用的全市场统计量。
- **为什么不取分位有例外**：`NO_PERCENTILE` 集合内是分类象限或已标准化过的量（如 `VAL.ERP_Z` 的 z-score），再取分位不产生新信息。其 `raw_value` 仍可消费（如 z 幅度写 L2 `flags`），但**不能挂到读 `percentile` 的 `members[]` 上**——否则就是「分母算它、分子永不算」，恒定压低 `coverage`（§5.2 v1.2）。

**指标计算方法（简述）**

一句话：每个原子指标每天算两个数——`raw_value`（原始值）与 `percentile`（该值在截至当日自身历史样本中的排位，0–100）。

1. 按 `indicator_dict.formula` 从源表算出 `raw_value`。
2. 用截至 `trade_date` 的历史样本算排位，并列值取中位秩；窗口、`min_periods`、`NO_PERCENTILE` 等参数与 as-of 规则均定义在 §2.4.1（`winsor` 为未生效预留键）。
3. **本层不翻转方向**，`percentile` 保持观测值排位；`NO_PERCENTILE` 集合内的指标跳过第 2 步。
4. 字段覆盖门禁不通过时，`raw_value` 与 `percentile` 一并置 NULL，写一行 `indicator_value`。

本层没有额外分支逻辑，以上四步即完整计算。

**输出**：

```json
{
  "indicator_code": "VAL.PE_TTM_MEDIAN",
  "market": "CN",
  "trade_date": "2026-11-04",
  "raw_value": 32.640000,
  "percentile": 63.40,
  "extra": {"n_sample": 2143, "survivorship_bias": true, "quality": "low"},
  "model_version": "b7c9d0e1"
}
```

下游接口：L2 只读 `(indicator_code, market, trade_date, percentile)`；信号族也直接读本层分位。

---

## 5 L2 维度指标

**六维总览（章首索引）**

> §5.x 编号只分配给按 §3 统一模板写出的维度对象，并与 `market_profile.md` §5 对齐：§5.1 宽度 / §5.2 估值 / §5.3 情绪 / §5.4 资金 / §5.5 压力 / §5.6 趋势（均为生效规格）；六维均已按模板写出并落地，本表只作横向索引，不占编号。

| 维度名称 | 维度指标编码 | 业务问题 | 极性（高分=） | 成员数 | 聚合结构 | 有效市场 | 给 L3 的角色 |
|---|---|---|---|---|---|---|---|
| 宽度 Breadth | `DIM.BREADTH` | 涨势是否广泛 | 越广（描述型） | 11（HK 10） | 分族取代表 → 族间等权 | CN+HK | 方向置信度确认（不改状态） |
| 估值 Valuation | `DIM.VALUATION` | 贵不贵 | 越贵 | 6（HK 5） | PE 分位单锚 + 校验 | CN+HK | 描述型 + 反转信号；风险预算可选约束（默认关闭） |
| 情绪 Sentiment | `DIM.SENTIMENT` | 人在追还是怕（意愿） | 越亢奋 | 7（3 核心 + 4 `flags_only` 诊断） | 固定核心 3 → 2 块等权；HK 单成分代理 | CN 2019 起 / HK 2001 起（代理） | 解释维度（内部读数，恒不出档） |
| 资金 Flow | `DIM.FLOW` | 真金白银净流（能力） | 越流入 | 8（2 核心 + 6 `flags_only` 诊断） | L1 20 日累计标准化 → 按市场单锚 | CN 2020 起 / HK 2010 起（均单锚代理） | 解释维度（内部读数，恒不出档） |
| 压力 Stress | `DIM.STRESS` | 系统稳不稳 | 越危险 | 9（8 核心 + 1 诊断） | 4 块两级等权 | CN+HK | 系统性压力主判据、预算约束（经 `SYSTEMIC_STRESS.level`） |
| 趋势 Trend | `DIM.TREND` | 指数往哪走 / 有多陡 | 越强（描述型） | 3（标准 L1 原子） | σ 归一化强度单锚（t 作 raw、expanding 分位作 score） | CN+HK | 方向状态归 `BIZ.MARKET_REGIME`；`score` 兼作 L3 强度门槛与输出（§6.1 v2），不参与方向合成 |

六维不共用一套聚合算法，各自有独立生产者，按 `dimension_code` 分派；扁平等权只在成员同质时可用，否则权重会退化成「写了几个指标」。

### 5.1 宽度 Breadth · `DIM.BREADTH`

#### 定义

**一句话定位**：`DIM.BREADTH` 回答「上涨的参与面有多广——多少股票在涨、涨势是否广泛」。

| 规格项 | 值 |
|---|---|
| 编码 / 层级 | `DIM.BREADTH` / L2 维度指标 |
| 业务问题 | 涨势是否广泛 |
| 极性 | `polarity = high_good`：高分 = 参与面越广；**描述型刻度，不等于「越好 / 越安全」** |
| 值域 | `score` 0–100（分位读数）、`state` 五档（标签见「本环节参数」） |
| 频率 / 粒度 | 日频落库；主键 `(dimension_code, market, trade_date)` |
| 市场 | `CN` / `HK`；两侧分位基数与横截面口径不同，**禁止横比** |

**边界（不回答什么）**：① 指数自身涨跌与趋势（趋势维 / `BIZ.MARKET_REGIME`）；② 个股平均相关性与同步下跌（压力维）；③ 行业参与广度（`SECTOR.UP_RATIO`，第④层）；④ 成交额**水平**（情绪维，本维度只保留「成交额集中度」这类结构量）。

> ⚠ **两条最易读错的地方**：① `high_good` 只是描述刻度——极广既可能是健康普涨，也可能是过热见顶（`%>MA200` 高位回落正是顶部信号族之一），依据见「计算模型」原理 3；② 宽度能揭示「指数是否由少数股撑起」，但与指数**共享当日横截面价格信息、不构成独立证据**，在 L3 的角色见「输出」。

#### 输入

来源表 `regime.indicator_value`，字段 `(indicator_code, market, trade_date, percentile)`；`direction` 取自 `regime.indicator_dict`，是 `members[].sign` 的唯一依据（§2.4 规则 2：极性只在本层翻转一次）。

| 指标名称 / 成员编码 | 角色（`block` / `representative`） | `direction` | 对齐 `sign` | 源 / 计算 | 频率 | CN 起点 | HK 起点 | `market_scope` |
|---|---|---|---|---|---|---|---|---|
| 上涨家数占比<br>`BREADTH.ADV_RATIO` | 涨跌家数族 · **代表** | `high_good` | `+1` | `adv / n × 100`，`n` = 当日有行情且过成交门槛的股票数 | 日 | 1990-12-20 | 2000-01-03 | `CN+HK` |
| 涨跌家数归一化净广度<br>`BREADTH.ADV_MINUS_DEC` | 涨跌家数族 · 成员 | `high_good` | `+1` | `100 × (adv − dec)/(adv + dec)`（A/D Line 的日增量，只存增量；`extra` 存 `adv/dec/n/unchanged` 供反查与审计） | 日 | 1990-12-20 | 2000-01-03 | `CN+HK` |
| 麦克莱林振荡<br>`BREADTH.MCCLELLAN` | 涨跌家数族 · 成员 | `high_good` | `+1` | `EMA_19(A) − EMA_39(A)`，`A` = 上项归一化净广度（ratio-adjusted + 简单 EMA） | 日 | 1990-12-20 | 2000-01-03 | `CN+HK` |
| 站上 MA60 占比<br>`BREADTH.PCT_ABOVE_MA60` | 均线族 · **代表** | `high_good` | `+1` | `close > MA_w` 家数 / 有 w 日以上历史的家数 × 100 | 日 | 1991-03 | 2000-03 | `CN+HK` |
| 站上 MA200 占比<br>`BREADTH.PCT_ABOVE_MA200` | 均线族 · 成员 | `high_good` | `+1` | 同上，w = 200 | 日 | 1991-10 | 2000-10 | `CN+HK` |
| 20 日新高−新低<br>`BREADTH.NEW_HIGH_LOW_20` | 新高新低族 · **代表** | `high_good` | `+1` | `(nh_w − nl_w)/ok_w × 100`，`ok_w` = 有 w 日以上历史的股票数 | 日 | 1991-01 | 2000-01 | `CN+HK` |
| 60 日新高−新低<br>`BREADTH.NEW_HIGH_LOW_60` | 新高新低族 · 成员 | `high_good` | `+1` | 同上，w = 60 | 日 | 1991-03 | 2000-03 | `CN+HK` |
| 涨跌力度比<br>`BREADTH.UP_DOWN_STRENGTH` | 结构·单成员族 · **代表** | `high_good` | `+1` | `avg(涨幅\|涨) / \|avg(跌幅\|跌)\|`，比值而非差值（差值受当日波动率驱动） | 日 | 1991-02 | 2000-01 | `CN+HK` |
| 涨跌停家数差<br>`BREADTH.LIMIT_NET` | 结构·单成员族 · **代表**（CN only） | `high_good` | `+1` | `(涨停 − 跌停)/n × 100`，主板 ±9.8 / 双创 ±19.6；制度起点 1996-12-16 | 日 | **1996-12-16**（制度起点；⚠ 库内仍有 1,505 行护栏前残留值，见「输入侧门禁」） | —（港股无涨跌停制度，非漏采） | `CN` |
| 成交额集中度<br>`BREADTH.AMT_CONC_TOP10` | `flags_only`（诊断，不进 `score`） | `high_risk` | `−1` | TOP 10% 个股成交额 / 全市场成交额 × 100（高 = 窄，须翻向） | 日 | 1990-12 | 2000-01 | `CN+HK` |
| 等权−市值加权 20 日收益差<br>`BREADTH.EQ_VS_CAP_20D` | `flags_only`（诊断，不进 `score`） | `high_good` | `+1` | `Σ20(等权日收益 − 市值加权日收益)`；CN 用 `circular_market_val`、HK 用 `total_market_val` | 日 | 2018-01-15（短样本，按 `n_sample` 降置信） | 2001-06 | `CN+HK` |

> ⚠ `SECTOR.UP_RATIO`（第④层）与 `BREADTH.AVG_CORR_VW`（压力维，编码前缀沿用；口径为方差加权而非简单平均）**不在 `members[]` 内**；A/D Line 与指数背离是派生量，不落表（见「输出」）。`coverage` 分母按 `members[].code` 联 `indicator_dict.market_scope` 计数，**HK 排除 CN-only 的 `LIMIT_NET`**（CN 11 个成员、HK 10 个）；不按 `scope` 或 `domain` 过滤。
> ⚠ 结构族四个成员性质互异（20 日带符号收益差 / 无符号集中度 / 当日力度比 / 制度性涨跌停净占比），且 `AMT_CONC_TOP10` 与其余三者极性相反——它不是同质族，标定后已拆为单成员族（原理 1）。

本维度专用参数（`state_bands` / `state_labels` / `flags_only` / `representative` / 标定判据组）的**定义见「计算模型」的「本环节参数」表**；通用参数（`min_periods` / `pct_window` / `winsor` / `coverage_min` / `dispersion_max` / `weight_cap`）定义在 §2.4，其中 `winsor` 当前为未生效预留键。下方是 `dimension_dict` 一行的**字典种子**——参数写入数据库的物化形态，**不是定义**；其中通用参数的取值与 §2.4 一致，不是第二处定义。种子真源为 `tools/calibrate_breadth.py --write` 的产物（族构成、代表与切点均由离线标定写入）。

**维度指标注册**  
对应表为 regime.dimension_dict  

```json
{
  "dimension_code": "DIM.BREADTH",
  "dimension_name": "宽度",
  "question": "上涨的参与面有多广（描述型，高分=越广，非越好）",
  "polarity": "high_good",
  "members": [
    {"code": "BREADTH.ADV_RATIO",         "sign":  1, "weight": 0.2, "block": "涨跌家数",              "representative": true,  "flags_only": false},
    {"code": "BREADTH.ADV_MINUS_DEC",     "sign":  1, "weight": 0.0, "block": "涨跌家数",              "representative": false, "flags_only": false},
    {"code": "BREADTH.MCCLELLAN",         "sign":  1, "weight": 0.0, "block": "涨跌家数",              "representative": false, "flags_only": false},
    {"code": "BREADTH.PCT_ABOVE_MA60",    "sign":  1, "weight": 0.2, "block": "均线",                 "representative": true,  "flags_only": false},
    {"code": "BREADTH.PCT_ABOVE_MA200",   "sign":  1, "weight": 0.0, "block": "均线",                 "representative": false, "flags_only": false},
    {"code": "BREADTH.NEW_HIGH_LOW_20",   "sign":  1, "weight": 0.2, "block": "新高新低",             "representative": true,  "flags_only": false},
    {"code": "BREADTH.NEW_HIGH_LOW_60",   "sign":  1, "weight": 0.0, "block": "新高新低",             "representative": false, "flags_only": false},
    {"code": "BREADTH.UP_DOWN_STRENGTH",  "sign":  1, "weight": 0.2, "block": "结构·UP_DOWN_STRENGTH", "representative": true,  "flags_only": false},
    {"code": "BREADTH.LIMIT_NET",         "sign":  1, "weight": 0.2, "block": "结构·LIMIT_NET",        "representative": true,  "flags_only": false},
    {"code": "BREADTH.AMT_CONC_TOP10",    "sign": -1, "weight": 0.0, "block": "诊断",                 "representative": false, "flags_only": true},
    {"code": "BREADTH.EQ_VS_CAP_20D",     "sign":  1, "weight": 0.0, "block": "诊断",                 "representative": false, "flags_only": true}
  ],
  "state_bands": {"CN": [31.20, 46.43, 59.67, 71.90], "HK": [30.19, 44.46, 55.87, 68.49]},
  "state_labels": ["极窄", "偏窄", "中性", "偏广", "极广"],
  "params": {"winsor": [2, 98], "coverage_min": 0.6, "dispersion_max": 30,
             "weight_cap": 0.25, "pct_window": 2520,
             "calib_start": "1996-01-01", "calib_end": "2025-12-31",
             "min_intra_corr": 0.5, "min_pc1": 0.6, "min_decade_corr": 0.3,
             "min_construct_corr": 0.4,
             "flags_only": ["BREADTH.AMT_CONC_TOP10", "BREADTH.EQ_VS_CAP_20D"],
             "rep_coverage_min": 1.0, "flag_gap": 15.0, "flag_extreme": 20.0,
             "quality_sample": 750, "min_periods": 250,
             "contrib_sign_split": true, "flags_shape": "dict",
             "dispersion_scope": "non_flags_only",
             "calib_version": "v2.1.12"},
  "params_hash": "4852d3274f07f359",
  "is_active": true
}
```

> 只有 `representative = true` 且非 `flags_only` 的成员进 `score`，其余 `weight = 0`；`flags_only` 成员仍计入 `coverage` / `contrib` / `flags`，但**不计入 `dispersion`**（方案 C，v2.1.12）。代表由 CN 标定、**两市场共用**（字典一行无法按市场存不同代表），HK 自动少一个 `LIMIT_NET` 族。⚠ `params.winsor` 是未生效预留键（§2.4 规则 1）；本种子口径与库内一致（`calib_version = v2.1.12`、`params_hash = 4852d3274f07f359`）。`rep_coverage_min = 1.0` 已登记并生效；v2.1.11 另登记 `flag_gap` / `flag_extreme`（纳入 `params_hash`）、`quality_sample` / `min_periods`（短样本降置信）、`contrib_sign_split`（`contrib` 按 delta 符号分组）、`flags_shape = "dict"` / `dispersion_scope = "non_flags_only"`（方案 C），详见附录 C。

**输入侧数据可用性门禁（L1 层）**：

- **横截面分母**：CN `n` = 当日 `change_pct IS NOT NULL` 且剔除 B 股；HK `n` = `close/prev_close` 非空、过 `HK_MIN_AMOUNT = 100000` 港元仙股门槛且 `prev_close ≠ 0`；均线 / 新高新低类另以「有 w 日以上历史」的 `ok_w` 为分母（各成员覆盖率不同）。停牌 / 新股 / 无成交靠 SQL 与 `ok_w` 隐式过滤，**无显式标志位**。⚠ 字段 100% 覆盖 ≠ 有效样本满覆盖：HK 过仙股门槛后分母本身是时变的，这是与 A/D 归一化同源的非平稳来源。
- **两市场涨跌判定不同源**：CN 用 `change_pct`；HK 的 `ADV_RATIO` / `UP_DOWN_STRENGTH` 用 `(close/prev_close − 1)`，而 `AVG_CORR_VW` / 等权收益用 `change_pct`。同一日两市场的「上涨」定义不完全一致。
- **制度起点**：`LIMIT_EFFECTIVE_FROM = 1996-12-16`，此前无涨跌停制度、用涨跌幅阈值判定会把早期大幅波动误记为涨跌停，故 `LIMIT_NET` 置 NaN；三个制度断点（1996-12-16 / 2020-08-24 / 2023-02-17）写入 `indicator_dict.breaks`，断点前后分位不可比。⚠ ST（±5%）无带生效日期的历史标志位，属**数据源缺口**而非代码缺口。✅ **2026-11-05 已清理**：1,505 行护栏前残留已 DELETE，宽度族按 `tools/recalc_breadth_percentile.py` 全量重算分位并递增 `model_version`（`BREADTH_CALIB_VERSION = v2.1.11`）；「置 NaN」现已是库内现状。
- **幸存者偏差**：`PIT_UNIVERSE_READY = False` —— 历史按当前成分股回填，已退市个股基本不在池内（按「是否仍更新」实测 CN 停更 20/5,711、HK 12/2,843；停更占比极低只说明该判据**无法量化**早年偏差，不构成「偏差严重」的证据；偏差存在由外部退市常识佐证，该判据识别不出早年已消失的标的）。偏差是**时变的、随年份衰减、方向随行情状态变化**：下跌日宽度比例被高估、早期年份最大。故所有宽度类分位写 `extra.survivorship_bias = true` / `extra.quality = 'low'`，**禁止用于跨年代水位比较，也禁止单独作为档位标定依据**；切点标定样本须报告按年代分层的档位占比。解除 = 口径变更：接入含退市股的全历史源、按 `trade_date` 重建 PIT 成分池后，递增 `model_version` 并按 §2.4 规则 4/5 全量重算。
- 上述门禁都定义在**输入侧**、由数据口径决定而非标定决定，故不进 `dimension_dict.params` / `params_hash`；但 `HK_MIN_AMOUNT` / `LIMIT_EFFECTIVE_FROM` 一旦变更会改动 `raw_value` 与参考样本集，须按 §2.2 递增版本并全量重算 L1 + L2。

**数据覆盖事实（逐指标起点见上表；HK 回填后的跨族约束）**：

| 实测项 | 结果 | 后果 |
|---|---|---|
| HK 宽度字段起点 | 2026-10-24 由 2019 回填至 **2000-01**（`change_pct` / `amount` 在 2000 年即满字段覆盖） | 宽度类分位基数不再差一个数量级；⚠ 字段非空 ≠ 有效样本满覆盖（须过仙股门槛） |
| 估值字段（PE / PB / 破净） | HK 实测 **PE/ERP 2001-09-06、PB/破净 2001-08-06**（`indicator_value` 首次非空日，已核定）；字段覆盖门禁 2002 起达 96% | 字段级门禁：有效字段数 < 当日样本一半即置 NaN；字典 remark 的「HK 2019」为旧值，已同步 |
| 市值加权 `EQ_VS_CAP_20D` | CN 2018-01-15 / HK 2001-06 | `total_market_val` 2000 年仅 6% 覆盖；CN 属短样本，按 `n_sample` 降置信 |
| 恒指点位 `HK.800000` | **2013-08-20** 起，未回填 | A/D 与指数背离在 2013-08 前不可算，不得用回填的行情起点冒充可算区间 |
| 成交额集中度 | 近一年 A 股 52~68（均值 59）、港股 86~93（均值 90） | 港股小盘股流动性差、集中度天然远高 → 两侧只能各自纵向看分位，不得横比 |
| `LIMIT_NET` | CN **制度起点** 1996-12-16；HK 无此制度 | 原「1990-12 起」在制度上不成立；✅ 1996 前 1,505 行护栏前残留已于 2026-11-05 清理并重算分位 |

→ **HK 回填只解除宽度类的字段覆盖限制**，估值 / 市值加权 / 恒指族仍各受其源约束；合成分按成员覆盖门禁逐层放开，不能从「宽度 2000 年起」外推为「所有指标 2000 年起」。

#### 计算模型

**原理与背景**

1. **为什么是「分族 → 每族 1 个代表 → 族间等权」而不是扁平等权**：11 个成员里 `ADV_RATIO` / `ADV_MINUS_DEC` / `MCCLELLAN` 是同一横截面现象的水平 / 增量 / 动量三种读法，MA60 与 MA200、新高 20 与 60 同理；扁平等权会让权重退化成「这一维写了几个指标」。族内固定取 1 个代表（按 `0.6 × 历史长度 + 0.4 × |PC1 载荷|` 的**加权和**取最高，写入字典并纳入 `params_hash`）保证可复现。族划分用**一阶差分**相关矩阵（水平值相关会被共同趋势污染产生伪高相关）+ PCA 第一主成分 + 年代分层稳定性三条判据，与 §2.4 的「剔除冗余成员」门槛是两条不同规则；三个同质族通过（相关 0.59~0.93 / PC1 0.79~0.96），结构族不通过（约 0.10 / 0.45）→ 拆为单成员族，不得当同质族取代表。⚠ 上述族划分 / 代表打分**只读标定样本区间**（见「本环节参数」），样本外年份不参与标定。
2. **为什么有 `flags_only`**：`EQ_VS_CAP_20D` / `AMT_CONC_TOP10` 与「核心宽度分」的**同期相关**（内部一致性，两市场取最小值）过低，属「少数权重股撑指数」的结构诊断而非参与广度；它们移出 `score`，但**仍计入 `coverage` / `dispersion` / `contrib` / `flags`**——否则出档门禁的分母会被悄悄缩小，把低覆盖日伪装成满分。
3. **为什么极性只在 L2 翻转一次、且 `high_good` 只是描述刻度**：`percentile` 是观测值排位，与好坏无关（§2.4 规则 2）；`AMT_CONC_TOP10` 的 `direction = high_risk`（高 = 窄）按 `sign = −1` 取 `100 − percentile`。在宽度维里翻向后的语义是「高分 = 广」，不是「高分 = 高风险」；目标极性以 `polarity = high_good` 为准，消费端不得二次翻转。⚠ **它仍不是价值判断**：极广既可能是健康普涨，也可能是过热见顶（`%>MA200` 高位回落正是顶部信号族之一），不得把高分读作利好 / 安全；「高 = 广 = 热」的单调先验尚未检验，见「输出」下游接口。
4. **为什么宽度分位用滚动 10 年、其余维度 expanding**：制度变迁（注册制 / 涨跌幅规则）使旧样本可比性下降，且幸存者偏差是时变的——`expanding` 会把 1990 年代不可信的早期水位永久留在参考分布里；按**有效观测行数**计窗口可避免停牌 / 节假日造成的长度漂移。窗口参数见 §2.4 规则 1 / ADR B-4。
5. **为什么 A/D 用归一化净广度、MCC 用 ratio-adjusted + 简单 EMA(19/39)**：`adv − dec` 是绝对家数差，CN 股票池从 1990 年不足十只扩到 5,711 只（跨 2~3 个数量级），累计线与分位会读成「年份」的函数；归一化后 `A_s ∈ [−100, 100]`、无量纲，在**分母口径稳定**的前提下才跨年代可比（分母 `adv+dec` 排除平盘、随停牌比例变化，HK 涨跌判定还与 CN 不同源），原始家数写 `extra` 不丢。19 / 39 日对应的 α ≈ 10% / 5% 是**平滑常数**，与 ratio-adjusted（净额除以总成交家数）是两件不同的事，两者都要保留。
6. **为什么按市场等频冻结五档、绝对读数不参与出档**：两侧制度、粒度、流动性结构不同（集中度 A 股均值 59 / 港股 90），美股经验阈值（`>50% 上涨`、`PCT_ABOVE_MA200 < 15%`）在 A/H 未标定且受幸存者偏差污染，只能写 `flags` / tooltip 作展示诊断；出档一律走各市场自身 `score` 分布的 P20/P40/P60/P80，等值归较高档。切点由全历史标定并冻结，**存在参数层前视**：用它回算历史档位时历史标签并非当时可知，验证报告必须注明。
7. **为什么派生量不落表**：`SI` 是 `MCC` 的 `cumsum`、背离是规则判定，同一事实不存两份。真正的约束是增量序列**一经落库即为终值**，修订必须走 `params_hash` + `model_version` 全量重算、不得静默覆盖；在此前提下「不落表」只是存储选择，不是防漂移手段，消费方按**固定版本**重算（而非每次请求实时计算）。

**指标计算方法（简述）**

核心公式：`score = Σ_r w_r · p_r / Σ_r w_r`，其中 `p_r = percentile_r`（`sign = −1` 时取 `100 − percentile_r`），`r` 遍历有效族代表，`w = min(1/n_eff, weight_cap)` 后按有效代表归一化。

0. 读当日 11 个成员的 `percentile`，按 `members[].sign` 对齐到「高分 = 广」。
1. 按 `members[].block` 分族，跳过 `flags_only` 与 `market_scope` 不覆盖本市场的成员（HK 自动少 `LIMIT_NET` 族），每族取 `representative = true` 的 1 个代表。
2. **只有代表进分数**：按 `w` 加权，缺失代表按有效权重重归一化（`min_count = 1`），结果裁剪到 0–100；其余成员 `weight = 0`。
3. 校验 / 诊断成员只做三件事：计入 `coverage`、写 `contrib`（正负各 top3）、写 `flags`（结构性背离标记）；**不改 `score`**。⚠ **方案 C（v2.1.12）**：`dispersion` 排除 `flags_only` 诊断成员，只按参与合成的成员算 —— 它们本就不进 `score`，用它们判断「平均分是否具代表性」是逻辑错配（阶段 3 对照实验：HK 未出档率由 12.61% 降到 6.27%，档位稳定性几乎不变）。
4. `coverage = valid / expected`（分母按 `market_scope` 覆盖本市场的成员计数），`dispersion_norm = dispersion(core) × sqrt(core_valid / 10)`；另单独算 `rep_coverage = 有效代表数 / 应有代表数`（`coverage` 分母含非代表成员，不能替代）；`confidence` 按「门禁通过且 `coverage ≥ 0.8`、`dispersion_norm ≤ 20` → 高 / 门禁通过但非高 → 中 / 未通过 → 低」三档，再按规则 6 的短样本判定压到低。
5. `state` 按 `state_bands[market]` 切五档，**仅在** `score` 非 NULL、`coverage ≥ coverage_min`、`dispersion_norm ≤ dispersion_max`、`rep_coverage ≥ rep_coverage_min` 时输出，否则 `state = NULL`，只留 `score` + 低置信（§2.4.3）；短样本降置信已接入（只降置信，不改 `score` / `state`）。

本层没有额外分支逻辑，以上五步即完整计算。

**本环节参数**（逐个对应上面的生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `state_bands` | CN `[31.20, 46.43, 59.67, 71.90]`<br>HK `[30.19, 44.46, 55.87, 68.49]` | 第 5 步；`digitize(score, state_bands[market])` | **把 0–100 的 `score` 切成五档写入 `state`**；只改档位，**不改变 `score` 数值**；等值归较高档 | 各市场 1996-01-01~2025-12-31 标定样本的 P20/P40/P60/P80，按市场冻结；「样本内每档 20.0%」是**等频切点的定义结果，不是标定有效性的验证结论**。⚠ 参数层前视（原理 6）；变更触发 `params_hash` 与 L2 全量重算 |
| `state_labels` | 极窄 / 偏窄 / 中性 / 偏广 / 极广 | 第 5 步，随 `state` 一起落库 | **决定 `state` 列写入哪个文字标签**；只影响标签文字，不改切点、不改 `score` | 数据字典枚举值，不是 UI 文案；与 `state_bands` 同步冻结 |
| `representative` / `block` | 5 个有效族各 1 个代表（CN；HK 4 个），见字典种子 | 第 1–2 步 | **决定哪些成员进 `score`、以及族间等权的粒度**；只改 `score` 与 `blocks`，不改单个 `percentile` | 一阶差分相关中位 ≥ `min_intra_corr`、PC1 ≥ `min_pc1`、年代分层相关 ≥ `min_decade_corr` 三条判据；代表按历史长度 × PC1 载荷打分。写入字典并纳入 `params_hash`，变更触发 L2 全量重算 |
| `flags_only` | `{BREADTH.AMT_CONC_TOP10, BREADTH.EQ_VS_CAP_20D}` | 第 1、3 步 | **把成员移出 `score`，但保留在 `coverage` / `dispersion` / `contrib` / `flags`**；改变 `score`，不改变成员是否入库 | **同期内部一致性**（不是构念效度：无外部效标 / 前瞻检验）：与核心宽度分同期相关（两市场取最小）< `min_construct_corr = 0.4`；仅检验 4 个结构族成员、不检验 3 个通过族代表。变更触发 `params_hash` 与 L2 重算 |
| `rep_coverage_min` | 1.0（**已启用**） | 第 5 步，出档判定 | **要求全部族代表都有值才出档**；只改 `state`，不改 `score` / `coverage` | 回归 2026-11-05 实测：13,926 个出档日中 336 天缺 1 个代表仍出档、其中 98 天 `confidence=高`。字典已登记该键并全量重算 L2；生产者缺省 0.0 仅为兼容旧种子 |
| 标定判据组与样本区间 | `min_intra_corr = 0.5` / `min_pc1 = 0.6` / `min_decade_corr = 0.3` / `min_construct_corr = 0.4`；`calib_start = 1996-01-01`、`calib_end = 2025-12-31` | **离线标定**（`tools/calibrate_breadth.py --write`），不在日频计算中生效 | 决定族构成、代表、切点；**不直接改日频输出**，经字典种子与 `params_hash` 传导 | ⚠ **窗口对全部标定环节生效**：族划分 / 代表 / 同期内部一致性剔除 / 切点一律只读 `[calib_start, calib_end]`；2026 起仅用于 `--probe` 样本外占比报告，不参与任何标定决策（2026-11-05 修前视）。判据与窗口由回归测试钉住；变更须重标并重算 L2 |
| `FLAG_GAP` / `FLAG_EXTREME` | 15 / 20 | 第 3 步的 `flags` 判据 | 快慢窗分位差 >15、极端分位 <20 时写结构性背离文案；**不改 `score` / `state`** | ✅ v2.1.11 已登记 `dimension_dict.params.flag_gap` / `flag_extreme`，纳入 `params_hash`；模块常量降级为缺省回退 |

> 通用参数 `min_periods` / `pct_window` / `winsor` / `coverage_min` / `dispersion_max` / `weight_cap` 定义在 §2.4 规则 1、3，本环节只引用名字、不重复数值（`winsor` 为未生效预留键）；`weight_cap` 对 CN 5 族（`1/5 = 0.2`）与 HK 4 族（`1/4 = 0.25`）**都不改变权重**，当前仅是有名无实的护栏。
> `HK_MIN_AMOUNT` / `LIMIT_EFFECTIVE_FROM` / `PIT_UNIVERSE_READY` 是 **L1 输入侧门禁**（定义在「输入」段），不是 L2 合成参数，不进 `dimension_dict.params` / `params_hash`。
> ✅ v2.1.11 已把 §2.4 规则 6 的 `n_sample < 750` 接入 `confidence`（只降置信、不改 `score` / `state`，并写 `flags.short_sample`）：`n_sample` 取**族代表**里最小的 L1 `extra.n_sample`（不用全体成员，避免晚起点的 `flags_only` 诊断成员把整维拖成短样本）。`quality_sample = 750` 已登记字典，缺省 0 时关闭以隔离新维度。

**落地形态**（原理确定后的实现）：

```python
for market, trade_date in calendar:
    ind = read(indicator_value, market, trade_date, members[].code)
    aligned = {c: (100.0 - p) if sign[c] == -1 else p
               for c, p in ind.percentile.items() if p is not None}

    eligible = [c for c in members[].code
                if market_scope[c] in (market, 'CN+HK', 'GLOBAL')]
    valid    = count(aligned WHERE c IN eligible)
    coverage = valid / len(eligible)
    dispersion_norm = std(aligned[core].values()) * sqrt(core_valid / 10)  # 方案 C：core = eligible 排除 flags_only

    # 族 → 每族固定 1 个代表 → 族间等权
    reps = [m.code for m in members
            if m.representative and not m.flags_only
            and market_scope[m.code] in (market, 'CN+HK', 'GLOBAL')]
    n_eff = len(reps)
    w = min(1.0 / n_eff, weight_cap) if n_eff else 0.0
    num = sum(w * aligned[r] for r in reps if r in aligned)
    den = sum(w for r in reps if r in aligned)
    score = clip(num / den, 0, 100) if den else None      # 缺失代表按有效权重重归一化
    rep_coverage = count(aligned[r] for r in reps if r in aligned) / max(n_eff, 1)

    ok = (score IS NOT NULL and coverage >= coverage_min
          and dispersion_norm <= dispersion_max
          and rep_coverage >= rep_coverage_min)           # 短样本降置信见 §2.4 规则 6，尚未接入
    confidence = '低'
    if ok:
        confidence = '高' if coverage >= 0.8 and dispersion_norm <= 20 else '中'
    state = state_labels[digitize(score, state_bands[market])] if ok else NULL

    write_one_row(dimension_value, PK=(dimension_code, market, trade_date),
                  score=score, state=state, coverage=coverage,
                  dispersion=..., blocks=每族{score, coverage, representative},
                  contrib=正负各 top3, flags=结构性背离标记,
                  confidence=confidence,
                  model_version=dimension_dict.params_hash)
```

出档门禁见 §2.4.3；PIT、幂等、重算粒度、版本化见 §2.4.4–5（**参考样本集变更的第二类全量重算触发见 §2.2 / 规则 4**：宽度是 rolling 窗口，历史回补仍会改变窗口内排位，不得只补新日期）。落库须用 `skip_null_updates=False`——`state = NULL` 必须能覆盖旧档，否则门禁不通过时会残留上一次的结论。`is_final` 列已于 v2.1.12 从三张值表删除（零消费方、T+1 流程从未实现）；口径新旧由 `model_version` 判定。`dimension_value` **没有 `quality` / `extra` 列**，可用性一律用 `confidence` + `flags` 承载，样本质量标在 L1 的 `indicator_value.extra`。

**宽度口径约束（输入侧偏差的传导）**：`PIT_UNIVERSE_READY = False` 期间，宽度分位带 `survivorship_bias = true` / `quality = 'low'`，绝对读数只作展示 / 诊断不参与出档，相对变化只可作 20/60 日**短窗**内的定性参考（若偏差随年份单调衰减，多年期趋势本身也被污染）。两市场构成 / 分母不同的指标（集中度、行业广度、涨跌力度比）一律各自纵向看分位、不得横向比数值。解除需两步：① 接含退市股的全历史源、重建 PIT 成分池并把 `PIT_UNIVERSE_READY` 置 `True`；② 重做族 / 切点标定、按 §2.4 规则 4 递增 `model_version` 并全量重算。在此之前，**解除字段覆盖限制 ≠ 解除口径约束**。

#### 输出

目标表 `regime.dimension_value`，字段字典见 §2.3.3。下例为库内真实一行（CN · 2026-11-03）：

```json
{
  "dimension_code": "DIM.BREADTH",
  "market": "CN",
  "trade_date": "2026-11-03",
  "score": 19.26,
  "state": "极窄",
  "coverage": 1.0000,
  "dispersion": 14.54,
  "blocks": {"涨跌家数": {"score": 39.74, "coverage": 1.0, "representative": "BREADTH.ADV_RATIO"},
             "均线": {"score": 8.63, "coverage": 1.0, "representative": "BREADTH.PCT_ABOVE_MA60"},
             "新高新低": {"score": 18.39, "coverage": 1.0, "representative": "BREADTH.NEW_HIGH_LOW_20"},
             "结构·UP_DOWN_STRENGTH": {"score": 3.23, "coverage": 1.0, "representative": "BREADTH.UP_DOWN_STRENGTH"},
             "结构·LIMIT_NET": {"score": 26.29, "coverage": 1.0, "representative": "BREADTH.LIMIT_NET"}},
  "contrib": {"pos": [{"code": "BREADTH.ADV_RATIO", "delta": 20.48},
                      {"code": "BREADTH.ADV_MINUS_DEC", "delta": 19.41},
                      {"code": "BREADTH.MCCLELLAN", "delta": 13.10}],
              "neg": [{"code": "BREADTH.UP_DOWN_STRENGTH", "delta": -16.03},
                      {"code": "BREADTH.PCT_ABOVE_MA200", "delta": -14.80},
                      {"code": "BREADTH.PCT_ABOVE_MA60", "delta": -10.63}]},
  "flags": {"n_sample": 2520, "structural": ["权重股掩盖多数股票下跌", "成交额集中度偏高"],
            "dispersion_norm": 13.79},
  "confidence": "高",
  "model_version": "4852d3274f07f359"
}
```

`score = 19.26 < CN 切点 P20 = 31.20` → 第 1 档「极窄」；`coverage = 1.0`（11/11）、`dispersion_norm = 14.54 × sqrt(9/10) ≈ 13.79 ≤ 30`（分母 9 = 11 成员 − 2 个 `flags_only`） → 出档且 `confidence = 高`。该行是 v2.1.11 口径；`flags` 为 dict（§2.3.3），`n_sample` 取族代表的最小有效观测数。

未出档差异：`state = null`、`confidence = "低"`，`score` 与 `blocks` 照出；`state = null` 只能读作「**未出档**」，不是「中性」（§2.4.6）。

> ✅ v2.1.11 起 `flags` 形状已在 §2.3.3 固定为 dict、写进 `dimension_dict.remark` 并由 `tests/test_breadth_v2111.py` 钉住；`blocks` 每族额外带 `representative`。下游解析以本节与 §2.3.3 为准，**不得再按字符串数组遍历**。

**派生量（不落表，由消费方按固定 `model_version` 重算）**：

| 派生量 | 公式 / 判据 | 为什么不落表 / 使用边界 |
|---|---|---|
| 麦克莱林累加 `SI` | `SI_t = Σ_{s≤t} MCC_s`，`SI_0 = 0` **固定** | 就是振荡的 `cumsum`，同一事实不存两份；水平可跨版本比较，但常用读法只看方向与相对穿越（与 20 日均线金叉 / 死叉、N 日变化符号），**不用「上穿 0 / 1000」**（起点固定后该判据无意义） |
| A/D Line 与指数背离 | 双高点比较：指数出现两个依次抬高的局部高点（间隔 ≥20 交易日，高点 = N 日窗口内最高，N 默认 60），A/D 在对应高点的**去趋势相对位置** `A_rel = (AD − MA_N(AD)) / σ_N(AD)` 依次降低；底背离对称。输出方向 / 幅度（两个 A/D 高点差 ÷ A/D 自身 60 日标准差，z 单位）/ 持续天数 / `flags.benchmark_mismatch` / `flags.sample_start` | 背离是**规则判定**而非一个数，N / 比较窗口 / 持续天数属消费方口径选择。基准须与宽度「同池」：CN 优先全 A 等权，用上证综指时须标 `benchmark_mismatch = true` 并降置信；HK 恒指仅约 80 只成分、与全港池错配，且 `HK.800000` 仅 2013-08-20 起 → **2013-08 前不输出**。⚠ 归一化 A/D 与指数点位不可直接相减或画在同一坐标轴；比较绝对水位会被 cumsum 的长期趋势支配，故用去趋势相对位置 |

> 增量序列一经落库即为终值，修订必须走 `params_hash` + `model_version` 全量重算，不得静默覆盖；消费方按固定版本拉取增量序列，不按「实时请求」口径取数。

下游接口（只声明数据字段，不规定展示）：`BIZ.MARKET_REGIME` 读 `state` 写 `detail.breadth_confirmation`（**仅描述，不调置信、不改状态**；§6.1 v2。其增量价值须经前瞻检验，未通过前不得表述为「互相印证」或「指数说谎」）；`BIZ.MARKET_TEMPERATURE` 读 `score` 的 as-of 分位 `u_i`，且「高 = 广 = 热」只是未经验证的展示标签；`SIG.REVERSAL` 宽度族（A/D 背离、`%>MA200` 高位回落 / 低位回升）**直接读 L1 原子 `percentile`**，不读本维度分；外部消费方读表时用 `model_version` 判口径。

---

### 5.2 估值 Valuation · `DIM.VALUATION`

#### 定义

回答「全市场价格相对盈利、净资产与现金回报处在自身历史的什么位置——贵还是便宜」。`polarity = high_risk`，高分 = 越贵；`score` 为 0–100 分位读数；月–季慢变量、日频落库；粒度 `(dimension_code, market, trade_date)`，`market ∈ {CN, HK}`，两侧分位基数不同禁止横比。不回答股 vs 债值不值（第②层）、往哪走、能上多少仓位。

#### 输入

来源表 `regime.indicator_value`，字段 `(indicator_code, market, trade_date, percentile)`。

| 指标名称 / 成员编码 | 角色 | `direction` | 对齐 `sign` | 源 / 计算 | 频率 | CN 起点 | HK 起点 | `market_scope` |
|---|---|---|---|---|---|---|---|---|
| 全市场 PE 中位数<br>`VAL.PE_TTM_MEDIAN` | **锚** | `high_risk` | `+1` | `a_daily_quote` / `hk_daily_quote` 的 `pe_ttm_ratio` 横截面中位数（`pe>0`） | 日 | **2018-01-02**（锚生效起点；字段 1991 起有回填但不可用，见「数据覆盖事实」） | **2001-09-06**（`indicator_value` 实测，已核定） | `CN+HK` |
| 全市场 PB 中位数<br>`VAL.PB_MEDIAN` | 校验 | `high_risk` | `+1` | 同上 `pb_ratio` | 日 | 2018-01-02（PB 无回填） | **2001-08-06** | `CN+HK` |
| 破净股占比<br>`VAL.PCT_BELOW_PB_1` | 校验 | `high_good` | `−1` | `count(pb<1)/count(pb>0)` | 日 | 2018-01-02（同上） | **2001-08-06** | `CN+HK` |
| 股息利差<br>`VAL.DIV_SPREAD` | 校验 | `high_good` | `−1` | 股息率 − 10Y 国债 | CN 日 / HK 月 | 2006-03 | **2000-01-03**（月频源 `HK.DIV_SPREAD` 1997-12-31 起 347 点） | `CN+HK` |
| 股权风险溢价 ERP<br>`VAL.ERP` | 校验 | `high_good` | `−1` | `1/PE中位数 − 10Y`（CN 中债 / HK 美债） | 日 | 2018-01-02 | **2001-09-06** | `CN+HK` |
| ERP 标准化偏离<br>`VAL.ERP_Z` | **参考（不进合成）** | `high_good` | — | ERP 扩展窗口 z-score，**不取分位**（`NO_PERCENTILE`）；走 `raw_value` 的 z 幅度通道，无 0–100 刻度 | 日 | ~2019 | **2002-09-16** | `CN+HK` |
| 巴菲特指标<br>`VAL.BUFFETT` | 校验 | `high_risk` | `+1` | `CN.MKT_CAP_TOTAL`（沪+深）/ GDP_ttm | 周频降采样 | 2022-02 | — | `CN` |
| ERP（乐咕月频口径）<br>`VAL.ERP_LG` | 参考（不进合成） | `high_good` | — | 乐咕全 A PE 中位数（月频）− 中债 10Y | 月 | 2006-03 | — | `CN` |

本维度专用参数（`quality_sample`、`check_divergence`、`state_bands`、`state_labels`）的**定义见「计算模型」的「本环节参数」表**；通用参数（`min_periods`、`coverage_min`、`dispersion_max`、`weight_cap`、`winsor`）定义在 §2.4 对应规则，其中 `winsor` 当前为未生效预留键。下方是 `dimension_dict` 的一行**字典种子**——这些参数写入数据库的物化形态，**不是定义**；其中通用参数的取值与 §2.4 一致，不是第二处定义。

**维度指标注册**  
对应表为 regime.dimension_dict   
```json
{
  "dimension_code": "DIM.VALUATION",
  "dimension_name": "估值",
  "question": "贵不贵",
  "polarity": "high_risk",
  "members": [
    {"code": "VAL.PE_TTM_MEDIAN",  "sign":  1, "weight": 1.0, "block": "anchor"},
    {"code": "VAL.PB_MEDIAN",      "sign":  1, "weight": 0.0, "block": "check"},
    {"code": "VAL.PCT_BELOW_PB_1", "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.DIV_SPREAD",     "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.ERP",            "sign": -1, "weight": 0.0, "block": "check"},
    {"code": "VAL.BUFFETT",        "sign":  1, "weight": 0.0, "block": "check"}
  ],
  "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
  "state_labels": ["极便宜", "偏便宜", "中性", "偏贵", "极贵"],
  "params": {"winsor": [2, 98], "coverage_min": 0.6, "dispersion_max": 30,
             "weight_cap": 0.25, "quality_sample": 750, "check_divergence": 20},
  "params_hash": "<口径指纹>",
  "is_active": true
}
```

> 只有锚参与 `score`，其余 `weight = 0`。`VAL.ERP_LG`、`VAL.ERP_Z` 与两条 `SECTOR.*` 不在 `members[]` 内（前两者离线/幅度参考、后两者属第④层）；`coverage` 分母按 `members[].code` 联 `indicator_dict.market_scope` 计数，不按 `scope` 或 `domain` 过滤。
> `state_bands` 必须是**按市场的 dict**（生产者读 `dim["state_bands"][market]`，与 `DIM.BREADTH` 一致）；上方 `[20,40,60,80]` 仅占位，生效值由 `tools/calibrate_valuation.py --write` 按各市场 `score` 分布的 P20/P40/P60/P80 冻结并纳入 `params_hash`。`blocks` / `flags` 形状由 `single_anchor` 分支定死并写入 `dimension_dict.remark`（§2.3.3 的两处待固定口径）。
> ⚠ **`VAL.ERP_Z` 为何不进 `members[]`**（v1.2，2026-11-05）：它是 z-score，`percentile` 按 §2.4 规则 1 恒为 NULL，而 `members[]` 的唯一消费方式是读 `percentile`（第 1 步 / 伪代码的 `aligned`）。留在数组里不会算错 `score`（`weight = 0`），但会**分母算它、分子永远不算**从而恒定压低 `coverage`（原 CN ≤6/7≈0.857、2022 前 ≤5/7≈0.714、HK ≤5/6≈0.833），且 `sign=-1` 与「校验项与锚相差 >20」判据全部失效、一个互补信号被静默丢掉。其 `raw_value` 仍保留，供 L2 写 `flags`（如 z 幅度极值，**阈值待标定**）与离线分析（**须单独读取 `raw_value`，不经过伪代码按 `members[].code` 的 SELECT**）；不进 `score` / `coverage` / `dispersion`。移出后 `expected` = CN 6 / HK 5，门禁恢复余量。

**输入侧数据可用性门禁（L1 层，2026-11-05 新增）**：`VALUATION_ASOF_START = 2018-01-02` —— CN 的 4 个派生指标 `VAL.PE_TTM_MEDIAN` / `VAL.PB_MEDIAN` / `VAL.PCT_BELOW_PB_1` / `VAL.ERP` 只从该日起产出；2018 前的 baostock 回填值不进 `raw_value`、不进 `expanding` 分位参考分布、也不进 `VAL.ERP_Z` 的扩展均值/标准差。另设字段级覆盖门禁（估值字段有效个股数 < 当日有行情家数一半 → 置 NaN，对齐 HK 侧既有护栏）：回填段已被日期护栏整体置 NaN，它是**第二道防御**，防止将来解除护栏时混入低覆盖样本。实现见 `market_state_daily.py` 的 `_valuation_before_mask` / `_valuation_asof_guard` / `calc_cn` / `_write_market`。
>
> - 该门禁**不是** `dimension_dict.params` 的合成参数（不是标定量，是数据口径判定），故不写入下方字典种子、不进 `params_hash`。
> - **两层门禁都只在回填段生效** → 对 2018+ 逐值零影响，按 §2.2 末注属「无数值影响」，**不触发** `params_hash` / `model_version` 变更与历史全量重算。
> - **解除门禁 = 口径变更**：须先解决数据口径，再递增版本并按 §2.4 规则 4/5 全量重算。
> - `VAL.DIV_SPREAD` / `VAL.ERP_LG` / `VAL.BUFFETT` 是乐咕 / 总市值等独立口径，不受此限。
> - ⚠ **2018+ 仍有偶发的字段级覆盖塌陷**（实测 2026-10-26~29 `pe>0` 有效占比仅 0.37~0.38，同期 PB 0.55），属**采集侧既有缺口、与本次回填无关**。本次刻意不让门禁作用于 2018+：若顺带把这几日置 NaN，库内旧值因 `skip_null_updates=True` 不会被清除，反而造成新旧混存。该问题须单独做清库 + 口径版本，已登记附录 D 待办。

**数据覆盖事实（2026-11-05 实测复核，替代 v1.0 的「2017 全年 0 条」）**：`backfill_a_valuation_baostock.py` 已把 CN `pe_ttm_ratio` 从 2018 前推到 1991（baostock 源，与东财同为「总市值 / 归母净利润TTM」口径，5 只票 973 个重叠交易日比值中位 1.0000），但**这段数据不能进日频估值锚的参考分布**：

| 实测项 | 结果 | 后果 |
|---|---|---|
| **QFQ 缩放** | 该列 `pe_qfq = pe_raw × (QFQ close / 未复权 close)`，复权因子随除权累积单调衰减；年度 PE 中位数 1991=0.12、1995=3.40、2000=19.08、2007=33.87、2015=37.73、2018=29.70 | 早年 PE 被系统性压低，同列水平值跨期不可比；`expanding` 分位会把历史段全判「极便宜」，并把 2018+ 顶到接近 100 分位 |
| **字段覆盖率** | 1991 39.9% → 1995 97.4% → 2005 30.9% → 2015 14.9% → **2017 11.8%** | 早期「全市场 PE 中位数」实为几百只票；字段级门禁单独挡不住 1994–96（覆盖 87–97% 却只有 3–5 倍 PE） |
| **成分池拼接** | 2017-12-29 有效 PE **302/3050** 只、pe_med 27.48；2018-01-02 **2984/3250** 只、pe_med 29.70 | 一夜扩大 10 倍、水平跳变 +8%；现有 `_coverage_gate` 只看行情家数 `n`，检测不到字段级塌陷 |
| **PB** | 2018 前 `pb_ratio` 全空（baostock `pbMRQ` 与东财 `PB_MRQ` 在财报披露窗口有 5%~9% 系统偏差，刻意不补） | 两个 PB 系校验项仍 2018 起；历史段 `coverage` 更低 |
| **L1 现状** | `indicator_value` 的 PE / PB / ERP 仍全部 2018-01-02 起（各 2143 行） | 生产读数尚未被污染；但 `--backfill` 的数据起点是 CN 1990，一旦全量重算即会引入 → 输入层护栏必须先于重算落地 |

→ **数据缺口从「没有」变成「有但不可比」**：不再是样本长度问题，而是口径一致性 + 成分池可比性问题。HK 侧起点**已按 `indicator_value` 首次非空日核定**（2026-11-05）：PE / ERP `2001-09-06`、PB / 破净 `2001-08-06`、DIV_SPREAD `2000-01-03`、ERP_Z `2002-09-16`，约 25 年样本（6189 行）远长于 CN 的 8 年 → **HK 具备单独出档的数据条件**，是否启用由 L2 标定与 v2.1.11 ⑥ 三口径对照实验决定。

#### 计算模型

**原理与背景**

1. **为什么是「单锚 + 校验」而不是等权平均**：6 个成员概念同源（PE / PB / ERP / 破净占比本质都是「价格相对盈利与净资产的位置」），等权会把同一份信息计多次；扁平等权还会让权重退化成「这一维写了几个指标」。单锚把职责拆开：一个读数负责定档，其余只负责提示背离与调置信度，因此校验项 `weight = 0`。`VAL.ERP_Z` 虽同源，但按 §2.4 规则 1 无 `percentile`、走 z 幅度通道，故不列入这 6 个成员（见输入段注）。
2. **为什么锚选 PE 中位数分位**：
   - 横截面中位数抗极端值（小盘股 PE 动辄数百），代价是丢掉「权重股贵不贵」的信息——这是明确的口径边界。
   - 统计依据（`market_profile.md` §4.1.1 实测）：`corr(E/P, rf) = −0.214`，盈利端分布区制平稳；`corr(ERP, rf) = −0.801`，ERP 受无风险利率区制驱动。本维度回答「股票自身贵不贵」，若用 ERP 作锚，债息下行会机械抬高读数，把「债券更不值」误读成「股票便宜」。股债性价比由第②层用 ERP 承担，两层不重算。
   - 由此 `sign = +1`：PE 分位高即贵，与维度极性一致，无需翻转，也无需利率区制校正。
3. **为什么分位窗口用 expanding**：估值均值回归周期 10–20 年，滚动窗口会把上一轮极端估值移出样本，使同一绝对水位在不同时点得到不同分位。§4.1.1 的窗口实测为滚动 10 年 92.8 / 滚动 20 年 95.1 / 全样本 95.2——顶端分位对窗口长度不敏感，但滚动会截断周期极值（10 年窗口的「最贵」仅 −1.64，全样本 −2.81）。因此不靠遗忘旧样本来处理利率漂移。
   ⚠ **两个前提须复核（2026-11-05）**：① 上述实测出自 `market_profile.md` §4.1.1 的 **252 个月频 ERP 观测**（含无风险利率），不是本维度的日频 PE 中位数分位——用它给 `DIM.VALUATION` 的 expanding 背书属口径错配，样本基数变化后须按日频 PE 重做窗口实测；② expanding 假设「同一绝对水位跨期可比」，而 CN 回填段是 QFQ 缩放值、且成分池在 2018-01-02 断裂（见「数据覆盖事实」），假设不成立。故在口径修正前，expanding 的参考样本仍以 2018-01-02 为起点，不纳入回填段。
4. **为什么有出档门禁**：通则见 §2.4.3。对本维度还有一层特殊理由——锚是单条序列，校验项与锚显著背离恰恰说明「此刻的贵贱判断不稳定」，此时给出五档结论比只留一个低置信分数更容易误导。
5. **为什么 CN 仍不出档（2026-11-05 改写理由）**：v1.0 的理由是「锚仅 2018 起、约 8 年，属数据**物理缺口**」。baostock 回填后该前提已被推翻，但**结论不变、理由整体更换**：① 回填段是 QFQ 缩放值，与 2018+ 不同质，直接拼进 expanding 参考分布会同时毁掉历史段与当下读数（水平跳变 + 伪趋势，与 `VAL.ERP_LG` 刻意不拼接属同类问题）；② 成分池在拼接点断裂（302 → 2984 只），PB / 破净等校验项 2018 前无源，6 个成员在历史段仅 1–2 个有效，`coverage` 约 0.17–0.33 < `coverage_min = 0.6`；③ `state_bands` / `check_divergence` 仍是未标定占位值，样本基数从 8 年变 28 年后必须重标。→ **「解除短样本约束」≠「能出档」**：先过数据口径门禁，再过 `coverage` / `dispersion` 门禁。HK 起点已核定为 2001-09-06（约 25 年），数据条件具备，可单独启用出档。
6. **为什么锚缺失不回落**：回落到 `VAL.ERP` 会把「绝对估值」静默换成「股债性价比」，语义改变而消费端无感知；宁可不出分，也不要一个口径已变却看起来正常的数。2018 前被护栏截断的锚同样按 `no_data` 处理，不回落到回填段，也不回落到其它成员。

**指标计算方法（简述）**

核心公式：`score = VAL.PE_TTM_MEDIAN.percentile`（`sign = +1`，PE 分位高即贵，不翻转）。

0. **先过数据可用性门禁（L1 输入侧）**：CN 在 `VALUATION_ASOF_START` 之前不产出 4 个派生估值指标，锚缺失即 `no_data`，**不回落回填段、不回落其它成员**（原理 6）。此门禁对 2018+ 逐值零影响，按 §2.2 末注不触发 `params_hash` / `model_version` 变更。
1. 读当日 6 个成员（HK 5 个，按 `market_scope` 排除 `VAL.BUFFETT`）的 `percentile`，按 `members[].sign` 对齐到「高分 = 贵」。
2. **只有锚进分数**：`score` 直接取锚的对齐分位；其余 5 个成员 `weight = 0`，不参与任何平均。
3. 校验项只做两件事：与锚相差超过阈值且分处两端时把 `confidence` 降一级；把正负贡献写 `contrib`、可用性写 `flags`。
4. `state` 按 `state_bands` 切五档，**仅在** `n_sample ≥ min_periods`、`coverage ≥ coverage_min`、`dispersion_norm ≤ dispersion_max`、且该市场已通过数据可用性门禁（口径一致 + 成分池可比 + `state_bands` 已标定）时输出；否则 `state = NULL`，只留 `score` + 低置信。门槛分两层，**解除短样本约束不等于自动出档**。

**本环节参数**（逐个对应上面的生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `quality_sample` | 750 | 第 3 步；伪代码 `if n_sample < ...` | `n_sample` 不足时把 `confidence` 降为「低」；**只降置信，不改 `score` / `state`** | = 3 × `min_periods`；调大会让更多历史行标低置信，须按 §2.2 重算 L2 |
| `check_divergence` | 20 | 第 3 步；伪代码的校验项判据 | 校验项与锚相差 >20 且分处 ≥60 / ≤40 时 `confidence` 降一级，多项冲突取最保守；**不改 `score`** | 阈值**尚未标定**，属待验证参数；调小会更频繁降置信，须重标并重算 |
| `state_bands` | `{"CN":[..], "HK":[..]}`（占位，按市场冻结） | 第 4 步；`digitize(score, state_bands[market])` | **把 0–100 的 `score` 切成五档写入 `state`**；只改变档位，**不改变 `score` 数值** | 默认值仅占位，生效值须按 §8.1.1 标定后冻结；变更触发 `params_hash` 与全量重算 |
| `state_labels` | 极便宜 / 偏便宜 / 中性 / 偏贵 / 极贵 | 第 4 步，随 `state` 一起落库 | **决定 `state` 列写入哪个文字标签**；只影响标签文字，不改变切点、也不改变 `score` | 数据字典枚举值，不是 UI 文案；与 `state_bands` 同步冻结 |

> 通用参数 `min_periods` / `coverage_min` / `dispersion_max` / `winsor`（未生效预留键）定义在 §2.4 规则 1、3，本环节只在第 4 步与伪代码里引用名字，不在此重复数值。`weight_cap` 对 `single_anchor` 结构不生效（见 §2.4）。
> `VALUATION_ASOF_START` 是 **L1 输入侧门禁**（定义在「输入」段），不是本环节的 L2 合成参数：它由数据口径决定、不由标定决定，故不进 `dimension_dict.params` / `params_hash`。

**落地形态**（原理确定后的实现）：

```python
for market, trade_date in calendar:
    # 第 0 步：L1 输入侧门禁（CN 2018 前不产出派生估值指标）
    if market == 'CN' and trade_date < VALUATION_ASOF_START:
        continue          # 锚 = no_data，不回落回填段 / 其它成员（原理 6）
    ind = read(indicator_value, market, trade_date, members[].code)
    if ind['VAL.PE_TTM_MEDIAN'].percentile is None:
        write no_data(dimension_value, market, trade_date); continue

    expected = count(members[].code JOIN indicator_dict
                     WHERE market_scope IN (market, 'CN+HK', 'GLOBAL'))
    valid    = count(ind WHERE percentile IS NOT NULL)
    coverage = valid / expected

    score = ind['VAL.PE_TTM_MEDIAN'].percentile * sign(+1)   # = PE 分位，高分 = 贵

    aligned = {c: (100.0 - p) if sign[c] == -1 else p
               for c, p in ind.percentile.items() if p is not None}
    dispersion_norm = std(aligned.values()) * sqrt(valid / 10)

    confidence = '高'
    if n_sample < quality_sample: confidence = '低'
    if 校验项与锚相差 > check_divergence 且分处 >=60 / <=40:
        confidence = 降一级(confidence)                        # 多项冲突取最保守

    state = band_of(score, state_bands) if (
        score IS NOT NULL and n_sample >= min_periods
        and coverage >= coverage_min
        and dispersion_norm <= dispersion_max
        and 数据可用性门禁已通过（口径一致 / 成分池可比 / state_bands 已标定）
    ) else NULL

    write_one_row(dimension_value, PK=(dimension_code, market, trade_date))
```

出档门禁见 §2.4.3；PIT、幂等、重算粒度、版本化见 §2.4.4–5（**参考样本集变更的第二类全量重算触发见 §2.2 / 规则 4**）。锚缺失即 `no_data`，**不回落其它成员、也不回落回填段**。校验项只写 `contrib` / `confidence` / `flags`，不改 `score`。

**CN 口径约束（v1.1 改写，替代原「CN 短样本约束」）**：锚生效起点仍为 2018-01-02，但理由已从「物理缺口」改为「回填段口径断裂 + 成分池不可比」（见「数据覆盖事实」与原理 5）。**输出不变**——`state` 恒 NULL、`confidence = 低`、`flags.short_sample = true`、`score` 照出但不等于可出档——但 `short_sample` 应读作「**锚的有效样本仍只有 2018 起的一段**」，不表示库里没有更早的 PE。解除需两步：① 数据侧给出真实（未 QFQ 缩放）PE 列，或按 `VAL.ERP_LG` 先例另立独立参考序列，并补齐 PB 与退市股成分池；② 重做窗口实测、重标 `state_bands` / `check_divergence`、按 §2.4 规则 4 递增 `model_version` 并全量重算。HK 起点已核定（2001-09-06），本次 L2 落地后按标定结果单独启用出档。

#### 输出

目标表 `regime.dimension_value`，字段字典见 §2.3。

```json
{
  "dimension_code": "DIM.VALUATION",
  "market": "HK",
  "trade_date": "2026-11-04",
  "score": 63.40,
  "state": "偏贵",
  "coverage": 1.0000,
  "dispersion": 12.30,
  "blocks": {"anchor": {"code": "VAL.PE_TTM_MEDIAN", "percentile": 63.40},
             "check": {"n": 4, "divergent": false}},
  "contrib": {"pos": [{"code": "VAL.PB_MEDIAN", "delta": 8.2}],
              "neg": [{"code": "VAL.ERP", "delta": -5.1}]},
  "flags": {"n_sample": 5120, "anchor_window": "expanding"},
  "confidence": "高",
  "model_version": "a1b2c3d4"
}
```

CN 口径约束期的差异：`state = null`、`confidence = "低"`、`flags.short_sample = true`（含义见上：锚的有效样本仍只有 2018 起）。`VALUATION_ASOF_START` 之前该维度行**整体不产出**，不是「分数为 0 的中性」；下游须按缺行处理，不得填 50。

下游接口（只声明数据字段，不规定展示）：`BIZ.MARKET_TEMPERATURE` 读 `score` 的 as-of 分位 `u_i`（CN 的 `u_i` 分位基数同样只含 2018 起）；`BIZ.RISK_BUDGET` 读 `score` 原值（约束 `optional = true` 默认关闭，关闭时 `detail.missing = 'constraint_off'`）；`SIG.REVERSAL` 估值族直接读 L1 原子 `percentile`；外部消费方读表时用 `model_version` 判口径——**解除护栏后 `model_version` 必须递增**，否则消费方无法区分新旧分位基数。

---

### 5.3 情绪 Sentiment · `DIM.SENTIMENT`

#### 定义

**一句话定位**：`DIM.SENTIMENT` 回答「人愿不愿意参与」——成交行为与一级发行热度有多亢奋；**是内部描述量，不是交易或预测信号**。

| 规格项 | 值 |
|---|---|
| 编码 / 层级 | `DIM.SENTIMENT` / L2 维度指标 |
| 业务问题 | 人在追还是怕（**意愿**，非真金白银的净流） |
| 极性 | `polarity = high_risk`（只作**刻度方向标签**、不承载价值判断：高分 = 亢奋 / 过热侧）；**亢奋 ≠ 好，更不构成买卖依据** |
| 值域 | `score` 0–100（分位读数）；`state` 五档（标签见「本环节参数」），**当前恒 NULL** |
| 频率 / 粒度 | 日频落库；主键 `(dimension_code, market, trade_date)` |
| 市场 | `CN`（固定核心 3 成员，2019-01-10 起）/ `HK`（**单成分代理分**，2001-01-18 起）；两侧成员集与分位基数不同，**禁止横比、禁止拼接成一条曲线** |
| 给 L3 的角色 | 解释维度：**无 A 类决策消费者**，`state` 恒 NULL、`score` 不驱动任何 `cap` |

**边界（不回答什么）**：① 真金白银的净流（资金维）；② 波动率指数（压力维）、两融 / 产业资本 / 回购（资金维）；③ 亢奋与未来收益的关系（`SIG.REVERSAL` 顶部 / 底部信号，§6）；④ 指数往哪走（趋势维 / `BIZ.MARKET_REGIME`）；⑤ 股债性价比与宏观条件（第①②层）。

> ✅ **落地状态（v1.13 / 2026-11-06）**：字典行、生产者 `build_sentiment_dimension`、全历史读数均已落地；`params_hash = 71c3e5e253ccec1e`，CN 1895 行（2019-01-10 起）/ HK 6350 行（2001-01-18 起），`state` 全部 NULL，`model_version` 全部一致。规格采用**方案 A：固定核心成员集 + 冻结有效起点**（附录 C v1.13 / ADR B-9），以数据覆盖率为现实边界，不用低覆盖分数凑历史。

#### 输入

来源表 `regime.indicator_value`，字段 `(indicator_code, market, trade_date, percentile)`；`direction` 取自 `regime.indicator_dict`，`members[].sign` 是极性唯一真源（§2.4 规则 2）；归属以 `indicator_dict.domain` 为准，**不看编码前缀**（`FLOW.*` / `FORECAST.*` 是跨层复用）。

7 个成员全部**已采集并产出 L1**（库内实测，`percentile` 与 `raw_value` 均无缺行差异）。按方案 A 分成「固定核心 3 个」与「诊断 4 个」：

| 指标名称 / 成员编码 | 角色（`block`） | `sign` | 源 / 计算 | 频率 | CN 起点（行数） | HK 起点（行数） | `market_scope` |
|---|---|---|---|---|---|---|---|
| 市场换手率<br>`FLOW.TURNOVER_RATIO` | **核心** · 交易行为 | `+1` | `Σ成交额 / Σ流通市值(CN) 或 Σ总市值(HK) × 100` | 日 | **2018-01-02**（2144） | **2000-01-03**（6599） | `CN+HK` |
| 近 30 日 IPO 上市家数<br>`SENT.IPO_COUNT_30D` | **核心** · 一级供给 | `+1` | `count(list_date ∈ (t−30d, t])` | 日（30 日窗） | **2010-01-13**（4081） | —（无源） | `CN` |
| 近 90 日 IPO 首日破发率<br>`SENT.IPO_BREAK_RATE_90D` | **核心** · 一级供给 | `−1` | `count(首日收盘 < 发行价)/count(上市) × 100` | 日（90 日窗） | **2010-01-13**（3780，含 301 天 NULL） | —（无源） | `CN` |
| 近 60 日权益类基金成立份额<br>`SENT.FUND_ISSUANCE_60D` | 诊断 · `flags_only` | `+1` | `Σ issue_share(亿份)` | 日（60 日窗） | 2023-01-11（923） | — | `CN` |
| 近 60 日最大单只权益基金份额<br>`SENT.FUND_MAX_60D` | 诊断 · `flags_only` | `+1` | `max(issue_share)` | 日（60 日窗） | 2023-01-11（923） | — | `CN` |
| 评级乐观度<br>`FORECAST.RATING_BULL_PCT` | 诊断 · `flags_only` | `+1` | `(买入+增持)/五档合计 × 100` | 逐日快照 | 2026-09-30（22） | — | `CN` |
| 港股卖空成交占比<br>`FLOW.SHORT_SELLING_RATIO` | 诊断 · `flags_only` | `−1` | `Σ沽空金额 / 全市场成交额 × 100` | 日 | — | 2026-06-04（103） | `HK` |

> ⚠ **诊断成员不进 `score` / `dispersion`**，只写 `coverage` / `contrib` / `flags`（§5.1 方案 C）。依据：基金 2023 / 评级 2026 才加入，纳入会造成两次口径跳变；评级 `raw_value` 仅 99.23~99.35，分位把微小波动放大成伪信息；港股卖空仅 103 行、`n_eff < 250`。
> ⚠ `sign` 为**理论推定**：`IPO_BREAK_RATE_90D` 的 `−1` 与 `IPO_COUNT_30D` 同期相关仅 0.02，离线符号敏感性显示翻转后与全量分数秩相关 0.647 / 平均绝对差 12.04 —— 数据无法判定符号，故在 `remark` 标注「弱证据」，不作预测依据（ADR B-8）。
> ⚠ `market_profile.md` §5.3 写作 `SENT.RATING_BULL_PCT`，真实编码为 `FORECAST.RATING_BULL_PCT`（§2.1 已登记）。两个 `FLOW.*` 的 `domain` 仍归资金，属落地接线待办（附录 D.1）；**`domain` 不影响 `coverage` 分母**（分母按 `market_scope`）。

**冻结有效起点**（`tools/calibrate_sentiment.py` 按各成员 as-of 非 NULL 分位观测数离线标定后写入 `params`，纳入 `params_hash`）：

| 成员 | CN 满 250 | CN 满 750 | HK 满 250 | HK 满 750 |
|---|---|---|---|---|
| `FLOW.TURNOVER_RATIO` | **2019-01-10** | **2021-01-29** | **2001-01-18** | 2003-02-05 |
| `SENT.IPO_COUNT_30D` | 2011-01-24 | 2013-02-19 | — | — |
| `SENT.IPO_BREAK_RATE_90D` | 2011-01-24 | 2014-01-29 | — | — |
| 基金两条（诊断） | 2024-01-19 | 2026-02-12 | — | — |
| 评级 / 港股卖空（诊断） | 未达 250 | — | 未达 250 | — |

→ **CN 可发布起点 = 2019-01-10**（核心 3 全部达标），**`confidence` 可升级起点 = 2021-01-29**（核心 3 全部满 750）；**HK 代理分起点 = 2001-01-18**。冻结在字典而非动态读 L1 `extra.n_sample`（该键情绪族未落地），因此 PIT 安全、不触发 L1 重算。

本维度专用参数（`score_members` 由「非 `flags_only` 且覆盖本市场」推导、`member_valid_from` / `member_confident_from`、`core_coverage_min`、`dispersion_core_max`、`flag_gap`、`state_bands_calibrated`、`availability_tier_*`、`low_common_factor_pc1`）的**定义见「计算模型」的「本环节参数」表**；通用参数（`min_periods`、`coverage_min`、`dispersion_max`、`weight_cap`、`winsor`）定义在 §2.4，其中 `winsor` 为未生效预留键。下方是**库内现行 `dimension_dict` 一行**（`tools/calibrate_sentiment.py --write` 产物），是参数的物化形态、不是定义。

**维度指标注册（现行）**  
对应表为 regime.dimension_dict  

```json
{
  "dimension_code": "DIM.SENTIMENT",
  "dimension_name": "情绪",
  "question": "人在追还是怕（意愿；高分=越亢奋，描述型，内部读数）",
  "polarity": "high_risk",
  "members": [
    {"code": "FLOW.TURNOVER_RATIO",       "sign":  1, "weight": 0.50, "block": "交易行为"},
    {"code": "SENT.IPO_COUNT_30D",        "sign":  1, "weight": 0.25, "block": "一级供给"},
    {"code": "SENT.IPO_BREAK_RATE_90D",   "sign": -1, "weight": 0.25, "block": "一级供给"},
    {"code": "SENT.FUND_ISSUANCE_60D",    "sign":  1, "weight": 0.0,  "block": "诊断", "flags_only": true},
    {"code": "SENT.FUND_MAX_60D",         "sign":  1, "weight": 0.0,  "block": "诊断", "flags_only": true},
    {"code": "FORECAST.RATING_BULL_PCT",  "sign":  1, "weight": 0.0,  "block": "诊断", "flags_only": true},
    {"code": "FLOW.SHORT_SELLING_RATIO",  "sign": -1, "weight": 0.0,  "block": "诊断", "flags_only": true}
  ],
  "block_agg": {"交易行为": "mean", "一级供给": "mean"},
  "state_bands": {"CN": [20, 40, 60, 80], "HK": [20, 40, 60, 80]},
  "state_labels": ["极低迷", "偏低迷", "中性", "偏亢奋", "极亢奋"],
  "params": {
    "coverage_min": 0.6, "dispersion_max": 30, "dispersion_core_max": 30,
    "dispersion_scope": "core_only", "weight_cap": 0.25,
    "min_periods": 250, "quality_sample": 750, "winsor": [2, 98],
    "core_coverage_min": 1.0, "flag_gap": 15.0,
    "state_bands_calibrated": false, "flags_shape": "dict",
    "hk_proxy_member": "FLOW.TURNOVER_RATIO",
    "availability_tier_core": "core3_fixed", "availability_tier_hk": "hk_single_proxy",
    "low_common_factor_pc1": 0.298,
    "member_valid_from": {
      "CN": {"FLOW.TURNOVER_RATIO": "2019-01-10", "SENT.IPO_COUNT_30D": "2011-01-24",
             "SENT.IPO_BREAK_RATE_90D": "2011-01-24",
             "SENT.FUND_ISSUANCE_60D": "2024-01-19", "SENT.FUND_MAX_60D": "2024-01-19"},
      "HK": {"FLOW.TURNOVER_RATIO": "2001-01-18"}},
    "member_confident_from": {
      "CN": {"FLOW.TURNOVER_RATIO": "2021-01-29", "SENT.IPO_COUNT_30D": "2013-02-19",
             "SENT.IPO_BREAK_RATE_90D": "2014-01-29",
             "SENT.FUND_ISSUANCE_60D": "2026-02-12", "SENT.FUND_MAX_60D": "2026-02-12"},
      "HK": {"FLOW.TURNOVER_RATIO": "2003-02-05"}}
  },
  "params_hash": "71c3e5e253ccec1e",
  "is_active": true
}
```

> `weight` 是「2 块等权 × 块内等权」推导的记录值（换手 `1/2`、IPO 两条各 `1/4`、诊断 `0`）；**生产者按 `members[].block` 两级平均，不按 `weight` 单层加权**。`coverage` 分母按全体 `members[].code` 联 `indicator_dict.market_scope` 计数（**CN 6 / HK 2**）、只作描述量；**产分门禁改用 `flags.core_coverage`**（固定核心集全有效）。`state_bands` 是按市场 dict 的占位值，`state_bands_calibrated = false` 期间全市场不出档；标定后改 `true` 会触发 `params_hash` 与 L2 全量重算。`remark` 已写明方案 A、PC1 0.298 限制与「禁止跨市场横比」。

**输入侧数据可用性门禁（L1 层）**：

- **权益类基金筛选（强制）**：`SENT.FUND_ISSUANCE_60D` / `SENT.FUND_MAX_60D` 只统计 `fund_type ∈ {股票型, 混合型-偏股}`（计算层按「不含债 / 固收 / 稳健 / 货币」实现）；债基高峰常出现在避险升温时，与股票情绪反向。筛选条件写进 `indicator_dict.formula`，不得由实现各自决定（附录 D.1 待办）。
- **`issue_share` 空值按缺失、不按 0**；**窗口内无 IPO 时 `IPO_BREAK_RATE_90D` 分母为 0 → NULL**（2013-01-31~2015-12-08 共 301 天），不得填 0 / 50。
- **事件类历史靠逐日累积**：`regime.ipo_event` / `fund_issuance_event` 源只给当前列表，`analyst_forecast_snapshot` 是逐日快照，断档不可补；`_mask_before` 之前一律 NULL。
- **零膨胀与并列值**：`IPO_COUNT_30D` 零膨胀（实测 10.3% 为 0），30 / 60 / 90 日窗平滑零值、不得缩成单日；并列值取**中位秩**（§2.4 规则 1）。⚠ 重叠窗口使相邻日高度自相关，`dispersion` 会被系统性压低，故本维用**核心成员原始 std**（不归一化）并配合 `core_coverage` 门禁，不套 `sqrt(n/10)`。
- 上述门禁由数据口径决定、不进 `dimension_dict.params`；筛选条件 / 空值 / 源起点变更会改动 `raw_value` 与参考样本集，须按 §2.2 递增版本并全量重算 L1 + L2。⚠ **`member_valid_from` / `member_confident_from` 是 L2 参数**（冻结在字典、纳入 `params_hash`），不属输入侧门禁。

**数据覆盖事实与可发布区间**（库内实测 + 本次计算结果）：

| 项 | CN | HK |
|---|---|---|
| L1 最长序列 | 2010-01-13 起 4081 天 | 2000-01-03 起 6599 天 |
| 核心 3 成员齐全 | 2018-01-02 起 | 不适用（HK 核心集仅换手 1 个） |
| **已产出 `dimension_value`** | **2019-01-10 ~ 2026-11-05，1895 行** | **2001-01-18 ~ 2026-11-05，6350 行**（单成分代理） |
| `confidence` 分布 | 低 1251 / 中 441 / 高 203 | 恒「低」6350 |
| `state` 五档 | 恒 NULL（0 行出档） | 恒 NULL（0 行出档） |
| `model_version` | 全部 = `71c3e5e253ccec1e` | 同左 |
| 未接入 / 未建 | 新增开户数（源 2023-08 停更）、Put/Call、投资者调查（无源）、BW 六成分中的封闭式基金折价率 / 新股发行占比 / IPO 首日涨幅（未建） | 港股 IPO / 基金 / 评级**无源**（`get_market_events.py` 只采 A 股、`calc_hk` 不产出 `SENT.*`） |

→ **2019-01-10 之前的 CN 不产出维度行**（不是「低覆盖分数」）：当时核心集未全部达到 250 个有效观测，分位本身不可信——实测核心成员前 250 个分位落在 `≥95|≤5` 的占比：换手 19.6%、破发率 20.8%（均匀分布应约 10%），冻结起点后分别降至 12.0% / 4.4%。这是「数据覆盖率是现实基础」的直接体现。

→ **HK 是单成分代理分，不是完整维度分**：`core` 在 HK 只有换手一个成员，`score` 直接取其对齐分位；写 `flags.single_block_proxy = true`、`availability_tier = "hk_single_proxy"`、`block_coverage = 0.5`、`missing_block = ["一级供给"]`、`dimension_completeness ≈ 0.2857`，恒 `state = NULL` + `confidence = 低`。⚠ **禁止与 CN 横比、禁止拼接成一条曲线**；`4/7` / `2/7` 是维度完整度，不是 `coverage`（旧表述更正见附录 C v1.11）。

#### 计算模型

**原理与背景**

1. **为什么固定核心成员集（方案 A）**：7 个成员的起点从 2010（IPO）到 2026（评级）跨越 16 年。若按「当日可用成员动态平均」，2010-2017 的分数是 2 个 IPO 成员、2018-2022 是 3 个、2023 后是 5 个 —— 同一条 `score` 曲线在不同年份代表不同构念，**跨期不可比**。因此只用 3 个从 2018-01-02 起全程有值的核心成员（换手 + IPO 数 + 破发率），全区间同一口径；这三个两两一阶差分相关仅 0.02~0.03，无冗余计权。代价是放弃 2019 前的历史与 2023 后的基金信息，换来可比的连续读数。
2. **为什么冻结 `member_valid_from` / `member_confident_from`**：`percentile` 非空 ≠ 可信。实测核心成员前 250 个分位落在 `≥95|≤5` 的占比：换手 19.6%、破发率 20.8%（均匀分布约 10%），短样本 expanding 分位明显退化。按 (market, code) 冻结「第 250 / 750 个有效观测日」写进 `params`，生产者只按字典日期判定 —— **PIT 安全、可复现、不依赖 L1 `extra.n_sample`**（情绪族该键未落地）、不触发 L1 重算。核心集未全部达标则**整行不产出**，不用低覆盖分数凑历史。
3. **为什么 4 个成员降为 `flags_only` 诊断**：① 基金 2023 / 评级 2026 才加入，纳入会造成两次口径跳变；② 评级 `raw_value` 仅 99.23~99.35，分位把微小波动放大成伪信息（实测 percentile std 26.34）；③ 港股卖空仅 103 行、`n_eff < 250`；④ 离线预检显示一级供给块 4 成员一阶差分相关中位 ≈0.045、PC1 解释率 0.298，远低于 §5.1 同质块判据（0.50 / 0.60），把它们与 IPO 混成一个块没有统计支撑。诊断成员仍写 `coverage` / `contrib` / `flags` 供下钻，不进 `score` / `dispersion`（§5.1 方案 C）。
4. **为什么两级块等权、且不套 `sqrt(n/10)`**：CN 核心集按数据生成机制分 2 块（交易行为 1 个 / 一级供给 2 个），块内等权、块间等权；成员数固定为 3，故 `dispersion` 用**核心原始 std** 配固定阈值 `dispersion_core_max = 30`，避免 §2.4 规则 3 的 `sqrt(n/10)` 在小 n 维度把门禁放宽到形同虚设（`n=3` 时因子仅 0.548）。`coverage` 保留 §2.3.3 原语义（全体 eligible，CN 6 / HK 2）但**只作描述量**；产分门禁改用 `flags.core_coverage`。
5. **为什么 HK 保留单成分代理分**：HK 核心集只有换手一个成员（IPO / 基金 / 评级均无港股源），`score` 直接取其对齐分位，标 `single_block_proxy` / `block_coverage = 0.5` / `block_incomplete`，恒 `state = NULL` + `confidence = 低`。保留它是为了让 HK 有一个可解释的连续读数，但**它不是完整维度分**，禁止与 CN 横比或拼接。
6. **为什么全时段不出档**：`state_bands_calibrated = false`（等频切点尚未标定），且核心成员共同样本仅 3.8 年、逐年均值漂移明显（2019 58.16 → 2023 37.96 → 2026 71.21），现在冻结切点会把某一 regimes 当常态。故只产 `score` + `confidence` + 限制标注，不给五档结论（§2.4.6：`state = NULL` 只能读作「未出档」）。
7. **已知限制（写进 `remark`，不得隐去）**：① 核心成员 PC1 解释率 0.298，无共同因子，等权平均主要是噪声混合；② 本项目无「情绪亢奋」外部效标，方向预测 AUC 0.44-0.54 近随机（研究稿结论），`sign` 的 `−1` 只是理论推定（符号敏感性：翻转后秩相关 0.647 / 平均绝对差 12.04）；③ 分位窗口按 §2.4 默认 `expanding`，情绪 / 资金「滚动 10 年」仍为 **ADR B-7 待裁决项**，窗口属 L1 按指标族登记、且 `FLOW.TURNOVER_RATIO` 跨维复用，不能按维度单设；④ 该 `score` 只能作内部描述量，**不得作强弱判断、预测依据或驱动任何 `cap`**。

**指标计算方法（简述）**

核心公式：`score = mean_b( mean_{c ∈ b, valid}( p_c ) )`，其中 `p_c = percentile_c`（`sign = −1` 时取 `100 − percentile_c`），`b` 遍历有效合成块；`members[].weight` 只是记录值，生产者按 `block` 两级平均。

0. 读 7 个成员 `percentile`，按 `members[].sign` 对齐到「高分 = 亢奋」（§2.4 规则 2）；按 `market_scope` 取 `eligible`（CN 6 / HK 2），核心集 `core` = 非 `flags_only` 且覆盖本市场者（CN 3 / HK 1）。
1. 按 `params.member_valid_from[market]` 冻结日期判定成员有效；`core_coverage = 有效核心数 / |core|`，**< `core_coverage_min = 1.0` 时整行不产出**。`coverage` 仍按全体 eligible 计，`flags.dimension_completeness = |eligible| / 7`（描述量）。
2. 按 `block` 两级平均得 `score`（CN 两块；HK 一块即代理分），裁剪到 0–100，不补 0 / 50；`dispersion_core` 只对 `core` 算原始 std（HK 单成员为 NULL）。
3. `confidence`：HK 恒「低」；CN 在 `member_confident_from` 全部达标前恒「低」，之后按 `dispersion_core ≤ 20` 且 `coverage ≥ 0.8` 升「高」、否则「中」；`dispersion_core > 30` 或 `score` 为 NULL 时保持「低」。
4. `state` 恒 NULL（`state_bands_calibrated = false`）；`flags` 写 `availability_tier` / `core_coverage` / `dimension_completeness` / `block_coverage` / `low_common_factor` / `member_valid_from` / `dispersion_core` / 可选标记；`blocks` 与 `contrib` 按 §2.3.3 形状落库。

本层没有额外分支逻辑，以上四步即完整计算。

**本环节参数**（逐个对应上面的生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `member_valid_from` | `{"CN":{换手 2019-01-10, IPO 两条 2011-01-24, 基金两条 2024-01-19}, "HK":{换手 2001-01-18}}` | 第 1 步，成员有效性判定 | **决定哪一日起该成员的分位才被采信**；未达标 / 未冻结的成员当日视为无效 → 核心集不全则整行不产出；改变 `score` 的可用区间，不改分位本身 | 按 (market, code) 取第 250 个非 NULL `percentile` 观测日离线冻结（原理 2）。**冻结在字典、纳入 `params_hash`**；变更触发 L2 全量重算，不触发 L1 重算 |
| `member_confident_from` | `{"CN":{换手 2021-01-29, IPO_COUNT 2013-02-19, IPO_BREAK 2014-01-29, 基金两条 2026-02-12}, "HK":{换手 2003-02-05}}` | 第 3 步，`confidence` 分档 | 核心集全部达标前 `confidence` 恒「低」、写 `flags.short_sample`；**只降置信，不改 `score` / `state`** | 取第 750 个观测日（= 3 × `min_periods`，§2.4 规则 6）；冻结口径同上。变更触发 L2 重算 |
| `core_coverage_min` | 1.0（**已启用**） | 第 1 步，产分门禁 | **要求固定核心集全部有效才产 `score`**；未达则整行不产出（不是低覆盖分数） | 与 §5.1 `rep_coverage_min` 同构，针对「构念缺成员」；与 `coverage`（全体成员描述量）职责分离。变更触发 L2 全量重算 |
| `dispersion_core_max` / `dispersion_scope` | 30 / `core_only` | 第 2–3 步 | **`dispersion` 只对核心集算原始 std（不做 `sqrt(n/10)` 归一化）**；> 30 时 `confidence` 保持低并写 `flags.high_dispersion` | 成员数固定为 3，归一化会放宽门禁（§2.4 规则 3 的小 n 限制）；重叠窗口成员高度自相关，原始 std 已偏小。变更触发 L2 重算 |
| `state_bands` / `state_bands_calibrated` | `{"CN":[20,40,60,80],"HK":[20,40,60,80]}`（占位）/ `false` | 第 4 步 | `state_bands` 把 `score` 切五档（只改档位、不改分数）；`false` 时**整维恒不出档** | 等频切点须按 2019-01-10 起固定成员集的 `score` 分布标定后冻结；核心样本仅 3.8 年、逐年漂移明显，本期不标定。改 `true` 触发 `params_hash` 与 L2 全量重算 |
| `state_labels` | 极低迷 / 偏低迷 / 中性 / 偏亢奋 / 极亢奋 | 第 4 步，随 `state` 落库 | 决定 `state` 的文字标签；只影响标签，不改切点 / `score` | 数据字典枚举值，与 `state_bands` 同步冻结。变更触发 L2 重算 |
| `flag_gap` | 15（**待重标**） | 第 3 步，`flags.structural` 判据 | 两块分位差 > 15 写结构性背离文案；**不改 `score` / `state`** | 沿用宽度族取值（§5.1），须按本维块间分布重标；变更触发 L2 重算 |
| `flags_shape` / `availability_tier_*` / `low_common_factor_pc1` | `dict` / `core3_fixed`·`hk_single_proxy` / `0.298` | 第 4 步，写 `flags` | 固定 `flags` 为 dict；写入可用性分层、维度完整度、块覆盖、`low_common_factor` 限制标注；不改 `score` | `0.298` 是离线预检实测 PC1 解释率；形状写入 `dimension_dict.remark` 并由 `tests/test_sentiment_dimension.py` 钉住。变更触发 L2 重算 |
| `hk_proxy_member` | `FLOW.TURNOVER_RATIO` | 第 2 步（HK 分支） | **声明 HK 单成分代理分取哪个成员**；HK 恒低置信 / 恒不出档 | HK 核心集只有换手一个成员（IPO / 基金 / 评级无港股源）。变更触发 L2 重算 |

> 通用参数 `min_periods` / `coverage_min` / `dispersion_max` / `weight_cap` / `winsor`（未生效预留键）定义在 §2.4，本环节只引用名字、不重复数值；`weight_cap` 在两级块平均下不生效（已在 `remark` 定死）。权益类筛选、空值缺失、事件源起点、重叠窗口自相关是 **L1 输入侧门禁 / 统计限制**（「输入」段），不进 `params_hash`。

**落地形态**（`build_dimension_value.py::build_sentiment_dimension`，v1.13 已落地）：

```python
for market, trade_date in calendar:
    p = read(indicator_value, market, trade_date, members[].code)
    aligned = {c: (100.0 - v) if sign[c] == -1 else v for c, v in p.items()}
    eligible = [c for c in members if market_scope[c] covers market]     # CN 6 / HK 2
    core     = [c for c in eligible if not flags_only[c]]                 # CN 3 / HK 1

    valid = (aligned[c] IS NOT NULL
             and trade_date >= member_valid_from[market][c])              # 冻结日期，PIT 安全
    core_coverage = count(core WHERE valid) / len(core)
    if core_coverage < core_coverage_min: continue                        # 整行不产出
    coverage = count(aligned WHERE c IN eligible) / len(eligible)          # 仅描述量

    blocks = 按 members[].block 对 core 分组，块内对有效成员取均值
    score  = clip(mean(有效块分), 0, 100)
    dispersion_core = std(核心有效成员分位, ddof=1) or NULL              # HK 单成员
    is_hk_proxy = (market == HK and len(core) == 1
                   and core[0] == hk_proxy_member)
    short = trade_date < max(member_confident_from[market][c] for c in core)
    confidence = ('低' if (is_hk_proxy or short
                          or dispersion_core > dispersion_core_max)
                  else ('高' if coverage >= 0.8 and dispersion_core <= 20
                        else '中'))
    state = NULL                                                          # 切点未标定

    flags = {availability_tier, core_coverage, dimension_completeness,
             block_coverage, low_common_factor, member_valid_from,
             dispersion_core,
             short_sample?, low_coverage?, high_dispersion?, structural?,
             single_block_proxy?(HK), missing_block?(HK), block_incomplete?(HK)}
    write_one_row(dimension_value, PK=(dimension_code, market, trade_date),
                  score=score, state=state, coverage=coverage,
                  dispersion=dispersion_core, blocks=..., contrib=..., flags=flags,
                  confidence=confidence, model_version=dimension_dict.params_hash)
```

出档门禁见 §2.4.3；**`state_bands_calibrated = false` 期间整维不出档**，标定后改 `true` 须触发 `params_hash` 与 L2 全量重算。PIT、幂等、重算粒度、版本化见 §2.4.4–5（**参考样本集变更的第二类全量重算触发见 §2.2 / 规则 4**：`expanding` 分位一旦历史回补，须按指标族全量重算，不得只补新日期）。落库须用 `skip_null_updates=False`——`state = NULL` 必须能覆盖旧档；`only_if_changed=True` 保证幂等。`dimension_value` 没有 `quality` / `extra` 列，可用性一律用 `confidence` + `flags` 承载（§2.3.3）。

生产者由 `build_all_dimensions()` 按 `dimension_code` 分派（不改现有宽度 / 估值生产者）；日频入口是 `market_state_daily.run()` 内的函数内 import，**常驻 scheduler 进程需重启后新生产者才自动接入**（本次已用 CLI 直接完成全历史计算）。全历史命令：`build_dimension_value.py --dim DIM.SENTIMENT --write --backfill`；字典由 `tools/calibrate_sentiment.py --write` 生成。契约由 `tests/test_sentiment_dimension.py`（11 项）钉住。

#### 输出

目标表 `regime.dimension_value`，字段字典见 §2.3.3。下例是**库内真实一行**（CN · 2026-11-05，v1.13 口径）：

```json
{
  "dimension_code": "DIM.SENTIMENT",
  "market": "CN",
  "trade_date": "2026-11-05",
  "score": 68.15,
  "state": null,
  "coverage": 1.0000,
  "dispersion": 29.14,
  "blocks": {"交易行为": {"score": 85.98, "coverage": 1.0},
             "一级供给": {"score": 50.32, "coverage": 1.0}},
  "contrib": {"pos": [{"code": "FLOW.TURNOVER_RATIO", "delta": 17.83},
                      {"code": "SENT.IPO_BREAK_RATE_90D", "delta": 2.78}],
              "neg": [{"code": "SENT.FUND_MAX_60D", "delta": -65.66},
                      {"code": "SENT.FUND_ISSUANCE_60D", "delta": -65.60},
                      {"code": "FORECAST.RATING_BULL_PCT", "delta": -47.70}]},
  "flags": {"availability_tier": "core3_fixed", "core_coverage": 1.0,
            "dimension_completeness": 0.8571, "block_coverage": 1.0,
            "low_common_factor": true, "dispersion_core": 29.14,
            "member_valid_from": {"FLOW.TURNOVER_RATIO": "2019-01-10",
                                  "SENT.IPO_COUNT_30D": "2011-01-24",
                                  "SENT.IPO_BREAK_RATE_90D": "2011-01-24"},
            "structural": ["交易行为与一级供给分位背离"]},
  "confidence": "中",
  "model_version": "71c3e5e253ccec1e"
}
```

`score = (85.98 + 50.32)/2 = 68.15`（两级块等权）；块间差 `35.66 > flag_gap = 15` → `flags.structural`；`dispersion_core = 29.14 ≤ 30` 但 `> 20` → `confidence = 中`；`state = null` 是**未出档**（切点未标定），不是「中性」（§2.4.6）。`contrib` 含诊断成员（基金 / 评级与核心分位背离较大），**只用于下钻、不进 `score`**；`flags.dimension_completeness = 6/7 ≈ 0.8571` 是描述量，不参与门禁。

HK 差异（同为库内真实行）：`score = 90.82`（换手单成分）、`state = null`、`confidence = "低"`、`dispersion = null`、`coverage = 1.0`、`blocks` 只有「交易行为」且带 `proxy: true`；`flags` 写 `availability_tier = "hk_single_proxy"`、`single_block_proxy = true`、`block_coverage = 0.5`、`missing_block = ["一级供给"]`、`block_incomplete = true`、`dimension_completeness = 0.2857`。**该读数不是完整维度分**，禁止与 CN 横比或拼接（原理 5）。

下游接口（只声明数据字段，不规定展示）：`BIZ.MARKET_TEMPERATURE` 当前零生产者、暂无消费方，将来若启用须**按 `availability_tier` 分段**取 as-of 分位 `u_i`（§2.4.7），不得把 CN 与 HK 混进同一分位基数；`BIZ.RISK_BUDGET` 的输入清单（§6）不含本维度，`score` / `state` 均不驱动任何 `cap`；`SIG.REVERSAL` 顶部 / 底部族（爆款基金、IPO 破发率极低、换手 >90 分位 / 新基金发行困难、换手 <20 分位）**直接读 L1 原子 `percentile`**，不读本维度分；外部消费方读表时用 `model_version` 判口径。

---

### 5.4 资金 Flow · `DIM.FLOW`

#### 定义

**一句话定位**：`DIM.FLOW` 回答「钱实际进出多少」——只收**有符号的净额**；是内部描述量，不是交易或预测信号。

| 规格项 | 值 |
|---|---|
| 编码 / 层级 | `DIM.FLOW` / L2 维度指标 |
| 业务问题 | 真金白银的净流强度（能力，非参与意愿） |
| 极性 | `polarity = high_risk`（只作**刻度方向标签**：高分 = 净流入 / 公司净买入侧；**流入 ≠ 好，更不构成买卖依据**） |
| 值域 | `score` 0–100（单锚的 as-of 分位）；`state` 五档（标签见「本环节参数」），**规格上恒 NULL**（`state_bands_calibrated = false`） |
| 频率 / 粒度 | 日频落库；主键 `(dimension_code, market, trade_date)` |
| 市场 | `CN` / `HK`；**按市场各自的单锚读数**，两侧核心成员不同、分位基数不同，**禁止横比、禁止拼接成一条曲线** |
| 给 L3 的角色 | 解释维度（`role = explanation`）：无 A 类决策消费者；`state` 恒 NULL、`score` 不驱动任何 `cap`，只供 `BIZ.MARKET_TEMPERATURE` 将来按可用性分层转自身 as-of 分位 `u_i`（§2.4.7） |

**边界（不回答什么）**：① 参与行为与意愿（换手、卖空、IPO / 基金发行 → §5.3 情绪）；② 流动性宽紧与系统稳健性（HIBOR / Amihud / 买卖价差 → 压力维）；③ 未来供给（`SENT.UNLOCK_NEXT_30D_SHARES` 是前瞻量，由 `SIG.REVERSAL` 读 L1 原子）；④ 指数往哪走、能上多少仓位（§6 `BIZ.*`）；⑤ 股债性价比与宏观条件（第①②层）。

> **落地状态（v1.15）**：本节已从设计契约转为生效规格。原 4 成员 / 2 块两级等权方案因数据不可用而废弃（决策留痕见附录 D.2），改为**按市场单锚 + 6 个 `flags_only` 诊断成员**；生产者 `build_flow_dimension`、字典 `tools/calibrate_flow.py`、L1 专项重算 `tools/recalc_flow_percentile.py`、回归测试 `tests/test_flow_dimension.py` 均已落地。库内 `params_hash = 67d92d2240e4d593`。

#### 输入

来源表 `regime.indicator_value`，字段 `(indicator_code, market, trade_date, percentile)`；`direction` 取自 `regime.indicator_dict`，`members[].sign` 是极性唯一真源（§2.4 规则 2）。L2 只读 `percentile`，**不得在维度层重算原始净额**（§4 单一真相源）。

**成员清单（现行 8 个：2 核心 + 6 诊断）**：

| 指标名称 / 成员编码 | 角色（`block`） | `sign` | 源 / L1 计算 | 起点 / 行数（实测） | `market_scope` |
|---|---|---|---|---|---|
| 两融净增强度<br>`FLOW.MARGIN_NET_20D` | **核心** · 杠杆资金净流 | `+1` | `Δ20 Σ两融余额 / Σ20 成交额 ×100` | 2019-01-02 / 1886 | `CN` |
| 港股回购净额强度<br>`FLOW.BUYBACK_NET_20D` | **核心** · 公司净买入 / 股份供给 | `+1` | `Σ20 回购额(归并下一开市日) / Σ20 成交额 ×100` | 2010-01-04 / 4150 | `HK` |
| 北向净流入强度<br>`FLOW.NORTHBOUND_NET` | 诊断 · `flags_only` | `+1` | 同上累计口径；**2024-08-16 断供** | 2014-12-12~2024-08-16 / 1341 | `CN` |
| 两融占流通市值<br>`FLOW.MARGIN_RATIO` | 诊断 · `flags_only` | `+1` | 水平占比；**分母 2026-10 覆盖塌陷** | 2019-01-02 / 1893 | `CN` |
| 产业资本净减持（60 日）<br>`SENT.INSIDER_NET_SELL_60D` | 诊断 · `flags_only` | `−1` | 事件窗口；分位退化 | 2025-10-09 / 262 | `CN` |
| 过去 30 日解禁市值<br>`SENT.UNLOCK_PAST_30D_VALUE` | 诊断 · `flags_only` | `−1` | 潜在供给，非真实净流 | 2016-01-04 / 2632 | `CN` |
| 南向净流入强度<br>`FLOW.SOUTHBOUND_NET` | 诊断 · `flags_only` | `+1` | `est_` 估算、样本短 | 2026-06-03~2026-09-24 / 80 | `HK` |
| 港股卖空成交占比<br>`FLOW.SHORT_SELLING_RATIO` | 诊断 · `flags_only` | `−1` | 参与度，非净额 | 2026-06-04 / 103 | `HK` |

> ⚠ **诊断成员不进 `score` / `dispersion`**，只写 `coverage` / `contrib` / `flags`（§5.1 方案 C）。`sign` 仍是**待构念效度检验的理论推定**（ADR B-8 同款纪律），`remark` 标「弱证据」，不作预测依据。

**不进 `members[]` 的项**（不计 `coverage` 分母）：`FLOW.HIBOR_3M`（货币市场利率 → 压力维）、`SENT.UNLOCK_NEXT_30D_SHARES`（前瞻量 → 信号层）、`FLOW.TURNOVER_RATIO`（参与度 → §5.3 情绪）、`daily_ggt_hold` 持股占比变化（派生量）、ETF / 基金申赎（无源）、A 股回购方案 `a_stock_repurchase_plan`（**方案累计快照，非日频增量**：按 `latest_ann_date` 聚合会把不同年代累计值相加，口径错误，见附录 D.2）。成交额只作标准化分母，不重复建指标。

**维度指标注册（现行）** — `tools/calibrate_flow.py --write` 产物，节选（完整以库内字典为准）：

```json
{
  "dimension_code": "DIM.FLOW",
  "dimension_name": "资金",
  "polarity": "high_risk",
  "members": [
    {"code": "FLOW.MARGIN_NET_20D",  "sign":  1, "weight": 1.0, "block": "杠杆资金净流"},
    {"code": "FLOW.BUYBACK_NET_20D", "sign":  1, "weight": 1.0, "block": "公司净买入/股份供给"},
    {"code": "FLOW.NORTHBOUND_NET",  "sign":  1, "weight": 0.0, "block": "诊断", "flags_only": true},
    {"code": "FLOW.MARGIN_RATIO",    "sign":  1, "weight": 0.0, "block": "诊断", "flags_only": true},
    {"code": "SENT.INSIDER_NET_SELL_60D",  "sign": -1, "weight": 0.0, "block": "诊断", "flags_only": true},
    {"code": "SENT.UNLOCK_PAST_30D_VALUE", "sign": -1, "weight": 0.0, "block": "诊断", "flags_only": true},
    {"code": "FLOW.SOUTHBOUND_NET",  "sign":  1, "weight": 0.0, "block": "诊断", "flags_only": true},
    {"code": "FLOW.SHORT_SELLING_RATIO", "sign": -1, "weight": 0.0, "block": "诊断", "flags_only": true}
  ],
  "block_agg": {"杠杆资金净流": "mean", "公司净买入/股份供给": "mean"},
  "state_bands": {"CN": [20,40,60,80], "HK": [20,40,60,80]},
  "state_labels": ["极流出", "偏流出", "中性", "偏流入", "极流入"],
  "params": {
    "coverage_min": 0.6, "core_coverage_min": 1.0,
    "state_bands_calibrated": false, "flags_shape": "dict",
    "proxy_member_by_market": {"CN": "FLOW.MARGIN_NET_20D", "HK": "FLOW.BUYBACK_NET_20D"},
    "availability_tier": "flow_single_anchor",
    "flow_asof_start": {"CN": "2019-01-02", "HK": "2010-01-04"},
    "regime_shift_2016": true, "northbound_discontinued": "2024-08-16",
    "margin_ratio_source_discontinued": true,
    "member_valid_from": {"CN": {"FLOW.MARGIN_NET_20D": "2020-01-10"},
                          "HK": {"FLOW.BUYBACK_NET_20D": "2010-12-31"}},
    "member_confident_from": {"CN": {"FLOW.MARGIN_NET_20D": "2022-02-17"},
                              "HK": {"FLOW.BUYBACK_NET_20D": "2013-01-10"}}
  },
  "params_hash": "67d92d2240e4d593",
  "is_active": true
}
```

> `weight` 只是记录值；**生产者按 `params.proxy_member_by_market` 取单锚，不读 `weight`**（两级块平均在单锚下退化为取值）。`coverage` 按全体 `members[]` 联 `market_scope` 计（CN 5 / HK 3），**只作描述量**；产分门禁改用 `flags.core_coverage`（单锚有效）。`state_bands` 是按市场 dict 的占位值，`state_bands_calibrated = false` 期间整维恒不出档。

**冻结有效起点**（`tools/calibrate_flow.py` 按第 250 / 750 个有效分位日离线标定，纳入 `params_hash`）：

| 核心成员 | 满 250 | 满 750 | 有效观测 |
|---|---|---|---|
| `FLOW.MARGIN_NET_20D`（CN） | **2020-01-10** | 2022-02-17 | 1886 |
| `FLOW.BUYBACK_NET_20D`（HK） | **2010-12-31** | 2013-01-10 | 4150 |

→ **CN 可发布起点 2020-01-10 / HK 2010-12-31**；冻结在字典而非动态读 L1 `extra.n_sample`（资金族未落地），PIT 安全、不触发 L1 重算。

**输入侧数据可用性门禁（L1 层，由 `indicator_dict.formula` 承担）**：

- 净额一律「**20 交易日累计 ÷ 同期成交额 ×100**」后再取 expanding 分位。原始净额是流量型成分，随市场规模漂移（CN 成交额日均 2010 年 2062 亿 → 2026 年 24511 亿；HK 438 亿 → 1979 亿），不标准化会把年份读成资金强弱。分母用 `a_daily_market_turnover` / `daily_market_turnover`（历史最全、过滤 `total_turnover <= 0`），不从行情表 `sum(amount)` 取。
- **`FLOW_ASOF_START` 护栏**：`MARGIN_NET_20D` 2019-01-02 起、`BUYBACK_NET_20D` 2010-01-04 起才产出 `raw_value`（护栏前 NULL、不进参考分布）；20 日窗口仍在护栏前数据上计算，保证起点当日窗口完整。CN 两融表虽已回补齐 2010 起，但 2015-2018 去杠杆段波动（2015 sd 1.37 / 2017 min −6.07）与标的池扩张（87 → 3993 只）会污染 expanding 分布，补齐段只作离线对照。HK 2010 前单票集中度最高 92%、2002 年 `amount` 全 NULL，同理排除。
- **回购日期对齐**：`daily_buyback_event.buyback_date` 含休市日（2010 起 145 个不在 HK 交易日历内，且 `trading_calendar` 的 HK 段滞后），按「下一个开市日」归并求和；交易日序列以成交额表为准。归并后 2010 起 4150 个交易日中 4115 天有回购（99.2%）。**当日无事件 = 真实的 0**，与「净额断供 = 缺失」严格区分。
- **缺口不是中性**：北向 2024-08-16 后无源 → `percentile` NULL，不补 0 / 50；窗口内有效观测不足 20 → NULL。南向 `est_net_inflow` 是持股变动反推的**估算值**（与 `hold_value_change_1d` 相关仅 0.19），源表 2026-04-20 起仅 130 天，20 日累计后只剩 80 个有效观测且 10 月源表交易日缺口造成断尾 → 降为诊断。
- **两融 NULL 修复**：`daily_margin_balance` 有 176,133 行 `rzrq_balance IS NULL` 但子字段均非空，新原子用 `COALESCE(rzrq_balance, rz_balance + rq_balance)`；`FLOW.MARGIN_RATIO` 既有值保持不变、不触发它重算。
- **半日市失真**：港股半日市成交额约为全日一半，标准化分母偏小 → 该日资金分位系统性偏高；由交易历标记识别，L2 只标注、不改 L1 值。
- 上述门禁由数据口径决定、不进 `dimension_dict.params`；但 `formula` / 护栏变更会改动 `raw_value` 与参考样本集，须按 §2.2 递增 `FLOW_CALIB_VERSION` 并用 `tools/recalc_flow_percentile.py --write` 全量重算 L1 + L2。当前 L1 版本 `v2.1.14` 只挂在 `FLOW_CALIB_CODES` 的 4 个编码上，**不波及其余 121 个指标**（实测 `params_hash` 逐值不变）。

**数据覆盖事实**（库内实测，2026-11-06）：

| 成员 / 源 | CN | HK | 说明 |
|---|---|---|---|
| 两融净增 `FLOW.MARGIN_NET_20D` | 2019-01-02 起 1886 行 | — | 源表 2010-03-31 起 4013 天（2017-04~2018-12 曾整段缺失，**已补齐**），2011 后近乎全覆盖 |
| 回购 `FLOW.BUYBACK_NET_20D` | — | 2010-01-04 起 4150 行 | 源表 1991-11-28 起 104221 条 / 1369 只 / 全 HK. 前缀，`amount` 非空 99.4%、与 `volume × avg_price` 零偏差 |
| 北向 `FLOW.NORTHBOUND_NET` | 2014-12-12~2024-08-16 / 1341 | — | 官方停披露，断点后 0 行 |
| 南向 `FLOW.SOUTHBOUND_NET` | — | 2026-06-03~2026-09-24 / 80 | `daily_ggt_hold` 仅 130 天；20 日累计 + 源表交易日缺口导致断尾 |
| `FLOW.MARGIN_RATIO` | 2019-01-02 起 1893 | — | 分母 `circular_market_val` 2018 前全 NULL、2026-10 有效率 89.53% → L1 raw 2.58 → 6.99、分位顶格，故不进核心 |
| 产业资本 / 解禁 / 卖空 | 262 / 2632 / — | — / — / 103 | 短样本或潜在供给，仅诊断 |
| A 股回购 `a_stock_repurchase_plan` | 6463 个方案快照 | — | **不可做日频时序**，见上文 |
| `daily_south_flow` 旧表 | — | 仅 13 行 | 已废弃 |
| `regime.sector_fund_flow` | 2026-09-30 起 21 天 | — | 太短，不接 |
| L1 `extra.n_sample` | 未落地 | 未落地 | 由冻结有效起点绕过，不依赖 |

#### 计算模型

**原理与背景**

1. **为什么废弃原 4 成员 / 2 块两级等权**：北向 2024-08-16 断供、HK 的杠杆与供给块无源 → 两侧天然各缺一整块，`block_coverage_min = 1.0` 使 `state` 永不可能出档；唯一产物 `score` 是 128 天 / 262 天样本驱动的单块代理，且 `INSIDER_NET_SELL_60D` 的 expanding 分位已退化（2025-11 月均 98.10 / std 0.43、2026-10 月均 5.42 / std 1.34，读的是时间趋势而非强度）。模型不是「保守」，是被数据锁死。
2. **为什么按市场单锚**：可用数据只有两条长历史、同源日更、有符号且已标准化的净额序列（CN 两融净增 / HK 公司回购），每市场恰好一条。复用 §5.3 情绪维「HK 单成分代理分」的既有纪律，把 `hk_proxy_member` 推广为 `proxy_member_by_market`，两市场都标 `single_block_proxy`。代价是两侧分数不可横比、不可拼接；收益是**两侧都从百天级短样本代理变成千年级可比读数**。
3. **为什么回购算资金维成员**：回购是**公司部门净买入 / 股份供给收缩**，属「真金白银的净额」与供给侧流量，与 CN 侧产业资本净减持同构；但它**不是外部资金流入**，语义边界写入 `indicator_dict.remark` 与本维 `remark`，不得与跨境净流混同。`sign = +1` 是理论推定。
4. **为什么取消块级门禁**：两侧各只有 1 个核心成员，`block_coverage_min` / `dispersion_core_max` / `weight_cap` 全部失效；`block_coverage` 改按**本市场应有块数（1）**计，恒 1.0，不再出现「两侧恒 0.5 → 永远不出档」的死局。出档由 `state_bands_calibrated = false` 单独承担。
5. **为什么冻结有效起点**：资金族 `min_periods = 0`、`extra.n_sample` 未落地，短样本 expanding 分位会退化（§5.3 原理 2 实测核心成员前 250 个分位极端占比 19.6%）。按 (market, code) 冻结第 250 / 750 个有效分位日写进 `params`，核心未达标则**整行不产出**，不用低覆盖分数凑历史。
6. **为什么 `confidence` 最高只到「中」**：单锚代理不是完整维度分，即使样本满 750 也不伪装成「高」；`dispersion` 恒 NULL。
7. **为什么不单设滚动分位窗口**：分位窗口是 L1 按指标族登记的参数，且 `FLOW.TURNOVER_RATIO` 被情绪 / 资金跨维复用，按维度单设违反 §4「同一原子只有一套分位」；`market_profile.md` §5.7 的「情绪 / 资金滚动 10 年」仍为 **ADR B-7 待裁决项**。HK 2016 制度漂移不由改窗口解决，改由 `flags.regime_shift_2016` 标注 + `remark` 限定「只作相对变化读数、不做跨年代水平比较」。

**指标计算方法（简述）**

核心公式：`score = p_proxy`（`sign = −1` 时取 `100 − p_proxy`），`proxy = params.proxy_member_by_market[market]`。

0. 读 8 个成员 `percentile`，按 `members[].sign` 对齐；按 `market_scope` 取 `eligible`（CN 5 / HK 3），`core` = 非 `flags_only`（各 1 个）。单锚配置不符（`core` 与 `proxy` 不一致）→ 整维不产行，不静默出分。
1. 按 `member_valid_from[market]` 冻结日期判定核心有效；`core_coverage < 1.0` 时**整行不产出**。`coverage` 仍按全体 eligible 计，只作描述量。
2. `score` = 单锚对齐分位并裁剪到 0–100，不补 0 / 50；`blocks` 只写本市场 1 块且带 `proxy: true`；`dispersion` 恒 NULL。
3. `confidence`：未达 `member_confident_from` → 低 + `flags.short_sample`；之后升「中」，单锚不出「高」。
4. `state` 恒 NULL（`state_bands_calibrated = false`）；`flags` 写 `single_block_proxy` / `availability_tier` / `core_coverage` / `dimension_completeness` / `block_coverage` / `flow_asof_start` / `member_valid_from` / `regime_shift_2016`（HK ≥ 2016）/ `northbound_discontinued`（CN）/ `margin_ratio_source_discontinued`（CN）/ 可选 `low_coverage` / `short_sample`。

本层没有额外分支逻辑，以上四步即完整计算。

**本环节参数**（逐个对应上面的生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `proxy_member_by_market` | `{CN: FLOW.MARGIN_NET_20D, HK: FLOW.BUYBACK_NET_20D}` | 第 0 步 | 声明各市场取哪个单锚；改变 `score` 来源 | 按数据实际覆盖反推；写入字典并纳入 `params_hash`，变更触发 L2 全量重算 |
| `member_valid_from` / `member_confident_from` | CN 2020-01-10 / 2022-02-17；HK 2010-12-31 / 2013-01-10 | 第 1、3 步 | 决定哪日起分位才被采信 / `confidence` 何时升「中」 | 第 250 / 750 个有效分位日离线冻结，PIT 安全、不触发 L1 重算；变更触发 L2 全量重算 |
| `core_coverage_min` | 1.0（**已启用**） | 第 1 步 | 单锚无效则整行不产出（不是低覆盖分数） | §5.3 同款；变更触发 L2 重算 |
| `state_bands` / `state_bands_calibrated` | 按市场占位 / `false` | 第 4 步 | `false` 时整维恒不出档 | 切点须按各市场 `score` 分布 P20/P40/P60/P80 标定后冻结；改 `true` 触发 `params_hash` 与 L2 全量重算 |
| `state_labels` | 极流出 / 偏流出 / 中性 / 偏流入 / 极流入 | 第 4 步 | 决定 `state` 文字标签 | 与切点同步冻结 |
| `flow_asof_start` / `regime_shift_2016` / `northbound_discontinued` / `margin_ratio_source_discontinued` | 2019-01-02 / 2010-01-04；`true`；2024-08-16；`true` | 第 4 步写 `flags` | 只写可用性 / 制度漂移标注，不改 `score` / `state` | 输入侧事实的字典映射，供消费方判读 |
| `flags_shape` | `dict` | 第 4 步 | 固定 `flags` 为 dict | 对齐 §2.3.3；形状写入 `remark` 并由 `tests/test_flow_dimension.py` 钉住 |

> 通用参数 `min_periods` / `coverage_min` / `dispersion_max` / `weight_cap` / `winsor` 定义在 §2.4，本维只作记录：`coverage` 只描述、单锚 `dispersion` 恒 NULL、`weight_cap` 与两级块平均均不生效。净额标准化 / 20 日累计 / 护栏 / 回购日归并 / 缺口不补 0 都是 **L1 输入侧门禁**，不进 `dimension_dict.params` / `params_hash`。

**落地形态**（`build_dimension_value.py::build_flow_dimension`，v1.15 已落地）：

```python
for market, trade_date in calendar:
    p = read(indicator_value, market, trade_date, members[].code)
    aligned = {c: (100.0 - v) if sign[c] == -1 else v for c, v in p.items()}
    proxy = params["proxy_member_by_market"][market]
    core  = [c for c in eligible if not flags_only[c]]
    if len(core) != 1 or core[0] != proxy: continue         # 配置不符不静默出分
    valid = aligned[proxy] IS NOT NULL and trade_date >= member_valid_from[market][proxy]
    core_coverage = 1.0 if valid else 0.0
    if core_coverage < core_coverage_min: continue           # 整行不产出
    coverage = count(aligned WHERE c IN eligible) / len(eligible)   # 仅描述量
    score   = clip(aligned[proxy], 0, 100)
    dispersion = NULL                                        # 单锚
    short   = trade_date < member_confident_from[market][proxy]
    confidence = '低' if short else '中'                      # 单锚不出「高」
    state   = NULL                                           # 切点未标定
    flags = {single_block_proxy: True, availability_tier, core_coverage,
             dimension_completeness, block_coverage: 1.0, flow_asof_start,
             member_valid_from, regime_shift_2016?(HK>=2016),
             northbound_discontinued?(CN), margin_ratio_source_discontinued?(CN),
             short_sample?, low_coverage?}
    write_one_row(dimension_value, PK=(dimension_code, market, trade_date),
                  score, state=NULL, coverage, dispersion=NULL,
                  blocks={本市场1块: {score, coverage, proxy: True}},
                  contrib=正负各 top3, flags, confidence,
                  model_version=dimension_dict.params_hash)
```

出档门禁见 §2.4.3；落库须用 `skip_null_updates=False`（`state = NULL` 必须能覆盖旧档）+ `only_if_changed=True`。PIT、幂等、重算粒度、版本化见 §2.4.4–5（**参考样本集变更的第二类全量重算**由 `tools/recalc_flow_percentile.py` 承担）。`dimension_value` 没有 `quality` / `extra` 列，可用性一律用 `confidence` + `flags` 承载。生产者由 `build_all_dimensions()` 按 `dimension_code` 分派，不改现有三生产者；`is_active = TRUE` 即自动接入日频（**常驻 scheduler 进程需重启后才生效**，本次已由 CLI 完成全历史计算）。全历史命令：`build_dimension_value.py --dim DIM.FLOW --write --backfill`。

#### 输出

目标表 `regime.dimension_value`，字段字典见 §2.3.3。**库内真实行**（2026-11-04，CN）：

```json
{
  "dimension_code": "DIM.FLOW",
  "market": "CN",
  "trade_date": "2026-11-04",
  "score": 1.03,
  "state": null,
  "coverage": 0.8000,
  "dispersion": null,
  "blocks": {"杠杆资金净流": {"score": 1.03, "coverage": 1.0, "proxy": true}},
  "contrib": {"pos": [], "neg": []},
  "flags": {"availability_tier": "flow_single_anchor", "core_coverage": 1.0,
            "dimension_completeness": 0.625, "block_coverage": 1.0,
            "single_block_proxy": true, "flow_asof_start": "2019-01-02",
            "member_valid_from": {"FLOW.MARGIN_NET_20D": "2020-01-10"},
            "northbound_discontinued": "2024-08-16",
            "margin_ratio_source_discontinued": true},
  "confidence": "中",
  "model_version": "67d92d2240e4d593"
}
```

`coverage = 4/5 = 0.8`（CN eligible 5 个、1 个诊断成员当日无值），只是描述量；`dimension_completeness = 5/8 = 0.625`；`block_coverage = 1.0` 是**按本市场应有 1 块**计，不再按全体 2 块判。`state = null` 只能读作「**未出档**」，不是「中性」（§2.4.6）。HK 同日 `score = 83.62`、`coverage = 0.6667`、`dimension_completeness = 0.375`、`flags.regime_shift_2016 = true`、`confidence = 中`。⚠ 两侧核心成员、分位基数、构念都不同，**禁止横比、禁止拼接成一条曲线**。

**已产出读数**（`build_dimension_value.py --dim DIM.FLOW --write --backfill`）：

| 市场 | 行数 | 区间 | `state` | `confidence` | `model_version` |
|---|---|---|---|---|---|
| CN | 1637 | 2020-01-10 ~ 2026-11-04 | 全 NULL（出档 0） | 低 500 / 中 1137 | 全部 = `67d92d2240e4d593` |
| HK | 3901 | 2010-12-31 ~ 2026-11-06 | 全 NULL（出档 0） | 低 500 / 中 3401 | 同左 |

现有三维（宽度 / 估值 / 情绪）行数与版本逐值未变；4 个资金原子与 4 个未改的 `FLOW.*` 指标 `model_version` 全部等于字典 `params_hash`（`FLOW_CALIB_VERSION` 隔离生效）。

下游接口（只声明数据字段，不规定展示）：`BIZ.MARKET_TEMPERATURE` 当前零生产者、暂无消费方，将来若启用须按 `availability_tier` / `flags` 分层取自身历史 as-of 分位 `u_i`（§2.4.7），不得把 CN 与 HK 混进同一分位基数；`BIZ.RISK_BUDGET` 的输入清单（§6）**不含本维度**，`score` / `state` 均不驱动任何 `cap`；`SIG.REVERSAL` 顶部 / 底部族与 `SENT.UNLOCK_NEXT_30D_SHARES` **直接读 L1 原子 `percentile`**，不读本维度分；外部消费方读表时用 `model_version` 判口径。

---

### 5.5 压力 Stress · `DIM.STRESS`

#### 定义

**一句话定位**：`DIM.STRESS` 回答「市场还能不能正常运转、风险有没有在聚集」——波动、相关性、信用、尾部四处是否同时失稳；对应业界 FSI / FCI（STLFSI、KCFSI、NFCI、ECB CISS）。

| 规格项 | 值 |
|---|---|
| 编码 / 层级 | `DIM.STRESS` / L2 维度指标 |
| 业务问题 | 系统稳健性（共振型故障，非单块极值） |
| 极性 | `polarity = high_risk`：高分 = 压力越大 / 越危险 |
| 值域 | `score` 0–100（4 块两级等权的分位读数）、`state` 五档（标签见「本环节参数」），**规格上恒 NULL**（`state_bands_calibrated = false`） |
| 频率 / 粒度 | 日频落库；主键 `(dimension_code, market, trade_date)` |
| 市场 | `CN` / `HK`；核心成员与分位基数不同（CN 8 核心 / HK 7 核心），**禁止横比、禁止拼接成一条曲线** |
| 聚合结构 | **固定核心集 9 成员**（8 核心 + 1 `flags_only` 诊断）→ 4 块（波动 / 相关性 / 信用 / 尾部）→ 块内等权 → 块间等权；空块从分母剔除 |
| 给 L3 的角色 | **决策维度**：`BIZ.SYSTEMIC_STRESS` 的主判据（`score` + `blocks`）；其 `level` 再作为 `BIZ.RISK_BUDGET` 的系统约束（§6），`RISK_BUDGET` 不直接读本维度 |

**边界（不回答什么）**：① 参与广度与结构分化（§5.1，见下方健康度三侧面）；② 指数方向与强度（趋势维 / `BIZ.MARKET_REGIME`）；③ 危机时点预测与跨资产加仓确认（第②层 / §4.2）；④ 仓位动作（§6）；⑤ 参与意愿与净流方向（§5.3 / §5.4）。

**健康度三侧面（只作诊断视角，不参与计算）**：

| 侧面 | 度量 | 健康特征 | 归属 |
|---|---|---|---|
| 参与广度 | `%>MA200`、A/D Line、行业参与广度 | 广泛参与 | §5.1 宽度 |
| 结构分化 | 等权−加权差、成交额集中度 | 分化低 | §5.1 宽度 |
| **系统稳健性** | 个股平均相关性、同步下跌、信用利差、波动、流动性 | 相关性低、流动性好 | **本节** |

#### 输入

来源表 `regime.indicator_value`，字段 `(indicator_code, market, trade_date, percentile)`；`direction` 取自 `regime.indicator_dict`，`members[].sign` 是极性唯一真源（§2.4 规则 2）。L2 只读 `percentile`，**不在维度层重算波动 / 利差 / 相关性原始值**（§4 单一真相源）。

**成员清单（现行 9 个，2026-11-06 实测起点）**：

| 子块（`block`） / 成员编码 | `sign` | 源 / 计算 | CN 起点 | HK 起点 | `market_scope` |
|---|---|---|---|---|---|
| 波动 `RISK.RV20`、`RISK.VIX_CHG20` | `+1` / `+1` | 本市场指数 20 日已实现波动 / VIX 20 日变化（差分型，平稳） | 1991-01-10 / 1990-12-20 | **2013-09-10** / 2000-01-03 | `CN+HK` / `GLOBAL` |
| 相关性 `RISK.SYNC_DOWN_20D`、`BREADTH.AVG_CORR_VW` | `+1` / `+1` | 下跌家数占比 20 日均值 / 方差加权平均相关性（60 日） | 1991-01-03 / 1991-02-08 | **2000-01-14** / **2000-02-23** | `CN+HK` |
| 信用 `RISK.CREDIT_SPREAD_CHG20`、`RISK.TERM_SPREAD` | `+1` / **`−1`** | AAA 中票信用利差 20 日变化 / 国债 10Y−1Y 期限利差（**倒挂低 = 危险**） | 2011-01-31 / 2006-03-01 | 2011-01-31 / —（无源） | `CN+HK` / `CN` |
| 尾部 `RISK.EXTREME_DOWN_PCT`、`RISK.MAXDD_250` | `+1` / **`−1`** | 单日跌幅超 3% 家数占比 / 250 日最大回撤（**raw 为负值，高分位 = 浅回撤**） | 1990-12-20 / 1991-03-15 | 2000-01-03 / **2013-11-14** | `CN+HK` |
| 诊断 `FLOW.HIBOR_3M`（HK `flags_only`） | `+1` | 3 个月 HIBOR 水平值（**分位随年代漂移，不进 `score` / `dispersion`**） | —（无源） | 2000-01-03 | `HK` |

> ⚠ **两处 `sign` 修正**（v1.17）：研究稿与 v1.16 规划种子把 21 个成员全写成 `+1`，但 `RISK.MAXDD_250` 的 `raw_value` 实测为 −72.32~0（负回撤），高分位代表回撤浅；`RISK.TERM_SPREAD` 字典 `direction = high_good`（倒挂低 = 危险）。二者取 `−1`，否则尾部与信用块方向读反。
> ⚠ **实测起点更正**：研究稿 v1.16 写「HK 本地 RV / 相关性 / 尾部 2019 起」，实测 HK 本地波动是 **2013-09-10**、回撤 **2013-11-14**、相关性 / 极端跌幅 **2000-01**、HIBOR **2000-01-03**。`indicator_value` 只写有值行，覆盖率须用「行数 ÷ 交易日历」；HK 覆盖率显示 100.17~102.5% 是**交易日历只到 2026-10-29、数据到 11-05** 的滞后，不是重复写行。
> ⚠ **编码前缀不表归属**：`BREADTH.AVG_CORR_VW` 前缀沿用宽度但归压力、`FLOW.HIBOR_3M` 前缀沿用资金但归压力诊断；一律不改编码以保序列连续，归属以 `indicator_dict.domain` 为准。字典 `domain` 的 legacy 投影（多数 `RISK.*` 标「风险」、HIBOR 标「资金」）属落地接线差异，不影响 `coverage` 分母、不触发 L1 重算。

**被移出 `members[]` 的 12 个成员**（不计 `coverage` 分母，编码登记在 `params.excluded_members`，只作离线诊断）：

| 类别 | 成员 | 理由 |
|---|---|---|
| 短样本（4） | `RISK.CREDIT_SPREAD_AA`、`CREDIT_SPREAD_AA_MINUS`、`GRADE_SPREAD`、`GRADE_SPREAD_AA_MINUS` | 仅 19~27 行（2026-09-28 / 10-12 起）；`GRADE_SPREAD_AA_MINUS` 分位恒 50.0、std 0。留在数组里会「分母算它、分子几乎永远不算」恒定压低 `coverage`（§5.2 v1.2 的 `VAL.ERP_Z` 同款） |
| 水平型漂移 + 跨资产冗余（8） | `RISK.RV60`、`RISK.VIX`、`RISK.BOND_RV60`、`COPPER_RV60`、`GOLD_RV60`、`OIL_RV60`、`SPX_RV60`、`RISK.CREDIT_SPREAD` | 水平值 expanding 分位随年代漂移（`CREDIT_SPREAD` 均值 30.3(2011–15) → 5.0(2021–23)；`VIX` 55.6 → 46.6），且 9 个波动成员块内相关中位仅 0.18；`OIL_RV60` 2018 起、`SPX_RV60` 2016 起，起点不齐 |

**维度指标注册（生效字典，节选；完整以库内 `dimension_dict` 为准）**  
对应表为 regime.dimension_dict

```json
{
  "dimension_code": "DIM.STRESS",
  "dimension_name": "压力",
  "polarity": "high_risk",
  "members": [
    {"code": "RISK.RV20",                 "sign":  1, "block": "波动"},
    {"code": "RISK.VIX_CHG20",           "sign":  1, "block": "波动"},
    {"code": "RISK.SYNC_DOWN_20D",       "sign":  1, "block": "相关性"},
    {"code": "BREADTH.AVG_CORR_VW",      "sign":  1, "block": "相关性"},
    {"code": "RISK.CREDIT_SPREAD_CHG20", "sign":  1, "block": "信用"},
    {"code": "RISK.TERM_SPREAD",         "sign": -1, "block": "信用"},
    {"code": "RISK.EXTREME_DOWN_PCT",    "sign":  1, "block": "尾部"},
    {"code": "RISK.MAXDD_250",           "sign": -1, "block": "尾部"},
    {"code": "FLOW.HIBOR_3M",            "sign":  1, "block": "诊断", "flags_only": true}
  ],
  "block_agg": {"波动": "mean", "相关性": "mean", "信用": "mean", "尾部": "mean"},
  "state_bands": {"CN": [20,40,60,80], "HK": [20,40,60,80]},
  "state_labels": ["极稳", "稳健", "正常", "紧张", "危机"],
  "params": {
    "core_coverage_min": 1.0, "state_bands_calibrated": false,
    "dispersion_no_gate": true, "dispersion_scope": "core_only",
    "availability_tier": "stress_core_fixed",
    "empty_block_policy": "drop_from_denominator",
    "resonance_floor": 80, "excluded_members": ["<12 个编码，见上表>"],
    "member_valid_from": {"CN": "<8 个核心各自的第 250 有效分位日>", "HK": "<7 个核心各自的第 250 有效分位日>"},
    "member_confident_from": {"CN": "<第 750 日，见库内>", "HK": "<第 750 日，见库内>"},
    "min_periods": 250, "quality_sample": 750,
    "coverage_min": 0.6, "dispersion_max": 30, "weight_cap": 0.25, "winsor": [2,98]
  },
  "params_hash": "17952671d0fa0438",
  "is_active": true
}
```

> ⚠ 上方 `excluded_members` / 两组冻结日期为**节选占位**，物化值在库内字典与 `tools/calibrate_stress.py`，并纳入 `params_hash`；`members[].weight` 是记录值，生产者按块两级等权、不按 `weight` 加权。通用参数（`coverage_min` / `dispersion_max` / `weight_cap` / `winsor`）的取值与 §2.4 一致，不是第二处定义；`winsor` 未生效、`weight_cap` 对分块结构不生效；`pct_window = expanding` 属 L1（§2.4 规则 1 / ADR B-4），不进 L2 字典 `params`。

**输入侧数据可用性门禁（L1 层）**：

- **`extra` / `min_periods` 缺口**：21 个压力原子中无一行 `indicator_value.extra` 登记 `n_sample`，且压力族 L1 `min_periods = 0`（只有宽度族为 250）。故 §2.4 规则 6 的「`n_sample < 750` 降置信」在本维**无法由 L1 承担**，改由字典冻结的第 250 / 750 个有效分位日判定（PIT 安全、不触发 L1 重算，与 §5.3 / §5.4 同款）。
- **差分型优先**：`VIX_CHG20` / `CREDIT_SPREAD_CHG20` 的分位均值各时期恒在 48~53、std 21~31；水平型 `CREDIT_SPREAD` / `FLOW.HIBOR_3M` / `RISK.VIX` 存在年代漂移，故后者移出 `score`（研究稿 §5.7 L2-a 要求的「水平型先平稳化」在本版用「换成员」而非改 L1 公式实现）。
- **缺口两类严格区分**：源缺失（iVIX / VHSI、CDS、高收益债利差、买卖价差）灰显、不按中性补值；指标未建（市场级 `RISK.AMIHUD_20D`、偏度 / 峰度、ES）待 M2 另案。二者均不改变 `coverage` 分母。
- **跨资产前视**：`US.*` 序列同日对齐、回填历史有已知前视 1 天（`_global_block` 自注未修）；本版核心集不含跨资产 RV60，仅 `RISK.VIX_CHG20` 受影响，须在 `remark` 保留该限制。
- **横截面口径**：`BREADTH.AVG_CORR_VW` / `RISK.EXTREME_DOWN_PCT` / `RISK.SYNC_DOWN_20D` 已接入 `_coverage_gate`，HK 沿用 `HK_MIN_AMOUNT = 100000` 仙股门槛；`BREADTH.AVG_CORR_VW` 是**方差加权**口径，与文献 average pairwise correlation 实测差约 0.09，不可直接对标。

**数据覆盖事实（2026-11-06 实测，分母 = `public.trading_calendar` 开市日）**：

| 子块 | CN 起点 / 2018+ 覆盖 | HK 起点 / 2018+ 覆盖 | 约束 |
|---|---|---|---|
| 本地波动 `RV20`、回撤 `MAXDD_250` | 1991-01 / 1991-03，100% | **2013-09 / 2013-11**，2016+ 100% | 60 / 250 日预热；HK 受 `HK.800000` 恒指点位 2013-08-20 起限制 |
| VIX 族、相关性、极端跌幅 | 1990-12~1991-02，100% | 2000-01~2000-02，100% | 60 日预热（相关性） |
| 信用 `CREDIT_SPREAD_CHG20` | 2011-01-31，100% | 2011-01-31，100% | 20 日差分，前 20 日无值 |
| 期限利差 `TERM_SPREAD` | 2006-03-01，100% | —（无源） | `market_scope = CN` |
| HIBOR（诊断） | —（无源） | 2000-01-03，100% | `market_scope = HK` |
| AA 系 4 项（已移出） | 2026-09/10，2018+ 覆盖 0.9~1.6% | 基本无源 | 19~27 行，分位不可用 |

→ **21 成员全齐**在 CN 只有 2026-10-12 起 19 个交易日、HK 永不可能；**固定核心集**下 CN 自 **2012-02-14**、HK 自 **2014-11-18** 起 8 / 7 个核心成员覆盖 99.97~100%。冻结起点后可产出 CN 3577 日 / HK 2944 日。

#### 计算模型

**原理与背景**

1. **为什么放弃原 21 成员 / 5 块 / 信用块第二紧**：实测 2018+ 分位相关 —— 波动 9 成员中位 **0.18**（−0.28~0.72）、相关性块 **−0.18**(CN)、信用 −0.04、尾部 −0.02，块间中位 0.01；块内均值把不同现象压成 ≈50。6 个危机窗口中，原方案 CN 在 2022-10 **−0.4**、2024-01 **+0.3**（相对全期均值），危机日超过全期 P90 的比例仅 **36% / 39%**。加上 AA 系 4 项 19~27 行分位（`GRADE_SPREAD_AA_MINUS` 恒 50）会绑架「取第二紧」，空块与异常保护成为常态而非例外 —— 这是设计不成立，不是调参问题。
2. **为什么固定核心集 + 4 块两级等权**：成员数不等于信息量，去掉短样本与漂移成员后，方案在两市场的危机日 > 全期 P90 占比升到 **48% / 41%**，年度漂移更小（CN 三段 49.3 / 44.3 / 49.2）。块内等权、块间等权保证「任一处失稳都算压力」的共振语义，且两市场同为 4 块，结构一致（HK 信用块单成员由 `mean(skipna)` 自然处理）。
3. **为什么块内用 `mean` 而非 `max` / trimmed max**：离线对照显示 `max` / trimmed max 把均值抬到 65（天花板效应），危机日命中率反降到 27~29%；且核心集每块只有 1~2 个成员，trimmed max 会退化为 `max`。真正的共振信息改由 `flags.resonance_cnt` 承载，不靠抬高 `score`。
4. **为什么修两处 `sign`**：见「输入」段注；`percentile` 是观测值排位、与好坏无关，极性只在 L2 由 `members[].sign` 翻一次（§2.4 规则 2），消费端不得二次翻转。
5. **为什么冻结有效起点**：压力族 `extra.n_sample` 未落地、`min_periods = 0`，短样本 expanding 分位会退化。按 (market, code) 冻结第 250 / 750 个有效分位日（CN 2012-02-14 / 2014-03-11；HK 2014-11-18 / 2016-11-29），写进字典并纳入 `params_hash`；核心成员任一缺失或早于冻结日**整行不产出**，不用低覆盖分数凑历史（§5.3 / §5.4 先例）。
6. **为什么 `dispersion` 只描述、不作门禁，`state` 恒 NULL**：块内成员低相关是**设计事实**而非异常，套 §2.4 的 `dispersion_norm ≤ 30` 会几乎每天挡掉读数；本维把 `dispersion` 写成描述量并标 `flags.dispersion_desc_only`。`state_bands` 仍需按事件锚定标定（真实危机频率 2–5%，等频分位会把 10% 时间标成「危机」），未标定前恒 NULL；`confidence` 最高「中」，不伪装成已验证的完整维度分。
7. **为什么分位窗口仍是 expanding、共振计数不进 `score`**：ADR B-4——滚动窗口会遗忘历史危机，与 FSI 捕捉「史无前例」的目的相反，业界 STLFSI / NFCI / CISS 用全样本或 expanding。「高分位命中率」方案在 HK 对危机极敏感（+19~+45），但刻度是比例（均值 15.1 / 22.8）而非分位读数，故只写 `flags.resonance_cnt`，供业务层 `resonance_cnt` 使用。
8. **为什么 M2 候选不进本版**：市场级 Amihud / 利率 CHG20 / 5Y 信用利差属「新建 L1 原子」，会改动 L1 版本体系并触发该族全量重算；已有 L1 的宏观风险量（`MACRO.US_TERM_SPREAD` / `DXY_CHG20` 等）属第②层外部风险环境，且两项已被其它维度占用。两者都须先有本版基线，再做「加入 vs 留一」对照实验，通过才写字典（附录 D）。

**指标计算方法（简述）**

核心公式：`score = mean_b( mean_{c∈b} p_c )`，`p_c = percentile`（`sign = −1` 时取 `100 − percentile`）；块内等权、块间等权，整块为空则从块间分母剔除（`empty_block_policy = drop_from_denominator`），不记 0、不补 50。

0. 按 `market_scope` 取 `eligible`，读 9 个成员的 `percentile` 并按 `sign` 对齐；`coverage = 有效 / 应有`（分母含 `flags_only` 诊断成员，§2.3.3）。
1. `core_valid` = 核心成员当日有值且 `trade_date ≥ member_valid_from[market][code]`；`core_coverage = 有效核心 / 应有核心`，`dispersion` = 核心对齐分位的原始 std（**只描述**）。
2. 4 块各自取块内均值写 `blocks`（含 `score` / `coverage` / `n`）；`score` = 有效块分的均值并裁剪到 0–100；`core_coverage < core_coverage_min (1.0)` → **整行不产出**。
3. `resonance_cnt` = 对齐分位 ≥ `resonance_floor (80)` 的核心成员数；`confidence` = 低（早于 `member_confident_from` 最晚日，写 `flags.short_sample`）/ 中；`state` 恒 NULL（`state_bands_calibrated = false`）。
4. 写一行 `dimension_value`，`model_version = dimension_dict.params_hash`；`contrib` 正负各 top3。

本层没有额外分支逻辑，以上四步即完整计算。

**本环节参数**（逐个对应上面的生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `members[]` / `block_agg` | 9 成员 / 4 块全 `mean` | 第 0、2 步 | 决定哪些原子进 `score`、块内怎么压成一个分；只改 `score` / `blocks`，不改单个 `percentile` | 覆盖实测与危机窗口对照（原理 1/2）；写入字典并纳入 `params_hash`，变更触发 L2 全量重算 |
| `sign` | `MAXDD_250 = −1`、`TERM_SPREAD = −1`，其余 `+1` | 第 0 步 | **把观测排位对齐到「高分 = 压力大」**；只改对齐值，不改 L1 | 以 `indicator_dict.direction` 与 raw 符号为准（原理 4）；变更触发 `params_hash` 与 L2 重算 |
| `member_valid_from` / `member_confident_from` | 第 250 / 750 个有效分位日（按市场逐成员） | 第 1、3 步 | 决定哪日起分位才被采信 / `confidence` 何时升「中」；**只改 `score` 是否存在与置信，不改数值** | 离线冻结、PIT 安全、绕过缺失的 `extra.n_sample`；变更触发 L2 全量重算 |
| `core_coverage_min` | 1.0 | 第 2 步 | 核心成员任一缺失 → 整行不产出（不是低覆盖分数） | §5.3 / §5.4 同款；变更触发 L2 重算 |
| `empty_block_policy` | `drop_from_denominator` | 第 2 步 | 空块不计入块间分母，有值块重归一化；空块写 `flags.empty_block` | 空块记 0 会压低总压力分；首版核心全齐时不会触发，属防御路径 |
| `dispersion_no_gate` / `dispersion_scope` | `true` / `core_only` | 第 1 步 | `dispersion` 只写描述量；**不作 §2.4 规则 3 门禁** | 块内低相关是设计事实（原理 6）；登记在 `remark`，否则易被误加门禁 |
| `resonance_floor` | 80 | 第 3 步 | 写 `flags.resonance_cnt`（≥80 的核心成员数）；**不改 `score` / `state`** | 共振计数的阈值，待事件锚定标定；变更触发 L2 重算 |
| `state_bands` / `state_labels` / `state_bands_calibrated` | 按市场占位 dict / 极稳~危机 / `false` | 第 3 步 | `false` 时整维恒不出档；标定后才切五档 | 须按事件锚定（非等频）标定并冻结；置 `true` 触发 `params_hash` 与 L2 全量重算 |
| `excluded_members` | 12 个编码 | 第 0 步 | 登记被移出的成员，**不计 `coverage` 分母** | 短样本 / 水平型漂移（输入段）；变更触发 `params_hash` 与 L2 重算 |

> 通用参数 `min_periods` / `coverage_min` / `dispersion_max` / `winsor`（未生效预留键）定义在 §2.4，本环节只引用名字、不重复数值；`weight_cap` 对分块结构不生效。跨资产对齐、AA 系短样本、缺口不补中性、`HK_MIN_AMOUNT` 与 `_coverage_gate` 都是 **L1 输入侧门禁**，不进 `dimension_dict.params` / `params_hash`。

**落地形态**（`build_dimension_value.py::build_stress_dimension`，v1.17 已落地）：

```python
for market, trade_date in calendar:
    ind = read(indicator_value, market, trade_date, members[].code)
    aligned = {c: (100.0 - p) if sign[c] == -1 else p for c, p in ind.percentile.items()}
    eligible = [c for c in members[].code
                if market_scope[c] in (market, 'CN+HK', 'GLOBAL')]
    core = [c for c in eligible if not flags_only[c]]

    core_valid = {c: aligned[c] IS NOT NULL and trade_date >= member_valid_from[market][c]
                  for c in core}
    core_coverage = count(core_valid) / len(core)
    if core_coverage < core_coverage_min: continue        # 整行不产出
    coverage = count(aligned WHERE c IN eligible) / len(eligible)
    dispersion = std(aligned[core])                       # 只描述，不作门禁

    blocks = {}
    for b in ('波动','相关性','信用','尾部'):
        vals = [aligned[c] for c in core WHERE block[c]==b and core_valid[c]]
        if vals: blocks[b] = {'score': mean(vals), 'coverage': len(vals)/expected(b), 'n': len(vals)}
        else: flags['empty_block'].append(b)              # 不写 blocks、不进分母
    score = clip(mean(blocks[b]['score'] for b in blocks), 0, 100)

    resonance_cnt = count(aligned[c] >= resonance_floor for c in core if core_valid[c])
    short = trade_date < max(member_confident_from[market][c] for c in core)
    confidence = '低' if short else '中'                  # 不出「高」
    state = NULL                                          # state_bands_calibrated = false
    write_one_row(dimension_value, PK=(dimension_code, market, trade_date),
                  score, state=NULL, coverage, dispersion, blocks,
                  contrib=正负各 top3,
                  flags={availability_tier, core_coverage, block_coverage,
                         dimension_completeness, resonance_cnt, resonance_floor,
                         dispersion_desc_only, excluded_members_count,
                         member_valid_from, frozen_from, short_sample?, low_coverage?},
                  confidence, model_version=dimension_dict.params_hash)
```

出档门禁见 §2.4.3；PIT、幂等、重算粒度、版本化见 §2.4.4–5（**参考样本集变更的第二类全量重算触发见 §2.2 / 规则 4**：压力是 expanding，历史回补会改变窗口内每一天的排位，不得只补新日期）。落库用 `skip_null_updates=False` + `only_if_changed=True`。`dimension_value` 没有 `quality` / `extra` 列，可用性一律用 `confidence` + `flags` 承载。生产者由 `build_all_dimensions()` 按 `dimension_code` 分派，不改现有四维生产者；`is_active = TRUE` 即自动接入日频（**常驻 scheduler 进程需重启后才生效**）。全历史命令：`build_dimension_value.py --dim DIM.STRESS --write --backfill`。

#### 输出

目标表 `regime.dimension_value`，字段字典见 §2.3.3。**库内真实行**（CN · 2026-11-04）：

```json
{
  "dimension_code": "DIM.STRESS",
  "market": "CN",
  "trade_date": "2026-11-04",
  "score": 60.86,
  "state": null,
  "coverage": 1.0000,
  "dispersion": 28.83,
  "blocks": {"波动":   {"score": 65.03, "coverage": 1.0, "n": 2},
             "相关性": {"score": 57.69, "coverage": 1.0, "n": 2},
             "信用":   {"score": 78.23, "coverage": 1.0, "n": 2},
             "尾部":   {"score": 42.50, "coverage": 1.0, "n": 2}},
  "contrib": {"pos": [{"code": "RISK.SYNC_DOWN_20D", "delta": 33.70},
                      {"code": "RISK.VIX_CHG20", "delta": 31.92},
                      {"code": "RISK.TERM_SPREAD", "delta": 22.17}],
              "neg": [{"code": "BREADTH.AVG_CORR_VW", "delta": -40.04},
                      {"code": "RISK.EXTREME_DOWN_PCT", "delta": -27.82},
                      {"code": "RISK.RV20", "delta": -23.58}]},
  "flags": {"availability_tier": "stress_core_fixed", "core_coverage": 1.0,
            "dimension_completeness": 0.8889, "block_coverage": 1.0,
            "resonance_cnt": 3, "resonance_floor": 80.0,
            "dispersion_desc_only": true, "excluded_members_count": 12,
            "frozen_from": "2012-02-14", "member_valid_from": "<8 个核心成员各自冻结日，见库内>"},
  "confidence": "中",
  "model_version": "17952671d0fa0438"
}
```

`score = (65.03 + 57.69 + 78.23 + 42.50) / 4 = 60.86`（4 块等权）；`coverage = 8/8 = 1.0`；`dimension_completeness = 8/9 ≈ 0.8889` 是**描述量**，不是 `coverage`；`dispersion = 28.83` 只描述、未触发任何门禁；`resonance_cnt = 3` 表示当日有 3 个核心成员处于自身历史 ≥80 分位；`state = null` 由 `state_bands_calibrated = false` 决定，只能读作「**未出档**」，不是「正常」（§2.4.6）。`flags.member_valid_from` 完整字典见库内，此处为节选占位。

未出档差异：`state = null`、`confidence = 低 / 中`，`score` 与 `blocks` 照出。维度层五档与 `BIZ.SYSTEMIC_STRESS` 四档（阈值 30 / 50 / 70）各自标定、档数不同，映射须写进业务层 `output_schema`，不得在本层混用。

**已产出读数**（`build_dimension_value.py --dim DIM.STRESS --write --backfill`）：

| 市场 | 行数 | 区间 | `state` | `confidence` | `model_version` |
|---|---|---|---|---|---|
| CN | 3577 | 2012-02-14 ~ 2026-11-05 | 全 NULL（出档 0） | 低 500 / 中 3077 | 全部 = `17952671d0fa0438` |
| HK | 2944 | 2014-11-18 ~ 2026-11-05 | 全 NULL（出档 0） | 低 500 / 中 2444 | 同左 |

现有四维（宽度 / 估值 / 情绪 / 资金）行数与 `model_version` 逐值未变；`--audit` 与全量 pytest 验证见附录 C。危机窗口描述性对照：2020-03 / 2022-03 / 2024-01 / 2025-04 两市场 `score` 均高于全期均值（CN +7.0~+16.7、HK +7.7~+25.3），2022-10 CN 仍为 −2.4（已登记限制，非有效性证明）。

下游接口（只声明数据字段，不规定展示）：`BIZ.SYSTEMIC_STRESS` 读 `score` + `blocks`（`trigger` 的唯一来源，块级门禁按有效块折算）；`BIZ.RISK_BUDGET` 的输入清单（§6）**不含本维度**，它读 `SYSTEMIC_STRESS.level`；`SIG.REVERSAL` 压力族（波动率从极低放大 / 冲高回落）**直接读 L1 原子 `percentile`**，不读本维度分；外部消费方读表时用 `model_version` 判口径——`state_bands` 标定或 M2 新增成员后必须递增，否则消费方无法区分新旧口径。

---

### 5.6 趋势 Trend · `DIM.TREND`

> **落地状态（v1.19 生效规格）**：本节已由设计契约转为生效规格。L1 三个 `TREND.*` 原子（`tools/recalc_trend_percentile.py`）、L2 字典种子（`tools/calibrate_trend.py`，`params_hash = 5038af6aebc31c43`）、生产者 `build_trend_dimension` 与回归测试 `tests/test_trend_dimension.py` 均已落地；全历史计算产出 **CN 7989 行 / HK 2752 行**，`state` 全 NULL、`model_version` 全部一致，现有五维行数与版本逐值未变。**纯 L2 + 按族 L1 改动**：新增 3 个原子并按 `TREND_CALIB_CODES` 隔离 `params_hash`，其余 123 个指标指纹与读数零变化；不改表结构、未触发 L1 全量重算。按用户约定未重启服务，日频自动接入需重启常驻 `market_scheduler` 进程（已在 CLI 完成全历史计算）。

#### 定义

回答「指数在往哪个方向走、走得有多陡」——**方向不在本层产出**，本维度只给出强度读数。`polarity = high_risk`（**只作刻度方向标签**：高分 = 趋势越陡；陡 ≠ 危险、≠ 偏多，也不构成买卖依据，同 §5.3 / §5.4）；`score` 为 0–100 的 **σ 归一化强度单锚的 expanding 分位**（不是原始 t 值、也不是「历史 X 分位」的绝对刻度）；`state` **规格上恒 NULL**，三态方向（上行 / 震荡 / 下行）由 `BIZ.MARKET_REGIME` 的状态机产出（§6）；日频落库；粒度 `(dimension_code, market, trade_date)`，`market ∈ {CN, HK}`，两侧序列长度与波动中枢不同（CN 36 年 / HK 12 年），**禁止横比、禁止拼接成一条曲线**。不回答参与广度（§5.1）、波动率水平（§5.5）、仓位 / 择时动作（§6）、股 vs 债性价比（第②层）。

**边界（不回答什么）**：① `score` 只承载「有多陡」，不承载「往哪走」，**强度与方向解耦**；② 方向判据直接读 L1 原子 `raw_value`，**不读本维度 `score`**（§6 / 研究稿 §5.8 ①）；③ 不产出预测值、不给仓位，不进 `BIZ.MARKET_TEMPERATURE` / `BIZ.RISK_BUDGET` / 信号族。

#### 输入

来源表 `public.daily_benchmark`，字段 `(bench_code, trade_date, last_price)`；三个 `TREND.*` 都是**标准 L1 原子**（`regime.indicator_value` 同时有 `raw_value` 与 `percentile`），由 `market_state_daily.py::_trend_atoms` 从指数收盘价现算。**v1.18 设计契约的 `NO_PERCENTILE` 破例已撤销**：`members[]` 统一读 `percentile`，不再需要「生产者读 `raw_value`」的特殊分支、`sign=null` / `state_bands=null` / 自定义 `blocks` 形状。

| 指标名称 / 成员编码 | 角色 | 定义 / 计算 | `unit` | 频率 | CN 起点 | HK 起点 | `market_scope` |
|---|---|---|---|---|---|---|---|
| σ 归一化趋势强度<br>`TREND.STRENGTH_T` | **锚**（`score` 唯一来源） | `\|TSMOM_12M\| / σ_252`，`σ_252 = std(日对数收益, 252 交易日) × √252`；σ=0 → NULL | `sigma` | 日 | 1993-01-04 | 2014-08-27 | `CN+HK` |
| 12 月时间序列动量<br>`TREND.TSMOM_12M` | 方向腿（`weight=0`） | `P_t / P_{t−252} − 1`，**价格收益、不减无风险利率** | `ratio` | 日 | 1993-01-04 | 2014-08-27 | `CN+HK` |
| 指数 MA200 偏离<br>`TREND.MA200_DEV` | 校验（`weight=0`） | `close / MA200 − 1`，MA200 = 200 **个交易日**简单均线 | `ratio` | 日 | 1993-01-04 | 2014-06-12 | `CN+HK` |

> `direction = neutral`（三个原子都不承载「高 = 好 / 坏」的价值判断），`sign = +1` 不翻转。删除 v1.18 的 `TREND.MOM_60D`：与 TSMOM 相关 >0.8，只作诊断纯属冗余计权。
> `σ_252` **不建独立原子指标**：由同一条收盘价现算，否则压力维度会再多一个「波动」成员（那里已有 `RISK.RV20` / `RISK.RV60`），属跨维重复计权（§5.5）。
> 最大回撤 `RISK.MAXDD_250` **归属压力维度**，按唯一归属规则不在本节重复计入（§5.5 / 研究稿 §5.0）。
> **L1 输入侧护栏 `TREND_ASOF_START`**：CN `1993-01-01`、HK `2013-08-20`（恒指实际起点）。护栏作用于**产出层**（早于该日 `raw_value` 置 NULL），200/252 日窗口仍在护栏前数据上计算——既保证起点当日窗口完整，又不把失真样本放进 expanding 参考分布。

**数据覆盖事实（2026-11-06 实测，覆盖率分母为 `trading_calendar` 开放日，统一截到 2026-10-29）**

| 项 | CN `SH.000001` | HK `HK.800000` |
|---|---|---|
| 指数序列 | 8757 行，1990-12-19 ~ 2026-11-05 | 3253 行，2013-08-20 ~ 2026-11-06 |
| 交易日历覆盖率 | **99.99%**（8752/8753，唯一缺 1993-01-03） | **100%**（3247/3247） |
| 字段质量 | `last_price` / `prev_close` 无 ≤0；`change_pct` 与 `last_price/prev_close−1` 偏差 >1pp 的行 **0** | 同左 |
| `close_20d_ago` | 99.77%（8737/8757），与 `LAG(last_price,20)` **零偏差** | 99.39%（3233/3253），**2026-10-27~11-06 共 9 行错位 5–13%** → 不作数据源 |
| L1 护栏后可输出 | 8238 行 / 原子（1993-01-04 起） | `MA200_DEV` 3054 行（2014-06-12 起）；`TSMOM_12M` / `STRENGTH_T` 3001 行（2014-08-27 起，252 日预热） |
| 分位口径 | expanding，`min_periods=0`（§2.4.1 默认；短样本由 L2 冻结起点护栏） | 同左 |

> ⚠ HK 交易日历只到 2026-10-29、指数数据到 11-06，覆盖率分母必须截到共同区间，否则会算出 >100% 的假覆盖。
> 实测复核：`2026-11-05` CN 的 `TREND.STRENGTH_T.raw_value = 0.391584`，与独立 SQL 复算的 t 值逐位一致。

**可用但未纳入维度分的其他数据**（归属约束，见 §5.0 唯一归属）：

| 数据 | 起点 / 覆盖 | 定位 |
|---|---|---|
| `RISK.RV20` / `RISK.RV60`（基于同两个指数） | CN 1991-01 / HK 2013-09；`raw_value` 零 NULL、分位 100% | σ 的**校验 / 备用**。实测用 `RV60/100` 作分母时 t P50 0.791 / 0.592、顶格率 8.49% / 1.47%，与 σ_252 接近但饱和略增，且 60 日窗口破坏「同窗口配对」→ 不作正式分母 |
| `BREADTH.PCT_ABOVE_MA200`、`MCCLELLAN`、`NEW_HIGH_LOW_60` | CN 1991-10 / HK 2000-01~10 | 已归 `DIM.BREADTH`，**不得进 `members[]`**；只能作 L3 `BIZ.MARKET_REGIME` 的 `detail.breadth_confirmation` 与下钻（§6.1 v2 不再调置信） |
| `SECTOR.UP_RATIO`（HK 由个股等权合成行业收益）、`SECTOR.MOM_20D_MEDIAN` | HK 2000-01 / CN 1999-12 | 行业趋势广度，同上；若将来要把 HK 趋势历史前推到 2000，需新建「等权市场指数」序列并处理幸存者偏差，属二期 |
| `a_daily_quote` / `hk_daily_quote` | CN 1990-12（8759 交易日）/ HK 2000-01（6632 交易日） | 全市场个股池，是上述长历史序列的底层源；不直接进本维度 |
| `HK.800700` 恒生科技、`SZ.399001`、31 个申万行业指数 | 2020-07 / 1991-04 / 1999-12 起 | 前者太短；后两者可作 CN 第二指数的稳健性对照，但换指数 = 换口径，须递增版本并重算 |

**维度指标注册（生效字典，节选；完整以库内 `dimension_dict` 为准）**  
对应表为 regime.dimension_dict   
```json
{
  "dimension_code": "DIM.TREND",
  "dimension_name": "趋势",
  "question": "指数往哪走 / 有多陡（高分=趋势越陡，与多空方向解耦）",
  "polarity": "high_risk",
  "members": [
    {"code": "TREND.STRENGTH_T", "sign": 1, "weight": 1.0, "block": "强度锚"},
    {"code": "TREND.TSMOM_12M", "sign": 1, "weight": 0.0, "block": "方向腿"},
    {"code": "TREND.MA200_DEV", "sign": 1, "weight": 0.0, "block": "校验"}
  ],
  "state_bands": {"CN": [20,40,60,80], "HK": [20,40,60,80]},
  "state_labels": ["极弱","偏弱","中性","偏强","极强"],
  "params": {"anchor_code": "TREND.STRENGTH_T", "sigma_win": 252,
             "tsmom_variant": "price", "no_state": true,
             "state_bands_calibrated": false, "core_coverage_min": 1.0,
             "index_code": {"CN":"SH.000001","HK":"HK.800000"},
             "trend_asof_start": {"CN":"1993-01-01","HK":"2013-08-20"},
             "member_valid_from": {
               "CN": {"TREND.STRENGTH_T":"1993-12-22", "TREND.TSMOM_12M":"1993-12-22",
                      "TREND.MA200_DEV":"1993-12-22"},
               "HK": {"TREND.STRENGTH_T":"2015-08-31", "TREND.TSMOM_12M":"2015-08-31",
                      "TREND.MA200_DEV":"2015-06-16"}},
             "member_confident_from": {
               "CN": {"TREND.STRENGTH_T":"1995-12-15", "TREND.TSMOM_12M":"1995-12-15",
                      "TREND.MA200_DEV":"1995-12-15"},
               "HK": {"TREND.STRENGTH_T":"2017-09-12", "TREND.TSMOM_12M":"2017-09-12",
                      "TREND.MA200_DEV":"2017-06-28"}}},
  "params_hash": "5038af6aebc31c43",
  "is_active": true
}
```

> `state_bands` / `state_labels` 是**占位但未标定**（`state_bands_calibrated = false`），`state` 恒 NULL 不是「门禁未过」也不是「中性」（§2.4.6）。`members[].weight` 是记录值，生产者按 `params.anchor_code` 取单锚，不按权重加权。HK 的 `MA200_DEV` 冻结日早于另两个成员（200 日预热 < 252 日），`confidence` 的短样本判定按**三成员中最晚**的 `member_confident_from` 取保守值。三个原子的 `indicator_dict.params_hash` 分别为 `STRENGTH_T = 743da7ae759de845` / `TSMOM_12M = 824a74f0ef9c1125` / `MA200_DEV = e53bed19c6d51c7e`。

#### 计算模型

**原理与背景**

1. **为什么不做等权平均**：三个成员来自同一条价格序列的不同窗口，两两相关普遍 >0.8；等权平均等于把同一份信息重复计权三次，违反「成分不得重复计权」前提（研究稿 §7.2 / 本文档 §5 章首「六维不共用一套聚合算法」）。故 `score` 只由一个锚给出，其余两项 `weight = 0`，只写 `contrib` / `blocks` / `flags`。
2. **为什么强度用 σ 归一化**：方向用 `sign(TSMOM)`、强度用 `TSMOM / σ`，是 AQR / Moskowitz-Ooi-Pedersen 时间序列动量一脉的标准做法，本质即 t 统计量 / 年化 Sharpe。三个候选的取舍：

   | 候选 | 问题 |
   |---|---|
   | 历史分位直接套在动量上 | 分位是横截面排序与多成分合成的工具；且 `\|·\|` 之后涨 / 跌两个**不对称**分布被压到同一尺度，会让「强涨」与「强跌」不可比 |
   | 固定绝对 scale（如 `\|MA200_DEV\| / 0.20`） | 常数不随波动率中枢漂移 → 同样偏离在高波年代读成满格、低波年代读成中等；且引入两个待标定参数 |
   | **σ 归一化** ✅ | σ 自适应 → 跨市场、跨时期、跨波动率区制可比；零标定参数；语义自然：「这段趋势相当于几个标准差」 |

   CN 的 σ_252 年均值从 1991 的 0.114 到 1993 的 0.832、2017 的 0.119（**7 倍漂移**），实证支持「必须自适应」。
3. **为什么把「固定 K 的线性刻度」换成「t 的 expanding 分位」（v1.19 核心整改）**：v1.18 用 `score = 100 × clip(t/K, 0, 1)`、`K = 3`。用真实序列复核（CN 8505 日 / HK 3001 日）后该刻度被证伪：

   | 实测项 | CN `SH.000001` | HK `HK.800000` |
   |---|---|---|
   | t 的 P50 / P90 / P95 / max | 0.703 / 2.342 / 3.049 / **32.18** | 0.599 / 1.624 / 1.922 / 3.954 |
   | `score@K=3` 均值 / P50 | 31.41 / 23.43 | **25.10 / 19.98** |
   | 顶格（`t ≥ 3`）比例 / 低于 20 分比例 | 7.41% / **43.05%** | 1.10% / **50.02%** |
   | 按年顶格：1991 / 1992 / 2007 / 2015 | **100% / 75% / 100% / 41%** | — |

   单边趋势年里价格位移远大于已实现波动，固定 K 被顶格；而 1993–96、2013 年 t 的 P50 只有 0.27–0.62（`score ≈ 10–20`）。**同一分值在不同年代含义完全不同**，「零标定参数、跨期可比」不成立；起点护栏只能把顶格率从 7.41% 降到 5.17%（1993 起），解决不了 2007 / 2015 的饱和；K 还必须按市场标定（t 的 P95：CN 3.05 / HK 1.92）。改为 expanding 分位后，每个时期都有完整 0–100 刻度，2007 的高分是真实历史极值而非模型失真，且**零标定参数**；`raw_value` 仍是 t，「几个标准差」的可读语义没有丢。落地实测 `score` 均值 CN 50.93 / HK 50.21、P50 51.17 / 48.33。
4. **为什么锚选 `STRENGTH_T` 而不是 `TSMOM_12M`**：维度只回答「有多陡」，`TSMOM_12M` 带方向，直接作 `score` 会让高分被读成「偏多」；取绝对值再 σ 归一化后无方向，方向单独由 L3 读 `raw_value` 判定。`STRENGTH_T` 与 `σ_252` 同为 252 交易日窗口，是 t 统计量配对正确的前提。
5. **为什么 CN 要 1993 起点护栏**：1991 / 1992 年上证刚开市，σ_252 年均值仅 0.114 / 0.557、t 的 P50 达 12.07 / 10.22。若让这段进 expanding 参考分布，会把 1993+ 的分位系统性压低。护栏在**产出层**置 NULL（窗口仍用护栏前数据算），与 §5.2 `VALUATION_ASOF_START`、§5.4 `FLOW_ASOF_START` 同款纪律。
6. **为什么方向不在维度层定义**：三态方向由 §6.1 的 `BIZ.MARKET_REGIME` 状态机（`sign(TSMOM)` + 强度分位门槛 + 确认天数 / 最小停留）产出；维度层再给一套 `state` 会出现「两层各有一套状态」。故 `state` 恒 NULL、强度与方向解耦。⚠ **v2 例外（2026-11-06）**：L3 用 `DIM.TREND.score > 30` 作无量纲死区，而该 `score` 就是 `TREND.STRENGTH_T.percentile` 的逐值拷贝（实测 10742/10742 相等），信息源仍是 L1 原子、未经平均稀释，不属于「用维度分做方向合成」；方向本身仍由 `sign(TSMOM_12M.raw_value)` 决定（§1.1 铁律①）。
7. **为什么动量用价格收益、不减无风险利率**：本层不建仓位、只描述价格趋势方向；减 `rf` 后 `sign(TSMOM)` 表达的是「是否跑赢现金」而非「指数往哪走」，且 CN 中债 10Y / HK 美债 10Y 中枢不同 → 同样的指数涨幅会在两市场判出不同状态，与跨市场同定义及 σ 归一化「跨市场可比」冲突。股息偏差（价格指数不含股息）的补法是**全收益指数**，不是减 `rf`；当前只有价格指数收盘价，显式记为已知偏差（不改变 `sign`、对强度只轻微低估）。若将来做 MOP 式回测，另建 `TREND.TSMOM_12M_EXCESS`，不进六维、不参与方向判定。
8. **为什么 `σ_252` 由收盘价现算、不建独立原子**：与 `TSMOM_12M` 同轴（都是年度收益量纲），t 统计量口径不变；另建原子会让压力维度多一个「波动」成员（§5.5）。

**指标计算方法（简述）**

核心公式（L1，`_trend_atoms` 现算）：

```
σ_252 = std(日对数收益, 252 个交易日) × √252
TSMOM_12M = P_t / P_{t−252} − 1
MA200_DEV = close / MA200(200 交易日) − 1
t        = |TSMOM_12M| / σ_252                  # σ=0 → NULL，不填 0
score    = TREND.STRENGTH_T.percentile          # L2，expanding 分位
```

0. **L1**：按 `index_code[market]` 读截至 `trade_date` 的全历史指数收盘价（PIT，`bench_code` 实体过滤 + 时间下界），现算三个 `raw_value`；序列 <200 / <252 交易日或 σ=0 的项置 NULL。
1. **L1 护栏**：早于 `trend_asof_start[market]` 的 `raw_value` 置 NULL，不进 expanding 参考分布；三个原子各自算 as-of 历史分位（§2.4 规则 1，expanding、中位秩）。
2. **L2 冻结起点门禁**：锚早于 `member_valid_from[market][anchor]`（第 250 个有效分位日）→ 不产行；`member_confident_from`（第 750 日）之前 `confidence` 恒「低」并写 `flags.short_sample`。
3. **L2 合成**：`score = aligned[anchor].clip(0,100)`；`sign=+1` 不翻转；`coverage` = 三个成员有效分位数 / 3；`dispersion` 恒 NULL（单锚）；`state` 恒 NULL（`state_bands_calibrated=false`）；校验项只写 `contrib` / `blocks` / `flags`，不改 `score`。
4. 落 `dimension_value`，`model_version = dimension_dict.params_hash`；锚缺失按**缺行**处理（与 §5.2 估值、§5.4 资金同款），不写 `score=NULL` 行、不填 0 或 50。

**本环节参数**（逐个对应上面的生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `anchor_code` | `TREND.STRENGTH_T` | 第 3 步 | 指定唯一进 `score` 的成员；**改变 `score` 序列** | 无方向且与 σ 同窗口；变更触发 `params_hash` + L2 全量重算 |
| `sigma_win` | 252 交易日 | L1 第 0 步 | σ 窗口，决定 t 分母与预热长度 | 与 `TSMOM_12M` 同窗口是配对正确前提；变更须递增 `TREND_CALIB_VERSION` 并重算 L1 三原子 + L2 + `BIZ.MARKET_REGIME` |
| `tsmom_variant` | `price` | L1 第 0 步 | 动量口径：价格收益、不减 `rf`；预留 `total_return` | 真源 `market_profile.md` §5.8 ③；`excess` 不作为本层取值；切换属口径变更，须重算 |
| `index_code` | CN `SH.000001` / HK `HK.800000` | L1 第 0 步 | 选定指数收盘序列；**换指数 = 换口径** | 两市场各固定一个，不随风格切换；变更须全量重算历史并同步 L3 |
| `trend_asof_start` | CN `1993-01-01` / HK `2013-08-20` | L1 第 1 步 | 分位参考样本起点；**不改变窗口计算，只挡失真样本入分布** | 实测标定（原理 5）；解除 = 口径变更，须递增版本并全量重算 |
| `member_valid_from` / `member_confident_from` | CN `1993-12-22` / `1995-12-15`；HK `2015-08-31` / `2017-09-12` | 第 2 步 | 冻结第 250 / 750 个有效分位日：前者之前不产行，之前 `confidence` 降「低」 | 由 `tools/calibrate_trend.py` 按 (market, code) 冻结，PIT 安全、不依赖 `extra.n_sample`；纳入 `params_hash` |
| `state_bands_calibrated` | `false` | 第 3 步 | `state` 恒 NULL（方向归 L3），**不改变 `score`** | 与 §5.3 / §5.4 / §5.5 同款机制；置 `true` 属口径变更，须重标切点并重算 |

> 通用参数 `coverage_min=0.6` / `dispersion_max=30` / `weight_cap=0.25` / `winsor` 对「单锚」不生效，只在字典中登记（§2.4）；`coverage` 只作描述量，不作产分门禁。
> ⚠ **参数分工**：**L3 方向参数**（`score_gate` / `confirm_days` / `min_dwell` / `turn_risk_rule` / `start_rule`，v2 无待标定项）真源在 **§6.1「本环节参数」表**；**强度侧参数**（`sigma_win` / `index_code` / `tsmom_variant` / 冻结起点）真源在**本节**。本节的 `dimension_dict.params_hash` 已纳入 `BIZ.MARKET_REGIME.params_hash`，改任一组都须触发该业务指标历史全量重算，不得只重算维度层（§2.2 / §6.1）。

**落地形态**（原理确定后的实现）：

```python
# L1 —— market_state_daily._trend_atoms（全历史读、按窗口 reindex）
s      = series(daily_benchmark, index_code[market], as_of=trade_date)
ret    = log(s / s.shift(1))
sigma  = ret.rolling(252, min_periods=252).std() * sqrt(252)
mom12  = s / s.shift(252) - 1
ma_dev = s / s.rolling(200, min_periods=200).mean() - 1
t      = abs(mom12) / sigma.where(sigma > 0)
raw    = {code: v.where(v.index >= trend_asof_start[market]) for code, v in raw.items()}
#   → indicator_value(raw_value=t, percentile=expanding_rank(t))，三个原子同法

# L2 —— build_dimension_value.build_trend_dimension
aligned = load_percentiles(market, members[].code, start, end)   # 通用管线，读 percentile
ready   = trade_date >= member_valid_from[market][anchor_code]
score   = aligned[anchor_code].where(ready).clip(0, 100)
if score is NaN: continue                                        # 锚缺失 = 缺行，不填 0
coverage = aligned.notna().sum(axis=1) / 3
dispersion = None
state      = None                                                # state_bands_calibrated = false
confidence = '中' if not short_sample else '低'
blocks     = {block: {'score': mean(该块成员分位), 'coverage': 有效数/块内数}}
write_one_row(dimension_value, PK=(dimension_code, market, trade_date),
              model_version=params_hash)
```

`state` 恒 NULL 由 `state_bands_calibrated=false` 决定，**不走 §2.4.3 出档门禁**；`coverage` / `dispersion` 不门禁、不改 `score`。PIT、幂等、重算粒度与版本化见 §2.4.4–5。

**口径约束**：① MA200 是 200 **个交易日**（约 290 自然日），不是 200 天；② 指数本身无复权，改个股 / ETF 口径须先定复权方式；③ 不读 `close_20d_ago`（HK 最近 9 行错位），需要 20 日动量一律用 `LAG(last_price,20)` 自算；④ `score` 是**分位读数**不是 t 值，不得按「X = 几个标准差」解读（那要读 L1 `raw_value`）；⑤ 1993 年仅 8 个交易日进入发布区、分位样本刚满 250，`confidence = 低` + `flags.short_sample`，早期读数只作趋势参考；⑥ CN / HK 样本长度 36 年 / 12 年，禁止横比或拼接成一条曲线。

#### 输出

目标表 `regime.dimension_value`，字段字典见 §2.3.3。

```json
{
  "dimension_code": "DIM.TREND",
  "market": "CN",
  "trade_date": "2026-11-05",
  "score": 28.27,
  "state": null,
  "coverage": 1.0000,
  "dispersion": null,
  "blocks": {"强度锚": {"score": 28.27, "coverage": 1.0},
             "方向腿": {"score": 37.10, "coverage": 1.0},
             "校验":   {"score": 23.14, "coverage": 1.0}},
  "contrib": {"pos": [{"code": "TREND.TSMOM_12M", "delta": 8.83}],
              "neg": [{"code": "TREND.MA200_DEV", "delta": -5.13}]},
  "flags": {"availability_tier": "trend_single_anchor",
            "anchor_code": "TREND.STRENGTH_T", "anchor_window": "expanding",
            "sigma_win": 252, "tsmom_variant": "price", "no_state": true,
            "trend_asof_start": "1993-01-01",
            "member_valid_from": {"TREND.STRENGTH_T": "1993-12-22",
                                  "TREND.TSMOM_12M": "1993-12-22",
                                  "TREND.MA200_DEV": "1993-12-22"}},
  "confidence": "中",
  "model_version": "5038af6aebc31c43"
}
```

**库内真实行**（非示意）：`score = 28.27` 即当日 `TREND.STRENGTH_T.percentile`（`raw_value = 0.391584`）；`blocks` 按 §2.3.3 标准形状 `{块名:{score,coverage}}` 落库，**不需要** v1.18 计划的自定义 `blocks` 形状与 DDL COMMENT 改动。`state = null` 是**规格上不出档**（方向归 L3），不是「中性」也不是门禁未过（§2.4.6）；`dispersion = null` 是单锚结构，不是缺数；`contrib` 的 `delta` 是校验项分位与 `score` 的偏离，**不参与加权**。

**已产出读数**（`tools/recalc_trend_percentile.py --write` → `tools/calibrate_trend.py --write` → `build_dimension_value.py --dim DIM.TREND --write --backfill`）：

| 层 | CN | HK |
|---|---|---|
| L1 三原子 | 各 8238 行（1993-01-04 ~ 2026-11-05），共 24714 行 | `MA200_DEV` 3054 行（2014-06-12 起）、`TSMOM_12M` / `STRENGTH_T` 各 3001 行（2014-08-27 起），共 9056 行 |
| L2 `DIM.TREND` | 7989 行（1993-12-22 ~ 2026-11-05） | 2752 行（2015-08-31 ~ 2026-11-06） |
| `state` / `dispersion` | 全 NULL / 出档 0 日 | 同左 |
| `score` 均值 / P50 | 50.93 / 51.17 | 50.21 / 48.33 |
| `model_version` | 全部 = `5038af6aebc31c43` | 同左 |

现有五维（宽度 / 估值 / 情绪 / 资金 / 压力）行数、`state` 出档数与 `model_version` 逐值未变；`tools/recalc_trend_percentile.py` 按 `TREND_CALIB_CODES` 隔离 `params_hash`，其余 123 个指标指纹零变化（落地前已逐条比对）。

下游接口（只声明数据字段，不规定展示）：`BIZ.MARKET_REGIME`（**§6.1 v2 已落地**）直接引用 `DIM.TREND.score` 写 `business_value.score`，并以 `score > 30` 作强度门槛、`sign(TREND.TSMOM_12M.raw_value)` 作方向；`TREND.MA200_DEV.raw_value` 只作 `turn_risk` 与 `evidence`；`DIM.BREADTH.state` 只写 `detail.breadth_confirmation`（不调置信）。⚠ 本节 `score` 是 L1 `TREND.STRENGTH_T.percentile` 的逐值拷贝（实测 10742/10742 相等），用作门槛不构成「维度分参与方向合成」，方向仍由原子决定（§1.1 铁律①）。`BIZ.RISK_BUDGET` 的 `direction` 约束默认关闭（§6.1）；`BIZ.MARKET_TEMPERATURE` 的四个分量**不含** `DIM.TREND`；`SIG.REVERSAL` 无趋势消费者。外部消费方读表时用 `model_version` 判口径 —— 改 `anchor_code` / `sigma_win` / `index_code` / `tsmom_variant` / 冻结起点 / 护栏必须递增 `dimension_dict.params_hash` 并**同步触发 `BIZ.MARKET_REGIME` 重算**（§6.1 已把该指纹纳入 `params_hash`），否则消费方无法区分新旧口径。

---

## 6 L3 业务指标

四个对象共用 §3 模板；阈值、状态机与切档参数一律存 `business_dict.params`，标定后冻结并纳入 `business_dict.params_hash`（变更的重算范围见 §2.2）。下表「计算模型」列即各对象的**指标计算方法简述**，原理紧随表后，实现细节见 `business_dict.params` 与各计算模块。

| 业务指标（名称 · 编码） | 定义 | 输入 | 计算模型 | 输出 |
|---|---|---|---|---|
| 市场方向 · `BIZ.MARKET_REGIME` | 指数处于什么趋势状态（描述性、非预测）；三态状态机 | `TREND.TSMOM_12M` / `TREND.MA200_DEV` 的 `raw_value` + `DIM.TREND.score` + `DIM.BREADTH.state` | `sign(TSMOM)` × `score>30` 分位死区 + 确认天数 + 最小停留；累计 `dwell` / `streak`，**必须全量重算**；**§6.1 已落地** | `state`、`score`、`since_date`、`dwell_days`、`status`、`confidence`、`detail.trend_state` / `turn_risk` |
| 市场温度 · `BIZ.MARKET_TEMPERATURE` | 相对冷热位置；`descriptive`，不驱动任何 `cap` | 四个 `DIM.*.score`（估值 / 情绪 / 资金 / 宽度）| 各维先转自身历史 as-of 分位 `u_i` 再等权平均；按冻结的 P20/P40/P60/P80 切五档 | `score`（分位均值，**非分位读数**）、`level`、`state`；`detail` 必须带 `components` 与 `quantiles` |
| 系统性压力 · `BIZ.SYSTEMIC_STRESS` | 系统稳不稳 | `DIM.STRESS.score` + `dimension_value.blocks` | 压力是共振型故障，按块级读数判 `trigger`，不只报最大块 | `level`（1–4）、`state`、`score`、`detail.trigger` / `spike` / `blocks` |
| 风险预算 · `BIZ.RISK_BUDGET` | 最多能承担多少风险；本层唯一动作出口 | `MARKET_REGIME.state`、`vol_state`、`SYSTEMIC_STRESS.level`、`DIM.VALUATION.score`（可选）、§4.2 跨资产确认度 | 多约束取 `min` → 档位映射 → `SWITCH_DAYS` 防抖；输出 `second_binding` 避免方向约束吞掉其余信息 | `score = cap×100`、`level`、`detail.binding_constraint` / `raw_cap` / `missing` |

**计算模型的原理（为什么是这种形态）**

- **市场方向**用**状态机**而非单阈值：趋势方向是带时间语义的状态，日频噪声会让阈值反复翻转。强度分位死区（`DIM.TREND.score > 30`，§6.1 v2）过滤边界抖动、确认天数要求证据连续、最小停留防止频繁换态；`dwell` / `streak` 是「进入本状态以来」的累计量，因此必须全量重算（§2.4.5）。⚠ 该状态**不是方向预测**：实测上行/下行前瞻收益分不开、下行反而最高，故 `RISK_BUDGET` 的 `direction` 约束默认关闭（§6.1 实测验收）。
- **市场温度**先把各维 `score` 转成自身 as-of 分位 `u_i` 再等权：六维分位基数、聚合结构、语义都不同，直接平均会因「单锚分位」与「多次平均分位」的方差差异产生隐式权重（§2.4.7）。合成值是**分位的均值**（分布向 50 集中），不是分位读数，不能按「X = 历史 X 分位」解读。
- **系统性压力**是**共振型**而非极值型故障：3 个块同时紧张与 1 个块爆表，风险性质不同，只报最大块会把两者混为一谈。故按 `dimension_value.blocks` 判 `trigger`，并保留 `spike` / `blocks` 明细。
- **风险预算**取 `min` 而非连乘：连乘让约束叠加放大（0.6 × 0.7 × 0.85 = 0.36），档位掉到无法解释的低位；`min` 对应「最紧的约束决定上限」，且可回答「被什么卡住的」。方向=下行时常把其余约束吞掉，故额外输出 `second_binding` 使被掩盖的信息可见。
- 信号按**族**去重计票：顶部清单里同族条目高度相关（「PE 贵 + ERP 低 + 换手高 + 基金爆款」往往只是一轮过热的四种表述），按条数累加会把一轮普通过热冲成「高风险」。`family_cnt` 计的是「几个不同侧面同时报警」。

**横切信号** 反转风险信号 `SIG.REVERSAL`：布尔条件清单，直接读 L1 原子分位，按族去重后计 `family_cnt`；命中记录写 `signal_value`（`days` 必填）。信号不进任何维度分或业务指标。

> ⚠ `level` 与 `score` 都是多态列，四个 `biz_code` 语义互不相容；消费方须按 `biz_code` 过滤并校验 `output_schema`（§2.3）。

---

### 6.1 市场方向 · `BIZ.MARKET_REGIME`

> **v2（2026-11-06 整改，已落地）**：本节由 `market_profile.md` §5.8 迁入后，按库内实测重新设计并实现，单文件生产者 `build_business_value.py`（L2 风格 `if/elif` 分派），验收 `tools/validate_regime.py`、回归 `tests/test_market_regime.py` 19 项。v1 的「两腿合取 + 双死区 + 三维网格标定」已撤销，理由与实测数字见「原理与背景」及「实测验收」。四个 `BIZ.*` 中仅本对象有生产者，其余三个仍以章首索引表为准（附录 D）。

#### 定义

回答「指数当前处于什么趋势状态」——**本层唯一的趋势状态机**，输出**描述性滞后状态，不是方向预测**。`kind = state_machine`（三态）；`state` = 上行 / 震荡 / 下行，`level` = 1=下行 / 2=震荡 / 3=上行（升序 = 越偏多，**仅为序数**）；`score` = 趋势强度 0–100，与方向解耦、直接引用 `DIM.TREND.score`（§5.6），不重算；日频落库；粒度 `(biz_code, market, trade_date)`，`market ∈ {CN, HK}`，两市场**同定义、同参数、禁止横比**。

**定位与下游约束（v2 核心整改）**：实测三态后续 6 月指数收益为 CN 下行 **+7.56%** / 上行 **+6.04%** / 震荡 +0.30%，HK +3.38% / +1.85% / −1.33% —— **上行与下行分不开，下行反而最高**，趋势延续假设不成立。故 `BIZ.RISK_BUDGET` 的 `direction` 约束**默认关闭**（`optional=true`）；**不得把 `state=下行` 解读为看空或减仓信号**。本指标可用的信息是「趋势是否确立 / 转折风险」：`detail.trend_state` 与 `detail.turn_risk`。

**边界（不回答什么）**：① 不输出预测值；② 不给仓位（`BIZ.RISK_BUDGET`，§6 章首）；③ 不区分波动高低；④ 不另算趋势强度口径；⑤ 不回答未来涨跌。

| 项 | 内容 |
|---|---|
| 编码 | `BIZ.MARKET_REGIME`（沿用，避免破坏表注释与消费方白名单） |
| 类型 | `kind = state_machine`；参数 5 项，**无待标定参数**，`params_hash = 0ccb0176f74177e5` |
| 生产者 | `build_business_value.py`（单文件，`--seed / --probe / --write / --biz`，`build_regime_business` + `if/elif` 分派）；**日频由 `market_state_daily.run()` 在 L2 之后调用 `build_all_business()`**（`market_scheduler` 工作日 18:45），也可独立 CLI 手动重算；10742 行 |
| 业界依据 | Faber (2007) MA200 战术择时 / MOP (2012) 时间序列动量 → 只取「平滑均线 + 端点敏感动量」的概念与 σ 归一化思路；本节不建多空组合、不做波动率缩放、无转移矩阵；HMM 路线归 `market_profile.md` §8.5 |

#### 输入

来源表 `regime.dimension_value`（`DIM.TREND.score` / `DIM.BREADTH.state`）与 `regime.indicator_value`（`TREND.TSMOM_12M` / `TREND.MA200_DEV` 的 `raw_value`）。全部输入均已真实落地，**零新增数据源**。

| 输入 | 口径 | 缺失 / 不足时的降级 |
|---|---|---|
| `DIM.TREND.score` | `TREND.STRENGTH_T` 的 expanding 分位（§5.6）；**同时充当强度门槛与输出 `score`** | 无该行 → **整行不发布**（起点由其决定）；不得填 0 / 50 |
| `TREND.TSMOM_12M.raw_value` | 12 月价格收益（不减 rf），**方向读数** | NULL → 整行不发布 |
| `TREND.MA200_DEV.raw_value` | `close / MA200 − 1`，仅用于 `turn_risk` 与 `evidence` | NULL → `turn_risk=false`、`evidence.ma200_dev=NULL`，**不影响 `state`** |
| `DIM.BREADTH.state` | 宽度五档（§5.1），仅写 `detail.breadth_confirmation` | 不出档 → `不可用`；**不调 `state`、不调 `confidence`** |

> **σ_252 不重算，也不引入新原子**：`STRENGTH_T = |TSMOM|/σ`，故 `TSMOM/σ = sign(TSMOM) × STRENGTH_T`；`DIM.TREND.score` 就是 `TREND.STRENGTH_T.percentile` 的逐值拷贝（**实测 10742/10742 行完全相等，`>30` 判定 10742/10742 一致**）。因此 `score > 30` 等价于「`|t|` 超过自身历史 30% 分位」的自适应死区，信息源仍是 L1 原子、未经过任何平均稀释，不违背 §1.1 层间铁律①；§5.6 的「`score` 不参与方向判定」应读作「不用被稀释的维度分做方向合成」，v2 的例外见该节下游接口注。
> **起点（消除 v1 错配）**：以 `DIM.TREND` 有效行 inner join 为基底 → CN `1993-12-22`（7990 行）、HK `2015-08-31`（2752 行）。v1 从 raw 首次非空起算，导致 CN ~240 / HK ~250 个交易日「有 state 无 score」，且恰是初始化待确认段。
> **统一降级（A8）**：输入缺失一律「退出计算 + 标注」，不得静默按中性处理。

本指标参数定义在「计算模型」的「本环节参数」表；下方是 `business_dict` 的一行**字典种子**（物化形态，不是定义）。

**业务指标注册**  
对应表为 `regime.business_dict`（已落库）  
```json
{
  "biz_code": "BIZ.MARKET_REGIME",
  "biz_name": "市场方向",
  "kind": "state_machine",
  "inputs": [
    {"kind": "atom", "code": "TREND.TSMOM_12M", "role": "direction"},
    {"kind": "atom", "code": "TREND.MA200_DEV", "role": "evidence_only"},
    {"kind": "dimension", "code": "DIM.TREND", "role": "strength_gate_and_score"},
    {"kind": "dimension", "code": "DIM.BREADTH", "role": "detail_only"}
  ],
  "params": {"score_gate": 30, "confirm_days": 5, "min_dwell": 20,
             "direction_atom": "TREND.TSMOM_12M", "strength_dim": "DIM.TREND",
             "turn_risk_rule": "sign_ma_ne_sign_mom",
             "start_rule": "inner_join_dim_trend",
             "upstream_dim_trend_params_hash": "5038af6aebc31c43"},
  "params_hash": "0ccb0176f74177e5",
  "output_schema": {
    "level_semantics": "1=下行 / 2=震荡 / 3=上行（升序=越偏多；仅为序数，不构成方向预测）",
    "score_semantics": "趋势强度 0-100，与方向解耦；直接引用 DIM.TREND.score",
    "state_semantics": "描述性趋势状态（滞后），不是方向预测；下游 direction 约束默认关闭",
    "detail_keys": ["candidate", "evidence", "breadth_confirmation", "trend_state", "turn_risk"]
  },
  "is_active": true
}
```

> `params_hash` 覆盖 `inputs` / `params` / `output_schema` + `upstream_dim_trend_params_hash`（§5.6 口径指纹）→ §5.6 变更自动换指纹并触发本指标全量重算，不得只重算维度层（§2.2）。
> 不再有 v1 的 `DEADBAND`（按市场标定）、δ、`CONF_BASE`、`MA200_WIN` / `TSMOM_WIN` / `TSMOM_VARIANT`（后三者真源在 §5.6）；**无「待标定」参数**，不做收益网格标定。

#### 计算模型

**原理与背景**

1. **为什么撤销两腿合取**：实测 `corr(MA200_DEV, TSMOM_12M)` CN **0.750** / HK **0.773**，两腿非零时同号 **86.6% / 90.0%**；合取只是把单一趋势信号的阈值调严，不产生独立信息（§5.6 原理 1）。v1 却为此引入 `DEADBAND`（按市场 2 个）+ δ 共 3 个待标定参数。
2. **为什么死区改为强度分位**：v1 的 `|TSMOM| > δ·σ_252` ≡ `|t| > 0.25`，而 `|t|` 中位 CN 0.703 / HK 0.599 → 只挡 **17.6% / 23.2%** 交易日，防抖形同虚设。`score > 30` 是同一条序列的 expanding 分位，波动自适应、无量纲、跨市场同参数、零标定，PIT / 幂等 / 版本化由 §5.6 保证；与显式 `expanding quantile(|t|, 0.30)` 的 raw 一致率 CN 97.2% / HK 90.2%（差异仅来自经验秩与线性插值的分位口径）。
3. **为什么方向与强度正交分解**：`sign(TSMOM)` 只回答「往哪」，`score` 只回答「够不够强」，二者合起来即 `z = TSMOM/σ` 的有符号 t 判据；不再有两个相关条件互证，也不重复计权。
4. **为什么状态机参数固定为先验值、不做收益网格**：v1 的 `(DEADBAND, N, M)` 三维网格 + 扣成本收益目标与「描述性状态」定位冲突，且在有限切换事件上必然过拟合。实测 `N=5, M=20` 已同时通过 §8.1 第 3 条（游程中位 CN 38 / HK 27 日）与第 5 条（状态机增量滞后中位 4 日、max 18 日、`>30 日` 占比 0）；B2 交互顺序有效，研究稿担心的 `M+N/2≈22` 日叠加滞后并未发生。
5. **为什么 v1 的样本约束作废**：v1 写「HK 宽度类数据 2019 起约 7.5 年 ≈ 1800 交易日、切换 10–30 次、<20 次沿用 CN 参数」。方向状态机吃的是指数收盘：实测 HK **3001 交易日（12.2 年）**、`N5M20` 下 **38 次切换**（CN 120 次）；「<20 次沿用 CN」分支不会触发，「按市场分别标定」又与该分支自相矛盾。v2 两市场同参数、不分别标定。
6. **为什么 `transition` 升级为可消费字段**：v1 把两腿反向子型并入「震荡」，与 `range_bound` 共用 state / cap，下游只读 state → 最有信息量的风险信号被淹没。v2 用零参数的 `turn_risk`（MA200 与动量符号背离），实测 f120 CN **−2.35%** / HK **−2.73%**（全样本 +5.0% / +1.4%）。
7. **为什么不做「趋势 × 波动」交叉态**：职责越界（规模归预算层）、频率不匹配、样本不足三条仍成立；按 HK 3001 日重算 9 态最小格子 ≈ 87（下行态 26.2% 再分三档），仍低于 §2.4 规则 1 的 250 观测门槛。
8. **为什么必须全量重算**：`dwell` / `streak` 是自进入本状态以来的累计量；「30 日缓冲」会从 `state=None` 起步把 `dwell` 重置为 1、锁死切换（§2.4 规则 5）。规模仅 1.1 万行，全量重算 2.28s / 峰值 77.8MB。

**指标计算方法（简述）**

核心公式（三层，逐交易日递推）：

```
raw = 上行  if TSMOM_12M > 0 且 DIM.TREND.score > 30
      下行  if TSMOM_12M < 0 且 DIM.TREND.score > 30
      震荡  其余（强度不足 / 动量为 0）
```

1. **第一层**：按上式判当日 `raw`，不看历史；任一必需输入缺失 → 整行不发布。
2. **第二层**：状态机递推（初始化 `dwell=1` 含当日；`MIN_DWELL` 只阻断切换、不阻断计数；仅连续指向同一候选才累计 `streak`），细节见「落地形态」。
3. **第三层**：`turn_risk = sign(MA200_DEV) × sign(TSMOM) == −1`；`trend_state = trend / range`；`breadth_confirmation` 四类；`confidence` 仅在宽度不出档或 `turn_risk` 时标「低」，否则「中」—— **均不改 `state`**。
4. **落库**：`score` 直接引用、`model_version = business_dict.params_hash`、`detail` 五键齐全；`only_if_changed=True` 保证幂等（重跑 10742 行、`updated_at` 逐值不变）。

**本环节参数**（逐个对应生效步骤）

| 参数 | 值 | 生效步骤 | **作用** | 取值依据 / 变更后果 |
|---|---|---|---|---|
| `score_gate` | 30 | 第 1 步 | 强度门槛；**只改 `raw` 判定，不改 `score`** | expanding 分位、跨市场同参数、零标定；变更触发 `params_hash` + 历史全量重算 |
| `confirm_days` (N) | 5 | 第 2 步 | 新状态需连续 N 日成立 | 实测增量滞后中位 4 日 / max 18 日，通过 §8.1 第 5 条；变更改变 `since` / `dwell` / `status` 全链 |
| `min_dwell` (M) | 20 | 第 2 步 | 最小停留，**只阻断切换、不阻断计数** | 实测游程中位 CN 38 / HK 27 日，通过 §8.1 第 3 条；同上 |
| `turn_risk_rule` | `sign_ma_ne_sign_mom` | 第 3 步 | 转折风险标记（零参数） | 实测 f120 分离（原理 6）；变更影响 `detail.turn_risk` 与 `confidence` |
| `start_rule` | `inner_join_dim_trend` | 输入 | 起点由上游有效行决定 | 消除 state / score 错配；起点变化须全量重算 |

> **无待标定参数**：以上均为先验值并由 `tools/validate_regime.py` 按 §8.1 第 1/3/5/7 条实测验收，不做 `(DEADBAND,N,M)` 收益网格。

**落地形态**（原理确定后的实现，见 `build_business_value.py::build_regime_business`，由 `build_all_business` 分派）：

```python
for market in ("CN", "HK"):
    f = load_frame(conn, market)   # inner join DIM.TREND 有效行 → 起点自动对齐
    valid = [(r, raw_state(r.tsmom, r.trend_score, 30)) for r in f if raw is not None]
    vectors = state_machine([raw for _, raw in valid], [dates], N=5, M=20)
    for (r, raw), v in zip(valid, vectors):
        write_one_row(
            business_value,
            PK=("BIZ.MARKET_REGIME", market, r.trade_date),
            state=v.state, level={"下行": 1, "震荡": 2, "上行": 3}[v.state],
            score=r.trend_score,                       # 直接引用，不重算
            since_date=v.since, dwell_days=v.dwell, status=v.status,
            confidence="低" if (宽度不可用 or turn_risk(r.ma, r.tsmom)) else "中",
            detail={"trend_state": "trend" if v.state != "震荡" else "range",
                    "turn_risk": ..., "candidate": ...,
                    "breadth_confirmation": ..., "evidence": ...},
            model_version=params_hash)
```

**边界与并列**

| 项 | 规则 |
|---|---|
| **初始化（B1）** | 首日 `state=raw`、`since=首日`、`dwell=1`、`status=待确认`；满 N 日转「已确认」 |
| **待确认时的 `state`（C2）** | 消费端读 `state` **一律不判断 `status`**。v1 的 C2 保守规则撤销：它只覆盖初始化 5 日，而真正高风险的是「候选已出现、state 未切换」期间；后者现由 `detail.candidate` 与 `turn_risk` 暴露 |
| **幂等（B4）** | **必须全量重算**，或从上一日落库行恢复完整状态向量 `(state, since, dwell, streak, candidate, status)`；`only_if_changed` 保证重复跑逐字段不变 |
| **并列 / 退化** | 三态互斥；`raw` 恒为三态之一，不存在空值 |
| **分数语义（A3）** | `score` = `DIM.TREND.score`，高分 = 趋势陡（无论上下）；方向只看 `state` |
| **监控阈值** | 游程 P95 按市场实测冻结：**CN 284 / HK 275** 交易日，不再用 v1 的 250 占位（HK 实测 P95 267 已超过 250，会常态误报） |

#### 输出

目标表 `regime.business_value`，字段字典见 §2.3.4。

| 输出字段 | 含义 | 落表列 |
|---|---|---|
| `state` | 上行 / 震荡 / 下行（当前生效状态） | `state` |
| `level` | 1=下行 / 2=震荡 / 3=上行（仅为序数） | `level` |
| `score` | 趋势强度 0–100（与方向解耦）= `DIM.TREND.score`，不重算 | `score` |
| `since` / `dwell_days` | 状态首次进入日 / 已停留交易日数（含当日） | `since_date` / `dwell_days` |
| `status` | 已确认 / 待确认（消费端不判断，见 C2） | `status` |
| `confidence` | 中 / 低（仅宽度不可用或 `turn_risk` 时低） | `confidence` |
| `detail.trend_state` | `trend` / `range` —— **实测唯一分离稳定的二分** | `detail` |
| `detail.turn_risk` | bool，MA200 与动量符号背离 | `detail` |
| `detail.candidate` | `{to, progress: streak/N}`，无候选为 `{}` | `detail` |
| `detail.breadth_confirmation` | 一致 / 中性 / 背离 / 不可用（仅描述） | `detail` |
| `detail.evidence` | `{tsmom_12m, ma200_dev, trend_score, score_gate, raw_state}`（当日原始读数） | `detail` |

```json
{
  "biz_code": "BIZ.MARKET_REGIME",
  "market": "HK",
  "trade_date": "2026-11-06",
  "score": 28.33,
  "state": "上行",
  "level": 3,
  "since_date": "2026-09-28",
  "dwell_days": 27,
  "status": "已确认",
  "confidence": "中",
  "detail": {
    "trend_state": "trend",
    "turn_risk": false,
    "candidate": {"to": "震荡", "progress": 0.4},
    "breadth_confirmation": "一致",
    "evidence": {"tsmom_12m": 0.012, "ma200_dev": 0.021,
                 "trend_score": 28.33, "score_gate": 30, "raw_state": "震荡"}
  },
  "model_version": "0ccb0176f74177e5"
}
```

样例可读作：状态仍是「上行」（已确认、已停留 27 日），但当日强度分位 28.33 ≤ 30 → `raw = 震荡`，已连续 2 日指向同一候选（`progress = 2/5`，尚未切换）。`score = 28.33` 是当日 `DIM.TREND.score` 的引用值，与方向无关。

> ⚠ **两组字段时间语义不同**：`state` / `level` / `since_date` / `dwell_days` / `status` / `detail.candidate` 是**状态机输出**（带滞回、可能停留在旧状态）；`detail.evidence.raw_state` / `turn_risk` / `breadth_confirmation` 是**当日原始读数**。`state=上行` 与 `raw_state=震荡` 同时出现是合法的，不得据此判为数据错误。
> `state` 恒为三态之一，无「未出档」NULL 语义；`score IS NULL` 不会发生（起点已对齐上游有效行）。

**失效与监控**

| 情形 | 处置 |
|---|---|
| 上游断供 / 起点之前 | **整行不发布**；`monitor_table_config` 已登记 `business_value`（`active=TRUE` / `time_column=trade_date` / `day/1`）—— ⚠ 该表 `only_if_changed=True`，状态不变时 `updated_at` 不刷新，故按业务日期判新鲜度，用 `updated_at` 会误报断供；日频由 `market_state_daily.run()` 在 L2 之后触发 |
| 宽度不出档 | 照常发布，`detail` 写「不可用」，不改 `state` / `confidence` 以外的字段 |
| `state` 久未变 | `dwell_days` 超过按市场实测的游程 P95（CN 284 / HK 275）→ 提示人工复核 |
| 当日值非终值 | 输入全是收盘即可确定的纯价格派生量，T 日收盘后即终值；无 `is_final` 列（附录 D v2.1.12），口径新旧由 `model_version` 判定；历史全量重算只由 `params_hash` 或上游参考样本集变更触发（§2.2） |

下游接口（只声明数据字段，不规定展示）：`BIZ.RISK_BUDGET` 的 `direction` 约束**默认关闭**（`optional=true`），启用前须通过 §8.1 第 1 条 + block bootstrap 检验；可消费 `detail.trend_state = range` / `turn_risk = true` 作**不确定性约束**（不加仓），而非方向性 cap。`BIZ.MARKET_TEMPERATURE` 的四个分量不含本指标。外部消费方须按 `biz_code` 过滤并用 `model_version` 判口径。

**实测验收（2026-11-06，`tools/validate_regime.py --bootstrap 500`）**

| 项 | CN | HK |
|---|---|---|
| 落库行数 / 起止 | 7990 / `1993-12-22 ~ 2026-11-06` | 2752 / `2015-08-31 ~ 2026-11-06` |
| 切换次数 / 游程 中位·P95 | 111 / 38·284 | 40 / 27·275 |
| 状态机增量滞后 中位·max / `>30 日`占比 | 4·18 / 0% | 4·16 / 0% |
| 三态占比 上行·下行·震荡 | 41.2 / 32.4 / 26.4% | 35.2 / 33.7 / 31.0% |
| 三态 f120 均值 上行·下行·震荡 | +6.04 / +7.56 / +0.30% | +1.85 / +3.38 / −1.33% |
| 两态 f120 均值 trend·range | +6.72 / +0.30% | +2.60 / −1.33% |
| 两态 f120 均值差 block-bootstrap 95% CI | **(0.0031, 0.1239)** 不含 0 | **(−0.0298, 0.1010)** 含 0 |
| `turn_risk` 占比 / f120 均值 | 20.7% / −2.35% | 24.2% / −2.73% |
| `confidence` 中·低 | 66.5 / 33.5% | 71.5 / 28.5% |

> ⚠ **未通过的验收项须留痕**：① §8.1 第 1 条「三态前瞻收益分离」**未通过**——上行与下行分不开，下行反而最高，这正是 `direction` 约束默认关闭的理由；② block bootstrap（第 7 条）下**仅 CN 的两态分离显著，HK 含 0**，HK 只能记为描述性；③ 第 4 条拐点对照未通过——HK `2021-02-18` 顶部后 60 交易日仍为「上行」、CN `2015-06-12` 顶部仍为「上行」（MA200 类信号的固有识别滞后，§8.1 第 5 条已规定「趋势识别滞后只报告、不设硬上限」）。④ 全部为样本内重叠窗口、未扣成本、未做样本外检验，**不得当作可交易信号**。
> 幂等与资源：重跑 `--write` 后 10742 行与 `max(updated_at)` 逐值不变；峰值 RSS **77.8MB**、耗时 **2.28s**。日频已随 `market_state_daily.run()` 接线（与 L2 同处常驻 scheduler 进程，规模远小于 L1/L2）；若将来要严格满足 `doc/compute_task_resource_standard.md` R4/R5 的「重计算任务独立 unit」，需由运维另建 oneshot + timer。

**扩展性约定（2026-11-06 二次整改，回归 L2 单文件模式）**：`build_business_value.py` **一个文件**容纳全部 L3 指标，按 `biz_code` 用 `if/elif` 分派（与 `build_dimension_value.build_all_dimensions` 同款），不为 4 个固定指标引入注册表 / `TypedDict` 契约 / DAG。只把真通用的部分抽成 5 个公共函数：`load_business_dict` / `active_business_codes` / `seed_business_dict` / `write_business_rows` / `build_all_business`；取数、公式、重算粒度、参数来源由各指标自己的 `build_<name>_business()` 决定，编排层不做假设。`BUSINESS_ORDER` 只是一个显式顺序 tuple（保证 `RISK_BUDGET` 排在 `SYSTEMIC_STRESS` 之后），不是拓扑排序。**命名纪律**：通用公共函数用通用名，指标专属一律 `<name>_` / `<NAME>_` 前缀，禁止 `compute` / `raw_state` / `state_machine` / `PARAMS` 等无归属名称；落库 `rows` 的键必须等于 `business_value` 表列名，是唯一例外。新增指标 = 在本文件加 `build_<name>_business()` + 种子生成器，并在 `BUSINESS_ORDER` / `seed_business_dict` / `build_all_business` 三处各加一个分支。字典 `params` 以库为准、不与代码种子强比对，标定工具可自由冻结切点。

---

## 7 验收

| # | Given | When | Then |
|---|---|---|---|
| 1 | 任一 `indicator_value.percentile` | 用 t 日之后的数据重算 | 历史行分位逐值不变（PIT 不刷写） |
| 2 | 修改 `dimension_dict.params` | 重算 | `params_hash` 变化，`dimension_value.model_version` 同步，历史行全量重算 |
| 3 | 锚（全市场 PE 中位数）`VAL.PE_TTM_MEDIAN.percentile = 63.4` | 计算 `DIM.VALUATION` | `score = 63.40`；改任一校验项分位后 `score` 不变 |
| 4 | 锚分位为 NULL | 同上 | `score IS NULL`、`state IS NULL`、`confidence = '低'`、`flags.no_data = true`，不回落 `VAL.ERP` |
| 5 | `coverage = 0.50` | 同上 | `score` 有值、`state IS NULL`、`flags.low_coverage = true` |
| 6 | `state IS NULL` | 消费方读取 | 不得解释为「中性」；`confidence = 低` 时不得按确定结论使用 |
| 7 | 同一 `(market, trade_date)` 连续跑两次 | 重算 L2 | 除 `updated_at` 外逐字段相等（幂等） |
| 8 | 消费方读 `business_value.level` | 未按 `biz_code` 过滤 | 应被 `output_schema.level_semantics` 校验拦截（多态列禁止跨 biz 比较） |
| 9 | 核心 3 成员任一缺失 / 早于 `member_valid_from` 冻结日 | 计算 `DIM.SENTIMENT` | **整行不产出**（`core_coverage < 1.0`），不得用低覆盖分数凑历史 |
| 10 | 7 个成员中任一 `flags_only` 诊断成员分位变化 | 改该成员后重算 | `score` / `dispersion` / `state` 不变，仅 `contrib` / `coverage` / `flags` 变化（`dispersion_scope = core_only`） |
| 11 | HK：核心集只有换手 1 个成员 | 计算 `DIM.SENTIMENT` | `score` = 换手对齐分位、`dispersion IS NULL`、`state IS NULL`、`confidence = '低'`、`flags.single_block_proxy = true`、`block_coverage = 0.5`、`missing_block = ['一级供给']`；**不得把 2/7 写成 `coverage`** |
| 12 | `state_bands_calibrated = false` | 任一日重算 | `state IS NULL`（0 行出档）；`model_version` 全部等于字典 `params_hash` |
| 13 | 并列值占多数的 `SENT.IPO_COUNT_30D`；同日连续跑两次 | 计算 `DIM.SENTIMENT` | 并列值取中位秩（§2.4 规则 1）；重算除 `updated_at` 外逐字段相等（幂等） |
| 14 | CN / HK 各只有 1 个核心成员、6 个 `flags_only` 诊断成员 | 计算 `DIM.FLOW` | `score` = `proxy_member_by_market[market]` 的对齐分位；`dispersion IS NULL`、`flags.single_block_proxy = true`、`flags.block_coverage = 1.0`（按本市场应有 1 块计）、`dimension_completeness` = CN 0.625 / HK 0.375；`coverage` 按全体 eligible 计且**只作描述量**；诊断成员分位变化不改 `score` / `dispersion` / `state` |
| 15 | 核心成员缺失、早于 `member_valid_from` 冻结日，或单锚配置与 `proxy` 不符 | 同上 | **整行不产出**（`core_coverage < 1.0` / 配置校验失败），不得用低覆盖分数凑历史、不得静默出分；`state_bands_calibrated = false` → `state IS NULL`、`confidence` 最高「中」，CN 与 HK 不得横比 |
| 16 | 4 块核心成员齐全（CN 8 / HK 7） | 计算 `DIM.STRESS` | `score` = 4 个块分的等权均值；`RISK.MAXDD_250` / `RISK.TERM_SPREAD` 取 `100 − percentile`；`FLOW.HIBOR_3M` 改值不改 `score` / `dispersion`，只改 `coverage` / `contrib` |
| 17 | 任一核心成员缺失或早于 `member_valid_from` 冻结日 | 同上 | **整行不产出**（`core_coverage < 1.0`）；高 `dispersion` 仍产行（`dispersion_no_gate = true`），只写 `flags.dispersion_desc_only` |
| 18 | `state_bands_calibrated = false` | 任一日重算 | `state IS NULL`（0 行出档）、`confidence` 最高「中」；`blocks` 落 4 块 `{score, coverage, n}`，`flags.resonance_cnt` = 对齐分位 ≥80 的核心成员数；`model_version` 全部等于字典 `params_hash` |
| 19 | `TREND.STRENGTH_T.percentile = 63.4` | 计算 `DIM.TREND` | `score = 63.40`；改 `TREND.TSMOM_12M` / `TREND.MA200_DEV` 分位后 `score` 不变（单锚、不做加权） |
| 20 | 锚早于 `member_valid_from` 冻结日，或锚分位为 NULL | 同上 | **整行不产出**（按缺行处理），不得填 0 / 50、不得回落其它成员；`state` / `dispersion` 恒 NULL，`confidence` 最高「中」，`flags.short_sample` 在 `member_confident_from` 前为 true |
| 21 | 任一日重算 `DIM.TREND` | 改 `sigma_win` / `index_code` / `tsmom_variant` / `trend_asof_start` / 冻结起点 | `params_hash` 变化且 `model_version` 同步，`state` 全 NULL（0 行出档）、`dispersion` 全 NULL，`blocks` 按 §2.3.3 标准形状；**并须触发 L1 三原子与 `BIZ.MARKET_REGIME` 重算**；其余 123 个 L1 指标 `params_hash` 逐值不变 |
| 22 | 任一交易日的指数收盘序列 | 计算 `TREND.STRENGTH_T` | `raw_value` = `\|TSMOM_12M\| / σ_252`（σ=0 或序列 <252 日 → NULL，不填 0）；早于 `trend_asof_start` 的 `raw_value` 全 NULL，护栏前样本不进 expanding 分位参考分布 |

---

## 附录 A 数据源清单

| 源 | 表 | 用途 |
|---|---|---|
| 宏观序列 | `regime.macro_series` / `macro_series_meta` / `v_macro_series` / `v_macro_latest` | 第①②层宏观、国债、景气 |
| 基准行情 | `public.daily_benchmark` | 美债 10Y、恒指、跨资产；§5.6 趋势维 3 个 `TREND.*` 原子的唯一源（指数收盘价，CN `SH.000001` / HK `HK.800000`） |
| A 股 / 港股行情 | `a_daily_quote` / `hk_daily_quote` | 宽度、估值、波动、动量 |
| 行业层 | `regime.sector_valuation_snapshot` / `sector_mapping` / `sector_daily` / `sector_fund_flow` | 第④层输入，不进六维 |
| 事件类 | `regime.ipo_event` / `fund_issuance_event` / `unlock_schedule` / `insider_trade` / `analyst_forecast_snapshot` | 情绪、资金、基本面信号 |
| 资金流 / 股份供给 | `daily_margin_balance` / `daily_buyback_event` / `daily_ggt_hold` / `daily_northbound_flow` | §5.4 资金维两条核心净额序列与 6 个诊断成员 |
| 市场成交额 | `a_daily_market_turnover` / `daily_market_turnover` | §5.4 净额标准化的同期分母、回购日归并的交易日基准 |
| 跨资产波动 / 中债曲线 / 港币利率 | `public.daily_benchmark`（VIX / 标普）、`regime.macro_series`（`CN.BOND_INDEX` / `CMDTY.*` / `HK.HIBOR_3M`）、`a_daily_quote` / `hk_daily_quote` | §5.5 压力维 9 个成员的 L1 原子源；AA 系信用利差源只保留最近 3 个交易日 |

估值字段起点（含 2018 前 baostock 回填段及其 QFQ 口径限制）、中债 / 美债起点、字段级门禁等数据覆盖事实，见本文档 §5.2「数据覆盖事实」；宽度类逐指标起点、HK 回填后的跨族约束与幸存者偏差，见本文档 §5.1「数据覆盖事实 / 输入侧门禁」。⚠ `a_daily_quote` 的 2018 前 PE 仅供个股级回测使用，**不进 L1 估值锚**（`VALUATION_ASOF_START`），勿据本清单自行拼接。资金维的起点护栏、回购日归并、`rzrq_balance` NULL 修复与制度漂移标注见 §5.4「输入侧门禁」；`a_stock_repurchase_plan` 是方案累计快照（非日频增量），不进 L1，见 §5.4 / D.2。压力维的逐子块起点 / 预热、跨资产对齐、AA 系短样本与源缺失缺口见 §5.5「输入侧数据可用性门禁」，其中 iVIX / VHSI / CDS / 买卖价差属**无源**，Amihud / ES 属**指标未建**，二者不得混作一类。趋势维的指数收盘起点（HK 2013-08-20，**非**研究稿 §5.6 ③ 旧表的 2019-01）、覆盖率实测、252 交易日预热与 L1 护栏 `TREND_ASOF_START` 见 §5.6「数据覆盖事实」；三个 `TREND.*` 原子**已于 v1.19 落地**（`market_state_daily.py::_trend_atoms`）。⚠ `close_20d_ago` 在 HK 2026-10-27~11-06 有 9 行错位 5–13%，**不作数据源**；`RISK.RV20` / `RISK.RV60` 基于同两个指数，只作 σ 校验 / 备用，不作 12 月动量的正式分母。

## 附录 B ADR（决策记录索引）

只列结论与一句理由，完整论证与变更编年见 `market_profile.md` 文首变更说明及附录 F。

| # | 决策 | 理由 |
|---|---|---|
| B-1 | 三层架构：原子 → 维度 → 业务 | 避免「123 个原子直接进 4 个合成」导致权重由成员个数决定 |
| B-2 | 六维不共用聚合算法，各自有独立生产者（按 `dimension_code` 分派） | 分布型 / 水平型 / 流量型 / 共振型 / 方向型的数学性质不同 |
| B-3 | 估值维度用 PE 中位数分位单锚 + 校验 | 盈利端分位不含 rf、区制平稳；股债性价比归第②层 |
| B-4 | 分位窗口：宽度滚动 10 年、其余 expanding | 宽度要同类制度环境；压力与估值要长记忆，滚动会移出历史极值 |
| B-5 | 维度分不可横比；连续型业务指标须先转 as-of 分位再等权 | 分位基数与聚合结构不同，直接平均产生隐式权重 |
| B-6 | 估值约束默认关闭，IC 检验通过前不进 `cap` | **结论不变，理由于 2026-11-05 整体更换**：v1.0 的「短样本分位」已随 baostock 回填作废，现由两条独立理由支撑 —— ① CN 回填段是 QFQ 缩放值且成分池断裂，锚的 expanding 参考分布仍只取 2018 起（§5.2），有效样本短是**门禁的结果而非原因**；② 估值分前瞻 IC 未检验、`state_bands` 未标定 |
| B-7 | 情绪 / 资金的分位窗口**待 ADR 裁决**，裁决前按 §2.4 默认 `expanding` | `market_profile.md` §5.7 的「滚动 10 年」有制度漂移依据（注册制 / 基金发行口径 / 卖空 2026 起点），但窗口是 **L1 按指标族登记**的参数，且 `FLOW.TURNOVER_RATIO` 被资金 / 温度跨维复用 → 不能按维度单设，否则违反 §4「同一原子只有一套分位」。裁决须给窗口实测、按指标族重算范围与 `params_hash` 变更清单；裁决前不得实现 |
| B-8 | 情绪维度**只发布内部连续读数**，不声明块划分 / `sign` 已通过严格构念效度检验 | 离线预检（923 日重叠样本）：一级供给块一阶差分相关中位 ≈0.045、PC1 解释率 0.298，低于 §5.1 的 0.50 / 0.60；年代分层受 3.8 年重叠样本限制不可判定；本项目无情绪外部效标（方向预测 AUC 0.44-0.54 近随机）。故把「发布」限定为「出 `state` 五档 / 进 L3 消费」，本次只产 `score` + 限制标注，由 `state` 恒 NULL、不进 L3、`flags.low_common_factor` 承担风险；`sign` 保留理论推定并在 `remark` 标「弱证据」（§5.3 原理 7 / ADR B-9） |
| B-9 | `DIM.SENTIMENT` 采用**方案 A：固定核心成员集 + 冻结有效起点**；HK 保留单成分代理分 | 成员起点跨 2010-2026，动态可用集会让同一条 `score` 曲线跨期不可比；短样本分位退化（核心成员前 250 日极端占比约 20%）。固定 3 个 2018 起齐全的核心成员 → CN 自 2019-01-10 全区间同一口径；4 个诊断成员不进 score，HK 代理分独立标注且禁与 CN 横比。代价：2019 前不产行、`state` 恒不出档（§5.3 / 附录 C v1.13） |
| B-10 | `DIM.FLOW` 采用**按市场单锚**：CN `FLOW.MARGIN_NET_20D` / HK `FLOW.BUYBACK_NET_20D` 各 1 核心 + 6 `flags_only` 诊断，`state` 恒 NULL | 原 4 成员 / 2 块两级等权模型中，北向 2024-08 断供、南向仅 130 天、产业资本 262 行且分位退化、`MARGIN_RATIO` 分母覆盖塌陷，`block_coverage_min = 1.0` 令两侧永不出档、唯一产物是短样本单块代理。库内只有两条长历史同源有符号净额可用 → 复用 §5.3 HK 代理分纪律并推广到两市场。回购是**公司部门净买入 / 股份供给收缩**，不是外部资金流入，语义边界写进 `remark`；HK 冻结 2010 后的 2016 制度漂移由 `flags.regime_shift_2016` 标注，不改分位窗口（ADR B-7 未裁决）。代价：两侧 `score` 禁横比、禁拼接，`confidence` 最高「中」 |
| B-11 | `DIM.STRESS` 采用**固定核心集 9 成员 / 4 块两级等权 + 冻结有效起点**，`dispersion` 不作门禁、`state` 恒 NULL | 原 21 成员 5 块模型中 AA 系 4 项仅 19~27 行分位、块内相关中位 0.18（相关性块 −0.18）、水平型分位随年代漂移，块均值把危机信号压成 ≈50（危机日 > 全期 P90 仅 36%/39%）；精简后升到 48%/41%。两处 `sign` 修正（`MAXDD_250` 负回撤 / `TERM_SPREAD` high_good）、12 个成员移出 `members[]`（避免 `VAL.ERP_Z` 式 coverage 稀释），冻结第 250 / 750 有效分位日绕过缺失的 L1 `extra.n_sample`。**零新增 L1**；M2 候选（Amihud / 利率 CHG20 / 5Y 信用利差）与已有 L1 的宏观风险量须先有本版基线再做加入实验（§5.5 / 附录 C v1.17） |
| B-12 | `DIM.TREND` 采用**σ 归一化强度单锚 + expanding 分位**，撤销固定 `K=3` 线性刻度与 `NO_PERCENTILE` 破例；`state` 恒 NULL | 真实序列实测：CN 1991/1992/2007/2015 在 `100×clip(t/3,0,1)` 下分别有 100%/75%/100%/41% 交易日顶格，HK 50% 交易日 <20 分，t 的 P95 CN 3.05 / HK 1.92 → 绝对刻度跨期不可比且须按市场标定；起点护栏只把顶格率降到 5.17%，解决不了单边趋势年的饱和。改为 t 作 L1 `raw_value`、其 expanding 分位作 `score` 后零标定参数、`raw_value` 的「几个标准差」语义保留，并让 `members[]` 回到 §2.4 通用管线（撤销 `sign=null` / `state_bands=null` / 自定义 `blocks` 形状 / 生产者读 `raw_value` 四处特殊分支）。CN 护栏 1993-01-01（1991-92 σ 失真）、HK 2013-08-20（恒指实际起点）；删除冗余 `TREND.MOM_60D`。代价：`score` 是分位读数、不再是「几个标准差」（须读 L1 `raw_value`），HK 仅 12 年样本、两侧禁横比（§5.6 / 附录 C v1.19） |

## 附录 C CHANGELOG

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-11-04 | 创建独立数据架构设计文档；建立对象地图、全局索引、统一四块模板；以 `DIM.VALUATION` 为完整样板 |
| v1.1 | 2026-11-05 | **A 股历史估值回填后的口径整改**（不改动单锚 / expanding / `sign` 结构）：① §5.2「数据覆盖事实」按库内实测重写（QFQ 缩放、覆盖率衰减、302→2984 只成分池断裂、PB 未回填）；② 新增输入侧门禁 `VALUATION_ASOF_START = 2018-01-02` 与字段级覆盖门禁，原理 3/5、CN 约束、伪代码、输出与下游接口同步改写——**CN 仍不出档，理由由「物理缺口」换成「口径断裂 + 成分池不可比」**；③ §2.2 / §2.4 规则 4/5 补「参考样本集变更」为第二类全量重算触发；④ ADR B-6 保留「默认关闭」结论、更换理由；⑤ 代码侧落地 `market_state_daily.py` 护栏 + 防回退测试 `tests/test_valuation_guard.py`；⑥ 自审只读跑 `calc_cn` 时发现 2018+ 亦有偶发字段级塌陷（2026-10-26~29 `pe>0` 有效占比 0.37~0.38），属采集侧既有缺口，**门禁刻意只作用于回填段**、避免 `skip_null_updates` 造成新旧混存，另案登记附录 D。两层门禁对 2018+ 逐值零影响，未触发 `params_hash` / `model_version` 变更与重算 |
| v1.2 | 2026-11-05 | **`VAL.ERP_Z` 移出 `DIM.VALUATION.members[]`**（接口错配整改）：它是 z-score、`percentile` 恒 NULL（§2.4 规则 1），而 `members[]` 只读 `percentile` → 留在数组里不改 `score`，却恒定压低 `coverage`、使 `sign=-1` 与校验判据失效。成员数（§5.1 旧标 8、`members[]` 实际 7）→**6（HK 5）**，六维总览表 / 原理 1·5 / 计算步骤 1·2 / 输入表 / 字典种子 / 输出样例（`coverage` 1.0000、`check.n` 4）及 `market_profile.md` §5.2 生效规格同步；§2.4 规则 1 与 §4 补「NO_PERCENTILE 命中项不得进 `members[]`」通用纪律。`raw_value` 保留供 L2 `flags` / 离线分析。纯契约修订，`DIM.VALUATION` 本版时点仍零生产者，不改 L1 数值、不触发 `params_hash` / `model_version` 变更与重算 |
| v1.3 | 2026-11-05 | **L2 落地前的契约收口**：① HK 起点按 `indicator_value` 首次非空日核定（PE/ERP 2001-09-06、PB/破净 2001-08-06、DIV_SPREAD 2000-01-03、ERP_Z 2002-09-16），替换「2002 / 2019 待核定」旧表述；② 修估值伪代码极性翻转 bug（`p * sign[c]` → `sign=-1` 取 `100−p`，对齐 §2.4 规则 2 与宽度实现）；③ `state_bands` 由全局 list 改按市场 dict（生产者要求 `dim["state_bands"][market]`）；④ 低频源 PIT 重复入样按「L2 `flags` 标注 + 字典 `remark` 登记」隔离，L1 分位口径修正另案（破坏性）；⑤ 落地 `build_dimension_value.py` 的 `single_anchor` 分支 + `tools/calibrate_valuation.py`，`DIM.VALUATION` **已完成全历史生产计算**：字典 `params_hash=988fb00aba749028`，`dimension_value` CN 2143 行（2018-01-02 起、恒不出档）/ HK 6189 行（2001-09-06 起、出档 5940 日），五档占比约 19~21%；宽度 `DIM.BREADTH` 数据未改动 |
| v1.4 | 2026-11-05 | **删除旧合成层 `regime.market_regime_daily`**：核查确认全项目 + 三个兄弟项目零生产消费方、无视图/外键依赖、schema 早已标注废弃，遂彻底清理——`market_state_daily.py` 删 `MR_TABLE` / `COMPOSITES` / `build_composites` / 季节状态机 / 顶底信号 / `mr_rows` 写入与统计，`sql/schema.sql`（唯一真相源）与 `regime_schema.py` 删 DDL 与注释自愈项，`monitor_collector.py` 删登记并改兜底名单，2 个测试改写，文档状态看板与编年同步。L1 `indicator_value` 与 L2 `dimension_value` 计算不受影响。删前已导出 15362 行快照 |
| v1.5 | 2026-11-05 | **L2 生产者模块重命名**：`regime_dimensions.py` → **`build_dimension_value.py`**（对齐项目 `build_*`「读库→计算→写派生表」惯例，宾语为目标表 `dimension_value`；`regime_` 前缀只留给 `regime_schema.py` 这类 DDL 管理）。9 个文件 29 处引用同步；**纯重命名**，无口径、参数、数据与调度配置变更（`market_scheduler.py` 只注册 `market_state_daily.py`，L2 由它内嵌 import） |
| v1.6 | 2026-11-05 | **L2 扩展隔离收口**（刻意不做插件发现/自动注册）：① `build_breadth_dimension` 的宽度专属 `flags` 抽成显式命名的 `_breadth_flags()`，避免新维度复用 `block_equal` 时静默产出空数组；② 阈值 `flag_gap` / `flag_extreme` / `min_periods` 改由各自 `dimension_dict.params` 读取（缺省回退模块常量，现有维度逐值不变）；③ `cn_state_disabled` 缺省由 `True` 改 **`False`**，新维度不写该键即正常出档，不再静默继承估值的 CN 口径约束（估值字典已显式写 true）；④ `build_all_dimensions()` 分派处固化「新增维度四步」注释，`reps` 统计不再绑定宽度编码；⑤ 新增 `tests/test_dimension_isolation.py` 9 项钉住「新增/未知维度不影响现有两维度」。已用改动前基线对 BREADTH/VALUATION × CN/HK 四组逐行对比，计算字段全部零变化 |
| v1.7 | 2026-11-05 | **删除 `dimension_dict` 聚合方式列，改按 `dimension_code` 分派**（六维算法无法复用，该字段即冗余中间层；两个生产者从未读取它，DDL 默认值还会让新维度静默走错生产者）。10 个文件 33 处同步：DDL / 标定工具 / `build_dimension_value.py` / 2 个测试 / 两份规格文档；库侧 `DROP COLUMN`。⚠ 该键原在 `params_hash` payload 内 → 字典 hash 变化，按方案 A 重写字典并**全量重算 L2 两个维度**（23715 行）对齐 `model_version`，同时清掉 BREADTH 既存的版本不一致。计算逻辑与参数未变 |
| v1.8 | 2026-11-05 | **L2 生产者按维度命名**：`build_block_equal` → `build_breadth_dimension`、`build_single_anchor` → `build_valuation_dimension`。删掉 `agg_method` 后分派键已是 `dimension_code`，而两个函数实为维度专属实现（含宽度 `flags` / 估值 CN 口径约束），按算法命名会误导复用；改后与 `_breadth_flags` 命名风格统一、分派处自解释。7 个文件 35 处引用一次性同步，无兼容别名；两个生产者补语义化 docstring。纯函数名变更，外部接口 `build_all_dimensions()` 未变，**数据契约 / `params_hash` / `model_version` 均未变，无需重算 L2、无需重启调度器**。顺带修正现行状态表里遗留的旧指纹与 `rep_coverage_min` 表述 |
| v1.2 | 2026-11-05 | **宽度维度数据契约迁入**：按 §3 统一模板把 `market_profile.md` §5.1 迁移为本文档 §5.1「宽度 Breadth · `DIM.BREADTH`」，与 §5.2 估值样板并列（编号与 `market_profile.md` §5 对齐；原 §5.1 六维总览改为章首无编号索引）；四块（定义 / 输入 / 计算模型 / 输出）对齐 `regime.dimension_dict` 真实种子与 `regime.dimension_value` 真实读数，含 11 个成员、5 个有效族代表（HK 4）、按市场冻结切点、输入侧门禁（仙股门槛 / 涨跌停制度起点 / 幸存者偏差）、两项不落表派生量与下游接口；实现状态与变更编年仍分别归附录 D 与 `market_profile.md`。§0 / 附录 A / 附录 C / 附录 D 交叉引用同步 |
| v1.5 | 2026-11-05 | **§5.1 准确性与科学性整改**（按 P0→P1→P2）：P0 ① 标定样本收口——族划分 / 代表 / 内部一致性剔除只读 `[calib_start, calib_end]`，2026 起仅作样本外报告（`tools/calibrate_breadth.py` + 回归测试）；② `LIMIT_NET` 护栏前残留 1,505 行如实披露并登记清理步骤。P1 ③ 代表级覆盖门禁 `rep_coverage_min`（生产者默认 0.0 兼容旧种子、字典登记后启用 + 回归测试）；④ `winsor` 降级为未生效预留键（§2.2 / §2.4 / §4 / §5.1 / §5.2 与 schema 注释同步）；⑤ 术语「构念效度」→「同期内部一致性」。P2 ⑥ 跨年代可比 / 停更占比 / 等频占比 / 代表打分 / `weight_cap` / `is_final` 等措辞与编辑瑕疵。**未执行破坏性数据变更**（清库与全量重算须单独确认）；只读复跑 `--probe` 确认收口后族 / 代表 / 切点 / `flags_only` 与库内一致 |

| v1.9 | 2026-11-05 | **阶段 1 + 阶段 2 合并落地**（一次 L2 全量重算）：① L1 宽度族口径升级 `BREADTH_CALIB_VERSION = v2.1.11` —— 补 `min_periods=250`、`extra.n_sample`、清理 `LIMIT_NET` 1996-12-16 前 1,505 行护栏残留；`_params_hash` 按 rolling / expanding 拆分版本，避免宽度升级波及其余 112 个指标；分位计算收敛到 `_compute_percentiles` 单一真相源，新增 `tools/recalc_breadth_percentile.py`。② L2 参数层 `calib_version = v2.1.11`、`params_hash = 176bef79e3c215bc` —— `flags` 切为 §2.3.3 的 dict（形状写入 `dimension_dict.remark` + 回归测试钉住）、登记 `flag_gap` / `flag_extreme` / `quality_sample` / `min_periods` / `contrib_sign_split`、启用短样本降置信；`tools/calibrate_breadth.py --params-only` 不重标族与切点。③ 全量重算后 CN 8508 行 / HK 6377 行，`model_version` 全部一致；`build_dimension_value.py --audit` = PASS 26 / WARN 7 / FAIL 0。④ **阶段 3 只做离线对照**（`tools/probe_breadth_gate.py`），`dispersion_norm` 口径未改 |

| v1.10 | 2026-11-05 | **删除 `is_final` + 阶段 3 方案 C 落地**：① 三张值表 DROP COLUMN `is_final`（零消费方、T+1 流程从未实现），`sql/schema.sql` / `regime_schema.py` / `market_state_daily.py` 的列定义、自愈 DDL、COMMENT、两个 L2 写入点、测试与本文档同步；口径新旧改由 `model_version` 判定。② `dispersion_scope = non_flags_only`：`dispersion` 排除 `flags_only` 诊断成员，归一化因子改用 `core_valid`；`flags` 增 `dispersion_norm` 供门禁复算；`params_hash` `176bef79e3c215bc → 4852d3274f07f359`，全量重算 L2 后 CN 出档 6709 / HK 5977 日，与对照实验 C 逐值一致。③ `--audit` = PASS 29 / WARN 3 / FAIL 0，全量 pytest 517 passed |
| v1.11 | 2026-11-05 | **情绪维度数据契约迁入**：按 §3 统一模板把 `market_profile.md` §5.3 整改后插入为本文档 §5.3「情绪 Sentiment · `DIM.SENTIMENT`」——四块（定义 / 输入 / 计算模型 / 输出）对齐 §2 全局索引与 §2.3.3 字段字典：7 个成员、3 块两级等权、`sign` 极性对齐、规划字典种子（未落库）、本环节参数六项、输入侧门禁（权益类基金筛选 / 空值按缺失 / 零膨胀中位秩）、数据覆盖事实、落地形态伪代码与示意行。整改口径：① `market_profile.md` 的「五块骨架」改统一四块，决策理由 / 变更编年 / UI 展示 / 排期不进本文件；② 分位窗口按 §2.4 规则 1 与 ADR B-4 统一为 `expanding`，`market_profile.md` 的「滚动 10 年」不进规格；③ `SENT.RATING_BULL_PCT` 更正为真实编码 `FORECAST.RATING_BULL_PCT`，§2.1 登记该前缀；④ 登记 4 项字典 / 计算层现状差异（IPO 族 `market_scope`、换手与卖空的 `domain` 归属、权益类筛选未写 `formula`、评级乐观度判别力弱）；⑤ HK 覆盖 4/7（规格）与 2/7（字典现状）均低于 `coverage_min` → 恒不出档。**纯文档契约迁移**：无生产者、无字典种子、无 `dimension_value` 行，不触发 `params_hash` / `model_version` 变更与重算。§0 / §2.1 / §5 章首注 / 附录 D 交叉引用同步。⚠ **本条「HK 覆盖 4/7 与 2/7 低于 `coverage_min` → 恒不出档」的表述已于 v1.12 更正**：二者是维度完整度，不是 `coverage` |
| v1.12 | 2026-11-05 | **§5.3 评审整改（P0→P1→P2）**。P0：① 修正 `coverage` 分母口径——分母按 `market_scope` 只计本市场成员，HK 齐备时为 1.0；`4/7` / `2/7` 改称**维度完整度**（`flags.dimension_completeness`，描述量、不作门禁），HK 不出档改由**块级门禁** `block_coverage_min = 1.0` + `state_bands_calibrated = false` + 短样本三条独立依据支撑；§2.3.3 字段字典、§2.4 规则 3 的小 n 限制同步；② IPO 两条成员按事实改回 `market_scope = CN`（`get_market_events.py` 只采 A 股 `stock_xgsglb_em`，`calc_hk` 不产出 `SENT.*`），HK 明确「无源」，DDL 两处 COMMENT 统一。P1：③ `FORECAST.RATING_BULL_PCT`（99.3% 常数）降为 `flags_only` 诊断成员，聚合改「2 块等权 + 1 诊断」、`dispersion_scope = non_flags_only`，消除单成员 1/3 权重；④ 分位窗口改登记为**待 ADR 裁决**（ADR B-7），理由改为「L1 按指标族 + 跨维复用不能按维度单设窗口」；⑤ `sign` 标为待构念效度检验（ADR B-8），块划分删「而非统计相关」并补统计判据；⑥ `n_sample` 由 `min` 改**中位数**、`quality_sample` 前置缺口径实登记，示意行改与 `flag_gap = 15` / 块分 / `coverage` 逐值自洽。P2：⑦ 删正文「现状差异」表移入附录 D.1，压缩落地状态与考据，规划种子与示意行统一标为设计契约；⑧ `weight` 明确为记录值、生产者两级平均；⑨ `polarity` 措辞改为「刻度方向标签」；⑩ 起点统一标待核定；⑪ §7 补 3 条情绪验收、头部版本同步、CHANGELOG v1.11 错误结论留痕更正。**纯文档与 COMMENT 整改**：不改表结构 / 生产代码 / 数据，不触发 `params_hash` / `model_version` 变更与重算 |
| v1.13 | 2026-11-06 | **§5.3 方案 A 落地：固定核心集 + 冻结有效起点 + HK 单成分代理分**。① 核查结论：7 个原子指标**全部已采集并产出 L1**（库内实测起点 2010-01-13 / 2018-01-02 / 2000-01-03 / 2023-01-11 / 2026-09-30 / 2026-06-04），无需新增采集；缺的是字典行、生产者与维度读数。② 模型：核心集固定 3 个（换手 + IPO 数 + 破发率）→ 2 块两级等权；基金 2 / 评级 / 港股卖空降为 `flags_only` 诊断；`member_valid_from` / `member_confident_from` 按 (market, code) 冻结第 250 / 750 个有效分位日期，写进 `params` 与 `params_hash`（PIT 安全、不依赖 L1 `extra.n_sample`、不触发 L1 重算）；`core_coverage = 1.0` 才产行，`dispersion` 用核心原始 std（不做 `sqrt(n/10)` 归一化）；`state_bands_calibrated = false` 恒不出档；HK 保留单成分代理分（恒低置信 / 禁横比）。③ 代码：新增 `build_dimension_value.py::build_sentiment_dimension` + 分派分支 + `DIM_SENTIMENT` 常量（不改现有两生产者）；新增 `tools/calibrate_sentiment.py`（`--probe/--write`）；新增 `tests/test_sentiment_dimension.py` 11 项。④ 计算：字典 `params_hash = 71c3e5e253ccec1e`，`--dim DIM.SENTIMENT --write --backfill` 产出 CN 1895 行（2019-01-10 起）/ HK 6350 行（2001-01-18 起），`state` 全 NULL、`model_version` 全一致、现有两维行数不变。⑤ 只读验证：按年 score 均值 58.16→37.96→71.21（逐年漂移，已登记限制）；留一法秩相关 0.592~0.842；`IPO_BREAK_RATE_90D` 符号翻转秩相关 0.647 / 平均绝对差 12.04（`sign` 标弱证据）；冻结起点后极端分位占比换手 19.6%→12.0%、破发率 20.8%→4.4%。⑥ 文档：§0 / §2.3.3 / §5 总览 / §5.3 全章 / §7 验收 / ADR B-8·B-9 / 附录 D 同步。**未改 L1 计算、未改表结构、未改 `market_state_daily.py`**，日频自动接入需重启常驻 scheduler 进程（已在 CLI 完成全历史计算） |
| v1.14 | 2026-11-06 | **资金维度数据契约迁入**：按 §3 统一模板把 `market_profile.md` §5.4 整改后插入为本文档 §5.4「资金 Flow · `DIM.FLOW`」——四块（定义 / 输入 / 计算模型 / 输出）对齐 §2 全局索引与 §2.3.3 字段字典：4 个成员、2 块两级等权、规划字典种子（未落库）、本环节参数 7 项、输入侧门禁（净额标准化 / 20 日累计 / `est_` 估算 / 半日市 / 缺口不补 0）、数据覆盖事实、落地形态伪代码与示意行。整改口径：① 研究稿的「引文 + ②–⑤」分节骨架改统一四块，决策理由 / 变更编年 / UI 展示 / 排期不进本文件（归 `market_profile.md` / 附录 B / D.2）；② **标准化与滚动累计下沉到 L1 `indicator_dict.formula`** —— 本文档 L2 只读 `percentile`，不得在维度层重算原始净额（§4 单一真相源）；③ **研究稿「CN 2/4=0.50、HK 1/4=0.25 → 永不出档」结论保留、口径与理由整体更换**：按 §2.3.3 `coverage` = CN 2/3≈0.6667 / HK 1.0（均 ≥ 0.6，不得据此判门禁），`2/4` / `1/4` 是全体成员口径、`flags.dimension_completeness` = 0.75 / 0.25 只是描述量；恒不出档改由**块级门禁 `block_coverage_min = 1.0` + `state_bands_calibrated = false` + 短样本**三条独立依据支撑，两侧 `score` 均标 `single_block_proxy`、禁横比（v1.12 / v1.13 同款纪律）；④ 分位窗口按 §2.4 默认 `expanding`，研究稿「情绪 / 资金滚动 10 年」仍属 ADR B-7 待裁决；⑤ `sign` 标为待构念效度检验的推定值（`INSIDER_NET_SELL_60D` 取 `−1` 与顶部信号读法有张力）；⑥ 成员编码按字典现状登记（`SENT.INSIDER_NET_SELL_60D` / `SENT.UNLOCK_NEXT_30D_SHARES`），5 项字典 / 计算层接线差异集中到 D.2（南向单日口径、HIBOR 归属、解禁归属、换手 / 卖空 `domain`、回购未接指标）。**纯文档契约迁移**：无生产者、无字典种子、无 `dimension_value` 行，不触发 `params_hash` / `model_version` 变更与重算。头部版本 / §0 / §5 章首注与总览表 / §7 验收（+2 条）/ 附录 D 交叉引用同步 |
| v1.15 | 2026-11-06 | **§5.4 资金维度落地：按市场单锚 + 冻结有效起点**。① 数据核查：4 个原成员中北向 2024-08-16 断供、南向仅 130 天、产业资本 262 行且 expanding 分位退化、`FLOW.MARGIN_RATIO` 分母 `circular_market_val` 2018 前全 NULL / 2026-10 有效率 89.53%（L1 raw 2.58→6.99、分位顶格）；另发现两融表 2017-04~2018-12 缺口已补齐、港股回购 1991 起 104221 条 / 99.4% 金额有效。② 模型：废弃 4 成员 / 2 块两级等权，改按市场单锚（CN `FLOW.MARGIN_NET_20D`、HK `FLOW.BUYBACK_NET_20D`）+ 6 `flags_only` 诊断；`block_coverage` 按本市场应有 1 块计，`dispersion` 恒 NULL、`confidence` 最高「中」、`state` 恒 NULL、两侧标 `single_block_proxy` 且禁横比。③ L1：`market_state_daily.py` 新增 2 原子 + 把南向 / 北向改成「20 交易日累计 ÷ 同期成交额 ×100」；新增 `FLOW_CALIB_VERSION = v2.1.14` 与 `FLOW_CALIB_CODES` 精确枚举（实测其余 121 个指标 `params_hash` 逐值不变）、`FLOW_ASOF_START` 护栏（CN 2019-01-02 / HK 2010-01-04）、回购日归并下一开市日、`rzrq_balance` NULL 的 COALESCE 修复；新增 `tools/recalc_flow_percentile.py` 把破坏性重算范围锁在 4 个编码（删 2392 行旧口径残留，写 7457 个有效观测，峰值内存 68MB）。④ L2：新增 `build_flow_dimension` + 分派分支 + `DIM_FLOW` 常量；`tools/calibrate_flow.py` 冻结第 250 / 750 有效分位日（CN 2020-01-10 / 2022-02-17，HK 2010-12-31 / 2013-01-10）；字典 `params_hash = 67d92d2240e4d593`；`--dim DIM.FLOW --write --backfill` 产出 CN 1637 行 / HK 3901 行，出档 0 日，现有三维行数与版本逐值未变。⑤ 验证：`tests/test_flow_dimension.py` 12 项、全量 pytest **540 passed / 329 subtests**、`--audit` 全 PASS、`tools/scan_big_table_loads.py` 2 项既有疑似与本次改动无关；4 个资金原子与 4 个未改 `FLOW.*` 指标 `model_version` 全部等于字典 `params_hash`。⑥ 文档：§5.4 整章重写为生效规格，§0 / §1.2 / §5 总览 / §7 验收 / 附录 A / ADR B-10 / 附录 D 状态看板与 D.2 清账同步；`sql/schema.sql` 与 `regime_schema.py` 仅同步 `dimension_dict.members` 的 COMMENT，无 DDL / 表结构变更。**按用户约定未重启服务、未做浏览器验证**；日频自动接入需重启常驻 scheduler 进程。 |
| v1.16 | 2026-11-06 | **压力维度数据契约迁入**：按 §3 统一模板把 `market_profile.md` §5.5 整改后插入为本文档 §5.5「压力 Stress · `DIM.STRESS`」——四块（定义 / 输入 / 计算模型 / 输出）对齐 §2 全局索引与 §2.3.3 字段字典：21 个成员 / 5 块、规划字典种子（未落库）、本环节参数 7 项、输入侧门禁（跨资产对齐 / AA 系短样本 / 缺口不补中性 / 方差加权口径）、数据覆盖事实、落地形态伪代码与**示意行**（非库内真实读数）。整改口径：① 研究稿的「引文 + ②–⑤」分节骨架改统一四块，决策理由 / 变更编年 / UI 展示 / 排期不进本文件；② 成员数按章首总览表由研究稿「20 个」更正为 **21 个**（`FLOW.HIBOR_3M` 由 §5.4 移入后 9+2+7+2+1；`RISK.AMIHUD_20D` 待建，建成后 22）；③ **信用块口径以「第二紧（trimmed max）+ 异常保护」为准**，研究稿 §5.10 ② 的「取最紧」为 2026-10-24 修正前旧表述；④ `coverage` 列按 §2.3.3 成员口径（联 `indicator_dict.market_scope`），块级完整度降为 `flags.block_coverage` / `dimension_completeness` **描述量**；业务层「块级 coverage < 3/5 → 不出 level」是 `BIZ.SYSTEMIC_STRESS` 门禁，空块剔除期间折算 3/4，不搬进维度层；⑤ 分位窗口按 §2.4 规则 1 / ADR B-4 为 `expanding`（研究稿 §5.7 旧「滚动 10 年」已废弃），`weight_cap` 对分块结构不生效、`winsor` 未生效；⑥ `direction` / `sign` / `market_scope` / 起点标为**待核定的构念推定**，不复制 123 行字典；`RISK.*` 前缀与 `domain` legacy 投影登记为接线差异（不触发 L1 重算）；⑦ 源缺失（iVIX / VHSI / CDS / 买卖价差）与指标未建（Amihud / 偏度峰度 / ES）分两类，均不进 `members[]`、不按中性填值。**纯文档契约迁移**：压力维零生产者、无字典种子、无 `dimension_value` 行，不触发 `params_hash` / `model_version` 变更与重算。头部版本 / §0 / §2.1 / §5 章首注 / §7 验收（+3 条）/ 附录 A / 附录 D 状态看板交叉引用同步 |
| v1.17 | 2026-11-06 | **压力维度按固定核心集落地（M1）**：① 输入侧实测 21 成员起点 / 覆盖率，确认 AA 系 4 项仅 19~27 行、HK 本地 RV 起点 2013（非研究稿 2019）、水平型分位年代漂移、两处 `sign` 读反、`extra.n_sample` 全缺；② 模型改为 9 成员（8 核心 + 1 诊断）/ 4 块两级等权 / 冻结第 250 / 750 有效分位日 / `dispersion` 只描述 / `state` 恒 NULL / `flags.resonance_cnt`；原 21 成员 5 块与信用块第二紧方案废弃（离线对照：危机日 > 全期 P90 占比 36%/39% → 48%/41%）；③ 代码：新增 `tools/calibrate_stress.py`（字典 `params_hash = 17952671d0fa0438`）、`build_dimension_value.py::build_stress_dimension` + 分派分支 + `DIM_STRESS` 常量、`tests/test_stress_dimension.py` 17 项；④ 计算：`--dim DIM.STRESS --write --backfill` 产出 CN 3577 行（2012-02-14 起）/ HK 2944 行（2014-11-18 起），`state` 全 NULL、`model_version` 全一致、现有四维行数与版本逐值未变，峰值内存约 93 MB；⑤ 文档：§5.5 整章改写为生效规格，§0 / §2.3.3 / §5 章首注与总览表 / §7 验收 / 附录 A / ADR B-11 / 附录 D 同步；`sql/schema.sql` 与 `regime_schema.py` 仅同步 `dimension_dict` 两列 COMMENT，无 DDL / 表结构变更。**零新增 L1、不改 `market_state_daily.py`、未触发 L1 重算**；按用户约定未重启服务、未做浏览器验证，日频自动接入需重启常驻 scheduler 进程 |
| v1.18 | 2026-11-06 | **趋势维度数据契约迁入**：按 §3 统一模板把 `market_profile.md` §5.6 整改后插入为本文档 §5.6「趋势 Trend · `DIM.TREND`」——四块（定义 / 输入 / 计算模型 / 输出）对齐 §2 全局索引与 §2.3.3 字段字典：3 个 `TREND.*` 成员（均待建）、σ 归一化单锚 `score`、规划字典种子（未落库）、本环节参数 6 项、输入侧门禁（252 日预热 / `σ_252 = 0` 不填 0）、数据覆盖与起点、三候选取舍、落地形态伪代码与**示意行**（非库内真实读数）。整改口径：① 研究稿「引文 + ②–⑤」分节骨架改统一四块，变更编年 / UI 展示 / 排期不进本文件；② **本维度是六维里唯一不读 `indicator_value.percentile` 的 L2**，三个成员以 `NO_PERCENTILE` 只写 `raw_value` → 显式裁决 §2.4 规则 1「不得列入 `members[]`」的冲突：生产者须读 `raw_value`、`coverage` 按其有效数计，例外写入 `dimension_dict.remark` 且不得被其它维度复用；③ **`state` 规格上恒 NULL**（方向状态归 `BIZ.MARKET_REGIME`）、`score` **不参与方向判定**（方向读 L1 原子 `raw_value`），澄清研究稿 §5.6 ⑤「决策维度 / 主判据」与 §5.8 ①「`DIM.TREND.score` 只作展示」的张力，并明确 `score` 不被温度 / 预算 / 信号消费；④ `polarity` 按 DDL 既有枚举取 `high_risk` 作刻度方向标签（不新造枚举），`sign` 不适用、`state_bands` / `state_labels` 为 NULL、`dispersion` / `contrib` 恒 NULL；⑤ **HK 起点采用研究稿 §5.8 ② v2.1.7 更正值 2013-08-20 / 实际约 2014-08**，§5.6 ③ 旧表「2019-01 / 约 2020-01」作废并留痕；⑥ 参数分工：方向参数真源仍在 `market_profile.md` §5.8 ③（待迁入，登记附录 D），强度参数 `SIGMA_WIN` / `K` 真源在 §5.6，两组共入 `BIZ.MARKET_REGIME.params_hash`、变更须触发 L2 + L3 重算。**纯文档契约迁移**：趋势维零生产者、无字典种子、无 `dimension_value` 行，不触发 `params_hash` / `model_version` 变更与重算。头部版本 / §0 / §5 章首注 / §7 验收（+3 条）/ 附录 A / 附录 D 状态看板交叉引用同步 |
| v1.19 | 2026-11-06 | **趋势维度按数据实测整改并落地（§5.6 整章改写为生效规格）**。① **输入核查**：`SH.000001` 8757 行（1990-12-19 起，日历覆盖率 99.99%）、`HK.800000` 3253 行（2013-08-20 起，100%），`change_pct` / `prev_close` 与收盘价零偏差；`close_20d_ago` CN 99.77% 且零偏差、**HK 最近 9 行错位 5–13% 不可用**；`RISK.RV20/60`（同指数）、`BREADTH.PCT_ABOVE_MA200`（HK 2000 起）、`SECTOR.UP_RATIO`（HK 2000 起）、31 个申万行业指数等可用数据按唯一归属登记为「校验 / L3 方向确认 / 二期」，不进维度分。② **模型证伪**：真实序列上 `score=100×clip(t/3,0,1)` 的 CN 1991/1992/2007/2015 分别有 100%/75%/100%/41% 交易日顶格，HK 50% 交易日 <20 分，t 的 P95 CN 3.05 / HK 1.92 → 固定 K 跨期不可比且须按市场标定；起点护栏只能把顶格率降到 5.17%，解决不了单边牛市饱和。③ **新模型**：`TREND.STRENGTH_T = \|TSMOM_12M\|/σ_252` 作标准 L1 原子（`raw_value` 可读「几个标准差」），`score` 取其 **expanding 分位**；撤销 v1.18 的 `NO_PERCENTILE` 破例 / `sign=null` / `state_bands=null` / 自定义 `blocks` 形状，`members[]` 回到 §2.4 通用管线；单锚 `STRENGTH_T` + 两个 `weight=0` 方向腿/校验项，`state` 恒 NULL（`state_bands_calibrated=false`）、`dispersion` 恒 NULL、`blocks` 用 §2.3.3 标准形状。删除冗余的 `TREND.MOM_60D`。④ **代码**：`market_state_daily.py` 新增 `TREND_CALIB_VERSION` / `TREND_CALIB_CODES` / `TREND_ASOF_START`（CN 1993-01-01 / HK 2013-08-20）与 `_trend_atoms()`，`_params_hash` 按族隔离（**其余 123 个指标指纹逐值零变化**）；新增 `tools/recalc_trend_percentile.py`（按族回填，避免全量 L1 重算）、`tools/calibrate_trend.py`（冻结第 250/750 有效分位日）、`build_dimension_value.py::build_trend_dimension` + 分派分支 + `DIM_TREND` 常量、`tests/test_trend_dimension.py` 18 项。⑤ **计算**：L1 三原子共 **33770 行**（CN 各 8238 行 1993-01-04 起；HK `MA200_DEV` 3054 行 2014-06-12 起、另两个各 3001 行 2014-08-27 起）；字典 `params_hash = 5038af6aebc31c43`，冻结起点 CN 1993-12-22 / HK 2015-08-31；L2 产出 **CN 7989 行 / HK 2752 行**，`state` 全 NULL、`dispersion` 全 NULL、`model_version` 全一致，`score` 均值 CN 50.93 / HK 50.21（对照 K=3 的 31.41 / 25.10）。`2026-11-05` CN 的 `raw_value = 0.391584` 与独立 SQL 复算逐位一致；现有五维行数与版本逐值未变。⑥ **文档 / DDL**：§5.6 整章改写，§0 / §5 章首注与总览 / §7 验收（+3 条，改写 19–21）/ 附录 A / C / D 同步；`sql/schema.sql` 与 `regime_schema.py` 仅同步 `dimension_dict.members` / `params`（`K=3` 废弃）/ `dimension_value.state` / legacy `dimension` 的 COMMENT，**无 DDL / 表结构变更**。⑦ 落地口径微调：L1 分位沿用 §2.4 默认 expanding + `min_periods=0`（不对新族单设 250），短样本改由 L2 冻结起点承担；锚缺失按**缺行**处理（与估值/资金同款），不写 `score=NULL` 行。**按用户约定未重启服务、未做浏览器验证**；日频自动接入需重启常驻 scheduler 进程 |

| v1.20 | 2026-11-06 | **L3 市场方向按实测整改并落地（§6.1 整章重写为生效规格）**。① 数据核查：CN `SH.000001` 8758 行（1990-12-19 起）/ HK `HK.800000` 3253 行（2013-08-20 起）；L1 三原子 CN 8239 行（1993-01-04 起）、HK 3001 行（2014-08-27 起）；`DIM.TREND` CN 7990 行（1993-12-22 起）/ HK 2752 行（2015-08-31 起）；`DIM.BREADTH.state` 出档 CN 78.8% / HK 93.7%；三张 L3 表原本全空。② **模型证伪**：v1 两腿 corr 0.750/0.773、非零同号 86.6%/90.0%，δ=0.25 死区只挡 17.6%/23.2% 交易日；三态 f120 前瞻收益 CN 下行 +7.56% / 上行 +6.04%（分不开、下行最高）、HK +3.38% / +1.85%，趋势延续假设不成立；v1「HK 1800 日 / 切换 10–30 次 / <20 次沿用 CN」全部作废（实测 HK 3001 日、切换 38 次）。③ **v2**：方向 `sign(TSMOM_12M.raw_value)` × 强度门槛 `DIM.TREND.score>30`（= `STRENGTH_T` expanding 分位，等价自适应 q30 死区，实测与 L1 `percentile` 10742/10742 逐值相等）；起点 inner join `DIM.TREND` 有效行；保留 `N=5/M=20` 与 B2 交互顺序；新增 `detail.trend_state` / `turn_risk`；`RISK_BUDGET.direction` 约束默认关闭。**零待标定参数、零新增数据源**。④ 代码：`build_business_value.py`（含 `business_dict` 种子 / `--seed` / `--probe` / `--write` 全量重算）、`tools/validate_regime.py`（含 120 日 block bootstrap）、`tests/test_market_regime.py` 12 项；`monitor_collector._REGIME_TABLE_CONFIG` 补 `business_dict` / `business_value`（后者 `active=FALSE` 预登记，日频 unit 待接入）。⑤ 计算：`params_hash = 0ccb0176f74177e5`，落库 **CN 7990 / HK 2752 = 10742 行**，`model_version` 全一致、`score` 零空值、`detail` 五键齐全；重跑 `--write` 行数与 `updated_at` 逐值不变（幂等）。验收：切换 111/40、游程中位 38/27 日、增量滞后中位 4 日 / max 18 日（>30 日占比 0）；两态 f120 CN +6.72%/+0.30%（bootstrap 95% CI (0.0031,0.1239) 不含 0）、HK +2.60%/−1.33%（CI (−0.0298,0.1010) 含 0，仅描述性）；§8.1 第 1/4 条未通过已留痕。⑥ 资源：峰值 RSS **77.8MB** / 2.28s，独立 CLI 未挂 scheduler（R3–R6）；全量 pytest **587 passed / 329 subtests**；`scan_big_table_loads.py` 2 项既有疑似与本次无关。⑦ 文档：§0 / §2.3.4 / §5.1 / §5.6 / §6 章首 / §6.1 全章 / 附录 C·D / 研究稿 §5.11·§8.1 勘误同步；**无 DDL / 表结构变更**（`business_dict` / `business_value` 均为既有表，JSONB 列容纳新键）；`sql/schema.sql` 与 `regime_schema.py` 仅同步 `business_value.detail` 列 COMMENT（补 `trend_state` / `turn_risk`），无列 / 表增删。**按用户约定未重启服务、未做浏览器验证**；日频自动接入须按 §4.5 建独立 oneshot+timer 六项资源参数，由运维定时启用 |

| v1.21 | 2026-11-06 | **L3 编排层扩展化整改（不改口径、不改数据）**。① 拆层：`build_business_value.py` 改为纯通用编排层（注册表 `BUSINESS_PRODUCERS` + `TypedDict` 契约 `BusinessProducer` + `active_business_codes` 读 `business_dict.is_active` 分派 + `--biz` 过滤 + 通用 `seed_business_dict` / `load_business_dict_row` / `write_business_rows` / `purge_stale_business_rows` / `build_all_business`），新增 `business_regime.py` 承载 `BIZ.MARKET_REGIME` 全部专属逻辑。② **命名纪律**：指标层所有专属函数 / 常量一律 `regime_` / `REGIME_` 前缀（`regime_compute` / `regime_raw_state` / `regime_state_machine` / `REGIME_PARAMS` / `produce_regime` …），编排层才用通用名；落库 `rows` 的键因 DDL 约束保持表列名，是唯一例外。③ **参数真源**：运行时阈值与输入接线改从 `business_dict.params` / `inputs` 读（按 `role` 取编码），模块级 `REGIME_PARAMS` / `REGIME_INPUTS` 只用于生成种子，消除「代码常量 + 字典」双份维护；新增 `_as_json_value` 正确处理 `inputs` 为 JSON 数组（曾误吞成 `{}`）。④ 新增指标的标准动作 = 写 `business_<name>.py`（三入口）+ 注册一行 + `--seed`。⑤ **兼容性验收**：`params_hash` 仍为 `0ccb0176f74177e5`（曾因 `output_schema.score_semantics` 被无意简化而变，已恢复原文）；`--probe` / `--write` 逐值不变（CN 7990 / HK 2752 = 10742 行、切换 111/40、`turn_risk` 1652/666），重跑 `--write` 后 `max(updated_at)` 逐值不变。⑥ 测试：`tests/test_market_regime.py` 12 → **18 项**（新增命名纪律、参数真源、注册表契约、JSONB 数组回归），全量 pytest **593 passed / 329 subtests**；IDE basedpyright ERROR 归零（基线 `build_dimension_value.py` 有 75 项同类告警，属项目既有）。⑦ 文档：§6.1 生产者 / 落地形态 / 扩展约定、附录 D、本条同步。**无 DDL / 表结构变更、无数据重算、未重启服务** |

| v1.22 | 2026-11-06 | **L3 编排层去过度设计：合并回单文件（参考 L2 `build_dimension_value.py`）**。① 撤销 v1.21 的 `BUSINESS_PRODUCERS` 注册表 / `BusinessProducer(TypedDict)` / `expected_business_dict_row` / `purge_stale_business_rows` / `load_business_dict_row` 强校验，删除 `business_regime.py`，全部逻辑并入 `build_business_value.py`。② 改为 `if/elif` 按 `biz_code` 分派（L2 同款），只保留 5 个真通用公共函数；新增指标 = 加 `build_<name>_business()` + 种子生成器 + 三处一行分支。③ `BUSINESS_ORDER` 显式顺序表替代 `ORDER BY biz_code`，保证 `RISK_BUDGET` 排在 `SYSTEMIC_STRESS` 之后（修复 v1.21 执行顺序缺陷）。④ `load_business_dict` 对齐 L2 `load_dimension_dict`：只做结构校验、**params 以库为准**，不再与代码种子强比对，标定类指标不会被拒绝（修复 v1.21 缺陷 #2）。⑤ 取数 / 重算粒度 / 参数来源 / external 源 / 同层依赖留给各指标生产者自己决定，编排层不做假设。⑥ **命名纪律保留**：通用函数用通用名，指标专属一律 `regime_` / `REGIME_` 前缀（含 DataFrame 列名与业务局部变量）。⑦ 兼容性：`params_hash` 仍为 `0ccb0176f74177e5`；`--seed --write` 后 CN 7990 / HK 2752 = 10742 行、`max(updated_at)` 逐值不变，切换 111/40、`turn_risk` 1652/666 与 v1.20 / v1.21 逐值一致。⑧ 测试 18 → **19 项**（新增执行顺序、生产 hash、标定 params 不被拒绝；并断言已撤销的注册表 / purge 等不存在），全量 pytest **594 passed / 329 subtests**。**无 DDL / 表结构变更、无数据重算、未重启服务** |

| v1.23 | 2026-11-06 | **L3 业务指标日频接线（补上缺失的运行时调用方）**。① 此前 `build_business_value.py` 只有文档用法示例、CLI 手动执行与测试 import，**无任何生产运行时调用**；本次在 `market_state_daily.run()` 中、L2 `build_all_dimensions()` **完成之后** 调用 `build_all_business(conn, markets=..., write=...)`，结果写入 `stats["business"]` 并打印。② 加 `l2_ok` 守卫：L2 失败时跳过 L3，避免用上一轮 `dimension_value` 陈旧读数算业务指标；异常隔离与 L2 同款，业务层失败不拖垮 L1/L2 采集轮次。③ 调度：`market_scheduler.py` 的「市场状态计算」工作日 18:45 已在进程内调 `run()`，无需新增任务；`market_state_daily.py` 顶部流水线图、`market_scheduler.py` 注释、`build_business_value.py` 资源说明同步。④ 监控配套：`monitor_collector._REGIME_TABLE_CONFIG` 的 `business_value` 由 `active=FALSE / updated_at` 改为 **`active=TRUE / time_column=trade_date / day/1`** —— 该表 `only_if_changed=True`，状态不变时 `updated_at` 不刷新，用 `updated_at` 会误报断供；兜底 UPDATE 的 IN 列表加入 `business_value`，已执行幂等迁移使库内生效（`business_dict` 保持 `updated_at/lowfreq`）。⑤ 测试：新增 `tests/test_l3_wiring.py` 4 项（源码顺序断言 + mock 验证 `run()` 在 L2 之后调 L3、`write/markets` 透传、L2 失败跳过、监控时间列），全量 pytest **598 passed / 329 subtests**。⑥ 数据零变更：`params_hash` 仍 `0ccb0176f74177e5`，10742 行与 `max(updated_at)` 逐值不变；本次未运行真实 L1 采集（用 mock 验证接线）。⑦ 资源说明：L3 与 L2 同处常驻 scheduler（10742 行 / 78MB / 2s）；严格按 R4/R5 迁独立 unit 属后续运维决策。**无 DDL / 表结构变更、未重启服务、未做浏览器验证** |

## 附录 D 状态看板

| 项 | 状态 |
|---|---|
| L1 `indicator_value` 生产者（`market_state_daily.py`） | 已落地 |
| L2 `dimension_dict` 种子与 `dimension_value` 生产者 | **六个维度全部已落地**（情绪见 D.1、资金见 D.2、压力见 §5.5、趋势见 §5.6） |
| 其余 L2 维度 | **压力**：✅ v1.17 已落地（§5.5 生效规格）；**趋势**：✅ v1.19 已落地（§5.6 生效规格），三个 `TREND.*` 原子 / 字典 / 生产者 / `dimension_value` 读数齐备；**L3 市场方向**：✅ §6.1 v2 已落地（单文件 `build_business_value.py` + 日频接线 `market_state_daily.run()` 在 L2 之后 + `business_dict` 种子 + 10742 行读数 + 验收脚本 + 19 项测试 + 4 项接线测试）；剩余：另三个 `BIZ.*` 零生产者 |
| 压力 `DIM.STRESS` | ✅ **v1.17 已落地**（§5.5）：9 成员 / 4 块两级等权 + 冻结起点；`params_hash = 17952671d0fa0438`；CN 3577 行（2012-02-14 起）/ HK 2944 行（2014-11-18 起），`state` 全 NULL、`model_version` 全一致。待办：`state_bands` 事件锚定标定、`sign` 构念效度检验、`domain` legacy 投影对齐、M2 加入实验（Amihud / 利率 CHG20 / 5Y 信用利差） |
| 资金 `DIM.FLOW` | ✅ **v1.15 已落地**：`build_flow_dimension` + `tools/calibrate_flow.py` + `tools/recalc_flow_percentile.py` + 12 项测试；`params_hash = 67d92d2240e4d593`；CN 1637 行（2020-01-10 起）/ HK 3901 行（2010-12-31 起），`state` 全 NULL、`model_version` 全一致 |
| 资金维度待办（非字典项） | `state_bands` 等频切点标定、`sign` 的构念效度检验（弱证据）、分位窗口裁决（ADR B-7）、资金族 L1 `extra.n_sample`、CN 流向源补强 / A 股回购日频快照（本期不接）；HK 2016 制度漂移已由 `flags.regime_shift_2016` 标注 |
| 情绪 `DIM.SENTIMENT` | ✅ **v1.13 已落地**：`build_sentiment_dimension` + `tools/calibrate_sentiment.py` + 11 项测试；`params_hash = 71c3e5e253ccec1e`；CN 1895 行（2019-01-10 起）/ HK 6350 行（2001-01-18 起），`state` 全 NULL、`model_version` 全一致 |
| 四个 `BIZ.*` 业务指标、`signal_value`、`v_signal_summary` | `BIZ.MARKET_REGIME` ✅ 已落地（§6.1 v2）；其余三个 `BIZ.*` 与信号仍仅 DDL、零生产者 |
| 按 §3 模板迁移其余对象 | 已完成 `DIM.BREADTH`（§5.1）、`DIM.VALUATION`（§5.2）、`DIM.SENTIMENT`（§5.3）、`DIM.FLOW`（§5.4）、`DIM.STRESS`（§5.5）与 `DIM.TREND`（§5.6，**均为生效规格**）；**六维已全部按模板写出并落地**；L3 `BIZ.MARKET_REGIME` 已按 v2 整改并落地（§6.1） |
| 趋势 `DIM.TREND` | ✅ **v1.19 已落地**：`market_state_daily.py::_trend_atoms` + `tools/recalc_trend_percentile.py` + `tools/calibrate_trend.py` + `build_trend_dimension` + `tests/test_trend_dimension.py`；L1 三原子共 33770 行，字典 `params_hash = 5038af6aebc31c43`；CN 7989 行（1993-12-22 起）/ HK 2752 行（2015-08-31 起），`state` 全 NULL、`dispersion` 全 NULL、`model_version` 全一致；其余 123 个 L1 指标 `params_hash` 逐值零变化 |
| 趋势维度待办（非字典项） | ① ✅ 已收口：§6.1 v2 撤销 `DEADBAND` / `CONF_BASE` 等待标定参数，改用 `score_gate` / `confirm_days` / `min_dwell` 三项先验值并冻结进 `business_dict.params_hash = 0ccb0176f74177e5`；② ✅ L3 方向状态机与 `DIM.TREND.score` 引用**已接线**（`build_business_value.py::build_regime_business`，CN 7990 / HK 2752 行）；③ HK 趋势历史仅 12 年（恒指 2013-08-20 起），若要前推到 2000 需新建「等权市场指数」并重建 PIT 成分池（§5.6「可用但未纳入」）；④ `state_bands` 占位未标定（本维规格上恒不出档）；⑤ 1993 年发布区仅 8 个交易日、分位样本刚满 250，早期读数只作趋势参考；⑥ `close_20d_ago` HK 2026-10-27~11-06 共 9 行错位 5–13%，待采集侧修复，当前不作数据源 |
| `DIM.SENTIMENT` 字典 / 计算层接线差异 | **剩 3 项待办**，明细与判定见 **D.1**（换手与卖空的 `domain` 归属、权益类筛选未写 `formula`、`remark` 判别力弱结论）；第 4 项「评级乐观度降为诊断成员」已落地 |
| 情绪维度待办（非字典项） | `state_bands` 等频切点标定（ADR B-9 决定本期不标定）、分位窗口裁决（ADR B-7）、港股一级供给源判定、构念共同因子 / `sign` 的弱证据限制登记（ADR B-8，已写进 `remark`）；情绪族 L1 `extra.n_sample` 可后续补齐作质量标注，**产分已由冻结起点绕过、不依赖它** |
| HK 情绪维度 | ✅ 已按方案 A 产出**单成分代理分**（换手，6350 行）：恒 `state = NULL` / `confidence = 低`、`flags.single_block_proxy = true`、禁与 CN 横比；港股 IPO / 破发 / 基金仍无源 |
| 生产接入 | 六个维度全历史均已由 CLI 直接计算；**日频自动接入需重启常驻 `market_scheduler` 进程**（`market_state_daily.run()` 内嵌 import 有模块缓存）。⚠ v1.19 新增的 3 个 L1 原子与 `DIM.TREND` 生产者也在该缓存内，重启前不会自动接入日频；重启时机由运维决定 |
| `LIMIT_NET` 1996 前护栏残留 | ✅ 2026-11-05 已 DELETE 1,505 行，并按 v2.1.11 全量重算宽度族分位（附录 C v1.9） |
| 代表级覆盖门禁 `rep_coverage_min` | ✅ 已登记 `1.0` 并生效，L2 已全量重算 |
| `is_final` 列 | ✅ v2.1.12 从三张值表删除（零消费方、T+1 流程从未实现）；口径新旧由 `model_version` 判定 |
| 旧合成层 `regime.market_regime_daily` | ✅ 2026-11-05 已删除（零消费方）；`sql/schema.sql` / `regime_schema.py` / 生产者 / 监控 / 测试全清，删前快照 `/tmp/market_regime_daily_backup_20261105.csv.gz` |
| PIT 成分池（含退市股）、`LIMIT_NET` 的 ST 判定 | 未实现，属数据源缺口 |
| HK 估值起点核定 | 已完成（PE/ERP 2001-09-06 等，见 §5.2 输入表） |
| `DIM.VALUATION` 字典与生产数据 | `params_hash=88391451881125eb`；CN 2143 行 / HK 6189 行（`model_version` 与字典一致） |
| L2 新增维度的扩展方式 | 写独立 `build_xxx()` + `build()` 分派加一分支 + 写字典一行 + 补独立测试；现有生产者不改（详见 `build_dimension_value.py` 模块 docstring 与分派处注释） |
| `DIM.VALUATION` 字典种子与生产者 | 本次落地（`tools/calibrate_valuation.py --write` + `build_dimension_value.py` `single_anchor` 分支） |
| 低频源 PIT 重复入样（DIV_SPREAD HK / BUFFETT） | L2 `flags` 标注 + 字典 `remark` 登记；L1 分位口径修正属破坏性变更，另案 |
| CN 估值锚输入侧护栏（`VALUATION_ASOF_START` + 字段级覆盖门禁） | **已落地**（`market_state_daily.py` + `tests/test_valuation_guard.py`）；2018 前回填段不进 L1 |
| 解除护栏的前置：真实 PE 列 / 独立参考序列、PB 回填、退市股成分池、窗口实测与 `state_bands` 重标 | 待办（§5.2「CN 口径约束」）；解除后须递增 `model_version` 并全量重算 |
| L1 估值序列是否已按回填段重算 | **否**（`indicator_value` 仍 2018-01-02 起，各 2143 行）——刻意保持，勿直接跑 `--backfill` 前先确认护栏 |
| 2018+ 偶发字段级覆盖塌陷（实测 2026-10-26~29 `pe>0` 有效占比 0.37~0.38） | **采集侧既有缺口，本次未处理**；须单独清库 + 口径版本，勿与回填护栏混做（否则 `skip_null_updates` 会留下新旧混存） |
| L1 宽度族 `min_periods=250` / `extra.n_sample` | ✅ 已落地（`market_state_daily.py` + `tools/recalc_breadth_percentile.py`），前 250 有效观测分位置 NULL；非宽度指标仍用旧口径（`EXPANDING_CALIB_VERSION` 保持 v2.1.7） |
| `DIM.BREADTH` 字典与生产数据 | `params_hash=4852d3274f07f359`；CN 8509 行（出档 6709）/ HK 6378 行（出档 5977），`model_version` 全部一致；`--audit` = PASS 29 / WARN 3 / FAIL 0 |
| `flags` / `blocks` JSONB 形状 | ✅ §2.3.3 已固定为 dict，`dimension_dict.remark` + `tests/test_breadth_v2111.py` 钉住；`sql/schema.sql` 列注释待同步 |
| 阶段 3 dispersion 门禁对照实验 | ✅ 只读报告（`tools/probe_breadth_gate.py`）已评审，**按方案 C 落地**：`dispersion_scope = non_flags_only`，生产端出档日与实验预测逐值一致 |
| 非宽度族 554 行 `model_version IS NULL` | 未处理（MACRO.CREDIT_IMPULSE HK 523 / MACRO.LPR 30 / RISK.SYNC_DOWN_20D 1），属既有残留，另案 |

### D.1 `DIM.SENTIMENT` 字典 / 计算层接线差异（v1.13 收口）

规格（§5.3）与当前代码的差异，**属于落地接线待办，不是规格口径选项**；实现状态不写入规格正文，集中于此。4 项中第 1 / 4 项已定案或落地，剩 2 / 3 项待办。

| # | 规格（§5.3） | 代码 / 字典现状 | 判定与对齐动作 |
|---|---|---|---|
| 1 | IPO 数量 / 破发率 `market_scope = CN`；HK 无源 | `market_state_daily.py` 把 `SENT.IPO_*` / `SENT.FUND_*` 全部登记为 `market_scope="CN"` | ✅ 已定案（v1.12）：`get_market_events.py` 只有 A 股源 `stock_xgsglb_em`、`calc_hk` 不产出 `SENT.*`；两处 DDL COMMENT 已同步。**7 个原子指标全部已采集，无采集缺口** |
| 2 | `FLOW.TURNOVER_RATIO` / `FLOW.SHORT_SELLING_RATIO` 归情绪（意愿 / 能力分界） | 二者 legacy `dimension="资金"`，`domain` 由 `dimension` 投影 → 字典仍归资金 | 待办：改 `domain`（不改编码、以保序列连续），确认资金维不再引用。⚠ 已核实 `market_state_daily.py::_params_hash` **不含 `dimension`**，改归属不触发 L1 重算，也不影响 `coverage` 分母 |
| 3 | 基金发行限定权益类、筛选条件写进 `indicator_dict.formula` | 计算层已按 `fund_type` 排除「债 / 固收 / 稳健 / 货币」，`formula` 未写筛选 | 待办：把筛选条件补进 `formula`。⚠ 改 `formula` **会变更 L1 `_params_hash`**，须按 §2.2 递增版本并全量重算 L1 + L2，故本期刻意不动 |
| 4 | `FORECAST.RATING_BULL_PCT` 为 `flags_only` 诊断成员 | 实测 `raw_value` 99.23~99.35（22 行），分位把微小波动放大（std 26.34） | ✅ 已落地（v1.13）：字典写 `flags_only = true`，不进 `score` / `dispersion`；`remark` 保留判别力弱结论 |

> 剩余 2 / 3 项都不改 `DIM.SENTIMENT` 已产出的数值（第 2 项只改归属；第 3 项只改字典文案，但会触发 L1 指纹重算），执行前须单独确认重算范围。

### D.2 `DIM.FLOW` 落地接线差异（v1.15 清账）

规格（§5.4）与代码的差异已在落地时逐项处理；本节保留**被废弃的旧决策**与**剩余待办**，避免后人按 v1.14 的设计契约重造。

| # | v1.14 规格 / 现状 | v1.15 判定与动作 | 状态 |
|---|---|---|---|
| 1 | 南向 / 北向为单日净额，规格要求 20 交易日累计 + ÷ 同期成交额标准化 | 已在 `market_state_daily.py` 改 `formula` 与计算层（`_flow_atoms_cn` / `_flow_atoms_hk`），并按 §2.2 递增 `FLOW_CALIB_VERSION`、`tools/recalc_flow_percentile.py --write` 全量重算 L1（删旧口径 2392 行） | ✅ 已清 |
| 2 | `FLOW.HIBOR_3M` 不进资金维（归压力维流动性块） | 资金维 8 个成员不含它；字典 `domain` 归属仍是 legacy 投影，**待办**（不影响 `coverage` 分母、不触发 L1 重算） | 待办 |
| 3 | `SENT.UNLOCK_NEXT_30D_SHARES` 是前瞻量，由 `SIG.REVERSAL` 直接读 L1 | 资金维不含它；归属对齐到信号层仍待办 | 待办 |
| 4 | `FLOW.TURNOVER_RATIO` / `FLOW.SHORT_SELLING_RATIO` 归情绪 | 换手不进资金维；卖空在资金维是 `flags_only` 诊断、在情绪维同为诊断（与 D.1 第 2 项同源的 `domain` 归属待办） | 待办 |
| 5 | 港股回购「有表未接指标」 | **已接入**：`FLOW.BUYBACK_NET_20D` 成为 HK 唯一核心成员（1991 起 10.4 万条，2010 起进分位） | ✅ 已清 |
| 6 | ETF / 基金申赎无源、A 股回购方案不进 `members[]` | 维持：`a_stock_repurchase_plan` 是方案累计快照，按 `latest_ann_date` 聚合会把不同年代累计值相加，口径错误；`daily_south_flow` 旧表仅 13 行、`regime.sector_fund_flow` 仅 21 天 | 维持 |
| 7 | `block_coverage_min = 1.0` + 两侧各缺一整块 → 恒不出档、单块代理 | **已废弃**：改按市场单锚，`block_coverage` 按本市场应有 1 块计（恒 1.0），`state` 只由 `state_bands_calibrated = false` 挡；原 `2/4` / `1/4` 的 `coverage` 口径一并作废 | ✅ 已清 |
| 8 | `FLOW.MARGIN_RATIO` 是杠杆与供给块核心成员 | 分母 `circular_market_val` 2026-10 覆盖塌陷（有效率 89.53%，L1 raw 2.58→6.99、分位顶格）→ 降为 `flags_only` 诊断，写 `flags.margin_ratio_source_discontinued`；L1 既有值保持不变、不触发重算 | ✅ 已清 |
| 9 | `SENT.INSIDER_NET_SELL_60D` 是核心成员、`sign = −1` 待构念效度检验 | 源仅 262 行且 expanding 分位退化（2025-11 月均 98.10 / 2026-10 月均 5.42）→ 降为 `flags_only` 诊断；`sign` 保留理论推定 | ✅ 已清 |

> 剩余 2 / 3 / 4 项只改 `indicator_dict.domain` 归属，不改 L1 数值、不触发 `model_version`；执行前按 §2.2 单独确认。`est_` 估算、南向起点、半日市与 2016 制度漂移已在 §5.4「输入侧门禁」登记，不在此重复。
