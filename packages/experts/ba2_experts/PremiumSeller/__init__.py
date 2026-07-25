"""PremiumSeller — systematic short-premium option income expert (spec:
docs/superpowers/specs/2026-07-24-premium-seller-expert-design.md).

Sells defined-risk put credit spreads (and, when enabled, naked puts / short
strangles under stricter sub-rails) on a static large-cap universe, gated by
GA-tunable entry signals (IVR, IV-HV spread, SMA trend, earnings exclusion,
FMP-rating floor) and managed by GA-tunable exit signals (profit capture,
tested-delta, roll-DTE, credit-multiple stops, circuit breaker). Bypasses the
classic RM: lifecycle is owned by OptionPortfolioManager.
"""

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ba2_common.core.backtest_context import BacktestContext
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation
from ba2_common.logger import logger

from ba2_experts.PremiumSeller import signals, structures

_IV_SEED_WEEKS = 52          # weekly ATM-IV samples for the IVR history seed
_CHAIN_DTE_PAD = (10, 15)    # chain window around target DTE: [t-10, t+15]


class PremiumSeller(MarketExpertInterface):
    """RM-bypass option income expert (backtest-only in v1)."""

    bypasses_classic_rm: bool = True
    manages_between_entries: bool = True
    portfolio_manager_classpath: str = "ba2_experts.PremiumSeller.portfolio.OptionPortfolioManager"
    BACKTEST_WARMUP_BARS: int = 300     # SMA-200/HV lookback floor (FactorRanker pattern)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._iv_history: Dict[str, List[float]] = {}
        self._context: Optional[BacktestContext] = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    @classmethod
    def description(cls) -> str:
        return ("Systematic option premium seller: GA-tuned entry signals (IVR, "
                "IV-HV, trend, earnings, rating) and exits on short option structures")

    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        return {
            "can_recommend_instruments": True,
            "should_expand_instrument_jobs": False,
            "required_instrument_selection_method": "expert",
            "schedules_open_positions": False,
            "uses_risk_manager": False,
        }

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "static_universe": {"type": "str", "required": True, "default": "",
                                "description": "Comma-separated underlyings (large caps)."},
            "iv_rank_enabled": {"type": "bool", "required": False, "default": True,
                                "description": "Entry gate: only sell when IVR >= iv_rank_min."},
            "iv_rank_min": {"type": "float", "required": False, "default": 50.0,
                            "description": "IV rank threshold (0-100)."},
            "iv_hv_enabled": {"type": "bool", "required": False, "default": False,
                              "description": "Entry gate: only sell when IV-HV >= iv_hv_min_pp."},
            "iv_hv_min_pp": {"type": "float", "required": False, "default": 2.0,
                             "description": "Min implied-minus-realized vol spread (vol points)."},
            "hv_lookback": {"type": "int", "required": False, "default": 20,
                            "description": "Realized-vol lookback (trading days)."},
            "trend_filter_enabled": {"type": "bool", "required": False, "default": False,
                                     "description": "Only sell puts above the SMA(trend_sma)."},
            "trend_sma": {"type": "int", "required": False, "default": 200,
                          "description": "Trend filter SMA period."},
            "earnings_filter_enabled": {"type": "bool", "required": False, "default": True,
                                        "description": "Exclude earnings inside the DTE window."},
            "fmp_rating_floor_enabled": {"type": "bool", "required": False, "default": False,
                                         "description": "Exclude names graded below fmp_rating_min."},
            "fmp_rating_min": {"type": "float", "required": False, "default": 3.0,
                               "description": "Min analyst grade score (1-5)."},
            "min_volume": {"type": "int", "required": False, "default": 25,
                           "description": "Minimum daily traded volume for a contract to be "
                                          "selectable. The fill engine caps an order at 10% of "
                                          "a bar's volume, so a thinner contract yields an "
                                          "order that can never fill. 0 disables (the "
                                          "unconditional premium floor still applies)."},
            "target_delta": {"type": "float", "required": False, "default": 0.30,
                             "description": "Short-strike target |delta|."},
            "target_dte": {"type": "int", "required": False, "default": 38,
                           "description": "Target days to expiry."},
            "spread_width": {"type": "float", "required": False, "default": 5.0,
                             "description": "Put credit spread width ($)."},
            "min_credit_ratio": {"type": "float", "required": False, "default": 0.10,
                                 "description": "Min credit / width ratio."},
            "enable_put_credit_spread": {"type": "bool", "required": False, "default": True,
                                         "description": "Allow put credit spreads."},
            "enable_short_put": {"type": "bool", "required": False, "default": False,
                                 "description": "Allow naked short puts (undefined-risk rails)."},
            "enable_short_strangle": {"type": "bool", "required": False, "default": False,
                                      "description": "Allow short strangles (undefined-risk rails)."},
            "risk_per_structure_pct": {"type": "float", "required": False, "default": 3.0,
                                       "description": "Risk budget per structure (% of balance)."},
            "profit_capture_pct": {"type": "float", "required": False, "default": 50.0,
                                   "description": "Exit: % of max credit captured."},
            "strangle_capture_pct": {"type": "float", "required": False, "default": 25.0,
                                     "description": "Exit: % captured for strangles."},
            "tested_delta_enabled": {"type": "bool", "required": False, "default": False,
                                     "description": "Exit: close when short leg |delta| >= tested_delta."},
            "tested_delta": {"type": "float", "required": False, "default": 0.30,
                             "description": "Tested-side delta threshold."},
            "roll_dte": {"type": "int", "required": False, "default": 21,
                         "description": "Exit: close when remaining DTE <= this."},
            "dr_stop_enabled": {"type": "bool", "required": False, "default": False,
                                "description": "Exit: defined-risk stop at N x credit loss."},
            "dr_stop_credit_mult": {"type": "float", "required": False, "default": 2.0,
                                    "description": "Defined-risk stop multiple of credit."},
            "ur_stop_enabled": {"type": "bool", "required": False, "default": True,
                                "description": "Exit: undefined-risk stop at N x credit loss."},
            "ur_stop_credit_mult": {"type": "float", "required": False, "default": 2.0,
                                    "description": "Undefined-risk stop multiple of credit."},
            "max_deployment_pct": {"type": "float", "required": False, "default": 40.0,
                                   "description": "Max committed capital (% of balance)."},
            "undefined_risk_max_pct": {"type": "float", "required": False, "default": 20.0,
                                       "description": "Max naked committed (% of balance, notional basis)."},
            "max_notional_leverage": {"type": "float", "required": False, "default": 3.0,
                                      "description": "Max short notional / balance."},
            "max_concurrent_structures": {"type": "int", "required": False, "default": 10,
                                          "description": "Max open structures."},
            "circuit_breaker_pct": {"type": "float", "required": False, "default": 20.0,
                                    "description": "Flatten book when balance drawdown exceeds this %."},
        }

    # ------------------------------------------------------------------
    # Live path: not in v1 (spec §11)
    # ------------------------------------------------------------------
    def run_analysis(self, *args, **kwargs):
        raise NotImplementedError("PremiumSeller is backtest-only in v1 (spec §11)")

    def render_market_analysis(self, market_analysis) -> str:
        """Abstract-method satisfaction only — no live UI in v1 (spec §11)."""
        raise NotImplementedError("PremiumSeller is backtest-only in v1 (spec §11)")

    # ------------------------------------------------------------------
    # Backtest path
    # ------------------------------------------------------------------
    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation:
        settings = self._resolved_settings(context)
        self._context = context      # _fetch_closes reads providers off the instance
        specs = []
        for sym in self._universe(settings):
            spec = self._evaluate_symbol(sym, as_of, context, settings)
            if spec is not None:
                specs.append(spec)
            if len(specs) >= int(settings["max_concurrent_structures"]):
                break
        if not specs:
            return Recommendation(OrderRecommendation.HOLD, 0.0, None,
                                  "No structures passed the entry gates",
                                  raw_outputs={"targets": {"structures": []}})
        return Recommendation(OrderRecommendation.OVERWEIGHT, 0.0, None,
                              f"PremiumSeller: {len(specs)} candidate structures",
                              raw_outputs={"targets": {"structures": specs},
                                           "name": "PremiumSeller targets",
                                           "type": "option_income"})

    def _resolved_settings(self, context: BacktestContext) -> Dict[str, Any]:
        """Class-definition defaults under the engine/GA-resolved settings — the
        GA's model:* genes arrive via context.settings and always win."""
        resolved = {k: v["default"] for k, v in self.get_settings_definitions().items()
                    if "default" in v}
        resolved.update(context.settings or {})
        return resolved

    def _universe(self, settings: Dict[str, Any]) -> List[str]:
        raw = settings["static_universe"]
        if isinstance(raw, str):
            return [s.strip().upper() for s in raw.split(",") if s.strip()]
        return [str(s).upper() for s in raw]

    # -- per-symbol pipeline (spec §4) ----------------------------------
    def _evaluate_symbol(self, sym: str, as_of: datetime, context: BacktestContext,
                         settings: Dict[str, Any]):
        account = context.account
        target_dte = int(settings["target_dte"])

        # Chain FIRST: no chain -> nothing to do (cheapest rejection).
        d0 = as_of.date() + timedelta(days=target_dte - _CHAIN_DTE_PAD[0])
        d1 = as_of.date() + timedelta(days=target_dte + _CHAIN_DTE_PAD[1])
        chain = account.get_option_chain(sym, d0, d1)
        if not chain:
            return None
        # Liquidity filter BEFORE strike selection (2026-07-25). PremiumSeller picks strikes
        # with its own closest_to_delta and never went through option_selector, so it was the
        # one option path with no tradability guard: it could select a $0.03 contract, or one
        # trading 2 lots/day that the fill engine's participation cap would then refuse. See
        # structures.filter_tradeable.
        chain = structures.filter_tradeable(chain, int(settings["min_volume"]) or None)
        if not chain:
            return None

        iv_now = account.get_atm_implied_volatility(sym)
        self._update_iv_history(sym, as_of, account, iv_now)
        ivr = signals.iv_rank(self._iv_history.get(sym, []), iv_now)

        if settings["iv_rank_enabled"]:
            if ivr is None or ivr < float(settings["iv_rank_min"]):
                return None

        closes = None
        if settings["iv_hv_enabled"] or settings["trend_filter_enabled"]:
            closes = self._fetch_closes(sym, as_of, settings)
        if settings["iv_hv_enabled"]:
            hv = signals.realized_vol_annualized(closes or [], int(settings["hv_lookback"]))
            if hv is None or iv_now is None or (iv_now - hv) * 100.0 < float(settings["iv_hv_min_pp"]):
                return None
        if settings["trend_filter_enabled"]:
            avg = signals.sma(closes or [], int(settings["trend_sma"]))
            spot = closes[-1] if closes else None
            if avg is None or spot is None or spot < avg:
                return None
        if settings["earnings_filter_enabled"] and self._earnings_blocked(sym, as_of, target_dte):
            return None
        if settings["fmp_rating_floor_enabled"] and self._rating_blocked(sym, as_of, settings):
            return None

        equity = account.get_balance()
        if equity is None or equity <= 0:
            return None
        risk_budget = equity * float(settings["risk_per_structure_pct"]) / 100.0
        max_notional = float(settings["max_notional_leverage"]) * equity

        builders = []
        if settings["enable_put_credit_spread"]:
            builders.append(lambda: structures.build_put_credit_spread(
                sym, chain, as_of.date(), target_dte, -abs(float(settings["target_delta"])),
                float(settings["spread_width"]), float(settings["min_credit_ratio"]), risk_budget))
        if settings["enable_short_put"]:
            builders.append(lambda: structures.build_short_put(
                sym, chain, as_of.date(), target_dte, -abs(float(settings["target_delta"])),
                risk_budget, max_notional))
        if settings["enable_short_strangle"]:
            builders.append(lambda: structures.build_short_strangle(
                sym, chain, as_of.date(), target_dte, abs(float(settings["target_delta"])),
                risk_budget, max_notional))
        for build in builders:
            spec = build()
            if spec is not None:
                return spec
        return None

    # -- data helpers ----------------------------------------------------
    def _update_iv_history(self, sym: str, as_of: datetime, account, iv_now) -> None:
        hist = self._iv_history.setdefault(sym, [])
        provider = getattr(account, "options_provider", None)
        if not hist and provider is not None:
            # Cold start: seed ~1y of weekly ATM-IV points from the OPRA cache
            # (backtest only; live would sample forward bar by bar).
            for w in range(_IV_SEED_WEEKS, 0, -1):
                d = (as_of - timedelta(weeks=w)).date()
                try:
                    v = provider.get_atm_iv(sym, d)
                except Exception:
                    v = None
                if v is not None:
                    hist.append(v)
        if iv_now is not None:
            hist.append(iv_now)
        del hist[: max(0, len(hist) - 5 * _IV_SEED_WEEKS)]

    def _fetch_closes(self, sym: str, as_of: datetime,
                      settings: Dict[str, Any]) -> Optional[List[float]]:
        """Recent daily closes via the OHLCV provider. get_ohlcv_data's contract is a
        single pd.DataFrame with a capital-C "Close" column (FactorRanker consumes it
        the same way). None on any missing data — the gates treat None as "cannot
        evaluate" and skip, never a fabricated number."""
        lookback = max(int(settings["trend_sma"]), int(settings["hv_lookback"])) + 10
        data = self._context.providers.ohlcv().get_ohlcv_data(
            sym, end_date=as_of, lookback_days=int(lookback * 1.5) + 10, interval="1d")
        if data is None or getattr(data, "empty", True) or "Close" not in getattr(data, "columns", []):
            return None
        closes = [c for c in data["Close"].tolist()
                  if c is not None and not (isinstance(c, float) and math.isnan(c))]
        return closes or None

    def _earnings_blocked(self, sym: str, as_of: datetime, target_dte: int) -> bool:
        """Exclude when a report lands inside (as_of, as_of + DTE window] (spec §4.2).

        Approximation: the eventual REPORT date from the as_of-clamped statements
        cache stands in for the scheduled date (schedules drift by days; the
        window is 30-45 DTE). No point-in-time scheduled-calendar source exists
        in the platform (the fmpsdk bulk calendar is live-only, see
        FMPEarningsDrift module docstring)."""
        from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
            FMPCompanyDetailsProvider,
        )
        window_end = as_of + timedelta(days=target_dte + 5)
        try:
            res = FMPCompanyDetailsProvider().get_past_earnings(
                sym, "quarterly", window_end, lookback_periods=3, format_type="dict")
        except Exception as e:
            logger.warning(f"PremiumSeller: earnings check failed for {sym}: {e}")
            return False                    # data unavailable -> do not block
        rows = res.get("earnings") if isinstance(res, dict) else None
        if isinstance(rows, dict):
            rows = [rows]
        dates = []
        for r in rows or []:
            try:
                dates.append(datetime.fromisoformat(str(r.get("report_date"))[:10]).date())
            except (TypeError, ValueError):
                continue
        return signals.earnings_within(dates, as_of.date(), target_dte + 5)

    def _rating_blocked(self, sym: str, as_of: datetime, settings: Dict[str, Any]) -> bool:
        """True iff the latest grades-historical row on/before as_of scores below
        the floor. stable/grades-historical rows carry AGGREGATE StrongBuy..StrongSell
        counts (no per-grade strings), scored by signals.analyst_counts_score.
        Rows with no usable counts do not block (the floor only excludes
        KNOWN-bad names). Point-in-time safe: only rows dated on/before as_of
        are considered."""
        from ba2_common.config import get_app_setting
        from ba2_common.core.provider_utils import parse_provider_date
        from ba2_experts.FMPRating import fetch_grades_historical_cached

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            return False
        try:
            rows = fetch_grades_historical_cached(api_key, sym) or []
        except Exception as e:
            logger.warning(f"PremiumSeller: rating fetch failed for {sym}: {e}")
            return False
        best = None
        for r in rows:
            d = parse_provider_date(r.get("date")) if isinstance(r, dict) else None
            if d is None or d.date() > as_of.date():
                continue
            if best is None or d.date() > best[0]:
                best = (d.date(), r)
        if best is None:
            return False
        score = signals.analyst_counts_score(best[1])
        if score is None:
            return False
        return score < float(settings["fmp_rating_min"])
