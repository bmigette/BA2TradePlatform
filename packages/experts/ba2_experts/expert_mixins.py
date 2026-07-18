"""
Shared mixins for market expert implementations (EX-2 / CQ-3).

- AnalysisStatusRenderMixin: the pending/running/failed/skipped render scaffolding
  and the status dispatcher that were previously copy-pasted across FMPRating,
  FinnHubRating, FMPSenateTraderCopy and FMPSenateTraderWeight. Experts customize
  the label texts via class attributes and keep their own ``_render_completed``.
- FMPApiKeyMixin: FMP API key lookup shared by all FMP-backed experts.
- FMPCongressTradingMixin: senate/house trade fetching shared by the two Senate
  trader experts (Copy keeps return-None-on-error semantics, Weight keeps
  raise-on-error semantics — both expressed through ``_fetch_congress_trades``
  parameters so behavior is unchanged).
"""

from typing import Any, Dict, List, Optional

import requests

from ba2_common.config import get_app_setting
from ba2_common.core.models import MarketAnalysis
from ba2_common.core.types import MarketAnalysisStatus
from ba2_common.logger import logger as _module_logger


class AnalysisStatusRenderMixin:
    """Status dispatcher + pending/running/failed/skipped cards for expert UIs.

    Subclasses must implement ``_render_completed(market_analysis)`` and may
    override the ``RENDER_*`` class attributes to customize the card texts.
    ``*_MESSAGE`` templates receive ``{symbol}`` via ``str.format``.
    """

    RENDER_PENDING_TITLE = "Analysis Pending"
    RENDER_PENDING_MESSAGE = "Analysis for {symbol} is queued"
    RENDER_RUNNING_TITLE = "Analysis Running"
    RENDER_RUNNING_MESSAGE = "Analyzing {symbol}..."
    RENDER_FAILED_TITLE = "Analysis Failed"

    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """Render market analysis results in the UI by status."""
        from nicegui import ui

        try:
            if market_analysis.status == MarketAnalysisStatus.PENDING:
                self._render_pending(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.RUNNING:
                self._render_running(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.FAILED:
                self._render_failed(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.COMPLETED:
                self._render_completed(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.SKIPPED:
                self._render_skipped(market_analysis)
            else:
                with ui.card().classes('w-full p-4'):
                    ui.label(f"Unknown analysis status: {market_analysis.status}")

        except Exception as e:
            self.logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Rendering Error').classes('text-h5 text-negative')
                ui.label(f'Failed to render analysis: {str(e)}').classes('text-grey-7')

    def _render_pending(self, market_analysis: MarketAnalysis) -> None:
        """Render pending analysis state."""
        from nicegui import ui

        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('schedule', size='3rem', color='grey').classes('mb-4')
            ui.label(self.RENDER_PENDING_TITLE).classes('text-h5')
            ui.label(self.RENDER_PENDING_MESSAGE.format(symbol=market_analysis.symbol)).classes('text-grey-7')

    def _render_running(self, market_analysis: MarketAnalysis) -> None:
        """Render running analysis state."""
        from nicegui import ui

        with ui.card().classes('w-full p-8 text-center'):
            ui.spinner(size='3rem', color='primary').classes('mb-4')
            ui.label(self.RENDER_RUNNING_TITLE).classes('text-h5')
            ui.label(self.RENDER_RUNNING_MESSAGE.format(symbol=market_analysis.symbol)).classes('text-grey-7')

    def _render_failed(self, market_analysis: MarketAnalysis) -> None:
        """Render failed analysis state."""
        from nicegui import ui

        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('error', color='negative', size='2rem')
                ui.label(self.RENDER_FAILED_TITLE).classes('text-h5 text-negative ml-2')

            if market_analysis.state and isinstance(market_analysis.state, dict):
                error_msg = market_analysis.state.get('error', 'Unknown error')
                ui.label(f'Error: {error_msg}').classes('text-grey-8')

    def _render_skipped(self, market_analysis: MarketAnalysis) -> None:
        """Render skipped analysis state."""
        from nicegui import ui

        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('skip_next', color='orange', size='2rem')
                ui.label('Analysis Skipped').classes('text-h5 text-orange ml-2')

            if market_analysis.state and isinstance(market_analysis.state, dict):
                skip_msg = market_analysis.state.get('skip_message') or market_analysis.state.get('skip_reason', 'Analysis was skipped')
                ui.label(f'Reason: {skip_msg}').classes('text-grey-8')


class FMPApiKeyMixin:
    """FMP API key lookup shared by FMP-backed experts."""

    def _get_fmp_api_key(self) -> Optional[str]:
        """Get FMP API key from app settings."""
        api_key = get_app_setting('FMP_API_KEY')
        if not api_key:
            # self.logger may not be assigned yet when this runs from __init__;
            # fall back to the module logger rather than raising AttributeError.
            getattr(self, 'logger', _module_logger).warning("FMP API key not found in app settings")
        return api_key


class FMPCongressTradingMixin(FMPApiKeyMixin):
    """Senate/house trade fetching shared by the FMP Senate trader experts."""

    def _fetch_congress_trades(
        self,
        chamber: str,
        symbol: Optional[str] = None,
        timeout: int = 30,
        raise_on_error: bool = False,
        full_history: bool = False,
        max_pages: int = 200,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch senate/house trades from the FMP API.

        Args:
            chamber: ``"senate"`` or ``"house"``.
            symbol: Stock symbol to query. When None, fetches the latest
                disclosures across all symbols (``{chamber}-latest`` endpoint).
            timeout: Request timeout in seconds.
            raise_on_error: When True, request failures raise ``ValueError``
                (FMPSenateTraderWeight semantics); when False they are logged
                with traceback and ``None`` is returned (Copy semantics).
            full_history: Only meaningful when ``symbol`` is None (the unscoped
                ``{chamber}-latest`` feed). See "Pagination-depth design" below.
            max_pages: Safety cap on pagination when ``full_history=True``
                (default 200 = up to 200k rows — mirrors
                ``tools/build_senate_universe.py``'s ``--max-pages`` default).

        Returns:
            List of trade records, ``[]`` for non-list payloads, or ``None`` on
            error / missing API key (when not raising).

        --- Pagination-depth design (read before touching this method) --------------
        BUG THIS FIXES: the unscoped ``symbol=None`` branch used to fetch ONLY
        ``page=0`` of the ``{chamber}-latest`` endpoint — ~1000 rows, i.e. whatever
        the most recent ~4 months of disclosures happened to be at fetch time. Any
        backtest walking further back than that (e.g. a multi-year GA grid starting
        2023-01-01) saw ZERO unscoped trades for nearly the whole run — confirmed in
        practice: a 4-job Senate matrix grid (2023-01-01..2026-06-30) scored
        ``trades: 0, fitness: -1e9`` for every individual across every generation,
        because ``FMPSenateTraderWeight``'s basket-mode ``_gather_all`` depends
        entirely on this unscoped fetch.

        FIX: ``full_history=True`` makes the unscoped branch paginate
        ``page=0..max_pages-1`` (mirroring ``tools/build_senate_universe.py``'s
        ``_fetch_latest_disclosures`` — the same endpoint, same pagination shape),
        accumulating every row until a page comes back empty (end of feed) or
        ``max_pages`` is hit. No floor date is threaded through: this fetch happens
        ONCE per (frozen/hermetic) run via a prewarm step that has no principled way
        to know every future caller's date range without peeking at a specific
        backtest's ``--start`` — and per this codebase's hermetic-backtest
        philosophy (0-fetch guarantee: prewarm once with full knowledge, then the
        run itself never fetches), the runtime ``_gather_all`` call should NOT be the
        one deciding "how far back is enough" per bar. Paginating unconditionally to
        the end of the feed (verified in ``build_senate_universe.py`` to reach back
        to ~2012/2019 for senate/house) is simple, gets the answer right for any
        realistic backtest start date, and the extra rows cost nothing beyond a
        one-time prewarm fetch (an in-memory/disk cache serves them for the rest of
        the run either way). ``full_history=False`` (the default) preserves the
        EXACT original page-0-only behavior byte-for-byte — every existing caller
        (``FMPSenateTraderCopy``'s live path, which only ever needs "recent"
        disclosures) is unaffected unless it explicitly opts in.

        A PER-PAGE FETCH FAILURE stops the walk and returns whatever was accumulated so
        far, rather than discarding it (mirrors ``_fetch_latest_disclosures``'s "stop this
        chamber, keep the other" catch). Confirmed live 2026-07-18: FMP's ``house-latest``
        endpoint hits a genuine (non-retryable) HTTP 400 once ``page`` exceeds an internal
        depth limit (~page 100) — nowhere near ``max_pages=200`` and not a transient error
        ``fmp_http_get``'s retry logic would recover from. Without the catch, that single
        deep page would raise and lose the 100k+ rows already fetched (and the disk-cache
        write, since ``fmp_history_disk_cached`` never persists on a raised exception) —
        re-introducing a truncation bug of a different shape. Treating a hard per-page
        failure as "reached the end of what this feed will give up" is the safe, honest
        reading: it is observably still a MASSIVE improvement over the single-page
        original (100+ pages vs. 1), even on a chamber whose API-side depth limit is more
        restrictive than the safety cap.

        CACHE-KEY DESIGN: ``fmp_history_disk_cached`` serves whatever was last
        written under a given ``(namespace, key)`` for the remainder of a
        frozen/hermetic run, and in HERMETIC mode ignores file age entirely. If the
        shallow (page-0) and deep (paginated) results shared the SAME "ALL" key,
        whichever fetch happened to run first — or was left on disk from an earlier
        shallow-fetching process — would silently satisfy a LATER caller that needed
        the other depth. A deep caller silently served a stale shallow cache entry
        is exactly this bug, just hidden behind a cache hit. So ``full_history=True``
        uses its OWN cache key (``"ALL_FULL_HISTORY"``, distinct from ``"ALL"``):
        a shallow request can never read a deep-or-shallow file under the wrong key,
        and a deep request can never silently read a shallow one. Both keys are
        idempotent and permanent for the life of a frozen/hermetic run, exactly like
        every other ``fmp_history_disk_cached`` namespace — no depth/floor tracking
        needed inside the key itself.
        ------------------------------------------------------------------------------
        """
        label = f"{chamber} trades"
        if not self._api_key:
            self.logger.error(f"Cannot fetch {label}: FMP API key not configured")
            return None

        from ba2_providers.fmp_common import fmp_http_get, FMPError

        symbol_text = f" for {symbol}" if symbol else " (all)"
        paginate = symbol is None and full_history
        try:
            if symbol:
                url = f"https://financialmodelingprep.com/stable/{chamber}-trades"
                params = {
                    "apikey": self._api_key,
                    "symbol": symbol.upper(),
                }
                self.logger.debug(f"Fetching FMP {label} for {symbol}")
            else:
                # Latest disclosures endpoint; page 0 by default (paginate expands this).
                url = f"https://financialmodelingprep.com/stable/{chamber}-latest"
                params = {
                    "apikey": self._api_key,
                    "page": 0,
                    "limit": 1000,  # Maximum allowed per request
                }
                self.logger.debug(
                    f"Fetching {'FULL-HISTORY (paginated)' if paginate else 'latest page of'} "
                    f"FMP {label} (latest disclosures)"
                )

            # Route through the GLOBAL FMP rate-limit gate (a raw requests.get storms
            # the limit under the parallel grid). fmp_http_get retries 429/5xx with a
            # shared backoff and calls raise_for_status internally.
            def _do_fetch():
                if paginate:
                    import time as _time
                    rows: List[Dict[str, Any]] = []
                    for page in range(max_pages):
                        # A mid-pagination failure (retries already exhausted inside
                        # fmp_http_get) must NOT discard everything accumulated so far —
                        # mirrors tools/build_senate_universe.py's _fetch_latest_disclosures,
                        # which stops that chamber's walk on any fetch error and keeps what
                        # it has. Confirmed live 2026-07-18: FMP's house-latest endpoint
                        # returns a genuine HTTP 400 once page exceeds its own internal depth
                        # limit (~page 100) — NOT a transient/retryable error, so without this
                        # catch the whole deep fetch (and its cache write) would be lost, even
                        # though 100+ pages of real history were already fetched.
                        try:
                            resp = fmp_http_get(
                                url, {**params, "page": page}, symbol="",
                                endpoint=f"{chamber}-trades", timeout=timeout,
                            )
                        except (requests.exceptions.RequestException, FMPError) as e:
                            self.logger.warning(
                                f"Full-history {label} pagination stopped at page {page} "
                                f"(fetch failed: {e}); keeping {len(rows)} rows fetched so far."
                            )
                            break
                        d = resp.json()
                        if not isinstance(d, list) or not d:
                            break  # end of feed
                        rows.extend(d)
                        if page < max_pages - 1:
                            _time.sleep(0.1)  # gentle on the shared rate-limit gate
                    return rows
                resp = fmp_http_get(
                    url, params, symbol=(symbol or ""), endpoint=f"{chamber}-trades",
                    timeout=timeout,
                )
                d = resp.json()
                return d if isinstance(d, list) else []

            # BACKTEST-ONLY disk cache (freeze-gated; live = passthrough): time-invariant PAST
            # disclosure data otherwise refetched every analysis bar. as_of filtering happens
            # downstream, so no lookahead. The per-symbol ``-trades`` list is keyed by symbol; the
            # ``-latest`` (all-symbols, recency) list is cached under a single fixed per-chamber
            # key (shallow "ALL" or deep "ALL_FULL_HISTORY" — see the pagination-depth design
            # above) so it is fetched once per worker instead of (universe size) times per bar.
            from ba2_providers.fmp_common import fmp_history_disk_cached
            if symbol:
                data = fmp_history_disk_cached(f"congress_{chamber}_trades", symbol, _do_fetch)
            elif full_history:
                data = fmp_history_disk_cached(f"congress_{chamber}_latest", "ALL_FULL_HISTORY", _do_fetch)
            else:
                data = fmp_history_disk_cached(f"congress_{chamber}_latest", "ALL", _do_fetch)
            self.logger.debug(
                f"Received {len(data) if isinstance(data, list) else 0} {chamber} trade records{symbol_text}"
            )

            return data if isinstance(data, list) else []

        except (requests.exceptions.RequestException, FMPError) as e:
            error_msg = f"Failed to fetch FMP {label}{symbol_text}: {e}"
            if raise_on_error:
                self.logger.error(error_msg)
                raise ValueError(error_msg) from e
            self.logger.error(error_msg, exc_info=True)
            return None
        except Exception as e:
            error_msg = f"Unexpected error fetching {label}{symbol_text}: {e}"
            if raise_on_error:
                self.logger.error(error_msg)
                raise ValueError(error_msg) from e
            self.logger.error(error_msg, exc_info=True)
            return None
