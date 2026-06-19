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

from typing import Any, Dict, Optional, List
from datetime import datetime, timezone, timedelta
import json
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
    )

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
        all_trades = senate + house

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
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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

        # Live (as_of=None) reads the account/broker quote (the original live
        # source); backtest (as_of set) reads the OHLCV close-at-as_of.
        current_price = (self._get_current_price(symbol) if as_of is None
                         else providers.price_at_date(symbol, as_of))
        return {
            "all_trades": all_trades,
            "current_price": current_price,
            "exec_price_by_trade": exec_price_by_trade,
            "trader_history_by_name": trader_history_by_name,
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
            d = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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
    
    def _filter_trades(self, trades: List[Dict[str, Any]],
                      max_disclose_days: int,
                      max_exec_days: int,
                      max_price_delta_pct: float,
                      current_price: float,
                      symbol: str,
                      now: Optional[datetime] = None,
                      exec_price_by_trade: Optional[Dict[tuple, Optional[float]]] = None) -> List[Dict[str, Any]]:
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
        max_price_delta_pct = int (max_price_delta_pct)
        max_disclose_days = int(max_disclose_days)
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
                disclose_date = datetime.strptime(disclose_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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
                                     now: Optional[datetime] = None) -> Dict[str, Any]:
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
                    exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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
        
        # Cap symbol focus at 10% to avoid extreme confidence values
        symbol_focus_pct = min(10.0, symbol_focus_pct)
        
        # Use symbol focus % as confidence modifier (capped at 10%)
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
    
    def _calculate_confidence(self, trade: Dict[str, Any], 
                             trader_confidence_modifier: float) -> float:
        """
        Calculate confidence for a trade recommendation.
        
        Formula:
        1. Start at 50% base confidence
        2. Add trader pattern modifier (0 to +10, from the trader's symbol portfolio focus %)
        3. Add investment size boost (up to +20%)

        Args:
            trade: Trade record with amount information
            trader_confidence_modifier: Confidence adjustment from the trader's symbol
                portfolio focus (0 to +10; capped at 10 in _calculate_trader_confidence)
            
        Returns:
            Confidence percentage (0-100)
        """
        # Base confidence
        confidence = 50.0
        
        # Add trader pattern modifier
        confidence += trader_confidence_modifier
        
        # Add investment size boost (up to +20% for very large trades)
        try:
            amount = self._parse_amount(trade.get('amount', '0'))

            # Calculate boost: +10% per $500k, capped at +20%
            investment_boost = min(20.0, (amount / 500000) * 10.0)
            confidence += investment_boost
            
            self.logger.debug(f"Trade amount: ${amount:,.0f} -> boost: +{investment_boost:.1f}%")
            
        except Exception as e:
            self.logger.debug(f"Error parsing trade amount: {e}")
        
        # Final cap at 100%, floor at 0%
        confidence = min(100.0, max(0.0, confidence))
        
        return confidence
    
    def _calculate_recommendation(self, filtered_trades: List[Dict[str, Any]],
                                  symbol: str,
                                  current_price: float,
                                  max_exec_days: int,
                                  trader_history_by_name: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                                  min_traders: Optional[int] = None,
                                  min_trades_required: Optional[int] = None,
                                  growth_multiplier: Optional[float] = None,
                                  confidence_to_profit_factor: Optional[float] = None,
                                  now: Optional[datetime] = None) -> Dict[str, Any]:
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

            # Calculate confidence modifier based on trader's portfolio allocation to this symbol
            # This looks at what % of their money is focused on the current symbol
            trader_stats = self._calculate_trader_confidence(trader_history or [], transaction_type, symbol, current_price, max_exec_days, now=now)
            trader_confidence_modifier = trader_stats['confidence_modifier']
            symbol_focus_pct = trader_stats.get('symbol_focus_pct', 0.0)
            
            # Calculate confidence for this specific trade
            trade_confidence = self._calculate_confidence(trade, trader_confidence_modifier)
            
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
                           f"{num_total_trades} trades (min {min_trades_required}) - returning HOLD")
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': f'Below minimum thresholds: {num_unique_traders} unique trader(s) (min {min_traders}), '
                          f'{num_total_trades} trade(s) (min {min_trades_required}). Both must be met.',
                'trades': trade_details,
                'trade_count': len(filtered_trades),
                'buy_count': buy_count,
                'sell_count': sell_count,
                'total_buy_amount': total_buy_amount,
                'total_sell_amount': total_sell_amount,
                'avg_price_delta': 0.0,
                'price_confidence_adj': 0.0
            }

        # Determine overall signal based on net portfolio allocation
        # Compare the total symbol focus % for buy trades vs sell trades
        # This weighs traders by how much of their portfolio they're allocating to this symbol
        
        # Calculate total symbol focus for buy and sell sides
        buy_symbol_focus_total = sum(t['symbol_focus_pct'] for t in trade_details 
                                     if 'purchase' in t['type'].lower() or 'buy' in t['type'].lower())
        sell_symbol_focus_total = sum(t['symbol_focus_pct'] for t in trade_details 
                                      if 'sale' in t['type'].lower() or 'sell' in t['type'].lower())
        
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

        overall_confidence = min(100.0, max(0.0, 50.0 + avg_symbol_focus_pct * growth_multiplier + price_confidence_adj))
        
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
- Symbol Focus: {trade_info['symbol_focus_pct']:.1f}% (of trader's portfolio, capped at 10%)
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

Signal Determination:
- Buy Trades: {buy_count} trades (${total_buy_amount:,.0f}) with {buy_symbol_focus_total:.1f}% total portfolio focus
- Sell Trades: {sell_count} trades (${total_sell_amount:,.0f}) with {sell_symbol_focus_total:.1f}% total portfolio focus
- Net Portfolio Focus: {net_symbol_focus:+.1f}% (buy focus - sell focus)
- Signal is determined by net portfolio allocation, not just trade count
- More portfolio focus on buys = BUY, more on sells = SELL, equal = HOLD

Confidence Calculation Method:
1. Calculate Symbol Focus % for each trader:
   - Look at all their trades in the past year
   - Calculate: ($ spent on {symbol} / $ spent on all symbols) × 100
   - This shows what % of their portfolio is allocated to {symbol}
   - Cap at 10% to avoid extreme values
2. Average Symbol Focus across relevant {signal.value} traders: {avg_symbol_focus_pct:.1f}%
3. Price Movement Adjustment: avg delta {avg_price_delta:+.1f}% → {price_confidence_adj:+.1f} confidence
4. Confidence Formula: 50 + (Avg Symbol Focus % × {growth_multiplier}) + price adj ({price_confidence_adj:+.1f}) = {overall_confidence:.1f}%
   - 10% portfolio allocation × {growth_multiplier} = {10 * growth_multiplier:.0f}% bonus
   - Price dropped 10% on BUY → +5 confidence (better entry)
   - Price rose 10% on BUY → -5 confidence (opportunity partly gone)
5. Expected Profit: Uses same formula = {abs(expected_profit):.1f}%

Symbol Focus Analysis:
This measures how much conviction/focus the trader has on {symbol}.
Higher allocation % = trader is putting significant money into this stock → higher confidence
Lower allocation % = trader is just dabbling or diversifying → lower confidence
Focus is capped at 10% to represent maximum conviction.

Example: If trader bought $100k of {symbol} and $1M total stocks → 10% focus → maximum confidence

Settings:
- Growth Confidence Multiplier: {growth_multiplier} (configurable, controls sensitivity)

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
                        'price_confidence_adj': recommendation_data.get('price_confidence_adj', 0.0)
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
