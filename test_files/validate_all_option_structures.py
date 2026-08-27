#!/usr/bin/env python
"""MANUAL paper-trading validation: open ALL 16 supported option structures on a
real Alpaca PAPER account, one at a time, then cancel every order.

Ad-hoc operator script (NOT pytest-collected; lives in test_files/, uses run_*
function names). Exercises the broker-facing surface directly:
    get_option_chain / get_option_quote / get_atm_implied_volatility /
    get_option_positions / submit_option_order / cancel_order

For each structure: build real legs off the live chain, submit at real
bid/ask, wait briefly, then cancel. Meant to be run while the market is
CLOSED so nothing fills — orders sit NEW/PENDING_NEW and cancel cleanly.
covered_call is expected to be REFUSED (no 100-share lots held on this
account) -- that refusal IS the thing being validated, not a bug.

Usage
-----
    .venv/Scripts/python.exe test_files/validate_all_option_structures.py [SYMBOL] [--account-id N]

Requires DB_FILE / CACHE_FOLDER env vars pointed at the target environment's
state folder if not running via the console entrypoint (see CLAUDE.md dev/prod
docs) -- this script does NOT set them itself.
"""
import argparse
import sys
import time
from datetime import date, timedelta

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba2_trade_platform.core.types import OptionRight, OrderDirection
from ba2_trade_platform.core.option_types import OptionLeg


DTE_MIN = 30
DTE_MAX = 60
CANCEL_WAIT_S = 2


def run_resolve_account(account_id):
    from ba2_trade_platform.core.utils import get_account_instance_from_id
    from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Could not instantiate account id={account_id}")
    if not isinstance(account, OptionsAccountInterface):
        raise RuntimeError(f"Account id={account_id} is not options-capable")
    return account


def run_print_account_info(account):
    print("\n=== ACCOUNT INFO ===")
    raw = account.client.get_account()
    print(f"account_number        : {raw.account_number}")
    print(f"options_trading_level  : {raw.options_trading_level} (need 3 for spreads)")
    print(f"options_buying_power   : {raw.options_buying_power}")
    print(f"buying_power           : {raw.buying_power}")
    print(f"cash                   : {raw.cash}")
    print(f"multiplier (margin)    : {raw.multiplier}")
    print(f"shorting_enabled       : {raw.shorting_enabled}")

    print("\n=== BROKER FUNCTION: get_option_positions() ===")
    positions = account.get_option_positions()
    print(f"open option positions  : {len(positions) if positions is not None else 'UNKNOWN (None)'}")
    for p in positions or []:
        print(f"  {p}")


def run_fetch_chains(account, underlying):
    """Pull calls + puts, pick the expiry with the most strikes on BOTH sides."""
    today = date.today()
    expiry_min = today + timedelta(days=DTE_MIN)
    expiry_max = today + timedelta(days=DTE_MAX)
    print(f"\n=== BROKER FUNCTION: get_option_chain({underlying}, {expiry_min}..{expiry_max}) ===")
    calls = account.get_option_chain(underlying, expiry_min, expiry_max, OptionRight.CALL)
    puts = account.get_option_chain(underlying, expiry_min, expiry_max, OptionRight.PUT)
    print(f"call rows: {len(calls)}   put rows: {len(puts)}")
    if not calls or not puts:
        raise RuntimeError("Empty chain on one side; cannot proceed.")

    call_expiries = {}
    for c in calls:
        call_expiries.setdefault(c.expiry, []).append(c)
    put_expiries = {}
    for p in puts:
        put_expiries.setdefault(p.expiry, []).append(p)

    common = set(call_expiries) & set(put_expiries)
    if not common:
        raise RuntimeError("No expiry present on both call and put chains.")
    best_expiry = max(common, key=lambda e: min(len(call_expiries[e]), len(put_expiries[e])))
    call_rows = sorted(call_expiries[best_expiry], key=lambda c: c.strike)
    put_rows = sorted(put_expiries[best_expiry], key=lambda c: c.strike)
    print(f"Chosen expiry: {best_expiry}  ({len(call_rows)} calls / {len(put_rows)} puts)")
    return call_rows, put_rows, best_expiry


def nearest(rows, target_strike):
    return min(rows, key=lambda c: abs(c.strike - target_strike))


def run_pick_reference_contracts(account, underlying, calls, puts):
    spot = None
    try:
        spot = account.get_instrument_current_price(underlying)
    except Exception:
        spot = None
    if spot is None:
        spot = calls[len(calls) // 2].strike
        print(f"(No live spot; using median call strike {spot} as reference)")
    else:
        print(f"Underlying spot: {spot}")

    atm_call = nearest(calls, spot)
    put_at_call_strike = nearest(puts, atm_call.strike)
    atm_put = nearest(puts, spot)
    call_10 = nearest(calls, spot * 1.10)
    call_15 = nearest(calls, spot * 1.15)
    call_lower_10 = nearest(calls, spot * 0.90)
    put_10 = nearest(puts, spot * 0.90)
    put_15 = nearest(puts, spot * 0.85)
    put_near_5 = nearest(puts, spot * 0.95)

    refs = dict(
        atm_call=atm_call, put_at_call_strike=put_at_call_strike, atm_put=atm_put,
        call_10=call_10, call_15=call_15, call_lower_10=call_lower_10,
        put_10=put_10, put_15=put_15, put_near_5=put_near_5,
    )
    print("\n=== REFERENCE CONTRACTS ===")
    for name, c in refs.items():
        print(f"  {name:20s} {c.symbol:24s} strike={c.strike:<8g} bid={c.bid} ask={c.ask}")
    return refs, spot


def leg(c, side, ratio_qty=1):
    return OptionLeg(
        contract_symbol=c.symbol, side=side, ratio_qty=ratio_qty,
        position_intent=("buy_to_open" if side == OrderDirection.BUY else "sell_to_open"),
        option_type=c.option_type, strike=c.strike, expiry=c.expiry, underlying=c.underlying,
    )


def build_structures(refs):
    """name -> (legs, limit_price, requires) built from the reference contracts.

    Sign convention (matches OptionsAccountInterface.submit_option_order):
    limit_price >= 0 is a net DEBIT, negative is a net CREDIT.
    """
    r = refs
    structures = {}

    structures["long_call"] = ([leg(r["atm_call"], OrderDirection.BUY)],
                                r["atm_call"].ask)
    structures["long_put"] = ([leg(r["atm_put"], OrderDirection.BUY)],
                               r["atm_put"].ask)
    structures["covered_call"] = ([leg(r["call_10"], OrderDirection.SELL)],
                                   r["call_10"].bid)
    structures["cash_secured_put"] = ([leg(r["put_10"], OrderDirection.SELL)],
                                       r["put_10"].bid)

    structures["bull_call_spread"] = (
        [leg(r["atm_call"], OrderDirection.BUY), leg(r["call_10"], OrderDirection.SELL)],
        _sub(r["atm_call"].ask, r["call_10"].bid))
    structures["bear_call_spread"] = (
        [leg(r["call_10"], OrderDirection.SELL), leg(r["call_15"], OrderDirection.BUY)],
        _neg(_sub(r["call_10"].bid, r["call_15"].ask)))
    structures["bull_put_spread"] = (
        [leg(r["put_10"], OrderDirection.SELL), leg(r["put_15"], OrderDirection.BUY)],
        _neg(_sub(r["put_10"].bid, r["put_15"].ask)))
    structures["bear_put_spread"] = (
        [leg(r["atm_put"], OrderDirection.BUY), leg(r["put_10"], OrderDirection.SELL)],
        _sub(r["atm_put"].ask, r["put_10"].bid))

    structures["long_straddle"] = (
        [leg(r["atm_call"], OrderDirection.BUY), leg(r["put_at_call_strike"], OrderDirection.BUY)],
        _add(r["atm_call"].ask, r["put_at_call_strike"].ask))
    structures["short_straddle"] = (
        [leg(r["atm_call"], OrderDirection.SELL), leg(r["put_at_call_strike"], OrderDirection.SELL)],
        _neg(_add(r["atm_call"].bid, r["put_at_call_strike"].bid)))
    structures["long_strangle"] = (
        [leg(r["call_10"], OrderDirection.BUY), leg(r["put_10"], OrderDirection.BUY)],
        _add(r["call_10"].ask, r["put_10"].ask))
    structures["short_strangle"] = (
        [leg(r["call_10"], OrderDirection.SELL), leg(r["put_10"], OrderDirection.SELL)],
        _neg(_add(r["call_10"].bid, r["put_10"].bid)))

    structures["iron_condor"] = (
        [leg(r["put_10"], OrderDirection.SELL), leg(r["put_15"], OrderDirection.BUY),
         leg(r["call_10"], OrderDirection.SELL), leg(r["call_15"], OrderDirection.BUY)],
        _neg(_sub(_add(r["put_10"].bid, r["call_10"].bid), _add(r["put_15"].ask, r["call_15"].ask))))
    structures["jade_lizard"] = (
        [leg(r["put_10"], OrderDirection.SELL), leg(r["call_10"], OrderDirection.SELL),
         leg(r["call_15"], OrderDirection.BUY)],
        _neg(_sub(_add(r["put_10"].bid, r["call_10"].bid), r["call_15"].ask)))
    structures["call_butterfly"] = (
        [leg(r["call_lower_10"], OrderDirection.BUY, ratio_qty=1),
         leg(r["atm_call"], OrderDirection.SELL, ratio_qty=2),
         leg(r["call_15"], OrderDirection.BUY, ratio_qty=1)],
        _sub(_add(r["call_lower_10"].ask, r["call_15"].ask), _mul2(r["atm_call"].bid)))
    structures["put_ratio_spread"] = (
        [leg(r["put_near_5"], OrderDirection.BUY, ratio_qty=1),
         leg(r["put_10"], OrderDirection.SELL, ratio_qty=2)],
        _sub(r["put_near_5"].ask, _mul2(r["put_10"].bid)))

    return structures


def _sub(a, b):
    return None if a is None or b is None else round(a - b, 4)


def _add(a, b):
    return None if a is None or b is None else round(a + b, 4)


def _neg(a):
    return None if a is None else round(-a, 4)


def _mul2(a):
    return None if a is None else round(2 * a, 4)


def run_all_structures(account, structures):
    results = []
    for name, (legs, limit_price) in structures.items():
        print(f"\n{'=' * 70}\n=== {name} ===")
        if limit_price is None or any(
                (leg_.side == OrderDirection.BUY and _leg_ask_missing(legs)) for leg_ in legs):
            pass  # missing-quote check happens implicitly below
        if limit_price is None:
            print(f"SKIP: missing quote(s), cannot price {name}")
            results.append((name, "SKIP_NO_QUOTE", None, None))
            continue
        is_debit = "spread" in name and name in ("bull_call_spread", "bear_put_spread")
        print(f"legs: {[(l.contract_symbol, l.side.value, l.ratio_qty) for l in legs]}")
        print(f"limit_price: {limit_price}")
        try:
            order = account.submit_option_order(
                legs, quantity=1, order_type="limit", limit_price=limit_price,
                option_strategy=name,
            )
        except ValueError as e:
            print(f"REFUSED (expected for some structures): {e}")
            results.append((name, "REFUSED", None, str(e)))
            continue
        except Exception as e:
            print(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, "ERROR", None, str(e)))
            continue

        if order is None:
            print("submit_option_order returned None (submission failed; see logged error above)")
            results.append((name, "SUBMIT_FAILED", None, None))
            continue

        print(f"SUBMITTED: order.id={order.id} broker_order_id={order.broker_order_id} "
              f"status={order.status}")
        results.append((name, "SUBMITTED", order.id, order.broker_order_id))

        time.sleep(CANCEL_WAIT_S)
        try:
            ok = account.cancel_order(order.id)
            print(f"cancel_order({order.id}) -> {ok}")
        except Exception as e:
            print(f"CANCEL FAILED for {name} (order.id={order.id}): {e}")

    return results


def _leg_ask_missing(legs):
    return False  # placeholder kept for readability of the guard above


def run_print_summary(results):
    print(f"\n{'=' * 70}\n=== SUMMARY ===")
    for name, status, order_id, extra in results:
        print(f"  {name:20s} {status:16s} order_id={order_id} {extra or ''}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open + cancel all 16 option structures.")
    parser.add_argument("underlying", nargs="?", default="SPY")
    parser.add_argument("--account-id", type=int, default=3)
    args = parser.parse_args(argv)

    print("=" * 70)
    print("VALIDATE ALL OPTION STRUCTURES (paper account, submit + immediate cancel)")
    print(f"account_id={args.account_id}  underlying={args.underlying}")
    print("=" * 70)

    account = run_resolve_account(args.account_id)
    run_print_account_info(account)

    calls, puts, expiry = run_fetch_chains(account, args.underlying.upper())

    print("\n=== BROKER FUNCTION: get_option_quote() ===")
    sample = calls[len(calls) // 2]
    quote = account.get_option_quote(sample.symbol)
    print(f"get_option_quote({sample.symbol}) -> {quote}")

    print("\n=== BROKER FUNCTION: get_atm_implied_volatility() ===")
    iv = account.get_atm_implied_volatility(args.underlying.upper())
    print(f"get_atm_implied_volatility({args.underlying.upper()}) -> {iv}")

    refs, spot = run_pick_reference_contracts(account, args.underlying.upper(), calls, puts)
    structures = build_structures(refs)
    results = run_all_structures(account, structures)
    run_print_summary(results)

    print("\n=== BROKER FUNCTION: get_option_positions() (post-run, should still be empty) ===")
    print(account.get_option_positions())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
