# services/backup_service.py — 每個 Event 的 JSON 永久備份
#
# 設計原則：
#   - 每個 event 一個獨立 .json 檔（不 append 同檔，避免損毀）
#   - 寫入成功後更新 ev.raw_backup_path
#   - 寫入失敗 → 呼叫 deadletter_service.write()，不拋例外
#   - 此模組是整個系統的「write-ahead log」

import os
import logging
from pathlib import Path
import orjson
import pytz
from datetime import datetime

from storage.event_schema import LifeEvent
from services import deadletter_service

_TZ = pytz.timezone("Asia/Taipei")
_BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backup"))
_log = logging.getLogger(__name__)


def write_backup(ev: LifeEvent) -> None:
    """
    將 event 寫入 backup/YYYYMMDD/<timestamp>_<sender後6碼>.json。

    成功 → ev.raw_backup_path 填入相對路徑
    失敗 → deadletter_service.write()，不拋例外（webhook 繼續執行）
    """
    try:
        today = ev.timestamp[:8]               # "20260521"
        day_dir = _BACKUP_DIR / today
        day_dir.mkdir(parents=True, exist_ok=True)

        sender_short = ev.sender[-6:] if len(ev.sender) >= 6 else ev.sender
        filename = f"{ev.timestamp}_{sender_short}.json"
        path = day_dir / filename

        path.write_bytes(orjson.dumps(
            {
                "timestamp": ev.timestamp,
                "type": ev.type,
                "sender": ev.sender,
                "content": ev.content,
                "raw_backup_path": str(path.relative_to(Path("."))),
            },
            option=orjson.OPT_INDENT_2,
        ))

        # 回填路徑（dataclass 是 mutable，允許此操作）
        ev.raw_backup_path = str(path.relative_to(Path(".")))
        _log.info("backup ok: %s", ev.raw_backup_path)

    except Exception as e:
        _log.error("backup failed for %s: %s", ev.timestamp, e)
        deadletter_service.write(ev, str(e))
