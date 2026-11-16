"""LINE 精簡版訊息：每日選股 1 則、盤前 1 則，只留「能不能買、買哪檔、價格」。

完整版（多則、含類股/量價解析）仍保留給 Telegram；LINE 可用環境變數
LINE_STYLE=full 切回完整版。
"""

from __future__ import annotations

from datetime import datetime

MAX_BUY = 5
MAX_WATCH = 5
DISCLAIMER = "（系統自動分析，僅供參考）"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return f"{f:,.2f}".rstrip("0").rstrip(".")


def _index_moves() -> tuple[float | None, float | None]:
    """加權指數今日、近 5 日漲跌幅（%）；取不到回 None。沿用 market.py 的快取資料。"""
    try:
        from .datasources import get_index_history

        close = get_index_history("TAIEX")["close"].astype(float)
        if len(close) < 6:
            return None, None
        return (close.iloc[-1] / close.iloc[-2] - 1) * 100, (close.iloc[-1] / close.iloc[-6] - 1) * 100
    except Exception:
        return None, None


def _market_lines(market: dict | None) -> list[str]:
    if not market or market.get("close") is None:
        return ["大盤：資料暫缺"]
    close, ma = float(market["close"]), float(market.get("ma20") or 0)
    bullish = market.get("bullish", True)
    day, week = _index_moves()
    moves = "｜".join(x for x in (
        f"今日 {day:+.2f}%" if day is not None else "",
        f"5 日 {week:+.2f}%" if week is not None else "",
    ) if x)
    head = f"大盤：{'🟢' if bullish else '🔴'} 加權 {close:,.0f}" + (f"（{moves}）" if moves else "")
    lines = [head]
    if ma:
        gap = (close / ma - 1) * 100
        verdict = "可依訊號操作" if bullish else "先別進場，買進訊號已降為觀察"
        lines.append(f"　月線 {ma:,.0f}，{'站上' if bullish else '跌破'} {gap:+.1f}% → {verdict}")
    return lines


def _pool_line(signals: list[dict]) -> str | None:
    trends = [s["trend"] for s in signals if s.get("trend")]
    if not trends:
        return None
    up = sum(t.get("chg_5d", 0) > 0 for t in trends)
    above = sum(bool(t.get("above_ma20")) for t in trends)
    avg = sum(t.get("chg_5d", 0) for t in trends) / len(trends)
    return f"觀察池 {len(trends)} 檔：5 日上漲 {up}｜站上月線 {above}｜平均 {avg:+.1f}%"


def _night_pct(night_note: str | None) -> str | None:
    import re

    m = re.search(r"夜盤 ([+-]\d+(?:\.\d+)?%)", night_note or "")
    return m.group(1) if m else None


def format_daily_brief(
    signals: list[dict], market: dict | None = None, night_note: str | None = None, header: str | None = None
) -> str:
    today = datetime.now()
    wd = "一二三四五六日"[today.weekday()]
    buys = [s for s in signals if s.get("action") == "BUY"]
    watches = [s for s in signals if s.get("action") == "WATCH"]

    lines = [header or f"📊 {today:%m/%d}（{wd}）收盤選股", *_market_lines(market)]
    pool = _pool_line(signals)
    if pool:
        lines.append(pool)
    night = _night_pct(night_note)
    if night:
        lines.append(f"昨晚夜盤 {night}" + ("，買進訊號已保守處理" if "降為 WATCH" in (night_note or "") else ""))
    lines.append("")

    if buys:
        lines.append(f"🟢 明天可買（{len(buys)} 檔）")
        for s in buys[:MAX_BUY]:
            lines.append(f"{s['stock_id']} {s.get('name', '')}　{s.get('signal_score', '')} 分")
            lines.append(
                f"　買 {_num(s.get('entry_price'))}｜停損 {_num(s.get('stop_loss_price'))}"
                f"｜停利 {_num(s.get('target_price'))}"
            )
        if len(buys) > MAX_BUY:
            lines.append(f"…另有 {len(buys) - MAX_BUY} 檔，見 Google Sheet")
        lines.append("　開盤價進場，最多抱 20 個交易日")
    else:
        lines.append("🟢 明天可買：無")

    if watches:
        top = "、".join(f"{s['stock_id']} {s.get('name', '')}" for s in watches[:MAX_WATCH])
        lines.append("")
        lines.append(f"🟡 觀察（接近買點）：{top}")

    if header is None:  # 每日排程那則才附模擬帳戶
        from .sim import LAST_SUMMARY

        if LAST_SUMMARY:
            lines.append("")
            lines.extend(LAST_SUMMARY)

    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_premarket_brief(night: dict | None, signals: list[dict]) -> str:
    from .night_session import tailwind_tag

    today = datetime.now()
    wd = "一二三四五六日"[today.weekday()]
    lines = [f"🌙 {today:%m/%d}（{wd}）盤前"]
    if night:
        lines.append(f"昨晚夜盤 {night['pct']:+.2f}% → 預估{night['label']}開出")
    else:
        lines.append("夜盤資料暫缺，照昨天名單操作")

    actionable = [s for s in signals if str(s.get("action", "")).upper() in ("BUY", "WATCH")]
    if actionable:
        latest_day = actionable[0].get("date", "")
        batch = [s for s in actionable if s.get("date", "") == latest_day]
        buys = [s for s in batch if str(s["action"]).upper() == "BUY"]
        lines.append("")
        if buys:
            tag = tailwind_tag(night["bias"] if night else "flat")
            lines.append(f"🟢 今天可買（{tag}）")
            for s in buys[:MAX_BUY]:
                lines.append(
                    f"{s.get('stock_id', '')} {s.get('name', '')}　停損 {_num(s.get('stop_loss_price'))}"
                    f"｜停利 {_num(s.get('target_price'))}"
                )
            if night and night.get("bias") == "bear":
                lines.append("　夜盤偏空，建議等開盤穩住再進")
        else:
            lines.append("🟢 今天可買：無（昨天沒有買進訊號）")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
