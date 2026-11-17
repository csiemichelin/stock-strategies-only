"""當沖策略雲端端點：本機常駐程式（凱基 API）把計畫、盤前結果、進出場回報上來；網頁與 LINE 從這裡讀。

POST /api/daytrade/plan       隔天要觀察的候選名單（盤後選股結果）
POST /api/daytrade/event      macro / preopen / entry / exit / halt / summary / info / error / heartbeat
POST /api/daytrade/backtest   本機回測結果
GET  /api/daytrade            網頁用：計畫、今天的事件、最近每日損益、回測、本機程式最後回報時間
GET  /api/daytrade/macro      本機 08:30 查：台指期夜盤、台積電 ADR、那斯達克漲跌
POST /api/daytrade/commands   網頁請本機程式做事：{type: screen|backtest, params}
GET  /api/daytrade/commands/next      本機程式輪詢，領走最舊的待辦
POST /api/daytrade/commands/{id}/done 本機回報完成／失敗
全部走既有的 x-api-key 驗證。
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from stock_strategies import webstore

TZ = timezone(timedelta(hours=8))
_lock = threading.Lock()
_heartbeat: dict = {"at": None, "mode": None, "note": None, "agent_at": None}
_macro_cache: dict = {"day": None, "value": None}
LINE_EVENTS = {"plan", "macro", "preopen", "entry", "exit", "halt", "summary", "error"}


class PlanIn(BaseModel):
    trade_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    data_date: str
    candidates: list[dict]
    stats: dict = {}
    params: dict = {}
    notes: list[str] = []


class CommandIn(BaseModel):
    type: str = Field(pattern=r"^(screen|backtest)$")
    params: dict[str, Any] = {}


class CommandDone(BaseModel):
    ok: bool = True
    message: str = ""


COMMANDS_KEY = "daytrade_commands"
STALE_MINUTES = 90  # 本機領走後太久沒回報 → 視為失敗，可以重新送


def _commands() -> list[dict]:
    return list(webstore.load(COMMANDS_KEY) or [])


def _expire(cmds: list[dict]) -> bool:
    changed = False
    for c in cmds:
        if c["status"] == "running" and c.get("started_at"):
            started = datetime.fromisoformat(c["started_at"])
            if _now() - started > timedelta(minutes=STALE_MINUTES):
                c.update(status="failed", message="本機程式太久沒回報（可能被關掉），請重新送出", finished_at=_now().isoformat(timespec="seconds"))
                changed = True
    return changed


def enqueue(type_: str, params: dict | None = None) -> dict:
    """同類型已有待辦或執行中就沿用（例如重複按回測）。"""
    with _lock:
        cmds = _commands()
        _expire(cmds)
        for c in cmds:
            if c["type"] == type_ and c["status"] in ("pending", "running") and c.get("params") == (params or {}):
                return c
        cmd = {"id": _now().strftime("%Y%m%d%H%M%S") + f"-{type_}", "type": type_, "params": params or {},
               "status": "pending", "created_at": _now().isoformat(timespec="seconds"), "message": ""}
        webstore.save(COMMANDS_KEY, (cmds + [cmd])[-20:])
        return cmd


class EventIn(BaseModel):
    type: str = Field(pattern=r"^(macro|preopen|entry|exit|halt|summary|info|error|heartbeat)$")
    symbol: Optional[str] = None
    name: Optional[str] = None
    price: Optional[float] = None
    shares: Optional[int] = None
    pnl: Optional[float] = None
    ok: Optional[bool] = None
    mode: Optional[str] = None
    reason: str = ""
    extra: dict[str, Any] = {}


def _now() -> datetime:
    return datetime.now(TZ)


def _today() -> str:
    return _now().date().isoformat()


def _push(text: str) -> None:
    try:
        from stock_strategies.line_push import line_enabled, send_line

        if line_enabled():
            send_line(text)
    except Exception as exc:  # noqa: BLE001 推播失敗不影響紀錄
        print(f"[daytrade] LINE failed: {exc}", file=sys.stderr)


def _mode_tag(mode: str | None) -> str:
    return {"paper": "（紙上模擬）", "kgi-sim": "（凱基測試環境）", "live": ""}.get(mode or "", "")


def plan_text(plan: dict) -> str:
    cands = plan["candidates"]
    lines = [f"⚡ 當沖觀察名單 {plan['trade_date'][5:]}（{plan['data_date'][5:]} 盤後選出）"]
    if not cands:
        lines.append("今天沒有符合條件的標的，明天不做當沖。")
    for c in cands:
        share = c.get("daytrader_share")
        lines.append(f"{c['stock_id']} {c.get('name', '')}　收 {c['close']:g}（{c['change_pct']:+.1f}%）"
                     f"量 {c['volume_lots']:,.0f} 張・{c['volume_ratio']:.1f} 倍"
                     + ("" if share is None else f"・隔日沖 {share * 100:.0f}%"))
    lines += [""] + [f"・{n}" for n in plan.get("notes", [])] + ["明早 08:59 盤前體檢後再通知能不能做。"]
    return "\n".join(x for x in lines if x is not None)


def event_text(ev: dict) -> str | None:
    t, tag = ev["type"], _mode_tag(ev.get("mode"))
    sym = f"{ev.get('symbol') or ''} {ev.get('name') or ''}".strip()
    if t == "macro":
        return f"{'🟢' if ev.get('ok') else '🛑'} 當沖盤前：{ev['reason']}"
    if t == "preopen":
        return f"{'✅' if ev.get('ok') else '⏭️'} {sym}　{ev['reason']}"
    if t == "entry":
        return f"📥 當沖進場{tag} {sym} {ev.get('shares', 0) // 1000} 張 @{ev.get('price')}\n{ev['reason']}"
    if t == "exit":
        pnl = ev.get("pnl") or 0
        return f"{'💰' if pnl > 0 else '🔻'} 當沖出場{tag} {sym} @{ev.get('price')}　{pnl:+,.0f} 元\n{ev['reason']}"
    if t in ("halt", "error"):
        return f"⚠️ 當沖{tag}：{ev['reason']}"
    if t == "summary":
        return f"📊 今日當沖{tag}：{ev['reason']}"
    return None


def _append_event(ev: dict) -> None:
    key = f"daytrade_day_{_today()}"
    with _lock:
        day = webstore.load(key) or {"date": _today(), "events": []}
        day["events"].append(ev)
        webstore.save(key, day)
        if ev["type"] in ("exit", "summary"):
            hist = webstore.load("daytrade_history") or []
            exits = [e for e in day["events"] if e["type"] == "exit"]
            item = {"date": _today(), "trades": len(exits), "pnl": round(sum(e.get("pnl") or 0 for e in exits), 2),
                    "wins": sum(1 for e in exits if (e.get("pnl") or 0) > 0), "mode": ev.get("mode")}
            hist = [h for h in hist if h["date"] != item["date"]] + [item]
            webstore.save("daytrade_history", hist[-120:])


def _us_change(stock_id: str) -> float | None:
    """美股前一交易日漲跌幅（FinMind USStockPrice）；抓不到回 None。"""
    import requests

    try:
        start = (_now() - timedelta(days=10)).strftime("%Y-%m-%d")
        body = requests.get("https://api.finmindtrade.com/api/v4/data", timeout=15, params={
            "dataset": "USStockPrice", "data_id": stock_id, "start_date": start,
            "token": os.environ.get("FINMIND_TOKEN", "")}).json()
        rows = [r for r in body.get("data", []) if r.get("Close")]
        if len(rows) < 2:
            return None
        return (float(rows[-1]["Close"]) / float(rows[-2]["Close"]) - 1) * 100
    except Exception as exc:  # noqa: BLE001
        print(f"[daytrade] US {stock_id} failed: {exc}", file=sys.stderr)
        return None


def macro_snapshot() -> dict:
    if _macro_cache["day"] == _today() and _macro_cache["value"]:
        return _macro_cache["value"]
    night = None
    try:
        from stock_strategies.night_session import get_night_session

        n = get_night_session()
        night = None if not n else round(float(n["pct"]), 2)
    except Exception as exc:  # noqa: BLE001
        print(f"[daytrade] night failed: {exc}", file=sys.stderr)
    value = {"night_pct": night, "adr_pct": _us_change("TSM"), "nasdaq_pct": _us_change("QQQ"),
             "at": _now().isoformat(timespec="seconds")}
    for k in ("adr_pct", "nasdaq_pct"):
        value[k] = None if value[k] is None else round(value[k], 2)
    if _now().hour >= 8:  # 08:00 前夜盤可能還沒結算，不快取
        _macro_cache.update(day=_today(), value=value)
    return value


def install(app: FastAPI) -> None:
    @app.post("/api/daytrade/plan")
    def post_plan(plan: PlanIn):
        data = {**plan.model_dump(), "received_at": _now().isoformat(timespec="seconds")}
        webstore.save("daytrade_plan", data)
        _push(plan_text(data))
        return {"ok": True}

    @app.post("/api/daytrade/event")
    def post_event(ev: EventIn):
        data = {**ev.model_dump(), "ts": _now().isoformat(timespec="seconds")}
        if ev.type == "heartbeat":
            _heartbeat.update(at=data["ts"], mode=ev.mode, note=ev.reason)
            return {"ok": True}
        _heartbeat.update(at=data["ts"], mode=ev.mode or _heartbeat["mode"])
        _append_event(data)
        text = event_text(data) if ev.type in LINE_EVENTS else None
        if text:
            _push(text)
        return {"ok": True}

    @app.post("/api/daytrade/backtest")
    def post_backtest(result: dict):
        webstore.save("daytrade_backtest", {**result, "received_at": _now().isoformat(timespec="seconds")})
        return {"ok": True}

    @app.post("/api/daytrade/commands")
    def post_command(cmd: CommandIn):
        return enqueue(cmd.type, cmd.params)

    @app.get("/api/daytrade/commands/next")
    def next_command(mode: Optional[str] = None):
        _heartbeat.update(agent_at=_now().isoformat(timespec="seconds"), mode=mode or _heartbeat["mode"])
        with _lock:
            cmds = _commands()
            changed = _expire(cmds)
            cmd = next((c for c in cmds if c["status"] == "pending"), None)
            if cmd:
                cmd.update(status="running", started_at=_now().isoformat(timespec="seconds"))
                changed = True
            if changed:
                webstore.save(COMMANDS_KEY, cmds)
        return {"command": cmd}

    @app.post("/api/daytrade/commands/{cmd_id}/done")
    def command_done(cmd_id: str, done: CommandDone):
        with _lock:
            cmds = _commands()
            cmd = next((c for c in cmds if c["id"] == cmd_id), None)
            if cmd is None:
                raise HTTPException(404, "找不到這個指令")
            cmd.update(status="done" if done.ok else "failed", message=done.message[:300],
                       finished_at=_now().isoformat(timespec="seconds"))
            webstore.save(COMMANDS_KEY, cmds)
        return cmd

    @app.get("/api/daytrade/macro")
    def get_macro():
        return macro_snapshot()

    @app.get("/api/daytrade")
    def get_daytrade():
        def load(k, default=None):
            try:
                v = webstore.load(k)
                return default if v is None else v
            except Exception as exc:  # noqa: BLE001
                print(f"[daytrade] load {k} failed: {exc}", file=sys.stderr)
                return default

        return {"plan": load("daytrade_plan"), "today": load(f"daytrade_day_{_today()}", {"date": _today(), "events": []}),
                "history": load("daytrade_history", []), "backtest": load("daytrade_backtest"),
                "heartbeat": dict(_heartbeat), "commands": load(COMMANDS_KEY, [])[-10:]}
