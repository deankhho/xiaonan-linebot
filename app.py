# app.py — 主程式入口（整合版）
# LINE Bot Webhook + /healthz（含子系統狀態 + 5 分鐘 TTL cache）
# 支援：text（Gemini + #指令）| image | video

import logging
import os
import time

from flask import Flask, request, abort, jsonify
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
    VideoMessageContent,
)

from handlers.message_handler import handle_text, handle_image, handle_video
from services import sheets_service, drive_service, gemini_service

# ── Logging（所有模組共用）──────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s %(levelname)s %(name)s %(message)s",
)
_log = logging.getLogger(__name__)

# ── Flask + LINE Webhook ─────────────────────────────────────────
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


@handler.add(MessageEvent, message=VideoMessageContent)
def on_video(event):
    handle_video(event)


# ── 健康檢查（含 5 分鐘 TTL cache，避免打爆 API quota）────────────
_healthz_cache: dict = {}
_HEALTHZ_TTL = 300   # 5 分鐘


@app.route("/healthz")
def healthz():
    """
    回傳各子系統狀態：
    {
      "status": "ok" | "degraded",
      "sheets": "ok" | "error",
      "drive":  "ok" | "error",
      "gemini": "ok" | "error",
      "cached": true | false
    }
    真正呼叫 API 最多每 5 分鐘一次，平時回傳 cache。
    """
    now = time.time()
    if now - _healthz_cache.get("ts", 0) < _HEALTHZ_TTL:
        # 回傳快取結果
        result = dict(_healthz_cache)
        result["cached"] = True
        return jsonify(result)

    # 實際檢查（更新 cache）
    sheets_ok = sheets_service.health_check()
    drive_ok  = drive_service.health_check()
    gemini_ok = gemini_service.health_check()

    _healthz_cache.update({
        "ts":     now,
        "sheets": "ok" if sheets_ok else "error",
        "drive":  "ok" if drive_ok  else "error",
        "gemini": "ok" if gemini_ok else "error",
        "status": "ok" if (sheets_ok and drive_ok and gemini_ok) else "degraded",
    })

    result = {k: v for k, v in _healthz_cache.items() if k != "ts"}
    result["cached"] = False
    return jsonify(result)


# ── 啟動時預熱 dedup cache ────────────────────────────────────────
def _on_startup() -> None:
    _log.info("loading recent event ids for dedup cache...")
    sheets_service.load_recent_ids(500)


with app.app_context():
    _on_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    _log.info("starting on port %d", port)
    app.run(host="0.0.0.0", port=port)
