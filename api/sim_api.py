"""模擬帳戶 API（網頁「模擬」頁）

GET  /api/sim         所有模擬帳戶（每個策略一個）+ 回放進度（所有裝置看到同一份）
POST /api/sim/start   {capital, start_date?, strategy_ids[]}：每個策略各開一個帳戶並排比較；
                      start_date 早於今天 → 先歷史回放再接每日紙上交易
POST /api/sim/stop    {slot?}：中止該帳戶，以最後收盤價結算所有持股；回放中則取消回放
POST /api/sim/remove  {slot}：刪掉已中止的帳戶
"""

from __future__ import annotations

import re
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from stock_strategies import loader, sim

TZ = timezone(timedelta(hours=8))


MAX_ACCOUNTS = 4
SID = r"^[a-zA-Z0-9_-]{1,60}$"


class SimStartIn(BaseModel):
    capital: float = Field(10_000, ge=1_000, le=100_000_000)
    start_date: Optional[date] = None
    strategy_id: Optional[str] = Field(None, pattern=SID)  # 舊版前端
    strategy_ids: list[str] = Field(default_factory=list, max_length=MAX_ACCOUNTS)


class SlotIn(BaseModel):
    slot: str = Field("", pattern=r"^[a-zA-Z0-9_-]{0,60}$")


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


def _accounts(fresh: bool = False) -> list[dict]:
    return [_view(s) for s in sim.load_all(fresh)]


def _response(fresh: bool = False) -> dict:
    accounts = _accounts(fresh)
    return {"account": accounts[0] if accounts else None, "accounts": accounts, "job": job.snapshot()}


def _run_replays(capital: float, start: str, todo: list[tuple[str, dict]]):
    try:
        for i, (slot, strategy) in enumerate(todo, 1):
            label = f"[{i}/{len(todo)}] {strategy.get('name', strategy['id'])}"

            def progress(_label=label, **kw):
                if kw.get("phase"):
                    kw["phase"] = f"{_label}・{kw['phase']}"
                job.update(**kw)

            job.update(phase=f"{label}・準備中", completed=0, total=None)
            state = sim.replay(capital, start, strategy, progress=progress, cancel=job.cancel)
            if job.cancel.is_set():
                job.update(status="cancelled", current=None)
                return
            sim.save_state(state, slot)
        job.update(status="completed", current=None, phase="完成")
    except Exception as exc:
        traceback.print_exc()
        job.update(status="failed", error=str(exc)[:300], current=None)


def _pick_slots(strategies: list[dict]) -> list[tuple[str, dict]]:
    """每個策略一個帳戶：同策略沿用原帳戶（進行中就拒絕），第一個帳戶位置空著就用它。"""
    existing = {s["slot"]: s for s in sim.load_all(fresh=True)}
    running = sum(1 for s in existing.values() if s.get("status") == "running")
    out, used = [], set()
    for strategy in strategies:
        same = next((slot for slot, st in existing.items() if st["strategy"]["id"] == strategy["id"]), None)
        if same is not None and existing[same].get("status") == "running":
            raise HTTPException(409, f"「{strategy.get('name', strategy['id'])}」已經有進行中的模擬，請先中止")
        slot = same if same is not None else ("" if "" not in existing and "" not in used else strategy["id"])
        used.add(slot)
        out.append((slot, strategy))
    if running + len(out) > MAX_ACCOUNTS:
        raise HTTPException(400, f"同時最多 {MAX_ACCOUNTS} 個進行中的模擬帳戶")
    return out


def install(app: FastAPI) -> None:
    @app.get("/api/sim")
    def get_sim():
        try:
            return _response()
        except Exception as exc:
            raise HTTPException(500, f"讀取模擬帳戶失敗：{exc}")

    @app.post("/api/sim/start")
    def start_sim(payload: SimStartIn):
        if job.busy:
            raise HTTPException(409, "歷史回放進行中，請稍候")
        ids = list(dict.fromkeys(payload.strategy_ids or [payload.strategy_id or "default"]))
        strategies = []
        for sid in ids:
            if not re.fullmatch(SID[1:-1], sid):
                raise HTTPException(422, f"策略代號格式錯誤：{sid}")
            strategy = loader.get_strategy(sid)
            if not strategy:
                raise HTTPException(404, f"找不到策略 {sid}")
            strategies.append(strategy)
        today = datetime.now(TZ).date()
        start = payload.start_date or today
        if start > today:
            raise HTTPException(400, "開始日期不能晚於今天")
        if start < today - timedelta(days=365):
            raise HTTPException(400, "歷史回放最多一年")
        todo = _pick_slots(strategies)
        if start >= today:
            for slot, strategy in todo:
                state = sim.new_state(payload.capital, strategy, today.isoformat(), "forward")
                state["last_date"] = today.isoformat() if datetime.now(TZ).hour >= 15 else None
                sim.save_state(state, slot)
            job.update(status="idle", phase=None, error=None)
            return _response(fresh=True)
        job.cancel.clear()
        job.update(status="running", phase="準備中", completed=0, total=None, current=None, error=None)
        threading.Thread(target=_run_replays, args=(payload.capital, start.isoformat(), todo),
                         name="sim-replay", daemon=True).start()
        return {"account": None, "accounts": _accounts(), "job": job.snapshot()}

    @app.post("/api/sim/stop")
    def stop_sim(payload: Optional[SlotIn] = None):
        if job.busy:
            job.cancel.set()
            job.update(phase="取消中")
            return {"account": None, "accounts": _accounts(), "job": job.snapshot()}
        slot = payload.slot if payload else ""
        if payload is None:  # 舊版前端：中止第一個進行中的帳戶
            running = [s for s in sim.load_all(fresh=True) if s.get("status") == "running"]
            slot = running[0]["slot"] if running else ""
        state = sim.load_state(fresh=True, slot=slot)
        if not state:
            raise HTTPException(404, "目前沒有模擬帳戶")
        if state.get("status") == "running":
            sim.stop(state)
            sim.save_state(state, slot)
        res = _response(fresh=True)
        res["account"] = _view(state)
        return res

    @app.post("/api/sim/remove")
    def remove_sim(payload: SlotIn):
        state = sim.load_state(fresh=True, slot=payload.slot)
        if not state:
            raise HTTPException(404, "找不到這個帳戶")
        if state.get("status") == "running":
            raise HTTPException(409, "請先中止再刪除")
        sim.clear_state(payload.slot)
        return _response(fresh=True)
