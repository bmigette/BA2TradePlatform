# Data Provider Refactoring - Phase 1: Foundation

## Overview
This document outlines Phase 1 of the data provider architecture refactoring, establishing interfaces and directory structure.

## Phase 1 Goals
1. Add new API key settings (Alpaca, FMP)
2. Create core/interfaces directory structure
3. Define all data provider interfaces
4. Create provider module structure
5. Document implementation patterns

## 1. New App Settings

Add the following global settings to support new providers:

```python
# In ba2_trade_platform/core/models.py - AppSetting model already exists

# New settings to add via UI or database:
- alpaca_market_api_key (str): Alpaca Markets API key
- alpaca_market_api_secret (str): Alpaca Markets API secret  
- fmp_api_key (str): Financial Modeling Prep API key
```

## 2. Directory Structure

```
ba2_trade_platform/
├── core/
│   ├── interfaces/
│   │   ├── __init__.py
│   │   ├── AccountInterface.py (moved from core/)
│   │   ├── MarketExpertInterface.py (moved from core/)
│   │   ├── ExtendableSettingsInterface.py (moved from core/)
│   │   ├── DataProviderInterface.py (new - base for all providers)
│   │   ├── MarketIndicatorsInterface.py (new)
│   │   ├── CompanyFundamentalsOverviewInterface.py (new)
│   │   ├── CompanyFundamentalsDetailsInterface.py (new)
│   │   ├── MarketNewsInterface.py (new)
│   │   ├── MacroEconomicsInterface.py (new)
│   │   └── CompanyInsiderInterface.py (new)
│   └── ...
└── modules/
    ├── dataproviders/
    │   ├── __init__.py
    │   ├── indicators/
    │   │   ├── __init__.py
    │   │   ├── AlphaVantageIndicatorsProvider.py
    │   │   └── YFinanceIndicatorsProvider.py
    │   ├── fundamentals/
    │   │   ├── __init__.py
    │   │   ├── overview/
    │   │   │   ├── __init__.py
    │   │   │   ├── AlphaVantageFundamentalsOverviewProvider.py
    │   │   │   └── OpenAIFundamentalsOverviewProvider.py
    │   │   └── details/
    │   │       ├── __init__.py
    │   │       ├── AlphaVantageFundamentalsDetailsProvider.py
    │   │       ├── YFinanceFundamentalsDetailsProvider.py
    │   │       └── SimFinFundamentalsDetailsProvider.py
    │   ├── news/
    │   │   ├── __init__.py
    │   │   ├── AlpacaNewsProvider.py (new)
    │   │   ├── AlphaVantageNewsProvider.py
    │   │   ├── OpenAINewsProvider.py
    │   │   ├── GoogleNewsProvider.py
    │   │   ├── FinnhubNewsProvider.py
    │   │   └── RedditNewsProvider.py
    │   ├── macro/
    │   │   ├── __init__.py
    │   │   └── FREDMacroProvider.py
    │   └── insider/
    │       ├── __init__.py
    │       ├── AlphaVantageInsiderProvider.py
    │       ├── YFinanceInsiderProvider.py
    │       └── FinnhubInsiderProvider.py
    └── ...
```

## 3. Interface Definitions

### Design Principles

**Date Range Parameters:**
All interfaces follow a consistent pattern for date range queries:
- **`end_date`**: Always required - the end date of the data range (inclusive)
- **`start_date`**: Optional - explicit start date (mutually exclusive with `lookback_days`)
- **`lookback_days`** or **`lookback_periods`**: Optional - number of days/periods to look back from `end_date` (mutually exclusive with `start_date`)

**Usage Examples:**
```python
# Using explicit date range
provider.get_company_news(
    symbol="AAPL",
    end_date=datetime(2025, 10, 8),
    start_date=datetime(2025, 9, 1),
    format_type="markdown"
)

# Using lookback pattern (more convenient)
provider.get_company_news(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=30,  # Last 30 days
    format_type="markdown"
)

# For financial statements (period-based lookback)
provider.get_balance_sheet(
    symbol="AAPL",
    frequency="quarterly",
    end_date=datetime.now(),
    lookback_periods=4,  # Last 4 quarters
    format_type="dict"
)
```

**Point-in-Time Queries:**
Some methods like `get_fundamentals_overview()` use `as_of_date` instead of ranges when only the most recent data is relevant.

### 3.1 Base DataProviderInterface

```python
from abc import ABC, abstractmethod
from typing import Dict, Any, Literal
from datetime import datetime

class DataProviderInterface(ABC):
    """Base interface for all data providers."""
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Return the provider name (e.g., 'alpaca', 'yfinance')."""
        pass
    
    @abstractmethod
    def get_supported_features(self) -> list[str]:
        """Return list of supported features."""
        pass
    
    @abstractmethod
    def validate_config(self) -> bool:
        """Validate provider configuration (API keys, etc.)."""
        pass
    
    def format_response(
        self, 
        data: Any, 
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Format response in requested format.
        
        Args:
            data: Raw data to format
            format_type: Either "dict" for structured data or "markdown" for LLM consumption
            
        Returns:
            Formatted data in requested format
        """
        if format_type == "dict":
            return self._format_as_dict(data)
        else:
            return self._format_as_markdown(data)
    
    @abstractmethod
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format data as structured dictionary."""
        pass
    
    @abstractmethod
    def _format_as_markdown(self, data: Any) -> str:
        """Format data as markdown for LLM consumption."""
        pass
```

### 3.2 MarketIndicatorsInterface

```python
from abc import abstractmethod
from typing import Annotated, Optional
from datetime import datetime

class MarketIndicatorsInterface(DataProviderInterface):
    """Interface for technical indicator providers."""
    
    @abstractmethod
    def get_indicator(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        indicator: Annotated[str, "Indicator name (e.g., 'rsi', 'macd', 'close_50_sma')"],
        end_date: Annotated[datetime, "End date for data (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for data (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        interval: Annotated[str, "Data interval (1d, 1h, etc.)"] = "1d",
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get technical indicator data.
        
        Args:
            symbol: Stock ticker symbol
            indicator: Indicator name
            end_date: End date for data (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Number of days to look back from end_date (use either this OR start_date, not both)
            interval: Data interval
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "indicator": str,
                "interval": str,
                "start_date": str,
                "end_date": str,
                "data": [{
                    "date": str,
                    "value": float
                }],
                "metadata": {
                    "description": str,
                    "interpretation": str
                }
            }
            If format_type='markdown': Formatted markdown string
        """
        pass
    
    @abstractmethod
    def get_supported_indicators(self) -> list[str]:
        """Return list of supported indicator names."""
        pass
```

### 3.3 CompanyFundamentalsOverviewInterface

```python
class CompanyFundamentalsOverviewInterface(DataProviderInterface):
    """Interface for company fundamentals overview (high-level metrics)."""
    
    @abstractmethod
    def get_fundamentals_overview(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        as_of_date: Annotated[datetime, "Date for fundamentals (uses most recent data as of this date)"],
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get company fundamentals overview.
        
        Note: This is a point-in-time query (as_of_date), not a range query.
        Returns the most recent fundamentals data available as of the specified date.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "company_name": str,
                "as_of_date": str,
                "data_date": str,  # Actual date of the fundamentals data
                "metrics": {
                    "market_cap": float,
                    "pe_ratio": float,
                    "peg_ratio": float,
                    "eps": float,
                    "dividend_yield": float,
                    "beta": float,
                    "52_week_high": float,
                    "52_week_low": float,
                    # ... other key metrics
                }
            }
            If format_type='markdown': Formatted markdown table
        """
        pass
```

### 3.4 CompanyFundamentalsDetailsInterface

```python
class CompanyFundamentalsDetailsInterface(DataProviderInterface):
    """Interface for detailed company financials (statements)."""
    
    @abstractmethod
    def get_balance_sheet(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get balance sheet(s).
        
        Args:
            symbol: Stock ticker symbol
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_periods, but not both.
        
        Returns multiple balance sheets within the date range or period count.
        """
        pass
    
    @abstractmethod
    def get_income_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get income statement(s).
        
        Args:
            symbol: Stock ticker symbol
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_periods, but not both.
        
        Returns multiple income statements within the date range or period count.
        """
        pass
    
    @abstractmethod
    def get_cashflow_statement(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        frequency: Annotated[Literal["annual", "quarterly"], "Reporting frequency"],
        end_date: Annotated[datetime, "End date for statement range (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for statement range (mutually exclusive with lookback_periods)"] = None,
        lookback_periods: Annotated[Optional[int], "Number of periods to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get cash flow statement(s).
        
        Args:
            symbol: Stock ticker symbol
            frequency: Annual or quarterly reporting
            end_date: End date (inclusive) - gets most recent statement as of this date
            start_date: Start date (use either this OR lookback_periods, not both)
            lookback_periods: Number of periods to look back (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_periods, but not both.
        
        Returns multiple cash flow statements within the date range or period count.
        """
        pass
```

### 3.5 MarketNewsInterface

```python
class MarketNewsInterface(DataProviderInterface):
    """Interface for market news providers."""
    
    @abstractmethod
    def get_company_news(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a company.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "start_date": str,
                "end_date": str,
                "article_count": int,
                "articles": [{
                    "title": str,
                    "summary": str,
                    "source": str,
                    "published_at": str,
                    "url": str,
                    "sentiment": str (optional)
                }]
            }
            If format_type='markdown': Formatted markdown
        """
        pass
    
    @abstractmethod
    def get_global_news(
        self,
        end_date: Annotated[datetime, "End date for news (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for news (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        limit: Annotated[int, "Maximum number of articles"] = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            limit: Maximum number of articles to return
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        """
        pass
```

### 3.6 MacroEconomicsInterface

```python
class MacroEconomicsInterface(DataProviderInterface):
    """Interface for macroeconomic data providers."""
    
    @abstractmethod
    def get_economic_indicators(
        self,
        end_date: Annotated[datetime, "End date for indicators (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for indicators (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        indicators: Annotated[Optional[list[str]], "List of indicator names (e.g., ['GDP', 'UNRATE', 'CPIAUCSL'])"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get economic indicators (GDP, unemployment, inflation, etc.).
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            indicators: List of indicator names, or None for all available
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        """
        pass
    
    @abstractmethod
    def get_yield_curve(
        self,
        end_date: Annotated[datetime, "End date for yield curve data (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get Treasury yield curve data.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        If only end_date is provided (both start_date and lookback_days are None),
        returns single most recent yield curve as of end_date.
        """
        pass
    
    @abstractmethod
    def get_fed_calendar(
        self,
        end_date: Annotated[datetime, "End date for Fed events (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for Fed events (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get Federal Reserve calendar and meeting minutes.
        
        Args:
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        """
        pass
```

### 3.7 CompanyInsiderInterface

```python
class CompanyInsiderInterface(DataProviderInterface):
    """Interface for insider trading data providers."""
    
    @abstractmethod
    def get_insider_transactions(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for transactions (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date for transactions (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get insider trading transactions.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns:
            If format_type='dict': {
                "symbol": str,
                "start_date": str,
                "end_date": str,
                "transaction_count": int,
                "transactions": [{
                    "filing_date": str,
                    "transaction_date": str,
                    "insider_name": str,
                    "title": str,
                    "transaction_type": str,  # "purchase", "sale"
                    "shares": float,
                    "price": float,
                    "value": float
                }]
            }
            If format_type='markdown': Formatted markdown
        """
        pass
    
    @abstractmethod
    def get_insider_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for sentiment calculation (inclusive)"],
        start_date: Annotated[Optional[datetime], "Start date (mutually exclusive with lookback_days)"] = None,
        lookback_days: Annotated[Optional[int], "Days to look back from end_date (mutually exclusive with start_date)"] = None,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get aggregated insider sentiment metrics.
        
        Args:
            symbol: Stock ticker symbol
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date (use either this OR start_date, not both)
            format_type: Output format
        
        Note: Must provide either start_date or lookback_days, but not both.
        
        Returns metrics like MSPR (Monthly Share Purchase Ratio), net changes, etc.
        """
        pass
```

## 4. Implementation Pattern

Each provider should follow this pattern:

```python
class AlpacaNewsProvider(MarketNewsInterface):
    """Alpaca Markets news provider using official API."""
    
    def __init__(self):
        """Initialize with API credentials from app settings."""
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import AppSetting
        from sqlmodel import Session, select
        
        engine = get_db()
        with Session(engine.bind) as session:
            api_key_setting = session.exec(
                select(AppSetting).where(AppSetting.key == "alpaca_market_api_key")
            ).first()
            api_secret_setting = session.exec(
                select(AppSetting).where(AppSetting.key == "alpaca_market_api_secret")
            ).first()
            
            if not api_key_setting or not api_secret_setting:
                raise ValueError("Alpaca API credentials not configured in app settings")
            
            self.api_key = api_key_setting.value_str
            self.api_secret = api_secret_setting.value_str
    
    def get_provider_name(self) -> str:
        return "alpaca"
    
    def get_supported_features(self) -> list[str]:
        return ["company_news", "global_news"]
    
    def validate_config(self) -> bool:
        return bool(self.api_key and self.api_secret)
    
    def get_company_news(
        self, 
        symbol: str, 
        start_date: datetime, 
        end_date: datetime,
        limit: int = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        # Implementation using Alpaca API
        # https://docs.alpaca.markets/reference/news-3
        raw_data = self._fetch_from_alpaca_api(symbol, start_date, end_date, limit)
        return self.format_response(raw_data, format_type)
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        # Convert Alpaca response to standard dict format
        pass
    
    def _format_as_markdown(self, data: Any) -> str:
        # Convert to markdown for LLM
        pass
```

## 5. Provider Registration

Create a provider registry in `modules/dataproviders/__init__.py`:

```python
from typing import Type, Dict
from ba2_trade_platform.core.interfaces import (
    MarketIndicatorsInterface,
    CompanyFundamentalsOverviewInterface,
    CompanyFundamentalsDetailsInterface,
    MarketNewsInterface,
    MacroEconomicsInterface,
    CompanyInsiderInterface
)

# Registry maps provider name to provider class
INDICATORS_PROVIDERS: Dict[str, Type[MarketIndicatorsInterface]] = {
    "alphavantage": AlphaVantageIndicatorsProvider,
    "yfinance": YFinanceIndicatorsProvider,
}

FUNDAMENTALS_OVERVIEW_PROVIDERS: Dict[str, Type[CompanyFundamentalsOverviewInterface]] = {
    "alphavantage": AlphaVantageFundamentalsOverviewProvider,
    "openai": OpenAIFundamentalsOverviewProvider,
}

FUNDAMENTALS_DETAILS_PROVIDERS: Dict[str, Type[CompanyFundamentalsDetailsInterface]] = {
    "alphavantage": AlphaVantageFundamentalsDetailsProvider,
    "yfinance": YFinanceFundamentalsDetailsProvider,
    "simfin": SimFinFundamentalsDetailsProvider,
}

NEWS_PROVIDERS: Dict[str, Type[MarketNewsInterface]] = {
    "alpaca": AlpacaNewsProvider,
    "alphavantage": AlphaVantageNewsProvider,
    "openai": OpenAINewsProvider,
    "google": GoogleNewsProvider,
    "finnhub": FinnhubNewsProvider,
    "reddit": RedditNewsProvider,
}

MACRO_PROVIDERS: Dict[str, Type[MacroEconomicsInterface]] = {
    "fred": FREDMacroProvider,
}

INSIDER_PROVIDERS: Dict[str, Type[CompanyInsiderInterface]] = {
    "alphavantage": AlphaVantageInsiderProvider,
    "yfinance": YFinanceInsiderProvider,
    "finnhub": FinnhubInsiderProvider,
}

def get_provider(category: str, provider_name: str):
    """Get provider instance by category and name."""
    registries = {
        "indicators": INDICATORS_PROVIDERS,
        "fundamentals_overview": FUNDAMENTALS_OVERVIEW_PROVIDERS,
        "fundamentals_details": FUNDAMENTALS_DETAILS_PROVIDERS,
        "news": NEWS_PROVIDERS,
        "macro": MACRO_PROVIDERS,
        "insider": INSIDER_PROVIDERS,
    }
    
    if category not in registries:
        raise ValueError(f"Unknown provider category: {category}")
    
    provider_class = registries[category].get(provider_name)
    if not provider_class:
        raise ValueError(f"Provider '{provider_name}' not found in category '{category}'")
    
    return provider_class()
```

## 6. Next Steps (Phase 2+)

1. Implement each provider class
2. Update TradingAgents interface.py to use new providers
3. Add provider selection to expert settings UI
4. Create migration guide for existing code
5. Add comprehensive tests

## 7. Benefits

✅ **Consistent API**: All providers implement same interfaces  
✅ **Easy Testing**: Mock providers for unit tests  
✅ **Flexible Configuration**: Switch providers via settings  
✅ **Type Safety**: Full type hints with Python 3.11+  
✅ **Dual Format**: Support both dict and markdown outputs  
✅ **Extensible**: Easy to add new providers  
✅ **Clean Separation**: Data fetching separated from business logic
