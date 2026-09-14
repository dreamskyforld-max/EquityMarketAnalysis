#!/usr/bin/env python3
"""
公司资料 + 主营构成（全市场：A股 + 港股）→ company_profile / company_revenue_breakdown

数据源：富途 OpenAPI（无需新增依赖）
- get_company_profile(code)：一行一票静态资料。A股 24 字段 / 港股 22 字段并集：
  公司全称、ISIN、成立日期、发行价/发行量、注册地址/注册办事处/办公地址、董监高、
  审计机构、公司类别、年结日、员工数、联系方式、「公司业务」（主营业务描述）、
  「公司简介」（长文）。
- get_financials_revenue_breakdown(code[, date, financial_type])：主营构成，
  按 PRODUCT/BUSINESS（部分标的含 REGION）口径给出各条目收入与占比；
  不传 date 时返回最新期数据 + screen_date_list（全历史报告期，港股回溯至 2005/H1）。

采集范围（run(codes=None) 全市场模式）：
- company_profile：quote_universe 全部可采集正股（1 票 1 次调用，约 9100 只）；
- company_revenue_breakdown：仅关注池（stock_info.is_active）最近 N 期
  （1 票 1 期 1 次调用；全市场 × 全历史 40+ 期调用量不可接受，刻意限制）。
- run(codes=[...]) 指定票模式：profile + breakdown 都采。

口径约定：
- company_profile 一行一票，覆盖式 upsert 且 skip_null_updates=True：
  某字段返回空时保留库中已有值（多源补缺/防回退友好）。
- breakdown 唯一键 (stock_code, period, breakdown_type, item_name, source)，
  同一报告期重述时原地更新。
- 上市日期 / 所属市场 / 证券简称不重复存（stock_info 已有 list_date / exchange_type）。
- 富途「公司业务」→ main_business；「公司简介」→ company_intro（长文）。

日志：
- 每只票采集后打印一行（时间戳 + 序号 + 代码 + 名称 + 产出行数 + 累计 ok/fail），
  失败与跳过同样入日志，长跑（全市场 ≈ 2.7 小时）可按行观察进度；
- 另有事件级日志：批内重复键告警 [警告]、批次入库失败 [DB]。

用法：
    python3 get_company_profile.py                  # 全市场 profile + 关注池 breakdown
    python3 get_company_profile.py HK.00700         # 单票全采
    python3 get_company_profile.py --periods 8      # breakdown 采最近 8 期
    python3 get_company_profile.py --skip-breakdown # 只采公司资料
    python3 get_company_profile.py --limit 200      # 调试：全市场只采前 200 只
    python3 get_company_profile.py --start 2346     # 断点续采：从第 2346 只开始（序号同日志）
    python3 get_company_profile.py --start-code HK.02345   # 断点续采：从指定代码开始
"""
from __future__ import annotations

import argparse
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from collector_runtime import close_shared_ctx, get_shared_ctx
from db import bulk_upsert, get_conn

TABLE_PROFILE = "company_profile"
TABLE_BREAKDOWN = "company_revenue_breakdown"

PROFILE_CONFLICT = ["stock_code"]
BREAKDOWN_CONFLICT = ["stock_code", "period", "breakdown_type", "item_name", "source"]

BREAKDOWN_PERIODS = 4          # 默认采最近 N 期主营构成
PROFILE_BATCH = 300            # profile 每 N 票入库一次
BREAKDOWN_BATCH = 3000         # breakdown 每 N 行入库一次

# ── 富途字段名 → 表列名（A股/港股两套字段共用；未列出的字段忽略）────────────
# 忽略：公司代码/A股证券代码/上市日期/上市交易所/所属市场（stock_info 已有）、
#       A股证券简称（stock_info.stock_name 已有）
_FIELD_MAP = {
    "公司名称": "company_name",
    "ISIN代码": "isin",
    "发行价格": "issue_price",
    "发行数量": "issue_qty",
    "成立日期": "founded_date",
    "公司注册地址": "registered_address",        # A股=详细地址；港股=注册地地区
    "注册办事处": "registered_office",           # 港股详细注册办事处（A股无此字段）
    "总办事处及主要营业地点": "office_address",   # 港股
    "公司办公地址": "office_address",             # A股
    "公司办公地址邮编": "office_postcode",
    "董事长": "chairman",                        # 港股
    "法人代表": "legal_rep",                     # A股
    "总经理": "general_manager",
    "公司秘书": "company_secretary",
    "证券/股证事务代表": "sec_rep",
    "审计机构": "auditor",                       # 港股
    "会计师事务所": "auditor",                   # A股
    "法律顾问": "legal_advisor",
    "公司类别": "company_category",              # 港股（如「境外注册内地个人控制」）
    "年结日": "fiscal_year_end",
    "员工数量": "employee_count",
    "电话": "phone",
    "传真": "fax",
    "邮箱": "email",
    "网址": "website",
    "公司业务": "main_business",
    "公司简介": "company_intro",
    "企业法人营业执照注册号": "business_license_no",
}

_DATE_COLS = {"founded_date"}
_INT_COLS = {"issue_qty", "employee_count"}
_FLOAT_COLS = {"issue_price"}

_QTY_RE = re.compile(r"([\d,\.]+)\s*(亿|万)?")


# ═════════════════════════════════════════════════════════════
# 解析/转换（纯函数，可单测）
# ═════════════════════════════════════════════════════════════

def _to_float(v):
    """数值文本 → float；空/非法返回 None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(v):
    f = _to_float(v)
    return int(f) if f is not None else None


def _parse_date(v):
    """富途日期文本 → date。样例: '2004/06/16'、'1999-11-23'；失败返回 None。"""
    s = str(v or "").strip()
    if not s:
        return None
    s = s[:10]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_qty(v):
    """富途发行数量文本 → 股数。样例: '4.20亿股'→420000000，'4831.85万股'→48318500。"""
    if v is None:
        return None
    m = _QTY_RE.search(str(v))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2)
    if unit == "亿":
        num *= 1e8
    elif unit == "万":
        num *= 1e4
    return int(round(num))


_HKT = timezone(timedelta(hours=8))   # 富途 screen_date 时间戳是「北京时间零点」，
                                      # 按 UTC 转换会少一天（实测 2026/H1 得 06-29，应为 06-30）


def _ts_to_date(ts) -> "datetime.date | None":
    """富途 screen_date 秒级时间戳 → 报告期截止日（按 UTC+8 换算）。"""
    if ts in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=_HKT).date()
    except (ValueError, OSError, TypeError):
        return None


# ═════════════════════════════════════════════════════════════
# 行构造（纯函数，可单测）
# ═════════════════════════════════════════════════════════════

def profile_row(code: str, df) -> "dict | None":
    """富途 get_company_profile 返回的 name/value 长表 → company_profile 行。

    无值的字段不写入 dict（配合 bulk_upsert(skip_null_updates=True)：
    保护库中已有值不被本次的空值清掉）。
    公司全称为空视为无效返回 None（该票计数为失败）。
    """
    row: dict = {
        "stock_code": code,
        "source": "futu",
        "updated_at": datetime.now(timezone.utc),
    }
    for _, r in df.iterrows():
        raw_name = str(r.get("name") or "").strip()
        col = _FIELD_MAP.get(raw_name)
        if not col:
            continue
        raw = r.get("value")
        # pandas 把 None 读成 NaN（float），需显式剔除，否则会被写成字符串 "nan"；
        # 富途偶发返回 "--" 之类占位符，一并视为空值
        if raw is None or (isinstance(raw, float) and math.isnan(raw)):
            continue
        raw_s = str(raw).strip()
        if raw_s == "" or raw_s in ("nan", "None", "--", "-"):
            continue
        if col in _DATE_COLS:
            val = _parse_date(raw_s)
        elif col in _INT_COLS:
            val = _parse_qty(raw_s) if col == "issue_qty" else _to_int(raw_s)
        elif col in _FLOAT_COLS:
            val = _to_float(raw_s)
        else:
            val = raw_s
        if val is None or val == "":
            continue
        row[col] = val
    return row if row.get("company_name") else None


def _period_end_of(payload: dict, period: str, date_map: "dict | None" = None):
    """报告期截止日：优先用首次调用汇总的 date_map（{period_text: 时间戳}）。

    坑：富途只有「不传 date 的首次调用」才返回 screen_date_list，
    逐期调用（传 date/financial_type）返回的 payload 不带该列表 → 历史期的
    period_end 必须由 date_map 提供，否则恒为 NULL（实测踩坑）。
    """
    if date_map:
        ts = date_map.get(period)
        if ts is not None:
            return _ts_to_date(ts)
    for p in payload.get("screen_date_list") or []:
        if str(p.get("period_text") or "").strip() == period:
            return _ts_to_date(p.get("date"))
    return None


def breakdown_rows(code: str, payload, date_map: "dict | None" = None) -> list[dict]:
    """富途 get_financials_revenue_breakdown 返回 dict → company_revenue_breakdown 行列表。

    结构: {'period': '2026/H1', 'currency_code': 'CNY',
           'breakdown_list': [{'type': 'PRODUCT', 'item_list': [
               {'name':..., 'main_oper_income':..., 'ratio':...}, ...]}, ...],
           'screen_date_list': [...]}   # 仅首次（不传 date）调用返回
    """
    rows: list[dict] = []
    if not isinstance(payload, dict):
        return rows
    period = str(payload.get("period") or "").strip()
    if not period:
        return rows
    currency = payload.get("currency_code")
    period_end = _period_end_of(payload, period, date_map)
    now = datetime.now(timezone.utc)
    for blk in payload.get("breakdown_list") or []:
        btype = str(blk.get("type") or "").strip()
        if not btype:
            continue
        for item in blk.get("item_list") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rows.append({
                "stock_code": code,
                "period": period,
                "period_end": period_end,
                "breakdown_type": btype,
                "item_name": name,
                "revenue": _to_float(item.get("main_oper_income")),
                "ratio": _to_float(item.get("ratio")),
                "currency": currency,
                "source": "futu",
                "updated_at": now,
            })
    return rows


# ═════════════════════════════════════════════════════════════
# 采集（富途）
# ═════════════════════════════════════════════════════════════

# 富途「公司详情」接口限流：每 30 秒最多 30 次（实测报错原文）。
# 滑动窗口计数 + 留 4 次余量，避免与调度器整点的其他 futu 任务互相挤配额。
_RATE_WINDOW_SEC = 30.0
_RATE_MAX_CALLS = 26
_RATE_LIMIT_MARK = "频率太高"
_call_times: deque[float] = deque()


def _throttle_futu():
    """按滑动窗口限速（调用前必须执行）。"""
    while True:
        now = time.monotonic()
        while _call_times and now - _call_times[0] >= _RATE_WINDOW_SEC:
            _call_times.popleft()
        if len(_call_times) < _RATE_MAX_CALLS:
            _call_times.append(now)
            return
        time.sleep(_RATE_WINDOW_SEC - (now - _call_times[0]) + 0.05)


def _futu_call(fn, *args, **kwargs):
    """限速 + 限流错误等待重试一次；返回 (ret, data)。"""
    for attempt in (1, 2):
        _throttle_futu()
        ret, data = fn(*args, **kwargs)
        if ret == 0:
            return ret, data
        if _RATE_LIMIT_MARK in str(data) and attempt == 1:
            time.sleep(2.0)     # 错峰后重试一次（窗口边缘抖动常见）
            continue
        return ret, data
    return ret, data


def fetch_profile_row(ctx, code: str) -> "tuple[dict | None, str]":
    """单票公司资料。返回 (row, note)：

    - 成功: (row, "")；
    - 接口失败: (None, "接口失败: {错误}")；
    - 富途返回空表/无公司名（实测 HK.00007 智富资源投资）: (None, "富途无公司资料（空表）")。

    不在此打印（由调用方按每票一行统一输出，避免重复日志）。
    """
    ret, data = _futu_call(ctx.get_company_profile, code)
    if ret != 0:
        return None, f"接口失败: {data}"
    row = profile_row(code, data)
    if row is None:
        return None, "富途无公司资料（空表）"
    return row, ""


def fetch_breakdown_rows(ctx, code: str, periods: int) -> list[dict]:
    """单票主营构成（最近 periods 期）。

    首次调用不传 date → 富途直接返回最新期数据 + screen_date_list 全历史期列表；
    其余期逐期调用（1 期 1 次请求）。
    """
    ret, first = _futu_call(ctx.get_financials_revenue_breakdown, code)
    if ret != 0:
        print(f"[{code}] get_financials_revenue_breakdown 失败: {first}")
        return []
    if not isinstance(first, dict):
        return []
    screen = first.get("screen_date_list") or []
    date_map = {str(p.get("period_text") or "").strip(): p.get("date") for p in screen}
    rows = breakdown_rows(code, first, date_map)
    cur_period = str(first.get("period") or "").strip()
    # 期次按 period_text 去重（screen_date_list 偶发同期重复，重复采会同键互相覆盖）
    seen_periods = {cur_period}
    others = []
    for p in screen:
        pt = str(p.get("period_text") or "").strip()
        if not pt or pt in seen_periods:
            continue
        seen_periods.add(pt)
        others.append(p)
    for p in others[:max(periods - 1, 0)]:
        ret2, payload = _futu_call(
            ctx.get_financials_revenue_breakdown,
            code, date=p.get("date"), financial_type=p.get("financial_type"))
        if ret2 != 0:
            print(f"[{code}] 主营构成 {p.get('period_text')} 期拉取失败: {payload}")
            continue
        rows.extend(breakdown_rows(code, payload, date_map))
    return rows


# ═════════════════════════════════════════════════════════════
# 入库
# ═════════════════════════════════════════════════════════════

def _dedup_rows(rows: list[dict], key_cols: list[str], tag: str) -> list[dict]:
    """按唯一键组内去重，保留最后一条（与 get_dividend_history._dedup_rows 同款防御）。

    防御 ON CONFLICT DO UPDATE "cannot affect row a second time"：
    同一批 upsert 内出现重复唯一键会直接报错中断整批（psycopg2 语义，实测踩坑）。
    重复来源：富途 breakdown_list 内条目偶发重名 / screen_date_list 期次重复。
    检出重复时打印告警（保留最后一条，不静默丢数据）。
    """
    seen: dict[tuple, dict] = {}
    dups: list[tuple] = []
    for r in rows:
        key = tuple(r.get(c) for c in key_cols)
        if key in seen:
            dups.append(key)
        seen[key] = r
    if dups:
        print(f"[警告] {tag} 批内重复唯一键 {len(dups)} 个（已保留最后一条）: {dups[:3]}")
    return list(seen.values())


def save_profiles(rows: list[dict]) -> int:
    """覆盖式 upsert；空值不覆盖已有值（多源补缺友好）。"""
    if not rows:
        return 0
    rows = _dedup_rows(rows, PROFILE_CONFLICT, "company_profile")
    with get_conn() as conn:
        bulk_upsert(conn, TABLE_PROFILE, rows,
                    conflict_cols=PROFILE_CONFLICT, skip_null_updates=True)
    return len(rows)


def save_breakdown(rows: list[dict]) -> int:
    if not rows:
        return 0
    rows = _dedup_rows(rows, BREAKDOWN_CONFLICT, "company_revenue_breakdown")
    with get_conn() as conn:
        bulk_upsert(conn, TABLE_BREAKDOWN, rows,
                    conflict_cols=BREAKDOWN_CONFLICT, skip_null_updates=True)
    return len(rows)


# ═════════════════════════════════════════════════════════════
# 股票池
# ═════════════════════════════════════════════════════════════

def _market_codes() -> list[str]:
    """全市场可采集正股（quote_universe；表异常/为空时回退 stock_info 全表）。"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code FROM quote_universe "
                "WHERE is_collectable AND sec_type = 'STOCK' ORDER BY stock_code"
            )
            codes = [r[0] for r in cur.fetchall()]
        if codes:
            return codes
        print("[股票池] quote_universe 无数据，回退 stock_info 全表")
    except Exception as e:
        print(f"[股票池] quote_universe 读取失败({type(e).__name__}: {e})，回退 stock_info 全表")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT stock_code FROM stock_info WHERE market IN ('HK','SH','SZ') "
            "ORDER BY stock_code"
        )
        return [r[0] for r in cur.fetchall()]


def _watch_codes() -> list[str]:
    """关注池（stock_info.is_active=TRUE）：主营构成的默认采集范围。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_code FROM stock_info WHERE is_active = TRUE "
                    "ORDER BY stock_code")
        return [r[0] for r in cur.fetchall()]


# ═════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════

def _collect(targets: list[str], watch: set[str], ctx, periods: int,
             skip_breakdown: bool, base: int = 0,
             total: "int | None" = None) -> dict:
    """逐票采集并分批入库。返回统计字典。

    base/total: 日志序号锚点与总规模。断点续采（--start N）时日志序号与首轮衔接
    （如从第 2346 只续采，日志即从 [2346/9136] 开始，便于对照中断位置）。
    """
    t0 = time.time()
    total = total or len(targets)
    prof_buf: list[dict] = []
    brk_buf: list[dict] = []
    n_prof = n_brk = ok = fail = 0

    def _flush_prof():
        """批次入库；单批失败只告警不中断整批（后续票继续采，可重跑补齐）。"""
        nonlocal n_prof, prof_buf
        if not prof_buf:
            return
        try:
            n_prof += save_profiles(prof_buf)
        except Exception as e:
            print(f"[DB] profile 批次入库失败（{len(prof_buf)} 行，可重跑补齐）: "
                  f"{type(e).__name__}: {e}")
        prof_buf = []

    def _flush_brk():
        nonlocal n_brk, brk_buf
        if not brk_buf:
            return
        try:
            n_brk += save_breakdown(brk_buf)
        except Exception as e:
            print(f"[DB] breakdown 批次入库失败（{len(brk_buf)} 行，可重跑补齐）: "
                  f"{type(e).__name__}: {e}")
        brk_buf = []

    for i, code in enumerate(targets, 1):
        name, note, brk_n = "", "", 0
        try:
            pr, note = fetch_profile_row(ctx, code)
            if pr:
                prof_buf.append(pr)
                ok += 1
                name = str(pr.get("company_name") or "")
            else:
                fail += 1
            if not skip_breakdown and code in watch:
                try:
                    rows = fetch_breakdown_rows(ctx, code, periods)
                except Exception as e:      # breakdown 异常不影响 profile 已入库的事实
                    rows = []
                    note = (note + "；" if note else "") + \
                        f"breakdown 异常 {type(e).__name__}: {e}"
                brk_buf.extend(rows)
                brk_n = len(rows)
        except Exception as e:      # 单票异常隔离，不中断整批
            fail += 1
            note = f"异常 {type(e).__name__}: {e}"
        if len(prof_buf) >= PROFILE_BATCH or i == len(targets):
            _flush_prof()
        if len(brk_buf) >= BREAKDOWN_BATCH or i == len(targets):
            _flush_brk()
        # 每票一条日志（用户要求：采集成功即打印；失败/跳过同样入日志便于长跑定位）
        ts = datetime.now().strftime("%H:%M:%S")
        if note:
            print(f"{ts} [{base + i}/{total}] {code} 失败：{note}"
                  f"（累计 ok={ok} fail={fail}）")
        else:
            detail = f"profile + breakdown {brk_n} 行" if brk_n else "profile"
            print(f"{ts} [{base + i}/{total}] {code} {name} 完成"
                  f"（{detail}；累计 ok={ok} fail={fail}）")

    _flush_prof()
    _flush_brk()
    stats = {
        "targets": len(targets), "ok": ok, "fail": fail,
        "profile_saved": n_prof, "breakdown_saved": n_brk,
        "elapsed_s": round(time.time() - t0, 1),
    }
    return stats


def run(codes=None, ctx=None, periods: int = BREAKDOWN_PERIODS,
        skip_breakdown: bool = False, limit: int = 0,
        watch_only: bool = False, start: int = 1,
        start_code: "str | None" = None) -> dict:
    """采集入口（常驻调用 / CLI 共用）。

    codes: None/空 = 全市场模式（profile 全市场 + breakdown 仅关注池）；
           list    = 指定票模式（profile + breakdown 都采）。
    periods: breakdown 采集的报告期数（最近 N 期，默认 4）。
    skip_breakdown: True 时只采公司资料。
    limit: >0 时对最终目标列表只取前 N 只（调试/分批用）。
    watch_only: True 时全市场范围收窄为关注池（is_active），profile + breakdown 都采。
    start: 从第 N 只开始（1-based，与日志序号一致）——中断后续采用。
    start_code: 从指定代码开始（在目标列表中定位，优先级高于 start）。

    断点说明：序号锚定「本次运行的目标列表」（全市场按 stock_code 排序；
    quote_universe 每日 08:30 刷新，票数变化会让同一序号指向不同票 →
    跨天续采建议用 start_code）。start 与 limit 组合语义：先断点截断、再取前 limit 只。

    注意：富途「公司详情」限流为 30 次/30 秒，本脚本已按 26 次/30 秒 节流；
    全市场约 9100 只 ≈ 2.7 小时（周任务，夜里跑）。
    """
    # own_ctx：自己创建就自己关闭（get_stock_sector.py 同款模式）。
    # 常驻调用（scheduler）传入共享 ctx → 不能关（其他任务还在复用）；
    # CLI 独立运行自建 ctx → 收尾必须 close，否则 futu 非 daemon 接收线程
    # 会让进程跑完不退出（表现为「命令挂住无输出」）。
    own_ctx = ctx is None
    if own_ctx:
        ctx = get_shared_ctx()
    try:
        if codes:
            targets = [c.strip() for c in codes if str(c).strip()]
            watch = set(targets)
        else:
            if watch_only:
                targets = _watch_codes()
                watch = set(targets) if not skip_breakdown else set()
            else:
                targets = _market_codes()
                watch = set() if skip_breakdown else set(_watch_codes())

        # ── 断点续采：--start-code 优先于 --start；之后再用 --limit 限量 ──
        total = len(targets)
        base = 0
        if start_code:
            key = str(start_code).strip()
            if key not in targets:
                print(f"[断点] 目标列表中找不到 {key}，退出"
                      f"（可先不加 --start-code 运行以确认目标范围）")
                return {}
            base = targets.index(key)
            targets = targets[base:]
        elif start > 1:
            base = min(start - 1, total)
            targets = targets[base:]
        if limit > 0:
            targets = targets[:limit]

        if not codes:
            scope = "关注池" if watch_only else "全市场"
            rng = f"（全局序号 {base + 1}~{base + len(targets)}，共 {total} 只）"
            if skip_breakdown:
                print(f"[{scope}] profile 目标 {len(targets)} 只{rng}；breakdown 跳过")
            else:
                print(f"[{scope}] profile 目标 {len(targets)} 只{rng}；"
                      f"breakdown 目标 {len(watch)} 只（最近 {periods} 期）")

        if not targets:
            print("无采集目标，退出")
            return {}

        stats = _collect(targets, watch, ctx, periods, skip_breakdown,
                         base=base, total=total)
        print(f"公司资料采集完成: {stats}")
        return stats
    finally:
        if own_ctx:
            close_shared_ctx()


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="公司资料 + 主营构成采集（富途）")
    ap.add_argument("code", nargs="*", default=[],
                    help="股票代码（如 HK.00700 SH.600900）；不填 = 全市场")
    ap.add_argument("--periods", type=int, default=BREAKDOWN_PERIODS,
                    help=f"主营构成采集最近 N 期（默认 {BREAKDOWN_PERIODS}）")
    ap.add_argument("--skip-breakdown", action="store_true",
                    help="只采公司资料，不采主营构成")
    ap.add_argument("--limit", type=int, default=0,
                    help="全市场模式只采前 N 只（调试用）")
    ap.add_argument("--watch-only", action="store_true",
                    help="只采关注池（stock_info.is_active），profile + breakdown 都采")
    ap.add_argument("--start", type=int, default=1,
                    help="从第 N 只开始采集（1-based，与日志序号一致；中断后续采用）")
    ap.add_argument("--start-code", default=None,
                    help="从指定代码开始采集（在目标列表定位，优先级高于 --start）")
    args = ap.parse_args()
    run(args.code or None, periods=args.periods,
        skip_breakdown=args.skip_breakdown, limit=args.limit,
        watch_only=args.watch_only, start=args.start, start_code=args.start_code)
