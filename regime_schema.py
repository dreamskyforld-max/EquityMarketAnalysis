#!/usr/bin/env python3
"""市场状态分析系统（regime schema）结构定义 —— 唯一建表入口。

定位与 profiling/schema.py 一致：
  · 本模块是「代码侧」的建表真相源（幂等 ensure_schema），同时逐字登记进 sql/schema.sql
    （schema.sql 是部署/更新的基准，两者必须保持一致，改这里必须同步改那里）。
  · 适用：新库自举（bootstrap）、存量库补齐、结构比对（sql/_schema_diff.py）。

覆盖对象（P0 已落地，见 doc/market_profile_collection_plan.md §5.2）：
  · regime.macro_series        宏观/资产配置时序长表（PIT：release_time + revision）
  · regime.v_macro_latest      最新修订视图
  · regime.indicator_dict      指标字典（口径/方向/频率/来源）
  · regime.indicator_value     指标时序长表（原始值 + 历史分位）
  · public.trading_calendar    交易日历（共享工具表，A股/港股分别维护）
  · regime.market_regime_daily 合成层（温度计/季节/风险/FSI/**跨资产风险偏好**）

P1 已落地（随采集脚本）：
  · regime.analyst_forecast_snapshot  一致预期逐日快照（差分依赖）
  · regime.ipo_event                   IPO 明细
  · regime.fund_issuance_event         新成立基金明细
  · regime.unlock_schedule             限售解禁排期
  · regime.insider_trade               董监高增减持明细

用法:
    python3 regime_schema.py          # 直接建表（幂等，可重复执行）
    from regime_schema import ensure_schema; ensure_schema(conn)
"""
import sys

# ── DDL（与 sql/schema.sql 对应章节逐字一致）────────────────────────────────
_DDL = {
    "schema": "CREATE SCHEMA IF NOT EXISTS regime",

    "macro_series_meta": """
CREATE TABLE IF NOT EXISTS regime.macro_series_meta (
    series_code     VARCHAR(40)     PRIMARY KEY,
    series_name     VARCHAR(80)     NOT NULL,
    unit            VARCHAR(16),                        -- pct / bp / index / yi_yuan / count ...
    freq            VARCHAR(8)      NOT NULL,           -- day / month / quarter
    market          VARCHAR(8)      NOT NULL DEFAULT 'CN',  -- CN / HK / US / GLOBAL / CMDTY
    source          VARCHAR(48),                        -- 采集来源（含通道+接口名），如 akshare:bond_china_yield / pbc:social-financing
    lag_days        INT,                                -- 发布滞后天数：release_time = period_date + lag_days（PIT 输入，宁晚勿早）
    collect_params  JSONB,                              -- 采集契约参数：{func,col,date_col,nbs,sdmx,pbc,use_release_col,row_filter,release_from_date}
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    remark          TEXT,                               -- 序列级口径备注（**不逐行重复**，旧版曾放在 fact.extra 里）
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "macro_series": """
CREATE TABLE IF NOT EXISTS regime.macro_series (
    id              BIGSERIAL       PRIMARY KEY,
    series_code     VARCHAR(40)     NOT NULL REFERENCES regime.macro_series_meta(series_code),
    period_date     DATE            NOT NULL,           -- 数据所属期（月频取统计月份对应日期，口径见 meta.remark）
    value           NUMERIC(20,6),
    release_time    TIMESTAMPTZ,                        -- 实际可得时间（PIT，防前视）
    revision        INT             NOT NULL DEFAULT 0, -- 修订版本（同 period 多次修订各存一行）
    extra           JSONB,
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (series_code, period_date, revision)
)
""",

    "macro_latest_view": """
CREATE OR REPLACE VIEW regime.v_macro_latest AS
SELECT DISTINCT ON (series_code, period_date) *
FROM regime.macro_series
ORDER BY series_code, period_date, revision DESC
""",

    "macro_series_wide_view": """
CREATE OR REPLACE VIEW regime.v_macro_series AS
SELECT m.series_code, m.series_name, m.unit, m.freq, m.market, m.source,
       s.period_date, s.value, s.release_time, s.revision, s.extra,
       s.created_at, s.updated_at
FROM regime.macro_series s JOIN regime.macro_series_meta m USING (series_code)
""",

    "macro_series_idx": """
CREATE INDEX IF NOT EXISTS idx_macro_series_code_date
    ON regime.macro_series (series_code, period_date DESC)
""",

    "indicator_dict": """
CREATE TABLE IF NOT EXISTS regime.indicator_dict (
    indicator_code  VARCHAR(48)     PRIMARY KEY,       -- 如 BREADTH.ADV_RATIO / VAL.PE_MEDIAN / FSI.COMPOSITE
    indicator_name  VARCHAR(80)     NOT NULL,
    scope           VARCHAR(16)     NOT NULL,          -- 分析域作用域（**非架构分层**）：MACRO=外生宏观/跨市场 ALLOC=资产配置 MARKET=市场自身 SECTOR=行业
    dimension       VARCHAR(24)     NOT NULL,          -- 宽度/估值/情绪/资金/风险偏好/金融压力/风格/宏观/景气
    market_scope    VARCHAR(16)     NOT NULL,          -- CN / HK / CN+HK / GLOBAL
    unit            VARCHAR(16),                       -- pct / ratio / count / bp / score_0_100 ...
    direction       VARCHAR(12)     NOT NULL,          -- high_risk / high_good / neutral（分位化方向统一依据）
    freq            VARCHAR(8)      NOT NULL,          -- day / week / month / quarter
    source          VARCHAR(64),                       -- 依赖表/数据源
    formula         TEXT,                              -- 计算口径（引用实现函数）
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    remark          VARCHAR(200),
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "indicator_value": """
CREATE TABLE IF NOT EXISTS regime.indicator_value (
    indicator_code  VARCHAR(48)     NOT NULL,
    market          VARCHAR(8)      NOT NULL,          -- CN / HK / GLOBAL
    trade_date      DATE            NOT NULL,
    raw_value       NUMERIC(20,6),
    percentile      NUMERIC(6,2),                      -- 0-100 历史分位（原始值分位，未做方向调整）
    extra           JSONB,                             -- 明细（如顶部信号命中清单）
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (indicator_code, market, trade_date)
)
""",

    "indicator_value_idx": """
CREATE INDEX IF NOT EXISTS idx_indicator_value_date
    ON regime.indicator_value (trade_date DESC, market)
""",

    "trading_calendar": """
CREATE TABLE IF NOT EXISTS trading_calendar (
    market      VARCHAR(8)  NOT NULL,          -- CN / HK
    cal_date    DATE        NOT NULL,
    is_open     BOOLEAN     NOT NULL,
    src         VARCHAR(40),                   -- akshare / derived:daily_market_turnover / manual
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (market, cal_date)
)
""",

    "ipo_event": """
CREATE TABLE IF NOT EXISTS regime.ipo_event (
    stock_code      VARCHAR(20)     PRIMARY KEY,
    stock_name      VARCHAR(80),
    market          VARCHAR(8),                        -- SH / SZ / BJ
    board           VARCHAR(24),                       -- 源「板块」：非科创板/科创板/创业板/北交所
    subscribe_date  DATE,                              -- 申购日期（前瞻排期，未上市即有值）
    list_date       DATE,                              -- 上市日期（待上市为空，后续 upsert 补齐）
    issue_price     NUMERIC(12,4),
    issue_pe        NUMERIC(12,4),
    industry_pe     NUMERIC(12,4),
    issue_shares    NUMERIC(20,4),                     -- 发行总数（**万股**，源口径）
    lottery_rate    NUMERIC(12,6),                     -- 中签率 %
    first_day_close NUMERIC(12,4),
    first_day_chg   NUMERIC(12,4),                     -- 上市首日涨跌幅 %
    is_break_issue  BOOLEAN,                           -- 首日破发：首日收盘价 < 发行价
    limit_up_days   INT,                               -- 连续一字板数量
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:stock_xgsglb_em',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "ipo_event_idx": """
CREATE INDEX IF NOT EXISTS idx_ipo_event_list_date ON regime.ipo_event (list_date DESC)
""",

    "fund_issuance_event": """
CREATE TABLE IF NOT EXISTS regime.fund_issuance_event (
    fund_code       VARCHAR(20)     PRIMARY KEY,
    fund_name       VARCHAR(160),
    company         VARCHAR(80),                       -- 发行公司
    fund_type       VARCHAR(40),                       -- 源「基金类型」：股票型/混合型-偏股/债券型...
    subscribe_period VARCHAR(40),                      -- 集中认购期（文本，源未结构化）
    setup_date      DATE,                              -- 成立日期
    issue_share     NUMERIC(20,4),                     -- 募集份额（**亿份**；源常滞后为空 → upsert 不可覆盖已有值）
    manager         VARCHAR(80),
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:fund_new_found_em',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "fund_issuance_idx": """
CREATE INDEX IF NOT EXISTS idx_fund_issuance_date ON regime.fund_issuance_event (setup_date DESC)
""",

    "unlock_schedule": """
CREATE TABLE IF NOT EXISTS regime.unlock_schedule (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    stock_name      VARCHAR(80),
    unlock_date     DATE            NOT NULL,
    holder_type     VARCHAR(64)     NOT NULL DEFAULT '未知',   -- 源「限售股类型」（可能为逗号复合串）
    unlock_shares   BIGINT,                            -- 解禁数量（股）
    actual_shares   BIGINT,                            -- 实际解禁数量（股）
    unlock_value    NUMERIC(20,2),                     -- 实际解禁市值（元）
    float_ratio     NUMERIC(12,8),                     -- 占解禁前流通市值比例（源为小数，非百分数）
    pre_close       NUMERIC(12,4),                     -- 解禁前一交易日收盘价
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:stock_restricted_release_detail_em',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, unlock_date, holder_type)
)
""",

    "unlock_schedule_idx": """
CREATE INDEX IF NOT EXISTS idx_unlock_date ON regime.unlock_schedule (unlock_date DESC)
""",

    "insider_trade": """
CREATE TABLE IF NOT EXISTS regime.insider_trade (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    stock_name      VARCHAR(80),
    direction       VARCHAR(8)      NOT NULL,          -- BUY=增持 / SELL=减持
    ann_date        DATE,                              -- 公告日期
    end_date        DATE,                              -- 截止日期（定期报告口径为报告期末）
    holder_name     VARCHAR(80),                       -- 董监高姓名
    position        VARCHAR(64),                       -- 董监高职务
    change_shares   BIGINT,                            -- 变动数量（股，源含正负号）
    change_ratio    NUMERIC(12,6),                     -- 变动比例（源口径，多数字段缺失）
    avg_price       NUMERIC(12,4),                     -- 成交均价
    end_shares      NUMERIC(20,4),                     -- 期末持股数量（万股，源口径）
    event_value     NUMERIC(20,2),                     -- 变动金额（元）＝|变动数量|×成交均价（成交均价缺失时为空）
    reason          VARCHAR(64),                       -- 持股变动原因：竞价交易/大宗交易/定期报告
    data_kind       VARCHAR(16),                       -- 临时公告 / 定期报告
    dedup_key       VARCHAR(40)     NOT NULL,          -- 源内去重键（md5）
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:stock_hold_management_detail_cninfo',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, source, dedup_key)
)
""",

    "insider_trade_idx": """
CREATE INDEX IF NOT EXISTS idx_insider_ann_date ON regime.insider_trade (ann_date DESC)
""",

    "sector_valuation_snapshot": """
CREATE TABLE IF NOT EXISTS regime.sector_valuation_snapshot (
    snapshot_date   DATE            NOT NULL,          -- 快照日（采集日，取最近 A 股交易日）
    sw_code         VARCHAR(12)     NOT NULL,          -- 申万一级行业代码（801xxx）
    sw_name         VARCHAR(40),
    constituent_count INT,                             -- 成份个数（口径参考）
    pe_static       NUMERIC(12,4),                     -- 静态市盈率
    pe_ttm          NUMERIC(12,4),                     -- TTM(滚动)市盈率
    pb              NUMERIC(12,4),                     -- 市净率
    div_yield       NUMERIC(12,4),                     -- 静态股息率 %
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:sw_index_first_info',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (snapshot_date, sw_code)
)
""",

    "sector_valuation_idx": """
CREATE INDEX IF NOT EXISTS idx_sector_val_code
    ON regime.sector_valuation_snapshot (sw_code, snapshot_date DESC)
""",

    "sector_mapping": """
CREATE TABLE IF NOT EXISTS regime.sector_mapping (
    ths_name        VARCHAR(40)     PRIMARY KEY,       -- 同花顺行业名（= sector_fund_flow.sector_name 的键）
    ths_code        VARCHAR(12),                       -- 同花顺行业代码（881xxx）
    sw_code         VARCHAR(12),                       -- 映射到的申万一级代码（801xxx）
    sw_name         VARCHAR(40),
    method          VARCHAR(24)     NOT NULL DEFAULT 'return_corr',
    corr            NUMERIC(8,4),                      -- 与映射行业的日收益相关系数（判别依据）
    corr_runner_up  NUMERIC(8,4),                      -- 次优相关系数（判别裕度 = corr − runner_up）
    runner_up_code  VARCHAR(12),
    overlap_days    INT,                               -- 相关性所用重叠交易日数
    as_of           DATE,                              -- 计算日
    confirmed       BOOLEAN         NOT NULL DEFAULT FALSE,  -- 人工确认（低置信度需人工，重算时保留）
    remark          VARCHAR(120),
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:corr_return',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "sector_mapping_idx": """
CREATE INDEX IF NOT EXISTS idx_sector_mapping_sw ON regime.sector_mapping (sw_code)
""",

    "sector_daily": """
CREATE TABLE IF NOT EXISTS regime.sector_daily (
    trade_date      DATE            NOT NULL,          -- 交易日
    sw_code         VARCHAR(12)     NOT NULL,          -- 申万一级行业代码（801xxx）
    sw_name         VARCHAR(40),
    close           NUMERIC(12,4),                     -- 行业指数收盘
    change_pct      NUMERIC(10,4),                     -- 当日涨跌幅 %
    ret_20d         NUMERIC(10,4),                     -- 20 日收益 %
    ret_60d         NUMERIC(10,4),                     -- 60 日收益 %
    rs_20d          NUMERIC(10,4),                     -- 20 日相对强度：ret_20d − 31 行业等权均值（横截面均值恒为 0）
    rs_rank         SMALLINT,                          -- 20 日 RS 排名（1=最强）
    mom_rank        SMALLINT,                          -- 20 日收益排名（同 rs_rank，保留语义区分）
    turnover        NUMERIC(20,4),                     -- 成交额（源单位，申万源为亿量级，仅行业内纵向可比）
    fund_flow_net   NUMERIC(16,4),                     -- 映射汇总的同花顺净流入（亿元；未映射为空）
    fund_flow_net_5d NUMERIC(16,4),                    -- 近 5 日累计净流入（亿元；不足 5 日为空）
    flow_rank       SMALLINT,                          -- 当日净流入排名（1=净流入最多）
    mapped_ths_count SMALLINT,                         -- 参与汇总的同花顺行业数（0=该行业无映射）
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (trade_date, sw_code)
)
""",

    "sector_daily_idx": """
CREATE INDEX IF NOT EXISTS idx_sector_daily_code ON regime.sector_daily (sw_code, trade_date DESC)
""",

    "sector_fund_flow": """
CREATE TABLE IF NOT EXISTS regime.sector_fund_flow (
    trade_date      DATE            NOT NULL,          -- 归属交易日（收盘后采集，取最近 A 股交易日）
    sector_name     VARCHAR(40)     NOT NULL,          -- 同花顺行业名（**≠ 申万一级**，跨表分析需映射）
    sector_index    NUMERIC(12,4),                     -- 行业指数点位（源口径）
    change_pct      NUMERIC(10,4),                     -- 行业涨跌幅 %
    inflow          NUMERIC(16,4),                     -- 流入资金（亿元）
    outflow         NUMERIC(16,4),                     -- 流出资金（亿元）
    net_inflow      NUMERIC(16,4),                     -- 净额（亿元）＝流入−流出
    company_count   INT,                               -- 行业公司家数
    leader_name     VARCHAR(40),                       -- 领涨股
    leader_chg      NUMERIC(10,4),                     -- 领涨股涨跌幅 %
    source          VARCHAR(48)     NOT NULL DEFAULT 'akshare:stock_fund_flow_industry',
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (trade_date, sector_name)
)
""",

    "sector_fund_flow_idx": """
CREATE INDEX IF NOT EXISTS idx_sector_flow_date ON regime.sector_fund_flow (trade_date DESC)
""",

    "analyst_forecast_snapshot": """
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
)
""",

    "analyst_forecast_idx": """
CREATE INDEX IF NOT EXISTS idx_forecast_stock_date
    ON regime.analyst_forecast_snapshot (stock_code, snapshot_date DESC)
""",

    "market_regime_daily": """
CREATE TABLE IF NOT EXISTS regime.market_regime_daily (
    trade_date          DATE        NOT NULL,
    market              VARCHAR(8)  NOT NULL,          -- CN / HK
    thermometer         NUMERIC(6,2),                  -- 市场温度计 0-100（天气）
    season              VARCHAR(8),                    -- 春/夏/秋/冬
    season_score        NUMERIC(6,2),
    risk_score          NUMERIC(6,2),                  -- 风险指数 0-100
    fsi                 NUMERIC(6,2),                  -- 金融压力指数 0-100
    risk_appetite       NUMERIC(6,2),                  -- 跨资产风险偏好 0-100（§5.4：6 成分等权，2026-10-09 起有值）
    top_signal_cnt      SMALLINT,                      -- 顶部信号命中数
    bottom_signal_cnt   SMALLINT,
    detail              JSONB,                         -- 分项/成员明细
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (trade_date, market)
)
""",
}

# 与 sql/schema.sql 对应的 COMMENT（单独执行，保证既有库补注释）
_COMMENTS = [
    ("TABLE", "regime.macro_series_meta",
     "原始序列注册表（维度表）：series_code 的身份/口径/出处；macro_series 为纯事实表（只存观测值+PIT）"),
    ("COLUMN", "regime.macro_series_meta.unit",
     "pct=百分比 / bp=基点 / index=指数点 / yi_yuan=亿元 / count=计数；"
     "⚠ 单位换算（如万亿→亿元）属**显式迁移**：须同步 UPDATE 历史 value，本列只表达当前口径"),
    ("COLUMN", "regime.macro_series_meta.lag_days",
     "发布滞后天数：release_time = period_date + lag_days（**PIT 输入**，宁晚勿早）；改此值即改该序列的可得时点"),
    ("COLUMN", "regime.macro_series_meta.collect_params",
     "采集契约参数（JSONB）：{func,col,date_col,nbs,sdmx,pbc,use_release_col,row_filter,release_from_date}；"
     "配合 lag_days 可从本表重建完整采集定义"),
    ("TABLE", "regime.macro_series",
     "宏观/资产配置时序**事实表**（采集层原始值，PIT：release_time + revision；元数据见 macro_series_meta）"),
    ("COLUMN", "regime.macro_series.series_code",
     "序列编码（命名空间：CN./HK./US./FX./CMDTY.）"),
    ("COLUMN", "regime.macro_series.period_date",
     "数据所属期；月/季频为统计期，不含发布日期"),
    ("COLUMN", "regime.macro_series.release_time",
     "数据实际可得时间，PIT 回测按此过滤，禁止用 period_date 代替"),
    ("COLUMN", "regime.macro_series.revision",
     "修订版本号，0=首次发布；宏观数据修订时新增行而非覆盖"),
    ("TABLE", "regime.indicator_dict",
     "指标字典：所有指标的口径/方向/频率登记（新增指标须先进字典）"),
    ("COLUMN", "regime.indicator_dict.scope",
     "分析域作用域：MACRO=外生宏观/跨市场 ALLOC=资产配置 MARKET=市场自身 SECTOR=行业；"
     "**非 ODS/DW/ADS 架构分层**（架构分层由表承载，见 macro_series→indicator_value→market_regime_daily）"),
    ("COLUMN", "regime.indicator_dict.direction",
     "high_risk=越高越危险 / high_good=越高越好 / neutral；分位化与合成按此统一方向"),
    ("COLUMN", "regime.indicator_dict.formula",
     "计算口径描述（引用实现函数），保证可解释、可追溯"),
    ("TABLE", "regime.analyst_forecast_snapshot",
     "分析师一致预期每日快照（修正宽度的差分依赖；必须逐日采集，历史不可回溯重建）"),
    ("COLUMN", "regime.analyst_forecast_snapshot.snapshot_date",
     "快照日期（采集日）；同一天重复运行幂等覆盖"),
    ("COLUMN", "regime.analyst_forecast_snapshot.fy1_year",
     "财年标签：源列名（如「2026预测每股收益」）逐年滚动，故年份必须随值存，跨年比较须按标签对齐"),
    ("TABLE", "regime.ipo_event",
     "IPO 明细（一票一行；家数/首日涨幅/破发率由查询聚合，不另建日频汇总表）"),
    ("COLUMN", "regime.ipo_event.list_date",
     "上市日期；待上市（仅申购排期）为空 —— 采集用 skip_null_updates，避免重跑把已有值抹成 NULL"),
    ("COLUMN", "regime.ipo_event.is_break_issue",
     "首日破发：首日收盘价 < 发行价（情绪低迷信号）"),
    ("TABLE", "regime.fund_issuance_event",
     "新成立基金明细（按 setup_date 聚合出「发行热度/爆款基金」信号）"),
    ("COLUMN", "regime.fund_issuance_event.issue_share",
     "募集份额（亿份）；源侧常滞后为空 → 采集必须 skip_null_updates 保护已入库值"),
    ("TABLE", "regime.unlock_schedule",
     "限售股解禁排期（A股；个股级，市场压力由查询按 unlock_date 聚合）"),
    ("COLUMN", "regime.unlock_schedule.float_ratio",
     "占解禁前流通市值比例（源为小数而非百分数）；解禁压力的权重依据"),
    ("TABLE", "regime.insider_trade",
     "董监高增减持明细（净增持=底部信号 / 净减持激增=顶部信号）"),
    ("COLUMN", "regime.insider_trade.data_kind",
     "临时公告=事件级（含成交均价）/ 定期报告=季报持股快照（变动比例多缺失）"),
    ("COLUMN", "regime.insider_trade.dedup_key",
     "源内去重键（md5：公告日+董监高+股数+口径），避免同一事件重复落库"),
    ("TABLE", "regime.sector_valuation_snapshot",
     "申万一级行业估值每日快照（源只给当前横截面无历史 → 行业估值分位只能逐日累积）"),
    ("COLUMN", "regime.sector_valuation_snapshot.snapshot_date",
     "快照日（取最近 A 股交易日）；同一天重复运行幂等覆盖"),
    ("COLUMN", "regime.sector_valuation_snapshot.div_yield",
     "静态股息率 %（用于「股息率-国债利差」类行业配置口径）"),
    ("TABLE", "regime.sector_mapping",
     "同花顺行业 → 申万一级 的映射（用**日收益相关系数**客观推断，非人工拍脑袋；低置信度需人工 confirmed）"),
    ("COLUMN", "regime.sector_mapping.corr",
     "与映射行业的日收益相关系数；判别裕度 = corr − corr_runner_up，裕度小说明分类歧义"),
    ("COLUMN", "regime.sector_mapping.confirmed",
     "人工确认标记；重算映射时保留已确认行，避免自动结果覆盖人工判断"),
    ("TABLE", "regime.sector_daily",
     "逐行业日频横截面（价格/动量/相对强度/排名 + 映射汇总的资金流；计算层写入）"),
    ("COLUMN", "regime.sector_daily.rs_20d",
     "20 日相对强度 = 行业 ret_20d − 31 行业等权均值；**横截面均值恒为 0**，故只看相对排序不看绝对水平"),
    ("COLUMN", "regime.sector_daily.turnover",
     "成交额（申万源单位与个股口径不同，亿量级）→ 仅行业间/纵向比较可用，禁止与个股混算"),
    ("TABLE", "regime.sector_fund_flow",
     "行业资金流日频横截面（同花顺源；源只给「当前快照」无历史 → 历史逐日累积）"),
    ("COLUMN", "regime.sector_fund_flow.trade_date",
     "归属交易日：收盘后采集并取「最近 A 股交易日」，避免节假日把快照记成非交易日"),
    ("COLUMN", "regime.sector_fund_flow.sector_name",
     "同花顺行业名（约 90 个细分行业，≠ 申万一级 31 个）；跨表与申万指数join需先建映射（P2）"),
    ("COLUMN", "regime.sector_fund_flow.net_inflow",
     "净额（亿元）＝流入−流出；**必须收盘后采集**，盘中值不完整"),
    ("TABLE", "regime.indicator_value",
     "指标时序长表：口径见 indicator_dict；raw_value=原始值，percentile=历史分位(0-100)"),
    ("COLUMN", "regime.indicator_value.percentile",
     "原始值的历史分位（0-100，expanding 只用当日及之前，无前视；未做方向调整——方向见 indicator_dict.direction，合成/展示由消费方取反）"),
    ("TABLE", "trading_calendar",
     "交易日历（A股/港股分别维护；衍生自成交额表 + AKShare 校对）"),
    ("COLUMN", "trading_calendar.is_open",
     "是否开市（当前只落开市日，即表中存在即为交易日）"),
    ("COLUMN", "trading_calendar.src",
     "来源：derived:*=从现有表派生 / akshare / manual=人工修正"),
]


META_COLS = ("series_name", "unit", "freq", "market", "source",
             "lag_days", "collect_params", "remark")
"""macro_series 事实表已剥离的「元数据 + 采集契约」列 → 维度表 macro_series_meta。"""


def upsert_macro_series(conn, rows, conflict_cols=None, skip_null_updates=True) -> int:
    """写 regime.macro_series：**元数据与采集契约拆进维度表，事实表只留观测本身**。

    采集器按「一行带齐元数据」的方式构造 rows，本函数负责拆分：
      ① series_name/unit/freq/market/source/lag_days/collect_params/remark
         → upsert `macro_series_meta`（按 series_code 去重）；
      ② 其余列 → 写 `macro_series` 事实表。

    · **remark 自动上提**：若采集器仍把口径备注写在 `extra.remark`（历史写法），此处
      自动搬到 meta.remark 并从 extra 剥离 —— 避免同一段备注在每一行重复存储。
    ⚠ 事实表有 FK → meta，故**必须先写 meta 再写 fact**（本函数已保证顺序）。
    """
    from db import bulk_upsert

    if not rows:
        return 0
    conflict_cols = conflict_cols or ["series_code", "period_date", "revision"]
    meta: dict = {}
    fact = []
    for r in rows:
        code = r.get("series_code")
        extra = r.get("extra")
        remark = r.get("remark")
        if not remark and isinstance(extra, dict) and extra.get("remark"):
            remark = extra.pop("remark")            # 上提：extra → meta
        if code:
            m = {c: r[c] for c in META_COLS if r.get(c) is not None}
            if remark:
                m["remark"] = remark
            if code in meta:
                meta[code].update(m)
            else:
                meta[code] = dict(m, series_code=code)
        fact.append({k: v for k, v in r.items()
                     if k not in META_COLS and k != "remark"})
    if meta:
        bulk_upsert(conn, "regime.macro_series_meta", list(meta.values()),
                    conflict_cols=["series_code"], skip_null_updates=True)
    bulk_upsert(conn, "regime.macro_series", fact,
                conflict_cols=conflict_cols, skip_null_updates=skip_null_updates)
    return len(fact)


def seed_series_meta(conn, specs) -> int:
    """把采集器代码里的序列定义播种进 macro_series_meta（**只补空缺，不覆盖**）。

    specs: [{"series_code","series_name","unit","freq","market","source",
             "lag_days","collect_params","remark"}, ...]

    → 已在库里的序列**以库为准**（改契约/单位/滞后/备注直接改库，不必改代码重启）；
      新序列由代码播种一次，此后同样归库管。

    ⚠ 不能用 bulk_upsert(do_nothing=True)：meta 行往往已存在（身份回填阶段建的），
      DO NOTHING 会全部跳过、契约永远写不进去；也不能用 skip_null_updates=True
      （那是「新值覆盖」，会把库里的人工调整冲掉）。这里要的是「只补空缺」。
    """
    from psycopg2.extras import Json

    rows = [(
        s["series_code"], s["series_name"], s.get("unit"), s.get("freq", "day"),
        s.get("market") or s["series_code"].split(".")[0], s.get("source"),
        s.get("lag_days"),
        Json(s["collect_params"]) if s.get("collect_params") else None,
        s.get("remark"),
    ) for s in specs]
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO regime.macro_series_meta
            (series_code, series_name, unit, freq, market, source,
             lag_days, collect_params, remark)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (series_code) DO UPDATE SET
                series_name    = COALESCE(macro_series_meta.series_name,    EXCLUDED.series_name),
                unit           = COALESCE(macro_series_meta.unit,           EXCLUDED.unit),
                freq           = COALESCE(macro_series_meta.freq,           EXCLUDED.freq),
                market         = COALESCE(macro_series_meta.market,         EXCLUDED.market),
                source         = COALESCE(macro_series_meta.source,         EXCLUDED.source),
                lag_days       = COALESCE(macro_series_meta.lag_days,       EXCLUDED.lag_days),
                collect_params = COALESCE(macro_series_meta.collect_params, EXCLUDED.collect_params),
                remark         = COALESCE(macro_series_meta.remark,         EXCLUDED.remark)""",
            rows)
    return len(rows)


def load_series_defs(conn, codes=None, source_prefix=None) -> dict:
    """读「生效定义」→ {series_code: {name, unit, freq, market, source, lag_days, params, remark}}。

    采集器用它把自己的代码清单与库登记合并：**库里有 → 以库为准**；没有 → 用代码兜底。
    """
    import json

    sql = """SELECT series_code, series_name, unit, freq, market, source,
                    lag_days, collect_params, remark
             FROM regime.macro_series_meta WHERE is_active"""
    args = []
    if codes:
        sql += " AND series_code = ANY(%s)"
        args.append(list(codes))
    elif source_prefix:
        sql += " AND source LIKE %s"
        args.append(source_prefix + "%")
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    out = {}
    for code, name, unit, freq, market, source, lag, params, remark in rows:
        if isinstance(params, str):          # psycopg2 有时返回 JSON 字符串
            params = json.loads(params) if params else None
        out[code] = dict(name=name, unit=unit, freq=freq, market=market, source=source,
                         lag_days=lag, params=params or {}, remark=remark)
    return out


def apply_series_defs(rows: list, defs: dict) -> list:
    """把 load_series_defs 读到的「生效定义」贴回每个 fact 行。

    适用于代码里按循环生成 series_code 的采集器（如中债曲线 = 曲线 × 期限）：行已按代码
    清单构造好，这里统一用库里登记的值覆盖名称/单位/契约/备注，再由 upsert_macro_series
    拆进 macro_series_meta。**库里没有该序列时原样保留**（用代码兜底）。
    """
    for r in rows:
        m = defs.get(r.get("series_code"))
        if not m:
            continue
        # ⚠ 不覆盖 `source`：它表示「这次实际从哪取的数」，应由采集代码按**生效参数**现算，
        #   否则改了 DB 里的 symbol/indicator，source 会留在旧值上造成登记失真。
        for k, col in (("name", "series_name"), ("unit", "unit"),
                       ("freq", "freq"), ("market", "market")):
            if m.get(k) is not None:
                r[col] = m[k]
        if m.get("lag_days") is not None:
            r["lag_days"] = m["lag_days"]
        if m.get("params"):
            r["collect_params"] = m["params"]
        if m.get("remark"):
            r["remark"] = m["remark"]
    return rows


def ensure_schema(conn=None, verbose: bool = False) -> None:
    """幂等建表 + 补注释。conn 为空时自建连接（CLI 用）。"""
    from db import get_conn

    if conn is None:
        with get_conn() as c:
            ensure_schema(c, verbose=verbose)
        return

    with conn.cursor() as cur:
        for name, ddl in _DDL.items():
            cur.execute(ddl)
            if verbose:
                print(f"  ✅ {name}")
        for kind, target, text in _COMMENTS:
            cur.execute(f"COMMENT ON {kind} {target} IS %s", (text,))
    conn.commit()


def _main() -> int:
    from db import get_conn

    print("=" * 60)
    print("regime schema 建表（幂等）")
    print("=" * 60)
    with get_conn() as conn:
        ensure_schema(conn, verbose=True)
        with conn.cursor() as cur:
            print("\n── 建表结果核对 ──")
            for schema, table in [("regime", "macro_series"), ("regime", "indicator_dict"),
                                  ("regime", "indicator_value"), ("public", "trading_calendar")]:
                cur.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
                print(f"  {schema}.{table:20s} rows = {cur.fetchone()[0]}")
            cur.execute("""
                SELECT table_name FROM information_schema.views
                WHERE table_schema='regime' AND table_name='v_macro_latest'""")
            print(f"  regime.v_macro_latest  {'✅ 存在' if cur.fetchone() else '❌ 缺失'}")
    print("\n完成。注意：sql/schema.sql 必须同步登记（部署基准）。")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
