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

    from stock_strategies import webstore

    monkeypatch.setenv("WEBSTORE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    webstore._memory.clear()
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(compare, "run", fake_run)
    import api.cloud as cloud

    monkeypatch.setattr(cloud, "finmind_usage", lambda: None)
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
    from stock_strategies import webstore

    mod._last["result"] = None  # 重啟後從儲存讀回
    webstore._memory.clear()
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


def test_low_finmind_quota_starts_anyway_and_says_it_will_wait(tmp_path, monkeypatch):
    import threading

    import api.cloud as cloud

    gate = threading.Event()
    c, mod = _client(tmp_path, monkeypatch, lambda *a, **k: gate.wait(5) and {})
    monkeypatch.setattr(cloud, "finmind_usage", lambda *a, **k: {"used": 580, "limit": 600, "remaining": 20})
    r = c.post("/api/backtest", json={"start_date": START})
    assert r.status_code == 200, r.text
    note = r.json()["job"]["params"]["quota_note"]
    assert "只剩 20 次" in note and "自動" in note
    gate.set()


def _index(days=200):
    dates = pd.bdate_range(end=date.today(), periods=days).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "close": [100.0 + i for i in range(days)]})


def test_replay_waits_for_quota_then_continues(monkeypatch):
    from stock_strategies import cache, datasources, sheet, sim

    monkeypatch.setattr(sheet, "read_watchlist", lambda: [{"stock_id": "2330", "name": "x"}])
    calls, waits = [], []

    def flaky(sid, years):
        calls.append(sid)
        if len(calls) == 1:
            raise cache.FinMindRateLimitError("用完")
        return pd.DataFrame()

    monkeypatch.setattr(sim, "_full_history", flaky)
    monkeypatch.setattr(sim, "QUOTA_WAITER", lambda report, cancel, force=False: waits.append(force))
    monkeypatch.setattr(datasources, "get_index_history", lambda *a, **k: _index())
    with pytest.raises(RuntimeError, match="歷史股價"):  # 重試後抓到的是空資料 → 沒有任何股票可回放
        sim.replay(10_000, START, {"id": "default", "name": "x"})
    assert len(calls) == 2 and True in waits  # 用完 → 等 → 重試成功


def test_replay_stops_with_clear_message_when_quota_runs_out(monkeypatch):
    from stock_strategies import cache, datasources, sheet, sim

    monkeypatch.setattr(sheet, "read_watchlist", lambda: [{"stock_id": str(i), "name": "x"} for i in range(10)])

    def no_quota(sid, years):
        raise cache.FinMindRateLimitError("FinMind 免費額度暫時用完（每小時 600 次），約 5 分鐘後再試")

    monkeypatch.setattr(sim, "_full_history", no_quota)
    monkeypatch.setattr(datasources, "get_index_history", lambda *a, **k: _index())
    with pytest.raises(RuntimeError, match="額度用完"):
        sim.replay(10_000, START, {"id": "default", "name": "x"})
    monkeypatch.setattr(datasources, "get_index_history", lambda *a, **k: pd.DataFrame())
    with pytest.raises(RuntimeError, match="加權指數"):
        sim.replay(10_000, START, {"id": "default", "name": "x"})


def test_backtest_with_enough_log_needs_no_quota(tmp_path, monkeypatch):
    """回測紀錄夠用時不必抓資料：額度再少也不提醒、不估算。"""
    import threading

    import api.cloud as cloud
    from stock_strategies import daylog, sim

    gate = threading.Event()
    c, _ = _client(tmp_path, monkeypatch, lambda *a, **k: gate.wait(5) and {})
    monkeypatch.setattr(cloud, "finmind_usage", lambda *a, **k: {"used": 599, "limit": 600, "remaining": 1})
    monkeypatch.setattr(daylog, "needs_data", lambda strategy, start, log=None: False)
    monkeypatch.setattr(sim, "estimate_finmind_calls", lambda *a, **k: pytest.fail("不該估算"))
    r = c.post("/api/backtest", json={"start_date": START})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["params"]["quota_note"] is None
    gate.set()


def test_api_multi_strategy_and_daytrade(tmp_path, monkeypatch):
    seen = []

    def fake_run(capital, start_date, strategy, *, progress=None, cancel=None):
        seen.append(strategy["id"])
        progress(phase="逐日回放", completed=1, total=2, current=start_date)
        return {"capital": capital, "start_date": start_date, "strategy": {"id": strategy["id"], "name": strategy["id"]},
                "result": {"return_pct": len(seen)}, "benchmark": {"return_pct": 2.0}, "trades": []}

    c, mod = _client(tmp_path, monkeypatch, fake_run)
    from api import daytrade_api

    queued = []
    monkeypatch.setattr(daytrade_api, "enqueue", lambda t, p: queued.append((t, p)) or {"id": "cmd1"})
    r = c.post("/api/backtest", json={"strategy_ids": ["default", "conservative"], "include_daytrade": True,
                                      "start_date": START})
    assert r.status_code == 200, r.text
    for _ in range(100):
        body = c.get("/api/backtest").json()
        if body["job"]["status"] == "completed":
            break
        time.sleep(0.02)
    res = body["result"]
    assert seen == ["default", "conservative"]
    assert [x["strategy"]["id"] for x in res["runs"]] == ["default", "conservative"]
    assert queued == [("backtest", {"start_date": START})] and res["request"]["daytrade_command"] == "cmd1"
    assert c.post("/api/backtest", json={"strategy_ids": ["a", "b", "c", "d"], "start_date": START}).status_code == 422


def test_run_many_daytrade_only_has_benchmark(monkeypatch):
    import stock_strategies.data as data

    px = pd.DataFrame({"date": ["2026-01-02", "2026-01-05", "2026-01-06"],
                       "open": [100.0, 101.0, 102.0], "close": [100.0, 90.0, 110.0]})
    monkeypatch.setattr(data, "get_price_history", lambda sid, years: px)
    r = compare.run_many(10_000, "2026-01-05", [])
    assert r["runs"] == [] and r["start_date"] == "2026-01-05" and r["benchmark"]["available"]


def test_estimate_calls_uses_cache(tmp_path, monkeypatch):
    import json as _json

    from stock_strategies import cache, sim

    monkeypatch.setattr(cache, "_cache_dir", lambda: tmp_path)
    today = pd.Timestamp.now()
    now_utc = pd.Timestamp.now(tz="UTC").isoformat()

    def meta(ds, sid, min_date):
        p = cache._meta_path(ds, sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"fetched_at": now_utc, "min_date": min_date,
                                  "max_date": str(today.normalize().date()), "rows": 1}))

    deep = (today - pd.Timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    shallow = (today - pd.Timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    meta("TaiwanStockPrice", "2330", deep)          # 今天選過股、歷史夠長 → 0
    meta("TaiwanStockPrice", "2317", shallow)       # 只有 3 年 → 回測要重抓 1 次
    for ds in ("TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet"):
        meta(ds, "2330", None)
        meta(ds, "2317", None)
    meta("TaiwanStockPrice", "TAIEX", deep)
    est = sim.estimate_finmind_calls(4, ["2330", "2317", "2454"])  # 2454 全沒有 → 3 次
    assert est["price"] == 2 and est["fundamental"] == 2 and est["index"] == 0 and est["total"] == 4
