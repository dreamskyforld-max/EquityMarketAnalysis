# 市场状态分析特性 · 数据架构设计文档

| 项 | 内容 |
|---|---|
| 文档性质 | **数据层规格**（当前生效的结构与合约） |
| 版本 | v1.0 |
| 编制日期 | 2026-11-04 |
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

**与 `doc/market_profile.md` 的关系**：该文件是可行性研究报告，保留宏观层（§3）、资产配置层（§4）、方法论依据（§7、§8）与全部变更历史；**数据层规格以本文档为准**。本文档按统一模板完整写出全局骨架与 `DIM.BREADTH`（§5.1）、`DIM.VALUATION`（§5.2）两个完整样板，其余对象在对象地图中列出、按模板逐节迁移（进度见附录 D）；§5.x 编号与该文件 §5 对齐，六维总览为章首无编号索引。

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
| L2 | 维度指标（6 个：宽度 / 估值 / 情绪 / 资金 / 压力 / 趋势） | `DIM.*` | `regime.dimension_dict` | `regime.dimension_value` | `(dimension_code, market, trade_date)` | `build_dimension_value.py`（已落地宽度 `build_breadth_dimension` + 估值 `build_valuation_dimension`，按 `dimension_code` 分派） |
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
| `FLOW.*` | 资金 | L1 原子指标族 |
| `STRESS.*` | 压力 | L1 原子指标族 |
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
| `coverage` | `NUMERIC(5,4)` | 0–1，有效成员 / 应有成员 | 判断是否"只有半个维度"；分母按 `market_scope` 计 |
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

六维不是扁平等权，而是分块（宽度 4 族、情绪 3 块、资金 2 块、压力 5 块）。单个 `score` 看不出是哪一块在动；`SYSTEMIC_STRESS.trigger` 判「信用在扩」还是「波动在冲」时，**只能用 `blocks`**——成员级 `contrib` 拿不到「信用块取最紧」这种块级语义，没有它只能用 top3 反推。

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
| `MARKET_REGIME` | `candidate`、`evidence`、`breadth_confirmation` | 候选态是什么、证据、宽度确认 |
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
| `dispersion_max` | 30 | 规则 3，对 `dispersion_norm = dispersion × sqrt(n/10)` 判定时 | **归一化分歧度 > 30 时不出 `state`**，只保留 `score` + 低置信。**不修改 `score`**；`sqrt(n/10)` 使同一阈值不受成员数影响 | 成员严重背离时平均分不具代表性。变更触发 `dimension_dict.params_hash`，仅重算 L2 |
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

> §5.x 编号只分配给按 §3 统一模板写出的维度对象，并与 `market_profile.md` §5 对齐：§5.1 宽度 / §5.2 估值；本表是横向索引，不占编号。

| 维度名称 | 维度指标编码 | 业务问题 | 极性（高分=） | 成员数 | 聚合结构 | 有效市场 | 给 L3 的角色 |
|---|---|---|---|---|---|---|---|
| 宽度 Breadth | `DIM.BREADTH` | 涨势是否广泛 | 越广（描述型） | 11（HK 10） | 分族取代表 → 族间等权 | CN+HK | 方向置信度确认（不改状态） |
| 估值 Valuation | `DIM.VALUATION` | 贵不贵 | 越贵 | 6（HK 5） | PE 分位单锚 + 校验 | CN+HK | 描述型 + 反转信号；风险预算可选约束（默认关闭） |
| 情绪 Sentiment | `DIM.SENTIMENT` | 人在追还是怕（意愿） | 越亢奋 | 7 | 3 块等权 | CN 为主 | 解释维度 |
| 资金 Flow | `DIM.FLOW` | 真金白银净流（能力） | 越流入 | 4 | 先标准化 → 2 块等权 | CN+HK | 解释维度 |
| 压力 Stress | `DIM.STRESS` | 系统稳不稳 | 越危险 | 21 | 5 块，信用块取第二紧 | CN+HK | 系统性压力主判据、预算约束 |
| 趋势 Trend | `DIM.TREND` | 指数往哪走 | 越强 | 3 | 规则判定 + σ 归一化单锚 | CN+HK | 市场方向主判据 |

六维不共用一套聚合算法，各自有独立生产者，按 `dimension_code` 分派；扁平等权只在成员同质时可用，否则权重会退化成「写了几个指标」。

### 5.1 宽度 Breadth · `DIM.BREADTH`（按统一模板的完整样板）

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

下游接口（只声明数据字段，不规定展示）：`BIZ.MARKET_REGIME` 读 `state` 做 `breadth_confirmation`（**只调置信、不改状态**；其增量价值须经前瞻检验，未通过前不得表述为「互相印证」或「指数说谎」）；`BIZ.MARKET_TEMPERATURE` 读 `score` 的 as-of 分位 `u_i`，且「高 = 广 = 热」只是未经验证的展示标签；`SIG.REVERSAL` 宽度族（A/D 背离、`%>MA200` 高位回落 / 低位回升）**直接读 L1 原子 `percentile`**，不读本维度分；外部消费方读表时用 `model_version` 判口径。

---

### 5.2 估值 Valuation · `DIM.VALUATION`（按统一模板的完整样板）

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

## 6 L3 业务指标

四个对象共用 §3 模板；阈值、状态机与切档参数一律存 `business_dict.params`，标定后冻结并纳入 `business_dict.params_hash`（变更的重算范围见 §2.2）。下表「计算模型」列即各对象的**指标计算方法简述**，原理紧随表后，实现细节见 `business_dict.params` 与各计算模块。

| 业务指标（名称 · 编码） | 定义 | 输入 | 计算模型 | 输出 |
|---|---|---|---|---|
| 市场方向 · `BIZ.MARKET_REGIME` | 指数往哪走；三态状态机 | L1 原子（`MA200` 偏离、`TSMOM_12M`）| 两腿合取 + 死区 + 确认天数 + 最小停留；状态持续期间累计 `dwell` / `streak`，**必须全量重算** | `state`（上行/震荡/下行）、`score`（趋势强度）、`since_date`、`dwell_days`、`status`、`confidence` |
| 市场温度 · `BIZ.MARKET_TEMPERATURE` | 相对冷热位置；`descriptive`，不驱动任何 `cap` | 四个 `DIM.*.score`（估值 / 情绪 / 资金 / 宽度）| 各维先转自身历史 as-of 分位 `u_i` 再等权平均；按冻结的 P20/P40/P60/P80 切五档 | `score`（分位均值，**非分位读数**）、`level`、`state`；`detail` 必须带 `components` 与 `quantiles` |
| 系统性压力 · `BIZ.SYSTEMIC_STRESS` | 系统稳不稳 | `DIM.STRESS.score` + `dimension_value.blocks` | 压力是共振型故障，按块级读数判 `trigger`，不只报最大块 | `level`（1–4）、`state`、`score`、`detail.trigger` / `spike` / `blocks` |
| 风险预算 · `BIZ.RISK_BUDGET` | 最多能承担多少风险；本层唯一动作出口 | `MARKET_REGIME.state`、`vol_state`、`SYSTEMIC_STRESS.level`、`DIM.VALUATION.score`（可选）、§4.2 跨资产确认度 | 多约束取 `min` → 档位映射 → `SWITCH_DAYS` 防抖；输出 `second_binding` 避免方向约束吞掉其余信息 | `score = cap×100`、`level`、`detail.binding_constraint` / `raw_cap` / `missing` |

**计算模型的原理（为什么是这种形态）**

- **市场方向**用**状态机**而非单阈值：趋势方向是带时间语义的状态，日频噪声会让阈值反复翻转。死区过滤边界抖动、确认天数要求证据连续、最小停留防止频繁换态；`dwell` / `streak` 是「进入本状态以来」的累计量，因此必须全量重算（§2.4.5）。
- **市场温度**先把各维 `score` 转成自身 as-of 分位 `u_i` 再等权：六维分位基数、聚合结构、语义都不同，直接平均会因「单锚分位」与「多次平均分位」的方差差异产生隐式权重（§2.4.7）。合成值是**分位的均值**（分布向 50 集中），不是分位读数，不能按「X = 历史 X 分位」解读。
- **系统性压力**是**共振型**而非极值型故障：3 个块同时紧张与 1 个块爆表，风险性质不同，只报最大块会把两者混为一谈。故按 `dimension_value.blocks` 判 `trigger`，并保留 `spike` / `blocks` 明细。
- **风险预算**取 `min` 而非连乘：连乘让约束叠加放大（0.6 × 0.7 × 0.85 = 0.36），档位掉到无法解释的低位；`min` 对应「最紧的约束决定上限」，且可回答「被什么卡住的」。方向=下行时常把其余约束吞掉，故额外输出 `second_binding` 使被掩盖的信息可见。
- 信号按**族**去重计票：顶部清单里同族条目高度相关（「PE 贵 + ERP 低 + 换手高 + 基金爆款」往往只是一轮过热的四种表述），按条数累加会把一轮普通过热冲成「高风险」。`family_cnt` 计的是「几个不同侧面同时报警」。

**横切信号** 反转风险信号 `SIG.REVERSAL`：布尔条件清单，直接读 L1 原子分位，按族去重后计 `family_cnt`；命中记录写 `signal_value`（`days` 必填）。信号不进任何维度分或业务指标。

> ⚠ `level` 与 `score` 都是多态列，四个 `biz_code` 语义互不相容；消费方须按 `biz_code` 过滤并校验 `output_schema`（§2.3）。

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

---

## 附录 A 数据源清单

| 源 | 表 | 用途 |
|---|---|---|
| 宏观序列 | `regime.macro_series` / `macro_series_meta` / `v_macro_series` / `v_macro_latest` | 第①②层宏观、国债、景气 |
| 基准行情 | `public.daily_benchmark` | 美债 10Y、恒指、跨资产 |
| A 股 / 港股行情 | `a_daily_quote` / `hk_daily_quote` | 宽度、估值、波动、动量 |
| 行业层 | `regime.sector_valuation_snapshot` / `sector_mapping` / `sector_daily` / `sector_fund_flow` | 第④层输入，不进六维 |
| 事件类 | `regime.ipo_event` / `fund_issuance_event` / `unlock_schedule` / `insider_trade` / `analyst_forecast_snapshot` | 情绪、资金、基本面信号 |

估值字段起点（含 2018 前 baostock 回填段及其 QFQ 口径限制）、中债 / 美债起点、字段级门禁等数据覆盖事实，见本文档 §5.2「数据覆盖事实」；宽度类逐指标起点、HK 回填后的跨族约束与幸存者偏差，见本文档 §5.1「数据覆盖事实 / 输入侧门禁」。⚠ `a_daily_quote` 的 2018 前 PE 仅供个股级回测使用，**不进 L1 估值锚**（`VALUATION_ASOF_START`），勿据本清单自行拼接。

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

## 附录 D 状态看板

| 项 | 状态 |
|---|---|
| L1 `indicator_value` 生产者（`market_state_daily.py`） | 已落地 |
| L2 `dimension_dict` 种子与 `dimension_value` 生产者 | **宽度 `DIM.BREADTH` + 估值 `DIM.VALUATION` 已落地**（v1.3；估值见下） |
| 其余四个 L2 维度（情绪 / 资金 / 压力 / 趋势） | 仅 DDL 与设计稿，零生产者 |
| 四个 `BIZ.*` 业务指标、`signal_value`、`v_signal_summary` | 仅 DDL，零生产者 |
| 按 §3 模板迁移其余对象 | 已完成 `DIM.BREADTH`（§5.1，由 `market_profile.md` §5.1 迁移）与 `DIM.VALUATION`（§5.2）；其余四个维度待办，推广后 `market_profile.md` §5 的数据层规格由本文档取代 |
| `LIMIT_NET` 1996 前护栏残留 | ✅ 2026-11-05 已 DELETE 1,505 行，并按 v2.1.11 全量重算宽度族分位（附录 C v1.9） |
| 代表级覆盖门禁 `rep_coverage_min` | ✅ 已登记 `1.0` 并生效，L2 已全量重算 |
| `is_final` 列 | ✅ v2.1.12 从三张值表删除（零消费方、T+1 流程从未实现）；口径新旧由 `model_version` 判定 |
| 旧合成层 `regime.market_regime_daily` | ✅ 2026-11-05 已删除（零消费方）；`sql/schema.sql` / `regime_schema.py` / 生产者 / 监控 / 测试全清，删前快照 `/tmp/market_regime_daily_backup_20261105.csv.gz` |
| PIT 成分池（含退市股）、`LIMIT_NET` 的 ST 判定 | 未实现，属数据源缺口 |
| HK 估值起点核定 | 已完成（PE/ERP 2001-09-06 等，见 §5.2 输入表） |
| `DIM.VALUATION` 字典与生产数据 | `params_hash=88391451881125eb`；CN 2143 行 / HK 6189 行（`model_version` 与字典一致） |
| L2 新增维度的扩展方式 | 写独立 `build_xxx()` + `build()` 分派加一分支 + 写字典一行 + 补独立测试；现有两生产者不改（详见 `build_dimension_value.py` 模块 docstring 与分派处注释） |
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
