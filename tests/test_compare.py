import time
from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stock_strategies import compare

START = (date.today() - timedelta(days=90)).isoformat()


def test_max_drawdown():
    assert compare.max_drawdown([100, 120, 90, 130, 117]) == -25.0
    assert compare.max_drawdown([100, 101, 102]) == 0.0


def test_trade_stats():
    trades = [
        {"side": "BUY", "fee": 1},
        {"side": "SELL", "pnl": 100, "pnl_pct": 10, "fee": 5},
        {"side": "SELL", "pnl": -50, "pnl_pct": -5, "fee": 4},
        {"side": "SELL", "pnl": 30, "pnl_pct": 3, "fee": 2},
    ]
    s = compare.trade_stats(trades)
    assert s["trades"] == 3 and s["win_rate"] == 66.7
    assert s["avg_win_pct"] == 6.5 and s["avg_loss_pct"] == -5
    assert s["profit_factor"] == 2.6 and s["fees"] == 12


def test_benchmark_buy_and_hold_with_costs():
    px = pd.DataFrame({"date": ["2026-01-02", "2026-01-05", "2026-01-06"],
                       "open": [100.0, 101.0, 102.0], "close": [100.0, 90.0, 110.0]})
    b = compare.benchmark(10_000, ["2026-01-02", "2026-01-05", "2026-01-06"], px)
    assert b["shares"] == 99  # 100 股要 10,014 元（含手續費）買不起
    assert b["curve"][1]["total"] < b["curve"][0]["total"]
    # 賣出：110×99=10,890，手續費 15、證交稅 0.1% 10
    assert b["final"] == pytest.approx(10_000 - 9_914 + 10_890 - 15 - 10)
    assert b["max_drawdown_pct"] < 0 and b["return_pct"] > 0


def _client(tmp_path, monkeypatch, fake_run):
    from api import backtest_api

    monkeypatch.setenv("BACKTEST_FILE", str(tmp_path / "bt.json"))
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(compare, "run", fake_run)
    backtest_api._last["result"] = None
    backtest_api.job.update(status="idle", error=None)
    app = FastAPI()
    backtest_api.install(app)
    return TestClient(app), backtest_api


def test_api_runs_in_background_and_keeps_result(tmp_path, monkeypatch):
    def fake_run(capital, start_date, strategy, *, progress=None, cancel=None):
        progress(phase="逐日回放", completed=1, total=2, current=start_date)
        return {"capital": capital, "start_date": start_date, "strategy": {"id": strategy["id"]},
                "result": {"return_pct": 1.0}, "benchmark": {"return_pct": 2.0}}

    c, mod = _client(tmp_path, monkeypatch, fake_run)
    assert c.get("/api/backtest").json()["result"] is None
    r = c.post("/api/backtest", json={"strategy_id": "default", "start_date": START})
    assert r.status_code == 200
    for _ in range(100):
        body = c.get("/api/backtest").json()
        if body["job"]["status"] == "completed":
            break
        time.sleep(0.02)
    assert body["result"]["benchmark"]["return_pct"] == 2.0 and body["result"]["capital"] == 10_000
    assert body["job"]["params"]["strategy_id"] == "default"
    mod._last["result"] = None  # 重啟後從檔案讀回
    assert c.get("/api/backtest").json()["result"]["start_date"] == START


def test_api_validation_and_busy(tmp_path, monkeypatch):
    import threading

    gate = threading.Event()

    def slow_run(capital, start_date, strategy, *, progress=None, cancel=None):
        gate.wait(5)
        return None if cancel.is_set() else {"ok": True}

    c, mod = _client(tmp_path, monkeypatch, slow_run)
    assert c.post("/api/backtest", json={"start_date": "2099-01-01"}).status_code == 400
    assert c.post("/api/backtest", json={"start_date": "2020-01-01"}).status_code == 400
    assert c.post("/api/backtest", json={"start_date": START, "capital": 10}).status_code == 422
    assert c.post("/api/backtest", json={"start_date": START}).status_code == 200
    assert c.post("/api/backtest", json={"start_date": START}).status_code == 409
    c.post("/api/backtest/cancel")
    gate.set()
    for _ in range(100):
        if mod.job.snapshot()["status"] != "running":
            break
        time.sleep(0.02)
    assert mod.job.snapshot()["status"] == "cancelled"
