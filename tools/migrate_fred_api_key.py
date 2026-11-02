"""Move a FRED API key stored under the legacy AppSetting name ``FRED_API_KEY`` to the
canonical ``fred_api_key``. Idempotent: a second run reports ``nothing-to-do``.

The test platform's settings page used to save the key as ``FRED_API_KEY``, which nothing
reads (see ``ba2_common.core.fred_api_key``). Run this once per settings DB that may carry
such a row. It uses a plain ``sqlite3`` connection, so it works with the app running.

Usage:
    python tools/migrate_fred_api_key.py --db C:/Users/<you>/Documents/ba2/test/dl_forecasting.db
    python tools/migrate_fred_api_key.py --db <db> --dry-run   # report, change nothing
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))

from ba2_common.core.fred_api_key import (  # noqa: E402
    FRED_API_KEY_SETTING, LEGACY_FRED_API_KEY_SETTINGS, migrate_legacy_fred_api_key)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, nargs="+", help="settings DB(s) to migrate")
    ap.add_argument("--dry-run", action="store_true", help="report what would change")
    args = ap.parse_args(argv)

    names = (FRED_API_KEY_SETTING,) + tuple(LEGACY_FRED_API_KEY_SETTINGS)
    marks = ",".join("?" * len(names))
    for db in args.db:
        if not os.path.exists(db):
            print(f"{db}: MISSING")
            return 1
        conn = sqlite3.connect(db, timeout=30)
        try:
            present = [r[0] for r in conn.execute(
                f"SELECT key FROM appsetting WHERE key IN ({marks})", names)]
            outcome = migrate_legacy_fred_api_key(conn)
            if args.dry_run:
                conn.rollback()
                print(f"{db}: present={present} -> would be: {outcome} (dry run)")
            else:
                conn.commit()
                print(f"{db}: present={present} -> {outcome}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
