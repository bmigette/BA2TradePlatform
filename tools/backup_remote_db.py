"""Weekly backup of a REMOTE SQLite database (the remote227 stage-1 isolated DB) into the Drive
backup folder, next to the nightly archives from ``backup_dbs.py``.

WHAT.  For each entry in ``REMOTES``:
  1. over ONE ssh session, run a small python3 program on the host that copies the live DB with
     SQLite's online-backup API (safe while the GA writes), ``PRAGMA quick_check``s the copy,
     switches it to journal_mode=DELETE and zips it under the source filename into the host's
     scratch dir (``remote_tmp``);
  2. ``scp`` the archive here as ``<dest>/<name>_<YYYY-MM-DD>.sqlite.zip.part``, then one
     ``os.replace`` to the final name;
  3. delete the remote archive; keep only the newest ``--keep`` (default 4) local archives.
Exit 0 only if every remote succeeded. Logs to ``<dest>/backup.log`` (shared with the nightly).

WHY a separate script.  The nightly copies LOCAL files; this one needs a remote execution
step and a transfer, and it runs weekly (the stage-1 results only change at generation
boundaries and live nowhere else -- operator 2026-09-15: "we should make a weekly backup just
in case"). Retention/logging helpers are imported from ``backup_dbs``.

WHEN.  Windows Task Scheduler ``BA2 Remote DB Backup`` runs it weekly, Sunday 01:00, as the
interactive user (the Drive ``G:`` and the ssh keys are per-session).

Usage:
    .venv\\Scripts\\python.exe tools\\backup_remote_db.py [--only remote227-stage1] [--keep 4]
        [--dest "G:\\Mon Drive\\backup\\BA2"] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup_dbs import DEFAULT_DEST, archive_name, log, prune  # noqa: E402

DEFAULT_KEEP = 4
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=30"]
#: name -> {host, db, remote_tmp}. Names prefix the archive filenames the retention matches on.
REMOTES: Dict[str, Dict[str, str]] = {
    "remote227-stage1": {
        "host": "debian@141.94.199.227",
        "db": "/home/debian/ba2-grid/home/test/dl_forecasting.db",
        "remote_tmp": "/home/debian/ba2-grid/backup",
    },
}

#: Runs on the host under python3 (stdin). argv: db, out_zip. Prints "OK <bytes>" on success.
_REMOTE_PROGRAM = r'''
import contextlib, os, sqlite3, sys, zipfile
db, out = sys.argv[1], sys.argv[2]
copy = out + ".copy.sqlite"
os.makedirs(os.path.dirname(out), exist_ok=True)
for p in (copy, copy + "-wal", copy + "-shm", out, out + ".part"):
    if os.path.exists(p):
        os.unlink(p)
with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % db, uri=True)) as s, \
        contextlib.closing(sqlite3.connect(copy)) as d:
    s.backup(d, pages=8192)
    d.execute("PRAGMA journal_mode=DELETE")
with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % copy, uri=True)) as c:
    v = c.execute("PRAGMA quick_check").fetchone()[0]
if v != "ok":
    print("QUICK_CHECK_FAILED " + str(v))
    sys.exit(3)
with zipfile.ZipFile(out + ".part", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    z.write(copy, arcname=os.path.basename(db))
os.replace(out + ".part", out)
for p in (copy, copy + "-wal", copy + "-shm"):
    if os.path.exists(p):
        os.unlink(p)
print("OK %d" % os.path.getsize(out))
'''


def _run(cmd: List[str], stdin: Optional[str] = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)


def remote_archive(host: str, db: str, out_zip: str) -> int:
    """Build the archive on the host; returns its size in bytes. Raises RuntimeError on failure."""
    cmd = ["ssh", *SSH_OPTS, host, f"nice -n 10 python3 - {shlex.quote(db)} {shlex.quote(out_zip)}"]
    r = _run(cmd, stdin=_REMOTE_PROGRAM)
    tail = (r.stdout.strip().splitlines() or [""])[-1]
    if r.returncode != 0 or not tail.startswith("OK "):
        raise RuntimeError(f"remote step rc={r.returncode}: {tail or r.stderr.strip()[-400:]}")
    return int(tail.split()[1])


def fetch(host: str, remote_path: str, final: Path) -> int:
    part = final.with_name(final.name + ".part")
    if part.exists():
        part.unlink()
    r = _run(["scp", *SSH_OPTS, "-q", f"{host}:{remote_path}", str(part)])
    if r.returncode != 0:
        raise RuntimeError(f"scp rc={r.returncode}: {r.stderr.strip()[-400:]}")
    os.replace(part, final)
    return final.stat().st_size


def remote_cleanup(host: str, remote_path: str) -> None:
    _run(["ssh", *SSH_OPTS, host, f"rm -f {shlex.quote(remote_path)}"], timeout=120)


def backup_one(name: str, spec: Dict[str, str], dest: Path, keep: int, day: _dt.date,
               dry_run: bool) -> bool:
    final = dest / archive_name(name, day)
    remote_zip = f"{spec['remote_tmp']}/{final.name}"
    log(f"[{name}] {spec['host']}:{spec['db']} -> {final.name}", dest)
    if dry_run:
        victims = prune(dest, name, keep, dry_run=True)
        log(f"[{name}] dry-run: would prune {[v.name for v in victims]}", dest)
        return True
    t0 = time.monotonic()
    try:
        remote_size = remote_archive(spec["host"], spec["db"], remote_zip)
        t1 = time.monotonic()
        local_size = fetch(spec["host"], remote_zip, final)
        t2 = time.monotonic()
        if local_size != remote_size:
            final.unlink()
            raise RuntimeError(f"size mismatch after transfer: remote {remote_size} local {local_size}")
        log(f"[{name}] ok: remote copy+zip {t1 - t0:.0f}s, transfer {t2 - t1:.0f}s -> "
            f"{local_size / 1048576:.1f} MB", dest)
    except Exception as e:  # noqa: BLE001 -- one remote failing must not stop the others; exit 1
        log(f"[{name}] FAILED: {type(e).__name__}: {e}", dest)
        return False
    finally:
        remote_cleanup(spec["host"], remote_zip)
    victims = prune(dest, name, keep)
    if victims:
        log(f"[{name}] pruned {[v.name for v in victims]} (keep {keep})", dest)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma list of remote names (default: all)")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="archives to keep per remote")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="destination folder (created if absent)")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    args = ap.parse_args(argv)
    if args.keep < 1:
        ap.error("--keep must be >= 1")
    dest = Path(args.dest)
    names = [n.strip() for n in args.only.split(",")] if args.only else list(REMOTES)
    unknown = [n for n in names if n not in REMOTES]
    if unknown:
        ap.error(f"unknown remote(s) {unknown}; choose from {list(REMOTES)}")
    if not args.dry_run:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"cannot create destination {dest}: {e}", file=sys.stderr)
            return 2
    day = _dt.date.today()
    log(f"remote backup start: {names} -> {dest} (keep {args.keep})", dest if dest.exists() else None)
    ok = True
    for name in names:
        ok = backup_one(name, REMOTES[name], dest, args.keep, day, args.dry_run) and ok
    log(f"remote backup {'OK' if ok else 'FAILED'}", dest if dest.exists() else None)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
