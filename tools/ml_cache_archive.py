"""Export/import the shared ML data (provider cache + backtest/optimize results DB) as a
single portable zip -- for moving a warmed-up cache to/from another machine, or backing it up
off the dev box (e.g. onto Google Drive) without hand-picking dozens of cache subfolders.

Sources (fixed, matching ba2_common.config's data layout -- see that file's "BA2 data root
layout" comment):
    cache: CACHE_FOLDER   (~/Documents/ba2/common/cache -- OHLCV/FMP/options/screener provider
                           cache, shared by both platforms)
    db:    TEST_DIR/dl_forecasting.db (~/Documents/ba2/test/dl_forecasting.db -- the backtest/
                           optimize results DB: backtests, strategies, job results)

The DB is live (WAL mode, actively written by a running server) so a raw file copy can grab an
inconsistent snapshot mid-write. Export always takes it via sqlite3's backup API instead, which
is the engine's own safe-copy path and does not require stopping the server.

Usage:
    python tools/ml_cache_archive.py export --dest-dir "G:\\Mon Drive\\Work\\AiTrading\\Test ML Cache"
    python tools/ml_cache_archive.py export --dest-dir <dir> --name my_export.zip --overwrite
    python tools/ml_cache_archive.py import --archive <path/to/export.zip>
    python tools/ml_cache_archive.py import --archive <path> --overwrite --force
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
from ba2_common.config import CACHE_FOLDER, TEST_DIR  # noqa: E402

DEFAULT_NAME = "ba2_ml_cache_export.zip"
DB_ARCNAME = "db/dl_forecasting.db"
CACHE_ARC_PREFIX = "cache/"
MANIFEST_ARCNAME = "manifest.json"


def _default_db_path() -> str:
    return os.path.join(TEST_DIR, "dl_forecasting.db")


def _human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}PB"


def _safe_sqlite_copy(src_path: str, dst_path: str) -> None:
    """Copy a (possibly live, WAL-mode) SQLite DB via the backup API -- safe under concurrent
    writers, unlike a raw file copy which can grab a torn mid-write snapshot."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _iter_cache_files(cache_dir: str):
    for root, _dirs, files in os.walk(cache_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, cache_dir).replace(os.sep, "/")
            yield full, CACHE_ARC_PREFIX + rel


def cmd_export(args: argparse.Namespace) -> None:
    if not os.path.isdir(CACHE_FOLDER):
        raise SystemExit(f"Cache folder not found: {CACHE_FOLDER}")
    db_path = _default_db_path()
    if not os.path.isfile(db_path):
        raise SystemExit(f"Results DB not found: {db_path}")

    os.makedirs(args.dest_dir, exist_ok=True)
    final_path = os.path.join(args.dest_dir, args.name)
    if os.path.exists(final_path) and not args.overwrite:
        raise SystemExit(f"{final_path} already exists. Pass --overwrite to replace it.")

    compression = zipfile.ZIP_DEFLATED if args.compress else zipfile.ZIP_STORED
    # Most of the cache is already-compressed parquet -- DEFLATE barely shrinks it and just
    # burns CPU over tens of GB, so STORED (no recompression) is the default.

    tmp_dir = tempfile.mkdtemp(prefix="ba2_ml_cache_export_")
    tmp_zip = os.path.join(tmp_dir, "export.zip.partial")
    tmp_db = os.path.join(tmp_dir, "dl_forecasting.db")
    t0 = time.monotonic()
    try:
        print(f"Snapshotting {db_path} via sqlite backup API...")
        _safe_sqlite_copy(db_path, tmp_db)
        db_size = os.path.getsize(tmp_db)

        print(f"Scanning {CACHE_FOLDER} ...")
        cache_files = list(_iter_cache_files(CACHE_FOLDER))
        cache_size = sum(os.path.getsize(f) for f, _ in cache_files)
        print(f"  {len(cache_files):,} files, {_human(cache_size)}")

        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_cache_dir": CACHE_FOLDER,
            "source_db_path": db_path,
            "cache_file_count": len(cache_files),
            "cache_bytes": cache_size,
            "db_bytes": db_size,
            "compression": "deflate" if args.compress else "stored",
        }

        print(f"Writing {tmp_zip} ...")
        with zipfile.ZipFile(tmp_zip, "w", compression=compression, allowZip64=True) as zf:
            zf.writestr(MANIFEST_ARCNAME, json.dumps(manifest, indent=2))
            zf.write(tmp_db, DB_ARCNAME)
            done = 0
            next_report = 2000
            for full, arcname in cache_files:
                zf.write(full, arcname)
                done += 1
                if done >= next_report:
                    print(f"  ...{done:,}/{len(cache_files):,} files")
                    next_report += 2000

        zip_size = os.path.getsize(tmp_zip)
        print(f"Moving to {final_path} ...")
        shutil.move(tmp_zip, final_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    elapsed = time.monotonic() - t0
    print(f"Done in {elapsed:.0f}s: {final_path} ({_human(zip_size)})")
    print(f"  cache: {len(cache_files):,} files, {_human(cache_size)}")
    print(f"  db:    {_human(db_size)}")


def cmd_import(args: argparse.Namespace) -> None:
    if not os.path.isfile(args.archive):
        raise SystemExit(f"Archive not found: {args.archive}")

    cache_dir = args.cache_dir or CACHE_FOLDER
    db_path = args.db_path or _default_db_path()

    with zipfile.ZipFile(args.archive, "r") as zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_ARCNAME))
            print("Archive manifest:")
            for k, v in manifest.items():
                print(f"  {k}: {v}")
        except KeyError:
            manifest = None
            print("(no manifest.json in this archive -- proceeding without it)")

        # Refuse to clobber a DB that looks like it's currently attached to a running server
        # (WAL/SHM sidecars present) unless the caller explicitly forces it.
        wal_sidecars = [db_path + suffix for suffix in ("-wal", "-shm")
                        if os.path.exists(db_path + suffix)]
        if wal_sidecars and not args.force:
            raise SystemExit(
                f"{db_path} has active WAL sidecars ({', '.join(wal_sidecars)}) -- looks like a "
                "server is running against it. Stop it first, or pass --force to override.")

        if not args.overwrite:
            conflicts = []
            if os.path.exists(db_path):
                conflicts.append(db_path)
            if os.path.isdir(cache_dir) and os.listdir(cache_dir):
                conflicts.append(cache_dir + " (non-empty)")
            if conflicts:
                raise SystemExit(
                    "Refusing to import over existing data (pass --overwrite to proceed): "
                    + "; ".join(conflicts))

        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        print(f"Restoring DB -> {db_path}")
        with zf.open(DB_ARCNAME) as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst)

        print(f"Restoring cache -> {cache_dir}")
        members = [m for m in zf.namelist() if m.startswith(CACHE_ARC_PREFIX)]
        for i, member in enumerate(members, 1):
            rel = member[len(CACHE_ARC_PREFIX):]
            if not rel:
                continue
            target = os.path.join(cache_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if i % 2000 == 0:
                print(f"  ...{i:,}/{len(members):,} files")

    print(f"Done: restored {len(members):,} cache files + DB.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="Archive the provider cache + results DB into one zip.")
    p_export.add_argument("--dest-dir", required=True, help="Directory to write the zip into.")
    p_export.add_argument("--name", default=DEFAULT_NAME, help=f"Zip filename (default: {DEFAULT_NAME}).")
    p_export.add_argument("--overwrite", action="store_true",
                          help="Replace the destination zip if it already exists.")
    p_export.add_argument("--compress", action="store_true",
                          help="Use DEFLATE instead of STORED (slower, saves little on parquet).")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="Restore a cache+DB export back onto this machine.")
    p_import.add_argument("--archive", required=True, help="Path to the export zip.")
    p_import.add_argument("--cache-dir", default=None,
                          help=f"Restore target for the cache (default: {CACHE_FOLDER}).")
    p_import.add_argument("--db-path", default=None,
                          help=f"Restore target for the DB (default: {_default_db_path()}).")
    p_import.add_argument("--overwrite", action="store_true",
                          help="Proceed even if the target DB/cache already has content.")
    p_import.add_argument("--force", action="store_true",
                          help="Proceed even if the target DB looks like it's live (WAL sidecars present).")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
