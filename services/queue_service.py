# services/queue_service.py — File-based Atomic Queue
#
# 設計原則：
#   - pending.jsonl：webhook append（每行一個 event JSON）
#   - flush_pending()：atomic rename → processing_<ts>.jsonl
#     確保同時只有一個 worker 在處理，且 crash 後資料不遺失
#   - recover_stuck_jobs()：worker startup 時呼叫，
#     將殘留的 processing_*.jsonl 合併回 pending.jsonl
#   - retry.jsonl：worker 失敗的 event（人工補救）

import os
import logging
from pathlib import Path
from datetime import datetime
import orjson
import pytz

from storage.event_schema import LifeEvent

_TZ = pytz.timezone("Asia/Taipei")
_QUEUE_DIR = Path(os.getenv("QUEUE_DIR", "queue"))
_PENDING = _QUEUE_DIR / "pending.jsonl"
_RETRY   = _QUEUE_DIR / "retry.jsonl"
_log = logging.getLogger(__name__)


def _ensure_dir() -> None:
    _QUEUE_DIR.mkdir(parents=True, exist_ok=True)


def enqueue(ev: LifeEvent) -> None:
    """將 event append 到 pending.jsonl（一行一筆）"""
    _ensure_dir()
    with _PENDING.open("ab") as f:
        f.write(ev.to_json() + b"\n")
    _log.debug("enqueued: %s", ev.timestamp)


def flush_pending() -> tuple[list[LifeEvent], Path | None]:
    """
    Atomic rename：pending.jsonl → processing_<ts>.jsonl

    回傳 (events, proc_path)。
    若 pending 不存在或為空，回傳 ([], None)。
    """
    _ensure_dir()
    if not _PENDING.exists() or _PENDING.stat().st_size == 0:
        return [], None

    ts = datetime.now(_TZ).strftime("%Y%m%d_%H%M%S")
    proc_path = _QUEUE_DIR / f"processing_{ts}.jsonl"
    _PENDING.rename(proc_path)   # atomic on POSIX

    events: list[LifeEvent] = []
    for line in proc_path.read_bytes().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(LifeEvent.from_json(line))
        except Exception as e:
            _log.error("queue parse error (skip line): %s", e)

    _log.info("flush_pending: %d events → %s", len(events), proc_path.name)
    return events, proc_path


def done_processing(path: Path | None) -> None:
    """Worker 處理完畢後刪除 processing 檔"""
    if path and path.exists():
        path.unlink()
        _log.debug("done_processing: deleted %s", path.name)


def move_to_retry(ev: LifeEvent, error: str) -> None:
    """Worker 處理失敗時，將 event 移入 retry.jsonl（人工補救）"""
    _ensure_dir()
    record = orjson.dumps({
        "event": orjson.loads(ev.to_json()),
        "error": error,
        "failed_at": datetime.now(_TZ).strftime("%Y%m%d_%H%M%S"),
    })
    with _RETRY.open("ab") as f:
        f.write(record + b"\n")
    _log.warning("moved to retry: %s (error: %s)", ev.timestamp, error)


def recover_stuck_jobs() -> None:
    """
    Worker startup 時呼叫（Crash Recovery）。

    glob processing_*.jsonl → 內容 append 回 pending.jsonl → 刪除 processing 檔。
    確保 worker crash 後，未處理的 event 不會遺失。
    """
    _ensure_dir()
    stuck_files = sorted(_QUEUE_DIR.glob("processing_*.jsonl"))
    if not stuck_files:
        return

    _log.warning("found %d stuck processing file(s), recovering...", len(stuck_files))
    for stuck in stuck_files:
        try:
            data = stuck.read_bytes()
            with _PENDING.open("ab") as f:
                f.write(data)
                if not data.endswith(b"\n"):
                    f.write(b"\n")
            stuck.unlink()
            _log.info("recovered: %s → pending.jsonl", stuck.name)
        except Exception as e:
            _log.error("recover failed for %s: %s", stuck.name, e)
