# Earnings Data Feature Implementation

## Summary
Added comprehensive earnings data support to the BA2 Trade Platform's fundamentals data providers, enabling retrieval of historical earnings and future earnings estimates across Yahoo Finance, Financial Modeling Prep, and AlphaVantage data sources.

## Changes Made

### 1. Core Interface Updates
**File**: `ba2_trade_platform/core/interfaces/CompanyFundamentalsDetailsInterface.py`

Added two new abstract methods to the `CompanyFundamentalsDetailsInterface`:

#### `get_past_earnings()`
- Retrieves historical earnings data with actual vs estimated EPS
- Calculates earnings surprises and surprise percentages
- Default lookback: 8 quarters (2 years)
- Returns both dict and markdown formats

**Method Signature**:
```python
def get_past_earnings(
    symbol: str,
    frequency: Literal["annual", "quarterly"],
    end_date: datetime,
    lookback_periods: int = 8,
    format_type: Literal["dict", "markdown"] = "markdown"
) -> Dict[str, Any] | str
```

**Return Format (dict)**:
```python
{
    "symbol": str,
    "frequency": str,
    "end_date": str (ISO),
    "lookback_periods": int,
    "earnings": [{
        "fiscal_date_ending": str,
        "report_date": str,
        "reported_eps": float,
        "estimated_eps": float,
        "surprise": float,
        "surprise_percent": float
    }],
    "retrieved_at": str (ISO)
}
```

#### `get_earnings_estimates()`
- Retrieves future earnings estimates with analyst consensus
- Includes average, high, low estimates and analyst count
- Default forward periods: 4 quarters
- Returns both dict and markdown formats

**Method Signature**:
```python
def get_earnings_estimates(
    symbol: str,
    frequency: Literal["annual", "quarterly"],
    as_of_date: datetime,
    lookback_periods: int = 4,
    format_type: Literal["dict", "markdown"] = "markdown"
) -> Dict[str, Any] | str
```

**Return Format (dict)**:
```python
{
    "symbol": str,
    "frequency": str,
    "as_of_date": str (ISO),
    "estimates": [{
        "fiscal_date_ending": str,
        "estimated_eps_avg": float,
        "estimated_eps_high": float,
        "estimated_eps_low": float,
        "number_of_analysts": int
    }],
    "retrieved_at": str (ISO)
}
```

### 2. Yahoo Finance Provider Implementation
**File**: `ba2_trade_platform/modules/dataproviders/fundamentals/details/YFinanceCompanyDetailsProvider.py`

#### Implementation Details:
- **Past Earnings**: Uses `ticker.get_earnings(freq='quarterly')` and `ticker.get_earnings(freq='yearly')`
  - Extracts 'Reported EPS' and 'Estimated EPS' from DataFrame
  - Calculates surprise and surprise percentage
  - Filters by end_date and applies lookback_periods limit

- **Earnings Estimates**: Uses `ticker.get_earnings_estimate()`
  - Extracts 'Avg. Estimate', 'High Estimate', 'Low Estimate', 'Number of Analysts'
  - Filters for future periods only (after as_of_date)
  - Limits to specified number of forward periods

- **Markdown Formatting**: 
  - `_format_past_earnings_markdown()`: Table with date, reported/estimated EPS, surprise
  - `_format_earnings_estimates_markdown()`: Table with date, avg/high/low estimates, analyst count

- **Supported Features Updated**: Added `"past_earnings"` and `"earnings_estimates"`

### 3. FMP Provider Implementation
**File**: `ba2_trade_platform/modules/dataproviders/fundamentals/details/FMPCompanyDetailsProvider.py`

#### Implementation Details:
- **Past Earnings**: Uses `fmpsdk.historical_earning_calendar()`
  - API returns actual vs estimated EPS with surprise data
  - Fields: `eps` (reported), `epsEstimated`, `date`
  - Calculates surprise percentage if not provided

- **Earnings Estimates**: Uses `fmpsdk.analyst_estimates()`
  - API parameters: symbol, period ('quarter'/'annual'), limit
  - Fields: `estimatedEpsAvg`, `estimatedEpsHigh`, `estimatedEpsLow`, `numberAnalystEstimatedEps`
  - Filters for future dates only

- **Markdown Formatting**: Same structure as Yahoo Finance provider
  - Consistent table formats across all providers

- **Supported Features Updated**: Added `"past_earnings"` and `"earnings_estimates"`

### 4. AlphaVantage Provider Implementation
**File**: `ba2_trade_platform/modules/dataproviders/fundamentals/details/AlphaVantageCompanyDetailsProvider.py`

#### Implementation Details:
- **Past Earnings**: Uses `EARNINGS` API function
  - Retrieves `annualEarnings` or `quarterlyEarnings` based on frequency
  - Fields: `reportedEPS`, `estimatedEPS`, `fiscalDateEnding`, `reportedDate`
  - Handles "None" string values from API (converts to 0)
  - Calculates surprise and surprise percentage

- **Earnings Estimates**: Uses `EARNINGS` API function
  - Extracts `estimatedEPS` for future periods
  - Limited data: AlphaVantage doesn't provide high/low/analyst count
  - Uses avg estimate for all three fields (high/low/avg)
  - Sets number_of_analysts to 0 (not provided by API)

- **Markdown Formatting**: Same structure as other providers
  - Consistent user experience across data sources

- **Supported Features Updated**: Added `"past_earnings"` and `"earnings_estimates"`

### 5. Test Script
**File**: `test_files/test_earnings_data.py`

Comprehensive test suite that:
- Tests all three providers (YFinance, FMP, AlphaVantage)
- Tests both `get_past_earnings()` and `get_earnings_estimates()`
- Tests with AAPL (Apple Inc.) as the test symbol
- Validates dict and markdown output formats
- Displays sample data from each provider
- Reports pass/fail status for each provider

**Usage**:
```powershell
.venv\Scripts\python.exe test_files\test_earnings_data.py
```

## Data Source Comparison

| Feature | Yahoo Finance | FMP | AlphaVantage |
|---------|--------------|-----|--------------|
| Past Earnings | ✅ Full data | ✅ Full data | ✅ Full data |
| Earnings Estimates | ✅ Full data | ✅ Full data | ⚠️ Limited* |
| High/Low Estimates | ✅ Yes | ✅ Yes | ❌ No |
| Analyst Count | ✅ Yes | ✅ Yes | ❌ No |
| API Key Required | ❌ No | ✅ Yes | ✅ Yes |
| Rate Limits | Moderate | Varies by tier | 5 calls/min (free) |

*AlphaVantage only provides average estimates for future periods, not high/low/analyst count.

## Key Features

### 1. Consistent Interface
All three providers implement the same interface methods with identical signatures and return structures, ensuring easy switching between data sources.

### 2. Flexible Date Handling
- **Past Earnings**: `end_date` + `lookback_periods` for historical data
- **Estimates**: `as_of_date` + `lookback_periods` for forward-looking data

### 3. Error Handling
- Graceful handling of missing data (returns empty earnings/estimates list)
- Proper error messages in both dict and markdown formats
- Detailed logging for debugging

### 4. Data Enrichment
- Automatic calculation of earnings surprises
- Surprise percentage calculation
- Date formatting standardization

### 5. Multiple Output Formats
- **Dict Format**: Structured data for programmatic use
- **Markdown Format**: Human/LLM-readable tables

## Usage Examples

### Getting Past Earnings (2 years, quarterly)
```python
from ba2_trade_platform.modules.dataproviders.fundamentals.details import YFinanceCompanyDetailsProvider
from datetime import datetime

provider = YFinanceCompanyDetailsProvider()
earnings = provider.get_past_earnings(
    symbol="AAPL",
    frequency="quarterly",
    end_date=datetime.now(),
    lookback_periods=8,  # 8 quarters = 2 years
    format_type="dict"
)

# Access data
for earning in earnings["earnings"]:
    print(f"{earning['fiscal_date_ending']}: Reported ${earning['reported_eps']:.2f}, "
          f"Estimated ${earning['estimated_eps']:.2f}, "
          f"Surprise {earning['surprise_percent']:.1f}%")
```

### Getting Earnings Estimates (next 4 quarters)
```python
estimates = provider.get_earnings_estimates(
    symbol="AAPL",
    frequency="quarterly",
    as_of_date=datetime.now(),
    lookback_periods=4,  # Next 4 quarters
    format_type="markdown"
)

# Get markdown table for LLM/display
print(estimates)
```

## Agent Integration (COMPLETED)

### Fundamental Analyst Agent Updates

#### Toolkit Methods Added (`agent_utils_new.py`)
Two new methods added to the Toolkit class:

1. **`get_past_earnings()`**
   - Aggregates past earnings from all configured fundamentals_details providers
   - Returns markdown-formatted historical earnings with surprises
   - Default: 8 quarters (2 years) of data

2. **`get_earnings_estimates()`**
   - Aggregates forward earnings estimates from all providers
   - Returns markdown-formatted analyst consensus data
   - Default: 4 quarters of forward estimates

#### Tool Wrappers Added (`fundamentals_analyst.py`)
Two new @tool decorated functions added to the fundamentals analyst:

```python
@tool
def get_past_earnings(symbol: str, end_date: str, lookback_periods: int = 8, frequency: str = "quarterly") -> str:
    """Get historical earnings data showing actual vs estimated EPS for the past 2 years."""
    return toolkit.get_past_earnings(symbol, end_date, lookback_periods, frequency)

@tool
def get_earnings_estimates(symbol: str, as_of_date: str, lookback_periods: int = 4, frequency: str = "quarterly") -> str:
    """Get forward earnings estimates from analysts for the next 4 quarters."""
    return toolkit.get_earnings_estimates(symbol, as_of_date, lookback_periods, frequency)
```

#### System Prompt Updates (`prompts.py`)
Enhanced FUNDAMENTALS_ANALYST_SYSTEM_PROMPT with earnings analysis guidance:

**EARNINGS ANALYSIS:** 
- **Earnings Quality**: Review past 2 years (8 quarters) for consistency and growth trends
- **Earnings Surprises**: Analyze beat/meet/miss patterns to assess management execution
- **Surprise Trends**: Identify positive/negative patterns showing company strength/weakness
- **Forward Guidance**: Compare estimates with historical performance for realism assessment
- **Analyst Consensus**: Evaluate estimate range spread for confidence/uncertainty signals

The agent now has 7 tools available:
1. `get_balance_sheet`
2. `get_income_statement`
3. `get_cashflow_statement`
4. `get_insider_transactions`
5. `get_insider_sentiment`
6. **`get_past_earnings`** ← NEW
7. **`get_earnings_estimates`** ← NEW

## Testing

Run the test script to verify all three providers:
```powershell
.venv\Scripts\python.exe test_files\test_earnings_data.py
```

Expected output:
- ✓ All three providers initialize successfully
- ✓ Past earnings data retrieved for each provider
- ✓ Earnings estimates retrieved for each provider
- ✓ Both dict and markdown formats work correctly
- ✓ Data structure validation passes

## Related Files
- Core interface: `ba2_trade_platform/core/interfaces/CompanyFundamentalsDetailsInterface.py`
- YFinance: `ba2_trade_platform/modules/dataproviders/fundamentals/details/YFinanceCompanyDetailsProvider.py`
- FMP: `ba2_trade_platform/modules/dataproviders/fundamentals/details/FMPCompanyDetailsProvider.py`
- AlphaVantage: `ba2_trade_platform/modules/dataproviders/fundamentals/details/AlphaVantageCompanyDetailsProvider.py`
- Test script: `test_files/test_earnings_data.py`

## Notes
- All providers follow the ExtendableSettingsInterface pattern
- Earnings data is cached using provider_utils decorators (@log_provider_call)
- Proper logging throughout for debugging and monitoring
- Confidence level handling: All values stored as 1-100 scale (not 0-1)
- Never use default values for live market data (prices, earnings, etc.)
