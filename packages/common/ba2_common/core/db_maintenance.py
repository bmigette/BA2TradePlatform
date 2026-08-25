"""Startup database housekeeping: retention purge, then a CONDITIONAL ``VACUUM``.

WHY THIS IS A SEPARATE MODULE FROM ``db.py``. ``db.py`` is the connection/engine layer
and is deliberately import-light; this is policy — how long history is kept and when a
full-file rewrite is worth its cost. Keeping them apart also keeps the vacuum's very
particular locking rules (below) documented where they are implemented.

THE VACUUM RULES, because getting them wrong is expensive:

  * ``VACUUM`` cannot run inside a transaction, so the connection is put into
    ``AUTOCOMMIT`` isolation. Under pysqlite's default isolation that is not merely
    belt-and-braces: any implicit ``BEGIN`` turns the statement into an error.
  * ``VACUUM`` takes an EXCLUSIVE lock on the WHOLE database and rewrites every page.
    Anything else that wants to write waits for it. It therefore belongs on the startup
    path BEFORE the job manager, the worker queue, the Smart-RM queue and the UI exist —
    see ``main.initialize_system``, which is the only caller.
  * It is GATED. Rewriting a 400 MB file on every boot to reclaim nothing costs a
    multi-second exclusive lock, ~400 MB of temporary disk and a full re-write of a
    file the user's money lives in. The gate is the freelist: only space that is
    actually reclaimable justifies the rewrite.
  * In WAL mode the vacuumed image lands in the -wal file, and the main database is not
    truncated until a checkpoint. Without the explicit ``wal_checkpoint(TRUNCATE)`` the
    space would not come back to the filesystem at all — the vacuum would "succeed" and
    reclaim nothing visible.

NOTHING HERE MAY STOP THE PLATFORM BOOTING. ``run_startup_maintenance`` never raises:
housekeeping does not get to prevent trading. It is also never silent — every outcome,
including "skipped", is logged, and every failure is logged at ERROR with a stack.

The purge itself is NOT implemented here. It lives in
``ba2_trade_platform.core.cleanup.cleanup_activity_logs``, which is what the Settings →
Cleanup tab calls, and is injected as ``purge_fn``. One implementation, two callers:
a second copy of "delete activity logs older than N days" is precisely the drift this
codebase keeps paying for. (Injection rather than an import because ``ba2_common`` is a
shared package and must not import the live platform.)
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, Callable, Dict, Optional

from ba2_common.core.db import get_engine, _db_write_lock
from ba2_common.logger import logger

#: Decimal units throughout — the sizes this feature reports are compared against the
#: numbers an operator reads off ``ls -l`` and the DB reports, which are decimal.
BYTES_PER_MB = 1_000_000
BYTES_PER_GB = 1_000_000_000

#: How much activity-log history is kept. 60 days: the Activity Monitor's own age
#: buckets stop at "> 180 days" but the table is a debugging/audit aid, not a record of
#: record — the money history lives in ``transaction``/``tradingorder``, which nothing
#: here ever touches.
DEFAULT_ACTIVITY_LOG_RETENTION_DAYS = 60
ACTIVITY_LOG_RETENTION_DAYS_ENV = "BA2_ACTIVITY_LOG_RETENTION_DAYS"

#: Reclaimable space below which a ``VACUUM`` is NOT worth its exclusive lock.
#:
#: 50 MB, measured against the shape of the live database (399 MB, 4 KiB pages) rather
#: than picked round:
#:   * COST is proportional to the LIVE data, not to the reclaimed amount — a vacuum
#:     rewrites all 399 MB whatever the freelist holds. So the question is only ever
#:     "is the reclaim worth one full rewrite?".
#:   * The first purge frees ~59 MB of activity log, which clears this bar — the boot
#:     that finally cleans up therefore also compacts the file.
#:   * Steady state afterwards is ~53 activity-log rows/day (~130 KB, ~31 pages). At
#:     that rate the freelist would need well over a year to reach 50 MB again, and in
#:     practice new rows re-use free pages first. So this fires essentially once, which
#:     is the point: the gate exists to stop a per-boot rewrite, not to schedule one.
#:   * It is also comfortably above ordinary churn, so a busy day cannot trip it.
DEFAULT_VACUUM_MIN_FREE_MB = 50.0
VACUUM_MIN_FREE_MB_ENV = "BA2_VACUUM_MIN_FREE_MB"

#: Database size at which the UI tells the user about the cleanup tool. 1 GB.
DEFAULT_DB_SIZE_WARN_GB = 1.0
DB_SIZE_WARN_GB_ENV = "BA2_DB_SIZE_WARN_GB"

#: Module-level so a test can substitute a statement that FAILS, and so the two
#: statements below are visible together. Not a knob: nothing configures these.
_VACUUM_SQL = "VACUUM"
#: WAL-mode only (a no-op elsewhere): return the freed pages to the FILESYSTEM.
_WAL_CHECKPOINT_SQL = "PRAGMA wal_checkpoint(TRUNCATE)"


# ---------------------------------------------------------------------------
# Configuration. Bad values are REFUSED, never silently defaulted.
# ---------------------------------------------------------------------------

def _refuse(source: str, raw: Any, requirement: str) -> "ValueError":
    return ValueError(
        f"invalid database-maintenance setting {source}: {raw!r} — {requirement}. "
        f"Refusing to guess: silently falling back to a default is how an operator who "
        f"asked for 180 days quietly gets 60 and loses four months of history."
    )


def resolve_retention_days(value: Any = None) -> int:
    """How many days of ``activitylog`` to keep. Explicit argument > env > default.

    Raises:
        ValueError: if the configured value is not a whole number of days >= 1. A
            retention of 0 would mean "delete everything including today", which is
            never what a mistyped setting meant.
    """
    if value is None:
        raw = os.getenv(ACTIVITY_LOG_RETENTION_DAYS_ENV)
        if raw is None:
            return DEFAULT_ACTIVITY_LOG_RETENTION_DAYS
        source = ACTIVITY_LOG_RETENTION_DAYS_ENV
    else:
        raw = value
        source = "retention_days"

    requirement = "expected a whole number of days >= 1"
    if isinstance(raw, bool):
        # bool is an int subclass; True would otherwise resolve to "keep 1 day".
        raise _refuse(source, raw, requirement)
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _refuse(source, raw, requirement) from None
    if days < 1:
        raise _refuse(source, raw, requirement)
    return days


def resolve_vacuum_min_free_mb(value: Any = None) -> float:
    """Reclaimable megabytes at or above which a ``VACUUM`` is worth running.

    Raises:
        ValueError: if the configured value is not a finite number >= 0.
    """
    if value is None:
        raw = os.getenv(VACUUM_MIN_FREE_MB_ENV)
        if raw is None:
            return DEFAULT_VACUUM_MIN_FREE_MB
        source = VACUUM_MIN_FREE_MB_ENV
    else:
        raw = value
        source = "min_free_mb"

    requirement = "expected a number of megabytes >= 0"
    if isinstance(raw, bool):
        raise _refuse(source, raw, requirement)
    try:
        megabytes = float(str(raw).strip())
    except (TypeError, ValueError):
        raise _refuse(source, raw, requirement) from None
    if not math.isfinite(megabytes) or megabytes < 0:
        raise _refuse(source, raw, requirement)
    return megabytes


def resolve_db_size_warn_bytes(value: Any = None) -> int:
    """Database size (in bytes) at which the UI should point at the cleanup tool.

    Raises:
        ValueError: if the configured value is not a finite number > 0.
    """
    if value is None:
        raw = os.getenv(DB_SIZE_WARN_GB_ENV)
        if raw is None:
            return int(DEFAULT_DB_SIZE_WARN_GB * BYTES_PER_GB)
        source = DB_SIZE_WARN_GB_ENV
    else:
        raw = value
        source = "warn_gb"

    requirement = "expected a size in gigabytes > 0"
    if isinstance(raw, bool):
        raise _refuse(source, raw, requirement)
    try:
        gigabytes = float(str(raw).strip())
    except (TypeError, ValueError):
        raise _refuse(source, raw, requirement) from None
    if not math.isfinite(gigabytes) or gigabytes <= 0:
        raise _refuse(source, raw, requirement)
    return int(gigabytes * BYTES_PER_GB)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def format_bytes(size: Optional[int]) -> str:
    """Human-readable decimal size, or ``'unknown'``. Never raises."""
    if size is None:
        return "unknown"
    if size >= BYTES_PER_GB:
        return f"{size / BYTES_PER_GB:.2f} GB"
    if size >= BYTES_PER_MB:
        return f"{size / BYTES_PER_MB:.1f} MB"
    return f"{size / 1_000:.1f} kB"


def database_file_path() -> Optional[str]:
    """The configured database FILE, or ``None`` when there is not one.

    ``None`` covers the backtest's ``:memory:`` engine (nothing to vacuum, nothing to
    size) and any engine we cannot interrogate.
    """
    try:
        database = get_engine().url.database
    except Exception as e:  # noqa: BLE001 — a measurement must never break a caller
        logger.warning(f"could not resolve the database file path: {e}")
        return None
    if not database or database == ":memory:":
        return None
    return database


def database_file_size_bytes() -> Optional[int]:
    """Size of the database file in bytes, or ``None`` if it cannot be measured.

    Only the main file: the ``-wal`` sidecar is a transient checkpoint buffer, not
    retained data, and reporting it would make the size jump around for reasons the
    reader cannot act on.
    """
    path = database_file_path()
    if path is None:
        return None
    try:
        return os.path.getsize(path)
    except OSError as e:
        logger.warning(f"could not measure the database file {path!r}: {e}")
        return None


def _freelist_bytes(engine) -> int:
    """Bytes currently on the database's free list (reclaimable by ``VACUUM``)."""
    with engine.connect() as conn:
        pages = conn.exec_driver_sql("PRAGMA freelist_count").scalar()
        page_size = conn.exec_driver_sql("PRAGMA page_size").scalar()
    return int(pages) * int(page_size)


# ---------------------------------------------------------------------------
# VACUUM
# ---------------------------------------------------------------------------

def vacuum_if_needed(min_free_mb: Any = None) -> Dict[str, Any]:
    """``VACUUM`` the database iff at least ``min_free_mb`` is reclaimable.

    EXCLUSIVE LOCK: only ever call this while nothing else in the process is scheduled
    to touch the database (i.e. at startup, before the queues and the UI).

    Returns a dict describing what was decided and done. Raises only if the database
    itself does — the caller (``run_startup_maintenance``) is what guarantees a failure
    cannot stop the boot.
    """
    threshold_bytes = int(resolve_vacuum_min_free_mb(min_free_mb) * BYTES_PER_MB)
    path = database_file_path()
    if path is None:
        logger.debug("VACUUM skipped: this engine has no database file")
        return {"vacuumed": False, "reason": "no database file",
                "free_bytes": 0, "threshold_bytes": threshold_bytes, "reclaimed_bytes": 0}

    engine = get_engine()
    free_bytes = _freelist_bytes(engine)
    if free_bytes < threshold_bytes:
        logger.info(
            f"VACUUM skipped: {format_bytes(free_bytes)} reclaimable is below the "
            f"{format_bytes(threshold_bytes)} threshold — rewriting "
            f"{format_bytes(database_file_size_bytes())} under an exclusive lock would "
            f"not pay for itself"
        )
        return {"vacuumed": False, "reason": "below threshold", "free_bytes": free_bytes,
                "threshold_bytes": threshold_bytes, "reclaimed_bytes": 0}

    # The in-process write mutex, so no other thread in this process can be mid-write
    # when the exclusive lock is taken. (At startup there is nobody; this is defence in
    # depth for any future caller.) Nothing inside calls back into the db helpers, so
    # the non-reentrant lock cannot deadlock against itself.
    with _db_write_lock:
        # AUTOCOMMIT: VACUUM cannot run inside a transaction. ``with`` guarantees the
        # connection is closed — and therefore returned to the pool clean — even when
        # the statement raises, which is what stops a failed vacuum leaving a write
        # transaction open for the next writer to block on forever.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # Fold the WAL into the main file BEFORE measuring. In WAL mode recent
            # writes live in the -wal sidecar, so the main file can be a fraction of
            # the real database (measured: 4 kB for a 2.5 MB database) and a
            # "reclaimed" figure computed from it comes out NEGATIVE.
            conn.exec_driver_sql(_WAL_CHECKPOINT_SQL)
            size_before = os.path.getsize(path)
            started = time.perf_counter()
            conn.exec_driver_sql(_VACUUM_SQL)
            # ...and again afterwards, or the compacted image sits in the -wal and the
            # filesystem never gets the space back.
            conn.exec_driver_sql(_WAL_CHECKPOINT_SQL)
            elapsed = time.perf_counter() - started

    size_after = os.path.getsize(path)
    reclaimed = size_before - size_after
    logger.info(
        f"VACUUM done in {elapsed:.1f}s: {format_bytes(size_before)} -> "
        f"{format_bytes(size_after)} (reclaimed {format_bytes(reclaimed)}; "
        f"{format_bytes(free_bytes)} was on the free list)"
    )
    return {"vacuumed": True, "reason": "at or above threshold", "free_bytes": free_bytes,
            "threshold_bytes": threshold_bytes, "reclaimed_bytes": reclaimed,
            "size_before": size_before, "size_after": size_after, "seconds": elapsed}


# ---------------------------------------------------------------------------
# The startup pass
# ---------------------------------------------------------------------------

def run_startup_maintenance(
        purge_fn: Callable[[int], Dict[str, Any]],
        *,
        retention_days: Any = None,
        min_free_mb: Any = None,
) -> Dict[str, Any]:
    """Purge old activity logs, then ``VACUUM`` if it is worth it. NEVER RAISES.

    Args:
        purge_fn: called as ``purge_fn(days_to_keep)``; must return the cleanup result
            dict ``{'deleted_count': int, 'error': str | None}``. In production this is
            ``ba2_trade_platform.core.cleanup.cleanup_activity_logs`` — the same function
            the Settings → Cleanup tab calls.
        retention_days: overrides the env/default retention.
        min_free_mb: overrides the env/default vacuum gate.

    Returns:
        ``{'purge': <result or None>, 'vacuum': <result or None>}``. ``None`` means that
        stage FAILED (and said so at ERROR); it never means "did nothing".
    """
    outcome: Dict[str, Any] = {"purge": None, "vacuum": None}
    started = time.perf_counter()

    try:
        days = resolve_retention_days(retention_days)
        purged = purge_fn(days)
        if not isinstance(purged, dict):
            raise TypeError(
                f"the purge callable returned {type(purged).__name__}; expected the "
                f"cleanup result dict {{'deleted_count': int, 'error': str | None}}")
        outcome["purge"] = purged
        if purged["error"]:
            # cleanup_activity_logs SWALLOWS its own exception and reports it in the
            # dict, so a caller that only watches for a raised exception would read a
            # total failure as a successful purge of zero rows.
            logger.error(
                f"STARTUP MAINTENANCE: the activity-log purge (keep {days}d) reported an "
                f"error and did NOT complete: {purged['error']}. The activity log will "
                f"keep growing until this is fixed."
            )
        else:
            logger.info(
                f"STARTUP MAINTENANCE: purged {purged['deleted_count']} activity-log "
                f"rows older than {days} days")
    except Exception as e:
        logger.error(
            f"STARTUP MAINTENANCE: the activity-log purge FAILED and was skipped: {e}. "
            f"Startup continues — housekeeping must never stop the platform booting — "
            f"but the activity log is NOT being trimmed.",
            exc_info=True,
        )

    try:
        outcome["vacuum"] = vacuum_if_needed(min_free_mb)
    except Exception as e:
        logger.error(
            f"STARTUP MAINTENANCE: VACUUM failed and was abandoned: {e}. Startup "
            f"continues; the database keeps its current size.",
            exc_info=True,
        )

    logger.info(f"STARTUP MAINTENANCE: finished in {time.perf_counter() - started:.1f}s "
                f"(database now {format_bytes(database_file_size_bytes())})")
    return outcome
