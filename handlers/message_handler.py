# handlers/message_handler.py — LINE Webhook Orchestrator（整合版）
#
# 設計原則：
#   - reply 先行（不依賴任何 IO，防 LINE 30s timeout）
#   - background thread 處理所有 IO（Drive + Sheets）
#   - worker_lock 串行化 background 工作（防 API throttling）
#   - dedup atomic（is_duplicate_and_mark）防 LINE retry 重複寫入
#
# 支援：
#   text  → 特殊指令（#幫助 / #查詢）或 Gemini 一般助理
#   image → Pillow 壓縮 → Drive 上傳
#   video → 方案 A 串流上傳 Drive（峰值記憶體 ~5MB）

import os
import threading
import logging
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, TextMessage,
)

from storage.event_schema import TextEvent, MediaEvent, make_timestamp, make_event_id
from services import sheets_service, drive_service, gemini_service

_log         = logging.getLogger(__name__)
_config      = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
_worker_lock = threading.Lock()   # 串行化 background IO，防 API throttling


# ── 主要 handler ─────────────────────────────────────────────

def handle_text(event) -> None:
    ts       = make_timestamp()
    user_id  = event.source.user_id
    text     = event.message.text.strip()
    event_id = make_event_id(ts, user_id)

    if sheets_service.is_duplicate_and_mark(event_id):
        _log.info("dedup skip text: %s", event_id)
        return

    # ── 特殊指令 ────────────────────────────────────────────
    if text == "#幫助":
        reply = (
            "【小暖使用說明】\n\n"
            "✉️ 直接傳文字 → 我會回覆並記錄\n"
            "📷 傳照片 → 壓縮後存入雲端硬碟\n"
            "🎬 傳影片 → 串流存入雲端硬碟\n"
            "#查詢 → 最近 10 筆對話紀錄\n"
            "#幫助 → 這份說明"
        )
        _reply(event.reply_token, reply)
        ev = TextEvent.from_user(user_id, text, ts)
        ev.ai_response = reply
        ev.status      = "ok"
        threading.Thread(target=_save_text, args=(ev,), daemon=True).start()
        return

    if text == "#查詢":
        history = sheets_service.get_recent_text_history(user_id, 10)
        if not history:
            reply = "還沒有對話紀錄喔，先跟我說說話吧！💙"
        else:
            lines = []
            for item in history:
                # timestamp 格式 YYYYMMDD_HHMMSS → 取 HH:MM
                t = item["time"][9:13]               # "HHMMSS" 取前 4 碼
                t = f"{t[:2]}:{t[2:]}"               # → "HH:MM"
                c = item["content"][:20]
                c += "…" if len(item["content"]) > 20 else ""
                lines.append(f"[{t}] {c}")
            reply = "【最近 10 筆對話】\n" + "\n".join(lines)
        _reply(event.reply_token, reply)
        ev = TextEvent.from_user(user_id, text, ts)
        ev.ai_response = reply
        ev.status      = "ok"
        threading.Thread(target=_save_text, args=(ev,), daemon=True).start()
        return

    # ── 一般文字：Gemini 一般助理（同步取得回覆再 reply）───
    ai_response = gemini_service.generate_reply(text)
    _reply(event.reply_token, ai_response)

    ev = TextEvent.from_user(user_id, text, ts)
    ev.ai_response = ai_response
    ev.status      = "ok"
    threading.Thread(target=_save_text, args=(ev,), daemon=True).start()


def handle_image(event) -> None:
    ts       = make_timestamp()
    user_id  = event.source.user_id
    msg_id   = event.message.id
    event_id = make_event_id(ts, user_id)

    if sheets_service.is_duplicate_and_mark(event_id):
        _log.info("dedup skip image: %s", event_id)
        return

    ev = MediaEvent.from_line(user_id, "image", ts)
    _reply(event.reply_token, "📷 收到照片，正在儲存…")
    threading.Thread(target=_process_image, args=(ev, msg_id), daemon=True).start()


def handle_video(event) -> None:
    ts       = make_timestamp()
    user_id  = event.source.user_id
    msg_id   = event.message.id
    event_id = make_event_id(ts, user_id)

    if sheets_service.is_duplicate_and_mark(event_id):
        _log.info("dedup skip video: %s", event_id)
        return

    ev = MediaEvent.from_line(user_id, "video", ts)
    _reply(event.reply_token, "🎬 收到影片，串流儲存中（稍後完成）…")
    threading.Thread(target=_process_video, args=(ev, msg_id), daemon=True).start()


# ── Background workers（worker_lock 串行）───────────────────

def _save_text(ev: TextEvent) -> None:
    with _worker_lock:
        sheets_service.append_text_event(ev)


def _process_image(ev: MediaEvent, message_id: str) -> None:
    with _worker_lock:
        raw               = drive_service.download_line_content(message_id)
        filename          = drive_service.make_filename(ev.timestamp, message_id, "jpg")
        ev.drive_url, ev.status = drive_service.compress_and_upload(raw, filename)
        sheets_service.append_media_event(ev)


def _process_video(ev: MediaEvent, message_id: str) -> None:
    with _worker_lock:
        filename          = drive_service.make_filename(ev.timestamp, message_id, "mp4")
        ev.drive_url, ev.status = drive_service.stream_and_upload_video(message_id, filename)
        sheets_service.append_media_event(ev)


# ── 工具 ─────────────────────────────────────────────────────

def _reply(reply_token: str, text: str) -> None:
    try:
        with ApiClient(_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token = reply_token,
                    messages    = [TextMessage(text=text)],
                )
            )
    except Exception as e:
        _log.error("reply failed: %s", e)
