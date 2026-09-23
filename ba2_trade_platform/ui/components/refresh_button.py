"""The one Refresh button.

The same action was drawn seven different ways across the app: an emoji "🔄 Refresh"
in flat primary (Overview), outline with an icon (Live Trades, Option Trades,
Portfolio Allocation), FILLED teal (Market Analysis job tabs), flat dense (two Market
Analysis card headers), outline with NO icon (Trade Recommendations, the income
panel), "Refresh Now" (Activity Monitor), and `props('outlined')` on two Settings
buttons -- a QInput prop that QBtn ignores, so those rendered filled while meant as
outline. A reader moving between pages could not learn what Refresh looks like, and
three different buttons side by side on Trade Recommendations were three different
heights.

Outline with the refresh icon is the style most of them already had, and the one that
reads as a secondary action: it re-reads data, it changes nothing. Every page's
primary actions stay filled, so Refresh never competes with them.
"""
from typing import Callable, Optional

from nicegui import ui


def refresh_button(on_click: Callable, *, label: str = 'Refresh', dense: bool = False,
                   tooltip: Optional[str] = None) -> ui.button:
    """Draw the Refresh button. ``dense`` for a card header, where a full-height button
    would stand taller than the title beside it.

    ``label`` is for the few refreshes that re-read something specific ("Refresh
    Statistics") -- the look stays the same, only the words say what is re-read.
    """
    button = ui.button(label, icon='refresh', on_click=on_click)
    button.props('outline dense' if dense else 'outline')
    if tooltip:
        button.tooltip(tooltip)
    return button
