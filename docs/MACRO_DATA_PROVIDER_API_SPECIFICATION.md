# Macro Data Provider API Specification

## Overview

This document specifies the complete API contract for all macro/fundamental data providers in the BA2 Trade Platform. It ensures consistency across providers and defines the exact parameters, return types, and error handling behavior.

## Core Interface

All macro providers must implement the following methods:

### 1. get_treasury_yields

**Purpose**: Retrieve current and historical Treasury yield curve data.

**Signature**:
```python
def get_treasury_yields(
    self,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Union[str, Dict[str, Any]]
```

**Parameters**:
- `start_date` (date, optional): Start date for yield data. Default is 30 days ago.
- `end_date` (date, optional): End date for yield data. Default is today.
- `format_type` (str): Output format - "markdown" (default), "dict", or "both"

**Return Type (markdown)**:
```
String in markdown format containing:
- Markdown headers and formatting
- Tabular representation of yields
- Key statistics (trend, changes, inversions)
- Analysis and interpretation
```

**Return Type (dict)**:
```python
{
    "yields": [
        {
            "maturity": "1M",  # or "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"
            "date": "2025-09-22",  # ISO 8601 date
            "value": 4.85
        },
        # ... more yields
    ],
    "dates": ["2025-09-22", "2025-09-23"],  # For time series visualization
    "maturities": ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"],
    "metadata": {
        "source": "provider_name",
        "count": 11,  # number of maturity points
        "latest_date": "2025-09-22"
    }
}
```

**Return Type (both)**:
```python
{
    "text": "# Treasury Yields\n...",  # Markdown string
    "data": {...}  # Dict structure above
}
```

**Error Handling**: Raise exception with descriptive message if data cannot be retrieved.

---

### 2. get_economic_indicators

**Purpose**: Retrieve key economic indicators (unemployment, inflation, GDP, etc.).

**Signature**:
```python
def get_economic_indicators(
    self,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Union[str, Dict[str, Any]]
```

**Parameters**:
- `start_date` (date, optional): Start date for indicator data. Default is 1 year ago.
- `end_date` (date, optional): End date for indicator data. Default is today.
- `format_type` (str): Output format - "markdown" (default), "dict", or "both"

**Return Type (markdown)**:
```
String in markdown format containing:
- Markdown headers and statistics
- Current values vs previous periods
- Trend analysis
- Year-over-year comparisons
```

**Return Type (dict)**:
```python
{
    "indicators": [
        {
            "name": "Unemployment Rate",  # e.g., "Inflation Rate", "GDP", "Consumer Confidence"
            "date": "2025-09-01",
            "value": 4.2,
            "unit": "%",  # or "trillions", "index points", etc.
            "previous_value": 4.1,  # value from previous period
            "change": 0.1,
            "change_percent": 2.4
        },
        # ... more indicators
    ],
    "metadata": {
        "source": "provider_name",
        "indicators_count": 4,
        "latest_date": "2025-09-01"
    }
}
```

---

### 3. get_inflation_data

**Purpose**: Retrieve inflation-related economic data (CPI, PPI, etc.).

**Signature**:
```python
def get_inflation_data(
    self,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Union[str, Dict[str, Any]]
```

**Parameters**:
- `start_date` (date, optional): Start date for inflation data. Default is 2 years ago.
- `end_date` (date, optional): End date for inflation data. Default is today.
- `format_type` (str): Output format - "markdown" (default), "dict", or "both"

**Return Type (markdown)**:
```
String in markdown format containing:
- Current and historical CPI/PPI values
- Year-over-year inflation rates
- Monthly/quarterly trends
- Market expectations vs actual
```

**Return Type (dict)**:
```python
{
    "cpi_data": [
        {
            "date": "2025-09-01",
            "value": 308.4,
            "yoy_change": 2.4,  # Year-over-year % change
            "mom_change": 0.2   # Month-over-month % change
        },
        # ... more CPI data points
    ],
    "ppi_data": [
        # Same structure as CPI
    ],
    "metadata": {
        "source": "provider_name",
        "latest_cpi_date": "2025-09-01",
        "latest_ppi_date": "2025-09-01"
    }
}
```

---

### 4. get_employment_data

**Purpose**: Retrieve employment statistics (unemployment rate, non-farm payrolls, jobless claims, etc.).

**Signature**:
```python
def get_employment_data(
    self,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Union[str, Dict[str, Any]]
```

**Parameters**:
- `start_date` (date, optional): Start date for employment data. Default is 2 years ago.
- `end_date` (date, optional): End date for employment data. Default is today.
- `format_type` (str): Output format - "markdown" (default), "dict", or "both"

**Return Type (markdown)**:
```
String in markdown format containing:
- Unemployment rate trend
- NFP (Non-Farm Payrolls) reports
- Initial jobless claims
- Labor force participation rate
```

**Return Type (dict)**:
```python
{
    "unemployment_rate": [
        {
            "date": "2025-09-01",
            "value": 4.2,
            "previous": 4.1,
            "change": 0.1
        },
        # ... more data
    ],
    "non_farm_payrolls": [
        {
            "date": "2025-09-01",
            "value": 142000,  # thousands of jobs
            "previous": 159000,
            "change": -17000
        },
        # ... more data
    ],
    "jobless_claims": [
        {
            "date": "2025-09-20",  # Weekly data
            "value": 224000,
            "previous": 225000
        },
        # ... more data
    ],
    "metadata": {
        "source": "provider_name",
        "latest_unemployment_date": "2025-09-01",
        "unemployment_unit": "%"
    }
}
```

---

### 5. get_fed_calendar

**Purpose**: Retrieve Federal Reserve calendar events, FOMC meeting dates, and announcements.

**Signature**:
```python
def get_fed_calendar(
    self,
    end_date: Optional[date] = None,
    lookback_days: int = 30,
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Union[str, Dict[str, Any]]
```

**Parameters**:
- `end_date` (date, optional): End date for calendar events. Default is today.
- `lookback_days` (int): Number of days to look back. Default is 30.
- `format_type` (str): Output format - "markdown" (default), "dict", or "both"

**Return Type (markdown)**:
```
String in markdown format containing:
- Upcoming FOMC meeting dates
- Fed announcement schedule
- Key event dates
- Interest rate decisions
```

**Return Type (dict)**:
```python
{
    "events": [
        {
            "date": "2025-11-18",
            "event": "FOMC Meeting",
            "type": "fomc_meeting",  # or "fomc_announcement", "rate_decision", "minutes_release"
            "time": "18:00",  # HH:MM UTC
            "description": "Federal Open Market Committee Meeting",
            "impact": "high"  # or "medium", "low"
        },
        {
            "date": "2025-11-19",
            "event": "FOMC Rate Decision",
            "type": "rate_decision",
            "time": "18:00",
            "description": "Federal Reserve announces interest rate decision",
            "impact": "high"
        },
        # ... more events
    ],
    "metadata": {
        "source": "provider_name",
        "events_count": 5,
        "period_start": "2025-08-20",
        "period_end": "2025-09-20"
    }
}
```

---

## Format Type Semantics

### format_type="markdown" (DEFAULT)
- Returns markdown-formatted string for LLM consumption
- Optimized for readability and analysis
- Can include markdown formatting (headers, tables, emphasis)
- No need to parse data - ready for direct use

### format_type="dict"
- Returns JSON-serializable Python dictionary
- Structured data ONLY (no markdown content)
- Direct arrays for visualization: `"dates": [...], "values": [...], "maturities": [...]`
- ISO 8601 strings for dates (not datetime objects)
- Enables direct data binding in UI components
- CRITICAL: Must not contain markdown or Python objects

### format_type="both"
- Returns dictionary with two keys:
  - `"text"`: markdown-formatted string (same as markdown format)
  - `"data"`: structured dictionary (same as dict format)
- Allows consumers to choose their preferred format

---

## Error Handling Requirements

### Exception Types
All providers must raise descriptive exceptions when:
- API key is invalid or missing
- Rate limits are exceeded
- Network connection fails
- API returns error response
- Data is unavailable for the requested period

### Error Messages
- Include provider name in error message
- Include timestamp of error
- Include specific reason (e.g., "Invalid API key", "Rate limit exceeded")
- Example: `"FRED provider error: Invalid API key provided"`

### Retry Logic
- Implementations may retry transient errors (network, rate limits)
- Must not retry authentication errors
- Should log retry attempts at debug level

---

## Data Quality Requirements

### Date Handling
- All dates must be ISO 8601 format: `"YYYY-MM-DD"`
- Timezone should be UTC unless specified otherwise
- Use `date` objects, never `datetime` in dict format timestamps

### Numerical Precision
- Maintain original precision from data source
- Percentages stored as numeric values (not pre-formatted strings)
- Examples: `4.2` (not "4.2%"), `308.4` (not "308.40")

### Missing Data
- If data is unavailable for requested period, indicate clearly
- Don't use default/placeholder values
- Raise exception rather than returning incomplete data silently

### Currency
- US Dollar assumed unless specified
- Include currency indicator in metadata if non-USD

---

## Default Date Ranges

| Method | Default Lookback |
|--------|------------------|
| `get_treasury_yields` | 30 days |
| `get_economic_indicators` | 1 year |
| `get_inflation_data` | 2 years |
| `get_employment_data` | 2 years |
| `get_fed_calendar` | 30 days (lookback_days parameter) |

---

## Implementation Checklist

When implementing a macro provider, ensure:

- [ ] All 5 methods are implemented
- [ ] `format_type` parameter is supported with all three options
- [ ] Markdown format is LLM-friendly and readable
- [ ] Dict format is clean and JSON-serializable
- [ ] Default date ranges are applied correctly
- [ ] Error messages are descriptive with provider name
- [ ] Dates are in ISO 8601 format
- [ ] Metadata includes source and count information
- [ ] No default/fallback values for missing data
- [ ] Docstrings explain parameters and return types
- [ ] Tests cover all three format types

---

## Usage Examples

### Example 1: Get Treasury Yields (Markdown)
```python
provider = MacroProvider()
yields_md = provider.get_treasury_yields(format_type="markdown")
# Returns ready-to-display markdown string
```

### Example 2: Get Treasury Yields (Dict)
```python
yields_dict = provider.get_treasury_yields(format_type="dict")
# Returns {"yields": [...], "dates": [...], "metadata": {...}}
# Ready for visualization or further processing
```

### Example 3: Get Treasury Yields (Both)
```python
yields_both = provider.get_treasury_yields(format_type="both")
# Returns {"text": "# Treasury Yields\n...", "data": {...}}
# Flexible consumption
```

### Example 4: Get Economic Indicators with Custom Date Range
```python
from datetime import date, timedelta

end = date.today()
start = end - timedelta(days=180)

indicators = provider.get_economic_indicators(
    start_date=start,
    end_date=end,
    format_type="dict"
)
```

---

## Provider Implementations

### Current Providers

| Provider | get_treasury_yields | get_economic_indicators | get_inflation_data | get_employment_data | get_fed_calendar |
|----------|:--:|:--:|:--:|:--:|:--:|
| FRED | ✅ | ✅ | ✅ | ✅ | ✅ |
| Finnhub | ✅ | ✅ | ✅ | ✅ | ✅ |
| EconDB | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## Breaking Changes and Versioning

Any changes to method signatures or return structures require:
1. Major version bump in provider
2. Update to this specification
3. Migration guide for existing consumers
4. Deprecation period with warnings (if applicable)
