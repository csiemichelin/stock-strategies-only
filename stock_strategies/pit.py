"""策略實驗室・當時名單版（point-in-time）。

原本的實驗室拿「現在」的觀察清單回頭測，等於事後挑贏家——名單裡的股票等權重抱著就比 0050 多七個百分點，
選股型策略的好成績有多少是名單送的分不出來。這裡改成：每個月底只用當天為止的資料排名單
（上市普通股、近 60 個交易日平均成交金額前 100 名），策略只能從當時的名單裡挑，後來下市的股票也照樣算。

資料全部來自證交所與公開資訊觀測站（twse.py），不花 FinMind 額度。驗證方法跟實驗室一樣：
walk-forward、成本照實扣、同時要贏過 0050 和「等權重持有當時名單」才算有證據。

    uv run python -m stock_strategies.pit            # 抓資料（第一次約三小時）＋驗證，報告存到 webstore
    uv run python -m stock_strategies.pit --if-due 25
"""

from __future__ import annotations

import bisect
import sys
import time
from datetime import datetime, timedelta

import pandas as pd

from . import lab, twse

TOP = 100                # 名單大小
OTHER_TOPS = (50, 200)   # 另外兩種名單大小：只用來看結論穩不穩，不拿來挑
LIQUIDITY_DAYS = 60      # 用近 60 個交易日的平均成交金額排名
MIN_TRADED = 40          # 這 60 天裡至少 40 天有成交
JUMP = 0.105             # 上市股票單日漲跌停 10%：超過就是參考價被調整過（分割、減資）
SLIP = 0.001             # 滑價測試：買賣各再讓 0.1%
REPORT = "lab_pit_report"
TRACK = "lab_track"      # 模擬帳戶從哪一天開始、LINE 通知到哪一次了
ORDER = ("none", "tentative", "strong")
CANDIDATES = [c for c in lab.CANDIDATES if c["id"] in ("revenue", "combo", "trend")]


def _log(msg: str) -> None:
    print(f"[pit] {datetime.now(lab.TZ):%H:%M:%S} {msg}", flush=True)


def revenue_series(months: dict) -> dict | None:
    """{YYYY-MM: 當月營收} → 實驗室用的格式：公布期限（次月 10 日）、年增率、近 3 個月平均 ÷ 近 12 個月平均。"""
    if len(months) < 13:
        return None
    idx = pd.period_range(min(months), max(months), freq="M")
    rev = pd.Series([months.get(str(p)) for p in idx], index=idx, dtype=float)
    rev = rev.where(rev > 0)
    yoy = rev / rev.shift(12) - 1
    accel = rev.rolling(3).mean() / rev.rolling(12).mean()
    avail = [(p.to_timestamp(how="end").normalize() + pd.Timedelta(days=10)).strftime("%Y-%m-%d") for p in idx]
    return {"avail": avail, "yoy": [float(v) for v in yoy], "accel": [float(v) for v in accel]}


def rank_universe(quotes: pd.DataFrame, days: list[str], depth: int) -> dict:
    """每個月底的當時名單：{月底: [代號，依近 LIQUIDITY_DAYS 個交易日平均成交金額由大到小]}。只用到當天為止的資料。"""
    stocks = quotes[~quotes["id"].isin(twse.EXTRA)].drop_duplicates(["date", "id"])
    value = stocks.pivot(index="date", columns="id", values="value").reindex(days)
    liquidity = value.fillna(0.0).rolling(LIQUIDITY_DAYS).mean()
    traded = value.notna().rolling(LIQUIDITY_DAYS).sum()
    ranked = {}
    for d in lab.month_ends(days):
        row = liquidity.loc[d][traded.loc[d] >= MIN_TRADED].dropna()
        row = row[row > 0].sort_values(ascending=False)
        if len(row):
            ranked[d] = list(row.index[:depth])
    return ranked


def build(years: int = lab.YEARS, depth: int | None = None) -> lab.Data:
    """抓齊（或讀快取）證交所資料、排出每個月底的當時名單，回傳實驗室引擎用的資料。"""
    depth = depth or max(TOP, *OTHER_TOPS)
    start = (datetime.now() - timedelta(days=365 * years + 60)).strftime("%Y-%m-%d")
    cal = twse.calendar(start)
    quotes = twse.load_quotes([d for d, _ in cal])
    if quotes.empty:
        raise RuntimeError("抓不到證交所的行情")
    last = quotes["date"].max()
    cal = [(d, v) for d, v in cal if d <= last]
    days = [d for d, _ in cal]
    index = pd.Series([v for _, v in cal], index=days, dtype=float)
    ranked = rank_universe(quotes, days, depth)
    keep = {s for ids in ranked.values() for s in ids} | {lab.BENCH}
    _log(f"交易日 {len(days)} 天（{days[0]}～{days[-1]}）；曾經進過前 {depth} 名的有 {len(keep) - 1} 檔")
    ex = twse.exrights(start)
    px, events, adjusted = {}, 0, 0
    for sid, g in quotes[quotes["id"].isin(keep)].groupby("id"):
        known = [e for e in ex.get(sid, []) if g["date"].iloc[0] < e[0] <= g["date"].iloc[-1]]
        neutral = set(g.loc[g["flag"] == twse.FLAG_X, "date"]) - {d for d, _ in known}
        px[sid], n = lab.adjust(g[["date", "open", "high", "low", "close", "volume"]], known, jump=JUMP,
                                neutral=neutral)
        events += len(known)
        adjusted += n
    if lab.BENCH not in px:
        raise RuntimeError("證交所的行情裡找不到 0050，沒辦法比較")
    rev_start = (pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y-%m")
    rev = twse.revenue(rev_start)
    _check_revenue(rev, rev_start, days, ranked)
    revenue = {}
    for sid in keep:
        series = revenue_series(rev.get(sid) or {})
        if series:
            revenue[sid] = series
    known_names = twse.names()
    names = {s: known_names.get(s, s) for s in keep}
    names[lab.BENCH] = "元大台灣50"
    data = lab.Data(px, index, names, revenue)
    data.ranked, data.top = ranked, TOP
    data.dividend_events, data.other_adjustments = events, max(0, adjusted - events)
    _log(f"股價 {len(px)} 檔、除權息 {events} 筆、其他參考價調整 {data.other_adjustments} 筆、月營收 {len(revenue)} 檔")
    return data


def _check_revenue(rev: dict, rev_start: str, days: list[str], ranked: dict) -> None:
    """月營收沒到齊就不要往下算——少了資料，營收排名會變成只從「剛好有資料的」裡面挑，換股名單就錯了。"""
    decisions = lab.mid_months(days)
    if not decisions:
        return
    decided = decisions[-1]                                             # 最近一次營收換股的決定日
    due = (pd.Timestamp(decided[:7] + "-01") - pd.Timedelta(days=1)).strftime("%Y-%m")  # 那天應該看得到的最新月份
    first = (pd.Timestamp(days[0]) - pd.Timedelta(days=400)).strftime("%Y-%m")  # 第一個交易日往前 13 個月起都要有
    gaps = twse.revenue_gaps(rev, max(rev_start, first), due)
    if gaps:
        raise RuntimeError(f"月營收還沒抓齊（缺 {len(gaps)} 個月：{'、'.join(gaps[:6])}{'…' if len(gaps) > 6 else ''}），抓齊了再驗證")
    lists = [d for d in ranked if d <= decided]
    members = set(ranked[max(lists)][:TOP]) if lists else set()
    have = sum(1 for s in members if due in (rev.get(s) or {}))
    if members and have < 0.9 * len(members):
        raise RuntimeError(f"{due} 的月營收只到 {have}/{len(members)} 檔，等資料到齊再決定換股")


def verdict(stats: dict, control: dict) -> str:
    """當時名單沒有事後偏誤，所以可以給 tentative／strong——但要同時贏過 0050 和「等權重持有當時名單」，取比較弱的那個。"""
    n = len(stats["folds"])
    a = lab._rule(stats["excess_cagr_pct"], stats["t_stat"], stats["win_folds"], n)
    b = lab._rule(control["excess_cagr_pct"], control["t_stat"], control["win_folds"], n)
    return ORDER[min(ORDER.index(a), ORDER.index(b))]


def _walk(data: lab.Data, top: int, frame: dict, slip: float = 0.0) -> dict:
    """用前 top 名的當時名單把對照組和每個候選策略跑一遍 walk-forward。"""
    days, fold_days, oos, grid_start, end, b_ret = (frame[k] for k in ("days", "folds", "oos", "grid_start", "end",
                                                                     "b_ret"))
    data.top = top
    first = sorted(data.eligible(grid_start) or [])
    # 對照組一檔只佔 1/top，本金放大 100 倍免得「買不到一股」或零股進位讓它吃虧
    u_ret = lab.simulate(data, lab.plan_universe(data), grid_start, end, top, initial=first,
                         capital=lab.CAPITAL * 100, slip=slip)["ret"].reindex(oos).fillna(0.0)
    out = []
    for cand in CANDIDATES:
        cid = cand["id"]
        planner, slots = lab.PLANNERS[cid]
        plans = {lab._label(cid, cfg): (planner(data, *cfg), slots(cfg)) for cfg in lab.GRIDS[cid]}
        runs = {k: lab.simulate(data, plan, grid_start, end, n, slip=slip) for k, (plan, n) in plans.items()}
        ret, chosen = lab.walk_forward({k: v["ret"] for k, v in runs.items()}, fold_days, days)
        stats = lab._stats(ret, b_ret, fold_days)
        control = lab._stats(ret, u_ret, fold_days)
        for row, name in zip(stats["folds"], chosen):
            row["config"] = name
        last = runs[chosen[-1]]
        exposure = pd.concat([runs[k]["exposure"].reindex(fd) for k, fd in zip(chosen, fold_days)])
        plan = plans[chosen[-1]][0]
        decided = max(plan) if plan else None
        out.append({**cand, "_live": live_plan(plans, chosen, fold_days, days),
                    "configs": len(runs), "current": chosen[-1], **stats,
                    "vs_universe": {k: control[k] for k in ("excess_cagr_pct", "t_stat", "win_folds")},
                    "verdict": verdict(stats, control),
                    "exposure_pct": round(float(exposure.reindex(oos).fillna(0.0).mean()) * 100, 1),
                    "trades_per_year": last["trades_per_year"], "cost_pct_per_year": last["cost_pct_per_year"],
                    "curve": lab._curve(ret),
                    "picks": {"decided": decided, "config": chosen[-1],
                              "stocks": [{"id": s, "name": data.names.get(s, s),
                                          "close": round(float(data.c[s][decided]), 2)} for s in plan[decided]]}
                    if decided else None})
    return {"u_ret": u_ret, "candidates": out}


def live_plan(plans: dict, chosen: list[str], fold_days: list[list[str]], days: list[str]) -> tuple[dict, dict]:
    """照 walk-forward 每一段選到的參數接起來、實際可以跟著做的換股計畫：（{決策日: 目標}, {決策日: 檔數}）。
    plans：{參數名稱: (換股計畫, 檔數)}。換參數時，新的一段開始前一天多一次換股，換成新參數最近一次決定的名單。"""
    starts = [fd[0] for fd in fold_days]
    where = {d: i for i, d in enumerate(days)}

    def fold_of(day: str) -> int:  # 這次決定在哪一段執行（隔天開盤）
        nxt = days[min(where[day] + 1, len(days) - 1)]
        return max(0, bisect.bisect_right(starts, nxt) - 1)

    plan, sizes = {}, {}
    for d in sorted({d for p, _ in plans.values() for d in p}):
        p, n = plans[chosen[fold_of(d)]]
        if d in p:
            plan[d], sizes[d] = p[d], n
    for k in range(1, len(chosen)):
        if chosen[k] == chosen[k - 1] or where[starts[k]] == 0:
            continue
        eve = days[where[starts[k]] - 1]
        p, n = plans[chosen[k]]
        earlier = [d for d in p if d <= eve]
        if earlier and eve not in plan:
            plan[eve], sizes[eve] = p[max(earlier)], n
    return plan, sizes


def track(data: lab.Data, candidates: list[dict], after: str) -> list[dict]:
    """每個候選策略的模擬帳戶：after 之後第一個交易日開盤，買進當時最近一次決定的名單，之後照計畫換股。
    每次都從頭重算（不存部位），所以漏跑幾天、重跑幾次結果都一樣。"""
    start = next((d for d in data.days if d > after), None)
    end = data.days[-1]
    bench = lab.simulate(data, {}, start, end, 1, initial=[lab.BENCH])["ret"] if start else None
    out = []
    for c in candidates:
        plan, sizes = c["_live"]
        decided = max((d for d in plan if d <= end), default=None)
        seed = max((d for d in plan if d <= after), default=None)
        row = {"id": c["id"], "name": c["name"], "verdict": c["verdict"], "config": c["current"], "since": start,
               "days": 0, "return_pct": 0.0, "bench_pct": 0.0, "holdings": [], "cash_pct": 100.0,
               "decided": decided, "next": None, "changed": None}
        name = lambda s: {"id": s, "name": data.names.get(s, s)}  # noqa: E731
        if decided:  # 最近一次決定的名單跟前一次比，換了哪些（那天晚上沒通知到的話，隔天補通知用）
            prev = max((d for d in plan if d < decided), default=None)
            old, new = (plan[prev] if prev else []), plan[decided]
            row["changed"] = {"sell": [name(s) for s in old if s not in new], "buy": [name(s) for s in new if s not in old]}
        if start is None:  # 還沒開始：下一個交易日開盤要買的
            if seed:
                row["next"] = {"sell": [], "keep": [], "buy": [name(s) for s in plan[seed]]}
            out.append(row)
            continue
        r = lab.simulate(data, plan, start, end, sizes.get(seed, 10), initial=plan[seed] if seed else [], sizes=sizes)
        equity = r["cash"] + sum(r["prices"][s] * n for s, n in r["positions"].items())
        held = sorted(r["positions"], key=lambda s: -r["prices"][s] * r["positions"][s])
        row.update(days=len(r["ret"]), return_pct=round(float((1 + r["ret"]).prod() - 1) * 100, 2),
                   bench_pct=round(float((1 + bench).prod() - 1) * 100, 2),
                   holdings=[{**name(s), "shares": r["positions"][s], "price": round(r["prices"][s], 2),
                              "weight_pct": round(r["prices"][s] * r["positions"][s] / equity * 100, 1)}
                             for s in held],
                   cash_pct=round(r["cash"] / equity * 100, 1) if equity else 100.0)
        if r["pending"] is not None:  # 今天收盤後剛決定：下一個交易日開盤要換成這份名單
            target = set(r["pending"])
            row["next"] = {"sell": [name(s) for s in held if s not in target],
                           "keep": [name(s) for s in held if s in target],
                           "buy": [name(s) for s in r["pending"] if s not in r["positions"]]}
        out.append(row)
    return out


def _brief(c: dict) -> dict:
    return {"id": c["id"], "cagr_pct": c["cagr_pct"], "excess_cagr_pct": c["excess_cagr_pct"], "t_stat": c["t_stat"],
            "win_folds": c["win_folds"], "vs_universe": c["vs_universe"], "verdict": c["verdict"]}


def _notes(data: lab.Data, bench: dict, universe: dict) -> list[str]:
    gap = round(universe["cagr_pct"] - bench["cagr_pct"], 2)
    return [
        f"名單怎麼來：每個月底只用當天為止的資料，把上市普通股依近 {LIQUIDITY_DAYS} 個交易日的平均成交金額排名、取前 {TOP} 名；"
        "策略只能從當時的名單裡挑，後來下市或退出名單的股票當時在名單裡就照樣算。"
        f"把這份當時名單等權重買進抱著，年化 {universe['cagr_pct']}%（同期 0050 是 {bench['cagr_pct']}%，"
        f"{'多' if gap >= 0 else '少'} {abs(gap)} 個百分點）——沒有事後挑名單，這才是選股策略真正要贏的對照。",
        "訊號當天收盤後產生、隔天開盤價成交；手續費 0.1425%（未打折）與證交稅都有扣。"
        f"滑價另外測了一次（買賣各再讓 {SLIP * 100:.1f}%）；漲停買不到、跌停賣不掉的情況沒有模擬。",
        "月營收用公開資訊觀測站的彙總表，等法定期限（次月 10 日，遇假日順延）過後的下一個交易日才使用；"
        "少數晚申報或事後更正的公司，用的是後來的數字。每月換股的決定日都選在當天就能確定的日子，實際上做得到。",
        f"股價用證交所的除權除息計算結果表還原（{data.dividend_events} 筆）；減資、分割等其他參考價調整"
        f"（{getattr(data, 'other_adjustments', 0)} 筆）當天不算報酬。",
        "只涵蓋上市股票（不含上櫃）；資料來源是證交所與公開資訊觀測站的公開資料，不花 FinMind 額度。",
    ]


def _track_after(days: list[str], save: bool) -> str:
    """模擬帳戶從哪一天「之後」開始：第一次跑的那個交易日（隔天開盤才進場，不回頭補買）。"""
    from . import webstore

    try:
        state = webstore.load(TRACK, fresh=True) or {}
    except Exception:  # noqa: BLE001
        state = {}
    if not state.get("after"):
        state["after"] = days[-1]
        if save:
            webstore.save_quietly(TRACK, state)
    return state["after"]


def run(data: lab.Data | None = None, years: int = lab.YEARS, save: bool = True,
        eval_floor: str = "2020-01-01", track_after: str | None = None) -> dict:
    """用當時名單把候選策略驗證一遍，回傳報告（save=True 時存到 webstore 給網頁、LINE 用）。"""
    from . import webstore

    t0 = time.time()
    data = data or build(years)
    days = data.days
    eval_start = max(eval_floor, (pd.Timestamp(days[0]) + pd.Timedelta(days=365 * 3 + 120)).strftime("%Y-%m-%d"))
    grid_start = days[min(len(days) - 1, lab.WARMUP_DAYS)]
    fold_days = lab.folds(days, eval_start)
    if len(fold_days) < 2:
        raise RuntimeError("資料太短，切不出兩段以上的樣本外期間")
    oos = [d for fd in fold_days for d in fd]
    end = days[-1]
    b_ret = lab.simulate(data, {}, grid_start, end, 1, initial=[lab.BENCH])["ret"].reindex(oos).fillna(0.0)
    frame = {"days": days, "folds": fold_days, "oos": oos, "grid_start": grid_start, "end": end, "b_ret": b_ret}
    _log(f"驗證：樣本外 {oos[0]}～{oos[-1]}，{len(fold_days)} 段")
    main_run = _walk(data, TOP, frame)
    slipped = {c["id"]: c for c in _walk(data, TOP, frame, slip=SLIP)["candidates"]}
    others = {top: _walk(data, top, frame) for top in OTHER_TOPS if top <= max(len(v) for v in data.ranked.values())}
    data.top = TOP
    b_stats = lab._stats(b_ret, b_ret, fold_days)
    u_stats = lab._stats(main_run["u_ret"], b_ret, fold_days)
    out = main_run["candidates"]
    after = track_after or _track_after(days, save)
    accounts = track(data, out, after)
    for c in out:
        c.pop("_live")
        c["with_slippage"] = _brief(slipped[c["id"]])
        c["other_tops"] = [{"top": top, **_brief(next(x for x in run_["candidates"] if x["id"] == c["id"]))}
                           for top, run_ in others.items()]
    month_ends = sorted(data.ranked)
    changes = [len(set(data.ranked[b][:TOP]) - set(data.ranked[a][:TOP])) for a, b in zip(month_ends, month_ends[1:])]
    benchmark = {"id": lab.BENCH, "name": "抱 0050（含息）",
                 **{k: b_stats[k] for k in ("total_pct", "cagr_pct", "mdd_pct", "vol_pct", "sharpe")},
                 "folds": b_stats["folds"], "curve": lab._curve(b_ret)}
    universe = {"id": "universe", "name": "當時名單等權重持有", "stocks": TOP,
                **{k: u_stats[k] for k in ("total_pct", "cagr_pct", "excess_cagr_pct", "mdd_pct", "vol_pct", "sharpe",
                                           "t_stat", "win_folds")},
                "folds": u_stats["folds"], "curve": lab._curve(main_run["u_ret"]),
                "other_tops": [{"top": top, "cagr_pct": (s := lab._stats(run_["u_ret"], b_ret, fold_days))["cagr_pct"],
                                "excess_cagr_pct": s["excess_cagr_pct"]} for top, run_ in others.items()]}
    report = {
        "generated_at": datetime.now(lab.TZ).isoformat(timespec="seconds"),
        "period": {"start": oos[0], "end": oos[-1], "days": len(oos), "years": round(len(oos) / 252, 1),
                   "data_start": days[0]},
        "capital": lab.CAPITAL, "fold_months": lab.FOLD_MONTHS, "train_days": lab.TRAIN_DAYS,
        "top": TOP, "liquidity_days": LIQUIDITY_DAYS, "slip_pct": SLIP * 100,
        "trials": sum(c["configs"] for c in out),
        "benchmark": benchmark, "universe": universe, "candidates": out,
        "passed": [c["id"] for c in out if c["verdict"] != "none"],
        "tracking": {"after": after, "as_of": end, "accounts": accounts},
        "data": {"days": len(days), "stocks_ever": len(data.stocks), "with_revenue": len(data.revenue),
                 "dividend_events": data.dividend_events,
                 "other_adjustments": getattr(data, "other_adjustments", 0),
                 "changes_per_month": round(sum(changes) / len(changes), 1) if changes else 0.0},
        "notes": _notes(data, benchmark, universe),
        "seconds": round(time.time() - t0),
    }
    if save:
        webstore.save(REPORT, report)
    return report


def summary(report: dict) -> list[str]:
    p, b, u = report["period"], report["benchmark"], report["universe"]
    lines = [f"樣本外 {p['start']}～{p['end']}：{b['name']} 年化 {b['cagr_pct']}%、最大回撤 {b['mdd_pct']}%",
             f"  對照｜{u['name']}（前 {report['top']} 名）：年化 {u['cagr_pct']}%（比 0050 {u['excess_cagr_pct']:+}），"
             f"回撤 {u['mdd_pct']}%" + "".join(f"；前 {o['top']} 名 {o['cagr_pct']}%" for o in u["other_tops"])]
    for c in report["candidates"]:
        v, s = c["vs_universe"], c["with_slippage"]
        lines.append(
            f"  {c['name']}：年化 {c['cagr_pct']}%（比 0050 {c['excess_cagr_pct']:+}），回撤 {c['mdd_pct']}%，"
            f"贏 {c['win_folds']}/{len(c['folds'])} 段，t={c['t_stat']}；比當時名單 {v['excess_cagr_pct']:+}、"
            f"贏 {v['win_folds']} 段、t={v['t_stat']} → {c['verdict']}｜持股 {c['exposure_pct']}%、"
            f"一年 {c['trades_per_year']} 筆、成本 {c['cost_pct_per_year']}%｜目前參數 {c['current']}")
        lines.append("    各段超額：" + " ".join(f"{f['excess_pct']:+.0f}" for f in c["folds"]))
        lines.append(f"    加滑價：比 0050 {s['excess_cagr_pct']:+}、t={s['t_stat']}、比當時名單 "
                     f"{s['vs_universe']['excess_cagr_pct']:+} → {s['verdict']}"
                     + "".join(f"；前 {o['top']} 名：比 0050 {o['excess_cagr_pct']:+}、比名單 "
                               f"{o['vs_universe']['excess_cagr_pct']:+}（t={o['vs_universe']['t_stat']}）→ {o['verdict']}"
                               for o in c["other_tops"]))
        if c.get("picks"):
            k = c["picks"]
            lines.append(f"    目前持股（{k['decided']} 決定）：" + ("、".join(f"{x['id']} {x['name']}" for x in k["stocks"])
                                                             or "空手"))
    return lines


VERDICT_TEXT = {"strong": "證據強", "tentative": "初步有證據", "none": "沒有足夠證據"}


def _names(items: list[dict]) -> str:
    return "、".join(f"{x['id']} {x['name']}" for x in items) or "無"


def _verdict_line(c: dict) -> str:
    """一句話講這個策略驗證得怎麼樣；沒通過的要講清楚為什麼（數字比 0050 高不等於有證據）。"""
    n, v = len(c["folds"]), c["vs_universe"]
    head = f"・{c['name']}：{VERDICT_TEXT[c['verdict']]}（年化 {c['cagr_pct']}%"
    if c["verdict"] != "none":
        return (f"{head}，每年贏 0050 {c['excess_cagr_pct']} 個百分點、贏當時名單 {v['excess_cagr_pct']}；"
                f"{n} 段贏 {c['win_folds']} 段、t 值 {c['t_stat']}）")
    if c["excess_cagr_pct"] > 0:
        return (f"{head}、比 0050 多 {c['excess_cagr_pct']} 個百分點，但不穩定：{n} 段只贏 {c['win_folds']} 段、"
                f"t 值 {c['t_stat']}（要 2 以上）、最大回撤 {c['mdd_pct']}%）")
    return f"{head}，每年輸 0050 {abs(c['excess_cagr_pct'])} 個百分點）"


def line_message(report: dict, state: dict, v32_excess: float | None = None) -> tuple[str | None, dict]:
    """要不要多推一則「策略實驗室」的 LINE、內容是什麼。回傳（訊息或 None, 更新後的通知紀錄）。
    會推的時機：第一次、通過驗證的策略今天收盤後決定換股、每週最後一個交易日（週五）的摘要。
    原本每天的選股訊息完全不受影響——這是另外多的一則。"""
    tracking = report.get("tracking") or {}
    accounts = {a["id"]: a for a in tracking.get("accounts") or []}
    as_of = tracking.get("as_of") or report["period"]["end"]
    passed = [c for c in report["candidates"] if c["verdict"] != "none"]
    told = dict(state.get("told") or {})

    def swap(a: dict) -> dict | None:  # 這個帳戶最近一次換股（還沒執行的優先）
        move = a["next"] or (a.get("changed") if a["days"] else None)
        return move if move and (move["sell"] or move["buy"]) else None

    first = not state.get("intro")  # 第一則是介紹：列出目前持股就好，不當成「換股通知」
    changes = [] if first else [c for c in passed if (a := accounts.get(c["id"])) and swap(a)
                                and told.get(c["id"]) != a["decided"]]
    week = "%d-W%02d" % datetime.fromisoformat(as_of).isocalendar()[:2]
    weekly = datetime.fromisoformat(as_of).weekday() == 4 and state.get("week") != week
    if not (first or changes or weekly):
        return None, state
    b = report["benchmark"]
    ahead = any(accounts[c["id"]]["next"] for c in changes)
    lines = ["🔬 策略實驗室" + ("｜下個交易日開盤換股" if ahead else "｜換股通知" if changes
                            else "｜每週摘要" if weekly and not first else ""),
             f"用當時的名單重驗（樣本外 {report['period']['years']} 年，同期抱 0050 年化 {b['cagr_pct']}%）："]
    lines.extend(_verdict_line(c) for c in report["candidates"])
    if v32_excess is not None:
        lines.append("・V3.2 現行策略（每日選股用的）：沒有足夠證據（每年"
                     + (f"輸 0050 {abs(v32_excess)}" if v32_excess < 0 else f"贏 0050 {v32_excess}") + " 個百分點）")
    if not passed:
        lines.append("→ 目前沒有任何策略有足夠的樣本外證據贏過抱 0050。")
    for c in passed or report["candidates"]:
        a = accounts.get(c["id"])
        if not a:
            continue
        lines.append("")
        lines.append(f"📌 {c['name']}（{a['config']}）" + ("" if passed else "｜未通過驗證，僅觀察"))
        move = swap(a) if c in passed else None
        if move and a["next"]:
            lines.append(f"🔁 下一個交易日開盤換股（{a['decided']} 收盤後決定）")
            lines.append(f"賣出：{_names(move['sell'])}")
            lines.append(f"買進：{_names(move['buy'])}")
            if move.get("keep"):
                lines.append(f"續抱：{_names(move['keep'])}")
        elif move and c in changes:  # 決定那天晚上沒通知到（例如當天資料抓不到），補通知
            lines.append(f"🔁 已照 {a['decided']} 的決定換股：賣出 {_names(move['sell'])}；買進 {_names(move['buy'])}")
        if a["holdings"] and c in passed and not (move and a["next"]):
            lines.append(f"目前持股（{a['decided']} 決定）：{_names(a['holdings'])}")
        if a["days"]:
            lines.append(f"模擬帳戶 {a['since'][5:].replace('-', '/')} 起 {a['return_pct']:+}%（同期 0050 {a['bench_pct']:+}%）")
        elif a["since"] is None:
            lines.append("模擬帳戶從下一個交易日開盤開始記錄")
    lines.append("")
    lines.append("以上是歷史驗證與模擬結果，不是投資建議；細節在網頁「策略實驗室」。")
    for c in passed:  # 這則訊息已經列出它們最新的持股
        if c["id"] in accounts:
            told[c["id"]] = accounts[c["id"]]["decided"]
    new_state = {**state, "intro": True, "told": told}
    if weekly:
        new_state["week"] = week
    return "\n".join(lines), new_state


def notify(report: dict) -> bool:
    """需要的話推一則 LINE（沒設定 LINE 就只印出來）。"""
    from . import webstore
    from .line_push import line_enabled, send_line

    state = webstore.load(TRACK, fresh=True) or {}
    v32 = None
    try:
        old = webstore.load(lab.REPORT, fresh=True) or {}
        v32 = next((c["excess_cagr_pct"] for c in old.get("candidates", []) if c["id"] == "v32"), None)
    except Exception:  # noqa: BLE001
        pass
    text, new_state = line_message(report, state, v32)
    if not text:
        _log("今天沒有需要另外通知的事")
        return False
    _log("LINE 訊息：\n" + text)
    if line_enabled():
        send_line(text)
        webstore.save_quietly(TRACK, new_state)
        return True
    _log("沒有設定 LINE，訊息只印出來、不送")
    return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    from . import webstore

    ap = argparse.ArgumentParser(description="策略實驗室（當時名單版）：資料來自證交所，不花 FinMind 額度")
    ap.add_argument("--if-due", type=int, default=0, help="上次報告在這麼多天內就不重跑")
    ap.add_argument("--years", type=int, default=lab.YEARS)
    ap.add_argument("--notify", action="store_true", help="有換股或每週摘要時多推一則 LINE")
    args = ap.parse_args(argv)
    if args.if_due:
        try:
            old = webstore.load(REPORT, fresh=True) or {}
            age = (datetime.now(lab.TZ) - datetime.fromisoformat(old["generated_at"])).days
            if age < args.if_due:
                _log(f"上次驗證是 {age} 天前（{old['generated_at'][:10]}），還不用重跑")
                return 0
        except Exception:  # noqa: BLE001 沒有舊報告就照跑
            pass
    twse.download(args.years)  # 第一次要抓十年份（行情跟月營收同時抓）；平常只補新的
    report = run(years=args.years)
    _log(f"完成（{report['seconds']} 秒）")
    for line in summary(report):
        _log(line)
    if args.notify:
        try:
            notify(report)
        except Exception as exc:  # noqa: BLE001 通知失敗不影響報告
            _log(f"LINE 通知失敗：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
