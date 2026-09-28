"""Capture BOTH export payloads (expert_settings, ruleset) for EVERY backtest in a test DB.

Used to prove a refactor of the export derivation is byte-identical: run it before the change
and after, then diff the two files. Point it at a COPY of the test DB -- the test app's engine
sets WAL pragmas on connect.

Usage:
    DATABASE_URL=sqlite:////tmp/ba2-test-copy.db \
      ~/ba2-venvs/test/bin/python tools/export_golden_capture.py /tmp/out.json
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "testplatform", "backend")
# THIS checkout's packages first: the venv's editable installs may point at another checkout.
for p in (BACKEND, os.path.join(REPO, "testplatform"),
          *(os.path.join(REPO, "packages", n) for n in ("experts", "providers", "common"))):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(BACKEND)

if not os.environ.get("DATABASE_URL"):
    sys.exit("Set DATABASE_URL to a COPY of the test DB (sqlite:////tmp/ba2-test-copy.db)")

import app.models  # noqa: F401,E402  -- registers every model
from app.api.backtests import _derive_export_payload  # noqa: E402
from app.models.backtest import Backtest  # noqa: E402
from app.models.database import SessionLocal  # noqa: E402


def main() -> int:
    import ba2_common
    print(f"ba2_common from {ba2_common.__file__}")
    out_path = sys.argv[1]
    db = SessionLocal()
    out = {}
    try:
        ids = [row[0] for row in db.query(Backtest.id).order_by(Backtest.id).all()]
        for bt_id in ids:
            bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
            entry = {}
            for kind in ("expert_settings", "ruleset"):
                try:
                    entry[kind] = _derive_export_payload(bt, kind, db)
                except Exception as e:  # noqa: BLE001 -- the error IS the captured behaviour
                    entry[kind] = {"__error__": type(e).__name__,
                                   "detail": str(getattr(e, "detail", e))}
            out[str(bt_id)] = entry
    finally:
        db.close()
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
    print(f"captured {len(out)} backtests -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
