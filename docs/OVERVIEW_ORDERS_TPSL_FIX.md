# Overview Orders & TP/SL Attribution Fix

**Date**: October 6, 2025

## Issues Fixed

### 1. Failed Orders Alert "View Orders" Button Not Working

**Problem**: The "View Orders" button in the failed orders alert used `ui.navigate.to('/#account')`, which doesn't work because there's no route defined for `#account`.

**Root Cause**: Incorrect navigation method - should use tab switching instead of URL navigation.

**Solution**: Changed to use `tabs.set_value('account')` to properly switch to the Account Overview tab.

**File**: `ba2_trade_platform/ui/pages/overview.py`

**Change**:
```python
# Before:
ui.button('View Orders', on_click=lambda: ui.navigate.to('/#account')).props('outline color=red')

# After:
def switch_to_account_tab():
    if self.tabs_ref:
        self.tabs_ref.set_value('account')

ui.button('View Orders', on_click=switch_to_account_tab).props('outline color=red')
```

---

### 2. TP/SL Orders Missing Expert Attribution

**Problem**: Take Profit and Stop Loss orders were created with `open_type=MANUAL` and missing `expert_recommendation_id`, making it impossible to track which expert generated them.

**Root Cause**: Multiple issues:
1. `AlpacaAccount._create_tp_order_object()` didn't copy `expert_recommendation_id` or set `open_type`
2. `TradeActions.create_order_record()` didn't handle TP/SL orders properly (no fallback to copy from existing_order)

**Solution**: 

#### A. TP Orders (AlpacaAccount)

**File**: `ba2_trade_platform/modules/accounts/AlpacaAccount.py`

**Changes**:
- Added `expert_recommendation_id=original_order.expert_recommendation_id` to copy expert attribution
- Added `open_type=OrderOpenType.AUTOMATIC` to mark as automatically created
- Imported `OrderOpenType` enum

```python
def _create_tp_order_object(self, original_order: TradingOrder, tp_price: float) -> TradingOrder:
    from ...core.types import OrderType as CoreOrderType, OrderOpenType
    
    # ... existing code ...
    
    tp_order = TradingOrder(
        # ... existing fields ...
        expert_recommendation_id=original_order.expert_recommendation_id,  # ✅ NEW
        open_type=OrderOpenType.AUTOMATIC,  # ✅ NEW
        comment=f"TP for order {original_order.id}",
        created_at=datetime.now(timezone.utc)
    )
    
    return tp_order
```

#### B. SL Orders (TradeActions)

**File**: `ba2_trade_platform/core/TradeActions.py`

**Changes**:
Enhanced `create_order_record()` method to:
1. Copy `expert_recommendation_id` from `existing_order` if no `expert_recommendation` is provided (for TP/SL actions)
2. Automatically set `open_type=AUTOMATIC` for orders with `linked_order_id` (TP/SL orders)
3. Set `open_type=AUTOMATIC` for orders with `expert_recommendation_id`
4. Default to `open_type=MANUAL` only for truly manual orders

```python
def create_order_record(self, side: str, quantity: float, order_type: str = "market", 
                      limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                      linked_order_id: Optional[int] = None) -> Optional[TradingOrder]:
    # ... existing code ...
    
    # First try to get expert recommendation from self.expert_recommendation (for BUY/SELL/CLOSE actions)
    if self.expert_recommendation:
        expert_instance_id = self.expert_recommendation.instance_id
        expert_recommendation_id = self.expert_recommendation.id
        # ... add to comment ...
    # ✅ NEW: For TP/SL orders, copy from existing_order if no expert_recommendation
    elif self.existing_order and self.existing_order.expert_recommendation_id:
        expert_recommendation_id = self.existing_order.expert_recommendation_id
        # Get expert instance ID from the recommendation
        from .db import get_instance
        from .models import ExpertRecommendation
        expert_rec = get_instance(ExpertRecommendation, expert_recommendation_id)
        if expert_rec:
            expert_instance_id = expert_rec.instance_id
            # ... add to comment ...
    
    # ✅ NEW: Determine open_type intelligently
    from .types import OrderOpenType
    if linked_order_id is not None:
        # This is a TP/SL order (has a linked parent order)
        open_type = OrderOpenType.AUTOMATIC
    elif expert_recommendation_id is not None:
        # Order created from expert recommendation
        open_type = OrderOpenType.AUTOMATIC
    else:
        # Manual order
        open_type = OrderOpenType.MANUAL
    
    order = TradingOrder(
        # ... existing fields ...
        expert_recommendation_id=expert_recommendation_id,
        open_type=open_type,  # ✅ NEW
        # ... rest of fields ...
    )
```

---

## Impact

### Before Fix:
- ❌ "View Orders" button in failed orders alert didn't work
- ❌ TP orders created with `open_type=MANUAL` and no `expert_recommendation_id`
- ❌ SL orders created with `open_type=MANUAL` and no `expert_recommendation_id`
- ❌ Couldn't track which expert created TP/SL orders
- ❌ TP/SL orders looked like manual orders in the UI

### After Fix:
- ✅ "View Orders" button properly navigates to Account Overview tab
- ✅ TP orders have `open_type=AUTOMATIC` and proper `expert_recommendation_id`
- ✅ SL orders have `open_type=AUTOMATIC` and proper `expert_recommendation_id`
- ✅ Full traceability from expert → recommendation → main order → TP/SL orders
- ✅ Correct categorization in UI (Automatic vs Manual)
- ✅ Expert performance tracking includes TP/SL order results

---

## Technical Details

### Expert Attribution Chain

The fix ensures the following attribution chain is maintained:

```
ExpertInstance (id: 1)
  ↓
ExpertRecommendation (id: 5, instance_id: 1)
  ↓
Main Order (id: 10, expert_recommendation_id: 5, open_type: AUTOMATIC)
  ↓
TP Order (id: 11, expert_recommendation_id: 5, open_type: AUTOMATIC, depends_on_order: 10)
  ↓
SL Order (id: 12, expert_recommendation_id: 5, open_type: AUTOMATIC, depends_on_order: 10)
```

### Order Comment Format

All orders now include expert attribution in comments:
- Main order: `[ACC:1/TR:1/REC:5]`
- TP order: `[ACC:1/TR:1/REC:5]` + "TP for order 10"
- SL order: `[ACC:1/TR:1/REC:5]` (via create_order_record)

Where:
- `ACC` = Account ID
- `TR` = Trading expert instance ID
- `REC` = Expert recommendation ID

---

## Testing Recommendations

1. **Navigation Test**:
   - Create an ERROR order
   - Verify failed orders alert appears on Overview tab
   - Click "View Orders" button
   - Confirm navigation to Account Overview tab

2. **TP/SL Attribution Test**:
   - Run an expert that creates buy/sell recommendations
   - Set TP and SL values on the order
   - Submit the order
   - Verify TP/SL orders in database have:
     - `expert_recommendation_id` matching parent order
     - `open_type = 'automatic'`
   - Check Profit Per Expert chart includes TP/SL order results

3. **Expert Performance Tracking**:
   - Complete a full trade cycle (entry → TP/SL exit)
   - Verify expert profit calculation includes TP/SL order profits
   - Confirm expert performance metrics are accurate

---

## Related Files

- `ba2_trade_platform/ui/pages/overview.py` - Overview tab with failed orders alert
- `ba2_trade_platform/modules/accounts/AlpacaAccount.py` - TP order creation
- `ba2_trade_platform/core/TradeActions.py` - SL order creation via actions
- `ba2_trade_platform/core/models.py` - TradingOrder model
- `ba2_trade_platform/core/types.py` - OrderOpenType enum
