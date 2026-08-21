"""The concrete broker seams on ReadOnlyAccountInterface / AccountInterface.

They are CONCRETE, never @abstractmethod: ReadOnlyAccountInterface already has 12
abstract methods, and adding a 13th would break instantiation of IBKRAccount,
TastyTradeAccount and every stub in the test suite.

_DictAccount below is the IBKR / TastyTrade shape: get_account_info() returns a
plain dict. AlpacaAccount's pydantic shape is covered in tests/test_alpaca_account_snapshot.py.
"""
import dataclasses
from datetime import datetime

import pytest

from ba2_common.core.account_types import (
    MARKET_HOURS_SOURCE_BROKER,
    MARKET_HOURS_SOURCE_FALLBACK,
    MARKET_HOURS_SOURCE_UNAVAILABLE,
    AccountSnapshot,
    MarketHours,
)
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.market_calendar import NY_TZ, clear_nyse_calendar_cache


class _DictAccount(ReadOnlyAccountInterface):
    """A broker whose get_account_info() returns a dict (IBKR / TastyTrade shape)."""

    def __init__(self, id, info):
        self.id = id
        self._info = info
        self._settings_cache = None

    def get_account_info(self):
        return self._info

    def get_balance(self):
        return None

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def symbols_exist(self, symbols):
        return {}

    def _get_instrument_current_price_impl(self, *a, **k):
        return None

    def get_balance_history(self, *a, **k):
        return []

    def get_dividends(self, *a, **k):
        return []

    def get_filled_trades(self, *a, **k):
        return []

    def get_order(self, *a, **k):
        return None

    def refresh_orders(self, *a, **k):
        return True

    def refresh_positions(self, *a, **k):
        return True


def test_snapshot_from_a_dict_broker_reads_the_tastytrade_field_names():
    """TastyTrade names them cash_balance / net_liquidating_value / equity_buying_power."""
    acct = _DictAccount(1, {
        "cash_balance": "12000.25",
        "net_liquidating_value": "48000.00",
        "equity_buying_power": "96000.00",
        "margin_multiplier": "2",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 12000.25
    assert snap.net_liquidation == 48000.00
    assert snap.buying_power == 96000.00
    assert snap.margin_multiplier == 2.0


def test_snapshot_from_a_dict_broker_reads_the_plain_field_names():
    acct = _DictAccount(1, {
        "cash": "500.00",
        "equity": "10000.00",
        "buying_power": "10000.00",
        "long_market_value": "9500.00",
        "short_market_value": "0",
        "multiplier": "1",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 10000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == 0.0


def test_a_broker_publishing_only_portfolio_value_gets_both_equity_and_net_liquidation():
    """AccountSnapshot's contract (account_types.py): "An adapter whose broker
    publishes only one MUST set BOTH to that value rather than leave one None."

    ``portfolio_value`` was in the equity chain but not the net_liquidation one,
    so such a broker got equity set and net_liquidation None -- and
    net_liquidation is the headline total value the page reports. The base
    mirrors whichever one it found rather than making every adapter remember.
    """
    snap = _DictAccount(1, {"portfolio_value": "48000.00"}).get_account_snapshot()

    assert snap.equity == 48000.00
    assert snap.net_liquidation == 48000.00


def test_a_broker_publishing_only_net_liquidation_gets_equity_too():
    """The mirror runs both ways -- it is one rule, not a portfolio_value patch."""
    snap = _DictAccount(1, {"net_liquidation": "31000.00"}).get_account_snapshot()

    assert snap.net_liquidation == 31000.00
    assert snap.equity == 31000.00


def test_the_two_are_left_alone_when_the_broker_publishes_both():
    """They legitimately DIVERGE where liquidation value is not the mark; the
    mirror must only fill a hole, never overwrite."""
    snap = _DictAccount(1, {"equity": "50000", "net_liquidation": "49500"}).get_account_snapshot()

    assert snap.equity == 50000.0
    assert snap.net_liquidation == 49500.0


def test_a_positive_short_magnitude_is_negated_to_the_platform_convention():
    """AccountSnapshot: short_market_value is NEGATIVE while shorts are held.

    TastyTrade publishes ``short-equity-value`` as a positive MAGNITUDE. Left
    unnegated it flips the sign of gross exposure, so the base normalises it and
    no adapter has to remember.
    """
    snap = _DictAccount(1, {"short_market_value": "2500.00"}).get_account_snapshot()

    assert snap.short_market_value == -2500.00


def test_an_already_negative_short_value_is_untouched():
    """Alpaca's convention is already negative -- do not double-negate it."""
    snap = _DictAccount(1, {"short_market_value": "-2500.00"}).get_account_snapshot()

    assert snap.short_market_value == -2500.00


def test_a_flat_account_reports_a_zero_short_value_not_minus_zero():
    snap = _DictAccount(1, {"short_market_value": "0"}).get_account_snapshot()

    assert snap.short_market_value == 0.0


def test_derivative_buying_power_is_the_last_resort_for_buying_power():
    """The third name in the chain, and the only one an options-only TastyTrade
    account may publish. It had no coverage at all."""
    snap = _DictAccount(1, {"derivative_buying_power": "7500.00"}).get_account_snapshot()

    assert snap.buying_power == 7500.00


def test_the_plain_buying_power_name_wins_over_the_derivative_one():
    snap = _DictAccount(1, {"buying_power": "9000",
                            "derivative_buying_power": "7500"}).get_account_snapshot()

    assert snap.buying_power == 9000.0


def test_pending_transfer_in_is_read_on_the_dict_path_too():
    """Only the attribute path asserted it (as None); an incoming ACH is money
    the allocation page must see."""
    snap = _DictAccount(1, {"pending_transfer_in": "1000.00"}).get_account_snapshot()

    assert snap.pending_transfer_in == 1000.00


def test_snapshot_multiplier_above_one_marks_the_account_as_margin():
    assert _DictAccount(1, {"multiplier": "4"}).get_account_snapshot().is_margin_account is True


def test_snapshot_multiplier_of_one_is_a_cash_account():
    assert _DictAccount(1, {"multiplier": "1"}).get_account_snapshot().is_margin_account is False


def test_snapshot_of_a_broker_returning_none_is_all_unknown_not_all_zero():
    """None means "the broker told us nothing". A 0.0 here would let a caller
    plan against an account it cannot see."""
    snap = _DictAccount(1, None).get_account_snapshot()
    assert snap == AccountSnapshot()
    assert snap.buying_power is None


def test_snapshot_leaves_a_non_numeric_field_as_none_rather_than_guessing():
    snap = _DictAccount(1, {"buying_power": "n/a", "cash": "100"}).get_account_snapshot()
    assert snap.buying_power is None
    assert snap.cash == 100.0


def test_snapshot_from_an_attribute_broker_uses_the_getattr_branch():
    """The other half of the tolerant probe: an object, not a dict.

    This is the shape that broke TradeActions.py:1493 -- ``.get()`` on a pydantic
    TradeAccount raises AttributeError. Task 31 tests AlpacaAccount's OVERRIDE, so
    without this the base's ``getattr`` branch would have no coverage at all.
    ``raw`` stays {} because only a dict can be copied into it.
    """
    class _Attrs:
        cash = "500.00"
        equity = "10000.00"
        buying_power = "20000.00"
        long_market_value = "9500.00"
        short_market_value = "-250.00"
        multiplier = "2"

    snap = _DictAccount(1, _Attrs()).get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 20000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == -250.0
    assert snap.margin_multiplier == 2.0
    assert snap.is_margin_account is True
    assert snap.raw == {}
    # A field the object simply does not carry stays unknown, never 0.0.
    assert snap.pending_transfer_in is None
    assert snap.non_marginable_buying_power is None


def test_snapshot_survives_a_broker_that_raises():
    class _Boom(_DictAccount):
        def get_account_info(self):
            raise RuntimeError("connection reset")

    assert _Boom(1, None).get_account_snapshot() == AccountSnapshot()


def test_get_cash_transfers_defaults_to_empty_for_a_broker_that_does_not_implement_it():
    """[] by default so no existing broker breaks. Alpaca and TastyTrade override it."""
    assert _DictAccount(1, {}).get_cash_transfers() == []


def test_get_cash_transfers_accepts_a_date_window_without_complaining():
    from datetime import date
    acct = _DictAccount(1, {})
    assert acct.get_cash_transfers(start_date=date(2026, 8, 1),
                                   end_date=date(2026, 8, 31)) == []


def test_get_symbol_margin_info_defaults_to_empty_so_the_caller_falls_back():
    """A symbol the broker cannot describe is OMITTED, never defaulted here -- the
    caller substitutes the conservative bp_factor = account multiplier."""
    assert _DictAccount(1, {}).get_symbol_margin_info(["AAPL", "MSFT"]) == {}


# ---------------------------------------------------------------------------
# get_available_position_quantity -- the cancel-and-replace gate (I9)
#
# It used to exist ONLY on AlpacaAccount, so TradeManager's
# `except Exception: available_qty = None` fired on every other broker and
# replacement_blocked_by_qty(None) returned False -- the guard that stops a
# 40310000 "insufficient qty" rejection from dropping a position's protective
# stop was a permanent no-op everywhere but Alpaca.
#
# The base DERIVES it from get_positions() (the mandatory seam every broker
# implements), the same way get_account_snapshot() derives from
# get_account_info(). It NEVER returns None: "cannot answer" is 0.0, which
# BLOCKS (defer, retry next refresh) rather than submitting blind.
# ---------------------------------------------------------------------------

class _BookAccount(_DictAccount):
    """A broker whose get_positions() returns whatever the test sets."""

    def __init__(self, book):
        super().__init__(1, {})
        self._book = book

    def get_positions(self):
        if isinstance(self._book, Exception):
            raise self._book
        return self._book


class _Pos:
    """The attribute-shaped position (Alpaca / IBKR return a Position model)."""

    def __init__(self, symbol, qty, qty_available=None):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


def test_available_qty_reads_the_brokers_qty_available_for_a_dict_position():
    acct = _BookAccount([{"symbol": "AAPL", "qty": 10.0, "qty_available": 4.0}])
    assert acct.get_available_position_quantity("AAPL") == 4.0


def test_available_qty_reads_the_brokers_qty_available_for_an_object_position():
    acct = _BookAccount([_Pos("AAPL", 10.0, 4.0)])
    assert acct.get_available_position_quantity("AAPL") == 4.0


def test_available_qty_is_absolute_so_a_short_works_too():
    """A short holds -100; the buy-to-cover replacement needs 100 of them."""
    acct = _BookAccount([_Pos("AAPL", -100.0, -100.0)])
    assert acct.get_available_position_quantity("AAPL") == 100.0


def test_a_fully_encumbered_position_reports_zero_and_therefore_blocks():
    """The exact 40310000 shape: the broker still holds the shares against the
    just-canceled order, so none are available yet."""
    acct = _BookAccount([_Pos("AAPL", 6.0, 0.0)])
    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_a_symbol_the_broker_does_not_hold_is_zero_not_unknown():
    """A protective stop for shares the broker does not have is a GUARANTEED
    rejection. 0.0 defers it (WAITING_TRIGGER, retried next refresh) instead of
    submitting it into a hard ERROR."""
    acct = _BookAccount([_Pos("MSFT", 10.0, 10.0)])
    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_a_flat_book_is_zero():
    assert _BookAccount([]).get_available_position_quantity("AAPL") == 0.0


def test_a_positions_FETCH_FAILURE_blocks_rather_than_submitting_blind():
    """get_positions() returns None when the fetch FAILED (tri-state contract).
    An unverified book must never be read as "the qty is free" -- that is the
    direction that gets the replacement rejected and the stop dropped. 0.0 defers
    and the next refresh retries, so this is self-healing."""
    assert _BookAccount(None).get_available_position_quantity("AAPL") == 0.0


def test_a_broker_that_raises_blocks_too():
    assert _BookAccount(RuntimeError("connection reset")
                        ).get_available_position_quantity("AAPL") == 0.0


def test_a_broker_that_publishes_no_qty_available_falls_back_to_the_held_qty():
    """IBKR builds Position without qty_available. The broker CONFIRMS it holds
    the shares; only the transient encumbrance is unknown. Blocking such a broker
    forever would convert a seconds-long race into a PERMANENT, silent absence of
    protection -- strictly worse than the rejection this gate avoids. The gate
    still bites where it matters: a stale leg asking for more than the position's
    total size is deferred instead of submitted into a rejection."""
    acct = _BookAccount([_Pos("AAPL", 100.0, None)])
    assert acct.get_available_position_quantity("AAPL") == 100.0


def test_a_stale_leg_larger_than_the_whole_position_still_blocks_on_such_a_broker():
    acct = _BookAccount([_Pos("AAPL", 100.0, None)])
    available = acct.get_available_position_quantity("AAPL")
    # 181-share leg vs a 100-share position -> the broker would reject it.
    assert available is not None and available < 181.0


def test_a_non_numeric_quantity_blocks_rather_than_guessing():
    acct = _BookAccount([_Pos("AAPL", "n/a", "n/a")])
    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_available_qty_never_returns_none():
    """None means "don't block" to replacement_blocked_by_qty. The base must not
    be able to produce it -- that is the whole point of the hoist."""
    for book in (None, [], RuntimeError("boom"), [_Pos("MSFT", 1.0, 1.0)],
                 [_Pos("AAPL", "n/a", None)]):
        assert _BookAccount(book).get_available_position_quantity("AAPL") is not None


def test_symbol_matching_is_case_and_whitespace_insensitive():
    acct = _BookAccount([_Pos("AAPL", 10.0, 7.0)])
    assert acct.get_available_position_quantity(" aapl ") == 7.0


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

def _ny(year, month, day, hour=0, minute=0, second=0) -> datetime:
    """A frozen instant in exchange-local time. Every test below pins one."""
    return datetime(year, month, day, hour, minute, second, tzinfo=NY_TZ)


def _capture_errors(monkeypatch):
    """Collect ``logger.error`` messages emitted by ReadOnlyAccountInterface.

    NOT caplog. Two independent reasons it cannot be used here:
      * the package logger sets ``propagate = False``
        (packages/common/ba2_common/logger.py:19, and
        ba2_trade_platform/logger.py:24 for the app one), so caplog's ROOT handler
        never sees the record; and
      * tests/test_penny_gainers_fix.py:53 replaces the logger module with a
        MagicMock at import time, so under a full-suite collection even
        re-enabling propagation patches a mock rather than the real logger.
    Patching the module-under-test's own ``logger`` is immune to both.
    """
    import sys
    # sys.modules, not `from ...interfaces import ReadOnlyAccountInterface`: that
    # package __init__ re-exports the CLASS under the same name, so the plain
    # import binds the class and `.logger` would not exist on it.
    module = sys.modules["ba2_common.core.interfaces.ReadOnlyAccountInterface"]
    messages = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


class _CountingAccount(_DictAccount):
    """Counts how many times the seam actually reached the implementation."""

    def __init__(self, id=1, info=None):
        super().__init__(id, info or {})
        self.impl_calls = 0

    def _get_market_hours_impl(self, now):
        self.impl_calls += 1
        return super()._get_market_hours_impl(now)


class _BrokerAccount(_DictAccount):
    """A broker that answers for itself -- the Alpaca / TastyTrade shape."""

    def __init__(self, hours):
        super().__init__(1, {})
        self._hours = hours
        self.seen_now = []

    def _get_market_hours_impl(self, now):
        self.seen_now.append(now)
        return self._hours


class _DegradingAccount(_DictAccount):
    """The canonical adapter fallback: broker failed, so defer to super's calendar.

    This is the shape BOTH broker adapters must use. Note super()._get_market_hours_impl,
    NOT super().get_market_hours() -- the latter calls back into this method.
    """

    def __init__(self):
        super().__init__(1, {})
        self.impl_calls = 0

    def _get_market_hours_impl(self, now):
        self.impl_calls += 1
        return dataclasses.replace(super()._get_market_hours_impl(now),
                                   detail="broker clock endpoint 503")


class _ExplodingAccount(_DictAccount):
    def __init__(self):
        super().__init__(1, {})
        self.impl_calls = 0

    def _get_market_hours_impl(self, now):
        self.impl_calls += 1
        raise RuntimeError("broker clock endpoint 503")


def test_the_default_seam_answers_from_the_nyse_fallback():
    """A broker that implements nothing still gets a real, holiday-aware answer."""
    hours = _CountingAccount().get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert hours.is_open is True
    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert hours.close_at == _ny(2026, 8, 19, 16, 0)


def test_the_default_seam_is_closed_on_an_observed_holiday():
    """2026-07-03 -- Independence Day observed, July 4 being a Saturday."""
    assert _CountingAccount().is_market_open(now=_ny(2026, 7, 3, 12, 0)) is False


def test_the_default_seam_is_closed_at_the_weekend():
    assert _CountingAccount().is_market_open(now=_ny(2026, 8, 22, 12, 0)) is False


def test_the_default_seam_respects_the_half_day_early_close():
    acct = _CountingAccount()

    assert acct.is_market_open(now=_ny(2026, 11, 27, 12, 0)) is True
    acct.clear_market_hours_cache()
    assert acct.is_market_open(now=_ny(2026, 11, 27, 14, 0)) is False


def test_is_market_open_delegates_to_get_market_hours():
    acct = _BrokerAccount(MarketHours(is_open=True, source=MARKET_HOURS_SOURCE_BROKER))

    assert acct.is_market_open(now=_ny(2026, 8, 22, 12, 0)) is True  # broker overrides the calendar


def test_a_broker_override_is_used_verbatim():
    """The override point is _get_market_hours_impl, never get_market_hours itself,
    and the broker's answer wins outright -- no cross-check against the calendar."""
    published = MarketHours(
        is_open=False,
        next_open=_ny(2026, 8, 20, 9, 30),
        next_close=_ny(2026, 8, 20, 16, 0),
        source=MARKET_HOURS_SOURCE_BROKER,
        status="Extended",
    )
    acct = _BrokerAccount(published)

    assert acct.get_market_hours(now=_ny(2026, 8, 19, 18, 0)) is published


def test_the_injected_now_is_handed_to_the_override_not_the_wall_clock():
    """`now=` is THE clock-freeze seam. An override that ignores it and reads its
    own clock makes every test of it non-deterministic."""
    acct = _BrokerAccount(MarketHours(is_open=True, source=MARKET_HOURS_SOURCE_BROKER))
    frozen = _ny(2026, 8, 19, 12, 0)

    acct.get_market_hours(now=frozen)

    assert acct.seen_now == [frozen]


def test_an_adapter_degrades_through_super_impl_without_recursing():
    """THE shape both broker adapters use on their failure path. If it were written
    as super().get_market_hours(), this test would blow the stack instead of
    passing -- get_market_hours() is what calls _get_market_hours_impl()."""
    acct = _DegradingAccount()

    hours = acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert acct.impl_calls == 1
    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert hours.is_open is True
    assert hours.detail == "broker clock endpoint 503"


# --- caching ---------------------------------------------------------------

def test_a_second_call_inside_the_ttl_is_served_from_cache():
    """The wizard page asks repeatedly; the status changes a few times a day."""
    acct = _CountingAccount()
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0, 30))

    assert acct.impl_calls == 1


def test_the_ttl_expires():
    acct = _CountingAccount()
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 1, 1))

    assert acct.impl_calls == 2


def test_the_cache_expires_at_the_bell_even_inside_the_ttl():
    """This is the whole reason a plain elapsed-seconds TTL is wrong here: an entry
    taken at 15:59:30 must NOT still say "open" at 16:00:01."""
    acct = _CountingAccount()
    assert acct.get_market_hours(now=_ny(2026, 8, 19, 15, 59, 30)).is_open is True

    later = acct.get_market_hours(now=_ny(2026, 8, 19, 16, 0, 1))

    assert acct.impl_calls == 2
    assert later.is_open is False


def test_the_cache_expires_at_the_open_even_inside_the_ttl():
    acct = _CountingAccount()
    assert acct.get_market_hours(now=_ny(2026, 8, 19, 9, 29, 30)).is_open is False

    assert acct.get_market_hours(now=_ny(2026, 8, 19, 9, 30, 1)).is_open is True
    assert acct.impl_calls == 2


def test_a_broker_answer_is_cached_like_any_other():
    acct = _BrokerAccount(MarketHours(is_open=True, source=MARKET_HOURS_SOURCE_BROKER))
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0, 30))

    assert len(acct.seen_now) == 1


def test_clear_market_hours_cache_forces_a_refetch():
    acct = _CountingAccount()
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    acct.clear_market_hours_cache()
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0, 5))

    assert acct.impl_calls == 2


def test_the_cache_is_per_instance_and_never_lands_on_the_class():
    """A tuple rebound on the instance -- not a dict mutated on the class."""
    first, second = _CountingAccount(1), _CountingAccount(2)
    first.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    second.get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert first.impl_calls == 1 and second.impl_calls == 1
    assert ReadOnlyAccountInterface._market_hours_cache is None


def test_a_cache_entry_is_not_reused_for_an_earlier_instant():
    """Guards a backwards clock and any caller replaying an older `now`."""
    acct = _CountingAccount()
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    acct.get_market_hours(now=_ny(2026, 8, 19, 11, 59))

    assert acct.impl_calls == 2


# --- failure -------------------------------------------------------------

def test_a_failing_broker_lookup_fails_closed():
    """Never "open" on an error. is_known then tells the UI it is a failure, not a
    genuine closure, and detail carries the reason for the banner."""
    hours = _ExplodingAccount().get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert hours.is_open is False
    assert hours.source == MARKET_HOURS_SOURCE_UNAVAILABLE
    assert hours.is_known is False
    assert "503" in hours.detail


def test_an_unavailable_answer_is_never_cached():
    """A failure is not a fact. Caching it would keep the wizard blocked for a full
    TTL after the broker recovered; the next call must retry."""
    acct = _ExplodingAccount()
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0, 1))

    assert acct.impl_calls == 2
    assert acct._market_hours_cache is None


def test_the_default_impl_degrades_to_unavailable_when_the_calendar_is_missing(monkeypatch):
    """Broker silent AND calendar dead. Shut for the gate, "unknown" for the UI --
    NOT a fallback answer, because nothing answered."""
    import sys

    clear_nyse_calendar_cache()
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", None)
    try:
        hours = _CountingAccount().get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    finally:
        clear_nyse_calendar_cache()

    assert hours.is_open is False
    assert hours.source == MARKET_HOURS_SOURCE_UNAVAILABLE
    assert hours.is_known is False
    assert hours.detail


def test_an_adapter_degrading_into_a_dead_calendar_reports_unavailable(monkeypatch):
    """dataclasses.replace(super()._get_market_hours_impl(now), detail=...) must NOT
    force source=FALLBACK: if the calendar is dead too, the honest answer is
    UNAVAILABLE, and the UI must be allowed to say so."""
    import sys

    clear_nyse_calendar_cache()
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", None)
    try:
        hours = _DegradingAccount().get_market_hours(now=_ny(2026, 8, 19, 12, 0))
    finally:
        clear_nyse_calendar_cache()

    assert hours.source == MARKET_HOURS_SOURCE_UNAVAILABLE
    assert hours.is_known is False
    assert hours.detail == "broker clock endpoint 503"


def test_get_market_hours_never_propagates_an_exception():
    """It is called from a UI render path and from the money path. Whatever an
    adapter does, this returns a MarketHours."""
    hours = _ExplodingAccount().get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert isinstance(hours, MarketHours)


def test_is_market_open_is_false_when_the_lookup_failed():
    assert _ExplodingAccount().is_market_open(now=_ny(2026, 8, 19, 12, 0)) is False


def test_a_failing_lookup_is_logged_as_an_error(monkeypatch):
    errors = _capture_errors(monkeypatch)

    _ExplodingAccount().get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert any("market hours" in e.lower() for e in errors), errors


def test_a_naive_now_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="timezone-aware"):
        _CountingAccount().get_market_hours(now=datetime(2026, 8, 19, 12, 0))


# --- gaps found by mutation ------------------------------------------------

def test_a_naive_now_is_refused_BEFORE_the_implementation_is_reached():
    """Found by mutation: deleting the seam's own naive check left
    test_a_naive_now_is_refused_rather_than_guessed green, because the ValueError
    then came from a DOUBLE FAULT instead -- the calendar raised, and building the
    UNAVAILABLE MarketHours(as_of=<naive now>) raised again from inside the except
    handler. Same exception type, same "timezone-aware" text, but
    get_market_hours had propagated out of its own error path, breaking its
    documented "NEVER raises and never propagates".

    Pinning impl_calls == 0 is what distinguishes "the seam refused it up front"
    from "it ran the whole lookup and blew up on the way out".
    """
    acct = _CountingAccount()

    with pytest.raises(ValueError, match="timezone-aware"):
        acct.get_market_hours(now=datetime(2026, 8, 19, 12, 0))

    assert acct.impl_calls == 0


def test_the_cache_is_already_stale_AT_the_bell_not_just_after_it():
    """Found by mutation: `now <= transition` survived every other test, because
    the nearest one asks at 16:00:01. The stale answer at exactly 16:00:00 is the
    dangerous one -- it is the first instant an opening market order is refused,
    and a cache taken 30 seconds earlier would still be saying "open".
    """
    acct = _CountingAccount()
    assert acct.get_market_hours(now=_ny(2026, 8, 19, 15, 59, 30)).is_open is True

    at_the_bell = acct.get_market_hours(now=_ny(2026, 8, 19, 16, 0, 0))

    assert acct.impl_calls == 2
    assert at_the_bell.is_open is False


# --- the override contract, pinned structurally ----------------------------

def test_get_market_hours_and_is_market_open_are_concrete_on_the_interface():
    """The template/impl split, asserted on the CLASS rather than in prose.

    ``get_market_hours`` and ``is_market_open`` are defined HERE and are
    effectively FINAL; ``_get_market_hours_impl`` is the sole override point. An
    adapter that overrides a template method loses the boundary-expiry cache --
    the same mistake that disabled IBKRAccount's submit_order.
    """
    for name in ("get_market_hours", "is_market_open", "clear_market_hours_cache",
                 "_get_market_hours_impl"):
        assert name in vars(ReadOnlyAccountInterface), name
        assert not getattr(getattr(ReadOnlyAccountInterface, name),
                           "__isabstractmethod__", False), name


def test_calling_super_get_market_hours_from_an_override_re_enters_the_template():
    """WHY the contract says ``super()._get_market_hours_impl(now)``.

    Both broker adapters were originally written with ``super().get_market_hours()``
    on their failure path and it was caught in review. This pins what that costs,
    because the damage is SILENT: ``get_market_hours`` catches Exception, and
    RecursionError is one, so the stack blows and is then swallowed into a
    permanent "unavailable" -- the wizard refuses to submit forever, with hundreds
    of re-entries burned on every UI render, and nothing crashes to say so.
    """
    class _RecursingAccount(_DictAccount):
        def __init__(self):
            super().__init__(1, {})
            self.impl_calls = 0

        def _get_market_hours_impl(self, now):
            self.impl_calls += 1
            return self.get_market_hours(now=now)   # THE BUG: re-enters the template

    acct = _RecursingAccount()
    hours = acct.get_market_hours(now=_ny(2026, 8, 19, 12, 0))

    assert acct.impl_calls > 50           # re-entered, not called once
    assert hours.source == MARKET_HOURS_SOURCE_UNAVAILABLE
    assert hours.is_known is False        # ... and swallowed, so nothing crashes
