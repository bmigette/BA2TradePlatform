"""Monthly ETF membership selection with an absolute trend gate and shared rules.

Ranks the last completed month's prices, holds the selected membership constant
through the current month, and emits SELL for funds outside that membership.
The ordinary rules/RM own orders and protection; this expert never submits orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from zoneinfo import ZoneInfo

import pandas as pd

from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon)
from ba2_common.logger import get_expert_logger


def monthly_selection(histories, as_of, settings):
    """Pure, causal ranking. Missing/stale/nonpositive prices fail the whole basket."""
    universe = settings["universe_symbols"]
    if not isinstance(universe, list) or not universe or len(set(universe)) != len(universe):
        raise ValueError("universe_symbols must contain distinct ticker strings")
    lookback, trend, top_n = (int(settings[k]) for k in ("momentum_bars", "trend_bars", "top_n"))
    if min(lookback, trend, top_n) < 1 or top_n > len(universe):
        raise ValueError("Invalid lookback, trend length or number of holdings")
    local = as_of.astimezone(ZoneInfo("America/New_York")) if as_of.tzinfo else as_of
    today = local.date()
    anchor = today.replace(day=1)
    scores, prices, dates = {}, {}, {}
    for symbol in universe:
        frame = histories[symbol]
        if frame is None or frame.empty:
            raise ValueError(f"Missing ETF history: {symbol}")
        frame = frame.copy()
        frame["Date"] = pd.to_datetime(frame["Date"], utc=True)
        frame = frame.sort_values("Date")
        # Daily candles are date-labelled. Even at 09:30 their final close must
        # never enter a decision; retain only earlier calendar trading dates.
        frame = frame[frame["Date"].dt.date < today]
        if frame.empty or frame["Date"].duplicated().any():
            raise ValueError(f"Missing or duplicate completed candles: {symbol}")
        close = pd.to_numeric(frame["Close"], errors="raise")
        if not all(math.isfinite(v) and v > 0 for v in close):
            raise ValueError(f"Invalid ETF closes: {symbol}")
        if (today - frame["Date"].iloc[-1].date()).days > 7:
            raise ValueError(f"Stale current ETF history: {symbol}")
        prices[symbol] = float(close.iloc[-1])
        month = frame[frame["Date"].dt.date < anchor]
        if len(month) < max(lookback + 1, trend):
            raise ValueError(f"Insufficient ETF warmup: {symbol}, {len(month)} bars before {anchor}; "
                             f"need {max(lookback + 1, trend)} (analysis date {today})")
        dates[symbol] = month["Date"].iloc[-1].date().isoformat()
        if (anchor - month["Date"].iloc[-1].date()).days > 7:
            raise ValueError(f"Stale monthly anchor: {symbol}")
        close = month["Close"].astype(float)
        momentum = float(close.iloc[-1] / close.iloc[-lookback - 1] - 1)
        above_trend = bool(close.iloc[-1] > close.iloc[-trend:].mean())
        scores[symbol] = {"momentum": momentum, "above_trend": above_trend,
                          "eligible": momentum > 0 and above_trend}
    if len(set(dates.values())) != 1:
        raise ValueError("ETF monthly candles have inconsistent anchor dates")
    eligible = [s for s in universe if scores[s]["eligible"]]
    selected = sorted(eligible, key=lambda s: (-scores[s]["momentum"], s))[:top_n]
    return {"selected": selected, "scores": scores, "prices": prices,
            "anchor_dates": dates, "selection_month": anchor.isoformat()}


class ETFTrend(MarketExpertInterface):
    @classmethod
    def description(cls):
        return "Monthly ETF momentum ranking, positive-trend gate and cash when none qualifies"

    @classmethod
    def get_settings_definitions(cls):
        return {
            "universe_symbols": {"type": "json", "required": True,
                                 "default": ["SPY", "IEF", "TLT", "GLD"],
                                 "description": "Fixed research universe; use the same symbols in the instance instrument list"},
            "momentum_bars": {"type": "int", "required": True, "default": 252,
                              "description": "Trading-bar momentum lookback at prior month-end"},
            "trend_bars": {"type": "int", "required": True, "default": 200,
                           "description": "Moving-average positive trend filter at prior month-end"},
            "top_n": {"type": "int", "required": True, "default": 2,
                      "description": "Maximum eligible funds in the monthly selection"},
        }

    _SETTING_KEYS = ("universe_symbols", "momentum_bars", "trend_bars", "top_n")

    def __init__(self, id):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("ETFTrend", id)

    def _analyze(self, symbol, providers, settings, as_of):
        if symbol not in settings["universe_symbols"]:
            raise ValueError(f"{symbol} is outside universe_symbols")
        calendar_days = math.ceil(max(settings["momentum_bars"] + 1, settings["trend_bars"]) * 7 / 5) + 120
        provider = providers.ohlcv()
        histories = {s: provider.get_ohlcv_data(s, end_date=as_of, lookback_days=calendar_days, interval="1d")
                     for s in settings["universe_symbols"]}
        result = monthly_selection(histories, as_of, settings)
        selected = symbol in result["selected"]
        return Recommendation(
            signal=OrderRecommendation.BUY if selected else OrderRecommendation.SELL,
            confidence=100.0, current_price=result["prices"][symbol],
            expected_profit_percent=0.0,  # no price target/return forecast; rank membership is deterministic
            details=f"Monthly membership for {result['selection_month']}: {result['selected']}. "
                    "Confidence denotes a satisfied rule, not a probability of profit.",
            raw_outputs=result)

    def analyze_as_of(self, as_of, context):
        # The daily engine's entry pass sets _gather_symbol; its management pass
        # and the newer context callers also carry extra['symbol'].
        symbol = context.extra["symbol"] if "symbol" in context.extra else self._gather_symbol
        try:
            return self._analyze(symbol, context.providers, context.settings, as_of)
        except (ValueError, KeyError) as exc:
            # The engine treats these provider-data exceptions as fatal. An ordinary
            # ValueError would be logged and skipped, turning incomplete baskets into
            # plausible-looking cash-only backtests.
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            raise FMPHistoryCacheMiss(f"ETFTrend cannot evaluate the complete basket: {exc}") from exc

    def run_analysis(self, symbol, market_analysis):
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            rec = self._analyze(symbol, self._live_providers(), self._resolve_settings(self._SETTING_KEYS),
                                datetime.now(timezone.utc))
            add_instance(ExpertRecommendation(
                instance_id=self.id, symbol=symbol, market_analysis_id=market_analysis.id,
                recommended_action=rec.signal, expected_profit_percent=0.0,
                price_at_date=rec.current_price, confidence=rec.confidence, details=rec.details,
                risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.LONG_TERM,
                subtype=market_analysis.subtype, data={"ETFTrend": rec.raw_outputs}))
            market_analysis.state = {"ETFTrend": rec.raw_outputs}
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
        except Exception as exc:
            self.logger.error("ETFTrend analysis failed for %s: %s", symbol, exc, exc_info=True)
            market_analysis.status = MarketAnalysisStatus.FAILED
            market_analysis.state = {"error": str(exc)}
            update_instance(market_analysis)

    def render_market_analysis(self, market_analysis):
        return json.dumps(market_analysis.state, indent=2)
