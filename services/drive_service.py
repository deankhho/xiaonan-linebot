# services/drive_service.py — Google Drive 上傳（定稿版）
#
# 流程：
#   webhook: download_line_content → save_to_temp（在 request thread）
#   worker:  upload_from_temp → 回傳 URL → cleanup_temp（在 background thread）
#
# Phase 1 只支援 image（video 不做）

import io
import os
import logging
from pathlib import Path
import requests
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

_SCOPES       = ["https://www.googleapis.com/auth/drive.file"]
_FOLDER_ID    = os.environ.get("DRIVE_FOLDER_ID", "")
_LINE_API     = "https://api-data.line.me/v2/bot/message"
_LINE_TOKEN   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
_TEMP_DIR     = Path(os.environ.get("TEMP_DIR", "temp"))
_IMAGE_MAX_PX = 1920
_IMAGE_QUALITY = 85
_log = logging.getLogger(__name__)


# ── 工具函式 ──────────────────────────────────────────────────

def make_filename(timestamp: str, message_id: str, ext: str) -> str:
    """命名格式：20260521_181233_a1b2c3d4.jpg"""
    return f"{timestamp}_{message_id[:8]}.{ext}"


# ── Request thread 用（快速，不呼叫外部 API 以外的長時操作）─────

def download_line_content(message_id: str) -> bytes:
    """從 LINE 伺服器下載媒體內容（原始 bytes）"""
    url = f"{_LINE_API}/{message_id}/content"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {_LINE_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def save_to_temp(raw: bytes, filename: str) -> Path:
    """將原始 bytes 存到 temp/ 目錄，回傳完整路徑"""
    _TEMP_DIR.mkdir(parents=True, exist_ok=True)
    path = _TEMP_DIR / filename
    path.write_bytes(raw)
    _log.debug("saved to temp: %s (%d bytes)", filename, len(raw))
    return path


# ── Worker thread 用（允許耗時）──────────────────────────────

def _compress_image(raw: bytes) -> bytes:
    """Pillow 壓縮：縮放長邊 + JPEG 重新編碼"""
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((_IMAGE_MAX_PX, _IMAGE_MAX_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=_IMAGE_QUALITY, optimize=True)
    return buf.getvalue()


def _get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"),
        scopes=_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload_bytes(data: bytes, filename: str) -> str:
    """壓縮後上傳到 Drive，設公開可讀，回傳分享 URL"""
    service = _get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg", resumable=False)
    file = service.files().create(
        body={"name": filename, "parents": [_FOLDER_ID]},
        media_body=media,
        fields="id",
    ).execute()
    file_id = file["id"]
    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
    ).execute()
    url = f"https://drive.google.com/file/d/{file_id}/view"
    _log.info("uploaded to Drive: %s → %s", filename, url)
    return url


def upload_from_temp(timestamp: str) -> str:
    """
    從 temp/ 找 <timestamp>*.jpg → 壓縮 → 上傳 Drive → 回傳 URL。
    由 async_worker 在 background thread 呼叫。
    """
    matches = list(_TEMP_DIR.glob(f"{timestamp}*.jpg"))
    if not matches:
        raise FileNotFoundError(f"temp file not found for timestamp: {timestamp}")
    temp_path = matches[0]
    raw = temp_path.read_bytes()
    compressed = _compress_image(raw)
    return _upload_bytes(compressed, temp_path.name)


def delete_temp(timestamp: str) -> None:
    """上傳完成後刪除 temp 暫存檔"""
    for path in _TEMP_DIR.glob(f"{timestamp}*.jpg"):
        path.unlink(missing_ok=True)
        _log.debug("temp deleted: %s", path.name)
