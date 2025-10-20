# Bid/Ask Spread Impact on P/L Calculations - Analysis

**Date:** 2025-10-15  
**Test Script:** `test_files/test_price_comparison.py`

## Executive Summary

The analysis confirms that using BID prices for P/L calculations caused a **$1,837.46 discrepancy** compared to Alpaca's position prices. This explains why the widget showed **-$1,689.82** instead of the correct **$147.64**.

## Price Type Comparison

### P/L Results by Price Type

| Price Type | Total P/L | Difference from Broker | Usage |
|-----------|-----------|------------------------|--------|
| **BID** | **-$1,689.82** | **-$1,837.46** ❌ | Old widget (WRONG) |
| **ASK** | $951.77 | +$804.13 ❌ | Would overestimate |
| **MID** | -$369.02 | -$516.66 ⚠️ | Closer but not exact |
| **POSITION** | **$147.64** | **$0.00** ✅ | Alpaca's price (CORRECT) |

**Broker Reported P/L:** $147.64 (matches Position price calculation exactly)

## Spread Analysis

### Largest Spreads (Most Impact)

| Symbol | Bid Price | Ask Price | Position Price | Spread | Spread % | Position vs Bid |
|--------|-----------|-----------|----------------|--------|----------|-----------------|
| **FSK** | $12.83 | $17.30 | $15.14 | $4.47 | **34.8%** | +$2.31 (18.0%) |
| **ET** | $14.00 | $18.64 | $16.53 | $4.64 | **33.1%** | +$2.53 (18.1%) |
| **EPD** | $26.38 | $35.12 | $30.70 | $8.74 | **33.1%** | +$4.32 (16.4%) |
| **GBDC** | $11.99 | $15.79 | $13.99 | $3.80 | **31.7%** | +$2.00 (16.7%) |
| **ADBE** | $335.99 | $440.00 | $337.00 | $104.01 | **31.0%** | +$1.01 (0.3%) |

### Symbols with Significant Position vs Bid Differences

| Symbol | Bid Price | Position Price | Difference | % Difference | Impact on P/L |
|--------|-----------|----------------|------------|--------------|---------------|
| **BABA** | $141.09 | $166.91 | +$25.82 | **+18.3%** | Major |
| **ET** | $14.00 | $16.53 | +$2.53 | +18.1% | Major |
| **FSK** | $12.83 | $15.14 | +$2.31 | +18.0% | Major |
| **GBDC** | $11.99 | $13.99 | +$2.00 | +16.7% | Major |
| **EPD** | $26.38 | $30.70 | +$4.32 | +16.4% | Major |
| **AAPL** | $234.17 | $248.39 | +$14.22 | +6.1% | Moderate |
| **ASML** | $950.00 | $1027.37 | +$77.37 | +8.1% | Moderate |
| **AVGO** | $325.44 | $351.63 | +$26.19 | +8.0% | Moderate |

## Key Insights

### 1. Wide Spreads on Less Liquid Assets
- **MLPs/BDCs** (FSK, ET, EPD, GBDC): Spreads of 30-35%
- **ADRs** (BABA): Spread affects position pricing significantly
- These symbols drove most of the discrepancy

### 2. Position Price ≠ Bid/Ask
Alpaca's "position price" is typically:
- **NOT** the bid price (what you'd get selling immediately)
- **NOT** the ask price (what you'd pay buying immediately)
- **CLOSER TO** mid-price or last trade price
- Used consistently for P/L calculations by the broker

### 3. Bid/Ask Asymmetry
- Using BID: Under-values positions by **$1,837** (pessimistic)
- Using ASK: Over-values positions by **$804** (optimistic)
- Using MID: Still off by **$517** (better but not accurate)
- Using POSITION: **Exact match** with broker ✅

## Why Position Price Differs from Bid

Position price appears to be based on:
1. **Last traded price** - actual market transactions
2. **Mark price** - fair market value calculated by broker
3. **Mid-point price** - average of bid/ask when no recent trades

This is more representative of "true value" than bid (worst case) or ask (best case).

## Impact on Trading

### Old Widget Behavior (WRONG)
```python
# Used bid prices for P/L calculation
prices = account.get_instrument_current_price(symbols)  # Default price_type='bid'
pl = (bid_price - entry_price) * quantity
# Result: -$1,689.82 (wrong, too pessimistic)
```

### New Widget Behavior (CORRECT)
```python
# Uses broker position prices
broker_positions = account.get_positions()
for pos in broker_positions:
    price = pos['current_price']  # Alpaca's position price
    pl = (price - entry_price) * quantity
# Result: $147.64 (correct, matches broker)
```

## Recommendations

### ✅ DONE - Widget Fix
- Updated `FloatingPLPerExpertWidget` to use position prices
- Updated `FloatingPLPerAccountWidget` to use position prices
- Results now match Alpaca broker exactly

### ✅ DONE - Cache System
- Fixed price cache to distinguish bid/ask/mid prices
- Cache keys now include price_type: `"symbol:bid"`, `"symbol:ask"`, `"symbol:mid"`
- Prevents accidentally mixing price types

### 📋 Future Considerations

1. **Display Options**
   - Consider showing bid/ask alongside position price for transparency
   - Show spread % for positions to understand liquidity
   - Highlight positions with wide spreads (>10%)

2. **Risk Management**
   - Wide spreads (>20%) indicate illiquid positions
   - Position price may not reflect immediate exit value
   - Consider liquidity when sizing positions

3. **Price Type Selection**
   - **Use POSITION price** for: P/L calculations, performance tracking
   - **Use BID price** for: Conservative exit value estimates
   - **Use ASK price** for: Entry cost estimates
   - **Use MID price** for: Fair value analysis (but not for P/L!)

## Technical Details

### Price Fetching (After Fix)
```python
# Fetch specific price types using updated cache system
bid_prices = account.get_instrument_current_price(symbols, price_type='bid')
ask_prices = account.get_instrument_current_price(symbols, price_type='ask')
mid_prices = account.get_instrument_current_price(symbols, price_type='mid')

# Each caches separately:
# cache['AAPL:bid'] = 234.17
# cache['AAPL:ask'] = 259.04
# cache['AAPL:mid'] = 246.61
```

### Position Price Source
```python
# Get Alpaca's position prices (what broker uses for P/L)
positions = account.get_positions()
for pos in positions:
    current_price = pos['current_price']  # Mark/last trade price
    unrealized_pl = pos['unrealized_pl']  # Calculated using current_price
```

## Validation

Test run confirmed:
- ✅ Bid prices fetch correctly and cache with `:bid` suffix
- ✅ Ask prices fetch correctly and cache with `:ask` suffix
- ✅ Mid prices calculated correctly as (bid+ask)/2
- ✅ Position prices match Alpaca's unrealized P/L calculation
- ✅ Widget P/L now matches broker exactly ($147.64)

## Conclusion

The $1,837 discrepancy was caused by:
1. **Using wrong price type** - Bid instead of Position
2. **Wide spreads** on illiquid assets (30-35%)
3. **Lack of cache separation** - Could mix bid/ask prices (now fixed)

**Solution implemented:**
- Widgets use position prices directly from broker
- Price cache properly separates bid/ask/mid prices
- Test scripts validate correct behavior

**Result:** Widget P/L now matches broker exactly ✅
