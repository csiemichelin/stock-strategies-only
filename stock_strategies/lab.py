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

  5. 名單偏誤的對照組：觀察清單是「現在」的市值前 100 大，等於事後挑贏家，從裡面選股的策略數字會虛胖。
     所以另外算「同一份名單等權重買進持有」當對照：選股型策略要同時贏 0050 和這個對照才算「有跡象」，
     而且在換成「當時的名單」重新驗證之前，不會被標成「有證據」。

已知還沒解決的偏誤（報告裡會寫出來）：名單偏誤只能用上面的對照組估計、沒辦法完全排除；
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
from contextlib import contextmanager
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
    {"id": "v32", "name": "V3.2 現行策略", "kind": "fixed", "universe": True,
     "desc": "基本面＋技術＋歷史勝率評分，+10%／−8%、最長抱 20 天，大盤跌破月線不買"},
    {"id": "v32_nobt", "name": "V3.2 拿掉歷史勝率", "kind": "fixed", "universe": True,
     "desc": "同上，但總分不看「歷史勝率」那一項（它是樣本內統計）"},
    {"id": "revenue", "name": "月營收動能", "kind": "grid", "universe": True,
     "desc": "每月營收公布後，買營收成長最強的幾檔，抱到下個月再換"},
    {"id": "combo", "name": "營收動能＋趨勢", "kind": "grid", "universe": True,
     "desc": "跟月營收動能一樣排名，但只買股價站在 120 日均線之上的"},
    {"id": "trend", "name": "趨勢動能", "kind": "grid", "universe": True,
     "desc": "每月第一個交易日收盤後，買過去漲最多、且站上均線的幾檔；大盤跌破半年線就全部空手"},
    {"id": "timing", "name": "0050 均線擇時", "kind": "grid", "universe": False,
     "desc": "0050 站上長均線才持有，跌破就空手（每月第一個交易日檢查一次）"},
]
GRIDS = {
    "revenue": [("yoy", 5), ("yoy", 10), ("accel", 5), ("accel", 10)],
    "combo": [("yoy", 5), ("yoy", 10), ("accel", 5), ("accel", 10)],
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


def _wait_for_quota(reason: str, need: int = 120) -> None:
    """等 FinMind 額度恢復。額度是「最近一小時」滾動計算、不是整點歸零，所以每 5 分鐘看一次，
    有 need 次可用就繼續；最多等 75 分鐘。"""
    from . import cache

    _log(f"{reason}，暫停等額度恢復（每 5 分鐘檢查一次，最多 75 分鐘）")
    for n in range(15):
        time.sleep(300)
        left = _quota_left()
        if left is None or left >= need:
            _log(f"額度恢復了（剩 {left} 次），等了 {(n + 1) * 5} 分鐘")
            break
    cache._quota_block_until = 0.0


def _fetch(fn, *args):
    """抓一筆 FinMind 資料；額度快用完就等它恢復（這個工作不急，寧可等也不要缺資料）。"""
    from .cache import FinMindRateLimitError

    for attempt in range(4):
        if _calls["n"] % 20 == 0:
            left = _quota_left()
            if left is not None and left < 50:
                _wait_for_quota(f"FinMind 額度只剩 {left} 次")
        _calls["n"] += 1
        try:
            return fn(*args)
        except FinMindRateLimitError:
            if attempt == 3:
                raise
            _wait_for_quota("FinMind 額度用完")
    return None


# ---------------------------------------------------------------- 資料


SPLITS = (1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 1 / 20)  # 常見的分割比例（一股變 2、3、4、5、10、20 股）


def adjust(px: pd.DataFrame, events: list[tuple[str, float]], jump: float = 0.3,
           neutral: set | None = None) -> tuple[pd.DataFrame, int]:
    """含息還原股價。events：[(除權息日, 參考價÷前一日收盤)]，除權息日之前的價格都乘上因子
    （除權息日當天沒成交的，算在之後第一個有成交的日子）。
    單日收盤變動超過 ±jump 一定是分割或減資（一般漲跌停 10%、槓桿 ETF 20%），也當成調整、不算報酬；
    剛好接近 1 股拆成幾股的比例時用整數比例還原，分割前後停牌那幾天的漲跌才不會被吃掉。
    neutral：參考價被調整過、但查不到調整幅度的日子（例如減資恢復買賣），那一天不算報酬。"""
    px = px.sort_values("date").reset_index(drop=True)
    dates = px["date"].astype(str).str[:10].to_numpy()
    close = px["close"].to_numpy(dtype=float)
    factor = np.ones(len(px))
    known = set()
    for day, f in events:
        i = int(np.searchsorted(dates, day))
        if 0 < i < len(px) and f and f > 0:
            factor[i] *= f
            known.add(i)
    for i in range(1, len(px)):
        if i in known or not (close[i - 1] > 0 and close[i] > 0):
            continue
        ratio = close[i] / close[i - 1]
        if not 1 - jump < ratio < 1 + jump:
            split = min(SPLITS, key=lambda v: abs(ratio / v - 1))
            factor[i] = split if abs(ratio / split - 1) < 0.1 else ratio
        elif neutral and dates[i] in neutral:
            factor[i] = ratio
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
        self.ranked: dict = {}  # 當時名單（pit.py 才有）：{月底: [依近期成交金額排序的代號]}
        self.top = 0            # 名單取前幾名

    def eligible(self, day: str) -> set | None:
        """day 當天可以挑的股票：最近一個月底排出來的名單。None 表示不限制（用的是現在的觀察清單）。"""
        if not self.ranked:
            return None
        keys = sorted(self.ranked)
        i = bisect.bisect_right(keys, day) - 1
        return set(self.ranked[keys[i]][:self.top]) if i >= 0 else set()


@contextmanager
def _reuse_recent(hours: float):
    """這段期間內，快取裡「hours 小時內抓過」的資料一律直接用——實驗室看的是好幾年的結果，不需要當天最新的資料。
    同一兩天內重跑（例如修了程式再驗證一次）就完全不必再花 FinMind 額度。"""
    from . import cache

    original = cache._is_fresh

    def fresh(dataset, data_id, fresh_days=None, today=None):
        try:
            at = pd.to_datetime(cache.read_meta(dataset, data_id).get("fetched_at"), utc=True)
            if (pd.Timestamp.now(tz="UTC") - at).total_seconds() < hours * 3600:
                return True
        except Exception:  # noqa: BLE001
            pass
        return original(dataset, data_id, fresh_days, today)

    cache._is_fresh = fresh
    try:
        yield
    finally:
        cache._is_fresh = original


def load_data(years: int = YEARS, watch: list[dict] | None = None, reuse_hours: float = 36) -> Data:
    """抓齊實驗室要用的資料；reuse_hours 小時內抓過的直接用快取。"""
    with _reuse_recent(reuse_hours):
        return _load_data(years, watch)


def _load_data(years: int, watch: list[dict] | None) -> Data:
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


def span_years(dates) -> float:
    """這段期間是幾年——用日曆天算。台股一年大約 243 個交易日，用「交易日 ÷ 252」會把年數算少、年化報酬算高。"""
    if len(dates) < 2:
        return max(len(dates) / 252, 1e-9)
    days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days + 1
    return max(days / 365.25, 1e-9)


def _result(dates: list, equity: list, exposure: list, trades: int, costs: float, capital: float) -> dict:
    eq = pd.Series(equity, index=dates, dtype=float)
    ret = eq.pct_change()
    if len(ret):
        ret.iloc[0] = eq.iloc[0] / capital - 1
    years = span_years(dates)
    return {"ret": ret, "exposure": pd.Series(exposure, index=dates, dtype=float),
            "trades_per_year": round(trades / years, 1),
            "cost_pct_per_year": round(costs / (float(eq.mean()) or 1.0) / years * 100, 2)}


def simulate(data: Data, plan: dict, start: str, end: str, slots: int, initial: list | None = None,
             capital: float = CAPITAL, slip: float = 0.0, sizes: dict | None = None, rebalance: bool = False) -> dict:
    """排名型策略的引擎。plan：{決策日: [目標股票]}，決策日收盤後決定、下一個交易日開盤執行：
    不在名單裡的全部賣掉，新進名單的每檔買「總資產 ÷ slots」；留在名單裡的不動（少一點交易成本）。
    slip：滑價，買的時候比開盤價貴這個比例、賣的時候便宜這個比例。
    sizes：{決策日: 檔數}，那一次換股改用這個檔數（中途換參數時用）。
    rebalance：留在名單裡的也調回「總資產 ÷ slots」（多的賣掉、少的補足）——真正的等權重，對照組用。
    不調的話，檔數一多（例如 100 檔）賣出的錢常常不夠買新進的，久了會變成只抱少數幾檔漲很多的。
    回傳的 positions／cash／pending 是最後一天收盤後的狀態（pending：已決定、下一個交易日開盤要換成的名單）。"""
    cash, pos, last = float(capital), {}, {}
    pending = (initial, slots) if initial is not None else None
    dates, equity, exposure, trades, costs = [], [], [], 0, 0.0
    for day in data.days:
        if day < start or day > end:
            continue
        if pending is not None:
            (targets, n_slots), pending = pending, None
            for sid in [s for s in pos if s not in targets]:
                price = data.o[sid].get(day)
                if price is None or not price > 0:
                    continue  # 當天沒交易：留到下次換股再賣
                proceeds, fee = sim.sell_proceeds(sid, price * (1 - slip), pos.pop(sid))
                cash += proceeds
                costs += fee
                trades += 1
            total = cash + sum(last.get(s, 0.0) * n for s, n in pos.items())
            goal = total / n_slots
            if rebalance:  # 留下來的超過目標就先賣掉多的，錢才夠補不足的和新進的
                for sid in [s for s in pos if s in targets]:
                    price = data.o[sid].get(day)
                    if price is None or not price > 0:
                        continue
                    extra = pos[sid] - int(goal // price)
                    if extra <= 0:
                        continue
                    proceeds, fee = sim.sell_proceeds(sid, price * (1 - slip), extra)
                    pos[sid] -= extra
                    if not pos[sid]:
                        del pos[sid]
                    cash += proceeds
                    costs += fee
                    trades += 1
            for sid in targets:
                price = data.o.get(sid, {}).get(day)
                if price is None or not price > 0 or (sid in pos and not rebalance):
                    continue
                price *= 1 + slip
                have = pos.get(sid, 0)
                shares = int(min(cash, goal - have * price) // (price * (1 + sim.FEE_RATE)))
                while shares > 0 and sim.buy_cost(price, shares)[0] > cash:
                    shares -= 1
                if shares <= 0:
                    continue
                cost, fee = sim.buy_cost(price, shares)
                cash -= cost
                costs += fee
                trades += 1
                pos[sid] = have + shares
        for sid in pos:
            price = data.c[sid].get(day)
            if price is not None and price > 0:
                last[sid] = price
        held = sum(last.get(s, 0.0) * n for s, n in pos.items())
        dates.append(day)
        equity.append(cash + held)
        exposure.append(held / (cash + held) if cash + held > 0 else 0.0)
        if day in plan:
            pending = (plan[day], (sizes or {}).get(day, slots))
    out = _result(dates, equity, exposure, trades, costs, capital)
    out.update(positions=dict(pos), cash=cash, prices={s: last.get(s, 0.0) for s in pos},
               pending=list(pending[0]) if pending is not None else None)
    return out


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


def month_starts(days: list[str]) -> list[str]:
    """每個月第一個交易日。月底那天當下並不知道「這是這個月最後一個交易日」（隔天可能還開盤），
    要等下個月第一個交易日出現才確定——所以每月換股的策略在這一天收盤後決定，實際上才做得到。"""
    return [d for i, d in enumerate(days) if i and days[i - 1][:7] != d[:7]]


def mid_months(days: list[str]) -> list[str]:
    """月營收到齊的那一天：法定期限是每月 10 日、遇假日順延到下一個上班日（當天半夜前都還能申報），
    所以用「10 日以後第一個交易日」的再下一個交易日，收盤後決定。"""
    out, seen = [], set()
    for i, d in enumerate(days[:-1]):
        if d[8:10] >= "10" and d[:7] not in seen:
            seen.add(d[:7])
            if i and days[i + 1][:7] == d[:7]:  # i == 0：資料從月中才開始，看不出 10 日那天是哪一天
                out.append(days[i + 1])
    return out


def plan_revenue(data: Data, metric: str, top: int) -> dict:
    plan = {}
    for d in mid_months(data.days):
        fresh = (pd.Timestamp(d) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        allowed, scores = data.eligible(d), {}
        for sid, r in data.revenue.items():
            if allowed is not None and sid not in allowed:
                continue
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


def plan_combo(data: Data, metric: str, top: int) -> dict:
    """營收動能＋趨勢：營收排名照舊，但只留股價站在 120 日均線之上的（營收好、股價卻在跌的不買）。"""
    close = data.close[data.stocks]
    above = close > close.rolling(120, min_periods=96).mean()
    ranked = plan_revenue(data, metric, len(data.stocks))  # 不截斷的完整排名
    return {d: [s for s in order if s in above.columns and bool(above.at[d, s])][:top] for d, order in ranked.items()}


def plan_trend(data: Data, lookback: int, top: int) -> dict:
    close = data.close[data.stocks]
    mom = close / close.shift(lookback) - 1
    ok = (close > close.rolling(lookback, min_periods=int(lookback * 0.8)).mean()) & (mom > 0)
    bull = data.index > data.index.rolling(120).mean()
    plan = {}
    for d in month_starts(data.days):
        if not bool(bull.get(d, False)):
            plan[d] = []
            continue
        allowed = data.eligible(d)
        row = mom.loc[d][ok.loc[d]].dropna().sort_values(ascending=False)
        plan[d] = [s for s in row.index if d in data.c[s] and (allowed is None or s in allowed)][:top]
    return plan


def plan_timing(data: Data, window: int) -> dict:
    close = data.close[BENCH]
    ma = close.rolling(window).mean()
    return {d: ([BENCH] if close[d] > ma[d] else []) for d in month_starts(data.days) if ma[d] == ma[d]}


def plan_universe(data: Data) -> dict:
    """對照組的名單：每個月初，名單裡當天有交易的個股（新掛牌的從下個月初算進來；用當時名單時跟著名單換）。"""
    plan = {}
    for d in month_starts(data.days):
        allowed = data.eligible(d)
        plan[d] = [s for s in data.stocks if d in data.c[s] and (allowed is None or s in allowed)]
    return plan


def universe_returns(data: Data, start: str, end: str, slip: float = 0.0) -> pd.Series:
    """對照組的每日報酬：每個月初把名單調成等權重（留下來的也調回相同比重），手續費、證交稅照算。
    一檔只佔一點點，所以本金放大 100 倍，免得零股進位讓它吃虧。"""
    plan = plan_universe(data)
    allowed = data.eligible(start)
    first = [s for s in data.stocks if start in data.c[s] and (allowed is None or s in allowed)]
    sizes = {d: max(1, len(names)) for d, names in plan.items()}
    return simulate(data, plan, start, end, max(1, len(first)), initial=first, capital=CAPITAL * 100, slip=slip,
                    sizes=sizes, rebalance=True)["ret"]


PLANNERS = {"revenue": (plan_revenue, lambda cfg: cfg[1]), "combo": (plan_combo, lambda cfg: cfg[1]),
            "trend": (plan_trend, lambda cfg: cfg[1]),
            "timing": (plan_timing, lambda cfg: 1)}


def _label(cid: str, cfg: tuple) -> str:
    if cid in ("revenue", "combo"):
        return (f"{'營收年增率' if cfg[0] == 'yoy' else '近3月÷近12月營收'}前 {cfg[1]} 檔"
                + ("＋站上 120 日線" if cid == "combo" else ""))
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
    years = span_years(list(ret.index))
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


def _rule(excess: float, t: float, wins: int, n: int) -> str:
    if excess > 0 and t >= 3 and wins >= 0.6 * n:
        return "strong"
    if excess > 0 and t >= 2 and wins > 0.5 * n:
        return "tentative"
    return "none"


def _verdict(stats: dict, vs_universe: dict | None = None) -> str:
    """strong／tentative：有（強／初步）證據贏 0050。從「事後名單」選股的策略另有 vs_universe（對等權重持有同一份名單），
    兩邊都過也只給 promising（有跡象，但名單偏誤還沒排除），不算證據。"""
    n = len(stats["folds"])
    base = _rule(stats["excess_cagr_pct"], stats["t_stat"], stats["win_folds"], n)
    if vs_universe is None:
        return base
    control = _rule(vs_universe["excess_cagr_pct"], vs_universe["t_stat"], vs_universe["win_folds"], n)
    return "promising" if base != "none" and control != "none" else "none"


def _curve(ret: pd.Series) -> list:
    pts = [[d, round(float(v), 1)] for d, v in ((1 + ret).cumprod() * 100).items()]
    return pts[::5] + ([pts[-1]] if (len(pts) - 1) % 5 else [])


def _notes(data: Data, bench_cagr: float, universe_cagr: float) -> list[str]:
    gap = round(universe_cagr - bench_cagr, 2)
    notes = [
        "觀察清單用的是「現在」的名單（市值前 100 大等），等於事後挑贏家：每個月初把這份名單的個股調成等權重，"
        f"年化 {universe_cagr}%（同期 0050 是 {bench_cagr}%）"
        + (f"，多出來的 {gap} 個百分點是事後挑名單的虛胖，不是選股能力。" if gap > 0 else "。")
        + "所以選股型策略要同時贏過 0050 和這個對照才算「有跡象」，而且要換成當時的名單重驗之後才能算證據。",
        "訊號當天收盤後產生、隔天開盤價成交；手續費 0.1425%（未打折）與證交稅都有扣，沒算滑價。",
        "基本面只用當時已公布的年報（每年 4/1 起才用前一年）；月營收等法定期限（次月 10 日，遇假日順延）過後的下一個交易日才使用。",
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
    u_ret = universe_returns(data, grid_start, end).reindex(oos).fillna(0.0)
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
        versus = None
        if cand["universe"]:  # 從事後名單選股的：另外跟「等權重持有同一份名單」比
            control = _stats(ret, u_ret, fold_days)
            versus = {k: control[k] for k in ("excess_cagr_pct", "t_stat", "win_folds")}
        out.append({**cand, "configs": configs, "current": chosen[-1] if chosen else None, **stats,
                    "vs_universe": versus, "verdict": _verdict(stats, versus),
                    "exposure_pct": round(float(exposure.reindex(oos).fillna(0.0).mean()) * 100, 1),
                    "trades_per_year": last["trades_per_year"], "cost_pct_per_year": last["cost_pct_per_year"],
                    "curve": _curve(ret)})
    b_stats = _stats(b_ret, b_ret, fold_days)
    u_stats = _stats(u_ret, b_ret, fold_days)
    report = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "period": {"start": oos[0], "end": oos[-1], "days": len(oos), "years": round(span_years(oos), 1),
                   "data_start": days[0]},
        "capital": CAPITAL, "fold_months": FOLD_MONTHS, "train_days": TRAIN_DAYS,
        "trials": sum(c["configs"] for c in out),
        "benchmark": {"id": BENCH, "name": "抱 0050（含息）" if data.dividend_events else "抱 0050（未含息）",
                      **{k: b_stats[k] for k in ("total_pct", "cagr_pct", "mdd_pct", "vol_pct", "sharpe")},
                      "folds": b_stats["folds"], "curve": _curve(b_ret)},
        "universe": {"id": "universe", "name": "名單等權重持有（事後名單）", "stocks": len(data.stocks),
                     **{k: u_stats[k] for k in ("total_pct", "cagr_pct", "excess_cagr_pct", "mdd_pct", "vol_pct",
                                                "sharpe")},
                     "folds": u_stats["folds"], "curve": _curve(u_ret)},
        "candidates": out,
        "passed": [c["id"] for c in out if c["verdict"] in ("strong", "tentative")],
        "promising": [c["id"] for c in out if c["verdict"] == "promising"],
        "data": {"stocks": len(data.px), "companies": len(data.stocks), "with_revenue": len(data.revenue),
                 "with_fundamentals": sum(1 for s in data.stocks if (data.funds.get(s) or {}).get("eps")),
                 "dividend_events": data.dividend_events, "skipped": data.skipped[:30]},
        "notes": _notes(data, b_stats["cagr_pct"], u_stats["cagr_pct"]),
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
    u = report["universe"]
    _log(f"  對照｜{u['name']}（{u['stocks']} 檔）：年化 {u['cagr_pct']}%（比 0050 {u['excess_cagr_pct']:+}），"
         f"回撤 {u['mdd_pct']}%")
    for c in report["candidates"]:
        v = c["vs_universe"]
        _log(f"  {c['name']}：年化 {c['cagr_pct']}%（比 0050 {c['excess_cagr_pct']:+}），回撤 {c['mdd_pct']}%，"
             f"贏 {c['win_folds']}/{len(c['folds'])} 段，t={c['t_stat']}"
             + (f"；比名單對照 {v['excess_cagr_pct']:+}、贏 {v['win_folds']} 段、t={v['t_stat']}" if v else "")
             + f" → {c['verdict']}｜持股 {c['exposure_pct']}%、一年 {c['trades_per_year']} 筆、成本 {c['cost_pct_per_year']}%"
             + (f"｜目前參數 {c['current']}" if c["current"] else ""))
        _log("    各段超額：" + " ".join(f"{f['excess_pct']:+.0f}" for f in c["folds"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
