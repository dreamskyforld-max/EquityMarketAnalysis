# 个股画像 L2/L3 结论指标 · 数据契约设计文档

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.4（决策已全部拍板；P0 冻结 enum 值域与 JSONB schema 后升 v1.0，未实现） |
| 编制日期 | 2026-11-12 |
| 适用范围 | 个股画像已落地标签（③④⑤⑥⑦⑩ 等）之上的结论层 |
| 文档性质 | **数据架构 / 数据契约 + 口径规格**；不含实现代码、不建表 |
| 规格真源 | 本文 §3–§7（口径）/ §8（数据源）/ §10（验收） |
| 上位文档 | `doc/stock profile hd.md`（12 域设计，§4.2 / §4.3 / §9）、`doc/market_state_data_architecture.md`（三层范式，§1.1 / §2.3 / §2.4）、`doc/market_profile.md` §5.0 |
| 消费方 | `stock-realtime`（`/Users/fredhan/mkt/stock-realtime`），读 PostgreSQL `profile.*` |

> **范围声明**：本文档只讨论**数据架构的设计与落地**（分层、编码、落表、口径、算法、数据源、计算调度、验收）。**前端如何展示、域卡顺序、页面交互均不在本文档范围**，属前端设计开发任务。文中引用消费端代码仅用于确认「按什么数据契约读」，不分析展示形态。

---

## 0 阅读契约

> **领域边界**：⑧ 题材概念、⑪ 事件风险、⑫ 预测评分三个空域**不在本期范围**。⑫ 预测评分继续冻结——本层全部结论为**描述性判断**，不是预测评分。

> **字段单一职责（v0.3 定）**：规则契约用 `tag_registry.conclusion_dict`、判定依据用 `tag_value.conclusion_detail`、口径版本用 `version`。三者各守本职；**不引入 `params_hash` 之类的内容指纹，不把哈希串或规则内容塞进 `version`**。详见 §4.3 / §4.4。

> **版本即口径（v0.3 定）**：规则升级 = bump `@tag(version=...)`，由 `sync_registry()` 贯通到 `tag_registry.version`，再由结论生产者写进 `tag_value.version`。`version` 表达「该标签 / 该值行的口径版本」；对结论标签而言口径就是判定规则，因此规则版本是它的**本职**，不是复用。被否掉的是把 `params_hash` 哈希串当版本号用。

> **与 regime 三层的关系**：复用「L1 原子 / L2 维度 / L3 业务」的术语、分层纪律与「宁可不出档」的出档哲学（`build_dimension_value.py:23-26`）；**不复用其算法口径与 `params_hash` 字典机制**（regime 是一市场一日一行的时序 expanding 分位 + 标定脚本机器生成字典；个股是一票一日一行的横截面分位 + 代码显式定义规则）。

> **明确的非目标**（四条，任一条被突破即视为需求走偏）：
> 1. 不预测涨跌、不输出预期收益、不做方向概率；
> 2. 不输出买入 / 卖出 / 持有建议，不产出「合适 / 不合适」这类决策结论；
> 3. 不产出单一综合评分或可排序的总分（避免退回「打分排序」范式，`doc/stock profile hd.md:37-45`）；
> 4. 不引入预测模型（LightGBM / HMM / 蒸馏）与分钟级 `tick_data`（覆盖仅约 80 只）。

> **现状摘要（2026-11-12 核查）**：设计稿 12 域 / 123 标签；代码 `profiling/tags/` 只有 9 个域模块，注册 77 个标签、其中 **59 个 `active` 产出**、18 个 `planned_no_data`。`profile.tag_value` 是 EAV（`key_value` 分档/枚举 + `num_value` 连续值 + `confidence`），`profiling/engine.py:167 compute_tag()` 只做「算快照 → diff → 版本化写入」，**全仓无任何跨标签聚合、无 score / state / verdict 列**。

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
| D3 | **结论必须带依据** | 每条结论都要能回溯到命中的原子标签与规则（字段设计见 §4.3） |

### 1.3 时间尺度区分（易被忽略）

③ 估值、⑦ 交易特征、⑩ 位置是**日频**（随价变）；④⑤ 成长与质量是**季频**（财报驱动）。

> 数据契约里必须区分**档案结论**（慢变量、季度更新）与**今日状态**（快变量、日频）。把三者揉成一个「每日全量刷新」的结论，会出现「财报没出但结论天天跳」，直接摧毁可信度。各结论的 `update_freq` 按域分别声明（§5）。

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

1. **判定型结论从原子读数 / 原始量价算，不从被平均稀释的分档算。** 规则是条件合取，需要精确边界；先对多个分档取平均会丢掉边界信息（regime §5.0 铁律①同款，`doc/market_profile.md:1343-1346`）。故 L2 输入是**各个 L1 标签的原值**，不是「域内平均分」。
2. **横截面分位与时序位置不得混进同一字段。** 现有 L1 的 1–5 档是「同一天全市场/行业内排第几」（横截面），`tst_price_position_52w` 是「个股跟自己 252 日区间比」（时序）。两者语义不同，禁止相加或互相覆写（`profiling/tags/trend.py:1-16`）。

### 2.3 与 regime 的四条差异（**不可照搬**）

| # | 差异 | 后果 |
| --- | --- | --- |
| A1 | **分位基准**：regime L1 是时序 as-of expanding 分位（250/750 有效观测门槛）；个股 L1 是横截面等频五等分 | regime 的 `coverage` = 成员数据完整度、`dispersion_norm` = 「平均分是否有代表性」；个股层 `coverage` 必须重定义为「域内标签可得率 + 当日横截面有效样本量」，照抄公式无意义 |
| A2 | **成员齐备度**：⑦ 9/9、⑥ 6/6 齐；⑤ 4/8、⑨ 8/14、⑩ 1/7，⑧⑪⑫ 为 0 | regime 的 `DIM.FLOW` / `DIM.SENTIMENT` 因成员不齐被逼成「单锚代理分、恒不出档、恒低置信」（`build_dimension_value.py:874-895, 667-686`）。个股层会大面积出现同类降级，必须先定门禁，不能事后补 |
| A3 | **不可加性更严重**：regime §2.4 规则 7 已明令「六维分不可横向比较或求均值」 | ③ 估值 / ④ 质量 / ⑦ 交易 / ⑨ 资金是完全不同构念，**禁止套一个统一的「域内标签加权平均」模板**。每个结论必须单独声明聚合结构（状态机 / 条件合取 / 计数 / 描述量），与 `build_dimension_value.py` 按 `dimension_code` 分派独立生产者同款 |
| A4 | **验证代价**：regime 每个 L3 都是「原规格被实测推翻后重写」才落地（pattern v1.32 / divergence v1.33 / reversal v1.34 的 remark 全是「撤销原规格」） | 个股 × ~7,500 只 × 历史日的前瞻验证量与过拟合风险高得多；故 §10 明确**不以「前瞻收益显著分离」为上线门槛** |

---

## 3 消费端数据契约（决定结论必须长成什么样）

> 本章只写**按什么数据契约被读取**（决定落表形状），不讨论任何展示形态。

### 3.1 读取方式（2026-11-12 核查）

唯一 SQL 层是 `stock-realtime/db.py`：

| 契约 | 行为 | 位置 |
| --- | --- | --- |
| 筛选查询 | `conditions: {tag_code: [value_code]}` → `EXISTS(... key_value = ANY(%s) AND eff_to='9999-12-31')`；标签间 AND、同标签内 OR；支持 `as_of` 回放 | `db.py:8475-8568` |
| 单票全量标签 | `SELECT ... FROM profile.tag_value WHERE stock_code=%s AND eff_to='9999-12-31'`，按 `domain` 分组 | `db.py:8749-8797` |
| 元数据 / 值域 | `SELECT ... FROM profile.tag_registry WHERE status='active'`；`enum` 值域经 `JOIN profile.enum_value ON e.enum_type = split_part(r.value_ref, ':', 2)` 翻译 | `db.py:8098-8123, 8126-8169, 8771-8775` |
| 数据版本登记 | `VERSION_SOURCES` 登记 `profile.tag_value` / `tag_registry`，供下游感知数据变化 | `db.py:2976-2993, 6325-6330` |

### 3.2 由读取契约反推的四条硬约束

| # | 约束 | 依据 |
| --- | --- | --- |
| C1 | **结论必须是离散 `enum` 标签，落 `profile.tag_value`** | 现有 EAV 的 `value_type` 只认 `enum/bool/tier/float`（`profiling/registry.py:91-92`），无结论 / JSON 载体；自由文本或加权连续分要另起一套表与消费逻辑 |
| C2 | **`value_type=ENUM` + `value_ref='enum:<type>'` + `profile.enum_value` 中文值域** | `registry.py:100-113` 强制 ACTIVE enum 必须声明值域；`db.py:8114-8122` 用 `value_ref` 做 code→label 翻译 |
| C3 | **标签必须 `status='active'` 且有非空值域才进入筛选字典** | `db.py:8137-8141, 8158-8172`；未实现的结论用 `planned_no_data + blocked_reason` 注册，不得静默缺项 |
| C4 | **结论必须自带可追溯依据** | 现有 `tag_value` 无 JSONB 列，需新增 `conclusion_detail`（§4.3，已定） |

### 3.3 ⑫ 不是本期落点

⑫ 域在设计稿里定义为「模型输出、必须带置信度」（`doc/stock profile hd.md:207`），与本层「规则驱动的描述性结论」语义冲突。本层结论用**新 domain**（§4.1），不去占用 ⑫ 的域定义，避免语义混淆。

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

**命名依据**：`dim` = Dimension（维度指标）、`biz` = Business（业务指标），与 `doc/market_state_data_architecture.md:41-47` / `doc/market_profile.md:1325-1329` 的分层术语逐字一致；后缀 `_conclusion` / `_state` / `_type` 明确「这是结论，不是原子指标」。regime 侧是 `DIM.BREADTH`（大写点分、落在 `regime.dimension_value.dimension_code`），本层是小写下划线（落在 `profile.tag_value.tag_code`），不同表不同列，无冲突。

> **L1 编码不改**：59 个已上线标签的 `tag_code` 已被消费端按字面引用，重命名会破坏既有数据契约。分级通过**新增标签的编码前缀 + `source_type` + `parent_tag`** 体现，不回改 L1。

元数据约定（`@tag` 装饰器，`profiling/registry.py:62-131`）：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `value_type` | `ENUM` | 已拍板：结论是离散 enum |
| `value_ref` | `enum:<enum_type>` | 由 `enum_values` 内置值域自动生成（`registry.py:101-105`）；中文名进 `profile.enum_value` |
| `num_value` / `num_unit` | `NULL` / `NULL` | **结论不带连续分**。避免 `key_value` 与 `num_value` 语义打架 |
| `source_type` | `derived`（新增受控值） | 现有注释为 `rule/stat/model/external/manual`；`source_type` 是 TEXT 无 CHECK，新增值需在实现时同步注释。**计算接线按此字段识别结论标签**（§9），不依赖编码前缀 |
| `version` | **规则口径版本**（`v1` 起，升级时 bump） | 结论标签的口径就是判定规则；由 `sync_registry()` 同步到 `tag_registry.version`，由结论生产者写进 `tag_value.version`（§4.4）。**不放哈希串、不放规则内容** |
| `parent_tag` | 上游主标签的 `tag_code` | 用现有列表达层级，**无需加列** |
| `is_exclusive` | **`False`（建议）** | `profiling/tags/trend.py:186-191` 刻意把该标记留给 ⑩ 域的 6 阶段状态机 `tst_stage` 作域内识别信号；结论层改用 `source_type=derived` 识别，不占用它 |
| `confidence_req` | `False` | 结论是确定性规则，非模型类 |
| `confidence` | 恒 `1.0` | **不改现有语义**（`sql/schema.sql:1431`：规则/统计类恒 1.0，模型类为预测概率）。可得性一律走出档门禁 + `conclusion_detail.coverage`，不塞进 `confidence` |
| `pit_capable` | 逐结论声明 | 依赖财报的（④⑤）为 `False`（港股披露日无源，§8.5）；依赖量价的（③⑦⑩）为 `True` |
| `status` | `active` | 首期 6 个全部实现；若某项判定条件不可得，用 `planned_no_data + blocked_reason` 注册，不得静默缺项 |

#### 4.1.1 domain 的作用与取值

`tag_registry.domain` 是 `TEXT NOT NULL`（`sql/schema.sql:1360` 注释「所属域，如『证券属性』，不带设计文档里的域序号」）。它在数据契约里承担四种**键**的作用，不是一句自由文案：

| 作用 | 位置 | 说明 |
| --- | --- | --- |
| 分组键 | `stock-realtime/db.py:8137-8183` | `get_screen_meta()` 以 `ORDER BY domain, tag_code` 把标签组织成「域 → 标签 → 取值」三级字典 |
| 分组键 | `stock-realtime/db.py:8895, 8902` | `profile_stock_detail()` 按 `meta["domain"]` 把单票标签分组、算域内聚合 |
| 排序键 | `db.py:8047-8050, 8180-8182`（`_SCREEN_DOMAIN_ORDER` 9 域）、`db.py:8641-8645`（`_PROFILE_DOMAIN_ORDER` 12 域） | 两份**硬编码中文域名白名单**决定顺序；不在表内的域排末位 |
| 分类键 | `db.py:8647`（`_PROFILE_MODEL_DOMAINS`）、`_PROFILE_MINIBAR_DOMAINS`、`_PROFILE_DOMAIN_COLORS`、`_PROFILE_RADAR_DIMS` | 按中文 domain 名硬编码匹配，决定该域是否被标为模型域、是否进雷达等 |
| 过滤键 | `profiling/registry.py:165-167`、`profiling/run.py:157-159` | `by_domain()` 用 `startswith` 前缀匹配；`compute --domain 规模属性` 按域重算 |

> ⚠ 因此 **domain 的取值是被下游按字面字符串匹配的语义键**，改名/换语言会让上述白名单全部失配。这是数据契约事实，不是展示设计（前端如何渲染不在本文档范围）。

**是否需要中文名**：

| | 判断 |
| --- | --- |
| 现状 | 9 个已落地域全部是**中文业务域名**：证券属性 / 规模属性 / 估值水平 / 盈利质量 / 成长特征 / 股东回报 / 交易特征 / 资金关注度 / 趋势状态 |
| 本期结论 | **沿用中文**。改成英文编码会与三处中文白名单失配，且需要消费端联动改造，收益为零 |
| 架构判断 | 中文**不是语义必需**。更规范的做法是「稳定编码 + 中文显示名分离」——项目里 `tag_code`/`tag_name`、`enum_value.code`/`label` 已经是这个范式，而 `domain` 目前是编码与显示名合一的历史遗留。将来若需重命名或国际化，应新增 `domain_code` + 显示名映射，**不要直接改 domain 字符串** |

**domain 归属：方案 A（已定）**

v0.3 暂定的「域结论」「综合结论」是**技术层级名**，而现有 9 域是**业务域名**（描述「关于什么」），两者语义不同；层级信息已由 `tag_code` 前缀（`dim_`/`biz_`）+ `source_type='derived'` 表达，再塞进 `domain` 会让一个字段承担两种职责。故采用方案 A：

| 层 | 归属 | 说明 |
| --- | --- | --- |
| L2 四个 | `估值水平` / `盈利质量` / `成长特征` / `趋势状态` | 沿用各自业务域，结论与原子依据同域，靠前缀 + `source_type` 区分层级 |
| L3 两个 | **`综合研判`** | 唯一新建的中文业务域名；跨域结论没有单一业务归属 |

**实现登记**：`综合研判` 需补进 `stock-realtime/db.py` 的 `_SCREEN_DOMAIN_ORDER`（9 域）与 `_PROFILE_DOMAIN_ORDER`（12 域）两份白名单，否则落到「不在表内 → 排末位」的兜底分支；同时确认 `_PROFILE_MODEL_DOMAINS`（`{"趋势状态","事件风险","预测评分"}`）不应包含它——本层结论是规则驱动的描述性判断，不是模型输出。此条属**数据契约登记项**（白名单按字面匹配），不涉及展示设计。

> 补充事实：`db.py:8805-8826 _tag_pct()` 只对 `num_unit='percentile_0_100'` 或数字档位 `tier` 算分位，`enum` 标签返回 `None` 不进 `pcts`，因此结论标签放进现有业务域**不会污染域内分位计算**。

### 4.2 出档门禁（统一，逐结论可收紧）

> **核心纪律：不确定 → 不写行，不设 `insufficient` 枚举码。** 把「不知道」变成一个可检索的枚举值，等于让下游筛出一个确定的错误概念（regime `A8`「整行不发布」同款，`build_business_value.py:368-369`）。

| 门禁 | 判据 | 不通过时 |
| --- | --- | --- |
| G1 成员可得率 | `coverage = 有效输入成员数 / 应到成员数 >= 0.6`（**初值；实现时按覆盖率与出档率实测标定**，结果落在 `tag_registry.conclusion_dict.params`） | 不写行，`tag_run_log` 记 skipped |
| G2 关键输入存在 | 判定的主锚（如估值双分位、位置档）缺一即不发布 | 不写行，不用其它成员回填凑数 |
| G3 数据新鲜度 | 当日行情未到货 → `SkipTag`（`profiling/quantile.py:59-67`） | 整批不发布，不用前一日快照冒充 |
| G4 负 / 无效值 | 走**业务枚举码**而非丢弃：负 PE → `loss`，PB≤0 → `negative`（沿用 `profiling/tags/valuation.py:106` 口径） | 写业务码，原因写进 `conclusion_detail` |

`confidence` 恒 1.0；**可得性只写进 `conclusion_detail.coverage`**。§5 / §6 各表「依据键」即 `tag_value.conclusion_detail` 的键集合。

### 4.3 字段设计（**已定：职责不同 → 名称不同**）

> **规则契约与判定依据是两类不同性质的数据，用两个不同名字的字段各归其位**：
> - **规则契约**（这个标签「怎么判」）→ 对同一标签**按版本固定**，同一版本内每天相同 → 标签级 → `tag_registry.conclusion_dict`。
> - **判定依据**（这票这天「为什么判成这个值」）→ **逐票逐日不同** → 值级 → `tag_value.conclusion_detail`。
>
> 不使用 `params_hash` 之类的内容指纹；口径标识由 `version` 承担。

| 字段 | 表 | 粒度 | 职责（唯一） | 何时变化 |
| --- | --- | --- | --- | --- |
| `conclusion_dict JSONB` | `tag_registry` | 一标签一行 | **当前生效规则契约**（只存当前版，不存历史） | 仅规则升级（低频，触发全量重算） |
| `conclusion_detail JSONB` | `tag_value` | 一票一标签一生效区间一行 | **该行判定依据** | 每条结论都可能不同 |
| `version` | `tag_registry` | 一标签一行 | 标签口径版本（**结论标签 = 规则版本**） | 规则升级 |
| `version` | `tag_value` | 每行 | 该值行所依据的口径版本 | 规则升级后全量重算 |

> **两个新字段对 59 个 L1 标签均为 `NULL`**；`version` 两列保持现有语义（值由 `@tag` 的 `meta.version` 写入，`profiling/engine.py:357-367, 413`），无需改动写入层。

#### 4.3.1 `tag_registry.conclusion_dict`（当前生效规则契约）

```json
{
  "inputs": [{"tag_code": "val_pe_hist_pct", "role": "anchor"}, "..."],
  "params": {"coverage_min": 0.6, "divergence_gap": 2, "confirm_days": null},
  "rules": [
    {"rule_id": "VAL-01", "conditions": "...", "result": "rel_cheap"},
    {"rule_id": "VAL-02", "conditions": "...", "result": "hist_cheap_ind_expensive"}
  ],
  "output_schema": {
    "key_value_semantics": "相对估值位置的枚举类型；不是绝对贵贱、不是预测",
    "value_codes": {"rel_cheap": "相对偏便宜", "...": "..."}
  }
}
```

- **包含完整规则表**（`rules`：rule_id → 条件 → enum），不只是参数；这样「规则契约」是完整的，`conclusion_detail.rule_id` 才有可查的对应项。
- **不重复放版本号**：当前期望版本的唯一真相源是 `tag_registry.version`；不放 `history[]`（Q1 已定只存当前规则）。
- 落成一个 JSONB 列，**不新建独立字典表**（6 个结论、规则由代码显式定义，独立表是过度设计）。

#### 4.3.2 `tag_value.conclusion_detail`（该行判定依据）

```json
{
  "rule_id": "VAL-01",
  "as_of": "2026-11-12",
  "coverage": 0.83,
  "missing_inputs": ["val_fcf_yield_tier"],
  "evidence": {"pe_hist_pct": 18.4, "pe_ind_pct": 71.2, "tier_pe": "1", "tier_pb": "2"},
  "gates": {"G1": true, "G2": true, "G3": true, "G4": false}
}
```

- `evidence` 自包含：读到这一行即可独立复算「为什么判成这个值」，无需回查上游标签状态。
- **不放版本号**（`tag_value.version` 已是）、**不放规则全文**（`tag_registry.conclusion_dict` 已是）、**不放原始判定快照**（§4.4.3）。
- 该列随 `tag_value` 的 `eff_from/eff_to` 一起生效。

### 4.4 口径版本、重算与覆盖（**已定**）

#### 4.4.1 为什么用版本号，不用 `params_hash`

`params_hash` 是 regime 的模式：字典由 `tools/calibrate_*.py --write` **机器生成**，参数多、切点冻结，需要内容指纹自动检测「内容变了但版本没变」。个股层只有 6 个结论、规则由 `@tag` + 结论生产者代码显式定义、变更低频，且**规则升级本来就是触发全量重算的动作**——bump 版本号是流程起点，不是额外负担。

内容指纹唯一多出的能力是自动拦截「改了内容忘了 bump」，但它也检测不到「判定逻辑写在代码里、字典没变」的情况；该风险由发布流程 + 单测承担。故**去掉 `params_hash`**，用可读版本号。

#### 4.4.2 触发链路

```
规则变更（改 conclusion_dict 的 inputs / params / rules / output_schema）
  → bump @tag(version="v2")
  → sync_registry() 把 tag_registry.version 更新为 v2（registry.py:276-284 全列 upsert）
  → 全量重算历史：结论生产者按 as_of 逐日重算，直接覆盖
  → 覆盖时同步写 tag_value.version = tag_registry.version = "v2"
  → tag_run_log.version 记录本次重算版本
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

> 查询带 `tag_code` 实体过滤（走 `idx_tag_value_lookup` 前导列），符合 AGENTS.md R7。

**规则变更历史**不落库：`conclusion_dict` 只存当前版（Q1），历史规则由 git 提交记录 + `tag_run_log.version` 时间线追溯。

#### 4.4.3 全量重算的四条实现约束（与版本/hash 无关，必须遵守）

| # | 约束 | 依据 |
| --- | --- | --- |
| 1 | **按 `as_of` 取当时的 L1 版本**：`eff_from<=as_of AND eff_to>=as_of`，不得取 `eff_to='9999-12-31'` 的当前有效值，否则把后来修正过的数据用到历史日期 → 前视偏差 | `doc/stock profile hd.md:489` 高危项；`stock-realtime/db.py:8528-8533` 的 `as_of` 回放同款 |
| 2 | **直接覆盖走 `DELETE + INSERT`**，不用 `UPDATE key_value`：`UNIQUE(stock_code, tag_code, key_value, eff_from)`（`sql/schema.sql:1425`）把 `key_value` 纳入唯一键，新口径算出不同 enum 时会撞约束 | `sql/schema.sql:1425`；`profiling/engine.py:359-362` 的 upsert 可作写入参考 |
| 3 | **门禁不通过时旧结论必须关闭**（`eff_to`），否则残留旧口径 | `build_dimension_value.py:506-509` 的 `skip_null_updates=False` 教训 |
| 4 | **资源红线**：全量覆盖跨 `eff_from` 年份分区，须分批（按 `tag_code` / 分区）、单事务 ≤5 万行、独立 oneshot unit、实测峰值内存、登记监控埋点 | AGENTS.md 计算任务资源约定 R1/R3/R4/R6；项目 09-22 写爆发打满磁盘队列事故 |

> 结论标签不走 `engine.compute_tag()`（§9 已按 `source_type='derived'` 跳过），所以「直接覆盖」与 engine 的 `mode="version"`（关闭旧行 + 插新行）是两条独立路径，不冲突。但需在 `sql/schema.sql` 注释登记这一例外，避免与 L1「不覆盖旧行」的版本化语义混淆。

#### 4.4.4 不保留「复盘当时结论」

> **已定（业务判断）**：「复盘当时系统给了什么结论」这个场景不存在、无意义，**不需要考虑**。
>
> 因此：`as_of` 回放得到的是「用当前规则回算的历史值」，不是当时真实发布值；不设原始判定快照、不设规则历史表、不设双版本并存。对回测而言口径统一反而是优点（避免规则变更造成假收益）。

### 4.5 唯一真相源

| 信息 | 唯一家 |
| --- | --- |
| 标签元数据 / 口径 / 值域 | `profiling/registry.py` 的 `@tag` 装饰器 → `sync_registry()` 写 `tag_registry`（代码即字典） |
| enum 中文名 | `profile.enum_value`（一个概念 = 一个 code，禁止同义异名） |
| 结论值 | `profile.tag_value.key_value`（EAV + `eff_from/eff_to` 版本化） |
| **当前规则契约** | `tag_registry.conclusion_dict` |
| **该行判定依据** | `tag_value.conclusion_detail` |
| **当前口径版本** | `tag_registry.version`（由 `@tag.version` 同步） |
| **该行口径版本** | `tag_value.version`（由结论生产者写入） |

---

## 5 L2 维度指标规格（4 个）

> **统一骨架**：① 定义 / ② 输入清单 / ③ 判定规则 / ④ enum 值域 / ⑤ 门禁与频率 / ⑥ 依据键 / ⑦ 措辞约束。
> **输入一律为 L1 原子标签的 `key_value` / `num_value` 原值或 `v_daily_quote` 原始量价，不使用「域内平均分」**（§2.2 铁律①）。
> 章节标题即正式 `tag_code`；各表「依据键」为 `tag_value.conclusion_detail` 的键集合。
> 所有值域用 `enum_values` 内置声明，`label` 为中文短句、`short_label` 为短语。

### 5.1 `dim_valuation_conclusion` 贵不贵

| 项 | 内容 |
| --- | --- |
| 回答 | 相对自身历史与同行，价格处在什么位置；双分位是否一致 |
| 输入 | `val_pe_ttm_tier`、`val_pb_tier`、`val_ps_ttm_tier`、`val_pcf_ttm_tier`、`val_peg_tier`、`val_fcf_yield_tier`（横截面档）；`val_pe_hist_pct`、`val_pe_ind_pct`（历史 / 行业分位）；`val_pe_ttm_tier` 的 `LOSS` / `NEGATIVE` 枚举 |
| 判定 | 双分位同向且同档 → 相对档；双分位背离 → 单列背离码；`LOSS/NEGATIVE` 优先于一切分档；FCF 收益率作校验项，只调 `evidence`，不改结论 |
| enum 值域（草案） | `rel_cheap` 相对偏便宜 / `rel_fair` 相对中性 / `rel_expensive` 相对偏贵 / `hist_cheap_ind_expensive` 历史便宜但同行偏贵 / `hist_expensive_ind_cheap` 历史偏贵但同行便宜 / `loss` 亏损无 PE / `negative` 净资产为负 |
| 门禁 / 频率 | G1 + G2（双分位至少一个可得）；`daily`；A+H；`pit_capable=True` |
| 依据键 | `rule_id`、`as_of`、`pe_hist_pct`、`pe_ind_pct`、`tier_pe`、`tier_pb`、`fcf_yield`、`coverage`、`missing_inputs`、`gates` |
| 措辞 | **禁用「低估 / 高估 / 便宜 / 该买」**，一律「相对偏…」；`compute_logic` 必须写明「横截面分位不等于绝对贵贱」 |

### 5.2 `dim_quality_conclusion` 好不好

| 项 | 内容 |
| --- | --- |
| 回答 | 盈利的水平、稳定性、真实性与杠杆结构 |
| 输入 | `qal_roe_tier`、`qal_gross_margin_tier`、`qal_net_margin_tier`、`qal_leverage_tier`、`qal_roe_stability`、`qal_earnings_quality`、`qal_profit_years`；补数后增强 `roic` / 周转率（只进 evidence，§8.4） |
| 判定 | 水平（ROE / 毛利率）× 稳定性 × 真实性（OCF/净利）三腿合取；杠杆单独降级；连续盈利期数作韧性证据 |
| enum 值域（草案） | `solid` 盈利稳且现金实 / `high_but_volatile` 盈利高但波动大 / `paper_profit` 利润高但现金转化弱 / `leveraged` 高杠杆支撑 / `weak` 盈利水平偏弱 / `deteriorating` 稳定性或真实性走弱 |
| 门禁 / 频率 | G1 + G2（`qal_roe_tier` 与 `qal_earnings_quality` 至少一个）；`quarterly`（财报驱动）；A+H；`pit_capable=False` |
| 依据键 | `rule_id`、`as_of`、`roe_tier`、`roe_stability`、`ocf_to_profit`、`leverage_tier`、`profit_years`、`coverage` |
| 措辞 | 禁用「好公司 / 优质 / 白马」；「盈利稳且现金实」描述的是**财报特征**，不是「证券好不好」 |

### 5.3 `dim_growth_conclusion` 快不快

| 项 | 内容 |
| --- | --- |
| 回答 | 增速、持续性、加速度，以及增收与增利是否背离 |
| 输入（**判定用**） | **仅两市场共有的 4 个 active 标签**：`grw_revenue_yoy_tier`、`grw_profit_yoy_tier`、`grw_growth_persistence`、`grw_growth_accel`；外加营收/净利增速档的**背离** |
| 输入（**仅 evidence**） | 新补的扣非增速（仅 A 股）、研发强度（仅 A 股）、CAPEX 强度（仅港股）、并购现金流（仅港股）、商誉（仅 A 股）——见 §8 |
| 判定 | 增速档 × 持续性 × 加速度 → 状态；营收档与净利档背离 ≥2 档 → 单列背离码 |
| enum 值域（草案） | `fast_sustained` 高增速且持续 / `fast_accelerating` 增速抬升 / `fast_decelerating` 增速放缓 / `slow_steady` 低速但稳 / `contracting` 负增长 / `revenue_profit_divergent` 增收不增利或反之 |
| 门禁 / 频率 | G1 + G2；`quarterly`；A+H；`pit_capable=False` |
| 依据键 | `rule_id`、`as_of`、`rev_yoy`、`np_yoy`、`persistence`、`accel_pp`、`divergence_gap`、`evidence_enhancements[]`、`market_scope`、`missing_inputs` |
| **⚠ 关键约束** | **A/H 可用成员集不同**（§8 探测结论）。若把新补的 A 股扣非 / 港股 CAPEX 放进判定，同一 enum 在两市场语义会不一致——正是 regime `DIM.FLOW` 单锚代理分的陷阱（§2.3 A2）。故新补数据**一律只进 evidence**，判定只用共有 4 标签。此约束不得因「数据补上了」而放宽 |

### 5.4 `dim_position_state` 当前位置与状态

| 项 | 内容 |
| --- | --- |
| 回答 | **当前处于什么位置 / 状态**（滞后描述），替代口语的「现在买合不合适」 |
| 输入 | `tst_price_position_52w`（时序位置，已有 `pit_capable=True`）、`trd_momentum_12_1`、`trd_reversal_1m`、`trd_max_drawdown`、`trd_turnover`、`att_volume_surge`、`att_turnover_surge_tier`；**从 `v_daily_quote` 现算**：MA20/MA60、60 日量能倍数、局部极值（可移植 `support_resistance.py:99-174 daily_structural` / `kline_structure.py:109-123 detect_swings` 的**日线子算法**） |
| 判定 | **确定性条件合取 + 稳定性处理**（§7），不做加权打分、不用方向概率 |
| enum 值域（草案） | `low_quiet` 低位缩量 / `low_volume_start` 低位放量启动 / `trend_continuation` 趋势中继 / `high_overheat` 高位过热 / `downtrend_unresolved` 下跌未止 / `range_bound` 区间震荡 |
| 门禁 / 频率 | G1 + G2（位置档必须存在）+ G3 新鲜度；`daily`；A+H；`pit_capable=True` |
| 依据键 | `rule_id`、`as_of`、`position_52w`、`ma_alignment`、`volume_ratio_60`、`momentum_tier`、`turnover_surge`、`dwell_days`、`status`、`candidate{to,progress}`、`evidence_flags` |
| **⚠ 声明（必须逐字落入 `compute_logic`）** | 「**描述性滞后状态，不是方向预测、不构成买卖建议**」；照抄 `REGIME_OUTPUT_SCHEMA.state_semantics`（`build_business_value.py:190-195`）的写法 |
| 覆盖标注 | 两融 / 沽空 / 南向拥挤标签只覆盖子集（`daily_margin_balance` A 股 4,597 只 / `daily_short_selling` 869 只 / `daily_ggt_hold` 627 只，`profiling/tags/attention.py:8-16`），只写 `evidence_flags`，**不进判定** |

> **不采用 6 阶段 `tst_stage`**：`profiling/tags/trend.py:18-22` 明确「待建：需 t+20/t+60 前瞻收益验证闭环，短期无法收敛，暂不注册」。本结论是**降维的 6 档描述状态**，不是那套状态机的替代品，也不占用 `tst_stage` 的编码。

---

## 6 L3 业务指标规格（**首期 2 个，已定**）

### 6.0 准入三判据（对齐 `doc/market_profile.md:1352`）

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
| 输入 | `dim_valuation_conclusion.key_value` × `dim_quality_conclusion.key_value`（只读两个 L2 的枚举码，不重算原子） |
| 判定 | 交叉矩阵（3 类估值 × 3 类质量）→ 6–8 个类型码；任一侧未出档 → 整行不发布 |
| enum 值域（草案） | `quality_rel_cheap` 盈利稳且相对不贵 / `quality_rel_fair` 盈利稳价格中性 / `quality_rel_expensive` 盈利稳但价格偏贵 / `value_trap_risk` 相对便宜但质量走弱（价值陷阱嫌疑）/ `weak_and_expensive` 质量弱且价格偏贵 / `loss_no_pe` 亏损无估值锚 |
| 频率 | `daily`（因估值侧日频）；`pit_capable=False`（质量侧财报无港股披露日） |
| 依据键 | `rule_id`、`as_of`、`valuation_code`、`quality_code`、`matrix_cell`、`cross_check{ocf_to_profit, pe_hist_pct}`、`coverage` |
| 措辞 | 禁用「推荐 / 优选 / 值得买 / 评级」；`value_trap_risk` 写「嫌疑」，不作认定 |

### 6.2 `biz_growth_quality_type` 增长质量

| 项 | 内容 |
| --- | --- |
| 信息增量 | `dim_growth_conclusion` 只说「快不快」，说不出「是不是真增长」；交叉现金流 / 盈利真实性 / FCF 符号才产生该判断 |
| 输入 | `dim_growth_conclusion.key_value` × `qal_earnings_quality` × `financial_indicator.free_cash_flow` 符号（现有字段，两市场均有，但港股为 OCF−CAPEX 推算、A 股为每股 FCFF 推算，**口径不同，只取符号不取数值**） |
| 判定 | 增速状态 × 现金转化 × FCF 符号 → 类型码；背离优先于增速 |
| enum 值域（草案） | `sustained_cash_backed` 增长持续且现金支撑 / `cash_burning_expansion` 高增长但自由现金流为负 / `paper_growth` 增长但利润现金转化弱 / `one_off_like` 增速跳变而持续性弱 / `contraction` 收缩 |
| 频率 | `quarterly`；`pit_capable=False` |
| 依据键 | `rule_id`、`as_of`、`growth_code`、`ocf_to_profit`、`fcf_sign`、`persistence`、`accel_pp`、`evidence_enhancements[]` |
| 措辞 | 禁用「可持续增长 / 真成长 / 未来增速」；`one_off_like` 写「疑似」，不作认定 |

> **L3 首期只做 2 个（已定）**。不做「总分 / 综合评级 / 拥挤度预测」——分别违反 §0 非目标 3 与 1。

---

## 7 算法与模型选型

### 7.1 选型结论

> **确定性条件合取 + 稳定性处理；零预测模型、零 tick 数据、零前瞻收益优化。**

| 组件 | 选用 | 来源 |
| --- | --- | --- |
| 判定核心 | 条件合取规则（AND / 优先级首个命中） | `build_business_value.py:672-678 pattern_first_match` 同款；§2.2 铁律① |
| 稳定性处理 | **首期：`state` = 当日裸判 + 游程标注（`since_date` / `dwell_days` / `status` / `candidate`）**，见 §7.1.1 | `build_business_value.py:800-823`（pattern v1.32）、`:1563-1573`（reversal v1.34） |
| 强度与方向解耦 | 强度量只作**门槛**不作结论；方向腿直接读原子原值 | `build_business_value.py:256-267`；`tools/calibrate_trend.py:17-21` |
| 可解释性 | 每条结论带 `rule_id` + 命中输入原值 + `coverage` + 缺失成员，写进 `conclusion_detail` | `REGIME_OUTPUT_SCHEMA.detail_keys`、`build_business_value.py:393-405` |
| 出档门禁 | 「宁可说不确定，不给确定的错结论」 | `build_dimension_value.py:23-26` |

#### 7.1.1 什么是「防抖」，`regime 5/20` 又是什么

结论逐日计算。若每天独立用当日数据裸判，日线噪声会让 enum 在相邻两日来回跳（如「低位缩量」→「低位放量启动」→「低位缩量」），数据上表现为**日翻转率过高、结论不可信**。防抖 = 给判定加**状态记忆（滞回 / hysteresis）**，让新状态必须持续足够久才允许切换。

regime 的 `BIZ.MARKET_REGIME` 用的是**状态机改值**（`build_business_value.py:288-314 regime_state_machine`）：

- `dwell` = 当前状态已停留交易日数；`streak` = 候选状态连续出现天数；`candidate` = 正在挑战的新状态；
- 换态条件（`:307-309`）：`dwell >= min_dwell` **且** `streak >= confirm_days`；
- 三个参数在 `REGIME_PARAMS`（`build_business_value.py:163-181`）：
  - `REGIME_DEFAULT_CONFIRM_DAYS = 5`（`:170`）→ 新方向须连续出现 **5 天**；
  - `REGIME_DEFAULT_MIN_DWELL = 20`（`:171`）→ 当前状态至少已停留 **20 天**；
  - `REGIME_DEFAULT_SCORE_GATE = 30`（`:169`）→ 趋势强度 ≤30 强制判「震荡」。

**这就是此前所称的「regime 5/20」：两个滞回参数，不是指标阈值，也不是模型超参。**

**为什么个股层不直接沿用 5/20**：

1. regime 是 2 个市场 × 30 年 ≈ 1.1 万行、状态切换次数期望很低；个股是 ~7,500 只 × 多年，标的数多 3 个数量级，且个股日线噪声远大于市场指数。
2. **regime 自己已证伪状态机对稀有档的适用性**：`BIZ.MARKET_PATTERN` v1.32 撤销状态机、改为「`state` 恒当日 raw + 游程标注」，原因是 `switch_confirm_days>=5` 会压死稀有形态（`build_business_value.py:515-530` 的 remark）；`BIZ.REVERSAL_RISK` v1.34 同款（`REVERSAL_REMARK:1341-1358`）。个股 6–8 档里若有稀有档，状态机可能让它们永不出现。
3. §10 已有「日翻转率 <5%」验收线可直接度量稳定性，不必先靠状态机改值。

**已定**：首期采用**裸判 + 游程标注**（单日幂等、任意日期可独立复算，符合 PIT）；若实测日翻转率不达标，再引入 `confirm_days` / `min_dwell` 滞回并按个股数据重新标定，**不直接抄 5/20**。

### 7.2 明确不采用的路线（附证据）

| 路线 | 不采用理由 |
| --- | --- |
| 6 阶段状态机 `tst_stage` | `profiling/tags/trend.py:18-22`：「需 t+20/t+60 前瞻收益验证闭环，短期无法收敛，暂不注册」 |
| HMM / Markov-Switching | `doc/market_profile.md:2440,3270-3275`：「HMM 在市场层的适用性优于个股层…规则为主 + HMM 交叉验证」；二期可选（M9），首期不引入 |
| LightGBM 蒸馏 / 监督预测 | `doc/stock profile hd.md:526-558` 的蒸馏路线以「各阶段前瞻收益显著分离」为前提，而项目已在个股与市场两层反复证明该前提不成立（§1.2 D1） |
| `trend_segmenter` ZigZag+PELT / `phase_analysis` 5 态 / `force_dynamics` 相位 / `kline_structure` / Volume Profile | 全部建立在分钟级 `trend_snapshot` + `tick_data`，默认单票或约 80 只 `collect_tick` 池（`build_force_rounds.py:3,253`、`profiling/tags/attention.py:12,365-366`：`fund_flow_daily` 仅 80 只、「全市场建标签会让 99% 的股票无值」）。**仅日线局部极值 / 枢轴 / 均线子算法可移植** |
| `predictor_scan` / `direction_backtest` / `ofi_short_horizon` | 均为已证伪的预测因子扫描（AUC 0.44–0.54 / ~0.5，`predictor_scan.py:13-17`、`ofi_short_horizon.py:145-148`） |
| 以「各档前瞻收益显著分离（p<0.05）」为上线门槛 | `build_business_value.py:203-206`、`doc/market_profile.md:3154`：实测上行/下行分不开、下行反而最高；该门槛过不了，且会把项目拖回「标签准不准」的无限循环 |
| `params_hash` 内容指纹 | 见 §4.4.1：regime 的标定脚本机器生成字典才需要；个股层 6 个结论、规则由代码显式定义，用可读 `version` 即可 |

### 7.3 数据层约束（实现时必须遵守）

| # | 约束 |
| --- | --- |
| 1 | 只用 `v_daily_quote`（A 股 1990-12 起 5,711 只 / 港股 2019-01 起 2,831 只，`doc/market_profile_collection_plan.md:82-83`）；**不碰 `tick_data`** |
| 2 | `high_52w/low_52w` 在 `a_daily_quote` 历史覆盖≈0，**必须按 252 交易日自回算**（`profiling/tags/trend.py:30-32,121-183` 已有实现，含脏数据过滤与 500 只/批流式加载） |
| 3 | `daily_trend`（MA/MACD/RSI）只跑 `collect_trend` 采集池（`market_scheduler.py:92-103`），**均线一律自己按 close 现算** |
| 4 | 港股 `turnover_rate` 2026 年前为空，需自算；估值/市值仅 2018 起、退市股留存极少，须标幸存者偏差 |
| 5 | 复用 `profiling/tags/technical.py:43-50,209-255` 的按股票分批 + dtype 瘦身框架，不要一次性拉全市场（AGENTS.md 计算任务资源约定 R1） |

---

## 8 数据源与补数计划

### 8.1 2026-11-12 AKShare 字段探测结论（只读实测，未改任何文件）

`financial_indicator` 是指标摘要表（`sql/schema.sql:771-792`，14 列），**无三表明细表**。采集端 `get_financial.py` 用 AKShare：港股 `stock_financial_hk_analysis_indicator_em` + 现金流量表、A 股 `stock_financial_abstract`（新浪长表）、披露日 `stock_yjbb_em`（仅 A 股）。

| 缺失标签 | 上次判断 | 探测结论 | 建议 |
| --- | --- | --- | --- |
| `grw_non_gaap_yoy_tier` 扣非增速 | 无源 | **A 股可得**：新浪长表 80 行指标中第 4 / 53 行是「扣非净利润」，实测有值（茅台 2026Q3 643.2 亿、2025 年报 822.9 亿；分众 2026Q3 35.9 亿）。同花顺 `stock_financial_abstract_ths` **直接给「扣非净利润同比增长率」**，0.4 秒/只、104 期。**港股无源**（该接口传港股代码抛错） | **第一批** |
| `grw_capex_intensity_tier` 资本开支强度 | 港股部分可得 | **港股年报口径 100% 可得**：`_fetch_hk_cash_flow`（`get_financial.py:276-297`）已抓「购建固定资产」「购建无形资产及其他资产」的 `AMOUNT`，只在内存算 FCF 未落库；实测腾讯 96 期中 38 期有值，**25 个年报期 25/25 全覆盖**（2025 年报 874.8 亿）。A 股无 CAPEX 分解 | **第一批** |
| `grw_rd_intensity_tier` 研发强度 | 无源 | **仅 A 股可得**：东财 `stock_profit_sheet_by_report_em`（203 列）含 `RESEARCH_EXPENSE` / `RESEARCH_EXPENSE_YOY`，最新期有值（茅台 2026Q3 1.48 亿 / +30.6%），非空 37/104 期（早期会计准则未单列），附 `NOTICE_DATE` 披露日。**3.3 秒/只、21 次分页**，全 A 股约 5.2 小时/轮。港股利润表 32 科目 / 资产负债表 71 科目 / 分析指标 36 列**均无研发** | **第二批**（需独立 unit） |
| `grw_source` 成长来源 | 完全无源 | **部分可得（降级）**：港股现金流量表有「收购附属公司」（年报 19/25 有值，腾讯 2025 年报 361.9 亿）、「购买子公司少数股权而支付的现金」；A 股新浪长表有「商誉」（分众 1.72 亿）。**是现金流/商誉代理，不是并购公告解析** | 只进 evidence 或第三期，不宣称「识别并购驱动」 |
| 员工规模增速 | 静态值无时序 | `company_profile.employee_count` 有 A+H 静态水平值但覆盖式 upsert 不产生历史，增速不可算；连 `planned_no_data` 都未注册 | **放弃** |

**顺带零成本可得**（同一批未映射行，对 ④③ 域也有用）：`投入资本回报率`（ROIC）、`净资产收益率_平均_扣除非经常损益`、`存货/应收/总资产周转率`、`经营活动净现金/归属母公司的净利润`、`每股息税前利润` / `息税前利润率`；港股侧 `ROIC_YEARLY`、`加:折旧及摊销`（年报 19/25）、资产负债表 `现金及等价物` / `短期贷款` / `长期贷款`。

### 8.2 批次与采集成本

| 批次 | 内容 | 新增请求成本 | 落地方式 |
| --- | --- | --- | --- |
| **P1（第一批）** | 扣非增速、CAPEX 强度、④ 域 ROIC / 周转率增强 | **零**：新浪长表与港股现金流量表都在现有「财务指标全量」每周一 02:00 任务里已采（`market_scheduler.py:242-249`），只需扩 `A_INDICATOR_MAP` 白名单 + 加列落库 | 改 `get_financial.py` + `sql/schema.sql` + `profiling/quantile.py` 显式 SELECT |
| **P2（第二批）** | A 股研发强度 | 东财利润表 3.3s/只 ≈ 5.2h/轮、21 次分页/只 | **必须独立 systemd unit**（AGENTS.md R2：禁止塞进 `market_scheduler`），低频（季频足够） |
| **P3（可选）** | 并购代理 | 港股零成本、A 股零成本 | 只写 evidence |
| 放弃 | 员工增速 | — | 维持 `blocked_reason` |

### 8.3 实现时的 schema 变更清单（**本版不改，仅登记**）

1. `financial_indicator` 新增列（扣非净利润 / 商誉 / ROIC / 周转率 / CAPEX 两项 / 并购现金流 / 研发费用），**同步 `sql/schema.sql` 与 `profiling/schema.py`**（AGENTS.md 数据库结构变更约定，最高优先级）；
2. `profiling/quantile.py` 的 `load_financial_latest` / `load_financial_history` / `load_financial_ttm` / `load_financial_context` **全是显式列 SELECT**（`:202-204,233-235,287,303-304,317-318,352-366,410-431`），新列不会自动进入标签底表，必须逐处同步；
3. 若做「扣非净利 TTM 同比」，需扩展 `load_financial_ttm`（现仅覆盖营收 / OCF / 净利，公式 `TTM = 本期累计 + 上年全年 − 上年同期累计`）或直接用同花顺现成同比；**禁止再把累计口径 `SUM(近4期)`**（`profiling/quantile.py:16-20` 记录过分母虚高 2.4 倍的事故）；
4. **`tag_registry` 新增 `conclusion_dict JSONB`、`tag_value` 新增 `conclusion_detail JSONB`**（§4.3）；两处 DDL 同步 `sql/schema.sql` + `profiling/schema.py`；`tag_value` 是分区表，加列会自动传播到各分区，需在注释登记「结论标签直接覆盖」这一例外（§4.4.3）；
5. **不新建独立字典表、不新建 evidence 侧表、不新增 `version` / `rule_version` / `params_hash` 列**；`version` 两列语义保持现状（`meta.version` 写入）；
6. 本设计**不引入任何新第三方依赖**，`requirements.txt` 无需改动。

### 8.4 补数的使用纪律（§5.3 的延伸）

> 新补字段**一律只进 `evidence`，不进 L2/L3 判定输入**，直到满足两个条件才允许放宽：① 两市场都有等价字段；② 该字段的覆盖率通过 G1 门禁并写进 `market_scope`。
>
> 理由：A 股有扣非/研发/商誉，港股有 CAPEX/并购现金流，成员集不对称；放进判定会让同一个 enum 在 A/H 上语义不同（§2.3 A2）。

### 8.5 PIT 约束

- `financial_indicator` 按 `report_date`（报告期）入库；`announce_date` **仅 A 股有且覆盖 96.5%**，港股恒 NULL（`get_financial.py:194,223`）；A 股回填存在「历史行普遍后移约 1 年、间隔中位数 392~475 天」污染（`profiling/quantile.py:207-209`）。
- 故所有依赖财报的结论 `pit_capable=False`、`update_freq=quarterly`，**不用于严格回测**；`profiling/tags/growth.py:12` 注释「全部标签 pit_capable=False（财报无披露日）」与现状已不一致，实现时顺手校正文档注释。
- 东财利润表的 `NOTICE_DATE` 可补齐 A 股披露日（P2 顺带），港股披露日仍无源。
- ⚠ 全量重算历史时仍须按 `as_of` 取当时的 L1 版本（§4.4.3 约束 1），避免把修正后数据用到历史日期。

---

## 9 计算与调度

### 9.1 接线

```
system/compute-profile.timer (Mon-Fri 17:30, oneshot / MemoryMax=512M)
  → compute_profile.run()
       ① registry.sync_registry()          写 tag_registry / enum_value（含 6 个结论标签元数据
                                           与 version；conclusion_dict 由规则定义同步）
       ② 逐标签独立事务循环               只算 59 个 L1 原子标签
       ③ 【新增】conclusion_producer       读 profile.tag_value + v_daily_quote
                                           → 算 4 个 L2 + 2 个 L3
                                           → 写回 tag_value（含 conclusion_detail + version）
```

| # | 约束 |
| --- | --- |
| 1 | **结论标签不能走 `engine.compute_tag()`**：它是单标签独立事务、输入为 `(conn, tag_code, as_of)`，而结论依赖多个上游标签。`compute_profile.py:113-121` 的循环必须**按 `source_type='derived'` 跳过**结论标签，交由 ③ 单独处理。按元数据识别而非编码前缀 |
| 2 | ③ 必须在 ② **全部完成并提交之后**跑，否则读到半批数据。顺序由 `compute_profile._compute()` 编排，不用 DAG 框架 |
| 3 | ③ 内部顺序：先 4 个 L2，再 2 个 L3（L3 只读 L2 的枚举码，不重算原子） |
| 4 | **不得迁入 `market_scheduler` 进程内**：画像已因内存-IO 雪崩迁出独立 oneshot（`doc/compute_task_resource_standard.md`）。结论生产者接在 `compute-profile.service` 之后或同链路第二段 |
| 5 | 遵守 AGENTS.md 计算资源红线：大表查询带实体过滤 / 时间下界（R7）、按股票分批（R1）、单事务 ≤5 万行（R3）、上线前实测峰值内存（R4）、登记监控埋点（R6，`collection_task_log` 或 `tag_run_log`） |
| 6 | **版本写入**：③ 覆盖每行时必须写 `tag_value.version = tag_registry.version`（当前口径版本）；`tag_run_log.version` 同源。这是 §4.4.2 检测漏重算残留的前提 |
| 7 | **覆盖方式**：直接覆盖走 `DELETE + INSERT`（按 `(stock_code, tag_code, eff_from)`），不用 `UPDATE key_value`；门禁不通过时关闭旧行的 `eff_to`，不残留旧结论（§4.4.3 约束 2/3） |
| 8 | **历史输入**：重算历史日期时按 `eff_from<=as_of AND eff_to>=as_of` 取当时的 L1 版本，不取当前有效值（§4.4.3 约束 1） |
| 9 | 幂等：同日重复跑得到同一结果；`eff_from/eff_to` 版本化语义保持（结论标签为「直接覆盖」例外，已在 schema 注释登记） |

---

## 10 验证与验收

### 10.1 效标（**描述性效标，不以预测收益为门槛**）

| 指标 | 目标 | 说明 |
| --- | --- | --- |
| 覆盖率 | 分市场报：L2 各结论 A/H 分别 ≥80%；L3 ≥70% | 成员不齐的域允许更低，但必须写进 `blocked_reason` / `conclusion_detail.missing_inputs`，不得静默 |
| 稳定性 | **日频结论单日翻转率 <5%** | 对齐 `doc/stock profile hd.md:698`（日频标签 <5%/日）；§7.1.1 稳定性处理以此为验收线 |
| 状态游程 | 报告 `dwell_days` 分布与状态切换次数 | 防「每天换一个结论」；参照 `build_business_value.py:420-429` |
| 分布合理性 | 各 enum 值域的样本占比，报告是否有 0% 档 / 单档 >80% | 参照 regime `zero_states` / `zero_bands` 统计 |
| 口径一致性 | 同一 enum 在 A/H 语义一致；`conclusion_detail.coverage` 与门禁判定可复算 | §8.4 |
| 版本一致性 | 全量重算后，`tag_value.version` 全部等于 `tag_registry.version`；§4.4.2 残留查询返回 0 行 | 版本即口径（§0） |
| 人工抽检 | 抽 30 只（A/H 各半、含次新股 / 亏损股 / ST / 停牌复牌）核对结论与依据是否自洽 | 规则类验收（设计稿 §8.2） |
| 可解释 | 每条结论都能由 `conclusion_detail.rule_id` + `evidence` 复算出同一个 enum | 防「结论与依据对不上」 |

### 10.2 明确不做

- **不做**「各档 t+20/t+60 前瞻收益显著分离（p<0.05）」作为上线门槛（§7.2）；
- 若做前瞻收益，只能作为「样本内、重叠窗口、未扣成本、仅描述性」的**披露项**，不得写进验收结论或对外宣称可交易；
- 不做跨结论横比、不做雷达汇总、不做「综合分」；
- **不做「复盘当时系统给了什么结论」**（§4.4.4 已定：场景不存在、无意义）。

### 10.3 防回退测试（实现时补）

至少覆盖：门禁不通过不写行 / 旧结论被正确关闭 / 负 PE 走 `loss` 不回填分档 / A/H 成员集不对称时 evidence 不进判定 / `version` 升级后全量重算且残留查询为 0 / 重算按 `as_of` 取 L1 当时版本（防前视偏差）/ 直接覆盖不违反 `UNIQUE(stock_code,tag_code,key_value,eff_from)` / `conclusion_dict` 与 `conclusion_detail` 各守本职、无同名混用 / 6 阶段 `tst_stage` 编码未被占用。

---

## 11 合规与措辞

> 设计稿 §9 风险 8 / `:744`：系统定位为「内部研究工具 / 信息聚合」，**不输出买卖建议**；对外服务需明确免责与资质边界。

| 禁用 | 允许 | 理由 |
| --- | --- | --- |
| 低估 / 高估 / 便宜 / 贵 | 相对偏便宜 / 相对偏贵（含参照系） | 横截面分位 ≠ 绝对贵贱 |
| 好公司 / 优质股 / 白马 | 盈利稳且现金实 / 利润现金转化弱 | 描述财报特征，不评价证券 |
| 买点 / 卖点 / 合适 / 不合适 / 建议 | 低位缩量 / 高位过热 / 当前位置与状态 | 滞后描述，非建议 |
| 推荐 / 优选 / 评级 / 打分 | 交叉类型标签（含「嫌疑」「疑似」） | 不做排序与预测 |
| 未来增速 / 可持续增长 / 预期收益 | 增速持续 / 增速放缓 / 现金转化弱 | 现状判断，非预测 |

**每个结论标签的 `compute_logic` 与 `enum_value.label` 必须遵守上表，且 `compute_logic` 含一句**：「本结论为描述性判断，不构成投资建议，不预测未来价格走势」。

---

## 12 决策记录（**全部已拍板，无待决策项**）

| 议题 | 结论 | 章节 |
| --- | --- | --- |
| 标签编码 | **A 方案**：`dim_*` / `biz_*`，6 个正式 `tag_code` | §4.1 |
| domain 归属 | **方案 A**：L2 沿用各自业务域；L3 归新建域 `综合研判`；中文取值，层级不进 domain | §4.1.1 |
| 规则契约 / 判定依据 | `tag_registry.conclusion_dict` / `tag_value.conclusion_detail`，职责不同故名称不同 | §4.3 |
| 口径标识 | 纯 `version`，**不用 `params_hash`**；规则升级 = bump `@tag(version=...)` | §4.4 |
| 规则字典内容 | 只存当前生效规则（含完整 `rules` 规则表），不存历史、不存 hash | §4.3.1 |
| 规则升级处理 | 全量重算历史、直接覆盖、统一新口径 | §4.4.2 / §4.4.3 |
| 复盘当时结论 | **场景不存在、无意义，不保留快照** | §4.4.4 |
| 稳定性处理 | **裸判 + 游程标注**；日翻转率不达标再引入滞回，不抄 regime 5/20 | §7.1.1 |
| 门禁阈值 G1 | `0.6` 初值，实现时按覆盖率与出档率实测标定，结果进 `conclusion_dict.params` | §4.2 |
| L3 首期范围 | 2 个 | §6 |
| 三个空域 | ⑧⑪⑫ 不在本期范围；⑫ 预测评分继续冻结 | §0 |
| NOT / 排除筛选 | 本期不考虑 | — |
| 前端展示设计 | 不在本文档范围，属前端设计开发任务 | §0 范围声明 |

---

## 13 实施路线（评审通过后）

| 阶段 | 内容 | 产出 | 验收锚点 |
| --- | --- | --- | --- |
| **P0 契约冻结** | 冻结 6 组 enum 值域与措辞、两个新字段的 JSONB schema、`综合研判` 在下游白名单的登记；§12 决策已全部拍板 | 本文升 v1.0 | 全部结论可用 enum 表达，值域无「预测/建议」措辞；字段职责无重叠；`综合研判` 已被两份 domain 白名单识别 |
| **P1 补数（第一批）** | 扩 `A_INDICATOR_MAP`、港股 CAPEX 落库、`financial_indicator` 加列、`quantile.py` 显式 SELECT 同步、`sql/schema.sql` 同步 | 零新增请求的新字段 | 新旧标签结果一致性对比（参考 `doc/compute_task_resource_standard.md` §5 的 worktree 对比法） |
| **P2 落表 + L2 四结论** | 两个 JSONB 列 DDL、结论生产者、4 个 `dim_*` enum | 4 个可检索 enum + 依据明细 | §10.1 覆盖率 / 翻转率 / 版本一致性 / 人工抽检 |
| **P3 L3 两结论** | 2 个 `biz_*` enum（**首期范围已定**） | 2 个交叉类型 enum | §6.0 三判据逐条核对 |
| **P4 补数（第二批）** | A 股研发强度（独立 unit，季频） | evidence 增强 | 覆盖率达标后进 evidence，不进判定 |

> **每阶段收尾必须回到 §1.1 逐条核对**：产出是否仍在回答投资者的六个问题，有没有滑向「打分 / 预测 / 建议」。这是防止需求走偏的闸门，与 `doc/market_profile.md` 的对齐闸门同款。

---

## 附录 A：2026-11-12 字段探测记录

| 接口 | 参数 | 结果 | 耗时 |
| --- | --- | --- | --- |
| `ak.stock_financial_abstract` | `symbol='600519'` / `'002027'` | `(80, 106)`，80 行指标；「扣非净利润」「商誉」「投入资本回报率」「存货/应收/总资产周转率」「ROE_扣非」「OCF/归母净利」均有真实数值；**无研发** | 快 |
| `ak.stock_financial_abstract_ths` | `symbol='600519', indicator='按报告期'` | `(104, 25)`，**直接给「扣非净利润同比增长率」**，1998 起；港股代码抛 `AttributeError`（不支持） | 0.4s |
| `ak.stock_profit_sheet_by_report_em` | `symbol='SH600519'` | `(104, 203)`，`RESEARCH_EXPENSE` / `_YOY` / `ME_RESEARCH_EXPENSE` / `NOTICE_DATE`；最新期有值；非空 37/104 | 3.3s / 21 次分页 |
| `ak.stock_financial_hk_report_em` | `stock='00700', symbol='现金流量表'` | `(1810, 11)`，60 科目 / 96 报告期；年报 25 期中「购建固定资产」25/25、「收购附属公司」19/25、「经营业务现金净额」25/25 | 快 |
| 同上 | `symbol='资产负债表'` | 71 科目，**无商誉** | 快 |
| 同上 | `symbol='利润表'` | 32 科目，**无研发 / 无商誉** | 快 |
| `ak.stock_financial_hk_analysis_indicator_em` | `symbol='00700'` | `(9, 36)`，含 `ROIC_YEARLY` / `ROA` / `CURRENT_RATIO`；无扣非 / 研发 / 商誉 | 快 |

环境：`.venv/bin/python3` / `akshare 1.18.60`。全程只读，未修改项目任何文件。

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

## 附录 C：关键证据索引

| 主题 | 位置 |
| --- | --- |
| 12 域定义 / 蓝橙域 / 合规 / PIT 高危项 | `doc/stock profile hd.md:192-209, 380-432, 489, 729-744` |
| 三层范式 / 层间铁律 / L3 准入 / 不可加性 | `doc/market_state_data_architecture.md:35-62, 274-299`；`doc/market_profile.md:1321-1352` |
| L2 五件套 / 出档门禁 / 口径变更纪律 | `build_dimension_value.py:23-37, 300-511, 859-871` |
| L3 状态机 / 防抖参数 / 非预测声明 / 证伪结论 | `build_business_value.py:163-208, 256-314, 354-430, 515-530, 800-823, 1341-1358, 1563-1573` |
| regime 字典表与 `params_hash`（**仅作对照，本层不采用**） | `build_business_value.py:107-126, 244-247, 969-1004`；`build_dimension_value.py:502` |
| `version` 写入链路（本层沿用） | `profiling/engine.py:357-367, 359-362, 413`；`profiling/registry.py:85, 276-284` |
| 现有标签注册表 / enum 值域约束 | `profiling/registry.py:39-131`；`profiling/schema.py:26-135` |
| `profile.*` 表结构 / 唯一约束 / 版本化注释 | `sql/schema.sql:1354-1462`（尤其 `:1421, 1425, 1428`） |
| `financial_indicator` 表 / PIT 护栏 / TTM | `sql/schema.sql:771-812`；`profiling/quantile.py:186-213, 254-387` |
| 采集端白名单 / 港股现金流量表 / FCF | `get_financial.py:108-153, 222-271, 276-326, 495-533` |
| 消费端读取 SQL / enum 值域翻译 / `as_of` 回放 | `stock-realtime/db.py:2976-2993, 6325-6330, 8098-8169, 8475-8568, 8749-8797` |
| 位置档实现 / 6 阶段暂不注册 | `profiling/tags/trend.py:18-32, 121-220` |
| 计算任务资源规范 | `AGENTS.md`「计算任务资源约定」；`doc/compute_task_resource_standard.md` |

---

*本文为设计契约（v0.4，决策已全部拍板）。任何实现改动前，须先完成 P0 契约冻结并升 v1.0；涉及数据库结构的改动必须同步 `sql/schema.sql`（AGENTS.md 最高优先级约束）。*
