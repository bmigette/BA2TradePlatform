"""Step 2 of a backtest -> live-instance deploy: read the payload dumped by
export_deploy_payload.py and write it into the LIVE trade DB.

Generalised 2026-08-09 from the Senate-only version: works for ANY expert (the class is resolved
from the payload's expert_name via the live registry instead of being hardcoded), and CREATES the
ExpertInstance when ``target_instance_id`` is null so a first-of-its-kind deploy needs no
hand-made row.

allow_automated_trade_opening is forced ON for a freshly created instance: it defaults to False,
and an instance that silently never places an order looks identical to one whose strategy simply
found no setup -- a trap this project has already hit once.

OPERATOR NOTE (2026-09-03): every live O_CC / O_WHEEL ExpertInstance deployed BEFORE 2026-09-03
must be re-exported and re-imported through this pair of tools (then POST /api/reload): the live
option lifecycle pass no longer closes the written call at the roll window -- the ruleset's
`cc_dte` rule (repository-resolved, identical in backtest and live) owns that exit, and an old
payload does not carry it. The forced/derived settings the backtest handler applies now travel in
the payload through the shared table in ba2_common.core.deploy_parity (pinned by
testplatform/backend/tests/backtest/test_deploy_round_trip_parity.py).

For each entry: converts entry/exit TradeRule lists to a live ruleset export via
``trade_rules_to_live_export``, imports it as NEW Ruleset+EventAction rows via
``RulesImporter.import_multiple_rulesets`` (never touches the existing rulesets -- old ones are
left orphaned, not deleted, so this is reversible), repoints the target ExpertInstance's
enter_market_ruleset_id/open_positions_ruleset_id at the new rulesets, and writes the expert_params
via the expert's own ``save_settings`` (so value typing follows get_settings_definitions exactly,
same as any other settings save through the app).

Usage: python tools/import_deploy_payload.py <payload.json>
"""
import json
import os
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
for p in (REPO, os.path.join(REPO, "packages", "experts")):
    if p not in sys.path:
        sys.path.insert(0, p)

LIVE_DB = os.environ.get("BA2_LIVE_DB", os.path.expanduser(r"~\Documents\ba2\trade\db.sqlite"))

from ba2_common.core import db as _ba2_db  # noqa: E402
_ba2_db.configure_db(LIVE_DB)

from ba2_common.core.db import add_instance, get_instance, update_instance  # noqa: E402
from ba2_common.core.deploy_parity import (  # noqa: E402
    SCREENER_UNIVERSE_SETTING, live_settings_from_universe,
)
from ba2_common.core.models import ExpertInstance  # noqa: E402
from ba2_common.core.rules_convert import trade_rules_to_live_export  # noqa: E402
from ba2_common.core.rules_export_import import RulesImporter  # noqa: E402


def _expert_class(name: str):
    """Resolve an expert class by name from the LIVE registry (no hardcoded import)."""
    from ba2_trade_platform.modules.experts import experts as _live_experts
    for cls in _live_experts:
        if cls.__name__ == name:
            return cls
    raise SystemExit(f"expert {name!r} not found in the live registry")


def main() -> int:
    payload_path = sys.argv[1]
    with open(payload_path) as f:
        payloads = json.load(f)

    print(f"LIVE_DB = {LIVE_DB}")
    for entry in payloads:
        inst_id = entry["target_instance_id"]
        label = entry["label"]
        bt_id = entry["backtest_id"]
        print(f"\n=== {label}: backtest {bt_id} -> instance {inst_id} ===")

        expert_name = entry["expert_name"]   # payload is authoritative; no silent default
        created = False
        if inst_id is None:
            acct = entry.get("account_id")
            if acct is None:
                print("FATAL: target_instance_id is null but no account_id in payload")
                return 1
            inst = ExpertInstance(
                account_id=int(acct), expert=expert_name, alias=label,
                virtual_equity_pct=float(entry.get("virtual_equity_pct") or 10.0),
                enabled=True,
            )
            inst_id = add_instance(inst)
            inst = get_instance(ExpertInstance, inst_id)
            created = True
            print(f"CREATED ExpertInstance {inst_id} ({expert_name}, account {acct}, "
                  f"{inst.virtual_equity_pct:g}% virtual equity)")
        else:
            inst = get_instance(ExpertInstance, inst_id)
            if inst is None:
                print(f"FATAL: ExpertInstance {inst_id} not found in {LIVE_DB}")
                return 1
            if inst.expert != expert_name:
                print(f"FATAL: instance {inst_id} is expert={inst.expert!r}, expected {expert_name!r}")
                return 1
        old_enter, old_open = inst.enter_market_ruleset_id, inst.open_positions_ruleset_id
        print(f"current rulesets: enter={old_enter} open={old_open}")

        entry_rules = entry["ruleset"]["entry_rules"]
        exit_rules = entry["ruleset"]["exit_rules"]
        live_export = trade_rules_to_live_export(entry_rules, exit_rules, name=label)
        n_rulesets = len(live_export["rulesets"])
        print(f"live_export: {n_rulesets} ruleset(s) "
              f"({[r['subtype'] for r in live_export['rulesets']]})")

        ruleset_ids, warnings = RulesImporter.import_multiple_rulesets(live_export)
        for w in warnings:
            print(f"  warning: {w}")
        by_subtype = dict(zip((r["subtype"] for r in live_export["rulesets"]), ruleset_ids))
        new_enter = by_subtype.get("enter_market")
        new_open = by_subtype.get("open_positions")
        print(f"created rulesets: enter={new_enter} open={new_open}")

        inst.enter_market_ruleset_id = new_enter
        inst.open_positions_ruleset_id = new_open
        inst.user_description = (
            f"Deployed from {label} (backtest {bt_id}). "
            + ("Created by import_deploy_payload." if created
               else f"Old rulesets {old_enter}/{old_open} left orphaned (not deleted).")
        )
        inst.alias = label
        update_instance(inst)
        print(f"expertinstance {inst_id}: rulesets repointed, alias/description updated")

        expert = _expert_class(expert_name)(inst_id)
        expert_params = dict(entry["settings"]["settings"]["expert_params"])
        # THE UNIVERSE BLOCK, which this tool used to DROP -- the common root of review
        # findings V1 (the six screener:* genes) and V2 (the $100 underlying-price cap every
        # option grid screened on). The exporter has always built it; consuming only
        # ``settings.expert_params`` meant a genome selected on cheap names was deployed onto
        # whatever universe the live instance happened to have. The live settings exist under
        # the SAME names, so the whole repair is this mapping.
        universe_params = live_settings_from_universe(entry["settings"].get("universe"))
        if universe_params:
            # The universe is part of WHAT WAS SCORED, so it wins over the run's base settings
            # for the same reason the forced gates do.
            expert_params = {**expert_params, **universe_params}
            print(f"universe: {len(universe_params)} screener setting(s) carried into "
                  f"expert_params ({SCREENER_UNIVERSE_SETTING}="
                  f"{universe_params[SCREENER_UNIVERSE_SETTING]!r})")
        if created:
            # Defaults to False; without it the instance analyses and never trades. The export
            # now carries it explicitly (deploy_parity), so this is a floor for an OLD payload.
            expert_params.setdefault("allow_automated_trade_opening", True)
        expert.save_settings({k: (v, None) for k, v in expert_params.items()})
        print(f"expertsetting: saved {len(expert_params)} keys for instance {inst_id}"
              + ("  (incl. allow_automated_trade_opening)" if created else ""))
        # Behaviours the backtest derived that live has no analogue for. NOT applied -- a
        # deploy that differs from its backtest must SAY so (review V4/V5).
        for k, v in (entry["settings"].get("backtest_only") or {}).items():
            print(f"  BACKTEST-ONLY, not applied: {k}={v!r}")

    print("\n=== deploy complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
