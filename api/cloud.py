"""雲端部署附加功能（全部由環境變數開關，本機預設不啟用）

- API_KEY：設定後，除 /api/health 外的 /api/* 都要帶 `x-api-key` header
- STRATEGY_STORE=sheet：策略同步到 Google Sheet 的「Strategies」分頁
  （雲端主機磁碟是暫存的；啟動時從 Sheet 拉回，存檔／刪除後推上 Sheet）
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


def install(app: FastAPI) -> None:
    _install_api_key(app)
    _install_sheet_store(app)
