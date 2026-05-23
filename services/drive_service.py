# services/drive_service.py — Google Drive 上傳（Cloud-native 版）
#
# 圖片：Pillow 壓縮 → bytes 上傳（圖片通常 < 5MB，安全）
# 影片：方案 A 串流上傳
#       requests.get(stream=True) → iter_content → MediaIoBaseUpload(resumable=True)
#       峰值記憶體 ≈ chunk_size（5MB），不全部載入 RAM

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
_VIDEO_CHUNK   = 5 * 1024 * 1024   # 5MB：Google Drive resumable 建議值
_log = logging.getLogger(__name__)


def make_filename(timestamp: str, message_id: str, ext: str) -> str:
    """命名格式：20260523_143022_a1b2c3d4.jpg"""
    return f"{timestamp}_{message_id[:8]}.{ext}"


# ── 圖片（原有流程保留）──────────────────────────────────────

def download_line_content(message_id: str) -> bytes:
    """圖片：全部載入 bytes（圖片 < 5MB，安全）"""
    url  = f"{_LINE_API}/{message_id}/content"
    resp = requests.get(
        url,
        headers = {"Authorization": f"Bearer {_LINE_TOKEN}"},
        timeout = 30,
    )
    resp.raise_for_status()
    return resp.content


def compress_and_upload(raw: bytes, filename: str) -> tuple[str, str]:
    """圖片：Pillow 壓縮 → Drive 上傳 → 回傳 (drive_url, status)"""
    try:
        compressed = _compress(raw)
        url        = _upload_bytes(compressed, filename, "image/jpeg")
        _log.info("image upload ok: %s", filename)
        return url, "ok"
    except Exception as e:
        _log.error("image upload failed for %s: %s", filename, e)
        return "", "drive_failed"


# ── 影片（方案 A 串流上傳）──────────────────────────────────

class _LineStreamIO(io.RawIOBase):
    """
    將 requests streaming response（iter_content）包裝為 RawIOBase，
    供 BufferedReader → MediaIoBaseUpload 讀取。
    記憶體峰值 ≈ chunk_size（5MB），不全部載入 RAM。
    """

    def __init__(self, response: requests.Response, chunk_size: int = _VIDEO_CHUNK):
        self._iter = response.iter_content(chunk_size=chunk_size)
        self._buf  = b""
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:
        if self._done:
            return 0

        # 補充 buffer（可能一次 next() 拿到的 chunk 比 b 小）
        while not self._buf:
            try:
                self._buf = next(self._iter)
            except StopIteration:
                self._done = True
                return 0

        n        = len(b)
        chunk    = self._buf[:n]
        self._buf = self._buf[n:]
        b[:len(chunk)] = chunk
        return len(chunk)


def stream_and_upload_video(message_id: str, filename: str) -> tuple[str, str]:
    """
    影片：方案 A 串流上傳。
    1. requests.get(stream=True) — 不全部載入記憶體
    2. _LineStreamIO → BufferedReader — 分塊讀取
    3. MediaIoBaseUpload(resumable=True) — 分 5MB 上傳到 Drive
    回傳 (drive_url, status)
    """
    try:
        url  = f"{_LINE_API}/{message_id}/content"
        resp = requests.get(
            url,
            headers = {"Authorization": f"Bearer {_LINE_TOKEN}"},
            stream  = True,
            timeout = 120,    # 影片下載允許較長 timeout
        )
        resp.raise_for_status()

        # 串流 IO 包裝
        stream_io = io.BufferedReader(_LineStreamIO(resp), buffer_size=_VIDEO_CHUNK)
        media     = MediaIoBaseUpload(
            stream_io,
            mimetype  = "video/mp4",
            chunksize = _VIDEO_CHUNK,
            resumable = True,
        )

        service = _get_service()
        request = service.files().create(
            body       = {"name": filename, "parents": [_FOLDER_ID]},
            media_body = media,
            fields     = "id",
        )

        # 逐 chunk 上傳
        file_id  = None
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                _log.info(
                    "video upload progress: %.0f%% (%s)",
                    status.progress() * 100,
                    filename,
                )
        file_id = response["id"]

        # 設定公開可讀
        service.permissions().create(
            fileId = file_id,
            body   = {"role": "reader", "type": "anyone"},
        ).execute()

        drive_url = f"https://drive.google.com/file/d/{file_id}/view"
        _log.info("video upload ok: %s", filename)
        return drive_url, "ok"

    except Exception as e:
        _log.error("video upload failed for %s: %s", filename, e)
        return "", "drive_failed"


def health_check() -> bool:
    """確認 Drive 可連線（供 /healthz 使用）"""
    try:
        _get_service().files().list(pageSize=1).execute()
        return True
    except Exception:
        return False


# ── 內部函式 ─────────────────────────────────────────────────

def _compress(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((_IMAGE_MAX_PX, _IMAGE_MAX_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=_IMAGE_QUALITY, optimize=True)
    return buf.getvalue()


def _upload_bytes(data: bytes, filename: str, mimetype: str) -> str:
    service = _get_service()
    media   = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
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


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"),
        scopes=_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)
