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
import requests

from ...core.interfaces import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ...logger import get_expert_logger
from ...config import get_app_setting


class FMPSenateTraderWeight(MarketExpertInterface):
    """
    FMPSenateTraderWeight Expert Implementation
    
    Expert that uses FMP's Senate/House trading data to generate recommendations
    using a sophisticated weighted algorithm. Analyzes government official trades
    for a symbol, evaluates trader performance history, and calculates confidence based on:
    1. Portfolio allocation percentages (symbol focus)
    2. Historical trader performance
    3. Investment size and timing
    """
    
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
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
    
    def _get_fmp_api_key(self) -> Optional[str]:
        """Get FMP API key from app settings."""
        api_key = get_app_setting('FMP_API_KEY')
        if not api_key:
            self.logger.warning("FMP API key not found in app settings")
        return api_key
    
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
                "description": "Growth to confidence multiplier",
                "tooltip": "Multiplier applied to trader's average growth on the symbol to calculate confidence. Formula: 50 + (avg_growth * multiplier). Higher values increase confidence for successful traders. Example: 10% growth * 5.0 = 50% bonus confidence."
            },
            "confidence_to_profit_factor": {
                "type": "float",
                "required": True,
                "default": 0.15,
                "description": "Confidence to profit factor",
                "tooltip": "Factor to convert confidence to expected profit. Default 0.15 means 100% confidence = 15% expected profit. Formula: expected_profit = confidence * factor."
            }
        }
    
    def _fetch_senate_trades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch senate trades from FMP API for a specific symbol.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            List of trade records or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch senate trades: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/stable/senate-trades"
            params = {
                "symbol": symbol.upper(),
                "apikey": self._api_key
            }
            
            self.logger.debug(f"Fetching FMP senate trades for {symbol}")
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} senate trade records for {symbol}")
            
            return data if isinstance(data, list) else []
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Failed to fetch FMP senate trades for {symbol}: {e}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error fetching senate trades for {symbol}: {e}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
    
    def _fetch_house_trades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch house trades from FMP API for a specific symbol.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            List of trade records or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch house trades: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/stable/house-trades"
            params = {
                "symbol": symbol.upper(),
                "apikey": self._api_key
            }
            
            self.logger.debug(f"Fetching FMP house trades for {symbol}")
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} house trade records for {symbol}")
            
            return data if isinstance(data, list) else []
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Failed to fetch FMP house trades for {symbol}: {e}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error fetching house trades for {symbol}: {e}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
    
    def _fetch_trader_history(self, trader_name: str) -> Optional[List[Dict[str, Any]]]:
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
        
        # Retry configuration
        max_retries = 3
        timeout = 60  # Increased timeout for FMP API
        
        # Fetch senate trades by name
        for attempt in range(1, max_retries + 1):
            try:
                senate_url = f"https://financialmodelingprep.com/stable/senate-trades-by-name"
                senate_params = {
                    "name": trader_name,
                    "apikey": self._api_key
                }
                
                self.logger.debug(f"Fetching senate trade history for {trader_name} (attempt {attempt}/{max_retries})")
                senate_response = requests.get(senate_url, params=senate_params, timeout=timeout)
                senate_response.raise_for_status()
                
                senate_data = senate_response.json()
                if isinstance(senate_data, list):
                    all_trades.extend(senate_data)
                    self.logger.debug(f"Found {len(senate_data)} senate trades by {trader_name}")
                break  # Success, exit retry loop
                
            except requests.exceptions.ReadTimeout as e:
                if attempt < max_retries:
                    self.logger.warning(f"Timeout fetching senate trades for {trader_name} (attempt {attempt}/{max_retries}), retrying...")
                    continue
                else:
                    self.logger.error(f"Failed to fetch senate trades for {trader_name} after {max_retries} attempts: {e}")
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Failed to fetch senate trader history for {trader_name}: {e}")
                break  # Don't retry non-timeout errors
            except Exception as e:
                self.logger.error(f"Unexpected error fetching senate trader history for {trader_name}: {e}")
                break
        
        # Fetch house trades by name
        for attempt in range(1, max_retries + 1):
            try:
                house_url = f"https://financialmodelingprep.com/stable/house-trades-by-name"
                house_params = {
                    "name": trader_name,
                    "apikey": self._api_key
                }
                
                self.logger.debug(f"Fetching house trade history for {trader_name} (attempt {attempt}/{max_retries})")
                house_response = requests.get(house_url, params=house_params, timeout=timeout)
                house_response.raise_for_status()
                
                house_data = house_response.json()
                if isinstance(house_data, list):
                    all_trades.extend(house_data)
                    self.logger.debug(f"Found {len(house_data)} house trades by {trader_name}")
                break  # Success, exit retry loop
                
            except requests.exceptions.ReadTimeout as e:
                if attempt < max_retries:
                    self.logger.warning(f"Timeout fetching house trades for {trader_name} (attempt {attempt}/{max_retries}), retrying...")
                    continue
                else:
                    self.logger.error(f"Failed to fetch house trades for {trader_name} after {max_retries} attempts: {e}")
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Failed to fetch house trader history for {trader_name}: {e}")
                break  # Don't retry non-timeout errors
            except Exception as e:
                self.logger.error(f"Unexpected error fetching house trader history for {trader_name}: {e}")
                break
        
        self.logger.debug(f"Found total of {len(all_trades)} trades by {trader_name} (senate + house)")
        return all_trades if all_trades else None
    
    def _get_price_at_date(self, symbol: str, date: datetime) -> Optional[float]:
        """
        Get the stock price at a specific date using FMP API.
        
        Args:
            symbol: Stock symbol
            date: Date to get price for
            
        Returns:
            Opening price on that date or None if not available
        """
        if not self._api_key:
            return None
        
        url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol.upper()}"
        params = {
            "from": date.strftime("%Y-%m-%d"),
            "to": date.strftime("%Y-%m-%d"),
            "apikey": self._api_key
        }
        
        # Retry logic: 3 attempts with increased timeout
        max_retries = 3
        timeout = 60  # Increased timeout for FMP API
        
        for attempt in range(1, max_retries + 1):
            try:
                self.logger.debug(f"Fetching price for {symbol} on {date.date()} (attempt {attempt}/{max_retries})")
                response = requests.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                
                data = response.json()
                
                # Extract historical data
                historical = data.get('historical', [])
                if historical and len(historical) > 0:
                    price = historical[0].get('open')
                    self.logger.debug(f"Got price ${price} for {symbol} on {date.date()}")
                    return price
                
                self.logger.warning(f"No price data found for {symbol} on {date.date()}")
                return None
                
            except requests.exceptions.ReadTimeout as e:
                if attempt < max_retries:
                    self.logger.warning(f"Timeout fetching price for {symbol} on {date.date()} (attempt {attempt}/{max_retries}), retrying...")
                    continue
                else:
                    self.logger.error(f"Failed to fetch price for {symbol} on {date.date()} after {max_retries} attempts: {e}")
                    return None
            except Exception as e:
                self.logger.error(f"Error fetching price for {symbol} on {date.date()}: {e}")
                return None
        
        return None
    
    def _get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for the symbol from the account."""
        try:
            from ...core.utils import get_account_instance_from_id
            
            expert_instance = get_instance(ExpertInstance, self.id)
            if not expert_instance:
                return None
            
            account = get_account_instance_from_id(expert_instance.account_id)
            if not account:
                return None
            
            return account.get_instrument_current_price(symbol)
            
        except Exception as e:
            self.logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None
    
    def _filter_trades(self, trades: List[Dict[str, Any]], 
                      max_disclose_days: int, 
                      max_exec_days: int,
                      max_price_delta_pct: float,
                      current_price: float,
                      symbol: str) -> List[Dict[str, Any]]:
        """
        Filter trades based on configured settings.
        
        Args:
            trades: List of trade records
            max_disclose_days: Maximum days since disclosure
            max_exec_days: Maximum days since execution
            max_price_delta_pct: Maximum price change percentage
            current_price: Current stock price
            symbol: Stock symbol to filter for
            
        Returns:
            Filtered list of trades
        """
        now = datetime.now(timezone.utc)
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
                
                # Only fetch price if trade passed all date filters
                # This avoids unnecessary API calls for old trades
                exec_price = self._get_price_at_date(trade.get('symbol', ''), exec_date)
                if not exec_price:
                    self.logger.debug(f"Could not get execution price for {trader_name}'s trade, skipping")
                    continue
                
                # Check price delta - only filter out unfavourable moves
                # BUY trades: price dropping is unfavourable (negative delta)
                # SELL trades: price rising is unfavourable (positive delta)
                price_delta_pct = (current_price - exec_price) / exec_price * 100
                trade_type = trade.get('type', '').lower()
                is_buy = 'purchase' in trade_type or 'buy' in trade_type
                unfavourable_move = -price_delta_pct if is_buy else price_delta_pct
                if unfavourable_move > max_price_delta_pct:
                    self.logger.debug(f"Price moved {price_delta_pct:+.1f}% against {'BUY' if is_buy else 'SELL'} "
                                      f"(max unfavourable: {max_price_delta_pct}%), filtering out")
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
    
    def _calculate_trader_confidence(self, trader_history: List[Dict[str, Any]], 
                                     current_trade_type: str,
                                     current_symbol: str,
                                     current_price: float,
                                     max_exec_days: int = 60) -> Dict[str, Any]:
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
        
        # Time thresholds
        now = datetime.now(timezone.utc)
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
                except:
                    continue
                
                # Parse amount (remove $, commas, extract numeric value)
                # Format is typically "$15,001 - $50,000" or similar
                if '-' in amount_str:
                    # Take the average of the range
                    parts = amount_str.split('-')
                    low = ''.join(c for c in parts[0] if c.isdigit() or c == '.')
                    high = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
                    amount = (float(low) + float(high)) / 2 if low and high else 0
                else:
                    amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
                    amount = float(amount_str) if amount_str else 0
                
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
        2. Add trader pattern modifier (-30 to +30)
        3. Add investment size boost (up to +20%)
        
        Args:
            trade: Trade record with amount information
            trader_confidence_modifier: Confidence adjustment based on trader's pattern (-30 to +30)
            
        Returns:
            Confidence percentage (0-100)
        """
        # Base confidence
        confidence = 50.0
        
        # Add trader pattern modifier
        confidence += trader_confidence_modifier
        
        # Add investment size boost (up to +20% for very large trades)
        try:
            amount_str = trade.get('amount', '0')
            
            # Parse amount (handle ranges like "$15,001 - $50,000")
            if '-' in amount_str:
                parts = amount_str.split('-')
                low = ''.join(c for c in parts[0] if c.isdigit() or c == '.')
                high = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
                amount = (float(low) + float(high)) / 2 if low and high else 0
            else:
                amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
                amount = float(amount_str) if amount_str else 0
            
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
                                  max_exec_days: int) -> Dict[str, Any]:
        """
        Calculate trading recommendation from filtered senate trades.
        
        Args:
            filtered_trades: List of relevant trades after filtering
            symbol: Stock symbol
            current_price: Current stock price
            max_exec_days: Maximum days since execution (for filtering trader history)
            
        Returns:
            Dictionary with recommendation details
        """
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
            
            # Get trader's full history (all symbols, all time) to assess trading pattern
            trader_history = self._fetch_trader_history(trader_name)
            self.logger.debug(f"Found {len(trader_history or [])} total trades by {trader_name}")
            
            # Calculate confidence modifier based on trader's portfolio allocation to this symbol
            # This looks at what % of their money is focused on the current symbol
            trader_stats = self._calculate_trader_confidence(trader_history or [], transaction_type, symbol, current_price, max_exec_days)
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
                try:
                    amount_str = trade.get('amount', '0')
                    # Parse amount range (e.g., "$15,001 - $50,000")
                    if '-' in amount_str:
                        parts = amount_str.split('-')
                        low = ''.join(c for c in parts[0] if c.isdigit() or c == '.')
                        high = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
                        amount = (float(low) + float(high)) / 2 if low and high else 0
                    else:
                        amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
                        amount = float(amount_str) if amount_str else 0
                    total_buy_amount += amount
                except:
                    pass
            elif is_sell:
                sell_count += 1
                try:
                    amount_str = trade.get('amount', '0')
                    # Parse amount range (e.g., "$15,001 - $50,000")
                    if '-' in amount_str:
                        parts = amount_str.split('-')
                        low = ''.join(c for c in parts[0] if c.isdigit() or c == '.')
                        high = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
                        amount = (float(low) + float(high)) / 2 if low and high else 0
                    else:
                        amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
                        amount = float(amount_str) if amount_str else 0
                    total_sell_amount += amount
                except:
                    pass
        
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
        
        # Get growth confidence multiplier from settings
        growth_multiplier = self.get_setting_with_interface_default('growth_confidence_multiplier')
        
        # Calculate average symbol focus percentage across relevant trades
        if relevant_trades:
            avg_symbol_focus_pct = sum(t['symbol_focus_pct'] for t in relevant_trades) / len(relevant_trades)
        else:
            avg_symbol_focus_pct = 0.0
        
        # Apply symbol focus-based formula
        # Confidence: 50 + (symbol_focus_pct * multiplier)
        # Logic: symbol_focus_pct is capped at 10%, so with default multiplier 5.0:
        #   10% portfolio allocation * 5.0 = 50% bonus = 100% total confidence
        #   0% portfolio allocation * 5.0 = 0% bonus = 50% total confidence
        overall_confidence = min(100.0, max(0.0, 50.0 + avg_symbol_focus_pct * growth_multiplier))
        
        # Expected Profit: Confidence multiplied by profit factor (always positive regardless of BUY/SELL)
        # Example: 80% confidence * 0.15 factor = 12% expected profit
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
3. Confidence Formula: 50 + (Avg Symbol Focus % × {growth_multiplier}) = {overall_confidence:.1f}%
   - 10% portfolio allocation × {growth_multiplier} = {10 * growth_multiplier:.0f}% bonus = {50 + 10 * growth_multiplier:.0f}% confidence
   - 0% portfolio allocation × {growth_multiplier} = 0% bonus = 50% confidence
4. Expected Profit: Uses same formula = {abs(expected_profit):.1f}%

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
        from ...core.utils import calculate_fmp_trade_metrics
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
                price_at_date=current_price or 0.0,
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
            
            # Get settings
            settings_def = self.get_settings_definitions()
            max_disclose_days = int(self.get_setting_with_interface_default('max_disclose_date_days'))
            max_exec_days = int(self.get_setting_with_interface_default('max_trade_exec_days'))
            max_price_delta_pct = float(self.get_setting_with_interface_default('max_trade_price_delta_pct'))
            
            # Get current price first
            current_price = self._get_current_price(symbol)
            if not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")
            
            # Fetch both senate and house trades
            senate_trades = self._fetch_senate_trades(symbol)
            house_trades = self._fetch_house_trades(symbol)
            
            if senate_trades is None and house_trades is None:
                raise ValueError("Failed to fetch trades from FMP API (both senate and house failed)")
            
            # Combine trades from both sources
            all_trades = []
            if senate_trades:
                all_trades.extend(senate_trades)
            if house_trades:
                all_trades.extend(house_trades)
            
            self.logger.info(f"Fetched {len(senate_trades) if senate_trades else 0} senate trades and "
                       f"{len(house_trades) if house_trades else 0} house trades for {symbol}")
            
            # Filter trades based on settings
            filtered_trades = self._filter_trades(
                all_trades, max_disclose_days, max_exec_days, 
                max_price_delta_pct, current_price, symbol
            )
            
            # Calculate recommendation
            recommendation_data = self._calculate_recommendation(
                filtered_trades, symbol, current_price, max_exec_days
            )
            
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
                        'percent_of_yearly': trade_metrics.get('percent_of_yearly', 0.0)
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
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """
        Render FMPSenateTrade market analysis results in the UI.
        
        Args:
            market_analysis: The market analysis instance to render.
        """
        from nicegui import ui
        
        try:
            # Handle different analysis states
            if market_analysis.status == MarketAnalysisStatus.PENDING:
                self._render_pending(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.RUNNING:
                self._render_running(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.FAILED:
                self._render_failed(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.COMPLETED:
                self._render_completed(market_analysis)
            elif market_analysis.status == MarketAnalysisStatus.SKIPPED:
                self._render_skipped(market_analysis)
            else:
                with ui.card().classes('w-full p-4'):
                    ui.label(f"Unknown analysis status: {market_analysis.status}")
                    
        except Exception as e:
            self.logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
            with ui.card().classes('w-full p-8 text-center'):
                ui.icon('error', size='3rem', color='negative').classes('mb-4')
                ui.label('Rendering Error').classes('text-h5 text-negative')
                ui.label(f'Failed to render analysis: {str(e)}').classes('text-grey-7')
    
    def _render_pending(self, market_analysis: MarketAnalysis) -> None:
        """Render pending analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-8 text-center'):
            ui.icon('schedule', size='3rem', color='grey').classes('mb-4')
            ui.label('Analysis Pending').classes('text-h5')
            ui.label(f'Senate trade analysis for {market_analysis.symbol} is queued').classes('text-grey-7')
    
    def _render_running(self, market_analysis: MarketAnalysis) -> None:
        """Render running analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-8 text-center'):
            ui.spinner(size='3rem', color='primary').classes('mb-4')
            ui.label('Analysis Running').classes('text-h5')
            ui.label(f'Fetching senate/house trading data for {market_analysis.symbol}...').classes('text-grey-7')
    
    def _render_failed(self, market_analysis: MarketAnalysis) -> None:
        """Render failed analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('error', color='negative', size='2rem')
                ui.label('Analysis Failed').classes('text-h5 text-negative ml-2')
            
            if market_analysis.state and isinstance(market_analysis.state, dict):
                error_msg = market_analysis.state.get('error', 'Unknown error')
                ui.label(f'Error: {error_msg}').classes('text-grey-8')
    
    def _render_skipped(self, market_analysis: MarketAnalysis) -> None:
        """Render skipped analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('skip_next', color='orange', size='2rem')
                ui.label('Analysis Skipped').classes('text-h5 text-orange ml-2')
            
            if market_analysis.state and isinstance(market_analysis.state, dict):
                skip_reason = market_analysis.state.get('skip_reason', 'Analysis was skipped')
                ui.label(f'Reason: {skip_reason}').classes('text-grey-8')
    
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
                                    
                                    # Trader activity statistics
                                    with ui.column().classes('mt-2 text-xs').style('color: #718096'):
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
                                        ui.label(f'Pattern: {modifier:+.1f}%').classes(f'text-xs text-{modifier_color}')
                    
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

For each trade:
1. **Base Confidence**: Start at 50%
2. **+ Trader Performance**: Add historical growth % from trader's past trades
   - Positive if past trades gained value
   - Negative if past trades lost value
3. **Investment Size Boost**: +5% per $100k invested
4. **Cap**: Maximum 80% before investment boost, 100% final cap

**Trade Filtering:**
- Only trades disclosed within last **{max_disclose}** days
- Only trades executed within last **{max_exec}** days  
- Only trades where price hasn't moved more than **±{max_delta}%** (opportunity still available)

**Signal Logic:**
- **BUY**: More government officials buying than selling
- **SELL**: More selling than buying
- **HOLD**: Equal activity or no relevant trades

**Expected Profit**: Based on average price movement since trade execution dates

**Note**: Government officials must disclose trades within 30-45 days, creating a natural delay. This expert looks for patterns in disclosed trades that may still have upside/downside potential.
                    '''.format(max_disclose=max_disclose, max_exec=max_exec, max_delta=max_delta)).classes('text-sm')
