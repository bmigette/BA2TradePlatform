# Ruleset Testing Script

## Overview
`test_rulesets.py` is a comprehensive testing tool for evaluating how rulesets perform against market analysis recommendations. It helps you:

- **Test ruleset configurations** before enabling automated trading
- **Debug ruleset logic** to understand why recommendations pass or fail
- **Analyze historical performance** of your ruleset rules
- **Safely validate changes** in dry-run mode (no orders created)

## Features

✅ **Dry-Run Mode** (default) - Test without creating orders  
✅ **Live Mode** - Actually create orders for testing  
✅ **Specific Expert Testing** - Test individual expert instances  
✅ **Historical Analysis** - Test recommendations older than 24 hours  
✅ **Targeted Testing** - Test specific recommendation IDs  
✅ **Detailed Evaluation** - See exactly why recommendations pass/fail  
✅ **Batch Testing** - Test all enabled experts at once  

## Usage

### Basic Usage

Test all enabled experts with recent recommendations (last 24 hours):
```bash
.venv\Scripts\python.exe test_rulesets.py
```

### Test Specific Expert

Test a single expert instance:
```bash
.venv\Scripts\python.exe test_rulesets.py --expert-id 1
```

### Test Specific Recommendations

Test specific recommendation IDs (useful for older recommendations):
```bash
.venv\Scripts\python.exe test_rulesets.py --recommendation-ids 123,456,789
```

### Test Older Recommendations

Look back further than 24 hours (e.g., 7 days):
```bash
.venv\Scripts\python.exe test_rulesets.py --hours 168
```

### Combine Options

Test specific expert with 7 days of history:
```bash
.venv\Scripts\python.exe test_rulesets.py --expert-id 1 --hours 168
```

### Live Mode (Create Orders)

⚠️ **Warning**: This will actually create PENDING orders!
```bash
.venv\Scripts\python.exe test_rulesets.py --no-dry-run
```

### Verbose Output

Show detailed evaluation information:
```bash
.venv\Scripts\python.exe test_rulesets.py --verbose
```

## Command-Line Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--expert-id N` | Test specific expert instance ID | All enabled experts |
| `--recommendation-ids X,Y,Z` | Test specific recommendation IDs | Recent recommendations |
| `--hours N` | Look back N hours for recommendations | 24 |
| `--no-dry-run` | Actually create orders (not dry-run) | Dry-run enabled |
| `--verbose` or `-v` | Show detailed evaluation info | Brief output |

## Output Explanation

### Expert Information
```
Testing Expert Instance ID: 1
================================================================================

Expert Type: TradingAgents
Enabled: True
Account ID: 1
Allow Automated Trade Opening: True
Allow Automated Trade Modification: False

📋 Enter Market Ruleset: Conservative Entry
   Description: Only high confidence trades with low risk
   Event Actions: 3
```

### Recommendation Evaluation
```
[1/5] Testing Recommendation #123
  Symbol: AAPL
  Action: buy
  Confidence: 0.85
  Risk Level: LOW
  Expected Profit: 12.5%
  Time Horizon: SHORT_TERM
  Created: 2025-09-30 10:30:00
  📋 Evaluating against ruleset: Conservative Entry
  ✅ PASSED ruleset evaluation
  💭 DRY RUN: Would create PENDING order
```

### Summary
```
================================================================================
SUMMARY - Expert 1
================================================================================
Total Recommendations: 5
✅ Passed Ruleset: 3
❌ Failed Ruleset: 2
Success Rate: 60.0%

✓ DRY RUN MODE: No orders created (would create 3 orders)
```

## Common Use Cases

### 1. Test New Ruleset Configuration

Before enabling automated trading:
```bash
# Test with recent data
.venv\Scripts\python.exe test_rulesets.py --expert-id 1

# Test with historical data
.venv\Scripts\python.exe test_rulesets.py --expert-id 1 --hours 168
```

### 2. Debug Why Recommendations Are Rejected

Run with verbose mode to see detailed evaluation:
```bash
.venv\Scripts\python.exe test_rulesets.py --expert-id 1 --verbose
```

Check the logs in `ba2_trade_platform/logs/app.debug.log` for detailed TradeManager evaluation output.

### 3. Test Specific Failed Recommendations

If you see recommendations in the UI that you think should have passed:
```bash
# Get the recommendation IDs from the database or UI
.venv\Scripts\python.exe test_rulesets.py --recommendation-ids 123,456
```

### 4. Validate Changes to Ruleset

After modifying a ruleset:
```bash
# Test against same historical data
.venv\Scripts\python.exe test_rulesets.py --expert-id 1 --hours 168
```

Compare the results before and after your changes.

### 5. Performance Analysis

Test all experts to see overall ruleset performance:
```bash
.venv\Scripts\python.exe test_rulesets.py --hours 168
```

This shows which experts have effective rulesets and which need tuning.

## Understanding Results

### ✅ Passed Ruleset
- Recommendation met all ruleset criteria
- In live mode: PENDING order would be created
- In dry-run: No action taken (just logged)

### ❌ Failed Ruleset
- Recommendation rejected by at least one ruleset rule
- No order created
- Check logs to see which specific rule failed

### ⚠️ No Ruleset Assigned
- No enter_market ruleset configured
- Recommendation would pass by default (if automated trading enabled)
- Consider assigning a ruleset for better control

## Best Practices

1. **Always test in dry-run first**: Default mode prevents accidental order creation
2. **Test with historical data**: Use `--hours 168` to test with a week of data
3. **Compare before/after**: Run tests before and after ruleset changes
4. **Check logs**: Detailed evaluation logic is in `app.debug.log`
5. **Start conservative**: Begin with strict rulesets, then loosen as needed
6. **Regular validation**: Periodically test to ensure rulesets still work as expected

## Troubleshooting

### No Recommendations Found
- Check if expert has run market analysis recently
- Increase `--hours` value
- Use `--recommendation-ids` to test specific older recommendations

### All Recommendations Fail
- Check ruleset configuration in Settings page
- Review ruleset rules - they may be too strict
- Check logs for specific failure reasons

### Can't Create Orders (Live Mode)
- Ensure expert has "Allow automated trade opening" enabled
- Ensure enter_market ruleset is assigned
- Check account configuration

## Integration with Workflow

This script integrates with the automated trading workflow:

```
Market Analysis → Recommendations → test_rulesets.py → Validate Rulesets
                                         ↓
                              Adjust Rules if Needed
                                         ↓
                              Enable Automated Trading
                                         ↓
                  TradeManager → Ruleset Evaluation → PENDING Orders → Review
```

## Example Session

```bash
# 1. Test current configuration
.venv\Scripts\python.exe test_rulesets.py --expert-id 1
# Result: 2/10 recommendations passed (20%)

# 2. Review and adjust ruleset in UI
# (Loosen confidence requirement from 0.9 to 0.7)

# 3. Test again with same data
.venv\Scripts\python.exe test_rulesets.py --expert-id 1 --hours 24
# Result: 5/10 recommendations passed (50%)

# 4. Satisfied with results, enable automated trading
# (Set "Allow automated trade opening" in expert settings)

# 5. Monitor real execution
# Orders appear in Account Overview for review
```

## See Also

- **Settings Page** → Configure rulesets and assign to experts
- **Account Overview** → Review PENDING orders created by automation
- **Market Analysis** → View recommendations being evaluated
- **RULESET_AUTOMATION_IMPLEMENTATION.md** → Technical documentation
