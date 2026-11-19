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


def _local_path(slot: str = "") -> Path:
    base = Path(os.environ.get("SIM_STATE_FILE", Path(__file__).resolve().parent.parent / ".cache" / "sim_state.json"))
    return base if not slot else base.with_name(f"{base.stem}_{slot}{base.suffix}")


SLOT_RE = __import__("re").compile(r"^[a-zA-Z0-9_-]{1,60}$")


def _tabs(slot: str = "") -> tuple[str, str, str]:
    """帳戶分頁：第一個帳戶沿用 SimState/SimTrades/SimEquity，其他帳戶加「_策略代號」。"""
    if slot and not SLOT_RE.match(slot):
        raise ValueError(f"帳戶代號格式錯誤：{slot}")
    sfx = f"_{slot}" if slot else ""
    return f"SimState{sfx}", f"SimTrades{sfx}", f"SimEquity{sfx}"


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


_cache: dict = {"state": None, "ts": 0.0}  # 第一個帳戶（slot ""）
_slot_cache: dict[str, dict] = {}          # 其他帳戶
_slots_cache: dict = {"slots": None, "ts": 0.0}


def _cache_for(slot: str) -> dict:
    return _cache if not slot else _slot_cache.setdefault(slot, {"state": None, "ts": 0.0})


def _cache_ttl() -> float:
    return float(os.environ.get("SIM_CACHE_TTL", "60"))


def _remember(state: dict | None, slot: str = "") -> None:
    import copy
    import time

    _cache_for(slot).update(state=copy.deepcopy(state), ts=time.monotonic())


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


def _read_sheet_state(sh, slot: str = "") -> dict | None:
    """一次 API 呼叫讀三個分頁（原本 6 次：每個分頁先查 metadata 再讀值）。"""
    t_state, t_trades, t_equity = _tabs(slot)
    try:
        res = sh.values_batch_get([f"'{t_state}'!A2", f"'{t_trades}'!A1:K", f"'{t_equity}'!A1:D"])
        core_v, trades_v, equity_v = (r.get("values", []) for r in res["valueRanges"])
        core = core_v[0][0] if core_v and core_v[0] else ""
        trades, equity = _rows_to_records(trades_v), _rows_to_records(equity_v)
    except Exception:  # 分頁還不存在等 → 走原本逐頁讀（會順便建立分頁）
        core = _ws(sh, t_state, rows=5, cols=2).acell("A2").value
        if not core:
            return None
        trades = _ws(sh, t_trades).get_all_records(numericise_ignore=["all"])
        equity = _ws(sh, t_equity).get_all_records(numericise_ignore=["all"])
    if not core:
        return None
    state = json.loads(core)
    state["trades"] = [_typed(t, ("shares", "price", "amount", "fee", "pnl", "pnl_pct")) for t in trades]
    state["equity"] = [_typed(e, ("cash", "holdings", "total")) for e in equity]
    return state


def load_state(fresh: bool = False, slot: str = "") -> dict | None:
    """讀模擬帳戶。Sheet 模式下同一行程 60 秒內重用記憶體（每日排程在 GitHub Actions 另一個行程寫入，最晚 60 秒後看到）。"""
    import copy
    import time

    with _store_lock:
        if not _use_sheet():
            p = _local_path(slot)
            state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        else:
            c = _cache_for(slot)
            if not fresh and c["ts"] and time.monotonic() - c["ts"] < _cache_ttl():
                state = copy.deepcopy(c["state"])
            else:
                from .sheet import get_gsheet

                state = _read_sheet_state(get_gsheet(), slot)
                _remember(state, slot)
        if state is not None:
            state["slot"] = slot
        return state


def list_slots(fresh: bool = False) -> list[str]:
    """有哪些帳戶（不管進行中或已中止）。"""
    import time

    with _store_lock:
        if not _use_sheet():
            base = _local_path()
            out = [""] if base.exists() else []
            out += sorted(p.stem[len(base.stem) + 1:] for p in base.parent.glob(f"{base.stem}_*{base.suffix}"))
            return out
        if not fresh and _slots_cache["slots"] is not None and time.monotonic() - _slots_cache["ts"] < _cache_ttl():
            return list(_slots_cache["slots"])
        from .sheet import get_gsheet

        titles = [w.title for w in get_gsheet().worksheets()]
        out = (["" ] if "SimState" in titles else []) + sorted(t[len("SimState_"):] for t in titles if t.startswith("SimState_"))
        _slots_cache.update(slots=out, ts=time.monotonic())
        return list(out)


def load_all(fresh: bool = False) -> list[dict]:
    """所有帳戶（進行中排前面）。"""
    states = [s for s in (load_state(fresh, slot) for slot in list_slots(fresh)) if s]
    return sorted(states, key=lambda s: (s.get("status") != "running", s.get("created_at") or ""))


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


def save_state(state: dict, slot: str | None = None) -> None:
    slot = state.get("slot", "") if slot is None else slot
    state = {**state, "slot": slot}
    with _store_lock:
        if not _use_sheet():
            p = _local_path(slot)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            return
        from .sheet import get_gsheet

        sh = get_gsheet()
        t_state, t_trades, t_equity = _tabs(slot)
        core = {k: v for k, v in state.items() if k not in ("trades", "equity")}
        ws = _ws(sh, t_state, rows=5, cols=2)
        ws.update(range_name="A1:A2", values=[["state（勿手動修改）"], [json.dumps(core, ensure_ascii=False)]],
                  value_input_option="RAW")
        for title, cols, rows in ((t_trades, TRADE_COLS, state["trades"]), (t_equity, EQUITY_COLS, state["equity"])):
            w = _ws(sh, title, rows=max(1000, len(rows) + 10), cols=len(cols))
            w.clear()
            data = [cols] + [["" if r.get(c) is None else r.get(c) for c in cols] for r in rows]
            w.update(range_name="A1", values=data, value_input_option="RAW")
        _remember(state, slot)
        _slots_cache.update(slots=None, ts=0.0)


def clear_state(slot: str = "") -> None:
    with _store_lock:
        if not _use_sheet():
            _local_path(slot).unlink(missing_ok=True)
            return
        from .sheet import get_gsheet

        sh = get_gsheet()
        if slot:  # 其他帳戶直接刪分頁
            import gspread

            for title in _tabs(slot):
                try:
                    sh.del_worksheet(sh.worksheet(title))
                except gspread.WorksheetNotFound:
                    pass
        else:
            for title in _tabs(""):
                _ws(sh, title).clear()
        _remember(None, slot)
        _slots_cache.update(slots=None, ts=0.0)


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


def daily_update(results: list[dict], evaluate_for=None) -> list[str]:
    """main.py 每日呼叫。每個進行中的帳戶各自推進；不是預設策略的帳戶用 evaluate_for(strategy) 重算訊號
    （同一個行程內股價有快取，幾乎不再用 FinMind 額度）。沒有 evaluate_for 時沿用預設策略的訊號。"""
    global LAST_SUMMARY
    LAST_SUMMARY = []
    try:
        accounts = [s for s in load_all(fresh=True) if s.get("status") == "running"]
    except Exception as exc:  # 模擬失敗不影響每日選股
        print(f"[sim] 模擬帳戶讀取失敗：{exc}", file=sys.stderr)
        return []
    if not accounts:
        return []
    dates = [r.get("date") for r in results if r.get("date")]
    signal_date = _latest_bar_date() or (max(dates) if dates else datetime.now(TZ).strftime("%Y-%m-%d"))
    out: list[str] = []
    for state in accounts:
        try:
            sid = state["strategy"]["id"]
            signals = results
            if sid != "default" and evaluate_for is not None:
                from .loader import get_strategy

                strategy = get_strategy(sid)
                if strategy:
                    signals = evaluate_for(strategy)
            events = advance(state, signals, signal_date)
            save_state(state)
            lines = summary_lines(state, events)
            if len(accounts) > 1:
                lines[0] = lines[0].replace("💼 模擬帳戶", f"💼 模擬帳戶・{state['strategy']['name']}", 1)
            out += lines
            print("[sim] " + " / ".join(lines))
        except Exception as exc:  # noqa: BLE001 一個帳戶失敗不影響其他帳戶
            print(f"[sim] {state.get('strategy', {}).get('name')} 更新失敗：{exc}", file=sys.stderr)
    LAST_SUMMARY = out
    return out


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


def estimate_finmind_calls(years: int, stock_ids: list[str] | None = None) -> dict:
    """回放大概要打幾次 FinMind：看快取裡哪些股票的股價／財報今天還沒抓過、歷史不夠長。
    今天已經用網頁選過股（同一台主機）的話，通常只剩幾次。"""
    import json as _json

    import pandas as pd

    from .cache import _is_fresh, _meta_path
    from .sheet import read_watchlist

    ids = stock_ids if stock_ids is not None else [str(r["stock_id"]) for r in read_watchlist()]
    need = (datetime.now() - timedelta(days=365 * years + 60 - 20)).strftime("%Y-%m-%d")
    price = fin = 0
    for sid in ids:
        mp = _meta_path("TaiwanStockPrice", sid)
        try:
            meta = _json.loads(mp.read_text()) if mp.exists() else {}
        except Exception:  # noqa: BLE001
            meta = {}
        deep_enough = str(meta.get("min_date") or "9999")[:10] <= need
        if not (deep_enough and _is_fresh("TaiwanStockPrice", sid, None)):
            price += 1  # 不夠新就增量補、不夠長就重抓 5 年，都是一次
        for ds in ("TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet"):
            if not _is_fresh(ds, sid, None):
                fin += 1
    index = 0 if _is_fresh("TaiwanStockPrice", "TAIEX", None) else 1
    total = price + fin + index
    return {"total": total, "price": price, "fundamental": fin, "index": index, "stocks": len(ids),
            "checked_at": pd.Timestamp.now(tz="Asia/Taipei").isoformat(timespec="seconds")}


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


QUOTA_WAITER = None  # 網頁回測／回放會設定：額度不夠時等下一小時自動繼續（見 api.cloud.quota_waiter）


def _fetch_with_quota(fn, sid, report, cancel):
    """抓一檔資料：先確認額度；碰到額度用完就等額度恢復再重試（最多重試 3 次）。"""
    from .cache import FinMindRateLimitError

    for attempt in range(4):
        if QUOTA_WAITER is not None:
            QUOTA_WAITER(report, cancel, force=attempt > 0)
        if cancel and cancel.is_set():
            raise RuntimeError("已取消")
        try:
            return fn(sid)
        except FinMindRateLimitError:
            if QUOTA_WAITER is None or attempt == 3:
                raise
    return None


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
            raw = _fetch_with_quota(lambda x: _full_history(x, years + 1), sid, report, cancel)
            if raw is None or len(raw) < 100:
                continue
            stocks.append(_StockReplay(sid, name, raw, params, years))
            funds[sid] = _fetch_with_quota(get_fundamental, sid, report, cancel)
        except Exception as exc:
            skipped.append({"stock_id": sid, "name": name, "reason": str(exc)[:120]})
            print(f"[replay] {sid} 略過：{exc}", file=sys.stderr)

    quota_skips = [x for x in skipped if "額度" in x["reason"] or "限流" in x["reason"]]
    if quota_skips and len(quota_skips) > 0.1 * max(1, len(watch)):
        raise RuntimeError(f"FinMind 免費額度用完（每小時 600 次），有 {len(quota_skips)} 檔資料抓不到，"
                           "結果會失真，請約一小時後再試，並避免和「開始選股」排在同一小時")
    if QUOTA_WAITER is not None:
        QUOTA_WAITER(report, cancel)
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
