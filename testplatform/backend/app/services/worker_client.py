"""Master-side client for talking to a remote worker's HTTP server (push model).

Every call authenticates with that worker's own password. A *worker* here is a plain dict
``{id, name, url, password, capacity}`` resolved once from the Worker row (so it can be handed to
dispatcher threads without dragging a SQLAlchemy session across threads).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

from app.services import cache_sync

logger = logging.getLogger(__name__)


def _base(worker: dict) -> str:
    return str(worker["url"]).rstrip("/")


def _headers(worker: dict) -> dict:
    return {"Authorization": f"Bearer {worker.get('password') or ''}"}


def health(worker: dict, timeout: float = 10.0) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/health", headers=_headers(worker))
        r.raise_for_status()
        return r.json()


def version(worker: dict, timeout: float = 10.0) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{_base(worker)}/version", headers=_headers(worker))
        r.raise_for_status()
        return r.json()


def quick_status(worker: dict, timeout: float = 1.5) -> tuple:
    """Live one-shot reachability probe. Returns ``(status, capacity)`` where status is
    ``"online"``/``"offline"`` and capacity is the worker's reported slot count (or None).

    Never raises — a timeout/unreachable host maps to ``("offline", None)``. Used by the
    dashboard/workers API to show the TRUE badge instead of the last value the DB happened to
    store (the CLI/distributed path talks to workers directly and never writes status back)."""
    try:
        h = health(worker, timeout=timeout)
        cap = h.get("capacity")
        return ("online", int(cap) if cap else None)
    except Exception:  # noqa: BLE001 — unreachable / timeout / auth -> offline
        return ("offline", None)


def run_trial(worker: dict, config: dict, fitness_metric: str, timeout: float = 1800.0) -> dict:
    """Push ONE trial to the worker and return its ``{ok,fitness,trades,error,fatal}`` summary.

    Sends the master's ``cache_root`` so the worker can remap absolute cache paths embedded in the
    config (screener_store, options_cache_db, ...) to ITS OWN local cache — the master and worker
    don't share a filesystem path.
    """
    from ba2_common.config import CACHE_FOLDER
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{_base(worker)}/run-trial", headers=_headers(worker),
                   json={"config": config, "fitness_metric": fitness_metric,
                         "cache_root": CACHE_FOLDER})
        r.raise_for_status()
        return r.json()


def push_cache(worker: dict, log: Callable[[str], None] = logger.info) -> dict:
    """Diff the master's cache against the worker's manifest and stream the missing files as ONE tar.

    Returns ``{pushed, ...}``. No-op (pushed=0) when the worker is already in sync.
    """
    base, headers = _base(worker), _headers(worker)
    with httpx.Client(timeout=60.0) as c:
        r = c.get(f"{base}/cache/manifest", headers=headers)
        r.raise_for_status()
        remote = r.json()
    local = cache_sync.build_manifest()
    missing = cache_sync.diff_missing(local["files"], remote)
    if not missing:
        log(f"cache push -> {worker['name']}: already in sync ({local['count']} files)")
        return {"pushed": 0, "extracted": 0}
    log(f"cache push -> {worker['name']}: streaming {len(missing)} file(s)...")
    stream = cache_sync.iter_tar(missing, local["root"])
    with httpx.Client(timeout=None) as c:  # large upload: no read timeout
        r = c.post(f"{base}/cache/push", headers=headers, content=stream)
        r.raise_for_status()
        res = r.json()
    log(f"cache push -> {worker['name']}: {res}")
    return {"pushed": len(missing), **res}


def ensure_synced(worker: dict, master_version: Optional[str],
                  log: Callable[[str], None] = logger.info, max_wait: float = 300.0) -> bool:
    """Make the worker run a compatible build: if its app version differs from the master's,
    trigger its /update and wait (polling /version) until it matches. Returns True if usable,
    False to exclude.

    Compatibility is keyed on ``app_version`` (not the git commit) so that ordinary pushes —
    docs, scratch scripts, unrelated fixes — don't force every connected worker to self-update
    mid-run. A worker only needs to re-sync when the app version is intentionally bumped.
    """
    try:
        wv = version(worker).get("app_version")
    except Exception as e:  # noqa: BLE001
        log(f"worker {worker['name']} unreachable ({e}); excluding")
        return False
    if not master_version or not wv or wv == master_version:
        return True
    log(f"worker {worker['name']} version {wv} != master {master_version}; updating + waiting...")
    try:
        with httpx.Client(timeout=120.0) as c:
            c.post(f"{_base(worker)}/update", headers=_headers(worker))
    except Exception as e:  # noqa: BLE001 — the restart may drop the connection; that's expected
        logger.debug(f"update call returned/dropped (expected on restart): {e}")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3.0)
        try:
            if version(worker).get("app_version") == master_version:
                log(f"worker {worker['name']} updated to {master_version}")
                return True
        except Exception:  # noqa: BLE001 — still restarting
            continue
    log(f"worker {worker['name']} did not converge to {master_version} in {max_wait:.0f}s; excluding")
    return False
