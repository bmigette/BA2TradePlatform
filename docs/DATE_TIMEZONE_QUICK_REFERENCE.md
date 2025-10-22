# Date/Timezone Quick Reference Card

## TL;DR - Key Points

1. **Store**: All dates in UTC timezone
2. **Display**: Convert to local timezone for users
3. **Use**: `format_for_display()` for all UI date displays

## Models - All Correct ✅

```python
# ExpertRecommendation, MarketAnalysis, AnalysisOutput, 
# Transaction, TradingOrder, TradeActionResult

created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc))
```

## Utility Functions - Use These

```python
from ba2_trade_platform.core.date_utils import (
    format_for_display,      # For UI display
    format_relative,         # For "2 hours ago" style
    utc_to_local,           # For conversion
    local_to_utc,           # For user input
    ensure_utc,             # For defensive conversion
    get_utc_now,            # Current UTC time
)
```

## Common Patterns

### Display a Timestamp
```python
# GOOD
ui.label(format_for_display(market_analysis.created_at))
# Output: "2025-10-22 09:15:30 PDT"

# ALSO GOOD - Relative
ui.label(format_relative(market_analysis.created_at))
# Output: "2 hours ago"
```

### Store a User-Entered Date
```python
# GOOD
from ba2_trade_platform.core.date_utils import local_to_utc
utc_date = local_to_utc(user_input_datetime)
transaction.open_date = utc_date

# BAD - Don't do this
transaction.open_date = user_input_datetime  # Loses timezone info
```

### Convert API Date to UTC
```python
# GOOD
from ba2_trade_platform.core.date_utils import ensure_utc
api_date = datetime.fromisoformat(api_response['date'])
transaction.created_at = ensure_utc(api_date)

# BAD - Don't do this
transaction.created_at = api_date  # May not be UTC
```

## DateTime Fields Reference

| Model | Field | Stores | Display |
|-------|-------|--------|---------|
| ExpertRecommendation | created_at | UTC ✅ | format_for_display() |
| MarketAnalysis | created_at | UTC ✅ | format_for_display() |
| AnalysisOutput | created_at, start_date, end_date | UTC ✅ | format_for_display() |
| Transaction | open_date, close_date, created_at | UTC ✅ | format_for_display() |
| TradingOrder | created_at | UTC ✅ | format_for_display() |
| TradeActionResult | created_at | UTC ✅ | format_for_display() |

## Checklist for New Code

When you create/update code with dates:

- [ ] **Database**: Use `DateTime.now(timezone.utc)` for defaults
- [ ] **UI Display**: Use `format_for_display()` for all timestamps
- [ ] **User Input**: Use `local_to_utc()` before storing
- [ ] **API Data**: Use `ensure_utc()` before storing
- [ ] **Queries**: Remember all stored dates are UTC
- [ ] **Testing**: Verify dates appear in user's local timezone

## Documentation Files

1. **Quick this file**: Quick reference card
2. **`DATE_TIMEZONE_HANDLING_STANDARDS.md`**: Comprehensive guide with examples
3. **`DATE_TIMEZONE_IMPLEMENTATION_AUDIT.md`**: Current status and roadmap
4. **`DATE_TIMEZONE_REVIEW_SUMMARY.md`**: Executive summary

## Imports You'll Need

```python
# For UI display
from ba2_trade_platform.core.date_utils import format_for_display, format_relative

# For database operations
from datetime import datetime, timezone

# For data conversion
from ba2_trade_platform.core.date_utils import ensure_utc, local_to_utc, utc_to_local
```

## Mistakes to Avoid

```python
# ❌ DON'T - Naive datetime
transaction.open_date = datetime.now()

# ✅ DO - UTC datetime
transaction.open_date = datetime.now(timezone.utc)

# ❌ DON'T - Display UTC to user
ui.label(str(market_analysis.created_at))  # Shows UTC time

# ✅ DO - Convert to local
ui.label(format_for_display(market_analysis.created_at))  # Shows local time

# ❌ DON'T - Store user input as-is
transaction.open_date = user_entered_datetime

# ✅ DO - Convert to UTC
transaction.open_date = local_to_utc(user_entered_datetime)
```

## Help?

- **Models question**: Check `ba2_trade_platform/core/models.py` - all correct ✅
- **UI display question**: Check `DATE_TIMEZONE_HANDLING_STANDARDS.md` - UI patterns section
- **Data provider question**: Check `DATE_TIMEZONE_HANDLING_STANDARDS.md` - Data provider section
- **Timezone issue**: Check `DATE_TIMEZONE_IMPLEMENTATION_AUDIT.md` - Common pitfalls

## Summary

✅ **Models**: All DateTime fields correctly use UTC - no changes needed  
✅ **Utilities**: date_utils.py provides all needed functions  
📋 **UI**: 6 pages need to update date displays (use format_for_display)  
📋 **Providers**: 4+ data sources need UTC conversion audit  

**Start**: Import and use `format_for_display()` in your UI code today!
