# services/async_worker.py — Background Worker（Singleton）
#
# 設計原則：
#   - Double lock 防止 race condition（spawn lock + execution lock）
#   - Worker startup 時呼叫 recover_stuck_jobs（crash recovery）
#   - 逐筆處理，單筆失敗不中斷其他 event
#   - Drive upload 和 Sheets append 都在此 thread 執行（不在 webhook thread）

import threading
import logging

from services import queue_service, drive_service, sheets_service

_log = logging.getLogger(__name__)

# _worker_lock：保護 _run 執行中（不重入）
_worker_lock = threading.Lock()

# _spawn_lock：保護 spawn 瞬間的 race window（Patch 1）
# 防止兩個同時到達的 webhook 都看到 _worker_lock.locked()==False
_spawn_lock = threading.Lock()


def trigger_worker() -> None:
    """
    觸發 background worker。

    若 worker 正在執行（_worker_lock 已取得）→ skip。
    否則用 _spawn_lock 保護 spawn，確保只 spawn 一個 thread。
    """
    # 快速檢查：已在跑就不做
    if _worker_lock.locked():
        return

    # Atomic spawn guard（non-blocking acquire）
    if not _spawn_lock.acquire(blocking=False):
        return  # 另一個 webhook 正在 spawn，交給它
    try:
        # 雙重確認（spawn 取得後再看一次）
        if _worker_lock.locked():
            return
        t = threading.Thread(target=_run, daemon=True, name="xiaonan-worker")
        t.start()
        _log.debug("worker spawned")
    finally:
        _spawn_lock.release()


def _run() -> None:
    """Worker 主體，持有 _worker_lock 期間不允許第二個 worker 進入"""
    with _worker_lock:
        try:
            _process_pending()
        except Exception as e:
            _log.error("worker unexpected error: %s", e)


def _process_pending() -> None:
    """讀取 pending queue → 逐筆 Drive upload + Sheets append"""
    # Crash recovery：先處理殘留的 processing 檔
    queue_service.recover_stuck_jobs()

    events, proc_path = queue_service.flush_pending()
    if not events:
        _log.debug("pending queue empty, worker done")
        return

    _log.info("worker processing %d event(s)", len(events))

    for ev in events:
        drive_url = ""
        try:
            if ev.type == "image":
                drive_url = drive_service.upload_from_temp(ev.timestamp)
        except Exception as e:
            _log.error("drive upload failed for %s: %s", ev.timestamp, e)
            queue_service.move_to_retry(ev, f"drive: {e}")
            continue   # Sheets 也不寫（Drive URL 會空缺）

        # Sheets append（失敗不移入 retry，sheets_service 內部已 log）
        sheets_service.append_event(ev, drive_url=drive_url)

        # 上傳成功後清 temp
        if ev.type == "image":
            drive_service.delete_temp(ev.timestamp)

    queue_service.done_processing(proc_path)
    _log.info("worker done, processed %d event(s)", len(events))
