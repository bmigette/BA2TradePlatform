# Session 14 Handoff - Technical Indicators Feature (In Progress)

**Date:** 2026-01-24
**Session Duration:** ~3 hours
**Tests Passing:** 33/206 (16.0%) - unchanged from Session 13
**Status:** Feature 128 NOT completed, technical blocker encountered

## Attempted Work

### Goal
Implement Feature 128: Dataset preview overlays technical indicators on chart
- Add SMA, EMA, RSI, MACD indicators to the candlestick chart
- Create toggle controls for showing/hiding indicators
- Display indicators in separate panels (MACD) or overlaid on price chart (SMA/EMA)

### What Was Attempted

1. **Backend Modifications** (reverted):
   - Added TechnicalIndicators import to datasets.py preview endpoint
   - Attempted to calculate SMA_20, EMA_20, RSI_14, MACD in preview endpoint
   - Tried multiple approaches to handle NaN/Inf values in JSON serialization
   - All attempts resulted in "Out of range float values are not JSON compliant" error

2. **Debugging Steps Taken**:
   - Verified indicators module works correctly (test_macd_direct.py - PASS)
   - Verified MACD returns correct keys: 'macd', 'signal', 'histogram' (not 'MACD')
   - Tested indicators on actual dataset (no Inf values, expected NaN values)
   - Attempted fixes:
     * Replace Inf with NaN
     * Manual NaN to None conversion
     * pandas.to_json() with date_format='iso'
     * Response() object with manual JSON string
     * String concatenation to avoid re-encoding
   - Root cause: FastAPI JSON encoder rejects float NaN even after pandas conversion

3. **Server Management Issues**:
   - Multiple uvicorn processes running on port 8002
   - Auto-reload not working reliably
   - Started secondary server on port 8003 for testing
   - Cache clearing didn't resolve stale code issues

### Technical Blocker

**Problem:** FastAPI's JSON encoder (starlette/Python's json module) does not accept NaN values even when pandas.to_json() correctly converts them to null in the JSON string. When json.loads() parses the string back to Python dicts, NaN values are restored, causing encoding to fail.

**Error:** `ValueError: Out of range float values are not JSON compliant`

**Why This Happens:**
1. Pandas DataFrames with indicators have NaN values (expected for early rows)
2. pandas.to_json() converts NaN →  null (correct)
3. json.loads() converts null → NaN (Python float)
4. FastAPI tries to encode response → fails on NaN values

### Verification Tests Run

✅ Dashboard loads correctly
✅ Datasets list displays
✅ Dataset details page shows candlestick chart
✅ Candlestick chart with zoom/pan works (Session 13 work intact)
✅ Indicators module functions correctly in isolation
✅ Original OHLC data has no Inf/NaN values

### Files Changed (Reverted)

- backend/app/api/datasets.py - reverted to working state
- Created multiple test files (deleted)

##Solutions for Next Session

### Option 1: Custom JSON Encoder (Recommended)
Create a custom FastAPI JSONResponse class that uses orjson or a custom encoder:

```python
from fastapi.responses import ORJSONResponse
import orjson

# In main.py, set default response class
app = FastAPI(default_response_class=ORJSONResponse)

# orjson handles NaN gracefully
```

OR use a custom encoder:

```python
import json
import numpy as np

class NaNEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, float) and np.isnan(obj):
            return None
        return super().default(obj)

# Use in datasets.py
return Response(
    content=json.dumps(data, cls=NaNEncoder),
    media_type="application/json"
)
```

### Option 2: Separate Indicators Endpoint
Create `/api/datasets/{id}/indicators` endpoint that returns indicators separately:

```python
@router.get("/{dataset_id}/indicators")
async def get_dataset_indicators(...):
    # Calculate indicators
    # Return with careful NaN handling
    return indicators_data
```

Frontend fetches both preview and indicators, merges client-side.

### Option 3: Frontend Calculation
Calculate indicators in the frontend using a JavaScript library like technicalindicators or ta-lib-js. This avoids backend JSON serialization issues entirely.

### Option 4: Pre-calculate During Dataset Creation
When dataset is created (POST /api/datasets), calculate indicators and save to separate columns in the CSV. Preview endpoint just reads and returns existing data (no NaN issues if saved as strings or handled during creation).

## Recommended Next Steps

1. **Clean Server State:**
   ```bash
   # Kill all uvicorn processes
   taskkill /F /IM python.exe /FI "WINDOWTITLE eq*uvicorn*"

   # Clear Python cache
   find backend -name "*.pyc" -delete
   find backend -name "__pycache__" -type d -exec rm -rf {} +

   # Start fresh
   ./init.sh
   ```

2. **Implement Option 1 (Custom JSON Encoder):**
   - Install orjson: `pip install orjson`
   - Update main.py to use ORJSONResponse as default
   - Retry adding indicators to preview endpoint
   - Test thoroughly

3. **Alternative: Implement Option 4 (Pre-calculate):**
   - Safer approach, indicators calculated once during dataset creation
   - No JSON encoding issues in preview
   - Better performance (no recalculation on each preview)

## Current State

- **Backend:** Port 8002 (multiple processes), Port 8003 (test server)
- **Frontend:** Port 5173, pointing to port 8002
- **Database:** Contains 1 AAPL dataset (251 rows)
- **Code:** All changes reverted, back to Session 13 state
- **Tests:** 33/206 passing (no regression)

## Key Learnings

1. FastAPI/Starlette's JSON encoder is strict about NaN/Inf values
2. pandas.to_json() creates valid JSON but parsing it back reintroduces NaN
3. Need custom JSON encoding strategy for DataFrames with indicators
4. Multiple server processes can cause confusing debugging (check with netstat)
5. orjson or custom encoders are necessary for scientific/ML data in FastAPI

## Files to Review

- `backend/app/indicators.py` - Technical indicators module (working correctly)
- `backend/app/api/datasets.py` - Preview endpoint (reverted to working state)
- Session 13 work (candlestick chart) - still functional

## Next Agent Should

1. Choose one of the 4 solution options above
2. Implement the chosen solution
3. Test thoroughly with browser automation
4. Mark Feature 128 as passing if successful
5. Continue to Feature 129 (sentiment markers) or other UI features

**Total Time Spent This Session:** ~3 hours on debugging JSON encoding issue
**Blocker Severity:** Medium - workarounds available, feature not critical for MVP
**Recommendation:** Implement Option 1 (ORJSONResponse) or Option 4 (pre-calculate)
