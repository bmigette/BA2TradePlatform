"""Host-provided hooks/accessors injected into the extracted packages at wiring
time (Phase 6 Task 4). These are *referenced* by ``core/seam_wiring.py`` (Task 7),
which installs them into the ``ba2_common`` / ``ba2_providers`` / ``ba2_experts``
seams once at startup.

Two seams are served here:

1. The **instrument auto-adder hook** (``auto_add_instruments_hook``). The extracted
   ``ba2_experts`` PennyMomentumTrader screening must NOT import the live
   ``InstrumentAutoAdder`` infra, so it calls an optional host hook
   (``ba2_experts.get_instrument_auto_adder_hook()``) with the screened symbols.
   The live host installs this wrapper, which queues those symbols into the live
   ``InstrumentAutoAdder`` background service. The package invokes the hook as
   ``hook(symbols)`` (a single positional ``list[str]`` -- confirmed against
   ``ba2_experts/PennyMomentumTrader/screening.py`` and the
   ``set_instrument_auto_adder_hook`` docstring ``fn(symbols: list[str])``), so this
   wrapper takes exactly that and supplies the live queue method's remaining
   arguments itself.

2. The **default indicator provider** (``get_default_indicator_provider``) for ATR
   fetches. ``ba2_common.core.position_sizing.get_latest_atr(symbol,
   indicator_provider, ...)`` needs a ``MarketIndicatorsInterface`` impl injected
   (``ba2_common`` never imports ``ba2_providers``). The classic risk manager
   ``ba2_common.core.TradeRiskManagement.TradeRiskManagement(indicator_provider=...)``
   threads this provider through its constructor (Phase 0 ATR pattern (a)); the
   wiring seeds the RM singleton with the provider returned here. The provider is a
   ``ba2_providers`` ``PandasIndicatorCalc``, which requires an ``ohlcv_provider`` --
   so it is built with a yfinance OHLCV source (a bare
   ``get_provider("indicators", "pandas")`` raises ``TypeError``: missing required
   ``ohlcv_provider``).
"""

from __future__ import annotations

from typing import Any, List

from ..logger import logger


def auto_add_instruments_hook(symbols: List[str]) -> None:
    """Live hook for ``ba2_experts.set_instrument_auto_adder_hook``.

    Queues ``symbols`` into the live ``InstrumentAutoAdder`` background service so
    package expert code (Penny screening) can register screened candidates without
    importing the live infra. ``ba2_experts`` calls this with a single positional
    ``list[str]``; the remaining ``queue_instruments_for_addition`` arguments
    (``expert_shortname`` / ``source`` / ``extra_labels``) are supplied here because
    the package hook carries only the symbols.

    Best-effort: any failure (service not running, queue closed) is logged and
    swallowed so a screening run never breaks on auto-add plumbing. ``InstrumentAutoAdder``
    is imported lazily to avoid import-time cycles during startup wiring.
    """
    if not symbols:
        return
    try:
        from .InstrumentAutoAdder import get_instrument_auto_adder

        adder = get_instrument_auto_adder()
        adder.queue_instruments_for_addition(
            symbols=symbols,
            expert_shortname="",
            source="expert",
            extra_labels=["auto_added"],
        )
    except Exception as e:
        logger.warning(f"auto_add_instruments_hook failed for {symbols}: {e}")


def get_default_indicator_provider() -> Any:
    """Return a ``ba2_providers`` indicator provider for ATR fetches.

    Built as a ``PandasIndicatorCalc`` (registry key ``("indicators", "pandas")``)
    backed by a yfinance OHLCV provider (``("ohlcv", "yfinance")``). The
    ``ohlcv_provider`` is required -- ``PandasIndicatorCalc.__init__`` takes it as a
    positional argument, so a bare ``get_provider("indicators", "pandas")`` raises
    ``TypeError``.

    Used by the wiring to seed the classic risk manager's ``indicator_provider`` so
    ``position_sizing.get_latest_atr`` resolves ATR through ``ba2_providers`` without
    ``ba2_common`` ever importing it.
    """
    from ba2_providers import get_provider

    ohlcv_provider = get_provider("ohlcv", "yfinance")
    return get_provider("indicators", "pandas", ohlcv_provider=ohlcv_provider)
