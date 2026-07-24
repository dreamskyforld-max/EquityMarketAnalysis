#!/usr/bin/env python3
"""
支撑位 / 压力位（S/R）分析 —— 第一版
===================================================================
数据源（均为稳定表）：
  1. tick_data    → 成交量分布 Volume Profile（密集成交区 = 支撑/压力）
  2. daily_quote  → 长期拐点(swing high/low)、枢轴点、均线、52周高低
  3. daily_cbbc   → 牛熊证街货回收价（港股专属强信号：
                     牛证回收价=下方支撑/杀牛目标，熊证回收价=上方压力/杀熊目标）

说明：
  - 本模块仅做"市场共识位"识别，定位为解释+纪律+指标验证工具，
    不宣称能预测突破方向（项目已有结论：港股分钟/日线方向近随机，弱式有效）。
  - 用法应是看价格"接近该位时的反应"（减速/放量反转 vs 缩量穿过），而非方向准确率。

输出：dict（含 current_price / support[] / resistance[]，每项带 level/score/sources/details），
      同时打印可读摘要。供后续 UI 复用。

用法：
    python3 support_resistance.py                      # 默认 HK.00700，截至今天
    python3 support_resistance.py HK.00700              # 指定股票
    python3 support_resistance.py HK.00700 2026-07-23   # 指定截至日期
"""
import sys
import math
from datetime import date, timedelta
from db import get_conn


# ---------- 工具 ----------
def _auto_bucket(price):
    """按现价自动选分桶步长（约 0.2% 价位，最低 0.5）。"""
    if not price:
        return 1.0
    return max(0.5, round(price * 0.002))


# ---------- 1. Volume Profile（tick_data 价格分桶聚合） ----------
def volume_profile(conn, stock_code, start_date, bucket):
    cur = conn.cursor()
    cur.execute("""
        SELECT (FLOOR(price / %(bucket)s) * %(bucket)s)::numeric AS pb,
               SUM(volume)                                            AS vol,
               SUM(CASE WHEN ticker_direction='BUY'  THEN volume ELSE 0 END) AS buy_vol,
               SUM(CASE WHEN ticker_direction='SELL' THEN volume ELSE 0 END) AS sell_vol,
               SUM(turnover)                                          AS tov
        FROM tick_data
        WHERE stock_code = %(code)s
          AND ticker_direction IN ('BUY','SELL')
          AND tick_time >= %(start)s
        GROUP BY 1
        ORDER BY 1
    """, {"code": stock_code, "bucket": bucket, "start": start_date})
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return [], {"source": "tick_data (成交量分布 Volume Profile)", "start_date": str(start_date),
                    "n_buckets": 0, "total_volume": 0, "top_buckets": []}
    buckets = [(float(r[0]), int(r[1]), int(r[2]), int(r[3]), float(r[4] or 0)) for r in rows]
    vols = [b[1] for b in buckets]
    mean_v, max_v = sum(vols) / len(vols), max(vols)
    cands = []
    n = len(buckets)
    total_vol = sum(vols)
    for i, b in enumerate(buckets):
        is_peak = (i == 0 or b[1] >= buckets[i - 1][1]) and (i == n - 1 or b[1] >= buckets[i + 1][1])
        is_hot = b[1] > mean_v * 1.2
        if (is_peak and is_hot) or b[1] == max_v:
            cands.append({
                "level": round(b[0] + bucket / 2, 4),           # 桶中心
                "source": "volume_profile",
                "weight": b[1] / max_v if max_v else 0.0,
                "meta": {"volume": b[1], "buy_volume": b[2], "sell_volume": b[3],
                         "turnover": b[4], "bucket": bucket},
            })
    top = sorted(buckets, key=lambda b: b[1], reverse=True)[:15]
    top_buckets = [{
        "level": round(b[0] + bucket / 2, 2),
        "volume": b[1],
        "pct": round(b[1] / total_vol * 100, 2) if total_vol else 0,
        "buy_volume": b[2],
        "sell_volume": b[3],
        "turnover": round(b[4], 1),
    } for b in top]
    info = {
        "source": "tick_data (成交量分布 Volume Profile)",
        "start_date": str(start_date),
        "n_buckets": len(rows),
        "total_volume": total_vol,
        "top_buckets": top_buckets,
    }
    return cands, info


# ---------- 2. 长期结构位（daily_quote） ----------
def daily_structural(conn, stock_code, as_of_date, lookback_days):
    cur = conn.cursor()
    need = lookback_days + 12                                   # 多取给 swing 窗口
    cur.execute("""
        SELECT trade_date, high_price, low_price, last_price
        FROM daily_quote
        WHERE stock_code = %(code)s AND trade_date <= %(asof)s
        ORDER BY trade_date DESC
        LIMIT %(need)s
    """, {"code": stock_code, "asof": as_of_date, "need": need})
    rows = cur.fetchall()[::-1]                                 # 升序
    cur.close()
    if not rows:
        return [], None
    dates = [r[0] for r in rows]
    highs = [float(r[1]) for r in rows]
    lows = [float(r[2]) for r in rows]
    closes = [float(r[3]) for r in rows]
    cands = []
    swing_highs, swing_lows = [], []
    W = 5
    for i in range(len(rows)):
        lo, hi = max(0, i - W), min(len(rows), i + W + 1)
        if highs[i] == max(highs[lo:hi]):
            swing_highs.append({"date": str(dates[i]), "level": highs[i]})
            cands.append({"level": highs[i], "source": "swing_high", "weight": 1.0,
                          "meta": {"date": str(dates[i])}})
        if lows[i] == min(lows[lo:hi]):
            swing_lows.append({"date": str(dates[i]), "level": lows[i]})
            cands.append({"level": lows[i], "source": "swing_low", "weight": 1.0,
                          "meta": {"date": str(dates[i])}})
    # 枢轴点（最新一日 H/L/C）
    H, L, C = highs[-1], lows[-1], closes[-1]
    PP = (H + L + C) / 3
    pivots = {"base_date": str(dates[-1])}
    for k, v in {
        "R3": H + 2 * (PP - L), "R2": PP + (H - L), "R1": 2 * PP - L,
        "S1": 2 * PP - H, "S2": PP - (H - L), "S3": L - 2 * (H - PP),
    }.items():
        pivots[k] = round(v, 4)
        cands.append({"level": round(v, 4), "source": f"pivot_{k}", "weight": 0.8,
                      "meta": {"base_date": str(dates[-1])}})
    # 均线
    ma = {}
    for win in (20, 60, 120):
        if len(closes) >= win:
            ma[f"ma{win}"] = round(sum(closes[-win:]) / win, 4)
            cands.append({"level": ma[f"ma{win}"], "source": f"ma{win}", "weight": 0.6, "meta": {}})
    # 52周高低
    cur2 = conn.cursor()
    cur2.execute("""
        SELECT high_52w, low_52w FROM daily_quote
        WHERE stock_code = %(code)s AND trade_date <= %(asof)s
          AND high_52w IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
    """, {"code": stock_code, "asof": as_of_date})
    r52 = cur2.fetchone()
    cur2.close()
    w52 = {"high": float(r52[0]), "low": float(r52[1])} if r52 else None
    if r52:
        cands.append({"level": float(r52[0]), "source": "52w_high", "weight": 0.7, "meta": {}})
        cands.append({"level": float(r52[1]), "source": "52w_low", "weight": 0.7, "meta": {}})
    info = {
        "source": "daily_quote (长期结构位)",
        "n_days": len(rows),
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "pivots": pivots,
        "ma": ma,
        "52w": w52,
    }
    return cands, info


# ---------- 3. 牛熊证街货（daily_cbbc） ----------
def cbbc_levels(conn, stock_code, as_of_date, lookback_days, current_price=None):
    """牛熊证的"杀牛/杀熊"机制只对贴近现价的活跃区有效，故对远离现价的
    深度价外证施加指数距离衰减（rel_dist 0→1，~0.15 处衰减到 0.37）。"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, bull_call_level, bull_street_volume,
               bear_call_level, bear_street_volume
        FROM daily_cbbc
        WHERE stock_code = %(code)s AND trade_date <= %(asof)s
        ORDER BY trade_date DESC
        LIMIT %(need)s
    """, {"code": stock_code, "asof": as_of_date, "need": lookback_days})
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return [], {"source": "daily_cbbc (牛熊证街货)", "n_days": 0,
                    "bull_clusters": [], "bear_clusters": [], "history_sample": []}
    max_sv = max([r[2] or 0 for r in rows] + [r[4] or 0 for r in rows] + [1])
    cands = []
    bull_bins, bear_bins = {}, {}
    history = []
    for d, bcl, bsv, ccl, csv in rows:
        for lvl, sv, src, bins in ((bcl, bsv, "cbbc_bull", bull_bins),
                                   (ccl, csv, "cbbc_bear", bear_bins)):
            if not lvl:
                continue
            w = (sv or 0) / max_sv
            if current_price:
                rel = abs(float(lvl) - current_price) / current_price
                w *= max(0.05, math.exp(-rel / 0.15))
            cands.append({"level": float(lvl), "source": src, "weight": w,
                          "meta": {"date": str(d), "street_volume": sv}})
            key = round(float(lvl))                       # 按整数价位聚合，看多少天落在此
            bins.setdefault(key, [0, 0])
            bins[key][0] += 1
            bins[key][1] += (sv or 0)
        history.append({"date": str(d), "bull": float(bcl) if bcl else None,
                        "bull_sv": bsv, "bear": float(ccl) if ccl else None, "bear_sv": csv})

    def top_bins(bins):
        return sorted(({"level": k, "days": v[0], "street_volume": v[1]} for k, v in bins.items()),
                      key=lambda x: x["street_volume"], reverse=True)[:6]

    info = {
        "source": "daily_cbbc (牛熊证街货)",
        "n_days": len(rows),
        "bull_clusters": top_bins(bull_bins),     # 下方牛证回收价聚集区
        "bear_clusters": top_bins(bear_bins),     # 上方熊证回收价聚集区
        "history_sample": history[:20],
    }
    return cands, info


# ---------- 4. 聚类 + 评分 ----------
def _cluster_and_score(cands, current_price, merge_pct=0.008):
    if not cands:
        return []
    merge_abs = (current_price or 0) * merge_pct or 0.5
    s = sorted(cands, key=lambda x: x["level"])
    clusters = []
    for c in s:
        if clusters and abs(c["level"] - clusters[-1]["level"]) <= merge_abs:
            clusters[-1]["members"].append(c)
        else:
            clusters.append({"level": c["level"], "members": [c]})
    out = []
    for cl in clusters:
        members = cl["members"]
        tot_w = sum(m["weight"] for m in members) or 1e-9
        lvl = sum(m["level"] * m["weight"] for m in members) / tot_w
        sources = sorted(set(m["source"] for m in members))
        resonance = 1 + 0.25 * (len(sources) - 1)            # 多源共振加权
        out.append({
            "level": round(lvl, 4),
            "raw_score": tot_w * resonance,
            "sources": sources,
            "touch": len(members),
            "details": {m["source"]: m["meta"] for m in members},
        })
    mx = max((o["raw_score"] for o in out), default=1) or 1
    for o in out:
        o["score"] = round(o["raw_score"] / mx * 100, 1)
        del o["raw_score"]
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ---------- 5. 编排 ----------
def _run(conn, stock_code, as_of_date, lookback_days, vp_lookback_days, bucket, top_k, include_debug=False):
    # 现价：优先 daily_quote，回退 tick_data 最新价
    cur = conn.cursor()
    cur.execute("""
            SELECT last_price FROM daily_quote
            WHERE stock_code = %(code)s AND trade_date <= %(asof)s
              AND last_price IS NOT NULL
            ORDER BY trade_date DESC LIMIT 1
        """, {"code": stock_code, "asof": as_of_date})
    rp = cur.fetchone()
    cur.close()
    if rp:
        current_price = float(rp[0])
    else:
        cur = conn.cursor()
        cur.execute("SELECT price FROM tick_data WHERE stock_code=%(code)s ORDER BY tick_time DESC LIMIT 1",
                    {"code": stock_code})
        rp = cur.fetchone()
        cur.close()
        current_price = float(rp[0]) if rp else None
    if bucket is None:
        bucket = _auto_bucket(current_price)

    vp_start = as_of_date - timedelta(days=vp_lookback_days)
    vp_cands, vp_info = volume_profile(conn, stock_code, vp_start, bucket)
    d_cands, d_info = daily_structural(conn, stock_code, as_of_date, lookback_days)
    cb_cands, cb_info = cbbc_levels(conn, stock_code, as_of_date, lookback_days, current_price)
    cands = vp_cands + d_cands + cb_cands

    all_clustered = _cluster_and_score(cands, current_price)
    support = [c for c in all_clustered if current_price and c["level"] < current_price][:top_k]
    resistance = [c for c in all_clustered if current_price and c["level"] > current_price][:top_k]
    result = {
        "stock_code": stock_code,
        "as_of_date": str(as_of_date),
        "current_price": current_price,
        "bucket": bucket,
        "support": support,
        "resistance": resistance,
        "n_candidates": len(cands),
        "windows": {
            "volume_profile": f"{vp_start} ~ {as_of_date} (近 {vp_lookback_days} 天 tick)",
            "daily_structural": f"近 {lookback_days} 天日线 (实际 {d_info.get('n_days')} 行)",
            "cbbc": f"近 {lookback_days} 天 (实际 {cb_info.get('n_days')} 行)",
        },
    }
    if include_debug:
        result["debug"] = {"volume_profile": vp_info, "daily_structural": d_info, "cbbc": cb_info}
    return result


def analyze_support_resistance(stock_code="HK.00700", as_of_date=None,
                               lookback_days=90, vp_lookback_days=30,
                               bucket=None, top_k=8, conn=None, include_debug=False):
    if as_of_date is None:
        as_of_date = date.today()
    if conn is not None:
        return _run(conn, stock_code, as_of_date, lookback_days,
                    vp_lookback_days, bucket, top_k, include_debug)
    with get_conn() as conn:
        return _run(conn, stock_code, as_of_date, lookback_days,
                    vp_lookback_days, bucket, top_k, include_debug)


# ---------- 打印 ----------
def _fmt(n):
    return f"{n:,}" if isinstance(n, (int, float)) else "-"


def _print_level_details(c):
    for src, meta in c.get("details", {}).items():
        if src == "volume_profile":
            print(f"        └ {src}: 成交量 {_fmt(meta.get('volume'))}  "
                  f"买 {_fmt(meta.get('buy_volume'))} / 卖 {_fmt(meta.get('sell_volume'))}")
        elif src.startswith("cbbc"):
            print(f"        └ {src}: 街货量 {_fmt(meta.get('street_volume'))}  日期 {meta.get('date')}")
        elif src.startswith("swing"):
            print(f"        └ {src}: 日期 {meta.get('date')}")
        else:
            print(f"        └ {src}")


def _print_debug(dbg, current_price):
    print(f'\n{"-" * 72}')
    print("中间数据（用于判断证据充分性）")
    print(f'{"-" * 72}')

    vp = dbg.get("volume_profile", {})
    print(f"\n[1] 成交量分布 Volume Profile  ({vp.get('source')})")
    print(f"    窗口起始 {vp.get('start_date')}  桶数 {vp.get('n_buckets')}  "
          f"总成交量 {_fmt(vp.get('total_volume'))}")
    print(f"    {'价位':>10} {'成交量':>14} {'占比%':>7} {'主动买':>12} {'主动卖':>12}")
    for b in vp.get("top_buckets", []):
        print(f"    {b['level']:>10.2f} {_fmt(b['volume']):>14} {b['pct']:>7} "
              f"{_fmt(b['buy_volume']):>12} {_fmt(b['sell_volume']):>12}")

    d = dbg.get("daily_structural", {})
    print(f"\n[2] 长期结构位  ({d.get('source')})  实际 {d.get('n_days')} 天日线")
    print("    Swing 高点:", ", ".join(f"{s['level']:.2f}({s['date']})" for s in d.get("swing_highs", [])))
    print("    Swing 低点:", ", ".join(f"{s['level']:.2f}({s['date']})" for s in d.get("swing_lows", [])))
    pv = d.get("pivots", {})
    print("    枢轴点:", ", ".join(f"{k}={v:.2f}" for k, v in pv.items() if k != "base_date"),
          f"  (基准 {pv.get('base_date')})")
    print("    均线:", ", ".join(f"{k}={v:.2f}" for k, v in d.get("ma", {}).items()))
    w52 = d.get("52w")
    if w52:
        print(f"    52周: 高 {w52['high']:.2f} / 低 {w52['low']:.2f}")

    cb = dbg.get("cbbc", {})
    print(f"\n[3] 牛熊证街货  ({cb.get('source')})  实际 {cb.get('n_days')} 天")
    print("    下方牛证回收价聚集区 (价位 / 出现天数 / 累计街货):")
    for x in cb.get("bull_clusters", []):
        print(f"      {x['level']:>10.2f}  天数 {x['days']:>3}  街货 {_fmt(x['street_volume'])}")
    print("    上方熊证回收价聚集区:")
    for x in cb.get("bear_clusters", []):
        print(f"      {x['level']:>10.2f}  天数 {x['days']:>3}  街货 {_fmt(x['street_volume'])}")
    print("    近 20 日逐日回收价 / 街货:")
    for h in cb.get("history_sample", []):
        print(f"      {h['date']}  牛 {h['bull']}(街货 {_fmt(h['bull_sv'])})  "
              f"熊 {h['bear']}(街货 {_fmt(h['bear_sv'])})")
    print()


def _print_report(r, verbose=False):
    w = r.get("windows", {})
    print(f'\n{"=" * 72}')
    print(f"支撑/压力位分析  {r['stock_code']}  截至 {r['as_of_date']}  现价 {r['current_price']}")
    print(f"候选点 {r['n_candidates']}  价格分桶 {r['bucket']}")
    for k, v in w.items():
        print(f"  · {k}: {v}")
    print(f'{"=" * 72}')
    print("【压力位】（现价上方）")
    for c in r["resistance"]:
        print(f"  {c['level']:>10.2f}  强度 {c['score']:>5}  触碰 {c['touch']}  来源 {','.join(c['sources'])}")
        if verbose:
            _print_level_details(c)
    print("【支撑位】（现价下方）")
    for c in r["support"]:
        print(f"  {c['level']:>10.2f}  强度 {c['score']:>5}  触碰 {c['touch']}  来源 {','.join(c['sources'])}")
        if verbose:
            _print_level_details(c)
    print()
    if verbose and r.get("debug"):
        _print_debug(r["debug"], r["current_price"])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = len(args) != len(sys.argv[1:])
    code = args[0] if len(args) > 0 else "HK.00700"
    asof = args[1] if len(args) > 1 else None
    if asof:
        asof = date.fromisoformat(asof)
    r = analyze_support_resistance(code, as_of_date=asof, include_debug=verbose)
    _print_report(r, verbose=verbose)
