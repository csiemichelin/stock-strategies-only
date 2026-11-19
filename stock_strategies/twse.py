"""證交所與公開資訊觀測站的公開資料——完全不花 FinMind 額度。

  calendar()     交易日與加權指數收盤（FMTQIK，一個月一次請求）
  load_quotes()  每日全市場收盤行情（MI_INDEX，一個交易日一次請求；含當時還在、後來下市的股票）
  exrights()     除權除息計算結果表（TWT49U，一年一次請求）
  revenue()      全體上市公司月營收（公開資訊觀測站 t21sc03，一個月兩次請求）

抓過的都存在 TWSE_CACHE_DIR（預設 .cache/twse）：第一次十年份約三小時（同一個網站兩次請求之間固定間隔，
不對證交所造成負擔），之後每次只補新的交易日。GitHub Actions 上用 actions/cache 保存這個資料夾。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

TZ = timezone(timedelta(hours=8))
BASE = "https://www.twse.com.tw/rwd/zh/"
MOPS = "https://mopsov.twse.com.tw/nas/t21/sii/"
HEADERS = {"User-Agent": "Mozilla/5.0 (stock-strategies lab)"}
PAUSE = float(os.environ.get("TWSE_PAUSE", "3.2"))   # 同一個網站兩次請求的間隔（秒）
RETRY_WAIT = 30.0                                     # 失敗後第一次重試前等多久；之後每次加倍
COMMON = re.compile(r"^[1-9]\d{3}$")                  # 四碼、1～9 開頭：普通股（91 開頭是存託憑證，另外排除）
EXTRA = {"0050"}                                      # 另外保留的 ETF（當比較基準）
COLUMNS = ["date", "id", "open", "high", "low", "close", "volume", "value", "flag"]
FLAG_X = 2                                            # 漲跌欄是 X（不比價）：當天有除權息、減資恢復買賣等參考價調整

_last: dict[str, float] = {}


def _log(msg: str) -> None:
    print(f"[twse] {datetime.now(TZ):%H:%M:%S} {msg}", flush=True)


def _dir() -> Path:
    path = Path(os.environ.get("TWSE_CACHE_DIR", ".cache/twse"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> datetime:
    return datetime.now(TZ)


def _read_json(name: str, default):
    path = _dir() / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(name: str, value) -> None:
    path = _dir() / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _get(url: str, encoding: str = "utf-8", tries: int = 6) -> str:
    """抓一個網址（同一個網站固定間隔；失敗就等久一點再試）。"""
    host = url.split("/")[2]
    err = ""
    for attempt in range(tries):
        wait = _last.get(host, 0.0) + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            _last[host] = time.monotonic()
            if r.status_code == 200 and r.content:
                return r.content.decode(encoding, "replace")
            err = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            _last[host] = time.monotonic()
            err = repr(exc)[:160]
        if attempt < tries - 1:
            pause = min(900.0, RETRY_WAIT * 2 ** attempt)
            _log(f"{host} 沒有正常回應（{err}），{pause:.0f} 秒後再試（{attempt + 1}/{tries - 1}）")
            time.sleep(pause)
    raise RuntimeError(f"抓不到 {url}：{err}")


def _get_json(url: str, tries: int = 6) -> dict:
    """抓 JSON；回來的不是 JSON（被擋下來時會是一頁 HTML）就等一下再試。"""
    for attempt in range(tries):
        text = _get(url)
        try:
            body = json.loads(text)
            if isinstance(body, dict):
                return body
        except ValueError:
            pass
        if attempt < tries - 1:
            pause = min(900.0, RETRY_WAIT * 2 ** attempt)
            _log(f"回應不是預期的格式，{pause:.0f} 秒後再試（{attempt + 1}/{tries - 1}）：{text[:80]!r}")
            time.sleep(pause)
    raise RuntimeError(f"{url} 一直拿不到正確的資料")


def _text(v) -> str:
    return re.sub(r"<[^>]+>|&nbsp;", "", str(v)).strip()


def _num(v) -> float | None:
    try:
        return float(_text(v).replace(",", ""))
    except ValueError:
        return None


def _roc_date(v) -> str | None:
    """民國日期（113/01/02 或 113年01月02日）→ 2024-01-02。"""
    m = re.match(r"\s*(\d{2,3})\D+(\d{1,2})\D+(\d{1,2})", str(v))
    return f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= end[:7]:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _settled(fetched: str | None, after: str) -> bool:
    """這份資料是不是在 after（YYYY-MM-DD）之後才抓的——是的話內容不會再變，不用重抓。"""
    return bool(fetched) and fetched[:10] > after


# ---------------------------------------------------------------- 交易日與加權指數


def calendar(start: str, end: str | None = None) -> list[tuple[str, float]]:
    """start～end 之間的交易日與加權指數收盤：[(YYYY-MM-DD, 指數)]。"""
    end = end or _now().strftime("%Y-%m-%d")
    store = _read_json("calendar.json", {"months": {}, "fetched": {}})
    changed = 0
    for month in _months(start, end):
        last_day = (pd.Timestamp(month + "-01") + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
        if _settled(store["fetched"].get(month), last_day):
            continue
        body = _get_json(f"{BASE}afterTrading/FMTQIK?date={month.replace('-', '')}01&response=json")
        rows = []
        for r in body.get("data") or []:
            day, value = _roc_date(r[0]), _num(r[4])
            if day and value:
                rows.append([day, value])
        store["months"][month] = rows
        store["fetched"][month] = _now().isoformat(timespec="seconds")
        changed += 1
        if changed % 12 == 0:
            _write_json("calendar.json", store)
            _log(f"交易日曆 {month}")
    if changed:
        _write_json("calendar.json", store)
    return [(d, float(v)) for m in sorted(store["months"]) for d, v in store["months"][m] if start <= d <= end]


# ---------------------------------------------------------------- 每日全市場行情


def parse_quotes(body: dict, day: str, names: dict | None = None) -> pd.DataFrame | None:
    """MI_INDEX 的回應 → 當天有成交的普通股（外加 0050）。沒有行情表（假日、還沒公布）回 None。"""
    table = next((t for t in body.get("tables") or []
                  if "證券代號" in (t.get("fields") or []) and "收盤價" in t["fields"]), None)
    if table is None:
        return None
    f = {name: i for i, name in enumerate(table["fields"])}
    rows = []
    for r in table.get("data") or []:
        sid, name = str(r[f["證券代號"]]).strip(), _text(r[f["證券名稱"]])
        if sid not in EXTRA and (not COMMON.match(sid) or sid.startswith("91") or name.endswith("-DR")):
            continue
        o, h, lo, c = (_num(r[f[k]]) for k in ("開盤價", "最高價", "最低價", "收盤價"))
        if not o or not c:
            continue  # 當天沒成交
        sign = _text(r[f["漲跌(+/-)"]])
        flag = FLAG_X if sign.upper() == "X" else 1 if sign == "+" else -1 if sign == "-" else 0
        rows.append((day, sid, o, h or max(o, c), lo or min(o, c), c, int(_num(r[f["成交股數"]]) or 0),
                     float(_num(r[f["成交金額"]]) or 0.0), flag))
        if names is not None:
            names[sid] = name
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=COLUMNS)
    return df.astype({"volume": "int64", "flag": "int8"})


def quotes_of(day: str, names: dict | None = None) -> pd.DataFrame | None:
    body = _get_json(f"{BASE}afterTrading/MI_INDEX?date={day.replace('-', '')}&type=ALLBUT0999&response=json")
    return parse_quotes(body, day, names)


def names() -> dict:
    return _read_json("names.json", {})


def _merge_year(path: Path, have: pd.DataFrame, new: list[pd.DataFrame], known: dict) -> pd.DataFrame:
    if not new:
        return have
    have = pd.concat([have, *new] if len(have) else new, ignore_index=True)
    tmp = path.with_suffix(".tmp")
    have.to_parquet(tmp, index=False)
    tmp.replace(path)
    _write_json("names.json", known)
    return have


def load_quotes(days: list[str]) -> pd.DataFrame:
    """這些交易日的全市場行情（長表）。沒抓過的才去抓；每 40 天存一次，中斷了下次接著抓。"""
    known = names()
    years = sorted({d[:4] for d in days})
    stored, todo = {}, {}
    for year in years:
        path = _dir() / f"quotes_{year}.parquet"
        stored[year] = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
        done = set(stored[year]["date"].unique())
        todo[year] = [d for d in days if d[:4] == year and d not in done]
    total = sum(len(v) for v in todo.values())
    if total:
        _log(f"要補 {total} 個交易日的全市場行情，約 {total * (PAUSE + 0.8) / 60:.0f} 分鐘")
    frames, fetched, t0 = [], 0, time.time()
    for year in years:
        path = _dir() / f"quotes_{year}.parquet"
        have, new = stored.pop(year), []
        try:
            for n, day in enumerate(todo[year], 1):
                df = quotes_of(day, known)
                fetched += 1
                if df is None:
                    _log(f"{day} 還沒有行情（尚未公布或當天休市）")
                else:
                    new.append(df)
                if n % 40 == 0:
                    have, new = _merge_year(path, have, new, known), []
                    left = (total - fetched) * (time.time() - t0) / fetched / 60
                    _log(f"行情 {day}（{fetched}/{total}，約再 {left:.0f} 分鐘）")
        finally:
            have, new = _merge_year(path, have, new, known), []
        want = {d for d in days if d[:4] == year}
        frames.append(have[have["date"].isin(want)])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    return out.sort_values(["date", "id"]).reset_index(drop=True)


# ---------------------------------------------------------------- 除權息


def exrights(start: str, end: str | None = None) -> dict[str, list[tuple[str, float]]]:
    """除權除息計算結果：{代號: [(除權息日, 參考價 ÷ 前一日收盤)]}（含現金增資的除權）。"""
    end = end or _now().strftime("%Y-%m-%d")
    store = _read_json("exrights.json", {"years": {}, "fetched": {}})
    changed = False
    for year in range(int(start[:4]), int(end[:4]) + 1):
        key = str(year)
        if _settled(store["fetched"].get(key), f"{year}-12-31"):
            continue
        last = min(end, f"{year}-12-31").replace("-", "")
        body = _get_json(f"{BASE}exRight/TWT49U?startDate={year}0101&endDate={last}&response=json")
        rows = []
        for r in body.get("data") or []:
            day, sid, before, ref = _roc_date(r[0]), str(r[1]).strip(), _num(r[3]), _num(r[4])
            if day and before and ref and (COMMON.match(sid) or sid in EXTRA):
                rows.append([day, sid, before, ref])
        store["years"][key] = rows
        store["fetched"][key] = _now().isoformat(timespec="seconds")
        changed = True
    if changed:
        _write_json("exrights.json", store)
    out: dict[str, list[tuple[str, float]]] = {}
    for key in sorted(store["years"]):
        for day, sid, before, ref in store["years"][key]:
            if start <= day <= end:
                out.setdefault(sid, []).append((day, ref / before))
    return out


# ---------------------------------------------------------------- 月營收


def parse_revenue(html: str) -> dict[str, float]:
    """公開資訊觀測站月營收彙總表 → {代號: 當月營收（千元）}。"""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        if len(cells) >= 5 and re.fullmatch(r"\d{4}", cells[0]):
            value = _num(cells[2])
            if value is not None:
                out[cells[0]] = value
    return out


def revenue(start_month: str, end_month: str | None = None) -> dict[str, dict[str, float]]:
    """全體上市公司月營收：{代號: {YYYY-MM: 當月營收（千元）}}；end_month 預設是上個月。"""
    now = _now()
    end_month = end_month or (pd.Timestamp(now.strftime("%Y-%m-01")) - pd.Timedelta(days=1)).strftime("%Y-%m")
    store = _read_json("revenue.json", {"months": {}, "fetched": {}})
    changed = 0
    for month in _months(start_month + "-01", end_month + "-01"):
        # 法定公布期限是次月 10 日；再多留幾天給晚申報的，之後就當作定案
        final_after = (pd.Timestamp(month + "-01") + pd.offsets.MonthEnd(0) + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
        if _settled(store["fetched"].get(month), final_after):
            continue
        roc, m = int(month[:4]) - 1911, int(month[5:7])
        rows: dict[str, float] = {}
        for kind in (0, 1):  # 0 國內公司、1 第一上市的外國公司（KY）
            try:
                rows.update(parse_revenue(_get(f"{MOPS}t21sc03_{roc}_{m}_{kind}.html", encoding="big5", tries=4)))
            except RuntimeError as exc:
                _log(f"{month} 月營收（{'KY' if kind else '國內'}）抓不到：{exc}")
        if rows or month not in store["months"]:
            store["months"][month] = rows
        if rows:
            store["fetched"][month] = now.isoformat(timespec="seconds")
        changed += 1
        if changed % 12 == 0:
            _write_json("revenue.json", store)
            _log(f"月營收 {month}（{len(rows)} 家）")
    if changed:
        _write_json("revenue.json", store)
    out: dict[str, dict[str, float]] = {}
    for month in sorted(store["months"]):
        if start_month <= month <= end_month:
            for sid, value in store["months"][month].items():
                out.setdefault(sid, {})[month] = float(value)
    return out


# ---------------------------------------------------------------- 一次抓齊


def download(years: int = 10) -> dict:
    """把 years 年份的資料抓齊（已經有的不會重抓），回傳各項筆數。"""
    start = (_now() - timedelta(days=365 * years + 60)).strftime("%Y-%m-%d")
    cal = calendar(start)
    _log(f"交易日 {len(cal)} 天（{cal[0][0]}～{cal[-1][0]}）")
    ex = exrights(start)
    _log(f"除權息 {sum(len(v) for v in ex.values())} 筆、{len(ex)} 檔")
    rev = revenue((pd.Timestamp(start) - pd.Timedelta(days=400)).strftime("%Y-%m"))
    _log(f"月營收 {len(rev)} 家、{len({m for v in rev.values() for m in v})} 個月")
    q = load_quotes([d for d, _ in cal])
    _log(f"行情 {q['date'].nunique()} 天、{q['id'].nunique()} 檔、{len(q)} 列")
    return {"days": len(cal), "quote_days": int(q["date"].nunique()), "stocks": int(q["id"].nunique()),
            "exrights": sum(len(v) for v in ex.values()), "revenue_companies": len(rev)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="抓證交所／公開資訊觀測站的歷史資料（不花 FinMind 額度）")
    ap.add_argument("--years", type=int, default=10)
    args = ap.parse_args(argv)
    download(args.years)
    return 0


if __name__ == "__main__":
    sys.exit(main())
