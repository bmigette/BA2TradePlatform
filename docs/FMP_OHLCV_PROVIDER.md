# FMP OHLCV Provider - Implementation Complete

## Summary

Successfully implemented the Financial Modeling Prep (FMP) OHLCV data provider with full integration into the BA2 Trade Platform.

## Features

### Supported Intervals
- **Intraday**: 1min, 5min, 15min, 30min, 1hour, 4hour
- **Daily**: 1day (daily historical data)

### API Endpoints Used
1. **Daily Data**: `/api/v3/historical-price-full/{symbol}`
   - Returns complete daily historical data
   - Supports date range filtering (`from` and `to` parameters)
   
2. **Intraday Data**: `/api/v3/historical-chart/{interval}/{symbol}`
   - Returns intraday data at specified intervals
   - Note: Free tier has limited historical intraday access (typically last 5 days)

### Key Capabilities
- ✅ **Caching**: Automatic caching of OHLCV data with CSV-based storage
- ✅ **Timezone Aware**: All dates are UTC timezone-aware (compatible with base class)
- ✅ **Multiple Intervals**: Supports both intraday and daily data
- ✅ **Date Flexibility**: Accepts both `start_date/end_date` and `lookback_days` parameters
- ✅ **Fallback Support**: Can be used as fallback provider in TradingAgents
- ✅ **Logging**: Centralized logging via base class `@log_provider_call` decorator

## Implementation Details

### File Structure
```
ba2_trade_platform/
├── modules/
│   └── dataproviders/
│       └── ohlcv/
│           └── FMPOHLCVProvider.py  (NEW - 290 lines)
└── modules/
    └── dataproviders/
        └── __init__.py  (UPDATED - added FMP to registry)
```

### Architecture Pattern
Follows the refactored base class pattern:
- **Base Class**: `MarketDataProviderInterface.get_ohlcv_data_formatted()`
  - Handles date normalization
  - Manages caching
  - Provides formatted output (dict/markdown/both)
  - Centralizes logging

- **Provider Implementation**: `FMPOHLCVProvider._get_ohlcv_data_impl()`
  - Only implements FMP-specific API calls
  - Returns timezone-aware pandas DataFrame
  - No duplicate logic

### Code Highlights

#### Timeframe Mapping
```python
TIMEFRAME_MAP = {
    "1m": "1min", "1min": "1min",
    "5m": "5min", "5min": "5min",
    "15m": "15min", "15min": "15min",
    "30m": "30min", "30min": "30min",
    "1h": "1hour", "1hour": "1hour",
    "4h": "4hour", "4hour": "4hour",
    "1d": "daily", "1day": "daily",
}
```

#### Timezone-Aware Dates
```python
# Convert Date to datetime with UTC timezone
df['Date'] = pd.to_datetime(df['Date'], utc=True)
```

This ensures compatibility with the base class filtering logic, which uses timezone-aware datetime objects.

## Integration

### 1. Provider Registry
Added to `ba2_trade_platform/modules/dataproviders/__init__.py`:
```python
OHLCV_PROVIDERS: Dict[str, Type[DataProviderInterface]] = {
    "yfinance": YFinanceDataProvider,
    "alphavantage": AlphaVantageOHLCVProvider,
    "alpaca": AlpacaOHLCVProvider,
    "fmp": FMPOHLCVProvider,  # NEW
}
```

### 2. TradingAgents Settings
Updated `ba2_trade_platform/modules/experts/TradingAgents.py`:
```python
"vendor_stock_data": {
    "type": "list", "required": True, "default": ["yfinance"],
    "description": "Data vendor(s) for OHLCV stock price data",
    "valid_values": ["yfinance", "alpaca", "alpha_vantage", "fmp"],  # Added "fmp"
    "multiple": True,
    "tooltip": "...FMP provides daily and intraday data (1min to 4hour intervals)."
},
```

### 3. Configuration
Requires `FMP_API_KEY` in AppSetting table:
- Set via Settings page in web UI
- Or directly in database: `INSERT INTO AppSetting (key, value_str) VALUES ('FMP_API_KEY', 'your-key-here');`

## Usage Examples

### Basic Usage
```python
from ba2_trade_platform.modules.dataproviders import FMPOHLCVProvider
from datetime import datetime, timedelta

# Initialize provider
provider = FMPOHLCVProvider()

# Get daily data
result = provider.get_ohlcv_data_formatted(
    symbol="AAPL",
    lookback_days=30,
    interval="1d",
    format_type="both"  # Returns dict and markdown
)

print(result["text"])  # Markdown table
print(result["data"])  # Dict with data points
```

### In TradingAgents
```python
# In expert instance settings:
{
    "vendor_stock_data": ["fmp", "yfinance"],  # FMP first, YFinance as fallback
    ...
}
```

## Test Results

### Test Suite (`test_fmp_ohlcv.py`)
```
============================================================
Test Summary
============================================================
Daily Data           [PASS]  ✓ Successfully fetches daily historical data
Intraday Data        [PASS]  ✓ Successfully fetches 15min intraday data  
Lookback Days        [PASS]  ✓ lookback_days parameter works correctly
Caching              [PASS]  ✓ Cache reduces fetch time from 0.97s to 0.03s

Pass Rate: 4/4 (100%)  🎉
```

### Sample Output
```
[OK] Retrieved 23 daily bars
  Symbol: AAPL
  Interval: 1d
  Period: 2025-09-09 to 2025-10-09

  First bar: {'date': '2025-09-09T00:00:00+00:00', 'open': 220.54, ...}
  Last bar: {'date': '2025-10-09T00:00:00+00:00', 'open': 227.30, ...}
```

## API Limitations

### Free Tier
- **Daily Data**: Full historical access (15+ years)
- **Intraday Data**: Last ~5 days only
- **Rate Limits**: 250 requests/day

### Premium Tiers
- Extended intraday history
- Higher rate limits
- Real-time data access

## Benefits Over Other Providers

### vs YFinance
- ✅ More reliable API (no scraping)
- ✅ Consistent data format
- ✅ Official API with support
- ❌ Requires API key

### vs Alpaca
- ✅ More historical data available
- ✅ Better for backtesting (15+ years daily data)
- ❌ Free tier has limited intraday

### vs AlphaVantage
- ✅ Simpler API responses
- ✅ Better documentation
- ✅ More generous free tier (250 vs 25 requests/day)

## Files Modified

1. **Created**:
   - `ba2_trade_platform/modules/dataproviders/ohlcv/FMPOHLCVProvider.py` (290 lines)
   - `test_fmp_ohlcv.py` (235 lines)
   - `docs/FMP_OHLCV_PROVIDER.md` (this file)

2. **Modified**:
   - `ba2_trade_platform/modules/dataproviders/__init__.py` (+3 lines)
     * Added FMP import
     * Added to OHLCV_PROVIDERS registry
     * Added to __all__ exports
   
   - `ba2_trade_platform/modules/experts/TradingAgents.py` (+1 line)
     * Added "fmp" to vendor_stock_data valid_values
     * Updated tooltip with FMP description

## Next Steps (Optional)

### Enhancements
1. **Add more intervals**: Support weekly/monthly data via different endpoints
2. **Real-time quotes**: Integrate FMP real-time quote API for live prices
3. **Extended intraday**: Add support for premium tier extended intraday history
4. **Batch requests**: Optimize multiple symbol requests with batch API calls

### Integration
1. **Expert Settings UI**: FMP now automatically appears in TradingAgents provider dropdown
2. **Fallback Chain**: Configure as primary or fallback provider in expert settings
3. **Documentation**: Add to user guide for provider selection recommendations

## Troubleshooting

### No Data Returned
- **Check API Key**: Verify FMP_API_KEY is set in AppSetting table
- **Check Symbol**: Ensure symbol is valid US stock ticker
- **Check Date Range**: FMP free tier has limited intraday history

### Timezone Errors
- If you see "Invalid comparison between dtype=datetime64[ns] and datetime":
  - Clear cache: `Remove-Item "C:\Users\...\cache\FMPOHLCVProvider\*"`
  - Dates are now timezone-aware (UTC) - old cached data was tz-naive

### Rate Limits
- Free tier: 250 requests/day
- Cache helps reduce API calls
- Consider premium tier for heavy usage

## Related Documentation
- [OHLCV Provider Refactoring](./OHLCV_PROVIDER_REFACTORING_COMPLETE.md)
- [FMP API Documentation](https://site.financialmodelingprep.com/developer/docs)
- [FMPRating Expert](./FMP_RATING_EXPERT.md) - Uses same API key
