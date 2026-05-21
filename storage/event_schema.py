# storage/event_schema.py — 凍結的 Event Schema（定稿，不再修改欄位）
#
# 主鍵：timestamp（YYYYMMDD_HHMMSS，台北時間）
# drive_url 不在此 schema，只存在 Google Sheets（由 async_worker 寫入）
# 此檔案一旦部署，只允許新增欄位，不可刪除或改名

from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import ClassVar
import orjson
import pytz

_TZ = pytz.timezone("Asia/Taipei")


def make_timestamp() -> str:
    """產生台北時間主鍵字串，格式：YYYYMMDD_HHMMSS"""
    return datetime.now(_TZ).strftime("%Y%m%d_%H%M%S")


@dataclass
class LifeEvent:
    """
    家庭生命紀錄事件（immutable event log）。

    欄位：
      timestamp       主鍵，YYYYMMDD_HHMMSS（台北時間）
      type            "text" | "image"
      sender          LINE user_id
      content         文字內容；image 填 ""
      raw_backup_path backup 成功後填入相對路徑；初始為 ""
    """

    timestamp: str
    type: str
    sender: str
    content: str
    raw_backup_path: str = ""

    # Sheets 欄位標題（與 to_sheet_row 順序一致）
    SHEET_HEADERS: ClassVar[list[str]] = [
        "時間", "類型", "傳送者", "內容", "Drive連結", "備份路徑"
    ]

    # ── 工廠方法 ──────────────────────────────────────────────

    @classmethod
    def from_text(cls, user_id: str, content: str) -> "LifeEvent":
        return cls(
            timestamp=make_timestamp(),
            type="text",
            sender=user_id,
            content=content,
        )

    @classmethod
    def from_image(cls, user_id: str, timestamp: str) -> "LifeEvent":
        """timestamp 由 handler 產生，確保與 temp 檔名一致"""
        return cls(
            timestamp=timestamp,
            type="image",
            sender=user_id,
            content="",
        )

    # ── 序列化 ────────────────────────────────────────────────

    def to_json(self) -> bytes:
        """序列化為 JSON bytes（orjson，用於 backup / queue）"""
        return orjson.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: bytes) -> "LifeEvent":
        """從 JSON bytes 還原（用於 queue 讀取）"""
        d = orjson.loads(data)
        return cls(**d)

    def to_sheet_row(self, drive_url: str = "") -> list[str]:
        """
        轉為 Google Sheets 一列（6 欄）。
        drive_url 由 async_worker 傳入，不存在 event 本身。
        """
        return [
            self.timestamp,
            self.type,
            self.sender,
            self.content,
            drive_url,
            self.raw_backup_path,
        ]
