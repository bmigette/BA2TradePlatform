"""Penny-contract floor in ``passes_liquidity`` (2026-07-25).

The options grid passes NEITHER ``min_open_interest`` NOR ``max_spread_pct``, so before this
the liquidity gate was a complete no-op for every GA trial and the search happily "optimised"
near-worthless contracts. Real evidence from the v8 grid: the OS3 (skewed-credit) winner's 12
trades had entry premiums of $0.01-$0.09 (median $0.04), each producing $2-$7 of P&L on a $20k
account. Those fills are unobtainable live — the bid/ask spread is 100%+ of the contract's
value — so the floor is unconditional rather than another opt-in knob.
"""
from datetime import date

from ba2_common.core.option_selector import passes_liquidity
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight


def _quote(bid, ask, last=None, oi=1000):
    return OptionContract(
        symbol="X260821C00100000", underlying="X", option_type=OptionRight.CALL,
        strike=100.0, expiry=date(2026, 8, 21), bid=bid, ask=ask, last=last,
        open_interest=oi)


def test_penny_contract_rejected_even_with_no_gates_configured():
    # The exact OS3 profile: cent-priced contract, no OI/spread gate configured.
    assert passes_liquidity(_quote(0.01, 0.05), None, None) is False
    assert passes_liquidity(_quote(0.03, 0.05), None, None) is False   # mid 0.04
    assert passes_liquidity(_quote(0.08, 0.10), None, None) is False   # mid 0.09


def test_tradeable_premium_still_passes():
    assert passes_liquidity(_quote(0.10, 0.12), None, None) is True    # mid 0.11
    assert passes_liquidity(_quote(1.50, 1.60), None, None) is True


def test_floor_falls_back_to_last_when_not_two_sided():
    assert passes_liquidity(_quote(None, None, last=0.02), None, None) is False
    assert passes_liquidity(_quote(None, None, last=2.00), None, None) is True


def test_completely_unpriced_contract_is_left_to_callers_quote_guards():
    """No bid/ask/last at all is a DATA gap, not a penny contract — the actions' own
    missing-quote checks decline those explicitly (and log), so the floor must not swallow
    them here and turn a loud data problem into a silent empty chain."""
    assert passes_liquidity(_quote(None, None, last=None), None, None) is True


def test_floor_composes_with_the_opt_in_gates():
    # Above the floor but failing OI -> still rejected by the existing gate.
    assert passes_liquidity(_quote(1.50, 1.60, oi=10), 500, None) is False
    # Below the floor but with great OI -> rejected by the floor.
    assert passes_liquidity(_quote(0.01, 0.02, oi=100000), 500, None) is False
