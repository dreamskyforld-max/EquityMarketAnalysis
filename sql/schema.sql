-- ============================================================================
-- EquityMarketAnalysis 数据库表设计
-- 数据库: market_db (PostgreSQL)
-- 用途: 持久化日数据采集结果 + 盘中趋势快照数据
-- ============================================================================

-- ============================================================================
-- 第一部分：基础/参考表
-- ============================================================================

-- 1. 股票基本信息表（参考数据）
CREATE TABLE IF NOT EXISTS stock_info (
    stock_code      VARCHAR(20)     PRIMARY KEY,        -- 如 HK.00700, SH.600519
    stock_name      VARCHAR(100),                       -- 股票名称
    market          CHAR(2),                            -- 市场: HK/SH/SZ
    symbol          VARCHAR(10),                        -- 纯数字代码: 00700
    currency        VARCHAR(10)     DEFAULT '港元',      -- 货币单位
    is_active       BOOLEAN         DEFAULT TRUE,       -- 是否活跃
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);

COMMENT ON TABLE  stock_info                     IS '股票基本信息（参考数据）';
COMMENT ON COLUMN stock_info.stock_code           IS '股票完整代码，如 HK.00700 / SH.600519 / SZ.000001';
COMMENT ON COLUMN stock_info.stock_name           IS '股票中文名称';
COMMENT ON COLUMN stock_info.market               IS '所属市场：HK(港股) / SH(上证) / SZ(深证)';
COMMENT ON COLUMN stock_info.symbol               IS '纯数字代码，如 00700 / 600519';
COMMENT ON COLUMN stock_info.currency             IS '交易货币单位，港股默认港元，A股默认元';
COMMENT ON COLUMN stock_info.is_active            IS '是否仍在活跃采集，FALSE 时跳过该股票';
COMMENT ON COLUMN stock_info.created_at           IS '记录创建时间';
COMMENT ON COLUMN stock_info.updated_at           IS '记录最后更新时间';

-- 1.1 股票-指数成分归属表（参考数据，低频刷新）
-- 反向建表：遍历「已知指数 → 全成分」，每只成分股落一行。
-- 查询某股票所属指数：SELECT sector_code, sector_name FROM stock_sector WHERE stock_code = 'HK.00700'
CREATE TABLE IF NOT EXISTS stock_sector (
    stock_code      VARCHAR(20)     NOT NULL,        -- 股票完整代码，如 HK.00700 / SH.600000
    sector_code     VARCHAR(20)     NOT NULL,        -- 指数代码：港股用富途 plate code(如 HK.800000) / A股用中证代码(如 000300)
    sector_name     VARCHAR(100),                       -- 指数中文名，如 恒生科技 / 沪深300
    sector_type     VARCHAR(20)     DEFAULT 'INDEX',    -- 归属类型，当前统一 INDEX
    weight          NUMERIC(10,4),                      -- 成分权重(可选，当前数据源未提供则为 NULL)
    source          VARCHAR(20),                        -- 数据来源：futu / akshare
    updated_at      TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (stock_code, sector_code)
);

COMMENT ON TABLE  stock_sector                    IS '股票-指数成分归属（参考数据，低频刷新）';
COMMENT ON COLUMN stock_sector.stock_code          IS '股票完整代码，如 HK.00700 / SH.600000';
COMMENT ON COLUMN stock_sector.sector_code         IS '指数代码：港股=富途 plate code(如 HK.800000)，A股=中证代码(如 000300)';
COMMENT ON COLUMN stock_sector.sector_name         IS '指数中文名，如 恒生科技 / 沪深300';
COMMENT ON COLUMN stock_sector.sector_type         IS '归属类型，当前统一为 INDEX';
COMMENT ON COLUMN stock_sector.weight              IS '成分权重(可选)，当前数据源未提供时为 NULL';
COMMENT ON COLUMN stock_sector.source              IS '数据来源：futu(港股) / akshare(A股)';
COMMENT ON COLUMN stock_sector.updated_at          IS '记录最后刷新时间';

-- ============================================================================
-- 第二部分：日级别数据采集表（每日盘后采集一次）
-- ============================================================================

-- 2. 每日行情快照
CREATE TABLE IF NOT EXISTS daily_quote (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,           -- 交易日
    update_time     TIMESTAMPTZ,                        -- 快照更新时间
    last_price      NUMERIC(12,4),                      -- 最新价
    open_price      NUMERIC(12,4),                      -- 今开
    high_price      NUMERIC(12,4),                      -- 最高
    low_price       NUMERIC(12,4),                      -- 最低
    prev_close      NUMERIC(12,4),                      -- 昨收
    change_pct      NUMERIC(8,4),                       -- 涨跌幅(%)
    volume          BIGINT,                             -- 成交量(股)
    turnover        NUMERIC(20,2),                      -- 成交额(元)
    turnover_rate   NUMERIC(8,4),                       -- 换手率(%)
    volume_ratio    NUMERIC(8,4),                       -- 量比
    high_52w        NUMERIC(12,4),                      -- 52周最高
    low_52w         NUMERIC(12,4),                      -- 52周最低
    total_market_val   NUMERIC(20,2),                   -- 总市值（原值，港元/元）
    circular_market_val NUMERIC(20,2),                  -- 流通市值（原值，港元/元）
    pe_ratio        NUMERIC(12,4),                      -- 静态市盈率
    pe_ttm_ratio    NUMERIC(12,4),                      -- 市盈率(TTM)
    pb_ratio        NUMERIC(12,4),                      -- 市净率
    dividend_ratio_ttm NUMERIC(8,4),                    -- 股息率(TTM, %)
    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);

COMMENT ON TABLE  daily_quote                    IS '每日行情快照（可衍生计算K线/超额收益等）';
COMMENT ON COLUMN daily_quote.stock_code          IS '股票完整代码，如 HK.00700';
COMMENT ON COLUMN daily_quote.trade_date          IS '交易日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_quote.update_time         IS '富途行情快照的更新时间（盘中时非空，盘后为当日收盘时间）';
COMMENT ON COLUMN daily_quote.last_price          IS '最新价（当日收盘价）';
COMMENT ON COLUMN daily_quote.open_price          IS '当日开盘价';
COMMENT ON COLUMN daily_quote.high_price          IS '当日最高价';
COMMENT ON COLUMN daily_quote.low_price           IS '当日最低价';
COMMENT ON COLUMN daily_quote.prev_close          IS '前一交易日收盘价（昨收）';
COMMENT ON COLUMN daily_quote.change_pct          IS '涨跌幅(%) = (最新价-昨收)/昨收*100';
COMMENT ON COLUMN daily_quote.volume              IS '当日成交量（股）';
COMMENT ON COLUMN daily_quote.turnover            IS '当日成交额（元/港元，取决于股票市场）';
COMMENT ON COLUMN daily_quote.turnover_rate       IS '换手率(%)';
COMMENT ON COLUMN daily_quote.volume_ratio        IS '量比（当日成交量/5日均量）';
COMMENT ON COLUMN daily_quote.high_52w            IS '52周最高价';
COMMENT ON COLUMN daily_quote.low_52w             IS '52周最低价';
COMMENT ON COLUMN daily_quote.total_market_val    IS '总市值（原值，港元/元，来自富途快照 total_market_val）';
COMMENT ON COLUMN daily_quote.circular_market_val IS '流通市值（原值，港元/元，来自富途快照 circular_market_val）';
COMMENT ON COLUMN daily_quote.pe_ratio            IS '静态市盈率（亏损股为空，来自富途快照 pe_ratio）';
COMMENT ON COLUMN daily_quote.pe_ttm_ratio        IS '市盈率TTM（来自富途快照 pe_ttm_ratio）';
COMMENT ON COLUMN daily_quote.pb_ratio            IS '市净率（来自富途快照 pb_ratio）';
COMMENT ON COLUMN daily_quote.dividend_ratio_ttm  IS '股息率TTM(%)（来自富途快照 dividend_ratio_ttm）';
COMMENT ON COLUMN daily_quote.created_at          IS '数据写入数据库的时间';

CREATE INDEX idx_daily_quote_stock_date ON daily_quote (stock_code, trade_date DESC);

-- 3. 每日基准指数行情
CREATE TABLE IF NOT EXISTS daily_benchmark (
    id                  BIGSERIAL       PRIMARY KEY,
    bench_code          VARCHAR(20)     NOT NULL,           -- 如 HK.800000, SH.000001, SZ.399001
    bench_name          VARCHAR(50),                        -- 恒生指数/上证指数/深证成指
    trade_date          DATE            NOT NULL,
    update_time         TIMESTAMPTZ,
    last_price          NUMERIC(12,4),
    prev_close          NUMERIC(12,4),
    change_pct          NUMERIC(8,4),
    close_20d_ago       NUMERIC(12,4),                      -- 20个交易日前收盘价
    created_at          TIMESTAMPTZ     DEFAULT NOW(),
    volume        bigint,
    turnover      numeric,

    UNIQUE (bench_code, trade_date));

COMMENT ON TABLE  daily_benchmark                     IS '每日基准指数行情（与 daily_quote 联合计算超额收益）';
COMMENT ON COLUMN daily_benchmark.bench_code          IS '基准指数代码：HK.800000(恒生) / SH.000001(上证) / SZ.399001(深证)';
COMMENT ON COLUMN daily_benchmark.bench_name          IS '基准指数中文名称：恒生指数 / 上证指数 / 深证成指';
COMMENT ON COLUMN daily_benchmark.trade_date          IS '交易日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_benchmark.update_time         IS '富途行情快照更新时间';
COMMENT ON COLUMN daily_benchmark.last_price          IS '基准指数最新价（当日收盘点位）';
COMMENT ON COLUMN daily_benchmark.prev_close          IS '基准指数前一交易日收盘点位';
COMMENT ON COLUMN daily_benchmark.change_pct          IS '基准指数涨跌幅(%)';
COMMENT ON COLUMN daily_benchmark.close_20d_ago       IS '20个交易日前的收盘价，用于计算中期趋势';
COMMENT ON COLUMN daily_benchmark.created_at          IS '数据写入数据库的时间';

CREATE INDEX idx_daily_bench_code_date ON daily_benchmark (bench_code, trade_date DESC);

-- 4. 北向资金（市场级别，非个股）
CREATE TABLE IF NOT EXISTS daily_northbound_flow (
    id                  BIGSERIAL       PRIMARY KEY,
    trade_date          DATE            NOT NULL,
    net_inflow          NUMERIC(16,2),                      -- 北向合计净流入(亿元)
    created_at          TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (trade_date)
);

COMMENT ON TABLE  daily_northbound_flow              IS '北向资金市场整体净流入（数据源：东方财富沪深港通页面）';
COMMENT ON COLUMN daily_northbound_flow.trade_date   IS '交易日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_northbound_flow.net_inflow   IS '北向资金当日合计净流入金额（亿元），正数=净买入，负数=净卖出，0=无数据(网站未更新或节假日)';
COMMENT ON COLUMN daily_northbound_flow.created_at   IS '数据写入数据库的时间';

CREATE INDEX idx_northbound_date ON daily_northbound_flow (trade_date DESC);

-- 5. 南向资金 → 已合并到 daily_ggt_hold（见下方），daily_south_flow 已废弃

-- 6. 牛熊证街货分布
CREATE TABLE IF NOT EXISTS daily_cbbc (
    id                  BIGSERIAL       PRIMARY KEY,
    stock_code          VARCHAR(20)     NOT NULL,
    trade_date          DATE            NOT NULL,
    bull_call_level     NUMERIC(12,4),                      -- 牛证回收价(港元)
    bull_street_volume  BIGINT,                             -- 牛证街货量(张)
    bear_call_level     NUMERIC(12,4),                      -- 熊证回收价(港元)
    bear_street_volume  BIGINT,                             -- 熊证街货量(张)
    created_at          TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);

COMMENT ON TABLE  daily_cbbc                         IS '牛熊证街货分布（数据源：港交所 CBBC 完整列表 CSV）';
COMMENT ON COLUMN daily_cbbc.stock_code              IS '正股完整代码，如 HK.00700';
COMMENT ON COLUMN daily_cbbc.trade_date              IS '交易日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_cbbc.bull_call_level         IS '街货量最大的牛证回收价（港元），股价跌破此价牛证作废';
COMMENT ON COLUMN daily_cbbc.bull_street_volume      IS '街货量最大的牛证对应街货量（张），代表散户看多力量';
COMMENT ON COLUMN daily_cbbc.bear_call_level         IS '街货量最大的熊证回收价（港元），股价涨破此价熊证作废';
COMMENT ON COLUMN daily_cbbc.bear_street_volume      IS '街货量最大的熊证对应街货量（张），代表散户看空力量';
COMMENT ON COLUMN daily_cbbc.created_at              IS '数据写入数据库的时间';

CREATE INDEX idx_cbbc_stock_date ON daily_cbbc (stock_code, trade_date DESC);

-- 7. 全日沽空数据
CREATE TABLE IF NOT EXISTS daily_short_selling (
    id                  BIGSERIAL       PRIMARY KEY,
    stock_code          VARCHAR(20)     NOT NULL,
    trade_date          DATE            NOT NULL,           -- 沽空数据对应的交易日
    stock_name          VARCHAR(100),                       -- 港交所返回的股票名称
    data_date           VARCHAR(50),                        -- 港交所页面上的日期文本（如 "02 Jun 2026"）
    short_selling_vol   BIGINT,                             -- 全日沽空股数
    short_selling_amt   NUMERIC(16,2),                      -- 全日沽空金额(亿港元)
    created_at          TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);

COMMENT ON TABLE  daily_short_selling                     IS '港股全日沽空数据（数据源：港交所全日沽空快照页面，Big5编码）';
COMMENT ON COLUMN daily_short_selling.stock_code          IS '股票完整代码，如 HK.00700（仅支持港股）';
COMMENT ON COLUMN daily_short_selling.trade_date          IS '沽空数据所对应的交易日 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_short_selling.stock_name          IS '港交所页面中返回的股票名称';
COMMENT ON COLUMN daily_short_selling.data_date           IS '港交所页面上标注的发布日期文本，如 "02 Jun 2026"';
COMMENT ON COLUMN daily_short_selling.short_selling_vol   IS '当日全日沽空股数';
COMMENT ON COLUMN daily_short_selling.short_selling_amt   IS '当日全日沽空金额（亿港元）';
COMMENT ON COLUMN daily_short_selling.created_at          IS '数据写入数据库的时间';

CREATE INDEX idx_short_selling_stock_date ON daily_short_selling (stock_code, trade_date DESC);

-- 8. 公司回购记录（单次回购明细，累计汇总可动态计算）
CREATE TABLE IF NOT EXISTS daily_buyback_event (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    buyback_date    DATE            NOT NULL,               -- 回购日期
    volume          BIGINT,                                 -- 回购数量(股)
    high_price      NUMERIC(12,4),                          -- 回购最高价
    low_price       NUMERIC(12,4),                          -- 回购最低价
    avg_price       NUMERIC(12,4),                          -- 回购均价
    amount          NUMERIC(20,2),                          -- 回购金额(元)
    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, buyback_date)
);

COMMENT ON TABLE  daily_buyback_event               IS '公司回购单次记录（数据源：东方财富港股回购页面）';
COMMENT ON COLUMN daily_buyback_event.stock_code    IS '股票完整代码，如 HK.00700（仅支持港股）';
COMMENT ON COLUMN daily_buyback_event.buyback_date  IS '实际回购发生日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_buyback_event.volume        IS '该次回购数量（股）';
COMMENT ON COLUMN daily_buyback_event.high_price    IS '该次回购最高成交价';
COMMENT ON COLUMN daily_buyback_event.low_price     IS '该次回购最低成交价';
COMMENT ON COLUMN daily_buyback_event.avg_price     IS '该次回购均价（成交金额/成交数量）';
COMMENT ON COLUMN daily_buyback_event.amount        IS '该次回购总金额（元/港元）';
COMMENT ON COLUMN daily_buyback_event.created_at    IS '数据写入数据库的时间';

CREATE INDEX idx_buyback_event_stock_date ON daily_buyback_event (stock_code, buyback_date DESC);

-- 9. A股回购方案（方案维度进度快照，数据源：AKShare stock_repurchase_em）
--    与港股 daily_buyback_event（逐日明细）粒度不同，故独立成表。
--    A股不强制每日披露回购，仅有「回购方案 + 累计已回购」口径，无逐日明细。
CREATE TABLE IF NOT EXISTS a_stock_repurchase_plan (
    id                      BIGSERIAL       PRIMARY KEY,
    stock_code              VARCHAR(20)     NOT NULL,   -- 如 SH.600519 / SZ.000333
    stock_name              VARCHAR(40),
    plan_id                 VARCHAR(120)    NOT NULL,   -- 合成业务键: code|start_date|plan_amt_lo|plan_amt_hi
    progress                VARCHAR(20),                -- 实施进度: 董事会预案/股东大会通过/实施中/完成实施/停止实施
    plan_price_min          NUMERIC(12,4),             -- 计划回购价格区间-下限
    plan_price_max          NUMERIC(12,4),             -- 计划回购价格区间-上限
    plan_qty_min            BIGINT,                    -- 计划回购数量区间-下限(股)
    plan_qty_max            BIGINT,                    -- 计划回购数量区间-上限(股)
    plan_amt_min            NUMERIC(20,2),             -- 计划回购金额区间-下限(元)
    plan_amt_max            NUMERIC(20,2),             -- 计划回购金额区间-上限(元)
    start_date              DATE,                      -- 回购起始时间
    repurchased_price_min   NUMERIC(12,4),             -- 已回购股份价格区间-下限
    repurchased_price_max   NUMERIC(12,4),             -- 已回购股份价格区间-上限
    repurchased_qty         BIGINT,                    -- 已回购股份数量(累计,股)
    repurchased_amt         NUMERIC(20,2),             -- 已回购金额(累计,元)
    latest_ann_date         DATE,                      -- 最新公告日期(该快照对应日)
    created_at              TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, plan_id)
);

COMMENT ON TABLE  a_stock_repurchase_plan          IS 'A股回购方案进度快照（数据源：AKShare stock_repurchase_em，方案维度，无逐日明细）';
COMMENT ON COLUMN a_stock_repurchase_plan.stock_code    IS '股票完整代码，如 SH.600519';
COMMENT ON COLUMN a_stock_repurchase_plan.plan_id       IS '合成业务键 code|start_date|plan_amt_lo|plan_amt_hi，唯一标识一个回购方案';
COMMENT ON COLUMN a_stock_repurchase_plan.progress      IS '实施进度: 董事会预案/股东大会通过/实施中/完成实施/停止实施';
COMMENT ON COLUMN a_stock_repurchase_plan.plan_amt_max  IS '计划回购金额区间-上限(元)，代表方案拟回购力度上限';
COMMENT ON COLUMN a_stock_repurchase_plan.repurchased_amt IS '已回购金额(累计,元)，代表方案已落地力度';
COMMENT ON COLUMN a_stock_repurchase_plan.latest_ann_date IS '最新公告日期，该进度快照对应的披露日';

CREATE INDEX idx_repurchase_plan_stock ON a_stock_repurchase_plan (stock_code);
CREATE INDEX idx_repurchase_plan_ann_date ON a_stock_repurchase_plan (latest_ann_date DESC);

-- 9. 港股通持股
CREATE TABLE IF NOT EXISTS daily_ggt_hold (
    id                  BIGSERIAL       PRIMARY KEY,
    stock_code          VARCHAR(20)     NOT NULL,
    trade_date          DATE            NOT NULL,
    hold_num            BIGINT,                             -- 持股数量(股)
    hold_ratio          NUMERIC(8,4),                       -- 持股比例(%)
    hold_num_change     BIGINT,                             -- 持股数量变动(股)，正=增持，负=减持
    hold_ratio_change   NUMERIC(8,4),                       -- 持股比例变动(%)
    close_price         NUMERIC(10,3),                      -- 当日收盘价
    change_pct          NUMERIC(8,4),                       -- 当日涨跌幅(%)
    est_net_inflow      NUMERIC(16,2),                      -- 估算净流入（亿港元）= 持股变动 × 收盘价
    hold_value          NUMERIC(16,2),                      -- 持股市值（港元）
    hold_value_change_1d NUMERIC(16,2),                     -- 持股市值较1日前变动（港元）
    hold_value_change_5d NUMERIC(16,2),                     -- 持股市值较5日前变动（港元）
    hold_value_change_10d NUMERIC(16,2),                    -- 持股市值较10日前变动（港元）
    created_at          TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);

COMMENT ON TABLE  daily_ggt_hold                       IS '港股通（南向资金）个股持股明细（数据源：AKShare）';
COMMENT ON COLUMN daily_ggt_hold.stock_code            IS '股票完整代码，如 HK.00700（仅支持港股）';
COMMENT ON COLUMN daily_ggt_hold.trade_date            IS '持股数据对应的交易日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_ggt_hold.hold_num              IS '港股通（南向）合计持有该股票的数量（股）';
COMMENT ON COLUMN daily_ggt_hold.hold_ratio            IS '港股通持股占该股票总股本的比例(%)';
COMMENT ON COLUMN daily_ggt_hold.hold_num_change       IS '持股数量较上一交易日变动（股），正数=增持，负数=减持，NULL=无变动数据';
COMMENT ON COLUMN daily_ggt_hold.hold_ratio_change     IS '持股比例较上一交易日变动(%)，正数=增加，负数=减少';
COMMENT ON COLUMN daily_ggt_hold.created_at            IS '数据写入数据库的时间';

CREATE INDEX idx_ggt_hold_stock_date ON daily_ggt_hold (stock_code, trade_date DESC);

-- 10. 融资融券全量明细（A股沪深两市专用）
-- 数据来自沪深交易所逐日公布的「融资融券明细」，覆盖全市场全部标的，含融资+融券双向。
CREATE TABLE IF NOT EXISTS daily_margin_balance (
    id               BIGSERIAL       PRIMARY KEY,
    stock_code       VARCHAR(20)     NOT NULL,               -- A股代码（SH.xxxxxx / SZ.xxxxxx）
    trade_date       DATE            NOT NULL,
    rz_balance       NUMERIC(18,2),                          -- 融资余额（元）
    rz_buy           NUMERIC(18,2),                          -- 融资买入额（元）
    rz_repay         NUMERIC(18,2),                          -- 融资偿还额（元）
    rz_net           NUMERIC(18,2),                          -- 融资净买入（元）= 融资买入 - 融资偿还
    rq_balance       NUMERIC(18,2),                          -- 融券余额（元）
    rq_sell          NUMERIC(18,2),                          -- 融券卖出量（股）
    rq_repay         NUMERIC(18,2),                          -- 融券偿还量（股）
    rzrq_balance     NUMERIC(18,2),                          -- 融资融券余额合计（元）
    created_at       TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_margin_trade_date ON public.daily_margin_balance USING btree (trade_date);

COMMENT ON TABLE  daily_margin_balance                IS '融资融券全量日度明细（仅支持A股沪深两市，数据源：AKShare沪深交易所融资融券明细）';
COMMENT ON COLUMN daily_margin_balance.stock_code     IS 'A股完整代码，如 SH.600519 / SZ.000001';
COMMENT ON COLUMN daily_margin_balance.trade_date     IS '交易日期 (YYYY-MM-DD)';
COMMENT ON COLUMN daily_margin_balance.rz_balance     IS '当日融资余额（元）';
COMMENT ON COLUMN daily_margin_balance.rz_buy         IS '当日融资买入额（元）';
COMMENT ON COLUMN daily_margin_balance.rz_repay       IS '当日融资偿还额（元）';
COMMENT ON COLUMN daily_margin_balance.rz_net         IS '融资净买入（元）= 融资买入额 - 融资偿还额，正数=净增加(看多)';
COMMENT ON COLUMN daily_margin_balance.rq_balance     IS '当日融券余额（元）';
COMMENT ON COLUMN daily_margin_balance.rq_sell        IS '当日融券卖出量（股）';
COMMENT ON COLUMN daily_margin_balance.rq_repay       IS '当日融券偿还量（股）';
COMMENT ON COLUMN daily_margin_balance.rzrq_balance   IS '融资融券余额合计（元）= 融资余额 + 融券余额';
COMMENT ON COLUMN daily_margin_balance.created_at     IS '数据写入数据库的时间';

CREATE INDEX idx_margin_stock_date ON daily_margin_balance (stock_code, trade_date DESC);

-- 11. 趋势技术指标（均线/MACD/RSI）
CREATE TABLE IF NOT EXISTS daily_trend (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,
    ma5             NUMERIC(12,4),                      -- 5日均线
    ma10            NUMERIC(12,4),                      -- 10日均线
    ma20            NUMERIC(12,4),                      -- 20日均线
    ma60            NUMERIC(12,4),                      -- 60日均线
    macd_dif        NUMERIC(12,4),                      -- MACD DIF值
    macd_dea        NUMERIC(12,4),                      -- MACD DEA值
    macd_hist       NUMERIC(12,4),                      -- MACD柱状值 = 2*(DIF-DEA)
    rsi14           NUMERIC(8,4),                       -- 14日RSI
    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);

COMMENT ON TABLE  daily_trend                    IS '每日趋势技术指标（均线/MACD/RSI，数据源：富途OpenAPI日K线计算）';
COMMENT ON COLUMN daily_trend.stock_code         IS '股票完整代码';
COMMENT ON COLUMN daily_trend.trade_date         IS '计算指标所用的最近交易日';
COMMENT ON COLUMN daily_trend.ma5               IS '5日均线（前复权收盘价简单移动平均）';
COMMENT ON COLUMN daily_trend.ma10              IS '10日均线';
COMMENT ON COLUMN daily_trend.ma20              IS '20日均线';
COMMENT ON COLUMN daily_trend.ma60              IS '60日均线';
COMMENT ON COLUMN daily_trend.macd_dif          IS 'MACD DIF = EMA12 - EMA26';
COMMENT ON COLUMN daily_trend.macd_dea          IS 'MACD DEA = DIF的9日EMA';
COMMENT ON COLUMN daily_trend.macd_hist         IS 'MACD柱状线 = 2*(DIF-DEA)，正值看涨，负值看跌';
COMMENT ON COLUMN daily_trend.rsi14             IS '14日相对强弱指标(0-100)，>70超买，<30超卖';
COMMENT ON COLUMN daily_trend.created_at        IS '数据写入数据库的时间';

CREATE INDEX idx_trend_stock_date ON daily_trend (stock_code, trade_date DESC);

-- ============================================================================
-- 第三部分：盘中实时数据表（交易时段多次采集）
-- ============================================================================

-- 12. 大小单资金流向（盘中实时）
CREATE TABLE IF NOT EXISTS realtime_order_size (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    snapshot_time   TIMESTAMPTZ     NOT NULL,
    last_price      NUMERIC(12,4),
    large_in_flow   NUMERIC(16,2),                          -- 大单合计净流入(亿元)：特大+大
    super_in_flow   NUMERIC(16,2),                          -- 特大单流入(亿元)
    super_out_flow  NUMERIC(16,2),                          -- 特大单流出(亿元)
    super_net       NUMERIC(16,2),                          -- 特大单净流入(亿元)
    big_in_flow     NUMERIC(16,2),                          -- 大单流入(亿元)
    big_out_flow    NUMERIC(16,2),                          -- 大单流出(亿元)
    big_net         NUMERIC(16,2),                          -- 大单净流入(亿元)
    small_total     NUMERIC(16,2),                          -- 中小单合计净流入(亿元)：中+小
    mid_in_flow     NUMERIC(16,2),                          -- 中单流入(亿元)
    mid_out_flow    NUMERIC(16,2),                          -- 中单流出(亿元)
    mid_net         NUMERIC(16,2),                          -- 中单净流入(亿元)
    small_in_flow   NUMERIC(16,2),                          -- 小单流入(亿元)
    small_out_flow  NUMERIC(16,2),                          -- 小单流出(亿元)
    small_net       NUMERIC(16,2),                          -- 小单净流入(亿元)
    direction       VARCHAR(50),                            -- 资金方向描述
    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, snapshot_time)
);

COMMENT ON TABLE  realtime_order_size                  IS '盘中实时大单/小单资金流向分化（数据源：富途 OpenAPI get_capital_distribution）';
COMMENT ON COLUMN realtime_order_size.stock_code       IS '股票完整代码，如 HK.00700';
COMMENT ON COLUMN realtime_order_size.snapshot_time    IS '数据快照时间戳（含时区），盘中每隔数秒/数分钟获取一次';
COMMENT ON COLUMN realtime_order_size.last_price       IS '快照时刻的最新成交价';
COMMENT ON COLUMN realtime_order_size.large_in_flow    IS '大单合计净流入（亿元）= 特大单净流入 + 大单净流入';
COMMENT ON COLUMN realtime_order_size.super_in_flow    IS '特大单流入（亿元），原始值来自富途 API capital_in_super';
COMMENT ON COLUMN realtime_order_size.super_out_flow   IS '特大单流出（亿元），原始值来自富途 API capital_out_super';
COMMENT ON COLUMN realtime_order_size.super_net        IS '特大单净流入（亿元）= 特大单流入 - 特大单流出，代表机构主力动向';
COMMENT ON COLUMN realtime_order_size.big_in_flow      IS '大单流入（亿元），原始值来自富途 API capital_in_big';
COMMENT ON COLUMN realtime_order_size.big_out_flow     IS '大单流出（亿元），原始值来自富途 API capital_out_big';
COMMENT ON COLUMN realtime_order_size.big_net          IS '大单净流入（亿元）= 大单流入 - 大单流出';
COMMENT ON COLUMN realtime_order_size.small_total      IS '中小单合计净流入（亿元）= 中单净流入 + 小单净流入，代表散户动向';
COMMENT ON COLUMN realtime_order_size.mid_in_flow      IS '中单流入（亿元），原始值来自富途 API capital_in_mid';
COMMENT ON COLUMN realtime_order_size.mid_out_flow     IS '中单流出（亿元），原始值来自富途 API capital_out_mid';
COMMENT ON COLUMN realtime_order_size.mid_net          IS '中单净流入（亿元）= 中单流入 - 中单流出';
COMMENT ON COLUMN realtime_order_size.small_in_flow    IS '小单流入（亿元），原始值来自富途 API capital_in_small';
COMMENT ON COLUMN realtime_order_size.small_out_flow   IS '小单流出（亿元），原始值来自富途 API capital_out_small';
COMMENT ON COLUMN realtime_order_size.small_net        IS '小单净流入（亿元）= 小单流入 - 小单流出';
COMMENT ON COLUMN realtime_order_size.direction        IS '资金方向文字描述，如"大单与中小单均为净流入"/"大单净流入，中小单净流出"等';
COMMENT ON COLUMN realtime_order_size.created_at       IS '数据写入数据库的时间';

CREATE INDEX idx_order_size_stock_time ON realtime_order_size (stock_code, snapshot_time DESC);

-- ============================================================================
-- 第四部分：趋势快照表（每3分钟采样，多维度聚合）
-- ============================================================================

-- 13. 趋势快照（盘中每1分钟聚合采样）
CREATE TABLE IF NOT EXISTS trend_snapshot (
    id                  BIGSERIAL       PRIMARY KEY,
    stock_code          VARCHAR(20)     NOT NULL,
    snapshot_time       TIMESTAMPTZ     NOT NULL,           -- 采样时间点（如 2026-06-02 13:06:00 CST）
    snapshot_date       DATE            GENERATED ALWAYS AS ((snapshot_time AT TIME ZONE 'Asia/Hong_Kong')::DATE) STORED,
    price               NUMERIC(12,4),                      -- 最新价
    super_in_net        NUMERIC(16,2),                      -- 特大单净流入(亿元)
    big_in_net          NUMERIC(16,2),                      -- 大单净流入(亿元)
    mid_in_net          NUMERIC(16,2),                      -- 中单净流入(亿元)
    small_in_net        NUMERIC(16,2),                      -- 小单净流入(亿元)
    buy_sell_ratio      NUMERIC(8,4),                       -- 主动买卖比
    excess_return_pct   NUMERIC(8,4),                       -- 超额收益(%)
    volume              BIGINT,                              -- 累计成交量（股）
    turnover            NUMERIC(16,2),                      -- 累计成交额（亿元）
    buy_levels_str      TEXT,                               -- 买盘5档摘要: "472.8(6K) 472.6(14K) ..."
    sell_levels_str     TEXT,                               -- 卖盘5档摘要: "473.0(13K) 473.2(32K) ..."
    created_at          TIMESTAMPTZ     DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     DEFAULT NOW(),           -- 实时推送刷新时间，前端据此判断数据是否更新

    UNIQUE (stock_code, snapshot_time)
);

COMMENT ON TABLE  trend_snapshot                      IS '盘中趋势快照（每3分钟一次，聚合价格/资金/盘口/超额收益等多维实时数据）';
COMMENT ON COLUMN trend_snapshot.stock_code           IS '股票完整代码，如 HK.00700';
COMMENT ON COLUMN trend_snapshot.snapshot_time        IS '采样时间点（含时区），交易时段内每3分钟记录一次';
COMMENT ON COLUMN trend_snapshot.snapshot_date        IS '生成列 = snapshot_time 转 Asia/Hong_Kong 时区的日期，用于按日索引查询';
COMMENT ON COLUMN trend_snapshot.price                IS '采样时刻的最新成交价，NULL=该时刻无有效行情数据';
COMMENT ON COLUMN trend_snapshot.super_in_net         IS '特大单净流入（亿元），NULL=该时刻无资金流向数据';
COMMENT ON COLUMN trend_snapshot.big_in_net           IS '大单净流入（亿元）';
COMMENT ON COLUMN trend_snapshot.mid_in_net           IS '中单净流入（亿元）';
COMMENT ON COLUMN trend_snapshot.small_in_net         IS '小单净流入（亿元）';
COMMENT ON COLUMN trend_snapshot.buy_sell_ratio       IS '主动买卖比 = 主动性买盘股数 / 主动性卖盘股数，>1=主动买入占优，<1=主动卖出占优';
COMMENT ON COLUMN trend_snapshot.excess_return_pct    IS '实时超额收益(%) = 个股当日涨跌幅 - 基准指数当日涨跌幅';
COMMENT ON COLUMN trend_snapshot.volume               IS '累计成交量（股），从 get_market_snapshot 获取，NULL=该时刻无数据';
COMMENT ON COLUMN trend_snapshot.turnover             IS '累计成交额（亿元），从 get_market_snapshot 获取，NULL=该时刻无数据';
COMMENT ON COLUMN trend_snapshot.buy_levels_str       IS '买盘前5档摘要字符串，格式: "472.8(6K) 472.6(14K) ..."，括号内K=千股';
COMMENT ON COLUMN trend_snapshot.sell_levels_str      IS '卖盘前5档摘要字符串，格式: "473.0(13K) 473.2(32K) ..."，可据此分析盘口挂单压力';
COMMENT ON COLUMN trend_snapshot.created_at           IS '数据写入数据库的时间';

CREATE INDEX idx_trend_snapshot_stock_time ON trend_snapshot (stock_code, snapshot_time DESC);
CREATE INDEX idx_trend_snapshot_date ON trend_snapshot (snapshot_date);
CREATE INDEX idx_trend_snapshot_stock_updated ON trend_snapshot (stock_code, updated_at DESC);
-- 监控 monitor_collector 的 MAX(snapshot_time) WHERE snapshot_time>=窗口 查询用：
-- 复合索引 (stock_code, snapshot_time) 中 snapshot_time 非前导列，无法用于单独范围查询，
-- 故加单列索引，使该查询走 Index Only Scan Backward（~1ms）而非全表扫。
CREATE INDEX idx_trend_snapshot_time ON trend_snapshot (snapshot_time);

-- ============================================================================
-- 第五部分：采集运行日志
-- ============================================================================

-- 14. 数据采集运行日志
CREATE TABLE IF NOT EXISTS collection_run_log (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    run_time        TIMESTAMPTZ     DEFAULT NOW(),          -- 采集开始时间
    end_time        TIMESTAMPTZ,                            -- 采集结束时间
    status          VARCHAR(20)     DEFAULT 'running',      -- running/success/partial/failed
    total_modules   INT,                                    -- 总模块数
    success_modules INT,                                    -- 成功模块数
    failed_modules  INT,                                    -- 失败模块数
    error_detail    TEXT,                                   -- 错误详情(JSON)
    trigger_type    VARCHAR(20)     DEFAULT 'manual',       -- 触发方式: manual/scheduled/wecom_cmd
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

COMMENT ON TABLE  collection_run_log                  IS '数据采集运行日志（记录每次采集任务的执行情况）';
COMMENT ON COLUMN collection_run_log.stock_code       IS '本次采集的目标股票代码';
COMMENT ON COLUMN collection_run_log.run_time         IS '采集任务开始执行的时间';
COMMENT ON COLUMN collection_run_log.end_time         IS '采集任务结束的时间，NULL=运行中或异常终止';
COMMENT ON COLUMN collection_run_log.status           IS '执行状态：running(运行中) / success(全部成功) / partial(部分失败) / failed(全部失败)';
COMMENT ON COLUMN collection_run_log.total_modules    IS '本次采集计划执行的数据模块总数';
COMMENT ON COLUMN collection_run_log.success_modules  IS '采集成功的模块数量';
COMMENT ON COLUMN collection_run_log.failed_modules   IS '采集失败的模块数量';
COMMENT ON COLUMN collection_run_log.error_detail     IS '失败模块的错误详情，JSON格式，如 {"get_cbbc":"下载失败"}';
COMMENT ON COLUMN collection_run_log.trigger_type     IS '触发方式：manual(手动) / scheduled(定时器) / wecom_cmd(企业微信指令)';
COMMENT ON COLUMN collection_run_log.created_at       IS '记录创建时间';

CREATE INDEX idx_collection_run_stock_time ON collection_run_log (stock_code, run_time DESC);

-- ============================================================================
-- 第六部分：逐笔成交数据
-- ============================================================================

-- 16. 逐笔成交明细（富途 TICKER 推送实时采集）
CREATE TABLE IF NOT EXISTS tick_data (
    id              BIGSERIAL,                              -- 自增代理键（刻意不设 PRIMARY KEY：去重由 sequence 唯一约束保证，避免冗余索引拖累批量写入）
    stock_code      VARCHAR(20)     NOT NULL,               -- 股票代码，如 HK.00700
    tick_time       TIMESTAMPTZ     NOT NULL,               -- 逐笔成交时间（含毫秒）
    price           NUMERIC(12,4),                          -- 成交价
    volume          BIGINT,                                 -- 成交量（股）
    turnover        NUMERIC(20,2),                          -- 成交额（港元）
    ticker_direction VARCHAR(10),                           -- 买卖方向：BUY/SELL/NEUTRAL
    sequence        BIGINT          NOT NULL,               -- 富途逐笔序号（同一时刻跨股票共享，非 per-stock 唯一）
    -- 复合唯一键 (stock_code, sequence)：富途 sequence 是"同一时刻跨股票共享的包序号"，
    -- 并非单票唯一，若仅以 sequence 单列 UNIQUE 会因其他股票抢键导致本票数据被静默丢弃。
    CONSTRAINT tick_data_stock_seq_unique UNIQUE (stock_code, sequence),
    tick_type       VARCHAR(20),                            -- 成交类型：AUTO_MATCH/AUCTION/...
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

COMMENT ON TABLE  tick_data                        IS '逐笔成交明细（富途 TICKER 推送实时采集）';
COMMENT ON COLUMN tick_data.stock_code             IS '股票完整代码，如 HK.00700';
COMMENT ON COLUMN tick_data.tick_time              IS '逐笔成交发生时间（含时区，精确到毫秒）';
COMMENT ON COLUMN tick_data.price                  IS '成交价格';
COMMENT ON COLUMN tick_data.volume                 IS '该笔成交股数';
COMMENT ON COLUMN tick_data.turnover               IS '该笔成交金额（港元）';
COMMENT ON COLUMN tick_data.ticker_direction       IS '买卖方向：BUY=主动买入, SELL=主动卖出, NEUTRAL=中性/不明';
COMMENT ON COLUMN tick_data.sequence               IS '富途全局唯一逐笔序号，用于幂等去重（重连补偿数据自动跳过）';
COMMENT ON COLUMN tick_data.tick_type               IS '成交类型：AUTO_MATCH(自动对盘), AUCTION(竞价), ODD_LOT(碎股) 等';
COMMENT ON COLUMN tick_data.created_at             IS '数据写入数据库的时间';

CREATE INDEX idx_tick_data_stock_time ON tick_data (stock_code, tick_time DESC);
CREATE INDEX idx_tick_data_time       ON tick_data (tick_time);
-- stock-realtime 高频聚合查询加速：覆盖分钟聚合 / 日粒度指纹 / 四档大单统计 / 量能统计，
-- WHERE 均为 stock_code + tick_time 范围 + ticker_direction 过滤。INCLUDE 使其成为 Index Only Scan。
CREATE INDEX idx_tick_data_stock_time_dir
  ON tick_data (stock_code, tick_time, ticker_direction)
  INCLUDE (turnover, volume, price);

-- 16.1 全量逐笔旁路落盘表（诊断用，默认不写入）
-- 富途推送的每一笔逐笔都无去重原样写入本表（由配置 full_tick_capture 控制开关），
-- 没有 sequence 唯一约束，重复推送全部保留。事后可与 tick_data 比对，定位
-- "推送了但 tick_data 没入库"（被去重跳过 / 缺失）的问题。开启会产生大量写入与磁盘占用，
-- 仅排查时临时打开，定位完即关闭并 truncate。
CREATE TABLE IF NOT EXISTS full_tick_data (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    tick_time       TIMESTAMPTZ     NOT NULL,
    price           NUMERIC(12,4),
    volume          BIGINT,
    turnover        NUMERIC(20,2),
    ticker_direction VARCHAR(10),
    sequence        BIGINT          NOT NULL,               -- 富途逐笔序号（与 tick_data.sequence 同义，本表不约束唯一）
    tick_type       VARCHAR(20),
    received_at     TIMESTAMPTZ     DEFAULT NOW()           -- 本批落盘时间，用于还原推送时刻
);

COMMENT ON TABLE  full_tick_data           IS '富途逐笔全量旁路落盘（无去重），诊断"推了但 tick_data 没落"问题用，平时为空';
COMMENT ON COLUMN full_tick_data.sequence   IS '富途逐笔序号，与 tick_data.sequence 同义，本表不唯一';
COMMENT ON COLUMN full_tick_data.received_at IS '落盘时间，约等价收到批次时刻';

CREATE INDEX idx_full_tick_seq        ON full_tick_data (sequence);
CREATE INDEX idx_full_tick_stock_time ON full_tick_data (stock_code, tick_time);
CREATE INDEX idx_full_tick_received   ON full_tick_data (received_at);


-- ============================================================================
-- 第七部分：量化交易行为标签
-- ============================================================================

-- 18. 逐笔量化标签（tick_data 所有列 + 量化特征评分）
CREATE TABLE IF NOT EXISTS tick_quant_detail (
    id              BIGSERIAL       PRIMARY KEY,
    -- 来自 tick_data 的字段
    stock_code      VARCHAR(20)     NOT NULL,
    tick_time       TIMESTAMPTZ     NOT NULL,
    price           NUMERIC(12,4),
    volume          BIGINT,
    turnover        NUMERIC(20,2),
    ticker_direction VARCHAR(10),
    sequence        BIGINT          NOT NULL,
    tick_type       VARCHAR(20),
    -- 量化特征评分
    quant_score     DOUBLE PRECISION NOT NULL,      -- 加权总分 0~1
    f1_speed        DOUBLE PRECISION NOT NULL,      -- F1 速度分（sigmoid）
    f2_burst        DOUBLE PRECISION NOT NULL,      -- F2 爆发密度（时段归一化）
    f4_slice        DOUBLE PRECISION NOT NULL,      -- F4 拆单切片（burst 长度）
    is_quant        BOOLEAN         NOT NULL,       -- quant_score >= 0.5
    -- 元数据
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quant_detail_time ON public.tick_quant_detail USING btree (stock_code, tick_time);

COMMENT ON TABLE  tick_quant_detail                      IS '逐笔量化行为标签表（基于 tick_data + 3 维特征评分），自包含分析表，无需 JOIN tick_data';
COMMENT ON COLUMN tick_quant_detail.sequence             IS '关联 tick_data.sequence，同一笔成交';
COMMENT ON COLUMN tick_quant_detail.quant_score          IS '量化综合分（F1*0.50 + F2*0.35 + F4*0.15），0~1';
COMMENT ON COLUMN tick_quant_detail.f1_speed             IS 'F1 速度分：前向间隔 sigmoid 映射，中心 300ms';
COMMENT ON COLUMN tick_quant_detail.f2_burst             IS 'F2 爆发密度：±5s 窗口 / 15分钟块均值，时段归一化';
COMMENT ON COLUMN tick_quant_detail.f4_slice             IS 'F4 拆单切片：连续同向同股数 burst 长度，指数衰减映射';
COMMENT ON COLUMN tick_quant_detail.is_quant             IS '是否疑似量化（quant_score >= 0.5）';

CREATE INDEX idx_quant_detail_stock_time ON tick_quant_detail (stock_code, tick_time);
CREATE INDEX idx_quant_detail_score     ON tick_quant_detail (stock_code, quant_score);
CREATE INDEX idx_quant_detail_is_quant  ON tick_quant_detail (stock_code, is_quant);

-- ============================================================================
-- 第九部分：趋势分段
-- ============================================================================

-- 19. 趋势分段（两层切分结果，作为 tick_data 行为分析的骨架）
CREATE TABLE IF NOT EXISTS trend_segment (
    id              BIGSERIAL       PRIMARY KEY,
    stock_code      VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,

    -- 段层级
    l1_seg_idx      INT             NOT NULL,       -- L1方向段序号
    l1_direction    VARCHAR(10)     NOT NULL,       -- 上涨/下跌/横盘
    l2_seg_idx      INT             NOT NULL,       -- L2子段在父段内序号
    l2_global_idx   INT             NOT NULL,       -- L2全局序号
    l2_rhythm       VARCHAR(20)     NOT NULL,       -- 急跌/缓跌/加速跌/减速跌/停顿/反弹/回调/急涨/缓涨/加速涨/减速涨/波动

    -- 时间边界（精确到 trend_snapshot 时间戳，用于 JOIN tick_data）
    start_time      TIMESTAMPTZ     NOT NULL,       -- tick_time >= start_time
    end_time        TIMESTAMPTZ     NOT NULL,       -- tick_time < end_time
    duration_min    INT             NOT NULL,       -- 段时长（快照点数）

    -- 段内价格特征（从 trend_snapshot.price 提取）
    start_price     NUMERIC(12,4),                  -- 段首价
    end_price       NUMERIC(12,4),                  -- 段尾价
    high_price      NUMERIC(12,4),                  -- 段内最高价
    low_price       NUMERIC(12,4),                  -- 段内最低价
    change_pct      NUMERIC(8,4),                   -- 涨跌幅%
    slope_pct_min   NUMERIC(8,4),                   -- 斜率%/分钟

    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE(stock_code, trade_date, l2_global_idx)
);

COMMENT ON TABLE  trend_segment                      IS '趋势分段（两层切分：L1 ZigZag方向 + L2 PELT节奏），作为 tick_data 行为分析的骨架';
COMMENT ON COLUMN trend_segment.l1_seg_idx           IS 'L1方向段序号（从1起，跨天不连续）';
COMMENT ON COLUMN trend_segment.l1_direction         IS '方向：上涨/下跌/横盘';
COMMENT ON COLUMN trend_segment.l2_seg_idx           IS 'L2子段在父段内的序号（从1起）';
COMMENT ON COLUMN trend_segment.l2_global_idx        IS 'L2全局序号（从1起，一天内连续）';
COMMENT ON COLUMN trend_segment.l2_rhythm            IS '节奏标签：急跌/缓跌/加速跌/减速跌/停顿/反弹/回调/急涨/缓涨/加速涨/减速涨/波动';
COMMENT ON COLUMN trend_segment.start_time           IS '段起始时间（精确到微秒），tick_data 关联条件: tick_time >= start_time';
COMMENT ON COLUMN trend_segment.end_time             IS '段结束时间（精确到微秒），tick_data 关联条件: tick_time < end_time';
COMMENT ON COLUMN trend_segment.duration_min         IS '段时长（trend_snapshot 采样点数）';
COMMENT ON COLUMN trend_segment.start_price          IS '段起始时刻价格';
COMMENT ON COLUMN trend_segment.end_price            IS '段结束时刻价格';
COMMENT ON COLUMN trend_segment.high_price           IS '段内最高价';
COMMENT ON COLUMN trend_segment.low_price            IS '段内最低价';
COMMENT ON COLUMN trend_segment.change_pct           IS '段涨跌幅(%)';
COMMENT ON COLUMN trend_segment.slope_pct_min        IS '段内价格斜率(%/分钟)';

CREATE INDEX idx_trend_seg_stock_date ON trend_segment(stock_code, trade_date);
CREATE INDEX idx_trend_seg_time      ON trend_segment(start_time, end_time);
CREATE INDEX idx_trend_seg_rhythm    ON trend_segment(stock_code, trade_date, l2_rhythm);


-- ============================================================================
-- 第十部分：财务指标（年度）
-- ============================================================================

-- 20. 年度财务指标
CREATE TABLE IF NOT EXISTS financial_indicator (
    id                  BIGSERIAL       PRIMARY KEY,
    stock_code          VARCHAR(20)     NOT NULL,
    report_date         DATE            NOT NULL,           -- 报告期截止日（如 2025-12-31）
    report_type         VARCHAR(10)     DEFAULT 'annual',   -- 报告类型：annual(年报)
    revenue             NUMERIC(18,2),                      -- 营业收入（元）
    net_profit          NUMERIC(18,2),                      -- 归母净利润（元）
    gross_profit_rate   NUMERIC(12,4),                      -- 毛利率(%)
    net_profit_rate     NUMERIC(12,4),                      -- 销售净利率(%)
    roe                 NUMERIC(12,4),                      -- ROE 加权净资产收益率(%)
    debt_ratio          NUMERIC(12,4),                      -- 资产负债率(%)
    revenue_yoy         NUMERIC(12,4),                      -- 营业收入同比增长率(%)
    net_profit_yoy      NUMERIC(12,4),                      -- 归母净利润同比增长率(%)
    operating_cash_flow NUMERIC(18,2),                      -- 经营活动现金流量净额（元）
    free_cash_flow      NUMERIC(18,2),                      -- 自由现金流（元），暂留空，待补充 CAPEX
    created_at          TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, report_date)
);

COMMENT ON TABLE  financial_indicator                          IS '年度财务指标（A股：AKShare stock_financial_abstract；港股：stock_financial_hk_analysis_indicator_em，OCF 由 OCF_SALES×OPERATE_INCOME 推算）';
COMMENT ON COLUMN financial_indicator.stock_code               IS '股票完整代码，如 HK.00700 / SH.600519';
COMMENT ON COLUMN financial_indicator.report_date              IS '报告期截止日 (YYYY-MM-DD)，年报为 12-31';
COMMENT ON COLUMN financial_indicator.report_type              IS '报告类型，暂时只存 annual 年报，后续可扩展 Q1/Q2/H1/Q3';
COMMENT ON COLUMN financial_indicator.revenue                  IS '营业收入（元）';
COMMENT ON COLUMN financial_indicator.net_profit               IS '归母净利润（元）';
COMMENT ON COLUMN financial_indicator.gross_profit_rate        IS '毛利率(%)，毛利率 = (营收-营业成本)/营业成本';
COMMENT ON COLUMN financial_indicator.net_profit_rate          IS '销售净利率(%)，净利率 = 净利润/营收';
COMMENT ON COLUMN financial_indicator.roe                      IS '加权净资产收益率 ROE(%)';
COMMENT ON COLUMN financial_indicator.debt_ratio               IS '资产负债率(%)';
COMMENT ON COLUMN financial_indicator.revenue_yoy              IS '营业收入同比增长率(%)';
COMMENT ON COLUMN financial_indicator.net_profit_yoy           IS '归母净利润同比增长率(%)';
COMMENT ON COLUMN financial_indicator.operating_cash_flow      IS '经营活动现金流量净额（元）。港股由 OCF_SALES% × OPERATE_INCOME 推算，A股直接取自抽象表';
COMMENT ON COLUMN financial_indicator.free_cash_flow           IS '自由现金流（元），暂留空 NULL，后续补充 CAPEX 数据后计算（FCF = OCF - CAPEX）';
COMMENT ON COLUMN financial_indicator.created_at               IS '数据写入数据库的时间';

CREATE INDEX idx_financial_indicator_stock_date ON financial_indicator (stock_code, report_date DESC);

-- 21. 港股全市场（主板）总成交额快照
CREATE TABLE IF NOT EXISTS daily_market_turnover (
    id              BIGSERIAL       PRIMARY KEY,
    trade_date      DATE            NOT NULL,           -- 数据日期（交易日，T+1 采集则为前一日）
    snapshot_time   TIMESTAMPTZ     NOT NULL,           -- 快照时间（采集时刻）
    total_turnover  NUMERIC(20,2),                      -- 全市场总成交额（港元）
    total_volume    BIGINT,                             -- 全市场总成交量（股）
    stock_count     INT,                                -- 参与统计的标的数量
    created_at      TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (trade_date)
);
CREATE UNIQUE INDEX uq_market_turnover_tradedate ON public.daily_market_turnover USING btree (trade_date);

COMMENT ON TABLE  daily_market_turnover                     IS '港股全市场总成交额（数据源：新浪 stock_hk_daily 逐只聚合，T+1 全天完整值）';
COMMENT ON COLUMN daily_market_turnover.snapshot_time       IS '快照时间戳（日频=盘后一次，分钟级=每分钟一次）';
COMMENT ON COLUMN daily_market_turnover.total_turnover      IS '全市场总成交额（港元），SUM(个股成交额)';
COMMENT ON COLUMN daily_market_turnover.total_volume        IS '全市场总成交量（股），SUM(个股成交量)';
COMMENT ON COLUMN daily_market_turnover.stock_count         IS '参与聚合的标的数量（用于校验是否拉全）';
COMMENT ON COLUMN daily_market_turnover.created_at          IS '数据写入数据库的时间';

CREATE INDEX idx_market_turnover_time ON daily_market_turnover (snapshot_time DESC);

-- 22. 港股全量股票简版日线（数据池）
CREATE TABLE IF NOT EXISTS hk_daily_quote (
    id          BIGSERIAL       PRIMARY KEY,
    stock_code  VARCHAR(20)     NOT NULL,           -- 如 HK.00700
    trade_date  DATE            NOT NULL,           -- 交易日
    open        NUMERIC(12,4),                      -- 开盘价
    high        NUMERIC(12,4),                      -- 最高价
    low         NUMERIC(12,4),                      -- 最低价
    close       NUMERIC(12,4),                      -- 收盘价
    volume      BIGINT,                             -- 成交量（股）
    amount      NUMERIC(20,2),                      -- 成交额（港元）
    turnover_rate       NUMERIC(8,4),               -- 换手率(%)
    volume_ratio        NUMERIC(8,4),               -- 量比
    high_52w            NUMERIC(12,4),              -- 52周最高价
    low_52w             NUMERIC(12,4),              -- 52周最低价
    total_market_val    NUMERIC(20,2),              -- 总市值（港元）
    circular_market_val NUMERIC(20,2),              -- 流通市值（港元）
    pe_ratio            NUMERIC(12,4),              -- 市盈率(静态)
    pe_ttm_ratio        NUMERIC(12,4),              -- 市盈率(TTM)
    pb_ratio            NUMERIC(12,4),              -- 市净率
    dividend_ratio_ttm  NUMERIC(8,4),               -- 股息率(TTM, %)
    update_time         TIMESTAMPTZ,                -- 数据更新时间（富途快照的 update_time）
    created_at  TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, trade_date)
);

COMMENT ON TABLE  hk_daily_quote             IS '港股全量股票简版日线数据池（数据源：富途 get_market_snapshot 快照 / request_history_kline 历史）';
COMMENT ON COLUMN hk_daily_quote.stock_code  IS '股票代码，如 HK.00700';
COMMENT ON COLUMN hk_daily_quote.amount      IS '成交额（港元），全天完整值';
COMMENT ON COLUMN hk_daily_quote.update_time IS '数据更新时间（富途快照的 update_time；历史回溯则为交易日）';

CREATE INDEX idx_hk_daily_quote_date  ON hk_daily_quote (trade_date DESC);
CREATE INDEX idx_hk_daily_quote_stock ON hk_daily_quote (stock_code, trade_date DESC);

-- ============================================================================
-- 第十一部分：辅助视图
-- ============================================================================

-- 视图：逐笔成交按日聚合（用于筹码分布等高精度分析）
CREATE OR REPLACE VIEW v_tick_daily_agg AS
SELECT
    stock_code,
    tick_time::DATE                                 AS trade_date,
    COUNT(*)                                        AS tick_count,
    SUM(volume)                                     AS total_volume,
    SUM(turnover)                                   AS total_turnover,
    SUM(CASE WHEN ticker_direction = 'BUY'  THEN volume ELSE 0 END)  AS buy_volume,
    SUM(CASE WHEN ticker_direction = 'SELL' THEN volume ELSE 0 END)  AS sell_volume,
    MIN(price)                                      AS low_price,
    MAX(price)                                      AS high_price,
    AVG(price)                                      AS avg_price
FROM tick_data
GROUP BY stock_code, tick_time::DATE
ORDER BY stock_code, tick_time::DATE DESC;

COMMENT ON VIEW v_tick_daily_agg IS '逐笔成交按日聚合视图：按日汇总逐笔数据，可对比例行 daily_kline 验证数据完整性';


-- 视图：股票最新行情快照
CREATE OR REPLACE VIEW v_latest_quote AS
SELECT DISTINCT ON (stock_code)
    stock_code, trade_date, last_price, open_price, high_price, low_price,
    prev_close, change_pct, volume, turnover, turnover_rate, volume_ratio,
    high_52w, low_52w, update_time
FROM daily_quote
ORDER BY stock_code, trade_date DESC;

COMMENT ON VIEW v_latest_quote IS '各股票最新交易日行情快照，取 daily_quote 中每只股票 trade_date 最大的一行';

-- 视图：单日超额收益（基于 daily_quote + daily_benchmark 动态计算）
CREATE OR REPLACE VIEW v_daily_excess_return AS
SELECT
    q.stock_code,
    q.trade_date,
    q.change_pct                        AS stock_change_pct,
    b.change_pct                        AS bench_change_pct,
    q.change_pct - b.change_pct         AS excess_return_pct
FROM daily_quote q
JOIN daily_benchmark b ON b.trade_date = q.trade_date
WHERE b.bench_code = CASE
    WHEN q.stock_code LIKE 'HK.%' THEN 'HK.800000'
    WHEN q.stock_code LIKE 'SH.%' THEN 'SH.000001'
    WHEN q.stock_code LIKE 'SZ.%' THEN 'SZ.399001'
END;

COMMENT ON VIEW v_daily_excess_return IS '单日超额收益 = 个股涨跌幅 - 对应市场基准指数涨跌幅，自动根据股票代码前缀匹配基准';

-- 视图：股票最新趋势快照
CREATE OR REPLACE VIEW v_latest_trend_snapshot AS
SELECT DISTINCT ON (stock_code)
    stock_code, snapshot_time, price, super_in_net, big_in_net, small_in_net,
    buy_sell_ratio, excess_return_pct, buy_levels_str, sell_levels_str
FROM trend_snapshot
ORDER BY stock_code, snapshot_time DESC;

COMMENT ON VIEW v_latest_trend_snapshot IS '各股票最新盘中趋势快照，取 trend_snapshot 中每只股票 snapshot_time 最大的一行';

-- 视图：季度单季财务指标（累计值自动拆解为 Q1/Q2/Q3/Q4 单季）
CREATE OR REPLACE VIEW v_financial_quarterly AS
WITH base AS (
    SELECT
        stock_code, report_date, report_type,
        revenue, net_profit, operating_cash_flow, free_cash_flow,
        gross_profit_rate, net_profit_rate, roe, debt_ratio,
        revenue_yoy, net_profit_yoy,
        EXTRACT(YEAR FROM report_date)::int AS report_year
    FROM financial_indicator
),
ordered AS (
    SELECT
        stock_code, report_date, report_type,
        revenue, net_profit, operating_cash_flow, free_cash_flow,
        gross_profit_rate, net_profit_rate, roe, debt_ratio,
        revenue_yoy, net_profit_yoy, report_year,
        LAG(revenue)             OVER w AS prev_revenue,
        LAG(net_profit)          OVER w AS prev_net_profit,
        LAG(operating_cash_flow) OVER w AS prev_ocf,
        LAG(free_cash_flow)      OVER w AS prev_fcf,
        LAG(report_year)         OVER w AS prev_year
    FROM base
    WINDOW w AS (PARTITION BY stock_code ORDER BY report_date)
)
SELECT
    stock_code,
    report_date,
    -- 标签转换：H1→Q2(单季)，annual→Q4(单季) 仅当同年有前序期
    CASE
        WHEN report_type = 'Q1'     THEN 'Q1'
        WHEN report_type = 'H1' AND report_year = prev_year THEN 'Q2'
        WHEN report_type = 'Q3' AND report_year = prev_year THEN 'Q3'
        WHEN report_type = 'annual' AND report_year = prev_year THEN 'Q4'
        ELSE report_type
    END AS report_type,
    -- 流量指标（可累计→可相减求单季）
    CASE WHEN report_type = 'Q1'                         THEN revenue
         WHEN report_year = prev_year                    THEN revenue - COALESCE(prev_revenue, 0)
         ELSE revenue
    END AS revenue,
    CASE WHEN report_type = 'Q1'                         THEN net_profit
         WHEN report_year = prev_year                    THEN net_profit - COALESCE(prev_net_profit, 0)
         ELSE net_profit
    END AS net_profit,
    CASE WHEN report_type = 'Q1'                         THEN operating_cash_flow
         WHEN report_year = prev_year                    THEN operating_cash_flow - COALESCE(prev_ocf, 0)
         ELSE operating_cash_flow
    END AS operating_cash_flow,
    -- FCF：Q1/Q3 原值保留，Q2/Q4 无法计算(数据源季报缺capex)，设 NULL
    CASE WHEN report_type IN ('Q1', 'Q3')                                  THEN free_cash_flow
         WHEN report_type = 'H1'     AND (report_year != prev_year OR prev_year IS NULL)  THEN free_cash_flow
         WHEN report_type = 'annual' AND (report_year != prev_year OR prev_year IS NULL)  THEN free_cash_flow
         ELSE NULL
    END AS free_cash_flow,
    -- 比率/存量指标：不可相减，保持原值
    gross_profit_rate,
    net_profit_rate,
    roe,
    debt_ratio,
    revenue_yoy,
    net_profit_yoy
FROM ordered
ORDER BY stock_code, report_date;

COMMENT ON VIEW v_financial_quarterly IS '单季财务指标视图：将 financial_indicator 中累计值(Q1/H1/Q3/annual)拆解为 Q1/Q2/Q3/Q4 单季数据。revenue/net_profit/ocf 通过同年 LAG() 相减得到；free_cash_flow 因数据源 Q1/Q3 季报缺失 capex 明细，Q2/Q4 单季无法计算设 NULL；比率指标(ROE/毛利率等)保持原值。';

-- ============================================================================
-- 第三部分：扩展采集 / 监控 / 分析中间表
-- （以下表在采集/分析脚本中以 CREATE TABLE IF NOT EXISTS 自建，未纳入原 schema，
--   此处集中补录，保证 schema.sql 为库的完整真相源）
-- ============================================================================

-- 3.1 A股市场成交额 / 行情（get_a_market_turnover.py）
CREATE TABLE IF NOT EXISTS a_daily_market_turnover (
    id              BIGSERIAL       PRIMARY KEY,
    trade_date      DATE            NOT NULL,
    snapshot_time   TIMESTAMPTZ     NOT NULL,
    total_turnover  NUMERIC(22,2),
    total_volume    BIGINT,
    stock_count     INT,
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (trade_date)
);

COMMENT ON TABLE  a_daily_market_turnover            IS 'A股全市场每日成交额/成交量快照（盘后采集）';
COMMENT ON COLUMN a_daily_market_turnover.trade_date IS '交易日';
COMMENT ON COLUMN a_daily_market_turnover.snapshot_time IS '快照时刻(已转 UTC)';

CREATE TABLE IF NOT EXISTS a_daily_quote (
    id                  BIGSERIAL       PRIMARY KEY,
    stock_code          VARCHAR(20)     NOT NULL,
    trade_date          DATE            NOT NULL,
    open                NUMERIC(12,4),
    high                NUMERIC(12,4),
    low                 NUMERIC(12,4),
    close               NUMERIC(12,4),
    volume              BIGINT,
    amount              NUMERIC(22,2),
    turnover_rate       NUMERIC(8,4),
    volume_ratio        NUMERIC(8,4),
    high_52w            NUMERIC(12,4),
    low_52w             NUMERIC(12,4),
    total_market_val    NUMERIC(22,2),
    circular_market_val NUMERIC(22,2),
    pe_ratio            NUMERIC(12,4),
    pe_ttm_ratio        NUMERIC(12,4),
    pb_ratio            NUMERIC(12,4),
    dividend_ratio_ttm  NUMERIC(8,4),
    update_time         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_aquote_trade_date ON public.a_daily_quote USING btree (trade_date);

COMMENT ON TABLE  a_daily_quote                  IS 'A股个股每日行情/估值快照（盘后采集）';
COMMENT ON COLUMN a_daily_quote.stock_code       IS '股票完整代码，如 SH.600519';
COMMENT ON COLUMN a_daily_quote.trade_date       IS '交易日';

-- 3.2 全球基准指数分钟行情（get_global_benchmarks_minute.py）
CREATE TABLE IF NOT EXISTS benchmark_minute (
    id          BIGSERIAL       PRIMARY KEY,
    bench_code  VARCHAR(20)     NOT NULL,
    bench_name  VARCHAR(50),
    ts          TIMESTAMPTZ     NOT NULL,   -- 已转 UTC
    mkt_time    TIMESTAMPTZ,                -- 市场本地时间(便于核对)
    open        NUMERIC(14,4),
    high        NUMERIC(14,4),
    low         NUMERIC(14,4),
    close       NUMERIC(14,4),
    source      VARCHAR(20),
    created_at  TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (bench_code, ts)
);

COMMENT ON TABLE  benchmark_minute           IS '全球基准指数分钟级行情（盘中采集）';
COMMENT ON COLUMN benchmark_minute.bench_code IS '基准代码，如 HK.800000 / SPX';
COMMENT ON COLUMN benchmark_minute.ts         IS '时间戳(UTC)';

-- 3.3 行业层级映射（build_sector_hierarchy.py）
-- 富途细粒度 INDUSTRY → GICS 一级部门（参考数据，低频刷新）
CREATE TABLE IF NOT EXISTS sector_hierarchy (
    sector_code   VARCHAR(20)   NOT NULL,        -- 细粒度行业板块 code（= stock_sector.sector_code，如 HK.LIST1019）
    sector_name   VARCHAR(100),                   -- 细粒度行业板块英文名（冗余自 stock_sector，方便查询）
    parent_code   VARCHAR(30)   NOT NULL,        -- 一级部门 code（GICS：ENERGY/MATERIALS/...）
    parent_name   VARCHAR(50),                    -- 一级部门中文名（如 能源/金融）
    sector_type   VARCHAR(20)   DEFAULT 'INDUSTRY',
    updated_at    TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (sector_code)
);

COMMENT ON TABLE  sector_hierarchy              IS '行业层级映射：富途细粒度 INDUSTRY → GICS 一级部门（参考数据，低频刷新）';
COMMENT ON COLUMN sector_hierarchy.parent_code  IS '一级部门 code：GICS 11 部门 + CONGLOMERATES(综合企业) + OTHER(兜底)';
COMMENT ON COLUMN sector_hierarchy.parent_name  IS '一级部门中文名';

-- 3.4 个股-基准相关性（benchmark_correlation_daily.py）
CREATE TABLE IF NOT EXISTS benchmark_correlation (
    stock_code      TEXT    NOT NULL,
    bench_code      TEXT    NOT NULL,
    bench_name      TEXT    NOT NULL,
    calc_date       DATE    NOT NULL,
    days            INTEGER,
    pearson         DOUBLE PRECISION,
    spearman        DOUBLE PRECISION,
    lead_lag_neg1   DOUBLE PRECISION,
    gap_r           DOUBLE PRECISION,
    window5_r       DOUBLE PRECISION,
    window10_r      DOUBLE PRECISION,
    window20_r      DOUBLE PRECISION,
    siphon5_r       DOUBLE PRECISION,
    siphon10_r      DOUBLE PRECISION,
    siphon20_r      DOUBLE PRECISION,
    regime_up_r     DOUBLE PRECISION,
    regime_down_r   DOUBLE PRECISION,
    shock_r_normal      DOUBLE PRECISION,
    shock_r_volatile   DOUBLE PRECISION,
    extreme_signal  TEXT,
    verdict         TEXT,
    signals         TEXT,
    data_start      DATE,
    data_end        DATE,
    detail_json     JSONB,
    updated_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (stock_code, bench_code, calc_date)
);

COMMENT ON TABLE  benchmark_correlation          IS '个股与各基准指数相关性指标（日级重算）';
COMMENT ON COLUMN benchmark_correlation.stock_code IS '股票完整代码';
COMMENT ON COLUMN benchmark_correlation.bench_code IS '基准代码';

-- 3.5 宏观环境评分（macro_environment_score.py）
CREATE TABLE IF NOT EXISTS macro_environment_score (
    stock_code       TEXT    NOT NULL,
    trade_date       DATE    NOT NULL,
    total_score      DOUBLE PRECISION,
    risk_score       DOUBLE PRECISION,
    liquidity_score  DOUBLE PRECISION,
    valuation_score  DOUBLE PRECISION,
    label            TEXT,
    summary          TEXT,
    risk_note        TEXT,
    liquidity_note   TEXT,
    valuation_note   TEXT,
    detail_json      JSONB,
    updated_at       TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (stock_code, trade_date)
);

COMMENT ON TABLE  macro_environment_score         IS '个股宏观环境综合评分（风险/流动性/估值维度）';
COMMENT ON COLUMN macro_environment_score.stock_code IS '股票完整代码';
COMMENT ON COLUMN macro_environment_score.trade_date IS '评分对应交易日';

-- 3.6 采集监控（monitor_collector.py）
-- 任务运行日志
CREATE TABLE IF NOT EXISTS collection_task_log (
    id            BIGSERIAL PRIMARY KEY,
    task_name     VARCHAR(80)  NOT NULL,
    market        VARCHAR(8),
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    status        VARCHAR(16)  NOT NULL,   -- running/ok/timeout/error
    duration_s    NUMERIC(8,2),
    error_msg     TEXT,
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tasklog_name_time
    ON collection_task_log (task_name, started_at DESC);

COMMENT ON TABLE  collection_task_log          IS '采集任务运行日志';
COMMENT ON COLUMN collection_task_log.status   IS 'running/ok/timeout/error';

-- 接口调用日志
CREATE TABLE IF NOT EXISTS collection_api_log (
    id            BIGSERIAL PRIMARY KEY,
    api_name      VARCHAR(40)  NOT NULL,   -- get_market_snapshot/request_history_kline/...
    success       BOOLEAN      NOT NULL,
    latency_s     NUMERIC(8,3),
    error_msg     TEXT,
    called_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_apilog_name_time
    ON collection_api_log (api_name, called_at DESC);

COMMENT ON TABLE  collection_api_log         IS '富途/东财等接口调用日志（成功率/延迟监控）';
COMMENT ON COLUMN collection_api_log.api_name IS '接口名，如 get_market_snapshot';

-- 告警
CREATE TABLE IF NOT EXISTS collection_alert (
    id            BIGSERIAL PRIMARY KEY,
    category      VARCHAR(24)  NOT NULL,   -- task_stall/task_fail/api_fail/api_slow/data_stale
    severity      VARCHAR(8)   NOT NULL,   -- warn/crit
    source        VARCHAR(80),            -- 关联任务名 / 表名 / 接口名
    message       TEXT         NOT NULL,
    first_seen    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    occurrences   INT          NOT NULL DEFAULT 1,
    resolved      BOOLEAN      NOT NULL DEFAULT FALSE,
    resolved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (category, source, severity, resolved)
);
CREATE INDEX IF NOT EXISTS idx_alert_open
    ON collection_alert (resolved, last_seen DESC);

COMMENT ON TABLE  collection_alert          IS '采集健康度告警（去重聚合：同 category+source+severity+resolved 合并计数）';
COMMENT ON COLUMN collection_alert.category IS 'task_stall/task_fail/api_fail/api_slow/data_stale';
COMMENT ON COLUMN collection_alert.severity IS 'warn/crit';

-- 监控元数据：需要监控的表
CREATE TABLE IF NOT EXISTS monitor_table_config (
    id            BIGSERIAL   PRIMARY KEY,
    db_name       VARCHAR(32) NOT NULL DEFAULT 'public',
    table_name    VARCHAR(64) NOT NULL,
    time_column   VARCHAR(32) NOT NULL,
    period        VARCHAR(12) NOT NULL DEFAULT 'day',
    expect_lag    INT         NOT NULL DEFAULT 0,
    refresh_weekday INT       NOT NULL DEFAULT 0,  -- period='week' 时生效：刷新日星期几(0=Mon..6=Sun)
    active        BOOLEAN     NOT NULL DEFAULT TRUE,
    remark        VARCHAR(120),
    UNIQUE (db_name, table_name)
);

COMMENT ON TABLE  monitor_table_config       IS '采集监控配置：被监控表及其刷新周期/期望滞后（运营配置表）';
COMMENT ON COLUMN monitor_table_config.period IS 'minute(盘中高频)/day(日更)/week(周更)/lowfreq(低频)';
COMMENT ON COLUMN monitor_table_config.expect_lag IS '允许滞后：minute→分钟，day/week/lowfreq→天';

-- 监控元数据：高频任务 stall 阈值
CREATE TABLE IF NOT EXISTS monitor_task_config (
    task_name         VARCHAR(80) PRIMARY KEY,
    max_interval_min  INT NOT NULL,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    remark            VARCHAR(120)
);

COMMENT ON TABLE  monitor_task_config        IS '采集监控配置：高频任务两次触发最大允许间隔（分钟）';
COMMENT ON COLUMN monitor_task_config.max_interval_min IS '该任务两次触发的最大允许间隔（分钟）';
