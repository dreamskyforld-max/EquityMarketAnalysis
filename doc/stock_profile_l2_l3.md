# 个股画像 L2/L3 结论指标 · 数据契约设计文档

| 项目 | 内容 |
| --- | --- |
| 文档版本 | **v1.0**（进度与生产状态见 §12.2） |
| 编制日期 | 2026-11-12 |
| 适用范围 | 个股画像现有 L1 标签（③④⑤⑥⑦⑩ 等）之上的结论层 |
| 文档性质 | **数据架构 / 数据契约 + 口径规格**；不含实现代码、不建表 |
| 规格真源 | 本文 §3–§7（口径）/ §4.3.1（规则契约 schema）/ §4.6（落表与版本策略）/ 附录 D（enum 值域）/ §8（数据源）/ §10（验收） |
| 上位文档 | `doc/stock profile hd.md`（12 域设计，§4.2 / §4.3 / §9）、`doc/market_state_data_architecture.md`（三层范式，§1.1 / §2.3 / §2.4）、`doc/market_profile.md` §5.0 |
| 消费方 | `stock-realtime`（`/Users/fredhan/mkt/stock-realtime`），读 PostgreSQL `profile.*` |

> **范围声明**：本文档只讨论**数据架构的设计与落地**（分层、编码、落表、口径、算法、数据源、计算调度、验收）。**前端如何展示、域卡顺序、页面交互均不在本文档范围**，属前端设计开发任务。文中引用消费端代码仅用于确认「按什么数据契约读」，不分析展示形态。

---

## 0 阅读契约

> **领域边界**：⑧ 题材概念、⑪ 事件风险、⑫ 预测评分三个空域**不在本期范围**。⑫ 预测评分继续冻结——本层全部结论为**描述性判断**，不是预测评分。

> **字段单一职责**：规则契约的只读发布物放 `tag_registry.conclusion_dict`、判定依据放 `tag_value.conclusion_detail`、口径版本放 `version`。三者各守本职；**不引入 `params_hash` 之类的内容指纹，不把哈希串或规则内容塞进 `version`**。详见 §4.3 / §4.4 / §4.6。

> **版本即口径**：规则升级 = bump `@tag(version=...)`，由 `sync_registry()` 贯通到 `tag_registry.version`，再由结论生产者写进 `tag_value.version`。`version` 表达「该标签 / 该值行的口径版本」；对结论标签而言口径就是判定规则，因此规则版本是它的**本职**，不是复用。被否掉的是把 `params_hash` 哈希串当版本号用。

> **结论行粒度**：结论采用**事件行**（值变化才写行），日变量只刷新当前有效行的 `conclusion_detail`。全部落表/覆盖/门禁动作以 **§4.6 为唯一真相源**，其余章节不得另立口径。

> **规则表值域**：规则条件里的字段与字面值**必须来自 L1 真实值域**（§4.3.1 的 `inputs[]` 声明 + 附录 B 的标签），不得使用 L1 中不存在的枚举码或虚构字段。所有 `field` 的来源、取值列、类型在 §5/§6 各表的「`inputs（判定用）`」栏逐项声明。

> **与 regime 三层的关系**：复用「L1 原子 / L2 维度 / L3 业务」的术语、分层纪律与「宁可不出档」的出档哲学（`build_dimension_value.py:23-26`）；**不复用其算法口径与 `params_hash` 字典机制**（regime 是一市场一日一行的时序 expanding 分位 + 标定脚本机器生成字典；个股是一票一日一行的横截面分位 + 代码显式定义规则）。

> **明确的非目标**（四条，任一条被突破即视为需求走偏）：
> 1. 不预测涨跌、不输出预期收益、不做方向概率；
> 2. 不输出买入 / 卖出 / 持有建议，不产出「合适 / 不合适」这类决策结论；
> 3. 不产出单一综合评分或可排序的总分（避免退回「打分排序」范式，`doc/stock profile hd.md:37-45`）；
> 4. 不引入预测模型（LightGBM / HMM / 蒸馏）与分钟级 `tick_data`（覆盖仅约 80 只）。

> **L1 现状与约束**：设计稿 12 域 / 123 标签；代码 `profiling/tags/` 只有 9 个域模块，注册 77 个标签、其中 **59 个 `active` 产出**、18 个 `planned_no_data`。`profile.tag_value` 是 EAV（`key_value` 分档/枚举 + `num_value` 连续值 + `confidence`）；`profiling/engine.py:167 compute_tag()` 是单标签独立事务，默认 `mode="auto"` 当日走 snapshot 原地 `UPDATE`，历史日才走版本链（关闭旧行 + 插新行）。**`profiling/` 内核内无任何跨标签聚合、无 score / state / verdict 列**（regime / 宏观模块有 `score`/`state` 列，不属画像内核）。

---

## 1 目标与范围

### 1.1 投资者问题 → 系统结论映射

普通投资者打开一只票，真正的问题是「**这公司值不值这个价、我现在买会不会站岗**」。下表把口语问题翻译成系统可回答、且**合规可发布**的结论；「不得写成」一列是硬约束。

| # | 投资者问的 | 系统回答的（业务语义） | 不得写成 | 层 | 主要数据域 |
| --- | --- | --- | --- | --- | --- |
| Q1 | 贵还是便宜 | 相对自身历史 / 同行，价格处在什么位置；双分位是否一致 | 「低估 / 高估」「值得买」 | L2 | ③ 估值水平 |
| Q2 | 公司好不好 | 盈利的水平、稳定性、真实性与杠杆 | 「优质股 / 好股票」「会涨」 | L2 | ④ 盈利质量 |
| Q3 | 长得快不快 | 增速、持续性、加速度，以及增收与增利是否背离 | 「高成长股」「未来增速」 | L2 | ⑤ 成长特征 |
| Q4 | 现在买合不合适 | **当前处于什么位置 / 状态**（滞后描述） | 「买点 / 卖点」「合适 / 不合适」 | L2 | ⑩ 趋势状态 + ⑦⑨ |
| Q5 | 综合起来是什么类型的标的 | 好公司 × 好价格的交叉类型 | 「推荐 / 评级 / 打分」 | L3 | ③④⑤ 交叉 |
| Q6 | 增长是不是真的 | 增长 × 现金流 / 盈利真实性的交叉类型 | 「可持续增长预测」 | L3 | ⑤④ 交叉 |

**Q5/Q6 是本层的价值核心**：三个单域结论都表达不出的「交叉信息」，正是 L3 的准入依据（§6.0 三判据）。只把某域重新切档不构成 L3。

### 1.2 三条产品纪律

| # | 纪律 | 理由 |
| --- | --- | --- |
| D1 | **描述不预测** | 项目实证：方向预测 AUC 0.44–0.54、OFI ~0.5、上行/下行前瞻收益分不开（CN f120 +7.2%/+7.6%，下行反而最高，`build_business_value.py:197-208`）；`doc/market_state_data_architecture.md:939,1760,1825-1828` |
| D2 | **便宜 ≠ 值得买** | 现有标签是横截面等频五等分（`profiling/quantile.py:8-12`，1=最低 / 5=最高），PE 档 1 只是「全市场最低 20%」，可能是价值陷阱 / 周期顶 / 亏损股。低分位不得直译为「便宜」，必须交叉 ④ |
| D3 | **结论必须带依据** | 每条结论都要能回溯到命中的原子标签与规则（字段设计见 §4.3，消费端读取契约见 §3.4） |

### 1.3 时间尺度区分（易被忽略）

③ 估值、⑦ 交易特征、⑩ 位置是**日频**（随价变）；④⑤ 成长与质量是**季频**（财报驱动）。L3 `biz_value_quality_type` 频率取上游最快侧为 `daily`，但**只在任一 L2 码变化时切换**（§4.6 事件行），不产生「财报没出但结论天天跳」。

> 数据契约里必须区分**档案结论**（慢变量、季度更新）与**今日状态**（快变量、日频）。各结论的 `update_freq` 按域分别声明（§5），`pit_capable` 按判定用输入的最弱环声明（§4.1 / §8.5）。

---

## 2 术语与分层

### 2.1 三层定义（对齐 regime §1.1，个股版）

```
外部源表（v_daily_quote / financial_indicator / stock_sector / dividend_* ...）
        │
        ▼
L1 原子指标   profile.tag_value        59 个 active 标签：分档 / 枚举 / 连续值
        │  只交 (stock_code, tag_code, key_value, num_value, eff_from/eff_to)
        ▼
L2 维度指标   profile.tag_value        dim_* 4 个 enum：贵不贵 / 好不好 / 快不快 / 什么位置
        │  只交 (stock_code, tag_code, key_value, eff_from/eff_to)
        ▼
L3 业务指标   profile.tag_value        biz_* 2 个 enum：好公司×好价格 / 增长质量
```

**关键决定：L2/L3 不新建数据表，仍落 `profile.tag_value`**，以「新标签」的形式注册进 `profile.tag_registry`。理由见 §3。

### 2.2 两条层间铁律（个股版）

1. **判定型结论从原子读数 / 原始量价算，不从被平均稀释的分档算。** 规则是条件合取，需要精确边界；先对多个分档取平均会丢掉边界信息（regime §5.0 铁律①同款，`doc/market_profile.md §5.0「三层之间的两条铁律」，约 :1397-1401`）。故 L2 输入是**各个 L1 标签的原值**（`key_value` 档位或 `num_value` 连续值），不是「域内平均分」。
2. **横截面分位与时序位置不得混进同一字段。** 现有 L1 的 1–5 档是「同一天全市场/行业内排第几」（横截面），`tst_price_position_52w` 是「个股跟自己 252 日区间比」（时序）。两者语义不同，禁止相加或互相覆写（`profiling/tags/trend.py:1-16`）。

### 2.3 与 regime 的四条差异（**不可照搬**）

| # | 差异 | 后果 |
| --- | --- | --- |
| A1 | **分位基准**：regime L1 是时序 as-of expanding 分位（250/750 有效观测门槛）；个股 L1 是横截面等频五等分 | regime 的 `coverage` = 成员数据完整度、`dispersion_norm` = 「平均分是否有代表性」；个股层 `coverage` 必须重定义为「该结论判定用输入的可得率」（§4.2），照抄公式无意义 |
| A2 | **成员齐备度**：⑦ 9/9、⑥ 6/6 齐；⑤ 4/8、⑨ 8/14、⑩ 1/7，⑧⑪⑫ 为 0 | regime 的 `DIM.FLOW` / `DIM.SENTIMENT` 因成员不齐被逼成「单锚代理分、恒不出档、恒低置信」（`build_dimension_value.py:874-895, 667-686`）。个股层会大面积出现同类降级，必须先定门禁，不能事后补。**此处的「域成员数」是设计稿口径，严禁用作 G1 分母**（§4.2） |
| A3 | **不可加性更严重**：regime §2.4 规则 7 已明令「六维分不可横向比较或求均值」 | ③ 估值 / ④ 质量 / ⑦ 交易 / ⑨ 资金是完全不同构念，**禁止套一个统一的「域内标签加权平均」模板**。每个结论必须单独声明聚合结构（条件合取 / 计数 / 描述量），与 `build_dimension_value.py` 按 `dimension_code` 分派独立生产者同款 |
| A4 | **验证代价**：regime 每个 L3 都是「原规格被实测推翻后重写」才落地（pattern v1.32 / divergence v1.33 / reversal v1.34 的 remark 全是「撤销原规格」） | 个股 × ~7,500 只 × 历史日的前瞻验证量与过拟合风险高得多；故 §10 明确**不以「前瞻收益显著分离」为上线门槛** |

---

## 3 消费端数据契约（决定结论必须长成什么样）

> 本章只写**按什么数据契约被读取**（决定落表形状），不讨论任何展示形态。

### 3.1 读取方式

唯一 SQL 层是 `stock-realtime/db.py`：

| 契约 | 行为 | 位置 |
| --- | --- | --- |
| 筛选查询 | `conditions: {tag_code: [value_code]}` → `EXISTS(... key_value = ANY(%s) AND eff_to='9999-12-31')`；标签间 AND、同标签内 OR；支持 `as_of` 回放（`eff_from<=as_of AND eff_to>=as_of`） | `db.py:8475-8568`、`:8528-8533` |
| 单票全量标签 | `SELECT ... FROM profile.tag_value WHERE stock_code=%s AND eff_to='9999-12-31'`；`domain` 在该段取出，真正按域分组在 `profile_stock_detail()` | `db.py:8749-8797`、`:8888-8902` |
| 元数据 / 值域 | `SELECT ... FROM profile.tag_registry WHERE status='active'`；`enum` 值域经 `JOIN profile.enum_value e ON e.enum_type = split_part(r.value_ref, ':', 2)` 翻译 | `db.py:8098-8123, 8126-8169, 8114-8122` |
| 数据版本登记 | **`DATA_SOURCES`** 登记 `profile_tag_value` / `profile_tag_registry`（`db.py:6246, 6326-6330`），其 `version_fn` 定义在 `:2976-2993`：`tag_registry` 取 `max(updated_at)`，**`tag_value` 取 `tag_run_log max(created_at) where status='ok'`**（不是读 `tag_value` 表本身）。`VERSION_SOURCES`（`:56-76`）只含普通表，无 `profile.*` | `db.py:56-76, 2976-2993, 6246, 6326-6330` |

> ⚠ **对结论层的硬含义**：结论生产者**必须**写 `tag_run_log`（`status='ok'` + `version`），否则 `get_node_version()` 不前进，下游 DAG 不重绘。见 §3.4 / §9.1 约束 6。

### 3.2 由读取契约反推的四条硬约束

| # | 约束 | 依据 |
| --- | --- | --- |
| C1 | **结论必须是离散 `enum` 标签，落 `profile.tag_value`** | 现有 EAV 的 `value_type` 只认 `enum/bool/tier/float`（小写，`profiling/registry.py:91-92`），无结论 / JSON 载体；自由文本或加权连续分要另起一套表与消费逻辑 |
| C2 | **`value_type='enum'` + `value_ref='enum:<type>'` + `profile.enum_value` 中文值域** | `registry.py:100-113` 强制 ACTIVE enum 必须声明值域；`db.py:8114-8122` 用 `value_ref` 做 code→label 翻译；`:8115,8121-8122` 用 `COALESCE(e.short_label, e.label)` 作筛选字典显示文本。`enum` 的 `key_value` 存**规范 code**（小写），不是中文 label |
| C3 | **标签必须 `status='active'` 且有非空值域才进入筛选字典** | `db.py:8137-8141, 8158-8172`；`get_screen_meta()` 对空值域直接 `continue`（`:8171-8172`），无占位、无告警。未实现的结论用 `planned_no_data + blocked_reason` 注册；消费端不读 `blocked_reason`，因此「不得静默缺项」只能在生产侧保证，不能当作消费端能力 |
| C4 | **结论必须自带可追溯依据** | 判定依据落 `tag_value.conclusion_detail`（§4.3）；读取契约见 §3.4 |

### 3.3 ⑫ 不是本期落点

⑫ 域在设计稿里定义为「模型输出、必须带置信度」（`doc/stock profile hd.md:207`），与本层「规则驱动的描述性结论」语义冲突。本层结论用**新 domain**（§4.1），不去占用 ⑫ 的域定义，避免语义混淆。

### 3.4 结论依据与 DAG 刷新的读取契约

| # | 契约项 | 要求 |
| --- | --- | --- |
| R1 | **依据可读** | 消费端单票标签查询需带上 `tv.conclusion_detail`（或新增 `get_conclusion_detail(stock_code, tag_code, as_of)`）。|
| R2 | **版本列前进** | 每个结论标签每轮写一条 `tag_run_log`（`status='ok'`、`version` 同 `RULES`），作为 `DATA_SOURCES["profile_tag_value"].version_fn` 的唯一刷新信号。漏写 = 数据已更新但前端不重绘 |
| R3 | **as_of 一致** | 消费端 `as_of` 回放读到的是「当前规则回算的历史值」（§4.4.4），结论生产者全量重建历史时须保证每个 `as_of` 都有可回放行（§4.6） |
| R4 | **不进域内分位** | `db.py:8805-8826 _tag_pct()` 对 `num_unit='percentile_0_100'` 或数字档位 `tier` 才算分位，`enum` 返回 `None`。结论标签必须 `num_unit=NULL` **且 `value_range=NULL`**（`:8814` 未检查 `value_type=='tier'`，误带数字型 `value_range` 会被误算进域均值） |
| R5 | **short_label 必填** | 6 组共 35 个 enum code 的 `short_label` 全部非空（附录 D）。否则 `COALESCE` 回退长句，筛选下拉显示失控 |

---

## 4 数据契约

### 4.1 对象清单与编码规范（**A 方案，已定**）

| 层 | 标签编码 | domain | 数量（首期） | 粒度 | 落表 |
| --- | --- | --- | --- | --- | --- |
| L1 原子 | 现有 3 字母域缩写：`idt_/scl_/val_/qal_/grw_/shr_/trd_/att_/tst_` | 现有 9 个中文域名 | 59 active | `(stock_code, tag_code, eff_from)` | `profile.tag_value` |
| **L2 维度指标** | **`dim_*`** | **沿用各自业务域**（`估值水平` / `盈利质量` / `成长特征` / `趋势状态`），不新增域 | 4 | 同上 | 同上 |
| **L3 业务指标** | **`biz_*`** | **`综合研判`**（新建，唯一新增域） | 2 | 同上 | 同上 |

正式 `tag_code`（A 方案，全文统一使用）：

| 层 | `tag_code` | `domain` | 规格 |
| --- | --- | --- | --- |
| L2 | `dim_valuation_conclusion` | `估值水平` | §5.1 贵不贵 |
| L2 | `dim_quality_conclusion` | `盈利质量` | §5.2 好不好 |
| L2 | `dim_growth_conclusion` | `成长特征` | §5.3 快不快 |
| L2 | `dim_position_state` | `趋势状态` | §5.4 当前位置与状态 |
| L3 | `biz_value_quality_type` | `综合研判` | §6.1 好公司 × 好价格 |
| L3 | `biz_growth_quality_type` | `综合研判` | §6.2 增长质量 |

> **domain 归属已定（方案 A）**：L2 结论与其原子依据同域，靠 `tag_code` 前缀 + `source_type='derived'` 区分层级；L3 跨域，归唯一新建域 `综合研判`。**层级不进 domain**——它是业务分组键，不是技术层级标记（§4.1.1）。`综合研判` 需登记进下游两份 domain 白名单（§4.1.1），否则排末位。

**命名依据**：`dim` = Dimension、`biz` = Business，与 `doc/market_state_data_architecture.md:41-47` / `doc/market_profile.md §5.0` 的分层术语一致；后缀 `_conclusion` / `_state` / `_type` 明确「这是结论，不是原子指标」。regime 侧是 `DIM.BREADTH`（大写点分、落在 `regime.dimension_value.dimension_code`），本层是小写下划线（落在 `profile.tag_value.tag_code`），不同表不同列，无冲突。

> **L1 编码不改**：59 个已上线标签的 `tag_code` 已被消费端按字面引用，重命名会破坏既有数据契约。分级通过**新增标签的编码前缀 + `source_type` + `parent_tag`** 体现，不回改 L1。

**元数据约定**（`@tag` 装饰器，`profiling/registry.py:62-131`；下表只列 `TagMeta` 真实存在的参数）：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `value_type` | `enum`（**小写**） | 代码常量 `registry.ENUM="enum"`；消费端 `db.py:8169-8170` 按小写判断，写大写会被静默跳过（`registry.py:91-92` 会先报错） |
| `value_ref` | `enum:<enum_type>` | 由 `enum_values` 内置值域自动生成（`registry.py:101-105`）；中文名进 `profile.enum_value` |
| `value_range` | **`NULL`（硬约束）** | 结论层不声明 tier 值域；见 §3.4 R4 |
| `num_unit` | `NULL` | **结论不带连续分** |
| `source_type` | `derived`（新增受控值） | 现有注释为 `rule/stat/model/external/manual`（`profiling/schema.py:40`）；`source_type` 是 TEXT 无 CHECK，新增值需在实现时同步注释。**计算接线按此字段识别结论标签**（§9），不依赖编码前缀 |
| `version` | **规则口径版本**（`v1` 起，升级时 bump） | 由 `sync_registry()` 同步到 `tag_registry.version`，由结论生产者写进 `tag_value.version`（§4.4）。**不放哈希串、不放规则内容** |
| `parent_tag` | 上游主标签的 `tag_code` | 用现有列表达层级，**无需加列** |
| `is_exclusive` | **`False`（建议）** | `profiling/tags/trend.py:186-191` 刻意把该标记留给 ⑩ 域的 6 阶段状态机 `tst_stage`；结论层改用 `source_type=derived` 识别 |
| `confidence_req` | `False` | 结论是确定性规则，非模型类 |
| `pit_capable` | **按「判定用输入」的最弱环声明** | ③⑦⑩ 判定输入仅量价 / PE 分位 → `True`；④⑤ 及两个 L3 → `False`（港股披露日无源，§8.5）。不得按「域」整体拍值 |
| `status` | `active` | 首期 6 个全部实现；若某项判定条件不可得，用 `planned_no_data + blocked_reason` 注册 |

**值行约定**（`profile.tag_value` 的列，不是 `@tag` 参数）：`key_value` = enum 规范 code；`num_value = NULL`；`num_unit = NULL`；`confidence = 1.0` 恒值（`sql/schema.sql:1431`：规则/统计类恒 1.0，模型类为预测概率）——可得性一律走出档门禁 + `conclusion_detail.coverage`，不塞进 `confidence`。

#### 4.1.1 domain 的作用与取值

`tag_registry.domain` 是 `TEXT NOT NULL`（`sql/schema.sql:1360` 注释「所属域，如『证券属性』，不带设计文档里的域序号」）。它在数据契约里承担四种**键**的作用，不是一句自由文案：

| 作用 | 位置 | 说明 |
| --- | --- | --- |
| 分组键 | `stock-realtime/db.py:8137-8183` | `get_screen_meta()` 以 `ORDER BY domain, tag_code` 把标签组织成「域 → 标签 → 取值」三级字典 |
| 分组键 | `db.py:8888-8902` | `profile_stock_detail()` 按 `meta["domain"]` 把单票标签分组、算域内聚合 |
| 排序键 | `db.py:8047-8050, 8180-8182`（`_SCREEN_DOMAIN_ORDER` 9 域）、`db.py:8641-8645`（`_PROFILE_DOMAIN_ORDER` 12 域） | 两份**硬编码中文域名白名单**决定顺序；不在表内的域排末位（最终域顺序由白名单而非 SQL 的 `ORDER BY` 决定） |
| 分类键 | `db.py:8647`（`_PROFILE_MODEL_DOMAINS`）、`_PROFILE_MINIBAR_DOMAINS`、`_PROFILE_DOMAIN_COLORS`、`_PROFILE_RADAR_DIMS` | 按中文 domain 名硬编码匹配 |
| 过滤键 | `profiling/registry.py:165-167`、`profiling/run.py:157-159` | `by_domain()` 用 `startswith` 前缀匹配；`compute --domain 规模属性` 按域重算 |

> ⚠ 因此 **domain 的取值是被下游按字面字符串匹配的语义键**，改名/换语言会让上述白名单全部失配。这是数据契约事实，不是展示设计。

**是否需要中文名**：

| | 判断 |
| --- | --- |
| 现状 | 9 个已落地域全部是**中文业务域名**：证券属性 / 规模属性 / 估值水平 / 盈利质量 / 成长特征 / 股东回报 / 交易特征 / 资金关注度 / 趋势状态 |
| 本期结论 | **沿用中文**。改成英文编码会与三处中文白名单失配，且需要消费端联动改造，收益为零 |
| 架构判断 | 中文**不是语义必需**。更规范的做法是「稳定编码 + 中文显示名分离」——项目里 `tag_code`/`tag_name`、`enum_value.code`/`label` 已经是这个范式。将来若需重命名或国际化，应新增 `domain_code` + 显示名映射，**不要直接改 domain 字符串** |

**domain 归属：方案 A（已定）**

「域结论」「综合结论」是**技术层级名**，而现有 9 域是**业务域名**，语义不同；层级信息已由 `tag_code` 前缀 + `source_type='derived'` 表达，再塞进 `domain` 会让一个字段承担两种职责。

| 层 | 归属 | 说明 |
| --- | --- | --- |
| L2 四个 | `估值水平` / `盈利质量` / `成长特征` / `趋势状态` | 沿用各自业务域，结论与原子依据同域 |
| L3 两个 | **`综合研判`** | 唯一新建的中文业务域名；跨域结论没有单一业务归属 |

**白名单登记要求**：

| 白名单 | 动作 | 理由 |
| --- | --- | --- |
| `_SCREEN_DOMAIN_ORDER`（9 域）、`_PROFILE_DOMAIN_ORDER`（12 域） | **追加到末尾** | 稳定排序不影响既有 9 域相对顺序，改动最小；语义上综合结论在单域之后。白名单外域有 `db.py:9036-9055` 兜底追加，不登记也不丢数据，只是排末位/取默认分类 |
| `_PROFILE_MODEL_DOMAINS`（`{"趋势状态","事件风险","预测评分"}`） | **不进** | 本层是规则驱动的描述性判断，不是模型输出 |
| `_PROFILE_RADAR_DIMS`（8 维）、`_PROFILE_MINIBAR_DOMAINS`（8 域） | **不进** | §10.2「不做雷达汇总」、§0 非目标 3「不做可排序总分」；结论 enum 进雷达会被误当成可比较维度 |
| `_PROFILE_DOMAIN_COLORS` | **登记中性色**（走项目颜色 token，不硬编码色值） | 否则取默认 `neutral` |

此四条属**数据契约登记项**（白名单按字面匹配），不涉及展示设计。登记后验收：`get_screen_meta()` 能把 6 个新标签组织进「综合研判」域且值域非空（C3 契约）。

### 4.2 出档门禁（统一，逐结论可收紧）

> **核心纪律：不确定 → 不写行，不设 `insufficient` 枚举码。** 把「不知道」变成一个可检索的枚举值，等于让下游筛出一个确定的错误概念（regime `A8`「整行不发布」同款，`build_business_value.py:368-369`）。

**G1 分母**：`coverage = 当日有效 inputs 成员数 / len(conclusion_dict.inputs)`，其中 `inputs` **只含该结论的「判定用」输入**；`evidence_only` 成员、`derived_fields` 派生字段**均不计入分母与门禁**。`LOSS` / `NEGATIVE` 等业务码在 cast 失败时视为**无效成员**。**严禁用 §2.3 A2 的设计稿域成员数（⑤ 4/8、⑩ 1/7 等）当分母**——那样 `dim_growth_conclusion`（上限 0.5）与 `dim_position_state`（上限 0.14）会被门禁永久锁死。

| 门禁 | 判据 | 不通过时的动作 |
| --- | --- | --- |
| G1 成员可得率 | `coverage >= 0.6`（初值；按结论分别用覆盖率与出档率实测标定，结果写进 `conclusion_dict.params`） | **日常增量**：关闭当前有效行 `eff_to`、不插新行；**历史全量重建**：不落行（删除语义）。两种场景统一见 §4.6。`tag_run_log` 记 `skipped` |
| G2 关键输入存在 | 判定的主锚（如估值双分位、位置档）缺一即不发布 | 同 G1，不用其它成员回填凑数 |
| G3 数据新鲜度 | 当日行情未到货 → `SkipTag`（`profiling/quantile.py:59-67`） | 整批不发布，不用前一日快照冒充 |
| G4 负 / 无效值（**豁免 G1/G2**） | 走**业务枚举码**而非丢弃：L1 大写 `LOSS`（PE≤0，`valuation.py:87-90`）/ `NEGATIVE`（PB≤0，`valuation.py:104-106`）**映射**为结论层小写 `loss` / `negative`，原始码保留在 `evidence`。**`loss_flag`/`negative_flag` 为 true 时直接发布业务码，不受 G1 覆盖率与 G2 主锚缺失限制**——亏损股通常同时缺两个 PE 分位（`coverage=2/4=0.5`），不豁免会被 G1 挡掉 | 写业务码，原因写进 `conclusion_detail`；`loss`/`negative` 视为「无有效估值锚」（§6.1） |

> **大小写纪律**：结论层 enum code 统一**小写**（与现有 `surge`/`normal` 等一致），L1 的 `LOSS`/`NEGATIVE` 属不同 `enum_type`、不改；二者是**映射关系**不是「沿用同一码」，不违反 §4.5「一个概念 = 一个 code」（该约束作用于同一 `enum_type` 内）。

`confidence` 恒 1.0；**可得性只写进 `conclusion_detail.coverage`**。§5 / §6 各表「依据键」即 `tag_value.conclusion_detail` 的键集合。

### 4.3 字段设计（**已定：职责不同 → 名称不同**）

> **规则契约与判定依据是两类不同性质的数据，用两个不同名字的字段各归其位**：
> - **规则契约**（这个标签「怎么判」）→ 对同一标签**按版本固定** → 标签级 → `tag_registry.conclusion_dict`（**只读发布物**，真源是代码里的 `RULES`，§4.3.1）。
> - **判定依据**（这票这天「为什么判成这个值」）→ 逐票逐生效区间不同 → 值级 → `tag_value.conclusion_detail`。
>
> 不使用 `params_hash` 之类的内容指纹；口径标识由 `version` 承担。

| 字段 | 表 | 粒度 | 职责（唯一） | 何时变化 |
| --- | --- | --- | --- | --- |
| `conclusion_dict JSONB` | `tag_registry` | 一标签一行 | **当前生效规则契约的只读发布物**（只存当前版，不存历史） | 仅规则升级（低频，触发全量重算） |
| `conclusion_detail JSONB` | `tag_value` | 一票一标签一**取值生效区间**一行 | **该行判定依据**：`rule_id`/`evidence`/`coverage`/`gates` 为该行生效时的判定快照（生效日见 `eff_from`），`as_of` 等日变量见 §4.6 | 判定快照随新行一次性写入、不刷新；日变量在当前有效行原地刷新（§4.6） |
| `version` | `tag_registry` | 一标签一行 | 标签口径版本（**结论标签 = 规则版本**） | 规则升级 |
| `version` | `tag_value` | 每行 | 该值行所依据的口径版本 | 规则升级后全量重建 |

> **两个字段对 59 个 L1 标签均为 `NULL`**；`version` 两列保持现有语义（`profiling/engine.py:357-367` 写 `tag_value.version`、`:413` 写 `tag_run_log.version`；`profiling/registry.py:276-284` 全列 upsert 写 `tag_registry.version`）。

#### 4.3.1 `tag_registry.conclusion_dict`（结构化规则契约）

**唯一可执行真源是结论生产者代码里的 `RULES: dict[tag_code, RuleSpec]`**；判定只读内存 `RULES`，**不读 JSONB**。新增 `sync_conclusion_dict(conn)` 接在 `sync_registry()` 之后，把 `RULES` 序列化写入该列，并校验 `tag_registry.version == RULES[code].version`，不一致直接报错。**不要给 `TagMeta` 加 `conclusion_dict` 字段**——`sync_registry()` 用 `asdict(TagMeta)` 全列 upsert，加字段会把规则耦合进注册表写入链。

冻结后的 JSONB schema（以 `dim_valuation_conclusion` 为例）：

```json
{
  "inputs": [
    {"field": "pe_hist_pct", "tag_code": "val_pe_hist_pct", "source_col": "num_value",
     "dtype": "float", "role": "anchor"},
    {"field": "pe_ind_pct", "tag_code": "val_pe_ind_pct", "source_col": "num_value",
     "dtype": "float", "role": "anchor"},
    {"field": "loss_flag", "tag_code": "val_pe_ttm_tier", "source_col": "key_value",
     "dtype": "bool", "match": "LOSS", "role": "flag"},
    {"field": "negative_flag", "tag_code": "val_pb_tier", "source_col": "key_value",
     "dtype": "bool", "match": "NEGATIVE", "role": "flag"}
  ],
  "derived_fields": [
    {"field": "pe_gap", "expr": "abs(pe_hist_pct - pe_ind_pct)", "dtype": "float"},
    {"field": "pe_gap_dir", "expr": "hist_lower if pe_hist_pct < pe_ind_pct else hist_higher",
     "dtype": "enum", "codes": ["hist_lower", "hist_higher"]}
  ],
  "params": {"coverage_min": 0.6, "cheap_pctl": 20, "expensive_pctl": 80,
             "divergence_gap_pctl": 30},
  "rules": [
    {"rule_id": "VAL-00", "priority": 0,
     "conditions": {"all": [{"field": "loss_flag", "op": "==", "value": true}]},
     "result": "loss"},
    {"rule_id": "VAL-02", "priority": 10,
     "conditions": {"all": [{"field": "pe_gap", "op": ">=", "value": 30},
                            {"field": "pe_gap_dir", "op": "==", "value": "hist_lower"}]},
     "result": "hist_cheap_ind_expensive"},
    {"rule_id": "VAL-04", "priority": 20,
     "conditions": {"all": [{"field": "pe_hist_pct", "op": "<=", "value": 20},
                            {"field": "pe_ind_pct", "op": "<=", "value": 20}]},
     "result": "rel_cheap"}
  ],
  "output_schema": {
    "key_value_semantics": "相对估值位置的枚举类型；不是绝对贵贱、不是预测",
    "value_codes": {"rel_cheap": "相对偏便宜", "loss": "亏损（无 PE 估值锚）"}
  }
}
```

| 冻结项 | 规定 |
| --- | --- |
| `inputs[]` | 每项必须有 `field`；声明 `source_col`（`key_value` / `num_value`）+ `dtype`（`str` / `int` / `float` / `enum` / `bool`）；来自 L1 标签给 `tag_code`，来自原始表现算量给 `source` + `codes` 全码表，`source` 允许 `v_daily_quote` / `financial_indicator` / `derived`（上游结论码归约）；`role` 取 `anchor` / `flag` / `aux`。**`dtype=bool` 时可用 `match` 把 L1 某个 `key_value` 码映射为 `true`、其余为 `false`**（如 `loss_flag` 的 `match: "LOSS"`）。**L1 档位 `key_value` 是字符串 `"1".."5"`（`quantile.py:455`），必须按 `dtype=int` cast 后比较**；`LOSS` / `NEGATIVE`、以及 `free_cash_flow IS NULL` 派生的 `fcf_sign` 缺值，均视为**无效成员**。6 个结论的 `inputs[]` 必须全部声明（§5 / §6 各表） |
| `derived_fields[]` | 声明 `field` + `expr`（**仅文档说明，引擎不解析**）+ `dtype` + `codes`（枚举时）；由生产者在喂条件引擎前算好并写进 `evidence`。**派生字段不进 G1 分母**；禁止在 `rules[].conditions` 内写表达式或字段引用 |
| `rules[].conditions` | **结构化条件对象，禁止自由文本**。组合子 `all` / `any` / `not`；叶子 `{field, op, value}`，`value` **只能是字面量**；`op` 白名单 `<=` `<` `>=` `>` `==` `in`（**删 `between`**，用 `>=` + `<=` 两个叶子表达）；`field` 必须在 `inputs[]` 或 `derived_fields[]` 内。**§5/§6 规则表为便于阅读写参数名，发布到 `RULES` 时必须展开成 `params` 中对应的字面值**；`sync_conclusion_dict()` 需校验展开值与 `params` 一致，否则报错 |
| 命中方式 | `first_match`：按 `priority` 升序取首个全匹配规则；**每条规则表必须有 `priority=99` 兜底**，其 `conditions` 统一写 `{"all": []}`（空合取恒真）。§5/§6 表格里的「其余」即此兜底 |
| 值域声明 | `source` 型现算成员必须在 `inputs[].codes` 给全码表（如 `ma_alignment` 的 `bullish/neutral/bearish`）；`enum` 型 L1 成员必须给 `codes` 且与真实 `enum_values` 一致 |
| 可复算性 | 只有「字面量条件 + 结构化输入」才能由 `conclusion_detail.rule_id + evidence` 机器复算出同一 enum（§10.1「可解释」验收）。自由文本 / 表达式不满足 |
| 版本号 | **不放 `version`、不放 `history[]`**；当前期望版本的唯一真相源是 `tag_registry.version`（由 `RULES[code].version` 校验） |
| 载体 | 一个 JSONB 列，**不新建独立字典表**；规则历史由 git 提交 + `tag_run_log.version` 时间线追溯 |

> `params` 中的切点数值已标定**冻结**（`coverage_min=0.6` / `cheap_pctl=20` / `expensive_pctl=80` / `divergence_gap_pctl=30`）；§5/§6 各表 `params` 同属冻结值；schema 形状、算子集合、`inputs[]` 声明格式同为冻结项。

**§5/§6 规则表 → `RULES` 的投影约定**：六张规格表是 `RULES` 的人类可读投影，发布时按下列规则**机械展开**，`sync_conclusion_dict()` 逐条校验：

| 规格表写法 | `RULES` 中的形态 |
| --- | --- |
| `inputs（判定用）` 栏的文字声明 | `inputs[]` 字面对象：`field` / `tag_code` 或 `source` / `source_col` / `dtype` / `codes`（枚举时） |
| 条件里的 `AND` / `OR` | `{"all":[...]}` / `{"any":[...]}` |
| 大写 `IN` | `op: "in"`（**小写**） |
| 参数名（如 `cheap_pctl`） | `params` 中对应的**字面值**；校验展开值与 `params` 一致 |
| 「其余」兜底 | `priority=99`、`conditions: {"all": []}` |
| `derived_fields` 栏 | `derived_fields[]`（`expr` 仅文档说明） |
| 归约量（如 `valuation_class`） | `source: "derived"` 型 `inputs[]` 成员 + `codes` + 归约映射 |
| `bool` 标志（如 `loss_flag`） | `dtype: "bool"` + `match: "<L1 码>"`，不重复列 `codes` |

> 6 份 `inputs[]` 必须按此展开并逐字段核对 L1 真实值域；规则表字段与字面值必须落在 L1 真实值域内。

#### 4.3.2 `tag_value.conclusion_detail`（该行判定依据）

```json
{
  "rule_id": "VAL-04",
  "coverage": 0.83,
  "missing_inputs": ["val_fcf_yield_tier"],
  "evidence": {"pe_hist_pct": 18.4, "pe_ind_pct": 21.2, "tier_pe": "1", "tier_pb": "2",
               "loss_flag": false, "negative_flag": false, "pe_gap": 2.8,
               "pe_gap_dir": "hist_lower"},
  "gates": {"G1": true, "G2": true, "G3": true, "G4": false},
  "as_of": "2026-11-12",
  "state_raw": "rel_cheap",
  "since_date": "2026-11-03",
  "dwell_days": 7,
  "status": "confirmed",
  "candidate": {"to": null, "progress": 0}
}
```

- `evidence` 自包含：读到这一行即可独立复算「为什么判成这个值」，无需回查上游标签状态；它是**该行生效时**的判定快照，状态持续期间不刷新。派生字段（`pe_gap` 等）也写进 `evidence`，保证复算不依赖重放。
- **不放版本号**（`tag_value.version` 已是）、**不放规则全文**（`conclusion_dict` 已是）、**不放原始判定快照**（§4.4.4）。
- 末 6 个键（`as_of` / `state_raw` / `since_date` / `dwell_days` / `status` / `candidate`）为**日变量**：`as_of` = 当前计算日、`since_date` = 该行 `eff_from`；仅 `dim_position_state` 等需要游程标注的结论使用；只在**当前有效行**原地刷新，不改 `key_value` / `eff_from` / `eff_to` / `version`（§4.6）。
- 该列随 `tag_value` 的 `eff_from/eff_to` 一起生效。

### 4.4 口径版本、重算与覆盖（**已定**）

#### 4.4.1 为什么用版本号，不用 `params_hash`

`params_hash` 是 regime 的模式：字典由 `tools/calibrate_*.py --write` **机器生成**，参数多、切点冻结，需要内容指纹自动检测「内容变了但版本没变」。个股层只有 6 个结论、规则由 `RULES` 代码显式定义、变更低频，且**规则升级本来就是触发全量重算的动作**——bump 版本号是流程起点，不是额外负担。

内容指纹唯一多出的能力是自动拦截「改了内容忘了 bump」，但它也检测不到「判定逻辑写在代码里、字典没变」的情况；该风险由 `sync_conclusion_dict()` 的 version 一致性校验 + 单测承担。故**去掉 `params_hash`**，用可读版本号。

#### 4.4.2 触发链路

```
规则变更（改 RULES 的 inputs / params / rules / output_schema）
  → bump @tag(version="v2") 与 RULES[code].version
  → sync_registry() 把 tag_registry.version 更新为 v2（registry.py:276-284 全列 upsert）
  → sync_conclusion_dict() 写 conclusion_dict，并校验 version 一致
  → 全量重建历史：删光 6 个 tag_code 的全部行 → 按 as_of 逐日重算 → 事件行压缩
  → 所有新行写 tag_value.version = RULES[code].version = "v2"
  → tag_run_log 写 status='ok' + version（下游 DAG 刷新信号，§3.4 R2）
```

**检测漏重算残留**（版本号即口径标识，无需额外字段）：

```sql
SELECT tv.stock_code, tv.tag_code, tv.version AS row_ver, r.version AS expected
FROM profile.tag_value tv
JOIN profile.tag_registry r ON r.tag_code = tv.tag_code
WHERE r.source_type = 'derived'
  AND tv.tag_code IN ('dim_valuation_conclusion', 'dim_quality_conclusion',
                      'dim_growth_conclusion', 'dim_position_state',
                      'biz_value_quality_type', 'biz_growth_quality_type')
  AND tv.version IS DISTINCT FROM r.version;
```

> 查询带 `tag_code` 实体过滤（走 `idx_tag_value_lookup` 前导列，`sql/schema.sql:1441-1442`），符合 AGENTS.md R7。**为什么能返回 0 行**：全量重建会删光旧行重写，日常门禁关闭的行 `version` 仍为当前版（§4.6），不存在旧版残留。

**规则变更历史**不落库：`conclusion_dict` 只存当前版，历史规则由 git 提交记录 + `tag_run_log.version` 时间线追溯。

#### 4.4.3 全量重建的四条实现约束

| # | 约束 | 依据 |
| --- | --- | --- |
| 1 | **按 `as_of` 取当时的 L1 版本**：`eff_from<=as_of AND eff_to>=as_of`，不得取 `eff_to='9999-12-31'` 的当前有效值，否则把后来修正过的数据用到历史日期 → 前视偏差 | `doc/stock profile hd.md:489` 高危项；`stock-realtime/db.py:8528-8533` 的 `as_of` 回放同款。注意 L1 当日是 snapshot 原地 `UPDATE`，历史日是版本链，回放严格性受此限制（§8.5） |
| 2 | **仅规则升级的全量重建走 `DELETE + INSERT`**：`UNIQUE(stock_code, tag_code, key_value, eff_from)`（`sql/schema.sql:1425`）把 `key_value` 纳入唯一键，新口径算出不同 enum 时 `UPDATE key_value` 会撞约束 | 日常增量不走此路径（§4.6）；`profiling/engine.py:359-362` 的 upsert 可作写入参考 |
| 3 | **门禁不通过**：历史重建 = 删除（不落行）；日常增量 = 关闭当前有效行 `eff_to`（不插新行，该行 `version` 仍为当前版） | `build_dimension_value.py:506-509` 的 `skip_null_updates=False` 教训；与 §4.2、§4.4.2、§10.1 统一由 §4.6 裁决 |
| 4 | **资源红线**：全量重建跨 `eff_from` 年份分区，须分批（按 `tag_code` / 分区）、按股票分批加载、单事务 ≤5 万行、实测峰值内存、登记监控埋点。**不注册 systemd 服务**：全量重建由手动脚本 `compute_conclusions_rebuild.py --yes` 执行（默认预演、全程 INFO 日志、末尾打印耗时与峰值 RSS）；R4 的「独立 unit」形式不适用，R1/R3/R6/R7 由 producer 分批 + 脚本日志实测保留 | `doc/compute_task_resource_standard.md` §2：R1 分批 / R3 峰值内存 / R6 单事务≤5 万行 / R7 查询带下界 / §7 监控；项目 09-22 写爆发打满磁盘队列事故 |

> 结论标签不走 `engine.compute_tag()`（§9 按 `source_type='derived'` 跳过），所以「规则升级时直接覆盖」与 L1「值变化关闭旧行 + 插新行」是两条独立路径。`sql/schema.sql` 注释只需登记**「规则升级时结论标签全量重建」**这一例外，不必声明「结论标签普遍直接覆盖」——日常增量仍沿用版本链语义。

#### 4.4.4 不保留「复盘当时结论」

> **已定（业务判断）**：「复盘当时系统给了什么结论」这个场景不存在、无意义，**不需要考虑**。
>
> 因此：`as_of` 回放得到的是「用当前规则回算的历史值」，不是当时真实发布值；不设原始判定快照、不设规则历史表、不设双版本并存。对回测而言口径统一反而是优点（避免规则变更造成假收益）。

### 4.5 唯一真相源

| 信息 | 唯一家 |
| --- | --- |
| 标签元数据 / 口径 / 值域 | `profiling/registry.py` 的 `@tag` 装饰器 → `sync_registry()` 写 `tag_registry`（代码即字典） |
| **L1 标签真实值域** | 各 `profiling/tags/*.py` 的 `value_range` / `enum_values`（规则表 `inputs[]` 必须与之对齐，§4.3.1） |
| enum 中文名 | `profile.enum_value`（同一 `enum_type` 内一个概念 = 一个 code）；结论层 `label` / `short_label` 见**附录 D** |
| **可执行判定规则** | **结论生产者代码里的 `RULES`（唯一可执行真源）** |
| **规则契约的只读发布物** | `tag_registry.conclusion_dict`（`sync_conclusion_dict()` 写入并校验 version） |
| 结论值 | `profile.tag_value.key_value`（EAV + `eff_from/eff_to` 版本化，事件行） |
| **该行判定依据** | `tag_value.conclusion_detail` |
| **结论行日变量** | 当前有效行的 `conclusion_detail`（原地刷新，不产生新行） |
| **当前口径版本** | `tag_registry.version`（由 `@tag.version` 同步；生产者从内存 `RULES` 取，不查库） |
| **该行口径版本** | `tag_value.version`（由结论生产者写入） |

### 4.6 结论行粒度与版本策略（**唯一真相源**）

> **决策：事件行**。结论值只在**取值发生变化**时写新行；`eff_from/eff_to` 沿用 `tag_value` 的「取值生效区间」语义（`sql/schema.sql:1428, 1432`）。**不采用逐日行**（~7,500 只 × 6 结论 × 250 日 ≈ 1,100 万行/年，且与 L1 版本化语义分叉）。

| 理由 | 说明 |
| --- | --- |
| 消费契约零改造 | 当前态筛选 `eff_to='9999-12-31'`、`as_of` 回放 `eff_from<=as_of AND eff_to>=as_of` 都天然满足（`db.py:8535-8539, 8528-8533`） |
| 行数少一个数量级 | 约 45–135 万行/年（按每年 10–30 次状态切换估），单事务 5 万行红线压力小 |
| 与 L1 语义一致 | 无需在全表登记「结论标签直接覆盖」例外，只需登记「规则升级时全量重建」（§4.4.3） |
| 游程可推 | `since_date = eff_from`、`dwell_days = as_of − eff_from`，可直接从事件行算出 |

**日变量的归属（解决「游程标注是日变量、事件行不更新」的矛盾）**：

| 数据 | 落点 | 刷新方式 |
| --- | --- | --- |
| `state`（结论 enum） | `key_value` | 值切换才变：关闭旧行 + 插新行 |
| `rule_id` / `coverage` / `missing_inputs` / `evidence` / `gates` | 该行的 `conclusion_detail` | **判定快照**：随新行一次性写入，状态持续期间不刷新；生效日见 `eff_from` |
| `as_of` / `state_raw` / `since_date` / `dwell_days` / `status` / `candidate{to,progress}` | **当前有效行**的 `conclusion_detail` | **每日原地 `UPDATE` 非键列**（不动 `key_value`/`eff_from`/`eff_to`/`version`），不违反 `UNIQUE(stock_code,tag_code,key_value,eff_from)`；`as_of` = 当前计算日、`since_date` = 该行 `eff_from` |

**四种场景的统一动作表**（§4.2 / §4.4.2 / §4.4.3 / §9.1 一律以此为准）：

| 场景 | 动作 | `version` |
| --- | --- | --- |
| **① 规则升级（全量重建）** | bump `RULES.version` → `DELETE` 6 个 `tag_code` 的全部历史行 → 按 `as_of` 逐日重算 → 用「值变化才落行」压缩成事件行 → `INSERT`。这是唯一用 `DELETE+INSERT` 的场景 | 全部行写新版本 |
| **② 日常增量·值变** | 关闭旧行 `eff_to` + 插新行（完整 `conclusion_detail`） | 当前版本 |
| **③ 日常增量·值不变** | 只 `UPDATE` 当前有效行的日变量；门禁不通过则关闭当前有效行、不插新行 | 当前版本（关闭行不改 `version`） |
| **④ 历史重建·门禁不通过** | **不落行**（删除语义，对应 §4.2「不写行」） | — |

**由此得到的一致性结论**：

1. 残留查询（§4.4.2）全表扫描后返回 0 行：重建删光旧行、关闭行 `version` 仍为当前版，无旧版残留 → §10.1「版本一致性」验收可达。
2. `as_of` 回放得到「当前规则回算的历史值」→ 与 §4.4.4 一致。
3. **日翻转率离线验收**：按 `as_of` 从 D1 到 Dn 回放 `SELECT key_value WHERE eff_from<=as_of AND eff_to>=as_of` 生成逐日序列，比较相邻日。库里不存逐日行也能算（`§10.1`）。
4. `candidate` 由「当日裸判 `state_raw` 与当前 `state` 不一致」产生，属当日信息，只在当前有效行刷新。
5. **幂等**：同日重复跑得到同一结果；值不变时只刷新日变量，不产生新行，不产生重复 `eff_from`。

---

## 5 L2 维度指标规格（4 个）

> **统一骨架**：① 定义 / ② `inputs（判定用）` 与 `evidence_only` 两栏 / ③ 判定规则表（`rule_id` / 条件 / 结果，与 `conclusion_dict.rules` 逐行对应）/ ④ enum 值域 / ⑤ 门禁与频率 / ⑥ 依据键 / ⑦ 措辞约束。
> **输入一律为 L1 原子标签的 `key_value` / `num_value` 原值或 `v_daily_quote` 原始量价，不使用「域内平均分」**（§2.2 铁律①）。
> **`inputs（判定用）` 即 `conclusion_dict.inputs`，是 G1 分母的唯一依据**（§4.2）；`evidence_only` 与 `derived_fields` 不进门禁、不进判定、不影响 `pit_capable`。
> **每个 `field` 必须声明来源 `tag_code`（或 `source`）、取值列 `source_col`、类型 `dtype`**（§4.3.1）。L1 档位 `key_value` 是字符串 `"1".."5"`，按 `dtype=int` cast 后比较；`LOSS` / `NEGATIVE` 视为无效成员。
> 章节标题即正式 `tag_code`；各表「依据键」为 `tag_value.conclusion_detail` 的键集合。
> 结论层值域用 `enum_values` 内置声明，`label` 为中文短句、**`short_label` 必填**（附录 D 为唯一真相源）；code 统一小写。表中 `params` 切点为冻结值。
> **规则表里的参数名不是条件值**：各表为便于阅读写 `cheap_pctl` 这类参数名，但 `RULES` 的 `conditions[].value` 只接受字面量，发布时必须展开（§4.3.1）；标定后 `params` 与展开值同步更新，由 `sync_conclusion_dict()` 校验。

### 5.1 `dim_valuation_conclusion` 贵不贵

| 项 | 内容 |
| --- | --- |
| 回答 | 相对自身历史与同行，价格处在什么位置；双分位是否一致 |
| `inputs（判定用）` | `pe_hist_pct` ← `val_pe_hist_pct.num_value`，`float`，0–100 分位（`num_unit='percentile_0_100'`）；`pe_ind_pct` ← `val_pe_ind_pct.num_value`，`float`，0–100；`loss_flag` ← `val_pe_ttm_tier.key_value == 'LOSS'`，`bool`；`negative_flag` ← `val_pb_tier.key_value == 'NEGATIVE'`，`bool`。**G1 分母 = 4**，全部 `pit_capable=True`（`valuation.py:91,109,266,289`） |
| `derived_fields` | `pe_gap = abs(pe_hist_pct − pe_ind_pct)`（`float`）；`pe_gap_dir ∈ {hist_lower, hist_higher}`（`enum`）。二者写进 `evidence`，**不进 G1 分母** |
| `evidence_only` | `val_ps_ttm_tier`、`val_pcf_ttm_tier`、`val_peg_tier`、`val_fcf_yield_tier`（四个 `pit_capable=False`）、`val_pb_ind_pct` 的 `key_value` 档位；以及 `val_pe_ttm_tier` / `val_pb_tier` 的**档位与原值**——这两个标签的 `LOSS`/`NEGATIVE` 业务码已作为 `loss_flag`/`negative_flag` 进入判定 `inputs[]`，此处仅记录其数值，**不重复参与判定**。只写 `evidence`，不进 G1 分母、不改结论 |
| 判定规则表 | 见下表；命中方式 `first_match`（§4.3.1） |
| enum 值域 | 7 码，code / label / short_label 见**附录 D.1** |
| 门禁 / 频率 | G1 + G2（**双分位必须同时可得**，缺一即不发布——与 `tools/calibrate_conclusion_cutpoints.py` 及生产者的 `role=anchor` 校验一致；否则 VAL-06 兜底会把数据缺失误判成「相对中性」）；**G4 业务码豁免 G1/G2**（§4.2）；`daily`；A+H；`pit_capable=True` |
| 依据键 | `rule_id`、`as_of`、`pe_hist_pct`、`pe_ind_pct`、`pe_gap`、`pe_gap_dir`、`loss_flag`、`negative_flag`、`tier_pe`、`tier_pb`、`fcf_yield`、`coverage`、`missing_inputs`、`gates` |
| 措辞 | **禁用「低估 / 高估 / 便宜 / 该买」**，一律「相对偏…」；`compute_logic` 必须写明「横截面分位不等于绝对贵贱」 |

| `rule_id` | 优先级 | 条件（全为字面量比较） | 结果 |
| --- | --- | --- | --- |
| VAL-00 | 0 | `loss_flag == true` | `loss` |
| VAL-01 | 1 | `negative_flag == true` | `negative` |
| VAL-02 | 10 | `pe_gap >= divergence_gap_pctl AND pe_gap_dir == 'hist_lower'` | `hist_cheap_ind_expensive` |
| VAL-03 | 11 | `pe_gap >= divergence_gap_pctl AND pe_gap_dir == 'hist_higher'` | `hist_expensive_ind_cheap` |
| VAL-04 | 20 | `pe_hist_pct <= cheap_pctl AND pe_ind_pct <= cheap_pctl` | `rel_cheap` |
| VAL-05 | 21 | `pe_hist_pct >= expensive_pctl AND pe_ind_pct >= expensive_pctl` | `rel_expensive` |
| VAL-06 | 99 | 其余 | `rel_fair` |

> `params`（已冻结）：`coverage_min=0.6`、`cheap_pctl=20`、`expensive_pctl=80`、`divergence_gap_pctl=30`。**20/80 对齐 `quantile.assign_tier` 的 20/40/60/80 档界**（`quantile.py:453-454`）与 `valuation.py:60-71 _pct_to_tier`；`divergence_gap_pctl` 单位为**分位点差**，与 §5.3 的「档差」是不同参数、不共用键名。

**供 §6.1 使用的 3 类归约映射**：`rel_cheap` / `hist_cheap_ind_expensive` → `cheap`；`rel_fair` / `hist_expensive_ind_cheap` → `neutral`；`rel_expensive` → `expensive`；`loss` / `negative` → `no_anchor`（无有效估值锚）。

### 5.2 `dim_quality_conclusion` 好不好

| 项 | 内容 |
| --- | --- |
| 回答 | 盈利的水平、稳定性、真实性与杠杆结构 |
| `inputs（判定用）` | `roe_tier` ← `qal_roe_tier.key_value`，`int`（`"1".."5"`，`LOSS` 无效）；`roe_stability` ← `qal_roe_stability.key_value`，`int`（**1=最稳定、5=波动最大**，`quality.py:178-181`）；`earnings_quality_tier` ← `qal_earnings_quality.key_value`，`int`（**1=OCF/净利最低、纸面利润风险最高；5=现金支撑充足；`LOSS` 无效**，`quality.py:205-209`）；`leverage_tier` ← `qal_leverage_tier.key_value`，`int`（**1=杠杆最低、5=最高**，`quality.py:151-154`）。**G1 分母 = 4** |
| `evidence_only` | `qal_gross_margin_tier`、`qal_net_margin_tier`、`qal_profit_years`（`"0".."4"`）；补数后的 `roic` / 周转率（§8.4）。连续盈利期数作韧性描述，不改结论 |
| 判定规则表 | 水平（ROE）× 稳定性 × 真实性三腿合取；杠杆单独降级；见下表 |
| enum 值域 | 6 码，见**附录 D.2** |
| 门禁 / 频率 | G1 + G2（`roe_tier` 与 `earnings_quality_tier` 至少一个有效）；`quarterly`（财报驱动）；A+H；`pit_capable=False` |
| 依据键 | `rule_id`、`as_of`、`roe_tier`、`roe_stability`、`earnings_quality_tier`、`leverage_tier`、`profit_years`、`coverage`、`missing_inputs`、`gates` |
| 措辞 | 禁用「好公司 / 优质 / 白马」；「盈利稳且现金实」描述的是**财报特征**，不是「证券好不好」 |

| `rule_id` | 优先级 | 条件 | 结果 |
| --- | --- | --- | --- |
| QAL-01 | 0 | `roe_stability >= volatile_min AND earnings_quality_tier <= quality_weak_max` | `deteriorating` |
| QAL-02 | 1 | `earnings_quality_tier <= quality_weak_max` | `paper_profit` |
| QAL-03 | 2 | `leverage_tier >= leverage_high` | `leveraged` |
| QAL-04 | 10 | `roe_tier >= roe_high AND roe_stability <= stable_max` | `solid` |
| QAL-05 | 11 | `roe_tier >= roe_high` | `high_but_volatile` |
| QAL-06 | 99 | 其余 | `weak` |

> `params`（已冻结）：`coverage_min=0.6`、`stable_max=2`、`volatile_min=4`、`quality_weak_max=2`、`leverage_high=4`、`roe_high=4`。

**供 §6.1 使用的 3 类归约映射**：`solid` → `solid`；`high_but_volatile` → `volatile`；`paper_profit` / `leveraged` / `weak` / `deteriorating` → `weak`。

### 5.3 `dim_growth_conclusion` 快不快

| 项 | 内容 |
| --- | --- |
| 回答 | 增速、持续性、加速度，以及增收与增利是否背离 |
| `inputs（判定用）` | `rev_tier` ← `grw_revenue_yoy_tier.key_value`，`int`（`"1".."5"`）；`np_tier` ← `grw_profit_yoy_tier.key_value`，`int`；`rev_yoy` / `np_yoy` ← 同两标签的 `num_value`，`float`（增速%，`num_unit='pct'`）；`persistence` ← `grw_growth_persistence.key_value`，`int`（`"0".."3"`，0=连续 0 期、3=连续 ≥5 期，`growth.py:123-133`）；`accel` ← `grw_growth_accel.key_value`，`enum`（`accelerating` / `steady` / `decelerating`，≥+5pp / 其余 / ≤−5pp，`growth.py:38-42,155-163`）。**G1 分母 = 6** |
| `derived_fields` | `growth_gap = abs(rev_tier − np_tier)`（`int`），写进 `evidence`，不进 G1 分母 |
| `evidence_only` | 新补的扣非增速（仅 A 股）、研发强度（仅 A 股）、CAPEX 强度（仅港股）、并购现金流（仅港股）、商誉（仅 A 股）——见 §8 |
| 判定规则表 | 见下表；背离优先于增速 |
| enum 值域 | 6 码，见**附录 D.3** |
| 门禁 / 频率 | G1 + G2；`quarterly`；A+H；`pit_capable=False` |
| 依据键 | `rule_id`、`as_of`、`rev_tier`、`np_tier`、`rev_yoy`、`np_yoy`、`growth_gap`、`persistence`、`accel`、`evidence_enhancements[]`、`market_scope`、`coverage`、`missing_inputs`、`gates` |
| **⚠ 关键约束** | **A/H 可用成员集不同**（§8.1）。若把新补的 A 股扣非 / 港股 CAPEX 放进判定，同一 enum 在两市场语义会不一致——正是 regime `DIM.FLOW` 单锚代理分的陷阱（§2.3 A2）。故新补数据**一律只进 evidence**，判定只用共有 4 标签。此约束不得因「数据补上了」而放宽（§8.4） |

| `rule_id` | 优先级 | 条件 | 结果 |
| --- | --- | --- | --- |
| GRW-00 | 0 | `growth_gap >= divergence_gap_tier` | `revenue_profit_divergent` |
| GRW-01 | 1 | `rev_yoy < 0 OR np_yoy < 0` | `contracting` |
| GRW-02 | 10 | `rev_tier >= high_growth_min AND np_tier >= high_growth_min AND persistence >= persistence_min AND accel == 'steady'` | `fast_sustained` |
| GRW-03 | 11 | `rev_tier >= high_growth_min AND accel == 'accelerating'` | `fast_accelerating` |
| GRW-04 | 12 | `rev_tier >= high_growth_min AND accel == 'decelerating'` | `fast_decelerating` |
| GRW-05 | 99 | 其余 | `slow_steady` |

> `params`（已冻结）：`coverage_min=0.6`、`divergence_gap_tier=2`（**1–5 档差**）、`high_growth_min=4`、`persistence_min=2`。

### 5.4 `dim_position_state` 当前位置与状态

| 项 | 内容 |
| --- | --- |
| 回答 | **当前处于什么位置 / 状态**（滞后描述），替代口语的「现在买合不合适」 |
| `inputs（判定用）` | `position_tier` ← `tst_price_position_52w.key_value`，`int`（`"1".."5"`，`1=[0,20)` … `5=[80,100]`，`trend.py:83-89,203-204`）；`momentum_tier` ← `trd_momentum_12_1.key_value`，`int`（`"1".."5"`，1=最弱，`technical.py:282-291`）；`volume_surge` ← `att_volume_surge.key_value`，`enum`（`surge`/`normal`/`shrink`，`attention.py:294-298`）；`volume_ratio_60` ← **`att_volume_surge.num_value`**，`float`——**复用 L1 已算的「当日量 / 近 60 日均量」倍数（`attention.py:310-323`），不再另起现算**，避免第二真相源与 G1 分母重复计数；`ma_alignment` ← `source: "v_daily_quote"`，由 `close` 的 `rolling(20)` / `rolling(60)` **现算 MA20/MA60**（`v_daily_quote` 无 ma 列，`sql/schema.sql:1083-1134`；`daily_trend` 的 MA 列只覆盖 `collect_trend` 采集池，`:459-460`），`enum`，`codes: ["bullish","neutral","bearish"]`（`MA20>MA60 且 close>MA20` → `bullish`；`MA20<MA60 且 close<MA20` → `bearish`；其余 `neutral`）。**G1 分母 = 5** |
| `evidence_only` | `tst_price_position_52w.num_value`（0–100 分位）、`trd_reversal_1m`、`trd_max_drawdown`、`trd_turnover`、`att_turnover_surge_tier`（`"1".."5"`）、`trd_volatility_annual`；两融 / 沽空 / 南向拥挤标签只覆盖子集（`daily_margin_balance` A 股 4,597 只 / `daily_short_selling` 869 只 / `daily_ggt_hold` 627 只，`profiling/tags/attention.py:8-16`），只写 `evidence_flags`，**不进判定、不进 G1 分母**。`att_volume_surge` / `att_turnover_surge_tier` 基于 `a_daily_quote`/`hk_daily_quote`（`attention.py:300,335`），不依赖 `fund_flow_daily` |
| 判定规则表 | **确定性条件合取 + 稳定性处理**（§7），不做加权打分、不用方向概率。见下表 |
| enum 值域 | 6 码，见**附录 D.4** |
| 门禁 / 频率 | G1 + G2（`position_tier` 必须存在）+ G3 新鲜度；`daily`；A+H；`pit_capable=True` |
| 依据键 | `rule_id`、`as_of`、`position_tier`、`position_52w`、`ma_alignment`、`volume_ratio_60`、`volume_surge`、`momentum_tier`、`evidence_flags`、`coverage`、`missing_inputs`、`gates`；**日变量** `state_raw`、`since_date`、`dwell_days`、`status`、`candidate{to,progress}`（落点见 §4.6） |
| **⚠ 声明（必须逐字落入 `compute_logic`）** | 「**描述性滞后状态，不是方向预测、不构成买卖建议**」；照抄 `REGIME_OUTPUT_SCHEMA.state_semantics`（`build_business_value.py:190-195`）的写法 |

| `rule_id` | 优先级 | 条件 | 结果 |
| --- | --- | --- | --- |
| POS-00 | 0 | `position_tier <= low_pos_max AND volume_ratio_60 <= vol_quiet_max` | `low_quiet` |
| POS-01 | 1 | `position_tier <= low_pos_max AND volume_ratio_60 >= vol_active_min AND ma_alignment == 'bullish'` | `low_volume_start` |
| POS-02 | 2 | `position_tier >= high_pos_min AND volume_ratio_60 >= vol_active_min` | `high_overheat` |
| POS-03 | 3 | `ma_alignment == 'bearish' AND momentum_tier <= weak_momentum_max` | `downtrend_unresolved` |
| POS-04 | 4 | `ma_alignment == 'bullish' AND position_tier == 3` | `trend_continuation` |
| POS-05 | 99 | 其余 | `range_bound` |

> `params`（已冻结）：`coverage_min=0.6`、`low_pos_max=2`、`high_pos_min=4`、`vol_quiet_max=0.5`、`vol_active_min=2.0`、`weak_momentum_max=2`。
> **code 命名**：`low_volume_start` / `trend_continuation` 的 code 保留，但 label 已去掉「启动 / 中继」的方向暗示（附录 D.4）；**不得据 code 字面解读为方向判断**。

> **不采用 6 阶段 `tst_stage`**：`profiling/tags/trend.py:18-22` 明确「待建：需 t+20/t+60 前瞻收益验证闭环，短期无法收敛，暂不注册」。本结论是**降维的 6 档描述状态**，不是那套状态机的替代品，也不占用 `tst_stage` 的编码。

---

## 6 L3 业务指标规格（**首期 2 个**）

### 6.0 准入三判据（对齐 `doc/market_profile.md §5.0「L3 业务指标准入」，约 :1408`）

每个 L3 结论必须同时满足，否则不得登记：

| 判据 | 要求 |
| --- | --- |
| ① 信息增量 | 必须引入单个 L2 表达不出的东西（跨域合成 / 多条件共振）；仅把某域重新切档不算 |
| ② 消费契约与动作语义 | 有明确的**数据消费契约**：按 `tag_code + key_value` 可检索、落 `tag_value`、带值域语义声明 |
| ③ 单一真相源 | 同一构念在数据层**只给一个结论**；不得与 L2 的 enum 值重复出档 |

### 6.1 `biz_value_quality_type` 好公司 × 好价格

| 项 | 内容 |
| --- | --- |
| 信息增量 | 单看「相对偏便宜」不知道便宜是不是因为公司变差；单看「盈利稳」不知道价格是否已透支。**交叉才产生「价值陷阱嫌疑」这类单域无法表达的判断** |
| `inputs（判定用）` | `valuation_class` ← `dim_valuation_conclusion.key_value` 按 §5.1 归约映射，`enum`（`cheap`/`neutral`/`expensive`/`no_anchor`）；`quality_class` ← `dim_quality_conclusion.key_value` 按 §5.2 归约映射，`enum`（`solid`/`volatile`/`weak`）。**只读两个 L2 的枚举码，不重算原子**；**G1 分母 = 2** |
| `evidence_only` | `valuation_code`、`quality_code` 原码、`ocf_to_profit`、`pe_hist_pct`（写 `cross_check` 作交叉校验，不改结论） |
| 判定 | 3×3 交叉矩阵（下表）；**任一侧未出档，或 `valuation_class == 'no_anchor'` → 整行不发布** |
| enum 值域 | **5 码**，见**附录 D.5**；不设 `loss_no_pe` 码（与 L2 的 `loss` 表达同一事实、违反判据③，且丢掉质量侧信息） |
| 频率 / PIT | `daily`（估值侧日频），但**只在任一 L2 码变化时切换**（§4.6 事件行）；`pit_capable=False`（质量侧财报无港股披露日） |
| 依据键 | `rule_id`、`as_of`、`valuation_code`、`quality_code`、`matrix_cell`、`cross_check{ocf_to_profit, pe_hist_pct}`、`coverage`、`gates` |
| 措辞 | 禁用「推荐 / 优选 / 值得买 / 评级」；`value_trap_risk` 写「嫌疑」，不作认定 |

| `valuation_class` \ `quality_class` | `solid` | `volatile` | `weak` |
| --- | --- | --- | --- |
| `cheap` | `VQ-C1` → `quality_rel_cheap` | `VQ-C2` → `value_trap_risk` | `VQ-C3` → `value_trap_risk` |
| `neutral` | `VQ-C4` → `quality_rel_fair` | **不发布** | **不发布** |
| `expensive` | `VQ-C6` → `quality_rel_expensive` | `VQ-C8` → `weak_and_expensive` | `VQ-C9` → `weak_and_expensive` |

> `VQ-Cx` 即 `conclusion_dict.rules[].rule_id`，与 `conclusion_detail.rule_id` 对应（§4.3.1）。`neutral × (volatile|weak)` 无明确交叉特征 → 按「宁可不出档」整行不发布（§4.2 纪律），不为凑满码表强行造类型。

**规则表结构化与兜底**：

| `rule_id` | `priority` | 条件 | 结果 |
| --- | --- | --- | --- |
| VQ-00 | 0 | `valuation_class == 'no_anchor'` 或任一侧未出档 | **整行不发布** |
| VQ-C1 / C2 / C3 / C4 / C6 / C8 / C9 | 10 / 11 / 12 / 20 / 30 / 31 / 32 | `valuation_class == '<行类>' AND quality_class == '<列类>'` | 矩阵对应码 |
| **VQ-99** | **99** | `{"all": []}`（空合取恒真） | **整行不发布**（覆盖 `neutral × (volatile / weak)` 及所有未列 cell） |

> 「整行不发布」不写 `tag_value` 行，按 §4.6 场景③关闭当前有效行；**它不是一个 enum 码**，不占附录 D 值域。

### 6.2 `biz_growth_quality_type` 增长质量

| 项 | 内容 |
| --- | --- |
| 信息增量 | `dim_growth_conclusion` 只说「快不快」，说不出「是不是真增长」；交叉现金流 / 盈利真实性 / FCF 符号才产生该判断 |
| `inputs（判定用）` | `growth_code` ← `dim_growth_conclusion.key_value`，`enum`（附录 D.3 六码）；`earnings_quality_tier` ← `qal_earnings_quality.key_value`，`int`（1=纸面利润风险最高）；`fcf_sign` ← `financial_indicator.free_cash_flow` **符号**，`int`（−1/0/1）；`persistence` ← `grw_growth_persistence.key_value`，`int`（`"0".."3"`）；`accel` ← `grw_growth_accel.key_value`，`enum`（三码）。**G1 分母 = 5** |
| `evidence_only` | 增速原值、研发强度 / 扣非增速 / CAPEX 强度等补数字段（`evidence_enhancements[]`，§8.4） |
| 说明 | `free_cash_flow` **取自 `financial_indicator`**（`get_financial.py:61,77,148` → `:519-523` 入库；`sql/schema.sql:786`），港股为 OCF−CAPEX 推算、A 股为每股 FCFF 推算（`get_financial.py:300-326`），**口径不同，只取符号不取数值**；CAPEX 明细见 `capex_fixed` / `capex_intangible` 列（§8.1），按 §8.4 只进 evidence。**`free_cash_flow IS NULL`**（港股现金流接口为空 `get_financial.py:300-307`、A 股缺每股 FCFF 行 `:316-320`）→ `fcf_sign` 为**无效成员**，不计入 G1 有效成员数，但 G1 分母仍为 5（`§4.2` 无效成员规则） |
| 判定规则表 | 见下表；背离优先于增速 |
| enum 值域 | 5 码，见**附录 D.6** |
| 频率 / PIT | `quarterly`；`pit_capable=False` |
| 依据键 | `rule_id`、`as_of`、`growth_code`、`earnings_quality_tier`、`fcf_sign`、`persistence`、`accel`、`evidence_enhancements[]`、`coverage`、`missing_inputs`、`gates` |
| 措辞 | 禁用「可持续增长 / 真成长 / 未来增速」；`one_off_like` 写「疑似」，不作认定 |

| `rule_id` | 优先级 | 条件 | 结果 |
| --- | --- | --- | --- |
| GQ-00 | 0 | `growth_code == 'contracting'` | `contraction` |
| GQ-01 | 1 | `earnings_quality_tier <= quality_weak_max` | `paper_growth` |
| GQ-02 | 2 | `fcf_sign < 0 AND growth_code in ('fast_sustained','fast_accelerating')` | `cash_burning_expansion` |
| GQ-03 | 3 | `persistence <= 1 AND accel == 'accelerating' AND growth_code in ('fast_accelerating','fast_sustained')` | `one_off_like` |
| GQ-04 | 99 | 其余 | `sustained_cash_backed` |

> `params`（已冻结）：`coverage_min=0.6`、`quality_weak_max=2`。

> **L3 首期只做 2 个**。不做「总分 / 综合评级 / 拥挤度预测」——分别违反 §0 非目标 3 与 1。

---

## 7 算法与模型选型

### 7.1 选型结论

> **确定性条件合取 + 稳定性处理；零预测模型、零 tick 数据、零前瞻收益优化。**

| 组件 | 选用 | 来源 |
| --- | --- | --- |
| 判定核心 | 条件合取规则（`first_match` 按优先级首个全匹配，§4.3.1；条件字段与字面值全部来自 L1 真实值域） | `build_business_value.py:672-678 pattern_first_match` 同款；§2.2 铁律① |
| 稳定性处理 | **首期：`state` = 当日裸判 + 游程标注**，落点按 §4.6 事件行（`state` 落 `key_value`，游程落当前有效行 `conclusion_detail`） | `build_business_value.py:800-823`（pattern v1.32）、`:1563-1573`（reversal v1.34） |
| 强度与方向解耦 | 强度量只作**门槛**不作结论；方向腿直接读原子原值 | `build_business_value.py:256-267`；`tools/calibrate_trend.py:17-21` |
| 可解释性 | 每条结论带 `rule_id` + 命中输入原值 + `coverage` + 缺失成员，写进 `conclusion_detail`；**由结构化条件机器复算**（§4.3.1） | `REGIME_OUTPUT_SCHEMA.detail_keys`、`build_business_value.py:393-405` |
| 出档门禁 | 「宁可说不确定，不给确定的错结论」 | `build_dimension_value.py:23-26` |

#### 7.1.1 什么是「防抖」，`regime 5/20` 又是什么

结论逐日计算。若每天独立用当日数据裸判，日线噪声会让 enum 在相邻两日来回跳（如「低位缩量」→「低位放量」→「低位缩量」），数据上表现为**日翻转率过高、结论不可信**。防抖 = 给判定加**状态记忆（滞回 / hysteresis）**，让新状态必须持续足够久才允许切换。

regime 的 `BIZ.MARKET_REGIME` 用的是**状态机改值**（`build_business_value.py:288-314 regime_state_machine`）：

- `dwell` = 当前状态已停留交易日数；`streak` = 候选状态连续出现天数；`candidate` = 正在挑战的新状态；
- 换态条件（`:307-309`）：`dwell >= min_dwell` **且** `streak >= confirm_days`；
- 三个参数在 `REGIME_PARAMS`（`build_business_value.py:163-181`）：`REGIME_DEFAULT_CONFIRM_DAYS = 5`（`:170`）、`REGIME_DEFAULT_MIN_DWELL = 20`（`:171`）、`REGIME_DEFAULT_SCORE_GATE = 30`（`:169`）。

**「regime 5/20」指：两个滞回参数，不是指标阈值，也不是模型超参。**

**为什么个股层不直接沿用 5/20**：

1. regime 是 2 个市场 × 30 年 ≈ 1.1 万行、状态切换次数期望很低；个股是 ~7,500 只 × 多年，标的数多 3 个数量级，且个股日线噪声远大于市场指数。
2. **regime 自己已证伪状态机对稀有档的适用性**：`BIZ.MARKET_PATTERN` v1.32 撤销状态机、改为「`state` 恒当日 raw + 游程标注」，原因是 `switch_confirm_days>=5` 会压死稀有形态（`build_business_value.py:515-530` 的 remark）；`BIZ.REVERSAL_RISK` v1.34 同款（`:1341-1358`）。个股 6–8 档里若有稀有档，状态机可能让它们永不出现。
3. §10 已有「日翻转率 <5%」验收线可直接度量稳定性，不必先靠状态机改值。

**已定**：首期采用**裸判 + 游程标注**（单日幂等；任意日期可由 `eff_from/eff_to` 回放复算，日变量按 §4.6 在当前有效行刷新）；若实测日翻转率不达标，再引入 `confirm_days` / `min_dwell` 滞回并按个股数据重新标定，**不直接抄 5/20**。

### 7.2 明确不采用的路线（附证据）

| 路线 | 不采用理由 |
| --- | --- |
| 6 阶段状态机 `tst_stage` | `profiling/tags/trend.py:18-22`：「需 t+20/t+60 前瞻收益验证闭环，短期无法收敛，暂不注册」 |
| HMM / Markov-Switching | `doc/market_profile.md §8.5（约 :3331）`：「HMM 在市场层的适用性优于个股层…规则为主 + HMM 交叉验证」；二期可选（M9，约 `:3475`），首期不引入 |
| LightGBM 蒸馏 / 监督预测 | `doc/stock profile hd.md:526-558` 的蒸馏路线以「各阶段前瞻收益显著分离」为前提，而项目已在个股与市场两层反复证明该前提不成立（§1.2 D1） |
| `trend_segmenter` ZigZag+PELT / `phase_analysis` 5 态 / `force_dynamics` 相位 / `kline_structure` / Volume Profile | 全部建立在分钟级 `trend_snapshot` + `tick_data`，默认单票或约 80 只 `collect_tick` 池（`build_force_rounds.py:3,253`、`profiling/tags/attention.py:12,365-366`：`fund_flow_daily` 仅 80 只、「全市场建标签会让 99% 的股票无值」）。**仅日线局部极值 / 枢轴 / 均线子算法可移植**。注意 §5.4 的 `att_volume_surge` / `att_turnover_surge_tier` 基于日线，不属此列 |
| `predictor_scan` / `direction_backtest` / `ofi_short_horizon` | 均为已证伪的预测因子扫描（AUC 0.44–0.54 / ~0.5，`predictor_scan.py:13-17`、`ofi_short_horizon.py:145-148`） |
| 以「各档前瞻收益显著分离（p<0.05）」为上线门槛 | `build_business_value.py:203-206`、`doc/market_profile.md §8 状态识别与验证（约 :3210）`：实测上行/下行分不开、下行反而最高；该门槛过不了，且会把项目拖回「标签准不准」的无限循环 |
| `params_hash` 内容指纹 | 见 §4.4.1：regime 的标定脚本机器生成字典才需要；个股层 6 个结论、规则由代码显式定义，用可读 `version` + `sync_conclusion_dict()` 校验即可 |
| 规则条件里写表达式 / 字段引用 / `between` 算子 | 会引入 DSL 解析器，违背「零新依赖、代码显式定义」；一律下沉为 `derived_fields`（§4.3.1） |
| 结论按日逐行存储 | 采用事件行（值变化才写行，§4.6）；逐日行会产生大量重复值，且 `as_of` 逐日序列可由 `eff_from/eff_to` 回放生成（§10.1） |
| 直接沿用 regime 的 `5/20` 滞回参数 | 个股标的数多约 3 个数量级、日线噪声更大，且 regime 自身已证伪状态机对稀有档的适用性；首期用裸判 + 游程标注，翻转率不达标再按个股标定（§7.1.1） |
| `NOT` / 排除筛选 | 本期不考虑；消费端筛选是标签间 AND、同标签内 OR（§3.1），无 NOT 语法，不进本期数据契约 |
| 用原始 OCF/净利比率自造 `ocf_floor` 阈值 | OCF/净利的行业差异极大，且已有标定好的 `qal_earnings_quality` 档位专司此事，再设阈值等于第二真相源；判定统一用 L1 档位 |

### 7.3 数据层约束（实现时必须遵守）

| # | 约束 |
| --- | --- |
| 1 | 只用 `v_daily_quote`（A 股 1990-12 起 5,711 只 / 港股 2019-01 起 2,831 只，`doc/market_profile_collection_plan.md:82-83`）；**不碰 `tick_data`** |
| 2 | `high_52w/low_52w` 在 `a_daily_quote` 历史覆盖≈0，**必须按 252 交易日自回算**（`profiling/tags/trend.py:30-32,121-183` 已有实现，含脏数据过滤与 500 只/批流式加载） |
| 3 | `daily_trend`（MA/MACD/RSI）只跑 `collect_trend` 采集池（`market_scheduler.py:92-103`），**均线一律自己按 close 现算** |
| 4 | 港股 `turnover_rate` 2026 年前为空，需自算；估值/市值仅 2018 起、退市股留存极少，须标幸存者偏差 |
| 5 | 复用 `profiling/tags/technical.py:43-50,209-255` 的按股票分批 + dtype 瘦身框架，不要一次性拉全市场（`doc/compute_task_resource_standard.md` §2 R1） |

---

## 8 数据源与补数计划

### 8.1 数据源字段可得性

`financial_indicator` 是指标摘要表（`sql/schema.sql:771-812`，DDL **17 列**、其中业务/日期列 15 个），**无三表明细表**（`schema.sql` 无利润表/资产负债表/现金流量表 DDL，只有 `v_financial_quarterly` 视图）。采集端 `get_financial.py` 用 AKShare：港股 `stock_financial_hk_analysis_indicator_em` + 现金流量表、A 股 `stock_financial_abstract`（新浪长表）、披露日 `stock_yjbb_em`（仅 A 股）。

| 缺失字段 | 可得性 | 落地批次 |
| --- | --- | --- |
| `grw_non_gaap_yoy_tier` 扣非增速 | **A 股可得**：新浪长表 80 行指标中第 4 / 53 行是「扣非净利润」，实测有值（茅台 2026Q3 643.2 亿、2025 年报 822.9 亿；分众 2026Q3 35.9 亿）。同花顺 `stock_financial_abstract_ths` **直接给「扣非净利润同比增长率」**，0.4 秒/只、104 期。**港股无源**（该接口传港股代码抛错） | **第一批** |
| `grw_capex_intensity_tier` 资本开支强度 | **港股年报口径 100% 可得**：`_fetch_hk_cash_flow`（`get_financial.py:276-297`）已抓「购建固定资产」「购建无形资产及其他资产」的 `AMOUNT`；实测腾讯 96 期中 38 期有值，**25 个年报期 25/25 全覆盖**（2025 年报 874.8 亿）。A 股无 CAPEX 分解。**FCF 结果落 `financial_indicator.free_cash_flow`**（`get_financial.py:56-61,77,148,300-326` → `:519-523` 入库），CAPEX 明细见 `capex_fixed` / `capex_intangible` 列 | **第一批** |
| `grw_rd_intensity_tier` 研发强度 | **仅 A 股可得**：东财 `stock_profit_sheet_by_report_em`（203 列）含 `RESEARCH_EXPENSE` / `RESEARCH_EXPENSE_YOY`，最新期有值（茅台 2026Q3 1.48 亿 / +30.6%），非空 37/104 期（早期会计准则未单列），附 `NOTICE_DATE` 披露日。**3.3 秒/只、21 次分页**，全 A 股约 5.2 小时/轮。港股利润表 32 科目 / 资产负债表 71 科目 / 分析指标 36 列**均无研发** | **第二批**（需独立 unit） |
| `grw_source` 成长来源 | **部分可得（降级）**：港股现金流量表有「收购附属公司」（年报 19/25 有值，腾讯 2025 年报 361.9 亿）、「购买子公司少数股权而支付的现金」；A 股新浪长表有「商誉」（分众 1.72 亿）。**是现金流/商誉代理，不是并购公告解析** | 只进 evidence 或第三期，不宣称「识别并购驱动」 |
| 员工规模增速 | `company_profile.employee_count` 有 A+H 静态水平值但覆盖式 upsert 不产生历史，增速不可算；连 `planned_no_data` 都未注册 | **放弃** |

**顺带零成本可得**（同一批未映射行，对 ④③ 域也有用）：`投入资本回报率`（ROIC）、`净资产收益率_平均_扣除非经常损益`、`存货/应收/总资产周转率`、`经营活动净现金/归属母公司的净利润`、`每股息税前利润` / `息税前利润率`；港股侧 `ROIC_YEARLY`、`加:折旧及摊销`（年报 19/25）、资产负债表 `现金及等价物` / `短期贷款` / `长期贷款`。

### 8.2 批次与采集成本

| 批次 | 内容 | 新增请求成本 | 落地方式 |
| --- | --- | --- | --- |
| **P1（第一批）** | 扣非增速、CAPEX 强度、④ 域 ROIC / 周转率增强 | **零**：新浪长表与港股现金流量表都在现有「财务指标全量」每周一 02:00 任务里已采（`market_scheduler.py:242-249`），只需扩 `A_INDICATOR_MAP` 白名单 + 加列落库 | 改 `get_financial.py` + `sql/schema.sql` + `profiling/quantile.py` 显式 SELECT |
| **P2（第二批）** | A 股研发强度 | 东财利润表 3.3s/只 ≈ 5.2h/轮、21 次分页/只 | **必须独立 systemd unit**（`doc/compute_task_resource_standard.md` §2 R4 独立 oneshot + 六项资源参数；R5 禁止塞进 `market_scheduler`），低频（季频足够） |
| **P3（可选）** | 并购代理 | 港股零成本、A 股零成本 | 只写 evidence |
| 放弃 | 员工增速 | — | 维持 `blocked_reason` |

### 8.3 结构约束（维护时必须遵守）

1. `financial_indicator` 与 `profile.*` 的结构变更一律同步 `sql/schema.sql` 与 `profiling/schema.py`（AGENTS.md 数据库结构变更约定，最高优先级）；`tag_value` 是分区表（`PARTITION BY RANGE(eff_from)`），对父表加列会自动传播到各分区。
2. `profiling/quantile.py` 的 `load_financial_latest` / `load_financial_history` / `load_financial_ttm` 为**显式列 SELECT**，`load_financial_context` 是 pandas 显式列投影；新增财务列不会自动进入标签底表，必须逐处同步。
3. 若做「扣非净利 TTM 同比」，需扩展 `load_financial_ttm`（现仅覆盖营收 / OCF / 净利，公式 `TTM = 本期累计 + 上年全年 − 上年同期累计`）或直接用同花顺现成同比；**禁止把累计口径 `SUM(近4期)`**（分母会虚高约 2.4 倍）。
4. `tag_registry.conclusion_dict` / `tag_value.conclusion_detail` 的职责见 §4.3；**不给 `TagMeta` 加 `conclusion_dict` 字段**（`sync_registry()` 用 `asdict(TagMeta)` 全列 upsert），不新建独立字典表或 evidence 侧表，不新增 `version` / `rule_version` / `params_hash` 列。
5. 本设计**不引入任何新第三方依赖**，`requirements.txt` 无需改动。

### 8.4 补数的使用纪律（§5.3 的延伸）

> 新补字段**一律只进 `evidence_only`，不进 `conclusion_dict.inputs`（即不进判定、不进 G1 分母）**，直到满足两个条件才允许放宽：① 两市场都有等价字段；② 该字段的覆盖率通过 G1 门禁并写进 `market_scope`。
>
> 理由：A 股有扣非/研发/商誉，港股有 CAPEX/并购现金流，成员集不对称；放进判定会让同一个 enum 在 A/H 上语义不同（§2.3 A2）。

### 8.5 PIT 约束

- `financial_indicator` 按 `report_date`（报告期）入库；`announce_date` **仅 A 股有且覆盖 96.5%**，港股恒 NULL（`get_financial.py:193-194,223`）；A 股回填存在「历史行普遍后移约 1 年、间隔中位数 392~475 天」污染（`profiling/quantile.py:207-209`）。
- **按「判定用输入」的最弱环声明 `pit_capable`**（§4.1）：④⑤ 及两个 L3 的判定含财报衍生且港股无披露日 → `False`、`update_freq=quarterly`，**不用于严格回测**；③⑦⑩ 判定输入仅量价 / PE 分位（§5.1 已把 4 个 `pit_capable=False` 的估值档移到 `evidence_only`）→ `True`、`update_freq=daily`。这与 §1.3 的日频/季频口径一致。
- `profiling/tags/growth.py` 8 个 `@tag` 全部 `pit_capable=False`，与代码一致；「财报无披露日」应理解为「A 股 96.5% 有 `announce_date`、港股恒 NULL」。
- 东财利润表的 `NOTICE_DATE` 可补齐 A 股披露日（P2 顺带），港股披露日仍无源。
- ⚠ 全量重建历史时仍须按 `as_of` 取当时的 L1 版本（§4.4.3 约束 1）。**已知局限**：L1 当日走 snapshot 原地 `UPDATE`（`engine.py:224-225,369-381`），历史日才是版本链，因此「取当时版本」的严格性受既有 L1 写入方式限制；不得据此宣称严格 PIT。

---

## 9 计算与调度

### 9.1 接线

```
system/compute-profile.timer (Mon-Fri 17:30, oneshot / MemoryMax=512M)
  → compute_profile.run()
       ① registry.sync_registry()          写 tag_registry / enum_value（含 6 个结论标签元数据
                                           与 version）
          sync_conclusion_dict()           写 tag_registry.conclusion_dict（从 RULES 序列化，
                                           并校验 version 一致）
       ② 逐标签独立事务循环               遍历 77 个标签，按 source_type='derived' 跳过 6 个
                                           结论标签；实际产出 59 个 L1
       ③ conclusion_producer              读 RULES（内存）+ profile.tag_value + v_daily_quote
                                           → 按 §4.3.1 声明做 cast / 派生字段计算
                                           → 算 4 个 L2 + 2 个 L3
                                           → 按 §4.6 事件行写回 tag_value
                                             （含 conclusion_detail + version）
                                           → 每个结论标签写 tag_run_log(status='ok', version)
```

> **落地前提**：结论生产者随 `compute-profile.service`（oneshot）链路运行，不进 `market_scheduler` 进程（约束 4）；timer 启用、首日防双跑与内存峰值核验属运维动作，见 §12.1 第 2 项。

| # | 约束 |
| --- | --- |
| 1 | **结论标签不能走 `engine.compute_tag()`**：它是单标签独立事务、输入为 `(conn, tag_code, as_of=None, dry_run=False, force=False, mode="auto")`，而结论依赖多个上游标签。`compute_profile.py:113-121` 的循环当前遍历 `registry.all_tags()`（77 个），必须**按 `source_type='derived'` 跳过**结论标签，交由 ③ 单独处理。按元数据识别而非编码前缀 |
| 2 | ③ 必须在 ② **全部完成并提交之后**跑，否则读到半批数据。顺序由 `compute_profile._compute()` 编排，不用 DAG 框架。注意 `sync_registry()` 当前是「单独连接先提交、失败只记 error 不中断」（`compute_profile.py:99-104`），故 **③ 的 `version` 一律从内存 `RULES` 取，不查库**，避免字典同步失败当天读到旧版本 |
| 3 | ③ 内部顺序：先 4 个 L2，再 2 个 L3（L3 只读 L2 的枚举码，不重算原子） |
| 4 | **不得迁入 `market_scheduler` 进程内**：画像已因内存-IO 雪崩迁出独立 oneshot（`doc/compute_task_resource_standard.md` §1 事故档案 / §2 R5）。结论生产者接在 `compute-profile.service` 之后或同链路第二段 |
| 5 | 遵守 `doc/compute_task_resource_standard.md` §2 红线：大表查询带实体过滤 / 时间下界（**R7**）、按股票分批（**R1**）、单事务 ≤5 万行（**R6**）、实测峰值内存（**R3** + §5）、登记监控埋点（**§7**，`collection_task_log` 或 `tag_run_log`）。全量重建走手动脚本 `compute_conclusions_rebuild.py`（默认预演、`--yes` 执行、全程日志 + 峰值 RSS）；仍不进 `market_scheduler`、不进 17:30 日常链路（R5 成立） |
| 6 | **版本写入**：③ 每行必须写 `tag_value.version = RULES[code].version`；`tag_run_log` 同源且 **`status='ok'`**——`DATA_SOURCES["profile_tag_value"].version_fn` 取 `tag_run_log max(created_at) where status='ok'`，漏写则下游 DAG 不刷新（§3.1 / §3.4 R2）。这是 §4.4.2 残留检测与 §10.1 版本一致性验收的前提 |
| 7 | **覆盖方式**：**仅规则升级的全量重建**走 `DELETE + INSERT`（按 6 个 `tag_code` 删光后按 `as_of` 重算）；日常增量按 §4.6 事件行（值变 → 关闭旧行 + 插新行；值不变 → 原地刷新日变量；门禁不通过 → 关闭当前行不插新行），**不用 `UPDATE key_value`** |
| 8 | **历史输入**：重建历史日期时按 `eff_from<=as_of AND eff_to>=as_of` 取当时的 L1 版本，不取当前有效值（§4.4.3 约束 1） |
| 9 | 幂等：同日重复跑得到同一结果；值不变只刷新日变量、不产生新行；`eff_from/eff_to` 版本化语义保持（唯一例外已收窄为「规则升级时全量重建」，§4.6） |
| 10 | **规则真源**：判定只读内存 `RULES`，`conclusion_dict` 是只读发布物；`sync_conclusion_dict()` 负责写入与 version 一致性校验（§4.3.1） |
| 11 | **输入归一化**：按 `inputs[].source_col` 取 `key_value` / `num_value`，按 `dtype` 做 cast（档位字符串 → `int`），`LOSS`/`NEGATIVE` 置为无效成员；`derived_fields` 在喂条件引擎前算好并写进 `evidence`。禁止在条件求值器里解析表达式 |

---

## 10 验证与验收

### 10.1 效标（**描述性效标，不以预测收益为门槛**）

| 指标 | 目标 | 说明 |
| --- | --- | --- |
| 覆盖率 | 分市场报：L2 各结论 A/H 分别 ≥80%；**L3 按「可交叉候选」口径 ≥70%** | 按 §4.2 的 G1 分母（`conclusion_dict.inputs`）统计。L3 分母**排除** `no_anchor` 与设计上不发布的 cell，并单列排除数；用全样本分母会把设计使然的排除误判为覆盖不足。**若某结论出档率 <80%，先查 inputs 覆盖率与 `blocked_reason`，不得直接下调 `coverage_min` 凑数**；不达标的写进 `conclusion_detail.missing_inputs`，不得静默。 |
| 稳定性 | **日频结论单日翻转率 <5%** | 对齐 `doc/stock profile hd.md:698`（日频标签 <5%/日）。**离线算法**：按 `as_of` 回放生成逐日序列再比较相邻日（§4.6 第 3 条），不要求库里存逐日行 |
| 状态游程 | 报告 `dwell_days` 分布与状态切换次数 | 由事件行 `eff_from` 与当前有效行 `conclusion_detail` 统计；防「每天换一个结论」；参照 `build_business_value.py:420-429` |
| 分布合理性 | 各 enum 值域的样本占比，报告是否有 0% 档 / 单档 >80% | 参照 regime `zero_states` / `zero_bands` 统计。**双分位合取 + 兜底 `rel_fair` 易造成单档 >80%**，命中时优先调 `divergence_gap_pctl` 或细分 `rel_fair`，不得下调 `coverage_min` |
| **值域对齐** | 6 份 `inputs[]` 中每个 `field` 的 `tag_code` / `source_col` / `dtype` / `codes` 与 L1 真实值域一致；规则表所有字面值都在该字段值域内 | §4.3.1；**P0 准入门槛** |
| 口径一致性 | 同一 enum 在 A/H 语义一致；`conclusion_detail.coverage` 与门禁判定可复算 | §8.4 |
| 版本一致性 | 规则升级全量重建后，`tag_value.version` 全部等于 `tag_registry.version`；§4.4.2 残留查询返回 **0 行** | 版本即口径（§0）；残留可达性论证见 §4.6 |
| 人工抽检 | 抽 30 只（A/H 各半、含次新股 / 亏损股 / ST / 停牌复牌）核对结论与依据是否自洽 | 规则类验收（设计稿 §8.2） |
| 可解释 | 每条结论都能由 `conclusion_detail.rule_id` + `evidence` 经 **`conclusion_dict.rules` 的结构化条件**机器复算出同一个 enum | 防「结论与依据对不上」；自由文本 / 表达式条件不满足此项（§4.3.1） |
| 消费端接线 | `conclusion_detail` 可被消费端读出；`tag_run_log(status='ok')` 写入后 `get_node_version()` 前进；6 组 `short_label` 全部非空 | §3.4 R1/R2/R5；附录 D |

### 10.2 明确不做

- **不做**「各档 t+20/t+60 前瞻收益显著分离（p<0.05）」作为上线门槛（§7.2）；
- 若做前瞻收益，只能作为「样本内、重叠窗口、未扣成本、仅描述性」的**披露项**，不得写进验收结论或对外宣称可交易；
- 不做跨结论横比、不做雷达汇总、不做「综合分」；
- **不做「复盘当时系统给了什么结论」**（§4.4.4 已定：场景不存在、无意义）。

### 10.3 防回退测试

必须覆盖：

1. 门禁不通过不写行（历史重建 = 删除语义、日常增量 = 关闭 `eff_to`）；
2. 旧结论被正确关闭、关闭行 `version` 仍为当前版、残留查询返回 0 行；
3. 负 PE 走 `loss`（L1 `LOSS` → 结论层 `loss` 映射）不回填分档；`NEGATIVE` 同；`LOSS`/`NEGATIVE` 不计入 G1 有效成员；
4. A/H 成员集不对称时 `evidence_only` 不进判定、不进 G1 分母；`derived_fields` 不进 G1 分母；
5. `version` 升级后全量重建且残留查询为 0；`sync_conclusion_dict()` 的 version 不一致能报错；
6. 重建按 `as_of` 取 L1 当时版本（防前视偏差）；
7. `DELETE+INSERT` 不违反 `UNIQUE(stock_code,tag_code,key_value,eff_from)`；日常 `UPDATE` 只改非键列；
8. `conclusion_dict` 与 `conclusion_detail` 各守本职、无同名混用；`RULES` 与 `conclusion_dict` 内容一致；
9. **事件行**：值不变只刷新 `conclusion_detail` 日变量、不产生新行、不产生重复 `eff_from`；`dwell_days = as_of − eff_from` 可复算；
10. 结构化条件可由 `rule_id + evidence` 复算出同一 enum；`value_range=NULL` 时 `_tag_pct()` 返回 `None`；
11. **值域对齐**：所有 `rules[].conditions[].field` 在 `inputs[]` / `derived_fields[]` 内；所有 `value` 字面值在该字段声明的 `codes` / 值域内；档位 `key_value`（字符串 `"1".."5"`）按 `dtype=int` cast 后比较结果符合预期；`op` 不含 `between`、条件内不含表达式；
12. 6 组 enum 共 35 个 `short_label` 非空；6 阶段 `tst_stage` 编码未被占用；结论层 enum code 全小写、`LOSS`/`NEGATIVE` 原码保留在 `evidence`。

---

## 11 合规与措辞

> 设计稿 §9 风险 8（`doc/stock profile hd.md:740,744`）：系统定位为「内部研究工具 / 信息聚合」，**不输出买卖建议**；对外服务需明确免责与资质边界。

| 禁用 | 允许 | 理由 |
| --- | --- | --- |
| 低估 / 高估 / 便宜 / 贵 | 相对偏便宜 / 相对偏贵（含参照系） | 横截面分位 ≠ 绝对贵贱 |
| 好公司 / 优质股 / 白马 | 盈利稳且现金实 / 利润现金转化弱 | 描述财报特征，不评价证券 |
| 买点 / 卖点 / 合适 / 不合适 / 建议 | 低位缩量 / 高位过热 / 当前位置与状态 | 滞后描述，非建议 |
| 推荐 / 优选 / 评级 / 打分 | 交叉类型标签（含「嫌疑」「疑似」） | 不做排序与预测 |
| 未来增速 / 可持续增长 / 预期收益 | 增速持续 / 增速放缓 / 现金转化弱 | 现状判断，非预测 |

**每个结论标签的 `compute_logic` 与 `enum_value.label` 必须遵守上表，且 `compute_logic` 含一句**：「本结论为描述性判断，不构成投资建议，不预测未来价格走势」。附录 D 的 `label` / `short_label` 已按此表逐条复核。

---

## 12 实施路线

### 12.1 实施前置

| # | 动作 | 责任 | 详情 |
| --- | --- | --- | --- |
| 1 | `综合研判` 白名单登记 + 消费端 `conclusion_detail` 读取（§3.4 R1） | **本轮不实施**（`stock-realtime` 不在本轮范围）。两项均为消费端能力、不影响数据契约本身：未登记时该域按 `db.py:9036-9055` 兜底排末位、不丢数据；消费端暂读不到判定依据，需由消费端项目自行排期 | §4.1.1 / §3.4 |
| 2 | `compute-profile.timer` 三步确认：旧 scheduler 已用新代码重启 → `enable --now` → 首日防双跑 + 内存峰值核验 | **运维，用户执行**；未确认前 17:30 日常链路不会自动运行 | §9.1 落地前提 |

> **范围限定**：下表所有「已完成」均指本机 `localhost/market_db`（2026-11-12 实测）；**生产库未部署、未调度**。完成度逐项盘点见 §12.2。

| 阶段 | 内容 | 产出 | 验收锚点 |
| --- | --- | --- | --- |
| **P0 契约冻结** | **已完成** | 本文 **v1.0** | 全部结论可用 enum 表达、值域无「预测/建议」措辞；35 个 `short_label` 非空；规则表字段与字面值全部对齐 L1 真实值域；结构化条件可按投影约定机器复算；切点经实测标定冻结；残留查询设计上可达 0 行 |
| **P1 补数（第一批）** | **部分完成（结构就绪、数据未回填）**：代码/schema 侧均已落地——扩 `A_INDICATOR_MAP` 6 项、港股 CAPEX/并购明细透传、`financial_indicator` 加 10 列、`quantile.py` 显式 SELECT 同步、`sql/schema.sql` + 本机库幂等迁移；**但本机库 10 列 `non_null = 0 / 365,369 行`**，全量财务采集尚未重跑；且按 §8.4 只进 evidence，当前结论生产者还未消费这些字段 | 零新增请求的新字段（仅结构） | 比率列实测 `NUMERIC(12,4)`；`fetch_a('600519')` **函数级**实测有值（未写库）；`tests/test_p1_financial_columns.py` 全绿；**回填后须复核各列非空行数** |
| **P2 落表 + L2 四结论** | **已完成**：两个 JSONB 列、`RULES` + `sync_conclusion_dict()`、结论生产者、4 个 `dim_*` + 2 个 `biz_*`、`compute_profile` / `profiling.run` 接线、手动重建脚本、防回退测试。`tag_run_log` 已接线；**§3.4 消费端读取本轮不实施** | 6 个可检索 enum + 依据明细 | 本机端到端两次运行：事件行 `inserted=0 / refreshed / closed=811`、残留查询 0、`pytest tests/ -q` 全绿 |
| **P3 L3 两结论** | **已并入 P2 一次落地**（`biz_value_quality_type` 5 码 + `biz_growth_quality_type` 5 码） | 2 个交叉类型 enum | §6.0 三判据；`growth_code` 设为主锚后，L2 未出档的票正确不发布 |
| **P4 补数（第二批）** | A 股研发强度（独立 unit，季频）；`rd_expense` 列已在 P1 登记、暂不采集 | evidence 增强 | 覆盖率达标后进 evidence，不进判定 |

> **每阶段收尾必须回到 §1.1 逐条核对**：产出是否仍在回答投资者的六个问题，有没有滑向「打分 / 预测 / 建议」。这是防止需求走偏的闸门，与 `doc/market_profile.md` 的对齐闸门同款。

### 12.2 实施进度盘点（2026-11-12，以本机库实测为准）

> **本节所有「已完成」均指本机 `localhost/market_db`**（2026-11-12 实测），生产库一行未动、代码未部署、timer 未启用。

#### 12.2.1 已完成（代码 + 本机库均有证据）

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| P0 契约冻结 | ✅ | 文档 **v1.0**；切点经 `tools/calibrate_conclusion_cutpoints.py` 实测标定冻结；35 个 `short_label` 已入 `profile.enum_value` |
| P2 落表 + 4 个 L2 | ✅ | 两个 JSONB 列已生效并传播到 4 个分区；`RULES` / `sync_conclusion_dict()` / 生产者 / `compute_profile` + `profiling.run` 接线 / 6 标签注册 / 防回退测试全部落地；本机当前有效 L2 合计 **27,274 行**，`conclusion_detail` 全非空 |
| P3 两个 L3 | ✅（并入 P2 一次落地） | `biz_value_quality_type` **1,873** 行、`biz_growth_quality_type` **7,354** 行；`growth_code` 设为主锚后 L2 未出档的票正确不发布 |
| 重建入口 | ✅（手动脚本形态） | `compute_conclusions_rebuild.py` 手动脚本：默认预演 / `--yes` 执行 / 全程 INFO 日志；`system/compute-conclusions-rebuild.service` 已删除、bootstrap 不再渲染 |
| 全局 | — | 6 个结论当前有效合计 **36,501 行**、`conclusion_detail` 非空 36,501 行；`tag_run_log` 12 条 `ok/v1`；残留查询 **0 行**；回归测试全绿 |

> P0 中「6 组 enum 中文措辞终审」是按 §11 禁用词表由本层复核通过，**未走业务侧单独签字**；若需正式业务确认，列入待办。

#### 12.2.2 部分完成

| 项 | 已完成 | 未完成 |
| --- | --- | --- |
| **P1 补数（第一批）** | schema / 代码侧全就绪：`financial_indicator` 10 个新列 + 幂等迁移、`A_INDICATOR_MAP` 6 项、港股 CAPEX / 并购明细透传、`quantile.py` 显式 SELECT 同步、`tests/test_p1_financial_columns.py` | **数据未回填**：本机库 10 列 `non_null = 0 / 365,369 行`（全量财务采集未重跑）；`fetch_a('600519')` 只是函数级验证、**未写库**；且按 §8.4 只进 evidence，当前结论生产者**尚未消费**这些字段 |

#### 12.2.3 未完成清单

| # | 项 | 性质 | 章节 |
| --- | --- | --- | --- |
| 1 | 消费端 `stock-realtime/db.py`：`综合研判` 白名单登记 + `conclusion_detail` 读取 | **本轮不实施**（非遗漏）；消费端暂读不到判定依据，该域按 `db.py:9036-9055` 兜底排末位、不丢数据 | §12.1 第 1 项 / §3.4 |
| 2 | `compute-profile.timer` 三步确认、生产部署、服务重启 | **运维，用户执行**；未确认前 17:30 日常链路不会自动运行结论生产者 | §12.1 第 2 项 / §9.1 |
| 3 | P4 A 股研发强度采集 | 未做；`rd_expense` 列已登记、暂不采集 | §12 P4 |
| 4 | 全量历史逐日回放 | 未做；受 L1 当日 snapshot 原地 UPDATE 的 PIT 局限，`rebuild()` 只重建「当前 L1 有效值 + 当前规则」下的结论 | §8.5 |
| 5 | §10.1 剩余验收：日翻转率 <5%、A/H 分市场覆盖率、30 只人工抽检 | 未跑（分别需结论层逐日序列、分市场统计、人工核对）；值域对齐 / 版本一致性 / 可解释 / 分布合理性已由测试与端到端覆盖 | §10.1 |
| 6 | P1 数据回填与 evidence 接线 | 未做 | §12.2.2 |

#### 12.2.4 生产视角的关键结论

**在生产上，结论层目前是「代码就绪，但零数据、零调度」**——本机那 36,501 行由手动 `produce()` 产出，生产库一行未动。要让生产真正跑起来，最短路径：

1. 完成 §12.1 第 2 项（旧 scheduler 确认 → `enable --now compute-profile.timer` → 首日防双跑 + 内存峰值核验）；
2. 部署代码，由 `deploy.sh up` 走 `sql/sync_schema.sh` 增量同步 DDL（两个 JSONB 列 + 10 个 P1 列）；
3. 在生产手动跑一次 `compute_profile.run()`（或等 17:30 链路），核对 6 个结论出档与 `tag_run_log(status='ok')`；
4. 重跑财务全量采集回填 P1 新列，复核各列非空行数。

---

## 附录 A：数据源字段可得性证据

| 接口 | 参数 | 结果 |
| --- | --- | --- |
| `ak.stock_financial_abstract` | `symbol='600519'` / `'002027'` | `(80, 106)`，80 行指标；「扣非净利润」「商誉」「投入资本回报率」「存货/应收/总资产周转率」「ROE_扣非」「OCF/归母净利」均有真实数值；**无研发** |
| `ak.stock_financial_abstract_ths` | `symbol='600519', indicator='按报告期'` | `(104, 25)`，**直接给「扣非净利润同比增长率」**，1998 起；港股代码抛 `AttributeError`（不支持） |
| `ak.stock_profit_sheet_by_report_em` | `symbol='SH600519'` | `(104, 203)`，`RESEARCH_EXPENSE` / `_YOY` / `ME_RESEARCH_EXPENSE` / `NOTICE_DATE`；最新期有值；非空 37/104 |
| `ak.stock_financial_hk_report_em` | `stock='00700', symbol='现金流量表'` | `(1810, 11)`，60 科目 / 96 报告期；年报 25 期中「购建固定资产」25/25、「收购附属公司」19/25、「经营业务现金净额」25/25 |
| 同上 | `symbol='资产负债表'` | 71 科目，**无商誉** |
| 同上 | `symbol='利润表'` | 32 科目，**无研发 / 无商誉** |
| `ak.stock_financial_hk_analysis_indicator_em` | `symbol='00700'` | `(9, 36)`，含 `ROIC_YEARLY` / `ROA` / `CURRENT_RATIO`；无扣非 / 研发 / 商誉 |

## 附录 B：参与结论的现有标签（59 active 中的相关项）

| 域 | tag_code |
| --- | --- |
| ③ 估值水平（9） | `val_pe_ttm_tier` `val_pb_tier` `val_ps_ttm_tier` `val_pcf_ttm_tier` `val_peg_tier` `val_pe_ind_pct` `val_pe_hist_pct` `val_pb_ind_pct` `val_fcf_yield_tier` |
| ④ 盈利质量（7） | `qal_roe_tier` `qal_gross_margin_tier` `qal_net_margin_tier` `qal_leverage_tier` `qal_roe_stability` `qal_earnings_quality` `qal_profit_years` |
| ⑤ 成长特征（4） | `grw_revenue_yoy_tier` `grw_profit_yoy_tier` `grw_growth_persistence` `grw_growth_accel` |
| ⑥ 股东回报（6，第二批） | `shr_dividend_yield_tier` `shr_payout_tier` `shr_consecutive_years` `shr_dividend_growth` `shr_buyback_tier` `shr_total_yield_tier` |
| ⑦ 交易特征（9） | `trd_momentum_12_1` `trd_reversal_1m` `trd_volatility_annual` `trd_beta` `trd_residual_vol` `trd_downside_vol` `trd_liquidity` `trd_turnover` `trd_max_drawdown` |
| ⑨ 资金关注度（8） | `att_margin_balance_tier` `att_margin_net_tier` `att_margin_trend` `att_short_selling_tier` `att_southbound_hold_tier` `att_southbound_trend` `att_volume_surge` `att_turnover_surge_tier` |
| ⑩ 趋势状态（1） | `tst_price_position_52w` |

> **值域事实（规则表 `inputs[]` 的对齐基准）**：`assign_tier` 输出的 `key_value` 是**字符串** `"1".."5"`（`profiling/quantile.py:455`），边界 20/40/60/80（`:453-454`）；`num_value` 才是连续值。`qal_roe_stability` 1=最稳定 / 5=波动最大；`qal_earnings_quality` 1=纸面利润风险最高 / 5=现金支撑充足、另有 `LOSS`；`qal_leverage_tier` 1=最低 / 5=最高；`grw_growth_persistence` 为 `"0".."3"` 连续正增长期数档；`grw_growth_accel` 为 `accelerating`/`steady`/`decelerating`（±5pp）；`tst_price_position_52w` 档 `1=[0,20)` … `5=[80,100]`；`att_volume_surge` 为 `surge`/`normal`/`shrink`；`trd_momentum_12_1` 1=最弱。**L1 只有 3 个域用 `enum_values`，其余 6 域用 `TIER + value_range`**，故结论层必须按 `source_col` 明确取 `key_value` 还是 `num_value`。

## 附录 C：关键证据索引

> **引用格式**：以「文件 + 符号 / 章节」为主，行号仅作辅助（纯行号引用易随代码改动错位）。

| 主题 | 位置（符号 / 章节为主） |
| --- | --- |
| 12 域定义 / 蓝橙域 / 合规 / PIT 高危项 | `doc/stock profile hd.md` §4.2、§5.2、§9（辅助行号 `:192-209, 489, 729-744`） |
| 三层范式 / 层间铁律 / L3 准入 / 不可加性 | `doc/market_state_data_architecture.md` §1.1、§2.4（`:35-62, 274-299`）；`doc/market_profile.md` §5.0「三层之间的两条铁律」「L3 业务指标准入」（`:1377-1408`） |
| L2 五件套 / 出档门禁 / 口径变更纪律 | `build_dimension_value.py` 模块 docstring（`:23-37`）、`build_breadth_dimension`（`:300`）、`skip_null_updates=False`（`:506-509`）、`params_hash`（`:502`） |
| L3 状态机 / 防抖参数 / 非预测声明 / 证伪结论 | `build_business_value.py` `REGIME_PARAMS`（`:163-181`）、`REGIME_OUTPUT_SCHEMA.state_semantics`（`:190-195`）、`regime_state_machine`（`:288-314`）、`pattern_first_match`（`:672-678`）、pattern v1.32 remark（`:515-530`）、`REVERSAL_REMARK` v1.34（`:1341-1358`） |
| regime 字典表与 `params_hash`（**仅作对照，本层不采用**） | `build_business_value.py:107-126, 244-247, 969-1004`；`build_dimension_value.py:502` |
| `version` 写入链路（本层沿用） | `profiling/engine.py:357-367`（`tag_value.version`）、`:407-413`（`tag_run_log.version`）；`profiling/registry.py` `TagMeta.version`（`:85`）、`sync_registry()` 全列 upsert（`:276-284`） |
| 现有标签注册表 / enum 值域约束 | `profiling/registry.py` `TagMeta`（`:62-131`）、`_sync_enum_values`（`:219-253`）；`profiling/schema.py:26-135` |
| `profile.*` 表结构 / 唯一约束 / 版本化注释 / 索引 | `sql/schema.sql:1354-1462`（尤其 `:1421` version、`:1425` UNIQUE、`:1428` 版本化注释、`:1441-1442` `idx_tag_value_lookup` 前导列为 `tag_code`、`:1395-1399` `short_label`） |
| `financial_indicator` 表 / PIT 护栏 / TTM | `sql/schema.sql:771-812`（17 列）；`profiling/quantile.py:186-213, 254-387` |
| 采集端白名单 / 港股现金流量表 / FCF | `get_financial.py` `A_INDICATOR_MAP`（`:108-118`）、`_fetch_hk_cash_flow`（`:276-297`）、`_calc_hk_fcf` / `_calc_fcff`（`:300-326`）、全量入库主循环（`:495-523`） |
| 消费端读取 SQL / enum 值域翻译 / `as_of` 回放 / DAG 版本 | `stock-realtime/db.py` `VERSION_SOURCES`（`:56-76`）、`DATA_SOURCES` + `version_fn`（`:2976-2993, 6246, 6326-6330`）、`get_screen_meta`（`:8098-8183`）、`screen_query`（`:8475-8568`）、`_tag_pct`（`:8805-8826`）、`profile_stock_detail`（`:8888-8902`） |
| 位置档实现 / 6 阶段暂不注册 | `profiling/tags/trend.py:1-32, 121-220` |
| 计算任务资源规范 | `AGENTS.md`「计算任务资源约定」；`doc/compute_task_resource_standard.md` §1–§8（§2 为 R1–R7、§5 为 worktree 对比法、§7 为监控登记） |
| 结论切点标定 | `tools/calibrate_conclusion_cutpoints.py`（只读、不写库；切点已冻结，见 §4.3.1 / §5） |

## 附录 D：6 组 enum 值域冻结表（**唯一真相源**）

> 共 **35 个 code**。`label` 为中文短句、`short_label` 为短语（≤6 字），全部写进 `enum_values` 的 `(label, short_label)` 元组并同步到 `profile.enum_value`（§3.4 R5）。措辞已按 §11 复核。
> `code` / `label` / `short_label` 均为冻结值；中文措辞按 §11 禁用词表复核。

### D.1 `dim_valuation_conclusion`（7）

| code | label | short_label |
| --- | --- | --- |
| `rel_cheap` | 相对偏便宜 | 相对偏低 |
| `rel_fair` | 相对中性 | 相对中性 |
| `rel_expensive` | 相对偏贵 | 相对偏高 |
| `hist_cheap_ind_expensive` | 相对自身历史偏低、相对同行偏高 | 历史低同行高 |
| `hist_expensive_ind_cheap` | 相对自身历史偏高、相对同行偏低 | 历史高同行低 |
| `loss` | 亏损（无 PE 估值锚） | 亏损无锚 |
| `negative` | 净资产为负（无 PB 估值锚） | 净资产为负 |


### D.2 `dim_quality_conclusion`（6）

| code | label | short_label |
| --- | --- | --- |
| `solid` | 盈利稳且现金实 | 稳且现金实 |
| `high_but_volatile` | 盈利高但波动大 | 高但波动 |
| `paper_profit` | 利润高但现金转化弱 | 现金转化弱 |
| `leveraged` | 高杠杆支撑 | 高杠杆 |
| `weak` | 盈利水平偏弱 | 盈利偏弱 |
| `deteriorating` | 稳定性与真实性均偏弱 | 质量均偏弱 |

> `deteriorating` 的 label 只作横截面描述；L1 无时序档位，不得写「走弱」。

### D.3 `dim_growth_conclusion`（6）

| code | label | short_label |
| --- | --- | --- |
| `fast_sustained` | 高增速且持续 | 高且持续 |
| `fast_accelerating` | 增速抬升 | 增速抬升 |
| `fast_decelerating` | 增速放缓 | 增速放缓 |
| `slow_steady` | 低速但稳 | 低速稳 |
| `contracting` | 负增长 | 负增长 |
| `revenue_profit_divergent` | 增收不增利或反之 | 增收增利背离 |

### D.4 `dim_position_state`（6）

| code | label | short_label |
| --- | --- | --- |
| `low_quiet` | 低位缩量 | 低位缩量 |
| `low_volume_start` | 低位放量 | 低位放量 |
| `trend_continuation` | 趋势段内运行 | 趋势段内 |
| `high_overheat` | 高位过热 | 高位过热 |
| `downtrend_unresolved` | 下跌未止 | 下跌未止 |
| `range_bound` | 区间震荡 | 区间震荡 |


### D.5 `biz_value_quality_type`（5）

| code | label | short_label |
| --- | --- | --- |
| `quality_rel_cheap` | 盈利稳且价格相对偏低 | 稳且偏低 |
| `quality_rel_fair` | 盈利稳价格中性 | 稳且中性 |
| `quality_rel_expensive` | 盈利稳但价格相对偏贵 | 稳且偏高 |
| `value_trap_risk` | 相对偏便宜但盈利质量走弱（价值陷阱嫌疑） | 价值陷阱嫌疑 |
| `weak_and_expensive` | 盈利质量弱且价格相对偏贵 | 弱且偏高 |


### D.6 `biz_growth_quality_type`（5）

| code | label | short_label |
| --- | --- | --- |
| `sustained_cash_backed` | 增长持续且现金支撑 | 现金支撑 |
| `cash_burning_expansion` | 高增长但自由现金流为负 | 扩张耗现金 |
| `paper_growth` | 增长但利润现金转化弱 | 转化弱 |
| `one_off_like` | 疑似一次性因素（增速跳变、持续性弱） | 疑似一次性 |
| `contraction` | 收缩 | 收缩 |


---

*本文为个股画像 L2/L3 结论层的数据契约设计文档（v1.0）。涉及数据库结构的改动必须同步 `sql/schema.sql`（AGENTS.md 最高优先级约束）；新增/修改「读库→计算→写库」逻辑前须过 `doc/compute_task_resource_standard.md` §6 检查清单。*
