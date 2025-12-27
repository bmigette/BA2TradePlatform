"""
Tools Page

Collection of utility tools for market research and data exploration.
Contains tabbed interface with various tools like FMP Senate Trade browser.
"""

from nicegui import ui
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import asyncio
import requests

from ...config import get_app_setting
from ...logger import logger


class FMPSenateTradeTab:
    """
    Tab for browsing and searching FMP Senate/House trades.
    
    Allows users to search trades by symbol, trader name, and date range,
    displaying results in a paginated table.
    """
    
    def __init__(self):
        self.trades_table = None
        self.loading_spinner = None
        self.results_container = None
        
        # Filter state
        self.symbol_filter = ""
        self.trader_filter = ""
        self.trade_type_filter = "all"  # all, senate, house
        self.date_from = None
        self.date_to = None
        
        # Pagination
        self.current_page = 1
        self.page_size = 50
        self.total_records = 0
        self.all_trades = []  # Cached trades data
        
        self._api_key = get_app_setting('FMP_API_KEY')
        
        self.render()
    
    def render(self):
        """Render the FMP Senate Trade tab content."""
        with ui.card().classes('w-full'):
            ui.label('FMP Senate & House Trade Browser').classes('text-lg font-bold')
            ui.label('Search and explore trades made by US Senators and House Representatives').classes('text-sm text-secondary-custom mb-4')
            
            # API key warning
            if not self._api_key:
                with ui.card().classes('w-full alert-banner warning mb-4'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('warning', color='warning')
                        ui.label('FMP API key not configured. Please set FMP_API_KEY in Settings > App Settings.').classes('text-[#ffd93d]')
            
            # Filters row
            with ui.card().classes('w-full mb-4'):
                ui.label('Filters').classes('text-md font-semibold mb-2')
                
                with ui.row().classes('w-full gap-4 flex-wrap items-end'):
                    # Symbol filter
                    self.symbol_input = ui.input(
                        label='Symbol',
                        placeholder='e.g., AAPL, MSFT'
                    ).classes('w-32')
                    self.symbol_input.on('keydown.enter', lambda: self._search_trades())
                    
                    # Trader name filter
                    self.trader_input = ui.input(
                        label='Trader Name',
                        placeholder='e.g., Pelosi'
                    ).classes('w-48')
                    self.trader_input.on('keydown.enter', lambda: self._search_trades())
                    
                    # Trade type filter (Senate/House/All)
                    trade_type_options = {
                        'all': 'All Trades',
                        'senate': '🏛️ Senate Only',
                        'house': '🏠 House Only'
                    }
                    self.trade_type_select = ui.select(
                        options=trade_type_options,
                        value='all',
                        label='Chamber'
                    ).classes('w-40')
                    
                    # Date range filters
                    with ui.column().classes('gap-1'):
                        ui.label('Date From').classes('text-xs text-gray-600')
                        self.date_from_input = ui.input(
                            placeholder='YYYY-MM-DD'
                        ).classes('w-36')
                        # Set default to 30 days ago
                        default_from = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
                        self.date_from_input.value = default_from
                    
                    with ui.column().classes('gap-1'):
                        ui.label('Date To').classes('text-xs text-gray-600')
                        self.date_to_input = ui.input(
                            placeholder='YYYY-MM-DD'
                        ).classes('w-36')
                        # Set default to today
                        self.date_to_input.value = datetime.now().strftime('%Y-%m-%d')
                    
                    # Search button
                    ui.button('Search', on_click=self._search_trades, icon='search').props('color=primary')
                    
                    # Clear filters button
                    ui.button('Clear', on_click=self._clear_filters, icon='clear').props('flat')
            
            # Results section
            self.results_container = ui.column().classes('w-full')
            with self.results_container:
                # Loading spinner (hidden by default)
                self.loading_spinner = ui.spinner('dots', size='lg').classes('hidden')
                
                # Results info
                self.results_info = ui.label('').classes('text-sm text-gray-600 mb-2')
                
                # Trades table
                columns = [
                    {'name': 'chamber', 'label': 'Chamber', 'field': 'chamber', 'align': 'center', 'sortable': True},
                    {'name': 'trader', 'label': 'Trader', 'field': 'trader', 'align': 'left', 'sortable': True},
                    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left', 'sortable': True},
                    {'name': 'asset_description', 'label': 'Asset', 'field': 'asset_description', 'align': 'left'},
                    {'name': 'transaction_type', 'label': 'Type', 'field': 'transaction_type', 'align': 'center', 'sortable': True},
                    {'name': 'amount', 'label': 'Amount', 'field': 'amount', 'align': 'right'},
                    {'name': 'transaction_date', 'label': 'Trade Date', 'field': 'transaction_date', 'align': 'center', 'sortable': True},
                    {'name': 'disclosure_date', 'label': 'Disclosed', 'field': 'disclosure_date', 'align': 'center', 'sortable': True},
                    {'name': 'days_since_trade', 'label': 'Days Ago', 'field': 'days_since_trade', 'align': 'center', 'sortable': True},
                ]
                
                self.trades_table = ui.table(
                    columns=columns,
                    rows=[],
                    row_key='id',
                    pagination={'rowsPerPage': self.page_size, 'sortBy': 'transaction_date', 'descending': True}
                ).classes('w-full dark-pagination')
                
                # Style the table
                self.trades_table.add_slot('body-cell-chamber', '''
                    <q-td :props="props">
                        <q-badge :color="props.value === 'Senate' ? 'blue' : 'green'">
                            {{ props.value }}
                        </q-badge>
                    </q-td>
                ''')
                
                self.trades_table.add_slot('body-cell-transaction_type', '''
                    <q-td :props="props">
                        <q-badge :color="props.value.includes('Purchase') || props.value.includes('Buy') ? 'positive' : props.value.includes('Sale') || props.value.includes('Sell') ? 'negative' : 'grey'">
                            {{ props.value }}
                        </q-badge>
                    </q-td>
                ''')
                
                self.trades_table.add_slot('body-cell-symbol', '''
                    <q-td :props="props">
                        <span class="font-bold text-blue-600">{{ props.value }}</span>
                    </q-td>
                ''')
        
        # Auto-search on load if API key is present
        if self._api_key:
            asyncio.create_task(self._async_search_trades())
    
    def _clear_filters(self):
        """Clear all filters and reset to defaults."""
        self.symbol_input.value = ""
        self.trader_input.value = ""
        self.trade_type_select.value = "all"
        self.date_from_input.value = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        self.date_to_input.value = datetime.now().strftime('%Y-%m-%d')
        self.all_trades = []
        self.trades_table.rows = []
        self.results_info.text = ''
    
    def _search_trades(self):
        """Trigger async search for trades."""
        asyncio.create_task(self._async_search_trades())
    
    async def _async_search_trades(self):
        """Asynchronously search for trades based on filters."""
        if not self._api_key:
            ui.notify('FMP API key not configured', type='warning')
            return
        
        try:
            # Show loading
            self.loading_spinner.classes(remove='hidden')
            self.results_info.text = 'Searching...'
            
            # Get filter values
            symbol = self.symbol_input.value.strip().upper() if self.symbol_input.value else None
            trader = self.trader_input.value.strip().lower() if self.trader_input.value else None
            trade_type = self.trade_type_select.value
            date_from = self.date_from_input.value.strip() if self.date_from_input.value else None
            date_to = self.date_to_input.value.strip() if self.date_to_input.value else None
            
            # Fetch trades in background thread
            all_trades = await asyncio.to_thread(
                self._fetch_all_trades,
                symbol=symbol,
                trade_type=trade_type
            )
            
            if all_trades is None:
                self.loading_spinner.classes(add='hidden')
                self.results_info.text = 'Error fetching trades. Check logs for details.'
                ui.notify('Error fetching trades', type='negative')
                return
            
            # Filter trades by additional criteria
            filtered_trades = self._filter_trades(
                all_trades,
                trader_filter=trader,
                date_from=date_from,
                date_to=date_to
            )
            
            # Format for table display
            table_rows = self._format_trades_for_table(filtered_trades)
            
            # Update table
            self.all_trades = filtered_trades
            self.trades_table.rows = table_rows
            self.total_records = len(table_rows)
            
            # Update results info
            self.results_info.text = f'Found {self.total_records} trades'
            if symbol:
                self.results_info.text += f' for {symbol}'
            if trader:
                self.results_info.text += f' by traders matching "{trader}"'
            
            # Hide loading
            self.loading_spinner.classes(add='hidden')
            
        except RuntimeError as e:
            # Handle client disconnection gracefully
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                logger.debug("[FMPSenateTradeTab] Client disconnected during search")
            else:
                logger.error(f"Error searching trades: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error searching trades: {e}", exc_info=True)
            self.loading_spinner.classes(add='hidden')
            self.results_info.text = f'Error: {str(e)}'
            ui.notify(f'Error searching trades: {str(e)}', type='negative')
    
    def _fetch_all_trades(self, symbol: Optional[str] = None, trade_type: str = "all") -> Optional[List[Dict[str, Any]]]:
        """
        Fetch trades from FMP API.
        
        Args:
            symbol: Optional symbol to filter by
            trade_type: 'all', 'senate', or 'house'
            
        Returns:
            List of trade records or None if error
        """
        all_trades = []
        
        try:
            # Fetch Senate trades
            if trade_type in ['all', 'senate']:
                senate_trades = self._fetch_senate_trades(symbol)
                if senate_trades:
                    # Add chamber indicator
                    for trade in senate_trades:
                        trade['_chamber'] = 'Senate'
                    all_trades.extend(senate_trades)
            
            # Fetch House trades
            if trade_type in ['all', 'house']:
                house_trades = self._fetch_house_trades(symbol)
                if house_trades:
                    # Add chamber indicator
                    for trade in house_trades:
                        trade['_chamber'] = 'House'
                    all_trades.extend(house_trades)
            
            logger.info(f"Fetched {len(all_trades)} total trades (symbol={symbol}, type={trade_type})")
            return all_trades
            
        except Exception as e:
            logger.error(f"Error fetching trades: {e}", exc_info=True)
            return None
    
    def _fetch_senate_trades(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Fetch senate trades from FMP API."""
        try:
            if symbol:
                url = "https://financialmodelingprep.com/stable/senate-trades"
                params = {"apikey": self._api_key, "symbol": symbol}
            else:
                url = "https://financialmodelingprep.com/stable/senate-latest"
                params = {"apikey": self._api_key, "page": 0, "limit": 1000}
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            return data if isinstance(data, list) else []
            
        except Exception as e:
            logger.error(f"Error fetching senate trades: {e}", exc_info=True)
            return []
    
    def _fetch_house_trades(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Fetch house trades from FMP API."""
        try:
            if symbol:
                url = "https://financialmodelingprep.com/stable/house-trades"
                params = {"apikey": self._api_key, "symbol": symbol}
            else:
                url = "https://financialmodelingprep.com/stable/house-latest"
                params = {"apikey": self._api_key, "page": 0, "limit": 1000}
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            return data if isinstance(data, list) else []
            
        except Exception as e:
            logger.error(f"Error fetching house trades: {e}", exc_info=True)
            return []
    
    def _filter_trades(self, trades: List[Dict[str, Any]], 
                       trader_filter: Optional[str] = None,
                       date_from: Optional[str] = None,
                       date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Filter trades by trader name and date range.
        
        Args:
            trades: List of trade records
            trader_filter: Trader name substring to match (case-insensitive)
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)
            
        Returns:
            Filtered list of trades
        """
        filtered = []
        
        # Parse date filters
        date_from_dt = None
        date_to_dt = None
        
        if date_from:
            try:
                date_from_dt = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                logger.warning(f"Invalid date_from format: {date_from}")
        
        if date_to:
            try:
                date_to_dt = datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                logger.warning(f"Invalid date_to format: {date_to}")
        
        for trade in trades:
            # Get trader name
            first_name = trade.get('firstName', '') or trade.get('first_name', '') or ''
            last_name = trade.get('lastName', '') or trade.get('last_name', '') or ''
            trader_name = f"{first_name} {last_name}".strip().lower()
            
            # Filter by trader name
            if trader_filter and trader_filter not in trader_name:
                continue
            
            # Get transaction date
            trans_date_str = trade.get('transactionDate', '') or trade.get('transaction_date', '')
            if trans_date_str:
                try:
                    trans_date = datetime.strptime(trans_date_str, "%Y-%m-%d")
                    
                    # Filter by date range
                    if date_from_dt and trans_date < date_from_dt:
                        continue
                    if date_to_dt and trans_date > date_to_dt:
                        continue
                        
                except ValueError:
                    pass
            
            filtered.append(trade)
        
        return filtered
    
    def _format_trades_for_table(self, trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format trades for table display.
        
        Args:
            trades: List of trade records
            
        Returns:
            List of formatted rows for the table
        """
        rows = []
        now = datetime.now()
        
        for i, trade in enumerate(trades):
            try:
                # Extract trader name
                first_name = trade.get('firstName', '') or trade.get('first_name', '') or ''
                last_name = trade.get('lastName', '') or trade.get('last_name', '') or ''
                trader_name = f"{first_name} {last_name}".strip() or 'Unknown'
                
                # Extract dates
                trans_date = trade.get('transactionDate', '') or trade.get('transaction_date', '') or ''
                disclosure_date = trade.get('disclosureDate', '') or trade.get('disclosure_date', '') or ''
                
                # Calculate days since trade
                days_since = ''
                if trans_date:
                    try:
                        trans_dt = datetime.strptime(trans_date, "%Y-%m-%d")
                        days_since = (now - trans_dt).days
                    except ValueError:
                        pass
                
                # Extract transaction type
                trans_type = trade.get('type', '') or trade.get('transactionType', '') or trade.get('transaction_type', '') or 'Unknown'
                
                # Extract amount/range
                amount = trade.get('amount', '') or trade.get('amountRange', '') or ''
                
                # Get symbol and description
                symbol = trade.get('symbol', '') or trade.get('ticker', '') or ''
                asset_desc = trade.get('assetDescription', '') or trade.get('asset_description', '') or trade.get('assetType', '') or ''
                
                # Truncate long descriptions
                if len(asset_desc) > 50:
                    asset_desc = asset_desc[:47] + '...'
                
                rows.append({
                    'id': f"{i}_{symbol}_{trans_date}",
                    'chamber': trade.get('_chamber', 'Unknown'),
                    'trader': trader_name,
                    'symbol': symbol,
                    'asset_description': asset_desc,
                    'transaction_type': trans_type,
                    'amount': amount,
                    'transaction_date': trans_date,
                    'disclosure_date': disclosure_date,
                    'days_since_trade': days_since
                })
                
            except Exception as e:
                logger.warning(f"Error formatting trade: {e}")
                continue
        
        # Sort by transaction date descending
        rows.sort(key=lambda x: x.get('transaction_date', ''), reverse=True)
        
        return rows


class AnalystRatingsTab:
    """
    Tab for viewing analyst ratings from multiple sources.
    
    Shows ratings from Finnhub and FMP with target prices.
    """
    
    def __init__(self):
        self.ratings_container = None
        self.loading_spinner = None
        self.symbol_input = None
        
        # API keys
        self._fmp_api_key = get_app_setting('FMP_API_KEY')
        self._finnhub_api_key = get_app_setting('finnhub_api_key')
        
        self.render()
    
    def render(self):
        """Render the Analyst Ratings tab content."""
        with ui.card().classes('w-full'):
            ui.label('Analyst Ratings Browser').classes('text-lg font-bold')
            ui.label('View analyst ratings and price targets from Finnhub and FMP').classes('text-sm mb-4').style('color: #a0aec0;')
            
            # API key warnings
            missing_keys = []
            if not self._fmp_api_key:
                missing_keys.append('FMP_API_KEY')
            if not self._finnhub_api_key:
                missing_keys.append('finnhub_api_key')
            
            if missing_keys:
                with ui.card().classes('w-full alert-banner warning mb-4'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('warning', color='warning')
                        ui.label(f'Missing API keys: {", ".join(missing_keys)}. Configure in Settings > App Settings.').classes('text-[#ffd93d]')
            
            # Search section
            with ui.card().classes('w-full mb-4'):
                ui.label('Search Symbol').classes('text-md font-semibold mb-2')
                
                with ui.row().classes('w-full gap-4 items-end'):
                    self.symbol_input = ui.input(
                        label='Symbol',
                        placeholder='e.g., AAPL, MSFT, NVDA'
                    ).classes('w-48')
                    self.symbol_input.on('keydown.enter', lambda: self._search_ratings())
                    
                    ui.button('Search', on_click=self._search_ratings, icon='search').props('color=primary')
                    ui.button('Clear', on_click=self._clear_results, icon='clear').props('flat')
            
            # Results section
            self.ratings_container = ui.column().classes('w-full gap-4')
            with self.ratings_container:
                self.loading_spinner = ui.spinner('dots', size='lg').classes('hidden')
                self.results_area = ui.column().classes('w-full gap-4')
    
    def _clear_results(self):
        """Clear search results."""
        self.symbol_input.value = ""
        self.results_area.clear()
    
    def _search_ratings(self):
        """Trigger async search for ratings."""
        asyncio.create_task(self._async_search_ratings())
    
    async def _async_search_ratings(self):
        """Asynchronously search for analyst ratings."""
        symbol = self.symbol_input.value.strip().upper() if self.symbol_input.value else None
        
        if not symbol:
            ui.notify('Please enter a symbol', type='warning')
            return
        
        try:
            # Show loading
            self.loading_spinner.classes(remove='hidden')
            self.results_area.clear()
            
            # Fetch ratings from both sources in parallel
            finnhub_data = None
            fmp_grades_data = None
            fmp_target_data = None
            fmp_consensus_data = None
            
            if self._finnhub_api_key:
                finnhub_data = await asyncio.to_thread(self._fetch_finnhub_ratings, symbol)
            
            if self._fmp_api_key:
                fmp_grades_data = await asyncio.to_thread(self._fetch_fmp_grades, symbol)
                fmp_target_data = await asyncio.to_thread(self._fetch_fmp_price_target, symbol)
                fmp_consensus_data = await asyncio.to_thread(self._fetch_fmp_grades_consensus, symbol)
            
            # Hide loading
            self.loading_spinner.classes(add='hidden')
            
            # Display results
            with self.results_area:
                ui.label(f'Analyst Ratings for {symbol}').classes('text-xl font-bold mb-2')
                
                with ui.row().classes('w-full gap-4 flex-wrap'):
                    # Finnhub section
                    self._render_finnhub_section(finnhub_data, symbol)
                    
                    # FMP section
                    self._render_fmp_section(fmp_grades_data, fmp_target_data, fmp_consensus_data, symbol)
                    
        except RuntimeError as e:
            if "client" in str(e).lower() and "deleted" in str(e).lower():
                logger.debug("[AnalystRatingsTab] Client disconnected during search")
            else:
                logger.error(f"Error searching ratings: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error searching ratings: {e}", exc_info=True)
            self.loading_spinner.classes(add='hidden')
            ui.notify(f'Error searching ratings: {str(e)}', type='negative')
    
    def _fetch_finnhub_ratings(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch recommendation trends from Finnhub API."""
        try:
            url = "https://finnhub.io/api/v1/stock/recommendation"
            params = {"symbol": symbol, "token": self._finnhub_api_key}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error fetching Finnhub ratings for {symbol}: {e}", exc_info=True)
            return None
    
    def _fetch_fmp_grades(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch analyst grades from FMP API."""
        try:
            url = f"https://financialmodelingprep.com/stable/grades"
            params = {"symbol": symbol, "apikey": self._fmp_api_key, "limit": 20}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            logger.error(f"Error fetching FMP grades for {symbol}: {e}", exc_info=True)
            return None
    
    def _fetch_fmp_price_target(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch price target consensus from FMP API."""
        try:
            url = f"https://financialmodelingprep.com/stable/price-target-consensus"
            params = {"symbol": symbol, "apikey": self._fmp_api_key}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
            
        except Exception as e:
            logger.error(f"Error fetching FMP price target for {symbol}: {e}", exc_info=True)
            return None
    
    def _fetch_fmp_grades_consensus(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch grades consensus summary from FMP API."""
        try:
            url = f"https://financialmodelingprep.com/stable/grades-consensus"
            params = {"symbol": symbol, "apikey": self._fmp_api_key}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
            
        except Exception as e:
            logger.error(f"Error fetching FMP grades consensus for {symbol}: {e}", exc_info=True)
            return None
    
    def _render_finnhub_section(self, data: Optional[List[Dict[str, Any]]], symbol: str):
        """Render Finnhub ratings section."""
        with ui.card().classes('min-w-[400px] flex-1'):
            with ui.row().classes('items-center gap-2 mb-2'):
                ui.icon('analytics', size='sm', color='blue')
                ui.label('Finnhub Analyst Recommendations').classes('text-lg font-semibold')
            
            if not self._finnhub_api_key:
                ui.label('API key not configured').classes('italic').style('color: #a0aec0;')
                return
            
            if not data:
                ui.label('No data available').classes('italic').style('color: #a0aec0;')
                return
            
            # Use the most recent period
            latest = data[0] if data else {}
            period = latest.get('period', 'Unknown')
            
            strong_buy = latest.get('strongBuy', 0)
            buy = latest.get('buy', 0)
            hold = latest.get('hold', 0)
            sell = latest.get('sell', 0)
            strong_sell = latest.get('strongSell', 0)
            total = strong_buy + buy + hold + sell + strong_sell
            
            ui.label(f'Period: {period}').classes('text-sm mb-2').style('color: #a0aec0;')
            
            # Ratings summary with color-coded badges
            with ui.row().classes('gap-2 flex-wrap mb-3'):
                ui.badge(f'Strong Buy: {strong_buy}', color='green').props('outline')
                ui.badge(f'Buy: {buy}', color='teal').props('outline')
                ui.badge(f'Hold: {hold}', color='grey').props('outline')
                ui.badge(f'Sell: {sell}', color='orange').props('outline')
                ui.badge(f'Strong Sell: {strong_sell}', color='red').props('outline')
            
            # Visual breakdown using progress bars
            if total > 0:
                with ui.column().classes('w-full gap-1'):
                    for label, value, color in [
                        ('Strong Buy', strong_buy, 'positive'),
                        ('Buy', buy, 'teal'),
                        ('Hold', hold, 'grey'),
                        ('Sell', sell, 'warning'),
                        ('Strong Sell', strong_sell, 'negative'),
                    ]:
                        pct = value / total * 100 if total > 0 else 0
                        with ui.row().classes('w-full items-center gap-2'):
                            ui.label(f'{label}').classes('w-24 text-sm')
                            ui.linear_progress(value=pct/100, show_value=False, color=color).classes('flex-1')
                            ui.label(f'{pct:.0f}%').classes('w-12 text-sm text-right')
            
            # Calculate consensus
            if total > 0:
                buy_pct = (strong_buy + buy) / total * 100
                sell_pct = (strong_sell + sell) / total * 100
                hold_pct = hold / total * 100
                
                consensus = 'BUY' if buy_pct > sell_pct and buy_pct > hold_pct else \
                           'SELL' if sell_pct > buy_pct and sell_pct > hold_pct else 'HOLD'
                
                consensus_color = 'green' if consensus == 'BUY' else 'red' if consensus == 'SELL' else 'grey'
                
                ui.separator().classes('my-2')
                with ui.row().classes('items-center gap-2'):
                    ui.label('Consensus:').classes('font-semibold')
                    ui.badge(consensus, color=consensus_color)
                    ui.label(f'({total} analysts)').classes('text-sm').style('color: #a0aec0;')
            
            # Historical data table
            if len(data) > 1:
                ui.separator().classes('my-2')
                ui.label('Recent History').classes('text-sm font-semibold mb-1')
                
                history_columns = [
                    {'name': 'period', 'label': 'Period', 'field': 'period'},
                    {'name': 'strongBuy', 'label': 'SB', 'field': 'strongBuy'},
                    {'name': 'buy', 'label': 'Buy', 'field': 'buy'},
                    {'name': 'hold', 'label': 'Hold', 'field': 'hold'},
                    {'name': 'sell', 'label': 'Sell', 'field': 'sell'},
                    {'name': 'strongSell', 'label': 'SS', 'field': 'strongSell'},
                ]
                
                ui.table(columns=history_columns, rows=data[:6], row_key='period').classes('w-full').props('dense')
    
    def _render_fmp_section(self, grades_data: Optional[List[Dict[str, Any]]], 
                           target_data: Optional[Dict[str, Any]],
                           consensus_data: Optional[Dict[str, Any]],
                           symbol: str):
        """Render FMP ratings section."""
        with ui.card().classes('min-w-[400px] flex-1'):
            with ui.row().classes('items-center gap-2 mb-2'):
                ui.icon('trending_up', size='sm', color='purple')
                ui.label('FMP Analyst Grades & Price Targets').classes('text-lg font-semibold')
            
            if not self._fmp_api_key:
                ui.label('API key not configured').classes('italic').style('color: #a0aec0;')
                return
            
            # Price Target Consensus section
            if target_data:
                ui.label('Price Target Consensus').classes('text-md font-semibold mb-2 mt-2')
                
                target_high = target_data.get('targetHigh', 0)
                target_low = target_data.get('targetLow', 0)
                target_consensus = target_data.get('targetConsensus', 0)
                target_median = target_data.get('targetMedian', 0)
                
                with ui.row().classes('gap-4 flex-wrap mb-3'):
                    with ui.column().classes('items-center'):
                        ui.label('High').classes('text-xs').style('color: #a0aec0;')
                        ui.label(f'${target_high:.2f}').classes('text-lg font-bold').style('color: #00d4aa;')
                    with ui.column().classes('items-center'):
                        ui.label('Consensus').classes('text-xs').style('color: #a0aec0;')
                        ui.label(f'${target_consensus:.2f}').classes('text-lg font-bold').style('color: #74c0fc;')
                    with ui.column().classes('items-center'):
                        ui.label('Median').classes('text-xs').style('color: #a0aec0;')
                        ui.label(f'${target_median:.2f}').classes('text-lg font-bold').style('color: #b197fc;')
                    with ui.column().classes('items-center'):
                        ui.label('Low').classes('text-xs').style('color: #a0aec0;')
                        ui.label(f'${target_low:.2f}').classes('text-lg font-bold').style('color: #ff6b6b;')
            else:
                ui.label('Price target data not available').classes('italic text-sm').style('color: #a0aec0;')
            
            ui.separator().classes('my-2')
            
            # Grades Consensus section
            if consensus_data:
                ui.label('Grades Consensus').classes('text-md font-semibold mb-2')
                
                strong_buy = consensus_data.get('strongBuy', 0)
                buy = consensus_data.get('buy', 0)
                hold = consensus_data.get('hold', 0)
                sell = consensus_data.get('sell', 0)
                strong_sell = consensus_data.get('strongSell', 0)
                consensus = consensus_data.get('consensus', 'N/A')
                
                # Consensus badge
                consensus_color = 'green' if 'buy' in consensus.lower() else \
                                 'red' if 'sell' in consensus.lower() else 'grey'
                
                with ui.row().classes('items-center gap-2 mb-2'):
                    ui.label('Consensus:').classes('font-semibold')
                    ui.badge(consensus, color=consensus_color)
                
                # Ratings breakdown
                with ui.row().classes('gap-2 flex-wrap mb-3'):
                    ui.badge(f'Strong Buy: {strong_buy}', color='green').props('outline')
                    ui.badge(f'Buy: {buy}', color='teal').props('outline')
                    ui.badge(f'Hold: {hold}', color='grey').props('outline')
                    ui.badge(f'Sell: {sell}', color='orange').props('outline')
                    ui.badge(f'Strong Sell: {strong_sell}', color='red').props('outline')
            
            ui.separator().classes('my-2')
            
            # Recent Analyst Grades section
            if grades_data:
                ui.label('Recent Analyst Actions').classes('text-md font-semibold mb-2')
                
                grades_columns = [
                    {'name': 'date', 'label': 'Date', 'field': 'date', 'align': 'left'},
                    {'name': 'gradingCompany', 'label': 'Firm', 'field': 'gradingCompany', 'align': 'left'},
                    {'name': 'previousGrade', 'label': 'Previous', 'field': 'previousGrade', 'align': 'center'},
                    {'name': 'newGrade', 'label': 'New Grade', 'field': 'newGrade', 'align': 'center'},
                ]
                
                # Format rows
                formatted_grades = []
                for grade in grades_data[:10]:
                    formatted_grades.append({
                        'date': grade.get('date', '')[:10] if grade.get('date') else '',
                        'gradingCompany': grade.get('gradingCompany', 'Unknown'),
                        'previousGrade': grade.get('previousGrade', '-'),
                        'newGrade': grade.get('newGrade', '-'),
                    })
                
                grades_table = ui.table(
                    columns=grades_columns, 
                    rows=formatted_grades, 
                    row_key='date'
                ).classes('w-full').props('dense')
                
                # Add slot for grade coloring
                grades_table.add_slot('body-cell-newGrade', '''
                    <q-td :props="props">
                        <q-badge :color="props.value.toLowerCase().includes('buy') ? 'positive' : props.value.toLowerCase().includes('sell') ? 'negative' : props.value.toLowerCase().includes('hold') || props.value.toLowerCase().includes('neutral') ? 'grey' : 'blue'">
                            {{ props.value }}
                        </q-badge>
                    </q-td>
                ''')
            else:
                ui.label('No recent analyst grades available').classes('italic text-sm').style('color: #a0aec0;')


def content():
    """Render the Tools page with tabbed layout."""
    with ui.tabs().classes('w-full') as tabs:
        fmp_senate_tab = ui.tab('FMP Senate Trade', icon='account_balance')
        analyst_ratings_tab = ui.tab('Analyst Ratings', icon='analytics')
    
    with ui.tab_panels(tabs, value=fmp_senate_tab).classes('w-full'):
        with ui.tab_panel(fmp_senate_tab):
            FMPSenateTradeTab()
        
        with ui.tab_panel(analyst_ratings_tab):
            AnalystRatingsTab()
