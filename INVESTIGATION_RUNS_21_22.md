# SmartRiskManagerJob Run Analysis - Runs #21 and #22

## Summary of Findings

### Run #22 - TA-Dynamic-grok (Allocation Bug: 7% vs 5% limit)

**Status**: COMPLETED (but with BUG)  
**Expert**: TradingAgents / TA-Dynamic-grok (Expert ID: 11)  
**Model Used**: grok-4-0709  
**Actions Taken**: 17  
**Initial Equity**: $5,006.62  
**Final Equity**: $5,005.48  

#### **THE BUG: Allocation Exceeds 5% Limit**

The Smart Risk Manager opened 7 new positions, but the total allocation exceeded the allowed 5% per position limit:

| Symbol | Quantity | Price | Notional Value | % of Portfolio | Stated Limit |
|--------|----------|-------|-----------------|----------------|--------------|
| KO | 10 | $70.62 | $706.20 | 14.1% | 5% max |
| PG | 5 | $145.70 | $728.50 | 14.5% | 5% max |
| PFE | 30 | $24.47 | $734.10 | 14.7% | 5% max |
| JPM | 2 | $316.40 | $632.80 | 12.6% | 5% max |
| HMY | 40 | $18.20 | $728.00 | 14.5% | 5% max |
| UNH | 2 | $322.15 | $644.30 | 12.9% | 5% max |
| JNJ | 4 | $188.90 | $755.60 | 15.1% | 5% max |
| **TOTAL** | | | **$5,129.50** | **102.4%** | ~5% each |

#### Root Cause Analysis

The issue is in the Smart Risk Manager's position sizing calculation. Instead of allocating 5% per position (~$250 with initial equity of $5006.62), the model allocated **~$730 per position** which is actually **~14.6% of portfolio per position** OR **~2.9% per new position** if they were meant to be equal-weighted across 7 new positions.

The LLM's reasoning in the graph_state says: 
> "Quantities calculated to keep individual positions under $1001 limit, with optional SL/TP set at ~5% below current..."

This shows the LLM was trying to respect the **$1001 (20% portfolio limit)** but MISSED the **5% per-position guideline**. The LLM allocated based on hitting price points rather than portfolio percentage.

#### Expected Behavior

If respecting the 5% limit with $5,006 equity:
- Max per position: ~$250
- For 7 positions spread evenly: ~$35 each
- OR if each position should be 5%: Need $7,500+ to afford 7x $250 positions

#### Impact

- **Account Risk Increase**: Positions are 2.8x-3x larger than allowed, amplifying portfolio volatility
- **Regulatory/Risk Compliance**: Violates internal position sizing constraints
- **Diversity Loss**: While sector diversification improved, size concentration offsets benefits

---

### Run #21 - TA-Dynamic-QwenMax (Failure Investigation)

**Status**: FAILED  
**Expert**: TradingAgents / TA-Dynamic-QwenMax (Expert ID: 14)  
**Model Used**: (unknown - job failed before execution)  
**Actions Taken**: 0  
**Initial Equity**: $5,000.63  
**Final Equity**: $5,000.63 (no change)  
**Error Message**: None stored (status is FAILED but no error_message field populated)

#### Investigation

The job shows status=FAILED with 0 actions executed and no error message recorded. This indicates:

1. **Early Failure**: Job failed before any LLM execution (likely during initialization/setup)
2. **Missing Error Context**: The error_message field is NULL, making root cause diagnosis impossible
3. **No Graph State**: Since job failed early, no graph_state is likely available

#### Recommended Actions

1. **Check application logs** at `logs/` directory for the exact error
2. **Check SmartRiskManagerJob execution code** for why error_message isn't being populated
3. **Look for stack traces** around timestamp 2025-11-11 (when run #21 was created)

---

## Recommended Fixes

### For Run #22 Bug (Position Sizing)

**File**: `ba2_trade_platform/modules/experts/[SmartRiskManager].py` (or where position opening logic lives)

**Issue**: Position sizing calculation uses absolute $ amounts instead of portfolio percentages

**Fix**:
```python
# Calculate portfolio percentage-based size
max_position_pct = 0.05  # 5% per position
position_size = account_balance * max_position_pct

# THEN calculate quantity based on current price
quantity = int(position_size / current_price)

# DO NOT use: quantity = position_size / current_price without capping
```

### For Run #21 (Missing Error Logging)

**File**: SmartRiskManager execution code

**Issue**: When job fails, error_message isn't being populated

**Fix**: Add try-catch at the top level that captures error to `error_message` field:

```python
try:
    # Execute job
    ...
except Exception as e:
    job.error_message = str(e)
    job.status = "FAILED"
    session.add(job)
    session.commit()
```

---

## Questions for User

1. What is the **exact 5% limit** - per individual position or aggregate new positions?
2. Should the Smart Risk Manager **refuse to execute** if it can't fit within limits, or should it **scale down quantities**?
3. Why is `error_message` NULL for failed run #21? Should be non-null for debugging.
