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
from ba2_common.core.interfaces.ExpertDataExportInterface import (
    EXPORT_BYPASS_ID, ExpertDataExportInterface, ExpertMetric,
)
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.core.models import AnalysisOutput, MarketAnalysis
from ba2_providers.StockScreener import StockScreener
from ba2_providers.fmp_common import FMPHermeticViolation
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


class FactorRanker(ExpertDataExportInterface, MarketExpertInterface):
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
            # sector_neutralize REMOVED 2026-08-08. It was declared as a reserved
            # placeholder ("not applied in v1") and never read by any code path, so
            # the settings UI offered a switch that silently did nothing -- the same
            # inert-knob class as the ATR genes and DeterministicScorer's
            # fundamentals_max_age_days. No instance had a stored value (checked in
            # the dev, prod and test DBs), so nothing is lost by dropping it.
            # Re-add it in the SAME commit that implements the neutralization; the
            # intent stays recorded in docs/plans/2026-06-02-factorranker-design.md.
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

    def validate_deployed_settings(self) -> None:
        """Flag a deploy whose factor ranking cannot do anything.

        ``long_only_top_n`` does ``picks = ranked[:top_n]``. When the candidate pool is already
        <= ``top_n`` that slice discards NOTHING: the expert holds the whole screener output,
        equal-weighted, and factor_weight_{momentum,value,quality,pead} have zero effect on what
        is held. The genome still LOOKS tuned, and the backtest that justified it ran the same
        way, so nothing downstream ever notices.

        Found live 2026-08-06 on 3 of 6 FactorRanker instances (14: top_n 15 >= max_stocks 10;
        26: 30 >= 30; 27: 35 >= 30). Two of those differed only in inert genes, so they held the
        same names on one account at ~2x combined exposure — something neither backtest modelled.

        Warns rather than raises: those instances are live, and blocking the save would make an
        existing bad config unfixable through the normal path.
        """
        try:
            if self._resolve_universe_source() != "screener":
                return  # a static universe is not capped by the screener; top_n is meaningful
            g = self.get_setting_with_interface_default
            top_n = int(g("top_n") or 0)
            max_stocks = int(g("screener_max_stocks") or 0)
        except Exception as e:  # noqa: BLE001 — never fail a save over a diagnostic
            self.logger.debug(f"FactorRanker: ranking-inertness check skipped ({e})")
            return

        if top_n and max_stocks and top_n >= max_stocks:
            self.logger.warning(
                f"FactorRanker instance {self.id}: RANKING IS INERT — top_n={top_n} >= "
                f"screener_max_stocks={max_stocks}, so every screened candidate is held and the "
                f"factor weights (momentum/value/quality/pead) have NO effect on selection. "
                f"Set top_n < screener_max_stocks for the ranking to discriminate. NOTE: doing so "
                f"changes the strategy to one the GA never evaluated — prefer re-optimising over "
                f"hand-editing a deployed genome."
            )

    def _metric_store_settings(self) -> Dict[str, Any]:
        """Translate this expert's ``screener_*``-prefixed settings into the UNPREFIXED
        keys the fast metric_store expects.

        The base-interface screener settings are ``screener_*``-prefixed, but
        ``ba2_providers.screener.metric_store.screen_universe_as_of`` reads unprefixed
        keys (e.g. ``market_cap_min``). A translator is therefore mandatory — passing the
        raw ``screener_*`` keys would match nothing in the store's ``settings.get(...)``
        lookups (so every filter is a no-op and the WHOLE universe is selected).

        The key set is taken from ``METRIC_STORE_KEYS`` and normalised by the store's own
        ``normalize_screener_settings`` (the same helper daily_backtest_handler and
        strategy_optimization_handler use) rather than hand-listed here. A hand-rolled list
        silently drops any key the store learns later: it had omitted ``price_drop_days`` and
        ``float_min``/``float_max`` — all three ARE supported (the store carries
        ``price_drop_pct_2..30`` and ``float_shares``). Live consequence on 2026-07-27:
        instances 26/27 differed only on ``price_drop_days`` (3 vs 2) once ``top_n`` had
        swallowed the pool, that difference was discarded here, and both resolved the same
        universe -> byte-identical portfolios -> double-sized positions on one broker account.

        ``screener_universe_mode`` / ``screener_provider`` remain unsupported by the store and
        are correctly filtered out by the normalizer.

        Deriving the key set from ``METRIC_STORE_KEYS`` cuts BOTH ways, and the second edge drew
        blood on 2026-08-05: ``dollar_volume_min`` was added to the store's key set (47c4b26) with
        no matching ``screener_dollar_volume_min`` EXPERT setting, so this comprehension asked for
        a setting that did not exist, ``get_setting_with_interface_default`` raised, and the whole
        metric_store path fell over. The fallback then hit the live StockScreener, which a backtest
        cannot reach -- so every FactorRanker run since screened an EMPTY universe and traded
        nothing (opts 251/252/255 failed, 257 "completed" with 0 trades).

        So: a store key with no expert setting is SKIPPED and named in a warning, never fatal. A
        missing key costs one unexpressed filter; raising costs the entire universe. The warning
        is what keeps it from becoming the silent no-op the docstring above warns about.
        """
        from ba2_providers.screener.metric_store import (
            METRIC_STORE_KEYS, normalize_screener_settings,
        )
        raw, unsupported = {}, []
        for key in METRIC_STORE_KEYS:
            setting = f"screener_{key}"
            try:
                raw[setting] = self.get_setting_with_interface_default(setting)
            except ValueError:
                # get_setting_with_interface_default raises ValueError for exactly one reason --
                # the key is absent from the merged interface definitions. Probing via the real
                # lookup (rather than inspecting the definitions dict) keeps this working for any
                # settings-provider shape, including the test stubs.
                unsupported.append(setting)
        if unsupported:
            self.logger.warning(
                f"FactorRanker: the metric_store supports {sorted(unsupported)} but this expert "
                f"defines no such setting(s) -- those filters are NOT applied. Add them to the "
                f"screener settings block in MarketExpertInterface to enable them."
            )
        return normalize_screener_settings(raw)

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
        # BACKTEST ONLY. The store is a prebuilt weekly snapshot, so resolving "today"
        # against it returns the newest scan date <= today -- on 2026-07-27 that was
        # 2026-06-27, i.e. live orders were being placed off a 31-day-stale universe
        # (stale caps / relative volume / Weinstein stage, and no new listings). A live
        # deploy inherits ``screener_store`` from the backtest export
        # (testplatform backtests.py, the bypass-expert overlay), so the setting being
        # present must NOT be read as "use the store live". ``_precomputed_factor_inputs``
        # already guards the same way (``as_of is None -> return None, None``); this is
        # the universe path catching up with it.
        if store and as_of is not None:
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
        except FMPHermeticViolation:
            # NEVER degrade a hermetic breach to an empty universe. This handler is what made the
            # 2026-08-05 metric_store regression invisible for two days: the store path raised, the
            # fallback reached for the live screener, the guard fired correctly -- and then this
            # `except Exception` swallowed it and returned [], so the job screened NOTHING, traded
            # NOTHING, and still exited 0 (opt 257: one backtest, 0 trades, 0%).
            #
            # A breach can only happen inside a backtest, so re-raising cannot affect live: the
            # graceful empty-universe degradation below still covers every real screener failure.
            # strategy_optimization_handler already classifies FMPHermeticViolation as FATAL; it
            # just never got to see one.
            raise
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
            if self.id == EXPORT_BYPASS_ID:
                # A SYMBOL360 export instance (see _gather_holdings for the identical
                # rationale) has no real ExpertInstance row/account to resolve here --
                # get_instance(ExpertInstance, self.id) and get_account_instance(...)
                # both hit real DB/live-infra lookups with no settings-only escape
                # hatch. Degrade to "no liquidity filtering" rather than erroring: a
                # SYMBOL360 user dialing in a nonzero min_price override (Task 12's
                # settings expander lets them edit any setting) must get a graceful
                # result, not an opaque DB/resolver failure for a knob unrelated to
                # holdings.
                self.logger.debug(
                    "FactorRanker: min_price guard skipped for a SYMBOL360 export "
                    "instance (no live account to price against)")
            else:
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
        reads FactorPortfolioManager; in Phase 4 the backtest account supplies it.

        ``FactorPortfolioManager(self.id)`` does two real DB/live-infra lookups
        (``get_instance_resolver().get_account_instance(...)`` and
        ``get_instance(ExpertInstance, self.id)``) — there is no settings-only
        way to satisfy either for a read-only export. A SYMBOL360
        (``ExpertDataExportInterface.export_symbol_data``) bypass instance is
        stamped with the sentinel ``EXPORT_BYPASS_ID`` (never a real
        ExpertInstance row), so short-circuit to "nothing held" rather than
        raising: holdings only drive the ranked book's BUY/HOLD/SELL ``action``
        display tag (not the composite/factor scores the export surfaces), and
        a bypass instance genuinely has no live portfolio to report.
        """
        if self.id == EXPORT_BYPASS_ID:
            return []
        pm = FactorPortfolioManager(self.id)
        return list(pm.get_holdings()[0])

    def _store_factor_inputs(self, universe: List[str], as_of: Optional[datetime]):
        """``(precomputed_momentum, price_as_of)`` read point-in-time from the screener metric store,
        or ``(None, None)`` to fall back to OHLCV fetches.

        Lets FactorRanker read 12-1 momentum and the as_of close from the store instead of fetching
        ~400 days of daily OHLCV per symbol per rebalance — so it holds NO price history in memory.
        Used ONLY when a ``screener_store`` is configured AND it covers every universe symbol with
        the needed columns (the screener universe always does; an incomplete/old store -> full
        OHLCV fallback, results unchanged). The values are read AS-OF (latest scan <= as_of): with a
        DAILY store the read lands on the exact analysis bar -> byte-identical to the runtime factor
        (daily ranking); with a weekly store they refresh on the scan cadence (held, like the
        universe). Momentum NaN / non-positive-start -> 0.0 (matches ``factors.momentum_12_1``)."""
        if not universe or as_of is None:
            return None, None
        store = (self.get_setting_with_interface_default("screener_store") or "").strip()
        if not store:
            return None, None
        try:
            import pandas as pd
            from ba2_providers.screener import metric_store as ms
            df = ms.load_store(store)
            rows = ms.metrics_as_of(df, as_of.strftime("%Y-%m-%d"), ["momentum_12_1", "close"])
        except Exception:  # noqa: BLE001 — any store issue -> safe OHLCV fallback
            return None, None
        if not rows or not all(s in rows for s in universe):
            return None, None  # incomplete coverage -> full fallback (results identical)

        def _num(v):
            return None if v is None or pd.isna(v) else float(v)

        momentum = None
        if all("momentum_12_1" in rows[s] for s in universe):
            momentum = {s: (_num(rows[s].get("momentum_12_1")) or 0.0) for s in universe}
        price_as_of = None
        if all(_num(rows[s].get("close")) is not None for s in universe):
            price_as_of = {s: _num(rows[s].get("close")) for s in universe}
        return momentum, price_as_of

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

        # Precomputed point-in-time inputs from the metric store (when configured + built with them):
        # momentum_12_1 and the as_of close, so the momentum + value factors need NO OHLCV history.
        precomputed_momentum, price_as_of = self._store_factor_inputs(universe, as_of)

        factors: Dict[str, Dict[str, float]] = {}
        for name, (fetch_name, calc) in _FACTOR_PIPELINE.items():
            if float(weights.get(name, 0.0)) == 0.0:
                continue
            if name == "momentum" and precomputed_momentum is not None:
                factors[name] = {s: precomputed_momentum.get(s, 0.0) for s in universe}
                continue
            factors[name] = self._compute_factor(
                name, fetch_name, calc, universe, as_of=as_of,
                pead_drift_window_days=pead_window, ohlcv_provider=ohlcv_provider,
                price_as_of=(price_as_of if name == "value" else None))

        holdings = self._gather_holdings() if universe else []
        return {
            "universe": universe,
            "factors": factors,
            "holdings": holdings,
            # ``prices`` is not consumed downstream (the portfolio reads live prices off the account);
            # it was a whole-universe OHLCV fetch that bloated the memo, so we no longer fetch it.
            "prices": {},
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
                        pead_drift_window_days=None, ohlcv_provider=None,
                        price_as_of=None) -> Dict[str, float]:
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
        # Precomputed point-in-time prices from the metric store (value factor): use them instead of
        # an OHLCV fetch so FactorRanker holds no per-symbol price series in memory.
        if price_as_of is not None and _accepts_kwarg(fetcher, "price_as_of"):
            fetch_kwargs["price_as_of"] = price_as_of
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
    # Data export (SYMBOL360)
    # ------------------------------------------------------------------

    def _build_export_metrics(self, rec: Recommendation, settings: Dict[str, Any]) -> List[Any]:
        """Surface the REQUESTED symbol's row from the ranked book as
        composite + per-factor metric rows.

        FactorRanker is basket-level: ``rec`` carries the WHOLE resolved
        universe's ranking in ``raw_outputs["book"]["ranking"]``, one dict
        per symbol, and neither ``rec`` nor ``settings`` (this method's only
        two parameters) identifies which symbol the caller actually asked
        about. ``export_symbol_data`` (``ExpertDataExportInterface._bypass_instance``)
        stashes the requested symbol on the instance as ``self._export_symbol``
        for exactly this reason -- read it back here rather than assuming
        position 0.

        Callers are expected to pin the universe to a single symbol via
        ``overrides`` (``instrument_selection_method``/``universe_source``
        ``"static"`` + ``enabled_instruments={symbol: {...}}``), so in the
        intended usage ``ranking`` has exactly one row and it already matches
        the requested symbol. We still match BY SYMBOL (case-insensitively)
        rather than indexing ``ranking[0]`` blindly: a caller that forgets to
        pin the universe (or a screener-sourced universe_source override that
        resolves >1 name) would otherwise silently report some OTHER symbol's
        score as if it were the requested one. Only when no override was
        applied AND the (unpinned) universe happens to resolve to exactly one
        name do we fall back to that single row.

        A skipped Recommendation (empty universe / no factors enabled) carries
        no ``raw_outputs["book"]`` at all -- defer to the base class so
        ``rec.skip_reason`` still reaches the UI as a "Skipped" row instead of
        silently vanishing into an empty metrics list (the base's default
        _build_export_metrics already renders exactly that row; every other
        wired expert does the same check first for the same reason).
        """
        if rec.skip:
            return super()._build_export_metrics(rec, settings)

        raw = rec.raw_outputs or {}
        book = raw.get("book") or {}
        ranking = book.get("ranking") or []
        if not ranking:
            return []

        symbol = getattr(self, "_export_symbol", None)
        row = None
        if symbol:
            row = next(
                (r for r in ranking if str(r.get("symbol", "")).upper() == symbol.upper()),
                None)
        if row is None:
            if len(ranking) == 1:
                row = ranking[0]  # unambiguous: a genuinely single-symbol universe
            else:
                self.logger.warning(
                    f"FactorRanker._build_export_metrics: requested symbol {symbol!r} not "
                    f"found in a {len(ranking)}-row ranking -- the universe was not pinned "
                    f"to that single symbol; returning no metrics rather than guessing.")
                return []

        # _build_book always writes round(...get(sym, 0.0), 4) for composite/factor
        # z-scores, so these are real floats, never None -- the `or 0.0` below is
        # defense-in-depth against a future _build_book change, not a known gap.
        composite = row.get("composite") or 0.0
        out = [ExpertMetric(
            "Composite factor score", composite, f"{composite:+.3f}",
            "buy" if composite > 0 else ("sell" if composite < 0 else "neutral"),
            f"Rank {row.get('rank')} of {book.get('universe_size', len(ranking))}")]
        for name, z in (row.get("factors") or {}).items():
            z = z or 0.0
            out.append(ExpertMetric(
                f"Factor: {name}", z, f"{z:+.3f}",
                "buy" if z > 0 else ("sell" if z < 0 else "neutral")))
        return out

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        from ba2_experts.FactorRanker.ui import render_market_analysis as _render
        _render(self, market_analysis)
