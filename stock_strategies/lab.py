"""策略實驗室：用多年的樣本外（walk-forward）結果，檢驗哪個策略「有證據」贏過抱 0050。

為什麼要有這個：網頁上的回測只看最近一年、同一組參數，漂亮的結果常常只是運氣或偷看未來。
這裡的做法：
  1. 資料盡量誠實：股價含息還原、基本面只用當時已公布的年報、月營收在法定公布日之後才看得到、
     手續費與證交稅照實扣、訊號隔天開盤才成交。
  2. 候選策略少而固定（寫死在 CANDIDATES／GRIDS），避免亂試一堆參數後挑最漂亮的。
  3. walk-forward：把評估期切成每 6 個月一段；每一段只用「之前約 3 年」的表現挑參數，
     再看那組參數在這一段（樣本外）的成績，全部串起來才是最後的績效。
  4. 結論規則寫死：樣本外年化報酬贏 0050、過半的區段都贏、月超額報酬 t 值 >= 2 才算「初步有證據」，
     t >= 3 才算「證據強」；否則就是「沒有足夠證據」。

已知還沒解決的偏誤（報告裡會寫出來）：觀察清單是「現在」的名單（倖存者偏誤，結果偏樂觀）；
用開盤價成交、沒算滑價。

用法：python -m stock_strategies.lab [--if-due 25] [--years 10]
每月由 GitHub Actions（.github/workflows/lab.yml）執行，結果存在 webstore 的 lab_report，網頁 /lab 顯示。
"""

from __future__ import annotations

import bisect
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import sim

TZ = timezone(timedelta(hours=8))
BENCH = "0050"
CAPITAL = 1_000_000
YEARS = 10
TRAIN_DAYS = 750      # 挑參數用的訓練期：約 3 年
FOLD_MONTHS = 6       # 每段樣本外 6 個月
SWITCH_COST = 0.006   # 換參數時多扣一次來回成本
WARMUP_DAYS = 260     # 排名型策略開始前的暖身（最長的均線／動能要 250 天）
REPORT = "lab_report"
FUND_HISTORY = "fund_history"

CANDIDATES = [
    {"id": "v32", "name": "V3.2 現行策略", "kind": "fixed",
     "desc": "基本面＋技術＋歷史勝率評分，+10%／−8%、最長抱 20 天，大盤跌破月線不買"},
    {"id": "v32_nobt", "name": "V3.2 拿掉歷史勝率", "kind": "fixed",
     "desc": "同上，但總分不看「歷史勝率」那一項（它是樣本內統計）"},
    {"id": "revenue", "name": "月營收動能", "kind": "grid",
     "desc": "每月營收公布後，買營收成長最強的幾檔，抱到下個月再換"},
    {"id": "trend", "name": "趨勢動能", "kind": "grid",
     "desc": "每月底買過去漲最多、且站上均線的幾檔；大盤跌破半年線就全部空手"},
    {"id": "timing", "name": "0050 均線擇時", "kind": "grid",
     "desc": "0050 站上長均線才持有，跌破就空手（每月底檢查一次）"},
]
GRIDS = {
    "revenue": [("yoy", 5), ("yoy", 10), ("accel", 5), ("accel", 10)],
    "trend": [(60, 5), (60, 10), (120, 5), (120, 10), (250, 5), (250, 10)],
    "timing": [(120,), (200,), (250,)],
}


def _log(msg: str) -> None:
    print(f"[lab] {msg}", flush=True)


# ---------------------------------------------------------------- FinMind 額度

_calls = {"n": 0}


def _quota_left() -> int | None:
    """FinMind 這個小時還剩幾次（官方 user_info，不算額度）；查不到回 None。"""
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        return None
    try:
        import requests

        body = requests.get("https://api.web.finmindtrade.com/v2/user_info", params={"token": token},
                            timeout=10).json()
        return int(body["api_request_limit"]) - int(body["user_count"])
    except Exception:  # noqa: BLE001
        return None


def _wait_next_hour(reason: str) -> None:
    from . import cache

    now = datetime.now(TZ)
    wait = (60 - now.minute) * 60 - now.second + 120
    _log(f"{reason}，等 {wait // 60} 分鐘到下一個小時再繼續")
    time.sleep(wait)
    cache._quota_block_until = 0.0


def _fetch(fn, *args):
    """抓一筆 FinMind 資料；額度快用完就等下一個小時（這個工作不急，寧可等也不要缺資料）。"""
    from .cache import FinMindRateLimitError

    for attempt in range(4):
        if _calls["n"] % 20 == 0:
            left = _quota_left()
            if left is not None and left < 50:
                _wait_next_hour(f"FinMind 額度這個小時只剩 {left} 次")
        _calls["n"] += 1
        try:
            return fn(*args)
        except FinMindRateLimitError:
            if attempt == 3:
                raise
            _wait_next_hour("FinMind 額度用完")
    return None


# ---------------------------------------------------------------- 資料


def adjust(px: pd.DataFrame, events: list[tuple[str, float]]) -> tuple[pd.DataFrame, int]:
    """含息還原股價。events：[(除權息日, 參考價÷前一日收盤)]，除權息日之前的價格都乘上因子。
    單日收盤變動超過 ±30%（一般漲跌停 10%、槓桿 ETF 20%）一定是分割或減資，也當成調整、不算報酬。"""
    px = px.sort_values("date").reset_index(drop=True)
    dates = px["date"].astype(str).str[:10].to_numpy()
    close = px["close"].to_numpy(dtype=float)
    known = dict(events)
    factor = np.ones(len(px))
    for i in range(1, len(px)):
        f = known.get(dates[i])
        if f is None and close[i - 1] > 0 and close[i] > 0 and not 0.7 < close[i] / close[i - 1] < 1.3:
            f = close[i] / close[i - 1]
        if f:
            factor[i] = f
    suffix = np.cumprod(factor[::-1])[::-1]   # suffix[i] = factor[i] × factor[i+1] × …
    cum = np.append(suffix[1:], 1.0)           # 第 i 天要乘上「之後」發生的所有調整
    out = px.copy()
    out["date"] = dates
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].to_numpy(dtype=float) * cum
    return out, int((factor != 1.0).sum())


def _dividend_events(stock_id: str, start: str) -> list[tuple[str, float]]:
    """除權息結果表：每次除權息的（日期, 參考價÷前一日收盤）。"""
    from .cache import fetch_finmind_cached

    df = _fetch(fetch_finmind_cached, "TaiwanStockDividendResult", stock_id, start)
    if df is None or df.empty or not {"date", "before_price", "after_price"} <= set(df.columns):
        return []
    before = pd.to_numeric(df["before_price"], errors="coerce")
    after = pd.to_numeric(df["after_price"], errors="coerce")
    return [(str(d)[:10], float(a / b)) for d, b, a in zip(df["date"], before, after)
            if b == b and a == a and b > 0 and 0.2 < a / b < 1.0]


def annual_history(stock_id: str, start: str = "2012-01-01") -> dict:
    """所有四季齊全年度的 EPS、ROE（年份用字串當 key，方便存成 JSON）。"""
    from .cache import clear_cache, fetch_finmind_cached, read_meta
    from .data import _compute_roe

    for dataset in ("TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet"):
        if str(read_meta(dataset, stock_id).get("since") or start)[:10] > start:
            clear_cache(dataset, stock_id)  # 快取是每日選股留下的（只有近幾年）：重抓完整的
    fs = fetch_finmind_cached("TaiwanStockFinancialStatements", stock_id, start)
    if fs.empty or "type" not in fs.columns:
        return {"eps": {}, "roe": {}}
    fs = fs.copy()
    fs["date"] = pd.to_datetime(fs["date"])
    fs["year"] = fs["date"].dt.year
    fs["value"] = pd.to_numeric(fs["value"], errors="coerce")
    eps = fs[fs["type"] == "EPS"].groupby("year")["value"].agg(["sum", "count"])
    return {"eps": {str(int(y)): round(float(r["sum"]), 2) for y, r in eps.iterrows() if r["count"] >= 4},
            "roe": {str(int(y)): round(float(v), 2) for y, v in _compute_roe(fs, stock_id, start).items()}}


def fund_asof(hist: dict | None, day: str) -> dict:
    """day 當天「已公布」的最近三個完整年度：年報期限是隔年 3/31，保守起見 4/1 起才用前一年。"""
    hist = hist or {}
    last = int(day[:4]) - (1 if day[5:10] >= "04-01" else 2)
    return {k: {y: hist[k][str(y)] for y in range(last - 2, last + 1) if str(y) in hist.get(k, {})}
            for k in ("eps", "roe")}


def _fund_histories(stock_ids: list[str]) -> dict:
    """歷年 EPS／ROE：過去的年度不會變，存在 Google Sheet，只有缺的或該有新年報的才問 FinMind（每檔 2 次）。"""
    from . import webstore

    try:
        store = dict((webstore.load(FUND_HISTORY, fresh=True) or {}).get("stocks") or {})
    except Exception as exc:  # noqa: BLE001
        _log(f"讀不到歷年財報紀錄：{exc}")
        store = {}
    today = datetime.now(TZ).date()
    need = str(today.year - (1 if today.strftime("%m-%d") >= "04-05" else 2))
    fetched = 0
    for sid in stock_ids:
        old = store.get(sid)
        if old:
            age = (today - datetime.strptime(old.get("at") or "2000-01-01", "%Y-%m-%d").date()).days
            if need in old.get("eps", {}) or age < 30:
                continue
        try:
            store[sid] = {**_fetch(annual_history, sid), "at": today.isoformat()}
            fetched += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"{sid} 歷年財報抓不到：{exc}")
    if fetched:
        try:
            webstore.save(FUND_HISTORY, {"stocks": store})
        except Exception as exc:  # noqa: BLE001
            _log(f"歷年財報紀錄寫入失敗：{exc}")
    _log(f"歷年財報：{len(stock_ids)} 檔，這次向 FinMind 抓 {fetched} 檔")
    return store


def _revenue(stock_id: str, start: str) -> dict | None:
    """月營收（公布期限之後才看得到）：年增率，以及近 3 個月平均 ÷ 近 12 個月平均。"""
    from .cache import fetch_finmind_cached
    from .datasources import get_month_revenue

    _fetch(fetch_finmind_cached, "TaiwanStockMonthRevenue", stock_id, start)  # 先確定抓到（下面那個會把額度錯誤吞掉）
    df = get_month_revenue(stock_id, start)
    if df is None or df.empty:
        return None
    rev = pd.to_numeric(df["revenue"], errors="coerce")
    accel = rev.rolling(3).mean() / rev.rolling(12).mean()
    return {"avail": [str(d)[:10] for d in df["avail_date"]], "yoy": [float(v) for v in df["yoy"]],
            "accel": [float(v) for v in accel]}


class Data:
    """實驗室用的資料，股價都是還原後的日線；建好之後所有計算都離線。"""

    def __init__(self, px: dict, index: pd.Series, names: dict, revenue: dict | None = None,
                 funds: dict | None = None):
        self.px = px            # 代號 -> DataFrame(date 字串, open, high, low, close, volume)
        self.index = index      # 加權指數收盤（index 是日期字串）
        self.names = names
        self.days = list(index.index)
        self.revenue = revenue or {}
        self.funds = funds or {}
        self.o = {s: dict(zip(df["date"], df["open"].astype(float))) for s, df in px.items()}
        self.c = {s: dict(zip(df["date"], df["close"].astype(float))) for s, df in px.items()}
        close = pd.DataFrame({s: pd.Series(self.c[s]) for s in px}).reindex(self.days)
        self.close = close.where(close > 0).ffill()  # 沒成交的日子（0 或缺值）沿用前一天的收盤
        self.stocks = [s for s in px if not s.startswith("00")]  # 00 開頭的是 ETF
        self.skipped: list[str] = []
        self.dividend_events = 0


def load_data(years: int = YEARS, watch: list[dict] | None = None) -> Data:
    from .cache import fetch_finmind_cached
    from .data import get_price_history
    from .datasources import get_index_history
    from .sheet import read_watchlist

    watch = watch if watch is not None else read_watchlist()
    names = {str(r["stock_id"]): str(r.get("name", "")) for r in watch}
    names.setdefault(BENCH, "元大台灣50")
    start = (datetime.now() - timedelta(days=365 * years + 60)).strftime("%Y-%m-%d")
    _log(f"讀取資料：{len(names)} 檔、{years} 年（{start} 起）；FinMind 這個小時還剩 {_quota_left()} 次")
    _fetch(fetch_finmind_cached, "TaiwanStockPrice", "TAIEX", "2015-01-01")
    idx = get_index_history("TAIEX")
    if idx is None or idx.empty:
        raise RuntimeError("抓不到加權指數")
    idx = idx[idx["date"] >= pd.to_datetime(start)]
    index = pd.Series(idx["close"].astype(float).to_numpy(), index=idx["date"].astype(str).str[:10])
    px, skipped, events = {}, [], 0
    for n, sid in enumerate(names, 1):
        try:
            raw = _fetch(get_price_history, sid, years)
            if raw is None or len(raw) < 250:
                skipped.append(sid)
                continue
            try:
                ev = _dividend_events(sid, start)
            except Exception as exc:  # noqa: BLE001 除權息抓不到就不還原（報告會註明）
                _log(f"{sid} 除權息資料抓不到：{exc}")
                ev = []
            px[sid], _ = adjust(raw[["date", "open", "high", "low", "close", "volume"]], ev)
            events += len(ev)
        except Exception as exc:  # noqa: BLE001
            skipped.append(sid)
            _log(f"{sid} 略過：{exc}")
        if n % 40 == 0:
            _log(f"股價與除權息 {n}/{len(names)}")
    if BENCH not in px:
        raise RuntimeError("抓不到 0050 的股價，沒辦法比較")
    stocks = [s for s in px if not s.startswith("00")]
    revenue, rev_start = {}, (pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    for sid in stocks:
        try:
            r = _revenue(sid, rev_start)
            if r:
                revenue[sid] = r
        except Exception as exc:  # noqa: BLE001
            _log(f"{sid} 月營收抓不到：{exc}")
    _log(f"股價 {len(px)} 檔（略過 {len(skipped)} 檔）、除權息 {events} 筆、月營收 {len(revenue)}/{len(stocks)} 檔")
    data = Data(px, index, names, revenue, _fund_histories(stocks))
    data.skipped, data.dividend_events = skipped, events
    return data


# ---------------------------------------------------------------- 引擎


def _result(dates: list, equity: list, exposure: list, trades: int, costs: float, capital: float) -> dict:
    eq = pd.Series(equity, index=dates, dtype=float)
    ret = eq.pct_change()
    if len(ret):
        ret.iloc[0] = eq.iloc[0] / capital - 1
    years = max(len(dates) / 252, 1e-9)
    return {"ret": ret, "exposure": pd.Series(exposure, index=dates, dtype=float),
            "trades_per_year": round(trades / years, 1),
            "cost_pct_per_year": round(costs / (float(eq.mean()) or 1.0) / years * 100, 2)}


def simulate(data: Data, plan: dict, start: str, end: str, slots: int, initial: list | None = None,
             capital: float = CAPITAL) -> dict:
    """排名型策略的引擎。plan：{決策日: [目標股票]}，決策日收盤後決定、下一個交易日開盤執行：
    不在名單裡的全部賣掉，新進名單的每檔買「總資產 ÷ slots」；留在名單裡的不動（少一點交易成本）。"""
    cash, pos, last = float(capital), {}, {}
    pending = initial
    dates, equity, exposure, trades, costs = [], [], [], 0, 0.0
    for day in data.days:
        if day < start or day > end:
            continue
        if pending is not None:
            targets, pending = pending, None
            for sid in [s for s in pos if s not in targets]:
                price = data.o[sid].get(day)
                if price is None or not price > 0:
                    continue  # 當天沒交易：留到下次換股再賣
                proceeds, fee = sim.sell_proceeds(sid, price, pos.pop(sid))
                cash += proceeds
                costs += fee
                trades += 1
            total = cash + sum(last.get(s, 0.0) * n for s, n in pos.items())
            for sid in targets:
                price = data.o.get(sid, {}).get(day)
                if sid in pos or price is None or not price > 0:
                    continue
                shares = int(min(cash, total / slots) // (price * (1 + sim.FEE_RATE)))
                while shares > 0 and sim.buy_cost(price, shares)[0] > cash:
                    shares -= 1
                if shares <= 0:
                    continue
                cost, fee = sim.buy_cost(price, shares)
                cash -= cost
                costs += fee
                trades += 1
                pos[sid] = shares
        for sid in pos:
            price = data.c[sid].get(day)
            if price is not None and price > 0:
                last[sid] = price
        held = sum(last.get(s, 0.0) * n for s, n in pos.items())
        dates.append(day)
        equity.append(cash + held)
        exposure.append(held / (cash + held) if cash + held > 0 else 0.0)
        if day in plan:
            pending = plan[day]
    return _result(dates, equity, exposure, trades, costs, capital)


def run_v32(data: Data, strategy: dict, start: str, end: str, capital: float = CAPITAL) -> dict:
    """V3.2 那一套（evaluate 評分＋sim.step 的停利停損規則），但基本面只給「當時已公布」的年報。"""
    from .evaluate import evaluate
    from .loader import merge_params
    from .market import apply_market_filter

    params = merge_params(strategy)
    years = int(params["backtest_years"])
    sim._install_replay_hooks()
    stocks = [sim._StockReplay(sid, data.names.get(sid, ""), df, params, years) for sid, df in data.px.items()]
    ohlc = {st.stock_id: st.raw[["open", "high", "low", "close"]].to_numpy(dtype=float) for st in stocks}
    ma = data.index.rolling(int(params.get("market_filter_ma_period", 20))).mean()
    state = sim.new_state(capital, strategy, start, "replay")
    days = [d for d in data.days if start <= d <= end]
    for n, day in enumerate(days):
        if n % 10 == 0:
            for st in stocks:
                st.compact()
        if n % 250 == 0:
            _log(f"  {strategy.get('name', '')} {day}（{n}/{len(days)}）")
        signals, bars = [], {}
        for st in stocks:
            s, L = st.window(day)
            if L == 0 or st.dates[L - 1] != day:
                continue
            o, h, low, c = ohlc[st.stock_id][L - 1]
            if o > 0 and h > 0 and low > 0 and c > 0:
                bars[st.stock_id] = {"open": o, "high": h, "low": low, "close": c}
            fund = fund_asof(data.funds.get(st.stock_id), day)
            if not sim._could_buy(st, L - 1, params, sim._fund_pass(fund, params)):
                continue
            with sim._as_of(st, day, fund):
                r = evaluate(st.stock_id, st.name, strategy=strategy)
            if r:
                signals.append(r)
        if params.get("market_filter_enabled", True):
            level, avg = data.index.get(day), ma.get(day)
            apply_market_filter(signals, {"bullish": bool(level > avg) if avg == avg else True})
        sim.step(state, day, bars, signals)
    eq = state["equity"]
    return _result([p["date"] for p in eq], [p["total"] for p in eq],
                   [p["holdings"] / p["total"] if p["total"] > 0 else 0.0 for p in eq],
                   sum(1 for t in state["trades"] if t.get("side") == "SELL"),
                   sum(t.get("fee") or 0 for t in state["trades"]), capital)


# ---------------------------------------------------------------- 候選策略（每月換股）


def month_ends(days: list[str]) -> list[str]:
    return [d for i, d in enumerate(days[:-1]) if days[i + 1][:7] != d[:7]]


def mid_months(days: list[str]) -> list[str]:
    """每個月第一個「11 日以後」的交易日（月營收法定 10 日前公布）。"""
    out, seen = [], set()
    for d in days:
        if d[8:10] >= "11" and d[:7] not in seen:
            seen.add(d[:7])
            out.append(d)
    return out


def plan_revenue(data: Data, metric: str, top: int) -> dict:
    plan = {}
    for d in mid_months(data.days):
        fresh = (pd.Timestamp(d) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        scores = {}
        for sid, r in data.revenue.items():
            i = bisect.bisect_right(r["avail"], d) - 1
            if i < 0 or r["avail"][i] < fresh or d not in data.c.get(sid, {}):
                continue
            yoy, accel = r["yoy"][i], r["accel"][i]
            if not yoy > 0:
                continue
            if metric == "yoy":
                scores[sid] = yoy
            elif accel > 1:
                scores[sid] = accel
        plan[d] = [s for s, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:top]]
    return plan


def plan_trend(data: Data, lookback: int, top: int) -> dict:
    close = data.close[data.stocks]
    mom = close / close.shift(lookback) - 1
    ok = (close > close.rolling(lookback, min_periods=int(lookback * 0.8)).mean()) & (mom > 0)
    bull = data.index > data.index.rolling(120).mean()
    plan = {}
    for d in month_ends(data.days):
        if not bool(bull.get(d, False)):
            plan[d] = []
            continue
        row = mom.loc[d][ok.loc[d]].dropna().sort_values(ascending=False)
        plan[d] = [s for s in row.index if d in data.c[s]][:top]
    return plan


def plan_timing(data: Data, window: int) -> dict:
    close = data.close[BENCH]
    ma = close.rolling(window).mean()
    return {d: ([BENCH] if close[d] > ma[d] else []) for d in month_ends(data.days) if ma[d] == ma[d]}


PLANNERS = {"revenue": (plan_revenue, lambda cfg: cfg[1]), "trend": (plan_trend, lambda cfg: cfg[1]),
            "timing": (plan_timing, lambda cfg: 1)}


def _label(cid: str, cfg: tuple) -> str:
    if cid == "revenue":
        return f"{'營收年增率' if cfg[0] == 'yoy' else '近3月÷近12月營收'}前 {cfg[1]} 檔"
    if cid == "trend":
        return f"{cfg[0]} 日動能前 {cfg[1]} 檔"
    return f"{cfg[0]} 日均線"


# ---------------------------------------------------------------- walk-forward


def folds(days: list[str], start: str, months: int = FOLD_MONTHS) -> list[list[str]]:
    """把 start 之後的交易日切成每 months 個月一段；最後不滿 40 天的併到前一段。"""
    def key(d):
        return (int(d[:4]) * 12 + int(d[5:7]) - 1) // months

    out, cur = [], []
    for d in days:
        if d < start:
            continue
        if cur and key(d) != key(cur[0]):
            out.append(cur)
            cur = []
        cur.append(d)
    if cur:
        if out and len(cur) < 40:
            out[-1] = out[-1] + cur
        else:
            out.append(cur)
    return out


def _sharpe(ret: pd.Series) -> float:
    sd = float(ret.std())
    return float(ret.mean()) / sd * math.sqrt(252) if sd > 0 else 0.0


def walk_forward(runs: dict, fold_days: list[list[str]], all_days: list[str],
                 train: int = TRAIN_DAYS) -> tuple[pd.Series, list[str]]:
    """runs：{參數名稱: 每日報酬}。每一段只看「這一段開始之前」train 個交易日的表現挑參數，
    再拿那組參數在這一段的報酬；換參數時多扣一次來回成本。回傳（樣本外每日報酬, 每段用的參數）。"""
    keys = list(runs)
    where = {d: i for i, d in enumerate(all_days)}
    parts, chosen = [], []
    for fd in fold_days:
        i0 = where[fd[0]]
        past = all_days[max(0, i0 - train):i0]
        best = keys[0]
        if len(keys) > 1 and len(past) >= 250:
            best = max(keys, key=lambda k: _sharpe(runs[k].reindex(past).fillna(0.0)))
        part = runs[best].reindex(fd).fillna(0.0).copy()
        if chosen and best != chosen[-1]:
            part.iloc[0] -= SWITCH_COST
        parts.append(part)
        chosen.append(best)
    return pd.concat(parts), chosen


def _stats(ret: pd.Series, bench: pd.Series, fold_days: list[list[str]]) -> dict:
    eq = (1 + ret).cumprod()
    years = max(len(ret) / 252, 1e-9)
    total, b_total = float(eq.iloc[-1]), float((1 + bench).prod())
    cagr = (total ** (1 / years) - 1) * 100 if total > 0 else -100.0
    b_cagr = (b_total ** (1 / years) - 1) * 100 if b_total > 0 else -100.0
    curve = np.append(1.0, eq.to_numpy())
    mdd = float((curve / np.maximum.accumulate(curve) - 1).min() * 100)
    month = [d[:7] for d in ret.index]
    excess = (1 + ret).groupby(month).prod() - (1 + bench).groupby(month).prod()
    sd = float(excess.std(ddof=1)) if len(excess) > 2 else 0.0
    t = float(excess.mean()) / (sd / math.sqrt(len(excess))) if sd > 0 else 0.0
    rows = []
    for fd in fold_days:
        r = float((1 + ret.reindex(fd).fillna(0.0)).prod() - 1)
        b = float((1 + bench.reindex(fd).fillna(0.0)).prod() - 1)
        rows.append({"start": fd[0], "end": fd[-1], "ret_pct": round(r * 100, 2), "bench_pct": round(b * 100, 2),
                     "excess_pct": round((r - b) * 100, 2)})
    return {"total_pct": round((total - 1) * 100, 2), "cagr_pct": round(cagr, 2),
            "excess_cagr_pct": round(cagr - b_cagr, 2), "mdd_pct": round(mdd, 2),
            "vol_pct": round(float(ret.std()) * math.sqrt(252) * 100, 2), "sharpe": round(_sharpe(ret), 2),
            "t_stat": round(t, 2), "win_folds": sum(1 for f in rows if f["excess_pct"] > 0), "folds": rows}


def _verdict(stats: dict) -> str:
    n = len(stats["folds"])
    if stats["excess_cagr_pct"] > 0 and stats["t_stat"] >= 3 and stats["win_folds"] >= 0.6 * n:
        return "strong"
    if stats["excess_cagr_pct"] > 0 and stats["t_stat"] >= 2 and stats["win_folds"] > 0.5 * n:
        return "tentative"
    return "none"


def _curve(ret: pd.Series) -> list:
    pts = [[d, round(float(v), 1)] for d, v in ((1 + ret).cumprod() * 100).items()]
    return pts[::5] + ([pts[-1]] if (len(pts) - 1) % 5 else [])


def _notes(data: Data) -> list[str]:
    notes = [
        "觀察清單用的是「現在」的名單（市值前 100 大等），等於事後挑贏家；真實成績只會比這裡差。",
        "訊號當天收盤後產生、隔天開盤價成交；手續費 0.1425%（未打折）與證交稅都有扣，沒算滑價。",
        "基本面只用當時已公布的年報（每年 4/1 起才用前一年）；月營收在法定公布日（次月 10 日）隔天才使用。",
        (f"股價已還原除權息與分割（共 {data.dividend_events} 筆除權息）。" if data.dividend_events
         else "這次抓不到除權息資料，股價沒有含息還原：0050 與各策略的報酬都少算了配息。"),
    ]
    if data.skipped:
        notes.append(f"有 {len(data.skipped)} 檔資料不足或抓不到，沒有納入：{'、'.join(data.skipped[:10])}")
    return notes


def run(data: Data | None = None, years: int = YEARS, save: bool = True, eval_floor: str = "2020-01-01") -> dict:
    """跑完整個實驗室，回傳報告（save=True 時存到 webstore 給網頁用）。"""
    from . import loader, webstore

    t0 = time.time()
    data = data or load_data(years)
    days = data.days
    eval_start = max(eval_floor, (pd.Timestamp(days[0]) + pd.Timedelta(days=365 * 3 + 120)).strftime("%Y-%m-%d"))
    grid_start = days[min(len(days) - 1, WARMUP_DAYS)]
    fold_days = folds(days, eval_start)
    if len(fold_days) < 2:
        raise RuntimeError("資料太短，切不出兩段以上的樣本外期間")
    oos = [d for fd in fold_days for d in fd]
    end = days[-1]
    b_ret = simulate(data, {}, grid_start, end, 1, initial=[BENCH])["ret"].reindex(oos).fillna(0.0)
    default = loader.get_strategy("default") or {"id": "default", "name": "預設策略", "params": {}}
    out = []
    for cand in CANDIDATES:
        cid = cand["id"]
        _log(f"驗證 {cand['name']}…")
        if cand["kind"] == "fixed":
            strategy = default if cid == "v32" else {
                **default, "name": cand["name"], "params": {**(default.get("params") or {}), "weight_backtest": 0}}
            last = run_v32(data, strategy, eval_start, end)
            ret, exposure, chosen, configs = last["ret"].reindex(oos).fillna(0.0), last["exposure"], [], 1
        else:
            planner, slots = PLANNERS[cid]
            runs = {_label(cid, cfg): simulate(data, planner(data, *cfg), grid_start, end, slots(cfg))
                    for cfg in GRIDS[cid]}
            ret, chosen = walk_forward({k: v["ret"] for k, v in runs.items()}, fold_days, days)
            last, configs = runs[chosen[-1]], len(runs)
            exposure = pd.concat([runs[k]["exposure"].reindex(fd) for k, fd in zip(chosen, fold_days)])
        stats = _stats(ret, b_ret, fold_days)
        for row, name in zip(stats["folds"], chosen):
            row["config"] = name
        out.append({**cand, "configs": configs, "current": chosen[-1] if chosen else None, **stats,
                    "verdict": _verdict(stats),
                    "exposure_pct": round(float(exposure.reindex(oos).fillna(0.0).mean()) * 100, 1),
                    "trades_per_year": last["trades_per_year"], "cost_pct_per_year": last["cost_pct_per_year"],
                    "curve": _curve(ret)})
    b_stats = _stats(b_ret, b_ret, fold_days)
    report = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "period": {"start": oos[0], "end": oos[-1], "days": len(oos), "years": round(len(oos) / 252, 1),
                   "data_start": days[0]},
        "capital": CAPITAL, "fold_months": FOLD_MONTHS, "train_days": TRAIN_DAYS,
        "trials": sum(c["configs"] for c in out),
        "benchmark": {"id": BENCH, "name": "抱 0050（含息）" if data.dividend_events else "抱 0050（未含息）",
                      **{k: b_stats[k] for k in ("total_pct", "cagr_pct", "mdd_pct", "vol_pct", "sharpe")},
                      "folds": b_stats["folds"], "curve": _curve(b_ret)},
        "candidates": out,
        "passed": [c["id"] for c in out if c["verdict"] != "none"],
        "data": {"stocks": len(data.px), "companies": len(data.stocks), "with_revenue": len(data.revenue),
                 "with_fundamentals": sum(1 for s in data.stocks if (data.funds.get(s) or {}).get("eps")),
                 "dividend_events": data.dividend_events, "skipped": data.skipped[:30]},
        "notes": _notes(data),
        "seconds": round(time.time() - t0),
    }
    if save:
        webstore.save(REPORT, report)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    from . import webstore

    ap = argparse.ArgumentParser(description="策略實驗室：walk-forward 樣本外驗證")
    ap.add_argument("--if-due", type=int, default=0, help="上次報告在這麼多天內就不重跑")
    ap.add_argument("--years", type=int, default=YEARS)
    args = ap.parse_args(argv)
    if args.if_due:
        try:
            old = webstore.load(REPORT, fresh=True) or {}
            age = (datetime.now(TZ) - datetime.fromisoformat(old["generated_at"])).days
            if age < args.if_due:
                _log(f"上次驗證是 {age} 天前（{old['generated_at'][:10]}），還不用重跑")
                return 0
        except Exception:  # noqa: BLE001 沒有舊報告就照跑
            pass
    report = run(years=args.years)
    p, b = report["period"], report["benchmark"]
    _log(f"完成（{report['seconds']} 秒）：樣本外 {p['start']}～{p['end']}，{b['name']} 年化 {b['cagr_pct']}%、"
         f"最大回撤 {b['mdd_pct']}%")
    for c in report["candidates"]:
        _log(f"  {c['name']}：年化 {c['cagr_pct']}%（比 0050 {c['excess_cagr_pct']:+}），回撤 {c['mdd_pct']}%，"
             f"贏 {c['win_folds']}/{len(c['folds'])} 段，t={c['t_stat']} → {c['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
