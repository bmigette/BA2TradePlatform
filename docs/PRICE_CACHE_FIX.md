# Price Cache System Fix - Bid/Ask/Mid Separation

**Date:** 2025-10-15  
**Status:** COMPLETED ✅  
**Bug Severity:** CRITICAL (could cause wrong prices in trading calculations)

## Problem Statement

The price cache system in `AccountInterface.get_instrument_current_price()` did NOT distinguish between different price types (bid/ask/mid). Cache keys only used symbol names, causing:

### The Bug
```python
# OLD (BROKEN) cache behavior:
cache['AAPL'] = 234.17  # Stored without price_type

# Request bid price for AAPL
get_instrument_current_price('AAPL', price_type='bid')  # API call, caches as cache['AAPL'] = 234.17

# Request ask price for AAPL
get_instrument_current_price('AAPL', price_type='ask')  # Returns cached 234.17 (WRONG! Should be 259.04)
```

### Impact
- **Wrong prices**: Requesting ask price would return cached bid price (or vice versa)
- **Financial risk**: Trading calculations using wrong prices could lead to losses
- **P/L errors**: Widgets using different price types would show incorrect values

## Root Cause

**File:** `ba2_trade_platform/core/interfaces/AccountInterface.py`

### Cache Key Format (OLD)
```python
# Single symbol
if symbol in account_cache:  # ❌ Only checks symbol, not price_type
    cached_data = account_cache[symbol]

# Bulk fetch
for symbol in symbols:
    if symbol in account_cache:  # ❌ Only checks symbol, not price_type
        result[symbol] = account_cache[symbol]['price']
```

## Solution

### Cache Key Format (NEW)
Include price_type in cache keys: `"symbol:price_type"`

```python
# Single symbol
cache_key = f"{symbol}:{price_type}"  # ✅ e.g., "AAPL:bid", "AAPL:ask"
if cache_key in account_cache:
    cached_data = account_cache[cache_key]

# Bulk fetch
for symbol in symbols:
    cache_key = f"{symbol}:{price_type}"  # ✅ Separate cache entries
    if cache_key in account_cache:
        result[symbol] = account_cache[cache_key]['price']
```

## Changes Made

### 1. AccountInterface.py - Method Signature (Line 566)
```python
# Added price_type parameter with default
def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
```

### 2. AccountInterface.py - Single Symbol Cache (Lines ~598-655)
**Before:**
- Cache key: `symbol`
- Lookup: `if symbol in account_cache:`
- Store: `account_cache[symbol] = {price, timestamp}`

**After:**
- Cache key: `f"{symbol}:{price_type}"`
- Lookup: `if cache_key in account_cache:`
- Store: `account_cache[cache_key] = {price, timestamp}`
- Symbol lock: Uses `cache_key` instead of `symbol`
- Implementation call: Passes `price_type=price_type`

### 3. AccountInterface.py - Bulk Fetch Cache (Lines ~666-723)
**Before:**
- Cache check: `if symbol in account_cache:`
- Cache store: `account_cache[symbol] = {price, timestamp}`
- Implementation call: `_get_instrument_current_price_impl(symbols_to_fetch)`

**After:**
- Cache check: `cache_key = f"{symbol}:{price_type}"` then `if cache_key in account_cache:`
- Cache store: `account_cache[cache_key] = {price, timestamp}`
- Implementation call: `_get_instrument_current_price_impl(symbols_to_fetch, price_type=price_type)`

### 4. AccountInterface.py - Abstract Method (Line 534)
Updated signature to match implementations:
```python
@abstractmethod
def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
```

### 5. AlpacaAccount.py - Mid Price Support (Line 672)
Added 'mid' as alias for 'avg' price_type:
```python
elif price_type in ('avg', 'mid'):  # Support both names
    return (bid_price + ask_price) / 2
```

## Verification

### Test Script: `test_files/test_price_cache_fix.py`

**Test 1: Single Symbol Cache Separation**
- ✅ Fetch bid for AAPL → cached as `AAPL:bid`
- ✅ Fetch ask for AAPL → cached as `AAPL:ask` (different value)
- ✅ Fetch mid for AAPL → cached as `AAPL:mid` (correctly calculated)
- ✅ Verify bid ≠ ask (spread: 10.62%)
- ✅ Cache hits return correct prices for each type

**Test 2: Bulk Fetch Cache Separation**
- ✅ Bulk fetch bid for [AAPL, MSFT, GOOGL] → all cached with `:bid` suffix
- ✅ Bulk fetch ask for [AAPL, MSFT, GOOGL] → all cached with `:ask` suffix
- ✅ Verify bid ≠ ask for all symbols

**Test Results:**
```
================================================================================
✓✓✓ ALL TESTS PASSED ✓✓✓
Price cache correctly distinguishes bid/ask/mid in both single and bulk fetches
================================================================================
```

### Example Cache State After Fix
```python
account_cache = {
    'AAPL:bid': {'price': 234.17, 'timestamp': ...},
    'AAPL:ask': {'price': 259.04, 'timestamp': ...},
    'AAPL:mid': {'price': 246.61, 'timestamp': ...},
    'MSFT:bid': {'price': 495.00, 'timestamp': ...},
    'MSFT:ask': {'price': 536.00, 'timestamp': ...},
    # ... separate entries for each symbol:price_type combination
}
```

## Price Type Support

The system now correctly supports three price types:

1. **`'bid'`** (default): Bid price (price you can sell at)
2. **`'ask'`**: Ask price (price you can buy at)
3. **`'mid'`** or **`'avg'`**: Mid-point = (bid + ask) / 2

## Backward Compatibility

✅ **Fully backward compatible** - default `price_type='bid'` matches old behavior for code not specifying price_type.

## Files Modified

1. ✅ `ba2_trade_platform/core/interfaces/AccountInterface.py`
   - Lines 534-549: Updated abstract method signature
   - Lines 566-572: Added price_type parameter to public method
   - Lines 598-655: Fixed single symbol cache logic
   - Lines 666-723: Fixed bulk fetch cache logic

2. ✅ `ba2_trade_platform/modules/accounts/AlpacaAccount.py`
   - Lines 672-678: Added 'mid' as alias for 'avg' price_type

3. ✅ `test_files/test_price_cache_fix.py` (NEW)
   - Comprehensive test suite verifying cache separation

## Related Context

This fix was discovered while investigating P/L widget discrepancies:
- Widgets were using bid prices from cache
- Broker positions use different `current_price` (mark price)
- User identified cache bug: "maybe you need to adjust the price cache system to disting bid / ask cache"

See related fixes:
- `FloatingPLPerExpertWidget.py`: Now uses broker position prices instead of cached bid
- `FloatingPLPerAccountWidget.py`: Same fix applied

## Deployment Notes

⚠️ **Cache Invalidation**: Existing cache entries use old format (`symbol` only). After deployment:
- Old cache entries will expire naturally (TTL from `config.PRICE_CACHE_TIME`)
- New requests create entries with new format (`symbol:price_type`)
- No migration needed - cache is runtime-only (not persisted to database)

## Success Metrics

✅ **All tests pass** with 100% accuracy  
✅ **Cache correctly separates** bid/ask/mid prices  
✅ **No performance degradation** - bulk fetch still optimized  
✅ **Backward compatible** - existing code works without changes  
✅ **Financial safety** - prevents wrong prices in trading calculations
