# Performance Page Expert Shortname Display

**Date**: 2025-10-08  
**Status**: ✅ Complete  
**Impact**: Performance analytics now shows expert shortnames (user_description) instead of class names

## Problem

The Performance tab was displaying expert class names (e.g., "TradingAgents") in the dropdown and charts, which is not user-friendly when you have multiple instances of the same expert class.

**Example**:
- You might have 3 TradingAgents instances with different configurations
- All showed as "TradingAgents" making them indistinguishable
- Users set `user_description` field to give meaningful names like "TA - Aggressive", "TA - Conservative"

## Solution

Updated the Performance page to use the `user_description` field (shortname) when available, with fallback to the expert class name.

### Changes Made

Updated 3 locations in `ba2_trade_platform/ui/pages/performance.py`:

#### 1. Expert Filter Dropdown (lines ~370-380)

**Before**:
```python
expert_options = {expert.id: expert.expert for expert in experts}
```

**After**:
```python
# Use user_description (shortname) if available, otherwise use expert class name
expert_options = {
    expert.id: expert.user_description if expert.user_description else expert.expert 
    for expert in experts
}
```

#### 2. Transaction Metrics Calculation (lines ~73-77)

**Before**:
```python
expert = session.get(ExpertInstance, expert_id)
expert_name = expert.expert if expert else f"Expert {expert_id}"
```

**After**:
```python
expert = session.get(ExpertInstance, expert_id)
if expert:
    expert_name = expert.user_description if expert.user_description else expert.expert
else:
    expert_name = f"Expert {expert_id}"
```

#### 3. Monthly Metrics Calculation (lines ~143-145)

**Before**:
```python
expert = session.get(ExpertInstance, txn.expert_id)
expert_name = expert.expert if expert else f"Expert {txn.expert_id}"
```

**After**:
```python
expert = session.get(ExpertInstance, txn.expert_id)
if expert:
    expert_name = expert.user_description if expert.user_description else expert.expert
else:
    expert_name = f"Expert {txn.expert_id}"
```

## Impact

Now all Performance page components show expert shortnames:

1. ✅ **Expert Filter Dropdown**: Shows "TA - Aggressive" instead of "TradingAgents"
2. ✅ **Performance Charts**: X-axis labels use shortnames
3. ✅ **Summary Metrics**: Cards show shortnames
4. ✅ **Detailed Table**: Expert column shows shortnames
5. ✅ **Monthly Trends**: Legend shows shortnames

## Fallback Behavior

The implementation gracefully handles missing data:

- **If `user_description` is set**: Uses shortname (e.g., "TA - Aggressive")
- **If `user_description` is NULL/empty**: Falls back to class name (e.g., "TradingAgents")
- **If expert not found**: Shows "Expert {id}"

## ExpertInstance Model Reference

```python
class ExpertInstance(SQLModel, table=True):
    id: int | None
    account_id: int
    expert: str                           # Class name (e.g., "TradingAgents")
    enabled: bool
    user_description: str | None          # ✅ Shortname (e.g., "TA - Aggressive")
    virtual_equity_pct: float
    enter_market_ruleset_id: int | None
    open_positions_ruleset_id: int | None
```

## Testing

After the fix:
1. ✅ Open Performance tab
2. ✅ Check expert dropdown shows shortnames
3. ✅ Select expert and verify charts use shortnames
4. ✅ Verify table displays shortnames correctly
5. ✅ Test with experts that don't have user_description (should show class name)

## Related Files

- **Modified**: `ba2_trade_platform/ui/pages/performance.py`
- **Model**: `ba2_trade_platform/core/models.py` (ExpertInstance)

## User Experience Improvement

**Before**:
```
Expert Dropdown:
- TradingAgents
- TradingAgents
- TradingAgents
```
❌ Can't tell them apart!

**After**:
```
Expert Dropdown:
- TA - Aggressive Growth
- TA - Conservative Value
- TA - Balanced Portfolio
```
✅ Clear and meaningful names!
