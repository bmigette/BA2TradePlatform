# packages/common/tests/test_option_capacity_downsize.py
"""Review 2026-08-30 F6 — the assignment-capacity gate DOWNSIZES instead of refusing.

The gate (short-put strike notional vs CASH, account-wide, cumulative) used to refuse
the ENTIRE entry when the book-wide delivery bill would exceed cash. At $20k that made
most of the 5-30% sizing gene band a zero-trade region: an IC on a $100 name sized to 6
units was refused outright even though 2 units fit. Stage-1 verdicts for the whole
put-side credit family measured this gate, not the market.

Operator decision (2026-08-30): SHARED code changes from refuse-all to open-what-fits —
compute the maximum affordable UNITS (capacity remaining / per-unit delivery cost,
floored; a unit is ``contracts_per_unit`` short-put contracts, so a 1x2 put ratio
spread clamps to an EVEN contract count by construction) and clamp the order quantity.
Refusal remains for: even 1 unit does not fit (existing marker text, extended to say
the downsize was attempted), and every UNMEASURABLE verdict (unknown is still not
'fine'). The naked-pair price caps are untouched.

Uses the wiring harness (real TradeActions builders, controlled book/cash).
"""
import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import (
    ASSIGNMENT_CAPACITY_REFUSAL,
)

from tests.test_option_assignment_capacity_wiring import FakeAccount, act, _own_db  # noqa: F401


IC = dict(strike_method="percent_otm", strike_param=10.0, wing_width_pct=5.0,
          dte_min=10, dte_max=40, sizing=14.0)
# FakeAccount chain at spot 100: short put 90 / long put 85, short call 110 / long call
# 115 -> width 5, credit 0.4, per-contract reserve $460; the short put costs $9,000 of
# delivery per contract. sizing=14% of $20k = $2,800 -> 6 contracts.


def test_an_ic_sized_past_capacity_opens_what_fits_not_zero():
    """$20,000 account: 6 sized units need $54,000 of delivery; capacity affords 2
    ($18,000 <= $20,000 < $27,000). The entry opens 2 — not 0, not 6."""
    acct = FakeAccount(spot=100.0, balance=20_000.0)
    res = act(acct, "open_iron_condor", **IC).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    assert sub["quantity"] == 2, sub


def test_the_downsized_reserve_reflects_the_downsized_quantity():
    """The capacity ledger after the open carries the CLAMPED size: the persisted
    option_reserve is 2 x $460, not the 6-unit $2,760."""
    acct = FakeAccount(spot=100.0, balance=20_000.0)
    res = act(acct, "open_iron_condor", **IC).execute()
    assert res["success"], res["message"]
    assert res["data"]["option_reserve"] == pytest.approx(2 * 460.0)
    assert res["data"]["quantity"] == 2


def test_capacity_for_zero_units_still_refuses_with_the_marker():
    """$8,999 of cash cannot take delivery of even ONE 90-strike put ($9,000): the
    refusal keeps the existing marker text and says the downsize was attempted."""
    acct = FakeAccount(spot=100.0, balance=8_999.0)
    res = act(acct, "open_iron_condor", **IC).execute()
    assert not res["success"]
    assert ASSIGNMENT_CAPACITY_REFUSAL in res["message"], res["message"]
    assert "downsiz" in res["message"].lower(), res["message"]


def test_an_unmeasurable_book_still_refuses_without_downsizing():
    """Downsizing is pure arithmetic on MEASURED figures; an unmeasurable held book
    must keep refusing outright (unknown is not 'fine', and there is no room number to
    clamp to)."""
    from tests.test_option_assignment_capacity_wiring import held_short_put

    acct = FakeAccount(spot=100.0, balance=1_000_000.0)
    rows = held_short_put(strike=100.0, qty=1)
    rows[1].strike = None                     # the held short put loses its strike
    acct.hold(rows)
    assert acct.short_put_assignment_exposure().cost is None
    res = act(acct, "open_iron_condor", **IC).execute()
    assert not res["success"]
    assert ASSIGNMENT_CAPACITY_REFUSAL in res["message"], res["message"]
    assert "downsiz" not in res["message"].lower(), res["message"]


def test_a_put_ratio_spread_clamps_in_units_keeping_the_short_count_even():
    """A 1x2 put ratio spread owes TWO short puts per unit. A 2-unit order on $30,000
    of cash clamps to 1 UNIT (2 short contracts, an even count) — never to 1.5 units or
    an odd contract count."""
    kw = dict(strike_method="percent_otm", strike_param=5.0, wing_width_pct=5.0,
              dte_min=10, dte_max=40, sizing=60.0)
    acct = FakeAccount(spot=100.0, balance=30_000.0)
    res = act(acct, "open_put_ratio_spread", **kw).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    assert sub["quantity"] == 1, sub
    short_leg = [l for l in sub["legs"] if l.ratio_qty == 2][0]
    per_unit = 2 * short_leg.strike * 100.0        # a UNIT is two short contracts
    # Exactly one unit fits: 1 x per_unit <= cash < 2 x per_unit.
    assert per_unit <= 30_000.0 < 2 * per_unit
