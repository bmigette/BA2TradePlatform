"""
SYMBOL360 Tool

An at-a-glance opportunity check for one symbol. The data still comes from the
platform's own experts and providers (DeterministicScorer's technical and fundamental
evidence, analyst ratings and price targets, earnings, insider and congressional
activity, Weinstein stage, relative volume) -- but it is presented as a verdict per
area (Strong Buy .. Strong Sell) with plain-English reasons, and an overall YES/NO
reached by a transparent tally of those areas. The derivations and per-expert settings
remain available, collapsed, under "Advanced".

The verdict logic lives in ``ui/utils/symbol360_view.py`` (pure, unit-tested); this
module fetches and draws.

Original design: docs/superpowers/specs/2026-08-17-symbol360-design.md
"""
import asyncio
from datetime import date, datetime, timedelta
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
from ...modules.experts.FinnHubRating import FinnHubRating
from ...modules.experts.FMPEarningsDrift import FMPEarningsDrift
from ...modules.experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
from ...modules.experts.FMPRating import FMPRating
from ..components.InstrumentGraph import InstrumentGraph
from ..components.symbol_chart_data import build_chart_data
from ..utils.symbol360_view import (
    VERDICT_COLOR, VERDICT_LABEL, Overall, SummaryCard, build_summary,
)
# DeterministicScorer.data has no in-tree shim (package-only helper, like
# symbol_snapshot below) -- reached directly, same convention Task 9/10 used.
from ba2_experts.DeterministicScorer.data import fetch_price_targets
from ba2_common.core.interfaces.ExpertDataExportInterface import (
    DETAIL_TOOLTIP_STYLE, plan_metric_detail,
)
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


#: Same greens and reds as the rest of the app (allocation deltas, P&L).
_STATUS_COLOR = {"good": "#21ba45", "bad": "#c10015", "neutral": "#a0aec0"}
_STATUS_ICON = {"good": "✓", "bad": "✗", "neutral": "–"}


def _verdict_chip(verdict: Optional[str], *, large: bool = False) -> None:
    """Strong Buy .. Strong Sell. A card that abstains says so rather than drawing Hold:
    'no signal' and 'hold' are different statements."""
    if verdict is None:
        ui.badge("No signal", color="grey-8").props("outline")
        return
    chip = ui.badge(VERDICT_LABEL[verdict], color=VERDICT_COLOR[verdict])
    if large:
        chip.classes("text-md q-px-md q-py-xs")


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
    # get_provider("ohlcv", "fmp") -> FMPOHLCVProvider.__init__ raises ValueError immediately
    # when FMP_API_KEY is unset; guard upfront (same pattern as _fetch_header/_fetch_rvol/
    # _fetch_congress) so a missing key degrades to an "unavailable" card instead of an
    # uncaught exception that _run logs as a scary ERROR-level stack trace.
    if not get_app_setting("FMP_API_KEY"):
        return None
    ohlcv_provider = get_provider("ohlcv", "fmp")
    indicator_calc = PandasIndicatorCalc(ohlcv_provider)   # ctor REQUIRES ohlcv_provider
    return build_chart_data(symbol, ohlcv_provider, indicator_calc)


def _fetch_weinstein(symbol: str) -> Optional[Dict[str, Any]]:
    # See _fetch_chart's comment -- same missing-key guard, same reason.
    if not get_app_setting("FMP_API_KEY"):
        return None
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
            ui.label("Is this symbol a good opportunity? One verdict per area, and an overall call").classes(
                "text-sm mb-4").style("color: #a0aec0;")
            with ui.row().classes("w-full gap-4 items-center"):
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
            # FactorRanker is deliberately NOT fetched. Its score is a cross-sectional
            # z-score, which is identically zero against the one-symbol universe this page
            # would have to pin it to (its own export says so) -- a verdict chip built on
            # it would be the same for every symbol ever searched.
        ]

    def _render_cards(self, symbol: str, results: Dict[str, Any]) -> None:
        """The at-a-glance view: verdict banner, chart, one card per area, context.

        Every decision -- which lines are good or bad, each card's verdict, the overall
        tally -- is made by the pure ``symbol360_view`` module. This method only draws, so
        what a card SAYS is unit-tested without a browser.
        """
        header, cards, backdrop, overall = build_summary(symbol, results, date.today())
        with self.cards_container:
            self._render_banner(header, overall)
            self._render_chart_card(symbol, results.get("chart"))
            with ui.grid(columns=2).classes("w-full gap-4"):
                for card in cards:
                    self._render_summary_card(card)
            self._render_summary_card(backdrop)
            self._render_advanced(results)

    # ---------------------------------------------------------------- summary view

    def _render_banner(self, header: Dict[str, str], overall: Overall) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between gap-4"):
                with ui.column().classes("gap-0 min-w-0"):
                    with ui.row().classes("items-baseline gap-3"):
                        ui.label(header["symbol"]).classes("text-2xl font-bold")
                        ui.label(header["price"]).classes("text-xl")
                        ui.label(header["change"]).classes("text-md").style(
                            f"color: {_STATUS_COLOR[header['change_status']]};")
                    if header["name"]:
                        ui.label(header["name"]).classes("text-md")
                    meta = " · ".join(x for x in (header["sector"],
                                                  f"Market cap {header['market_cap']}")
                                      if x and "n/a" not in x)
                    if meta:
                        ui.label(meta).classes("text-sm").style("color: #a0aec0;")
                with ui.column().classes("items-end gap-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("OVERALL").classes("text-sm").style("color: #a0aec0;")
                        _verdict_chip(overall.verdict, large=True)
                        if overall.answer:
                            good = overall.answer == "YES"
                            ui.label(f"{'✅' if good else '❌'} {overall.answer}").classes(
                                "text-lg font-bold").style(
                                f"color: {_STATUS_COLOR['good' if good else 'bad']};")
                    ui.label(overall.summary).classes("text-sm").style("color: #a0aec0;")

    def _render_summary_card(self, card: SummaryCard) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(card.title).classes("text-md font-bold")
                if card.votes:
                    _verdict_chip(card.verdict)
                else:
                    ui.label("context").classes("text-xs").style("color: #a0aec0;")
            if card.unavailable is not None:
                ui.label(card.unavailable).classes("text-sm").style("color: #a0aec0;")
                return
            for line in card.lines:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.label(_STATUS_ICON[line.status]).style(
                        f"color: {_STATUS_COLOR[line.status]}; min-width: 1.1rem;")
                    ui.label(line.text).classes("text-sm")
            if card.votes and card.verdict is not None:
                ui.label(card.tally_text).classes("text-xs mt-1").style("color: #a0aec0;")
            if card.facts or card.tables:
                with ui.expansion("Show details").classes("w-full text-sm"):
                    if card.facts:
                        with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-1"):
                            for label, value in card.facts:
                                ui.label(label).classes("text-sm").style("color: #a0aec0;")
                                ui.label(value).classes("text-sm")
                    for table in card.tables:
                        ui.label(table.title).classes("text-sm font-bold mt-3")
                        columns = [{"name": f"c{i}", "label": c, "field": f"c{i}",
                                    "align": "left"} for i, c in enumerate(table.columns)]
                        rows = [{"id": n, **{f"c{i}": v for i, v in enumerate(r)}}
                                for n, r in enumerate(table.rows)]
                        ui.table(columns=columns, rows=rows, row_key="id",
                                 pagination={"rowsPerPage": 10}).classes("w-full").props("dense")

    def _render_advanced(self, results: Dict[str, Any]) -> None:
        """The previous per-expert view, kept for power use but out of the way.

        The expert breakdowns (with their scoring derivations) and the per-expert
        settings overrides both live here. Collapsed by default: the summary above is the
        page; this is for checking how an expert reached a number or re-running it with
        different settings.
        """
        analyst = results.get("analyst") or {}
        exports = [
            ("DeterministicScorer", results.get("detscorer")),
            ("Earnings / PEAD", results.get("earnings")),
            ("Insider Activity", results.get("insider")),
            ("FMP Rating", analyst.get("fmp")),
            ("FinnHub Rating", analyst.get("finnhub")),
        ]
        with ui.expansion("Advanced — expert breakdowns and settings").classes("w-full"):
            for title, export in exports:
                self._render_export_card(title, export)

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
                    if export.signal_unavailable_reason:
                        # No badge: the expert declared its signal is a fixed
                        # constant, not a verdict on this symbol (see
                        # EXPORT_SIGNAL_UNAVAILABLE_REASON). Say why, rather
                        # than leaving a silent gap where a badge used to be.
                        ui.label(f"no per-symbol signal — "
                                 f"{export.signal_unavailable_reason}").classes(
                            "text-xs").style("color: #a0aec0;")
            if export is None:
                ui.label("Failed to load").classes("text-sm text-red-400")
                return
            if export.error:
                ui.label(f"Error: {export.error}").classes("text-sm text-red-400")
                return
            if export.confidence is not None:
                ui.label(f"Confidence: {export.confidence:.1f}%").classes("text-xs").style(
                    "color: #a0aec0;")
            elif export.confidence_unavailable_reason:
                # The expert declared it never computes one (see
                # EXPORT_CONFIDENCE_UNAVAILABLE_REASON). Printing "0.0%" here --
                # what the placeholder in its Recommendation would produce --
                # asserts zero conviction, which it never said.
                ui.label(f"Confidence: n/a — {export.confidence_unavailable_reason}").classes(
                    "text-xs").style("color: #a0aec0;")
            for m in export.metrics:
                layout = plan_metric_detail(m)
                with ui.row().classes("w-full items-center gap-2"):
                    if m.signal:
                        ui.badge("", color=_SIGNAL_COLOR.get(m.signal, "grey")).classes("w-3 h-3 p-0")
                    ui.label(m.label).classes("text-sm font-medium").style("min-width: 12rem;")
                    ui.label(m.display).classes("text-sm")
                    if layout.mode == "tooltip":
                        # ui.icon(...).tooltip(text) gives a q-tooltip with NO width
                        # bound; nest an explicit ui.tooltip so the shared wrapping /
                        # max-width style can be applied to it.
                        with ui.icon("info", size="xs").classes("text-xs"):
                            ui.tooltip(layout.text).style(DETAIL_TOOLTIP_STYLE)
                if layout.mode == "panel":
                    self._render_detail_panel(layout)
            self._render_settings_expander(export)

    @staticmethod
    def _render_detail_panel(layout) -> None:
        """A metric's evidence, in a collapsed expander instead of a tooltip.

        A multi-step derivation is not tooltip content: hover text is HTML, so
        its newlines collapse into one unreadable line that cannot be scrolled,
        selected or copied (the FMPRating card's reported defect). Structured
        (label, value) evidence is drawn as a small two-column table; the
        step-by-step text keeps its line breaks via `white-space: pre-wrap`.
        Which detail lands here is decided by the pure `plan_metric_detail`."""
        with ui.expansion(layout.title).classes("w-full"):
            if layout.table:
                ui.table(
                    columns=[
                        {"name": "k", "label": "", "field": "k", "align": "left"},
                        {"name": "v", "label": "", "field": "v", "align": "right"},
                    ],
                    rows=[{"id": i, "k": k, "v": v} for i, (k, v) in enumerate(layout.table)],
                    row_key="id",
                ).props("dense flat hide-header").classes("w-full dark-pagination")
            if layout.text:
                ui.label(layout.text).classes("text-xs").style(
                    "white-space: pre-wrap; font-family: ui-monospace, monospace; "
                    "color: #cbd5e0;")

    def _render_settings_expander(self, export: ExpertDataExport) -> None:
        """Collapsed '⚙ Settings' expander pre-filled from settings_used,
        FILTERED to export.relevant_settings -- the expert's own curated
        allowlist of knobs that actually change what this card displays
        (see ExpertDataExportInterface.export_relevant_settings). This
        excludes both the ~50 generic base-class settings (enable_buy,
        risk_manager_model, sizing_mode, screener_*, ...) that were never
        card-relevant for any expert, and each expert's own execution/sizing
        knobs (e.g. FMPEarningsDrift's expected_profit_percent) that only
        shape a Recommendation field this card never renders. Dict/list-
        valued settings (universe configs, schedules, ...) are additionally
        skipped -- plumbing, not user-tunable, even if a future expert's
        allowlist accidentally included one. 'Save & Re-run' only PERSISTS
        the override (keyed by the real expert_name -- for the Analyst
        card's two sub-experts that means each is saved under its own name,
        not a nested blob) and prompts a fresh search; it does not
        live-refresh this one card in place (per the design doc, that's
        acceptable for this task)."""
        with ui.expansion("⚙ Settings").classes("w-full"):
            fields: Dict[str, Tuple[Any, type]] = {}
            for key, value in export.settings_used.items():
                if key not in export.relevant_settings:
                    continue
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
                # MERGE into whatever's already persisted, don't replace it wholesale:
                # `fields` only covers export.relevant_settings (the curated subset
                # rendered above), so a blind replace would silently drop any
                # previously-saved override on a key outside today's allowlist (e.g.
                # from before a curation change, or a future manual storage write)
                # every time this button is clicked -- turning a save into a partial
                # data loss with no warning.
                collected: Dict[str, Any] = dict(_get_overrides(export.expert_name))
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
