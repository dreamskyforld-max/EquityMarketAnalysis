#!/usr/bin/env python3
"""标签元数据注册表：代码即字典

每个标签用一个纯函数实现，元数据通过 `@tag` 装饰器写在函数旁边：

    @tag(
        code="idt_market", name="市场", domain="①证券属性",
        value_type="enum", value_range={"A股": "沪深两市", "港股": "香港市场"},
        source_type="rule", update_freq="static",
        data_sources=["stock_info"],
        compute_logic="取 stock_info.market，SH/SZ→A股，HK→港股",
    )
    def idt_market(as_of: date) -> pd.DataFrame:
        ...

好处：改变口径就必须改代码，字典不会与实现漂移。模块被 import 时标签自动注册，
`sync_registry()` 把全部元数据 upsert 到 profile.tag_registry。

字段说明（与 tag_registry 表一一对应）：
    multi_value     多重归属：一只股票可同时有多个取值（如指数成分、概念板块）
    is_exclusive    同时点互斥：一只股票同时只能有一个取值（如 ⑩ 趋势状态）
    confidence_req  必须输出置信度（模型类标签）
    pit_capable     能否回溯历史：依赖数据是否自带生效时点。
                    例：stock_sector 只有当前快照 → 行业标签 pit_capable=False，
                    历史 as_of 无法还原当时的行业归属。
    status          active=已实现；planned_no_data=口径已定但数据源缺失，暂不计算
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Any, Callable, Optional

from psycopg2.extras import Json

log = logging.getLogger(__name__)

# 状态常量
ACTIVE = "active"
PLANNED_NO_DATA = "planned_no_data"
DEPRECATED = "deprecated"

# 取值类型常量
ENUM = "enum"
BOOL = "bool"
TIER = "tier"
FLOAT = "float"

# num_value 的计量单位（受控枚举，防自由文本漂移）
UNIT_X = "x"                       # 倍数（PE/PB/PS/PCF）
UNIT_PCT = "pct"                   # 百分比 0-100（ROE/毛利率/负债率/增速/股息率）
UNIT_PP = "pp"                     # 百分点差值（增速的二阶变化）
UNIT_PCTL = "percentile_0_100"     # 横截面/时序百分位
UNIT_CNY_HKD = "CNY|HKD"           # 按市场的原币金额（A 股元 / 港股港元）
UNIT_YEARS = "years"               # 年
UNIT_COUNT = "count"               # 计数（连续期数）
UNIT_RANK = "rank"                 # 组内排名（1 起）
_VALID_UNITS = {UNIT_X, UNIT_PCT, UNIT_PP, UNIT_PCTL, UNIT_CNY_HKD, UNIT_YEARS, UNIT_COUNT, UNIT_RANK}


@dataclass
class TagMeta:
    """一个标签的完整元数据。"""

    code: str
    name: str
    domain: str
    value_type: str
    source_type: str
    update_freq: str
    # 可选
    value_range: Optional[dict[str, Any]] = None   # tier / float 的取值说明 {code: label}
    value_ref: Optional[str] = None                # 值域来源：'enum:<type>' 或 'table:<表>.<列>[过滤]'
    num_unit: Optional[str] = None                 # num_value 的计量单位（UNIT_* 常量，enum/bool 标签为空）
    enum_type: Optional[str] = None                # 内置枚举的值域类型（可被多个标签复用）
    enum_values: Optional[dict[str, tuple[str, str]]] = None   # {code: (label, short_label)}
    data_sources: Optional[list[str]] = None
    compute_logic: Optional[str] = None
    parent_tag: Optional[str] = None
    multi_value: bool = False
    is_exclusive: bool = False
    confidence_req: bool = False
    pit_capable: bool = False
    version: str = "v1"
    status: str = ACTIVE
    blocked_reason: Optional[str] = None
    owner: Optional[str] = None

    def __post_init__(self):
        if self.value_type not in (ENUM, BOOL, TIER, FLOAT):
            raise ValueError(f"[{self.code}] value_type 非法: {self.value_type}")
        if self.status not in (ACTIVE, PLANNED_NO_DATA, DEPRECATED):
            raise ValueError(f"[{self.code}] status 非法: {self.status}")
        if self.is_exclusive and self.multi_value:
            raise ValueError(f"[{self.code}] is_exclusive 与 multi_value 互斥")
        if self.status != ACTIVE and not self.blocked_reason:
            raise ValueError(f"[{self.code}] status={self.status} 时必须给出 blocked_reason")

        # enum 值域：内置型必须有 enum_values（进 profile.enum_value），引用型必须有 value_ref
        if self.enum_values:
            if not self.enum_type:
                self.enum_type = self.code
            if not self.value_ref:
                self.value_ref = f"enum:{self.enum_type}"
            dup_label = [n for n, c in Counter(v[0] for v in self.enum_values.values()).items() if c > 1]
            if dup_label:
                raise ValueError(f"[{self.code}] enum_values 的 label 重复（同一概念多个码）: {dup_label}")
        # 未实现（planned_no_data）的标签值域取决于将来采用的数据源，提前写死会固化错误口径
        if self.value_type == ENUM and self.status == ACTIVE and not self.value_ref:
            raise ValueError(
                f"[{self.code}] enum 类型必须声明值域：给 enum_values（内置）或 value_ref（引用已有表）"
            )
        if self.num_unit is not None and self.num_unit not in _VALID_UNITS:
            raise ValueError(f"[{self.code}] num_unit 非法: {self.num_unit}（允许 {_VALID_UNITS}）")
        if self.value_type in (ENUM, BOOL) and self.num_unit not in (None, UNIT_PCTL):
            raise ValueError(f"[{self.code}] enum/bool 标签的 num_value 无连续含义，num_unit 应留空")

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["tag_code"] = d.pop("code")
        d["tag_name"] = d.pop("name")
        # enum_type / enum_values 不是 tag_registry 的列：值域单独落 profile.enum_value
        d.pop("enum_type", None)
        d.pop("enum_values", None)
        d["data_sources"] = list(self.data_sources) if self.data_sources else None
        # dict 需显式转成 psycopg2 的 Json 适配器，否则写入 JSONB 会报 can't adapt type 'dict'
        d["value_range"] = Json(self.value_range) if self.value_range is not None else None
        return d


_REGISTRY: dict[str, TagMeta] = {}
_FUNCS: dict[str, Callable[..., Any]] = {}


def tag(**meta_kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """标签注册装饰器。元数据写在计算函数上，导入即注册。"""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        meta = TagMeta(**meta_kw)
        if meta.code in _REGISTRY:
            raise ValueError(f"标签重复注册: {meta.code}")
        _REGISTRY[meta.code] = meta
        _FUNCS[meta.code] = fn
        setattr(fn, "tag_meta", meta)  # 便于外部直接取元数据
        return fn

    return deco


def all_tags() -> list[TagMeta]:
    return list(_REGISTRY.values())


def get(code: str) -> Optional[TagMeta]:
    return _REGISTRY.get(code)


def get_func(code: str) -> Callable[..., Any]:
    return _FUNCS[code]


def by_domain(domain_prefix: str) -> list[TagMeta]:
    """按域前缀过滤（如 '①' 匹配 ①证券属性 域的全部标签）。"""
    return [m for m in _REGISTRY.values() if m.domain.startswith(domain_prefix)]


def domains() -> list[str]:
    return sorted({m.domain for m in _REGISTRY.values()})


def validate_enum_uniqueness() -> None:
    """校验同一 enum_type 内，label 与 short_label 合并后全局唯一。

    这是「一个概念只能有一个名称」的硬约束：若某个中文名同时是 A 码的 label
    和 B 码的简称（如「上交所」与「上海证券交易所」各立一码），直接报错。
    """
    seen: dict[tuple[str, str], str] = {}
    for meta in all_tags():
        if not meta.enum_values:
            continue
        for code, names in meta.enum_values.items():
            for name in (n for n in names[:2] if n):  # 只查 label / short_label
                key = (meta.enum_type or meta.code, name)
                prev = seen.get(key)
                if prev:
                    raise ValueError(
                        f"枚举概念冲突：enum_type={key[0]} 中名称 {name!r} 同时出现在 "
                        f"{prev} 与 {meta.code}（一个概念只能有一个名称、一个 code）"
                    )
                seen[key] = meta.code


def _sync_enum_values(conn) -> int:
    """把代码里声明的 enum_values 同步到 profile.enum_value（幂等）。"""
    from psycopg2 import extras

    rows = []
    for meta in all_tags():
        if not meta.enum_values:
            continue
        enum_type = meta.enum_type or meta.code
        for sort_order, (code, names) in enumerate(meta.enum_values.items()):
            rows.append((
                enum_type, code, names[0],
                names[1] if len(names) > 1 else None,
                names[2] if len(names) > 2 else None,
                sort_order,
            ))
    if not rows:
        return 0

    with conn.cursor() as cur:
        extras.execute_values(
            cur,
            """
            INSERT INTO profile.enum_value
                (enum_type, code, label, short_label, parent_code, sort_order)
            VALUES %s
            ON CONFLICT (enum_type, code) DO UPDATE SET
                label       = EXCLUDED.label,
                short_label = EXCLUDED.short_label,
                parent_code = EXCLUDED.parent_code,
                sort_order  = EXCLUDED.sort_order,
                updated_at  = NOW()
            """,
            rows,
        )
    return len(rows)


def sync_registry(conn) -> tuple[int, int]:
    """把代码里注册的标签元数据 upsert 到 profile.tag_registry，值域同步到 profile.enum_value。

    返回 (新增数, 更新数)。只同步元数据，不删除库里已下线但代码里已移除的标签
    （历史标签值仍需可追溯，删除字典会丢失口径说明）。
    """
    from psycopg2 import extras

    validate_enum_uniqueness()
    inserted = updated = 0
    with conn.cursor() as cur:
        cur.execute("SELECT tag_code, tag_name, domain, version, status FROM profile.tag_registry")
        existing = {r[0]: r for r in cur.fetchall()}

        for meta in all_tags():
            row = meta.to_row()
            if row["tag_code"] not in existing:
                inserted += 1
            else:
                updated += 1
            cols = list(row.keys())
            sql = f"""
                INSERT INTO profile.tag_registry ({",".join(cols)})
                VALUES %s
                ON CONFLICT (tag_code) DO UPDATE SET
                    {",".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "tag_code")},
                    updated_at = NOW()
            """
            extras.execute_values(cur, sql, [tuple(row[c] for c in cols)])

    n_enum = _sync_enum_values(conn)
    log.info(
        "标签字典同步完成：标签 新增 %d / 更新 %d（合计 %d）；枚举值域同步 %d 条",
        inserted, updated, len(_REGISTRY), n_enum,
    )
    return inserted, updated
