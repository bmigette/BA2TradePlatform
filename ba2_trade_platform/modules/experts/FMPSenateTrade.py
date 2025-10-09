"""
FMP Senate/House Trade Expert

Expert that analyzes government official trading activity using FMP's
Senate Trading API to generate trading recommendations based on:
- Recent trades by senators and representatives
- Historical performance of those traders
- Size of investment (confidence boost for larger trades)

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
from ...logger import logger
from ...config import get_app_setting


class FMPSenateTrade(MarketExpertInterface):
    """
    FMPSenateTrade Expert Implementation
    
    Expert that uses FMP's Senate/House trading data to generate recommendations.
    Analyzes government official trades for a symbol, evaluates trader performance
    history, and calculates confidence based on:
    1. Historical performance (growth) of the same person's previous trades
    2. Total money invested (larger trades = higher confidence)
    """
    
    @classmethod
    def description(cls) -> str:
        return "Government official trading activity analysis using FMP Senate Trading API"
    
    def __init__(self, id: int):
        """Initialize FMPSenateTrade expert with database instance."""
        super().__init__(id)
        
        self._load_expert_instance(id)
        self._api_key = self._get_fmp_api_key()
    
    def _load_expert_instance(self, id: int) -> None:
        """Load and validate expert instance from database."""
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with ID {id} not found")
    
    def _get_fmp_api_key(self) -> Optional[str]:
        """Get FMP API key from app settings."""
        api_key = get_app_setting('FMP_API_KEY')
        if not api_key:
            logger.warning("FMP API key not found in app settings")
        return api_key
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """Define configurable settings for FMPSenateTrade expert."""
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
            }
        }
    
    def _fetch_senate_trades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch senate/house trades from FMP API for a specific symbol.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            List of trade records or None if error
        """
        if not self._api_key:
            logger.error("Cannot fetch senate trades: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/api/v4/senate-trading"
            params = {
                "symbol": symbol.upper(),
                "apikey": self._api_key
            }
            
            logger.debug(f"Fetching FMP senate trades for {symbol}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.debug(f"Received {len(data) if isinstance(data, list) else 0} senate trade records for {symbol}")
            
            return data if isinstance(data, list) else []
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch FMP senate trades for {symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching senate trades for {symbol}: {e}", exc_info=True)
            return None
    
    def _fetch_trader_history(self, trader_name: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch all previous trades by a specific senator/representative.
        
        Args:
            trader_name: Name of the government official
            
        Returns:
            List of all trades by this person or None if error
        """
        if not self._api_key:
            logger.error("Cannot fetch trader history: FMP API key not configured")
            return None
        
        try:
            # FMP API doesn't have name-based search, so we need to fetch recent trades
            # and filter by name (not ideal but works for the use case)
            url = f"https://financialmodelingprep.com/api/v4/senate-trading"
            params = {
                "apikey": self._api_key
            }
            
            logger.debug(f"Fetching trade history for {trader_name}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if not isinstance(data, list):
                return []
            
            # Filter for this specific trader
            trader_history = [
                trade for trade in data 
                if trade.get('representative', '').lower() == trader_name.lower()
            ]
            
            logger.debug(f"Found {len(trader_history)} previous trades by {trader_name}")
            return trader_history
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch trader history for {trader_name}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching trader history for {trader_name}: {e}", exc_info=True)
            return None
    
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
        
        try:
            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol.upper()}"
            params = {
                "from": date.strftime("%Y-%m-%d"),
                "to": date.strftime("%Y-%m-%d"),
                "apikey": self._api_key
            }
            
            logger.debug(f"Fetching price for {symbol} on {date.date()}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # Extract historical data
            historical = data.get('historical', [])
            if historical and len(historical) > 0:
                price = historical[0].get('open')
                logger.debug(f"Got price ${price} for {symbol} on {date.date()}")
                return price
            
            logger.warning(f"No price data found for {symbol} on {date.date()}")
            return None
            
        except Exception as e:
            logger.error(f"Error fetching price for {symbol} on {date.date()}: {e}", exc_info=True)
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
            logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None
    
    def _filter_trades(self, trades: List[Dict[str, Any]], 
                      max_disclose_days: int, 
                      max_exec_days: int,
                      max_price_delta_pct: float,
                      current_price: float) -> List[Dict[str, Any]]:
        """
        Filter trades based on configured settings.
        
        Args:
            trades: List of trade records
            max_disclose_days: Maximum days since disclosure
            max_exec_days: Maximum days since execution
            max_price_delta_pct: Maximum price change percentage
            current_price: Current stock price
            
        Returns:
            Filtered list of trades
        """
        now = datetime.now(timezone.utc)
        filtered_trades = []
        
        for trade in trades:
            # Parse dates
            try:
                disclose_date_str = trade.get('disclosureDate', '')
                exec_date_str = trade.get('transactionDate', '')
                
                if not disclose_date_str or not exec_date_str:
                    logger.debug(f"Trade missing dates, skipping")
                    continue
                
                # Parse dates (FMP returns YYYY-MM-DD format)
                disclose_date = datetime.strptime(disclose_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                
                # Check disclose date
                days_since_disclose = (now - disclose_date).days
                if days_since_disclose > max_disclose_days:
                    logger.debug(f"Trade disclosed {days_since_disclose} days ago (max: {max_disclose_days}), filtering out")
                    continue
                
                # Check execution date
                days_since_exec = (now - exec_date).days
                if days_since_exec > max_exec_days:
                    logger.debug(f"Trade executed {days_since_exec} days ago (max: {max_exec_days}), filtering out")
                    continue
                
                # Check price delta
                exec_price = self._get_price_at_date(trade.get('symbol', ''), exec_date)
                if exec_price:
                    price_delta_pct = abs((current_price - exec_price) / exec_price * 100)
                    if price_delta_pct > max_price_delta_pct:
                        logger.debug(f"Price moved {price_delta_pct:.1f}% (max: {max_price_delta_pct}%), filtering out")
                        continue
                    
                    # Add calculated fields to trade
                    trade['exec_price'] = exec_price
                    trade['current_price'] = current_price
                    trade['price_delta_pct'] = (current_price - exec_price) / exec_price * 100
                    trade['days_since_disclose'] = days_since_disclose
                    trade['days_since_exec'] = days_since_exec
                
                filtered_trades.append(trade)
                
            except Exception as e:
                logger.warning(f"Error processing trade: {e}")
                continue
        
        logger.info(f"Filtered {len(filtered_trades)} trades from {len(trades)} total")
        return filtered_trades
    
    def _calculate_trader_performance(self, trader_history: List[Dict[str, Any]], 
                                     current_trade_symbol: str) -> float:
        """
        Calculate the historical performance growth for a trader.
        
        Args:
            trader_history: List of previous trades by this person
            current_trade_symbol: Symbol of current trade (to exclude it)
            
        Returns:
            Average growth percentage across all completed trades
        """
        if not trader_history:
            return 0.0
        
        growth_values = []
        
        for trade in trader_history:
            # Skip the current symbol to avoid circular logic
            if trade.get('symbol', '').upper() == current_trade_symbol.upper():
                continue
            
            try:
                exec_date_str = trade.get('transactionDate', '')
                if not exec_date_str:
                    continue
                
                exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                symbol = trade.get('symbol', '')
                
                if not symbol:
                    continue
                
                # Get execution price and current price
                exec_price = self._get_price_at_date(symbol, exec_date)
                if not exec_price:
                    continue
                
                current_price_hist = self._get_current_price(symbol)
                if not current_price_hist:
                    continue
                
                # Calculate growth
                growth_pct = ((current_price_hist - exec_price) / exec_price) * 100
                
                # For purchases, positive growth is good
                # For sales, we inverse the logic (selling before a drop is good)
                transaction_type = trade.get('type', '').lower()
                if 'sale' in transaction_type or 'sell' in transaction_type:
                    growth_pct = -growth_pct
                
                growth_values.append(growth_pct)
                
            except Exception as e:
                logger.debug(f"Error calculating growth for historical trade: {e}")
                continue
        
        if not growth_values:
            return 0.0
        
        # Calculate average growth
        avg_growth = sum(growth_values) / len(growth_values)
        logger.debug(f"Calculated average growth: {avg_growth:.1f}% from {len(growth_values)} trades")
        
        return avg_growth
    
    def _calculate_confidence(self, trade: Dict[str, Any], 
                             trader_performance: float,
                             max_confidence: float = 80.0) -> float:
        """
        Calculate confidence for a trade recommendation.
        
        Formula:
        1. Start at 50% base confidence
        2. Add trader performance (growth), capped at max_confidence
        3. Add 5% per $100k invested
        
        Args:
            trade: Trade record with amount information
            trader_performance: Historical growth percentage
            max_confidence: Maximum confidence cap (default 80%)
            
        Returns:
            Confidence percentage (0-100)
        """
        # Base confidence
        confidence = 50.0
        
        # Add historical performance (can be positive or negative)
        confidence += trader_performance
        
        # Cap at max_confidence before adding investment boost
        confidence = min(max_confidence, max(0, confidence))
        
        # Add investment size boost (+5% per $100k)
        try:
            amount_str = trade.get('amount', '0')
            # Remove non-numeric characters (like $, commas)
            amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
            amount = float(amount_str) if amount_str else 0
            
            # Calculate boost: +5% per $100k
            investment_boost = (amount / 100000) * 5.0
            confidence += investment_boost
            
            logger.debug(f"Investment amount: ${amount:,.2f} -> boost: +{investment_boost:.1f}%")
            
        except Exception as e:
            logger.debug(f"Error parsing trade amount: {e}")
        
        # Final cap at 100%
        confidence = min(100.0, max(0, confidence))
        
        return confidence
    
    def _calculate_recommendation(self, filtered_trades: List[Dict[str, Any]],
                                  symbol: str,
                                  current_price: float) -> Dict[str, Any]:
        """
        Calculate trading recommendation from filtered senate trades.
        
        Args:
            filtered_trades: List of relevant trades after filtering
            symbol: Stock symbol
            current_price: Current stock price
            
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
                'trade_count': 0
            }
        
        # Aggregate trade information
        buy_count = 0
        sell_count = 0
        total_buy_amount = 0.0
        total_sell_amount = 0.0
        trade_details = []
        
        for trade in filtered_trades:
            trader_name = trade.get('representative', 'Unknown')
            transaction_type = trade.get('type', '').lower()
            
            # Determine if it's a buy or sell
            is_buy = 'purchase' in transaction_type or 'buy' in transaction_type
            is_sell = 'sale' in transaction_type or 'sell' in transaction_type
            
            # Get trader history and calculate performance
            trader_history = self._fetch_trader_history(trader_name)
            trader_performance = self._calculate_trader_performance(trader_history or [], symbol)
            
            # Calculate confidence for this specific trade
            trade_confidence = self._calculate_confidence(trade, trader_performance)
            
            # Store trade details
            trade_info = {
                'trader': trader_name,
                'type': transaction_type,
                'amount': trade.get('amount', 'N/A'),
                'exec_date': trade.get('transactionDate', 'N/A'),
                'disclose_date': trade.get('disclosureDate', 'N/A'),
                'exec_price': trade.get('exec_price'),
                'current_price': trade.get('current_price'),
                'price_delta_pct': trade.get('price_delta_pct', 0),
                'trader_performance': trader_performance,
                'confidence': trade_confidence,
                'days_since_exec': trade.get('days_since_exec', 0),
                'days_since_disclose': trade.get('days_since_disclose', 0)
            }
            trade_details.append(trade_info)
            
            # Count buy/sell
            if is_buy:
                buy_count += 1
                try:
                    amount_str = trade.get('amount', '0')
                    amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
                    total_buy_amount += float(amount_str) if amount_str else 0
                except:
                    pass
            elif is_sell:
                sell_count += 1
                try:
                    amount_str = trade.get('amount', '0')
                    amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
                    total_sell_amount += float(amount_str) if amount_str else 0
                except:
                    pass
        
        # Determine overall signal based on trade consensus
        if buy_count > sell_count:
            signal = OrderRecommendation.BUY
            dominant_count = buy_count
            dominant_amount = total_buy_amount
        elif sell_count > buy_count:
            signal = OrderRecommendation.SELL
            dominant_count = sell_count
            dominant_amount = total_sell_amount
        else:
            signal = OrderRecommendation.HOLD
            dominant_count = buy_count + sell_count
            dominant_amount = total_buy_amount + total_sell_amount
        
        # Calculate overall confidence (average of individual trade confidences weighted by dominance)
        if signal == OrderRecommendation.BUY:
            relevant_trades = [t for t in trade_details if 'purchase' in t['type'].lower() or 'buy' in t['type'].lower()]
        elif signal == OrderRecommendation.SELL:
            relevant_trades = [t for t in trade_details if 'sale' in t['type'].lower() or 'sell' in t['type'].lower()]
        else:
            relevant_trades = trade_details
        
        if relevant_trades:
            overall_confidence = sum(t['confidence'] for t in relevant_trades) / len(relevant_trades)
        else:
            overall_confidence = 50.0
        
        # Calculate expected profit based on average price delta
        avg_price_delta = sum(t['price_delta_pct'] for t in trade_details) / len(trade_details) if trade_details else 0
        expected_profit = avg_price_delta if signal == OrderRecommendation.BUY else -avg_price_delta
        
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
- Trader Historical Performance: {trade_info['trader_performance']:+.1f}%
- Trade Confidence: {trade_info['confidence']:.1f}%
"""
        
        details += f"""
Confidence Calculation Method:
1. Base Confidence: 50%
2. + Trader Historical Performance (average across past trades)
3. + Investment Size Boost (+5% per $100k invested)
4. Cap: Max 80% before investment boost, 100% final

Expected Profit is based on the average price movement since execution.
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
            'total_sell_amount': total_sell_amount
        }
    
    def _create_expert_recommendation(self, recommendation_data: Dict[str, Any], 
                                     symbol: str, market_analysis_id: int,
                                     current_price: Optional[float]) -> int:
        """Create ExpertRecommendation record in database."""
        try:
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
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence, "
                       f"based on {recommendation_data['trade_count']} senate/house trades")
            return recommendation_id
            
        except Exception as e:
            logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
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
            logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
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
        logger.info(f"Starting FMPSenateTrade analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Get settings
            max_disclose_days = self.settings.get('max_disclose_date_days', 30)
            max_exec_days = self.settings.get('max_trade_exec_days', 60)
            max_price_delta_pct = self.settings.get('max_trade_price_delta_pct', 10.0)
            
            # Get current price first
            current_price = self._get_current_price(symbol)
            if not current_price:
                raise ValueError(f"Unable to get current price for {symbol}")
            
            # Fetch senate trades
            all_trades = self._fetch_senate_trades(symbol)
            
            if all_trades is None:
                raise ValueError("Failed to fetch senate trades from FMP API")
            
            # Filter trades based on settings
            filtered_trades = self._filter_trades(
                all_trades, max_disclose_days, max_exec_days, 
                max_price_delta_pct, current_price
            )
            
            # Calculate recommendation
            recommendation_data = self._calculate_recommendation(
                filtered_trades, symbol, current_price
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
            market_analysis.state = {
                'senate_trade': {
                    'recommendation': {
                        'signal': recommendation_data['signal'].value,
                        'confidence': recommendation_data['confidence'],
                        'expected_profit_percent': recommendation_data['expected_profit_percent'],
                        'details': recommendation_data['details']
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
            
            logger.info(f"Completed FMPSenateTrade analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%, "
                       f"{recommendation_data['trade_count']} trades analyzed)")
            
        except Exception as e:
            logger.error(f"FMPSenateTrade analysis failed for {symbol}: {e}", exc_info=True)
            
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
                logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
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
            else:
                with ui.card().classes('w-full p-4'):
                    ui.label(f"Unknown analysis status: {market_analysis.status}")
                    
        except Exception as e:
            logger.error(f"Error rendering market analysis {market_analysis.id}: {e}", exc_info=True)
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
        with ui.card().classes('w-full'):
            # Header
            with ui.card_section().classes('bg-purple-1'):
                ui.label('Senate/House Trading Activity Analysis').classes('text-h5 text-weight-bold')
                ui.label(f'{market_analysis.symbol} - Government Official Trades').classes('text-grey-7')
            
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
                        ui.label('Recommendation').classes('text-grey-6 text-caption')
                        with ui.row().classes('items-center gap-2'):
                            ui.icon(signal_icon, color=signal_color, size='2rem')
                            ui.label(signal).classes(f'text-h4 text-{signal_color}')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Confidence').classes('text-grey-6 text-caption')
                        ui.label(f'{confidence:.1f}%').classes('text-h4')
                    
                    with ui.column().classes('text-right'):
                        ui.label('Expected Profit').classes('text-grey-6 text-caption')
                        profit_color = 'positive' if expected_profit > 0 else 'negative' if expected_profit < 0 else 'grey'
                        ui.label(f'{expected_profit:+.1f}%').classes(f'text-h4 text-{profit_color}')
                
                if current_price:
                    ui.separator().classes('my-2')
                    ui.label(f'Current Price: ${current_price:.2f}').classes('text-grey-7')
            
            # Trade Statistics
            total_trades = stats.get('total_trades', 0)
            filtered_trades = stats.get('filtered_trades', 0)
            buy_count = stats.get('buy_count', 0)
            sell_count = stats.get('sell_count', 0)
            total_buy_amount = stats.get('total_buy_amount', 0)
            total_sell_amount = stats.get('total_sell_amount', 0)
            
            with ui.card_section().classes('bg-grey-1'):
                ui.label('Trade Activity Summary').classes('text-subtitle1 text-weight-medium mb-3')
                
                with ui.grid(columns=2).classes('w-full gap-4'):
                    # Total Trades
                    with ui.card().classes('bg-blue-50'):
                        ui.label('Total Trades Found').classes('text-caption text-grey-7')
                        ui.label(str(total_trades)).classes('text-h5 text-blue-700')
                        ui.label(f'{filtered_trades} after filtering').classes('text-xs text-blue-600')
                    
                    # Buy Activity
                    with ui.card().classes('bg-green-50'):
                        ui.label('Buy Trades').classes('text-caption text-grey-7')
                        ui.label(str(buy_count)).classes('text-h5 text-green-700')
                        ui.label(f'${total_buy_amount:,.0f} total').classes('text-xs text-green-600')
                    
                    # Sell Activity
                    with ui.card().classes('bg-red-50'):
                        ui.label('Sell Trades').classes('text-caption text-grey-7')
                        ui.label(str(sell_count)).classes('text-h5 text-red-700')
                        ui.label(f'${total_sell_amount:,.0f} total').classes('text-xs text-red-600')
                    
                    # Signal Strength
                    with ui.card().classes('bg-purple-50'):
                        ui.label('Signal Strength').classes('text-caption text-grey-7')
                        consensus_pct = (max(buy_count, sell_count) / (buy_count + sell_count) * 100) if (buy_count + sell_count) > 0 else 0
                        ui.label(f'{consensus_pct:.0f}%').classes('text-h5 text-purple-700')
                        ui.label('consensus').classes('text-xs text-purple-600')
            
            # Individual Trades
            if trades:
                with ui.card_section():
                    ui.label(f'Individual Trades ({len(trades)})').classes('text-subtitle1 text-weight-medium mb-3')
                    
                    for i, trade in enumerate(trades[:5], 1):  # Show top 5
                        trade_type = trade.get('type', 'Unknown')
                        is_buy = 'purchase' in trade_type.lower() or 'buy' in trade_type.lower()
                        
                        with ui.card().classes(f'w-full {"bg-green-50" if is_buy else "bg-red-50"}'):
                            with ui.row().classes('w-full items-start justify-between'):
                                with ui.column().classes('flex-grow'):
                                    ui.label(f'Trade #{i}: {trade.get("trader", "Unknown")}').classes('text-weight-medium')
                                    ui.label(f'{trade_type} - {trade.get("amount", "N/A")}').classes('text-sm text-grey-7')
                                    
                                    with ui.row().classes('gap-4 mt-2 text-xs'):
                                        ui.label(f'Exec: {trade.get("exec_date", "N/A")} ({trade.get("days_since_exec", 0)}d ago)')
                                        ui.label(f'Disclosed: {trade.get("disclose_date", "N/A")} ({trade.get("days_since_disclose", 0)}d ago)')
                                
                                with ui.column().classes('text-right'):
                                    ui.label(f'Confidence: {trade.get("confidence", 0):.1f}%').classes('text-sm text-weight-medium')
                                    
                                    exec_price = trade.get('exec_price')
                                    price_delta = trade.get('price_delta_pct', 0)
                                    if exec_price:
                                        delta_color = 'positive' if price_delta > 0 else 'negative'
                                        ui.label(f'${exec_price:.2f} → ${trade.get("current_price", 0):.2f}').classes('text-xs text-grey-7')
                                        ui.label(f'{price_delta:+.1f}%').classes(f'text-sm text-{delta_color}')
                                    
                                    perf = trade.get('trader_performance', 0)
                                    if perf != 0:
                                        perf_color = 'positive' if perf > 0 else 'negative'
                                        ui.label(f'Trader Hist: {perf:+.1f}%').classes(f'text-xs text-{perf_color}')
                    
                    if len(trades) > 5:
                        ui.label(f'+ {len(trades) - 5} more trades').classes('text-sm text-grey-6 mt-2')
            
            # Settings
            max_disclose = settings.get('max_disclose_date_days', 30)
            max_exec = settings.get('max_trade_exec_days', 60)
            max_delta = settings.get('max_trade_price_delta_pct', 10.0)
            
            with ui.card_section():
                ui.label('Filter Settings').classes('text-subtitle1 text-weight-medium mb-2')
                
                with ui.row().classes('gap-4'):
                    ui.label(f'Max Disclose Age: {max_disclose} days').classes('text-sm text-grey-7')
                    ui.label(f'Max Exec Age: {max_exec} days').classes('text-sm text-grey-7')
                    ui.label(f'Max Price Delta: {max_delta}%').classes('text-sm text-grey-7')
            
            # Methodology
            with ui.expansion('Calculation Methodology', icon='info').classes('w-full'):
                with ui.card_section().classes('bg-grey-1'):
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
