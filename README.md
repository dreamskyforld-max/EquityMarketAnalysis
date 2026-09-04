# EquityMarketAnalysis

以**港股为主、A股为辅、含全球基准**的行情数据采集 + 流动性/资金流分析 + 量化回测平台。

- 实时盘中行情来自 **富途 FutuOpenD**（逐笔 / 报价 / 快照）
- 盘后与日频数据来自 **东方财富 / 新浪 / AKShare / 港交所 / FRED / 恒生官网** 等免费源
- 落库 **PostgreSQL**（`market_db`），由常驻调度器自动采集，结果通过**企业微信**推送，并供可视化看板消费

> 本仓库是**数据采集与分析后端**。可视化看板（Streamlit）在独立仓库 `stock-dashboard` 中维护。

---

## 目录

- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [目录结构](#目录结构)
- [环境要求](#环境要求)
- [安装步骤](#安装步骤)
- [配置说明](#配置说明)
- [使用示例](#使用示例)
- [部署](#部署)
- [贡献指南](#贡献指南)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 功能特性

- **多源行情采集**：港股 / A股 / 全球指数 / 南向资金 / 沽空 / 牛熊证 / 回购 / 融资余额 / 财务 / 板块成分 / 相关性 / 宏观评分。
- **盘中实时链路**：富途 TICKER / QUOTE 订阅，逐笔写入 `tick_data` 与 `realtime_order_size`，含断线重连与订阅健康检查。
- **收盘全量任务**：港股 16:20 / A股 15:10 自动批量落库。
- **三层资金流分析**：市场整体 → 板块迁徙 → 个股排行 → 情绪佐证（沽空 / 回购）。
- **量化分析**：方向回测、多因子、订单行为、相位分析、支撑阻力、阈值、OFI 短周期、Tick 分布等。
- **企业微信推送**：机器人（Bot A/B + Webhook）采集与告警。
- **可观测性**：独立常驻看门狗（`monitor_collector.py`）埋点 + 企微告警，与调度器解耦避免同源单点。

---

## 系统架构

```
            ┌──────────────────────── 数据源 ────────────────────────┐
            │  富途 FutuOpenD(实时)   东方财富/新浪/AKShare(盘后)        │
            │  FRED / 恒生官网 / 港交所 / yfinance                    │
            └────────────────────────────┬───────────────────────────┘
                                         │
                          ┌──────────────▼───────────────┐
                          │  collector_runtime (共享上下文) │
                          │  富途 OpenQuoteContext 加锁串行 │
                          └──────────────┬───────────────┘
                                         │
                 ┌───────────────────────┼───────────────────────┐
                 ▼                       ▼                       ▼
        实时盘中采集(逐笔/报价)      收盘全量采集(get_*)      历史回填(backfill_*)
                 │                       │                       │
                 └───────────────────────┼───────────────────────┘
                                         ▼
                            PostgreSQL  market_db  (~21 表)
                                         │
                 ┌───────────────────────┼───────────────────────┐
                 ▼                       ▼                       ▼
        market_scheduler(APScheduler)  liquidity/* 分析    企微推送 / 看板
                 │                                               (stock-dashboard)
        monitor_collector (看门狗告警)
```

**核心数据流约定**：所有入库脚本实现统一的 `run(codes=None, ctx=None)` 入口，由 `market_scheduler` 通过共享 `collector_runtime` 常驻调用，避免每分钟 `subprocess` 冷启动。

---

## 目录结构

```
EquityMarketAnalysis/
├── config.py / config.conf / config.example.conf   # 统一配置加载（config.conf 不入库）
├── db.py                                            # PostgreSQL 连接 + upsert / bulk_upsert
├── collector_runtime.py                             # 进程内共享富途上下文（加锁串行 + 重连）
├── market_scheduler.py                              # APScheduler 常驻调度（从 stock_info 自动生成任务）
├── monitor_collector.py                             # 独立看门狗（企微告警 + 埋点）
├── get_*.py / collect_*.py                          # 各采集脚本（统一 run(codes, ctx) 约定）
├── backfill_*.py / gen_backfill.py                  # 历史数据回填
├── liquidity/                                       # 三层资金流分析（market / sector / stock / report）
├── {analysis}*.py                                   # 量化分析：方向回测 / 多因子 / 支撑阻力 / OFI ...
├── sql/schema.sql                                   # 全量表结构（当前 33 张表）；sql/cleanup.sql 清理过期
├── system/                                          # systemd 单元(Linux) + macOS launchd plist
├── bootstrap.sh                                     # 服务器一键部署（幂等，从官方源安装）
├── deploy.sh                                        # 本地 → 生产服务器增量推送（rsync + 重启）
├── tests/                                           # pytest（db / get_buyback / get_cbbc / get_south_flow / scheduler / wecom）
└── trend_cache/                                     # 趋势 JSON 缓存 + qrobot_plugin.py（企微插件）
```

---

## 环境要求

| 组件 | 版本 / 说明 |
|------|------------|
| Python | **3.10+**（推荐 3.12；依赖 `pandas==3.0.2` 需 ≥3.10） |
| 数据库 | PostgreSQL 12+（`market_db`） |
| 实时行情 | 富途 **FutuOpenD** 网关（本地或服务器，端口 `11111`） |
| 操作系统 | 开发：macOS；生产：OpenCloudOS 9 / RHEL9 系（由 `bootstrap.sh` 用 `dnf` 安装） |
| 第三方 API | 可选：FRED API Key、恒生官网、企业微信 Bot/Webhook Key（见[配置说明](#配置说明)） |

> ⚠️ 富途 LV1 免费行情额度有限：QUOTE 实时报价订阅需显式指定少量有权限的代码（`config.conf [ticker] quote_stocks`）。逐笔（TICKER）主链路不受影响。

---

## 安装步骤

### 1. 克隆仓库

```bash
git clone <your-repo-url> EquityMarketAnalysis
cd EquityMarketAnalysis
```

### 2. 创建虚拟环境并安装依赖

```bash
# 使用项目自带的虚拟环境目录 .venv（已被 .gitignore 忽略）
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

> 若需固定离线环境，依赖已锁定版本（见 `requirements.txt`，含 `futu-api`、`akshare`、`psycopg2-binary`、`APScheduler`、`wecom-aibot-sdk` 等）。

### 3. 配置数据库连接与密钥

复制配置模板，并填入真实值（**`config.conf` 不入库，已被 `.gitignore` 忽略**）：

```bash
cp config.example.conf config.conf
```

必填项（详见[配置说明](#配置说明)）：

- `[database]`：PostgreSQL 连接（host / port / dbname / user / password）
- `[futu]`：FutuOpenD 地址（默认 `127.0.0.1:11111`）
- 可选：`[fred]` FRED Key、`[wecom]` / `[wecom_webhook]` 企微、`[sync_server]` 同步服务器

支持**环境变量覆盖**：`DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD`，以及 `.env`（如 `FRED_API_KEY`）。

### 4. 初始化数据库

```bash
# 需先创建数据库 market_db（名称可改，与 config.conf [database] dbname 对应）
createdb market_db            # 或用 psql: CREATE DATABASE market_db;

# 执行建表语句（约 21 张表）
psql -d market_db -f sql/schema.sql
```

### 5. 启动 FutuOpenD 网关

- 安装并运行富途 OpenD（本地开发默认 `127.0.0.1:11111`；服务器由 `bootstrap.sh` 渲染 `FutuOpenD.service` 常驻）。
- 确保已登录行情账户且 LV1 行情权限可用。

### 6. 验证安装

```bash
# 连接与 upsert 写入自检（需要数据库可用）
python3 -m pytest tests/test_db.py -q

# 单独跑一次行情快照（不依赖调度器）
python3 get_quote.py HK.00700
```

能正常打印行情并写入 `daily_quote` 即安装成功。

---

## 配置说明

`config.conf` 为 INI 格式，主要区段：

| 区段 | 关键字段 | 说明 |
|------|----------|------|
| `[database]` | `host / port / dbname / user / password` | PostgreSQL 连接；可用 `DB_*` 环境变量覆盖 |
| `[futu]` | `host / port` | FutuOpenD 网关地址（默认 `127.0.0.1:11111`） |
| `[ticker]` | `stocks / quote_stocks / buffer_size / flush_interval_seconds / reconnect_delay_seconds / health_check_seconds / full_tick_capture` | 订阅标的、缓冲与重连参数；`quote_stocks` 显式指定 LV1 QUOTE 订阅；`full_tick_capture` 诊断用全量落盘（默认关） |
| `[eastmoney]` | `hk_fs / page_size / timeout / max_retry` | 东方财富板块 / 资金源参数 |
| `[fred]` | `api_key` | FRED 宏观数据 Key |
| `[wecom]` | `bot_a_id / bot_a_secret / bot_b_id / bot_b_secret` | 企微 AI Bot A/B |
| `[wecom_webhook]` | `key` | 企微群机器人 Webhook Key |
| `[sync_server]` | `host / user / password` | 代码同步服务器（部署用） |

---

## 使用示例

### 运行常驻调度器（核心）

调度器从 `stock_info` 表（`is_active`）动态加载股票，按市场预设自动生成盘中 / 收盘任务。

```bash
# 前台运行（调试）
python3 market_scheduler.py

# 后台运行
nohup python3 market_scheduler.py > log/scheduler.out 2>&1 &
```

新增 / 停用股票：`setup_new_stock.py` / `setup_new_stocks.py`（写入 `stock_info`，调度器自动感知）。

### 单独运行采集脚本

多数 `get_*.py` 支持命令行直接传股票代码（空格分隔），便于调试：

```bash
python3 get_quote.py                     # 默认 HK.00700 + HK.800000(恒指)
python3 get_quote.py SH.600900           # 单只
python3 get_quote.py SH.600900 HK.00700  # 多只
python3 get_benchmark.py                 # 恒生等基准指数 → benchmark_daily
python3 get_south_flow.py                # 南向资金
python3 get_cbbc.py                      # 牛熊证
python3 get_buyback.py                   # 港股回购
python3 get_margin_balance.py            # 融资融券余额
```

### 回填历史数据

```bash
python3 backfill_daily_quote.py               # 默认 SH.600900，近 60 天
python3 backfill_daily_quote.py HK.00700 365  # 指定股票 + 天数（自动分页）
python3 backfill_south_flow.py                # 南向资金历史回填
python3 backfill_hk_market_turnover.py           # 市场成交额历史回填
```

### 三层资金流分析

```bash
python3 -m liquidity.fund_flow_analysis            # 默认近 20 日
python3 -m liquidity.fund_flow_analysis --days 40  # 近 40 日
python3 -m liquidity.fund_flow_analysis --top 15   # 各排行取 15 只
```

程序化调用：

```python
from liquidity.report import liquidity_panorama
result = liquidity_panorama()   # dict: {"market": ..., "sectors": ..., "stocks": ...}
```

### 量化分析示例

```bash
python3 direction_backtest.py          # 方向（涨跌）回测
python3 multi_factor_analysis.py       # 多因子分析
python3 support_resistance.py          # 支撑 / 阻力位
python3 ofi_short_horizon.py           # OFI 短周期分析
python3 analyze_tick_distribution.py   # Tick 分布分析
```

### 运行测试

```bash
python3 -m pytest tests/ -q
```

> 测试依赖真实数据库（如 `tests/test_db.py` 需 `market_db` 可用）。

---

## 部署

### 服务器一键部署（bootstrap.sh）

模型：**本机 = 开发源，服务器 = 运行环境**。`bootstrap.sh` 在目标 Linux 服务器上运行，一切从官方源安装（apt/dnf、PyPI、富途官网），不复制任何运行环境。

```bash
# 1) 把项目代码（含 bootstrap.sh）同步到服务器 APP_DIR
# 2) 在服务器以 root 运行（幂等，可重复执行；密钥走环境变量或交互输入）
sudo bash bootstrap.sh
```

它会：创建应用用户 `mkt`、初始化 PostgreSQL、建 `.venv`、渲染 `system/` 下的 systemd 单元（含 `FutuOpenD.service`、`market-scheduler.service`、`monitor-collector.service`、`ticker-collector.service`、`streamlit-dashboard.service`、`wecom-collector-a.service`）、启动常驻服务。

目标服务器：**OpenCloudOS 9**（RHEL9 系，`dnf`）。应用以 **`mkt`** 非 root 用户运行，富途网关以 `root` 运行（最小权限）。

### 本地 → 生产增量推送（deploy.sh）

```bash
./deploy.sh sync        # 增量推送代码到服务器（不重启）
./deploy.sh up          # sync + 重启 4 个应用服务（日常发版）
./deploy.sh restart     # 重启全部 / 指定服务
./deploy.sh bootstrap   # 交互式重跑服务器端 bootstrap.sh
./deploy.sh status      # 查看服务状态
./deploy.sh logs        # 查看日志
./deploy.sh fix-perms   # 修复属主
```

> 密钥分权：`rsync` 走推送通道（`deploy-sync`），root 操作走 root 通道（`deploy_key`）。`config.conf` / `.env` / `.venv` / `log` / `trend_cache` 受 rsync exclude 保护，不会被覆盖。

### 本地 macOS 调试

`system/` 下提供 launchd plist（`com.equity.*.plist`），脚本按路径自动切换（检测到服务器路径则用它，否则用脚本所在目录）。

---

## 贡献指南

欢迎提交 Issue 与 Pull Request。

### 分支与提交流程

1. 从 `main` 切出特性分支：`git checkout -b feat/<short-desc>` 或 `fix/<short-desc>`。
2. 保持提交原子、聚焦；提交信息遵循 **Conventional Commits**：
   - `feat:` 新功能（如新增采集脚本）
   - `fix:` 缺陷修复
   - `refactor:` 重构（不改变外部行为）
   - `chore:` 构建 / 部署 / 配置变动
   - `docs:` 文档
3. 推送并发起 PR，描述**改动动机、影响范围、验证方式**。

### 新增采集脚本约定（重要）

所有入库脚本需遵循统一约定，才能被 `market_scheduler` 常驻调用：

```python
def run(codes=None, ctx=None):
    """采集入口。codes: 代码列表；ctx: 共享富途上下文（可空，为空时取 get_shared_ctx()）。"""
    ctx = ctx or get_shared_ctx()
    # ... 采集 + 通过 db.upsert / db.bulk_upsert 落库 ...

if __name__ == "__main__":
    # 保留命令行直接运行（便于调试），接受空格分隔的股票代码
    targets = sys.argv[1:] or ["HK.00700"]
    run(targets)
```

- 入库统一走 `db.py` 的 `upsert` / `bulk_upsert`（ON CONFLICT DO UPDATE / NOTHING）。
- 富途调用**必须**经 `collector_runtime` 的共享上下文（非线程安全，需加锁串行）。
- 股票列表从 `stock_info` 表动态加载，不要硬编码标的清单。
- 新增收盘任务在 `market_scheduler.py` 的 `MARKET_PRESETS` 中登记。

### 代码风格

- Python：遵循 PEP 8；脚本头部保留模块 docstring（数据源 / 写入表 / 用法）。
- 日志统一走 `log_utils.setup_logger`（`./log` 目录、按天滚动、保留 10 天）。
- 敏感信息（密钥、密码）**只**进 `config.conf` / 环境变量，**绝不**硬编码或提交。
- 新增依赖需同时更新 `requirements.txt` 并固定版本。

### 数据库结构变更约定（强制）

`sql/schema.sql` 是数据库结构的**唯一真相源（source of truth）**。任何在代码里新增 / 删除 / 修改表、列、索引、视图的操作，**必须**同步更新 `sql/schema.sql`，否则会导致：

- `sync_schema.sh` 在部署时把"代码已建但 schema 漏写"的表误判为差异；
- 新环境 bootstrap 时缺表、或他人拉库后结构不一致。

适用范围（不限于）：

- Python 中 `CREATE TABLE IF NOT EXISTS ...` 内联建表 → 同步到 `schema.sql`；
- `df.to_sql(...)` 自动建表 → 在 `schema.sql` 补对应 `CREATE TABLE`；
- 新增 / 删除列、修改列类型、新增 / 删除索引、新增视图。

自查（提交前建议执行，只读、不修改数据库）：

```bash
# 本地连 market_db 检测 schema.sql 与本地库的差异，仅生成修复 SQL 不执行
APP_DIR=$(pwd) python3 sql/_schema_diff.py
# 或针对任意服务器（生成修复 SQL 供人工 review）
./sql/sync_schema.sh <ROOT_HOST> <SSH_KEY> <APP_DIR>
```

差异结果只生成 SQL 文本（高风险操作默认注释），由人工确认后手动执行，脚本本身绝不改库。

### 文档

- `*.md`（AI 生成文档）默认被 `.gitignore` 忽略；**`README.md` 是例外**，需随项目演进持续更新。
- 不要在 README 中写入任何密钥或内部服务器明文凭证。

---

## 常见问题

- **`ImportError: psycopg2 ... not valid for use in process`（macOS）**：受管 Python 的 Hardened Runtime 拒绝加载外部 `.so`。请使用项目 `.venv` 内的 Python，或对 venv 的 Python 解除签名后再用（详见团队知识库）。
- **QUOTE 订阅收不到数据**：LV1 免费额度有限，`[ticker] quote_stocks` 需显式指定有权限的代码；`health_check_seconds` 会在 FutuOpenD 每日重启后自动重建订阅。
- **调度器没自动跑任务**：检查 `stock_info` 中目标股票的 `is_active` 与 `market` 字段；任务按交易时段门控（`is_trading_hours`）。
- **`config.conf` 改动不生效**：确认未被 `.gitignore` 误覆盖，且 `DB_*` 环境变量未优先于配置文件。

---

## 免责声明

本项目仅用于**个人研究与学习**，所有数据来自第三方公开接口，不构成任何投资建议。使用过程中产生的数据准确性、可用性及由此引发的任何后果由使用者自行承担。请遵守各数据源的使用条款与频率限制。
