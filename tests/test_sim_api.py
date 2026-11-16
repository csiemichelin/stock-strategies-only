from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import sim_api
from stock_strategies import sim


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_FILE", str(tmp_path / "sim.json"))
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    app = FastAPI()
    sim_api.install(app)
    return TestClient(app)


def test_forward_start_stop_and_conflict(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/api/sim").json()["account"] is None
    r = c.post("/api/sim/start", json={"capital": 10000})
    assert r.status_code == 200 and r.json()["account"]["summary"]["total"] == 10000
    assert c.post("/api/sim/start", json={"capital": 5000}).status_code == 409
    r = c.post("/api/sim/stop")
    assert r.json()["account"]["status"] == "stopped"
    assert c.post("/api/sim/start", json={"capital": 20000}).json()["account"]["initial_capital"] == 20000


def test_validation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.post("/api/sim/start", json={"capital": 10}).status_code == 422
    assert c.post("/api/sim/start", json={"capital": 10000, "start_date": "2099-01-01"}).status_code == 400


def test_daily_update_trades_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_FILE", str(tmp_path / "sim.json"))
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    st = sim.new_state(10000, {"id": "default", "name": "預設"}, "2026-11-16", "forward")
    sim.save_state(st)
    sig = {"stock_id": "2308", "name": "台達電", "action": "BUY", "signal_score": 72, "entry_price": 100,
           "stop_loss_price": 92, "target_price": 110, "position_size_pct": 20, "date": "2026-11-16"}
    monkeypatch.setattr(sim, "_latest_bar_date", lambda: "2026-11-16")
    monkeypatch.setattr(sim, "_bars_for", lambda ids, after, until: {})
    lines = sim.daily_update([sig])
    assert any("明天開盤買：2308 台達電" in l for l in lines)
    monkeypatch.setattr(sim, "_latest_bar_date", lambda: "2026-11-17")
    monkeypatch.setattr(sim, "_bars_for", lambda ids, after, until: {"2026-11-17": {"2308": {"open": 101, "high": 102, "low": 100, "close": 101.5}}})
    lines = sim.daily_update([])
    assert any(l.startswith("今日：買 2308 台達電 19 股") for l in lines)
    assert sim.load_state()["positions"][0]["shares"] == 19
