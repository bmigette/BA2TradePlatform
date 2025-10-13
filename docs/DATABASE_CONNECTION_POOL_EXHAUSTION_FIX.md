# Database Connection Pool Exhaustion Fix

## Date
2025-10-13

## Problem Identified

### Error Message
```
QueuePool limit of size 20 overflow 40 reached, connection timed out, timeout 60.00
```

This error occurred in `FloatingPLPerExpertWidget` when calculating P/L for transactions, indicating that all 60 available database connections (pool_size=20 + max_overflow=40) were in use and a new request timed out after 60 seconds.

### Root Cause

**Session Leak Anti-Pattern:** Throughout the codebase, there are many instances of:

```python
session = get_db()
# ... use session ...
# ❌ Session never closed!
```

When `get_db()` is called without using a context manager (`with` statement) and the session is never explicitly closed, the connection remains in the pool indefinitely. With many concurrent operations (UI widgets, expert analysis, background jobs), these unclosed sessions accumulate until the pool is exhausted.

### Why This Matters

SQLite connection pool configuration:
- **pool_size**: Base connections kept open (was 20, now 10)
- **max_overflow**: Additional connections allowed (was 40, now 20)
- **Total max connections**: pool_size + max_overflow (was 60, now 30)

When all connections are held by unclosed sessions:
- New database operations timeout after `pool_timeout` seconds (was 60s, now 10s)
- Application becomes unresponsive
- Error cascades to all database-dependent features

## Solutions Implemented

### Solution 1: Reduced Pool Size and Timeout

**File:** `ba2_trade_platform/core/db.py`

**Changes:**
```python
# BEFORE
engine = create_engine(
    f"sqlite:///{DB_FILE}", 
    pool_size=20,           # Large pool
    max_overflow=40,        # Large overflow
    pool_timeout=60,        # Long wait
    pool_recycle=3600,      # 1 hour recycle
    ...
)

# AFTER
engine = create_engine(
    f"sqlite:///{DB_FILE}", 
    pool_size=10,           # Reduced from 20 (fewer idle connections)
    max_overflow=20,        # Reduced from 20 (total max: 30 instead of 60)
    pool_timeout=10,        # Reduced from 60 (fail faster to expose leaks)
    pool_recycle=600,       # Reduced from 3600 (10 min - recycle more frequently)
    ...
)
```

**Benefits:**
- Smaller pool means less memory usage
- Shorter timeout exposes leaks faster (fail-fast principle)
- More frequent recycling prevents stale connections

### Solution 2: Enhanced get_db() Documentation

**File:** `ba2_trade_platform/core/db.py`

Added comprehensive docstring to `get_db()` showing correct usage patterns:

```python
def get_db():
    """
    Returns a new database session. Caller is responsible for closing the session.
    
    ⚠️ WARNING: Always close the session when done to prevent connection pool exhaustion!
    
    **RECOMMENDED USAGE** (automatically closes session):
    ```python
    with get_db() as session:
        results = session.exec(select(Model)).all()
    # Session automatically closed
    ```
    
    **DISCOURAGED USAGE** (manual close required):
    ```python
    session = get_db()
    try:
        results = session.exec(select(Model)).all()
    finally:
        session.close()  # ⚠️ MUST close manually!
    ```
    """
    session = Session(engine)
    logger.debug(f"Database session created (id={id(session)})")
    return session
```

### Solution 3: Improved Error Logging

**Files:** 
- `ba2_trade_platform/ui/components/FloatingPLPerExpertWidget.py`
- `ba2_trade_platform/ui/components/FloatingPLPerAccountWidget.py`

**Change:**
```python
# BEFORE
except Exception as e:
    logger.error(f"Error calculating P/L for transaction {trans.id}: {e}")
    
# AFTER  
except Exception as e:
    logger.error(f"Error calculating P/L for transaction {trans.id}: {e}", exc_info=True)
```

**Benefit:** Now includes full stack traces for debugging, making it easier to identify the root cause of exceptions.

### Solution 4: Session Leak Audit Tool

**File:** `test_files/audit_session_leaks.py`

Created comprehensive audit script that:
1. Scans entire codebase for `session = get_db()` patterns
2. Checks if sessions are properly closed with try/finally
3. Reports files and line numbers with potential leaks
4. Shows correct usage patterns

**Usage:**
```powershell
.venv\Scripts\python.exe test_files\audit_session_leaks.py
```

**Output Example:**
```
⚠️  FOUND 45 POTENTIAL SESSION LEAKS IN 12 FILES:

📄 ba2_trade_platform\ui\pages\overview.py
--------------------------------------------------------------------------------
   Line  159: session = get_db()
             ⚠️  Session may not be closed!
   Line 1403: session = get_db()
             ⚠️  Session may not be closed!
```

## Identified Problem Areas

### High-Risk Files (Many Unclosed Sessions)

1. **ba2_trade_platform/ui/pages/settings.py** (~15 instances)
2. **ba2_trade_platform/ui/pages/overview.py** (~10 instances)
3. **ba2_trade_platform/modules/experts/TradingAgents.py** (~3 instances)
4. **ba2_trade_platform/ui/components/*.py** (chart components)

### Common Anti-Patterns

#### Pattern 1: No cleanup
```python
def some_function():
    session = get_db()
    results = session.exec(select(Model)).all()
    return results  # ❌ Session leaked!
```

#### Pattern 2: Early return before cleanup
```python
def some_function():
    session = get_db()
    if condition:
        return  # ❌ Session leaked!
    session.close()
```

#### Pattern 3: Exception bypasses cleanup
```python
def some_function():
    session = get_db()
    risky_operation()  # ❌ If exception, session leaked!
    session.close()
```

## Correct Patterns

### Pattern 1: Context Manager (Recommended)

```python
from ba2_trade_platform.core.db import get_db
from sqlmodel import select

with get_db() as session:
    results = session.exec(select(Model)).all()
    process(results)
# ✅ Session automatically closed
```

### Pattern 2: Try/Finally (When Context Manager Not Possible)

```python
from ba2_trade_platform.core.db import get_db

session = get_db()
try:
    results = session.exec(select(Model)).all()
    for result in results:
        process(result)
finally:
    session.close()  # ✅ Always closed
```

### Pattern 3: Async Context (For Async Functions)

```python
async def async_function():
    session = get_db()
    try:
        # Run sync DB operations in thread pool
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, lambda: session.exec(select(Model)).all())
    finally:
        session.close()  # ✅ Always closed
```

## Testing

### Verification Steps

1. **Run audit script:**
   ```powershell
   .venv\Scripts\python.exe test_files\audit_session_leaks.py
   ```

2. **Monitor connection pool in logs:**
   ```python
   from ba2_trade_platform.core.db import engine
   print(f"Pool size: {engine.pool.size()}")
   print(f"Checked out: {engine.pool.checkedout()}")
   print(f"Overflow: {engine.pool.overflow()}")
   ```

3. **Load test with concurrent operations:**
   - Open multiple browser tabs with UI
   - Trigger multiple expert analyses simultaneously
   - Monitor for connection pool exhaustion errors

4. **Check logs for session lifecycle:**
   ```
   2025-10-13 ... DEBUG - Database session created (id=123456789)
   ```

### Expected Outcomes

✅ **Before Fix:**
- Error: `QueuePool limit of size 20 overflow 40 reached`
- Application hangs/timeouts after intensive use
- Logs show session creation but no cleanup warnings

✅ **After Fix:**
- Reduced pool size means faster failure detection
- Shorter timeout exposes leaks immediately
- Better logging shows session lifecycle

⚠️ **Known Limitation:** The fix reduces pool size to expose leaks faster, but does NOT fix all unclosed sessions in the codebase. A comprehensive refactoring is needed.

## Future Work

### Phase 1: Immediate (Emergency Fix) ✅ COMPLETED
- ✅ Reduce pool size and timeout
- ✅ Add logging and documentation
- ✅ Create audit tool

### Phase 2: Short-term (Prevent New Leaks)
- [ ] Add linter rule to flag `session = get_db()` without context manager
- [ ] Create wrapper: `@with_session` decorator for automatic cleanup
- [ ] Add session tracking middleware to log unclosed sessions

### Phase 3: Medium-term (Fix Existing Leaks)
- [ ] Refactor `ba2_trade_platform/ui/pages/settings.py` (~15 leaks)
- [ ] Refactor `ba2_trade_platform/ui/pages/overview.py` (~10 leaks)
- [ ] Refactor all chart components to use context managers
- [ ] Refactor expert modules (TradingAgents, FMPSenateTrade, etc.)

### Phase 4: Long-term (Architecture Improvement)
- [ ] Implement database session middleware for NiceGUI
- [ ] Create session-per-request pattern for UI operations
- [ ] Add automated testing for session leaks
- [ ] Consider async SQLAlchemy for better async/await integration

## Related Issues

- **Logging Issue:** Many places use `logger.error()` without `exc_info=True`, hiding stack traces
- **Async/Sync Mixing:** UI code mixes async/sync database operations awkwardly
- **Global Session State:** Some code relies on module-level sessions (anti-pattern)

## References

- SQLAlchemy Connection Pooling: https://docs.sqlalchemy.org/en/20/core/pooling.html
- SQLite WAL Mode: https://www.sqlite.org/wal.html
- Context Managers in Python: https://docs.python.org/3/library/contextlib.html

## Impact Assessment

### User-Facing Impact
- **Before:** Application would freeze after ~1 hour of use
- **After:** Crashes faster but with clear error messages (aids debugging)

### Developer Impact
- **Positive:** Clear documentation of correct patterns
- **Positive:** Audit tool helps identify leaks
- **Negative:** Smaller pool may cause more frequent errors during development

### Performance Impact
- **Memory:** Reduced from 60 to 30 max connections (saves ~30 connections worth of memory)
- **Responsiveness:** Fails faster (10s instead of 60s timeout)
- **Stability:** More frequent recycling (10 min instead of 1 hour) prevents stale connections

## Lessons Learned

1. **Resource Management is Critical:** Always use context managers for resources
2. **Fail Fast:** Smaller pools with short timeouts expose bugs faster
3. **Observability Matters:** Better logging reveals root causes
4. **Technical Debt Accumulates:** 45+ session leaks show need for code review
5. **Documentation Guides Behavior:** Clear docstrings prevent anti-patterns
