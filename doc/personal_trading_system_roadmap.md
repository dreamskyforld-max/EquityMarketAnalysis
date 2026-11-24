# 个人股票交易系统 · 能力模型、差距分析与增量开发路线

| 项 | 内容 |
|---|---|
| 文档性质 | 深度研究 / 可行性评估 / 架构建议稿（**非设计定稿、非投资建议**） |
| 评估基准 | 2026-11-24 对 `EquityMarketAnalysis` 与 `stock-realtime` 两个仓库的只读实测 + 库结构实查 |
| 上位文档 | `doc/industry_analysis_feasibility_investor.md`（用户侧信息需求结论）、`doc/industry_allocation_feasibility.md`、`AGENTS.md`（工程红线）、`UI_PAGE_STANDARD.md` |
| 状态 | 建议稿。所有表结构、阈值均为**候选方案**，未经确认不得落地 |

---

## 0. 先回答「为什么现有系统不能指导决策」

### 0.1 三条实证

**① 后端 73 张表 / 10 个视图里，没有任何一张属于「我」这个主体。**

`sql/schema.sql` 全文通读的结果：表全部描述**市场**（行情、逐笔、财务、宏观、资金流、行业分类、市场状态、画像标签、采集监控）。逐一排除易误判项：

- `realtime_order_size` → 是「大单/中单/小单金额分档」，不是委托单；
- `daily_ggt_hold` → 是南向资金市场持股，不是我的持仓；
- `regime.insider_trade.position` → 是董监高职务；
- `regime.signal_value` → 是市场反转条件命中，且 schema 自述「本表仍**零生产者**」，`tests/test_reversal_risk.py:216-218` 有硬断言禁止生产者写入；
- `benchmark_correlation.signals` → 是相关性文本结论，不是交易信号。

**没有账户、现金、持仓、订单、成交、费用、净值、策略、组合**。系统在描述一个与我无关的市场。

**② 前端其实已经有交易决策页，但它是一个「孤立的日志本」。**

`stock-realtime/pages/plan.py`（60KB）+ `trade_plan` / `trade_plan_checklist` / `trade_plan_log` 三张自建表，已实现：

- 状态机：`观望中 → 已买入 → 已卖出 → 已复盘`，另有 `已作废`；
- 字段：`buy_low/buy_high`（买入区间）、`target`、`stop_price`、`shares`、`hold_period`、`risk_amount`、`source`、`note`、买卖价/股数/日期、`review_pnl`、`review_note`；
- KPI：计划总数 / 执行率 / 兑现率 / 止损挂设率 / 待复盘数；
- 完整的用户隔离（`user_id` 外键 + `db._own_plan_id` fail-closed）。

但 `plan.py` 头部注释明确写着：

> 「刷新：纯用户数据、**无市场依赖** → 不注册 DAG section（同 usermgmt.py），改动后手动 refresh。」

这句话是全部问题的浓缩：**计划页不读行情**。`stop_price` 不会被价格触发，`target` 不会自动达标，持仓浮盈不会更新，`review_pnl` 是手填数字不是算出来的，`risk_amount` 有字段但没有任何校验逻辑。计划与账户、计划与市场、计划与结果，三条线全断。

**③ 方向预测在你自己的系统里已经被证伪。**

| 证据 | 原文 |
|---|---|
| `predictor_scan.py:665` | 「放弃方向预测，转向波动率/尾部风险预测」 |
| `predictor_scan.py:1-17` | 已测：20 特征 walk-forward logistic AUC **0.44–0.54**、OFI 分钟级 AUC≈**0.5**、12 特征 Ridge 次日收益 |
| `support_resistance.py:13-14` | 「不宣称能预测突破方向（项目已有结论：港股分钟/日线方向近随机，弱式有效）」 |
| `liquidity/fund_flow_analysis.py:3-4` | 「纯描述性/横截面分析，**不做涨跌预测（方向预测已证伪）**」 |
| `sql/schema.sql:2437/2439` | 「不产出单一综合分」「只描述当前状态，**不含超配/低配/买卖含义**」 |
| `tools/validate_regime.py:8` | 「结论仅作描述性证据，**不构成可交易信号**」 |

**这一条决定了整个方案的定位**：如果「个人交易系统」被理解成「再加一个能告诉我买什么会涨的模型」，那是在跟已有实证结论对着干，也是过拟合和数据窥探的温床。

### 0.2 范式判断

现有系统是**观察系统**（MIS / BI）：回答「市场现在是什么样」。按 DSS 经典定义，只有数据展示而没有模型库与规则库驱动，属于 MIS 而非决策支持系统。

交易系统是**决策与责任系统**（DSS）：回答「我该做什么、做多少、做错了怎么办、上次为什么错」。

两者的鸿沟不是「指标不够」或「数据不够」，而是缺四个**主体性要素**：

| 缺失要素 | 含义 | 现状 |
|---|---|---|
| **主体** | 我是谁、有多少钱、持有什么、成本多少 | 完全没有 |
| **约束** | 哪些动作绝对不许做（风险预算 / 硬闸门） | 只有字段，无引擎 |
| **承诺** | 事前写下的可证伪规则 + 版本 | 无策略实体 |
| **反馈** | 真实结果 → 归因 → 规则迭代 | 只有手填 `review_pnl` |

一个类比：你已经造了一台精度极高的天文望远镜（73 表、逐笔、PIT 宏观、200+ 指标），但交易系统需要的是**航海日志、压舱石、舵，和一套事前写在纸上的航行规则**。再加镜片不会让船自动到港。

### 0.3 必须延续你自己已经得出的结论

`doc/industry_analysis_feasibility_investor.md` 的结论同样适用于交易系统：

> 「**排名表天然诱导追第一名的行为，等于每天制造一个噪声信号**……别给我一个分数，给我一个尺度。」
> 「停止投入：六维合成、QUADRANT、RANK、把拥挤闸门升格为信号。」

外部证据同向：散户亏损的主因不是选不出好股，而是**换手率、处置效应、注意力驱动买入**（A 股个人投资者月换手率 67.81%，换手率最高档与最低档月超额净收益相差 2.11pct；中国个人投资者卖出盈利股比例比卖出亏损股高约 20%）。改善路径是「降低频率 + 事先写下卖出规则 + 不在被推到眼前的股票里挑买入对象」。

**因此本方案的设计铁律**：

> 系统输出的是 **约束校验结果 + 证据尺度 + 决策审计**，不输出「综合评分」「买入推荐排名」「目标价」。
> 决策权保留在人手里（human-in-the-loop），系统负责让每一次决策**事前可拦截、事中可解释、事后可归因**。

---

## 1. 现状实证盘点

### 1.1 可复用资产（很值钱，不要重造）

**后端 `EquityMarketAnalysis`**

| 资产 | 位置 | 交易系统里的用途 |
|---|---|---|
| `v_daily_quote`（QFQ 前复权统一日线，HK+A） | schema.sql:1119 | 估值、收益对照、基准对齐（**注意 4.3 的陷阱**） |
| `trading_calendar`（HK/A 日历日） | :1736 | 净值曲线、持有天数、T+2 交收日计算 |
| `daily_benchmark` / `benchmark_minute` | :217 / :1202 | 相对收益、超额收益、归因基准 |
| `regime.macro_series`（PIT 事实表，带 `revision`） | :1652 | **无前视的 as-of 证据快照底座** |
| `regime.indicator_value` 200+ 原子指标 + 历史分位（无前视） | :2077 附近 | 「尺度」而非分数：给决策提供历史位置 |
| `profile.tag_value` EAV + `eff_from` 版本链 + `params_hash` | :1382-1489 | **口径/规则版本管理的现成原型** |
| 行业分类（HSICS / 申万）+ 个股映射 | :1915-1998 | 行业集中度约束、Brinson 分组归因 |
| `support_resistance.py` | 根目录 | 支撑/阻力位 → 止损位的**客观锚**（不是预测） |
| `dividend_history` / `regime.unlock_schedule` / `ipo_event` | :1504 / :1798 / :1750 | 公司行动、持仓事件日历 |
| `tick_data` / `force_round_daily` | :628 / :2289 | 滑点/冲击估计、流动性约束 |
| `monitor_collector` + `collection_alert` + 企微 Webhook | 根目录 | **数据新鲜度闸门 + 现成的预警推送通道** |
| `direction_backtest.py` walk-forward / Wilson CI | 根目录 | 规则验证工作台的方法论骨架（产出需资产化） |

**前端 `stock-realtime`**

| 资产 | 位置 | 用途 |
|---|---|---|
| 多用户 / session / `user_id` 隔离 / admin 角色 | `auth.py`、`db._own_plan_id` | 账户与持仓天然按用户隔离 |
| `/system/plan` 交易决策页 + 三表 | `pages/plan.py` | **直接改造成账户工作台，不新建页面壳** |
| 计划状态机 + checklist + 复盘段 + KPI 带 | 同上 | 保留，补「账户事实」而非重写交互 |
| 自选股 / 选股方案 / 阈值配置（按 `stock_code`） | `pages/watchlist.py` / `screen.py` / `config_store.py` | 候选池来源、规则参数存储范式 |
| `UI_PAGE_STANDARD.md` + `THEME_TOKENS` + 弹窗/表格/ECharts 封装 | `layout.py`、`config.py` | 页面层几乎零重造 |
| 21 张自建表 `CREATE TABLE IF NOT EXISTS` 懒建机制 | `sql/stock_realtime_schema.sql` | 加表路径成熟 |
| **占位页 `/stock/research`（指标研究 P0）/ `/stock/gate`（验证门控 P1）** | `config.py:359-371` | **规则验证工作台已预留位置** |

> 关键结论：**UI 壳、用户体系、计划/复盘骨架、数据底座都已具备**。真正要建的是「账户事实层 + 约束引擎 + 反馈闭环」，不是再造一套系统。

### 1.2 明确不存在（逐项确认）

| 能力 | 判定 | 依据 |
|---|---|---|
| 账户 / 现金 / 多币种 | 确认不存在 | 无 account / cash 表 |
| 持仓状态机 / 日持仓快照 | 确认不存在 | 仅 `plan.py` 有「持仓中 · N 天」文案 |
| 订单 / 成交 / 部分成交 / 撤单 | 确认不存在 | 无 order / fill 表 |
| 交易成本模型（佣金/印花税/平台费/滑点） | 确认不存在 | 全库 Grep 无 commission / slippage / fee 实现 |
| 仓位 / 风险预算引擎 | 部分存在 | `trade_plan.risk_amount` 有字段无校验；`market_state_daily.py` 里「仓位」只是指标判读注释 |
| 组合净值曲线 | 确认不存在 | 无 nav / equity_curve 表或计算 |
| 夏普 / Sortino / Calmar / VaR / 组合回撤 | 确认不存在 | 市场层已有 `RISK.RV20/RV60/MAXDD_250`，但组合层零实现 |
| 业绩归因（Brinson / 因子 / 规则级） | 确认不存在 | `regime.dimension_value.contrib` 是维度分数贡献，非业绩归因 |
| 策略 / 规则实体与版本 | 部分存在 | 有 `params_hash` / `model_version` 管**指标口径**，无策略实体 |
| 决策快照（当时知道什么） | 确认不存在 | — |
| 预警规则表与推送 | 确认不存在 | `watchlist._SIGNAL_ENABLED = False`；`screen_scheme.notify_tag_change` 注释「推送能力建设中，先存配置」 |
| 生产级买卖信号 / 组合建议 | 确认不存在（且代码明文禁止） | schema.sql:2318/2437/2439 |
| 回测结果资产（落库 / 版本 / 可对照） | 确认不存在 | `direction_backtest` / `multi_factor_analysis` / `threshold_analysis*` / `phase_analysis` / `train_buy_sell_index` **全部 stdout**，无落库、无调度、无模型持久化 |
| 服务化 API | 确认不存在 | 后端是 APScheduler 定时脚本 + systemd oneshot；前端直连同一 PG |

---

## 2. 个人交易系统应该具备什么能力（7 层模型）

| 层 | 名称 | 职责 | 输入 → 输出 | 验收标准 |
|---|---|---|---|---|
| **L0** | 主体层 | 账户、现金、持仓、成交流水、公司行动、费用 | 手工录入/券商同步 → **唯一权威的账户事实** | 任一时刻能回答「我有多少钱、持有谁、成本多少、净盈亏多少（含费）」 |
| **L1** | 约束层 | 风险预算 + 事前硬闸门 | 候选动作 + 账户事实 → **通过 / 拦截 + 具体违反了哪条** | 任何想越过红线的动作，在提交前被拦住并给出理由 |
| **L2** | 承诺层 | 策略/规则实体、版本、生命周期、失效条件 | 「如果 A 且 B，则买 N 股、止损 P、失效条件 C」 → 可版本化、可证伪的规则 | 每笔交易能追溯到「哪条规则的哪个版本」 |
| **L3** | 证据层 | **现有后端全部指标**（只读） | 市场数据 → **尺度 / 历史分位 / 状态 / 事件**，不产出分数与推荐 | 沿用你「别给分数给尺度」的结论；带 as-of 时点与数据新鲜度 |
| **L4** | 决策层 | 人在环：提案 → 检查 → 确认 | 候选动作 + 约束校验 + 证据 + 决策快照 → **决策记录** | 输出不是「买入指令」，是「这次动作会触碰哪些线、证据是什么、你确认吗」 |
| **L5** | 执行层 | 订单 / 成交记录、成本模型、交收 | 确认后的决策 → 成交事实 + 实际成本 | 决策价 vs 成交价可对照（滑点），T+2 交收可追踪 |
| **L6** | 反馈层 | 净值、绩效风险、三层归因、行为诊断 | 账户事实 + 市场数据 → **规则级评价 + 偏差诊断 + 退役建议** | 能回答「赚钱是靠规则、靠 beta 还是靠运气」「我有没有处置效应」 |

**层间契约**：L3 绝不直接连 L5。所有市场数据只能作为**证据**进入 L4，经 L1 校验、L2 登记、人工确认后才产生 L5 的动作。这条契约是防止「把描述性指标悄悄升格成交易信号」的工程保障——你现有代码里多处明文禁止的正是这件事。

**为什么 L1/L6 比 L3 重要**：量化工程的普遍结论是预测信号只占工程实现不到 10%，组合构建、执行摩擦与归因才决定长期存续；一个理论年化 25% 的策略，在不可交易标的、行业约束、点差规费、滑点冲击四层衰减后，实盘净收益约 14.5%，**超过 40% 的理论收益被摩擦吃掉**。你的系统目前把 100% 的精力投在 L3。

---

## 3. 差距矩阵

| 能力 | 现状 | 目标 | 处置 | 落地项目 |
|---|---|---|---|---|
| 市场数据 / 指标 / 宏观 | 完备（73 表、200+ 指标） | 保持，只做只读证据 | **复用** | 后端 |
| 用户 / 权限 / 页面壳 / 主题 | 完备 | 保持 | **复用** | 前端 |
| 交易计划 / checklist / 复盘 | 已有骨架 | 升级为「计划 → 成交 → 持仓 → 复盘」链路 | **改造** | 前端 |
| 账户 / 现金 / 持仓 / 成交 | 无 | L0 | **新建** | 前端（自有数据） |
| 费用 / 公司行动 / 成本基础 | 无 | L0 | **新建** | 前端 + 后端事件数据 |
| 风险预算 / pre-trade gate | 字段有、引擎无 | L1 | **新建** | 前端服务层 |
| 策略规则实体 + 版本 | 无 | L2 | **新建** | 前端 |
| as-of 决策快照 | 无（但 PIT 底座已有） | L2/L4 | **新建** | 前端写 + 后端只读 |
| 预警规则 + 推送 | 配置开关有、通道无 | L1/L5 | **打通** | 前端 + 企微 Webhook（已有） |
| 净值 / 绩效 / 归因 | 无 | L6 | **新建** | 前端 + 后端基准数据 |
| 回测资产化 | stdout | 规则验证工作台 | **改造** | 后端 → 占位页 `/stock/research`、`/stock/gate` |
| 服务化决策引擎 | 无（定时脚本 + 直连 PG） | 带事务/校验的服务层 | **新建（轻量）** | 可先嵌在前端 `db.py` 服务层，不另起服务 |

---

## 4. 还缺哪些数据

### 4.1 自有数据（外部数据源永远无法替代，最高优先级）

建议全部落在 **`stock-realtime` 自建表**（与 `app_users` 同库、同 `user_id` 隔离），不要塞进后端 `EquityMarketAnalysis` 的共享行情库——后者是多项目共用的**市场事实库**，混入个人账户数据会破坏职责边界与 `schema.sql` 的唯一真相源约定。

| # | 数据 | 建议表（候选名） | 关键字段 |
|---|---|---|---|
| 1 | 账户主体 | `trading_account` | `user_id, broker, market, currency, base_currency, opened_at, status` |
| 2 | 资金流水 | `account_cash_flow` | `account_id, flow_type(入金/出金/股息/利息/费用/汇兑), amount, currency, trade_date, settle_date, ref_id` |
| 3 | 订单 | `trade_order` | `account_id, plan_id, rule_id, stock_code, side, order_type, qty, limit_price, placed_at, status, decide_price, decide_at` |
| 4 | 成交 | `trade_fill` | `order_id, fill_qty, fill_price, fill_at, gross_amount, fees_jsonb, settle_date` |
| 5 | 持仓日快照 | `position_daily` | `account_id, stock_code, trade_date, qty, cost_basis, avg_cost, last_price, market_value, unrealized_pnl, realized_pnl_to_date` |
| 6 | 费用规则（版本化） | `fee_schedule` | `broker, market, eff_from, eff_to, stamp_duty, commission_rate, commission_min, platform_fee, sfc_levy, trading_fee, ccass_rate/min/max, afc_levy, currency` |
| 7 | 公司行动（作用于我的持仓） | `position_corporate_action` | `account_id, stock_code, action_type(分红/送股/合股/拆股/供股/退市), ex_date, ratio, cash_amount, applied_at` |
| 8 | 策略 / 规则 | `trading_rule` / `trading_rule_version` | `rule_id, version, params_hash, statement(条件句), entry/exit/invalid_condition, eff_from, eff_to, status` |
| 9 | 决策快照 | `decision_snapshot` | `plan_id, rule_version, account_state_jsonb, evidence_jsonb(as-of 指标/分位/事件), data_freshness, expected_invalidation, created_at` |
| 10 | 净值 / 基准 | `portfolio_nav` | `account_id, trade_date, nav, cash, market_value, fees_to_date, benchmark_code, benchmark_ret, active_ret` |
| 11 | 预警规则与推送记录 | `alert_rule` / `alert_log` | `rule_type, threshold, channel, triggered_at, payload, ack_at` |

其中 **#9 决策快照**是最容易被忽略、也最决定复盘质量的一张表：它把「下单那一刻系统知道什么」冻结下来（指标值 + 分位 + 数据新鲜度 + 当时的账户状态 + 规则版本 + 失效条件）。没有它，复盘必然被后见之明污染——你会用事后走势去评价事前决策。后端已有的 PIT 宏观事实表与 `params_hash` 口径版本纪律，正好是它的现成底座。

### 4.2 需补的市场 / 参考数据

| 数据 | 用途 | 现状判断 | 建议 |
|---|---|---|---|
| 汇率日频（HKD/CNY/USD） | 多币种账户净值折算、汇兑损益 | `daily_benchmark.open_price` 注释提到「FRED/汇率类源」为单值序列，**可能已有部分**，需实查 `bench_code` 清单确认 | 有则复用，无则补一条采集 |
| 每手股数 / 最小价位 tick size | 订单可成交性校验、整手取整 | 确认不存在 | 港股 2026 起分阶段标准化每手股数（1/50/100/500/1000/2000/5000/10000），**必须做成按生效日期版本化的表**，不能硬编码 |
| 停牌 / 最后交易日 / 退市 | 持仓估值与流动性风险 | 后端有 `sync_quote_universe` 软删与 `is_collectable`，但无「停牌期间」事实 | 补日级可交易标记 |
| 总回报基准（含股息） | 业绩归因的公平基准 | `daily_benchmark` 现有多为价格指数 | 明确区分价格 / 总回报口径，避免高估超额 |
| 无风险利率 | 夏普 / Sortino | 后端已有中债/美债系列 | **复用** |
| 真实成交价 vs 决策价 | 滑点 / 实施差额 | 自己产生，无记录 | 由 `trade_order.decide_price` + `trade_fill.fill_price` 产生 |

### 4.3 ⚠️ 一个必须先解决的口径陷阱：QFQ 前复权不能用来做账本

`hk_daily_quote` / `a_daily_quote` / `v_daily_quote` 的价格列**统一 QFQ 前复权**，且 `schema.sql:910` 明确写着：

> 「收盘价（QFQ 前复权，**锚定最新交易日**）；**每次除息事件后需重跑 `backfill_hk_market_turnover.py` 再锚定历史**」

也就是说，**历史价格会被事后重写**。这对「看图形态/算历史分位」是对的，但对账本是灾难：

- 你 3 个月前以 400 HKD 买入的持仓，除息后历史 K 线被整体下调，用 QFQ 序列算出来的「浮盈」与券商结单对不上；
- 成本基础、已实现盈亏、净值曲线会随每次除权重算而漂移，历史净值不可复现；
- 回测里用 QFQ 也隐含「当时就能知道未来复权因子」的轻微前视。

**对策**：账本与净值用**未复权成交价 + 公司行动调整成本基础**（`position_corporate_action` 显式记录每一次分红/拆合股对 qty 与 cost_basis 的影响），QFQ 序列只用于证据层与形态/分位计算。两套价格口径必须在代码里显式命名区分，禁止混用。这一条如果不在 P1 建表时定死，后期所有归因数据都要重算。

### 4.4 费率必须参数化（一个实证反例）

检索港股交易成本的公开资料时，同一批 2026 年资料给出**互相冲突**的数字：印花税有 `0.10%` 与 `0.13%` 两个版本，CCASS 结算费有 `0.002%(min 2 / max 100)` 与 `0.0042%` 两个版本；相对一致的是 SFC 征费 `0.0027%`、HKEX 交易费 `0.00565%`、AFRC `0.00015%`，富途港股佣金 `0.03%`（最低 HKD 3/单）+ 平台费 `HKD 15/单`。

**这本身就是结论**：不要把费率写进代码或常量。做成 `fee_schedule` 按 `broker / market / eff_from` 版本化的配置表，并且**以你的真实券商结单校准**（`trade_fill.fees_jsonb` 存实际扣费，定期与规则表对账）。粗略量级参考：一笔 HKD 50,000 的港股往返，各类规费+佣金+平台费合计约 HKD 140–150，即约 **0.28%–0.30% 单边往返成本**——这个数字足以吃掉大量短周期信号，是「降低换手」最直接的量化论据。

制度约束也要进数据模型：港股 **T+0 可日内回转 + T+2 交收**（港交所规划 2028 下半年转 T+1）、碎股不进中央自动撮合、卖出资金可用性由券商决定、除净日前一交易日买入才能在 T+2 后登记收息。

---

## 5. 还缺哪些功能

按投入产出比排序：

| 优先级 | 功能 | 为什么它才叫「指导决策」 |
|---|---|---|
| **P0** | 风险偏好与风格的书面定义 | 不写代码。写下风格/周期/资金/最大可承受回撤，以及 2–3 个要纠正的具体行为。没有这个，所有阈值都是拍脑袋 |
| **P1** | **账本闭环**（账户 → 成交 → 持仓 → 净值） | 系统第一次能回答「我现在暴露了多少、这笔下去会突破哪条线」。**这是「指导决策」的最小充分条件** |
| **P1** | 费用模型 + 公司行动 + 成本基础 | 没有净盈亏，一切绩效与归因都是假的 |
| **P2** | **Pre-trade 硬闸门** | 单票上限、单一行业上限、总仓位上限、单笔风险预算、相关性集中、日/累计回撤熔断；输出「拦截 + 违反条款 + 当前值」 |
| **P2** | 预警通道 | 止损/目标/失效条件触发 → 企微 Webhook（通道已有）+ 站内通知；启用 `watchlist._SIGNAL_ENABLED`，把「先存配置」的 `notify_tag_change` 接通 |
| **P2** | 数据新鲜度闸门 | 决策页必须挂后端 `collection_alert` / `monitor_*`：数据断供或清单塌陷时**禁止产生新决策**（与 2026-10-26 清单塌陷事故同类） |
| **P3** | 规则实体 + 版本 + as-of 决策快照 | 让「这笔依据什么、当时知道什么、规则后来改没改」可审计 |
| **P4** | 净值曲线 + 绩效/风险指标 | 夏普、Sortino、最大回撤、Calmar、胜率、盈亏比、换手率、与基准的同期对照 |
| **P4** | 三层归因 | ① Brinson 配置/选择（按 HSICS 或申万分组）；② 因子/风格暴露（区分 alpha 与 beta）；③ **规则级归因**（每条规则的样本外胜率、盈亏比、贡献、是否退役） |
| **P4** | 行为偏差诊断 | 处置效应（盈利/亏损卖出比例）、换手率、注意力驱动买入（是否来自当日热门/涨停）、计划外交易占比 |
| **P5** | 回测资产化 / 验证工作台 | 把 `direction_backtest` 等 stdout 结果落库 + 版本 + 成本入模 + walk-forward；接到已有的 `/stock/research`、`/stock/gate` 占位页。**只用于证伪候选规则，不产出推荐** |
| **P6** | 券商对接 / 自动执行（可选） | 保留人工确认、单笔/单日限额、异常熔断；个人自用不涉及投顾牌照，但一旦对外发布或代客理财即落入《证券期货投资者适当性管理办法》范围 |

---

## 6. 增量开发路线

### P0 · 目标对齐（不写代码）

- 交付：一页《交易宪法》——风格与持仓周期、资金规模与币种、最大可承受回撤、单票/行业上限、绝对禁止行为清单、**「指导决策」的验收标准**。
- 关键抉择：验收标准是「**事前拦截违规动作 + 事后可归因**」，还是「给出候选标的」？本方案强烈建议前者（理由见 §0.3）。

### P1 · 账本闭环（最高 ROI，建议先做且只做这一期）

落在 `stock-realtime`，新增 §4.1 的 #1–#7、#10 表；改造 `trade_plan` 而非替换：

```
trade_plan（计划/意图，保留现状）
    ↓ plan_id
trade_order（确认后的动作，含 decide_price / rule_version）
    ↓ order_id
trade_fill（实际成交，含 fees_jsonb / settle_date）
    ↓ 每日收盘批量
position_daily（权威持仓快照，含 cost_basis）
    ↓
portfolio_nav（净值 + 基准对照）
```

- 先**手工录入成交**，不接券商（券商对接复杂度与合规风险都高，而账本价值 90% 来自「记录」而非「自动」）。
- 必须同步：① `ensure_*_table()` 懒建函数；② 回填 `sql/stock_realtime_schema.sql`；③ 登记 `db.DATA_SOURCES` / `VERSION_SOURCES`（参与页面刷新时）；④ 新增页面路径加入 `main.py:_SKIP_ROUTES`；⑤ 跑 `scripts/audit_page.py` 到 0 error。
- **验收**：任选历史某日，系统算出的持仓市值、已实现/未实现盈亏、净值与券商结单误差 < 费率口径容差；且**三个月后重算同一日，数字不变**（证明不受 QFQ 重写影响）。

### P2 · 约束引擎 + 预警

- `pre_trade_gate(order_draft, account_state, rules) -> {allowed, violations[], evidence}` 纯函数 + 单测；规则参数存表、可版本化。
- 闸门必须是**服务端校验**，不能只做前端禁用按钮（前端已有 17 个 `/api/*` 薄封装，直接加在写接口前置即可）。
- 接通企微 Webhook；启用 `watchlist` 提醒带；决策页挂数据新鲜度。
- **验收**：构造 6 类违规样例（超单票、超行业、超总仓位、单笔风险超预算、回撤熔断中、数据断供），全部被拦截并给出准确原因。

### P3 · 规则与决策快照

- `trading_rule_version` 复用后端 `params_hash` / `eff_from` 的版本纪律（命名对齐 AGENTS.md「三名一致」）。
- 每次确认决策写一条 `decision_snapshot`：`as-of` 指标值取自后端 PIT 表与 `regime.indicator_value`，**只取决策时点之前已可得的数据**；同时记录 `data_freshness` 与 `expected_invalidation`。
- **验收**：随机抽 10 笔历史决策，能在不看后验行情的前提下完整复述当时的依据与失效条件。

### P4 · 净值、归因与行为诊断

- 净值重算 / 历史归因属于「读库 → 计算 → 写库」，**必须遵守 `AGENTS.md` 计算任务资源约定**：独立 systemd `oneshot + timer`（照抄 `system/compute-profile.service` 的六项限额 `MemoryMax / MemorySwapMax=0 / RuntimeMaxSec / Nice / IOSchedulingPriority / OOMScoreAdjust`），**禁止塞进 `market_scheduler` 进程**；大表查询带时间下界、单批 ≤50 万行、单事务 ≤5 万行；上线前 `/usr/bin/time -v` 实测峰值内存并写入提交说明。
- 任务级写 `collection_task_log` 埋点或登记 `monitor_table_config`，否则断供无人知道。
- 归因先做**规则级**（最贴合个人系统），Brinson/因子归因放后面；基准用 `daily_benchmark`，注意同窗口、总回报口径。

### P5 · 验证工作台（可选）

- 把 `direction_backtest.py` / `multi_factor_analysis.py` / `threshold_analysis*` / `phase_analysis.py` 的 stdout 结果落库 + `params_hash` + 成本入模 + walk-forward。
- 接到已预留的 `/stock/research`（指标研究）与 `/stock/gate`（验证门控）占位页。
- **硬约束**：此层只输出「假设是否被证伪」，禁止把结果升格为配置建议——与后端 schema 现有禁令一致。

### P6 · 券商对接（可选，谨慎）

- 人工确认 + 单笔/单日限额 + 异常熔断三件套缺一不可；合规边界见 §7。

---

## 7. 风险与漏洞评估

| # | 风险 | 机制 | 对策 |
|---|---|---|---|
| 1 | **定位错** | 把「指导决策」做成「预测涨跌」，与自家证伪结论冲突 | 铁律：输出约束/尺度/审计，不输出推荐；L3→L5 层间契约 |
| 2 | **账本口径错** | 用会被事后重写的 QFQ 价格算持仓与净值 → 账实不符、历史不可复现 | 未复权成交价 + 公司行动调整成本基础；两套价格显式命名区分（§4.3） |
| 3 | **费率硬编码** | 公开资料已实证冲突（0.10% vs 0.13%）、费率会变 | `fee_schedule` 版本化 + 用 `fees_jsonb` 与结单对账 |
| 4 | **后见之明偏差** | 复盘看后验走势，无法还原当时信息 | `decision_snapshot` as-of 冻结 |
| 5 | **前视泄漏** | 用「当日收盘后算出的指标」解释「盘中决策」；财报按报告期而非披露日入库 | 复用后端 PIT `revision` 纪律；快照记录每个证据的 `available_at` |
| 6 | **过拟合 / OOS 污染** | 反复看样本外结果改规则 → OOS 被污染；回测 Sharpe > 2 却无法解释 | 规则结构在看结果前锁定；参数 ≤3 个；`Deflated Sharpe`；保留被否方案记录 |
| 7 | **回测≠实盘** | 现有回测全部 stdout、无成本、无版本、无滑点，不能直接当预期收益 | 回测必须报毛/净收益 + 换手 + IR；与实盘 `trade_fill` 做实施差额对照 |
| 8 | **排名/综合分诱导追高** | 每天必有第一名，等于每天制造噪声信号 | 沿用你自己文档的结论；页面给历史分位与事件，不给总分和榜单 |
| 9 | **数据断供时决策失真** | 决策页「无市场依赖」，清单塌陷/采集停摆时仍可正常下单 | 决策前置数据新鲜度闸门（复用 `collection_alert` / `snap_guard.check_scope_health` 思路） |
| 10 | **约束形同虚设** | 只在 UI 禁用按钮，绕过 API 即可写入 | 闸门放服务端写接口前置；纯函数 + 单测覆盖 |
| 11 | **多项目共用库的 schema 漂移** | 两个项目各有 schema 文件，新表漏登记 → 部署缺表 | 前端 `sql/stock_realtime_schema.sql` 与后端 `sql/schema.sql` 双份同步；跑 `_schema_diff.py` / `_code_schema_diff.py`；遵守 `AGENTS.md` 命名三铁律 |
| 12 | **资源事故** | 服务器 ~1.9GB 内存，历史净值重算/归因若全量加载大表会整机雪崩（项目已有 4 次事故档案） | 独立 systemd 单元 + 六项限额 + 分批 + 峰值内存实测（§ P4） |
| 13 | **合规边界** | 个人自用不受经营机构义务约束；对外发布/有偿荐股/代客理财即落入《适当性管理办法》 | 保持单用户自用，不开放公开推荐 |
| 14 | **自动下单失控** | 港股 T+0 可日内回转，无人工确认时错误会被放大 | 人工确认 + 限额 + 熔断；P6 之前完全不接 |

---

## 8. 与项目既有工程约定的对齐

- **数据库结构**：`EquityMarketAnalysis/sql/schema.sql` 是后端唯一真相源；`stock-realtime/sql/stock_realtime_schema.sql` 是前端 21 张自建表的登记处。新增账户表**只改前者之外的后者**，但如果新增任何后端侧表/视图/索引，必须按 `AGENTS.md` 走 `_dump_schema_from_db.py --apply` + 三个 diff 脚本自检。
- **命名三铁律**：新建表/脚本/标签 code 遵守「名实匹配 / 三名一致 / 单一职责」。`trade_order` / `trade_fill` / `position_daily` / `portfolio_nav` / `trading_rule_version` 均为单一职责、口径自明；避免 `info` / `data` / `temp` / 泛化的 `portfolio_info`。
- **用户隔离**：所有新表带 `user_id` 外键 + `ON DELETE CASCADE`，全部读写过滤，跨实体 ID 走 `_own_*` fail-closed（照抄 `db._own_plan_id`）。
- **资源红线**：见 §7 #12 与 `doc/compute_task_resource_standard.md` §6 检查清单。
- **文档**：本文件是 `*.md`，按项目 `.gitignore` 约定默认**不入库**；若要纳入版本管理需显式 `git add -f`。

---

## 9. 需要你拍板的 5 个前置参数

这些参数决定 P1 的表结构与所有阈值，**不是措辞问题，是架构分叉点**：

1. **「指导决策」的验收标准**：是「事前拦截违规动作 + 事后可归因」（本方案推荐），还是「系统给出候选标的」？这一条直接决定 L4 是否有「提案」概念。
2. **交易风格与持仓周期**：短线（1–数日）/ 波段（数周–数月）/ 中长线（半年以上）。决定数据频率、成本敏感度、约束参数、是否值得做盘中预警。
3. **账户范围与币种**：几个券商、HK / A / 美股 / 港股通、是否需要多币种折算与汇兑损益。决定 `trading_account` 与汇率数据。
4. **是否接实盘下单**：还是「手工录入成交」就够。决定要不要做订单路由与合规评估（P6 是否进入路线）。
5. **资金规模量级**：决定整手约束、滑点/容量建模的必要性，以及风险预算用百分比还是绝对金额。

确认后可直接给出 P1 的**完整 DDL、埋点清单、迁移脚本与验收用例**。

---

## 附录 · 证据索引

| 结论 | 主要证据 |
|---|---|
| 后端 73 表 / 10 视图、无账户类表 | `EquityMarketAnalysis/sql/schema.sql` 全文；`README.md:86` |
| 无服务化 API、43 条调度任务 | `market_scheduler.py`；`requirements.txt`；`trend_cache/qrobot_plugin.py`（唯一 Flask） |
| 方向预测证伪 | `predictor_scan.py:1-17,659,665`；`support_resistance.py:13-14`；`liquidity/fund_flow_analysis.py:3-4`；`tools/validate_regime.py:8` |
| 禁止配置建议 | `sql/schema.sql:2318,2437,2439`；`profiling/tags/conclusion.py:21` |
| 前端交易计划现状 | `stock-realtime/pages/plan.py:1-29`；`sql/stock_realtime_schema.sql:345-389`；`db.py:10780-10804` |
| 前端写操作与用户隔离 | `stock-realtime/db.py`；`auth.py`；`AGENTS.md` |
| 预警未打通 | `watchlist.py:105-108`（`_SIGNAL_ENABLED=False`）；`sql/stock_realtime_schema.sql:257` |
| QFQ 前复权与重锚 | `sql/schema.sql:878-883,904-910,1172` |
| 反排名 / 反综合分立场 | `doc/industry_analysis_feasibility_investor.md` §0–§1 |
| 工程红线 | `AGENTS.md`（schema 唯一真相源、计算任务资源约定、命名三铁律、清单塌陷门控） |

> 外部方法论参考：DSS 四库结构（数据库/模型库/知识库/人机交互）与「只有数据展示 = MIS 而非 DSS」；CRISP-DM 业务理解先行；量化五层架构（PIT → 信号 → 组合 → 执行 → 风控归因）；Brinson / 因子 / 信号级三层归因；行为金融关于处置效应、过度交易、有限注意力的实证；港股 T+0 交易 / T+2 交收与 2026 每手股数标准化安排。
> 本文为系统设计与方法论层面的资料整理，**不构成任何投资建议**；历史回测结果不代表未来表现。
