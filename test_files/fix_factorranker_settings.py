"""One-shot: materialize the equity + FactorRanker settings that were left as
None (no DB row) on the 10 BA2NewStrat FactorRanker instances.

Root cause: the create script only persisted a handful of settings, so the rest
had no DB row. The UI's hardcoded equity fields read settings_source.get(key,
default), which returns None (key present, value None) instead of the default —
so "min balance" / "max equity per instrument" rendered blank. (The UI load was
also fixed to fall back to the interface default; this script makes the values
explicit on the instances.)

For each instance:
  - max_virtual_equity_per_instrument_percent  <- round(max_weight_per_name * 100)
    (keeps the concentrated config's 20% cap coherent; 10% for the rest)
  - min_available_balance_pct                  <- 10.0 (conservative; cosmetic for
    FactorRanker, which sizes from virtual balance * gross_exposure directly)
  - every other FactorRanker-specific setting still None -> its interface default,
    so the instance is fully explicit in the UI.

Idempotent: only fills settings that are currently None; never overwrites a value
already set (factor weights, top_n, weighting, screener filters, schedule, etc.).

Run:
  .venv/Scripts/python.exe -m test_files.fix_factorranker_settings
"""

import sys

from sqlmodel import select

from ba2_trade_platform.core.db import get_db, init_db
from ba2_trade_platform.core.models import AccountDefinition, ExpertInstance
from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.modules.experts.FactorRanker import FactorRanker

ACCOUNT_NAME = "BA2NewStrat"
EXPERT_CLASS = "FactorRanker"


def main() -> int:
    init_db()

    with get_db() as s:
        account = s.exec(
            select(AccountDefinition).where(AccountDefinition.name == ACCOUNT_NAME)
        ).first()
        if not account:
            print(f"ERROR: account '{ACCOUNT_NAME}' not found.")
            return 1
        instances = s.exec(
            select(ExpertInstance)
            .where(ExpertInstance.account_id == account.id)
            .where(ExpertInstance.expert == EXPERT_CLASS)
        ).all()

    if not instances:
        print("No FactorRanker instances found.")
        return 1

    fr_defaults = {k: meta.get("default") for k, meta in FactorRanker.get_settings_definitions().items()}

    print(f"Materializing settings on {len(instances)} FactorRanker instances:\n")
    for inst in instances:
        expert = get_expert_instance_from_id(inst.id, use_cache=False)
        settings = expert.settings  # every definition key, None where no DB row

        filled = []

        # FactorRanker-specific settings: fill any None with its interface default.
        for key, default in fr_defaults.items():
            if settings.get(key) is None and default is not None:
                expert.save_setting(key, default)
                filled.append(key)

        # Effective per-name cap (now persisted or already set) drives the
        # base "max equity per instrument" so the two stay coherent.
        eff_max_weight = expert.get_setting_with_interface_default("max_weight_per_name")
        max_equity_pct = round(float(eff_max_weight) * 100.0, 1)

        if settings.get("max_virtual_equity_per_instrument_percent") is None:
            expert.save_setting("max_virtual_equity_per_instrument_percent", max_equity_pct)
            filled.append(f"max_virtual_equity_per_instrument_percent={max_equity_pct}")

        if settings.get("min_available_balance_pct") is None:
            expert.save_setting("min_available_balance_pct", 10.0)
            filled.append("min_available_balance_pct=10.0")

        alias = inst.alias or f"id={inst.id}"
        if filled:
            print(f"  id={inst.id:>4}  {alias:<28}  filled {len(filled)}: "
                  f"{', '.join(filled)}")
        else:
            print(f"  id={inst.id:>4}  {alias:<28}  already complete")

    print("\nDone. Re-open an instance in the UI — equity fields now show values.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
