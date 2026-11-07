"""A rate budget for ONE Alpha Vantage API key, shared by every process that opts in.

The premium plan allows 75 calls/minute for the KEY, not per process. ``FileRateBudget`` keeps the
timestamps of the last minute's calls in a small JSON file guarded by an OS file lock, so any number of
cooperating processes (several option downloaders, or any tool that adopts it) stay under ONE ceiling:

* ``per_minute``  -- this owner's own cap (its ``--calls-per-minute``);
* ``key_limit``   -- the ceiling over ALL owners in the file (default 75).

A process that does not use the file (``tools/backfill_av_estimates.py`` makes ~45 calls/min on its own
sleep) is invisible to it: give the downloader a ``per_minute`` of ``75 - that tool's rate - margin``
while both run.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

try:  # pragma: no cover - platform branch
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
except ImportError:  # pragma: no cover
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileRateBudget:
    def __init__(self, path: str, owner: str, per_minute: int, key_limit: int = 75, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 window_s: float = 60.0):
        if per_minute < 1 or key_limit < 1:
            raise ValueError("per_minute and key_limit must be >= 1")
        self.path, self.owner = path, owner
        self.per_minute, self.key_limit = int(per_minute), int(key_limit)
        self._clock, self._sleep, self._window = clock, sleep, window_s
        self._thread_lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "a"):
                pass

    def _try(self) -> Tuple[bool, float]:
        """(granted, seconds to wait if not)."""
        with open(self.path, "r+") as fh:
            _lock(fh)
            try:
                raw = fh.read().strip()
                try:
                    calls: List[list] = json.loads(raw) if raw else []
                except ValueError:
                    calls = []
                now = self._clock()
                calls = [c for c in calls if now - c[0] < self._window]
                mine = [c for c in calls if c[1] == self.owner]
                wait = 0.0
                if len(calls) >= self.key_limit:
                    wait = max(wait, self._window - (now - calls[0][0]))
                if len(mine) >= self.per_minute:
                    wait = max(wait, self._window - (now - mine[0][0]))
                granted = wait <= 0
                if granted:
                    calls.append([now, self.owner])
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(calls))
                fh.flush()
                return granted, wait
            finally:
                _unlock(fh)

    def acquire(self) -> None:
        """Block until one call may be made, and count it."""
        while True:
            with self._thread_lock:
                granted, wait = self._try()
            if granted:
                return
            self._sleep(min(max(wait, 0.05), 5.0))
