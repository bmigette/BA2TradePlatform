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
from ...core.types import MarketAnalysisStatus, OrderRecommendation, RiskLevel, TimeHorizon
from ...logger import logger
from ...config import get_app_setting


class FMPSenateTraderCopy(MarketExpertInterface):
    """
    FMPSenateTraderCopy Expert Implementation
    
    Expert that copies trades from specific government officials.
    When a trade is found from a followed senator/representative within
    the age criteria, generates immediate BUY/SELL recommendation with
    100% confidence and 50% expected profit.
    
    This expert can recommend its own instruments and does not expand
    instrument jobs to avoid duplication.
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
            "can_recommend_instruments": True,  # This expert can select its own instruments
        }
    
    def __init__(self, id: int):
        """Initialize FMPSenateTraderCopy expert with database instance."""
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
            "should_expand_instrument_jobs": {
                "type": "bool",
                "required": False,
                "default": False,
                "description": "Expand instrument jobs",
                "tooltip": "When false, prevents job duplication by running analysis as-is without expanding to additional instruments. Recommended: False for copy trading to avoid excessive API calls."
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
            logger.error("Cannot fetch senate trades: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/stable/senate-trades"
            params = {
                "symbol": symbol.upper(),
                "apikey": self._api_key
            }
            
            logger.debug(f"Fetching FMP senate trades for {symbol}")
            response = requests.get(url, params=params, timeout=30)
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
    
    def _fetch_house_trades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch house trades from FMP API for a specific symbol.
        
        Args:
            symbol: Stock symbol to query
            
        Returns:
            List of trade records or None if error
        """
        if not self._api_key:
            logger.error("Cannot fetch house trades: FMP API key not configured")
            return None
        
        try:
            url = f"https://financialmodelingprep.com/stable/house-trades"
            params = {
                "symbol": symbol.upper(),
                "apikey": self._api_key
            }
            
            logger.debug(f"Fetching FMP house trades for {symbol}")
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            logger.debug(f"Received {len(data) if isinstance(data, list) else 0} house trade records for {symbol}")
            
            return data if isinstance(data, list) else []
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch FMP house trades for {symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching house trades for {symbol}: {e}", exc_info=True)
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
                    logger.debug(f"Trade missing dates, skipping: {trader_name}")
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
                logger.error(f"Error processing trade: {e}", exc_info=True)
                continue
        
        logger.info(f"Filtered {len(filtered_trades)} trades from {len(trades)} total based on age criteria")
        return filtered_trades
    
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
                                 current_price: float) -> Dict[str, Any]:
        """
        Generate recommendations from copy trades.
        
        Args:
            copy_trades: List of trades from followed traders
            symbol: Stock symbol
            current_price: Current stock price
            
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
        
        # Determine signal based on the most recent trade
        # If multiple trades exist, use the most recent one
        most_recent_trade = max(symbol_trades, key=lambda t: t.get('exec_date', ''))
        
        transaction_type = most_recent_trade.get('type', '').lower()
        trader_name = f"{most_recent_trade.get('firstName', '')} {most_recent_trade.get('lastName', '')}".strip() or 'Unknown'
        
        # Determine signal
        is_buy = 'purchase' in transaction_type or 'buy' in transaction_type
        is_sell = 'sale' in transaction_type or 'sell' in transaction_type
        
        if is_buy:
            signal = OrderRecommendation.BUY
        elif is_sell:
            signal = OrderRecommendation.SELL
        else:
            signal = OrderRecommendation.HOLD
        
        # Copy trade: 100% confidence, 50% expected profit (always positive)
        confidence = 100.0
        expected_profit = 50.0  # Always positive regardless of BUY/SELL
        
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
- Primary Trader: {trader_name}

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
        
        return {
            'signal': signal,
            'confidence': confidence,
            'expected_profit_percent': expected_profit,
            'details': details,
            'trades': trade_details,
            'trade_count': len(symbol_trades),
            'copy_trades_found': len(copy_trades)
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
                risk_level=RiskLevel.MEDIUM,  # Copy trades are medium risk
                time_horizon=TimeHorizon.MEDIUM_TERM,  # Medium term based on disclosure lag
                market_analysis_id=market_analysis_id,
                created_at=datetime.now(timezone.utc)
            )
            
            recommendation_id = add_instance(expert_recommendation)
            logger.info(f"Created ExpertRecommendation (ID: {recommendation_id}) for {symbol}: "
                       f"{recommendation_data['signal'].value} with {recommendation_data['confidence']:.1f}% confidence, "
                       f"based on {recommendation_data['trade_count']} copy trades")
            return recommendation_id
            
        except Exception as e:
            logger.error(f"Failed to create ExpertRecommendation for {symbol}: {e}", exc_info=True)
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
            logger.error(f"Failed to store analysis outputs: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """
        Run FMPSenateTraderCopy analysis for a symbol and create ExpertRecommendation.
        
        Args:
            symbol: Financial instrument symbol to analyze
            market_analysis: MarketAnalysis instance to update with results
        """
        logger.info(f"Starting FMPSenateTraderCopy analysis for {symbol} (Analysis ID: {market_analysis.id})")
        
        try:
            # Update status to running
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)
            
            # Get settings
            copy_trade_names_setting = self.settings.get('copy_trade_names', '').strip()
            if not copy_trade_names_setting:
                raise ValueError("No copy trade names configured. Please set 'copy_trade_names' in expert settings.")
            
            # Parse copy trade names
            copy_trade_names = [name.strip().lower() for name in copy_trade_names_setting.split(',') if name.strip()]
            if not copy_trade_names:
                raise ValueError("No valid copy trade names found after parsing.")
            
            max_disclose_days = self.settings.get('max_disclose_date_days', 30)
            max_exec_days = self.settings.get('max_trade_exec_days', 60)
            
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
            
            logger.info(f"Fetched {len(senate_trades) if senate_trades else 0} senate trades and "
                       f"{len(house_trades) if house_trades else 0} house trades for {symbol}")
            
            # Filter trades by age
            filtered_trades = self._filter_trades_by_age(
                all_trades, max_disclose_days, max_exec_days, symbol
            )
            
            # Find copy trades from followed traders
            copy_trades = self._find_copy_trades(filtered_trades, copy_trade_names)
            
            # Generate recommendations
            recommendation_data = self._generate_recommendations(
                copy_trades, symbol, current_price
            )
            
            # Create ExpertRecommendation record
            recommendation_id = self._create_expert_recommendation(
                recommendation_data, symbol, market_analysis.id, current_price
            )
            
            # Store analysis outputs
            self._store_analysis_outputs(
                market_analysis.id, symbol, recommendation_data, 
                all_trades, filtered_trades, copy_trades
            )
            
            # Store analysis state
            market_analysis.state = {
                'copy_trade': {
                    'recommendation': {
                        'signal': recommendation_data['signal'].value,
                        'confidence': recommendation_data['confidence'],
                        'expected_profit_percent': recommendation_data['expected_profit_percent'],
                        'details': recommendation_data['details']
                    },
                    'trade_statistics': {
                        'total_trades': len(all_trades),
                        'filtered_trades': len(filtered_trades),
                        'copy_trades_found': len(copy_trades),
                        'symbol_trades': recommendation_data['trade_count']
                    },
                    'trades': recommendation_data['trades'],
                    'settings': {
                        'copy_trade_names': copy_trade_names_setting,
                        'max_disclose_date_days': max_disclose_days,
                        'max_trade_exec_days': max_exec_days
                    },
                    'expert_recommendation_id': recommendation_id,
                    'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
                    'current_price': current_price
                }
            }
            
            # Update status to completed
            market_analysis.status = MarketAnalysisStatus.COMPLETED
            update_instance(market_analysis)
            
            logger.info(f"Completed FMPSenateTraderCopy analysis for {symbol}: {recommendation_data['signal'].value} "
                       f"(confidence: {recommendation_data['confidence']:.1f}%, "
                       f"{recommendation_data['trade_count']} relevant trades found)")
            
        except Exception as e:
            logger.error(f"FMPSenateTraderCopy analysis failed for {symbol}: {e}", exc_info=True)
            
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
                logger.error(f"Failed to store error output: {db_error}", exc_info=True)
            
            raise
    
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
    
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        """Render completed analysis with detailed UI."""
        from nicegui import ui
        
        if not market_analysis.state or 'copy_trade' not in market_analysis.state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        
        state = market_analysis.state['copy_trade']
        rec = state.get('recommendation', {})
        stats = state.get('trade_statistics', {})
        trades = state.get('trades', [])
        settings = state.get('settings', {})
        current_price = state.get('current_price')
        
        # Main card
        with ui.card().classes('w-full'):
            # Header
            with ui.card_section().classes('bg-orange-1'):
                ui.label('Senate/House Copy Trading Analysis').classes('text-h5 text-weight-bold')
                ui.label(f'{market_analysis.symbol} - Following Specific Traders').classes('text-grey-7')
            
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
                        ui.label('Copy Trade Signal').classes('text-grey-6 text-caption')
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
            copy_trades_found = stats.get('copy_trades_found', 0)
            symbol_trades = stats.get('symbol_trades', 0)
            
            with ui.card_section().classes('bg-grey-1'):
                ui.label('Copy Trade Activity Summary').classes('text-subtitle1 text-weight-medium mb-3')
                
                with ui.grid(columns=2).classes('w-full gap-4'):
                    # Total Trades
                    with ui.card().classes('bg-blue-50'):
                        ui.label('Total Trades Found').classes('text-caption text-grey-7')
                        ui.label(str(total_trades)).classes('text-h5 text-blue-700')
                        ui.label(f'{filtered_trades} after age filter').classes('text-xs text-blue-600')
                    
                    # Copy Trades
                    with ui.card().classes('bg-orange-50'):
                        ui.label('Copy Trades Found').classes('text-caption text-grey-7')
                        ui.label(str(copy_trades_found)).classes('text-h5 text-orange-700')
                        ui.label('from followed traders').classes('text-xs text-orange-600')
                    
                    # Symbol Trades
                    with ui.card().classes('bg-purple-50'):
                        ui.label(f'{market_analysis.symbol} Trades').classes('text-caption text-grey-7')
                        ui.label(str(symbol_trades)).classes('text-h5 text-purple-700')
                        ui.label('relevant to this symbol').classes('text-xs text-purple-600')
                    
                    # Status
                    status_color = 'green' if copy_trades_found > 0 else 'grey'
                    with ui.card().classes(f'bg-{status_color}-50'):
                        ui.label('Copy Trade Status').classes('text-caption text-grey-7')
                        status_text = 'ACTIVE' if copy_trades_found > 0 else 'NO MATCHES'
                        ui.label(status_text).classes(f'text-h5 text-{status_color}-700')
                        ui.label('following traders').classes(f'text-xs text-{status_color}-600')
            
            # Individual Trades
            if trades:
                with ui.card_section():
                    ui.label(f'Copy Trades for {market_analysis.symbol} ({len(trades)})').classes('text-subtitle1 text-weight-medium mb-3')
                    
                    for i, trade in enumerate(trades, 1):
                        trade_type = trade.get('type', 'Unknown')
                        is_buy = 'purchase' in trade_type.lower() or 'buy' in trade_type.lower()
                        
                        with ui.card().classes(f'w-full {"bg-green-50" if is_buy else "bg-red-50"}'):
                            with ui.row().classes('w-full items-start justify-between'):
                                with ui.column().classes('flex-grow'):
                                    ui.label(f'Trade #{i}: {trade.get("trader", "Unknown")}').classes('text-weight-medium')
                                    ui.label(f'{trade_type} - {trade.get("amount", "N/A")}').classes('text-sm text-grey-7')
                                    ui.label(f'Matched: {trade.get("matched_target", "Unknown")}').classes('text-sm text-orange-600')
                                    
                                    with ui.row().classes('gap-4 mt-2 text-xs'):
                                        ui.label(f'Exec: {trade.get("exec_date", "N/A")} ({trade.get("days_since_exec", 0)}d ago)')
                                        ui.label(f'Disclosed: {trade.get("disclose_date", "N/A")} ({trade.get("days_since_disclose", 0)}d ago)')
                                
                                with ui.column().classes('text-right'):
                                    ui.label('100% Confidence').classes('text-sm text-weight-medium text-positive')
                                    ui.label('Copy Trade').classes('text-xs text-orange-600')
            
            # Followed Traders
            copy_trade_names = settings.get('copy_trade_names', '')
            if copy_trade_names:
                with ui.card_section():
                    ui.label('Followed Traders').classes('text-subtitle1 text-weight-medium mb-2')
                    ui.label(copy_trade_names).classes('text-sm text-grey-7 bg-grey-2 rounded p-2')
            
            # Settings
            max_disclose = settings.get('max_disclose_date_days', 30)
            max_exec = settings.get('max_trade_exec_days', 60)
            
            with ui.card_section():
                ui.label('Filter Settings').classes('text-subtitle1 text-weight-medium mb-2')
                
                with ui.row().classes('gap-4'):
                    ui.label(f'Max Disclose Age: {max_disclose} days').classes('text-sm text-grey-7')
                    ui.label(f'Max Exec Age: {max_exec} days').classes('text-sm text-grey-7')
            
            # Methodology
            with ui.expansion('Copy Trading Methodology', icon='info').classes('w-full'):
                with ui.card_section().classes('bg-grey-1'):
                    ui.markdown(f'''
**Copy Trading Logic:**

1. **Target Selection**: Follow specific senators/representatives by name
2. **Age Filtering**: Only consider trades within configured age limits
3. **Signal Generation**: 
   - **BUY** if followed trader bought the instrument
   - **SELL** if followed trader sold the instrument
   - **HOLD** if no relevant trades found
4. **Confidence**: Always **100%** for copy trades
5. **Expected Profit**: Always **50%** (fixed target)

**Multiple Trades Handling:**
- If multiple trades exist for the same instrument, the **most recent trade** determines the signal
- Only one recommendation per instrument to avoid conflicts

**Age Filtering:**
- Trades disclosed more than **{max_disclose}** days ago are ignored
- Trades executed more than **{max_exec}** days ago are ignored

**Followed Traders:**
```
{copy_trade_names}
```

**Expert Properties:**
- **can_recommend_instruments**: True (can select its own instruments)
- **should_expand_instrument_jobs**: False (no job duplication)

**Note**: This expert prioritizes simplicity and speed. When a followed trader makes a qualifying trade, it immediately generates a high-confidence recommendation without complex analysis.
                    ''').classes('text-sm')