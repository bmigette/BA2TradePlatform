# Data Provider Format Specification

**Date**: October 21, 2025  
**Version**: 1.0  
**Status**: ACTIVE SPECIFICATION

## Critical Rule: format_type Parameter Behavior

### Overview
All data providers implementing `format_type` parameter MUST support the same standardized signatures and behaviors:

```python
format_type: Literal["dict", "markdown", "both"] = "markdown"
```

### Format Type Semantics

#### 1. format_type="markdown" (DEFAULT)
**Purpose**: Markdown-formatted string for LLM consumption
**Output**: String containing formatted markdown (e.g., tables, headers, paragraphs)
**Use Case**: Agent reasoning, human-readable reports
**Example**:
```python
result = provider.get_indicator("AAPL", "rsi", ..., format_type="markdown")
# Returns: "# RSI (Relative Strength Index)\n\n| Date | Value |\n..."
```

#### 2. format_type="dict" (CRITICAL)
**Purpose**: Structured Python dictionary for programmatic consumption
**Output**: JSON-serializable dict with NO markdown content
**Structure**: Must expose structured data directly, not wrapped in markdown
**Key Requirement**: Dict must be valid JSON (no Python objects, datetime should be ISO strings)

**CORRECT format for indicator data**:
```python
{
    "symbol": "AAPL",
    "indicator": "rsi",
    "interval": "1d",
    "start_date": "2025-09-21T00:00:00",
    "end_date": "2025-10-21T00:00:00",
    "dates": ["2025-09-22T09:30:00", "2025-09-22T10:30:00", ...],  # Direct arrays for visualization
    "values": [72.3, 68.1, 75.2, ...],                              # Direct arrays for visualization
    "metadata": {
        "count": 147,
        "first_date": "2025-09-22T09:30:00",
        "last_date": "2025-10-21T15:30:00",
        "data_type": "float",
        "precision": 4,
        "description": "...",
        "usage": "...",
        "tips": "...",
        "missing_periods": []
    }
}
```

**WRONG - Contains markdown/non-serializable data**:
```python
# ❌ WRONG - markdown inside dict
{
    "data": "# RSI\n| Date | Value |\n..."  # markdown string - WRONG!
}

# ❌ WRONG - datetime objects (not JSON serializable)
{
    "dates": [datetime(2025, 9, 22), ...]  # datetime object - WRONG!
}

# ❌ WRONG - nested old format
{
    "data": [{"date": ..., "value": ...}]  # Old format with data_points
}
```

#### 3. format_type="both" (OPTIONAL)
**Purpose**: Return both markdown and structured dict
**Output**: Dict with two keys:
```python
{
    "text": "markdown string (same as format_type='markdown')",
    "data": {structured_dict (same as format_type='dict')}
}
```

### Implementation Rules

**Rule 1: "dict" format must be clean**
- ✅ Only JSON-serializable types (str, int, float, bool, list, dict, null)
- ✅ ISO 8601 strings for dates (no datetime objects)
- ✅ Direct `"dates"` and `"values"` arrays for visualization data
- ❌ NO markdown content
- ❌ NO Python objects
- ❌ NO binary data

**Rule 2: "markdown" format can be complex**
- ✅ Include markdown tables, headers, paragraphs
- ✅ Include data points for reference
- ✅ Use for human-readable output

**Rule 3: Separation of concerns**
- ❌ NEVER put markdown inside "dict" format
- ❌ NEVER hide structured data in markdown strings
- ❌ NEVER duplicate data in both formats unnecessarily

**Rule 4: Backward compatibility**
- Create separate `markdown_response` and `structured_response` dicts
- Return appropriate response based on `format_type` parameter
- Use intermediate variables to keep logic clear

## Implementation Template

```python
def get_indicator(
    self,
    symbol: str,
    indicator: str,
    start_date: datetime,
    end_date: datetime,
    interval: str = "1d",
    format_type: Literal["dict", "markdown", "both"] = "markdown"
) -> Dict[str, Any] | str:
    """
    Returns:
        - format_type="markdown": Markdown-formatted string
        - format_type="dict": Structured dict (dates/values/metadata) 
        - format_type="both": Dict with "text" (markdown) and "data" (structured dict) keys
    """
    # 1. Calculate/fetch raw data
    data_points = [...]  # List of {date, value} dicts
    iso_dates = [point["date"] for point in data_points]
    values = [point["value"] for point in data_points]
    
    # 2. Build structured response (CLEAN - no markdown)
    structured_response = {
        "symbol": symbol.upper(),
        "indicator": indicator,
        "dates": iso_dates,        # Direct arrays!
        "values": values,          # Direct arrays!
        "metadata": {
            "count": len(data_points),
            "description": "...",
            # ... other fields
        }
    }
    
    # 3. Build markdown response (can include data points)
    markdown_response = {
        "symbol": symbol.upper(),
        "indicator": indicator,
        "data": data_points,       # OK for markdown - for reference
        "metadata": {...}
    }
    
    # 4. Return based on format_type
    if format_type == "dict":
        return structured_response
    elif format_type == "both":
        return {
            "text": self._format_markdown(markdown_response),
            "data": structured_response
        }
    else:  # markdown
        return self._format_markdown(markdown_response)
```

## Applicable Providers

### VERIFIED (format_type="both" supported)
- ✅ PandasIndicatorCalc - Structured with dates/values arrays
- ✅ AlphaVantageIndicatorsProvider - Structured with dates/values arrays
- ⚠️ SocialMediaDataProviderInterface - Uses "both" format

### REVIEW NEEDED
- [ ] All CompanyFundamentalsDetailsInterface implementations
- [ ] All MarketNewsInterface implementations  
- [ ] All MacroEconomicsInterface implementations
- [ ] All CompanyInsiderInterface implementations

## Data Format Examples

### Good: Indicator Provider (Structured)
```python
# dict format
{
    "symbol": "AAPL",
    "dates": ["2025-10-01T09:30:00", "2025-10-02T09:30:00"],
    "values": [72.5, 71.2],
    "metadata": {"count": 2, "description": "RSI indicator"}
}
```

### Good: News Provider (Flexible)
```python
# dict format - can have article objects since they're serializable
{
    "articles": [
        {
            "title": "Stock rises...",
            "date": "2025-10-21T00:00:00",
            "sentiment": 0.85
        }
    ]
}
```

### Bad: Any Provider
```python
# ❌ WRONG - markdown in dict
{"data": "# Header\n| Table |"}

# ❌ WRONG - datetime object in dict
{"date": datetime.now()}

# ❌ WRONG - nested data in "data" field
{"data": [{...}]}  # Should be "items" or direct array
```

## Visualization Requirement: dates/values Arrays

For visualization tools like InstrumentGraph, providers SHOULD include direct `dates` and `values` arrays in the dict format:

```python
# For visualization
"dates": ["2025-09-22T09:30:00", "2025-09-22T10:30:00", ...],
"values": [288.3469, 288.9163, ...],
```

This avoids the need for nested iteration and allows UI components to directly map data to chart points.

## Testing Checklist

For each provider using `format_type`:

- [ ] `format_type="markdown"` returns string (no dicts)
- [ ] `format_type="dict"` returns dict with NO markdown content
- [ ] `format_type="dict"` is fully JSON-serializable (`json.dumps(result)` works)
- [ ] `format_type="both"` returns dict with "text" and "data" keys
- [ ] Dates use ISO 8601 format (no datetime objects)
- [ ] Arrays are direct: `"dates": [...]` not `"data": [{"date": ...}]`
- [ ] Metadata includes all relevant context
- [ ] Backward compatibility maintained for old format

## References

- **Indicator Providers**: PandasIndicatorCalc, AlphaVantageIndicatorsProvider
- **Related Files**:
  - `ba2_trade_platform/core/interfaces/MarketIndicatorsInterface.py`
  - `ba2_trade_platform/modules/dataproviders/indicators/*.py`
  - `ba2_trade_platform/modules/experts/TradingAgentsUI.py` (consumer)

## Update History

| Date | Change | Status |
|------|--------|--------|
| 2025-10-21 | Initial specification | Active |
| 2025-10-21 | PandasIndicatorCalc fixed | ✅ |
| 2025-10-21 | AlphaVantageIndicatorsProvider fixed | ✅ |
