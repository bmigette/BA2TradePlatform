"""``Idempotency-Key`` for management-API writes (design section 3).

A write that carries the header and succeeded is remembered for 10 minutes, keyed by ``(method, path, key)`` and a
fingerprint of the request body. A repeat within the window returns the FIRST response (marked
``Idempotent-Replay: true``) instead of sending a second order. Rules:
* only 2xx responses are stored -- a failed attempt may be retried under the same key;
* the same key with a different body is refused (422): it is a client bug, not a retry;
* a repeat while the first request is still running is refused (409) -- the first one may yet place its order;
* dry runs neither read nor write the store;
* in-memory, per process, lost on restart (a restart is far outside the 10 minute window that matters).
"""
import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

TTL_SECONDS = 600.0
MAX_KEY_CHARS = 200


@dataclass
class Stored:
    status_code: int
    body: bytes
    media_type: Optional[str]


@dataclass
class _Entry:
    fingerprint: str
    expires: float
    stored: Optional[Stored]        # None while the first request is in flight


def fingerprint(body: bytes, query: str) -> str:
    return hashlib.sha256(body + b"?" + query.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, ttl: float = TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str], _Entry] = {}

    def _purge(self, now: float) -> None:
        for k in [k for k, e in self._entries.items() if e.expires <= now]:
            del self._entries[k]

    def begin(self, scope: str, key: str, fp: str) -> Tuple[str, Optional[Stored]]:
        """``("run", None)`` -- go ahead (the key is now reserved); ``("replay", stored)``; ``("busy", None)``;
        ``("mismatch", None)``."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            entry = self._entries[(scope, key)] if (scope, key) in self._entries else None
            if entry is None:
                self._entries[(scope, key)] = _Entry(fp, now + self._ttl, None)
                return "run", None
            if entry.fingerprint != fp:
                return "mismatch", None
            if entry.stored is None:
                return "busy", None
            return "replay", entry.stored

    def finish(self, scope: str, key: str, stored: Stored) -> None:
        with self._lock:
            entry = self._entries[(scope, key)]
            entry.stored = stored
            entry.expires = self._clock() + self._ttl      # the window runs from completion

    def abort(self, scope: str, key: str) -> None:
        with self._lock:
            self._entries.pop((scope, key), None)


STORE = IdempotencyStore()
