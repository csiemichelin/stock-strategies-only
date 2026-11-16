from stock_strategies import notify
from stock_strategies.line_brief import format_daily_brief, format_premarket_brief


def _sig(sid, name, action, score, entry=100.0):
    return {"stock_id": sid, "name": name, "action": action, "signal_score": score,
            "entry_price": entry, "stop_loss_price": round(entry * 0.92, 2), "target_price": round(entry * 1.1, 2)}


def test_daily_brief_with_buys():
    sigs = [_sig("2308", "台達電", "BUY", 72, 1660), _sig("2330", "台積電", "WATCH", 64, 1450), _sig("1101", "台泥", "SKIP", 30)]
    msg = format_daily_brief(sigs, market={"bullish": True})
    assert "明天可買（1 檔）" in msg and "2308 台達電" in msg
    assert "買 1,660｜停損 1,527.2｜停利 1,826" in msg
    assert "觀察（接近買點）：2330 台積電" in msg
    assert "1101" not in msg
    assert len(msg.splitlines()) <= 15


def test_daily_brief_bearish_no_buy():
    msg = format_daily_brief([_sig("2330", "台積電", "WATCH", 64)], market={"bullish": False})
    assert "跌破月線" in msg and "明天可買：無" in msg


def test_premarket_brief():
    night = {"pct": -1.8, "label": "大跌", "bias": "bear"}
    sigs = [{"date": "2026-11-16", "action": "BUY", "stock_id": "2308", "name": "台達電",
             "stop_loss_price": 1527.2, "target_price": 1826}]
    msg = format_premarket_brief(night, sigs)
    assert "昨晚夜盤 -1.80% → 預估大跌開出" in msg
    assert "2308 台達電" in msg and "等開盤穩住" in msg


def test_line_uses_brief_by_default(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_TOKEN", "t")
    monkeypatch.setenv("LINE_USER_ID", "U")
    monkeypatch.delenv("LINE_STYLE", raising=False)
    assert len(notify.format_messages([_sig("2308", "台達電", "BUY", 72)], market={"bullish": True})) == 1
    monkeypatch.setenv("LINE_STYLE", "full")
    assert len(notify.format_messages([], market={"bullish": True})) > 1


def test_strip_underscore_italic():
    from stock_strategies.line_push import _strip_markdown
    assert _strip_markdown("_僅供參考_ LINE_USER_ID") == "僅供參考 LINE_USER_ID"
