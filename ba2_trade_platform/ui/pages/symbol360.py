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
from typing import Any, Callable, Dict, List, Tuple

from nicegui import app, ui

from ...logger import logger

STORAGE_KEY = "symbol360_settings"   # app.storage.user[STORAGE_KEY][expert_name] = overrides


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
        self.progress_container = None
        self.cards_container = None
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
                ui.button("Search", on_click=self._search, icon="search").props("color=primary")

        self.progress_container = ui.column().classes("w-full gap-1 mb-4")
        self.cards_container = ui.column().classes("w-full gap-4")

    def _search(self) -> None:
        symbol = (self.symbol_input.value or "").strip().upper()
        if not symbol:
            ui.notify("Enter a symbol", type="warning")
            return
        asyncio.create_task(self._async_search(symbol))

    async def _async_search(self, symbol: str) -> None:
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
                    results[key] = result
                    status_labels[key].text = f"✅ {title}"
                except Exception as e:
                    logger.error(f"Symbol360: card '{key}' failed for {symbol}: {e}", exc_info=True)
                    results[key] = None
                    status_labels[key].text = f"❌ {title} (error)"
                finally:
                    done += 1
                    overall.value = done / len(cards)

            await asyncio.gather(*(_run(key, title, fn) for key, title, fn in cards))
            self._render_cards(symbol, results)

        except RuntimeError as e:
            # Handle client disconnection gracefully (user closed the tab/page mid-search)
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                logger.debug("[Symbol360Tab] Client disconnected during search")
            else:
                logger.error(f"Error in Symbol360 search: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error in Symbol360 search: {e}", exc_info=True)
            ui.notify(f"Error searching {symbol}: {str(e)}", type="negative")

    def _card_specs(self, symbol: str) -> List[Tuple[str, str, Callable[[str], Any]]]:
        raise NotImplementedError("filled in by Task 12")

    def _render_cards(self, symbol: str, results: Dict[str, Any]) -> None:
        raise NotImplementedError("filled in by Task 12")
