# TP/SL Percent Storage: Architecture Diagrams

## Overall Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     NORMAL FLOW (Percent Stored)                 │
└─────────────────────────────────────────────────────────────────┘

1. ACTION EVALUATION
   ┌──────────────────────┐
   │ TradeActionEvaluator │
   │  .evaluate()         │
   └──────────────────────┘
            │
            ↓
   ┌──────────────────────────────┐
   │ AdjustTakeProfitAction       │
   │  .execute()                  │
   │  reference_value="order_open_price" (or in action_config)
   └──────────────────────────────┘
            │
            ↓
   ┌──────────────────────────────┐
   │ AccountInterface             │
   │  .set_order_tp()             │
   └──────────────────────────────┘
            │
            ↓
2. PERCENT CALCULATION & STORAGE
   ┌──────────────────────────────────────┐
   │ AlpacaAccount                        │
   │  ._set_order_tp_impl()               │
   │                                      │
   │  tp_percent = (tp_price - filled)   │
   │               / filled * 100         │
   │                                      │
   │  Example: (268.45 - 239.69) / 239.69│
   │         = 12.0%                      │
   └──────────────────────────────────────┘
            │
            ↓
   ┌────────────────────────────────┐
   │ _create_tp_order_object()      │
   │                                │
   │ tp_order.limit_price = $268.45 │
   │ tp_order.data = {              │
   │   "type": "tp",                │
   │   "tp_percent": 12.0,          │
   │   "parent_filled_price": 239.69│
   │ }                              │
   └────────────────────────────────┘
            │
            ↓
   ┌─────────────────────────────┐
   │ DATABASE                    │
   │ TradingOrder saved with:    │
   │ - status: WAITING_TRIGGER   │
   │ - limit_price: $268.45      │
   │ - data: {tp_percent: 12.0}  │
   └─────────────────────────────┘

3. PARENT ORDER FILLED
   ┌─────────────────────┐
   │ Parent Order        │
   │ Status: FILLED      │
   │ open_price: $239.69 │
   └─────────────────────┘

4. TRIGGER DETECTION & RECALCULATION
   ┌──────────────────────────────────┐
   │ TradeManager                     │
   │  ._check_all_waiting_trigger_   │
   │   orders()                       │
   │                                  │
   │ for each WAITING_TRIGGER order:  │
   │   if "tp_percent" in data:       │
   │     new_price = parent.open *    │
   │                 (1 + percent/100)│
   │     = 239.69 * 1.12 = $268.45   │
   │                                  │
   │   Update order.limit_price       │
   └──────────────────────────────────┘
            │
            ↓
5. SUBMIT TO BROKER
   ┌────────────────────────┐
   │ Broker (Alpaca)        │
   │ SUBMIT TP ORDER        │
   │ Limit: $268.45         │
   │ Quantity: Same as entry│
   └────────────────────────┘
            │
            ↓
   ┌────────────────────────┐
   │ TP EXECUTES CORRECTLY  │
   │ ✓ Not affected by      │
   │   market price changes │
   │ ✓ Uses parent filled   │
   │   price, not bid/ask   │
   └────────────────────────┘
```

## Fallback Flow (When Percent Missing)

```
┌──────────────────────────────────────────┐
│    FALLBACK FLOW (Percent Not Stored)     │
└──────────────────────────────────────────┘

1. OLD ORDER WITHOUT PERCENT
   ┌──────────────────┐
   │ TradingOrder     │
   │ (created before  │
   │  this feature)   │
   │                  │
   │ data: null or {} │
   │ limit_price: $240│
   └──────────────────┘

2. TRANSACTION TP/SL SET
   ┌──────────────────┐
   │ Transaction      │
   │ take_profit: $250│
   └──────────────────┘

3. FALLBACK CALCULATION
   ┌────────────────────────────────────┐
   │ AccountInterface                   │
   │  ._submit_pending_tp_sl_orders()   │
   │                                    │
   │  Call:                             │
   │   _ensure_tp_sl_percent_stored(    │
   │     tp_order, parent_order)        │
   └────────────────────────────────────┘
            │
            ↓
   ┌────────────────────────────────────┐
   │ _ensure_tp_sl_percent_stored()     │
   │                                    │
   │ if "tp_percent" not in data:       │
   │   tp_percent = (limit_price - filled)
   │                 / filled * 100     │
   │   = (240 - 239.69) / 239.69 * 100 │
   │   = 0.13%  [CORRECTED FROM FALLBACK]
   │                                    │
   │   Store in order.data["tp_percent"]│
   │   Log: "FALLBACK calculation"      │
   └────────────────────────────────────┘
            │
            ↓
   ┌──────────────────────┐
   │ ORDER.DATA POPULATED │
   │ Now has tp_percent   │
   │ Ready for trigger    │
   └──────────────────────┘

4. CONTINUE AS NORMAL
   [Same as steps 3-5 in normal flow above]
```

## Data Structure Relationships

```
┌─────────────────────────────────────────────────────────────┐
│                       TradingOrder                          │
├─────────────────────────────────────────────────────────────┤
│ id: int                                                     │
│ symbol: "AMD"                                               │
│ quantity: 100                                               │
│ side: BUY                                                   │
│ order_type: MARKET (parent) / SELL_LIMIT (TP)             │
│ status: FILLED (parent) / WAITING_TRIGGER (TP/SL)          │
│ open_price: 239.69  ← FILLED PRICE (key reference!)       │
│ limit_price: 268.45  ← TP TARGET                           │
│ stop_price: null                                            │
│                                                             │
│ ┌─────────────────────────────────────────────────────┐   │
│ │ data: JSON (NEW FIELD)                              │   │
│ ├─────────────────────────────────────────────────────┤   │
│ │ {                                                   │   │
│ │   "type": "tp",                    [Order type]    │   │
│ │   "tp_percent": 12.0,        [Target %: 1-100]    │   │
│ │   "parent_filled_price": 239.69,   [Immutable ref] │   │
│ │   "recalculated_at_trigger": false, [State flag]   │   │
│ │   "calculation_timestamp": "2025-10-21T12:34:56Z"  │   │
│ │ }                                                   │   │
│ └─────────────────────────────────────────────────────┘   │
│                                                             │
│ depends_on_order: 338  ← Links to parent (entry) order    │
│ depends_order_status_trigger: FILLED  ← When to trigger   │
│ transaction_id: 142  ← Links to transaction (shared TP/SL)│
└─────────────────────────────────────────────────────────────┘
```

## Three-Layer Calculation System

```
PERCENT CALCULATION HIERARCHY
═══════════════════════════════

                    ┌──────────────┐
                    │   LAYER 1    │
                    │  PREFERRED   │
                    └──────────────┘
                          │
                          ↓
              ┌──────────────────────────┐
              │ TradeActionEvaluator     │
              │ Calculates percent when  │
              │ evaluating action        │
              │                          │
              │ Stores directly in       │
              │ action_config            │
              └──────────────────────────┘
                          │
                          ├─ Percent available? ✓ Use it
                          │
                          └─ Percent missing? ✗ Fall through
                                  │
                                  ↓
              ┌──────────────────────────┐
              │   LAYER 2                │
              │  FALLBACK               │
              ├──────────────────────────┤
              │ _submit_pending_tp_sl    │
              │ _orders()                │
              │                          │
              │ Calls:                   │
              │ _ensure_tp_sl_percent_  │
              │ stored()                 │
              │                          │
              │ Calculates from current  │
              │ limit_price if missing   │
              └──────────────────────────┘
                          │
                          ├─ Percent now in data? ✓ Use it
                          │
                          └─ Still missing? ✗ Fall through
                                  │
                                  ↓
              ┌──────────────────────────┐
              │   LAYER 3                │
              │  TRIGGER TIME           │
              ├──────────────────────────┤
              │ _check_all_waiting_      │
              │ trigger_orders()         │
              │                          │
              │ Uses percent from data   │
              │ if available             │
              │                          │
              │ Logs debug if missing    │
              └──────────────────────────┘
                          │
                          ↓
              Result: Percent always available
              by order submission time
```

## Class Diagram: Percent Storage Methods

```
┌─────────────────────────────────────────────────────────────┐
│              AccountInterface (BASE CLASS)                   │
│ ════════════════════════════════════════════════════════════│
│                                                              │
│ PUBLIC METHODS:                                              │
│  • set_order_tp(order, price) → TradingOrder               │
│  • set_order_sl(order, price) → TradingOrder               │
│                                                              │
│ PROTECTED METHODS:                                           │
│  • _submit_pending_tp_sl_orders(order)                      │
│  │                                                           │
│  └─→ _ensure_tp_sl_percent_stored(tp_sl_order, parent)◄────│
│      └─ NEW: Calculates percent if missing                 │
│         └─ Stores in order.data                            │
│         └─ Logs as FALLBACK if calculated                  │
│                                                              │
│  • _set_order_tp_impl() [ABSTRACT]                          │
│  • _set_order_sl_impl() [ABSTRACT]                          │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                          ▲
                          │ implements
                          │
┌─────────────────────────────────────────────────────────────┐
│            AlpacaAccount (IMPLEMENTATION)                   │
│ ════════════════════════════════════════════════════════════│
│                                                              │
│ IMPLEMENTATION:                                              │
│  • _set_order_tp_impl(order, tp_price) → TradingOrder      │
│    ├─ Calculate: tp_percent = (price - filled) / filled    │
│    ├─ Create: _create_tp_order_object(order, price, %)     │
│    └─ Store: order.data["tp_percent"]                      │
│                                                              │
│  • _create_tp_order_object(order, price, tp_percent)       │
│    └─ NEW: Accepts percent parameter                       │
│                                                              │
│  • _set_order_sl_impl(order, sl_price) → TradingOrder      │
│    ├─ Calculate: sl_percent = (price - filled) / filled    │
│    ├─ Create: _create_sl_order_object(order, price, %)     │
│    └─ Store: order.data["sl_percent"]                      │
│                                                              │
│  • _create_sl_order_object(order, price, sl_percent)       │
│    └─ NEW: Stores SL percent in data field                 │
│                                                              │
│  • _find_existing_sl_order(transaction_id)                 │
│    └─ NEW: Finds existing SL for transaction              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Log Output Progression

```
COMPLETE LOG TRAIL FOR ONE TP ORDER
════════════════════════════════════

STEP 1: Calculate percent (in _set_order_tp_impl)
───────────────────────────────────────────────────
[INFO] Calculated TP percent: 12.00% from filled price 
       $239.69 to target $268.45 for AMD

STEP 2: Store percent (in _create_tp_order_object)
──────────────────────────────────────────────────
[INFO] Created WAITING_TRIGGER TP order 123 at $268.45 
       with metadata: tp_percent=12.00% (will submit when 
       order 122 is FILLED)

STEP 3: Parent order filled (from broker)
─────────────────────────────────────────
[DEBUG] Order 122 status changed: PENDING → FILLED

STEP 4: Trigger detection (in TradeManager)
────────────────────────────────────────────
[INFO] Parent order 122 is in trigger status FILLED, 
       processing dependent order 123

[INFO] Recalculated TP price for order 123: parent 
       filled $239.69 * (1 + 12.00%) = $268.45 
       (was $268.45)

STEP 5: Submit to broker
────────────────────────
[INFO] Submitting dependent order 123: SELL 100 AMD @ 
       SELL_LIMIT $268.45 (triggered by parent order 122)

[INFO] Successfully submitted dependent order 123


FALLBACK LOG (if percent was missing initially)
═════════════════════════════════════════════════

[INFO] Submitting pending TP order ($268.45) to broker 
       for order 122

[INFO] Calculated and stored TP percent for order 123: 
       12.00% (parent filled $239.69 → TP target $268.45) 
       - FALLBACK calculation

[INFO] Successfully submitted TP order to broker
```

## Testing Verification Flowchart

```
                    ┌──────────────────┐
                    │ TEST CASE START  │
                    └─────────┬────────┘
                              │
                    ┌─────────▼────────┐
                    │ Create TP Order  │
                    │ with 12% target  │
                    └─────────┬────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
   ┌─────────┐          ┌─────────┐          ┌──────────┐
   │Verify   │          │Verify   │          │Verify    │
   │Percent  │          │Stored   │          │Order.data│
   │Calc.    │          │in data  │          │Structure │
   │=12.0%   │          │field    │          │Complete  │
   └────┬────┘          └────┬────┘          └────┬─────┘
        │                    │                    │
        ✓                    ✓                    ✓
        │                    │                    │
        └────────┬───────────┴────────┬──────────┘
                 │                    │
        ┌────────▼─────────────────────▼────────┐
        │ Trigger parent order to FILLED       │
        └────────┬──────────────────────────────┘
                 │
        ┌────────▼──────────────────┐
        │ TradeManager runs         │
        │ Check WAITING_TRIGGER     │
        └────────┬──────────────────┘
                 │
        ┌────────▼──────────────────┐
        │ TP price recalculated     │
        │ 239.69 * 1.12 = 268.45    │
        └────────┬──────────────────┘
                 │
        ┌────────▼──────────────────┐
        │ Verify logs show:         │
        │ "Recalculated TP price"   │
        └────────┬──────────────────┘
                 │
        ┌────────▼──────────────────┐
        │ Verify broker receives    │
        │ limit_price = $268.45     │
        │ (not affected by market)  │
        └────────┬──────────────────┘
                 │
        ┌────────▼──────────────────┐
        │ ✓ TEST PASSES             │
        └───────────────────────────┘
```

---

**For detailed information, see**:
- `TP_SL_PERCENT_STORAGE_ARCHITECTURE.md` - Full technical documentation
- `TP_SL_DESIGN_DECISIONS.md` - Design rationale and examples
