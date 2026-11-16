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
