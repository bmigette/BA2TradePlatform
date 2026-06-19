import pytest
from datetime import date
from app.services.backtest.daily_backtest_handler import validate_options_window


def test_pre_2024_option_run_rejected():
    with pytest.raises(ValueError):
        validate_options_window("2023-06-01", uses_options=True)


def test_2024_option_run_ok():
    validate_options_window("2024-06-01", uses_options=True)  # no raise


def test_equity_run_any_date_ok():
    validate_options_window("2020-01-01", uses_options=False)  # no raise


def test_date_object_accepted():
    validate_options_window(date(2024, 2, 1), uses_options=True)  # boundary ok


# --- injection wiring -------------------------------------------------------
# A present ``options_cache_db`` => the account gets a HistoricalOptionsProvider
# (account._options set); absent => account._options is None (equity-only). This mirrors
# the run_daily_backtest branch (build provider iff options_cache_db) without the heavy
# engine setup; the full submit-through-engine path is covered by the Task 11 e2e.
def _seed_options_cache(db):
    from app.services.backtest.options_cache import OptionsHistoryCache

    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": "AAPL240315C00180000", "option_type": "call", "strike": 180.0,
         "expiry": "2024-03-15", "bid": 2.0, "ask": 2.1, "last": 2.05, "iv": 0.25,
         "delta": 0.5, "gamma": 0.01, "theta": -0.03, "vega": 0.1,
         "open_interest": 1000, "volume": 50}])
    return c


def _make_account(options_provider):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    settings = {
        "starting_cash": 100000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_open",
    }
    return BacktestAccount(1, ps, settings, options_provider=options_provider)


def test_account_gets_options_provider_when_cache_present(tmp_path):
    from app.services.backtest.options_provider import HistoricalOptionsProvider

    db = str(tmp_path / "opt.db")
    _seed_options_cache(db)
    options_cache_db = db
    uses_options = bool(options_cache_db)
    assert uses_options is True
    provider = HistoricalOptionsProvider(options_cache_db) if uses_options else None
    account = _make_account(provider)
    assert account._options is not None


def test_account_options_none_when_cache_absent():
    options_cache_db = None
    uses_options = bool(options_cache_db)
    assert uses_options is False
    provider = None
    account = _make_account(provider)
    assert account._options is None
