"""
FMP Senate/House Trade Expert (Weighted Algorithm)

Expert that analyzes government official trading activity using FMP's
Senate Trading API to generate trading recommendations based on:
- Recent trades by senators and representatives
- Historical performance of those traders
- Size of investment (confidence boost for larger trades)
- Weighted algorithm that considers portfolio allocation percentages

API Documentation: https://site.financialmodelingprep.com/developer/docs#senate-trading
"""

from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import bisect
import json
import os
import re
import requests

from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ba2_common.core.db import get_db, update_instance, add_instance
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.logger import get_expert_logger
from ba2_common.config import get_app_setting
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin, FMPCongressTradingMixin
# NOTE: parse_fmp_amount_range / calculate_fmp_trade_metrics are imported LOCALLY inside
# methods to avoid a circular import (core.utils -> modules.experts -> ...).


@lru_cache(maxsize=None)
def _parse_ymd_utc(date_str: str) -> datetime:
    """Parse a "%Y-%m-%d" trade-date string as UTC midnight.

    Profiled a 6-month/3-symbol backtest at 1336s total; ``datetime.strptime`` alone
    (re-resolving the C locale on every call) accounted for ~570s of that across ~17.5M
    calls, almost all of them re-parsing the SAME handful of disclosure/exec date strings
    that recur across every bar and every GA trial. ``fromisoformat`` skips locale
    resolution entirely, and memoizing collapses the repeats to one parse per unique
    string (bounded: real trade dates, at most a few thousand distinct values ever)."""
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)


class FMPSenateTraderWeight(AnalysisStatusRenderMixin, FMPCongressTradingMixin, MarketExpertInterface):
    """
    FMPSenateTraderWeight Expert Implementation
    
    Expert that uses FMP's Senate/House trading data to generate recommendations
    using a sophisticated weighted algorithm. Analyzes government official trades
    for a symbol, evaluates trader performance history, and calculates confidence based on:
    1. Portfolio allocation percentages (symbol focus)
    2. Historical trader performance
    3. Investment size and timing
    """
    
    RENDER_PENDING_MESSAGE = 'Senate trade analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Fetching senate/house trading data for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "Government official trading activity analysis using weighted algorithm based on portfolio allocation"
    
    def __init__(self, id: int):
        """Initialize FMPSenateTraderWeight expert with database instance."""
        super().__init__(id)
        
        self._load_expert_instance(id)
        self._api_key = self._get_fmp_api_key()
        
        # Initialize expert-specific logger
        self.logger = get_expert_logger("FMPSenateTraderWeight", id)
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FMPSenateTraderWeight expert."""
        return {
            "max_disclose_date_days": {
                "type": "int", 
                "required": True, 
                "default": 30,
                "description": "Maximum days since trade disclosure",
                "tooltip": "Trades disclosed more than this many days ago will be filtered out. Lower values focus on recent activity."
            },
            "max_trade_exec_days": {
                "type": "int",
                "required": True,
                "default": 60,
                "description": "Maximum days since trade execution",
                "tooltip": "Trades executed more than this many days ago will be filtered out. Helps focus on recent trading activity."
            },
            "max_trade_price_delta_pct": {
                "type": "float",
                "required": True,
                "default": 10.0,
                "description": "Maximum price change since trade (%)",
                "tooltip": "Trades where the price has already moved more than this percentage will be filtered out (opportunity may be gone)."
            },
            "growth_confidence_multiplier": {
                "type": "float",
                "required": True,
                "default": 5.0,
                "description": "Portfolio allocation to confidence multiplier",
                "tooltip": "Multiplier applied to trader's portfolio allocation % on the symbol. Formula: 50 + (avg_symbol_focus% * multiplier) + (price_movement / 2). Symbol focus is capped at 10%. Example: 10% allocation * 5.0 = 50% bonus → 100% confidence."
            },
            "confidence_to_profit_factor": {
                "type": "float",
                "required": True,
                "default": 0.15,
                "description": "Confidence to profit factor",
                "tooltip": "Factor to convert confidence to expected profit. Default 0.15 means 100% confidence = 15% expected profit. Formula: expected_profit = confidence * factor."
            },
            "min_traders": {
                "type": "int",
                "required": True,
                "default": 2,
                "description": "Minimum unique traders required",
                "tooltip": "Minimum number of unique traders that must have traded the symbol. Both min_traders and min_trades must be met. Default 2: need at least 2 different traders."
            },
            "min_trades": {
                "type": "int",
                "required": True,
                "default": 2,
                "description": "Minimum total trades required",
                "tooltip": "Minimum number of total trades required for the symbol. Both min_traders and min_trades must be met. Default 2: need at least 2 trades total."
            },
            "min_trade_amount": {
                "type": "float",
                "required": True,
                "default": 0.0,
                "description": "Minimum trade dollar amount ($)",
                "tooltip": "Trades below this dollar amount (midpoint of the FMP disclosure range) are filtered out. Small trades carry little conviction. 0 = no minimum."
            },
            "sell_signal_weight": {
                "type": "float",
                "required": True,
                "default": 1.0,
                "description": "Weight of SELL disclosures in the signal (0-1)",
                "tooltip": "Congressional sells are often liquidity/diversification-driven (weak signal) while buys carry information. This scales the sell side's portfolio-focus total in the signal. 1.0 = symmetric (legacy), 0.5 = half-weight sells, 0 = ignore sells."
            },
            "symbol_focus_cap_pct": {
                "type": "float",
                "required": True,
                "default": 10.0,
                "description": "Symbol portfolio-focus cap (%)",
                "tooltip": "A trader's symbol focus (their $ on this symbol / total $ traded, yearly) is capped here before feeding confidence. Raising the cap rewards very concentrated conviction more."
            },
            "size_boost_max": {
                "type": "float",
                "required": True,
                "default": 20.0,
                "description": "Max confidence boost from trade size (points)",
                "tooltip": "Confidence points added when the winning side's average trade size reaches size_boost_full_amount. Scales linearly below that. 0 disables the size term."
            },
            "size_boost_full_amount": {
                "type": "float",
                "required": True,
                "default": 1000000.0,
                "description": "Trade size for full size boost ($)",
                "tooltip": "Dollar amount at which a trade earns the full size_boost_max confidence points (default $1M). A $500k trade with defaults earns half the max."
            },
            "consensus_bonus_per_trader": {
                "type": "float",
                "required": True,
                "default": 2.0,
                "description": "Confidence bonus per extra unique trader (points)",
                "tooltip": "Each unique trader on the winning side beyond the first adds this many confidence points (breadth of consensus). 0 disables."
            },
            "consensus_bonus_max": {
                "type": "float",
                "required": True,
                "default": 10.0,
                "description": "Max consensus bonus (points)",
                "tooltip": "Cap on the total consensus bonus from unique-trader count."
            },
            "skill_horizon_days": {
                "type": "int",
                "required": True,
                "default": 60,
                "description": "Trader-skill forward-return horizon (days)",
                "tooltip": "Each trader's PAST disclosed buys are scored by the symbol's return over this many days after execution. Only windows fully in the past are scored (no lookahead)."
            },
            "skill_min_past_trades": {
                "type": "int",
                "required": True,
                "default": 5,
                "description": "Min scored past trades for a skill rating",
                "tooltip": "Traders with fewer scored past buys than this get a NEUTRAL skill (0) — not enough history to judge."
            },
            "skill_max_past_trades": {
                "type": "int",
                "required": True,
                "default": 50,
                "description": "Max past trades scored per trader",
                "tooltip": "Most-recent cap on how many past buys are scored per trader (bounds price-history fetches)."
            },
            "skill_lookback_months": {
                "type": "int",
                "required": True,
                "default": 12,
                "description": "Skill scoring lookback window (months)",
                "tooltip": "Only past buys executed within this many months of the as-of date are eligible for skill scoring — a trader's recent pattern is more relevant than activity from years ago, and it bounds the scan for a very prolific trader (e.g. a member of Congress with 10k+ disclosed trades) to a manageable recent window. Combined with skill_max_past_trades (whichever is more restrictive wins)."
            },
            "skill_signal_weight": {
                "type": "float",
                "required": True,
                "default": 0.5,
                "description": "Trader-skill weight in the signal (0-1)",
                "tooltip": "Each trade's signal contribution is multiplied by (1 + skill x this). Skill is -1..+1 from the trader's historical hit rate. 0 disables skill weighting of the signal."
            },
            "skill_confidence_weight": {
                "type": "float",
                "required": True,
                "default": 10.0,
                "description": "Trader-skill confidence adjustment (points)",
                "tooltip": "Confidence points added/subtracted per unit of the winning side's average skill (-1..+1). Default 10: a perfect-hit-rate cohort adds +10, a always-wrong cohort subtracts 10. 0 disables."
            },
            "min_trader_avg_hold_days": {
                "type": "float",
                "required": True,
                "default": 0.0,
                "description": "Min average holding period to trust a trader (days)",
                "tooltip": "Excludes a trader's trades when their historical average buy->sell round-trip (FIFO-paired, same symbol) is below this many days AND they have >= min_trader_hold_roundtrips scored round-trips. Filters out intraday/scalper-like disclosure patterns (e.g. a member with 10k+ disclosed trades) that don't reflect conviction positions. 0 = disabled (no filter). Costs zero extra fetches (computed from the same disclosure history already fetched for skill scoring)."
            },
            "min_trader_hold_roundtrips": {
                "type": "int",
                "required": True,
                "default": 3,
                "description": "Min round-trips required to judge a trader's hold time",
                "tooltip": "A trader with fewer FIFO-paired buy/sell round-trips than this is NOT filtered by min_trader_avg_hold_days (not enough history to judge — treated as pass, not scalper)."
            }
        }
    
    # ------------------------------------------------------------------
    # Backtest contract (Phase 1): _gather (two-stage provider I/O) + _process
    # (pure). The SAME pair runs live (run_analysis, as_of=None) and in backtest
    # (analyze_as_of, as_of=<date>).
    #
    # This is the riskiest expert because the live decision functions INTERLEAVE
    # fetches: _filter_trades called _get_price_at_date per trade and
    # _calculate_recommendation called _fetch_trader_history per trade. _gather
    # PRE-RESOLVES those two maps (exec_price_by_trade, trader_history_by_name)
    # so _process is pure (reads the maps, never calls a provider/HTTP). The
    # trader-history map is sliced to disclosure <= as_of (no-lookahead); with
    # as_of=None it is the full live history (byte-identical to the pre-refactor
    # path, which used datetime.now()).
    #
    # current_price: LIVE (as_of=None) reads _get_current_price's account quote
    # (the original live source); BACKTEST (as_of set) reads providers.price_at_date
    # (OHLCV as_of close). exec prices (per trade) still come from the
    # already-date-aware _get_price_at_date(symbol, date).
    #
    # symbol is resolved BEFORE _gather and stashed on self._gather_symbol: live
    # run_analysis sets it from its symbol arg; analyze_as_of sets it from
    # context.extra["symbol"].
    # ------------------------------------------------------------------
    _SETTING_KEYS = (
        "max_disclose_date_days", "max_trade_exec_days", "max_trade_price_delta_pct",
        "growth_confidence_multiplier", "confidence_to_profit_factor",
        "min_traders", "min_trades",
        "min_trade_amount", "sell_signal_weight", "symbol_focus_cap_pct",
        "size_boost_max", "size_boost_full_amount",
        "consensus_bonus_per_trader", "consensus_bonus_max",
        "skill_horizon_days", "skill_min_past_trades", "skill_max_past_trades", "skill_lookback_months",
        "skill_signal_weight", "skill_confidence_weight",
        "min_trader_avg_hold_days", "min_trader_hold_roundtrips",
    )

    @classmethod
    def _setting_or_default(cls, settings: Optional[Dict[str, Any]], key: str) -> Any:
        """Read *key* from a plain settings dict, falling back to the interface default.

        Trial/test settings dicts may predate newer knobs; every new setting must degrade
        to its declared default so old configs keep producing the same recommendation."""
        if settings and key in settings and settings[key] is not None:
            return settings[key]
        return cls.get_settings_definitions()[key]["default"]

    @staticmethod
    def _trader_name(trade: Dict[str, Any]) -> str:
        """Build the trader display/lookup name from FMP firstName/lastName."""
        return f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'

    @staticmethod
    def _trade_key(trade: Dict[str, Any]) -> tuple:
        """Stable per-trade key for the exec_price map (symbol + execution date)."""
        return (str(trade.get('symbol', '')).upper(), str(trade.get('transactionDate', '')))

    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        symbol = self._gather_symbol
        senate = self._fetch_senate_trades(symbol) or []
        house = self._fetch_house_trades(symbol) or []
        # Dedup amended/duplicate filings: FMP can return the same disclosure more than
        # once (amendments, senate+house overlaps for the same person). Counting a dupe
        # twice double-weights one trade in min_trades and the focus totals.
        seen: set = set()
        all_trades = []
        for trade in senate + house:
            fp = (self._trader_name(trade), str(trade.get('symbol', '')).upper(),
                  str(trade.get('transactionDate', '')), str(trade.get('type', '')).lower(),
                  str(trade.get('amount', '')))
            if fp in seen:
                continue
            seen.add(fp)
            all_trades.append(trade)

        # Stage 1: pre-resolve the per-trade execution price (already date-aware).
        exec_price_by_trade: Dict[tuple, Optional[float]] = {}
        for trade in all_trades:
            exec_date_str = trade.get('transactionDate', '')
            if not exec_date_str:
                continue
            key = self._trade_key(trade)
            if key in exec_price_by_trade:
                continue
            try:
                exec_date = _parse_ymd_utc(exec_date_str)
            except (ValueError, TypeError):
                continue
            exec_price_by_trade[key] = self._get_price_at_date(trade.get('symbol', ''), exec_date)

        # Stage 2: pre-resolve each trader's full history, sliced to disclosure <= as_of.
        ceiling = as_of or datetime.now(timezone.utc)
        trader_history_by_name: Dict[str, List[Dict[str, Any]]] = {}
        for trade in all_trades:
            name = self._trader_name(trade)
            if name in trader_history_by_name:
                continue
            history = self._fetch_trader_history(name) or []
            if as_of is not None:
                history = [h for h in history
                           if self._disclosure_date_ok(h, ceiling)]
            trader_history_by_name[name] = history

        # Stage 3: pre-resolve each trader's SKILL score (historical hit rate of their
        # RECENT disclosed buys over the configured forward horizon). This needs per-date
        # price lookups (provider I/O), so it MUST live in _gather — _process stays pure.
        # Settings are stashed on self by run_analysis/analyze_as_of; skill knobs fall
        # back to interface defaults when absent (old trial configs). Skipped entirely
        # when both skill weights are 0 (no fetches for a disabled feature). Cached
        # cross-run/cross-trial (see _get_trader_skill_cached) — a GA job re-walks the SAME
        # dates hundreds of times, and this was profiled as the dominant per-bar cost (one
        # prolific trader's history re-scanned ~2000x in a single 3-symbol run).
        gather_settings = getattr(self, "_gather_settings", None)
        skill_signal_w = float(self._setting_or_default(gather_settings, "skill_signal_weight"))
        skill_conf_w = float(self._setting_or_default(gather_settings, "skill_confidence_weight"))
        trader_skill_by_name: Dict[str, Dict[str, Any]] = {}
        if skill_signal_w != 0.0 or skill_conf_w != 0.0:
            horizon = int(self._setting_or_default(gather_settings, "skill_horizon_days"))
            min_past = int(self._setting_or_default(gather_settings, "skill_min_past_trades"))
            max_past = int(self._setting_or_default(gather_settings, "skill_max_past_trades"))
            lookback_months = int(self._setting_or_default(gather_settings, "skill_lookback_months"))
            for name, history in trader_history_by_name.items():
                trader_skill_by_name[name] = self._get_trader_skill_cached(
                    name, history, now=ceiling, horizon_days=horizon,
                    min_past_trades=min_past, max_past_trades=max_past,
                    lookback_months=lookback_months, is_live=(as_of is None))

        # Stage 4: pre-resolve each trader's SCALPER classification (avg buy/sell hold time).
        # Pure date arithmetic (no I/O) but still costs an O(history) scan per trader, so it
        # gets the same cross-run cache as skill (see _get_trader_hold_info_cached). Skipped
        # entirely when the filter is off (default 0.0 for legacy/direct callers — the GA
        # grid itself never explores 0, see ba2test_launcher.py's _EXPERT_OPT floor).
        trader_hold_info_by_name: Dict[str, Dict[str, Any]] = {}
        if float(self._setting_or_default(gather_settings, "min_trader_avg_hold_days")) > 0:
            trader_hold_info_by_name = {
                name: self._get_trader_hold_info_cached(name, history)
                for name, history in trader_history_by_name.items()
            }

        # Live (as_of=None) reads the account/broker quote (the original live
        # source); backtest (as_of set) reads the OHLCV close-at-as_of.
        current_price = (self._get_current_price(symbol) if as_of is None
                         else providers.price_at_date(symbol, as_of))
        return {
            "all_trades": all_trades,
            "current_price": current_price,
            "exec_price_by_trade": exec_price_by_trade,
            "trader_history_by_name": trader_history_by_name,
            "trader_skill_by_name": trader_skill_by_name,
            "trader_hold_info_by_name": trader_hold_info_by_name,
            "symbol": symbol,
        }

    @staticmethod
    def _disclosure_date_ok(trade: Dict[str, Any], ceiling: datetime) -> bool:
        """True if the trade's disclosure date is on/before the as_of ceiling.

        A trade is only knowable once it has been DISCLOSED; history rows whose
        disclosureDate is after as_of would be lookahead. Rows with an unparseable
        disclosure date are kept (the live path never filtered on it)."""
        ds = trade.get('disclosureDate', '')
        if not ds:
            return True
        try:
            d = _parse_ymd_utc(ds)
        except (ValueError, TypeError):
            return True
        return d <= ceiling

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        now = as_of or datetime.now(timezone.utc)
        symbol = data_bundle["symbol"]
        current_price = data_bundle["current_price"]
        filtered = self._filter_trades(
            data_bundle["all_trades"],
            int(settings["max_disclose_date_days"]),
            int(settings["max_trade_exec_days"]),
            float(settings["max_trade_price_delta_pct"]),
            current_price, symbol,
            now=now,
            exec_price_by_trade=data_bundle["exec_price_by_trade"],
            min_trade_amount=float(self._setting_or_default(settings, "min_trade_amount")),
        )
        rec = self._calculate_recommendation(
            filtered, symbol, current_price,
            int(settings["max_trade_exec_days"]),
            trader_history_by_name=data_bundle["trader_history_by_name"],
            min_traders=int(settings["min_traders"]),
            min_trades_required=int(settings["min_trades"]),
            growth_multiplier=float(settings["growth_confidence_multiplier"]),
            confidence_to_profit_factor=float(settings["confidence_to_profit_factor"]),
            now=now,
            trader_skill_by_name=data_bundle.get("trader_skill_by_name") or {},
            trader_hold_info_by_name=data_bundle.get("trader_hold_info_by_name") or {},
            sell_signal_weight=float(self._setting_or_default(settings, "sell_signal_weight")),
            symbol_focus_cap_pct=float(self._setting_or_default(settings, "symbol_focus_cap_pct")),
            size_boost_max=float(self._setting_or_default(settings, "size_boost_max")),
            size_boost_full_amount=float(self._setting_or_default(settings, "size_boost_full_amount")),
            consensus_bonus_per_trader=float(self._setting_or_default(settings, "consensus_bonus_per_trader")),
            consensus_bonus_max=float(self._setting_or_default(settings, "consensus_bonus_max")),
            skill_signal_weight=float(self._setting_or_default(settings, "skill_signal_weight")),
            skill_confidence_weight=float(self._setting_or_default(settings, "skill_confidence_weight")),
            min_trader_avg_hold_days=float(self._setting_or_default(settings, "min_trader_avg_hold_days")),
            min_trader_hold_roundtrips=int(self._setting_or_default(settings, "min_trader_hold_roundtrips")),
            is_live=(as_of is None),
        )
        return Recommendation(
            signal=rec["signal"], confidence=round(rec["confidence"], 1),
            current_price=current_price, details=rec["details"],
            expected_profit_percent=rec["expected_profit_percent"],
            raw_outputs={"name": "Senate Trade Analysis", "type": "senate_trade_analysis",
                         "text": rec["details"], "recommendation": rec,
                         "filtered_trades": filtered})

    def analyze_as_of(self, as_of: datetime, context: BacktestContext) -> Recommendation:
        """BacktestInterface entry: resolve the gather-time symbol then run the SAME
        _gather(two-stage pre-resolve)+_process the live path drives."""
        self._gather_symbol = context.extra.get("symbol", getattr(self, "_gather_symbol", None))
        # Stash settings for _gather: the skill pre-resolve (stage 3) reads its knobs
        # (horizon/min/max/weights) at gather time, before _process sees the dict.
        self._gather_settings = context.settings
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)

    def _fetch_senate_trades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch senate trades for a symbol (raises ValueError on request failure)."""
        return self._fetch_congress_trades("senate", symbol, timeout=60, raise_on_error=True)
    
    def _fetch_house_trades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch house trades for a symbol (raises ValueError on request failure)."""
        return self._fetch_congress_trades("house", symbol, timeout=60, raise_on_error=True)
    
    def _fetch_trader_history(self, trader_name: str) -> Optional[List[Dict[str, Any]]]:
        """Cached (backtest-only) wrapper around the per-trader disclosure fetch.

        ``_gather`` refetches each congressperson's FULL history for every held symbol on every
        analysis bar — the dominant cold cost of a Senate backtest. The freeze-gated disk cache
        collapses that storm to one fetch per trader (disclosure history is time-invariant PAST
        data; the per-trade as_of/disclosure filtering runs in ``_gather`` AFTER this, so no
        lookahead). Live (freeze flag off) is a straight passthrough — always fresh.
        """
        from ba2_providers.fmp_common import fmp_history_disk_cached
        cache_key = re.sub(r"[^A-Za-z0-9]+", "_", trader_name or "").strip("_") or "unknown"
        return fmp_history_disk_cached(
            "congress_trader_history", cache_key,
            lambda: self._fetch_trader_history_uncached(trader_name),
        )

    def _fetch_trader_history_uncached(self, trader_name: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch all previous trades by a specific senator/representative using name-based search.
        
        Args:
            trader_name: Name of the government official (first or last name)
            
        Returns:
            List of all trades by this person or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch trader history: FMP API key not configured")
            return None
        
        all_trades = []
        
        # Route through the GLOBAL FMP rate-limit gate (raw requests.get storms the
        # limit under the parallel grid). fmp_http_get retries 429/5xx + transient
        # errors with a shared backoff and calls raise_for_status internally.
        from ba2_providers.fmp_common import fmp_http_get, FMPError
        timeout = 60  # Increased timeout for FMP API

        # Fetch senate trades by name
        try:
            senate_url = f"https://financialmodelingprep.com/stable/senate-trades-by-name"
            senate_params = {
                "name": trader_name,
                "apikey": self._api_key
            }

            self.logger.debug(f"Fetching senate trade history for {trader_name}")
            senate_response = fmp_http_get(
                senate_url, senate_params, symbol=trader_name,
                endpoint="senate-trades-by-name", timeout=timeout,
            )

            senate_data = senate_response.json()
            if isinstance(senate_data, list):
                all_trades.extend(senate_data)
                self.logger.debug(f"Found {len(senate_data)} senate trades by {trader_name}")

        except (requests.exceptions.RequestException, FMPError) as e:
            self.logger.error(f"Failed to fetch senate trader history for {trader_name}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error fetching senate trader history for {trader_name}: {e}")

        # Fetch house trades by name
        try:
            house_url = f"https://financialmodelingprep.com/stable/house-trades-by-name"
            house_params = {
                "name": trader_name,
                "apikey": self._api_key
            }

            self.logger.debug(f"Fetching house trade history for {trader_name}")
            house_response = fmp_http_get(
                house_url, house_params, symbol=trader_name,
                endpoint="house-trades-by-name", timeout=timeout,
            )

            house_data = house_response.json()
            if isinstance(house_data, list):
                all_trades.extend(house_data)
                self.logger.debug(f"Found {len(house_data)} house trades by {trader_name}")

        except (requests.exceptions.RequestException, FMPError) as e:
            self.logger.error(f"Failed to fetch house trader history for {trader_name}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error fetching house trader history for {trader_name}: {e}")

        self.logger.debug(f"Found total of {len(all_trades)} trades by {trader_name} (senate + house)")
        return all_trades if all_trades else None
    
    def _fetch_price_history_uncached(self, symbol: str) -> List[Dict[str, Any]]:
        """The raw FULL per-symbol daily history (list of {date, open, ...}) from FMP, or []."""
        from ba2_providers.fmp_common import fmp_http_get, FMPError
        url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol.upper()}"
        # Full history (wide `from`) so any past trade exec date resolves from ONE fetch; the
        # per-date open is identical to the old from=to=date query. Time-invariant past data.
        params = {"from": "1990-01-01", "apikey": self._api_key}
        try:
            response = fmp_http_get(
                url, params, symbol=symbol, endpoint="historical-price-full", timeout=60,
            )
            data = response.json()
            hist = data.get("historical", []) if isinstance(data, dict) else []
            return hist if isinstance(hist, list) else []
        except (requests.exceptions.RequestException, FMPError) as e:
            self.logger.error(f"Failed to fetch price history for {symbol}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error fetching price history for {symbol}: {e}")
            return []

    def _get_price_at_date(self, symbol: str, date: datetime) -> Optional[float]:
        """Opening price for ``symbol`` on ``date`` (or None if not a trading day / unavailable).

        The per-symbol FULL daily history is fetched ONCE and disk-cached (backtest-only, via
        ``fmp_history_disk_cached``) so spawned grid workers read it from disk instead of
        re-hitting FMP for every (symbol, exec-date) on every analysis bar — the dominant cold
        cost of a Senate backtest. An in-memory date->open map dedups repeat lookups within a run
        (live path too: a symbol with K trades fetches once, not K times). The per-date open is
        byte-identical to the old single-day ``from=to=date`` query; the live path (freeze flag
        off) passes through to a fresh fetch exactly as before.
        """
        if not self._api_key:
            return None
        sym = symbol.upper() if symbol else ""
        if not sym:
            return None

        price_maps = getattr(self, "_hp_price_map", None)
        if price_maps is None:
            price_maps = self._hp_price_map = {}
        smap = price_maps.get(sym)
        if smap is None:
            from ba2_providers.fmp_common import fmp_history_disk_cached
            hist = fmp_history_disk_cached(
                "historical_price_full", sym,
                lambda: self._fetch_price_history_uncached(sym),
            ) or []
            smap = {row.get("date"): row.get("open") for row in hist if row.get("date")}
            price_maps[sym] = smap

        return smap.get(date.strftime("%Y-%m-%d"))

    # --- Cross-run scoring cache (hold-days / skill) --------------------------------------
    # _calculate_trader_avg_hold_days/_calculate_trader_skill re-iterate a trader's FULL
    # disclosure history (up to ~13k trades for the most prolific member of Congress
    # discovered live) — cheap in isolation, but a GA optimization job re-runs the SAME
    # chronological walk hundreds of times (once per trial), each re-scoring every trader on
    # every bar they appear on. Persisted here (JSON, CACHE_FOLDER/fmp_history) so the score
    # is computed ONCE across the whole grid, not once per trial. Pure functions of their
    # inputs — the cache never trusts a value computed from DIFFERENT inputs (see the exact
    # key match below), so this is strictly a speedup, never a correctness risk: a cache
    # miss just falls through to the same computation as before caching existed.
    _HOLD_CACHE_FILE = "congress_scalper_scores.json"
    _SKILL_CACHE_FILE = "congress_skill_scores.json"
    _CONFIDENCE_CACHE_FILE = "congress_confidence_scores.json"
    # Append only N new entries at a time to a JSON-Lines delta file, rather than
    # re-serializing the whole accumulated dict on every flush. An earlier version of this
    # throttle rewrote the FULL dict every _CACHE_FLUSH_EVERY entries — still O(n^2) total
    # (just with a smaller constant), and profiling a 6-month/3-symbol backtest showed it
    # cost ~270s of a 1336s run because skill/confidence are keyed per trader PER DAY
    # (hundreds of thousands of entries in one run). Appending only the new entries is
    # O(new entries) per flush, O(n) total for the whole run. Periodically COMPACTED (see
    # _CACHE_COMPACT_EVERY) so the delta file — and the replay cost of loading it — stays
    # bounded across a long-lived worker process reused for many GA trials.
    #
    # 2026-07-17: _CACHE_COMPACT_EVERY=2000 was tuned for a live/backtest process whose writes
    # are spread across a whole run (one call per bar per trader) — a compaction's cost scales
    # with the CURRENT cache size (a full json.dump of the accumulated dict), and at that
    # cadence the file never gets big enough for that to matter. The date-range skill prewarm
    # (_do_senate_scores in testplatform/ba2test_launcher.py) writes ~1.4M entries in a single
    # TIGHT loop instead — profiled live: the skill cache had grown to 159MB+ from prior runs,
    # and compacting every 2000 entries meant re-serializing that whole (and growing) file
    # roughly 700 times over one prewarm run, which measured as THE dominant cost (far more
    # than the actual scoring math, even before the sliding-window optimization below).
    # Raising both thresholds ~40-50x keeps the same architecture (bounded delta growth for a
    # long-lived worker, crash only loses cheaply-recomputable cache entries) while making
    # compaction rare enough that a bulk prewarm's total compaction cost stays a small fraction
    # of total runtime — a delta file up to _CACHE_FLUSH_EVERY-1 entries lost on a hard crash,
    # or an un-compacted delta up to _CACHE_COMPACT_EVERY lines replayed at next process start,
    # are both cheap regardless (pure functions of their inputs; the delta itself is durable,
    # nothing is lost until compaction, only left un-folded into the base file).
    _CACHE_FLUSH_EVERY = 1000
    _CACHE_COMPACT_EVERY = 100_000

    def _scoring_cache_path(self, filename: str) -> str:
        from ba2_common.config import CACHE_FOLDER
        return os.path.join(CACHE_FOLDER, "fmp_history", filename)

    def _scoring_cache_delta_path(self, filename: str) -> str:
        return self._scoring_cache_path(filename) + ".delta.jsonl"

    def _load_scoring_cache(self, attr: str, filename: str) -> Dict[str, Any]:
        """Lazily load + memoize one JSON cache file on ``self`` (one disk read per
        instance/process, not per lookup). Merges the compacted base file with any
        not-yet-compacted append-only delta lines (see ``_save_scoring_cache_throttled``)."""
        cache = getattr(self, attr, None)
        if cache is not None:
            return cache
        path = self._scoring_cache_path(filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001 — missing/corrupt cache -> start fresh, never fatal
            cache = {}
        try:
            with open(self._scoring_cache_delta_path(filename), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    cache[entry["k"]] = entry["v"]
        except Exception:  # noqa: BLE001 — missing/corrupt delta -> ignore, never fatal
            pass
        setattr(self, attr, cache)
        return cache

    def _save_scoring_cache(self, attr: str, filename: str) -> None:
        """Unconditional COMPACTING flush — writes the FULL cache dict now and clears any
        pending delta file. Callers on the hot per-bar path should use
        ``_save_scoring_cache_throttled`` instead; this is for the occasional caller (e.g.
        tests, or periodic compaction) that wants a guaranteed, fully-compacted write."""
        cache = getattr(self, attr, {})
        path = self._scoring_cache_path(filename)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, path)
            delta_path = self._scoring_cache_delta_path(filename)
            if os.path.exists(delta_path):
                os.remove(delta_path)
            setattr(self, attr + "_pending", [])
            setattr(self, attr + "_since_compact", 0)
        except Exception as e:  # noqa: BLE001 — a cache-write failure must never fail scoring
            self.logger.debug(f"Failed to persist scoring cache {filename}: {e}")

    def _save_scoring_cache_throttled(self, attr: str, filename: str, key: str) -> None:
        """Append ``key`` (and its already-stored value) to the pending list; every
        ``_CACHE_FLUSH_EVERY`` pending entries, APPEND just those to the JSON-Lines delta
        file (O(new entries), not O(total cache size) — see the class-level comment on
        why a whole-dict rewrite, even throttled, is still O(n^2) total). Every
        ``_CACHE_COMPACT_EVERY`` entries, fold the delta back into the base file via a full
        compacting ``_save_scoring_cache`` so a long-lived worker process (reused across
        many GA trials) doesn't accumulate an ever-growing delta file. The in-memory dict
        (``self.<attr>``) is ALWAYS up to date regardless of flush/compact timing — only a
        crash mid-run can lose the last <N pending entries (harmless — pure functions of
        their inputs, recomputed next time)."""
        pending_attr = attr + "_pending"
        pending = getattr(self, pending_attr, None) or []
        cache = getattr(self, attr)
        pending.append({"k": key, "v": cache[key]})
        if len(pending) < self._CACHE_FLUSH_EVERY:
            setattr(self, pending_attr, pending)
            return
        since_compact_attr = attr + "_since_compact"
        since_compact = getattr(self, since_compact_attr, 0) + len(pending)
        if since_compact >= self._CACHE_COMPACT_EVERY:
            self._save_scoring_cache(attr, filename)  # also clears pending/since_compact
            return
        delta_path = self._scoring_cache_delta_path(filename)
        try:
            os.makedirs(os.path.dirname(delta_path), exist_ok=True)
            with open(delta_path, "a", encoding="utf-8") as f:
                for entry in pending:
                    f.write(json.dumps(entry) + "\n")
        except Exception as e:  # noqa: BLE001 — a cache-write failure must never fail scoring
            self.logger.debug(f"Failed to append scoring cache delta {filename}: {e}")
        setattr(self, pending_attr, [])
        setattr(self, since_compact_attr, since_compact)

    def _get_trader_hold_info_cached(self, trader_name: str,
                                     trader_history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Cached wrapper around ``_calculate_trader_avg_hold_days``. Keyed by
        (trader, history length) — hold-days has NO settings/as-of dependency beyond what's
        already reflected in which trades are in ``trader_history``, so an exact length match
        is a safe, sufficient freshness check (a NEW disclosure changes the length)."""
        cache = self._load_scoring_cache("_hold_cache", self._HOLD_CACHE_FILE)
        n = len(trader_history)
        entry = cache.get(trader_name)
        if entry and entry.get("history_len") == n:
            return {"avg_hold_days": entry.get("avg_hold_days"), "roundtrips": entry.get("roundtrips")}
        result = self._calculate_trader_avg_hold_days(trader_history)
        cache[trader_name] = {"history_len": n, **result}
        self._save_scoring_cache_throttled("_hold_cache", self._HOLD_CACHE_FILE, trader_name)
        return result

    def _get_trader_skill_cached(self, trader_name: str, trader_history: List[Dict[str, Any]],
                                 now: datetime, horizon_days: int, min_past_trades: int,
                                 max_past_trades: int, lookback_months: int = 12,
                                 is_live: bool = False) -> Dict[str, Any]:
        """Cached wrapper around ``_calculate_trader_skill``. Keyed by (trader, history
        length, an as-of bucket, and the scoring settings) — unlike hold-days, skill
        genuinely depends on ``now`` even at a FIXED history: a trade's horizon window can go
        from "not yet complete" to "complete" as time passes with NO new disclosure, so the
        as-of period must be part of the key (not just history length).

        Backtest buckets by exact DAY: as_of advances daily and each day is a distinct,
        correctness-relevant point (a GA job re-walks the SAME dates every trial, so day
        granularity still gets the full cross-trial cache win). LIVE buckets by MONTH instead
        — a live analysis can run far more often than daily, and a member of Congress's
        trading skill does not meaningfully change day to day, so re-scoring monthly is
        plenty fresh while cutting live's scoring cost ~30x versus a daily refresh.
        """
        cache = self._load_scoring_cache("_skill_cache", self._SKILL_CACHE_FILE)
        n = len(trader_history)
        as_of_bucket = now.strftime("%Y-%m") if is_live else now.date().isoformat()
        key = (f"{trader_name}|{n}|{as_of_bucket}|{horizon_days}|{min_past_trades}"
               f"|{max_past_trades}|{lookback_months}")
        entry = cache.get(key)
        if entry is not None:
            return entry
        sorted_candidates = self._sorted_buy_candidates_cached(trader_name, trader_history)
        result = self._calculate_trader_skill(
            trader_history, now=now, horizon_days=horizon_days,
            min_past_trades=min_past_trades, max_past_trades=max_past_trades,
            lookback_months=lookback_months, _sorted_candidates=sorted_candidates)
        cache[key] = result
        self._save_scoring_cache_throttled("_skill_cache", self._SKILL_CACHE_FILE, key)
        return result

    def _get_trader_confidence_cached(self, trader_name: str, trader_history: List[Dict[str, Any]],
                                      current_trade_type: str, current_symbol: str,
                                      current_price: float, max_exec_days: int, now: datetime,
                                      symbol_focus_cap_pct: float, is_live: bool) -> Dict[str, Any]:
        """Cached wrapper around ``_calculate_trader_confidence`` — a THIRD O(history) scan
        found the same way as skill/hold-days (profiled live: it iterates the trader's FULL
        history to compute yearly buy/sell totals + a symbol-specific focus subtotal, and is
        called MORE often than skill/hold-days since it runs once per qualifying trade in
        ``_calculate_recommendation``'s main loop, not once per unique trader per bar).

        Keyed by (trader, history length, the CURRENT symbol/side being evaluated, an as-of
        bucket, and the settings) — unlike hold-days, this depends on ``current_symbol``
        (the symbol-specific subtotal) and ``current_trade_type`` (buy vs sell side), and
        like skill it depends on ``now`` even at fixed history (the recent/yearly windows
        slide). Same day/month bucketing split as skill (see _get_trader_skill_cached)."""
        cache = self._load_scoring_cache("_confidence_cache", self._CONFIDENCE_CACHE_FILE)
        n = len(trader_history)
        as_of_bucket = now.strftime("%Y-%m") if is_live else now.date().isoformat()
        key = (f"{trader_name}|{n}|{current_symbol.upper()}|{current_trade_type}|{as_of_bucket}"
               f"|{max_exec_days}|{symbol_focus_cap_pct}")
        entry = cache.get(key)
        if entry is not None:
            return entry
        result = self._calculate_trader_confidence(
            trader_history, current_trade_type, current_symbol, current_price,
            max_exec_days, now=now, symbol_focus_cap_pct=symbol_focus_cap_pct)
        cache[key] = result
        self._save_scoring_cache_throttled("_confidence_cache", self._CONFIDENCE_CACHE_FILE, key)
        return result

    def _price_on_or_after(self, symbol: str, date: datetime, max_walk_days: int = 5) -> Optional[float]:
        """First available open on/after ``date`` (walks weekends/holidays, up to
        ``max_walk_days`` forward). Used by the skill scorer, whose dates (disclosure
        exec dates, exec+horizon) routinely land on non-trading days."""
        for offset in range(max_walk_days + 1):
            price = self._get_price_at_date(symbol, date + timedelta(days=offset))
            if price:
                return price
        return None

    @staticmethod
    def _sorted_buy_candidates(
        trader_history: List[Dict[str, Any]]
    ) -> Tuple[List[Tuple[datetime, str]], List[datetime]]:
        """Parse + BUY-filter a trader's disclosure history into ``(exec_date, symbol)``
        candidates, factored out of ``_calculate_trader_skill`` so this O(history) pass can run
        ONCE per trader per process (see ``_sorted_buy_candidates_cached``) instead of on every
        (as-of day, horizon, lookback) call — for a prolific trader (10k+ disclosed trades)
        scored across a multi-year day-by-day grid, the repeated O(history) rebuild was the
        dominant cost of a full date-range skill prewarm (see
        ``testplatform/ba2test_launcher.py``'s ``_do_senate_scores``).

        Returns ``(candidates_desc, ascending_dates)``:
          - ``candidates_desc``: candidates sorted by exec_date DESCENDING (most-recent first),
            ties broken by original disclosure order — a stable sort with ``reverse=True`` keeps
            tied elements in their original relative order, so this is byte-identical to what a
            fresh ``sort(key=..., reverse=True)`` over the SAME candidates would produce for ANY
            as-of/horizon/lookback window (restricting a stably-sorted sequence to a contiguous
            date sub-range never reorders the elements that remain).
          - ``ascending_dates``: the parallel list of just the dates, ascending — kept solely so
            ``_calculate_trader_skill`` can ``bisect`` a window's boundaries in O(log n) instead
            of re-scanning the whole list.
        """
        out = []
        for t in trader_history:
            ttype = str(t.get('type', '')).lower()
            if 'purchase' not in ttype and 'buy' not in ttype:
                continue
            exec_str = t.get('transactionDate', '')
            sym = str(t.get('symbol', '')).upper()
            if not exec_str or not sym:
                continue
            try:
                exec_date = _parse_ymd_utc(exec_str)
            except (ValueError, TypeError):
                continue
            out.append((exec_date, sym))
        out.sort(key=lambda c: c[0], reverse=True)
        ascending_dates = sorted(c[0] for c in out)
        return out, ascending_dates

    def _sorted_buy_candidates_cached(
        self, trader_name: str, trader_history: List[Dict[str, Any]]
    ) -> Tuple[List[Tuple[datetime, str]], List[datetime]]:
        """In-memory (never disk-persisted) memo of ``_sorted_buy_candidates`` keyed by
        (trader, history length) — mirrors the ``_hp_price_map`` pattern: cheap to recompute
        once per process but expensive to redo on every one of hundreds/thousands of skill
        lookups for the same trader within a single backtest/prewarm run."""
        memo = getattr(self, "_buy_candidates_memo", None)
        if memo is None:
            memo = self._buy_candidates_memo = {}
        key = (trader_name, len(trader_history))
        cached = memo.get(key)
        if cached is None:
            cached = memo[key] = self._sorted_buy_candidates(trader_history)
        return cached

    def _calculate_trader_skill(self, trader_history: List[Dict[str, Any]],
                                now: datetime, horizon_days: int,
                                min_past_trades: int, max_past_trades: int,
                                lookback_months: int = 12,
                                _sorted_candidates: Optional[
                                    Tuple[List[Tuple[datetime, str]], List[datetime]]] = None
                                ) -> Dict[str, Any]:
        """Score a trader's historical SKILL from the forward returns of their RECENT past buys.

        For each past disclosed BUY (any symbol) executed within ``lookback_months`` of
        ``now`` AND whose ``horizon_days`` forward window is fully in the past (exec + horizon
        <= now — no lookahead), compute the symbol's return from the exec-date open to the
        open ``horizon_days`` later (both via the disk-cached full price history; non-trading
        days walk forward a few days). The skill score maps the hit rate onto [-1, +1]:
        hit_rate 1.0 -> +1, 0.5 -> 0 (coin flip), 0.0 -> -1. Traders with fewer than
        ``min_past_trades`` scored buys get a NEUTRAL 0 (not enough recent history to judge);
        at most ``max_past_trades`` most-recent buys are scored.

        ``lookback_months`` bounds BOTH the methodology (a trader's RECENT pattern is more
        relevant than activity from years ago) and the scan cost: a prolific trader (e.g. a
        member of Congress with 10k+ disclosed trades) would otherwise have their entire
        history walked every time, dominated by an ancient period their current skill may not
        reflect at all.

        Sells are NOT scored: congressional sells are dominated by liquidity noise; the
        skill question is "do this person's BUYS predict returns?".

        ``_sorted_candidates`` (internal): an optional precomputed
        ``_sorted_buy_candidates(trader_history)`` result — pass it (e.g. via
        ``_sorted_buy_candidates_cached``) to turn the window lookup below into an O(log n)
        bisect instead of an O(n) rebuild. Omitted, it's computed fresh (same cost as before).
        """
        neutral = {"skill_score": 0.0, "scored_trades": 0, "hit_rate": None,
                   "avg_fwd_return_pct": 0.0}
        if not trader_history or horizon_days <= 0:
            return neutral

        lookback_floor = now - timedelta(days=max(1, lookback_months) * 30)
        horizon_cutoff = now - timedelta(days=horizon_days)

        candidates_desc, ascending_dates = (
            _sorted_candidates if _sorted_candidates is not None
            else self._sorted_buy_candidates(trader_history)
        )
        if not candidates_desc:
            return neutral

        # candidates_desc is sorted most-recent-first; ascending_dates is the same dates
        # ascending, purely to binary-search the window boundaries in O(log n). Elements
        # strictly newer than horizon_cutoff sit at the FRONT of candidates_desc (n - hi of
        # them); elements strictly older than lookback_floor sit at the BACK (lo of them) — the
        # in-window slice is what's left between those two counts.
        n = len(ascending_dates)
        lo = bisect.bisect_left(ascending_dates, lookback_floor)
        hi = bisect.bisect_right(ascending_dates, horizon_cutoff)
        window = candidates_desc[n - hi: n - lo]

        returns: List[float] = []
        for exec_date, sym in window:
            if len(returns) >= max_past_trades:
                break
            entry = self._price_on_or_after(sym, exec_date)
            fwd = self._price_on_or_after(sym, exec_date + timedelta(days=horizon_days))
            if not entry or not fwd:
                continue
            returns.append((fwd - entry) / entry * 100.0)

        if len(returns) < min_past_trades:
            return neutral
        hits = sum(1 for r in returns if r > 0)
        hit_rate = hits / len(returns)
        return {
            "skill_score": max(-1.0, min(1.0, (hit_rate - 0.5) * 2.0)),
            "scored_trades": len(returns),
            "hit_rate": hit_rate,
            "avg_fwd_return_pct": sum(returns) / len(returns),
        }

    @staticmethod
    def _calculate_trader_avg_hold_days(trader_history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Estimate a trader's average holding period from FIFO-paired buy/sell round-trips
        in their OWN disclosure history (no price data needed — pure date arithmetic on data
        already fetched for skill scoring / prewarm, so this filter costs ZERO extra fetches).

        Per symbol, buys are queued chronologically; each sell pops the oldest still-open buy
        and the gap in days is one round-trip. A trader who flips positions same-day/next-day
        across many symbols (a "scalper", e.g. the extreme case discovered live: one member of
        Congress with 12,958 disclosed trades) reads as a low avg_hold_days — congressional
        insider signal is about conviction positions held for a stretch, not high-frequency
        flips, so such traders are excluded via min_trader_avg_hold_days (see
        _calculate_recommendation). Returns roundtrip count so a trader with too few round
        trips to judge isn't penalized (mirrors skill's min_past_trades neutral-below-min)."""
        by_symbol: Dict[str, List[tuple]] = {}
        for t in trader_history:
            ttype = str(t.get('type', '')).lower()
            sym = str(t.get('symbol', '')).upper()
            d = t.get('transactionDate', '')
            if not sym or not d:
                continue
            is_buy = 'purchase' in ttype or 'buy' in ttype
            is_sell = 'sale' in ttype or 'sell' in ttype
            if not (is_buy or is_sell):
                continue
            try:
                dt = _parse_ymd_utc(d)
            except (ValueError, TypeError):
                continue
            by_symbol.setdefault(sym, []).append((dt, is_buy))

        hold_days: List[float] = []
        for rows in by_symbol.values():
            rows.sort(key=lambda r: r[0])
            open_buys: List[datetime] = []
            for dt, is_buy in rows:
                if is_buy:
                    open_buys.append(dt)
                elif open_buys:
                    buy_dt = open_buys.pop(0)  # FIFO: oldest open buy closes first
                    hold_days.append((dt - buy_dt).days)

        if not hold_days:
            return {"avg_hold_days": None, "roundtrips": 0}
        return {"avg_hold_days": sum(hold_days) / len(hold_days), "roundtrips": len(hold_days)}

    def _filter_trades(self, trades: List[Dict[str, Any]],
                      max_disclose_days: int,
                      max_exec_days: int,
                      max_price_delta_pct: float,
                      current_price: float,
                      symbol: str,
                      now: Optional[datetime] = None,
                      exec_price_by_trade: Optional[Dict[tuple, Optional[float]]] = None,
                      min_trade_amount: float = 0.0) -> List[Dict[str, Any]]:
        """
        Filter trades based on configured settings.

        Args:
            trades: List of trade records
            max_disclose_days: Maximum days since disclosure
            max_exec_days: Maximum days since execution
            max_price_delta_pct: Maximum price change percentage
            current_price: Current stock price
            symbol: Stock symbol to filter for
            now: The reference 'now' for age math (as_of in backtest); defaults to
                datetime.now(utc) so the live path is byte-identical.
            exec_price_by_trade: Pre-resolved execution-price map keyed by
                _trade_key(trade) (Phase 1: resolved in _gather so _process stays
                pure). When None (legacy callers) prices are fetched inline.

        Returns:
            Filtered list of trades
        """
        now = now or datetime.now(timezone.utc)
        filtered_trades = []
        max_exec_days = int(max_exec_days)
        # float, NOT int: the GA searches fractional thresholds (e.g. 7.5%); the old
        # int() cast silently truncated them, aliasing half the search grid.
        max_price_delta_pct = float(max_price_delta_pct)
        max_disclose_days = int(max_disclose_days)
        min_trade_amount = float(min_trade_amount or 0.0)
        for trade in trades:
            # Parse dates
            try:
                # FMP API uses 'disclosureDate' for disclosure date and 'transactionDate' for execution date
                disclose_date_str = trade.get('disclosureDate', '')
                exec_date_str = trade.get('transactionDate', '')
                
                trader_name = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'

                if not disclose_date_str or not exec_date_str:
                    # Build trader name for logging
                    self.logger.debug(f"Trade missing dates, skipping: {trader_name}")
                    continue
                
                # Parse dates (FMP returns YYYY-MM-DD format)
                disclose_date = _parse_ymd_utc(disclose_date_str)
                exec_date = _parse_ymd_utc(exec_date_str)
                # Build trader name for logging

                # Check if this is the correct symbol (case insensitive)
                trade_symbol = trade.get('symbol', '').upper()
                if trade_symbol != symbol.upper():
                    #logger.debug(f"Trade symbol {trade_symbol} doesn't match requested symbol {symbol}, filtering out")
                    continue
                # Check disclose date
                days_since_disclose = (now - disclose_date).days
                if days_since_disclose > max_disclose_days:
                    #logger.debug(f"Trade disclosed {days_since_disclose} days ago (max: {max_disclose_days}), filtering out")
                    continue
                
                # Check execution date
                days_since_exec = (now - exec_date).days
                if days_since_exec > max_exec_days:
                    #logger.debug(f"Trade executed {days_since_exec} days ago (max: {max_exec_days}), filtering out")
                    continue

                # Minimum trade size: sub-threshold trades carry little conviction.
                if min_trade_amount > 0 and self._parse_amount(trade.get('amount', '0')) < min_trade_amount:
                    self.logger.debug(f"Trade below min amount ${min_trade_amount:,.0f}, filtering out")
                    continue

                # Execution price: read from the pre-resolved map (Phase 1) so
                # _process is pure; fall back to an inline fetch only when no map
                # was supplied (legacy direct callers).
                if exec_price_by_trade is not None:
                    exec_price = exec_price_by_trade.get(self._trade_key(trade))
                else:
                    exec_price = self._get_price_at_date(trade.get('symbol', ''), exec_date)
                if not exec_price:
                    self.logger.debug(f"Could not get execution price for {trader_name}'s trade, skipping")
                    continue
                
                # Check price delta - only filter when price moved in the trade's favour
                # beyond the threshold (opportunity already passed)
                # BUY + price UP too much → filter (already ran up, too late)
                # SELL + price DOWN too much → filter (already dropped, too late)
                # Moves against the trade are NOT filtered (better entry opportunity)
                price_delta_pct = (current_price - exec_price) / exec_price * 100
                trade_type = trade.get('type', '').lower()
                is_buy = 'purchase' in trade_type or 'buy' in trade_type
                favourable_move = price_delta_pct if is_buy else -price_delta_pct
                if favourable_move > max_price_delta_pct:
                    self.logger.debug(f"Price moved {price_delta_pct:+.1f}% in favour of {'BUY' if is_buy else 'SELL'} "
                                      f"(max: {max_price_delta_pct}%), opportunity passed - filtering out")
                    continue
                
                # Add calculated fields to trade
                trade['exec_price'] = exec_price
                trade['current_price'] = current_price
                trade['price_delta_pct'] = (current_price - exec_price) / exec_price * 100
                trade['days_since_disclose'] = days_since_disclose
                trade['days_since_exec'] = days_since_exec
                trade['disclose_date'] = disclose_date_str
                trade['exec_date'] = exec_date_str
                
                filtered_trades.append(trade)
                
            except Exception as e:
                self.logger.error(f"Error processing trade: {e}", exc_info=True)
                continue
        
        self.logger.info(f"Filtered {len(filtered_trades)} trades from {len(trades)} total")
        return filtered_trades
    
    @staticmethod
    def _parse_amount(amount_str) -> float:
        """Parse an FMP trade amount string to a numeric dollar value.

        Thin wrapper that imports the shared helper lazily to avoid the
        core.utils -> modules.experts circular import.
        """
        from ba2_common.core.utils import parse_fmp_amount_range
        return parse_fmp_amount_range(amount_str)

    def _calculate_trader_confidence(self, trader_history: List[Dict[str, Any]],
                                     current_trade_type: str,
                                     current_symbol: str,
                                     current_price: float,
                                     max_exec_days: int = 60,
                                     now: Optional[datetime] = None,
                                     symbol_focus_cap_pct: float = 10.0) -> Dict[str, Any]:
        """
        Calculate confidence based on trader's portfolio allocation to the current symbol.
        
        Logic:
        - Calculate % of money spent on current symbol vs total portfolio (yearly)
        - Higher % = stronger conviction/focus on this specific stock
        - Cap at 10% to avoid extreme confidence values
        - Use this allocation % to determine confidence level
        
        Args:
            trader_history: List of all trades by this person (all symbols, all time)
            current_trade_type: Type of the current trade ('purchase' or 'sale')
            current_symbol: The symbol being analyzed
            current_price: Current price of the symbol (not used in this calculation)
            max_exec_days: Days to look back for recent activity
            
        Returns:
            Dictionary with confidence modifier, symbol focus %, and trading statistics
        """
        if not trader_history:
            return {
                'confidence_modifier': 0.0,
                'symbol_focus_pct': 0.0,
                'recent_buy_amount': 0.0,
                'recent_sell_amount': 0.0,
                'recent_buy_count': 0,
                'recent_sell_count': 0,
                'yearly_buy_amount': 0.0,
                'yearly_sell_amount': 0.0,
                'yearly_buy_count': 0,
                'yearly_sell_count': 0
            }
        
        # Time thresholds (now == as_of in backtest, datetime.now in live)
        now = now or datetime.now(timezone.utc)
        max_exec_days = int(max_exec_days)  # Ensure it's an integer
        recent_threshold = now - timedelta(days=max_exec_days)
        yearly_threshold = now - timedelta(days=365)
        
        # Recent period (max_exec_days)
        recent_buy_amount = 0.0
        recent_sell_amount = 0.0
        recent_buy_count = 0
        recent_sell_count = 0
        
        # Yearly period
        yearly_buy_amount = 0.0
        yearly_sell_amount = 0.0
        yearly_buy_count = 0
        yearly_sell_count = 0
        
        # Symbol-specific tracking (yearly)
        yearly_symbol_buy_amount = 0.0
        yearly_symbol_sell_amount = 0.0
        
        for trade in trader_history:
            try:
                transaction_type = trade.get('type', '').lower()
                amount_str = trade.get('amount', '0')
                exec_date_str = trade.get('transactionDate', '')
                
                # Skip if no date
                if not exec_date_str:
                    continue
                
                # Parse date
                try:
                    exec_date = _parse_ymd_utc(exec_date_str)
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"Skipping trade with unparseable date {exec_date_str!r}: {e}")
                    continue

                # Parse amount (handles "$15,001 - $50,000" ranges and single values)
                amount = self._parse_amount(amount_str)

                # Classify as buy or sell
                is_buy = 'purchase' in transaction_type or 'buy' in transaction_type
                is_sell = 'sale' in transaction_type or 'sell' in transaction_type
                
                # Get trade symbol
                trade_symbol = trade.get('symbol', '').upper()
                is_current_symbol = (trade_symbol == current_symbol.upper())
                
                # Count for recent period (max_exec_days)
                if exec_date >= recent_threshold:
                    if is_buy:
                        recent_buy_amount += amount
                        recent_buy_count += 1
                    elif is_sell:
                        recent_sell_amount += amount
                        recent_sell_count += 1
                
                # Count for yearly period
                if exec_date >= yearly_threshold:
                    if is_buy:
                        yearly_buy_amount += amount
                        yearly_buy_count += 1
                        # Track symbol-specific buys
                        if is_current_symbol:
                            yearly_symbol_buy_amount += amount
                    elif is_sell:
                        yearly_sell_amount += amount
                        yearly_sell_count += 1
                        # Track symbol-specific sells
                        if is_current_symbol:
                            yearly_symbol_sell_amount += amount
                    
            except Exception as e:
                self.logger.debug(f"Error parsing trade: {e}")
                continue
        
        # Calculate symbol focus percentage
        # What % of the trader's buys/sells (by dollar amount) are focused on this specific symbol?
        current_is_buy = 'purchase' in current_trade_type.lower() or 'buy' in current_trade_type.lower()
        
        symbol_focus_pct = 0.0
        total_volume = yearly_buy_amount + yearly_sell_amount
        
        if total_volume == 0:
            return {
                'confidence_modifier': 0.0,
                'symbol_focus_pct': 0.0,
                'recent_buy_amount': recent_buy_amount,
                'recent_sell_amount': recent_sell_amount,
                'recent_buy_count': recent_buy_count,
                'recent_sell_count': recent_sell_count,
                'yearly_buy_amount': yearly_buy_amount,
                'yearly_sell_amount': yearly_sell_amount,
                'yearly_buy_count': yearly_buy_count,
                'yearly_sell_count': yearly_sell_count,
                'yearly_symbol_buy_amount': yearly_symbol_buy_amount,
                'yearly_symbol_sell_amount': yearly_symbol_sell_amount
            }
        
        # Calculate what % of their trading activity (by dollar) is focused on this symbol
        if current_is_buy:
            # For buy trades, calculate % of total buys spent on this symbol
            if yearly_buy_amount > 0:
                symbol_focus_pct = (yearly_symbol_buy_amount / yearly_buy_amount) * 100
        else:
            # For sell trades, calculate % of total sells from this symbol
            if yearly_sell_amount > 0:
                symbol_focus_pct = (yearly_symbol_sell_amount / yearly_sell_amount) * 100
        
        # Cap symbol focus (configurable) to avoid extreme confidence values
        cap = float(symbol_focus_cap_pct or 10.0)
        symbol_focus_pct = min(cap, symbol_focus_pct)

        # Use symbol focus % as confidence modifier (capped)
        confidence_modifier = symbol_focus_pct
        
        self.logger.debug(f"Trader pattern (yearly): {yearly_buy_count} buys (${yearly_buy_amount:,.0f}), "
                    f"{yearly_sell_count} sells (${yearly_sell_amount:,.0f})")
        self.logger.debug(f"Symbol {current_symbol} (yearly): ${yearly_symbol_buy_amount:,.0f} buys, "
                    f"${yearly_symbol_sell_amount:,.0f} sells")
        self.logger.debug(f"Symbol focus: {symbol_focus_pct:.1f}% of trader's {'buys' if current_is_buy else 'sells'} (capped at 10%, confidence modifier)")
        self.logger.debug(f"Trader pattern (recent {max_exec_days}d): {recent_buy_count} buys (${recent_buy_amount:,.0f}), "
                    f"{recent_sell_count} sells (${recent_sell_amount:,.0f})")
        
        return {
            'confidence_modifier': confidence_modifier,
            'symbol_focus_pct': symbol_focus_pct,
            'recent_buy_amount': recent_buy_amount,
            'recent_sell_amount': recent_sell_amount,
            'recent_buy_count': recent_buy_count,
            'recent_sell_count': recent_sell_count,
            'yearly_buy_amount': yearly_buy_amount,
            'yearly_sell_amount': yearly_sell_amount,
            'yearly_buy_count': yearly_buy_count,
            'yearly_sell_count': yearly_sell_count,
            'yearly_symbol_buy_amount': yearly_symbol_buy_amount,
            'yearly_symbol_sell_amount': yearly_symbol_sell_amount
        }
    
    def _size_boost(self, trade: Dict[str, Any],
                    size_boost_max: float = 20.0,
                    size_boost_full_amount: float = 1_000_000.0) -> float:
        """Confidence points from the trade's dollar size: linear up to
        ``size_boost_max`` at ``size_boost_full_amount`` (defaults reproduce the
        original +10/$500k-capped-at-20 curve). This term now feeds the OVERALL
        confidence (it was previously computed per trade but never aggregated —
        trade size had no effect on the final signal)."""
        if not size_boost_max or size_boost_full_amount <= 0:
            return 0.0
        try:
            amount = self._parse_amount(trade.get('amount', '0'))
            return min(float(size_boost_max), (amount / float(size_boost_full_amount)) * float(size_boost_max))
        except Exception as e:  # noqa: BLE001 — a bad amount string is a 0 boost, not a crash
            self.logger.debug(f"Error parsing trade amount: {e}")
            return 0.0

    def _calculate_confidence(self, trade: Dict[str, Any],
                             trader_confidence_modifier: float,
                             size_boost_max: float = 20.0,
                             size_boost_full_amount: float = 1_000_000.0) -> float:
        """
        Calculate confidence for ONE trade (stored per-trade for transparency/UI).

        Formula:
        1. Start at 50% base confidence
        2. Add trader pattern modifier (from the trader's symbol portfolio focus %)
        3. Add investment size boost (via _size_boost; same term that now also feeds
           the overall confidence as the winning side's average)

        Returns:
            Confidence percentage (0-100)
        """
        confidence = 50.0
        confidence += trader_confidence_modifier
        confidence += self._size_boost(trade, size_boost_max, size_boost_full_amount)
        return min(100.0, max(0.0, confidence))
    
    def _calculate_recommendation(self, filtered_trades: List[Dict[str, Any]],
                                  symbol: str,
                                  current_price: float,
                                  max_exec_days: int,
                                  trader_history_by_name: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                                  min_traders: Optional[int] = None,
                                  min_trades_required: Optional[int] = None,
                                  growth_multiplier: Optional[float] = None,
                                  confidence_to_profit_factor: Optional[float] = None,
                                  now: Optional[datetime] = None,
                                  trader_skill_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
                                  trader_hold_info_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
                                  sell_signal_weight: float = 1.0,
                                  symbol_focus_cap_pct: float = 10.0,
                                  size_boost_max: float = 20.0,
                                  size_boost_full_amount: float = 1_000_000.0,
                                  consensus_bonus_per_trader: float = 2.0,
                                  consensus_bonus_max: float = 10.0,
                                  skill_signal_weight: float = 0.5,
                                  skill_confidence_weight: float = 10.0,
                                  min_trader_avg_hold_days: float = 0.0,
                                  min_trader_hold_roundtrips: int = 3,
                                  is_live: bool = False) -> Dict[str, Any]:
        """
        Calculate trading recommendation from filtered senate trades.

        Args:
            filtered_trades: List of relevant trades after filtering
            symbol: Stock symbol
            current_price: Current stock price
            max_exec_days: Maximum days since execution (for filtering trader history)
            trader_history_by_name: Pre-resolved per-trader history map (Phase 1:
                resolved in _gather, sliced to disclosure <= as_of). When None
                (legacy callers) history is fetched inline via _fetch_trader_history.
            min_traders / min_trades_required / growth_multiplier /
            confidence_to_profit_factor: resolved settings passed in so _process is
                pure (no self.get_setting reads). When None they are read from
                settings inline (legacy direct callers).
            now: reference 'now' for trader-history thresholds (as_of in backtest).

        Returns:
            Dictionary with recommendation details
        """
        now = now or datetime.now(timezone.utc)
        if not filtered_trades:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': 'No relevant senate/house trades found within configured parameters',
                'trades': [],
                'trade_count': 0,
                'buy_count': 0,
                'sell_count': 0,
                'total_buy_amount': 0.0,
                'total_sell_amount': 0.0
            }
        
        # Aggregate trade information
        buy_count = 0
        sell_count = 0
        total_buy_amount = 0.0
        total_sell_amount = 0.0
        trade_details = []
        scalpers_filtered = 0

        for trade in filtered_trades:
            # Build trader name from firstName and lastName
            first_name = trade.get('firstName', '')
            last_name = trade.get('lastName', '')
            trader_name = f"{first_name} {last_name}".strip() or 'Unknown'
            
            transaction_type = trade.get('type', '').lower()
            
            # Determine if it's a buy or sell
            is_buy = 'purchase' in transaction_type or 'buy' in transaction_type
            is_sell = 'sale' in transaction_type or 'sell' in transaction_type
            
            # Get trader's full history (all symbols, all time) to assess trading pattern.
            # Phase 1: read from the pre-resolved map (sliced to disclosure <= as_of in
            # _gather) so _process is pure; fall back to an inline fetch only when no
            # map was supplied (legacy direct callers).
            if trader_history_by_name is not None:
                trader_history = trader_history_by_name.get(trader_name, [])
            else:
                trader_history = self._fetch_trader_history(trader_name)
            self.logger.debug(f"Found {len(trader_history or [])} total trades by {trader_name}")

            # SCALPER FILTER: exclude a trader whose historical buy/sell round-trips (FIFO
            # per symbol) average below min_trader_avg_hold_days — congressional insider
            # signal is about conviction positions, not intraday/high-frequency flips (the
            # live incident that motivated this: one member of Congress with 12,958 disclosed
            # trades). Only filters when enough round-trips exist to judge (min_trader_
            # hold_roundtrips) — insufficient history passes through, matching skill's
            # neutral-below-min-trades pattern. Reads the PRE-RESOLVED, CACHED map from
            # _gather (_get_trader_hold_info_cached) — never recomputed here; falls back to a
            # direct (uncached) call only for legacy callers that didn't pass the map.
            if min_trader_avg_hold_days > 0:
                if trader_hold_info_by_name is not None:
                    hold_info = trader_hold_info_by_name.get(trader_name) \
                        or {"avg_hold_days": None, "roundtrips": 0}
                else:
                    hold_info = self._calculate_trader_avg_hold_days(trader_history or [])
                if (hold_info['avg_hold_days'] is not None
                        and hold_info['roundtrips'] >= min_trader_hold_roundtrips
                        and hold_info['avg_hold_days'] < min_trader_avg_hold_days):
                    self.logger.debug(
                        f"Filtering scalper-like trader {trader_name}: avg hold "
                        f"{hold_info['avg_hold_days']:.1f}d over {hold_info['roundtrips']} "
                        f"round-trips (min {min_trader_avg_hold_days}d)")
                    scalpers_filtered += 1
                    continue

            # Calculate confidence modifier based on trader's portfolio allocation to this symbol
            # This looks at what % of their money is focused on the current symbol. Cached
            # (see _get_trader_confidence_cached) — this was the MOST-called of the three
            # O(history) scans found in profiling, since it runs once per qualifying trade.
            trader_stats = self._get_trader_confidence_cached(
                trader_name, trader_history or [], transaction_type, symbol, current_price,
                max_exec_days, now, symbol_focus_cap_pct, is_live)
            trader_confidence_modifier = trader_stats['confidence_modifier']
            symbol_focus_pct = trader_stats.get('symbol_focus_pct', 0.0)

            # Trader skill (pre-resolved in _gather; neutral 0 when disabled/unknown).
            skill_info = (trader_skill_by_name or {}).get(trader_name) or {}
            trader_skill = float(skill_info.get('skill_score', 0.0))
            # Per-trade SIGNAL weight: skill scales this trade's focus contribution.
            # Clamped >= 0 so a maximally-bad trader zeroes out rather than inverting.
            trade_signal_weight = max(0.0, 1.0 + trader_skill * float(skill_signal_weight))

            # Calculate confidence for this specific trade
            trade_confidence = self._calculate_confidence(
                trade, trader_confidence_modifier, size_boost_max, size_boost_full_amount)
            
            # Store trade details
            trade_info = {
                'trader': trader_name,
                'type': transaction_type,
                'amount': trade.get('amount', 'N/A'),
                'exec_date': trade.get('exec_date', trade.get('transactionDate', 'N/A')),
                'disclose_date': trade.get('disclose_date', trade.get('disclosureDate', 'N/A')),
                'exec_price': trade.get('exec_price'),
                'current_price': trade.get('current_price'),
                'price_delta_pct': trade.get('price_delta_pct', 0),
                'trader_confidence_modifier': trader_confidence_modifier,
                'symbol_focus_pct': symbol_focus_pct,
                'trader_skill': trader_skill,
                'trader_skill_trades': int(skill_info.get('scored_trades', 0)),
                'trader_skill_hit_rate': skill_info.get('hit_rate'),
                'signal_weight': trade_signal_weight,
                'size_boost': self._size_boost(trade, size_boost_max, size_boost_full_amount),
                'confidence': trade_confidence,
                'days_since_exec': trade.get('days_since_exec', 0),
                'days_since_disclose': trade.get('days_since_disclose', 0),
                # Trader statistics
                'trader_recent_buys': f"{trader_stats['recent_buy_count']} (${trader_stats['recent_buy_amount']:,.0f})",
                'trader_recent_sells': f"{trader_stats['recent_sell_count']} (${trader_stats['recent_sell_amount']:,.0f})",
                'trader_yearly_buys': f"{trader_stats['yearly_buy_count']} (${trader_stats['yearly_buy_amount']:,.0f})",
                'trader_yearly_sells': f"{trader_stats['yearly_sell_count']} (${trader_stats['yearly_sell_amount']:,.0f})",
                'yearly_symbol_buys': f"${trader_stats.get('yearly_symbol_buy_amount', 0):,.0f}",
                'yearly_symbol_sells': f"${trader_stats.get('yearly_symbol_sell_amount', 0):,.0f}"
            }
            trade_details.append(trade_info)
            
            # Count buy/sell
            if is_buy:
                buy_count += 1
                total_buy_amount += self._parse_amount(trade.get('amount', '0'))
            elif is_sell:
                sell_count += 1
                total_sell_amount += self._parse_amount(trade.get('amount', '0'))
        
        # Check minimum traders and trades thresholds (passed in by _process so
        # this stays pure; legacy callers fall back to a settings read).
        if min_traders is None:
            min_traders = int(self.get_setting_with_interface_default('min_traders'))
        if min_trades_required is None:
            min_trades_required = int(self.get_setting_with_interface_default('min_trades'))
        unique_traders = set(t['trader'] for t in trade_details)
        num_unique_traders = len(unique_traders)
        num_total_trades = len(trade_details)

        if num_unique_traders < min_traders or num_total_trades < min_trades_required:
            self.logger.info(f"Below minimums: {num_unique_traders} traders (min {min_traders}), "
                           f"{num_total_trades} trades (min {min_trades_required}) "
                           f"({scalpers_filtered} scalper-filtered) - returning HOLD")
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': f'Below minimum thresholds: {num_unique_traders} unique trader(s) (min {min_traders}), '
                          f'{num_total_trades} trade(s) (min {min_trades_required}). Both must be met. '
                          f'({scalpers_filtered} trade(s) excluded as scalper-like.)',
                'trades': trade_details,
                'trade_count': len(filtered_trades),
                'buy_count': buy_count,
                'sell_count': sell_count,
                'total_buy_amount': total_buy_amount,
                'total_sell_amount': total_sell_amount,
                'avg_price_delta': 0.0,
                'price_confidence_adj': 0.0,
                'scalpers_filtered': scalpers_filtered,
            }

        # Determine overall signal based on net SKILL-WEIGHTED portfolio allocation.
        # Each trade contributes its trader's symbol focus x that trader's skill weight
        # (1 + skill x skill_signal_weight, so proven traders count more, proven-bad ones
        # less); the SELL side is additionally scaled by sell_signal_weight because
        # congressional sells are dominated by liquidity/diversification noise while buys
        # carry the information.
        def _is_buy_t(t):
            return 'purchase' in t['type'].lower() or 'buy' in t['type'].lower()

        def _is_sell_t(t):
            return 'sale' in t['type'].lower() or 'sell' in t['type'].lower()

        buy_symbol_focus_total = sum(t['symbol_focus_pct'] * t['signal_weight']
                                     for t in trade_details if _is_buy_t(t))
        sell_symbol_focus_total = (sum(t['symbol_focus_pct'] * t['signal_weight']
                                       for t in trade_details if _is_sell_t(t))
                                   * float(sell_signal_weight))

        net_trades = buy_count - sell_count
        net_amount = total_buy_amount - total_sell_amount
        net_symbol_focus = buy_symbol_focus_total - sell_symbol_focus_total

        # Use net portfolio allocation (symbol focus) to determine signal
        # This considers both the number of traders AND how much they're allocating
        if net_symbol_focus > 0:
            # More portfolio allocation to buys = BUY signal
            signal = OrderRecommendation.BUY
            dominant_count = buy_count
            dominant_amount = total_buy_amount
        elif net_symbol_focus < 0:
            # More portfolio allocation to sells = SELL signal
            signal = OrderRecommendation.SELL
            dominant_count = sell_count
            dominant_amount = total_sell_amount
        else:
            # Equal portfolio allocation = HOLD (they cancel out)
            signal = OrderRecommendation.HOLD
            dominant_count = buy_count + sell_count
            dominant_amount = total_buy_amount + total_sell_amount
        
        # Calculate overall confidence and expected profit based on symbol focus percentage
        # Filter trades by signal direction for symbol focus calculation
        if signal == OrderRecommendation.BUY:
            relevant_trades = [t for t in trade_details if 'purchase' in t['type'].lower() or 'buy' in t['type'].lower()]
        elif signal == OrderRecommendation.SELL:
            relevant_trades = [t for t in trade_details if 'sale' in t['type'].lower() or 'sell' in t['type'].lower()]
        else:
            relevant_trades = trade_details
        
        # Get growth confidence multiplier (passed in by _process; legacy fallback)
        if growth_multiplier is None:
            growth_multiplier = self.get_setting_with_interface_default('growth_confidence_multiplier')

        # Calculate average symbol focus percentage across relevant trades
        if relevant_trades:
            avg_symbol_focus_pct = sum(t['symbol_focus_pct'] for t in relevant_trades) / len(relevant_trades)
        else:
            avg_symbol_focus_pct = 0.0
        
        # Apply symbol focus-based formula
        # Confidence: 50 + (symbol_focus_pct * multiplier) + price_movement_adjustment
        # Logic: symbol_focus_pct is capped at 10%, so with default multiplier 5.0:
        #   10% portfolio allocation * 5.0 = 50% bonus = 100% total confidence
        #   0% portfolio allocation * 5.0 = 0% bonus = 50% total confidence
        #
        # Price movement adjustment (delta / 2):
        #   BUY + price down 10% → +5 confidence (better entry)
        #   BUY + price up 10% → -5 confidence (opportunity partly gone)
        #   SELL + price up 10% → +5 confidence (better entry for short)
        #   SELL + price down 10% → -5 confidence (opportunity partly gone)
        avg_price_delta = 0.0
        if relevant_trades:
            deltas = [t.get('price_delta_pct', 0.0) for t in relevant_trades]
            avg_price_delta = sum(deltas) / len(deltas)
        is_buy_signal = signal == OrderRecommendation.BUY
        # For BUY: price down (negative delta) is good → negate to get positive adjustment
        # For SELL: price up (positive delta) is good → keep as positive adjustment
        price_confidence_adj = (-avg_price_delta if is_buy_signal else avg_price_delta) / 2

        # SIZE term: winning side's average dollar-size boost (0..size_boost_max). This
        # was previously computed per trade but never aggregated — trade size had NO
        # effect on the final confidence.
        avg_size_boost = (sum(t.get('size_boost', 0.0) for t in relevant_trades) / len(relevant_trades)
                          if relevant_trades else 0.0)

        # CONSENSUS term: breadth of unique traders on the winning side beyond the first.
        winning_traders = len({t['trader'] for t in relevant_trades})
        consensus_bonus = min(float(consensus_bonus_max),
                              float(consensus_bonus_per_trader) * max(0, winning_traders - 1))

        # SKILL term: winning side's average historical skill (-1..+1) scaled to points.
        avg_skill = (sum(t.get('trader_skill', 0.0) for t in relevant_trades) / len(relevant_trades)
                     if relevant_trades else 0.0)
        skill_confidence_adj = avg_skill * float(skill_confidence_weight)

        overall_confidence = min(100.0, max(0.0,
            50.0 + avg_symbol_focus_pct * growth_multiplier + price_confidence_adj
            + avg_size_boost + consensus_bonus + skill_confidence_adj))
        
        # Expected Profit: Confidence multiplied by profit factor (always positive regardless of BUY/SELL)
        # Example: 80% confidence * 0.15 factor = 12% expected profit (passed in by
        # _process; legacy fallback to a settings read)
        if confidence_to_profit_factor is None:
            confidence_to_profit_factor = self.get_setting_with_interface_default('confidence_to_profit_factor')
        expected_profit = overall_confidence * confidence_to_profit_factor
        
        # Build detailed report
        details = f"""FMP Senate/House Trading Analysis

Current Price: ${current_price:.2f}

Trade Activity Summary:
- Total Relevant Trades: {len(filtered_trades)}
- Buy Trades: {buy_count} (${total_buy_amount:,.0f})
- Sell Trades: {sell_count} (${total_sell_amount:,.0f})

Overall Signal: {signal.value}
Confidence: {overall_confidence:.1f}%
Expected Profit: {expected_profit:.1f}%

Individual Trade Analysis:
"""
        
        for i, trade_info in enumerate(trade_details, 1):
            details += f"""
Trade #{i}:
- Trader: {trade_info['trader']}
- Type: {trade_info['type']}
- Amount: {trade_info['amount']}
- Execution Date: {trade_info['exec_date']} ({trade_info['days_since_exec']} days ago)
- Disclosure Date: {trade_info['disclose_date']} ({trade_info['days_since_disclose']} days ago)
- Execution Price: ${trade_info['exec_price']:.2f}
- Current Price: ${trade_info['current_price']:.2f}
- Price Change: {trade_info['price_delta_pct']:+.1f}%
- Symbol Focus: {trade_info['symbol_focus_pct']:.1f}% (of trader's portfolio, capped at {symbol_focus_cap_pct:.0f}%)
- Trader Skill: {trade_info['trader_skill']:+.2f} ({trade_info['trader_skill_trades']} past buys scored) → signal weight {trade_info['signal_weight']:.2f}
- Trade Confidence: {trade_info['confidence']:.1f}%
- Trader Recent Activity (last {max_exec_days}d): {trade_info['trader_recent_buys']} buys, {trade_info['trader_recent_sells']} sells
- Trader Yearly Activity (all symbols): {trade_info['trader_yearly_buys']} buys, {trade_info['trader_yearly_sells']} sells
- Trader Yearly {symbol} Activity (used for portfolio focus %): {trade_info['yearly_symbol_buys']} buys, {trade_info['yearly_symbol_sells']} sells
"""
        
        details += f"""

📊 Note on Trade Data:
- The {len(filtered_trades)} trades shown above are FILTERED trades (recent, price hasn't moved too much)
- "Yearly Symbol Activity" shows ALL {symbol} trades by that trader in the past year (not just filtered)
- Portfolio focus % is calculated from yearly activity to understand their true allocation
- Only filtered trades are used to generate the BUY/SELL signal

Signal Determination (skill-weighted, sell-discounted):
- Buy Trades: {buy_count} trades (${total_buy_amount:,.0f}) with {buy_symbol_focus_total:.1f} skill-weighted focus score
- Sell Trades: {sell_count} trades (${total_sell_amount:,.0f}) with {sell_symbol_focus_total:.1f} skill-weighted focus score (sell weight {sell_signal_weight})
- Net Focus Score: {net_symbol_focus:+.1f} (buy - sell)
- Each trade's focus is multiplied by its trader's skill weight (1 + skill × {skill_signal_weight});
  the sell side is additionally scaled by {sell_signal_weight} (congressional sells are mostly liquidity noise)
- More weighted focus on buys = BUY, more on sells = SELL, equal = HOLD

Confidence Calculation Method:
1. Calculate Symbol Focus % for each trader:
   - Look at all their trades in the past year
   - Calculate: ($ spent on {symbol} / $ spent on all symbols) × 100
   - Cap at {symbol_focus_cap_pct:.0f}% to avoid extreme values
2. Average Symbol Focus across relevant {signal.value} traders: {avg_symbol_focus_pct:.1f}%
3. Price Movement Adjustment: avg delta {avg_price_delta:+.1f}% → {price_confidence_adj:+.1f} confidence
4. Size Boost: winning side's avg trade-size boost = +{avg_size_boost:.1f} (max {size_boost_max:.0f} at ${size_boost_full_amount:,.0f})
5. Consensus Bonus: {winning_traders} unique {signal.value} trader(s) → +{consensus_bonus:.1f} (per-extra-trader {consensus_bonus_per_trader}, cap {consensus_bonus_max})
6. Skill Adjustment: winning side's avg skill {avg_skill:+.2f} × {skill_confidence_weight} = {skill_confidence_adj:+.1f}
   (skill = trader's historical hit rate of past disclosed buys over the forward horizon, -1..+1; neutral 0 without enough scored history)
7. Confidence Formula: 50 + (Avg Focus % × {growth_multiplier}) + price adj ({price_confidence_adj:+.1f}) + size (+{avg_size_boost:.1f}) + consensus (+{consensus_bonus:.1f}) + skill ({skill_confidence_adj:+.1f}) = {overall_confidence:.1f}%
8. Expected Profit: confidence × factor = {abs(expected_profit):.1f}%

Symbol Focus Analysis:
This measures how much conviction/focus the trader has on {symbol}.
Higher allocation % = trader is putting significant money into this stock → higher confidence
Lower allocation % = trader is just dabbling or diversifying → lower confidence
Focus is capped at 10% to represent maximum conviction.

Example: If trader bought $100k of {symbol} and $1M total stocks → 10% focus → maximum confidence

Settings:
- Growth Confidence Multiplier: {growth_multiplier} (configurable, controls sensitivity)
- Scalper Filter: {scalpers_filtered} trade(s) excluded (trader's avg buy/sell hold time below the configured minimum)

Note: Only {signal.value} trades are used for confidence calculation.
All {len(trade_details)} trades shown above for transparency.
"""
        
        return {
            'signal': signal,
            'confidence': overall_confidence,
            'expected_profit_percent': expected_profit,
            'details': details,
            'trades': trade_details,
            'trade_count': len(filtered_trades),
            'buy_count': buy_count,
            'sell_count': sell_count,
            'total_buy_amount': total_buy_amount,
            'total_sell_amount': total_sell_amount,
            'avg_price_delta': avg_price_delta,
            'price_confidence_adj': price_confidence_adj,
            # New confidence terms (also surfaced for UI/inspection)
            'avg_size_boost': avg_size_boost,
            'consensus_bonus': consensus_bonus,
            'avg_trader_skill': avg_skill,
            'skill_confidence_adj': skill_confidence_adj,
            'winning_traders': winning_traders,
            'scalpers_filtered': scalpers_filtered,
            # Add trade metrics
            'trade_metrics': self._calculate_trade_metrics(filtered_trades)
        }
    
    def _calculate_trade_metrics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Calculate financial metrics for trades including total money spent
        and percentage of yearly trading.
        
        Args:
            trades: List of filtered trade dictionaries
            
        Returns:
            Dictionary with financial metrics
        """
        from ba2_common.core.utils import calculate_fmp_trade_metrics
        return calculate_fmp_trade_metrics(trades)
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], 
                                     symbol: str, market_analysis_id: int,
                                     current_price: Optional[float]) -> int:
        """Create ExpertRecommendation record in database."""
        try:
            # Get trade metrics
            trade_metrics = recommendation_data.get('trade_metrics', {})
            
            # Store senate trade specific data with trade metrics
            senate_trade_data = {
                'buy_count': recommendation_data.get('buy_count', 0),
                'sell_count': recommendation_data.get('sell_count', 0),
                'total_buy_amount': recommendation_data.get('total_buy_amount', 0.0),
                'total_sell_amount': recommendation_data.get('total_sell_amount', 0.0),
                'trade_count': recommendation_data.get('trade_count', 0),
                # Financial metrics
                'money_spent': trade_metrics.get('total_money_spent', 0.0),
                'percent_of_yearly': trade_metrics.get('percent_of_yearly', 0.0),
                'avg_trade_amount': trade_metrics.get('avg_trade_amount', 0.0),
                'min_trade_amount': trade_metrics.get('min_trade_amount', 0.0),
                'max_trade_amount': trade_metrics.get('max_trade_amount', 0.0)
            }
            
            expert_recommendation = ExpertRecommendation(
                instance_id=self.id,
                symbol=symbol,
                recommended_action=recommendation_data['signal'],
                expected_profit_percent=recommendation_data['expected_profit_percent'],
                price_at_date=current_price,
                details=recommendation_data['details'][:100000] if recommendation_data['details'] else None,
                confidence=round(recommendation_data['confidence'], 1),  # Store as 1-100 scale
                risk_level=RiskLevel.MEDIUM,  # Senate trades are medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Medium term based on disclosure lag
                market_analysis_id=market_analysis_id,
                data={'SenateWeight': senate_trade_data},  # Store with "SenateWeight" key
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            trade_metrics = recommendation_data.get('trade_metrics', {})
            self.logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence, "
                       f"based on {recommendation_data['trade_count']} senate/house trades, "
                       f"Total spent: ${trade_metrics.get('total_money_spent', 0.0):,.0f}, "
                       f"Percent of yearly: {trade_metrics.get('percent_of_yearly', 0.0):.1f}%")
            return recommendation_id
            
        except Exception as e:
            self.logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str,
                               recommendation_data: Dict[str, Any],
                               all_trades: List[Dict[str, Any]],
                               filtered_trades: List[Dict[str, Any]]) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store main analysis details
            details_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Senate Trade Analysis",
                type="senate_trade_analysis",
                text=recommendation_data['details']
            )
            session.add(details_output)
            
            # Store individual trade details
            if recommendation_data['trades']:
                trades_text = json.dumps(recommendation_data['trades'], indent=2)
                trades_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="Individual Trade Details",
                    type="trade_details",
                    text=trades_text
                )
                session.add(trades_output)
            
            # Store full trade data (all and filtered)
            all_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="All Senate Trades (Raw Data)",
                type="all_trades_raw",
                text=json.dumps(all_trades, indent=2)
            )
            session.add(all_trades_output)
            
            filtered_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Filtered Trades (After Settings)",
                type="filtered_trades",
                text=json.dumps(filtered_trades, indent=2)
            )
            session.add(filtered_trades_output)
            
            # Store summary statistics
            summary_text = f"""Senate Trade Summary:
- Total Trades Found: {len(all_trades)}
- Trades After Filtering: {len(filtered_trades)}
- Buy Trades: {recommendation_data['buy_count']}
- Sell Trades: {recommendation_data['sell_count']}
- Total Buy Amount: ${recommendation_data['total_buy_amount']:,.2f}
- Total Sell Amount: ${recommendation_data['total_sell_amount']:,.2f}
- Overall Signal: {recommendation_data['signal'].value}
- Confidence: {recommendation_data['confidence']:.1f}%"""
            
            summary_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Trade Summary",
                type="trade_summary",
                text=summary_text
            )
            session.add(summary_output)
            
            session.commit()
            
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run FMPSenateTrade analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        self.logger.info(f"Starting FMPSenateTrade analysis for {symbol} (Analysis ID: {market_analysis.id})")

        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            # Resolve settings into a plain dict so _process stays pure (no self.* reads)
            settings = self._resolve_settings(self._SETTING_KEYS)
            max_disclose_days = int(settings['max_disclose_date_days'])
            max_exec_days = int(settings['max_trade_exec_days'])
            max_price_delta_pct = float(settings['max_trade_price_delta_pct'])

            # Thin orchestrator: _gather(as_of=None) + _process — the EXACT same pair
            # the backtest engine drives via analyze_as_of. _gather raises if the FMP
            # fetch fails (raise_on_error=True fetchers), preserving the live contract.
            self._gather_symbol = symbol
            self._gather_settings = settings  # skill knobs read at gather time (stage 3)
            providers = self._live_providers()
            bundle = self._gather(providers, as_of=None)
            current_price = bundle["current_price"]
            if not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")

            all_trades = bundle["all_trades"]
            rec = self._process(bundle, settings, as_of=None)

            # The full recommendation_data dict (trades, counts, metrics) and the raw
            # filtered trade list are carried in raw_outputs for DB persistence.
            recommendation_data = rec.raw_outputs["recommendation"]
            filtered_trades = rec.raw_outputs["filtered_trades"]

            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id, current_price
            )

            # Store analysis outputs
            self._store_analysis_outputs(
                market_analysis.id, symbol, recommendation_data,
                all_trades, filtered_trades
            )

            # Store analysis state
            trade_metrics = recommendation_data.get('trade_metrics', {})
            market_analysis.state = {
                'senate_trade': {
                    'recommendation': {
                        'signal': recommendation_data['signal'].value,
                        'confidence': recommendation_data['confidence'],
                        'expected_profit_percent': recommendation_data['expected_profit_percent'],
                        'details': recommendation_data['details'],
                        # Add financial metrics
                        'money_spent': trade_metrics.get('total_money_spent', 0.0),
                        'percent_of_yearly': trade_metrics.get('percent_of_yearly', 0.0),
                        'avg_price_delta': recommendation_data.get('avg_price_delta', 0.0),
                        'price_confidence_adj': recommendation_data.get('price_confidence_adj', 0.0),
                        'avg_size_boost': recommendation_data.get('avg_size_boost', 0.0),
                        'consensus_bonus': recommendation_data.get('consensus_bonus', 0.0),
                        'avg_trader_skill': recommendation_data.get('avg_trader_skill', 0.0),
                        'skill_confidence_adj': recommendation_data.get('skill_confidence_adj', 0.0)
                    },
                    'trade_statistics': {
                        'total_trades': len(all_trades),
                        'filtered_trades': len(filtered_trades),
                        'buy_count': recommendation_data['buy_count'],
                        'sell_count': recommendation_data['sell_count'],
                        'total_buy_amount': recommendation_data['total_buy_amount'],
                        'total_sell_amount': recommendation_data['total_sell_amount']
                    },
                    'trades': recommendation_data['trades'],
                    'settings': {
                        'max_disclose_date_days': max_disclose_days,
                        'max_trade_exec_days': max_exec_days,
                        'max_trade_price_delta_pct': max_price_delta_pct
                    },
                    'expert_recommendation_id': recommendation_id,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
                    'current_price': current_price
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            self.logger.info(f"Completed FMPSenateTrade analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%, "
                       f"{recommendation_data['trade_count']} trades analyzed)")
            
        except Exception as e:
            self.logger.error(f"FMPSenateTrade analysis failed for {symbol}: {e}", exc_info=True)
            
            # Update status to failed
            market_analysis.state = {
                'error': str(e),
                'error_timestamp': datetime.now(timezone.utc).isoformat(),
                'analysis_failed': True
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            
            # Create error output
            try:
                session = get_db()
                error_output = AnalysisOutput(
                    market_analysis_id=market_analysis.id,
                    name="Analysis Error",
                    type="error",
                    text=f"FMPSenateTrade analysis failed for {symbol}: {str(e)}"
                )
                session.add(error_output)
                session.commit()
                session.close()
            except Exception as db_error:
                self.logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed analysis with detailed UI."""
        from nicegui import ui
        
        if not market_analysis.state or 'senate_trade' not in market_analysis.state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        
        state = market_analysis.state['senate_trade']
        rec = state.get('recommendation', {})
        stats = state.get('trade_statistics', {})
        trades = state.get('trades', [])
        settings = state.get('settings', {})
        current_price = state.get('current_price')
        
        # Main card
        with ui.card().classes('w-full').style('background-color: #1e2a3a'):
            # Header
            with ui.card_section().style('background: linear-gradient(135deg, #00d4aa 0%, #00b894 100%)'):
                ui.label('Senate/House Trading Activity Analysis').classes('text-h5 text-weight-bold').style('color: white')
                ui.label(f'{market_analysis.symbol} - Government Official Trades').style('color: rgba(255,255,255,0.8)')
            
            # Recommendation summary
            signal = rec.get('signal', 'HOLD')
            confidence = rec.get('confidence', 0.0)
            expected_profit = rec.get('expected_profit_percent', 0.0)
            
            # Color based on signal
            if signal == 'BUY':
                signal_color = 'positive'
                signal_icon = 'trending_up'
            elif signal == 'SELL':
                signal_color = 'negative'
                signal_icon = 'trending_down'
            else:
                signal_color = 'grey'
                signal_icon = 'trending_flat'
            
            with ui.card_section():
                with ui.row().classes('w-full items-center justify-between'):
                    with ui.column():
                        ui.label('Recommendation').classes('text-caption').style('color: #a0aec0')
                        with ui.row().classes('items-center gap-2'):
                            ui.icon(signal_icon, color=signal_color, size='2rem')
                            ui.label(signal).classes(f'text-h4 text-{signal_color}')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Confidence').classes('text-caption').style('color: #a0aec0')
                        ui.label(f'{confidence:.1f}%').classes('text-h4').style('color: #e2e8f0')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Expected Profit').classes('text-caption').style('color: #a0aec0')
                        profit_color = 'positive' if expected_profit > 0 else 'negative' if expected_profit < 0 else 'grey'
                        ui.label(f'{expected_profit:+.1f}%').classes(f'text-h4 text-{profit_color}')
                
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').style('color: #a0aec0')

                # Price movement impact on confidence
                avg_price_delta = rec.get('avg_price_delta', 0.0)
                price_confidence_adj = rec.get('price_confidence_adj', 0.0)
                if avg_price_delta != 0.0:
                    delta_color = '#00d4aa' if price_confidence_adj > 0 else '#ff6b6b'
                    ui.label(
                        f'Avg Price Move: {avg_price_delta:+.1f}% → Confidence {price_confidence_adj:+.1f}'
                    ).classes('text-sm').style(f'color: {delta_color}')

                # New confidence terms (size / consensus / trader skill)
                _size = rec.get('avg_size_boost', 0.0)
                _cons = rec.get('consensus_bonus', 0.0)
                _skill = rec.get('avg_trader_skill', 0.0)
                _skill_adj = rec.get('skill_confidence_adj', 0.0)
                if _size or _cons or _skill_adj:
                    ui.label(
                        f'Adjustments — Size: +{_size:.1f} | Consensus: +{_cons:.1f} | '
                        f'Trader Skill: {_skill:+.2f} → {_skill_adj:+.1f}'
                    ).classes('text-sm').style('color: #a0aec0')

            # Trade Statistics
            total_trades = stats.get('total_trades', 0)
            filtered_trades = stats.get('filtered_trades', 0)
            buy_count = stats.get('buy_count', 0)
            sell_count = stats.get('sell_count', 0)
            total_buy_amount = stats.get('total_buy_amount', 0)
            total_sell_amount = stats.get('total_sell_amount', 0)
            
            with ui.card_section().style('background-color: #141c28'):
                ui.label('Trade Activity Summary').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                
                with ui.grid(columns=2).classes('w-full gap-4'):
                    # Total Trades
                    with ui.card().style('background-color: rgba(66, 153, 225, 0.15)'):
                        ui.label('Total Trades Found').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(total_trades)).classes('text-h5').style('color: #63b3ed')
                        ui.label(f'{filtered_trades} after filtering').classes('text-xs').style('color: #4299e1')
                    
                    # Buy Activity
                    with ui.card().style('background-color: rgba(0, 212, 170, 0.15)'):
                        ui.label('Buy Trades').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(buy_count)).classes('text-h5').style('color: #00d4aa')
                        ui.label(f'${total_buy_amount:,.0f} total').classes('text-xs').style('color: #00b894')
                    
                    # Sell Activity
                    with ui.card().style('background-color: rgba(255, 107, 107, 0.15)'):
                        ui.label('Sell Trades').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(sell_count)).classes('text-h5').style('color: #ff6b6b')
                        ui.label(f'${total_sell_amount:,.0f} total').classes('text-xs').style('color: #fc8181')
                    
                    # Signal Strength
                    with ui.card().style('background-color: rgba(159, 122, 234, 0.15)'):
                        ui.label('Signal Strength').classes('text-caption').style('color: #a0aec0')
                        consensus_pct = (max(buy_count, sell_count) / (buy_count + sell_count) * 100) if (buy_count + sell_count) > 0 else 0
                        ui.label(f'{consensus_pct:.0f}%').classes('text-h5').style('color: #9f7aea')
                        ui.label('consensus').classes('text-xs').style('color: #b794f4')
            
            # Individual Trades
            if trades:
                with ui.card_section():
                    ui.label(f'Individual Trades ({len(trades)})').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                    
                    for i, trade in enumerate(trades[:5], 1):  # Show top 5
                        trade_type = trade.get('type', 'Unknown')
                        is_buy = 'purchase' in trade_type.lower() or 'buy' in trade_type.lower()
                        
                        bg_color = 'rgba(0, 212, 170, 0.1)' if is_buy else 'rgba(255, 107, 107, 0.1)'
                        with ui.card().classes('w-full').style(f'background-color: {bg_color}'):
                            with ui.row().classes('w-full items-start justify-between'):
                                with ui.column().classes('flex-grow'):
                                    ui.label(f'Trade #{i}: {trade.get("trader", "Unknown")}').classes('text-weight-medium').style('color: #e2e8f0')
                                    ui.label(f'{trade_type} - {trade.get("amount", "N/A")}').classes('text-sm').style('color: #a0aec0')
                                    
                                    with ui.row().classes('gap-4 mt-2 text-xs').style('color: #a0aec0'):
                                        ui.label(f'Exec: {trade.get("exec_date", "N/A")} ({trade.get("days_since_exec", 0)}d ago)')
                                        ui.label(f'Disclosed: {trade.get("disclose_date", "N/A")} ({trade.get("days_since_disclose", 0)}d ago)')
                                    
                                    # Trader total activity statistics (all symbols)
                                    ui.separator().classes('mt-2 mb-1')
                                    with ui.column().classes('text-xs').style('color: #718096'):
                                        ui.label('Total trades (all symbols):').classes('text-xs text-weight-medium')
                                        ui.label(f'Recent: {trade.get("trader_recent_buys", "N/A")} buys, {trade.get("trader_recent_sells", "N/A")} sells')
                                        ui.label(f'Yearly: {trade.get("trader_yearly_buys", "N/A")} buys, {trade.get("trader_yearly_sells", "N/A")} sells')
                                
                                with ui.column().classes('text-right'):
                                    ui.label(f'Confidence: {trade.get("confidence", 0):.1f}%').classes('text-sm text-weight-medium').style('color: #e2e8f0')
                                    
                                    exec_price = trade.get('exec_price')
                                    price_delta = trade.get('price_delta_pct', 0)
                                    if exec_price:
                                        delta_color = 'positive' if price_delta > 0 else 'negative'
                                        ui.label(f'${exec_price:.2f} → ${trade.get("current_price", 0):.2f}').classes('text-xs').style('color: #a0aec0')
                                        ui.label(f'{price_delta:+.1f}%').classes(f'text-sm text-{delta_color}')
                                    
                                    modifier = trade.get('trader_confidence_modifier', 0)
                                    if modifier != 0:
                                        modifier_color = 'positive' if modifier > 0 else 'negative'
                                        ui.label(f'Symbol Alloc: {modifier:+.1f}%').classes(f'text-xs text-{modifier_color}')
                    
                    if len(trades) > 5:
                        ui.label(f'+ {len(trades) - 5} more trades').classes('text-sm mt-2').style('color: #718096')
            
            # Settings
            max_disclose = settings.get('max_disclose_date_days', 30)
            max_exec = settings.get('max_trade_exec_days', 60)
            max_delta = settings.get('max_trade_price_delta_pct', 10.0)
            
            with ui.card_section():
                ui.label('Filter Settings').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                
                with ui.row().classes('gap-4'):
                    ui.label(f'Max Disclose Age: {max_disclose} days').classes('text-sm').style('color: #a0aec0')
                    ui.label(f'Max Exec Age: {max_exec} days').classes('text-sm').style('color: #a0aec0')
                    ui.label(f'Max Price Delta: {max_delta}%').classes('text-sm').style('color: #a0aec0')
            
            # Methodology
            with ui.expansion('Calculation Methodology', icon='info').classes('w-full').style('color: #e2e8f0'):
                with ui.card_section().style('background-color: #141c28'):
                    ui.markdown('''
**Confidence Calculation:**

1. **Base Confidence**: Start at 50%
2. **+ Portfolio Allocation**: Avg symbol focus % × multiplier (max 10% focus)
3. **+ Price Movement**: avg price delta / 2
   - BUY + price dropped → positive (better entry)
   - BUY + price rose → negative (opportunity partly gone)
   - SELL: opposite logic
4. **Cap**: 0% floor, 100% ceiling

**Trade Filtering:**
- Only trades disclosed within last **{max_disclose}** days
- Only trades executed within last **{max_exec}** days
- Only trades where price hasn't moved more than **+{max_delta}%** in the trade's favour (opportunity passed)

**Signal Logic:**
- **BUY**: More government officials buying than selling
- **SELL**: More selling than buying
- **HOLD**: Equal activity or no relevant trades

**Expected Profit**: Based on average price movement since trade execution dates

**Note**: Government officials must disclose trades within 30-45 days, creating a natural delay. This expert looks for patterns in disclosed trades that may still have upside/downside potential.
                    '''.format(max_disclose=max_disclose, max_exec=max_exec, max_delta=max_delta)).classes('text-sm')
