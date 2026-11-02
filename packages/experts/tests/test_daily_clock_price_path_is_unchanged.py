"""A backtest on the DAILY clock prices its decisions exactly as it did before the decision-price seam.

The seam (``MarketExpertInterface._decision_price``) routes a backtest price through the account so
that the INTRADAY clock gets "the close of the latest ended bar". The daily clock (option backtests,
``execution_interval=1d``) must not move: it keeps the bundle's last daily close <= as_of
(forward-filled when the symbol has no bar on the decision day), and DeterministicScorer keeps its
own frame's last close as the last resort. These tests pin each path.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.backtest_context import LiveProviderBundle
from ba2_common.core.knowability import NoDecisionPrice, intraday_decisions
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_experts.DeterministicScorer import DeterministicScorer

NOW = datetime(2024, 1, 4, tzinfo=timezone.utc)


class _Ohlcv:
    """Daily provider: a halted name, last bar two sessions BEFORE the decision day (close 97.5)."""

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d", **kw):
        if symbol == "NOBARS":
            return pd.DataFrame({"Close": []})
        return pd.DataFrame({"Date": [pd.Timestamp("2024-01-02")], "Close": [97.5]})


class _ExplodingAccount:
    def get_instrument_current_price(self, symbol):
        raise AssertionError("a daily-clock backtest must not read the account for its price")


def _expert():
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    return e


def _bundle():
    return LiveProviderBundle(lambda cat, name, **kw: {"ohlcv": _Ohlcv()}[cat])


def test_daily_clock_no_bar_on_the_decision_day_gives_the_forward_filled_last_close_as_before():
    e, b = _expert(), _bundle()
    e._decision_account_cache = (b, _ExplodingAccount())
    assert e._decision_price(b, "HALTED", NOW) == 97.5 == b.price_at_date("HALTED", NOW)


def test_daily_clock_a_symbol_the_provider_has_nothing_for_is_none_as_before():
    e, b = _expert(), _bundle()
    e._decision_account_cache = (b, _ExplodingAccount())
    assert e._decision_price(b, "NOBARS", NOW) is None


def test_daily_clock_never_resolves_the_account():
    e, b = _expert(), _bundle()
    e._decision_account = lambda providers: (_ for _ in ()).throw(AssertionError("no account lookup"))
    assert e._decision_price(b, "HALTED", NOW) == 97.5


def test_intraday_clock_reads_the_account_and_an_undecidable_symbol_is_none():
    e, b = _expert(), _bundle()

    class Acct:
        def get_instrument_current_price(self, s):
            if s == "NOBARS":
                raise NoDecisionPrice("no bar ended")
            return 12.5

    e._decision_account_cache = (b, Acct())
    with intraday_decisions(True):
        assert e._decision_price(b, "HALTED", NOW) == 12.5
        assert e._decision_price(b, "NOBARS", NOW) is None


def test_a_non_universe_symbol_on_the_daily_clock_keeps_the_provider_behaviour():
    """The provider (not the account) decides: whatever the provider raises (a hermetic cache miss)
    still propagates, which the Senate gather already catches as the cache-miss types."""
    class Missing(Exception):
        pass

    class Raising:
        def get_ohlcv_data(self, *a, **k):
            raise Missing("cache miss")

    b = LiveProviderBundle(lambda cat, name, **kw: Raising())
    e = _expert()
    with pytest.raises(Missing):
        e._decision_price(b, "ZZZ", NOW)


# ----------------------------------------------------------------------------- DeterministicScorer
def _scorer_gather(price, frame_close=101.0, *, intraday):
    from tests.test_order_levels_current_price import _scorer_with
    e, providers = _scorer_with(bundle_price=price, frame_close=frame_close)
    if intraday:
        e._decision_price = lambda p, s, a: price
    return e, providers


def test_deterministic_scorer_prices_from_the_seam_on_both_clocks_never_from_its_frame(monkeypatch):
    """DeterministicScorer's price is the seam's (daily clock: the bundle's last close <= as_of), and a
    seam answer of None is never papered over with the frame's last close.
    KNOWN, REPORTED DIFFERENCE from before the seam: a name whose last daily print is more than the
    bundle read's 7-day window before the decision day used to be priced from the stale frame."""
    from ba2_experts.DeterministicScorer import data
    e, providers = _scorer_gather(107.0, 101.0, intraday=False)
    assert e._gather(providers, as_of=NOW)["current_price"] == 107.0
    e2, providers2 = _scorer_gather(None, 101.0, intraday=False)
    assert e2._gather(providers2, as_of=NOW)["current_price"] is None
