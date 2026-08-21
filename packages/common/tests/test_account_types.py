"""ba2_common.core.account_types: the four broker-seam value objects.

These are pure dataclasses with no DB/SDK deps. They exist so that no call site
has to guess whether get_account_info() handed it a pydantic object (Alpaca), a
dict (IBKR/TastyTrade) or None (Alpaca auth failure).
"""
from datetime import date

from ba2_common.core.account_types import (
    CASH_TRANSFER_DEPOSIT,
    CASH_TRANSFER_DIVIDEND,
    CASH_TRANSFER_WITHDRAWAL,
    MARGIN_SOURCE_ASSET,
    MARGIN_SOURCE_DEFAULT,
    AccountSnapshot,
    CashTransfer,
    MarginInfo,
    OrderImpact,
)


def test_account_snapshot_defaults_every_money_field_to_none():
    """An empty snapshot means "the broker told us nothing", never "zero".

    The caller must raise rather than substitute a default, so a 0.0 default here
    would silently authorise a plan against an account we know nothing about.
    """
    snap = AccountSnapshot()
    assert snap.cash is None
    assert snap.equity is None
    assert snap.net_liquidation is None
    assert snap.buying_power is None
    assert snap.non_marginable_buying_power is None
    assert snap.margin_multiplier is None
    assert snap.long_market_value is None
    assert snap.short_market_value is None
    assert snap.pending_transfer_in is None
    assert snap.is_margin_account is False
    assert snap.supports_fractional is False
    assert snap.raw == {}


def test_account_snapshot_raw_dicts_are_not_shared_between_instances():
    a = AccountSnapshot()
    b = AccountSnapshot()
    a.raw["x"] = 1
    assert b.raw == {}


def test_cash_transfer_deposit_is_income():
    ev = CashTransfer(external_id="act-1", event_date=date(2026, 8, 1),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=1000.0)
    assert ev.is_income is True


def test_cash_transfer_dividend_is_income():
    ev = CashTransfer(external_id="act-3", event_date=date(2026, 8, 10),
                      event_type=CASH_TRANSFER_DIVIDEND, amount=12.34, symbol="AAPL")
    assert ev.is_income is True


def test_cash_transfer_withdrawal_is_not_income():
    ev = CashTransfer(external_id="act-2", event_date=date(2026, 8, 5),
                      event_type=CASH_TRANSFER_WITHDRAWAL, amount=-250.0)
    assert ev.is_income is False


def test_cash_transfer_zero_amount_deposit_is_not_income():
    """A zero-dollar deposit cannot fund an allocation run."""
    ev = CashTransfer(external_id="act-4", event_date=date(2026, 8, 6),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=0.0)
    assert ev.is_income is False


def test_cash_transfer_negative_amount_deposit_is_not_income():
    """A reversed/returned deposit arrives as a DEPOSIT with a negative amount.

    This is the case the ``amount > 0`` guard exists for: without it, a clawback
    would be counted as new money to allocate.
    """
    ev = CashTransfer(external_id="act-5", event_date=date(2026, 8, 7),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=-1000.0)
    assert ev.is_income is False


def test_cash_transfer_event_type_literals_are_the_persisted_spellings():
    """These strings go into portfolio_income_event.event_type, so they are a
    schema contract: respelling one orphans every row already stored under it."""
    assert CASH_TRANSFER_DEPOSIT == "DEPOSIT"
    assert CASH_TRANSFER_WITHDRAWAL == "WITHDRAWAL"
    assert CASH_TRANSFER_DIVIDEND == "DIVIDEND"


def test_margin_info_defaults_to_the_conservative_source():
    info = MarginInfo(symbol="AAPL", bp_factor=2.0)
    assert info.source == MARGIN_SOURCE_DEFAULT == "default"
    assert info.marginable is True
    assert info.fractionable is None       # TRI-STATE: "the broker did not say"
    assert info.min_order_size is None
    assert info.min_trade_increment is None


def test_margin_info_defaults_the_fractional_money_floor_to_unknown():
    """``None`` means "the broker published no floor", never 0.0 -- the same
    no-fabricated-values rule ``min_trade_increment`` follows."""
    assert MarginInfo(symbol="AAPL", bp_factor=1.0).min_fractional_notional is None


def test_margin_info_separates_a_share_minimum_from_a_money_minimum():
    """UNITS, and they are NOT interchangeable.

    ``min_order_size`` is a SHARE COUNT: it mirrors Alpaca's
    ``Asset.min_order_size`` and the engine compares it against a rounded share
    quantity (``portfolio_allocation.round_quantity`` -> ``qty < min_order_size``).
    ``min_fractional_notional`` is DOLLARS.

    TastyTrade publishes only the money one -- verbatim, from a live dry-run:
    ``below_notional_value_minimum: Fractional equities orders cannot have a
    notional value less than $5.`` Parking that 5.0 in ``min_order_size`` would be
    read by the engine as "5 SHARES": on a $34 ETF that suppresses every order
    below $170 instead of every order below $5, i.e. a 34x over-suppression that
    silently stops the account trading and looks like nothing at all.
    """
    info = MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                      min_fractional_notional=5.0)
    assert info.min_fractional_notional == 5.0
    assert info.min_order_size is None      # different number, different UNIT


def test_margin_info_records_where_the_factor_came_from():
    info = MarginInfo(symbol="AAPL", bp_factor=1.0, initial_margin_rate=0.5,
                      source=MARGIN_SOURCE_ASSET)
    assert info.source == MARGIN_SOURCE_ASSET
    assert info.initial_margin_rate == 0.5


def test_order_impact_bp_cost_flips_the_brokers_negative_buy_sign():
    """TastyTrade reports a BUY as a NEGATIVE change_in_buying_power. The engine
    consumes a POSITIVE cost, so bp_cost must flip the sign."""
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=-1500.0)
    assert impact.bp_cost == 1500.0


def test_order_impact_bp_cost_is_zero_when_the_order_frees_buying_power():
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=900.0)
    assert impact.bp_cost == 0.0


def test_order_impact_bp_cost_is_zero_at_exactly_zero_change():
    """The boundary of the ``< 0`` branch: a no-op order consumes nothing."""
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=0.0)
    assert impact.bp_cost == 0.0


def test_order_impact_defaults_to_accepted_with_no_errors():
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=-10.0)
    assert impact.accepted is True
    assert impact.warnings == []
    assert impact.errors == []


# ---------------------------------------------------------------------------
# MarginInfo.fractionable is TRI-STATE
# ---------------------------------------------------------------------------

def test_margin_info_fractionable_carries_all_three_states():
    """False means the broker SAID no. None means it said nothing. Collapsing the
    two is how "the lookup failed" becomes "this symbol is not fractionable"."""
    said_yes = MarginInfo(symbol="AAPL", bp_factor=1.0, fractionable=True)
    said_no = MarginInfo(symbol="BRKA", bp_factor=1.0, fractionable=False)
    said_nothing = MarginInfo(symbol="XYZ", bp_factor=1.0)

    assert said_yes.fractionable is True
    assert said_no.fractionable is False
    assert said_nothing.fractionable is None


def test_unknown_fractionability_is_falsy_so_the_whole_share_fallback_is_unchanged():
    """The widening must not change any landed behaviour: _round_shares tests
    `margin.fractionable` for truth, and None is falsy, so an undescribed symbol
    still rounds to whole shares -- the conservative direction."""
    assert not MarginInfo(symbol="XYZ", bp_factor=1.0).fractionable


def test_fractionable_true_with_an_unknown_step_is_still_a_legal_pair():
    """The landed min_trade_increment contract is unchanged: None means the broker
    published no step, and the caller falls back to whole shares."""
    info = MarginInfo(symbol="AAPL", bp_factor=1.0, fractionable=True,
                      min_trade_increment=None)
    assert info.fractionable is True
    assert info.min_trade_increment is None


# ---------------------------------------------------------------------------
# MarketHours
# ---------------------------------------------------------------------------

def test_market_hours_defaults_to_closed_with_nothing_known():
    """The zero value must be SHUT. A default of is_open=True would let a
    half-built adapter authorise submission into a closed market."""
    from ba2_common.core.account_types import (
        MARKET_HOURS_SOURCE_FALLBACK, MarketHours)

    hours = MarketHours(is_open=False)

    assert hours.is_open is False
    assert hours.open_at is None
    assert hours.close_at is None
    assert hours.next_open is None
    assert hours.next_close is None
    assert hours.status is None
    assert hours.detail is None
    assert hours.as_of is None
    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK


def test_market_hours_is_frozen():
    """Task 83 caches one instance and hands it to every caller; an in-place edit
    would poison every later reader (same rule as MarginInfo)."""
    import dataclasses
    import pytest
    from ba2_common.core.account_types import MarketHours

    hours = MarketHours(is_open=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        hours.is_open = False


def test_dataclasses_replace_is_how_an_adapter_derives_a_variant():
    """The adapters' fallback path is
    `dataclasses.replace(super()._get_market_hours_impl(now), detail=reason)`,
    so replace() must work on this type and must not disturb `source`."""
    import dataclasses
    from ba2_common.core.account_types import MARKET_HOURS_SOURCE_FALLBACK, MarketHours

    base = MarketHours(is_open=False, source=MARKET_HOURS_SOURCE_FALLBACK)
    derived = dataclasses.replace(base, detail="broker clock endpoint 503")

    assert derived.detail == "broker clock endpoint 503"
    assert derived.source == MARKET_HOURS_SOURCE_FALLBACK
    assert base.detail is None


def test_status_carries_the_brokers_own_word_unnormalised():
    """The banner says "extended hours" from THIS field. There is no close_at_ext
    field and is_open stays regular-session-only."""
    from ba2_common.core.account_types import MarketHours

    hours = MarketHours(is_open=False, status="Extended",
                        detail="Extended-hours session; regular session is closed")

    assert hours.status == "Extended"
    assert hours.is_open is False


def test_next_transition_is_the_earlier_of_next_open_and_next_close():
    """While the market is OPEN the next status change is today's close, even
    though next_open (tomorrow morning) is also populated."""
    from datetime import datetime, timezone
    from ba2_common.core.account_types import MarketHours

    hours = MarketHours(
        is_open=True,
        next_close=datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
        next_open=datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc),
    )

    assert hours.next_transition == datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)


def test_next_transition_while_shut_is_the_next_open():
    from datetime import datetime, timezone
    from ba2_common.core.account_types import MarketHours

    hours = MarketHours(
        is_open=False,
        next_open=datetime(2026, 8, 24, 13, 30, tzinfo=timezone.utc),
        next_close=datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
    )

    assert hours.next_transition == datetime(2026, 8, 24, 13, 30, tzinfo=timezone.utc)


def test_next_transition_is_none_when_no_boundary_is_known():
    """An "unavailable" answer has no boundary, so nothing can be cached against one."""
    from ba2_common.core.account_types import MarketHours

    assert MarketHours(is_open=False).next_transition is None


def test_is_known_is_false_only_for_the_unavailable_source():
    from ba2_common.core.account_types import (
        MARKET_HOURS_SOURCE_BROKER,
        MARKET_HOURS_SOURCE_FALLBACK,
        MARKET_HOURS_SOURCE_UNAVAILABLE,
        MarketHours,
    )

    assert MarketHours(is_open=True, source=MARKET_HOURS_SOURCE_BROKER).is_known is True
    assert MarketHours(is_open=False, source=MARKET_HOURS_SOURCE_FALLBACK).is_known is True
    assert MarketHours(is_open=False, source=MARKET_HOURS_SOURCE_UNAVAILABLE).is_known is False


def test_an_unavailable_answer_is_shut_but_not_known():
    """The whole reason for the third constant: the submit gate fails closed while
    the banner still says "unknown" rather than lying that the market is closed."""
    from ba2_common.core.account_types import (
        MARKET_HOURS_SOURCE_UNAVAILABLE, MarketHours)

    hours = MarketHours(is_open=False, source=MARKET_HOURS_SOURCE_UNAVAILABLE,
                        detail="broker clock endpoint 503 and no offline calendar")

    assert hours.is_open is False
    assert hours.is_known is False
    assert "503" in hours.detail


def test_the_three_source_spellings_are_exactly_these():
    from ba2_common.core.account_types import (
        MARKET_HOURS_SOURCE_BROKER,
        MARKET_HOURS_SOURCE_FALLBACK,
        MARKET_HOURS_SOURCE_UNAVAILABLE,
    )

    assert MARKET_HOURS_SOURCE_BROKER == "broker"
    assert MARKET_HOURS_SOURCE_FALLBACK == "fallback"
    assert MARKET_HOURS_SOURCE_UNAVAILABLE == "unavailable"


# --- the naive-datetime invariant every other chunk relies on ----------------

def test_a_naive_datetime_in_any_slot_is_refused_at_construction():
    """Read as UTC instead of Eastern, a naive instant moves the 09:30/16:00
    boundary by four or five hours -- the difference between "submit" and "the
    broker rejects the batch". The seam must be UNABLE to emit one."""
    from datetime import datetime
    import pytest
    from ba2_common.core.account_types import MarketHours

    naive = datetime(2026, 8, 19, 12, 0)
    for slot in ("open_at", "close_at", "next_open", "next_close", "as_of"):
        with pytest.raises(ValueError, match="timezone-aware"):
            MarketHours(is_open=True, **{slot: naive})


def test_the_naive_error_names_the_offending_field():
    from datetime import datetime
    import pytest
    from ba2_common.core.account_types import MarketHours

    with pytest.raises(ValueError, match="next_open"):
        MarketHours(is_open=False, next_open=datetime(2026, 8, 24, 9, 30))


def test_aware_datetimes_in_every_slot_are_accepted():
    from datetime import datetime, timezone
    from ba2_common.core.account_types import MarketHours

    when = datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    hours = MarketHours(is_open=True, open_at=when, close_at=when,
                        next_open=when, next_close=when, as_of=when)

    assert hours.as_of == when


def test_is_open_is_REQUIRED_so_no_adapter_can_half_fill_this_into_an_open():
    """No default for ``is_open``. Found by mutation: giving it ``= True``
    survived the whole file, because every other test passes it explicitly.

    The docstring of test_market_hours_defaults_to_closed_with_nothing_known
    claims this invariant ("A default of is_open=True would let a half-built
    adapter authorise submission into a closed market") but cannot pin it --
    it constructs MarketHours(is_open=False). This one pins it: an adapter that
    forgets the field gets a TypeError at construction, not a silent "open".
    """
    import pytest
    from ba2_common.core.account_types import MarketHours

    with pytest.raises(TypeError):
        MarketHours()
