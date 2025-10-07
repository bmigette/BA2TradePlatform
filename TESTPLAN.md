# BA2 Trade Platform - Comprehensive Test Plan

**Last Updated**: 2025-10-07  
**Version**: 1.0

## Overview

This test plan provides a comprehensive list of manual and automated tests to validate the BA2 Trade Platform functionality in real-world scenarios. Tests are organized by feature area and include both happy path and edge case scenarios.

---

## 1. Order Management

### 1.1 Market Order Submission

#### Test 1.1.1: Submit Simple Market Buy Order
**Preconditions**:
- Account with sufficient buying power
- Valid symbol (e.g., AAPL)
- Market is open

**Steps**:
1. Navigate to account page
2. Create new market BUY order for 1 share of AAPL
3. Submit order
4. Wait for order to fill

**Expected Results**:
- ✅ Order created in database with PENDING status
- ✅ Order submitted to broker successfully
- ✅ Broker order ID populated
- ✅ Order status updates to FILLED
- ✅ `filled_qty` matches order quantity
- ✅ `open_price` populated with broker's execution price
- ✅ Transaction auto-created with WAITING status
- ✅ Transaction status updates to OPENED after fill
- ✅ Transaction `open_price` matches order `open_price`
- ✅ Tracking comment includes epoch timestamp and metadata

**Validation Queries**:
```sql
-- Check order was created and filled
SELECT id, symbol, quantity, status, filled_qty, open_price, broker_order_id 
FROM tradingorder WHERE symbol = 'AAPL' ORDER BY created_at DESC LIMIT 1;

-- Check transaction was created and opened
SELECT id, symbol, quantity, status, open_price, open_date 
FROM transaction WHERE symbol = 'AAPL' ORDER BY created_at DESC LIMIT 1;
```

---

#### Test 1.1.2: Submit Market Sell Order (Short)
**Preconditions**:
- Account with margin enabled
- Valid symbol
- Market is open

**Steps**:
1. Create market SELL order for 1 share
2. Submit order

**Expected Results**:
- ✅ Order fills successfully
- ✅ Transaction created with negative quantity
- ✅ Transaction status OPENED after fill

---

#### Test 1.1.3: Market Order During Market Closed
**Preconditions**:
- Market is closed (after hours or weekend)

**Steps**:
1. Submit market order

**Expected Results**:
- ✅ Order created with PENDING status
- ✅ Order submitted to broker
- ✅ Order remains in PENDING_NEW or ACCEPTED until market opens
- ✅ Order fills when market opens
- ✅ Price cache not used (fresh price fetched)

---

### 1.2 Limit Order Submission

#### Test 1.2.1: Submit Limit Buy Order
**Preconditions**:
- Account with buying power
- Current price known

**Steps**:
1. Get current price for symbol (e.g., $175.00)
2. Create limit BUY order at $174.00 (below market)
3. Submit order

**Expected Results**:
- ✅ Order created with `limit_price` = $174.00
- ✅ Order submitted to broker
- ✅ Order status ACCEPTED or PENDING_NEW
- ✅ Transaction created and linked
- ✅ Order fills when market price hits limit price

---

#### Test 1.2.2: Limit Order Validation
**Steps**:
1. Try to submit limit order without limit_price

**Expected Results**:
- ❌ Order validation fails
- ❌ Error message: "limit_price is required for BUY_LIMIT orders"

---

### 1.3 Stop Orders

#### Test 1.3.1: Submit Stop Loss Order
**Preconditions**:
- Existing open position
- Current price known

**Steps**:
1. Open position at $175.00
2. Create stop SELL order at $170.00
3. Submit order

**Expected Results**:
- ✅ Stop order created
- ✅ Order linked to transaction
- ✅ Order triggers when price drops to $170.00

---

### 1.4 Order Cancellation

#### Test 1.4.1: Cancel Unfilled Order
**Preconditions**:
- Limit order in ACCEPTED status

**Steps**:
1. Submit limit order
2. Cancel order via UI or API

**Expected Results**:
- ✅ Order canceled at broker
- ✅ Order status updated to CANCELED
- ✅ Transaction refreshed correctly

---

### 1.5 Order Refresh and Synchronization

#### Test 1.5.1: Refresh Orders from Broker
**Steps**:
1. Submit multiple orders
2. Modify order status at broker directly (if possible)
3. Click "Refresh Orders" button

**Expected Results**:
- ✅ All orders refreshed from broker
- ✅ Status updates reflected in database
- ✅ `open_price` updated from broker's `filled_avg_price`
- ✅ `filled_qty` synchronized
- ✅ No duplicate orders created

---

#### Test 1.5.2: Heuristic Order Mapping (Error Recovery)
**Preconditions**:
- Order in ERROR status without broker_order_id

**Steps**:
1. Create order that fails with broker_order_id missing
2. Click "Attempt sync from broker" button

**Expected Results**:
- ✅ Order mapped via comment field heuristic
- ✅ `broker_order_id` populated
- ✅ Order status updated
- ✅ Success notification shown

---

## 2. Transaction Management

### 2.1 Automatic Transaction Creation

#### Test 2.1.1: Market Order Auto-Creates Transaction
**Steps**:
1. Submit market order without transaction_id

**Expected Results**:
- ✅ Transaction auto-created
- ✅ Transaction linked to order
- ✅ Transaction has estimated `open_price` (current market price)
- ✅ Transaction status = WAITING
- ✅ Expert ID populated if order has expert_recommendation_id

---

#### Test 2.1.2: Limit Order Requires Transaction
**Steps**:
1. Try to submit limit order without transaction_id

**Expected Results**:
- ❌ Validation error
- ❌ Message: "Non-market orders must be attached to an existing transaction"

---

### 2.2 Transaction Status Transitions

#### Test 2.2.1: WAITING → OPENED Transition
**Preconditions**:
- Transaction with WAITING status
- Linked market order

**Steps**:
1. Wait for market order to fill
2. Run refresh_transactions()

**Expected Results**:
- ✅ Transaction status → OPENED
- ✅ `open_date` set to current time
- ✅ `open_price` updated from oldest filled market entry order
- ✅ Transaction `open_price` matches order `open_price` exactly

---

#### Test 2.2.2: OPENED → CLOSED via Take Profit
**Preconditions**:
- Transaction OPENED with filled position
- Take profit order created

**Steps**:
1. Wait for TP order to fill OR manually close position
2. Run refresh_transactions()

**Expected Results**:
- ✅ Transaction status → CLOSED
- ✅ `close_date` set
- ✅ `close_price` set from closing order `open_price`

---

#### Test 2.2.3: WAITING → CLOSED (All Orders Canceled)
**Preconditions**:
- Transaction WAITING
- All orders canceled before fill

**Steps**:
1. Cancel all linked orders
2. Run refresh_transactions()

**Expected Results**:
- ✅ Transaction status → CLOSED
- ✅ `close_date` set
- ✅ No `open_price` (never opened)

---

#### Test 2.2.4: OPENED → CLOSED (Balanced Position)
**Preconditions**:
- Transaction OPENED
- Multiple buy/sell orders

**Steps**:
1. Open position with 2 shares (BUY 2)
2. Close 1 share (SELL 1)
3. Close 1 share (SELL 1)
4. Run refresh_transactions()

**Expected Results**:
- ✅ Total buy = Total sell (position balanced)
- ✅ Transaction status → CLOSED
- ✅ `close_price` from last filled order

---

### 2.3 Transaction Price Updates

#### Test 2.3.1: Open Price Correction After Broker Sync
**Preconditions**:
- Transaction created with estimated `open_price` = $100.00

**Steps**:
1. Market order fills at $100.50 (actual execution)
2. Run refresh_orders() to sync from broker
3. Run refresh_transactions()

**Expected Results**:
- ✅ Order `open_price` = $100.50 (from broker)
- ✅ Transaction `open_price` updated to $100.50
- ✅ Old estimated price replaced

---

#### Test 2.3.2: Close Price from Multiple Orders
**Preconditions**:
- Transaction with 3 shares opened

**Steps**:
1. Sell 1 share at $105.00
2. Sell 2 shares at $105.50
3. Run refresh_transactions()

**Expected Results**:
- ✅ Position balanced (3 buy = 3 sell)
- ✅ Transaction `close_price` = $105.50 (from last order)
- ✅ Transaction CLOSED

---

#### Test 2.3.3: Price Update for Closed Transaction
**Preconditions**:
- Transaction already CLOSED

**Steps**:
1. Manually update order `open_price` in database
2. Run refresh_transactions()

**Expected Results**:
- ✅ Transaction `open_price` updated even though CLOSED
- ✅ Ensures historical accuracy

---

### 2.4 Transaction Closing

#### Test 2.4.1: Manual Close Transaction (No Filled Orders)
**Preconditions**:
- Transaction with unfilled orders only

**Steps**:
1. Click "Close Transaction" button

**Expected Results**:
- ✅ All unfilled orders canceled at broker
- ✅ PENDING/WAITING_TRIGGER orders marked CLOSED
- ✅ Transaction status → CLOSING → CLOSED
- ✅ Success message shown

---

#### Test 2.4.2: Close Transaction with Filled Position
**Preconditions**:
- Transaction OPENED with filled position

**Steps**:
1. Click "Close Transaction"

**Expected Results**:
- ✅ Closing market order created
- ✅ Closing order submitted to broker
- ✅ Order fills and closes position
- ✅ Transaction auto-closes after fill
- ✅ Success notification

---

#### Test 2.4.3: Retry Failed Close Order
**Preconditions**:
- Existing close order in ERROR status

**Steps**:
1. Click "Close Transaction" again

**Expected Results**:
- ✅ Error order resubmitted
- ✅ No duplicate close orders created
- ✅ Message: "Retried close order for [SYMBOL]"

---

#### Test 2.4.4: Close Transaction Already in Progress
**Preconditions**:
- Transaction status = CLOSING

**Steps**:
1. Click "Close Transaction" again

**Expected Results**:
- ✅ Continues with close process
- ✅ Handles retry scenario
- ✅ No errors thrown

---

## 3. Price Caching

### 3.1 Price Cache Functionality

#### Test 3.1.1: Fresh Price Fetch
**Steps**:
1. Clear price cache
2. Call get_instrument_current_price("AAPL")

**Expected Results**:
- ✅ Price fetched from broker API
- ✅ Price cached with timestamp
- ✅ Log: "Cached new price for AAPL: $XXX.XX"

---

#### Test 3.1.2: Cache Hit (Within Cache Time)
**Preconditions**:
- PRICE_CACHE_TIME = 30 seconds
- Price fetched 10 seconds ago

**Steps**:
1. Call get_instrument_current_price("AAPL") again

**Expected Results**:
- ✅ Cached price returned
- ✅ No broker API call made
- ✅ Log: "Returning cached price for AAPL: $XXX.XX (age: 10.0s)"

---

#### Test 3.1.3: Cache Miss (Expired)
**Preconditions**:
- Price fetched 40 seconds ago
- PRICE_CACHE_TIME = 30 seconds

**Steps**:
1. Call get_instrument_current_price("AAPL")

**Expected Results**:
- ✅ Cache expired
- ✅ Fresh price fetched from broker
- ✅ Cache updated with new price and timestamp
- ✅ Log: "Cache expired for AAPL (age: 40.0s > 30s)"

---

#### Test 3.1.4: Multiple Symbols in Cache
**Steps**:
1. Fetch price for AAPL
2. Fetch price for MSFT
3. Fetch price for GOOGL
4. Fetch AAPL again (within cache time)

**Expected Results**:
- ✅ Each symbol cached separately
- ✅ AAPL returned from cache on second call
- ✅ MSFT and GOOGL fetched fresh

---

#### Test 3.1.5: Cache Configuration via Environment
**Steps**:
1. Set PRICE_CACHE_TIME=60 in .env file
2. Restart application
3. Verify config.PRICE_CACHE_TIME = 60

**Expected Results**:
- ✅ Cache time configurable
- ✅ Default 30 seconds used if not set

---

## 4. Expert Recommendations and Rule Evaluation

### 4.1 Recommendation Generation

#### Test 4.1.1: Expert Generates BUY Recommendation
**Preconditions**:
- Expert instance enabled
- Symbol configured

**Steps**:
1. Trigger expert analysis
2. Wait for recommendation generation

**Expected Results**:
- ✅ ExpertRecommendation created
- ✅ Fields populated: symbol, recommended_action, confidence, risk_level, etc.
- ✅ Confidence stored as 1-100 (e.g., 78.5)
- ✅ price_at_date populated

---

#### Test 4.1.2: Recommendation Display in UI
**Steps**:
1. Navigate to expert recommendations page
2. View recommendation details

**Expected Results**:
- ✅ Confidence displayed as "78.5%" (not 0.785%)
- ✅ Recommendation action shows "BUY" (enum value extracted)
- ✅ Risk level and time horizon visible

---

### 4.2 Rule Evaluation

#### Test 4.2.1: Evaluate Trade Conditions
**Preconditions**:
- Ruleset with conditions defined
- Test recommendation

**Steps**:
1. Navigate to ruleset test page
2. Enter test parameters
3. Click "Run Test"

**Expected Results**:
- ✅ Condition evaluation results shown
- ✅ Operators displayed (>=, <, ==, etc.)
- ✅ Values displayed
- ✅ Reference values shown (if applicable)
- ✅ PASSED/FAILED status for each condition

**UI Display Example**:
```
✅ PASSED: confidence: value >= 70 (ref: 80.5)
❌ FAILED: risk_level: value == LOW (ref: MEDIUM)
```

---

#### Test 4.2.2: Evaluate Trade Actions
**Preconditions**:
- Ruleset with actions (BUY, SELL, etc.)

**Steps**:
1. Run ruleset evaluation

**Expected Results**:
- ✅ Actions displayed with parameters
- ✅ Take Profit shown: "TP: 15.0%"
- ✅ Stop Loss shown: "SL: 5.0%"
- ✅ Quantity shown: "Qty: 100.0%"
- ✅ Order type shown: "Type: MARKET"

**UI Display Example**:
```
Action 1: BUY
TP: 15.0% | SL: 5.0% | Qty: 100.0% | Type: MARKET
```

---

#### Test 4.2.3: Complex Rule with Multiple Conditions
**Preconditions**:
- Ruleset with AND/OR logic

**Steps**:
1. Create ruleset with 3+ conditions
2. Evaluate against test data

**Expected Results**:
- ✅ All conditions evaluated
- ✅ Summary shows total conditions checked
- ✅ Passed/failed count accurate
- ✅ Final rule result (executed/not executed) correct

---

### 4.3 Automated Trading from Expert

#### Test 4.3.1: Expert Generates Order from Recommendation
**Preconditions**:
- Expert with enter_market_ruleset configured
- Account linked

**Steps**:
1. Expert generates BUY recommendation
2. Ruleset evaluates to TRUE
3. Action creates order

**Expected Results**:
- ✅ TradingOrder created
- ✅ Order linked to ExpertRecommendation
- ✅ Order submitted to broker
- ✅ Transaction auto-created
- ✅ expert_id populated in transaction

---

## 5. Balance Usage and Equity Tracking

### 5.1 Virtual Equity Allocation

#### Test 5.1.1: Expert with Virtual Equity
**Preconditions**:
- Account balance: $10,000
- Expert with virtual_equity_pct = 50%

**Steps**:
1. Expert generates order
2. Check order quantity calculation

**Expected Results**:
- ✅ Expert allocated $5,000 (50% of $10,000)
- ✅ Order quantity based on $5,000 allocation
- ✅ Does not exceed allocated equity

---

### 5.2 Balance Usage Chart

#### Test 5.2.1: View Balance Usage Over Time
**Steps**:
1. Navigate to account overview
2. View balance usage chart

**Expected Results**:
- ✅ Chart shows balance and equity over time
- ✅ Open positions equity shown
- ✅ Pending orders equity shown
- ✅ Free balance calculated correctly

---

## 6. Data Visualization

### 6.1 Candlestick Charts

#### Test 6.1.1: Display Instrument Price Chart
**Steps**:
1. Navigate to instrument page
2. View candlestick chart

**Expected Results**:
- ✅ Candlesticks rendered correctly
- ✅ X-axis shows dates (not indices)
- ✅ Volume bars visible (opacity 0.15)
- ✅ Range slider disabled

---

### 6.2 Technical Indicators

#### Test 6.2.1: Display Trend Indicators
**Steps**:
1. View chart with trend indicators enabled

**Expected Results**:
- ✅ SMA, EMA overlays on price chart
- ✅ Aligned correctly with datetime
- ✅ No x-axis misalignment

---

#### Test 6.2.2: Display Momentum Indicators
**Steps**:
1. View chart with momentum indicators

**Expected Results**:
- ✅ RSI, ATR in separate subplot
- ✅ Categorized correctly
- ✅ Scaling appropriate

---

#### Test 6.2.3: Display Oscillators
**Steps**:
1. View chart with oscillators

**Expected Results**:
- ✅ MACD, Stochastic in subplot
- ✅ No duplicate range sliders
- ✅ Proper labeling

---

## 7. Error Handling and Edge Cases

### 7.1 Database Locking

#### Test 7.1.1: Concurrent Order Submissions
**Steps**:
1. Submit 5 orders simultaneously from different UI tabs

**Expected Results**:
- ✅ No "database is locked" errors
- ✅ WAL mode handles concurrency
- ✅ Retry logic succeeds
- ✅ All orders created successfully

---

### 7.2 Broker API Errors

#### Test 7.2.1: Order Submission Fails
**Steps**:
1. Submit order with invalid parameters
2. Or simulate broker API down

**Expected Results**:
- ✅ Order created in database (for tracking)
- ✅ Order status = ERROR
- ✅ Error message logged
- ✅ User notified with error details

---

#### Test 7.2.2: Price Fetch Fails
**Steps**:
1. Request price for invalid symbol
2. Or simulate API error

**Expected Results**:
- ✅ None returned
- ✅ No cache entry created
- ✅ Error logged
- ✅ Graceful fallback

---

### 7.3 Session Management

#### Test 7.3.1: No Session Attachment Errors
**Steps**:
1. Perform multiple database operations
2. Close transactions
3. Update orders

**Expected Results**:
- ✅ No "already attached to session" errors
- ✅ Proper session handling
- ✅ Objects properly expunged/merged

---

## 8. Integration Tests

### 8.1 End-to-End Trading Flow

#### Test 8.1.1: Complete Long Position Trade
**Steps**:
1. Expert generates BUY recommendation
2. Ruleset evaluates and creates BUY order
3. Order fills
4. Transaction opens
5. Take profit order created
6. TP order fills
7. Transaction closes

**Expected Results**:
- ✅ All steps execute successfully
- ✅ Prices accurate at each step
- ✅ Transaction profit/loss calculated
- ✅ All database records consistent

---

#### Test 8.1.2: Complete Short Position Trade
**Steps**:
1. Expert recommends SELL
2. Short order fills
3. Transaction opens (negative quantity)
4. Cover with BUY order
5. Transaction closes

**Expected Results**:
- ✅ Short position handled correctly
- ✅ Profit/loss calculated for short
- ✅ Transaction balances correctly

---

### 8.2 Multi-Account Testing

#### Test 8.2.1: Multiple Accounts with Same Symbol
**Steps**:
1. Set up 2 accounts
2. Both trade AAPL simultaneously

**Expected Results**:
- ✅ Orders isolated per account
- ✅ Transactions tracked separately
- ✅ No cross-contamination

---

## 9. Performance Tests

### 9.1 Price Cache Performance

#### Test 9.1.1: Cache Reduces API Calls
**Steps**:
1. Monitor API call count
2. Request same symbol price 10 times within cache window

**Expected Results**:
- ✅ Only 1 API call made
- ✅ 9 cache hits
- ✅ Significant performance improvement

---

### 9.2 Bulk Operations

#### Test 9.2.1: Refresh 100+ Orders
**Steps**:
1. Create 100+ orders
2. Click "Refresh Orders"

**Expected Results**:
- ✅ Completes within reasonable time (< 10 seconds)
- ✅ No timeouts
- ✅ All orders updated

---

## 10. User Interface Tests

### 10.1 Responsive Design

#### Test 10.1.1: Mobile View
**Steps**:
1. Access platform on mobile device
2. Navigate through pages

**Expected Results**:
- ✅ Layout responsive
- ✅ All features accessible
- ✅ Charts readable

---

### 10.2 Real-time Updates

#### Test 10.2.1: Auto-Refresh After Actions
**Steps**:
1. Submit order
2. Close transaction
3. Sync from broker

**Expected Results**:
- ✅ Page auto-reloads after action
- ✅ Updated data visible
- ✅ Smooth UX

---

## 11. Configuration and Environment

### 11.1 Environment Variables

#### Test 11.1.1: Load Config from .env
**Steps**:
1. Set variables in .env file
2. Start application
3. Verify config loaded

**Expected Results**:
- ✅ API keys loaded
- ✅ PRICE_CACHE_TIME configurable
- ✅ account_refresh_interval managed through AppSetting in database (not environment variable)

---

## Test Execution Guidelines

### Test Execution Order
1. **Setup Tests**: Environment, configuration
2. **Unit Tests**: Price cache, validation logic
3. **Integration Tests**: Order submission, transaction management
4. **End-to-End Tests**: Complete trading flows
5. **Performance Tests**: Load testing, cache efficiency
6. **UI Tests**: Visual and functional validation

### Test Environment Requirements
- **Paper Trading Account**: Use broker's paper trading/sandbox
- **Test Database**: Separate from production
- **Market Hours**: Some tests require market open, others can run anytime
- **Cleanup**: Reset database between test runs

### Success Criteria
- **Critical Tests**: 100% pass rate (order submission, transaction management)
- **Important Tests**: 95% pass rate (UI, caching)
- **Nice-to-Have Tests**: 85% pass rate (edge cases, performance)

### Test Documentation
For each test failure:
1. Document exact steps to reproduce
2. Capture logs (check logs/ directory)
3. Screenshot UI state
4. Note database state (SQL queries)
5. Create issue in repository with details

---

## Automated Testing Script Template

```python
# test_trading_flow.py
import time
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderType

def test_market_order_flow():
    """Test complete market order flow"""
    account = AlpacaAccount(account_id=1)
    
    # Create order
    order = TradingOrder(
        account_id=1,
        symbol="AAPL",
        quantity=1,
        side=OrderDirection.BUY,
        order_type=OrderType.MARKET
    )
    
    # Submit
    result = account.submit_order(order)
    assert result is not None, "Order submission failed"
    
    # Wait for fill
    time.sleep(5)
    
    # Refresh
    account.refresh_orders()
    account.refresh_transactions()
    
    # Verify
    # ... add assertions

if __name__ == "__main__":
    test_market_order_flow()
    print("✅ All tests passed!")
```

---

## Appendix: SQL Queries for Validation

### Check Order States
```sql
SELECT id, symbol, status, filled_qty, open_price, broker_order_id, created_at
FROM tradingorder 
ORDER BY created_at DESC LIMIT 20;
```

### Check Transaction States
```sql
SELECT id, symbol, status, quantity, open_price, close_price, open_date, close_date
FROM transaction 
ORDER BY created_at DESC LIMIT 20;
```

### Check Price Cache (Debugging)
```python
# Test price cache persistence (per-instance)
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import AccountInstance
from ba2_trade_platform.core.AccountInterface import get_account_instance

account = get_account_instance(1)  # Get account instance
print(account._price_cache)  # View instance-specific cache
```

### Check Failed Orders
```sql
SELECT id, symbol, status, comment, created_at
FROM tradingorder 
WHERE status = 'ERROR'
ORDER BY created_at DESC;
```

---

## Continuous Testing

### Daily Checks
- [ ] Submit and verify at least 1 test order
- [ ] Check error logs for anomalies
- [ ] Verify price cache working

### Weekly Checks
- [ ] Run full test suite
- [ ] Review transaction accuracy
- [ ] Check database integrity

### Monthly Checks
- [ ] Performance testing
- [ ] Load testing with high order volume
- [ ] Disaster recovery test (database restore)

---

**End of Test Plan**
