# services/sheets_service.py — Google Sheets（Source of Truth）
#
# 設計：
#   - Sheets = 唯一 event store（cloud-native，跨重啟持久）
#   - dedup：atomic lock + in-memory cache（啟動時預熱 500 筆）
#   - runtime 不打 Sheets lookup（避免 webhook 被拖慢）
#   - Sheets 失敗 → logging.error 固定格式（Render logs 可救回）

import os
import threading
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build

from storage.event_schema import LifeEvent

_SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
_SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
_SHEET_NAME     = os.environ.get("SHEET_NAME", "記錄")
_log = logging.getLogger(__name__)

# ── Dedup cache（in-process，atomic lock）───────────────────
_seen_ids:  set[str]      = set()
_MAX_SEEN:  int           = 1000
_dedup_lock: threading.Lock = threading.Lock()


def load_recent_ids(n: int = 500) -> None:
    """
    啟動時呼叫一次：讀最近 n 筆 EventID 到 _seen_ids。
    之後 runtime 只查 cache，不打 Sheets API。
    """
    try:
        rows = _get_service().spreadsheets().values().get(
            spreadsheetId=_SPREADSHEET_ID,
            range=f"{_SHEET_NAME}!A2:A",   # EventID 欄
        ).execute().get("values", [])

        recent = [row[0] for row in rows if row][-n:]
        with _dedup_lock:
            _seen_ids.update(recent)
        _log.info("load_recent_ids: %d ids loaded", len(recent))
    except Exception as e:
        _log.warning("load_recent_ids failed (non-fatal): %s", e)


def is_duplicate_and_mark(event_id: str) -> bool:
    """
    原子操作：check + mark。

    True  → 已見過，呼叫方應靜默 return
    False → 新事件，已加入 cache
    """
    with _dedup_lock:
        if event_id in _seen_ids:
            return True
        # cache miss → 直接放行（不打 Sheets，接受極低機率重複）
        if len(_seen_ids) >= _MAX_SEEN:
            _seen_ids.clear()
        _seen_ids.add(event_id)
        return False


# ── 主要功能 ─────────────────────────────────────────────────

def append_event(ev: LifeEvent) -> None:
    """
    將 event 追加到 Sheets 末列。
    失敗 → logging.error 固定格式（可 grep Render logs 補救）。
    """
    try:
        _get_service().spreadsheets().values().append(
            spreadsheetId  = _SPREADSHEET_ID,
            range          = f"{_SHEET_NAME}!A1",
            valueInputOption = "USER_ENTERED",
            insertDataOption = "INSERT_ROWS",
            body           = {"values": [ev.to_sheet_row()]},
        ).execute()
        _log.info("sheets append ok: %s", ev.event_id)
    except Exception as e:
        # 固定格式：之後可 grep "SHEETS_FAIL" 從 Render logs 取回資料
        _log.error(
            "SHEETS_FAIL event_id=%s payload=%s",
            ev.event_id,
            ev.to_json().decode(),
        )


def init_headers() -> None:
    """部署後執行一次，建立第一列標題。已有標題則跳過。"""
    try:
        svc    = _get_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId = _SPREADSHEET_ID,
            range         = f"{_SHEET_NAME}!A1:G1",
        ).execute()
        if result.get("values"):
            _log.info("headers already exist, skipped")
            return
        svc.spreadsheets().values().update(
            spreadsheetId    = _SPREADSHEET_ID,
            range            = f"{_SHEET_NAME}!A1",
            valueInputOption = "USER_ENTERED",
            body             = {"values": [LifeEvent.SHEET_HEADERS]},
        ).execute()
        _log.info("headers initialized: %s", LifeEvent.SHEET_HEADERS)
    except Exception as e:
        _log.error("init_headers failed: %s", e)


# ── 內部 ─────────────────────────────────────────────────────

def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"),
        scopes=_SCOPES,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)
