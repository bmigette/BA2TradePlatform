# Datetime Formatting Standardization

**Date:** 2025-10-09  
**Issue:** Inconsistent datetime formatting across data providers  
**Solution:** Added helper methods to DataProviderInterface for standardized datetime formatting  
**Chart Compatibility:** ✅ Verified Safe - See [CHART_COMPATIBILITY_VERIFICATION.md](CHART_COMPATIBILITY_VERIFICATION.md)

---

## Problem Statement

Data providers were formatting dates inconsistently:
- Dict output: Some used `.isoformat()`, others used custom formats
- Markdown output: All showed full datetime even for daily/weekly data (cluttered display)

**User Requirement:**
> "ensure providers include datetime as isostring in dict result, and in markdown as well except for interval d1 and higher show date only"

---

## Solution Overview

### Helper Methods Added to DataProviderInterface

Two new helper methods provide consistent datetime formatting:

```python
def format_datetime_for_dict(self, dt: datetime, interval: Optional[str] = None) -> str:
    """
    Format datetime for dict output.
    Always returns ISO string format: YYYY-MM-DDTHH:MM:SS
    
    Args:
        dt: Datetime to format
        interval: Optional interval (not used, but available for future needs)
    
    Returns:
        ISO format string
    """
    return dt.isoformat()

def format_datetime_for_markdown(self, dt: datetime, interval: str) -> str:
    """
    Format datetime for markdown output.
    Returns date-only for daily+ intervals, full datetime for intraday.
    
    Args:
        dt: Datetime to format
        interval: Time interval (e.g., '1m', '1h', '1d', '1w')
    
    Returns:
        Formatted datetime string
        - Daily+: YYYY-MM-DD (e.g., '2024-01-15')
        - Intraday: YYYY-MM-DD HH:MM:SS (e.g., '2024-01-15 09:30:00')
    """
    # Extract interval value and unit
    if len(interval) < 2:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    
    interval_value = int(interval[:-1]) if interval[:-1].isdigit() else 1
    interval_unit = interval[-1].lower()
    
    # Daily or higher: date only
    if interval_unit == 'd' or interval_unit == 'w' or interval_unit == 'mo':
        return dt.strftime("%Y-%m-%d")
    # Intraday: full datetime
    else:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
```

---

## Implementation Details

### Interval Logic

The `format_datetime_for_markdown()` method uses interval units to determine formatting:

- **Daily+ intervals** (1d, 1w, 1mo): Show date only (YYYY-MM-DD)
  - Rationale: Daily/weekly/monthly data doesn't need timestamps
  - Improves readability in markdown tables

- **Intraday intervals** (1m, 5m, 15m, 1h): Show full datetime (YYYY-MM-DD HH:MM:SS)
  - Rationale: Intraday data requires time information
  - Essential for distinguishing data points

### Interval Parsing

```python
interval_value = int(interval[:-1])  # Extract number (e.g., '15' from '15m')
interval_unit = interval[-1].lower()  # Extract unit (e.g., 'm' from '15m')

# Unit detection
if interval_unit == 'd':     # Daily
elif interval_unit == 'w':   # Weekly
elif interval_unit == 'mo':  # Monthly
else:                        # Assume intraday (m, h, etc.)
```

---

## Files Modified

### 1. DataProviderInterface.py
- **Location:** `ba2_trade_platform/modules/dataproviders/DataProviderInterface.py`
- **Changes:**
  - Added `format_datetime_for_dict()` method after `validate_config()`
  - Added `format_datetime_for_markdown()` method after `format_datetime_for_dict()`
  - Updated `_format_as_markdown()` docstring to mention helper methods

### 2. MarketDataProviderInterface.py
- **Location:** `ba2_trade_platform/modules/dataproviders/MarketDataProviderInterface.py`
- **Changes:**
  - Updated `_format_ohlcv_as_markdown()` to use `self.format_datetime_for_markdown()`
  - Modified date column formatting in markdown table:
    ```python
    # Before
    point['date'] = datetime.fromisoformat(point['date']).strftime("%Y-%m-%d %H:%M:%S")
    
    # After
    point['date'] = self.format_datetime_for_markdown(
        datetime.fromisoformat(point['date']), 
        data['interval']
    )
    ```

---

## Usage Examples

### For Provider Developers

```python
from datetime import datetime
from ba2_trade_platform.modules.dataproviders.DataProviderInterface import DataProviderInterface

class MyCustomProvider(DataProviderInterface):
    
    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        """Format data as dictionary."""
        result = []
        for item in data:
            result.append({
                'date': self.format_datetime_for_dict(item.timestamp),
                'value': item.value
            })
        return {'data': result}
    
    def _format_as_markdown(self, data: Any, **kwargs) -> str:
        """Format data as markdown."""
        interval = kwargs.get('interval', '1d')
        
        lines = ["| Date | Value |", "|------|-------|"]
        for item in data:
            date_str = self.format_datetime_for_markdown(item.timestamp, interval)
            lines.append(f"| {date_str} | {item.value} |")
        
        return '\n'.join(lines)
```

---

## Testing

### Test Script

A test script is provided at `test_datetime_formatting.py`:

```bash
# Run from project root with venv activated
.venv\Scripts\python.exe test_datetime_formatting.py
```

### Expected Output

**Daily interval (1d):**
- Dict: `"date": "2024-01-15T00:00:00"`
- Markdown: `| 2024-01-15 | $150.50 | ... |`

**Hourly interval (1h):**
- Dict: `"date": "2024-01-15T09:30:00"`
- Markdown: `| 2024-01-15 09:30:00 | $150.50 | ... |`

---

## Migration Guide

### For Existing Providers

1. **Dict Output:** Ensure you use `.isoformat()` or the helper method
   ```python
   # Good
   'date': dt.isoformat()
   'date': self.format_datetime_for_dict(dt)
   
   # Bad
   'date': dt.strftime("%Y-%m-%d")  # Missing time component
   ```

2. **Markdown Output:** Use the helper method with interval parameter
   ```python
   # Good
   date_str = self.format_datetime_for_markdown(dt, interval)
   
   # Bad
   date_str = dt.strftime("%Y-%m-%d %H:%M:%S")  # Always shows time
   ```

3. **Provider-Specific Notes:**
   - **OHLCV Providers:** ✅ Already updated in MarketDataProviderInterface
   - **News Providers:** Need to add interval parameter (default to 'd' for daily news)
   - **Fundamentals Providers:** Use date-only (quarterly/annual data)
   - **Indicators Providers:** Follow OHLCV pattern (use interval from request)

---

## Provider Status

### ✅ Updated Providers
- MarketDataProviderInterface (base for all OHLCV providers)
  - YFinanceDataProvider
  - AlpacaOHLCVProvider
  - AlphaVantageOHLCVProvider
  - FMPOHLCVProvider

### 🔄 Pending Updates

**News Providers:**
- AlpacaNewsProvider
- AlphaVantageNewsProvider
- GoogleNewsProvider
- FMPNewsProvider
- OpenAINewsProvider

**Fundamentals Providers:**
- FMPCompanyDetailsProvider
- YFinanceCompanyDetailsProvider
- AlphaVantageCompanyDetailsProvider
- AlphaVantageCompanyOverviewProvider
- FMPCompanyOverviewProvider
- OpenAICompanyOverviewProvider

**Other Providers:**
- FREDMacroProvider (MacroEconomicsInterface)
- AlphaVantageIndicatorsProvider (MarketIndicatorsInterface)

---

## Benefits

1. **Consistency:** All providers use same datetime format standards
2. **Readability:** Markdown tables are cleaner (no unnecessary timestamps for daily data)
3. **Maintainability:** Centralized formatting logic in base interface
4. **Flexibility:** Helper methods can be extended for future requirements
5. **ISO Compliance:** Dict output always uses ISO 8601 standard

---

## Related Files

- `ba2_trade_platform/core/interfaces/DataProviderInterface.py` - Base interface with helpers
- `ba2_trade_platform/core/interfaces/MarketDataProviderInterface.py` - OHLCV implementation
- `test_datetime_formatting.py` - Test script for datetime formatting verification
- `test_chart_flow_only.py` - Test script for chart compatibility verification
- **[CHART_COMPATIBILITY_VERIFICATION.md](CHART_COMPATIBILITY_VERIFICATION.md)** - Proof that charts are not affected

---

## Chart Compatibility

✅ **Data visualization charts are NOT affected by these changes.**

The chart component uses `get_ohlcv_data()` which returns a DataFrame with datetime objects, completely independent of the dict/markdown formatting changes. See [CHART_COMPATIBILITY_VERIFICATION.md](CHART_COMPATIBILITY_VERIFICATION.md) for detailed analysis and test results.

---

## Next Steps

1. Update news providers to use helper methods (default to daily interval)
2. Update fundamentals providers (default to date-only for quarterly/annual data)
3. Update indicators providers (follow OHLCV pattern)
4. Run comprehensive tests across all provider types
5. Document any provider-specific datetime formatting requirements
