# 行业热点（第④层）· 数据架构设计文档

| 项 | 内容 |
|---|---|
| 文档性质 | **数据层规格**（当前生效的结构与合约，面向数据使用方与前端设计） |
| 版本 | v1.3（2026-11-16 修订：第④层 7 张专属表迁入独立 `industry` 域并去掉 `sector_` 前缀，另新建 `business_dict`，域内共 8 张；L3 由宽表改为按 `biz_code` 分行的长表；5 张共用输入表仍留 `regime`） |
| 编制日期 | 2026-11-16 |
| 上位文档 | `doc/industry_allocation_feasibility.md`（可行性分析与设计稿；本文只写生效规格，不重述论证过程） |
| 表结构唯一真相源 | `sql/schema.sql`、`regime_schema.py` |
| 口径唯一真相源 | `sector_layer.py` / `sector_layer_hk.py` / `sector_stage2_agg.py` / `hk_sector_index.py` 的实现 |
| 适用范围 | 第④层「行业热点」数据底座与三层指标体系（L1 原子 / L2 维度 / L3 业务） |
| 实现载体 | `sector_layer.py`（CN）、`sector_layer_hk.py`（HK）、`sector_stage2_agg.py`、`hk_sector_index.py`、`get_constituent.py` |

> ⚠ **规格审查声明（v1.2）**：v1.0 曾把上位设计稿的**规划口径**当作生效规格，已逐项对照代码修正：闸门按市场分类、`applicable` 判定式、L1 的 `coverage` / `quality` / `n_sample` / `extra.pit_capable` 实际取值、`rank_n` 口径、L2/L3 `params_hash` 的实际 payload。**凡本文与代码冲突，以代码为准**。
> v1.1 曾把两件事并列登记为「工程缺口」，复核后性质不同，v1.2 分别处置：
> ① **L2/L3 改锚不换 `params_hash` 是真实机制缺陷** → 已修复，指纹改为纳入结构参数 + 上游 L1 定义 + 闸门阈值（§2.2）；
> ② **`SHORT_SAMPLE` 不产出闸门是有意设计**，不是遗漏 → 撤回缺陷定性，改按设计意图说明（§6.3）。短样本信息由 L1 `quality` 与 L2/L3 `confidence` 完整承载，未丢失。
>
> 🔴 **v1.3 结构变更（本次）**：① 新建独立 `industry` 域，第④层 7 张专属表迁入并去前缀，另新建 `business_dict`，域内共 8 张（`indicator_dict` / `indicator_value` / `dimension_value` / `business_dict` / `business_value` / `constituent` / `constituent_change` / `hk_index_daily`）；② **5 张共用输入表仍留 `regime`**（`sector_daily` / `sector_valuation_snapshot` / `sector_fund_flow` / `sector_mapping` / `analyst_forecast_snapshot`），由第③层读写，本层跨 schema 只读；③ L3 旧宽表 `business_value` → 长表 `industry.business_value` + 字典 `industry.business_dict`，`biz_code` 进主键，业务指标成为**开放集合**（增减只改字典行，不加列）。

---

## 0 阅读契约

> ⚠ **领域边界声明**：本层输出是**当前状态的横截面刻画 + 风险闸门**，不是配置建议。不产出「超配 / 标配 / 低配」、不产出买卖建议、不产出单一综合分；`quadrant` 的象限名只描述当前状态。**不产出跨市场统一排序**：CN（31 个申万一级）与 HK（113 个恒生三级）物理分行、各自分位、绝不混排。排序有效性前瞻检验未通过（阶段 4 判定见 §9），排序只能读作「当前状态描述」。

| 你要找什么 | 去哪读 |
|---|---|
| 系统里有哪些数据对象、谁依赖谁、谁在什么时候生产 | §1 对象地图 |
| 编码、版本指纹、字段类型、通用规则 | §2 全局索引（**每条信息只在这里定义一次**）；字典表逐列定义见 §2.3.5 |
| 指标叫什么中文名、解决什么问题 | §2.1（前缀与 L2/L3 名称）、§4（L1 中文名）、§5.0 / §6.0（L2/L3 名称与用户问句） |
| 某个指标怎么定义、吃什么、怎么算、吐什么、什么时候为空 | §3 统一模板 → §4 / §5 / §6 |
| **前端怎么展示：展示名、一句话问句、算法详情、输入输出形式** | **§7 前端展示契约** |
| 我要查某张表 / 某个字段 / 写一条 SQL | §8 数据使用方查询手册 |
| 做成什么样算对 | §9 验收 |
| 数据源与口径决策 | 附录 A / 附录 B |

**规格唯一真相源 = 本文档 + `sql/schema.sql` + 实现代码**；三者冲突时以 DDL 与代码为准。本文不写 UI 视觉样式、不写可行性论证、不写项目排期——分别见 UI 交接文档、上位可行性文档与其实施记录。

**与 `doc/industry_allocation_feasibility.md` 的关系**：后者是设计稿（目标重设、数据盘点、指标设计、分期计划、阶段 0~4 实施记录）；**数据层规格以本文档为准**。凡本文未写明的历史取舍（为何不做中观景气外采、为何删除 §6.10 打分卡、为何 HK 不排名），回查上位文档附录 B 对照表与阶段实施记录。

> ⚠ **中文名称的来源必须分清**：
> ① **L1 中文名称**取自代码字典的 `indicator_name`（已落库，`industry.indicator_dict`），是既有事实，前端可直接取用；
> ② **L2 六维 / L3 字段的中文名称与「一句话问句」是本文为数据契约补写的展示建议**，代码与库中**没有对应字段**，前端采用前需确认措辞（见 §7.1）。本文不把这些建议写成已落库事实。

---

## 1 对象地图

### 1.1 四层依赖

```
regime 域共用输入（sector_daily / sector_valuation_snapshot / sector_fund_flow /
          sector_mapping / analyst_forecast_snapshot）+ 公共表（a_daily_quote /
          financial_indicator / daily_margin_balance / daily_ggt_hold / stock_sector）
        │   ⚠ 这 5 张行业输入表留在 regime，由第③层读写；本层跨 schema 只读
        ▼
industry 域数据底座   constituent (CN PIT 版本链) + constituent_change
                     hk_index_daily (HK 两版自算指数 + 行业日聚合)
        │
        ▼
L1 原子指标  industry.indicator_value   一行业×一日×一指标：raw + 横截面分位 + 时序分位
        │  只交 (market, sector_code, trade_date, indicator_code, ...)
        ▼
L2 维度指标  industry.dimension_value   六维各自 0–100 分 + 结构 / 置信 / 成员说明
        │  只交 (dimension_code, score, coverage, confidence, members_used, extra)
        ▼
L3 业务指标  industry.business_value + industry.business_dict
            一行业×一日×一业务指标一行；biz_code 开放集合，按字典行增减
```

**三条层间铁律**：

1. **方向只定义一次**：原子层 `raw_value` 不翻向；极性在 `indicator_dict.direction` 声明，维度层按 `DIMENSION_DEFS.sign` 唯一翻转（`sign=-1` 取 `100−p`），消费端不得二次翻转。
2. **判定型读原子、连续型读维度**：拥挤度闸门（`SEC.CROWD.GATE`）走原子规则判定，不经维度分平均稀释；`state_side` / `risk_side` 走维度分。
3. **分位是 as-of 水平读数**：横截面分位只在同一 `(market, trade_date)` 的有效行业集内计算；时序分位只用截至当日的历史样本。分位不是「变化量」，RRG 纵轴由两次分位之差单独构造。

### 1.2 对象清单（表级）

| 表 | 层 | 粒度 / 主键 | 生产者与调度 | 使用方常读它做什么 |
|---|---|---|---|---|
| `industry.indicator_dict` | 字典 | `indicator_code` | 代码即字典：`sector_layer.sync_dict` 一次性幂等 upsert（一行一概念，两市场差异落 `params.per_market`） | 查中文名、单位、方向、公式、模型、起点、`params_hash`、`remark` 三段式 |
| `industry.indicator_value` | L1 | `(market, sector_code, trade_date, indicator_code)` | `sector_layer.py` 19:10 / `sector_layer_hk.py` 19:20（独立 systemd unit） | 取原始读数、两种分位、`n_sample` / `coverage` / `quality` |
| `industry.dimension_value` | L2 | `(market, sector_code, trade_date, dimension_code)` | 同上 | 取六维 score 与结构 / 置信 / 成员；`CROWDING` 读 `extra.crowd_gate` |
| `industry.business_dict` | 字典 | `biz_code` | 代码即字典：`sector_layer.sync_dict` 一次性幂等 upsert（一行一概念） | 查业务指标中文名、一句话问句、输入清单、输出契约、`is_active`、独立 `params_hash`；新增/下线指标只改这里 |
| `industry.business_value` | L3 | `(biz_code, market, sector_code, trade_date)` | 同上 | 按 `biz_code` 取状态侧 / 风险侧 / 象限 / 排名 / 闸门读数与共有 `applicable` / `confidence`；每行业日 5 行（CN）/ 4 行（HK） |
| `industry.constituent` | 底座 | `(market, sector_code, stock_code, eff_from)` | `get_constituent.py`，交易日 18:05（调度器进程内） | 按 `[eff_from, eff_to)` 取当时成分；查权重（百分数） |
| `industry.constituent_change` | 底座 | `id` / `UNIQUE(snapshot_date, market, sector_code, stock_code, change_type)` | 同上 | 审计 ADD / REMOVE / WEIGHT 变更，重建任意历史截面 |
| `industry.hk_index_daily` | 底座 | `(trade_date, sector_code)` | `hk_sector_index.py`（由 `sector_layer_hk.py` 调用） | HK 两版指数点位 / 日收益、行业聚合、南向净流入 |
| `regime.sector_daily` | 外部输入 | 交易日 × 申万一级 | `market_state_daily.calc_sector_daily`（18:45） | 价格类与资金类 L1 的直接上游 |
| `regime.sector_valuation_snapshot` / `sector_fund_flow` / `analyst_forecast_snapshot` | 外部输入 | 快照日 × 行业 / 股票 | 采集层（15:35 / 18:15 / 18:40） | 估值、资金、一致预期原始快照 |

> ⚠ `market` 是**市场键**（该行由哪个市场的计算产出），取值 `CN` / `HK`，进入所有输出表主键；字典表不设市场键，**任何查询不得跨 `market` 合并排名或合并分位**。行业集合差异（CN 31 申万一级 / HK 113 恒生三级）由 `market` 隔离，不再单列 `classification`。
> ⚠ 全量历史回填写库尚未执行（各阶段只跑过小窗口真实写库 + dry-run）；timer 默认增量 45 天，不依赖历史回填即可工作。使用方按实际可查日期范围读取，不要假设长历史已铺满。

---

## 2 全局索引（唯一真相源）

> 本章每条信息只有一个家。**参数不单独索引**：每个参数定义在它生效的计算环节（指标字典 `params` / `CALIB_VERSION` / 输入侧门禁）；后续章节只引用名字，不重复抄数值。

### 2.1 指标代码前缀、中文名称与存储形态

| 编码 / 前缀 | 中文名称 | 层级与说明 |
|---|---|---|
| `SEC.TREND.*` | 行业相对强弱与趋势 | L1 原子族；状态侧；A 组 |
| `SEC.VAL.*` | 行业估值 | L1 原子族；风险侧；B 组 |
| `SEC.FLOW.*` | 行业资金 | L1 原子族；状态侧；C 组 |
| `SEC.CROWD.*` | 行业拥挤度 | L1 原子族；风险侧；D 组 |
| `SEC.FUND.*` | 行业基本面与预期 | L1 原子族；状态侧；E 组（HK 不产出） |
| `SEC.BREADTH.*` | 行业内宽度 | L1 原子族；状态侧；F 组 |
| `SEC.DIM.<NAME>` | 见下表六维 | ⚠ **只用于 `params_hash` 构造**；值表 `dimension_code` 存的是**不带前缀**的 `TREND` / `FLOW` / `VALUATION` / `CROWDING` / `FUNDAMENTAL` / `BREADTH` |
| `SEC.BIZ.*` | 业务指标（开放集合） | 现行 5 个：`STATE_SIDE` / `RISK_SIDE` / `QUADRANT` / `RANK` / `GATE`；**是主键列**，字典 `industry.business_dict` 登记中文名、问句、输入、输出契约、独立 `params_hash` 与 `is_active`；详见 §6.0 |
| `SECTOR.*` | 市场状态层行业指标 | 第③层（如 `SECTOR.UP_RATIO`），**不是本文对象**；本层只在其等权合成一致性验收时引用 |
| `MACRO.*` / `DIM.*` / `BIZ.*` | 市场状态第③层 | 不进本层口径；见 `doc/market_state_data_architecture.md` |

**L2 六维的名称对照**（中文名称与「一句话问句」为本文补写的展示建议，见 §7.1；编码列即 `dimension_value.dimension_code` 的实际取值）：

| `dimension_code` | 中文名称（建议） | 面向用户的一句话问句（建议） |
|---|---|---|
| `TREND` | 行业趋势与相对强弱 | 这个行业最近相对其他行业是强还是弱？位置在改善还是转差？ |
| `FLOW` | 行业资金流向 | 钱是在往这个行业流，还是在往外撤？ |
| `VALUATION` | 行业估值水位 | 这个行业现在比别的行业贵还是便宜？ |
| `CROWDING` | 行业拥挤度 | 这个行业是不是已经被资金挤得太拥挤？ |
| `FUNDAMENTAL` | 行业基本面与盈利预期 | 行业内公司盈利在变好还是变差？分析师预期在上调还是下调？ |
| `BREADTH` | 行业内部参与广度 | 上涨是全行业一起涨，还是只靠少数几只股票撑着？ |

**L3 字段的名称对照**（同为本文补写的展示建议）：

| `biz_code` | 中文名称（建议） | 面向用户的一句话问句（建议） | 主输出列 |
|---|---|---|---|
| `SEC.BIZ.STATE_SIDE` | 状态侧热度 | 这个行业当前整体状态在同日所有行业中偏强还是偏弱？ | `score` |
| `SEC.BIZ.RISK_SIDE` | 风险侧水平 | 这个行业当前估值风险高不高？ | `score` |
| `SEC.BIZ.QUADRANT` | 状态象限 | 把状态与风险组合起来，当前属于哪一类局面？ | `state` |
| `SEC.BIZ.RANK` | 当日状态排名 | 今天所有行业按状态排序，它排第几？（共几个可比行业） | `level` |
| `SEC.BIZ.GATE` | 风险与可用性闸门 | 哪些原因让今天的结论需要打折看？ | `detail.gate_flags[]` |

**L3 共有质量元数据列**（每个 `biz_code` 行都带）：`coverage`（维度覆盖度）、`applicable`（可读标记）、`confidence`（置信度）、`params_hash`、`extra`（明细与口径说明）。各指标中文名与问句已落 `industry.business_dict.biz_name` / `.question`；措辞仍属本文建议、前端采用前需确认。

编码表按编码索引（名称紧邻其右）；其余表格一律名称在前、编码在后。新增编码须先登记字典定义（代码中的 `INDICATOR_DEFS` / `HK_INDICATOR_DEFS`），再写计算；逐指标明细以字典表为准，本文 §4 列分组清单、中文名称与关键约束。

### 2.2 版本、口径指纹与重算范围

| 口径指纹 | **实际 payload（代码）** | 变更后的重算范围 |
|---|---|---|
| `indicator_dict.params_hash`（L1） | 统一：`layer / code / DICT_VERSION / concept / per_market.{CN,HK}`（两市场实现差异全纳入） | 该指标全部历史 L1 原子 |
| L2 `params_hash` | `_hash_spec(_dim_spec()[name])`；spec = `layer / code / version / structure / side / anchor / members[{code,sign}] / upstream{原子code: {l1_hash, def}}`。`def` 取「影响数值」的 L1 定义字段（`code/unit/direction/formula/model_key/source/freq/value_col/pct_x_col/pct_t_col/n_col/extra_kind`） | 仅 `dimension_value` 该维度历史；**因 L3 纳入六维指纹，会自动连带 L3** |
| L3 `params_hash` | `_hash_spec(_biz_spec(biz_code, dim_hashes))`；**每个 `biz_code` 独立指纹**。spec = `layer / code / version / kind / side / inputs / output_schema / upstream_dims{六维指纹} / params{闸门阈值}`；阈值含 `enabled_dimensions / coverage_min / valid_days_20_min / valid_days_window / crowd_pct_high / short_sample / min_periods / pit_start`（HK 另含 `cap_start / no_rank`） | 仅该 `biz_code` 的 `business_value` 历史；改单个业务指标不牵连其它 |
| `hk_index_daily.model_version` | 数据层 D4 口径（M8 两版指数、聚合列） | 表 27 全量 + HK L1/L2/L3 |

> ✅ **v1.2 机制修复**：旧实现把 L2/L3 指纹只绑在 `structure` 字面量上，改锚 / 改成员 / 翻 `sign` / 改 L1 定义 / 改闸门阈值都不换指纹，历史行会混存两套口径却显示同一版本号。现按下列规则全量纳入，**传导链自动生效**：
> `改 L1 定义 → L1 指纹变 → L2 spec.upstream.l1_hash 变 → L2 指纹变 → L3 spec.upstream_dims 变 → L3 指纹变`。
> 实现为纯函数（`_dim_spec` / `_biz_spec` / `_hash_spec`，规范化 `json.dumps(sort_keys=True)` 后取 md5 前 16 位），**不读库、不引入上游字典必须先落库的顺序约束**；指纹在 `_score_rows` / `_biz_rows` 的**循环外预计算一次**，热路径无额外开销。回归断言见 `tests/test_sector_params_hash.py` 与 §9 第 12 条。
> ⚠ **明确排除**：`indicator_name` / `remark` / `is_active` 属文案或启用开关，不进指纹（与既有 `remark` 纪律一致）。成员启用状态变化必然伴随 `formula` / `source` / `model_key` 调整，会经 `upstream` 自动传导。
> ⚠ **口径变更仍须人工触发重算**：指纹变化只是让新旧口径**可区分**，不会自动重写历史行。`params_hash` 变动后必须全量重算 L2 + L3 并按版本分段展示。**当前全量回填写库尚未执行，是做这件事成本最低的时点**。

| 市场 | `CALIB_VERSION` | 口径段 | 说明 |
|---|---|---|---|
| CN | `s2.1` | 阶段 1 `s1.1` → 阶段 2 `s2.1` | 六维启用、coverage 分母 3→6、拥挤 k=2→k=3、`PARTIAL_STATE` 消失（六维齐备时）。**两期 L2/L3 读数不可直接比较**，须按 `params_hash` / `extra.calib_version` 分段 |
| HK | `hk1.0` | 独立版本线 | payload 含市场，保证与 CN 同 code 指标的 `params_hash` 不同；HK 不改动 CN 的 `s2.1` |

> ⚠ **读到任何一行先做两件事**：① `model_version`（L1）/ `params_hash`（L2/L3）与对应字典不等 → 旧口径数据；② 值表 `market` 是否属于目标市场。
> ⚠ `remark` **不进** `params_hash`：只改文案不换指纹、不触发历史重算；但「代码即字典」，库内手改会被 `sync_dict` / `sync_dict_hk` 每轮幂等 upsert 覆盖，必须改代码中的定义表。
> ⚠ 除 `params_hash` 变更外，**参考样本集变化也是重算触发**：如快照类数据回补、成分起点护栏调整会改变所有后续日的排位；此类变更须换 `CALIB_VERSION` 并全量重算。

### 2.3 指标存储表与字典表字段说明

字段说明回答的是「读到这一列时，它是什么、拿它可以做什么、什么时候不可用」，不只是类型与值域。

#### 2.3.1 `industry.indicator_value`（L1 原子读数）

一行业一日一指标一行。主键 `(market, sector_code, trade_date, indicator_code)`。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 什么时候为空 |
|---|---|---|---|
| `market` / `sector_code` / `trade_date` / `indicator_code` | `VARCHAR(8)/(20)/DATE/VARCHAR(64)` | 一行的身份 | 点查；中文名称联 `indicator_dict.indicator_name`（§7.1）；`market` 决定分位分母，禁止跨市场拼接 |
| `raw_value` | `NUMERIC(20,6)` | 指标原始值，单位见 `indicator_dict.unit` | 展示绝对值、画原始曲线；**量纲各异，不跨指标比较**；枚举类指标存编码（RRG 1–4、GATE 0–4），中文 label 在 `extra.label` |
| `percentile_xsec` | `NUMERIC(6,2)` | 0–100 的**当日横截面中位秩分位**（M1） | 短历史期主用口径；`NULL` = 当日有效行业 `<10`、无源或该指标不取横截面分位 |
| `percentile_ts` | `NUMERIC(6,2)` | 0–100 的 **as-of 时序分位**（M2） | 跨期比较自身水位；`NULL` = `n_sample < 250` 或指标不取时序分位。**CN 估值 / 资金快照仅 27 日，恒为 NULL** |
| `n_sample` | `INT` | ⚠ **含义随指标而异，不是统一的「历史样本数」**：M1 横截面分位类 = 当日有效行业数 / 有值成分数（`n_valid` / `n_val` / `constituent_n`）；M2 时序分位类 = 截至当日的历史有效观测数（`amt_share_n` / `pe_n_sample`）；M7 = 可比快照数（`revision_n`） | 前端展示须按 `model_key` 注明是「同日样本数」还是「历史观测数」；`quality` 的 `short_sample` 判定与 L2/L3 置信度只取**对应维度**的样本数（§2.3.2） |
| `coverage` | `NUMERIC(5,4)` | **CN 恒 NULL**；HK = 当日有行情成分数 / 有映射成分数 | 只有 HK 可读作成分覆盖度。⚠ `coverage<0.6` 的「组分读数作废」是**上游聚合层门禁**（`sector_stage2_agg` / 表 27），不是这一列在使用时才生效 |
| `quality` | `VARCHAR(16)` | **实际只有 `ok` / `short_sample` 两种取值** | 写库规则：`model_key='M2' 且 n_sample < 750 → short_sample`，其余一律 `ok`。⚠ 不存在 `unavailable` 取值；全空行是**不写行**（`raw/px/pt/ns` 全空时跳过），不是写一行 `unavailable` |
| `model_version` | `VARCHAR(16)` | `= indicator_dict.params_hash` | 判口径新旧 |
| `extra` | `JSONB` | 质量与明细 | 形状见下 |
| `updated_at` | `TIMESTAMPTZ` | 写入时间 | 排查重算 / 补数 |

`extra` 常用键（**CN 与 HK 不同，勿混用**）：

| 键 | CN | HK | 含义 |
|---|---|---|---|
| `rank_n` | ✅ | ✅ | ⚠ **两市场口径不同**：CN = 当日有 `state_side` 的行业数（业务层 `count`），HK = 当日有效行业数（`n_valid`，实测 109~112）。均随分母时变，早期段不与后期直接比 |
| `pit_capable` | ✅ | ✅ | CN：**仅 `source` 含 `constituent` 的指标**按 `d >= 2021-12-13` 判定；其余价格类指标**全期恒 `true`**。HK：全期 `false` |
| `survivorship_bias` | — | ✅ | HK 全期 `true`（成分只有当前快照） |
| `granularity` | — | ✅ | HK 的「113 vs 31 不可混排」声明 |
| `label` | ✅ | ✅ | 枚举指标中文标签（RRG 象限 1–4 / 拥挤档位） |
| `k` / `partial` / `members` | ✅ | ✅ | 拥挤度实际参与因子数、是否部分模式、命中因子名列表。⚠ **没有 `hit` 键**（命中数在 `raw_value`） |

> ⚠ `percentile_xsec` / `percentile_ts` **未做方向调整**：它们是观测值的排位。`DIV_YIELD` 等 `high_good` 指标在风险侧需读反向语义时，以字典 `direction` 与 L2 `sign` 为准，消费端不得自行再翻。
> ⚠ 低频源（财务季频、一致预期快照）被 PIT 填成日频后，同一取值重复入样，**月内 / 快照内分位斜率不含信息**。

#### 2.3.2 `industry.dimension_value`（L2 维度读数）

一行业一日一维度一行。主键 `(market, sector_code, trade_date, dimension_code)`。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 什么时候为空 |
|---|---|---|---|
| `dimension_code` | `VARCHAR(24)` | 六维之一，**不带前缀**（`TREND` / `FLOW` / `VALUATION` / `CROWDING` / `FUNDAMENTAL` / `BREADTH`） | 必须先按 `dimension_code` 过滤，禁止跨维度比较或求均值；中文名见 §2.1 / §5.0 |
| `side` | `VARCHAR(12)` | `state` / `risk` | 状态侧高分 = 更强 / 更健康；风险侧高分 = 更贵 / 更拥挤 |
| `score` | `NUMERIC(6,2)` | 0–100，已按该维度 `sign` 统一方向 | 画曲线、作为 L3 输入。**`CROWDING` 恒 NULL**（规则判定，档位在 `extra.crowd_gate`）；HK `FLOW` / `FUNDAMENTAL` 恒 NULL（`unavailable`）；`equal` 结构下有效成员 `<2` 时也为 NULL |
| `coverage` | `NUMERIC(5,4)` | **0 或 1**：单锚 / 等权维度 `score` 非 NULL 即 1.0；`CROWDING` 特判为 `crowd_gate != 'NA'` 时 1.0 | 只反映「本行分数有没有出来」，**不反映构念完整性**；构念缺口由 `extra.structure` / `extra.reason` 承担 |
| `dispersion` | `NUMERIC(6,2)` | 实现恒 `0.0` | ⚠ 当前**未计算成员分歧度、也未作出档门禁**，不得据此判断成员一致性；前端不应把该列当作「成员一致程度」展示 |
| `confidence` | `VARCHAR(12)` | `normal` / `low` | `confidence_of(n_sample, coverage, pit_capable)`：`coverage<0.6` 或 `pit_capable=false` 或 `n_sample<750` → `low`。HK 全期 `low` |
| `members_used` | `JSONB` | 参与合成的原子 code 列表 | 单锚 = `[锚 code]`；等权 = 4 个成员 code；⚠ **`CROWDING` 与 `unavailable` 维度恒 `[]`**（结构定义里 `anchor=None, members=[]`），不是漏写 |
| `params_hash` | `VARCHAR(16)` | 维度口径指纹 | v1.2 起含 `anchor` / `members`（含 `sign`）/ 上游 L1 定义，见 §2.2；`equal` 结构下 `extra.calib_break` 登记 `s1.1→s2.1` 断点 |
| `extra` | `JSONB` | 结构、锚、成员、门禁说明 | 形状见下 |
| `updated_at` | `TIMESTAMPTZ` | 写入时间 | 排查重算 / 补数 |

`extra` 常用键：`structure`（`single_anchor` / `equal` / `rule` / `unavailable`）、`anchor`、`members`、`weight`（`equal` 时 0.25）、`calib_break`、`crowd_gate`（`NORMAL/WATCH/HIGH/EXTREME/NA`）、`k`、`partial`、`reason`（`unavailable` 的原因）；HK 另有 `pit_capable=false`。

> ⚠ 本表没有 `quality` / `state` 列；可用性一律用 `confidence` + `extra` 承载。
> ⚠ **不产出单一综合分**：状态侧 4 组与风险侧 2 组是异质信息，加权求和会产生「低估值抵消高拥挤」的无意义读数。

#### 2.3.3 `industry.business_value`（L3 业务读数，长表）

**一行业 × 一日 × 一业务指标一行**。主键 `(biz_code, market, sector_code, trade_date)`；每个行业日按 `business_dict.is_active` 产出 5 行（CN）/ 4 行（HK，RANK 不启用）。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 什么时候为空 |
|---|---|---|---|
| `biz_code` | `VARCHAR(32)` | 业务指标编码（主键列） | **读任何字段前必须按此过滤并校验 `business_dict.output_schema`**；5 个值见 §6.0；禁止跨 biz 比较或聚合 |
| `market` / `sector_code` / `trade_date` | 同 L1 | 一行的身份 | 点查；HK/CN 不混排 |
| `score` | `NUMERIC(6,2)` | ⚠ **多态**：`STATE_SIDE`=状态侧热度、`RISK_SIDE`=风险侧水平（高=更贵）；其余 biz_code 恒 NULL | 不是跨 biz 可比的「程度分」 |
| `state` | `VARCHAR(24)` | ⚠ **多态**：`QUADRANT`=强势·未拥挤 / 强势·偏贵拥挤 / 冷门·未拥挤 / 弱势·偏贵拥挤 / NA；其余恒 NULL | 只描述当前状态，不含配置建议 |
| `level` | `SMALLINT` | ⚠ **多态**：`RANK`=当日排名名次（1..`rank_n`）；其余恒 NULL | `applicable=false` 时 NULL；HK 无 RANK 行 |
| `detail` | `JSONB` | 该 biz_code 特有输出 | `STATE_SIDE`: `state_members/state_n/partial`；`QUADRANT`: `state_side/risk_side`；`RANK`: `rank_no/rank_n`；`GATE`: `gate_flags[]/crowd_gate/crowd_k/crowd_partial`（HK 另含 `no_rank/no_rank_reason`）|
| `coverage` | `NUMERIC(5,4)` | 共有元数据：可用维度数 / **当期已启用维度数** | CN 分母 6、HK 分母 4；阶段 1 分母 3 → 阶段 2 分母 6 是口径断点 |
| `applicable` | `BOOLEAN` | 共有元数据：是否可出业务读数 | `false` 时前端只提示数据不足，**不显示排名与象限** |
| `confidence` | `VARCHAR(12)` | 共有元数据：`normal` / `low` | HK 全期 `low`；CN 快照短样本期也基本 `low` |
| `params_hash` | `VARCHAR(16)` | **该 biz_code 独立口径指纹** | 改单个业务指标只换它自己的指纹 |
| `extra` | `JSONB` | 共有口径说明 | `enabled_dimensions` / `calib_version` / `pit_capable` / `rank_n`；HK 另有 `no_rank` / `no_rank_reason` / `survivorship_bias` / `granularity` |
| `updated_at` | `TIMESTAMPTZ` | 写入时间 | 排查重算 / 补数 |

#### 2.3.4 数据底座表字段要点

| 表 | 关键列与使用约束 |
|---|---|
| `industry.constituent` | `eff_from = max(源计入日期, 2021-12-13)`；`eff_to IS NULL` 表示至今；PIT 取用条件 `trade_date >= eff_from AND (eff_to IS NULL OR trade_date < eff_to)`；`weight` 是**百分数**（8.72 = 8.72%，单行业合计≈100，不是 0~1）；权重变化 ≥0.01 个百分点才关旧开新。**无历史剔除记录，2021-12-13 前成分不可还原** |
| `industry.constituent_change` | `change_type ∈ {ADD, REMOVE, WEIGHT}`；按日快照审计，可重建任意历史截面；只在首次采到之后逐步累积，不是回填产物 |
| `industry.hk_index_daily` | `ret_eq` 等权日收益（%）、`ret_cap` 总市值加权日收益（%）、`idx_eq` / `idx_cap` 两版点位（基期 1000）、`pe_agg` / `pb_agg` 市值加权调和值（仅 `pe>0` / `pb>0`，覆盖成分 <60% 置 NULL）、`n_constituent`、`southbound_net`（亿港元，2026-04 起）。**两版指数相关 0.63、约 23% 行业日方向相反**，小成分行业极端值主导等权版；`n_constituent` 随行透出供消费方自行门禁 |

#### 2.3.5 `industry.indicator_dict`（指标字典表字段说明）

**什么时候读它**：想知道某原子指标的**中文名称、单位、方向（极性）、频率、来源、公式、计算模型、市场归属、起点护栏、口径指纹、是否启用**。一行一个领域概念，主键 `indicator_code`；两市场的公式 / 来源 / 模型 / 启用状态差异登记在 `params.per_market.{CN,HK}`，**按 `indicator_code` 单列点查即可**。

| 列 | 类型 | 我读到的是什么 | 用来做什么 / 注意什么 |
|---|---|---|---|
| `market_scope` / `indicator_code` | `VARCHAR(16)/(64)` | 概念身份 + 适用范围声明 | 主键仅 `indicator_code`；`market_scope` 是声明列（`CN` / `HK` / `CN+HK`），`sync_dict` 一次写入 |
| `indicator_name` | `VARCHAR(100)` | **指标中文名称** | 前端展示名的来源（§4 各表已逐条列出）；以代码定义表为准 |
| `domain` | `VARCHAR(24)` | 原子族：`TREND` / `VAL` / `FLOW` / `CROWD` / `FUND` / `BREADTH` | 分组筛选；与 A–F 组一一对应（§4） |
| `side` | `VARCHAR(12)` | `state`（状态侧）/ `risk`（风险侧） | 决定极性解释：状态侧高分 = 更强 / 更健康；风险侧高分 = 更贵 / 更拥挤 |
| `unit` | `VARCHAR(16)` | 单位 / 值域，如 `pct` / `x` / `yi_yuan` / `score_0_100` / `enum` / `rank` | 解释 `raw_value` 的量纲；**不同单位不得比较原始值**；`enum` 编码含义见 §4 |
| `direction` | `VARCHAR(12)` | `high_good` / `high_risk` / `neutral` | **方向唯一真源**：L2 按 `DIMENSION_DEFS.sign` 翻转，消费端禁止二次翻转（§1.1 层间铁律 1） |
| `freq` | `VARCHAR(8)` | 频率，默认 `day` | 财务类虽为季频源，落库仍按日频 PIT 台阶；读斜率时须注意（§2.3.1 注） |
| `source` | `VARCHAR(160)` | 上游表 / 接口 | 追溯数据出处；快照类断档即永久缺失 |
| `formula` | `TEXT` | 核心公式（人读版） | **前端「算法详情」可直接展示此列**（§7.2）；与 `model_key` 联读 |
| `model_key` | `VARCHAR(8)` | 计算模型编号 `M0`–`M8` | 对应 §2.4 的模型与护栏 |
| `params` | `JSONB` | 口径参数物化形态 | `{"version": "v2.0", "per_market": {"CN": {...}, "HK": {...}}}`；**字典种子不是参数定义** |
| `params_hash` | `VARCHAR(16)` | 口径指纹 | payload 见 §2.2；与值表 `model_version` 比对判新旧 |
| `min_periods` | `INT` | 分位最小有效观测数 | 写库规则：**仅 `model_key='M2'` 写 250，其余一律 0**；未达标不出分位 |
| `pit_capable` | `BOOLEAN` | 字典层静态声明 | CN 行恒 `true`、HK 行恒 `false`。⚠ **逐日实际 PIT 状态看值表 `extra.pit_capable`**（§2.3.1） |
| `is_active` | `BOOLEAN` | 是否产出值表行 | `false` = 登记但不写库。实例：HK `SEC.CROWD.TURNOVER_PCT` 因 `turnover_rate` 仅 75 日零星有值而 `is_active=false`，拥挤度降 k=2 |
| `remark` | `TEXT` | 三段式说明：`定义：…　·　判读：…　·　数据：…` | **前端「算法详情」的可读说明来源**（§7.2）；单行、全角空格包围中点分隔，可扩展 `口径：` / `⚠` / `已知限制：`；不进 `params_hash` |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | 建行 / 最近写入时间 | 排查口径变更与幂等 upsert |

> ⚠ **代码即字典**：`sector_layer.INDICATOR_DEFS` / `sector_layer_hk.HK_INDICATOR_DEFS` 是唯一真源，每轮幂等 upsert 会覆盖库内手改。新增 / 修改指标必须先改代码定义表。
> ⚠ 字典只有 L1 原子指标的定义；L2 六维结构定义在 `DIMENSION_DEFS` / `HK_DIMENSION_DEFS`，L3 无独立字典表。**前端展示 L2/L3 的名称与问句见 §2.1 / §5.0 / §6.0，这些不是落库字段**。

**字典种子（一行落库形态，节选）**：

```json
{
  "market_scope": "CN+HK",
  "indicator_code": "SEC.VAL.PE_XSEC_PCT", "indicator_name": "PE横截面分位",
  "domain": "VAL", "side": "risk", "unit": "score_0_100",
  "direction": "high_risk", "freq": "day",
  "source": "regime.sector_valuation_snapshot",
  "formula": "M1(pe_ttm)，越高越贵", "model_key": "M1",
  "params": {"version": "s2.1"}, "params_hash": "…",
  "min_periods": 0, "pit_capable": true, "is_active": true,
  "remark": "定义：…　·　判读：…　·　数据：…"
}
```

```json
{
  "market_scope": "CN+HK",
  "indicator_code": "SEC.CROWD.TURNOVER_PCT", "indicator_name": "换手率分位(本期不可用)",
  "domain": "CROWD", "side": "risk", "unit": "score_0_100",
  "direction": "high_risk", "freq": "day", "formula": "M6+M2", "model_key": "M2",
  "params": {"version": "v2.0", "per_market": {"CN": {...}, "HK": {...}}}, "params_hash": "…",
  "min_periods": 250, "pit_capable": false, "is_active": false,
  "remark": "定义：…　·　判读：…　·　数据：…"
}
```

### 2.4 通用计算规则（M0–M8）

| 模型 | 名称 | 公式 / 步骤 | 边界与护栏 |
|---|---|---|---|
| **M0** | 原值直取 | 直接取上游列（`sector_daily.ret_20d`、`sector_valuation_snapshot.pe_ttm` 等） | 量纲以字典 `unit` 为准；不跨指标比大小 |
| **M1** | 横截面分位 | `p_xsec = 100 × (n_less + 0.5·n_equal) / n_valid`，同一 `(market, trade_date)` 内取中位秩 | `n_valid < 10` 不给分位；并列值取中位秩；分母写入 `extra.rank_n` |
| **M2** | 时序分位 | `p_ts = 100 × (历史样本中 ≤ 当前值的比例)`，as-of；`ROLL_WINDOW=2520`（10 年）滚动 | `MIN_PERIODS = 250` 未达标置 NULL；`SHORT_SAMPLE = 750` 降置信；不得用短样本分位填充 |
| **M3** | 相对强度 RS | `rs_N(t) = ret_N(sector,t) − mean_{s∈S_t} ret_N(s,t)`，`S_t` 为当日有效行业集 | **基准是当日有效行业等权均值，不是大盘指数**；横截面均值恒 0 |
| **M4** | 归一与滚动 | `net_norm = net_1d / 行业成交额`；`rolling_sum(N, min_periods=N)` | 窗口不足不给值（防部分窗口当完整窗口）；资金净额必须先归一才能取时序分位 |
| **M5** | 条件命中 / 闸门 | `hit = Σ 1{p_i > 80}`，`CROWD_PCT_HIGH = 80`；`gate = f(hit, k)` | 拥挤度不做加权平均、不做乘积；**缺失因子既不进分子也不进分母**；`k<2 → NA`；`k=2` 不开 `EXTREME` |
| **M6** | PIT 成分聚合 | `JOIN constituent c ON c.stock_code = q.stock_code AND t ∈ [c.eff_from, c.eff_to)`；财务按 `announce_date ≤ t` 取最新（实现为「月批次 + 次月 1 日生效」） | `coverage < 0.6` 作废并清库；`pit_capable=false` 段标 `survivorship_bias`；按月 × 按股票分批 |
| **M7** | 快照差分（盈利修正） | 同 `stock_code`、同 `fy1_year` 相邻快照 EPS 比较：`Δ>+0.5% → +1`、`Δ<−0.5% → −1`，否则 0；`breadth = (Σ上调−Σ下调)/Σ可比 ×100`，滚动 30 快照 | 财年标签不一致剔除；行业覆盖 <30% 或有效样本 <5 家 → NULL；快照日本身即 PIT |
| **M8** | 自算行业指数（HK） | `ret_t = Σ w_i·(close_i,t/close_i,t−1 − 1)`；等权与总市值加权两版并存 | 加权版用 `total_market_val`（`circular_market_val` 长期缺失），2002 起；等权版 2000-01-03 起；与 CN 官方申万指数不同构 |

**五条数据可用性铁律**（使用方必须遵守）：

1. **PIT**：任何成分聚合按 `[eff_from, eff_to)` 取用；财务按公告可见日；分位 as-of 计算，禁止事后刷写。CN 自下而上指标起点 **2021-12-13**，HK 全期 `pit_capable=false`。
2. **A/H 分离**：`market` 是值表主键列；不得在同一查询里对 CN/HK 混合排名、混合分位、混合校准。
3. **历史深度分级**：长历史（价格类，1999 起 / HK 2000 起）可出时序分位；快照类（估值 / 资金流 / 一致预期，2026-09-30 起仅 27 / 27 / 25 日）首期**只出横截面分位**，时序分位置 NULL 并写 `n_sample`。
4. **分母时变**：CN `rank_n` 16→28→31（早期有效行业数）、HK 109~112；同源占比分母取「当日有效行业合计」，早期段不与后期直接比排名或成交额占比。
5. **短样本不伪装**：短样本以 L1 `quality='short_sample'` + L2/L3 `confidence='low'` 承载，**不产出 `gate_flags`**（§6.3）。这是有意设计：闸门是屏蔽结论的硬开关，短样本是降置信的软标记；若按全局 `n_sample<750` 触发闸门，快照期所有行业会 `applicable=false`、系统不产出任何排名。

---

## 3 统一对象模板

所有 L1 / L2 / L3 对象共用同一套五块，不每节另发明骨架：

| 块 | 写什么 | 不写什么 |
|---|---|---|
| **定义** | 中文名称、编码、层级、**面向用户的一句话问句（这个指标解决什么问题）**、极性、值域、粒度与主键、市场、边界（不回答什么） | 决策理由、历史版本 |
| **输入** | 上游**接口**：表名、字段、指标 code + 中文名、频率、起点、市场 | 上游内部怎么算、数据源考据 |
| **计算模型** | ① 原理与背景（为什么这样算）；② 指标计算方法（核心公式，可直接作为前端「算法说明」）；③ 本环节参数（名称 / 值 / 生效步骤 / 作用 / 依据 / 变更后果）；④ 落地形态（伪代码）；⑤ 门禁与落库规则（引用 §2.4） | 变更编年、评审过程、项目排期 |
| **输出** | 目标表 + 逐字段值域 / 单位 / 可空性 + 一行完整 JSON 样例 + 字段字典引用（§2.3）+ 给下游的一句话接口 | 下游怎么用 |
| **前端展示** | 展示名取哪一列、读数怎么格式化、算法详情展开读哪些字段、空值 / 低置信怎么呈现（汇总规则见 §7） | UI 视觉样式、组件选型 |

「原理与背景」写**方法本身的依据**，不是变更理由（后者归附录 B）。简述抓要点、原理给依据、落地形态给实现细节；三者不可互相替代。

**参数定义写在它生效的计算环节里**，每个参数须交代六件事：**名称、值、生效步骤、作用（改变什么 / 不改变什么）、取值依据、变更后果**（触发哪个 `params_hash`、重算范围见 §2.2）。字典种子只是参数落库的物化形态，不是定义。

---

## 4 L1 原子指标

**定义**：一个可观测量的原始值及其横截面 / 时序分位。CN 41 条 active + HK 24 条 active（HK 另登记 1 条 `is_active=false`）。一行业一日一指标一行，主键见 §2.3.1。
**面向用户的一句话问句**：「这个行业在某个具体侧面上，今天的原始读数是多少？在同类行业里排什么位置、在自身历史里处于什么水位？」
**中文名称来源**：下表「中文名称」列取自代码字典 `indicator_name`（已落库）。同名 code 在 CN / HK 名称不同时以 `CN名 / HK名` 并列；只在一侧存在的指标在「CN / HK」列标注。

**输入**：`regime.sector_daily`、`regime.sector_valuation_snapshot`、`regime.sector_fund_flow`、`regime.analyst_forecast_snapshot`（快照类）；`a_daily_quote`、`financial_indicator`、`daily_margin_balance`、`daily_ggt_hold` × `industry.constituent`（自下而上类）；HK 走 `industry.hk_index_daily`。逐指标接线见字典 `source` 列与 `sector_layer.py` 映射表。

**计算模型**：通用规则见 §2.4 M0–M8；本层不翻转方向，不产出综合分。

**输出**：目标表 `industry.indicator_value`，一行样例：

```json
{
  "market": "CN", "sector_code": "801780",
  "trade_date": "2026-11-11", "indicator_code": "SEC.VAL.PE_XSEC_PCT",
  "raw_value": 31.24, "percentile_xsec": 63.40, "percentile_ts": null,
  "n_sample": 31, "coverage": null, "quality": "ok",
  "model_version": "4f2a…", "extra": {"rank_n": 31, "pit_capable": true}
}
```

下游接口：L2 只读 `(indicator_code, market, sector_code, trade_date, percentile_xsec/percentile_ts)`；`extra` 用于下钻与质量判断。**前端展示**：展示名取字典 `indicator_name`，读数与单位取 `raw_value` + `unit`，算法详情取字典 `formula` / `model_key` / `remark`（§7.2）。

### 4.1 A 组 · 相对强弱与趋势 `SEC.TREND.*`（状态侧）

**极性**：高分 = 相对更强（RET 系列为 `neutral` 原始量）。长历史（CN 1999 起 / HK 2000 起），首期最扎实的一组。

| 中文名称 | 代码 | 定义 | 输入 / 模型 | CN | HK | 关键可用性 |
|---|---|---|---|---|---|---|
| 行业20日收益 / 行业20日收益(等权指数) | `SEC.TREND.RET_20D` | 20 交易日收益（%） | `sector_daily.ret_20d`；HK 用 `idx_eq`；M0 / M8 | ✅ | ✅ | CN 三批起点前为 NULL |
| 行业60日收益 / 行业60日收益(等权指数) | `SEC.TREND.RET_60D` | 60 交易日收益（%） | 同上 | ✅ | ✅ | 中期动能 |
| 20日相对强度 | `SEC.TREND.RS_20D` | 超额相对强度（对当日有效行业等权均值，%） | M3 | ✅ | ✅ | 横截面均值恒 0；**不是行业指数/大盘指数** |
| 60日相对强度 | `SEC.TREND.RS_60D` | 同上，60 日 | M3 | ✅ | ✅ | 计算层新增列，非 `sector_daily` 原生字段 |
| 相对强度横截面分位 | `SEC.TREND.RS_XSEC_PCT` | RS_20D 当日横截面分位 | M1 | ✅ | ✅ | **TREND 维度单锚**；RRG 横轴；`n_valid<10` 不给值 |
| RS分位20日变化 | `SEC.TREND.RS_MOM_20D` | RS 分位的 20 日变化 | M4 | ✅ | ✅ | −100~100；不足 21 日 NULL；RRG 纵轴 |
| RRG四象限 / 四象限状态 | `SEC.TREND.RRG_QUADRANT` | 1领先 / 2走强 / 3走弱 / 4落后 | M5 | ✅ | ✅ | `raw_value` 存编码、中文在 `extra.label`；**非标准 JdK RS-Ratio/Momentum** |
| 动量排名 / 20日动量排名分位 | `SEC.TREND.MOM_RANK` | 当日动量排名（CN）/ 排名反向分位（HK） | M0 / M1 | ✅ | ✅ | 分位已反向翻转，消费端不得二次翻转 |

### 4.2 B 组 · 估值 `SEC.VAL.*`（风险侧）

**极性**：高分 = 更贵。只作风险侧读数，**不用「便宜」抵消拥挤**。

| 中文名称 | 代码 | 定义 | 输入 / 模型 | CN | HK | 关键可用性 |
|---|---|---|---|---|---|---|
| 行业PE-TTM / 行业PE(市值加权调和) | `SEC.VAL.PE_TTM` | 行业 PE 原值（x） | CN `sector_valuation_snapshot` M0；HK `hk_index_daily.pe_agg` M6 | ✅ | ✅ | HK 为市值加权调和值，**不是官方行业指数 PE**；覆盖成分 <60% 置 NULL |
| 行业PB / 行业PB(市值加权调和) | `SEC.VAL.PB` | 行业 PB 原值（x） | 同上 | ✅ | ✅ | 同 PE 约束 |
| 行业股息率 / 行业股息率(市值加权) | `SEC.VAL.DIV_YIELD` | 行业股息率（%） | M0 / M6 | ✅ | ✅ | 高 = 现金回报高、估值相对不贵 |
| PE横截面分位 | `SEC.VAL.PE_XSEC_PCT` | PE 当日横截面分位 | M1 | ✅ | ✅ | **VALUATION 单锚**，也是拥挤估值因子（跨层重复计权已登记）；L3 `risk_side` 的唯一来源 |
| PB横截面分位 | `SEC.VAL.PB_XSEC_PCT` | PB 当日横截面分位 | M1 | ✅ | ✅ | 与 PE_XSEC_PCT 同源，仅校验不进维度分 |
| 行业PE/市场PE中位数 / 相对全行业估值 | `SEC.VAL.REL_TO_MARKET` | 行业 PE / 当日行业中位数 | M0 | ✅ | ✅ | 高度冗余，仅校验 |
| PE时序分位 / PE 自身历史分位 | `SEC.VAL.PE_TS_PCT` | PE 自身历史 as-of 分位 | M2 | ✅ | ✅ | CN 🔴 仅 27 个快照日 « 250，**恒 NULL + `n_sample`**；HK 2002 起可出，2002 前 `BREAK` 闸门 |
| 行业内个股PE离散度 | `SEC.VAL.INTRA_DISPERSION` | 成分股 PE 横截面 std 分位 | M6 + M1 | ✅ | — | 依赖 PIT 成分；个股 `pe_ttm_ratio` 2018 起，早期 NULL；仅校验不进维度分 |

### 4.3 C 组 · 资金 `SEC.FLOW.*`（状态侧）

**极性**：高分 = 资金面更强。

| 中文名称 | 代码 | 定义 | 输入 / 模型 | CN | HK | 关键可用性 |
|---|---|---|---|---|---|---|
| 行业资金净流入 | `SEC.FLOW.NET_1D` | 当日资金净流入（亿元） | `sector_daily.fund_flow_net` M0 | ✅ | — | 同花顺→申万映射，只纳入 `confirmed OR corr≥0.6`；`mapped_ths_count` 随读数透出 |
| 近5日资金净流入 | `SEC.FLOW.NET_5D` | 近 5 日累计（亿元） | M4 | ✅ | — | 窗口不足置 NULL |
| 近20日资金净流入 | `SEC.FLOW.NET_20D` | 近 20 日累计（亿元） | M4 | ✅ | — | 需 ≥20 快照日 |
| 归一资金净额 | `SEC.FLOW.NET_NORM` | `net_1d / 行业成交额` | M4 | ✅ | — | **必须先归一才能取时序分位**；`turnover` 单位亿元（实测覆盖全 A 99.8%+） |
| 资金净额横截面分位 | `SEC.FLOW.XSEC_PCT` | NET_NORM 当日横截面分位 | M1 | ✅ | — | **FLOW 维度单锚** |
| 资金流排名 | `SEC.FLOW.FLOW_RANK` | 当日排名 | M0 | ✅ | — | 仅校验，分位已反向翻转 |
| 量价-资金背离 | `SEC.FLOW.PRICE_FLOW_GAP` | `rs_rank − flow_rank` | M0 | ✅ | — | `direction=neutral`，**不进维度分、不作预测信号** |
| 南向资金按行业净流入 | `SEC.FLOW.SOUTHBOUND_NET` | 南向净流入（亿港元） | M6 | — | ✅ | 源 2026-04-20 起仅 136 日、估算值、当前成分回算 → `survivorship_bias=true`；HK 唯一资金读数 |
| 行业两融净买入 | `SEC.FLOW.MARGIN_NET` | 两融净买入（亿元） | M6 | ✅ | — | 依赖 PIT 成分 |

### 4.4 D 组 · 拥挤度 `SEC.CROWD.*`（风险侧）

**极性**：高分 = 更拥挤。三因子**条件命中计数**，不做乘积、不做加权平均。

| 中文名称 | 代码 | 定义 | 输入 / 模型 | CN | HK | 关键可用性 |
|---|---|---|---|---|---|---|
| 成交额占比时序分位 | `SEC.CROWD.AMT_SHARE_PCT` | 成交额占比的 10 年滚动时序分位 | M4 → M2 | ✅ | ✅ | CN 1999 起 6,498 日；HK 2000 起；**唯一在快照期仍能出时序分位的因子**；早期 `rank_n<31` 段不可与后期比 |
| 行业换手率横截面分位 / 换手率分位(本期不可用) | `SEC.CROWD.TURNOVER_PCT` | 行业换手率分位 | CN M6+M1；HK M6+M2 | ✅ | ⛔ | CN 阶段 2 起可用（首期用横截面分位）；HK `turnover_rate` 仅 75 日零星有值 → `is_active=false`，**不写库**，拥挤降 k=2 |
| 拥挤·估值因子 / 估值因子分位(复用) | `SEC.CROWD.VAL_PCT` | 直取 `PE_XSEC_PCT` | M1 | ✅ | ✅ | 复用不重复计权；与 `VALUATION` 维度语义不同，**不得当两条独立证据** |
| 拥挤因子命中数 | `SEC.CROWD.HIT_CNT` | `Σ1{p_i>80}`，k=当日可用因子数 | M5 | ✅ | ✅ | CN k=3、HK k=2；缺失因子不进分子分母；`k<2 → NULL` |
| 拥挤档位 | `SEC.CROWD.GATE` | `NORMAL/WATCH/HIGH/EXTREME/NA` | M5 | ✅ | ✅ | CN k=3 可判 `EXTREME`；HK k=2 **最高只到 HIGH**；CN `EXTREME` 仅 45 行且全在最近 30 日，前瞻验证 `NOT_EVALUABLE`，**验证前只作纯展示标记** |

> ⚠ CN 阶段 1（k=2、部分模式）与阶段 2（k=3）分母不同，两期闸门不可跨期比较；HK 与 CN 的 k 也不同，不可横比。
> ⚠ `GATE` 的 `raw_value` 是编码（`NORMAL=0 / WATCH=1 / HIGH=2 / EXTREME=3 / NA=4`），中文在 `extra.label`；前端**不得把编码直接上屏**。

### 4.5 E 组 · 基本面与预期 `SEC.FUND.*`（状态侧，仅 CN）

**极性**：高分 = 基本面 / 预期更改善。以可计算的财务 PIT 聚合 + 一致预期快照替代不可得的中观景气。

| 中文名称 | 代码 | 定义 | 输入 / 模型 | 关键可用性 |
|---|---|---|---|---|
| 行业ROE中位数 | `SEC.FUND.ROE_MEDIAN` | 成分股 ROE PIT 中位数（%） | M6 | 季频台阶，「月批次 + 次月 1 日生效」，读数最多滞后一个月 |
| 行业净利同比中位数 | `SEC.FUND.NP_YOY_MEDIAN` | 净利同比 PIT 中位数（%） | M6 | `winsorize [2,98]` 只用于稳健估计参考分布，**不对最终读数截尾** |
| 行业营收同比中位数 | `SEC.FUND.REV_YOY_MEDIAN` | 营收同比 PIT 中位数（%） | M6 | 与净利同比联读区分「增收不增利」 |
| 行业盈利改善宽度 | `SEC.FUND.IMPROVEMENT_BREADTH` | 净利同比为正的成分占比（%） | M6 | 归一比率，天然平稳 |
| 财务覆盖度 | `SEC.FUND.COVERAGE` | 有 ROE 成分数 / 成分数（0–1） | M6 | **门禁**：`<0.6` 本组全部读数作废并清库；0.6 为实测标定，不得随手调 |
| 行业盈利修正宽度 | `SEC.FUND.REVISION_BREADTH` | 30 快照滚动（−100~100%） | M7 | 实测仅 25 个快照日，窗口未满 → 按实际 `n` 出值 + `confidence=low`；覆盖 <30% 或有效样本 <5 家 → NULL；阶段 4 载荷 0.156 <0.3，登记为待移出成员 |
| 行业评级乐观度 | `SEC.FUND.RATING_BULL_PCT` | (买入+增持)/评级五档合计（%） | M0 | 实测 99.3% 结构性集中，**仅极端背离提示，不进维度分** |
| 预期覆盖度 | `SEC.FUND.FORECAST_COVERAGE` | 有预期成分数 / 成分数（0–1） | M6 | **门禁**：`<0.3` 时 `REVISION_BREADTH` 不出值 |

> 🔴 HK 不做 E 组：`financial_indicator` 港股 `announce_date` 全 NULL（M6 财务 PIT 不可用），一致预期仅 A 股。`HK_DIMENSION_DEFS` 将 `FUNDAMENTAL` 标为 `unavailable`。

### 4.6 F 组 · 行业内宽度 `SEC.BREADTH.*`（状态侧）

**极性**：高分 = 广度更健康（`RET_DISPERSION` 为 `high_risk`，维度内 `sign=-1` 翻转）。

| 中文名称 | 代码 | 定义 | 输入 / 模型 | CN | HK | 关键可用性 |
|---|---|---|---|---|---|---|
| 行业内上涨占比 / 成分股收涨占比 | `SEC.BREADTH.UP_RATIO` | 成分股当日收涨占比（%） | M6 `count-if/n` | ✅ | ✅ | **与市场级 `SECTOR.UP_RATIO` 不同**：后者是「多少行业在涨」，本项是「行业内多少股票在涨」；HK 等权合成 52 日逐日完全一致 |
| 行业内站上MA60占比 / 成分股站上MA60占比 | `SEC.BREADTH.ABOVE_MA60` | `close > MA60` 成分占比（%） | M6 | ✅ | ✅ | 需 60 日暖机，首 60 日 NULL |
| 行业内新高−新低归一净占比 / 60日新高−新低归一净占比 | `SEC.BREADTH.NEW_HIGH_LOW` | 60 日新高−新低归一净占比（−100~100%） | M6 | ✅ | ✅ | 计数类净额必须归一，不得用家数差 |
| 行业内收益离散度 / 成分20日收益离散度 | `SEC.BREADTH.RET_DISPERSION` | 成分股 20 日收益横截面 std | M6 | ✅ | ✅ | 阶段 4 载荷 0.160 <0.3，登记为待移出成员 |

---

## 5 L2 维度指标

### 5.0 六维总览（章首索引）

| 中文名称（建议） | `dimension_code` | 侧 | 面向用户的一句话问句（建议） | 极性（高分=） | 聚合结构 | CN 锚 / 成员 | HK 锚 / 成员 | 可用性 |
|---|---|---|---|---|---|---|---|---|
| 行业趋势与相对强弱 | `TREND` | 状态 | 这个行业最近相对其他行业是强还是弱？位置在改善还是转差？ | 相对更强 | 单锚 | `SEC.TREND.RS_XSEC_PCT` | 同 | CN/HK 均可用 |
| 行业资金流向 | `FLOW` | 状态 | 钱是在往这个行业流，还是在往外撤？ | 资金更强 | 单锚 | `SEC.FLOW.XSEC_PCT` | ⛔ `unavailable` | HK 仅有 136 日南向，单锚不成立 |
| 行业估值水位 | `VALUATION` | 风险 | 这个行业现在比别的行业贵还是便宜？ | 更贵 | 单锚 | `SEC.VAL.PE_XSEC_PCT` | 同（自算调和 PE） | HK 2002 起；CN 快照仅 27 日 |
| 行业拥挤度 | `CROWDING` | 风险 | 这个行业是不是已经被资金挤得太拥挤？ | 更拥挤 | **规则判定** | `HIT_CNT` + `GATE` | 同（k=2） | `score` 恒 NULL，档位在 `extra.crowd_gate` |
| 行业基本面与盈利预期 | `FUNDAMENTAL` | 状态 | 行业内公司盈利在变好还是变差？分析师预期在上调还是下调？ | 更改善 | 4 成员等权 | ROE / 净利同比 / 改善宽度 / 修正宽度 | ⛔ `unavailable` | CN 起点 2021-12-13；HK 财务无公告日期 |
| 行业内部参与广度 | `BREADTH` | 状态 | 上涨是全行业一起涨，还是只靠少数几只股票撑着？ | 更健康 | 4 成员等权 | 4 个 F 组原子 | 同 | 需 60 日暖机 |

**四种聚合结构**（`extra.structure`）：

| 结构 | 含义 | `score` | `members_used` |
|---|---|---|---|
| `single_anchor` | 单锚：只取一个原子的对齐分位，其余原子仍落 L1 但只作校验 | = 锚的分位 | `[锚 code]` |
| `equal` | 4 成员等权，每成员 25%（恰好满足单成员 ≤25% 上限）；`sign=-1` 取 `100−p`；**有效成员 <2 时不出分** | 成员对齐分位的均值 | 4 个成员 code |
| `rule` | 规则判定：不产出连续分，只产出分类档位 | 恒 NULL | `[]` |
| `unavailable` | 本期无可靠数据源，写一行说明原因 | 恒 NULL | `[]` |

> ⚠ 单锚结构的原因：可用成员 <4 时等权会让单成员权重 33%，突破 25% 上限；故阶段 1 一律走单锚，阶段 2 只有 `FUNDAMENTAL` / `BREADTH` 成员齐 4 个才改等权（口径断点 `s1.1→s2.1`）。

### 5.1 行业趋势与相对强弱 · `TREND`

| 块 | 内容 |
|---|---|
| **定义** | 中文名称：行业趋势与相对强弱；编码 `TREND`；状态侧；**问句：这个行业最近相对其他行业是强还是弱？位置在改善还是转差？**；`score` 0–100，高分 = 相对更强；粒度 `(market, sector_code, trade_date, TREND)`；CN / HK 均产出。**不回答**：未来涨跌、绝对涨跌幅度（那是 `RET_20D` 原子）、大盘相对强弱 |
| **输入** | `industry.indicator_value` 的 `SEC.TREND.RS_XSEC_PCT`（相对强度横截面分位，`percentile_xsec`）；校验项 `RS_MOM_20D` / `RRG_QUADRANT` / `MOM_RANK` 仍落 L1，**不进 score** |
| **计算模型** | **原理**：相对强弱是横截面概念，用「行业 20 日收益 − 当日有效行业等权均值」（M3）作原始量，再取当日横截面中位秩分位（M1）。选单锚而非多成员平均，是因为 RS 的水平 / 变化 / 排名是同一现象的不同读法，平均会把权重退化成「写了几个指标」。<br>**公式**：`score = p_xsec(RS_20D)`（M1：`100×(n_less + 0.5·n_equal)/n_valid`）；`sign=+1` 不翻向。<br>**参数**：`XSEC_MIN_N=10`（有效行业不足不给分位）；`ROLL_WINDOW=2520` / `MIN_PERIODS=250`（仅对 M2 类原子生效）；锚 code 登记在 `DIMENSION_DEFS["TREND"].anchor`。<br>**落地形态**：`df["dim_TREND"] = _anchor_pct("SEC.TREND.RS_XSEC_PCT")`。<br>**门禁**：锚为 NULL → `score=NULL`、`coverage=0`、`confidence=low` |
| **输出** | `industry.dimension_value`：`score`（0–100，可空）、`side='state'`、`coverage`（0 或 1）、`dispersion=0.0`、`confidence`、`members_used=["SEC.TREND.RS_XSEC_PCT"]`、`params_hash`、`extra={"structure":"single_anchor","anchor":"SEC.TREND.RS_XSEC_PCT","members":[...] }`。下游接口：参与 L3 `state_side` 均值 |
| **前端展示** | 展示名「行业趋势与相对强弱」；读数 `score` 格式化为 0–100 分并显示「相对同日其他行业的位置」；算法详情展开显示输入原子中文名「相对强度横截面分位」+ 公式 + `n_valid`；`score=NULL` 时显示「数据不足」而非 50 分 |

### 5.2 行业资金流向 · `FLOW`

| 块 | 内容 |
|---|---|
| **定义** | 中文名称：行业资金流向；编码 `FLOW`；状态侧；**问句：钱是在往这个行业流，还是在往外撤？**；`score` 0–100，高分 = 资金面相对更强；CN 产出、**HK 本期 `unavailable`**。**不回答**：资金流入能否预测未来收益（纯描述性） |
| **输入** | CN：`SEC.FLOW.XSEC_PCT`（资金净额横截面分位，`percentile_xsec`）；其上游 `sector_daily.fund_flow_net` × `sector_mapping` → 归一 `NET_NORM` → M1。HK：无同花顺行业资金流序列，只有 `SOUTHBOUND_NET`（2026-04 起 136 日） |
| **计算模型** | **原理**：资金净额随市场规模漂移，必须先除以行业成交额归一（M4）才能横截面比较；再取当日 M1 分位。HK 因缺少行业级资金流源，单锚不成立，按「宁可不产分，也不用短样本代理冒充」处理为 `unavailable`。<br>**公式（CN）**：`score = p_xsec(net_1d / 行业成交额)`；`sign=+1`。<br>**参数**：`XSEC_MIN_N=10`；`rolling_sum(N, min_periods=N)` 用于 `NET_5D/20D`（不进 score）。<br>**落地形态**：CN `_anchor_pct("SEC.FLOW.XSEC_PCT")`；HK `df["dim_FLOW"] = NaN`，`extra.reason` 写入不可用原因。<br>**门禁**：HK 恒 `score=NULL`、`coverage=0`、`confidence=low` |
| **输出** | CN 同 §5.1 结构，`members_used=["SEC.FLOW.XSEC_PCT"]`；HK 一行 `score=NULL` + `extra.structure='unavailable'` + `extra.reason`（**不是缺行**）。下游：CN 参与 `state_side`；HK 不参与 |
| **前端展示** | CN 显示 0–100 分 + 算法详情（归一公式、映射准入 `confirmed OR corr≥0.6`、`mapped_ths_count`）；HK **必须显示「本期不可用」并给出 reason**，不得显示 0 分或空图表了事 |

### 5.3 行业估值水位 · `VALUATION`

| 块 | 内容 |
|---|---|
| **定义** | 中文名称：行业估值水位；编码 `VALUATION`；**风险侧**；**问句：这个行业现在比别的行业贵还是便宜？**；`score` 0–100，**高分 = 更贵（风险更高）**；CN / HK 均产出。**不回答**：便宜是否值得买、能否用低估值抵消拥挤 |
| **输入** | CN：`SEC.VAL.PE_XSEC_PCT`（PE横截面分位），上游 `regime.sector_valuation_snapshot.pe_ttm`（2026-09-30 起 27 个快照日）；HK：`SEC.VAL.PE_XSEC_PCT`，上游 `hk_index_daily.pe_agg`（市值加权调和 PE，2002 起）。校验项 `PB_XSEC_PCT` / `REL_TO_MARKET` / `PE_TS_PCT` / `INTRA_DISPERSION` 不进 score |
| **计算模型** | **原理**：官方快照只有当前横截面、无历史序列，故首期只做「同日同市场内比贵贱」的横截面分位（M1），不做「比自己历史」的时序分位（M2 需 250 观测）。用 PE 单锚避免与 PB / 相对市场比值重复计权。<br>**公式**：`score = p_xsec(pe)`，`direction='high_risk'`，`sign=+1`（PE 越高分位越高，天然对齐「高分=更贵」）。<br>**参数**：`XSEC_MIN_N=10`；`MIN_PERIODS=250`（`PE_TS_PCT` 未达标恒 NULL）；HK `CAP_START=2002-01-01`（之前覆盖率 <6% → `BREAK` 闸门）。<br>**落地形态**：`_anchor_pct("SEC.VAL.PE_XSEC_PCT")`。<br>**门禁**：锚 NULL → `score=NULL`；CN 快照短样本 → `confidence=low` |
| **输出** | `side='risk'`、`score`、`coverage`、`confidence`、`members_used=["SEC.VAL.PE_XSEC_PCT"]`、`extra.structure='single_anchor'`。**这是 L3 `risk_side` 的唯一来源** |
| **前端展示** | 高风险读数**必须标注极性**：「分数越高 = 越贵」，避免用户按状态侧习惯误读为「越好」；算法详情注明 CN 是官方快照、HK 是个股调和聚合，**两者不同构、不可拼接**；CN 显示「仅 27 个快照日，暂无历史分位」 |

### 5.4 行业拥挤度 · `CROWDING`

| 块 | 内容 |
|---|---|
| **定义** | 中文名称：行业拥挤度；编码 `CROWDING`；**风险侧**；**问句：这个行业是不是已经被资金挤得太拥挤？**；**输出是分类档位而非分数**：`NORMAL/WATCH/HIGH/EXTREME/NA`；CN / HK 均产出。**不回答**：拥挤后一定下跌（阶段 4 验证 `NOT_EVALUABLE`，验证前只作纯展示标记） |
| **输入** | L1 原子分位：`SEC.CROWD.AMT_SHARE_PCT`（成交额占比时序分位）、`SEC.CROWD.TURNOVER_PCT`（行业换手率分位，CN 有 / HK 未启用）、`SEC.CROWD.VAL_PCT`（估值因子分位）；派生 `SEC.CROWD.HIT_CNT` / `SEC.CROWD.GATE` |
| **计算模型** | **原理**：拥挤是「多个侧面同时处于极端」的共振型状态，不是程度量。用条件命中计数（M5）而非乘积（任一因子为 0 会整体归零）或加权平均（违反单成员 ≤25%，且三个 0–100 分位相乘不可读）。<br>**公式**：`valid = {因子: 分位 > NULL}`，`k = len(valid)`，`hit = Σ1{p_i > 80}`；`k<2 → NA`；`hit>=3 且 k=3 → EXTREME`；`hit>=2 → HIGH`；`hit==1 → WATCH`；否则 `NORMAL`。**缺失因子既不进分子也不进分母**（`k` 减 1），不得按 0 计。<br>**参数**：`CROWD_PCT_HIGH=80.0`；`k=3`（CN 阶段 2）/ `k=2`（HK 及 CN 阶段 1 部分模式，`partial=true`，**不出 EXTREME**）。<br>**落地形态**：`crowd_gate({...}) → dict(gate, hit, k, partial, members)`。<br>**门禁**：`k<2 → gate=NA`、`coverage=0` |
| **输出** | `score` **恒 NULL**；档位在 `extra.crowd_gate`，并带 `extra.k` / `extra.partial`；`members_used=[]`；`side='risk'`。下游：L3 用 `gate ∈ {HIGH, EXTREME}` 触发 `CROWDING` 闸门，**不并入 `risk_side` 数值** |
| **前端展示** | 展示档位中文（正常 / 关注 / 偏高 / 极高 / 不可用）+ 「k 个因子中命中 hit 个」；`partial=true` 时必须显示「2/3 因子，不判定极高」；编码不得直接上屏；阶段 4 验证未通过前**不得写成「风险提示」或「减仓信号」** |

### 5.5 行业基本面与盈利预期 · `FUNDAMENTAL`

| 块 | 内容 |
|---|---|
| **定义** | 中文名称：行业基本面与盈利预期；编码 `FUNDAMENTAL`；状态侧；**问句：行业内公司盈利在变好还是变差？分析师预期在上调还是下调？**；`score` 0–100，高分 = 更改善；**仅 CN，起点 2021-12-13**；HK `unavailable`。**不回答**：中观景气（量/价/库存/产能，未采集）、目标价修正 |
| **输入** | 4 个 L1 原子的 `percentile_xsec`：`SEC.FUND.ROE_MEDIAN`（行业ROE中位数）、`SEC.FUND.NP_YOY_MEDIAN`（行业净利同比中位数）、`SEC.FUND.IMPROVEMENT_BREADTH`（行业盈利改善宽度）、`SEC.FUND.REVISION_BREADTH`（行业盈利修正宽度）；上游 `financial_indicator` + `analyst_forecast_snapshot` × `constituent` |
| **计算模型** | **原理**：财务是季频台阶、预期是每日快照，两者频率不同；取各自当日横截面分位后等权，可避免量纲与频率差异。4 成员各 25% 恰好满足单成员权重上限。HK 因 `financial_indicator.announce_date` 全 NULL、一致预期仅 A 股，无法 PIT 聚合 → `unavailable`。<br>**公式**：`score = mean(p_i)`（`i` 为 4 成员，全 `sign=+1`）；**有效成员 <2 → `score=NULL`**。<br>**参数**：`fund_coverage>=0.6` / `forecast_coverage>=0.3` 门禁（上游聚合层）；财务 PIT「月批次 + 次月 1 日生效」；`REVISION_BREADTH` 滚动 30 快照、±0.5% 噪声阈值。<br>**落地形态**：`df[4 个 _eq_ 列].mean(axis=1, skipna=True)`，再按 `<2` 有效成员置 NaN。<br>**门禁**：CN 2021-12-13 前无成分 → 4 成员全 NULL → `score=NULL` |
| **输出** | `side='state'`、`score`、`coverage`、`confidence`（因 `revision_n` 仅 25 → `low`）、`members_used`（4 个 code）、`extra.structure='equal'` / `extra.weight=0.25` / `extra.calib_break='s1.1→s2.1 单锚/未启用 → 分块等权'`。下游：参与 CN `state_side` |
| **前端展示** | 显示 0–100 分 + 「由 4 项等权合成」并允许展开 4 个成员中文名与各自分位；**必须标注「财务数据最多滞后一个月、盈利修正窗口尚未满 30 快照」**；HK 显示「本期不可用 + reason」 |

### 5.6 行业内部参与广度 · `BREADTH`

| 块 | 内容 |
|---|---|
| **定义** | 中文名称：行业内部参与广度；编码 `BREADTH`；状态侧；**问句：上涨是全行业一起涨，还是只靠少数几只股票撑着？**；`score` 0–100，高分 = 广度更健康；CN / HK 均产出。**不回答**：市场级宽度（第③层 `SECTOR.UP_RATIO` 是「多少行业在涨」） |
| **输入** | 4 个 L1 原子的 `percentile_xsec`：`SEC.BREADTH.UP_RATIO`（行业内上涨占比）、`SEC.BREADTH.ABOVE_MA60`（行业内站上MA60占比）、`SEC.BREADTH.NEW_HIGH_LOW`（行业内新高−新低归一净占比）、`SEC.BREADTH.RET_DISPERSION`（行业内收益离散度） |
| **计算模型** | **原理**：三项是「多少成分参与」的归一比率（天然平稳，不随股票池扩缩漂移），第四项是分化程度，方向相反需翻转；等权后得到「参与是否广泛」的横截面读数。<br>**公式**：`score = mean(p_UP, p_MA60, p_NHL, 100 − p_DISP)`；**有效成员 <2 → `score=NULL`**。`RET_DISPERSION` 的 `direction='high_risk'`，由 `sign=-1` 唯一翻转。<br>**参数**：`MA60` 需 60 交易日暖机；`NEW_HIGH_LOW` 60 日窗口且必须归一（不得用家数差）；`XSEC_MIN_N=10`。<br>**落地形态**：`df[4 个 _eq_ 列].mean(axis=1, skipna=True)`。<br>**门禁**：`ABOVE_MA60` 在起点后首 60 日 NULL 但不阻塞出分（只要 ≥2 个成员有效） |
| **输出** | `side='state'`、`score`、`coverage`、`confidence`、`members_used`（4 个 code）、`extra.structure='equal'` / `weight=0.25`。下游：参与 CN / HK 的 `state_side` |
| **前端展示** | 显示 0–100 分 + 4 个成员中文名与各自分位；**必须标注「与市场级『多少行业在涨』不是一回事」**；`RET_DISPERSION` 已翻转，展示时不得再次翻向 |

### 5.7 L2 输出样例（修正版）

```json
{
  "market": "HK", "sector_code": "HK.LIST1019",
  "trade_date": "2026-11-13", "dimension_code": "CROWDING", "side": "risk",
  "score": null, "coverage": 1.0, "dispersion": 0.0, "confidence": "low",
  "members_used": [],
  "params_hash": "…",
  "extra": {"structure": "rule", "anchor": null, "members": [],
            "crowd_gate": "HIGH", "k": 2, "partial": true,
            "pit_capable": false}
}
```

下游接口：L3 按 `(market, sector_code, trade_date, dimension_code)` 点查；`CROWDING` 读 `extra.crowd_gate`，不读 `score`。

> ⚠ **六维分不可横向比较或求均值**：分位基数、聚合结构、语义都不同。唯一合法的数值使用是各自按 `side` 参与 L3 的 `state_side` / `risk_side` 均值（且仅同侧、同 `market`）。

---

## 6 L3 业务指标

> 🔴 **v1.3 结构变更**：L3 由宽表（`business_value`，一行业一日一行多列）改为**长表** `industry.business_value`（一行业一日一业务指标一行），并新增 `industry.business_dict` 字典。旧设计把设计稿 §1.3 的 5 类业务交付物压成一行多列，没有 `biz_code`、无法各自版本化 / 重算 / 增减；现按业务指标编码分行，**新增或下线业务指标只增删字典行与值表行，不加表字段**。代码真源为 `sector_layer.BIZ_DEFS` / `sector_layer_hk.HK_BIZ_DEFS`。

### 6.0 业务指标字典（`industry.business_dict`）

| `biz_code` | 中文名称（本文补写建议） | 面向用户的一句话问句（本文补写建议） | `kind` | 主输出列 | CN / HK |
|---|---|---|---|---|---|
| `SEC.BIZ.STATE_SIDE` | 状态侧热度 | 这个行业当前整体状态在同日所有行业中偏强还是偏弱？ | `continuous` | `score` | ✅ / ✅ |
| `SEC.BIZ.RISK_SIDE` | 风险侧水平 | 这个行业当前估值风险高不高？ | `continuous` | `score` | ✅ / ✅ |
| `SEC.BIZ.QUADRANT` | 状态象限 | 状态与风险组合起来，当前属于哪一类局面？ | `enum` | `state` | ✅ / ✅ |
| `SEC.BIZ.RANK` | 当日状态排名 | 今天所有行业按状态排序，它排第几？共几个可比行业？ | `rank` | `level` | ✅ / ⛔ HK 字典登记但 `is_active=false` |
| `SEC.BIZ.GATE` | 风险与可用性闸门 | 哪些原因让今天的结论需要打折看？ | `gate` | `detail` | ✅ / ✅ |

字典主键 `biz_code`，一行一概念，市场差异落 `params.per_market`；每行带 `inputs` / `params` / `output_schema`（多态列语义）/ 独立 `params_hash` / `is_active` / `remark`。**新增指标 = 加一行字典 + 计算层加一个分支；下线 = 改 `is_active=false`**。

### 6.1 逐指标规格（前端展示的直接依据）

| `biz_code` | 解决什么问题 | 输入 | 计算模型 | 输出形式 / 值域 | 为空时 |
|---|---|---|---|---|---|
| `SEC.BIZ.STATE_SIDE` | 行业当前整体状态偏强还是偏弱？ | 当期**有分的**状态侧维度 `score`（CN 4 组 / HK 2 组） | 等权均值 `mean(skipna=True)`；有效维度 `<2 → NULL`；`detail.state_members/state_n/partial` | `score` 0–100；`state`/`level` 恒 NULL | 状态维度 <2 组有分 |
| `SEC.BIZ.RISK_SIDE` | 行业当前估值风险高不高？ | `VALUATION.score` | 直取，不重算 | `score` 0–100，**高分 = 更贵** | 估值锚无分 |
| `SEC.BIZ.QUADRANT` | 状态与风险组合起来是哪类局面？ | `state_side` / `risk_side` + 当日同市场横截面中位数 | `hi_s = state >= median(state)`；`hi_r = risk >= median(risk)` → 四象限 | `state` 为 5 个枚举中文字符串（含 `NA`）；`score`/`level` 恒 NULL | 任一侧 NULL → `NA` |
| `SEC.BIZ.RANK` | 今天它排第几？共几个可比行业？ | 同日同市场 `state_side` | `rank(ascending=False, method='min')`；`rank_n = count(state_side)` | `level` 1..`rank_n`；`detail.rank_no/rank_n` | `applicable=false` 时 NULL；**HK 无此行** |
| `SEC.BIZ.GATE` | 哪些原因让结论需要打折看？ | `coverage`、`crowd_gate`、日期、状态维度数 | 规则见 §6.3；`detail.gate_flags[]` | `state`/`score`/`level` 恒 NULL；全量在 `detail` | 不为空（无闸门时 `[]`） |

**共有质量元数据列**（每行都带，不随 `biz_code` 变化）：`coverage`（可用维度 / 已启用维度，CN 分母 6 / HK 分母 4）、`applicable`、`confidence`、`params_hash`（该 `biz_code` 独立指纹）、`extra`（`enabled_dimensions` / `calib_version` / `pit_capable` / `rank_n`；HK 另有 `no_rank` / `no_rank_reason` / `survivorship_bias` / `granularity`）。

> ⚠ 五个业务指标**只描述当日横截面状态**，不得展示为「看好 / 看空」「建议买入」；阶段 4 前瞻 IC 检验未通过（§9 第 16 条）。
> ⚠ `STATE_SIDE` 与 `RISK_SIDE` **不相加、不求均值**：一个「越贵越危险」、一个「越强越健康」，合成会产生伪精确。

### 6.2 计算模型与参数

```
state_side = mean(当期有分的状态侧维度 score)       # 有效维度 <2 → NULL
risk_side  = VALUATION.score
quadrant   = quadrant_of(state_side, risk_side, median_t(state_side), median_t(risk_side))
rank_no    = rank(ascending=False, method='min')     # 同 (market, trade_date)
coverage   = 可用维度数 / len(ENABLED_DIMENSIONS)   # CN 6 / HK 4
applicable = coverage >= 0.6
             AND valid_days_20 >= 15
             AND 'DATA_COVERAGE' not in gate_flags    # CN / HK 共同
             AND 'BREAK' not in gate_flags            # ⚠ 仅 HK 代码含此项；CN 不写 BREAK，等价恒真
confidence = confidence_of(pe_n_sample, coverage, pit_capable)
```

| 参数 | 值 | 生效步骤 | 作用 | 变更后果 |
|---|---|---|---|---|
| `ENABLED_DIMENSIONS` | CN 6 维 / HK 4 维 | `coverage` 分母、`state_side` 成员范围 | 决定分母与参与状态侧的维度集合 | 口径变更；须换 `CALIB_VERSION` + 全量重算 |
| `coverage_min` | 0.6 | `applicable` 判定、`DATA_COVERAGE` 闸门 | 只改 `applicable` / 闸门，不改 `score` | 口径变更 |
| `valid_days_20` 门槛 | 近 20 交易日 ≥15 日有 `rs_20d` | `applicable` 判定 | 防止刚起步的行业出排名 | 口径变更 |
| `CAP_START`（HK） | 2002-01-01 | `BREAK` 闸门 | 2002 前 `total_market_val` 覆盖率 <6% | 口径变更 |
| `SHORT_SAMPLE` | 750 | `confidence_of` | **只降置信**，不进闸门、不改 `score` | 口径变更 |
| `CALIB_VERSION` | CN `s2.1` / HK `hk1.0` | 各 `biz_code` 的 `params_hash` | 区分口径段 | 换版本即全量重算 |

### 6.3 闸门（`SEC.BIZ.GATE.detail.gate_flags`）

> ✅ **`SHORT_SAMPLE` 不产出闸门是有意设计**：设计稿 §3.6 实施期细化 ① 与 `_biz_rows` 代码注释一致 —— 若按全局 `n_sample<750` 触发，快照期所有行业 `applicable=false`、不产出任何排名。信息由 L1 `quality='short_sample'` 与 L2/L3 `confidence='low'` 承载。⚠ `BREAK` **仅 HK 写入**（2002 前 `total_market_val` 覆盖率 <6%）；CN 早期段已由 `DATA_COVERAGE` 挡住，效果等价。

| 闸门 | CN | HK | 触发条件 | 后果 |
|---|---|---|---|---|
| `DATA_COVERAGE` | ✅ | ✅ | `coverage < 0.6` | `applicable=false`；CN 不出排名 |
| `BREAK` | ❌ | ✅ | `trade_date < 2002-01-01` | HK `applicable=false`；`extra.break_notes` |
| `CROWDING` | ✅ | ✅ | `crowd_gate ∈ {HIGH, EXTREME}` | 仅提示，不屏蔽排名 |
| `PARTIAL_STATE` | ✅（条件） | ✅（恒开） | CN：`state_n < 状态维度数`；HK：状态侧仅 2 组 | 降置信 / `detail.partial` |
| ~~`SHORT_SAMPLE`~~ | — | — | 不产出闸门 | 由 `quality` + `confidence` 承载 |

> ⚠ HK 不排名是独立决策：字典登记 `SEC.BIZ.RANK` 但 `is_active=false`、值表无该行；`SEC.BIZ.GATE.detail.no_rank / no_rank_reason` 与共有 `extra` 双处说明。**前端不得用 `applicable` 推断 HK 是否有排名**。

### 6.4 输出样例（长表，每行业日 5 行）

```json
// 行 1/5：状态侧热度
{"biz_code":"SEC.BIZ.STATE_SIDE","market":"CN",
 "sector_code":"801780","trade_date":"2026-11-11","score":58.32,
 "state":null,"level":null,"detail":{"state_members":["dim_BREADTH","dim_FLOW","dim_FUNDAMENTAL","dim_TREND"],"state_n":4,"partial":false},
 "coverage":1.0,"applicable":true,"confidence":"low","params_hash":"…",
 "extra":{"enabled_dimensions":["TREND","FLOW","VALUATION","CROWDING","FUNDAMENTAL","BREADTH"],
         "calib_version":"s2.1","pit_capable":true,"rank_n":31}}
```
```json
// 行 5/5：闸门
{"biz_code":"SEC.BIZ.GATE","market":"CN",
 "sector_code":"801780","trade_date":"2026-11-11","score":null,"state":null,"level":null,
 "detail":{"gate_flags":["CROWDING"],"crowd_gate":"HIGH","crowd_k":3,"crowd_partial":false},
 "coverage":1.0,"applicable":true,"confidence":"low","params_hash":"…",
 "extra":{"calib_version":"s2.1","pit_capable":true,"rank_n":31}}
```

**出向契约**：只承诺「表 + 列 + 字典 `output_schema` + `params_hash` 语义」，不承诺行数。消费方**必须按 `biz_code` 过滤并校验 `output_schema`**，不得 `SELECT *`、不得跨 biz 比较 `score` / `state` / `level`。

---

## 7 前端展示契约

> 本章回答「前端拿这些数据怎么呈现，才能让用户清楚知道每个指标解决什么问题、算法是什么、输入输出长什么样」。只定义**数据到界面的映射契约**，不定义视觉样式。

### 7.1 每个指标必须呈现的三段信息

| 段 | 作用 | L1 原子指标的数据来源 | L2 维度 / L3 字段的数据来源 |
|---|---|---|---|
| ① **展示名** | 让用户知道看的是什么 | `indicator_dict.indicator_name`（**已落库**，§4 各表已列出） | ⚠ **本文 §2.1 / §5.0 / §6.1 补写的建议名，代码未落库**，前端采用前需确认措辞 |
| ② **一句话问句** | 让用户知道这个指标解决什么问题 | 字典 `remark` 的「定义」段 + 本文 §4 的「定义」列 | ⚠ 本文 §5.0 / §6.0 补写的建议问句，**非落库字段** |
| ③ **读数 + 单位 + 位置** | 让用户看到今天的数与它在什么刻度上 | `raw_value` + `unit`，配 `percentile_xsec`（同日同侪位置）/ `percentile_ts`（自身历史水位） | `score` 0–100（L2）/ `state_side`·`risk_side`·`rank_no`（L3）；枚举值取 `extra.label` 或 `quadrant` |

> ⚠ **②的文字若要上屏，属于新增用户可见文案，须经确认后再定稿**；本文给出的是数据契约建议，不是已批准的 UI 文案。

### 7.2 「算法详情」展开面板的字段清单

用户点击某指标查看详细算法时，前端应能展示下列六项；括号内为取数来源：

| 展示项 | 取数来源 | 说明 |
|---|---|---|
| 指标定义 / 单位 / 值域 | 字典 `indicator_name` / `unit` / `remark`「定义」段（L2/L3 用本文 §5 / §6） | 一句话说清算的是什么 |
| **输入** | 字典 `source` / `formula`；L2 用 `members_used` 联字典取各成员中文名；L3 用 `extra.state_members` | 列出上游表·列 + 原子指标中文名 |
| **计算模型 / 公式** | 字典 `formula` + `model_key`（M0–M8 说明见 §2.4）；L2 用 `extra.structure` / `extra.anchor` / `extra.weight`；L3 用 §6.2 | 把公式文字与参数一起展示，不要只给数字 |
| **参数与门禁** | 字典 `min_periods` / `params`；§2.4 通用规则；L2 `extra.reason`；L3 `gate_flags` | 例如「时序分位需 250 个有效观测，当前 27 个，故为空」 |
| **输出形式** | `raw_value` / `percentile_xsec` / `percentile_ts`（L1）；`score` / `extra.crowd_gate`（L2）；§6.1 各列（L3） | 标明单位、值域、是否可空 |
| **口径版本与可追溯** | `model_version`（L1）/ `params_hash`（L2/L3）/ `extra.calib_version` / `extra.pit_capable` | 让用户 / 客服能判断「这行数属于哪套口径」 |

### 7.3 上屏纪律

| 类别 | 内容 |
|---|---|
| **可直接上屏** | L1 中文名 + `raw_value`（带 `unit`）、L1 两种分位（须标明「同日同侪」/「自身历史」）、L2 `score`（须带 `side` 极性说明）、L3 `state_side` / `risk_side` / `rank_no` / `quadrant`、`confidence`、`gate_flags` 中文标签 |
| **必须带限定语** | `rank_no`：必须标注「当日横截面状态排序，非投资建议、非未来收益预测」；`risk_side`：必须标注「分数越高 = 越贵」；`CROWDING` 档位：必须标注 `extra.k` / `partial`，`partial=true` 时显示「2/3 因子，不判定极高」；HK 任何读数：必须标注「非 PIT / 含幸存者偏差 / 置信度低」 |
| **必须先解码再上屏** | `raw_value` 为枚举编码时（`RRG_QUADRANT` 1–4、`CROWD.GATE` 0–4）取 `extra.label`；`dimension_code` / 闸门 / `structure` 等英文枚举按 §2.1 / §5.0 / §6.3 映射中文 |
| **禁止展示** | 单一综合分（本层不产出）；「超配 / 低配 / 买入 / 卖出」字样；把 `quadrant` 解释为建议；把 HK 排名留空却暗示「暂无数据」（应说明「本期不排名」）；把负 IC 反向解读为做空信号；跨 CN/HK 混排或同图比较 |

### 7.4 空值、低置信与闸门的呈现

| 情形 | 判定 | 呈现 |
|---|---|---|
| 指标全空 | L1 表**不写行**（§2.3.1） | 显示「该指标当日无数据」，不要填 0 / 50 |
| 时序分位为空 | **M2 类指标** `percentile_ts IS NULL` 且 `n_sample < 250`；M1 类指标本就不取时序分位 | M2 显示「历史样本不足（n=27 / 需 250），暂不提供历史分位」；M1 直接不展示历史分位入口。两类都仍可显示 `raw_value` 与横截面分位 |
| 维度不可用 | L2 `extra.structure='unavailable'` | 显示「本期不可用」+ `extra.reason`，不是显示 0 分 |
| 拥挤度无 `score` | `dimension_code='CROWDING'` | 显示档位（`extra.crowd_gate`），不要画 0–100 分数条 |
| 低置信 | `confidence='low'` | 加「低置信」标记并在详情说明原因（短样本 / 覆盖不足 / 非 PIT） |
| 不可出结论 | `applicable=false` | **隐藏排名与象限**，只显示「数据不足」+ `gate_flags` 中文说明 |
| 口径分段 | `params_hash` / `extra.calib_version` 变化 | 时间序列按口径分段显示并标注断点，不得把 `s1.1` 与 `s2.1` 连成一条线 |

### 7.5 市场隔离与页面结构建议

- CN（`SW_L1`，31 行业）与 HK（`HS_L3`，113 行业）**必须是两个独立视图**，不共用排名榜、不共用分位刻度、不共用一条时间轴；两市场同名指标中文名可能不同（§4 用 `/` 并列），按当前视图取对应名称。
- HK 视图：隐藏排名列；`FLOW` / `FUNDAMENTAL` 显示「本期不可用 + 原因」；`PARTIAL_STATE` 恒开、`confidence` 恒 `low`，需在页面级显式声明。
- CN 视图：2021-12-13 前的历史段 `pit_capable=false`、`coverage` 最高 0.5 → `applicable=false`，长历史仅适合展示**不依赖成分的价格类原子**（A/B/C/D 组），不要在该段展示六维或排名。

---

## 8 数据使用方查询手册

> 所有示例为只读 SQL；值表查询必须带 `market`，字典按 `indicator_code` / `biz_code` 单列点查。`%` 字面在 Python 参数化执行（`cur.execute(sql, params)`）时须双写成 `%%`，否则 psycopg2 会误判为格式符。

| 我要… | 读哪张表 | 要点 |
|---|---|---|
| 某行业某天的全部原子读数 + 中文名 | `indicator_value` JOIN `indicator_dict` | 按 5 列主键点查；联字典取 `indicator_name` / `unit` / `formula` / `remark`；先看 `quality` / `n_sample` |
| 某行业当期六维分 | `dimension_value` | `CROWDING` 读 `extra->>'crowd_gate'`，不读 `score`；`dispersion` 恒 0 不要用 |
| 某天行业排名（CN） | `business_value` + `business_dict` | 按 `biz_code='SEC.BIZ.RANK'` 过滤，`WHERE market='CN' AND applicable AND level IS NOT NULL ORDER BY level` |
| 判断某行业这天能不能当结论用 | `business_value` | 看 `applicable` + `SEC.BIZ.GATE` 行的 `detail.gate_flags` + `confidence`；HK 不据此推断排名 |
| 取当时成分股（PIT） | `constituent` | `trade_date >= eff_from AND (eff_to IS NULL OR trade_date < eff_to)` |
| HK 两版指数的差异 | `hk_index_daily` | 同时取 `ret_eq` / `ret_cap`、`n_constituent`；小成分行业须自行门禁 |
| 查口径是否变了 | 字典 `params_hash` vs 值表 `model_version` / `params_hash` | 不等即旧口径；L2/L3 的 `params_hash` 无对应字典行，须与 `sector_layer._dim_hashes()` / `_biz_params_hash()` 的当前值比对（§2.2）。⚠ 指纹变化只让新旧口径可区分，**不会自动重写历史行** |

```sql
-- 1) CN 某日有效排名（禁止跨市场混排；L3 长表按 biz_code 过滤）
SELECT v.sector_code, v.level AS rank_no,
       v.detail->>'rank_n' AS rank_n, v.confidence,
       d.biz_name, d.question
FROM industry.business_value v
JOIN industry.business_dict d
  ON d.biz_code = v.biz_code
WHERE v.market = 'CN'
  AND v.biz_code = 'SEC.BIZ.RANK' AND v.trade_date = DATE '2026-11-11'
  AND v.applicable AND v.level IS NOT NULL
ORDER BY v.level;

-- 1b) 某行业当天的完整业务读数（5 行，中文名与问句来自字典）
SELECT v.biz_code, d.biz_name, d.question, v.score, v.state, v.level,
       v.detail, v.coverage, v.applicable, v.confidence
FROM industry.business_value v
JOIN industry.business_dict d
  ON d.biz_code = v.biz_code
WHERE v.market = 'CN'
  AND v.sector_code = '801780' AND v.trade_date = DATE '2026-11-11'
ORDER BY v.biz_code;

-- 2) 某行业的六维读数、中文名所需信息与拥挤档位
SELECT dimension_code, side, score, coverage, confidence, members_used,
       extra->>'structure' AS structure,
       extra->>'crowd_gate' AS crowd_gate,
       extra->>'reason' AS unavailable_reason
FROM industry.dimension_value
WHERE market = 'CN'
  AND sector_code = '801780' AND trade_date = DATE '2026-11-11'
ORDER BY dimension_code;

-- 3) 原子读数 + 字典中文名 / 公式 / 三段式说明（算法详情面板取数）
SELECT v.indicator_code, d.indicator_name, d.unit, v.raw_value,
       v.percentile_xsec, v.percentile_ts, v.n_sample, v.quality,
       d.formula, d.model_key, d.remark
FROM industry.indicator_value v
JOIN industry.indicator_dict d
  ON d.indicator_code = v.indicator_code
WHERE v.market = 'CN'
  AND v.sector_code = '801780' AND v.trade_date = DATE '2026-11-11'
ORDER BY v.indicator_code;

-- 4) PIT 成分取用（CN，2021-12-13 起）
SELECT stock_code, weight, eff_from, eff_to
FROM industry.constituent
WHERE market = 'CN' AND sector_code = '801010'
  AND DATE '2026-11-11' >= eff_from
  AND (eff_to IS NULL OR DATE '2026-11-11' < eff_to)
ORDER BY weight DESC;
```

> ⚠ `SELECT *` 禁止用于生产消费：维度 / 业务列可能增删，必须按白名单取列（§6.4）。

---

## 9 验收

| # | Given | When | Then |
|---|---|---|---|
| 1 | 任一 L1 分位 | 用 t 日之后的数据重算 | 历史行分位逐值不变（as-of，不刷写） |
| 2 | 改 `INDICATOR_DEFS` 的公式 / 单位 / 极性 / 模型 / `CALIB_VERSION` | 重算 | `params_hash` 变化，`model_version` 同步，该指标历史全量重算 |
| 3 | 只改字典 `remark` | 重算 | `params_hash` 不变、历史读数不变；但库内手改会被幂等 upsert 覆盖 |
| 4 | M2 时序分位类指标 `n_sample = 27`（如 `SEC.VAL.PE_TS_PCT`） | 计算 L1 | `percentile_ts IS NULL`、`quality='short_sample'`，**不得用 27 日分位填充**；M1 横截面类指标的 `n_sample` 是当日行业数，不适用本条 |
| 5 | 当日有效行业数 `n_valid = 9` | 计算 M1 | `percentile_xsec IS NULL`（`<10` 不给分位） |
| 6 | CN 与 HK 同一交易日 | 查询排名 | 不得出现同一 ranking 序列；HK `rank_no` 恒 NULL |
| 7 | `dimension_code='CROWDING'` | 读 L2 | `score IS NULL`、`members_used=[]`、档位在 `extra.crowd_gate`，k / partial 随行透出 |
| 8 | HK `FLOW` / `FUNDAMENTAL` | 读 L2 | 各有一行 `score=NULL` + `extra.reason`，不是缺行 |
| 9 | `coverage = 0.50` | 读 L3 | `gate_flags` 含 `DATA_COVERAGE`、`applicable=false`、`rank_no IS NULL`（CN） |
| 10 | `quadrant ≠ 'NA'` | 读 L3 | 象限名不得被解释为超配 / 低配 / 买卖含义 |
| 11 | 同一 `(market, trade_date)` 连续跑两次 | 重算 L3 | 除 `updated_at` 外逐字段相等（幂等） |
| 12 | **改 L2 的 `anchor` / `members` / `sign` 但不动 `structure`** | 重算 | `params_hash` **必须变化**，且因 L3 纳入六维指纹，L3 `params_hash` 同步变化（§2.2）；指纹变化后须全量重算 L2+L3 并按版本分段展示。**改 `remark` / `indicator_name` 时两处指纹均不得变化** |
| 13 | `CALIB_VERSION s1.1 → s2.1` | 对比两期 L2/L3 | `params_hash` 全变、`extra.calib_break` 登记断点；两期读数不可直接比 |
| 14 | CN 成分表 `eff_from` 最小 2021-12-13 | 取 2021-12-12 的自下而上指标 | 无读数；不得用当前成分回算后声称 PIT |
| 15 | HK 两版指数同一行业日 | 对比 `ret_eq` / `ret_cap` | 差异与 `n_constituent` 随行可查；不静默截尾、不改值 |
| 16 | 阶段 4 前瞻 IC 样本外区间跨 0 | 决策闸门 | 排序维持「当前状态描述」、权重维持等权、不得宣称有效；负 IC 不得反向解读为做空信号 |
| 17 | 任一交易日的 L3 行 | 检查 `gate_flags` 与 `confidence` | **不得出现 `SHORT_SAMPLE`**（设计上归 `confidence`，见 §6.3），但短样本日 `confidence` 必须为 `low`；CN **不得出现 `BREAK`**；HK 2002 前必须出现 `BREAK` |
| 18 | 任一 L1 行 | 检查 `coverage` | CN 恒 NULL、HK 为成分覆盖度；不得把 CN 的 NULL 当作「覆盖度为 0」 |
| 19 | 前端展示某指标 | 取三段信息 | ① 展示名（L1 取字典 `indicator_name`；L2/L3 取本文建议名）② 一句话问句 ③ 读数 + 单位 + 算法详情（§7.2 六项）齐备 |

---

## 附录 A 数据源清单

| 源 | 表 | 用途 |
|---|---|---|
| 行业日聚合 | `regime.sector_daily` | A 组价格 / RS / 动量、C 组资金净流、D 组成交额占比的上游；由 `market_state_daily.calc_sector_daily` 产出 |
| 行业基准 | `public.daily_benchmark`（`CN.SW*` 31 个申万一级指数） | 1999-12-30 起长历史价格；`turnover` 单位亿元（实测覆盖全 A 99.8%+） |
| 行业估值快照 | `regime.sector_valuation_snapshot` | CN 估值 PE / PB / 股息率；2026-09-30 起逐日累积（27 日） |
| 行业资金快照 | `regime.sector_fund_flow` + `sector_mapping` | 同花顺约 90 细分行业 → 申万 31；只纳入 `confirmed OR corr≥0.6`；2026-09-30 起（27 日） |
| 一致预期 | `regime.analyst_forecast_snapshot` | M7 盈利修正；A 股约 2,438 只；2026-09-30 起（25 日），仅 EPS + 评级五档 |
| 个股行情 | `a_daily_quote` / `hk_daily_quote` | 自下而上聚合（宽度、离散度、换手、估值） |
| 财务 | `financial_indicator` | ROE / 同比；CN 按 `announce_date` PIT，HK `announce_date` 全 NULL |
| 资金明细 | `daily_margin_balance` / `daily_ggt_hold` | A 股两融 / 港股南向按行业聚合 |
| 行业分类 | `public.stock_sector`（INDUSTRY / `HK.LIST*`）、`sector_hierarchy` | HK 113 个恒生三级行业；GICS 仅展示标签 |
| 成分主数据 | `industry.constituent` / `..._change` | CN 申万成分 PIT 版本链（AKShare `index_component_sw`）；2021-12-13 起 |
| HK 自算底座 | `industry.hk_index_daily` | 两版指数与行业聚合；等权 2000 起、加权 2002 起 |

⚠ CN `sector_valuation_snapshot` 与 HK `pe_agg` **不同构**（官方快照 vs 个股调和聚合），不得拼接成一条时序。⚠ 快照类源断档即永久缺失，监控 `day/1`；北向行业明细源已停披露，不追。

## 附录 B 关键口径决策索引

| # | 决策 | 理由 |
|---|---|---|
| B-1 | 目标从「给配置建议」改为「状态读数 + 风险闸门」 | 「超配」隐含未来相对收益判断，与「度量非预测」定位冲突；前瞻 IC 未通过 |
| B-2 | 不产出单一综合分，状态侧 / 风险侧分离 | 异质信息扁平平均产生「低估值抵消高拥挤」的伪精确 |
| B-3 | CN / HK 共用 4 张输出表，物理分行；字典 PK 扩为复合键 | 消费端统一；避免 HK 同名指标覆盖 CN 行；不混排 |
| B-4 | HK 本期只做 A/B/D/F + 南向，L3 不排名 | 资金仅 136 日、财务无公告日期；113 vs 31 不同构 |
| B-5 | 自下而上指标起点限 2021-12-13 | 免费源无完整 PIT 成分与剔除记录；不得用当前成分回算后声称 PIT |
| B-6 | M8 加权版改用 `total_market_val` | `circular_market_val` 2000–2023 全 NULL、2024-05 起仅 65 日零星有值 |
| B-7 | 拥挤度用条件命中计数，不用乘积 / 加权平均 | 乘积使任一因子为 0 时整体归零；分位相乘不可读；加权违反单成员 ≤25% |
| B-8 | 快照类首期只出横截面分位，时序分位置 NULL | 27 / 27 / 25 日 « `min_periods=250`；诚实空缺优于虚假精确 |
| B-9 | 阶段 4 判定排序「不通过」后维持等权、只作状态描述 | 样本外 IC bootstrap 区间全部跨 0、分组单调性不显著；负 IC 不构成做空信号 |
| B-10 | `remark` 三段式全量重写，且不进 `params_hash` | 统一可读性；只改文案不换指纹、不触发无谓历史重算 |
| B-11 | **规格文档以代码为最终裁决，规划口径不得写成「已实现」** | v1.0 把设计稿的五类闸门、L1 `coverage` / `quality` 取值、L2 指纹 payload 当作生效规格，与实际写库逻辑不符；v1.1 逐项核对修正 |
| B-12 | **L2/L3 口径指纹纳入结构参数 + 上游 L1 定义 + 闸门阈值** | 旧 payload 只含 `structure`，改锚 / 改成员 / 翻 sign / 改 L1 定义 / 改阈值都不换指纹。v1.2 改为纯函数 spec + 规范化序列化，建立 `L1→L2→L3` 自动传导；不读库、循环外预计算。文案类字段明确排除 |
| B-13 | **`SHORT_SAMPLE` 不做闸门，短样本只降置信** | 设计稿 §3.6 实施期细化 ① 与代码注释一致：按全局 750 触发会让快照期所有行业 `applicable=false`。信息由 L1 `quality` + L2/L3 `confidence` 承载。v1.2 撤回 v1.1 的缺陷定性，按设计意图登记 |
| B-14 | **第④层迁入独立 `industry` 域：迁入 7 张专属表 + 新建 `business_dict`，域内 8 张** | 5 张行业输入表（`sector_daily` / 估值 / 资金流 / 映射 / 一致预期）被 `market_state_daily.py` 第③层直接读写，全迁会让 18:45 市场状态计算报「表不存在」；同库跨 schema 只读无成本，故留在 `regime` |
| B-15 | **L3 改长表 + `business_dict`，`biz_code` 进主键** | 旧宽表把设计稿 §1.3 的 5 类业务交付物压成一行多列，没有指标编码 → 无法各自版本化 / 重算 / 增减，且与第③层 `business_value` 长表范式不一致。长表 + 字典使业务指标成为开放集合：新增/下线只改字典行，不加列；每个 `biz_code` 独立 `params_hash`。HK 的 `SEC.BIZ.RANK` 字典登记但 `is_active=false`，值表不产出 |
