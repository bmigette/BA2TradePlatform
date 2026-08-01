"""Step 1 of the S6 TOP2/TOP4 -> dev-instance deploy: dump the export payloads (ruleset +
expert_settings) for the two chosen Backtest rows to a JSON file, read-only against the TEST
platform DB. A separate script (import_senate_deploy_payload.py) reads that file and writes to
the LIVE trade DB -- kept as two processes so a read against the test DB and a write against the
live DB never share one process's DB-module global state.

Usage: python tools/export_senate_deploy_payload.py <out.json>
"""
import json
import os
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
BACKEND = os.path.join(REPO, "testplatform", "backend")
for p in (BACKEND, os.path.join(REPO, "testplatform")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(BACKEND)

from app.models.database import DATABASE_URL as _DB_URL  # noqa: E402
if _DB_URL.startswith("sqlite:///"):
    from ba2_common.core import db as _ba2_db  # noqa: E402
    _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))

import app.models  # noqa: F401,E402
from app.models.database import SessionLocal  # noqa: E402
from app.models.backtest import Backtest  # noqa: E402
from app.api.backtests import _derive_export_payload  # noqa: E402

# (backtest_id, target_instance_id, deploy_label)
PLAN = [
    (842, 29, "sen5min3-S6top2"),
    (845, 28, "sen5min3-S6top4"),
]


def main() -> int:
    out_path = sys.argv[1]
    db = SessionLocal()
    payloads = []
    for bt_id, inst_id, label in PLAN:
        bt = db.query(Backtest).filter_by(id=bt_id).first()
        if bt is None:
            print(f"FATAL: backtest {bt_id} not found")
            return 1
        ruleset = _derive_export_payload(bt, "ruleset", db)
        settings = _derive_export_payload(bt, "expert_settings", db)
        payloads.append({
            "backtest_id": bt_id,
            "target_instance_id": inst_id,
            "label": label,
            "ruleset": ruleset,
            "settings": settings,
        })
        print(f"backtest {bt_id} -> instance {inst_id} ({label}): "
              f"{len(ruleset['entry_rules'])} entry rules, {len(ruleset['exit_rules'])} exit rules, "
              f"{len(settings['settings']['expert_params'])} expert params")

    with open(out_path, "w") as f:
        json.dump(payloads, f, indent=2, default=str)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
