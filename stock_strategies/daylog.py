"""回測逐日紀錄（Google Sheet「BacktestLog」分頁；沒設 Sheet 時存本機 .cache/webstore/backtest_log.jsonl）。

回測／模擬回放算過的交易日都記下來：同一組策略參數之後再回測，算過的日子直接拿紀錄逐日模擬
（不用 FinMind、幾秒就好），只有還沒算過的新交易日才下載資料計算。換開始日、換本金也照樣沿用，
因為記的是「每天的訊號和 K 棒」，不是某一次回測的結果。每日排程（GitHub Actions）跑完選股後也會
順手把最新一天補進最近用過的策略，所以網頁回測通常一天都不用算。

紀錄只增不改，同一天重複寫入時讀取端合併（後寫的為準）。每列：kind、key、date、payload(JSON)、written_at
  sig   key=策略參數雜湊  date=交易日  {"buys": [當天收盤後的 BUY 訊號], "n": 有 K 棒的檔數}
  bars  key=""           date=交易日  {股票代號: [開, 高, 低, 收]}（所有策略共用）
  meta  key=策略參數雜湊  date=使用日  {"id", "name", "params"}（每日排程靠這個知道要補哪些策略）

每組參數記錄過的交易日一定是連續的一段（計算順序保證：先補中間、再往後、最後才往前），
所以「紀錄夠新、開始日在紀錄範圍內」時可以完全不碰 FinMind（連大盤指數都不用抓）。

key 包含合併後的全部參數與判斷邏輯原始碼的雜湊：改了參數或選股邏輯，舊紀錄自然不再沿用。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TAB = "BacktestLog"
HEADER = ["kind", "key", "date", "payload", "written_at"]
KINDS = ("sig", "bars", "meta")
CELL_LIMIT = 45_000  # Google Sheet 單格上限 5 萬字，留點餘裕
LOG_VERSION = "1"  # 回放規則改了（例如訊號欄位、完整日判斷）就加一，舊紀錄全部重算
LOGIC_FILES = ("evaluate.py", "indicators.py", "volume.py", "backtest.py", "market.py")
SIG_FIELDS = ("stock_id", "name", "signal_score", "position_size_pct", "entry_price", "stop_loss_price",
              "target_price")
TTL = 30
TZ = timezone(timedelta(hours=8))

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "log": None}
_logic_hash: str | None = None


def _use_sheet() -> bool:
    return bool(os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_CREDS_JSON"))


def _local_path() -> Path:
    root = Path(os.environ.get("WEBSTORE_DIR", Path(__file__).resolve().parent.parent / ".cache" / "webstore"))
    return root / "backtest_log.jsonl"


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- key


def logic_version() -> str:
    """選股判斷邏輯的原始碼雜湊（統一換行，Windows／Linux 一樣）；程式改了舊紀錄就不沿用。"""
    global _logic_hash
    if _logic_hash is None:
        h = hashlib.sha1(LOG_VERSION.encode())
        base = Path(__file__).resolve().parent
        for name in LOGIC_FILES:
            p = base / name
            if p.exists():
                h.update(name.encode() + p.read_bytes().replace(b"\r\n", b"\n"))
        _logic_hash = h.hexdigest()[:8]
    return _logic_hash


def _norm(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return float(v)  # 20 和 20.0 視為同一個參數


def strategy_key(strategy: dict | None) -> str:
    from .loader import merge_params

    params = {k: _norm(v) for k, v in merge_params(strategy).items()}
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False) + "|" + logic_version()
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 紀錄


class Log:
    def __init__(self):
        self.sig: dict[str, dict[str, dict]] = {}
        self.bars: dict[str, dict[str, list]] = {}
        self.meta: dict[str, dict] = {}

    def add(self, kind: str, key: str, day: str, payload) -> None:
        if kind == "sig" and isinstance(payload, dict):
            self.sig.setdefault(key, {})[day] = payload
        elif kind == "bars" and isinstance(payload, dict):
            self.bars.setdefault(day, {}).update(payload)
        elif kind == "meta" and isinstance(payload, dict):
            if key not in self.meta or day >= self.meta[key].get("date", ""):
                self.meta[key] = {**payload, "date": day}

    def done(self, key: str) -> dict[str, dict]:
        """這組參數已經記錄、而且有當天 K 棒的交易日。"""
        return {d: v for d, v in self.sig.get(key, {}).items() if d in self.bars}

    def servable(self, key: str, start_date: str, now: datetime | None = None) -> list[str] | None:
        """不抓任何資料就能回測的話，回傳要用的交易日；不行（沒紀錄、開始日比紀錄早、紀錄落後超過一個交易日）回 None。"""
        done = self.done(key)
        if not done:
            return None
        lo, hi = min(done), max(done)
        if start_date > hi or (start_date < lo and not _only_weekend_between(start_date, lo)):
            return None
        if _weekdays_between(hi, latest_expected_day(now)) > 1:
            return None
        return sorted(d for d in done if d >= start_date)


def _only_weekend_between(start: str, end: str) -> bool:
    """[start, end) 之間是不是只有六日（開始日選在週末時，第一個交易日就是下週一）。"""
    d, stop = date.fromisoformat(start), date.fromisoformat(end)
    if (stop - d).days > 3:
        return False
    while d < stop:
        if d.weekday() < 5:
            return False
        d += timedelta(days=1)
    return True


def _weekdays_between(after: str, until: str) -> int:
    """(after, until] 之間有幾個平日。"""
    d, stop, n = date.fromisoformat(after), date.fromisoformat(until), 0
    while d < stop:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def _parse(rows) -> Log:
    log = Log()
    for r in rows:
        if len(r) < 4 or r[0] not in KINDS:
            continue
        try:
            payload = json.loads(r[3])
        except (TypeError, ValueError):
            continue
        log.add(r[0], str(r[1]), str(r[2])[:10], payload)
    return log


def _worksheet():
    import gspread

    from .sheet import get_gsheet

    sh = get_gsheet()
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB, rows=100, cols=len(HEADER))
        ws.update(range_name="A1:E1", values=[HEADER], value_input_option="RAW")
        return ws


def _read_rows() -> list[list]:
    if _use_sheet():
        return _worksheet().get_all_values()[1:]
    p = _local_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def load(fresh: bool = False) -> Log:
    with _lock:
        if not fresh and _cache["log"] is not None and time.monotonic() - _cache["at"] < TTL:
            return _cache["log"]
    log = _parse(_read_rows())
    with _lock:
        _cache.update(at=time.monotonic(), log=log)
    return log


def forget() -> None:
    """放掉記憶體裡的紀錄（API 主行程只用來估額度，不必一直留著）。"""
    with _lock:
        _cache.update(at=0.0, log=None)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _encode(kind: str, key: str, day: str, payload, stamp: str) -> list[list[str]]:
    """一格放不下的 dict 拆成好幾列（讀取時合併）。"""
    text = _dump(payload)
    if len(text) <= CELL_LIMIT or not isinstance(payload, dict):
        return [[kind, key, day, text, stamp]]
    rows, part, size = [], {}, 2
    for k, v in payload.items():
        item = len(_dump({k: v}))
        if part and size + item > CELL_LIMIT:
            rows.append([kind, key, day, _dump(part), stamp])
            part, size = {}, 2
        part[k] = v
        size += item
    if part:
        rows.append([kind, key, day, _dump(part), stamp])
    return rows


def append(entries: list[tuple[str, str, str, object]]) -> None:
    """entries：(kind, key, date, payload)。"""
    stamp = datetime.now(TZ).isoformat(timespec="seconds")
    rows = [row for kind, key, day, payload in entries for row in _encode(kind, key, day, payload, stamp)]
    if not rows:
        return
    if _use_sheet():
        _worksheet().append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
    else:
        p = _local_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with _lock:
        log = _cache["log"]
        if log is not None:
            for row in rows:
                log.add(row[0], row[1], row[2], json.loads(row[3]))


def touch(strategy: dict) -> None:
    """記下「今天用過這組參數」（一天一次），每日排程據此決定要幫哪些策略補最新一天。"""
    from .loader import merge_params

    key, day = strategy_key(strategy), today()
    if load().meta.get(key, {}).get("date") == day:
        return
    append([("meta", key, day, {"id": strategy.get("id", "default"), "name": strategy.get("name", ""),
                                "params": merge_params(strategy)})])


def recent_strategies(log: Log, max_age_days: int = 30, limit: int = 5) -> list[dict]:
    """最近用過、而且有紀錄的策略（新到舊），參數照當時記下的。"""
    cutoff = (datetime.now(TZ).date() - timedelta(days=max_age_days)).isoformat()
    items = sorted(((m.get("date", ""), k, m) for k, m in log.meta.items()
                    if m.get("date", "") >= cutoff and log.sig.get(k)), reverse=True)
    return [{"id": m.get("id", "default"), "name": m.get("name", ""), "params": m.get("params", {})}
            for _, _k, m in items[:limit]]


def latest_expected_day(now: datetime | None = None) -> str:
    """推估最新一個「應該有資料」的交易日：平日 15 點後算今天，否則往前找平日（不含國定假日）。"""
    now = now or datetime.now(TZ)
    d = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def needs_data(strategy: dict, start_date: str, log: Log | None = None) -> bool:
    """這組參數從 start_date 回測要不要再抓資料：紀錄夠用就不用（最新一天沒有的話留給每日排程補）。"""
    log = log or load()
    return log.servable(strategy_key(strategy), start_date) is None
