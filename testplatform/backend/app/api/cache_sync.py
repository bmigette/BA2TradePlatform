"""Cache-sync API (master side) — lets a remote worker mirror the master's immutable cache.

``GET /api/cache/manifest``  -> the list of syncable cache files (path/size/mtime).
``GET /api/cache/download``  -> stream one cache file by relative path (traversal-guarded).

Both are gated by the shared worker token (``BA2_WORKER_TOKEN`` / ``BA2_ADMIN_TOKEN``).
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse

from app.services import cache_sync
from app.services.worker_auth import verify_worker_token

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/manifest")
async def cache_manifest(authorization: str = Header(default=None)):
    """List every syncable cache file under CACHE_FOLDER (immutable provider history)."""
    verify_worker_token(authorization)
    return cache_sync.build_manifest()


@router.get("/download")
async def cache_download(path: str = Query(..., description="Relative path from the manifest."),
                         authorization: str = Header(default=None)):
    """Stream one cache file by its manifest-relative path."""
    verify_worker_token(authorization)
    try:
        target = cache_sync.safe_resolve(path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Cache file not found.")
    return FileResponse(str(target), filename=target.name)
