# FMPSenateTrade Expert Test Script

**Date**: October 10, 2025  
**Purpose**: Direct testing of FMPSenateTrade expert without queue system  
**Files Created**:
- `test_files/test_fmp_senate_trade.py` - Main test script
- `test_files/test_fmp_senate_trade.bat` - Windows batch file launcher
- `test_files/test_fmp_senate_trade.ps1` - PowerShell launcher

## Overview

This test script allows developers to test the FMPSenateTrade expert directly, bypassing the WorkerQueue system. This is useful for:
- 🔧 Development and debugging
- ✅ Verifying fixes and enhancements
- 🔍 Testing specific symbols quickly
- 📊 Analyzing expert behavior in detail

## Usage

### Method 1: Python Script (All Platforms)

```bash
# Test AAPL (default)
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py

# Test any symbol
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py MSFT
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py TSLA
```

### Method 2: Batch File (Windows)

```cmd
# Test AAPL (default)
test_files\test_fmp_senate_trade.bat

# Test any symbol
test_files\test_fmp_senate_trade.bat MSFT
test_files\test_fmp_senate_trade.bat TSLA
```

### Method 3: PowerShell (Windows)

```powershell
# Test AAPL (default)
.\test_files\test_fmp_senate_trade.ps1

# Test any symbol
.\test_files\test_fmp_senate_trade.ps1 -Symbol MSFT
.\test_files\test_fmp_senate_trade.ps1 -Symbol TSLA
```

## What the Script Does

### Step 1: Create/Get Test Expert Instance
- Looks for existing "Test Senate Trade" expert instance
- If not found, creates a new FMPSenateTrade expert instance
- Uses first available account in database

### Step 2: Create Test Market Analysis
- Creates a new `MarketAnalysis` record
- Status: `PENDING` → `RUNNING` → `COMPLETED` (or `FAILED`)
- Links to the test expert instance

### Step 3: Initialize Expert
- Instantiates `FMPSenateTrade` class with expert instance ID
- Loads settings from database
- Displays current expert settings

### Step 4: Run Analysis
- Calls `expert.run_analysis(symbol, market_analysis)`
- Fetches senate/house trading data from FMP API
- Filters trades based on settings
- Calculates recommendation and confidence

### Step 5: Display Results
- Shows recommendation (BUY/SELL/HOLD)
- Shows confidence percentage
- Shows trade statistics (counts, amounts)
- Lists individual trades with details

## Output Example

```
╔════════════════════════════════════════════════════════════╗
║         FMPSenateTrade Expert Test Script                 ║
╚════════════════════════════════════════════════════════════╝

Testing symbol: AAPL
Timestamp: 2025-10-10 15:30:45

================================================================================
Testing FMPSenateTrade Expert for AAPL
================================================================================

[Step 1] Creating/Getting test expert instance...
Using existing test expert instance: 15

[Step 2] Creating test market analysis for AAPL...
Created test market analysis: 450 for AAPL

[Step 3] Initializing FMPSenateTrade expert (ID: 15)...
Expert initialized successfully
Expert settings: {'max_disclose_date_days': 30, 'max_trade_exec_days': 60, ...}

[Step 4] Loading market analysis (ID: 450)...

[Step 5] Running FMPSenateTrade analysis for AAPL...
--------------------------------------------------------------------------------
Fetching FMP senate trades for AAPL
Received 299 senate trade records for AAPL
Filtered 12 trades from 299 total
Created ExpertRecommendation (ID: 352) for AAPL: BUY with 68.5% confidence
Completed FMPSenateTrade analysis for AAPL: BUY (confidence: 68.5%, 12 trades)
--------------------------------------------------------------------------------
✅ Analysis completed successfully!

[Step 6] Analysis Results:
--------------------------------------------------------------------------------

Recommendation:
  Signal: BUY
  Confidence: 68.5%
  Expected Profit: 4.2%

Trade Statistics:
  Total Trades Found: 299
  Filtered Trades: 12
  Buy Trades: 8
  Sell Trades: 4
  Total Buy Amount: $850,000
  Total Sell Amount: $250,000

Individual Trades (12):

  Trade #1:
    Trader: John Doe
    Type: Purchase
    Amount: $50,001 - $100,000
    Exec Date: 2025-09-25 (15 days ago)
    Confidence: 72.3%
    Trader Performance: +8.5%

  Trade #2:
    Trader: Jane Smith
    Type: Purchase
    Amount: $100,001 - $250,000
    Exec Date: 2025-09-28 (12 days ago)
    Confidence: 65.7%
    Trader Performance: +5.2%

  ... and 10 more trades

================================================================================
✅ Test completed successfully!
Market Analysis ID: 450
You can view full results in the database or UI
================================================================================

Press Enter to exit...
```

## Testing Different Scenarios

### Test 1: Stock with Recent Senate Trading Activity
```bash
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py NVDA
```
**Expected**: Multiple trades found, recommendation generated

### Test 2: Stock with No Recent Activity
```bash
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py ZZZZZ
```
**Expected**: Few or no trades found, HOLD recommendation with low confidence

### Test 3: Blue Chip Stock
```bash
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py AAPL
```
**Expected**: Moderate activity, clear recommendation

### Test 4: Small Cap Stock
```bash
.venv\Scripts\python.exe test_files\test_fmp_senate_trade.py SOME_SMALL_CAP
```
**Expected**: Limited or no senate trading data

## Viewing Full Results

After running the test, you can view complete results in multiple ways:

### 1. Database Query
```sql
-- Get the market analysis
SELECT * FROM market_analysis WHERE id = 450;

-- Get the expert recommendation
SELECT * FROM expert_recommendation WHERE market_analysis_id = 450;

-- Get analysis outputs
SELECT * FROM analysis_output WHERE market_analysis_id = 450;
```

### 2. Web UI
1. Navigate to `/market_analysis/450` (use the ID from test output)
2. View full senate trade analysis with UI visualization

### 3. Log Files
```
logs/app.debug.log - Detailed debug information
```

## Troubleshooting

### Error: "No account found"
**Solution**: Create at least one account in the database first
```bash
# Run main application and create an account through UI
.venv\Scripts\python.exe main.py
```

### Error: "FMP API key not configured"
**Solution**: Add FMP_API_KEY to your `.env` file or app settings
```env
FMP_API_KEY=your_api_key_here
```

### Error: "Failed to fetch senate trades"
**Causes**:
- Invalid API key
- API rate limit exceeded
- Network connectivity issues
- Invalid symbol

**Solution**: Check logs for detailed error message

### Error: "No trades found"
**Causes**:
- Symbol has no recent senate/house trading activity
- Filtering settings too strict (check `max_disclose_date_days`, `max_trade_exec_days`)

**Solution**: 
- Try a different symbol
- Adjust expert settings to be less restrictive

## Settings Impact on Results

The test uses settings from the expert instance. Key settings that affect results:

| Setting | Effect |
|---------|--------|
| `max_disclose_date_days` | Trades disclosed more than N days ago are filtered out |
| `max_trade_exec_days` | Trades executed more than N days ago are filtered out |
| `max_trade_price_delta_pct` | Trades where price moved >N% are filtered out |

To test with different settings:
1. Update settings through UI before running test
2. Or modify settings in the test script programmatically

## Benefits of Direct Testing

### 1. **Speed**
- ⚡ No queue wait time
- ⚡ Immediate execution
- ⚡ Instant results

### 2. **Debugging**
- 🔍 Full stack traces for errors
- 🔍 Detailed logging output
- 🔍 Easy to add breakpoints

### 3. **Iteration**
- 🔄 Quick test-fix-test cycle
- 🔄 Test multiple symbols rapidly
- 🔄 Verify fixes immediately

### 4. **Development**
- 🛠️ Test new features
- 🛠️ Validate API changes
- 🛠️ Profile performance

## Comparison: Queue vs Direct Testing

| Aspect | Queue System | Direct Testing |
|--------|-------------|----------------|
| **Execution** | Asynchronous | Synchronous |
| **Speed** | Slower (queue wait) | Immediate |
| **Output** | Database + UI | Console + Database + UI |
| **Errors** | Logged to file | Full stack trace visible |
| **Debugging** | Harder | Easier |
| **Use Case** | Production | Development |

## Extending the Test Script

### Add Custom Settings
```python
# In test_fmp_senate_trade.py, after creating expert instance
session = get_db()
expert = session.query(ExpertInstance).get(expert_id)

# Add custom settings
from ba2_trade_platform.core.models import ExpertSetting
setting = ExpertSetting(
    expert_instance_id=expert_id,
    key='max_disclose_date_days',
    value_str='15'  # Test with tighter filter
)
session.add(setting)
session.commit()
session.close()
```

### Test Multiple Symbols
```python
symbols = ['AAPL', 'MSFT', 'TSLA', 'NVDA', 'AMD']
for symbol in symbols:
    print(f"\n{'='*80}")
    print(f"Testing {symbol}")
    print('='*80)
    test_fmp_senate_trade(symbol)
```

### Save Results to File
```python
import json

# After analysis completes
results = {
    'symbol': symbol,
    'timestamp': datetime.now().isoformat(),
    'recommendation': rec,
    'statistics': stats,
    'trades': trades
}

with open(f'results_{symbol}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json', 'w') as f:
    json.dump(results, f, indent=2)
```

## Related Files

- **Expert Implementation**: `ba2_trade_platform/modules/experts/FMPSenateTrade.py`
- **Test Script**: `test_files/test_fmp_senate_trade.py`
- **Database Models**: `ba2_trade_platform/core/models.py`
- **Queue System**: `ba2_trade_platform/core/WorkerQueue.py` (bypassed by this test)

## Best Practices

1. **Always test with virtual environment**: Use `.venv\Scripts\python.exe`
2. **Check logs after test**: Review `logs/app.debug.log` for details
3. **Clean up test data**: Remove test market analyses periodically
4. **Use realistic symbols**: Test with stocks that have senate trading activity
5. **Verify API key**: Ensure FMP_API_KEY is valid before testing

## Future Enhancements

1. **Batch Testing**: Test multiple symbols in one run
2. **Performance Profiling**: Measure execution time per step
3. **Result Comparison**: Compare results across different settings
4. **Mock API Data**: Test with cached API responses
5. **Automated Assertions**: Validate expected outcomes
