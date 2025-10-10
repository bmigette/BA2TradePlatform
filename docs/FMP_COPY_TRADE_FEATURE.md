# FMP Senate Trade Copy Trading Feature

**Date:** 2025-10-10  
**Status:** ✅ Complete  
**Feature:** Copy trading specific senators/representatives

## Overview

Added a **copy trading mode** to the FMPSenateTrade expert that allows users to automatically follow specific government officials' trades with maximum confidence, bypassing the weighted algorithm entirely.

## Feature Description

### Copy Trade Mode

When enabled, the expert will:
1. **Immediately generate recommendations** when a tracked trader makes a trade
2. **Use 100% confidence** (maximum conviction)
3. **Set 50% expected profit target**
4. **Bypass all weighted algorithm calculations**
5. **Ignore all other traders' activity**

### Normal Mode (Default)

When disabled (default), the expert uses the standard weighted algorithm:
1. Analyzes all filtered trades
2. Calculates symbol focus percentage for each trader
3. Determines net position (buy/sell trades cancel)
4. Confidence: 50 + (symbol_focus_pct × 5)
5. Expected profit: 50 + (symbol_focus_pct × 5)

## Configuration

### New Setting

**Setting Name:** `copy_trade_names`

**Type:** String (comma-separated list)

**Default:** `""` (empty = disabled)

**Description:** Names of senators/representatives to copy trade

**Example Values:**
- `""` - Disabled (use weighted algorithm)
- `"Nancy Pelosi"` - Copy trade Nancy Pelosi only
- `"Nancy Pelosi, Josh Gottheimer"` - Copy trade multiple people
- `"Cleo Fields"` - Copy trade Cleo Fields

**Tooltip:** 
> Enter names of senators/representatives to copy trade (e.g., 'Nancy Pelosi, Josh Gottheimer'). Any trade by these people will generate 100% confidence BUY/SELL recommendation with 50% expected profit, bypassing the weighted algorithm. Leave empty to use normal weighted algorithm for all traders.

## Implementation Details

### Setting Definition

Location: `FMPSenateTrade.get_settings_definitions()`

```python
"copy_trade_names": {
    "type": "str",
    "required": False,
    "default": "",
    "description": "Copy trade specific traders (comma-separated)",
    "tooltip": "..."
}
```

### Copy Trade Detection

Location: `_calculate_recommendation()` method (~line 630)

**Logic Flow:**

1. **Parse setting:**
   ```python
   copy_trade_names_setting = self.settings.get('copy_trade_names', '').strip()
   copy_trade_names = [name.strip().lower() for name in copy_trade_names_setting.split(',') if name.strip()]
   ```

2. **Check each filtered trade:**
   ```python
   for trade in filtered_trades:
       first_name = trade.get('firstName', '').lower()
       last_name = trade.get('lastName', '').lower()
       full_name = f"{first_name} {last_name}".strip()
       
       # Partial match on any part of name
       for target_name in copy_trade_names:
           if target_name in full_name or target_name in first_name or target_name in last_name:
               is_copy_trade_target = True
               break
   ```

3. **Generate immediate recommendation:**
   ```python
   if is_copy_trade_target:
       # Determine signal from transaction type
       is_buy = 'purchase' in transaction_type or 'buy' in transaction_type
       is_sell = 'sale' in transaction_type or 'sell' in transaction_type
       
       signal = OrderRecommendation.BUY if is_buy else OrderRecommendation.SELL
       
       # Fixed values for copy trade
       confidence = 100.0
       expected_profit = 50.0 if signal == OrderRecommendation.BUY else -50.0
       
       # Return immediately, skip weighted algorithm
       return {...}
   ```

### Name Matching

**Matching Strategy:** Partial, case-insensitive match

**Examples:**
- Setting: `"Nancy Pelosi"` → Matches: "Nancy Pelosi", "NANCY PELOSI", "nancy pelosi"
- Setting: `"Pelosi"` → Matches: "Nancy Pelosi", "Paul Pelosi"
- Setting: `"Fields"` → Matches: "Cleo Fields"

**Priority:** First matching trade wins (others ignored)

## Output Examples

### Copy Trade Mode Output

```
FMP Senate/House Trading Analysis - COPY TRADE MODE

Current Price: $255.39

🎯 COPY TRADING: Cleo Fields

Trade Details:
- Trader: Cleo Fields
- Type: purchase
- Amount: $100,001 - $250,000
- Execution Date: 2025-09-03 (37 days ago)
- Disclosure Date: 2025-10-01 (9 days ago)
- Execution Price: $237.21
- Current Price: $255.39
- Price Change: +7.7%

Overall Signal: BUY
Confidence: 100.0% (Copy Trade - Maximum Confidence)
Expected Profit: +50.0%

Copy Trade Mode Active:
This trader is in your copy trade list. Recommendations are generated immediately
with 100% confidence and 50% expected profit target, bypassing the weighted algorithm.

Note: Only the first matching trade from your copy trade list is used.
Other trades are ignored in copy trade mode.
```

### Normal Mode Output (for comparison)

```
FMP Senate/House Trading Analysis

Current Price: $255.39

Trade Activity Summary:
- Total Relevant Trades: 4
- Buy Trades: 2 ($250,001)
- Sell Trades: 2 ($65,001)

Overall Signal: HOLD
Confidence: 96.3%
Expected Profit: 96.3%

[...individual trade analysis...]

Signal Determination:
- Net trades: 2 buys - 2 sells = 0
- Buys and sells cancel each other (senators voting with their trades)
- More buys = BUY signal, more sells = SELL signal
```

## Usage

### Via UI (Future)

1. Navigate to FMP Senate Trade expert settings
2. Enter trader names in "Copy trade specific traders" field
3. Save settings
4. Run analysis on any symbol

### Via Database/Script

```python
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertSetting
from sqlmodel import select

session = get_db()

# Enable copy trade for Nancy Pelosi
stmt = select(ExpertSetting).where(
    ExpertSetting.instance_id == expert_id,
    ExpertSetting.key == 'copy_trade_names'
)
setting = session.exec(stmt).first()

if setting:
    setting.value_str = "Nancy Pelosi"
else:
    setting = ExpertSetting(
        instance_id=expert_id,
        key='copy_trade_names',
        value_str="Nancy Pelosi"
    )
    session.add(setting)

session.commit()
```

### Using Test Script

```bash
# Enable copy trade for Cleo Fields
.venv\Scripts\python.exe test_files\test_copy_trade.py "Cleo Fields"

# Enable copy trade for multiple traders
.venv\Scripts\python.exe test_files\test_copy_trade.py "Nancy Pelosi, Josh Gottheimer"

# Disable copy trade (use weighted algorithm)
.venv\Scripts\python.exe test_files\test_copy_trade.py ""
```

## Test Results

**Test Scenario:** AAPL with Cleo Fields in filtered trades

### Copy Trade Mode Enabled

**Setting:** `copy_trade_names = "Cleo Fields"`

**Result:**
- ✅ Signal: **BUY** (Cleo Fields made a purchase)
- ✅ Confidence: **100.0%** (fixed)
- ✅ Expected Profit: **50.0%** (fixed)
- ✅ Trade Count: **1** (only Cleo Fields' trade)
- ✅ Ignored: 3 other trades (2 sells, 1 buy by other traders)

### Copy Trade Mode Disabled

**Setting:** `copy_trade_names = ""`

**Result:**
- ✅ Signal: **HOLD** (2 buys - 2 sells = 0 net)
- ✅ Confidence: **96.3%** (50 + 9.25 * 5)
- ✅ Expected Profit: **96.3%**
- ✅ Trade Count: **4** (all trades analyzed)
- ✅ Weighted algorithm applied

## Benefits

### 1. **Simplicity**
- No complex calculations
- Immediate actionable signals
- Clear conviction level

### 2. **Trust-Based Trading**
- Follow specific "smart money" traders
- Proven track record traders only
- Personal confidence in specific individuals

### 3. **Speed**
- Bypasses expensive API calls (trader history)
- No portfolio analysis needed
- Instant recommendations

### 4. **Risk Management**
- Fixed 50% profit target
- 100% confidence for position sizing
- Clear entry/exit expectations

## Edge Cases

### 1. **Multiple Matching Trades**

**Scenario:** Multiple copy trade targets in same symbol

**Behavior:** First match wins, others ignored

**Rationale:** Avoid conflicting signals (one buying, one selling)

### 2. **No Matching Trades**

**Scenario:** Copy trade list set, but no matching trades found

**Behavior:** Falls back to weighted algorithm

**Example:** Setting = "Nancy Pelosi", but only Cleo Fields traded AAPL

### 3. **Partial Name Matches**

**Scenario:** Setting = "Pelosi"

**Matches:** "Nancy Pelosi", "Paul Pelosi", etc.

**Behavior:** All partial matches are treated as copy trade targets

### 4. **Empty String vs Null**

**Both treated as disabled:**
- `copy_trade_names = ""`
- `copy_trade_names = null`
- Setting not present

## Files Modified

1. **`ba2_trade_platform/modules/experts/FMPSenateTrade.py`**
   - Added `copy_trade_names` setting definition
   - Added copy trade detection logic in `_calculate_recommendation()`
   - Added special formatting for copy trade output

2. **`test_files/test_copy_trade.py`** (NEW)
   - Helper script to set copy trade names
   - Supports command-line arguments
   - Clear status output

## Future Enhancements

### 1. **Multiple Trader Aggregation**

Instead of "first match wins", aggregate all matching trades:
- 2 matching traders buying → Stronger BUY signal
- 1 buying, 1 selling → Cancel out (HOLD)

### 2. **Custom Confidence/Profit Per Trader**

```python
"copy_trade_settings": {
    "Nancy Pelosi": {"confidence": 100, "profit": 50},
    "Josh Gottheimer": {"confidence": 90, "profit": 40}
}
```

### 3. **Weighted Copy Trading**

Combine copy trade with weighted algorithm:
- Copy trader confidence: 100%
- Other traders: Weighted average
- Final: Weighted combination

### 4. **Trade History Requirements**

Only copy if trader has:
- Minimum trade count
- Minimum success rate
- Minimum time horizon

## Conclusion

The copy trading feature provides a powerful alternative to the weighted algorithm for users who want to follow specific government officials' trades with maximum conviction. It simplifies decision-making and provides clear, actionable signals based on trust in specific individuals rather than complex portfolio analysis.

**Key Advantages:**
- ✅ 100% confidence for position sizing
- ✅ 50% profit targets
- ✅ Instant recommendations
- ✅ Bypasses complex calculations
- ✅ Trust-based trading approach

**Production Ready:** ✅ Yes

---

**Related Documentation:**
- Main Implementation: `ba2_trade_platform/modules/experts/FMPSenateTrade.py`
- Test Script: `test_files/test_copy_trade.py`
- Symbol Focus Feature: `docs/FMP_SYMBOL_FOCUS_IMPLEMENTATION.md`
