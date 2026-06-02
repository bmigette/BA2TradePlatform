"""One-shot creation script for 10 FactorRanker instances on BA2NewStrat.

RUN ONCE. This writes to the PRODUCTION database
(``~/Documents/ba2_trade_platform/db.sqlite``): it inserts ExpertInstance rows
and their settings via the ORM / settings API. It is idempotent — if FactorRanker
instances already exist on the BA2NewStrat account it prints their ids and exits
WITHOUT creating duplicates.

Per Parts B/C/D of docs/plans/2026-06-02-factorranker-screener-and-instances.md:
  * 5 static Nasdaq-50 instances (different rank algos)
  * 5 screener-based instances (penny filters disabled — wrong for factor models)
  * Each gets virtual_equity_pct=10.0 and instrument_selection_method="expert".

SAFETY: instances are created with NO execution schedule, so JobManager never
auto-trades them. A schedule must be added in the UI before they can trade.

Run from the project root:
  .venv/Scripts/python.exe test_files/create_factorranker_instances.py        # Windows
  .venv/bin/python test_files/create_factorranker_instances.py                # Linux/macOS
"""

import sys

from sqlmodel import select

from ba2_trade_platform.core.db import add_instance, get_db, init_db
from ba2_trade_platform.core.models import AccountDefinition, ExpertInstance
from ba2_trade_platform.core.utils import get_expert_instance_from_id


# Account to create the instances on (resolved by name to be safe).
ACCOUNT_NAME = "BA2NewStrat"
EXPERT_CLASS = "FactorRanker"
VIRTUAL_EQUITY_PCT = 10.0


# -------- Part B: Nasdaq-50 static list ------------------------------------

NASDAQ_50 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "AVGO", "META", "GOOGL", "GOOG", "TSLA", "COST",
    "NFLX", "PLTR", "CSCO", "TMUS", "AMD", "PEP", "LIN", "INTU", "TXN", "ADBE",
    "QCOM", "BKNG", "AMGN", "ISRG", "HON", "AMAT", "GILD", "CMCSA", "ADP", "VRTX",
    "PANW", "ADI", "MU", "LRCX", "REGN", "MELI", "KLAC", "SBUX", "CDNS", "SNPS",
    "MAR", "ORLY", "CSX", "ABNB", "FTNT", "ADSK", "WDAY", "NXPI", "ROP", "PCAR",
]


# -------- Part C: the 10 configs -------------------------------------------
#
# Common to all: instrument_selection_method="expert", virtual_equity_pct=10.0,
# NO schedule. Screener configs MUST disable the penny filters
# (screener_relative_volume_min=0, screener_price_drop_pct=0).
#
# Each config is a dict:
#   alias            : display name for the instance
#   universe_source  : "static" | "screener"
#   factors          : factor_weights dict
#   overrides        : factor/rank overrides (top_n, weighting, and optionally
#                      max_weight_per_name / pead_drift_window_days)
#   screener         : screener_* settings (screener configs only)

CONFIGS = [
    # 1
    {
        "alias": "FR-N50-Momentum",
        "universe_source": "static",
        "factors": {"momentum": 1.0},
        "overrides": {"top_n": 15, "weighting": "equal"},
    },
    # 2
    {
        "alias": "FR-N50-Value",
        "universe_source": "static",
        "factors": {"value": 1.0},
        "overrides": {"top_n": 15, "weighting": "equal"},
    },
    # 3
    {
        "alias": "FR-N50-Quality",
        "universe_source": "static",
        "factors": {"quality": 1.0},
        "overrides": {"top_n": 15, "weighting": "equal"},
    },
    # 4
    {
        "alias": "FR-N50-MultiFactor",
        "universe_source": "static",
        "factors": {"momentum": 1.0, "value": 1.0, "quality": 1.0},
        "overrides": {"top_n": 20, "weighting": "equal"},
    },
    # 5
    {
        "alias": "FR-N50-MultiScore",
        "universe_source": "static",
        "factors": {"momentum": 1.0, "value": 1.0, "quality": 1.0},
        "overrides": {"top_n": 20, "weighting": "score"},
    },
    # 6
    {
        "alias": "FR-Scr-LargeCap-Multi",
        "universe_source": "screener",
        "factors": {"momentum": 1.0, "value": 1.0, "quality": 1.0},
        "overrides": {"top_n": 20, "weighting": "equal"},
        "screener": {
            "screener_market_cap_min": 10_000_000_000,
            "screener_market_cap_max": 0,
            "screener_price_min": 10.0,
            "screener_volume_min": 1_000_000,
            "screener_sort_metric": "market_cap",
            "screener_max_stocks": 50,
        },
    },
    # 7
    {
        "alias": "FR-Scr-MidCap-Value",
        "universe_source": "screener",
        "factors": {"value": 1.0, "quality": 1.0},
        "overrides": {"top_n": 20, "weighting": "equal"},
        "screener": {
            "screener_market_cap_min": 2_000_000_000,
            "screener_market_cap_max": 20_000_000_000,
            "screener_price_min": 0.0,
            "screener_volume_min": 500_000,
            "screener_sort_metric": "composite",
            "screener_max_stocks": 60,
        },
    },
    # 8
    {
        "alias": "FR-Scr-HighLiq-Momentum",
        "universe_source": "screener",
        "factors": {"momentum": 1.0},
        "overrides": {"top_n": 15, "weighting": "equal"},
        "screener": {
            "screener_market_cap_min": 5_000_000_000,
            "screener_market_cap_max": 0,
            "screener_price_min": 0.0,
            "screener_volume_min": 5_000_000,
            "screener_sort_metric": "volume",
            "screener_max_stocks": 50,
        },
    },
    # 9
    {
        "alias": "FR-Scr-Broad-AllFactor",
        "universe_source": "screener",
        "factors": {"momentum": 1.0, "value": 1.0, "quality": 1.0, "pead": 0.5},
        "overrides": {"top_n": 25, "weighting": "equal", "pead_drift_window_days": 60},
        "screener": {
            "screener_market_cap_min": 1_000_000_000,
            "screener_market_cap_max": 0,
            "screener_price_min": 5.0,
            "screener_volume_min": 1_000_000,
            "screener_sort_metric": "market_cap",
            "screener_max_stocks": 80,
        },
    },
    # 10
    {
        "alias": "FR-Scr-Concentrated",
        "universe_source": "screener",
        "factors": {"momentum": 1.0, "value": 1.0, "quality": 1.0},
        "overrides": {"top_n": 8, "weighting": "equal", "max_weight_per_name": 0.20},
        "screener": {
            "screener_market_cap_min": 20_000_000_000,
            "screener_market_cap_max": 0,
            "screener_price_min": 0.0,
            "screener_volume_min": 0,
            "screener_sort_metric": "market_cap",
            "screener_max_stocks": 40,
        },
    },
]


# -------- helpers ----------------------------------------------------------

def resolve_account_id(name: str) -> int | None:
    """Return the AccountDefinition id for the given account name, or None."""
    with get_db() as session:
        account = session.exec(
            select(AccountDefinition).where(AccountDefinition.name == name)
        ).first()
        return account.id if account else None


def existing_factorranker_instance_ids(account_id: int) -> list[int]:
    """Return ids of any FactorRanker ExpertInstance already on the account."""
    with get_db() as session:
        instances = session.exec(
            select(ExpertInstance)
            .where(ExpertInstance.account_id == account_id)
            .where(ExpertInstance.expert == EXPERT_CLASS)
        ).all()
        return [inst.id for inst in instances]


def configure_instance(expert, config: dict) -> None:
    """Apply all settings for one config to a freshly created expert instance."""
    source = config["universe_source"]

    # Universe selection mode.
    expert.save_setting("instrument_selection_method", "expert")
    expert.save_setting("universe_source", source)

    if source == "static":
        expert.set_enabled_instruments(
            {s: {"enabled": True, "weight": 1.0} for s in NASDAQ_50}
        )
    else:
        # Screener-based universe. Always disable the penny-momentum filters
        # (rvol, price_drop) — they are wrong for factor strategies.
        expert.save_setting("screener_relative_volume_min", 0)
        expert.save_setting("screener_price_drop_pct", 0)
        for key, value in config["screener"].items():
            expert.save_setting(key, value)

    # Per-factor weights (one float each; 0 disables a factor).
    for fname in ("momentum", "value", "quality", "pead"):
        expert.save_setting(f"factor_weight_{fname}", float(config["factors"].get(fname, 0.0)))

    # Factor / rank overrides (top_n, weighting, and where listed
    # max_weight_per_name / pead_drift_window_days).
    for key, value in config["overrides"].items():
        expert.save_setting(key, value)


# -------- main -------------------------------------------------------------

def main() -> int:
    init_db()

    # 1. Resolve the account by name.
    account_id = resolve_account_id(ACCOUNT_NAME)
    if account_id is None:
        print(f"ERROR: account '{ACCOUNT_NAME}' not found. Aborting.")
        return 1
    print(f"Resolved account '{ACCOUNT_NAME}' -> id {account_id}")

    # 2. Dedupe guard.
    existing = existing_factorranker_instance_ids(account_id)
    if existing:
        print(
            f"FactorRanker instances already exist on account {account_id}: {existing}"
        )
        print("Nothing to do — not creating duplicates.")
        return 0

    # 3. Create + configure each instance.
    created_ids: list[int] = []
    for config in CONFIGS:
        instance = ExpertInstance(
            account_id=account_id,
            expert=EXPERT_CLASS,
            enabled=True,
            alias=config["alias"],
            virtual_equity_pct=VIRTUAL_EQUITY_PCT,
        )
        iid = add_instance(instance)

        expert = get_expert_instance_from_id(iid, use_cache=False)
        configure_instance(expert, config)

        created_ids.append(iid)
        print(f"  created id={iid:>4}  {config['alias']}  ({config['universe_source']})")

    print()
    print(f"Created {len(created_ids)} FactorRanker instances on account "
          f"{account_id}: {created_ids}")

    # 4. One-instance round-trip verification.
    verify_id = created_ids[0]
    verify = get_expert_instance_from_id(verify_id, use_cache=False)
    settings = verify.settings
    print()
    print(f"Verification round-trip (instance id={verify_id}, "
          f"alias={CONFIGS[0]['alias']}):")
    print(f"  factor weights          = " + ", ".join(
        f"{f}={settings.get('factor_weight_' + f)}" for f in ("momentum", "value", "quality", "pead")))
    print(f"  universe_source         = {settings.get('universe_source')}")
    print(f"  instrument_selection    = {settings.get('instrument_selection_method')}")
    print(f"  top_n                   = {settings.get('top_n')}")
    print(f"  weighting               = {settings.get('weighting')}")
    enabled = verify._get_enabled_instruments_config()
    print(f"  enabled-instruments cnt = {len(enabled)} (expected {len(NASDAQ_50)})")

    # 5. Safety note.
    print()
    print("=" * 70)
    print("SAFETY: no execution schedule was set on any instance.")
    print("They CANNOT auto-trade until a schedule is added in the UI.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
