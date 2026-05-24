# storage/event_schema.py — 事件 Schema（拆分版）
#
# TextEvent  → 對話記錄（文字 + AI 回應）
# MediaEvent → 媒體記錄（圖片 / 影片）
#
# event_id 一律由呼叫方傳入（使用 event.message.id，LINE 保證全域唯一）

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


# ── 文字事件 ─────────────────────────────────────────────────

@dataclass
class TextEvent:
    """
    文字對話事件。
    欄位：event_id / timestamp / sender / content / ai_response / status
    event_id = event.message.id（LINE 全域唯一，用於 dedup）
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
    def from_user(cls, event_id: str, user_id: str, content: str, timestamp: str) -> "TextEvent":
        return cls(
            event_id  = event_id,
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
    媒體事件（圖片 / 影片 / PDF）。
    欄位：event_id / timestamp / sender / media_type / category / drive_url / status
    event_id = event.message.id（LINE 全域唯一，用於 dedup）
    """

    event_id:   str
    timestamp:  str
    sender:     str
    media_type: str            # "image" | "video" | "pdf"
    category:   str = "其他"   # 手動標記分類；未標記預設「其他」
    drive_url:  str = ""       # Drive 分享 URL；失敗填 ""
    status:     str = "pending"

    SHEET_HEADERS: ClassVar[list[str]] = [
        "EventID", "時間", "傳送者", "類型", "分類", "Drive連結", "狀態"
    ]

    @classmethod
    def from_line(cls, event_id: str, user_id: str, media_type: str, timestamp: str) -> "MediaEvent":
        return cls(
            event_id   = event_id,
            timestamp  = timestamp,
            sender     = user_id,
            media_type = media_type,
        )

    def to_sheet_row(self) -> list[str]:
        """7 欄，順序與 SHEET_HEADERS 一致"""
        return [
            self.event_id,
            self.timestamp,
            self.sender,
            self.media_type,
            self.category,
            self.drive_url,
            self.status,
        ]

    def to_json(self) -> bytes:
        return orjson.dumps(asdict(self))
