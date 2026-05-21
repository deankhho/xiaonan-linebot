# services/deadletter_service.py — Backup 失敗的最後防線
#
# 當 backup_service 無法寫入時呼叫此模組。
# 此模組本身「不拋例外」—— 若連 deadletter 都無法寫入，
# 只 print 到 stderr，讓 webhook 仍可回覆使用者。

import os
import sys
import logging
from datetime import datetime
from pathlib import Path
import orjson
import pytz

from storage.event_schema import LifeEvent

_TZ = pytz.timezone("Asia/Taipei")
_DEADLETTER_DIR = Path(os.getenv("DEADLETTER_DIR", "deadletter"))
_log = logging.getLogger(__name__)


def write(ev: LifeEvent, error: str) -> None:
    """
    將失敗的 event 寫入 deadletter/YYYYMMDD.jsonl。

    格式（每行一筆）：
      {"event": {...}, "error": "...", "failed_at": "YYYYMMDD_HHMMSS"}

    永遠不拋例外，失敗時 print 到 stderr。
    """
    try:
        _DEADLETTER_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(_TZ).strftime("%Y%m%d")
        path = _DEADLETTER_DIR / f"{today}.jsonl"
        record = {
            "event": {
                "timestamp": ev.timestamp,
                "type": ev.type,
                "sender": ev.sender,
                "content": ev.content,
            },
            "error": error,
            "failed_at": datetime.now(_TZ).strftime("%Y%m%d_%H%M%S"),
        }
        with path.open("ab") as f:
            f.write(orjson.dumps(record) + b"\n")
        _log.warning("deadletter written: %s (error: %s)", ev.timestamp, error)
    except Exception as e:
        # 最後防線也失敗 → 只能 print，不能再拋
        print(
            f"[DEADLETTER CRITICAL] cannot write deadletter for {ev.timestamp}: {e}",
            file=sys.stderr,
        )
