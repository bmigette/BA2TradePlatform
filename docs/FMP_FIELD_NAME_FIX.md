# FMP API Field Name Fix - representative → firstName + lastName

## Issue
The FMP stable API endpoints return `firstName` and `lastName` as separate fields instead of a combined `representative` field.

## Changes Made

### 1. Fixed `_calculate_recommendation()` Method
**File:** `ba2_trade_platform/modules/experts/FMPSenateTrade.py`
**Line:** ~513-517

**Before:**
```python
for trade in filtered_trades:
    trader_name = trade.get('representative', 'Unknown')  # ❌ Field doesn't exist
    transaction_type = trade.get('type', '').lower()
```

**After:**
```python
for trade in filtered_trades:
    # Build trader name from firstName and lastName
    first_name = trade.get('firstName', '')
    last_name = trade.get('lastName', '')
    trader_name = f"{first_name} {last_name}".strip() or 'Unknown'
    
    transaction_type = trade.get('type', '').lower()
```

### 2. Fixed `_filter_trades()` Date Field Names
**File:** `ba2_trade_platform/modules/experts/FMPSenateTrade.py`
**Line:** ~312-320

**Before:**
```python
# FMP API uses 'dateRecieved' for disclosure date and 'transactionDate' for execution date
disclose_date_str = trade.get('dateRecieved', '')  # ❌ Wrong field name
exec_date_str = trade.get('transactionDate', '')

if not disclose_date_str or not exec_date_str:
    logger.debug(f"Trade missing dates, skipping: {trade.get('representative', 'Unknown')}")  # ❌ Field doesn't exist
    continue
```

**After:**
```python
# FMP API uses 'disclosureDate' for disclosure date and 'transactionDate' for execution date
disclose_date_str = trade.get('disclosureDate', '')  # ✅ Correct field name
exec_date_str = trade.get('transactionDate', '')

if not disclose_date_str or not exec_date_str:
    # Build trader name for logging
    trader_name = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip() or 'Unknown'
    logger.debug(f"Trade missing dates, skipping: {trader_name}")
    continue
```

### 3. Fixed `_calculate_recommendation()` Trade Info Fallback
**File:** `ba2_trade_platform/modules/experts/FMPSenateTrade.py`
**Line:** ~539

**Before:**
```python
'disclose_date': trade.get('disclose_date', trade.get('dateRecieved', 'N/A')),  # ❌ Wrong field name
```

**After:**
```python
'disclose_date': trade.get('disclose_date', trade.get('disclosureDate', 'N/A')),  # ✅ Correct field name
```

## API Response Structure

The FMP stable API returns data in this format:

```json
{
  "symbol": "AAPL",
  "disclosureDate": "2025-10-05",
  "transactionDate": "2025-09-18",
  "firstName": "Shelley",
  "lastName": "Moore Capito",
  "office": "Shelley Moore Capito",
  "district": "WV",
  "owner": "Spouse",
  "assetDescription": "Apple Inc",
  "assetType": "Stock",
  "type": "Sale",
  "amount": "$15,001 - $50,000",
  "capitalGainsOver200USD": "False",
  "comment": "",
  "link": "https://efdsearch.senate.gov/search/view/ptr/..."
}
```

**Important Field Mappings:**
- No `representative` field - must construct from `firstName` + `lastName`
- Disclosure date is `disclosureDate` (not `dateRecieved`)
- Transaction/execution date is `transactionDate`

## Benefits

✅ **Correct Field Mapping:** Now properly reads trader names from API response
✅ **Graceful Handling:** Falls back to 'Unknown' if names missing
✅ **Consistent Logging:** Debug messages show correct trader names
✅ **No Data Loss:** Properly combines first and last names with space

## Testing

Test with any symbol that has government trades:
```python
expert.run_analysis("AAPL", market_analysis)
```

Expected logs should now show:
```
Found X senate trades by Nancy Pelosi
Found Y house trades by James Smith
```

Instead of:
```
Found 0 senate trades by Unknown
```

## Related Changes

This fix complements the earlier API endpoint updates:
- Senate trades: `/stable/senate-trades`
- House trades: `/stable/house-trades`
- Senate by name: `/stable/senate-trades-by-name`
- House by name: `/stable/house-trades-by-name`

## Files Modified

- `ba2_trade_platform/modules/experts/FMPSenateTrade.py` (2 locations)

## No Breaking Changes

- Existing functionality preserved
- Only changes internal field mapping
- Output format unchanged
