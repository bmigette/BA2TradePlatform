# Option capture + selection replay: design

Status: validated with the owner 2026-11-05 (three parts, each approved). Plan:
`docs/plans/2026-11-05-option-capture-replay-plan.md`. Builds on the stock capture/replay
(`docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md`).

## Why

Live already records each option trade's `option_trade_record_v1` (legs, quotes, greeks, spot) and
the stock-side expert inputs, but NOT the option chain / quote reads that choose and price the
contract. So no live option decision can be re-run offline, and the replay report marks
"Rules/sizing/Execution" `not_run`. Goal: prove that the SHARED option action code (the code the
backtest runs) picks the same contracts, limit price and quantity as live did, when fed the
broker's real data. Out of scope: proving ThetaData parquet equals the broker (a full replay BT
provider; not chosen).

## Part 1: what is recorded

* **Taps** (the existing `observe_provider` pattern: passthrough, record only inside an active
  capture scope, never raise into the caller):
  * option market data on `OptionsAccountInterface`: `get_option_chain`, `get_option_quote`,
    `get_atm_implied_volatility`, `get_option_underlying_price`;
  * sizing reads made by the option actions: `option_capital_equity`, `get_account_snapshot`,
    `check_option_buying_power`, `get_positions`, `check_short_put_assignment_capacity`.
  Concrete accounts (Alpaca, IBKR, Backtest) OVERRIDE these methods, so a decorator on the
  interface alone would be lost: `OptionsAccountInterface.__init_subclass__` wraps every override.
  Request identity = call arguments only; time-derived keys through `replay_now`.
* **Scopes.** The enter-market pass opens its capture scope only when the expert has a
  market-condition profile; widen to "or the expert's account is an options account". The
  open-positions pass gets the same scope (it has none, so exits/rolls/TP-SL record nothing).
* **Per-action record** at `_OptionEntryAction.execute` and `CloseOptionAction.execute`: the
  recommendation fields, the action config (EventAction params), the expert settings read, the
  decision clock, the DB rows the action reads (ExpertInstance + settings; for exits the
  transaction's TradingOrder rows), and the outcome (legs, quantity, limit price, entry/exit
  record) or the refusal reason.
* Live effect: none on decisions; disk only (size per pass measured on 8082; the store has no GC,
  already an open follow-up).

## Part 2: the replay (`ba2-test replay option-selection --bundle <dir>`)

Inside the existing offline isolation: a tape options account answers every tapped read by exact
request identity (the taps' own identity functions, imported); a read not on the tape is a typed
`ReplayMiss` -> `missing_capture` for that action, never a fallback. A scratch in-memory DB is
seeded from the record. The real shared action class is rebuilt from the recorded config and
recommendation and run under the recorded clock; the tape account captures
`submit_option_order` / `close_option_position` instead of sending. Exact comparison (no
tolerance), per stage: selection (contracts, sides, ratios, expiry), pricing (limit), sizing
(quantity), record (entry/exit record key by key), refusal (same reason). The report header lists
the capabilities NOT run (historical parquet vs broker).

## Part 3: errors, tests, rollout

* Recorder never raises; failures go to `CaptureHealth` + one ERROR per pass. `ReplayMiss` joins
  the never-absorb list so `absorb_if_benign` cannot swallow it.
* Tests: tap-marker guard over every concrete options account (overrides included); capture ->
  export -> replay round trip for one entry and one exit (all stages match); a deleted chain
  observation -> `missing_capture`; an edited strike rule -> selection mismatch; byte-identical
  backtest gate on an existing option backtest row (before/after this branch).
* Rollout: branch `feat/option-capture-replay`; ba2_common PACKAGE_VERSION + APP + TEST bumps;
  ba2_common MINIMUM NOT raised (taps cannot change GA results; the byte gate proves it), so the
  push does not re-sync workers. Push and the 8081/8082 restarts need the owner's go. First
  real check: one 8082 trading day, then `ba2-test replay option-selection`.
