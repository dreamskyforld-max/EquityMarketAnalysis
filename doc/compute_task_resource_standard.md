# 计算任务资源规范（内存 / IO / 编排）— 强制

> **适用范围**：所有「读库 → 计算 → 写库」的逻辑 —— 采集脚本（`get_*.py`）、画像计算（`compute_profile.py` + `profiling/`）、市场状态计算（`market_state_daily.py`）、回填脚本（`backfill_*.py`）等。
>
> **强制级别**：新增 / 修改任何计算逻辑时，必须逐条对照 §2「硬性红线」与 §6「上线前检查清单」。本规范是 `AGENTS.md` 的落地细则（那边只放摘要 + 指针）。
>
> **一句话版本**：这台机器只有 **~1.9GB 物理内存 + 1GB swap + 一块慢云盘**，任何"一次性把大表拉进进程"的操作都可能把整机拖死 —— 先算账、再分批、重任务必须独立进程 + 资源上限。

---

## 1. 事故档案（本规范存在的理由）

| 日期 | 形态 | 根因 | 代价 |
|---|---|---|---|
| 09-22 | **写爆发** | 大事务一次性落库，打满磁盘队列 | 整机僵死（修复：拆成逐标签小事务，见 `compute_profile.py` 注释） |
| 09-29 | **内存雪崩**（5.5h） | 画像计算一次性加载全市场行情 ~220 万行，峰值 **958MB** > 机器可用 ~700MB → 内核 direct reclaim + swap + 慢盘 IO 互相锁死 | 全机无响应，susstat/journald 同时断档 |
| 09-30 | **内存雪崩**（8.4h） | 同上（100% 复现，卡点都在 `trd_momentum_12_1` 触发 `_compute_all`） | 恢复瞬间可用内存仅 **29MB**、commit 150%；PG 单次 `fsync` 阻塞 **139s / 983s** |
| 10-01 ~ 10-05 | **遗留锁死**（4 天） | 上两次冻结的伤：scheduler 进程内一个 futu 调用永不返回、永久持有 `collector_runtime._call_lock`；重连只在抛异常时触发 → 全量采集超时 350+ 次/天，**只有重启进程才能恢复** | 4 天数据停采 |
| 10-05（预防） | 同款模式被拦截 | `market_state_daily._per_stock_windows` 一次性加载 ~200 万行，实测峰值 **888MB** | 上线前改造为按股票分批：**888MB → 221MB**，结果逐值一致 |

**关键机器事实**（做任何资源设计前必须记住）：
- 物理内存 ~1.9GB、swap ~1GB；常驻服务（PG + FutuOpenD + 4 个 Python 服务 + 前端）已占 ~1.2GB → **可用余量通常只有 600~700MB**。
- 云盘 fsync 在压力下曾阻塞 139s/983s → **任何"用内存换 CPU"的写法，代价会被磁盘放大成整机级故障**。
- 常量级参考：`a_daily_quote` 全史 ~1700 万行、画像窗口（262 交易日）~140 万行；`hk_daily_quote` 窗口 ~60 万行；`tick_data` 亿级；`daily_benchmark` ~24 万行。

---

## 2. 硬性红线（MUST / MUST NOT）

| # | 规则 | 判定方式 |
|---|---|---|
| **R1** | 任何"一次性把大表拉进进程"的操作（`fetchall()` / `pd.read_sql_query` 无下界）必须满足 **行数 × 行宽 < 100MB**；超限必须分批（§4.1 / §4.3） | §3 速算 + §5 实测 |
| **R2** | 大表清单（当前量级）：`a_daily_quote`、`hk_daily_quote`、`tick_data`、`daily_quote`、`daily_benchmark`。命中即触发 R1/R7 审查 | `tools/scan_big_table_loads.py` |
| **R3** | 峰值内存红线：**常驻进程内**任务 ≤ 机器可用内存 × 30%（≈200MB）；**独立单元**任务 cgroup `MemoryMax` ≤ 可用 × 70%（≈512MB），且实测峰值 ≤ 上限的 1/2 | §5 实测 |
| **R4** | 数据量随历史增长的重计算任务**必须独立进程**：systemd `oneshot` + `timer`，且显式给出 `MemoryMax` / `MemorySwapMax=0` / `RuntimeMaxSec` / `Nice` / `IOSchedulingPriority` / `OOMScoreAdjust` 六项（模板 §4.5） | 检查 unit 文件 |
| **R5** | **禁止**在常驻调度进程（`market_scheduler.py` 内 `run_module`）里跑数据量随历史增长的整表计算（画像、市场状态、全市场回填） | code review |
| **R6** | 写入：单事务 upsert **≤ 5 万行**；更长写入必须逐批 commit（09-22 教训） | code review |
| **R7** | 大表查询**必须带时间下界**（`BETWEEN` / `>=`）或实体过滤（`stock_code = ANY(...)`）；**禁止**无下界全表扫描 | 扫描工具 + review |

---

## 3. 内存估算速查（设计时先算账，再写代码）

**每单元格字节数**：

| 列类型 | 建议 dtype | 字节/行 | 220 万行折算 |
|---|---|---|---|
| str / date（object） | → `category`（低基数）/ `datetime64` | 56~68 | **~150MB 每列** |
| `category` | 低基数列必用 | ~4 + 字典 | ~10~18MB |
| `datetime64[ns]` | 日期列必用 | 8 | ~18MB |
| `float64` | 关键数值列（价格/收益/回撤） | 8 | ~18MB |
| `float32` | 均值类（金额/换手率），**不可用于价格派生** | 4 | ~9MB |

**峰值公式**：
```
峰值 ≈ Σ(各列行数 × 字节) × 叠加系数
叠加系数：fetchall + DataFrame 并存 ≈ 2×；concat / merge / sort_values 各再 +1×；
          rolling 等多中间列 ≈ +0.5×
```

**三个可直接对照的实例**：

| 规模 | 形态 | 实测峰值 | 判定 |
|---|---|---|---|
| 220 万行 × 4 个 object 列 | 一次性 fetchall + concat/merge/sort | **958MB** | ❌ 事故 |
| 200 万行 × 3 列（含 2 object） | `pd.read_sql_query` + rolling | **888MB** | ❌ 事故（上线前拦截） |
| 20 万行级 | 单次加载 + pivot | ~50-80MB | ✅ 可用（仍需实测） |
| 500 只 × 300 天（≈15 万行/批） | 分批加载 | ~40MB/批 | ✅ 标准做法 |

> **经验法则：超过 50 万行，就必须假设"不能一次性加载"。**

---

## 4. 标准做法（代码模板）

### 4.1 分批取数：server-side cursor

公共工具（`profiling/tags/_base.py`）：

```python
from profiling.tags._base import _read_sql_stream, _distinct_codes

# 逐批回传（PG 服务端游标），客户端每批即时转 DataFrame 并按 dtypes 瘦身
df = _read_sql_stream(conn, sql, params,
                      chunk_size=50_000,
                      dtypes={"stock_code": "category",
                              "trade_date": "datetime64[ns]",
                              "amount": "float32"})
```

要点：**dtype 必须在每批内就转换**，否则拼回终态时 object 列的累积峰值会原样回来。

### 4.2 dtype 瘦身（性价比最高的一步，改动 10 行以内）

```python
q["stock_code"] = q["stock_code"].astype("category")   # 150MB → ~12MB（220 万行）
q["trade_date"] = pd.to_datetime(q["trade_date"])      # 150MB → ~18MB
q["market"]     = q["market"].astype("category")
q["bench_code"] = q["bench_code"].astype("category")
q["amount"]     = q["amount"].astype("float32")        # 仅均值类列可用 float32
# ⚠ 价格/收益等派生源列保持 float64，不冒精度风险
```

注意：`category` 列做 `groupby` 时要显式 `observed=True`，否则会产生空组；转 `category` 前若需 `map`/字符串运算，先 `.astype(str)`。

### 4.3 按实体分批：峰值与总行数彻底脱钩（首选）

适用任何"逐实体（股票/行业/序列）计算"的场景。**等价性前提：窗口计算不跨实体**（如逐股 rolling、逐股聚合），此时按实体分批与全量计算数学等价。

```python
codes = _distinct_codes(conn, table, start, as_of)      # PG 端 DISTINCT，客户端只收几千行
rows = []
for i in range(0, len(codes), _BATCH_CODES):            # _BATCH_CODES = 500
    batch = codes[i:i + _BATCH_CODES]
    f = _load_batch(conn, table, batch, start, as_of)   # WHERE stock_code = ANY(%s) + ORDER BY
    for code, s in f.groupby("stock_code", sort=False, observed=True):
        rows.append(compute_one(code, s))
    del f                                               # 逐批释放，别等 GC
```

实例参考：`profiling/tags/technical.py::_compute_all`、`profiling/tags/trend.py::_positions`、`market_state_daily.py::_per_stock_windows`（改造后 888MB → 221MB）。

### 4.4 顺序与释放

- **逐市场 / 逐批处理**，算完 `del`，不要让 A+H 两份数据同时驻留。
- SQL 层 `ORDER BY`（走已有索引）**替代** pandas 的 `sort_values`；用 `WHERE stock_code = ANY(...)` 缩小 merge/join 规模。
- 大对象（窗口行情）在常驻进程里靠 `del` 不一定归还 OS（glibc arena 驻留，水位只涨不降）→ 这也是重任务要独立进程的原因之一。

### 4.5 独立任务单元模板（systemd）

```ini
# system/compute-profile.service —— 重计算任务的标配
[Unit]
Description=全量画像计算（每日一次，由 compute-profile.timer 触发）
After=network.target postgresql.service

[Service]
Type=oneshot
User=mkt
WorkingDirectory=/home/mkt/EquityMarketAnalysis
ExecStart=/home/mkt/EquityMarketAnalysis/.venv/bin/python3 compute_profile.py
MemoryMax=512M          # 硬上限：超限只杀自己，不触发全机回收
MemorySwapMax=0         # 禁止换出到慢盘（该盘 fsync 曾阻塞 983s）
RuntimeMaxSec=3600      # 硬超时兜底（曾卡 8.4 小时）
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7  # 让位于采集任务
OOMScoreAdjust=500      # 内存吃紧时优先牺牲本任务
# 无 [Install]：不随机自启，仅由 .timer 触发
```

配套：`timer` 用 `OnCalendar=`（与业务时刻对齐）+ `AccuracySec=1min`；任务内保留 `collection_task_log` 埋点（见 §7）。

---

## 5. 度量与验证（怎么证明达标 —— 不许"我觉得没问题"）

```bash
# Linux 单次运行峰值（最常用）
/usr/bin/time -v python3 your_task.py 2>&1 | grep "Maximum resident set size"

# systemd 单元的真实峰值（跑完一次后）
systemctl show compute-profile -p MemoryPeak

# macOS 本地开发
/usr/bin/time -l python3 your_task.py 2>&1 | grep "maximum resident set size"
```

**改造成分批 / 瘦身之后，必须证明"结果没变"**（本次三处改造都是这么验的）：

```bash
# ① 取旧代码基线（不污染工作区）
git worktree add /tmp/old-baseline HEAD && ln -sf $PWD/config.conf /tmp/old-baseline/
# ② 两边对同一 as_of 跑，存 pickle
# ③ 逐值对比
python3 -c "
import pandas as pd
o = pd.read_pickle('/tmp/old.pkl'); n = pd.read_pickle('/tmp/new.pkl')
print('shape', o.shape, n.shape, '完全相等', o.equals(n))
print('各列最大差', (o - n).abs().max().to_dict())
"
```

**验收标准（写进提交说明）**：实测峰值 / 耗时 / 与旧版对比结论 / 机器余量倍数。示例：`峰值 958MB → 221MB，耗时 49.6s，.equals()=True，占可用余量 1/3`。

---

## 6. 上线前检查清单（新计算逻辑逐条打勾）

- [ ] 是否命中大表（R2）？查询是否带时间下界 / 实体过滤（R7）？
- [ ] 用 §3 公式估算峰值；> 50 万行是否已分批（R1）？
- [ ] 是否**实测**峰值（§5）？与机器可用内存（~700MB）比是否留 ≥2 倍余量（R3）？
- [ ] 重计算任务是否独立 unit + 六项资源参数（R4/R5，模板 §4.5）？
- [ ] 写入是否 ≤5 万行/事务（R6）？
- [ ] 是否登记监控：任务级埋点（`collection_task_log`）或表级（`monitor_table_config`）（§7）？
- [ ] 是否验证结果一致性（新旧对比 / 单测）并写入提交说明（§5）？
- [ ] 是否跑过 `python3 tools/scan_big_table_loads.py` 并确认告警项？

---

## 7. 监控与自愈（"断供必须有人知道"）

| 层次 | 机制 | 位置 |
|---|---|---|
| 任务级 | 写 `collection_task_log` 埋点（`record_task_start/end`），并在 `monitor_task_config` 登记，由 `monitor_collector` 的 task_stall 判停摆（阈值 warn=1.5× / crit=2× 期望间隔） | `compute_profile.py::_task_start/_task_end`（`_in_scheduler()` 去重）、`monitor_collector.py::_SEED_TASK_CONFIG` |
| 表级 | `monitor_table_config` 登记时间列 + period + expect_lag（配置驱动，改表即生效） | `monitor_collector.py` |
| 独立单元 | `systemctl show <unit> -p MemoryPeak -p NRestarts`；失败看 `journalctl -u <unit>` | unit 文件 |

**已知盲区（待补）**：「进程还活着但所有外部调用都失败」这类故障（本次故障 B，4 天没人发现）—— task_stall/data_stale 都依赖"任务是否在跑"，抓不到"跑了但全失败"。建议后续加"连续 N 分钟 API 零成功"告警（基于 `collection_api_log`）。

---

## 8. 反例清单（这些写法已经被证明会出事）

| 反例 | 后果 | 正确做法 |
|---|---|---|
| `pd.read_sql_query("SELECT ... FROM a_daily_quote WHERE trade_date BETWEEN ...", conn)` 拉 200 万行后再 rolling | 888MB 峰值 → 整机雪崩 | §4.3 按实体分批 |
| `cur.fetchall()` 然后 `pd.DataFrame(rows)` | 两份并存（÷2 内存浪费） | §4.1 服务端游标 |
| 为"省 CPU"把 SQL 窗口函数全下推 | PG work_mem 紧张 → 临时文件写满慢盘 → **IO 雪崩** | 内存与 IO 都要算账，别把压力从内存搬到磁盘 |
| 在 `market_scheduler` 里跑整表计算 | 一次卡死 = 全量采集停摆（要重启进程） | §4.5 独立单元 |
| 不加 `MemorySwapMax=0` | 换出到慢盘 → swap 抖动拖死全机 | §4.5 模板 |
| 无下界的 `DELETE`/`UPDATE`/全表 `SELECT ... ORDER BY` | 同样的内存/IO 形态另一种入口 | 加下界 + 分批 commit |
