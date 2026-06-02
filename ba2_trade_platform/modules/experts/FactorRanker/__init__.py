"""FactorRanker — configurable cross-sectional multi-factor equity expert.

Ranks a candidate universe each rebalance by a weighted blend of momentum,
post-earnings-drift, value and quality factors, holds the long-only top slice,
and rebalances via :class:`FactorPortfolioManager`. It runs as a single batch
job (``run_analysis("EXPERT", ma)``) and writes a ``MarketAnalysis`` audit trail —
no ``ExpertRecommendation`` records, no ``SmartRiskManager``.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

from ....core.db import add_instance, update_instance
from ....core.interfaces import MarketExpertInterface
from ....core.models import AnalysisOutput, MarketAnalysis
from ....core.StockScreener import StockScreener
from ....core.types import MarketAnalysisStatus
from ....logger import get_expert_logger

from . import data
from .construction import long_only_top_n
from .factors import (
    composite_score, cross_sectional_zscore, earnings_surprise, momentum_12_1,
    quality_score, rank_symbols, value_score,
)
from .portfolio import FactorPortfolioManager


# Map factor name -> (data fetcher attribute name, pure calculator). The fetcher is
# resolved off the `data` module at call time (not captured here) so it stays
# patchable in tests. Adding a factor is a one-line change.
_FACTOR_PIPELINE = {
    "momentum": ("fetch_close_prices", momentum_12_1),
    "value": ("fetch_value_inputs", value_score),
    "quality": ("fetch_quality_inputs", quality_score),
    "pead": ("fetch_pead_inputs", earnings_surprise),
}


class FactorRanker(MarketExpertInterface):
    """Configurable cross-sectional multi-factor equity ranker."""

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
            "factor_weights": {
                "type": "json", "required": False,
                "default": {"momentum": 1.0, "value": 1.0, "quality": 1.0, "pead": 0.0},
                "description": "Per-factor weights; 0 disables a factor (momentum/value/quality/pead).",
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
        }

    def __init__(self, id: int):
        super().__init__(id)
        self.logger = get_expert_logger("FactorRanker", id)

    # ------------------------------------------------------------------
    # Analysis pipeline
    # ------------------------------------------------------------------

    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Rank the universe and rebalance. ``symbol`` is the "EXPERT" batch marker."""
        self.logger.info(f"FactorRanker analysis starting (analysis {market_analysis.id}, symbol={symbol})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            universe = self._resolve_universe()
            if not universe:
                self._mark_skipped(market_analysis, "No candidate instruments configured")
                return

            weights = self.get_setting_with_interface_default("factor_weights") or {}
            winsorize_pct = float(self.get_setting_with_interface_default("winsorize_pct") or 0.0)

            # Fetch + compute only the enabled factors (weight > 0).
            factor_values: Dict[str, Dict[str, float]] = {}
            for name, (fetch_name, calc) in _FACTOR_PIPELINE.items():
                if float(weights.get(name, 0.0)) == 0.0:
                    continue
                factor_values[name] = self._compute_factor(name, fetch_name, calc, universe)

            if not factor_values:
                self._mark_skipped(market_analysis, "No factors enabled (all weights are 0)")
                return

            comp = composite_score(factor_values, weights, winsorize_pct)
            ranked = rank_symbols(comp)
            targets = long_only_top_n(
                ranked, comp,
                top_n=int(self.get_setting_with_interface_default("top_n")),
                weighting=self.get_setting_with_interface_default("weighting"),
                max_weight_per_name=float(self.get_setting_with_interface_default("max_weight_per_name")),
                gross_exposure=float(self.get_setting_with_interface_default("gross_exposure")),
            )

            FactorPortfolioManager(self.id).rebalance(targets)

            book = self._build_book(ranked, comp, factor_values, targets, weights, winsorize_pct)
            market_analysis.state = {"factor_ranker": book}
            self._write_output(market_analysis, "Ranked book", "factor_ranking", book)
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            self.logger.info(
                f"FactorRanker analysis complete: ranked {len(ranked)} names, "
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

    def _screen_universe(self) -> List[str]:
        """Resolve the candidate universe by running the configured StockScreener.

        Reads the expert's ``screener_*`` settings (already part of the base
        interface) and returns the matched symbols (uppercased). Failures degrade
        to an empty universe rather than raising, so one bad screen doesn't crash
        the rebalance.
        """
        try:
            result = StockScreener(dict(self.settings)).screen()
            syms = [r["symbol"].upper() for r in (result.get("results") or []) if r.get("symbol")]
            self.logger.info(f"FactorRanker: screener returned {len(syms)} candidates")
            return syms
        except Exception as e:
            self.logger.error(f"FactorRanker: screener universe resolution failed: {e}", exc_info=True)
            return []

    def _resolve_universe(self) -> List[str]:
        """Candidate pool to rank, after the min_price liquidity guard.

        ``universe_source`` selects how the pool is built: ``screener`` runs the
        configured StockScreener; ``static`` (default) uses the enabled_instruments
        config. (``get_enabled_instruments`` returns the "EXPERT" marker for
        expert-selection, so read the config directly.)"""
        source = (self.get_setting_with_interface_default("universe_source") or "static").lower()
        if source == "screener":
            universe = self._screen_universe()
        else:
            universe = list(self._get_enabled_instruments_config().keys())

        min_price = float(self.get_setting_with_interface_default("min_price") or 0.0)
        if min_price > 0 and universe:
            from ....core.utils import get_account_instance_from_id
            from ....core.models import ExpertInstance
            from ....core.db import get_instance
            instance = get_instance(ExpertInstance, self.id)
            account = get_account_instance_from_id(instance.account_id)
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

    def _compute_factor(self, name, fetch_name, calc, universe) -> Dict[str, float]:
        """Fetch a factor's inputs in bulk and run its calculator.

        The fetcher is looked up on the ``data`` module at call time so it stays
        patchable in tests.
        """
        inputs = getattr(data, fetch_name)(universe)
        if name == "pead":
            window = int(self.get_setting_with_interface_default("pead_drift_window_days"))
            return calc(inputs, drift_window_days=window)
        return calc(inputs)

    def _build_book(self, ranked, comp, factor_values, targets, weights, winsorize_pct) -> Dict[str, Any]:
        """Assemble the ranked-book dict stored in MarketAnalysis.state / shown in the UI."""
        zscores = {
            name: cross_sectional_zscore(vals, winsorize_pct)
            for name, vals in factor_values.items()
        }
        ranking = []
        for i, sym in enumerate(ranked):
            ranking.append({
                "symbol": sym,
                "rank": i + 1,
                "composite": round(comp.get(sym, 0.0), 4),
                "factors": {name: round(z.get(sym, 0.0), 4) for name, z in zscores.items()},
                "target_weight": round(targets.get(sym, 0.0), 4),
                "action": "HOLD" if sym in targets else "—",
            })
        return {
            "rebalanced_at": datetime.now(timezone.utc).isoformat(),
            "universe_size": len(ranked),
            "held_count": len(targets),
            "gross_exposure": float(self.get_setting_with_interface_default("gross_exposure")),
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
        from .ui import render_market_analysis as _render
        _render(self, market_analysis)
