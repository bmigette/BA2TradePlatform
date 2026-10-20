"""
Alpaca History OHLCV Provider

The backtest OHLCV history built offline by ``tools/prepare_history.py --source alpaca`` from raw
Alpaca SIP bars and the verified split table (plan ``docs/plans/2026-10-20-alpaca-history-source.md``).
The files sit in the same layout as the FMP cache, ``CACHE_FOLDER/AlpacaHistoryOHLCVProvider/
<SYM>_1d.parquet`` and ``<SYM>_5min.parquet`` (``Date`` New York naive, ``Open, High, Low, Close,
Volume, effective_date``; the 5-min files hold the regular session only), and the base cache layer
(``MarketDataProviderInterface.get_ohlcv_data``) serves them. The ``_meta/`` folder of the build is
not a bar file and is never read.

CACHE-ONLY: there is no vendor behind this class. A read the cache cannot answer, and any top-up of a
stale cache, raises ``HistoryTopUpUnavailable``. It needs no key and has no split calendar: the split
basis is the build's, not re-checked against a vendor calendar.
"""

from typing import Annotated
from datetime import datetime
import pandas as pd

from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.logger import logger


class HistoryTopUpUnavailable(RuntimeError):
    """The Alpaca history cache cannot answer a read, and this provider has no live source to ask."""


class AlpacaHistoryOHLCVProvider(MarketDataProviderInterface):
    """Cache-only OHLCV provider over the Alpaca-built history (registry key ``alpaca_hist``).

    The cache folder is the class name (the base derives it), so a push or a reader that names
    ``AlpacaHistoryOHLCVProvider`` and this class agree by construction.

    Hooks left at the base's defaults on purpose: ``WRITES_FULL_FETCH_MARKER`` is False (no cold full
    fetch ever happens, so there is no marker to write) and ``_split_calendar`` returns None (no
    vendor calendar to compare the build's basis with)."""

    def __init__(self):
        super().__init__()
        logger.debug("Initialized AlpacaHistoryOHLCVProvider (cache-only)")

    def _get_ohlcv_data_impl(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        start_date: Annotated[datetime, "Start date for data"],
        end_date: Annotated[datetime, "End date for data"],
        interval: Annotated[str, "Data interval"] = "1d",
    ) -> pd.DataFrame:
        raise HistoryTopUpUnavailable(
            f"AlpacaHistoryOHLCVProvider has no live source: {symbol} ({interval}) "
            f"{start_date} .. {end_date} is not in the prepared cache (or the cache is stale). "
            f"Build it with `tools/prepare_history.py --source alpaca`.")

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alpaca_hist"

    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["ohlcv", "intraday", "daily"]

    def validate_config(self) -> bool:
        """No configuration: the provider reads the prepared cache only."""
        return True
