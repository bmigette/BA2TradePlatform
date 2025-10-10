# FMPSenateTrade Trader Statistics Enhancement

## Overview

Enhanced the FMPSenateTrade expert to provide time-based trader statistics, showing trading activity for both recent period (configurable via `max_exec_days`) and yearly period. This provides better insight into trader behavior patterns and recent activity trends.

## Changes Made

### 1. Enhanced `_calculate_trader_confidence()` Method

**Previous Behavior:**
- Analyzed ALL trades (all time) without time filtering
- Returned only confidence modifier (single float value)

**New Behavior:**
- Analyzes trades in TWO time windows:
  - **Recent Period**: Last N days (configurable via `max_exec_days`, default 60 days)
  - **Yearly Period**: Last 365 days
- Returns dictionary with detailed statistics:
  ```python
  {
      'confidence_modifier': float,
      'recent_buy_amount': float,
      'recent_sell_amount': float,
      'recent_buy_count': int,
      'recent_sell_count': int,
      'yearly_buy_amount': float,
      'yearly_sell_amount': float,
      'yearly_buy_count': int,
      'yearly_sell_count': int
  }
  ```

**Key Implementation Details:**
```python
# Time thresholds
now = datetime.now(timezone.utc)
max_exec_days = int(max_exec_days)  # Ensure it's an integer
recent_threshold = now - timedelta(days=max_exec_days)
yearly_threshold = now - timedelta(days=365)

# Track statistics for both periods
for trade in trader_history:
    exec_date = parse_date(trade['transactionDate'])
    amount = parse_amount(trade['amount'])
    
    # Recent period (max_exec_days)
    if exec_date >= recent_threshold:
        if is_buy:
            recent_buy_amount += amount
            recent_buy_count += 1
    
    # Yearly period
    if exec_date >= yearly_threshold:
        if is_buy:
            yearly_buy_amount += amount
            yearly_buy_count += 1
```

**Confidence Calculation:**
- Still based on **yearly** data (365 days) for stability
- Directional bias: `(yearly_buy - yearly_sell) / total_yearly_volume`
- Provides more reliable signal than short-term fluctuations

### 2. Updated Trade Details

**New Fields Added to `trade_info` Dictionary:**
```python
'trader_recent_buys': "43 ($3,115,522)",      # formatted string
'trader_recent_sells': "0 ($0)",               # formatted string
'trader_yearly_buys': "168 ($18,304,584)",     # formatted string
'trader_yearly_sells': "3 ($833,002)"          # formatted string
```

### 3. Enhanced Test Output

**Test File:** `test_files/test_fmp_senate_trade.py`

**Added Lines:**
```python
logger.info(f"    Trader Recent Activity: {trade.get('trader_recent_buys', 'N/A')} buys, {trade.get('trader_recent_sells', 'N/A')} sells")
logger.info(f"    Trader Yearly Activity: {trade.get('trader_yearly_buys', 'N/A')} buys, {trade.get('trader_yearly_sells', 'N/A')} sells")
```

**Example Output:**
```
Trade #3:
  Trader: Cleo Fields
  Type: purchase
  Amount: $100,001 - $250,000
  Exec Date: 2025-09-03 (37 days ago)
  Confidence: 80.9%
  Trader Pattern Modifier: +27.4%
  Trader Recent Activity: 43 ($3,115,522) buys, 0 ($0) sells  ← NEW!
  Trader Yearly Activity: 168 ($18,304,584) buys, 3 ($833,002) sells  ← NEW!
```

### 4. Enhanced Details String

**File:** `FMPSenateTrade.py` (~line 745)

**Added to Trade Analysis:**
```python
- Trader Recent Activity: {trade_info['trader_recent_buys']} buys, {trade_info['trader_recent_sells']} sells
- Trader Yearly Activity: {trade_info['trader_yearly_buys']} buys, {trade_info['trader_yearly_sells']} sells
```

### 5. Enhanced UI Display

**File:** `FMPSenateTrade.py` (~line 1180)

**Added UI Elements:**
```python
# Trader activity statistics
with ui.column().classes('mt-2 text-xs text-grey-6'):
    ui.label(f'Recent: {trade.get("trader_recent_buys", "N/A")} buys, {trade.get("trader_recent_sells", "N/A")} sells')
    ui.label(f'Yearly: {trade.get("trader_yearly_buys", "N/A")} buys, {trade.get("trader_yearly_sells", "N/A")} sells')
```

## Real-World Examples from Test

### Example 1: Cleo Fields (Strong Accumulator)

**Yearly Pattern:**
- 168 buys ($18,304,584)
- 3 sells ($833,002)
- **Bias: +0.91** (very bullish)

**Recent Activity (60 days):**
- 43 buys ($3,115,522)
- 0 sells ($0)
- **Pattern: Accelerating accumulation!** 🚀

**Analysis:**
- Trader is heavily buying stocks across portfolio
- Recent activity shows even more aggressive buying
- BUY signals get +27.4% confidence boost

### Example 2: Sheldon Whitehouse (Strong Distributor)

**Yearly Pattern:**
- 1 buy ($32,500)
- 49 sells ($1,196,024)
- **Bias: -0.95** (very bearish)

**Recent Activity (60 days):**
- 0 buys ($0)
- 15 sells ($340,508)
- **Pattern: Active distribution** 📉

**Analysis:**
- Trader is aggressively selling across portfolio
- Recent activity confirms selling trend
- SELL signals get +28.4% confidence boost

### Example 3: Shelley Moore Capito (Moderate Distributor)

**Yearly Pattern:**
- 22 buys ($225,011)
- 60 sells ($529,030)
- **Bias: -0.40** (moderately bearish)

**Recent Activity (60 days):**
- 0 buys ($0)
- 2 sells ($65,001)
- **Pattern: Light selling activity**

**Analysis:**
- Trader shows moderate selling bias
- Recent activity is lighter
- SELL signals get +12.1% confidence boost

## Benefits

### 1. Better Trend Detection
- **Recent activity** shows current momentum
- **Yearly activity** shows established pattern
- Can identify accelerating vs. decelerating traders

### 2. Context for Confidence
- Users can see WHY confidence is high/low
- Understand if trader is actively trading or dormant
- Assess if pattern is consistent or changing

### 3. Data Transparency
- Full visibility into trader behavior
- No hidden calculations
- Easy to verify and audit

### 4. Actionable Insights

**Strong Signal Indicators:**
- Recent activity aligns with yearly pattern
- High volume in both periods
- Example: Cleo Fields with 43 recent buys out of 168 yearly

**Weak Signal Indicators:**
- Recent activity contradicts yearly pattern
- Low volume in recent period
- Dormant trader with no recent activity

## Performance Impact

### API Calls
- **No additional API calls required**
- Uses existing trader history data
- Just adds date-based filtering and aggregation

### Processing Time
- Minimal overhead (~0.01 seconds per trader)
- Date parsing and amount parsing already done
- Just adds counters and comparisons

### Memory Usage
- Stores 4 additional strings per trade (formatted)
- Example: `"43 ($3,115,522)"` ~20 bytes
- Negligible impact overall

## Configuration

**Setting Used:** `max_exec_days`
- Controls "Recent Activity" time window
- Default: 60 days
- Affects both filtering AND statistics calculation

**Example:**
- `max_exec_days = 30`: Shows last 30 days activity
- `max_exec_days = 90`: Shows last 90 days activity

## Testing

### Test Command
```bash
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py AAPL
```

### Expected Output
Each trade should display:
1. Trader name and trade details
2. Confidence score
3. Pattern modifier
4. **Recent Activity** (last N days)
5. **Yearly Activity** (last 365 days)

### Sample Output
```
Trade #3:
    Trader: Cleo Fields
    Type: purchase
    Amount: $100,001 - $250,000
    Confidence: 80.9%
    Trader Pattern Modifier: +27.4%
    Trader Recent Activity: 43 ($3,115,522) buys, 0 ($0) sells
    Trader Yearly Activity: 168 ($18,304,584) buys, 3 ($833,002) sells
```

## Debug Logging

Added enhanced debug logging to help troubleshoot:

```
Trader pattern (yearly): 168 buys ($18,304,584), 3 sells ($833,002) -> bias: 0.91, modifier: 27.4%
Trader pattern (recent 60d): 43 buys ($3,115,522), 0 sells ($0)
```

Shows:
- Yearly statistics and calculated bias
- Recent activity for comparison
- Helps identify trends and patterns

## UI Improvements

### Before
```
Trade #1: Shelley Moore Capito
sale - $15,001 - $50,000
Exec: 2025-09-18 (22d ago)
Confidence: 62.7%
Pattern: +12.1%
```

### After
```
Trade #1: Shelley Moore Capito
sale - $15,001 - $50,000
Exec: 2025-09-18 (22d ago)
Disclosed: 2025-09-25 (15d ago)

Recent: 0 ($0) buys, 2 ($65,001) sells     ← NEW!
Yearly: 22 ($225,011) buys, 60 ($529,030) sells  ← NEW!

Confidence: 62.7%
Pattern: +12.1%
```

## Future Enhancements

Consider adding:

1. **Trend Indicators**
   - "↑ Accelerating" if recent > yearly average
   - "↓ Decelerating" if recent < yearly average

2. **Visual Indicators**
   - Color code based on activity level
   - Green for active buyers, red for active sellers

3. **Activity Score**
   - Normalize activity across different time periods
   - Compare trader activity to peers

4. **Historical Snapshots**
   - Track how patterns change over time
   - Identify trend reversals early

## Summary

This enhancement provides **time-based context** for trader behavior analysis:

✅ **Recent Activity** (configurable window) - Shows current momentum
✅ **Yearly Activity** (365 days) - Shows established pattern  
✅ **No Performance Impact** - Uses existing data efficiently
✅ **Full Transparency** - All data visible to user
✅ **Better Decisions** - Context enables better trade assessment

The feature helps users understand:
- Is the trader actively trading now?
- Is their current behavior consistent with history?
- Should I trust this signal more or less?

Example insights:
- "Cleo Fields bought 43 times in 60 days out of 168 yearly → Very active accumulator!"
- "Sheldon Whitehouse sold 15 times in 60 days, 0 buys → Strong distribution pattern"
