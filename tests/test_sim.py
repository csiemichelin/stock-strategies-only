import numpy as np
import pandas as pd
import pytest

from stock_strategies import sim
from stock_strategies.backtest import backtest
from stock_strategies.loader import merge_params


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def buy_signal(sid="2330", price=100.0, pct=20):
    return {"stock_id": sid, "name": "測試", "action": "BUY", "signal_score": 70, "entry_price": price,
            "stop_loss_price": round(price * 0.92, 2), "target_price": round(price * 1.1, 2), "position_size_pct": pct}


@pytest.fixture
def state():
    return sim.new_state(10_000, {"id": "default", "name": "預設", "params": {"hold_days": 3}}, "2026-01-02", "forward")


def test_fees_and_tax():
    assert sim.buy_cost(100, 10) == (1001, 1)          # 0.1425% 不足 1 元 → 低消 1
    assert sim.sell_proceeds("2330", 100, 10) == (996, 4)  # 手續費 1 + 證交稅 3
    assert sim.tax_rate("0050") == 0.001 and sim.tax_rate("00679B") == 0.0


def test_buy_next_open_then_take_profit(state):
    sim.step(state, "2026-01-02", {}, [buy_signal(price=100)])
    assert state["pending"][0]["stock_id"] == "2330"
    ev = sim.step(state, "2026-01-05", {"2330": bar(101, 103, 100, 102)}, [])
    pos = state["positions"][0]
    assert ev[0]["side"] == "BUY" and pos["entry_price"] == 101
    assert pos["shares"] == 19  # 20% × 10,000 = 2,000 → 19 股 @101（含手續費）
    ev = sim.step(state, "2026-01-06", {"2330": bar(104, 111, 103, 109)}, [])
    sell = [e for e in ev if e["side"] == "SELL"][0]
    assert sell["price"] == 110.0 and sell["reason"] == "停利" and not state["positions"]
    assert sim.summary(state)["pnl"] > 0


def test_stop_loss_gap_and_hold_expiry(state):
    sim.step(state, "2026-01-02", {}, [buy_signal("A", 100), buy_signal("B", 50)])
    sim.step(state, "2026-01-05", {"A": bar(100, 101, 99, 100), "B": bar(50, 51, 49, 50)}, [])
    ev = sim.step(state, "2026-01-06", {"A": bar(90, 91, 89, 90), "B": bar(50, 51, 49, 50)}, [])
    a = [e for e in ev if e.get("stock_id") == "A"][0]
    assert a["price"] == 90 and a["reason"] == "停損（開盤跳空）"
    ev = sim.step(state, "2026-01-07", {"B": bar(50, 51, 49, 50.5)}, [])
    b = [e for e in ev if e.get("stock_id") == "B"][0]
    assert b["reason"] == "持有滿 3 日" and b["price"] == 50.5


def test_same_day_stop_and_target_is_conservative(state):
    sim.step(state, "2026-01-02", {}, [buy_signal(price=100)])
    sim.step(state, "2026-01-05", {"2330": bar(100, 100, 100, 100)}, [])
    ev = sim.step(state, "2026-01-06", {"2330": bar(100, 115, 85, 100)}, [])
    assert ev[0]["reason"] == "停損"


def test_not_enough_cash_skips(state):
    sim.step(state, "2026-01-02", {}, [buy_signal(price=5000)])
    ev = sim.step(state, "2026-01-05", {"2330": bar(5000, 5000, 5000, 5000)}, [])
    assert ev[0]["type"] == "skip" and not state["positions"]


def test_stop_liquidates(state):
    sim.step(state, "2026-01-02", {}, [buy_signal(price=100)])
    sim.step(state, "2026-01-05", {"2330": bar(100, 101, 99, 100)}, [])
    sim.stop(state)
    assert state["status"] == "stopped" and not state["positions"] and state["trades"][-1]["side"] == "SELL"


def _synthetic(n=900, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    o = c * (1 + rng.normal(0, 0.006, n))
    return pd.DataFrame({"date": pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d"),
                         "open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99,
                         "close": c, "volume": rng.integers(1_000_000, 5_000_000, n).astype(float)})


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_replay_backtest_matches_original_logic(seed):
    params = merge_params(None)
    raw = _synthetic(seed=seed)
    st = sim._StockReplay("X", "X", raw, params, 3)
    for day in raw["date"].iloc[[300, 500, 700, -1]]:
        s, L = st.window(day)
        original = backtest(st.ind.iloc[s:L].reset_index(drop=True), params)
        fast = st.backtest(s, L)
        assert fast["samples"] == original["samples"]
        assert fast["winrate"] == original["winrate"]


def test_replay_hooks_feed_as_of_data_to_evaluate():
    from stock_strategies.evaluate import evaluate

    sim._install_replay_hooks()
    params = merge_params(None)
    raw = _synthetic(seed=4)
    st = sim._StockReplay("X", "X", raw, params, 3)
    day = raw["date"].iloc[600]
    with sim._as_of(st, day, {"eps": {2023: 10, 2024: 12}, "roe": {2023: 20, 2024: 22}}):
        r = evaluate("X", "X")
    assert r["action"] in ("BUY", "WATCH", "SKIP")
    assert r["entry_price"] == pytest.approx(float(raw["close"].iloc[600]))
    assert sim._ctx.cur is None  # 離開回放後不影響一般選股


def test_summary_lines(state):
    sim.step(state, "2026-01-02", {}, [buy_signal(price=100)])
    ev = sim.step(state, "2026-01-05", {"2330": bar(100, 101, 99, 100)}, [])
    lines = sim.summary_lines(state, ev)
    assert lines[0].startswith("💼 模擬帳戶（本金 10,000）")
    assert any(l.startswith("今日：買 2330") for l in lines) and any(l.startswith("持倉：2330") for l in lines)


def test_local_store_roundtrip(tmp_path, monkeypatch, state):
    monkeypatch.setenv("SIM_STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    sim.save_state(state)
    assert sim.load_state()["initial_capital"] == 10_000
    sim.clear_state()
    assert sim.load_state() is None


class _FakeSheet:
    def __init__(self, state):
        import json as _j

        core = {k: v for k, v in state.items() if k not in ("trades", "equity")}
        self.batch_calls = 0
        self.ranges = {
            "SimState": [[_j.dumps(core, ensure_ascii=False)]],
            "SimTrades": [sim.TRADE_COLS, ["2026-01-05", "BUY", "0050", "元大台灣50", "19", "1,001.5", "19028.5", "27"]],
            "SimEquity": [sim.EQUITY_COLS, ["2026-01-05", "100", "9900", "10000"], ["", "", "", ""]],
        }

    def values_batch_get(self, ranges):
        self.batch_calls += 1
        return {"valueRanges": [{"values": self.ranges[r.split("'")[1]]} for r in ranges]}


def test_sheet_load_is_one_batch_call_and_cached(monkeypatch, state):
    from stock_strategies import sheet

    fake = _FakeSheet(state)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "x")
    monkeypatch.setenv("GOOGLE_CREDS_JSON", "{}")
    monkeypatch.setattr(sheet, "get_gsheet", lambda: fake)
    sim._cache.update(state=None, ts=0.0)
    s1 = sim.load_state()
    assert s1["trades"][0]["stock_id"] == "0050" and s1["trades"][0]["shares"] == 19
    assert s1["trades"][0]["price"] == 1001.5 and s1["trades"][0]["pnl"] is None
    assert len(s1["equity"]) == 1 and s1["equity"][0]["total"] == 10000
    s1["cash"] = -1  # 呼叫端改動不影響快取
    assert sim.load_state()["cash"] == 10_000 and fake.batch_calls == 1
    sim.load_state(fresh=True)
    assert fake.batch_calls == 2
    sim._cache.update(state=None, ts=0.0)
