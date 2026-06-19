"""Host-side wiring of the ba2_common / ba2_experts seams for the backtest engine.

Phase 0 defined the seams (instance resolver, LLM service, TradeConditions provider
resolver, ATR indicator injection) but left them unconfigured; the live
BA2TradePlatform wires them in Phase 6. BA2TestPlatform wires its OWN
(backtest-flavoured) versions here so the inherited AccountInterface / expert /
TradeConditions / TradeRiskManagement code can resolve instances, a (loud, unused)
LLM service, and providers, all against the backtest cache.

Confirmed against the installed Phase-0 packages (NOT the plan's draft guesses):
  * ba2_common.core.instance_resolver.set_instance_resolver / get_instance_resolver,
    InstanceResolver Protocol = {get_expert_instance, get_account_instance,
    get_account_instance_from_transaction}.
  * ba2_common.core.interfaces.LLMServiceInterface.set_llm_service, LLMServiceInterface
    (ABC with create_llm + do_llm_call_with_websearch), LLMServiceNotConfigured.
  * ba2_common.core.TradeConditions.set_provider_resolver(fn) with fn(category, name, **kw).
  * ba2_providers.get_provider(category, name, **kwargs); the indicators/"pandas"
    provider (PandasIndicatorCalc) REQUIRES an ohlcv_provider in its constructor, so
    make_indicator_provider() builds it with the ohlcv/"fmp" provider (the no-arg call
    would raise TypeError -> get_provider silently falls back to a broken default).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ba2_common.core.instance_resolver import (
    set_instance_resolver,
    InstanceResolver,  # noqa: F401  (exported for typing / Protocol checks)
)
from ba2_common.core.interfaces.LLMServiceInterface import (
    set_llm_service,
    LLMServiceInterface,
    LLMServiceNotConfigured,
)


class BacktestInstanceResolver:
    """Resolves expert / account ids to live instances for the backtest run.

    Satisfies the ba2_common ``InstanceResolver`` Protocol. The backtest engine
    constructs the ``BacktestAccount`` + expert instances and registers them here so
    the inherited AccountInterface / TradeManager-equivalent code (which calls
    ``get_instance_resolver().get_account_instance(...)`` etc.) finds them.
    """

    def __init__(self) -> None:
        self._accounts: Dict[int, Any] = {}
        self._experts: Dict[int, Any] = {}

    # -- registration (host fills these in before driving the loop) -------------
    def register_account(self, account_id: int, instance: Any) -> None:
        self._accounts[int(account_id)] = instance

    def register_expert(self, expert_id: int, instance: Any) -> None:
        self._experts[int(expert_id)] = instance

    # -- InstanceResolver Protocol ----------------------------------------------
    def get_account_instance(self, account_id: int) -> Any:
        try:
            return self._accounts[int(account_id)]
        except KeyError:
            raise KeyError(
                f"BacktestInstanceResolver: no account registered for id={account_id}. "
                f"Registered accounts: {sorted(self._accounts)}"
            )

    def get_expert_instance(self, expert_id: int) -> Any:
        try:
            return self._experts[int(expert_id)]
        except KeyError:
            raise KeyError(
                f"BacktestInstanceResolver: no expert registered for id={expert_id}. "
                f"Registered experts: {sorted(self._experts)}"
            )

    def get_account_instance_from_transaction(self, transaction: Any) -> Any:
        return self.get_account_instance(transaction.account_id)


class _NoLLMService(LLMServiceInterface):
    """The clean experts (FMPEarningsDrift / FMPInsiderClusterBuy) never call an LLM.

    Configure a loud-failing service so any *accidental* LLM call during a backtest is
    caught immediately rather than silently producing a (lookahead-prone) response.
    """

    def create_llm(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured(
            "Backtest engine does not provide an LLM service "
            "(clean experts must not call LLMs)."
        )

    def do_llm_call_with_websearch(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured(
            "Backtest engine does not provide an LLM service "
            "(clean experts must not call LLMs)."
        )


_resolver: Optional[BacktestInstanceResolver] = None


def get_backtest_resolver() -> BacktestInstanceResolver:
    """Return the process-wide backtest instance resolver.

    Raises if ``wire_backtest_seams()`` has not been called yet (loud, not silent).
    """
    if _resolver is None:
        raise RuntimeError(
            "seam wiring not initialised; call wire_backtest_seams() first"
        )
    return _resolver


def wire_backtest_seams() -> BacktestInstanceResolver:
    """Install the resolver + LLM service + TradeConditions provider resolver once.

    Idempotent per process: repeated calls return the SAME resolver and do NOT
    re-register the seams. Returns the resolver so the caller can register the
    BacktestAccount / expert instances on it.
    """
    global _resolver
    if _resolver is None:
        _resolver = BacktestInstanceResolver()
        set_instance_resolver(_resolver)  # ba2_common instance-resolution seam
        set_llm_service(_NoLLMService())  # ba2_common LLM-service seam
        _wire_provider_resolver()         # TradeConditions data-access seam
    return _resolver


# Per-run OHLCV provider override (process-global; set by run_daily_backtest at the start of
# each trial and cleared at the end). When set, the TradeConditions provider resolver returns
# THIS provider for any ("ohlcv", *) request — so the expert's price_at_date / data-condition
# OHLCV fetches go through the run's MemoizedOHLCVProvider (one in-memory load per worker,
# shared across the whole GA population) instead of re-reading the disk cache every bar. Trials
# run sequentially within a worker process, so a single global is safe.
_ohlcv_override: Optional[Any] = None


def set_backtest_ohlcv_override(provider: Optional[Any]) -> None:
    """Install (or clear, with None) the per-run OHLCV provider the resolver hands experts."""
    global _ohlcv_override
    _ohlcv_override = provider


def _wire_provider_resolver() -> None:
    """Route ``TradeConditions`` data fetches through ba2_providers.get_provider.

    Phase 0 severed the ba2_common -> ba2_providers edge; data-driven conditions now
    resolve a provider through this host-injected resolver. The signature matches
    ba2_providers.get_provider exactly: fn(category, name, **kwargs). When a per-run OHLCV
    override is set, ("ohlcv", *) resolves to it (the memoized in-memory provider).
    """
    from ba2_common.core import TradeConditions
    from ba2_providers import get_provider  # ba2_providers is allowed here (host side)

    def _resolve(category: str, name: str, **kwargs: Any) -> Any:
        if category == "ohlcv" and _ohlcv_override is not None:
            return _ohlcv_override
        return get_provider(category, name, **kwargs)

    TradeConditions.set_provider_resolver(_resolve)


def make_indicator_provider(ohlcv_provider: Any = None) -> Any:
    """Build the indicator provider injected into ``TradeRiskManagement`` / ATR sizing.

    Phase 0 made ``position_sizing.get_latest_atr(symbol, indicator_provider, ...)`` and
    ``TradeRiskManagement(indicator_provider=...)`` take an *injected* provider so
    ba2_common never imports ba2_providers. The indicators/"pandas" provider
    (PandasIndicatorCalc) REQUIRES an OHLCV provider in its constructor, so we build it
    from the ohlcv/"fmp" provider here.

    Args:
        ohlcv_provider: the OHLCV provider to back the indicator calc. If omitted, the
            default ohlcv/"fmp" provider is constructed. The backtest engine passes its
            as_of-aware OHLCV provider so ATR is computed against the backtest cache.
    """
    from ba2_providers import get_provider

    if ohlcv_provider is None:
        ohlcv_provider = get_provider("ohlcv", "fmp")
    return get_provider("indicators", "pandas", ohlcv_provider=ohlcv_provider)
