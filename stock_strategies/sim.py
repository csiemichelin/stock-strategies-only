"""模擬帳戶（紙上交易）

跟 LINE 每日選股同一套邏輯：
- 訊號：evaluate() + 大盤濾鏡（每日排程另含夜盤濾鏡），只買 BUY
- 進場：訊號隔天「開盤價」買零股，部位 = 總資產 × position_size_pct（預設 20%）
- 出場：碰到訊號給的停損價／停利價就賣；持有滿 hold_days（預設 20 個交易日）收盤賣
  ‧ 開盤就跳空穿過 → 以開盤價成交；盤中碰到 → 以停損／停利價成交
  ‧ 同一天停損、停利都碰到時，保守假設先停損
- 費稅：手續費 0.1425%（零股低消 1 元）；證交稅 股票 0.3%、ETF 0.1%、債券 ETF 免稅

兩種用法：
- daily_update(results)：每日排程（main.py）呼叫，往後逐日紙上交易
- replay(...)：從過去某天開始回放到今天，之後接著每日紙上交易
狀態存在 Google Sheet（SimState / SimTrades / SimEquity 分頁）；沒設 Sheet 時存本機 JSON。
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEE_RATE = 0.001425
MIN_FEE = 1
TZ = timezone(timedelta(hours=8))

# 每日報告附加的模擬摘要（line_brief 會讀）
LAST_SUMMARY: list[str] = []


# ---------------------------------------------------------------- 費用


def tax_rate(stock_id: str) -> float:
    sid = str(stock_id)
    if sid.startswith("00"):
        return 0.0 if sid.endswith("B") else 0.001
    return 0.003


def buy_cost(price: float, shares: int) -> tuple[float, float]:
    amount = price * shares
    fee = max(MIN_FEE, math.floor(amount * FEE_RATE))
    return amount + fee, fee


def sell_proceeds(stock_id: str, price: float, shares: int) -> tuple[float, float]:
    amount = price * shares
    fee = max(MIN_FEE, math.floor(amount * FEE_RATE))
    tax = math.floor(amount * tax_rate(stock_id))
    return amount - fee - tax, fee + tax


# ---------------------------------------------------------------- 狀態


def new_state(capital: float, strategy: dict, start_date: str, mode: str) -> dict:
    now = datetime.now(TZ).isoformat(timespec="seconds")
    return {
        "status": "running",
        "mode": mode,  # forward | replay
        "strategy": {"id": strategy.get("id", "default"), "name": strategy.get("name", "預設策略")},
        "params": {
            "hold_days": int(strategy.get("params", {}).get("hold_days", 20)),
        },
        "initial_capital": float(capital),
        "cash": float(capital),
        "start_date": start_date,
        "last_date": None,
        "created_at": now,
        "stopped_at": None,
        "positions": [],
        "pending": [],
        "trades": [],
        "equity": [],
    }


def total_value(state: dict, prices: dict[str, float] | None = None) -> float:
    value = state["cash"]
    for p in state["positions"]:
        px = (prices or {}).get(p["stock_id"], p.get("last_price", p["entry_price"]))
        value += px * p["shares"]
    return value


# ---------------------------------------------------------------- 引擎


def _fill_pending(state: dict, date: str, bars: dict, events: list) -> None:
    pending, state["pending"] = state["pending"], []
    for order in pending:
        bar = bars.get(order["stock_id"])
        if not bar or not bar.get("open"):
            continue  # 當天沒有成交資料（停牌等）→ 取消
        if any(p["stock_id"] == order["stock_id"] for p in state["positions"]):
            continue
        price = float(bar["open"])
        equity = total_value(state)
        budget = min(state["cash"], equity * order["position_pct"] / 100)
        shares = int(budget // (price * (1 + FEE_RATE)))
        while shares > 0 and buy_cost(price, shares)[0] > state["cash"]:
            shares -= 1
        if shares <= 0:
            events.append({"type": "skip", "stock_id": order["stock_id"], "name": order["name"],
                           "reason": "資金不足 1 股"})
            continue
        cost, fee = buy_cost(price, shares)
        state["cash"] -= cost
        state["positions"].append({
            "stock_id": order["stock_id"], "name": order["name"], "shares": shares,
            "entry_price": price, "entry_date": date, "cost": round(cost, 2),
            "stop_price": order["stop_price"], "target_price": order["target_price"],
            "days_held": 0, "last_price": price, "signal_date": order["signal_date"],
        })
        trade = {"date": date, "side": "BUY", "stock_id": order["stock_id"], "name": order["name"],
                 "shares": shares, "price": price, "amount": round(cost, 2), "fee": fee,
                 "reason": f"{order['signal_date']} BUY 訊號，開盤進場"}
        state["trades"].append(trade)
        events.append(trade)


def _exit_price(pos: dict, bar: dict, hold_days: int) -> tuple[float, str] | None:
    o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
    stop, target = pos["stop_price"], pos["target_price"]
    if stop and o <= stop:
        return o, "停損（開盤跳空）"
    if target and o >= target:
        return o, "停利（開盤跳空）"
    if stop and l <= stop:
        return stop, "停損"
    if target and h >= target:
        return target, "停利"
    if pos["days_held"] >= hold_days:
        return c, f"持有滿 {hold_days} 日"
    return None


def _process_exits(state: dict, date: str, bars: dict, events: list) -> None:
    hold_days = state["params"]["hold_days"]
    keep = []
    for pos in state["positions"]:
        bar = bars.get(pos["stock_id"])
        if not bar or not bar.get("close"):
            keep.append(pos)
            continue
        pos["days_held"] += 1
        hit = _exit_price(pos, bar, hold_days)
        pos["last_price"] = float(bar["close"])
        if not hit:
            keep.append(pos)
            continue
        price, reason = hit
        proceeds, costs = sell_proceeds(pos["stock_id"], price, pos["shares"])
        state["cash"] += proceeds
        pnl = proceeds - pos["cost"]
        trade = {"date": date, "side": "SELL", "stock_id": pos["stock_id"], "name": pos["name"],
                 "shares": pos["shares"], "price": round(price, 2), "amount": round(proceeds, 2), "fee": costs,
                 "pnl": round(pnl, 2), "pnl_pct": round(pnl / pos["cost"] * 100, 2), "reason": reason}
        state["trades"].append(trade)
        events.append(trade)
    state["positions"] = keep


def _mark(state: dict, date: str) -> dict:
    holdings = sum(p["last_price"] * p["shares"] for p in state["positions"])
    point = {"date": date, "cash": round(state["cash"], 2), "holdings": round(holdings, 2),
             "total": round(state["cash"] + holdings, 2)}
    if state["equity"] and state["equity"][-1]["date"] == date:
        state["equity"][-1] = point
    else:
        state["equity"].append(point)
    return point


def _queue_signals(state: dict, date: str, signals: list[dict]) -> None:
    held = {p["stock_id"] for p in state["positions"]}
    queued = {o["stock_id"] for o in state["pending"]}
    buys = [s for s in signals if s.get("action") == "BUY"]
    buys.sort(key=lambda s: -(s.get("signal_score") or 0))
    for s in buys:
        sid = str(s["stock_id"])
        if sid in held or sid in queued:
            continue
        state["pending"].append({
            "stock_id": sid, "name": s.get("name", ""), "signal_date": date,
            "signal_score": s.get("signal_score"), "position_pct": float(s.get("position_size_pct") or 20),
            "ref_price": s.get("entry_price"), "stop_price": s.get("stop_loss_price"),
            "target_price": s.get("target_price"),
        })
        queued.add(sid)


def step(state: dict, date: str, bars: dict, signals: list[dict] | None = None) -> list[dict]:
    """處理一個交易日：開盤成交掛單 → 檢查停損停利／到期 → 收盤估值 → 收盤訊號掛明日單。"""
    events: list[dict] = []
    _fill_pending(state, date, bars, events)
    _process_exits(state, date, bars, events)
    _mark(state, date)
    if signals is not None:
        _queue_signals(state, date, signals)
    state["last_date"] = date
    return events


def stop(state: dict) -> dict:
    """中止模擬：以最後收盤價結算所有持股，取消掛單。"""
    date = state.get("last_date") or datetime.now(TZ).strftime("%Y-%m-%d")
    for pos in state["positions"]:
        price = pos.get("last_price", pos["entry_price"])
        proceeds, costs = sell_proceeds(pos["stock_id"], price, pos["shares"])
        state["cash"] += proceeds
        pnl = proceeds - pos["cost"]
        state["trades"].append({"date": date, "side": "SELL", "stock_id": pos["stock_id"], "name": pos["name"],
                                "shares": pos["shares"], "price": round(price, 2), "amount": round(proceeds, 2),
                                "fee": costs, "pnl": round(pnl, 2), "pnl_pct": round(pnl / pos["cost"] * 100, 2),
                                "reason": "中止模擬，以最後收盤價結算"})
    state["positions"], state["pending"] = [], []
    _mark(state, date)
    state["status"] = "stopped"
    state["stopped_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    return state


def summary(state: dict) -> dict:
    total = total_value(state)
    init = state["initial_capital"]
    sells = [t for t in state["trades"] if t["side"] == "SELL"]
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    peak, mdd = init, 0.0
    for pt in state["equity"]:
        peak = max(peak, pt["total"])
        mdd = min(mdd, (pt["total"] / peak - 1) * 100)
    return {
        "total": round(total, 2), "pnl": round(total - init, 2), "pnl_pct": round((total / init - 1) * 100, 2),
        "cash": round(state["cash"], 2), "holdings": round(total - state["cash"], 2),
        "closed_trades": len(sells), "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "max_drawdown_pct": round(mdd, 2),
    }


def summary_lines(state: dict, events: list[dict]) -> list[str]:
    s = summary(state)
    lines = [f"💼 模擬帳戶（本金 {state['initial_capital']:,.0f}）",
             f"總資產 {s['total']:,.0f}（{s['pnl']:+,.0f}｜{s['pnl_pct']:+.2f}%）"]
    acts = []
    for e in events:
        if e.get("side") == "BUY":
            acts.append(f"買 {e['stock_id']} {e['name']} {e['shares']} 股 @{e['price']:g}")
        elif e.get("side") == "SELL":
            acts.append(f"賣 {e['stock_id']} {e['name']} {e['pnl_pct']:+.1f}%（{e['reason']}）")
    if acts:
        lines.append("今日：" + "；".join(acts))
    if state["positions"]:
        lines.append("持倉：" + "、".join(f"{p['stock_id']} {p['name']}" for p in state["positions"]))
    if state["pending"]:
        lines.append("明天開盤買：" + "、".join(f"{o['stock_id']} {o['name']}" for o in state["pending"]))
    return lines


# ---------------------------------------------------------------- 儲存


def _local_path() -> Path:
    return Path(os.environ.get("SIM_STATE_FILE", Path(__file__).resolve().parent.parent / ".cache" / "sim_state.json"))


def _use_sheet() -> bool:
    return bool(os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_CREDS_JSON"))


_store_lock = threading.Lock()
TRADE_COLS = ["date", "side", "stock_id", "name", "shares", "price", "amount", "fee", "pnl", "pnl_pct", "reason"]
EQUITY_COLS = ["date", "cash", "holdings", "total"]


def _ws(sh, title, rows=1000, cols=12):
    import gspread

    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


_cache: dict = {"state": None, "ts": 0.0}


def _cache_ttl() -> float:
    return float(os.environ.get("SIM_CACHE_TTL", "60"))


def _remember(state: dict | None) -> None:
    import copy
    import time

    _cache.update(state=copy.deepcopy(state), ts=time.monotonic())


def _rows_to_records(values: list[list]) -> list[dict]:
    if not values:
        return []
    header, out = values[0], []
    for row in values[1:]:
        if not any(str(v).strip() for v in row):
            continue
        row = list(row) + [""] * (len(header) - len(row))
        out.append(dict(zip(header, row)))
    return out


def _read_sheet_state(sh) -> dict | None:
    """一次 API 呼叫讀三個分頁（原本 6 次：每個分頁先查 metadata 再讀值）。"""
    try:
        res = sh.values_batch_get(["'SimState'!A2", "'SimTrades'!A1:K", "'SimEquity'!A1:D"])
        core_v, trades_v, equity_v = (r.get("values", []) for r in res["valueRanges"])
        core = core_v[0][0] if core_v and core_v[0] else ""
        trades, equity = _rows_to_records(trades_v), _rows_to_records(equity_v)
    except Exception:  # 分頁還不存在等 → 走原本逐頁讀（會順便建立分頁）
        core = _ws(sh, "SimState", rows=5, cols=2).acell("A2").value
        if not core:
            return None
        trades = _ws(sh, "SimTrades").get_all_records(numericise_ignore=["all"])
        equity = _ws(sh, "SimEquity").get_all_records(numericise_ignore=["all"])
    if not core:
        return None
    state = json.loads(core)
    state["trades"] = [_typed(t, ("shares", "price", "amount", "fee", "pnl", "pnl_pct")) for t in trades]
    state["equity"] = [_typed(e, ("cash", "holdings", "total")) for e in equity]
    return state


def load_state(fresh: bool = False) -> dict | None:
    """讀模擬帳戶。Sheet 模式下同一行程 60 秒內重用記憶體（每日排程在 GitHub Actions 另一個行程寫入，最晚 60 秒後看到）。"""
    import copy
    import time

    with _store_lock:
        if not _use_sheet():
            p = _local_path()
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if not fresh and _cache["ts"] and time.monotonic() - _cache["ts"] < _cache_ttl():
            return copy.deepcopy(_cache["state"])
        from .sheet import get_gsheet

        state = _read_sheet_state(get_gsheet())
        _remember(state)
        return state


def _typed(row: dict, numeric) -> dict:
    out = {}
    for k, v in row.items():
        v = None if v == "" else v
        if k in numeric and v is not None:
            v = float(str(v).replace(",", ""))
            if k == "shares":
                v = int(v)
        out[k] = v
    return out


def save_state(state: dict) -> None:
    with _store_lock:
        if not _use_sheet():
            p = _local_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            return
        from .sheet import get_gsheet

        sh = get_gsheet()
        core = {k: v for k, v in state.items() if k not in ("trades", "equity")}
        ws = _ws(sh, "SimState", rows=5, cols=2)
        ws.update(range_name="A1:A2", values=[["state（勿手動修改）"], [json.dumps(core, ensure_ascii=False)]],
                  value_input_option="RAW")
        for title, cols, rows in (("SimTrades", TRADE_COLS, state["trades"]), ("SimEquity", EQUITY_COLS, state["equity"])):
            w = _ws(sh, title, rows=max(1000, len(rows) + 10), cols=len(cols))
            w.clear()
            data = [cols] + [["" if r.get(c) is None else r.get(c) for c in cols] for r in rows]
            w.update(range_name="A1", values=data, value_input_option="RAW")
        _remember(state)


def clear_state() -> None:
    with _store_lock:
        if not _use_sheet():
            _local_path().unlink(missing_ok=True)
            return
        from .sheet import get_gsheet

        sh = get_gsheet()
        for title in ("SimState", "SimTrades", "SimEquity"):
            _ws(sh, title).clear()
        _remember(None)


# ---------------------------------------------------------------- 每日排程


def _bars_for(stock_ids, after: str | None, until: str) -> dict[str, dict[str, dict]]:
    """{date: {stock_id: bar}}，涵蓋 (after, until]。"""
    from .data import get_price_history

    out: dict[str, dict[str, dict]] = {}
    for sid in stock_ids:
        try:
            px = get_price_history(sid, 1)
        except Exception as exc:
            print(f"[sim] {sid} 價格取得失敗：{exc}", file=sys.stderr)
            continue
        for row in px.to_dict("records"):
            d = str(row["date"])[:10]
            if (after is None or d > after) and d <= until:
                out.setdefault(d, {})[str(sid)] = row
    return out


def advance(state: dict, signals: list[dict] | None, signal_date: str) -> list[dict]:
    """把帳戶推進到 signal_date（含補跑漏掉的交易日），最後一天掛入新訊號。"""
    ids = {p["stock_id"] for p in state["positions"]} | {o["stock_id"] for o in state["pending"]}
    events: list[dict] = []
    by_date = _bars_for(ids, state.get("last_date"), signal_date) if ids else {}
    dates = sorted(by_date)
    if not dates or dates[-1] != signal_date:
        dates.append(signal_date)  # 沒持倉時也要記錄當天
    for d in dates:
        if state.get("last_date") and d <= state["last_date"]:
            continue
        events += step(state, d, by_date.get(d, {}), signals if d == signal_date else None)
    if state.get("last_date") == signal_date and signals is not None and not any(
        o["signal_date"] == signal_date for o in state["pending"]
    ):
        _queue_signals(state, signal_date, signals)
    return events


def daily_update(results: list[dict]) -> list[str]:
    """main.py 每日呼叫。沒有進行中的模擬就什麼都不做。"""
    global LAST_SUMMARY
    LAST_SUMMARY = []
    try:
        state = load_state()
        if not state or state.get("status") != "running":
            return []
        dates = [r.get("date") for r in results if r.get("date")]
        signal_date = _latest_bar_date() or (max(dates) if dates else datetime.now(TZ).strftime("%Y-%m-%d"))
        events = advance(state, results, signal_date)
        save_state(state)
        LAST_SUMMARY = summary_lines(state, events)
        print("[sim] " + " / ".join(LAST_SUMMARY))
        return LAST_SUMMARY
    except Exception as exc:  # 模擬失敗不影響每日選股
        print(f"[sim] 模擬帳戶更新失敗：{exc}", file=sys.stderr)
        return []


def _latest_bar_date() -> str | None:
    try:
        from .datasources import get_index_history

        return str(get_index_history("TAIEX")["date"].iloc[-1])[:10]
    except Exception:
        return None


# ---------------------------------------------------------------- 歷史回放

_ctx = threading.local()


class _StockReplay:
    """一檔股票的回放資料：指標與每個訊號日的回測結果先算好，逐日查表。"""

    def __init__(self, stock_id, name, raw_px, params, years):
        import numpy as np

        from .indicators import add_indicators, tech_score_at

        self.stock_id, self.name, self.params, self.years = stock_id, name, params, years
        self.raw = raw_px.reset_index(drop=True)
        self.ind = add_indicators(self.raw.copy())
        self.dates = [str(d)[:10] for d in self.raw["date"]]
        hold = int(params["hold_days"])
        min_score = int(params["min_tech_score_for_signal"])
        target, stop_ = float(params["target_return"]), float(params["stop_loss"])
        n = len(self.ind)
        self.flag = np.zeros(n, dtype=bool)
        self.outcome = np.full(n, np.nan)
        opens, highs, lows, closes = (self.ind[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
        self.tscore = np.zeros(n)
        for i, row in enumerate(self.ind.to_dict("records")):  # 每天的技術分數先算好（dict 比 iloc 快很多）
            self.tscore[i] = tech_score_at(row, params)["score"]
        for i in range(n - hold - 1):
            if self.tscore[i] < min_score:
                continue
            entry = opens[i + 1]
            if not entry or np.isnan(entry) or entry <= 0 or i + 2 + hold > n:
                continue
            hi, lo = highs[i + 2:i + 2 + hold].max(), lows[i + 2:i + 2 + hold].min()
            if hi >= entry * (1 + target) and not lo <= entry * (1 - stop_):
                r = target
            elif lo <= entry * (1 - stop_):
                r = -stop_
            else:
                r = (closes[i + 1 + hold] - entry) / entry
            self.flag[i], self.outcome[i] = True, r
        self.hold = hold

    def compact(self):
        """pandas 3 的 Copy-on-Write 會在原表記住每個切片的參照，回放一年累積上百 MB；
        定期換成新的表，舊參照就能釋放（Render 只有 512MB）。"""
        self.raw = self.raw.copy()
        self.ind = self.ind.copy()

    def window(self, date):
        import bisect

        end = bisect.bisect_right(self.dates, date)
        start_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=365 * self.years + 60)).strftime("%Y-%m-%d")
        start = bisect.bisect_left(self.dates, start_date)
        return start, end

    def backtest(self, s, L):
        import numpy as np

        lo, hi = s + 60, L - self.hold - 1
        if hi <= lo:
            return {"winrate": None, "samples": 0, "avg_return": None}
        mask = self.flag[lo:hi]
        rets = self.outcome[lo:hi][mask]
        if len(rets) == 0:
            return {"winrate": None, "samples": 0, "avg_return": None}
        return {"winrate": round(float((rets > 0).sum()) / len(rets), 3), "samples": int(len(rets)),
                "avg_return": round(float(np.mean(rets)), 4)}


def _install_replay_hooks():
    """讓 evaluate() 在回放時讀「當天以前」的資料；非回放時完全照原本函式。"""
    from . import evaluate as ev

    if getattr(ev, "_sim_hooked", False):
        return
    orig_px, orig_ind, orig_bt, orig_fund = ev.get_price_history, ev.add_indicators, ev.backtest, ev.get_fundamental

    def fund_hook(stock_id):
        c = getattr(_ctx, "cur", None)
        return orig_fund(stock_id) if c is None else c["fund"]

    def px_hook(stock_id, years=3):
        c = getattr(_ctx, "cur", None)
        if c is None:
            return orig_px(stock_id, years)
        return c["stock"].raw.iloc[c["s"]:c["L"]].reset_index(drop=True)

    def ind_hook(df, *a, **kw):
        c = getattr(_ctx, "cur", None)
        if c is None:
            return orig_ind(df, *a, **kw)
        return c["stock"].ind.iloc[c["s"]:c["L"]].reset_index(drop=True)

    def bt_hook(df, params=None):
        c = getattr(_ctx, "cur", None)
        if c is None:
            return orig_bt(df, params)
        return c["stock"].backtest(c["s"], c["L"])

    ev.get_price_history, ev.add_indicators, ev.backtest, ev.get_fundamental = px_hook, ind_hook, bt_hook, fund_hook
    ev._sim_hooked = True


@contextmanager
def _as_of(stock: _StockReplay, date: str, fund: dict):
    """只影響目前執行緒；基本面用最新年報（回放期間內通常不變）。"""
    s, L = stock.window(date)
    _ctx.cur = {"stock": stock, "s": s, "L": L, "fund": fund}
    try:
        yield
    finally:
        _ctx.cur = None


def _full_history(stock_id: str, years: int):
    """回放要比每日選股多一年的歷史；快取若是每日選股留下的（只有 3 年）就重抓一次。"""
    from .cache import clear_cache
    from .data import get_price_history

    raw = get_price_history(stock_id, years)
    need = datetime.now() - timedelta(days=365 * years + 60 - 20)
    if len(raw) and str(raw["date"].iloc[0])[:10] > need.strftime("%Y-%m-%d"):
        clear_cache("TaiwanStockPrice", stock_id)
        raw = get_price_history(stock_id, years)
    return raw


_MAX_VOLUME_BONUS = 10 + 8 + 5 + 8  # volume.detect_patterns 所有加分項的總和（上限）


def _fund_pass(fund: dict, params: dict) -> bool:
    """與 evaluate() 的基本面判斷相同。"""
    eps, roe = list(fund.get("eps", {}).values()), list(fund.get("roe", {}).values())
    return (len(eps) >= 2 and len(roe) >= 2 and min(eps) > params["eps_threshold"]
            and min(roe) > params["roe_threshold"])


def _could_buy(st: "_StockReplay", i: int, params: dict, fund_ok: bool) -> bool:
    """evaluate() 給 BUY 的必要條件：基本面門檻（若要求）且技術分數（含量價加分上限）夠高。"""
    if params["fundamental_pass_required"] and not fund_ok:
        return False
    bonus = _MAX_VOLUME_BONUS if params["use_volume_patterns"] else 0
    return min(100, st.tscore[i] + bonus) >= params["min_tech_score_for_buy"]


def replay(capital: float, start_date: str, strategy: dict, *, progress=None, cancel=None,
           prune: bool = True) -> dict:
    """從 start_date 回放到最新交易日，回傳 state（status=running，之後接每日排程）。"""
    from .data import get_fundamental, get_price_history
    from .datasources import get_index_history
    from .evaluate import evaluate
    from .loader import merge_params
    from .market import apply_market_filter
    from .sheet import read_watchlist

    report = progress or (lambda **kw: None)
    params = merge_params(strategy)
    years = int(params["backtest_years"])
    _install_replay_hooks()

    watch = read_watchlist()
    report(phase="下載歷史資料", completed=0, total=len(watch), current=None)
    stocks, funds, skipped = [], {}, []
    for i, row in enumerate(watch):
        if cancel and cancel.is_set():
            break
        sid, name = str(row["stock_id"]), row.get("name", "")
        report(completed=i, current=sid)
        try:
            raw = _full_history(sid, years + 1)
            if len(raw) < 100:
                continue
            stocks.append(_StockReplay(sid, name, raw, params, years))
            funds[sid] = get_fundamental(sid)
        except Exception as exc:
            skipped.append({"stock_id": sid, "name": name, "reason": str(exc)[:120]})
            print(f"[replay] {sid} 略過：{exc}", file=sys.stderr)

    quota_skips = [x for x in skipped if "額度" in x["reason"] or "限流" in x["reason"]]
    if quota_skips and len(quota_skips) > 0.1 * max(1, len(watch)):
        raise RuntimeError(f"FinMind 免費額度用完（每小時 600 次），有 {len(quota_skips)} 檔資料抓不到，"
                           "結果會失真，請約一小時後再試，並避免和「開始選股」排在同一小時")
    idx = get_index_history("TAIEX")
    if idx is None or idx.empty or "date" not in idx.columns:
        raise RuntimeError("抓不到加權指數資料（多半是 FinMind 免費額度用完），請約一小時後再試")
    idx = idx.assign(date=idx["date"].astype(str).str[:10]).reset_index(drop=True)
    idx["ma"] = idx["close"].astype(float).rolling(int(params.get("market_filter_ma_period", 20))).mean()
    days = [d for d in idx["date"] if d >= start_date]
    state = new_state(capital, strategy, days[0] if days else start_date, "replay")
    state["skipped"] = skipped
    fund_ok = {st.stock_id: _fund_pass(funds.get(st.stock_id, {}), params) for st in stocks}
    report(phase="逐日回放", completed=0, total=len(days), current=None)
    for n, day in enumerate(days):
        if cancel and cancel.is_set():
            break
        report(completed=n, current=day)
        if n % 10 == 0:
            for st in stocks:
                st.compact()
        signals, bars = [], {}
        for st in stocks:
            s, L = st.window(day)
            if L == 0 or st.dates[L - 1] != day:
                continue
            bars[st.stock_id] = st.raw.iloc[L - 1].to_dict()
            if prune and not _could_buy(st, L - 1, params, fund_ok.get(st.stock_id, False)):
                continue  # 這天不可能出現 BUY：模擬只看 BUY，略過 evaluate 結果完全相同
            with _as_of(st, day, funds.get(st.stock_id, {"eps": {}, "roe": {}})):
                r = evaluate(st.stock_id, st.name, strategy=strategy)
            if r:
                r["date"] = day
                signals.append(r)
        row = idx[idx["date"] == day].iloc[0]
        bullish = bool(row["close"] > row["ma"]) if row["ma"] == row["ma"] else True
        if params.get("market_filter_enabled", True):
            apply_market_filter(signals, {"bullish": bullish})
        step(state, day, bars, signals)
    report(completed=len(days), current=None)
    return state
