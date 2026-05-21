# handlers/message_handler.py — LINE Webhook Orchestrator（Cloud-native）
#
# 設計原則：
#   - reply 先行（最快，不依賴任何 IO），reply 失敗只 log 不中斷
#   - background thread 處理所有 IO（Drive + Sheets）
#   - worker_lock 串行化 background 工作（防 API throttling）
#   - dedup atomic（is_duplicate_and_mark）防 LINE retry 重複寫入
#
# Phase 1：text + image（video 不支援）

import os
import threading
import logging
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, TextMessage,
)

from storage.event_schema import LifeEvent, make_timestamp
from services import sheets_service, drive_service

_log         = logging.getLogger(__name__)
_config      = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
_worker_lock = threading.Lock()   # 串行化 background IO，防 burst throttling


# ── 主要 handler ─────────────────────────────────────────────

def handle_text(event) -> None:
    ts       = make_timestamp()
    user_id  = event.source.user_id
    event_id = f"{ts}_{user_id[-6:]}"

    if sheets_service.is_duplicate_and_mark(event_id):
        _log.info("dedup skip text: %s", event_id)
        return

    ev = LifeEvent.from_text(user_id, event.message.text, ts)

    try:
        _reply(event.reply_token, "✅ 已記錄")
    except Exception as e:
        _log.error("reply failed: %s", e)

    threading.Thread(target=_process_text, args=(ev,), daemon=True).start()


def handle_image(event) -> None:
    ts       = make_timestamp()
    user_id  = event.source.user_id
    msg_id   = event.message.id
    event_id = f"{ts}_{user_id[-6:]}"

    if sheets_service.is_duplicate_and_mark(event_id):
        _log.info("dedup skip image: %s", event_id)
        return

    ev = LifeEvent.from_image(user_id, ts)

    try:
        _reply(event.reply_token, "📷 已記錄")
    except Exception as e:
        _log.error("reply failed: %s", e)

    threading.Thread(target=_process_image, args=(ev, msg_id), daemon=True).start()


# ── Background worker（串行）────────────────────────────────

def _process_text(ev: LifeEvent) -> None:
    with _worker_lock:
        ev.status = "ok"
        sheets_service.append_event(ev)


def _process_image(ev: LifeEvent, message_id: str) -> None:
    with _worker_lock:
        raw            = drive_service.download_line_content(message_id)
        filename       = drive_service.make_filename(ev.timestamp, message_id, "jpg")
        ev.drive_url, ev.status = drive_service.compress_and_upload(raw, filename)
        sheets_service.append_event(ev)


# ── 工具 ─────────────────────────────────────────────────────

def _reply(reply_token: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token = reply_token,
                messages    = [TextMessage(text=text)],
            )
        )
