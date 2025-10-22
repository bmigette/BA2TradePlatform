# Bug: OPEN_POSITIONS Recommendations Not Evaluated Against Ruleset

## Summary
OPEN_POSITIONS recommendations are created but never evaluated against the open_positions ruleset. This means recommended actions (BUY, SELL, HOLD, CLOSE) are logged to the database but never result in actual trading actions being executed.

## Affected Case
- **Expert:** 9 (TradingAgents)
- **Symbol:** LRCX
- **Analysis Type:** OPEN_POSITIONS
- **Recommendation:** HOLD with trigger condition `profit_loss_percent > -10.0` [actual: -6.50]
- **Expected Action:** Close existing position (sell long or buy to cover short)
- **Actual Result:** No action executed - recommendation logged only

## Root Cause Analysis

### WorkerQueue.py Line 659 (WorkerQueue.execute_worker method)
```python
# Check if this was the last ENTER_MARKET analysis task for this expert
# If so, trigger automated order processing
if task.subtype == AnalysisUseCase.ENTER_MARKET:
    self._check_and_process_expert_recommendations(task.expert_instance_id)
```

**Problem:** The code only processes recommendations for `ENTER_MARKET` analysis tasks. When an `OPEN_POSITIONS` task completes, no automated processing occurs.

### Missing Flow
1. Analysis completes for `OPEN_POSITIONS` task
2. Expert creates `ExpertRecommendation` with action (e.g., HOLD, CLOSE, BUY, SELL)
3. **No code path exists to evaluate this recommendation against the `open_positions_ruleset`**
4. No `TradeAction` records created
5. No `TradingOrder` records created
6. Recommendation is effectively ignored

### Current Architecture

**For ENTER_MARKET:**
```
Analysis Task (ENTER_MARKET)
  ↓
Expert generates ExpertRecommendation
  ↓
WorkerQueue.execute_worker() completes task
  ↓
_check_and_process_expert_recommendations() called
  ↓
TradeManager.process_expert_recommendations_after_analysis()
  ↓
TradeActionEvaluator.evaluate() with enter_market_ruleset
  ↓
TradeAction + TradingOrder created (if conditions met)
```

**For OPEN_POSITIONS:**
```
Analysis Task (OPEN_POSITIONS)
  ↓
Expert generates ExpertRecommendation
  ↓
WorkerQueue.execute_worker() completes task
  ↓
[NO PROCESSING] ← BUG IS HERE
  ↓
Recommendation sits in database unused
```

## Why This Matters

The open_positions ruleset is configured with triggers like:
- `profit_loss_percent > -10.0` → Action: Close
- `rsi > 70` → Action: Sell (take profit)
- `confidence < 30` → Action: Close (reduce confidence)

Without evaluation against the ruleset, these triggers have zero effect on actual trading positions, making the entire OPEN_POSITIONS analysis feature non-functional for automated trading.

## Solution Required

Need to add code similar to `_check_and_process_expert_recommendations()` but for OPEN_POSITIONS:

### Option 1: Extend existing method
Modify `_check_and_process_expert_recommendations()` to handle both ENTER_MARKET and OPEN_POSITIONS:
- For ENTER_MARKET: call `TradeManager.process_expert_recommendations_after_analysis()`
- For OPEN_POSITIONS: call new method `TradeManager.process_open_positions_recommendations()`

### Option 2: Add separate handler
Create `_check_and_process_open_positions_recommendations()` similar to the existing method but for OPEN_POSITIONS analysis tasks.

### Option 3: Unified recommendation processor
Create a single method that handles both use cases:
```python
def _process_analysis_recommendations(self, expert_instance_id: int, use_case: AnalysisUseCase):
    if use_case == AnalysisUseCase.ENTER_MARKET:
        # Process enter_market recommendations
    elif use_case == AnalysisUseCase.OPEN_POSITIONS:
        # Process open_positions recommendations
```

## Implementation Steps

1. Add method to `TradeManager` to process OPEN_POSITIONS recommendations
   - Similar to `process_expert_recommendations_after_analysis()` but:
   - Uses `open_positions_ruleset_id` instead of `enter_market_ruleset_id`
   - Only processes recommendations for symbols with existing positions
   - Filters recommendations by `recommended_action` (CLOSE, SELL, BUY should trigger actions; HOLD might not)

2. Update `WorkerQueue.execute_worker()` to handle OPEN_POSITIONS:
   ```python
   if task.subtype == AnalysisUseCase.ENTER_MARKET:
       self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.ENTER_MARKET)
   elif task.subtype == AnalysisUseCase.OPEN_POSITIONS:
       self._check_and_process_expert_recommendations(task.expert_instance_id, AnalysisUseCase.OPEN_POSITIONS)
   ```

3. Ensure thread safety with appropriate locking

4. Test with existing OPEN_POSITIONS recommendations:
   - Verify ruleset evaluation occurs
   - Verify TradeAction records created for met conditions
   - Verify TradingOrder records created for actionable recommendations

## Related Code References

- `WorkerQueue.execute_worker()` - Line 659
- `WorkerQueue._check_and_process_expert_recommendations()` - Line 679
- `TradeManager.process_expert_recommendations_after_analysis()` - Line 841
- `ExpertInstance.open_positions_ruleset_id` - models.py line 32
- Analysis Task Types - `core/types.py` (AnalysisUseCase enum)

## Verification

Once fixed, LRCX OPEN_POSITIONS analysis should:
1. ✅ Generate HOLD recommendation
2. ✅ Evaluate trigger: `profit_loss_percent > -10.0` (-6.50 > -10.00 = TRUE)
3. ✅ Create TradeAction for "Close existing position"
4. ✅ Create TradingOrder with SELL side for LRCX position
5. ✅ Order awaits risk management or execution based on settings
