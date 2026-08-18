"""Assembles (price_data, indicators_data) for InstrumentGraph without a
persisted MarketAnalysis -- the "live recalculation" path TradingAgentsUI.py
sketches, factored out so SYMBOL360 can call it too.

Note: TradingAgentsUI.py's live-recalculation branch calls
``PandasIndicatorCalc().get_indicator_data(..., start_date=<str>, ...)``.
Neither of those exists on the real class: ``PandasIndicatorCalc.__init__``
requires an ``ohlcv_provider`` (no zero-arg construction), the method is
``get_indicator`` (not ``get_indicator_data`` -- that name belongs to the
TradingAgents *Toolkit*, a different class), and it takes real ``datetime``
objects, not date strings (``validate_date_range`` dereferences
``start_date.tzinfo`` directly). That branch is dead/unexercised code. This
helper calls the real, working ``PandasIndicatorCalc.get_indicator`` API
instead of copying the broken reference call shape.
"""
from datetime import datetime, timedelta
from typing import Dict, Tuple

import pandas as pd

from ...logger import logger

# Small fixed set -- SYMBOL360 is a snapshot view, not a configurable chart.
# These are PandasIndicatorCalc.SUPPORTED_INDICATOR_KEYS entries (stockstats
# indicator names with fixed periods baked in -- there is no "sma_200"/
# "rsi_14" style parametrized key; "sma" is spelled "close_200_sma" and RSI
# has no period suffix at all).
DEFAULT_INDICATORS = ["close_200_sma", "rsi"]


def build_chart_data(symbol: str, ohlcv_provider, indicator_calc,
                     lookback_days: int = 400, interval: str = "1d"
                     ) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """Fetch OHLCV + a small fixed indicator overlay set for one symbol,
    shaped exactly as InstrumentGraph expects (price_data: DatetimeIndex'd
    OHLCV DataFrame; indicators_data: {display_name: single-column
    DatetimeIndex'd DataFrame}), both tz-aware (UTC).

    Price data failures propagate (it's the load-bearing series -- no
    fallbacks for live data, per project convention). A single indicator
    overlay failing (bad data, provider hiccup, even an unexpected exception
    type) is logged and skipped rather than blocking the whole chart -- one
    broken overlay should not prevent the price chart from rendering.
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback_days)
    price_data = ohlcv_provider.get_ohlcv_data(
        symbol=symbol, start_date=start_date, end_date=end_date, interval=interval)
    if price_data is None or price_data.empty:
        return price_data, {}
    if "Date" in price_data.columns and not isinstance(price_data.index, pd.DatetimeIndex):
        price_data = price_data.copy()
        price_data["Date"] = pd.to_datetime(price_data["Date"])
        price_data.set_index("Date", inplace=True)
    if isinstance(price_data.index, pd.DatetimeIndex) and price_data.index.tz is None:
        price_data.index = price_data.index.tz_localize("UTC")

    indicators_data: Dict[str, pd.DataFrame] = {}
    for indicator in DEFAULT_INDICATORS:
        try:
            result = indicator_calc.get_indicator(
                symbol=symbol, indicator=indicator,
                start_date=start_date, end_date=end_date,
                interval=interval, format_type="dict")
        except Exception as e:
            logger.warning(f"Skipping indicator '{indicator}' for {symbol}: {e}")
            continue
        if not isinstance(result, dict) or "dates" not in result or "values" not in result:
            logger.warning(f"Indicator '{indicator}' for {symbol} returned an unexpected "
                            f"shape, skipping: {type(result).__name__}")
            continue
        dates = pd.to_datetime(result["dates"])
        name = indicator.replace("_", " ").title()
        df = pd.DataFrame({name: result["values"]}, index=dates)
        df.index.name = "Date"
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        indicators_data[name] = df
    return price_data, indicators_data
