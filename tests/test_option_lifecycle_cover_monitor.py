"""OPT-L1, the continuous half: the live pass notices a covered call losing its shares.

Two cover seams already exist and NEITHER can see the case that actually happens.
``check_cover_for_covered_call`` refuses to WRITE an uncovered covered call, and the
exit guard refuses to SELL shares pledged to one. Both police platform ACTIONS. But a
broker-side risk-manager stop — ``TradeRiskManagement`` submits it as an OCO leg — fills
at 3am: the shares are gone, no platform code ran, no seam was crossed, and the short
call stays naked until somebody looks.

``run_option_lifecycle_pass`` is what looks, every pass, and it runs ahead of
OPEN_POSITIONS so a cover lost overnight is acted on before any new entry is considered
that cycle.

The headline test here is ``test_the_overnight_stop``: the REAL service, driven twice
against a doubled broker whose ``get_positions()`` goes from 100 shares to empty between
the two passes, with the platform's own ledger still holding the equity transaction
open — which is exactly the state a 3am fill leaves behind. The first pass holds. The
second raises ``cover_lost`` and closes.

The other half of the discipline gets as much room: an UNMEASURABLE cover closes
nothing. A position feed that will not answer is not a position that has gone, and this
suite fails if the pass ever liquidates on one.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional

import pytest

import ba2_trade_platform.core.option_lifecycle_service as svc
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_common.core.option_lifecycle import (
    LIFECYCLE_COVER_LOST, LIFECYCLE_HOLD, LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_UNKNOWN,
)

from tests.factories import (
    create_account_definition, create_expert_instance, create_trading_order,
    create_transaction,
)
# Reused rather than re-created: FakeAccount is built on the REAL
# ``OptionsAccountInterface`` machinery (``held_shares_for_cover``,
# ``has_pending_closing_order`` and ``submit_option_order`` are all the inherited
# implementations), and a second copy of that double is how two views of one book drift.
from tests.test_option_lifecycle_service import (
    BASE_SETTINGS, EXPIRY_FAR, EXPIRY_NEAR, FROZEN_NOW, FakeAccount, FakeExpert,
    _capture_errors, _contract, occ, open_credit_spread, quote_spread, run,
)

UNDERLYING = "ACN"
STRIKE = 110.0


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clean_breaker_state():
    svc.reset_breaker_states()
    yield
    svc.reset_breaker_states()


@pytest.fixture
def wired(monkeypatch):
    """An account + an option expert, with the service's factories pointed at them."""
    acct_def = create_account_definition()
    expert_row = create_expert_instance(account_id=acct_def.id)
    account = FakeAccount(acct_def.id)
    expert = FakeExpert(BASE_SETTINGS, expert_id=expert_row.id)
    monkeypatch.setattr(svc, "_resolve_expert", lambda eid: expert)
    monkeypatch.setattr(svc, "_resolve_account", lambda aid: account)
    return account, expert, expert_row


# --------------------------------------------------------------------------- builders
def equity_position(*, symbol=UNDERLYING, qty=100.0, side=OrderDirection.BUY,
                    asset_class="us_equity"):
    """What the BROKER publishes for a share lot. The shape the adapters hand back."""
    return SimpleNamespace(symbol=symbol, qty=qty, qty_available=qty, side=side,
                           asset_class=asset_class)


def hold_shares(account, expert_row, *, symbol=UNDERLYING, qty=100.0, price=250.0):
    """A share lot the PLATFORM and the BROKER both agree on.

    Both halves, deliberately. A Transaction plus a FILLED buy is the platform's view; a
    row in ``get_positions()`` is the broker's. A fixture that publishes only the first
    is the recurring defect in this repo's option doubles — it describes an account that
    believes it holds shares the broker has never heard of, which is not a covered call,
    it is the incident. ``hold_shares(...); account.positions_result = []`` is how a test
    asks for that state ON PURPOSE.
    """
    txn = create_transaction(
        symbol=symbol, quantity=qty, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=price, expert_id=expert_row.id,
        asset_class=AssetClass.EQUITY)
    create_trading_order(
        account.id, symbol=symbol, quantity=qty, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn.id,
        asset_class=AssetClass.EQUITY, open_price=price, filled_qty=qty)
    account.positions_result = list(account.positions_result or []) + [
        equity_position(symbol=symbol, qty=qty)]
    return txn


def open_covered_call(account, expert_row, *, underlying=UNDERLYING, expiry=EXPIRY_FAR,
                      strike=STRIKE, credit=2.00, qty=1, bought_back=False):
    """One OPENED ``covered_call``: the short call parent + its filled leg.

    ``bought_back`` adds the offsetting BUY so the contract nets flat — the structure
    that still carries the tag but can no longer have anything called away.
    """
    contract = occ(underlying, expiry, "C", strike)
    txn = create_transaction(
        symbol=underlying, quantity=qty, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_price=-abs(credit), expert_id=expert_row.id,
        multiplier=100, asset_class=AssetClass.OPTION, option_strategy="covered_call",
        expiry=expiry)
    create_trading_order(
        account.id, symbol=contract, quantity=qty, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=OrderStatus.FILLED, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, multiplier=100, contract_symbol=contract,
        option_type=OptionRight.CALL, strike=strike, expiry=expiry,
        underlying_symbol=underlying, open_price=abs(credit), filled_qty=qty)
    if bought_back:
        create_trading_order(
            account.id, symbol=contract, quantity=qty, side=OrderDirection.BUY,
            order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
            transaction_id=txn.id, asset_class=AssetClass.OPTION, multiplier=100,
            contract_symbol=contract, option_type=OptionRight.CALL, strike=strike,
            expiry=expiry, underlying_symbol=underlying, open_price=0.40, filled_qty=qty)
    return txn


def quote_call(account, *, underlying=UNDERLYING, expiry=EXPIRY_FAR, strike=STRIKE,
               px=1.50, delta=0.20):
    """Publish the call's chain row. Default: 1.55 to buy back a 2.00 credit -> +22.5%,
    healthy and clear of every rail, so no test can pass on an accidental exit."""
    c = _contract(underlying, expiry, OptionRight.CALL, strike,
                  bid=round(px - 0.05, 4), ask=round(px + 0.05, 4), last=px, delta=delta)
    account.chain[c.symbol] = c


def reasons(result) -> List[str]:
    return [d.reason for d in result.decisions]


# ===========================================================================
# The overnight stop — the whole reason this exists
# ===========================================================================
def test_the_overnight_stop(wired):
    """The REAL service, twice, with the shares leaving in between.

    Pass 1: the broker reports 100 ACN shares and the covered call is a plain hold.
    Overnight a broker-side stop fills — nothing in the platform runs, so the equity
    transaction is still OPENED and the option ledger is untouched; only
    ``get_positions()`` changes.
    Pass 2: the same code, the same book, the same clock — and now ``cover_lost``.

    Nothing about the option position changed. That is the point: this is the only
    monitor in the platform that can tell.
    """
    account, expert, expert_row = wired
    hold_shares(account, expert_row, qty=100.0)
    cc = open_covered_call(account, expert_row)
    quote_call(account)

    first = run(expert_row)
    assert reasons(first) == [LIFECYCLE_HOLD], \
        f"precondition: a fully covered call must be a plain hold, got {first.decisions!r}"
    assert first.cover_lost == []
    assert first.submitted == []

    # 3am: the risk manager's stop fills at the broker. NO platform code runs — the
    # equity Transaction is still OPENED and every option row is exactly as it was.
    account.positions_result = []

    second = run(expert_row)
    assert reasons(second) == [LIFECYCLE_COVER_LOST]
    assert [d.transaction_id for d in second.cover_lost] == [cc.id]
    assert "NAKED" in second.cover_lost[0].detail
    # ...and it does not merely report: the naked call is actually closed.
    assert [s.transaction_id for s in second.submitted] == [cc.id]
    assert second.submitted[0].reason == LIFECYCLE_COVER_LOST


def test_a_naked_covered_call_is_reported_as_such_even_while_it_is_winning(wired):
    """A profit-taking exit would ALSO have closed this one, and would have filed the
    incident under 'profit_capture'. The reason is the alarm: an operator scanning the
    day's exits has to see that a short call went naked, not that a winner was banked."""
    account, expert, expert_row = wired
    open_covered_call(account, expert_row)
    quote_call(account, px=0.90)                    # 0.95 to close a 2.00 credit -> +52.5%
    account.positions_result = []                   # a MEASURED zero: the shares are gone

    result = run(expert_row)
    assert result.decisions[0].pnl_pct >= BASE_SETTINGS["profit_capture_pct"], (
        "precondition: this structure is past its profit target, so profit_capture would "
        f"otherwise have claimed the exit; got {result.decisions!r}")
    assert reasons(result) == [LIFECYCLE_COVER_LOST]
    assert [s.reason for s in result.submitted] == [LIFECYCLE_COVER_LOST]


# ===========================================================================
# The intact book is left alone
# ===========================================================================
def test_an_intact_covered_call_is_not_flagged(wired):
    account, expert, expert_row = wired
    hold_shares(account, expert_row, qty=100.0)
    open_covered_call(account, expert_row)
    quote_call(account)

    result = run(expert_row)
    assert reasons(result) == [LIFECYCLE_HOLD]
    assert result.cover_lost == []
    assert result.cover_unmeasurable == []
    assert result.submitted == []


def test_cover_is_counted_account_wide_not_by_the_expert_that_bought_it(wired):
    """Another expert's shares still cover the call: the broker does not care who
    bought them. ``held_shares_for_cover`` reads the POSITION BOOK for that reason, and
    a fixture with no equity Transaction of our own must still be covered."""
    account, expert, expert_row = wired
    open_covered_call(account, expert_row)
    quote_call(account)
    account.positions_result = [equity_position(qty=100.0)]     # no ledger row of ours

    result = run(expert_row)
    assert reasons(result) == [LIFECYCLE_HOLD]


def test_a_short_call_already_bought_back_needs_no_cover(wired):
    """The contract nets flat, so nothing can be called away and no share count can make
    this structure naked. Without the ``required <= 0`` skip an empty position book would
    'close' every wound-down covered call in the sleeve, forever."""
    account, expert, expert_row = wired
    open_covered_call(account, expert_row, bought_back=True)
    quote_call(account)
    account.positions_result = []                     # a MEASURED zero shares

    result = run(expert_row)
    assert LIFECYCLE_COVER_LOST not in reasons(result)
    assert result.cover_lost == []


def test_a_credit_spread_is_never_flagged_for_cover(wired):
    """Only the ``covered_call`` tag promises SHARE cover — the same line the entry guard
    draws. A short put vertical against a flat account is not a naked covered call."""
    account, expert, expert_row = wired
    open_credit_spread(account, expert_row)
    quote_spread(account)
    account.positions_result = []

    result = run(expert_row)
    assert reasons(result) == [LIFECYCLE_HOLD]
    assert result.cover_lost == []
    assert result.cover_unmeasurable == []


# ===========================================================================
# Unmeasurable is not lost — nothing is liquidated on a feed that will not answer
# ===========================================================================
def test_an_unreadable_share_position_does_not_liquidate_but_does_log(wired, monkeypatch):
    """The broker answers, but the ACN row will not say how many shares it holds.

    ``held_shares_for_cover`` reports UNKNOWN for exactly that row, and the pass must
    NOT read it as 'the shares are gone'. Closing a healthy covered call over a damaged
    position row is a self-inflicted loss — and the alternative failure, saying nothing,
    is just as bad, so it has to be loud.
    """
    account, expert, expert_row = wired
    errors = _capture_errors(monkeypatch)
    cc = open_covered_call(account, expert_row)
    quote_call(account)
    account.positions_result = [equity_position(qty=None)]      # present, unquantified

    result = run(expert_row)

    assert LIFECYCLE_COVER_LOST not in reasons(result)
    assert result.cover_lost == []
    assert result.submitted == [], "an unmeasurable cover must close NOTHING"
    # ...and it is not a hold either.
    assert reasons(result) == [LIFECYCLE_UNKNOWN]
    assert [d.transaction_id for d in result.unknown] == [cc.id]
    assert result.cover_unmeasurable == [cc.id]
    said = [m for m in errors if str(cc.id) in m and "UNKNOWN" in m]
    assert said, f"the unmeasurable cover must be logged at ERROR; got {errors!r}"
    assert any("NOTHING is being closed" in m for m in said), \
        f"the log must say why no action was taken; got {said!r}"
    assert any("not 'the cover is fine'" in m.lower() for m in said), \
        f"the log must distinguish 'unknown' from 'fine'; got {said!r}"


def test_a_position_accessor_that_raises_is_unmeasurable_not_uncovered(wired, monkeypatch):
    """The same discipline one layer down: a broker call that throws leaves the cover
    unknown, closes nothing, and does not take the rest of the sleeve down with it."""
    account, expert, expert_row = wired
    errors = _capture_errors(monkeypatch)
    cc = open_covered_call(account, expert_row)
    quote_call(account)

    def boom(underlying):
        raise RuntimeError("the position service timed out")

    monkeypatch.setattr(account, "held_shares_for_cover", boom)

    result = run(expert_row)
    assert result.aborted is False
    assert LIFECYCLE_COVER_LOST not in reasons(result)
    assert result.submitted == []
    assert result.cover_unmeasurable == [cc.id]
    assert any("timed out" in m for m in errors), \
        f"the failed read must be logged at ERROR; got {errors!r}"


def test_an_unmeasurable_cover_does_not_stop_the_rest_of_the_book(wired, monkeypatch):
    """One unreadable ticker must not strand every other structure. The spread at its
    profit target still closes on the same pass."""
    account, expert, expert_row = wired
    open_covered_call(account, expert_row)
    quote_call(account)
    spread_txn, *_ = open_credit_spread(account, expert_row, underlying="MSFT")
    quote_spread(account, underlying="MSFT", short_px=0.85, long_px=0.05)   # +55%
    account.positions_result = [equity_position(qty="?")]        # unreadable ACN row

    result = run(expert_row)
    assert LIFECYCLE_UNKNOWN in reasons(result)
    assert [s.transaction_id for s in result.submitted] == [spread_txn.id]
    assert result.submitted[0].reason == LIFECYCLE_PROFIT_CAPTURE


# ===========================================================================
# A stand-down suppresses ENTRIES, not EXITS
# ===========================================================================
def test_a_cover_lost_structure_does_not_block_any_other_exit(wired):
    """The rule the circuit breaker established: a book that has lost cover must still
    be able to CLOSE. Both structures are submitted on the one pass."""
    account, expert, expert_row = wired
    cc = open_covered_call(account, expert_row)
    quote_call(account)
    spread_txn, *_ = open_credit_spread(account, expert_row, underlying="MSFT",
                                        expiry=EXPIRY_NEAR)
    quote_spread(account, underlying="MSFT", expiry=EXPIRY_NEAR)
    account.positions_result = []                    # a MEASURED zero: the cover is gone

    result = run(expert_row)
    submitted = {s.transaction_id: s.reason for s in result.submitted}
    assert submitted[cc.id] == LIFECYCLE_COVER_LOST
    assert submitted[spread_txn.id] == "roll_dte"
    assert result.failed == []


def test_a_cover_lost_close_that_is_already_working_is_not_re_submitted(wired):
    """``has_pending_closing_order`` guards this exit like every other. A cover-lost
    structure is re-decided on every pass until it actually closes, and without the
    guard that is a fresh market order every cycle."""
    account, expert, expert_row = wired
    cc = open_covered_call(account, expert_row)
    quote_call(account)
    account.positions_result = []

    first = run(expert_row)
    assert [s.transaction_id for s in first.submitted] == [cc.id]

    second = run(expert_row)
    assert second.cover_lost, "the structure is still naked, so it is still flagged"
    assert second.submitted == []
    assert second.skipped_pending_close == [cc.id]


# ===========================================================================
# Several claims on one pool
# ===========================================================================
def test_two_covered_calls_on_one_lot_are_allocated_oldest_first(wired):
    """200 shares held as two lots, two covered calls, one lot sold overnight. Comparing
    each call against the raw 100 remaining would report BOTH as comfortably covered and
    miss a genuinely naked one. First written, first covered."""
    account, expert, expert_row = wired
    older = open_covered_call(account, expert_row, strike=110.0)
    newer = open_covered_call(account, expert_row, strike=115.0)
    quote_call(account, strike=110.0)
    quote_call(account, strike=115.0)
    account.positions_result = [equity_position(qty=100.0)]      # one lot left of two

    result = run(expert_row)
    by_txn = {d.transaction_id: d.reason for d in result.decisions}
    assert by_txn[older.id] == LIFECYCLE_HOLD
    assert by_txn[newer.id] == LIFECYCLE_COVER_LOST
    assert [d.transaction_id for d in result.cover_lost] == [newer.id]


def test_a_multi_contract_covered_call_needs_the_whole_lot(wired):
    """3 contracts is 300 shares, not 100. Sizing the requirement off the contract count
    alone (or off a hard-coded one lot) reports a 200-share shortfall as covered."""
    account, expert, expert_row = wired
    open_covered_call(account, expert_row, qty=3)
    quote_call(account)
    account.positions_result = [equity_position(qty=200.0)]

    result = run(expert_row)
    assert reasons(result) == [LIFECYCLE_COVER_LOST]
    assert "300 required" in result.cover_lost[0].detail


def test_the_position_book_is_asked_once_per_ticker(wired, monkeypatch):
    """Three covered calls on one name are one question. The accessor re-reads
    ``get_positions()`` every time it is asked, and a per-structure read would also let
    two structures on one pass see two different books."""
    account, expert, expert_row = wired
    calls: List[str] = []
    real = account.held_shares_for_cover

    def counting(underlying):
        calls.append(underlying)
        return real(underlying)

    monkeypatch.setattr(account, "held_shares_for_cover", counting)
    for strike in (110.0, 115.0, 120.0):
        open_covered_call(account, expert_row, strike=strike)
        quote_call(account, strike=strike)
    account.positions_result = [equity_position(qty=1000.0)]

    run(expert_row)
    assert calls == [UNDERLYING]


# ===========================================================================
# The ledger itself can be the thing that cannot be measured
# ===========================================================================
def test_a_short_leg_with_no_option_type_is_unmeasurable_not_uncovered(wired, monkeypatch):
    """A short leg whose right was never recorded MIGHT be the call. The requirement is
    then unknown, and unknown closes nothing — in either direction."""
    from ba2_common.core.trade_store import orders_where
    from ba2_trade_platform.core.db import update_instance

    account, expert, expert_row = wired
    errors = _capture_errors(monkeypatch)
    cc = open_covered_call(account, expert_row)
    quote_call(account)
    account.positions_result = []
    for order in orders_where(transaction_id=cc.id):
        if order.contract_symbol:
            order.option_type = None
            update_instance(order)

    result = run(expert_row)
    assert LIFECYCLE_COVER_LOST not in reasons(result)
    assert result.submitted == []
    assert result.cover_unmeasurable == [cc.id]
    assert reasons(result) == [LIFECYCLE_UNKNOWN]
    assert any("UNKNOWN" in m and str(cc.id) in m for m in errors), \
        f"an unsizeable obligation must be logged at ERROR; got {errors!r}"
