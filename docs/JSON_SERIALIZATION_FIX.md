# JSON Serialization Fix - Pandas Timestamp Issue

## Issue

**Error:**
```
TypeError: Type is not JSON serializable: Timestamp
```

**Stack Trace:** Error occurred in NiceGUI's JSON serialization when rendering Plotly charts in the InstrumentGraph component.

**Root Cause:** Pandas `Timestamp` objects in `DatetimeIndex` were being passed directly to Plotly chart configuration, but NiceGUI's JSON serializer (`orjson`) cannot serialize pandas Timestamp objects.

---

## Problem Details

### Where It Occurred

**File:** `ba2_trade_platform/ui/components/InstrumentGraph.py`

**Context:** When rendering price and indicator charts in the Data Visualization tab of TradingAgents analysis results.

### Why It Failed

When building Plotly chart configurations, the code passed pandas `DatetimeIndex` objects directly as x-axis data:

```python
# ❌ BEFORE - This causes JSON serialization error
fig.add_trace(
    go.Candlestick(
        x=self.price_data.index,  # DatetimeIndex with Timestamp objects
        open=self.price_data['Open'],
        ...
    )
)
```

**The Problem:**
1. `self.price_data.index` is a pandas `DatetimeIndex`
2. `DatetimeIndex` contains pandas `Timestamp` objects
3. NiceGUI converts the Plotly figure to JSON for rendering
4. `orjson` (NiceGUI's JSON library) cannot serialize `Timestamp` objects
5. Raises `TypeError: Type is not JSON serializable: Timestamp`

---

## Solution

### Fix Applied

Convert all pandas `DatetimeIndex` objects to lists of strings **before** passing to Plotly:

```python
# ✅ AFTER - Convert to string list first
x_data = self.price_data.index.strftime('%Y-%m-%d').tolist() if isinstance(self.price_data.index, pd.DatetimeIndex) else self.price_data.get('Date', range(len(self.price_data)))

fig.add_trace(
    go.Candlestick(
        x=x_data,  # List of date strings, not Timestamp objects
        open=self.price_data['Open'],
        ...
    )
)
```

### Changes Made

**File:** `ba2_trade_platform/ui/components/InstrumentGraph.py`

**Modified Sections:**

1. **Candlestick Chart (lines ~138-154)**
   - Convert price data index to string list
   - Format: `'%Y-%m-%d'` (e.g., "2025-10-02")

2. **Line Chart for Close Price (lines ~156-169)**
   - Convert price data index to string list
   - Used when OHLC data not available

3. **Technical Indicators (lines ~171-193)**
   - Convert indicator DataFrame index to string list
   - Applied to all visible indicators

4. **Volume Chart (lines ~195-211)**
   - Convert price data index to string list
   - Volume bars use same dates as price chart

**Pattern Used:**
```python
x_data = df.index.strftime('%Y-%m-%d').tolist() if isinstance(df.index, pd.DatetimeIndex) else fallback
```

**Why This Works:**
- `DatetimeIndex.strftime()` converts Timestamps to strings
- `.tolist()` converts pandas array to Python list
- Strings are JSON-serializable
- Plotly handles date strings correctly for x-axis

---

## Testing

### Verify Fix

1. **Navigate to TradingAgents Analysis:**
   - Open any completed TradingAgents market analysis
   - Click "Data Visualization" tab

2. **Expected Behavior:**
   - ✅ Price chart renders without errors
   - ✅ Technical indicators display correctly
   - ✅ Volume chart shows if data available
   - ✅ Date axis shows properly formatted dates
   - ✅ No JSON serialization errors in console

3. **Test Scenarios:**
   - Analysis with OHLC data (candlestick chart)
   - Analysis with only Close data (line chart)
   - Multiple technical indicators enabled
   - Toggling indicators on/off

### Verification Script

```python
# Test DatetimeIndex conversion
import pandas as pd

# Create sample DatetimeIndex
dates = pd.date_range('2025-01-01', periods=10, freq='D')
df = pd.DataFrame({'Close': range(10)}, index=dates)

# BEFORE (causes error when JSON-serialized)
print(type(df.index))  # <class 'pandas.core.indexes.datetimes.DatetimeIndex'>
print(type(df.index[0]))  # <class 'pandas._libs.tslibs.timestamps.Timestamp'>

# AFTER (JSON-serializable)
x_data = df.index.strftime('%Y-%m-%d').tolist()
print(type(x_data))  # <class 'list'>
print(type(x_data[0]))  # <class 'str'>
print(x_data[0])  # '2025-01-01'

import json
json.dumps(x_data)  # ✅ Works!
```

---

## Related Code

### Other Places Using DatetimeIndex

**Files to check for similar issues:**

1. ✅ `InstrumentGraph.py` - **FIXED**
   - All chart x-axis data converted to strings

2. ⚠️ `TradingAgentsUI.py` - **CHECK IF NEEDED**
   - Uses price_data but passes to InstrumentGraph
   - Fix in InstrumentGraph handles it

3. ⚠️ `overview.py` - **CHECK IF NEEDED**
   - Uses datetime objects but converts to timestamps
   - May need review if using pandas DatetimeIndex

### Prevention Pattern

**When working with pandas DataFrames in NiceGUI:**

```python
# ✅ DO THIS - Convert before passing to UI components
df_for_ui = df.copy()
if isinstance(df_for_ui.index, pd.DatetimeIndex):
    df_for_ui.index = df_for_ui.index.strftime('%Y-%m-%d')

# ✅ DO THIS - Convert to dict with string dates
data_dict = {
    'dates': df.index.strftime('%Y-%m-%d').tolist(),
    'values': df['column'].tolist()
}

# ❌ DON'T DO THIS - Pass DatetimeIndex directly
ui.plotly(fig_with_datetimeindex)  # Will fail!
```

---

## Key Learnings

### JSON Serialization in NiceGUI

1. **NiceGUI uses `orjson`** for JSON serialization
2. **Pandas types are not serializable** by default
3. **Convert before passing to UI** components
4. **Strings are safe** - use `.strftime().tolist()`

### Pandas DatetimeIndex Handling

1. **Check type first:** `isinstance(df.index, pd.DatetimeIndex)`
2. **Convert to strings:** `df.index.strftime('%Y-%m-%d').tolist()`
3. **Alternative:** Convert to Python datetime: `df.index.to_pydatetime().tolist()`
4. **For timestamps:** `df.index.to_series().astype(int) // 10**9` (Unix epoch)

### Plotly Integration

1. **Plotly accepts strings** for date axes
2. **Auto-formats** date strings correctly
3. **Timezone handling:** Use ISO format if needed: `'%Y-%m-%dT%H:%M:%S'`
4. **Performance:** String conversion has minimal overhead

---

## Impact

### Before Fix
- ❌ Data Visualization tab crashed with TypeError
- ❌ Unable to view price charts
- ❌ Unable to view technical indicators
- ❌ Poor user experience

### After Fix
- ✅ Charts render correctly
- ✅ All data displays properly
- ✅ Indicators toggle smoothly
- ✅ No serialization errors
- ✅ Professional visualization experience

---

## Date

October 2, 2025
