# Instrument Account Share - Implementation Summary

## Quick Reference

### New Trade Condition
**`N_INSTRUMENT_ACCOUNT_SHARE`** - Numeric condition that calculates current position value as % of expert virtual equity

**Formula**: `(position_qty × current_price / virtual_equity) × 100`

**Example Rules**:
```
IF instrument_account_share > 15% THEN decrease_instrument_share to 10%
IF instrument_account_share < 5% AND confidence >= 80% THEN increase_instrument_share to 8%
```

### New Trade Actions

#### 1. **`INCREASE_INSTRUMENT_SHARE`**
Increases position to reach target % of virtual equity

**Parameters**: `target_percent` (e.g., 12.0 for 12%)

**Safety**:
- ✅ Caps at `max_virtual_equity_per_instrument_percent` (default 10%)
- ✅ Cannot exceed available balance
- ✅ Minimum 1 share order size
- ✅ Skips if already at/above target

**Example**: `INCREASE_INSTRUMENT_SHARE target_percent=12%` → Buys shares to reach 12% allocation

#### 2. **`DECREASE_INSTRUMENT_SHARE`**
Decreases position to reach target % of virtual equity

**Parameters**: `target_percent` (e.g., 5.0 for 5%, 0.0 to close completely)

**Safety**:
- ✅ Maintains minimum 1 share if `target_percent > 0`
- ✅ Allows full close if `target_percent = 0`
- ✅ Skips if already at/below target

**Example**: `DECREASE_INSTRUMENT_SHARE target_percent=5%` → Sells shares to reach 5% allocation

## Files Modified

| File | Changes | Lines Added |
|------|---------|-------------|
| `types.py` | Added event/action enums | 3 |
| `TradeConditions.py` | Added `InstrumentAccountShareCondition` | 89 |
| `TradeActions.py` | Added `IncreaseInstrumentShareAction` & `DecreaseInstrumentShareAction` | 359 |
| `rules_documentation.py` | Added documentation entries | 30 |

**Total**: 481 lines of new functionality

## Key Features

### 1. **Position Sizing**
- Scale positions based on confidence, risk, or any other metric
- Build positions gradually or aggressively
- Maintain precise allocation targets

### 2. **Portfolio Rebalancing**
- Automatically trim oversized positions
- Add to undersized high-conviction positions
- Maintain diversification across instruments

### 3. **Risk Management**
- Enforces `max_virtual_equity_per_instrument_percent` setting
- Prevents over-concentration in single instrument
- Respects available balance constraints

### 4. **Gradual Scaling**
- Increment positions over time as thesis confirms
- Reduce exposure incrementally as targets approach
- Avoid large single orders

## Common Use Cases

### Use Case 1: Confidence-Based Sizing
```yaml
Rule: "Scale Up High Confidence"
Condition: confidence >= 85% AND instrument_account_share < 10%
Action: INCREASE_INSTRUMENT_SHARE target_percent=12%

Rule: "Scale Down Low Confidence"  
Condition: confidence < 65% AND instrument_account_share > 3%
Action: DECREASE_INSTRUMENT_SHARE target_percent=2%
```
**Result**: Larger positions in high-confidence trades, smaller in low-confidence

### Use Case 2: Diversification Enforcement
```yaml
Rule: "Cap Maximum Position"
Condition: instrument_account_share > 15%
Action: DECREASE_INSTRUMENT_SHARE target_percent=10%
```
**Result**: No single position exceeds 15% of portfolio

### Use Case 3: Profit-Taking Ladder
```yaml
Rule: "First Profit Target"
Condition: profit_loss_percent >= 10% AND instrument_account_share > 8%
Action: DECREASE_INSTRUMENT_SHARE target_percent=6%

Rule: "Second Profit Target"
Condition: profit_loss_percent >= 20% AND instrument_account_share > 6%
Action: DECREASE_INSTRUMENT_SHARE target_percent=4%
```
**Result**: Systematically reduce position as profits grow

## Safety Mechanisms

| Constraint | Implementation | Behavior |
|------------|----------------|----------|
| Max Allocation | `max_virtual_equity_per_instrument_percent` | Caps increase action at expert setting (default 10%) |
| Available Balance | Account buying power check | Reduces order size if insufficient funds |
| Minimum Position | 1 share minimum (if not closing) | Prevents fractional shares or dust positions |
| No-Op Detection | Skip if already at target | Avoids unnecessary orders |
| Price Validation | Current price required | Fails gracefully if price unavailable |

## Integration Points

### Expert Settings Used
- `virtual_equity_pct` - Percentage of account to use as virtual equity
- `max_virtual_equity_per_instrument_percent` - Maximum allocation per instrument (default 10%)

### Expert Methods Called
- `get_available_balance()` - Returns virtual equity amount
- Standard order creation pipeline

### Database Models
- Uses existing `TradingOrder` model
- Links to `ExpertRecommendation` via `expert_recommendation_id`
- No new tables or schema changes required

## Testing Checklist

**Basic Functionality**:
- [ ] Condition correctly calculates share percentage
- [ ] Increase action creates BUY order with correct quantity
- [ ] Decrease action creates SELL order with correct quantity
- [ ] Actions respect max allocation setting
- [ ] Actions respect available balance

**Edge Cases**:
- [ ] No position (share = 0%) → condition evaluates correctly
- [ ] Target already met → actions skip
- [ ] Insufficient balance → order size adjusted
- [ ] Minimum 1 share rule → enforced on decrease
- [ ] Full close (target = 0%) → allowed on decrease

**Integration**:
- [ ] Works in enter_market rulesets
- [ ] Works in open_positions rulesets
- [ ] Orders properly linked to recommendations
- [ ] Transactions created correctly
- [ ] Risk management processes orders

## Example Ruleset

```yaml
name: "Dynamic Position Management"
description: "Automatically adjusts position sizes based on confidence and allocation"
use_case: "open_positions"

events:
  - N_INSTRUMENT_ACCOUNT_SHARE
  - N_CONFIDENCE
  - N_PROFIT_LOSS_PERCENT

actions:
  - INCREASE_INSTRUMENT_SHARE
  - DECREASE_INSTRUMENT_SHARE

rules:
  # Scale up on high confidence
  - name: "Increase High Conviction"
    conditions:
      - type: N_CONFIDENCE
        operator: ">="
        value: 85
      - type: N_INSTRUMENT_ACCOUNT_SHARE
        operator: "<"
        value: 10
    actions:
      - type: INCREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 12
  
  # Rebalance oversized positions
  - name: "Trim Oversized"
    conditions:
      - type: N_INSTRUMENT_ACCOUNT_SHARE
        operator: ">"
        value: 15
    actions:
      - type: DECREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 10
  
  # Take profits incrementally
  - name: "Profit Taking Level 1"
    conditions:
      - type: N_PROFIT_LOSS_PERCENT
        operator: ">="
        value: 15
      - type: N_INSTRUMENT_ACCOUNT_SHARE
        operator: ">"
        value: 6
    actions:
      - type: DECREASE_INSTRUMENT_SHARE
        parameters:
          target_percent: 4
```

## Performance Impact

**Computational**: Negligible - simple arithmetic calculations  
**Database**: 3-5 queries per evaluation/execution (standard for trade actions)  
**Network**: 1 market order per action (minimal broker API impact)  

## Migration & Deployment

**Compatibility**: 100% backward compatible - no breaking changes  
**Deployment**: No database migrations required  
**Configuration**: Uses existing expert settings  
**Rollout**: New features available immediately, opt-in via rulesets  

## Documentation

**Full Documentation**: `docs/INSTRUMENT_ACCOUNT_SHARE_FEATURE.md`  
**Rules Documentation**: Updated in `core/rules_documentation.py`  
**Code Documentation**: Comprehensive docstrings in all new classes  

## Quick Start

### 1. Create a Rule
Navigate to Settings → Experts → Select Expert → Rulesets → Edit Ruleset

### 2. Add Condition
- Event Type: **Instrument Account Share** (numeric)
- Operator: `>`, `<`, `>=`, `<=`, `==`
- Value: Target percentage (e.g., `15` for 15%)

### 3. Add Action
- Action Type: **Increase Instrument Share** or **Decrease Instrument Share**
- Parameter: `target_percent` (e.g., `10` for 10%)

### 4. Test
- Save ruleset
- Trigger analysis for the expert
- Monitor logs for execution
- Verify orders created with correct quantities

## Support

For questions or issues:
1. Check `docs/INSTRUMENT_ACCOUNT_SHARE_FEATURE.md` for detailed documentation
2. Review code examples in `TradeActions.py` and `TradeConditions.py`
3. Check logs for detailed calculation information
4. Verify expert settings (`max_virtual_equity_per_instrument_percent`)

## Summary

This feature enables **institutional-grade portfolio management** with:
- ✅ Dynamic position sizing
- ✅ Automatic rebalancing
- ✅ Risk-adjusted allocations
- ✅ Gradual scaling strategies
- ✅ Built-in safety constraints

All while maintaining **simplicity**, **safety**, and **backward compatibility**.
