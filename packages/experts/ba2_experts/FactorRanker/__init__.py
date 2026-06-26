"""FactorRanker — configurable cross-sectional multi-factor equity expert.

Ranks a candidate universe each rebalance by a weighted blend of momentum,
post-earnings-drift, value and quality factors, holds the long-only top slice,
and rebalances via :class:`FactorPortfolioManager`. It runs as a single batch
job (``run_analysis("EXPERT", ma)``) and writes a ``MarketAnalysis`` audit trail —
no ``ExpertRecommendation`` records, no ``SmartRiskManager``.
"""

import inspect
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.core.models import AnalysisOutput, MarketAnalysis
from ba2_providers.StockScreener import StockScreener
from ba2_common.core.types import MarketAnalysisStatus, OrderRecommendation, Recommendation
from ba2_common.logger import get_expert_logger

from ba2_experts.FactorRanker import data
from ba2_experts.FactorRanker.construction import long_only_top_n
from ba2_experts.FactorRanker.factors import (
    composite_score, cross_sectional_zscore, earnings_surprise, momentum_12_1,
    quality_score, rank_symbols, value_score,
)
from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager


# Map factor name -> (data fetcher attribute name, pure calculator). The fetcher is
# resolved off the `data` module at call time (not captured here) so it stays
# patchable in tests. Adding a factor is a one-line change.
_FACTOR_PIPELINE = {
    "momentum": ("fetch_close_prices", momentum_12_1),
    "value": ("fetch_value_inputs", value_score),
    "quality": ("fetch_quality_inputs", quality_score),
    "pead": ("fetch_pead_inputs", earnings_surprise),
}


def _accepts_kwarg(fn, name: str) -> bool:
    """True iff ``fn`` accepts ``name`` as a keyword argument (declared param or **kwargs).

    Used so the OHLCV-provider speedup is threaded ONLY to fetchers that accept it,
    leaving fundamentals-only fetchers (and any patched/mock fetcher without the kwarg)
    called exactly as before. Best-effort: a non-introspectable callable returns False.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == name and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


class FactorRanker(MarketExpertInterface):
    """Configurable cross-sectional multi-factor equity ranker."""

    # Backtest warmup window (in trading BARS) the engine preloads before the first
    # trading bar. The backtest now serves FactorRanker's daily OHLCV from the in-memory
    # MemoizedOHLCVProvider, whose window is bounded by [start - warmup, end]. The momentum
    # fetcher requests ``lookback_days=400`` CALENDAR days; for the memoized closes to be a
    # superset of that direct-provider window (so momentum_12_1 — which indexes from the END,
    # iloc[-252]/iloc[-22] — is RESULTS-IDENTICAL), the derived calendar warmup must be
    # >= ~400 days. ``daily_backtest_handler.derive_warmup_days`` converts bars -> calendar
    # days as ``int(bars * 1.45) + 10`` (floored at 60). 300 bars -> int(300*1.45)+10 = 445
    # calendar days (>= the 400d momentum window, with margin), and ~307 trading bars before
    # the first analysis bar (>= the 252-bar momentum lookback). The default table value (252
    # bars -> 375 days) was < 400 and would have truncated the memoized momentum window at
    # early bars, changing results. The handler reads this via getattr(cls, ...), so it lives
    # here in BA2TradeExperts.
    BACKTEST_WARMUP_BARS: int = 300

    # BYPASS marker (piece 1a): FactorRanker does NOT use the classic risk manager
    # or the shared enter/exit ruleset. It emits {symbol: weight} target weights in
    # analyze_as_of(...).raw_outputs["targets"] and rebalances via its own
    # FactorPortfolioManager. The engine reads this flag and routes those targets
    # directly to the portfolio manager (skipping TradeActionEvaluator /
    # TradeRiskManagement / position_sizing); the optimizer drops rm:*/cond:/exit:/tp/sl.
    bypasses_classic_rm: bool = True

    @classmethod
    def description(cls) -> str:
        return ("Configurable cross-sectional multi-factor equity ranker "
                "(momentum / value / quality / PEAD)")

    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        return {
            # Selects + ranks its own universe in one batch run, and handles both
            # entries and exits there, so no per-symbol jobs and no separate
            # open-positions schedule.
            "can_recommend_instruments": True,
            "should_expand_instrument_jobs": False,
            "required_instrument_selection_method": "expert",
            "schedules_open_positions": False,
            # Executes via its own portfolio manager, not the SmartRiskManager.
            "uses_risk_manager": False,
        }

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "universe_source": {
                "type": "str", "required": False, "default": "static",
                "choices": ["static", "screener"],
                "description": "Candidate universe: 'static' (enabled_instruments) or 'screener' (StockScreener filters).",
            },
            # One float per factor (0 disables it). Kept as separate scalar settings
            # rather than one JSON blob so each renders as a simple number field.
            "factor_weight_momentum": {
                "type": "float", "required": False, "default": 1.0,
                "description": "Momentum factor weight (0 disables it).",
            },
            "factor_weight_value": {
                "type": "float", "required": False, "default": 1.0,
                "description": "Value factor weight (0 disables it).",
            },
            "factor_weight_quality": {
                "type": "float", "required": False, "default": 1.0,
                "description": "Quality factor weight (0 disables it).",
            },
            "factor_weight_pead": {
                "type": "float", "required": False, "default": 0.0,
                "description": "Post-earnings-drift (PEAD) factor weight (0 disables it).",
            },
            "top_n": {
                "type": "int", "required": False, "default": 20,
                "description": "Number of top-ranked names to hold.",
            },
            "weighting": {
                "type": "str", "required": False, "default": "equal",
                "choices": ["equal", "score"],
                "description": "Position weighting: equal (1/N) or score-proportional.",
            },
            "max_weight_per_name": {
                "type": "float", "required": False, "default": 0.10,
                "description": "Maximum portfolio weight per holding (0-1).",
            },
            "gross_exposure": {
                "type": "float", "required": False, "default": 1.0,
                "description": "Total gross exposure to deploy across the book (1.0 = fully invested).",
            },
            "winsorize_pct": {
                "type": "float", "required": False, "default": 0.02,
                "description": "Winsorize each factor's tails at this fraction before z-scoring.",
            },
            "sector_neutralize": {
                "type": "bool", "required": False, "default": False,
                "description": "Sector-neutralize factor scores (reserved; not applied in v1).",
            },
            "pead_drift_window_days": {
                "type": "int", "required": False, "default": 60,
                "description": "Post-earnings drift window (days) for the PEAD factor.",
            },
            "min_price": {
                "type": "float", "required": False, "default": 0.0,
                "description": "Minimum share price liquidity guard (0 disables).",
            },
            "min_dollar_volume": {
                "type": "float", "required": False, "default": 0.0,
                "description": "Minimum average dollar volume guard (reserved; not applied in v1).",
            },
            "hard_stop_pct": {
                "type": "float", "required": False, "default": 0.0,
                "description": "Optional per-name hard stop between rebalances (0 disables).",
            },
            "screener_store": {
                "type": "str", "required": False, "default": "",
                "description": "Path to a prebuilt screener metric store (parquet dir, built via ba2-test build-screener-metrics). When set with universe_source=screener, FactorRanker resolves its candidate universe from the fast metric_store (survivorship-biased: current tradable names) instead of the slower survivorship-free StockScreener.",
            },
        }

    def __init__(self, id: int):
        super().__init__(id)
        self.logger = get_expert_logger("FactorRanker", id)

    # ------------------------------------------------------------------
    # Analysis pipeline
    # ------------------------------------------------------------------

    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Thin live orchestrator: resolve settings -> _gather(as_of=None) ->
        _process -> rebalance + persist state/output. Runs the EXACT same
        _gather/_process the backtest engine drives via analyze_as_of; with
        as_of=None the data fetch + ranking are byte-identical to the pre-refactor
        live behaviour. ``symbol`` is the "EXPERT" batch marker."""
        self.logger.info(f"FactorRanker analysis starting (analysis {market_analysis.id}, symbol={symbol})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            # Resolve settings (weights/winsorize/top_n/weighting/max_weight/gross/
            # pead window) into a plain dict; _gather + _process read only this dict.
            settings = self._resolve_factor_settings()
            self._gather_settings = settings

            # Gather (universe + factors(as_of=None) + holdings + prices) then
            # process (pure ranking -> target weights). The held set used by the
            # book is captured in _gather BEFORE the rebalance, matching the
            # pre-refactor ordering.
            bundle = self._gather(self._live_providers(), as_of=None)
            rec = self._process(bundle, settings, as_of=None)

            if rec.skip:
                self._mark_skipped(market_analysis, rec.skip_reason)
                return

            targets = rec.raw_outputs["targets"]
            book = rec.raw_outputs["book"]

            # The rebalance is LIVE-ONLY; in backtest (Phase 4) analyze_as_of returns
            # the targets and the engine routes them to submit_order.
            FactorPortfolioManager(self.id).rebalance(targets)

            market_analysis.state = {"factor_ranker": book}
            self._write_output(market_analysis, "Ranked book", "factor_ranking", book)
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            self.logger.info(
                f"FactorRanker analysis complete: ranked {book['universe_size']} names, "
                f"holding {len(targets)} (analysis {market_analysis.id})"
            )

        except Exception as e:
            self.logger.error(f"FactorRanker analysis failed: {e}", exc_info=True)
            market_analysis.state = {
                "factor_ranker": {"error": str(e), "failed": True}
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            self._write_output(market_analysis, "Analysis error", "error", {"error": str(e)})
            raise

    # ------------------------------------------------------------------
    # Pipeline helpers
    # ------------------------------------------------------------------

    def _metric_store_settings(self) -> Dict[str, Any]:
        """Translate this expert's ``screener_*``-prefixed settings into the UNPREFIXED
        keys the fast metric_store expects.

        The base-interface screener settings are ``screener_*``-prefixed, but
        ``ba2_providers.screener.metric_store.screen_universe_as_of`` reads unprefixed
        keys (e.g. ``market_cap_min``). A translator is therefore mandatory — passing the
        raw ``screener_*`` keys would match nothing in the store's ``settings.get(...)``
        lookups (so every filter is a no-op and the WHOLE universe is selected).

        metric_store does NOT support ``screener_float_*`` / ``screener_price_drop_days`` /
        ``screener_universe_mode`` / ``screener_provider`` — that's the documented
        consolidation tradeoff (the store is precomputed from current tradable names with a
        fixed drop window), so those are intentionally not passed.
        """
        g = self.get_setting_with_interface_default
        return {
            "market_cap_min": g("screener_market_cap_min"),
            "market_cap_max": g("screener_market_cap_max"),
            "price_min": g("screener_price_min"),
            "price_max": g("screener_price_max"),
            "volume_min": g("screener_volume_min"),
            "volume_max": g("screener_volume_max"),
            "relative_volume_min": g("screener_relative_volume_min"),
            "price_drop_pct": g("screener_price_drop_pct"),
            "max_stocks": g("screener_max_stocks"),
            "sort_metric": g("screener_sort_metric"),
            "weinstein_stage2_only": g("screener_weinstein_stage2_only"),
        }

    def _screen_universe(self, as_of: Optional[datetime] = None) -> List[str]:
        """Resolve the candidate universe.

        OPT-IN fast path: when the ``screener_store`` setting points at a prebuilt
        metric store (parquet dir), resolve the candidate universe from the in-memory
        ``ba2_providers.screener.metric_store`` (~microseconds/scan) instead of the slow
        ``StockScreener`` (~minutes/scan). The store is survivorship-biased (current
        tradable names); the StockScreener path is survivorship-free. Any failure
        (missing store, load/screen error) logs a warning and FALLS BACK to StockScreener,
        so a misconfigured store degrades gracefully rather than crashing the rebalance.

        Default (``screener_store`` unset): runs the configured ``StockScreener``,
        reading the expert's ``screener_*`` settings (part of the base interface) and
        returning the matched symbols (uppercased). Failures degrade to an empty
        universe rather than raising, so one bad screen doesn't crash the rebalance.

        ``as_of`` is threaded into both paths so the BACKTEST screens the point-in-time
        universe for that date (the store resolves to the latest scan date <= as_of;
        StockScreener screens the survivorship-free universe); ``None`` (live) screens
        today's universe as before. Live runs have no prebuilt store, so the default
        StockScreener path keeps working unchanged.
        """
        store = (self.get_setting_with_interface_default("screener_store") or "").strip()
        if store:
            try:
                from ba2_providers.screener import metric_store as ms  # local import (opt-in)
                df = ms.load_store(store)
                day = (as_of or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
                syms = ms.screen_universe_as_of(df, day, self._metric_store_settings())
                syms = [s.upper() for s in syms]
                self.logger.info(
                    f"FactorRanker: metric_store returned {len(syms)} candidates "
                    f"(store={store}, as_of={day})"
                )
                return syms
            except Exception as e:
                self.logger.warning(
                    f"FactorRanker: metric_store universe resolution failed ({e}); "
                    f"falling back to StockScreener", exc_info=True)
                # fall through to the StockScreener path below

        try:
            result = StockScreener(dict(self.settings), as_of=as_of).screen()
            syms = [r["symbol"].upper() for r in (result.get("results") or []) if r.get("symbol")]
            self.logger.info(f"FactorRanker: screener returned {len(syms)} candidates")
            return syms
        except Exception as e:
            self.logger.error(f"FactorRanker: screener universe resolution failed: {e}", exc_info=True)
            return []

    def _resolve_universe_source(self) -> str:
        """Resolve the canonical static-vs-screener choice for the candidate pool.

        Two knobs can express this, deliberately at DIFFERENT layers:

        * ``instrument_selection_method`` (the platform-wide base-interface setting,
          ``static|dynamic|expert|screener``) is the LIVE/ORCHESTRATION declaration.
          FactorRanker pins it to ``expert`` via ``required_instrument_selection_method``
          (get_expert_properties) — that is LOAD-BEARING: ``get_enabled_instruments``
          returns the ``"EXPERT"`` batch marker and JobManager routes the single
          ``run_analysis("EXPERT", ...)`` batch off it. We do NOT relax that; the UI
          forces+disables the dropdown, so a live FactorRanker is always ``expert`` here.
        * ``universe_source`` (``static|screener``) is FactorRanker's INTERNAL choice of
          HOW it self-selects once the platform has handed it the ``EXPERT`` batch:
          from the ``enabled_instruments`` config (``static``) or via the screener /
          metric_store (``screener``). The just-shipped ``--screener`` optimize flow
          (ba2test_launcher ``_cmd_optimize`` + strategy_optimization_handler
          ``_build_daily_trial_config``) drives this by pushing ``universe_source="screener"``
          + ``screener_store`` onto the per-trial settings, so it MUST keep working.

        Consolidation (backward-compatible): the user can ALSO drive the internal choice
        through the standard ``instrument_selection_method`` knob, but only where it does
        not collide with the load-bearing ``expert`` marker — i.e. when it is EXPLICITLY
        ``static`` or ``screener``. The pinned/live value ``expert`` (and the unrelated
        ``dynamic``) defer to ``universe_source`` exactly as today. Precedence:

        1. ``instrument_selection_method`` when it is explicitly ``static``/``screener``
           (the standard platform knob wins when the user uses it to mean static-vs-screener);
        2. otherwise ``universe_source`` (default ``static``) — the existing behaviour, so the
           live ``expert``-pinned path and the ``--screener`` flow are unchanged.
        """
        # Read the RAW instance setting (NOT get_setting_with_interface_default): the base-interface
        # DEFAULT for instrument_selection_method is "static", so falling back to it would silently
        # OVERRIDE this expert's own universe_source whenever ism isn't EXPLICITLY chosen. That is
        # exactly the optimize / persist / re-run path (which never sets ism) — it was forcing a
        # static universe and silently disabling the screener (universe_source="screener" + the
        # optimized screener genes). Only an EXPLICIT static/screener wins here; an unset ism (incl.
        # the live-pinned "expert", which isn't static/screener) defers to universe_source.
        ism = (self.settings.get("instrument_selection_method") or "").lower()
        if ism in ("static", "screener"):
            return ism
        return (self.get_setting_with_interface_default("universe_source") or "static").lower()

    def _resolve_universe(self, as_of: Optional[datetime] = None) -> List[str]:
        """Candidate pool to rank, after the min_price liquidity guard.

        The static-vs-screener choice is resolved by ``_resolve_universe_source`` (which
        honours both ``universe_source`` and an explicit ``instrument_selection_method``
        of ``static``/``screener`` — see that method for the layering/precedence). When it
        resolves to ``screener`` we run the configured StockScreener / metric_store
        (threading ``as_of`` for point-in-time membership); ``static`` (default) uses the
        enabled_instruments config. (``get_enabled_instruments`` returns the "EXPERT" marker
        for expert-selection, so read the config directly.)"""
        source = self._resolve_universe_source()
        if source == "screener":
            universe = self._screen_universe(as_of)
        else:
            universe = list(self._get_enabled_instruments_config().keys())

        min_price = float(self.get_setting_with_interface_default("min_price") or 0.0)
        if min_price > 0 and universe:
            from ba2_common.core.instance_resolver import get_instance_resolver
            from ba2_common.core.models import ExpertInstance
            from ba2_common.core.db import get_instance
            instance = get_instance(ExpertInstance, self.id)
            account = get_instance_resolver().get_account_instance(instance.account_id)
            filtered = []
            for sym in universe:
                price = account.get_instrument_current_price(sym)
                if price is not None and price >= min_price:
                    filtered.append(sym)
                else:
                    self.logger.debug(f"FactorRanker: {sym} dropped by min_price guard (price={price})")
            universe = filtered

        min_dollar_volume = float(self.get_setting_with_interface_default("min_dollar_volume") or 0.0)
        if min_dollar_volume > 0:
            self.logger.debug("FactorRanker: min_dollar_volume guard is reserved and not enforced in v1")

        return universe

    def _factor_weights(self) -> Dict[str, float]:
        """Assemble the per-factor weight dict from the individual float settings
        (factor_weight_momentum / _value / _quality / _pead)."""
        return {
            name: float(self.get_setting_with_interface_default(f"factor_weight_{name}") or 0.0)
            for name in _FACTOR_PIPELINE
        }

    # ------------------------------------------------------------------
    # Backtest contract (Phase 1): _gather (live concerns) + _process (pure)
    #
    # FactorRanker is a BASKET-level expert: it has no per-symbol ExpertRecommendation
    # seam and no SmartRiskManager. _process returns a single Recommendation whose
    # raw_outputs carries the TARGET WEIGHTS dict (+ the ranked book); live
    # run_analysis hands those targets to FactorPortfolioManager.rebalance, and the
    # Phase-4 backtest engine routes them to submit_order. current_price is None
    # because the decision is cross-sectional, not a single instrument's price.
    #
    # _gather resolves every live concern (universe, holdings) and threads as_of to
    # all four data fetchers; _process is pure (composite_score/rank/construction).
    # ------------------------------------------------------------------
    _FACTOR_SETTING_KEYS = (
        "winsorize_pct", "top_n", "weighting", "max_weight_per_name",
        "gross_exposure", "pead_drift_window_days",
    )

    def _resolve_factor_settings(self) -> Dict[str, Any]:
        """Resolve the construction settings _process consumes into a plain dict,
        plus the per-factor weights under ``_factor_weights`` (so _process never
        reads self for config — matches the optimizer-override flow)."""
        settings = self._resolve_settings(self._FACTOR_SETTING_KEYS)
        settings["_factor_weights"] = self._factor_weights()
        return settings

    def _gather_holdings(self) -> List[str]:
        """Symbols currently held by this expert (live concern). In Phase 1 this
        reads FactorPortfolioManager; in Phase 4 the backtest account supplies it."""
        pm = FactorPortfolioManager(self.id)
        return list(pm.get_holdings()[0])

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        """Resolve the universe, fetch + compute each enabled factor (threading
        as_of), read current holdings, and fetch as_of closes. Returns the
        data_bundle _process consumes. ``self._gather_settings`` must be set by the
        caller (run_analysis / analyze_as_of) so the gather-time weights + pead
        window are available without reading self config inside the loop."""
        settings = self._gather_settings
        weights = settings["_factor_weights"]
        pead_window = int(settings["pead_drift_window_days"])

        # Transparent backtest speedup: the backtest builds a MemoizedOHLCVProvider
        # (fetch-once-and-slice, in-memory) and exposes it as ``providers.ohlcv()``.
        # Thread it into every OHLCV-using fetcher so the daily closes are sliced from
        # memory instead of re-reading + re-parsing the parquet on every analysis bar.
        # On the LIVE path ``providers.ohlcv()`` is the plain FMP provider, so behaviour
        # is unchanged; the fetchers fall back to ``FMPOHLCVProvider()`` if it is None.
        ohlcv_provider = providers.ohlcv()

        universe = self._resolve_universe(as_of)
        factors: Dict[str, Dict[str, float]] = {}
        for name, (fetch_name, calc) in _FACTOR_PIPELINE.items():
            if float(weights.get(name, 0.0)) == 0.0:
                continue
            factors[name] = self._compute_factor(
                name, fetch_name, calc, universe, as_of=as_of,
                pead_drift_window_days=pead_window, ohlcv_provider=ohlcv_provider)

        holdings = self._gather_holdings() if universe else []
        prices = (data.fetch_close_prices(universe, as_of=as_of,
                                          ohlcv_provider=ohlcv_provider)
                  if universe else {})
        return {
            "universe": universe,
            "factors": factors,
            "holdings": holdings,
            "prices": prices,
            "current_price": None,   # FactorRanker is basket-level (cross-sectional)
        }

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        """PURE: composite z-score -> rank -> long-only top-N target weights.

        Returns a Recommendation whose raw_outputs carries the target weights dict
        + the ranked book (no ExpertRecommendation seam). Empty universe / no enabled
        factors -> skip=True (preserves the live _mark_skipped outcomes)."""
        # current_price is None for this basket-level expert (the decision is
        # cross-sectional, not a single instrument's price); _gather sets it.
        current_price = data_bundle.get("current_price")
        if not data_bundle["universe"]:
            return Recommendation(
                OrderRecommendation.HOLD, 0.0, current_price,
                "No candidate instruments configured",
                skip=True, skip_reason="No candidate instruments configured")
        if not data_bundle["factors"]:
            return Recommendation(
                OrderRecommendation.HOLD, 0.0, current_price,
                "No factors enabled (all weights are 0)",
                skip=True, skip_reason="No factors enabled (all weights are 0)")

        weights = settings["_factor_weights"]
        winsorize_pct = float(settings["winsorize_pct"] or 0.0)
        gross_exposure = float(settings["gross_exposure"])

        comp = composite_score(data_bundle["factors"], weights, winsorize_pct)
        ranked = rank_symbols(comp)
        targets = long_only_top_n(
            ranked, comp,
            top_n=int(settings["top_n"]),
            weighting=settings["weighting"],
            max_weight_per_name=float(settings["max_weight_per_name"]),
            gross_exposure=gross_exposure,
        )
        book = self._build_book(
            ranked, comp, data_bundle["factors"], targets, weights, winsorize_pct,
            held=set(data_bundle["holdings"]), gross_exposure=gross_exposure)
        return Recommendation(
            OrderRecommendation.OVERWEIGHT, 0.0, current_price,
            f"Ranked {len(ranked)} names, holding {len(targets)}",
            raw_outputs={"targets": targets, "book": book,
                         "name": "Ranked book", "type": "factor_ranking"})

    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation:
        """BacktestInterface entry: runs the SAME _gather+_process as the live path.
        The backtest engine consumes rec.raw_outputs['targets'] (weight-based).

        ``context.settings`` carries the engine/optimizer-resolved decision settings
        (``factor_weight_*`` / ``top_n`` / ``winsorize_pct`` / ...) but NOT the derived
        ``_factor_weights`` dict that ``_gather``/``_process`` require — on the live path
        ``_resolve_factor_settings`` adds it. Derive it here from the EFFECTIVE settings so the
        optimizer's ``model:factor_weight_*`` genes are honoured (the GA overrides arrive via
        ``context.settings``, not on ``self``), falling back to this expert's interface default
        for any weight the engine did not pass. Without this every bar raised
        ``KeyError('_factor_weights')`` and the bypass expert silently traded nothing."""
        settings = dict(context.settings)
        settings.setdefault("_factor_weights", {
            name: float(
                settings.get(f"factor_weight_{name}",
                             self.get_setting_with_interface_default(f"factor_weight_{name}"))
                or 0.0)
            for name in _FACTOR_PIPELINE
        })
        self._gather_settings = settings
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, settings, as_of)

    def _compute_factor(self, name, fetch_name, calc, universe, as_of=None,
                        pead_drift_window_days=None, ohlcv_provider=None) -> Dict[str, float]:
        """Fetch a factor's inputs in bulk and run its calculator.

        The fetcher is looked up on the ``data`` module at call time so it stays
        patchable in tests. ``as_of`` is threaded to every fetcher (Phase 1 backtest
        contract): all four fetchers (``fetch_close_prices``/``fetch_value_inputs``/
        ``fetch_quality_inputs``/``fetch_pead_inputs``) accept ``as_of`` as a kwarg;
        ``as_of=None`` (live) is byte-identical to the pre-refactor fetch.

        ``ohlcv_provider`` is the transparent backtest speedup (the in-memory
        MemoizedOHLCVProvider). It is threaded ONLY to the OHLCV-using fetchers
        (``fetch_close_prices`` for momentum, ``fetch_value_inputs`` for value), which
        accept it as a kwarg; the fundamentals-only fetchers (quality/pead) don't use
        OHLCV, so it is not passed to them. Support is detected by introspection so a
        mock/patched fetcher without the kwarg is still called unchanged.

        ``pead_drift_window_days`` is resolved by the caller (live ``run_analysis``
        reads the setting; ``_gather`` passes the value from ``self._gather_settings``)
        so this helper stays free of ``self.get_setting`` reads for the backtest path.
        """
        fetcher = getattr(data, fetch_name)
        fetch_kwargs = {"as_of": as_of}
        if ohlcv_provider is not None and _accepts_kwarg(fetcher, "ohlcv_provider"):
            fetch_kwargs["ohlcv_provider"] = ohlcv_provider
        inputs = fetcher(universe, **fetch_kwargs)
        if name == "pead":
            if pead_drift_window_days is None:
                pead_drift_window_days = int(
                    self.get_setting_with_interface_default("pead_drift_window_days"))
            return calc(inputs, drift_window_days=int(pead_drift_window_days))
        return calc(inputs)

    def _build_book(self, ranked, comp, factor_values, targets, weights, winsorize_pct,
                    held=None, gross_exposure=None) -> Dict[str, Any]:
        """Assemble the ranked-book dict stored in MarketAnalysis.state / shown in the UI.

        ``action`` is the intended trade for this rebalance, comparing the target
        book to the symbols currently held (``held``): BUY (new), HOLD (kept),
        SELL (dropped holding), "—" (ranked but neither targeted nor held).

        ``gross_exposure`` is supplied by the caller (resolved settings) so this stays
        free of ``self.get_setting`` reads on the pure ``_process`` path; it falls
        back to the live setting only when not provided (legacy direct callers).
        """
        held = held or set()
        if gross_exposure is None:
            gross_exposure = float(self.get_setting_with_interface_default("gross_exposure"))
        zscores = {
            name: cross_sectional_zscore(vals, winsorize_pct)
            for name, vals in factor_values.items()
        }
        ranking = []
        for i, sym in enumerate(ranked):
            in_target = sym in targets
            if in_target and sym not in held:
                action = "BUY"
            elif in_target:
                action = "HOLD"
            elif sym in held:
                action = "SELL"
            else:
                action = "—"
            ranking.append({
                "symbol": sym,
                "rank": i + 1,
                "composite": round(comp.get(sym, 0.0), 4),
                "factors": {name: round(z.get(sym, 0.0), 4) for name, z in zscores.items()},
                "target_weight": round(targets.get(sym, 0.0), 4),
                "action": action,
            })
        return {
            "rebalanced_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": len(ranked),
            "held_count": len(targets),
            "gross_exposure": float(gross_exposure),
            "weights": {k: v for k, v in weights.items() if v},
            "targets": targets,
            "ranking": ranking,
        }

    def _mark_skipped(self, market_analysis: MarketAnalysis, reason: str) -> None:
        self.logger.info(f"FactorRanker analysis skipped: {reason}")
        market_analysis.state = {"factor_ranker": {"skipped": True, "reason": reason}}
        market_analysis.status = MarketAnalysisStatus.SKIPPED
        update_instance(market_analysis)

    def _write_output(self, market_analysis: MarketAnalysis, name: str, type_: str, payload: dict) -> None:
        import json
        try:
            add_instance(AnalysisOutput(
                market_analysis_id=market_analysis.id,
                name=name,
                type=type_,
                text=json.dumps(payload, indent=2, default=str),
                provider_category="analysis",
                provider_name="FactorRanker",
            ))
        except Exception as e:
            self.logger.error(f"FactorRanker: failed to write AnalysisOutput '{name}': {e}", exc_info=True)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        from ba2_experts.FactorRanker.ui import render_market_analysis as _render
        _render(self, market_analysis)
