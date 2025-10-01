# Trade Risk Management Implementation

## Overview
The `TradeRiskManagement` class implements a comprehensive risk management system for automated trading that prioritizes recommendations based on expected profit while maintaining portfolio diversification and position sizing constraints.

## Core Components

### 1. TradeRiskManagement Class
- **Location**: `ba2_trade_platform/core/TradeRiskManagement.py`
- **Purpose**: Review and prioritize pending orders with quantity=0, calculate appropriate position sizes
- **Main Method**: `review_and_prioritize_pending_orders(expert_instance_id: int)`

### 2. Key Settings
- **max_virtual_equity_per_instrument_percent**: Default 10%, limits allocation per instrument
- **allow_automated_trade_opening**: Must be enabled for risk management to process orders
- **enable_buy/enable_sell**: Controls which order types can be processed

## Algorithm Details

### Step 1: Order Collection and Filtering
1. Collect all pending orders with `quantity=0` for the expert
2. Filter orders based on expert's buy/sell permissions
3. Fetch linked recommendations with expected profit data

### Step 2: Profit-Based Prioritization
1. Sort orders by expected profit percentage (descending)
2. Group orders by instrument symbol
3. Calculate ROI ranking for special case handling

### Step 3: Virtual Balance Calculation
1. Get account's virtual trading balance
2. Calculate maximum equity per instrument (balance × max_virtual_equity_per_instrument_percent)
3. Account for existing positions to avoid over-allocation

### Step 4: Position Sizing Algorithm

#### Standard Allocation
- Distribute available equity across instruments for diversification
- Prefer smaller position sizes to allow more instruments
- Balance between profit potential and risk distribution

#### Special Case: Top 3 ROI Exception
- If an instrument's price > max_equity_per_instrument BUT < total_available_balance
- Allow single order allocation if the recommendation is in top 3 ROI
- Ensures high-profit opportunities aren't missed due to size constraints

#### Existing Position Handling
- Query existing transactions for the expert and instrument
- Subtract existing allocations from available per-instrument equity
- Prevents exceeding position limits across multiple orders

### Step 5: Quantity Calculation
1. For each prioritized order:
   - Calculate maximum affordable shares based on current price
   - Apply per-instrument equity limits
   - Consider existing positions
   - Set quantity to 0 if insufficient funds or limits exceeded

### Step 6: Database Updates
- Update pending orders with calculated quantities
- Log all decisions and constraints applied
- Maintain audit trail of risk management decisions

## Integration Points

### TradeManager Integration
- Called automatically from `process_expert_recommendations_after_analysis()`
- Only executes when `allow_automated_trade_opening` is enabled
- Processes orders after all analysis jobs complete

### Expert Settings Integration
- New setting: `max_virtual_equity_per_instrument_percent`
- Default value: 10.0 (10%)
- UI integration in expert configuration pages
- Validation: 1.0% to 100.0% range

## Decision Matrix

| Condition | Action |
|-----------|--------|
| Order type disabled (buy/sell) | Skip order |
| No linked recommendation | Skip order |
| Insufficient total balance | Set quantity to 0 |
| Exceeds per-instrument limit | Check top 3 ROI exception |
| Top 3 ROI + fits in total balance | Allocate full amount |
| Standard case | Allocate based on diversification |
| Existing positions exceed limit | Set quantity to 0 |

## Error Handling
- Graceful degradation when recommendation data missing
- Default to conservative position sizing on calculation errors
- Comprehensive logging for debugging and audit
- Continue processing remaining orders if individual order fails

## Performance Considerations
- Single database session for all updates
- Bulk fetching of related data (recommendations, positions)
- Efficient sorting and grouping algorithms
- Minimal API calls for current market prices

## Security and Validation
- Validate all monetary calculations
- Ensure quantities are non-negative
- Verify expert permissions before processing
- Audit trail for all risk management decisions

## Future Enhancements
- Dynamic risk adjustment based on market conditions
- Machine learning for optimal position sizing
- Integration with external risk management systems
- Real-time position monitoring and rebalancing