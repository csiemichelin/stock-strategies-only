"""網頁要長期保留的小資料（上次選股結果、上次回測結果）。

Render 免費方案的磁碟每次重新部署就清空，所以有設 Google Sheet 時存到「WebCache」分頁：
一列一筆，A=名稱、B=更新時間、C 之後=JSON 分段（單格上限 5 萬字）。沒設 Sheet 時存本機檔案。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TAB = "WebCache"
CHUNK = 45_000
TZ = timezone(timedelta(hours=8))
_lock = threading.Lock()
_memory: dict[str, object] = {}


def _use_sheet() -> bool:
    return bool(os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_CREDS_JSON"))


def _local(name: str) -> Path:
    root = Path(os.environ.get("WEBSTORE_DIR", Path(__file__).resolve().parent.parent / ".cache" / "webstore"))
    return root / f"{name}.json"


def _worksheet():
    import gspread

    from .sheet import get_gsheet

    sh = get_gsheet()
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=TAB, rows=20, cols=30)


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def save(name: str, obj) -> None:
    text = json.dumps(obj, ensure_ascii=False)
    with _lock:
        _memory[name] = json.loads(text)
        if not _use_sheet():
            p = _local(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return
        ws = _worksheet()
        names = ws.col_values(1)
        row = names.index(name) + 1 if name in names else len(names) + 1
        old_width = len(ws.row_values(row)) if name in names else 0
        chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
        values = [name, datetime.now(TZ).isoformat(timespec="seconds"), *chunks]
        values += [""] * max(0, old_width - len(values))  # 清掉上次較長的尾巴
        if ws.col_count < len(values):
            ws.add_cols(len(values) - ws.col_count)
        if ws.row_count < row:
            ws.add_rows(row - ws.row_count)
        ws.update(range_name=f"A{row}:{_col(len(values))}{row}", values=[values], value_input_option="RAW")


def load(name: str, fresh: bool = False):
    """fresh=True：不用這個行程記住的版本，重新讀一次（別台機器可能剛寫過）。"""
    with _lock:
        if name in _memory and not fresh:
            return _memory[name]
        if not _use_sheet():
            p = _local(name)
            obj = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        else:
            ws = _worksheet()
            names = ws.col_values(1)
            if name not in names:
                _memory.pop(name, None)
                return None
            row = ws.row_values(names.index(name) + 1)
            text = "".join(row[2:])
            obj = json.loads(text) if text else None
        if obj is not None:
            _memory[name] = obj
        return obj


def save_quietly(name: str, obj) -> None:
    """存不成功不影響主流程（只記 log）。"""
    try:
        save(name, obj)
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"[webstore] save {name} failed: {exc}", file=sys.stderr)
