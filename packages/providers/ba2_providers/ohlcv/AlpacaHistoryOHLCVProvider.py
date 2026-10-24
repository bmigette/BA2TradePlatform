"""
Alpaca History OHLCV Provider

The backtest OHLCV history built offline by ``tools/prepare_history.py --source alpaca`` from raw
Alpaca SIP bars and the verified split table (plan ``docs/plans/2026-10-20-alpaca-history-source.md``).
The files sit in the same layout as the FMP cache, ``CACHE_FOLDER/AlpacaHistoryOHLCVProvider/
<SYM>_1d.parquet`` and ``<SYM>_5min.parquet`` (``Date`` New York naive, ``Open, High, Low, Close,
Volume, effective_date``; the 5-min files hold the regular session only), and the base cache layer
(``MarketDataProviderInterface.get_ohlcv_data``) serves them. The ``_meta/`` folder of the build is
not a bar file and is never read.

WITHOUT credentials it is CACHE-ONLY (backtests, GA workers): a read the cache cannot answer, and any
top-up of a stale cache, raises ``HistoryTopUpUnavailable``; it needs no key and has no split calendar.

WITH credentials (an account's Alpaca keys; plan 2026-10-24-history-source-task11) a LATEST read appends the
finished sessions Alpaca has beyond the file's last bar, in place and ungated (owner decision), under the
rules of ``alpaca_history.live_topup`` (contiguous, split table applied, no mixed basis) and the per-symbol
cross-process lock of ``alpaca_history.live_state``. A pinned (backtest) read never reaches the top-up.
"""

from typing import Annotated, Any, Callable, Optional
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

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, *,
                 fetcher: Optional[Any] = None, clock: Optional[Callable[[], datetime]] = None,
                 corporate_actions_refresher: Optional[Callable[[str], None]] = None,
                 corporate_actions_client: Optional[Any] = None,
                 fmp_split_rows: Optional[Callable[..., list]] = None):
        super().__init__()
        self._fetcher = fetcher
        self._ca_client = corporate_actions_client
        self._fmp_split_rows = fmp_split_rows
        if api_key and api_secret:
            if self._fetcher is None:
                from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveBars
                self._fetcher = AlpacaLiveBars(api_key, api_secret)
            if self._ca_client is None:
                from ba2_providers.ohlcv.alpaca_history.corporate_actions import AlpacaCorporateActions
                self._ca_client = AlpacaCorporateActions(api_key, api_secret)
        self._clock = clock
        self._refresher = corporate_actions_refresher
        logger.debug(f"Initialized AlpacaHistoryOHLCVProvider ({'live top-up' if self._fetcher else 'cache-only'})")

    @property
    def live(self) -> bool:
        return self._fetcher is not None

    def _get_ohlcv_data_impl(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        start_date: Annotated[datetime, "Start date for data"],
        end_date: Annotated[datetime, "End date for data"],
        interval: Annotated[str, "Data interval"] = "1d",
    ) -> pd.DataFrame:
        if self.live:
            # get_ohlcv_data ensured the requested coverage first; reaching a cold miss here means that did not produce a file
            from ba2_providers.ohlcv.alpaca_history.live_topup import SymbolNotInHistory
            raise SymbolNotInHistory(f"{symbol} ({interval}): no file after the live backfill was attempted")
        raise HistoryTopUpUnavailable(
            f"AlpacaHistoryOHLCVProvider has no live source: {symbol} ({interval}) "
            f"{start_date} .. {end_date} is not in the prepared cache (or the cache is stale). "
            f"Build it with `tools/prepare_history.py --source alpaca`.")

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval='1d', use_cache=True,
                       max_cache_age_hours=24, lookback_days=30):
        """The base read, preceded by the coverage rule for the REQUESTED start (indicator warm-up included by the caller):
        a live provider downloads only what the request needs (a symbol with no file, or a partial live file starting later
        than asked); a cache-only provider (backtests) REFUSES a window starting before a partial live backfill."""
        from ba2_common.core.provider_utils import validate_date_range
        try:
            key = self._live_interval(interval, symbol)
        except HistoryTopUpUnavailable:
            key = None
        if key is not None:
            want_start = validate_date_range(start_date, end_date, lookback_days)[0].date()
            from ba2_providers.ohlcv.alpaca_history import live_backfill
            if self.live:
                from ba2_common.core import ohlcv_final_bars
                now = self._clock() if self._clock is not None else ohlcv_final_bars.now_utc()
                live_backfill.ensure_coverage(symbol, key, want_start, fetcher=self._fetcher, now=now,
                                              ca_client=self._ca_client, fmp_rows=self._fmp_split_rows)
            else:
                live_backfill.check_backtest_window(symbol.upper(), key, want_start)
        return super().get_ohlcv_data(symbol, start_date=start_date, end_date=end_date, interval=interval,
                                      use_cache=use_cache, max_cache_age_hours=max_cache_age_hours,
                                      lookback_days=lookback_days)

    def _is_cache_valid(self, cache_file: str, max_age_hours: int = 24) -> bool:
        """File age says nothing here: the cache is an offline build, so a fresh mtime can sit on bars
        that lag the calendar. Never "valid by mtime", so a latest daily read always reaches
        ``_refresh_parquet_if_stale``, which compares the last bar with the current one."""
        return False

    def _live_interval(self, interval: str, symbol: str) -> str:
        from ba2_common.core import native_cache
        canon = native_cache.normalize_interval(interval)
        if canon == "1d":
            return "1d"
        if canon == "5m":
            return "5min"
        raise HistoryTopUpUnavailable(f"{symbol}: the live Alpaca top-up supports '1d' and '5min', not {interval!r}")

    def _refresh_parquet_if_stale(self, df, symbol, interval, provider_name):
        """A LATEST read (``end_date=None``) whose cache lags the newest finished session is topped up when this
        provider has account credentials, else it raises (the cache-only behaviour). A cache already current is
        returned as is. Pinned (backtest) reads never get here."""
        if self.live:
            return self._live_topup(df, symbol, interval, provider_name)
        last = pd.to_datetime(df['Date']).iloc[-1]
        last = last.to_pydatetime().replace(tzinfo=None)
        if last >= self.normalize_time_to_interval(datetime.now(), interval):
            return df
        raise HistoryTopUpUnavailable(
            f"{symbol} ({interval}): the prepared Alpaca history ends {last} and a latest read needs "
            f"newer bars; this provider has no live top-up. Rebuild with "
            f"`tools/prepare_history.py --source alpaca`, or read a pinned end_date.")

    def _live_topup(self, df, symbol, interval, provider_name):
        from ba2_common.core import native_cache, ohlcv_final_bars
        from ba2_providers.ohlcv.alpaca_history import live_state, live_topup
        key = self._live_interval(interval, symbol)
        now = self._clock() if self._clock is not None else ohlcv_final_bars.now_utc()
        newest = ohlcv_final_bars.last_final_session_day(symbol, now)
        last_day = pd.to_datetime(df['Date']).max().date()
        if newest is None or newest <= last_day:
            return df                                      # nothing finished beyond the file: no lock, no fetch
        from ba2_providers.ohlcv.alpaca_history import corporate_actions
        corporate_actions.assert_fresh(now)                # a dead daily refresh stops the live reads, loudly
        fetch = self._fetcher.daily if key == "1d" else self._fetcher.intraday_5min

        def attempt():
            corporate_actions.assert_not_in_review(symbol)  # an undecided corporate action blocks this symbol
            with live_state.locked_symbol(symbol):
                cur = native_cache.read_timeseries(provider_name, symbol, interval, as_of=None)
                try:
                    events = live_state.manifest_events(symbol)
                except KeyError:
                    raise live_topup.SymbolNotInHistory(
                        f"{symbol} ({interval}): not in the Alpaca build manifest; the live top-up does not backfill")
                splits = [(d, f) for d, f, k in events if k == "split"]
                spinoffs = [(d, f) for d, f, k in events if k == "spinoff"]
                merged, rep = live_topup.topup_frame(
                    cur, lambda a, b: fetch(symbol, a, b), splits, spinoffs,
                    interval=key, now_utc=now, symbol=symbol)
                if rep["appended"]:
                    native_cache.write_timeseries(provider_name, symbol, interval, merged)
                    logger.info(f"{symbol} ({interval}): appended {rep['appended']} live bar(s), sessions "
                                f"{rep['sessions'][0]}..{rep['sessions'][-1]}")
                return merged
        try:
            return attempt()
        except live_topup.PendingCorporateAction:
            if self._refresher is None:
                raise
            self._refresher(symbol)                        # outside the lock: the refresh re-bases under it
            return attempt()                               # one retry; a second failure propagates

    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "alpaca_hist"

    def get_supported_features(self) -> list[str]:
        """Get supported features of this provider."""
        return ["ohlcv", "intraday", "daily"]

    def validate_config(self) -> bool:
        """No configuration: the provider reads the prepared cache only."""
        return True
