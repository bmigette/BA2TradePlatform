"""HTTP cache synchronisation — the "avoid redownload" core for remote workers.

Everything under ``CACHE_FOLDER`` is immutable provider history (OHLCV parquet, fmp_history,
the screener metric_store + fundamentals, the options/screener sqlite caches). It is therefore
safe to sync ONE-WAY master -> worker: a worker mirrors the master's cache and then runs the
hermetic backtest with zero provider/network calls (the backtest contract raises
``BacktestCacheMiss`` rather than fetching).

This module is both the SERVER side (``build_manifest`` / ``safe_resolve``, used by
``app/api/cache_sync.py``) and the worker CLIENT side (``sync_cache``, used by
``ba2-test worker`` / ``ba2-test sync-cache``). Dedup is by ``(rel_path, size)`` — immutable
history means a size match is an identity match, so re-syncs only fetch genuinely new files.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

from ba2_common.config import CACHE_FOLDER

logger = logging.getLogger(__name__)

# Transient / in-use sidecar files that must never be synced (they are machine-local and would
# corrupt a fresh sqlite open on the worker). The main ``.sqlite`` IS synced; the worker opens
# it cleanly and re-derives any -wal/-shm.
_SKIP_SUFFIXES = (".tmp", ".part", ".lock", "-wal", "-shm", ".journal")


def cache_root(root: Optional[str] = None) -> Path:
    return Path(root or CACHE_FOLDER)


def _is_syncable(p: Path) -> bool:
    name = p.name
    if name.startswith("."):
        return False
    return not any(name.endswith(s) for s in _SKIP_SUFFIXES)


def build_manifest(root: Optional[str] = None) -> dict:
    """Enumerate every syncable cache file under *root* (default ``CACHE_FOLDER``).

    Returns ``{root, count, total_bytes, files:[{rel_path, size, mtime}]}`` with POSIX-style
    relative paths (stable across OSes). Recurses the whole cache tree so newly-added buckets
    are covered automatically (no allowlist to drift).
    """
    base = cache_root(root)
    files: List[dict] = []
    if base.is_dir():
        for p in base.rglob("*"):
            if not p.is_file() or not _is_syncable(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({
                "rel_path": p.relative_to(base).as_posix(),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    return {
        "root": str(base),
        "count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }


def safe_resolve(rel_path: str, root: Optional[str] = None) -> Path:
    """Resolve *rel_path* under *root*, rejecting any path that escapes it (traversal guard).

    Absolute paths and ``../`` escapes both resolve outside *root* and are rejected.
    """
    base = cache_root(root).resolve()
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"path escapes cache root: {rel_path!r}")
    return target


# --------------------------------------------------------------------------------------------
# Worker CLIENT side
# --------------------------------------------------------------------------------------------
def sync_cache(master_url: str, token: str, dest: Optional[str] = None,
               max_workers: int = 8, log: Callable[[str], None] = logger.info) -> dict:
    """Mirror the master's cache into *dest* (default local ``CACHE_FOLDER``).

    Fetches the master manifest, downloads only files that are missing locally or whose size
    differs (immutable history ⇒ size-match = skip), in parallel, with atomic temp+rename so an
    interrupted download never leaves a half file that a later size-check would treat as
    complete. Returns ``{total, downloaded, skipped, bytes, failed}``.
    """
    import httpx
    from concurrent.futures import ThreadPoolExecutor, as_completed

    base_url = master_url.rstrip("/")
    dest_root = Path(dest or CACHE_FOLDER)
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=60.0) as client:
        resp = client.get(f"{base_url}/api/cache/manifest", headers=headers)
        resp.raise_for_status()
        manifest = resp.json()

    files = manifest.get("files", [])
    todo = []
    for f in files:
        local = dest_root / f["rel_path"]
        try:
            up_to_date = local.is_file() and local.stat().st_size == f["size"]
        except OSError:
            up_to_date = False
        if not up_to_date:
            todo.append(f)

    log(f"cache sync: {len(files)} files on master, {len(todo)} to download "
        f"({manifest.get('total_bytes', 0) / 1e6:.0f} MB total)")

    downloaded = 0
    bytes_dl = 0
    failed: List[dict] = []

    def _download(f: dict) -> int:
        local = safe_resolve(f["rel_path"], str(dest_root))
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".part")
        with httpx.Client(timeout=600.0) as client:
            with client.stream("GET", f"{base_url}/api/cache/download",
                               params={"path": f["rel_path"]}, headers=headers) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
        os.replace(tmp, local)  # atomic on same filesystem
        return f["size"]

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            futs = {ex.submit(_download, f): f for f in todo}
            n = 0
            for fut in as_completed(futs):
                f = futs[fut]
                try:
                    bytes_dl += fut.result()
                    downloaded += 1
                except Exception as e:  # noqa: BLE001 — one bad file must not abort the sync
                    failed.append({"path": f["rel_path"], "error": str(e)})
                n += 1
                if n % 50 == 0 or n == len(todo):
                    log(f"cache sync: {n}/{len(todo)} downloaded ({len(failed)} failed)")

    return {
        "total": len(files),
        "downloaded": downloaded,
        "skipped": len(files) - len(todo),
        "bytes": bytes_dl,
        "failed": failed,
    }
