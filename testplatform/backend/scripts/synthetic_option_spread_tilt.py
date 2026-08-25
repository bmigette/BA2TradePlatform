"""SYNTHETIC measurement: what the single-leg limit-spread fix costs each option shape.

*** THE BOOK BELOW IS SYNTHETIC. It is a hand-built, deliberately symmetric illustration,
*** NOT the output of any grid/backtest run and NOT a claim about any real strategy's edge.
*** Its only purpose is to size the FRICTION ASYMMETRY the fix removes, in a book where
*** both strategies are constructed to earn exactly the same gross P&L so the difference
*** between them is purely execution cost.

WHY IT EXISTS
-------------
``_option_fill_price`` used to charge the modeled option bid-ask spread only on its
market-style branch. Multi-leg combo CHILDREN take that branch (they carry no limit_price
— the parent holds the net limit), but every SINGLE-LEG option order the platform submits
carries a limit price, so the whole wheel / 0DTE / long-option branch crossed no spread on
either end while credit structures paid ~5% of premium per leg per side. This script prices
the same two books under the code BEFORE and AFTER the fix and prints the difference.

Both fill prices come from the real engine:
  * AFTER  = ``BacktestAccount._option_fill_price`` as it stands now.
  * BEFORE = the same call for legs with NO limit_price (that branch is untouched), and the
    raw bar premium for legs WITH one — which is literally what the old code returned
    (``if px > limit: return None; fill_px = px``).
Cross-check: reverting the fix and re-running makes the engine reproduce the BEFORE column
exactly (this script asserts it when run with --engine-before).

Run (from the repo root, with the packages on PYTHONPATH):
    venv/bin/python testplatform/backend/scripts/synthetic_option_spread_tilt.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backtest.backtest_account import BacktestAccount   # noqa: E402
from ba2_common.core.types import OrderDirection, OrderType          # noqa: E402

# --- grid defaults, verbatim (ba2test_launcher.py --option-spread-pct/--min-tick) --------
CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,     # commissions are identical per leg either way; excluded
    "slippage_bps": 0.0,             # isolate the SPREAD
    "fill_model": "next_bar_open",
    "option_spread_pct": 5.0,
    "option_spread_min_tick": 0.02,
}
CYCLES = 24            # fortnightly for a year
CONTRACTS = 1          # per cycle, per strategy
MULT = 100

# --- the synthetic book ------------------------------------------------------------------
# XYZ at $100. Both strategies open a $1.50 credit and close it at 50% of max profit, so
# each earns exactly $75 per cycle GROSS. Every contract is liquid (volume >= 100) so the
# thin-contract widening is not what drives the gap; thin wings would only make the
# multi-leg side look worse still.
VOL = 250.0

# WHEEL — single-leg premium seller. One order per side, each carrying a limit price,
# exactly as PremiumSeller / TradeActions submit them.
WHEEL = [
    # (premium, side, has_limit)
    (1.50, OrderDirection.SELL, True),    # sell to open the 30-delta put
    (0.75, OrderDirection.BUY, True),     # buy to close at 50% of max profit
]

# IRON CONDOR — 4 legs in, 4 legs out; children carry NO limit price (the parent holds the
# net), so they always paid the spread. Net credit 1.50 in, net debit 0.75 out.
CONDOR = [
    (1.20, OrderDirection.SELL, False), (0.45, OrderDirection.BUY, False),
    (1.15, OrderDirection.SELL, False), (0.40, OrderDirection.BUY, False),
    (0.60, OrderDirection.BUY, False), (0.225, OrderDirection.SELL, False),
    (0.575, OrderDirection.BUY, False), (0.20, OrderDirection.SELL, False),
]


class _StubOptions:
    def __init__(self):
        self.px = 1.0

    def get_bar(self, occ, day):
        return {"open": self.px, "close": self.px, "volume": VOL,
                "strike": 95.0, "option_type": "put"}


class _StubPrice:
    """Spot 100 vs a 95 put: intrinsic 0, so the no-arbitrage guard never binds here."""

    def next_bar_date(self, symbol, as_of):
        import datetime as _dt
        return _dt.date(2024, 3, 6)

    def bar_at(self, symbol, day):
        return {"open": 100.0, "close": 100.0, "high": 100.0, "low": 100.0}


def _account():
    a = BacktestAccount(id=1, price_source=_StubPrice(), settings=dict(CFG))
    a._options = _StubOptions()
    return a


def _price_leg(acct, premium, side, has_limit, engine_before=False):
    """(after, before) signed cash per share for one leg: + = credit, - = debit."""
    acct._options.px = premium
    is_buy = side == OrderDirection.BUY
    # A marketable limit: 10% of the premium of room, so the order still clears once the
    # spread is crossed and the measurement is pure PRICE impact, not a lost fill.
    limit = premium * 1.10 if is_buy else premium * 0.90
    order = SimpleNamespace(
        id=1, symbol="XYZ", underlying_symbol="XYZ", contract_symbol="XYZ240315P00095000",
        order_type=(OrderType.BUY_LIMIT if is_buy else OrderType.SELL_LIMIT),
        limit_price=(limit if has_limit else None), side=side, quantity=CONTRACTS,
        multiplier=MULT, strike=95.0, option_type=None, position_intent=None,
        parent_order_id=(None if has_limit else 99),
    )
    after = acct._option_fill_price(order, None)
    if after is None:
        raise SystemExit(f"leg did not fill (premium {premium}, buy={is_buy}) — the "
                         f"synthetic limits must stay marketable for this measurement")
    if engine_before:
        before = after
    else:
        # The code as it stood before the fix: limit orders filled at the raw bar premium;
        # the no-limit (multi-leg child) branch is untouched, so it is the same call.
        bar = acct._options.get_bar(None, None)
        before = premium if has_limit else acct._option_slip(premium, is_buy, bar)
    sign = -1.0 if is_buy else 1.0
    return sign * after, sign * before


def _book(acct, legs, engine_before=False):
    after = before = gross = 0.0
    for premium, side, has_limit in legs:
        a, b = _price_leg(acct, premium, side, has_limit, engine_before)
        after += a
        before += b
        gross += (-premium if side == OrderDirection.BUY else premium)
    return after * MULT, before * MULT, gross * MULT


def main():
    engine_before = "--engine-before" in sys.argv
    acct = _account()
    rows = []
    for name, legs, crossings in (("WHEEL  (single-leg, 1 contract)", WHEEL, 2),
                                  ("CONDOR (4-leg credit, 1 structure)", CONDOR, 8)):
        after, before, gross = _book(acct, legs, engine_before)
        rows.append((name, crossings, gross, before, after))

    print("*** SYNTHETIC BOOK — illustrative, not a backtest result ***")
    print(f"    option_spread_pct={CFG['option_spread_pct']} "
          f"option_spread_min_tick={CFG['option_spread_min_tick']} "
          f"slippage_bps={CFG['slippage_bps']} commissions excluded")
    print(f"    {CYCLES} cycles/year x {CONTRACTS} contract, both shapes constructed to earn "
          f"the SAME gross\n")
    hdr = f"{'':36} {'cross':>5} {'gross/cyc':>10} {'net BEFORE':>11} {'net AFTER':>10} {'cost of fix':>12}"
    print(hdr)
    print("-" * len(hdr))
    per_year = {}
    for name, crossings, gross, before, after in rows:
        print(f"{name:36} {crossings:>5} {gross:>10.2f} {before:>11.2f} {after:>10.2f} "
              f"{after - before:>12.2f}")
        per_year[name] = (gross * CYCLES, before * CYCLES, after * CYCLES)

    print(f"\nPer YEAR ({CYCLES} cycles):")
    for name, (gross, before, after) in per_year.items():
        print(f"  {name:36} gross {gross:>9.2f}   net before {before:>9.2f}   "
              f"net after {after:>9.2f}   friction {gross - after:>8.2f} "
              f"({(gross - after) / gross * 100:.1f}% of gross)")

    (wg, wb, wa) = per_year["WHEEL  (single-leg, 1 contract)"]
    (cg, cb, ca) = per_year["CONDOR (4-leg credit, 1 structure)"]
    print(f"\nTHE TILT (same gross by construction, so any gap is pure execution cost):")
    print(f"  wheel minus condor, BEFORE the fix: {wb - cb:>8.2f}")
    print(f"  wheel minus condor, AFTER  the fix: {wa - ca:>8.2f}")
    print(f"  fabricated advantage removed:       {(wb - cb) - (wa - ca):>8.2f} "
          f"({((wb - cb) - (wa - ca)) / wg * 100:.1f}% of the wheel's gross)")
    print("\nNOT MEASURED HERE: post-fix, a marginal single-leg limit that only just cleared "
          "the raw print no longer fills at all (it retries or is missed). This book keeps "
          "every limit marketable so the number above is price impact only — the real effect "
          "is at least this large.")


if __name__ == "__main__":
    main()
