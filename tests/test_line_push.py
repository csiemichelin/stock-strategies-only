from stock_strategies import line_push, notify


def test_strip_markdown():
    assert line_push._strip_markdown("📊 *V3.0 報告* 與 *2330 台積電*") == "📊 V3.0 報告 與 2330 台積電"


def test_send_dispatches_to_line(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_TOKEN", "t")
    monkeypatch.setenv("LINE_USER_ID", "U1")
    calls = []

    class R:
        ok = True

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return R()

    monkeypatch.setattr(line_push.requests, "post", fake_post)
    notify.send_telegram("*hi*")
    url, headers, body = calls[0]
    assert url == line_push.LINE_PUSH_API
    assert headers["Authorization"] == "Bearer t"
    assert body == {"to": "U1", "messages": [{"type": "text", "text": "hi"}]}
