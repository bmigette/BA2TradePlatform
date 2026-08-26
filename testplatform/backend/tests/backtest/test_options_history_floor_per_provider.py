"""The options-history floor is a PER-VENDOR fact, not a global one.

Alpaca's 2024-01-18 was measured against Alpaca's own API (four long-dated contracts, all
first-barring on exactly that date). It says nothing about any other vendor, and enforcing
it globally refuses windows a different vendor can serve.

These tests pin three things:

  1. each vendor answers with ITS OWN floor, DELEGATED to the provider class rather than
     re-typed here -- an env override of the TastyTrade floor must move the registry's
     answer, which a copied literal could not do;
  2. an unknown vendor has an UNKNOWN floor, which is not the same as no floor;
  3. the backtest guard consults the floor of the provider serving ITS store, and the
     message says which vendor it is talking about.
"""
import pytest
from datetime import date

from app.services.backtest.daily_backtest_handler import (
    backtest_options_provider, validate_options_window,
)


# --------------------------------------------------------------------------- #
# 1. Per-vendor floors, delegated (not copied)
# --------------------------------------------------------------------------- #
def test_alpaca_floor_is_the_measured_2024_01_18():
    from ba2_providers.options import options_history_floor

    assert options_history_floor("alpaca") == date(2024, 1, 18)


def test_alpaca_floor_is_the_providers_own_answer():
    """Not a second literal: the registry must ASK AlpacaOptionsProvider."""
    from ba2_providers.options import AlpacaOptionsProvider, options_history_floor

    assert options_history_floor("alpaca") == AlpacaOptionsProvider().history_floor()


def test_tastytrade_has_its_own_floor_and_it_is_not_alpacas():
    from ba2_providers.options import TastyTradeOptionsProvider, options_history_floor

    tt = options_history_floor("tastytrade")
    assert tt == TastyTradeOptionsProvider().history_floor()
    assert tt != options_history_floor("alpaca")
    # The whole point: TastyTrade reaches into 2023, which Alpaca cannot.
    assert tt < date(2023, 1, 1)


def test_the_tastytrade_floor_is_read_LIVE_not_frozen_at_import(monkeypatch):
    """DISCRIMINATOR. A registry holding its own ``date(2022, 10, 1)`` literal would pass
    the two tests above and fail this one: the env override moves the provider's floor, so
    it must move the registry's answer too."""
    from ba2_providers.options import options_history_floor

    monkeypatch.setenv("TASTYTRADE_OPTIONS_HISTORY_FLOOR", "2021-06-15")
    assert options_history_floor("tastytrade") == date(2021, 6, 15)


def test_an_unknown_vendor_has_an_unknown_floor_not_no_floor():
    from ba2_providers.options import options_history_floor

    with pytest.raises(ValueError, match="frobnicate"):
        options_history_floor("frobnicate")


# --------------------------------------------------------------------------- #
# 2. The guard consults the SERVING provider's floor
# --------------------------------------------------------------------------- #
def test_a_tastytrade_served_run_may_start_in_2023():
    validate_options_window("2023-01-01", uses_options=True, provider="tastytrade")


def test_an_alpaca_served_run_may_not():
    with pytest.raises(ValueError):
        validate_options_window("2023-01-01", uses_options=True, provider="alpaca")


def test_the_refusal_names_the_vendor_it_is_speaking_for():
    """A message that just says "options history starts 2024-01-18" invites the reader to
    lower one number; naming the vendor points at the real question (which store am I
    reading?)."""
    with pytest.raises(ValueError) as e:
        validate_options_window("2023-01-01", uses_options=True, provider="alpaca")
    assert "alpaca" in str(e.value)


def test_an_unknown_vendor_is_refused_rather_than_waved_through():
    with pytest.raises(ValueError):
        validate_options_window("2023-01-01", uses_options=True, provider="frobnicate")


# --------------------------------------------------------------------------- #
# 3. Which provider actually serves the backtest today
# --------------------------------------------------------------------------- #
def test_the_backtest_store_is_served_by_alpaca_today():
    """ESTABLISHED, not assumed. ``HistoricalOptionsProvider`` reads exactly one store --
    the ``OptionsHistoryCache`` sqlite -- and the only writer of that schema is
    ``fetch_options.build_cache``, which is hard-wired to Alpaca. The TastyTrade parquet
    tree is written by tools/warm_options_history.py and read by NOTHING on the backtest
    path (only the read-only chain viewer), so no run can span both vendors.
    """
    assert backtest_options_provider() == "alpaca"


def test_the_default_guard_still_refuses_2023():
    """Consequence of the above, and the regression that matters: making the floor
    per-vendor must NOT quietly admit a window the store cannot serve."""
    with pytest.raises(ValueError):
        validate_options_window("2023-01-01", uses_options=True)
