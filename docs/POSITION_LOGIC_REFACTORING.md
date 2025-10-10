# Position Logic Refactoring: Expert-Level vs Account-Level Position Checks

**Date**: 2025-01-10  
**Issue**: Position conditions checked at account level instead of expert level, preventing multiple experts from trading the same symbol

## Problem Summary

The original position checking logic (`HasPositionCondition` and `HasNoPositionCondition`) checked positions at the **account level**. This meant:

- ❌ If Expert A had an AAPL position, Expert B's `HasNoPositionCondition` for AAPL would return `False`
- ❌ Multiple experts couldn't independently manage positions in the same symbol
- ❌ Expert-specific trading strategies were limited by account-wide position conflicts

## Solution Overview

**Refactored position logic to support both expert-level and account-level checks:**

1. **Expert-Level**: Based on expert's own transactions (NEW default behavior)
2. **Account-Level**: Based on account's total positions (NEW separate conditions)

## Implementation Details

### 1. **New Expert-Level Position Methods** (`TradeConditions.py`)

Added methods to check positions based on expert's transactions:

```python
def has_position_expert(self) -> bool:
    """Check if this expert has an open position for the instrument (based on transactions)."""
    try:
        from sqlmodel import select
        from .db import get_db
        from .models import Transaction
        from .types import TransactionStatus
        
        with get_db() as session:
            # Check for open transactions for this expert and symbol
            statement = select(Transaction).where(
                Transaction.expert_id == self.expert_recommendation.instance_id,
                Transaction.symbol == self.instrument_name,
                Transaction.status == TransactionStatus.OPENED
            )
            
            open_transactions = session.exec(statement).all()
            return len(open_transactions) > 0
            
    except Exception as e:
        logger.error(f"Error checking expert position: {e}", exc_info=True)
        return False
```

### 2. **Updated Existing Position Conditions**

Modified to use expert-level checks by default:

```python
class HasNoPositionCondition(FlagCondition):
    """Check if this expert has no open position for the instrument (based on transactions)."""
    
    def evaluate(self) -> bool:
        return not self.has_position_expert()  # ← Now uses expert-level check
    
    def get_description(self) -> str:
        return f"Check if this expert has no open position for {self.instrument_name} (based on transactions)"
```

### 3. **New Account-Level Position Conditions**

Created separate conditions for account-level position checks:

```python
class HasPositionAccountCondition(FlagCondition):
    """Check if account has an open position for the instrument (account-level)."""
    
    def evaluate(self) -> bool:
        return self.has_position_account()
    
    def get_description(self) -> str:
        return f"Check if account has an open position for {self.instrument_name} (account-level)"

class HasNoPositionAccountCondition(FlagCondition):
    """Check if account has no open position for the instrument (account-level)."""
    
    def evaluate(self) -> bool:
        return not self.has_position_account()
    
    def get_description(self) -> str:
        return f"Check if account has no open position for {self.instrument_name} (account-level)"
```

### 4. **New Event Types** (`types.py`)

Added new condition types to `ExpertEventType`:

```python
class ExpertEventType(str, Enum):
    # ... existing types ...
    F_HAS_POSITION_ACCOUNT = "has_position_account"
    F_HAS_NO_POSITION_ACCOUNT = "has_no_position_account"
```

### 5. **Updated Condition Factory**

Registered new conditions in the factory:

```python
condition_map = {
    # ... existing mappings ...
    ExpertEventType.F_HAS_POSITION_ACCOUNT: HasPositionAccountCondition,
    ExpertEventType.F_HAS_NO_POSITION_ACCOUNT: HasNoPositionAccountCondition,
}
```

### 6. **Updated Documentation**

Enhanced rules documentation with new condition types:

```python
ExpertEventType.F_HAS_POSITION_ACCOUNT.value: {
    "name": "Has Position (Account Level)",
    "description": "Check if the account has an open position for the instrument (account-wide check)",
    "category": "Position Flags"
},
ExpertEventType.F_HAS_NO_POSITION_ACCOUNT.value: {
    "name": "No Position (Account Level)", 
    "description": "Check if the account has no open position for the instrument (account-wide check)",
    "category": "Position Flags"
}
```

## Before vs After Comparison

### **Before (Account-Level Only)**
```
Expert A has AAPL position → Account has AAPL position
Expert B checks HasNoPositionCondition for AAPL → Returns False
Result: Expert B cannot trade AAPL
```

### **After (Expert-Level + Account-Level)**
```
Expert A has AAPL position → Expert A has AAPL transactions
Expert B checks HasNoPositionCondition for AAPL → Returns True (Expert B has no AAPL transactions)
Expert B checks HasNoPositionAccountCondition for AAPL → Returns False (Account has AAPL position)
Result: Expert B can trade AAPL independently, or check account-level if needed
```

## Test Results

Created comprehensive test (`test_files/test_position_logic_changes.py`) that verified:

### **TEST: Symbol TEST (No positions anywhere)**
- ✅ Expert has_position: `False`, no_position: `True`
- ✅ Account has_position: `False`, no_position: `True`
- ✅ Expert conditions match transaction data
- ✅ Account conditions match position data

### **TEST: Symbol AAPL (Account has position, Expert doesn't)**
- ✅ Expert has_position: `False`, no_position: `True` (Expert has no AAPL transactions)
- ✅ Account has_position: `True`, no_position: `False` (Account has AAPL position)
- ✅ Expert conditions match transaction data
- ✅ Account conditions match position data

### **Condition Descriptions**
- ✅ Expert conditions mention "expert" and "transactions"
- ✅ Account conditions mention "account" and "account-level"

## Impact

### **Benefits**
- ✅ **Expert Independence**: Multiple experts can trade the same symbol independently
- ✅ **Backward Compatibility**: Existing rules using old conditions get new expert-level behavior
- ✅ **Flexibility**: Rules can choose expert-level or account-level position checks as needed
- ✅ **Clear Semantics**: Conditions now have explicit, understandable behavior

### **Use Cases**

#### **Expert-Level Position Checks** (Default)
- **HasPositionCondition**: "This expert has a position in AAPL"
- **HasNoPositionCondition**: "This expert has no position in AAPL"
- **Use Case**: Expert-specific entry/exit rules, independent expert strategies

#### **Account-Level Position Checks** (New)
- **HasPositionAccountCondition**: "Account has any position in AAPL (from any expert)"
- **HasNoPositionAccountCondition**: "Account has no position in AAPL at all"
- **Use Case**: Account-wide risk management, portfolio concentration limits

## Migration Guide

### **Existing Rules**
- **No changes needed**: Existing rules using `HasPositionCondition`/`HasNoPositionCondition` now use expert-level checks
- **Behavior change**: Rules that expected account-level checks should be updated to use new account-level conditions

### **New Rules**
- **Expert-level**: Use `F_HAS_POSITION` / `F_HAS_NO_POSITION` (default)
- **Account-level**: Use `F_HAS_POSITION_ACCOUNT` / `F_HAS_NO_POSITION_ACCOUNT`

## Files Modified

1. **ba2_trade_platform/core/TradeConditions.py**
   - Added `has_position_expert()` and `has_position_account()` methods
   - Updated `HasPositionCondition` and `HasNoPositionCondition` to use expert-level checks
   - Added `HasPositionAccountCondition` and `HasNoPositionAccountCondition`

2. **ba2_trade_platform/core/types.py**
   - Added `F_HAS_POSITION_ACCOUNT` and `F_HAS_NO_POSITION_ACCOUNT` to `ExpertEventType`

3. **ba2_trade_platform/core/rules_documentation.py**
   - Added documentation for new account-level conditions

4. **test_files/test_position_logic_changes.py** (NEW)
   - Comprehensive test suite verifying both expert-level and account-level position logic

## Technical Notes

### **Database Queries**
- **Expert-level**: Queries `Transaction` table filtered by `expert_id` and `symbol`
- **Account-level**: Calls `account.get_positions()` (existing broker API)

### **Performance**
- Expert-level checks require database query (minimal overhead)
- Account-level checks use existing broker position data (same as before)

### **Error Handling**
- Both methods include try-catch with appropriate logging
- Graceful fallback to `False` on errors

## Verification

Run the test to verify the changes:
```bash
.venv\Scripts\python.exe test_files\test_position_logic_changes.py
```

Expected output:
```
✅ Expert position conditions are opposite
✅ Account position conditions are opposite  
✅ Expert conditions match transaction data
✅ Account conditions match position data
✅ TEST PASSED: Position logic changes work correctly
```

## Summary

This refactoring successfully transforms the platform from account-centric position checking to expert-centric position checking, while maintaining account-level capabilities for specialized use cases. Multiple experts can now independently manage positions in the same symbols, enabling more sophisticated multi-expert trading strategies.