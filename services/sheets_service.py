# services/sheets_service.py — Google Sheets（Source of Truth）
#
# 兩張工作表：
#   對話記錄 — TextEvent（文字 + AI 回應）
#   媒體記錄 — MediaEvent（圖片 / 影片）
#
# 設計：
#   - dedup：atomic lock + in-memory cache（啟動預熱 500 筆）
#   - #查詢：從 local history cache 讀取，不打 Sheets API
#   - Sheets 失敗 → logging.error 固定格式（SHEETS_FAIL），Render logs 可救回

import os
import json
import threading
import logging
from collections import deque
from google.oauth2 import service_account
from googleapiclient.discovery import build

from storage.event_schema import TextEvent, MediaEvent

_SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
_SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
_SHEET_TEXT     = "對話記錄"
_SHEET_MEDIA    = "媒體記錄"
_log = logging.getLogger(__name__)

# ── Dedup cache（in-process，atomic lock）───────────────────
_seen_ids:    set[str]        = set()
_MAX_SEEN:    int             = 1000
_dedup_lock:  threading.Lock  = threading.Lock()

# ── Local text history cache（#查詢 用，不打 Sheets）────────
# 結構：{"sender": str, "time": str, "content": str, "response": str}
_text_history: deque = deque(maxlen=200)
_history_lock: threading.Lock = threading.Lock()


# ── 啟動預熱 ─────────────────────────────────────────────────

def load_recent_ids(n: int = 500) -> None:
    """
    啟動時呼叫一次：讀最近 n 筆 EventID 到 dedup cache。
    失敗為 non-fatal（Sheets 不可用時仍可接受 webhook）。
    """
    try:
        rows = _get_service().spreadsheets().values().get(
            spreadsheetId = _SPREADSHEET_ID,
            range         = f"{_SHEET_TEXT}!A2:A",
        ).execute().get("values", [])

        recent = [row[0] for row in rows if row][-n:]
        with _dedup_lock:
            _seen_ids.update(recent)
        _log.info("load_recent_ids: %d ids loaded", len(recent))
    except Exception as e:
        _log.warning("load_recent_ids failed (non-fatal): %s", e)


# ── Dedup ────────────────────────────────────────────────────

def is_duplicate_and_mark(event_id: str) -> bool:
    """
    原子操作：check + mark。
    True  → 已見過（LINE retry），呼叫方靜默 return
    False → 新事件，已加入 cache
    """
    with _dedup_lock:
        if event_id in _seen_ids:
            return True
        if len(_seen_ids) >= _MAX_SEEN:
            _seen_ids.clear()
        _seen_ids.add(event_id)
        return False


# ── #查詢：local cache（不打 Sheets API）─────────────────────

def get_recent_text_history(sender: str, n: int = 10) -> list[dict]:
    """
    從 in-memory history cache 取最近 n 筆對話。
    快速、不消耗 Sheets quota、不 timeout。
    """
    with _history_lock:
        user_items = [
            item for item in _text_history
            if item["sender"] == sender
        ]
    return user_items[-n:]


# ── 主要寫入功能 ─────────────────────────────────────────────

def append_text_event(ev: TextEvent) -> None:
    """
    追加到「對話記錄」工作表。
    同時更新 local history cache（供 #查詢 使用）。
    失敗 → logging.error 固定格式（SHEETS_FAIL）。
    """
    # 先更新 local cache（一定成功）
    with _history_lock:
        _text_history.append({
            "sender":   ev.sender,
            "time":     ev.timestamp,
            "content":  ev.content,
            "response": ev.ai_response,
        })

    # 再寫 Sheets（可能失敗，但有 log 可救回）
    try:
        _get_service().spreadsheets().values().append(
            spreadsheetId    = _SPREADSHEET_ID,
            range            = f"{_SHEET_TEXT}!A1",
            valueInputOption = "USER_ENTERED",
            insertDataOption = "INSERT_ROWS",
            body             = {"values": [ev.to_sheet_row()]},
        ).execute()
        _log.info("text append ok: %s", ev.event_id)
    except Exception as e:
        _log.error(
            "SHEETS_FAIL event_id=%s payload=%s",
            ev.event_id,
            ev.to_json().decode(),
        )


def append_media_event(ev: MediaEvent) -> None:
    """
    追加到「媒體記錄」工作表。
    失敗 → logging.error 固定格式（SHEETS_FAIL）。
    """
    try:
        _get_service().spreadsheets().values().append(
            spreadsheetId    = _SPREADSHEET_ID,
            range            = f"{_SHEET_MEDIA}!A1",
            valueInputOption = "USER_ENTERED",
            insertDataOption = "INSERT_ROWS",
            body             = {"values": [ev.to_sheet_row()]},
        ).execute()
        _log.info("media append ok: %s", ev.event_id)
    except Exception as e:
        _log.error(
            "SHEETS_FAIL event_id=%s payload=%s",
            ev.event_id,
            ev.to_json().decode(),
        )


def init_headers() -> None:
    """
    部署後執行一次，建立兩張工作表的標題列。
    已有標題則跳過。
    """
    svc = _get_service()
    for sheet_name, headers in [
        (_SHEET_TEXT,  TextEvent.SHEET_HEADERS),
        (_SHEET_MEDIA, MediaEvent.SHEET_HEADERS),
    ]:
        try:
            result = svc.spreadsheets().values().get(
                spreadsheetId = _SPREADSHEET_ID,
                range         = f"{sheet_name}!A1:Z1",
            ).execute()
            if result.get("values"):
                _log.info("headers already exist: %s", sheet_name)
                continue
            svc.spreadsheets().values().update(
                spreadsheetId    = _SPREADSHEET_ID,
                range            = f"{sheet_name}!A1",
                valueInputOption = "USER_ENTERED",
                body             = {"values": [headers]},
            ).execute()
            _log.info("headers init ok: %s → %s", sheet_name, headers)
        except Exception as e:
            _log.error("init_headers failed for %s: %s", sheet_name, e)


def health_check() -> bool:
    """確認 Sheets 可連線（供 /healthz 使用）"""
    try:
        _get_service().spreadsheets().get(
            spreadsheetId = _SPREADSHEET_ID
        ).execute()
        return True
    except Exception:
        return False


# ── 內部 ─────────────────────────────────────────────────────

def _get_service():
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON missing")
    creds_info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=_SCOPES,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)
