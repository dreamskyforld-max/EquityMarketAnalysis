#!/usr/bin/env python3
"""申万行业分类层级采集 —— 写入 regime.sw_industry_classification。

数据源：AKShare
  · sw_index_first_info()  一级 31 个
  · sw_index_second_info() 二级 131 个（含「上级行业」= 一级名）
  · sw_index_third_info()  三级 336 个（含「上级行业」= 二级名）

职责（单一职责）：只采**分类层级**（code/name/level/parent），不采行情、不采估值、不采个股映射。
下游：get_sw_industry_quotes.py 读本表取三级代码清单，不再写死 SW_L1 常量。

落库：UNIQUE(sw_code) 幂等 upsert；每日 08:10 由 market_scheduler 进程内调用 run()。
实测耗时：三接口合计 ≈20.4s（网络为主），远低于调度阈值。

用法：
    python3 get_sw_industry_classification.py            # 采集（调度用）
    python3 get_sw_industry_classification.py --dry-run  # 只打印不写库
"""
import argparse
import datetime
import sys

from db import bulk_upsert, get_conn

TABLE = "regime.sw_industry_classification"
CONFLICT = ["sw_code"]
SOURCE_LEVELS = (
    ("sw_index_first_info", 1, None),
    ("sw_index_second_info", 2, "first"),
    ("sw_index_third_info", 3, "second"),
)


def _read_levels() -> tuple[list, dict]:
    """读三级分类，返回 (rows, code_by_name)。code_by_name 供下级取 parent_code。"""
    import akshare as ak
    import pandas as pd

    as_of = datetime.date.today()
    rows, code_by_name = [], {}
    for fn_name, level, parent_level in SOURCE_LEVELS:
        df = getattr(ak, fn_name)()
        if df is None or len(df) == 0:
            raise RuntimeError(f"{fn_name} 返回空数据，拒绝写入（避免清空分类表）")
        cols = {str(c): c for c in df.columns}
        if "行业代码" not in cols or "行业名称" not in cols:
            raise RuntimeError(f"{fn_name} 列结构变化：缺 行业代码/行业名称，实际 {list(df.columns)}")
        c_code, c_name = cols["行业代码"], cols["行业名称"]
        c_parent = cols.get("上级行业")
        for _, r in df.iterrows():
            code = str(r[c_code]).replace(".SI", "").strip()
            name = str(r[c_name]).strip()
            if not code or code == "nan":
                continue
            pcode = None
            if parent_level and c_parent is not None:
                pname = str(r[c_parent]).strip()
                pcode = code_by_name.get(pname)
                if pcode is None:
                    raise RuntimeError(
                        f"{fn_name}: 行业 {name}({code}) 的上级「{pname}」在本级码表中不存在，"
                        "层级链断裂，拒绝写入")
            rows.append({
                "sw_code": code, "sw_name": name, "level": level,
                "parent_code": pcode, "as_of": as_of,
            })
            code_by_name[name] = code
    return rows, code_by_name


def run(codes=None, ctx=None) -> dict:
    """采集入口。codes/ctx 为调度器兼容参数（本任务忽略）。"""
    rows, _ = _read_levels()
    counts = {}
    for r in rows:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    if counts.get(1) != 31 or counts.get(2) != 131:
        # 三级 336 会随申万年度调整变动，不硬断言；一/二级数量稳定，作健康闸
        raise RuntimeError(f"申万分类数量异常：一级 {counts.get(1)} / 二级 {counts.get(2)}，拒绝写入")
    with get_conn() as conn:
        bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT)
    print(f"写入 {TABLE}：{len(rows)} 行（一级 {counts.get(1)} / 二级 {counts.get(2)} / 三级 {counts.get(3)}）")
    return {"rows": len(rows), **{f"L{k}": v for k, v in counts.items()}}


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = ap.parse_args()
    rows, _ = _read_levels()
    from collections import Counter
    print(f"dry-run: {len(rows)} 行，按层级 {dict(Counter(r['level'] for r in rows))}")
    for r in rows[:6]:
        print("  ", r)
    if not args.dry_run:
        with get_conn() as conn:
            bulk_upsert(conn, TABLE, rows, conflict_cols=CONFLICT)
        print("已写入", TABLE)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
