"""Step 1 of a backtest -> live-instance deploy: dump the export payloads (ruleset +
expert_settings) for the chosen Backtest rows to a JSON file, read-only against the TEST platform
DB. A separate script (import_deploy_payload.py) reads that file and writes to the LIVE trade DB
-- kept as two processes so a read against the test DB and a write against the live DB never
share one process's DB-module global state.

Generalised 2026-08-09 from the Senate-only version: the plan comes from the command line rather
than a hardcoded constant, and the target instance may be NEW (pass ``new`` instead of an id, plus
--account) so a first-of-its-kind deploy no longer needs a hand-made ExpertInstance row.

Usage:
    python tools/export_deploy_payload.py <out.json> <backtest_id>:<instance_id|new>:<label> [...]
        [--account N] [--equity-pct P]

Example (deploy backtest 881 as a NEW instance on account 3, 10% virtual equity):
    python tools/export_deploy_payload.py /tmp/dep.json 881:new:goal2020-mid_ED_S1top1         --account 3 --equity-pct 10
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

def _parse_plan(argv):
    """['881:new:label', '842:29:other'] -> [(bt_id, inst_id|None, label)]."""
    plan = []
    for spec in argv:
        parts = spec.split(":")
        if len(parts) != 3:
            raise SystemExit(f"bad plan entry {spec!r}; want <backtest_id>:<instance_id|new>:<label>")
        bt, inst, label = parts
        plan.append((int(bt), None if inst.lower() == "new" else int(inst), label))
    return plan


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out_path")
    ap.add_argument("plan", nargs="+", help="<backtest_id>:<instance_id|new>:<label>")
    ap.add_argument("--account", type=int, default=None,
                    help="account_id for entries whose instance is 'new'")
    ap.add_argument("--equity-pct", type=float, default=10.0,
                    help="virtual_equity_pct for NEW instances (default 10)")
    args = ap.parse_args()
    out_path = args.out_path
    PLAN = _parse_plan(args.plan)
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
            "target_instance_id": inst_id,          # None -> import creates the instance
            "account_id": args.account,
            "virtual_equity_pct": args.equity_pct,
            "expert_name": bt.expert_name,
            "label": label,
            "ruleset": ruleset,
            "settings": settings,
        })
        print(f"backtest {bt_id} -> instance {inst_id or 'NEW'} ({label}) [{bt.expert_name}]: "
              f"{len(ruleset['entry_rules'])} entry rules, {len(ruleset['exit_rules'])} exit rules, "
              f"{len(settings['settings']['expert_params'])} expert params")

    with open(out_path, "w") as f:
        json.dump(payloads, f, indent=2, default=str)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
