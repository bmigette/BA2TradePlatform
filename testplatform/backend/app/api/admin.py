"""
Admin API endpoints.

Provides secure endpoints for CLI-driven server updates (git pull + reinstall + restart),
a version probe (used by remote workers to detect a code mismatch), and log file reading.
Protected by a bearer token configured via BA2_ADMIN_TOKEN environment variable.

The update/restart mechanics live in ``app.services.self_update`` (monorepo-aware, shared with
the ``ba2-test worker`` CLI) so the master and a remote worker update + restart identically.
"""

import collections
import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pathlib import Path

from app.services import self_update

logger = logging.getLogger(__name__)

router = APIRouter()

# The git repository root is the MONOREPO root (the dir holding .git), NOT this app's sub-dir.
# self_update.resolve_repo_root walks up to find it (post-consolidation the .git lives one level
# above testplatform/, so the old "three levels up" assumption pointed at the wrong directory).
PROJECT_ROOT = self_update.resolve_repo_root()

# Backend root (where logs/ directory lives)
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def verify_admin_token(authorization: str):
    """Validate the Authorization header against BA2_ADMIN_TOKEN.

    Raises HTTPException on failure.
    """
    admin_token = os.environ.get("BA2_ADMIN_TOKEN")

    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail="Admin token is not configured on the server (BA2_ADMIN_TOKEN not set).",
        )

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header.",
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
        )

    token = parts[1]
    if not hmac.compare_digest(token, admin_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid admin token.",
        )


@router.get("/version")
async def get_version(authorization: str = Header(default=None)):
    """Report the running code identity (app version + git commit + repo root).

    Remote workers poll this to detect a code mismatch with the master: a distributed GA trial
    must run the IDENTICAL code, so a worker self-updates when its git commit differs.
    """
    verify_admin_token(authorization)
    return self_update.get_version_info(PROJECT_ROOT)


@router.post("/update")
async def update_server(
    authorization: str = Header(default=None),
    reinstall: str = Query(
        default="auto",
        description="Reinstall the shared package chain after pull: 'auto' (only when "
                    "non-editable), 'true' (always), or 'false' (never).",
    ),
):
    """
    Pull latest code (+ reinstall the shared packages when non-editable) and restart.

    Requires BA2_ADMIN_TOKEN to be set in the environment.
    The request must include an Authorization: Bearer <token> header.
    """
    verify_admin_token(authorization)

    reinstall_mode: "str | bool" = {"true": True, "false": False}.get(reinstall.lower(), "auto")
    logger.info(f"admin update: git pull in {PROJECT_ROOT} (reinstall={reinstall_mode})")
    report = self_update.perform_update(reinstall=reinstall_mode, root=PROJECT_ROOT)
    if not report.get("ok"):
        step = report.get("step", "update")
        detail = report.get("git_pull") or report.get("reinstall_logs") or "update failed"
        raise HTTPException(status_code=500, detail=f"update failed at {step}: {detail}")

    logger.info(f"admin update ok: {report.get('git_pull')}")

    # Restart in a background thread so this response can flush first. Stop the task queues
    # (terminating their subprocess workers) before re-exec'ing.
    self_update.schedule_restart(delay=2.0, on_before_restart=self_update.stop_task_queues)

    return {
        "git_pull": report.get("git_pull"),
        "reinstalled": report.get("reinstalled"),
        "editable": report.get("editable"),
        "version": report.get("version"),
        "restart": "scheduled",
        "message": "Server will restart shortly.",
    }


@router.get("/logs/{level}")
async def read_logs(
    level: str,
    lines: int = Query(default=100, ge=1, le=10000),
    search: Optional[str] = Query(default=None),
    authorization: str = Header(default=None),
):
    """
    Read the last N lines from a log file.

    *level* must be one of ``info``, ``error``, or ``debug``.
    Optionally filter lines with a case-insensitive *search* string.
    """
    verify_admin_token(authorization)

    valid_levels = ("info", "error", "debug")
    if level not in valid_levels:
        raise HTTPException(
            status_code=400,
            detail="Invalid log level '{}'. Must be one of: {}".format(
                level, ", ".join(valid_levels)
            ),
        )

    log_file = BACKEND_ROOT / "logs" / "{}.log".format(level)

    if not log_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Log file not found: logs/{}.log".format(level),
        )

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            if search:
                search_lower = search.lower()
                all_lines = [
                    line.rstrip("\n") for line in fh
                    if search_lower in line.lower()
                ]
            else:
                all_lines = collections.deque(fh, maxlen=lines)
                all_lines = [line.rstrip("\n") for line in all_lines]
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Failed to read log file: {}".format(str(exc)),
        )

    total = len(all_lines)
    result_lines = all_lines[-lines:] if lines < total else list(all_lines)

    return {
        "level": level,
        "lines": result_lines,
        "total_lines": total,
        "file": "logs/{}.log".format(level),
    }


@router.post("/db-cleanup")
async def db_cleanup(authorization: str = Header(default=None)):
    """
    Clean up the database: clear stale task results, VACUUM to reclaim space.

    This can reclaim hundreds of MB from completed backtest/training results
    that are already stored in their respective domain tables.
    """
    verify_admin_token(authorization)

    from app.services.task_queue import get_task_queue
    from app.models.database import engine

    task_queue = get_task_queue()
    cleared = task_queue.clear_completed_results(days=0)

    # Run VACUUM to reclaim space (SQLite doesn't free pages until VACUUM)
    db_path = None
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            # Get DB file size before
            result = conn.execute(text("PRAGMA page_count"))
            pages_before = result.scalar()
            page_size = conn.execute(text("PRAGMA page_size")).scalar()
            size_before_mb = (pages_before * page_size) / (1024 * 1024)

            conn.execute(text("VACUUM"))
            conn.commit()

            result = conn.execute(text("PRAGMA page_count"))
            pages_after = result.scalar()
            size_after_mb = (pages_after * page_size) / (1024 * 1024)

        return {
            "cleared_results": cleared,
            "size_before_mb": round(size_before_mb, 1),
            "size_after_mb": round(size_after_mb, 1),
            "reclaimed_mb": round(size_before_mb - size_after_mb, 1),
        }
    except Exception as e:
        return {
            "cleared_results": cleared,
            "vacuum_error": str(e),
        }
