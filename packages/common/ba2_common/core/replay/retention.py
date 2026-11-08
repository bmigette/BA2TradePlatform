"""Replay store retention: statistics, and an explicit, operator-run prune. NOTHING here runs by itself.

ONE implementation, two callers: ``ba2-test replay prune`` (CLI) and the live app's Settings -> Cleanup tab.

The store only grows (a loaded decision pass is ~400 KB). A prune deletes SESSIONS older than ``keep_days``
and then every object file no remaining session can reach:

* A session is deleted only when it is not ``open`` (an open session is being written by a live process;
  this also protects the running app's own session) and its end (its start when it has none) is older than
  the cutoff.
* An object is deleted only when (a) it is unreachable from every kept or open session (reachability follows
  the ``$frame`` / ``$ref`` links between objects, exactly as ``export_session`` does) AND (b) its file is
  older than the age bound ``min(cutoff, oldest kept/open session start, now - 1 day)``. Rule (b) is what
  makes the prune safe against a live writer: an object a recorder published a moment ago, or re-used by a
  new analysis (``ObjectStore.put`` refreshes the mtime of an object it finds already published), is never
  touched, even though no committed record references it yet.
* The object scan is repeated AFTER the sessions are deleted and BEFORE any file is, so anything a live
  writer committed in between is seen.
* An inconsistency (a kept session references an object that is not on disk, an undecodable object, a
  second prune already running) REFUSES the whole prune before anything is deleted; a file that cannot be
  deleted is reported (and raised at the end), never skipped silently.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from ba2_common.core.replay.codec import frame_refs
from ba2_common.core.replay.schemas import ReplayStatus, SessionRecord
from ba2_common.core.replay.store import ReplayIndex
from ba2_common.logger import logger

__all__ = ["RetentionError", "RetentionRefused", "statistics", "preview_prune", "execute_prune",
           "format_plan", "human_bytes", "MIN_KEEP_DAYS"]

LOCK_NAME = "prune.lock"
MIN_KEEP_DAYS = 1
_SUFFIXES = {".json": "json", ".arrow": "arrow"}
_GRACE = timedelta(days=1)
_DAYS_SHOWN = 14


class RetentionError(RuntimeError):
    """A prune step failed after the prune had started (the report says how far it got)."""


class RetentionRefused(RetentionError):
    """The prune was refused before anything was deleted (bad input or an inconsistent store)."""


# --------------------------------------------------------------------------- store access
def _open_index(root) -> Tuple[Path, ReplayIndex]:
    root = Path(root)
    if not (root / "index.sqlite").is_file():
        raise RetentionRefused(f"{root} is not a replay store: no index.sqlite")
    if not (root / "objects").is_dir():
        raise RetentionRefused(f"{root} is not a replay store: no objects directory")
    return root, ReplayIndex(root / "index.sqlite")


def _object_files(root: Path) -> Iterator[Path]:
    for path in (root / "objects").rglob("*"):
        if path.is_file():
            yield path


def _check_keep_days(keep_days: Any) -> int:
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < MIN_KEEP_DAYS:
        raise RetentionRefused(f"keep_days must be a whole number of days >= {MIN_KEEP_DAYS}, got {keep_days!r}")
    return keep_days


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _session_end(session: SessionRecord) -> datetime:
    return _utc(session.ended_at if session.ended_at is not None else session.started_at)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else _utc(value).isoformat()


# --------------------------------------------------------------------------- statistics
def statistics(root) -> Dict[str, Any]:
    """What the store holds: sessions by status, object count and bytes on disk, date range, growth per day."""
    root, index = _open_index(root)
    try:
        sessions = index.list_sessions()
        by_status = Counter(s.status for s in sessions)
        files = 0
        object_bytes = 0
        by_day: Counter = Counter()
        for path in _object_files(root):
            stat = path.stat()
            if path.suffix in _SUFFIXES:
                files += 1
            object_bytes += stat.st_size
            by_day[datetime.fromtimestamp(stat.st_mtime, timezone.utc).date().isoformat()] += stat.st_size
        index_bytes = sum(p.stat().st_size for p in root.glob("index.sqlite*") if p.is_file())
        started = sorted(_utc(s.started_at) for s in sessions)
        return {
            "root": str(root),
            "sessions": len(sessions),
            "sessions_by_status": dict(sorted(by_status.items())),
            "objects": files,
            "object_bytes": object_bytes,
            "index_bytes": index_bytes,
            "total_bytes": object_bytes + index_bytes,
            "oldest_session": _iso(started[0]) if started else None,
            "newest_session": _iso(started[-1]) if started else None,
            "bytes_by_day": dict(sorted(by_day.items())[-_DAYS_SHOWN:]),
        }
    finally:
        index.close()


# --------------------------------------------------------------------------- planning
class _Scan:
    """One consistent look at the store: which sessions go, which object files go, and why not the rest."""

    def __init__(self) -> None:
        self.delete_sessions: List[SessionRecord] = []
        self.kept: List[SessionRecord] = []
        self.open: List[SessionRecord] = []
        self.delete_files: List[Tuple[str, Path, int]] = []       # (hash or file stem, path, bytes)
        self.kept_young: int = 0                                   # unreachable, but inside the age bound
        self.kept_reachable: int = 0
        self.bound: Optional[datetime] = None
        self.problems: List[str] = []


def _children(path: Path, problems: List[str]) -> Tuple[str, ...]:
    """The objects a JSON object links to ($frame / $ref); arrow frames link to nothing."""
    if path.suffix != ".json":
        return ()
    try:
        return tuple(frame_refs("json", path.read_bytes()))
    except Exception as exc:  # noqa: BLE001 -- reported; the caller refuses
        problems.append(f"object {path.name} cannot be read for its links: {type(exc).__name__}: {exc}")
        return ()


def _reachable(index: ReplayIndex, session_ids: List[str], paths: Dict[str, Path],
               problems: List[str], label: str) -> Set[str]:
    """Every object hash reachable from ``session_ids`` (their own records, then the links between objects)."""
    seen: Set[str] = set()
    pending: List[str] = []
    for session_id in session_ids:
        pending.extend(index.root_hashes(session_id))
    while pending:
        object_hash = pending.pop()
        if object_hash in seen:
            continue
        seen.add(object_hash)
        path = paths.get(object_hash)
        if path is None:
            problems.append(f"{label}: a referenced object {object_hash} is not on disk")
            continue
        pending.extend(_children(path, problems))
    return seen


def _scan(root: Path, index: ReplayIndex, keep_days: int, now: datetime) -> _Scan:
    scan = _Scan()
    cutoff = now - timedelta(days=keep_days)
    for session in index.list_sessions():
        if session.status == ReplayStatus.SESSION_OPEN:
            scan.open.append(session)
        elif _session_end(session) < cutoff:
            scan.delete_sessions.append(session)
        else:
            scan.kept.append(session)
    protected = scan.kept + scan.open
    starts = [_utc(s.started_at) for s in protected]
    scan.bound = min([cutoff, now - _GRACE] + starts)

    paths: Dict[str, Path] = {}
    sizes: Dict[Path, Tuple[int, float]] = {}
    for path in _object_files(root):
        stat = path.stat()
        sizes[path] = (stat.st_size, stat.st_mtime)
        if path.suffix in _SUFFIXES:
            paths[path.stem] = path
    reachable = _reachable(index, [s.session_id for s in protected], paths, scan.problems, "a kept or open session")
    bound_ts = scan.bound.timestamp()
    for path, (size, mtime) in sorted(sizes.items(), key=lambda kv: str(kv[0])):
        if path.suffix in _SUFFIXES and path.stem in reachable:
            scan.kept_reachable += 1
            continue
        if path.suffix not in _SUFFIXES and path.suffix != ".tmp":
            scan.problems.append(f"unexpected file in the object store: {path}")
            continue
        if mtime >= bound_ts:
            scan.kept_young += 1
            continue
        scan.delete_files.append((path.stem, path, size))
    return scan


def _report(scan: _Scan, keep_days: int, now: datetime, *, executed: bool) -> Dict[str, Any]:
    return {
        "executed": executed,
        "keep_days": keep_days,
        "cutoff": _iso(now - timedelta(days=keep_days)),
        "object_age_bound": _iso(scan.bound),
        "sessions_delete": [
            {"session_id": s.session_id, "status": s.status,
             "started_at": _iso(s.started_at), "ended_at": _iso(s.ended_at)}
            for s in scan.delete_sessions],
        "sessions_kept": len(scan.kept),
        "sessions_open_protected": [s.session_id for s in scan.open],
        "objects_delete": len(scan.delete_files),
        "bytes_delete": sum(size for _, _, size in scan.delete_files),
        "objects_kept_reachable": scan.kept_reachable,
        "objects_kept_young": scan.kept_young,
        "problems": list(scan.problems),
    }


def preview_prune(root, keep_days: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """What :func:`execute_prune` would delete. Deletes nothing. ``problems`` non-empty means execute would refuse."""
    keep_days = _check_keep_days(keep_days)
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    root, index = _open_index(root)
    try:
        return _report(_scan(root, index, keep_days, now), keep_days, now, executed=False)
    finally:
        index.close()


def execute_prune(root, keep_days: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Delete the sessions older than ``keep_days`` and then every unreachable, old-enough object file."""
    keep_days = _check_keep_days(keep_days)
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    root, index = _open_index(root)
    lock = root / LOCK_NAME
    try:
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RetentionRefused(f"another prune holds {lock} (delete the file only if no prune is running)") from None
        try:
            os.write(handle, f"pid={os.getpid()} at={now.isoformat()}\n".encode())
            os.close(handle)
            return _execute_locked(root, index, keep_days, now)
        finally:
            lock.unlink()
    finally:
        index.close()


def _execute_locked(root: Path, index: ReplayIndex, keep_days: int, now: datetime) -> Dict[str, Any]:
    first = _scan(root, index, keep_days, now)
    if first.problems:
        raise RetentionRefused("the store is inconsistent, nothing was deleted: " + "; ".join(first.problems))
    if first.delete_sessions:
        index.delete_sessions([s.session_id for s in first.delete_sessions])
        logger.info(f"replay retention: deleted {len(first.delete_sessions)} session(s) older than {keep_days} day(s)")
    # Look again with the sessions gone and BEFORE touching a file: what a live writer committed meanwhile counts.
    scan = _scan(root, index, keep_days, now)
    if scan.problems:
        raise RetentionRefused(
            f"the store became inconsistent after the sessions were deleted ({len(first.delete_sessions)} gone); "
            f"no object file was deleted: " + "; ".join(scan.problems))
    scan.delete_sessions = first.delete_sessions
    deleted: List[str] = []
    failures: List[str] = []
    freed = 0
    for object_hash, path, size in scan.delete_files:
        try:
            path.unlink()
        except FileNotFoundError:
            pass                                    # already gone: the goal state
        except OSError as exc:
            failures.append(f"{path.name}: {exc}")
            continue
        freed += size
        deleted.append(object_hash)
    index.delete_object_rows(deleted)
    report = _report(scan, keep_days, now, executed=True)
    report["objects_delete"] = len(deleted)
    report["bytes_delete"] = freed
    report["failures"] = failures
    logger.info(f"replay retention: deleted {len(deleted)} object file(s), {freed} bytes")
    if failures:
        raise RetentionError(f"{len(failures)} object file(s) could not be deleted: " + "; ".join(failures[:5])
                             + f" (the other {len(deleted)} were)")
    return report


# --------------------------------------------------------------------------- text
def human_bytes(count: float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def format_plan(plan: Dict[str, Any]) -> str:
    """The plan / result as text lines (the CLI prints it; the UI shows the same figures)."""
    verb = "deleted" if plan["executed"] else "would delete"
    lines = [f"keep {plan['keep_days']} day(s): sessions ended before {plan['cutoff']} go; "
             f"object files newer than {plan['object_age_bound']} are never touched"]
    if plan["problems"]:
        lines.append(f"REFUSED / would refuse -- {len(plan['problems'])} inconsistency(ies):")
        lines += [f"  - {p}" for p in plan["problems"]]
    lines.append(f"{verb} {len(plan['sessions_delete'])} session(s):")
    lines += [f"  - {s['session_id']} ({s['status']}, started {s['started_at']}, ended {s['ended_at']})"
              for s in plan["sessions_delete"]]
    lines.append(f"{verb} {plan['objects_delete']} object file(s), {human_bytes(plan['bytes_delete'])}")
    lines.append(f"kept: {plan['sessions_kept']} session(s), {len(plan['sessions_open_protected'])} open "
                 f"(protected), {plan['objects_kept_reachable']} reachable object(s), "
                 f"{plan['objects_kept_young']} unreferenced but too young to delete")
    return os.linesep.join(lines)
