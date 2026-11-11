"""Background worker: keeps the reminder queue moving.

Local/Docker runs it as a daemon thread. On serverless (Vercel) the same
`run_tick()` is invoked by the cron endpoint /api/cron/tick instead.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

from . import db, engine

INTERVAL = int(os.environ.get("REBUY_TICK_SECONDS", "20"))
_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()


def run_tick() -> dict:
    """One worker pass; records heartbeat metadata for the admin queue page."""
    with _lock:
        stats = engine.tick(limit=int(os.environ.get("REBUY_TICK_BATCH", "400")))
        db.meta_set("last_tick", db.now_iso())
        db.meta_set("last_tick_stats", json.dumps(stats))
        db.meta_set("tick_count", str(int(db.meta_get("tick_count", "0") or 0) + 1))
        return stats


def _loop():
    time.sleep(3)
    while not _stop.is_set():
        try:
            run_tick()
        except Exception:  # noqa: BLE001 - a worker must never die on one bad tick
            traceback.print_exc()
            db.meta_set("last_error", traceback.format_exc()[-800:])
        _stop.wait(INTERVAL)


def start():
    global _thread
    if os.environ.get("REBUY_WORKER") == "0":
        print("worker disabled (REBUY_WORKER=0)")
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="rebuy-worker", daemon=True)
    _thread.start()
    print(f"worker started (every {INTERVAL}s)")


def stop():
    _stop.set()
