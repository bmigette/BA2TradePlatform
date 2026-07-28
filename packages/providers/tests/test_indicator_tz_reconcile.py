"""Indicator range-filter must reconcile tz-awareness in BOTH directions (2026-07-28).

``_calculate_indicator_for_range`` filters ``df['Date']`` against the requested bounds. pandas
raises TypeError on ANY naive-vs-aware comparison, but only the "df is aware, bounds are naive"
direction was handled. The MIRROR case — naive frame, tz-AWARE bounds — is exactly what the
backtest passes (``as_of`` is UTC-aware), so it raised:

    TypeError: Cannot compare tz-naive and tz-aware datetime-like objects

``@log_provider_call`` swallowed that into "Failed to get atr for <SYMBOL>", and GA pool workers
run at ``logging.disable(ERROR)`` (``_worker_init``), so it never surfaced in ANY grid log.
Observed live: a ONE-MONTH Senate backtest lost ATR for every symbol it traded (WFC, MSFT, DIS,
ADBE). Consequence: ``use_atr_stop`` / ``atr_multiplier`` / ``atr_period`` were inert GA genes
and the stop silently fell back to ``min_stop_loss_pct``.

The frame's awareness comes from whatever the OHLCV provider returns, so these inject a fake
provider to drive both cases directly.
"""
import pandas as pd
import pytest

from ba2_providers.indicators.PandasIndicatorCalc import PandasIndicatorCalc


class _FakeOHLCV:
    """Returns a fixed daily frame whose Date column is tz-aware or naive on demand."""

    def __init__(self, tz):
        self.tz = tz

    def get_ohlcv_data(self, symbol, start_date, end_date, interval="1d"):
        idx = pd.date_range("2022-01-03 09:30", periods=420, freq="D", tz=self.tz)
        n = len(idx)
        return pd.DataFrame({
            "Date": idx,
            "Open": [100.0 + i * 0.1 for i in range(n)],
            "High": [101.0 + i * 0.1 for i in range(n)],
            "Low": [99.0 + i * 0.1 for i in range(n)],
            "Close": [100.5 + i * 0.1 for i in range(n)],
            "Volume": [1_000_000] * n,
        })


def _run(df_tz, start, end, indicator="atr"):
    calc = PandasIndicatorCalc(_FakeOHLCV(df_tz))
    return calc._calculate_indicator_for_range("TEST", indicator, start, end, "1d")


S_NAIVE = pd.Timestamp("2023-01-05 09:30")
E_NAIVE = pd.Timestamp("2023-01-20 09:30")
S_UTC = pd.Timestamp("2023-01-05 09:30", tz="UTC")
E_UTC = pd.Timestamp("2023-01-20 09:30", tz="UTC")


# --------------------------------------------------------------------------- #
# the regression: naive frame + AWARE bounds
# --------------------------------------------------------------------------- #
def test_naive_frame_with_aware_bounds_does_not_raise():
    """THE BUG: naive cache frame + UTC-aware bounds from the backtest's as_of."""
    out = _run(None, S_UTC, E_UTC)
    assert not out.empty, "ATR came back empty -- the stop silently falls back to a pct floor"


def test_naive_frame_aware_bounds_selects_the_same_rows_as_naive_bounds():
    """Reconciling must not shift the window: the same instants select the same rows."""
    aware = _run(None, S_UTC, E_UTC)
    naive = _run(None, S_NAIVE, E_NAIVE)
    assert list(aware["Date"]) == list(naive["Date"])
    assert not aware.empty


def test_non_utc_aware_bounds_are_converted_not_truncated():
    """A non-UTC bound must be converted to UTC BEFORE tz is dropped. Simply stripping tzinfo
    would shift the window by the offset and silently select the wrong rows."""
    ny = _run(None, pd.Timestamp("2023-01-05 04:30", tz="America/New_York"),
              pd.Timestamp("2023-01-20 04:30", tz="America/New_York"))
    assert list(ny["Date"]) == list(_run(None, S_UTC, E_UTC)["Date"])
    assert not ny.empty


# --------------------------------------------------------------------------- #
# the direction that already worked must keep working
# --------------------------------------------------------------------------- #
def test_aware_frame_with_naive_bounds_still_works():
    assert not _run("UTC", S_NAIVE, E_NAIVE).empty


def test_aware_frame_with_aware_bounds_still_works():
    assert not _run("UTC", S_UTC, E_UTC).empty


def test_aware_frame_with_differently_zoned_bounds():
    out = _run("UTC", pd.Timestamp("2023-01-05 04:30", tz="America/New_York"),
               pd.Timestamp("2023-01-20 04:30", tz="America/New_York"))
    assert not out.empty


def test_naive_frame_with_naive_bounds_unchanged():
    assert not _run(None, S_NAIVE, E_NAIVE).empty


# --------------------------------------------------------------------------- #
# all four awareness combinations agree
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("df_tz", [None, "UTC"])
@pytest.mark.parametrize("bound_tz", [None, "UTC"])
def test_every_awareness_combination_returns_the_same_window(df_tz, bound_tz):
    out = _run(df_tz, pd.Timestamp("2023-01-05 09:30", tz=bound_tz),
               pd.Timestamp("2023-01-20 09:30", tz=bound_tz))
    assert not out.empty, f"empty for df_tz={df_tz} bound_tz={bound_tz}"
    assert len(out) == 16, f"window size differs for df_tz={df_tz} bound_tz={bound_tz}"
