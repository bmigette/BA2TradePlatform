# Setting Default Refactoring

**Date**: November 11, 2025  
**Issue**: Position sizing was defaulting to unlimited (100%) instead of respecting expert's configured allocation  
**Root Cause**: Hardcoded default values (e.g., `10.0`, `100.0`) scattered throughout the codebase, disconnected from interface definitions

## Problem

The `max_virtual_equity_per_instrument_percent` setting had:
- **Interface definition default**: 10.0% (documented)
- **Code defaults**: 100.0% (unlimited)

This caused Run #22 to allocate 7% of account instead of 5% because:
1. Expert TA-Dynamic-grok had no explicit setting configured
2. Code used hardcoded default of 100% (unlimited position size)
3. LLM allocated $730/position × 7 = $5,110 (102% of $5,006.62 virtual_equity)
4. Result: 7% of $50k account instead of intended 5% expert allocation

## Solution

Created centralized helper functions to retrieve setting defaults from interface definitions instead of hardcoding them:

### New Helper Functions in `core/utils.py`

#### 1. `get_setting_default_from_interface(interface_class, setting_key)`
Retrieves the default value from an interface class's setting definition.

**Example:**
```python
from ba2_trade_platform.core.interfaces import MarketExpertInterface
from ba2_trade_platform.core.utils import get_setting_default_from_interface

default = get_setting_default_from_interface(
    MarketExpertInterface, 
    "max_virtual_equity_per_instrument_percent"
)
# Returns: 10.0
```

**Benefits:**
- Single source of truth for defaults
- Easier to update defaults (change in interface only)
- Fails explicitly if setting doesn't exist

#### 2. `get_setting_with_interface_default(settings, interface_class, setting_key, log_warning=True)`
Gets a setting value from the settings dict, falling back to interface default with warning.

**Example:**
```python
from ba2_trade_platform.core.utils import get_setting_with_interface_default

# If setting not configured:
value = get_setting_with_interface_default(
    expert.settings, 
    MarketExpertInterface, 
    "max_virtual_equity_per_instrument_percent"
)
# Logs warning: "Using interface default for setting 'max_virtual_equity_per_instrument_percent': 10.0"
# Returns: 10.0

# If setting is configured:
value = get_setting_with_interface_default(
    expert.settings, 
    MarketExpertInterface, 
    "max_virtual_equity_per_instrument_percent"
)
# Logs nothing
# Returns: configured value
```

**Benefits:**
- Automatic fallback to interface defaults
- Logs warnings when using defaults (visibility into configuration)
- Handles None values gracefully

## Changed Files

### 1. `core/utils.py`
- Added `get_setting_default_from_interface()` function
- Added `get_setting_with_interface_default()` function
- Both functions use `get_merged_settings_definitions()` to get interface defaults

### 2. `core/SmartRiskManagerGraph.py`
Updated 4 locations to use helper instead of hardcoded defaults:

**Line 1214** (expert_config initialization):
```python
# Before:
"max_virtual_equity_per_instrument_percent": settings.get("max_virtual_equity_per_instrument_percent", 10.0)

# After:
"max_virtual_equity_per_instrument_percent": get_setting_with_interface_default(
    settings, MarketExpertInterface, "max_virtual_equity_per_instrument_percent"
)
```

**Line 1720** (research_node position sizing):
- Changed from `.get(..., 10.0)` to `get_setting_with_interface_default(...)`

**Line 2207** (analysis_node position sizing):
- Changed from `.get(..., 10.0)` to `get_setting_with_interface_default(...)`

**Line 3439** (summary_node position sizing):
- Changed from `.get(..., 10.0)` to `get_setting_with_interface_default(...)`

### 3. `core/SmartRiskManagerToolkit.py`
**Line 1929** (position size limit check):
```python
# Before:
max_position_pct = settings.get("max_virtual_equity_per_instrument_percent", 10.0)

# After:
max_position_pct = get_setting_with_interface_default(
    settings, MarketExpertInterface, "max_virtual_equity_per_instrument_percent"
)
```

## Benefits

1. **Single Source of Truth**: All defaults come from interface definitions
2. **Visibility**: Warnings logged when using defaults
3. **Maintainability**: Change default once in interface, applies everywhere
4. **Consistency**: All settings can use this pattern, not just `max_virtual_equity_per_instrument_percent`
5. **Type Safety**: Helper catches missing settings with clear error messages

## Testing

Helper functions tested with:
1. ✓ Getting default from interface
2. ✓ Falling back to default when setting not configured
3. ✓ Returning configured value when set
4. ✓ Handling None values (treating as not configured)
5. ✓ Logging warnings on fallback to default

## Logs to Expect

When an expert doesn't have a setting configured, you'll see:
```
WARNING - Using interface default for setting 'max_virtual_equity_per_instrument_percent': 10.0
```

This indicates:
- The expert has no explicit setting configured
- Using the interface default (10.0%)
- This is the correct behavior - no action needed

## Migration Guide

To use this pattern for other settings:

```python
# Old pattern (hardcoded default):
value = settings.get("some_setting", default_value)

# New pattern (interface default):
from ba2_trade_platform.core.utils import get_setting_with_interface_default
from ba2_trade_platform.core.interfaces import MarketExpertInterface

value = get_setting_with_interface_default(
    settings, 
    MarketExpertInterface, 
    "some_setting",
    log_warning=True  # Optional, default is True
)
```

## Related Issues Fixed

- **Issue**: Run #22 (TA-Dynamic-grok) allocated 7% instead of 5%
  - **Fix**: Now uses 10% default (from interface), expert can override to 5% if desired
- **Issue**: Inconsistent defaults across codebase
  - **Fix**: All defaults now come from single source (interface definitions)
