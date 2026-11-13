"""LINE Messaging API 推播（取代已停止服務的 LINE Notify）

需要環境變數：
- LINE_CHANNEL_TOKEN：LINE Developers → Messaging API → Channel access token (long-lived)
- LINE_USER_ID：你的 User ID（Basic settings 最下方 "Your user ID"，U 開頭）
"""

import os
import re
import sys

import requests

LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_LIMIT = 5000


def line_enabled() -> bool:
    return bool(os.environ.get("LINE_CHANNEL_TOKEN") and os.environ.get("LINE_USER_ID"))


def _strip_markdown(text: str) -> str:
    # 訊息格式原本寫給 Telegram Markdown；LINE 不支援，去掉 *粗體* 標記
    return re.sub(r"\*([^*\n]+)\*", r"\1", text)


def send_line(text: str):
    text = _strip_markdown(text)
    if len(text) > LINE_TEXT_LIMIT:
        text = text[: LINE_TEXT_LIMIT - 1] + "…"
    r = requests.post(
        LINE_PUSH_API,
        headers={"Authorization": f"Bearer {os.environ['LINE_CHANNEL_TOKEN']}"},
        json={
            "to": os.environ["LINE_USER_ID"],
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )
    if not r.ok:
        print(f"LINE 送失敗: {r.status_code} {r.text}", file=sys.stderr)
