"""READ-ONLY dry-run probe: does TastyTrade ACCEPT a fractional order QUANTITY?

The open question: the account holds 18 positions at 5-decimal quantities, which proves
the broker STORES 5 dp. It does not prove the broker ACCEPTS a 5-dp quantity on an order
you submit -- those holdings could come from DRIP / dollar-based fills where TastyTrade
computes the share count itself. If `minimum_increment_precision = 0` really means
"submitted orders must be whole shares", then reading `value` was the wrong fix.

A dry run settles it: the broker validates the order and returns a buying-power effect
WITHOUT creating anything.

SAFETY
  * `place_order` is GUARDED, not blocked: it raises unless `dry_run is True`.
    A live submission is impossible by construction, including by a bug in this file.
  * `place_complex_order`, `delete_order`, `replace_order`, `edit_order` stay fully blocked.
  * BUY LIMIT far below the market, so even a hypothetical live order could not fill.
  * Credentials read from a backup with sqlite mode=ro. No secret printed.

Run:
  PYTHONPATH=packages/common:packages/providers:packages/experts \
  venv/bin/python test_files/probe_tastytrade_dryrun.py
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_files.probe_tastytrade_live import build_account, load_settings  # noqa: E402

SYMBOL = "SCHD"          # fractionable=True, already held at 0.05715
LIMIT_PRICE = Decimal(os.environ.get("TT_LIMIT", "34.00"))  # near market; a far-off
                                 # limit can itself be rejected, which would confound the test
_calls = []


def install_raw_error_dump():
    """validate_response swallows any error whose shape lacks code+message pairs,
    raising TastytradeError('') with no detail. Print the raw body first."""
    import tastytrade.utils as ttu
    import tastytrade.session as tts
    original = ttu.validate_response

    def loud(response):
        if response.status_code // 100 != 2:
            print(f"     HTTP {response.status_code}")
            print(f"     RAW  {response.text[:900]}")
        return original(response)

    ttu.validate_response = loud
    tts.validate_response = loud
    if hasattr(tts, "validate_and_parse"):
        orig_parse = tts.validate_and_parse

        def loud_parse(response):
            if response.status_code // 100 != 2:
                print(f"     HTTP {response.status_code}")
                print(f"     RAW  {response.text[:900]}")
            return orig_parse(response)
        tts.validate_and_parse = loud_parse
    print("raw-error dump installed")


def install_dry_run_guard():
    """place_order allowed ONLY with dry_run=True. Everything else blocked."""
    from tastytrade.account import Account

    original = Account.place_order

    async def guarded(self, session, order, dry_run=True, **kw):
        if dry_run is not True:
            raise RuntimeError(
                f"GUARD: place_order called with dry_run={dry_run!r} - refused. "
                "This probe may never submit a live order.")
        _calls.append(order)
        return await original(self, session, order, dry_run=True, **kw)

    Account.place_order = guarded

    def deny(name):
        async def _blocked(*a, **k):
            raise RuntimeError(f"TRIPWIRE: {name} blocked")
        return _blocked

    for name in ("place_complex_order", "delete_order", "replace_order", "edit_order"):
        if hasattr(Account, name):
            setattr(Account, name, deny(name))

    print("guard armed: place_order requires dry_run=True; all mutating calls blocked\n")


def build_market_order(qty):
    """A MARKET buy -- TastyTrade refuses a fractional LIMIT with
    `fractional_market_orders_only`, so market is the only way to test precision.
    Market orders carry NO price and NO price-effect."""
    from tastytrade.order import (Leg, NewOrder, OrderAction, OrderTimeInForce,
                                  OrderType, InstrumentType)
    leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=SYMBOL,
              action=OrderAction.BUY_TO_OPEN, quantity=qty)
    return NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.MARKET,
                    legs=[leg])


def build_leg_order(qty):
    """A BUY LIMIT for `qty` shares, priced far below the market.

    price is NEGATIVE because NewOrder.price_effect is a computed field derived from the
    SIGN of price -- a BUY is a debit. Never set price_effect by hand.
    """
    from tastytrade.order import (Leg, NewOrder, OrderAction, OrderTimeInForce,
                                  OrderType, InstrumentType)
    leg = Leg(instrument_type=InstrumentType.EQUITY, symbol=SYMBOL,
              action=OrderAction.BUY_TO_OPEN, quantity=qty)
    return NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.LIMIT,
                    legs=[leg], price=-LIMIT_PRICE)


def try_qty(acct, qty, label):
    print(f"\n{'-' * 68}\n{label}: quantity={qty}\n{'-' * 68}")
    order = (build_market_order(qty) if os.environ.get('TT_MARKET')
             else build_leg_order(qty))
    sent = order.model_dump_json(exclude_none=True, by_alias=True)
    print(f"  wire: {sent[:190]}")
    try:
        resp = acct._run_async(
            acct._account.place_order(acct._session, order, dry_run=True))
    except Exception as e:
        print(f"  !! {type(e).__name__}: {str(e)[:400]!r}")
        print(f"     args={getattr(e, 'args', None)!r}")
        for attr in ("response", "json", "body", "detail", "errors", "code", "message"):
            if hasattr(e, attr):
                print(f"     e.{attr} = {getattr(e, attr)!r}")
        resp = getattr(e, "response", None)
        if resp is not None:
            print(f"     status={getattr(resp, 'status_code', None)} "
                  f"text={str(getattr(resp, 'text', ''))[:400]}")
        return None
    errs = getattr(resp, "errors", None) or []
    warns = getattr(resp, "warnings", None) or []
    print(f"  ACCEPTED: errors={len(errs)} warnings={len(warns)}")
    for m in errs:
        print(f"     ERROR   {getattr(m, 'code', '?')}: {getattr(m, 'message', m)}")
    for m in warns:
        print(f"     WARNING {getattr(m, 'code', '?')}: {getattr(m, 'message', m)}")
    bpe = getattr(resp, "buying_power_effect", None)
    if bpe is not None:
        print(f"     change_in_buying_power        = "
              f"{getattr(bpe, 'change_in_buying_power', None)!r}")
        print(f"     isolated_order_margin_req     = "
              f"{getattr(bpe, 'isolated_order_margin_requirement', None)!r}")
    fee = getattr(resp, "fee_calculation", None)
    if fee is not None:
        print(f"     total_fees                    = "
              f"{getattr(fee, 'total_fees', None)!r}")
    po = getattr(resp, "order", None) or getattr(resp, "placed_order", None)
    if po is not None:
        for lg in (getattr(po, "legs", None) or []):
            print(f"     echoed leg quantity           = {getattr(lg, 'quantity', None)!r}")
    return resp


def main():
    install_raw_error_dump()
    install_dry_run_guard()
    acct = build_account(load_settings())
    print(f"connected: {acct._account.account_number}")

    from tastytrade.instruments import Equity
    eq = acct._run_async(Equity.get(acct._session, SYMBOL))
    print(f"{SYMBOL}: is_fractional_quantity_eligible="
          f"{eq.is_fractional_quantity_eligible!r}")

    cases = [(Decimal("1"), "CONTROL - whole share"),
             (Decimal("0.05"), "2 dp"),
             (Decimal("0.05715"), "5 dp (matches an existing holding)"),
             (Decimal("0.123456"), "6 dp"),
             (Decimal("0.1234567"), "7 dp")]
    results = [(lbl, q, try_qty(acct, q, lbl)) for q, lbl in cases]
    whole = results[0][2]
    frac2 = results[1][2]
    frac5 = results[2][2]
    print(f"\n{'=' * 68}\nPRECISION LADDER\n{'=' * 68}")
    for lbl, q, r in results:
        okr = r is not None and not (getattr(r, "errors", None) or [])
        print(f"  {str(q):<12} {lbl:<34} {'ACCEPTED' if okr else 'refused'}")

    def ok(r):
        return r is not None and not (getattr(r, "errors", None) or [])

    print(f"\n{'=' * 68}\nVERDICT\n{'=' * 68}")
    print(f"  whole share (1)      accepted: {ok(whole)}")
    print(f"  fractional (0.05)    accepted: {ok(frac2)}")
    print(f"  fractional (0.05715) accepted: {ok(frac5)}")
    if not ok(whole):
        print("\n  -> INCONCLUSIVE. The WHOLE-SHARE control was refused too, so nothing here"
              "\n     says anything about fractional quantities. Read the RAW body above and"
              "\n     fix the setup first. Causes seen in practice:"
              "\n       * a read-only OAuth token          -> HTTP 403 'insufficient scopes'"
              "\n       * TT_MARKET=1 outside RTH          -> tif_no_after_hours_opening_market_orders"
              "\n     ALWAYS check the control before believing any verdict below.")
    elif ok(frac5):
        print("\n  -> TastyTrade ACCEPTS a 5-dp submitted quantity, confirming that"
              "\n     QuantityDecimalPrecision.value (5) is the quantity precision and"
              "\n     minimum_increment_precision (0) is a different concept.")
    elif ok(frac2):
        print("\n  -> 2 dp accepted but 5 dp refused: the real step is coarser than `value`.")
    else:
        print("\n  -> Whole shares accepted, fractional refused. Read the RAW body: the"
              "\n     reason matters and is NOT necessarily about precision. Known codes:"
              "\n       fractional_market_orders_only  -> you sent a LIMIT; fractional is"
              "\n           market/notional-market only. Re-run with TT_MARKET=1 during RTH."
              "\n       below_notional_value_minimum   -> under the $5 fractional floor;"
              "\n           raise the quantity, this says nothing about precision."
              "\n       fractional_equity_invalid_fractional_precision -> genuinely too many"
              "\n           decimals; the message states the true maximum for that symbol.")

    print(f"\n  place_order calls made: {len(_calls)}, all dry_run=True. "
          f"Nothing was submitted.")


if __name__ == "__main__":
    main()
