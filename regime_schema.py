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
  · regime.market_regime_daily **已废弃（v2.1，保留待迁移）**：旧合成层（温度计/季节/风险/FSI/跨资产风险偏好），
      由 dimension_value（L2）与 business_value（L3）取代；迁移完成后 drop
  · regime.dimension_dict / dimension_value   维度指标（L2，六维）
  · regime.business_dict / business_value     业务指标（L3：市场状态/系统性压力/风险预算）
  · regime.signal_dict / signal_value         反转风险信号（横切，不并入任何合成）

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
    dimension       VARCHAR(24)     NOT NULL,          -- 【legacy】旧分组（11 取值，语义混杂）；v2.1 起维度归属看 domain
    domain          VARCHAR(16),                       -- v2.1 维度归属：宽度/估值/情绪/资金/压力/趋势（NULL=宏观条件输入）
    role            VARCHAR(16)     DEFAULT 'context', -- status=参与状态/预算 · context=背景 · explanation=解释维度
    market_scope    VARCHAR(16)     NOT NULL,          -- CN / HK / CN+HK / GLOBAL
    unit            VARCHAR(16),                       -- pct / ratio / count / bp / score_0_100 ...
    direction       VARCHAR(12)     NOT NULL,          -- high_risk / high_good / neutral（分位化方向统一依据）
    freq            VARCHAR(8)      NOT NULL,          -- day / week / month / quarter
    source          VARCHAR(64),                       -- 依赖表/数据源
    formula         TEXT,                              -- 计算口径（引用实现函数）
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    remark          TEXT,                              -- 规范化三段式：【定义】是什么 /【判读】怎么看 /【数据】样本与断点
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
    is_final        BOOLEAN         NOT NULL DEFAULT FALSE,  -- FALSE=当日初步值（源未齐），T+1 全链路重算后置 TRUE
    model_version   VARCHAR(16),                       -- 本行口径（= indicator_dict.params_hash）
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

    # ── v2.1 三层指标架构：L2 维度指标 ─────────────────────────────────────
    "dimension_dict": """
CREATE TABLE IF NOT EXISTS regime.dimension_dict (
    dimension_code   VARCHAR(24)     PRIMARY KEY,       -- DIM.BREADTH / DIM.VALUATION / ...
    dimension_name   VARCHAR(40)     NOT NULL,          -- 宽度 / 估值 / 情绪 / 资金 / 压力 / 趋势
    question         VARCHAR(120),                      -- 该维度回答的业务问题（一句话）
    polarity         VARCHAR(12)     NOT NULL,          -- high_good=越高越好 / high_risk=越高越危险
    members          JSONB           NOT NULL,          -- [{code, sign, weight, block}]；sign 统一极性，block 为分族/分块名
    agg_method       VARCHAR(16)     NOT NULL DEFAULT 'block_equal',
                                                        -- block_equal=分块等权 / mean_winsor=扁平等权(仅同质成员) / single_anchor=单锚+校验 / rule=规则判定
    block_agg        JSONB,                             -- 块内聚合方式 {"块名":"mean"|"max"}（压力维度信用块**取第二紧**=trimmed max，见 §5.5）
    state_bands      JSONB,                             -- 5 档阈值（默认 [20,40,60,80]）
    state_labels     JSONB,                             -- 5 档标签（按极性命名，如 极窄/偏窄/中性/偏广/极广）
    params           JSONB,                             -- {winsor:[2,98],coverage_min:0.6,dispersion_max:30,weight_cap:0.25, K:3(趋势)}
    params_hash      VARCHAR(16),                       -- 口径指纹：变化必须触发历史重算
    is_active        BOOLEAN         NOT NULL DEFAULT TRUE,
    remark           TEXT,
    updated_at       TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "dimension_value": """
CREATE TABLE IF NOT EXISTS regime.dimension_value (
    dimension_code  VARCHAR(24)     NOT NULL,
    market          VARCHAR(8)      NOT NULL,           -- CN / HK
    trade_date      DATE            NOT NULL,
    score           NUMERIC(6,2),                       -- 0-100，方向已统一到该维度极性
    state           VARCHAR(12),                        -- 定性档；NULL = 覆盖度/分歧不达标，不出档
    coverage        NUMERIC(5,4),                       -- 有效成员 / 应有成员（0-1）
    dispersion      NUMERIC(6,2),                       -- 成员分位标准差
    blocks          JSONB,                              -- 块级分数 {"波动":{"score":..,"coverage":..},...}；SYSTEMIC_STRESS.trigger 依赖本列
    contrib         JSONB,                              -- {pos:[{code,delta}],neg:[{code,delta}]} 正负各 top3
    flags           JSONB,                              -- 结构性背离/极端（如 NEW_HIGH_LOW_20 vs _60）
    confidence      VARCHAR(8),                         -- 高/中/低
    is_final        BOOLEAN         NOT NULL DEFAULT FALSE,  -- FALSE=当日初步值（上游原子未齐）；T+1 全链路重算
    model_version   VARCHAR(16),                        -- 口径版本（= dimension_dict.params_hash）
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (dimension_code, market, trade_date)
)
""",

    "dimension_value_idx": """
CREATE INDEX IF NOT EXISTS idx_dim_value_date
    ON regime.dimension_value (market, trade_date DESC)
""",

    # ── v2.1 三层指标架构：L3 业务指标 ─────────────────────────────────────
    "business_dict": """
CREATE TABLE IF NOT EXISTS regime.business_dict (
    biz_code        VARCHAR(32)     PRIMARY KEY,        -- BIZ.MARKET_REGIME / BIZ.SYSTEMIC_STRESS / BIZ.RISK_BUDGET
    biz_name        VARCHAR(40)     NOT NULL,
    kind            VARCHAR(16)     NOT NULL,           -- state_machine / band_composite / constraint_min
    inputs          JSONB           NOT NULL,           -- [{kind:dimension|atom|external, code, sign}]
    params          JSONB,                              -- 阈值/状态机参数（DEADBAND/CONFIRM_DAYS/MIN_DWELL/bands）
    params_hash     VARCHAR(16),                        -- 口径指纹：变化必须触发历史重算
    output_schema   JSONB,                              -- 字段说明（state/level/since/binding_constraint/trigger...）
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    remark          TEXT,
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "business_value": """
CREATE TABLE IF NOT EXISTS regime.business_value (
    biz_code       VARCHAR(32)     NOT NULL,
    market         VARCHAR(8)      NOT NULL,
    trade_date     DATE            NOT NULL,
    score          NUMERIC(6,2),                        -- 连续读数（纯状态类可空）
    state          VARCHAR(16),                         -- 上行/震荡/下行；正常/关注/警戒/危机
    level          SMALLINT,                            -- 档位序号（1 起，便于比较与画图）
    since_date     DATE,                                -- 进入当前状态的日期（状态机）
    dwell_days     SMALLINT,                            -- 已在当前状态停留的交易日数
    status         VARCHAR(12),                         -- 已确认 / 待确认
    confidence     VARCHAR(8),                          -- 高/中/低（宽度确认等外部调节项写这里）
    detail         JSONB,                               -- {candidate,evidence,binding_constraint,trigger,spike,missing}
    is_final       BOOLEAN         NOT NULL DEFAULT FALSE, -- FALSE=当日初步值（源未齐），T+1 重算后置 TRUE
    model_version  VARCHAR(16),
    updated_at     TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (biz_code, market, trade_date)
)
""",

    "business_value_idx": """
CREATE INDEX IF NOT EXISTS idx_biz_value_date
    ON regime.business_value (market, trade_date DESC)
""",

    # ── v2.1 三层指标架构：横切信号 ────────────────────────────────────────
    "signal_dict": """
CREATE TABLE IF NOT EXISTS regime.signal_dict (
    signal_code    VARCHAR(40)     PRIMARY KEY,         -- SIG.REVERSAL.TOP_* / _BOTTOM_*
    signal_name    VARCHAR(60)     NOT NULL,
    direction      VARCHAR(8)      NOT NULL,            -- TOP / BOTTOM
    category       VARCHAR(16),                         -- 宽度/估值/情绪/资金/宏观/基本面
    market_scope   VARCHAR(16)     NOT NULL DEFAULT 'CN+HK',  -- CN / HK / CN+HK（无源市场不登记该族 → v_signal_summary.unavailable_families）
    expr           TEXT,                                -- 条件的人可读描述（非可执行 DSL）
    inputs         JSONB,                               -- 引用的原子指标清单
    params         JSONB,                               -- 阈值参数（**唯一真源**：不得写在代码里）
    params_hash    VARCHAR(16),                         -- 口径指纹：变化必须触发该信号历史重算
    is_active      BOOLEAN         NOT NULL DEFAULT TRUE,
    remark         TEXT,
    updated_at     TIMESTAMPTZ     DEFAULT NOW()
)
""",

    "signal_value": """
CREATE TABLE IF NOT EXISTS regime.signal_value (
    signal_code    VARCHAR(40)     NOT NULL,
    market         VARCHAR(8)      NOT NULL,
    trade_date     DATE            NOT NULL,
    hit            BOOLEAN         NOT NULL,            -- 当日是否命中
    days           SMALLINT,                            -- 已连续命中交易日数（"回落 3 天" vs "回落 40 天"）
    strength       NUMERIC(12,4),                       -- 命中强度（如背离幅度 pp），可空
    detail         JSONB,
    model_version  VARCHAR(16),
    updated_at     TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (signal_code, market, trade_date)
)
""",

    "signal_value_idx": """
CREATE INDEX IF NOT EXISTS idx_signal_value_date
    ON regime.signal_value (trade_date DESC, market, hit)
""",

    # 信号汇总视图（market_profile.md §5.11 ⑥）：族去重后的顶/底计数，不新建业务指标
    "v_signal_summary": """
CREATE OR REPLACE VIEW regime.v_signal_summary AS
WITH markets AS (
    SELECT DISTINCT market FROM regime.signal_value
),
fam AS (
    SELECT m.market,
           d.category,
           COUNT(d.signal_code) AS n_sig
    FROM markets m
    JOIN regime.signal_dict d
      ON d.is_active
     AND (d.market_scope IS NULL
          OR d.market_scope = 'CN+HK'
          OR d.market_scope = 'GLOBAL'
          OR d.market_scope = m.market)
    GROUP BY m.market, d.category
),
unavail AS (
    SELECT market,
           ARRAY_AGG(category ORDER BY category) AS unavailable_families,
           COUNT(*) FILTER (WHERE n_sig > 0)     AS available_family_cnt
    FROM fam
    GROUP BY market
),
agg AS (
    SELECT v.market,
           v.trade_date,
           (COUNT(*) FILTER (WHERE d.direction = 'TOP'    AND v.hit))::int AS top_cnt,
           (COUNT(*) FILTER (WHERE d.direction = 'BOTTOM' AND v.hit))::int AS bottom_cnt,
           (COUNT(DISTINCT d.category) FILTER (WHERE d.direction = 'TOP'    AND v.hit))::int AS top_family_cnt,
           (COUNT(DISTINCT d.category) FILTER (WHERE d.direction = 'BOTTOM' AND v.hit))::int AS bottom_family_cnt,
           COALESCE(ARRAY_AGG(DISTINCT d.category) FILTER (WHERE d.direction = 'TOP'    AND v.hit), ARRAY[]::varchar[]) AS top_families,
           COALESCE(ARRAY_AGG(DISTINCT d.category) FILTER (WHERE d.direction = 'BOTTOM' AND v.hit), ARRAY[]::varchar[]) AS bottom_families
    FROM regime.signal_value v
    JOIN regime.signal_dict  d ON d.signal_code = v.signal_code
    WHERE d.is_active
    GROUP BY v.market, v.trade_date
)
SELECT a.market,
       a.trade_date,
       a.top_cnt,
       a.top_family_cnt,
       a.bottom_cnt,
       a.bottom_family_cnt,
       a.top_families,
       a.bottom_families,
       COALESCE(u.unavailable_families, ARRAY[]::varchar[]) AS unavailable_families,
       u.available_family_cnt
FROM agg a
LEFT JOIN unavail u ON u.market = a.market
""",

    # ── v2.1 增量列（幂等 ADD COLUMN，不破坏既有数据与既有代码）──────────
    "indicator_dict_v21_cols": """
ALTER TABLE regime.indicator_dict ADD COLUMN IF NOT EXISTS domain VARCHAR(16);
ALTER TABLE regime.indicator_dict ADD COLUMN IF NOT EXISTS role   VARCHAR(16) DEFAULT 'context'
""",

    # ── v2.1.1 评审整改增量列（B2 口径指纹 / B3 初步值 / B10 制度断点）────
    "v211_cols": """
ALTER TABLE regime.indicator_dict  ADD COLUMN IF NOT EXISTS params_hash VARCHAR(16);
ALTER TABLE regime.indicator_dict  ADD COLUMN IF NOT EXISTS breaks      JSONB;
ALTER TABLE regime.signal_dict     ADD COLUMN IF NOT EXISTS market_scope VARCHAR(16) NOT NULL DEFAULT 'CN+HK';
ALTER TABLE regime.signal_dict     ADD COLUMN IF NOT EXISTS params_hash  VARCHAR(16);
ALTER TABLE regime.indicator_value ADD COLUMN IF NOT EXISTS is_final      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE regime.indicator_value ADD COLUMN IF NOT EXISTS model_version VARCHAR(16);
ALTER TABLE regime.dimension_value ADD COLUMN IF NOT EXISTS is_final      BOOLEAN NOT NULL DEFAULT FALSE
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
    ("COLUMN", "regime.indicator_dict.source",
     "本指标引用的**原始源编码**：无前缀=regime.macro_series（查 macro_series_meta）、"
     "bench:=public.daily_benchmark（查 bench_code）；由 market_state_daily 的接线映射"
     "（PIT_MAP_*/BENCH_MAP/MOM_MAP）派生、每轮全量重写，故为空表示「由公式从多源计算」"
     "（来源见 formula），**不是遗漏**；接线校验见 tests/test_indicator_sources.py"),
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
    ("COLUMN", "regime.indicator_value.market",
     "**市场键**（主键三列之一）：'CN'=A 股 / 'HK'=港股，语义是「该行由哪个市场的计算产出」"
     "（calc_cn / calc_hk），不是标的所在地。同指标可两侧各有一条时序（如 VAL.ERP）；"
     "该指标在**哪些市场有数据**由 indicator_dict.market_scope 声明，两者一致性由 "
     "tests/test_indicator_dict_consistency.py 校验（声明 CN 却出现 HK 行会被判失败）"),
    ("COLUMN", "regime.indicator_value.percentile",
     "原始值的历史分位（0-100，expanding 只用当日及之前，无前视；未做方向调整——方向见 "
     "indicator_dict.direction，合成/展示由消费方取反）。"
     "⚠ **低频源（月/季）的分位有已知漂移**：这类序列在指标层被 PIT 前向填充成日频行 → "
     "同一取值被**重复计入**扩展样本（实测每个取值占的行数：LPR_1Y 137.7 / PMI_MFG 70.9 / "
     "CPI_YOY 43.2，而日频源 ≈1.0），故分位在两次数据更新之间会**缓慢上爬**（约 1~2 分位点/月，"
     "样本越大越轻）→ **月内分位斜率不含信息**，不要据此判趋势、也不要算「分位 N 日变化」；"
     "跨月/跨期的跳变才是真更新。即本分位实为**按交易日行数加权**的历史排位，非按观测次数"),
    ("TABLE", "trading_calendar",
     "交易日历（A股/港股分别维护；衍生自成交额表 + AKShare 校对）"),
    ("COLUMN", "trading_calendar.is_open",
     "是否开市（当前只落开市日，即表中存在即为交易日）"),
    ("COLUMN", "trading_calendar.src",
     "来源：derived:*=从现有表派生 / akshare / manual=人工修正"),
    # ── v2.1 三层指标架构 ──────────────────────────────────────────────
    ("TABLE", "regime.dimension_dict",
     "维度指标字典（L2）：六个维度（宽度/估值/情绪/资金/压力/趋势）的成员、极性、聚合方式与阈值参数；"
     "口径真相源在代码，本表是可版本化投影"),
    ("COLUMN", "regime.dimension_dict.members",
     "成员清单 [{code,sign,weight,block}]；sign 用于统一极性（+1 顺向、-1 取 100−分位），"
     "block 为分族/分块名（宽度=4族、情绪=3块、资金=2块、压力=5块）。"
     "**同一原子指标只能归属一个维度**（情绪=意愿、资金=真金白银净流的分界见 market_profile.md §5.0），"
     "重复归属会被归属校验测试拦下"),
    ("COLUMN", "regime.dimension_dict.agg_method",
     "**六维不共用同一算法，只共用框架**（market_profile.md §5.7 L2-b）："
     "block_equal=分块等权（块内按 block_agg 聚、块间等权；情绪/资金/压力）；"
     "single_anchor=单锚+校验（估值：ERP 定档，其余只调置信）；"
     "mean_winsor=扁平等权（仅成员同质时可用，else 权重会退化成「写了几个指标」）；"
     "rule=规则判定（趋势：成员全为同一价格序列的不同窗口，相关>0.8 → 不做平均；"
     "方向由 L3 的 BIZ.MARKET_REGIME 判定，本维度的 score 只承载「趋势有多陡」，"
     "用**单锚 σ 归一化**：|TSMOM_12M| / σ_252，再 /K 映射到 0-100，见 §5.6）"),
    ("COLUMN", "regime.dimension_dict.block_agg",
     "块内聚合方式 {\"块名\":\"mean\"|\"max\"}。默认 mean；"
     "压力维度的信用块**取第二紧**（trimmed max）而非 max——"
     "AA 系成员源只留近 3 日，max 会被短样本成员永久绑架；"
     "块内 coverage < 0.5 时退化为均值并标 low_confidence（§5.5）"),
    ("COLUMN", "regime.dimension_dict.params",
     "维度级参数：winsor=[2,98]（**仅用于稳健估计参考分布**，分位结果不截尾）、coverage_min=0.6、"
     "dispersion_max=30（对 dispersion_norm = dispersion × sqrt(n/10) 判定）、weight_cap=0.25、"
     "K=3（**仅趋势维度**：σ 归一化后「几个标准差算满格」。纯展示参数——score 对 K 是单调变换、不改排序，"
     "且趋势维度 state=NULL 不出档，故改 K 不影响任何判定、不需重标阈值）"),
    ("COLUMN", "regime.dimension_dict.params_hash",
     "口径指纹（成员/权重/winsor/阈值/门禁的哈希）。**变化即代表口径变更**，必须触发该维度历史重算；"
     "消费端读到的 model_version 与本值不等即为旧口径数据"),
    ("TABLE", "regime.dimension_value",
     "维度指标日频读数（L2）：一市场一日一维一行，由计算层写入"),
    ("COLUMN", "regime.dimension_value.state",
     "定性档（5 档，标签按极性命名，如宽度 极窄/偏窄/中性/偏广/极广、压力 极稳/稳健/正常/紧张/危机）。"
     "**NULL 表示不出档**（coverage < 0.6 或 dispersion > 30）——此时只可读 score，不可读定性结论"),
    ("COLUMN", "regime.dimension_value.coverage",
     "有效成员 / 应有成员（0-1）。港股情绪维度只有 2/7 成员 → 约 0.29，必然不出档；"
     "缺口是信息，不得按中性处理"),
    ("COLUMN", "regime.dimension_value.dispersion",
     "成员分位的标准差。内部严重背离（如短期动能强、中期未修复）时该值会明显抬升，"
     "此时\"平均分\"不可直接当结论，必须下钻 contrib / flags"),
    ("COLUMN", "regime.dimension_value.flags",
     "结构性背离与极端标记（如 NEW_HIGH_LOW_20 与 _60 反向、MA60 与 MA200 分层、等权与市值加权差极端）；"
     "这些是设计好的快慢/结构对照，不能被平均分抹掉"),
    ("TABLE", "regime.business_dict",
     "业务指标字典（L3）：市场状态 / 系统性压力 / 风险预算的输入清单、类型与参数"),
    ("COLUMN", "regime.business_dict.kind",
     "state_machine=状态机（趋势三态，带死区/确认天数/最小停留）；band_composite=从维度分分档；"
     "constraint_min=多约束取最紧（风险预算）"),
    ("COLUMN", "regime.business_dict.inputs",
     "输入清单 [{kind:dimension|atom|external, code, sign}]。**kind 不是装饰**："
     "连续型业务指标从 dimension 取，判定型从 atom 取（规则要精确边界），"
     "external 指第②层引用（如 §4.2 跨资产确认度），本层不得重算"),
    ("TABLE", "regime.business_value",
     "业务指标日频读数（L3）：一市场一日一指标一行"),
    ("COLUMN", "regime.business_value.state",
     "状态标签：MARKET_REGIME=上行/震荡/下行；SYSTEMIC_STRESS=正常/关注/警戒/危机；"
     "RISK_BUDGET 见 level。**状态带 since/dwell，不允许日频翻转**（滞后带 + 确认天数 + 最小停留）"),
    ("COLUMN", "regime.business_value.since_date",
     "当前状态的首次进入日（状态机产出）；配合 dwell_days 判断状态是否已稳定"),
    ("COLUMN", "regime.business_value.detail",
     "增量信息集中在此：MARKET_REGIME={candidate,evidence,breadth_confirmation}；"
     "SYSTEMIC_STRESS={trigger,spike,blocks}（压力 70 分时\"信用在扩\"还是\"波动在冲\"处置不同）；"
     "RISK_BUDGET={binding_constraint,raw_cap,inputs,constrained_by,missing}（比数字更有用的是\"被什么卡住的\"）"),
    ("COLUMN", "regime.dimension_value.blocks",
     "块级分数 {\"块名\":{\"score\":..,\"coverage\":..}}。**SYSTEMIC_STRESS.trigger 的唯一数据来源**——"
     "成员级 contrib 拿不到「信用块取最紧」的语义，没有本列只能用成员 top3 反推"),
    ("COLUMN", "regime.business_value.is_final",
     "FALSE=当日初步值（18:45 计算时中债/事件类源尚未落库，T 日口径不完整），T+1 重算后置 TRUE。"
     "消费端展示终值、调试看初步值；**不得把 is_final=FALSE 的值当定论**"),
    ("TABLE", "regime.signal_dict",
     "反转风险信号字典：顶部/底部条件清单（清单正文见 market_profile.md §8.2/§8.3）"),
    ("TABLE", "regime.signal_value",
     "信号日频命中记录。**信号不得并入任何维度分或业务指标**——它是条件命中，不是程度读数"),
    ("COLUMN", "regime.signal_value.days",
     "已连续命中交易日数。\"站上 MA200 比例回落 3 天\"与\"回落 40 天\"是完全不同的信息，"
     "单给计数会抹掉这个差别，故本列必填"),
    ("COLUMN", "regime.signal_dict.params",
     "阈值唯一真源（如 {pct_high:90}、{drawdown_pp:10,window:20}）。"
     "⚠ 不得写在代码里——否则与 §5.7 的 params_hash 体系脱节，调阈值不触发重算"),
    ("COLUMN", "regime.signal_dict.params_hash",
     "口径指纹。signal_value.model_version 取本值；变化即口径变更，必须重算该信号的历史命中"),
    ("COLUMN", "regime.signal_dict.market_scope",
     "该信号在哪些市场有源。港股无涨跌停/两融 → 对应信号只登记 CN；"
     "该族在 HK 因此进 v_signal_summary.unavailable_families、不进 family_cnt 的分母"),
    ("VIEW", "regime.v_signal_summary",
     "信号汇总（§5.11 ⑥）：top_family_cnt / bottom_family_cnt 为**族去重**后的计数"
     "（同族命中多条只计 1 票），判读用族数、条数只作参考。"
     "⚠ 汇总用视图表达，不新建业务指标——信号是条件命中，不是程度读数"),
    ("COLUMN", "regime.indicator_value.is_final",
     "FALSE=当日初步值（T 日 18:45 计算时中债/事件类源尚未落库）。"
     "⚠ T+1 源补齐后必须沿 L1→L2→L3 **全链路**重算并覆盖——"
     "只重算 business_value 会让维度分与业务值来自不同口径"),
    ("COLUMN", "regime.dimension_value.is_final",
     "FALSE=当日初步值（上游原子为初步值）。同 indicator_value.is_final：T+1 必须全链路重算"),
    ("COLUMN", "regime.indicator_dict.breaks",
     "制度/源断点 [{date, reason}]（注册制、涨跌幅规则变更、北向 2024-08 停披露、港股卖空 2026-06 起等）。"
     "断点前后的分位**不可比**，前端须按断点分段显示；不登记的断点会被误读成市场信号"),
    ("COLUMN", "regime.indicator_dict.domain",
     "v2.1 维度归属（L2）：宽度/估值/情绪/资金/压力/趋势。"
     "NULL = 宏观条件输入（不属任何维度、不参与维度聚合），由业务指标按需直接引用"),
    ("COLUMN", "regime.indicator_dict.role",
     "指标在架构中的角色：status=参与状态/预算判定 · context=仅作背景与解释 · "
     "explanation=解释维度成员（有信号级消费者、无决策级消费者，如情绪与资金维度）"),
    ("COLUMN", "regime.indicator_dict.dimension",
     "【legacy】旧分组字段（11 个取值，其中\"资金\"混入 M1/LPR/DXY 等宏观利率、\"趋势\"为 0），"
     "仅作浏览分组；v2.1 起维度归属一律看 domain。计划在计算层迁移时改名 theme，避免与 domain 混淆"),
    ("TABLE", "regime.market_regime_daily",
     "【v2.1 已废弃，保留待迁移】旧合成层：温度计/季节/风险/FSI/跨资产风险偏好。"
     "继任者：dimension_value（L2 六维）+ business_value（L3 市场状态/系统性压力/风险预算）+ signal_value。"
     "旧字段口径（如 season 实际只由 PCT_ABOVE_MA200 的 25 分位决定）不再维护"),
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
            for vname in ("v_macro_latest", "v_macro_series"):
                cur.execute("""
                    SELECT table_name FROM information_schema.views
                    WHERE table_schema='regime' AND table_name=%s""", (vname,))
                print(f"  regime.{vname:20s} {'✅ 存在' if cur.fetchone() else '❌ 缺失'}")
    print("\n完成。注意：sql/schema.sql 必须同步登记（部署基准）。")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
