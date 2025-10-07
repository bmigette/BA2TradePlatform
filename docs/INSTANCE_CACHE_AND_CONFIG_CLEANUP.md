# Instance-Level Price Cache and Config Cleanup

**Date**: 2025-10-07  
**Status**: ✅ Completed

## Overview

This document describes the changes made to convert the price cache from class-level to instance-level storage and remove unused configuration variables.

## Changes Made

### 1. Price Cache: Class-Level → Instance-Level

**Rationale**: Each account instance should maintain its own price cache to ensure independent price data between different accounts (e.g., paper trading vs. live trading).

#### Before (Class-Level)
```python
class AccountInterface(ExtendableSettingsInterface):
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"
    
    # Class-level price cache: {symbol: {'price': float, 'timestamp': datetime}}
    _price_cache: Dict[str, Dict[str, Any]] = {}
    
    def __init__(self, id: int):
        self.id = id
```

**Problem**: All account instances shared the same cache, which could cause issues when:
- Multiple accounts exist (paper vs. live)
- Prices differ between broker endpoints
- Testing requires isolated price data

#### After (Instance-Level)
```python
class AccountInterface(ExtendableSettingsInterface):
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"
    
    def __init__(self, id: int):
        self.id = id
        # Instance-level price cache: {symbol: {'price': float, 'timestamp': datetime}}
        self._price_cache: Dict[str, Dict[str, Any]] = {}
```

**Benefits**:
- ✅ Each account instance has independent price cache
- ✅ Paper trading and live trading can have different cached prices
- ✅ Better isolation for testing and debugging
- ✅ Prevents cross-contamination between accounts

### 2. Removed ACCOUNT_REFRESH_INTERVAL from config.py

**Rationale**: Account refresh interval is managed through the `AppSetting` model in the database, not through environment variables.

#### Before
```python
# config.py
account_refresh_interval = 60  # Default to 60 minutes

def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, ALPHA_VANTAGE_API_KEY, FILE_LOGGING, account_refresh_interval, PRICE_CACHE_TIME
    
    # Load account refresh interval from environment, default to 60 minutes
    try:
        account_refresh_interval = int(os.getenv('ACCOUNT_REFRESH_INTERVAL', account_refresh_interval))
    except ValueError:
        account_refresh_interval = 60
```

#### After
```python
# config.py
# Removed account_refresh_interval entirely

def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, ALPHA_VANTAGE_API_KEY, FILE_LOGGING, PRICE_CACHE_TIME
    
    # account_refresh_interval managed through AppSetting in database
```

**Where account_refresh_interval is Actually Managed**:

1. **UI Settings** (`ba2_trade_platform/ui/pages/settings.py`):
   - Users configure refresh interval through UI
   - Stored as AppSetting with key `'account_refresh_interval'`
   - Default: 5 minutes (not 60)

2. **JobManager** (`ba2_trade_platform/core/JobManager.py`):
   - Reads refresh interval from AppSetting
   - Creates default AppSetting if not exists
   - Uses value to schedule account refresh jobs

**Benefits**:
- ✅ Single source of truth (database AppSetting)
- ✅ User-configurable through UI
- ✅ No environment variable confusion
- ✅ Removed dead code from config.py

## Files Modified

### 1. `ba2_trade_platform/core/AccountInterface.py`
**Changes**:
- Moved `_price_cache` from class-level to instance-level in `__init__()`
- Updated comment to indicate "Instance-level price cache"

### 2. `ba2_trade_platform/config.py`
**Changes**:
- Removed `account_refresh_interval = 60` variable declaration
- Removed `account_refresh_interval` from `global` statement in `load_config_from_env()`
- Removed ACCOUNT_REFRESH_INTERVAL environment variable loading code block

### 3. `TESTPLAN.md`
**Changes**:
- Updated test 11.1.1 expected results to clarify account_refresh_interval is database-managed
- Updated cache inspection example to use instance cache access:
  ```python
  account = get_account_instance(1)
  print(account._price_cache)  # View instance-specific cache
  ```

### 4. `docs/PRICE_CACHING_IMPLEMENTATION.md`
**Changes**:
- Updated "Cache Storage Structure" section to show instance-level implementation
- Added note about per-account instance isolation benefits
- Updated "Thread Safety" section with instance-level locking example
- Updated all test examples to use `account._price_cache` instead of `AccountInterface._price_cache`
- Updated "Manual Cache Clear" section with instance-specific examples
- Updated "Limitations" section to reflect instance-level behavior
- Updated "Future Enhancements" to remove "Per-Account Cache" (already implemented)

## Usage Examples

### Accessing Price Cache (After Changes)

```python
from ba2_trade_platform.core.AccountInterface import get_account_instance

# Get account instance
account = get_account_instance(1)

# View this account's price cache
print(account._price_cache)

# Clear this account's price cache
account._price_cache.clear()

# Remove specific symbol from this account's cache
if "AAPL" in account._price_cache:
    del account._price_cache["AAPL"]
```

### Multiple Account Instances

```python
from ba2_trade_platform.core.AccountInterface import get_account_instance

# Paper trading account
paper_account = get_account_instance(1)
paper_price = paper_account.get_instrument_current_price("AAPL")

# Live trading account
live_account = get_account_instance(2)
live_price = live_account.get_instrument_current_price("AAPL")

# Each account has independent cache
print(f"Paper cache: {paper_account._price_cache}")
print(f"Live cache: {live_account._price_cache}")
# These are separate dictionaries
```

## Migration Notes

### No Database Migration Required
- Price cache is runtime-only (not persisted)
- No database schema changes
- Existing code continues to work

### Code Compatibility
- All existing code using `get_instrument_current_price()` works unchanged
- Cache is transparent to callers
- Only direct cache access code needs updating (if any exists)

## Testing Verification

Run the following to verify changes:

```python
from ba2_trade_platform.core.AccountInterface import get_account_instance

# Test 1: Verify instance-level cache
account1 = get_account_instance(1)
account2 = get_account_instance(1)  # Different instance, same account ID

# Fetch price in first instance
price1 = account1.get_instrument_current_price("AAPL")
assert "AAPL" in account1._price_cache

# Second instance should have empty cache (different object)
assert "AAPL" not in account2._price_cache

print("✅ Instance-level cache verified")
```

```python
# Test 2: Verify account_refresh_interval removed from config
from ba2_trade_platform import config

# Should not exist
assert not hasattr(config, 'account_refresh_interval')

# PRICE_CACHE_TIME should still exist
assert hasattr(config, 'PRICE_CACHE_TIME')
assert config.PRICE_CACHE_TIME == 30

print("✅ Config cleanup verified")
```

## Benefits Summary

### Instance-Level Cache
1. **Isolation**: Each account instance has independent cache
2. **Accuracy**: Paper and live accounts don't share prices
3. **Testing**: Easier to test without cache pollution
4. **Debugging**: Can inspect cache per account instance

### Config Cleanup
1. **Simplicity**: One less environment variable to manage
2. **Consistency**: account_refresh_interval only in database (single source of truth)
3. **User-Friendly**: Configurable through UI, not environment files
4. **No Confusion**: Clear separation between env-configurable (PRICE_CACHE_TIME) and UI-configurable (account_refresh_interval) settings

## Related Documentation

- `PRICE_CACHING_IMPLEMENTATION.md` - Complete price caching feature documentation
- `TESTPLAN.md` - Section 3 (Price Caching) and Section 11 (Configuration)
- `FILLED_AVG_PRICE_REMOVAL_AND_UI_ENHANCEMENTS.md` - Related price handling improvements
