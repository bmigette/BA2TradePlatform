"""Nightly backup of the BA2 SQLite databases to the Google Drive backup folder.

WHAT.  For each database in ``DATABASES`` (the PROD trade DB, the DEV trade DB and the TEST/GA DB):
  1. copy it with SQLite's ONLINE BACKUP API (``Connection.backup``) into a local temp file --
     safe while the platform / a GA is writing, unlike copying the file (a WAL-mode DB copied
     mid-write is corrupt), and it needs no lock the writers would notice;
  2. ``PRAGMA quick_check`` the copy -- a backup nobody verified is not a backup;
  3. deflate it into ``<dest>/<name>_<YYYY-MM-DD>.sqlite.zip`` through a ``.part`` name and one
     ``os.replace`` (never a half-written archive under the final name);
  4. keep only the newest ``--keep`` (default 7) archives per database, oldest deleted.
Exit code 0 only if every database succeeded; one failure never stops the others.

WHERE.  ``--dest`` defaults to the operator's Drive backup folder ``G:\Mon Drive\backup\BA2``
(``G:`` is Google Drive for desktop, mounted per interactive session -- the scheduled task
therefore runs in the logged-on session).
The temp copy lands in ``--tmp`` (default: the system temp dir) and needs free space equal to
the largest DB (the test DB is ~14 GB); it is deleted after the archive is published.

WHEN.  Windows Task Scheduler ``BA2 DB Backup`` runs this daily at 00:00 with the dev venv
python from the repo root (registered 2026-09-15; see ``docs/RUNBOOK-goal2020-grid.md``).

Usage:
    .venv\\Scripts\\python.exe tools\\backup_dbs.py [--only prod,test] [--keep 7]
        [--dest "G:\\Mon Drive\\backup\\BA2"] [--tmp DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List

HOME = Path.home()
#: name -> live database path. Keep the names stable: they prefix the archive filenames the
#: retention sweep matches on.
DATABASES: Dict[str, Path] = {
    "prod": HOME / "Documents" / "ba2_trade_platform-prod" / "db.sqlite",
    "dev": HOME / "Documents" / "ba2" / "trade" / "db.sqlite",
    "test": HOME / "Documents" / "ba2" / "test" / "dl_forecasting.db",
}
DEFAULT_DEST = Path(r"G:\Mon Drive\backup\BA2")
DEFAULT_KEEP = 7
ARCHIVE_SUFFIX = ".sqlite.zip"
#: Pages per backup step; the sleep between steps lets a writer proceed (the API restarts a
#: step transparently if the source changed underneath it).
_PAGES_PER_STEP = 8192
_SLEEP_BETWEEN_STEPS_S = 0.0


def log(msg: str, dest: Path | None = None) -> None:
    line = f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    if dest is not None:
        try:
            with open(dest / "backup.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def archive_name(name: str, day: _dt.date) -> str:
    return f"{name}_{day:%Y-%m-%d}{ARCHIVE_SUFFIX}"


def _sidecars(db: Path) -> List[Path]:
    """The DB file plus the WAL/SHM siblings SQLite creates next to a WAL-mode database."""
    return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]


def remove_copy(db: Path) -> None:
    """Delete a temp copy and its sidecars. Raises on failure: a 14 GB leftover per night in the
    temp dir is exactly the kind of silent failure this script must not have."""
    for p in _sidecars(db):
        if p.exists():
            p.unlink()


def online_copy(src: Path, dst: Path) -> None:
    """SQLite online backup of ``src`` into a fresh file ``dst``.

    Connections are closed explicitly: ``with sqlite3.connect(...)`` only commits/rolls back,
    it does NOT close, and an open handle makes the later unlink fail on Windows."""
    remove_copy(dst)
    with contextlib.closing(sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)) as s,             contextlib.closing(sqlite3.connect(dst)) as d:
        s.backup(d, pages=_PAGES_PER_STEP, sleep=_SLEEP_BETWEEN_STEPS_S)
        # The header copied from a WAL-mode source leaves the copy in WAL mode; switch it back so
        # the archived file is a single self-contained file with nothing pending in a -wal.
        d.execute("PRAGMA journal_mode=DELETE")


def quick_check(path: Path) -> str:
    with contextlib.closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as c:
        return str(c.execute("PRAGMA quick_check").fetchone()[0])


def deflate(src: Path, final: Path, arcname: str) -> int:
    """Zip ``src`` into ``final`` (stored inside as ``arcname``) via a ``.part`` sibling;
    returns the archive size in bytes."""
    part = final.with_name(final.name + ".part")
    if part.exists():
        part.unlink()
    with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(src, arcname=arcname)
    os.replace(part, final)
    return final.stat().st_size


def prune(dest: Path, name: str, keep: int, dry_run: bool = False) -> List[Path]:
    """Delete all but the newest ``keep`` archives of ``name``. Sorted by the date in the
    filename (not mtime: Drive sync can rewrite mtimes)."""
    archives = sorted(dest.glob(f"{name}_????-??-??{ARCHIVE_SUFFIX}"), key=lambda p: p.name)
    victims = archives[:-keep] if keep > 0 else archives
    for p in victims:
        if not dry_run:
            try:
                p.unlink()
            except OSError as e:
                log(f"  prune: could not delete {p.name}: {e}", dest)
    return victims


def backup_one(name: str, src: Path, dest: Path, tmp_dir: Path, keep: int, day: _dt.date,
               dry_run: bool) -> bool:
    if not src.is_file():
        log(f"[{name}] MISSING source {src}", dest)
        return False
    final = dest / archive_name(name, day)
    size_mb = src.stat().st_size / 1048576
    log(f"[{name}] {src} ({size_mb:.0f} MB) -> {final.name}", dest)
    if dry_run:
        victims = prune(dest, name, keep, dry_run=True)
        log(f"[{name}] dry-run: would prune {[v.name for v in victims]}", dest)
        return True
    tmp_copy = tmp_dir / f"ba2_backup_{name}_{os.getpid()}.sqlite"
    t0 = time.monotonic()
    try:
        online_copy(src, tmp_copy)
        t1 = time.monotonic()
        verdict = quick_check(tmp_copy)
        if verdict != "ok":
            log(f"[{name}] FAILED quick_check on the copy: {verdict}", dest)
            return False
        t2 = time.monotonic()
        archived = deflate(tmp_copy, final, arcname=src.name)
        t3 = time.monotonic()
        log(f"[{name}] ok: copy {t1 - t0:.0f}s, check {t2 - t1:.0f}s, zip {t3 - t2:.0f}s -> "
            f"{archived / 1048576:.0f} MB ({archived / max(src.stat().st_size, 1) * 100:.0f}% of source)",
            dest)
    except Exception as e:  # noqa: BLE001 -- one DB failing must not stop the others; reported + exit 1
        log(f"[{name}] FAILED: {type(e).__name__}: {e}", dest)
        return False
    finally:
        try:
            remove_copy(tmp_copy)
        except OSError as e:
            log(f"[{name}] WARNING: temp copy left behind at {tmp_copy}: {e}", dest)
            ok_cleanup = False
        else:
            ok_cleanup = True
    if not ok_cleanup:
        return False
    victims = prune(dest, name, keep)
    if victims:
        log(f"[{name}] pruned {[v.name for v in victims]} (keep {keep})", dest)
    return True


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma list of database names (default: all)")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="archives to keep per database")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="destination folder (created if absent)")
    ap.add_argument("--tmp", default=None, help="temp dir for the online copy (default: system temp)")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    args = ap.parse_args(argv)
    if args.keep < 1:
        ap.error("--keep must be >= 1")

    dest = Path(args.dest)
    names = [n.strip() for n in args.only.split(",")] if args.only else list(DATABASES)
    unknown = [n for n in names if n not in DATABASES]
    if unknown:
        ap.error(f"unknown database(s) {unknown}; choose from {list(DATABASES)}")
    if not args.dry_run:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"cannot create destination {dest}: {e}", file=sys.stderr)
            return 2
    elif not dest.exists():
        print(f"(dry-run) destination {dest} does not exist yet; it would be created")
    tmp_dir = Path(args.tmp) if args.tmp else Path(tempfile.gettempdir())
    day = _dt.date.today()
    log(f"backup start: {names} -> {dest} (keep {args.keep}, tmp {tmp_dir})", dest if dest.exists() else None)
    ok = True
    for name in names:
        ok = backup_one(name, DATABASES[name], dest, tmp_dir, args.keep, day, args.dry_run) and ok
    log(f"backup {'OK' if ok else 'FAILED'}", dest if dest.exists() else None)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
