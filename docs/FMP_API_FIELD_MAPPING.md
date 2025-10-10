# FMP API Field Mapping - Complete Reference

## Overview
Complete field mapping for FMP Senate/House Trading API stable endpoints.

## Actual API Response Structure

Based on real API response from FMP:

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
  "link": "https://efdsearch.senate.gov/search/view/ptr/8bf1954e-5a3d-4258-a57d-cdc73c0ff1f4/"
}
```

## Field Mapping Changes

### Critical Field Name Corrections

| Old Field Name (Incorrect) | Correct Field Name | Usage |
|---------------------------|-------------------|-------|
| `representative` | `firstName` + `lastName` | Trader name (must be constructed) |
| `dateRecieved` | `disclosureDate` | Date trade was disclosed |
| `transactionDate` | `transactionDate` | Date trade was executed (unchanged) |

## Key Fields Used by FMPSenateTrade Expert

### Essential Fields
- **`symbol`** (str): Stock ticker symbol
- **`transactionDate`** (str, YYYY-MM-DD): Trade execution date
- **`disclosureDate`** (str, YYYY-MM-DD): Trade disclosure date
- **`firstName`** (str): Official's first name
- **`lastName`** (str): Official's last name
- **`type`** (str): "Purchase", "Sale", etc.
- **`amount`** (str): Dollar range like "$15,001 - $50,000"

### Additional Context Fields
- **`office`** (str): Full name of official
- **`district`** (str): State/district abbreviation
- **`owner`** (str): "Self", "Spouse", "Dependent Child", etc.
- **`assetDescription`** (str): Company name
- **`assetType`** (str): "Stock", "Bond", etc.
- **`capitalGainsOver200USD`** (str): "True" or "False"
- **`comment`** (str): Additional notes (often empty)
- **`link`** (str): Direct link to official filing

## Code Implementation

### Constructing Trader Name
```python
# Build trader name from firstName and lastName
first_name = trade.get('firstName', '')
last_name = trade.get('lastName', '')
trader_name = f"{first_name} {last_name}".strip() or 'Unknown'
```

### Extracting Dates
```python
# Get disclosure and transaction dates
disclose_date_str = trade.get('disclosureDate', '')
exec_date_str = trade.get('transactionDate', '')

# Parse dates (FMP returns YYYY-MM-DD format)
disclose_date = datetime.strptime(disclose_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
exec_date = datetime.strptime(exec_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
```

### Parsing Amount
```python
# Extract numeric value from amount string
amount_str = trade.get('amount', '0')
# Amount format: "$15,001 - $50,000" or "$1,000,001 +"
# Remove non-numeric characters for rough estimation
amount_str = ''.join(c for c in amount_str if c.isdigit() or c == '.')
amount = float(amount_str) if amount_str else 0
```

## All Changes Applied

### 1. `_filter_trades()` Method
**Line:** ~312-320
- Changed `dateRecieved` → `disclosureDate`
- Updated comment to reflect correct field name
- Fixed trader name construction in logging

### 2. `_calculate_recommendation()` Method
**Line:** ~513-517
- Changed `representative` → `firstName` + `lastName`
- Proper name construction with fallback

**Line:** ~539
- Changed fallback `dateRecieved` → `disclosureDate`

## Testing Checklist

- [ ] Test with AAPL (high government trading activity)
- [ ] Verify trader names appear correctly (not "Unknown")
- [ ] Check disclosure dates are parsed correctly
- [ ] Confirm transaction dates are parsed correctly
- [ ] Validate amount parsing works with different formats
- [ ] Test with both senate and house trades

## API Endpoints Reference

All endpoints use these same field names:

1. **Senate by Symbol:**
   - URL: `https://financialmodelingprep.com/stable/senate-trades?symbol=AAPL`
   
2. **House by Symbol:**
   - URL: `https://financialmodelingprep.com/stable/house-trades?symbol=AAPL`
   
3. **Senate by Name:**
   - URL: `https://financialmodelingprep.com/stable/senate-trades-by-name?name=Pelosi`
   
4. **House by Name:**
   - URL: `https://financialmodelingprep.com/stable/house-trades-by-name?name=Smith`

## Common Pitfalls to Avoid

❌ **DON'T:**
```python
trader = trade.get('representative')  # Field doesn't exist
date = trade.get('dateRecieved')      # Typo/wrong field
```

✅ **DO:**
```python
trader = f"{trade.get('firstName', '')} {trade.get('lastName', '')}".strip()
date = trade.get('disclosureDate')
```

## Additional Notes

### Owner Field Usage
The `owner` field indicates who owns the asset:
- "Self" - Official themselves
- "Spouse" - Official's spouse
- "Dependent Child" - Official's dependent child
- "Joint" - Jointly owned

**Current Implementation:** Not used in confidence calculation, but could be added as a future enhancement (e.g., boost confidence for "Self" trades vs "Spouse" trades).

### Capital Gains Flag
The `capitalGainsOver200USD` field indicates if the trade resulted in capital gains over $200.

**Current Implementation:** Not used, but could be valuable for:
- Filtering out small/insignificant trades
- Understanding which trades were profitable for the official

### Links to Original Filings
Each trade includes a direct link to the official disclosure form.

**Potential Use:** Could be displayed in UI for transparency and verification.

## Version History

- **v1.0** (Initial): Used incorrect field names from v4 API
- **v2.0** (Current): Updated to stable API with correct field names
  - `representative` → `firstName` + `lastName`
  - `dateRecieved` → `disclosureDate`
  - Maintained `transactionDate` (already correct)
