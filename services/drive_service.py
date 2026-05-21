# services/drive_service.py — Google Drive 上傳（Cloud-native 版）
#
# 由 background thread 呼叫（不在 webhook request thread）
# Phase 1 只支援 image

import io
import os
import logging
import requests
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

_SCOPES        = ["https://www.googleapis.com/auth/drive.file"]
_FOLDER_ID     = os.environ.get("DRIVE_FOLDER_ID", "")
_LINE_API      = "https://api-data.line.me/v2/bot/message"
_LINE_TOKEN    = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
_IMAGE_MAX_PX  = 1920
_IMAGE_QUALITY = 85
_log = logging.getLogger(__name__)


def make_filename(timestamp: str, message_id: str, ext: str) -> str:
    """命名格式：20260521_181233_a1b2c3d4.jpg"""
    return f"{timestamp}_{message_id[:8]}.{ext}"


def download_line_content(message_id: str) -> bytes:
    """從 LINE 伺服器下載媒體原始 bytes"""
    url = f"{_LINE_API}/{message_id}/content"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {_LINE_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def compress_and_upload(raw: bytes, filename: str) -> tuple[str, str]:
    """
    Pillow 壓縮 → 上傳 Drive（公開可讀）→ 回傳 (drive_url, status)

    成功 → ("https://drive.google.com/file/d/.../view", "ok")
    失敗 → ("", "drive_failed")
    """
    try:
        compressed = _compress(raw)
        url = _upload(compressed, filename)
        _log.info("drive upload ok: %s", filename)
        return url, "ok"
    except Exception as e:
        _log.error("drive upload failed for %s: %s", filename, e)
        return "", "drive_failed"


# ── 內部函式 ─────────────────────────────────────────────────

def _compress(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((_IMAGE_MAX_PX, _IMAGE_MAX_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=_IMAGE_QUALITY, optimize=True)
    return buf.getvalue()


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"),
        scopes=_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload(data: bytes, filename: str) -> str:
    service = _get_service()
    media   = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg", resumable=False)
    file    = service.files().create(
        body       = {"name": filename, "parents": [_FOLDER_ID]},
        media_body = media,
        fields     = "id",
    ).execute()
    file_id = file["id"]
    service.permissions().create(
        fileId = file_id,
        body   = {"role": "reader", "type": "anyone"},
    ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view"
