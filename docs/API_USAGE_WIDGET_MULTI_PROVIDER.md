# API Usage Widget Multi-Provider Support

## Overview
Updated the Overview page API Usage widget to support both OpenAI and Naga AI providers, displaying usage statistics for both services in a unified interface.

## Changes Made

### 1. Widget Rename
**File**: `ba2_trade_platform/ui/pages/overview.py`

- Renamed `_render_openai_spending_widget()` → `_render_api_usage_widget()`
- Changed widget title from "💰 OpenAI API Usage" to "💰 API Usage"
- Updated render method to call new function name

### 2. New Naga AI Usage Function
**Function**: `_get_naga_ai_usage_data_async()`

Fetches usage data from Naga AI API endpoints:
- **Balance Endpoint**: `https://api.naga.ac/v1/account/balance`
  - Returns current account balance
  - Response format: `{"balance": "75.0000000000000000"}`
  - Balance is returned as string, converted to float

- **Activity Endpoint**: `https://api.naga.ac/v1/account/activity`
  - Returns usage statistics with structure:
    ```json
    {
      "period_days": 30,
      "total_stats": {
        "total_requests": 0,
        "total_cost": "0E-10",
        "total_input_tokens": 0,
        "total_output_tokens": 0
      },
      "daily_stats": [],
      "top_models": [],
      "api_key_usage": []
    }
    ```
  - Uses `daily_stats` array for time-based calculations
  - Falls back to `total_stats.total_cost` if no daily breakdown

**Key Features**:
- Requires `naga_ai_admin_api_key` from database settings
- Calculates week and month costs from activity data
- Handles string-formatted numbers (e.g., "0E-10" scientific notation)
- Robust error handling for different response formats

### 3. Updated UI Loading Function
**Function**: `_load_api_usage_data()` (formerly `_load_openai_usage_data()`)

**Concurrent Data Fetching**:
```python
openai_data_task = asyncio.create_task(self._get_openai_usage_data_async())
naga_ai_data_task = asyncio.create_task(self._get_naga_ai_usage_data_async())
openai_data, naga_ai_data = await asyncio.gather(openai_data_task, naga_ai_data_task)
```

**Display Structure**:
```
┌─────────────────────────────────┐
│ 💰 API Usage                    │
├─────────────────────────────────┤
│ 🤖 OpenAI                       │
│   Last Week:     $12.34         │
│   Last Month:    $45.67         │
│   Remaining:     $100.00        │
├─────────────────────────────────┤
│ 🌊 Naga AI                      │
│   Last Week:     $0.00          │
│   Last Month:    $0.00          │
│   Balance:       $75.00         │
├─────────────────────────────────┤
│ Last updated: 2025-10-20 18:19  │
└─────────────────────────────────┘
```

**Features**:
- Two separate sections with provider-specific icons
- Smaller font sizes (text-xs) for compact display
- Error handling per provider (one can fail without affecting the other)
- Shows "Balance" for Naga AI instead of "Remaining" (different terminology)

## API Key Requirements

### OpenAI
- **Preferred**: `openai_admin_api_key` (starts with `sk-admin`)
  - Required for accessing usage/billing data
  - Get at: https://platform.openai.com/settings/organization/admin-keys
- **Fallback**: `openai_api_key` (regular key)
  - May lack permissions for billing data
  - Shows helpful error message with link to get admin key

### Naga AI
- **Required**: `naga_ai_admin_api_key`
  - Set in Settings > App Settings
  - Used for both balance and activity endpoints
  - No fallback (regular key not sufficient)

## Error Handling

### Per-Provider Errors
Each provider displays errors independently:

**OpenAI Errors**:
- Invalid admin key format validation
- Permission issues with helpful links
- Rate limiting detection
- Network errors

**Naga AI Errors**:
- Missing API key detection
- Invalid key (401) responses
- Network timeout handling
- Response parsing errors

### Graceful Degradation
- If one provider fails, the other still displays
- Loading state shows for both until data arrives
- Errors shown with ⚠️ icon and clear messages

## Data Type Handling

### String to Float Conversion
Naga AI returns numeric values as strings:

```python
# Balance conversion
balance_str = balance_data.get('balance', '0')
remaining_credit = float(balance_str)  # "75.0000000000000000" → 75.0

# Cost conversion (handles scientific notation)
cost_str = day_stat.get('total_cost', '0')
cost = float(cost_str)  # "0E-10" → 0.0
```

### Date Parsing
Supports multiple date formats:
```python
try:
    day_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
except:
    day_date = datetime.strptime(date_str, '%Y-%m-%d')
```

## Testing

### Test Script
Created: `test_files/test_naga_ai_usage.py`

**Features**:
- Tests balance endpoint
- Tests activity endpoint
- Displays full response structure
- Calculates usage statistics
- Shows detailed error messages

**Run**:
```powershell
.venv\Scripts\python.exe test_files\test_naga_ai_usage.py
```

**Example Output**:
```
============================================================
Testing Naga AI Usage API
============================================================
✅ Found Naga AI Admin API key: ng-X7SvLpX...

------------------------------------------------------------
Testing Balance Endpoint
------------------------------------------------------------
Fetching: https://api.naga.ac/v1/account/balance
Status Code: 200
✅ Success!
Response: {'balance': '75.0000000000000000'}

💰 Current Balance: $75.00

------------------------------------------------------------
Testing Activity Endpoint
------------------------------------------------------------
Fetching: https://api.naga.ac/v1/account/activity
Status Code: 200
✅ Success!

📊 Response Structure:
  activities: 0 items
  daily_stats: 0 items
  total_stats: ['total_requests', 'total_cost', 'total_input_tokens', 'total_output_tokens']

📊 Total Stats:
  total_requests: 0
  total_cost: 0E-10
  total_input_tokens: 0
  total_output_tokens: 0
```

## Usage Statistics Calculation

### Time Ranges
- **Last Week**: Now - 7 days
- **Last Month**: Now - 30 days

### OpenAI
Uses `/v1/organization/costs` endpoint:
- Returns daily cost buckets
- Sums costs for week and month periods
- Fetches credit limits from `/v1/organization/limits`

### Naga AI
Uses `/v1/account/activity` endpoint:
- Processes `daily_stats` array
- Sums `total_cost` per day for time periods
- If no daily stats, uses `total_stats.total_cost` as month total

## Future Enhancements

### Potential Improvements
1. **Cache activity data** to reduce API calls
2. **Add usage charts** showing trends over time
3. **Token usage metrics** for both providers
4. **Cost per model breakdown** from Naga AI top_models
5. **API key usage section** from Naga AI api_key_usage
6. **Alert thresholds** for high spending

### Additional Providers
Easy to extend to more AI providers:
1. Create async fetch function
2. Add to concurrent fetch in `_load_api_usage_data()`
3. Add display section with provider icon and metrics

## Implementation Notes

### Async Pattern
All API calls are async to avoid blocking UI:
```python
async with aiohttp.ClientSession() as session:
    async with session.get(url, headers=headers, timeout=...) as response:
        data = await response.json()
```

### Concurrent Fetching
Both providers fetched simultaneously:
```python
openai_data, naga_ai_data = await asyncio.gather(
    self._get_openai_usage_data_async(),
    self._get_naga_ai_usage_data_async()
)
```
Reduces total loading time from 2 sequential calls to 1 parallel call.

### Client Lifecycle Management
Checks if NiceGUI client still exists before UI updates:
```python
try:
    loading_label.delete()
except RuntimeError:
    # Client has been deleted (user navigated away)
    return
```
Prevents errors when user navigates away during loading.

## Migration Path

### For Existing Users
No action required:
- OpenAI usage continues to work as before
- Naga AI section shows error if admin key not configured
- Widget title changed but functionality preserved

### For New Users
Setup requirements:
1. Add OpenAI admin API key (for billing data)
2. Add Naga AI admin API key (for usage tracking)
3. Both optional - widget shows errors if not configured

## Related Files

**Modified**:
- `ba2_trade_platform/ui/pages/overview.py`
  - `_render_api_usage_widget()` - Main widget render
  - `_load_api_usage_data()` - Async data loading and UI display
  - `_get_openai_usage_data_async()` - Existing OpenAI fetch
  - `_get_naga_ai_usage_data_async()` - New Naga AI fetch

**Created**:
- `test_files/test_naga_ai_usage.py` - Test script for Naga AI API
- `docs/API_USAGE_WIDGET_MULTI_PROVIDER.md` - This documentation

**Database Settings**:
- `openai_admin_api_key` - OpenAI admin key (preferred for usage)
- `openai_api_key` - OpenAI regular key (fallback)
- `naga_ai_admin_api_key` - Naga AI admin key (required)

## Summary

✅ **Completed Tasks**:
1. Renamed widget to "API Usage" (generic)
2. Implemented Naga AI usage data fetching
3. Updated UI to show both providers
4. Tested with real API endpoints
5. Handled data type conversions
6. Added comprehensive error handling

✅ **Benefits**:
- Unified view of all AI API costs
- Concurrent data fetching (faster loading)
- Independent error handling per provider
- Easy to extend to more providers
- Maintains backward compatibility

The implementation is production-ready and fully functional! 🎉
