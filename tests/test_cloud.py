import importlib
import json

import pytest
from fastapi.testclient import TestClient


class FakeWS:
    def __init__(self):
        self.rows = [["id", "name", "source", "updated_at", "json"]]

    def get_all_records(self, **_):
        h = self.rows[0]
        return [dict(zip(h, r)) for r in self.rows[1:]]

    def append_row(self, row, **_):
        self.rows.append(list(row))

    def append_rows(self, rows, **_):
        self.rows.extend(list(r) for r in rows)

    def col_values(self, col):
        return [r[col - 1] for r in self.rows]

    def update(self, range_name, values, **_):
        idx = int(range_name.split(":")[0][1:]) - 1
        self.rows[idx] = list(values[0])

    def delete_rows(self, idx):
        del self.rows[idx - 1]


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    for f in ("default", "conservative"):
        src = f"strategies/{f}.json"
        (tmp_path / f"{f}.json").write_text(open(src, encoding="utf-8").read(), encoding="utf-8")
    monkeypatch.setenv("STRATEGY_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("STRATEGY_STORE", "sheet")
    import stock_strategies.loader as loader
    importlib.reload(loader)
    import api.cloud as cloud
    importlib.reload(cloud)
    ws = FakeWS()
    monkeypatch.setattr(cloud, "_worksheet", lambda: ws)
    import api.main as main
    importlib.reload(main)
    yield main, ws, tmp_path
    for k in ("STRATEGY_DIR", "API_KEY", "STRATEGY_STORE"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(loader)
    importlib.reload(cloud)
    importlib.reload(main)


def test_api_key_required(app_env):
    main, _, _ = app_env
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/strategies").status_code == 401
        assert c.get("/api/strategies", headers={"x-api-key": "wrong"}).status_code == 401
        assert c.get("/api/strategies", headers={"x-api-key": "secret"}).status_code == 200


def test_seed_save_delete_sync_to_sheet(app_env):
    main, ws, tmp = app_env
    h = {"x-api-key": "secret"}
    with TestClient(main.app) as c:  # startup → seed sheet with local strategies
        assert {"default", "conservative"} <= set(ws.col_values(1))
        r = c.post("/api/strategies", json={"id": "mine", "name": "我的策略", "params": {}}, headers=h)
        assert r.status_code == 200
        assert "mine" in ws.col_values(1)
        assert json.loads(ws.rows[-1][4])["name"] == "我的策略"
        assert c.delete("/api/strategies/mine", headers=h).status_code == 200
        assert "mine" not in ws.col_values(1)


def test_pull_restores_from_sheet(app_env):
    main, ws, tmp = app_env
    ws.append_row(["restored", "還原", "manual", "", json.dumps({"id": "restored", "name": "還原", "params": {}})])
    with TestClient(main.app) as c:
        r = c.get("/api/strategies/restored", headers={"x-api-key": "secret"})
        assert r.status_code == 200 and r.json()["name"] == "還原"
    assert (tmp / "restored.json").exists()



def test_latest_run_and_manual_line_push(app_env, monkeypatch):
    import time

    from fastapi import FastAPI

    import api.cloud as cloud
    import stock_strategies.line_push as lp
    from stock_strategies import line_brief

    main, _, _ = app_env
    sent = []
    monkeypatch.setenv("LINE_CHANNEL_TOKEN", "t")
    monkeypatch.setenv("LINE_USER_ID", "U")
    monkeypatch.setattr(lp, "send_line", sent.append)
    monkeypatch.setattr(line_brief, "_index_moves", lambda: (None, None))

    def fake_screen(strategy, limit=None, *, progress=None, cancel=None):
        progress(completed=0, total=1, current="2330")
        row = {"stock_id": "2330", "name": "台積電", "action": "BUY", "signal_score": 70,
               "entry_price": 100, "stop_loss_price": 92, "target_price": 110}
        return {"strategy": {"id": strategy["id"], "name": strategy["name"]},
                "market": {"bullish": True, "close": 100, "ma20": 90}, "results": [row]}

    main.runs._runner = fake_screen
    cloud._install_runs(FastAPI(), main.runs)  # 用真正的包裝邏輯包住假 runner
    h = {"x-api-key": "secret"}
    with TestClient(main.app) as c:
        assert c.get("/api/runs/latest", headers=h).json() == {"job": None}
        job = c.post("/api/runs", json={"strategy_id": "default"}, headers=h).json()
        for _ in range(100):
            latest = c.get("/api/runs/latest", headers=h).json()["job"]
            if latest["status"] == "completed":
                break
            time.sleep(0.05)
        assert latest["id"] == job["id"] and latest["status"] == "completed"
        last = c.get("/api/runs/last-result", headers=h).json()["run"]
        assert last["result"]["results"][0]["stock_id"] == "2330" and last["finished_at"]
    assert sent and sent[0].startswith("🖐️ 手動觸發選股") and "2330 台積電" in sent[0]


def test_response_cache_is_behind_api_key(app_env, monkeypatch):
    import api.cloud as cloud
    import api.main as main_mod

    monkeypatch.setenv("WEB_CACHE_TTL", "600")
    cloud._response_cache.clear()
    calls = []
    importlib.reload(main_mod)
    monkeypatch.setattr(main_mod, "get_market_state", lambda: calls.append(1) or {"bullish": True, "close": 1, "ma20": 1, "note": "x"})
    h = {"x-api-key": "secret"}
    with TestClient(main_mod.app) as c:
        assert c.get("/api/market", headers=h).json()["note"] == "x"
        r = c.get("/api/market", headers=h)
        assert r.headers.get("x-cache") == "hit" and r.json()["close"] == 1
        assert c.get("/api/market").status_code == 401  # 快取不能繞過驗證
    assert len(calls) == 1
    cloud._response_cache.clear()


def test_watchlist_error_not_cached(app_env, monkeypatch):
    import api.cloud as cloud
    import api.main as main_mod

    monkeypatch.setenv("WEB_CACHE_TTL", "600")
    cloud._response_cache.clear()
    importlib.reload(main_mod)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("sheet down")

    monkeypatch.setattr(main_mod, "read_watchlist", boom)
    h = {"x-api-key": "secret"}
    with TestClient(main_mod.app) as c:
        c.get("/api/watchlist", headers=h)
        c.get("/api/watchlist", headers=h)
    assert len(calls) == 2
    cloud._response_cache.clear()


def test_run_in_subprocess_progress_result_and_errors():
    import os
    import threading

    import api.cloud as cloud

    def runner(strategy, limit=None, *, progress=None, cancel=None):
        progress(completed=0, total=2, current="2330")
        progress(completed=2, total=2, current=None)
        return {"pid": os.getpid(), "results": [{"stock_id": "2330"}], "name": strategy["name"]}

    seen = []
    out = cloud.run_in_subprocess(runner)({"name": "x"}, progress=lambda **st: seen.append(st))
    assert out["pid"] != os.getpid() and out["name"] == "x"
    assert seen[-1] == {"completed": 2, "total": 2, "current": None}

    def boom(strategy, limit=None, *, progress=None, cancel=None):
        raise ValueError("壞掉")

    with pytest.raises(RuntimeError, match="壞掉"):
        cloud.run_in_subprocess(boom)({})

    def dies(strategy, limit=None, *, progress=None, cancel=None):
        os._exit(9)

    with pytest.raises(RuntimeError, match="意外結束"):
        cloud.run_in_subprocess(dies)({})

    def slow(strategy, limit=None, *, progress=None, cancel=None):
        done = []
        for i in range(200):
            if cancel.wait(0.05):
                break
            done.append(i)
        return {"partial": len(done)}

    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    out = cloud.run_in_subprocess(slow)({}, cancel=cancel)
    assert out["partial"] < 200


def test_api_stays_responsive_while_screening(monkeypatch):
    """選股在子行程跑時，API 行程不會被計算卡住。"""
    import time

    import api.cloud as cloud

    def busy(strategy, limit=None, *, progress=None, cancel=None):
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            sum(i * i for i in range(10_000))  # 純 Python 計算，會霸佔 GIL
        return {}

    import threading
    t = threading.Thread(target=cloud.run_in_subprocess(busy), args=({},))
    t.start()
    worst = 0.0
    while t.is_alive():
        s = time.perf_counter()
        sum(range(1000))
        time.sleep(0.01)
        worst = max(worst, time.perf_counter() - s)
    t.join()
    assert worst < 0.2


def test_numpy_results_are_json_safe(app_env):
    """選股結果裡的 numpy.bool_/float64（例如基本面判斷）不能讓 /api/runs/{id} 回 500。"""
    import time

    import numpy as np
    import pandas as pd

    from fastapi import FastAPI

    import api.cloud as cloud

    main, _, _ = app_env

    def np_runner(strategy, limit=None, *, progress=None, cancel=None):
        row = {"stock_id": "2330", "action": "BUY", "signal_score": np.float64(70.5),
               "components": {"fundamental_pass": np.bool_(True), "backtest_samples": np.int64(9),
                              "eps_min": np.float64("nan"), "when": pd.Timestamp("2026-11-16")}}
        return {"strategy": {"id": strategy["id"], "name": strategy["name"]},
                "market": {"bullish": np.bool_(True), "close": np.float64(1.0), "ma20": 1.0}, "results": [row]}

    main.runs._runner = np_runner
    cloud._install_runs(FastAPI(), main.runs)
    h = {"x-api-key": "secret"}
    with TestClient(main.app) as c:
        job = c.post("/api/runs", json={"strategy_id": "default"}, headers=h).json()
        for _ in range(100):
            r = c.get(f"/api/runs/{job['id']}", headers=h)
            if r.json()["status"] == "completed":
                break
            time.sleep(0.05)
    assert r.status_code == 200
    comp = r.json()["result"]["results"][0]["components"]
    assert comp["fundamental_pass"] is True and comp["backtest_samples"] == 9 and comp["eps_min"] is None
    assert comp["when"].startswith("2026-11-16")


def test_data_day_boundaries():
    from datetime import datetime, timedelta, timezone

    import api.cloud as cloud

    tw = timezone(timedelta(hours=8))
    assert cloud.data_day(datetime(2026, 11, 16, 14, 29, tzinfo=tw)) == "2026-11-13"  # 週一收盤前 → 上週五
    assert cloud.data_day(datetime(2026, 11, 16, 14, 30, tzinfo=tw)) == "2026-11-16"  # 收盤資料出來後 → 今天
    assert cloud.data_day(datetime(2026, 11, 17, 9, 0, tzinfo=tw)) == "2026-11-16"
    assert cloud.data_day(datetime(2026, 11, 22, 20, 0, tzinfo=tw)) == "2026-11-20"  # 週日 → 週五


def test_same_period_reuses_result(app_env, monkeypatch):
    """同一資料日、同一策略再按選股：直接讀上次結果，不重跑、不重複推 LINE；額度不足或改了參數才重跑。"""
    import api.cloud as cloud

    calls, sent = [], []
    monkeypatch.setattr(cloud, "_notify_line", lambda s, r: sent.append(1))

    def runner(strategy, limit=None, *, progress=None, cancel=None):
        calls.append(1)
        note = ["FinMind 免費額度暫時用完"] if len(calls) == 1 else []
        return {"strategy": {"id": strategy["id"], "name": strategy["name"]}, "market": {},
                "results": [{"stock_id": "2885", "action": "ERROR" if note else "SKIP", "risk_notes": note}]}

    wrapped = _wrap(cloud, runner)
    s = {"id": "default", "name": "預設", "params": {"a": 1}}
    first = wrapped(dict(s))
    assert "reused" not in first and len(calls) == 1
    second = wrapped(dict(s))  # 上次有額度不足的 ERROR → 要重跑補上
    assert "reused" not in second and len(calls) == 2
    seen = []
    third = wrapped(dict(s), progress=lambda **st: seen.append(st))
    assert third["reused"]["data_day"] == cloud.data_day() and len(calls) == 2 and len(sent) == 2
    assert seen[-1]["completed"] == 1
    fourth = wrapped({**s, "params": {"a": 2}})  # 策略參數改了 → 重跑
    assert "reused" not in fourth and len(calls) == 3


def _wrap(cloud, runner):
    from fastapi import FastAPI

    class FakeRuns:
        pass

    fake = FakeRuns()
    fake._runner = runner
    cloud._install_runs(FastAPI(), fake)
    return fake._runner
