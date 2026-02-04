# UI Performance Optimization Plan

Date: 2026-02-04
Status: In Progress

## Problem

The overview page and market analysis detail page load slowly (multiple seconds). Root causes include:
- Multiple sequential synchronous DB queries blocking the UI thread
- No performance instrumentation to identify bottlenecks
- LazyTable only used on 2 of 13 pages
- Repeated session open/close cycles per page render
- Broker API calls mixed in with page rendering

## Step 1: Config + DB Instrumentation

### Add configurable threshold to config.py
- `DB_PERF_LOG_THRESHOLD_MS = 100` (default, overridable via .env)
- Only log DB operations exceeding this threshold to avoid log spam

### Instrument db.py functions
- `get_instance()` - time the DB query, log if >100ms with model class + ID
- `get_all_instances()` - time the DB query, log if >100ms with model class + count
- `add_instance()` / `update_instance()` - time the full operation including lock wait
- Format: `[DB:query] get_instance(ModelName, 42) - 123.45ms`

### Lock wait measurement
- Measure how long threads wait to acquire `_db_write_lock`
- Log wait time + caller function name when wait exceeds threshold
- Format: `[DB:lock_wait] add_instance(ActivityLog) waited 234.56ms for write lock`

### Extend PerfLogger
- Add `DB = "db"` category
- Add `QUERY = "query"` and `LOCK_WAIT = "lock_wait"` operations

## Step 2: Page PERF Logging

Add `PerfLogger.timer()` to all page render functions that currently lack it:

- `overview.py` - `OverviewTab.render()` + individual widget timers
- `market_analysis_detail.py` - `content()` + sub-timers for each DB fetch
- `marketanalysis.py` - main render
- `performance.py` - render
- `settings.py` - render
- `llm_usage.py` - render
- `marketanalysishistory.py` - render

## Step 3: Overview Page Async Conversion

### Convert all widgets to async with loading spinners
Each widget renders a card shell with a spinner immediately, then loads data asynchronously:
- `_check_and_display_error_orders()` -> async
- `_check_and_display_pending_orders()` -> async
- `_render_analysis_jobs_widget()` -> async
- `_render_order_statistics_widget()` -> async
- `_render_order_recommendations_widget()` -> async

### Consolidate COUNT queries
- Analysis jobs: 4 separate COUNT queries -> 1 grouped query
- Order statistics: 3 COUNT queries per account -> 1 grouped query
- Recommendations: 6 COUNT queries -> 2 grouped queries (week + month)

### Reduce session churn
- Use `with get_db() as session:` pattern within each async widget
- Avoid multiple get_db() + close() cycles per widget

## Step 4: Market Analysis Detail Optimization

### Consolidate DB sessions
- Load analysis + expert in a single session (avoid 3 separate sessions)
- Combine recommendations + orders into a single joined query

### Async content loading
- Render header (title, status, back button) synchronously
- Load heavy content (outputs, recommendations, expert rendering) asynchronously
- Show loading spinner while heavy content loads

## Files to Modify

1. `ba2_trade_platform/config.py` - Add DB_PERF_LOG_THRESHOLD_MS
2. `ba2_trade_platform/core/db.py` - Add timing instrumentation
3. `ba2_trade_platform/ui/utils/perf_logger.py` - Add DB categories
4. `ba2_trade_platform/ui/pages/overview.py` - Async conversion + PERF logging
5. `ba2_trade_platform/ui/pages/market_analysis_detail.py` - Optimization + PERF logging
6. `ba2_trade_platform/ui/pages/marketanalysis.py` - PERF logging
7. `ba2_trade_platform/ui/pages/performance.py` - PERF logging
8. `ba2_trade_platform/ui/pages/settings.py` - PERF logging
9. `ba2_trade_platform/ui/pages/llm_usage.py` - PERF logging
10. `ba2_trade_platform/ui/pages/marketanalysishistory.py` - PERF logging
