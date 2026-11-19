"""回測比較 API（網頁「回測比較」頁）

GET  /api/backtest          目前進度 + 最近一次結果（所有裝置看到同一份）
POST /api/backtest          {strategy_ids[], include_daytrade, start_date, capital}：背景跑「多個策略 vs 抱 0050」；
                            勾了當沖就另外請本機凱基程式回測同一段期間（結果在 /api/daytrade 的 backtest）
POST /api/backtest/cancel   取消
不會動到模擬交易帳戶。
"""

from __future__ import annotations

import os
import re
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from stock_strategies import compare, loader, webstore

TZ = timezone(timedelta(hours=8))
MAX_DAYS = 365
MIN_DAYS = 7
MIN_QUOTA = 150  # 估不出需求時的保守值
QUOTA_MARGIN = 20  # 估計值之外多留一點（大盤、偶發重試）


MAX_STRATEGIES = 3  # Render 免費方案 0.1 CPU / 512MB，一次最多比三個


class BacktestIn(BaseModel):
    strategy_id: Optional[str] = Field(None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")  # 舊版前端
    strategy_ids: list[str] = Field(default_factory=list, max_length=MAX_STRATEGIES)
    include_daytrade: bool = False
    start_date: date
    capital: float = Field(10_000, ge=1_000, le=100_000_000)
    recompute: bool = False  # 不沿用回測紀錄，全部重算（觀察清單改了的時候用）


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
        from api.cloud import quota_waiter
        from stock_strategies import sim

        sim.QUOTA_WAITER = quota_waiter()  # 額度不夠就等下一小時自動繼續，不直接失敗
        return compare.run_many(payload["capital"], payload["start_date"], payload["strategies"],
                                progress=progress, cancel=cancel, recompute=bool(payload.get("recompute")))

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
        result["request"] = payload.get("params")
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
        ids = list(dict.fromkeys(payload.strategy_ids or ([payload.strategy_id] if payload.strategy_id else [])))
        if not ids and not payload.include_daytrade:
            ids = ["default"]
        if len(ids) > MAX_STRATEGIES:
            raise HTTPException(400, f"一次最多比較 {MAX_STRATEGIES} 個策略")
        strategies = []
        for sid in ids:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", sid):
                raise HTTPException(422, f"策略代號格式錯誤：{sid}")
            strategy = loader.get_strategy(sid)
            if not strategy:
                raise HTTPException(404, f"找不到策略 {sid}")
            strategies.append(strategy)
        from api.cloud import finmind_usage

        usage = finmind_usage()
        quota_note = None
        if strategies and usage:
            from stock_strategies import loader as _loader, sim as _sim

            from stock_strategies import daylog as _daylog

            years = max(int(_loader.merge_params(s)["backtest_years"]) for s in strategies) + 1
            try:
                start_s = payload.start_date.isoformat()
                fetch = payload.recompute or any(_daylog.needs_data(s, start_s) for s in strategies)
                need = _sim.estimate_finmind_calls(years)["total"] if fetch else 0  # 紀錄夠用就不抓資料
            except Exception as exc:  # noqa: BLE001 估不出來就用保守值
                print(f"[backtest] estimate failed: {exc}")
                need = MIN_QUOTA
            finally:
                _daylog.forget()
            if need and usage["remaining"] < need + QUOTA_MARGIN:
                quota_note = (f"這次大約要用 {need} 次 FinMind，這個小時只剩 {usage['remaining']} 次："
                              "會先用完本小時的額度，再自動等下一小時繼續（可以關掉頁面）")
        today = datetime.now(TZ).date()
        if payload.start_date > today - timedelta(days=MIN_DAYS):
            raise HTTPException(400, f"回測期間至少要 {MIN_DAYS} 天")
        if payload.start_date < today - timedelta(days=MAX_DAYS + 5):
            raise HTTPException(400, "回測期間最長一年")
        daytrade_cmd = None
        if payload.include_daytrade:
            from api import daytrade_api

            daytrade_cmd = daytrade_api.enqueue("backtest", {"start_date": payload.start_date.isoformat()})
        names = [s.get("name", s["id"]) for s in strategies] + (["當沖"] if payload.include_daytrade else [])
        params = {"strategy_id": ids[0] if ids else "daytrade", "strategy_name": "、".join(names), "quota_note": quota_note,
                  "strategy_ids": ids, "include_daytrade": payload.include_daytrade,
                  "daytrade_command": daytrade_cmd["id"] if daytrade_cmd else None,
                  "start_date": payload.start_date.isoformat(), "capital": payload.capital}
        job.cancel.clear()
        job.update(status="running", phase="準備中", completed=0, total=None, current=None, error=None,
                   params=params, started_at=datetime.now(TZ).isoformat(timespec="seconds"))
        data = {"capital": payload.capital, "start_date": payload.start_date.isoformat(), "strategies": strategies,
                "params": params, "recompute": payload.recompute}
        threading.Thread(target=_execute, args=(data,), name="backtest", daemon=True).start()
        return {"job": job.snapshot(), "result": last_result()}

    @app.post("/api/backtest/cancel")
    def cancel_backtest():
        if job.busy:
            job.cancel.set()
            job.update(phase="取消中")
        return {"job": job.snapshot(), "result": last_result()}
