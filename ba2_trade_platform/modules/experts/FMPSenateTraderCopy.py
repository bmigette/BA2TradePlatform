"""
FMP Senate/House Trade Copy Expert

Expert that copies trades from specific government officials using FMP's
Senate Trading API. When specified senators/representatives make trades,
this expert generates immediate recommendations with 100% confidence
and fixed profit expectations.

Key Features:
- can_recommend_instruments: True (can select its own instruments)
- should_expand_instrument_jobs: False (no job duplication)
- Simple copy trading logic with 100% confidence
- Age-based filtering for relevance

API Documentation: https://site.financialmodelingprep.com/developer/docs#senate-trading
"""

from typing import Any, Dict, Optional, List
from datetime import datetime, timezone, timedelta
import json
import requests

from ...core.interfaces import MarketExpertInterface
from ...core.models import ExpertInstance, MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ...core.db import get_db, get_instance, update_instance, add_instance
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon, AnalysisUseCase
from ...logger import get_expert_logger
from ...config import get_app_setting


class FMPSenateTraderCopy(MarketExpertInterface):
    """
    FMPSenateTraderCopy Expert Implementation
    
    Expert that copies trades from specific government officials across multiple instruments.
    Operates in multi-instrument mode: fetches all trades from followed traders and 
    generates separate ExpertRecommendation records for each symbol they traded.
    
    When a trade is found from a followed senator/representative within the age criteria, 
    generates immediate BUY/SELL recommendation with 100% confidence and 50% expected profit.
    
    Does not expand instrument jobs to avoid duplication - processes all instruments 
    in a single analysis run.
    """
    
    @classmethod
    def description(cls) -> str:
        return "Copy trades from specific senators/representatives with 100% confidence"
    
    @classmethod
    def get_expert_properties(cls) -> Dict[str, Any]:
        """
        Get expert-specific properties and capabilities.
        
        Returns:
            Dict[str, Any]: Dictionary containing expert properties and capabilities
        """
        return {
            "can_recommend_instruments": True,  # Reserved for future job expansion functionality
            "should_expand_instrument_jobs": False,  # Prevent job duplication - expert processes all instruments at once
        }
    

    
    def __init__(self, id: int):
        """Initialize FMPSenateTraderCopy expert with database instance."""
        super().__init__(id)
        
        self._load_expert_instance(id)
        self._api_key = self._get_fmp_api_key()
        
        # Initialize expert-specific logger
        self.logger = get_expert_logger("FMPSenateTraderCopy", id)
    
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
        """Define configurable settings for FMPSenateTraderCopy expert."""
        return {
            "copy_trade_names": {
                "type": "str",
                "required": True,
                "default": "",
                "description": "Senators/representatives to copy trade (comma-separated)",
                "tooltip": "Enter names of senators/representatives to copy trade (e.g., 'Nancy Pelosi, Josh Gottheimer'). Any trade by these people will generate 100% confidence BUY/SELL recommendation with 50% expected profit."
            },
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
            "close_only_for_same_trader": {
                "type": "bool",
                "required": True,
                "default": True,
                "description": "For Open Positions: Only close if same trader reverses",
                "tooltip": "When True, SELL/BUY recommendations for closing positions only trigger if the original trader reverses direction. When False, uses standard averaging logic for the lookback interval."
            },
        }
    
    def _fetch_senate_trades(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch senate trades from FMP API for a specific symbol or all trades.
        
        Args:
            symbol: Optional stock symbol to query. If None, fetches all trades.
            
        Returns:
            List of trade records or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch senate trades: FMP API key not configured")
            return None
        
        try:
            # Use different endpoints based on whether symbol is provided
            if symbol:
                # Use symbol-specific endpoint
                url = f"https://financialmodelingprep.com/stable/senate-trades"
                params = {
                    "apikey": self._api_key,
                    "symbol": symbol.upper()
                }
                self.logger.debug(f"Fetching FMP senate trades for {symbol}")
            else:
                # Use latest disclosures endpoint with pagination for all trades
                url = f"https://financialmodelingprep.com/stable/senate-latest"
                params = {
                    "apikey": self._api_key,
                    "page": 0,
                    "limit": 1000  # Maximum allowed per request
                }
                self.logger.debug("Fetching all FMP senate trades (latest disclosures)")
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            symbol_text = f" for {symbol}" if symbol else " (all)"
            self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} senate trade records{symbol_text}")
            
            return data if isinstance(data, list) else []
            
        except requests.exceptions.RequestException as e:
            symbol_text = f" for {symbol}" if symbol else " (all)"
            self.logger.error(f"Failed to fetch FMP senate trades{symbol_text}: {e}", exc_info=True)
            return None
        except Exception as e:
            symbol_text = f" for {symbol}" if symbol else " (all)"
            self.logger.error(f"Unexpected error fetching senate trades{symbol_text}: {e}", exc_info=True)
            return None
    
    def _fetch_house_trades(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch house trades from FMP API for a specific symbol or all trades.
        
        Args:
            symbol: Optional stock symbol to query. If None, fetches all trades.
            
        Returns:
            List of trade records or None if error
        """
        if not self._api_key:
            self.logger.error("Cannot fetch house trades: FMP API key not configured")
            return None
        
        try:
            # Use different endpoints based on whether symbol is provided
            if symbol:
                # Use symbol-specific endpoint
                url = f"https://financialmodelingprep.com/stable/house-trades"
                params = {
                    "apikey": self._api_key,
                    "symbol": symbol.upper()
                }
                self.logger.debug(f"Fetching FMP house trades for {symbol}")
            else:
                # Use latest disclosures endpoint with pagination for all trades
                url = f"https://financialmodelingprep.com/stable/house-latest"
                params = {
                    "apikey": self._api_key,
                    "page": 0,
                    "limit": 1000  # Maximum allowed per request
                }
                self.logger.debug("Fetching all FMP house trades (latest disclosures)")
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            symbol_text = f" for {symbol}" if symbol else " (all)"
            self.logger.debug(f"Received {len(data) if isinstance(data, list) else 0} house trade records{symbol_text}")
            
            return data if isinstance(data, list) else []
            
        except requests.exceptions.RequestException as e:
            symbol_text = f" for {symbol}" if symbol else " (all)"
            self.logger.error(f"Failed to fetch FMP house trades{symbol_text}: {e}", exc_info=True)
            return None
        except Exception as e:
            symbol_text = f" for {symbol}" if symbol else " (all)"
            self.logger.error(f"Unexpected error fetching house trades{symbol_text}: {e}", exc_info=True)
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
    
    def _filter_trades_by_age(self, trades: List[Dict[str, Any]], 
                             max_disclose_days: int, 
                             max_exec_days: int,
                             symbol: str) -> List[Dict[str, Any]]:
        """
        Filter trades based on age criteria.
        
        Args:
            trades: List of trade records
            max_disclose_days: Maximum days since disclosure
            max_exec_days: Maximum days since execution
            symbol: Stock symbol to filter for
            
        Returns:
            Filtered list of trades
        """
        now = datetime.now(timezone.utc)
        filtered_trades = []
        max_exec_days = int(max_exec_days)
        max_disclose_days = int(max_disclose_days)
        
        for trade in trades:
            try:
                # Parse dates
                disclose_date_str = trade.get('disclosureDate', '')
                exec_date_str = trade.get('transactionDate', '')
                
                trader_name = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'

                if not disclose_date_str or not exec_date_str:
                    self.logger.debug(f"Trade missing dates, skipping: {trader_name}")
                    continue
                
                # Parse dates (FMP returns YYYY-MM-DD format)
                disclose_date = datetime.strptime(disclose_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

                # Check if this is the correct symbol (case insensitive)
                trade_symbol = trade.get('symbol', '').upper()
                if trade_symbol != symbol.upper():
                    continue
                
                # Check disclose date
                days_since_disclose = (now - disclose_date).days
                if days_since_disclose > max_disclose_days:
                    continue
                
                # Check execution date
                days_since_exec = (now - exec_date).days
                if days_since_exec > max_exec_days:
                    continue
                
                # Add calculated fields to trade
                trade['days_since_disclose'] = days_since_disclose
                trade['days_since_exec'] = days_since_exec
                trade['disclose_date'] = disclose_date_str
                trade['exec_date'] = exec_date_str
                
                filtered_trades.append(trade)
                
            except Exception as e:
                self.logger.error(f"Error processing trade: {e}", exc_info=True)
                continue
        
        self.logger.info(f"Filtered {len(filtered_trades)} trades from {len(trades)} total based on age criteria")
        return filtered_trades
    
    def _filter_trades(self, trades: List[Dict[str, Any]], 
                      copy_trade_names: List[str],
                      max_disclose_days: int, 
                      max_exec_days: int) -> List[Dict[str, Any]]:
        """
        Filter trades based on trader names and age criteria.
        
        Args:
            trades: List of trade records
            copy_trade_names: List of trader names to copy (lowercase)
            max_disclose_days: Maximum days since disclosure
            max_exec_days: Maximum days since execution
            
        Returns:
            Filtered list of trades
        """
        now = datetime.now(timezone.utc)
        filtered_trades = []
        max_exec_days = int(max_exec_days)
        max_disclose_days = int(max_disclose_days)
        
        for trade in trades:
            try:
                # Parse dates
                disclose_date_str = trade.get('disclosureDate', '')
                exec_date_str = trade.get('transactionDate', '')
                
                trader_name = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'
                trader_name_lower = trader_name.lower()

                if not disclose_date_str or not exec_date_str:
                    self.logger.debug(f"Trade missing dates, skipping: {trader_name}")
                    continue
                
                # Filter by trader name first (before date calculations to save time)
                if trader_name_lower not in copy_trade_names:
                    continue
                
                # Parse dates (FMP returns YYYY-MM-DD format)
                disclose_date = datetime.strptime(disclose_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

                # Check disclose date
                days_since_disclose = (now - disclose_date).days
                if days_since_disclose > max_disclose_days:
                    continue
                
                # Check execution date
                days_since_exec = (now - exec_date).days
                if days_since_exec > max_exec_days:
                    continue
                
                # Trade passed all filters
                filtered_trades.append(trade)
                
            except Exception as e:
                self.logger.warning(f"Error processing trade {trade}: {e}")
                continue
        
        self.logger.info(f"Filtered {len(filtered_trades)} trades from {len(trades)} total by trader names and age")
        return filtered_trades
    
    def _filter_trades_by_age_multi(self, trades: List[Dict[str, Any]], 
                                   max_disclose_days: int, 
                                   max_exec_days: int) -> List[Dict[str, Any]]:
        """
        Filter trades based on age criteria without symbol filtering.
        
        Args:
            trades: List of trade records
            max_disclose_days: Maximum days since disclosure
            max_exec_days: Maximum days since execution
            
        Returns:
            Filtered list of trades
        """
        now = datetime.now(timezone.utc)
        filtered_trades = []
        max_exec_days = int(max_exec_days)
        max_disclose_days = int(max_disclose_days)
        
        for trade in trades:
            try:
                # Parse dates
                disclose_date_str = trade.get('disclosureDate', '')
                exec_date_str = trade.get('transactionDate', '')
                
                trader_name = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'

                if not disclose_date_str or not exec_date_str:
                    self.logger.debug(f"Trade missing dates, skipping: {trader_name}")
                    continue
                
                # Parse dates (FMP returns YYYY-MM-DD format)
                disclose_date = datetime.strptime(disclose_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

                # Check disclose date
                days_since_disclose = (now - disclose_date).days
                if days_since_disclose > max_disclose_days:
                    continue
                
                # Check execution date
                days_since_exec = (now - exec_date).days
                if days_since_exec > max_exec_days:
                    continue
                
                # Add calculated fields to trade
                trade['days_since_disclose'] = days_since_disclose
                trade['days_since_exec'] = days_since_exec
                trade['disclose_date'] = disclose_date_str
                trade['exec_date'] = exec_date_str
                
                filtered_trades.append(trade)
                
            except Exception as e:
                self.logger.error(f"Error processing trade: {e}", exc_info=True)
                continue
        
        self.logger.info(f"Filtered {len(filtered_trades)} trades from {len(trades)} total based on age criteria (multi-symbol)")
        return filtered_trades
    
    def _check_open_positions_trader_status(self, symbol: str) -> tuple[int, int]:
        """
        Check the trader name status of open positions for a given symbol.
        
        This is used for backward compatibility: if orders were created before trader name linking
        was implemented, they won't have trader names. In mixed state (some with, some without),
        we should fall back to averaging logic.
        
        Args:
            symbol: The trading symbol
            
        Returns:
            Tuple of (total_orders_count, orders_with_trader_names_count)
            - total_orders_count: Total number of open positions
            - orders_with_trader_names_count: Count of orders with trader names linked
        """
        try:
            from sqlmodel import Session, select
            from ...core.db import get_db
            from ...core.models import TradingOrder, ExpertRecommendation
            from ...core.types import OrderStatus
            
            with Session(get_db().bind) as session:
                # Query for trading orders with this symbol that are open
                # Orders linked to this expert via recommendations
                statement = select(TradingOrder).join(
                    ExpertRecommendation,
                    TradingOrder.expert_recommendation_id == ExpertRecommendation.id,
                    isouter=True
                ).where(
                    ExpertRecommendation.instance_id == self.id,
                    TradingOrder.symbol == symbol,
                    TradingOrder.status.in_([
                        OrderStatus.PENDING,
                        OrderStatus.NEW,
                        OrderStatus.PARTIALLY_FILLED,
                        OrderStatus.FILLED
                    ])
                )
                
                orders = session.exec(statement).all()
                total_orders = len(orders)
                
                # Count orders with trader names linked
                orders_with_trader_names = 0
                for order in orders:
                    if order.data and 'trader_name' in order.data:
                        trader_name = order.data.get('trader_name', '').strip()
                        if trader_name:
                            orders_with_trader_names += 1
                
                if total_orders > 0:
                    self.logger.debug(f"Open positions check for {symbol}: {total_orders} total, {orders_with_trader_names} with trader names")
                else:
                    self.logger.debug(f"No open positions for {symbol}")
                
                return total_orders, orders_with_trader_names
                
        except Exception as e:
            self.logger.error(f"Error checking open positions trader status for {symbol}: {e}", exc_info=True)
            # Default to (0, 0) (no orders) if error occurs - this will cause same_trader mode to proceed
            return 0, 0
    
    def _find_copy_trades(self, filtered_trades: List[Dict[str, Any]], 
                         copy_trade_names: List[str]) -> List[Dict[str, Any]]:
        """
        Find trades from followed senators/representatives.
        
        Args:
            filtered_trades: Age-filtered trades
            copy_trade_names: List of normalized names to copy trade
            
        Returns:
            List of trades from followed traders
        """
        copy_trades = []
        
        for trade in filtered_trades:
            first_name = trade.get('firstName', '').lower()
            last_name = trade.get('lastName', '').lower()
            full_name = f"{first_name} {last_name}".strip()
            
            # Check if this trader matches any copy trade name (partial match)
            is_copy_trade_target = False
            matched_target = None
            for target_name in copy_trade_names:
                if target_name in full_name or target_name in first_name or target_name in last_name:
                    is_copy_trade_target = True
                    matched_target = target_name
                    break
            
            if is_copy_trade_target:
                trade['matched_target'] = matched_target
                copy_trades.append(trade)
        
        return copy_trades
    
    def _generate_recommendations(self, copy_trades: List[Dict[str, Any]],
                                 symbol: str,
                                 current_price: float,
                                 subtype=None,
                                 all_copy_trades: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        """
        Generate recommendations from copy trades.
        
        Args:
            copy_trades: List of trades from followed traders
            symbol: Stock symbol
            current_price: Current stock price
            subtype: AnalysisUseCase (ENTER_MARKET or OPEN_POSITION)
            
        Returns:
            Dictionary with recommendation details
        """
        if not copy_trades:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': 'No trades found from followed senators/representatives within age criteria',
                'trades': [],
                'trade_count': 0,
                'copy_trades_found': 0
            }
        
        # Group trades by instrument to issue only one recommendation per instrument
        instrument_trades = {}
        for trade in copy_trades:
            trade_symbol = trade.get('symbol', '').upper()
            if trade_symbol not in instrument_trades:
                instrument_trades[trade_symbol] = []
            instrument_trades[trade_symbol].append(trade)
        
        # For the requested symbol, determine the recommendation
        symbol_trades = instrument_trades.get(symbol.upper(), [])
        if not symbol_trades:
            return {
                'signal': OrderRecommendation.HOLD,
                'confidence': 0.0,
                'expected_profit_percent': 0.0,
                'details': f'No trades found for {symbol} from followed traders',
                'trades': [],
                'trade_count': 0,
                'copy_trades_found': len(copy_trades)
            }
        
        # Determine signal based on the trades
        # If multiple trades exist, analyze all of them
        # Group by signal direction
        buy_trades = [t for t in symbol_trades if 'purchase' in t.get('type', '').lower() or 'buy' in t.get('type', '').lower()]
        sell_trades = [t for t in symbol_trades if 'sale' in t.get('type', '').lower() or 'sell' in t.get('type', '').lower()]
        
        # Determine dominant signal
        if len(buy_trades) > len(sell_trades):
            signal = OrderRecommendation.BUY
            dominant_trades = buy_trades
        elif len(sell_trades) > len(buy_trades):
            signal = OrderRecommendation.SELL
            dominant_trades = sell_trades
        elif len(buy_trades) > 0:
            # Equal trades, use most recent
            most_recent_trade = max(symbol_trades, key=lambda t: t.get('exec_date', ''))
            transaction_type = most_recent_trade.get('type', '').lower()
            is_buy = 'purchase' in transaction_type or 'buy' in transaction_type
            signal = OrderRecommendation.BUY if is_buy else OrderRecommendation.SELL
            dominant_trades = symbol_trades
        else:
            signal = OrderRecommendation.HOLD
            dominant_trades = []
        
        # Collect all unique traders for this symbol with the dominant signal
        trader_names = []
        for trade in dominant_trades:
            trader_name_detail = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'
            if trader_name_detail not in trader_names:
                trader_names.append(trader_name_detail)
        
        # Primary trader is the first one (most recent)
        if dominant_trades:
            most_recent_trade = max(dominant_trades, key=lambda t: t.get('exec_date', ''))
            primary_trader = f"{most_recent_trade.get('firstName', '')} {most_recent_trade.get('lastName', '')}".strip() or 'Unknown'
        else:
            primary_trader = 'Unknown'
        
        expected_profit = 0.0
        
        # Build trade details
        trade_details = []
        for trade in symbol_trades:
            trader_name_detail = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'
            trade_info = {
                'trader': trader_name_detail,
                'type': trade.get('type', 'Unknown'),
                'amount': trade.get('amount', 'N/A'),
                'exec_date': trade.get('exec_date', 'N/A'),
                'disclose_date': trade.get('disclose_date', 'N/A'),
                'days_since_exec': trade.get('days_since_exec', 0),
                'days_since_disclose': trade.get('days_since_disclose', 0),
                'matched_target': trade.get('matched_target', 'Unknown')
            }
            trade_details.append(trade_info)
        
        # Build detailed report
        details = f"""FMP Senate/House Trading Analysis - COPY TRADE MODE

Current Price: ${current_price:.2f}

🎯 COPY TRADING ACTIVE - Following {len(copy_trades)} total trades

Symbol {symbol} Analysis:
- Trades Found: {len(symbol_trades)}
- Most Recent Signal: {signal.value}
- Primary Trader: {primary_trader}
- Number of Traders with {signal.value}: {len(trader_names)}

Trade Details for {symbol}:
"""
        
        for i, trade_info in enumerate(trade_details, 1):
            details += f"""
Trade #{i}:
- Trader: {trade_info['trader']} (matched: {trade_info['matched_target']})
- Type: {trade_info['type']}
- Amount: {trade_info['amount']}
- Execution Date: {trade_info['exec_date']} ({trade_info['days_since_exec']} days ago)
- Disclosure Date: {trade_info['disclose_date']} ({trade_info['days_since_disclose']} days ago)
"""
        
        details += f"""

Overall Signal: {signal.value}
Confidence: {confidence:.1f}% (Copy Trade - Maximum Confidence)
Expected Profit: {expected_profit:+.1f}%

Copy Trade Mode:
- This expert follows specific senators/representatives
- Generates immediate recommendations with 100% confidence
- Fixed 50% expected profit target
- Only one recommendation per instrument (most recent trade wins)

Total Copy Trades Found: {len(copy_trades)} (across all instruments)
Trades for {symbol}: {len(symbol_trades)}

Note: If multiple trades exist for the same instrument, the most recent trade determines the signal.
"""
        
        # Calculate trader money spent and percentage of yearly trading
        from ...core.utils import calculate_fmp_trade_metrics
        trade_metrics = calculate_fmp_trade_metrics(symbol_trades, all_trader_trades=all_copy_trades)

        # Confidence: 60% base + percent_of_yearly (capped at 100%)
        percent_of_yearly = trade_metrics.get('percent_of_yearly', 0.0)
        confidence = min(100.0, 60.0 + percent_of_yearly)

        return {
            'signal': signal,
            'confidence': confidence,
            'expected_profit_percent': expected_profit,
            'details': details,
            'trades': trade_details,
            'trade_count': len(symbol_trades),
            'copy_trades_found': len(copy_trades),
            'trader_name': primary_trader,  # Primary trader for this recommendation
            'trader_names': trader_names,  # All traders with same signal direction
            'num_traders': len(trader_names),  # Count of unique traders
            'trade_metrics': trade_metrics  # Financial metrics for the trades
        }
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], 
                                     symbol: str, market_analysis_id: int,
                                     current_price: Optional[float]) -> int:
        """Create ExpertRecommendation record in database."""
        try:
            # Get trade metrics
            trade_metrics = recommendation_data.get('trade_metrics', {})
            
            # Store senate copy specific data with trade metrics
            senate_copy_data = {
                'trader_names': recommendation_data.get('trader_names', []),
                'num_traders': recommendation_data.get('num_traders', 1),
                'trade_count': recommendation_data.get('trade_count', 0),
                'trades': recommendation_data.get('trades', []),
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
                risk_level=RiskLevel.MEDIUM,  # Copy trades are medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Medium term based on disclosure lag
                market_analysis_id=market_analysis_id,
                data={'SenateCopy': senate_copy_data},  # Store with "SenateCopy" key
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            self.logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence, "
                       f"based on {recommendation_data['trade_count']} copy trades from {recommendation_data.get('num_traders', 1)} trader(s), "
                       f"Total spent: ${trade_metrics.get('total_money_spent', 0.0):,.0f}, "
                       f"Percent of yearly: {trade_metrics.get('percent_of_yearly', 0.0):.1f}%")
            return recommendation_id
            
        except Exception as e:
            self.logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
            raise
    
    def _store_analysis_outputs(self, market_analysis_id: int, symbol: str,
                               recommendation_data: Dict[str, Any],
                               all_trades: List[Dict[str, Any]],
                               filtered_trades: List[Dict[str, Any]],
                               copy_trades: List[Dict[str, Any]]) -> None:
        """Store detailed analysis outputs in database."""
        session = get_db()
        
        try:
            # Store main analysis details
            details_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Copy Trade Analysis",
                type="copy_trade_analysis",
                text=recommendation_data['details']
            )
            session.add(details_output)
            
            # Store copy trade details
            if recommendation_data['trades']:
                trades_text = json.dumps(recommendation_data['trades'], indent=2)
                trades_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name="Copy Trade Details",
                    type="copy_trade_details",
                    text=trades_text
                )
                session.add(trades_output)
            
            # Store full trade data
            all_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="All Trades (Raw Data)",
                type="all_trades_raw",
                text=json.dumps(all_trades, indent=2)
            )
            session.add(all_trades_output)
            
            filtered_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Age-Filtered Trades",
                type="filtered_trades",
                text=json.dumps(filtered_trades, indent=2)
            )
            session.add(filtered_trades_output)
            
            copy_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Copy Trades Found",
                type="copy_trades",
                text=json.dumps(copy_trades, indent=2)
            )
            session.add(copy_trades_output)
            
            # Store summary statistics
            summary_text = f"""Copy Trade Summary:
- Total Trades Found: {len(all_trades)}
- Trades After Age Filter: {len(filtered_trades)}
- Copy Trades Found: {len(copy_trades)}
- Symbol Trades: {recommendation_data['trade_count']}
- Overall Signal: {recommendation_data['signal'].value}
- Confidence: {recommendation_data['confidence']:.1f}%"""
            
            summary_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Copy Trade Summary",
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
    
    def _store_multi_symbol_analysis_outputs(self, market_analysis_id: int,
                                           all_trades: List[Dict[str, Any]],
                                           filtered_trades: List[Dict[str, Any]],
                                           copy_trades: List[Dict[str, Any]],
                                           trades_by_symbol: Dict[str, List[Dict[str, Any]]],
                                           symbol_recommendations: Dict[str, Dict[str, Any]]) -> None:
        """Store comprehensive analysis outputs for multi-symbol analysis."""
        session = get_db()
        
        try:
            # Store main analysis summary
            summary_text = f"""Multi-Instrument Copy Trade Analysis Summary:

Total Trades Found: {len(all_trades)}
Trades After Age Filter: {len(filtered_trades)}
Copy Trades Found: {len(copy_trades)}
Symbols with Copy Trades: {len(trades_by_symbol)}

Symbols Analyzed: {', '.join(sorted(trades_by_symbol.keys()))}

Recommendations Generated:"""
            
            for symbol, rec_data in symbol_recommendations.items():
                summary_text += f"\n- {symbol}: {rec_data['signal']} ({rec_data['confidence']:.1f}% confidence, "
                summary_text += f"{rec_data['trade_count']} trades)"
            
            summary_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Multi-Symbol Copy Trade Summary",
                type="multi_symbol_summary",
                text=summary_text
            )
            session.add(summary_output)
            
            # Store detailed breakdown by symbol
            for symbol, symbol_trades in trades_by_symbol.items():
                symbol_details = {
                    'symbol': symbol,
                    'trade_count': len(symbol_trades),
                    'recommendation': symbol_recommendations.get(symbol, {}),
                    'trades': symbol_trades
                }
                
                symbol_output = AnalysisOutput(
                    market_analysis_id=market_analysis_id,
                    name=f"Copy Trades for {symbol}",
                    type="symbol_copy_trades",
                    text=json.dumps(symbol_details, indent=2)
                )
                session.add(symbol_output)
            
            # Store all raw trades
            all_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="All Raw Trades",
                type="all_trades_raw",
                text=json.dumps(all_trades, indent=2)
            )
            session.add(all_trades_output)
            
            # Store filtered trades
            filtered_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="Age-Filtered Trades",
                type="filtered_trades",
                text=json.dumps(filtered_trades, indent=2)
            )
            session.add(filtered_trades_output)
            
            # Store copy trades
            copy_trades_output = AnalysisOutput(
                market_analysis_id=market_analysis_id,
                name="All Copy Trades Found",
                type="copy_trades",
                text=json.dumps(copy_trades, indent=2)
            )
            session.add(copy_trades_output)
            
            session.commit()
            
        except Exception as e:
            session.rollback()
            self.logger.error(f"Failed to store multi-symbol analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run FMPSenateTraderCopy analysis across all instruments from followed traders.
        
        This expert analyzes per trader, not per symbol. It fetches all trades from 
        followed traders and creates separate ExpertRecommendation records for each 
        symbol traded by those traders.
        
        Supports different analysis use cases:
        - ENTER_MARKET: Generate BUY/SELL recommendations based on trader activity
        - OPEN_POSITIONS: Generate SELL/BUY recommendations to close existing positions
        
        Args:
            symbol: Placeholder symbol (typically "MULTI" for multi-instrument analysis)
            market_analysis: MarketAnalysis instance to update with results (includes subtype)
        """
        self.logger.info(f"Starting FMPSenateTraderCopy analysis (Analysis ID: {market_analysis.id}, "
                        f"SubType: {market_analysis.subtype.value if market_analysis.subtype else 'ENTER_MARKET'})")
        
        try:
            # Route based on analysis use case
            if market_analysis.subtype == AnalysisUseCase.OPEN_POSITIONS:
                self._run_open_positions_analysis(symbol, market_analysis)
            else:
                # Default: ENTER_MARKET analysis
                self._run_enter_market_analysis(symbol, market_analysis)
                
        except Exception as e:
            self.logger.error(f"FMPSenateTraderCopy analysis failed: {e}", exc_info=True)
            
            # Update status to failed
            market_analysis.state = {
                'error': str(e),
                'error_timestamp': datetime.now(timezone.utc).isoformat(),
                'analysis_failed': True
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            raise
    
    def _run_enter_market_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run ENTER_MARKET analysis: Generate BUY/SELL recommendations based on trader activity.
        
        This is the standard analysis mode that generates recommendations to enter positions.
        """
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Get settings
            copy_trade_names_setting = (self.get_setting_with_interface_default('copy_trade_names', log_warning=False) or '').strip()
            if not copy_trade_names_setting:
                raise ValueError("No copy trade names configured. Please set 'copy_trade_names' in expert settings.")
            
            # Parse copy trade names
            copy_trade_names = [name.strip().lower() for name in copy_trade_names_setting.split(',') if name.strip()]
            if not copy_trade_names:
                raise ValueError("No valid copy trade names found after parsing.")
            
            settings_def = self.get_settings_definitions()
            max_disclose_days = int(self.get_setting_with_interface_default('max_disclose_date_days'))
            max_exec_days = int(self.get_setting_with_interface_default('max_trade_exec_days'))
            
            # Fetch all trades (no symbol filter)
            senate_trades = self._fetch_senate_trades(symbol=None)
            house_trades = self._fetch_house_trades(symbol=None)
            
            if senate_trades is None and house_trades is None:
                raise ValueError("Failed to fetch trades from FMP API (both senate and house failed)")
            
            # Combine trades from both sources
            all_trades = []
            if senate_trades:
                all_trades.extend(senate_trades)
            if house_trades:
                all_trades.extend(house_trades)
            
            self.logger.info(f"Fetched {len(senate_trades) if senate_trades else 0} senate trades and "
                       f"{len(house_trades) if house_trades else 0} house trades for all symbols")
            
            # Filter trades by age (no symbol filter)
            filtered_trades = self._filter_trades_by_age_multi(
                all_trades, max_disclose_days, max_exec_days
            )
            
            # Find copy trades from followed traders
            copy_trades = self._find_copy_trades(filtered_trades, copy_trade_names)
            
            # Group copy trades by symbol
            trades_by_symbol = {}
            for trade in copy_trades:
                trade_symbol = trade.get('symbol', '').upper().strip()
                if trade_symbol:
                    if trade_symbol not in trades_by_symbol:
                        trades_by_symbol[trade_symbol] = []
                    trades_by_symbol[trade_symbol].append(trade)
            
            self.logger.info(f"Found copy trades for {len(trades_by_symbol)} symbols: {sorted(trades_by_symbol.keys())}")
            
            # Filter out symbols not supported by the broker
            from ...core.utils import get_account_instance_from_id
            expert_instance = get_instance(ExpertInstance, self.id)
            if expert_instance:
                account = get_account_instance_from_id(expert_instance.account_id)
                if account:
                    supported_symbols = account.filter_supported_symbols(
                        list(trades_by_symbol.keys()), 
                        log_prefix=f"FMPSenateTraderCopy-{self.id}"
                    )
                    # Keep only supported symbols in trades_by_symbol
                    trades_by_symbol = {s: trades_by_symbol[s] for s in supported_symbols if s in trades_by_symbol}
                    self.logger.info(f"After filtering: {len(trades_by_symbol)} supported symbols")
            
            # Create recommendations for each symbol
            recommendation_ids = []
            symbol_recommendations = {}
            
            for trade_symbol, symbol_trades in trades_by_symbol.items():
                try:
                    # Get current price for this symbol
                    current_price = self._get_current_price(trade_symbol)
                    if not current_price:
                        self.logger.warning(f"Unable to get current price for {trade_symbol}, using 0.0")
                        current_price = 0.0
                    
                    # Generate recommendations for this symbol
                    recommendation_data = self._generate_recommendations(
                        symbol_trades, trade_symbol, current_price, subtype=AnalysisUseCase.ENTER_MARKET,
                        all_copy_trades=copy_trades
                    )
                    
                    # Create ExpertRecommendation record for this symbol
                    recommendation_id = self._create_expert_recommendation(
                        recommendation_data, trade_symbol, market_analysis.id, current_price
                    )
                    
                    recommendation_ids.append(recommendation_id)
                    symbol_recommendations[trade_symbol] = {
                        'recommendation_id': recommendation_id,
                        'signal': recommendation_data['signal'].value,
                        'confidence': recommendation_data['confidence'],
                        'expected_profit_percent': recommendation_data['expected_profit_percent'],
                        'current_price': current_price,
                        'trade_count': len(symbol_trades),
                        'trader_name': recommendation_data.get('trader_name', 'Unknown'),  # Include trader name
                        # Add financial metrics from trade metrics
                        'money_spent': recommendation_data.get('trade_metrics', {}).get('total_money_spent', 0.0),
                        'percent_of_yearly': recommendation_data.get('trade_metrics', {}).get('percent_of_yearly', 0.0)
                    }
                    
                    self.logger.info(f"Created recommendation for {trade_symbol}: {recommendation_data['signal'].value} "
                               f"({recommendation_data['confidence']:.1f}% confidence)")
                    
                except Exception as e:
                    self.logger.error(f"Error creating recommendation for {trade_symbol}: {e}", exc_info=True)
                    # Continue with other symbols
            
            # Store comprehensive analysis outputs
            self._store_multi_symbol_analysis_outputs(
                market_analysis.id, all_trades, filtered_trades, copy_trades, 
                trades_by_symbol, symbol_recommendations
            )
            
            # Store analysis state with multi-symbol information
            # Include trader names per symbol for UI display
            traders_by_symbol = {}
            for trade_symbol, recs in symbol_recommendations.items():
                if recs and 'trader_name' in recs:
                    traders_by_symbol[trade_symbol] = recs['trader_name']
            
            market_analysis.state = {
                'copy_trade_multi': {
                    'analysis_type': 'multi_instrument',
                    'total_symbols': len(trades_by_symbol),
                    'symbols_analyzed': sorted(trades_by_symbol.keys()),
                    'symbol_recommendations': symbol_recommendations,
                    'traders_by_symbol': traders_by_symbol,  # Store trader names for UI
                    'trade_statistics': {
                        'total_trades': len(all_trades),
                        'filtered_trades': len(filtered_trades),
                        'copy_trades_found': len(copy_trades),
                        'symbols_with_trades': len(trades_by_symbol)
                    },
                    'settings': {
                        'copy_trade_names': copy_trade_names_setting,
                        'max_disclose_date_days': max_disclose_days,
                        'max_trade_exec_days': max_exec_days
                    },
                    'expert_recommendation_ids': recommendation_ids,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat()
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            self.logger.info(f"Completed FMPSenateTraderCopy analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%, "
                       f"{recommendation_data['trade_count']} relevant trades found)")
            
        except Exception as e:
            self.logger.error(f"FMPSenateTraderCopy analysis failed for {symbol}: {e}", exc_info=True)
            
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
                    text=f"FMPSenateTraderCopy analysis failed for {symbol}: {str(e)}"
                )
                session.add(error_output)
                session.commit()
                session.close()
            except Exception as db_error:
                self.logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
    def _run_open_positions_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run OPEN_POSITIONS analysis: Generate SELL/BUY recommendations to close existing positions.
        
        Logic:
        - If "close_only_for_same_trader" is True (default):
          - Only generate SELL recommendations if a trader who previously BUY'ed is now SELL'ing (same trader reversal)
          - Only generate BUY recommendations if a trader who previously SELL'ed is now BUY'ing (same trader reversal)
          - If we have existing positions with no trader name (backward compatibility), fall back to averaging logic
        - If "close_only_for_same_trader" is False:
          - Use standard averaging logic from lookback interval (compare BUY vs SELL counts)
        """
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Get settings
            copy_trade_names_setting = (self.get_setting_with_interface_default('copy_trade_names', log_warning=False) or '').strip()
            if not copy_trade_names_setting:
                raise ValueError("No copy trade names configured. Please set 'copy_trade_names' in expert settings.")
            
            close_only_for_same_trader = bool(self.get_setting_with_interface_default('close_only_for_same_trader'))
            
            # Parse copy trade names
            copy_trade_names = [name.strip().lower() for name in copy_trade_names_setting.split(',') if name.strip()]
            if not copy_trade_names:
                raise ValueError("No valid copy trade names found after parsing.")
            
            settings_def = self.get_settings_definitions()
            max_disclose_days = int(self.get_setting_with_interface_default('max_disclose_date_days'))
            max_exec_days = int(self.get_setting_with_interface_default('max_trade_exec_days'))
            
            # OPEN_POSITIONS: Fetch trades only for the specific symbol being analyzed
            # This ensures we only analyze the position we're holding
            senate_trades = self._fetch_senate_trades(symbol=symbol)
            house_trades = self._fetch_house_trades(symbol=symbol)
            
            if senate_trades is None and house_trades is None:
                raise ValueError(f"Failed to fetch trades for {symbol} from FMP API (both senate and house failed)")
            
            # Combine trades
            all_trades = (senate_trades or []) + (house_trades or [])
            self.logger.info(f"Fetched {len(all_trades)} trades for {symbol} from senate and house")
            
            # Filter trades by settings
            filtered_trades = self._filter_trades(all_trades, copy_trade_names, max_disclose_days, max_exec_days)
            self.logger.info(f"Filtered to {len(filtered_trades)} trades for {symbol} within age criteria")
            
            # For OPEN_POSITIONS, we only care about the symbol being analyzed
            # Filter trades to only include the specific symbol (in case API returned multiple)
            symbol_trades = [t for t in filtered_trades if t.get('symbol', '').upper() == symbol.upper()]
            
            if not symbol_trades:
                self.logger.info(f"No recent trades found for {symbol} by tracked traders - returning HOLD")
                # No trades means no action needed - position can stay open
                # Create HOLD recommendation
                current_price = self._get_current_price(symbol)
                if not current_price:
                    self.logger.warning(f"Unable to get current price for {symbol}, using 0.0")
                    current_price = 0.0
                
                hold_recommendation = ExpertRecommendation(
                    instance_id=self.instance.id,
                    market_analysis_id=market_analysis.id,
                    symbol=symbol,
                    recommended_action=OrderRecommendation.HOLD,
                    expected_profit_percent=0.0,
                    price_at_date=current_price,
                    details=f"No recent trades by tracked traders for {symbol}",
                    confidence=0.0,
                    risk_level=RiskLevel.LOW,
                    time_horizon=TimeHorizon.SHORT_TERM,
                    data={}
                )
                recommendation_id = add_instance(hold_recommendation)
                
                # Store minimal state
                market_analysis.state = {
                    'copy_trade_multi': {
                        'analysis_type': 'open_positions_close',
                        'symbol': symbol,
                        'result': 'hold',
                        'reason': 'no_recent_trades',
                        'trade_statistics': {
                            'total_trades': len(all_trades),
                            'filtered_trades': len(filtered_trades),
                            'symbol_trades': 0
                        }
                    }
                }
                market_analysis.status = MarketAnalysisStatus.COMPLETED
                update_instance(market_analysis)
                return
            
            self.logger.info(f"Found {len(symbol_trades)} trades for {symbol} by tracked traders")
            
            # Determine the effective mode based on whether orders have trader names
            effective_close_only_for_same_trader = close_only_for_same_trader
            use_fallback_mode = False
            
            if close_only_for_same_trader:
                # Check if we have ANY open positions (with or without trader names)
                # This method returns (has_any_orders, has_orders_with_trader_names)
                has_any_orders, has_orders_with_trader_names = self._check_open_positions_trader_status(symbol)
                
                if has_any_orders and not has_orders_with_trader_names:
                    # Mixed state: We have orders but none with trader names
                    # Fall back to averaging logic for backward compatibility
                    self.logger.debug(f"Found open positions for {symbol} but none have trader names, "
                                    f"falling back to averaging logic (backward compatibility)")
                    effective_close_only_for_same_trader = False
                    use_fallback_mode = True
                elif not has_any_orders:
                    # No orders exist, can use same_trader mode with current trades
                    self.logger.debug(f"No open positions for {symbol}, using same_trader mode with current Senate/House trades")
                # else: has_orders_with_trader_names is True, continue with same_trader mode
            
            # Generate close recommendations based on effective mode
            if effective_close_only_for_same_trader:
                # NEW MODE: Check for same trader reversals
                recommendation_data = self._generate_close_recommendations_same_trader(
                    symbol_trades, symbol
                )
            else:
                # OLD MODE: Use averaging logic (or fallback mode)
                recommendation_data = self._generate_close_recommendations_averaging(
                    symbol_trades, symbol
                )
            
            if not recommendation_data:
                # No recommendation in this mode - return HOLD
                mode_name = 'same_trader' if effective_close_only_for_same_trader else 'averaging'
                if use_fallback_mode:
                    mode_name = 'averaging (fallback - no trader names)'
                self.logger.info(f"No close recommendation for {symbol} in {mode_name} mode - returning HOLD")
                
                current_price = self._get_current_price(symbol)
                if not current_price:
                    self.logger.warning(f"Unable to get current price for {symbol}, using 0.0")
                    current_price = 0.0
                
                hold_recommendation = ExpertRecommendation(
                    instance_id=self.instance.id,
                    market_analysis_id=market_analysis.id,
                    symbol=symbol,
                    recommended_action=OrderRecommendation.HOLD,
                    expected_profit_percent=0.0,
                    price_at_date=current_price,
                    details=f"No actionable signal for {symbol} in {mode_name} mode",
                    confidence=0.0,
                    risk_level=RiskLevel.LOW,
                    time_horizon=TimeHorizon.SHORT_TERM,
                    data={}
                )
                recommendation_id = add_instance(hold_recommendation)
                
                # Store state
                market_analysis.state = {
                    'copy_trade_multi': {
                        'analysis_type': 'open_positions_close',
                        'symbol': symbol,
                        'close_mode': mode_name,
                        'result': 'hold',
                        'reason': 'no_actionable_signal',
                        'trade_statistics': {
                            'total_trades': len(all_trades),
                            'filtered_trades': len(filtered_trades),
                            'symbol_trades': len(symbol_trades)
                        }
                    }
                }
                market_analysis.status = MarketAnalysisStatus.COMPLETED
                update_instance(market_analysis)
                return
            
            # Get current price
            current_price = self._get_current_price(symbol)
            if not current_price:
                self.logger.warning(f"Unable to get current price for {symbol}, using 0.0")
                current_price = 0.0
            
            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id, current_price
            )
            
            self.logger.info(f"Created close recommendation for {symbol}: {recommendation_data['signal'].value}")
            
            # Store analysis state
            mode_name = 'same_trader' if effective_close_only_for_same_trader else 'averaging'
            if use_fallback_mode:
                mode_name = 'averaging (fallback - no trader names)'
            
            market_analysis.state = {
                'copy_trade_multi': {
                    'analysis_type': 'open_positions_close',
                    'symbol': symbol,
                    'close_mode': mode_name,
                    'recommendation_id': recommendation_id,
                    'signal': recommendation_data['signal'].value,
                    'confidence': recommendation_data['confidence'],
                    'expected_profit_percent': recommendation_data['expected_profit_percent'],
                    'current_price': current_price,
                    'trade_count': len(symbol_trades),
                    'trader_name': recommendation_data.get('trader_name', 'Unknown'),
                    'trader_names': recommendation_data.get('trader_names', []),
                    'num_traders': recommendation_data.get('num_traders', 1),
                    'trade_statistics': {
                        'total_trades': len(all_trades),
                        'filtered_trades': len(filtered_trades),
                        'symbol_trades': len(symbol_trades)
                    },
                    'settings': {
                        'copy_trade_names': copy_trade_names_setting,
                        'close_only_for_same_trader': close_only_for_same_trader,
                        'max_disclose_date_days': max_disclose_days,
                        'max_trade_exec_days': max_exec_days
                    },
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat()
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            self.logger.info(f"Completed FMPSenateTraderCopy OPEN_POSITIONS analysis for {symbol}: "
                           f"{recommendation_data['signal'].value} recommendation created")
            
        except Exception as e:
            self.logger.error(f"FMPSenateTraderCopy OPEN_POSITIONS analysis failed: {e}", exc_info=True)
            
            # Update status to failed
            market_analysis.state = {
                'error': str(e),
                'error_timestamp': datetime.now(timezone.utc).isoformat(),
                'analysis_failed': True
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            raise
    
    def _generate_close_recommendations_same_trader(self, symbol_trades: List[Dict[str, Any]], 
                                                    symbol: str) -> Optional[Dict[str, Any]]:
        """
        Generate close recommendations only if same trader reverses direction.
        
        Returns None if no valid reversal found.
        """
        # TODO: Implement same-trader reversal detection
        # This requires checking historical positions and matching trader names
        # For now, return None to skip recommendations in this mode
        return None
    
    def _generate_close_recommendations_averaging(self, symbol_trades: List[Dict[str, Any]], 
                                                 symbol: str) -> Optional[Dict[str, Any]]:
        """
        Generate close recommendations using standard averaging logic.
        
        Compare BUY vs SELL counts in the lookback period to generate recommendations.
        """
        # TODO: Implement averaging logic
        # Count BUY vs SELL trades and generate opposite signal if one dominates
        return None
    
    def render_market_analysis(self, market_analysis: MarketAnalysis) -> None:
        """
        Render FMPSenateTraderCopy market analysis results in the UI.
        
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
            ui.label('Copy Trade Analysis Pending').classes('text-h5')
            ui.label(f'Searching for followed trader activity on {market_analysis.symbol}...').classes('text-grey-7')
    
    def _render_running(self, market_analysis: MarketAnalysis) -> None:
        """Render running analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-8 text-center'):
            ui.spinner(size='3rem', color='primary').classes('mb-4')
            ui.label('Copy Trade Analysis Running').classes('text-h5')
            ui.label(f'Fetching trades from followed senators/representatives for {market_analysis.symbol}...').classes('text-grey-7')
    
    def _render_failed(self, market_analysis: MarketAnalysis) -> None:
        """Render failed analysis state."""
        from nicegui import ui
        
        with ui.card().classes('w-full p-4'):
            with ui.row().classes('items-center mb-4'):
                ui.icon('error', color='negative', size='2rem')
                ui.label('Copy Trade Analysis Failed').classes('text-h5 text-negative ml-2')
            
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
        """Render completed multi-symbol analysis with detailed UI."""
        from nicegui import ui
        
        # Check if this is the new multi-symbol analysis
        if (market_analysis.state and 'copy_trade_multi' in market_analysis.state):
            self._render_multi_symbol_completed(market_analysis)
            return
        
        # Legacy single-symbol analysis fallback
        if not market_analysis.state or 'copy_trade' not in market_analysis.state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        
        self._render_single_symbol_completed(market_analysis)
    
    def _render_multi_symbol_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed multi-symbol analysis."""
        from nicegui import ui
        
        state = market_analysis.state['copy_trade_multi']
        symbol_recommendations = state.get('symbol_recommendations', {})
        stats = state.get('trade_statistics', {})
        settings = state.get('settings', {})
        symbols_analyzed = state.get('symbols_analyzed', [])
        
        # Main card
        with ui.card().classes('w-full').style('background-color: #1e2a3a'):
            # Header
            with ui.card_section().style('background: linear-gradient(135deg, #00d4aa 0%, #00b894 100%)'):
                ui.label('Multi-Instrument Copy Trading Analysis').classes('text-h5 text-weight-bold').style('color: white')
                ui.label(f'Following Specific Traders Across {len(symbols_analyzed)} Instruments').style('color: rgba(255,255,255,0.8)')
            
            # Overall Statistics
            total_trades = stats.get('total_trades', 0)
            filtered_trades = stats.get('filtered_trades', 0)
            copy_trades_found = stats.get('copy_trades_found', 0)
            symbols_with_trades = stats.get('symbols_with_trades', 0)
            
            with ui.card_section().style('background-color: #141c28'):
                ui.label('Copy Trade Activity Summary').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                
                with ui.grid(columns=2).classes('w-full gap-4'):
                    # Total Trades
                    with ui.card().style('background-color: rgba(66, 153, 225, 0.15)'):
                        ui.label('Total Trades Found').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(total_trades)).classes('text-h5').style('color: #63b3ed')
                        ui.label(f'{filtered_trades} after age filter').classes('text-xs').style('color: #4299e1')
                    
                    # Copy Trades
                    with ui.card().style('background-color: rgba(255, 169, 77, 0.15)'):
                        ui.label('Copy Trades Found').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(copy_trades_found)).classes('text-h5').style('color: #ffa94d')
                        ui.label('from followed traders').classes('text-xs').style('color: #ed8936')
                    
                    # Symbols
                    with ui.card().style('background-color: rgba(159, 122, 234, 0.15)'):
                        ui.label('Instruments Analyzed').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(symbols_with_trades)).classes('text-h5').style('color: #9f7aea')
                        ui.label('with copy trades').classes('text-xs').style('color: #b794f4')
                    
                    # Status
                    status_color = '#00d4aa' if copy_trades_found > 0 else '#a0aec0'
                    status_bg = 'rgba(0, 212, 170, 0.15)' if copy_trades_found > 0 else 'rgba(160, 174, 192, 0.15)'
                    with ui.card().style(f'background-color: {status_bg}'):
                        ui.label('Copy Trade Status').classes('text-caption').style('color: #a0aec0')
                        status_text = 'ACTIVE' if copy_trades_found > 0 else 'NO MATCHES'
                        ui.label(status_text).classes('text-h5').style(f'color: {status_color}')
                        ui.label('multi-instrument mode').classes('text-xs').style(f'color: {status_color}')
            
            # Recommendations by Symbol
            if symbol_recommendations:
                with ui.card_section():
                    # Get trader names from state
                    traders_by_symbol = state.get('traders_by_symbol', {})
                    
                    ui.label(f'Recommendations Generated ({len(symbol_recommendations)})').classes('text-subtitle1 text-weight-medium mb-4').style('color: #e2e8f0')
                    
                    # Display recommendations in a grid (3 per row - balanced size)
                    with ui.grid(columns=3).classes('w-full gap-4'):
                        for symbol, rec_data in sorted(symbol_recommendations.items()):
                            signal = rec_data.get('signal', 'HOLD')
                            confidence = rec_data.get('confidence', 0.0)
                            expected_profit = rec_data.get('expected_profit_percent', 0.0)
                            current_price = rec_data.get('current_price', 0.0)
                            trade_count = rec_data.get('trade_count', 0)
                            trader_name = rec_data.get('trader_name', traders_by_symbol.get(symbol, 'Unknown'))
                            
                            # Signal colors
                            if signal == 'BUY':
                                signal_color = 'positive'
                                signal_icon = 'trending_up'
                                bg_color = 'rgba(0, 212, 170, 0.1)'
                            elif signal == 'SELL':
                                signal_color = 'negative'
                                signal_icon = 'trending_down'
                                bg_color = 'rgba(255, 107, 107, 0.1)'
                            else:
                                signal_color = 'grey'
                                signal_icon = 'trending_flat'
                                bg_color = 'rgba(160, 174, 192, 0.1)'
                            
                            with ui.card().classes('w-full shadow-sm').style(f'background-color: {bg_color}'):
                                with ui.column().classes('w-full gap-3 p-4'):
                                    # Header: Symbol and Signal
                                    with ui.row().classes('w-full items-center justify-between mb-2'):
                                        ui.label(symbol).classes('text-h5 text-weight-bold').style('color: #e2e8f0')
                                        ui.icon(signal_icon, color=signal_color, size='1.8rem')
                                    
                                    # Signal and Trader name(s)
                                    num_traders = rec_data.get('num_traders', 1)
                                    trader_names_list = rec_data.get('trader_names', [trader_name])
                                    
                                    with ui.column().classes('w-full mb-3'):
                                        with ui.row().classes('w-full items-center gap-2'):
                                            ui.label(signal).classes(f'text-h6 text-weight-bold text-{signal_color}')
                                            if num_traders > 1:
                                                ui.badge(str(num_traders), color='blue').classes('text-xs')
                                        
                                        # Show trader names
                                        if num_traders == 1:
                                            ui.label(f'by {trader_name}').classes('text-sm italic mt-1').style('color: #a0aec0')
                                        else:
                                            # Multiple traders
                                            traders_text = ', '.join(trader_names_list[:3])
                                            if len(trader_names_list) > 3:
                                                traders_text += f' +{len(trader_names_list) - 3}'
                                            ui.label(f'by {traders_text}').classes('text-sm italic mt-1').style('color: #a0aec0')
                                    
                                    # Trade count
                                    with ui.row().classes('items-center gap-2 mb-3 pb-3').style('border-bottom: 1px solid #2d3748'):
                                        ui.icon('receipt', size='sm', color='orange')
                                        ui.label(f'{trade_count} Trade{"s" if trade_count != 1 else ""}').classes('text-sm text-weight-medium').style('color: #ffa94d')
                                    
                                    # Get financial metrics
                                    money_spent = rec_data.get('money_spent', 0.0)
                                    percent_yearly = rec_data.get('percent_of_yearly', 0.0)
                                    
                                    # Stats grid - extended to 2 rows
                                    with ui.column().classes('w-full gap-3'):
                                        # Row 1: Confidence, Expected Profit, Current Price
                                        with ui.grid(columns=3).classes('w-full gap-3'):
                                            # Confidence
                                            with ui.column().classes('text-center'):
                                                ui.label(f'{confidence:.0f}%').classes('text-h6 text-weight-bold').style('color: #63b3ed')
                                                ui.label('Confidence').classes('text-xs').style('color: #a0aec0')
                                            
                                            # Expected Profit
                                            with ui.column().classes('text-center'):
                                                profit_color = 'positive' if expected_profit > 0 else 'negative' if expected_profit < 0 else 'grey'
                                                ui.label(f'{expected_profit:+.1f}%').classes(f'text-h6 text-weight-bold text-{profit_color}')
                                                ui.label('Expected').classes('text-xs').style('color: #a0aec0')
                                            
                                            # Current Price
                                            with ui.column().classes('text-center'):
                                                price_label = f'${current_price:.2f}' if current_price > 0 else 'N/A'
                                                ui.label(price_label).classes('text-h6 text-weight-bold').style('color: #e2e8f0')
                                                ui.label('Price').classes('text-xs').style('color: #a0aec0')
                                        
                                        # Row 2: Money Spent, Percent of Yearly
                                        with ui.grid(columns=2).classes('w-full gap-3 mt-2 pt-2').style('border-top: 1px solid #2d3748'):
                                            # Money Spent
                                            with ui.column().classes('text-center'):
                                                money_label = f'${money_spent:,.0f}' if money_spent > 0 else '$0'
                                                ui.label(money_label).classes('text-h6 text-weight-bold').style('color: #ffa94d')
                                                ui.label('Money Spent').classes('text-xs').style('color: #a0aec0')
                                            
                                            # Percent of Yearly Trading
                                            with ui.column().classes('text-center'):
                                                ui.label(f'{percent_yearly:.1f}%').classes('text-h6 text-weight-bold').style('color: #9f7aea')
                                                ui.label('of Yearly').classes('text-xs').style('color: #a0aec0')
            
            # Followed Traders
            copy_trade_names = settings.get('copy_trade_names', '')
            if copy_trade_names:
                with ui.card_section():
                    ui.label('Followed Traders').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                    ui.label(copy_trade_names).classes('text-sm rounded p-2').style('color: #a0aec0; background-color: #141c28')
            
            # Settings
            max_disclose = settings.get('max_disclose_date_days', 30)
            max_exec = settings.get('max_trade_exec_days', 60)
            
            with ui.card_section():
                ui.label('Filter Settings').classes('text-subtitle1 text-weight-medium mb-2').style('color: #e2e8f0')
                
                with ui.row().classes('gap-4'):
                    ui.label(f'Max Disclose Age: {max_disclose} days').classes('text-sm').style('color: #a0aec0')
                    ui.label(f'Max Exec Age: {max_exec} days').classes('text-sm').style('color: #a0aec0')
            
            # Methodology
            with ui.expansion('Multi-Instrument Copy Trading Methodology', icon='info').classes('w-full').style('color: #e2e8f0'):
                with ui.card_section().style('background-color: #141c28'):
                    ui.markdown(f'''
**Multi-Instrument Copy Trading Logic:**

1. **Multi-Symbol Analysis**: Analyzes all trades from followed traders across all instruments
2. **Per-Symbol Recommendations**: Creates separate recommendations for each instrument traded
3. **Target Selection**: Follow specific senators/representatives by name
4. **Age Filtering**: Only consider trades within configured age limits
5. **Signal Generation**: 
   - **BUY** if followed trader bought the instrument
   - **SELL** if followed trader sold the instrument
   - **HOLD** if no relevant trades found
6. **Confidence**: Always **100%** for copy trades
7. **Expected Profit**: Always **50%** (fixed target)

**Multi-Instrument Benefits:**
- **Comprehensive Coverage**: Captures all trading activity from followed traders
- **No Symbol Bias**: Doesn't require pre-selecting instruments to analyze
- **Efficient Processing**: Single API call fetches all trades, multiple recommendations generated
- **Real-time Discovery**: Automatically finds new instruments being traded

**Age Filtering:**
- Trades disclosed more than **{max_disclose}** days ago are ignored
- Trades executed more than **{max_exec}** days ago are ignored

**Followed Traders:**
```
{copy_trade_names}
```

**Expert Properties:**
- **can_recommend_instruments**: True (selects instruments based on trader activity)
- **should_expand_instrument_jobs**: False (prevents job duplication)

**Note**: This expert operates in multi-instrument mode, analyzing per trader rather than per symbol. Each qualifying trade generates a separate high-confidence recommendation for that specific instrument.
                    ''').classes('text-sm')
    
    def _render_single_symbol_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed single-symbol analysis (legacy)."""
        from nicegui import ui
        
        state = market_analysis.state['copy_trade']
        rec = state.get('recommendation', {})
        stats = state.get('trade_statistics', {})
        trades = state.get('trades', [])
        settings = state.get('settings', {})
        current_price = state.get('current_price')
        
        # Main card
        with ui.card().classes('w-full').style('background-color: #1e2a3a'):
            # Header
            with ui.card_section().style('background: linear-gradient(135deg, #00d4aa 0%, #00b894 100%)'):
                ui.label('Senate/House Copy Trading Analysis').classes('text-h5 text-weight-bold').style('color: white')
                ui.label(f'{market_analysis.symbol} - Following Specific Traders').style('color: rgba(255,255,255,0.8)')
            
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
                        ui.label('Copy Trade Signal').classes('text-caption').style('color: #a0aec0')
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
                
                # Add price and financial metrics
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').style('color: #a0aec0')
                
                # Add money spent and percent of yearly if available
                if rec.get('money_spent') or rec.get('percent_of_yearly'):
                    with ui.row().classes('gap-4 mt-2'):
                        money_spent = rec.get('money_spent', 0.0)
                        if money_spent > 0:
                            ui.label(f'💰 Money Spent: ${money_spent:,.0f}').classes('text-sm text-weight-medium').style('color: #ffa94d')
                        
                        percent_yearly = rec.get('percent_of_yearly', 0.0)
                        if percent_yearly > 0:
                            ui.label(f'📊 {percent_yearly:.1f}% of Yearly Trading').classes('text-sm text-weight-medium').style('color: #9f7aea')
            
            # Trade Statistics
            total_trades = stats.get('total_trades', 0)
            filtered_trades = stats.get('filtered_trades', 0)
            copy_trades_found = stats.get('copy_trades_found', 0)
            symbol_trades = stats.get('symbol_trades', 0)
            
            with ui.card_section().style('background-color: #141c28'):
                ui.label('Copy Trade Activity Summary').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                
                with ui.grid(columns=2).classes('w-full gap-4'):
                    # Total Trades
                    with ui.card().style('background-color: rgba(66, 153, 225, 0.15)'):
                        ui.label('Total Trades Found').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(total_trades)).classes('text-h5').style('color: #63b3ed')
                        ui.label(f'{filtered_trades} after age filter').classes('text-xs').style('color: #4299e1')
                    
                    # Copy Trades
                    with ui.card().style('background-color: rgba(255, 169, 77, 0.15)'):
                        ui.label('Copy Trades Found').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(copy_trades_found)).classes('text-h5').style('color: #ffa94d')
                        ui.label('from followed traders').classes('text-xs').style('color: #ed8936')
                    
                    # Symbol Trades
                    with ui.card().style('background-color: rgba(159, 122, 234, 0.15)'):
                        ui.label(f'{market_analysis.symbol} Trades').classes('text-caption').style('color: #a0aec0')
                        ui.label(str(symbol_trades)).classes('text-h5').style('color: #9f7aea')
                        ui.label('relevant to this symbol').classes('text-xs').style('color: #b794f4')
                    
                    # Status
                    status_color = '#00d4aa' if copy_trades_found > 0 else '#a0aec0'
                    status_bg = 'rgba(0, 212, 170, 0.15)' if copy_trades_found > 0 else 'rgba(160, 174, 192, 0.15)'
                    with ui.card().style(f'background-color: {status_bg}'):
                        ui.label('Copy Trade Status').classes('text-caption').style('color: #a0aec0')
                        status_text = 'ACTIVE' if copy_trades_found > 0 else 'NO MATCHES'
                        ui.label(status_text).classes('text-h5').style(f'color: {status_color}')
                        ui.label('following traders').classes('text-xs').style(f'color: {status_color}')
            
            # Individual Trades
            if trades:
                with ui.card_section():
                    ui.label(f'Copy Trades for {market_analysis.symbol} ({len(trades)})').classes('text-subtitle1 text-weight-medium mb-3').style('color: #e2e8f0')
                    
                    for i, trade in enumerate(trades, 1):
                        trade_type = trade.get('type', 'Unknown')
                        is_buy = 'purchase' in trade_type.lower() or 'buy' in trade_type.lower()
                        
                        bg_color = 'rgba(0, 212, 170, 0.1)' if is_buy else 'rgba(255, 107, 107, 0.1)'
                        with ui.card().classes('w-full').style(f'background-color: {bg_color}'):
                            with ui.row().classes('w-full items-start justify-between'):
                                with ui.column().classes('flex-grow'):
                                    ui.label(f'Trade #{i}: {trade.get("trader", "Unknown")}').classes('text-weight-medium').style('color: #e2e8f0')
                                    ui.label(f'{trade_type} - {trade.get("amount", "N/A")}').classes('text-sm').style('color: #a0aec0')
                                    ui.label(f'Matched: {trade.get("matched_target", "Unknown")}').classes('text-sm').style('color: #ffa94d')
                                    
                                    with ui.row().classes('gap-4 mt-2 text-xs').style('color: #a0aec0'):
                                        ui.label(f'Exec: {trade.get("exec_date", "N/A")} ({trade.get("days_since_exec", 0)}d ago)')
                                        ui.label(f'Disclosed: {trade.get("disclose_date", "N/A")} ({trade.get("days_since_disclose", 0)}d ago)')
                                
                                with ui.column().classes('text-right'):
                                    ui.label('100% Confidence').classes('text-sm text-weight-medium text-positive')
                                    ui.label('Copy Trade').classes('text-xs').style('color: #ffa94d')
