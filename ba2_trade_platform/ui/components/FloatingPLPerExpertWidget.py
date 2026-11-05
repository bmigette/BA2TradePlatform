"""
Floating P/L Per Expert Widget
Displays unrealized profit/loss for open positions grouped by expert.
"""
from ...logger import logger  # noqa: F401 -- tests capture this module's log lines through it
from .FloatingPLPerAccountWidget import _FloatingPLWidgetBase
from ...services.floating_pl import PerExpertPL


class FloatingPLPerExpertWidget(PerExpertPL, _FloatingPLWidgetBase):
    """Widget component showing floating profit/loss per expert."""

    _title = '📊 Floating P/L Per Expert'
