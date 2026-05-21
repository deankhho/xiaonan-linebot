# services/sheets_service.py — Google Sheets 寫入（Projection Layer）
#
# 設計原則：
#   - 由 async_worker 在 background thread 呼叫
#   - drive_url 由 worker 傳入（不在 event schema 中）
#   - 全包 try/except，失敗只 log，不拋例外（Sheets 是次要層）

import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build

from storage.event_schema import LifeEvent

_SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
_SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
_SHEET_NAME     = os.environ.get("SHEET_NAME", "記錄")
_log = logging.getLogger(__name__)


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"),
        scopes=_SCOPES,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def append_event(ev: LifeEvent, drive_url: str = "") -> None:
    """
    將 event 追加到 Sheets 末列。

    drive_url 由 async_worker 傳入（image 上傳後取得，text 填 ""）。
    失敗只 log，不拋例外。
    """
    try:
        row = ev.to_sheet_row(drive_url=drive_url)
        _get_service().spreadsheets().values().append(
            spreadsheetId=_SPREADSHEET_ID,
            range=f"{_SHEET_NAME}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        _log.info("sheets append ok: %s", ev.timestamp)
    except Exception as e:
        _log.error("sheets append failed for %s: %s", ev.timestamp, e)


def init_headers() -> None:
    """
    初始化試算表第一列標題（部署後執行一次）。
    若第一列已有資料則跳過。
    """
    try:
        service = _get_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=_SPREADSHEET_ID,
            range=f"{_SHEET_NAME}!A1:F1",
        ).execute()
        if result.get("values"):
            _log.info("headers already exist, skipped")
            return
        service.spreadsheets().values().update(
            spreadsheetId=_SPREADSHEET_ID,
            range=f"{_SHEET_NAME}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [LifeEvent.SHEET_HEADERS]},
        ).execute()
        _log.info("headers initialized")
    except Exception as e:
        _log.error("init_headers failed: %s", e)
