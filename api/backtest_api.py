"""回測比較 API（網頁「回測比較」頁）

GET  /api/backtest          目前進度 + 最近一次結果（所有裝置看到同一份）
POST /api/backtest          {strategy_id, start_date, capital}：背景跑「策略 vs 抱 0050」
POST /api/backtest/cancel   取消
不會動到模擬交易帳戶。
"""

from __future__ import annotations

import os
import threading
import traceback
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from stock_strategies import compare, loader, webstore

TZ = timezone(timedelta(hours=8))
MAX_DAYS = 365
MIN_DAYS = 7


class BacktestIn(BaseModel):
    strategy_id: str = Field("default", pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    start_date: date
    capital: float = Field(10_000, ge=1_000, le=100_000_000)


class _Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state = {"status": "idle", "phase": None, "completed": 0, "total": None, "current": None,
                      "error": None, "params": None, "started_at": None}

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
_last: dict = {"result": None}


def last_result():
    if _last["result"] is None:
        try:
            _last["result"] = webstore.load("last_backtest")
        except Exception as exc:  # noqa: BLE001
            print(f"[webstore] load last_backtest failed: {exc}")
    return _last["result"]


def _save(result: dict) -> None:
    _last["result"] = result
    webstore.save_quietly("last_backtest", result)  # 存 Google Sheet，重新部署後還看得到


def _runner():
    """Render 上跟網頁選股一樣放到子行程，計算不拖慢其他頁面。"""
    def run(payload, _limit=None, *, progress=None, cancel=None):
        return compare.run(payload["capital"], payload["start_date"], payload["strategy"],
                           progress=progress, cancel=cancel)

    from api.cloud import run_in_subprocess

    isolation = os.environ.get("RUN_ISOLATION", "process" if os.environ.get("RENDER") else "thread")
    return run_in_subprocess(run) if isolation == "process" else run


def _execute(payload: dict) -> None:
    try:
        result = _runner()(payload, None, progress=job.update, cancel=job.cancel)
        if job.cancel.is_set() or result is None:
            job.update(status="cancelled", current=None, phase="已取消")
            return
        from api.cloud import to_plain

        result = to_plain(result)
        result["finished_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        _save(result)
        job.update(status="completed", current=None, phase="完成")
    except Exception as exc:
        traceback.print_exc()
        job.update(status="failed", error=str(exc)[:300], current=None)


def install(app: FastAPI) -> None:
    @app.get("/api/backtest")
    def get_backtest():
        return {"job": job.snapshot(), "result": last_result()}

    @app.post("/api/backtest")
    def start_backtest(payload: BacktestIn):
        from api import sim_api

        if job.busy:
            raise HTTPException(409, "已有回測在跑，請等它完成或先取消")
        if sim_api.job.busy:
            raise HTTPException(409, "模擬交易的歷史回放進行中，請稍後再試")
        strategy = loader.get_strategy(payload.strategy_id)
        if not strategy:
            raise HTTPException(404, f"找不到策略 {payload.strategy_id}")
        today = datetime.now(TZ).date()
        if payload.start_date > today - timedelta(days=MIN_DAYS):
            raise HTTPException(400, f"回測期間至少要 {MIN_DAYS} 天")
        if payload.start_date < today - timedelta(days=MAX_DAYS + 5):
            raise HTTPException(400, "回測期間最長一年")
        params = {"strategy_id": strategy["id"], "strategy_name": strategy.get("name", strategy["id"]),
                  "start_date": payload.start_date.isoformat(), "capital": payload.capital}
        job.cancel.clear()
        job.update(status="running", phase="準備中", completed=0, total=None, current=None, error=None,
                   params=params, started_at=datetime.now(TZ).isoformat(timespec="seconds"))
        data = {"capital": payload.capital, "start_date": payload.start_date.isoformat(), "strategy": strategy}
        threading.Thread(target=_execute, args=(data,), name="backtest", daemon=True).start()
        return {"job": job.snapshot(), "result": last_result()}

    @app.post("/api/backtest/cancel")
    def cancel_backtest():
        if job.busy:
            job.cancel.set()
            job.update(phase="取消中")
        return {"job": job.snapshot(), "result": last_result()}
