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

from ...config import get_app_setting
from ...core.models import MarketAnalysis
from ...core.types import MarketAnalysisStatus
from ...logger import logger as _module_logger


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

        Returns:
            List of trade records, ``[]`` for non-list payloads, or ``None`` on
            error / missing API key (when not raising).
        """
        label = f"{chamber} trades"
        if not self._api_key:
            self.logger.error(f"Cannot fetch {label}: FMP API key not configured")
            return None

        symbol_text = f" for {symbol}" if symbol else " (all)"
        try:
            if symbol:
                url = f"https://financialmodelingprep.com/stable/{chamber}-trades"
                params = {
                    "apikey": self._api_key,
                    "symbol": symbol.upper(),
                }
                self.logger.debug(f"Fetching FMP {label} for {symbol}")
            else:
                # Latest disclosures endpoint with pagination for all trades
                url = f"https://financialmodelingprep.com/stable/{chamber}-latest"
                params = {
                    "apikey": self._api_key,
                    "page": 0,
                    "limit": 1000,  # Maximum allowed per request
                }
                self.logger.debug(f"Fetching all FMP {label} (latest disclosures)")

            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()

            data = response.json()
            self.logger.debug(
                f"Received {len(data) if isinstance(data, list) else 0} {chamber} trade records{symbol_text}"
            )

            return data if isinstance(data, list) else []

        except requests.exceptions.RequestException as e:
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
