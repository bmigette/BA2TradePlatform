# FMP Rating Expert Settings Type Conversion Fix

**Date:** October 9, 2025  
**Issue:** `TypeError: '<' not supported between instances of 'int' and 'str'`

## Problem

The FMPRating expert was failing during analysis with a TypeError when comparing analyst counts:

```python
if analyst_count < min_analysts:  # ❌ TypeError
```

### Error Details

```
TypeError: '<' not supported between instances of 'int' and 'str'
File: FMPRating.py, line 206
Context: if analyst_count < min_analysts:
```

The error occurred when:
- `analyst_count` = `5` (integer)
- `min_analysts` = `"3"` (string from database)

## Root Cause

Settings retrieved from the database via `self.settings.get()` return **string values**, even when the setting definition specifies numeric types like `"int"` or `"float"`.

### Settings Definition

```python
@classmethod
def get_settings_definitions(cls) -> Dict[str, Any]:
    return {
        "profit_ratio": {
            "type": "float",  # ← Defined as float
            "default": 1.0,
            ...
        },
        "min_analysts": {
            "type": "int",  # ← Defined as int
            "default": 3,
            ...
        }
    }
```

### Database Storage

Settings are stored in the `expertsetting` table with these columns:
- `value_str` - String values
- `value_float` - Float values
- `value_json` - JSON values

However, the `ExtendableSettingsInterface.settings` property getter doesn't automatically convert types based on the definition - it just returns the raw value from the database.

### Problematic Code

```python
# Line ~436 (run_analysis method)
profit_ratio = self.settings.get('profit_ratio', 1.0)  # ❌ Returns "1.0" string
min_analysts = self.settings.get('min_analysts', 3)    # ❌ Returns "3" string

# Line ~206 (_calculate_recommendation method)
if analyst_count < min_analysts:  # ❌ TypeError: comparing int < str
```

## Solution

Added explicit type conversion when retrieving settings to ensure they match their declared types.

### Fixed Code

```python
# Line ~435-437 (run_analysis method)
# Get settings with proper type conversion
profit_ratio = float(self.settings.get('profit_ratio', 1.0))  # ✅ Convert to float
min_analysts = int(self.settings.get('min_analysts', 3))      # ✅ Convert to int

# Line ~726-728 (render_analysis_report method)
# Settings with proper type conversion
profit_ratio = float(settings.get('profit_ratio', 1.0))  # ✅ Convert to float
min_analysts = int(settings.get('min_analysts', 3))      # ✅ Convert to int
```

## Changes Made

### File: `ba2_trade_platform/modules/experts/FMPRating.py`

#### 1. run_analysis() method (line ~436)
**Before:**
```python
profit_ratio = self.settings.get('profit_ratio', 1.0)
min_analysts = self.settings.get('min_analysts', 3)
```

**After:**
```python
profit_ratio = float(self.settings.get('profit_ratio', 1.0))
min_analysts = int(self.settings.get('min_analysts', 3))
```

#### 2. render_analysis_report() method (line ~727)
**Before:**
```python
profit_ratio = settings.get('profit_ratio', 1.0)
min_analysts = settings.get('min_analysts', 3)
```

**After:**
```python
profit_ratio = float(settings.get('profit_ratio', 1.0))
min_analysts = int(settings.get('min_analysts', 3))
```

## Impact

### Fixed Operations

✅ **Analyst count comparison** - `analyst_count < min_analysts` now works correctly  
✅ **Confidence calculation** - `min(20, (analyst_count - min_analysts) * 2)` now works  
✅ **Profit ratio calculations** - All profit calculations use proper float arithmetic  
✅ **UI display** - Settings display correctly in analysis reports

### Example Usage

```python
# Settings stored in database
expert_setting.value_str = "5"  # min_analysts setting

# Without fix
min_analysts = self.settings.get('min_analysts', 3)
print(type(min_analysts))  # <class 'str'>
print(min_analysts)        # "5"
analyst_count = 10
if analyst_count < min_analysts:  # ❌ TypeError!
    pass

# With fix
min_analysts = int(self.settings.get('min_analysts', 3))
print(type(min_analysts))  # <class 'int'>
print(min_analysts)        # 5
analyst_count = 10
if analyst_count < min_analysts:  # ✅ Works correctly (10 < 5 = False)
    pass
```

## Why This Happens

The `ExtendableSettingsInterface` stores settings in different database columns based on type:
- Strings → `value_str`
- Floats → `value_float`  
- JSON → `value_json`

However, the `settings` property getter doesn't have access to the type definitions from `get_settings_definitions()`, so it can't automatically convert types. It just returns whatever is in the appropriate column.

**Current behavior:** Returns the raw value from database  
**Expected behavior:** Convert based on type definition (future enhancement)

## Best Practice

Always explicitly convert settings to their expected types when retrieving them:

```python
# ✅ CORRECT - Explicit type conversion
int_setting = int(self.settings.get('some_int_setting', 0))
float_setting = float(self.settings.get('some_float_setting', 0.0))
bool_setting = bool(self.settings.get('some_bool_setting', False))
str_setting = str(self.settings.get('some_str_setting', ''))

# ❌ WRONG - Assumes type is correct
int_setting = self.settings.get('some_int_setting', 0)  # May return string "0"
```

## Related Issues

This same issue could affect other experts if they:
1. Define numeric settings (int/float)
2. Retrieve them without type conversion
3. Use them in numeric operations or comparisons

### Potentially Affected Experts

Search for similar patterns in:
- ✅ `FMPRating.py` - Fixed
- 🔍 `FinnHubRating.py` - Check if it has numeric settings
- 🔍 `TradingAgents.py` - Check if it has numeric settings
- 🔍 Other custom experts - Check for numeric settings

## Future Improvement

Consider enhancing `ExtendableSettingsInterface` to automatically convert types based on `get_settings_definitions()`:

```python
def get_typed_setting(self, key: str, default: Any = None) -> Any:
    """Get setting with automatic type conversion based on definition."""
    value = self.settings.get(key, default)
    definitions = self.get_settings_definitions()
    
    if key in definitions:
        setting_type = definitions[key].get('type')
        if setting_type == 'int':
            return int(value)
        elif setting_type == 'float':
            return float(value)
        elif setting_type == 'bool':
            return bool(value)
    
    return value
```

This would allow:
```python
min_analysts = self.get_typed_setting('min_analysts', 3)  # Auto-converts to int
```

## Testing

To verify the fix:

```python
# Test with FMPRating expert
from ba2_trade_platform.modules.experts import FMPRating
from ba2_trade_platform.core.models import ExpertInstance

# Create expert instance
expert = FMPRating(expert_instance_id=1)

# Set settings via database (stored as strings)
expert.settings['min_analysts'] = '5'
expert.settings['profit_ratio'] = '1.5'

# Run analysis - should no longer crash
result = expert.run_analysis('AAPL', market_analysis)

# Verify type conversion worked
print(f"min_analysts type: {type(expert.settings.get('min_analysts'))}")  # Should work without error
```

## Documentation Updated

- ✅ Created `FMP_RATING_SETTINGS_TYPE_FIX.md`
- 📝 Consider adding to developer guide about setting type conversions
