# Stock decision-pass capture and replay: design

Status: DESIGN ONLY (nothing implemented, nothing committed). Author context: replay-parity audit
2026-11-08, gaps #3 (stock decision stage not recorded) and #4 (every pass stored as `error`, replay
reports TradeManager `unsupported`). Builds on `docs/plans/2026-11-05-option-capture-replay-design.md`
(the option equivalent) and `docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md`.
Sibling work that touches neighbouring files and must merge first or be rebased on: option clock fix
(`option_selection.py` decision frame), full-settings recording, provenance.

All line numbers are `dev` @ 751e16c5 in the `BA2-replayparity` worktree.

## 0. Prod effect (read this first)

Today a stock pass records nothing: `_decision_capture_scope` (`ba2_trade_platform/core/TradeManager.py:2836-2937`)
returns `nullcontext()` unless the expert has a market-condition resolver or option rules (`:2889-2935`).
After this change, with capture ON (it is on in all three apps):

| App | Today | After |
|---|---|---|
| 8080 dev (stock, ~25 instances) | analyses only (fmp/market_data/macro) | + one pass record per enter-market pass and per open-positions pass that reaches rule evaluation, with rules / sizing / execute steps |
| 8081 prod (live, stock) | analyses only | same as 8080. LIVE MONEY: the recorder is on the order path, see the failure policy below |
| 8082 options (option + stock-route experts) | option passes only (21 error-stamped rows in 5 days) | + the stock-route entries/exits of the same experts; the option passes now end with a real outcome |

Measured cost (Task 1, `BA2_REPLAY_DECISION_DRYRUN=1`, `tests/test_decision_pass_sizing_counter.py::test_measure_realistic_pass`;
the estimates this replaces were 30-150 KB and 50-300 ms per pass). Fixture: ONE enter-market pass of a classic-RM expert with
20 recommendations (12 funded), 30 open transactions (entry + TP + SL order each), a four-condition entry ruleset with
stop-loss and take-profit actions, ~3 KB of `data` and ~640 characters of `details` per recommendation (synthetic: the
absolute figures scale with the real rows), fake account; median of 3 alternating off/on runs on the dev machine.

| Part | Steps | Before the size cut | After the size cut | Per unit (after) |
|---|---|---|---|---|
| `pass_state` | 1 | 173,998 | 198,495 (a ~25 KB index plus the shared row objects) | ~1.0 KB per row object (~200 rows) |
| `rules` | 20 | 131,811 | 77,289 | ~3.9 KB per recommendation (the evaluation details) |
| `size` | 1 | 71,009 | 21,652 | ~1.1 KB per candidate |
| `execute` | 12 | 193,921 | 78,929 | ~6.6 KB per funded entry |
| decision summary (`decision_object`) | 1 | 12,748 | 12,748 | |
| broker-read observations now taken inside the pass (quote taps) | (not steps) | 40,504 | 7,265 (identical payloads are one object) | |
| **Total per pass (first pass, nothing on disk yet)** | | **623,991** | **396,389 (-36%)** | |

(Before = the Task 1 measurement redone on this fixture with distinct per-recommendation `data`, which is what
real analyses carry; with identical payloads it was the 612,451 first quoted. Pass-thread cost is unchanged: 54 ms of
recording, wall 1.41 s without capture vs 1.59 s with the dry run.)

The size cut (coordinator decision, nothing had shipped, so the payload shape the replay reads is the new one):

* (a) A step no longer repeats a recommendation row. `rules`, `size` and `execute` hold the row as a `SharedValue`
  (`codec.py`: stored as its own content-addressed JSON object, referenced by hash, decoded back transparently), and it is
  byte-identical to the one `pass_state` holds, so it is the SAME object on disk. This is the by-id reference of the brief
  with one difference: the reader gets the full row from the payload without a lookup in `pass_state`, and a
  recommendation that `pass_state` did not hold still works.
* (b) `execute` no longer carries the raw `rows_after` (the normalised `orders` / `transactions` of the view are the record).
* (c) Every Transaction, TradingOrder and ExpertRecommendation row in `pass_state` is its own object, and so is any row
  field of 512+ JSON characters (a recommendation's `data` / `details`, which an order row repeats verbatim). The same
  holds for the order dumps inside the recorded `submit_order` effects, the candidates in `size` and the orders of the
  `execute` view. An unchanged row is the same bytes on the next pass, so the store keeps one copy (`put` is a no-op on an
  existing hash).

Dedupe over two consecutive passes (test `test_unchanged_rows_dedupe_across_two_consecutive_passes`: same fixture,
nothing funded so only the book's own churn changes, dry-run counter with a process-wide set of stored hashes):

| | pass 1 | pass 2 (new bytes on disk) |
|---|---|---|
| `pass_state` | 198,300 | 85,803 (60 of the 120 order rows changed state between the passes: the fake broker moved the waiting TP/SL legs to NEW; the 30 transactions, the other 60 orders and the 20 recommendations are not rewritten) |
| `rules` | 77,289 | 0 (byte-identical payloads) |
| `size` | 14,946 | 14,946 (its candidates carry a creation timestamp) |
| total | 299,497 | 108,690 (-64%) |

A pass that funds entries sees the previous pass' new positions as new rows (measured: 12 funded, second pass 201,511 of
333,408 new). The dedupe is exact byte equality of a row, so it only helps for rows that did not change.

Estimated growth, ~(passes/day x bytes): all passes cold 396 KB: 8080 ~100 passes = 40 MB/day, 8082 ~60 = 24 MB/day,
8081 ~30 = 12 MB/day (before the cut: 62 / 37 / 19 MB). With the repeat-pass dedupe above (109-200 KB new per pass): 8080
11-20 MB/day, 8082 7-12, 8081 3-6. Two costs the cut introduces and the numbers do not show: file count (every changed row is
one object file; a worst-case repeat pass writes ~60-100 new files, each fsynced on the WRITER thread, not the pass thread)
and the first pass of a session being larger in `pass_state` (198 vs 174 KB) because nothing dedupes within a pass.

So a LOADED pass is still several times the old 30-150 KB estimate (the evaluation details in `rules`, 3.9 KB per
recommendation, are the remaining bulk; they repeat each condition once more inside `rule_evaluations`, a further ~35% cut
available there but it changes the stored `evaluation_details` shape, so it is NOT done). A pass with few recommendations
and nothing funded is proportionally smaller (use the per-unit figures). The store has no GC (below), so retention is the
first follow-up.

* Passes: 8080 ~100 (enter + exit) per trading day, 8082 ~60, 8081 ~30 (audit numbers, unchanged). A pass record is only
  written when the pass reaches evaluation; early exits that are not decisions are discarded and COUNTED (section 2.4).
* Store growth on top of today's 10-55 MB/day/app (audit section 1): see the measured size above times the pass count. The
  store has no GC (`ObjectStore.iter_orphans`, `store.py:149`, has no caller).
* Observation writes go through the existing async queue (`store.submit`), not the pass thread.
* Decisions: unchanged. The recorder only reads, uses its own short-lived sessions (the enter loop's read-only
  session invariant, `TradeManager.py:3175-3195`, is not touched) and never raises into the pass. Proven by
  `tests/test_decision_pass_capture_roundtrip.py` (real passes, capture on == capture off, also with every recorder broken).
* GA/backtest: not affected. Taps are passthrough outside a capture scope; the one shared-code edit that matters
  (the `TradeConditions` clock seam, Task 3) is a no-op when `as_of` is given. BT byte gate (row 2201): IDENTICAL.

Failure policy (the owner rule "no silent failure"): recording is never a dependency of trading, but every failure
to record is COUNTED, WRITTEN into the session and SHOWN (section 5). The `nullcontext()`-plus-log-line pattern at
`TradeManager.py:2924,2935` is removed.

## 1. The live path (what happens, and what it reads)

Trigger: `WorkerQueue.py:~1690-1721` picks the manager per expert. SMART mode queues a `SmartRiskManagerTask`
(LLM graph, `SmartRiskManagerGraph.py:2679 run_smart_risk_manager`); CLASSIC mode calls
`TradeManager.process_expert_recommendations_after_analysis(id)` (`:3001`, enter) or
`process_open_positions_recommendations(id)` (`:3910`, exit). Manual triggers: `ui/api_routes.py:244`,
`ui/pages/marketanalysis.py:5228` (same methods, so also recorded). Also `_process_...` is entered once per
"no more pending analysis jobs", and returns early for several reasons (below).

### 1.1 Enter-market pass (`_process_expert_recommendations_after_analysis`, `TradeManager.py:3059-3540`)

| # | Step | Code | Reads |
|---|---|---|---|
| E0 | lock (0.5 s), else return `[]` | `:3076-3095` | in-process lock |
| E1 | `_publish_benchmark_regime()` | `:3110` | SPY daily bars (provider), clock |
| E2 | resolve expert, `ExpertInstance` row | `:3118-3130` | DB: ExpertInstance, ExpertSetting |
| E3 | gates: `allow_automated_trade_opening`, `enter_market_ruleset_id`, pending analysis jobs | `:3134-3150` | settings, DB: MarketAnalysis/queue |
| E4 | account object | `:3153-3162` | AccountDefinition (credentials live here: never recorded) |
| E5 | recommendation selection: cutoff `datetime.now()-lookback` (`:3165`), HOLD filter, latest per symbol, sort by expected profit | `:3180-3215` | DB: ExpertRecommendation, WALL CLOCK (not `replay_now`) |
| E6 | per recommendation: `TradeActionEvaluator.evaluate` (rules) | `:3234` -> `TradeActionEvaluator.py:405` | ruleset rows (`_load_ruleset_and_actions` `:443`: Ruleset, EventAction, links), `TradeConditions` reads: account price (`TradeConditions.py:346`), `account.get_positions()` (`:302`), OHLCV/indicator providers (`:2528,2593,2854,2996`, already tapped via `MarketDataProviderInterface.py:1761`, `MarketIndicatorsInterface.py:112`), fundamentals (`:3138`), market-condition windows (tapped by `begin_decision`), and `date.today()` (`TradeConditions.py:419-431`) |
| E7 | no actions / errors: stores `TradeActionResult(evaluation_only/evaluation_error)` | `:3243-3290` | writes DB |
| E8 | dup-position gate `open_or_waiting_position` | `:3293` (`trade_cycle.py:89`) | DB: Transaction |
| E9 | `expert.has_sufficient_equity_for_trading()` | `:3306` | account snapshot, DB: transactions, bulk prices |
| E10 | route `entry_route` (equity vs option vs mixed refusal) | `:3320` (`trade_cycle.py:53`) | none |
| E11 | OPTION: `evaluator.execute(submit_to_broker=True)` (self-sizing, already captured by the option record) | `:3336` | see option design |
| E12 | EQUITY: `build_entry_candidate(rec, account.id, lot_size=fired_entry_lot_size(evaluator))` (transient qty 0 order) | `:3353` (`trade_cycle.py:145,126`) | none |
| E13 | RM sizing of ALL candidates in one pass: `get_risk_management().size_candidate_orders` | `:3380` -> `TradeRiskManagement.py:534` -> `_size_prioritized_orders :410` -> `_calculate_order_quantities :1329` | settings (`enable_buy/sell`, `max_virtual_equity_per_instrument_percent`, `sizing_mode`, `diversification_factor`, regime scale), `expert._available_balance_breakdown` (`MarketExpertInterface.py:1959`: virtual balance = account snapshot x expert share, used = capital-holding transactions + bulk `get_instrument_current_price`), `_get_existing_allocations` (`:1280`), bulk prices (`:1428`), fractionable lookup (`_fractionable_by_symbol :1750`), commission (`:1787`), ATR via the injected indicator provider (`_risk_atr_quantity :1910`, `_ensure_safeguard_stop :1833`, `position_sizing.get_latest_atr :461`, end_date `datetime.now` unless `as_of`) |
| E14 | the RM already BUILDS a full explanation: `_record_candidate_run` (`:1144`) -> `risk_manager_run.record_run` (`risk_manager_run.py:223`): per-symbol `decisions` (outcome FUNDED/REFUSED_*, reason, quantity, operands, binding constraint via `traces`/`_clamp_binding :791`) and `context` (virtual, used, available, max per instrument, sizing mode, regime scale). It is persisted as a `RiskManagerRun` row in live and returns `None` in a backtest (`inmem_trades_active`) | | writes DB |
| E15 | per FUNDED candidate: `evaluator.execute()` (creates the order, transaction, TP/SL bracket rows via the action classes: `BuyAction.execute TradeActions.py:932`, `create_order_record :542`, `_AdjustPriceLevelAction.compute_price :1592`) | `:3397` | DB writes; account price |
| E16 | `_persist_funded_entry` (qty + RM safeguard stop onto the row), protective-leg qty stamp | `:3419`, `:1961` | DB writes |
| E17 | `_submit_funded_entry_with_retry`: `reconcile_protective_stop` (ruleset SL vs RM safeguard, tighter wins), `stamp_safeguard_anchor`, `account.submit_order(order, sl_price=...)`, `record_max_loss_stop`; on failure `_fail_unsent_entry` | `:3477`, `:2016-2080` (`position_sizing.py:356`, `trade_cycle.py:245,270`) | account price, broker submit |
| E18 | post: `account.refresh_orders(fetch_all=True)`, `_check_all_waiting_trigger_orders` | `:3505-3530` | broker (lifecycle, NOT a decision) |

Early returns that are NOT decisions: lock miss (E0), pending analysis jobs (E3). Early returns that ARE decisions
worth explaining: automation off, no ruleset, no recommendations (E3/E5), per-recommendation gates E8/E9/E10.

### 1.2 Open-positions pass (`process_open_positions_recommendations`, `TradeManager.py:3910-4240`)

Same frame (lock, regime publish `:3951`, `allow_automated_trade_modification` `:3974`, `open_positions_ruleset_id`),
then per latest recommendation (preferring OPEN_POSITIONS-subtype rows, `:4012-4040`): book =
`entry_order.positions_to_manage` (`:4087`, reads `account.has_pending_closing_order`), `oldest_entry_order` (`:4119`),
`evaluator.evaluate` (`:4131`), `evaluator.execute(submit_to_broker=allow_automated_trade_modification)` (`:4188`).
There is NO separate RM sizing; sizing-like decisions are inside the actions: `CloseAction.execute` (`TradeActions.py:1054`),
`AdjustTakeProfitAction/AdjustStopLossAction.compute_price/execute` (`:1346,1592,1814,2138`),
`Increase/DecreaseInstrumentShareAction` (`:2188,2398`). Their EFFECTS go through five account methods only:
`submit_order`, `close_transaction`, `reduce_transaction`, `adjust_tp`, `adjust_sl` (`AccountInterface.py:272,2372,2290,1790,1795`;
grep of `TradeActions.py`).

### 1.3 Smart RM vs classic RM

Smart RM is an LLM graph (`SmartRiskManagerGraph.py`, queue in `SmartRiskManagerQueue.py`/`WorkerQueue.py:1188`) with its
own table `SmartRiskManagerJob`; it does not call `size_candidate_orders`. Its decisions are not reproducible offline
(model output). V1 records NOTHING for it beyond a pass marker (`smart_rm_task_queued`, job id) and the replay report
shows `unsupported: smart risk manager (LLM)` for those experts. What can be replayed later is its tool-call
effects (they use the same five account effect methods), not its reasoning. Classic RM is `TradeRiskManagement`;
the options RM (`OptionRiskManagement.py`) is covered by the option record.

### 1.4 Backtest counterpart (for parity, section 4)

`DailyBacktestEngine` (`testplatform/backend/app/services/backtest/daily_engine.py`) calls the SAME classes:
`TradeActionEvaluator.evaluate/execute` (`:1589`, `:1912`), the shared `trade_cycle` helpers (`:1676`, `:2033`),
`entry_order.positions_to_manage` (`:2013`), `TradeRiskManagement.size_candidate_orders` (`:2448`, with
`as_of=` the simulated bar, `indicator_provider=` pandas). The two runtimes differ in the DRIVER (TradeManager vs engine)
and in two details: BT calls `execute(submit_to_broker=False)` then `account.submit_order` (`:2455+`), live calls
`execute()` then `_submit_funded_entry_with_retry`; `record_run` is a no-op in BT. Existing rule-level parity:
`app/services/backtest/parity_harness.py` (fixture of recorded recommendations replayed through the BT evaluator).

## 2. What to record

### 2.1 Taxonomy: one generalised action record, three families

The option record (`option_action_record.py`) is a per-ACTION record: `execute()` is self-contained (select + size +
submit). A stock decision is a PIPELINE across steps with a batched RM in the middle, so the unit is a STEP, but
the mechanics are the same: inputs sufficient to rebuild the real shared code, the DB rows it reads, the broker reads on the
existing tape (window = `tape_span`), the outcome including effects. So:

* Extract the family-neutral core of `option_action_record.py` into `ba2_common/core/replay/action_record.py`:
  the thread-local frame, `note_submit`/`note_record` -> generic `note_effect(method, **call)`, `build_outcome`,
  `replay_effects`, `_encodable`, `_is_secret`, `db_row_instances`, `_dump`, and the `tape_span` / decision-time
  protocol (payload shape section 2.3). Keep `option_action_record` as the OPTION FAMILY on top of it (same module
  name, same public names, same `RECORD_VERSION = 1` payloads, provider `"option_action"`), so the 4 existing
  recordings and `option_selection.py` keep working unchanged.
* New stock family `ba2_common/core/replay/decision_record.py`, provider `"decision_step"`, `RECORD_VERSION = 1`,
  method = step kind: `rules`, `size`, `execute`. Same observation mechanism (`record_observation`), same
  `tape_span`, so `option_selection.action_window`/`SpanTape` (`:117,506`) are reused as they are.
* Coordination: the option clock fix (sibling branch rp-a) edits `option_selection.py`; extract the shared core
  AFTER it merges (Task 2) so the move is a pure refactor with the option byte tests green.

### 2.2 Pass identification (replaces "TradeManager as an expert")

* `_decision_capture_scope` keeps opening a `capture_scope` per pass but the meta becomes explicit:
  `expert_class` = the REAL expert class of the instance (resolved the way `get_expert_instance_from_id` does),
  `use_case` as today, plus `branch_flags = {"record_kind": "decision_pass", "pass": "enter-market"|"open-positions",
  "trigger": "scheduler"|"manual"}`. This stops the pass from masquerading as the expert `TradeManager` in
  inventory (`inventory.py`) and `expert_replay.py:372`.
* New outcome `ReplayStatus.OUTCOME_DECISION = "decision"` (`schemas.py:49`) and an additive `AnalysisRecord.decision_object`
  (hash of a frozen pass summary: pass kind, recommendations considered, per-symbol verdicts, funded set, created
  order ids, errors). `CaptureContext.set_decision(summary)` joins `set_outcome` (`context.py:296`). Bump
  `SCHEMA_VERSION` (`service.py`) with an N-1 reader (audit item 14).
* `capture_scope.__exit__` already does `set_outcome(error=exc)` for an exception (`context.py:716`); the pass body now
  ends with `set_decision` or `set_skip(reason)` on every exit path. Fixes the "analysis ended without a recorded
  outcome" stamp (`context.py:521`) at its source: a pass that ends without one is a recorder bug and is counted as such.
* Session `capabilities` (`replay_capture.py:281`) gains `decision_families: ["stock_rules","stock_size","stock_execute"]`
  next to `recorded_experts`, so a session states what it can and cannot replay.

### 2.3 Taps and records

Pass-level state captured ONCE at scope open (a `pass_state` frozen object, hashed, referenced by every step):
expert instance row, FULL resolved expert settings (this is the audit #5 fix; secrets by key token excluded and
NAMED in `redacted_setting_keys`, same rule as `option_action_record._is_secret`), the enter/open ruleset rows
(`Ruleset`, `EventAction` rows, link rows), the account class/id and class declarations
(`_ACCOUNT_DECLARATIONS`), the capital-holding Transactions (WAITING/OPENED/CLOSING, any symbol) and all their
TradingOrders (`CAPITAL_HOLDING_TRANSACTION_STATUSES`, as in `option_action_record._collect_db_rows`), the cutoff
timestamp and the lookback, and the recommendation set the selection saw (rows, ids, created_at).

Steps (each stores its own `inputs`, `db_rows` DELTA vs `pass_state` where the step mutates the DB, `tape_span`,
`outcome`; payload shape identical to the option record):

1. `rules` (tap at `TradeActionEvaluator.evaluate`, one record per recommendation, both passes; E6 / `:4131`).
   inputs: recommendation fields (id, instance_id, symbol, recommended_action, expected_profit_percent, price_at_date,
   risk_level, time_horizon, confidence, data, created_at, subtype), `ruleset_id`, `existing_order` (exit),
   `existing_transaction_ids`, `instrument_name`. outcome: the returned `action_summaries` (type, config, description),
   `get_evaluation_details()` verbatim (`TradeActionEvaluator.py:1671`: condition and rule evaluations with the
   values each condition read), or the error. Gates after it (E8 dup-position, E9 equity, E10 route) are recorded as
   `gate` verdicts in the same record by a `note_gate(name, passed, detail)` call at the three sites, so "why was a
   passing rule not traded" is answered.
2. `size` (tap at `TradeRiskManagement.size_candidate_orders`, one record per call, enter pass; E13). inputs: the
   candidate list (order dump of the transient order + the recommendation), `option_decisions` presence, the RM constructor
   args (`as_of`, the indicator-provider kind), expert settings (in `pass_state`). outcome: the `funded` list
   (symbol, quantity, stop_price) AND the `decisions` + `context` that `_record_candidate_run` (`:1144`) builds. Tap
   point: a `note_sizing_run(decisions, context)` call inside `_record_candidate_run` BEFORE `record_run`, so the same
   explanation is available in live capture and in a backtest (where `record_run` does nothing). No new sizing code, no
   re-derivation: the RM's own operand/binding trace (`_open_trace`, `_clamp_binding`) is the "binding constraint".
3. `execute` (tap at `TradeActionEvaluator.execute`, one record per call: E15 per funded symbol, `:4188` per exit
   symbol, and E11 option ones are skipped because the option record covers them). inputs: reference to its `rules`
   record (seq), `submit_to_broker`, for entries the RM-sized `quantity` and `stop_price` handed in
   (`:3419`), `lot_size`. outcome: the action results (`success`, `message`, `data`), the rows it CREATED/CHANGED
   (post-execute dumps of the TradingOrder entry, its protective-leg children (the TP/SL staged by the bracket
   logic), the Transaction incl. `take_profit`/`stop_loss`/`meta_data` anchors), and the EFFECTS: ordered list of the
   five account effect calls with arguments (`submit_order(order dump, tp_price, sl_price, is_closing_order)`,
   `close_transaction(id)`, `reduce_transaction(id, qty)`, `adjust_tp/adjust_sl(txn id, price, source)`) and
   each call's return or exception text. For entries the effect list includes the `sl_price` produced by
   `_entry_submit_stop`/`reconcile_protective_stop` (`:1999`), i.e. the final TP/SL handed to the broker.

Effect tap (the missing piece): `AccountInterface.__init_subclass__` wraps the five effect methods exactly like
`OptionsAccountInterface.__init_subclass__` wraps the reads (`option_taps.tap_option_methods` pattern, MRO-resolved,
double-wrap guard, `tap_depth` for nested calls such as `adjust_tp_sl` -> `adjust_tp`). The wrapper is passthrough and
records into the current step frame (`note_effect`) only inside a capture scope. Same guard test as the option taps
(every concrete account, overrides included).

Account READ taps needed by stock steps and not yet tapped: `has_pending_closing_order` (`entry_order.py:85,101`),
`get_fractionable`, and the sizing reads (`get_account_snapshot`, `get_positions`, `get_instrument_current_price`) which
are tapped today for accounts that are `OptionsAccountInterface` (all three live brokers). Task 4 moves the sizing-read subset
of `OPTION_TAPPED_METHODS` into a shared account tap table (provider `"broker"`, unchanged names/identity so old
recordings still resolve) and adds the two missing ones. Requirement: a guard test that greps the decision path
(`TradeConditions`, `TradeActions`, `TradeRiskManagement`, `MarketExpertInterface` balance code, `entry_order`,
`trade_cycle`) for `account.<method>` and fails on an untapped, unallowlisted method (the allowlist is the
effect set and pure id/setting reads).

Clock: `TradeConditions._today`-style reads (`TradeConditions.py:419-431`, and the 13 `datetime.now/date.today`
sites in that file) and the `cutoff_time` sites (`TradeManager.py:3165,4000`) use the wall clock. Route them through
`replay_now(as_of)` (identity outside a capture scope, so live behaviour is unchanged); the pass records ONE clock
read at open (the decision instant) and each step's `clock_start/clock_end` as in the option record.

### 2.4 Failure handling inside the record

* `_decision_capture_scope` returns a scope for EVERY expert whenever a store exists. Early-exit classification at
  scope exit: decision-grade reasons (automation off, no ruleset, no recommendations, all recommendations gated) end with
  `set_skip(reason)` and ARE written (~1 KB). Non-pass reasons (lock miss E0, pending analysis jobs E3) call
  `context.discard(reason)`, which writes nothing but increments `CaptureHealth` kind `pass_discarded` and a
  per-reason counter, so the discard rate is visible and a regression that discards real passes shows up.
* A step recorder failure -> `context.note_failure(...)` (existing, `context.py:572`) and the step is marked
  `recorded: false` with the error in the pass summary; replay reports it `missing_capture` naming the step. Never a
  half-record that passes.
* Unencodable value -> the `unencodable` marker pattern of `_encodable_submit` (replay: `missing_capture`).

### 2.5 Not recorded, and why

* Smart RM reasoning (LLM; section 1.3). The queue/trigger is recorded as a pass marker.
* Post-submit lifecycle: `refresh_orders`, fills, `_check_all_waiting_trigger_orders`, OCO repair, wash-trade expiry (E18,
  `refresh_accounts :512`). These are broker observations, not decisions (report text at `report.py:106`).
* Broker-internal bracket construction inside `AlpacaAccount.submit_order`/IBKR/Tasty: adapter code, covered by their own
  tests; the replay compares what the SHARED code handed over (args, SL/TP prices, quantities), not what an adapter did.
* Credentials: `AccountDefinition` / `AccountSetting` rows are never dumped; only class name, id, declared class
  attributes. Expert settings pass the `SECRET_KEY_TOKENS` filter (`observe.py`). API keys never enter `pass_state`.
* Pre-analysis balance-gate skips and FAILED analyses (audit #7): a separate item (analysis-side), not part of this design.
* Basket experts (FactorRanker, Senate): their analysis side is unrecorded (audit #7); their passes are recorded here like
  any other expert's, and replay rows for them say `unsupported: analysis not recorded` only for the expert-input stage.

## 3. Replay design

CLI: `ba2-test replay decision --bundle <dir> [--expert-instance N] [--symbol S]` (peer of `option-selection`,
registered where `ba2test_launcher.py` registers it). Implementation `testplatform/backend/app/services/replay/decision_pass.py`,
built from the same parts as `option_selection.py`:

* Tape: `SpanTape` (`:117`) per step window (`action_window :506` extended to the new providers), refusing any read not on
  the tape as a typed `ReplayMiss` -> `missing_capture`.
* Account: generalise `TapeOptionsAccount` (`:160`) into `TapeAccount` (stock reads + the five effect methods that RECORD
  the call and return the recorded return) with the option methods added by the option subclass;
  `_install_refusals` (`:272`) already refuses every un-served method loudly.
* DB: `_scratch_trading_db` (`:363`) seeded from `pass_state` (+ step delta). The REAL evaluator, actions and RM write into it.
* Expert: `build_replay_expert` (`expert_replay.py:73`) builds the real expert class from the recorded full settings; a
  `_ScratchResolver` (`:335`) serves expert, tape account and an indicator-provider shim through the existing
  `set_indicator_provider_resolver` seam (`TradeRiskManagement.py:155`).
* Clock: `CaptureContext.for_replay` with the step's clock span; the pass decision instant is the first read.

Per recorded pass the replay runs, in recorded order, per symbol: `rules` (real `TradeActionEvaluator.evaluate`) ->
gates verdicts -> (entries) one `size` for the batch (real `size_candidate_orders`, candidates rebuilt with the shared
`build_entry_candidate`) -> `execute` per funded symbol (real `evaluator.execute()` on the evaluator rebuilt by the
replayed `rules` step, then the same `_persist_funded_entry`/`reconcile_protective_stop` composition). To keep that
composition ONE function rather than a copy of TradeManager's loop, Task 7 extracts the funded-entry body
(`TradeManager.py:3397-3480` minus broker retry) into `ba2_common/core/trade_cycle.py` (`stage_funded_entry(...)`) and both
TradeManager and the replay (and later the BT engine) call it. A dependent step whose predecessor is `missing_capture`
is `missing_capture` ("depends on rules step seq N"), never skipped.

Stages and comparison (EXACT equality, no tolerance, like the option replay; a numeric difference reports the delta via
`FieldDiff`, `report.py:~170`):

| Stage row (report) | Compared | Source |
|---|---|---|
| Selection | recommendation set after cutoff/HOLD/latest-per-symbol/sort, from the recorded rows | `pass_state` (pure re-computation) |
| Rules | action summaries; every condition evaluation (type, result, the values it read); rule `executed`/`all_conditions_met`; gate verdicts | `rules` record |
| Sizing | funded set; per symbol quantity and `stop_price`; the run `decisions` (outcome, reason, operands, binding) and `context` figures | `size` record |
| TP/SL | transaction `take_profit`/`stop_loss` and anchors after `execute`; protective-leg rows; the `sl_price` handed to `submit_order` | `execute` record |
| Order build | order row fields (side, type, quantity, limit, stop, comment excluding ids/timestamps), transaction row | `execute` record |
| Submit / refusal | ordered effect calls with arguments; or the refusal (`success=False`, message, error repr), `_fail_unsent_entry` path | `execute` record |

`report.py`: `STAGE_ROWS` (`:~115`) gets "Selection", "Rules and sizing" split into Rules / Sizing / TP-SL and "Execution"
split into Order build / Submit, all filled by `ReplayStatus.CAPABILITY_DECISION` (already declared, `schemas.py:82`,
`report.py:56,106`, currently produced by nothing). `merge_coverage` writes coverage rows with that capability.
`replay inventory` counts a pass by `record_kind` instead of as an `expert_class` analysis; `replay experts` skips
`decision_pass` rows (no more `unsupported` noise); `CAPABILITY_NOTES` text stays: broker fills are not promised.
A pass that is partly unsupported (Smart RM) is reported per step, never as a green pass.

Miss reporting: every miss names (pass id, step, kind: `clock_read` / observation identity / db row / un-tapped method)
exactly as `_miss_text` does for options. `tape.misses` non-empty after a step that "swallowed" it is the finding
(`option_selection.py:597-600` pattern). An un-tapped account method during replay raises through `_install_refusals`.
Code drift (audit #6): the report header prints recorded vs running `source_revision` and marks every difference
`code_drift` candidate when they differ; a decision replay is only meaningful on the recording-era code or a deliberate
comparison across revisions, and the header says which.

## 4. Backtest parity

Principle (owner rule): one shared function, two callers. What is already shared: evaluator, conditions, actions, RM,
`trade_cycle` (section 1.4). What the decision replay adds is a second CALLER of those same functions fed recorded inputs,
and that is the live-vs-shared-code proof (stage "Rules/Sizing/Execute match"). The next step, the comparison with
`run_daily_backtest` (`daily_backtest_handler.py:966` -> `DailyBacktestEngine`), is deliberately LATER:

1. Prerequisite A: the normalised stage views (`rules_view`, `sizing_view`, `execute_view`, section 3 table) become pure
   functions in `ba2_common` (Task 6) used by the recorder, the replay AND the BT engine, so "same" means the same bytes.
2. Prerequisite B: the BT engine's driver is brought onto the same composition: it calls `stage_funded_entry` (Task 7) and
   emits the same step records through the same taps when a capture scope is open (a replay-capture scope inside a backtest
   is in-memory: no store needed, the engine just collects the views). Today the engine duplicates TradeManager's loop
   (`daily_engine.py:2448-2480` vs `TradeManager.py:3380-3480`) and passes `submit_to_broker=False`; that asymmetry is
   named, not fixed here.
3. Then `ba2-test replay decision-vs-backtest`: seed a `BacktestAccount` (tape-fed balances, positions, prices from the
   recorded broker reads, scratch DB from `pass_state`), feed the RECORDED recommendations to `_stage_recommendation_candidate`
   + `_size_and_submit_candidates` (`daily_engine.py:1536,2429`) and compare views. A difference there is by definition a
   driver-loop divergence, since inputs and shared code are identical.
4. Not claimed: that the BT's OWN data (cache prices, `analyze_as_of`) reproduce live inputs. That is the existing
   `historical` capability and the decision price semantics differ by design (CLAUDE.md "Prices in decision code": BT = close
   of the last ended bar, live = broker bid), so the comparison with the backtest's own data is a measured drift report
   (`FieldDiff` deltas), not a pass/fail.

## 5. Failure policy and visibility (all three apps)

* Never a dependency: every tap and the scope are wrapped so an exception inside recording cannot reach the pass; the pass
  result is identical with capture on or off (a test runs the same pass both ways and compares created orders).
* Never silent: new `CaptureHealth` kinds (`context.py:100`): `pass_not_recorded` (scope could not open / no session / resolver
  or account lookup failed), `pass_discarded` (with reason counters), `step_not_recorded`. Counted at the exact sites that
  today return `nullcontext()` (`TradeManager.py:2924,2929,2935,2939-2948`), each with ONE `logger.error` per pass.
* Evidence in the store, not only in a log: a failed-to-open pass still submits a minimal `AnalysisRecord`
  (`record_kind=decision_pass`, outcome `error`, error "pass not recorded: <why>") when a store exists; when no session is
  open the counter is the only evidence and `finalize_session` copies `CaptureHealth` into `capabilities.capture_health`.
* Surfaces: `replay_capture.get_capture_health()` (`:205`) has no consumer outside a test today; wire it into the existing
  ops/health endpoint (`ui/api_routes.py`) and into `replay inventory` (red header when any counter is non-zero).
* Independent witness for coverage: `replay inventory` adds a "decision coverage" line per day: `RiskManagerRun` rows
  (classic entries, written by live regardless of capture) and `TradeActionResult` rows vs recorded `size`/`rules` steps. A
  mismatch is a coverage gap shown by number, the same idea as the proposed daily option-action coverage report (audit #2).

## 6. Tasks (TDD; one commit each; targeted pytest; no version bumps, the coordinator bumps at merge)

Files: A = `packages/common/tests/replay/`, B = `testplatform/backend/tests/replay/`, C = `tests/` (root).

1. Measure first (no behaviour change). Counter-only dry run behind `BA2_REPLAY_DECISION_DRYRUN=1`: per pass, bytes of
   `pass_state` + steps if they were written. Test C `test_decision_pass_sizing_counter.py`. Gate: replaces the estimates in
   section 0 before Task 5 ships.
2. Extract `action_record.py` core from `option_action_record.py` (after rp-a merges). Tests A `test_action_record_core.py`;
   the existing option record/replay tests stay green byte-for-byte (B `test_option_selection.py`).
3. Clock seams: `TradeConditions` `_today` and the 13 now-sites, `TradeManager` cutoffs, through `replay_now`. Failing test
   first: a condition evaluated under `for_replay` returns the recorded day. Tests A `test_trade_conditions_clock.py`; BT byte
   gate on one stock backtest row before/after (as the option design did).
4. Account tap tables: shared sizing-read table + `has_pending_closing_order`, `get_fractionable`; effect taps on the five
   methods via `AccountInterface.__init_subclass__`. Tests A `test_account_taps_guard.py` (every concrete account,
   overrides included), `test_effect_tap_nested.py` (`adjust_tp_sl` -> `adjust_tp` recorded once).
5. Schema + scope: `OUTCOME_DECISION`, `decision_object`, `set_decision`, `discard`, health kinds, `SCHEMA_VERSION` bump
   with N-1 reader; `_decision_capture_scope` rewrite (every expert, pass meta, counted failures, no `nullcontext` on
   error). Tests A `test_decision_outcome.py`, C `test_decision_capture_scope.py` (stock expert gets a scope; lock miss
   discards and counts; resolver failure on the exit pass records `pass_not_recorded` and still runs the pass).
6. Records: `decision_record.py` (`rules`, `size`, `execute`, `pass_state`), `note_gate`, `note_sizing_run` in
   `_record_candidate_run`, pure view functions (`rules_view`, `sizing_view`, `execute_view`) in `ba2_common`. Tests A
   `test_decision_record.py` (payload shape, secret redaction: a setting named `*_api_key` is absent and named in
   `redacted_setting_keys`; unencodable marker), C `test_decision_pass_capture_roundtrip.py` (drive the real
   `process_expert_recommendations_after_analysis` against a fake account with capture on; assert the pass ends `decision`,
   not `error`, and the created orders equal those of a capture-off run).
7. Extract `stage_funded_entry` into `trade_cycle.py` and call it from `TradeManager` (live behaviour pinned by the existing
   TradeManager tests; add one characterisation test first). Tests C `test_stage_funded_entry.py`.
8. Replay: `TapeAccount`, `decision_pass.py`, CLI `replay decision`, report stage rows + `CAPABILITY_DECISION` coverage,
   inventory `record_kind`, `experts` skip. Tests B `test_decision_pass_replay.py`: round trip enter pass (rules, size, execute
   all match); edited ruleset condition -> Rules difference; deleted `get_account_snapshot` observation -> `missing_capture`
   naming the step; changed `max_virtual_equity_per_instrument_percent` -> Sizing difference with delta; dependent execute
   `missing_capture` when its rules step is; exit pass round trip (close, adjust_sl); a real exported 8080 bundle once Task 6
   has run for a trading day.
9. Visibility: health into the ops endpoint, `capabilities.capture_health` at finalize, inventory decision-coverage line.
   Tests C `test_capture_health_surface.py`, B `test_inventory_decision_coverage.py`.
10. LATER (separate plan): BT parity (section 4), Smart RM tool-call effects, retention/GC, basket experts' analysis capture.

Order: 1, 2, 3, 4 can run in parallel pairs; 5 before 6; 7 before 8; 9 any time after 5. Roll out 5+6 to 8080 first
(dev), one full trading day, run Task 8's replay on the exported session, then 8082, then 8081.

## 7. Risks and open questions for the owner

1. Prod 8081 gets a recorder on the live order path. Mitigation is the capture-on/off equality test plus the 8080-first
   rollout. Decision needed: record prod immediately or only after one clean 8080 week?
2. Wall-clock reads in conditions (Task 3) are the largest replay-correctness risk; the 13 sites need individual review (some
   use `as_of`, some `created_at or now()`). If one is missed, replay shows `clock_read` misses, not silent passes.
3. Un-tapped account methods in the decision path are the likely long tail (Task 4 guard finds them). Each new tap changes
   what an older recording can answer; old sessions replay as `missing_capture` for stock steps by design (nothing retroactive).
4. Expert-class-specific balance logic: replay builds the real expert via `__new__`; classes overriding
   `_get_enabled_instruments_config`/balance methods may read state not in `pass_state`. First pass supports the four recorded
   experts + DeterministicScorer instances; other classes report `unsupported` per step.
5. Size: DeterministicScorer instances with many open positions may push `pass_state` to hundreds of KB per pass. Option:
   store each Transaction/Order row as its own content-addressed object so unchanged rows dedupe across passes (store is
   already content addressed). Decide after Task 1 numbers.
6. Exactness: exact float equality across Python/pandas versions is assumed for sizing (ATR); a worker/host with different
   numpy could show 1-ulp differences. Policy proposed: still exact, report the delta, never a tolerance (matches the option replay).
7. Sibling-branch overlap: `option_selection.py` (rp-a), full-settings recording (rp-b, same `meta["settings"]` seam,
   `MarketExpertInterface.py:1542`), provenance (rp-c, `ProviderObservation` fields). The schema bump in Task 5 should be
   ONE bump shared with them, decided by the coordinator.
8. Discard policy (E0/E3) hides nothing only if the counters are watched; confirm the owner wants the counter on the ops endpoint
   rather than a persisted "discarded" row per call (~300/day would be noise).
9. Not verified here: whether `BacktestAccount` exposes the same effect methods with identical signatures (needed for Task 4
   guard and section 4); the exact set of sites in `TradeConditions` that call the wall clock beyond the 13 counted by grep.

## 8. Implementation notes (Tasks 1, 5, 6 as built, 2026-11-08)

Where the build departs from, or pins down, the text above:

* Gates (`note_gate`): a gate verdict is taken AFTER the `rules` step has closed, so it cannot join that record. It is listed per
  symbol in the pass' frozen summary (`decision_object`: `symbols[SYM].gates`, with `rules`, `size`, `execute` digests, the funded
  set, created order ids, swallowed errors and `steps_not_recorded`); the replay's "Rules" stage reads it from there.
* `execute` step: it spans execute + persist + broker submit for an entry (so the `submit_order` effect and the final stop price are
  inside it) and execute for an exit. It is opened at the two TradeManager sites (the funded-entry loop and the exit execute), not
  inside `TradeActionEvaluator.execute`, because only those callers compose the persist/submit tail. `rules` and `size` are taps in
  the shared `TradeActionEvaluator.evaluate` / `TradeRiskManagement.size_candidate_orders` (passthrough outside a capture scope).
  Option actions are skipped (the option action record covers them). Task 7 moves the entry composition into `trade_cycle`.
* The exit pass' capture scope now opens in the public `process_open_positions_recommendations`, before the lock and the early
  returns (the body moved to `_process_open_positions_recommendations`), so lock misses are counted discards and automation-off /
  no-ruleset exits are recorded skips. The enter pass is wrapped the same way. The market-condition scope still opens inside it.
* `pass_state` is written when the pass reaches evaluation (after the recommendation selection), not at scope open: the
  recommendation set it must hold does not exist earlier. Per-step payloads store the normalised views (`rules_view`,
  `sizing_view`, `execute_view` in `decision_record`) as their `outcome`, so the replay compares with the same functions.
* Host timezone: `branch_flags["host_timezone"] = {name, utc_offset_seconds}` on every pass and `capabilities["host_timezone"]` on
  the session. `TradeConditions._wall_today()` under replay takes the calendar day of the recorded instant in that zone (the fixed
  offset when the host could not name its zone) and raises `ReplayMiss` when the record has none.
* Schema 3 (`OUTCOME_DECISION`, `AnalysisRecord.decision_object`, health kinds `pass_not_recorded` / `pass_discarded` /
  `step_not_recorded`; schemas 1 and 2 still load). `CaptureHealth.total` excludes discards (an accounted-for outcome).
* The replay-side `experts`, `gather-tape` and `historical` capabilities skip decision-pass rows (Task 8 gives them their own).
* Task 7 as built: `trade_cycle.stage_funded_entry(evaluator, symbol, quantity, stop_price, on_executed=...)` is execute + detached fetch of
  the main order + `persist_funded_entry` + `stamp_leg_quantities` (the live-only leg stamp stays inside it: the backtest account builds
  its legs at fill time). TradeManager calls it and keeps the broker tail (`_submit_funded_entry_with_retry`, `_fail_unsent_entry`);
  `_persist_funded_entry` / `_row_or_none` delegate to the shared `persist_funded_entry` / `row_or_none`. The backtest engine's own
  loop (`daily_engine.py:2448+`) is NOT changed (section 4 is later): it still has its own copy of the composition.

## 9. Implementation notes (Task 8 as built, 2026-11-08)

* Size cut (section 0): rows are `SharedValue` objects (`codec.py`, tag `$ref`), steps reference recommendations by hash, `execute` carries no raw
  `rows_after`; the replay reads the new shape.
* `testplatform/backend/app/services/replay/decision_pass.py`; CLI `ba2-test replay decision --bundle <dir> [--out <dir>]`; report capability
  `decision` (`CAPABILITY_DECISION`), coarse stage rows Selection / Rules and sizing / Execution plus the finer Decision rules / sizing / TP-SL /
  order build / submit-refusal rows (only on a decision report); the inventory counts passes by `record_kind` (`analyses_by_record_kind`,
  `decision_passes`) and the decision capability's `not_run` is owed by decision passes only; `experts` / `gather` / `historical` skip them.
* `TapeAccount` is a SUBCLASS of `TapeOptionsAccount` (not a generalisation of it): the option replay is untouched. It serves the sizing reads
  (`get_balance`, `get_account_info`, `get_account_snapshot`, `get_positions`, `get_instrument_current_price`, `has_pending_closing_order`,
  `get_fractionable`) from the tape, `get_account_snapshot` / `get_positions` from whichever of `broker_options` / `broker` holds them, and answers
  the six effect methods with the return live recorded (recording each call in the live effect shape). A call the recording does not hold in that
  order is refused as `UnrecordedEffect` and shows as a Submit difference.
* Gaps found while building it (fixed here, all in the taps): `get_balance` is a broker call of its own at Alpaca / TastyTrade (the margin-off
  tradable balance) and `get_account_info` is the buying-power clamp's fallback; neither was tapped, so a stock pass could not have replayed. Both
  are in `SIZING_READ_TAPPED_METHODS` now, and the account guard also matches `getattr(account, "name", ...)` spellings. `pass_state` also records the
  account's non-secret settings (`margin_enabled`, `margin_factor`, `minimum_equity_threshold_percent`, ...; credential-shaped keys named in
  `redacted_account_setting_keys`).
* Funded-entry composition is shared: `trade_cycle.stage_funded_entry` (execute, detached order fetch, persist RM quantity + stop, stamp leg
  quantities) and `trade_cycle.submit_funded_entry` (the stop reconciliation, anchor stamp, `submit_order`, max-loss record, DB-lock retry), plus
  `select_enter_candidates`. TradeManager calls them; so does the replay. What is still two copies: the enter loop's ORCHESTRATION (select, evaluate,
  gates, candidate, size, funded loop) in `TradeManager` and in `_PassReplay._enter`, and the backtest engine's own loop.
* Comparison scope (named, not hidden): effects are compared with ids / timestamps / broker ids removed and transaction ids as ordinals; the order
  view keeps only the main orders (protective legs are built by the broker ADAPTER) and the transaction view drops only the fields the adapter writes from
  broker state (`meta_data`, `status`, `quantity`, `open_price`, `close_price`, `created_at`); `take_profit` / `stop_loss` ARE compared (section 10); the TP/SL stage compares the prices handed to the account.
* Not replayed yet: rules that read market-data providers (OHLCV / indicator / fundamentals conditions) report `missing_capture` because the
  isolation's provider resolver refuses and no tape-backed provider is wired for them (the RM's ATR read IS served from the tape when live recorded
  one); option entries/exits; Smart RM passes; the unsent-entry compensation path.
* The tests are in the ROOT suite (`tests/test_decision_pass_replay.py`, trade venv): driving the real TradeManager needs the root harness and
  `langchain`, which the testplatform test venv does not have (`ModelFactory` import). They replay through `ba2test_launcher._cmd_replay`.

## 10. Review fixes (2026-11-08, branch sdr/fix)

* C1: the funded-entry loop binds `order` the moment `stage_funded_entry` knows it (the `on_executed` hook), BEFORE the persist, so a
  failing `persist_funded_entry` / `stamp_leg_quantities` still reaches `_fail_unsent_entry` for THAT order (as before the refactor).
* I1: an account effect inside a pass but outside every step frame (dependent TP/SL legs from `_check_all_waiting_trigger_orders`,
  compensation, refresh) is kept in the pass summary's `effects_outside_steps` (capped at 200; beyond that counted `step_not_recorded`).
* I2: a full queue / closed store hands the drop to the writer (`_drops`), never writes the index from the pass thread; with no live writer the
  drop is written with a 250 ms index wait and counted/logged on failure.
* I3: per-pass budget (`PASS_RECORD_SECONDS_BUDGET` 30 s of recorder time, `PASS_RECORD_FAILURE_BUDGET` 25 failures): later steps of that pass are
  not recorded, each counted `step_not_recorded` and listed in `steps_not_recorded`; the summary says why (`recording_budget_exceeded`). Rollout
  allow-list: AppSetting `replay_decision_pass_experts` (comma-separated expert classes; absent / blank / `*` = every expert, the default when capture
  is on; the row is not auto-created). Experts not listed are counted as discards with reason `expert_not_allowed`. An excluded expert also loses its market-condition window and option-action capture (they are read inside the same pass scope).
* I4: created-order-id bookkeeping runs inside `_finish_decision_pass`'s guard; the pass' return value always reaches the caller.
* I5/I6/I7: the replay gates every payload `version` (`unsupported` naming it); `take_profit` / `stop_loss` of the transaction are compared (the tape
  account stamps handed prices as the adapters do, only where the recorded transactions show the adapter stamped them); pass gaps use the `decision`
  capability and per-step payload gaps are named in `steps_not_recorded`.
* M4 (limit, not fixed): the capture context is a `ContextVar`, so account effects made on OTHER threads (worker threads spawned inside a pass,
  the IBKR runtime's asyncio thread, broker callback threads) do not see the pass and are NOT recorded. The shared decision code calls the account on
  the pass thread, which is what the tap records; adapter-internal threads are adapter behaviour (section 2.5).
* M6: `BA2_REPLAY_DECISION_DRYRUN` is ignored (one ERROR) outside pytest unless `BA2_REPLAY_DECISION_DRYRUN_CONFIRM=dev` is also set.
