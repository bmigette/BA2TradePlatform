# FMP API Endpoint Fix

## Issue
FMP Senate/House trading endpoints were returning 400 Bad Request errors during manual analysis execution.

## Root Cause
The code was using incorrect endpoint URLs when fetching all trades (without a symbol filter):
- **Wrong**: `/stable/senate-trades` (returns 400 when no symbol provided)
- **Wrong**: `/stable/house-trades` (returns 400 when no symbol provided)

## Solution
Updated both `_fetch_senate_trades()` and `_fetch_house_trades()` methods in `FMPSenateTraderCopy.py` to use the correct endpoints based on whether a symbol is provided:

### Symbol-Specific Trades
When fetching trades for a specific symbol, use the original endpoints:
- Senate: `/stable/senate-trades?symbol=AAPL`
- House: `/stable/house-trades?symbol=AAPL`

### All Trades (Latest Disclosures)
When fetching all trades (no symbol), use the latest disclosure endpoints with pagination:
- Senate: `/stable/senate-latest?page=0&limit=100`
- House: `/stable/house-latest?page=0&limit=100`

## Code Changes

### File: `ba2_trade_platform/modules/experts/FMPSenateTraderCopy.py`

#### `_fetch_senate_trades()` - Lines 113-159
```python
# Before: Always used /stable/senate-trades
url = f"https://financialmodelingprep.com/stable/senate-trades"

# After: Use different endpoints based on symbol parameter
if symbol:
    # Use symbol-specific endpoint
    url = f"https://financialmodelingprep.com/stable/senate-trades"
    params = {
        "apikey": self._api_key,
        "symbol": symbol.upper()
    }
else:
    # Use latest disclosures endpoint with pagination
    url = f"https://financialmodelingprep.com/stable/senate-latest"
    params = {
        "apikey": self._api_key,
        "page": 0,
        "limit": 100  # Maximum allowed per request
    }
```

#### `_fetch_house_trades()` - Lines 161-207
```python
# Before: Always used /stable/house-trades
url = f"https://financialmodelingprep.com/stable/house-trades"

# After: Use different endpoints based on symbol parameter
if symbol:
    # Use symbol-specific endpoint
    url = f"https://financialmodelingprep.com/stable/house-trades"
    params = {
        "apikey": self._api_key,
        "symbol": symbol.upper()
    }
else:
    # Use latest disclosures endpoint with pagination
    url = f"https://financialmodelingprep.com/stable/house-latest"
    params = {
        "apikey": self._api_key,
        "page": 0,
        "limit": 100  # Maximum allowed per request
    }
```

## API Documentation Reference
From FMP API docs (https://site.financialmodelingprep.com/developer/docs#senate-latest):

- **Latest Senate Financial Disclosures**: `/stable/senate-latest?page=0&limit=100`
  - Returns latest financial disclosures from U.S. Senate members
  - Supports pagination with `page` and `limit` parameters
  - Maximum 250 records per request, page maxed at 100

- **Latest House Financial Disclosures**: `/stable/house-latest?page=0&limit=100`
  - Returns latest financial disclosures from U.S. House members
  - Supports pagination with `page` and `limit` parameters
  - Maximum 250 records per request, page maxed at 100

- **Senate Trading Activity (by symbol)**: `/stable/senate-trades?symbol=AAPL`
  - Monitor trading activity of US Senators for specific symbol

- **U.S. House Trades (by symbol)**: `/stable/house-trades?symbol=AAPL`
  - Track financial trades by U.S. House members for specific symbol

## Pagination Support
The latest disclosure endpoints support pagination:
- **page**: Page number (0-based)
- **limit**: Records per page (max 100)
- **Maximum**: 250 records per request

### Current Implementation
- Uses `page=0` and `limit=100` to fetch first 100 records
- Does NOT currently implement multi-page fetching

### Future Enhancement (Optional)
If more than 100 records are needed, implement pagination loop:
```python
all_trades = []
page = 0
while True:
    params = {
        "apikey": self._api_key,
        "page": page,
        "limit": 100
    }
    response = requests.get(url, params=params, timeout=30)
    data = response.json()
    if not data or len(data) == 0:
        break
    all_trades.extend(data)
    if len(data) < 100:  # Last page
        break
    page += 1
return all_trades
```

## Testing
To verify the fix works:
1. Run manual analysis for Expert 8 (FMPSenateTraderCopy)
2. Check logs for successful API calls:
   - `Fetching all FMP senate trades (latest disclosures)`
   - `Fetching all FMP house trades (latest disclosures)`
   - `Received N senate trade records (all)`
   - `Received N house trade records (all)`
3. Verify no 400 Bad Request errors appear

## Impact
- **Fixed**: Manual analysis now works for Expert 8
- **Fixed**: Scheduled jobs will now successfully fetch senate/house trades
- **Improved**: Code now uses correct FMP API endpoints
- **Note**: Currently fetches only first 100 records per chamber (can be extended with pagination if needed)
