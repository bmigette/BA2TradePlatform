#!/usr/bin/env python
"""MANUAL paper-trading validation for the Options Trading Phase 1 feature.

Requires a configured Alpaca PAPER account (Level 3) in the dev DB.
Submits LIVE (paper) option orders only when invoked with --confirm.

This is NOT a pytest test. It is a standalone operator script. It lives in
test_files/ (outside the pytest testpaths) and uses run_* function names so
pytest never collects it. DO NOT run it in CI or against a live/real-money
account.

Usage
-----
    # Dry run: print account info + the chosen contract, submit NOTHING.
    venv/bin/python test_files/validate_options_paper.py SPY

    # Full paper round-trip: open a 1-lot long call at the ASK, wait for the
    # position, then close it at the BID. Places LIVE paper orders.
    venv/bin/python test_files/validate_options_paper.py SPY --confirm

    # Optional explicit account id (otherwise the first Alpaca account is used):
    venv/bin/python test_files/validate_options_paper.py SPY --confirm --account-id 1

Pricing rule: buys are ALWAYS priced at the ASK, sells (closes) ALWAYS at the
BID. We never use the mid for live order submission.
"""
import argparse
import sys
import time
from datetime import date, timedelta

# Make the package importable when run as a script from the repo root.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.types import OptionRight, OrderDirection
from ba2_trade_platform.core.option_types import OptionLeg


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DTE_MIN = 30                 # earliest days-to-expiry to consider
DTE_MAX = 45                 # latest days-to-expiry to consider
MIN_OPEN_INTEREST = 100      # liquidity floor
MAX_SPREAD_PCT = 15.0        # reject contracts with a wider bid/ask spread (%)
POSITION_POLL_ATTEMPTS = 6   # times to poll for the opened position
POSITION_POLL_SLEEP = 5      # seconds between polls


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------
def run_resolve_account(account_id=None):
    """Return an instantiated, options-capable Alpaca account from the dev DB.

    If account_id is given, use it directly. Otherwise pick the first
    AccountDefinition with provider == "Alpaca".
    """
    from ba2_trade_platform.core.utils import get_account_instance_from_id
    from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    if account_id is None:
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import AccountDefinition
        from sqlmodel import select

        with get_db() as session:
            rows = session.exec(
                select(AccountDefinition).where(AccountDefinition.provider == "Alpaca")
            ).all()
        if not rows:
            raise RuntimeError(
                "No AccountDefinition with provider=='Alpaca' found in the dev DB."
            )
        print(f"Found {len(rows)} Alpaca account definition(s):")
        for r in rows:
            print(f"  id={r.id} name={getattr(r, 'name', '?')}")
        account_id = rows[0].id
        print(f"Using Alpaca account id={account_id}")

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Could not instantiate account id={account_id}")
    if not isinstance(account, OptionsAccountInterface):
        raise RuntimeError(
            f"Account id={account_id} is not options-capable "
            f"(does not implement OptionsAccountInterface)."
        )
    return account


def run_print_account_info(account):
    """Print account info + raw Alpaca options-trading level / buying power."""
    print("\n=== ACCOUNT INFO ===")
    info = account.get_account_info()
    print(info)

    # get_account_info() returns the raw Alpaca account object, but fetch the
    # raw client account too so the operator can confirm Level 3 explicitly.
    try:
        raw = account.client.get_account()
        level = getattr(raw, "options_trading_level", None)
        obp = getattr(raw, "options_buying_power", None)
        print(f"\noptions_trading_level : {level}  (expect 3 for spreads/long calls)")
        print(f"options_buying_power  : {obp}")
    except Exception as e:
        print(f"(Could not read raw Alpaca account options fields: {e})")


# ---------------------------------------------------------------------------
# Chain selection
# ---------------------------------------------------------------------------
def run_pick_contract(account, underlying):
    """Fetch a ~30-45 DTE call chain, filter to liquid, return nearest-ATM."""
    today = date.today()
    expiry_min = today + timedelta(days=DTE_MIN)
    expiry_max = today + timedelta(days=DTE_MAX)
    print(
        f"\n=== FETCHING CHAIN for {underlying} "
        f"({expiry_min} .. {expiry_max}, CALLs) ==="
    )

    chain = account.get_option_chain(
        underlying, expiry_min, expiry_max, OptionRight.CALL
    )
    if not chain:
        raise RuntimeError(f"Empty option chain returned for {underlying}.")
    print(f"Chain rows returned: {len(chain)}")

    # Reference spot for ATM selection: use the underlying's current price.
    spot = None
    try:
        spot = account.get_instrument_current_price(underlying)
    except Exception:
        spot = None
    if spot is None:
        # Fall back to the chain's strike distribution midpoint.
        strikes = sorted(c.strike for c in chain)
        spot = strikes[len(strikes) // 2]
        print(f"(No live spot; using median strike {spot} as ATM reference)")
    else:
        print(f"Underlying spot: {spot}")

    def is_liquid(c):
        if c.bid is None or c.ask is None or c.ask <= 0:
            return False
        if (c.open_interest or 0) < MIN_OPEN_INTEREST:
            return False
        sp = c.spread_pct
        if sp is None or sp > MAX_SPREAD_PCT:
            return False
        return True

    liquid = [c for c in chain if is_liquid(c)]
    print(
        f"Liquid contracts (OI>={MIN_OPEN_INTEREST}, "
        f"spread<={MAX_SPREAD_PCT}%): {len(liquid)}"
    )
    if not liquid:
        raise RuntimeError(
            "No liquid contracts matched the filters; widen the thresholds."
        )

    chosen = min(liquid, key=lambda c: abs(c.strike - spot))
    return chosen


def run_print_contract(contract):
    print("\n=== CHOSEN CONTRACT ===")
    print(f"  symbol   : {contract.symbol}")
    print(f"  strike   : {contract.strike}")
    print(f"  expiry   : {contract.expiry}")
    print(f"  bid/ask  : {contract.bid} / {contract.ask}  (mid={contract.mid})")
    print(f"  spread%  : {contract.spread_pct}")
    print(f"  delta    : {contract.delta}")
    print(f"  iv       : {contract.implied_volatility}")
    print(f"  OI/vol   : {contract.open_interest} / {contract.volume}")


# ---------------------------------------------------------------------------
# Round trip (only with --confirm)
# ---------------------------------------------------------------------------
def run_open_long_call(account, contract):
    """Submit a 1-lot long call (buy_to_open) priced at the ASK."""
    if contract.ask is None or contract.ask <= 0:
        raise RuntimeError("Chosen contract has no usable ask; cannot price the buy.")
    ask = contract.ask
    leg = OptionLeg(
        contract_symbol=contract.symbol,
        side=OrderDirection.BUY,
        ratio_qty=1,
        position_intent="buy_to_open",
        option_type=contract.option_type,
        strike=contract.strike,
        expiry=contract.expiry,
        underlying=contract.underlying,
    )
    print(f"\n=== SUBMITTING BUY (long_call) @ ASK={ask} ===")
    order = account.submit_option_order(
        [leg],
        quantity=1,
        order_type="limit",
        limit_price=ask,            # buys are ALWAYS at the ask
        option_strategy="long_call",
    )
    print("Submitted order:")
    print(order)
    return order


def run_wait_for_position(account, contract):
    """Poll get_option_positions() until the contract appears, or time out."""
    print("\n=== WAITING FOR OPEN POSITION ===")
    for attempt in range(1, POSITION_POLL_ATTEMPTS + 1):
        positions = account.get_option_positions()
        match = next(
            (p for p in positions if p.contract_symbol == contract.symbol), None
        )
        if match is not None:
            print(f"Position found on attempt {attempt}:")
            print(match)
            return match
        print(
            f"  attempt {attempt}/{POSITION_POLL_ATTEMPTS}: not filled yet "
            f"(held option positions: {len(positions)})"
        )
        time.sleep(POSITION_POLL_SLEEP)
    print("Timed out waiting for the position to appear (it may still fill).")
    return None


def run_close_position(account, position, contract):
    """Close the held option position priced at the BID."""
    if contract.bid is None or contract.bid <= 0:
        raise RuntimeError("Chosen contract has no usable bid; cannot price the close.")
    bid = contract.bid
    print(f"\n=== CLOSING POSITION @ BID={bid} ===")
    close_order = account.close_option_position(
        position,
        order_type="limit",
        limit_price=bid,            # sells/closes are ALWAYS at the bid
    )
    print("Close order:")
    print(close_order)
    return close_order


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Manual Alpaca PAPER options validation (Phase 1)."
    )
    parser.add_argument(
        "underlying", nargs="?", default="SPY",
        help="Underlying symbol (default: SPY)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually SUBMIT live paper orders (open + close). "
             "Without this flag the script only fetches and prints.",
    )
    parser.add_argument(
        "--account-id", type=int, default=None,
        help="Explicit AccountDefinition id (default: first Alpaca account).",
    )
    args = parser.parse_args(argv)

    print("=" * 70)
    print("MANUAL paper-trading validation for Options Trading Phase 1.")
    print("Requires a configured Alpaca PAPER account in the dev DB.")
    print(f"Mode: {'LIVE PAPER ORDERS (--confirm)' if args.confirm else 'DRY RUN (no orders)'}")
    print("=" * 70)

    try:
        account = run_resolve_account(args.account_id)
        run_print_account_info(account)

        contract = run_pick_contract(account, args.underlying.upper())
        run_print_contract(contract)

        if not args.confirm:
            print(
                "\nDRY RUN complete. No orders were submitted. "
                "Re-run with --confirm to place live paper orders."
            )
            return 0

        # --- Live paper round trip --------------------------------------
        order = run_open_long_call(account, contract)
        if order is None:
            print("Open order returned None (submission failed). Aborting close.")
            return 1

        position = run_wait_for_position(account, contract)
        if position is None:
            print(
                "No filled position to close. Inspect the open order in Alpaca "
                "and close manually if needed."
            )
            return 1

        run_close_position(account, position, contract)
        print("\n=== ROUND TRIP COMPLETE ===")
        return 0

    except Exception as e:
        print(f"\n!!! VALIDATION FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
