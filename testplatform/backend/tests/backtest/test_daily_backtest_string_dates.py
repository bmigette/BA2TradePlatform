"""run_daily_backtest must accept ISO STRING dates, not only datetimes.

The genetic optimizer's _build_daily_trial_config forwards the dates from the JSON
optimization_config (which are strings) straight into run_daily_backtest. Before the fix
that crashed in AsOfPriceSource.preload (`start - timedelta`), which the GA swallowed as a
per-trial WARNING -> every daily-expert optimization silently completed with 0 real trials.
"""
import pytest

from app.services.backtest import daily_backtest_handler as H
from tests.backtest.fixtures.e2e_support import (
    ensure_host_schema,
    hermetic_providers,
    new_backtest_row,
)
from tests.backtest.fixtures.hermetic_providers import (
    EARNINGS_DRIFT_SETTINGS,
    TRADE_END,
    TRADE_START,
    UNIVERSE,
)


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    ensure_host_schema()
    yield


def test_run_daily_backtest_accepts_iso_string_dates():
    bt_id = new_backtest_row("str-dates")
    config = {
        "backtest_id": bt_id,
        "name": "str-dates",
        "start_date": TRADE_START.isoformat(),  # STRING (as the optimizer passes from JSON)
        "end_date": TRADE_END.isoformat(),      # STRING
        "enabled_instruments": list(UNIVERSE),
        "experts": [{"class": "FMPEarningsDrift", "settings": EARNINGS_DRIFT_SETTINGS}],
        "initial_capital": 100000.0,
        "account_settings": {
            "starting_cash": 100000.0, "commission_per_trade": 1.0,
            "slippage_bps": 0.0, "fill_model": "next_bar_open",
        },
        "warmup_days": 30, "seed": 42, "subtype": "daily_expert",
    }
    with hermetic_providers():
        res = H.run_daily_backtest(config)
    assert res and "equity_curve" in res and res["equity_curve"], "string-date run produced no results"
