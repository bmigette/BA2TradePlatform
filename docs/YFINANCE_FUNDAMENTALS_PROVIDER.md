# YFinance Fundamentals Provider Migration

## Overview
Converted YFinance fundamental data functions (`get_balance_sheet`, `get_cashflow`, `get_income_statement`) from legacy dataflows to BA2 provider system with comprehensive debug logging.

## Implementation

### New Provider: `YFinanceCompanyDetailsProvider`
**Location:** `ba2_trade_platform/modules/dataproviders/fundamentals/details/YFinanceCompanyDetailsProvider.py`

**Interface:** Implements `CompanyFundamentalsDetailsInterface`

**Methods:**
1. **`get_balance_sheet(symbol, frequency="quarterly", format_type="dict")`**
   - Fetches balance sheet data from Yahoo Finance
   - Supports quarterly/annual frequency
   - Returns dict or markdown formatted output
   - Groups data by Assets, Liabilities, and Equity in markdown

2. **`get_income_statement(symbol, frequency="quarterly", format_type="dict")`**
   - Fetches income statement data from Yahoo Finance
   - Supports quarterly/annual frequency
   - Returns dict or markdown formatted output
   - Lists all line items in markdown

3. **`get_cashflow_statement(symbol, frequency="quarterly", format_type="dict")`**
   - Fetches cash flow statement data from Yahoo Finance
   - Supports quarterly/annual frequency
   - Returns dict or markdown formatted output
   - Groups by Operating, Investing, and Financing Activities in markdown

### Debug Logging
All methods use the `@log_provider_call` decorator for automatic debug logging:
- Logs provider class name
- Logs method name
- Logs input arguments (excluding 'self')
- Logs result type and size
- Logs errors with full tracebacks

### Provider Registration
**Category:** `fundamentals_details`
**Provider Name:** `yfinance`

```python
FUNDAMENTALS_DETAILS_PROVIDERS: Dict[str, Type[CompanyFundamentalsDetailsInterface]] = {
    "alphavantage": AlphaVantageCompanyDetailsProvider,
    "yfinance": YFinanceCompanyDetailsProvider,
}
```

### Routing Configuration
**File:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/interface.py`

**BA2_PROVIDER_MAP entries:**
```python
("get_balance_sheet", "yfinance"): ("fundamentals_details", "yfinance"),
("get_cashflow", "yfinance"): ("fundamentals_details", "yfinance"),
("get_income_statement", "yfinance"): ("fundamentals_details", "yfinance"),
```

## Usage Example

```python
from ba2_trade_platform.modules.dataproviders import get_provider

# Get the YFinance fundamentals provider
provider = get_provider("fundamentals_details", "yfinance")

# Fetch quarterly balance sheet as dict
balance_sheet = provider.get_balance_sheet("AAPL", frequency="quarterly", format_type="dict")

# Fetch annual income statement as markdown
income_stmt = provider.get_income_statement("AAPL", frequency="annual", format_type="markdown")

# Fetch quarterly cash flow as dict
cashflow = provider.get_cashflow_statement("AAPL", frequency="quarterly", format_type="dict")
```

## Legacy Functions
The original functions in `tradingagents/dataflows/y_finance.py` still exist but are now superseded by this BA2 provider:
- `get_balance_sheet()` - Use `YFinanceCompanyDetailsProvider.get_balance_sheet()`
- `get_cashflow()` - Use `YFinanceCompanyDetailsProvider.get_cashflow_statement()`
- `get_income_statement()` - Use `YFinanceCompanyDetailsProvider.get_income_statement()`

## Data Format

### Dict Format
Returns dictionary with:
```python
{
    "symbol": "AAPL",
    "frequency": "quarterly",
    "periods": ["2024-09-30", "2024-06-30", ...],
    "data": {
        "Total Assets": [123.45, 120.30, ...],
        "Total Liabilities": [67.89, 65.43, ...],
        ...
    }
}
```

### Markdown Format
Returns formatted markdown string with:
- Header with symbol and frequency
- All available periods (most recent first)
- Grouped financial data (for balance sheet and cash flow)
- All values in millions

## Benefits
1. **Consistent Logging:** Automatic debug logs for all provider calls
2. **Standardized Interface:** Follows CompanyFundamentalsDetailsInterface
3. **Flexible Output:** Supports both dict and markdown formats
4. **Error Handling:** Comprehensive error handling with detailed logging
5. **No Fallbacks:** Raises errors instead of silently failing with default values
6. **Data Grouping:** Markdown output intelligently groups related line items

## Registry Cleanup
Removed legacy `FUNDAMENTALS_PROVIDERS` registry:
- Removed from `ba2_trade_platform/modules/dataproviders/__init__.py`
- Updated `get_provider()` to exclude "fundamentals" category
- Updated `list_providers()` to exclude "fundamentals" category
- Removed non-existent `AlphaVantageFundamentalsProvider` references

## Files Modified
1. **Created:** `ba2_trade_platform/modules/dataproviders/fundamentals/details/YFinanceCompanyDetailsProvider.py`
2. **Updated:** `ba2_trade_platform/modules/dataproviders/fundamentals/details/__init__.py`
3. **Updated:** `ba2_trade_platform/modules/dataproviders/fundamentals/__init__.py`
4. **Updated:** `ba2_trade_platform/modules/dataproviders/__init__.py`
5. **Updated:** `ba2_trade_platform/thirdparties/TradingAgents/tradingagents/dataflows/interface.py`

## Testing
To test the provider:
```python
# Test basic functionality
provider = get_provider("fundamentals_details", "yfinance")
result = provider.get_balance_sheet("AAPL")
print(result)

# Test markdown format
markdown = provider.get_income_statement("AAPL", format_type="markdown")
print(markdown)

# Check debug logs
# Look for entries like: "Provider call: YFinanceCompanyDetailsProvider.get_balance_sheet"
```

## Dependencies
- `yfinance` - Yahoo Finance data library
- `pandas` - Data manipulation
- `@log_provider_call` - Debug logging decorator

## Notes
- Data is fetched directly from Yahoo Finance via the `yfinance` library
- Quarterly data shows last 4 quarters (or fewer if not available)
- Annual data shows last 4 years (or fewer if not available)
- All financial values are in millions
- Missing data points are handled gracefully (shown as "N/A" in markdown)
