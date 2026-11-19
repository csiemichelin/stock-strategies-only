"""基本面快照（近三年年度 EPS／ROE）：所有地方共用一份，省 FinMind 額度。

get_fundamental() 每檔要打 2 次 FinMind（損益表、資產負債表）。這些年度數字一年才變一次，但 GitHub Actions 的
每日選股、Render 的網頁選股、回測／回放各有各的快取（而且重新部署就清空），以前每台機器每天各抓一次。
現在算好的結果存在 Google Sheet（WebCache 分頁的 fund_snapshot；沒設 Sheet 時存本機），MAX_AGE_DAYS 天內
誰要用都直接拿，過期才重抓、抓完寫回去給大家用。設 FUND_SNAPSHOT=0 可關掉（一律直接問 FinMind）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

NAME = "fund_snapshot"
MAX_AGE_DAYS = 7
RELOAD_SECONDS = 600  # 常駐的 API 行程每 10 分鐘重讀一次（別台機器可能更新過）
TZ = timezone(timedelta(hours=8))
_lock = threading.Lock()
_state: dict = {"loaded": False, "at": 0.0, "stocks": {}, "dirty": set()}


def enabled() -> bool:
    return os.environ.get("FUND_SNAPSHOT", "1") != "0"


def reset() -> None:
    with _lock:
        _state.update(loaded=False, at=0.0, stocks={}, dirty=set())


def _today() -> date:
    return datetime.now(TZ).date()


def _load_locked() -> None:
    if _state["loaded"] and (_state["dirty"] or time.monotonic() - _state["at"] < RELOAD_SECONDS):
        return
    _state.update(loaded=True, at=time.monotonic())
    try:
        from . import webstore

        saved = webstore.load(NAME, fresh=True) or {}
        _state["stocks"] = dict(saved.get("stocks") or {})
    except Exception as exc:  # noqa: BLE001 讀不到就當沒有快照，照舊問 FinMind
        print(f"[fundstore] load failed: {exc}", file=sys.stderr)


def _encode(fund: dict) -> dict:
    """年份當字串、NaN 記成 null，JSON 才存得下。"""
    return {k: {str(int(y)): (None if v is None or v != v else float(v)) for y, v in (fund.get(k) or {}).items()}
            for k in ("eps", "roe")}


def _decode(entry: dict) -> dict:
    return {k: {int(y): (float("nan") if v is None else float(v)) for y, v in (entry.get(k) or {}).items()}
            for k in ("eps", "roe")}


def _fresh(entry: dict) -> bool:
    try:
        age = (_today() - date.fromisoformat(str(entry.get("d", ""))[:10])).days
    except ValueError:
        return False
    return 0 <= age <= MAX_AGE_DAYS


def get(stock_id: str) -> dict | None:
    """MAX_AGE_DAYS 天內的快照；沒有或過期回 None。"""
    if not enabled():
        return None
    with _lock:
        _load_locked()
        entry = _state["stocks"].get(str(stock_id))
    return _decode(entry) if entry and _fresh(entry) else None


def put(stock_id: str, fund: dict) -> None:
    if not enabled():
        return
    entry = _encode(fund)
    with _lock:
        _load_locked()
        old = _state["stocks"].get(str(stock_id))
        if old and (old.get("eps") or old.get("roe")) and not (entry["eps"] or entry["roe"]):
            return  # 以前有數字、這次 FinMind 回空的（多半是暫時抓不到）：不覆蓋，下次再問
        _state["stocks"][str(stock_id)] = {"d": _today().isoformat(), **entry}
        _state["dirty"].add(str(stock_id))


def flush() -> int:
    """把這次新抓到的寫回共用快照（先重讀一次，合併別台機器剛寫的）。失敗只記 log，不影響主流程。"""
    with _lock:
        mine = {sid: _state["stocks"][sid] for sid in _state["dirty"] if sid in _state["stocks"]}
    if not mine:
        return 0
    try:
        from . import webstore

        latest = dict((webstore.load(NAME, fresh=True) or {}).get("stocks") or {})
        for sid, entry in mine.items():
            if sid not in latest or str(latest[sid].get("d", "")) <= entry["d"]:
                latest[sid] = entry
        webstore.save(NAME, {"stocks": latest})
        with _lock:
            _state["stocks"] = {**latest, **{k: v for k, v in _state["stocks"].items() if k not in latest}}
            _state["dirty"] -= set(mine)
    except Exception as exc:  # noqa: BLE001
        print(f"[fundstore] save failed: {exc}", file=sys.stderr)
        return 0
    return len(mine)
