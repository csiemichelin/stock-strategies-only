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
