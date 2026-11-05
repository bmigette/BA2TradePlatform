# Option capture + selection replay Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Record the option chain/quote/IV and sizing reads of live option entry and exit actions, and
replay each recorded action offline through the SHARED action code, comparing contracts, limit,
quantity and trade record exactly.

**Architecture:** Read the design first: `docs/plans/2026-11-05-option-capture-replay-design.md`. The
stock capture/replay stack is the template; read the module docstrings of
`packages/common/ba2_common/core/replay/{observe,context,service,store,codec}.py` and
`testplatform/backend/app/services/replay/{gather_tape,expert_replay,isolation,report}.py` before
coding. Taps reuse `observe_provider`; the replay reuses `replay_isolation`, `load_bundle`, `ReplayMiss`
and the report module.

**Tech Stack:** Python 3.11, SQLModel/SQLite, pytest.

**Worktree:** `C:\Users\basti\Documents\dev\BA2-optreplay`, branch `feat/option-capture-replay` (from
origin/dev 36c83f05). Never touch the main clone `C:\Users\basti\Documents\dev\BA2TradePlatform`.

**Test commands (worktree root; ONE pytest at a time; never mix suites in one invocation):**
- ba2_common: `C:\Users\basti\ba2-venvs\trade\Scripts\python.exe -m pytest packages/common/tests/<file> -q`
- root (host, live code): `C:\Users\basti\ba2-venvs\trade\Scripts\python.exe -m pytest tests/<file> -q`
- testplatform backend: `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/replay/<file> -q`
  (run `tests/replay` and `tests/backtest` as SEPARATE invocations).
Run targeted files only, never the full suites.

**House rules that bite here:**
- Configuration: explicit dict access, no `.get(key, default)` hiding a missing key.
- No silent failure: a recorder failure goes to `context.note_failure(...)` (CaptureHealth); a replay
  gap is a typed `ReplayMiss`, never a fallback or an empty result.
- `ba2_common.logger` has `propagate=False`: assert logs by monkeypatching the module logger.
- Never kill python processes broadly. Do not push. Do not restart any app.
- Commit trailers on every commit:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01F8HzfECLzdvfEbz74Aokc7
  ```

---

### Task 1: Option read + sizing taps, auto-applied to every override

**Files:**
- Create: `packages/common/ba2_common/core/replay/option_taps.py`
- Modify: `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py` (class body, add `__init_subclass__`)
- Test: `packages/common/tests/test_option_replay_taps.py`

**What:** `option_taps.py` defines, for each tapped method, an identity function (arguments only:
`underlying`, `expiry_min`, `expiry_max`, `option_type`, `strike_min`, `strike_max` for the chain;
`contract_symbol` for the quote; `underlying` for ATM IV and underlying price, plus `price_type` when
the signature has it; for the sizing reads, their own arguments) and a table
`OPTION_TAPPED_METHODS: dict[str, (provider, identity_fn)]` covering:
`get_option_chain`, `get_option_quote`, `get_atm_implied_volatility`, `get_option_underlying_price`,
`option_capital_equity`, `get_account_snapshot`, `check_option_buying_power`, `get_positions`,
`check_short_put_assignment_capacity`. Provider name `"broker_options"`. Every identity also carries
`account_class` and `account_id` (as `quote_identity` in `ReadOnlyAccountInterface.py:169` does; copy
its shape, do not import-and-diverge). No identity key may hold `datetime.now()`.

`OptionsAccountInterface.__init_subclass__(cls, **kw)`: for every name in `OPTION_TAPPED_METHODS`
that is defined in `cls.__dict__` (an override) and is not already marked with
`observe.OBSERVED_ATTRIBUTE`, replace it with `observe_provider(provider, name, identity=...)(fn)`.
Methods inherited unchanged are already wrapped where first defined. Skip `abstractmethod`s (keep
the ABC working). `QuoteCachingAccount` (`ba2_trade_platform/core/option_pnl_display.py:58`) wraps
another account: give the class an opt-out attribute `_replay_taps_exempt = True` with a comment
(the inner account records), and honour it.

**Steps:**
1. Failing tests: (a) a fake concrete subclass overriding all nine methods -> each attribute carries
   `OBSERVED_ATTRIBUTE`; (b) inside `capture_scope` (see `packages/common/tests/test_replay_context_clock.py`
   / `test_replay_store.py` for how tests open one) calling `get_option_chain` records exactly one
   observation whose identity equals the expected dict and whose payload round-trips through the codec
   to an equal `List[OptionContract]`; (c) outside a scope the method returns the identical object and
   records nothing; (d) a tap whose identity fn raises -> the value still returns, `CaptureHealth`
   counts one failure; (e) subclass of a subclass does not double-wrap.
2. Run, see FAIL. 3. Implement. 4. Run, PASS. If the codec refuses `OptionContract`/`OptionQuote`/
   `OptionPosition` dataclasses, register them the way the existing `$dataclass` tag registers other
   types (read `codec.py`), test the round trip.
5. Commit `feat(replay): option chain/quote/IV + sizing taps on every OptionsAccountInterface override`.

### Task 2: Guard test over the shipped accounts

**Files:** Test: `tests/test_option_replay_tap_guard.py` (root suite: imports live accounts)

Assert for `AlpacaAccount`, `IBKRAccount` and (import-guarded, testplatform on path) `BacktestAccount`:
every `OPTION_TAPPED_METHODS` name resolves (MRO) to a callable carrying `OBSERVED_ATTRIBUTE`, and no
class in the MRO defines one of them without the marker unless it sets `_replay_taps_exempt`. Commit.

### Task 3: Per-action record for option entries and closes

**Files:**
- Create: `packages/common/ba2_common/core/replay/option_action_record.py`
- Modify: `packages/common/ba2_common/core/TradeActions.py` (`_OptionEntryAction.execute` ~L4417 and
  `CloseOptionAction.execute` ~L6932; read both fully first; find the actual entry/close execute
  methods by `grep -n "def execute"`)
- Test: `packages/common/tests/test_option_action_record.py`

**What:** a context manager `record_option_action(action)` used as the outermost statement of both
`execute` methods. When `current_capture()` is None or replaying, it is a no-op. Otherwise it records
ONE observation (provider `"option_action"`, method = action class name) with identity
`{action_class, expert_instance_id, symbol, transaction_id (exits), recommendation_id}` and payload:
- `inputs`: the action's constructor inputs needed to rebuild it (inspect `TradeAction.__init__` and
  the option subclasses: recommendation fields, the EventAction config dict, account id, expert id,
  existing order/transaction ids), the decision clock read via `replay_now()`;
- `db_rows`: serialized `ExpertInstance` + its settings rows; for closes the transaction and its
  `TradingOrder` rows (the rows the action reads at TradeActions ~L6902/7107/7190/7226). Use
  `model_dump()`; record the model class name per row;
- `outcome`: filled in the `finally` from what the action did: the legs/quantity/limit passed to
  `submit_option_order` / `close_option_position` (capture them by having the context manager install
  a thread-local "last submit" slot that `OptionsAccountInterface.submit_option_order` and
  `close_option_position` write to when a record is open; one line each), `data["entry_record"]` /
  `exit_record`, and the `TradeActionResult` success/message (refusal reason).
Any failure building the record -> `note_failure`, the action result is untouched. The action's own
exceptions propagate unchanged (record outcome `"error"` with the repr, then re-raise).

**Steps:** failing tests with a fake options account + in-memory DB (see existing option action tests:
`grep -ln "_OptionEntryAction\|BuyCallAction" tests packages/common/tests | head`): an entry inside a
capture scope records inputs + outcome legs/qty/limit equal to what the fake account received; a refused
entry records the refusal message; outside a scope nothing is recorded and behaviour is identical
(same result object fields). Implement. PASS. Commit.

### Task 4: Capture scopes for option experts (host, live code)

**Files:**
- Modify: `ba2_trade_platform/core/TradeManager.py` (`_decision_capture_scope` ~L2641; open-positions
  pass ~L3740)
- Test: `tests/test_option_capture_scopes.py`

**What:** `_decision_capture_scope`: open the scope when the expert has a market-condition resolver
**or** the expert's account is an `OptionsAccountInterface` (resolve via the existing account factory
`get_account_instance_from_id`; a failure to resolve -> log ERROR, nullcontext, as the existing
failure branches do). Keep the meta shape; `expert_class` stays `"TradeManager"`, `use_case`
ENTER_MARKET. Add the same scope (factor the body into one helper taking the use case) around the
open-positions evaluation, with `use_case` OPEN_POSITIONS, opened OUTSIDE
`_open_exit_pass_market_condition_scope` (order is load-bearing: see the docstring at ~L2746).
Update the comment at ~L3746 ("No replay-capture scope"). Tests: options expert without a profile ->
scope opens on both passes; stock expert without a profile -> still nullcontext (coverage of stock
deployments must not move); capture off -> nullcontext. Commit.

### Task 5: ReplayMiss is never absorbed

**Files:** Modify `packages/common/ba2_common/core/failure_modes.py`; Test
`packages/common/tests/test_failure_modes_replay_miss.py`. Add `ReplayMiss` to the never-absorb
registry (read how `is_never_absorbed` decides; import lazily if a cycle appears). Test:
`absorb_if_benign` under observe AND legacy modes re-raises a `ReplayMiss`. Commit.

### Task 6: Replay service `option_selection`

**Files:**
- Create: `testplatform/backend/app/services/replay/option_selection.py`
- Modify: `testplatform/backend/app/services/replay/__init__.py` (docstring list),
  `testplatform/backend/app/services/replay/report.py` (capability title + stage rows),
  `testplatform/ba2test_launcher.py` (`_cmd_replay` ~L737 + its argparse: `replay option-selection --bundle <dir> [--out <file>]`)
- Test: `testplatform/backend/tests/replay/test_option_selection.py`

**What:**
- `TapeOptionsAccount(AccountInterface, OptionsAccountInterface)`: built from one action's tape
  (observations of the enclosing pass filtered to `"broker_options"`), answers each tapped method by
  identity lookup using the SAME identity functions from `option_taps.py` (import them); a miss raises
  `ReplayMiss(request)`. Class name and id taken from the recorded identity. `submit_option_order` /
  `close_option_position` store the call and return a synthetic order id; every other broker write
  raises.
- For each `"option_action"` observation: enter `replay_isolation()`, build a scratch in-memory DB
  (look at how `tests/replay/test_market_condition_capture.py` and `historical.py` get a clean DB),
  insert `db_rows`, enter replay mode with the recorded clock (as `expert_replay` does), rebuild the
  action class from `inputs`, run `execute()`, then compare stages exactly:
  `selection` (per leg: contract symbol, side, ratio, expiry), `pricing` (limit), `sizing` (quantity),
  `record` (entry/exit record key by key, list the differing keys), `refusal` (same message).
  A `ReplayMiss` -> status `missing_capture` naming the request; any other exception -> `error`.
- Report: one row per action with the per-stage status; header lists `historical_provider` as NOT run.
**Steps:** TDD with hand-built bundles first (one entry whose tape is complete -> all stages `match`;
remove the chain observation -> `missing_capture`; change the action config's strike/delta param ->
`selection` mismatch). Commit.

### Task 7: Round trip through the real recorder

**Files:** Test `testplatform/backend/tests/replay/test_option_capture_roundtrip.py`

Capture one option ENTRY and one CLOSE with a fake broker-backed options account inside a real
`capture_scope` + store in `tmp_path`, export the session with the existing export service
(`service.py`), run `option_selection` on the bundle -> every stage `match` for both. Commit.

### Task 8: Byte-identical backtest gate

**Files:** Create `tools/option_replay_bytegate.py` (or a scratchpad script; it is a one-off gate,
keep it out of `tools/` unless reusable).

Pick an existing option backtest row in `C:\Users\basti\Documents\ba2\test\dl_forecasting.db` (any
recent single-leg and one multi-leg). Rebuild config via `rerun_handler.rebuild_config_for_backtest`,
run `run_daily_backtest` with sys.path pinned to (a) a detached worktree at origin/dev 36c83f05 and (b)
this branch, socket disabled, dump canonical JSON of trades + equity curve; require byte-identical.
Report the two hashes. Remove the detached worktree afterwards.

### Task 9: Versions, docs, final checks

- Bump: `packages/common/ba2_common/version.py` + `packages/common/pyproject.toml` (ba2_common
  2026.11.00028 -> 00029), `ba2_trade_platform/version.py` APP +1, `testplatform/version.py` TEST +1
  (0168 -> 0169). Do NOT raise `testplatform/required_package_versions.py`.
- Run `C:\Users\basti\ba2-venvs\trade\Scripts\python.exe tools/check_package_versions.py --base origin/dev --include-worktree` -> OK.
- Add a short "Option selection replay" section to
  `docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md` status notes pointing at the design.
- Commit `chore(replay): option capture replay; ba2_common 00029, APP +1, TEST 0169 (minimum unchanged)`.
