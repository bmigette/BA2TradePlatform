# Trade Manager Thread Safety - Flow Diagrams

## 1. Lock Acquisition Flow

```
Thread A                                Thread B
   |                                       |
   |  process_expert_recommendations       |  process_expert_recommendations
   |  (expert_id=1, enter_market)          |  (expert_id=1, enter_market)
   |                                       |
   v                                       v
[Get/Create Lock]                      [Get/Create Lock]
   |                                       |
   | lock_key = "expert_1_usecase_enter_market"
   |                                       |
   v                                       v
[Try Acquire]                          [Try Acquire]
timeout=0.5s                           timeout=0.5s
   |                                       |
   | SUCCESS! (t=0.00s)                    | BLOCKED... (t=0.00s)
   |                                       |
   v                                       | Waiting...
[LOG: Acquired lock]                      |
   |                                       | Still waiting... (t=0.25s)
   v                                       |
[Process Recommendations]                 |
   |                                       | Still waiting... (t=0.50s)
   | ... processing ...                    |
   |                                       v
   | ... still processing ...          [TIMEOUT!]
   |                                       |
   |                                       v
   |                                   [LOG: Could not acquire lock]
   |                                       |
   |                                       v
   |                                   [return []]
   | ... done processing ...               |
   |                                       X (Thread B exits)
   v
[finally block]
   |
   v
[Release Lock]
   |
   v
[LOG: Released lock]
   |
   v
[return created_orders]
   |
   X (Thread A exits)
```

## 2. Duplicate Transaction Check Flow

```
process_expert_recommendations_after_analysis(expert_id=1)
   |
   v
[Lock Acquired Successfully]
   |
   v
[Load Recommendations]
   |
   v
recommendations = [AAPL, GOOGL, MSFT]
   |
   v
For each recommendation:
   |
   |-- AAPL ----------------------------------
   |     |
   |     v
   | [Evaluate through ruleset]
   |     |
   |     v
   | [Passed! Actions to execute]
   |     |
   |     v
   | [SAFETY CHECK: Query existing transactions]
   |     |
   |     | SELECT * FROM transaction WHERE
   |     | expert_instance_id = 1 AND
   |     | symbol = 'AAPL' AND
   |     | status IN ('OPENED', 'WAITING')
   |     |
   |     v
   | [Result: Transaction #456 found (WAITING)]
   |     |
   |     v
   | [LOG WARNING: Existing transaction found]
   |     |
   |     v
   | [SKIP - continue to next]
   |     |
   |     X
   |
   |-- GOOGL ---------------------------------
   |     |
   |     v
   | [Evaluate through ruleset]
   |     |
   |     v
   | [Passed! Actions to execute]
   |     |
   |     v
   | [SAFETY CHECK: Query existing transactions]
   |     |
   |     | SELECT * FROM transaction WHERE
   |     | expert_instance_id = 1 AND
   |     | symbol = 'GOOGL' AND
   |     | status IN ('OPENED', 'WAITING')
   |     |
   |     v
   | [Result: None found]
   |     |
   |     v
   | [SAFE TO PROCEED]
   |     |
   |     v
   | [Execute actions - create orders]
   |     |
   |     v
   | [Order #789 created for GOOGL]
   |     |
   |     ✓
   |
   |-- MSFT ----------------------------------
   |     |
   |     v
   | [Evaluate through ruleset]
   |     |
   |     v
   | [Passed! Actions to execute]
   |     |
   |     v
   | [SAFETY CHECK: Query existing transactions]
   |     |
   |     | SELECT * FROM transaction WHERE
   |     | expert_instance_id = 1 AND
   |     | symbol = 'MSFT' AND
   |     | status IN ('OPENED', 'WAITING')
   |     |
   |     v
   | [Result: None found]
   |     |
   |     v
   | [SAFE TO PROCEED]
   |     |
   |     v
   | [Execute actions - create orders]
   |     |
   |     v
   | [Order #790 created for MSFT]
   |     |
   |     ✓
   |
   v
[Risk Management for created orders]
   |
   v
[Auto-submit orders to broker]
   |
   v
[Refresh order statuses]
   |
   v
[Check waiting trigger orders]
   |
   v
[finally: Release lock]
   |
   v
[return [Order#789, Order#790]]
```

## 3. Concurrent Expert Processing (Different Experts)

```
Thread A                        Thread B                        Thread C
expert_id=1                     expert_id=2                     expert_id=1
   |                               |                               |
   v                               v                               v
lock_key =                     lock_key =                     lock_key =
"expert_1_enter_market"        "expert_2_enter_market"        "expert_1_enter_market"
   |                               |                               |
   v                               v                               v
[Acquire Lock A] SUCCESS!      [Acquire Lock B] SUCCESS!      [Try Lock A] BLOCKED!
   |                               |                               |
   v                               v                               v
[Processing...]                [Processing...]                [Timeout 0.5s]
   |                               |                               |
   |                               |                               v
   |                               |                           [Skip - return []]
   |                               |                               |
   |                               |                               X
   |                               |
   v                               v
[Complete]                     [Complete]
   |                               |
   v                               v
[Release Lock A]               [Release Lock B]
   |                               |
   X                               X

RESULT:
- Thread A: Processed expert 1 successfully
- Thread B: Processed expert 2 successfully (concurrent with A)
- Thread C: Skipped (expert 1 already being processed)
```

## 4. Error Handling Flow

```
process_expert_recommendations_after_analysis(expert_id=1)
   |
   v
[Acquire Lock] SUCCESS!
   |
   v
try:
   |
   v
   [Load expert instance]
   |
   v
   [Load recommendations]
   |
   v
   [Process recommendation #1]
   |
   v
   [Execute actions]
   |
   v
   [Process recommendation #2]
   |
   v
   [Execute actions]
   |
   v
   💥 EXCEPTION! Database connection lost
   |
   v
except Exception as e:
   |
   v
   [LOG ERROR: Error processing expert recommendations]
   |
   v
finally:
   |
   v
   [Release Lock] ← ALWAYS EXECUTES!
   |
   v
   [LOG: Released lock]
   |
   v
return created_orders (may be partial list)
   |
   X

RESULT: Lock released despite error, other threads can proceed
```

## 5. Safety Check States

```
┌─────────────────────────────────────────────────────────────┐
│  Transaction States                                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  WAITING ────┐                                              │
│              │                                              │
│              ├──> BLOCKS new orders (Safety Check)          │
│              │                                              │
│  OPENED  ────┘                                              │
│                                                             │
│                                                             │
│  CLOSING ────┐                                              │
│              │                                              │
│              ├──> ALLOWS new orders (Safe to proceed)       │
│              │                                              │
│  CLOSED  ────┘                                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘

Example Scenarios:

Scenario 1: New Recommendation for AAPL
   Existing: Transaction #100 (AAPL, expert_1, WAITING)
   Action: SKIP - Log warning
   Reason: Position opening in progress

Scenario 2: New Recommendation for GOOGL
   Existing: Transaction #101 (GOOGL, expert_1, OPENED)
   Action: SKIP - Log warning
   Reason: Position already open

Scenario 3: New Recommendation for MSFT
   Existing: Transaction #102 (MSFT, expert_1, CLOSING)
   Action: ALLOW - Proceed
   Reason: Position being closed, safe to open new

Scenario 4: New Recommendation for TSLA
   Existing: Transaction #103 (TSLA, expert_1, CLOSED)
   Action: ALLOW - Proceed
   Reason: Previous position closed

Scenario 5: New Recommendation for NVDA
   Existing: None
   Action: ALLOW - Proceed
   Reason: No existing position
```

## 6. Lock Dictionary Structure

```
TradeManager Instance
   |
   |-- _processing_locks: Dict[str, threading.Lock]
   |     |
   |     |-- "expert_1_usecase_enter_market" → Lock Object A
   |     |
   |     |-- "expert_1_usecase_open_positions" → Lock Object B
   |     |
   |     |-- "expert_2_usecase_enter_market" → Lock Object C
   |     |
   |     |-- "expert_3_usecase_enter_market" → Lock Object D
   |     |
   |     └── ... (grows as needed)
   |
   └-- _locks_dict_lock: threading.Lock
         |
         └── (Meta-lock to protect the dictionary itself)

Thread Safety:
   To add/get lock from dictionary:
      with self._locks_dict_lock:
          if key not in self._processing_locks:
              self._processing_locks[key] = threading.Lock()
          return self._processing_locks[key]
```

## 7. Complete Flow with All Safety Features

```
┌──────────────────────────────────────────────────────────────────────┐
│  process_expert_recommendations_after_analysis(expert_id=1)          │
└──────────────────────────────────────────────────────────────────────┘
                              |
                              v
                    ┌─────────────────────┐
                    │  Get/Create Lock    │
                    │  for expert_1       │
                    └─────────────────────┘
                              |
                              v
                    ┌─────────────────────┐
                    │  Try Acquire Lock   │
                    │  (timeout=0.5s)     │
                    └─────────────────────┘
                              |
                    ┌─────────┴─────────┐
                    |                   |
                  FAIL               SUCCESS
                    |                   |
                    v                   v
          ┌──────────────────┐  ┌──────────────────┐
          │  Log: Skipping   │  │  Log: Acquired   │
          └──────────────────┘  └──────────────────┘
                    |                   |
                    v                   v
              return []           try { ... }
                                        |
                                        v
                              ┌──────────────────────┐
                              │  Load Recommendations│
                              └──────────────────────┘
                                        |
                                        v
                              ┌──────────────────────┐
                              │  For each rec:       │
                              │  1. Evaluate ruleset │
                              └──────────────────────┘
                                        |
                                        v
                              ┌──────────────────────┐
                              │  SAFETY CHECK:       │
                              │  Existing txn?       │
                              └──────────────────────┘
                                        |
                              ┌─────────┴─────────┐
                              |                   |
                           FOUND              NOT FOUND
                              |                   |
                              v                   v
                    ┌──────────────────┐  ┌──────────────────┐
                    │  Log: Warning    │  │  Execute actions │
                    │  Skip this rec   │  │  Create orders   │
                    └──────────────────┘  └──────────────────┘
                              |                   |
                              v                   v
                          continue            created_orders
                                                  |
                                        ┌─────────┴─────────┐
                                        |                   |
                                        v                   v
                              ┌──────────────────┐  ┌──────────────────┐
                              │  Risk Management │  │  Auto-submit     │
                              └──────────────────┘  └──────────────────┘
                                        |
                              ┌─────────┴─────────┐
                              |                   |
                          SUCCESS              ERROR
                              |                   |
                              v                   v
                    ┌──────────────────┐  ┌──────────────────┐
                    │  Complete        │  │  Log error       │
                    │  normally        │  │  (partial result)│
                    └──────────────────┘  └──────────────────┘
                              |                   |
                              └─────────┬─────────┘
                                        v
                              ┌──────────────────────┐
                              │  finally {           │
                              │    Release Lock      │
                              │    Log: Released     │
                              │  }                   │
                              └──────────────────────┘
                                        |
                                        v
                              return created_orders
```

## Key Takeaways

1. **Lock prevents concurrent processing** of same expert
2. **Short timeout (0.5s)** prevents blocking - threads skip instead
3. **Safety check prevents duplicates** by checking existing transactions
4. **Finally block guarantees** lock release even on errors
5. **Different experts can process concurrently** (different lock keys)
6. **Clear logging** at every decision point for debugging

## Visual Summary

```
🔒 LOCK SYSTEM
   ✓ Per-expert/use_case locks
   ✓ 0.5s timeout - no blocking
   ✓ Always released (finally)

🛡️ SAFETY CHECK
   ✓ Checks OPENED/WAITING transactions
   ✓ Per-symbol verification
   ✓ Logs warnings on duplicates

📊 RESULT
   ✓ Thread-safe
   ✓ No duplicates
   ✓ Graceful degradation
   ✓ Clear logging
```
