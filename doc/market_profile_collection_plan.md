# 市场状态分析系统 · 数据采集方案（采集层）

> ⚠ **本文件为 v2.0 旧稿留档**：文中出现的 `§5.9` / `§5.10`、「市场温度计 / 风险指数 / FSI / 季节」等章节号与合成名称均为旧编号、旧口径，已于研究稿 v2.1.20 重编号并下线；不得按本文件的章节号去现行规格中索引。

> ⚠ **历史留档说明（2026-11-05）**：本文是 P1 采集层的编年记录，**以下涉及 `regime.market_regime_daily` 的描述仅反映当时状态**。该旧合成表已于 2026-11-05 彻底删除（零消费方、schema 已标废弃），DDL 从 `sql/schema.sql` / `regime_schema.py` 移除，生产者与监控登记同步清理；当前生效规格见 `doc/market_state_data_architecture.md` 与 `doc/market_profile.md`。

| 项目 | 内容 |
|---|---|
| 文档版本 | **v2.10**（2026-10-09：§3 第一层宏观收口 + **社融换源（央行直连）/ 信贷脉冲真值口径 / 通胀预期 T10YIE** + §3.4 外部环境与 §5.4 跨资产风险偏好；仅余 ECRI（付费源）；新增 §7.6~§7.24） |
| 编制日期 | 2026-09-30 |
| 配套产物 | `_probe_macro_sources.py`（探测工具，可复用）+ `doc/macro_source_probe_report.md`（探测报告） |
| 定位 | 《市场状态分析系统可行性研究报告》v2.0 的**采集层落地方案**（只覆盖「数据怎么来、怎么存、多久采一次」） |
| 范围 | A 股 + 港股 + 全球基准；①宏观 / ②资产配置 / ③市场状态 / ④行业配置 的原始数据 |
| 依据 | 真实库实测探测（2026-09-30，`market_db` 23 张表逐表统计）+ 采集层代码盘点（`market_scheduler.py` / `get_*.py` / `backfill_*.py` / `sql/schema.sql`） |
| 不在本文档范围 | 指标计算口径细节、分位化与合成算法、状态判定与验证闭环、前端可视化（另立文档） |

---

## 0. 摘要与结论

**三句话结论：**

1. **现有采集层已覆盖第③层（市场状态）原始数据需求的约 60–70%**，A 股行情可回溯至 1990 年、两融至 2010 年；真正的短缺不是"缺采集脚本"，而是 **补历史深度 + 补宏观/国债/信用 + 补 A 股情绪事件 + 补退市股**。
2. **存储不新建体系**：沿用 Postgres `market_db` + `db.upsert/bulk_upsert` 幂等 + `market_scheduler` 常驻调度 + `monitor_collector` 监控；新增 **10 张表**，集中放在新 schema `regime`（沿用 `profile.*` 先例），宽基/行业指数复用 `daily_benchmark` 不建新表。
3. **落地节奏**：P0（5–8 人日，先补已有数据的短板 + 探测验证新源）→ P1（15–20 人日，宏观包 + 情绪事件包 + 行业指数）→ P2（分析师一致预期先免费近似、中观景气数据单独立项外采）。

**立即要做的三件事（P0，①②③ 均已于 2026-09-30 完成，落地记录见 §7.5）：**

1. ✅ **已完成**：`_probe_macro_sources.py` 实跑完毕（39 个候选 → 32 可用），报告见 `doc/macro_source_probe_report.md`，结论速查见本文 **§4.4**。**探测推翻了 3 项原计划**：① 南向持股回填**不可行**（东财源仅近 1 年）；② 港股卖空历史回填**未解决**（港交所日报归档 404）；③ 北向在东财源**完全断流**（日频资金字段自 2024-08-19 全空，持股市值改**季度披露**，替代源待定位）。
2. ✅ **已完成**：恒指 → 2013-08、FRED 长历史（NASDAQ/VIX → 1990）、中债收益率曲线 → **2006-03**、申万一级行业 → 1999-12、交易日历（CN 8798 + HK 1906 行）、退市名单 361 条、北向净流入历史 2264 行（截断 2024-08-16）。
3. ✅ **已完成**：`regime` schema + 4 张表（`macro_series`/`indicator_dict`/`indicator_value`/`trading_calendar`）建成并登记 `sql/schema.sql`；3 个采集脚本已挂调度（19:00 / 19:10 / 08:45）并重启调度器生效。

---

## 1. 对 `doc/market_profile.md` 的评估（采集层视角）

### 1.1 总体评价

框架（Top-Down 四层）、方法（分位化 + 规则打底 + 验证闭环）、分层投入判断（③ 优先、④ 中观单独立项）均扎实，**可直接作为采集层的需求输入**。但第 9 章「数据可行性分析」的**现状盘点已明显过时**：把已有资产当成缺口，又把几个真实硬约束漏掉了。若按该章直接排工，会重复建设已有数据、并在关键指标上做出错误的历史深度承诺。

### 1.2 文档说法 vs 实库实测（逐步核对）

| # | 文档说法（§9） | 实测实况（2026-09-30） | 判定 |
|---|---|---|---|
| 1 | §9.2 现有数据只列 6 张表 | 实际 **42 表 + 7 视图**：A 股全市场日线 1,705 万行（**1990-12 起**、5,711 只）、两融 610 万行（2010-03 起）、全球 17 个基准序列、财务/分红/公司资料/回购/板块成分齐全 | ❌ 严重低估现有基础 |
| 2 | §9.3「两融余额 需补充 P1」 | `daily_margin_balance` 已全量在库：2010-03-31 起 610 万行 / 4,604 只 | ❌ 已具备 |
| 3 | §9.3「破净数 需补充 P1」 | `a_daily_quote.pb_ratio` 可自算（2018 起） | ❌ 可自算，非采集项 |
| 4 | §9.3「10Y 国债收益率 P0、难度低」 | 美债 10Y 已有（FRED，1990 起）；**中国国债收益率完全没有**（A 股 ERP 分母缺失） | ✅ 判断对，但需区分中美 |
| 5 | §9.3「iVIX / VHSI P0、难度低」 | iVIX 有免费代理（50ETF 期权 QVIX，2015 起）；**VHSI 无稳定免费源**，建议降级为恒指已实现波动率 | ⚠ 难度判断偏乐观 |
| 6 | §9.2「hk_daily_quote 支撑港股宽度全套」 | 成立，但是 **2019-01 起**（非 2015）；且 2026 年前换手率字段为空需自算 | ⚠ 部分成立 |
| 7 | §9.4 幸存者偏差对策「保存历史截面快照」 | 全市场日线池逐日落地，**日截面已天然 PIT**，快照机制隐含已具备；真正缺的是**退市股本身**（A 股 2019–2025 退市股仅留存 19 只，港股 0 只） | ⚠ 风险判断对、对策找错点 |
| 8 | §9.3「涨跌停、北向 P1」 | 涨跌停可自算（需分板块规则）；北向脚本已存在但**从未挂调度**（表 0 行） | ✅ 缺口属实，成本极低 |

### 1.3 文档遗漏的 5 个硬约束（采集层必须面对）

1. **北向净流入 2024-08-16 起官方停止披露**（探测精确定位）——且东财源连「买入/卖出成交额」也已为空，只剩「持股市值」仍在（且已改季度）。文档把北向当常规净流入采集项需修正。**（2026-11-06 复核：替代源排查完毕，官方只剩「成交总额/笔数/十大活跃股」且无买卖方向 → 日频净流入无可用源，见 §2.1 表下北向注。）**
2. **A 股估值/市值字段只有 2018 年起**（`pe_ratio/pe_ttm_ratio/pb_ratio/total_market_val` 同年限）——2015 牛熊的估值分位**不可比**，需外部市场级估值长序列补齐。
3. **关键表的历史深度远不够做分位**：南向持股仅 5 个月（2026-04 起）、沽空仅 4 个月（2026-06 起）、CBBC 仅 4 个月——按文档 §1.4「至少 2015 起」的要求，这三项必须回填或降级。
4. **无交易日历表**——MA、分位、前瞻收益验证、T+N 全部依赖它。
5. **基金/IPO/解禁/增减持/开户/涨跌停家数 全部 0 覆盖**（无任何采集脚本）。

### 1.4 文档写得好、可直接采纳的部分

- 分层投入判断（③ 优先、④ 中观单独立项）与实测一致；
- §10.2/10.3 存储与更新调度选型（Postgres + 日频收盘后）与现有架构一致，无需新建体系；
- §10.1 架构图的「**指标字典 / 指标时序（带快照）**」——这是现有库**唯一缺的一层**，本方案按它设计（见 §5）。

### 1.5 建议回写进 `market_profile.md` 的 3 条修正

1. §1.4「至少 2015 起」改为分层表述：**量价 A 股 1990 起 / 港股 2019 起；估值市值 A 股 2018 起；南向/卖空需回填或降级**。
2. §9.3 缺口表按本文档 §3 重写（两融/破净已具备；北向口径已变；VHSI 降级）。
3. §9.4 幸存者偏差：对策由「保存历史截面快照」改为「**回补退市股 + 宽度指标做含/不含退市股敏感性标注**」（快照机制现有日线池已隐含具备）。

---

## 2. 采集层实况盘点（实测基线）

### 2.1 已采集数据与覆盖深度

| 层 | 数据 | 表 | 实测覆盖 | 深度判定 |
|---|---|---|---|---|
| ③ | A 股全市场日线（量价+估值+市值） | `a_daily_quote` | 1990-12-19 起，1,705 万行，5,711 只；最新日 5,291 只 | 量价 ✅；**估值/市值仅 2018 起 ⚠**；股息率字段几乎全空 ❌ |
| ③ | 港股全市场日线（量价+PE+市值） | `hk_daily_quote` | **2019-01-02 起**，364 万行，2,831 只；最新日 2,259 只 | ⚠ 缺 2015–2018；换手率 2026 前为空需自算 |
| ③ | A 股全市场成交额 | `a_daily_market_turnover` | 1990-12 起，8,737 行 | ✅ |
| ③ | 港股全市场成交额 | `daily_market_turnover` | 2019-01 起，1,906 行 | ✅（同港股行情起点） |
| ③ | 两融全量明细 | `daily_margin_balance` | 2010-03-31 起，610 万行，4,604 只 | ✅（品种起点即 2010） |
| ③ | 南向个股持股 | `daily_ggt_hold` | **2026-04-20 起，5 个月**，685 只 | ❌ 需回填 |
| ③ | 港股卖空 | `daily_short_selling` | **2026-06-04 起，4 个月**，945 只 | ❌ 需回填（探源） |
| ③ | 牛熊证街货 | `daily_cbbc` | 2026-06-04 起，1,598 行 | ⚠ 低优先 |
| ③ | 回购 | `daily_buyback_event` / `a_stock_repurchase_plan` | 1991-11 / 2006-03 起 | ✅ |
| ③ | 分红、财务 | `dividend_history` / `financial_indicator` | 1991-03 / 1989-12 起，8,230 / 8,346 只，211 个报告期 | ✅ |
| ①② | 全球基准日线（17 序列） | `daily_benchmark` | 美债 10Y/2Y 1990 起、上证 1990 起、USDCNY 1994 起、恒指 2018-05 起、恒科 2020-07 起、SP500/VIX 2018-05 起（FRED 源可回填至 1990s）、日经/KOSPI/DAX 2014 起 | ⚠ 部分可回填 |
| ③④ | 板块/指数成分 | `stock_sector` + `sector_hierarchy` | 富途行业 196 个 / 7,998 只、概念 120 个 / 1,226 只、**指数仅 8 个**（沪深300、中证500/1000、创业板指、上证50、恒指/国企/恒科） | ⚠ 缺宽基与行业指数 |
| ③ | 北向 | `daily_northbound_flow` | **0 行**（脚本存在但从未挂调度；且净流入官方已 2024-08-16 停披露，见表下注） | ❌ 需改造（口径拆分） |
| ③ | 退市股 | — | A 股仅 19 只（2019–2025 退市）留存，港股 0 只 | ❌ 幸存者偏差**已实际存在** |
| — | 全市场清单 | `quote_universe` | HK 3,787 / SH 2,380 / SZ 2,969（全部 collectable） | ✅ |

> **北向注（实测，2026-09-30 复核）**：`stock_hsgt_hist_em` 的「净买入 / 买入额 / 卖出额」最后一个非空日 = **2024-08-16**；**「持股市值」自 2024-08-19 起改为季度披露**（仅季末 8 个点：2024-09-30 ~ 2026-06-30），其余日期为 0 → 北向在该源上**日频完全断流**。已入库历史净流入 2264 行（2014-11-17 ~ 2024-08-16），消费方必须按断点分段。
>
> **替代源结论（2026-11-06 排查完毕，不再列为待办）**：官方调整后按日只公布「成交总额 + 总成交笔数 + ETF 成交总额 + 前十大成交活跃证券及其成交额」——**没有买卖方向**，无法反推净流入；单只证券北向合计持有数量改为**季度**公布。当日实测：东财 `stock_hsgt_hist_em` 最新到 2026-11-05 但资金字段全 NaN、`stock_hsgt_fund_flow_summary_em` 北向行净买额/净流入 = 0、`stock_hsgt_hold_stock_em` / `stock_hsgt_stock_statistics_em` 直接报错、`push2` 的 kamt 接口 `hk2sh/hk2sz.dayNetAmtIn = 0`。**日频「北向净流入」已不存在任何合规数据源**（含付费源，2024-08-19 后均为估算或停更）。因此 `daily_northbound_flow` 定性为封存的历史序列，监控已置 `active=false`；若将来确需"北向活跃度"，只能新采「成交总额」（无方向，需另写交易所页面解析器）。
>
> 盘中实时链路（`tick_data` 2,231 万行 / `trend_snapshot` 92 万行 / `benchmark_minute`）对第③层**日频**指标非必需，本方案不动。

### 2.2 可复用的采集层机制（新任务必须遵守）

| 机制 | 载体 | 约定 |
|---|---|---|
| 调度 | `market_scheduler.py`（APScheduler，Asia/Hong_Kong） | 全局任务登记在 `GLOBAL_TASKS`；逐票任务在 `MARKET_PRESETS`；交易时段门控 `is_trading_hours` |
| 采集入口 | `collector_runtime.run_module()` | 所有入库脚本统一 `run(codes=None, ctx=None)`；富途调用必须走共享上下文（加锁串行 + 断线重连） |
| 写入 | `db.py` `upsert` / `bulk_upsert` | ON CONFLICT 幂等，业务唯一键；回填脚本支持 `--resume/--dry-run` |
| 建表 | 采集脚本内联 `CREATE TABLE IF NOT EXISTS` | **必须同步 `sql/schema.sql`**（AGENTS.md 强制，部署基准） |
| 监控 | `monitor_collector.py` + `monitor_table_config` / `monitor_task_config` | 新表新任务登记后自动纳入停滞告警 |
| 数据源防护 | `snap_guard.py`、东财退避策略、`log_utils.setup_logger` | 沿用 |
| 依赖 | `requirements.txt` | 新增第三方依赖必须同步（固定版本） |

---

## 3. 需新采集的数据清单（按层）

> 判定口径：**"需新采集"只包含真正要从外部拉取的数据**；凡可由现有表自算的指标（宽度、估值分位、拥挤度、ERP、信贷脉冲…）均归计算层，不进采集清单。

### 3.1 第①层 宏观（缺口最大）

| 数据项 | 现状 | 频率 |
|---|---|---|
| 中国国债收益率曲线（10Y/5Y/2Y/1Y）→ 期限利差、**A 股 ERP 分母** | ❌ 完全缺失（美债已有） | 日 |
| 信用利差（AAA/AA 中短期票据收益率） | ❌ | 日 |
| 社融存量同比、M1/M2 同比及剪刀差 | ❌ | 月 |
| 信贷脉冲（社融/GDP 二阶导） | ❌（自算，依赖社融 + GDP） | 月 |
| PMI（制造业/非制造业/新订单）、CPI、PPI、PPI-CPI 剪刀差、工业利润 | ❌ | 月 |
| HIBOR / 香港银行间流动性 | ❌ | 日 |
| 大宗商品（铜 / 原油 / 黄金） | ❌ | 日 |
| 中美利差、股债相关性 | —（自算） | 日 |
| 恒指波动率 | ❌ → **降级方案**：恒指 20/60 日已实现波动率（自算） | 日 |

### 3.2 第②层 资产配置

| 数据项 | 现状 | 频率 |
|---|---|---|
| 市场级估值长序列（全 A PE/PB 中位数，2005 起） | ❌（个股级 2018 起，不足） | 日 |
| 巴菲特指标 = 总市值 / GDP | 市值 2018 起；GDP ❌ | 月 |
| 股息率（市场级）− 国债利差 | A 股股息率字段几乎全空 → **自算**（`dividend_history` + 市值） | 日 |

### 3.3 第③层 市场状态

| 数据项 | 现状 | 频率 |
|---|---|---|
| 北向（净流入截断至 2024-08-16 + 持股市值） | ❌ 脚本已存在未启用；净流入已停披露 | 日 |
| A 股涨跌停家数 / 破净占比 / 新高新低 / % > MA | 可**自算**（涨跌停需分板块规则） | 日 |
| iVIX（50ETF 期权 QVIX） | ❌ | 日 |
| IPO 家数 / 首日涨幅 / 破发率 | ❌ | 日 |
| 新成立基金份额（爆款基金） | ❌ | 日 |
| 新增开户数 | ❌ | 月 |
| 解禁规模（未来 N 月） | ❌ | 日 |
| 产业资本净增减持 | ❌ | 日 |
| 港股行情 2015–2018 回填 | ❌ | 一次性 |
| 南向持股回填（→2017） | ❌ | 一次性 |
| 卖空历史回填 | ❌（需探 HKEX 归档） | 一次性 |
| 恒生综合指数 / 恒生行业指数 | ❌（可自算合成） | 日 |
| ETF 申赎 | ❌（P2） | 日 |

### 3.4 第④层 行业配置

| 数据项 | 现状 | 频率 |
|---|---|---|
| 申万一级行业指数日线（31 个，长历史） | ❌ | 日 |
| 行业资金流（A 股行业板块主力净流入） | ❌ | 日 |
| 行业估值（自算，2018+） | —（计算层） | 日 |
| 分析师一致预期**每日快照**（修正宽度差分依赖） | ❌（现为人工 JSON，不入库） | 日 |
| 中观景气数据（量价库存产能） | ❌ **单独立项，采集层不承接** | 周/月 |

---

## 4. 数据源选型

### 4.1 选型原则

沿用项目既有取舍：

1. **免费源优先**，付费/外采单独决策（仅分析师一致预期与中观数据考虑）；
2. **富途额度不足就换源**（先例：富途 K 线 100 只/7 天受限，回填改走新浪/AKShare，见 `backfill_a_market_turnover.py` 头注）；
3. **先探测后开发**（项目已有 `_probe_*.py` / `verify_sources_v2.py` 习惯）——免费源字段与历史深度常变；
4. 新增依赖必须同步 `requirements.txt`（固定版本 + 行尾用途注释）。

### 4.2 数据源矩阵

> 接口名为**候选**，标 〔探〕者必须先经 `_probe_macro_sources.py` 验证可用性与历史深度。
> **2026-09-30 已完成首轮探测**（39 候选 → 32 可用）：结论速查见 **§4.4**，完整明细见 `doc/macro_source_probe_report.md`。**本表与 §4.4 冲突时，以 §4.4 为准。**

| 数据项 | 首选源（候选接口） | 备选 | 可回溯（实测） | 频率 | 落库 | 优先级 |
|---|---|---|---|---|---|---|
| 中国国债收益率 | AKShare `bond_china_yield` ✅ | 中债官网直爬 | **≥2010**（按年窗口分段，单次跨度<1年，2002 窗口空） | 日 | `regime.macro_series` | **P0** |
| 市场级估值长序列 | AKShare 乐咕乐股全 A PE/PB ✅ | 中证指数官网 | 2005-01 起（**月频**，261 行） | 月（源即月频） | `regime.macro_series` | **P0** |
| iVIX | AKShare `index_option_50etf_qvix` ❌ 本机不可达 | 300ETF QVIX / 自算已实现波动率 | 待服务器验证/降级 | 日 | `regime.macro_series` | **P0**（降级预案） |
| 北向 | ❌ **净买入 + 成交额 2024-08-16 断流**；持股市值改季度 | **无**（2026-11-06 排查：官方只剩成交总额，无买卖方向） | 净流入 2014-11 ~ 2024-08 | 已停 | `daily_northbound_flow` | **已封存**（监控 active=false） |
| 南向持股回填 | ❌ **东财源仅近 1 年**（2021–2024 窗口全空） | 港交所 CCASS 持股（待调研） | 回填**不可行** → 改持续采集 | 一次性 → 改日频 | `daily_ggt_hold` | **P0 → 降级** |
| 恒指回填 | AKShare `stock_hk_index_daily_sina(HSI)` ✅ | `collect_benchmark_history.py` | **2013-08** 起（3227 行，覆盖 2015） | 一次性 | `daily_benchmark` | **P0** |
| 恒科回填 | AKShare sina 源（HSI 同族）⚠ / em 源 ❌ 被断连 | 富途 K 线 | 待试（sina 同族） | 一次性 | `daily_benchmark` | P1 |
| FRED 长历史回填 | 复用 `collect_benchmark_history.py`（FRED 1990 起） | — | 1990 | 一次性 | `daily_benchmark` | **P0** |
| 信用利差（AAA + **AA**） | ✅ AAA 走 `bond_china_yield`（一次 3 条曲线）；**AA 必须走另一接口** `bond_china_close_return(symbol='中短期票据(AA)')` | 中债官网 | AAA 10Y 2011 / 5Y 2010 起；**AA 源只保留最近 3 个交易日 → 逐日累积** | 日 | `regime.macro_series` + 指标 `RISK.CREDIT_SPREAD_AA` / `RISK.GRADE_SPREAD`（等级利差） | ✅ |
| 猪价 / 菜篮子（CPI 高频） | ✅ `spot_hog_year_trend_soozhu`（≈7.5 月窗）/ `macro_china_vegetable_basket`（2005 起完整日频） | 搜猪网 / 农业农村部 | 菜篮子 2005-09 起 5,822 行；猪价逐日累积 200 行 | 日 | `regime.macro_series` + `MACRO.PORK_PRICE` / `MACRO.VEG_BASKET_YOY` | ✅ |
| **核心 CPI** | ✅ **已落地**（2026-10-08，见 §7.17）：国家统计局新版「国家数据」直连 `queryIndexTreeAsync` → `queryIndicatorsByCid` → `stream/esData`。官方口径名「**不包括食品和能源**居民消费价格指数(上年同月=100)」 | 外采（需 2021 前历史时） | **2021-01 起 68 个月**（源库该口径仅 2021 起；更早时段只有旧八大类） | 月（日检幂等） | `regime.macro_series` `CN.CORE_CPI_YOY` + 指标 `MACRO.CORE_CPI_YOY` | ✅ |
| 社融 / M0M1M2 / CPI / PPI / PMI(含**新订单分项**) / 工业利润 / GDP / LPR / 用电量 / 消费者信心 | ✅ **已落地** `get_macro_monthly.py`（27 序列 + 2 派生） | NBS 月度库 / **中国人民银行官网**（社融，§7.24） | **社融存量 2016-01+ / 存量同比 2016-01+ / 增量 2016-01+（央行直连，替代停更的商务部镜像）** / CPI 2008+ / PPI 2006+ / GDP 2006+ / PMI 新订单 2005-01+ / 工业利润 2000-02+ / M0 2008-01+ | 月/季（日检幂等） | `regime.macro_series` | ✅ |
| **通胀预期（10Y 盈亏平衡）** | ✅ **已落地**（2026-10-09，见 §7.24）：FRED `T10YIE`（10Y 名义 − 10Y TIPS） | 外采（中债隐含通胀预期无免费源） | **2003-01 起 5,947 行**，最新 2.35% | 日（并入每小时全球基准采集） | `daily_benchmark` `US.T10YIE` + 指标 `MACRO.BREAKEVEN_10Y` | ✅ |
| **用电量 / 货运量 / 发电量（月度）** | ✅ **已落地**：用电量（2026-10-08 修复解析）、货运量、**发电量**（2026-10-09，见 §7.19，走 NBS 新版接口） | 国家统计局 | 用电量 2003-12 起 240 行；货运量 2000-01 起 291 行；**发电量 2000-02 起 280 行**（均至 2026-08） | 月 | `regime.macro_series`（`CN.ELECTRICITY_YOY` / `CN.FREIGHT_YOY` / `CN.POWER_GEN_YOY`） | ✅ |
| **产出缺口 / 美林投资时钟** | ✅ **自算**（2026-10-09，见 §7.21）：实际水平 = NBS **已季调**单季环比链成；潜在 = **单边** HP（λ=1600，逐期只用已发布样本）；时钟 = 缺口方向 × CPI 6 月方向 | 输入 = NBS 季度库（新接口） | 缺口 **2014Q1 起 ~50 季**（源链 2011Q1 + 12 季暖机）；单季同比输入 1993Q1 起 | 日（季度输入，PIT 填充） | `regime.indicator_value`：`MACRO.OUTPUT_GAP` / `MACRO.CLOCK_QUADRANT`（分类值，**不分位**） | ✅ |
| **OECD 综合领先指标 CLI** | ✅ **已落地**（2026-10-08，见 §7.18）：OECD 官方 **SDMX REST**（`sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1`）——即 `data-explorer.oecd.org` 可视化页背后的接口，**无鉴权、无 WAF、纯 requests 可取** | 外采（无必要） | **中国 1992-05 起 413 个月无缺口**；美/日/德/英 1980-01 起；韩 1990 起；G7/G20/G4E 聚合齐全；最新 **2026-09** | 月（并入 18:20 日检幂等） | `regime.macro_series`：`CN.CLI` / `US.CLI` / `JP.CLI` / `DE.CLI` / `KR.CLI` / `GLOBAL.CLI_G7` / `GLOBAL.CLI_G20` + 指标 `MACRO.CLI_CN` / `MACRO.CLI_GLOBAL` | ✅ |
| **中美利差 / 美债期限利差 10Y−2Y** | ✅ **已落地**（跨表派生：`macro_series` ↔ `daily_benchmark`） | 派生（中债 + FRED） | 中美利差 2006-03 起 5,008 行；美债 10Y−2Y 1990-12 起 8,737 行 | 日 | `regime.macro_series` + 指标 `MACRO.CN_US_SPREAD` / `MACRO.US_TERM_SPREAD` | ✅ |
| HIBOR / Shibor | ✅ **已落地** `get_macro_daily.py`（rate_interbank，隔夜/1W/1M/3M） | 香港银行公会 / 全国银行间同业拆借中心 | HIBOR **1996-07** 起、Shibor 2006 起、CNH Hibor 2013 起 | 日 | `regime.macro_series` | ✅ |
| 商品（铜/金/油） | ✅ **已落地**（CU0/AU0/SC0 连续合约收盘价） | FRED WTI | 铜 2005 / 金 2005 / 油 2005（源实际起点） | 日 | `regime.macro_series` | ✅ |
| 涨跌停家数 | **自算**（`change_pct` + 板块规则） | AKShare 涨停池 ✅（实测 57 只，与自算一致） | 1990（自算） | 日 15:40 | `regime.indicator_value` | P1 |
| 破净占比 | **自算**（`pb_ratio < 1`）〔源侧 akshare 接口 ❌ 失配〕 | — | 2018 | 日 | `regime.indicator_value` | P1 |
| 新高新低家数 | **自算**（全市场日线 20/60 日）〔源侧 akshare 接口 ❌ 失配〕 | — | 1990 | 日 | `regime.indicator_value` | P1 |
| IPO / 破发 | ✅ **已落地** `get_market_events.py`：`stock_xgsglb_em`（含待上市排期） | 交易所官网 | 2010-01 起（4,033 行，2,410 上市 / 383 待上市排期） | 日 19:30 | `regime.ipo_event` | ✅ P1 |
| 基金发行 | ✅ **已落地**：`fund_new_found_em` | 东财 | 2013-08 起（6,968 行；份额覆盖 96–100%） | 日 19:30 | `regime.fund_issuance_event` | ✅ P1 |
| 新增开户数 | AKShare `stock_account_statistics_em` ✅ | 中登公司 | 月频（101 行） | 月 | `regime.macro_series` | P1 |
| 解禁 | ✅ **已落地**：改用**个股明细** `stock_restricted_release_detail_em`（区间可拉，含占流通比） | 东财 | **2016-01 起 22,648 行 + 未来 180 天排期** | 日 19:30 | `regime.unlock_schedule` | ✅ P1 |
| 增减持 | ✅ **已落地**：换**巨潮** `stock_hold_management_detail_cninfo`（分增持/减持两次调用；东财源实测 ReadTimeout 弃用） | — | 源滚动近 1 年（29,976 行：事件级 15,359 / 定期报告 14,617） | 日 19:30 | `regime.insider_trade` | ✅ P1 |
| 申万一级行业指数 | ✅ **已落地**：`index_hist_sw`（31 个一级行业） | 申万官网 | **1999-12** 起，14.4 万行；已补**增量调度**（18:10） | 日 | `daily_benchmark`（`CN.SW*`） | ✅ P1 |
| 行业资金流 | ✅ **已换源落地**：`stock_fund_flow_industry`（**同花顺**；东财 push2 两轮探测均 ConnectionError） | 同花顺 | 源只给当前快照（约 90 个细分行业）→ 历史从上线起累积 | 日 15:35 | `regime.sector_fund_flow`（新表） | ✅ P1 |
| 退市股清单 | AKShare `stock_info_sh_delist` ✅ 159 条 / `stock_info_sz_delist` ✅ 208 条 | 交易所公告 | 全历史 | 一次性 | `stock_info.delisting/delist_date` | P1 |
| 卖空历史 | HKEX 日报归档 ❌ 旧日期全 404 | 港交所月报/其他归档（待调研） | **未解决** → 暂按 2026-06 起 | 一次性 | `daily_short_selling` | P1（待调研） |
| 交易日历 | AKShare `tool_trade_date_hist_sina` ✅ | — | 1990-12 ~ 2026-12（8797 行，A 股） | 一次性+年度更新 | `trading_calendar` | **P0** |
| 恒生行业指数 | **自算**（`stock_sector` 成分合成） | 恒生官网 | 2019 | 日 | `daily_benchmark` | P2 |
| 分析师一致预期 | ✅ **已落地** `get_analyst_forecast.py`：`stock_profit_forecast_em`（实测 **2,438 只**唯一代码 / 源 2930 行含重复；含研报数 + 5 档评级 + 4 财年 EPS；无净利润/营收/目标价） | 朝阳永续 / Wind（外采，需长历史时） | **快照从上线起**（源无历史序列，不可回填） | 日 18:40 | `regime.analyst_forecast_snapshot` | ✅ P1 |
| 沪深港通当日汇总 | AKShare `stock_hsgt_fund_flow_summary_em` ✅ | — | 仅当日（南向仍有值，北向需核对） | 日 | `daily_northbound_flow`（待定） | P2 |
| ETF 申赎 | 交易所每日 ETF 份额 | AKShare | 待探 | 日 | 待定 | P2 |

### 4.3 已知源风险与口径变化

| 项 | 风险 | 对策 |
|---|---|---|
| 北向资金 | ✅ 已精确定位断点：**净买入/买入额/卖出额最后一个非空日 = 2024-08-16**；**持股市值自 2024-08-19 起改为季度披露**（仅 8 个季末点） | 口径拆分：① 净流入指标只算到 2024-08-16 并标注断点（已入库 2264 行）；② 「持股市值」按季频使用、**不可当日频**；③ **日频替代源已确认不存在**（2026-11-06 实测：交易所/港交所只剩成交总额+笔数+十大活跃股，无买卖方向；个股持股改季度）→ 该表已封存（监控 active=false），不再作为待办 |
| 港股卖空历史 | ❌ 实测港交所日报归档（dayquot `dYYMMDDe.htm`）**2024/2022/2018 全部 404**，回填路径未找到 | 暂按「从 2026-06 起持续采集」；分位用短窗口并标注；P1 待调研月报/其他归档 |
| VHSI | 无稳定免费源 | 降级：恒指已实现波动率（20/60 日）；后续再评估外采 |
| A 股估值历史 | 个股 PE/PB 仅 2018 起 | 市场级用乐咕乐股长序列（**月频 2005 起**，实测 261 行）；个股级分位从 2018 起，标注口径 |
| 免费源反爬 | 实测：东财 push2 断连（行业资金流）、东财 datacenter 读超时（增减持）、em 恒科接口断连、乐咕乐股源格式变更导致 akshare 失配 2 例 | 沿用退避策略 + `collection_api_log` 成功率监控 + 探测脚本定期复核；受影响项走备用源或自算（见 §4.4） |
| AKShare 实现失配 | `stock_a_below_net_asset_statistics`（KeyError marketId）、`stock_a_high_low_statistics`（TypeError）在当前 akshare 1.18.60 下均不可用 | **一律降级自算**：破净用 `a_daily_quote.pb_ratio<1`；新高新低用全市场日线自算（且自算历史更长，反成优势） |
| 退市股 | 免费源对退市股覆盖不可靠 | 退市名单实测可得（沪 159 / 深 208 条）→ 先入库名单，再逐票试补；补不到的进入「已知缺失清单」，宽度指标做敏感性标注 |

### 4.4 探测结论速查（2026-09-30 实测，39 候选 → 32 可用）

> 由 `_probe_macro_sources.py` 实跑产出；完整明细（逐窗口行数/字段/报错）见 `doc/macro_source_probe_report.md`。

**A. 可直接开发（可立即写采集脚本）**

| 数据项 | 接口 | 实测深度 | 备注 |
|---|---|---|---|
| 中国国债收益率 | `bond_china_yield` | **≥2010**（2010/2015/2018/2022 各约 750 行/年） | 单次跨度<1年 → 回填 ≈17 次调用；2002 窗口实测为空 |
| 全A 估值 PE/PB | `stock_a_ttm_lyr` / `stock_a_all_pb` | 2005-01 起，**月频** 261 行 | 是月频！长周期估值分位可用，日频分位仍需个股自算（2018+） |
| 宏观月频包 | `macro_china_shrzgm` / `money_supply` / `cpi` / `ppi` / `pmi` | 136 / 224 / 224 / 248 / 225 行 | 日检幂等模式 |
| HIBOR | `rate_interbank("香港银行同业拆借市场","Hibor港币","1月"/"隔夜")` | **1996-07** 起 7450 行 | 另有 Hibor人民币（离岸 CNH）2013 起 |
| 商品（铜/金/油） | `futures_zh_daily_sina(CU0/AU0/SC0)` | 2005 / 2008 / 2018 起 | |
| 信用利差 | `bond_china_close_return` | 75 个品种枚举成功；中短期票据(AAA) 可取 | 单次窗口<1月 → 回填按月分段 |
| 北向历史 | `stock_hsgt_hist_em` | 2014-11-17 起 2762 行 | **2024-08-16 后资金字段全空**，持股市值仍在 |
| 涨停池（校核源） | `stock_zt_pool_em` | 当日 57 只（与自算 57 完全一致） | 自算方案的交叉验证通过 |
| IPO / 破发 | `stock_xgsglb_em` | 2010 起 4033 行 | |
| 基金发行 | `fund_new_found_em` | 2013 起 6917 行 | |
| 新增开户数 | `stock_account_statistics_em` | 月频 101 行 | |
| 解禁 | `stock_restricted_release_summary_em` | 未来窗口 62 行（2026-10~12） | **必须显式传 start/end**（akshare 默认写死 2022-11~12） |
| 交易日历 | `tool_trade_date_hist_sina` | 1990-12 ~ 2026-12，8797 行 | A 股日历；港股日历需另行派生 |
| 恒指历史 | `stock_hk_index_daily_sina("HSI")` | **2013-08** 起 3227 行 | 覆盖"回填到 2015"目标 |
| 退市名单 | `stock_info_sh_delist` / `stock_info_sz_delist` | 159 / 208 条 | 退市回补的起点 |
| 申万行业指数 | `index_hist_sw(symbol, "day")` | **1999-12** 起（示例 6465 行） | 31 个一级行业可全量回填 → daily_benchmark |
| 一致预期（近似） | `stock_profit_forecast_em` | 2930 只 | 含评级分布 + 预测 EPS，快照源 |
| 沪深港通当日汇总 | `stock_hsgt_fund_flow_summary_em` | 当日 4 行 | 南向有值；北向字段待核对 |

**B. 需替代方案（勿按原计划开发）**

| 数据项 | 实测问题 | 处置 |
|---|---|---|
| **南向持股历史回填** | 东财源**仅近 1 年**（2021/2022/2023/2024 窗口全空，2026-09 有值） | 回填取消；改持续日频采集；另找源（港交所 CCASS，P1 调研）；分位用短窗口并标注 |
| **港股卖空历史回填** | 港交所日报归档（dayquot）2024/2022/2018 全 404 | 暂按 2026-06 起；P1 调研月报/其他归档；分位短窗口 + 标注 |
| **北向资金（日频）** | 东财源 2024-08-19 起：净买额/买入额/卖出额全空，**持股市值也改季度披露**（仅 8 个季末点）；港交所 daily/monthly 子页 404 | 已入库历史净流入（2014-11-17 ~ 2024-08-16，2264 行）；持股市值按季频使用；日频替代源 P1 待定位 |
| iVIX | 本机网络不可达（Errno 60 timeout） | 服务器侧重试；不可得则降级为恒指/上证已实现波动率 |
| 增减持 | 东财 datacenter ReadTimeout（数据量大） | 分批拉取 / 换 `stock_hold_management_person_em` |
| 行业资金流 | 东财 push2 连接被断（疑似反爬；两轮探测均失败） | ✅ **已换同花顺源**（`stock_fund_flow_industry`）落地，见 §7.11；注意其行业分类 ≈90 个细分行业，**≠ 申万一级 31 个**，跨表分析需映射（P2） |
| 破净、新高新低 | akshare 实现与源失配（KeyError / TypeError） | **自算**（破净 `pb_ratio<1`；新高新低全市场日线）——自算历史更长，反成优势 |
| 恒科历史（em 源） | 连接被断 | 用 sina 同族接口（`stock_hk_index_daily_sina`，HSI 已验证） |

**C. 探测脚本用法**（保留为"源变更复核工具"，免费源变化时可重跑）

```bash
python3 _probe_macro_sources.py                          # 全部候选
python3 _probe_macro_sources.py --layer 3 --timeout 120  # 只看第③层
python3 _probe_macro_sources.py --only CN.BOND10Y,CN.PMI # 指定 series
python3 _probe_macro_sources.py --write doc/macro_source_probe_report.md
```

---

## 5. 存储方案

### 5.1 分层与 schema 规划

```
采集层（外部数据）        →  ① 原始表（现有风格，宽表/明细）
                              regime.macro_series / ipo_event / fund_issuance_event /
                              unlock_schedule / insider_trade / analyst_forecast_snapshot
                              + public.trading_calendar（共享工具表）
计算层（读原始表算指标）  →  ② 指标长表  regime.indicator_dict + regime.indicator_value
合成层（分位 → 综合指数） →  ③ 合成表    regime.market_regime_daily
```

- 新子系统独立 schema **`regime`**（沿用 `profile.*` 先例，避免污染 `public`、避免与共用同库的前端项目命名冲突）；
- `trading_calendar` 例外放 `public`（跨项目通用工具表；**若前端项目已有同类表则复用，勿建两份**）；
- 宽基/行业指数**不建新表**：复用 `daily_benchmark`，扩展 `bench_code` 命名空间（如 `CN.000985` 中证全指、`CN.SW801010` 申万一级行业、`HK.HSI_IND` 恒生行业）；
- **不建"市场宽度日频宽表"**：宽度/估值/情绪/资金全部作为 `indicator_value` 长表中的指标（如需宽表视图后续派生）。

### 5.2 建表 DDL 草案

> 落地约定：由新采集脚本内联 `CREATE TABLE IF NOT EXISTS ...`（与 `monitor_collector.py` / `get_hk_market_turnover.py` 一致），**并逐字同步 `sql/schema.sql`**（新增一节，节号按 schema.sql 现状编排）；改完依次跑：
> ```bash
> APP_DIR=$(pwd) python3 sql/_code_schema_diff.py      # 代码建表 vs schema 漏写
> APP_DIR=$(pwd) python3 sql/_dump_schema_from_db.py   # 库 → schema.sql 反向对齐
> APP_DIR=$(pwd) python3 sql/_schema_diff.py           # schema.sql → 库 差异
> ```

```sql
-- ============================================================================
-- 市场状态分析系统（Market Regime & Sentiment Analysis，独立 schema: regime）
-- 建表 DDL 由采集/计算脚本幂等执行，此处作为结构真相源登记，供新服务器部署与结构比对使用。
-- 设计说明见 doc/market_profile_collection_plan.md 与 doc/market_profile.md。
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS regime;

-- 1. 宏观与时序指标长表（月/季/日频统一承载；长表避免 30+ 张单指标表）
CREATE TABLE IF NOT EXISTS regime.macro_series (
    id              BIGSERIAL       PRIMARY KEY,
    series_code     VARCHAR(40)     NOT NULL,           -- 序列编码，如 CN.BOND10Y / CN.CPI_YOY / US.DGS10
    series_name     VARCHAR(80)     NOT NULL,
    period_date     DATE            NOT NULL,           -- 数据所属期（月频取发布月份首日，口径见 remark/extra）
    value           NUMERIC(20,6),
    unit            VARCHAR(16),                        -- pct / bp / index / yi_yuan / count ...
    freq            VARCHAR(8)      NOT NULL,           -- day / month / quarter
    market          VARCHAR(8)      NOT NULL DEFAULT 'CN',  -- CN / HK / US / GLOBAL
    source          VARCHAR(48),                        -- 采集来源，如 akshare:bond_china_yield / fred
    release_time    TIMESTAMPTZ,                        -- 实际可得时间（PIT，防前视）
    revision        INT             NOT NULL DEFAULT 0, -- 修订版本（同 period 多次修订各存一行）
    extra           JSONB,
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (series_code, period_date, revision)
);

COMMENT ON TABLE  regime.macro_series              IS '宏观/资产配置时序长表（采集层原始值，PIT：release_time + revision）';
COMMENT ON COLUMN regime.macro_series.series_code  IS '序列编码（命名空间：CN./HK./US./FX./CMDTY.）';
COMMENT ON COLUMN regime.macro_series.period_date  IS '数据所属期；月/季频为统计期，不含发布日期';
COMMENT ON COLUMN regime.macro_series.release_time IS '数据实际可得时间，PIT 回测按此过滤，禁止用 period_date 代替';
COMMENT ON COLUMN regime.macro_series.revision     IS '修订版本号，0=首次发布；宏观数据修订时新增行而非覆盖';
COMMENT ON COLUMN regime.macro_series.unit         IS 'pct=百分比 / bp=基点 / index=指数点 / yi_yuan=亿元 / count=计数';

CREATE INDEX IF NOT EXISTS idx_macro_series_code_date
    ON regime.macro_series (series_code, period_date DESC);

-- 最新修订视图（读接口默认走它）
CREATE OR REPLACE VIEW regime.v_macro_latest AS
SELECT DISTINCT ON (series_code, period_date) *
FROM regime.macro_series
ORDER BY series_code, period_date, revision DESC;

-- 2. 交易日历（共享工具表，放 public；区分 A 股 / 港股休市）
CREATE TABLE IF NOT EXISTS trading_calendar (
    market      VARCHAR(8)  NOT NULL,          -- CN / HK
    cal_date    DATE        NOT NULL,
    is_open     BOOLEAN     NOT NULL,
    src         VARCHAR(24),                   -- derived:a_daily_market_turnover / akshare / manual
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (market, cal_date)
);

COMMENT ON TABLE  trading_calendar          IS '交易日历（A股/港股分别维护；衍生自成交额表 + AKShare 校对）';
COMMENT ON COLUMN trading_calendar.src      IS '来源：derived:*=从现有表派生 / akshare / manual=人工修正';

-- 3. IPO 明细（一票一行，用于家数/首日涨幅/破发率）
-- 【已落地 2026-10-04】实现以 `sql/schema.sql` §5 为准，与草案差异见 §7.9 差异表
--   （新增 subscribe_date/board/issue_shares(万股)/lottery_rate/limit_up_days；
--     PK 改为 stock_code 且 list_date 可空 —— 源含「仅申购排期、未上市」的行，需承载前瞻排期）。
--   以下为设计留档。
CREATE TABLE IF NOT EXISTS regime.ipo_event (
    stock_code      VARCHAR(20)     PRIMARY KEY,
    stock_name      VARCHAR(80),
    market          VARCHAR(8)      NOT NULL,          -- SH / SZ / BJ / HK
    list_date       DATE            NOT NULL,
    issue_price     NUMERIC(12,4),
    issue_pe        NUMERIC(12,4),
    raised_amount   NUMERIC(20,2),                     -- 募资额（原币）
    first_day_open  NUMERIC(12,4),
    first_day_close NUMERIC(12,4),
    first_day_chg   NUMERIC(10,4),                     -- 首日涨跌幅 %
    is_break_issue  BOOLEAN,                           -- 首日破发（收盘 < 发行价）
    source          VARCHAR(32),
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, list_date)
);

COMMENT ON TABLE  regime.ipo_event                 IS 'IPO 明细（A股+港股；截面统计由查询聚合，不另建日频汇总表）';
COMMENT ON COLUMN regime.ipo_event.market          IS 'SH / SZ / BJ / HK';
COMMENT ON COLUMN regime.ipo_event.first_day_chg   IS '上市首日涨跌幅 %（相对发行价）';
COMMENT ON COLUMN regime.ipo_event.is_break_issue  IS '首日是否破发：TRUE=收盘价 < 发行价';

CREATE INDEX IF NOT EXISTS idx_ipo_event_list_date
    ON regime.ipo_event (list_date DESC);

-- 4. 基金成立事件（爆款基金信号：按成立日聚合份额）
-- 【已落地 2026-10-04】实现以 `sql/schema.sql` §6 为准：新增 company/subscribe_period/manager，
--   去掉 issue_amount（源无募集金额，仅有募集份额亿份；且源侧份额滞后 1–2 日 → upsert 用
--   skip_null_updates 保护已入库值）。以下为设计留档。
CREATE TABLE IF NOT EXISTS regime.fund_issuance_event (
    fund_code       VARCHAR(20)     PRIMARY KEY,
    fund_name       VARCHAR(120),
    setup_date      DATE            NOT NULL,          -- 基金成立日
    fund_type       VARCHAR(24),                       -- 股票型/混合型/债券型/指数型/...
    issue_share     NUMERIC(20,4),                     -- 成立份额（亿份）
    issue_amount    NUMERIC(20,2),                     -- 成立规模（亿元）
    source          VARCHAR(32),
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);

COMMENT ON TABLE  regime.fund_issuance_event            IS '新成立基金明细（按 setup_date 聚合出"发行热度/爆款"信号）';
COMMENT ON COLUMN regime.fund_issuance_event.issue_share IS '成立份额（亿份），首募规模是市场情绪核心代理';

CREATE INDEX IF NOT EXISTS idx_fund_issuance_date
    ON regime.fund_issuance_event (setup_date DESC);

-- 5. 限售解禁排期（未来 N 月压力测算）
-- 【已落地 2026-10-04】实现以 `sql/schema.sql` §7 为准，源改用**可按日期区间拉的个股明细**
--   `stock_restricted_release_detail_em`（原草案想的 `*_queue_em` 需逐股循环，不可行）；
--   新增 actual_shares / float_ratio（占解禁前流通市值比例，压力权重）/ pre_close；
--   holder_type 设 NOT NULL DEFAULT '未知'（UNIQUE 键含该列，NULL 会破坏幂等）。
--   实测单次可拉 2016–2026 全区间（24,893 行）→ 已回填 2016 起 22,648 行。以下为设计留档。
CREATE TABLE IF NOT EXISTS regime.unlock_schedule (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    unlock_date     DATE            NOT NULL,
    holder_type     VARCHAR(32),                       -- 首发原股东/定增/股权激励/其他
    unlock_shares   BIGINT,                            -- 解禁股数
    unlock_value    NUMERIC(20,2),                     -- 解禁市值（按公告日均价估算）
    source          VARCHAR(32),
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, unlock_date, holder_type)
);

COMMENT ON TABLE  regime.unlock_schedule               IS '限售股解禁排期（A股；港股基石/禁售期数据源弱，暂不覆盖）';
COMMENT ON COLUMN regime.unlock_schedule.holder_type   IS '解禁股东类型：首发原股东/定增/股权激励/其他';

CREATE INDEX IF NOT EXISTS idx_unlock_date
    ON regime.unlock_schedule (unlock_date DESC);

-- 6. 产业资本增减持（事件级；去重键模式对齐 dividend_history）
-- 【已落地 2026-10-04】实现以 `sql/schema.sql` §8 为准：源由「东财 datacenter（实测 ReadTimeout）」
--   改为**巨潮 cninfo**（分「增持/减持」两次调用，含事件级临时公告 + 定期报告两类口径）；
--   新增 stock_name/end_date/avg_price/end_shares/event_value/data_kind；
--   dedup_key 为 md5（源侧存在关键字段完全相同的重复行，入库前必须显式去重）；
--   **北交所（BJ.*）不采**：源侧「变动数量」量纲失真（实测 1168 条事件级记录占全市场金额 65%，
--   含 79 亿股级别异常值），且不在本项目 A 股池内。以下为设计留档。
CREATE TABLE IF NOT EXISTS regime.insider_trade (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    ann_date        DATE            NOT NULL,          -- 公告日
    trade_date      DATE,                              -- 变动日期（可空）
    holder_name     VARCHAR(120),
    holder_type     VARCHAR(32),                       -- 高管/持股5%以上股东/...
    direction       VARCHAR(8)      NOT NULL,          -- BUY / SELL
    change_shares   BIGINT,
    change_ratio    NUMERIC(10,6),                     -- 变动比例 %
    change_value    NUMERIC(20,2),
    source          VARCHAR(24)     NOT NULL,
    dedup_key       VARCHAR(120)    NOT NULL,          -- 源内去重键（对齐 dividend_history 模式）
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, source, dedup_key)
);

COMMENT ON TABLE  regime.insider_trade            IS '产业资本增减持明细（底部/顶部信号清单依赖：净增持=底部信号，净减持激增=顶部信号）';
COMMENT ON COLUMN regime.insider_trade.direction  IS 'BUY=增持 / SELL=减持';
COMMENT ON COLUMN regime.insider_trade.dedup_key  IS '源内唯一去重键（如 公告日+股东+股数哈希），避免重复落库';

CREATE INDEX IF NOT EXISTS idx_insider_ann_date ON regime.insider_trade (ann_date DESC);
CREATE INDEX IF NOT EXISTS idx_insider_stock_date ON regime.insider_trade (stock_code, ann_date DESC);

-- 7. 分析师一致预期每日快照（Revision Breadth 靠跨日 diff；不可事后补算）
-- 【已落地，形态按真实源修正】原草案为 (snapshot_date, stock_code, forecast_year) 长表，
-- 含 np_avg/revenue_avg/rating_avg；**实测源 `stock_profit_forecast_em` 是宽表**
-- （4 个财年 EPS 列 + 5 档评级计数，无净利润/营收/目标价）→ 改为「一股一日一行」宽表：
--   行数 2.9M/年 → 0.73M/年；财年标签随值存 fyN_year，跨年 diff 时按标签对齐。
CREATE TABLE IF NOT EXISTS regime.analyst_forecast_snapshot (
    snapshot_date   DATE            NOT NULL,          -- 快照日（采集日）
    stock_code      VARCHAR(20)     NOT NULL,
    stock_name      VARCHAR(50),
    report_count    INT,                               -- 研报数（近六个月覆盖机构数的近似）
    rating_buy      INT,                               -- 近六个月机构评级分布（5 档计数）
    rating_overweight INT,
    rating_neutral  INT,
    rating_reduce   INT,
    rating_sell     INT,
    fy1_year        SMALLINT,                          -- 最近财年标签（源列名逐年滚动，必须随值一起存）
    fy1_eps         NUMERIC(12,4),
    fy2_year        SMALLINT,
    fy2_eps         NUMERIC(12,4),
    fy3_year        SMALLINT,
    fy3_eps         NUMERIC(12,4),
    fy4_year        SMALLINT,
    fy4_eps         NUMERIC(12,4),
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:stock_profit_forecast_em',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (snapshot_date, stock_code, source)
);

COMMENT ON TABLE  regime.analyst_forecast_snapshot               IS '分析师一致预期每日快照（修正宽度的差分依赖；必须逐日采集，历史不可回溯重建）';
COMMENT ON COLUMN regime.analyst_forecast_snapshot.snapshot_date IS '快照日期（采集日）；同一天重复运行幂等覆盖';
COMMENT ON COLUMN regime.analyst_forecast_snapshot.fy1_year      IS '财年标签：源列名（如「2026预测每股收益」）逐年滚动，年份必须随值存，跨年比较须按标签对齐';

CREATE INDEX IF NOT EXISTS idx_forecast_stock_date
    ON regime.analyst_forecast_snapshot (stock_code, snapshot_date DESC);

-- 8. 指标字典（口径/方向/频率/来源登记，禁止口径漂移）
CREATE TABLE IF NOT EXISTS regime.indicator_dict (
    indicator_code  VARCHAR(48)     PRIMARY KEY,       -- 如 BREADTH.ADV_RATIO / VAL.PE_MEDIAN / FSI.COMPOSITE
    indicator_name  VARCHAR(80)     NOT NULL,
    layer           SMALLINT        NOT NULL,          -- 1=宏观 2=资产配置 3=市场状态 4=行业
    dimension       VARCHAR(24)     NOT NULL,          -- 宽度/估值/情绪/资金/风险偏好/金融压力/风格/宏观/景气
    market_scope    VARCHAR(16)     NOT NULL,          -- CN / HK / CN+HK / GLOBAL
    unit            VARCHAR(16),                       -- pct / ratio / count / bp / score_0_100 ...
    direction       VARCHAR(8)      NOT NULL,          -- high_risk / high_good / neutral（分位化方向统一依据）
    freq            VARCHAR(8)      NOT NULL,          -- day / week / month / quarter
    source          VARCHAR(64),                       -- 依赖表/数据源
    formula         TEXT,                              -- 计算口径（可追溯到代码函数）
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    remark          VARCHAR(200),
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);

COMMENT ON TABLE  regime.indicator_dict             IS '指标字典：所有指标的口径/方向/频率登记（新增指标须先进字典）';
COMMENT ON COLUMN regime.indicator_dict.direction   IS 'high_risk=越高越危险 / high_good=越高越好 / neutral；分位化与合成按此统一方向';
COMMENT ON COLUMN regime.indicator_dict.formula     IS '计算口径描述（引用实现函数），保证可解释、可追溯';

-- 9. 指标时序长表（原始值 + 历史分位同表；条数极少：200 指标 × 2 市场 × 250 日 ≈ 10 万行/年）
CREATE TABLE IF NOT EXISTS regime.indicator_value (
    indicator_code  VARCHAR(48)     NOT NULL,
    market          VARCHAR(8)      NOT NULL,          -- CN / HK / GLOBAL
    trade_date      DATE            NOT NULL,
    raw_value       NUMERIC(20,6),
    percentile      NUMERIC(6,2),                      -- 0-100 历史分位（原始值分位，未做方向调整）
    extra           JSONB,                             -- 明细（如顶部信号命中清单）
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (indicator_code, market, trade_date)
);

COMMENT ON TABLE  regime.indicator_value               IS '指标时序长表：口径见 indicator_dict；raw_value=原始值，percentile=历史分位(0-100)';
COMMENT ON COLUMN regime.indicator_value.percentile    IS '原始值的历史分位（0-100，expanding 只用当日及之前，无前视；未做方向调整——方向见 indicator_dict.direction，合成/展示由消费方取反）';

CREATE INDEX IF NOT EXISTS idx_indicator_value_date
    ON regime.indicator_value (trade_date DESC, market);

-- 10. 市场状态合成（温度计/季节/风险/FSI；一市场一日一行）
-- ⚠【历史留档，勿照抄】该表已于 2026-11-05 彻底删除（零消费方），
--   DDL 已从 sql/schema.sql / regime_schema.py 移除；当前规格见 market_state_data_architecture.md。
CREATE TABLE IF NOT EXISTS regime.market_regime_daily (
    trade_date          DATE        NOT NULL,
    market              VARCHAR(8)  NOT NULL,          -- CN / HK
    thermometer         NUMERIC(6,2),                  -- 市场温度计 0-100（天气）
    season              VARCHAR(8),                    -- 冬/春/夏/秋
    season_score        NUMERIC(6,2),
    risk_score          NUMERIC(6,2),                  -- 风险指数 0-100
    fsi                 NUMERIC(6,2),                  -- 金融压力指数 0-100
    risk_appetite       NUMERIC(6,2),                  -- 跨资产风险偏好 0-100（GLOBAL 维度）
    top_signal_cnt      SMALLINT,                      -- 顶部信号命中数（0-18）
    bottom_signal_cnt   SMALLINT,
    detail              JSONB,                         -- 命中清单/分项明细
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (trade_date, market)
);

COMMENT ON TABLE  regime.market_regime_daily                IS '市场状态合成表（温度计/季节/风险/FSI），由计算层写入';
COMMENT ON COLUMN regime.market_regime_daily.season         IS '冬=熊市中后段 / 春=熊末牛初 / 夏=牛市中段 / 秋=牛市末段';
COMMENT ON COLUMN regime.market_regime_daily.top_signal_cnt IS '顶部预警信号命中数（见 market_profile.md §8.2，0-3正常/4-6警惕/7-9高风险/≥10极高风险）';

-- 11. stock_info 补列（退市标注，配合退市股回补与宽度敏感性分析）
ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS delist_date DATE;
COMMENT ON COLUMN stock_info.delist_date IS '退市日期（NULL=未退市）；配合 delisting 布尔列用于幸存者偏差敏感性分析';
```

**监控登记（建表后随 DDL 一并 INSERT，幂等）：**

| 表 | period | expect_lag | 说明 |
|---|---|---|---|
| `regime.macro_series` | `lowfreq`（time_column=`updated_at`） | 3 | 日频序列（国债）天天刷新 `updated_at`；月频序列靠它一起变新 |
| `trading_calendar` | `lowfreq`（`updated_at`） | 7 | |
| `regime.indicator_dict` | `lowfreq`（`updated_at`） | 30 | 字典随计算任务同步（2026-09-30 起 active） |
| `regime.indicator_value` | `day`（`updated_at`） | 1 | ✅ 2026-09-30 起 active（计算层上线，工作日 18:45） |
| `regime.market_regime_daily` | `day`（`updated_at`） | 1 | ✅ 同上 |
| `regime.ipo_event` | `day` | 5 | P1 待建（无 IPO 的日子本来就不更新，滞后容忍放大） |
| `regime.fund_issuance_event` | `day` | 5 | P1 待建 |
| `regime.unlock_schedule` | `day` | 5 | P1 待建 |
| `regime.insider_trade` | `day` | 3 | P1 待建 |
| `regime.analyst_forecast_snapshot` | `day` | 2 | P1 待建（**逐日必须更新**，差分依赖，缺一天即断点） |

### 5.3 单位与口径约定（采集层必须统一）

| 项 | 约定 |
|---|---|
| 金额单位 | 新表统一**原币**（A 股元 / 港股港元），列注释标明；`macro_series.unit` 记录（亿元/元/点/bp/pct） |
| 前复权 | 港股回填沿用 QFQ，除息后需重跑（`backfill_hk_market_turnover.py` 已有先例） |
| 宽度口径 universe | **A 股**：排除 B 股（`SH.90*` / `SZ.20*`）、排除上市 < 60 日次新；**港股**：设流动性门槛（建议 30 日均成交额 ≥ 100 万港元 且 市值 ≥ 5 亿港元，**具体阈值待标定**，先记录在 `indicator_dict.remark`） |
| 停牌处理 | 停牌股自算指标时视作「不涨不跌」，分母是否含停牌股在 `indicator_dict.formula` 明示 |
| 涨跌停判定 | 按板块规则：主板 ±10% / 创业板·科创板 ±20% / ST ±5% / 北交所 ±30%，阈值取 ±(limit−0.1pp) 防浮点误差 |
| 北向口径断点 | **2024-08-16** 为净买入/成交额最后一个有效日；此后仅「持股市值」可得 → 指标计算须按日期分段，禁止跨断点连续比较 |

### 5.4 PIT 与快照策略

| 数据 | 机制 |
|---|---|
| 个股日截面 | 现有全市场日线池**逐日落地即天然 PIT**（`a_daily_quote` / `hk_daily_quote`），无需额外快照机制 |
| 宏观 | `release_time` + `revision` 双字段（读接口默认走 `regime.v_macro_latest`） |
| 一致预期 | 逐日快照（`snapshot_date`），**无法事后补算**，因此必须尽早启动采集 |
| 成分股 | `stock_sector` 为当前快照无历史 → 跨期行业分析需在 `indicator_dict.formula` 标注「用当前成分回溯」的已知偏差 |

---

## 6. 采集频率与调度

### 6.1 频率矩阵

| 数据 | 频率 | 时点 | 落地方式 |
|---|---|---|---|
| 现有日频采集（行情/成交额/两融/南向/卖空/回购） | 日 | 已在跑（16:10–18:30 多档） | 现有 `GLOBAL_TASKS` 不动 |
| **① 市场层指标计算**（宽度/估值/情绪/资金/FSI/季节） | 日 | A 股 16:30 / 港股 **18:40**（等沽空 18:30 终值） | 新增计算 job（读库算，不采集） |
| 涨跌停自算、破净、新高新低 | 日 | A 股 15:40 | 挂 A 股收盘任务后 |
| 北向（持股市值 + 净流入历史截断） | 日 | 16:10 | 改造 `get_northbound.py`：净流入已 2024-08-16 断流；成交额源待定位（见 §4.4-B） |
| 中国国债 / 信用利差 / HIBOR / 商品 / 市场估值序列 / iVIX | 日 | **18:30**（避交易时段，实测已登记） | `get_macro_daily.py` |
| IPO / 基金发行 / 解禁 / 增减持 | 日 | 19:30 | 新 `get_market_events.py` |
| 一致预期快照 | 日 | **18:40**（实测） | `get_analyst_forecast.py` |
| 申万一级行业指数 | 日 | **18:10**（实测，增量更新） | `backfill_sw_industry.py`（默认增量，只 upsert 尾部） |
| 行业资金流 | 日 | **15:35**（A 股收盘后） | `get_sector_fund_flow.py` → `regime.sector_fund_flow`；**源只给当前快照，漏采即永久缺失** |
| 宏观月/季频（PMI/CPI/PPI/社融/M1M2/工业利润/开户数） | **每天检查、发布即落库**（幂等 upsert） | **18:20**（实测，单次日检） | 「日检-幂等」模式，免维护官方发布日历 |
| 板块成分/权重、财务、分红 | 周 | 已有（周二/周一/周六） | 复用 |
| 历史回填 | 一次性 | — | `backfill_*`，支持 `--resume`（见附录 B） |

### 6.2 新增调度任务（已登记，2026-09-30）

**已落地（P0-2，`market_scheduler.py` 的 `GLOBAL_TASKS`，重启后运行进程日志显示「共计注册 107 个任务」）：**

```python
# 中债收益率曲线（国债 + 中短期票据AAA + 商业银行普通债AAA，官方盘后发布）
("中债收益率曲线", "get_cn_bond.py",
 {"hour": 19, "minute": 0, "day_of_week": "mon-fri"}, None, True, None, 300),
# 全A 市场级估值（乐咕乐股，月频序列但月内滚动更新）
("全A市场估值", "get_macro_valuation.py",
 {"hour": 19, "minute": 10, "day_of_week": "mon-fri"}, None, True, None, 300),
# 交易日历维护（A股 AKShare 预排 + 港股由成交额表派生）
("交易日历维护", "init_trading_calendar.py",
 {"hour": 8, "minute": 45, "day_of_week": "mon-fri"}, None, True, None, 300),

# 市场状态计算层（读原始表算指标 → regime.indicator_value / market_regime_daily）
("市场状态计算", "market_state_daily.py",
 {"hour": 18, "minute": 45, "day_of_week": "mon-fri"}, None, True, None, 1800),

# 宏观包（P1）：月/季频 + 日频（PIT 要求排在计算层之前）
("宏观月频采集", "get_macro_monthly.py",
 {"hour": 18, "minute": 20, "day_of_week": "mon-fri"}, None, True, None, 600),
("宏观日频采集", "get_macro_daily.py",
 {"hour": 18, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 600),

# 分析师一致预期每日快照（P1）：18:40，早于计算层；漏采一天即永久断档
("一致预期快照", "get_analyst_forecast.py",
 {"hour": 18, "minute": 40, "day_of_week": "mon-fri"}, None, True, None, 600),

# 市场情绪事件（P1）：19:30；事件指标为「公告先行/滞后一日」口径，晚于计算层无碍
("市场事件采集", "get_market_events.py",
 {"hour": 19, "minute": 30, "day_of_week": "mon-fri"}, None, True, None, 900),
```

**已登记任务（实测时刻，2026-10-04 重启后共 112 个任务）：**

| 任务名 | 脚本 | 触发（mon-fri） | 说明 |
|---|---|---|---|
| 交易日历维护 | `init_trading_calendar.py` | 08:45 | A股 AKShare 预排 + 港股由成交额表派生 |
| 宏观月频采集 | `get_macro_monthly.py` | 18:20 | PMI/CPI/PPI/社融/M1M2 等，日检-幂等（发布即落库） |
| 宏观日频采集 | `get_macro_daily.py` | 18:30 | HIBOR / 商品 / 信用利差派生 |
| 一致预期快照 | `get_analyst_forecast.py` | 18:40 | 逐日快照，**不可断档**（源无历史序列） |
| 市场状态计算 | `market_state_daily.py` | 18:45 | 读原始表算指标 + 分位 + 合成（不采集） |
| 市场事件采集 | `get_market_events.py` | 19:30 | IPO / 基金发行 / 解禁 / 增减持（四类一次跑完） |
| 行业资金流 | `get_sector_fund_flow.py` | 15:35 | 同花顺源（东财已断连）；收盘后采集，只给当前快照 |
| 申万行业指数 | `backfill_sw_industry.py` | 18:10 | 增量更新（调度不传参 → run() 默认 incremental=True） |

**待落地（P1 剩余）：**

| 拟用任务名 | 脚本 | 触发 | 说明 |
|---|---|---|---|
| 北向（待替代源） | 待定 | — | 东财源已断流；待定位港交所/交易所源后再挂 |

> 说明：调度器代码改动**必须重启进程**才生效（`launchctl kickstart -k gui/501/com.equity.a-scheduler`，本机 macOS）。

### 6.3 新脚本命名与实现约定

- 命名：宏观 `get_macro_*.py`、事件 `get_market_events.py`、行业 `get_industry_*.py`、一致预期 `get_analyst_forecast.py`、回填 `backfill_market_valuation.py` 等；
- 统一入口 `run(codes=None, ctx=None)`（宏观/事件类不需要 `codes`，但签名保持兼容，便于调度器统一调用）；
- 写入一律 `db.upsert` / `db.bulk_upsert` + 业务唯一键；**禁止**绕开 `db.py` 直连写；
- 日志走 `log_utils.setup_logger`；接口调用埋点对齐 `collection_api_log` 的 api_name 命名；
- 内联 `CREATE TABLE IF NOT EXISTS` 与 `schema.sql` 逐字一致；新表登记 `monitor_table_config`；
- 新增第三方依赖 → 同步 `requirements.txt`（分组注释 + 固定版本 + 用途）。

---

## 7. 分期落地路线

### 7.1 P0（5–8 人日）——补短板 + 验证新源

| # | 任务 | 产出 | 验收标准 | 状态 |
|---|---|---|---|---|
| 1 | `_probe_macro_sources.py` 探测全部候选接口 | `doc/macro_source_probe_report.md` + 本文 §4.4 | 39 候选 → 32 可用；6 项不可用均有替代方案 | ✅ 2026-09-30 |
| 2 | 回填：**恒指**、FRED 长历史、**国债**、**申万行业**、交易日历、退市名单、北向历史（南向/卖空回填已确认不可行，见 §4.4-B） | `daily_benchmark` / `regime.macro_series` / `trading_calendar` / `stock_info` | 见 §7.5 实测数字 | ✅ 2026-09-30 |
| 3 | 采集新源：中国国债收益率（`bond_china_yield` 按年分段）+ 市场级估值（乐咕乐股月频） | `regime.macro_series` | 国债起点 2006-03（≤2010 达标）；估值起点 2005-01 | ✅ 2026-09-30（iVIX 待服务器验证，降级预案见 §4.4-B） |
| 4 | 北向：净流入历史入库（截断 2024-08-16）；日频替代源 P1 定位 | `daily_northbound_flow` | 历史完整 2014-11-17 ~ 2024-08-16（2264 行）且断点标注 | ✅ 2026-09-30（替代源待办） |
| 5 | 建 `regime.*`（4 表）+ `public.trading_calendar`，登记 `sql/schema.sql` + 监控 | DDL + schema 同步 + 监控登记 | `_code_schema_diff.py` 无漏写；`_schema_diff.py` 无本项目差异；监控登记生效 | ✅ 2026-09-30 |
| 6 | 挂调度（3 个新任务）并重启 scheduler | `market_scheduler.py` GLOBAL_TASKS | 运行进程日志出现「共计注册 107 个任务」含新任务 | ✅ 2026-09-30 |

### 7.2 P1（15–20 人日）——宏观包 + 情绪事件包 + 行业

- ✅ **计算层**（22 指标 + 3 合成 + 季节 + 顶底信号 → `regime.indicator_value` / `market_regime_daily`，已挂 18:45 调度）——详见 §7.6；
- ✅ 申万一级行业指数回填（31 行业 → `daily_benchmark`）、退市股清单回补（361 条）——P0-2 一并完成；
- ✅ **宏观包**（月/季频 20 序列 + 日频拆借利率/商品 + 4 个利差派生；计算层已接入 FSI 信用维度与季节 v2）——详见 §7.7；~~注：工业利润源不可得，用**工业增加值同比**替代~~（**2026-10-09 订正：工业利润经 NBS 月度库已落地**，见 §7.20）；开户数源停更于 2023-08；
- ✅ **分析师一致预期每日快照**（宽表 2,438 只/日 + 修正宽度指标）——详见 §7.8；
- ✅ **A 股情绪事件包**（IPO/基金发行/解禁/增减持 4 张表 + 7 指标，解禁回填 2016 起）——详见 §7.9；
  破净/涨跌停/新高新低**已由计算层自算实现**；
- ✅ **行业层**（申万一级指数增量调度 + 同花顺行业资金流换源 + 6 个行业指标）——详见 §7.11；
  **逐行业横截面（`sector_daily`：RS×资金流×排名）与申万↔同花顺映射已于 §7.13 完成**；
  余下 P2：行业估值快照（PE/PB 无历史源，需逐日累积）、RRG 可视化；
- ⬜ 港股卖空/南向历史探源、北向日频替代源定位。

### 7.3 P2（需外采决策）

- 分析师一致预期：先上东财免费近似（**快照从上线日起累积，越早越值钱**），同步评估朝阳永续/Wind 采购成本；
- 恒生行业指数自算合成；ETF 申赎；
- **行业估值快照**（`sw_index_first_info` 的 PE/PB/股息率只有当前快照 → 逐日快照累积，与一致预期快照同类约束）；**已于 §7.14 完成**；
  余下：基于 `sector_daily` 的 RRG（相对强度-动量四象限）可视化；
- 中观景气数据：单独立项（不在本方案范围）。

### 7.4 全局验收口径

1. 所有新表在 `sql/schema.sql` 有逐字登记，且 `_code_schema_diff.py` / `_schema_diff.py` / `_dump_schema_from_db.py` 三脚本无差异；
2. 每个新指标在 `regime.indicator_dict` 有登记（口径/方向/频率/来源四要素齐全）；
3. 每个新任务在 `monitor_table_config` / `monitor_task_config` 有登记，`collection_alert` 无长期未决告警；
4. 数据深度承诺与 §2.1 实测基线一致，所有降级项在指标字典 `remark` 中明示。

### 7.5 P0-2 落地记录（2026-09-30）

**实测数据结果（全部入库并核对）**

| 数据 | 落地对象 | 实测结果 |
|---|---|---|
| 中债收益率曲线 | `regime.macro_series` | 10.75 万条：国债 `CN.BOND_*` **2006-03-01 起**（8 期限）；中短期票据 AAA `CN.MTN_AAA_*` 2006-12 起（7 期限，无 30Y）；商业银行普通债 AAA `CN.BANK_AAA_*` 2009-12 起（8 期限）。**信用利差源顺带解决**（同一接口返回 3 条曲线，差值由计算层算） |
| 全A 市场级估值 | `regime.macro_series` | 5 序列 × 261 行 = 1305 条，**2005-01-31 起（月频）**；乐咕乐股自带的全历史/近10年分位存入 `extra` JSONB |
| 交易日历 | `public.trading_calendar` | CN 8798 行（1990-12-19 ~ 2026-12-31，AKShare 预排 + 实际交易日补 1 天）、HK 1906 行（2019-01-02 ~ 2026-09-30，由成交额表派生） |
| 退市名单 | `stock_info.delisting / delist_date` | 361 条（沪 159 + 深 208），全部带退市日期；年份分布 2026:19 / 2025:31 / 2024:55 / 2023:46 / 2022:44 …（⚠ 早年退市股名单可能不全，宽度敏感性分析时注意） |
| 申万一级行业指数 | `daily_benchmark`（`CN.SW*`） | 31 个行业、143,530 行；15 个行业 1999-12 起、12 个 2014-02 起、3 个（石油石化/环保/美容护理）2021-12 起（源数据如此，非缺采） |
| 恒生指数 | `daily_benchmark` | **2013-08-20 起** 3228 行（sina 源；指数不复权，与富途重叠区间数值一致） |
| FRED 长历史 | `daily_benchmark` | NASDAQCOM 1990-01 起（9254 行）、VIXCLS 1990-01 起（9283 行）、DTWEXBGS 2006-01 起（5198 行）；**SP500/DJIA 受 FRED 源本身限制仅到 2016-09**（非采集问题） |
| 北向净流入历史 | `daily_northbound_flow` | 2264 行（2014-11-17 ~ 2024-08-16）；断点后无数据，脚本打印断流提示且不写脏数据 |

**同时修掉的既有缺陷 / 基建增强（计划外，但阻塞验收）**

| 项 | 问题 | 处置 |
|---|---|---|
| `monitor_collector.data_stale` | `_compute_window_start` 解包 5 元素而 SQL 查 6 列 → 每次抛 `ValueError`，**data_stale 巡检自 2026-08-22 起实际停摆**（该日即最后一次告警时间） | 修元组解包；实测 `_check_data_stale()` 跑通 |
| `monitor_collector` 多 schema | 巡检只用 `table_name` 拼 SQL，无法监控 `regime.*` | 读 `db_name`，非 public 拼限定名（public 保持裸名，不影响既有告警 source） |
| `db.py` | ① `sql.Identifier('regime.macro_series')` 生成带点怪表名 → relation does not exist；② dict/list（JSONB）报 `can't adapt type 'dict'` | 新增 `_table_ident / _table_alias / _adapt_value`，`upsert` 与 `bulk_upsert` 均支持；143 项测试 + 冒烟验证通过 |
| `sql/_schema_diff.py` | 视图正则不支持 `schema.view`（把 `regime` 当视图名）→ 每次误报「视图 regime 缺失」 | 正则支持限定名；非 public 视图与表一致剔除 |
| `stock_info.delist_date` | 方案声明但库里无此列 | `backfill_delisted.py` 内幂等 ALTER + `schema.sql` 登记 |

**遗留 / 待办（转 P1）**

1. **北向日频替代源**（港交所 / 沪深交易所官网成交额）——P0 唯一未闭环的口径；
2. **南向持股历史**（东财仅近 1 年）与**卖空历史**（港交所日报归档 404）——均需另找源；
3. **iVIX** 服务器侧验证、**行业资金流**换源、**增减持**分批拉取；
4. **退市股历史行情试补**（`backfill_delisted_quotes.py`，另立）——直接影响宽度口径完整性；
5. ~~**计算层**（`indicator_dict` 指标登记 + `indicator_value` 写入 + `market_regime_daily`）→ P1~~ → ✅ 已于同日晚间落地，见 §7.6；
6. `a_daily_quote.ps_ttm_ratio / pcf_ttm_ratio`：真实库仍存在、`schema.sql` 已声明下线（**有意为之的待删列**）→ `_schema_diff.py` 会持续提示高危 DROP，人工确认后执行即可，**不是缺陷**。

### 7.6 P1 计算层落地记录（2026-09-30）

**产出**：`market_state_daily.py`（计算层主模块，读原始表 → 写 `regime` 三张表）。

| 维度 | 指标（22 个，CN/HK 双市场按可用性分别落库） |
|---|---|
| 宽度（8） | 上涨家数占比、麦克莱林振荡、涨跌家数净差、60 日新高−新低占比、站上 MA60/MA200 占比、等权−市值加权 20 日收益差、涨跌停净差（A 股按板块阈值） |
| 估值（4） | PE-TTM 中位数、PB 中位数、破净股占比、**ERP**（CN 用中债国债 10Y / HK 用美债 10Y） |
| 资金（5） | 市场换手率（成交额/流通市值）、两融占流通市值比、南向净流入、北向净流入（断点截断）、港股卖空成交占比 |
| 风险（5） | RV20 / RV60（年化）、近 250 日最大回撤、单日跌幅超 3% 家数占比、下跌家数占比 20 日均值（系统性同步代理） |
| 合成（3+2） | 温度计 / 风险指数 / FSI（等权，成员与符号显式声明）；季节（v1 规则版）；顶部 / 底部信号命中计数（各 6 条可算子集，明细存 `detail`） |

**实测结果（全部入库并核对）**

| 项 | 结果 |
|---|---|
| `regime.indicator_value` | CN **121,252 行**（1990-12-20 ~ 2026-09-30，8,736 个交易日）、HK **30,359 行**（2019-01-02 ~ 2026-09-30） |
| `regime.market_regime_daily` | CN 8,737 行、HK 1,906 行（每日一行，含季节/温度计/风险/FSI/顶底信号计数） |
| 指标字典 | 22 条（`regime.indicator_dict`，字段即口径，代码即字典） |
| 覆盖分段（预期内） | 宽度/风险类 **1990–1991 起**；估值/换手/ERP **2018-01 起**（个股估值字段起点）；两融占比 **2019-01 起**；北向净值 **2014-11 ~ 2024-08-16**（断点） |
| 性能 | 增量（45 天窗口）**≈25s**；全历史回填 **CN 279s / HK 26s**（4 年一块，可中断续跑） |

**牛熊时点对照（抽样，用于 M1「与已知牛熊区间吻合」验收）**

| 时点 | 季节 | 温度计 | 风险 | FSI | 顶/底信号 |
|---|---|---|---|---|---|
| 2015-06-12（5178 顶） | 夏 | 80.4 | 73.9 | 53.8 | 0 / 0 |
| 2016-01-28（熔断底） | **冬** | 25.8 | 59.4 | **90.3** | 1 / 0 |
| 2018-12-28（熊底） | **冬** | 25.9 | 22.4 | 64.5 | 0 / **4** |
| 2020-03-23（疫情底） | 夏 | 48.8 | 47.2 | 68.3 | 1 / 2 |
| 2021-02-18（核心资产顶） | 夏 | 67.6 | 60.0 | 39.0 | 0 / 0 |
| 2024-02-05（微盘踩踏底） | **冬** | 30.8 | 22.8 | 62.4 | 0 / **3** |
| 2026-09-30（最新） | 秋 | 38.3 | 74.6 | 74.1 | 1 / 0 |

**已知口径限制（必须在消费端标注，勿当缺陷）**

1. **2018 年前缺估值/资金维度**：估值类顶底信号（PE 分位、ERP、换手、两融）在 2018 前不可算 → 该区间顶底信号计数**系统性偏少**；季节判定在 2018 前实际只用宽度维度（故 2007-10 大顶被判「夏」而非「秋」）。
2. **FSI 信用维度缺失**（信用利差未接入）→ 现为「波动 + 流动性 + 尾部 + 同步性」四维，待宏观月频包接入后补齐。
3. **相关性用代理**：真实两两相关矩阵成本过高，v1 用「下跌家数占比 20 日均值」代理系统性同步。
4. **港股宽度设了成交额门槛**（10 万港元）以剔除仙股；门槛变更需整段回填。
5. 分位是**原始值分位**（非方向对齐），方向由 `indicator_dict.direction` 声明；合成成员表在 `market_state_daily.COMPOSITES` 显式声明符号。

**本次修掉的数据质量问题（阻塞计算，计划外）**

| 项 | 问题 | 处置 |
|---|---|---|
| `a_daily_quote` numeric **NaN 污染** | 场内基金 `SH.520900` 的估值字段被写成 PostgreSQL numeric `NaN`（42 行）→ `SUM()`/`AVG()` 按日聚合整体传播为 NaN（`count()` 仍算非空，极隐蔽）。**根因在 pandas NaN 直写** | ① 40 行数据修复：`SET col=NULL WHERE col='NaN'`（42 行）；② 写入层根治：`db._adapt_value` 统一把 NaN/NaT → NULL（此后所有采集脚本免疫）；③ 计算层对可能残留的 NaN 加 `NULLIF(col,'NaN'::numeric)` 防御 |
| B 股未排除 | 排除条件写 `left(stock_code,4) <> ALL(['SH.90','SZ.20'])`——4 字符与 5 字符前缀永不相等 → B 股一直在宽度口径内 | 统一改 `left(stock_code,5)`（CN 两处查询） |
| 窗口计算性能 | PG 的 `max()/min()` 移动窗口无逆函数 → 1.4M 行 ×60 帧 ≈ 33s | 改为「SQL 取原始列 + pandas rolling（C 实现）」≈2s，整体增量 59s → 25s |

**收尾**：`indicator_dict` / `indicator_value` / `market_regime_daily` 监控已置 `active=TRUE`；调度器新增「市场状态计算」（工作日 18:45，等 A 股 16:10、港股 16:30、沽空 18:30 终值）并重启生效（**共 108 个任务**）。

**P1 剩余（未做）**：~~宏观月频包~~、~~HIBOR/商品落库~~ 已于 §7.7 完成；余下：A 股情绪事件包（IPO/基金发行/解禁/增减持）、行业资金流换源、分析师一致预期快照、北向/南向/卖空的替代源定位。

### 7.7 P1 宏观包落地记录（2026-09-30）

**产出**：`get_macro_monthly.py`（月/季频）+ `get_macro_daily.py`（日频 + 利差派生）

| 组 | 序列 |
|---|---|
| 货币信用 | 社融增量、社融-人民币贷款、新增人民币贷款、M1/M2 同比、**M1−M2 剪刀差(派生)**、LPR 1Y/5Y |
| 通胀价格 | CPI 同比、PPI 同比、**PPI−CPI 剪刀差(派生)** |
| 经济周期 | 制造业/非制造业 PMI、工业增加值同比、GDP / GDP 同比 |
| 市场与实体 | 沪深市价总值（沪/深，巴菲特指标分子）、全社会用电量同比、消费者信心指数 |
| 流动性 | HIBOR 隔夜/1W/1M/3M + Hibor人民币(CNH)、Shibor 隔夜/1W/1M/3M |
| 商品 | 沪铜 / 沪金 / 原油（连续合约收盘价） |
| **利差派生** | 信用利差（中票 AAA 10Y/5Y − 国债）、银行债利差、**期限利差（国债 10Y−1Y）** |

**实测**：月频 3,274 行（20 序列；LPR 为日频重复报价，去重掉 1,598 行）+ 派生 448 行；日频 78,275 行（HK 28,425 / CN 37,929 / CMDTY 11,921）。

**计算层接入（4 个新指标 + 合成升级）**

| 指标 | 市场 | 覆盖 | 去向 |
|---|---|---|---|
| `MACRO.CREDIT_IMPULSE`（信贷脉冲代理） | CN+HK | 2017-01 起（受社融源 136 个月限制） | 温度计(+)、FSI(−)、**季节规则 v2** |
| `RISK.CREDIT_SPREAD` | CN | 2011-01 起 | 风险指数(+)、FSI(+) ← **补齐计划 §5.6 的信用维度** |
| `RISK.TERM_SPREAD` | CN | 2006-03 起 | 风险指数(−)、FSI(−) |
| `FLOW.HIBOR_3M` | HK | 2019 全（源 1996 起） | 温度计(−)、FSI(+) ← 港币流动性维度 |

- **季节规则 v2**：加入信用脉冲——「春」要求信用脉冲为正，「秋」可由信用脉冲见顶回落触发；`season_score` 变为（便宜度、宽度、信用脉冲）三分位均值。
- **PIT 实现**：macro_series → 交易日用 `merge_asof` 取「release_time ≤ 当日 23:59」的最近观测（月频数据次年 10 日才发布，直接按 period_date 对齐即为前视）。
- **合成变化对照**：CN 2018-12-28 风险 22.35 → 26.62、FSI 64.51 → 61.90（并入信用与期限利差后更全面）；HK 2021-02-18 顶部信号仍命中 4 个（结论稳定）。

**已知限制**：① 社融源仅 136 个月（2015+）→ 信贷脉冲代理仅覆盖 2017 起；② 真值口径（社融存量/GDP 二阶导）需外采；③ `CN.ACCOUNT_NEW`（新增开户数）源已停更于 2023-08。

**调度（顺序有依赖）**：`宏观月频采集` 18:20 → `宏观日频采集` 18:30 → `市场状态计算` 18:45（计算层按 PIT 取当日已发布值，宏观必须排在计算之前）；重启后共 **110 个任务**。

**P1 剩余（未做）**：A 股情绪事件包（IPO/基金发行/解禁/增减持，含 4 张新表）、行业资金流换源、~~分析师一致预期快照~~（已于 §7.8 完成）、北向/南向/卖空替代源、退市股行情试补。

### 7.8 P1 分析师一致预期快照落地记录（2026-09-30）

**产出**：`regime.analyst_forecast_snapshot` 表 + `get_analyst_forecast.py`（逐日快照采集）。

| 项 | 实测结果 |
|---|---|
| 源 | AKShare `stock_profit_forecast_em`（东财盈利预测），列名即「研报数 + 5 档评级计数 + 4 个财年 EPS」 |
| 首次快照 | 2026-09-30：**2,438 只**（SH 1,208 / SZ 1,102 / **BJ 128**），全部有 FY1 EPS 与评级分布；FY 列 = 2025/2026/2027/2028 |
| 表形态 | **宽表（一股一日一行）** 而非草案的长表：源本身是宽表，长表会把行数放大约 4 倍（2.9M → 0.73M 行/年）；财年标签随值存 `fyN_year` |
| 调度 | `一致预期快照` **18:40**（早于 18:45 计算层，当日可用）；重启后共 **111 个任务** |

**采集层实测到的两个源侧问题（已修）**

1. **源侧重复返回**：`stock_profit_forecast_em` 对 492 个代码**完全重复返回**（2930 行 → 唯一代码 2438）。
   不显式去重虽会被 `ON CONFLICT` 静默合并，但**行数对不上会掩盖源侧异常** → 采集层显式去重并打印警告。
2. **北交所代码前缀**：源含 `92xxxx`（161 行）等北交所代码，最初按「6/9→SH，其余→SZ」二分导致**误标为 SZ.92xxxx**；
   已改为按代码段映射（92/83/87/88/43→BJ，60/68/90→SH，00/30→SZ），并清理当日脏行重跑。

**计算层接入（2 个新指标）**

| 指标 | 口径 | 状态 |
|---|---|---|
| `FORECAST.REVISION_BREADTH` | (Σ30 快照上调家数 − 下调家数) / Σ30 可比样本 × 100；同日只比 FY1 且财年标签须一致，±0.5% 内视为噪声 | **按设计需 ≥2 个快照日** → 次日（10-01 起）开始有值；已入温度计(+) |
| `FORECAST.RATING_BULL_PCT` | (买入+增持) / 五档合计 × 100 | 已入库 2026-09-30 = **99.3%**；⚠ 判别力弱（卖方评级结构性集中在买入/增持），已在指标字典 `remark` 标注「仅作极端背离提示，勿用于常规分位择时」；已入温度计(+) |

**已知限制**：① 免费源无历史序列 → **快照从上线日起累积，无法回填**（漏采一天即永久断档，故监控 `expect_lag=1`）；② 无净利润/营收/目标价字段，与草案字段存在差异（已按真实可得字段建表）；③ 覆盖仅 A 股（港股无对应免费源）。

**P1 剩余（未做）**：A 股情绪事件包（IPO/基金发行/解禁/增减持，含 4 张新表）、行业资金流换源、北向/南向/卖空替代源、退市股行情试补。

### 7.9 P1 A 股情绪事件包落地记录（2026-10-04）

**产出**：4 张表 + 1 个采集脚本（4 个子采集）+ 7 个指标 + 2 个合成成员。

| 子采集 | 源（AKShare） | 表 | 首轮入库 | 覆盖 |
|---|---|---|---|---|
| ipo | `stock_xgsglb_em(symbol='全部股票')` | `regime.ipo_event` | 4,033 行 | 申购 2010-01 起；上市 4,027 行 + 待上市排期 |
| fund | `fund_new_found_em()` | `regime.fund_issuance_event` | 6,968 行（份额覆盖 96–100%） | 成立 2013-08 起 |
| unlock | `stock_restricted_release_detail_em(start,end)` | `regime.unlock_schedule` | 23,443 行（含**回填 2016 起** 22,648 行） | 2016-01 ~ 2027-04（未来 180 天排期） |
| insider | `stock_hold_management_detail_cninfo('增持'/'减持')` | `regime.insider_trade` | 29,976 行 | 源滚动近 1 年（2025-10-09 起） |

**草案 → 实现的关键偏离（均已写进 DDL 注释）**

| 表 | 草案 | 实现 | 原因（实测） |
|---|---|---|---|
| ipo_event | PK `(stock_code, list_date)`；含 raised_amount/first_day_open | PK `stock_code`；list_date **可空**；改存 subscribe_date/board/issue_shares(万股)/lottery_rate/limit_up_days | 源含「仅申购排期、未上市」的行（上市日期为空）→ 需承载前瞻排期；`skip_null_updates` 保证后续补齐不回退 |
| fund_issuance_event | 含 issue_amount（亿元） | 去掉 issue_amount；PK `fund_code` | 源只有「募集份额（亿份）」，无募集金额 |
| unlock_schedule | 用 `*_queue_em`（逐股） | 改用 `*_detail_em`(**区间**拉取)；新增 float_ratio/pre_close/actual_shares | 逐股循环 5 千只不现实；区间接口实测可一次拉 2016–2026（24,893 行）→ 顺带完成历史回填 |
| insider_trade | 用东财 `*_detail_em` | 改用**巨潮** `*_detail_cninfo`（增持/减持各一次） | 东财 datacenter 实测 ReadTimeout；cninfo 稳定且含事件级成交均价 |

**采集层实测到的 3 个数据质量问题**

1. **源侧重复行**：insider 有 39 行关键字段完全相同 → 同一批 INSERT 内重复冲突键会被 PG 拒绝
   （`cannot affect row a second time`）；跨批重复则被**静默合并**、行数对不上。
   → `_upsert` 统一按业务唯一键**显式去重并打印**（四类事件共用）。
2. **北交所量纲失真**：BJ 的「变动数量」出现 79 亿股级异常值 —— 1,168 条事件级记录（占事件数 8%）
   却占全市场金额 **65%**（¥1,149 亿），会直接污染「净减持」指标；沪深两市内部自洽
   （抽检：伊利亚事长 6,199 万股 × 26.51 = ¥16.4 亿 ✓）。
   → **BJ 一律不采**（源侧不可靠 + 不在本项目 A 股池），已清理已入库的 2,048 行。
3. **基金份额滞后**：新成立基金当日份额多为空（1–2 日后到账）→ 采集用 `skip_null_updates` 保护；
   实测月度披露覆盖 96–100%，**无需为指标引入滞后窗口**（当前 60 日份额 207 亿份 = 真实的发行降温，非数据缺失）。

**PIT（防前视）设计决策**

- **未来解禁用「股数」口径**：解禁市值需用解禁前收盘价计算，解禁前不可知 → 未来压力用 `Σ解禁股数`
  （排期公告先行，PIT 安全）；市值口径只做**已实现**（过去 30 日）→ 两个指标分工明确（§5 指标备注已写明）。
- IPO/基金用 list_date / setup_date（均已确定才入库）；董监高用 ann_date（公告日）。
- 窗口聚合 `_win_agg` 为「左开右闭 + 前瞻窗口」两套语义，配 `_mask_before` 处理源覆盖前区间
  （否则「源无数据」会被读成 0，对家数/金额口径尤其致命）→ 新增 `tests/test_event_indicators.py`（12 项，含 PIT 专项）。

**计算层（7 个指标 + 2 个合成成员）**

| 指标 | 2026-09-30 值 | 分位 | 说明 |
|---|---|---|---|
| `SENT.IPO_COUNT_30D` | 21 家 | 58.6 | 入温度计(+) |
| `SENT.IPO_BREAK_RATE_90D` | 0% | 29.0 | 90 日窗口（30 日窗口常年为 0，无判别力） |
| `SENT.FUND_ISSUANCE_60D` | 207 亿份 | **2.6** | 权益类（剔除债/固收/稳健/货币）；入温度计(+) |
| `SENT.FUND_MAX_60D` | 7.0 亿份 | 1.9 | 爆款缺失，与发行冰点互证 |
| `SENT.UNLOCK_NEXT_30D_SHARES` | 125 亿股 | 25.0 | 入风险指数(+) |
| `SENT.UNLOCK_PAST_30D_VALUE` | 2,084 亿元 | 39.2 | 已实现口径 |
| `SENT.INSIDER_NET_SELL_60D` | 52.3 亿元 | 13.1 | 入风险指数(+)；仅事件级 |

合成成员变更后**已做 CN 全历史重算**（`--market CN --backfill`，5 分钟）：indicator_value 132,443 → 147,533 行，
指标字典 28 → 35。注意：合成成员随数据可得性变化（早期年份成员少、噪声大）→ **看分位趋势，不看绝对水平**。

**调度与监控**：新增任务「市场事件采集」19:30（mon-fri，重启后共 112 个任务）；4 张表登记 `monitor_table_config`
（`updated_at` + day/1 —— 事件业务日期天然稀疏，**必须用 updated_at 判「采集器是否还活着」**，否则数日无新事件即误报）。

**已知限制**：① 增减持源仅滚动近 1 年，更早历史只能逐日累积；② `change_ratio` 源口径未校准（勿直接当百分比用）；
③ 港股无对应的免费事件源（IPO/基金/解禁/增减持均仅 A 股）；④ 北交所整体不覆盖。

---

**当前 P1 剩余（唯一权威清单）**：北向/南向/卖空替代源定位、退市股行情试补。
（行业层已于 §7.11 完成；**P1 采集+计算主体至此收口**。）

### 7.10 监控日历基准修复 + 监控服务常驻（2026-10-04）

**问题（实测 2026-10-02）**：A 股国庆休市、港股开市，监控一律用**港股日历**取「最近交易日」，
于是 A 股专属表被判落后（告警原文：`regime.analyst_forecast_snapshot 最新交易日 2026-09-30，
已落后 2 天`）——该类表在 10-01/10-02 本就不该有数据。

**根因有两层**（第二层是查证时才发现的）：

1. `_last_trading_date()` / `is_intraday_active()` 硬编码 `market="HK"`，且 `_check_data_stale`
   对全表只算一个 `today`；
2. 更底层：`_refresh_trading_days()` 只从富途取 **HK/US**，A 股日历**根本不存在** ——
   `is_trading_day(d, "CN")` 会静默回落港股集合，因此必须先补 A 股日历源。

**改动**

| # | 内容 |
|---|---|
| 1 | `monitor_table_config` 新增 `market VARCHAR(8) DEFAULT 'HK'`（含存量库 `ADD COLUMN IF NOT EXISTS`） |
| 2 | 新增 `_CN_MONITOR_TABLES`（**10 张 A 股专属表**，按表内实际数据分类）+ `_migrate_market_config()` 幂等迁移 |
| 3 | A 股日历改读**本项目 `public.trading_calendar`**（`_load_cn_trading_days`，由 `init_trading_calendar.py` 每日 08:45 维护），读不到才回落港股并打告警 |
| 4 | `_last_trading_date(ref, market)` / `is_intraday_active(market)` / `_compute_window_start` 全部按表的市场取基准（`(period, lag, wd, market)` 缓存） |
| 5 | 新增 `tests/test_monitor_market_calendar.py`（7 项 + 18 子项）：CN 通过/HK 告警对照、默认参数兼容、配置清单完整性 |

**A 股专属表清单（依据表内数据，非表名猜测）**：`daily_margin_balance`（仅 SH./SZ.）、
`daily_trend`（仅 SH.）、`realtime_order_size`（仅 SH.）、`daily_northbound_flow`、`macro_environment_score`，
以及 5 张 regime 表（一致预期/IPO/基金/解禁/增减持）。
**保持 HK**：混合/全球表（`daily_quote`/`daily_benchmark`/`benchmark_minute`/`indicator_value`/
`market_regime_daily`/`stock_sector`/`trading_calendar`）——用更宽的港股日历才不漏报；
实测为港股的（`daily_buyback_event`/`daily_cbbc`/`daily_short_selling`/`daily_ggt_hold`/
`trend_snapshot`/`tick_data`/`hk_daily_quote`/`daily_market_turnover`）已在测试中锁死不得误划为 CN。

**同时发现并修复的运维问题**：`monitor_collector.py` 设计了常驻模式（60s 巡检），但
**既无 launchd 服务也无进程在跑**（告警的 `last_seen` 只在我们手动巡检时前进）——
即此前所有停滞判断实际无人执行。已补 `~/Library/LaunchAgents/com.equity.a-monitor.plist`
（对齐 a-scheduler 结构，日志 `monitor.log`），`launchctl bootstrap` 后 PID 常驻，
启动日志确认 `交易日状态刷新成功: HK=48天, CN=44天`。

**验证（端到端）**：修复后重跑巡检，A 股误报全部解除（一致预期 / 两融 / 趋势跟踪），
同时**港股真告警保留**（港交所 10-01/02 开市而采集器停摆，属真实缺口）。全量测试 162 项通过。

**残余（已知，未改）**：① 港股通类表（`daily_ggt_hold` 等）真实交易日是「沪深港**共同开市**」的
交集，单一市场日历无法表达，暂按 HK（各自独家假日仍会有偏差）；② `day` 口径用**日历日**减法
（`today − lag 天`）而非交易日减法，故长假后首日、采集器尚未运行时仍会短暂告警（随后自动 resolve）。

**顺带发现的两张真停滞表（待决策，非本方案范围）**：`public.macro_environment_score`
（脚本 `macro_environment_score.py` 在 `market_scheduler.py:155` **已注释停用**，表自 2026-09-23 停更，
但监控仍按 day/0 → 长期挂红）；`public.daily_northbound_flow`（0 行启用计划未完成，数据止于 2024-08-16）。
处置建议：恢复调度，或把该表监控改 `active=false` 并注明原因。

---

### 7.11 P1 行业层落地记录（2026-10-04）

**产出**：申万一级行业指数补增量调度 + 1 张新表（`regime.sector_fund_flow`）+ 1 个采集脚本 + 6 个行业指标。

| 项 | 状态 |
|---|---|
| 申万一级指数 | **P0-2 已回填**（31 个 `CN.SW*`，1999-12-30 起 14.4 万行），但**未挂调度**（一次性脚本）且缺 09-30 → 本轮补 **18:10 增量任务** |
| 增量实现 | 源 `index_hist_sw` **无日期参数**（只能整段拉回）→ 全量请求 + **只 upsert 库内最新日−3 天之后的尾部**（20 万行写入压到约 30 行，保留迟到/修订自愈） |
| 行业资金流 | 东财 `stock_sector_fund_flow_rank` **两轮探测均 ConnectionError** → 换**同花顺** `stock_fund_flow_industry(symbol='即时')`（90 个细分行业，首次入库 2026-09-30） |
| 新表 | `regime.sector_fund_flow`（PK `(trade_date, sector_name)`；已同步 `schema.sql` §10、登记监控为 **CN 日历**） |
| 调度 | 新增「行业资金流」**15:35**（A 股收盘后）+「申万行业指数」**18:10**；重启后共 **114 个任务** |

**关键设计决策（均有实测依据）**

1. **行业资金流必须收盘后采集**：源只给「当前快照」（即时/3日/5日/10日/20日**排行**），无历史序列 →
   盘中取到的即时值是**不完整的当日累计**；且漏采一天即永久缺失（故监控 day/1，同一致预期快照）。
2. **归属交易日取「最近 A 股交易日」**（读 `public.trading_calendar`）：节假日重跑只会幂等覆盖同一交易日，
   不会把快照记成非交易日（实测 10-04 写入 trade_date=09-30 ✓）。
3. **相对基准用「行业等权均值」而非沪深300**：库内 `daily_benchmark` **没有沪深300**（只有上证/深证成指），
   且行业轮动分析的标准做法就是比等权均值 → `SECTOR.RS_DISPERSION` = 各行业 20 日收益 − 行业等权均值的横截面标准差。
4. **行业层不入合成**（温度计/风险指数成员不变）：行业层按方案定位是第④层独立分析，
   且其历史起点不一（申万 1999 / 资金流 2026-09）→ 混入市场级合成会污染可比性。
5. 申万指数源侧成交量/成交额单位与个股口径不同（亿量级）→ 本层**只用价格类字段**。

**顺带修掉的 bug（重要）**：行业指标最初返回 **Timestamp 索引**，与全模块「date 索引」约定不一致，
`_clip` 里 `date` 与 `Timestamp` 直接比较抛 `TypeError` → **CN 全历史重算的 10 个分块全部静默失败**
（只 tail 汇总时看不到错误）。已修：统一转 date 索引 + 给 `_clip` 加索引归一化防御 +
新增 `tests/test_sector_indicators.py`（7 项，含该回归与输出契约）。

**计算层（6 个指标，2026-09-30 实测）**

| 指标 | 值 | 分位 | 备注 |
|---|---|---|---|
| `SECTOR.UP_RATIO`（行业上涨占比） | 67.74% | 57.8 | 1999-12 起，6,474 行 |
| `SECTOR.MOM_20D_MEDIAN`（行业20日动量中位数） | −4.51% | 22.8 | 6,454 行 |
| `SECTOR.RS_DISPERSION`（轮动强度） | 3.41 | 40.2 | 方向 neutral（高=分化剧烈） |
| `SECTOR.FLOW_NET`（行业资金净流入） | −194.73 亿元 | — | 源无历史 → 逐日累积（分位待满 5 日后有意义） |
| `SECTOR.FLOW_UP_RATIO`（净流入行业占比） | 43.33% | — | 同上 |
| `SECTOR.FLOW_NET_5D`（近5日净流入） | 空 | — | `min_periods=5`：不足 5 日不给值（防部分窗口充完整窗口） |

CN 全量重算后 indicator_value 147,533 → **166,901 行**（+19,368 = 3 个申万类指标的历史 + 资金流 2 行）。

**已知限制**：① 同花顺行业分类（≈90 个细分）**≠ 申万一级（31 个）**，跨表 join 需先建映射表（P2）；
② 行业资金流**无历史**，分位/滚动口径需逐日累积（与一致预期快照同类约束）；
③ 逐行业横截面（RS/RRG/估值分位）尚未落表 —— 现仅落市场级聚合，逐行业需新表 `sector_daily`（P2）。

### 7.12 交付自检口径（新增易错项，纳入验收）

| # | 检查项 | 为什么 |
|---|---|---|
| 1 | 长任务输出**不得只 tail 汇总**：`grep -E "❌|失败|Traceback"` | 本轮「10 个分块全失败但汇总仍显示旧行数」正是被 tail 掩盖 |
| 2 | 新指标的**索引类型**必须是 `datetime.date`（不是 Timestamp） | `_clip` 的日期比较依赖该约定 |
| 3 | 常驻服务三查：plist 存在 / `launchctl list` 有 PID / 日志有周期输出 | 见 §7.10（监控曾"设计了但没跑"） |
| 4 | 调度器**在进程内**调 `run(codes, ctx=...)`，**无法传命令行参数** | 日常默认值必须写成调度期望的行为（如申万增量为默认） |
| 5 | 新表/新列必须同步 `sql/schema.sql`（含 `monitor_table_config` 等监控元数据表） | 本轮 `market` 列漏同步被 `_schema_diff` 抓出 |

---

### 7.13 P2 逐行业层落地记录（2026-10-04）

**产出**：`regime.sector_mapping`（90 行映射，覆盖**全部 31 个申万一级**）+ `regime.sector_daily`（**15.5 万行**，
1999-12-30 起 31 行业 × 交易日）+ 月度复核任务 + 5 项不变量测试。

**映射方法的三轮演进（本案最有价值的部分）**

| 方案 | 结果 | 判定 |
|---|---|---|
| ① 原始收益相关性取最高 | ❌ **市场因子主导**（所有行业 raw corr 0.6~0.95）：物流→纺织服饰、白色家电→轻工制造、综合→纺织服饰 | 弃用 |
| ② 超额收益相关性（减 31 行业等权均值） | 明显改善（钢铁 0.928、半导体 0.936、白酒→食品饮料），但仍错：橡胶制品→轻工制造、风电设备→环保、多元金融→商贸零售（这批 corr 均 <0.55） | 仅作审计 |
| ③ **规则为主 + ②交叉校验**（最终） | 90 个同花顺行业的**公认对应**写成显式 `RULES`（逐条可审计，`confirmed=true`）；相关性只用于提示 `corr_suggests` / `low_corr_with_rule` | ✅ 采用 |

两条实测证据说明「相关性不能当映射依据」：
- **风格因子冒充行业因子**：剔除市场因子后残余相关主要是成长/价值风格 —— 实测「塑料制品 / 消费电子 /
  金属新材料」都被高相关地拉向**机械设备**（成长风格共振）；
- **两套指数编制方法不同**：THS 白色家电与申万家用电器一年累计收益差 **46 个百分点**（+40.4% vs −6.1%）、
  超额相关性为负；但同日原始相关性最高（0.64，错位 1 日后仅 0.02）→ **排除数据错位，纯属编制差异**。

**`sector_daily` 设计要点**

- `rs_20d` = 行业 20 日收益 − 31 行业等权均值 → **横截面均值恒为 0**：只看相对排序，避免把「全市场普涨」误读成行业强势；
- 资金流汇总**准入**：只纳入 `confirmed=true` 或 `corr ≥ 0.6` 的映射（相关性已被证明会被风格因子误导，
  未确认的低置信映射宁可不汇总）；`mapped_ths_count` 显示该行业实际由几个同花顺行业汇总而来；
- `flow_rank` 与 `rs_rank` 并列 → 「**强且资金流入**」的行业可直接筛出（本轮目标）。

**实测（2026-09-30）**：RS 前三 = 房地产(+6.4%/20日) / 医药生物 / 银行；资金流前三 = 医药生物 **84.4 亿**
（由 6 个同花顺行业汇总）/ 银行 41.1 亿 / 房地产 25.1 亿；**31 行业资金流合计 = 市场级 `SECTOR.FLOW_NET`（−194.73 亿）** ✓ 守恒。

**数据事实（写入文档以免误读）**：申万 31 个一级行业在源侧有**三个分批起始**——
1999-12-30（21 个）/ **2014-02-21**（12 个：计算机/传媒/通信/银行/非银/汽车/机械/电力设备/军工/建材/建筑装饰/煤炭）/
**2021-12-13**（3 个：石油石化/环保/美容护理）→ 早期横截面样本少，`rs_rank` 的分母随时间变化。

**调度与监控**：新增「行业映射重算」**每月 1 日 08:20**（重启后共 **115 个任务**）；3 张表登记监控
（`sector_daily` day/1、`sector_fund_flow` day/1、`sector_mapping` lowfreq/45），且全部按 **CN 日历**判定。

**测试**（`tests/test_sector_indicators.py` 共 12 项，新增 5 项不变量）：映射目标必须是真实申万代码、
资金流行业必须**全有映射**、31 个申万行业必须**全覆盖**、每日 `rs_20d` 横截面均值 ≈ 0 且 `rs_rank` 唯一覆盖 1..N、
**资金流守恒**（按映射汇总合计 = 市场级指标）。

**已知限制**：① 行业估值（PE/PB/股息率）源 `sw_index_first_info` **只有当前快照**、无历史 →
需逐日快照累积（P2 待办，与一致预期同类约束）；② 同花顺资金流无历史；③ 映射需随指数公司分类调整而复核（已挂月度任务）。

---

### 7.14 P2 行业估值快照落地记录（2026-10-04）

**产出**：`regime.sector_valuation_snapshot`（31 行业 × 快照日）+ `get_sector_valuation.py`（18:15 采集）
+ 2 个市场级指标（`SECTOR.PE_TTM_MEDIAN` / `SECTOR.PB_MEDIAN`）+ 3 项契约测试。

**为什么必须现在就上**：源 `sw_index_first_info`（申万宏源官方口径）**只提供当前横截面**，
无任何历史序列 → 行业估值分位只能逐日累积，**越早开始越值钱**（与一致预期快照、行业资金流同类约束；
断档即永久缺失，故监控 `day/1`）。

**首日实测（2026-09-30）**：PE_TTM 最低 = 银行 7.4 / 非银金融 7.6 / 房地产 9.1；
最高 = 电子 49.7 / 国防军工 50.4 / 综合 66.3 —— 与 A 股行业估值结构常识吻合 ✓；
31 行业中位数 PE_TTM = **21.60**、PB = **1.99**（已入 `indicator_value`；因仅 1 个快照日，分位暂为 50，
已在指标 `remark` 标注「早期分位无意义」）。

**分层设计**：采集层只落**原始快照**（不掺计算）；市场级中位数由计算层算并进 `indicator_value`；
逐行业估值明细留在快照表供 join（`sector_daily` 不掺估值列 —— 避免计算层 upsert 把采集值覆盖成 NULL）。

**测试（3 项）**：`sw_code` 必须是真实申万代码；取值域合理（PE ∈ (0,300]、PB ∈ (0,100]、
股息率 ∈ [0,20] —— 挡源侧单位/口径漂移）；**市场级指标 = 快照中位数**（防「改了 SQL 忘改指标口径」）。

**调度与监控**：新增「行业估值快照」**18:15**（早于 18:45 计算层；重启后共 **116 个任务**）；
表登记监控并按 **CN 日历**判定（CN 专属表累计 14 张）。

---

### 7.15 §3 宏观层补齐落地记录（2026-10-08）

**背景**：按 `market_profile.md` §3（第一层：全球宏观）逐项核对采集实况后，发现 3 个可立即修复的问题。
本轮修掉其中 2 个 + 新增 2 条派生序列。

| # | 问题（实测） | 处置 |
|---|---|---|
| 1 | **中美利差/美债利差未派生**（数据早已在库：`CN.BOND_10Y` 2006+ 与 `US.DGS10/DGS2` 1990+） | 新增**跨表派生**能力（`macro_series` ↔ `daily_benchmark`，同日做差、不做 ffill）：`CN.US_SPREAD_10Y` 5,008 行、`US.TERM_SPREAD_10Y_2Y` 8,737 行；并在计算层新增指标 `MACRO.CN_US_SPREAD` / `MACRO.US_TERM_SPREAD`（**CN/HK 双侧**接入，港股受外部变量驱动更强） |
| 2 | **用电量被"压成年度"**（源 `统计时间="2026.8"` 点分隔不被解析 → 每月折叠到「年初」，库里只剩 24 行形似年度数据） | `_parse_period` 支持点分隔 + **回归测试**；重采后为**月度 240 行（2003-12 ~ 2026-08）**，并清理旧的 24 行错误数据 |
| 3 | 货运量未采集 | 新增 `CN.FREIGHT_YOY`（`macro_china_society_traffic_volume`，显式取「统计对象=合计」，**291 行 / 2000-01 起 / 至 2026-08**）；顺带给 `_collect_one` 加**行过滤**能力（多口径接口必需，否则会被最后一行覆盖成单一子口径） |

**两点更正（上一轮盘点中的误判，据此修正）**

- **GDP 并未停更**：`CN.GDP` 最新 `period_date=2026-04-01` 是**「2026 年第1-2季度」（H1 累计）**的标记值——
  该采集器的季度口径为「**累计区间的末季度起始月**」（Q1→01-01、H1→04-01、9M→07-01、FY→10-01），
  `value` 同为**年内累计值**（非单季）。已在序列 `remark` 与序列定义注释中写死该口径，避免再次误读。
- **社融确为源侧停更**（非采集 bug）：直接验证 `macro_china_shrzgm` 返回的 136 行尾行 = `202604`，
  其上游是**商务部镜像**（`data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery`）；
  已枚举 akshare 全部 85 个 `macro_china_*` 接口确认**无新鲜替代** → 序列 `remark` 标注停更 +
  提供替代口径（`CN.NEW_LOAN` 更新至 2026-08）+ 换源（央行直连）列 P1。

**实测读数（2026-10-08）**：`MACRO.CN_US_SPREAD` = **−3.593pp**，历史分位 **0.03**（即**接近历史最阔水平**，
人民币与外资配置的外部压力读数）；`MACRO.US_TERM_SPREAD` = +0.510pp（未倒挂，分位 37.6）。
FSI 纳入 `MACRO.US_TERM_SPREAD`（sign −1：倒挂→压力↑）→ 双市场全量重算
（CN indicator_value 166,901 → **180,676** 行，HK 34,172 → **38,046** 行，最新到 2026-10-08）。

**测试**：新增 `tests/test_macro_monthly_parse.py`（9 项）：点分隔/中文月份/紧凑格式/季度累计四档、
非法输入、用电量必须月度（行数/月数比自检）、货运量覆盖、跨表派生存在性、**中美利差 = 中债10Y − 美债10Y 同日校验**。
全量 **205 项通过**。

**未做（P1 尾巴）**：MLF/OMO、政策事件类（§3.5）。（**社融换源已完成**，见 §7.24；猪价菜价、AA 级信用利差见 §7.16，核心 CPI 见 §7.17，均已落地）

---

### 7.16 §3 通胀与信用维度补齐（2026-10-08）

**产出**：AA 档信用利差（含**等级利差**）+ 通胀高频（菜篮子、猪价）+ CPI 环比 + 4 个指标（FSI 纳入等级利差）。

| 项 | 源与实测 | 覆盖 |
|---|---|---|
| **AA 中短票曲线** | ⚠ **必须走独立接口** `bond_china_close_return(symbol='中短期票据(AA)')`——`bond_china_yield` 默认只给国债 + 中票AAA + 商业银行AAA，**无 AA** | 源**只保留最近 3 个交易日**、且无数据窗口直接报错（长假后 `KeyError: newDateValue`）→ 已加**逐步放宽回看**（7/14/30/45/60 天）重试；**不可回填**，逐日累积 |
| 派生序列 | `CN.CREDIT_SPREAD_MTN_AA_5Y`（AA 中票 5Y − 国债 5Y）、`CN.GRADE_SPREAD_AA_AAA_5Y`（**AA − AAA 等级利差**） | — |
| **菜篮子批发价** | `macro_china_vegetable_basket` ✅ **2005-09-27 起完整日频**（5,822 行，最新 **2026-10-08**） | CPI 食品项的日频先行验证 |
| **生猪价格** | `spot_hog_year_trend_soozhu`（瘦肉型）✅ 约 **7.5 个月滚动窗口**（200 行，2026-03-23 起） | 逐日累积；源**未标注单位**（量级=元/公斤，已在 `extra.remark` 注明） |
| CPI 环比 | `CN.CPI_MOM`（同 `macro_china_cpi` 的「全国-环比增长」列） | 2008-01 起 224 行 |

**核心 CPI：原结论「免费源不可得」经复核为**错**，2026-10-08 已落地（详见 §7.17）**
- 原判断只试了 akshare `macro_china_nbs_nation`（走**老接口** `easyquery.htm`，实测 403 URLACL，
  页面内同源 fetch 亦被挡）与 `macro_china_cpi`（确实无核心分项）→ 未试官网**新版前端**接口。
- 实际：国家统计局「国家数据」（`data.stats.gov.cn/dg/website`）改走
  `/publicrelease/web/external/**` 一组新接口，**纯 requests 可直连**（仅需一次 WAF 握手）。
- 官方口径名 =「**不包括食品和能源**居民消费价格指数 (上年同月=100)」→ 指数−100 = 核心 CPI 同比。
- 数据自洽校验：2026-08 核心 CPI **+1.0%** vs 整体 CPI **+0.8%**（食品通缩拖累整体 → 核心高于整体，方向正确）。
- 「替代口径」（`CN.CPI_MOM` + 菜篮子 + 猪价）**保留**：它们是日频/月频高频验证，与核心 CPI 互补而非替代。

**新增指标（实测读数，2026-10-08）**

| 指标 | 值 | 说明 |
|---|---|---|
| `RISK.CREDIT_SPREAD_AA` | 0.409pp | AA 绝对信用利差；⚠ 历史仅 4 天（源 3 日窗）→ 分位无意义（已标注） |
| `RISK.GRADE_SPREAD` | 0.137pp | **等级利差**（AA−AAA）：同期限仅等级不同 → 纯风险偏好；已入 **FSI（sign +1）** |
| `MACRO.VEG_BASKET_YOY` | **−3.73%**（分位 17.7） | 菜篮子同比为负 → 与 CPI 同比 0.8% 的低通胀一致 |
| `MACRO.PORK_PRICE` | 10.20（分位 54.1） | 猪价；历史 133 天（源 7.5 月窗）→ 分位参考价值有限 |
| `MACRO.PORK_PRICE` / `VEG_BASKET_YOY` | — | 均**不入合成**（通胀维度暂只作观察）|

双市场全量重算：CN indicator_value 180,676 → **185,680** 行；CN FSI 09-30 = 58.1 → 10-08 = **63.7**。

**测试**（`tests/test_macro_monthly_parse.py` 新增 4 项）：AA 曲线存在且新鲜、猪价/菜篮子覆盖、
**AA 信用利差 = AA5Y − 国债5Y**、**等级利差 = AA − AAA 且必须为正**（挡方向写反）。全量 **209 项通过**。

**未做（P1 尾巴）**：MLF/OMO、政策事件类（§3.5 六类）、ECRI（付费源）、产出缺口（可由库内 GDP 自算，见 §7.18 收尾清单）。（**OECD CLI 已于 §7.18 落地**）
（**核心 CPI 已于 §7.17 落地**——本节「源不可得」的旧结论已作废）

---

### 7.17 核心 CPI 落地记录（2026-10-08，**更正 §7.16 的错误结论**）

**纠错**：§7.16 记「核心 CPI 免费源不可得（已实测）」——**结论错误**。原实测只覆盖了两条路：
akshare `macro_china_nbs_nation`（走**老接口** `easyquery.htm`，实测 403 URLACL，连在页面内同源
fetch 也被 WAF 拦）与 `macro_china_cpi`（确实无核心分项）。但国家统计局**新版前端**
「国家数据」（用户提供入口 `data.stats.gov.cn/dg/website/page.html#/pc/national/monthData`）
已改走另一组接口，**纯 requests 可直连**（仅需一次 WAF 握手）。

**新接口链路（已封进 `get_macro_monthly.py` 的 `NBS_*`，不必再用浏览器）**

| 步骤 | 接口 | 说明 |
|---|---|---|
| ① 建会话 | `GET /dg/website/page.html` | 取 cookie；**再打一次目录树接口做「WAF 握手」**（见下「坑 1」） |
| ② 下钻目录 | `GET .../external/new/queryIndexTreeAsync?pid=&code=1` | `code=1` 月度数据；逐级 pid 下钻 |
| ③ 列指标 | `GET .../external/new/queryIndicatorsByCid?cid=` | 叶子目录下的指标（官方名 `i_showname` + `_id`） |
| ④ 取序列 | `POST .../external/stream/esData` | body 需 `cid/indicatorIds/das/showType/dts/**rootId**` |

**指标定位口径**：官方名不是俗称「核心 CPI」，而是
**「不包括食品和能源居民消费价格指数 (上年同月=100)」**（按 `i_showname` 关键字匹配，勿按俗称搜）；
源为**指数型**（100=持平）→ 入库统一换算同比（**指数 − 100**），与 `CN.CPI_YOY`（已是 %）同口径。

**实测踩到并已修的 3 个坑**

1. **WAF 首次挑战**：每个新会话的**第一次** API 调用返回 200 + HTML 反爬页（39KB，
   「Please enable JavaScript…」），同时下发 cookie；**同会话下一次即返回 JSON**。
   → 若重试时「每次重建会话」就会把通关状态丢掉、永远卡在第一次（首轮正式跑就是这么挂的，连续 3 次失败）；
   现改为：建会话时**主动握手**消耗挑战 + 重试**沿用同一会话**（仅 HTTP 层错误才重建）。
2. **`dts=""` 只返回默认最近 9 个月**：核心 CPI 只回来 17 条、首段被截断。
   → 必须显式传区间（`["200001MM-今月MM"]`）；实测 27 年跨度请求可一次返回 324 个月，无分页上限。
3. **同一指标被切成多个时段目录**：`(2026-)` / `(2021-2025)` / `(2016-2020)` / `(-2015)`，
   各段**指标 id 不同**，且早期段**根本没有该指标** → 必须遍历全部匹配叶子并合并。

**时段覆盖实测**（4 个叶子 × 3 种口径：上年同月=100 / 上年同期=100 / 上月=100）

| 时段目录 | 指标数 | 含核心 CPI |
|---|---|---|
| `(2026-)` | 13 | ✅ |
| `(2021-2025)` | 13 | ✅ |
| `(2016-2020)` | 9 | ❌（旧八大类，无核心口径） |
| `(-2015)` | 9 | ❌（同上） |

→ **核心 CPI 可得区间 = 2021-01 ~ 2026-08 共 68 个月**，是**源库分类标准的结构性边界**（非采集限制）；
年度/季度库同样无核心口径。若要 2021 前历史只能外采（Wind/朝阳永续）。

**产出与校验**

- 新序列 `CN.CORE_CPI_YOY`（`regime.macro_series`，68 行，`release_time = period + 45d` 保守口径，
  与 `CN.CPI_YOY` 同惯例）+ 计算层指标 `MACRO.CORE_CPI_YOY`（通胀维度，**不入合成**，同猪价/菜篮子）。
- **数据自洽校验**：2026-08 核心 CPI **+1.0%** vs 整体 CPI **+0.8%** —— 食品通缩拖累整体、
  核心高于整体，方向与幅度均符合 2026 年格局（量纲换算正确性的关键旁证）。
- 新增 `tests/test_macro_nbs.py`（12 项）：期间码解析、**区间必须非空**、多段合并、空值剔除、
  rootId 取自上而下列（不许硬编码）、指数−100 换算、库内覆盖/逐月连续/`release_time > period_date`。
- 无需改调度：核心 CPI 并入既有「宏观月频采集」（18:20）每日幂等刷新。

---

### 7.18 OECD 综合领先指标 CLI 落地记录（2026-10-08）

**入口**：用户给的 `https://data-explorer.oecd.org/vis?df[id]=DSD_STES%40DF_CLI...` 是**可视化页**
（前端 SPA，`.M.LI...AA...H` 只是页面上的选择状态），真正可编程的是其背后的
**OECD 官方 SDMX REST**：

```
GET https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1/{area}.M.LI...AA...H?format=csvfile
```

- **无鉴权、无 WAF、纯 requests 可取**（与 NBS 的挑战页形成对比；一次 ~480KB CSV）；
- 键 `.M.LI...AA...H`（9 维）：`FREQ=M` 月度 / `MEASURE=LI` 领先指标 / `ADJUSTMENT=AA` 振幅调整 /
  `METHODOLOGY=H`；返回 `UNIT_MEASURE=IX`=指数（**100 = 长期趋势**）；
- 取数技巧：`REF_AREA` 支持 `CHN+USA+JPN+…`，**一次请求取回全部经济体**（实测 7 个 area 共 3,659 个月度观测）。

**覆盖实测（2026-10-08）**

| 序列 | REF_AREA | 覆盖 | 行数 |
|---|---|---|---|
| `CN.CLI` | CHN | 1992-05 ~ **2026-09** | 413（**无缺口**） |
| `US.CLI` / `JP.CLI` / `DE.CLI` | USA/JPN/DEU | 1980-01 ~ 2026-09 | 各 561 |
| `KR.CLI` | KOR | 1990-01 ~ 2026-09 | 441 |
| `GLOBAL.CLI_G7` / `GLOBAL.CLI_G20` | G7/G20 | 1980-01 ~ 2026-09 | 各 561 |

> G4E（美日德英）聚合也有；`EA20`/`OECD` 该数据流无此 code（未使用）。
> 最新期为 **2026-09**（当月即得）→ 发布滞后实际 ≤ 月初+37 天。

**⚠ 本轮自查出并修掉的一个前视错误（值得记）**

我最初按「月末 + 8 天可得」写成 `lag=15`，但 **`lag` 在项目口径里以「月初」（period_date）为基准**：
上月 CLI 的发布在**次月初**（≈ 月初 + 37 天），`lag=15` 等于把数据**提前约 3 周**视为可得 = **前视**。
→ 已改回月频默认 **`lag=45`**（= 月末+15），并加测试钉死
（`release_time >= period_date + 45d`；`tests/test_macro_oecd.py::test_pit_not_before_release`）。
教训：**「几天后可得」必须换算成「距 period_date 的天数」**再写 lag，两者差一个月。

**产出**

- `regime.macro_series` 7 个序列（一次请求，经 cache 复用；`market` 字段按 code 前缀派生，
  为此把 `_mk_row` 的硬编码 `"CN"` 改为 `defn.market or code 前缀`——既有 CN 序列行为不变，已加回归测试）；
- 计算层 2 个指标：`MACRO.CLI_CN`（景气，中国）、`MACRO.CLI_GLOBAL`（景气，G20）——**不入合成**，
  作周期定位的独立读数（同猪价/菜篮子/核心 CPI 的处理）；CN 全历史重算后入库；
- 测试 `tests/test_macro_oecd.py` **9 项**：一次请求复用 cache、area 并集完整、空观测（OBS_STATUS=M）跳过、
  market 派生回归、库内覆盖/值域/PIT。
- 调度：并入既有「宏观月频采集」（18:20 日检幂等），无需新增任务。

**当前读数（2026-09）**：中国 CLI = **97.41**（低于 100 = 长期趋势之下），且 2026-06 以来
98.15 → 97.88 → 97.64 → 97.41 **连续 4 个月走弱**；对照 G20 读数可分离「国内 vs 外部」驱动力。

**§3.1 缺口状态更新**（承上轮审计）：CLI ✅ 已补（§7.18）；**发电量 ✅ 已补（§7.19）**；
**PMI 新订单分项 ✅ / 工业企业利润 ✅ 已补（§7.20）**；M0 ✅（同批）；
**产出缺口 ✅ / 美林四象限 ✅（§7.21）**。
→ **§3.1 至此收口**，仅余 **ECRI**（付费源，建议标注「外采/放弃」）。

---

### 7.19 发电量落地记录（2026-10-09）

**入口**：用户提供路径 —— 国家数据「月度数据 → **能源** → 能源主要产品产量 → **发电量**」。
沿用 §7.17 的 NBS 新版接口（`queryIndexTreeAsync` → `queryIndicatorsByCid` → `stream/esData`）。

**该目录的结构（重要）**：`能源主要产品产量` 下有 22 个**平级**叶子 —— 原煤/原油/天然气/…/
**发电量 / 火力发电量 / 水力发电量 / 核能发电量 / 风力发电量 / 太阳能发电量** / 煤气；
每个叶子各挂 4 个口径：当期值(亿千瓦时) / 累计值(亿千瓦时) / **同比增长(%)** / 累计增长(%)。

> ⚠ 「发电量」与「火力发电量」等是**平级**叶子 → 采集的 `leaf_prefix` 必须是**前匹配**
> （`startswith`）而非包含匹配，否则火电/水电/核电/风电/太阳能 5 个叶子会一起命中，
> 把分品种发电量混算成总量。已加专项测试（`TestPowerGenLeafSelection`，断言只查总量叶子的 cid）。

**产出**

| 项 | 内容 |
|---|---|
| 序列 | `CN.POWER_GEN_YOY`（发电量同比 %，**2000-02 起 280 个月**，最新 **2026-08**） |
| 调度 | 并入既有「宏观月频采集」（18:20 日检幂等），无新任务 |
| PIT | `release_time = period + 45d`（实测 NBS 与工业增加值同批于次月中旬发布 → 45 天口径贴合） |
| 测试 | `tests/test_macro_nbs.py` 新增 6 项（叶子前匹配 + 库内覆盖 + 同比量纲 + PIT + 跨源符号一致率 + **缺口模式**） |

**⚠ 另一个实测口径特性：1–2 月合并发布（固有缺口）**

该序列 280 行 / 319 个月跨度 → **缺 39 个月**，按月份分布：**1 月 26 个 + 2 月 12 个 + 2012-06 一次源缺失**（97% 为 1/2 月）。
即 NBS 月度口径近年**只发布 1–2 月合计**、不发单独的 1 月/2 月值（早期只缺 1 月）。
→ 做滚动窗口/环比时必须按**缺失**处理，**绝不能补 0**（补 0 会把同比序列拖出假坑）。
同类序列同样存在：`CN.ELECTRICITY_YOY` 240/273（缺 33，其中 1 月 22）、`CN.FREIGHT_YOY` 291/320（缺 29）。
已写入三条序列的 remark，并加「缺口模式」回归测试（缺失月中 1/2 月占比须 ≥90%，挡「整年成片缺失」这类真故障）。

**⚠ 跨源交叉验证揭示的真实口径差异（已写进序列 remark）**

发电量与「全社会用电量」**不是同一口径、不可互替**：

| 对照 | 结果 |
|---|---|
| 重叠样本 | 226 个月（2003-12 ~ 2026-08） |
| 符号一致率 | **88%**（测试阈值取 80%） |
| 相关系数 | 全样本 0.63 / 近 60 月 **0.47** |
| 2026-08 背离 | 发电量 **−0.8%** vs 用电量 **+4.3%（差 5.1pp）** |

原因：本序列是**规模以上工业发电量**，**不含规模以下/分布式光伏与自备电厂**（近年分布式装机高增 →
规上发电量系统性低估总发电增速）。→ **实体活跃度优先看用电量**（需求侧、覆盖全社会），发电量作供给侧对照；
测试只校验「符号一致率 ≥80%」，**不要求高相关**（强行要求会把真实口径差误判为数据错误）。

**顺带验证：另两个 §3.1 缺口的可得性**（**已于 §7.20 采纳落地**）

| 目标 | NBS 位置 | 可用口径 |
|---|---|---|
| **工业企业利润** | 月度数据 → 工业 → **工业企业主要经济指标**（叶子，76 个指标） | `利润总额累计值(亿元)` / `利润总额累计增长(%)` ⚠ **累计口径**（年内累计同比，非单月），接入时须在 remark 注明 |
| **PMI 新订单** | 月度数据 → 采购经理指数 → **制造业采购经理指数**（叶子） | `新订单指数(%)` ✅ 月度、无累计问题 |

---

### 7.20 §3.1 剩余缺口落地记录：PMI 新订单 / 工业企业利润 / M0（2026-10-09）

**用户提供的四个目录路径 → 逐项处置**

| 目标 | 用户给的路径 | 处置 |
|---|---|---|
| **货运量** | 交通运输 → 货物运输量 | ⏸ **不重复采**：既有 `CN.FREIGHT_YOY`（akshare）与 NBS 源**逐月校验一致**（243 个重叠月、平均绝对差 **0.021pp**、最大 1.93pp）→ 说明 akshare 该序列本就是 NBS 数据，登记等价性即可 |
| **PMI 新订单** | 采购经理指数 → 制造业采购经理指数 | ✅ 新增 `CN.PMI_NEW_ORDERS`：**2005-01 起 261 个月**，最新 **2026-09**。akshare 的 `macro_china_pmi` 只有总指数（无分项）→ 必须走 NBS |
| **货币供应量** | 金融 → 货币供应量 | ✅ M1/M2 同样经 NBS 校验一致（223 月 / 0.022pp）；**且既有 akshare 表更新到 2026-08、NBS 表只到 2026-07** → 保留 akshare 为源；**补 M0**（同表已有「流通中的现金(M0)-同比增长」列）→ `CN.M0_YOY` |
| **工业企业利润** | 工业 → 工业企业主要经济指标 | ✅ 新增 `CN.INDUSTRIAL_PROFIT_YOY`：**2000-02 起 265 个月**，最新 2026-08 = 15.7%。⚠ **累计口径**（见下） |

> 注：M0 走 akshare 而非 NBS 是**有意为之**——NBS 货币供应量表实测比 akshare 晚一个月
> （2026-10-09 时 NBS 仅到 2026-07），且两者重叠月逐月一致（2026-07 = 11.6% ✓）。
> 若将来需要 2000-2008 的 M0 历史（akshare 表自 2008-01 起），可再补一条 NBS 序列。

**采集层修的三处（本轮实测踩到）**

1. **指标级前匹配**：`交通运输 → 货物运输量` 的 20 个指标里，「**货运量**_同比增长」正是
   「铁路**货运量**_同比增长」的子串 → 包含匹配会把铁路/公路/水运/民航 4 个分品种一并命中并混算。
   已把指标匹配改为 **startswith 前匹配**（与叶子的规则一致），并加专项测试
   （`TestIndicatorPrefixStrictness`，断言只命中总量指标）。
2. **零命中改为报错**：此前若官方名变化导致全时段都匹配不到，会**静默返回空序列**（看起来像"今天没新数据"）；
   现改为抛错（`run()` 逐序列捕获后打印 ❌），故障可见。
3. **加树/指标清单缓存**：NBS 序列从 1 条增到 5 条，不缓存会「每个序列重走一遍目录树」
   （≈20 次请求/日）→ 现共用缓存，日请求降到 ~8 次，兼顾速度与反爬风险。

**PIT 与口径**

- `CN.PMI_NEW_ORDERS`：lag=45（同 `CN.PMI_MFG` 惯例）。**注：PMI 实际于次月首日发布 → 45 天属偏保守**，
  为与既有总指数序列保持可比未单独收紧。
- `CN.INDUSTRIAL_PROFIT_YOY`：lag=45；**累计口径**（年初至今累计同比，NBS 该表无单月值）→
  已写入 remark「**勿与 CN.IP_YOY（单月同比）直接相减**」；缺口模式：**1 月每年缺**（1-2 月合并发布）、
  **2007-2010 仅 2/5/8/11 月有值**（该期为准季度发布）→ 消费方须按缺失处理。
- `CN.M0_YOY`：lag=45，与 M1/M2 同源同表。

**校验**

- **跨源互证**：`CN.PMI_NEW_ORDERS` 与 `CN.PMI_MFG`（不同来源路径）重叠 225 月、**相关 0.983** ✓；
- 值域核验：PMI 分项 29.3~65.1（围绕 50）✓、M0 同比 −17.6~42.5（含 2009 刺激期高增）✓、
  工业利润累计同比 −38.3~180.3（含 2000 年代高增与 2009 断崖）✓；
- 测试：`tests/test_macro_nbs.py` 新增 **6 项**（指标前匹配 / 零命中报错 / PMI 新订单+相关性 / 利润累计口径与 1 月缺口 / M0 覆盖与值域）。

**当前读数（2026-09 / 2026-08）**：PMI 新订单 **50.5**（2026-09，回到荣枯线上方，7 月曾 48.5）；
工业利润累计同比 **15.7%**（连续 3 个月回落：18.7 → 17.6 → 15.7）；M0 **11.2%** vs M1 **4.1%**
（M0 高企、M1 低迷 → 资金活化为**待观察**信号）。

---

### 7.21 产出缺口 + 美林投资时钟落地记录（2026-10-09）

**新增采集（NBS 季度库，沿用 §7.17 接口）**

| 序列 | 内容 | 覆盖 |
|---|---|---|
| `CN.GDP_QOQ` | GDP **环比增长速度**（NBS 已季调的单季环比 %）→ 链成 SA 实际水平 | 2011Q1 起 62 季 |
| `CN.GDP_YOY_Q` | GDP **单季实际同比**（「指数(上年同期=100) 当季值」−100） | **1993Q1 起 134 季** |

**季度接口的两个新特性（实测）**：① 期间码是 `202603SS`（2 位季度号，非 MM）；
② `dts` **必须逐期枚举**（`["201101SS", …]`）——季度区间串的 `SS/QQ`/对象等各种写法**一律 500**
（与月度可用区间串相反，已写进 `_nbs_dts` 注释与测试）。

**PIT 收紧：季度 lag 由 100 改为 110（含订正既有两序列）**

实测 NBS 季度 GDP 于**季后次月 15-20 日**发布（Q3 数据 ≈10-18）→ 距 `period_date`（季度首月 1 日）
约 **105-110 天**，原 `lag=100` 会让数据提前 5-10 天视为可得（**轻微前视**）。
→ `CN.GDP` / `CN.GDP_YOY` / 两条新序列统一改为 **lag=110**（历史 release_time 一并前移）。

**计算层指标**

| 指标 | 口径 | 说明 |
|---|---|---|
| `MACRO.OUTPUT_GAP` | **水平口径** %：(实际 − 潜在)/潜在 | 实际水平用 NBS **已季调**环比链成（基期任取，比值无关）；潜在 = **单边** HP（λ=1600）。⚠ 源链自 2011Q1、前 12 季暖机 → **2014Q1 起有值**（约 50 季） |
| `MACRO.CLOCK_QUADRANT` | 分类码 1-4：1=复苏 2=过热 3=滞胀 4=衰退 | 增长轴 = 产出缺口方向（本季 vs 上季）；通胀轴 = CPI 同比 **6 个月**方向（去单月噪声）；两轴各按自身 release_time 推进、任一更新即重算 |

**三个实现要点（都加了测试）**

1. **HP 必须单边**：HP 默认是**双边**滤波（趋势点用到未来期），全样本跑一遍取趋势 = **前视**。
   本实现对第 i 期只用 `lv[:i+1]` 重算并取末点（O(n) 次滤波，本数据量毫秒级）；
   测试断言 `release_time` 严格递增 + HP 自检（**线性输入的趋势＝自身**）。
2. **分类值不分位**：象限 1-4 是类别标签 → 新增 `NO_PERCENTILE` 集合，写库时 `percentile` 恒为
   `NULL`（测试断言不得有分位），避免被误用成"历史分位 25/50/75"。
3. **依赖**：本环境无 `statsmodels` → HP 用 `scipy.sparse` 三对角解自实现（scipy 已装，无新增依赖）。

**验证**

- HP 自检：线性序列趋势＝自身 ✓；随机游走趋势比原序列更平滑 ✓；长度 <3 原样返回 ✓。
- **缺口极值落在真实危机**：最低 **2020-04 = −9.55%**（疫情冲击）、最高 **2021-01 = +2.46%**（复苏）✓。
- 象限分布 `{1:62, 2:34, 3:65, 4:38}`（四类均衡，映射未退化）✓。
- 测试 `tests/test_regime_clock.py` **10 项**（HP 3 + 象限映射 2 + 缺口 2 + 库内契约 3）+
  `tests/test_macro_nbs.py` 补季度码/枚举 3 项。

**当前读数**：产出缺口 **+0.16%**（2026Q2，仍为小幅正缺口但连续走低：0.39 → 0.16）；
美林时钟 **2026-09 = 4 衰退**（增长↓ + 通胀↓；8 月为 3 滞胀）。

**§3.1 收口**：至此仅剩 **ECRI**（付费源）未做 → 建议在文档中标为「外采/放弃」，不再列为待办。

> **⚠ 本节「当前读数」「象限分布」均为 v1 口径的历史快照（2026-10-09），现行口径见下节 v2。**

---

### 7.21b 美林时钟 v2 口径修正与复评（2026-11-11）

**起因**：对 `MACRO.CLOCK_QUADRANT` 做了一次独立评测，v1 的四处结构性问题（库内实测，2014-2026）：

| 问题 | v1 实测证据 |
|---|---|
| 零阈值硬判 sign | \|Δgap\| < 0.1pp 占 29.4% 的季度、\|ΔCPI6\| < 0.1pp 占 7.3%、3.6% 恰为 0（被判「通胀↓」）；加 ±0.1pp 死区后仅 **75.9%** 的日频标签不变（800 个交易日改标签） |
| 参数无依据 | CPI 回看窗口 6→3/9/12 月，标签一致率仅 70.0~76.6% |
| 增长轴口径失真 | `sign(Δgap)` 与 `sign(ΔGDP_QOQ)` 一致率 72.5%、与 `sign(Δ单季实际同比)` 仅 **62.7%** —— 单边 HP 每期重估潜在产出，趋势变化混进了「增长动能」 |
| 混频错配 | 54 次象限切换里 **28 次（52%）由月度 CPI 触发**，而字典 `freq=quarter`；日频游程中位仅 40 个交易日、每年切换 4.4 次（美林时钟完整周期通常 3~5 年） |

**四处修正**（`market_state_daily.py`：`_gap_frame` / `_sign_eps` / `_infl_flag` / `_clock_points`）：

1. **统一季频**：只在季度 GDP 的 `release_time` 上重算；通胀轴在该时点按 PIT 取「已可见的最新 CPI」及其 **6 个日历月**前值（`merge_asof backward`）。月度 CPI 不再单独触发象限 → `freq=quarter` 名实相符。
2. **双轴死区 + 滞回**：`CLOCK_GAP_EPS` / `CLOCK_YOY_EPS` / `CLOCK_CPI_EPS` 均 0.20pp，落在死区内或非有限值 → 保持上一状态，替代 `>0` 硬阈值。
3. **增长轴双口径投票**：Δgap 与 `CN.GDP_YOY_Q`（单季实际同比，1993Q1 起）方向一致才更新；分歧或双缺失时保持上一状态。
4. **通胀水平门控**：CPI 同比 < 0 时「跌幅收窄」不判通胀上行。`_gap_frame` 另补三处防御：按 `period_date` 排序（旧按 release_time）、季度连续性校验、NaN 线性插值（旧填 0 = 零增长）；CPI 窗口按日历月索引（旧按行序 `k−6`，缺月即错位）。

**复评（同一库、同一日频轴，v1 = 库内 `indicator_value` 旧值，v2 = 新函数现算）**：

| 评测项 | v1 | v2 | 判定 |
|---|---|---|---|
| 事件数 / 切换次数 | 202 / 54 | **51 / 23** | ✅ 纯季频 |
| 切换频率（次/年） | 4.4 | **1.9** | ✅ 接近时钟周期尺度 |
| 日频游程中位（交易日） | 40 | **93** | ✅ 不再碎片化 |
| 死区扰动 0.20→0.25pp 一致率 | — | **0.92** | ✅ |
| 死区扰动 0.20→0.10pp 一致率 | 0.759（±0.1pp 同幅度） | **0.821** | ✅ 改善 6.2pp |
| CPI 窗口 6→7 / 6→9 一致率 | — / 0.766 | **0.895 / 0.860** | ✅ |
| 增长轴 vs `sign(Δ单季同比)` 一致率 | 0.647 | **0.725** | ✅ 改善 7.8pp |
| 象限分布（事件数） | 1:64 2:34 3:65 4:39（202 个事件，含月频） | 1:15 2:10 3:17 4:9（51 个季度事件） | ✅ 四类均衡 |
| 通缩期误判「通胀↑」 | 0（季度事件口径） | 0 | ➖ 季度口径下门控未触发，属预防性修正 |
| 象限 ↔ **经济状态**（正确的准确性检验） | 增长轴与 `Δ单季同比` 一致 64.7% | 增长轴 **72.5%**、通胀轴 **100%**（\|ΔCPI6\|>0.2pp 的 40 个季度） | ✅ 分类可靠 |

> ⚠ 初版复评曾把「象限 → 股/债」映射反向列作缺陷，属**循环论证**（详见 §7.21c 与下方订正）。该行已从评测判据中移除。

**未改善 / 未解决**：

- **评测框架订正（复盘）**：初版把「象限 → 股/债」映射反向列作算法缺陷，属**循环论证**——拿一条在中国市场未经验证的理论映射当真值，去检验一个「经济周期分类算法」。用户指出经济周期与股市本就不同步，§7.21c 的长样本验证证实该命题。故**资产映射反向不构成算法缺陷**，从评测判据中移除；`remark` 里「→股/→商品/→现金/→债」仍标注为理论示意、不得当配置信号，但这是**适用边界**问题，不是算法 bug。
- **CPI 窗口 6→12 月一致率仍只有 0.76、6→4 月 0.638**：这里的差异主要是「6 月动量 vs 年度趋势 / 短动量」属不同口径，不全是噪声；未通过调参强行贴合，避免过拟合。
- **PIT 只锁时点、不锁数值**：`macro_series` 全表 `revision=0`（26.7 万行），历史 GDP 季调环比用的是今天修订后的值；`release_time = period_date + lag` 是保守估计而非真实发布日（CPI 偏晚约 1 周）。补历史修订版本需改采集侧唯一约束 + 全量回填，且首次发布值已不可追溯 → **列为独立 workstream，本次不做**。
- **口径变更必须回填**：`formula` 已同步改写（含 0.2pp / 6 月参数），它进入 `_params_hash` → `indicator_dict.params_hash` 与 `indicator_value.model_version` 换新值。按 §5.7 PIT 铁律，需执行 `python3 market_state_daily.py --backfill` 才能让库内历史值切换到 v2；**本次只改代码与文档，未写库、未回填、未重启服务**。

**测试**：`tests/test_regime_clock.py` 由 10 项扩到 **18 项**（新增 `_sign_eps` / `_infl_flag` 纯函数 2 项、`_gap_frame` 排序与覆盖 1 项、纯季频 PIT 时点 1 项、增长轴一致率 1 项、切换率 1 项、参数扰动回归 2 项）。全量 pytest 结果见交付说明。

---

### 7.21c 经济周期与股市不同步：长样本验证（2026-11-11）

**命题**（用户提出）：经济复苏股市不一定涨、经济停滞股市反而涨；故不能用股市表现去评判「经济周期分类算法」是否准确。

**数据**：`daily_benchmark SH.000001`（上证综指，1990-12 起 8,760 交易日）、`CN.GDP_YOY_Q`（NBS 单季实际同比，1993Q1 起 135 季）、`CN.BOND_INDEX`（中债综合指数，2002 起）、`CN.CPI_YOY`。按统计期对齐做**描述性**宏观—市场关系检验（非交易回测，不含 PIT 可见性约束）。

| 检验 | 结果 |
|---|---|
| 季度股收益 vs 同期 GDP 同比水平（134 季） | corr **0.089** |
| 季度股收益 vs 同期 ΔGDP | corr **0.082** |
| 年度股收益 vs 年度 GDP 增速 / ΔGDP（1994-2026，33 年） | 0.254 / **0.249** |
| 增长↑季度股收益（n=45） | 均值 +4.28%、中位 +0.74%、胜率 57.8% |
| 增长↓季度股收益（n=89） | 均值 +1.17%、中位 −0.48%、胜率 47.2% |
| 股收益 vs 未来 2~3 季 GDP 水平 | 峰值 corr 仅 **0.154**（股市略领先但很弱） |

**背离是常态，不是例外**：33 年里有 17 个股市上涨年，其中 **10 个（59%）GDP 增速在下行**：

| 年份 | GDP 增速变化 | 上证年度收益 |
|---|---|---|
| 1996 | −1.08pp | **+65.1%** |
| 2009 | −0.53pp | **+80.0%** |
| 2014 | −0.28pp | **+52.9%** |
| 2019 | −0.65pp | **+22.3%** |
| 2020 | −4.15pp | **+13.9%** |
| 2024 | −0.38pp | **+12.7%** |
| 2025 | −0.03pp | **+18.4%** |

反向背离同样存在：GDP 加速但股市下跌 5 年（2002 −17.5% / 2004 −15.4% / 2005 −8.3% / 2010 −14.3% / 2023 −3.7%）。

**领先关系也不稳定**（按时代分段，`ret[t]` vs `GDP[t+2]`）：1993-2005 = **−0.128**、2006-2013 = **+0.662**、2014-2026 = **+0.147**。即「股市是经济晴雨表」只在 2006-2013 这一段成立，其余时期近零或为负 → regime-dependent，无法固化为配置规则。

**象限视角**：v2 象限与「股−债差」在 21 / 63 / 126 / 252 交易日四个尺度上方向都不稳定 —— 象限 4 在 63 日(+0.054)、126 日(+0.070) 最高，到 252 日翻成最低(−0.094)；象限 1 则相反(63 日 −0.003 → 252 日 +0.086)。同一分类在不同 horizon 符号翻转，进一步说明**该映射不是稳定规律**，而不是象限分错了。

**公开资料佐证**（网络检索的二手转述，未逐篇核验原文）：MSCI 对 1958-2018 全球主要市场的研究显示「经济增速越快股市回报未必更好、成熟市场甚至微弱负相关」（澎湃新闻转述）；长江商学院刘劲 / 陈宏亚测算 2000-2024 中国名义 GDP 年均 +11.4%、全部 A 股含红利年均回报仅 5.2%；厦门大学《Dissecting the Long-term Performance of the Chinese Stock Market》指出 1992-2014 实际 GDP 增长 8 倍而上证几乎是全球表现最差指数之一；AEMPS 对 2015Q1-2023Q4 的三阶段研究发现疫情前「经济领先市场」本就很弱、疫情期间双向关系崩塌；2025 年实例为前三季 GDP 5.4→5.2→4.8 逐季走低、上证 10 月突破 4000 点创十年新高。

**结论与边界**：

1. 用户命题成立，且比「不同步」更强 —— 连**稳定的领先关系**都不成立（分段符号会翻转）。
2. 因此 `MACRO.CLOCK_QUADRANT` 的正确验收标准是「是否忠实刻画**经济状态**」，而非「是否预测股市」：v2 增长轴 72.5%、通胀轴 100%（剔除死区内样本）→ 分类本身可靠。
3. Merrill Lynch(2004) 的「复苏→股 / 衰退→债」是特定市场长样本经验 + 贴现机制的产物，在中国不成立属**适用边界**，不是实现错误。`remark` 保留该文案但已标注「理论映射、未实证验证、不得当配置信号」；§4.4 若要用时钟做战术分，应走「周期定位 + 与 §4.2 快变量互校」，不得直接映射资产权重。
4. 本验证只改文档结论，**不改代码、不改算法、不回填**。

---

### 7.22 `get_global_benchmarks.py` 复用审计 + 汇率口径订正 + 季度 GDP 节点核对（2026-10-09）

**审计对象**：`get_global_benchmarks.py`（调度「全球指数采集」**每小时**跑，17 个 `bench_code` → `daily_benchmark`）
及其消费方。结论：**这批序列大多已在跑但没被 regime 层用起来** —— 属"零采集成本"可复用池。

| 序列 | 现状 | 复用建议 |
|---|---|---|
| `US.DGS10` / `US.DGS2` | ✅ **已被计算层消费**（中美利差、美债期限利差 10Y−2Y） | — |
| `US.VIXCLS` | ⚪ 仅被 `macro_environment_score.py` 消费（该任务**已在调度中注释停用**） | 可直接补 **§5.4 跨资产风险偏好**（全球风险温度） |
| `US.DTWEXBGS` / `US.DXY` | ⚪ 同上（双口径美元指数） | **§3.4 美元闸门**（双口径互为校验） |
| `US.EFFR` | ⚪ 同上 | **§3.4 美联储政策**的政策利率代理 |
| `US.SP500` / `US.NASDAQCOM` / `JP.N225` / `KR.KS11` / `DE.GDAXI` | ⚪ 仅入库 | **§4.2 跨资产信号**、A股/港股 vs 全球的相对强弱与相关性 |
| `HK.800000` / `HK.800700` | ✅ 港股计算层在用 | — |
| `SH.000001` / `SZ.399001` | ✅ A股计算层在用 | — |
| `FX.USDCNY` | ⚠ **口径标错**（见下） | 修正后在 **§3.4 人民币汇率**可用（在岸口径） |

**⚠ 口径订正：`FX.USDCNY` 是「在岸」不是「离岸」**

- 源是 `ak.currency_boc_safe()` = **中国银行外汇牌价（在岸折算价/中间价）**；实测库里最新 `6.733`
  与 `currency_boc_safe` 最新值 `673.30/100 = 6.733` **完全一致** ✓
- 更直接的旁证：`collect_benchmark_history.py` 的 `collect_currency` docstring **早就写明**
  「注意这是外汇管理局**在岸**中间价，非严格离岸 CNH」——只是 `bench_name` 与注释没同步。
- 已修 **3 处标签**（`get_global_benchmarks.py` / `collect_benchmark_history.py` / `macro_environment_score.py`）
  + 库内 `daily_benchmark` 8,061 行 `bench_name` 一并改名为「在岸人民币(中行折算价)」。
- **CNH（离岸）仍缺**：`forex_hist_em('USDCNH')` / `forex_spot_em()` **东财源断连**（同类 push2 问题）、
  `currency_boc_sina` 历史**停更 2023-11**、`fx_spot_quote` 是 CFETS **在岸** → 无稳定免费离岸源。
  → §3.4 的「人民币汇率」按**在岸**交付即可（CNH 作为可选增强，标注"待定位源"）。

**多脚本重复写入核查（结论：实际无冲突）**

`get_global_benchmarks.py`（每小时增量）/ `collect_benchmark_history.py`（一次性全历史）/
`backfill_benchmark.py`（回填）三脚本写**同一批 bench_code**，而 `daily_benchmark` **没有 source 列** →
理论上"最后写入者赢"、且无法溯源。实测核对 `US.DGS10` 与 **FRED 原始值 16 个交易日逐日完全一致（0.0bp）** ✓
→ akshare `bond_zh_us_rate` 与 FRED 同源，多写不产生数据冲突。
（建议：把 `get_global_benchmarks.py` 定为唯一日常权威源，另两个仅作一次性回填；如未来出现分歧，
再给 `daily_benchmark` 加 source 列。已登记为风险 **#21**。）

**季度 GDP 节点核对（用户指出「国民经济核算 → 国内生产总值」）**

该节点下是 `国内生产总值 (不变价) 当季值/累计值 (亿元)`（2007Q1 起 78 季）。
**结论：不用于产出缺口**，理由是实测证据 ——

| 缺口口径 | 输入 | 2020 疫情低点 |
|---|---|---|
| **现值（保留）** | NBS **已季调**单季环比链成水平 + 单边 HP(λ=1600) | **−9.55%** ✓ |
| 备选（否决） | 不变价当季值（**未季调**）→ 4 季滚动和去季节 + 单边 HP | **−3.53%**（冲击被摊平 ≈2.7×） |

未季调水平若直接 HP 会把季节性当周期；而 4 季滚动和会在危机时**把冲击摊到四个季度**——
恰好毁掉产出缺口最有价值的读数（危机深度）。故保留已季调环比链口径。
已顺带采集 `CN.GDP_REAL_Q`（实际 GDP 水平，2007Q1 起 78 季）作**水平参考与敏感性对照**，remark 写明上述证据。

---

### 7.23 §3.4 外部环境 + §5.4 跨资产风险偏好落地记录（2026-10-09）

**背景**：§7.22 审计发现 `get_global_benchmarks.py` 每小时采的 17 个基准里**只有 `US.DGS10`/`US.DGS2`
被计算层消费**；同时 `market_regime_daily.risk_appetite` 列**从未写过值**（schema 注释写着「暂空」）。
本轮把两者一次补齐——**零新增采集**，全部复用已在库的序列。

**新增 7 个计算层指标（全部不入既有合成，作独立读数）**

| 指标 | 维度 | 源 | 说明 |
|---|---|---|---|
| `MACRO.DXY` | 资金 | `daily_benchmark US.DXY` | ICE 美元指数；⚠ 2016-09 起（分位样本约 10 年） |
| `MACRO.DTWEXBGS` | 资金 | `US.DTWEXBGS`（FRED） | 美联储贸易加权美元；**2006 起，样本长于 DXY** |
| `MACRO.FED_FUNDS` | 资金 | `US.EFFR`（FRED） | 有效联邦基金利率（2001-12 起）；高位=紧缩 |
| `RISK.VIX` | 风险 | `US.VIXCLS`（FRED） | 全球风险温度（**1990 起长样本**） |
| `MACRO.SPX_MOM20` | 跨资产 | `US.SP500` | 标普 500 20 日动量 |
| `MACRO.COPPER_MOM20` | 跨资产 | `CMDTY.COPPER` | 铜 20 日动量（需求侧） |
| `MACRO.GOLD_MOM20` | 跨资产 | `CMDTY.GOLD` | 黄金 20 日动量（避险，指数中取反） |

**跨资产风险偏好指数（§5.4）落地 —— 填充 `risk_appetite`**

成分各自分位化、方向统一为「高分=风险偏好高」，**等权**合成 0-100（**7 成分**）：

`VIX(−) · 美元 DXY(−) · AAA 信用利差(−) · 信用等级利差 AA−AAA(−) · 标普动量(+) · 铜动量(+) · 黄金(−)`

- **信用用两个口径**（关键取舍）：AAA 绝对利差**长历史且稳**；AA−AAA 等级利差是更纯的风险偏好读数，
  但**源只保留最近 3 个交易日 → 历史上只有逐日累积的 5-7 个点**（实测），单靠它该成分分位近乎噪声，
  故两个都放（近期由等级利差贡献、历史由 AAA 利差支撑）。
- **未落两项**：①「股债相对表现」缺**债券总回报**序列（库里只有国债收益率）；②「EM vs DM 相对表现」
  缺 MSCI EM 等 EM 指数。
- **CN/HK 数值可能不同（已实测，属架构所致）**：分位按**各市场自身历史**展开
  （与其他合成一致——thermometer/fsi 亦如此）：CN 样本自 1990、HK 自 2019 →
  同一时点指数略有差异（实测 CN 51.3 vs HK 56.5，而长均值 47.1 vs 48.2）。**同市场内可比，
  跨市场比较须注意样本长度**。两个信用成分已同时接入 HK 的宏观映射（有 `MACRO.CN_US_SPREAD` 先例），
  以免两市场成分集不同。
- 双市场均写；首日无成分 → 空值（预期）。

**两个工程要点（都踩过 / 都加了测试）**

1. **动量必须在源市场日历上算**：20 日动量 = 源序列 `s/s.shift(20)−1`，再用 `merge_asof backward`
   对齐到本市场交易日。若先按本市场日历 reindex（外盘假期成 NaN）再 shift，「20 个交易日」会错位。
2. **`merge_asof` 两侧时间精度必须一致**：本市场索引建出来是 `M8[us]`、`date` 索引转出来是 `M8[s]`
   → 直接报 `incompatible merge keys`（实测首次干跑即失败），已统一为 `datetime64[ns]`。

**验证**

- 预览相关方向：指数与 **VIX 分位 −0.61**、与**标普动量分位 +0.79**（sign 未写反）✓；
- 库内分布（最终值）：CN `0.0~99.9`、均值 **49.6**；HK `11.8~88.9`、均值 **53.6**（均落在中性附近，合理）；
- 最新读数（2026-10-09）：**CN 58.1 / HK 62.3**（中性偏 Risk-On，与同日温度计 25.5 的"市场冷"形成对照）；
  同期 `MACRO.DXY` 102.03（分位 73.3）、`MACRO.FED_FUNDS` 3.88%（分位 **79.1**，处历史高位）、
  `RISK.VIX` 15.08（34.0）、铜动量 +2.06%（62.0）、黄金动量 **−4.81%**（9.2）。
- 新增 `tests/test_global_risk.py`（8 项）：对齐无前视 / 精度混用回归 / 动量源日历 / 指标入库覆盖 /
  双市场落库 / 0-100 值域与均值 / **与 VIX 负相关、与股指动量正相关**（挡 sign 写反）。

**未做（后续可选）**：把 VIX / 美元 / 风险偏好接入既有合成（温度计/风险指数/FSI 目前仍用原成员，
本轮一律作独立读数，避免动已验证的合成口径）；全球股指（日经/KOSPI/DAX）与 §4.2 跨资产信号尚未接。

---

### 7.24 社融换源（央行直连）+ 信贷脉冲修复 + 通胀预期落地（2026-10-09）

**① 社融换源：从「商务部镜像」到央行官网直连**

背景：`macro_china_shrzgm`（社融**增量**）上游是商务部镜像，**冻结于 2026-04**，而它正是
`MACRO.CREDIT_IMPULSE` 的唯一输入 → 该指标被**静默冻结**在 −3.21（实测 2026-06 起每月只有 1 个不同值；
比缺失更危险，因为表面上"每天都有值"）。

**央行接口路径（2026-10-09 实测，纯 requests）**

| 步骤 | 地址 | 说明 |
|---|---|---|
| ① 统计数据索引 | `/diaochatongjisi/116219/116319/index.html` | **UTF-8**；列出各年目录（1999-2026），每年下挂「社会融资规模」子页 |
| ② 年份子页 | 例子：`/116219/116319/5570903/5570885/index.html` | ⚠ **除当年外 URL 是随机 ID**（当年页才是 `/{年}ntjsj/shrzgm/`）→ 必须从索引页**就近配对**，勿硬编码 |
| ③ 条目附件 | `class="titp20"` 块内 | 每月发布「存量统计表」「增量统计表」，各带 **htm/xls/xlsx/pdf** → 抓 **htm** 即可解析（无需 Excel 库） |

**实测 3 个坑（都已处理 + 加测试）**

1. **htm 附件编码新旧不一**：2016-2019 是 **UTF-8**、2020+ 是 **GBK** → 按关键字自动判定（勿写死）。
2. **宽表每月份占 2 列**（存量 / 增速%）→ 必须**按位置**配对；先过滤空值再配对会**整体错位**
   （某月存量缺失时，后续月份会串位）。测试用「2026.2 存量列留空」的样例钉死。
3. **水平值有口径断点**：2018-12 及更早**不含国债/地方政府债**，2019-01 起纳入
   （实测 192.37 → 231.55 万亿元，跳升 20%）→ **水平值不可跨 2019 差分**，信贷脉冲只能取 2019-01 起。

**产出**：`CN.TSF_STOCK`（存量，源万亿元 → ×10000 统一亿元，**2016-01 起 122 个月**）、
`CN.TSF_STOCK_YOY`（存量同比，122 个月）、`CN.TSF_FLOW_M`（增量当月，128 个月）；
旧源 `CN.SHRZGM_INC` 标注「已被取代，仅保留历史」。

**② 信贷脉冲：口径试错三次才对（值得记的教训）**

| 口径 | 实测值域 | 判定 |
|---|---|---|
| 旧：12M 滚动社融增量的同比变化率 | 冻结在 −3.21 | ✗ 输入源停更 |
| 「存量/GDP 比率」的**同比变化** | 常年 **+10pp**（均值 9.8） | ✗ 量级跟随 **330%** 的比率水平，**没有周期含义** |
| 「流量/GDP 比率」的**月度变化** | **±1.5pp**（均值 0.05） | ✗ 量级过小、噪声主导 |
| **「流量/GDP 比率」的同比变化（Biggs/Mayer）** | **−5.5 ~ +5.9pp** | ✅ 2020 放水冲高 → 2021 断崖转负 → 2025 回升 → 2026 走弱，与市场共识一致 |

→ 最终口径：`impulse_t = F_t/GDP_ttm_t − F_{t−12}/GDP_ttm_{t−12}`，`F` = 存量 12 个月差分；
`GDP_ttm` = `CN.GDP` 差分回单季后的滚动 4 季合计；PIT 上逐月推进且 t 与 t−12 用**同一 GDP vintage**。
⚠ 需 24 个月流量 → **脉冲自 2021-02 起有值**（约 5.7 年，分位样本仍在累积）。
**测试用值域三重门禁**把上面三种错口径挡住（<4pp 或 >12pp 或首值早于 2021 均判失败）。

**③ 通胀预期（§3.3 最后一个缺口）**

`US.T10YIE`（FRED，10Y 名义 − 10Y TIPS）→ `daily_benchmark` **2003-01 起 5,947 行**，最新 **2.35%**；
计算层 `MACRO.BREAKEVEN_10Y`（通胀维度，GLOBAL，与 `MACRO.CORE_CPI_YOY` 对照可分离「预期 vs 现实」）。
中债隐含通胀预期无免费源，故以美债口径作全球锚（已在 remark 注明）。

**顺带修的工程问题**：`regime.indicator_dict.remark` 是 `varchar(200)`，本轮一条长 remark
直接把整轮计算打断（`StringDataRightTruncation`）→ 已在 `_ensure_dict` 加**按列宽截断**，
并按各列上限（name 80 / dimension 24 / unit 16 / source 64 / remark 200 …）统一裁剪。

**测试**：`tests/test_macro_pbc.py` **12 项**（htm 解析按位置配对 / 编码自动判定 / 年份就近配对 /
库内三条序列覆盖 / 存量量级 / **口径断点回归** / 信贷脉冲值域三重门禁 / T10YIE 历史与量级）。

**⚠ 顺带暴露的一个通用隐患：指标重定义后旧口径行会残留**

`regime.indicator_value` 是 **upsert（只写不删）**；当指标口径变更且**取值区间缩短**时，
旧口径的历史行不会被覆盖 → 残留成"看似正常的历史"。本轮实测残留 **993 行**（2017-01 ~ 2021-02、
值域高达 **+43.15**，正是旧代理口径的痕迹），与新口径（−5.5~+5.9）混在同一序列里。
→ 已按新首值日清理；`TestCreditImpulseInDb` 的**值域三重门禁 + 首值不早于 2021** 断言就是这类问题的门禁
（**正是它先报的警**）。口径变更的清单见风险表 #22。

---

## 8. 风险与对策

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| 1 | **退市股缺失（幸存者偏差已实际存在）** | 高 | 交易所退市名单回补 + 逐票试拉；补不到的进「已知缺失清单」；宽度指标做含/不含敏感性标注 |
| 2 | 免费源接口不稳定（字段/深度变化、反爬） | 高 | 先探测后开发；退避策略；`collection_api_log` 成功率监控；探测脚本定期复核 |
| 3 | 北向口径断点（2024-08） | 中 | 指标计算按日期分段；文档与指标字典标注 |
| 4 | 港股卖空历史回填不可得 | 中 | P1 探测兜底；不可得则分位改短窗口并标注（从 2026-06 起） |
| 5 | A 股估值历史仅 2018 起 | 中 | 市场级用外部长序列；个股级分位标注起点；不承诺 2015 估值可比 |
| 6 | 一致预期快照断档即不可补 | 中 | 尽早启动（P2 提前到 P1 末亦可）；断档期在 `indicator_value.extra` 标注 |
| 7 | 港股仙股污染宽度 | 中 | 流动性门槛（阈值待标定）；门槛变更须回溯重算并登记 |
| 8 | 口径漂移（多脚本多处定义） | 中 | 一切指标先登记 `indicator_dict` 再实现；单位约定见 §5.3 |
| 9 | 数据量增长 | 低 | 长表设计，量级 < 百万行/年；沿用 `cleanup.sql` 策略 |
| 10 | 合规 | 中 | 内部研究定位；对外输出需免责与资质评估（market_profile.md §11 已有） |
| 11 | ~~监控基准用港股日历，A 股专属表在「港股开市/A 股休市」日误报~~（**已于 §7.10 修复**：`market` 列 + A 股日历改读本项目 `trading_calendar`） | 中 → 已闭环 | 残余：港股通类表真实交易日是沪深港交集，单一日历无法表达（暂按 HK）；长假后首日采集器未跑前仍会短暂告警 |
| 16 | **免费源「镜像页」静默停更**（实测：社融源 `macro_china_shrzgm` 上游是商务部镜像，冻结在 2026-04；akshare 全库 85 个 `macro_china_*` 无新鲜替代） | 中 | 序列级「新鲜度自检」（本次已用「月/季序列停更筛选」发现）；停更序列在 `remark` 标注并提供替代口径（如社融 → `CN.NEW_LOAN`）；换源（央行官网直连）列 P1 |
| 17 | **源侧日期格式脆性**（实测：`统计时间="2026.8"` 点分隔不被识别 → 用电量被压成「一年一条」，看起来却像年度数据） | 中 | 解析器必须覆盖源侧全部格式 + **回归测试**（`tests/test_macro_monthly_parse.py`）；行数/月数比自检（`n / 月数 ≤ 1.6`）作为异常兜底 |
| 15 | **行业分类口径不统一**：同花顺行业资金流（≈90 细分）≠ 申万一级（31）≠ 东财板块（另有一套） | 中 | 本层先用市场级聚合（不跨表 join）；逐行业 RS×资金流 需先建映射表（P2），禁止按名称模糊匹配 |
| 14 | **常驻服务可能"设计了但没跑"**（实测：`monitor_collector.py` 有 60s 常驻循环，但无 plist、无进程 → 所有停滞判断实际无人执行，直至本轮回补 `com.equity.a-monitor`） | 高 | 凡声称常驻的服务，核查三件事：plist 存在、`launchctl list` 有 PID、日志有周期输出；纳入交付验收 |
| 12 | **采集器依赖本机开机**（长假/休眠期间任务不跑：实测 10-01~10-03 全部任务停摆，HK 在 10-01/02 开市因而真实缺数据） | 高 | 逐日快照类（一致预期/事件）断档即永久缺失 → 迁服务器常驻；本机期间监控会正确报出落后，不可当作误报忽略 |
| 13 | 事件源单位/口径不一致（实测：cninfo 北交所「变动数量」失真 79 亿股级、`change_ratio` 口径未校准） | 中 | **入库前做跨市场/量级抽检**（本轮借此发现并排除 BJ）；异常值不静默使用，先存疑再处置 |
| 18 | **官方源前置 WAF**：NBS「国家数据」每新会话首次调用返回 HTML 挑战页（200，非 JSON）；重试若重建会话则**永远卡在第一次** | 中 | 已实现「主动握手 + 同会话重试」（§7.17 坑 1）；其它官方源接入时沿用此模式，勿用「失败即重建会话」的朴素重试 |
| 19 | **数据源自身的覆盖边界会被误判为「不可得」**：核心 CPI 被误记「免费源不可得」达数日，实际是**源库仅 2021 起**该口径 + 旧接口被封 | 中 | **改用「换前端/换接口」而非「换数据源」**再判定不可得；结论须写明「已验证的接口清单」（§7.17 已是此写法） |
| 20 | **PIT 前视的第二种成因：不是数据不可回溯，而是 lag 口径换算错**。实测：OECD CLI 上月读数在**次月初**发布（≈ 月初+37 天），若按「月末+8 天」写成 `lag=15`（基准是月初）→ 数据被**提前约 3 周**视为可得 | **高** | 新源一律把「实测发布滞后」**换算成距 `period_date`（月初）的天数**再写 `lag`；每个新源加一条库内回归测试 `release_time ≥ period_date + lag`（§7.18 已示范） |
| 21 | **同一序列被多个脚本写、且表无 source 列**（`daily_benchmark` 被 3 个脚本写；实测 US.DGS10 与 FRED 逐日一致、暂无冲突） | 低 | 明确「每小时增量脚本 = 唯一日常权威源，其余仅一次性回填」；若未来出现值分歧，再给表加 `source` 列（详见 §7.22） |
| 22 | **指标重定义后旧口径行残留**（`indicator_value` 只写不删；信贷脉冲换口径后残留 993 行旧值、值域高达 +43 与新口径 −5.5~+5.9 混存） | 中 | 口径变更时按**新首值日**清理旧行；**值域 + 首值断言入测试**（本轮即由测试先发现）；必要时再做「按指标重建」工具 |

---

## 附录 A：探测脚本（已落地为 `_probe_macro_sources.py`，P0-1 完成）

> **状态：已实现并实跑完成（2026-09-30）**，结果见 §4.4 与 `doc/macro_source_probe_report.md`。
> 实际脚本相对下方设计骨架的增强：① 候选接口名/参数已按本机 akshare 1.18.60 实测签名逐条核对；
> ② 支持 `kwargs_list` 多窗口探测（用于界定历史起点，国债/南向即靠它得出结论）；
> ③ SIGALRM 单接口超时保护（默认 60s，可 `--timeout` 调整）；④ 两个专项探测 `credit_symbols`（信用债品种枚举）与
> `northbound_check`（北向字段逐年完整性，用于定位断点）；⑤ `--layer/--only/--write` 过滤与报告输出。
> **以下骨架仅作设计留档，一切以脚本为准。**

```python
#!/usr/bin/env python3
"""候选数据源探测（只读，不落库）——市场状态分析采集层 P0 第 1 步。

用法:
    python3 _probe_macro_sources.py                 # 探测全部
    python3 _probe_macro_sources.py --only CN.BOND10Y,CN.CPI_YOY
    python3 _probe_macro_sources.py --write doc/macro_source_probe_report.md

产出: 控制台报告（每接口: 可用性 / 返回行数 / 历史首日 / 最新日 / 字段清单）
"""
import argparse
import sys
import traceback
from datetime import date

# ── 候选清单（series_code → 探测方式）────────────────────────────────────
# kind: akshare / url / manual；ak_func=AKShare 函数名；kwargs=探测参数
CANDIDATES = [
    # ① 宏观
    dict(series="CN.BOND10Y",   desc="中债10年期国债收益率", kind="akshare",
         ak_func="bond_china_yield", kwargs=dict(start_date="2002-01-01", end_date=str(date.today()))),
    dict(series="CN.CREDIT_SPREAD", desc="信用利差(AAA中短票-国债)", kind="akshare",
         ak_func="bond_china_close_return", kwargs=dict()),
    dict(series="CN.SHRZGM",    desc="社会融资规模存量", kind="akshare",
         ak_func="macro_china_shrzgm", kwargs=dict()),
    dict(series="CN.MONEY_SUPPLY", desc="M0/M1/M2 货币供应", kind="akshare",
         ak_func="macro_china_money_supply", kwargs=dict()),
    dict(series="CN.PMI",       desc="制造业 PMI", kind="akshare",
         ak_func="macro_china_pmi", kwargs=dict()),
    dict(series="CN.CPI",       desc="CPI 同比", kind="akshare",
         ak_func="macro_china_cpi", kwargs=dict()),
    dict(series="CN.PPI",       desc="PPI 同比", kind="akshare",
         ak_func="macro_china_ppi", kwargs=dict()),
    dict(series="HK.HIBOR",     desc="HIBOR 隔夜/1M", kind="akshare",
         ak_func="rate_interbank", kwargs=dict(market="香港银行同业拆借市场", symbol="HIBOR", indicator="隔夜")),
    dict(series="CMDTY.COPPER", desc="沪铜主力", kind="akshare",
         ak_func="futures_zh_daily_sina", kwargs=dict(symbol="CU0")),
    dict(series="CMDTY.OIL",    desc="WTI 原油(FRED DCOILWTICO)", kind="akshare",
         ak_func="macro_usa_...", kwargs=dict()),   # 待定：或复用 FRED 路径

    # ② 资产配置
    dict(series="CN.VAL_PE",    desc="全A PE 中位数(长历史)", kind="akshare",
         ak_func="stock_a_ttm_lyr", kwargs=dict()),
    dict(series="CN.VAL_PB",    desc="全A PB(长历史)", kind="akshare",
         ak_func="stock_a_all_pb", kwargs=dict()),

    # ③ 市场状态
    dict(series="CN.IVIX",      desc="50ETF 期权波动率指数", kind="akshare",
         ak_func="index_option_50etf_qvix", kwargs=dict()),
    dict(series="CN.NORTHBOUND", desc="北向成交额(东财页面)", kind="url",
         url="https://data.eastmoney.com/hsgt/index.html"),
    dict(series="CN.LIMIT_POOL", desc="涨停/跌停池", kind="akshare",
         ak_func="stock_zt_pool_em", kwargs=dict(date="20260929")),
    dict(series="CN.IPO",       desc="新股/IPO 汇总", kind="akshare",
         ak_func="stock_xgsglb_em", kwargs=dict()),
    dict(series="CN.FUND_NEW",  desc="新成立基金", kind="akshare",
         ak_func="fund_new_found_em", kwargs=dict()),
    dict(series="CN.ACCOUNT",   desc="新增投资者开户数", kind="akshare",
         ak_func="stock_account_statistics_em", kwargs=dict()),
    dict(series="CN.UNLOCK",    desc="限售解禁排期", kind="akshare",
         ak_func="stock_restricted_release_queue_em", kwargs=dict(symbol="全部股票")),
    dict(series="CN.INSIDER",   desc="高管/股东增减持", kind="akshare",
         ak_func="stock_hold_management_detail_em", kwargs=dict()),
    dict(series="HK.SHORT_HIST", desc="港股卖空历史(HKEX 归档)", kind="url",
         url="https://www.hkex.com.hk/eng/stat/smstat/dayquot/d260929e.htm"),
    dict(series="CN.DELIST",    desc="沪深退市股名单", kind="akshare",
         ak_func="stock_info_sh_delist", kwargs=dict()),

    # ④ 行业
    dict(series="CN.SW_INDEX",  desc="申万一级行业指数日线", kind="akshare",
         ak_func="index_hist_sw", kwargs=dict(symbol="801010", period="day")),
    dict(series="CN.INDUSTRY_FLOW", desc="行业板块资金流", kind="akshare",
         ak_func="stock_sector_fund_flow_rank", kwargs=dict(indicator="今日", sector_type="行业资金流")),
    dict(series="CN.FORECAST",  desc="东财盈利预测(一致预期近似)", kind="akshare",
         ak_func="stock_profit_forecast_em", kwargs=dict()),
]


def probe_akshare(cand: dict) -> dict:
    """探测 AKShare 接口：行数 / 起止 / 字段。"""
    import akshare as ak
    func = getattr(ak, cand["ak_func"], None)
    if func is None:
        return {"ok": False, "err": f"AKShare 无此接口: {cand['ak_func']}"}
    try:
        df = func(**cand.get("kwargs", {}))
        if df is None or len(df) == 0:
            return {"ok": False, "err": "返回空 DataFrame"}
        return {
            "ok": True, "rows": len(df), "cols": list(df.columns)[:20],
            "head": df.head(1).to_dict("records"), "tail": df.tail(1).to_dict("records"),
        }
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def probe_url(cand: dict) -> dict:
    """探测网页源：状态码 + 是否含关键数据。"""
    import requests
    try:
        r = requests.get(cand["url"], timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        return {"ok": r.status_code == 200, "status": r.status_code, "bytes": len(r.content)}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的 series_code 过滤")
    ap.add_argument("--write", default="", help="报告输出路径(markdown)")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    targets = [c for c in CANDIDATES if not only or c["series"] in only]

    lines = ["| series | 说明 | 结果 | 行数 | 字段/详情 |", "|---|---|---|---|---|"]
    for cand in targets:
        try:
            r = probe_akshare(cand) if cand["kind"] == "akshare" else probe_url(cand)
        except Exception:
            r = {"ok": False, "err": traceback.format_exc(limit=1)}
        flag = "✅" if r.get("ok") else "❌"
        detail = r.get("cols") or r.get("err") or r.get("status")
        lines.append(f"| {cand['series']} | {cand['desc']} | {flag} | {r.get('rows', '-')} | {detail} |")
        print(f"{flag} {cand['series']:18s} {cand['desc']}: {detail}")

    if args.write:
        with open(args.write, "w", encoding="utf-8") as f:
            f.write("# 候选数据源探测报告\n\n"
                    f"> 生成时间: {date.today()}；本报告为 `_probe_macro_sources.py` 输出，"
                    f"仅记录可用性/深度/字段，不代表已开发。\n\n")
            f.write("\n".join(lines) + "\n")
        print(f"\n报告已写入 {args.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**探测后需人工判定的三件事**：① 历史首日是否达到承诺深度（国债 ≤2015、估值 ≤2015、行业指数 ≤2015）；② 字段单位与现有表口径是否冲突（如收益率是 % 还是 bp）；③ 是否有反爬/限流（连续请求是否会封）。

---

## 附录 B：一次性回填任务清单

| # | 目标 | 脚本（复用/新建） | 命令示例 | 验收 |
|---|---|---|---|---|
| 1 | ~~南向持股 → 2017~~ **已确认不可行**（东财源仅近 1 年，2021–2024 窗口实测全空） | — | — | 改为持续日频采集（现行 `get_south_flow.py`）+ P1 调研港交所 CCASS 源 |
| 2 | **恒指 → 2015（实测可到 2013-08）** | 复用 `collect_benchmark_history.py` / `backfill_benchmark.py`（sina 源） | `python3 collect_benchmark_history.py` | `HK.800000` 起点 ≤ 2015（目标 2013-08） |
| 3 | FRED 长历史（SP500/VIX/美元指数 → 1990s） | 复用 `collect_benchmark_history.py` | 同上 | `US.SP500` 起点 ≤ 2000 |
| 4 | **中国国债收益率 → 2006** | ✅ 已落地 `get_cn_bond.py --backfill`（按年分段，2005 起逐年试） | `python3 get_cn_bond.py --backfill` | ✅ 起点 **2006-03-01**（超预期，脚本名与原草案不同） |
| 5 | 市场级估值长序列 → 2005（**月频**） | ✅ 已落地 `get_macro_valuation.py` | `python3 get_macro_valuation.py` | ✅ 起点 2005-01-31（261 行，freq=month） |
| 6 | 港股行情 2015–2018 | 待办（复用 `backfill_hk_market_turnover.py` 试更早起点） | `python3 backfill_hk_market_turnover.py --start 2015-01-01` | 目标 `min(trade_date) <= 2015-01-05`（不可得则明示降级） |
| 7 | **申万一级行业指数 → 1999** | ✅ 已落地 `backfill_sw_industry.py`（31 行业） | `python3 backfill_sw_industry.py` | ✅ 31 个 `CN.SW*` 序列、143,530 行、起点 1999-12-30 |
| 8 | ~~港股卖空历史~~ **未解决**（港交所日报归档 404） | — | — | P1 调研月报/其他归档；不可得则按 2026-06 起 + 短窗口分位标注 |
| 9 | 退市股清单（+ 历史行情另立） | ✅ 名单已落地 `backfill_delisted.py`（沪 159 / 深 208 → 361 条） | `python3 backfill_delisted.py` | ✅ `stock_info.delist_date` 非空 361 条；**历史行情试补仍待办** |
| 10 | **交易日历** | ✅ 已落地 `init_trading_calendar.py`（CN 预排 + HK 派生） | `python3 init_trading_calendar.py` | ✅ CN 8798 行（1990-12 ~ 2026-12）+ HK 1906 行 |
| 11 | A 股市场成交额已全历史 | 已具备（`backfill_a_market_turnover.py`） | — | 无需动作 |
| 12 | 北向净流入历史（2014-11 ~ 2024-08-16） | ✅ 已重写 `get_northbound.py`（含断流保护） | `python3 get_northbound.py --backfill` | ✅ 2264 行；断点后不写脏数据并打印提示 |
| 13 | 恒指 → 2013 / FRED 长历史 | ✅ 扩了 `collect_benchmark_history.py`（新增 `hk_sina` 源 + FRED 1990 起） | `python3 collect_benchmark_history.py HK.800000` 等 | ✅ 恒指 2013-08 起；NASDAQ/VIX 1990 起；SP500/DJIA 受源限制 2016-09 起 |

---

## 附录 C：不新建清单（避免重复建设）

| 需求 | 已有载体 | 说明 |
|---|---|---|
| 宽基/行业指数日线 | `daily_benchmark` | 扩展 `bench_code` 命名空间，不建新表 |
| 涨跌停 / 破净 / 宽度 / 估值分位 | `regime.indicator_value`（**自算**） | 计算层职责，不进采集清单 |
| 全市场成交额、两融、南向、卖空、回购、CBBC | 现有表 | 只补历史深度，不重建 |
| 个股行情/估值 | `a_daily_quote` / `hk_daily_quote` | 只在需要时补 `2018` 前估值（走市场级替代） |
| 宏观环境评分（个股维度） | `macro_environment_score` | 已在库，调度暂注释；纳入计算层时再启用 |
| 采集监控/日志/告警 | `monitor_*` / `collection_*` | 新表新任务登记即可 |
| 个股画像 | `profile.*` | 第五层，本方案不触碰 |

---

*本文档为采集层落地方案；实施进展应回写本文档「§7 分期」对应行的验收列，避免方案与实况漂移。*

*变更记录：*
- *v2.10（2026-10-09）：**社融换源 + 信贷脉冲修复 + 通胀预期落地**（详见 §7.24）——① 社融改抓**央行官网**「存量/增量统计表」htm 附件（`CN.TSF_STOCK`/`CN.TSF_STOCK_YOY`/`CN.TSF_FLOW_M`，2016-01 起），替代冻结在 2026-04 的商务部镜像；踩坑三处：附件编码新旧不一（2016-2019 UTF-8 / 2020+ GBK）、宽表须**按位置**配对、**2018-12→2019-01 有 20% 口径断点**（央行纳入国债/地方债）；② `MACRO.CREDIT_IMPULSE` 从「增量代理」换为**真值口径**（流量/GDP 比率的同比变化，Biggs/Mayer），**口径试错三次**才对（一阶导常年 +10pp 无周期含义、月度变化仅 ±1.5pp 噪声主导），最终 −5.5~+5.9pp 且与共识叙事一致，值域三重门禁写进测试；③ 通胀预期 `US.T10YIE`（FRED 2003 起）+ `MACRO.BREAKEVEN_10Y` 补上 §3.3 最后一个缺口；④ 顺带修 `indicator_dict.remark` varchar(200) 超长打断整轮计算的问题（按列宽截断）。新增 `tests/test_macro_pbc.py`（12 项）。*
- *v2.9（2026-10-09）：**§3.4 外部环境 + §5.4 跨资产风险偏好落地**（详见 §7.23）——复用 `get_global_benchmarks` 每小时的既有采集（**零新增采集**），新增 7 个计算层指标：`MACRO.DXY`/`MACRO.DTWEXBGS`（美元双口径）、`MACRO.FED_FUNDS`（EFFR）、`RISK.VIX`、`MACRO.SPX_MOM20`/`MACRO.COPPER_MOM20`/`MACRO.GOLD_MOM20`（跨资产动量，新增「跨资产」维度）；**跨资产风险偏好指数（7 成分等权）入库填充 `market_regime_daily.risk_appetite`**（此前该列从未写过值）：VIX/美元/AAA 利差/AA−AAA 等级利差(取反) + 标普/铜/黄金动量，双市场均写；工程要点：动量在**源市场日历**上算、`merge_asof` 两侧时间精度必须统一（实测报错）；校验含「与 VIX 负相关、与股指动量正相关」方向测试；新增 `tests/test_global_risk.py`（8 项）。*


- *v2.8（2026-10-09）：**`get_global_benchmarks.py` 复用审计 + 汇率口径订正 + 季度 GDP 节点核对**（详见 §7.22）——① **口径订正**：`FX.USDCNY` 源为 `currency_boc_safe`（**在岸**中行折算价，实测与本币值 6.733 完全一致），原标「离岸人民币」有误，已修 3 处标签（两脚本 + 打分模板）并改库内 8,061 行 `bench_name`；**CNH 仍无稳定免费源**（东财断连 / 中行新浪停更 2023-11 / CFETS 为在岸）。② **复用清单**：17 个基准中仅 `US.DGS10`/`US.DGS2` 已被计算层消费；VIX/DXY/EFFR/全球指数等**已在跑但未用**，可直接补 §4.2 跨资产与 §5.4 风险偏好（零采集成本）。③ **重复写入核查**：3 脚本写同码且表无 source 列，但实测 `US.DGS10` 与 FRED **逐日一致（0.0bp）**→ 无冲突，登记风险 #21。④ **季度 GDP**：「国民经济核算 → 国内生产总值」的**不变价当季值**（2007Q1+，未季调）经实测**不用于产出缺口**——4 季滚动和会把 2020 疫情低点从 −9.55% 摊平到 −3.53%，故保留已季调环比链口径；顺带采集 `CN.GDP_REAL_Q` 作水平参考。*


- *v2.7（2026-10-09）：**产出缺口 + 美林投资时钟落地**（详见 §7.21）——新增采集 `CN.GDP_QOQ`（NBS 已季调环比，2011Q1 起 62 季）与 `CN.GDP_YOY_Q`（单季实际同比，**1993Q1 起 134 季**）；计算层新增 `MACRO.OUTPUT_GAP`（水平口径，**单边** HP λ=1600 → 缺口 2014Q1 起）与 `MACRO.CLOCK_QUADRANT`（分类值 1-4，**不分位**，新增 `NO_PERCENTILE` 契约）；**PIT 收紧**：季度 lag 100 → **110**（实测季后次月 15-20 日发布，原值提前 5-10 天视为可得），含订正既有 `CN.GDP`/`CN.GDP_YOY`；季度接口特性：期间码 `202603SS`、`dts` **必须逐期枚举**（区间串一律 500）；HP 用 scipy 稀疏解自实现（环境无 statsmodels）；验证含 HP 线性自检与「缺口极值落在 2020 疫情/2021 复苏」；新增 `tests/test_regime_clock.py`（10 项）。*
- *v2.6（2026-10-09）：**§3.1 剩余缺口收口**（详见 §7.20）——用户给四个目录路径，逐项处置：`CN.PMI_NEW_ORDERS`（**2005-01 起 261 月**，akshare 无分项故走 NBS）、`CN.INDUSTRIAL_PROFIT_YOY`（**2000-02 起 265 月**，⚠ 累计口径）、`CN.M0_YOY`（同表已有列，NBS 表晚一个月故仍用 akshare）；**货运量/M1M2 经 NBS 逐月校验一致（平均差 0.02pp）→ 不重复采**，并**订正**「工业利润源不可得」的旧结论。采集层修三处：指标匹配改**前匹配**（`货运量_同比增长` 是 `铁路货运量_同比增长` 的子串，包含匹配会混入 4 个分品种）、零命中改为**报错**（不再静默空序列）、加树/指标清单缓存在（日请求 ~20 → ~8）。跨源互证 PMI 新订单 vs 总指数相关 **0.983**。测试 +6 项。*
- *v2.5（2026-10-09）：**发电量落地**（详见 §7.19）——按用户给的路径（月度数据 → 能源 → 能源主要产品产量）走 NBS 新版接口，新增 `CN.POWER_GEN_YOY`（**2000-02 起 280 个月**）；发现并防住「发电量与火力/水力/核能/风力/太阳能发电量是**平级**叶子」的取数事故（`leaf_prefix` 必须前匹配，已加专项测试）；**跨源交叉验证揭示真实口径差**（规上发电量不含分布式光伏与自备电厂 → 2026-08 与全社会用电量背离 5.1pp、近 60 月相关仅 0.47），已写明「实体活跃度优先看用电量」；**发现并记录 NBS「1–2 月合并发布」固有缺口**（缺 39 个月中 97% 为 1/2 月；同类序列用电量/货运量同样存在，已补进三条 remark 并加缺口模式回归测试）；顺带**实测确认**另两个 §3.1 缺口的可得性（工业企业利润 = `利润总额累计增长(%)` 累计口径；PMI 新订单 = `新订单指数(%)`），尚未采纳。测试 +6 项。*
- *v2.4（2026-10-08）：**OECD 综合领先指标 CLI 落地**（详见 §7.18）——用户给的 `data-explorer.oecd.org` 页面背后是 **OECD 官方 SDMX REST**（`sdmx.oecd.org`，无鉴权/无 WAF），一次请求取回 7 个经济体；新增 `CN.CLI`（**1992-05 起 413 个月无缺口**）/`US.CLI`/`JP.CLI`/`DE.CLI`/`KR.CLI`/`GLOBAL.CLI_G7`/`GLOBAL.CLI_G20` + 计算层 `MACRO.CLI_CN`/`MACRO.CLI_GLOBAL`；**自查出并修掉一个前视错误**（`lag` 基准是月初不是月末，误设 lag=15 会把数据提前约 3 周视为可得 → 改回 45 并加 PIT 测试）；`_mk_row` 的 `market` 由硬编码 CN 改为按 code 前缀派生（既有序列行为不变）；新增 `tests/test_macro_oecd.py`（9 项）。*
- *v2.3（2026-10-08）：**核心 CPI 落地 + 纠错**（详见 §7.17）——国家统计局新版「国家数据」接口（`/dg/website/publicrelease/web/external/**`）纯 requests 可直连，官方口径名「不包括食品和能源居民消费价格指数(上年同月=100)」→ 新增 `CN.CORE_CPI_YOY`（**2021-01 起 68 个月**，源库分类标准的结构性边界）+ 计算层 `MACRO.CORE_CPI_YOY`；**§7.16 的「核心 CPI 免费源不可得」结论作废**（错因：只试了老接口 `easyquery.htm`，实测被 WAF 403）。修 3 个采集坑：WAF 首调挑战页（须同会话重试+主动握手）、`dts=""` 只返回 9 个月（须显式区间）、同一指标被切成多时段目录（须遍历合并）；新增 `tests/test_macro_nbs.py`（12 项）。*
- *v2.2（2026-10-08）：**§3 通胀与信用维度补齐**（详见 §7.16）——AA 中短票曲线（独立接口 + 3 日窗重试）→ `RISK.CREDIT_SPREAD_AA` + **等级利差** `RISK.GRADE_SPREAD`（入 FSI）；通胀高频：菜篮子（2005 起日频）+ 猪价（7.5 月窗）+ `CN.CPI_MOM`；**核心 CPI 确认免费源不可得**（NBS 直连被挡 + CPI 接口无核心分项）并给出替代组合；新增 4 项测试 → 全量 209 项通过。*
- *v2.1（2026-10-08）：**§3 宏观层补齐**（详见 §7.15）——新增**跨表派生**（中美利差 `CN.US_SPREAD_10Y`、美债期限利差 `US.TERM_SPREAD_10Y_2Y`）+ 2 个双市场指标（FSI 纳入美债期限利差）；修复**用电量被压成年度**的日期解析事故（点分隔格式，月度 240 行恢复）+ 新增**货运量**月度序列（291 行）与采集器行过滤能力；更正「GDP 停更」误判（实为累计口径标记）；确认社融为**源侧停更**并在序列标注；新增 `tests/test_macro_monthly_parse.py`（9 项）→ 全量 205 项通过。*
- *v2.0（2026-10-04）：**P2 行业估值快照落地**（详见 §7.14）——新增 `regime.sector_valuation_snapshot`（源无历史 → 逐日累积）+ `get_sector_valuation.py`（18:15）+ 2 个市场级估值指标（PE/PB 中位数，逐行业明细供 join）+ 3 项契约测试（含「指标 = 快照中位数」交叉校验）。调度 115 → **116 任务**；监控 CN 表 13 → 14 张。*
- *v1.9（2026-10-04）：**P2 逐行业层落地**（详见 §7.13）——新增 `regime.sector_mapping`（90 个同花顺行业 → 申万一级，**覆盖全部 31 个**；方法经三轮演进：原始相关性 ❌ → 超额相关性 → **规则为主 + 相关性交叉校验**，并按实测证据论证「相关性不能当映射依据」）+ `regime.sector_daily`（15.5 万行，1999-12-30 起，含 `rs_20d`/排名/映射汇总资金流）；月度映射复核任务（115 任务）；3 张表登记 CN 监控；新增 5 项不变量测试（含资金流守恒）。*
- *v1.8（2026-10-04）：**P1 行业层落地**（详见 §7.11）——申万一级行业指数补 18:10 增量调度（源无日期参数 → 只 upsert 尾部）、东财断连后**换同花顺源**建 `regime.sector_fund_flow`（15:35 收盘后采集）、新增 6 个行业指标（不入合成）；顺带修掉「Timestamp 索引导致 CN 全历史重算 10 块全失败」的 bug（已加防御 + 回归测试）；新增 §7.12 交付自检口径；任务数 112 → **114**。P1 采集+计算主体收口，仅剩北向/南向/卖空替代源与退市股行情试补。*
- *v1.7（2026-10-04）：**监控日历基准修复 + 监控服务常驻**（详见 §7.10）——`monitor_table_config` 新增 `market` 列、10 张 A 股专属表按 A 股日历判定；A 股日历改读本项目 `public.trading_calendar`（此前 `is_trading_day('CN')` 会静默回落港股）；补 `com.equity.a-monitor` launchd 常驻服务（此前监控**无人执行**）；新增 `tests/test_monitor_market_calendar.py`；A 股误报清除、港股真告警保留。*
- *v1.6（2026-10-04）：**P1 情绪事件包落地**（详见 §7.9）——新增 4 张表（`regime.ipo_event` / `fund_issuance_event` / `unlock_schedule` / `insider_trade`）+ `get_market_events.py`（4 子采集）；解禁改用区间明细接口并**回填 2016 起历史**（22,648 行）；采集中发现并处置 3 个源侧数据质量问题（重复行 / 北交所量纲失真 / 基金份额滞后）；计算层新增 7 指标、温度计与风险指数各纳入 2 成员并**完成 CN 全历史重算**；新增 `tests/test_event_indicators.py`（12 项，含 PIT 专项）；§6.2 任务时刻表按实测校正（新增「市场事件采集」19:30，共 112 任务）；风险表补 3 条（监控日历基准、本机开机依赖、事件源口径抽检）。*
- *v1.5（2026-09-30）：**P1 一致预期快照落地**（详见 §7.8）——新增 `regime.analyst_forecast_snapshot`（按真实源改为宽表，行数为长表方案的 1/4）+ `get_analyst_forecast.py`；计算层接入 `FORECAST.REVISION_BREADTH`（次日有值）与 `FORECAST.RATING_BULL_PCT`（已标注判别力弱）；调度新增 18:40 任务并重启（111 任务）；修复源侧重复返回与北交所代码前缀误判两个采集问题。*
- *v1.4（2026-09-30）：**P1 宏观包落地**（详见 §7.7）——新增 `get_macro_monthly.py`（20 序列 + 2 派生）/ `get_macro_daily.py`（拆借利率/商品/4 个利差派生）；计算层接入 4 个宏观指标并升级合成（**FSI 信用维度补齐**）与季节规则 v2；月频→交易日改为 `merge_asof` 的 PIT 填充；调度新增 2 个任务并修正「宏观必须先于计算」的顺序依赖。*
- *v1.3（2026-09-30）：**P1 计算层落地**（详见 §7.6）——新增 `market_state_daily.py`（22 指标 + 3 合成 + 季节 + 顶底信号）、`regime.market_regime_daily` 建表、调度任务「市场状态计算」（18:45，已重启生效/108 任务）、regime 三表监控转 active；回填 CN 8,737 日 / HK 1,906 日；同步修正分位口径注释（原始值分位）并记录 3 项数据质量问题（numeric NaN 污染、B 股筛选 bug、窗口计算性能）。*
- *v1.2（2026-09-30）：**P0-2 落地**（详见 §7.5）——新增 6 个脚本/模块 + `regime` schema 4 表 + 3 个调度任务（已重启调度器生效）；同步修正北向「持股市值」口径（实测为季度披露）、§4.3/§4.4 相关行、§6.2（草案 → 已登记）、附录 B（→ 落地状态）；并修复 3 个阻塞验收的既有缺陷（monitor data_stale 停摆、`db.py` 不支持 schema.table/JSONB、`_schema_diff` 视图正则）。*
- *v1.1（2026-09-30）：P0-1 探测完成（39 候选 → 32 可用），实测推翻了 3 项原计划（南向回填不可行、卖空历史未解决、北向需换源），已同步修订 §4.2 数据源矩阵、§4.3 风险、§4.4（新增结论速查）、§6 调度、§7.1 P0、附录 B。*
- *v1.0（2026-09-30）：首版，含实况盘点、存储 DDL 草案、调度草案、分期路线。*
