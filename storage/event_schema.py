# storage/event_schema.py — 事件 Schema（拆分版）
#
# TextEvent  → 對話記錄（文字 + AI 回應）
# MediaEvent → 媒體記錄（圖片 / 影片）

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import ClassVar
import orjson
import pytz

_TZ = pytz.timezone("Asia/Taipei")


def make_timestamp() -> str:
    """台北時間，格式：YYYYMMDD_HHMMSS"""
    return datetime.now(_TZ).strftime("%Y%m%d_%H%M%S")


def make_event_id(timestamp: str, user_id: str) -> str:
    """idempotency key：timestamp + user_id 末 6 碼"""
    return f"{timestamp}_{user_id[-6:]}"


# ── 文字事件 ─────────────────────────────────────────────────

@dataclass
class TextEvent:
    """
    文字對話事件。
    欄位：event_id / timestamp / sender / content / ai_response / status
    """

    event_id:    str
    timestamp:   str
    sender:      str
    content:     str           # 使用者訊息
    ai_response: str = ""      # Gemini 回覆；#指令 填回覆文字
    status:      str = "pending"

    SHEET_HEADERS: ClassVar[list[str]] = [
        "EventID", "時間", "傳送者", "訊息", "AI回應", "狀態"
    ]

    @classmethod
    def from_user(cls, user_id: str, content: str, timestamp: str) -> "TextEvent":
        return cls(
            event_id  = make_event_id(timestamp, user_id),
            timestamp = timestamp,
            sender    = user_id,
            content   = content,
        )

    def to_sheet_row(self) -> list[str]:
        """6 欄，順序與 SHEET_HEADERS 一致"""
        return [
            self.event_id,
            self.timestamp,
            self.sender,
            self.content,
            self.ai_response,
            self.status,
        ]

    def to_json(self) -> bytes:
        return orjson.dumps(asdict(self))


# ── 媒體事件 ─────────────────────────────────────────────────

@dataclass
class MediaEvent:
    """
    媒體事件（圖片 / 影片）。
    欄位：event_id / timestamp / sender / media_type / drive_url / status
    """

    event_id:   str
    timestamp:  str
    sender:     str
    media_type: str            # "image" | "video"
    drive_url:  str = ""       # Drive 分享 URL；失敗填 ""
    status:     str = "pending"

    SHEET_HEADERS: ClassVar[list[str]] = [
        "EventID", "時間", "傳送者", "類型", "Drive連結", "狀態"
    ]

    @classmethod
    def from_line(cls, user_id: str, media_type: str, timestamp: str) -> "MediaEvent":
        return cls(
            event_id   = make_event_id(timestamp, user_id),
            timestamp  = timestamp,
            sender     = user_id,
            media_type = media_type,
        )

    def to_sheet_row(self) -> list[str]:
        """6 欄，順序與 SHEET_HEADERS 一致"""
        return [
            self.event_id,
            self.timestamp,
            self.sender,
            self.media_type,
            self.drive_url,
            self.status,
        ]

    def to_json(self) -> bytes:
        return orjson.dumps(asdict(self))
