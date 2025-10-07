# Database Locking Fix - October 7, 2025

## Problem
SQLite database locking errors occurring during concurrent order submissions:
```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
[SQL: UPDATE tradingorder SET status=?, broker_order_id=? WHERE tradingorder.id = ?]
```

## Root Causes
1. **No WAL Mode**: SQLite was using default rollback journal, limiting concurrency
2. **Low Timeout**: Default SQLite busy timeout (5 seconds) too short for concurrent operations
3. **Raw Sessions**: AlpacaAccount creating sessions with `Session(get_db().bind)` bypassing thread lock
4. **No Retry Logic**: Database operations failed immediately on lock without retry

## Solution Implemented

### 1. SQLite WAL Mode (Write-Ahead Logging)
**File**: `ba2_trade_platform/core/db.py`

Added event listener to enable WAL mode on every connection:
```python
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")  # Multiple readers + 1 writer
    cursor.execute("PRAGMA synchronous=NORMAL")  # Faster while safe
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
    cursor.close()
```

**Benefits**:
- Multiple readers can access database simultaneously
- Writers don't block readers
- Significantly better concurrency for web UI + trading operations

### 2. Connection Args Update
**File**: `ba2_trade_platform/core/db.py`

```python
engine = create_engine(
    f"sqlite:///{DB_FILE}", 
    connect_args={
        "check_same_thread": False,
        "timeout": 30.0,  # SQLite busy timeout in seconds
    },
    pool_size=20,
    max_overflow=40,
    pool_timeout=60,
    pool_recycle=3600,
    pool_pre_ping=True,
    echo=False
)
```

### 3. Retry Decorator with Exponential Backoff
**File**: `ba2_trade_platform/core/db.py`

```python
def retry_on_lock(func):
    """Retry database operations on lock errors with exponential backoff."""
    def wrapper(*args, **kwargs):
        max_retries = 5
        base_delay = 0.1  # Start with 100ms
        
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "database is locked" in str(e).lower():
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)  # Exponential backoff
                        logger.warning(f"Database locked, retrying in {delay:.2f}s")
                        time.sleep(delay)
                    else:
                        raise
                else:
                    raise
    return wrapper
```

Applied to:
- `add_instance()`
- `update_instance()`

**Retry Schedule**:
- Attempt 1: Immediate
- Attempt 2: 100ms delay
- Attempt 3: 200ms delay
- Attempt 4: 400ms delay
- Attempt 5: 800ms delay
- Total max wait: ~1.5 seconds

### 4. AlpacaAccount Thread-Safe Updates
**File**: `ba2_trade_platform/modules/accounts/AlpacaAccount.py`

**Before** (causing locks):
```python
with Session(get_db().bind) as session:
    fresh_order = session.get(TradingOrder, trading_order.id)
    fresh_order.status = OrderStatus.PENDING_NEW
    session.add(fresh_order)
    session.commit()
```

**After** (thread-safe with retry):
```python
fresh_order = get_instance(TradingOrder, trading_order.id)
fresh_order.status = OrderStatus.PENDING_NEW
update_instance(fresh_order)  # Uses thread lock + retry logic
```

**Locations Fixed**:
1. `_submit_order_impl()` - Order submission success path (line ~357)
2. `_submit_order_impl()` - Order submission error handling (line ~382)

## Remaining Work

### Critical - Session Usage in Other Methods
The following methods still use `Session(get_db().bind)` and need conversion:

1. **`sync_orders()` (line 605)**: Syncs Alpaca order states to database
   - High volume operation during market hours
   - Updates multiple orders in single session
   - **Risk**: High - runs periodically, can conflict with order submissions

2. **`cancel_order()` (line 687)**: Cancels orders and TPs
   - Updates order status and linked TP orders
   - **Risk**: Medium - less frequent but critical for risk management

3. **`_ensure_order_in_session()` (line 722)**: Session management helper
   - Used by cancel_order
   - **Risk**: Low - internal helper, but needs fixing for cancel_order

4. **`get_open_positions()` (line 762)**: Fetches open positions
   - Read-only operation
   - **Risk**: Low - reads don't cause locks in WAL mode

### Recommended Fix Pattern

For **write operations** (sync_orders, cancel_order):
```python
# Instead of:
with Session(get_db().bind) as session:
    order = session.get(TradingOrder, order_id)
    order.status = new_status
    session.commit()

# Use:
order = get_instance(TradingOrder, order_id)
order.status = new_status
update_instance(order)  # Thread-safe + retry
```

For **batch updates** (sync_orders with multiple orders):
```python
# Consider adding batch_update_instances() to db.py:
@retry_on_lock
def batch_update_instances(instances, session=None):
    \"\"\"Update multiple instances in single transaction.\"\"\"
    with _db_write_lock:
        if not session:
            session = Session(engine)
        try:
            for instance in instances:
                session.add(instance)
            session.commit()
            for instance in instances:
                session.refresh(instance)
        except Exception as e:
            session.rollback()
            raise
```

## Testing Recommendations

1. **Load Test**: Submit 10+ orders simultaneously to test retry logic
2. **UI Stress Test**: Open multiple browser tabs, interact with different pages
3. **Monitor Logs**: Check for "Database locked, retrying" warnings
4. **WAL Verification**: Run `PRAGMA journal_mode` to confirm WAL active

## Performance Impact

**Expected Improvements**:
- ✅ Elimination of "database is locked" errors (90%+ reduction)
- ✅ Faster concurrent reads (WAL mode)
- ✅ Graceful degradation under load (retry logic)
- ⚠️ Slight overhead from retry decorator (~1ms per operation)
- ⚠️ WAL mode uses more disk space (WAL + SHM files)

## Migration Notes

**No Data Migration Required**: WAL mode activates on first connection after restart.

**Verification**:
```bash
# Check if WAL mode is active
sqlite3 ~/Documents/ba2_trade_platform/db.sqlite "PRAGMA journal_mode;"
# Should return: wal
```

**Rollback** (if needed):
```python
# In db.py, comment out WAL pragma:
# cursor.execute("PRAGMA journal_mode=WAL")
```

## References
- [SQLite WAL Mode](https://www.sqlite.org/wal.html)
- [SQLAlchemy Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- [Python threading.Lock](https://docs.python.org/3/library/threading.html#lock-objects)
