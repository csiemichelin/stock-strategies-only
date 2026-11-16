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


def _market_line(market: dict | None) -> str:
    if not market:
        return "大盤：資料暫缺"
    if market.get("bullish", True):
        return "大盤：🟢 站上月線，可依訊號操作"
    return "大盤：🔴 跌破月線，先別進場（買進訊號已降為觀察）"


def format_daily_brief(signals: list[dict], market: dict | None = None, night_note: str | None = None) -> str:
    today = datetime.now()
    wd = "一二三四五六日"[today.weekday()]
    buys = [s for s in signals if s.get("action") == "BUY"]
    watches = [s for s in signals if s.get("action") == "WATCH"]

    lines = [f"📊 {today:%m/%d}（{wd}）收盤選股", _market_line(market)]
    if night_note and "大跌" in night_note:
        lines.append("夜盤：🔴 昨晚大跌，買進訊號已保守處理")
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
