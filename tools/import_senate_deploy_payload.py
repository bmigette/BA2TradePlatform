"""Step 2 of the S6 TOP2/TOP4 -> dev-instance deploy: read the payload dumped by
export_senate_deploy_payload.py and write it into the LIVE trade DB.

For each entry: converts entry/exit TradeRule lists to a live ruleset export via
``trade_rules_to_live_export``, imports it as NEW Ruleset+EventAction rows via
``RulesImporter.import_multiple_rulesets`` (never touches the existing rulesets -- old ones are
left orphaned, not deleted, so this is reversible), repoints the target ExpertInstance's
enter_market_ruleset_id/open_positions_ruleset_id at the new rulesets, and writes the expert_params
via the expert's own ``save_settings`` (so value typing follows get_settings_definitions exactly,
same as any other settings save through the app).

Usage: python tools/import_senate_deploy_payload.py <payload.json>
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

from ba2_common.core.db import get_instance, update_instance  # noqa: E402
from ba2_common.core.models import ExpertInstance  # noqa: E402
from ba2_common.core.rules_convert import trade_rules_to_live_export  # noqa: E402
from ba2_common.core.rules_export_import import RulesImporter  # noqa: E402
from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight  # noqa: E402


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

        inst = get_instance(ExpertInstance, inst_id)
        if inst is None:
            print(f"FATAL: ExpertInstance {inst_id} not found in {LIVE_DB}")
            return 1
        if inst.expert != "FMPSenateTraderWeight":
            print(f"FATAL: instance {inst_id} is expert={inst.expert!r}, expected FMPSenateTraderWeight")
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
            f"Deployed from {label} (backtest {bt_id}), sen5min3 grid, end2025 window. "
            f"Old rulesets {old_enter}/{old_open} left orphaned (not deleted)."
        )
        inst.alias = label
        update_instance(inst)
        print(f"expertinstance {inst_id}: rulesets repointed, alias/description updated")

        expert = FMPSenateTraderWeight(inst_id)
        expert_params = entry["settings"]["settings"]["expert_params"]
        expert.save_settings({k: (v, None) for k, v in expert_params.items()})
        print(f"expertsetting: saved {len(expert_params)} keys for instance {inst_id}")

    print("\n=== deploy complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
