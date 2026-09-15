"""Per-role single-flight lock: at most one headless agent run per role at
a time. A lock file (not just in-memory state) so a receiver-process
restart can detect whether a run it doesn't remember starting is actually
still alive (PID check) or a crash left a stale lock behind.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LOCK_DIR = Path(__file__).resolve().parent / "run"


class RoleRunLock:
    def __init__(self, role: str, max_runtime_minutes: int):
        self.role = role
        self.max_runtime_seconds = max_runtime_minutes * 60
        self._mutex = threading.Lock()
        self._pending = False
        LOCK_DIR.mkdir(exist_ok=True)
        self._path = LOCK_DIR / f"{role}.lock"

    def _read(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
        return True

    def _clear_if_stale(self) -> None:
        state = self._read()
        if state is None:
            return
        age = time.time() - state.get("started_at", 0)
        pid_dead = not self._pid_alive(state.get("pid", -1))
        if pid_dead or age > self.max_runtime_seconds:
            self._path.unlink(missing_ok=True)

    def try_acquire(self, event_id: str) -> bool:
        """True if acquired (caller should launch a run now); False if
        another run is in flight (caller should mark this delivery pending
        and return 202 {queued: true})."""
        with self._mutex:
            self._clear_if_stale()
            if self._path.exists():
                self._pending = True
                return False
            self._path.write_text(json.dumps({
                "event_id": event_id, "started_at": time.time(), "pid": os.getpid(),
            }))
            return True

    def mark_pending(self) -> None:
        with self._mutex:
            self._pending = True

    def release_and_check_pending(self) -> bool:
        """Release the lock; return True if a delivery arrived while busy
        (caller should launch exactly one follow-up run -- its own
        read_messages call picks up everything unread, so multiple queued
        wakeups coalesce into one run, never one run per wakeup)."""
        with self._mutex:
            self._path.unlink(missing_ok=True)
            was_pending = self._pending
            self._pending = False
            return was_pending
