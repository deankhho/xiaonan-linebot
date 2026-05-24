# handlers/message_handler.py — LINE Webhook Orchestrator（整合版）
#
# 設計原則：
#   - reply 先行（不依賴任何 IO，防 LINE 30s timeout）
#   - ThreadPoolExecutor 取代 raw Thread + global lock（細粒度並發）
#   - 各 service 自行管理 lock（sheets: _history_lock, dedup: _dedup_lock）
#   - dedup key = event.message.id（LINE 保證全域唯一，無碰撞風險）
#
# 支援：
#   text  → 特殊指令（#幫助 / #查詢）或 Gemini 一般助理
#   image → Pillow 壓縮 → Drive 上傳
#   video → 串流下載到 /tmp → Drive 上傳（可 seek，支援 resumable）
#
# 群組支援：
#   - 只回應 #指令 或 @小暖（env: BOT_MENTION_NAME）
#   - user_id 為 None 時（使用者隱私設定）以 group:xxx 代替

import os
import logging
from concurrent.futures import ThreadPoolExecutor
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, TextMessage,
)
from linebot.v3.webhooks import GroupSource, RoomSource

from storage.event_schema import TextEvent, MediaEvent, make_timestamp
from services import sheets_service, drive_service, gemini_service

_log         = logging.getLogger(__name__)
_config      = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
_BOT_MENTION = os.environ.get("BOT_MENTION_NAME", "@小暖")  # 群組 @提及關鍵字

# ThreadPoolExecutor：允許最多 4 個 background 工作並發
# 圖片/影片/文字 Sheets 寫入彼此不阻塞
_executor = ThreadPoolExecutor(max_workers=4)


# ── 群組輔助函式 ──────────────────────────────────────────────

def _get_sender_id(event) -> str:
    """取得發送者 ID；群組中 user_id 可能為 None，改用 group:xxx"""
    uid = getattr(event.source, "user_id", None)
    if uid:
        return uid
    gid = getattr(event.source, "group_id", None)
    if gid:
        return f"group:{gid}"
    return "unknown"


def _is_group(event) -> bool:
    """判斷是否為群組或多人聊天室訊息"""
    return isinstance(event.source, (GroupSource, RoomSource))


# ── 主要 handler ─────────────────────────────────────────────

def handle_text(event) -> None:
    ts       = make_timestamp()
    user_id  = _get_sender_id(event)
    text     = event.message.text.strip()
    event_id = event.message.id          # LINE 全域唯一，無碰撞

    if sheets_service.is_duplicate_and_mark(event_id):
        _log.info("dedup skip text: %s", event_id)
        return

    # ── 群組處理：全部記錄，但只有 #指令 或 @提及才回覆 ────
    if _is_group(event):
        is_command = text.startswith("#")
        is_mention = _BOT_MENTION in text
        if not is_command and not is_mention:
            # 回覆確認 + 記錄
            _reply(event.reply_token, "✅ 已記錄")
            ev = TextEvent.from_user(event_id, user_id, text, ts)
            ev.ai_response = "✅ 已記錄"
            ev.status      = "ok"
            _executor.submit(_save_text, ev)
            return
        if is_mention and not is_command:
            text = text.replace(_BOT_MENTION, "").strip()  # 去掉 @小暖 後再處理

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
        ev = TextEvent.from_user(event_id, user_id, text, ts)
        ev.ai_response = reply
        ev.status      = "ok"
        _executor.submit(_save_text, ev)
        return

    if text == "#查詢":
        history = sheets_service.get_recent_text_history(user_id, 10)
        if not history:
            reply = "還沒有對話紀錄喔，先跟我說說話吧！💙"
        else:
            lines = []
            for item in history:
                # timestamp YYYYMMDD_HHMMSS → 取 HH:MM（index 9..12）
                t = item["time"][9:13]
                t = f"{t[:2]}:{t[2:]}"
                c = item["content"][:20]
                c += "…" if len(item["content"]) > 20 else ""
                lines.append(f"[{t}] {c}")
            reply = "【最近 10 筆對話】\n" + "\n".join(lines)
        _reply(event.reply_token, reply)
        ev = TextEvent.from_user(event_id, user_id, text, ts)
        ev.ai_response = reply
        ev.status      = "ok"
        _executor.submit(_save_text, ev)
        return

    # ── 一般文字：Gemini 一般助理（同步取得回覆再 reply）───
    ai_response = gemini_service.generate_reply(text)
    _reply(event.reply_token, ai_response)

    ev = TextEvent.from_user(event_id, user_id, text, ts)
    ev.ai_response = ai_response
    ev.status      = "ok"
    _executor.submit(_save_text, ev)


def handle_image(event) -> None:
    ts       = make_timestamp()
    user_id  = _get_sender_id(event)
    msg_id   = event.message.id         # LINE 全域唯一，同時作 event_id + 下載 key

    if sheets_service.is_duplicate_and_mark(msg_id):
        _log.info("dedup skip image: %s", msg_id)
        return

    ev = MediaEvent.from_line(msg_id, user_id, "image", ts)
    _reply(event.reply_token, "📷 收到照片，正在儲存…")
    _executor.submit(_process_image, ev, msg_id)


def handle_video(event) -> None:
    ts       = make_timestamp()
    user_id  = _get_sender_id(event)
    msg_id   = event.message.id

    if sheets_service.is_duplicate_and_mark(msg_id):
        _log.info("dedup skip video: %s", msg_id)
        return

    ev = MediaEvent.from_line(msg_id, user_id, "video", ts)
    _reply(event.reply_token, "🎬 收到影片，串流儲存中（稍後完成）…")
    _executor.submit(_process_video, ev, msg_id)


# ── Background workers（ThreadPoolExecutor，互不阻塞）────────

def _save_text(ev: TextEvent) -> None:
    """文字事件：寫 Sheets（sheets_service 內部自管 lock）"""
    try:
        sheets_service.append_text_event(ev)
    except Exception as e:
        _log.error("_save_text failed: %s", e)


def _process_image(ev: MediaEvent, message_id: str) -> None:
    """圖片：下載 → Pillow 壓縮 → Drive 上傳 → 寫 Sheets"""
    try:
        raw               = drive_service.download_line_content(message_id)
        filename          = drive_service.make_filename(ev.timestamp, message_id, "jpg")
        ev.drive_url, ev.status = drive_service.compress_and_upload(raw, filename)
        sheets_service.append_media_event(ev)
    except Exception as e:
        _log.error("_process_image failed: %s", e)


def _process_video(ev: MediaEvent, message_id: str) -> None:
    """影片：串流到 /tmp → Drive resumable 上傳 → 寫 Sheets"""
    try:
        filename          = drive_service.make_filename(ev.timestamp, message_id, "mp4")
        ev.drive_url, ev.status = drive_service.stream_and_upload_video(message_id, filename)
        sheets_service.append_media_event(ev)
    except Exception as e:
        _log.error("_process_video failed: %s", e)


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
