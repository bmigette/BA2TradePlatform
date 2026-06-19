# OHLCV Date Range + Per-Provider Cache + News Batch Fetch Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-provider OHLCV cache paths, date-range selection with extend-only semantics, and a new News Batch Fetch tool that fetches/enriches/analyzes news for multiple symbols over a date range.

**Architecture:**
- OHLCV cache files move from `datasets/cache/{SYM}_{interval}.csv` to `datasets/cache/{provider}/{SYM}_{interval}.csv`. A new `_get_cache_file()` helper on the base class centralises the path. The handler switches from `get_ohlcv_data(force_refresh=True)` to a new `extend_ohlcv_cache()` method that detects coverage gaps and only fetches/merges missing date ranges.
- The News Batch Fetch tab reuses the existing `SentimentService.fetch_news_for_ticker()` pipeline (which already handles Wayback Machine for articles > 1 yr old via `_try_wayback_machine`) and the `NewsCacheService` DB, adding a task-queue handler and a matching frontend component.

**Tech Stack:** Python / FastAPI (backend), React / TypeScript / Tailwind (frontend), pandas (OHLCV merge), SQLAlchemy (news cache), trafilatura + waybackpy (content enrichment).

---

## Task 1: Per-Provider Cache Path Helper in base.py

**Files:**
- Modify: `backend/dataproviders/base.py`
- Test: `backend/tests/test_ohlcv_cache.py`

### Step 1: Write failing test

```python
# In backend/tests/test_ohlcv_cache.py – add to class TestHandleOHLCVCacheFetch

def test_cache_file_is_per_provider(self, mock_task_queue, mock_provider):
    """Cache file path must include provider name as subdirectory."""
    # mock_provider.get_provider_name() returns 'yfinance' by default in fixture
    from dataproviders.base import MarketDataProviderInterface
    # A concrete stub is needed:
    class _Stub(MarketDataProviderInterface):
        def _get_ohlcv_data_impl(self, *a, **kw): return pd.DataFrame()
        def get_provider_name(self): return "testprov"
        def get_supported_features(self): return []
        def validate_config(self): return True

    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        s = _Stub()
        s.cache_folder = pathlib.Path(tmp)
        p = s._get_cache_file("AAPL", "1h")
        assert p == pathlib.Path(tmp) / "testprov" / "AAPL_1h.csv"
```

### Step 2: Run test to verify it fails

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py::TestHandleOHLCVCacheFetch::test_cache_file_is_per_provider -v
```
Expected: `AttributeError: '_Stub' object has no attribute '_get_cache_file'`

### Step 3: Add `_get_cache_file` to base.py

In `backend/dataproviders/base.py`, add this method to `MarketDataProviderInterface` (after `__init__`, before `_get_ohlcv_data_impl`):

```python
def _get_cache_file(self, symbol: str, interval: str) -> Path:
    """Return per-provider cache file path, creating the directory if needed."""
    provider_dir = self.cache_folder / self.get_provider_name()
    provider_dir.mkdir(parents=True, exist_ok=True)
    return provider_dir / f"{symbol}_{interval}.csv"
```

Then update **every** occurrence of the old cache file pattern in `get_ohlcv_data`:

Old (line ~132):
```python
cache_file = self.cache_folder / f"{symbol}_{interval}.csv"
```
Replace **both** occurrences (in the read path and in the write path) with:
```python
cache_file = self._get_cache_file(symbol, interval)
```

There are exactly two occurrences:
- Line ~132 inside `if use_cache and not force_refresh:` block
- Line ~170 inside `if (use_cache or force_refresh) and not df.empty:` block

### Step 4: Run test

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py::TestHandleOHLCVCacheFetch::test_cache_file_is_per_provider -v
```
Expected: PASS

### Step 5: Commit

```bash
git add backend/dataproviders/base.py backend/tests/test_ohlcv_cache.py
git commit -m "feat: per-provider OHLCV cache subdirectory via _get_cache_file"
```

---

## Task 2: Extend-Only Cache Method on base.py

**Files:**
- Modify: `backend/dataproviders/base.py`
- Test: `backend/tests/test_ohlcv_cache.py`

The handler will call `provider.extend_ohlcv_cache(symbol, start_date, end_date, interval)` instead of `get_ohlcv_data`. This method:
- Returns immediately if cache already covers the full requested range.
- Fetches only the uncovered head/tail and merges, deduplicates, sorts, then saves.

### Step 1: Write failing test

```python
# Add to backend/tests/test_ohlcv_cache.py

import pandas as pd
import tempfile, pathlib
from datetime import datetime
from unittest.mock import MagicMock, patch

class TestExtendOHLCVCache:
    """Tests for extend_ohlcv_cache extend-only semantics."""

    def _make_df(self, start: str, end: str) -> pd.DataFrame:
        dates = pd.date_range(start, end, freq='D')
        return pd.DataFrame({'Date': dates, 'Open': 1.0, 'High': 2.0,
                              'Low': 0.5, 'Close': 1.5, 'Volume': 100.0})

    def _make_provider(self, tmp_dir: str):
        from dataproviders.base import MarketDataProviderInterface
        class _Stub(MarketDataProviderInterface):
            def _get_ohlcv_data_impl(self, symbol, start, end, interval):
                dates = pd.date_range(start, end, freq='D')
                return pd.DataFrame({'Date': dates, 'Open': 1.0, 'High': 2.0,
                                     'Low': 0.5, 'Close': 1.5, 'Volume': 100.0})
            def get_provider_name(self): return "stub"
            def get_supported_features(self): return []
            def validate_config(self): return True
        p = _Stub()
        p.cache_folder = pathlib.Path(tmp_dir)
        return p

    def test_no_fetch_when_range_covered(self):
        """If cache already covers the range, _get_ohlcv_data_impl must not be called."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp)
            # Pre-populate cache covering 2024-01-01 to 2024-12-31
            cache_file = prov._get_cache_file("AAPL", "1d")
            self._make_df("2024-01-01", "2024-12-31").to_csv(cache_file, index=False)

            from unittest.mock import patch
            with patch.object(prov, '_get_ohlcv_data_impl', wraps=prov._get_ohlcv_data_impl) as mock_impl:
                prov.extend_ohlcv_cache("AAPL", datetime(2024, 3, 1), datetime(2024, 6, 1), "1d")
                mock_impl.assert_not_called()

    def test_full_fetch_when_no_cache(self):
        """If no cache file exists, fetches the full requested range."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp)
            prov.extend_ohlcv_cache("AAPL", datetime(2024, 1, 1), datetime(2024, 3, 31), "1d")
            cache_file = prov._get_cache_file("AAPL", "1d")
            assert cache_file.exists()
            df = pd.read_csv(cache_file)
            assert len(df) > 0

    def test_extends_right_only(self):
        """Appends newer data without re-fetching already-cached dates."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp)
            cache_file = prov._get_cache_file("AAPL", "1d")
            self._make_df("2024-01-01", "2024-06-30").to_csv(cache_file, index=False)

            with patch.object(prov, '_get_ohlcv_data_impl', wraps=prov._get_ohlcv_data_impl) as mock_impl:
                prov.extend_ohlcv_cache("AAPL", datetime(2024, 1, 1), datetime(2024, 12, 31), "1d")
                # Should only fetch the right gap (2024-07-01 onwards)
                assert mock_impl.call_count == 1
                call_start = mock_impl.call_args[0][1]
                assert call_start >= datetime(2024, 6, 28)  # Allow a few days buffer

    def test_no_duplicate_rows_after_extend(self):
        """Merged cache must not have duplicate Date rows."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = self._make_provider(tmp)
            cache_file = prov._get_cache_file("AAPL", "1d")
            self._make_df("2024-01-01", "2024-06-30").to_csv(cache_file, index=False)
            prov.extend_ohlcv_cache("AAPL", datetime(2024, 1, 1), datetime(2024, 09, 30), "1d")
            df = pd.read_csv(cache_file, parse_dates=['Date'])
            assert df['Date'].duplicated().sum() == 0
```

### Step 2: Run tests to verify they fail

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py::TestExtendOHLCVCache -v
```
Expected: `AttributeError: ... has no attribute 'extend_ohlcv_cache'`

### Step 3: Implement `extend_ohlcv_cache` in base.py

Add after `get_ohlcv_data` in `MarketDataProviderInterface`:

```python
def extend_ohlcv_cache(
    self,
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    interval: str = '1d'
) -> pd.DataFrame:
    """
    Fetch and cache OHLCV data for the requested range using extend-only semantics.

    - If the cache already covers [start_date, end_date] entirely, returns
      cached data without any API call.
    - Otherwise fetches only the uncovered head/tail portions, merges with
      existing data, deduplicates on Date, and overwrites the cache file.

    Args:
        symbol: Ticker symbol
        start_date: Desired range start (inclusive)
        end_date: Desired range end (inclusive)
        interval: Data interval ('1d', '1h', etc.)

    Returns:
        Full merged DataFrame (existing + newly fetched)
    """
    cache_file = self._get_cache_file(symbol, interval)

    def _to_ts(dt: datetime) -> pd.Timestamp:
        ts = pd.Timestamp(dt)
        return ts.tz_localize(None) if ts.tzinfo is None else ts.tz_localize(None)

    start_ts = _to_ts(start_date)
    end_ts = _to_ts(end_date)

    existing = pd.DataFrame()
    if cache_file.exists():
        try:
            existing = pd.read_csv(cache_file)
            existing['Date'] = pd.to_datetime(existing['Date']).dt.tz_localize(None)
        except Exception as e:
            logger.warning(f"Could not read existing cache {cache_file}: {e}")
            existing = pd.DataFrame()

    if not existing.empty:
        cache_min = existing['Date'].min()
        cache_max = existing['Date'].max()

        # Range fully covered — no fetch needed
        if cache_min <= start_ts and cache_max >= end_ts:
            logger.debug(f"Cache for {symbol}/{interval} already covers "
                         f"{start_date.date()} to {end_date.date()}, skipping fetch")
            return existing[(existing['Date'] >= start_ts) & (existing['Date'] <= end_ts)]

        # Collect gap pieces
        pieces = [existing]

        if start_ts < cache_min:
            logger.info(f"Extending {symbol}/{interval} left: "
                        f"{start_date.date()} to {cache_min.date()}")
            left = self._get_ohlcv_data_impl(symbol, start_date,
                                              cache_min.to_pydatetime(), interval)
            if not left.empty:
                left['Date'] = pd.to_datetime(left['Date']).dt.tz_localize(None)
                pieces.append(left)

        if end_ts > cache_max:
            logger.info(f"Extending {symbol}/{interval} right: "
                        f"{cache_max.date()} to {end_date.date()}")
            right = self._get_ohlcv_data_impl(symbol, cache_max.to_pydatetime(),
                                              end_date, interval)
            if not right.empty:
                right['Date'] = pd.to_datetime(right['Date']).dt.tz_localize(None)
                pieces.append(right)

        merged = (pd.concat(pieces, ignore_index=True)
                    .drop_duplicates(subset=['Date'])
                    .sort_values('Date')
                    .reset_index(drop=True))
    else:
        # No cache — fetch full range
        logger.info(f"No cache for {symbol}/{interval}, fetching "
                    f"{start_date.date()} to {end_date.date()}")
        merged = self._get_ohlcv_data_impl(symbol, start_date, end_date, interval)
        if not merged.empty:
            merged['Date'] = pd.to_datetime(merged['Date']).dt.tz_localize(None)

    if not merged.empty:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(cache_file, index=False)
        logger.info(f"Saved {len(merged)} rows to {cache_file}")

    return merged
```

### Step 4: Run tests

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py::TestExtendOHLCVCache -v
```
Expected: All 4 tests PASS

### Step 5: Commit

```bash
git add backend/dataproviders/base.py backend/tests/test_ohlcv_cache.py
git commit -m "feat: extend_ohlcv_cache with gap-only fetch and extend-only semantics"
```

---

## Task 3: Handler — Date Range + Switch to extend_ohlcv_cache

**Files:**
- Modify: `backend/app/services/ohlcv_cache_handler.py`
- Test: `backend/tests/test_ohlcv_cache.py`

### Step 1: Write failing test

```python
# Add to TestHandleOHLCVCacheFetch

def test_handler_passes_date_range(self, mock_task_queue, mock_provider):
    """Handler must pass start_date/end_date from payload to extend_ohlcv_cache."""
    mock_provider.extend_ohlcv_cache = MagicMock(return_value=pd.DataFrame({'Date': [], 'Close': []}))
    with patch('app.services.ohlcv_cache_handler.get_task_queue', return_value=mock_task_queue), \
         patch('app.api.datasets.get_ohlcv_provider', return_value=mock_provider):
        from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch

        handle_ohlcv_cache_fetch('task-1', {
            'provider': 'yfinance',
            'symbol': 'AAPL',
            'timeframes': ['1d'],
            'start_date': '2023-01-01',
            'end_date': '2024-12-31',
        })
        call_kwargs = mock_provider.extend_ohlcv_cache.call_args
        assert call_kwargs is not None
        # start_date and end_date must be datetime objects
        from datetime import datetime
        assert isinstance(call_kwargs[1].get('start_date') or call_kwargs[0][1], datetime)

def test_handler_uses_default_15yr_range_when_no_dates(self, mock_task_queue, mock_provider):
    """Handler defaults to 15-year range when start/end dates not in payload."""
    mock_provider.extend_ohlcv_cache = MagicMock(return_value=pd.DataFrame())
    with patch('app.services.ohlcv_cache_handler.get_task_queue', return_value=mock_task_queue), \
         patch('app.api.datasets.get_ohlcv_provider', return_value=mock_provider):
        from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch

        handle_ohlcv_cache_fetch('task-1', {
            'provider': 'yfinance', 'symbol': 'AAPL', 'timeframes': ['1d']
        })
        assert mock_provider.extend_ohlcv_cache.called
```

### Step 2: Run tests to verify failure

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py::TestHandleOHLCVCacheFetch::test_handler_passes_date_range -v
```

### Step 3: Update ohlcv_cache_handler.py

Replace the entire file content:

```python
"""
OHLCV Cache Fetch Handler

Background task handler for prefetching and caching OHLCV data
for multiple symbols and timeframes.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from app.services.task_queue import get_task_queue

logger = logging.getLogger(__name__)


def handle_ohlcv_cache_fetch(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Background task handler for OHLCV cache fetching.

    Fetches OHLCV data for a single symbol across multiple timeframes using
    extend-only semantics: already-cached date ranges are not re-fetched.

    Args:
        task_id: Task ID for progress tracking
        payload: Dict with keys:
            - provider: str (e.g., 'yfinance', 'fmp')
            - symbol: str (e.g., 'AAPL')
            - timeframes: list[str] (e.g., ['1d', '1h', '4h'])
            - start_date: str ISO date 'YYYY-MM-DD' (optional, default 15yr ago)
            - end_date: str ISO date 'YYYY-MM-DD' (optional, default today)

    Returns:
        Summary dict with status and results per timeframe
    """
    from app.api.datasets import get_ohlcv_provider

    task_queue = get_task_queue()
    provider_name = payload.get('provider', 'yfinance')
    symbol = payload.get('symbol', '')
    timeframes = payload.get('timeframes', ['1d'])

    if not symbol:
        return {'status': 'failed', 'error': 'symbol is required'}

    # Parse date range from payload or fall back to 15-year default
    end_date = datetime.now()
    start_date = end_date - timedelta(days=15 * 365)

    raw_start = payload.get('start_date')
    raw_end = payload.get('end_date')
    if raw_start:
        start_date = datetime.strptime(raw_start, '%Y-%m-%d')
    if raw_end:
        end_date = datetime.strptime(raw_end, '%Y-%m-%d')

    provider = get_ohlcv_provider(provider_name)
    results = {}
    total = len(timeframes)

    for i, tf in enumerate(timeframes):
        progress = (i / total) * 100
        task_queue.update_progress(task_id, progress, f"Fetching {symbol} {tf}...")

        try:
            df = provider.extend_ohlcv_cache(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                interval=tf
            )
            rows = len(df) if df is not None else 0
            results[tf] = {'status': 'success', 'rows': rows}
            logger.info(f"Cached {symbol} {tf}: {rows} rows")
        except Exception as e:
            results[tf] = {'status': 'error', 'error': str(e)}
            logger.error(f"Error caching {symbol} {tf}: {e}")

    task_queue.update_progress(task_id, 100, f"Completed {symbol}")

    return {
        'status': 'completed',
        'symbol': symbol,
        'provider': provider_name,
        'results': results
    }
```

### Step 4: Run tests

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py -v
```
Expected: All existing + new tests PASS

### Step 5: Commit

```bash
git add backend/app/services/ohlcv_cache_handler.py backend/tests/test_ohlcv_cache.py
git commit -m "feat: ohlcv handler accepts date range and delegates to extend_ohlcv_cache"
```

---

## Task 4: Backend API — Date Range + Updated Cache Status

**Files:**
- Modify: `backend/app/api/tools.py` (two endpoints)

No new tests needed here (these are thin API wrappers). Manual verification below.

### Step 1: Update `/ohlcv/fetch-cache` to forward date range

In `tools.py`, find `async def fetch_ohlcv_cache` (~line 1031). Update the payload construction block:

Old:
```python
payload={
    'provider': provider,
    'symbol': symbol,
    'timeframes': timeframes
},
```

New:
```python
payload={
    'provider': provider,
    'symbol': symbol,
    'timeframes': timeframes,
    'start_date': request.get('start_date'),   # None = use handler default
    'end_date': request.get('end_date'),
},
```

### Step 2: Update `/ohlcv/cache-status` to scan provider subdirectories

Find `async def get_ohlcv_cache_status` (~line 1094). Replace the scanning logic:

Old:
```python
if cache_dir.exists():
    for filepath in cache_dir.glob("*.csv"):
        ...
        name_parts = filepath.stem.rsplit('_', 1)
        if len(name_parts) == 2:
            symbol, interval = name_parts
        ...
        entries.append({
            "symbol": symbol,
            "interval": interval,
            ...
        })
```

New:
```python
if cache_dir.exists():
    # Scan both legacy flat files and new per-provider subdirectories
    csv_files = list(cache_dir.glob("*.csv"))          # legacy flat
    csv_files += list(cache_dir.glob("*/*.csv"))        # per-provider
    for filepath in csv_files:
        try:
            # Determine provider: parent is cache_dir (legacy) or a provider subfolder
            if filepath.parent == cache_dir:
                provider_name = "unknown"
            else:
                provider_name = filepath.parent.name

            name_parts = filepath.stem.rsplit('_', 1)
            if len(name_parts) == 2:
                symbol, interval = name_parts
            else:
                symbol = filepath.stem
                interval = "unknown"

            stat = filepath.stat()
            file_size = stat.st_size
            rows = 0
            try:
                with open(filepath, 'r') as f:
                    rows = sum(1 for _ in f) - 1
            except Exception:
                pass

            entries.append({
                "provider": provider_name,
                "symbol": symbol,
                "interval": interval,
                "file_size": file_size,
                "file_size_mb": round(file_size / (1024 * 1024), 2),
                "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "rows": max(0, rows),
                "filename": filepath.name
            })
        except Exception as e:
            logger.warning(f"Error reading cache file {filepath}: {e}")

# Sort by provider, symbol, interval
entries.sort(key=lambda x: (x['provider'], x['symbol'], x['interval']))
```

### Step 3: Verify manually

Start the backend and hit:
```bash
curl -s http://localhost:8000/api/tools/ohlcv/cache-status | python -m json.tool
```
Should show `"provider": "yfinance"` (or whichever) for existing cache entries.

### Step 4: Commit

```bash
git add backend/app/api/tools.py
git commit -m "feat: ohlcv cache API forwards date range and scans per-provider subdirectories"
```

---

## Task 5: Frontend OHLCVCacheTool — Date Range + Provider Column

**Files:**
- Modify: `frontend/src/pages/Tools.tsx` (OHLCVCacheTool component only)

### Step 1: Add state and date defaults

In `const OHLCVCacheTool: React.FC = () => {` block, add after the existing `useState` calls:

```typescript
// Default: today and 15 years ago
const defaultEnd = new Date().toISOString().split('T')[0];
const defaultStart = new Date(Date.now() - 15 * 365 * 24 * 60 * 60 * 1000)
  .toISOString().split('T')[0];
const [startDate, setStartDate] = useState(defaultStart);
const [endDate, setEndDate] = useState(defaultEnd);
```

### Step 2: Include dates in `handleFetchCache`

Find the `body: JSON.stringify({ provider, symbols, timeframes })` line inside `handleFetchCache`. Update to:
```typescript
body: JSON.stringify({ provider, symbols, timeframes, start_date: startDate, end_date: endDate })
```

### Step 3: Add date range inputs to the form

After the existing timeframes checkboxes section (before the Fetch Button), add:

```tsx
{/* Date Range */}
<div className="grid grid-cols-2 gap-4">
  <div>
    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
      Start Date
    </label>
    <input
      type="date"
      value={startDate}
      onChange={e => setStartDate(e.target.value)}
      className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
    />
  </div>
  <div>
    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
      End Date
    </label>
    <input
      type="date"
      value={endDate}
      onChange={e => setEndDate(e.target.value)}
      className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
    />
  </div>
</div>
```

### Step 4: Add Provider column to the cached data table

In the cached data table header, add `<th>Provider</th>` before `<th>Symbol</th>`.

In the table row, add `<td>{file.provider || '–'}</td>` before `<td>{file.symbol}</td>`.

Also update the `CacheFile` interface near the top of `OHLCVCacheTool` (if it exists as a local type) to include `provider: string`.

### Step 5: Update the Tools tab type

Find:
```typescript
const [activeTab, setActiveTab] = useState<'news' | 'fundamentals' | 'macro' | 'maintenance' | 'ohlcv'>('news');
```
This will also need `'newsbatch'` added in Task 9 — leave it for now.

### Step 6: Verify visually

Run `npm run dev` in the frontend, open the OHLCV tab, confirm date pickers appear and the cache table shows a Provider column.

### Step 7: Commit

```bash
git add frontend/src/pages/Tools.tsx
git commit -m "feat: OHLCV cache tool — date range inputs and provider column in table"
```

---

## Task 6: News Batch Fetch Handler

**Files:**
- Create: `backend/app/services/news_batch_handler.py`
- Test: `backend/tests/test_news_batch.py`

The handler uses `SentimentService.fetch_news_for_ticker()` which already calls:
- `enrich_articles_with_content()` (trafilatura for recent, Wayback Machine for articles > 1 yr old via `_try_wayback_machine` in `dataproviders/news/base.py`)
- `NewsCacheService.cache_articles_batch()` (persists to DB)

After fetching, the handler also runs sentiment analysis and persists it back via `NewsCacheService.update_sentiment_batch()`.

### Step 1: Write failing tests

Create `backend/tests/test_news_batch.py`:

```python
"""Tests for news batch fetch handler."""
import pytest
from unittest.mock import MagicMock, patch


class TestNewsBatchHandler:

    def test_missing_symbol_returns_failed(self):
        from app.services.news_batch_handler import handle_news_batch_fetch
        with patch('app.services.news_batch_handler.get_task_queue', return_value=MagicMock()):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': [],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })
        assert result['status'] == 'failed'

    def test_missing_dates_returns_failed(self):
        from app.services.news_batch_handler import handle_news_batch_fetch
        with patch('app.services.news_batch_handler.get_task_queue', return_value=MagicMock()):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
            })
        assert result['status'] == 'failed'

    def test_successful_fetch_calls_sentiment_service(self):
        mock_tq = MagicMock()
        mock_articles = [
            {'title': 'Test', 'url': 'http://x.com/1', 'date': '2024-01-15',
             'content': 'positive earnings', 'sentiment': None}
        ]
        mock_sentiment_svc = MagicMock()
        mock_sentiment_svc.fetch_news_for_ticker.return_value = mock_articles
        mock_sentiment_svc.analyze_news_articles.return_value = mock_articles

        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_tq), \
             patch('app.services.news_batch_handler.SentimentService',
                   return_value=mock_sentiment_svc):
            from app.services.news_batch_handler import handle_news_batch_fetch
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })

        assert result['status'] == 'completed'
        mock_sentiment_svc.fetch_news_for_ticker.assert_called_once()
        # Sentiment analysis should have been run
        mock_sentiment_svc.analyze_news_articles.assert_called_once()
```

### Step 2: Run tests to verify they fail

```bash
cd backend && ./venv/bin/python -m pytest tests/test_news_batch.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.news_batch_handler'`

### Step 3: Create `news_batch_handler.py`

Create `backend/app/services/news_batch_handler.py`:

```python
"""
News Batch Fetch Handler

Background task handler for bulk-fetching, enriching, and caching news articles
for multiple symbols over a date range.

Each article's webpage is fetched (or retrieved from Wayback Machine for articles
older than 1 year) and sentiment is analyzed via FinBERT before caching.
"""

import logging
from datetime import datetime
from typing import Dict, Any

from app.services.task_queue import get_task_queue
from app.services.sentiment import SentimentService

logger = logging.getLogger(__name__)


def handle_news_batch_fetch(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Background task handler for batch news fetching with content enrichment
    and sentiment analysis.

    For each symbol:
      1. Fetches articles from the provider (monthly chunks, deduped against cache).
      2. Enriches articles with full webpage content via trafilatura.
         Articles older than 1 year are tried via Wayback Machine first.
      3. Runs FinBERT sentiment analysis.
      4. Persists articles + sentiment to the news DB cache.

    Args:
        task_id: Task ID for progress tracking
        payload: Dict with keys:
            - provider: str (e.g., 'fmp', 'alphavantage', 'finnhub', 'alpaca')
            - symbols: list[str]
            - start_date: str 'YYYY-MM-DD' (required)
            - end_date: str 'YYYY-MM-DD' (required)

    Returns:
        Summary dict with status and per-symbol article counts.
    """
    task_queue = get_task_queue()

    provider = payload.get('provider', 'fmp')
    symbols = payload.get('symbols', [])
    raw_start = payload.get('start_date')
    raw_end = payload.get('end_date')

    if not symbols:
        return {'status': 'failed', 'error': 'symbols list is required'}
    if not raw_start or not raw_end:
        return {'status': 'failed', 'error': 'start_date and end_date are required'}

    start_date = datetime.strptime(raw_start, '%Y-%m-%d')
    end_date = datetime.strptime(raw_end, '%Y-%m-%d')

    sentiment_service = SentimentService()
    results = {}
    total = len(symbols)

    for i, symbol in enumerate(symbols):
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        base_progress = (i / total) * 100
        task_queue.update_progress(
            task_id, base_progress,
            f"[{i+1}/{total}] Fetching news for {symbol}..."
        )

        try:
            # fetch_news_for_ticker:
            #  - checks DB cache for already-known URLs (dedupe)
            #  - fetches new articles from provider in monthly chunks
            #  - enriches with trafilatura (Wayback Machine for old articles)
            #  - writes new articles to DB cache
            articles = sentiment_service.fetch_news_for_ticker(
                ticker=symbol,
                start_date=start_date,
                end_date=end_date,
                provider=provider,
                enrich_content=True,
                use_cache=True
            )

            new_articles = [a for a in articles if not a.get('sentiment')]

            task_queue.update_progress(
                task_id, base_progress + (0.8 / total) * 100,
                f"[{i+1}/{total}] Analyzing sentiment for {symbol} "
                f"({len(new_articles)} articles)..."
            )

            # Run FinBERT sentiment on articles that don't have it yet
            if new_articles:
                analyzed = sentiment_service.analyze_news_articles(new_articles)

                # Persist sentiment back to DB cache
                if sentiment_service._cache_service and analyzed:
                    updates = [
                        (a['url'], {
                            'label': a.get('sentiment'),
                            'score': a.get('sentiment_score'),
                            'positive_prob': a.get('positive_prob'),
                            'neutral_prob': a.get('neutral_prob'),
                            'negative_prob': a.get('negative_prob'),
                        })
                        for a in analyzed if a.get('url') and a.get('sentiment')
                    ]
                    if updates:
                        sentiment_service._cache_service.update_sentiment_batch(updates)

            results[symbol] = {
                'status': 'success',
                'total_articles': len(articles),
                'new_articles': len(new_articles),
            }
            logger.info(f"News batch {symbol}: {len(articles)} total, "
                        f"{len(new_articles)} new analyzed")

        except Exception as e:
            results[symbol] = {'status': 'error', 'error': str(e)}
            logger.error(f"Error in news batch fetch for {symbol}: {e}", exc_info=True)

    task_queue.update_progress(task_id, 100, "Completed")

    return {
        'status': 'completed',
        'provider': provider,
        'start_date': raw_start,
        'end_date': raw_end,
        'results': results,
    }
```

### Step 4: Run tests

```bash
cd backend && ./venv/bin/python -m pytest tests/test_news_batch.py -v
```
Expected: All 3 tests PASS

### Step 5: Commit

```bash
git add backend/app/services/news_batch_handler.py backend/tests/test_news_batch.py
git commit -m "feat: news batch fetch handler with content enrichment and sentiment caching"
```

---

## Task 7: Register Handler + Add API Endpoints

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/tools.py`

### Step 1: Register the handler in main.py

Find the handler registration block (around line 254):
```python
from app.services.ohlcv_cache_handler import handle_ohlcv_cache_fetch
task_queue.register_handler('ohlcv_cache_fetch', handle_ohlcv_cache_fetch)
```

Add after:
```python
from app.services.news_batch_handler import handle_news_batch_fetch
task_queue.register_handler('news_batch_fetch', handle_news_batch_fetch)
```

Update the log line to include the new handler:
```python
logger.info("Registered task handlers: dataset_regeneration, training_job, backtest, ohlcv_cache_fetch, news_batch_fetch")
```

### Step 2: Add `/news/batch-fetch` POST endpoint in tools.py

Add this endpoint after the existing `/ohlcv/cache-status` block (around line 1148):

```python
@router.post("/news/batch-fetch")
async def batch_fetch_news(request: Dict[str, Any]):
    """
    Queue news batch fetch jobs for multiple symbols.

    Each symbol gets its own background task that fetches articles,
    enriches with webpage content, analyzes sentiment, and caches results.

    Args:
        request: Dict with provider, symbols, start_date, end_date

    Returns:
        List of queued task IDs
    """
    from app.services.task_queue import get_task_queue

    provider = request.get('provider', 'fmp')
    symbols = request.get('symbols', [])
    start_date = request.get('start_date')
    end_date = request.get('end_date')

    if not symbols:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="symbols list is required and cannot be empty"
        )
    if not start_date or not end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date and end_date are required (YYYY-MM-DD)"
        )

    task_queue = get_task_queue()
    task_ids = []

    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        task_id = task_queue.queue_task(
            task_type='news_batch_fetch',
            name=f'News Batch: {symbol}',
            payload={
                'provider': provider,
                'symbols': [symbol],
                'start_date': start_date,
                'end_date': end_date,
            },
            description=f'Fetch and cache news for {symbol} ({start_date} to {end_date})',
            max_retries=1,
            timeout_seconds=3600  # News fetching can take a long time
        )
        task_ids.append({'symbol': symbol, 'task_id': task_id})

    logger.info(f"Queued {len(task_ids)} news batch fetch tasks")

    return {
        "task_ids": task_ids,
        "count": len(task_ids),
        "provider": provider,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/news/batch-status")
async def get_news_batch_status():
    """
    Get news cache statistics from the database.

    Returns:
        Article counts by provider and ticker
    """
    from app.services.news_cache import NewsCacheService
    try:
        cache = NewsCacheService()
        stats = cache.get_cache_stats()
        return stats
    except Exception as e:
        logger.error(f"Error getting news cache status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get news cache status: {str(e)}"
        )
```

### Step 3: Verify backend starts cleanly

```bash
cd backend && ./venv/bin/python -m uvicorn app.main:app --reload
```
Expected: No import errors, all handlers registered.

### Step 4: Commit

```bash
git add backend/app/main.py backend/app/api/tools.py
git commit -m "feat: register news_batch_fetch handler and add batch-fetch/batch-status API endpoints"
```

---

## Task 8: Frontend — News Batch Fetch Tab

**Files:**
- Modify: `frontend/src/pages/Tools.tsx`

### Step 1: Add tab type and tab button

In `Tools.tsx`, update the `activeTab` type:
```typescript
const [activeTab, setActiveTab] = useState<'news' | 'fundamentals' | 'macro' | 'maintenance' | 'ohlcv' | 'newsbatch'>('news');
```

Add a new tab button after the OHLCV tab button:
```tsx
<button
  onClick={() => setActiveTab('newsbatch')}
  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
    activeTab === 'newsbatch'
      ? 'border-blue-500 text-blue-600 dark:text-blue-400'
      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
  }`}
>
  News Batch Fetch
</button>
```

Add the content line:
```tsx
{activeTab === 'newsbatch' && <NewsBatchFetchTool />}
```

### Step 2: Add the `NewsBatchFetchTool` component

Add this component after `OHLCVCacheTool`. It mirrors the OHLCV tool structure but for news. Add appropriate interfaces before the component:

```typescript
interface NewsBatchTask {
  symbol: string;
  task_id: string;
  status?: string;
  progress?: number;
  progress_message?: string;
}

interface NewsCacheStats {
  total_articles: number;
  with_sentiment: number;
  with_content: number;
  by_provider: Record<string, number>;
}

const NewsBatchFetchTool: React.FC = () => {
  const [provider, setProvider] = useState('fmp');
  const [providers, setProviders] = useState<NewsProvider[]>([]);
  const [symbolInput, setSymbolInput] = useState('');
  const [symbols, setSymbols] = useState<string[]>([]);

  const defaultEnd = new Date().toISOString().split('T')[0];
  const defaultStart = new Date(Date.now() - 365 * 24 * 60 * 60 * 1000)
    .toISOString().split('T')[0];  // Default 1 year back for news
  const [startDate, setStartDate] = useState(defaultStart);
  const [endDate, setEndDate] = useState(defaultEnd);

  const [fetching, setFetching] = useState(false);
  const [activeTasks, setActiveTasks] = useState<NewsBatchTask[]>([]);
  const [cacheStats, setCacheStats] = useState<NewsCacheStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    fetchProviders();
    fetchCacheStats();
  }, []);

  // Poll active tasks
  useEffect(() => {
    if (activeTasks.length === 0) return;
    const interval = setInterval(async () => {
      const updatedTasks = await Promise.all(
        activeTasks.map(async t => {
          try {
            const r = await fetch(`http://localhost:8000/api/jobs/${t.task_id}`);
            if (r.ok) {
              const d = await r.json();
              return { ...t, status: d.status, progress: d.progress, progress_message: d.progress_message };
            }
          } catch { /* ignore */ }
          return t;
        })
      );
      setActiveTasks(updatedTasks);
      const stillActive = updatedTasks.filter(t => t.status !== 'completed' && t.status !== 'failed');
      if (stillActive.length === 0) {
        setFetching(false);
        fetchCacheStats();
        setMessage('All tasks completed!');
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [activeTasks]);

  const fetchProviders = async () => {
    try {
      const r = await fetch('http://localhost:8000/api/tools/news/providers');
      if (r.ok) {
        const d = await r.json();
        // Filter providers that support company news (exclude localfiles)
        setProviders((d.providers || []).filter((p: NewsProvider) => p.id !== 'localfiles'));
      }
    } catch { /* ignore */ }
  };

  const fetchCacheStats = async () => {
    try {
      const r = await fetch('http://localhost:8000/api/tools/news/batch-status');
      if (r.ok) {
        const d = await r.json();
        setCacheStats(d);
      }
    } catch { /* ignore */ }
  };

  const addSymbol = () => {
    const parts = symbolInput.split(/[\s,;]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
    setSymbols(prev => [...new Set([...prev, ...parts])]);
    setSymbolInput('');
  };

  const removeSymbol = (s: string) => setSymbols(prev => prev.filter(x => x !== s));

  const handleFetchBatch = async () => {
    if (symbols.length === 0) { setError('Please enter at least one symbol'); return; }
    if (!startDate || !endDate) { setError('Please select a date range'); return; }

    setError(null);
    setMessage(null);
    setFetching(true);
    setActiveTasks([]);

    try {
      const resp = await fetch('http://localhost:8000/api/tools/news/batch-fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, symbols, start_date: startDate, end_date: endDate })
      });
      if (!resp.ok) {
        const e = await resp.json();
        throw new Error(e.detail || 'Failed to queue tasks');
      }
      const data = await resp.json();
      setActiveTasks(data.task_ids.map((t: any) => ({ symbol: t.symbol, task_id: t.task_id, status: 'pending' })));
      setMessage(`Queued ${data.count} task(s). Fetching news for: ${symbols.join(', ')}`);
    } catch (e: any) {
      setError(e.message);
      setFetching(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Fetch Form */}
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-semibold mb-2 text-gray-900 dark:text-gray-100">
          News Batch Fetch
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Bulk-fetch news articles for one or more symbols over a date range.
          Article webpages are fetched (Wayback Machine for articles older than 1 year)
          and FinBERT sentiment is analyzed. All results are persisted to the news cache.
        </p>

        <div className="space-y-4">
          {/* Provider */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              News Provider
            </label>
            <select
              value={provider}
              onChange={e => setProvider(e.target.value)}
              className="border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            >
              {providers.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          {/* Symbols */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Symbols
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={symbolInput}
                onChange={e => setSymbolInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && addSymbol()}
                placeholder="AAPL, MSFT, TSLA"
                className="flex-1 border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <button
                onClick={addSymbol}
                className="px-3 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md text-sm hover:bg-gray-200 dark:hover:bg-gray-600"
              >
                Add
              </button>
            </div>
            {symbols.length > 0 && (
              <div className="flex flex-wrap gap-2 mt-2">
                {symbols.map(s => (
                  <span key={s} className="inline-flex items-center gap-1 px-2 py-1 bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 rounded text-sm">
                    {s}
                    <button onClick={() => removeSymbol(s)} className="hover:text-red-500">×</button>
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Date Range */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Start Date
              </label>
              <input
                type="date"
                value={startDate}
                onChange={e => setStartDate(e.target.value)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                End Date
              </label>
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
          </div>

          {error && <p className="text-red-500 text-sm">{error}</p>}
          {message && <p className="text-green-600 dark:text-green-400 text-sm">{message}</p>}

          <button
            onClick={handleFetchBatch}
            disabled={fetching || symbols.length === 0}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
          >
            {fetching ? 'Fetching...' : 'Fetch & Analyze'}
          </button>
        </div>
      </div>

      {/* Active Tasks */}
      {activeTasks.length > 0 && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <h3 className="text-lg font-semibold mb-3 text-gray-900 dark:text-gray-100">Active Tasks</h3>
          <div className="space-y-2">
            {activeTasks.map(t => (
              <div key={t.task_id} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded">
                <span className="font-medium text-sm text-gray-900 dark:text-gray-100">{t.symbol}</span>
                <span className="text-xs text-gray-500 dark:text-gray-400">{t.progress_message || t.status || 'pending'}</span>
                <span className={`text-xs px-2 py-1 rounded ${
                  t.status === 'completed' ? 'bg-green-100 text-green-800' :
                  t.status === 'failed' ? 'bg-red-100 text-red-800' :
                  'bg-blue-100 text-blue-800'
                }`}>{t.status || 'pending'}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Cache Stats */}
      {cacheStats && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              News Cache Stats
            </h3>
            <button
              onClick={fetchCacheStats}
              className="px-3 py-1 text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
            >
              Refresh
            </button>
          </div>
          <div className="grid grid-cols-3 gap-4 mb-4">
            <div className="text-center p-3 bg-gray-50 dark:bg-gray-700 rounded">
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{cacheStats.total_articles.toLocaleString()}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Total Articles</div>
            </div>
            <div className="text-center p-3 bg-gray-50 dark:bg-gray-700 rounded">
              <div className="text-2xl font-bold text-purple-600">{cacheStats.with_sentiment.toLocaleString()}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">With Sentiment</div>
            </div>
            <div className="text-center p-3 bg-gray-50 dark:bg-gray-700 rounded">
              <div className="text-2xl font-bold text-green-600">{cacheStats.with_content.toLocaleString()}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">With Content</div>
            </div>
          </div>
          {Object.keys(cacheStats.by_provider).length > 0 && (
            <div>
              <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">By Provider</h4>
              <div className="space-y-1">
                {Object.entries(cacheStats.by_provider).map(([prov, count]) => (
                  <div key={prov} className="flex justify-between text-sm text-gray-600 dark:text-gray-400">
                    <span>{prov}</span>
                    <span className="font-medium">{(count as number).toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
```

### Step 3: Verify visually

Run `npm run dev`, navigate to Tools → News Batch Fetch. Confirm:
- Provider dropdown populated
- Symbol add/remove works
- Date pickers render
- "Fetch & Analyze" button queues tasks
- Tasks show status polling
- Cache stats table shows totals

### Step 4: Commit

```bash
git add frontend/src/pages/Tools.tsx
git commit -m "feat: News Batch Fetch tab — bulk fetch, enrich, analyze and cache news"
```

---

## Task 9: Final Test Run

```bash
cd backend && ./venv/bin/python -m pytest tests/test_ohlcv_cache.py tests/test_news_batch.py -v
```

All tests must pass. Then:

```bash
git add .
git commit -m "chore: final test run — all OHLCV and news batch tests passing"
```

---

---

## Task 10: FMP Intraday Pagination (Chunked Requests)

**Files:**
- Modify: `backend/dataproviders/ohlcv/FMPOHLCVProvider.py`
- Test: No unit test needed (requires live API key) — verified by checking row counts.

**Problem:** `_fetch_intraday_data` sends one request for the entire date range. FMP silently truncates responses for fine-grained intervals over long ranges (e.g., 1m bars for > 1 month). The resulting cache files end up with far fewer rows than expected.

**Fix:** Chunk the date range inside `_fetch_intraday_data` with interval-appropriate chunk sizes, then concatenate.

### Step 1: Define chunk sizes and update `_fetch_intraday_data`

Replace the entire `_fetch_intraday_data` method body with a chunked implementation:

```python
# Chunk sizes for each interval (days per API request)
INTRADAY_CHUNK_DAYS = {
    "1min":  30,    # ~9 000 bars/month for 1m — FMP truncates beyond ~5 000
    "5min":  90,
    "15min": 180,
    "30min": 365,
    "1hour": 730,
    "4hour": 1825,  # 5 years
}
```

Add this as a class attribute on `FMPOHLCVProvider`.

Then rewrite `_fetch_intraday_data`:

```python
def _fetch_intraday_data(
    self,
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    fmp_interval: str
) -> pd.DataFrame:
    """
    Fetch intraday OHLCV data from FMP API, chunking long date ranges to avoid
    silent API truncation.
    """
    chunk_days = self.INTRADAY_CHUNK_DAYS.get(fmp_interval, 90)
    chunks = []
    chunk_start = start_date

    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end_date)

        url = f"{self.BASE_URL}/historical-chart/{fmp_interval}/{symbol}"
        params = {
            "apikey": self.api_key,
            "from": chunk_start.strftime("%Y-%m-%d"),
            "to": chunk_end.strftime("%Y-%m-%d"),
        }

        logger.debug(f"FMP intraday chunk: {chunk_start.date()} to {chunk_end.date()}")

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data and isinstance(data, list):
                chunk_df = pd.DataFrame(data)
                chunk_df = chunk_df.rename(columns={
                    "date": "Date", "open": "Open", "high": "High",
                    "low": "Low", "close": "Close", "volume": "Volume"
                })
                chunk_df = chunk_df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]
                chunk_df['Date'] = pd.to_datetime(chunk_df['Date'], utc=True)
                chunks.append(chunk_df)
                logger.debug(f"  Got {len(chunk_df)} bars for chunk")
        except Exception as e:
            logger.warning(f"FMP intraday chunk {chunk_start.date()}-{chunk_end.date()} failed: {e}")

        chunk_start = chunk_end + timedelta(days=1)

    if not chunks:
        return pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])

    df = pd.concat(chunks, ignore_index=True)
    df = df.drop_duplicates(subset=['Date'])

    # Filter to exact requested range
    start_ts = pd.Timestamp(start_date).tz_localize('UTC') if pd.Timestamp(start_date).tz is None else pd.Timestamp(start_date).tz_convert('UTC')
    end_ts = pd.Timestamp(end_date).tz_localize('UTC') if pd.Timestamp(end_date).tz is None else pd.Timestamp(end_date).tz_convert('UTC')
    df = df[(df['Date'] >= start_ts) & (df['Date'] <= end_ts)]

    df = df.sort_values('Date').reset_index(drop=True)
    logger.info(f"FMP intraday {symbol}/{fmp_interval}: {len(df)} total bars "
                f"({len(chunks)} chunk requests)")
    return df
```

Note: `timedelta` is already imported at the top of the file.

### Step 2: Verify manually (if FMP API key is configured)

Run a quick check in the Python REPL:
```python
from dataproviders.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
from datetime import datetime
p = FMPOHLCVProvider()
df = p._get_ohlcv_data_impl('AAPL', datetime(2024, 1, 1), datetime(2024, 3, 31), '1m')
print(len(df), df['Date'].min(), df['Date'].max())
# Expect ~23 000+ rows for 1m over 3 months
```

### Step 3: Commit

```bash
git add backend/dataproviders/ohlcv/FMPOHLCVProvider.py
git commit -m "fix: FMP intraday chunked requests to prevent silent API truncation"
```

---

## Wayback Machine — Confirmed Already Implemented

`_try_wayback_machine` in `backend/dataproviders/news/base.py` is already fully implemented and called from `fetch_url_content` when `published_at` is older than 1 year. No additional work needed.

---

## Summary of Changes

| File | Change |
|------|--------|
| `backend/dataproviders/base.py` | `_get_cache_file()` helper; `extend_ohlcv_cache()` method |
| `backend/app/services/ohlcv_cache_handler.py` | Accept date range payload; call `extend_ohlcv_cache` |
| `backend/app/api/tools.py` | Forward dates in fetch-cache; scan provider subdirs in cache-status; add batch-fetch + batch-status endpoints |
| `backend/app/main.py` | Register `news_batch_fetch` handler |
| `backend/app/services/news_batch_handler.py` | **New file** |
| `backend/tests/test_ohlcv_cache.py` | Per-provider path test; extend-only tests; date range tests |
| `backend/tests/test_news_batch.py` | **New file** |
| `frontend/src/pages/Tools.tsx` | Date inputs in OHLCV tool; Provider column; `NewsBatchFetchTool` component + tab |
