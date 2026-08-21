"""AlpacaAccount market hours, against a mocked TradingClient.

No live call anywhere: `client` is a MagicMock returning real alpaca-py `Clock`
model objects.

Every test FREEZES time explicitly -- either by passing `now` straight into
`_get_market_hours_impl(now)`, or through the interface's `get_market_hours(now=...)`
keyword, which contract 1.4 makes THE clock-freeze seam. None of these assertions may
depend on when the suite happens to run: a market-hours test that passes at 11:00 and
fails at 17:00 is worse than no test.

Timezone care: `Clock` timestamps come back tz-aware from the API, but a naive Eastern
datetime must never be read as UTC (that is four or five hours early depending on the
season), so `_to_market_utc` is pinned in both EDT and EST.

Structural care: AlpacaAccount overrides `_get_market_hours_impl` ONLY. Overriding
`get_market_hours` and then calling `super().get_market_hours()` on the failure path is
infinite recursion, because the interface's template method calls the impl. Two tests
below exist purely to keep that from coming back.
"""
import sys
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytz

from alpaca.trading.models import Clock

from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_trade_platform.core.account_types import (
    MARKET_HOURS_SOURCE_BROKER, MARKET_HOURS_SOURCE_FALLBACK,
    MARKET_HOURS_SOURCE_UNAVAILABLE, MarketHours,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

AA = sys.modules[AlpacaAccount.__module__]
_ET = pytz.timezone("America/New_York")


def _et(year, month, day, hour, minute):
    """A tz-aware Eastern datetime, the way Alpaca's clock publishes them."""
    return _ET.localize(datetime(year, month, day, hour, minute))


def _utc(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _bare_account():
    """An AlpacaAccount without __init__ (no credentials, no broker connection).

    Deliberately does NOT set a market-hours cache attribute: the cache lives on
    ReadOnlyAccountInterface as a CLASS attribute with a default (contract 1.4), so a
    bare instance already has it and the first real answer shadows it per instance.
    """
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._authentication_error = None
    acct._balance_cache_lock = threading.Lock()
    return acct


def _open_clock():
    """11:00 ET on Thursday 2026-08-20, mid-session."""
    return Clock(timestamp=_et(2026, 8, 20, 11, 0), is_open=True,
                 next_open=_et(2026, 8, 21, 9, 30), next_close=_et(2026, 8, 20, 16, 0))


def _closed_clock():
    """18:00 ET on Thursday 2026-08-20, after the bell."""
    return Clock(timestamp=_et(2026, 8, 20, 18, 0), is_open=False,
                 next_open=_et(2026, 8, 21, 9, 30), next_close=_et(2026, 8, 21, 16, 0))


def _capture_warnings(monkeypatch):
    """Collect logger.warning text without caplog.

    ba2_trade_platform/logger.py:24 sets propagate = False so caplog never sees these
    records, and tests/test_penny_gainers_fix.py:53 replaces the logger module with a
    MagicMock under full-suite collection. Assert on the call itself -- the idiom from
    tests/test_alpaca_fractional_submission.py:220-222.
    """
    messages = []
    monkeypatch.setattr(AA.logger, "warning", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


# ---------------------------------------------------------------------------
# Naive Eastern -> UTC
# ---------------------------------------------------------------------------

def test_a_naive_broker_datetime_is_read_as_eastern_not_as_utc():
    """Alpaca publishes some datetimes naive (Calendar.open, models.py:374-388).
    Reading the opening bell as UTC would put it four or five hours early."""
    assert AA._to_market_utc(datetime(2026, 8, 20, 9, 30)) == _utc(2026, 8, 20, 13, 30)  # EDT
    assert AA._to_market_utc(datetime(2026, 1, 5, 9, 30)) == _utc(2026, 1, 5, 14, 30)    # EST


def test_an_already_aware_datetime_is_converted_not_relabelled():
    assert AA._to_market_utc(_et(2026, 8, 20, 16, 0)) == _utc(2026, 8, 20, 20, 0)


# ---------------------------------------------------------------------------
# Shape: exactly one override point
# ---------------------------------------------------------------------------

def test_alpaca_overrides_the_impl_hook_and_nothing_else():
    """The whole anti-recursion invariant, as a structural assertion.

    `get_market_hours` is the interface's caching template and it CALLS
    `_get_market_hours_impl`. An adapter that overrides the template and then delegates
    to `super().get_market_hours()` never terminates. Caching, boundary expiry and
    `clear_market_hours_cache()` all belong to ReadOnlyAccountInterface (contract 3.3),
    so none of those names may reappear here either.
    """
    assert "_get_market_hours_impl" in AlpacaAccount.__dict__
    for banned in ("get_market_hours", "is_market_open", "_market_hours_cache",
                   "_MARKET_HOURS_CACHE_TTL", "_session_open_utc",
                   "clear_market_hours_cache"):
        assert banned not in AlpacaAccount.__dict__, banned


def test_the_public_seam_routes_to_the_alpaca_override():
    acct = _bare_account()
    acct.client.get_clock.return_value = _open_clock()

    hours = acct.get_market_hours(now=_utc(2026, 8, 20, 15, 0))

    assert hours.source == MARKET_HOURS_SOURCE_BROKER
    assert acct.is_market_open(now=_utc(2026, 8, 20, 15, 0)) is True


# ---------------------------------------------------------------------------
# Open / closed, straight off the broker clock
# ---------------------------------------------------------------------------

def test_market_open_reports_the_current_close_and_leaves_the_session_open_none():
    """Clock does not publish the CURRENT session's open -- while the market is open
    `next_open` is tomorrow's opening bell. Contract 1.5: leave `open_at` None rather
    than substituting a confidently wrong answer, and never spend a /calendar call."""
    acct = _bare_account()
    acct.client.get_clock.return_value = _open_clock()

    hours = acct._get_market_hours_impl(_utc(2026, 8, 20, 15, 0))

    assert hours.is_open is True
    assert hours.source == MARKET_HOURS_SOURCE_BROKER
    assert hours.open_at is None
    assert hours.close_at == _utc(2026, 8, 20, 20, 0)
    assert hours.next_open == _utc(2026, 8, 21, 13, 30)
    assert hours.next_close == _utc(2026, 8, 20, 20, 0)
    # as_of is the BROKER's own timestamp, normalised -- 11:00 ET is 15:00Z.
    assert hours.as_of == _utc(2026, 8, 20, 15, 0)
    # Alpaca's Clock publishes no status word; `status` is display-only and stays None.
    assert hours.status is None
    assert hours.is_known is True
    assert hours.next_transition == _utc(2026, 8, 20, 20, 0)


def test_as_of_is_the_brokers_own_clock_and_not_the_one_we_asked_with():
    """`as_of` echoes the BROKER's timestamp, which is how broker clock skew becomes
    visible at all.

    The test above pins `as_of` with a `now` that happens to EQUAL the broker's
    timestamp, so it cannot tell the two apart -- mutating the adapter to `as_of=now`
    survives it untouched. Here the two differ by 42 minutes, so only the broker's
    answer passes.
    """
    acct = _bare_account()
    acct.client.get_clock.return_value = _open_clock()     # broker says 11:00 ET = 15:00Z

    hours = acct._get_market_hours_impl(_utc(2026, 8, 20, 15, 42, 17))

    assert hours.as_of == _utc(2026, 8, 20, 15, 0)


def test_market_closed_reports_the_next_session_on_both_pairs():
    """Contract 1.1: when is_open is False, open_at == next_open and
    close_at == next_close -- the bounds describe the NEXT session."""
    acct = _bare_account()
    acct.client.get_clock.return_value = _closed_clock()

    hours = acct._get_market_hours_impl(_utc(2026, 8, 20, 22, 0))

    assert hours.is_open is False
    assert hours.source == MARKET_HOURS_SOURCE_BROKER
    assert hours.open_at == _utc(2026, 8, 21, 13, 30)
    assert hours.close_at == _utc(2026, 8, 21, 20, 0)
    assert hours.open_at == hours.next_open
    assert hours.close_at == hours.next_close


def test_an_early_close_day_comes_straight_off_the_clock():
    """2026-11-27 is the day after Thanksgiving: the session ends early. The broker's
    own clock already knows; no calendar lookup and no hardcoded half-day close."""
    acct = _bare_account()
    acct.client.get_clock.return_value = Clock(
        timestamp=_et(2026, 11, 27, 10, 0), is_open=True,
        next_open=_et(2026, 11, 30, 9, 30), next_close=_et(2026, 11, 27, 13, 0))

    hours = acct._get_market_hours_impl(_utc(2026, 11, 27, 15, 0))

    assert hours.source == MARKET_HOURS_SOURCE_BROKER
    assert hours.close_at == _utc(2026, 11, 27, 18, 0)     # 13:00 EST
    assert hours.next_open == _utc(2026, 11, 30, 14, 30)   # 09:30 EST


def test_the_calendar_endpoint_is_never_called():
    """/calendar and the _session_open_utc helper that used it are gone (contract 3.3);
    the shared offline calendar in ba2_common.core.market_calendar owns session times."""
    acct = _bare_account()
    acct.client.get_clock.return_value = _open_clock()

    acct._get_market_hours_impl(_utc(2026, 8, 20, 15, 0))

    acct.client.get_calendar.assert_not_called()


# ---------------------------------------------------------------------------
# Failure -> super's offline calendar, LOUDLY, and without recursing
# ---------------------------------------------------------------------------

def test_a_clock_failure_degrades_to_the_shared_offline_calendar_and_says_why(monkeypatch):
    """A broker hiccup must not raise out of a page render, and it must not silently
    masquerade as the broker's own answer -- `source` says which one you got and
    `detail` says why you got it."""
    warnings = _capture_warnings(monkeypatch)
    acct = _bare_account()
    acct.client.get_clock.side_effect = RuntimeError("connection reset")

    hours = acct._get_market_hours_impl(_utc(2026, 8, 20, 15, 0))

    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert hours.is_open is True      # 11:00 ET on a Thursday, per the offline calendar
    assert "connection reset" in (hours.detail or "")
    assert any("connection reset" in w and "fall back" in w for w in warnings), warnings


def test_an_empty_clock_response_degrades_too(monkeypatch):
    warnings = _capture_warnings(monkeypatch)
    acct = _bare_account()
    acct.client.get_clock.return_value = None

    hours = acct._get_market_hours_impl(_utc(2026, 8, 20, 15, 0))

    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert "returned nothing" in (hours.detail or "")
    assert any("returned nothing" in w for w in warnings), warnings


def test_an_unauthenticated_account_degrades_without_touching_the_client(monkeypatch):
    _capture_warnings(monkeypatch)
    acct = _bare_account()
    acct.client = None
    acct._authentication_error = "missing api_key"

    hours = acct._get_market_hours_impl(_utc(2026, 8, 20, 15, 0))

    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert "not authenticated" in (hours.detail or "")


def test_the_fallback_keeps_supers_source_so_a_dead_calendar_reads_unavailable(monkeypatch):
    """Contract 1.5: the failure path is dataclasses.replace(super()..., detail=...) and
    must NOT force source=fallback. Broker dead AND calendar dead is UNAVAILABLE --
    is_open False so the submit gate fails closed, is_known False so the UI says
    "unknown" rather than "closed". That is the get_positions() None-vs-[] lesson."""
    _capture_warnings(monkeypatch)
    frozen = _utc(2026, 8, 20, 15, 0)
    monkeypatch.setattr(
        ReadOnlyAccountInterface, "_get_market_hours_impl",
        lambda self, now: MarketHours(is_open=False,
                                      source=MARKET_HOURS_SOURCE_UNAVAILABLE,
                                      as_of=now, detail="pandas_market_calendars missing"))
    acct = _bare_account()
    acct.client.get_clock.side_effect = RuntimeError("connection reset")

    hours = acct._get_market_hours_impl(frozen)

    assert hours.source == MARKET_HOURS_SOURCE_UNAVAILABLE
    assert hours.is_known is False
    assert hours.is_open is False
    assert "connection reset" in (hours.detail or "")


def test_a_broker_failure_does_not_recurse_through_the_public_seam(monkeypatch):
    """The regression this whole rename exists for. With the override on
    `get_market_hours` and `super().get_market_hours()` on the failure path, this call
    raised RecursionError instead of returning a fallback."""
    _capture_warnings(monkeypatch)
    acct = _bare_account()
    acct.client.get_clock.side_effect = RuntimeError("connection reset")

    hours = acct.get_market_hours(now=_utc(2026, 8, 20, 15, 0))

    assert hours.source == MARKET_HOURS_SOURCE_FALLBACK
    assert acct.is_market_open(now=_utc(2026, 8, 20, 15, 0)) is True
