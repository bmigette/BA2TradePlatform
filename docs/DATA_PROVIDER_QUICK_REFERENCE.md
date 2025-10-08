# Data Provider Quick Reference

## Using Interfaces

### Import Interfaces
```python
from ba2_trade_platform.core.interfaces import (
    DataProviderInterface,
    MarketIndicatorsInterface,
    CompanyFundamentalsOverviewInterface,
    CompanyFundamentalsDetailsInterface,
    MarketNewsInterface,
    MacroEconomicsInterface,
    CompanyInsiderInterface
)
```

### Get Provider Instance
```python
from ba2_trade_platform.modules.dataproviders import get_provider, list_providers

# Get a specific provider
news_provider = get_provider("news", "alpaca")

# List available providers
all_providers = list_providers()  # Returns all categories
news_providers = list_providers("news")  # Returns: {'news': ['alpaca', 'alphavantage', ...]}
```

## Date Range Patterns

### Pattern 1: Explicit Date Range
```python
from datetime import datetime

result = provider.get_company_news(
    symbol="AAPL",
    end_date=datetime(2025, 10, 8),
    start_date=datetime(2025, 9, 1),
    format_type="markdown"
)
```

### Pattern 2: Lookback from End Date
```python
from datetime import datetime

result = provider.get_company_news(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=30,  # Last 30 days
    format_type="dict"
)
```

### Pattern 3: Period-based Lookback (Financial Statements)
```python
from datetime import datetime

result = provider.get_balance_sheet(
    symbol="AAPL",
    frequency="quarterly",
    end_date=datetime.now(),
    lookback_periods=4,  # Last 4 quarters
    format_type="dict"
)
```

### Pattern 4: Point-in-Time (Fundamentals Overview)
```python
from datetime import datetime

result = provider.get_fundamentals_overview(
    symbol="AAPL",
    as_of_date=datetime.now(),  # Most recent data as of this date
    format_type="markdown"
)
```

## Output Formats

### Dict Format (Structured Data)
```python
result = provider.get_company_news(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=7,
    format_type="dict"
)

# Returns:
# {
#     "symbol": "AAPL",
#     "start_date": "2025-10-01T00:00:00",
#     "end_date": "2025-10-08T00:00:00",
#     "article_count": 15,
#     "articles": [...]
# }
```

### Markdown Format (LLM-Ready)
```python
result = provider.get_company_news(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=7,
    format_type="markdown"
)

# Returns formatted markdown string suitable for LLM consumption
```

## Implementing a New Provider

### Step 1: Create Provider Class
```python
# File: ba2_trade_platform/modules/dataproviders/news/AlpacaNewsProvider.py

from datetime import datetime
from typing import Dict, Any, Literal, Optional
from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AppSetting
from sqlmodel import Session, select

class AlpacaNewsProvider(MarketNewsInterface):
    """Alpaca Markets news provider using official API."""
    
    def __init__(self):
        """Initialize with API credentials from app settings."""
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
        end_date: datetime, 
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Cannot specify both start_date and lookback_days")
        if not start_date and not lookback_days:
            raise ValueError("Must specify either start_date or lookback_days")
        
        # Calculate start_date if using lookback
        if lookback_days:
            from datetime import timedelta
            start_date = end_date - timedelta(days=lookback_days)
        
        # Fetch data from Alpaca API
        raw_data = self._fetch_from_alpaca_api(symbol, start_date, end_date, limit)
        
        # Format response
        return self.format_response(raw_data, format_type)
    
    def _fetch_from_alpaca_api(self, symbol, start_date, end_date, limit):
        # TODO: Implement Alpaca API call
        # https://docs.alpaca.markets/reference/news-3
        pass
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        # Convert Alpaca response to standard dict format
        return {
            "symbol": data["symbol"],
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "article_count": len(data["articles"]),
            "articles": data["articles"]
        }
    
    def _format_as_markdown(self, data: Any) -> str:
        # Convert to markdown for LLM
        md = f"# News for {data['symbol']}\n\n"
        md += f"**Period**: {data['start_date']} to {data['end_date']}\n"
        md += f"**Articles**: {len(data['articles'])}\n\n"
        
        for article in data["articles"]:
            md += f"## {article['title']}\n"
            md += f"**Source**: {article['source']} | **Published**: {article['published_at']}\n\n"
            md += f"{article['summary']}\n\n"
        
        return md
    
    def get_global_news(self, end_date, start_date=None, lookback_days=None, limit=50, format_type="markdown"):
        # Similar implementation for global news
        pass
```

### Step 2: Register Provider
```python
# File: ba2_trade_platform/modules/dataproviders/news/__init__.py

from .AlpacaNewsProvider import AlpacaNewsProvider

__all__ = ["AlpacaNewsProvider"]
```

### Step 3: Add to Registry
```python
# File: ba2_trade_platform/modules/dataproviders/__init__.py

from .news.AlpacaNewsProvider import AlpacaNewsProvider

NEWS_PROVIDERS: Dict[str, Type[MarketNewsInterface]] = {
    "alpaca": AlpacaNewsProvider,
    # ... other providers
}
```

### Step 4: Use Provider
```python
from ba2_trade_platform.modules.dataproviders import get_provider
from datetime import datetime

news_provider = get_provider("news", "alpaca")
news = news_provider.get_company_news(
    symbol="AAPL",
    end_date=datetime.now(),
    lookback_days=7,
    format_type="markdown"
)
print(news)
```

## Provider Categories

| Category | Interface | Use Case |
|----------|-----------|----------|
| `indicators` | `MarketIndicatorsInterface` | Technical analysis (RSI, MACD, SMA, etc.) |
| `fundamentals_overview` | `CompanyFundamentalsOverviewInterface` | High-level metrics (P/E, market cap, EPS) |
| `fundamentals_details` | `CompanyFundamentalsDetailsInterface` | Financial statements (balance sheet, income, cashflow) |
| `news` | `MarketNewsInterface` | Company and market news |
| `macro` | `MacroEconomicsInterface` | Economic indicators, yield curves, Fed calendar |
| `insider` | `CompanyInsiderInterface` | Insider transactions and sentiment |

## Common Patterns

### Validate Date Parameters Helper
```python
def _validate_date_range(start_date, end_date, lookback_days):
    """Validate and calculate date range parameters."""
    if start_date and lookback_days:
        raise ValueError("Cannot specify both start_date and lookback_days")
    if not start_date and not lookback_days:
        raise ValueError("Must specify either start_date or lookback_days")
    
    if lookback_days:
        from datetime import timedelta
        start_date = end_date - timedelta(days=lookback_days)
    
    return start_date, end_date
```

### Error Handling
```python
try:
    provider = get_provider("news", "alpaca")
    news = provider.get_company_news("AAPL", end_date=datetime.now(), lookback_days=7)
except ValueError as e:
    print(f"Provider error: {e}")
except Exception as e:
    logger.error(f"Unexpected error: {e}", exc_info=True)
```

## Testing Providers

### Mock Provider for Tests
```python
from ba2_trade_platform.core.interfaces import MarketNewsInterface

class MockNewsProvider(MarketNewsInterface):
    def get_provider_name(self) -> str:
        return "mock"
    
    def get_supported_features(self) -> list[str]:
        return ["company_news", "global_news"]
    
    def validate_config(self) -> bool:
        return True
    
    def get_company_news(self, symbol, end_date, start_date=None, lookback_days=None, limit=50, format_type="markdown"):
        return {
            "symbol": symbol,
            "start_date": str(start_date or end_date),
            "end_date": str(end_date),
            "article_count": 0,
            "articles": []
        }
    
    def _format_as_dict(self, data):
        return data
    
    def _format_as_markdown(self, data):
        return f"# Mock News for {data['symbol']}"
```
