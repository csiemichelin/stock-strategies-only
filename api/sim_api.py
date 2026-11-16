"""模擬帳戶 API（網頁「模擬」頁）

GET  /api/sim         目前模擬帳戶 + 回放進度（所有裝置看到同一份）
POST /api/sim/start   {capital, start_date?, strategy_id}；start_date 早於今天 → 先歷史回放再接每日紙上交易
POST /api/sim/stop    中止：以最後收盤價結算所有持股
"""

from __future__ import annotations

import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from stock_strategies import loader, sim

TZ = timezone(timedelta(hours=8))


class SimStartIn(BaseModel):
    capital: float = Field(10_000, ge=1_000, le=100_000_000)
    start_date: Optional[date] = None
    strategy_id: str = Field("default", pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class _Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state = {"status": "idle", "phase": None, "completed": 0, "total": None, "current": None, "error": None}

    def update(self, **kw):
        with self.lock:
            self.state.update({k: v for k, v in kw.items() if k in self.state})

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    @property
    def busy(self):
        return self.snapshot()["status"] == "running"


job = _Job()


def _view(state: dict | None) -> dict | None:
    if not state:
        return None
    return {**state, "summary": sim.summary(state)}


def _run_replay(capital: float, start: str, strategy: dict):
    try:
        state = sim.replay(capital, start, strategy, progress=job.update, cancel=job.cancel)
        if job.cancel.is_set():
            job.update(status="cancelled", current=None)
            return
        sim.save_state(state)
        job.update(status="completed", current=None, phase="完成")
    except Exception as exc:
        traceback.print_exc()
        job.update(status="failed", error=str(exc)[:300], current=None)


def install(app: FastAPI) -> None:
    @app.get("/api/sim")
    def get_sim():
        try:
            state = sim.load_state()
        except Exception as exc:
            raise HTTPException(500, f"讀取模擬帳戶失敗：{exc}")
        return {"account": _view(state), "job": job.snapshot()}

    @app.post("/api/sim/start")
    def start_sim(payload: SimStartIn):
        if job.busy:
            raise HTTPException(409, "歷史回放進行中，請稍候")
        current = sim.load_state()
        if current and current.get("status") == "running":
            raise HTTPException(409, "已有進行中的模擬，請先中止")
        strategy = loader.get_strategy(payload.strategy_id)
        if not strategy:
            raise HTTPException(404, f"找不到策略 {payload.strategy_id}")
        today = datetime.now(TZ).date()
        start = payload.start_date or today
        if start > today:
            raise HTTPException(400, "開始日期不能晚於今天")
        if start < today - timedelta(days=365):
            raise HTTPException(400, "歷史回放最多一年")
        if start >= today:
            state = sim.new_state(payload.capital, strategy, today.isoformat(), "forward")
            state["last_date"] = today.isoformat() if datetime.now(TZ).hour >= 15 else None
            sim.save_state(state)
            job.update(status="idle", phase=None, error=None)
            return {"account": _view(state), "job": job.snapshot()}
        job.cancel.clear()
        job.update(status="running", phase="準備中", completed=0, total=None, current=None, error=None)
        threading.Thread(target=_run_replay, args=(payload.capital, start.isoformat(), strategy),
                         name="sim-replay", daemon=True).start()
        return {"account": None, "job": job.snapshot()}

    @app.post("/api/sim/stop")
    def stop_sim():
        if job.busy:
            job.cancel.set()
            job.update(phase="取消中")
            return {"account": None, "job": job.snapshot()}
        state = sim.load_state()
        if not state:
            raise HTTPException(404, "目前沒有模擬帳戶")
        if state.get("status") == "running":
            sim.stop(state)
            sim.save_state(state)
        return {"account": _view(state), "job": job.snapshot()}
