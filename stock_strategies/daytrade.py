"""動能延續極短線（現股當沖）策略：純邏輯，不碰網路與券商，方便測試。

四個階段：
1. 盤後選股 screen()：量 ≥ 1 萬張、成交金額 ≥ 5 億、漲 3.5%–6.5%、收盤 ≥ 當日均價、
   量 ≥ 5 日均量 2 倍、隔日沖券商買超占比 < 10%，取前 5 檔
2. 盤前體檢 macro_check() / preopen_check()：夜盤、台積電 ADR、那斯達克；試撮跳空 +0.5%–2.5%、
   試撮量 ≥ 昨量 3%
3. 盤中進場 IntradayEngine：09:00–09:03 只記錄開盤區間；09:03–09:15 擇一進場
   A. 帶量突破開盤 3 分鐘高點且五檔委買 > 委賣  B. 回測分時均價不破、量縮後大單敲進
4. 風控：-0.8% 或跌破均價超過 60 秒停損；+1.0% 停利；進場 15 分鐘仍膠著就出場；13:20 強制平倉
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time

DEFAULT_PARAMS = {
    # 盤後選股
    "min_volume_lots": 10_000,
    "min_amount": 5e8,
    "min_change_pct": 3.5,
    "max_change_pct": 6.5,
    "volume_ratio_vs_avg5": 2.0,
    "max_daytrader_buy_share": 0.10,
    "max_candidates": 5,
    "daytrader_brokers": ["凱基台北", "富邦建國"],
    # 盤前
    "max_night_drop_pct": -1.0,
    "max_adr_drop_pct": -2.0,
    "max_nasdaq_drop_pct": -2.0,
    "min_gap_pct": 0.5,
    "max_gap_pct": 2.5,
    "min_preopen_volume_ratio": 0.03,
    # 盤中
    "opening_range_end": "09:03",
    "entry_end": "09:15",
    "force_close": "13:20",
    "breakout_volume_mult": 2.0,
    "vwap_touch_pct": 0.3,
    "vwap_break_tolerance_pct": 0.1,
    "big_order_lots": 20,
    "big_order_mult": 3.0,
    "stop_loss_pct": 0.8,
    "take_profit_pct": 1.0,
    "vwap_break_seconds": 60,
    "time_stop_minutes": 15,
    # 資金與風控
    "capital_per_trade": 100_000,
    "max_positions": 2,
    "max_trades_per_day": 3,
    "max_daily_loss": 1_500,
    "fee_rate": 0.001425,
    "fee_discount": 1.0,
    "daytrade_tax_rate": 0.0015,
}


def params_with(overrides: dict | None = None) -> dict:
    p = dict(DEFAULT_PARAMS)
    p.update({k: v for k, v in (overrides or {}).items() if k in DEFAULT_PARAMS})
    return p


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# ---------------------------------------------------------------- 價格跳動、成本


def tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_tick(price: float, up: bool) -> float:
    t = tick_size(price)
    n = price / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * t, 2)


def round_trip_cost(buy: float, sell: float, shares: int, p: dict) -> float:
    fee = lambda amt: max(20, math.floor(amt * p["fee_rate"] * p["fee_discount"]))  # noqa: E731 整股低消 20 元
    return fee(buy * shares) + fee(sell * shares) + math.floor(sell * shares * p["daytrade_tax_rate"])


def breakeven_win_rate(p: dict) -> float:
    """停利、停損固定時，打平所需勝率（含費稅）。"""
    cost = 2 * p["fee_rate"] * p["fee_discount"] + p["daytrade_tax_rate"]
    win = p["take_profit_pct"] / 100 - cost
    loss = p["stop_loss_pct"] / 100 + cost
    return loss / (win + loss) if win > 0 else 1.0


# ---------------------------------------------------------------- 1. 盤後選股


def daytrader_share(broker_rows: list[dict], volume_lots: float, brokers: list[str]) -> float | None:
    """隔日沖券商（名單比對分點名稱）合計買超張數 ÷ 當日成交量。沒資料回 None。"""
    if broker_rows is None or not volume_lots:
        return None
    net = sum(max(0.0, float(r.get("net_lots") or 0)) for r in broker_rows
              if any(b in str(r.get("broker", "")) for b in brokers))
    return net / volume_lots


def screen(rows: list[dict], avg5: dict[str, float], brokers: dict[str, list[dict]] | None,
           p: dict) -> tuple[list[dict], dict]:
    """rows：當日全市場 {stock_id,name,close,prev_close,volume_lots,amount,vwap}；
    avg5：前 5 日平均成交張數；brokers：{stock_id: [{broker, net_lots}]}（None＝沒有分點資料）。
    回傳（入選名單, 各關卡淘汰數）。"""
    stats = {"total": len(rows), "liquidity": 0, "change": 0, "vwap": 0, "volume_burst": 0, "daytrader": 0}
    picked = []
    for r in rows:
        vol, amt = r.get("volume_lots") or 0, r.get("amount") or 0
        if vol < p["min_volume_lots"] or amt < p["min_amount"]:
            stats["liquidity"] += 1
            continue
        prev = r.get("prev_close") or 0
        chg = (r["close"] / prev - 1) * 100 if prev else 0
        if not (p["min_change_pct"] <= chg <= p["max_change_pct"]):
            stats["change"] += 1
            continue
        vwap = r.get("vwap") or (amt / (vol * 1000) if vol else 0)
        if not vwap or r["close"] < vwap:
            stats["vwap"] += 1
            continue
        a5 = avg5.get(r["stock_id"])
        ratio = vol / a5 if a5 else None
        if ratio is None or ratio < p["volume_ratio_vs_avg5"]:
            stats["volume_burst"] += 1
            continue
        share = None
        if brokers is not None:
            share = daytrader_share(brokers.get(r["stock_id"], []), vol, p["daytrader_brokers"])
            if share is not None and share >= p["max_daytrader_buy_share"]:
                stats["daytrader"] += 1
                continue
        item = {**r, "change_pct": round(chg, 2), "vwap": round(vwap, 2), "volume_ratio": round(ratio, 2),
                "daytrader_share": None if share is None else round(share, 4)}
        item["score"], item["score_parts"] = score(item)
        picked.append(item)
    picked.sort(key=lambda x: (-x["score"], -x["amount"]))
    return picked[: p["max_candidates"]], stats


SCORE_GOOD = 60  # 這個分數以上算「推薦」，收盤後檢討命中率


def score(c: dict) -> tuple[int, dict]:
    """隔天動能延續的把握度 0–100：量能、收在高點、站穩均價、漲幅甜蜜點、流動性、隔日沖賣壓。"""
    parts = {}
    parts["量能"] = 25 * min(max((c.get("volume_ratio") or 0) - 1, 0) / 3, 1)            # 2 倍 8 分、4 倍以上 25 分
    hi, lo, close = c.get("high") or 0, c.get("low") or 0, c["close"]
    parts["收高"] = 20 * ((close - lo) / (hi - lo) if hi > lo else 1.0)                    # 收在當天最高 → 20
    vwap = c.get("vwap") or close
    parts["強於均價"] = 15 * min(max(close / vwap - 1, 0) / 0.02, 1)                      # 收盤高於均價 2% 以上 → 15
    parts["漲幅"] = 15 * max(0.0, 1 - abs((c.get("change_pct") or 0) - 5) / 1.5)          # 5% 最好，3.5/6.5 → 0
    amt = c.get("amount") or 0
    parts["流動性"] = 10 * min(max(math.log10(max(amt, 1) / 5e8), 0), 1)                   # 5 億 0 分、50 億 10 分
    share = c.get("daytrader_share")
    parts["隔日沖少"] = 7.5 if share is None else 15 * max(0.0, 1 - share / 0.10)
    parts = {k: round(v, 1) for k, v in parts.items()}
    return int(round(sum(parts.values()))), parts


# ---------------------------------------------------------------- 5. 收盤後檢討


def review_outcome(bars: list[dict], prev_close: float, p: dict) -> dict:
    """用隔天 3 分 K（開始時間標示）檢討：若在 09:03（開盤區間結束）進場，先碰停利還是先碰停損。
    同一根 K 棒兩個都碰到 → 保守算停損；都沒碰到 → 13:20 前最後一根收盤結算。"""
    bars = [b for b in sorted(bars, key=lambda b: b["time"]) if b["time"] < p["force_close"]]
    if not bars:
        return {"outcome": "no_data"}
    entry_bar = next((i for i, b in enumerate(bars) if b["time"] >= "09:00"), 0)
    entry = bars[entry_bar]["close"]
    tp, sl = entry * (1 + p["take_profit_pct"] / 100), entry * (1 - p["stop_loss_pct"] / 100)
    rest = bars[entry_bar + 1:]
    outcome, exit_price, exit_time = "flat", (rest[-1]["close"] if rest else entry), (rest[-1]["time"] if rest else bars[entry_bar]["time"])
    for b in rest:
        hit_tp, hit_sl = b["high"] >= tp, b["low"] <= sl
        if hit_sl:
            outcome, exit_price, exit_time = "loss", sl, b["time"]
            break
        if hit_tp:
            outcome, exit_price, exit_time = "win", tp, b["time"]
            break
    high = max([b["high"] for b in rest] or [entry])
    low = min([b["low"] for b in rest] or [entry])
    cost_pct = (2 * p["fee_rate"] * p["fee_discount"] + p["daytrade_tax_rate"]) * 100
    return {"outcome": outcome, "entry": round(entry, 2), "exit": round(exit_price, 2), "exit_time": exit_time,
            "ret_pct": round((exit_price / entry - 1) * 100 - cost_pct, 2),
            "gap_pct": round((bars[0]["open"] / prev_close - 1) * 100, 2) if prev_close else None,
            "max_up_pct": round((high / entry - 1) * 100, 2), "max_down_pct": round((low / entry - 1) * 100, 2)}


# ---------------------------------------------------------------- 2. 盤前


def macro_check(night_pct: float | None, adr_pct: float | None, nasdaq_pct: float | None, p: dict) -> tuple[bool, str]:
    if night_pct is not None and night_pct < p["max_night_drop_pct"]:
        return False, f"夜盤 {night_pct:+.2f}% 跌超過 {abs(p['max_night_drop_pct'])}%，今天全部取消"
    if adr_pct is not None and adr_pct <= p["max_adr_drop_pct"]:
        return False, f"台積電 ADR {adr_pct:+.2f}% 重挫，今天全部取消"
    if nasdaq_pct is not None and nasdaq_pct <= p["max_nasdaq_drop_pct"]:
        return False, f"那斯達克 {nasdaq_pct:+.2f}% 重挫，今天全部取消"
    parts = [f"{n} {v:+.2f}%" for n, v in (("夜盤", night_pct), ("台積電ADR", adr_pct), ("那斯達克", nasdaq_pct))
             if v is not None]
    return True, "總經濾網通過" + (f"（{'、'.join(parts)}）" if parts else "（資料暫缺，未過濾）")


def preopen_check(ref_price: float, est_price: float | None, est_volume_lots: float | None,
                  prev_volume_lots: float, p: dict) -> tuple[bool, str, float | None]:
    if not est_price or not ref_price:
        return False, "沒有試撮價", None
    gap = (est_price / ref_price - 1) * 100
    if gap < p["min_gap_pct"]:
        return False, f"試撮 {gap:+.2f}% 氣勢不足（< {p['min_gap_pct']}%）", gap
    if gap > p["max_gap_pct"]:
        return False, f"試撮 {gap:+.2f}% 開太高（> {p['max_gap_pct']}%），不追", gap
    ratio = (est_volume_lots or 0) / prev_volume_lots if prev_volume_lots else 0
    if ratio < p["min_preopen_volume_ratio"]:
        return False, f"試撮量只有昨量 {ratio * 100:.1f}%（< {p['min_preopen_volume_ratio'] * 100:.0f}%）", gap
    return True, f"試撮 {gap:+.2f}%、量 {ratio * 100:.1f}%，可做", gap


# ---------------------------------------------------------------- 3、4. 盤中


@dataclass
class Tick:
    ts: datetime
    price: float
    volume: float          # 這筆成交張數
    bid_sum: float = 0.0   # 五檔委買張數合計（最近一次）
    ask_sum: float = 0.0
    at_ask: bool = False   # 外盤成交（敲進）


@dataclass
class Signal:
    kind: str   # entry | exit
    symbol: str
    price: float
    reason: str
    mode: str = ""


@dataclass
class Position:
    entry: float
    entry_ts: datetime
    shares: int
    take_profit: float
    stop: float
    high: float = 0.0
    low: float = 0.0
    below_vwap_since: datetime | None = None


@dataclass
class SymbolState:
    symbol: str
    cum_amount: float = 0.0
    cum_volume: float = 0.0
    or_high: float = 0.0
    or_low: float = math.inf
    or_done: bool = False
    tick_count: int = 0
    vols: list = field(default_factory=list)        # (ts, volume)
    touched_vwap: bool = False
    pullback_low: float = math.inf
    entered: bool = False
    position: Position | None = None

    @property
    def vwap(self) -> float:
        return self.cum_amount / self.cum_volume if self.cum_volume else 0.0

    def avg_tick_volume(self) -> float:
        return self.cum_volume / self.tick_count if self.tick_count else 0.0

    def volume_between(self, start: datetime, end: datetime) -> float:
        return sum(v for t, v in self.vols if start <= t < end)


class IntradayEngine:
    """逐筆餵入成交，吐出進出場訊號。不下單，下單由呼叫端（本機程式／回測）處理。"""

    def __init__(self, symbols: list[str], p: dict):
        self.p = p
        self.states = {s: SymbolState(s) for s in symbols}
        self.or_end, self.entry_end = _hm(p["opening_range_end"]), _hm(p["entry_end"])
        self.force_close = _hm(p["force_close"])
        self.trades_today = 0
        self.realized = 0.0
        self.halted = ""

    def open_positions(self) -> int:
        return sum(1 for s in self.states.values() if s.position)

    def on_tick(self, symbol: str, t: Tick) -> list[Signal]:
        st = self.states.get(symbol)
        if st is None or t.price <= 0:
            return []
        now = t.ts.time()
        if t.volume > 0:
            st.cum_amount += t.price * t.volume
            st.cum_volume += t.volume
            st.tick_count += 1
            st.vols.append((t.ts, t.volume))
        out: list[Signal] = []
        if st.position:
            out += self._manage(st, t)
            return out
        if now < self.or_end:
            st.or_high, st.or_low = max(st.or_high, t.price), min(st.or_low, t.price)
            return out
        st.or_done = True
        if st.entered or now >= self.entry_end or self.halted:
            return out
        if self.open_positions() >= self.p["max_positions"] or self.trades_today >= self.p["max_trades_per_day"]:
            return out
        sig = self._entry_signal(st, t)
        if sig:
            st.entered = True
            self.trades_today += 1
            out.append(sig)
        return out

    def _entry_signal(self, st: SymbolState, t: Tick) -> Signal | None:
        p, vwap = self.p, st.vwap
        avg = st.avg_tick_volume()
        # 模式 A：帶量突破開盤區間高點，且五檔委買 > 委賣
        if st.or_high and t.price > st.or_high and t.volume >= p["breakout_volume_mult"] * max(avg, 1e-9) \
                and t.bid_sum > t.ask_sum:
            return Signal("entry", st.symbol, t.price, f"突破開盤 3 分鐘高點 {st.or_high:g}（量 {t.volume:g} 張）", "A")
        # 模式 B：回測分時均價線不破 → 量縮 → 大單敲進
        if vwap:
            near = t.price <= vwap * (1 + p["vwap_touch_pct"] / 100)
            broke = t.price < vwap * (1 - p["vwap_break_tolerance_pct"] / 100)
            if broke:
                st.touched_vwap, st.pullback_low = False, math.inf
            elif near:
                st.touched_vwap = True
                st.pullback_low = min(st.pullback_low, t.price)
            if st.touched_vwap and not broke:
                minute_ago = t.ts.replace(microsecond=0)
                from datetime import timedelta

                recent = st.volume_between(minute_ago - timedelta(seconds=60), minute_ago)
                open_ts = t.ts.replace(hour=9, minute=0, second=0, microsecond=0)
                minutes = max(1.0, (t.ts - open_ts).total_seconds() / 60)
                shrinking = recent < 0.6 * (st.cum_volume / minutes)
                big = t.at_ask and t.volume >= max(p["big_order_lots"], p["big_order_mult"] * max(avg, 1e-9))
                if big and (shrinking or recent == 0) and t.price >= vwap:
                    return Signal("entry", st.symbol, t.price,
                                  f"回測均價 {vwap:.2f} 不破後大單 {t.volume:g} 張敲進", "B")
        return None

    def open_position(self, symbol: str, price: float, shares: int, ts: datetime) -> Position:
        p = self.p
        pos = Position(entry=price, entry_ts=ts, shares=shares,
                       take_profit=round_tick(price * (1 + p["take_profit_pct"] / 100), up=True),
                       stop=price * (1 - p["stop_loss_pct"] / 100), high=price, low=price)
        self.states[symbol].position = pos
        return pos

    def close_position(self, symbol: str, price: float) -> float:
        st = self.states[symbol]
        pos = st.position
        if not pos:
            return 0.0
        pnl = (price - pos.entry) * pos.shares - round_trip_cost(pos.entry, price, pos.shares, self.p)
        self.realized += pnl
        st.position = None
        if self.realized <= -self.p["max_daily_loss"]:
            self.halted = f"今日虧損 {self.realized:,.0f} 元達上限，停止進場"
        return pnl

    def _manage(self, st: SymbolState, t: Tick) -> list[Signal]:
        from datetime import timedelta

        p, pos, now = self.p, st.position, t.ts
        pos.high, pos.low = max(pos.high, t.price), min(pos.low, t.price)
        if now.time() >= self.force_close:
            return [Signal("exit", st.symbol, t.price, "13:20 強制平倉")]
        if t.price >= pos.take_profit:
            return [Signal("exit", st.symbol, pos.take_profit, f"停利 +{p['take_profit_pct']}%")]
        if t.price <= pos.stop:
            return [Signal("exit", st.symbol, t.price, f"停損 -{p['stop_loss_pct']}%")]
        if st.vwap and t.price < st.vwap:
            pos.below_vwap_since = pos.below_vwap_since or now
            if (now - pos.below_vwap_since).total_seconds() >= p["vwap_break_seconds"]:
                return [Signal("exit", st.symbol, t.price, f"跌破均價 {st.vwap:.2f} 超過 {p['vwap_break_seconds']} 秒")]
        else:
            pos.below_vwap_since = None
        held = now - pos.entry_ts
        if held >= timedelta(minutes=p["time_stop_minutes"]):
            flat = pos.high - pos.low <= tick_size(pos.entry) + 1e-9
            first = st.volume_between(pos.entry_ts, pos.entry_ts + timedelta(minutes=5))
            last = st.volume_between(now - timedelta(minutes=5), now)
            if flat and last <= max(first, 1) * 0.5:
                return [Signal("exit", st.symbol, t.price, f"進場 {p['time_stop_minutes']} 分鐘膠著、量縮，時間停損")]
        return []


def shares_for(price: float, p: dict) -> int:
    """整股張數 × 1000；資金不夠一張回 0。"""
    lots = int(p["capital_per_trade"] // (price * 1000))
    return lots * 1000
