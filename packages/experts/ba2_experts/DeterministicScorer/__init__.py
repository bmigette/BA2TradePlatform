"""DeterministicScorer — LLM-free multi-section scoring expert.

Reproduces a TradingAgents-style verdict (BUY/SELL/HOLD + profit target) with
pure local math, zero LLM calls. Sections:

  TECHNICAL   (momentum 12-1 vol-adjusted, SMA200 trend, RSI14 mean-reversion,
               Donchian breakout; ADX routes trend vs mean-reversion emphasis)
  FUNDAMENTAL (Piotroski F-Score, quality, value, growth acceleration;
               Altman Z distress = hard veto)
  ANALYST     (dated FMP/FinnHub rating revisions, recency-decayed; OPTIONAL,
               weight defaults to 0 — ratings lag price and double-count momentum)
  MACRO       (deterministic regime composite used as an exposure multiplier)

final = tanh(Σ wᵢ·sectionᵢ / k_compress) → vetoes → × regime exposure
BUY if final > +θ_buy, SELL if final < −θ_sell (Schmitt trigger, hysteresis).
Target = price ± k_target·ATR14. Every weight/period/threshold is an
ExpertSetting (hand-tunable in the UI, GA-optimizable by the testplatform).

Design doc: docs/plans/2026-08-07-deterministic-scorer-expert.md
Research:   workspace/ba2/research-deterministic-scoring.md
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ba2_common.core.db import get_db, update_instance, add_instance
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.logger import get_expert_logger
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin, FMPApiKeyMixin

from . import data
from .technical import technical_score
from .fundamental import (fundamental_score, piotroski_f_score,
                          altman_z_and_variant, growth_acceleration)
from .analyst import (analyst_section_score, DEF_AW_GRADES, DEF_AW_TARGETS,
                      DEF_TARGET_WINDOW_DAYS, DEF_TARGET_SCALE, DEF_MIN_TARGETS)
from ba2_experts.earnings_surprise import pead_score
from .macro import (trend_score, vix_score, pmi_score, sahm_score, credit_score,
                    yield_curve_score, regime_composite, DEF_MW, DEF_YC_SCALE,
                    DEF_M_FLOOR, DEF_HARD_RISKOFF)
from .combine import (final_score, schmitt_trigger, atr_target_price,
                      atr_stop_price, confidence_from_score,
                      DEF_W_TECHNICAL, DEF_W_FUNDAMENTAL, DEF_W_ANALYST,
                      DEF_K_COMPRESS, DEF_THETA_BUY, DEF_THETA_SELL,
                      DEF_K_STOP, DEF_K_TARGET, DEF_VETO_CAP, DEF_W_EARNINGS)


class DeterministicScorer(AnalysisStatusRenderMixin, FMPApiKeyMixin, MarketExpertInterface):
    """Deterministic multi-section scoring expert (no LLM)."""

    RENDER_PENDING = "Deterministic scoring queued…"
    RENDER_RUNNING = "Computing technical/fundamental/macro scores…"

    def __init__(self, id: int):
        super().__init__(id)
        self.logger = get_expert_logger(self.__class__.__name__, id)

    # ------------------------------------------------------------------ meta
    @classmethod
    def description(cls) -> str:
        return ("Deterministic multi-section scorer (no LLM): technical momentum/trend, "
                "fundamental quality/value/F-Score, optional analyst revisions, macro "
                "regime exposure — weighted into a BUY/SELL/HOLD verdict with an "
                "ATR-based profit target. All weights tunable.")

    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        return {"can_recommend_instruments": False}

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            # ---- section weights (normalized internally) ----
            "w_technical": {"type": "float", "required": False, "default": DEF_W_TECHNICAL,
                            "description": "Weight of the TECHNICAL section"},
            "w_fundamental": {"type": "float", "required": False, "default": DEF_W_FUNDAMENTAL,
                              "description": "Weight of the FUNDAMENTAL section"},
            "w_analyst": {"type": "float", "required": False, "default": DEF_W_ANALYST,
                          "description": "Weight of the ANALYST section (0 = disabled; ratings lag price)"},
            "macro_mode": {"type": "str", "required": False, "default": "multiply",
                           "valid_values": ["multiply", "gate", "input", "off"],
                           "description": "How the macro regime acts: multiply=exposure scaler, gate=block in bad regime, input=4th weighted section, off"},
            "m_floor": {"type": "float", "required": False, "default": 0.25,
                        "description": "Minimum exposure multiplier in multiply mode"},
            "hard_riskoff": {"type": "float", "required": False, "default": -0.75,
                             "description": "Regime below this forces exposure to 0"},
            "macro_gate_min": {"type": "float", "required": False, "default": -0.5,
                               "description": "Gate mode: regime below this flattens bullish signals"},
            "w_macro": {"type": "float", "required": False, "default": 0.2,
                        "description": "Macro weight (input mode only)"},
            # ---- decision ----
            "k_compress": {"type": "float", "required": False, "default": DEF_K_COMPRESS,
                           "description": "tanh compression scale on the weighted raw score"},
            "theta_buy": {"type": "float", "required": False, "default": DEF_THETA_BUY,
                          "description": "BUY threshold on the final score"},
            "theta_sell": {"type": "float", "required": False, "default": DEF_THETA_SELL,
                           "description": "SELL threshold (negative side) on the final score"},
            "veto_cap": {"type": "float", "required": False, "default": DEF_VETO_CAP,
                         "description": "Max score allowed when a hard veto (Altman distress) fires. "
                                        "0 blocks the entry; go negative only if you want vetoed names shortable"},
            "skip_on_missing_section": {"type": "str", "required": False, "default": "skip",
                                        "valid_values": ["skip", "neutral"],
                                        "description": "Missing section: skip=renormalize over the available ones, "
                                                       "neutral=keep its weight at score 0 (drags toward flat)"},
            "w_earnings": {"type": "float", "required": False, "default": DEF_W_EARNINGS,
                           "description": "Weight of the EARNINGS/PEAD section (0 = disabled). "
                                          "Shares its maths with FMPEarningsDrift, which trades the "
                                          "same signal standalone — let the grid say whether it also "
                                          "adds value inside the composite"},
            "earnings_drift_window_days": {"type": "int", "required": False, "default": 75,
                                           "description": "Days after the report beyond which the drift is treated as spent"},
            "earnings_halflife_days": {"type": "int", "required": False, "default": 25,
                                       "description": "Half-life of the post-announcement decay"},
            "earnings_scale_pct": {"type": "float", "required": False, "default": 5.0,
                                   "description": "Surprise %% saturating the tanh (fallback when SUE has no history)"},
            "earnings_use_sue": {"type": "bool", "required": False, "default": True,
                                 "description": "Standardize the surprise by the name's OWN past dispersion (SUE) "
                                                "so a 3%% beat means the same for a utility and a biotech"},
            # ---- technical sub-settings ----
            "mom_lookback_days": {"type": "int", "required": False, "default": 252,
                                  "description": "Momentum lookback (trading days)"},
            "mom_skip_days": {"type": "int", "required": False, "default": 21,
                              "description": "Skip the most recent N days of momentum (short-term reversal)"},
            "vol_window": {"type": "int", "required": False, "default": 63,
                           "description": "Realized-vol window for vol-adjusted momentum"},
            "sma_trend_period": {"type": "int", "required": False, "default": 200,
                                 "description": "Trend SMA period"},
            "rsi_period": {"type": "int", "required": False, "default": 14,
                           "description": "RSI period"},
            "donchian_period": {"type": "int", "required": False, "default": 20,
                                "description": "Donchian channel period"},
            "adx_period": {"type": "int", "required": False, "default": 14,
                           "description": "ADX period"},
            "adx_gate": {"type": "float", "required": False, "default": 25.0,
                         "description": "ADX >= gate means trending (shifts weight to trend signals)"},
            "adx_rsi_boost": {"type": "float", "required": False, "default": 2.0,
                              "description": "RSI weight multiplier when NOT trending"},
            "tw_mom": {"type": "float", "required": False, "default": 0.45,
                       "description": "Technical sub-weight: vol-adjusted momentum"},
            "tw_d200": {"type": "float", "required": False, "default": 0.25,
                        "description": "Technical sub-weight: distance to trend SMA"},
            "tw_rsi": {"type": "float", "required": False, "default": 0.15,
                       "description": "Technical sub-weight: RSI mean-reversion"},
            "tw_don": {"type": "float", "required": False, "default": 0.15,
                       "description": "Technical sub-weight: Donchian breakout"},
            "scale_momvol": {"type": "float", "required": False, "default": 1.5,
                             "description": "tanh scale for vol-adjusted momentum"},
            "scale_d200": {"type": "float", "required": False, "default": 0.15,
                           "description": "tanh scale for distance-to-SMA"},
            "scale_rsi": {"type": "float", "required": False, "default": 15.0,
                          "description": "tanh scale for RSI deviation from 50"},
            # ---- fundamental sub-settings ----
            "fw_piotroski": {"type": "float", "required": False, "default": 0.25,
                             "description": "Fundamental sub-weight: Piotroski F-Score"},
            "fw_quality": {"type": "float", "required": False, "default": 0.30,
                           "description": "Fundamental sub-weight: quality (ROE/GP/A)"},
            "fw_value": {"type": "float", "required": False, "default": 0.25,
                         "description": "Fundamental sub-weight: value (EV/EBIT, FCF yield)"},
            "fw_growth": {"type": "float", "required": False, "default": 0.20,
                          "description": "Fundamental sub-weight: growth acceleration"},
            "z_veto": {"type": "float", "required": False, "default": 1.8,
                       "description": "Original Altman Z below this = hard distress veto"},
            "z_veto_adjusted": {"type": "float", "required": False, "default": 1.1,
                                "description": "Z'' (adjusted, non-manufacturers) distress cutoff — a DIFFERENT "
                                               "scale from the original Z, hence its own threshold"},
            "altman_variant": {"type": "str", "required": False, "default": "auto",
                               "valid_values": ["auto", "original", "adjusted"],
                               "description": "Altman variant: auto picks original when a market cap is available"},
            "fscore_disqualify": {"type": "int", "required": False, "default": 0,
                                  "description": "F-Score <= this = disqualify (0 = off)"},
            "scale_accel": {"type": "float", "required": False, "default": 0.05,
                            "description": "tanh scale for growth acceleration"},
            "fundamentals_max_age_days": {"type": "int", "required": False, "default": 450,
                                          "description": "Ignore fundamentals whose FILING is older than this "
                                                         "(0 = off). Statements are fetched ANNUALLY, so a full "
                                                         "cycle plus filing lag is ~400d: the plan's 180d would "
                                                         "blank the section for 8 months out of every 12"},
            # ---- analyst sub-settings ----
            "analyst_window_days": {"type": "int", "required": False, "default": 90,
                                    "description": "Trailing window for rating revisions"},
            "analyst_recency_halflife_days": {"type": "int", "required": False, "default": 30,
                                              "description": "Exponential recency-decay halflife"},
            "aw_grades": {"type": "float", "required": False, "default": DEF_AW_GRADES,
                          "description": "ANALYST sub-weight: rating-revision momentum"},
            "aw_targets": {"type": "float", "required": False, "default": DEF_AW_TARGETS,
                           "description": "ANALYST sub-weight: price-target upside + revision drift"},
            "analyst_target_window_days": {"type": "int", "required": False, "default": DEF_TARGET_WINDOW_DAYS,
                                           "description": "Trailing window of dated analyst price targets"},
            "analyst_target_scale": {"type": "float", "required": False, "default": DEF_TARGET_SCALE,
                                     "description": "Implied upside saturating the tanh (0.20 = 20%%)"},
            "analyst_min_targets": {"type": "int", "required": False, "default": DEF_MIN_TARGETS,
                                    "description": "Minimum targets in the window; below this the leg is dropped "
                                                   "(a 1-2 analyst consensus is noise dressed as agreement)"},
            # ---- macro sub-weights ----
            "mw_trend_index": {"type": "float", "required": False, "default": DEF_MW["trend_index"],
                               "description": "Macro weight: index trend vs SMA200"},
            "mw_breadth": {"type": "float", "required": False, "default": DEF_MW["breadth"],
                           "description": "Macro weight: breadth (% universe above SMA200)"},
            "mw_vix": {"type": "float", "required": False, "default": DEF_MW["vix"],
                       "description": "Macro weight: VIX level"},
            "mw_credit": {"type": "float", "required": False, "default": DEF_MW["credit"],
                          "description": "Macro weight: HY credit OAS"},
            "mw_yield_curve": {"type": "float", "required": False, "default": DEF_MW["yield_curve"],
                               "description": "Macro weight: 10y-3m yield curve"},
            "mw_pmi": {"type": "float", "required": False, "default": DEF_MW["pmi"],
                       "description": "Macro weight: ISM/NAPM"},
            "mw_sahm": {"type": "float", "required": False, "default": DEF_MW["sahm"],
                        "description": "Macro weight: Sahm rule unemployment"},
            "vix_calm": {"type": "float", "required": False, "default": 15.0,
                         "description": "VIX at/below this = calm (+1)"},
            "vix_stress": {"type": "float", "required": False, "default": 30.0,
                           "description": "VIX at/above this = stressed (-1)"},
            "yc_scale": {"type": "float", "required": False, "default": 0.005,
                         "description": "Yield-curve spread (decimal) saturating the tanh"},
            "index_symbol": {"type": "str", "required": False, "default": "SPY",
                             "description": "Index symbol for the macro trend input"},
            # ---- targets / exits ----
            "atr_period": {"type": "int", "required": False, "default": 14,
                           "description": "ATR period"},
            "k_stop": {"type": "float", "required": False, "default": DEF_K_STOP,
                       "description": "Stop distance in ATR multiples (hint for the risk manager)"},
            "k_target": {"type": "float", "required": False, "default": DEF_K_TARGET,
                         "description": "Profit target in ATR multiples"},
            "target_from_score": {"type": "bool", "required": False, "default": False,
                                  "description": "Stretch the target multiple up to +30% with |final score|"},
            # ---- data policy ----
            "min_history_days": {"type": "int", "required": False, "default": 260,
                                 "description": "Min OHLCV bars required (else SKIP)"},
        }

    _SETTING_KEYS = (
        "w_technical", "w_fundamental", "w_analyst", "macro_mode", "m_floor",
        "hard_riskoff", "macro_gate_min", "w_macro",
        "k_compress", "theta_buy", "theta_sell", "veto_cap", "skip_on_missing_section",
        "w_earnings", "earnings_drift_window_days", "earnings_halflife_days",
        "earnings_scale_pct", "earnings_use_sue",
        "mom_lookback_days", "mom_skip_days", "vol_window", "sma_trend_period",
        "rsi_period", "donchian_period", "adx_period", "adx_gate", "adx_rsi_boost",
        "tw_mom", "tw_d200", "tw_rsi", "tw_don",
        "scale_momvol", "scale_d200", "scale_rsi",
        "fw_piotroski", "fw_quality", "fw_value", "fw_growth",
        "z_veto", "z_veto_adjusted", "altman_variant",
        "fscore_disqualify", "scale_accel", "fundamentals_max_age_days",
        "analyst_window_days", "analyst_recency_halflife_days",
        "aw_grades", "aw_targets", "analyst_target_window_days",
        "analyst_target_scale", "analyst_min_targets",
        "mw_trend_index", "mw_breadth", "mw_vix", "mw_credit",
        "mw_yield_curve", "mw_pmi", "mw_sahm",
        "vix_calm", "vix_stress", "yc_scale", "index_symbol",
        "atr_period", "k_stop", "k_target", "target_from_score",
        "min_history_days",
    )

    # ------------------------------------------------------- backtest entry
    def analyze_as_of(self, as_of: datetime, context: "BacktestContext") -> Recommendation:
        """Pin the _gather inputs from the CONTEXT before delegating.

        _gather reads w_analyst / index_symbol off self, and only live
        run_analysis used to set them -- so in backtest they fell back to the
        getattr defaults, grades were never fetched, and the analyst section
        could never fire. w_analyst is a GA gene whose whole purpose is to
        measure whether ratings add value: it was silently pinned to 0.
        """
        settings = context.settings or {}
        extra = getattr(context, "extra", None) or {}
        self._gather_symbol = extra.get("symbol", getattr(self, "_gather_symbol", None))
        self._gather_w_analyst = float(settings.get("w_analyst", DEF_W_ANALYST) or 0.0)
        self._gather_w_earnings = float(settings.get("w_earnings", DEF_W_EARNINGS) or 0.0)
        self._gather_index_symbol = str(settings.get("index_symbol", data.INDEX_SYMBOL))
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, settings, as_of)

    # ---------------------------------------------------------------- gather
    def _gather(self, providers: "ProviderBundle", as_of: Optional[datetime]) -> Dict[str, Any]:
        """Fetch everything the sections need via providers (never raw HTTP).

        as_of=None => latest (live). Point-in-time is enforced by the provider
        interfaces (as_of_date/end_date semantics) and by slicing OHLCV to
        <= as_of. The bundle carries current_price (the as_of close).
        """
        symbol = self._gather_symbol
        df = data.fetch_ohlcv(providers, symbol, as_of)
        statements = data.fetch_statements(providers, symbol, as_of)

        grades_rows: list = []
        target_rows: list = []
        if float(getattr(self, "_gather_w_analyst", 0.0) or 0.0) > 0:
            api_key = self._get_fmp_api_key()
            if api_key:
                grades_rows = data.fetch_grades_history(api_key, symbol)
                target_rows = data.fetch_price_targets(api_key, symbol)

        earnings_rows = (data.fetch_past_earnings(providers, symbol, as_of)
                         if float(getattr(self, "_gather_w_earnings", 0.0) or 0.0) > 0 else [])
        macro_inputs = data.fetch_macro_series(providers, as_of)
        index_closes = data.fetch_index_closes(
            providers, as_of, str(getattr(self, "_gather_index_symbol", data.INDEX_SYMBOL)))

        current_price = None
        if df is not None and not df.empty:
            current_price = float(df["Close"].iloc[-1])

        return {
            "symbol": symbol,
            "ohlcv": df,
            "statements": statements,
            "grades_rows": grades_rows,
            "target_rows": target_rows,
            "earnings_rows": earnings_rows,
            "macro_inputs": macro_inputs,
            "index_closes": index_closes,
            "current_price": current_price,
        }

    # --------------------------------------------------------------- process
    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        """PURE decision logic. settings is a fully-resolved dict; never reads
        self for config."""
        symbol = data_bundle["symbol"]
        df = data_bundle.get("ohlcv")
        current_price = data_bundle.get("current_price")
        min_hist = int(settings.get("min_history_days", 260))

        if df is None or len(df) < min_hist or current_price is None:
            n = 0 if df is None else len(df)
            return Recommendation(
                signal=OrderRecommendation.HOLD, confidence=0.0,
                current_price=current_price or 0.0,
                details=f"Insufficient OHLCV history ({n} < {min_hist})",
                expected_profit_percent=0.0, skip=True,
                skip_reason="insufficient_history")

        # ---- TECHNICAL ----
        tech = technical_score(df, settings)

        # ---- FUNDAMENTAL ----
        fund = self._build_fundamental(data_bundle, settings, as_of)

        # ---- ANALYST (optional) ----
        analyst_score: Optional[float] = None
        analyst_info: Dict[str, Any] = {}
        if float(settings.get("w_analyst", 0.0) or 0.0) > 0:
            ref = as_of if as_of is not None else datetime.now(timezone.utc)
            # Two dated legs: rating revisions + price-target drift. EPS-estimate
            # revisions would be the stronger signal but FMP exposes no dated
            # estimate history (only a snapshot keyed by future fiscal period),
            # so they are not backtestable and deliberately absent.
            sec = analyst_section_score(
                data_bundle.get("grades_rows"), data_bundle.get("target_rows"),
                ref, current_price, settings)
            if sec is not None:
                analyst_score = sec["score"]
                analyst_info = sec

        # ---- EARNINGS / PEAD (optional) ----
        # Shares its maths with FMPEarningsDrift via ba2_experts.earnings_surprise:
        # that expert thresholds the surprise into a boolean entry signal, this
        # section keeps it continuous so the weighting has something to weigh.
        earnings_score: Optional[float] = None
        earnings_info: Dict[str, Any] = {}
        if float(settings.get("w_earnings", 0.0) or 0.0) > 0 and data_bundle.get("earnings_rows"):
            ref = as_of if as_of is not None else datetime.now(timezone.utc)
            pead = pead_score(data_bundle["earnings_rows"], ref, settings)
            if pead is not None:
                earnings_score = pead["score"]
                earnings_info = pead

        # ---- MACRO regime ----
        regime = self._build_regime(data_bundle, settings)

        veto = bool(fund.get("veto"))
        result = final_score(
            technical=tech.get("score"),
            fundamental=fund.get("score"),
            analyst=analyst_score,
            earnings=earnings_score,
            regime=regime.get("score") if regime else None,
            s=settings, veto=veto,
            # How many macro inputs actually backed the regime: without it a
            # lone binary index-trend reading can hit the hard risk-off cutoff
            # and flatten every position on a data-availability artefact.
            regime_n_inputs=regime.get("n_inputs") if regime else None)

        final = result["final"]
        # Stateless per-bar verdict: no prior position is threaded in, so the
        # hysteresis/reversal-margin arms of schmitt_trigger stay inactive here
        # (they are exercised by callers that DO carry prev_signal). Neither
        # exit_hysteresis nor min_score_delta is exposed as an ExpertSetting for
        # exactly that reason -- a GA gene that cannot move anything is worse
        # than a missing feature.
        prev_signal = None
        action = schmitt_trigger(final, settings, prev_signal)

        signal_map = {
            "BUY": OrderRecommendation.BUY,
            "SELL": OrderRecommendation.SELL,
            "HOLD": OrderRecommendation.HOLD,
        }
        signal = signal_map[action]

        atr = tech.get("atr")
        target_price = atr_target_price(current_price, atr, action, settings, final) \
            if action in ("BUY", "SELL") else None
        stop_price = atr_stop_price(current_price, atr, action, settings) \
            if action in ("BUY", "SELL") else None

        expected_profit_percent = None
        if target_price is not None and current_price:
            if action == "BUY":
                expected_profit_percent = (target_price / current_price - 1.0) * 100.0
            else:
                expected_profit_percent = (current_price / target_price - 1.0) * 100.0

        confidence = confidence_from_score(final) if action != "HOLD" else max(0.0, 100.0 * abs(final))

        calc = {
            "signal": signal,
            "confidence": confidence,
            "expected_profit_percent": expected_profit_percent or 0.0,
            "details": self._format_details(symbol, action, final, tech, fund,
                                            analyst_score, regime, result,
                                            current_price, target_price),
            "final_score": final,
            "technical": tech.get("score"),
            "fundamental": fund.get("score"),
            "analyst": analyst_score,
            "earnings": earnings_score,
            "regime": regime.get("score") if regime else None,
            "exposure_multiplier": result.get("exposure_multiplier"),
            "veto": veto,
            "target_price": target_price,
            "stop_price": stop_price,
            "atr": atr,
        }

        details = calc["details"]
        return Recommendation(
            signal=signal,
            confidence=confidence,
            current_price=current_price,
            details=details,
            expected_profit_percent=expected_profit_percent,
            target_price=target_price,
            raw_outputs={"calc": calc, "technical": tech, "fundamental": fund,
                         "analyst": analyst_info, "earnings": earnings_info,
                         "regime": regime,
                         "combination": result})

    # ------------------------------------------------------- section builders
    @staticmethod
    def _statement_age_days(stmt: Dict[str, Any], as_of: Optional[datetime]) -> Optional[float]:
        """Age of a statement at as_of, measured from its FILING date."""
        ref = as_of if as_of is not None else datetime.now(timezone.utc)
        raw = next((stmt.get(k) for k in
                    ("fillingDate", "filling_date", "filingDate", "acceptedDate",
                     "accepted_date", "date") if stmt.get(k)), None)
        if not raw:
            return None
        try:
            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
        ref = ref.replace(tzinfo=timezone.utc) if ref.tzinfo is None else ref
        return (ref - d).total_seconds() / 86400.0

    def _build_fundamental(self, data_bundle: Dict[str, Any],
                           settings: Dict[str, Any],
                           as_of: Optional[datetime] = None) -> Dict[str, Any]:
        """Assemble the fundamental snapshot from point-in-time statements ONLY.

        The overview provider is a 'now' snapshot (documented lookahead
        limitation), so the backtest-safe path derives every input from the
        dated statements + the as_of price: market cap = price x weighted avg
        shares, ROE from income/balance, value from an EV-based earnings yield.
        """
        statements = data_bundle.get("statements") or {}
        income = statements.get("income") or []
        balance = statements.get("balance") or []
        cashflow = statements.get("cashflow") or []
        current_price = data_bundle.get("current_price")

        # fundamentals_max_age_days (0 = off): stale filings describe a company
        # that no longer exists. This setting was declared and GA-visible but
        # never read, so it tuned nothing.
        max_age = int(settings.get("fundamentals_max_age_days", 0) or 0)
        if max_age > 0 and income:
            age = self._statement_age_days(income[0], as_of)
            if age is not None and age > max_age:
                stale = fundamental_score({}, settings)
                stale["snapshot"] = {}
                stale["market_cap"] = None
                stale["stale_fundamentals_age_days"] = age
                return stale

        snapshot: Dict[str, Any] = {}
        cur_inc = income[0] if income else {}
        prior_inc = income[1] if len(income) > 1 else {}
        cur_bal = balance[0] if balance else {}
        prior_bal = balance[1] if len(balance) > 1 else {}
        cur_cf = cashflow[0] if cashflow else {}

        # Market cap from the as_of price x latest weighted avg shares (keeps
        # the backtest free of the overview provider's 'now' snapshot).
        market_cap = None
        shares = cur_inc.get("weighted_average_shares_outstanding") \
            or cur_inc.get("weightedAverageShsOut")
        if shares and current_price:
            try:
                market_cap = float(shares) * float(current_price)
            except (TypeError, ValueError):
                market_cap = None

        if income and balance:
            cur = {**cur_bal, **cur_inc, **cur_cf}
            prior = {**prior_bal, **prior_inc}
            snapshot["fscore"] = piotroski_f_score(cur, prior)
            # Keep the variant: the distress cutoff differs (1.81 vs 1.1), so a
            # bare number cannot be thresholded correctly downstream.
            snapshot["z"], snapshot["z_variant"] = altman_z_and_variant(
                cur, market_cap, str(settings.get("altman_variant", "auto")))

        # Quality: ROE = net income / shareholder equity (tanh around 15%).
        ni = cur_inc.get("net_income") or cur_inc.get("netIncome")
        eq = cur_bal.get("total_shareholder_equity") or cur_bal.get("totalStockholdersEquity")
        if ni is not None and eq:
            try:
                roe = float(ni) / float(eq)
                snapshot["quality_norm"] = max(-1.0, min(1.0, math.tanh((roe - 0.10) / 0.10)))
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        # Value: operating earnings yield on enterprise value, tanh around a
        # 10% neutral yield (cheap = high yield = positive).
        opinc = cur_inc.get("operating_income") or cur_inc.get("operatingIncome")
        cash = cur_bal.get("cash_and_cash_equivalents") or cur_bal.get("cashAndCashEquivalents") or 0.0
        tl = cur_bal.get("total_liabilities") or cur_bal.get("totalLiabilities") or 0.0
        if opinc is not None and market_cap:
            try:
                ev = market_cap + float(tl or 0.0) - float(cash or 0.0)
                if ev > 0:
                    ey = float(opinc) / ev
                    snapshot["value_norm"] = max(-1.0, min(1.0, math.tanh((ey - 0.10) / 0.10)))
            except (TypeError, ValueError):
                pass

        # Growth: revenue AND EPS acceleration across annual income statements
        # (provider returns latest-first, so reverse to ascending). Uses the
        # shared calculator -- the inline reimplementation that used to live here
        # diverged from the unit-tested one and mis-signed negative earnings.
        def _accel(*field_names) -> Optional[float]:
            vals = []
            for stmt in reversed(income):
                raw = next((stmt.get(f) for f in field_names
                            if stmt.get(f) is not None), None)
                if raw is None:
                    continue
                try:
                    vals.append(float(raw))
                except (TypeError, ValueError):
                    continue
            return growth_acceleration(vals, min_points=3)

        rev_accel = _accel("total_revenue", "revenue")
        if rev_accel is not None:
            snapshot["rev_accel"] = rev_accel
        eps_accel = _accel("eps_diluted", "epsdiluted", "eps")
        if eps_accel is not None:
            snapshot["eps_accel"] = eps_accel

        result = fundamental_score(snapshot, settings)
        result["snapshot"] = snapshot
        result["market_cap"] = market_cap
        return result

    def _build_regime(self, data_bundle: Dict[str, Any],
                      settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if str(settings.get("macro_mode", "multiply")).lower() == "off":
            return None
        mi = data_bundle.get("macro_inputs") or {}
        idx = data_bundle.get("index_closes")
        # NOTE: the previous guard here was `isinstance(x, object)`, which is
        # True for EVERY value including None, so the fallback branch was
        # unreachable and the input resolved to a key data.py never wrote.
        inputs = {
            "trend_index": trend_score(idx, int(settings.get("sma_trend_period", 200))),
            "breadth": None,  # v2: universe breadth (needs screener integration)
            "vix": vix_score(mi.get("vix"), float(settings.get("vix_calm", 15.0)),
                             float(settings.get("vix_stress", 30.0))),
            "credit": credit_score(mi.get("oas_series")),
            "yield_curve": yield_curve_score(
                mi.get("spread_10y3m_series"),
                scale=float(settings.get("yc_scale", DEF_YC_SCALE))),
            "pmi": pmi_score(mi.get("pmi")),
            "sahm": sahm_score(mi.get("unrate_series")),
        }
        weights = {k: float(settings.get(f"mw_{k}", DEF_MW[k])) for k in DEF_MW}
        return regime_composite(inputs, weights)

    # ---------------------------------------------------------------- details
    @staticmethod
    def _format_details(symbol: str, action: str, final: float, tech: Dict,
                        fund: Dict, analyst: Optional[float], regime: Optional[Dict],
                        result: Dict, price: float,
                        target_price: Optional[float]) -> str:
        def fmt(v):
            return f"{v:+.2f}" if v is not None else "n/a"
        parts = [f"{symbol}: {action} (final {final:+.3f})"]
        parts.append(f"T={fmt(tech.get('score'))} F={fmt(fund.get('score'))} "
                     f"A={fmt(analyst)} R={fmt(regime.get('score') if regime else None)}")
        if result.get("veto"):
            parts.append("VETO: fundamental distress cap applied")
        if regime is not None and result.get("exposure_multiplier") != 1.0:
            parts.append(f"regime exposure x{result['exposure_multiplier']:.2f}")
        if target_price is not None:
            parts.append(f"price {price:.2f} -> target {target_price:.2f}")
        return " | ".join(parts)

    # ------------------------------------------------------------- live entry
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Live entry point: same _gather/_process the backtest drives."""
        self.logger.info(f"Starting DeterministicScorer analysis for {symbol} "
                         f"(Analysis ID: {market_analysis.id})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            settings = self._resolve_settings(self._SETTING_KEYS)
            self._gather_symbol = symbol
            self._gather_w_analyst = float(settings.get("w_analyst", 0.0) or 0.0)
            self._gather_w_earnings = float(settings.get("w_earnings", 0.0) or 0.0)
            self._gather_index_symbol = str(settings.get("index_symbol", "SPY"))

            providers = self._live_providers()
            bundle = self._gather(providers, as_of=None)
            rec = self._process(bundle, settings, as_of=None)

            if rec.skip:
                market_analysis.state = {
                    "skipped": True,
                    "skip_reason": rec.skip_reason,
                    "skip_message": rec.details,
                }
                market_analysis.status = MarketAnalysisStatus.SKIPPED
                update_instance(market_analysis)
                return

            calc = rec.raw_outputs["calc"]
            recommendation_id = self._create_expert_recommendation(
                calc, symbol, market_analysis.id, rec.current_price)
            self._store_analysis_outputs(market_analysis.id, symbol, rec)

            market_analysis.state = {
                "deterministic_scorer": {
                    "recommendation": {
                        "signal": calc["signal"].value,
                        "confidence": calc["confidence"],
                        "expected_profit_percent": calc["expected_profit_percent"],
                        "details": calc["details"],
                    },
                    "scores": {
                        "final": calc["final_score"],
                        "technical": calc["technical"],
                        "fundamental": calc["fundamental"],
                        "analyst": calc["analyst"],
                        "regime": calc["regime"],
                        "exposure_multiplier": calc["exposure_multiplier"],
                        "veto": calc["veto"],
                    },
                    "levels": {
                        "target_price": calc["target_price"],
                        "stop_price": calc["stop_price"],
                        "atr": calc["atr"],
                    },
                },
            }
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            self.logger.info(f"DeterministicScorer {symbol}: {calc['signal'].value} "
                             f"final={calc['final_score']:+.3f} conf={calc['confidence']:.1f}")
        except Exception as e:
            self.logger.error(f"DeterministicScorer analysis failed for {symbol}: {e}",
                              exc_info=True)
            market_analysis.status = MarketAnalysisStatus.FAILED
            market_analysis.state = {"error": str(e)}
            update_instance(market_analysis)

    # ------------------------------------------------------------ persistence
    def _create_expert_recommendation(self, calc: Dict[str, Any], symbol: str,
                                      market_analysis_id: int,
                                      current_price: Optional[float]) -> int:
        expert_recommendation = ExpertRecommendation(
            instance_id=self.id,
            symbol=symbol,
            recommended_action=calc["signal"],
            expected_profit_percent=calc["expected_profit_percent"],
            price_at_date=current_price,
            details=calc["details"][:100000] if calc["details"] else None,
            confidence=round(calc["confidence"], 1),
            risk_level=RiskLevel.MEDIUM,
            time_horizon=TimeHorizon.MEDIUM_TERM,
            market_analysis_id=market_analysis_id,
            data={
                "DeterministicScorer": {
                    "final_score": calc["final_score"],
                    "section_scores": {
                        "technical": calc["technical"],
                        "fundamental": calc["fundamental"],
                        "analyst": calc["analyst"],
                        "regime": calc["regime"],
                    },
                    "target_price": calc["target_price"],
                    "stop_price": calc["stop_price"],
                }
            },
            created_at=datetime.now(timezone.utc),
        )
        return add_instance(expert_recommendation)

    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str,
                                rec: Recommendation) -> None:
        """Persist one AnalysisOutput per section for the UI audit trail.

        Field names must match the model exactly: SQLModel DROPS unknown kwargs
        silently, so writing to `output_type`/`content` did not raise -- it left
        the NOT NULL `name`/`type` empty and threw away the payload, failing only
        at COMMIT and marking every live analysis FAILED.
        """
        import json

        blocks = {
            "score_tree": rec.raw_outputs.get("combination"),
            "technical": rec.raw_outputs.get("technical"),
            "fundamental": rec.raw_outputs.get("fundamental"),
            "analyst": rec.raw_outputs.get("analyst"),
            "regime": rec.raw_outputs.get("regime"),
        }
        session = get_db()
        try:
            for name, payload in blocks.items():
                if payload is None:
                    continue
                session.add(AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    symbol=symbol,
                    name=f"{symbol}_{name}",
                    type=name,
                    text=json.dumps(payload, default=str),
                    provider_category="expert",
                    provider_name=self.__class__.__name__,
                    format_type="dict",
                    created_at=datetime.now(timezone.utc),
                ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------ live UI
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> str:
        """Render the analysis detail UI (NiceGUI/Quasar) and return a markdown
        summary for the PDF export.

        Follows the FMPRating conventions: dark card (#1e2a3a), gradient header,
        caption labels in #a0aec0. Everything reads from ``market_analysis.state``
        (written by the live/backtest ``run_analysis``), never re-fetches data.
        """
        from nicegui import ui

        state = market_analysis.state or {}

        # ---- failed -------------------------------------------------------
        if state.get("error"):
            with ui.card().classes('w-full p-4').style('background-color: #1e2a3a'):
                ui.label('Analysis failed').classes('text-h6').style('color: #ff6b6b')
                ui.label(str(state["error"])).style('color: #a0aec0')
            return f"DeterministicScorer analysis failed: {state['error']}"

        # ---- skipped ------------------------------------------------------
        if state.get("skipped"):
            with ui.card().classes('w-full p-4').style('background-color: #1e2a3a'):
                ui.label('Analysis skipped').classes('text-h6').style('color: #f6b93b')
                ui.label(str(state.get("skip_reason") or "")).style('color: #a0aec0')
                if state.get("skip_message"):
                    ui.label(str(state["skip_message"])).style('color: #a0aec0')
            return f"DeterministicScorer skipped: {state.get('skip_reason')}"

        block = state.get("deterministic_scorer")
        if not block:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return ""

        rec = block.get("recommendation", {})
        scores = block.get("scores", {})
        levels = block.get("levels", {})
        signal = str(rec.get("signal", "HOLD")).upper()
        confidence = float(rec.get("confidence") or 0.0)
        final = float(scores.get("final") or 0.0)

        signal_meta = {
            "BUY": ("arrow_upward", "#00d4aa", "linear-gradient(135deg, #00d4aa 0%, #00b894 100%)"),
            "SELL": ("arrow_downward", "#ff6b6b", "linear-gradient(135deg, #ff6b6b 0%, #d63031 100%)"),
        }
        sig_icon, sig_color, header_bg = signal_meta.get(
            signal, ("remove", "#f6b93b", "linear-gradient(135deg, #f6b93b 0%, #e58e26 100%)"))

        def _bar(v):
            """Signed [-1, +1] score -> linear progress value/color."""
            v = max(-1.0, min(1.0, float(v or 0.0)))
            color = "#00d4aa" if v > 0.1 else ("#ff6b6b" if v < -0.1 else "#f6b93b")
            return (v + 1.0) / 2.0, color

        with ui.card().classes('w-full').style('background-color: #1e2a3a'):
            # ---- header ---------------------------------------------------
            with ui.card_section().style(f'background: {header_bg}'):
                ui.label('Deterministic Multi-Section Scorer').classes(
                    'text-h5 text-weight-bold').style('color: white')
                ui.label(f'{market_analysis.symbol} — technical · fundamental · analyst · macro').style(
                    'color: rgba(255,255,255,0.8)')

            # ---- signal row -------------------------------------------------
            with ui.card_section():
                with ui.row().classes('w-full items-center justify-between'):
                    with ui.column().classes('gap-0'):
                        ui.label('Recommendation').classes('text-caption').style('color: #a0aec0')
                        with ui.row().classes('items-center gap-2'):
                            ui.icon(sig_icon, color=sig_color, size='2rem')
                            ui.label(signal).classes('text-h5 text-weight-bold').style(f'color: {sig_color}')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Final score').classes('text-caption').style('color: #a0aec0')
                        ui.label(f'{final:+.3f}').classes('text-h5 text-weight-bold').style(
                            f'color: {sig_color}')
                    with ui.column().classes('gap-0 items-end'):
                        ui.label('Confidence').classes('text-caption').style('color: #a0aec0')
                        ui.label(f'{confidence:.0f}%').classes('text-h5 text-weight-bold').style(
                            'color: #e2e8f0')

            # ---- section scores ---------------------------------------------
            with ui.card_section():
                ui.label('Section scores').classes('text-subtitle1 text-weight-bold').style(
                    'color: #e2e8f0')
                sections = [
                    ("Technical (momentum/trend/RSI/Donchian)", scores.get("technical")),
                    ("Fundamental (F-Score/Z/quality/value/growth)", scores.get("fundamental")),
                    ("Analyst (dated rating revisions)", scores.get("analyst")),
                    ("Macro regime composite", scores.get("regime")),
                ]
                for label, value in sections:
                    v = float(value or 0.0)
                    frac, color = _bar(v)
                    with ui.row().classes('w-full items-center gap-2').style('margin-top: 6px'):
                        ui.label(label).classes('text-caption').style(
                            'color: #a0aec0; width: 280px')
                        ui.linear_progress(value=frac, color=color, show_value=False).classes(
                            'flex-grow').style('height: 8px')
                        ui.label(f'{v:+.2f}').classes('text-caption text-weight-bold').style(
                            f'color: {color}; width: 46px; text-align: right')
                mult = scores.get("exposure_multiplier")
                if mult is not None:
                    ui.label(f'Macro exposure multiplier: x{float(mult):.2f}').classes(
                        'text-caption').style('color: #a0aec0; margin-top: 8px')
                if scores.get("veto"):
                    with ui.row().classes('w-full items-center gap-2').style(
                            'margin-top: 8px; background-color: rgba(255,107,107,0.12);'
                            ' border-radius: 6px; padding: 6px 10px'):
                        ui.icon('warning', color='#ff6b6b')
                        ui.label('Fundamental distress veto applied (score capped)').style(
                            'color: #ff6b6b')

            # ---- levels -------------------------------------------------------
            with ui.card_section():
                ui.label('Levels (ATR-based)').classes('text-subtitle1 text-weight-bold').style(
                    'color: #e2e8f0')
                with ui.row().classes('w-full gap-6'):
                    for caption, key, fmt_color in (
                            ('Target', 'target_price', '#00d4aa'),
                            ('Stop', 'stop_price', '#ff6b6b'),
                            ('ATR(14)', 'atr', '#a0aec0')):
                        value = levels.get(key)
                        with ui.column().classes('gap-0'):
                            ui.label(caption).classes('text-caption').style('color: #a0aec0')
                            ui.label(f"{float(value):.2f}" if value else "—").classes(
                                'text-h6 text-weight-bold').style(f'color: {fmt_color}')
                if rec.get("expected_profit_percent"):
                    ui.label(f'Expected profit: {float(rec["expected_profit_percent"]):.1f}%').classes(
                        'text-caption').style('color: #a0aec0; margin-top: 6px')

            # ---- details -------------------------------------------------------
            if rec.get("details"):
                with ui.card_section():
                    ui.label('Details').classes('text-subtitle1 text-weight-bold').style(
                        'color: #e2e8f0')
                    ui.label(str(rec["details"])).style(
                        'color: #a0aec0; white-space: pre-wrap')

        # ---- markdown summary (PDF export / audit trail) -----------------------
        lines = [
            f"**DeterministicScorer** — {signal} (confidence {confidence:.0f}%)",
            f"final score {final:+.3f} = "
            f"T {float(scores.get('technical') or 0):+.2f} · "
            f"F {float(scores.get('fundamental') or 0):+.2f} · "
            f"A {float(scores.get('analyst') or 0):+.2f} · "
            f"regime {float(scores.get('regime') or 0):+.2f}",
        ]
        if scores.get("veto"):
            lines.append("Fundamental distress veto applied (score capped).")
        if levels.get("target_price"):
            lines.append(
                f"target {float(levels['target_price']):.2f} · "
                f"stop {float(levels.get('stop_price') or 0):.2f} · "
                f"ATR {float(levels.get('atr') or 0):.2f}")
        return "\n".join(lines)


def _scalar_series(value: float):
    """Wrap a scalar spread observation as a 1-element pandas Series for the
    regime calculators."""
    import pandas as pd
    return pd.Series([float(value)])
