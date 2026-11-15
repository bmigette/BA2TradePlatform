#!/usr/bin/env python
"""Load walk-forward report directories into the test DB's ``walk_forward_runs`` table (the UI's
Walk-forward page), e.g. a ``reports/goal2027_wf/`` copied from the host that ran the grid.

  python tools/import_wf_reports.py reports/goal2027_wf            # a grid dir (summary.json + cells)
  python tools/import_wf_reports.py reports/goal2027_wf/<cell>     # one chain's directory
  python tools/import_wf_reports.py reports/walk_forward/<prefix>  # a tools/run_walk_forward.py report

Reads ``cell.json`` / ``report.json`` / ``fold<k>.json`` / ``summary.json`` (app.services.wf_store);
never writes them. IDEMPOTENT: an unchanged chain is left alone, a changed one updated. A row this DB's
own runs wrote (the UI, or the grid on this host) is never overwritten unless --replace-live. Imported
rows are view-only: they carry no optimize args, so they cannot be resumed here. Their train job and
out-of-sample backtest links appear only when those rows exist in THIS DB with the recorded id/name.
The DB is the test platform's (DATABASE_URL, default BA2_HOME/test/dl_forecasting.db).
"""
from __future__ import annotations

import argparse
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO, "testplatform", "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("report_dir", nargs="+")
    ap.add_argument("--replace-live", action="store_true",
                    help="also overwrite rows this DB's own runs wrote (source ui / cli)")
    ap.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    a = ap.parse_args(argv)

    import app.models  # noqa: F401 -- register every model
    from app.models.database import DATABASE_URL, init_db
    from app.services import wf_store
    from app.services.walk_forward import WalkForwardError

    init_db()                                  # the table exists (migration 034)
    print(f"import_wf_reports: DB {DATABASE_URL}")
    rc = 0
    for d in a.report_dir:
        try:
            done = wf_store.import_report_dir(d, replace_live=a.replace_live, dry_run=a.dry_run)
        except (WalkForwardError, OSError, ValueError, KeyError) as e:
            print(f"  {d}: REFUSED: {type(e).__name__}: {e}", file=sys.stderr)
            rc = 2
            continue
        for name, action in done:
            print(f"  {action:10s} {name}")
        print(f"{d}: {len(done)} chain(s)" + (" (dry run, nothing written)" if a.dry_run else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
