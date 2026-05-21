# handlers/message_handler.py — LINE 訊息事件 Orchestrator
#
# 職責：只負責協調各 service，不含業務邏輯。
# 流程（每個 handler）：
#   1. [image] 下載媒體 → save_to_temp
#   2. backup（失敗 → deadletter，繼續）
#   3. enqueue
#   4. 立即回覆使用者
#   5. trigger_worker（singleton，已在跑則 skip）
#
# Phase 1：只處理 text / image，video 不支援

import os
import logging
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, TextMessage,
)

from storage.event_schema import LifeEvent, make_timestamp
from services import backup_service, queue_service, async_worker
from services import drive_service

_log    = logging.getLogger(__name__)
_config = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))


def _reply(reply_token: str, text: str) -> None:
    """回覆純文字訊息"""
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def handle_text(event) -> None:
    """
    文字訊息流程：
      build event → backup → enqueue → reply → trigger_worker
    """
    user_id = event.source.user_id
    content = event.message.text
    _log.info("text from %s", user_id)

    ev = LifeEvent.from_text(user_id=user_id, content=content)
    backup_service.write_backup(ev)    # 失敗 → deadletter，繼續
    queue_service.enqueue(ev)

    _reply(event.reply_token, "✅ 已記錄")
    async_worker.trigger_worker()


def handle_image(event) -> None:
    """
    照片訊息流程：
      download → save_to_temp → build event → backup → enqueue → reply → trigger_worker
    """
    user_id    = event.source.user_id
    message_id = event.message.id
    _log.info("image from %s (msg: %s)", user_id, message_id)

    # 在 request thread 下載，存 temp（不呼叫 Drive API，保持快速）
    ts       = make_timestamp()
    filename = drive_service.make_filename(ts, message_id, "jpg")
    raw      = drive_service.download_line_content(message_id)
    drive_service.save_to_temp(raw, filename)

    ev = LifeEvent.from_image(user_id=user_id, timestamp=ts)
    backup_service.write_backup(ev)    # 失敗 → deadletter，繼續
    queue_service.enqueue(ev)

    _reply(event.reply_token, "📷 已排程上傳")
    async_worker.trigger_worker()
