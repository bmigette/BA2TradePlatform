"""Option FILL prints: an isolated off-market open is never a fill, and a structure's net never
leaves its no-arbitrage bounds (plan: option fill prints, 2026-10-06).

The shapes come from the stage-1 O_BULLCS winner (bt 79):
  * LRCX 2022-10-12, 370/385 call spread -- the 370C "opened" at 5.06 inside a 5.06-13.35 day
    while the 385C opened at 9.87: the spread was filled at a NEGATIVE debit (paid to open).
  * CSCO 2023-12-01, 50/52.5 call spread, 115 contracts -- the 50C opened at 0.10 (range
    0.10-0.44, prior bid 0.43): net debit -0.01.

Every test drives the REAL entry path (``DailyBacktestEngine`` + an option entry action) through
``_structure_engine``, never hand-built order dicts, and each one fails on origin/dev where it
describes the bug.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus
from tests.backtest._structure_engine import (
    SESSIONS, SPOT, build_account, implied_iv, model_price, occ, run_structure)

FILL_DAY = "2024-02-02"      # the session after the 2024-02-01 decision bar
DECISION_DAY = "2024-02-01"


def _published(acct):
    """The results dict the grid persists (``build_results``), where the counters must show."""
    from app.services.backtest.results import build_results
    from tests.backtest._structure_engine import BASE_CFG
    acct.snapshot_equity(datetime(2024, 2, 9, tzinfo=timezone.utc))
    return build_results(acct, {"initial_capital": BASE_CFG["starting_cash"],
                                "account_settings": dict(BASE_CFG), "option_trade_records": True})


def _filled_children(acct, parent_id):
    return {o.contract_symbol: o for o in acct.get_orders()
            if o.parent_order_id == parent_id and o.status == OrderStatus.FILLED}


def _parent(acct):
    return next(o for o in acct.get_orders()
                if o.parent_order_id is None and o.option_strategy)


# ----------------------------------------------------------------------------------------
# Issue 1 -- the LRCX / CSCO shapes
# ----------------------------------------------------------------------------------------
def test_lrcx_shape_a_garbage_low_open_never_fills_a_negative_debit():
    """The long 180C opens at 1.00 (its session ran up to its real 6.5, which is also the close)
    while the short 185C opens at 4.30: the legs' own opens give a debit of -3.30. Unmodified
    origin/dev filled it there. The garbage print is repriced from the decision bar's iv."""
    long_call, short_call = occ("C", 180.0), occ("C", 185.0)
    _, acct, ctx, _res = run_structure(
        "open_bull_call_spread",
        overrides={(long_call, FILL_DAY): {"open": 1.00}})
    try:
        results = _published(acct)
        parent = _parent(acct)
        assert parent.status == OrderStatus.FILLED
        # The net debit of a call vertical lies strictly inside (0, width 5.00).
        assert 0.0 < parent.open_price < 5.0, parent.open_price
        legs = _filled_children(acct, parent.id)
        assert legs[long_call].open_price == pytest.approx(
            model_price("C", 180.0, datetime(2024, 2, 2).date()), abs=0.05)
        assert legs[short_call].open_price == pytest.approx(
            model_price("C", 185.0, datetime(2024, 2, 2).date()), abs=1e-9)
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_rejected"] == 1
        assert stats["option_fill_prints_replaced"] == 1
        assert stats["option_fill_prints_refused"] == 0
        assert stats["option_fills_at_replaced_print"] == 1      # the leg really filled there
        assert len(stats["option_fill_print_examples"]) == 1
        assert long_call in stats["option_fill_print_examples"][0]
        # ... and it is in the results dict the grid persists.
        assert results["option_fill_prints_replaced"] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_csco_shape_a_cheap_leg_opening_far_below_its_prior_bid_is_not_a_fill():
    """CSCO: a call quoted 0.43 the day before (session high 0.44) opens at 0.10. Same defect on
    a cheap leg: the 0.25 absolute floor of the tolerance (it binds, 50% of 0.43 is 0.215) must
    not hide a print 0.33 outside the references."""
    long_call = occ("C", 200.0)
    d1, d2 = datetime(2024, 2, 1).date(), datetime(2024, 2, 2).date()
    cheap = {"open": 0.43, "high": 0.44, "low": 0.43, "close": 0.43}
    overrides = {
        (long_call, DECISION_DAY): {**cheap, "iv": implied_iv(0.43, "C", 200.0, d1)},
        (long_call, FILL_DAY): {**cheap, "iv": implied_iv(0.43, "C", 200.0, d2),
                                "open": 0.10}}
    _, acct, ctx, _ = run_structure("buy_call", strike_method="percent_otm", strike_param=11.0,
                                    overrides=overrides)
    try:
        order = next(o for o in acct.get_orders() if o.contract_symbol == long_call
                     and o.status == OrderStatus.FILLED)
        assert order.open_price == pytest.approx(0.43, abs=0.03), order.open_price
        assert acct.option_integrity_stats()["option_fill_prints_replaced"] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_a_single_long_call_with_a_garbage_low_open_is_not_filled_at_it():
    """The single-leg case the vertical tests do not cover: a long call filled at a garbage low
    open is a free option. Every call strike opens at 0.50 here; the fill is repriced."""
    garbage = {(occ("C", k), FILL_DAY): {"open": 0.50} for k in (170.0, 175.0, 180.0, 185.0, 190.0)}
    _, acct, ctx, _ = run_structure("buy_call", overrides=garbage)
    try:
        order = next(o for o in acct.get_orders()
                     if o.contract_symbol and o.status == OrderStatus.FILLED)
        ref = model_price("C", order.strike, datetime(2024, 2, 2).date())
        assert order.open_price > 1.0, f"filled at the garbage print: {order.open_price}"
        assert order.open_price == pytest.approx(ref, abs=0.05)
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_replaced"] == 1 and stats["option_fill_prints_refused"] == 0
    finally:
        ctx.__exit__(None, None, None)


def test_a_normal_print_is_filled_untouched():
    """Bit-identical: no rejection, no counter, the fill IS the bar's open."""
    _, acct, ctx, _res = run_structure("open_bull_call_spread")
    try:
        results = _published(acct)
        parent = _parent(acct)
        legs = _filled_children(acct, parent.id)
        d = datetime(2024, 2, 2).date()
        assert legs[occ("C", 180.0)].open_price == model_price("C", 180.0, d)
        assert legs[occ("C", 185.0)].open_price == model_price("C", 185.0, d)
        stats = acct.option_integrity_stats()
        for key in ("option_fill_prints_rejected", "option_fill_prints_replaced",
                    "option_fill_prints_refused", "option_fill_prints_unverified",
                    "option_structure_fills_refused"):
            assert stats[key] == 0, key
        assert results["option_fill_prints_rejected"] == 0
    finally:
        ctx.__exit__(None, None, None)


def test_a_real_iv_drift_between_the_decision_and_the_close_is_not_rejected():
    """A genuine open between the two references (iv rose across the session) is not touched:
    the close prints 7.50 (iv up) and the open sits at 7.00, between BS(prior iv) 6.50 and
    BS(close iv) 7.50."""
    long_call = occ("C", 180.0)
    _, acct, ctx, _ = run_structure(
        "open_bull_call_spread",
        overrides={(long_call, FILL_DAY): {"open": 7.00, "close": 7.50, "iv": 0.29}})
    try:
        assert acct.option_integrity_stats()["option_fill_prints_rejected"] == 0
        assert acct.option_integrity_stats()["option_fill_prints_unverified"] == 0
    finally:
        ctx.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------
# Issue 1 -- the refusal path and the structure bounds
# ----------------------------------------------------------------------------------------
def test_a_rejected_print_with_no_defensible_price_refuses_the_order_and_counts_it():
    """No decision-bar iv: the garbage open cannot be repriced, so the order is REFUSED (it
    stays pending, then expires as a DAY order) rather than filled at a made-up price."""
    long_call = occ("C", 180.0)
    _, acct, ctx, _res = run_structure(
        "open_bull_call_spread",
        overrides={(long_call, FILL_DAY): {"open": 1.00},
                   (long_call, DECISION_DAY): {"iv": None}})
    try:
        results = _published(acct)
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_refused"] >= 1
        assert stats["option_fill_prints_replaced"] == 0
        assert stats["option_fill_prints_rejected"] == stats["option_fill_prints_refused"]
        assert results["option_fill_prints_refused"] == stats["option_fill_prints_refused"]
        # Nothing was filled at the garbage print on the fill day: any structure that exists
        # opened on a LATER session, at a sane price.
        for o in acct.get_orders():
            if o.contract_symbol == long_call and o.status == OrderStatus.FILLED:
                assert o.open_price > 1.5
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("long_open,short_open,label", [
    (3.00, 5.00, "negative debit (paid to open)"),
    (9.00, 3.00, "debit above the 5.00 width (a guaranteed loss)"),
])
def test_a_net_outside_the_structure_bounds_is_never_accepted(long_open, short_open, label):
    """With NO iv anywhere the per-leg screen has nothing to judge (both prints are
    'unverified'), so the structure's own bounds are the last line: a debit vertical's net
    must lie strictly inside (0, width)."""
    long_call, short_call = occ("C", 180.0), occ("C", 185.0)
    overrides = {}
    for d in SESSIONS:
        for sym in (long_call, short_call):
            overrides[(sym, d.isoformat())] = {"iv": None}
    overrides[(long_call, FILL_DAY)] = {"open": long_open, "close": long_open, "iv": None}
    overrides[(short_call, FILL_DAY)] = {"open": short_open, "close": short_open, "iv": None}
    _, acct, ctx, _res = run_structure("open_bull_call_spread", overrides=overrides)
    try:
        results = _published(acct)
        parents = sorted((o for o in acct.get_orders()
                          if o.parent_order_id is None and o.option_strategy),
                         key=lambda o: o.id)
        assert parents[0].status != OrderStatus.FILLED, label   # the bad session did not fill
        # Any structure that did open (a later, sane session) sits strictly inside (0, width).
        for p in parents:
            if p.status == OrderStatus.FILLED:
                assert 0.0 < p.open_price < 5.0, (label, p.open_price)
        assert acct.option_integrity_stats()["option_structure_fills_refused"] >= 1, label
        assert results["option_structure_fills_refused"] >= 1
        assert results["option_structure_fill_examples"]
    finally:
        ctx.__exit__(None, None, None)


def test_a_credit_structure_collecting_more_than_its_width_is_refused():
    """The mirror: a bull put spread (width 5) cannot collect 5 or more."""
    short_put, long_put = occ("P", 180.0), occ("P", 175.0)
    overrides = {}
    for d in SESSIONS:
        for sym in (short_put, long_put):
            overrides[(sym, d.isoformat())] = {"iv": None}
    overrides[(short_put, FILL_DAY)] = {"open": 9.0, "close": 9.0, "iv": None}
    overrides[(long_put, FILL_DAY)] = {"open": 2.0, "close": 2.0, "iv": None}
    _, acct, ctx, _ = run_structure("open_bull_put_spread", overrides=overrides)
    try:
        assert _parent(acct).status != OrderStatus.FILLED
        assert acct.option_integrity_stats()["option_structure_fills_refused"] >= 1
    finally:
        ctx.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------
# Issue 1 -- exits that use a print
# ----------------------------------------------------------------------------------------
def test_an_exit_at_a_garbage_print_is_repriced_not_taken():
    """Hold a long call, then sell-to-close it on a session whose OPEN is garbage (0.10 against a
    ~6 premium): the exit takes the repriced premium, not 0.10."""
    from ba2_common.core.option_types import OptionLeg
    sym = occ("C", 180.0)
    exit_day = "2024-02-06"
    acct, ps, ctx, _ = build_account(overrides={(sym, exit_day): {"open": 0.10}})
    try:
        def leg(side, intent):
            return OptionLeg(contract_symbol=sym, side=side, ratio_qty=1, position_intent=intent,
                             option_type=OptionRight.CALL, strike=180.0,
                             expiry=datetime(2024, 3, 15).date(), underlying="AAPL")
        acct.submit_option_order(legs=[leg(OrderDirection.BUY, "buy_to_open")], quantity=2,
                                 order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[sym].qty == 2
        ps.set_clock(datetime(2024, 2, 5))      # decide on the 5th, fill on the 6th
        closing = acct.submit_option_order(
            legs=[leg(OrderDirection.SELL, "sell_to_close")], quantity=2,
            order_type="market", option_strategy="close")
        acct.refresh_orders()
        closed = acct.get_order(closing.id)
        assert closed.status == OrderStatus.FILLED
        ref = model_price("C", 180.0, datetime(2024, 2, 6).date())
        assert closed.open_price == pytest.approx(ref, abs=0.05), closed.open_price
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_replaced"] == 1
        assert stats["option_fill_print_examples"][0].startswith("replaced:")
    finally:
        ctx.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------
# Issue 2 -- the payoff bounds of a held structure derive, through the real order path
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("action,strategy,legs", [
    ("open_bull_call_spread", "bull_call_spread", 2),
    ("open_bull_put_spread", "bull_put_spread", 2),
    ("open_iron_condor", "iron_condor", 4),
    ("open_call_butterfly", "call_butterfly", 3),
])
def test_group_bounds_derive_for_every_defined_risk_structure(action, strategy, legs):
    _, acct, ctx, _res = run_structure(action)
    try:
        results = _published(acct)
        contract_group, group_bounds = acct._option_group_bounds()
        assert len(contract_group) == legs
        (gb,) = group_bounds.values()
        assert gb["strategy"] == strategy
        assert gb["lo"] is not None and gb["hi"] is not None
        assert gb["lo"] <= 0.0 <= gb["hi"] or gb["lo"] == 0.0
        assert results["option_clamp_fallbacks"] == 0
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("action", ["open_bull_call_spread", "open_bull_put_spread",
                                    "open_iron_condor", "open_call_butterfly"])
def test_a_dead_first_attempt_on_the_same_contracts_does_not_hijack_the_group(action):
    """THE ROOT CAUSE of ``group_bounds_underivable`` (2,713 of 2,713 calls on bt 79): the first
    attempt's DAY order expired unfilled, the engine re-selected the SAME contracts the next
    day, and the held lots were attributed to the FIRST order that named them -- the dead
    parent, whose legs are all EXPIRED ("0 of 0 opening leg order(s)")."""
    _, probe, ctx0, _ = run_structure(action)
    try:
        first_leg = next(o for o in probe.get_orders() if o.parent_order_id is not None)
        refused_leg = first_leg.contract_symbol
    finally:
        ctx0.__exit__(None, None, None)
    # Day 1 cannot fill (one leg trades 1 contract that session: far below the participation
    # cap of the order), the order expires, and day 2 fills normally.
    _, acct, ctx, _res = run_structure(
        action, overrides={(refused_leg, FILL_DAY): {"volume": 1}})
    try:
        results = _published(acct)
        parents = [o for o in acct.get_orders() if o.parent_order_id is None and o.option_strategy]
        assert len(parents) >= 2, "the first attempt must have expired and been re-placed"
        assert parents[0].status != OrderStatus.FILLED
        contract_group, group_bounds = acct._option_group_bounds()
        assert contract_group, "a structure must be held"
        (gb,) = group_bounds.values()
        assert gb["lo"] is not None and gb["hi"] is not None
        assert results["option_clamp_fallbacks"] == 0
        # the held group is the FILLED parent, not the dead first one
        assert set(contract_group.values()) == {next(p.id for p in parents
                                                     if p.status == OrderStatus.FILLED)}
    finally:
        ctx.__exit__(None, None, None)
