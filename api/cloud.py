"""雲端部署附加功能（全部由環境變數開關，本機預設不啟用）

- API_KEY：設定後，除 /api/health 外的 /api/* 都要帶 `x-api-key` header
- STRATEGY_STORE=sheet：策略同步到 Google Sheet 的「Strategies」分頁
  （雲端主機磁碟是暫存的；啟動時從 Sheet 拉回，存檔／刪除後推上 Sheet）
- GET /api/runs/latest：目前執行中（或最近一次）的選股任務，讓其他裝置也看得到進度
- 網頁手動選股完成後，若有設 LINE_CHANNEL_TOKEN / LINE_USER_ID，推一則註明「手動觸發」的結果
- 載入加速：/api/market、/api/watchlist 回應在記憶體快取 10 分鐘（WEB_CACHE_TTL 秒）；
  啟動時背景預熱大盤資料、Google Sheet 連線與模擬帳戶
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from stock_strategies import loader

SHEET_TAB = "Strategies"
HEADER = ["id", "name", "source", "updated_at", "json"]
_lock = threading.Lock()


# ---------- API key ----------


def _install_api_key(app: FastAPI) -> None:
    key = os.environ.get("API_KEY", "")
    if not key:
        return

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        path = request.url.path
        if request.method != "OPTIONS" and path.startswith("/api/") and path != "/api/health":
            given = request.headers.get("x-api-key", "")
            if not hmac.compare_digest(given, key):
                return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        return await call_next(request)


# ---------- 載入加速：回應快取 + 啟動預熱 ----------

CACHED_PATHS = ("/api/market", "/api/watchlist")
_response_cache: dict[str, tuple[float, bytes, str]] = {}


def _install_response_cache(app: FastAPI) -> None:
    ttl = float(os.environ.get("WEB_CACHE_TTL", "600"))
    if ttl <= 0:
        return

    @app.middleware("http")
    async def cache_slow_gets(request: Request, call_next):
        path = request.url.path
        if request.method != "GET" or path not in CACHED_PATHS:
            return await call_next(request)
        key = str(request.url.path) + "?" + str(request.url.query)
        hit = _response_cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return Response(content=hit[1], media_type=hit[2], headers={"x-cache": "hit"})
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        cacheable = response.status_code == 200 and b'"error"' not in body
        if cacheable:
            _response_cache[key] = (time.monotonic(), body, response.media_type or "application/json")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=body, status_code=response.status_code, headers=headers,
                        media_type=response.media_type)


def _prewarm() -> None:
    """冷啟動後第一次開頁最慢（FinMind 大盤全歷史、Google 授權）；開機先在背景跑一次。"""
    from stock_strategies.market import get_market_state

    steps = [("market", get_market_state)]
    if os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_CREDS_JSON"):
        from stock_strategies import sim
        from stock_strategies.sheet import read_watchlist

        steps += [("watchlist", read_watchlist), ("sim", sim.load_state)]
    for name, fn in steps:
        t = time.monotonic()
        try:
            fn()
            print(f"[prewarm] {name} {time.monotonic() - t:.1f}s")
        except Exception as exc:
            print(f"[prewarm] {name} failed: {exc}", file=sys.stderr)


def _install_prewarm(app: FastAPI) -> None:
    if os.environ.get("PREWARM", "1") != "1":
        return

    @app.on_event("startup")
    def _start_prewarm():
        threading.Thread(target=_prewarm, name="prewarm", daemon=True).start()


# ---------- Strategy store: Google Sheet ----------


def _worksheet():
    import gspread

    from stock_strategies.sheet import get_gsheet

    sh = get_gsheet()
    try:
        return sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=200, cols=len(HEADER))
        ws.append_row(HEADER)
        return ws


def _row(strategy: dict) -> list:
    return [
        strategy["id"],
        strategy.get("name", ""),
        strategy.get("source", ""),
        strategy.get("updated_at", ""),
        json.dumps(strategy, ensure_ascii=False),
    ]


def pull_from_sheet() -> int:
    """Sheet → 本機 STRATEGY_DIR。Sheet 為空時把本機（內建）策略上傳當種子。"""
    ws = _worksheet()
    records = ws.get_all_records(numericise_ignore=["all"])
    if not records:
        local = [s for s in loader.list_strategies() if "error" not in s]
        if local:
            ws.append_rows([_row(s) for s in local], value_input_option="RAW")
        return len(local)
    count = 0
    for r in records:
        try:
            data = json.loads(r["json"])
            data["id"] = r["id"]
            _original_save(data)
            count += 1
        except Exception as exc:  # 單筆壞掉不影響其他策略
            print(f"[strategy-store] skip {r.get('id')}: {exc}", file=sys.stderr)
    return count


def _push(strategy: dict) -> None:
    with _lock:
        ws = _worksheet()
        ids = ws.col_values(1)
        if strategy["id"] in ids:
            row = ids.index(strategy["id"]) + 1
            ws.update(range_name=f"A{row}:E{row}", values=[_row(strategy)], value_input_option="RAW")
        else:
            ws.append_row(_row(strategy), value_input_option="RAW")


def _remove(sid: str) -> None:
    with _lock:
        ws = _worksheet()
        ids = ws.col_values(1)
        if sid in ids:
            ws.delete_rows(ids.index(sid) + 1)


_original_save = loader.save_strategy
_original_delete = loader.delete_strategy


def _save_and_push(data: dict) -> dict:
    clean = _original_save(data)
    _push(clean)
    return clean


def _delete_and_remove(sid: str) -> bool:
    ok = _original_delete(sid)
    if ok:
        _remove(sid)
    return ok


def _install_sheet_store(app: FastAPI) -> None:
    if os.environ.get("STRATEGY_STORE", "").lower() != "sheet":
        return
    loader.save_strategy = _save_and_push
    loader.delete_strategy = _delete_and_remove

    @app.on_event("startup")
    def _pull():
        try:
            n = pull_from_sheet()
            print(f"[strategy-store] synced {n} strategies from Google Sheet")
        except Exception as exc:
            print(f"[strategy-store] pull failed: {exc}", file=sys.stderr)


# ---------- Shared run status + LINE on manual runs ----------

_ACTIVE = ("queued", "running", "cancelling")


def latest_run(runs) -> dict | None:
    """執行中的任務優先，否則回最近建立的一筆（RunManager 保留 1 小時）。"""
    with runs._lock:
        runs._prune()
        jobs = list(runs._jobs.values())
        if not jobs:
            return None
        active = [j for j in jobs if j["status"] in _ACTIVE]
        job = active[0] if active else max(jobs, key=lambda j: j["created_at"])
        return runs._snapshot(job)


def _notify_line(strategy: dict, result: dict) -> None:
    from datetime import datetime, timezone, timedelta

    from stock_strategies.line_brief import format_daily_brief
    from stock_strategies.line_push import line_enabled, send_line

    if not line_enabled():
        return
    now = datetime.now(timezone(timedelta(hours=8)))
    header = f"🖐️ 手動觸發選股 {now:%m/%d %H:%M}（網頁｜策略：{strategy.get('name', strategy.get('id'))}）"
    try:
        send_line(format_daily_brief(result.get("results", []), market=result.get("market"), header=header))
    except Exception as exc:  # 推播失敗不影響網頁結果
        print(f"[line] manual-run push failed: {exc}", file=sys.stderr)


def _install_runs(app: FastAPI, runs) -> None:
    @app.get("/api/runs/latest")
    def _latest():
        return {"job": latest_run(runs)}

    original = runs._runner

    def runner_with_line(strategy, limit=None, *, progress=None, cancel=None, **kw):
        result = original(strategy, limit, progress=progress, cancel=cancel, **kw)
        if not (cancel and cancel.is_set()):
            _notify_line(strategy, result)
        return result

    runs._runner = runner_with_line


def install(app: FastAPI, runs=None) -> None:
    from api import sim_api

    _install_response_cache(app)
    _install_api_key(app)
    _install_sheet_store(app)
    _install_prewarm(app)
    if runs is not None:
        _install_runs(app, runs)
    sim_api.install(app)
