#!/usr/bin/env python3
"""市场情绪事件采集 —— IPO / 基金发行 / 解禁 / 董监高增减持。

四个子采集共用本入口（调度一次跑全量，`--kind` 可单跑）：

| kind | 源（AKShare） | 表 | 粒度 | 写入方式 |
|---|---|---|---|---|
| ipo | `stock_xgsglb_em`（东财新股） | `regime.ipo_event` | 一票一行 | 全量快照 upsert + skip_null_updates |
| fund | `fund_new_found_em`（东财新基金） | `regime.fund_issuance_event` | 一基一行 | 同上（募集份额滞后到账，禁覆盖） |
| unlock | `stock_restricted_release_detail_em` | `regime.unlock_schedule` | 一票一次解禁 | 窗口 [today-30, today+180] upsert |
| insider | `stock_hold_management_detail_cninfo`（巨潮） | `regime.insider_trade` | 一事一行 | 全量（源仅滚动近 1 年）upsert |

口径要点（均为实测结论）：
- `ipo_event.list_date` 为空 = 仅申购排期（待上市）→ upsert 必须 `skip_null_updates`，
  否则每天重跑会把已补上的上市日期/首日涨幅抹成 NULL；
- `fund_issuance_event.issue_share` 源侧滞后（新成立当日多为空、数日后才有值）→ 同上；
- unlock：源为**个股级**解禁明细，市场压力由查询按 `unlock_date` 聚合（不另建汇总表）；
  实测可一次拉 2016-2026（24,893 行）→ `--backfill unlock` 按年分块回填；
- insider：分「增持/减持」两次调用；含两种口径（临时公告=事件级有成交均价 /
  定期报告=季报持股快照，`change_ratio` 源口径未校准勿直接当百分比用）；
  去重键 md5(公告日+董监高+股数+口径)，源滚动近 1 年 → 更早历史需逐日累积；
- 四张表**显式写 `updated_at`**：监控以 `updated_at` 判「采集器是否还活着」（day/1）——
  事件表业务日期稀疏（可能数日无新事件），不能用业务日期判停滞。

用法:
    python3 get_market_events.py                        # 全部四类（调度用）
    python3 get_market_events.py --kind unlock          # 只跑解禁
    python3 get_market_events.py --backfill unlock      # 回填解禁历史（2016 起按年分块）
    python3 get_market_events.py --dry-run              # 只拉不写
"""
import argparse
import datetime
import hashlib
import sys

import pandas as pd

from db import get_conn, bulk_upsert

T_IPO = "regime.ipo_event"
T_FUND = "regime.fund_issuance_event"
T_UNLOCK = "regime.unlock_schedule"
T_INSIDER = "regime.insider_trade"

S_IPO = "akshare:stock_xgsglb_em"
S_FUND = "akshare:fund_new_found_em"
S_UNLOCK = "akshare:stock_restricted_release_detail_em"
S_INSIDER = "akshare:stock_hold_management_detail_cninfo"

KINDS = ("ipo", "fund", "unlock", "insider")
EXCHANGE_MAP = {"上海": "SH", "深圳": "SZ", "北京": "BJ"}
# 解禁窗口：回看 30 天（补数据修订/漏跑）+ 前瞻 180 天（未来压力测算）
UNLOCK_LOOKBACK, UNLOCK_LOOKAHEAD = 30, 180
BACKFILL_FIRST_YEAR = 2016


# ── 基础工具 ───────────────────────────────────────────────────────────────
def _prefix(code: str) -> str:
    """A 股 6 位代码 → SH./SZ./BJ. 前缀（含北交所，实测源含 92xxxx/83xxxx）。"""
    code = str(code).strip().zfill(6)
    if code.startswith(("92", "83", "87", "88", "43")):
        return "BJ." + code
    if code.startswith(("60", "68", "90")):
        return "SH." + code
    return "SZ." + code


def _num(v):
    x = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(x) else float(x)


def _int(v):
    x = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(x) else int(x)


def _date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    d = pd.to_datetime(v, errors="coerce")
    return None if pd.isna(d) else d.date()


def _str(v, limit=None):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s[:limit] if limit else s


def _add_days(d: datetime.date, days: int) -> datetime.date:
    return d + datetime.timedelta(days=days)


def _upsert(table: str, conflict: list, rows: list, dry_run: bool) -> int:
    """统一写入：显式刷 updated_at + skip_null_updates（保护后到字段）。

    写入前必须按业务唯一键**显式去重**：源侧可能返回关键字段完全相同的多行，
    同一批 INSERT 内出现重复冲突键会被 PG 直接拒绝
    （`ON CONFLICT DO UPDATE command cannot affect row a second time`）；
    跨页的重复则会被静默合并、行数对不上，同样掩盖源侧异常。
    """
    if not rows:
        print(f"  ⚠️ {table}: 源返回 0 行，跳过写入")
        return 0
    seen = {}
    for r in rows:
        seen[tuple(r.get(c) for c in conflict)] = r
    if len(seen) != len(rows):
        print(f"  ⚠️ 源侧重复 {len(rows) - len(seen)} 行（按 {conflict} 去重，保留后者）")
        rows = list(seen.values())
    if dry_run:
        print(f"  --dry-run：{table} 不写库（{len(rows)} 行）")
        return len(rows)
    now = datetime.datetime.now()
    for r in rows:
        r["updated_at"] = now
    with get_conn() as conn:
        before = _count(conn, table)
        bulk_upsert(conn, table, rows, conflict_cols=conflict, skip_null_updates=True)
        conn.commit()
        after = _count(conn, table)
    print(f"  ✅ {table}: 提交 {len(rows)} 行，库内 {before:,} → {after:,}（+{after - before:,}）")
    return len(rows)


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


# ── ① IPO 明细 ─────────────────────────────────────────────────────────────
def collect_ipo(dry_run: bool = False) -> int:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    print("[1/4] IPO 明细（stock_xgsglb_em）")
    df = ak.stock_xgsglb_em(symbol="全部股票")
    rows = []
    for _, r in df.iterrows():
        code = _str(r.get("股票代码"))
        if not code or not code.isdigit():
            continue
        ex = str(r.get("交易所") or "")
        market = next((v for k, v in EXCHANGE_MAP.items() if k in ex), None)
        issue_price = _num(r.get("发行价格"))
        first_close = _num(r.get("首日收盘价"))
        rows.append({
            "stock_code": _prefix(code),
            "stock_name": _str(r.get("股票简称"), 80),
            "market": market,
            "board": _str(r.get("板块"), 24),
            "subscribe_date": _date(r.get("申购日期")),
            "list_date": _date(r.get("上市日期")),
            "issue_price": issue_price,
            "issue_pe": _num(r.get("发行市盈率")),
            "industry_pe": _num(r.get("行业市盈率")),
            "issue_shares": _num(r.get("发行总数")),          # 万股
            "lottery_rate": _num(r.get("中签率")),
            "first_day_close": first_close,
            "first_day_chg": _num(r.get("涨幅")),
            "is_break_issue": (first_close < issue_price)
                              if (first_close is not None and issue_price) else None,
            "limit_up_days": _int(r.get("连续一字板数量")),    # 待上市行为文本 → None
            "source": S_IPO,
        })
    n_listed = sum(1 for r in rows if r["list_date"])
    print(f"  源 {len(df)} 行 → 规范化 {len(rows)} 行（已上市 {n_listed}，待上市/排期 {len(rows) - n_listed}）")
    return _upsert(T_IPO, ["stock_code"], rows, dry_run)


# ── ② 新成立基金 ───────────────────────────────────────────────────────────
def collect_fund(dry_run: bool = False) -> int:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    print("[2/4] 新成立基金（fund_new_found_em）")
    df = ak.fund_new_found_em()
    rows = []
    for _, r in df.iterrows():
        code = _str(r.get("基金代码"))
        if not code:
            continue
        rows.append({
            "fund_code": code,
            "fund_name": _str(r.get("基金简称"), 160),
            "company": _str(r.get("发行公司"), 80),
            "fund_type": _str(r.get("基金类型"), 40),
            "subscribe_period": _str(r.get("集中认购期"), 40),
            "setup_date": _date(r.get("成立日期")),
            "issue_share": _num(r.get("募集份额")),           # 亿份（常滞后为空）
            "manager": _str(r.get("基金经理"), 80),
            "source": S_FUND,
        })
    n_share = sum(1 for r in rows if r["issue_share"] is not None)
    print(f"  源 {len(df)} 行 → 规范化 {len(rows)} 行（有募集份额 {n_share}）")
    return _upsert(T_FUND, ["fund_code"], rows, dry_run)


# ── ③ 限售解禁 ─────────────────────────────────────────────────────────────
def _fetch_unlock(start: datetime.date, end: datetime.date) -> list:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    df = ak.stock_restricted_release_detail_em(
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    rows = []
    for _, r in df.iterrows():
        code = _str(r.get("股票代码"))
        if not code or not code.isdigit():
            continue
        rows.append({
            "stock_code": _prefix(code),
            "stock_name": _str(r.get("股票简称"), 80),
            "unlock_date": _date(r.get("解禁时间")),
            "holder_type": _str(r.get("限售股类型"), 64) or "未知",
            "unlock_shares": _int(r.get("解禁数量")),
            "actual_shares": _int(r.get("实际解禁数量")),
            "unlock_value": _num(r.get("实际解禁市值")),
            "float_ratio": _num(r.get("占解禁前流通市值比例")),
            "pre_close": _num(r.get("解禁前一交易日收盘价")),
            "source": S_UNLOCK,
        })
    return [r for r in rows if r["unlock_date"]]


def collect_unlock(dry_run: bool = False, start: datetime.date = None,
                   end: datetime.date = None) -> int:
    today = datetime.date.today()
    start = start or _add_days(today, -UNLOCK_LOOKBACK)
    end = end or _add_days(today, UNLOCK_LOOKAHEAD)
    print(f"[3/4] 限售解禁（stock_restricted_release_detail_em）窗口 {start} ~ {end}")
    rows = _fetch_unlock(start, end)
    n_future = sum(1 for r in rows if r["unlock_date"] > today)
    print(f"  → {len(rows)} 行（未来待解禁 {n_future}）")
    return _upsert(T_UNLOCK, ["stock_code", "unlock_date", "holder_type"], rows, dry_run)


def backfill_unlock(first_year: int, dry_run: bool = False) -> int:
    """按年分块回填解禁历史（实测单次可跨年，仍按年切分以便断点续跑/观察进度）。"""
    today = datetime.date.today()
    total = 0
    for year in range(first_year, today.year + 1):
        s = datetime.date(year, 1, 1)
        e = min(datetime.date(year, 12, 31), today)
        if s > today:
            break
        print(f"[回填] 解禁 {year}: {s} ~ {e}")
        rows = _fetch_unlock(s, e)
        print(f"  → {len(rows)} 行")
        total += _upsert(T_UNLOCK, ["stock_code", "unlock_date", "holder_type"], rows, dry_run)
    return total


# ── ④ 董监高增减持 ─────────────────────────────────────────────────────────
def collect_insider(dry_run: bool = False) -> int:
    import warnings
    warnings.filterwarnings("ignore")
    import akshare as ak

    print("[4/4] 董监高增减持（stock_hold_management_detail_cninfo）")
    rows = []
    n_bj = 0
    for kind, direction in (("增持", "BUY"), ("减持", "SELL")):
        df = ak.stock_hold_management_detail_cninfo(symbol=kind)
        n_tmp = 0
        for _, r in df.iterrows():
            code = _str(r.get("证券代码"))
            if not code or not code.isdigit():
                continue
            # 北交所不采：① 源侧「变动数量」量纲不可靠（实测出现 79 亿股级别失真值，
            #   1168 条事件级记录占全市场金额 65%）；② 不在本项目 A 股池（SH/SZ）内。
            if _prefix(code).startswith("BJ"):
                n_bj += 1
                continue
            ann = _date(r.get("公告日期"))
            end = _date(r.get("截止日期"))
            name = _str(r.get("董监高姓名"), 80) or _str(r.get("高管姓名"), 80)
            shares = _int(r.get("变动数量"))
            price = _num(r.get("成交均价"))
            data_kind = _str(r.get("数据来源"), 16)
            dedup = hashlib.md5(
                f"{code}|{ann}|{end}|{name}|{shares}|{data_kind}".encode()).hexdigest()
            rows.append({
                "stock_code": _prefix(code),
                "stock_name": _str(r.get("证券简称"), 80),
                "direction": direction,
                "ann_date": ann,
                "end_date": end,
                "holder_name": name,
                "position": _str(r.get("董监高职务"), 64),
                "change_shares": shares,
                "change_ratio": _num(r.get("变动比例")),
                "avg_price": price,
                "end_shares": _num(r.get("期末持股数量")),      # 万股
                "event_value": abs(shares) * price if (shares and price) else None,
                "reason": _str(r.get("持股变动原因"), 64),
                "data_kind": data_kind,
                "dedup_key": dedup,
                "source": S_INSIDER,
            })
            n_tmp += 1
        print(f"  {kind}: 源 {len(df)} 行 → {n_tmp} 行（{direction}）")
    n_event = sum(1 for r in rows if r["data_kind"] == "临时公告")
    print(f"  合计 {len(rows)} 行（事件级临时公告 {n_event}，定期报告快照 {len(rows) - n_event}）")
    if n_bj:
        print(f"  ℹ️ 已跳过北交所 {n_bj} 行（源侧量纲不可靠 + 不在本项目 A 股池）")
    return _upsert(T_INSIDER, ["stock_code", "source", "dedup_key"], rows, dry_run)


# ── 入口 ───────────────────────────────────────────────────────────────────
def run(codes=None, ctx=None, kinds=None, start=None, end=None, dry_run=False):
    kinds = list(kinds or KINDS)
    print("=" * 62)
    print(f"市场情绪事件采集：{', '.join(kinds)}")
    print("=" * 62)
    total = 0
    if "ipo" in kinds:
        total += collect_ipo(dry_run)
    if "fund" in kinds:
        total += collect_fund(dry_run)
    if "unlock" in kinds:
        total += collect_unlock(dry_run, start, end)
    if "insider" in kinds:
        total += collect_insider(dry_run)
    print("-" * 62)
    print(f"合计提交 {total} 行")
    return total


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default=",".join(KINDS),
                    help=f"逗号分隔，可选：{','.join(KINDS)}")
    ap.add_argument("--start", default=None, help="解禁窗口起（YYYY-MM-DD）")
    ap.add_argument("--end", default=None, help="解禁窗口止（YYYY-MM-DD）")
    ap.add_argument("--backfill", default=None, choices=["unlock"],
                    help="回填模式（目前仅 unlock，2016 起按年分块）")
    ap.add_argument("--first-year", type=int, default=BACKFILL_FIRST_YEAR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.backfill == "unlock":
        print("=" * 62)
        print(f"回填：限售解禁历史（{args.first_year} 起）")
        print("=" * 62)
        backfill_unlock(args.first_year, args.dry_run)
        return 0

    kinds = [k.strip() for k in args.kind.split(",") if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        print(f"未知 kind: {bad}，可选 {KINDS}")
        return 2
    _f = datetime.date.fromisoformat
    run(kinds=kinds,
        start=_f(args.start) if args.start else None,
        end=_f(args.end) if args.end else None,
        dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
