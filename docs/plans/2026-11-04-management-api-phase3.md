# Management API phase 3: the write endpoints

Date: 2026-11-04. Spec: `2026-11-04-management-api-design.md` (section 2 Writes, section 3). Builds on
`2026-11-04-management-api-phase1-2.md` (auth, `AuditedRoute`, error envelope, `services/`).

Prod trades live money. The rule for every task: **the API write does exactly what the equivalent UI action does,
no more.** So each write is extracted from the page into `services/` (one function, two callers) and the page is
switched to call it; a parity test drives both callers against a call-recording fake account.

## Tasks

1. **Broker write errors, expert lock, dry-run plumbing.**
   - `services/accounts.py::broker_write(account_id, what, fn, ...)` + `map_broker_error(exc, ...)`: OSError ->
     `BrokerUnreachable` 503; SDK `APIError`-family and `ValueError` -> `BrokerRefused` 502; class names only.
     (`broker_read` maps ValueError to `Unmeasurable`; for a write that is wrong, hence a separate helper.)
   - `TradeManager.hold_expert_locks(expert_id, timeout)`: context manager taking BOTH per-expert processing locks
     (`expert_<id>_usecase_enter_market` and `..._open_positions`, the ones the passes take) in a fixed order,
     waiting up to `timeout` seconds. `services/_locks.py::expert_write_lock(expert_id)` wraps it, raising
     `Conflict` (409) on timeout. A transaction without `expert_id` takes no lock.
2. **Idempotency-Key + audit redaction** in `api/audit.py` / new `api/idempotency.py`.
   - In-memory, thread-safe store, TTL 10 min, key scoped by `(method, path, key)`, request-body fingerprint.
     Repeat with the same fingerprint returns the stored 2xx response (`Idempotent-Replay: true`); same key with a
     different body is 422; a repeat while the first is in flight is 409. Only 2xx responses are stored; dry runs
     neither read nor write the store.
   - `_arguments()` scrubs the parsed JSON body with `services._scrub.scrub` (key-based and pattern-based), and a
     route may add definition-flagged secret keys through `request.state.audit_secret_keys`.
3. **TP/SL**: extract from `live_trades._update_position` / `_revert_tpsl_manual_override` into
   `services/trades.py`: `tp_sl_context` (txn + account, first-order lookup as the page), `validate_tp_sl`
   (`TransactionHelper.validate_tp_sl_prices` against the current price, open price when unreadable, as the page),
   `plan_tp_sl` (pure: the branch matrix tp/sl x changed/removed -> list of steps), `apply_tp_sl` (persists the
   transaction values, runs the plan, returns messages + the exception when a call raised), `clear_override_locks`.
   API entries `set_tp_sl` (guards, dry run, lock semantics) and `unlock_tp_sl`. The quantity-change branch stays in
   the page (the API has no quantity edit).
4. **Close**: extract `_close_position` / `_retry_close_position` into `prepare_close` (guards, orphan branch,
   account lookup) + `run_close` (`await account.close_transaction_async`). API entry `close_transaction`.
5. **Cancel**: `cancel_order` (guard, `account.cancel_order(order.id)`). The UI has no single-order cancel action
   (it only cancels TP/SL legs inside the TP/SL flows), so this service has one caller; the TP/SL
   "cancel all standing legs" step is the shared `_cancel_standing_tpsl`.
6. **Experts**: `update_expert` (`enabled`, `virtual_equity_pct`), `update_expert_settings`, `run_expert`.
   `_refuse_unserved_market_gates` moves from the settings page to `services/experts.check_market_gates` (the page
   delegates), because enabling an expert or changing `market_condition_profile` must be refused exactly as the
   dialog refuses it. The page's "refresh scheduled jobs" tail becomes `services/experts.refresh_expert`
   (shared). `/api/run-schedule`'s body becomes `run_schedule(expert_id, subtype_text, dry_run)` in
   `ui/api_routes.py` (both paths call it).
7. **Routes** (`api/transactions.py`, `orders.py`, `experts.py`; these routers use `AuditedRoute`), `?dry_run`,
   guard test over the route table (every non-GET route audited, token-required, covered by a test), and the
   parity test (UI path vs API path -> same call sequence).
8. Version bump, repeat runs, `tools/check_package_versions.py`.

## Decisions and deviations from the design

- **`lock` and `source`.** The accounts keep the manual-override lock keyed on `source == "manual"`; any other
  source is silently dropped on a locked side (returns True). The design says API calls pass `source="api"` AND
  support `lock`. So: `lock: true` -> `source="manual"` (the call the dialog makes, sets the lock); otherwise
  `source="api"`, and if a side being changed is currently locked the request is refused with 409 (never the
  silent no-op). The audit row always says `source: api`.
- **Partial bodies.** An omitted side is untouched (the resulting state is validated and applied exactly like the
  dialog's full-state submit). Removing a leg is not offered in v1; `null` is a 422.
- **Guards added for the API only**: tp-sl 409 on CLOSING/CLOSED/FAILED transactions. Close gets 409 on CLOSED
  for both callers (the page has no button for it).
- **Dry run**: returns the planned account calls and the standing TP/SL legs the account method may replace; the
  exact replacement is the account's decision and is not simulated.
- **Orphan close** (transaction with no orders) writes `account_id=1` into the close log exactly as the page does.
  Kept for parity; see open questions.
- Overview page keeps its own copies of the TP/SL branching (`overview.py` ~3995, 4266-4358). Not touched: out of
  scope (live_trades only). They should move to the same service later.

## Open questions

1. `overview.py` duplicates the TP/SL matrix; switch it to `services.trades.apply_tp_sl` in a follow-up.
2. The orphan-close branch hard-codes `account_id=1`.
3. `validate_tp_sl` falls back to the transaction's open price when the current price cannot be read (page
   behaviour, at odds with the no-fallback rule).
4. The settings page save (one form, ~40 widgets) is not extracted; only the shared primitives are.
