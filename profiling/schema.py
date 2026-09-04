#!/usr/bin/env python3
"""profile schema 建表与分区管理（幂等，可重复执行）

表清单：
    profile.tag_registry   标签字典（元数据，由 registry.sync_registry 写入）
    profile.tag_value      标签值（EAV + 版本化，按 eff_from 按年分区）
    profile.tag_run_log    每次标签计算的运行日志（治理用）

为什么用 EAV 而不是宽表：
    122+ 个标签且会持续增减，宽表每次加标签都要 DDL；EAV 加标签只是多一批行。
    代价是查询要多一层，但通过 (tag_code, value_key, eff_from, eff_to) 索引
    可以高效取「某标签某取值的全部股票」，这正是组合筛选需要的访问模式。

版本化语义：
    eff_from / eff_to 为闭区间，当前有效行的 eff_to = 9999-12-31。
    标签值变化时：关闭旧行（eff_to = as_of - 1）+ 插入新行（eff_from = as_of）。
    同一天内重复计算不会产生新版本，直接原地修正（避免 eff_to < eff_from 的无效区间）。
"""
import logging
from datetime import date

log = logging.getLogger(__name__)

_FAR_FUTURE = "9999-12-31"

_DDL = [
    # ── 标签字典 ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile.tag_registry (
        tag_code        TEXT    PRIMARY KEY,
        tag_name        TEXT    NOT NULL,
        domain          TEXT    NOT NULL,
        parent_tag      TEXT,
        value_type      TEXT    NOT NULL,          -- enum / bool / tier / float
        value_range     JSONB,                     -- 取值说明：tier/float 用 {code: label}；enum 的规范值域见 profile.enum_value
        value_ref       TEXT,                      -- 值域来源：'enum:<enum_type>' 引用 enum_value 表，
                                                   -- 或 'table:<表>.<列>[过滤]' 引用已有权威表（行业/指数/概念）
        num_unit        TEXT,                      -- num_value 的计量单位（x/pct/pp/percentile_0_100/CNY|HKD/years/count/rank），
                                                   -- enum/bool 标签为空。前端格式化展示与跨标签比较依赖此列
        source_type     TEXT    NOT NULL,          -- rule / stat / model / external / manual
        compute_logic   TEXT,                      -- 计算口径描述
        update_freq     TEXT    NOT NULL,          -- daily / weekly / monthly / quarterly / event / static
        data_sources    TEXT[],                    -- 依赖的数据表（血缘用）
        multi_value     BOOLEAN DEFAULT FALSE,     -- 多重归属（如指数成分、概念板块）
        is_exclusive    BOOLEAN DEFAULT FALSE,     -- 同时点互斥（仅 ⑩ 趋势状态为 true）
        confidence_req  BOOLEAN DEFAULT FALSE,     -- 是否必须带置信度（模型类标签 true）
        pit_capable     BOOLEAN DEFAULT FALSE,     -- 能否回溯历史（依赖数据是否带生效时点）
        version         TEXT    NOT NULL DEFAULT 'v1',
        status          TEXT    NOT NULL DEFAULT 'active',  -- active / planned_no_data / deprecated
        blocked_reason  TEXT,                      -- status != active 时的原因
        owner           TEXT,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    COMMENT ON TABLE profile.tag_registry IS
        '标签字典：每个标签的元数据（口径/来源/频率/版本/状态），由 profiling.registry 从代码装饰器同步写入'
    """,
    # ── 枚举值域字典 ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile.enum_value (
        enum_type    TEXT        NOT NULL,      -- 值域类型（如 exchange / board / market），可被多个标签复用
        code         TEXT        NOT NULL,      -- 规范代码：唯一标识，enum 标签落库的就是它
        label        TEXT        NOT NULL,      -- 中文正式名称（同一 enum_type 内唯一）
        short_label  TEXT,                      -- 中文简称（仅展示用，不参与比较与关联）
        parent_code  TEXT,                      -- 层级父项（如板块 → 所属交易所）
        sort_order   INT         DEFAULT 0,
        is_active    BOOLEAN     DEFAULT TRUE,
        updated_at   TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (enum_type, code)
    )
    """,
    """
    COMMENT ON TABLE profile.enum_value IS
        '枚举值域字典：enum 类型标签的规范取值。一个概念 = 一个 code，中文名只作 label，杜绝「上交所/上海证券交易所」这类同义异名'
    """,
    "COMMENT ON COLUMN profile.enum_value.code  IS '规范代码：优先采用外部权威代码（如交易所用 ISO 10383 MIC：XSHG/XSHE/XHKG）'",
    "COMMENT ON COLUMN profile.enum_value.label IS '中文正式名称，同一 enum_type 内唯一；简称放 short_label，不另立 code'",
    "COMMENT ON COLUMN profile.enum_value.parent_code IS '层级父项 code，用于板块归属交易所等上下级关系'",
    # ── 标签值 ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile.tag_value (
        id           BIGSERIAL,
        stock_code   TEXT             NOT NULL,
        tag_code     TEXT             NOT NULL,
        key_value    TEXT             NOT NULL,   -- 离散取值：筛选用（枚举 code / 分档 / 布尔）
        num_value    DOUBLE PRECISION,            -- 连续取值：研究回测用，保留信息量
        confidence   REAL             NOT NULL DEFAULT 1.0,
        eff_from     DATE             NOT NULL,   -- 生效日（含）
        eff_to       DATE             NOT NULL,   -- 失效日（含），当前有效 = 9999-12-31
        version      TEXT             NOT NULL DEFAULT 'v1',
        created_at   TIMESTAMPTZ      DEFAULT NOW(),
        update_time  TIMESTAMPTZ      DEFAULT NOW(),   -- 本行最后一次被修改的时间（同日原地修正时刷新）
        PRIMARY KEY (id, eff_from),               -- 分区表主键必须含分区键
        UNIQUE (stock_code, tag_code, key_value, eff_from)
    ) PARTITION BY RANGE (eff_from)
    """,
    """
    COMMENT ON TABLE profile.tag_value IS
        '股票标签值（EAV + 版本化，按 eff_from 按年分区）。key_value 供筛选，num_value 保留连续值供回测'
    """,
    "COMMENT ON COLUMN profile.tag_value.key_value  IS '离散取值，组合筛选与位图索引的键。enum 类型存规范 code（如 XSHG）而非中文名，中文显示名查 profile.enum_value'",
    "COMMENT ON COLUMN profile.tag_value.num_value  IS '连续取值（如真实 PE=87.3），分档会损失边界信息，此列保留原始数值供回测/排序/重标定'",
    "COMMENT ON COLUMN profile.tag_value.update_time IS '本行最后一次被修改的时间；产生新版本时新行的 update_time = 写入时间'",
    "COMMENT ON COLUMN profile.tag_value.confidence IS '置信度 0-1：规则/统计类恒为 1.0，模型类为校准后的预测概率，筛选时用于阈值过滤'",
    "COMMENT ON COLUMN profile.tag_value.eff_from   IS '生效日（含）。标签值变化不覆盖旧行，而是关闭旧版本并新增一行'",
    "COMMENT ON COLUMN profile.tag_value.eff_to     IS '失效日（含），当前有效行为 9999-12-31'",
    # ── 运行日志 ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile.tag_run_log (
        id           BIGSERIAL PRIMARY KEY,
        tag_code     TEXT        NOT NULL,
        as_of        DATE        NOT NULL,
        rows_total   INT,                          -- 本次计算产出的 (股票, 取值) 行数
        rows_new     INT,                          -- 新增行数
        rows_closed  INT,                          -- 关闭的旧版本行数
        duration_ms  INT,
        version      TEXT,
        status       TEXT,                         -- ok / skipped / error
        message      TEXT,
        created_at   TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    COMMENT ON TABLE profile.tag_run_log IS
        '标签计算运行日志：每次计算的行数变化与耗时，用于监控标签稳定性（新增/关闭行数异常 = 口径可能有问题）'
    """,
]

_INDEXES = [
    # 按标签取值取股票集合（组合筛选的主访问路径）
    "CREATE INDEX IF NOT EXISTS idx_tag_value_lookup ON profile.tag_value (tag_code, key_value, eff_from, eff_to, stock_code)",
    # 按股票查其全部标签（个股档案的主访问路径）
    "CREATE INDEX IF NOT EXISTS idx_tag_value_stock  ON profile.tag_value (stock_code, tag_code, eff_from DESC)",
]


def ensure_partition(conn, year: int) -> bool:
    """确保某年份的分区存在（幂等）。PG 不支持 CREATE TABLE ... PARTITION OF IF NOT EXISTS。"""
    name = f"tag_value_{year}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'profile' AND c.relname = %s)",
            (name,),
        )
        if cur.fetchone()[0]:
            return False
        cur.execute(
            f"CREATE TABLE profile.{name} PARTITION OF profile.tag_value "
            f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
        )
    log.info("已创建分区 profile.%s", name)
    return True


def ensure_schema(conn, years=None) -> None:
    """建 schema / 表 / 索引 / 分区（幂等）。

    years: 需要预建的年份分区列表，默认当年与下一年（兜底分区保证其余年份也能写入）。
    """
    if years is None:
        y = date.today().year
        years = (y, y + 1)

    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS profile")
        for stmt in _DDL:
            cur.execute(stmt)
        # 兜底分区：未预建年份的数据落这里，避免插入时因无分区而报错
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'profile' AND c.relname = 'tag_value_default')"
        )
        if not cur.fetchone()[0]:
            cur.execute(
                "CREATE TABLE profile.tag_value_default PARTITION OF profile.tag_value DEFAULT"
            )
        for stmt in _INDEXES:
            cur.execute(stmt)

    for y in years:
        ensure_partition(conn, y)
