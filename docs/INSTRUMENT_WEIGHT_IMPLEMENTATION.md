# Instrument Weight Support in Risk Management - October 2, 2025

## Overview

Implemented instrument weight support in the `TradeRiskManagement` system to allow prioritization and increased allocation for specific instruments based on user-configured weights.

## Feature Description

### What is Instrument Weight?

Instrument weight is a configurable parameter (stored in expert settings) that allows users to increase or decrease the allocation for specific instruments. The weight value modifies the calculated quantity using the formula:

```
final_quantity = base_quantity * (1 + weight/100)
```

### Weight Values

- **Default: 100** - No modification (multiplier of 2.0)
- **Weight < 100** - Reduces allocation (e.g., weight=50 gives multiplier of 1.5)
- **Weight > 100** - Increases allocation (e.g., weight=150 gives multiplier of 2.5)
- **Weight = 0** - Minimum allocation (multiplier of 1.0)

### Examples

| Weight | Formula | Multiplier | Base Qty | Final Qty |
|--------|---------|------------|----------|-----------|
| 0      | 10 * (1 + 0/100) | 1.0x | 10 | 10 |
| 50     | 10 * (1 + 50/100) | 1.5x | 10 | 15 |
| 100    | 10 * (1 + 100/100) | 2.0x | 10 | 20 |
| 150    | 10 * (1 + 150/100) | 2.5x | 10 | 25 |
| 200    | 10 * (1 + 200/100) | 3.0x | 10 | 30 |

## Implementation Details

### Data Storage

Instrument weights are stored in the expert's `enabled_instruments` setting as a dictionary:

```json
{
  "MSFT": {"enabled": true, "weight": 100.0},
  "AAPL": {"enabled": true, "weight": 150.0},
  "GOOGL": {"enabled": true, "weight": 75.0}
}
```

### Code Changes

#### 1. TradeRiskManagement.py

**Modified Method Signature:**
```python
def _calculate_order_quantities(
    self,
    prioritized_orders: List[Tuple[TradingOrder, ExpertRecommendation]],
    total_virtual_balance: float,
    max_equity_per_instrument: float,
    existing_allocations: Dict[str, float],
    account: AccountInterface,
    expert: 'MarketExpertInterface'  # NEW PARAMETER
) -> List[TradingOrder]:
```

**Added Imports:**
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .MarketExpertInterface import MarketExpertInterface
```

**Weight Application Logic:**
```python
# Apply instrument weight (formula: result * (weight/100))
if quantity > 0 and symbol in instrument_configs:
    instrument_weight = instrument_configs[symbol].get('weight', 100.0)
    if instrument_weight != 100.0:  # Only log if weight is non-default
        original_quantity = quantity
        weighted_quantity = quantity * (instrument_weight / 100.0)
        quantity = max(0, int(weighted_quantity))
        
        # Check if we can afford the weighted quantity
        weighted_cost = quantity * current_price
        if weighted_cost > remaining_balance or weighted_cost > available_for_instrument:
            # Revert to original quantity if weighted amount exceeds limits
            quantity = original_quantity
            self.logger.debug(f"  Weight {instrument_weight}% would exceed limits, keeping original quantity {quantity}")
        else:
            self.logger.info(f"  Applied instrument weight {instrument_weight}%: {original_quantity} -> {quantity} shares")
```

**Updated Method Call:**
```python
# Step 8: Calculate quantities for prioritized orders
updated_orders = self._calculate_order_quantities(
    prioritized_orders,
    total_virtual_balance,
    max_equity_per_instrument,
    existing_allocations,
    account,
    expert  # Pass expert instance
)
```

### Key Features

1. **Weight Retrieval**: Gets instrument configurations from expert settings using `expert._get_enabled_instruments_config()`

2. **Weight Application**: Applies the weight formula to the base calculated quantity

3. **Safety Checks**: 
   - Verifies weighted quantity doesn't exceed remaining balance
   - Verifies weighted quantity doesn't exceed per-instrument allocation limit
   - Reverts to original quantity if weighted amount exceeds limits

4. **Logging**:
   - Logs weight application only when weight differs from default (100.0)
   - Logs original and final quantities for transparency
   - Logs when weight would exceed limits and original quantity is kept

## Use Cases

### 1. High-Conviction Instruments
Set weight to 150-200 for instruments you have high confidence in, allocating 2.5x-3x the base amount.

### 2. Experimental/Low-Confidence Instruments
Set weight to 25-50 for instruments you want exposure to but with limited capital commitment (1.25x-1.5x).

### 3. Balanced Portfolio
Keep all weights at 100 for equal treatment based purely on profit potential and risk limits (2.0x).

### 4. Sector Weighting
Increase weights for instruments in favored sectors, decrease for less favored sectors.

## Interaction with Risk Management

The instrument weight is applied **after** all standard risk management calculations:

1. **Profit-based prioritization** - Orders sorted by ROI
2. **Balance checks** - Verify sufficient funds
3. **Per-instrument limits** - Apply max_virtual_equity_per_instrument_percent
4. **Diversification factor** - Apply 0.7x factor for multiple instruments
5. **Base quantity calculation** - Calculate initial quantity
6. **🆕 Weight application** - Apply instrument-specific weight multiplier
7. **Limit validation** - Ensure weighted quantity fits within constraints

### Order of Operations Example

For AAPL with weight=150 and $10,000 max per instrument:

```
1. Base calculation: 50 shares at $200 = $10,000 ✓
2. Apply diversification (0.7x): 35 shares
3. Apply weight (2.5x): 35 * 2.5 = 87.5 → 87 shares
4. Check cost: 87 * $200 = $17,400 ✗ (exceeds $10,000 limit)
5. Result: Keep original 35 shares (weight rejected due to limit)
```

## Configuration

### Setting Instrument Weights in UI

Instrument weights are configured in the Expert Settings page:

1. Navigate to **Settings → Experts**
2. Select an expert instance
3. Click "Configure Instruments"
4. For each enabled instrument, adjust the weight slider or enter a value
5. Save the configuration

### Default Behavior

- If no weight is specified for an instrument, default is **100** (2.0x multiplier)
- If instrument not in enabled_instruments config, weight is **100**
- Weight cannot be negative (UI prevents this)

## Testing Recommendations

### Test Case 1: Standard Weight (100)
- **Setup**: Configure instrument with weight=100
- **Expected**: Quantity doubled (base_qty * 2.0)
- **Verify**: Log shows no weight message (default not logged)

### Test Case 2: High Weight (200)
- **Setup**: Configure instrument with weight=200
- **Expected**: Quantity tripled (base_qty * 3.0) if within limits
- **Verify**: Log shows "Applied instrument weight 200%: X -> Y shares"

### Test Case 3: Low Weight (50)
- **Setup**: Configure instrument with weight=50
- **Expected**: Quantity increased by 50% (base_qty * 1.5)
- **Verify**: Log shows weight application

### Test Case 4: Weight Exceeds Limits
- **Setup**: High weight on expensive instrument near limit
- **Expected**: Weight rejected, original quantity kept
- **Verify**: Log shows "Weight X% would exceed limits, keeping original quantity Y"

### Test Case 5: Multiple Weighted Instruments
- **Setup**: Portfolio with varied weights (50, 100, 150, 200)
- **Expected**: Allocations reflect relative weights while respecting balance
- **Verify**: Higher-weighted instruments get larger allocations

## Benefits

1. **Flexibility**: Users can fine-tune allocations beyond profit-based sorting
2. **Risk Control**: Maintain conservative positions in uncertain instruments
3. **Conviction Trading**: Amplify positions in high-conviction opportunities
4. **Portfolio Customization**: Create personalized allocation strategies
5. **Safety**: Weight application respects all existing risk limits

## Backward Compatibility

- Existing configurations without explicit weights default to 100
- No database schema changes required (uses existing settings)
- No breaking changes to existing functionality
- Weight=100 produces same behavior as before (2.0x base quantity)

## Future Enhancements

Potential future improvements:

1. **Dynamic Weights**: Adjust weights based on market conditions
2. **Weight Ranges**: Set min/max bounds for weight values in UI
3. **Weight Presets**: Save and load weight profiles
4. **Group Weights**: Apply weights to instrument categories/sectors
5. **Performance-Based Weights**: Auto-adjust based on historical performance

## Related Documentation

- [Risk Management Implementation](riskmanagement.md)
- [Expert Settings Configuration](EXPERT_SETTINGS.md)
- [Instrument Selector Component](../ba2_trade_platform/ui/components/InstrumentSelector.py)
- [Market Expert Interface](../ba2_trade_platform/core/MarketExpertInterface.py)

## Files Modified

- `ba2_trade_platform/core/TradeRiskManagement.py`
  - Added `expert` parameter to `_calculate_order_quantities()`
  - Added instrument weight retrieval and application logic
  - Added TYPE_CHECKING import for type hints

## Logging Examples

### Weight Applied Successfully
```
INFO - Applied instrument weight 150%: 10 -> 25 shares
```

### Weight Exceeds Limits
```
DEBUG - Weight 200% would exceed limits, keeping original quantity 10
```

### Weight Configuration Retrieved
```
DEBUG - Retrieved instrument weight configurations: 15 instruments
```

## Summary

The instrument weight feature provides users with granular control over position sizing while maintaining all existing risk management safeguards. It integrates seamlessly with the existing risk management pipeline and requires no changes to database schema or user workflows beyond setting weight values in the instrument configuration UI.
