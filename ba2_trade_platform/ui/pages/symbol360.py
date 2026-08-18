"""
SYMBOL360 Tool

Consolidated per-symbol research dashboard: pulls every metric the platform's
experts already compute (Weinstein stage, RVOL, earnings/PEAD, insider
activity, analyst ratings, Senate/House trades, DeterministicScorer's
technical/fundamental/macro breakdown, FactorRanker's factor score) plus a
price chart, tagging each with buy/sell/neutral where applicable.

Design: docs/superpowers/specs/2026-08-17-symbol360-design.md
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from nicegui import app, ui

from ...config import get_app_setting
from ...core.interfaces import ExpertDataExport
from ...logger import logger
from ...modules.dataproviders import get_provider
from ...modules.dataproviders.indicators.PandasIndicatorCalc import PandasIndicatorCalc
from ...modules.experts.DeterministicScorer import DeterministicScorer
from ...modules.experts.expert_mixins import FMPCongressTradingMixin
from ...modules.experts.FactorRanker import FactorRanker
from ...modules.experts.FinnHubRating import FinnHubRating
from ...modules.experts.FMPEarningsDrift import FMPEarningsDrift
from ...modules.experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
from ...modules.experts.FMPRating import FMPRating
from ..components.InstrumentGraph import InstrumentGraph
from ..components.symbol_chart_data import build_chart_data
# DeterministicScorer.data has no in-tree shim (package-only helper, like
# symbol_snapshot below) -- reached directly, same convention Task 9/10 used.
from ba2_experts.DeterministicScorer.data import fetch_price_targets
from ba2_providers.symbol_snapshot import (
    fetch_profile, fetch_quote, rvol_from_quote, weinstein_from_closes,
)

STORAGE_KEY = "symbol360_settings"   # app.storage.user[STORAGE_KEY][expert_name] = overrides

# Weinstein needs sma_period(150) + slope_lookback(20) = 170 trading days of
# history at minimum (see ba2_common.core.weinstein.classify_weinstein_stage);
# 400 CALENDAR days (~280 trading days) leaves comfortable headroom. Reused as
# the chart's lookback too via build_chart_data's own default.
_WEINSTEIN_LOOKBACK_DAYS = 400

_SIGNAL_COLOR = {"buy": "positive", "sell": "negative", "hold": "grey"}


def _signal_badge(signal: Optional[str]) -> None:
    """Small colored buy/sell/hold badge -- no-op when signal is None (n/a)."""
    if not signal:
        return
    ui.badge(signal.upper(), color=_SIGNAL_COLOR.get(signal, "grey"))


# --------------------------------------------------------------------------
# Fetch functions -- each runs inside asyncio.to_thread(fn, symbol) from
# _async_search, so NONE of these may touch app.storage (see _get_overrides'
# docstring). Any per-card settings override is read on the UI thread inside
# _card_specs and closed over here as a plain dict.
# --------------------------------------------------------------------------

def _fetch_header(symbol: str) -> Optional[Dict[str, Any]]:
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        return None
    quote = fetch_quote(api_key, symbol)
    profile = fetch_profile(api_key, symbol)
    if quote is None and profile is None:
        return None
    return {"quote": quote, "profile": profile}


def _fetch_chart(symbol: str) -> Optional[Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]]:
    ohlcv_provider = get_provider("ohlcv", "fmp")   # raises if FMP_API_KEY unset
    indicator_calc = PandasIndicatorCalc(ohlcv_provider)   # ctor REQUIRES ohlcv_provider
    return build_chart_data(symbol, ohlcv_provider, indicator_calc)


def _fetch_weinstein(symbol: str) -> Optional[Dict[str, Any]]:
    ohlcv_provider = get_provider("ohlcv", "fmp")
    end_date = datetime.now()
    start_date = end_date - timedelta(days=_WEINSTEIN_LOOKBACK_DAYS)
    df = ohlcv_provider.get_ohlcv_data(symbol=symbol, start_date=start_date,
                                       end_date=end_date, interval="1d")
    if df is None or df.empty or "Close" not in df.columns:
        return None
    return weinstein_from_closes(df["Close"].tolist())


def _fetch_rvol(symbol: str) -> Optional[Dict[str, Any]]:
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        return None
    quote = fetch_quote(api_key, symbol)
    if quote is None:
        return None
    return {"rvol": rvol_from_quote(quote), "quote": quote}


def _fetch_analyst(symbol: str, fmp_overrides: Dict[str, Any],
                   finnhub_overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Wraps FMPRating + FinnHubRating (each independently overridable, keyed
    by their own expert_name -- see _render_export_card's Save&Re-run) plus
    optional dated individual price targets. The price-target sub-fetch is
    wrapped locally (not left to _run's per-card catch) because a failure
    there would otherwise discard the two sub-expert results that DID
    already succeed."""
    fmp_export = FMPRating.export_symbol_data(symbol, overrides=fmp_overrides)
    finnhub_export = FinnHubRating.export_symbol_data(symbol, overrides=finnhub_overrides)
    price_targets: List[Dict[str, Any]] = []
    api_key = get_app_setting("FMP_API_KEY")
    if api_key:
        try:
            price_targets = fetch_price_targets(api_key, symbol) or []
        except Exception as e:
            logger.warning(f"Symbol360: price-target fetch failed for {symbol}: {e}",
                           exc_info=True)
    return {"fmp": fmp_export, "finnhub": finnhub_export, "price_targets": price_targets}


def _fetch_congress(symbol: str) -> Optional[Dict[str, Any]]:
    """FMPCongressTradingMixin is NOT usable bare despite appearances: its
    ``_api_key``/``self.logger`` are only ever set by a real expert's
    __init__ (FMPSenateTraderCopy/Weight) -- a standalone instance has
    neither attribute, so both must be set explicitly here before calling."""
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        return None
    fetcher = FMPCongressTradingMixin()
    fetcher._api_key = api_key
    fetcher.logger = logger
    senate = fetcher._fetch_congress_trades("senate", symbol=symbol) or []
    house = fetcher._fetch_congress_trades("house", symbol=symbol) or []
    return {"senate": senate, "house": house}


def _fetch_factorranker(symbol: str, overrides: Dict[str, Any]) -> ExpertDataExport:
    """FactorRanker is basket-level; pin a synthetic single-symbol static
    universe so export_symbol_data scores just this one symbol. The 3
    universe keys are applied AFTER the user's overrides so pinning always
    wins for them; every other override key passes through untouched."""
    pinned = {
        **overrides,
        "instrument_selection_method": "static",
        "universe_source": "static",
        "enabled_instruments": {symbol: {"enabled": True}},
    }
    return FactorRanker.export_symbol_data(symbol, overrides=pinned)


def _get_overrides(expert_name: str) -> Dict[str, Any]:
    """Read persisted per-expert settings overrides. UI-thread only (see module docstring
    in ui/account_filter_context.py) — app.storage.user raises RuntimeError outside a UI
    context, e.g. if ever called from an asyncio.to_thread fetch worker."""
    try:
        return dict(app.storage.user.get(STORAGE_KEY, {}).get(expert_name, {}))
    except RuntimeError as e:
        logger.debug(f"Symbol360: storage unavailable reading overrides for {expert_name}: {e}")
        return {}


def _set_overrides(expert_name: str, overrides: Dict[str, Any]) -> None:
    """Persist per-expert settings overrides. UI-thread only — see _get_overrides."""
    try:
        store = dict(app.storage.user.get(STORAGE_KEY, {}))
        store[expert_name] = overrides
        app.storage.user[STORAGE_KEY] = store
    except RuntimeError as e:
        logger.warning(f"Symbol360: could not persist overrides for {expert_name}: {e}")


class Symbol360Tab:
    """One symbol, every metric the platform already computes."""

    def __init__(self):
        self.symbol_input = None
        self.search_button = None
        self.progress_container = None
        self.cards_container = None
        self._searching = False   # server-side re-entrancy guard — see _search()
        self.render()

    def render(self) -> None:
        with ui.card().classes("w-full"):
            ui.label("SYMBOL360").classes("text-lg font-bold")
            ui.label("Every metric the platform's experts compute for one symbol").classes(
                "text-sm mb-4").style("color: #a0aec0;")
            with ui.row().classes("w-full gap-4 items-end"):
                self.symbol_input = ui.input(label="Symbol", placeholder="e.g., AAPL").props(
                    "stack-label").classes("w-48")
                self.symbol_input.on("keydown.enter", lambda: self._search())
                self.search_button = ui.button("Search", on_click=self._search, icon="search").props(
                    "color=primary")

        self.progress_container = ui.column().classes("w-full gap-1 mb-4")
        self.cards_container = ui.column().classes("w-full gap-4")

    def _search(self) -> None:
        # Server-side re-entrancy guard: checked/set synchronously here (before the async task
        # even starts) rather than relying solely on the input/button's disabled prop, since
        # that round-trips to the browser and a fast double-click/Enter could otherwise slip a
        # second search in before the first one's disable() takes visible effect.
        if self._searching:
            ui.notify("A search is already in progress", type="warning")
            return
        symbol = (self.symbol_input.value or "").strip().upper()
        if not symbol:
            ui.notify("Enter a symbol", type="warning")
            return
        self._searching = True
        asyncio.create_task(self._async_search(symbol))

    async def _async_search(self, symbol: str) -> None:
        # UI-visible half of the re-entrancy guard: disable input+button for the duration so a
        # second search can't start (and tear down the containers) while this one's concurrent
        # fetches are still in flight. Re-enabled in `finally` so a failed search doesn't
        # permanently lock the UI.
        self.symbol_input.disable()
        self.search_button.disable()
        try:
            self.progress_container.clear()
            self.cards_container.clear()
            cards = self._card_specs(symbol)   # [(key, title, fetch_fn), ...] — Task 12 fills this in
            status_labels = {}
            with self.progress_container:
                overall = ui.linear_progress(value=0, show_value=False).classes("w-full")
                for key, title, _ in cards:
                    status_labels[key] = ui.label(f"⏳ {title}…")

            results: Dict[str, Any] = {}
            done = 0

            async def _run(key, title, fetch_fn):
                nonlocal done
                try:
                    result = await asyncio.to_thread(fetch_fn, symbol)
                except Exception as e:
                    logger.error(f"Symbol360: card '{key}' failed for {symbol}: {e}", exc_info=True)
                    result = None
                    status_text = f"❌ {title} (error)"
                else:
                    status_text = f"✅ {title}"
                results[key] = result
                status_labels[key].text = status_text  # let a disconnect RuntimeError propagate below
                done += 1
                overall.value = done / len(cards)

            await asyncio.gather(*(_run(key, title, fn) for key, title, fn in cards))
            self._render_cards(symbol, results)

        except RuntimeError as e:
            # Handle client disconnection gracefully (user closed the tab/page, or a newer
            # search tore down these containers mid-flight). NiceGUI raises RuntimeError with
            # "client...deleted" when the client itself disconnected, or "parent slot...deleted"
            # when just this element's container was cleared (e.g. by an overlapping search) —
            # both are a harmless "nothing left to update", not a real fetch failure.
            if "deleted" in str(e).lower():
                logger.debug(f"[Symbol360Tab] UI no longer available during search for {symbol}: {e}")
            else:
                logger.error(f"Error in Symbol360 search: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error in Symbol360 search: {e}", exc_info=True)
            ui.notify(f"Error searching {symbol}: {str(e)}", type="negative")
        finally:
            self._searching = False
            try:
                self.symbol_input.enable()
                self.search_button.enable()
            except RuntimeError as e:
                logger.debug(f"[Symbol360Tab] Could not re-enable input (client gone): {e}")

    def _card_specs(self, symbol: str) -> List[Tuple[str, str, Callable[[str], Any]]]:
        """[(key, title, fetch_fn), ...] -- fetch_fn(symbol) runs inside
        asyncio.to_thread, so every per-card settings override must be read
        HERE (UI thread) and closed over, never inside a fetch_fn itself."""
        fmp_rating_overrides = _get_overrides("FMPRating")
        finnhub_overrides = _get_overrides("FinnHubRating")
        earnings_overrides = _get_overrides("FMPEarningsDrift")
        insider_overrides = _get_overrides("FMPInsiderClusterBuy")
        detscorer_overrides = _get_overrides("DeterministicScorer")
        factorranker_overrides = _get_overrides("FactorRanker")

        return [
            ("header", "Header", _fetch_header),
            ("chart", "Price Chart", _fetch_chart),
            ("weinstein", "Weinstein Stage", _fetch_weinstein),
            ("rvol", "Relative Volume", _fetch_rvol),
            ("earnings", "Earnings / PEAD",
             lambda sym: FMPEarningsDrift.export_symbol_data(sym, overrides=earnings_overrides)),
            ("insider", "Insider Activity",
             lambda sym: FMPInsiderClusterBuy.export_symbol_data(sym, overrides=insider_overrides)),
            ("analyst", "Analyst Ratings",
             lambda sym: _fetch_analyst(sym, fmp_rating_overrides, finnhub_overrides)),
            ("congress", "Senate/House Activity", _fetch_congress),
            ("detscorer", "DeterministicScorer",
             lambda sym: DeterministicScorer.export_symbol_data(sym, overrides=detscorer_overrides)),
            ("factorranker", "FactorRanker",
             lambda sym: _fetch_factorranker(sym, factorranker_overrides)),
        ]

    def _render_cards(self, symbol: str, results: Dict[str, Any]) -> None:
        with self.cards_container:
            self._render_header_card(results.get("header"))
            self._render_chart_card(symbol, results.get("chart"))
            with ui.row().classes("w-full gap-4"):
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_weinstein_card(results.get("weinstein"))
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_rvol_card(results.get("rvol"))
            with ui.row().classes("w-full gap-4"):
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_export_card("Earnings / PEAD", results.get("earnings"))
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_export_card("Insider Activity", results.get("insider"))
            with ui.row().classes("w-full gap-4"):
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_analyst_card(results.get("analyst"))
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_congress_card(results.get("congress"))
            self._render_export_card("DeterministicScorer", results.get("detscorer"))
            self._render_export_card("FactorRanker", results.get("factorranker"))

    # ---------------------------------------------------------------- direct-data cards

    def _render_header_card(self, header: Optional[Dict[str, Any]]) -> None:
        with ui.card().classes("w-full"):
            if header is None:
                ui.label("Header unavailable (missing FMP API key or bad symbol)").classes(
                    "text-sm text-red-400")
                return
            quote = header.get("quote") or {}
            profile = header.get("profile") or {}
            with ui.row().classes("w-full items-center gap-6 flex-wrap"):
                name = profile.get("companyName")
                if name:
                    ui.label(name).classes("text-lg font-bold")
                price = quote.get("price")
                if price is not None:
                    ui.label(f"${price:,.2f}").classes("text-xl")
                change_pct = quote.get("changesPercentage")
                if change_pct is not None:
                    color = "#26a69a" if change_pct >= 0 else "#ef5350"
                    ui.label(f"{change_pct:+.2f}%").classes("text-lg").style(f"color: {color};")
            with ui.row().classes("w-full gap-6 flex-wrap mt-1"):
                sector = profile.get("sector")
                industry = profile.get("industry")
                mcap = quote.get("marketCap") or profile.get("mktCap")
                exch = quote.get("exchange") or profile.get("exchangeShortName")
                for label, value in (
                    ("Sector", sector), ("Industry", industry),
                    ("Mkt Cap", f"${mcap:,.0f}" if mcap else None),
                    ("Exchange", exch),
                ):
                    if value:
                        ui.label(f"{label}: {value}").classes("text-xs").style("color: #a0aec0;")

    def _render_chart_card(self, symbol: str, chart) -> None:
        if chart is None:
            with ui.card().classes("w-full"):
                ui.label("Price Chart").classes("text-md font-bold")
                ui.label("Chart unavailable").classes("text-sm text-red-400")
            return
        price_data, indicators_data = chart
        if price_data is None or price_data.empty:
            with ui.card().classes("w-full"):
                ui.label("Price Chart").classes("text-md font-bold")
                ui.label("No price data available").classes("text-sm text-red-400")
            return
        InstrumentGraph(symbol=symbol, price_data=price_data, indicators_data=indicators_data).render()

    def _render_weinstein_card(self, w: Optional[Dict[str, Any]]) -> None:
        with ui.card().classes("w-full"):
            ui.label("Weinstein Stage").classes("text-md font-bold")
            if w is None:
                ui.label("Unavailable").classes("text-sm text-red-400")
                return
            stage = w.get("stage")
            with ui.row().classes("items-center gap-2 mt-1"):
                ui.label(f"Stage {stage}" if stage is not None else "Unknown").classes("text-lg")
                _signal_badge(w.get("signal"))
            if w.get("reason"):
                ui.label(w["reason"]).classes("text-xs").style("color: #a0aec0;")
            for key, label in (("price", "Price"), ("sma", "SMA150"), ("slope_pct", "Slope %")):
                value = w.get(key)
                if value is not None:
                    ui.label(f"{label}: {value:,.2f}").classes("text-sm")

    def _render_rvol_card(self, r: Optional[Dict[str, Any]]) -> None:
        with ui.card().classes("w-full"):
            ui.label("Relative Volume").classes("text-md font-bold")
            if r is None:
                ui.label("Unavailable").classes("text-sm text-red-400")
                return
            rvol = r.get("rvol")
            with ui.row().classes("items-center gap-2 mt-1"):
                if rvol is None:
                    ui.label("Unknown (no average volume)").classes("text-sm").style(
                        "color: #a0aec0;")
                else:
                    ui.label(f"{rvol:.2f}x").classes("text-lg")
                    if rvol >= 2.0:
                        ui.badge("ELEVATED", color="warning")
            quote = r.get("quote") or {}
            volume, avg_volume = quote.get("volume"), quote.get("avgVolume")
            if volume is not None:
                ui.label(f"Volume: {volume:,}").classes("text-xs").style("color: #a0aec0;")
            if avg_volume is not None:
                ui.label(f"Avg Volume: {avg_volume:,}").classes("text-xs").style("color: #a0aec0;")

    def _render_analyst_card(self, analyst: Optional[Dict[str, Any]]) -> None:
        with ui.card().classes("w-full"):
            ui.label("Analyst Ratings").classes("text-md font-bold mb-2")
            if analyst is None:
                ui.label("Unavailable").classes("text-sm text-red-400")
                return
            with ui.row().classes("w-full gap-4 flex-wrap"):
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_export_card("FMP Rating", analyst.get("fmp"))
                with ui.column().classes("flex-1 min-w-0"):
                    self._render_export_card("FinnHub Rating", analyst.get("finnhub"))
            targets = analyst.get("price_targets") or []
            if targets:
                with ui.expansion(f"Dated Price Targets ({len(targets)})").classes("w-full"):
                    columns = [
                        {"name": "date", "label": "Published", "field": "date", "align": "center", "sortable": True},
                        {"name": "analyst", "label": "Analyst", "field": "analyst", "align": "left"},
                        {"name": "firm", "label": "Firm", "field": "firm", "align": "left"},
                        {"name": "target", "label": "Target", "field": "target", "align": "right", "sortable": True},
                    ]
                    rows = []
                    for i, t in enumerate(targets):
                        rows.append({
                            "id": i,
                            "date": t.get("publishedDate") or "",
                            "analyst": t.get("analystName") or "",
                            "firm": t.get("analystCompany") or "",
                            "target": t.get("priceTarget"),
                        })
                    rows.sort(key=lambda row: row["date"], reverse=True)
                    ui.table(columns=columns, rows=rows, row_key="id",
                            pagination={"rowsPerPage": 10, "sortBy": "date", "descending": True}
                            ).classes("w-full dark-pagination")

    def _render_congress_card(self, congress: Optional[Dict[str, Any]]) -> None:
        with ui.card().classes("w-full"):
            ui.label("Senate/House Activity").classes("text-md font-bold mb-2")
            if congress is None:
                ui.label("Unavailable (missing FMP API key)").classes("text-sm text-red-400")
                return
            trades = []
            for t in congress.get("senate") or []:
                t = dict(t); t["_chamber"] = "Senate"; trades.append(t)
            for t in congress.get("house") or []:
                t = dict(t); t["_chamber"] = "House"; trades.append(t)
            if not trades:
                ui.label("No recent trades found").classes("text-sm").style("color: #a0aec0;")
                return
            rows = []
            for i, t in enumerate(trades):
                first = t.get("firstName", "") or ""
                last = t.get("lastName", "") or ""
                rows.append({
                    "id": i,
                    "chamber": t["_chamber"],
                    "trader": f"{first} {last}".strip() or "Unknown",
                    "transaction_type": t.get("type") or t.get("transactionType") or "Unknown",
                    "amount": t.get("amount") or t.get("amountRange") or "",
                    "transaction_date": t.get("transactionDate") or "",
                })
            rows.sort(key=lambda row: row["transaction_date"], reverse=True)
            columns = [
                {"name": "chamber", "label": "Chamber", "field": "chamber", "align": "center", "sortable": True},
                {"name": "trader", "label": "Trader", "field": "trader", "align": "left", "sortable": True},
                {"name": "transaction_type", "label": "Type", "field": "transaction_type", "align": "center"},
                {"name": "amount", "label": "Amount", "field": "amount", "align": "right"},
                {"name": "transaction_date", "label": "Date", "field": "transaction_date", "align": "center", "sortable": True},
            ]
            table = ui.table(columns=columns, rows=rows, row_key="id",
                             pagination={"rowsPerPage": 10, "sortBy": "transaction_date", "descending": True}
                             ).classes("w-full dark-pagination")
            # Same Purchase/Sale coloring slot as FMPSenateTradeTab in tools.py.
            table.add_slot("body-cell-transaction_type", '''
                <q-td :props="props">
                    <q-badge :color="props.value.includes('Purchase') || props.value.includes('Buy') ? 'positive' : props.value.includes('Sale') || props.value.includes('Sell') ? 'negative' : 'grey'">
                        {{ props.value }}
                    </q-badge>
                </q-td>
            ''')

    # ---------------------------------------------------------------- ExpertDataExport cards

    def _render_export_card(self, title: str, export: Optional[ExpertDataExport]) -> None:
        """Shared renderer for every ExpertDataExportInterface-backed card
        (earnings/insider/FMPRating/FinnHubRating/DeterministicScorer/
        FactorRanker) -- error/skip states, signal badge, metric rows, and
        the per-card settings expander, written once instead of 6 times."""
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(title).classes("text-md font-bold")
                if export is not None and not export.error:
                    if export.skipped:
                        ui.icon("skip_next", color="orange").tooltip("Skipped")
                    _signal_badge(export.overall_signal)
            if export is None:
                ui.label("Failed to load").classes("text-sm text-red-400")
                return
            if export.error:
                ui.label(f"Error: {export.error}").classes("text-sm text-red-400")
                return
            if export.confidence is not None:
                ui.label(f"Confidence: {export.confidence:.1f}%").classes("text-xs").style(
                    "color: #a0aec0;")
            for m in export.metrics:
                with ui.row().classes("w-full items-center gap-2"):
                    if m.signal:
                        ui.badge("", color=_SIGNAL_COLOR.get(m.signal, "grey")).classes("w-3 h-3 p-0")
                    ui.label(m.label).classes("text-sm font-medium").style("min-width: 12rem;")
                    ui.label(m.display).classes("text-sm")
                    if m.detail:
                        ui.icon("info", size="xs").tooltip(m.detail).classes("text-xs")
            self._render_settings_expander(export)

    def _render_settings_expander(self, export: ExpertDataExport) -> None:
        """Collapsed '⚙ Settings' expander pre-filled from settings_used.
        Dict/list-valued settings (universe configs, schedules, ...) are
        plumbing, not user-tunable, and are skipped. 'Save & Re-run' only
        PERSISTS the override (keyed by the real expert_name -- for the
        Analyst card's two sub-experts that means each is saved under its
        own name, not a nested blob) and prompts a fresh search; it does not
        live-refresh this one card in place (per the design doc, that's
        acceptable for this task)."""
        with ui.expansion("⚙ Settings").classes("w-full"):
            fields: Dict[str, Tuple[Any, type]] = {}
            for key, value in export.settings_used.items():
                if isinstance(value, (dict, list)):
                    continue
                if isinstance(value, bool):
                    fields[key] = (ui.checkbox(key, value=value), bool)
                elif isinstance(value, int):
                    fields[key] = (ui.number(key, value=value, format="%d"), int)
                elif isinstance(value, float):
                    fields[key] = (ui.number(key, value=value), float)
                elif isinstance(value, str):
                    fields[key] = (ui.input(key, value=value), str)
                # else: unrecognized type -- skip, nothing sane to render

            def _save() -> None:
                collected: Dict[str, Any] = {}
                for key, (widget, caster) in fields.items():
                    try:
                        collected[key] = caster(widget.value)
                    except (TypeError, ValueError) as e:
                        logger.warning(f"Symbol360: could not save setting {key!r}={widget.value!r} "
                                       f"for {export.expert_name}: {e}")
                _set_overrides(export.expert_name, collected)
                ui.notify(f"Saved settings for {export.expert_name}. Search again to apply.",
                         type="positive")

            ui.button("Save & Re-run", on_click=_save, icon="save").props("size=sm color=primary")
