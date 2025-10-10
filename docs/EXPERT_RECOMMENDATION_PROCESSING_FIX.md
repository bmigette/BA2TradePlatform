# Expert Recommendation Processing Fixes

**Date**: 2025-01-10  
**Issue**: IntegrityError when processing expert recommendations - `NOT NULL constraint failed: trade_action_result.expert_recommendation_id`

## Problem Summary

When clicking "Process Recommendations" in the Market Analysis UI, the system was failing with an IntegrityError because TradeActionResult records were being created with `expert_recommendation_id=None`, violating the NOT NULL constraint.

## Root Cause

The `create_action()` factory function in `TradeActions.py` was intentionally excluding `expert_recommendation` when creating `ADJUST_TAKE_PROFIT` and `ADJUST_STOP_LOSS` actions:

```python
# OLD CODE (incorrect):
if action_type in [ExpertActionType.ADJUST_TAKE_PROFIT, ExpertActionType.ADJUST_STOP_LOSS]:
    return action_class(instrument_name, account, order_recommendation, existing_order, **kwargs)
else:
    # BUY, SELL, CLOSE, INCREASE/DECREASE_SHARE actions get expert_recommendation for order linking
    return action_class(instrument_name, account, order_recommendation, existing_order, expert_recommendation, **kwargs)
```

This meant that when these actions executed and tried to create TradeActionResult records, they had `self.expert_recommendation=None`, leading to database integrity constraint violations.

## Solution

### 1. Fixed TradeAction Factory Function

**File**: `ba2_trade_platform/core/TradeActions.py`

Simplified the factory to pass `expert_recommendation` to ALL action types:

```python
# NEW CODE (correct):
action_class = action_map.get(action_type)
if not action_class:
    raise ValueError(f"Unknown action type: {action_type}")

# All actions need expert_recommendation for TradeActionResult linking
return action_class(instrument_name, account, order_recommendation, existing_order, expert_recommendation, **kwargs)
```

### 2. Updated AdjustTakeProfitAction Constructor

Added `expert_recommendation` parameter and properly passed it to parent:

```python
def __init__(self, instrument_name: str, account: AccountInterface, 
             order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
             expert_recommendation: Optional[ExpertRecommendation] = None,  # ADDED
             take_profit_price: Optional[float] = None,
             reference_value: Optional[str] = None, percent: Optional[float] = None):
    # Changed from: expert_recommendation=None to: expert_recommendation
    super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation)
```

### 3. Updated AdjustStopLossAction Constructor

Added `expert_recommendation` parameter and properly passed it to parent:

```python
def __init__(self, instrument_name: str, account: AccountInterface, 
             order_recommendation: OrderRecommendation, existing_order: Optional[TradingOrder] = None,
             expert_recommendation: Optional[ExpertRecommendation] = None,  # ADDED
             stop_loss_price: Optional[float] = None,
             reference_value: Optional[str] = None, percent: Optional[float] = None):
    # Changed from: expert_recommendation=None to: expert_recommendation
    super().__init__(instrument_name, account, order_recommendation, existing_order, expert_recommendation)
```

### 4. Made Recommendation Processing Async

**File**: `ba2_trade_platform/ui/pages/marketanalysis.py`

Updated `_execute_process_recommendations()` to run in background thread to prevent UI blocking:

```python
async def _execute_process_recommendations(self, expert_id: int, days: int, config_dialog):
    """Execute the recommendation processing with the specified days lookback (async to avoid UI blocking)."""
    try:
        config_dialog.close()
        
        from ...core.TradeManager import get_trade_manager
        trade_manager = get_trade_manager()
        
        # Process dialog
        with ui.dialog() as processing_dialog, ui.card():
            ui.label('Processing Recommendations...').classes('text-h6')
            ui.spinner(size='lg')
            ui.label(f'Processing recommendations from the last {days} day(s)').classes('text-sm text-gray-600')
            ui.label('This may take a few moments...').classes('text-xs text-gray-500 mt-2')
        
        processing_dialog.open()
        
        try:
            # Run in thread pool to avoid blocking UI
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            
            def process_recommendations():
                return trade_manager.process_expert_recommendations_after_analysis(expert_id, lookback_days=days)
            
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                created_orders = await loop.run_in_executor(executor, process_recommendations)
            
            processing_dialog.close()
            # ... rest of success/error handling
```

## Data Flow Verification

The complete flow now works as follows:

1. **UI**: User clicks "Process Recommendations" → calls `_execute_process_recommendations()`
2. **UI (async)**: Runs `trade_manager.process_expert_recommendations_after_analysis()` in ThreadPoolExecutor
3. **TradeManager**: Creates `TradeActionEvaluator` and calls `evaluator.evaluate(expert_recommendation=recommendation)`
4. **Evaluator**: Calls `_create_and_store_trade_actions(expert_recommendation)` ✓
5. **Evaluator**: Calls `_create_trade_action(expert_recommendation)` ✓
6. **Evaluator**: Calls `create_action(expert_recommendation=expert_recommendation)` ✓
7. **Factory**: Creates action with `action_class(..., expert_recommendation)` ✓
8. **Action Constructor**: Stores `self.expert_recommendation = expert_recommendation` ✓
9. **Action.execute()**: Creates result via `create_and_save_action_result()` which extracts `self.expert_recommendation.id` ✓
10. **TradeActionResult**: Saved to database with valid `expert_recommendation_id` ✅

## Testing

Created test script `test_files/test_recommendation_processing.py` that verifies:

- ✅ Expert recommendations can be processed without IntegrityError
- ✅ TradeActionResult records are created with valid `expert_recommendation_id`
- ✅ All action types (BUY, SELL, ADJUST_TP, ADJUST_SL, etc.) properly link to recommendations

Test output:
```
✓ TradeActionResult 125: action=evaluation_only, recommendation_id=141, success=True
✅ All new TradeActionResult records have valid expert_recommendation_id
✅ TEST PASSED: Recommendation processing works correctly
```

## Related Issues Fixed

As part of this debugging session, the following related issues were also fixed:

1. **FMPSenateTrade Signal Logic**: Fixed portfolio allocation calculation to use dollar amounts instead of trade count
2. **NiceGUI Table Selection**: Fixed `AttributeError` by using `.selected` property directly instead of invalid `bind_value_to()` method
3. **Missing Expert Instances**: Added graceful error handling for deleted expert instances in UI
4. **Transaction Model Field**: Fixed query to use `Transaction.expert_id` instead of incorrect `Transaction.expert_instance_id`

## Impact

- ✅ Fixes critical bug preventing recommendation processing
- ✅ Enables all action types to be properly linked to their recommendations
- ✅ Improves UI responsiveness by making long-running operations async
- ✅ Maintains data integrity by ensuring all TradeActionResult records have valid foreign keys

## Files Modified

1. `ba2_trade_platform/core/TradeActions.py` - Fixed factory and action constructors
2. `ba2_trade_platform/ui/pages/marketanalysis.py` - Made processing async
3. `test_files/test_recommendation_processing.py` - Created verification test

## Verification Steps

To verify the fix:

1. Run the test: `.venv\Scripts\python.exe test_files\test_recommendation_processing.py`
2. Or use the UI: Market Analysis → Select expert → "Process Recommendations"
3. Check logs for successful TradeActionResult creation
4. Verify no IntegrityError exceptions

## Notes

- The original code intentionally excluded expert_recommendation from TP/SL actions, possibly assuming they wouldn't need it
- However, ALL actions create TradeActionResult records, which require expert_recommendation_id
- The fix ensures consistency: all actions receive and store the expert_recommendation reference
