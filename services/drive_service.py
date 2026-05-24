# services/drive_service.py — Google Drive 上傳（Cloud-native 版）
#
# 圖片：Pillow 壓縮 → bytes 上傳（圖片通常 < 5MB，安全）
# 影片：串流寫入 /tmp → 從磁碟上傳（可 seek，支援 resumable retry）→ 刪除暫存
#       峰值記憶體 ≈ chunk_size（5MB）
#
# 注意：原方案 A（_LineStreamIO）因 MediaIoBaseUpload(resumable=True) 需要 seek()
#       改為方案 B 混合：串流下載到 /tmp（不爆 RAM）+ 從磁碟上傳（可 seek）

import io
import os
import tempfile
import logging
import threading
import requests
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

_SCOPES        = ["https://www.googleapis.com/auth/drive.file"]
_FOLDER_ID     = os.environ.get("DRIVE_FOLDER_ID", "")
_LINE_API      = "https://api-data.line.me/v2/bot/message"
_LINE_TOKEN    = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
_IMAGE_MAX_PX  = 1920
_IMAGE_QUALITY = 85
_VIDEO_CHUNK   = 5 * 1024 * 1024   # 5MB chunk
_log = logging.getLogger(__name__)

# ── 子資料夾快取（process 記憶體，thread-safe）────────────────
_subfolder_cache: dict[str, str] = {}   # category → folder_id
_subfolder_lock  = threading.Lock()


def make_filename(timestamp: str, message_id: str, ext: str) -> str:
    """命名格式：20260523_143022_a1b2c3d4.jpg"""
    return f"{timestamp}_{message_id[:8]}.{ext}"


# ── 圖片 ─────────────────────────────────────────────────────

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


def compress_and_upload(raw: bytes, filename: str, category: str = "其他") -> tuple[str, str]:
    """圖片：Pillow 壓縮 → Drive 上傳（依分類放子資料夾）→ 回傳 (drive_url, status)"""
    try:
        compressed = _compress(raw)
        parent_id  = _get_or_create_subfolder(category) if category != "其他" else _FOLDER_ID
        url        = _upload_bytes(compressed, filename, "image/jpeg", parent_id)
        _log.info("image upload ok: %s [%s]", filename, category)
        return url, "ok"
    except Exception as e:
        _log.error("image upload failed for %s: %s", filename, e)
        return "", "drive_failed"


# ── 影片（串流 /tmp + resumable 上傳）───────────────────────

def stream_and_upload_video(message_id: str, filename: str, category: str = "其他") -> tuple[str, str]:
    """
    影片上傳流程：
    1. requests.get(stream=True) + iter_content → 串流寫入 /tmp（峰值記憶體 ~5MB）
    2. 從 /tmp 磁碟檔開啟上傳（seekable，支援 resumable retry）
    3. 上傳完成後刪除暫存檔
    回傳 (drive_url, status)
    """
    tmp_path = None
    try:
        # ── 步驟 1：串流下載到 /tmp ──────────────────────────
        url  = f"{_LINE_API}/{message_id}/content"
        resp = requests.get(
            url,
            headers = {"Authorization": f"Bearer {_LINE_TOKEN}"},
            stream  = True,
            timeout = 120,
        )
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in resp.iter_content(chunk_size=_VIDEO_CHUNK):
                if chunk:
                    tmp.write(chunk)
        _log.info("video downloaded to tmp: %s (%s)", tmp_path, filename)

        # ── 步驟 2：從磁碟上傳（可 seek，支援 resumable）───────
        parent_id = _get_or_create_subfolder(category) if category != "其他" else _FOLDER_ID
        with open(tmp_path, "rb") as f:
            media   = MediaIoBaseUpload(
                f,
                mimetype  = "video/mp4",
                chunksize = _VIDEO_CHUNK,
                resumable = True,
            )
            service = _get_service()
            request = service.files().create(
                body       = {"name": filename, "parents": [parent_id]},
                media_body = media,
                fields     = "id",
            )

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
        _log.info("video upload ok: %s [%s]", filename, category)
        return drive_url, "ok"

    except Exception as e:
        _log.error("video upload failed for %s: %s", filename, e)
        return "", "drive_failed"

    finally:
        # ── 步驟 3：一定刪除暫存檔 ───────────────────────────
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
                _log.info("tmp file deleted: %s", tmp_path)
            except Exception as e:
                _log.warning("tmp delete failed: %s", e)


# ── PDF ──────────────────────────────────────────────────────

def upload_pdf(message_id: str, original_name: str, timestamp: str, category: str = "其他") -> tuple[str, str]:
    """
    PDF：下載 bytes → Drive 上傳（依分類放子資料夾）→ 回傳 (drive_url, status)
    檔名格式：20260524_143022_a1b2c3d4_原始名稱.pdf
    """
    try:
        raw       = download_line_content(message_id)
        base      = os.path.splitext(original_name)[0]   # 去掉副檔名
        filename  = f"{timestamp}_{message_id[:8]}_{base}.pdf"
        parent_id = _get_or_create_subfolder(category) if category != "其他" else _FOLDER_ID
        url       = _upload_bytes(raw, filename, "application/pdf", parent_id)
        _log.info("pdf upload ok: %s [%s]", filename, category)
        return url, "ok"
    except Exception as e:
        _log.error("pdf upload failed for %s: %s", original_name, e)
        return "", "drive_failed"


# ── 健康檢查 ─────────────────────────────────────────────────

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


def _get_or_create_subfolder(category: str) -> str:
    """
    取得或建立分類子資料夾，回傳 folder_id。
    結果快取在 _subfolder_cache（process 記憶體，thread-safe）。
    """
    with _subfolder_lock:
        if category in _subfolder_cache:
            return _subfolder_cache[category]

        service = _get_service()
        # 查找現有子資料夾
        q = (
            f"name='{category}' and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"'{_FOLDER_ID}' in parents and "
            f"trashed=false"
        )
        results = service.files().list(q=q, fields="files(id)").execute()
        files   = results.get("files", [])

        if files:
            folder_id = files[0]["id"]
            _log.info("subfolder found: %s → %s", category, folder_id)
        else:
            folder = service.files().create(
                body   = {
                    "name":     category,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents":  [_FOLDER_ID],
                },
                fields = "id",
            ).execute()
            folder_id = folder["id"]
            _log.info("subfolder created: %s → %s", category, folder_id)

        _subfolder_cache[category] = folder_id
        return folder_id


def _upload_bytes(data: bytes, filename: str, mimetype: str, parent_id: str = "") -> str:
    parent    = parent_id or _FOLDER_ID
    service   = _get_service()
    media     = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    file      = service.files().create(
        body       = {"name": filename, "parents": [parent]},
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
    """個人 OAuth2 Token 上傳，使用使用者自己的 Drive 配額"""
    creds = Credentials(
        token         = None,
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN"),
        client_id     = os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET"),
        token_uri     = "https://oauth2.googleapis.com/token",
        scopes        = _SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)
