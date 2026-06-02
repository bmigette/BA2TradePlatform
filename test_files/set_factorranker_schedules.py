"""One-shot script to set execution schedules on the 10 FactorRanker instances.

Assignment rationale:
- Momentum-only strategies: weekly Wednesday 10:00 (captures shorter-term momentum)
- All others (value / quality / blended / all-factor): monthly 1st Wednesday 10:00
  (lower turnover suits these slower factors)

Time 10:00 avoids the 09:30 cluster used by ba2New experts.

Only sets execution_schedule_enter_market — FactorRanker has schedules_open_positions=False
so there is no separate open-positions schedule.

Run from the project root:
  .venv/Scripts/python.exe test_files/set_factorranker_schedules.py
"""

import sys

from sqlmodel import select

from ba2_trade_platform.core.db import get_db, init_db
from ba2_trade_platform.core.models import AccountDefinition, ExpertInstance
from ba2_trade_platform.core.utils import get_expert_instance_from_id

ACCOUNT_NAME = "BA2NewStrat"
EXPERT_CLASS = "FactorRanker"

# Weekly Wednesday 10:00 — for momentum strategies
WEEKLY_WED = {
    "days": {
        "monday": False, "tuesday": False, "wednesday": True,
        "thursday": False, "friday": False, "saturday": False, "sunday": False,
    },
    "times": ["10:00"],
}

# Monthly 1st Wednesday 10:00 — for value/quality/blended strategies
MONTHLY_1ST_WED = {
    "frequency": "monthly",
    "ordinal": 1,
    "weekday": "wednesday",
    "times": ["10:00"],
}

# Alias -> schedule assignment
SCHEDULE_BY_ALIAS = {
    "FR-N50-Momentum":          WEEKLY_WED,      # momentum only → weekly
    "FR-N50-Value":             MONTHLY_1ST_WED, # value only → monthly
    "FR-N50-Quality":           MONTHLY_1ST_WED, # quality only → monthly
    "FR-N50-MultiFactor":       MONTHLY_1ST_WED, # blended → monthly
    "FR-N50-MultiScore":        MONTHLY_1ST_WED, # blended (score) → monthly
    "FR-Scr-LargeCap-Multi":    MONTHLY_1ST_WED, # large-cap blended → monthly
    "FR-Scr-MidCap-Value":      MONTHLY_1ST_WED, # value/quality → monthly
    "FR-Scr-HighLiq-Momentum":  WEEKLY_WED,      # momentum, high-liq → weekly
    "FR-Scr-Broad-AllFactor":   MONTHLY_1ST_WED, # all-factor incl PEAD → monthly
    "FR-Scr-Concentrated":      MONTHLY_1ST_WED, # mega-cap concentrated → monthly
}


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
        print(f"No FactorRanker instances found on account '{ACCOUNT_NAME}'. Run create_factorranker_instances.py first.")
        return 1

    print(f"Found {len(instances)} FactorRanker instances on '{ACCOUNT_NAME}':\n")
    for inst in instances:
        alias = inst.alias or f"id={inst.id}"
        schedule = SCHEDULE_BY_ALIAS.get(alias)
        if not schedule:
            print(f"  SKIP id={inst.id} alias={alias!r} — no schedule mapping defined")
            continue

        expert = get_expert_instance_from_id(inst.id, use_cache=False)
        expert.save_setting("execution_schedule_enter_market", schedule)

        freq_label = (
            "weekly Wed 10:00"
            if schedule.get("frequency") != "monthly"
            else "monthly 1st Wed 10:00"
        )
        print(f"  id={inst.id:>4}  {alias:<30}  -> {freq_label}")

    print()
    print("Done. Schedules take effect on the next JobManager restart.")
    print("NOTE: execution_schedule_open_positions intentionally NOT set —")
    print("      FactorRanker has schedules_open_positions=False.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
