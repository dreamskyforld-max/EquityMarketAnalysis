# 行业配置（第四层）可行性分析与设计

> **文档性质**：设计稿（未实施）。目标与需求牵引 → 指标体系为主体 → 落地计划收尾。
> **上位文档**：`doc/market_profile.md`（下称「主报告」，重点评估其 §6 第四层 / §6.10 / §7 / §8.5 / §9）。
> **现状依据**：`sql/schema.sql`、`market_state_daily.py`、`doc/market_profile_collection_plan.md` §7.11/§7.13/§7.14/§7.8、`doc/market_state_data_architecture.md`、`doc/compute_task_resource_standard.md`。
> **数据基准**：2026-11（本项目行业层采集自 2026-09-30 起陆续上线）。
> **边界**：本文只做第 ④ 层指标与计算的设计，不覆盖前端页面契约（`market_profile_ui_handoff.md` 目前完全没有 ④ 层内容，需另行补齐）。

---

## 0. 结论摘要

| 问题 | 结论 |
|---|---|
| §6.10 目标设计是否合理 | **不合理**。它输出「超配/标配/低配建议 + 核心逻辑与风险」，与主报告 §0 判断 1、§1.2「度量非预测」、§11 风险 3 直接冲突；且把两项最不可得的数据（中观景气、盈利修正）设为最高权重，与 §9.1、§0 判断 2 自相矛盾；同时绕开了 §7.1/§7.2 的分位化与三层合成结构、§9.4 的 PIT 铁律、§0 判断 4 的 A/H 分离原则。详见 §1.2。 |
| 重设后的目标是否可行 | **可行**。主报告 §9.1 判定「行业层数据可得性中低、最大瓶颈」的前提是「中观景气需外采」；而项目现已落地 5 类行业数据（申万一级行业指数长历史、行业估值快照、行业资金流、行业横截面 `sector_daily`、一致预期快照），瓶颈已从「数据有无」转为「**成分主数据 PIT 缺失** + **快照类数据历史深度不足**」。把目标从「给配置建议」改为「给状态读数 + 风险闸门」，即可在不外采、不预测的前提下落地大部分指标。 |
| 落地的关键约束 | ① 必须先解决「股票→行业」成分主数据与生效日，否则一切自下而上聚合都有幸存者偏差；② 首期标准化以**当日横截面分位**为主，时序分位对快照类数据一律标注 `n_sample` 并降置信，不得用短样本分位冒充；③ CN / HK 必须分表分口径，**不产出跨市场统一排序、不产出单一综合分**；④ 排序有效性未完成前瞻检验前，不得升格为配置建议。 |

**执行状态（2026-11-14）**：**阶段 0 / 1 / 2 已完成**。

- **阶段 0**（只读探测）：`tools/probe_sector_constituent.py` + §2.1 实测盘点 + §2.2.1 决策点。三项结论改变后续设计：① D2 成交额标定**通过**，拥挤度第一因子解禁、无需新表；② D1 完整 PIT 历史**不可得**（官方 Excel HTTP 508，主源仅 2021-12-13 起计入日期且无剔除记录）；③ 一致预期仅 25 个快照日，30 快照窗口尚未满。
- **阶段 1**：4 张新表 + `sector_layer.py` + 独立 systemd timer + 监控登记 + 39 项测试；全量回填峰值经一次 892MB → 236MB 修复后达标（§4.2 实施记录）。
- **阶段 2**：`regime.sector_constituent` PIT 版本链（5,222 行 / 31 行业）+ 变更日志 + 交易日 18:05 采集 + `sector_stage2_agg.py` 按月×按股票分批聚合 + 14 个指标激活 + 六维口径 + 三因子拥挤闸门。全量 `pytest` **757 passed / 497 subtests**；峰值经 617MB → 176MB 修复后达标（§4.2 阶段 2 实施记录）。
- **三项决策已定并落地**：阶段 2 指标起点**限 2021-12-13 起**；权重只落新表 `sector_constituent`、不回写 `stock_sector`；接受 PIT 边界，官方 Excel / 付费源单独立项。
- **剩余**：阶段 3（HK）、阶段 4（前瞻验证）；全量回填写库与服务器部署由用户择机执行。

---

# 第一部分 · 目标与需求（牵引）

## 1.1 范围与上位约束

主报告把五层链条定义为「① 宏观 → ② 资产配置 → ③ 市场状态 → ④ 行业配置 → ⑤ 个股」，第 ④ 层回答「超配哪个行业？」（§2.1）。本文分析的是**这个第 ④ 层能否由本系统自动、可度量地回答**。

必须遵守的上位约束（均来自主报告，本文不重新论证）：

| # | 约束 | 出处 |
|---|---|---|
| C1 | 目标是**度量状态**而非**预测涨跌**；输出用于「仓位与风格」判断，不是赌方向 | §0 判断 1、§1.2、§11 风险 3 |
| C2 | A 股与港股**必须分开建指标体系**；口径混用 = 结论错误 | §0 判断 4、§6.11、§9.4 |
| C3 | 分位化是通用语言；原子 → 维度 → 业务三层合成；方向只在 `members[].sign` 定义一次；`min_periods=250`；单成员权重上限 25%；首期等权且权重写字典 | §7.1、§7.2 |
| C4 | 分位必须 **as-of 计算、禁止事后刷写**；必须用**当时成分股**，保存历史截面快照 | §7.1、§9.4 头条风险、§5.7 |
| C5 | 状态定义不依赖未来，验证使用未来（前瞻 3/6/12 月）；不通过则回改规则 | §8.5 |
| C6 | 每个指标可追溯到计算口径与数据源；支持任意历史日期快照 | §1.4 可解释 / 可回溯 |

## 1.2 对 §6.10「行业比较打分卡」的评估

§6.10 原文：7 个维度（景气趋势·高 / 盈利修正·高 / 成长性·中 / 盈利能力·中 / 估值·中高 / 量价动量·中 / 拥挤度反向·高），输出「行业排序 + 超配/标配/低配建议 + 核心逻辑与风险」。

| # | 设计动作 | 冲突对象 | 判定 | 依据 |
|---|---|---|---|---|
| 1 | 输出「超配/标配/低配建议」 | C1，且**与主报告内部自相矛盾** | ❌ 根本性错误 | §0 判断 1 明确「回答『明天涨跌』不可行；回答『当前什么状态、风险多大』完全可行」；§11 风险 3 把「过度解读（把度量当预测）」列为高等级风险。但 §1.2 可度量目标表最后一行仍写「**哪个行业值得超配？** → 景气度+盈利修正+估值+拥挤度打分卡」。§6.10 继承了这个未被整改的预测性表述。**「超配」隐含对未来相对收益的判断**，是配置决策而非状态度量。 |
| 2 | 把「景气趋势」「盈利修正」设为**高**权重 | §9.1、§0 判断 2、§11 风险 4 | ❌ 目标与数据可行性脱节 | §6.2 自述中观数据「需外采或定向爬取…是本项目最大的数据成本项，建议单独立项评估」；§6.6 自述一致预期「需外采」；§9.3 把两者列为 P1/P2 外采项；§0 判断 2 明确「第四层的景气度需中观数据，是最大瓶颈，应单独立项评估」。**打分卡把最不可得的两项放在最高权重**，首期要么不可实现，要么用代理变量冒充 → §7.2 所批评的「权重幻觉」。 |
| 3 | 权重写成「高 / 中 / 中高」形容词 | C3 | ❌ 不可复现 | 无数值、无字典表、无 `params_hash`、无法单测或回归；§7.2 明确要求「权重写在字典表而非代码」「首期等权」。7 个维度直接压成一个排序，跳过了 L1 原子 → L2 维度 → L3 业务三层结构，正是 §7.2 批评过的「成员个数决定权重」同类问题。 |
| 4 | 无分位化 / 无最小样本 / 无覆盖率门禁 | C3、§5.7 | ❌ 口径缺失 | 未定义分位窗口、方向翻转、`min_periods`、`n_sample` 降置信、coverage、dispersion；而行业层恰恰有大量**短历史**数据（估值快照、资金流、一致预期均自 2026-09-30 起累积），不做样本门槛会产出看似精确、实为无意义的分数。 |
| 5 | 未定义分类体系与横截面对象 | §6.1、§6.11、C2 | ❌ 排序对象未定义 | §6.1 并列 5 套体系（申万/中信/GICS/恒生/概念板块），§6.10 未指定用哪套、多少个行业、A/H 是否同表排序。现状：CN 是 31 个申万一级（1999 起，且 21/12/3 三批起点不同），HK 只有 `stock_sector` 当前快照 + 113 个恒生三级行业**等权合成**（`SECTOR.UP_RATIO` remark 明确「粒度细 3.6 倍、两市场构造不同、数值不可直接比」）。放进同一张打分卡排序无意义。 |
| 6 | 未做 PIT 成分设计 | C4、§9.4 头条风险 | ❌ 幸存者偏差 | 主报告把「用今日成分股算历史宽度 → 历史虚高、分位失真」列为**最严重**风险，要求「必须用当时成分股计算，保存历史截面快照」。而现状 `stock_sector` 无日期列、刷新时 `DELETE` 旧成分再全量写（`get_stock_sector.py:31-32`），`sector_hierarchy` 也无 `as_of`。打分卡若基于当前成分回算历史，历史读数系统性虚高。 |
| 7 | 「核心逻辑与风险」自由文本 / 「卖方标准产出」 | C6、§1.4 | ❌ 不可验收 | 卖方打分卡依赖分析师产业调研与主观判断，其「核心逻辑」无法定义输入、计算模型与输出契约，无法单测、无法追溯、无法监控断供；若改由 LLM 生成，则引入不可回溯内容，直接违反「每个指标可追溯到计算口径与数据源」。 |
| 8 | 无验证闭环与降级路径 | C5、§7.2 | ❌ 缺闸门 | 权重是「建议」但没有「验证不通过怎么办」。§7.2 规定「二期按前瞻收益 IC 重估」，§8.5 规定「不通过则回改规则」。本项目已有多次量化检验的负面结论（OFI 分钟级 AUC 0.50–0.54、日线方向 AUC 0.44–0.54、多空博弈指标描述性无预测力），**在验证通过前把排序表述为「值得超配」风险极高**。 |

**延伸判断**：不只是 §6.10，§6.3（产能/库存周期四象限）、§6.4（产业链图谱 + Neo4j）、§6.5（生命周期 / CR3 / HHI / 波特五力）本质是**卖方研究框架**，不是可自动计算的指标。它们是阅读材料的目录，不是指标体系；若照单落地会造成范围蔓延。本文把它们整体移出首期指标层（§1.4）。

**评估结论：§6.10 的目标设计不合理，应当重设。** 问题不在权重调多少，而在**目标类型选错**（预测性配置建议）+ **目标与数据可行性脱节**。

## 1.3 目标重设

> **一句话目标**：把第 ④ 层从「给出行业配置建议」改为「对行业的**当前状态**做可度量、可回溯、分市场的横截面刻画，并显式标注数据可用性与风险闸门」。系统输出**状态读数、相对排序、风险闸门**，不输出超配/低配/买卖建议。

| ID | 目标 | 可度量验收 |
|---|---|---|
| **G1** | **状态可度量**：每个行业、每个交易日产出一组标准化读数（相对强弱与趋势 / 估值水位 / 资金 / 拥挤 / 基本面与预期 / 行业宽度），全部落到 0–100 分位或明确分类值 | 每个读数带 `raw_value` / `percentile_xsec` / `percentile_ts` / `n_sample` / `coverage` / `model_version` / `as_of`；口径登记在字典表，可追溯到表·列·公式 |
| **G2** | **横向可比**：同一 `market` + 同一分类体系下可做当日横截面排序 | 排序键含 `(market, classification, trade_date)`；`rank_n` 记录当日有效行业数；CN/HK 物理分表、不合并；申万 31 行业三批起点不同的分母变化被记录 |
| **G3** | **风险与不确定性显式化**：拥挤、数据覆盖不足、样本过短、口径断点四类状态必须输出闸门 | 输出 `gate_flags[]`、`applicable`、`confidence`；触发闸门的行业**只出读数、不出排名**，不确定性不许藏进分数 |
| **G4** | **可验证且可降级**：排序有效性必须走前瞻检验；未通过前排序只作「当前状态描述」 | 分层收益分离 + Spearman IC + 单调性 + 各档样本占比，样本外 / block bootstrap；不通过则维持等权、保持「状态描述」，不得用于配置；权重二期才按 IC 重估 |

**取代 §6.10 输出的具体交付物**（见 §3.6）：
`状态侧读数`、`风险侧读数`、`2×2 状态矩阵（象限名只描述当前状态）`、`当日横截面排名 + 置信度`、`四类闸门`。
**不产出单一综合分**——理由见 §3.5：状态侧与风险侧是异质信息，加权求和会「用便宜抵消拥挤」，制造伪精确，这正是 §7.2「高冗余或异质时扁平平均不产生信息」的场景。

## 1.4 非目标（明确排除，防止范围蔓延）

| 排除项 | 原因 |
|---|---|
| 超配 / 标配 / 低配建议、买卖建议、「核心逻辑与风险」文本 | 违反 C1 / C6；属独立决策产品，不在本系统度量层 |
| 预测行业未来收益、宣称排序「有效」 | C5；验证通过前只作状态描述 |
| 中观景气数据（量/价/库存/产能/进出口/政策）外采 | §6.2 自述最大成本项；§9.3 P2「单独立项评估」；主报告 §0 判断 2 已定调。**单独立项，不在本方案范围** |
| 产业链图谱（Neo4j）、产能/库存周期四象限、生命周期 / CR3 / HHI / 波特五力 | 定性研究框架，无可计算输入；不进指标层 |
| A/H 统一 GICS 打分、跨市场统一排序 | 违反 C2；GICS 仅可作展示层标签，不作计算口径 |
| 目标价修正、盈利预测修正幅度、净利润/营收一致预期 | 现免费源 `stock_profit_forecast_em` 只有 EPS + 评级五档（§7.8 已知限制②），无这些字段；需外采评估 |
| 北向资金按行业拆分 | 2024-08-16 官方停披露，`daily_northbound_flow` 已封存且是市场级、无个股明细 |
| 前端页面 / UI 契约 | 另立文档；本文只定义到表与字段级出向契约 |

## 1.5 需求与验收口径

| 类型 | 要求 |
|---|---|
| 覆盖 | 首期 CN 申万一级 31 行业（日频）；HK 独立分期，不混排 |
| 更新 | 日频，收盘后、且不早于现有 18:45 市场状态计算（依赖其产出的 `sector_daily`） |
| 历史回溯 | 价格类可回溯至 1999-12-30（三批起点须标注）；快照类**只从上线日起累积，不得回填、不得伪装成 PIT** |
| 可解释 | 每个指标在字典表登记定义 / 输入 / 公式 / 方向 / 频率 / 源 / `params_hash` / `breaks` / `pit_capable` |
| 可回溯 | 所有分位 as-of 计算，禁止事后刷写；历史任意日期可重放 |
| 不可用时 | 明确 `NULL` + `quality` / `gate_flags`，**禁止用短样本分位填充**（§7.1、§5.7） |
| 验证 | 上线前跑 `git worktree` 新旧口径逐值对比；上线后跑前瞻 IC；每档样本占比必须报告（§8.5 通用条款） |

---

# 第二部分 · 数据基础（现状 → 缺口）

## 2.1 现有数据盘点

> 主报告 §9.1 写「④ 行业配置：可得性**中低**、中观数据需外采、最大瓶颈」，§3.4 初始盘点 5 项全为 ❌。**这些结论已被 2026-09-30 ~ 10-08 的落地覆盖**（`market_profile_collection_plan.md` §7.11 / §7.13 / §7.14 / §7.8）。可行性判断必须以现状为准。

| # | 表 / 字段 | 粒度与起点 | 可支撑 | 状态判定 |
|---|---|---|---|---|
| S1 | `daily_benchmark` `CN.SW*`（31 个申万一级行业指数 `last_price` / `turnover`） | 指数×日；1999-12-30 起，**横截面分母实测 16 → 28 → 31**（见下方实测盘点） | 相对强弱、动量、RRG、行业宽度（指数口径）、长历史时序分位 | ✅ **可直接用（价格类 + `turnover`）**；阶段 0 已标定 `turnover` 为**亿元**且完备覆盖全 A（合计/全市场 = 0.998~1.000），§7.11 决策 5 的「只用价格类字段」保守限制在本层可解除；但**不得与个股 `amount`（元）混用** |
| S2 | `regime.sector_daily`（15.5 万行） | 交易日×申万一级；`close/change_pct/ret_20d/ret_60d/rs_20d/rs_rank/mom_rank/turnover/fund_flow_net/fund_flow_net_5d/flow_rank/mapped_ths_count` | 大部分价格/资金原子的 raw 值 | ✅ **可直接用**，由 `market_state_daily.calc_sector_daily` 产出；`rs_20d` = 行业 20 日收益 − 31 行业等权均值（横截面均值恒 0） |
| S3 | `regime.sector_valuation_snapshot` | 快照日×31 行业；`pe_static/pe_ttm/pb/div_yield/constituent_count` | 行业估值横截面分位、相对市场估值 | ⚠ **仅 2026-09-30 起逐日累积**（源 `sw_index_first_info` 无历史，断档即永久缺失）→ 横截面分位可用；**时序分位首期不可用** |
| S4 | `regime.sector_fund_flow` + `regime.sector_mapping` | 交易日×同花顺约 90 细分行业；90 行映射覆盖全部 31 申万一级 | 行业资金净流入、排名、量价-资金背离 | ⚠ 源只给「即时」快照、无历史（2026-09-30 起）；跨口径 join 必须走 `sector_mapping`，且只纳入 `confirmed=true` 或 `corr≥0.6`（§7.13 实测：相关性会被风格因子误导） |
| S5 | `regime.analyst_forecast_snapshot` | 快照日×A 股约 2,438 只；`fy1..fy4_eps` / `fy1..fy4_year` / 评级五档 / `report_count` | 盈利修正宽度（市场级 `FORECAST.REVISION_BREADTH` **已落地**，30 快照滚动） | ⚠ 2026-09-30 起累积、仅 A 股、无净利润/营收/目标价、无行业键 → **下沉到行业需成分映射 + ≥30 快照** |
| S6 | `financial_indicator` | 股票×报告期；`revenue/net_profit/gross_profit_rate/roe/net_profit_yoy/revenue_yoy/.../announce_date` | 行业基本面聚合（PIT 按 `announce_date` 取用） | ✅ **可直接用**（季度，周一全量）；⚠ 频率台阶，须按混合频率处理 |
| S7 | `a_daily_quote` / `hk_daily_quote`（`v_daily_quote`） | 股票×日；QFQ `close`、`amount`、`turnover_rate`、`pe_ttm_ratio`、`pb_ratio`、`circular_market_val` | 行业内宽度 / 离散度 / 估值离散 / 成交额占比 / 拥挤度 | ✅ **数据有**，但**缺股票→行业的 PIT 映射**（见 D1） |
| S8 | `stock_sector` | 股票×板块多值，**无日期列**，刷新时先 DELETE 全量写 | 板块归属（当前快照） | ❌ **不可直接用于历史**：指数/概念成分、非行业分类；⚠ 阶段 0 实测 **`weight` 12,509 行全部为 NULL**（`get_stock_sector_weight.py` 的 PDF 权重未落本机库）；`INDUSTRY` 7,992 行 / 196 个板块是 BaoStock 证监会代码（`C39` 等）+ 富途港股板块，**不是申万分类**；无 `as_of` → 前视 / 幸存者偏差 |
| S9 | `sector_hierarchy` | 板块一行；`parent_code` = GICS 11 部门 | 展示层 A/H 统一标签 | ⚠ 静态、**未挂调度**、非股票级、无 `as_of`（`build_sector_hierarchy.py`） |
| S10 | `daily_ggt_hold` / `daily_margin_balance` / `daily_short_selling` / `daily_buyback_event` | 股票×日 | 港股南向按行业、A 股两融/卖空按行业 | ⚠ 可按 `stock_code` join `stock_sector`，但同样受当前成分偏差约束；阶段 0 实测南向 **2026-04-20 起仅 136 个交易日**（不足 1 年），回填不可行 |
| S11 | `daily_market_turnover` / `a_daily_market_turnover` / `daily_northbound_flow` | **市场级，无 `stock_code`** | 分母（成交额） | ⚠ 不能按股票关联；北向已封存 |

#### 实测盘点（阶段 0，2026-11-14）

复跑命令：`python3 tools/probe_sector_constituent.py --sources`（只读，不写库）。以下数字取代主报告 §3.4 的初始盘点与本文初稿中的估计值。

| 表 | 起点 | 终点 | 有效日数 | 行数 / 说明 |
|---|---|---|---|---|
| `daily_benchmark CN.SW*` | 1999-12-30 | 2026-11-11 | 6,498 | 144,305 / 31 行业 |
| `regime.sector_daily` | 1999-12-30 | 2026-11-11 | 6,498 | 156,468 / 31 行业 |
| `sector_valuation_snapshot` | 2026-09-30 | 2026-11-12 | **27** | 837（每日 31 行完整） |
| `sector_fund_flow` | 2026-09-30 | 2026-11-13 | **27** | 2,430 |
| `sector_mapping` | — | as_of 2026-10-04 | — | 90 行，**全部 `confirmed=true`** |
| `analyst_forecast_snapshot` | 2026-09-30 | 2026-11-12 | **25** | 60,114 / 最新日 2,384 只且 `fy1_eps` 全非空 |
| `financial_indicator` | 1989-12-31 | 2026-09-30 | — | 373,098 / 8,414 只 |
| `daily_ggt_hold` | 2026-04-20 | 2026-11-12 | 136 | — |
| `stock_sector` | — | — | — | INDUSTRY 7,992 / INDEX 2,125 / CONCEPT 2,392；`weight` **12,509 行全 NULL** |

**横截面分母 `rank_n` 实测**：2005-06-01 = 16、2010-06-01 = 16、2014-02-20 = 28、2014-02-21 = 28、2018-06-01 = 31、2021-12-13 = 31、2026-11-11 = 31。比主报告 §7.13 的「21/12/3 三批」更细：早期只有 16 个行业有行情，2014 前后增至 28，2018 起才齐 31。

**D2 标定实测**：`sum(31 行业 turnover) / (a_daily_market_turnover.total_turnover / 1e8)` 在 2026-11-02 ~ 11-11 共 8 个交易日为 **0.998088 ~ 0.999661**。判定：`sector_daily.turnover` 单位是**亿元**、31 行业互斥且近乎完备覆盖全 A（≥99.8%，差 0.1% 疑为 B 股 / 北交所未纳入申万分类）。**不需要**新建 `regime.sector_amount_daily` 或成分股 `amount` 聚合表；但同源占比的分母须取「当日有效行业合计」，早期行业数不足 31 时占比不可与后期直接比。

**三条直接推论**：
1. 拥挤度第一因子 `AMT_SHARE_PCT` 可立即出**时序分位**（`turnover` 有 6,498 日长历史），阶段 1 不再被 D2 阻塞；
2. 估值 / 资金流仅 27 日 « `min_periods=250`，时序分位一律 `NULL`；
3. `REVISION_BREADTH` 的 30 快照窗口**尚未满**（25 个快照日），按工作日节奏约 2026-11 下旬填满 → 阶段 2 首日仍须标 `n_sample` 并降置信。

**关键结论**：行业层「指数 + 估值 + 资金流 + 横截面 + 一致预期」五件套已落地，但**全部集中在 CN 申万一级**；港股行业层基本空白（只有 `SECTOR.UP_RATIO` 用 113 个恒生三级行业等权合成）。瓶颈已从「数据有无」转为：

1. **成分主数据 PIT 缺失**（最严重，违反 C4）；
2. **快照类数据历史深度不足**（估值 / 资金流 / 一致预期均自 2026-09-30 起，时序分位不可用）；
3. **A/H 分类口径未统一且不可比**（HK 粒度细 3.6 倍）；
4. **资金流是行业级快照**，无个股主力资金明细，无法自下而上重算。

## 2.2 需补采 / 需建设的数据清单

| ID | 数据项 | 落点 | 源与频率 | 为什么必须 | 优先级 |
|---|---|---|---|---|---|
| **D1** | **行业成分主数据（PIT）**：`(market, classification, sector_code, stock_code, eff_from, eff_to, weight, source)` + 变更日志 | 新建 `regime.sector_constituent`（+ `..._change`） | **阶段 0 已定主源**：AKShare `index_component_sw(symbol=<sw_code>)`（申万官方指数详情），31 次调用；返回 `证券代码 / 证券名称 / 最新权重 / 计入日期`，4 个抽测行业权重合计 99.99~100.00%。备选（**均不采为主源**）：`index_stock_cons`（`纳入日期` 含 `1900-01-01` 哨兵值）、`stock_industry_change_cninfo`（逐股查询，仅作增量校验）、东财 `stock_board_industry_name_em`（实测 ConnectionError）、`sw_index_third_cons`（实测列数失配）。完整 PIT 历史源 `stock_industry_clf_hist_sw` **不可用**（本机 SSLError；绕证书直连源站返回 **HTTP 508 / 0 字节**） | 所有自下而上聚合（行业内宽度、离散度、估值离散、换手率、财务聚合、盈利修正下沉）都依赖它；C4 要求「用当时成分股」 | ✅ **阶段 2 已落地**（`get_sector_constituent.py` 交易日 18:05；5,222 行 / 31 行业） |
| **D2** | ~~行业成交额口径标定 / 必要时新建 `sector_amount_daily`~~ → **已完成，无需建设** | 无新表，仅标定结论 | `regime.sector_daily.turnover` ↔ `a_daily_market_turnover.total_turnover` | 实测 8 日 ratio 0.998088~0.999661 → 单位亿元、完备覆盖全 A；拥挤度第一因子可直接用，§7.11 决策 5 的保守限制在本层解除 | ✅ **阶段 0 关闭** |
| **D3** | **一致预期快照的持续性与可选补字段**：逐日不可断（监控 `day/1`）；可选 `fy1_net_profit` / `fy1_revenue` / `target_price` | 现有 `regime.analyst_forecast_snapshot` | 东财 `stock_profit_forecast_em`（已落地 18:40） | 无历史序列、漏采一天即永久断档；行业修正宽度需 ≥30 快照且要按 `report_count` 做覆盖度门禁 | **P1**（不新增表，只加监控 + 可选列） |
| **D4** | **港股行业层基础**：`sector_hierarchy` 补 `as_of` 并挂调度；自算 `regime.hk_sector_index_daily`（等权 + 市值加权两版） | 1 张新表 + 1 个调度 | `hk_daily_quote` × `stock_sector` | HK 无官方行业指数表；自算指数是 HK 侧一切指标的前置；两版并列以标注方法论差异 | **P1 · 阻塞阶段 3** |
| **D5** | **行业估值 / 资金流 / 一致预期的历史深度** | 现有表逐日累积 | 已挂 18:15 / 15:35 / 18:40 | 只能靠时间累积，**越早越好**；断档即永久缺失。若未来采购付费历史源，属独立决策 | **P0（运维纪律，非开发）** |

### 2.2.1 阶段 0 探测结论：D1 的 PIT 边界（决策点）

探测结论是「**免费源只有当前成分 + 2021-12-13 起的计入日期，没有完整历史成分与剔除记录**」：

- `index_component_sw` 抽测 4 个行业（801010 / 801780 / 801950 / 801980），`计入日期` **最小值一律 2021-12-13**（申万 2021 版分类启用日），最大到 2026-10；它不是「从上市日起的成分变动史」，而是新版分类下的计入记录。
- 接口**不返回剔除日期**。`eff_to` 只能靠「逐日快照对比，股票从某行业消失时回写 eff_to」累积生成；上线首日没有任何 `eff_to` 可用 → **2021-12-13 之前的历史成分无法还原**。
- 唯一可能提供完整变动史的官方 Excel 源返回 HTTP 508，本机不可用（是否在生产服务器重试另议）。
- `stock_sector` 的 7,992 行 `INDUSTRY` 是证监会 / 富途板块，与申万 31 行业**不同分类**，不能拿来做申万行业聚合；且其 `weight` 全 NULL。

因此按文档原定铁律处理：**不得**用当前成分回算历史后声称 PIT。三条待用户决策：

| 决策 | 选项 | 建议 |
|---|---|---|
| **A. 阶段 2 指标的有效起点** | ① 限定在 **2021-12-13 之后**（PIT 可信段）；② 允许 1999 起回算但全程标 `pit_capable=false` / `survivorship_bias=true` / 降置信 | **建议 ①**：阶段 2 的行业内宽度、离散度、财务聚合、盈利修正下沉只出 2021-12-13 起读数；长历史（1999 起）仍由**不依赖成分**的价格类指标（A 组、C 组、拥挤度第一因子）承担，两段时间尺度分开看，不混排 |
| **B. 权重落点** | ① 只落新建 `regime.sector_constituent.weight`；② 顺带回写 `stock_sector.weight` | **建议 ①**：`stock_sector` 是前端展示层共用的当前快照，且分类口径不同（证监会/富途），回写会造成「不同分类的权重混在一列」 |
| **C. 完整 PIT 历史** | ① 接受 2021-12-13 边界；② 在服务器重试官方 Excel / 评估付费源 | **建议 ① 先落地、② 单独立项**；不影响阶段 1 开工 |

> 无论选哪项，`pit_capable` / `survivorship_bias` / `n_sample` / `confidence` 四个字段必须随读数透出，早期年份单独报告（§8.5 第⑤条、§5.1 幸存者偏差段）。诚实的空缺优于虚假的精确。

## 2.3 明确不采 / 单独立项

| 项 | 处置 |
|---|---|
| 中观景气数据（六类：量/价/库存/产能/进出口/政策事件） | **单独立项**，与本方案解耦；未立项前「景气趋势」维度整体不进指标体系 |
| 产业链图谱 / Neo4j、产能库存周期、CR3/HHI、波特五力 | 从指标层删除（研究框架，无可计算输入） |
| 目标价一致预期、净利润/营收一致预期、朝阳永续 / Wind 采购 | 外采决策，非本方案 |
| 北向资金行业明细 | 源已停披露且无个股明细，不追 |
| A/H 统一 GICS **计算口径** | 不做统一打分；GICS 仅作展示标签 |

## 2.4 数据可用性铁律

1. **PIT**：任何涉及成分股的聚合，成分按 `trade_date ∈ [eff_from, eff_to)` 取用；财务按 `announce_date ≤ trade_date` 取最新报告期；分位 as-of 计算，禁止事后刷写。
2. **A/H 分离**：`market` 是主键列，不得在同一查询里对 CN/HK 混合排名、混合分位、混合校准。
3. **历史深度分级**：长历史（价格类，1999 起）可出时序分位；短历史（快照类，2026-09 起）首期**只出横截面分位**，时序分位置 `NULL` 并写 `n_sample`；`n_sample < 750` 一律降置信（§5.7 退化规则），`< 250` 一律不出分位（§7.1）。
4. **跨口径映射**：同花顺资金流 → 申万必须走 `sector_mapping` 且只纳入 `confirmed OR corr≥0.6`；`mapped_ths_count` 必须随读数透出（§7.13）。
5. **横截面分母时变**：实测 `rank_n` = 16（≤2010）→ 28（2014 前后）→ 31（2018 起），比「21/12/3 三批」更细；`rank_n` 必须逐日记录，早期年份不得与后期直接比排名、比成交额占比（分母不同）。

---

# 第三部分 · 指标体系（内容主体）

## 3.1 分层、命名与输出契约

```
L1 原子指标（逐行业逐日，raw + 横截面分位 + 时序分位）
  → L2 维度读数（6 组，0–100 + coverage / dispersion / confidence）
      → L3 业务读数（状态侧 / 风险侧 / 状态矩阵 / 横截面排名 / 四类闸门）
```

**命名**：`SEC.<DOMAIN>.<NAME>`；`DOMAIN ∈ {TREND, VAL, FLOW, CROWD, FUND, BREADTH}`；维度 `SEC.DIM.<DOMAIN>`；业务 `SEC.BIZ.<NAME>`。
**粒度主键**：`(market, classification, sector_code, trade_date)`。CN 的 `classification='SW_L1'`，HK 的 `classification='HS_L3'`（或自算指数对应的分类），二者永不混排。

**为什么必须新建表**（不是复用 `indicator_value`）：`regime.indicator_value` 主键是 `(indicator_code, market, trade_date)`（`sql/schema.sql:1730`），**不含 `sector_code`**；`indicator_dict.scope='SECTOR'` 目前只是 8 个市场级聚合指标（§`market_state_daily.py:705-736`），没有逐行业明细。行业层需独立的实体键。也不把估值列掺进 `sector_daily`——§7.14 已明确「采集层只落原始快照，`sector_daily` 不掺估值列，避免计算层 upsert 把采集值覆盖成 NULL」。

| 新表（按 `sql/schema.sql` 末尾表号顺延，当前末尾为第 20 号 `force_round_daily`） | 用途 | 关键列 |
|---|---|---|
| `regime.sector_indicator_dict` | 指标字典（口径唯一真源） | `indicator_code PK`、`domain`、`market_scope`、`classification`、`unit`、`direction`、`freq`、`source`、`formula`、`model_key`、`params`、`params_hash`、`breaks`、`min_periods`、`pit_capable`、`is_active`、`remark`（三段式：定义/判读/数据） |
| `regime.sector_indicator_value` | L1 原子时序 | PK `(market, classification, sector_code, trade_date, indicator_code)`；`raw_value`、`percentile_xsec`、`percentile_ts`、`n_sample`、`coverage`、`quality`、`model_version`、`extra JSONB`（含 `survivorship_bias` / `mapped_ths_count` / `rank_n`） |
| `regime.sector_score_daily` | L2 维度 | PK `(market, classification, sector_code, trade_date, dimension_code)`；`score 0–100`、`coverage`、`dispersion`、`confidence`、`members_used[]`、`params_hash` |
| `regime.sector_biz_daily` | L3 业务 | PK 同上；`state_side`、`risk_side`、`quadrant`、`rank_no`（避开 SQL 关键字 `rank`）、`rank_n`、`coverage`、`gate_flags[]`、`applicable`、`confidence`、`params_hash` |

`regime.sector_constituent`（D1）与 `regime.hk_sector_index_daily`（D4）属数据层，同期建表。全部新表须同步 `sql/schema.sql` **与** `regime_schema.py`（两处逐字一致），并登记 `monitor_table_config`。

## 3.2 计算模型字典（指标「计算模型」的可复现单元）

> 指标表内只引用 `M*`，参数写在字典表 `params`；`params_hash` 变化触发该指标历史重算（§`schema.sql:2212`）。

| ID | 模型 | 公式 / 步骤 | 边界与护栏 |
|---|---|---|---|
| **M1** | 横截面分位 | `p_xsec = 100 × (n_less + 0.5·n_equal) / n_valid`，在**同一 `(market, classification, trade_date)`** 的 `n_valid` 个行业内取中位秩（§7.1 平均秩，不用上界） | `n_valid < 10` 不给分位；并列值用中位秩；分母 `n_valid` 写入 `extra.rank_n`（申万三批起点导致分母时变） |
| **M2** | 时序分位 | `p_ts = 100 × (历史样本中 ≤ 当前值 的比例)`，as-of 计算；窗口按维度定：估值 / 拥挤 expanding；资金 / 宽度 滚动 10 年；趋势不取分位（§7.1 / §5.7） | `min_periods=250` 未达标置 `NULL`；`n_sample < 750` 降置信；winsorize `[2,98]` **只用于稳健估计参考分布**，不对最终分位截尾；水平型成分先一阶差分 / 归一（§5.7 L2-a ③） |
| **M3** | 相对强度 RS | `rs_N(t) = ret_N(sector,t) − mean_{s∈S_t} ret_N(s,t)`；`S_t` = 当日有效行业集；`ret_N = close_t/close_{t−N} − 1` | 横截面均值恒 0；**基准是 31 行业等权均值，不是大盘指数**（§7.11 决策 3：库内 `daily_benchmark` 无沪深300）。主报告 §6.8 写「行业指数/大盘指数」不可照搬 |
| **M4** | 归一与滚动 | 净额类先归一：`net_norm = net_1d / (sum(abs(net_1d)) 或行业成交额)`；`rolling_sum(N, min_periods=N)` | **窗口不足不给值**（§7.11 教训：`FLOW_NET_5D` 不足 5 日为空，防部分窗口当完整窗口）；资金净额随市场规模漂移，未归一不得取时序分位（§5.7 L2-a ②） |
| **M5** | 条件命中 / 闸门 | `hit = Σ 1{cond_i}`；`gate = f(hit, coverage)` | 用于拥挤度等「共振型」读数，**不取加权平均**（乘积会把任一为 0 的因子整体归零，且 0–100 分位相乘后不可读）；对齐 §7.2「规则判定」结构 |
| **M6** | PIT 成分聚合 | `JOIN sector_constituent c ON c.stock_code = q.stock_code AND t ∈ [c.eff_from, c.eff_to)`；财务取 `max(report_date) WHERE announce_date ≤ t`；按 `median` / `mean` / `count-if` 聚合 | 覆盖度 `coverage = 有值成分数 / 成分数`；`coverage < 0.6` 作废并清库（复用 `market_state_daily._coverage_gate` 标定，0.6 是实测值，不得随手调）；`pit_capable=false` 的段标注 `survivorship_bias=true` |
| **M7** | 快照差分（盈利修正） | 同一 `stock_code`、同一 `fy1_year` 的相邻两次快照 `fy1_eps` 比较：`Δ > +0.5% → 上调(+1)`，`< −0.5% → 下调(−1)`，否则 0；`breadth = (Σ上调 − Σ下调) / Σ可比样本 × 100`，滚动 30 快照 | 沿用已落地的 `FORECAST.REVISION_BREADTH` 口径（`market_state_daily.py:2454-2486`）下沉到行业；财年标签不一致的样本剔除；行业 `report_count` 覆盖门禁（覆盖成分 <30% 或有效样本 <5 家 → `NULL`）；快照日本身即 PIT，按 ffill 对齐交易日不产生前视 |
| **M8** | 自算行业指数（HK） | `idx_t = Σ w_i · (close_i,t / close_i,t−1 − 1)`，`w` 取等权或流通市值加权，两版并存 | 沿用 `SECTOR.UP_RATIO` HK 侧的等权合成思路（`market_state_daily.py:1404`）；须设流动性门槛剔除仙股（§7.6 已知口径限制 4）；粒度与 CN 不可比，只各自看分位 |

**方向与冗余**：方向统一只在 `sector_indicator_dict.direction` + `dimension_dict.members[].sign` 定义一次，消费端不得二次翻转（§7.1）；同维度内成员**一阶差分相关 >0.8** 视为冗余，只留历史更长 / 口径更纯的一个（§7.2）；单成员权重上限 25%，成员 <4 时走规则判定或单锚封印（§7.2 三种标准结构）。

**维度极性约定**（避免「高分代表什么」歧义）：

| 侧 | 维度 | 极性 |
|---|---|---|
| 状态侧 | TREND / FLOW / FUND / BREADTH | 高分 = 该维度**更强 / 更健康** |
| 风险侧 | VAL / CROWD | 高分 = **更贵 / 更拥挤**（风险更高） |

## 3.3 数据 → 指标依赖矩阵（决定分期）

| 指标组 | S1 价格 | S2 横截面 | S3 估值快照 | S4 资金/映射 | S5 一致预期 | S6 财务 | S7 个股行情 | D1 成分 | D2 成交额 | D4 HK 指数 |
|---|---|---|---|---|---|---|---|---|---|---|
| A 相对强弱与趋势 | ✅ | ✅ | — | — | — | — | — | — | — | HK 需 ✅ |
| B 估值（横截面） | — | — | ✅ | — | — | — | — | — | — | HK 需 ✅ |
| B 估值（时序 / 离散） | — | — | ✅ | — | — | — | ✅ | ✅ | — | — |
| C 资金 | — | ✅ | — | ✅ | — | — | — | — | △（可用 `sum(abs(net))` 过渡） | ✅（南向） |
| D 拥挤 | ✅ | ✅ | ✅ | — | — | — | ✅ | ✅ | ✅ | ✅ |
| E 基本面与预期 | — | — | — | — | ✅ | ✅ | — | ✅ | — | — |
| F 行业宽度 | ✅（指数口径） | ✅ | — | — | — | — | ✅（个股口径） | ✅ | — | ✅ |

## 3.4 原子指标（L1）逐组定义

> 表内「输入」列格式为 `表.列（起点 / 频率）`；「输出」列格式为 `值域 · 粒度 · 可用性约束`。所有指标落 `regime.sector_indicator_value`。

### A 组 · 相对强弱与趋势 `SEC.TREND.*`（状态侧）

**组定义**：只用行业指数价格序列，刻画行业相对同侪的位置与位置变化。长历史（CN 1999 起），是首期最扎实、可立即开工的一组。**极性：高分 = 相对更强**。

| 代码 | 定义 | 输入 | 计算模型 | 输出 / 可用性 |
|---|---|---|---|---|
| `SEC.TREND.RET_20D` / `RET_60D` | 行业 20 / 60 交易日收益 | S1 `daily_benchmark.last_price`（1999 起，日） | `close/close.shift(N)−1`，×100 | % · 行业×日 · 直接取 S2 `ret_20d/ret_60d`；三批起点前为 NULL |
| `SEC.TREND.RS_20D` / `RS_60D` | 超额相对强度（对 31 行业等权均值） | 同上 | **M3** | %（横截面均值恒 0）· 行业×日 · S2 已有 `rs_20d`；`RS_60D` 新增；**不是行业指数/大盘指数** |
| `SEC.TREND.RS_XSEC_PCT` | 相对强度的当日横截面分位 | `RS_20D` | **M1** | 0–100 · 行业×日 · RRG 横轴；`n_valid` 随三批起点变化 |
| `SEC.TREND.RS_MOM_20D` | RS 分位的 20 日变化（位置加速度） | `RS_XSEC_PCT` | `p(t) − p(t−20)` | −100–100 · 行业×日 · RRG 纵轴；不足 21 日为 NULL |
| `SEC.TREND.RRG_QUADRANT` | 四象限状态 | 上两行 | **M5**：`rs_pct≥50 & Δ≥0 → 领先`；`<50 & Δ≥0 → 走强`；`≥50 & Δ<0 → 走弱`；`<50 & Δ<0 → 落后` | 分类值（不分位）· 行业×日 · ⚠ **横截面分位近似 RRG，非标准 JdK RS-Ratio/Momentum**，必须在字典 `remark` 标注 |
| `SEC.TREND.MOM_RANK` | N 日动量当日排名 | `RET_20D` | `rank(method='min')` | 1..`rank_n` · 行业×日 · S2 已有 `mom_rank` |
| `SEC.TREND.DISPERSION_INTRA` | 行业内成分股 20 日收益横截面标准差 | S7 `a_daily_quote.close` + **D1** | **M6** + `std(·)` | % · 行业×日 · **依赖 D1（阶段 2）**；`coverage<0.6` 作废 |

### B 组 · 估值 `SEC.VAL.*`（风险侧）

**组定义**：刻画行业估值水位，只作风险侧读数，**不用「便宜」去抵消拥挤**。**极性：高分 = 更贵**。

| 代码 | 定义 | 输入 | 计算模型 | 输出 / 可用性 |
|---|---|---|---|---|
| `SEC.VAL.PE_TTM` / `PB` / `DIV_YIELD` | 行业估值原始值 | S3 `sector_valuation_snapshot`（2026-09-30 起，日快照） | 原值直取 | x / x / % · 行业×日 · **不得掺进 `sector_daily`**（§7.14） |
| `SEC.VAL.PE_XSEC_PCT` / `PB_XSEC_PCT` | 当日 31 行业估值横截面分位 | 上三行 | **M1**（PE/PB 越高分位越高；股息率取 `100−p`） | 0–100 · 行业×日 · **首期主用口径**，不依赖历史深度 |
| `SEC.VAL.REL_TO_MARKET` | 行业 PE / 31 行业 PE 中位数 | `PE_TTM` | 比值 | ratio · 行业×日 · 与 XSEC 高度冗余，按 §7.2 去重后**只保留一个**（默认保留 XSEC，本项降级为校验项） |
| `SEC.VAL.PE_TS_PCT` | 行业 PE 自身历史分位 | `PE_TTM` 历史 | **M2**（expanding，全样本） | 0–100 · 行业×日 · 🔴 **首期不可用**：起点 2026-09-30、`<250` 观测 → 置 `NULL` + `n_sample`，待累积；不得用小样本分位填充 |
| `SEC.VAL.INTRA_DISPERSION` | 行业内个股 PE-TTM 分位离散度 | S7 `a_daily_quote.pe_ttm_ratio` + **D1** | **M6** + 横截面 std | 0–100 · 行业×日 · **依赖 D1**；个股估值字段 2018 起，早期为 NULL |

### C 组 · 资金 `SEC.FLOW.*`（状态侧）

**组定义**：刻画行业资金净流与量价配合。**极性：高分 = 资金面更强**。

| 代码 | 定义 | 输入 | 计算模型 | 输出 / 可用性 |
|---|---|---|---|---|
| `SEC.FLOW.NET_1D` | 行业资金净流入（同花顺映射汇总） | S4 `sector_fund_flow.net_inflow` + `sector_mapping`；S2 `fund_flow_net` | 只纳入 `confirmed OR corr≥0.6`；多条 THS → 同一 SW 求和；透出 `mapped_ths_count` | 亿元 · 行业×日 · S2 已有；须守恒（31 行业合计 = 市场级 `SECTOR.FLOW_NET`，§7.13 已实测） |
| `SEC.FLOW.NET_5D` / `NET_20D` | 近 N 日累计净流入 | `NET_1D` | **M4** `rolling_sum(N, min_periods=N)` | 亿元 · 行业×日 · 不足窗口置 NULL；20D 需 ≥20 快照日（2026-10 下旬起才有值） |
| `SEC.FLOW.NET_NORM` | 归一净额 | `NET_1D` + **D2** 行业成交额 | `net_1d / 行业成交额`（或 `net_1d / sum(abs(net_1d))` 作过渡） | ratio · 行业×日 · **必须先归一才能取时序分位**（§5.7 L2-a ②） |
| `SEC.FLOW.XSEC_PCT` / `FLOW_RANK` | 资金净流横截面分位 / 排名 | `NET_NORM` 或 `NET_1D` | **M1** / `rank` | 0–100 / 1..n · 行业×日 · S2 已有 `flow_rank` |
| `SEC.FLOW.PRICE_FLOW_GAP` | 量价-资金背离 | `rs_rank` / `flow_rank`（S2） | `gap = rs_rank − flow_rank`；正 = 价格排名落后于资金排名（资金领先），负 = 价格领先而资金未跟 | −n..n · 行业×日 · **纯描述性读数，不作预测信号**（与项目既有「指标无预测力」结论一致） |
| `SEC.FLOW.SOUTHBOUND_NET` | 港股南向资金按行业净流入 | S10 `daily_ggt_hold.est_net_inflow` + **D1/D4** | **M6** 求和 | 亿港元 · HK 行业×日 · 仅近 1 年、当前成分回算偏差 → `survivorship_bias=true` |
| `SEC.FLOW.MARGIN_NET` | A 股两融净买入按行业 | S10 `daily_margin_balance.rz_net` + **D1** | **M6** 求和 | 亿元 · CN 行业×日 · 依赖 D1 |

### D 组 · 拥挤度 `SEC.CROWD.*`（风险侧）

**组定义**：§6.9 原文「成交额占比分位 × 换手率分位 × 估值分位」用「×」含义不明。本设计明确改为**三因子条件命中计数**（§M5），不做乘积、不做加权平均——乘积会让任一因子为 0 时整体归零，且三个 0–100 分位相乘后不可读；加权平均则违反「单成员权重上限 25%」。**极性：高分 = 更拥挤**。

| 代码 | 定义 | 输入 | 计算模型 | 输出 / 可用性 |
|---|---|---|---|---|
| `SEC.CROWD.AMT_SHARE_PCT` | 行业成交额占比的时序分位 | S2 `sector_daily.turnover`（阶段 0 已标定：亿元、1999 起 6,498 日长历史） | 占比 = `行业成交额 / Σ 当日有效行业成交额`（同源比例，量纲自消）→ **M4** → **M2**（滚动 10 年） | 0–100 · 行业×日 · ✅ **阶段 1 即可用**，是唯一在短历史期仍能出时序分位的因子；早期 `rank_n<31` 段占比不可与后期比 |
| `SEC.CROWD.TURNOVER_PCT` | 行业换手率分位 | S7 `a_daily_quote.turnover_rate`（流通市值加权）+ **D1** | **M6** → **M2** | 0–100 · 行业×日 · 🔴 **依赖 D1，阶段 2 才有**；阶段 1 该因子恒缺，按下方「部分因子模式」处理 |
| `SEC.CROWD.VAL_PCT` | 估值因子分位 | `SEC.VAL.PE_XSEC_PCT` | 直取（不重复计权，登记为「复用」） | 0–100 · 行业×日 · ✅ 阶段 1 用横截面分位；待 `PE_TS_PCT` 满 250 观测后按字典切换 |
| `SEC.CROWD.HIT_CNT` | >80 分位的因子命中数 | 上三行 | **M5** `Σ 1{p_i > 80}`；同时输出 `extra.hit` / `extra.k`（当日可用因子数） | 0–`k` · 行业×日 · 单因子缺失时该因子**不参与分子也不参与分母**，`k` 随之减 1；`k < 2` → `hit=NULL`（不得把缺失按 0 计） |
| `SEC.CROWD.GATE` | 拥挤档位 | `HIT_CNT` + `k` | `k=3`：`3/3 → EXTREME`、`≥2 → HIGH`、`1 → WATCH`；`k=2`（部分因子）：`2/2 → HIGH`（**不出 EXTREME**）、`1 → WATCH`；`0 → NORMAL`；`k<2 → NA` | 分类值 · 行业×日 · `extra.partial = (k<3)`，与 §6.9「三者同时 >80% → 极高拥挤」一致：缺一因子时**无权判极高** |

**阶段 1 的部分因子模式**（阶段 0 结论带来的落地调整）：`TURNOVER_PCT` 依赖 D1，阶段 1 只有 `AMT_SHARE_PCT` + `VAL_PCT` 两因子（`k=2`）。此时 `GATE` 最高只到 `HIGH`，`extra.partial=true`，前端必须显示「2/3 因子」。阶段 2 成分表上线后 `k=3`，才开放 `EXTREME` 档；两阶段的闸门**不得跨期比较**（分母不同）。这样既不阻塞阶段 1 的风险提示，也不把两因子结论冒充 §6.9 的三因子共振。

> ✅ **阶段 2 已落地**：`k=3` / `partial=false`、`EXTREME` 解封（实测 `sector_biz_daily` 与 `SEC.CROWD.GATE.extra` 均正确）。但 `TURNOVER_PCT` 首期用**横截面分位 M1** 而非原设计的 M2 时序分位——成分快照起点 2021-12-13，样本不足以出稳定时序分位；待满 250 观测后按字典切换并登记为口径变更。

> ⚠ **跨层重复计权登记**（主报告 §5.11 的纪律）：`SEC.CROWD.VAL_PCT` 与 `SEC.DIM.VALUATION` 读同一批 PE 原子。二者语义不同（前者是风险共振的**一票**，后者是估值水位**程度**），消费端不得当作两条互相印证的独立证据，须在跨层引用登记表显式登记。

### E 组 · 基本面与预期 `SEC.FUND.*`（状态侧）

**组定义**：以**可计算**的财务 PIT 聚合 + 已落地的一致预期快照替代 §6.10 不可得的「景气趋势」。中观景气度不是指标，是外采数据体系，未立项前本组不含任何中观量。**极性：高分 = 基本面 / 预期更改善**。

| 代码 | 定义 | 输入 | 计算模型 | 输出 / 可用性 |
|---|---|---|---|---|
| `SEC.FUND.ROE_MEDIAN` | 成分股 ROE 中位数 | S6 `financial_indicator.roe` + `announce_date` + **D1** | **M6**（`announce_date ≤ t` 取最新报告期，`median`） | % · 行业×日（季频台阶，PIT ffill）· `freq=quarter` 写入字典，按混合频率处理 |
| `SEC.FUND.NP_YOY_MEDIAN` / `REV_YOY_MEDIAN` | 净利 / 营收同比中位数 | S6 `net_profit_yoy` / `revenue_yoy` + **D1** | **M6** + `median`；winsorize `[2,98]` 仅用于稳健估计参考分布 | % · 行业×日 · 极端值不截最终读数 |
| `SEC.FUND.IMPROVEMENT_BREADTH` | 行业内净利同比为正的成分占比 | 同上 | **M6** `count-if / n` | 0–100% · 行业×日 · 归一比率，天然平稳（§5.7 L2-a ③） |
| `SEC.FUND.COVERAGE` | 财务覆盖度 | 同上 | 有值成分数 / 成分数 | 0–1 · 行业×日 · **门禁**：`<0.6` 时本组全部读数作废并清库 |
| `SEC.FUND.REVISION_BREADTH` | 行业盈利修正宽度（30 快照滚动） | S5 `analyst_forecast_snapshot.fy1_eps/fy1_year/report_count` + **D1** | **M7** | −100–100% · 行业×日 · 🔴 阶段 0 实测**仅 25 个快照日**（2026-09-30 ~ 11-12），30 快照窗口**尚未填满**，按工作日节奏约 2026-11 下旬补齐；未填满时按实际 `n` 出值并写 `n_sample` + `confidence=low`；行业覆盖 <30% 或有效样本 <5 家 → `NULL`；**仅 A 股** |
| `SEC.FUND.RATING_BULL_PCT` | 评级乐观度 | S5 评级五档 | `(买入+增持)/五档合计` | % · 行业×日 · ⚠ 判别力弱（§7.8 实测 99.3%、结构性集中在买入/增持），**仅作极端背离提示，不进维度分** |

> 与 §6.6 的差异必须写进字典 `remark`：现免费源**只有 EPS + 评级五档**，所以首期只做「修正宽度」，**不做**修正幅度、目标价修正、行业汇总中位数；「修正趋势 1/3/6 月」需 ≥6 个月快照，属二期。

### F 组 · 行业宽度 `SEC.BREADTH.*`（状态侧）

**组定义**：行业内部的参与广度。指数口径可立即算；个股口径需 D1。**极性：高分 = 广度更健康**。

| 代码 | 定义 | 输入 | 计算模型 | 输出 / 可用性 |
|---|---|---|---|---|
| `SEC.BREADTH.UP_RATIO` | 成分股当日收涨占比 | S7 `a_daily_quote.change_pct` + **D1**（或 S1 指数口径过渡） | **M6** `count-if / n` | 0–100% · 行业×日 · 与市场级 `SECTOR.UP_RATIO` 区分：后者是「多少个行业在涨」，本项是「行业内多少股票在涨」 |
| `SEC.BREADTH.ABOVE_MA60` | 成分股 close > MA60 占比 | S7 `close` + **D1** | **M6** + rolling MA60 | 0–100% · 行业×日 · 需 60 日暖机 |
| `SEC.BREADTH.NEW_HIGH_LOW` | 成分股 60 日新高−新低**归一净占比** | S7 `close` + **D1** | `100×(n_high−n_low)/(n_high+n_low)` | −100–100% · 行业×日 · ⚠ 计数类净额必须归一（§5.7 L2-a ③），不得用家数差 |
| `SEC.BREADTH.RET_DISPERSION` | 成分股 20 日收益横截面 std | S7 + **D1** | **M6** + `std` | % · 行业×日 · 与 A 组 `DISPERSION_INTRA` 同源，**同维度内去重，只留一个** |

## 3.5 维度读数（L2）

严格按 §7.2 / §5.7「统一预处理 → 个性化聚合 → 统一出档」：

```
① 极性统一（direction / members[].sign，只定义一次）
② 平稳化（水平型先差分/去趋势；计数类净额先归一；资金净额先除成交额）
③ 去极值（winsorize [2,98] 只用于估计参考分布）
④ 分位化（横截面 M1 / 时序 M2，窗口按维度定，min_periods=250）
⑤ 聚合（维度内等权、单成员 ≤25% → 成员 <4 时改规则判定/单锚）
⑥ 有效性（coverage / dispersion / confidence / n_sample / members_used[]）
```

**阶段 1 落地口径**（实现见 `sector_layer.DIMENSION_DEFS`）：可用成员均 <4，按第 ⑤ 步的护栏**一律走单锚**，不扁平等权——3 个成员等权 = 33%，会突破 §7.2 单成员 25% 上限。校验成员仍落 `sector_indicator_value`，但不进维度分。

| 维度 | 侧 | 阶段 1 锚 / 校验成员 | 聚合结构 | 分位窗口 | 可用性门禁 |
|---|---|---|---|---|---|
| `SEC.DIM.TREND` | 状态 | 锚 `RS_XSEC_PCT`；校验 `RS_MOM_20D` / `RRG_QUADRANT` / `MOM_RANK` | **单锚** | 横截面 M1（趋势不取时序分位，§7.1） | `n_valid ≥ 10` |
| `SEC.DIM.FLOW` | 状态 | 锚 `XSEC_PCT`；校验 `NET_NORM` / `NET_5D` / `NET_20D` / `FLOW_RANK`；`PRICE_FLOW_GAP` 为 `neutral`，**不进分** | **单锚** | 横截面 M1（快照样本不足，取时序分位无意义） | 快照类 `n_sample` 必须透出 |
| `SEC.DIM.VALUATION` | 风险 | 锚 `PE_XSEC_PCT`；校验 `PB_XSEC_PCT` / `REL_TO_MARKET` / `PE_TS_PCT` | **单锚** | expanding（首期不可用，仅写 `n_sample`） | 短历史只出横截面 |
| `SEC.DIM.CROWDING` | 风险 | `HIT_CNT` / `GATE` | **规则判定** | 不取分位 | `k<2 → NA`；阶段 1 `k=2` 部分模式 |
| `SEC.DIM.FUNDAMENTAL` | 状态 | —（依赖 D1） | **阶段 2 未启用** | — | `score=NULL`，`extra.reason` 标注 |
| `SEC.DIM.BREADTH` | 状态 | —（依赖 D1） | **阶段 2 未启用** | — | `score=NULL`，`extra.reason` 标注 |

> 阶段 2 成员齐备（≥4）后才按 §7.2 改回分块等权（季频台阶 vs 日频快照分块、块内等权 → 块间等权）。**从单锚切到等权属口径变更**，必须换 `CALIB_VERSION` / `params_hash` 并登记 `breaks`，历史读数不可与阶段 1 直接比较。

**阶段 2 落地口径（✅ 已实现）**：`TREND` / `FLOW` / `VALUATION` 成员数未变，仍为单锚；`FUNDAMENTAL` / `BREADTH` 各 4 成员改为 `equal`（每成员 25%，恰好满足 §7.2 上限），`sign` 是唯一翻转点（`RET_DISPERSION` 为 `high_risk`，取 `100−p`）；`CROWDING` 仍规则判定 `score` 恒 NULL。`ENABLED_DIMENSIONS` 4→6、`coverage` 分母 3→6、`state_side` 2→4 组、`PARTIAL_STATE` 闸门在六维齐备时消失。全部由 `CALIB_VERSION s1.1→s2.1` 换掉全部 `params_hash`，`sector_score_daily.extra.calib_break` 显式登记该断点。实测 2026-11-11：五组出 `score`（均值 ≈50，横截面分位的数学性质）、`CROWDING` 恒 NULL。

**输出**：每个 `(market, classification, sector_code, trade_date, dimension_code)` 一行，含 `score 0–100` + `coverage` + `dispersion` + `confidence` + `members_used[]` + `params_hash`。低覆盖 / 短样本时 `score=NULL` 并写 `confidence='low'`，**不得用分位填充**。

> `SEC.DIM.CROWDING` 走规则判定，其 `score` 恒为 `NULL`，档位存 `extra.crowd_gate`（`NORMAL/WATCH/HIGH/EXTREME/NA`），`members_used` 记录三因子命中明细；`SEC.DIM.VALUATION` 在 `PE_TS_PCT` 可用前只以横截面分位出分并标 `confidence='low'`。

> **明确不产出「单一综合分」**：状态侧 4 组与风险侧 2 组是异质信息，加权求和会产生「低估值抵消高拥挤」这类无意义读数。这是 §7.2「扁平平均不产生信息，只产生权重幻觉」的直接应用，也是与 §6.10 最根本的方法论分歧。

## 3.6 业务读数与闸门（L3）

取代 §6.10 的「排序 + 超配建议 + 核心逻辑」：

| 输出 | 定义 | 计算模型 | 语义约束 |
|---|---|---|---|
| `state_side` | 状态侧热度 | `mean(当期已启用的状态侧单锚 score)`；阶段 1 = `TREND + FLOW` | 可用组 **<2 → `NULL`**；不足 4 组写 `extra.partial=true` 并加 `PARTIAL_STATE` 闸门；**不代表未来收益** |
| `risk_side` | 风险侧水平 | 阶段 1 = `VALUATION.score`；拥挤度以 `GATE` 单独承载，不并入数值 | 高分 = 更贵 |
| `quadrant` | 2×2 状态矩阵 | `state_side` 与 `risk_side` 各按当日横截面中位数二分 → `强势·未拥挤` / `强势·偏贵拥挤` / `冷门·未拥挤` / `弱势·偏贵拥挤`；缺任一侧 → `NA` | 🔴 **象限名只描述当前状态，不含超配/低配/买卖含义**；必须写进字段注释 |
| `rank_no` / `rank_n` | 当日横截面排名 | 按 `state_side` 在**同一 `(market, classification)`** 内降序，`rank(method='min')` | 分母 `rank_n` 实测 16→28→31 时变；CN/HK **绝不合并**；`applicable=false` 的行业不参与排名 |
| `coverage` | 可用维度 / **当期已启用维度** | 阶段 1 分母 = 3（TREND / FLOW / VALUATION）；阶段 2 启用 6 维后分母变化 | 🔴 **分母变化属口径断点**，须换 `params_hash` 并登记 `breaks`，两期读数不可直接比 |
| `gate_flags[]` | 五类闸门 | ① `DATA_COVERAGE`：`coverage<0.6`；② `SHORT_SAMPLE`；③ `BREAK`（指数编制/分类/源断点）；④ `CROWDING`：`GATE ∈ {HIGH, EXTREME}`；⑤ `PARTIAL_STATE`：状态侧维度不足 | 触发 ① 或 ③ → **只出读数、不出排名**；④⑤ 只降置信/提示，不屏蔽排名 |
| `applicable` | 是否可出业务读数 | 近 20 交易日有效读数 ≥15 日 **且** `coverage ≥ 0.6` **且** 无 ① ③ 闸门 | 类比 `force_round_daily.applicable` 与 `_coverage_gate`；`false` 时前端只提示「数据不足」，不显示排名 |
| `confidence` | 置信度 | `f(n_sample, coverage, pit_capable)`；阶段 1 因估值快照仅 27 日，业务行一律 `low` | `pit_capable=false` 的历史段一律不高于 `low` |

> **实施期口径细化**（不改目标，只把不可执行的规则改可执行）：
> ① **`SHORT_SAMPLE` 按「参与维度分的锚指标」判定，不按行业整体**。阶段 1 的锚全部是横截面分位（不依赖时序样本），若按 §5.7 的 `n_sample<750` 全局触发，估值快照的 27 日会让 **所有行业 `applicable=false`**，阶段 1 将完全没有排名输出。短样本信息仍由 `SEC.VAL.PE_TS_PCT` 等原子行的 `n_sample` / `quality=short_sample` 完整透出，不丢失。
> ② **`state_side` 只聚「当期已启用」的维度**，并把 `extra.state_members` / `extra.partial` / `extra.state_n` 随行写出。阶段 1 恒 `PARTIAL_STATE`；✅ 阶段 2 六维齐备后 `state_n=4`、闸门自动消失，实测 31 行业全部 `applicable=true` / `coverage=1.0`。两期状态分不可直接比较。
> ③ **阶段 2 的 `coverage` 分母从 3 变 6**：2021-12-13 之前的长历史段因缺 4 组自下而上维度，`coverage` 最高 0.5 < 0.6 → `applicable=false`、不出行排名，但**阶段 1 的原子读数（A/B/C/D 价格类）仍完整保留**。这正是决策 A「长历史由不依赖成分的价格类指标承担、两段不混排」的落地形态。`extra.pit_capable` 随行标注。

**出向契约**：只承诺「表 + 列 + `params_hash` 语义」，不承诺行数（维度/业务读数可能增删）；消费方按 `dimension_code` / 白名单读取，**不得 `SELECT *`**（主报告 §5.11 引用稳定性条款）。

## 3.7 验证闭环与降级路径

沿用 §8.5「定义不依赖未来、验证使用未来」：

| 检验 | 方法 | 通过门槛 | 不通过的处置 |
|---|---|---|---|
| 排序有效性 | 按 `state_side` 分 5 组，检验未来 20/60/120 日**行业超额收益**（相对 31 行业等权均值）的单调性与分离度；Spearman IC；按行业×月、block bootstrap | 分组单调且 IC 样本外显著 >0 | **排序保持「当前状态描述」定位**，不得宣称有效、不得用于配置、权重维持等权 |
| 拥挤闸门有效性 | `GATE=EXTREME` 后 20/60 日回撤幅度与概率 vs 全样本 | 显著放大 | 降级为纯展示标记，不作风险提示 |
| 构念效度 | 各维度成员与 `score` 的相关 / 单因子载荷 / Cronbach α；一阶差分相关矩阵 + PCA | 成员载荷 ≥0.3、无负相关；冗余成员移出 | 按 §8.5 门禁，未通过不得发布该维度 |
| 各档样本占比 | 象限 / 档位在标定样本内占比 | 按分位切档的档位须近似等频；偏离提示分布漂移 | 检查实现或重标，不改判据凑数 |
| 幸存者偏差 | `pit_capable=true/false` 分段读数对比；早期年份单列报告 | 差异可解释 | 早期段整体降置信，不静默混入 |
| 口径变更 | `params_hash` 前后逐值对比（`git worktree` 新旧代码） | 非口径变更时逐值相等 | 不相等即阻断发布 |

> 🔴 **决策闸门**：§6.10 的「超配/标配/低配建议」是否恢复，取决于 **① 中观景气与一致预期外采立项、② 成分 PIT 历史齐备、③ 上述前瞻检验通过** 三个条件同时满足，且需重新评估「预测」是否符合项目定位。**不在本方案内决策**。

---

# 第四部分 · 落地计划（收尾）

## 4.1 分期与依赖

```
阶段 0 数据闸门（探测 + 标定）
   └─→ 阶段 1 CN 纯价格/资金层（可立即开工，不依赖新增采集）
         ├─→ 阶段 4 验证闭环（持续，不早于阶段 1 + 数据累积）
         └─→ 阶段 2 成分主数据 + 基本面/宽度层（依赖 D1）
                └─→ 阶段 3 HK 行业层（依赖 D4）
```

## 4.2 各期交付与验收

### 阶段 0 · 数据闸门（前置，约 1 周）

| 项 | 内容 | 验收 | 阶段 0 结果（2026-11-14） |
|---|---|---|---|
| 数据可用性盘点 | 对 S1–S11 逐表跑 `min(trade_date)` / `count(distinct trade_date)` / 覆盖率 / 断档日 | 每个指标的「输入起点」有数字依据 | ✅ 完成，见 §2.1 实测盘点与附录 A |
| **D1 探测** | 新增 `tools/probe_sector_constituent.py`（遵循 §4.1「先探测后开发」）：候选源字段、覆盖、历史深度、PIT 可得性 | 产出探测报告 + 决策点 | ✅ 完成：主源定为 `index_component_sw`；**完整 PIT 历史不可得**（详见 §2.2.1，3 个决策点待确认） |
| **D2 标定** | 校验 `sector_daily.turnover` 量纲与全 A 成交额覆盖度 | 出具「可用 / 不可用 + 替代列」结论 | ✅ 完成：ratio 0.998~1.000 → 单位亿元、无需新表；D 组第一因子解禁 |
| 表与契约 | 4 张新表 DDL 进 `sql/schema.sql` + `regime_schema.py` | 三脚本自检无差异（§4.3） | ⏭ **顺延至阶段 1**（阶段 0 为只读探测，不建表、不写库） |

> 阶段 0 交付物：`tools/probe_sector_constituent.py`（可复跑，`--sources` 联网）+ 本文 §2.1 / §2.2 / §2.2.1 / §3.4 / 附录 A 的实测结论。**未改动任何表、采集脚本、调度或服务**，因此无需重启、无需部署。

### 阶段 1 · CN 纯价格 / 资金层（✅ 已完成，2026-11-14）

- **依赖数据**：S1 / S2 / S3（仅横截面）/ S4；**不依赖 D1**。
- **交付指标**：A 组全部、B 组横截面项、C 组（除 `SOUTHBOUND_NET` / `MARGIN_NET`）、**D 组两因子部分模式**（`AMT_SHARE_PCT` + `VAL_PCT`，`k=2`、`partial=true`、最高档 `HIGH`）、L2 六组、L3 状态矩阵 / 排名 / 闸门。D2 已在阶段 0 关闭，本阶段无数据前置。
- **工程**：`sector_layer.py`（计算）+ 4 张表 + `regime.sector_indicator_dict` 字典；独立 systemd `sector-layer.service` + `sector-layer.timer`（工作日 **18:50**，晚于 18:45 市场状态计算）；`collection_task_log` 任务级埋点（现有 `market_state_daily` 缺这一环，本层不继承该欠账）+ `monitor_table_config` 表级监控。

#### 阶段 1 实施记录（2026-11-14）

**交付物清单**

| 文件 | 变更 |
|---|---|
| `sector_layer.py` | **新增**计算层：29 条指标字典、六维定义、M1/M2/M3/M4/M5 实现、按月切片写入、任务埋点、CLI（`--backfill` / `--dry-run` / `--init-schema`） |
| `sql/schema.sql` | 追加第 **21–24** 号表（字典 / L1 / L2 / L3）+ 索引 + COMMENT |
| `regime_schema.py` | 同步 7 条 DDL + 10 条 COMMENT，新增 `ensure_sector_schema()`（只建本层表，供运行时幂等自举） |
| `system/sector-layer.service` / `.timer` | **新增** oneshot + 六项资源上限 / 工作日 18:50 |
| `deploy.sh` / `bootstrap.sh` | `TIMERS` / `units` / `enable_services` 接线 |
| `monitor_collector.py` | `_SEED_TASK_CONFIG` 任务埋点、`_REGIME_TABLE_CONFIG` 4 表登记、`_CN_MONITOR_TABLES` 4 表 CN 日历 |
| `tests/test_sector_layer.py` | **新增** 38 测试 / 92 subtests（口径纯函数 + 工程红线载体） |
| 本文 | §3.1 / §3.5 / §3.6 / §4.2 口径同步 |

**资源实测**（macOS 开发机，`/usr/bin/time -l`）

| 场景 | 峰值 RSS | 耗时 | 判定 |
|---|---|---|---|
| 增量小窗口（11 个交易日，dry-run） | 133 MB | 2.9 s | ✅ |
| 全量回填 1999-12-30 起（7 个 4 年块，dry-run） | **236 MB** | 69 s | ✅ ≤256 MB（R3 上限的一半） |

> 🔴 **R3 修复记录（本次最重要的一条工程教训）**：首版按 4 年块一次性构造输出行，一块 ≈ 31,000 个「行业日」× 25 个指标 ≈ **77 万行 dict**，全量 dry-run 峰值 **892 MB**（会被 `MemoryMax=512M` 的 cgroup OOM kill）。根因不是输入表大（输入只有 15 万行 × 15 列），而是**输出行在内存里整块堆积** —— 与 `doc/compute_task_resource_standard.md` §1 事故档案里「一次性加载」不同的新形态。修复：`_iter_month_frames()` 把年块再按**月**切片，生成即写、`del` + `gc.collect()`，峰值 892 → 236 MB，耗时 84 → 69 s。已在 `tests/test_sector_layer.py::test_month_slicing_guard_for_r3` 钉住，防后续重构回退。

**验收结果**

| 项 | 结果 |
|---|---|
| `py_compile`（4 个改动 .py） | 通过 |
| `tools/scan_big_table_loads.py`（3 文件） | 大表命中 0、疑似无下界 0 |
| `sql/_code_schema_diff.py` | 无漏写 |
| `sql/_schema_diff.py` | 差异仅剩与本层无关的既有项（`daily_market_turnover.trade_date` NOT NULL、`a_daily_quote.ps_ttm/pcf_ttm` 下线列等）；**4 张新表在真实库与 schema.sql 一致** |
| 真实写库（2026-11-01~11-11） | 字典 29 行 / L1 6,200 / L2 1,488 / L3 248；JSONB（`extra` / `gate_flags`）、`rank_no`、`quadrant`、`applicable` 均验证正常 |
| 全量 `pytest tests/ -v` | **726 passed, 2 warnings, 447 subtests**（新增 38 / 92） |

**口径落地核对**：`SEC.DIM.CROWDING` 规则判定 `score` 恒 NULL、档位在 `extra.crowd_gate`；阶段 2 的 `FUNDAMENTAL` / `BREADTH` 标 `unavailable_stage2`；`PARTIAL_STATE` 闸门恒开；`PE_TS_PCT` 只写 `n_sample` 不分位；`NET_20D` 不足 20 日为 NULL —— 均与 §3.5 / §3.6 一致。

**未执行 / 待决定**

- **全量回填写库未跑**（只跑了 `--backfill --dry-run`）：预计约 160 万行（已跳过全 NULL 行），属批量运维动作，建议低峰期单独执行 `python3 sector_layer.py --backfill` 并盯 WAL / 磁盘。timer 默认增量 45 天（回退 3 天自愈），**不依赖历史回填即可工作**。
- **监控配置入库**：代码已登记，由 `monitor_collector` 启动时幂等迁移生效；服务器需 `./deploy.sh up`（增量同步 `sql/schema.sql` + 渲染新 unit + enable timer）。本机未重启任何服务。
- 前端页面契约仍未覆盖（`market_profile_ui_handoff.md` 无 ④ 层内容）。
- ⚠ **`system/` 整目录被 `.gitignore:14` 忽略**（项目既有设计，`git ls-files system/` 为空，所有 unit 均不入库）。两个新 unit 靠 `deploy.sh` 的 rsync（排除清单**不含** `system/`）同步到服务器，再由 `bootstrap.sh` 渲染 / `enable_services` 启用，**不进版本控制**。若希望 unit 版本化需单独调整 `.gitignore`，属独立决策。

### 阶段 2 · 成分主数据 + 基本面 / 宽度层（✅ 已完成，2026-11-14）

#### 阶段 2 实施记录（2026-11-14）

**交付物清单**

| 文件 | 变更 |
|---|---|
| `get_sector_constituent.py` | **新增**：`index_component_sw` × 31 行业采集、6 位码→`SH./SZ./BJ.` 转换（zfill）、PIT 版本链 diff（ADD/REMOVE/WEIGHT + 微小波动阈值 0.01）、清单塌陷质量闸门 |
| `sector_stage2_agg.py` | **新增**：按月 × 按股票双层分批聚合；充分统计量合并；财务 PIT（月批次次月 1 日生效）、一致预期修正下沉（M7）、两融按行业 |
| `sector_layer.py` | 激活 14 个阶段 2 指标、六维口径、三因子拥挤闸门、`CALIB_VERSION s1.1→s2.1` 口径断点、coverage 分母 3→6 |
| `sql/schema.sql` / `regime_schema.py` | 追加第 **25–26** 号表（`sector_constituent` / `..._change`）+ 索引 + COMMENT，逐字双同步 |
| `market_scheduler.py` | 注册「行业成分主数据」**交易日 18:05**（早于 18:50 计算层；必须每日跑以累积 `eff_to`） |
| `monitor_collector.py` | 2 张表登记（`lowfreq/7`，事件驱动表避免无变更日误报）+ CN 日历 |
| `tests/test_sector_stage2.py` | **新增** 30 项测试 / 源码契约；`tests/test_sector_layer.py` 3 项断言随口径变更更新 |

**数据实测**：31 个行业接口全部可用，5,222 行 / 10.3s；单行业 20~541 只，权重合计 99.97~100.00%；`eff_from` 实测 2021-12-13 ~ 2026-11-03；幂等重跑 0 变更。决策 B 落实：权重只落新表，未回写 `stock_sector`。

**资源实测**（macOS 开发机，`/usr/bin/time -l`）

| 场景 | 峰值 RSS | 耗时 |
|---|---|---|
| 聚合模块小窗口（11 日） | **176 MB** | 31 s |
| `sector_layer` 端到端小窗口（2 日，dry-run） | 193 MB | 29 s |
| `sector_layer` 端到端 12 个月（dry-run） | **215 MB** | 300 s |
| 增量 45 天（timer 默认） | — | 约 35~40 s |

> 🔴 **R3 二次修复记录**：阶段 2 首版 `build_quote_agg` 一次性 `read_sql` 拉 45 万行，实测 **617MB**。先换 `_read_sql_stream` + 批内 dtype 瘦身降到 ~250MB，仍逼近 256MB 门槛；最终改为**按股票分批（1,000 只/批）+ 充分统计量（sum / 平方和 / count）跨批合并**，峰值与 620 万行总规模脱钩 → **176MB**。分批前后核心读数逐值一致（`up_ratio` / `above_ma60` / `turnover_w` / `margin_net` 完全相同），仅离散度因 float32 量化有约 0.5% 差异，已记录。全量回填（约 60 个月）预计 25 分钟，timer 的 `RuntimeMaxSec=1800` 对**增量**绰绰有余；全量回填写库仍建议低峰期手动跑。

**两处实施期口径细化**（写回 §3.4 / §3.5 / §3.6）：
1. `TURNOVER_PCT` 首期用**横截面分位 M1** 而非 M2 时序分位（成分快照起点 2021-12-13，时序样本不足）；满 250 观测后按字典切换，属口径变更。
2. 财务 PIT 用「**月批次 + 次月 1 日生效**」而非按交易日 cutoff：无前视（月中下旬才公告的财报不会在月初可见），代价是财务读数最多滞后一个月，与季频源节奏相称。

**验收结果**

| 项 | 结果 |
|---|---|
| 真实写库（2026-11-10~11-11） | 字典 **41 条全 active**；41 个指标各 31 行；六维中 5 组出 `score`、`CROWDING` 规则判定恒 NULL；31 行业全部 `applicable=true`、有 `rank_no`、`coverage=1.0` |
| 拥挤闸门 | `k=3` / `partial=false`，`EXTREME` 已解封；`SEC.CROWD.GATE` 的 `extra` 正确带 `k` / `label` / `members` |
| 幂等 | 成分采集第二次运行 ADD/REMOVE/WEIGHT/微小波动全 0 |
| `py_compile`（6 文件） | 通过 |
| `tools/scan_big_table_loads.py`（3 文件） | 大表命中 1、疑似无下界 0 |
| `sql/_code_schema_diff.py` | 无漏写 |
| `sql/_schema_diff.py` | 新表与真实库一致，仅剩与本层无关的既有差异 |
| 全量 `pytest tests/ -v` | **757 passed, 2 warnings, 497 subtests**（新增阶段 2 共 30 测试） |

**口径断点（必须随读数透出）**：`CALIB_VERSION s1.1 → s2.1` 换掉全部 `params_hash`；`coverage` 分母 3→6、`state_side` 从 2 组→4 组、拥挤度 k=2→k=3、`PARTIAL_STATE` 闸门在数据齐备时消失。**阶段 1 与阶段 2 的 L2/L3 读数不可直接比较**，前端须按 `params_hash` / `extra.calib_version` 分段显示。

**未执行 / 待决定**

- **全量回填写库未跑**（只跑了 12 个月 dry-run）：阶段 2 全量约 25 分钟 / ~200 万行，属批量运维动作，建议低峰期单独执行 `python3 sector_layer.py --backfill`。timer 默认增量 45 天，不依赖回填即可工作。
- **成分历史只能向前累积**：`eff_to` 由 18:05 每日快照逐步生成，2021-12-13 之前无成分、也不回算（决策 A）。服务器部署后需 `./deploy.sh up`（同步 `sql/schema.sql` + 重启调度器使 18:05 任务生效）；监控配置由 `monitor_collector` 启动时幂等迁移。本机未重启任何服务。
- `REVISION_BREADTH` 的 30 快照窗口仍只有 25 个快照日，按实际 `revision_n` 出值 + `confidence=low`，未冒充 30 快照。

### 阶段 3 · HK 行业层（约 2 周，依赖 D4）

- **交付**：`sector_hierarchy` 补 `as_of` 并挂调度；`regime.hk_sector_index_daily`（等权 + 市值加权两版，M8）；HK 侧 A/B/C/F 组与 `SOUTHBOUND_NET`。
- **纪律**：CN/HK 物理分表、分口径、各自分位，**不做统一排序**；GICS 只作展示标签；港股仙股设流动性门槛；南向仅近 1 年 + 当前成分偏差，须标注。
- **验收**：与现有 `SECTOR.UP_RATIO` HK 等权合成的一致性；粒度差异（113 三级 vs 31 一级）在字典 `remark` 与 `extra` 双处标注；两版指数差异报告。

### 阶段 4 · 验证闭环（持续，不早于阶段 1 完成 + 数据累积）

- 按 §3.7 跑前瞻检验，产出报告；**决策闸门**：通过 → 排序可标注「有效性已验证的状态排序」（仍不出配置建议）；不通过 → 保持「状态描述」+ 等权。
- 快照类数据满 250 观测后，逐项启用时序分位（B 组 `PE_TS_PCT`、C 组滚动窗口），启用前做 `params_hash` 变更登记与全历史重算。

### 明确不在本计划内

中观景气外采、产业链图谱、生命周期/竞争格局、A/H 统一 GICS 打分、目标价一致预期、前端页面契约。

## 4.3 工程红线与交付自检清单

| # | 检查项 | 依据 |
|---|---|---|
| 1 | 新表 / 新列**逐字同步** `sql/schema.sql` 与 `regime_schema.py`，并登记 `monitor_table_config`；跑 `_dump_schema_from_db.py` / `_schema_diff.py` / `_code_schema_diff.py` 三脚本无差异 | AGENTS.md 最高优先级约束；§7.12 交付自检第 5 条 |
| 2 | 新增依赖写 `requirements.txt`（分组注释 + `包==版本` + 行尾用途） | AGENTS.md / §4.1 选型原则 4 |
| 3 | **独立 systemd `oneshot` + `timer`**，六项参数齐全：`MemoryMax=512M` / `MemorySwapMax=0` / `RuntimeMaxSec` / `Nice=10` / `IOSchedulingPriority=7` / `OOMScoreAdjust=500`；**禁止塞进 `market_scheduler.py` 进程内** | 资源规范 **R4 / R5**；现状 `market_state_daily` 18:45 仍在调度进程内是已知工程债，本层不复制 |
| 4 | 分批取数：`_read_sql_stream` / 按行业分批，`stock_code`→`category`、`trade_date`→`datetime64`；大表查询带时间下界或实体过滤；单事务 upsert ≤5 万行 | R1 / R2 / R6 / R7；`profiling/tags/_base.py` |
| 5 | 上线前实测峰值内存（`/usr/bin/time -v` 或 `systemctl show -p MemoryPeak`）并把数字写进提交说明；独立单元 ≤512MB 且实测 ≤上限 1/2 | R3、AGENTS.md §上线前必测 |
| 6 | `collection_task_log` 任务级埋点 + `monitor_table_config` 表级监控（快照类 `day/1`，断档即告警） | 资源规范「必须登记监控」；§7.8/§7.14 断档永久缺失教训 |
| 7 | 调度器进程内 `run(codes, ctx=...)` **无法传命令行参数** → 日常默认值必须写成调度期望的行为；`--days` 之类默认值按增量设计 | §7.12 第 4 条；`build_force_rounds.py` 曾因默认 5 天超时 |
| 8 | 长任务日志**不得只 tail 汇总**，须 `grep -E -e ❌ -e 失败 -e Traceback`；索引类型统一 `datetime.date`（不是 `Timestamp`） | §7.12 第 1/2 条（曾致 10 个分块静默全失败） |
| 9 | 全量 `.venv/bin/python3 -m pytest tests/ -v`；不变量测试随指标同步新增 | 项目铁律 |
| 10 | 口径变更登记 `params_hash` + `breaks`；`git worktree` 新旧逐值对比证明结果没变 | §7.2 / 资源规范第 5 条 |
| 11 | 本文及 `doc/*.md` **不随 `deploy.sh` 同步到服务器**（rsync 排除 `*.md`，有意设计）；服务器端载体是 `sql/` / `system/` / `*.py` | `deploy.sh` `RSYNC_EXCLUDES` |

## 4.4 风险与决策点

| 风险 | 等级 | 对策 / 决策点 |
|---|---|---|
| D1 免费源无历史成分 → 幸存者偏差 | **高**（已按约束落地） | 阶段 0 实测完整 PIT 源 HTTP 508 不可用；✅ 阶段 2 已按决策 A 把自下而上指标起点限定在 2021-12-13 之后，`get_sector_constituent.py` 建 PIT 版本链、`eff_to` 由每日快照累积，`sector_stage2_agg.py` 按 `[eff_from, eff_to)` 取用，`extra.pit_capable` 随行标注。**残留限制**：`eff_to` 只能向前累积，无法回溯历史剔除；任何回算段须标 `pit_capable=false` / 降置信、早期年份单列 |
| 快照类历史深度不足（估值/资金/预期均 2026-09 起） | **高** | 首期只出横截面分位；时序分位 `NULL` + `n_sample`；满 250 观测后逐项启用并登记 `params_hash`；断档监控 `day/1` |
| 分类口径不一致（THS 90 ↔ SW 31；申万三批起点） | 中高 | 强制走 `sector_mapping` 且只纳入 `confirmed OR corr≥0.6`；`mapped_ths_count` / `rank_n` 透出；月度复核任务已存在 |
| A/H 混用 | 中高 | 主键含 `market` + `classification`，分表分口径；测试钉住「不得混排」 |
| 把状态读数当预测 / 排序被误读为建议 | **高** | 字段注释 + 字典 `remark` + 前端文案三重声明「不含配置建议」；`quadrant` 名只描述当前状态；验证未通过不升格 |
| 权重幻觉 / 重复计权 | 中 | 首期等权 + 字典表；单成员 ≤25%；一阶差分相关 >0.8 去重；跨层复用登记（§3.4 D 组注） |
| 单一综合分制造伪精确 | 中 | 状态侧 / 风险侧分离，只出矩阵不出合成分 |
| 资源雪崩（1.9GB 小机、四次事故） | **高** | 独立 unit/timer + 六项上限 + 分批 + 峰值实测；`regime.indicator_value` 已达 95 万行（R2 清单），行业层不得再制造一次性全表加载 |
| ~~`sector_daily.turnover` 量纲未标定~~ | ✅ **已关闭** | 阶段 0 实测 ratio 0.998088~0.999661 → 亿元口径、完备覆盖；无需新表。遗留约束：同源占比分母取「当日有效行业合计」，`rank_n<31` 的早期段不可与后期比 |

---

## 附录 A · 数据可用性核对 SQL（阶段 0 执行）

> 本附录保留可复跑 SQL；**2026-11-14 的实测结果已写入 §2.1「实测盘点」**。等价的一键复跑命令：
> `.venv/bin/python3 tools/probe_sector_constituent.py --sources`（只读；不加 `--sources` 则只跑本地库盘点 + D2 标定）。

```sql
-- 每张行业层表的起点 / 有效日数 / 最新日（数字写回 §2.1）
SELECT 'daily_benchmark.CN.SW*' AS src, min(trade_date), max(trade_date),
       count(DISTINCT trade_date) FROM daily_benchmark WHERE bench_code LIKE 'CN.SW%';
SELECT 'sector_daily' AS src, min(trade_date), max(trade_date),
       count(DISTINCT trade_date), count(DISTINCT sw_code) FROM regime.sector_daily;
SELECT 'sector_valuation_snapshot' AS src, min(snapshot_date), max(snapshot_date),
       count(DISTINCT snapshot_date) FROM regime.sector_valuation_snapshot;
SELECT 'sector_fund_flow' AS src, min(trade_date), max(trade_date),
       count(DISTINCT trade_date) FROM regime.sector_fund_flow;
SELECT 'analyst_forecast_snapshot' AS src, min(snapshot_date), max(snapshot_date),
       count(DISTINCT snapshot_date) FROM regime.analyst_forecast_snapshot;

-- 申万 31 行业三批起点（决定横截面分母 rank_n）
SELECT sw_code, min(trade_date), count(*) FROM regime.sector_daily
GROUP BY sw_code ORDER BY 2, 1;

-- 成分主数据可得性（D1 探测后的验收）
SELECT market, classification, count(DISTINCT sector_code), count(DISTINCT stock_code),
       min(eff_from), max(eff_from) FROM regime.sector_constituent GROUP BY 1, 2;
```

> ⚠ 以上为 psql / 只读客户端**直执行**语句。若移入 Python `cur.execute(sql, params)` 参数化执行，SQL 字面量里的 `%`（`LIKE 'CN.SW%'`）必须双写成 `%%`，否则 psycopg2 会误判为格式符并抛 `IndexError` / `ValueError`（本项目 `get_financial.py` 等已多次踩中）。

## 附录 B · §6.10 → 本设计的逐项对照

| §6.10 维度 / 输出 | 本文处置 | 理由 |
|---|---|---|
| 景气趋势（高） | **删除**（指标层不含中观量）；以 E 组「基本面与预期」替代其可计算部分 | 外采未立项；§6.2 自述最大成本项 |
| 盈利修正（高） | 保留但降为 E 组普通成员 `SEC.FUND.REVISION_BREADTH`，等权、带覆盖门禁 | 已落地市场级口径，可下沉到行业；但需 ≥30 快照 + D1 |
| 成长性 / 盈利能力（中） | 归 `SEC.DIM.FUNDAMENTAL`，PIT 财务聚合 | S6 已有，需 D1 |
| 估值（中高） | 移入**风险侧** `SEC.DIM.VALUATION`，不与状态侧相加 | 避免「便宜抵消拥挤」 |
| 量价动量（中） | 扩为 A 组（RS / RRG / 排名）+ C 组（资金） | S1/S2 长历史，最可立即落地 |
| 拥挤度（反向·高） | 改为 `SEC.CROWD.GATE` 条件命中闸门，不取权重、不取乘积 | §6.9 原文「×」含义不明；§7.2 规则判定结构 |
| 行业排序 | 保留，但加 `(market, classification)` 边界、`rank_n`、`confidence`、`applicable` | C2 / 三批起点 |
| 超配/标配/低配建议 | **删除** | 违反 C1；需三条件 + 独立决策（§3.7） |
| 核心逻辑与风险 | **删除**，改为结构化 `gate_flags[]` / `quadrant` / `n_sample` | 违反 C6 可解释可回溯 |
| 权重「高/中」 | 首期**等权写字典**，二期按前瞻 IC 重估 | §7.2 |
| 单一打分卡 | **不产出综合分**，改 2×2 状态矩阵 | 异质信息扁平平均不产生信息 |
