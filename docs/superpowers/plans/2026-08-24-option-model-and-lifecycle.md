# Option Data Model and Shared Lifecycle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make an option position fully recorded and continuously managed, on one code path shared
by live and backtest.

**Architecture:** `Transaction` becomes the *intent* (underlying + strategy + expiry); orders stay
the *execution* and get their leg fills reconciled. Management moves out of the rules engine into a
shared, expert-free lifecycle pass that runs on the existing `open_positions` trigger.
`PremiumSeller`/`OptionPortfolioManager` are promoted into that pass and deleted.

**Tech Stack:** SQLModel + Alembic, pytest, NiceGUI (untouched here), alpaca-py 0.43.2.

**Spec:** `docs/superpowers/specs/2026-08-24-option-model-and-lifecycle-design.md`

---

## Before you start — read this

**Land after the in-flight IV-rank work.** At time of writing these are uncommitted and belong to
another agent: `TradeManager.py`, `OptionsAccountInterface.py`, `iv_rank_audit.py`,
`PremiumSeller/{__init__,signals}.py`, `backtest_account.py`, `options_provider.py` and three test
files. Run `git status` first; if any is dirty, wait. Never edit around in-flight work — doing so
silently clobbered three hunks earlier in this project.

**Alembic head is `a3f1c07d9e21`.** New revisions chain off it. Confirm exactly one head when done.

**Environment.** venv is `venv/`, not `.venv/`. Never mix a `packages/*/tests/` path with a root
`tests/` path in one pytest invocation — pre-existing `ImportPathMismatchError`. Two commands:

```bash
venv/bin/python -m pytest tests/ -q
venv/bin/python -m pytest packages/common/tests/ -q
```

Baselines at time of writing: `tests/` **2734**, `packages/common/tests/` **1042**, providers 196,
experts 485, `testplatform/backend` 1488 + 1 known Windows-only failure
(`test_worker_server.py::test_logs_rejects_path_traversal` — not yours, do not "fix" it).

**House rules that have each cost real bugs here:**

- **Never use `caplog`.** `ba2_trade_platform/logger.py:24` sets `propagate = False` and
  `tests/test_penny_gainers_fix.py:53` swaps the logger module for a MagicMock under full
  collection. Use `_capture_errors(monkeypatch)`.
- **Freeze time explicitly, never to today** — a mutation once survived because the frozen date
  equalled the wall clock.
- **Unknown is never a value.** Missing quote, missing greek, missing IV must stay distinct from 0.
- **`ba2_trade_platform/core/*.py` are alias shims** whose edits are silently discarded — except
  `core/TradeManager.py`, `core/JobManager.py` and `core/portfolio_allocation_service.py`, which are
  real. Check a file's header.
- **Never touch the live DB** `~/Documents/ba2/trade/db.sqlite` or the 10 GB options cache. Read-only
  queries are encouraged. Migration tests use throwaway files via `BA2_DB_FILE` (not `DB_FILE`).
- **No live broker calls.** Everything mocked.
- **Commit explicit paths.** Never `git add -A`.
- **Mutation-test every money path**, restore byte-identically (`git hash-object`), leave
  `git status` clean.

---

## File structure

| file | responsibility | task |
|---|---|---|
| `packages/common/ba2_common/core/models.py` | `Transaction` gains `asset_class`, `option_strategy`, `expiry` | 1 |
| `alembic/versions/<new>_option_intent_columns.py` | the three columns, idempotent | 1 |
| `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py` | stamp intent + parent identity on submit; single-expiry guard | 2, 3 |
| `packages/common/ba2_common/core/option_lifecycle.py` **(new)** | the PURE decision function — positions + chain in, actions out | 6 |
| `packages/common/ba2_common/core/option_book.py` **(new)** | book totals, rails, circuit-breaker state — pure | 7 |
| `ba2_trade_platform/modules/accounts/AlpacaAccount.py` | leg-fill reconciliation; partial called-away split | 4, 5 |
| `ba2_trade_platform/core/option_lifecycle_service.py` **(new)** | the live runner: loads book, calls the pure fn, submits | 8 |
| `ba2_trade_platform/core/JobManager.py` | run the pass before the analyses; readiness reporting | 8, 9 |
| `testplatform/backend/app/services/backtest/backtest_account.py` | stop liquidating assigned stock | 10 |
| `testplatform/backend/app/services/backtest/daily_engine.py` | call the same pure fn | 10, 11 |

New pure modules go in `packages/common` so live and backtest call one implementation. Two
implementations of one rule is how the short-sign divergence happened.

---

# Block 1 — Transaction becomes the intent

### Task 1: Add the three intent columns

**Files:**
- Modify: `packages/common/ba2_common/core/models.py` (`Transaction`)
- Create: `alembic/versions/<rev>_option_intent_columns.py`
- Test: `packages/common/tests/test_option_intent_model.py`,
  `tests/test_option_intent_migration.py`

`Transaction` has 19 fields today and the only tell that a row is an option is `multiplier=100`.

- [ ] **Step 1: Write the failing test**

```python
# packages/common/tests/test_option_intent_model.py
from datetime import date
from ba2_common.core.models import Transaction
from ba2_common.core.types import AssetClass, OrderDirection


def test_transaction_records_the_option_intent():
    """Transaction is the INTENT: underlying + strategy + expiry. Not the contract."""
    txn = Transaction(symbol="ACN", quantity=1, side=OrderDirection.BUY,
                      asset_class=AssetClass.OPTION,
                      option_strategy="bull_call_spread",
                      expiry=date(2026, 8, 21))
    assert txn.asset_class is AssetClass.OPTION
    assert txn.option_strategy == "bull_call_spread"
    assert txn.expiry == date(2026, 8, 21)
    assert txn.symbol == "ACN", "symbol must stay the UNDERLYING, never an OCC string"


def test_an_equity_transaction_defaults_to_equity_and_no_option_fields():
    txn = Transaction(symbol="ACN", quantity=10, side=OrderDirection.BUY)
    assert txn.asset_class is AssetClass.EQUITY
    assert txn.option_strategy is None
    assert txn.expiry is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `venv/bin/python -m pytest packages/common/tests/test_option_intent_model.py -v`
Expected: FAIL — `TypeError: 'asset_class' is an invalid keyword argument`.

- [ ] **Step 3: Add the fields**

In `Transaction`, after `multiplier`:

```python
    asset_class: AssetClass = Field(default=AssetClass.EQUITY, index=True,
                                    description="EQUITY or OPTION. Before this existed the only "
                                                "tell was multiplier=100.")
    option_strategy: str | None = Field(default=None,
                                        description="The INTENT: bull_call_spread, iron_condor, "
                                                    "covered_call... None for equity.")
    expiry: date | None = Field(default=None, index=True,
                                description="The structure's expiry. Valid as a single value ONLY "
                                            "because every supported structure is single-expiry "
                                            "(no calendars/diagonals). See Task 2.")
```

Do **not** add `strike`: meaningful for one leg, misleading for four.

- [ ] **Step 4: Run it and watch it pass**

- [ ] **Step 5: Write the migration test**

```python
# tests/test_option_intent_migration.py
import subprocess, sqlite3, os, pytest

REV = "<your new revision id>"
COLS = ("asset_class", "option_strategy", "expiry")


def _upgrade(db_path):
    env = {**os.environ, "BA2_DB_FILE": db_path}
    r = subprocess.run(["venv/bin/python", "-m", "alembic", "upgrade", "head"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r


def _cols(db_path, table="transaction"):
    con = sqlite3.connect(db_path)
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    finally:
        con.close()


def test_a_clean_database_gains_all_three_columns(tmp_path):
    db = str(tmp_path / "clean.sqlite")
    _upgrade(db)
    assert set(COLS) <= set(_cols(db))


def test_running_the_upgrade_twice_is_a_no_op(tmp_path):
    """The sibling revision's runbook ends 'IF IT FAILS, JUST RE-RUN IT'. Hold that line."""
    db = str(tmp_path / "twice.sqlite")
    _upgrade(db)
    before = _cols(db)
    _upgrade(db)
    assert _cols(db) == before


def test_a_create_all_database_that_already_has_them_upgrades_cleanly(tmp_path):
    """init_db()'s create_all can build the table before Alembic ever runs."""
    db = str(tmp_path / "createall.sqlite")
    _build_via_create_all(db)          # see tests/test_portfolio_allocation_migration.py
    _upgrade(db)
    assert set(COLS) <= set(_cols(db))


def test_asset_class_is_not_null_and_defaults_to_the_stored_enum_NAME(tmp_path):
    """SQLModel str-enums store the NAME ('EQUITY'), not the value. A default written as
    the value produces rows no query matches."""
    db = str(tmp_path / "default.sqlite")
    _upgrade(db)
    con = sqlite3.connect(db)
    try:
        info = {r[1]: r for r in con.execute('PRAGMA table_info("transaction")')}
        assert info["asset_class"][3] == 1, "asset_class must be NOT NULL"
        assert "EQUITY" in str(info["asset_class"][4])
        assert info["option_strategy"][3] == 0 and info["expiry"][3] == 0
    finally:
        con.close()


def test_the_migrated_column_order_matches_what_create_all_would_build(tmp_path):
    """A column dropped from the CREATE is silently re-added by a trailing repair, so
    name-and-nullability assertions still pass. Position is what separates the paths."""
    migrated = str(tmp_path / "m.sqlite"); _upgrade(migrated)
    fresh = str(tmp_path / "f.sqlite"); _build_via_create_all(fresh)
    assert _cols(migrated) == _cols(fresh)
```

**Do not backfill.** No existing `Transaction` row gets an `option_strategy` or `expiry` — 23 of
the 82 historical option orders have unrecoverable contracts, so a backfill would be guesswork
wearing a fact's clothing. Forward-only, as the spec states.

**Trap:** SQLModel str-enums are stored by **NAME** (`'EQUITY'`), not value. A backfill that writes
the value silently produces rows no query matches.

- [ ] **Step 6: Write the migration**

`down_revision = 'a3f1c07d9e21'`. Guard every `add_column` with
`sa.inspect(op.get_bind()).has_column(...)` using a **fresh inspector per call** (a shared one
memoises reflection from before the first DDL). Follow
`alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py`, which is idempotent and carries
a runbook ending "IF IT FAILS, JUST RE-RUN IT". Match it.

- [ ] **Step 7: Prove it on throwaway DBs**

```bash
BA2_DB_FILE=/tmp/t1.sqlite venv/bin/python -m alembic upgrade head   # clean
BA2_DB_FILE=/tmp/t1.sqlite venv/bin/python -m alembic upgrade head   # re-run: no-op
venv/bin/python -m alembic heads                                     # exactly one
```

- [ ] **Step 8: Commit**

```bash
git add packages/common/ba2_common/core/models.py alembic/versions/ \
        packages/common/tests/test_option_intent_model.py tests/test_option_intent_migration.py
git commit -m "feat(options): Transaction records the intent - asset class, strategy, expiry"
```

---

### Task 2: Assert the single-expiry invariant

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py`
- Test: `packages/common/tests/test_option_single_expiry_guard.py`

A single `Transaction.expiry` is only correct because all 16 structures put every leg on one
expiry. If a calendar or diagonal is ever added the field becomes **wrong**, not merely incomplete.
Make that impossible to miss.

- [ ] **Step 1: Write the failing test**

```python
def test_submitting_legs_with_different_expiries_is_refused(...):
    """A calendar spread would make Transaction.expiry a lie. Refuse it at the boundary."""
    legs = [_leg(expiry=date(2026, 8, 21)), _leg(expiry=date(2026, 9, 18))]
    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order(legs, ...)


def test_all_legs_on_one_expiry_is_accepted(...):
    # the normal path must be unaffected
```

- [ ] **Step 2: Run it, watch it fail** — no exception is raised today.

- [ ] **Step 3: Add the guard** in `submit_option_order`, before any row is written. The message
  must name the offending expiries and say why (`Transaction.expiry` holds one value).

- [ ] **Step 4: Run it, watch it pass.** Then run the whole option suite —
  `venv/bin/python -m pytest tests/ -q -k option` — and confirm nothing legitimate now refuses.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(options): refuse a multi-expiry structure rather than mis-record its intent"
```

---

### Task 3: Stamp identity on the parent and the intent on the transaction

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py:99-152`
- Test: `packages/common/tests/test_option_parent_identity.py`

Today `submit_option_order` nulls `contract_symbol`/`option_type`/`strike`/`expiry` on a multi-leg
parent — and the parent is the row that fills. Consequence measured on the live DB: **of 28 filled
option orders only 11 record which contract was traded**, and
`OptionPortfolioManager._should_close` reads `parent.expiry`, so **roll-at-DTE has never fired for
any multi-leg**.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_multi_leg_parent_records_the_shared_expiry(...):
    """The roll-at-DTE gene reads parent.expiry. NULL there is why it never fires."""
    parent = submit_two_leg_spread(expiry=date(2026, 8, 21))
    assert parent.expiry == date(2026, 8, 21)


def test_a_multi_leg_parent_still_has_no_single_contract(...):
    """Four legs, four contracts, one parent - contract_symbol stays None. Honest, not lazy."""
    assert parent.contract_symbol is None
    assert parent.strike is None


def test_a_single_leg_order_records_its_full_contract(...):
    assert parent.contract_symbol == "ACN260821C00130000"
    assert parent.strike == 130.0


def test_the_transaction_records_the_intent(...):
    assert txn.asset_class is AssetClass.OPTION
    assert txn.option_strategy == "bull_call_spread"
    assert txn.expiry == date(2026, 8, 21)
    assert txn.symbol == "ACN"
```

- [ ] **Step 2: Run them, watch them fail** — `parent.expiry` is `None`; the transaction has no
  `option_strategy`.

- [ ] **Step 3: Implement.** Keep `contract_symbol`/`option_type`/`strike` NULL for multi-leg —
  that nulling is correct. Add `expiry` (now guaranteed shared by Task 2), and set the three
  intent fields on the `Transaction`.

- [ ] **Step 4: Run them, watch them pass.**

- [ ] **Step 5: Mutation-test.** Revert the parent `expiry` stamp; confirm the roll test fails.
  Restore byte-identically and verify with `git hash-object`.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(options): a filled parent records its expiry, and the transaction its intent"
```

---

### Task 4: Reconcile leg fills

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (`refresh_orders` path)
- Test: `tests/test_option_leg_fill_reconciliation.py`

Children carry the contracts but are never updated, sitting at `ACCEPTED / filled_qty=0` forever.
So per-leg economics are unrecoverable and the executed position is inferred from rows the DB says
never executed. `TradingOrder.legs_broker_ids` on the parent is the link.

- [ ] **Step 1: Write the failing test** — a parent with two `legs_broker_ids`, a mocked broker
  returning both legs filled at different prices; assert each child ends `FILLED` with its own
  `filled_qty` and `open_price`, and that the two prices differ (so a single shared value cannot
  pass).

- [ ] **Step 2: Run it, watch it fail** — children stay `ACCEPTED`, `filled_qty=0`.

- [ ] **Step 3: Implement.** Match children by `legs_broker_ids` ↔ `broker_order_id`. A leg the
  broker does not return must stay untouched, **not** be marked filled — the same
  `None`-is-not-empty rule that has bitten this codebase five times.

- [ ] **Step 4: Run it, watch it pass.**

- [ ] **Step 5: Add a partial-fill test** — one leg filled, one working. The filled child updates;
  the working one does not; the parent is not treated as complete.

- [ ] **Step 6: Mutation-test** — (a) mark every child filled regardless of the response, (b) copy
  the parent's price onto every child. Both must fail a test.

- [ ] **Step 7: Commit**

```bash
git commit -m "fix(options): reconcile leg fills so a structure's per-leg economics are real"
```

---

# Block 2 — Assignment correctness

### Task 5: Partial called-away must split, not erase

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (`_close_txn` / called-away path)
- Test: `tests/test_called_away_partial.py`

**Live money bug.** One assigned contract against a 300-share transaction closes **all 300** —
`_close_txn` has no partial close, so 200 real shares vanish from the ledger while sitting at the
broker. Currently only a warning.

- [ ] **Step 1: Write the failing test**

```python
def test_a_one_contract_called_away_leaves_the_other_200_shares_on_the_book(...):
    """300 shares, 1 contract called away. 100 leave, 200 STAY. Today all 300 vanish."""
    txn = _assigned_equity_txn(symbol="AAPL", shares=300.0)
    _reconcile_called_away(contracts=1, strike=150.0)

    closed = _closed_txns("AAPL")
    remaining = _open_txns("AAPL")
    assert sum(t.quantity for t in closed) == 100.0
    assert sum(t.quantity for t in remaining) == 200.0, "200 shares are still at the broker"
```

- [ ] **Step 2: Run it, watch it fail** — `remaining` is empty; all 300 closed.

- [ ] **Step 3: Implement the split.** Close 100 at the strike; leave a 200-share transaction open,
  carrying the original `open_price`, `expert_id` and `meta_data` (including
  `origin=csp_assignment`, or the remainder stops being wheel-eligible). Preserve `open_date` — the
  remaining shares were acquired then, and `DaysOpenedCondition` reads it.

- [ ] **Step 4: Run it, watch it pass.**

- [ ] **Step 5: Add the exact-match test** — 100 shares, 1 contract: closes fully, leaves nothing,
  and does **not** create a zero-quantity remainder row.

- [ ] **Step 6: Mutation-test** — (a) close the whole transaction (the original bug), (b) drop
  `meta_data` from the remainder. Both must fail.

- [ ] **Step 7: Commit**

```bash
git commit -m "fix(options): a partial called-away splits the lot instead of erasing 200 real shares"
```

---

# Block 3 — The shared lifecycle pass

### Task 6: Extract the pure decision function

**Files:**
- Create: `packages/common/ba2_common/core/option_lifecycle.py`
- Test: `packages/common/tests/test_option_lifecycle.py`

Promote `OptionPortfolioManager._should_close` / `_txn_metrics` / `_tested` into a pure function.
Pure means: positions, chain and settings in — decisions out. No DB, no broker, no clock.

- [ ] **Step 1: Write the failing tests**, one per exit, each with explicit inputs:

```python
def test_a_structure_at_the_profit_target_is_closed(): ...
def test_a_structure_past_the_credit_multiple_stop_is_closed(): ...
def test_a_structure_inside_the_roll_dte_window_is_closed(): ...
def test_a_tested_short_is_defended(): ...
def test_a_healthy_structure_is_left_alone(): ...

def test_a_structure_with_no_expiry_is_reported_not_silently_held():
    """The old code read parent.expiry, which was NULL for every multi-leg, so the roll
    never fired and nobody knew. Unknown must be loud."""

def test_a_missing_greek_does_not_read_as_a_safe_delta():
    """Unknown is not a value. A tested-delta check with no delta must not conclude 'untested'."""
```

- [ ] **Step 2: Run them, watch them fail** — module does not exist.

- [ ] **Step 3: Implement.** The reason reaches the outcome table and the activity log, so it must
  be a value, not a log line. Define exactly this, and use it unchanged in Tasks 8, 10 and 11:

```python
# packages/common/ba2_common/core/option_lifecycle.py
from dataclasses import dataclass
from typing import Optional

LIFECYCLE_HOLD = "hold"
LIFECYCLE_PROFIT_CAPTURE = "profit_capture"
LIFECYCLE_CREDIT_STOP = "credit_stop"
LIFECYCLE_ROLL_DTE = "roll_dte"
LIFECYCLE_TESTED = "tested"
LIFECYCLE_BREAKER = "circuit_breaker"
#: The decision could not be made: a missing expiry, greek or price. NOT a hold --
#: "we don't know" and "it's fine" are different facts, and collapsing them is the
#: mistake that hid the dead roll-DTE gene for an entire GA campaign.
LIFECYCLE_UNKNOWN = "unknown"

LIFECYCLE_CLOSING_REASONS = (LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_CREDIT_STOP,
                             LIFECYCLE_ROLL_DTE, LIFECYCLE_TESTED, LIFECYCLE_BREAKER)


@dataclass(frozen=True)
class LifecycleDecision:
    """What to do with ONE open option structure. Pure: no DB, no broker, no clock."""
    transaction_id: int
    reason: str                      # one of the LIFECYCLE_* constants
    detail: str                      # human-readable, e.g. "18 DTE <= roll_dte 21"
    pnl_pct: Optional[float] = None  # None means unmeasurable, never 0.0

    @property
    def should_close(self) -> bool:
        return self.reason in LIFECYCLE_CLOSING_REASONS


def decide(structures, chain_by_symbol, settings, as_of) -> list[LifecycleDecision]:
    """One decision per structure, in a deterministic order."""
```

- [ ] **Step 4: Run them, watch them pass.**

- [ ] **Step 5: Mutation-test each threshold** — invert every comparison, and make a missing greek
  read as untested. Each must fail a named test.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(options): pure lifecycle decisions, shared by live and backtest"
```

---

### Task 7: Book rails and the circuit breaker

**Files:**
- Create: `packages/common/ba2_common/core/option_book.py`
- Test: `packages/common/tests/test_option_book_rails.py`

Promote `_book_totals` / `_within_rails` plus the sleeve drawdown breaker. These are the four
things no rule can express, because `TradeActionEvaluator.evaluate(instrument_name,
expert_recommendation, ...)` is per-instrument by signature.

- [ ] **Step 1: Write the failing tests** — `max_deployment_pct`, `max_notional_leverage`,
  `undefined_risk_max_pct`, concurrent-structure cap, one-per-underlying cap, and the breaker
  tripping on peak-to-trough sleeve drawdown. Plus:

```python
def test_an_unknown_account_equity_declines_rather_than_assuming(...):
    """PremiumSeller's rails declined when balance was unknown rather than fabricating. Keep it."""
```

- [ ] **Step 2: Run them, watch them fail.**

- [ ] **Step 3: Implement.** Rails are **per-expert sleeves**, matching the original semantics.
  Account-wide caps are explicitly out of scope — say so in the docstring so nobody assumes.

- [ ] **Step 4: Run them, watch them pass.**

- [ ] **Step 5: Mutation-test** — disable each rail individually; each must fail its own test.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(options): book rails and sleeve circuit breaker, promoted out of PremiumSeller"
```

---

### Task 8: Run the pass on the open_positions trigger

**Files:**
- Create: `ba2_trade_platform/core/option_lifecycle_service.py`
- Modify: `ba2_trade_platform/core/JobManager.py` (`_execute_open_positions_analysis`)
- Test: `tests/test_option_lifecycle_service.py`

The pass runs **before** the analyses and **must not invoke an expert**. Maintenance is calendar
and state driven; paying for an FMP call plus an LLM analysis to discover a spread is at 21 DTE is
the cost behind the "options as fast as stocks" requirement.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_lifecycle_pass_runs_before_any_analysis_is_submitted(...):
    """Order matters: manage first, then let the expert opine on what remains."""


def test_the_lifecycle_pass_submits_no_market_analysis(...):
    """The whole point. Assert the analysis submitter was never called."""
    assert submit_market_analysis.call_count == 0


def test_a_close_is_guarded_against_a_pending_close(...):
    """The pass is scheduled and may overlap a manual action.
    has_pending_closing_order is the existing primitive."""


def test_a_broker_that_cannot_answer_stops_the_pass_for_that_account(...):
    """Never act against a book you cannot see. get_positions() returning None is
    'fetch failed', not 'flat' - that conflation has caused five incidents."""
```

- [ ] **Step 2: Run them, watch them fail.**

- [ ] **Step 3: Implement.** The service loads the expert's open option transactions, builds the
  book, calls the pure functions from Tasks 6 and 7, and submits closes through the existing
  `CloseOptionAction` / `submit_option_order`. Every close guarded by
  `has_pending_closing_order`.

- [ ] **Step 4: Run them, watch them pass.**

- [ ] **Step 5: Add an idempotence test** — run the pass twice with no state change; the second
  run submits nothing.

- [ ] **Step 6: Mutation-test** — (a) run the pass after the analyses, (b) drop the pending-close
  guard, (c) treat `get_positions() is None` as flat. Each must fail.

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(options): shared lifecycle pass on the open-positions trigger, no expert needed"
```

---

### Task 9: Report the two silent failure modes

**Files:**
- Modify: `ba2_trade_platform/core/JobManager.py` (startup reporting, beside
  `report_iv_rank_readiness`)
- Test: `tests/test_option_lifecycle_readiness.py`

Both of these are silent today, and a silent never-managed option position is exactly what this
work exists to remove.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_expert_holding_options_with_no_manage_schedule_is_reported(...):
    """No open_positions schedule means its options are NEVER managed. Say so at startup."""


def test_a_non_alpaca_account_holding_options_is_reported(...):
    """Only AlpacaAccount implements reconcile_option_assignments. On IBKR/TastyTrade a
    wheel stalls silently at leg 2."""


def test_a_healthy_setup_reports_nothing_alarming(...):
    """A report that always warns trains the user to ignore it."""
```

- [ ] **Step 2: Run them, watch them fail.**

- [ ] **Step 3: Implement.** Follow `report_iv_rank_readiness`'s shape. Never emit a count-only
  line that reads as success when it means "I cannot see anything" — the IV-rank report shipped
  `0/0 ARMED` for exactly that reason and it read as green.

- [ ] **Step 4: Run them, watch them pass.**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(options): report unmanaged option positions and brokers without assignment support"
```

---

# Block 4 — Parity, then delete the old path

### Task 10: The backtest must stop liquidating assigned stock

**Files:**
- Modify: `testplatform/backend/app/services/backtest/backtest_account.py`
  (`settle_single_leg_expiry`, `_pending_assignment_sells`)
- Modify: `testplatform/backend/app/services/backtest/daily_engine.py`
- Test: `testplatform/backend/tests/backtest/test_wheel_assignment.py`

Today a short ITM put assigns and the resulting stock is fully liquidated at the next bar's open.
**The wheel cannot be backtested at all.**

**DONE 2026-08-27** — commits `d2e6db1` (engine) and `7345634` (launcher wiring), branch
`wheel-engine`. The polarity landed the OTHER way round from the heading, deliberately:

- [x] **Step 1: Write the failing test** —
  `testplatform/backend/tests/backtest/test_wheel_assignment.py`, 6 tests including the named
  `test_assigned_shares_survive_to_the_next_bar_so_a_call_can_be_written`.

- [x] **Step 2: Run it, watch it fail** — 4 of 6 failed; the covered call could not even be
  written (`UNCOVERED SHORT CALL ... only 0 are free`) because the liquidation had already run.

- [x] **Step 3: Implement — as an OPT-IN switch, DEFAULT OFF.** Holding is NOT the new default.
  The liquidation is not a bug: assigned stock no option rule manages rides unmanaged to the
  end of the run (exercised ITM long calls doing exactly that were 67-85% of the OS1 blow-up's
  final equity) and the behaviour is pinned as intent. So `BacktestAccount` gained a
  `hold_assigned_stock` account setting (bool, required False) which suppresses ONLY the
  scheduling in `_book_assignment_share_leg`; everything else about an assignment is unchanged.
  `ba2test_launcher._HOLDS_ASSIGNED_STOCK = {"O_WHEEL"}` turns it on per strategy, and the
  O_WHEEL build refusal (`_allow_unrunnable_wheel` / `BA2_ALLOW_UNRUNNABLE_WHEEL`) is deleted.

- [x] **Step 4: Run it, watch it pass** — 6/6.

- [x] **Step 5: Full suite.** `testplatform/backend/tests`: **3300 passed, 158 skipped**, 1
  failed = the known Windows-only `test_worker_server.py::test_logs_rejects_path_traversal`.
  Baseline on `dev` was 3291 passed with the same single failure; the +9 are new tests.
  **NOTHING SHIFTED**, because default-off is bit-identical — proven, not asserted: a full
  `DailyBacktestEngine.run()` over an ITM short put (so it exercises
  `settle_single_leg_expiry` -> `_book_assignment_share_leg` ->
  `process_pending_assignment_liquidations`) was run against a `dev` worktree and against the
  change, dumping cash, equity, every order, every round-trip trade and every equity-curve
  point as sorted JSON. The outputs diff clean; the same probe with the switch ON differs in
  77 lines, so it is sensitive to what it certifies.

- [x] **Step 6: Commit.**

**OPEN ISSUE this task exposes — the wheel's stock has ONE exit, and it is not guaranteed.**
Once the shares are held, the only thing that closes them is the covered call being ASSIGNED
ITM (`_book_assignment_share_leg`'s `closing` branch delivers them against the held lot). If
the call keeps expiring worthless nothing sells the stock: every rule in O_WHEEL's exit list is
a `close_option`, `cc_guard` halts the chain while a call is open, `maybe_margin_call_liquidation`
only unwinds SHORT positions, and `DailyBacktestEngine.run` has no end-of-run flatten. The
shares ride to the end of the run and are reported `open_at_end` (marked to market, so the P&L
is not lost — but the capital is tied up). Pinned as a known limitation by
`test_a_worthless_call_leaves_the_shares_held_with_no_exit`. No exit rule was invented for it.

---

### Task 11: Prove live and backtest agree

**Files:**
- Test: `tests/test_option_lifecycle_parity.py`

- [ ] **Step 1: Write the test** — the same book and chain through the live service and the
  backtest engine must produce identical decisions and reasons. Cover profit capture, roll-DTE and
  a tested short.

- [ ] **Step 2: Run it.** If it fails, one engine is not calling the shared function — fix that
  rather than the assertion.

- [ ] **Step 3: Add a structural guard** — assert both engines import the decision function from
  `ba2_common.core.option_lifecycle` and neither defines its own. The short-sign divergence was two
  implementations of one rule; this is the test that would have caught it.

- [ ] **Step 4: Commit**

```bash
git commit -m "test(options): live and backtest decide identically, from one implementation"
```

---

### Task 12: Delete PremiumSeller and OptionPortfolioManager

**Files:**
- Delete: `packages/experts/ba2_experts/PremiumSeller/` (924 lines, 4 files)
- Delete: its tests
- Modify: `testplatform/backend/app/services/backtest/daily_engine.py` (the two `bypasses_classic_rm`
  call sites at ~`:680` and ~`:1388`)
- Modify: the expert registry if it lists it

Do this **last**, once Tasks 6-11 prove the capability survives elsewhere.

- [ ] **Step 1: Prove the logic is gone before deleting the source.** Grep every promoted behaviour
  — book rails, breaker, tested-delta, roll — and confirm each now has a test in
  `packages/common/tests/`. List them in the commit message. If any has no home, **stop** and add it
  first.

- [ ] **Step 2: Check for live callers**

```bash
grep -rn "PremiumSeller\|OptionPortfolioManager\|bypasses_classic_rm" \
  --include="*.py" ba2_trade_platform packages testplatform | grep -v /tests/
```

Expected: only the two `daily_engine.py` sites. Anything else, stop and report.

- [ ] **Step 3: Delete, and remove the `bypasses_classic_rm` branch** if nothing else uses it.

- [ ] **Step 4: Run everything** — all four suites plus the backtest suite. Report every count.

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor(options): delete PremiumSeller - its capabilities now serve every position"
```

---

### Task 13: Bump both versions and sweep

**Files:**
- Modify: `ba2_trade_platform/version.py`, `testplatform/version.py`

`packages/` changed, so **both** bump per the Section I rule — `worker_client.ensure_synced` keys on
`TEST_APP_VERSION` alone, and a `packages/` change without a bump leaves GA workers running
different `ba2_common` code from the master.

- [ ] **Step 1: Read both files and bump the build number by one.** Do not assume the values in this
  plan; read what is actually there. Trade must not go backwards relative to `dev`.

- [ ] **Step 2: Per-file sweep** of every file this plan created or modified, plus the pre-existing
  suites most likely to be disturbed: `tests/test_option_assignment.py`,
  `tests/test_options_account_interface.py`, `tests/test_option_conditions.py`,
  `tests/test_trade_manager_option_reconcile.py`, `tests/test_boot_smoke.py`.

- [ ] **Step 3: `venv/bin/python -m alembic heads`** — exactly one.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: bump both versions for the option model and lifecycle work"
```

---

## Follow-up (separate spec and plan, not this one)

**D — the grid runner.** 0DTE and 30–45 DTE arms across `FMPRating` and `DeterministicScorer`.
Settled in the spec: **ETFs and stocks are separate arms**, because index ETFs have daily expiries
and a small fixed universe while large-cap stocks list Friday weeklies and need a screener — so
analysis-day alignment differs and is a grid parameter, not a setting. Still open: the source for
an optionable-symbol screener filter (FMP does not expose one).

Also outstanding, from earlier audits: `strike_method` is ignored by 8 of 16 structures; the GA
only writes a scalar spread width so every backtested vertical is minimum-width; and **40 of 73
OS1 genes have no measurable effect**. Worth auditing before spending more GA time.
