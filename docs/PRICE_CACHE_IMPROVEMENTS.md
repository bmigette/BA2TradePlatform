# Price Cache and UI Performance Improvements

**Date:** October 13, 2025  
**Status:** ✅ Completed

## Problem Statement

### Issue 1: Price Cache Not Working
The price cache in `AccountInterface` was instance-level, causing cache loss every time a new account instance was created. This resulted in repeated API calls for the same symbol within the cache TTL window:

```
2025-10-13 22:52:43,520 - DEBUG - Cached new price for META: $710.99
2025-10-13 22:52:48,845 - DEBUG - Cached new price for META: $710.99
```

Each widget creation would instantiate a new account object, losing the cache.

### Issue 2: P/L Widgets Blocking UI
The Floating P/L widgets were performing synchronous database queries in the async method, potentially blocking the UI thread during heavy operations.

## Solution Implemented

### 1. Global Thread-Safe Price Cache

**Changes to `ba2_trade_platform/core/interfaces/AccountInterface.py`:**

#### Added Thread-Safe Global Cache
```python
from threading import Lock

class AccountInterface(ExtendableSettingsInterface):
    # Class-level price cache shared across all instances
    # Structure: {account_id: {symbol: {'price': float, 'timestamp': datetime}}}
    _GLOBAL_PRICE_CACHE: Dict[int, Dict[str, Dict[str, Any]]] = {}
    _CACHE_LOCK = Lock()  # Thread-safe access to cache
```

**Key Features:**
- **Global Cache**: Class-level variable persists across instance creation
- **Per-Account Indexing**: Cache is indexed by `account_id` for multi-account support
- **Thread-Safe**: Uses `threading.Lock()` to prevent race conditions
- **TTL Respected**: Still honors `PRICE_CACHE_TIME` from config.py (default: 60 seconds)

#### Updated Constructor
```python
def __init__(self, id: int):
    self.id = id
    # Ensure this account has an entry in the global cache
    with self._CACHE_LOCK:
        if self.id not in self._GLOBAL_PRICE_CACHE:
            self._GLOBAL_PRICE_CACHE[self.id] = {}
```

#### Refactored `get_instrument_current_price()` Method
```python
def get_instrument_current_price(self, symbol: str) -> Optional[float]:
    # Thread-safe cache access
    with self._CACHE_LOCK:
        account_cache = self._GLOBAL_PRICE_CACHE.get(self.id, {})
        
        if symbol in account_cache:
            cached_data = account_cache[symbol]
            time_diff = (datetime.now(timezone.utc) - cached_data['timestamp']).total_seconds()
            
            if time_diff < config.PRICE_CACHE_TIME:
                logger.debug(f"[Account {self.id}] Returning cached price for {symbol}: ${cached_data['price']} (age: {time_diff:.1f}s)")
                return cached_data['price']
    
    # Cache miss - fetch fresh price (outside lock to avoid blocking)
    price = self._get_instrument_current_price_impl(symbol)
    
    # Update cache if valid price (thread-safe)
    if price is not None:
        with self._CACHE_LOCK:
            if self.id not in self._GLOBAL_PRICE_CACHE:
                self._GLOBAL_PRICE_CACHE[self.id] = {}
            self._GLOBAL_PRICE_CACHE[self.id][symbol] = {
                'price': price,
                'timestamp': datetime.now(timezone.utc)
            }
    
    return price
```

**Performance Optimizations:**
- Lock is held only during cache read/write operations
- API calls (`_get_instrument_current_price_impl`) happen outside the lock
- Prevents blocking other threads during slow network operations

### 2. Non-Blocking P/L Widget Loading

**Changes to Both Widget Files:**
- `ba2_trade_platform/ui/components/FloatingPLPerExpertWidget.py`
- `ba2_trade_platform/ui/components/FloatingPLPerAccountWidget.py`

#### Extracted Synchronous Calculation Method
```python
def _calculate_pl_sync(self) -> Dict[str, float]:
    """Synchronous P/L calculation (runs in thread pool to avoid blocking)."""
    expert_pl = {}  # or account_pl
    
    session = get_db()
    try:
        # Get all open transactions
        transactions = session.exec(
            select(Transaction)
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
        ).all()
        
        # Calculate P/L for each transaction
        for trans in transactions:
            # ... database queries, price fetching, calculations ...
            pass
    finally:
        session.close()
    
    return expert_pl  # or account_pl
```

#### Updated Async Wrapper to Use Thread Pool
```python
async def _load_data_async(self, loading_label, content_container):
    """Calculate and display P/L (async wrapper for thread pool execution)."""
    try:
        # Run database queries in thread pool to avoid blocking UI
        loop = asyncio.get_event_loop()
        expert_pl = await loop.run_in_executor(None, self._calculate_pl_sync)
        
        # Clear loading message and display results (UI operations)
        # ... (existing display code remains unchanged) ...
```

**Benefits:**
- ✅ Database queries run in background thread pool
- ✅ UI remains responsive during P/L calculation
- ✅ No blocking on price fetches (which now use cached values)
- ✅ Loading state shows immediately while calculations run

## Testing

Created comprehensive test suite: `test_files/test_price_cache.py`

### Test Coverage

1. **Cache Persistence Test**
   - Verifies cache survives instance destruction
   - Creates two instances of same account
   - Confirms second instance uses cached price

2. **Per-Account Cache Test**
   - Tests cache isolation between accounts
   - Verifies each account has separate cache entries
   - Confirms no cache collision

3. **TTL Expiration Test**
   - Sets short cache TTL (3 seconds)
   - Verifies cache hit within TTL
   - Confirms cache miss after expiration
   - Verifies fresh price fetch after expiration

4. **Thread Safety Test**
   - Launches 5 concurrent threads
   - All threads fetch same symbol simultaneously
   - Verifies no race conditions or errors
   - Confirms all threads get consistent data

### Running Tests

```powershell
.venv\Scripts\python.exe test_files\test_price_cache.py
```

**Expected Output:**
```
================================================================================
TEST 1: Price Cache Persistence Across Instance Creation
================================================================================

1. First instance - Fetching price for META...
   Price: $710.99

2. Creating new instance of account 1...
3. New instance - Fetching price for META (should use cache)...
   [Account 1] Returning cached price for META: $710.99 (age: 0.1s)
   Price: $710.99

✅ SUCCESS: Cache persisted across instances ($710.99 == $710.99)

--------------------------------------------------------------------------------
TEST 2: Cache is Per-Account (account_id indexed)
...
```

## Configuration

The cache behavior is controlled by `config.py`:

```python
PRICE_CACHE_TIME = 60  # Default to 60 seconds
```

**Environment Variable:**
```bash
PRICE_CACHE_TIME=120  # Override to 120 seconds
```

**Recommended Values:**
- **Development:** 30-60 seconds (faster price updates)
- **Production:** 60-120 seconds (reduce API calls)
- **High-Frequency Trading:** 5-15 seconds (near real-time)

## Architecture Notes

### Cache Structure

```python
_GLOBAL_PRICE_CACHE = {
    1: {  # account_id
        'AAPL': {
            'price': 185.50,
            'timestamp': datetime(2025, 10, 13, 22, 52, 43, tzinfo=timezone.utc)
        },
        'META': {
            'price': 710.99,
            'timestamp': datetime(2025, 10, 13, 22, 52, 48, tzinfo=timezone.utc)
        }
    },
    2: {  # different account
        'AAPL': {
            'price': 185.50,
            'timestamp': datetime(2025, 10, 13, 22, 53, 10, tzinfo=timezone.utc)
        }
    }
}
```

### Why Per-Account Cache?

Different accounts may have:
- Different market data providers (live vs. paper)
- Different price feeds (delayed vs. real-time)
- Different geographic regions (pre-market vs. regular hours)

### Thread Safety Implementation

**Critical Section:** Only cache read/write operations
```python
with self._CACHE_LOCK:
    # Read/write cache
    pass
# Network operations outside lock
```

**Why This Matters:**
- Multiple UI widgets fetch prices concurrently
- Background tasks refresh positions/orders
- Expert agents analyze multiple symbols simultaneously
- Without locks: potential cache corruption, race conditions

## Performance Impact

### Before (Instance-Level Cache)
```
Widget 1: Fetch META → API call (200ms)
Widget 2: Fetch META → API call (200ms)  ❌ Duplicate call
Widget 3: Fetch META → API call (200ms)  ❌ Duplicate call
Total: 600ms + 3 API calls
```

### After (Global Cache)
```
Widget 1: Fetch META → API call (200ms) → Cache
Widget 2: Fetch META → Cache hit (0.1ms)  ✅ No API call
Widget 3: Fetch META → Cache hit (0.1ms)  ✅ No API call
Total: 200.2ms + 1 API call
```

**Savings:** 66% reduction in API calls, 67% faster total time

### UI Responsiveness

**Before (Synchronous DB Queries):**
- UI freezes during P/L calculation
- User sees loading spinner but can't interact
- Multi-second delays on large portfolios

**After (Thread Pool Execution):**
- UI remains fully responsive
- Users can navigate while P/L calculates
- Background processing doesn't block main thread

## Validation

### Code Quality
```powershell
# All files pass validation
get_errors -filePaths @(
    "ba2_trade_platform\core\interfaces\AccountInterface.py",
    "ba2_trade_platform\ui\components\FloatingPLPerExpertWidget.py",
    "ba2_trade_platform\ui\components\FloatingPLPerAccountWidget.py"
)
# Result: No errors found
```

### Log Output Verification
**Expected Log Pattern (Cache Working):**
```
[Account 1] Cached new price for META: $710.99
[Account 1] Returning cached price for META: $710.99 (age: 5.2s)
[Account 1] Returning cached price for META: $710.99 (age: 8.7s)
[Account 1] Cache expired for META (age: 61.3s > 60.0s)
[Account 1] Cached new price for META: $711.25
```

## Migration Notes

### Breaking Changes
- ⚠️ **None** - Backward compatible

### API Changes
- ✅ All public methods unchanged
- ✅ `get_instrument_current_price()` signature unchanged
- ✅ Return values unchanged

### Deployment Considerations

1. **Cache Warming:** First request after restart will be slow (cache empty)
2. **Memory Usage:** Minimal (~100 bytes per cached symbol)
3. **Thread Safety:** Tested with concurrent access
4. **Configuration:** `PRICE_CACHE_TIME` can be tuned per environment

## Future Enhancements

### Potential Improvements

1. **Cache Eviction Policy**
   - Current: Only TTL-based expiration
   - Future: LRU eviction for symbols not accessed recently
   - Benefit: Prevent unbounded cache growth

2. **Cache Preloading**
   - Preload common symbols on startup
   - Reduce initial latency for popular instruments

3. **Cross-Account Cache Sharing**
   - For live accounts using same data provider
   - Reduce API calls when multiple accounts track same symbols
   - Requires careful consideration of data source differences

4. **Cache Metrics**
   - Hit/miss rate tracking
   - Cache size monitoring
   - Performance analytics

## Files Changed

1. **Modified:**
   - `ba2_trade_platform/core/interfaces/AccountInterface.py` - Global thread-safe cache
   - `ba2_trade_platform/ui/components/FloatingPLPerExpertWidget.py` - Thread pool execution
   - `ba2_trade_platform/ui/components/FloatingPLPerAccountWidget.py` - Thread pool execution

2. **Created:**
   - `test_files/test_price_cache.py` - Comprehensive test suite
   - `docs/PRICE_CACHE_IMPROVEMENTS.md` - This documentation

## References

- **Issue Report:** Price cache not persisting (logs showing duplicate fetches)
- **Related:** P/L widgets blocking UI during calculation
- **Config:** `ba2_trade_platform/config.py` - `PRICE_CACHE_TIME` setting
- **Pattern:** Follows existing account interface architecture
