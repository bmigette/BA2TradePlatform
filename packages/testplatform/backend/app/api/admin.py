"""
Admin API endpoints.

Provides secure endpoints for CLI-driven server updates (git pull + restart)
and log file reading.
Protected by a bearer token configured via BA2_ADMIN_TOKEN environment variable.
"""

import collections
import hmac
import logging
import os
import subprocess
import sys
import threading
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter()

# Project root is three levels up from this file: admin.py -> api/ -> app/ -> backend/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

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


def _schedule_restart():
    """Replace the current process after a short delay to allow the response to be sent."""
    import time
    import signal
    time.sleep(2)
    logger.info("Restarting server...")

    # Stop all task queues (terminates subprocess workers)
    try:
        from app.services.task_queue import get_task_queue, get_training_task_queue, get_backtest_task_queue, get_ohlcv_task_queue
        for queue_getter in [get_training_task_queue, get_backtest_task_queue, get_task_queue, get_ohlcv_task_queue]:
            try:
                q = queue_getter()
                q.stop()
            except Exception:
                pass
        logger.info("All task queues stopped")
    except Exception as e:
        logger.warning(f"Failed to stop task queues: {e}")

    # Kill any remaining child processes (orphan prevention)
    import psutil
    try:
        current = psutil.Process()
        children = current.children(recursive=True)
        for child in children:
            logger.info(f"Terminating child process {child.pid}: {child.name()}")
            child.terminate()
        # Wait briefly for graceful termination
        psutil.wait_procs(children, timeout=5)
        # Force kill any survivors
        for child in current.children(recursive=True):
            logger.warning(f"Force killing child process {child.pid}")
            child.kill()
    except ImportError:
        # psutil not available — fall back to no child cleanup
        logger.warning("psutil not available — child processes may survive restart")
    except Exception as e:
        logger.warning(f"Child process cleanup error: {e}")

    # Rebuild the command using -m uvicorn to work on both Windows and Unix.
    uvicorn_args = []
    skip_next = False
    for i, arg in enumerate(sys.argv):
        if skip_next:
            skip_next = False
            continue
        if i == 0:
            continue
        uvicorn_args.append(arg)

    cmd = [sys.executable, "-m", "uvicorn"] + uvicorn_args
    logger.info("Restart command: %s", " ".join(cmd))
    os.execv(sys.executable, cmd)


@router.post("/update")
async def update_server(authorization: str = Header(default=None)):
    """
    Pull latest code from git and schedule a server restart.

    Requires BA2_ADMIN_TOKEN to be set in the environment.
    The request must include an Authorization: Bearer <token> header.
    """
    verify_admin_token(authorization)

    # Run git pull in the project root
    logger.info(f"Running git pull in {PROJECT_ROOT}")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        git_output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail="git pull failed (exit code {}): {}".format(
                    result.returncode, result.stderr.strip() or git_output
                ),
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="git pull timed out after 30 seconds.",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"git pull failed: {str(e)}",
        )

    logger.info(f"git pull output: {git_output}")

    # Schedule restart in a background thread so the response can be sent first
    threading.Thread(target=_schedule_restart, daemon=True).start()

    return {
        "git_pull": git_output,
        "restart": "scheduled",
        "message": "Server will restart in ~1 second.",
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
