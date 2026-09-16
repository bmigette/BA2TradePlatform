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

import logging
import threading
from typing import Any, Dict, List, Optional

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

    Registrations are THREAD-LOCAL: the resolver is a process-wide singleton (registered
    once into ba2_common), but every backtest registers the SAME ids (account_id=1 /
    expert_id=1). When the serve runs re-runs CONCURRENTLY in worker threads, a process-global
    registry would let run B's BacktestAccount overwrite run A's under id=1 — so run A's
    inherited code resolves run B's account (its balance/positions) and the two runs corrupt
    each other (garbage / negative-equity results). Keying the registry by thread isolates
    concurrent runs; sequential trials in one thread are unaffected (each re-registers id=1).
    """

    def __init__(self) -> None:
        self._tl = threading.local()

    def _accounts_map(self) -> Dict[int, Any]:
        m = getattr(self._tl, "accounts", None)
        if m is None:
            m = self._tl.accounts = {}
        return m

    def _experts_map(self) -> Dict[int, Any]:
        m = getattr(self._tl, "experts", None)
        if m is None:
            m = self._tl.experts = {}
        return m

    @property
    def _accounts(self) -> Dict[int, Any]:
        return self._accounts_map()

    @property
    def _experts(self) -> Dict[int, Any]:
        return self._experts_map()

    # -- registration (host fills these in before driving the loop) -------------
    def register_account(self, account_id: int, instance: Any) -> None:
        self._accounts_map()[int(account_id)] = instance

    def register_expert(self, expert_id: int, instance: Any) -> None:
        self._experts_map()[int(expert_id)] = instance

    def unregister_account(self, account_id: int) -> None:
        """Drop a registration. A run/test that is finished must not leave its account
        resolvable: the registry is per THREAD, not per run, so a later run on the same
        thread would otherwise still find a dead object under an id it never registered.
        Silent on an id that is not registered -- teardown must be idempotent."""
        self._accounts_map().pop(int(account_id), None)

    def unregister_expert(self, expert_id: int) -> None:
        """The expert twin of ``unregister_account``; same reason, same idempotence."""
        self._experts_map().pop(int(expert_id), None)

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


# Per-run OHLCV provider override (THREAD-LOCAL; set by run_daily_backtest at the start of
# each trial and cleared at the end). When set, the TradeConditions provider resolver returns
# THIS provider for any ("ohlcv", *) request — so the expert's price_at_date / data-condition
# OHLCV fetches go through the run's MemoizedOHLCVProvider (one in-memory load per worker,
# shared across the whole GA population) instead of re-reading the disk cache every bar.
#
# Thread-local, NOT a process-global: the serve runs re-runs CONCURRENTLY in worker threads, and
# a single global override let run B's MemoizedOHLCVProvider clobber run A's mid-run — so run A's
# expert read run B's prices (wrong fills, negative-equity garbage). Per-thread isolates them;
# sequential trials in one worker thread are unaffected (set at start, cleared at end).
_ohlcv_override_tl = threading.local()


def _current_ohlcv_override() -> Optional[Any]:
    return getattr(_ohlcv_override_tl, "provider", None)


def set_backtest_ohlcv_override(provider: Optional[Any]) -> None:
    """Install (or clear, with None) the per-run OHLCV provider the resolver hands experts.
    Thread-local — only affects the calling thread's backtest."""
    _ohlcv_override_tl.provider = provider


# Market-condition entry gates (design 2026-09-15 section 4.1). OPT-IN per run: only a config
# whose ``market_condition_profile`` is not ``"none"`` installs anything, and only then is the
# adapter module imported. The TradeConditions seam is process-global while backtests run
# CONCURRENTLY in worker threads (see the OHLCV override above), so the process gets ONE
# dispatching resolver and each run's resolver lives in THIS thread's slot; a thread without a
# run resolver resolves None (the gate reports ``no_context``).
MARKET_CONDITION_PROFILE_NONE = "none"
_market_condition_tl = threading.local()
_log = logging.getLogger(__name__)


def _current_market_condition_resolver() -> Optional[Any]:
    return getattr(_market_condition_tl, "resolver", None)


def _dispatch_market_condition_context(account: Any, instrument_name: str,
                                       expert_recommendation: Any) -> Optional[Any]:
    resolver = _current_market_condition_resolver()
    if resolver is None:
        return None
    return resolver(account, instrument_name, expert_recommendation)


def install_backtest_market_conditions(config: Dict[str, Any], price_source: Any) -> Optional[Any]:
    """Install this thread's market-condition resolver for one run, or nothing.

    ``config["market_condition_profile"]`` is required (``run_daily_backtest`` defaults it to
    ``"none"``). ``"none"`` returns None without importing the adapter or touching the seam; a
    registered profile builds the run's reader over ``price_source`` and returns the resolver;
    anything else raises.

    ``config["market_condition_manifest"]`` pins the published snapshot: present, the reader is a
    mapped-store reader (no calculation anywhere in the run); absent, it computes on a miss and
    warns once -- unless the config is an optimizer trial, which is refused (see below).
    """
    profile = config["market_condition_profile"]
    if profile == MARKET_CONDITION_PROFILE_NONE:
        # A run with market leaves but no profile would evaluate every gate as no_context and
        # place ZERO entries without a word (e.g. a trial config that dropped the key after an
        # earlier gated run installed the dispatcher in this process). Refuse it instead.
        leaves = market_condition_leaves_in(config)
        if leaves:
            raise ValueError(
                f"market_condition_profile is 'none' but the run's rules contain market-condition "
                f"leaves {leaves!r}: every such gate would be unknown and never pass. Set the "
                f"profile the rules were built for.")
        _market_condition_tl.resolver = None
        return None
    from ba2_common.core import TradeConditions
    from ba2_common.core.market_condition_readers import warn_research_mode
    from ba2_common.core.market_conditions import PROFILES

    if profile not in PROFILES:
        raise ValueError(f"market_condition_profile {profile!r} is not registered "
                         f"(known: {sorted(PROFILES)!r} or {MARKET_CONDITION_PROFILE_NONE!r})")
    from app.services.backtest.market_condition_bt import (
        BacktestMarketConditionReader,
        BacktestMarketConditionResolver,
    )

    # THE PINNED SNAPSHOT (design section 4.5). A search prepares ONE manifest before dispatch and
    # carries its digest in every trial config; the reader then serves that snapshot's published
    # rows and calculates nothing. A config WITHOUT the digest can only compute on a miss, which
    # is fine for a one-off research run and is not fine inside an optimization: the numbers would
    # come from whatever each worker's own cache happened to hold, no two hosts provably agreeing,
    # and nothing anywhere would say so. So an optimizer-assembled config (``_ga_trial``: GA trial,
    # re-run, robustness variant and top-N persist alike -- see
    # strategy_optimization_handler._build_daily_trial_config) is REFUSED here instead.
    digest = config.get("market_condition_manifest")
    if not digest:
        if config.get("_ga_trial"):
            raise ValueError(
                f"market_condition_profile {profile!r} is on but the trial config pins no "
                f"market_condition_manifest. An optimization must prepare one snapshot "
                f"(tools/warm_market_conditions.py plan/build/verify/prepare-host) and carry its "
                f"digest into every trial; computing 128-session indicators per trial is not a "
                f"fallback this path takes.")
        warn_research_mode(profile, "backtest reader")
    reader = BacktestMarketConditionReader(price_source, profile, manifest_digest=digest)
    check_market_condition_coverage(config, reader)
    resolver = BacktestMarketConditionResolver(reader)
    if TradeConditions.get_market_condition_context_resolver() is not _dispatch_market_condition_context:
        TradeConditions.set_market_condition_context_resolver(_dispatch_market_condition_context)
    _market_condition_tl.resolver = resolver
    return resolver


#: How many missing symbols a message names before it says "and N more".
_COVERAGE_NAMES = 20


def market_condition_universe(config: Any) -> List[str]:
    """The symbols this run can ever evaluate a market-condition gate for.

    ``enabled_instruments`` is the right list even for a screener run: the screener gates ENTRIES
    to a per-day subset of it (and the optimizer's ``screener_candidate`` has already narrowed it
    to what this trial's screen can ever select), so it is the superset a gate can be asked about.
    """
    return sorted({str(s).upper() for s in (config.get("enabled_instruments") or ())})


def check_market_condition_coverage(config: Dict[str, Any], reader: Any) -> List[str]:
    """Refuse (or, in research mode, report) a run whose universe the snapshot does not cover.

    THE FAILURE THIS PREVENTS. A pinned manifest covers exactly the symbols the warmup could warm
    -- the first real one covers 85 of the 98-symbol option universe, because 13 carry a split
    whose basis the prices cannot settle and need a full provider re-fetch first. For a symbol the
    manifest omits, every gate reads ``missing_session`` for the whole run: it never enters, and
    the genome that would have traded it scores as though its strategy simply did not fire there.
    A feature-cache miss must not become a property of the fitness landscape.

    A GA trial RAISES (the job fails, which is what an environment fault deserves); research mode
    logs one ERROR naming the symbols and continues, because a one-off run over a wider universe
    than the snapshot is a legitimate thing to do deliberately. Returns the missing symbols.
    """
    from ba2_common.core.market_condition_reader import coverage_detail, missing_coverage

    mapped = getattr(reader, "mapped_reader", None)
    if mapped is None:
        return []                      # research mode without a manifest: nothing to compare to
    universe = market_condition_universe(config)
    missing = missing_coverage(mapped, universe)
    if not missing:
        return []
    shown = ", ".join(missing[:_COVERAGE_NAMES])
    if len(missing) > _COVERAGE_NAMES:
        shown += f", and {len(missing) - _COVERAGE_NAMES} more"
    detail = coverage_detail(mapped, missing)
    message = (
        f"market-condition manifest {mapped.manifest_digest} does not cover "
        f"{len(missing)} of this run's {len(universe)} instruments: {shown}{detail}. "
        f"Every gate on those symbols would read missing_session for the whole run, so they "
        f"would silently never enter. Warm and re-publish the snapshot for the full universe "
        f"(tools/warm_market_conditions.py plan/build), or run the narrower universe.")
    if config.get("_ga_trial"):
        raise ValueError(message)
    _log.error(message)
    return missing


def market_condition_leaves_in(config: Any) -> List[str]:
    """Ids (or config paths, for a leaf without an id) of every condition leaf anywhere in
    ``config`` whose ``field`` is a registered market-condition field. Walks dicts and lists, and
    JSON-encoded rule trees held as strings (expert settings store trees that way)."""
    import json

    from ba2_common.core.market_conditions import PROFILES

    fields = {f.name for prof in PROFILES.values() for f in prof.fields}
    hits: List[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            field = node.get("field")
            if isinstance(field, str) and field in fields:
                hits.append(str(node["id"]) if node.get("id") else path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str) and node[:1] in ("{", "[") and any(f in node for f in fields):
            try:
                decoded = json.loads(node)
            except ValueError:
                hits.append(path)  # names a market field but is not a parseable tree: refuse too
                return
            walk(decoded, path)

    walk(config, "config")
    return hits


def clear_backtest_market_conditions() -> None:
    """Drop this thread's run resolver (idempotent). The dispatcher stays installed: another
    thread's run may still be using it, and with no slot set it resolves None."""
    _market_condition_tl.resolver = None


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
        override = _current_ohlcv_override()
        if category == "ohlcv" and override is not None:
            return override
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


class MetricStoreATRProvider:
    """ATR-only indicator provider that reads PRECOMPUTED ``atr_<period>`` columns from the
    screener metric store — fully offline (no network, no live OHLCV fetch), so it is hermetic
    and safe for the GA/optimize trial-worker path, which has no route to a live/as-of-clamped
    indicator provider (unlike the single-backtest handler's ``AsOfClampedOHLCVProvider`` path).

    Only understands ``indicator="atr"`` with a ``period`` in the store's precomputed
    ``metric_store.ATR_PERIODS``; anything else returns no values (the caller,
    ``position_sizing.get_latest_atr``, then logs "no ATR value returned" and the safeguard falls
    back to its risk%-only floor — the SAME safe behaviour as today, just narrower in scope).

    ``end_date`` MUST be the backtest's SIMULATED as-of date (threaded from
    ``TradeRiskManagement(as_of=...)`` -> ``get_latest_atr(end_date=...)``) — using wall-clock
    would resolve to the store's MOST RECENT row regardless of the historical bar being sized, a
    lookahead bug. The store is loaded via ``metric_store.load_store`` (memoised per worker), so
    repeated construction is cheap.
    """

    def __init__(self, store_dir: str):
        self._store_dir = store_dir

    def get_indicator(self, symbol: str, indicator: str, start_date: Any = None,
                      end_date: Any = None, lookback_days: Any = None, interval: str = "1d",
                      format_type: str = "dict", period: Any = None) -> Dict[str, Any]:
        from ba2_providers.screener import metric_store as ms

        empty = {"values": [], "dates": [], "symbol": symbol.upper(), "indicator": indicator}
        if indicator != "atr" or not period or int(period) not in ms.ATR_PERIODS:
            return empty
        if end_date is None:
            return empty  # never fall back to wall-clock here — see class docstring
        col = f"atr_{int(period)}"
        try:
            df = ms.load_store(self._store_dir)
            day = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)[:10]
            rows = ms.metrics_as_of(df, day, [col])
        except Exception:  # noqa: BLE001 — any store issue -> safe empty (caller's no-ATR fallback)
            return empty
        row = rows.get(symbol.upper()) or rows.get(symbol)
        if not row:
            return empty
        val = row.get(col)
        try:
            import math
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return empty
            return {"values": [float(val)], "dates": [day], "symbol": symbol.upper(),
                   "indicator": indicator}
        except (TypeError, ValueError):
            return empty


def make_atr_cache_indicator_provider(screener_store: Optional[str]) -> Optional[Any]:
    """``MetricStoreATRProvider`` bound to ``screener_store``, or ``None`` when no store is
    configured for this run (caller falls back to ``make_indicator_provider()``, the existing
    — currently non-hermetic in the GA path — live provider)."""
    if not screener_store:
        return None
    return MetricStoreATRProvider(screener_store)
