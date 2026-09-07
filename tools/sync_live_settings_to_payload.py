#!/usr/bin/env python
"""Make a live instance's SETTINGS exactly what its backtest ran, and nothing else.

    BA2_LIVE_DB=... python tools/sync_live_settings_to_payload.py payload.json
    BA2_LIVE_DB=... python tools/sync_live_settings_to_payload.py payload.json --apply

WHY NOT import_deploy_payload. That tool is a DEPLOY: it converts the rule trees, imports them
as NEW Ruleset + EventAction rows and repoints the instance, orphaning the old ones. When the
rulesets already match -- the 2026-09-07 parity review checked all 22 rules across the six live
instances and found them identical -- a redeploy churns the ruleset tables for nothing and makes
the diff that matters impossible to read. This tool touches settings only.

WHAT "EXACTLY AS BACKTEST" MEANS HERE. The comparison target is the shipped export contract,
``_derive_export_payload``, which is the same thing a redeploy would apply:

    expert defaults + the optimization's base expert settings + persisted fixed settings
    + the decoded model:* genes + the screener block (canonicalised onto the live names)
    + BACKTEST_FORCED_SETTINGS (the gates and pins the engine forces on every trial)

Values are compared through the expert's OWN settings reader, not against raw column contents,
because the reader is what the live code path actually sees -- a row can be present and still
read as something else (which is how a bool stored as "1" read False for months).

Reads first, writes second, and prints every difference either way: a settings sync that cannot
be reviewed before it runs is how a live sleeve silently changes strategy.
"""
import argparse
import json
import os
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
for p in (REPO, os.path.join(REPO, "packages", "experts")):
    if p not in sys.path:
        sys.path.insert(0, p)

LIVE_DB = os.environ.get("BA2_LIVE_DB", os.path.expanduser(r"~\Documents\ba2\trade\db.sqlite"))

#: Settings the payload cannot speak to, which must NOT be reverted by a sync.
#:
#: execution_schedule_* is derived by the importer (and repaired by fix_live_schedules) rather
#: than being an expert_param; virtual_equity_pct is a portfolio allocation decision, not a
#: property of the backtest. Overwriting either from a payload would undo a deliberate operator
#: choice under the banner of parity.
NEVER_SYNC = {
    "execution_schedule_enter_market",
    "execution_schedule_open_positions",
    "virtual_equity_pct",
}


def _equal(a, b) -> bool:
    """Same VALUE, tolerating the int/float and bool/int spellings the two sides use."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) <= 1e-9
    except (TypeError, ValueError):
        return a == b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("payload")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ns = ap.parse_args()

    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(LIVE_DB)
    from ba2_common.core.db import get_instance
    from ba2_common.core.deploy_parity import (
        live_settings_from_universe, unmapped_screener_keys,
    )
    from ba2_common.core.models import ExpertInstance
    from ba2_trade_platform.modules.experts import experts as live_experts

    with open(ns.payload) as f:
        payloads = json.load(f)

    print(f"LIVE_DB = {LIVE_DB}\nmode    = {'APPLY' if ns.apply else 'DRY RUN'}\n")
    total = 0
    for entry in payloads:
        inst_id = entry["target_instance_id"]
        inst = get_instance(ExpertInstance, inst_id)
        if inst is None:
            print(f"  !! instance {inst_id} not found")
            continue
        cls = next((c for c in live_experts if c.__name__ == entry["expert_name"]), None)
        if cls is None:
            print(f"  !! expert {entry['expert_name']!r} not in the live registry")
            continue
        expert = cls(inst_id)

        want = dict(entry["settings"]["settings"]["expert_params"])
        universe = entry["settings"].get("universe")
        want.update(live_settings_from_universe(universe))
        stray = unmapped_screener_keys(universe)
        if stray:
            print(f"  inst {inst_id}: WARNING screener key(s) nothing reads: {stray}")

        live = expert.settings
        diffs = []
        for key, value in sorted(want.items()):
            if key in NEVER_SYNC:
                continue
            cur = live.get(key)
            if cur is None or not _equal(cur, value):
                diffs.append((key, cur, value))

        print(f"=== instance {inst_id} ({entry['expert_name']}, backtest {entry['backtest_id']})"
              f" -- {len(want)} setting(s) from the payload")
        if not diffs:
            print("  already identical to the backtest")
        for key, cur, value in diffs:
            print(f"  {key}: live={cur!r} -> backtest={value!r}")
            total += 1
            if ns.apply:
                expert.save_settings({key: (value, None)})

        for k, v in (entry["settings"].get("backtest_only") or {}).items():
            print(f"  BACKTEST-ONLY, no live analogue, not applied: {k}={v!r}")
        print()

    print(f"{total} setting(s) {'written' if ns.apply else 'would change'}.")
    if total and ns.apply:
        print("POST /api/reload so the instance/settings caches are dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
