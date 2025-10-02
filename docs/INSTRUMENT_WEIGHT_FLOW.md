# Instrument Weight Calculation Flow

## Visual Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    RISK MANAGEMENT PIPELINE                         │
└─────────────────────────────────────────────────────────────────────┘

Step 1: Order Collection & Filtering
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌──────────────────────────────────────┐
│ Get pending orders (quantity=0)       │
│ Filter by buy/sell permissions        │
│ Link to recommendations (profit data) │
└──────────────────────────────────────┘
                  ↓

Step 2: Profit-Based Prioritization  
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌──────────────────────────────────────┐
│ Sort by expected_profit_percent DESC  │
│ Highest ROI opportunities first       │
└──────────────────────────────────────┘
                  ↓

Step 3: Balance & Limits Calculation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌──────────────────────────────────────┐
│ total_balance = $10,000               │
│ max_per_instrument = $1,000 (10%)     │
│ existing_allocations = {...}          │
└──────────────────────────────────────┘
                  ↓

Step 4: For Each Order (by priority)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌──────────────────────────────────────┐
│ 4a. Get current price                 │
│ 4b. Calculate max_qty_by_balance      │
│ 4c. Calculate max_qty_by_instrument   │
└──────────────────────────────────────┘
                  ↓
┌──────────────────────────────────────┐
│ 4d. Apply diversification (0.7x)      │
│     if multiple instruments           │
└──────────────────────────────────────┘
                  ↓
┌──────────────────────────────────────┐
│ 4e. Calculate base_quantity           │
│     = min(constraints) * 0.7          │
└──────────────────────────────────────┘
                  ↓
╔══════════════════════════════════════╗
║ ⭐ NEW: APPLY INSTRUMENT WEIGHT ⭐   ║
╠══════════════════════════════════════╣
║ 5a. Get instrument weight from       ║
║     expert settings (default=100)    ║
║                                      ║
║ 5b. Calculate weighted quantity:     ║
║     weighted_qty = base_qty *        ║
║                   (weight/100)       ║
║                                      ║
║ 5c. Check if affordable:             ║
║     cost = weighted_qty * price      ║
║                                      ║
║ 5d. If cost > limits:                ║
║     → Keep base_qty (no weight)      ║
║     Else:                            ║
║     → Use weighted_qty               ║
╚══════════════════════════════════════╝
                  ↓
Step 6: Update & Track
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌──────────────────────────────────────┐
│ order.quantity = final_quantity       │
│ remaining_balance -= (qty * price)    │
│ instrument_allocations[symbol] += cost│
└──────────────────────────────────────┘
                  ↓
Step 7: Database Update
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┌──────────────────────────────────────┐
│ Save updated orders to database       │
│ Log allocation summary                │
└──────────────────────────────────────┘
```

## Weight Formula Deep Dive

```
═══════════════════════════════════════════════════════════════
                    WEIGHT CALCULATION
═══════════════════════════════════════════════════════════════

Input:
  base_quantity = 10 shares
  instrument_weight = 150
  current_price = $100/share

Calculation:
  multiplier = weight / 100
             = 150 / 100
             = 1.5

  weighted_quantity = base_quantity * multiplier
                    = 10 * 1.5
                    = 15 shares

  weighted_cost = weighted_quantity * current_price
                = 15 * $100
                = $1,500

Validation:
  ✓ Check: weighted_cost <= remaining_balance?
  ✓ Check: weighted_cost <= max_per_instrument?
  
  If BOTH checks pass:
    final_quantity = 25 shares ✓
  
  If ANY check fails:
    final_quantity = 10 shares (revert to base)
    
═══════════════════════════════════════════════════════════════
```

## Weight Impact Table

```
┌────────┬────────────┬──────────────────────────────────┐
│ Weight │ Multiplier │ Example (base = 10 shares)       │
├────────┼────────────┼──────────────────────────────────┤
│  25    │   0.25x    │  10 → 2   (25% of base)          │
│  50    │   0.5x     │  10 → 5   (50% of base)          │
│  75    │   0.75x    │  10 → 7   (75% of base)          │
│ 100    │   1.0x     │  10 → 10  (DEFAULT, unchanged)   │
│ 125    │   1.25x    │  10 → 12  (25% more)             │
│ 150    │   1.5x     │  10 → 15  (50% more)             │
│ 175    │   1.75x    │  10 → 17  (75% more)             │
│ 200    │   2.0x     │  10 → 20  (doubled)              │
│ 300    │   3.0x     │  10 → 30  (tripled)              │
└────────┴────────────┴──────────────────────────────────┘
```

## Real-World Scenario

```
═══════════════════════════════════════════════════════════════
                    PORTFOLIO EXAMPLE
═══════════════════════════════════════════════════════════════

Configuration:
  • Total Balance: $10,000
  • Max Per Instrument: $1,000 (10%)
  • 4 Instruments Enabled

Risk Management calculates base quantities:

┌──────────┬────────┬────────┬──────────┬─────────────────┐
│ Instrument│  ROI   │ Price  │  Base    │ Base Cost       │
├──────────┼────────┼────────┼──────────┼─────────────────┤
│ AAPL     │ 15.2%  │ $180   │  3 shr   │  $540           │
│ MSFT     │ 12.8%  │ $380   │  1 shr   │  $380           │
│ GOOGL    │ 10.5%  │ $140   │  5 shr   │  $700           │
│ NVDA     │  9.1%  │ $500   │  1 shr   │  $500           │
└──────────┴────────┴────────┴──────────┴─────────────────┘
                         Total Allocated: $2,120

═══════════════════════════════════════════════════════════════

User configures weights:
  • AAPL: 150 (high conviction)
  • MSFT: 100 (neutral)
  • GOOGL: 50 (low conviction)
  • NVDA: 200 (very high conviction)

After weight application:

┌──────────┬────────┬──────────┬──────────┬─────────────────┐
│Instrument│ Weight │ Weighted │  Final   │  Final Cost     │
├──────────┼────────┼──────────┼──────────┼─────────────────┤
│ AAPL     │  150   │  7.5 shr │  7 shr   │ $1,260 ❌ LIMIT │
│          │        │  (revert)│  3 shr   │  $540 ✓         │
│ MSFT     │  100   │  2.0 shr │  2 shr   │  $760 ✓         │
│ GOOGL    │   50   │  7.5 shr │  7 shr   │  $980 ✓         │
│ NVDA     │  200   │  3.0 shr │  3 shr   │ $1,500 ❌ LIMIT │
│          │        │  (revert)│  1 shr   │  $500 ✓         │
└──────────┴────────┴──────────┴──────────┴─────────────────┘
                         Total Allocated: $2,780

Result:
  • AAPL: Kept original (weight would exceed $1k limit)
  • MSFT: Doubled from 1 to 2 shares (weight applied)
  • GOOGL: Reduced from 5 to 7 shares (weight applied)
  • NVDA: Kept original (weight would exceed $1k limit)

═══════════════════════════════════════════════════════════════
```

## Decision Tree

```
                   ┌─────────────────────┐
                   │ Start: Base Quantity│
                   │    Calculated       │
                   └──────────┬──────────┘
                              │
                              ▼
                   ┌─────────────────────┐
                   │  quantity > 0?      │
                   └──────────┬──────────┘
                      No ─────┤───── Yes
                              │            │
                    ┌─────────▼────────┐   │
                    │ Keep quantity=0  │   │
                    └──────────────────┘   │
                                           ▼
                              ┌──────────────────────┐
                              │ Symbol in instrument │
                              │    configs?          │
                              └──────────┬───────────┘
                                 No ─────┤───── Yes
                                         │            │
                           ┌─────────────▼────────┐   │
                           │ Use default weight   │   │
                           │    (100)             │   │
                           └──────────────────────┘   │
                                                      ▼
                                         ┌──────────────────────┐
                                         │ weight = 100?        │
                                         └──────────┬───────────┘
                                            Yes ────┤───── No
                                                    │            │
                                      ┌─────────────▼────────┐   │
                                      │ Keep base_quantity   │   │
                                      │ (no log)             │   │
                                      └──────────────────────┘   │
                                                                 ▼
                                                    ┌──────────────────────┐
                                                    │ Calculate:           │
                                                    │ weighted_qty =       │
                                                    │ base * (1+weight/100)│
                                                    └──────────┬───────────┘
                                                               │
                                                               ▼
                                                    ┌──────────────────────┐
                                                    │ weighted_cost =      │
                                                    │ weighted_qty * price │
                                                    └──────────┬───────────┘
                                                               │
                                                               ▼
                                                    ┌──────────────────────┐
                                                    │ cost <= balance AND  │
                                                    │ cost <= per_instr?   │
                                                    └──────────┬───────────┘
                                                       No ─────┤───── Yes
                                                               │            │
                                                 ┌─────────────▼────────┐   │
                                                 │ Revert to base_qty   │   │
                                                 │ Log: "exceeds limits"│   │
                                                 └──────────────────────┘   │
                                                                            ▼
                                                               ┌──────────────────────┐
                                                               │ Use weighted_qty     │
                                                               │ Log: "applied weight"│
                                                               └──────────────────────┘
                                                                            │
                                                                            ▼
                                                               ┌──────────────────────┐
                                                               │ order.quantity = qty │
                                                               │ Update balance       │
                                                               └──────────────────────┘
```

## Key Takeaways

1. **Weight is applied AFTER base calculation** - All risk management rules apply first
2. **Weight respects limits** - Never exceeds balance or per-instrument limits
3. **Default weight (100) is transparent** - No logging, maintains existing behavior
4. **Safety-first approach** - Reverts to base quantity if weighted amount unsafe
5. **User has control** - Can set weights per instrument for portfolio customization

