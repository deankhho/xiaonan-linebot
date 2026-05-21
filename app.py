# app.py — 主程式入口（定稿版）
# LINE Bot Webhook 接收 + /healthz 健康檢查
# Phase 1：只處理 text / image，video 不支援

import logging
import os

from flask import Flask, request, abort, jsonify
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent

from handlers.message_handler import handle_text, handle_image

# ── Logging 設定（所有模組共用）────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
_log = logging.getLogger(__name__)

# ── Flask + LINE Webhook ────────────────────────────────────────
app     = Flask(__name__)
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        _log.warning("invalid LINE signature")
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    handle_text(event)


@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event):
    handle_image(event)


# ── 健康檢查（Render / Railway 用）──────────────────────────────
@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    _log.info("starting on port %d", port)
    app.run(host="0.0.0.0", port=port)
