# storage/event_schema.py — 凍結的 Event Schema（定稿）
#
# Sheets = source of truth（cloud-native，不依賴本地磁碟）
# event_id = idempotency key，防 LINE webhook retry 重複寫入

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


@dataclass
class LifeEvent:
    """
    家庭生命紀錄事件。

    欄位：
      event_id   idempotency key：f"{timestamp}_{sender[-6:]}"
      timestamp  YYYYMMDD_HHMMSS（台北時間）
      type       "text" | "image"
      sender     LINE user_id
      content    文字內容；image 填 ""
      drive_url  Drive 分享 URL；text 填 ""；Drive 失敗填 ""
      status     "ok" | "drive_failed" | "pending"
    """

    event_id:  str
    timestamp: str
    type:      str
    sender:    str
    content:   str
    drive_url: str = ""
    status:    str = "pending"

    SHEET_HEADERS: ClassVar[list[str]] = [
        "EventID", "時間", "類型", "傳送者", "內容", "Drive連結", "狀態"
    ]

    # ── 工廠方法 ──────────────────────────────────────────────

    @classmethod
    def from_text(cls, user_id: str, content: str, timestamp: str) -> "LifeEvent":
        return cls(
            event_id  = f"{timestamp}_{user_id[-6:]}",
            timestamp = timestamp,
            type      = "text",
            sender    = user_id,
            content   = content,
        )

    @classmethod
    def from_image(cls, user_id: str, timestamp: str) -> "LifeEvent":
        return cls(
            event_id  = f"{timestamp}_{user_id[-6:]}",
            timestamp = timestamp,
            type      = "image",
            sender    = user_id,
            content   = "",
        )

    # ── 序列化 ────────────────────────────────────────────────

    def to_json(self) -> bytes:
        return orjson.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: bytes) -> "LifeEvent":
        return cls(**orjson.loads(data))

    def to_sheet_row(self) -> list[str]:
        """7 欄，順序與 SHEET_HEADERS 一致"""
        return [
            self.event_id,
            self.timestamp,
            self.type,
            self.sender,
            self.content,
            self.drive_url,
            self.status,
        ]
