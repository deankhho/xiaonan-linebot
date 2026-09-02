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
    ReplyMessageRequest, PushMessageRequest, TextMessage,
    QuickReply, QuickReplyItem, MessageAction,
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

# ── 分類系統 ──────────────────────────────────────────────────
_CATEGORIES = ["醫療", "旅遊", "生活", "知識", "財經", "其他"]
_CAT_PREFIX = "__cat_"   # Quick Reply 隱藏前綴，避免與一般文字衝突

# 等待分類選擇的暫存：{sender_id: {"msg_id", "media_type", "orig_name", "timestamp", "ev"}}
# Render 重啟後清空（可接受）
_pending_upload: dict[str, dict] = {}


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

    # ── Quick Reply 分類選擇回應（必須在群組過濾前，否則被攔截）─
    if text.startswith(_CAT_PREFIX):
        category = text[len(_CAT_PREFIX):]
        if category in _CATEGORIES and user_id in _pending_upload:
            pending = _pending_upload.pop(user_id)
            ev      = pending["ev"]
            ev.category = category
            if ev.media_type == "image":
                _executor.submit(_process_image, ev, pending["msg_id"])
            elif ev.media_type == "video":
                _executor.submit(_process_video, ev, pending["msg_id"])
            elif ev.media_type == "pdf":
                _executor.submit(_process_file, ev, pending["msg_id"], pending["orig_name"], pending["timestamp"])
            _reply(event.reply_token, f"✅ 已儲存到【{category}】")
        else:
            # 2026-09-03：暫存查無此人＝Render 在「傳檔」與「點分類」之間重啟過，
            # _pending_upload（記憶體 dict）被清空。舊版在這裡直接 return，使用者
            # 端完全沒有訊息，檔案也永遠不會上傳＝靜默遺失。改成明確告知要重傳。
            _log.warning("pending not found for %s (category=%s)", user_id, category)
            _reply(event.reply_token,
                   "⚠️ 找不到剛才那個檔案（伺服器中途重啟了）。\n"
                   "麻煩再傳一次，這次選分類就會存進去。")
        return  # 不寫 Sheets

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
            "📷 傳照片/影片/PDF → 點選分類後存入雲端\n"
            "#查詢 → 最近 10 筆對話紀錄\n"
            "#月報 → 當月統計\n"
            "#幫助 → 這份說明"
        )
        _reply(event.reply_token, reply)
        ev = TextEvent.from_user(event_id, user_id, text, ts)
        ev.ai_response = reply
        ev.status      = "ok"
        _executor.submit(_save_text, ev)
        return

    if text == "#月報":
        from datetime import datetime
        import pytz
        now        = datetime.now(pytz.timezone("Asia/Taipei"))
        year_month = now.strftime("%Y%m")
        display    = now.strftime("%Y年%-m月")
        text_count = sheets_service.get_monthly_text_count(year_month)
        media      = sheets_service.get_monthly_media_summary(year_month)
        by_type    = media.get("by_type", {})
        by_cat     = media.get("by_category", {})
        reply = (
            f"【{display}月報】\n\n"
            f"📝 對話：{text_count} 筆\n\n"
            f"📁 媒體：\n"
            f"  圖片 {by_type.get('image', 0)} 張\n"
            f"  影片 {by_type.get('video', 0)} 支\n"
            f"  PDF  {by_type.get('pdf',   0)} 份\n\n"
            f"🏷️ 分類：\n"
            + "\n".join(
                f"  {cat} {by_cat.get(cat, 0)}"
                for cat in _CATEGORIES
            )
        )
        _reply(event.reply_token, reply)
        return  # 月報不寫 Sheets

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
    ts      = make_timestamp()
    user_id = _get_sender_id(event)
    msg_id  = event.message.id

    if sheets_service.is_duplicate_and_mark(msg_id):
        _log.info("dedup skip image: %s", msg_id)
        return

    ev = MediaEvent.from_line(msg_id, user_id, "image", ts)
    _pending_upload[user_id] = {"msg_id": msg_id, "media_type": "image", "orig_name": "", "timestamp": ts, "ev": ev}
    _reply_with_category_prompt(event.reply_token, "📷", "照片", user_id)


def handle_file(event) -> None:
    ts        = make_timestamp()
    user_id   = _get_sender_id(event)
    msg_id    = event.message.id
    orig_name = getattr(event.message, "file_name", "file.pdf")

    if sheets_service.is_duplicate_and_mark(msg_id):
        _log.info("dedup skip file: %s", msg_id)
        return

    ev = MediaEvent.from_line(msg_id, user_id, "pdf", ts)
    _pending_upload[user_id] = {"msg_id": msg_id, "media_type": "pdf", "orig_name": orig_name, "timestamp": ts, "ev": ev}
    _reply_with_category_prompt(event.reply_token, "📄", "PDF", user_id)


def handle_video(event) -> None:
    ts      = make_timestamp()
    user_id = _get_sender_id(event)
    msg_id  = event.message.id

    if sheets_service.is_duplicate_and_mark(msg_id):
        _log.info("dedup skip video: %s", msg_id)
        return

    ev = MediaEvent.from_line(msg_id, user_id, "video", ts)
    _pending_upload[user_id] = {"msg_id": msg_id, "media_type": "video", "orig_name": "", "timestamp": ts, "ev": ev}
    _reply_with_category_prompt(event.reply_token, "🎬", "影片", user_id)


# ── Background workers（ThreadPoolExecutor，互不阻塞）────────

def _save_text(ev: TextEvent) -> None:
    """文字事件：寫 Sheets（sheets_service 內部自管 lock）"""
    try:
        sheets_service.append_text_event(ev)
    except Exception as e:
        _log.error("_save_text failed: %s", e)


def _process_image(ev: MediaEvent, message_id: str) -> None:
    """圖片：下載 → Pillow 壓縮 → Drive 上傳（依分類子資料夾）→ 寫 Sheets"""
    try:
        raw      = drive_service.download_line_content(message_id)
        filename = drive_service.make_filename(ev.timestamp, message_id, "jpg")
        ev.drive_url, ev.status = drive_service.compress_and_upload(raw, filename, ev.category)
        sheets_service.append_media_event(ev)
    except Exception as e:
        _log.error("_process_image failed: %s", e)


def _process_file(ev: MediaEvent, message_id: str, orig_name: str, timestamp: str) -> None:
    """PDF：下載 → Drive 上傳（依分類子資料夾）→ 寫 Sheets"""
    try:
        ev.drive_url, ev.status = drive_service.upload_pdf(
            message_id, orig_name, timestamp, ev.category
        )
        sheets_service.append_media_event(ev)
    except Exception as e:
        _log.error("_process_file failed: %s", e)


def _process_video(ev: MediaEvent, message_id: str) -> None:
    """影片：串流到 /tmp → Drive resumable 上傳（依分類子資料夾）→ 寫 Sheets"""
    try:
        filename = drive_service.make_filename(ev.timestamp, message_id, "mp4")
        ev.drive_url, ev.status = drive_service.stream_and_upload_video(
            message_id, filename, ev.category
        )
        sheets_service.append_media_event(ev)
    except Exception as e:
        _log.error("_process_video failed: %s", e)


# ── 工具 ─────────────────────────────────────────────────────

def _reply_with_category_prompt(reply_token: str, emoji: str, label: str,
                                user_id: str = "") -> None:
    """傳帶有分類 Quick Reply 按鈕的提示訊息。

    2026-09-03 加 push 後備：LINE 的 reply_token 時效很短，Render 免費方案
    休眠後冷啟實測要 65 秒（暖機時只要 0.26 秒），醒來時 token 早已失效 →
    reply_message 丟例外 → 舊版只寫 log，使用者端完全看不到分類按鈕 →
    不會去點分類 → 檔案永遠不會上傳（上傳是在點分類之後才觸發的）。
    改成 reply 失敗就改用 push_message 補送，push 不需要 reply_token。
    """
    items = [
        QuickReplyItem(action=MessageAction(label=cat, text=f"{_CAT_PREFIX}{cat}"))
        for cat in _CATEGORIES
    ]
    message = TextMessage(
        text        = f"{emoji} 收到{label}！請選擇分類：",
        quick_reply = QuickReply(items=items),
    )
    try:
        with ApiClient(_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=[message])
            )
        return
    except Exception as e:
        _log.warning("reply_with_category_prompt failed (%s), 改用 push 補送", e)

    # 後備：push（計費，但只在 reply 失敗時才走，正常情況不會觸發）
    if not user_id or user_id.startswith("group:") or user_id == "unknown":
        _log.error("push 後備無法執行：user_id 不可用（%r）", user_id)
        return
    try:
        with ApiClient(_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[message])
            )
        _log.info("push 後備補送成功：%s", user_id)
    except Exception as e:
        _log.error("push 後備也失敗：%s", e)


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
