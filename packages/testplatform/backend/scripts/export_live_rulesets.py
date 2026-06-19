"""Export in-scope dev-account experts' LIVE rulesets to JSON (for S1 of the optimization plan).

Calls the READ-ONLY live-ruleset importers in ``app.api.ruleset_meta``
(``_read_live_enter_market_trees`` + ``_read_live_open_positions_rules``) against the dev trade
DB and writes, per expert, a single ``{label}.json`` holding the optimizable
``buy_entry_conditions`` / ``sell_entry_conditions`` trees and the ``exit_conditions`` rules —
exactly the shape the Backtesting UI's "Import JSON" expects and the S1 strategy builder consumes.

The live DB is opened ``mode=ro`` (never written). Default source = the dev DB at
``ba2_common.config.DB_FILE``; override with ``--live-db``. Default output = ``docs/live_rulesets/``
(tracked, syncs across machines); use ``--out`` for a temp folder instead.

Run (test venv):
    python backend/scripts/export_live_rulesets.py
    python backend/scripts/export_live_rulesets.py --out %TEMP%\ba2_rulesets --live-db <path>
"""
from __future__ import annotations

import argparse
import json
import os
import sys


# Representative dev-account ExpertInstance ids per in-scope equity expert (account 3).
# FactorRanker is intentionally absent: it has no enter/open ruleset (factor-model bypass), so
# its S1 is its factor-weight/top-N params, not an imported ruleset.
DEFAULT_EXPERTS: dict[str, int] = {
    # label                 instance_id  (alias / rulesets)
    "FMPRating": 5,             # FMP-nas30-cons-hc: enter rs#10 "High Conviction", open rs#11
    "FMPEarningsDrift": 38,     # EarningsDrift:     enter rs#41 (PEAD),            open rs#42
    "FMPInsiderClusterBuy": 37, # InsiderCluster:    enter rs#39 (ICB),             open rs#40
}


def _enter_backend() -> str:
    """Put ``backend/`` on the path so ``import app...`` resolves (mirrors ba2test_launcher)."""
    here = os.path.dirname(os.path.abspath(__file__))
    backend = os.path.dirname(here)  # backend/scripts -> backend
    if backend not in sys.path:
        sys.path.insert(0, backend)
    return backend


def main(argv: list[str] | None = None) -> int:
    _enter_backend()
    from ba2_common.config import DB_FILE as DEV_DB
    from app.api.ruleset_meta import (
        _read_live_enter_market_trees,
        _read_live_open_positions_rules,
    )

    repo_root = os.path.dirname(_enter_backend())
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live-db", default=DEV_DB, help="Live trade DB to read (default: dev DB_FILE).")
    ap.add_argument("--out", default=os.path.join(repo_root, "docs", "live_rulesets"),
                    help="Output folder (default: docs/live_rulesets).")
    ap.add_argument("--experts", default=None,
                    help="Override map as label=instance_id,label=instance_id (default: in-scope set).")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.live_db):
        sys.exit(f"export-rulesets: live DB not found: {args.live_db}")
    experts = dict(DEFAULT_EXPERTS)
    if args.experts:
        experts = {}
        for pair in args.experts.split(","):
            label, _, sid = pair.partition("=")
            experts[label.strip()] = int(sid)

    os.makedirs(args.out, exist_ok=True)
    written = []
    for label, instance_id in experts.items():
        trees = _read_live_enter_market_trees(args.live_db, instance_id)
        exit_rules = _read_live_open_positions_rules(args.live_db, instance_id)
        payload = {
            "expert": label,
            "live_instance_id": instance_id,
            "buy_entry_conditions": trees.get("buy_entry_conditions"),
            "sell_entry_conditions": trees.get("sell_entry_conditions"),
            "exit_conditions": exit_rules,
        }
        path = os.path.join(args.out, f"{label}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        n_buy = len((trees.get("buy_entry_conditions") or {}).get("conditions", []) or [])
        n_sell = len((trees.get("sell_entry_conditions") or {}).get("conditions", []) or [])
        print(f"  {label:<22} instance {instance_id:>2}: "
              f"buy_gates={n_buy} sell_gates={n_sell} exit_rules={len(exit_rules)} -> {path}")
        written.append(path)

    print(f"\nexport-rulesets: wrote {len(written)} file(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
