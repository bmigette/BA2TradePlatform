# Portfolio Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give manually traded accounts a Portfolio Allocation page that turns instrument-label target percentages into reviewed, submitted broker orders, backed by a pure allocation engine, a per-event income ledger and a full TastyTrade trading surface.

**Architecture:** All allocation arithmetic lives in one pure, IO-free module (`ba2_common/core/portfolio_allocation.py`) that knows nothing about NiceGUI, the database or a broker SDK; a persistence layer (`ba2_common/core/portfolio_allocation_store.py`) owns every read and write of the five new tables; a live service (`ba2_trade_platform/core/portfolio_allocation_service.py`) wires the engine to positions, prices, broker prechecks and order submission; and four new broker seams (`get_account_snapshot`, `get_cash_transfers`, `get_symbol_margin_info`, `preview_order_impact`) make Alpaca and TastyTrade interchangeable behind broker-agnostic value objects.

**Tech Stack:** Python 3.12, SQLModel + SQLAlchemy + Alembic, NiceGUI, pytest, alpaca-py 0.43.2, tastytrade 12.0.2.

**Spec:** `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform/docs/superpowers/specs/2026-08-20-portfolio-allocation-design.md` — the approved design, authoritative for behaviour. Where this plan deviates from the spec's wording it says so explicitly and gives the verified reason.

---

## Key facts

An engineer with zero context will get these wrong. Every one was verified against the real files on 2026-08-20.

1. **`ba2_trade_platform/core/*.py` are ALIAS SHIMS.** They swap themselves out of `sys.modules` — edits to them are discarded. The real code is in `packages/common/ba2_common/`. Every NEW shared module needs a shim copied VERBATIM from `ba2_trade_platform/core/option_types.py` with only the `_importlib.import_module("...")` target changed. `tests/test_alias_shim_race.py` auto-discovers shims and asserts the race-guard ordering.
2. **The venv on this Mac is `venv/`, not `.venv/`.** Test command: `venv/bin/python -m pytest <path> -v`. Run **per file** — the full suite fails non-deterministically from a pre-existing session leak, so a per-file green is the only signal.
3. **`pytest.ini` has `testpaths = tests`**, so `packages/common/tests/*` is only collected when named by explicit path. It also sets `pythonpath = packages/common packages/providers packages/experts`, which is why `ba2_common` imports work under pytest but **not** under a bare `venv/bin/python` — the editable-install `.pth` files point at sibling repos that do not exist here. Any ad-hoc probe must run as `PYTHONPATH=packages/common:packages/providers:packages/experts venv/bin/python ...`.
4. **`get_positions()` returning `None` means the FETCH FAILED; `[]` means genuinely flat.** Conflating them on 2026-07-03 mass-closed 8 real open transactions during a DNS outage. The page must refuse to plan on `None`.
5. **`db.get_instance()` RAISES `InstanceNotFound`** (a `LookupError` subclass, `db.py:605`) when the row is missing — it does not return `None`, despite its own docstring. Never write `if not get_instance(...)`; wrap in `try/except InstanceNotFound`.
6. **`AccountInterface.submit_order()` returns a TRUTHY `TradingOrder` with `status == OrderStatus.WASHTRADE_LOCKED`** when the wash-trade gate fires, and `None` on hard failure with the reason appended to the row's `.comment` (truncated to 500 chars). ALWAYS inspect `result.status`, never `if result:`.
7. **An allocation-run order comment MUST NOT contain the substring `closing` (any case).** `AccountInterface.close_transaction` re-detects an existing close order with `order.order_type == OrderType.MARKET and 'closing' in order.comment.lower()` (`AccountInterface.py:1531-1536`), and allocation orders are MARKET orders.
8. **Alpaca fractional orders need `good_for='day'` AND `OrderType.MARKET`.** `AlpacaAccount._submit_order_impl` maps `good_for` through a `tif_map` and **defaults to `TimeInForce.GTC`** when it is `None` or unrecognised (`AlpacaAccount.py:940`, map at `:934-939`). Alpaca rejects fractional on GTC and on any non-market type. On rejection, retry once at `floor(qty)`; a floor of 0 is SKIPPED, not a failure.
9. **`tastytrade.account.Account.place_order(session, order, dry_run: bool = True)` — `dry_run` DEFAULTS TO `True`** (`site-packages/tastytrade/account.py:877-879`; `place_complex_order` likewise at `:894-896`). Every real submission must pass `dry_run=False` explicitly.
10. **`tastytrade.order.BuyingPowerEffect.change_in_buying_power` is a SIGNED Decimal** — negative for a buy (the `set_sign_for` validator, `order.py:381-393`). Always consume `OrderImpact.bp_cost`. Separately, `NewOrder.price_effect` is a **computed field** derived from the sign of `price` (`order.py:264-276`) and must never be set by hand — a BUY limit's `price` is written NEGATIVE.
11. **`Field(unique=True, index=True)` on `Instrument.name` emits `CREATE UNIQUE INDEX ix_instrument_name`** (verified by probe). The migration must use `ix_instrument_name`, **not** the spec's `uix_instrument_name`, or `init_db()`'s `create_all` on a fresh DB and Alembic on an existing one produce differently named indexes.
12. **The instrument merge is safe: no table has a foreign key to `instrument`** (verified by grep and by `pragma_foreign_key_list` over the live schema). On the LIVE DB (`~/Documents/ba2/trade/db.sqlite`, see fact 24): 2477 rows / 2353 distinct names / 124 duplicate groups, and zero indexes on `instrument`.
13. **`Transaction` HAS NO `account_id` column.** Transactions link to an account only through `TradingOrder.account_id`. Canonical query: `select(Transaction).join(TradingOrder).where(TradingOrder.account_id == ..., Transaction.status.in_([OPENED, CLOSING])).distinct()`.
14. **Read the gate as `account.get_setting_with_interface_default('manual_trading_enabled', log_warning=False)`.** `self.settings.get(key, default)` returns `None` (not the default) for a never-saved key, because the settings property seeds every DECLARED key to `None`. `get_setting_with_interface_default` also treats the literal string `"None"` as unset, and RAISES `ValueError` if the key is absent from the merged definitions. A saved boolean `False` IS returned correctly.
15. **Add `manual_trading_enabled` INSIDE the existing dict literal** in `ReadOnlyAccountInterface._ensure_builtin_settings` (`:31-41`). The body is guarded by `if not cls._builtin_settings:`, so a second block or a post-hoc `.update()` never runs.
16. **Valid `ui.notify` types are only `'positive' | 'negative' | 'warning' | 'info'`.** `ui/pages/settings.py:1023` and `:1041` use `type="error"` — an EXISTING BUG; do not copy it.
17. **`AlpacaAccount.get_account_info()` returns the RAW pydantic `TradeAccount`** (`:1489-1505`) or `None` on auth failure. Every numeric field on it is `Optional[str]` — including `multiplier` (`"1"`/`"2"`/`"4"`). Coerce every field through `float()`. `.get()` on it raises `AttributeError` — that is the `TradeActions.py:1493` bug.
18. **Alpaca's `Asset` exposes NO initial-margin field**, only `maintenance_margin_requirement` (a percentage such as `30.0`). Derive `initial_margin_rate`: `marginable -> 0.5` (Reg-T), otherwise `1.0`. Then `bp_factor = initial_margin_rate * account_multiplier`.
19. **`json.dumps` of a str-Enum yields its VALUE**, so `AllocationRow.side` may be an `OrderDirection` in memory. SEPARATELY, SQLModel stores python str-enums in enum COLUMNS **by NAME** — which is exactly why the five new tables use PLAIN `str` columns for `mode` / `event_type` / `valuation_mode`, matching `OptionActivity.activity_type`.
20. **`packages/common/tests/test_utils_pure.py` must be run with an explicit `PYTHONPATH`, or its leak gate FAILS rather than protecting you.** The check shells out via `subprocess.run([sys.executable, "-c", ...])`, and that child does NOT inherit pytest's `pythonpath` ini setting — so it dies with `ModuleNotFoundError: No module named 'ba2_common'` and the empty stdout reads as a failure. It fails at HEAD regardless of your change (confirmed twice, by `git stash` and by a worktree at the parent commit). Always run it as `PYTHONPATH=packages/common:packages/providers:packages/experts venv/bin/python -m pytest packages/common/tests/test_utils_pure.py -v`. With the path set the gate genuinely discriminates: `ba2_common.core.utils` reports CLEAN while `ba2_trade_platform.core.utils` reports `LEAK:ba2_providers,ba2_experts,ba2_trade_platform,langchain_core,fmpsdk`.
21. **`packages/common/tests/test_utils_pure.py` has TWO gates:** a subprocess leak check (`:32-38`) asserting `import ba2_common.core.utils` pulls in nothing from `ba2_providers` / `ba2_experts` / `ba2_trade_platform` / `nicegui` (note: `ba2_common.core.models` IS imported at module level in `utils.py:10` and is NOT forbidden), and an explicit list of pure helpers (`:51-71`, currently 24 names). New helpers must use the same **lazy** `from ba2_common.core.models import Instrument` inside the function body and must be added to that list.
22. **Foreign keys are DECLARATIVE ONLY:** the live DB runs with `PRAGMA foreign_keys = 0`, so `ondelete="CASCADE"` never fires. Account deletion must delete the new tables' rows explicitly, mirroring the `AccountSetting` cleanup loop at `ui/pages/settings.py:1025-1037`.
23. **`supports_trading` is read inconsistently and all three sites default to `True`:** `getattr(provider_cls, 'supports_trading', True)` from the CLASS at `ui/pages/settings.py:1435`, and `getattr(account, 'supports_trading', True)` from the INSTANCE at `core/TradeManager.py:921` and `:1223`. Re-parenting `TastyTradeAccount` means its local `supports_trading = False` pin must be REMOVED so it inherits `True`.
24. **Alembic head is `0a3e0bd24598`** (single head, verified two ways). Revision A (instrument merge) chains off it; revision B (the five new tables) chains off revision A's id, `f1a7c2e9b4d0`.
25. **The live trade DB is `~/Documents/ba2/trade/db.sqlite`** (399 MB; 2477 instrument rows / 2353 distinct names / 124 duplicate groups), resolved by `ba2_trade_platform/config.py:16` from `ba2_common/config.py:25` (`TRADE_DIR = BA2_HOME/trade`). `~/Documents/ba2_trade_platform/db.sqlite` is a STALE 19 MB file from 2026-06-18 — never target it. The live DB is stamped `d5e1b9a3c842`, two revisions behind the pre-existing head `0a3e0bd24598`, and `init_db()`'s `create_all` has already materialised tables Alembic does not know about (`option_activity`, `option_iv_snapshot`, `provider_cache`), so `migrate.py upgrade` may fail with a duplicate-column error. Check `PRAGMA table_info` and consider `alembic stamp` first. Do NOT run migrations against any real DB as part of a TDD loop. Alembic's DB override env var is `BA2_DB_FILE` (`alembic/env.py:21`); the app's is `DB_FILE`.
26. **`log_activity(...)` is ASYNCHRONOUS** (queued to a background worker) and returns `None` — never treat its return as a success signal. `ActivityLogType` has no allocation-specific member; reuse `ORDER_SUBMITTED`.
27. **`app.storage.user` raises `RuntimeError` outside a UI context**, so reads and writes must be guarded and must never happen from a thread pool. `ui/pages/symbol360.py:36` / `:163-181` is the precedent; the storage secret is configured at `ui/main.py:173`.
28. **Switching the global account HARD-RELOADS the page** (`ui/layout.py:124` runs `window.location.reload()`), so the allocation page never gets a chance to flush pending edits: every label / symbol / weight / comment edit must persist EAGERLY on change. There is no Save button.
29. **Two engine/UI names that would otherwise drift:** the "failed position fetch" exception is `PositionFetchFailed` (singular), defined ONCE in `ba2_common/core/portfolio_allocation.py`; and the persistence module is `portfolio_allocation_store.py` — there is no `portfolio_allocation_repo.py`.

---

## File structure

Every file this plan creates or modifies, with its single responsibility. **Never edit a SHIM** — edits are discarded.

### `packages/common/ba2_common/` — REAL (source of truth for shared/pure code)

| File | REAL/SHIM | Responsibility |
|---|---|---|
| `core/models.py` | REAL | The five new allocation tables + `unique=True` on `Instrument.name` |
| `core/utils.py` | REAL | `normalize_symbol`, `parse_instrument_symbol_list`, `get_symbols_by_label`; symbol normalisation in the four label helpers |
| `core/account_types.py` | REAL — **new** | The 4 broker-seam value objects: `AccountSnapshot`, `CashTransfer`, `MarginInfo`, `OrderImpact` |
| `core/portfolio_allocation.py` | REAL — **new** | The pure, IO-free allocation engine: value objects, arithmetic, validation, submission decisions |
| `core/portfolio_allocation_store.py` | REAL — **new** | The ONLY module that reads or writes the five allocation tables |
| `core/instrument_merge.py` | REAL — **new** | Idempotent duplicate-instrument merge, shared by the Alembic revision and its tests |
| `core/interfaces/ReadOnlyAccountInterface.py` | REAL | `manual_trading_enabled` setting + `get_account_snapshot` / `get_cash_transfers` / `get_symbol_margin_info` seams |
| `core/interfaces/AccountInterface.py` | REAL | The `preview_order_impact` seam |
| `core/TradeActions.py` | REAL | Fix `IncreaseInstrumentShareAction`'s buying-power read and its double-save bug |

### `ba2_trade_platform/core/` — SHIMS (create, never edit thereafter)

| File | REAL/SHIM | Responsibility |
|---|---|---|
| `core/account_types.py` | SHIM — **new** | Alias to `ba2_common.core.account_types` |
| `core/portfolio_allocation.py` | SHIM — **new** | Alias to `ba2_common.core.portfolio_allocation` |
| `core/portfolio_allocation_store.py` | SHIM — **new** | Alias to `ba2_common.core.portfolio_allocation_store` |
| `core/instrument_merge.py` | SHIM — **new** | Alias to `ba2_common.core.instrument_merge` |

### In-tree live-only — REAL

| File | REAL/SHIM | Responsibility |
|---|---|---|
| `core/portfolio_allocation_service.py` | REAL — **new** | Live wiring: positions/prices/margin, broker precheck, submission, run audit, income sync |
| `core/InstrumentAutoAdder.py` | REAL | Normalise the symbol before the lookup and the insert |
| `core/JobManager.py` | REAL | `ensure_instrument_exists()` lifted out of `submit_market_analysis` |
| `modules/accounts/AlpacaAccount.py` | REAL | `get_account_snapshot`, `get_symbol_margin_info`, `get_cash_transfers`, fractional-aware submission |
| `modules/accounts/TastyTradeAccount.py` | REAL | The full trading surface plus the four feature seams and six bug fixes |
| `ui/pages/portfolio_allocation.py` | REAL — **new** | The page: gating, default view, label/symbol editing |
| `ui/pages/portfolio_allocation_wizard.py` | REAL — **new** | Wizard steps, dry-run dialog, income panel, outcome table |
| `ui/utils/portfolio_allocation_view.py` | REAL — **new** | Pure view-model logic for the page (no NiceGUI, no DB) |
| `ui/main.py`, `ui/menus.py` | REAL | Route `/portfolioallocation` and the sidebar entry |
| `ui/pages/settings.py` | REAL | Symbol normalisation on the import + add paths; allocation-data cleanup on account deletion |
| `ui/pages/overview.py` | REAL | Persist the growth-chart label selection in `app.storage.user` |
| `alembic/versions/f1a7c2e9b4d0_*.py` | REAL — **new** | Merge duplicate instruments, add `ix_instrument_name` |
| `alembic/versions/f1c8a24b7e05_*.py` | REAL — **new** | Create the five allocation tables |
| `requirements.txt` | REAL | Pin `tastytrade==12.0.2` and `alpaca-py==0.43.2` |
| `tests/conftest.py` | REAL | Register the five new models |
| `ba2_trade_platform/version.py` | REAL | `APP_VERSION` bump (trade app) |
| `testplatform/version.py` | REAL (new) | `TEST_APP_VERSION` — decouples GA-worker sync from trade-app bumps |
| `testplatform/backend/app/services/self_update.py` | REAL | read the test platform's own version, not the trade app's |

---

## Task order and dependencies

```
A (instrument uniqueness)  ─┐
                            ├─► B (models + store) ──► F (page) ──┐
C (pure engine)  ───────────┘                                     ├─► G (wizard + submission)
                                                                  │
D (account seams) ──► E (TastyTrade)  ────────────────────────────┘

H (growth-chart persistence) — independent, any time
```

- **A** (Tasks 1-6) and **C** (Tasks 16-26) are independent of everything and of each other. Start with either, or both in parallel.
- **B** (Tasks 7-15) depends on **A**: its Alembic revision `f1c8a24b7e05` chains off A's revision id `f1a7c2e9b4d0`.
- **D** (Tasks 27-35) depends only on the pinned contracts (it creates `account_types.py`); **C Task 16** also imports `MarginInfo`/`OrderImpact` from it, so **D Task 27 must land before C Task 16**.
- **E** (Tasks 36-55) depends on **D**'s seams (Tasks 30, 32-33 in particular).
- **F** (Tasks 56-67) depends on **B** (the store and the models) and on **C Task 26** (the engine shim, for `PositionState` and `PositionFetchFailed`).
- **G** (Tasks 68-75) depends on **B**, **C**, **D** and **F**.
- **H** (Task 76) is independent.
- **I** (Tasks 77-78, the trade/test version split) is independent of everything else and may be done at any point.
- **Task 79** (bump both versions + full sweep) is last.

**Practical ordering for a single worker:** Task 27 (account value objects) → Section A → Section C → Section B → the rest of Section D → Section E → Section F → Section G → Section H → Section I → Task 79.

---

## Section A — Instrument uniqueness (foundation)

This section makes `instrument.name` a real primary identifier: every write normalises the
symbol, duplicate rows are merged by a shared (and therefore testable) function, and a unique
index enforces it forever after. Everything downstream in this plan resolves instruments by
name, so nothing else can be trusted until this is done.

**Two deviations from the design doc, both deliberate:**

1. The index is created as **`ix_instrument_name`**, not the doc's `uix_instrument_name`.
   `Instrument.name` becomes `Field(unique=True, index=True)`, and SQLModel's `create_all`
   emits exactly `CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)` on a fresh
   database (verified by probing a scratch SQLModel class). Naming the migration's index
   differently would leave a migrated database and a freshly-created one permanently
   disagreeing.
2. The merge groups rows by the **normalised** name (`.strip().upper()`), not the raw stored
   name. A unique index does not stop `aapl` and `AAPL` from coexisting, and once the helpers
   normalise their lookups (Task 1) a legacy lowercase row becomes unreachable — orphaned data
   with a label nobody can read. All live names are already uppercase, so on production this
   grouping is a no-op; it only protects against the rows the old code could have written.

---

### Task 1: Normalise symbols in the four shared label helpers

`add_label_to_instruments` / `remove_label_from_instruments` resolve a symbol with
`.first()`, and `get_labels_by_symbol` keys the result by the stored name. Feed any of them a
differently-cased symbol today and you silently create or miss a row. This task adds the pure
helper `normalize_symbol`, routes all four helpers through it, and resynchronises the
`overview.py` read path so it looks up by the same normalised key the helpers now return.

**Files:**
- Modify: `packages/common/ba2_common/core/utils.py:19-94`
- Modify: `packages/common/tests/test_utils_pure.py:52` and `:68`
- Modify: `ba2_trade_platform/ui/pages/overview.py` (5 lookup sites — see Step 3b)
- Test: `tests/test_instrument_labels.py`

- [ ] **Step 1: Write the failing test**

Append these seven methods to the end of the existing `class TestInstrumentLabels` in
`tests/test_instrument_labels.py` (the file already imports `select`, `get_db`, `Instrument`
and the four helpers at module level, and already defines the `_labels()` helper):

```python
    def test_normalize_symbol_strips_and_uppercases(self):
        from ba2_trade_platform.core.utils import normalize_symbol
        assert normalize_symbol('  aapl ') == 'AAPL'
        assert normalize_symbol('AAPL') == 'AAPL'
        assert normalize_symbol(None) == ''
        assert normalize_symbol('   ') == ''

    def test_normalize_symbol_rejects_non_strings(self):
        from ba2_trade_platform.core.utils import normalize_symbol
        assert normalize_symbol(123) == ''
        assert normalize_symbol(False) == ''
        assert normalize_symbol({'a': 1}) == ''

    def test_add_label_ignores_non_string_symbols(self):
        """A non-string symbol must never fabricate an Instrument row."""
        assert add_label_to_instruments([0], 'tech') == 0
        assert _labels('0') is None

    def test_add_label_stores_normalised_symbol(self):
        assert add_label_to_instruments(['  aapl  '], 'tech') == 1
        assert _labels('AAPL') == ['tech']
        assert _labels('  aapl  ') is None

    def test_add_label_twice_with_different_case_updates_one_row(self):
        add_label_to_instruments(['nflx'], 'streaming')
        add_label_to_instruments(['NFLX'], 'megacap')
        with get_db() as s:
            rows = s.exec(select(Instrument).where(Instrument.name == 'NFLX')).all()
        assert len(rows) == 1
        assert sorted(rows[0].labels) == ['megacap', 'streaming']

    def test_remove_label_matches_symbol_case_insensitively(self):
        add_label_to_instruments(['ORCL'], 'db')
        assert remove_label_from_instruments([' orcl '], 'db') == 1
        assert _labels('ORCL') == []

    def test_get_labels_by_symbol_normalises_query_and_keys(self):
        add_label_to_instruments(['AMZN'], 'retail')
        assert get_labels_by_symbol([' amzn ']) == {'AMZN': ['retail']}
```

Also extend the expected-helper list in `packages/common/tests/test_utils_pure.py`. Replace the
last line of the `expected = [...]` literal (currently `        "get_expert_options_for_ui",`)
with:

```python
        "get_expert_options_for_ui",
        "normalize_symbol", "parse_instrument_symbol_list",
```

and change that test's docstring (line 52) from
`    """All 24 pure-subset helpers named in the plan's split list are present."""` to:

```python
    """Every pure-subset helper named in the plan's split list, plus the symbol helpers."""
```

`parse_instrument_symbol_list` is added in Task 2; the list edit is done here so the two gates
in that file are touched once. Task 57 appends `get_symbols_by_label` to the same list and
does **not** touch the docstring again.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_labels.py -v`
Expected: FAIL — `test_normalize_symbol_strips_and_uppercases` errors with
`ImportError: cannot import name 'normalize_symbol' from 'ba2_trade_platform.core.utils'`, and
`test_add_label_stores_normalised_symbol` fails with `AssertionError: assert None == ['tech']`.
The two non-string tests fail the same way against a `str()`-coercing draft:
`assert '123' == ''` and `assert 1 == 0` (the `1` is a fabricated `Instrument(name='0')`).

- [ ] **Step 3: Write minimal implementation**

In `packages/common/ba2_common/core/utils.py`, replace lines 19-94 (the four label helpers)
with the following. `normalize_symbol`, the module-private `_normalized_symbols` and
`parse_instrument_symbol_list` are inserted ahead of
them so the helpers can use them, and the lazy `from ba2_common.core.models import Instrument`
inside every function body is preserved — `packages/common/tests/test_utils_pure.py:32-38` runs
a subprocess gate asserting that importing this module pulls in no models/providers/nicegui.

```python
def normalize_symbol(symbol) -> str:
    """Normalise an instrument symbol to its one canonical stored form.

    ``instrument.name`` will be UNIQUE once the merge migration lands, but a
    unique index does not stop ``aapl`` and ``AAPL`` from coexisting --
    uniqueness is only real if every read and write goes through here first.
    ``None``, blanks and non-strings collapse to ``""``; callers drop empties
    rather than writing a nameless Instrument. Non-strings are NOT coerced with
    ``str()``: that would turn ``0`` into a real ``Instrument(name='0')`` and
    report success. These helpers take user input from the Settings UI, so a bad
    symbol is dropped rather than raised on.
    """
    if not isinstance(symbol, str):
        return ""
    return symbol.strip().upper()


def _normalized_symbols(symbols) -> List[str]:
    """Sorted, de-duplicated normalised symbols, with empties dropped."""
    return sorted({n for s in symbols if (n := normalize_symbol(s))})


def parse_instrument_symbol_list(text) -> List[str]:
    """Parse a pasted/uploaded symbol list (one per line) into stored symbols.

    Blank lines are dropped, every symbol is normalised, and duplicates are
    removed while preserving first-seen order -- so one import file can never
    ask for two rows of the same instrument.
    """
    if not text:
        return []
    out: List[str] = []
    for line in str(text).splitlines():
        symbol = normalize_symbol(line)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def get_labels_by_symbol(symbols) -> Dict[str, List[str]]:
    """Return ``{symbol: [labels]}`` for symbols that have an Instrument row.

    Both the lookup and the returned keys are normalised (.strip().upper()), so
    ``get_labels_by_symbol(['aapl'])`` finds the ``AAPL`` row and returns it under
    ``'AAPL'``. Symbols without an Instrument (or with no labels) are simply
    omitted, so the caller can default to an empty list.
    """
    from ba2_common.core.models import Instrument
    syms = _normalized_symbols(symbols)
    if not syms:
        return {}
    out: Dict[str, List[str]] = {}
    with get_db() as session:
        rows = session.exec(select(Instrument).where(Instrument.name.in_(syms))).all()
        for inst in rows:
            out[normalize_symbol(inst.name)] = list(inst.labels or [])
    return out


def get_all_instrument_labels() -> List[str]:
    """Return the sorted, de-duplicated set of all labels in use across instruments.

    Labels are stripped and blanks dropped, so a legacy row holding ``' tech '``
    and a new one holding ``'tech'`` collapse to a single entry.
    """
    from ba2_common.core.models import Instrument
    labels = set()
    with get_db() as session:
        for inst in session.exec(select(Instrument)).all():
            for lbl in (inst.labels or []):
                cleaned = (lbl or "").strip()
                if cleaned:
                    labels.add(cleaned)
    return sorted(labels)


def add_label_to_instruments(symbols, label: str) -> int:
    """Add ``label`` to each symbol's Instrument, creating a minimal Instrument row
    when one doesn't exist. No-op for a blank label or a label already present.

    Symbols are normalised (.strip().upper()) before the lookup AND the insert, so
    ``['aapl']`` updates the existing ``AAPL`` row instead of creating a second one
    that the unique index would reject.

    The labels list is REASSIGNED (not mutated in place) so SQLAlchemy reliably
    detects the change on the JSON column. Returns the number of instruments
    created or updated.
    """
    from ba2_common.core.models import Instrument
    label = (label or "").strip()
    if not label:
        return 0
    changed = 0
    with get_db() as session:
        for sym in _normalized_symbols(symbols):
            inst = session.exec(select(Instrument).where(Instrument.name == sym)).first()
            if inst is None:
                session.add(Instrument(name=sym, labels=[label]))
                changed += 1
            elif label not in (inst.labels or []):
                inst.labels = list(inst.labels or []) + [label]
                session.add(inst)
                changed += 1
        if changed:
            session.commit()
    return changed


def remove_label_from_instruments(symbols, label: str) -> int:
    """Remove ``label`` from each symbol's Instrument (if present). Returns the
    number of instruments updated. Symbols are normalised (.strip().upper()) before
    the lookup, and the labels list is reassigned for change detection on the JSON
    column."""
    from ba2_common.core.models import Instrument
    label = (label or "").strip()
    if not label:
        return 0
    changed = 0
    with get_db() as session:
        for sym in _normalized_symbols(symbols):
            inst = session.exec(select(Instrument).where(Instrument.name == sym)).first()
            if inst and label in (inst.labels or []):
                inst.labels = [l for l in inst.labels if l != label]
                session.add(inst)
                changed += 1
        if changed:
            session.commit()
    return changed
```

No import changes are needed: `Dict`, `List`, `select` and `get_db` are already imported at the
top of the file. No shim change is needed either — `ba2_trade_platform/core/utils.py` does
`from ba2_common.core.utils import *`, so both new public helpers are re-exported automatically
(`_normalized_symbols` is private, so `import *` skips it and the purity gate's exported-helper
list does not need it).

- [ ] **Step 3b: Resynchronise the `overview.py` read path**

`get_labels_by_symbol` now returns NORMALISED keys, but all five callers still index the dict
with the raw symbol. Before this task both sides were raw — consistently wrong but consistent;
leaving them raw now means a non-uppercase symbol writes a label the next read cannot see. At
the three `or ['Unlabeled']` sites the miss is silent: the position's market value is
mis-bucketed in the label allocation chart with no error.

In `ba2_trade_platform/ui/pages/overview.py`, add `normalize_symbol` to the existing
`from ...core.utils import ...` on line 11, then wrap the lookup key at all five sites:

| Line | Before | After |
| --- | --- | --- |
| 1385 | `labels_by_symbol.get(p.get('symbol'), [])` | `labels_by_symbol.get(normalize_symbol(p.get('symbol')), [])` |
| 1580 | `refreshed.get(row.get('symbol'), [])` | `refreshed.get(normalize_symbol(row.get('symbol')), [])` |
| 5245 | `labels_by_symbol.get(sym)` | `labels_by_symbol.get(normalize_symbol(sym))` |
| 5259 | `labels_by_symbol.get(div.get('symbol'))` | `labels_by_symbol.get(normalize_symbol(div.get('symbol')))` |
| 5315 | `symbol_labels.get(pos.symbol)` | `symbol_labels.get(normalize_symbol(pos.symbol))` |

Only the dict lookup changes. The surrounding raw-symbol uses stay raw: the
`if row.get('symbol') in symbols` membership test at 1579 compares raw against the raw selected
list, and `symbol_info.setdefault(pos.symbol, ...)` at 5316 is a local grouping keyed
consistently by the raw symbol.

Out of scope: `overview.py:5775-5796` and `:6098-6111` build their own `symbol_labels` dict
directly from `Instrument` rows, bypassing `get_labels_by_symbol` and this normalisation
entirely. Task 76 owns that chart and fixes them there.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_labels.py -v`
Expected: PASS (15 passed — the 8 original tests plus the 7 new ones).

Then run the package gates, which must also stay green:
Run: `PYTHONPATH=packages/common:packages/providers:packages/experts venv/bin/python -m pytest packages/common/tests/test_utils_pure.py -v`
Expected: PASS (the subprocess leak gate proves `normalize_symbol` did not drag models in).
The `PYTHONPATH` prefix is REQUIRED: that gate shells out with `subprocess.run([sys.executable,
"-c", ...])`, and the subprocess does not inherit pytest's `pythonpath` ini, so without it the
child dies with `ModuleNotFoundError: No module named 'ba2_common'` and the gate fails for a
reason unrelated to the code under test (it fails that way at HEAD too).

There are no `tests/test_overview*.py` files, so Step 3b is covered only by import/compile
checks: `venv/bin/python -m py_compile ba2_trade_platform/ui/pages/overview.py` plus
`venv/bin/python -m pytest tests/test_boot_smoke.py tests/test_phase6_golden.py -q`.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/utils.py packages/common/tests/test_utils_pure.py tests/test_instrument_labels.py ba2_trade_platform/ui/pages/overview.py
git commit -m "feat(instruments): normalise symbols in the shared label helpers"
```

---

### Task 2: Normalise the two Settings instrument-creation paths

`ui/pages/settings.py` creates Instrument rows in two places: the `.txt` import upload
(`:367`) and the add/edit dialog (`:469`). Both store `name_input.value` verbatim. The upload
handler is a closure inside a NiceGUI dialog and cannot be reached from a test, so its parsing
is moved into the pure `parse_instrument_symbol_list` helper (added in Task 1), which is where
the test bites.

**Files:**
- Modify: `ba2_trade_platform/ui/pages/settings.py:11`, `:367`, `:379`, `:469`, `:479`, `:488`
- Test: `tests/test_instrument_symbol_import.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_instrument_symbol_import.py`:

```python
"""The Settings > Instruments import path must normalise the symbols it stores.

`parse_instrument_symbol_list` is the pure helper behind the .txt upload in
ui/pages/settings.py. The upload handler itself is a closure inside a NiceGUI
dialog and is unreachable from a test, so the parsing contract is pinned here and
the UI simply calls it.
"""
from ba2_trade_platform.core.utils import parse_instrument_symbol_list


def test_parse_symbol_list_uppercases_and_strips_each_line():
    assert parse_instrument_symbol_list("aapl\n  msft  \nNvDa") == ['AAPL', 'MSFT', 'NVDA']


def test_parse_symbol_list_drops_blank_and_whitespace_only_lines():
    assert parse_instrument_symbol_list("AAPL\n\n   \nMSFT\n") == ['AAPL', 'MSFT']


def test_parse_symbol_list_dedupes_case_variants_preserving_first_seen_order():
    assert parse_instrument_symbol_list("msft\nAAPL\nMSFT\n aapl ") == ['MSFT', 'AAPL']


def test_parse_symbol_list_empty_input_returns_empty_list():
    assert parse_instrument_symbol_list("") == []
    assert parse_instrument_symbol_list(None) == []


def test_settings_module_binds_normalisers():
    """The settings module must still import and actually bind the normalisers.

    The write sites are closures inside NiceGUI dialogs and unreachable from a
    test, so they are covered by inspection -- but the import itself is cheaply
    testable, and a missed re-export through the core.utils split-shim would
    otherwise only surface as an ImportError in production.
    """
    import ba2_trade_platform.ui.pages.settings as s

    assert s.normalize_symbol("  aapl ") == "AAPL"
    assert s.parse_instrument_symbol_list("aapl\nAAPL") == ["AAPL"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_symbol_import.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'parse_instrument_symbol_list' from 'ba2_trade_platform.core.utils'`
— unless Task 1 is already committed, in which case all four tests PASS immediately and you go
straight to the Step 3 edits (they are the point of this task; the helper is merely their
contract).

- [ ] **Step 3: Write minimal implementation**

Edit 1 — `ba2_trade_platform/ui/pages/settings.py:11`, extend the existing import:

```python
from ...core.utils import get_account_instance_from_id, get_expert_instance_from_id, normalize_symbol, parse_instrument_symbol_list
```

Edit 2 — line 367, inside `handle_upload`, replace:

```python
                        names = [line.strip() for line in content.splitlines() if line.strip()]
```

with:

```python
                        # instrument.name is UNIQUE: normalise + de-duplicate the file
                        # so an import can never ask for two rows of one instrument.
                        names = parse_instrument_symbol_list(content)
```

Edit 3 — line 379, so a legacy differently-cased row still matches, replace:

```python
                        existing_instruments = {inst.name: inst for inst in session.exec(select(Instrument)).all()}
```

with:

```python
                        existing_instruments = {normalize_symbol(inst.name): inst for inst in session.exec(select(Instrument)).all()}
```

Edit 4 — in the dialog's `save()`, hoist the normalisation above the `if is_edit:` branch so
the log lines and the writes can never disagree, replace:

```python
                    if is_edit:
                        logger.debug(f'Editing instrument {instrument.id}: {name_input.value}')
                        instrument.name = name_input.value
```

with:

```python
                    # Normalise once, then log and store the same value: a log line
                    # saying '  aapl ' next to a row holding 'AAPL' is a debugging trap.
                    name = normalize_symbol(name_input.value)
                    if is_edit:
                        logger.debug(f'Editing instrument {instrument.id}: {name}')
                        instrument.name = name
```

Edit 5 — in the dialog's `save()` create branch, use that same `name` for both the log lines
and the write, replace:

```python
                        logger.debug(f'Adding new instrument: {name_input.value}')
                        inst = Instrument(
                            name=name_input.value,
```

with:

```python
                        logger.debug(f'Adding new instrument: {name}')
                        inst = Instrument(
                            name=name,
```

and replace:

```python
                        logger.info(f'Instrument {name_input.value} added')
```

with:

```python
                        logger.info(f'Instrument {name} added')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_symbol_import.py -v`
Expected: PASS (5 passed).

Then verify the UI edits actually landed (the closures have no test coverage):
Run: `grep -n "parse_instrument_symbol_list\|normalize_symbol" ba2_trade_platform/ui/pages/settings.py`
Expected: exactly 4 lines — the import at `:11`, `names = parse_instrument_symbol_list(content)`,
the `existing_instruments` dict comprehension, and the hoisted
`name = normalize_symbol(name_input.value)` in the dialog's `save()`. (The two dialog write
sites now read that one `name` local, so they do not appear in this grep; confirm them by eye.)

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/pages/settings.py tests/test_instrument_symbol_import.py
git commit -m "feat(ui): normalise instrument symbols on the Settings import and add paths"
```

---

### Task 3: Normalise the two background instrument-creation paths

> **Also normalise the un-normalised instrument READ at `ba2_trade_platform/ui/pages/settings.py` (the file's only `Instrument.name == symbol`; it was `:2082` when the plan was written and is `:2085` after Task 2's hoist — grep for it rather than trusting the number).**
> It does `select(Instrument).where(Instrument.name == symbol)` when loading expert instrument
> config. Every WRITE path normalises as of Tasks 1-2, so this read can now miss a row it should
> find. It is one line — wrap the comparison value in `normalize_symbol(...)`. `normalize_symbol` is
> already imported in that module at `:11`.


`InstrumentAutoAdder._add_instrument_if_missing` and the auto-add block inside
`JobManager.submit_market_analysis` both do "SELECT by name, INSERT if missing" with the raw
symbol. The JobManager block is buried in a 200-line method that needs a live worker queue, so
it is lifted verbatim into a module-level `ensure_instrument_exists()` — which is testable and
is the only behaviour change worth having there.

> **Known limitation, deliberately out of scope** (spec "Risks"): `InstrumentAutoAdder.py:96-101`
> appends to `existing.labels` *in place* on a plain JSON column with no `MutableList` wrapper,
> so SQLAlchemy records no history and the commit emits no UPDATE — every label the auto-adder
> tries to add to an *existing* instrument is silently lost. Fixing it would start persisting
> thousands of expert labels and further pollute the label list, which deserves its own
> decision. Do NOT fix it here; it is called out because this task touches the same file.

**Files:**
- Modify: `ba2_trade_platform/core/InstrumentAutoAdder.py:13-17`, `:84-90`
- Modify: `ba2_trade_platform/core/JobManager.py:95` (insert), `:366-385` (replace)
- Test: `tests/test_instrument_autoadd_normalisation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_instrument_autoadd_normalisation.py`:

```python
"""Background instrument creation must store one normalised row per symbol.

Both writers are exercised for real against the in-memory test DB: the
InstrumentAutoAdder coroutine (with Yahoo lookup stubbed, so the test is offline)
and JobManager.ensure_instrument_exists.
"""
import asyncio

from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.InstrumentAutoAdder import InstrumentAutoAdder
from ba2_trade_platform.core.JobManager import ensure_instrument_exists


def _names():
    with get_db() as session:
        return sorted(i.name for i in session.exec(select(Instrument)).all())


def _run_auto_add(symbol):
    """Drive one auto-add with the network call replaced by a canned payload."""
    adder = InstrumentAutoAdder()

    async def fake_fetch(sym):
        return {'name': sym, 'category': 'Technology', 'company_name': 'Fake Corp'}

    adder._fetch_instrument_data = fake_fetch
    asyncio.run(adder._add_instrument_if_missing(symbol, 'expert-1', 'expert', []))


def test_auto_added_instrument_is_stored_under_the_normalised_name():
    _run_auto_add('  aapl  ')
    assert _names() == ['AAPL']


def test_auto_add_of_a_case_variant_updates_the_existing_row():
    _run_auto_add('AAPL')
    _run_auto_add('aapl')
    assert _names() == ['AAPL']
    with get_db() as session:
        inst = session.exec(select(Instrument).where(Instrument.name == 'AAPL')).first()
    assert inst.labels.count('expert-1') == 1


def test_auto_add_of_a_blank_symbol_creates_nothing():
    _run_auto_add('   ')
    assert _names() == []


def test_ensure_instrument_exists_creates_the_normalised_row_and_returns_it():
    assert ensure_instrument_exists(' tsla ') == 'TSLA'
    assert _names() == ['TSLA']


def test_ensure_instrument_exists_of_a_blank_symbol_creates_nothing():
    assert ensure_instrument_exists('  ') == ''
    assert _names() == []


def test_ensure_instrument_exists_is_idempotent_across_case():
    ensure_instrument_exists('TSLA')
    ensure_instrument_exists('tsla')
    assert _names() == ['TSLA']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_autoadd_normalisation.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'ensure_instrument_exists' from 'ba2_trade_platform.core.JobManager'`.
(After you add that function but before normalising the auto-adder, the first test fails with
`AssertionError: assert ['  aapl  '] == ['AAPL']`.)

- [ ] **Step 3: Write minimal implementation**

Edit 1 — `ba2_trade_platform/core/InstrumentAutoAdder.py`, add one import after line 15
(`from ..logger import logger`). Import straight from the package, NOT from
`..core.utils`: the in-tree split shim also pulls the expert/account registries, which is a
known circular-import trap, while `ba2_common.core.utils` is leak-gated pure code. Every other
import in that file goes through the in-tree shims, so record the reason in a comment — otherwise
a future reader "tidies" it back to `..core.utils` and drags the registries into a background
thread.

```python
# straight from the package: ..core.utils would drag in the expert/account registries
from ba2_common.core.utils import normalize_symbol
```

Edit 2 — `ba2_trade_platform/core/InstrumentAutoAdder.py:84-90`, replace:

```python
    async def _add_instrument_if_missing(self, symbol: str, expert_shortname: str, source: str, extra_labels: list = None):
        """Add instrument to database if it doesn't exist."""
        try:
            # Check if instrument already exists
            with get_db() as session:
                stmt = select(Instrument).where(Instrument.name == symbol)
                existing = session.exec(stmt).first()
```

with:

```python
    async def _add_instrument_if_missing(self, symbol: str, expert_shortname: str, source: str, extra_labels: list = None):
        """Add instrument to database if it doesn't exist."""
        try:
            # instrument.name is UNIQUE: normalise BEFORE the lookup and the insert,
            # so ' aapl ' resolves to the existing AAPL row instead of inserting a
            # second one the unique index would reject.
            symbol = normalize_symbol(symbol)
            if not symbol:
                logger.warning("InstrumentAutoAdder: blank symbol skipped")
                return

            # Check if instrument already exists
            with get_db() as session:
                stmt = select(Instrument).where(Instrument.name == symbol)
                existing = session.exec(stmt).first()
```

Edit 3 — `ba2_trade_platform/core/JobManager.py`. First extend the module header: add `get_db` to
the existing `from .db import ...` (`:26`) and add `from sqlmodel import select` plus
`from ba2_common.core.utils import normalize_symbol`. Unlike the auto-adder, nothing here can
cycle — `:24` already does `from ..core.utils import get_expert_instance_from_id` at module
scope, so the shim and its registries are loaded regardless.

Then insert this module-level function after line 95 (after `should_schedule_open_positions`,
before `class ControlMessageType`):

```python
def ensure_instrument_exists(symbol: str) -> str:
    """Create the ``auto_added`` Instrument row for ``symbol`` if it is missing.

    Lifted out of ``JobManager.submit_market_analysis`` so it can be tested: that
    method needs a live worker queue and an enabled expert before it ever reaches
    the auto-add. Behaviour is unchanged apart from normalisation.

    ``instrument.name`` is UNIQUE, so the symbol is normalised (.strip().upper())
    before BOTH the lookup and the insert, and the normalised form is returned for
    the caller to use downstream.

    Args:
        symbol: raw symbol, any case, possibly padded.

    Returns:
        str: the normalised symbol (``""`` for a blank input, in which case
        nothing is written).
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        logger.warning("ensure_instrument_exists: blank symbol, nothing to add")
        return symbol

    with get_db() as session:
        existing_instrument = session.exec(
            select(Instrument).where(Instrument.name == symbol)
        ).first()
        if not existing_instrument:
            session.add(Instrument(
                name=symbol,
                instrument_type='stock',  # Default to stock
                categories=[],
                labels=['auto_added'],
            ))
            session.commit()
            logger.info(f"Auto-added instrument '{symbol}' to database with label 'auto_added'")
    return symbol
```

Edit 4 — `ba2_trade_platform/core/JobManager.py:366-385`, replace the whole inline auto-add
block:

```python
        # Auto-add instrument if it doesn't exist in database
        from .models import Instrument
        from .db import get_db
        from sqlmodel import Session, select
        
        with get_db() as session:
            statement = select(Instrument).where(Instrument.name == symbol)
            existing_instrument = session.exec(statement).first()
            
            if not existing_instrument:
                # Create new instrument with auto_added label
                new_instrument = Instrument(
                    name=symbol,
                    instrument_type='stock',  # Default to stock
                    categories=[],
                    labels=['auto_added']
                )
                session.add(new_instrument)
                session.commit()
                logger.info(f"Auto-added instrument '{symbol}' to database with label 'auto_added'")
```

with:

```python
        # Auto-add instrument if it doesn't exist in database. The normalised
        # symbol is used from here on, so the analysis and the Instrument row
        # always agree on spelling.
        symbol = ensure_instrument_exists(symbol)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_autoadd_normalisation.py -v`
Expected: PASS (6 passed).

Run: `venv/bin/python -m pytest tests/test_job_scheduling.py -v`
Expected: PASS (the JobManager module still imports and its existing helpers are untouched).

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/core/InstrumentAutoAdder.py ba2_trade_platform/core/JobManager.py tests/test_instrument_autoadd_normalisation.py
git commit -m "feat(instruments): normalise symbols in the auto-adder and JobManager auto-add"
```

---

### Task 4: The reusable duplicate-instrument merge

**Where it lives and why:** `packages/common/ba2_common/core/instrument_merge.py`. It is shared
code, so per CLAUDE.md it belongs in the package rather than in-tree; and putting it there
(instead of inlining the SQL in the Alembic revision) means the migration and its tests run the
*same* code — a migration that inlines its own merge is a migration nobody can test. It takes a
SQLAlchemy `Connection` and uses raw SQL rather than the ORM for two reasons: it runs *inside* a
migration, where the ORM `Instrument` model already declares `unique=True` while the database
does not yet have the index, and where importing the app engine would open a second connection
to the wrong database. Per the mandatory shim rule for new shared modules, an in-tree alias shim
is added alongside it.

**Files:**
- Create: `packages/common/ba2_common/core/instrument_merge.py`
- Create: `ba2_trade_platform/core/instrument_merge.py` (SHIM)
- Test: `tests/test_instrument_merge.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_instrument_merge.py`:

```python
"""Merging duplicate `instrument` rows before `name` becomes unique.

The fixture database is built with RAW SQL, not SQLModel.metadata.create_all:
once `Instrument.name` is unique, create_all emits the unique index and the
duplicate rows this whole module is about could not be inserted at all. The table
definition below is the live pre-migration schema, verbatim.

These tests never touch a real database -- every one gets its own tmp_path file.
"""
import json
import sqlite3

import pytest
from sqlalchemy import create_engine, text

from ba2_trade_platform.core.instrument_merge import (
    merge_duplicate_instruments,
    report_duplicate_instruments,
)

_CREATE = (
    "CREATE TABLE instrument ("
    " id INTEGER NOT NULL,"
    " name VARCHAR NOT NULL,"
    " instrument_type VARCHAR(6),"
    " categories JSON,"
    " labels JSON,"
    " company_name VARCHAR,"
    " PRIMARY KEY (id))"
)
_INSERT = text(
    "INSERT INTO instrument (id, name, instrument_type, categories, labels, company_name)"
    " VALUES (:id, :name, :instrument_type, :categories, :labels, :company_name)"
)


def _make_db(tmp_path, rows):
    """rows: list of (id, name, instrument_type, categories, labels, company_name)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'instruments.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        for row in rows:
            conn.execute(_INSERT, {
                "id": row[0], "name": row[1], "instrument_type": row[2],
                "categories": None if row[3] is None else json.dumps(row[3]),
                "labels": None if row[4] is None else json.dumps(row[4]),
                "company_name": row[5],
            })
    return engine


def _write_raw_labels(engine, row_id, raw):
    """Store a labels value EXACTLY as given, bypassing the fixture's json.dumps.

    The malformed values live rows can hold cannot be produced by json.dumps.
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE instrument SET labels = :raw WHERE id = :id"),
                     {"raw": raw, "id": row_id})


def _dump(engine):
    with engine.connect() as conn:
        return [
            (r[0], r[1], r[2], r[3], json.loads(r[4] or "[]"), json.loads(r[5] or "[]"))
            for r in conn.execute(text(
                "SELECT id, name, instrument_type, company_name, categories, labels"
                " FROM instrument ORDER BY id"
            ))
        ]


def test_merge_keeps_lowest_id_and_coalesces_a_null_instrument_type(tmp_path):
    engine = _make_db(tmp_path, [
        (7, 'AAPL', None, [], ['ark26'], None),
        (9, 'AAPL', 'STOCK', [], [], 'Apple Inc'),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 1
    assert stats['rows_deleted'] == 1
    assert _dump(engine) == [(7, 'AAPL', 'STOCK', 'Apple Inc', [], ['ark26'])]


def test_merge_unions_disjoint_label_lists(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'MSFT', 'STOCK', ['Tech'], ['ark26'], None),
        (2, 'MSFT', 'STOCK', ['Software'], ['nasdaq30'], None),
    ])
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine) == [
        (1, 'MSFT', 'STOCK', None, ['Tech', 'Software'], ['ark26', 'nasdaq30'])
    ]


def test_merge_dedupes_overlapping_label_lists_preserving_order(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'NVDA', 'STOCK', [], ['semis', 'ark26'], None),
        (2, 'NVDA', 'STOCK', [], ['ark26', 'highrisk'], None),
    ])
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine)[0][5] == ['semis', 'ark26', 'highrisk']


def test_merge_collapses_three_rows_of_one_name_into_the_lowest_id(tmp_path):
    engine = _make_db(tmp_path, [
        (5, 'TSLA', None, [], ['a'], None),
        (6, 'TSLA', 'STOCK', [], ['b'], None),
        (7, 'TSLA', None, ['EV'], ['c', 'a'], 'Tesla Inc'),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['rows_deleted'] == 2
    assert _dump(engine) == [(5, 'TSLA', 'STOCK', 'Tesla Inc', ['EV'], ['a', 'b', 'c'])]


def test_merge_normalises_a_lower_case_name_into_its_upper_case_twin(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'META', 'STOCK', [], ['social'], None),
        (2, ' meta ', None, [], ['ark26'], None),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 1
    assert _dump(engine) == [(1, 'META', 'STOCK', None, [], ['social', 'ark26'])]


def test_merge_normalises_a_lone_badly_cased_name_without_deleting_it(tmp_path):
    engine = _make_db(tmp_path, [(3, ' ibm ', 'STOCK', [], ['legacy'], None)])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 0
    assert stats['rows_renamed'] == 1
    assert _dump(engine) == [(3, 'IBM', 'STOCK', None, [], ['legacy'])]


def test_merge_leaves_already_unique_rows_untouched(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'AAPL', 'STOCK', ['Tech'], ['ark26'], 'Apple Inc'),
        (2, 'MSFT', 'STOCK', [], [], None),
    ])
    before = _dump(engine)
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats == {'groups': 0, 'duplicate_groups': 0, 'rows_deleted': 0, 'rows_renamed': 0}
    assert _dump(engine) == before


def test_running_the_merge_twice_changes_nothing_the_second_time(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], ['a'], None),
        (2, 'AAPL', 'STOCK', ['Tech'], ['b'], 'Apple Inc'),
        (3, 'aapl', None, [], ['c'], None),
    ])
    with engine.begin() as conn:
        first = merge_duplicate_instruments(conn)
    after_first = _dump(engine)
    with engine.begin() as conn:
        second = merge_duplicate_instruments(conn)
    assert first['rows_deleted'] == 2
    assert second == {'groups': 0, 'duplicate_groups': 0, 'rows_deleted': 0, 'rows_renamed': 0}
    assert _dump(engine) == after_first


def test_dry_run_reports_the_same_work_but_writes_nothing(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], ['a'], None),
        (2, 'AAPL', 'STOCK', [], ['b'], None),
    ])
    before = _dump(engine)
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn, dry_run=True)
        plan = report_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 1 and stats['rows_deleted'] == 1
    assert plan[0]['name'] == 'AAPL'
    assert plan[0]['keep_id'] == 1 and plan[0]['delete_ids'] == [2]
    assert plan[0]['labels'] == ['a', 'b']
    assert _dump(engine) == before


def test_merge_tolerates_null_json_columns(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'GOOG', None, None, None, None),
        (2, 'GOOG', 'STOCK', None, ['x'], None),
    ])
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine) == [(1, 'GOOG', 'STOCK', None, [], ['x'])]


def test_merge_drops_malformed_label_values_instead_of_stringifying_them(tmp_path):
    """Non-strings must be DROPPED, never coerced into plausible-looking labels.

    ``str()`` would write a JSON null through as a real label named 'None' -- and
    only into rows the migration is already rewriting, so nobody would notice.
    """
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], [], None),
        (2, 'AAPL', 'STOCK', [], [], None),
        (3, 'AAPL', None, [], [], None),
    ])
    _write_raw_labels(engine, 1, '[1, 2.5, null, true, "keeper"]')   # non-string members
    _write_raw_labels(engine, 2, 'not json at all')                  # undecodable
    _write_raw_labels(engine, 3, '{"a": 1}')                         # decodes, but not a list
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine) == [(1, 'AAPL', 'STOCK', None, [], ['keeper'])]


def test_merge_keeps_the_lowest_ids_company_name_when_they_conflict(tmp_path):
    """Coalesce is first-non-null by id, so a conflict resolves to the keeper's."""
    engine = _make_db(tmp_path, [
        (4, 'AAPL', 'STOCK', [], [], 'Apple Inc'),
        (5, 'AAPL', 'STOCK', [], [], 'Apple Computer Inc'),
        (6, 'AAPL', 'STOCK', [], [], 'APPLE INC.'),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['rows_deleted'] == 2
    assert _dump(engine) == [(4, 'AAPL', 'STOCK', 'Apple Inc', [], [])]


def test_merge_writes_nothing_when_the_caller_never_commits(tmp_path):
    """The CALLER owns the transaction: connect() with no commit is a total no-op.

    Pinned because the stats come back fully populated either way -- the only
    signal that the merge was lost is the unchanged table.
    """
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], ['a'], None),
        (2, 'AAPL', 'STOCK', [], ['b'], None),
    ])
    before = _dump(engine)
    conn = engine.connect()
    stats = merge_duplicate_instruments(conn)
    conn.close()                        # no commit: SQLAlchemy 2.0 rolls back
    assert stats['rows_deleted'] == 1   # the work was reported...
    assert _dump(engine) == before      # ...but none of it survived


def test_merge_on_an_empty_table_is_a_no_op(tmp_path):
    engine = _make_db(tmp_path, [])
    with engine.begin() as conn:
        assert merge_duplicate_instruments(conn)['groups'] == 0
    assert _dump(engine) == []


def test_fixture_db_is_never_the_real_database(tmp_path):
    """Guard rail: the live production DB must never be opened by this module."""
    engine = _make_db(tmp_path, [])
    assert str(tmp_path) in str(engine.url)
    conn = sqlite3.connect(str(tmp_path / 'instruments.sqlite'))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {'instrument'}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_merge.py -v`
Expected: FAIL at collection with
`ModuleNotFoundError: No module named 'ba2_trade_platform.core.instrument_merge'`.

- [ ] **Step 3: Write minimal implementation**

Create `packages/common/ba2_common/core/instrument_merge.py`:

```python
"""Merge duplicate ``instrument`` rows so ``instrument.name`` can become unique.

Shared on purpose by BOTH the Alembic revision that runs it against the
production database and the tests that prove it correct: a migration that inlines
its own merge SQL is a migration nobody can test.

Raw SQL over a SQLAlchemy ``Connection``, not the ORM. This runs INSIDE a
migration, where the ORM ``Instrument`` model already declares ``unique=True``
while the database does not yet have the index, and where importing the app
engine would open a second connection to the wrong database.

Rows are grouped by the NORMALISED name (``.strip().upper()``), not the stored
one. A unique index alone does not stop ``aapl`` and ``AAPL`` from coexisting, and
once the label helpers normalise their lookups a leftover lower-case row is
unreachable -- orphaned data carrying labels nobody can read. Every live name is
already upper-case, so on production this grouping is a no-op.

Idempotent by construction: the plan is recomputed from the current table state on
every call, so a second run finds nothing and writes nothing.
"""
import json
from typing import Any, Dict, List

from sqlalchemy import text

from ba2_common.core.utils import normalize_symbol
from ba2_common.logger import logger

_SELECT_ROWS = text(
    "SELECT id, name, instrument_type, company_name, categories, labels "
    "FROM instrument ORDER BY id"
)
_UPDATE_ROW = text(
    "UPDATE instrument SET name = :name, instrument_type = :instrument_type, "
    "company_name = :company_name, categories = :categories, labels = :labels "
    "WHERE id = :id"
)
_DELETE_ROW = text("DELETE FROM instrument WHERE id = :id")


def _as_list(raw, *, row_id=None, column: str = "") -> List[str]:
    """Decode a JSON list column read through a RAW connection.

    SQLAlchemy's JSON type only decodes when its Core type is attached; a textual
    SELECT hands back the stored TEXT. Anything that is not a JSON list (NULL, an
    empty string, a stray scalar) decodes to ``[]``.

    Only genuine strings survive. Coercing members with ``str()`` would turn a
    JSON ``null`` into a real label named ``"None"`` and a nested ``["a"]`` into
    ``"['a']"`` -- plausible-looking garbage written into exactly the rows this
    migration rewrites, which is worse than dropping it. Every discard is logged
    with the row it came from, because a silent drop here is silent data loss.

    Args:
        raw: the stored column value: TEXT, an already-decoded list, or NULL.
        row_id: the ``instrument.id`` the value came from, named in the log.
        column: the column name (``"labels"`` / ``"categories"``), named in the log.

    Returns:
        List[str]: the string members of the decoded list, in stored order.
    """
    where = f"row id={row_id} {column}".strip()
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        decoded = raw
    else:
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"instrument merge: {where} holds undecodable JSON {raw!r}, treated as empty"
            )
            return []
    if decoded is None:          # a stored JSON `null` is just the ordinary empty case
        return []
    if not isinstance(decoded, list):
        logger.warning(
            f"instrument merge: {where} holds non-list JSON {raw!r}, treated as empty"
        )
        return []
    kept = [v for v in decoded if isinstance(v, str)]
    if len(kept) != len(decoded):
        dropped = [v for v in decoded if not isinstance(v, str)]
        logger.warning(
            f"instrument merge: {where} dropped {len(dropped)} non-string value(s) {dropped!r}"
        )
    return kept


def _union_preserving_order(lists) -> List[str]:
    """Concatenate lists, dropping repeats, keeping first-seen order."""
    out: List[str] = []
    for values in lists:
        for value in values:
            if value not in out:
                out.append(value)
    return out


def _first_non_null(values):
    """First value that is neither ``None`` nor an empty string, else ``None``."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def report_duplicate_instruments(connection) -> List[Dict[str, Any]]:
    """Describe every rewrite the table needs, WITHOUT writing anything.

    Args:
        connection: an open SQLAlchemy ``Connection`` (``op.get_bind()`` inside a
            migration). Read-only here, so no transaction is required.

    Returns:
        List[Dict[str, Any]]: one entry per group needing work, sorted by name,
        each with ``name`` (normalised), ``original_name`` (as stored on the
        keeper), ``keep_id`` (lowest id in the group), ``delete_ids``,
        ``instrument_type``, ``company_name``, ``categories`` and ``labels``
        (the merged values). Groups that are already a single correctly-named row
        are omitted, which is what makes a second run a no-op.
    """
    rows = connection.execute(_SELECT_ROWS).fetchall()
    groups: Dict[str, List[Any]] = {}
    for row in rows:
        groups.setdefault(normalize_symbol(row[1]), []).append(row)

    plan: List[Dict[str, Any]] = []
    for name in sorted(groups):
        members = groups[name]          # ordered by id, so members[0] is the keeper
        keeper = members[0]
        if len(members) == 1 and keeper[1] == name:
            continue
        plan.append({
            "name": name,
            "original_name": keeper[1],
            "keep_id": keeper[0],
            "delete_ids": [m[0] for m in members[1:]],
            "instrument_type": _first_non_null([m[2] for m in members]),
            "company_name": _first_non_null([m[3] for m in members]),
            "categories": _union_preserving_order(
                [_as_list(m[4], row_id=m[0], column="categories") for m in members]
            ),
            "labels": _union_preserving_order(
                [_as_list(m[5], row_id=m[0], column="labels") for m in members]
            ),
        })
    return plan


def merge_duplicate_instruments(connection, *, dry_run: bool = False) -> Dict[str, int]:
    """Collapse every duplicate ``instrument`` name onto its lowest id.

    For each group: keep the lowest id, coalesce ``instrument_type`` and
    ``company_name`` to the first non-null value, union ``labels`` and
    ``categories`` preserving order, delete the other rows.

    THE CALLER OWNS THE TRANSACTION. This function issues UPDATEs and DELETEs but
    never commits, so ``engine.connect()`` -> merge -> ``close()`` reports a full
    set of stats and silently writes NOTHING (SQLAlchemy 2.0 rolls back on close).
    Use ``engine.begin()``, or ``op.get_bind()`` inside a migration, which is
    already inside Alembic's transaction. That is deliberate: a failure part-way
    through must not leave the table half-merged.

    Args:
        connection: an open SQLAlchemy ``Connection`` in a transaction the caller
            will commit.
        dry_run: when True, compute and log the plan and write NOTHING.

    Returns:
        Dict[str, int]: ``groups`` (rows rewritten), ``duplicate_groups`` (groups
        that had more than one row), ``rows_deleted``, and ``rows_renamed``
        (groups whose SURVIVING name changed -- a group whose keeper was already
        normalised does not count, even when a mis-cased duplicate was deleted
        from it).
    """
    plan = report_duplicate_instruments(connection)
    stats = {
        "groups": len(plan),
        "duplicate_groups": sum(1 for g in plan if g["delete_ids"]),
        "rows_deleted": sum(len(g["delete_ids"]) for g in plan),
        "rows_renamed": sum(1 for g in plan if g["original_name"] != g["name"]),
    }

    if dry_run:
        logger.info(
            f"instrument merge DRY RUN: {stats['duplicate_groups']} duplicate group(s), "
            f"{stats['rows_deleted']} row(s) would be deleted, "
            f"{stats['rows_renamed']} name(s) would be normalised"
        )
        for group in plan:
            logger.info(
                f"instrument merge DRY RUN: {group['name']} keep id={group['keep_id']} "
                f"delete ids={group['delete_ids']} labels={group['labels']}"
            )
        return stats

    for group in plan:
        connection.execute(_UPDATE_ROW, {
            "id": group["keep_id"],
            "name": group["name"],
            "instrument_type": group["instrument_type"],
            "company_name": group["company_name"],
            "categories": json.dumps(group["categories"]),
            "labels": json.dumps(group["labels"]),
        })
        for dead_id in group["delete_ids"]:
            connection.execute(_DELETE_ROW, {"id": dead_id})

    logger.info(
        f"instrument merge: {stats['duplicate_groups']} duplicate group(s) merged, "
        f"{stats['rows_deleted']} row(s) deleted, {stats['rows_renamed']} name(s) normalised"
    )
    return stats
```

Create the in-tree alias shim `ba2_trade_platform/core/instrument_merge.py` — this is
`ba2_trade_platform/core/option_types.py` copied verbatim with only the module name swapped;
`tests/test_alias_shim_race.py` discovers every shim by its swap line and asserts this exact
ordering:

```python
"""Alias shim: this in-tree module IS ba2_common.core.instrument_merge (Phase 6 migration).

The in-tree path is aliased to the package module object in sys.modules so
existing ``from ba2_trade_platform...`` imports resolve unchanged AND
``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path
operate on the real package module. Single source of truth: ba2_common.core.instrument_merge."""
import importlib as _importlib
import sys as _sys

_pkg = _importlib.import_module("ba2_common.core.instrument_merge")
# RACE GUARD: mirror the package's names onto THIS module BEFORE swapping it out of
# sys.modules. The swap alone leaves the original module object permanently empty, so a
# second thread reaching a LAZY ``from .X import Y`` while the first is still executing
# this body gets that empty object and raises "cannot import name 'Y'". That silently
# killed a live Monday enter-market run on 2026-08-17; see
# docs/2026-08-17-alias-shim-race.md. Locals are captured first because the update copies
# the package namespace wholesale -- a package binding _sys/_pkg must not break the swap.
_modules, _me, _target = _sys.modules, __name__, _pkg
globals().update({k: v for k, v in vars(_pkg).items() if not k.startswith('__')})
_modules[_me] = _target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_merge.py -v`
Expected: PASS (15 passed).

Run: `venv/bin/python -m pytest tests/test_alias_shim_race.py -v`
Expected: PASS (the new shim satisfies the race-guard ordering checks).

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/instrument_merge.py ba2_trade_platform/core/instrument_merge.py tests/test_instrument_merge.py
git commit -m "feat(instruments): shared, idempotent duplicate-instrument merge"
```

---

### Task 5: The Alembic revision — merge, then the unique index

> **Verified state of the live DB (`~/Documents/ba2/trade/db.sqlite`), queried read-only on
> 2026-08-20 — you do not need to re-derive this:**
>
> | Query | Result |
> |---|---|
> | `COUNT(*) FROM instrument` | 2477 |
> | `COUNT(DISTINCT name)` | 2353 |
> | duplicate groups after normalising | **124** |
> | `trim(coalesce(name,'')) = ''` | **0** |
> | `name IS NULL` | **0** |
> | `name <> upper(trim(name))` | **0** |
>
> So on this database the merge does no renaming at all — it only collapses the 124 exact-duplicate
> groups — and the blank-name case cannot arise. Task 4 raised it because
> `merge_duplicate_instruments` groups by `normalize_symbol`, which maps blanks and non-strings to
> `""`, so several blank rows would collapse onto one row literally named `''` that then SATISFIES
> the unique index instead of tripping it. That is latent, not live: do NOT add blank-row deletion
> to this revision on account of it. Task 6 guards the write paths.

> **FIRST STEP OF THIS TASK: make alembic runnable at all.** On this machine `ba2_common` is not
> importable outside pytest — `venv/`'s editable install points at
> `/Users/bmigette/Documents/dev/BA2/BA2TradeCommon`, which does not exist, and only `pytest.ini`'s
> `pythonpath = packages/common packages/providers packages/experts` makes the imports resolve.
> `alembic/env.py:11` inserts only the repo root. Verified today:
>
> ```
> venv/bin/python -m alembic current
>   -> ModuleNotFoundError: No module named 'ba2_common'
>      (raised at ba2_trade_platform/config.py:6, before env.py reaches the models)
>
> PYTHONPATH=packages/common:packages/providers:packages/experts venv/bin/python -m alembic current
>   -> d5e1b9a3c842
> ```
>
> So `python migrate.py upgrade` is broken today, independently of this feature. Fix it in
> `alembic/env.py` next to the existing `sys.path.insert` — add the three `packages/*`
> directories the same way — so alembic and `migrate.py` are self-sufficient and nobody has to
> remember a prefix. Add a test that asserts `alembic current` exits 0 with a bare
> `venv/bin/python`. This is a prerequisite for testing your revision, not optional cleanup.
>
> **AS BUILT (Task 5).** The three directories are prepended, not appended: the existing
> repo-root line prepends, and so does pytest.ini's `pythonpath`, so *this* checkout's
> `packages/*` must win over the stale editable installs pointing elsewhere. They go on in one
> `sys.path[0:0] = [...]` slice — three separate `insert(0, ...)` calls would leave the
> REVERSED order (`experts, providers, common`) on `sys.path`, which is not what the tuple
> reads like. Verified order after the fix: `common, providers, experts, <repo root>`.
> The tradeoff, stated so it is not a surprise later: prepending puts `packages/*/tests` and
> the repo root's `tools`, `test_files` and `logs` ahead of site-packages, so any top-level
> module there now shadows a same-named installed distribution. Nothing collides today.
>
> **AS BUILT: this fix does not cover every alembic command.** `alembic heads` / `history` /
> `branches` (i.e. `migrate.py heads|history`) import every revision module but never run
> `env.py`, so the sys.path fix does not apply to them. A revision with a module-scope
> `import ba2_common` therefore breaks all three for everyone — observed, not theorised. This
> revision defers its merge import into `upgrade()` for exactly that reason, and
> `test_alembic_heads_is_this_revision_alone_without_a_pythonpath_prefix` locks it in. Any
> future revision importing app code must do the same, or `alembic.ini`'s `prepend_sys_path`
> must be extended (which needs `path_separator` changed off `os`, since this repo also runs
> on Windows).
>
> **AS BUILT: `migrate.py` still shells out to a bare `alembic` binary** (`subprocess.run`,
> `shell=True`), so `venv/bin/python migrate.py upgrade` fails with
> `/bin/sh: alembic: command not found` unless the venv is on `PATH`. Out of scope here and
> left alone; use `venv/bin/python -m alembic upgrade head`, or activate the venv first.


Head is `0a3e0bd24598` (verified: `venv/bin/python -m alembic heads` prints exactly
`0a3e0bd24598 (head)`, single head), so `down_revision = '0a3e0bd24598'`. The revision imports
the merge through the in-tree alias shim, exactly as `alembic/env.py:16` imports models, and
adds no new import surface. **AS BUILT: that import sits inside `upgrade()`, not at module
scope** — see the second AS BUILT note above; at module scope it breaks `alembic heads`. It
also carries a dry-run mode so the affected names can be inspected against the production
database before anything is written.

**This revision's id, `f1a7c2e9b4d0`, is the `down_revision` of Task 8's revision.**

**AS BUILT — three hardening changes after code review, all with tests:**

1. **The dry-run flag is fail-SAFE, not fail-dangerous.** A truthy whitelist
   (`1`/`true`/`yes`) meant `on`, `ON`, `Y`, `enabled`, `2` and `dry-run` all fell through and
   performed the real, irreversible merge. On the one flag whose entire job is to stop an
   operator deleting 124 production rows, an unrecognised value must never mean "go ahead".
   Inverted: only `''`, `0`, `false`, `no` disarm; everything else reports and aborts.
2. **The index guard checks the DEFINITION, not just the name.** An index merely *named*
   `ix_instrument_name` (non-unique, or over another column) used to satisfy the skip, so the
   migration reported success and stamped the revision while uniqueness was not enforced at
   all. It now raises. `downgrade()` got the same care — it refuses to drop an index it does
   not own rather than destroying someone else's.
3. **A production runbook lives in the revision docstring**, because `MIGRATIONS.md` only has
   generic advice: stop the app, back up (the only way back), catch up to `0a3e0bd24598`
   FIRST so the dry run is genuinely read-only, dry run (exit 1 + RuntimeError is SUCCESS),
   real run with the variable UNSET, verification queries, "if it fails just re-run — it rolls
   back atomically", and: **must not reach production before Task 6 ships in the same
   deployment.**

**Files:**
- Modify: `alembic/env.py` (the sys.path prerequisite above)
- Create: `alembic/versions/f1a7c2e9b4d0_merge_duplicate_instruments_unique_name.py`
- Test: `tests/test_instrument_unique_migration.py`

- [x] **Step 1: Write the failing test**

Create `tests/test_instrument_unique_migration.py`:

```python
"""The instrument-uniqueness migration, run for real against a fixture database.

Why importlib instead of `alembic upgrade`: in this codebase the base schema is
created by SQLModel.metadata.create_all, not by an initial migration, so
`alembic upgrade` from an empty sqlite file fails long before reaching this
revision. We build the pre-migration `instrument` table by hand (the live schema,
verbatim) and execute the real revision module's upgrade() bound to a live Alembic
Operations context -- the exact DDL and data SQL a production migration runs.

Every test uses its own tmp_path database. The live production DB is never opened.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest
import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_PATH = os.path.join(
    REPO, "alembic", "versions",
    "f1a7c2e9b4d0_merge_duplicate_instruments_unique_name.py",
)

_CREATE = (
    "CREATE TABLE instrument ("
    " id INTEGER NOT NULL,"
    " name VARCHAR NOT NULL,"
    " instrument_type VARCHAR(6),"
    " categories JSON,"
    " labels JSON,"
    " company_name VARCHAR,"
    " PRIMARY KEY (id))"
)
_INSERT = text(
    "INSERT INTO instrument (id, name, instrument_type, categories, labels, company_name)"
    " VALUES (:id, :name, :instrument_type, :categories, :labels, :company_name)"
)
_ROWS = [
    (1, 'AAPL', None, [], ['ark26'], None),
    (2, 'AAPL', 'STOCK', ['Tech'], ['nasdaq30'], 'Apple Inc'),
    (3, 'msft', 'STOCK', [], ['ark26'], None),
    (4, 'NVDA', 'STOCK', [], ['semis'], 'Nvidia Corp'),
]


def _load_migration_module():
    assert os.path.exists(MIGRATION_PATH), f"missing migration file {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("instrument_unique_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_premerge_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'premerge.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        for row in _ROWS:
            conn.execute(_INSERT, {
                "id": row[0], "name": row[1], "instrument_type": row[2],
                "categories": json.dumps(row[3]), "labels": json.dumps(row[4]),
                "company_name": row[5],
            })
    return engine


def _run_upgrade(engine, module):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.sa = sqlalchemy
        module.upgrade()


def _rows(engine):
    with engine.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(text("SELECT id, name FROM instrument ORDER BY id"))]


def _indexes(engine):
    with engine.connect() as conn:
        return {r[1]: r[2] for r in conn.execute(text("PRAGMA index_list(instrument)"))}


@pytest.fixture(autouse=True)
def _no_ambient_dry_run(monkeypatch):
    """Every test starts with BA2_INSTRUMENT_MERGE_DRY_RUN unset.

    The flag is fail-safe: anything that is not explicitly false means dry run. So
    a value left in the developer's shell would turn every real-run test in this
    file into a silent no-op that still passed its "nothing was written" asserts.
    The dry-run tests opt back in explicitly.
    """
    monkeypatch.delenv("BA2_INSTRUMENT_MERGE_DRY_RUN", raising=False)


def test_alembic_runs_without_a_pythonpath_prefix(tmp_path):
    """`alembic current` must work with a bare interpreter, no PYTHONPATH prefix.

    The Phase 6 packages (ba2_common/ba2_providers/ba2_experts) are only on
    sys.path for pytest, via pytest.ini's `pythonpath`; this checkout's editable
    installs point at an absolute path that does not exist here. Until env.py put
    packages/* on sys.path itself, `alembic current` -- and therefore
    `python migrate.py upgrade`, and therefore this revision -- died with
    ModuleNotFoundError: No module named 'ba2_common'.

    BA2_DB_FILE aims alembic at a throwaway file so no real database is opened.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["BA2_DB_FILE"] = str(tmp_path / "alembic_probe.sqlite")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_alembic_heads_is_this_revision_alone_without_a_pythonpath_prefix(tmp_path):
    """`alembic heads` must still work bare, and report exactly one head: ours.

    `heads` (like `history` and `branches`, i.e. `migrate.py heads|history`) imports
    every revision module but does NOT run env.py, so the sys.path fix in env.py does
    not apply to it. A revision that imports ba2_common at module scope breaks all
    three commands; this revision therefore defers that import into upgrade().
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["BA2_DB_FILE"] = str(tmp_path / "alembic_probe.sqlite")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # The id is deliberately NOT pinned: Task 8 chains f1c8a24b7e05 onto this
    # revision, which would break the assert without anything being wrong. What
    # matters here is that the command runs at all (no module-scope app import)
    # and that the chain has not accidentally forked.
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected exactly one head, got:\n{result.stdout}"
    assert '(head)' in heads[0], result.stdout


def test_migration_declares_the_verified_head_as_its_parent():
    module = _load_migration_module()
    assert module.revision == 'f1a7c2e9b4d0'
    assert module.down_revision == '0a3e0bd24598'


def test_upgrade_merges_duplicates_and_creates_the_unique_index(tmp_path):
    engine = _build_premerge_db(tmp_path)
    _run_upgrade(engine, _load_migration_module())

    assert _rows(engine) == [(1, 'AAPL'), (3, 'MSFT'), (4, 'NVDA')]
    assert _indexes(engine).get('ix_instrument_name') == 1

    with engine.connect() as conn:
        merged = conn.execute(text(
            "SELECT instrument_type, company_name, labels FROM instrument WHERE id = 1"
        )).fetchone()
    assert merged[0] == 'STOCK'
    assert merged[1] == 'Apple Inc'
    assert json.loads(merged[2]) == ['ark26', 'nasdaq30']


def test_production_shape_collapses_124_groups_and_still_indexes(tmp_path):
    """The live table's exact shape: 2477 rows, 2353 distinct names, 124 dup pairs.

    Read-only queries on 2026-08-20 put production at 2477 instrument rows over 2353
    distinct names with every name already `upper(trim(name))`, i.e. 124 groups of
    exactly two rows and no renaming at all. The small fixture above proves the
    merge's semantics; this proves CREATE UNIQUE INDEX actually succeeds once those
    124 groups are gone -- if even one duplicate survived the merge, the index
    creation, not the merge, is what would blow up in production.
    """
    db = tmp_path / "prodshape.sqlite"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        row_id = 0
        for i in range(2353):
            name = f"SYM{i:04d}"
            for _ in range(2 if i < 124 else 1):
                row_id += 1
                conn.execute(_INSERT, {
                    "id": row_id, "name": name, "instrument_type": "STOCK",
                    "categories": json.dumps([]), "labels": json.dumps([f"lab{row_id}"]),
                    "company_name": None,
                })
        assert row_id == 2477

    _run_upgrade(engine, _load_migration_module())

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM instrument")).scalar() == 2353
        assert conn.execute(text("SELECT count(DISTINCT name) FROM instrument")).scalar() == 2353
        assert conn.execute(text(
            "SELECT count(*) FROM instrument WHERE name <> upper(trim(name))"
        )).scalar() == 0
        # the merged pair keeps both rows' labels on the surviving lowest id
        assert json.loads(conn.execute(text(
            "SELECT labels FROM instrument WHERE name = 'SYM0000'"
        )).scalar()) == ['lab1', 'lab2']
    assert _indexes(engine).get('ix_instrument_name') == 1


def test_after_upgrade_the_database_rejects_a_duplicate_name(tmp_path):
    engine = _build_premerge_db(tmp_path)
    _run_upgrade(engine, _load_migration_module())

    conn = sqlite3.connect(str(tmp_path / 'premerge.sqlite'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO instrument (name) VALUES ('AAPL')")
    conn.close()


def test_running_the_upgrade_twice_succeeds_and_changes_nothing(tmp_path):
    engine = _build_premerge_db(tmp_path)
    module = _load_migration_module()
    _run_upgrade(engine, module)
    after_first = _rows(engine)
    _run_upgrade(engine, module)          # must not raise "index already exists"
    assert _rows(engine) == after_first
    assert _indexes(engine).get('ix_instrument_name') == 1


def test_dry_run_env_var_reports_and_aborts_without_touching_the_database(tmp_path, monkeypatch, capsys):
    engine = _build_premerge_db(tmp_path)
    before = _rows(engine)
    monkeypatch.setenv("BA2_INSTRUMENT_MERGE_DRY_RUN", "1")

    with pytest.raises(RuntimeError, match="BA2_INSTRUMENT_MERGE_DRY_RUN"):
        _run_upgrade(engine, _load_migration_module())

    printed = capsys.readouterr().out
    assert "AAPL" in printed and "MSFT" in printed
    assert _rows(engine) == before
    assert 'ix_instrument_name' not in _indexes(engine)


@pytest.mark.parametrize("value", ['1', 'true', 'TRUE', 'yes', ' 1 ', 'on', 'ON', 'Y', 'enabled', '2'])
def test_any_value_that_is_not_explicitly_false_means_dry_run(tmp_path, monkeypatch, value):
    """A truthy-value WHITELIST here would irreversibly delete production rows.

    'on', 'Y', 'enabled' and '2' are all things an operator plausibly types when
    they mean "just show me". Under a whitelist every one of them falls through to
    the real merge -- the exact failure this flag exists to prevent -- so anything
    unrecognised must abort instead.
    """
    engine = _build_premerge_db(tmp_path)
    before = _rows(engine)
    monkeypatch.setenv("BA2_INSTRUMENT_MERGE_DRY_RUN", value)

    with pytest.raises(RuntimeError, match="BA2_INSTRUMENT_MERGE_DRY_RUN"):
        _run_upgrade(engine, _load_migration_module())

    assert _rows(engine) == before
    assert 'ix_instrument_name' not in _indexes(engine)


@pytest.mark.parametrize("value", ['', '0', 'false', 'FALSE', 'no', ' no '])
def test_only_explicit_falsiness_arms_the_real_merge(tmp_path, monkeypatch, value):
    """The other direction: disarming still has to work, or the flag is a wall."""
    engine = _build_premerge_db(tmp_path)
    monkeypatch.setenv("BA2_INSTRUMENT_MERGE_DRY_RUN", value)

    _run_upgrade(engine, _load_migration_module())

    assert _rows(engine) == [(1, 'AAPL'), (3, 'MSFT'), (4, 'NVDA')]
    assert _indexes(engine).get('ix_instrument_name') == 1


@pytest.mark.parametrize("ddl", [
    "CREATE INDEX ix_instrument_name ON instrument (name)",          # right column, not unique
    "CREATE UNIQUE INDEX ix_instrument_name ON instrument (id)",     # unique, wrong column
])
def test_upgrade_refuses_an_index_that_only_borrows_the_name(tmp_path, ddl):
    """An index merely NAMED ix_instrument_name must not satisfy the guard.

    Skipping on the name alone means the migration reports success and stamps the
    revision while uniqueness is not enforced at all -- every downstream task then
    builds on an invariant that silently does not hold. Loud failure only.
    """
    engine = _build_premerge_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(ddl))
    before = _rows(engine)

    with pytest.raises(RuntimeError, match="not UNIQUE"):
        _run_upgrade(engine, _load_migration_module())

    # and the merge went back with it: untouched, not half-migrated
    assert _rows(engine) == before


class _CreateIndexExplodes:
    """Real Operations, except create_index raises -- a mid-migration failure."""

    def __init__(self, ops):
        self._ops = ops

    def __getattr__(self, name):
        return getattr(self._ops, name)

    def create_index(self, *args, **kwargs):
        raise RuntimeError("simulated failure during CREATE UNIQUE INDEX")


def test_a_failure_at_index_creation_rolls_the_entire_merge_back(tmp_path):
    """The property the whole recovery story rests on: all-or-nothing.

    `merge_duplicate_instruments` deliberately does not open or commit its own
    transaction, so the merge and the CREATE UNIQUE INDEX live or die together.
    That is what makes "if it fails, just re-run it" true, and what makes
    hand-repairing rows the wrong move.
    """
    engine = _build_premerge_db(tmp_path)
    module = _load_migration_module()
    before = _rows(engine)
    assert before == [(1, 'AAPL'), (2, 'AAPL'), (3, 'msft'), (4, 'NVDA')]

    with pytest.raises(RuntimeError, match="simulated failure"):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn, opts={"as_batch": True})
            module.op = _CreateIndexExplodes(Operations(ctx))
            module.sa = sqlalchemy
            module.upgrade()

    assert _rows(engine) == before      # deleted row back, 'msft' still lower-case
    assert 'ix_instrument_name' not in _indexes(engine)


def test_downgrade_drops_the_index_but_keeps_the_merged_rows(tmp_path):
    engine = _build_premerge_db(tmp_path)
    module = _load_migration_module()
    _run_upgrade(engine, module)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.sa = sqlalchemy
        module.downgrade()

    assert 'ix_instrument_name' not in _indexes(engine)
    assert _rows(engine) == [(1, 'AAPL'), (3, 'MSFT'), (4, 'NVDA')]
```

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_unique_migration.py -v`
Expected: FAIL — every migration test errors with
`AssertionError: missing migration file .../alembic/versions/f1a7c2e9b4d0_merge_duplicate_instruments_unique_name.py`,
and the two alembic-bootstrap tests fail on `returncode == 1` /
`ModuleNotFoundError: No module named 'ba2_common'`.
ACTUAL: `7 failed` for exactly those reasons. Three later tests were added with the migration
already in place and so were never observed red as written; each was instead proven non-vacuous
by reverting the behaviour it guards and watching it fail — the truthy-whitelist revert failed
`[on] [ON] [Y] [enabled] [2]`, the name-only index guard revert failed both impostor cases, and
injecting a `connection.commit()` mid-migration failed the rollback test. The 124-group
production-shape test is the only one with no such demonstration.

- [x] **Step 3: Write minimal implementation**

Create `alembic/versions/f1a7c2e9b4d0_merge_duplicate_instruments_unique_name.py`:

```python
"""merge duplicate instrument rows and make instrument.name unique

Revision ID: f1a7c2e9b4d0
Revises: 0a3e0bd24598
Create Date: 2026-08-20 12:00:00.000000

`instrument.name` had no unique constraint and no index, and production holds
duplicate names. `add_label_to_instruments` resolves a symbol with `.first()`
while `get_labels_by_symbol` keys by name, so on those symbols a label write
lands on an arbitrary row and can be invisible to the next read. Portfolio
allocation cannot be built on that.

The merge is unusually safe: NO table has a foreign key to `instrument` (verified
by grepping for `foreign_key="instrument` and by iterating pragma_foreign_key_list
over the live schema), so rows can be merged without repointing anything.

The merge itself lives in `ba2_common.core.instrument_merge` and is imported here
through the in-tree alias shim -- exactly how alembic/env.py imports models -- so
this migration and its tests execute the SAME code. It is idempotent: the plan is
recomputed from the current table state, so re-running writes nothing.

The import is INSIDE upgrade(), not at module scope. `alembic heads` / `history` /
`branches` load every revision module but never run env.py, and env.py is what puts
packages/* on sys.path (the venv's editable installs point at a path that does not
exist here). A module-scope import of the shim therefore breaks those commands with
ModuleNotFoundError: ba2_common, for every user, forever. Deferring it costs nothing:
upgrade() only ever runs under env.py.

INDEX NAME: `ix_instrument_name`, NOT `uix_instrument_name`. `Instrument.name` is
declared `Field(unique=True, index=True)`, which makes SQLModel's create_all emit
`CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)` on a fresh database
(verified by probe). Any other name here and a migrated database would disagree
with a freshly created one forever.

The merge is NOT reversible: downgrade only drops the index.

PRODUCTION RUNBOOK
==================
For ~/Documents/ba2/trade/db.sqlite. The order is load-bearing; do not skip ahead.

0. SHIP THIS TOGETHER WITH TASK 6, NEVER BEFORE IT. Once the unique index exists,
   write paths that today insert a silent duplicate start raising IntegrityError.
   Task 6 is what normalises and guards them. Same deployment, or not at all.

1. STOP THE APP FIRST. InstrumentAutoAdder and JobManager add instruments from
   background threads. One insert landing between the merge and the CREATE UNIQUE
   INDEX re-introduces a duplicate and fails the index creation.

2. BACK UP. THIS IS THE ONLY WAY BACK. `downgrade` drops the index but CANNOT
   restore the deleted rows -- no downgrade, no undo, only this copy:

       cp ~/Documents/ba2/trade/db.sqlite ~/Documents/ba2/trade/db.sqlite.bak-YYYYMMDD

3. CATCH UP TO THIS REVISION'S PARENT FIRST, ON ITS OWN. Production was last seen
   at d5e1b9a3c842, which is behind 0a3e0bd24598. Step 4 is only read-only if
   there is nothing left in front of it to apply:

       venv/bin/python -m alembic upgrade 0a3e0bd24598

4. DRY RUN. Read-only ONLY from 0a3e0bd24598 -- from any earlier revision alembic
   commits the intervening migrations before reaching this one (observed, not
   theorised), so step 3 is not optional:

       BA2_INSTRUMENT_MERGE_DRY_RUN=1 venv/bin/python -m alembic upgrade f1a7c2e9b4d0

   EXIT CODE 1 WITH A RuntimeError TRACEBACK IS SUCCESS, NOT FAILURE. Raising is
   how the dry run refuses to write. Now read the printed group count: 124 was the
   verified figure on 2026-08-20. Materially different means the table moved under
   you -- STOP and re-check instead of proceeding.

5. REAL RUN. UNSET the variable -- do not set it to 0. Any value this flag does not
   recognise as explicitly false means DRY RUN, so an unset variable is the only
   unambiguous way to ask for the real, irreversible merge:

       unset BA2_INSTRUMENT_MERGE_DRY_RUN
       venv/bin/python -m alembic upgrade f1a7c2e9b4d0

6. VERIFY, before restarting the app:

       sqlite3 db.sqlite "SELECT count(*), count(DISTINCT name) FROM instrument;"
         -> 2353|2353   (2477 rows before; 124 deleted)
       sqlite3 db.sqlite "SELECT sql FROM sqlite_master WHERE name='ix_instrument_name';"
         -> CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)
       venv/bin/python -m alembic current
         -> f1a7c2e9b4d0

7. IF IT FAILS, JUST RE-RUN IT. The merge and the index creation share one alembic
   transaction -- verified by injecting a failure at create_index against a copy of
   the real database, where all 124 merged groups rolled back intact. The table is
   either fully merged or untouched. Do NOT hand-repair rows.
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a7c2e9b4d0'
down_revision: Union[str, Sequence[str], None] = '0a3e0bd24598'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'ix_instrument_name'

# Only these disarm the dry run. See _dry_run_requested.
_EXPLICITLY_NOT_A_DRY_RUN = ('', '0', 'false', 'no')


def _dry_run_requested() -> bool:
    """Whether BA2_INSTRUMENT_MERGE_DRY_RUN asks for a report-only run.

    FAIL-SAFE, NOT FAIL-DANGEROUS: anything that is not explicitly false means
    yes. A whitelist of truthy spellings would silently perform the real,
    irreversible deletion of ~124 production rows for an operator who typed
    ``on``, ``Y``, ``enabled`` or ``2`` and believed they had asked for a report.
    On this flag, an unrecognised value must never mean "go ahead"; the cost of
    guessing wrong in this direction is one wasted, harmless run.
    """
    raw = os.environ.get('BA2_INSTRUMENT_MERGE_DRY_RUN', '').strip().lower()
    return raw not in _EXPLICITLY_NOT_A_DRY_RUN


def _find_index(connection):
    """The reflected definition of INDEX_NAME on `instrument`, or None."""
    for index in sa.inspect(connection).get_indexes('instrument'):
        if index['name'] == INDEX_NAME:
            return index
    return None


def _is_unique_on_name(index) -> bool:
    """Whether a reflected index really is UNIQUE over exactly (name)."""
    return bool(index.get('unique')) and list(index.get('column_names') or []) == ['name']


def upgrade() -> None:
    """Upgrade schema."""
    # Deferred on purpose -- see the module docstring: `alembic heads`/`history`
    # import this file without ever running env.py, which is what makes ba2_common
    # importable.
    from ba2_trade_platform.core.instrument_merge import (
        merge_duplicate_instruments,
        report_duplicate_instruments,
    )

    connection = op.get_bind()

    if _dry_run_requested():
        # print(), not logger: alembic's fileConfig can disable the app loggers,
        # and this report is the whole point of the dry run.
        plan = report_duplicate_instruments(connection)
        print(f"[instrument-merge dry-run] {len(plan)} instrument group(s) would be rewritten")
        for group in plan:
            print(
                f"[instrument-merge dry-run] {group['name']}: keep id={group['keep_id']} "
                f"delete ids={group['delete_ids']} type={group['instrument_type']} "
                f"labels={group['labels']} categories={group['categories']}"
            )
        raise RuntimeError(
            f"BA2_INSTRUMENT_MERGE_DRY_RUN is set: reported {len(plan)} group(s) and aborted "
            "before writing anything. Unset the variable to run the merge for real."
        )

    stats = merge_duplicate_instruments(connection)
    print(
        f"[instrument-merge] merged {stats['duplicate_groups']} duplicate group(s), "
        f"deleted {stats['rows_deleted']} row(s), normalised {stats['rows_renamed']} name(s)"
    )

    # Checked by DEFINITION, not just by name. Skipping on the name alone lets an
    # index that merely happens to be called ix_instrument_name -- non-unique, or
    # over the wrong column -- satisfy the guard: the migration would report
    # success, stamp the revision, and leave uniqueness unenforced, which is the
    # single invariant every downstream task is built on. Fail loudly instead.
    existing = _find_index(connection)
    if existing is None:
        op.create_index(INDEX_NAME, 'instrument', ['name'], unique=True)
    elif not _is_unique_on_name(existing):
        raise RuntimeError(
            f"{INDEX_NAME} already exists but is not UNIQUE(name): {existing!r}. "
            "Uniqueness is NOT enforced. Drop that index and re-run this migration."
        )


def downgrade() -> None:
    """Downgrade schema. The row merge cannot be undone; only the index is dropped."""
    connection = op.get_bind()

    # Same care as upgrade(): drop only the index this revision created. Dropping
    # whatever else happens to carry the name would destroy someone else's index
    # and report success.
    existing = _find_index(connection)
    if existing is None:
        return
    if not _is_unique_on_name(existing):
        raise RuntimeError(
            f"{INDEX_NAME} exists but is not the UNIQUE(name) index this revision "
            f"created: {existing!r}. Refusing to drop an index this migration does "
            "not own; inspect it and drop it by hand if that is really what you want."
        )
    op.drop_index(INDEX_NAME, table_name='instrument')
```

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_unique_migration.py -v`
Expected: PASS. ACTUAL: `28 passed` — 6 from the plan, 2 alembic-bootstrap, the 124-group
production-shape test, 16 parametrised dry-run-flag cases (both directions), 2 impostor-index
cases, and the merge-rolls-back-when-index-creation-fails test.

Then confirm the revision chain still has exactly one head — no PYTHONPATH prefix any more,
that is the whole point of the prerequisite:
Run: `venv/bin/python -m alembic heads`
Expected: `f1a7c2e9b4d0 (head)` — one line, no branch warning.

- [x] **Step 5: Commit**

```bash
git add alembic/env.py alembic/versions/f1a7c2e9b4d0_merge_duplicate_instruments_unique_name.py tests/test_instrument_unique_migration.py
git commit -m "feat(db): migration merging duplicate instruments and adding ix_instrument_name"
```

---

### Task 6: `unique=True` on `Instrument.name`

> **Guard the empty name before the index lands.** `normalize_symbol` returns `""` for blank or
> non-string input by design (it must not raise on Settings-UI input), and the add/edit dialog in
> `ba2_trade_platform/ui/pages/settings.py` (~`:471` edit branch, ~`:481` create branch) has no
> emptiness check — so a whitespace-only entry writes `Instrument(name='')`. With the unique index
> in place the FIRST such row is accepted and the SECOND raises `IntegrityError`, which surfaces as
> an unhandled exception in the UI. Add a guard in the dialog that rejects an empty normalised name
> with `ui.notify(..., type='negative')` and leaves the dialog open. Do NOT use `type='error'` —
> that is invalid, and the two existing uses at `settings.py:1023` and `:1041` are a known bug you
> are not fixing here.
>
> **The insert becomes a race the moment the index exists.** Both
> `JobManager.ensure_instrument_exists` and `InstrumentAutoAdder._add_instrument_if_missing` do
> select-then-insert, and both can run concurrently — the auto-adder has its own worker thread and
> APScheduler runs jobs on a pool. Today two threads that both miss the same symbol produce a
> harmless duplicate row; with `unique=True` the loser gets an `IntegrityError`. Wrap both inserts
> in `except IntegrityError: session.rollback()` and re-select, and add a test that two concurrent
> inserts of the same symbol yield one row and no exception.
>
> **Cover the submit path too.** `JobManager.submit_market_analysis` now calls
> `ensure_instrument_exists(symbol)`, which correctly writes nothing for a blank symbol and returns
> `""` — but the method then goes on to submit an analysis task for `""`. The old behaviour created
> an `Instrument(name='  ')`; both are wrong. Reject a blank normalised symbol there rather than
> submitting the task.


The migration gives an existing database the index; this makes the ORM (and `init_db()`'s
`create_all` on a fresh database) agree with it.

**Files:**
- Modify: `packages/common/ba2_common/core/models.py:547`
- Modify: `ba2_trade_platform/ui/pages/settings.py` (blank-name guard in the add/edit dialog)
- Modify: `ba2_trade_platform/core/JobManager.py` (blank-symbol guard in
  `submit_market_analysis`, `IntegrityError` handling in `ensure_instrument_exists`)
- Modify: `ba2_trade_platform/core/InstrumentAutoAdder.py` (`IntegrityError` handling in
  `_add_instrument_if_missing`)
- Test: `tests/test_instrument_unique_constraint.py`

- [x] **Step 1: Write the failing test**

Create `tests/test_instrument_unique_constraint.py`. Its core is the constraint itself:

```python
"""`instrument.name` uniqueness must be enforced by the schema, not by convention.

The helpers normalise symbols, but nothing stops a new code path from inserting a
second row for a symbol -- which is exactly how production accumulated its
duplicate groups. The database has to refuse it.
"""
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Instrument


def test_inserting_a_second_instrument_with_the_same_name_is_rejected():
    add_instance(Instrument(name='DUPX', labels=[]))
    with pytest.raises(IntegrityError):
        add_instance(Instrument(name='DUPX', labels=[]))


def test_create_all_emits_a_unique_index_named_ix_instrument_name(test_engine):
    """The name must match what the Alembic migration creates, or a migrated DB
    and a fresh DB disagree forever."""
    indexes = inspect(test_engine).get_indexes('instrument')
    unique_on_name = [ix['name'] for ix in indexes
                      if ix['column_names'] == ['name'] and ix['unique']]
    assert unique_on_name == ['ix_instrument_name']
```

The three routed sub-items are covered in the same file, because the constraint and the code that
has to survive it land together. As written it holds 16 tests:

- `test_inserting_a_second_instrument_with_the_same_name_is_rejected` (above)
- `test_create_all_emits_a_unique_index_named_ix_instrument_name` (above)
- `test_create_all_ddl_is_byte_identical_to_the_migrations_ddl` — compiles
  `CreateIndex` for the sqlite dialect and pins the exact string
  `CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)`, so the model and revision
  `f1a7c2e9b4d0` cannot drift.
- `test_dialog_refuses_to_save_an_instrument_whose_name_is_only_whitespace`,
  `test_dialog_refuses_to_rename_an_existing_instrument_to_a_blank_name`,
  `test_dialog_still_saves_a_valid_name_normalised` — the save handler is a closure inside a
  NiceGUI dialog, so `settings.ui` is replaced with a fake widget module (`_FakeUI`) and the REAL
  closure is then invoked. Asserts no row is written, the notification type is `negative`, and the
  dialog is not closed; the third is the positive control.
- `test_submit_market_analysis_refuses_a_blank_symbol`,
  `test_submit_market_analysis_still_accepts_a_real_symbol` — `JobManager.__new__(JobManager)` with
  `get_worker_queue` faked; asserts `ValueError` and that nothing was queued.
- `test_two_threads_calling_ensure_instrument_exists_create_one_row`,
  `test_two_threads_auto_adding_the_same_symbol_create_one_row` — two real threads. The
  session-scoped test engine is `sqlite:///:memory:` on a `SingletonThreadPool`, which gives every
  THREAD its own empty database, so these build a throwaway file DB with the production
  `_build_engine` and point `ba2_common.core.db._engine` at it. `get_db` is wrapped so each thread
  parks on a `threading.Barrier` after its first query: both leave their SELECT having missed, and
  both then INSERT. `_add_instrument_if_missing` swallows and logs its exceptions, so "no
  exception" is asserted against a logger spy, and each test also asserts the
  "created/added concurrently" INFO — otherwise "one row survived" would pass just as well if the
  barrier had failed to interleave and no race had happened.
- `test_the_loser_of_an_auto_add_race_carries_its_labels_to_the_surviving_row` — the two threads
  auto-add under DIFFERENT expert labels, so whichever loses, its label is only on the surviving
  row if the loser carried it over. Mutation-tested: switching the handler's reassignment to
  `winner.labels.extend(...)` makes it fail.
- `test_a_lost_auto_add_race_logs_no_error_anywhere` — spies on `ba2_common.core.db`'s logger as
  well, pinning that a handled race emits no ERROR + traceback from `@retry_on_lock`.
- `test_the_model_normalises_the_name_so_no_writer_can_skip_it`,
  `test_assignment_normalises_too_not_just_construction`,
  `test_a_writer_that_forgets_to_normalise_collides_instead_of_duplicating` — the model-level
  normalisation. The third is the one that matters: a writer that forgets `normalize_symbol` now
  collides with the existing row instead of quietly creating a lowercase duplicate.
- `test_a_none_name_is_still_rejected_loudly_by_the_not_null_column` — a characterisation test.
  It passed before the change too; it is here to pin the deliberate decision NOT to normalise
  `None` to `''`.

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_unique_constraint.py -v`
Expected: FAIL — `test_inserting_a_second_instrument...` fails with
`Failed: DID NOT RAISE <class 'sqlalchemy.exc.IntegrityError'>`, and
`test_create_all_emits_a_unique_index...` with `AssertionError: assert [] == ['ix_instrument_name']`.
Actual: 8 failed, 2 passed — the two positive controls pass before the change, as they must; the
race tests fail with `assert ['RACE', 'RACE'] == ['RACE']` (today's harmless duplicate row) and the
dialog tests with `assert [''] == []` (today's `Instrument(name='')`).

- [x] **Step 3: Write minimal implementation**

In `packages/common/ba2_common/core/models.py`, replace line 547 (note the existing line has one
trailing space after `str`):

```python
    name: str 
```

with:

```python
    # unique+index emits `CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)`,
    # byte-identical to what Alembic revision f1a7c2e9b4d0 creates -- so a migrated
    # database and a fresh create_all() one end up with the same schema.
    name: str = Field(unique=True, index=True)
```

`Field` is already imported at the top of the file. No `alembic/env.py` change is needed either —
it already imports the models through the shim. Verified: the emitted `CREATE TABLE instrument` is
byte-identical before and after (only the index is added).

The index alone is not enough, though: it is BINARY, so `AAPL`, `aapl` and `' AAPL'` remain
acceptable side by side and uniqueness holds only while every writer remembers to call
`normalize_symbol`. One that forgets reintroduces the duplicate groups in lowercase. So the same
class also normalises on the model:

```python
    def __setattr__(self, key, value):
        if key == "name" and value is not None:
            # Local import: ba2_common.core.utils imports this module.
            from ba2_common.core.utils import normalize_symbol
            value = normalize_symbol(value)
        super().__setattr__(key, value)
```

Four approaches were probed against SQLModel 0.0.37 / pydantic 2.12.5 before settling on this one:

| approach | at construction | on assignment |
| --- | --- | --- |
| pydantic `@field_validator` alone | **not applied** | not applied |
| SQLAlchemy `@validates` | **not applied** | not applied |
| `@field_validator` + `model_config = {"validate_assignment": True}` | applied | applied |
| `__setattr__` override (chosen) | applied | applied |

Table models skip pydantic validation in `__init__`, and `SQLModel.__setattr__` writes the raw
value into the SQLAlchemy-instrumented attribute *before* pydantic sees it — so a `@validates`
hook's normalised value is clobbered by the raw one on the very next line. `validate_assignment`
does work, but it switches on full pydantic validation for *every* field of the model (it starts
coercing `instrument_type='stock'` to the enum member, for instance); the `__setattr__` override
gets the same guarantee touching only `name`. `COLLATE NOCASE` was rejected: it needs a table
rebuild and would still admit `' AAPL'`.

`None` is deliberately NOT normalised to `''`: the column is NOT NULL, and that loud failure beats
silently storing the first nameless row. Verified there is no circular import on the cold path
(`import ba2_common.core.models` then construct) and no import leak (the cleanroom gate reports
CLEAN with `PYTHONPATH` set).

Then the three routed sub-items.

**a. `ui/pages/settings.py`** — in the add/edit dialog's `save()`, guard above the `is_edit` branch
(so it covers both create and rename) and above `session = get_db()` (so a rejected click does not
open a session):

```python
                    name = normalize_symbol(name_input.value)
                    if not name:
                        logger.warning('Rejected instrument save: name is empty after normalisation')
                        ui.notify('Instrument name is required', type='negative')
                        return
                    session = get_db()
                    labels = [l.strip() for l in labels_input.value.split(',')] if labels_input.value else []
```

**b. `core/JobManager.py`** — `from sqlalchemy.exc import IntegrityError`, then in
`ensure_instrument_exists` wrap the commit:

```python
            try:
                session.commit()
                logger.info(f"Auto-added instrument '{symbol}' to database with label 'auto_added'")
            except IntegrityError:
                session.rollback()
                winner = session.exec(
                    select(Instrument).where(Instrument.name == symbol)
                ).first()
                if winner is None:
                    raise
                logger.info(f"Instrument '{symbol}' was created concurrently; using the existing row")
```

and in `submit_market_analysis`, immediately after `symbol = ensure_instrument_exists(symbol)`:

```python
        if not symbol:
            raise ValueError("Cannot submit market analysis for a blank symbol")
```

(also documented in the method's `Raises:` block).

**c. `core/InstrumentAutoAdder.py`** — `from sqlalchemy.exc import IntegrityError`, then commit the
insert on our OWN session instead of through `add_instance`, and adopt the winner's row on a lost
race:

```python
            with get_db() as session:
                session.add(instrument)
                try:
                    session.commit()
                    instrument_id = instrument.id
                except IntegrityError:
                    session.rollback()
                    winner = session.exec(
                        select(Instrument).where(Instrument.name == symbol)
                    ).first()
                    if winner is None:
                        raise
                    wanted = ([expert_shortname] if expert_shortname else []) + list(extra_labels or [])
                    missing = [lbl for lbl in wanted if lbl not in (winner.labels or [])]
                    if missing:
                        # REASSIGN, never append: plain JSON column, no MutableList.
                        winner.labels = list(winner.labels or []) + missing
                        session.add(winner)
                        session.commit()
                    logger.info(f"Instrument {symbol} was added concurrently (ID {winner.id}); keeping the existing row and adding labels {missing}")
                    return
```

Two things here are not incidental:

- **Not `add_instance`.** It is wrapped by `@retry_on_lock`, which logs every non-lock exception at
  ERROR *with a full traceback* before re-raising (`db.py:266`). Routing the insert through it made
  the handled, expected lost race print a `UNIQUE constraint failed` traceback for operators to
  chase, directly above the INFO saying everything was fine. Owning the session — as
  `ensure_instrument_exists` already does — keeps the benign case quiet. This also drops the
  now-unused `add_instance` from the module's imports.
- **The loser must carry its labels over, by REASSIGNING the list.** Otherwise a lost race silently
  loses that expert's label, and nothing will ever repair it: the `existing` branch at `:106-112`
  appends to `Instrument.labels` IN PLACE, and that is a plain `Column(JSON)` with no `MutableList`
  wrapper, so the change is not change-detected and never persists. There is no self-healing pass
  and the merge migration is one-shot. (Those in-place appends are the documented out-of-scope
  `InstrumentAutoAdder` bug and are deliberately left alone — fixing them would start persisting
  thousands of expert labels.)

The re-select in (b) and (c) is load-bearing: if the row is still missing, the `IntegrityError` came
from some other constraint, and the bare `raise` re-raises it — surfacing to the caller in (b),
and in (c) landing in the method's own `except Exception` at `:181`, which logs it with a traceback
rather than swallowing it as a lost race.

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_unique_constraint.py -v`
Expected: PASS (16 passed).

Now re-run every test that writes Instrument rows, per file (the full suite is flaky from a
pre-existing session leak). Note the `PYTHONPATH` on the last one — point 20 above: its leak gate
shells out to a subprocess that does NOT inherit pytest's `pythonpath` ini setting, so without the
prefix it fails with `ModuleNotFoundError: No module named 'ba2_common'` and gates nothing:

```bash
venv/bin/python -m pytest tests/test_instrument_labels.py -v
venv/bin/python -m pytest tests/test_instrument_autoadd_normalisation.py -v
venv/bin/python -m pytest tests/test_instrument_symbol_import.py -v
venv/bin/python -m pytest tests/test_instrument_merge.py -v
venv/bin/python -m pytest tests/test_instrument_unique_migration.py -v
venv/bin/python -m pytest tests/test_models.py -v
PYTHONPATH=packages/common:packages/providers:packages/experts \
    venv/bin/python -m pytest packages/common/tests/test_utils_pure.py -v
```

Expected: all PASS — 15 / 6 / 5 / 15 / 28 / 12 / 7. (`tests/test_instrument_merge.py` and
`tests/test_instrument_unique_migration.py` build their `instrument` table with raw SQL precisely so
the new unique index cannot stop them from inserting the duplicates they exist to merge.)

As built, the wider sweep is: `tests/` 1282 passed; and with the same `PYTHONPATH` prefix,
`packages/experts/tests` 483 passed, `packages/providers/tests` 195 passed,
`packages/common/tests` 5 failed / 357 passed. Those 5 are `test_new_option_actions.py` and are
**pre-existing suite-order pollution, not this change**: the file passes 18/18 in isolation both
with and without it, and the whole-suite run fails identically on a stashed clean tree. Setting the
`PYTHONPATH` prefix is also what turns the three cleanroom leak gates from masked failures into
gates that actually run — with it set they pass, confirming `ba2_common.core.models` still pulls
nothing forbidden despite the new validator.

- [x] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/models.py ba2_trade_platform/core/JobManager.py \
    ba2_trade_platform/core/InstrumentAutoAdder.py ba2_trade_platform/ui/pages/settings.py \
    tests/test_instrument_unique_constraint.py
git commit -m "feat(models): make Instrument.name unique and indexed"
```

---

**Applying it to a real database** (not part of the TDD loop — the user runs this once, and
only after Task 6 is committed):

**Use the right database.** The live trade DB is
`~/Documents/ba2/trade/db.sqlite` — resolved by `ba2_trade_platform/config.py:16`
(`DB_FILE = os.path.join(TRADE_DIR, "db.sqlite")`) from
`packages/common/ba2_common/config.py:25` (`TRADE_DIR = BA2_HOME/trade`). It is 399 MB and holds
2477 instrument rows under 2353 distinct names — **124 duplicate groups**.

Do **not** use `~/Documents/ba2_trade_platform/db.sqlite`. That is a stale 19 MB file from
2026-06-18 that predates the shared-data-layout move; it is not what the app opens, and on this
Mac it currently cannot even be opened (`unable to open database file`). Verify before you run
anything:

```bash
# Confirm which file the app actually opens, and that it is the 399 MB one.
PYTHONPATH=packages/common:packages/providers:packages/experts \
    venv/bin/python -c "from ba2_trade_platform.config import DB_FILE; print(DB_FILE)"
ls -la ~/Documents/ba2/trade/db.sqlite
sqlite3 -readonly ~/Documents/ba2/trade/db.sqlite \
    "SELECT COUNT(*), COUNT(DISTINCT name) FROM instrument;"
```
Expected: the path prints as `/Users/<you>/Documents/ba2/trade/db.sqlite`, and the counts are
`2477|2353`.

```bash
DB=$HOME/Documents/ba2/trade/db.sqlite

# 1. Inspect the duplicate groups. Read-only: it prints the plan and aborts before writing.
BA2_INSTRUMENT_MERGE_DRY_RUN=1 BA2_DB_FILE=$DB \
    PYTHONPATH=packages/common:packages/providers:packages/experts \
    venv/bin/python -m alembic upgrade head

# 2. Back up, then run it for real. The merge deletes 124 rows and cannot be undone.
cp "$DB" "$DB.bak-$(date +%Y%m%d)"
BA2_DB_FILE=$DB PYTHONPATH=packages/common:packages/providers:packages/experts \
    venv/bin/python -m alembic upgrade head
```

Note for whoever runs it: the live DB is stamped `d5e1b9a3c842`, **two revisions behind** the
pre-existing head `0a3e0bd24598`, and `init_db()`'s `create_all` has already materialised tables
Alembic does not know about (`option_activity`, `option_iv_snapshot`, `provider_cache`). The
upgrade may therefore fail with a duplicate-column error before it ever reaches this plan's
revisions — check `PRAGMA table_info` on those tables and consider `alembic stamp` first.

Confirmed during Task 6 on throwaway databases: `alembic upgrade head` from an **empty** file dies
early with `NoSuchTableError: expertrecommendation` — the migration chain has never been runnable
from scratch, it only ever ran on top of a `create_all` database. What does work, and is what a
fresh install now gets:

```bash
# create_all first, then stamp and run only what create_all did not already build.
BA2_DB_FILE=$DB venv/bin/python -m alembic stamp f1a7c2e9b4d0
BA2_DB_FILE=$DB venv/bin/python -m alembic upgrade head
```

**The stamp target changed once Task 8 landed.** The original recipe stamped `0a3e0bd24598` and let
`f1a7c2e9b4d0` run. That no longer works: `create_all` now also builds the five allocation tables, so
`f1c8a24b7e05`'s `create_table` hits `table portfolio_allocation_config already exists`. Stamp
`f1a7c2e9b4d0` — or simply `stamp head`, since on a `create_all` database there is by definition
nothing left for either revision to do.

On such a database `create_all` has *already* built
`CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)` (Task 6's model change), and
`f1a7c2e9b4d0` correctly detects it, verifies it is UNIQUE over exactly `(name)`, skips the
`create_index`, and lands at head. The two provisioning paths were verified to produce the
identical `sqlite_master` entry.

Note the two different env vars: **alembic** reads `BA2_DB_FILE` (`alembic/env.py:21`), while the
**app** reads `DB_FILE` (`ba2_trade_platform/config.py:16`). The commands above target alembic, so
they use `BA2_DB_FILE`.

---

## Section B — Allocation tables and store

This section adds the five allocation tables, the Alembic revision that creates them, and
`portfolio_allocation_store.py` — the only module in the codebase that reads or writes them.

**Three things to know before you start.**

1. `packages/common/ba2_common/core/models.py` is the REAL model file. The in-tree
   `ba2_trade_platform/core/models.py` is an alias shim that swaps itself out of `sys.modules`;
   edits to it are discarded. Never edit a shim.
2. The store's cross-module imports from the allocation engine are deliberately tiny: the
   two `VALUATION_MODE_*` constants (Task 16 defines them) and `even_split_pct`
   (**amended in Task 10** — the store used to hand-roll a `_split_evenly` twin, but a
   `round()`-based twin diverges from the engine's `math.floor()`-based `even_split_pct`
   from 6 symbols up, e.g. `16.67 x 5 + 16.65` vs `16.66 x 5 + 16.70`; both total 100.0, so
   the pinned `33.33 / 33.33 / 33.34` tests could never have caught it). `_split_evenly`
   survives as a thin scaler over `even_split_pct`, so the defaults the page shows are
   bit-for-bit the ones `build_symbol_targets` computes.
3. **There is no `portfolio_allocation_repo.py`.** Task 63 appends the page's label/comment
   helpers to this same store module.

Test command on this Mac is `venv/bin/python -m pytest <path> -v` (`venv/`, **not** `.venv/`).
Run per file — the full suite fails non-deterministically from a pre-existing session leak.

---

### Task 7: The five allocation tables

**Files:**
- Modify: `packages/common/ba2_common/core/models.py` (append at end of file, currently 826 lines)
- Modify: `tests/conftest.py:22`
- Test: `tests/test_portfolio_allocation_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_models.py`:

```python
"""The five portfolio-allocation tables: round-trip, idempotency keys, computed properties."""
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import (
    PortfolioAllocationConfig,
    PortfolioAllocationLabel,
    PortfolioAllocationRun,
    PortfolioAllocationSymbol,
    PortfolioIncomeEvent,
)


def test_allocation_label_round_trips_with_its_fields(mock_account_def):
    add_instance(PortfolioAllocationLabel(
        account_id=mock_account_def.id, label="ARK26", target_pct=40.0,
        sort_order=2, comment="growth sleeve"))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationLabel)).one()
        assert row.label == "ARK26"
        assert row.target_pct == 40.0
        assert row.sort_order == 2
        assert row.comment == "growth sleeve"


def test_duplicate_label_on_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioAllocationLabel(
        account_id=mock_account_def.id, label="ARK26", target_pct=40.0))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioAllocationLabel(
            account_id=mock_account_def.id, label="ARK26", target_pct=60.0))


def test_same_symbol_in_two_labels_is_allowed(mock_account_def):
    add_instance(PortfolioAllocationSymbol(
        account_id=mock_account_def.id, label="ARK26", symbol="TSLA", weight_pct=50.0))
    add_instance(PortfolioAllocationSymbol(
        account_id=mock_account_def.id, label="HighRisk", symbol="TSLA", weight_pct=25.0))
    with get_db() as session:
        rows = session.exec(select(PortfolioAllocationSymbol)).all()
        assert sorted(r.label for r in rows) == ["ARK26", "HighRisk"]


def test_duplicate_external_id_on_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioIncomeEvent(
        account_id=mock_account_def.id, external_id="act-1",
        event_date=date(2026, 8, 1), event_type="DEPOSIT", amount=1000.0))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioIncomeEvent(
            account_id=mock_account_def.id, external_id="act-1",
            event_date=date(2026, 8, 1), event_type="DEPOSIT", amount=1000.0))


def test_income_event_open_amount_is_the_unconsumed_remainder():
    event = PortfolioIncomeEvent(
        account_id=1, external_id="act-1", event_date=date(2026, 8, 1),
        event_type="DIVIDEND", amount=250.0, consumed_amount=90.0)
    assert event.open_amount == 160.0


def test_income_event_open_amount_never_goes_negative():
    event = PortfolioIncomeEvent(
        account_id=1, external_id="act-1", event_date=date(2026, 8, 1),
        event_type="DEPOSIT", amount=100.0, consumed_amount=140.0)
    assert event.open_amount == 0.0


def test_run_net_buy_value_is_buys_minus_sells():
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 submitted_buy_value=5000.0, submitted_sell_value=1200.0)
    assert run.net_buy_value == 3800.0


def test_run_net_buy_value_is_zero_when_sells_exceed_buys():
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 submitted_buy_value=1000.0, submitted_sell_value=4000.0)
    assert run.net_buy_value == 0.0


def test_run_json_columns_round_trip(mock_account_def):
    add_instance(PortfolioAllocationRun(
        account_id=mock_account_def.id, mode="INVEST_LABEL", scope_label="ARK26",
        plan_json={"rows": [{"symbol": "TSLA", "side": "BUY"}], "scale_factor": 0.61},
        order_ids=[11, 12, 13]))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationRun)).one()
        assert row.plan_json["scale_factor"] == 0.61
        assert row.plan_json["rows"][0]["symbol"] == "TSLA"
        assert row.order_ids == [11, 12, 13]


def test_allocation_config_defaults_to_cost_mode_and_whole_shares(mock_account_def):
    add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig)).one()
        assert row.valuation_mode == "cost"
        assert row.allow_fractional is False


def test_allocation_config_round_trips_market_mode(mock_account_def):
    add_instance(PortfolioAllocationConfig(
        account_id=mock_account_def.id, valuation_mode="market", allow_fractional=True))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig)).one()
        assert row.valuation_mode == "market"
        assert row.allow_fractional is True


def test_a_second_config_row_for_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
```

`mock_account_def` is an existing fixture in `tests/conftest.py`: it persists an
`AccountDefinition` and returns it with a DB-assigned id.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_models.py -v`

Expected: a collection ERROR, not a test failure:
`ImportError: cannot import name 'PortfolioAllocationConfig' from 'ba2_common.core.models'`

- [ ] **Step 3: Write minimal implementation**

**3a.** Append to the END of `packages/common/ba2_common/core/models.py`. Every name used below
is already imported at the top of that file (`Field`, `SQLModel`, `Column`, `JSON`,
`UniqueConstraint`, `Dict`, `Any`, `List`, `DateTime`, `timezone`, `date`) — add NO new imports.

```python


class PortfolioAllocationConfig(SQLModel, table=True):
    """Per-account Portfolio Allocation page state that CHANGES MONEY.

    ``valuation_mode`` selects what "current value" means everywhere at once --
    the allocatable base, the ``% of label`` / ``% of total`` columns, and every
    delta -- so it belongs in a table rather than in session storage: a mode the
    user cannot see would silently reinterpret every number on the page.

    ``valuation_mode`` is a PLAIN str column (matching OptionActivity.activity_type):
    "cost" or "market" -- use ``VALUATION_MODE_COST`` / ``VALUATION_MODE_MARKET``
    from ``ba2_common.core.portfolio_allocation``, never a bare literal. One row
    per account, created on first use with the defaults "cost" and False.
    """
    __tablename__ = "portfolio_allocation_config"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE",
                            index=True, unique=True)
    valuation_mode: str = Field(default="cost", description="cost | market (plain str, see core.portfolio_allocation)")
    allow_fractional: bool = Field(default=False, description="Last fractional-shares choice, pre-filled into the wizard")
    updated_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class PortfolioAllocationLabel(SQLModel, table=True):
    """A label the user has chosen to MANAGE for an account's portfolio allocation.

    The row's EXISTENCE is the "managed" flag, so label selection needs no
    separate table -- deleting the row unmanages the label. ``target_pct`` is
    1-100 and, summed across all rows of one account, must equal exactly 100
    before a REBALANCE run may be submitted.
    """
    __tablename__ = "portfolio_allocation_label"
    __table_args__ = (
        UniqueConstraint('account_id', 'label', name='uix_pf_alloc_label_account_label'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    label: str = Field(index=True, description="Instrument label being managed (e.g. 'ARK26')")
    target_pct: float = Field(default=0.0, description="Target % of the base notional (1-100)")
    sort_order: int = Field(default=0, description="Display order of the label expansion on the page")
    comment: str | None = Field(default=None, description="Free-text note shown on the label header")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class PortfolioAllocationSymbol(SQLModel, table=True):
    """A symbol's weight WITHIN a managed label.

    Rows are created LAZILY: a symbol with no row uses the even-split default, so
    absence is meaningful and must never be backfilled for every symbol. A symbol
    may legitimately appear under several labels (its targets then SUM; the page
    shows a warning icon).
    """
    __tablename__ = "portfolio_allocation_symbol"
    __table_args__ = (
        UniqueConstraint('account_id', 'label', 'symbol',
                         name='uix_pf_alloc_symbol_account_label_symbol'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    label: str = Field(index=True)
    symbol: str = Field(index=True, description="Normalised (.strip().upper()) instrument symbol")
    weight_pct: float = Field(default=0.0, description="Weight % WITHIN the label (1-100)")
    comment: str | None = Field(default=None, description="Free-text note shown on the symbol row")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class PortfolioIncomeEvent(SQLModel, table=True):
    """One deposit or dividend, consumed oldest-first by allocation runs.

    ``event_type`` is a PLAIN str column (matching OptionActivity.activity_type):
    "DEPOSIT" or "DIVIDEND" -- use ``CASH_TRANSFER_DEPOSIT`` /
    ``CASH_TRANSFER_DIVIDEND`` from ``ba2_common.core.account_types``, never a
    bare literal. Withdrawals are NOT income and are never persisted here.

    ``(account_id, external_id)`` is the idempotency key: re-syncing the broker
    ledger upserts instead of duplicating, exactly as ``OptionActivity`` does.
    An event can be PARTIALLY consumed; the remainder stays open.
    """
    __tablename__ = "portfolio_income_event"
    __table_args__ = (
        UniqueConstraint('account_id', 'external_id', name='uix_pf_income_account_externalid'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    external_id: str = Field(index=True, description="Broker activity id (idempotency key)")
    event_date: date = Field(index=True, description="Broker settlement / pay date")
    event_type: str = Field(description="DEPOSIT | DIVIDEND (plain str, see core.account_types)")
    symbol: str | None = Field(default=None, index=True, description="Payer symbol for DIVIDEND; None for DEPOSIT")
    amount: float = Field(description="Positive cash amount in the account currency")
    consumed_amount: float = Field(default=0.0, description="How much of `amount` allocation runs have already spent")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    @property
    def open_amount(self) -> float:
        """Un-consumed remainder of this event; never negative."""
        return max(0.0, (self.amount or 0.0) - (self.consumed_amount or 0.0))


class PortfolioAllocationRun(SQLModel, table=True):
    """Audit row for one SUBMITTED allocation run.

    ``mode`` is a PLAIN str column: "REBALANCE" or "INVEST_LABEL" -- use
    ``ALLOCATION_MODE_REBALANCE`` / ``ALLOCATION_MODE_INVEST_LABEL`` from
    ``ba2_common.core.portfolio_allocation``.

    ``plan_json`` is ``AllocationPlan.to_dict()`` captured at SUBMIT time, which
    keeps a dry-run reproducible after the weights change. Income consumption is
    driven by the NET buy value (``net_buy_value`` below): a rebalance funded
    entirely by its own sells consumes no income.

    ``base_notional`` mirrors ``AllocationPlan.base_notional`` and so carries TWO
    meanings depending on ``mode``: in a REBALANCE it is the ALLOCATABLE BASE
    (buying power plus the current value of managed positions, at plan time); in
    an INVEST_LABEL run it is simply THE BUDGET being spent. Read it together
    with ``mode``.
    """
    __tablename__ = "portfolio_allocation_run"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    mode: str = Field(index=True, description="REBALANCE | INVEST_LABEL (plain str)")
    scope_label: str | None = Field(default=None, description="Label targeted by an INVEST_LABEL run; None for REBALANCE")
    base_notional: float = Field(default=0.0, description="REBALANCE: buying_power + current value of managed positions, at plan time. INVEST_LABEL: the budget being spent")
    available_buying_power: float = Field(default=0.0, description="Broker buying power snapshotted at plan time")
    allow_fractional: bool = Field(default=False, description="Whether fractional shares were opted in for this run")
    plan_json: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict, description="AllocationPlan.to_dict() at submit time")
    submitted_buy_value: float = Field(default=0.0, description="Sum of estimated value of BUY orders actually submitted")
    submitted_sell_value: float = Field(default=0.0, description="Sum of estimated value of SELL orders actually submitted")
    order_ids: List[int] = Field(sa_column=Column(JSON), default_factory=list, description="TradingOrder ids created by this run")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    @property
    def net_buy_value(self) -> float:
        """``max(0, buys - sells)`` -- what this run consumes from the income ledger."""
        return max(0.0, (self.submitted_buy_value or 0.0) - (self.submitted_sell_value or 0.0))
```

**3b.** In `tests/conftest.py`, extend the existing model import list. Replace line 22
(`    OptionIVSnapshot, OptionActivity,`) with:

```python
    OptionIVSnapshot, OptionActivity,
    PortfolioAllocationConfig, PortfolioAllocationLabel, PortfolioAllocationSymbol,
    PortfolioIncomeEvent, PortfolioAllocationRun,
```

`alembic/env.py` needs no change — its line 16 already imports `ba2_trade_platform.core.models`,
which resolves through the shim and registers the new tables on `SQLModel.metadata`.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_models.py -v`
Expected: PASS — `12 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/models.py tests/conftest.py tests/test_portfolio_allocation_models.py
git commit -m "feat(models): add the five portfolio allocation tables"
```

---

### Task 8: Alembic revision creating the five tables

**Files:**
- Create: `alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py`
- Test: `tests/test_portfolio_allocation_migration.py`

This revision chains **after Task 5's instrument-uniqueness revision**, so
`down_revision = 'f1a7c2e9b4d0'`. Do **not** chain off `0a3e0bd24598` — that is Task 5's
`down_revision`, not yours.

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_migration.py`:

```python
"""The allocation-tables Alembic revision must build exactly what the models declare."""
import importlib.util
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import SQLModel
from sqlalchemy import create_engine, inspect

REVISION_FILE = pathlib.Path(__file__).resolve().parents[1] / \
    "alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py"

ALLOCATION_TABLES = [
    "portfolio_allocation_config",
    "portfolio_allocation_label",
    "portfolio_allocation_symbol",
    "portfolio_income_event",
    "portfolio_allocation_run",
]


def _load_revision():
    spec = importlib.util.spec_from_file_location("pf_alloc_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated_engine(tmp_path):
    """A scratch sqlite with ONLY this revision's upgrade() applied."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
    return engine


def test_migration_creates_all_five_allocation_tables(migrated_engine):
    tables = inspect(migrated_engine).get_table_names()
    assert sorted(t for t in tables if t.startswith("portfolio_")) == sorted(ALLOCATION_TABLES)


@pytest.mark.parametrize("table_name", ALLOCATION_TABLES)
def test_migration_columns_match_the_model(migrated_engine, table_name):
    migrated = {c["name"] for c in inspect(migrated_engine).get_columns(table_name)}
    declared = {c.name for c in SQLModel.metadata.tables[table_name].columns}
    assert migrated == declared


@pytest.mark.parametrize("table_name", ALLOCATION_TABLES)
def test_migration_indexes_match_the_model(migrated_engine, table_name):
    migrated = {i["name"] for i in inspect(migrated_engine).get_indexes(table_name)}
    declared = {i.name for i in SQLModel.metadata.tables[table_name].indexes}
    assert migrated == declared


def test_migration_enforces_the_income_idempotency_key(migrated_engine):
    unique = {tuple(u["column_names"])
              for u in inspect(migrated_engine).get_unique_constraints("portfolio_income_event")}
    assert ("account_id", "external_id") in unique


def test_migration_allows_only_one_config_row_per_account(migrated_engine):
    index_names = {i["name"] for i in
                   inspect(migrated_engine).get_indexes("portfolio_allocation_config")}
    unique = {tuple(u["column_names"]) for u in
              inspect(migrated_engine).get_unique_constraints("portfolio_allocation_config")}
    unique_indexes = {tuple(i["column_names"]) for i in
                      inspect(migrated_engine).get_indexes("portfolio_allocation_config")
                      if i["unique"]}
    assert ("account_id",) in unique or ("account_id",) in unique_indexes, index_names


def test_migration_downgrade_drops_all_five_tables(migrated_engine):
    module = _load_revision()
    with migrated_engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
    remaining = inspect(migrated_engine).get_table_names()
    assert [t for t in remaining if t.startswith("portfolio_")] == []


def test_the_revision_is_chained_onto_the_instrument_revision():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = pathlib.Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic history has branched: {heads}"
    revision = script.get_revision("f1c8a24b7e05")
    assert revision.down_revision == "f1a7c2e9b4d0"
    assert script.get_revision(revision.down_revision) is not None
```

The `Operations.context(MigrationContext.configure(conn))` block installs the
`from alembic import op` proxy, so the revision's `upgrade()` runs against a scratch sqlite
without touching your real database. Comparing the result to `SQLModel.metadata` is what
catches model/migration drift.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_migration.py -v`
Expected: every test ERRORs in the `migrated_engine` fixture with
`FileNotFoundError: [Errno 2] No such file or directory: '.../alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py'`

- [ ] **Step 3: Write minimal implementation**

Create `alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py`:

```python
"""add the five portfolio allocation tables

Revision ID: f1c8a24b7e05
Revises: f1a7c2e9b4d0
Create Date: 2026-08-20

Creates portfolio_allocation_config, portfolio_allocation_label,
portfolio_allocation_symbol, portfolio_income_event and portfolio_allocation_run.
Chained AFTER the instrument merge + unique index so the destructive data
migration can be run and inspected on its own, without the schema additions
riding along.

Index names are the ones SQLAlchemy itself emits for these models
(``ix_<table>_<column>``), so init_db()'s create_all on a fresh DB and Alembic on
an existing one agree. Foreign keys are declarative only -- the live DB runs with
PRAGMA foreign_keys = 0 -- so account deletion must clear these tables
explicitly (see portfolio_allocation_store.delete_account_allocation_data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1c8a24b7e05'
down_revision: Union[str, Sequence[str], None] = 'f1a7c2e9b4d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portfolio_allocation_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("valuation_mode", sa.String(), nullable=False),
        sa.Column("allow_fractional", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # unique=True + index=True on the model emits ONE unique index, not a
    # UniqueConstraint -- mirror that exactly so create_all and Alembic agree.
    op.create_index("ix_portfolio_allocation_config_account_id",
                    "portfolio_allocation_config", ["account_id"], unique=True)
    op.create_index("ix_portfolio_allocation_config_updated_at",
                    "portfolio_allocation_config", ["updated_at"])

    op.create_table(
        "portfolio_allocation_label",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("target_pct", sa.Float(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "label", name="uix_pf_alloc_label_account_label"),
    )
    op.create_index("ix_portfolio_allocation_label_account_id", "portfolio_allocation_label", ["account_id"])
    op.create_index("ix_portfolio_allocation_label_label", "portfolio_allocation_label", ["label"])
    op.create_index("ix_portfolio_allocation_label_created_at", "portfolio_allocation_label", ["created_at"])

    op.create_table(
        "portfolio_allocation_symbol",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "label", "symbol",
                            name="uix_pf_alloc_symbol_account_label_symbol"),
    )
    op.create_index("ix_portfolio_allocation_symbol_account_id", "portfolio_allocation_symbol", ["account_id"])
    op.create_index("ix_portfolio_allocation_symbol_label", "portfolio_allocation_symbol", ["label"])
    op.create_index("ix_portfolio_allocation_symbol_symbol", "portfolio_allocation_symbol", ["symbol"])
    op.create_index("ix_portfolio_allocation_symbol_created_at", "portfolio_allocation_symbol", ["created_at"])

    op.create_table(
        "portfolio_income_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("consumed_amount", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "external_id", name="uix_pf_income_account_externalid"),
    )
    op.create_index("ix_portfolio_income_event_account_id", "portfolio_income_event", ["account_id"])
    op.create_index("ix_portfolio_income_event_external_id", "portfolio_income_event", ["external_id"])
    op.create_index("ix_portfolio_income_event_event_date", "portfolio_income_event", ["event_date"])
    op.create_index("ix_portfolio_income_event_symbol", "portfolio_income_event", ["symbol"])
    op.create_index("ix_portfolio_income_event_created_at", "portfolio_income_event", ["created_at"])

    op.create_table(
        "portfolio_allocation_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("scope_label", sa.String(), nullable=True),
        sa.Column("base_notional", sa.Float(), nullable=False),
        sa.Column("available_buying_power", sa.Float(), nullable=False),
        sa.Column("allow_fractional", sa.Boolean(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("submitted_buy_value", sa.Float(), nullable=False),
        sa.Column("submitted_sell_value", sa.Float(), nullable=False),
        sa.Column("order_ids", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accountdefinition.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_portfolio_allocation_run_account_id", "portfolio_allocation_run", ["account_id"])
    op.create_index("ix_portfolio_allocation_run_mode", "portfolio_allocation_run", ["mode"])
    op.create_index("ix_portfolio_allocation_run_created_at", "portfolio_allocation_run", ["created_at"])


def downgrade() -> None:
    op.drop_table("portfolio_allocation_run")
    op.drop_table("portfolio_income_event")
    op.drop_table("portfolio_allocation_symbol")
    op.drop_table("portfolio_allocation_label")
    op.drop_table("portfolio_allocation_config")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_migration.py -v`
Expected: PASS — `15 passed` (5 standalone tests plus two 5-way parametrisations)

If `test_migration_indexes_match_the_model` fails on `portfolio_allocation_config`, compare the
reported sets: SQLModel's `unique=True, index=True` emits exactly one unique index named
`ix_portfolio_allocation_config_account_id`, which is what the revision creates.

Do **not** run `python migrate.py upgrade` against your local dev DB as part of this task: that
live DB is at `d5e1b9a3c842` (two revisions behind head) and `init_db()`'s `create_all` has already
materialised tables Alembic does not know about, so an upgrade there may fail with a
duplicate-column error. Check `PRAGMA table_info` and consider `alembic stamp` first, separately.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py tests/test_portfolio_allocation_migration.py
git commit -m "feat(db): alembic revision creating the five portfolio allocation tables"
```

---

### Task 9: Store — managed labels (and the module's alias shim)

**Files:**
- Create: `packages/common/ba2_common/core/portfolio_allocation_store.py`
- Create: `ba2_trade_platform/core/portfolio_allocation_store.py` (SHIM)
- Test: `tests/test_portfolio_allocation_store.py`

- [x] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_store.py`:

```python
"""Repository layer for the portfolio-allocation tables, against the in-memory test DB."""
from datetime import date

import pytest

from ba2_trade_platform.core import portfolio_allocation_store as store


@pytest.fixture
def account_id(mock_account_def):
    """The id of a persisted AccountDefinition (conftest fixture)."""
    return mock_account_def.id


# --- managed labels --------------------------------------------------------

def test_get_managed_labels_is_empty_for_a_new_account(account_id):
    assert store.get_managed_labels(account_id) == []


def test_set_managed_label_creates_then_updates_one_row(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=40.0, comment="growth")
    store.set_managed_label(account_id, "ARK26", target_pct=55.0)
    rows = store.get_managed_labels(account_id)
    assert len(rows) == 1
    assert rows[0].target_pct == 55.0
    assert rows[0].comment == "growth"


def test_set_managed_label_leaves_unpassed_fields_untouched(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=40.0, comment="growth")
    store.set_managed_label(account_id, "ARK26", comment="renamed sleeve")
    row = store.get_managed_labels(account_id)[0]
    assert row.target_pct == 40.0
    assert row.comment == "renamed sleeve"


def test_managed_labels_come_back_in_sort_order(account_id):
    store.set_managed_label(account_id, "ZULU", target_pct=10.0, sort_order=0)
    store.set_managed_label(account_id, "ARK26", target_pct=90.0, sort_order=1)
    assert [r.label for r in store.get_managed_labels(account_id)] == ["ZULU", "ARK26"]


def test_set_managed_label_rejects_a_blank_label(account_id):
    with pytest.raises(ValueError):
        store.set_managed_label(account_id, "   ", target_pct=10.0)


def test_remove_managed_label_also_removes_its_symbol_weights(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=70.0)
    assert store.remove_managed_label(account_id, "ARK26") is True
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, "ARK26") == {}


def test_remove_managed_label_returns_false_when_not_managed(account_id):
    assert store.remove_managed_label(account_id, "NOPE") is False
```

`test_remove_managed_label_also_removes_its_symbol_weights` deliberately uses
`set_symbol_weight` / `get_symbol_rows`, which arrive in Task 10 — so it stays red until then.
That is intentional: unmanaging a label must not orphan its weight rows.

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: a collection ERROR. AS LANDED the message is the `from ... import` form,
not the dotted-module form, because the test imports the submodule off the package:
`ImportError: cannot import name 'portfolio_allocation_store' from 'ba2_trade_platform.core'`

- [x] **Step 3: Write minimal implementation**

Create `packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python
"""Portfolio allocation persistence: every read and write of the five allocation tables.

Pure DB code -- it never talks to a broker and never touches NiceGUI. The only
thing it borrows from the allocation ENGINE
(``ba2_common.core.portfolio_allocation``) is the two ``VALUATION_MODE_*``
constants, so that the page, the store and the engine cannot disagree on the
spelling of a mode. The UI calls these helpers; the engine receives the plain
values they produce.

(Task 10 widens that borrow to ``even_split_pct`` and rewords this paragraph --
see Task 10 Step 3.)

Two rules the callers depend on:

* A ``portfolio_allocation_label`` row's EXISTENCE is the "this label is managed"
  flag -- deleting the row unmanages the label.
* ``portfolio_allocation_symbol`` rows are created LAZILY. A symbol with no row
  takes the even-split default, so ``get_symbol_weights()`` returns a computed
  weight for every symbol you ask about and never an empty dict.
"""
from __future__ import annotations

from datetime import date as Date, datetime as DateTime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import (
    PortfolioAllocationConfig,
    PortfolioAllocationLabel,
    PortfolioAllocationRun,
    PortfolioAllocationSymbol,
    PortfolioIncomeEvent,
)
from ba2_common.logger import logger


# ---------------------------------------------------------------------------
# Managed labels
# ---------------------------------------------------------------------------

def get_managed_labels(account_id: int) -> List[PortfolioAllocationLabel]:
    """Every managed label of an account, in display order (sort_order, then name).

    Returns ``[]`` when the account manages nothing -- a legitimate empty state
    (nothing configured yet), not an error.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationLabel)
            .where(PortfolioAllocationLabel.account_id == account_id)
            .order_by(PortfolioAllocationLabel.sort_order, PortfolioAllocationLabel.label)
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows


def set_managed_label(account_id: int, label: str, *,
                      target_pct: Optional[float] = None,
                      sort_order: Optional[int] = None,
                      comment: Optional[str] = None) -> PortfolioAllocationLabel:
    """Create the managed-label row, or update only the fields you pass.

    ``None`` for a field means LEAVE IT UNCHANGED, so the page can save a comment
    without disturbing the percentage. Pass ``""`` to clear a comment.

    Raises:
        ValueError: when ``label`` is blank -- a nameless managed label is
        unreachable from the UI and would collide with the next blank one.
    """
    label = (label or "").strip()
    if not label:
        raise ValueError("set_managed_label requires a non-empty label")
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationLabel).where(
                PortfolioAllocationLabel.account_id == account_id,
                PortfolioAllocationLabel.label == label,
            )
        ).first()
        if row is None:
            row = PortfolioAllocationLabel(account_id=account_id, label=label)
            session.add(row)
        if target_pct is not None:
            row.target_pct = float(target_pct)
        if sort_order is not None:
            row.sort_order = int(sort_order)
        if comment is not None:
            row.comment = comment
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def remove_managed_label(account_id: int, label: str) -> bool:
    """Unmanage a label: delete its row AND every symbol-weight row underneath it.

    Returns True when a label row was deleted, False when the label was not
    managed in the first place.
    """
    label = (label or "").strip()
    if not label:
        return False
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationLabel).where(
                PortfolioAllocationLabel.account_id == account_id,
                PortfolioAllocationLabel.label == label,
            )
        ).first()
        symbol_rows = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
            )
        ).all()
        found = row is not None
        removed_symbols = len(symbol_rows)
        for symbol_row in symbol_rows:
            session.delete(symbol_row)
        if row is not None:
            session.delete(row)
        session.commit()
    if not found:
        return False
    logger.info(f"Unmanaged allocation label '{label}' for account {account_id} "
                f"({removed_symbols} symbol weight row(s) removed)")
    return True
```

`session.refresh(row)` after `commit()` matters: SQLAlchemy expires instances on commit, and
without the refresh the caller would hit a `DetachedInstanceError` once the `with` block closes
the session. `session.expunge_all()` on the read path does the same job for query results.

Create the mandatory in-tree alias shim `ba2_trade_platform/core/portfolio_allocation_store.py`
— copied verbatim from `ba2_trade_platform/core/option_types.py` with only the module name
changed:

```python
"""Alias shim: this in-tree module IS ba2_common.core.portfolio_allocation_store (Phase 6 migration).

The in-tree path is aliased to the package module object in sys.modules so
existing ``from ba2_trade_platform...`` imports resolve unchanged AND
``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path
operate on the real package module. Single source of truth: ba2_common.core.portfolio_allocation_store."""
import importlib as _importlib
import sys as _sys

_pkg = _importlib.import_module("ba2_common.core.portfolio_allocation_store")
# RACE GUARD: mirror the package's names onto THIS module BEFORE swapping it out of
# sys.modules. The swap alone leaves the original module object permanently empty, so a
# second thread reaching a LAZY ``from .X import Y`` while the first is still executing
# this body gets that empty object and raises "cannot import name 'Y'". That silently
# killed a live Monday enter-market run on 2026-08-17; see
# docs/2026-08-17-alias-shim-race.md. Locals are captured first because the update copies
# the package namespace wholesale -- a package binding _sys/_pkg must not break the swap.
_modules, _me, _target = _sys.modules, __name__, _pkg
globals().update({k: v for k, v in vars(_pkg).items() if not k.startswith('__')})
_modules[_me] = _target
```

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: `6 passed, 1 failed` — the only failure is
`test_remove_managed_label_also_removes_its_symbol_weights` with
`AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'set_symbol_weight'`. Task 10 turns it green.

Run: `venv/bin/python -m pytest tests/test_alias_shim_race.py -v`
Expected: PASS (the new shim satisfies the race-guard ordering checks).

- [x] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py ba2_trade_platform/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): managed-label CRUD in the portfolio allocation store"
```

---

### Task 10: Store — symbol weights with lazy even-split defaults

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [x] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- symbol weights (lazy rows, even-split defaults) -----------------------

def test_symbol_weights_default_to_an_even_split_when_no_rows_exist(account_id):
    weights = store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR", "COIN"])
    assert weights == {"TSLA": 33.33, "PLTR": 33.33, "COIN": 33.34}
    assert sum(weights.values()) == 100.0


def test_symbol_weights_split_the_remainder_among_unstored_symbols(account_id):
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=50.0)
    weights = store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR", "COIN"])
    assert weights == {"TSLA": 50.0, "PLTR": 25.0, "COIN": 25.0}


def test_symbol_weights_give_unstored_symbols_zero_when_stored_already_total_100(account_id):
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=100.0)
    weights = store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR"])
    assert weights == {"TSLA": 100.0, "PLTR": 0.0}


def test_symbol_weights_normalise_and_deduplicate_symbols(account_id):
    weights = store.get_symbol_weights(account_id, "ARK26", [" tsla ", "TSLA", "pltr"])
    assert list(weights.keys()) == ["TSLA", "PLTR"]
    assert weights == {"TSLA": 50.0, "PLTR": 50.0}


def test_symbol_weights_of_an_empty_label_are_empty(account_id):
    assert store.get_symbol_weights(account_id, "ARK26", []) == {}


def test_set_symbol_weight_stores_a_lowercase_symbol_uppercased(account_id):
    row = store.set_symbol_weight(account_id, "ARK26", " tsla ", weight_pct=60.0, comment="core")
    assert row.symbol == "TSLA"
    assert store.get_symbol_rows(account_id, "ARK26")["TSLA"].comment == "core"


def test_set_symbol_weight_rejects_a_blank_symbol(account_id):
    with pytest.raises(ValueError):
        store.set_symbol_weight(account_id, "ARK26", "", weight_pct=10.0)


def test_remove_symbol_weight_restores_the_even_split_default(account_id):
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=90.0)
    assert store.remove_symbol_weight(account_id, "ARK26", "TSLA") is True
    assert store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR"]) == {
        "TSLA": 50.0, "PLTR": 50.0}


def test_remove_symbol_weight_returns_false_when_no_row_exists(account_id):
    assert store.remove_symbol_weight(account_id, "ARK26", "TSLA") is False
```

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'get_symbol_weights'` (and the same for `set_symbol_weight` / `get_symbol_rows` / `remove_symbol_weight`)

- [x] **Step 3: Write minimal implementation**

First amend the header of `packages/common/ba2_common/core/portfolio_allocation_store.py`
— reword the docstring's engine-borrow paragraph and import the engine's split:

```python
Pure DB code -- it never talks to a broker and never touches NiceGUI. What it
borrows from the allocation ENGINE (``ba2_common.core.portfolio_allocation``) is
deliberately tiny: the two ``VALUATION_MODE_*`` constants, so that the page, the
store and the engine cannot disagree on the spelling of a mode, and
``even_split_pct``, so that the default weights this module hands the page are
bit-for-bit the ones the engine would compute. The UI calls these helpers; the
engine receives the plain values they produce.
```

```python
from ba2_common.core.portfolio_allocation import even_split_pct  # after the models import
```

**Amended (as landed, Task 10).** The original plan hand-rolled `_split_evenly` and said
the store "deliberately does not import the engine's functions, so both must produce the
same numbers". They did not: `round(100/6, 2) == 16.67` gives `16.67 x 5 + 16.65`, while
the engine's `math.floor`-based `even_split_pct(6)` gives `16.66 x 5 + 16.70`. Both total
exactly 100.0, so the pinned 3-symbol values (`33.33 / 33.33 / 33.34`, identical under both
rules) could never have caught the drift — the page would simply have shown different
defaults from the ones the engine allocated on, for any label of 6+ symbols. Sharing the one
function removes the failure mode instead of testing for it. Verified: over 100
symbol-count/stored-weight shapes, `get_symbol_weights` now equals
`build_symbol_targets` exactly.

Then append to the end of the same file:

```python


# ---------------------------------------------------------------------------
# Symbol weights (created lazily -- absence means "even-split default")
# ---------------------------------------------------------------------------

def _split_evenly(total_pct: float, count: int) -> List[float]:
    """Split ``total_pct`` across ``count`` slots, remainder on the LAST slot.

    ``_split_evenly(100.0, 3) == [33.33, 33.33, 33.34]``, which sums to exactly
    100.0 -- a naive ``3 x 33.33`` totals 99.99 and the engine's
    ``validate_symbol_weights`` (0.01pp tolerance) rejects it. Returns ``[]`` for
    ``count <= 0`` (an empty label gets nothing, not a ZeroDivisionError).

    The split itself is NOT re-derived here: it is the engine's ``even_split_pct``,
    scaled down to ``total_pct`` exactly the way ``build_symbol_targets`` scales a
    leftover (4dp). Sharing the one function is what makes it impossible for the
    defaults shown on the page to drift from the ones the engine computes.
    """
    parts = even_split_pct(count)
    if not parts:
        return []
    return [round(total_pct * pct / 100.0, 4) for pct in parts]


def _normalise_symbols(symbols) -> List[str]:
    """Uppercase, strip, drop blanks and de-duplicate, PRESERVING the given order."""
    out: List[str] = []
    seen = set()
    for raw in symbols or []:
        symbol = (raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def get_symbol_rows(account_id: int, label: str) -> Dict[str, PortfolioAllocationSymbol]:
    """The STORED weight rows of one label, keyed by symbol.

    Only symbols the user has actually edited have a row, so this is normally a
    subset of the label's symbols. Use ``get_symbol_weights()`` when you need a
    weight for every symbol.
    """
    label = (label or "").strip()
    if not label:
        return {}
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
            )
        ).all()
        rows = list(rows)
        session.expunge_all()
        return {row.symbol: row for row in rows}


def get_symbol_weights(account_id: int, label: str, symbols) -> Dict[str, float]:
    """``{symbol: weight_pct}`` for every symbol of a label, defaults filled in.

    Weights are 1-100 WITHIN the label. Rows are lazy, so a symbol with no row is
    not an error: the un-stored symbols share whatever is left of 100% evenly
    (all of it when nothing is stored), with the remainder on the last one.
    Symbols are normalised (.strip().upper()), duplicates collapse, and the order
    of ``symbols`` is preserved in the returned dict.

    Unlike ``get_symbol_rows()``, this never returns an empty dict for a label you
    passed symbols for -- ``{}`` here means you asked about no symbols at all.
    """
    syms = _normalise_symbols(symbols)
    if not syms:
        return {}
    stored_rows = get_symbol_rows(account_id, label)
    stored = {s: float(stored_rows[s].weight_pct) for s in syms if s in stored_rows}
    unstored = [s for s in syms if s not in stored]
    remaining = max(0.0, 100.0 - sum(stored.values()))
    filled = dict(zip(unstored, _split_evenly(remaining, len(unstored))))
    return {s: stored[s] if s in stored else filled[s] for s in syms}


def set_symbol_weight(account_id: int, label: str, symbol: str, *,
                      weight_pct: Optional[float] = None,
                      comment: Optional[str] = None) -> PortfolioAllocationSymbol:
    """Create or update ONE symbol's weight/comment inside a label.

    ``None`` for a field leaves it unchanged; pass ``""`` to clear a comment.
    Writing a row makes the weight explicit -- the symbol stops taking the
    even-split default, which is exactly what the user asked for by editing it.

    Raises:
        ValueError: when ``label`` or ``symbol`` is blank.
    """
    label = (label or "").strip()
    symbol = (symbol or "").strip().upper()
    if not label or not symbol:
        raise ValueError("set_symbol_weight requires a non-empty label and symbol")
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
                PortfolioAllocationSymbol.symbol == symbol,
            )
        ).first()
        if row is None:
            row = PortfolioAllocationSymbol(account_id=account_id, label=label, symbol=symbol)
            session.add(row)
        if weight_pct is not None:
            row.weight_pct = float(weight_pct)
        if comment is not None:
            row.comment = comment
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def remove_symbol_weight(account_id: int, label: str, symbol: str) -> bool:
    """Drop a symbol's stored weight so it returns to the even-split default.

    Returns True when a row was deleted, False when the symbol had none.
    """
    label = (label or "").strip()
    symbol = (symbol or "").strip().upper()
    if not label or not symbol:
        return False
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationSymbol).where(
                PortfolioAllocationSymbol.account_id == account_id,
                PortfolioAllocationSymbol.label == label,
                PortfolioAllocationSymbol.symbol == symbol,
            )
        ).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
    return True
```

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: PASS — `16 passed` (Task 9's deferred
`test_remove_managed_label_also_removes_its_symbol_weights` is now green too)

- [x] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): lazy symbol weights with even-split defaults"
```

---

### Task 11: Store — the per-account allocation config (valuation mode)

Spec decision 5a. `valuation_mode` selects what "current value" means in three places at once
— the allocatable base, the displayed percentages, and every delta — so it is a table, not
session storage. This task is the persistence half; Task 25 teaches the engine to honour it and
Task 66 puts the toggle on the page.

**Depends on Task 16** for the two `VALUATION_MODE_*` constants.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- per-account allocation config (valuation mode) ------------------------

def test_get_allocation_config_creates_the_row_with_spec_defaults(account_id):
    config = store.get_allocation_config(account_id)
    assert config.valuation_mode == "cost"
    assert config.allow_fractional is False
    # Reading twice must not create a second row (account_id is unique).
    assert store.get_allocation_config(account_id).id == config.id


def test_set_valuation_mode_persists_market(account_id):
    store.set_allocation_config(account_id, valuation_mode="market")
    assert store.get_allocation_config(account_id).valuation_mode == "market"


def test_set_allocation_config_leaves_unpassed_fields_untouched(account_id):
    store.set_allocation_config(account_id, valuation_mode="market", allow_fractional=True)
    store.set_allocation_config(account_id, allow_fractional=False)
    config = store.get_allocation_config(account_id)
    assert config.valuation_mode == "market"
    assert config.allow_fractional is False


def test_set_allocation_config_rejects_an_unknown_valuation_mode(account_id):
    with pytest.raises(ValueError):
        store.set_allocation_config(account_id, valuation_mode="marketish")


def test_valuation_mode_is_scoped_per_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.set_allocation_config(account_id, valuation_mode="market")
    assert store.get_allocation_config(other.id).valuation_mode == "cost"


def test_set_allocation_config_bumps_updated_at(account_id):
    first = store.get_allocation_config(account_id).updated_at
    store.set_allocation_config(account_id, valuation_mode="market")
    assert store.get_allocation_config(account_id).updated_at >= first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'get_allocation_config'`

- [ ] **Step 3: Write minimal implementation**

First widen the store's EXISTING module-level engine import (added in Task 10) in
`packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python
from ba2_common.core.portfolio_allocation import (
    VALUATION_MODE_COST,
    VALUATION_MODE_MARKET,
    even_split_pct,
)
```

Then append to the end of the same file:

```python


# ---------------------------------------------------------------------------
# Per-account config: valuation mode + the remembered fractional choice
# ---------------------------------------------------------------------------

def get_allocation_config(account_id: int) -> PortfolioAllocationConfig:
    """The account's allocation config, CREATING it with the defaults on first use.

    Defaults are ``valuation_mode="cost"`` and ``allow_fractional=False`` (spec
    decision 5a). Always returns a row, never ``None``: the page must always be
    able to state which valuation mode produced the numbers on screen.

    Pass the returned ``valuation_mode`` to the engine. It has to be passed: all
    three engine entry points (``compute_base_notional``, ``compute_allocation``,
    ``compute_label_investment``) take it as a REQUIRED keyword with no default,
    precisely so the base and the deltas cannot end up on different definitions of
    "current value". Their defaults used to disagree -- cost for the base, market
    for the solvers -- and a call site that forgot the keyword got both.
    """
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationConfig).where(
                PortfolioAllocationConfig.account_id == account_id)
        ).first()
        if row is None:
            row = PortfolioAllocationConfig(account_id=account_id)
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info(f"Created default allocation config for account {account_id} "
                        f"(valuation_mode={VALUATION_MODE_COST}, allow_fractional=False)")
        session.expunge(row)
        return row


def set_allocation_config(account_id: int, *,
                          valuation_mode: Optional[str] = None,
                          allow_fractional: Optional[bool] = None) -> PortfolioAllocationConfig:
    """Update the account's allocation config; ``None`` leaves a field unchanged.

    Raises:
        ValueError: when ``valuation_mode`` is neither ``VALUATION_MODE_COST`` nor
        ``VALUATION_MODE_MARKET``. A typo'd mode would silently reinterpret every
        percentage on the page -- and the engine only rejects it later, at plan
        time -- so it is refused here rather than stored.
    """
    if valuation_mode is not None and valuation_mode not in (
            VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")

    get_allocation_config(account_id)   # ensure the row exists
    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationConfig).where(
                PortfolioAllocationConfig.account_id == account_id)
        ).one()
        if valuation_mode is not None:
            row.valuation_mode = valuation_mode
        if allow_fractional is not None:
            row.allow_fractional = bool(allow_fractional)
        row.updated_at = DateTime.now(timezone.utc)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        logger.info(f"Allocation config for account {account_id}: "
                    f"valuation_mode={row.valuation_mode}, allow_fractional={row.allow_fractional}")
        return row
```

**As landed:** the `VALUATION_MODE_*` import is MODULE-LEVEL, not function-local as this plan
originally specified. The original rationale — "it keeps the store's module-level import graph to
`db` + `models` + `logger`, so importing the store never drags the engine in" — was already false
by the time this task ran: Task 10 added a module-level
`from ba2_common.core.portfolio_allocation import even_split_pct`, and the store's module
docstring already announces that it borrows both the two `VALUATION_MODE_*` constants and
`even_split_pct` from the engine. A second, function-local import of the same module would have
bought nothing and left two contradictory conventions in one file. The dependency stays
one-directional and cycle-free (store -> engine, never the reverse), which is the property that
actually mattered.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: PASS — `22 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): per-account valuation mode and fractional config"
```

---

### Task 12: Store — income ledger upsert and reads

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- income ledger ---------------------------------------------------------

def test_upsert_income_event_inserts_a_new_event(account_id):
    row = store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert row.id is not None
    assert store.get_open_income_total(account_id) == 1000.0


def test_reupserting_the_same_external_id_updates_instead_of_duplicating(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1250.0)
    events = store.get_open_income_events(account_id)
    assert len(events) == 1
    assert events[0].amount == 1250.0


def test_reupserting_an_event_does_not_reset_what_was_already_consumed(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    store.consume_income(account_id, 400.0)
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.get_open_income_total(account_id) == 600.0


def test_upsert_income_event_rejects_a_blank_external_id(account_id):
    with pytest.raises(ValueError):
        store.upsert_income_event(account_id, "  ", date(2026, 8, 1), "DEPOSIT", 100.0)


def test_open_income_events_are_ordered_oldest_first(account_id):
    store.upsert_income_event(account_id, "b", date(2026, 8, 10), "DIVIDEND", 50.0, symbol="AAPL")
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 500.0)
    assert [e.external_id for e in store.get_open_income_events(account_id)] == ["a", "b"]


def test_income_events_since_excludes_older_events(account_id):
    store.upsert_income_event(account_id, "old", date(2026, 6, 1), "DEPOSIT", 100.0)
    store.upsert_income_event(account_id, "new", date(2026, 8, 15), "DEPOSIT", 200.0)
    recent = store.get_income_events_since(account_id, date(2026, 8, 1))
    assert [e.external_id for e in recent] == ["new"]
```

`test_reupserting_an_event_does_not_reset_what_was_already_consumed` uses `consume_income`,
which arrives in Task 13 — it stays red until then. That is the point: a re-sync must never
resurrect money the platform already spent.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'upsert_income_event'`

- [ ] **Step 3: Write minimal implementation**

Append to the end of `packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python


# ---------------------------------------------------------------------------
# Income ledger
# ---------------------------------------------------------------------------

def upsert_income_event(account_id: int, external_id: str, event_date: Date,
                        event_type: str, amount: float,
                        symbol: Optional[str] = None) -> PortfolioIncomeEvent:
    """Insert or update one deposit/dividend, keyed on ``(account_id, external_id)``.

    ``external_id`` is the BROKER's own activity id, which makes re-syncing the
    same window idempotent -- exactly as ``OptionActivity`` does. Re-upserting an
    existing event refreshes date/type/amount/symbol and NEVER touches
    ``consumed_amount``: money already spent stays spent.

    ``event_type`` is a plain str -- pass ``CASH_TRANSFER_DEPOSIT`` or
    ``CASH_TRANSFER_DIVIDEND`` from ``ba2_common.core.account_types``, never a
    bare literal. Withdrawals are not income and must not be sent here.

    Raises:
        ValueError: when ``external_id`` is blank -- the idempotency key would
        collapse every event of the account onto one row.
    """
    external_id = (external_id or "").strip()
    if not external_id:
        raise ValueError("upsert_income_event requires a non-empty external_id")
    with get_db() as session:
        row = session.exec(
            select(PortfolioIncomeEvent).where(
                PortfolioIncomeEvent.account_id == account_id,
                PortfolioIncomeEvent.external_id == external_id,
            )
        ).first()
        if row is None:
            row = PortfolioIncomeEvent(
                account_id=account_id, external_id=external_id, event_date=event_date,
                event_type=event_type, amount=float(amount), symbol=symbol,
            )
            session.add(row)
        else:
            row.event_date = event_date
            row.event_type = event_type
            row.amount = float(amount)
            row.symbol = symbol
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def get_open_income_events(account_id: int) -> List[PortfolioIncomeEvent]:
    """Income events with money left, OLDEST FIRST (event_date, then id).

    That is exactly the order ``consume_income()`` spends them in.
    """
    with get_db() as session:
        rows = session.exec(
            select(PortfolioIncomeEvent)
            .where(PortfolioIncomeEvent.account_id == account_id)
            .order_by(PortfolioIncomeEvent.event_date, PortfolioIncomeEvent.id)
        ).all()
        rows = [row for row in rows if row.open_amount > 0]
        session.expunge_all()
        return rows


def get_open_income_total(account_id: int) -> float:
    """Total un-consumed income of an account; 0.0 when the ledger is empty."""
    return float(sum(row.open_amount for row in get_open_income_events(account_id)))


def get_income_events_since(account_id: int, since: Date) -> List[PortfolioIncomeEvent]:
    """Every income event on or after ``since``, NEWEST first -- the 30-day panel."""
    with get_db() as session:
        rows = session.exec(
            select(PortfolioIncomeEvent)
            .where(PortfolioIncomeEvent.account_id == account_id,
                   PortfolioIncomeEvent.event_date >= since)
            .order_by(PortfolioIncomeEvent.event_date.desc(), PortfolioIncomeEvent.id.desc())
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: `27 passed, 1 failed` — the only failure is
`test_reupserting_an_event_does_not_reset_what_was_already_consumed` with
`AttributeError: ... has no attribute 'consume_income'`. Task 13 turns it green.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): idempotent income-event upsert and ledger reads"
```

---

### Task 13: Store — FIFO income consumption

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- FIFO consumption ------------------------------------------------------

def test_consuming_with_a_zero_net_buy_value_consumes_nothing(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.consume_income(account_id, 0.0) == []
    assert store.get_open_income_total(account_id) == 1000.0


def test_consuming_partially_leaves_a_remainder_open(account_id):
    event = store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.consume_income(account_id, 300.0) == [(event.id, 300.0)]
    open_events = store.get_open_income_events(account_id)
    assert len(open_events) == 1
    assert open_events[0].consumed_amount == 300.0
    assert open_events[0].open_amount == 700.0


def test_consuming_spends_the_oldest_event_first_then_spills_over(account_id):
    first = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    second = store.upsert_income_event(account_id, "b", date(2026, 8, 5), "DIVIDEND", 500.0)
    assert store.consume_income(account_id, 250.0) == [(first.id, 100.0), (second.id, 150.0)]
    assert store.get_open_income_total(account_id) == 350.0


def test_consuming_more_than_the_ledger_holds_empties_it_without_error(account_id):
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    consumed = store.consume_income(account_id, 9999.0)
    assert sum(amount for _, amount in consumed) == 100.0
    assert store.get_open_income_total(account_id) == 0.0


def test_consuming_an_empty_ledger_returns_nothing(account_id):
    assert store.consume_income(account_id, 500.0) == []


def test_fully_consumed_events_drop_out_of_the_open_list(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.consume_income(account_id, 100.0)
    assert store.get_open_income_events(account_id) == []
    assert store.get_open_income_total(account_id) == 0.0


def test_consumption_is_scoped_to_one_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.upsert_income_event(other.id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.consume_income(account_id, 100.0)
    assert store.get_open_income_total(account_id) == 0.0
    assert store.get_open_income_total(other.id) == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'consume_income'`

- [ ] **Step 3: Write minimal implementation**

Append to the end of `packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python


def consume_income(account_id: int, net_buy_value: float) -> List[Tuple[int, float]]:
    """FIFO-consume the income ledger against a run's NET buy value.

    ``net_buy_value`` is ``max(0, submitted_buy_value - submitted_sell_value)``: a
    rebalance funded entirely by its own sells consumes nothing. Anything ``<= 0``
    consumes nothing and returns ``[]``.

    Events are spent oldest-first and the LAST one may be PARTIAL -- its remainder
    stays open for the next run.

    Returns:
        List[Tuple[int, float]]: ``[(income_event_id, amount_consumed)]`` for the
        events actually touched, oldest first. The total may be LESS than
        ``net_buy_value`` when the ledger cannot cover it; buying power, not the
        ledger, is the feasibility constraint.
    """
    remaining = float(net_buy_value)
    if remaining <= 0:
        return []
    consumed: List[Tuple[int, float]] = []
    with get_db() as session:
        rows = session.exec(
            select(PortfolioIncomeEvent)
            .where(PortfolioIncomeEvent.account_id == account_id)
            .order_by(PortfolioIncomeEvent.event_date, PortfolioIncomeEvent.id)
        ).all()
        for row in rows:
            if remaining <= 0:
                break
            open_amount = row.open_amount
            if open_amount <= 0:
                continue
            take = min(open_amount, remaining)
            row.consumed_amount = (row.consumed_amount or 0.0) + take
            session.add(row)
            consumed.append((row.id, take))
            remaining -= take
        if consumed:
            session.commit()
    logger.info(f"Allocation run consumed {len(consumed)} income event(s) for account "
                f"{account_id} against a net buy value of {net_buy_value:.2f}")
    return consumed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: PASS — `35 passed` (Task 12's deferred re-upsert test is now green too)

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): FIFO income consumption against a run's net buy value"
```

---

### Task 14: Store — allocation run audit

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- run audit -------------------------------------------------------------

def test_record_allocation_run_persists_the_plan_and_order_ids(account_id):
    run = store.record_allocation_run(
        account_id, "REBALANCE", {"rows": [{"symbol": "TSLA"}], "scale_factor": 0.61},
        base_notional=50_000.0, available_buying_power=20_000.0, allow_fractional=True,
        submitted_buy_value=8000.0, submitted_sell_value=3000.0, order_ids=[7, 8])
    assert run.id is not None
    stored = store.get_recent_runs(account_id)[0]
    assert stored.mode == "REBALANCE"
    assert stored.plan_json["scale_factor"] == 0.61
    assert stored.order_ids == [7, 8]
    assert stored.allow_fractional is True
    assert stored.net_buy_value == 5000.0


def test_recent_runs_are_newest_first_and_respect_the_limit(account_id):
    for i in range(3):
        store.record_allocation_run(account_id, "INVEST_LABEL", {}, scope_label=f"L{i}")
    runs = store.get_recent_runs(account_id, limit=2)
    assert [r.scope_label for r in runs] == ["L2", "L1"]


def test_recent_runs_is_empty_for_an_account_that_never_ran(account_id):
    assert store.get_recent_runs(account_id) == []


def test_update_allocation_run_totals_writes_back_what_was_actually_submitted(account_id):
    """The run row is created BEFORE submission so its id can be stamped into every
    order comment, then updated with the real totals afterwards."""
    run = store.record_allocation_run(account_id, "REBALANCE", {"rows": []},
                                      base_notional=10_000.0)
    updated = store.update_allocation_run_totals(
        run.id, submitted_buy_value=1600.0, submitted_sell_value=400.0, order_ids=[101, 102])
    assert updated.submitted_buy_value == 1600.0
    assert updated.submitted_sell_value == 400.0
    assert updated.order_ids == [101, 102]
    assert updated.net_buy_value == 1200.0
    assert store.get_recent_runs(account_id)[0].order_ids == [101, 102]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'record_allocation_run'`

- [ ] **Step 3: Write minimal implementation**

Append to the end of `packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python


# ---------------------------------------------------------------------------
# Run audit
# ---------------------------------------------------------------------------

def record_allocation_run(account_id: int, mode: str, plan_json: Dict[str, Any], *,
                          scope_label: Optional[str] = None,
                          base_notional: float = 0.0,
                          available_buying_power: float = 0.0,
                          allow_fractional: bool = False,
                          submitted_buy_value: float = 0.0,
                          submitted_sell_value: float = 0.0,
                          order_ids: Optional[List[int]] = None) -> PortfolioAllocationRun:
    """Persist the audit row for one allocation run.

    ``mode`` is a plain str -- pass ``ALLOCATION_MODE_REBALANCE`` or
    ``ALLOCATION_MODE_INVEST_LABEL`` from ``ba2_common.core.portfolio_allocation``.
    ``plan_json`` is ``AllocationPlan.to_dict()`` captured at submit time, which
    keeps the dry-run reproducible after the weights change.

    The live service calls this BEFORE submitting, with zero submitted values, so
    the run id exists to stamp into every order comment; it then calls
    ``update_allocation_run_totals`` with what was actually submitted.

    This does NOT consume income: call ``consume_income(account_id,
    run.net_buy_value)`` next, so the two writes stay separately auditable.
    """
    with get_db() as session:
        row = PortfolioAllocationRun(
            account_id=account_id,
            mode=mode,
            scope_label=scope_label,
            base_notional=float(base_notional),
            available_buying_power=float(available_buying_power),
            allow_fractional=bool(allow_fractional),
            plan_json=plan_json or {},
            submitted_buy_value=float(submitted_buy_value),
            submitted_sell_value=float(submitted_sell_value),
            order_ids=list(order_ids or []),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        logger.info(f"Recorded allocation run {row.id} ({mode}) for account {account_id}: "
                    f"buys {row.submitted_buy_value:.2f} / sells {row.submitted_sell_value:.2f}")
        return row


def update_allocation_run_totals(run_id: int, *,
                                 submitted_buy_value: float,
                                 submitted_sell_value: float,
                                 order_ids: List[int]) -> PortfolioAllocationRun:
    """Write back what a run ACTUALLY submitted, after the orders have gone out.

    Raises:
        InstanceNotFound: when the run row is gone. That is a real inconsistency
        (the run was recorded seconds earlier) and must not be swallowed.
    """
    from ba2_common.core.db import InstanceNotFound

    with get_db() as session:
        row = session.exec(
            select(PortfolioAllocationRun).where(PortfolioAllocationRun.id == run_id)
        ).first()
        if row is None:
            raise InstanceNotFound(f"PortfolioAllocationRun {run_id} not found")
        row.submitted_buy_value = float(submitted_buy_value)
        row.submitted_sell_value = float(submitted_sell_value)
        row.order_ids = list(order_ids or [])
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def get_recent_runs(account_id: int, limit: int = 20) -> List[PortfolioAllocationRun]:
    """The account's most recent allocation runs, NEWEST first."""
    with get_db() as session:
        rows = session.exec(
            select(PortfolioAllocationRun)
            .where(PortfolioAllocationRun.account_id == account_id)
            .order_by(PortfolioAllocationRun.created_at.desc(), PortfolioAllocationRun.id.desc())
            .limit(limit)
        ).all()
        rows = list(rows)
        session.expunge_all()
        return rows
```

The `id.desc()` tiebreak on top of `created_at.desc()` matters: three runs recorded in the same
test tick share a `created_at` to the microsecond, and without the tiebreak their order is
whatever SQLite returns.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: PASS — `39 passed`

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): allocation run audit rows"
```

---

### Task 15: Store — account-deletion cleanup

Foreign keys on these tables are declarative only: the live DB runs with
`PRAGMA foreign_keys = 0`, so `ondelete="CASCADE"` never fires. Deleting an account would
otherwise orphan its allocation rows forever, and the next account to reuse that id would
inherit them. Task 67 wires this helper into `ui/pages/settings.py:delete_account`.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- account deletion cleanup ---------------------------------------------

def test_deleting_account_allocation_data_removes_every_table_row(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=100.0)
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.record_allocation_run(account_id, "REBALANCE", {})
    store.set_allocation_config(account_id, valuation_mode="market")
    counts = store.delete_account_allocation_data(account_id)
    assert counts == {"config": 1, "labels": 1, "symbols": 1, "income_events": 1, "runs": 1}
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, "ARK26") == {}
    assert store.get_open_income_events(account_id) == []
    assert store.get_recent_runs(account_id) == []
    # The config row is gone, so the next read recreates it with the defaults.
    assert store.get_allocation_config(account_id).valuation_mode == "cost"


def test_deleting_allocation_data_leaves_other_accounts_alone(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_managed_label(other.id, "ARK26", target_pct=100.0)
    store.delete_account_allocation_data(account_id)
    assert [r.label for r in store.get_managed_labels(other.id)] == ["ARK26"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'delete_account_allocation_data'`

- [ ] **Step 3: Write minimal implementation**

Append to the end of `packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python


def delete_account_allocation_data(account_id: int) -> Dict[str, int]:
    """Delete every allocation row of an account. Returns per-table delete counts.

    The live DB runs with ``PRAGMA foreign_keys = 0``, so the ``ondelete="CASCADE"``
    declared on these tables NEVER fires. Account deletion must call this
    explicitly, exactly as the ``AccountSetting`` cleanup loop in
    ``ui/pages/settings.py`` does.
    """
    counts: Dict[str, int] = {}
    with get_db() as session:
        for key, model in (("config", PortfolioAllocationConfig),
                           ("labels", PortfolioAllocationLabel),
                           ("symbols", PortfolioAllocationSymbol),
                           ("income_events", PortfolioIncomeEvent),
                           ("runs", PortfolioAllocationRun)):
            rows = session.exec(select(model).where(model.account_id == account_id)).all()
            for row in rows:
                session.delete(row)
            counts[key] = len(rows)
        session.commit()
    logger.info(f"Deleted portfolio allocation data for account {account_id}: {counts}")
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: PASS — `41 passed`

Then re-run the other two files in this section to confirm nothing regressed:

```bash
venv/bin/python -m pytest tests/test_portfolio_allocation_models.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_migration.py -v
```

Expected: `12 passed` and `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): explicit account-deletion cleanup for allocation tables"
```

---

## Section C — Pure allocation engine

**Prerequisite:** `packages/common/ba2_common/core/account_types.py` must already exist (Task 27)
— the engine imports `MarginInfo` and `OrderImpact` from it. Verify before starting:

```bash
ls packages/common/ba2_common/core/account_types.py
```
Expected: the path prints. If you get `No such file or directory`, do Task 27 first.

**How to run these tests.** `pytest.ini` sets `testpaths = tests`, so the package test file is
only collected when you name it explicitly:

```bash
venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v
```

The venv on this Mac is `venv/`, not `.venv/`. Never run the whole suite to judge this work —
it fails non-deterministically from a pre-existing session leak.

---

### Task 16: Engine value objects and an empty plan

**Files:**
- Create: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [x] **Step 1: Write the failing test**

Create `packages/common/tests/test_portfolio_allocation.py`:

```python
"""Unit tests for the pure portfolio-allocation engine (no DB, no broker, no UI)."""
import json

import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, LabelTarget, PositionState, SymbolTarget,
)
from ba2_common.core.account_types import MarginInfo, OrderImpact
from ba2_common.core.types import OrderDirection


def _pos(symbol, price, quantity=0.0, cost_basis=0.0):
    return PositionState(symbol=symbol, quantity=quantity, cost_basis=cost_basis, price=price)


def test_allocation_row_side_buy_reports_is_buy_true():
    row = AllocationRow(symbol="AAA", side=OrderDirection.BUY)
    assert row.is_buy is True
    assert row.is_sell is False


def test_allocation_row_skipped_buy_reports_is_buy_false():
    row = AllocationRow(symbol="AAA", side=OrderDirection.BUY, skipped=True)
    assert row.is_buy is False


def test_plan_buy_rows_sorted_descending_by_estimated_value():
    small = AllocationRow(symbol="S", side=OrderDirection.BUY, estimated_value=10.0)
    big = AllocationRow(symbol="B", side=OrderDirection.BUY, estimated_value=99.0)
    plan = AllocationPlan(rows=[small, big])
    assert [r.symbol for r in plan.buy_rows] == ["B", "S"]


def test_plan_net_buy_value_is_buys_minus_sells_floored_at_zero():
    assert AllocationPlan(total_buy_value=100.0, total_sell_value=30.0).net_buy_value == 70.0
    assert AllocationPlan(total_buy_value=10.0, total_sell_value=30.0).net_buy_value == 0.0


def test_plan_to_dict_is_json_serialisable():
    plan = AllocationPlan(rows=[AllocationRow(symbol="AAA", side=OrderDirection.BUY)])
    blob = json.dumps(plan.to_dict())
    assert '"side": "BUY"' in blob


def test_compute_allocation_no_labels_returns_empty_plan():
    plan = pa.compute_allocation(0.0, 0.0, [], {}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows == []
    assert plan.total_buy_value == 0.0
    assert plan.scale_factor == 1.0
    assert plan.unallocatable_pct == 0.0


def test_position_fetch_failed_is_a_runtime_error_subclass():
    """Defined HERE, in the pure engine, so both the live service and the UI's
    view-model can raise/catch the same class without either importing the other."""
    assert issubclass(pa.PositionFetchFailed, RuntimeError)
```

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`

Expected: collection error —
```
packages/common/tests/test_portfolio_allocation.py:6: in <module>
    from ba2_common.core import portfolio_allocation as pa
E   ImportError: cannot import name 'portfolio_allocation' from 'ba2_common.core'
```

- [x] **Step 3: Write minimal implementation**

Create `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
"""Portfolio allocation arithmetic (pure; no DB, no broker, no UI).

Turns target percentages into per-symbol share deltas:

    label_notional  = base_notional * label.target_pct / 100
    symbol_notional = label_notional * symbol.weight_pct / 100   (targets SUM
                      when a symbol carries more than one managed label)
    target_quantity = round_quantity(symbol_notional, price, ...)
    delta_quantity  = target_quantity - current_quantity
    bp_cost         = |delta_notional| * bp_factor(symbol)       (buys only)

When ``sum(bp_cost of buys) > available_buying_power`` every BUY scales down
pro-rata and the plan records ``scale_factor``. Sells never scale.
"""

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.account_types import MarginInfo, OrderImpact  # noqa: F401 (re-exported)
from ba2_common.core.types import OrderDirection

# ---- module constants (exact spellings; use these, never bare literals) ----

ALLOCATION_MODE_REBALANCE = "REBALANCE"
ALLOCATION_MODE_INVEST_LABEL = "INVEST_LABEL"

#: How "current value" is measured -- see ``current_value`` and decision 5a.
#: PLAIN str, matching the ``portfolio_allocation_config.valuation_mode`` column.
VALUATION_MODE_COST = "cost"
VALUATION_MODE_MARKET = "market"

#: Decimal places used for a fractional quantity when the broker publishes no
#: ``min_trade_increment``.
DEFAULT_FRACTIONAL_DECIMALS = 4

#: Tolerance (percentage points) when checking that label targets total 100.
LABEL_TOTAL_TOLERANCE_PCT = 0.01

#: Quantities closer to zero than this are exactly zero (float noise guard).
QUANTITY_EPSILON = 1e-9

# Reason strings attached to AllocationRow.reasons / AllocationPlan.warnings.
# Pinned here so the UI and the tests agree on the exact text.
REASON_NO_PRICE = "no price - skipped"
REASON_NOT_MARGINABLE = "⚠ not marginable"
REASON_FRACTIONAL = "fractional"
REASON_WHOLE_SHARE_FLOOR = "rounded down to whole shares"
REASON_NEGATIVE_CLAMPED = "negative target clamped to 0"
REASON_CLOSE_TO_ZERO = "target 0 - close position"
REASON_MULTI_LABEL_FMT = "⚠ also in {labels}"
REASON_SCALED_FMT = "scaled ×{factor:.2f} to fit buying power"
WARNING_EMPTY_LABEL_FMT = "label '{label}' has no symbols - {pct:.2f}% unallocated"
WARNING_PRECHECK_DISAGREED_FMT = "broker precheck disagreed on {symbol} - re-solved"

# Validation messages from ``validate_label_targets``. Pinned so the UI and the
# tests agree on the exact text.
ERROR_LABEL_TOTAL_FMT = "label targets total {total:.2f}% - must total 100%"
ERROR_LABEL_NEGATIVE_FMT = "label '{label}' has a negative target ({pct:.2f}%)"
ERROR_LABEL_DUPLICATE_FMT = "duplicate label '{label}'"
ERROR_LABEL_NO_SYMBOLS_FMT = "label '{label}' has target {pct:.2f}% but no symbols"


class PositionFetchFailed(RuntimeError):
    """The broker's position fetch FAILED (``get_positions()`` returned ``None``).

    Distinct from a genuinely flat account (``[]``). Conflating the two on
    2026-07-03 mass-closed 8 real open transactions during a DNS outage, which is
    why this is an exception rather than an empty dict.

    It lives in the PURE engine so that the live service
    (``core/portfolio_allocation_service.py``) and the UI view-model
    (``ui/utils/portfolio_allocation_view.py``) can raise and catch the same
    class without either importing the other.
    """


@dataclass
class PositionState:
    """What the account CURRENTLY holds in one symbol, as the engine sees it.

    ``price`` is ``None`` when no live quote is available; the engine then SKIPS
    the symbol with a reason rather than sizing it at a guessed price (platform
    rule: no fallback values for live data).

    ``transaction_ids`` are the OPEN Transaction ids for the symbol, oldest
    first -- submission consumes them FIFO.
    """
    symbol: str
    quantity: float = 0.0
    cost_basis: float = 0.0
    price: Optional[float] = None
    market_value: Optional[float] = None
    transaction_ids: List[int] = field(default_factory=list)


@dataclass
class SymbolTarget:
    """A symbol's weight WITHIN one label. ``weight_pct`` is 1-100, not 0-1."""
    symbol: str
    weight_pct: float
    comment: Optional[str] = None


@dataclass
class LabelTarget:
    """A managed label and its share of the base notional.

    ``target_pct`` is 1-100 of ``base_notional``. Across all managed labels of an
    account it must total exactly 100 before a REBALANCE may be submitted. An
    empty ``symbols`` list cannot absorb its percentage: the engine allocates it
    nothing and adds ``target_pct`` to ``AllocationPlan.unallocatable_pct``.
    """
    label: str
    target_pct: float
    symbols: List[SymbolTarget] = field(default_factory=list)
    comment: Optional[str] = None


@dataclass
class AllocationRow:
    """One symbol's line in a plan: where it is, where it should be, the delta.

    ``delta_quantity`` is SIGNED (positive = buy, negative = sell); ``side`` is
    the matching ``OrderDirection`` (``None`` when the delta is exactly zero or
    the row was skipped). ``estimated_value`` and ``bp_cost`` are always POSITIVE
    magnitudes. ``bp_cost`` is 0.0 for sells -- sells free buying power and never
    scale. ``fractional`` records the SIZING MODE: True when this row was rounded
    on the fractional grid (toggle on AND the broker calls the symbol
    fractionable), not merely when the resulting quantity has a decimal part.
    """
    symbol: str
    labels: List[str] = field(default_factory=list)
    price: Optional[float] = None
    current_quantity: float = 0.0
    current_cost_basis: float = 0.0
    target_notional: float = 0.0
    target_quantity: float = 0.0
    delta_quantity: float = 0.0
    side: Optional[OrderDirection] = None
    estimated_value: float = 0.0
    bp_cost: float = 0.0
    bp_factor: float = 1.0
    fractional: bool = False
    skipped: bool = False
    reasons: List[str] = field(default_factory=list)

    @property
    def is_buy(self) -> bool:
        return self.side == OrderDirection.BUY and not self.skipped

    @property
    def is_sell(self) -> bool:
        return self.side == OrderDirection.SELL and not self.skipped

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict for ``portfolio_allocation_run.plan_json``."""
        return {
            "symbol": self.symbol,
            "labels": list(self.labels),
            "price": self.price,
            "current_quantity": self.current_quantity,
            "current_cost_basis": self.current_cost_basis,
            "target_notional": self.target_notional,
            "target_quantity": self.target_quantity,
            "delta_quantity": self.delta_quantity,
            "side": self.side.value if self.side is not None else None,
            "estimated_value": self.estimated_value,
            "bp_cost": self.bp_cost,
            "bp_factor": self.bp_factor,
            "fractional": self.fractional,
            "skipped": self.skipped,
            "reasons": list(self.reasons),
        }


@dataclass
class AllocationPlan:
    """A full dry-run: one AllocationRow per symbol plus plan-level totals.

    ``scale_factor`` < 1.0 means every BUY was scaled down pro-rata because
    ``sum(bp_cost of buys) > available_buying_power``. Sells never scale.
    ``unallocatable_pct`` is the share of the base that no label could absorb
    (empty labels, skipped no-price symbols) and shows in the dry-run as cash
    left over. ``required_buying_power`` is the POST-scaling figure -- what the
    plan as displayed actually needs.
    """
    rows: List[AllocationRow] = field(default_factory=list)
    base_notional: float = 0.0
    available_buying_power: float = 0.0
    required_buying_power: float = 0.0
    bp_usage_pct: float = 0.0
    scale_factor: float = 1.0
    unallocatable_pct: float = 0.0
    total_buy_value: float = 0.0
    total_sell_value: float = 0.0
    allow_fractional: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def buy_rows(self) -> List[AllocationRow]:
        """Buys, DESCENDING by estimated value -- the submission order (a
        shortfall then truncates the smallest positions)."""
        return sorted((r for r in self.rows if r.is_buy),
                      key=lambda r: r.estimated_value, reverse=True)

    @property
    def sell_rows(self) -> List[AllocationRow]:
        """Sells, descending by estimated value. Submitted BEFORE any buy."""
        return sorted((r for r in self.rows if r.is_sell),
                      key=lambda r: r.estimated_value, reverse=True)

    @property
    def net_buy_value(self) -> float:
        """``max(0, buys - sells)`` -- exactly what consumes the income ledger."""
        return max(0.0, self.total_buy_value - self.total_sell_value)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict for ``portfolio_allocation_run.plan_json``."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "base_notional": self.base_notional,
            "available_buying_power": self.available_buying_power,
            "required_buying_power": self.required_buying_power,
            "bp_usage_pct": self.bp_usage_pct,
            "scale_factor": self.scale_factor,
            "unallocatable_pct": self.unallocatable_pct,
            "total_buy_value": self.total_buy_value,
            "total_sell_value": self.total_sell_value,
            "allow_fractional": self.allow_fractional,
            "warnings": list(self.warnings),
        }


def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float) -> AllocationPlan:
    """Solve a full REBALANCE. Skeleton: with no labels there is nothing to do."""
    return AllocationPlan(base_notional=float(base_notional or 0.0),
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
```

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `7 passed`

- [x] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): pure allocation engine value objects and empty plan"
```

---

### Task 17: Notional targeting from a flat account

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [x] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_even_split_two_symbols_targets_half_the_base_each():
    labels = [LabelTarget("ARK26", 100.0, [SymbolTarget("AAA", 50.0), SymbolTarget("BBB", 50.0)])]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 50.0)}
    plan = pa.compute_allocation(100_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].target_notional == 50_000.0
    assert by["AAA"].target_quantity == 500.0
    assert by["BBB"].target_quantity == 1000.0
    assert plan.total_buy_value == 100_000.0
    assert plan.scale_factor == 1.0
    assert plan.bp_usage_pct == pytest.approx(10.0)


def test_uneven_label_and_symbol_weights_multiply_through():
    labels = [
        LabelTarget("ARK26", 40.0, [SymbolTarget("AAA", 70.0), SymbolTarget("BBB", 30.0)]),
        LabelTarget("NDX", 60.0, [SymbolTarget("CCC", 100.0)]),
    ]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 20.0), "CCC": _pos("CCC", 250.0)}
    plan = pa.compute_allocation(100_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].target_quantity == 280.0
    assert by["BBB"].target_quantity == 600.0
    assert by["CCC"].target_quantity == 240.0


def test_symbol_in_two_labels_sums_targets_into_one_row():
    labels = [
        LabelTarget("ARK26", 50.0, [SymbolTarget("XXX", 100.0)]),
        LabelTarget("HighRisk", 50.0, [SymbolTarget("XXX", 100.0)]),
    ]
    plan = pa.compute_allocation(100_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert len(plan.rows) == 1
    row = plan.rows[0]
    assert row.target_notional == 100_000.0
    assert row.target_quantity == 1000.0
    assert row.labels == ["ARK26", "HighRisk"]
    assert pa.REASON_MULTI_LABEL_FMT.format(labels="ARK26, HighRisk") in row.reasons


def test_labels_totalling_ninety_percent_leave_ten_percent_undeployed():
    labels = [
        LabelTarget("A", 40.0, [SymbolTarget("AAA", 100.0)]),
        LabelTarget("B", 50.0, [SymbolTarget("BBB", 100.0)]),
    ]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 100.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.total_buy_value == 9_000.0


def test_unknown_margin_uses_default_bp_factor():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=2.0)
    assert plan.rows[0].bp_factor == 2.0
    assert plan.rows[0].bp_cost == 20_000.0


def test_held_symbol_with_no_managed_label_is_absent_from_the_plan():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    current = {"AAA": _pos("AAA", 100.0),
               "ZZZ": _pos("ZZZ", 100.0, quantity=50.0, cost_basis=5_000.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert [r.symbol for r in plan.rows] == ["AAA"]


def test_fractional_off_floors_the_quantity():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].target_quantity == 3.0
    assert plan.rows[0].estimated_value == 900.0


def test_round_quantity_returns_zero_for_a_non_positive_price():
    assert pa.round_quantity(1_000.0, 0.0, None, allow_fractional=False) == 0.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "even_split_two or uneven_label or two_labels_sums or ninety_percent or unknown_margin or no_managed_label or fractional_off_floors or round_quantity_returns_zero"`

Expected: FAIL — `8 failed`, including
```
E   AssertionError: assert [] == ['AAA']
E   IndexError: list index out of range
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'round_quantity'
```

- [x] **Step 3: Write minimal implementation**

Delete the placeholder `compute_allocation` at the bottom of
`packages/common/ba2_common/core/portfolio_allocation.py` and put these three functions in its
place:

```python
def round_quantity(target_notional: float, price: float, margin: Optional[MarginInfo],
                   *, allow_fractional: bool) -> float:
    """Convert a notional to a tradeable share quantity. Always rounds DOWN, so a
    plan never over-spends its notional. Whole shares only for now.

    Returns 0.0 when ``price <= 0``; the caller must have already skipped a
    ``None`` price.
    """
    if price is None or price <= 0:
        return 0.0
    if target_notional is None or target_notional <= 0:
        return 0.0
    return float(math.floor(float(target_notional) / float(price)))


def _finalise_totals(plan: AllocationPlan) -> None:
    """Fill the plan-level money totals from its rows."""
    plan.total_buy_value = sum(r.estimated_value for r in plan.rows if r.is_buy)
    plan.total_sell_value = sum(r.estimated_value for r in plan.rows if r.is_sell)
    plan.required_buying_power = sum(r.bp_cost for r in plan.rows if r.is_buy)
    plan.bp_usage_pct = (plan.required_buying_power / plan.available_buying_power * 100.0
                         if plan.available_buying_power > 0 else 0.0)


def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float) -> AllocationPlan:
    """Solve a full REBALANCE: notional targets for every managed label.

    A symbol carried by several managed labels gets ONE row whose targets SUM
    (decision 7 -- no enforcement). A symbol missing from ``margin`` falls back
    to the conservative ``default_bp_factor`` (the account multiplier), which
    under-deploys rather than over-commits.
    """
    current = current or {}
    margin = margin or {}
    plan = AllocationPlan(base_notional=float(base_notional or 0.0),
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
    targets = {}
    sym_labels = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        for st in lt.symbols or []:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + plan.base_notional * share / 100.0
            sym_labels.setdefault(st.symbol, []).append(lt.label)

    for symbol, target_notional in targets.items():
        ps = current.get(symbol)
        m = margin.get(symbol)
        row = AllocationRow(symbol=symbol, labels=list(sym_labels[symbol]))
        row.bp_factor = float(m.bp_factor) if m is not None else float(default_bp_factor)
        row.price = ps.price if ps is not None else None
        if len(row.labels) > 1:
            row.reasons.append(REASON_MULTI_LABEL_FMT.format(labels=", ".join(row.labels)))
        row.target_notional = target_notional
        row.target_quantity = round_quantity(target_notional, row.price, m,
                                             allow_fractional=allow_fractional)
        row.delta_quantity = row.target_quantity
        if row.delta_quantity > 0:
            row.side = OrderDirection.BUY
        row.estimated_value = row.delta_quantity * row.price
        row.bp_cost = row.estimated_value * row.bp_factor
        plan.rows.append(row)

    _finalise_totals(plan)
    return plan
```

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `15 passed`

- [x] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): notional targeting across managed labels"
```

---

### Task 18: Deltas against the current positions

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [x] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_held_above_target_produces_a_sell_of_the_difference():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    plan = pa.compute_allocation(5_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == 50.0
    assert row.delta_quantity == -50.0
    assert row.side == OrderDirection.SELL
    assert row.estimated_value == 5_000.0
    assert row.bp_cost == 0.0
    assert plan.total_sell_value == 5_000.0


def test_held_below_target_produces_a_top_up_buy():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=20.0, cost_basis=1_800.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == 100.0
    assert row.delta_quantity == 80.0
    assert row.side == OrderDirection.BUY
    assert row.estimated_value == 8_000.0


def test_zero_target_on_a_held_symbol_closes_the_position():
    labels = [
        LabelTarget("KEEP", 100.0, [SymbolTarget("AAA", 100.0)]),
        LabelTarget("EXIT", 0.0, [SymbolTarget("BBB", 100.0)]),
    ]
    current = {"AAA": _pos("AAA", 100.0),
               "BBB": _pos("BBB", 20.0, quantity=30.0, cost_basis=500.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["BBB"].target_quantity == 0.0
    assert by["BBB"].delta_quantity == -30.0
    assert by["BBB"].side == OrderDirection.SELL
    assert pa.REASON_CLOSE_TO_ZERO in by["BBB"].reasons


def test_already_on_target_produces_no_order():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert plan.buy_rows == []
    assert plan.sell_rows == []


def test_whole_share_mode_never_emits_a_fractional_delta():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.5, cost_basis=1_050.0)}
    plan = pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].delta_quantity == 9.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "held_above_target or held_below_target or zero_target_on_a_held or already_on_target or whole_share_mode"`

Expected: FAIL — `5 failed`, including
```
E   AssertionError: assert 100.0 == 80.0
E   AssertionError: assert 0.0 == -30.0
E   AssertionError: assert 20.0 == 9.0
```

- [x] **Step 3: Write minimal implementation**

Replace the whole `compute_allocation` function in
`packages/common/ba2_common/core/portfolio_allocation.py` with:

```python
def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float) -> AllocationPlan:
    """Solve a full REBALANCE: every managed label, buys and sells.

    ``delta_quantity = target_quantity - current_quantity``, signed. A target of
    zero on a held symbol closes it outright (``REASON_CLOSE_TO_ZERO``) --
    including a fractional holding, which a broker will always let you flatten.
    Any other delta is rounded TOWARDS ZERO to whole shares, so whole-share mode
    can never emit a 0.37-share order.
    """
    current = current or {}
    margin = margin or {}
    plan = AllocationPlan(base_notional=float(base_notional or 0.0),
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
    targets = {}
    sym_labels = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        for st in lt.symbols or []:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + plan.base_notional * share / 100.0
            sym_labels.setdefault(st.symbol, []).append(lt.label)

    for symbol, target_notional in targets.items():
        ps = current.get(symbol)
        m = margin.get(symbol)
        row = AllocationRow(symbol=symbol, labels=list(sym_labels[symbol]))
        row.bp_factor = float(m.bp_factor) if m is not None else float(default_bp_factor)
        row.current_quantity = float(ps.quantity) if ps is not None else 0.0
        row.current_cost_basis = float(ps.cost_basis) if ps is not None else 0.0
        row.price = ps.price if ps is not None else None
        if len(row.labels) > 1:
            row.reasons.append(REASON_MULTI_LABEL_FMT.format(labels=", ".join(row.labels)))
        row.target_notional = target_notional
        row.target_quantity = round_quantity(target_notional, row.price, m,
                                             allow_fractional=allow_fractional)
        delta = row.target_quantity - row.current_quantity
        if row.target_quantity <= 0 and row.current_quantity > 0:
            delta = -row.current_quantity
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        else:
            delta = math.floor(delta) if delta > 0 else -math.floor(-delta)
        if abs(delta) < QUANTITY_EPSILON:
            delta = 0.0
        row.delta_quantity = delta
        if delta > 0:
            row.side = OrderDirection.BUY
        elif delta < 0:
            row.side = OrderDirection.SELL
        row.estimated_value = abs(delta) * row.price
        row.bp_cost = row.estimated_value * row.bp_factor if delta > 0 else 0.0
        plan.rows.append(row)

    _finalise_totals(plan)
    return plan
```

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `20 passed`

- [x] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): signed deltas against held positions, close on zero target"
```

---

### Task 19: Fractional-share rounding

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [x] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_fractional_on_without_increment_rounds_to_four_decimals():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == pytest.approx(3.3333)
    assert row.fractional is True
    assert pa.REASON_FRACTIONAL in row.reasons


def test_fractional_on_rounds_down_to_the_brokers_min_trade_increment():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_trade_increment=0.01)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    assert plan.rows[0].target_quantity == pytest.approx(3.33)


def test_fractional_requested_on_a_non_fractionable_symbol_falls_back_to_whole_shares():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == 3.0
    assert row.fractional is False
    assert pa.REASON_WHOLE_SHARE_FLOOR in row.reasons


def test_quantity_below_min_order_size_is_dropped_to_zero():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_order_size=5.0)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    assert plan.rows[0].target_quantity == 0.0
    assert plan.rows[0].delta_quantity == 0.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "four_decimals or min_trade_increment or non_fractionable or min_order_size"`

Expected: FAIL — `4 failed`, including
```
E   assert 3.0 == 3.3333 ± 3.3e-06
E   assert 3.0 == 3.33 ± 3.3e-06
E   AssertionError: assert 'rounded down to whole shares' in []
E   AssertionError: assert 3.0 == 0.0
```

- [x] **Step 3: Write minimal implementation**

Replace the whole `round_quantity` function with:

```python
def round_quantity(target_notional: float, price: float, margin: Optional[MarginInfo],
                   *, allow_fractional: bool) -> float:
    """Convert a notional to a tradeable share quantity.

    Fractional OFF: ``floor(target_notional / price)``.
    Fractional ON: rounded DOWN to ``margin.min_trade_increment`` when the broker
    publishes one, otherwise to ``DEFAULT_FRACTIONAL_DECIMALS`` (4) places.
    Fractional ON but ``margin`` is None or ``margin.fractionable`` is False:
    falls back to whole shares.

    Always rounds DOWN, so a plan never over-spends its notional. Returns 0.0
    when ``price <= 0``; the caller must have already skipped a ``None`` price.
    A result below ``margin.min_order_size`` is returned as 0.0.
    """
    if price is None or price <= 0:
        return 0.0
    if target_notional is None or target_notional <= 0:
        return 0.0
    raw = float(target_notional) / float(price)
    if allow_fractional and margin is not None and margin.fractionable:
        inc = margin.min_trade_increment
        if inc and inc > 0:
            qty = round(math.floor(round(raw / inc, 9)) * inc, 10)
        else:
            f = 10.0 ** DEFAULT_FRACTIONAL_DECIMALS
            qty = math.floor(raw * f) / f
    else:
        qty = float(math.floor(raw))
    if qty <= 0:
        return 0.0
    if margin is not None and margin.min_order_size is not None and qty < margin.min_order_size:
        return 0.0
    return qty
```

Then, inside `compute_allocation`, replace this block:

```python
        row.target_quantity = round_quantity(target_notional, row.price, m,
                                             allow_fractional=allow_fractional)
        delta = row.target_quantity - row.current_quantity
        if row.target_quantity <= 0 and row.current_quantity > 0:
            delta = -row.current_quantity
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        else:
            delta = math.floor(delta) if delta > 0 else -math.floor(-delta)
```

with:

```python
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        row.target_quantity = round_quantity(target_notional, row.price, m,
                                             allow_fractional=allow_fractional)
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)
        delta = row.target_quantity - row.current_quantity
        if row.target_quantity <= 0 and row.current_quantity > 0:
            delta = -row.current_quantity
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        elif not frac:
            delta = float(math.floor(delta) if delta > 0 else -math.floor(-delta))
```

(The `float()` wrap keeps `delta_quantity: float` honest: `math.floor()` returns
an `int`, so before Task 19 the whole-share path stored an `int` in a field
annotated `float` and `to_dict()` serialised `9` rather than `9.0`. The
fractional path returns genuine floats, so this is the natural place to fix it.)

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `24 passed`

- [x] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): fractional-share rounding with min increment and min order size"
```

---

### Task 20: Buying-power constraint and pro-rata scaling

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [ ] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_buys_scale_pro_rata_when_they_exceed_buying_power():
    labels = [LabelTarget("ONE", 100.0, [SymbolTarget("MARG", 50.0), SymbolTarget("NONM", 50.0)])]
    current = {"MARG": _pos("MARG", 100.0), "NONM": _pos("NONM", 100.0)}
    margin = {
        "MARG": MarginInfo(symbol="MARG", bp_factor=1.0, marginable=True),
        "NONM": MarginInfo(symbol="NONM", bp_factor=2.0, marginable=False),
    }
    plan = pa.compute_allocation(100_000.0, 60_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=2.0)
    by = {r.symbol: r for r in plan.rows}
    assert plan.scale_factor == pytest.approx(0.4)
    assert by["MARG"].delta_quantity == 200.0
    assert by["NONM"].delta_quantity == 200.0
    assert by["MARG"].bp_cost == pytest.approx(20_000.0)
    assert by["NONM"].bp_cost == pytest.approx(40_000.0)
    assert plan.required_buying_power == pytest.approx(60_000.0)
    assert plan.bp_usage_pct == pytest.approx(100.0)
    assert pa.REASON_NOT_MARGINABLE in by["NONM"].reasons
    assert "scaled ×0.40 to fit buying power" in by["MARG"].reasons


def test_sells_are_never_scaled_down():
    labels = [LabelTarget("ONE", 100.0, [SymbolTarget("BUYME", 50.0), SymbolTarget("SELLME", 50.0)])]
    current = {"BUYME": _pos("BUYME", 100.0),
               "SELLME": _pos("SELLME", 100.0, quantity=1000.0, cost_basis=100_000.0)}
    plan = pa.compute_allocation(100_000.0, 1_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["SELLME"].delta_quantity == -500.0
    assert by["SELLME"].estimated_value == 50_000.0
    assert not any("scaled" in r for r in by["SELLME"].reasons)
    assert by["BUYME"].delta_quantity == 10.0
    assert plan.total_sell_value == 50_000.0


def test_zero_buying_power_skips_every_buy():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 0.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].skipped is True
    assert plan.total_buy_value == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "scale_pro_rata or never_scaled or zero_buying_power"`

Expected: FAIL — `3 failed`, including
```
E   assert 1.0 == 0.4 ± 4.0e-07
E   AssertionError: assert 500 == 10.0
E   AssertionError: assert False is True
```

- [ ] **Step 3: Write minimal implementation**

Insert this function immediately ABOVE `def compute_allocation(` in
`packages/common/ba2_common/core/portfolio_allocation.py`:

```python
def _apply_bp_scaling(rows: List[AllocationRow], available_buying_power: float, *,
                      allow_fractional: bool,
                      margin: Optional[Dict[str, MarginInfo]] = None) -> float:
    """Scale every BUY pro-rata until the plan fits available buying power.

    SELLS NEVER SCALE -- they free buying power. The re-rounded quantity is fed
    back through ``round_quantity`` (so increments and min order sizes still
    hold) and each row's ``bp_cost`` is scaled by the SAME quantity ratio rather
    than recomputed, which preserves a broker-precheck cost when one has been
    substituted. A buy that scales to zero shares is marked ``skipped``.

    ``margin`` may be omitted (the precheck path has no margin dict); a
    fractional row then keeps its fractional grid via a synthetic MarginInfo.

    Returns the scale factor applied (1.0 when the plan already fitted).
    """
    buys = [r for r in rows if r.is_buy]
    required = sum(r.bp_cost for r in buys)
    avail = float(available_buying_power or 0.0)
    if not buys or required <= avail:
        return 1.0
    scale = (avail / required) if required > 0 else 0.0
    for r in buys:
        m = (margin or {}).get(r.symbol)
        if m is None and r.fractional:
            m = MarginInfo(symbol=r.symbol, bp_factor=r.bp_factor, fractionable=True)
        prev_qty = r.delta_quantity
        qty = round_quantity(r.estimated_value * scale, r.price, m,
                             allow_fractional=allow_fractional)
        ratio = (qty / prev_qty) if prev_qty > 0 else 0.0
        r.delta_quantity = qty
        r.target_quantity = r.current_quantity + qty
        r.estimated_value = qty * r.price
        r.bp_cost = r.bp_cost * ratio
        r.reasons.append(REASON_SCALED_FMT.format(factor=scale))
        if qty <= 0:
            r.skipped = True
    return scale
```

Inside `compute_allocation`, add the not-marginable reason — replace:

```python
        if len(row.labels) > 1:
            row.reasons.append(REASON_MULTI_LABEL_FMT.format(labels=", ".join(row.labels)))
        row.target_notional = target_notional
```

with:

```python
        if len(row.labels) > 1:
            row.reasons.append(REASON_MULTI_LABEL_FMT.format(labels=", ".join(row.labels)))
        if m is not None and not m.marginable:
            row.reasons.append(REASON_NOT_MARGINABLE)
        row.target_notional = target_notional
```

and replace the last two lines of `compute_allocation`:

```python
    _finalise_totals(plan)
    return plan
```

with:

```python
    plan.scale_factor = _apply_bp_scaling(plan.rows, plan.available_buying_power,
                                          allow_fractional=allow_fractional, margin=margin)
    _finalise_totals(plan)
    return plan
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `27 passed`

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): pro-rata buying-power scaling for buys (sells never scale)"
```

---

### Task 21: Degenerate inputs — no price, empty label, negative target

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [ ] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_symbol_without_a_price_is_skipped_not_guessed():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0)])]
    current = {"AAA": _pos("AAA", None), "BBB": _pos("BBB", 50.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].skipped is True
    assert by["AAA"].delta_quantity == 0.0
    assert pa.REASON_NO_PRICE in by["AAA"].reasons
    assert plan.unallocatable_pct == pytest.approx(60.0)
    assert by["BBB"].target_quantity == 80.0


def test_symbol_with_a_zero_price_is_skipped():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 0.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].skipped is True
    assert pa.REASON_NO_PRICE in plan.rows[0].reasons


def test_empty_managed_label_contributes_to_unallocatable_pct():
    labels = [LabelTarget("FULL", 70.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 30.0, [])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert [r.symbol for r in plan.rows] == ["AAA"]
    assert plan.rows[0].target_quantity == 70.0
    assert plan.unallocatable_pct == pytest.approx(30.0)
    assert "label 'EMPTY' has no symbols - 30.00% unallocated" in plan.warnings


def test_negative_label_target_is_clamped_to_zero():
    labels = [LabelTarget("A", -20.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    plan = pa.compute_allocation(10_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_notional == 0.0
    assert pa.REASON_NEGATIVE_CLAMPED in row.reasons
    assert row.delta_quantity == -10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "without_a_price or zero_price or empty_managed_label or negative_label_target"`

Expected: FAIL — `4 failed`:
```
E   TypeError: unsupported operand type(s) for *: 'float' and 'NoneType'
E   AssertionError: assert False is True
E   assert 0.0 == 30.0 ± 3.0e-05
E   AssertionError: assert -2000.0 == 0.0
```

- [ ] **Step 3: Write minimal implementation**

Inside `compute_allocation`, replace the target-aggregation block:

```python
    targets = {}
    sym_labels = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        for st in lt.symbols or []:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + plan.base_notional * share / 100.0
            sym_labels.setdefault(st.symbol, []).append(lt.label)
```

with:

```python
    targets = {}
    target_pcts = {}
    sym_labels = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        if not lt.symbols:
            # An empty label cannot absorb its percentage: it becomes cash left over.
            plan.unallocatable_pct += max(0.0, pct)
            plan.warnings.append(WARNING_EMPTY_LABEL_FMT.format(label=lt.label, pct=pct))
            continue
        for st in lt.symbols:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + plan.base_notional * share / 100.0
            target_pcts[st.symbol] = target_pcts.get(st.symbol, 0.0) + share
            sym_labels.setdefault(st.symbol, []).append(lt.label)
```

Then, still inside `compute_allocation`, replace:

```python
        row.target_notional = target_notional
        frac = bool(allow_fractional and m is not None and m.fractionable)
```

with:

```python
        if target_notional < 0:
            target_notional = 0.0
            row.reasons.append(REASON_NEGATIVE_CLAMPED)
        row.target_notional = target_notional
        if row.price is None or row.price <= 0:
            # No fallback price for live data -- skip the symbol and report it.
            row.skipped = True
            row.reasons.append(REASON_NO_PRICE)
            plan.unallocatable_pct += max(0.0, target_pcts.get(symbol, 0.0))
            plan.rows.append(row)
            continue
        frac = bool(allow_fractional and m is not None and m.fractionable)
```

Finally, replace the `compute_allocation` docstring with its full behavioural contract:

```python
    """Solve a full REBALANCE: every managed label, buys and sells.

    Args:
        base_notional: the allocatable base from ``compute_base_notional``.
        available_buying_power: broker buying power, the FEASIBILITY constraint
            (targets are notional, not buying power -- decision 2).
        labels: managed labels with ``target_pct`` (1-100) and symbol weights.
        current: ``{symbol: PositionState}``; a symbol absent here is treated as
            flat. The CALLER must have refused to build this dict when
            ``get_positions()`` returned ``None`` (fetch failure, not flat).
        margin: ``{symbol: MarginInfo}``; a symbol MISSING here falls back to
            ``default_bp_factor``.
        allow_fractional: opt-in per run (decision 12).
        default_bp_factor: conservative fallback == the account margin multiplier
            (assume no leverage); under-deploys rather than over-commits.

    Behaviour on degenerate input (never raises, always records a reason):
        * a label with no symbols -> allocates nothing, ``target_pct`` added to
          ``plan.unallocatable_pct`` and a ``WARNING_EMPTY_LABEL_FMT`` warning;
        * a symbol whose ``PositionState.price`` is ``None`` or <= 0 -> skipped
          with ``REASON_NO_PRICE`` (no guessed price -- no-fallback rule);
        * a negative computed target -> clamped to 0 with ``REASON_NEGATIVE_CLAMPED``;
        * a symbol in several managed labels -> targets SUM, one row, and
          ``REASON_MULTI_LABEL_FMT`` (no enforcement -- decision 7);
        * ``sum(bp_cost of buys) > available_buying_power`` -> every buy scaled
          pro-rata, ``plan.scale_factor`` set and ``REASON_SCALED_FMT`` added.

    Label percentages are NOT renormalised: a set totalling 90% deploys 90% of
    the base and leaves the rest as cash. Blocking submission is
    ``validate_label_targets``' job, not this function's.

    No minimum order threshold: every non-zero delta becomes a row (decision 11).
    Short positions are out of scope -- targets are long-only.

    Returns:
        AllocationPlan: one AllocationRow per managed symbol (including zero-delta
        and skipped rows, so the UI can show them) plus plan-level totals.
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `31 passed`

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): degenerate inputs - no price, empty label, negative target"
```

---

### Task 22: Weight defaults, target validation and the allocatable base

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [ ] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_even_split_of_three_totals_exactly_one_hundred():
    assert pa.even_split_pct(3) == [33.33, 33.33, 33.34]
    assert sum(pa.even_split_pct(3)) == 100.0


def test_even_split_of_seven_still_totals_exactly_one_hundred():
    parts = pa.even_split_pct(7)
    assert len(parts) == 7
    assert parts[0] == pytest.approx(14.28)
    assert sum(parts) == pytest.approx(100.0)


def test_even_split_of_zero_symbols_is_empty():
    assert pa.even_split_pct(0) == []
    assert pa.even_split_pct(-4) == []


def test_build_symbol_targets_defaults_to_even_when_nothing_stored():
    out = pa.build_symbol_targets(["A", "B", "C", "D"])
    assert [t.weight_pct for t in out] == [25.0, 25.0, 25.0, 25.0]
    assert [t.symbol for t in out] == ["A", "B", "C", "D"]


def test_build_symbol_targets_shares_the_remainder_among_unstored_symbols():
    out = pa.build_symbol_targets(["A", "B", "C"], {"A": 50.0})
    assert {t.symbol: t.weight_pct for t in out} == {"A": 50.0, "B": 25.0, "C": 25.0}


def test_validate_label_targets_accepts_a_valid_hundred_percent_set():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 40.0, [SymbolTarget("BBB", 100.0)])]
    assert pa.validate_label_targets(labels) == []


def test_validate_label_targets_rejects_a_total_below_one_hundred():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)])]
    errors = pa.validate_label_targets(labels)
    assert errors == [pa.ERROR_LABEL_TOTAL_FMT.format(total=60.0)]
    assert errors == ["label targets total 60.00% - must total 100%"]


def test_validate_label_targets_accepts_a_total_inside_the_tolerance():
    """LABEL_TOTAL_TOLERANCE_PCT is 0.01 PERCENTAGE POINTS: 33.33+33.33+33.34 passes."""
    labels = [LabelTarget("A", 33.33, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 33.33, [SymbolTarget("BBB", 100.0)]),
              LabelTarget("C", 33.34, [SymbolTarget("CCC", 100.0)])]
    assert pa.validate_label_targets(labels) == []


def test_validate_label_targets_rejects_a_non_zero_label_with_no_symbols():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 40.0, [])]
    errors = pa.validate_label_targets(labels)
    assert pa.ERROR_LABEL_NO_SYMBOLS_FMT.format(label="B", pct=40.0) in errors
    assert any("B" in e and "no symbols" in e for e in errors)


def test_validate_label_targets_rejects_duplicates():
    labels = [LabelTarget("A", 50.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("A", 50.0, [SymbolTarget("BBB", 100.0)])]
    assert pa.ERROR_LABEL_DUPLICATE_FMT.format(label="A") in pa.validate_label_targets(labels)


def test_validate_label_targets_rejects_a_negative_target():
    labels = [LabelTarget("A", 120.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", -20.0, [SymbolTarget("BBB", 100.0)])]
    errors = pa.validate_label_targets(labels)
    assert any("negative" in e for e in errors)


def test_compute_base_notional_adds_managed_cost_basis_to_buying_power():
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=900.0),
               "ZZZ": _pos("ZZZ", 100.0, quantity=99.0, cost_basis=9_900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA"]) == 5_900.0


def test_compute_base_notional_managed_symbol_with_no_position_contributes_zero():
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=1500.0)}
    assert pa.compute_base_notional(10_000.0, current, ["AAA", "NVDA"]) == 11_500.0


def test_compute_base_notional_counts_a_repeated_symbol_once():
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA", "AAA"]) == 5_900.0


def test_compute_base_notional_raises_when_buying_power_is_none():
    with pytest.raises(ValueError):
        pa.compute_base_notional(None, {}, ["AAA"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "even_split_of or build_symbol_targets or validate_label_targets or compute_base_notional"`

Expected: FAIL — `14 failed`, all of the form
```
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'even_split_pct'
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'build_symbol_targets'
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'validate_label_targets'
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'compute_base_notional'
```

- [ ] **Step 3: Write minimal implementation**

Insert these four functions immediately ABOVE `def round_quantity(` in
`packages/common/ba2_common/core/portfolio_allocation.py`:

```python
def even_split_pct(count: int) -> List[float]:
    """Split 100% evenly across ``count`` slots, exact to 2dp.

    The remainder lands on the LAST slot so the list always totals exactly 100.0
    (``even_split_pct(3) == [33.33, 33.33, 33.34]``). Returns ``[]`` for
    ``count <= 0`` -- an empty label gets nothing, not a ZeroDivisionError.
    """
    if count <= 0:
        return []
    each = math.floor(100.0 / count * 100.0) / 100.0
    out = [each] * count
    out[-1] = round(100.0 - each * (count - 1), 2)
    return out


def build_symbol_targets(symbols: List[str],
                         stored_weights: Optional[Dict[str, float]] = None) -> List[SymbolTarget]:
    """Resolve a label's symbol weights, filling in the even-split default.

    ``stored_weights`` is ``{symbol: weight_pct}`` from
    ``portfolio_allocation_symbol`` -- absent symbols are NOT an error, they take
    the even-split default (rows are created lazily by design).

    If ANY weight is stored for the label, the un-stored symbols share what is
    left of 100% evenly; if none are, every symbol gets ``even_split_pct``.
    Order of ``symbols`` is preserved.
    """
    syms = list(symbols or [])
    if not syms:
        return []
    stored = stored_weights or {}
    known = {s: float(stored[s]) for s in syms if s in stored}
    if not known:
        return [SymbolTarget(symbol=s, weight_pct=p)
                for s, p in zip(syms, even_split_pct(len(syms)))]
    unknown = [s for s in syms if s not in known]
    weights = dict(known)
    if unknown:
        remaining = max(0.0, 100.0 - sum(known.values()))
        for s, p in zip(unknown, even_split_pct(len(unknown))):
            weights[s] = round(remaining * p / 100.0, 4)
    return [SymbolTarget(symbol=s, weight_pct=weights[s]) for s in syms]


def validate_label_targets(labels: List[LabelTarget], *,
                           tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Validate a REBALANCE label set. Pure -- returns problems, never raises.

    Checks: targets total 100 +/- ``tolerance`` (0.01 PERCENTAGE POINTS by
    default, so 99.995 passes and 99.98 does not); no negative ``target_pct``; no
    duplicate label names; every non-zero label has at least one symbol.

    Returns:
        List[str]: human-readable error strings built from the ``ERROR_LABEL_*``
        formats; EMPTY means valid. Submit must be blocked while this is
        non-empty (decision 3).
    """
    errors = []
    total = sum(float(lt.target_pct or 0.0) for lt in labels or [])
    if abs(total - 100.0) > tolerance:
        errors.append(ERROR_LABEL_TOTAL_FMT.format(total=total))
    seen = set()
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        if lt.label in seen:
            errors.append(ERROR_LABEL_DUPLICATE_FMT.format(label=lt.label))
        seen.add(lt.label)
        if pct < 0:
            errors.append(ERROR_LABEL_NEGATIVE_FMT.format(label=lt.label, pct=pct))
        if pct > 0 and not lt.symbols:
            errors.append(ERROR_LABEL_NO_SYMBOLS_FMT.format(label=lt.label, pct=pct))
    return errors


def compute_base_notional(available_buying_power: float,
                          current: Dict[str, PositionState],
                          managed_symbols: List[str]) -> float:
    """Allocatable base = broker buying power + cost basis of MANAGED positions.

    Decision 1 of the design. Unmanaged positions are deliberately excluded: they
    are invisible to the page and already reduce ``available_buying_power``
    naturally. Symbols in ``managed_symbols`` with no ``current`` entry
    contribute 0; a repeated symbol is counted once.

    Task 25 adds a ``valuation_mode`` keyword so ``market`` mode can measure the
    same positions at ``qty x price`` instead.

    Raises:
        ValueError: if ``available_buying_power`` is None (no fallback for
        balances -- the caller must not have got here without a real number).
    """
    if available_buying_power is None:
        raise ValueError("compute_base_notional: available_buying_power is None")
    base = float(available_buying_power)
    for sym in dict.fromkeys(managed_symbols or []):
        ps = (current or {}).get(sym)
        if ps is not None:
            base += float(ps.cost_basis or 0.0)
    return base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `45 passed`

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): even-split weights, label validation and allocatable base"
```

---

### Task 23: INVEST_LABEL — put an amount into one label

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [ ] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_label_investment_splits_the_amount_and_only_buys():
    label = LabelTarget("ARK26", 40.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0)])
    current = {"AAA": _pos("AAA", 100.0, quantity=7.0, cost_basis=700.0),
               "BBB": _pos("BBB", 50.0, quantity=1000.0, cost_basis=50_000.0)}
    plan = pa.compute_label_investment(label, 10_000.0, current, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].delta_quantity == 60.0
    assert by["AAA"].target_quantity == 67.0
    assert by["BBB"].delta_quantity == 80.0
    assert plan.total_sell_value == 0.0
    assert plan.net_buy_value == plan.total_buy_value == 10_000.0


def test_label_investment_scales_down_to_available_buying_power():
    label = LabelTarget("ARK26", 100.0, [SymbolTarget("AAA", 100.0)])
    plan = pa.compute_label_investment(label, 10_000.0, {"AAA": _pos("AAA", 100.0)}, {},
                                       available_buying_power=2_500.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert plan.scale_factor == pytest.approx(0.25)
    assert plan.rows[0].delta_quantity == 25.0


def test_label_investment_on_an_empty_label_allocates_nothing():
    plan = pa.compute_label_investment(LabelTarget("EMPTY", 100.0, []), 10_000.0, {}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows == []
    assert plan.unallocatable_pct == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "label_investment"`

Expected: FAIL — `3 failed`
```
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'compute_label_investment'
```

- [ ] **Step 3: Write minimal implementation**

Append to the end of `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
def compute_label_investment(label: LabelTarget, amount: float,
                             current: Dict[str, PositionState],
                             margin: Dict[str, MarginInfo], *,
                             available_buying_power: float, allow_fractional: bool,
                             default_bp_factor: float) -> AllocationPlan:
    """Solve an INVEST_LABEL run: put ``amount`` into ONE label. Buys only.

    ``amount`` is split by the label's symbol weights. ``label.target_pct`` is
    IGNORED -- the amount is the whole budget, and it is ADDED to whatever the
    account already holds rather than rebalanced towards. No sells are ever
    produced, so ``plan.total_sell_value`` is always 0.0 and
    ``plan.net_buy_value == plan.total_buy_value``. Buying-power scaling,
    rounding, missing prices and missing margin info behave exactly as in
    ``compute_allocation``.
    """
    current = current or {}
    margin = margin or {}
    budget = max(0.0, float(amount or 0.0))
    plan = AllocationPlan(base_notional=budget,
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
    if not label.symbols:
        plan.unallocatable_pct = 100.0
        plan.warnings.append(WARNING_EMPTY_LABEL_FMT.format(label=label.label, pct=100.0))
        return plan
    for st in label.symbols:
        weight = float(st.weight_pct or 0.0)
        target_notional = budget * weight / 100.0
        ps = current.get(st.symbol)
        m = margin.get(st.symbol)
        row = AllocationRow(symbol=st.symbol, labels=[label.label])
        row.bp_factor = float(m.bp_factor) if m is not None else float(default_bp_factor)
        row.current_quantity = float(ps.quantity) if ps is not None else 0.0
        row.current_cost_basis = float(ps.cost_basis) if ps is not None else 0.0
        row.price = ps.price if ps is not None else None
        if m is not None and not m.marginable:
            row.reasons.append(REASON_NOT_MARGINABLE)
        if target_notional < 0:
            target_notional = 0.0
            row.reasons.append(REASON_NEGATIVE_CLAMPED)
        row.target_notional = target_notional
        if row.price is None or row.price <= 0:
            row.skipped = True
            row.reasons.append(REASON_NO_PRICE)
            plan.unallocatable_pct += max(0.0, weight)
            plan.rows.append(row)
            continue
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        qty = round_quantity(target_notional, row.price, m, allow_fractional=allow_fractional)
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)
        row.delta_quantity = qty
        row.target_quantity = row.current_quantity + qty
        if qty > 0:
            row.side = OrderDirection.BUY
        row.estimated_value = qty * row.price
        row.bp_cost = row.estimated_value * row.bp_factor
        plan.rows.append(row)
    plan.scale_factor = _apply_bp_scaling(plan.rows, plan.available_buying_power,
                                          allow_fractional=allow_fractional, margin=margin)
    _finalise_totals(plan)
    return plan
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `48 passed`

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): INVEST_LABEL solve - buys only, split by symbol weights"
```

---

### Task 24: Broker precheck re-solve and FIFO income consumption

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [ ] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_apply_order_impacts_replaces_the_estimated_bp_cost():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].bp_cost == 10_000.0
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-25_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=1_000_000.0)
    assert out.rows[0].bp_cost == 25_000.0
    assert "broker precheck disagreed on XXX - re-solved" in out.warnings
    assert plan.rows[0].bp_cost == 10_000.0


def test_apply_order_impacts_rescales_when_the_precheck_no_longer_fits():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 10_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-20_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=10_000.0)
    assert out.scale_factor == pytest.approx(0.5)
    assert out.rows[0].delta_quantity == 50.0
    assert out.rows[0].bp_cost == pytest.approx(10_000.0)


def test_apply_order_impacts_skips_a_rejected_order():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=0.0,
                                  accepted=False, errors=["symbol not tradeable"])}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=1_000_000.0)
    assert out.rows[0].skipped is True
    assert "symbol not tradeable" in out.rows[0].reasons
    assert out.total_buy_value == 0.0


def test_consume_income_events_takes_oldest_first_and_partially_consumes_the_last():
    out = pa.consume_income_events([(1, 100.0), (2, 250.0), (3, 500.0)], 300.0)
    assert out == [(1, 100.0), (2, 200.0)]


def test_consume_income_events_returns_nothing_for_a_sell_funded_run():
    assert pa.consume_income_events([(1, 100.0)], 0.0) == []
    assert pa.consume_income_events([(1, 100.0)], -50.0) == []


def test_consume_income_events_with_an_empty_ledger_returns_empty():
    assert pa.consume_income_events([], 500.0) == []


def test_consume_income_events_never_takes_more_than_the_ledger_holds():
    out = pa.consume_income_events([(1, 100.0), (2, 50.0)], 1_000.0)
    assert sum(a for _, a in out) == pytest.approx(150.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "apply_order_impacts or consume_income"`

Expected: FAIL — `7 failed`
```
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'apply_order_impacts'
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'consume_income_events'
```

- [ ] **Step 3: Write minimal implementation**

`copy`, `math` and `Tuple` are already imported at the top of the module (Task 16). Append to
the end of `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
def apply_order_impacts(plan: AllocationPlan, impacts: Dict[str, OrderImpact], *,
                        available_buying_power: float) -> AllocationPlan:
    """Re-solve a plan against broker PRECHECK results (precheck over estimation).

    For each row with a matching ``OrderImpact``, replaces the estimated
    ``bp_cost`` with ``impact.bp_cost`` (the positive, sign-corrected value) and
    re-runs the pro-rata buying-power scaling. A symbol with no impact keeps its
    estimated cost. ``impact.accepted is False`` marks the row ``skipped`` and
    copies ``impact.errors`` into ``row.reasons``.

    Returns a NEW AllocationPlan; ``plan`` is not mutated. Adds
    ``WARNING_PRECHECK_DISAGREED_FMT`` for each row whose cost changed.
    """
    out = AllocationPlan(
        rows=[copy.deepcopy(r) for r in plan.rows],
        base_notional=plan.base_notional,
        available_buying_power=float(available_buying_power or 0.0),
        unallocatable_pct=plan.unallocatable_pct,
        allow_fractional=plan.allow_fractional,
        warnings=list(plan.warnings),
    )
    for row in out.rows:
        impact = (impacts or {}).get(row.symbol)
        if impact is None:
            continue
        if not impact.accepted:
            row.skipped = True
            row.bp_cost = 0.0
            row.reasons.extend(impact.errors)
            continue
        if row.is_buy and abs(impact.bp_cost - row.bp_cost) > 0.005:
            out.warnings.append(WARNING_PRECHECK_DISAGREED_FMT.format(symbol=row.symbol))
            row.bp_cost = impact.bp_cost
    out.scale_factor = _apply_bp_scaling(out.rows, out.available_buying_power,
                                         allow_fractional=out.allow_fractional)
    _finalise_totals(out)
    return out


def consume_income_events(events: List[Tuple[int, float]],
                          net_buy_value: float) -> List[Tuple[int, float]]:
    """FIFO-consume the income ledger against a run's NET buy value. Pure.

    Args:
        events: ``[(income_event_id, open_amount)]``, ALREADY sorted oldest-first
            by ``event_date`` then ``id``. Pass plain tuples, not ORM rows, so
            this stays IO-free and unit-testable.
        net_buy_value: ``max(0, submitted_buy_value - submitted_sell_value)`` -- a
            rebalance funded entirely by its own sells consumes nothing.

    Returns:
        List[Tuple[int, float]]: ``[(income_event_id, amount_to_consume)]``, only
        for events actually touched. The last one may be PARTIAL; its remainder
        stays open. Empty when ``net_buy_value <= 0`` or the ledger is empty.
        The caller adds each amount to ``PortfolioIncomeEvent.consumed_amount``.
    """
    remaining = float(net_buy_value or 0.0)
    out = []
    if remaining <= 0:
        return out
    for event_id, open_amount in events or []:
        if remaining <= QUANTITY_EPSILON:
            break
        available = float(open_amount or 0.0)
        if available <= 0:
            continue
        take = min(available, remaining)
        out.append((event_id, take))
        remaining -= take
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `55 passed`

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): precheck re-solve and FIFO income-event consumption"
```

---

### Task 25: Valuation mode — `cost` vs `market`

> **Also make the skipped-row `side` consistent.** Task 21 leaves a no-price skip with `side=None`
> (it `continue`s before `side` is assigned), while Task 20's scaling branch leaves a scaled-away
> buy with `side="BUY"` even though `skipped=True`. Both are correctly excluded by `is_buy`/`is_sell`,
> but a consumer reading the raw `side` field — and Section G's dry-run table will — sees two
> different shapes for "no order". Clear `side = None` in the scaling branch when `qty <= 0`, and
> add a test asserting both kinds of skipped row serialise `side: null`.
>
> **And re-key the close branch, which is the live bug this task exists to fix.** It currently keys
> on `target_quantity <= 0`, so a `min_order_size` that zeroes a positive target liquidates the whole
> position — "hold ~3.33 shares" becomes a full exit. Re-key to `target_notional <= 0`. Task 21's
> negative clamp writes `target_notional = 0.0` as well as the quantity, so a genuinely negative
> target still liquidates correctly; add a test pinning both directions.
>
> **Two defects Task 24 found in `apply_order_impacts` — fold them in here rather than making a
> third edit to that function later.**
>
> 1. **The margin dict is dropped on the precheck re-solve.** It calls `_apply_bp_scaling` with no
>    `margin=`, so a `fractional=True` row is rebuilt as `MarginInfo(symbol, bp_factor,
>    fractionable=True)` with `min_order_size=None` and `min_trade_increment=None`. The re-solve then
>    rounds on the default 4-dp grid and skips the min-order-size filter, so it can emit a quantity
>    off the broker's increment or below its minimum. This is a broker-side rejection, not an
>    overspend — the money invariant holds — and it needs a broker with BOTH order preview AND
>    published fractional metadata, which is plausibly the empty set today (TastyTrade has the
>    preview, Alpaca the metadata). Latent, but cheap: add
>    `margin: Optional[Dict[str, MarginInfo]] = None` to `apply_order_impacts` and forward it.
> 2. **`out.scale_factor` overwrites rather than compounds.** If the first solve scaled to 0.6 and
>    the precheck forces another 0.5, the returned plan reports `0.5` when the true cumulative factor
>    against the original target is `0.3`, and affected rows accumulate a second `REASON_SCALED_FMT`.
>    Multiply into the incoming factor and de-duplicate the reason.
>
> Deliberate and NOT to be changed: a favourable precheck lowers `bp_cost` but never re-deploys the
> freed buying power, because `_apply_bp_scaling` only scales down. Never overspending beats fully
> deploying. On record so it is a decision rather than an accident.


Spec decision 5a. "How much of my portfolio is in this symbol" has two defensible answers, and
the mode selects the meaning of *current value* in three places at once — the allocatable base,
the displayed percentages, and every delta. They must never disagree.

**A wart to be explicit about — SINCE CLOSED, see amendment 5 below.** The task as written gave
the three entry points DIFFERENT parameter defaults, because each default was that function's
already-pinned behaviour and 55 passing tests depended on it:

| Function | Python default (as written) | Why |
|---|---|---|
| `compute_base_notional` | `VALUATION_MODE_COST` | Its pinned behaviour is "buying power + **cost basis** of managed positions" |
| `compute_allocation` / `compute_label_investment` | `VALUATION_MODE_MARKET` | Their pinned behaviour is `target_quantity - current_quantity`, i.e. shares vs shares, which IS market valuation |

That is a trap, and it was removed before Sections F and G were written: **all three now take
`valuation_mode` as a REQUIRED keyword with no default.** Build the signatures that way (the
Step 3 blocks below already show them without defaults) and pass the mode at every call site,
including in the tests appended by the earlier Section C tasks.

The **DB and page default is `cost`** (spec 5a; `portfolio_allocation_config.valuation_mode`
defaults to `"cost"`), and the page ALWAYS passes the mode explicitly to all three.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py`
- Test: `packages/common/tests/test_portfolio_allocation.py`

- [ ] **Step 1: Write the failing test**

Append to the end of `packages/common/tests/test_portfolio_allocation.py`:

```python
def test_current_value_in_cost_mode_is_the_cost_basis():
    state = _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)
    assert pa.current_value(state, pa.VALUATION_MODE_COST) == 900.0


def test_current_value_in_market_mode_is_quantity_times_price():
    state = _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)
    assert pa.current_value(state, pa.VALUATION_MODE_MARKET) == 2_000.0


def test_current_value_of_a_flat_symbol_is_zero_in_both_modes():
    assert pa.current_value(None, pa.VALUATION_MODE_COST) == 0.0
    assert pa.current_value(None, pa.VALUATION_MODE_MARKET) == 0.0


def test_current_value_in_market_mode_without_a_price_is_zero_not_a_guess():
    """The caller skips a no-price symbol anyway; this must not invent a value."""
    assert pa.current_value(_pos("AAA", None, quantity=10.0, cost_basis=900.0),
                            pa.VALUATION_MODE_MARKET) == 0.0


def test_current_value_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        pa.current_value(_pos("AAA", 10.0), "marketish")


def test_base_notional_in_market_mode_uses_quantity_times_price():
    current = {"AAA": _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA"],
                                    valuation_mode=pa.VALUATION_MODE_MARKET) == 7_000.0
    assert pa.compute_base_notional(5_000.0, current, ["AAA"],
                                    valuation_mode=pa.VALUATION_MODE_COST) == 5_900.0


def test_cost_mode_sizes_the_top_up_off_the_purchase_value_not_the_share_count():
    """Held 20 shares bought at 90 (cost basis 1800) now worth 100 each.

    market: target 100 shares, hold 20 -> buy 80.
    cost:   target notional 10000, cost basis 1800 -> spend 8200 -> buy 82.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=20.0, cost_basis=1_800.0)}
    market = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                   allow_fractional=False, default_bp_factor=1.0,
                                   valuation_mode=pa.VALUATION_MODE_MARKET)
    cost = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert market.rows[0].delta_quantity == 80.0
    assert cost.rows[0].delta_quantity == 82.0
    assert cost.rows[0].target_quantity == 102.0


def test_market_mode_trims_a_doubled_position_that_cost_mode_leaves_alone():
    """Bought 50 at 100 (cost basis 5000), now 200 each. Target notional 5000."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 200.0, quantity=50.0, cost_basis=5_000.0)}
    market = pa.compute_allocation(5_000.0, 1_000_000.0, labels, current, {},
                                   allow_fractional=False, default_bp_factor=1.0,
                                   valuation_mode=pa.VALUATION_MODE_MARKET)
    cost = pa.compute_allocation(5_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert market.rows[0].delta_quantity == -25.0     # 25 shares is now 5000
    assert market.rows[0].side == OrderDirection.SELL
    assert cost.rows[0].delta_quantity == 0.0          # already at its purchase weight
    assert cost.rows[0].side is None


def test_cost_mode_never_oversells_more_than_is_held():
    """Cost basis 20000 on only 10 shares now worth 100: the 15000 trim is clamped."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=20_000.0)}
    plan = pa.compute_allocation(5_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert plan.rows[0].delta_quantity == -10.0
    assert plan.rows[0].target_quantity == 0.0


def test_cost_mode_still_closes_a_position_on_a_zero_target():
    labels = [LabelTarget("EXIT", 0.0, [SymbolTarget("BBB", 100.0)])]
    current = {"BBB": _pos("BBB", 20.0, quantity=30.0, cost_basis=500.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert plan.rows[0].delta_quantity == -30.0
    assert pa.REASON_CLOSE_TO_ZERO in plan.rows[0].reasons


def test_compute_allocation_rejects_an_unknown_valuation_mode():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    with pytest.raises(ValueError):
        pa.compute_allocation(10_000.0, 1_000.0, labels, {"XXX": _pos("XXX", 100.0)}, {},
                              allow_fractional=False, default_bp_factor=1.0,
                              valuation_mode="marketish")


def test_all_three_entry_points_require_an_explicit_valuation_mode():
    """CONTRACT CHANGED: there is no Python default on ANY of the three.

    They used to have defaults that DISAGREED -- ``compute_base_notional`` fell back
    to cost (its pinned behaviour, "buying power + cost basis") and the two solvers
    to market (theirs, "shares vs shares") -- and this test pinned exactly that.
    But the mode picks the meaning of "current value" for the allocatable base, the
    displayed percentages and every delta AT ONCE, so a call site that forgot the
    keyword got a cost base and market deltas: no exception, no warning, just wrong
    money. Since no single default is right for every caller, none of them has one
    and the omission is a TypeError at the call site instead.
    """
    current = {"AAA": _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    with pytest.raises(TypeError):
        pa.compute_base_notional(0.0, current, ["AAA"])
    with pytest.raises(TypeError):
        pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                              allow_fractional=False, default_bp_factor=1.0)
    with pytest.raises(TypeError):
        pa.compute_label_investment(labels[0], 1_000.0, current, {},
                                    available_buying_power=1_000_000.0,
                                    allow_fractional=False, default_bp_factor=1.0)
```

**Also, in the same step:** every call to the three entry points already in the test file from
Tasks 16-24 is BARE and will now raise `TypeError`. Add an explicit `valuation_mode=` to each —
`VALUATION_MODE_COST` for the three `compute_base_notional` asserts (their numbers are cost-basis
arithmetic: `5_000 + 900`, `10_000 + 1_500`) and `VALUATION_MODE_MARKET` for the
`compute_allocation` / `compute_label_investment` calls (which is what their assertions were
silently getting). One of them is genuinely mode-sensitive:
`test_held_below_target_produces_a_top_up_buy` asserts a delta of **80** and is the MARKET half
of the fixture `test_cost_mode_sizes_the_top_up_off_the_purchase_value_not_the_share_count` uses
for **82** — pass `VALUATION_MODE_MARKET` there or it breaks. The guard tests
(`refuses_a_none_base_notional` and friends) must pass a VALID mode: the mode check runs FIRST
and raises the same `ValueError`, so an omitted one would make them pass for the wrong reason.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v -k "current_value or valuation_mode or cost_mode or market_mode or explicit_valuation_mode or base_notional_in_market"`

Expected: FAIL — `12 failed`, including
```
E   AttributeError: module 'ba2_common.core.portfolio_allocation' has no attribute 'current_value'
E   TypeError: compute_base_notional() got an unexpected keyword argument 'valuation_mode'
E   TypeError: compute_allocation() got an unexpected keyword argument 'valuation_mode'
```

- [ ] **Step 3: Write minimal implementation**

**3a.** Insert these two functions immediately ABOVE `def even_split_pct(` in
`packages/common/ba2_common/core/portfolio_allocation.py`:

```python
def current_value(state: Optional[PositionState], valuation_mode: str) -> float:
    """A position's CURRENT VALUE under the selected valuation mode (decision 5a).

    ``cost``   -> ``cost_basis`` (what you paid).
    ``market`` -> ``quantity * price`` (what it is worth now).

    A symbol with no position is 0.0 in both modes. In ``market`` mode a symbol
    with no price is 0.0 too -- the caller has already skipped it with
    ``REASON_NO_PRICE``, and inventing a value here would be exactly the
    guessed-price the platform forbids.

    Raises:
        ValueError: on any other mode string. A typo would silently reinterpret
        every percentage on the page.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    if state is None:
        return 0.0
    if valuation_mode == VALUATION_MODE_MARKET:
        if state.price is None or state.price <= 0:
            return 0.0
        return float(state.quantity or 0.0) * float(state.price)
    return float(state.cost_basis or 0.0)


def round_delta_quantity(delta_notional: float, price: float,
                         margin: Optional[MarginInfo], *, allow_fractional: bool,
                         current_quantity: float) -> float:
    """Turn a SIGNED notional delta into a SIGNED, tradeable share delta.

    Used by ``cost`` valuation mode, where the target is expressed against the
    purchase value rather than the share count. The magnitude is rounded DOWN by
    ``round_quantity`` (so increments and min order sizes still hold) and a sell
    is CLAMPED to ``current_quantity`` -- long-only, never oversell.
    """
    magnitude = round_quantity(abs(float(delta_notional or 0.0)), price, margin,
                               allow_fractional=allow_fractional)
    if delta_notional >= 0:
        return magnitude
    return -min(magnitude, float(current_quantity or 0.0))
```

**3b.** Replace `compute_base_notional`'s signature and body with the mode-aware version:

```python
def compute_base_notional(available_buying_power: float,
                          current: Dict[str, PositionState],
                          managed_symbols: List[str],
                          *, valuation_mode: str) -> float:
    """Allocatable base = broker buying power + current value of MANAGED positions.

    Decision 1 of the design; ``valuation_mode`` (decision 5a) selects whether
    "current value" is the cost basis or ``qty x price``. Unmanaged positions are
    deliberately excluded: they are invisible to the page and already reduce
    ``available_buying_power`` naturally. Symbols in ``managed_symbols`` with no
    ``current`` entry contribute 0; a repeated symbol is counted once.

    ``valuation_mode`` is REQUIRED and has NO Python default -- see the note on
    ``compute_allocation``. Pass the account's configured mode.

    Raises:
        ValueError: if ``available_buying_power`` is None (no fallback for
        balances), or if ``valuation_mode`` is unknown.
        TypeError: if ``valuation_mode`` is omitted.
    """
    if available_buying_power is None:
        raise ValueError("compute_base_notional: available_buying_power is None")
    base = float(available_buying_power)
    for sym in dict.fromkeys(managed_symbols or []):
        base += current_value((current or {}).get(sym), valuation_mode)
    return base
```

**3c.** In `compute_allocation`, add the parameter and the cost-mode branch. Change the
signature line from:

```python
def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float) -> AllocationPlan:
```

to:

```python
def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float,
                       valuation_mode: str) -> AllocationPlan:
```

Add this paragraph to its docstring, immediately after the `default_bp_factor:` argument line:

```
        valuation_mode: ``cost`` or ``market`` (decision 5a), REQUIRED. ``market``
            targets a SHARE COUNT (``target_notional / price``) and deltas against
            the held quantity; ``cost`` targets a PURCHASE VALUE and deltas against
            ``cost_basis``. Pass the same mode used to build ``base_notional``.
```

Add this guard as the FIRST statement of the function body, above `current = current or {}`:

```python
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
```

Then replace this block:

```python
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        row.target_quantity = round_quantity(target_notional, row.price, m,
                                             allow_fractional=allow_fractional)
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)
        delta = row.target_quantity - row.current_quantity
        if row.target_quantity <= 0 and row.current_quantity > 0:
            delta = -row.current_quantity
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        elif not frac:
            delta = float(math.floor(delta) if delta > 0 else -math.floor(-delta))
```

with:

```python
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)

        if target_notional <= 0 and row.current_quantity > 0:
            # Same in both modes: a zero target flattens the position outright.
            row.target_quantity = 0.0
            delta = -row.current_quantity
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        elif valuation_mode == VALUATION_MODE_COST:
            # Target a PURCHASE VALUE: spend the gap between it and the cost basis.
            delta = round_delta_quantity(
                target_notional - current_value(ps, VALUATION_MODE_COST),
                row.price, m, allow_fractional=allow_fractional,
                current_quantity=row.current_quantity)
            row.target_quantity = row.current_quantity + delta
        else:
            # Target a SHARE COUNT: target_notional / price, delta vs what is held.
            row.target_quantity = round_quantity(target_notional, row.price, m,
                                                 allow_fractional=allow_fractional)
            delta = row.target_quantity - row.current_quantity
            if not frac:
                delta = float(math.floor(delta) if delta > 0 else -math.floor(-delta))
```

**3d.** In `compute_label_investment`, add the same keyword for signature symmetry — an
INVEST_LABEL run always ADDS a budget, so the mode does not change its arithmetic, but the page
passes one mode to every engine call and must not have to special-case this one. Change the
signature from:

```python
def compute_label_investment(label: LabelTarget, amount: float,
                             current: Dict[str, PositionState],
                             margin: Dict[str, MarginInfo], *,
                             available_buying_power: float, allow_fractional: bool,
                             default_bp_factor: float) -> AllocationPlan:
```

to:

```python
def compute_label_investment(label: LabelTarget, amount: float,
                             current: Dict[str, PositionState],
                             margin: Dict[str, MarginInfo], *,
                             available_buying_power: float, allow_fractional: bool,
                             default_bp_factor: float,
                             valuation_mode: str) -> AllocationPlan:
```

and add this line to its docstring, at the end:

```
    ``valuation_mode`` is accepted for call-site symmetry and validated, but does
    not change the arithmetic: an INVEST_LABEL run ADDS a budget on top of the
    existing position rather than rebalancing towards a target value.
```

Add this guard as the FIRST statement of its body:

```python
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — `67 passed` (all 55 earlier tests stay green, which is the point of the
per-function defaults)

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation.py
git commit -m "feat(allocation): cost vs market valuation mode across base, targets and deltas"
```

**AS-LANDED AMENDMENT (Task 25).** The task shipped in three commits and changed
several CONTRACTS beyond the text above, including rewriting pinned tests — recorded
here so each reads as a deliberate change rather than a weakened test. Sections D-G
should treat this, not the code block above, as the engine's behaviour. Final count:
`127 passed` (not the stale `67`).

1. **`min_order_size` is an ORDER constraint, not a TARGET constraint.** It used to be
   applied inside `round_quantity` to the target holding, so a minimum the target fell
   under rewrote the target to 0 shares — and in `market` mode `delta = target -
   current` then LIQUIDATED the position. Re-keying the close branch to
   `target_notional` (the change specified above) removed the misleading
   `REASON_CLOSE_TO_ZERO` label but not the sell. Now: `round_quantity` and
   `round_delta_quantity` take `apply_min_order_size: bool = True`; `compute_allocation`
   passes `False` in BOTH valuation branches and applies one check to the signed delta
   via `_suppress_below_min_order`, which zeroes an unsendable trade, LEAVES THE
   POSITION WHERE IT IS and appends the new `REASON_BELOW_MIN_ORDER_FMT`
   (`"below broker min order size {size:g} - no order"`). It tests the MAGNITUDE,
   so an unsendable trim is suppressed exactly like an unsendable top-up.
   `compute_label_investment` does the same. `_apply_bp_scaling` keeps the default
   `True`: the value it rounds IS an order.
   *Test rewritten:* `test_quantity_below_min_order_size_is_dropped_to_zero` →
   `test_an_order_below_min_order_size_is_suppressed_and_the_target_kept`. Its
   `target_quantity == 0.0` assertion still holds numerically on a FLAT position but
   now means "held nothing, ordered nothing" instead of "target rewritten to nothing";
   it additionally asserts `target_notional` is untouched and the reason is present.

2. **`target_quantity` means the POST-TRADE holding, in both modes.** It was the ideal
   share count in `market` mode (20.0 for a 2000 target at 100 while holding 10.5, even
   though only 9 whole shares can be bought) and `current + delta` in `cost` mode
   (19.5). Now always `current_quantity + delta_quantity` — what the account owns if the
   row executes — which is what `_apply_bp_scaling` and `compute_label_investment`
   already wrote, what the dry-run column needs to be truthful, and the only one of the
   two comparable across rows. No existing test asserted the market-mode ideal, so
   nothing else needed rewriting; a sweep of 240 input combinations confirms the
   identity holds with no oversell.

A residual for the LIVE layer, not the engine: when a full close is itself below
`min_order_size`, the row now carries both `REASON_CLOSE_TO_ZERO` and
`REASON_BELOW_MIN_ORDER_FMT` and trades nothing. Most brokers exempt a full
close-position from the minimum, so the submit path may be able to send it anyway;
that is a live-layer decision and the engine deliberately does not assume it.

**THIRD COMMIT — fresh-eyes review, two Critical fixes.** A full read of the engine
against the spec (the first, since it was built in ten slices) found two ways to
liquidate a portfolio by accident. Both are fixed; spec conformance was otherwise
clean across every decision.

3. **`cost` mode sized a SELL off the market price, not the average cost.**
   `cost_basis` is `quantity x avg_entry_price`, so a basis gap converts to shares at
   the AVERAGE COST; only the BUY leg converts at the price. Dividing by the price
   made every trim wrong by `price / avg_cost`: with the price HALVED it asked for
   twice the shares and the hold-clamp turned a 50% trim into a **full liquidation**,
   with no reason string to distinguish it from an ordinary trim; with the price
   doubled it under-trimmed and never converged. `compute_allocation`'s cost branch
   now picks the divisor per leg and `round_delta_quantity`'s second parameter is
   renamed `price` → `unit_value` to stop the same mistake recurring. With the right
   divisor a basis gap can no longer exceed the basis, so the hold-clamp became
   mathematically unreachable from this path — it is kept as defence in depth and
   tested directly. *Test rewritten:* `test_cost_mode_never_oversells_more_than_is_held`
   pinned the DEFECT (asserting a full liquidation where a 7.5-share trim is right);
   it is now `test_cost_mode_trims_towards_the_target_basis_rounding_down`.
4. **A `None` or negative `base_notional` silently liquidated everything.**
   `float(base_notional or 0.0)` made a missing base a base of zero — i.e. a target of
   zero for every managed symbol. `compute_allocation` now raises `ValueError` on
   `None` and on negative, matching `compute_base_notional`; also on a `None`
   `available_buying_power`, and `compute_label_investment` on a `None` `amount` or
   buying power. Zero remains legal (a real, flat account).

Also in that commit: a precheck REJECTION is zeroed like every other no-order row
(it kept `side=BUY` and its quantity, so it rendered as a live BUY); market mode
re-rounds the DELTA onto the broker's increment (an on-grid target minus an off-grid
holding is off-grid); `AllocationPlan` gained `valuation_mode`, recorded in
`to_dict()` and propagated through `apply_order_impacts`, so `plan_json` can be read
back correctly; a scaled-away buy that was actually stopped by `min_order_size` now
says which rule stopped it; `REASON_MULTI_LABEL_FMT` reworded `"⚠ also in {labels}"` →
`"⚠ in {labels}"` (it renders inside one of the labels it names); `estimated_fees` and
the precheck's `warnings` now reach the row; the empty-label warning is suppressed at
0%; `MONEY_EPSILON` split from `QUANTITY_EPSILON`; and `__all__` added.
*Test also rewritten:* `test_compute_label_investment_arithmetic_is_identical_in_both_modes`
compared whole `to_dict()`s, which now legitimately differ by `valuation_mode`; it
compares the rows and the money totals and asserts the modes ARE recorded differently.

Five test gaps closed, three of which had surviving mutants: the
`compute_base_notional` mode guard (only observable with an empty managed list),
`sell_rows` ordering, decision 2 (margin changes a target's COST, not its SIZE),
never-short in both modes and against a pre-existing short, and `market_value` being
display-only. All five mutations are now killed.

**FOURTH COMMIT — `valuation_mode` is now REQUIRED on all three entry points.**
Closed before Sections F and G were written, i.e. before there was a single caller
to break.

5. **The three Python defaults deliberately disagreed, and that was the bug.**
   `compute_base_notional` defaulted to `cost` and `compute_allocation` /
   `compute_label_investment` to `market`, each matching its own historically-pinned
   behaviour (the "wart" table at the top of this task). But the mode selects the
   meaning of "current value" in THREE places at once — the allocatable base, the
   displayed percentages and every delta — and they must never disagree. One call
   site that forgot the keyword would get a **cost base with market deltas**: no
   exception, no warning, just wrong money on a page that submits real orders.
   All three now take `valuation_mode` as a **required keyword-only argument with
   no default**; omitting it is a `TypeError` at the call site. There is no default
   that is right for every caller, so there is no default at all.
   *Test rewritten:* `test_the_python_defaults_match_each_functions_pinned_behaviour`
   existed only to pin the disagreement; it is now
   `test_all_three_entry_points_require_an_explicit_valuation_mode`, which asserts
   the `TypeError` on all three. Count unchanged at `127 passed`.
   *Every other call site in the file gained an explicit mode:* `VALUATION_MODE_COST`
   for the three `compute_base_notional` asserts (their numbers are cost-basis
   arithmetic and would be wrong under market), `VALUATION_MODE_MARKET` everywhere
   else. Only ONE behavioural test was genuinely mode-sensitive —
   `test_held_below_target_produces_a_top_up_buy` asserts a delta of 80, the MARKET
   half of the same fixture `test_cost_mode_sizes_the_top_up_off_the_purchase_value_not_the_share_count`
   uses for 82 — every other holding in the file happens to sit at `avg_cost ==
   price` or hits the zero-target close branch, where the two modes agree. The
   `refuses_a_none_*` guards now pass a VALID mode on purpose: the mode check runs
   first and raises the same `ValueError`, so a bare call would have made them pass
   for the wrong reason.
   `portfolio_allocation_store.get_allocation_config`'s docstring, which described
   the disagreement as a live hazard, was updated to describe the fix.

**Still open, for whoever writes Sections F and G.** Two functions those sections
introduce carry a `valuation_mode` default of their own — `build_base_snapshot`
(Task 68) and `build_label_views` (Task 66), both `VALUATION_MODE_COST`. Each
default matches the DB default so neither can produce the cost-base/market-delta
split above, but both are the same SHAPE of omission-degrades-silently. Decide
deliberately rather than by inheritance. Separately, `apply_order_impacts`'
`margin=None` is a live instance of that shape: see the note on Task 69.

---

### Task 26: In-tree alias shim for the engine

**Files:**
- Create: `ba2_trade_platform/core/portfolio_allocation.py` (SHIM)
- Test: `tests/test_portfolio_allocation_shim.py`

Every new shared module needs an in-tree alias shim so `from ba2_trade_platform.core...` imports
(and `unittest.mock.patch` targets) resolve to the package module object.
`tests/test_alias_shim_race.py` auto-discovers every shim file and asserts the race guard, so
this file must carry it verbatim.

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_shim.py`:

```python
"""The in-tree portfolio_allocation path must BE the ba2_common module object."""
import importlib
import sys

import ba2_common.core.portfolio_allocation as pkg


def test_in_tree_path_resolves_to_the_package_module():
    shim = importlib.import_module("ba2_trade_platform.core.portfolio_allocation")
    assert shim is pkg
    assert sys.modules["ba2_trade_platform.core.portfolio_allocation"] is pkg


def test_shim_exposes_the_engine_entry_points():
    from ba2_trade_platform.core.portfolio_allocation import (
        AllocationPlan, PositionFetchFailed, VALUATION_MODE_COST, compute_allocation,
    )
    assert callable(compute_allocation)
    assert AllocationPlan().scale_factor == 1.0
    assert VALUATION_MODE_COST == "cost"
    assert issubclass(PositionFetchFailed, RuntimeError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_shim.py -v`

Expected: FAIL — both tests error with
```
E   ModuleNotFoundError: No module named 'ba2_trade_platform.core.portfolio_allocation'
```

- [ ] **Step 3: Write minimal implementation**

Create `ba2_trade_platform/core/portfolio_allocation.py` (copied verbatim from
`ba2_trade_platform/core/option_types.py` with only the import target changed — do not reword
the comment, the race guard is load-bearing):

```python
"""Alias shim: this in-tree module IS ba2_common.core.portfolio_allocation (Phase 6 migration).

The in-tree path is aliased to the package module object in sys.modules so
existing ``from ba2_trade_platform...`` imports resolve unchanged AND
``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path
operate on the real package module. Single source of truth: ba2_common.core.portfolio_allocation."""
import importlib as _importlib
import sys as _sys

_pkg = _importlib.import_module("ba2_common.core.portfolio_allocation")
# RACE GUARD: mirror the package's names onto THIS module BEFORE swapping it out of
# sys.modules. The swap alone leaves the original module object permanently empty, so a
# second thread reaching a LAZY ``from .X import Y`` while the first is still executing
# this body gets that empty object and raises "cannot import name 'Y'". That silently
# killed a live Monday enter-market run on 2026-08-17; see
# docs/2026-08-17-alias-shim-race.md. Locals are captured first because the update copies
# the package namespace wholesale -- a package binding _sys/_pkg must not break the swap.
_modules, _me, _target = _sys.modules, __name__, _pkg
globals().update({k: v for k, v in vars(_pkg).items() if not k.startswith('__')})
_modules[_me] = _target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_shim.py -v`
Expected: PASS — `2 passed`

Then confirm the shim survives the repo-wide guard:

Run: `venv/bin/python -m pytest tests/test_alias_shim_race.py -v`
Expected: PASS (the new shim satisfies the race-guard ordering checks).

- [ ] **Step 5: Commit**
```bash
git add ba2_trade_platform/core/portfolio_allocation.py tests/test_portfolio_allocation_shim.py
git commit -m "feat(allocation): in-tree alias shim for the allocation engine"
```

---

## Section D — Account seams and Alpaca

This section builds the broker-agnostic account seams (`AccountSnapshot`, `CashTransfer`,
`MarginInfo`, `OrderImpact`), their concrete no-op bases, the Alpaca implementations, the
`TradeActions.py:1493` fix, and fractional-aware Alpaca submission.

**Two things to internalise before you start.**

1. `packages/common/ba2_common/...` is the REAL source of truth. `ba2_trade_platform/core/*.py`
   files are *alias shims* that swap themselves out of `sys.modules`; editing a shim's body has
   no effect. New shared modules need a new shim, copied verbatim from
   `ba2_trade_platform/core/option_types.py` with only the `import_module(...)` target changed.
2. `get_account_snapshot()` / `get_cash_transfers()` / `get_symbol_margin_info()` /
   `preview_order_impact()` are **concrete, never `@abstractmethod`**. `ReadOnlyAccountInterface`
   already has 12 abstract methods; adding a 13th would make `IBKRAccount`, `TastyTradeAccount`
   and every test stub fail to instantiate with
   `TypeError: Can't instantiate abstract class ... without an implementation for abstract method 'get_account_snapshot'`.
   Concrete defaults mean every existing broker keeps working untouched.

Run tests **per file** (`venv/bin/python -m pytest <path> -v`). The venv is `venv/`, not
`.venv/`. The full suite fails non-deterministically from a pre-existing session leak, so a
per-file green is the signal.

---

### Task 27: The four broker-seam value objects (`account_types.py`) + its alias shim

**Do this task FIRST of everything in the plan** — Task 16 (the pure engine) imports
`MarginInfo` and `OrderImpact` from here.

**Files:**
- Create: `packages/common/ba2_common/core/account_types.py`
- Create: `ba2_trade_platform/core/account_types.py` (SHIM)
- Test: `packages/common/tests/test_account_types.py`
- Test: `tests/test_account_types_shim.py`

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_account_types.py`:
```python
"""ba2_common.core.account_types: the four broker-seam value objects.

These are pure dataclasses with no DB/SDK deps. They exist so that no call site
has to guess whether get_account_info() handed it a pydantic object (Alpaca), a
dict (IBKR/TastyTrade) or None (Alpaca auth failure).
"""
from datetime import date

from ba2_common.core.account_types import (
    CASH_TRANSFER_DEPOSIT,
    CASH_TRANSFER_DIVIDEND,
    CASH_TRANSFER_WITHDRAWAL,
    MARGIN_SOURCE_ASSET,
    MARGIN_SOURCE_DEFAULT,
    AccountSnapshot,
    CashTransfer,
    MarginInfo,
    OrderImpact,
)


def test_account_snapshot_defaults_every_money_field_to_none():
    """An empty snapshot means "the broker told us nothing", never "zero".

    The caller must raise rather than substitute a default, so a 0.0 default here
    would silently authorise a plan against an account we know nothing about.
    """
    snap = AccountSnapshot()
    assert snap.cash is None
    assert snap.equity is None
    assert snap.net_liquidation is None
    assert snap.buying_power is None
    assert snap.non_marginable_buying_power is None
    assert snap.margin_multiplier is None
    assert snap.long_market_value is None
    assert snap.short_market_value is None
    assert snap.pending_transfer_in is None
    assert snap.is_margin_account is False
    assert snap.supports_fractional is False
    assert snap.raw == {}


def test_account_snapshot_raw_dicts_are_not_shared_between_instances():
    a = AccountSnapshot()
    b = AccountSnapshot()
    a.raw["x"] = 1
    assert b.raw == {}


def test_cash_transfer_deposit_is_income():
    ev = CashTransfer(external_id="act-1", event_date=date(2026, 8, 1),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=1000.0)
    assert ev.is_income is True


def test_cash_transfer_dividend_is_income():
    ev = CashTransfer(external_id="act-3", event_date=date(2026, 8, 10),
                      event_type=CASH_TRANSFER_DIVIDEND, amount=12.34, symbol="AAPL")
    assert ev.is_income is True


def test_cash_transfer_withdrawal_is_not_income():
    ev = CashTransfer(external_id="act-2", event_date=date(2026, 8, 5),
                      event_type=CASH_TRANSFER_WITHDRAWAL, amount=-250.0)
    assert ev.is_income is False


def test_cash_transfer_zero_amount_deposit_is_not_income():
    """A zero-dollar deposit cannot fund an allocation run."""
    ev = CashTransfer(external_id="act-4", event_date=date(2026, 8, 6),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=0.0)
    assert ev.is_income is False


def test_cash_transfer_negative_amount_deposit_is_not_income():
    """A reversed/returned deposit arrives as a DEPOSIT with a negative amount.

    This is the case the ``amount > 0`` guard exists for: without it, a clawback
    would be counted as new money to allocate.
    """
    ev = CashTransfer(external_id="act-5", event_date=date(2026, 8, 7),
                      event_type=CASH_TRANSFER_DEPOSIT, amount=-1000.0)
    assert ev.is_income is False


def test_cash_transfer_event_type_literals_are_the_persisted_spellings():
    """These strings go into portfolio_income_event.event_type, so they are a
    schema contract: respelling one orphans every row already stored under it."""
    assert CASH_TRANSFER_DEPOSIT == "DEPOSIT"
    assert CASH_TRANSFER_WITHDRAWAL == "WITHDRAWAL"
    assert CASH_TRANSFER_DIVIDEND == "DIVIDEND"


def test_margin_info_defaults_to_the_conservative_source():
    info = MarginInfo(symbol="AAPL", bp_factor=2.0)
    assert info.source == MARGIN_SOURCE_DEFAULT == "default"
    assert info.marginable is True
    assert info.fractionable is False
    assert info.min_order_size is None
    assert info.min_trade_increment is None


def test_margin_info_records_where_the_factor_came_from():
    info = MarginInfo(symbol="AAPL", bp_factor=1.0, initial_margin_rate=0.5,
                      source=MARGIN_SOURCE_ASSET)
    assert info.source == MARGIN_SOURCE_ASSET
    assert info.initial_margin_rate == 0.5


def test_order_impact_bp_cost_flips_the_brokers_negative_buy_sign():
    """TastyTrade reports a BUY as a NEGATIVE change_in_buying_power. The engine
    consumes a POSITIVE cost, so bp_cost must flip the sign."""
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=-1500.0)
    assert impact.bp_cost == 1500.0


def test_order_impact_bp_cost_is_zero_when_the_order_frees_buying_power():
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=900.0)
    assert impact.bp_cost == 0.0


def test_order_impact_bp_cost_is_zero_at_exactly_zero_change():
    """The boundary of the ``< 0`` branch: a no-op order consumes nothing."""
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=0.0)
    assert impact.bp_cost == 0.0


def test_order_impact_defaults_to_accepted_with_no_errors():
    impact = OrderImpact(symbol="AAPL", change_in_buying_power=-10.0)
    assert impact.accepted is True
    assert impact.warnings == []
    assert impact.errors == []
```

`tests/test_account_types_shim.py`:
```python
"""The in-tree ba2_trade_platform.core.account_types must BE the package module.

Phase 6 alias shims swap themselves out of sys.modules so that both import paths
resolve to one module object; a shim that merely re-exported would give
unittest.mock.patch two different targets.
"""
from datetime import date


def test_in_tree_account_types_is_the_package_module():
    import ba2_common.core.account_types as pkg
    import ba2_trade_platform.core.account_types as shim
    assert shim is pkg


def test_in_tree_import_path_exposes_the_value_objects():
    from ba2_trade_platform.core.account_types import (
        CASH_TRANSFER_DEPOSIT,
        AccountSnapshot,
        CashTransfer,
        MarginInfo,
        OrderImpact,
    )
    assert CASH_TRANSFER_DEPOSIT == "DEPOSIT"
    assert AccountSnapshot().buying_power is None
    assert MarginInfo(symbol="A", bp_factor=1.0).symbol == "A"
    assert OrderImpact(symbol="A", change_in_buying_power=-1.0).bp_cost == 1.0
    assert CashTransfer(external_id="a", event_date=date(2026, 8, 1),
                        event_type=CASH_TRANSFER_DEPOSIT, amount=5.0).is_income is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_account_types.py -v`

Expected: collection error — `ModuleNotFoundError: No module named 'ba2_common.core.account_types'`

- [ ] **Step 3: Write minimal implementation**

`packages/common/ba2_common/core/account_types.py`:
```python
"""Broker-agnostic account value objects (pure dataclasses, no DB/SDK deps).

``get_account_info()`` returns a pydantic ``TradeAccount`` on Alpaca, a dict on
IBKR and TastyTrade, and ``None`` on Alpaca auth failure. These dataclasses are
the single shape every broker adapter maps ONTO, so no call site has to guess.

Every money field is a plain ``float``, and the ADAPTER coerces at the mapping
boundary: Alpaca publishes its balances as strings and TastyTrade's
``BuyingPowerEffect.change_in_buying_power`` is a ``Decimal``. Nothing here
re-coerces -- an uncoerced ``OrderImpact`` would make ``bp_cost`` return a
``Decimal`` in breach of its own ``-> float`` annotation, and that is the
adapter bug surfacing rather than being masked.

stdlib imports only -- this module must stay importable from both
``core/interfaces/*`` and ``core/portfolio_allocation.py`` with no cycle.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


# ``CashTransfer.event_type`` and ``portfolio_income_event.event_type`` are PLAIN
# str (small enums are str columns here -- matching OptionActivity.activity_type,
# and avoiding the SQLModel str-enum-stored-by-NAME migration trap). These are the
# ONLY legal spellings; always use the constant, never a bare literal.
CASH_TRANSFER_DEPOSIT = "DEPOSIT"
CASH_TRANSFER_WITHDRAWAL = "WITHDRAWAL"
CASH_TRANSFER_DIVIDEND = "DIVIDEND"

# Provenance of a MarginInfo.bp_factor, best first (see the design's
# "precheck over estimation" ordering).
MARGIN_SOURCE_PRECHECK = "precheck"    # broker order dry-run (preview_order_impact)
MARGIN_SOURCE_ASSET = "asset"          # per-asset metadata (Alpaca Asset + multiplier)
MARGIN_SOURCE_POSITION = "position"    # derived from a held position's requirement
MARGIN_SOURCE_DEFAULT = "default"      # conservative fallback = account multiplier


@dataclass
class AccountSnapshot:
    """Broker-agnostic cash / equity / buying-power state of one account.

    Every numeric field is ``Optional`` and defaults to ``None``: a field the
    broker did not supply stays ``None`` and the CALLER must raise rather than
    substitute a default (platform rule: no fallback values for prices, balances
    or quantities). ``None`` here means "unknown", never "zero".

    ``margin_multiplier`` is Alpaca's ``TradeAccount.multiplier`` (a STRING there:
    "1" / "2" / "4"), i.e. how many dollars of buying power one dollar of equity
    yields. It is the conservative ``default_bp_factor`` fed to the engine.

    ``equity`` is cash plus positions marked to market (Alpaca
    ``TradeAccount.equity``); ``net_liquidation`` is what the account would be
    worth if every position were closed right now (TastyTrade
    ``net-liquidating-value``). They are the same number for a cash/equities
    account and diverge only where liquidation value is not the mark. An adapter
    whose broker publishes only one MUST set BOTH to that value rather than
    leave one ``None``. Neither is the allocation denominator -- the engine's
    base is ``buying_power`` plus the managed position value (see
    ``build_base_snapshot``) -- so report ``net_liquidation`` as the account's
    headline total value.

    ``short_market_value`` is NEGATIVE while shorts are held (the Alpaca
    convention). A broker that publishes a positive magnitude instead
    (TastyTrade's ``short-equity-value``) MUST be negated by its adapter, so
    that gross exposure is one formula for every broker.
    """
    cash: Optional[float] = None
    equity: Optional[float] = None
    net_liquidation: Optional[float] = None
    buying_power: Optional[float] = None
    non_marginable_buying_power: Optional[float] = None
    margin_multiplier: Optional[float] = None
    is_margin_account: bool = False
    long_market_value: Optional[float] = None
    short_market_value: Optional[float] = None
    pending_transfer_in: Optional[float] = None
    supports_fractional: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CashTransfer:
    """One broker cash movement: a deposit, a withdrawal or a dividend.

    ``external_id`` MUST be the broker's own activity id -- it is the
    ``(account_id, external_id)`` idempotency key of ``portfolio_income_event``,
    so re-syncing the same window upserts instead of duplicating.

    ``amount`` is POSITIVE for deposits and dividends and NEGATIVE for
    withdrawals (Alpaca ``CSW`` net_amount). Only ``is_income`` rows are
    persisted to the ledger; withdrawals are not income.
    """
    external_id: str
    event_date: date
    event_type: str                     # CASH_TRANSFER_DEPOSIT | _WITHDRAWAL | _DIVIDEND
    amount: float
    symbol: Optional[str] = None        # payer symbol for DIVIDEND; None for cash transfers
    description: Optional[str] = None

    @property
    def is_income(self) -> bool:
        """True when this event may fund an allocation run."""
        return (
            self.event_type in (CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND)
            and self.amount > 0
        )


@dataclass
class MarginInfo:
    """Per-symbol margin / fractionability metadata used to size buying power.

    ``bp_factor`` = ``initial_margin_rate * account_multiplier`` -- the dollars of
    buying power one dollar of NOTIONAL consumes. A fully marginable stock in a
    2:1 account is ``0.5 * 2 = 1.0`` (dollar for dollar); a non-marginable one is
    ``1.0 * 2 = 2.0`` (double).

    ``min_order_size`` / ``min_trade_increment`` mirror Alpaca ``Asset`` field
    names exactly.
    """
    symbol: str
    bp_factor: float
    marginable: bool = True
    fractionable: bool = False
    min_order_size: Optional[float] = None
    min_trade_increment: Optional[float] = None
    initial_margin_rate: Optional[float] = None
    maintenance_margin_rate: Optional[float] = None
    source: str = MARGIN_SOURCE_DEFAULT


@dataclass
class OrderImpact:
    """Result of a broker-side order dry-run (precheck).

    ``change_in_buying_power`` is the broker's SIGNED value: TastyTrade's
    ``BuyingPowerEffect.change_in_buying_power`` is NEGATIVE for a buy (see the
    ``set_sign_for`` validator, tastytrade/order.py:366-393). Always consume the
    ``bp_cost`` property rather than the raw signed field.
    """
    symbol: str
    change_in_buying_power: float
    margin_requirement: Optional[float] = None   # isolated_order_margin_requirement
    estimated_fees: Optional[float] = None       # fee_calculation.total_fees
    accepted: bool = True                        # False when the broker returned errors
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def bp_cost(self) -> float:
        """Positive buying power CONSUMED by this order (0.0 when it frees BP)."""
        return -self.change_in_buying_power if self.change_in_buying_power < 0 else 0.0
```

`ba2_trade_platform/core/account_types.py` (copied verbatim from
`ba2_trade_platform/core/option_types.py`, only the module name changed):
```python
"""Alias shim: this in-tree module IS ba2_common.core.account_types (Phase 6 migration).

The in-tree path is aliased to the package module object in sys.modules so
existing ``from ba2_trade_platform...`` imports resolve unchanged AND
``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path
operate on the real package module. Single source of truth: ba2_common.core.account_types."""
import importlib as _importlib
import sys as _sys

_pkg = _importlib.import_module("ba2_common.core.account_types")
# RACE GUARD: mirror the package's names onto THIS module BEFORE swapping it out of
# sys.modules. The swap alone leaves the original module object permanently empty, so a
# second thread reaching a LAZY ``from .X import Y`` while the first is still executing
# this body gets that empty object and raises "cannot import name 'Y'". That silently
# killed a live Monday enter-market run on 2026-08-17; see
# docs/2026-08-17-alias-shim-race.md. Locals are captured first because the update copies
# the package namespace wholesale -- a package binding _sys/_pkg must not break the swap.
_modules, _me, _target = _sys.modules, __name__, _pkg
globals().update({k: v for k, v in vars(_pkg).items() if not k.startswith('__')})
_modules[_me] = _target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_account_types.py -v`
Expected: PASS (11 passed)

Run: `venv/bin/python -m pytest tests/test_account_types_shim.py tests/test_alias_shim_race.py -v`
Expected: PASS (`test_alias_shim_race.py` auto-discovers every shim by its swap line, so it now
covers the new one too)

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/account_types.py ba2_trade_platform/core/account_types.py packages/common/tests/test_account_types.py tests/test_account_types_shim.py
git commit -m "feat(core): broker-agnostic account value objects (AccountSnapshot/CashTransfer/MarginInfo/OrderImpact)"
```

---

### Task 28: Concrete `get_account_snapshot()` on `ReadOnlyAccountInterface` (dict brokers)

The base must read `get_account_info()` **tolerantly**, exactly the way
`MarketExpertInterface._get_actual_available_balance` does
(`packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:815`, whose
`_field` closure at `:829` is `obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)`
followed by `float()`). Read that method first; this is the same probe, widened to
every snapshot field and to the alternative names IBKR/TastyTrade use.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:3` (imports)
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:94` (insert before the `@abstractmethod` / `def get_positions` pair)
- Test: `packages/common/tests/test_account_seams.py`

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_account_seams.py`:
```python
"""The concrete broker seams on ReadOnlyAccountInterface / AccountInterface.

They are CONCRETE, never @abstractmethod: ReadOnlyAccountInterface already has 12
abstract methods, and adding a 13th would break instantiation of IBKRAccount,
TastyTradeAccount and every stub in the test suite.

_DictAccount below is the IBKR / TastyTrade shape: get_account_info() returns a
plain dict. AlpacaAccount's pydantic shape is covered in tests/test_alpaca_account_snapshot.py.
"""
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _DictAccount(ReadOnlyAccountInterface):
    """A broker whose get_account_info() returns a dict (IBKR / TastyTrade shape)."""

    def __init__(self, id, info):
        self.id = id
        self._info = info
        self._settings_cache = None

    def get_account_info(self):
        return self._info

    def get_balance(self):
        return None

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def symbols_exist(self, symbols):
        return {}

    def _get_instrument_current_price_impl(self, *a, **k):
        return None

    def get_balance_history(self, *a, **k):
        return []

    def get_dividends(self, *a, **k):
        return []

    def get_filled_trades(self, *a, **k):
        return []

    def get_order(self, *a, **k):
        return None

    def refresh_orders(self, *a, **k):
        return True

    def refresh_positions(self, *a, **k):
        return True


def test_snapshot_from_a_dict_broker_reads_the_tastytrade_field_names():
    """TastyTrade names them cash_balance / net_liquidating_value / equity_buying_power."""
    acct = _DictAccount(1, {
        "cash_balance": "12000.25",
        "net_liquidating_value": "48000.00",
        "equity_buying_power": "96000.00",
        "margin_multiplier": "2",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 12000.25
    assert snap.net_liquidation == 48000.00
    assert snap.buying_power == 96000.00
    assert snap.margin_multiplier == 2.0


def test_snapshot_from_a_dict_broker_reads_the_plain_field_names():
    acct = _DictAccount(1, {
        "cash": "500.00",
        "equity": "10000.00",
        "buying_power": "10000.00",
        "long_market_value": "9500.00",
        "short_market_value": "0",
        "multiplier": "1",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 10000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == 0.0


def test_snapshot_multiplier_above_one_marks_the_account_as_margin():
    assert _DictAccount(1, {"multiplier": "4"}).get_account_snapshot().is_margin_account is True


def test_snapshot_multiplier_of_one_is_a_cash_account():
    assert _DictAccount(1, {"multiplier": "1"}).get_account_snapshot().is_margin_account is False


def test_snapshot_of_a_broker_returning_none_is_all_unknown_not_all_zero():
    """None means "the broker told us nothing". A 0.0 here would let a caller
    plan against an account it cannot see."""
    snap = _DictAccount(1, None).get_account_snapshot()
    assert snap == AccountSnapshot()
    assert snap.buying_power is None


def test_snapshot_leaves_a_non_numeric_field_as_none_rather_than_guessing():
    snap = _DictAccount(1, {"buying_power": "n/a", "cash": "100"}).get_account_snapshot()
    assert snap.buying_power is None
    assert snap.cash == 100.0


def test_snapshot_from_an_attribute_broker_uses_the_getattr_branch():
    """The other half of the tolerant probe: an object, not a dict.

    This is the shape that broke TradeActions.py:1493 -- ``.get()`` on a pydantic
    TradeAccount raises AttributeError. Task 31 tests AlpacaAccount's OVERRIDE, so
    without this the base's ``getattr`` branch would have no coverage at all.
    ``raw`` stays {} because only a dict can be copied into it.
    """
    class _Attrs:
        cash = "500.00"
        equity = "10000.00"
        buying_power = "20000.00"
        long_market_value = "9500.00"
        short_market_value = "-250.00"
        multiplier = "2"

    snap = _DictAccount(1, _Attrs()).get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 20000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == -250.0
    assert snap.margin_multiplier == 2.0
    assert snap.is_margin_account is True
    assert snap.raw == {}
    # A field the object simply does not carry stays unknown, never 0.0.
    assert snap.pending_transfer_in is None
    assert snap.non_marginable_buying_power is None


def test_snapshot_survives_a_broker_that_raises():
    class _Boom(_DictAccount):
        def get_account_info(self):
            raise RuntimeError("connection reset")

    assert _Boom(1, None).get_account_snapshot() == AccountSnapshot()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_account_seams.py -v`

Expected: FAIL — every test errors with `AttributeError: '_DictAccount' object has no attribute 'get_account_snapshot'`

- [ ] **Step 3: Write minimal implementation**

Replace line 3 of `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py`:
```python
from datetime import datetime, timezone, timedelta
```
with:
```python
from datetime import datetime, timezone, timedelta, date
from ba2_common.core.account_types import AccountSnapshot, CashTransfer, MarginInfo
```

Then insert this method immediately **before** the `@abstractmethod` / `def get_positions(self)`
pair (line 96-97 as of HEAD, i.e. right after `get_account_info`'s closing `pass` at `:94`):
```python
    def get_account_snapshot(self) -> AccountSnapshot:
        """Broker-agnostic view of this account's cash / equity / buying power.

        CONCRETE ON PURPOSE: an ``@abstractmethod`` here would break every
        existing subclass's instantiation (IBKRAccount, TastyTradeAccount).

        The base implementation reads ``get_account_info()`` TOLERANTLY, in the
        manner of ``MarketExpertInterface._get_actual_available_balance``
        (MarketExpertInterface.py:815): the return may be a pydantic object
        (Alpaca ``TradeAccount``), a dict (IBKR, TastyTrade) or ``None`` (Alpaca
        auth failure), so every field is probed with a
        ``obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)``
        helper and coerced with ``float()`` (Alpaca ships them as STRINGS).
        Alpaca and TastyTrade override this properly.

        NEVER fabricates a number: a field the broker did not supply is left as
        ``None`` and the caller must raise rather than substitute a default
        (platform rule: no fallback values for prices/balances/quantities).

        Returns:
            AccountSnapshot: populated as far as the broker allows. An
            all-``None`` snapshot is a legitimate "the broker told us nothing"
            result, NOT an error -- it is the caller that must refuse to plan.
        """
        try:
            info = self.get_account_info()
        except Exception as e:
            logger.error(f"Account {self.id}: get_account_info() failed: {e}", exc_info=True)
            info = None

        if info is None:
            return AccountSnapshot()

        def _field(obj: Any, name: str) -> Optional[float]:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        def _first(*names: str) -> Optional[float]:
            """First of these field names the broker actually publishes."""
            for n in names:
                v = _field(info, n)
                if v is not None:
                    return v
            return None

        multiplier = _first("multiplier", "margin_multiplier")
        equity = _first("equity", "net_liquidating_value", "portfolio_value")
        return AccountSnapshot(
            cash=_first("cash", "cash_balance"),
            equity=equity,
            net_liquidation=_first("net_liquidation", "net_liquidating_value", "equity"),
            buying_power=_first("buying_power", "equity_buying_power", "derivative_buying_power"),
            non_marginable_buying_power=_first("non_marginable_buying_power",
                                               "cash_available_to_withdraw"),
            margin_multiplier=multiplier,
            is_margin_account=bool(multiplier is not None and multiplier > 1.0),
            long_market_value=_first("long_market_value"),
            short_market_value=_first("short_market_value"),
            pending_transfer_in=_first("pending_transfer_in"),
            supports_fractional=False,
            raw=dict(info) if isinstance(info, dict) else {},
        )

```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_account_seams.py -v`
Expected: PASS (8 passed)

Run: `venv/bin/python -m pytest packages/common/tests/test_interfaces_import.py -v`
Expected: PASS (proves no subclass lost the ability to instantiate)

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py packages/common/tests/test_account_seams.py
git commit -m "feat(accounts): concrete get_account_snapshot() seam on ReadOnlyAccountInterface"
```

---

### Task 29: Concrete `get_cash_transfers()` and `get_symbol_margin_info()` defaults

Both return an empty container so no existing broker breaks. `get_cash_transfers`
deliberately does **not** distinguish failure from emptiness (unlike `get_positions()`,
where `None` means "fetch failed"): an implementation that fails must log and return `[]`.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` (immediately after the `get_account_snapshot` you added in Task 28)
- Test: `packages/common/tests/test_account_seams.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `packages/common/tests/test_account_seams.py`:
```python
def test_get_cash_transfers_defaults_to_empty_for_a_broker_that_does_not_implement_it():
    """[] by default so no existing broker breaks. Alpaca and TastyTrade override it."""
    assert _DictAccount(1, {}).get_cash_transfers() == []


def test_get_cash_transfers_accepts_a_date_window_without_complaining():
    from datetime import date
    acct = _DictAccount(1, {})
    assert acct.get_cash_transfers(start_date=date(2026, 8, 1),
                                   end_date=date(2026, 8, 31)) == []


def test_get_symbol_margin_info_defaults_to_empty_so_the_caller_falls_back():
    """A symbol the broker cannot describe is OMITTED, never defaulted here -- the
    caller substitutes the conservative bp_factor = account multiplier."""
    assert _DictAccount(1, {}).get_symbol_margin_info(["AAPL", "MSFT"]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_account_seams.py -k "cash_transfers or margin_info" -v`

Expected: FAIL — `AttributeError: '_DictAccount' object has no attribute 'get_cash_transfers'`

- [ ] **Step 3: Write minimal implementation**

Insert immediately after the `get_account_snapshot` method added in Task 28 (still before
`@abstractmethod def get_positions`):
```python
    def get_cash_transfers(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[CashTransfer]:
        """Cash movements (deposits, withdrawals, dividends) over a date window.

        CONCRETE, returns ``[]`` by default so no existing broker breaks. Alpaca
        overrides it from the ``CSD``/``CSW`` activity endpoint that
        ``get_balance_history`` already calls inline (AlpacaAccount.py:4376-4382)
        plus the existing ``get_dividends()``; TastyTrade from
        ``get_history(types=["Money Movement"], page_offset=None)``.

        ``CashTransfer.external_id`` MUST be the broker's own activity id: it is
        the ``(account_id, external_id)`` idempotency key of
        ``portfolio_income_event``, so re-syncing the same window upserts rather
        than duplicating -- exactly as ``OptionActivity`` does.

        Args:
            start_date: inclusive lower bound; ``None`` means "broker default".
                A ``datetime`` is accepted (``date`` is its supertype).
            end_date: inclusive upper bound; ``None`` means "up to now".

        Returns:
            List[CashTransfer]: empty when the broker has none OR when the broker
            does not implement this seam. Unlike ``get_positions()``, this seam
            does NOT distinguish failure from emptiness -- an implementation that
            fails must log the error and return ``[]``.
        """
        return []

    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        """Per-symbol margin / fractionability metadata, for buying-power sizing.

        CONCRETE, returns ``{}`` by default. Alpaca derives each entry from
        ``Asset.marginable``, ``Asset.maintenance_margin_requirement``,
        ``Asset.fractionable``, ``Asset.min_order_size`` and
        ``Asset.min_trade_increment`` combined with ``TradeAccount.multiplier``.

        ``bp_factor = initial_margin_rate * account_multiplier`` -- the dollars of
        buying power one dollar of notional consumes. A fully marginable stock in
        a 2:1 account is ``0.5 * 2 = 1.0``; a non-marginable one is ``1.0 * 2 = 2.0``.

        Args:
            symbols: symbols to describe, already normalised (.strip().upper()).

        Returns:
            Dict[str, MarginInfo]: keyed by symbol. A symbol the broker cannot
            describe is OMITTED, never defaulted here -- the caller falls back to
            the conservative ``bp_factor = account multiplier`` (assume no
            leverage), which under-deploys rather than over-committing.
        """
        return {}

```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_account_seams.py -v`
Expected: PASS (11 passed -- Task 28 added an 8th snapshot test,
`test_snapshot_from_an_attribute_broker_uses_the_getattr_branch`, beyond the 7 the
plan originally listed, so this file holds 8 snapshot tests + these 3)

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py packages/common/tests/test_account_seams.py
git commit -m "feat(accounts): concrete get_cash_transfers()/get_symbol_margin_info() seams"
```

---

### Task 30: Concrete `preview_order_impact()` on `AccountInterface`

`None` means "this broker has no precheck", **not** "the order is free". A caller
that treats `None` as a zero impact will over-commit buying power.

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py:9` (imports)
- Modify: `packages/common/ba2_common/core/interfaces/AccountInterface.py:54` (insert before `_classify_order_error`)
- Test: `packages/common/tests/test_preview_order_impact.py`

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_preview_order_impact.py`:
```python
"""AccountInterface.preview_order_impact: a broker-side order dry-run.

CONCRETE and returning None by default. None means "this broker has no precheck",
NOT "the order is free" -- Alpaca has no order-preview endpoint and keeps the
base, so it relies on get_symbol_margin_info() instead. TastyTrade overrides it.
"""
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.models import TradingOrder
from ba2_common.core.types import OrderDirection, OrderType


class _StubTradingAccount(AccountInterface):
    """Minimal concrete AccountInterface: only preview_order_impact is under test,
    so every abstract method is a no-op stub purely to satisfy ABC instantiation."""

    def __init__(self, id):
        self.id = id
        self._settings_cache = None

    def _get_instrument_current_price_impl(self, *a, **k): return None
    def _submit_order_impl(self, *a, **k): return None
    def adjust_sl(self, *a, **k): return None
    def adjust_tp(self, *a, **k): return None
    def adjust_tp_sl(self, *a, **k): return None
    def cancel_order(self, *a, **k): return None
    def get_account_info(self): return {}
    def get_balance(self): return None
    def get_balance_history(self, *a, **k): return []
    def get_dividends(self, *a, **k): return []
    def get_filled_trades(self, *a, **k): return []
    def get_order(self, *a, **k): return None
    def get_orders(self, status=None): return []
    def get_positions(self): return []
    def modify_order(self, *a, **k): return None
    def refresh_orders(self, *a, **k): return True
    def refresh_positions(self, *a, **k): return True
    def symbols_exist(self, symbols): return {}


def _order():
    return TradingOrder(account_id=1, symbol="AAPL", quantity=10.0,
                        side=OrderDirection.BUY, order_type=OrderType.MARKET)


def test_preview_order_impact_returns_none_when_the_broker_has_no_precheck():
    assert _StubTradingAccount(1).preview_order_impact(_order()) is None


def test_preview_order_impact_does_not_mutate_or_persist_the_candidate_order():
    """It is a DRY RUN: it must not save the row or stamp a broker_order_id."""
    order = _order()
    _StubTradingAccount(1).preview_order_impact(order)
    assert order.id is None
    assert order.broker_order_id is None
    assert order.quantity == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_preview_order_impact.py -v`

Expected: FAIL — `AttributeError: '_StubTradingAccount' object has no attribute 'preview_order_impact'`

- [ ] **Step 3: Write minimal implementation**

In `packages/common/ba2_common/core/interfaces/AccountInterface.py`, insert this import line
immediately **before** line 9 (`from ba2_common.core.interfaces.ReadOnlyAccountInterface import ...`):
```python
from ba2_common.core.account_types import OrderImpact
```

Then insert this method at line 54, immediately **before**
`def _classify_order_error(self, exc: Exception) -> BrokerOrderErrorReason:` (i.e. right after
`_submit_order_impl`'s closing `pass`):
```python
    def preview_order_impact(self, trading_order: TradingOrder) -> Optional[OrderImpact]:
        """Broker-side dry-run of ONE order: what it would cost in buying power.

        CONCRETE, returns ``None`` by default. ``None`` means "this broker has no
        precheck", NOT "the order is free" -- a caller that treats ``None`` as a
        zero impact will over-commit. Alpaca has no order-preview endpoint and
        keeps the base ``None``, so it relies on ``get_symbol_margin_info()``.
        TastyTrade implements it with
        ``Account.place_order(session, order, dry_run=True)``.

        MUST NOT send a live order.
        ``tastytrade.account.Account.place_order``'s ``dry_run`` parameter
        DEFAULTS TO ``True`` (site-packages/tastytrade/account.py:877-879) --
        pass it explicitly here anyway, and never rely on that default at a real
        submission call site.

        Args:
            trading_order: a candidate TradingOrder (saved or unsaved) describing
                the order. This method must NOT mutate or persist it, and must
                not set ``broker_order_id``.

        Returns:
            Optional[OrderImpact]: ``None`` when the broker does not support
            prechecks OR when the preview call itself failed -- log the failure
            (``logger.error(..., exc_info=True)`` inside the except block); do not
            fabricate a zero impact.
        """
        return None

```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_preview_order_impact.py -v`
Expected: PASS (2 passed)

Run: `venv/bin/python -m pytest packages/common/tests/test_interfaces_import.py packages/common/tests/test_cleanroom_gate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/interfaces/AccountInterface.py packages/common/tests/test_preview_order_impact.py
git commit -m "feat(accounts): concrete preview_order_impact() seam on AccountInterface"
```

---

### Task 31: `AlpacaAccount.get_account_snapshot()` from the pydantic `TradeAccount`

`AlpacaAccount.get_account_info()` returns the **raw pydantic `TradeAccount`**
(AlpacaAccount.py:1489-1505) or `None` on auth failure. Every money field on it is
`Optional[str]` — including `multiplier`, which is the string `"1"`/`"2"`/`"4"` — so
everything goes through `float()`.

The Task-28 base probe already reads those attributes correctly (`getattr` + `float`).
The override exists for the two things the base cannot do: `supports_fractional`
(a *second* endpoint, `get_account_configurations()`, which the base must never
call) and a populated `raw`. It also pins Alpaca's behaviour with a test so a future
base-class refactor cannot silently break it.

Per the pinned contract the return type is `AccountSnapshot`, never `None`: an
auth failure yields an **all-`None` snapshot** (a legitimate "the broker told us
nothing"), which keeps the type stable while still forcing the caller to refuse
to plan.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py:14` (imports)
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py:1506` (insert before the `@alpaca_api_retry` that decorates `symbols_exist`)
- Test: `tests/test_alpaca_account_snapshot.py`

- [ ] **Step 1: Write the failing test**

`tests/test_alpaca_account_snapshot.py`:
```python
"""AlpacaAccount.get_account_snapshot against a REAL pydantic TradeAccount.

This is the whole point of the snapshot: Alpaca hands back a pydantic object with
no .get() and every money field typed Optional[str], while IBKR/TastyTrade hand
back a dict of floats. No live API call is made -- self.client is a MagicMock and
the TradeAccount / Asset objects are constructed from the installed alpaca-py SDK.
"""
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import AccountStatus
from alpaca.trading.models import TradeAccount

from ba2_trade_platform.core.account_types import AccountSnapshot
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account():
    """An AlpacaAccount without __init__ (no credentials, no broker connection).
    client is a MagicMock so _check_authentication() passes."""
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}
    return acct


def _trade_account(**overrides):
    """A real pydantic TradeAccount, money fields as STRINGS exactly like Alpaca."""
    kwargs = dict(
        id=uuid4(),
        account_number="PA1",
        status=AccountStatus.ACTIVE,
        cash="1000.50",
        equity="25000.00",
        buying_power="50000.00",
        non_marginable_buying_power="1000.50",
        multiplier="2",
        long_market_value="24000.00",
        short_market_value="0",
        pending_transfer_in="500.00",
    )
    kwargs.update(overrides)
    return TradeAccount(**kwargs)


def test_snapshot_coerces_alpacas_string_money_fields_to_floats():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    snap = acct.get_account_snapshot()

    assert snap.cash == 1000.50
    assert snap.equity == 25000.00
    assert snap.net_liquidation == 25000.00
    assert snap.buying_power == 50000.00
    assert snap.non_marginable_buying_power == 1000.50
    assert snap.long_market_value == 24000.00
    assert snap.short_market_value == 0.0
    assert snap.pending_transfer_in == 500.00


def test_snapshot_reads_the_string_multiplier_as_a_number_and_flags_margin():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account(multiplier="4")
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    snap = acct.get_account_snapshot()

    assert snap.margin_multiplier == 4.0
    assert snap.is_margin_account is True


def test_snapshot_of_a_cash_account_is_not_flagged_as_margin():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account(multiplier="1")
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=False)

    snap = acct.get_account_snapshot()

    assert snap.margin_multiplier == 1.0
    assert snap.is_margin_account is False


def test_snapshot_reports_fractional_capability_from_account_configurations():
    """TradeAccount has no fractional field -- it lives on AccountConfiguration."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    assert acct.get_account_snapshot().supports_fractional is True


def test_snapshot_reports_no_fractional_when_the_account_has_it_disabled():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=False)

    assert acct.get_account_snapshot().supports_fractional is False


def test_snapshot_keeps_the_account_identity_in_raw():
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)

    assert acct.get_account_snapshot().raw["account_number"] == "PA1"


def test_snapshot_on_auth_failure_is_all_unknown_not_all_zero():
    """get_account_info() returns None when Alpaca rejects the credentials. A 0.0
    buying power here would let the allocation page plan against a dead account."""
    acct = _bare_account()
    acct.client.get_account.side_effect = Exception("401 unauthorized")

    snap = acct.get_account_snapshot()

    assert snap == AccountSnapshot()
    assert snap.buying_power is None
    assert snap.margin_multiplier is None


def test_snapshot_still_returns_the_money_when_account_configurations_fails():
    """A failing capability probe must not lose the balances we did get."""
    acct = _bare_account()
    acct.client.get_account.return_value = _trade_account()
    acct.client.get_account_configurations.side_effect = Exception("500 server error")

    snap = acct.get_account_snapshot()

    assert snap.buying_power == 50000.00
    assert snap.supports_fractional is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_alpaca_account_snapshot.py -v`

Expected: FAIL — `test_snapshot_reports_fractional_capability_from_account_configurations` fails
with `assert False is True` and `test_snapshot_keeps_the_account_identity_in_raw` with
`KeyError: 'account_number'`. The purely-numeric tests already pass because the Task-28 base
probe reads the pydantic attributes correctly; that inherited pass is expected, and the override
is what pins `supports_fractional` and `raw`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/AlpacaAccount.py`, replace line 14:
```python
from ...core.interfaces import AccountInterface
```
with:
```python
from ...core.interfaces import AccountInterface
from ...core.account_types import (
    AccountSnapshot, CashTransfer, MarginInfo,
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_WITHDRAWAL, CASH_TRANSFER_DIVIDEND,
    MARGIN_SOURCE_ASSET,
)
```

Then insert this method at line 1506, immediately **before** the `@alpaca_api_retry` decorator
that precedes `def symbols_exist(...)` (i.e. right after `get_account_info`'s `return None`):
```python
    def get_account_snapshot(self) -> AccountSnapshot:
        """Broker-agnostic cash / equity / buying-power view of this Alpaca account.

        Overrides the tolerant base probe because Alpaca needs a SECOND endpoint
        (get_account_configurations) for the fractional-trading capability, which
        the base must not call. Every money field on the pydantic TradeAccount is
        Optional[str] -- including multiplier ("1"/"2"/"4") -- so everything goes
        through float().

        Returns an ALL-None AccountSnapshot (never None) when get_account_info()
        returns None on auth failure: the type stays stable and the caller must
        refuse to plan rather than substitute zeros.
        """
        info = self.get_account_info()
        if info is None:
            logger.error(f"[Account {self.id}] get_account_info() returned None -- empty snapshot")
            return AccountSnapshot()

        def _f(name: str) -> Optional[float]:
            val = getattr(info, name, None)
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                logger.warning(f"[Account {self.id}] TradeAccount.{name}={val!r} is not numeric")
                return None

        multiplier = _f('multiplier')
        equity = _f('equity')

        # TradeAccount carries no fractional flag; AccountConfiguration does.
        # A failure here must not lose the balances we already have.
        supports_fractional = False
        try:
            supports_fractional = bool(getattr(self.client.get_account_configurations(),
                                               'fractional_trading', False))
        except Exception as e:
            logger.debug(f"[Account {self.id}] Could not read account configurations: {e}")

        return AccountSnapshot(
            cash=_f('cash'),
            equity=equity,
            net_liquidation=equity,
            buying_power=_f('buying_power'),
            non_marginable_buying_power=_f('non_marginable_buying_power'),
            margin_multiplier=multiplier,
            is_margin_account=bool(multiplier is not None and multiplier > 1.0),
            long_market_value=_f('long_market_value'),
            short_market_value=_f('short_market_value'),
            pending_transfer_in=_f('pending_transfer_in'),
            supports_fractional=supports_fractional,
            raw={'account_number': getattr(info, 'account_number', None),
                 'status': str(getattr(info, 'status', None))},
        )

```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_alpaca_account_snapshot.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**
```bash
git add ba2_trade_platform/modules/accounts/AlpacaAccount.py tests/test_alpaca_account_snapshot.py
git commit -m "feat(alpaca): get_account_snapshot() from the pydantic TradeAccount"
```

---

### Task 32: `AlpacaAccount.get_symbol_margin_info()` — one `get_asset()` per symbol, cached

> **Alpaca publishes a MAINTENANCE requirement, not an initial rate.** `Asset` exposes
> `maintenance_margin_requirement`; the contract wants
> `bp_factor = initial_margin_rate × account_multiplier`. Do not silently substitute the maintenance
> number. Derive the initial rate explicitly and document it: Reg-T is `marginable → 0.5`, otherwise
> `1.0`, which is what makes the docstring's worked examples come out at 1.0 and 2.0 in a 2:1 account.
>
> **`bp_factor` is required and has no default**, so a symbol you can only partially describe must be
> OMITTED from the returned dict rather than given a guessed factor. The `{}`-means-fall-back-to-the-
> account-multiplier contract is only conservative if overrides honour omission.


**Alpaca has no bulk asset endpoint.** `TradingClient.get_asset(symbol_or_asset_id)`
(alpaca/trading/client.py:399) takes exactly one symbol, so a 40-symbol basket costs
40 HTTP calls. That is why the result is cached on the instance for its whole
lifetime — the allocation page asks for the same basket on every refresh, and the
data (marginability, fractionability, increments) does not change intraday.

Alpaca's `Asset` exposes **no initial-margin field**, only
`maintenance_margin_requirement` (a percentage such as `30.0`). So the initial rate
is *derived*: `marginable -> 0.5` (Reg-T), `not marginable -> 1.0`. Then
`bp_factor = initial_rate * account_multiplier`.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py:87` (add the cache to `__init__`)
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (insert after the `get_account_snapshot` added in Task 31)
- Test: `tests/test_alpaca_margin_info.py`

- [ ] **Step 1: Write the failing test**

`tests/test_alpaca_margin_info.py`:
```python
"""AlpacaAccount.get_symbol_margin_info against mocked Asset / TradeAccount objects.

bp_factor = initial_margin_rate * account_multiplier, and Alpaca's Asset has NO
initial-margin field, so the rate is derived: marginable -> 0.5 (Reg-T),
non-marginable -> 1.0. In a 2:1 account that is 1.0 vs 2.0.

No live API call: client is a MagicMock returning real alpaca-py model objects.
"""
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import (
    AccountStatus, AssetClass, AssetExchange, AssetStatus,
)
from alpaca.trading.models import Asset, TradeAccount

from ba2_trade_platform.core.account_types import MARGIN_SOURCE_ASSET
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account(multiplier="2"):
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}
    acct.client.get_account.return_value = TradeAccount(
        id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
        cash="1000", equity="25000", buying_power="50000", multiplier=multiplier)
    acct.client.get_account_configurations.return_value = MagicMock(fractional_trading=True)
    return acct


def _asset(symbol="AAPL", marginable=True, fractionable=True,
           min_order_size=0.001, min_trade_increment=0.001,
           maintenance_margin_requirement=30.0):
    # `asset_class` is exposed under the pydantic alias "class", so it must be
    # passed via a dict splat -- Asset(asset_class=...) raises "Field required".
    return Asset(
        id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
        symbol=symbol, status=AssetStatus.ACTIVE, tradable=True, marginable=marginable,
        shortable=True, easy_to_borrow=True, fractionable=fractionable,
        min_order_size=min_order_size, min_trade_increment=min_trade_increment,
        maintenance_margin_requirement=maintenance_margin_requirement)


def test_marginable_symbol_in_a_2x_account_consumes_buying_power_dollar_for_dollar():
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset("AAPL", marginable=True)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.bp_factor == 1.0          # 0.5 Reg-T * 2 multiplier
    assert info.initial_margin_rate == 0.5
    assert info.marginable is True
    assert info.source == MARGIN_SOURCE_ASSET


def test_non_marginable_symbol_in_a_2x_account_consumes_double():
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.return_value = _asset("GME", marginable=False)

    info = acct.get_symbol_margin_info(["GME"])["GME"]

    assert info.bp_factor == 2.0          # 1.0 * 2 multiplier
    assert info.initial_margin_rate == 1.0
    assert info.marginable is False


def test_marginable_symbol_in_a_cash_account_consumes_half():
    acct = _bare_account(multiplier="1")
    acct.client.get_asset.return_value = _asset("AAPL", marginable=True)

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].bp_factor == 0.5


def test_maintenance_margin_percentage_is_converted_to_a_rate():
    """Alpaca publishes 30.0 meaning 30%; MarginInfo carries the 0-1 rate."""
    acct = _bare_account()
    acct.client.get_asset.return_value = _asset("AAPL", maintenance_margin_requirement=30.0)

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].maintenance_margin_rate == 0.3


def test_fractionability_and_trade_increments_are_carried_through():
    acct = _bare_account()
    acct.client.get_asset.return_value = _asset(
        "AAPL", fractionable=True, min_order_size=0.001, min_trade_increment=0.001)

    info = acct.get_symbol_margin_info(["AAPL"])["AAPL"]

    assert info.fractionable is True
    assert info.min_order_size == 0.001
    assert info.min_trade_increment == 0.001


def test_symbols_are_normalised_before_lookup():
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    infos = acct.get_symbol_margin_info([" aapl ", "msft"])

    assert set(infos) == {"AAPL", "MSFT"}
    assert acct.client.get_asset.call_args_list[0][0][0] == "AAPL"


def test_a_symbol_the_broker_cannot_describe_is_omitted_not_defaulted():
    """An omitted symbol makes the caller fall back to the conservative
    account-multiplier factor. A fabricated entry here would over-deploy."""
    acct = _bare_account()

    def _get_asset(symbol):
        if symbol == "BADSYM":
            raise Exception("404 asset not found")
        return _asset(symbol)

    acct.client.get_asset.side_effect = _get_asset

    infos = acct.get_symbol_margin_info(["AAPL", "BADSYM"])

    assert set(infos) == {"AAPL"}


def test_a_second_request_for_the_same_symbol_hits_the_cache_not_the_api():
    """Alpaca has no bulk asset endpoint, so this is one HTTP call per symbol --
    the page refreshes the same basket repeatedly and must not re-fetch."""
    acct = _bare_account()
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    acct.get_symbol_margin_info(["AAPL", "MSFT"])
    calls_after_first = acct.client.get_asset.call_count
    acct.get_symbol_margin_info(["AAPL", "MSFT"])

    assert calls_after_first == 2
    assert acct.client.get_asset.call_count == 2


def test_a_cached_symbol_is_repriced_when_the_account_multiplier_changes():
    """The cache holds the ASSET facts (marginability, increments), which do not
    change intraday. The multiplier does -- Alpaca moves an account between 1/2/4
    as it crosses the PDT threshold -- and this process is long-lived, so a cache
    hit must re-derive bp_factor from the multiplier read on THIS call."""
    acct = _bare_account(multiplier="2")
    acct.client.get_asset.side_effect = lambda s: _asset(s, marginable=True)

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].bp_factor == 1.0

    acct.client.get_account.return_value = TradeAccount(
        id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
        cash="1000", equity="25000", buying_power="100000", multiplier="4")

    assert acct.get_symbol_margin_info(["AAPL"])["AAPL"].bp_factor == 2.0
    assert acct.client.get_asset.call_count == 1     # still no second asset fetch


def test_no_margin_info_at_all_when_the_account_multiplier_is_unknown():
    """Without a multiplier there is no honest bp_factor to compute."""
    acct = _bare_account()
    acct.client.get_account.return_value = None
    acct.client.get_asset.side_effect = lambda s: _asset(s)

    assert acct.get_symbol_margin_info(["AAPL"]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_alpaca_margin_info.py -v`

Expected: FAIL — the base returns `{}`, so the first test fails with `KeyError: 'AAPL'`.
`test_no_margin_info_at_all_when_the_account_multiplier_is_unknown` PASSES vacuously
against that same `{}`, so the run is `8 failed, 1 passed` before the repricing test
is added and `9 failed, 1 passed` after.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/AlpacaAccount.py`, in `__init__`, replace lines 87-88:
```python
        # Balance cache (5s TTL; serves stale value on fetch failure)
        self._balance_cache: Optional[float] = None
```
with:
```python
        # Per-symbol margin metadata cache. Alpaca has NO bulk asset endpoint, so
        # get_symbol_margin_info() costs one get_asset() HTTP call per NEW symbol;
        # the allocation page asks for the same basket on every refresh and the
        # ASSET facts do not change intraday, so cache for this instance's
        # lifetime. bp_factor is re-derived on every hit (the multiplier moves).
        self._margin_info_cache: Dict[str, MarginInfo] = {}

        # Balance cache (5s TTL; serves stale value on fetch failure)
        self._balance_cache: Optional[float] = None
```

Add `from dataclasses import replace` to the imports (used to reprice a cache hit).

Then insert this method immediately after the `get_account_snapshot` added in Task 31:
```python
    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        """Per-symbol margin / fractionability metadata for buying-power sizing.

        Alpaca has NO bulk asset endpoint -- TradingClient.get_asset() takes one
        symbol -- so this is one HTTP call per symbol NOT already cached. Callers
        should therefore pass the whole basket once and reuse the result.

        Alpaca's Asset exposes no INITIAL margin field, only
        maintenance_margin_requirement (a percentage, e.g. 30.0), so the initial
        rate is DERIVED: marginable -> 0.5 (Reg-T), otherwise 1.0. The
        maintenance number is reported separately as maintenance_margin_rate and
        is NEVER substituted for the initial rate. Then
        bp_factor = initial_rate * account multiplier -- 1.0 for a marginable
        symbol and 2.0 for a non-marginable one in a 2:1 account.

        The multiplier is read from get_account_info() (ONE get_account() call),
        not from get_account_snapshot(), which would add a get_account_configurations()
        round-trip for a fractional flag this method does not use. It is read
        fresh on every call and re-applied to cached entries, because Alpaca
        moves an account between 1/2/4 and this process is long-lived; only the
        Asset facts, which do not change intraday, are actually cached.

        A symbol the broker cannot describe is OMITTED, never defaulted here --
        the caller falls back to the conservative bp_factor = account multiplier.
        One symbol's failure never aborts the batch.
        """
        if not self._check_authentication():
            return {}

        info = self.get_account_info()
        multiplier = self._safe_float(getattr(info, 'multiplier', None)) if info is not None else None
        if multiplier is None:
            logger.warning(f"[Account {self.id}] No account multiplier -- cannot size bp_factor")
            return {}

        cache = self._margin_info_cache
        out: Dict[str, MarginInfo] = {}
        for raw_symbol in symbols:
            symbol = (raw_symbol or '').strip().upper()
            if not symbol:
                continue

            cached = cache.get(symbol)
            if cached is not None:
                # Only the Asset facts are cached; bp_factor is re-derived from the
                # multiplier read on THIS call. The None guard keeps an entry from a
                # future non-asset source (a precheck) out of the arithmetic.
                rate = cached.initial_margin_rate
                if rate is not None and cached.bp_factor != rate * multiplier:
                    cached = replace(cached, bp_factor=rate * multiplier)
                    cache[symbol] = cached
                out[symbol] = cached
                continue

            try:
                asset = self.client.get_asset(symbol)
            except Exception as e:
                logger.warning(f"[Account {self.id}] get_asset({symbol}) failed: {e}")
                continue
            if asset is None:
                logger.warning(f"[Account {self.id}] get_asset({symbol}) returned nothing")
                continue

            marginable = bool(getattr(asset, 'marginable', False))
            initial_rate = 0.5 if marginable else 1.0
            maint = self._safe_float(getattr(asset, 'maintenance_margin_requirement', None))
            margin_info = MarginInfo(
                symbol=symbol,
                bp_factor=initial_rate * multiplier,
                marginable=marginable,
                fractionable=bool(getattr(asset, 'fractionable', False)),
                min_order_size=self._safe_float(getattr(asset, 'min_order_size', None)),
                min_trade_increment=self._safe_float(getattr(asset, 'min_trade_increment', None)),
                initial_margin_rate=initial_rate,
                maintenance_margin_rate=(maint / 100.0) if maint is not None else None,
                source=MARGIN_SOURCE_ASSET,
            )
            cache[symbol] = margin_info
            out[symbol] = margin_info

        return out

    def _safe_float(self, value: Any) -> Optional[float]:
        """Coerce a broker-supplied number to a plain float, None when not numeric.

        Alpaca types these Optional[str] or Optional[float] depending on the
        field (TradeAccount.multiplier is a string, Asset.min_order_size a float),
        and the value dataclasses only carry floats.
        """
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.warning(f"[Account {self.id}] Non-numeric broker value {value!r}")
            return None

```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_alpaca_margin_info.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**
```bash
git add ba2_trade_platform/modules/accounts/AlpacaAccount.py tests/test_alpaca_margin_info.py
git commit -m "feat(alpaca): get_symbol_margin_info() from Asset metadata, cached per instance"
```

---

### Task 33: `AlpacaAccount.get_cash_transfers()` from the CSD/CSW activities + dividends

> **`external_id` is the highest-risk detail in this task.** The existing inline CSD/CSW code at
> `AlpacaAccount.py:4375-4381` reads only `act.get('date')` and `act.get('net_amount')` — it never
> touches the activity id. `CashTransfer.external_id` MUST carry `act['id']`, because the income
> ledger upserts on `(account_id, external_id)` and that key is the only thing making a re-sync
> idempotent. Get this wrong and every refresh duplicates the ledger.
>
> **Dividends and cash transfers are two id spaces.** This override composes the CSD/CSW activities
> *and* `get_dividends()`. Confirm the dividend records carry a stable broker id that cannot collide
> with a CSD/CSW id — if they can, namespace the `external_id` (e.g. prefix by source) rather than
> hoping. A collision silently drops one of the two events.


Before writing this, **read** `AlpacaAccount.get_balance_history` (`:4354`, whose
CSD/CSW loop is inline at `:4376-4382`) and `AlpacaAccount.get_dividends` (`:4283`).
Reuse their request idiom exactly: `self.client.get("/account/activities/<TYPE>", params or None)`
with `params["after"] = start.isoformat()` / `params["until"] = end.isoformat()`.
`TradingClient` does not expose `get_account_activities`, which is why both existing
methods use the raw REST `get()`.

`external_id` is the idempotency key of `portfolio_income_event`. CSD/CSW activities
carry a broker `id`. `get_dividends()` deliberately drops it (it nets DIVNRA tax
withholding into the amount, which is the number we want), so dividends get the
stable synthetic key `DIV:<symbol>:<YYYY-MM-DD>` — unique per payer per pay date.

**Answers to the two routed findings (settled while implementing):**

1. *Id spaces.* They cannot collide **in practice** — an Alpaca non-trade activity id is
   `<17 digits>::<uuid>`, which can never spell `DIV:<symbol>:<date>` — but "cannot in
   practice" is not the same as "cannot". The `DIV:` namespace is hoisted to a module
   constant `_DIVIDEND_KEY_PREFIX` and a CSD/CSW broker id that *starts with it* is
   re-namespaced to `<TYPE>:<id>`. Real ids are still carried through **verbatim**
   (`external_id == "act-1"`), so this costs nothing and removes the "hoping".
2. *Activity date vs T+1 settled date.* The ledger stores the **activity** date. The T+1
   shift in `get_balance_history` exists to line a transfer up with the equity-curve day
   it moved equity — a P/L-attribution concern. The ledger instead answers "what money
   arrived", and the shift target depends on which portfolio-history days happen to be in
   the requested window, so a shifted `event_date` would *wobble between syncs* for a
   rolling 30-day window.

**Sign handling is asymmetric, deliberately.** A CSW is forced negative (`-abs`) because a
WITHDRAWAL is never income, so its sign carries no information. A CSD **keeps the broker's
sign**: a negative deposit is a clawed-back ACH, and `CashTransfer.is_income`'s `amount > 0`
guard exists precisely to reject it (pinned by
`packages/common/tests/test_account_types.py::test_cash_transfer_negative_amount_deposit_is_not_income`).
`abs()`-ing a deposit would resurrect the clawback as new money to allocate.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (insert after the `get_symbol_margin_info` added in Task 32; also adds the shared `_fetch_activities()` helper and switches `get_balance_history`'s inline CSD/CSW loop onto it)
- Test: `tests/test_alpaca_cash_transfers.py`

- [ ] **Step 1: Write the failing test**

`tests/test_alpaca_cash_transfers.py`:
```python
"""AlpacaAccount.get_cash_transfers against a mocked activities endpoint.

Deposits (CSD) and withdrawals (CSW) come from /account/activities/<TYPE>;
dividends come through the existing get_dividends(), which itself reads
/account/activities/DIV and nets out DIVNRA tax withholding.

No live API call: client.get is a MagicMock with a routing side_effect.
"""
from datetime import date
from unittest.mock import MagicMock

from ba2_trade_platform.core.account_types import (
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account(activities):
    """activities: {"CSD": [...], "CSW": [...], "DIV": [...], "DIVNRA": [...]}"""
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}

    def _get(path, params=None):
        for key in ("DIVNRA", "DIV", "CSD", "CSW"):
            if path.endswith("/" + key):
                return activities.get(key, [])
        return []

    acct.client.get.side_effect = _get
    return acct


def _by_type(transfers):
    return {t.event_type: t for t in transfers}


def test_a_deposit_becomes_a_positive_income_event_keyed_by_the_broker_activity_id():
    acct = _bare_account({"CSD": [{"id": "act-1", "date": "2026-08-01",
                                   "net_amount": "1000", "description": "ACH IN"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DEPOSIT]

    assert ev.external_id == "act-1"
    assert ev.event_date == date(2026, 8, 1)
    assert ev.amount == 1000.0
    assert ev.symbol is None
    assert ev.is_income is True


def test_the_external_id_is_the_real_alpaca_activity_id_not_a_synthesised_one():
    """Alpaca non-trade activity ids look like <17 digits>::<uuid>; the ledger
    upserts on (account_id, external_id), so it must be carried through verbatim."""
    broker_id = "20260801000000000::9b8e1b4e-1a2f-4c3d-9e5a-6f7a8b9c0d1e"
    acct = _bare_account({"CSD": [{"id": broker_id, "date": "2026-08-01",
                                   "net_amount": "1000"}]})

    assert acct.get_cash_transfers()[0].external_id == broker_id


def test_a_withdrawal_is_negative_and_is_not_income():
    acct = _bare_account({"CSW": [{"id": "act-2", "date": "2026-08-05",
                                   "net_amount": "-250"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_WITHDRAWAL]

    assert ev.amount == -250.0
    assert ev.is_income is False


def test_a_withdrawal_reported_with_a_positive_amount_is_still_negated():
    """Do not depend on the broker's sign convention for CSW."""
    acct = _bare_account({"CSW": [{"id": "act-2", "date": "2026-08-05",
                                   "net_amount": "250"}]})

    assert _by_type(acct.get_cash_transfers())[CASH_TRANSFER_WITHDRAWAL].amount == -250.0


def test_a_reversed_deposit_keeps_its_negative_sign_and_is_not_income():
    """A clawed-back ACH arrives as a CSD with a NEGATIVE net_amount.

    CashTransfer.is_income guards this with ``amount > 0`` (pinned by
    packages/common/tests/test_account_types.py), so the adapter must NOT
    abs() a deposit -- that would resurrect the clawback as new money.
    """
    acct = _bare_account({"CSD": [{"id": "act-6", "date": "2026-08-07",
                                   "net_amount": "-1000"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DEPOSIT]

    assert ev.amount == -1000.0
    assert ev.is_income is False


def test_a_dividend_carries_its_payer_symbol_and_a_stable_external_id():
    acct = _bare_account({"DIV": [{"id": "act-3", "symbol": "AAPL",
                                   "date": "2026-08-10", "net_amount": "12.34"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DIVIDEND]

    assert ev.symbol == "AAPL"
    assert ev.amount == 12.34
    assert ev.event_date == date(2026, 8, 10)
    assert ev.external_id == "DIV:AAPL:2026-08-10"
    assert ev.is_income is True


def test_a_dividend_amount_is_net_of_the_nra_tax_withholding():
    acct = _bare_account({
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "100.00"}],
        "DIVNRA": [{"symbol": "AAPL", "date": "2026-08-10", "net_amount": "-15.00"}],
    })

    assert _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DIVIDEND].amount == 85.0


def test_a_dividend_without_a_payer_symbol_is_skipped_rather_than_keyed_on_none():
    """DIV:None:<date> is a fabricated identity; the ledger key must be real."""
    acct = _bare_account({"DIV": [{"id": "act-3", "symbol": None,
                                   "date": "2026-08-10", "net_amount": "12.34"}]})

    assert acct.get_cash_transfers() == []


def test_all_three_activity_kinds_come_back_from_one_call():
    acct = _bare_account({
        "CSD": [{"id": "act-1", "date": "2026-08-01", "net_amount": "1000"}],
        "CSW": [{"id": "act-2", "date": "2026-08-05", "net_amount": "-250"}],
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"}],
    })

    assert len(acct.get_cash_transfers()) == 3


def test_a_dividend_id_can_never_collide_with_a_cash_transfer_id():
    """Two id spaces: CSD/CSW carry the broker's own id, dividends a DIV: key.

    Worst case -- the broker hands the DIV activity the very id we would have
    synthesised -- the two events must still be distinct ledger rows.
    """
    acct = _bare_account({
        "CSD": [{"id": "DIV:AAPL:2026-08-10", "date": "2026-08-10", "net_amount": "1000"}],
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"}],
    })

    ids = [t.external_id for t in acct.get_cash_transfers()]

    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_resyncing_the_same_window_yields_the_same_external_ids():
    """The (account_id, external_id) upsert key must be stable across calls."""
    payload = {
        "CSD": [{"id": "act-1", "date": "2026-08-01", "net_amount": "1000"}],
        "CSW": [{"id": "act-2", "date": "2026-08-05", "net_amount": "-250"}],
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"}],
    }
    acct = _bare_account(payload)

    first = [t.external_id for t in acct.get_cash_transfers()]
    second = [t.external_id for t in acct.get_cash_transfers()]

    assert first == second == ["act-1", "act-2", "DIV:AAPL:2026-08-10"]


def test_the_date_window_is_passed_to_the_broker_as_after_and_until():
    acct = _bare_account({"CSD": []})

    acct.get_cash_transfers(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))

    csd_call = next(c for c in acct.client.get.call_args_list if c[0][0].endswith("/CSD"))
    assert csd_call[0][1] == {"after": "2026-08-01", "until": "2026-08-31"}


def test_an_activity_with_no_usable_date_is_skipped_rather_than_guessed():
    acct = _bare_account({"CSD": [{"id": "act-1", "date": None, "net_amount": "1000"},
                                  {"id": "act-9", "date": "2026-08-02", "net_amount": "5"}]})

    transfers = acct.get_cash_transfers()

    assert [t.external_id for t in transfers] == ["act-9"]


def test_an_activity_with_no_usable_amount_is_skipped_rather_than_zeroed():
    acct = _bare_account({"CSD": [{"id": "act-1", "date": "2026-08-01", "net_amount": None},
                                  {"id": "act-9", "date": "2026-08-02", "net_amount": "5"}]})

    assert [t.external_id for t in acct.get_cash_transfers()] == ["act-9"]


def test_a_failing_activities_endpoint_yields_an_empty_list_not_an_exception():
    """This seam does NOT distinguish failure from emptiness -- it logs and returns []."""
    acct = _bare_account({})
    acct.client.get.side_effect = Exception("503 service unavailable")

    assert acct.get_cash_transfers() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_alpaca_cash_transfers.py -v`

Expected: FAIL (13 failed, 2 passed) — the base returns `[]`, so the first test fails
with `KeyError: 'DEPOSIT'`. The 2 that pass are the vacuous ones whose assertion is
literally `== []` (the no-symbol dividend and the failing endpoint); they only become
meaningful once the method is real.

- [ ] **Step 3: Write minimal implementation**

Insert immediately after the `_safe_float` added in Task 32 (module constant near the
imports; `_fetch_activities` is shared with `get_balance_history`, see Step 3b):
```python
# Namespace of the SYNTHETIC external_id get_cash_transfers() mints for dividends,
# whose broker activity id get_dividends() drops. Keeps the dividend key space
# disjoint from the verbatim CSD/CSW broker ids sharing the same upsert column.
_DIVIDEND_KEY_PREFIX = "DIV:"


    def _fetch_activities(self, act_type: str,
                          params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Raw non-trade activities of one type, always a list, [] on failure.

        TradingClient exposes no get_account_activities, so every caller here
        (get_cash_transfers, get_balance_history) goes through the raw REST get()
        and has to normalise the single-object / None responses the same way.
        One activity type failing never aborts the others.
        """
        try:
            raw = self.client.get(f"/account/activities/{act_type}", params or None)
        except Exception as e:
            # Loud: both callers silently degrade (an empty ledger window, a
            # transfer-blind P/L) rather than surfacing this to the user.
            logger.error(f"[Account {self.id}] Could not fetch {act_type} activities: {e}",
                         exc_info=True)
            return []
        if isinstance(raw, list):
            return raw
        return [raw] if raw else []

    def get_cash_transfers(self, start_date=None, end_date=None) -> List[CashTransfer]:
        """Deposits, withdrawals and dividends over a window, from the activities API.

        Reuses the request idiom of get_balance_history and get_dividends
        (raw REST get() on /account/activities/<TYPE> with after/until), shared
        through _fetch_activities().

        external_id is the (account_id, external_id) idempotency key of
        portfolio_income_event, so it must be stable across re-syncs. CSD/CSW
        activities carry the broker's own id and it is passed through verbatim;
        get_dividends() deliberately drops it (it nets DIVNRA tax withholding
        into the amount, which is the number we want), so dividends get the
        stable synthetic key DIV:<symbol>:<YYYY-MM-DD> -- unique per payer per
        pay date, and namespaced so it cannot be mistaken for a broker id.

        event_date is the ACTIVITY date, not the T+1 settled date that
        get_balance_history shifts to. That shift exists to line a transfer up
        with the equity-curve day it moved equity (a P/L attribution concern);
        the ledger instead answers "what money arrived", and the shift target
        depends on which portfolio-history days happen to be in the window, so
        it would make event_date wobble between syncs.

        Signs are only normalised where that cannot destroy information: a CSW
        is forced negative (a WITHDRAWAL is never income, so its sign carries
        nothing), but a CSD keeps the broker's sign, because a NEGATIVE deposit
        is a clawed-back ACH and CashTransfer.is_income's ``amount > 0`` guard
        exists precisely to reject it.

        Returns [] and logs on failure -- this seam does NOT distinguish a broker
        outage from a genuinely empty window.
        """
        if not self._check_authentication():
            return []

        params: Dict[str, Any] = {}
        if start_date:
            params["after"] = start_date.isoformat()
        if end_date:
            params["until"] = end_date.isoformat()

        def _as_date(raw):
            try:
                return datetime.fromisoformat(str(raw)[:10]).date()
            except (ValueError, TypeError):
                return None

        transfers: List[CashTransfer] = []

        for act_type, event_type in (("CSD", CASH_TRANSFER_DEPOSIT),
                                     ("CSW", CASH_TRANSFER_WITHDRAWAL)):
            for act in self._fetch_activities(act_type, params):
                event_date = _as_date(act.get('date') or act.get('transaction_time'))
                if event_date is None:
                    logger.warning(f"[Account {self.id}] Skipping {act_type} activity "
                                   f"with no usable date: {act}")
                    continue
                amount = self._safe_float(act.get('net_amount'))
                if amount is None:
                    logger.warning(f"[Account {self.id}] Skipping {act_type} activity "
                                   f"with no usable amount: {act}")
                    continue
                external_id = str(act.get('id')
                                  or f"{act_type}:{event_date.isoformat()}:{amount}")
                if external_id.startswith(_DIVIDEND_KEY_PREFIX):
                    # Unreachable with real Alpaca ids (<17 digits>::<uuid>), but
                    # external_id is an UPSERT key: a broker id that shadowed a
                    # synthetic dividend key would silently merge two different
                    # events into one ledger row. Namespace it instead of hoping.
                    external_id = f"{act_type}:{external_id}"
                transfers.append(CashTransfer(
                    external_id=external_id,
                    event_date=event_date,
                    event_type=event_type,
                    amount=amount if event_type == CASH_TRANSFER_DEPOSIT else -abs(amount),
                    symbol=None,
                    description=act.get('description'),
                ))

        for div in self.get_dividends(start_date=start_date, end_date=end_date):
            event_date = _as_date(div.get('date'))
            if event_date is None:
                logger.warning(f"[Account {self.id}] Skipping dividend with no usable date: {div}")
                continue
            symbol = div.get('symbol')
            if not symbol:
                # DIV:None:<date> would be a fabricated identity, and external_id
                # is an upsert key -- drop it loudly rather than key on a guess.
                logger.warning(f"[Account {self.id}] Skipping dividend with no payer "
                               f"symbol (no stable external_id): {div}")
                continue
            amount = self._safe_float(div.get('amount'))
            if amount is None:
                logger.warning(f"[Account {self.id}] Skipping dividend with no usable "
                               f"amount: {div}")
                continue
            transfers.append(CashTransfer(
                external_id=f"{_DIVIDEND_KEY_PREFIX}{symbol}:{event_date.isoformat()}",
                event_date=event_date,
                event_type=CASH_TRANSFER_DIVIDEND,
                amount=amount,
                symbol=symbol,
                description=None,
            ))

        logger.debug(f"[Account {self.id}] Retrieved {len(transfers)} cash transfers")
        return transfers
```

- [ ] **Step 3b: Consolidate `get_balance_history` onto the shared helper**

Its inline CSD/CSW fetch becomes (one `try/except` per activity type instead of one
around both, so a CSD outage no longer skips CSW entirely):
```python
            transfer_by_date = {}
            for act_type in ['CSD', 'CSW']:
                for act in self._fetch_activities(act_type):
                    act_date = str(act.get('date', ''))[:10]
                    amount = self._safe_float(act.get('net_amount'))
                    if amount is None:
                        continue
                    transfer_by_date[act_date] = transfer_by_date.get(act_date, 0.0) + amount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_alpaca_cash_transfers.py -v`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**
```bash
git add ba2_trade_platform/modules/accounts/AlpacaAccount.py tests/test_alpaca_cash_transfers.py
git commit -m "feat(alpaca): get_cash_transfers() from CSD/CSW activities plus dividends"
```

---

### Task 34: Fix `TradeActions.py:1493` — `IncreaseInstrumentShareAction` has never worked

`self.account.get_account_info().get('buying_power', 0)` calls `.get()` on Alpaca's
pydantic `TradeAccount`, which raises `AttributeError`. It is **not** silently
swallowed as folklore has it: the `except` at `:1550` calls
`absorb_if_benign(e, InstanceNotFound)`, which re-raises anything that is not an
`InstanceNotFound`, so `execute()` blows up.

Fixing that line exposes a **second** crash six lines later in the same block:
`create_order_record()` already persists the row and returns its **integer id**
(see `SellAction`, which uses it correctly), but this action then feeds that int
back into `add_instance(order)` — `sqlalchemy.orm.exc.UnmappedInstanceError:
Class 'builtins.int' is not mapped`. Both must go, or the action still cannot
produce an order and the regression test cannot be green.

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py:1492-1493`
- Modify: `packages/common/ba2_common/core/TradeActions.py:1513-1535`
- Test: `packages/common/tests/test_increase_instrument_share_buying_power.py`

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_increase_instrument_share_buying_power.py`:
```python
"""IncreaseInstrumentShareAction on an ALPACA-SHAPED account.

TradeActions.py:1493 called .get('buying_power', 0) on the result of
get_account_info(), which on Alpaca is a pydantic TradeAccount with no .get().
absorb_if_benign() re-raises anything that is not an InstanceNotFound, so the
AttributeError escaped execute() and this action has never once produced an order
on Alpaca. The fix routes the read through the broker-agnostic
get_account_snapshot() seam.
"""
from uuid import uuid4

from alpaca.trading.enums import AccountStatus
from alpaca.trading.models import TradeAccount

from ba2_common.core import instance_resolver
from ba2_common.core.TradeActions import IncreaseInstrumentShareAction
from ba2_common.core.interfaces.AccountInterface import AccountInterface


class _AlpacaShapedAccount(AccountInterface):
    """get_account_info() returns a real pydantic TradeAccount, exactly like Alpaca.
    Every other abstract method is a stub purely to satisfy ABC instantiation."""

    def __init__(self, id, buying_power="50000"):
        self.id = id
        self._buying_power = buying_power
        self._settings_cache = None

    def get_account_info(self):
        return TradeAccount(id=uuid4(), account_number="PA1", status=AccountStatus.ACTIVE,
                            cash="10000", equity="60000", buying_power=self._buying_power,
                            non_marginable_buying_power="10000", multiplier="2",
                            long_market_value="50000", short_market_value="0")

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        return 100.0

    def _get_instrument_current_price_impl(self, *a, **k): return 100.0
    def _submit_order_impl(self, *a, **k): return None
    def adjust_sl(self, *a, **k): return None
    def adjust_tp(self, *a, **k): return None
    def adjust_tp_sl(self, *a, **k): return None
    def cancel_order(self, *a, **k): return None
    def get_balance(self): return 60000.0
    def get_balance_history(self, *a, **k): return []
    def get_dividends(self, *a, **k): return []
    def get_filled_trades(self, *a, **k): return []
    def get_order(self, *a, **k): return None
    def get_orders(self, status=None): return []
    def get_positions(self): return []
    def modify_order(self, *a, **k): return None
    def refresh_orders(self, *a, **k): return True
    def refresh_positions(self, *a, **k): return True
    def symbols_exist(self, symbols): return {}


class _FakeExpert:
    settings = {"max_virtual_equity_per_instrument_percent": 20.0}

    def get_virtual_balance(self):
        return 10000.0


class _FakeResolver:
    def get_expert_instance(self, expert_id): return _FakeExpert()
    def get_account_instance(self, account_id): return None
    def get_account_instance_from_transaction(self, transaction): return None


def _action(account):
    action = IncreaseInstrumentShareAction.__new__(IncreaseInstrumentShareAction)
    action.instrument_name = "AAPL"
    action.account = account
    action.order_recommendation = None
    action.existing_order = None
    action.expert_recommendation = type("Rec", (), {"instance_id": 42, "id": 7})()
    action.target_percent = 15.0
    action.submit_to_broker = False
    return action


def _run(action):
    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_FakeResolver())
    try:
        return action.execute()
    finally:
        instance_resolver.set_instance_resolver(previous)


def test_increase_share_creates_an_order_on_an_alpaca_shaped_account():
    """15% of a 10000 virtual equity at a 100 price, flat to start = 15 shares."""
    result = _run(_action(_AlpacaShapedAccount(1)))

    assert result["success"] is True, result["message"]
    assert result["data"]["quantity"] == 15.0
    assert result["data"]["side"] == "BUY"
    assert result["data"]["order_id"] is not None


def test_increase_share_clamps_the_order_to_the_available_buying_power():
    """Buying power of 500 cannot fund a 1500 target: 5 shares, not 15."""
    result = _run(_action(_AlpacaShapedAccount(1, buying_power="500")))

    assert result["success"] is True, result["message"]
    assert result["data"]["quantity"] == 5.0


def test_increase_share_refuses_to_size_when_buying_power_is_unknown():
    """No fabricated balance: an unreadable buying power blocks the order."""
    class _Blind(_AlpacaShapedAccount):
        def get_account_info(self):
            return None

    result = _run(_action(_Blind(1)))

    assert result["success"] is False
    assert "buying power" in result["message"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_increase_instrument_share_buying_power.py -v`

Expected: FAIL — `AttributeError: 'TradeAccount' object has no attribute 'get'`, raised from `TradeActions.py:1493` and re-raised through `failure_modes.py:150`

- [ ] **Step 3: Write minimal implementation**

In `packages/common/ba2_common/core/TradeActions.py`, replace lines 1492-1493:
```python
            # Check available balance
            account_balance = self.account.get_account_info().get('buying_power', 0)
```
with:
```python
            # Check available balance. get_account_info() is a pydantic TradeAccount on
            # Alpaca (no .get()), a dict on IBKR/TastyTrade and None on auth failure, so
            # read it through the broker-agnostic snapshot seam instead. buying_power is
            # None when the broker did not publish one -- refuse to size rather than
            # substituting a number (platform rule: no fallback values for balances).
            snapshot = self.account.get_account_snapshot()
            account_balance = snapshot.buying_power
            if account_balance is None:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message=f"Buying power unavailable for account {self.account.id}",
                    data={},
                )
```

Then replace the order-creation block (lines 1513-1535 as of HEAD):
```python
            # Create market order
            order = self.create_order_record(
                side=side,
                quantity=additional_qty,
                order_type="market"
            )
            
            if not order:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )
            
            # Save order to database
            order_id = add_instance(order)
            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to save order to database",
                    data={}
                )
```
with:
```python
            # Create market order. create_order_record() ALREADY persists the row and
            # returns its integer id (see SellAction, which uses it correctly) -- the old
            # code fed that int back into add_instance(), raising UnmappedInstanceError
            # ("Class 'builtins.int' is not mapped") on every single invocation.
            order_id = self.create_order_record(
                side=side,
                quantity=additional_qty,
                order_type="market"
            )

            if not order_id:
                return self.create_and_save_action_result(
                    action_type=ExpertActionType.INCREASE_INSTRUMENT_SHARE.value,
                    success=False,
                    message="Failed to create order record",
                    data={}
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_increase_instrument_share_buying_power.py -v`
Expected: PASS (3 passed)

Run: `venv/bin/python -m pytest packages/common/tests/test_trade_actions_account_interface_inmem.py -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add packages/common/ba2_common/core/TradeActions.py packages/common/tests/test_increase_instrument_share_buying_power.py
git commit -m "fix(actions): IncreaseInstrumentShareAction reads buying power via get_account_snapshot()"
```

---

### Task 35: Fractional-aware submission in `AlpacaAccount._submit_order_impl`

> **The fractional gate is now live, and it degrades silently.** Task 31 wired
> `supports_fractional` to `client.get_account_configurations().fractional_trading` — a SECOND
> network call, wrapped so a failure cannot void the money, falling back to `False`. So a transient
> outage makes a fractional-capable account look non-fractional and quietly routes to whole-share
> sizing. That is the correct conservative direction and this task should keep it.
>
> Decide deliberately whether you need to distinguish "known not fractional" from "couldn't ask". If
> you do, `supports_fractional` has to become tri-state and Task 31's method changes with it. If you
> don't — and silently sizing whole shares for one run is an acceptable cost — say so explicitly, so
> the next reader knows it was weighed rather than missed.


Read `AlpacaAccount.py:851-1000` before touching this (the line numbers in the
original draft, `:832-980`, drifted by ~19 as Tasks 31-34 landed). The relevant fact
is at `:951-959`: `good_for` is mapped through a `tif_map` dict whose keys are
`'day','gtc','opg','ioc','fok','cls'`, and the lookup is
`time_in_force = tif_map.get(good_for_value, TimeInForce.GTC)` — so a `good_for` of
`None` or anything unrecognised silently becomes **GTC**. Alpaca rejects fractional
quantities on GTC and on any non-market order type, so a fractional order built
without an explicit `good_for='day'` is guaranteed to be refused by the broker. The
allocation actions in `TradeActions.py` build their orders with `order_type="market"`
and no `good_for` at all, so this is the live path, not a hypothetical one.

Rather than trusting every future caller to set `good_for` correctly, the adapter
enforces it: a fractional MARKET order is forced to DAY.

**Deviation from the original draft (implemented as described here).** The draft
`raise`d on a fractional non-market order. It does NOT raise: it falls back ONCE to
`floor(qty)` whole shares and submits the same order type. Raising is wrong for the
real producer of fractional non-market orders — a protective TP/SL leg whose quantity
was inherited from a fractional position (`AccountInterface.submit_order` syncs a
leg's quantity to its parent entry). Raising there marks the leg ERROR and leaves the
position with **no** exit; flooring protects the whole-share part of it. Flooring never
rounds up, so it can only under-fill, never overspend the target. The floored quantity
is written back to the row so the ledger matches what the broker actually received.

Converting a fractional non-market order to MARKET was considered and rejected: it
would silently discard the caller's price protection and could fill at any price.

A floor of 0 leaves nothing to send. That is a **SKIP, not a failure** — no broker
rejected anything and the account is healthy — so the row is marked `CANCELED`
(terminal, non-error) with the reason in `comment`, no broker round-trip is made, and
`_submit_order_impl` returns `None` so `submit_order` does not chain TP/SL legs onto a
position that was never opened. There is no `OrderStatus.SKIPPED`; adding one would
ripple through every `OrderStatus` consumer for no behavioural gain, so `CANCELED` +
a `skipped:` comment prefix carries it.

**Decision on the routed tri-state question: `supports_fractional` stays BOOLEAN.**
"Couldn't ask" and "known not fractional" are deliberately collapsed to `False`,
because (a) the only defensible handling of an unknown is identical to `False`, so a
tri-state would add a type and change Task 31's signature to produce the same
behaviour; (b) the failure is one-directional — a degraded read can only *suppress*
fractional sizing (a smaller, always-legal whole-share order), never wrongly enable
it; and (c) it is not a single point of truth anyway, since a fractional order also
requires the per-symbol `fractionable` flag from `get_symbol_margin_info()`, an
independent read from a different endpoint. What a tri-state would really have bought
is *visibility*, so the swallowed `logger.debug` in Task 31's `get_account_snapshot`
was raised to a `logger.warning` naming the consequence. The silent debug line was the
actual defect, not the type.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/AlpacaAccount.py` — add `import math`; add the
  `_record_fractional_adjustment` helper just above `_submit_order_impl`; insert the fractional
  block immediately after the local `from ...core.types import OrderType as CoreOrderType`
  line inside `_submit_order_impl`; raise the `get_account_snapshot` fractional-read log
  from `debug` to `warning`.
- Test: `tests/test_alpaca_fractional_submission.py`

- [x] **Step 1: Write the failing test**

See `tests/test_alpaca_fractional_submission.py` (10 tests). Structure:

- `_alpaca_response()` returns a `SimpleNamespace`, **not** a `MagicMock`:
  `alpaca_order_to_tradingorder` reads the response with `getattr(..., None)` into a
  pydantic `TradingOrder`, and MagicMock attributes fail validation where a
  SimpleNamespace's missing attributes correctly fall back to `None`.
- `_bare_account()` is `object.__new__(AlpacaAccount)` with `id`, a MagicMock `client`
  (so `_check_authentication()` passes and nothing reaches the network),
  `_margin_info_cache`, and a real `_balance_cache_lock` — the post-submit path calls
  `invalidate_balance_cache()`, and without the lock the whole submission would land in
  the `except` block and be marked ERROR while the assertions still passed.

Coverage:
1. `test_fractional_market_order_goes_out_as_day_even_when_good_for_is_unset`
2. `test_fractional_market_order_overrides_an_explicit_gtc`
3. `test_whole_share_market_order_keeps_the_existing_gtc_default` *(regression guard — passes before the change)*
4. `test_whole_share_order_with_an_explicit_day_still_goes_out_as_day` *(regression guard)*
5. `test_fractional_quantity_is_never_sent_on_a_limit_order`
6. `test_fractional_limit_order_is_retried_once_at_floor_qty` — one submit call, `qty == 1.0`, DB row updated to 1.0
7. `test_fractional_stop_limit_leg_also_floors` — proves it is not limit-specific
8. `test_whole_share_limit_order_is_untouched_by_the_fractional_path` *(regression guard)*
9. `test_fractional_limit_order_that_floors_to_zero_is_skipped_not_failed` — no broker call, returns `None`, status `CANCELED` (not `ERROR`), comment contains `skipped` and `fractional`
10. `test_a_skipped_order_does_not_keep_the_fractional_quantity_as_if_it_were_live` — no `broker_order_id`

- [x] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_alpaca_fractional_submission.py -v`

Actual: **7 failed, 3 passed** — `assert <TimeInForce.GTC: 'gtc'> == <TimeInForce.DAY: 'day'>`
for the two TIF tests, `fractional qty 1.5 reached Alpaca` / `assert 4.25 == 4.0` for the
floor tests, and `Expected 'submit_order' to not have been called. Called 1 times.` for the
skip test. The 3 that passed are the whole-share regression guards (3, 4, 8), which are
supposed to pass both before and after.

- [x] **Step 3: Write minimal implementation**

Add `import math` to the module imports, and this helper immediately above
`_submit_order_impl`:
```python
    def _record_fractional_adjustment(self, trading_order: TradingOrder,
                                      quantity: Optional[float], reason: str) -> None:
        """Persist a submission-time fractional-quantity adjustment onto the order row.

        ``quantity`` is the whole-share quantity actually being sent, or ``None`` when
        nothing is being sent at all (the SKIP case: flooring left 0 shares). A skip is
        marked CANCELED -- terminal, but deliberately NOT ERROR: no broker rejected
        anything and the account is healthy, there was simply no whole share left to
        trade, so it must not show up as a broker failure in the UI or the logs.

        The reason is appended to ``comment`` (same convention as
        ``_handle_order_submit_error``) so it is legible in the Pending Orders UI and not
        only in the log.
        """
        fresh_order = get_instance(TradingOrder, trading_order.id)
        if not fresh_order:
            logger.error(
                f"Could not find order {trading_order.id} to record fractional adjustment: {reason}")
            return
        if quantity is None:
            fresh_order.status = OrderStatus.CANCELED
        else:
            fresh_order.quantity = quantity
        fresh_order.comment = (
            f"{fresh_order.comment} | {reason}" if fresh_order.comment else reason)[:500]
        update_instance(fresh_order)
```

Then, inside `_submit_order_impl`, find:
```python
            # Import OrderType enum from core.types to compare values
            from ...core.types import OrderType as CoreOrderType
```
and insert this block immediately after the import line (before the bracket-order comment):
```python

            # ---- Fractional quantities -----------------------------------------------
            # Alpaca accepts a fractional quantity ONLY on a DAY MARKET order. Two traps:
            #
            #  1. tif_map above resolves an unknown/absent good_for to GTC, and the
            #     allocation actions build their orders with no good_for at all -- so a
            #     fractional order would go out GTC and be refused by the broker. Force
            #     DAY rather than trusting every future caller to remember.
            #  2. Every non-MARKET type (limit / stop / stop-limit / OCO) refuses
            #     fractional outright -- including a protective TP/SL leg whose quantity
            #     was inherited from a fractional position. Those fall back ONCE to
            #     floor(qty) whole shares: it is a guaranteed rejection otherwise, and
            #     flooring under-fills rather than overspending the target. The floored
            #     quantity is written back so the ledger matches what the broker got.
            #
            # A floor of 0 leaves nothing to send. That is a SKIP, not a failure --
            # nothing was rejected and nothing is wrong with the account -- so the row is
            # marked CANCELED with the reason, never ERROR.
            #
            # Sizing itself is NOT re-derived here: the allocation engine already decided
            # fractional-vs-whole (opt-in per run, gated on the broker's own per-symbol
            # `fractionable` flag) and did the rounding. This is submission-time
            # enforcement of a broker constraint only.
            quantity_value = float(trading_order.quantity or 0.0)
            if quantity_value != int(quantity_value):
                if order_type_value == CoreOrderType.MARKET.value.lower():
                    if time_in_force != TimeInForce.DAY:
                        logger.info(
                            f"Order {trading_order.id} ({trading_order.symbol}) has fractional "
                            f"qty={quantity_value}; forcing time_in_force DAY (was "
                            f"{time_in_force.value}) — Alpaca rejects fractional on any other TIF"
                        )
                        time_in_force = TimeInForce.DAY
                else:
                    whole_shares = float(math.floor(quantity_value))
                    reason = (f"fractional qty {quantity_value} is not accepted by Alpaca on a "
                              f"{order_type_value} order")
                    if whole_shares <= 0:
                        logger.warning(
                            f"Order {trading_order.id} ({trading_order.symbol}) skipped: "
                            f"{reason}, and flooring leaves 0 whole shares — nothing submitted"
                        )
                        self._record_fractional_adjustment(
                            trading_order, None,
                            f"skipped: {reason}; flooring leaves 0 whole shares")
                        return None
                    logger.warning(
                        f"Order {trading_order.id} ({trading_order.symbol}): {reason}; "
                        f"submitting {whole_shares} whole shares instead of {quantity_value}"
                    )
                    trading_order.quantity = whole_shares
                    self._record_fractional_adjustment(
                        trading_order, whole_shares,
                        f"{reason}; floored to {whole_shares} whole shares")
```

A negative quantity needs no special case: `math.floor` of a negative is more negative,
and the `<= 0` branch catches it as a skip. Quantities are validated positive upstream
by `_validate_trading_order` anyway.

Finally, in `get_account_snapshot`, raise the swallowed fractional-read log from
`logger.debug` to `logger.warning` and name the consequence (see the tri-state decision
above).

- [x] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_alpaca_fractional_submission.py -v`
Actual: **10 passed**

Run: `venv/bin/python -m pytest tests/test_accounts/test_alpaca_idempotency.py tests/test_accounts/test_broker_error_handling.py tests/test_alpaca_order_type_mapping.py tests/test_alpaca_options.py tests/test_alpaca_account_snapshot.py tests/test_alpaca_margin_info.py tests/test_alpaca_cash_transfers.py -v`
Actual: **78 passed** (proves whole-share submission and Tasks 31-33 are untouched)

Full suites: `tests` **1387** (1377 + 10 new), `packages/common/tests` **517**,
`packages/experts/tests` **484**, `packages/providers/tests` **196**.

- [x] **Step 5: Commit**
```bash
git add ba2_trade_platform/modules/accounts/AlpacaAccount.py tests/test_alpaca_fractional_submission.py
git commit -m "fix(alpaca): force DAY time-in-force on fractional market orders, floor fractional non-market"
```

---

**AS-LANDED AMENDMENT (Section D review fixes).** An adversarial review of the
landed Section D returned CHANGES REQUESTED. Six of the fixes changed a
CONTRACT, including four previously-passing tests that were pinning the old
behaviour (two of them pinning outright bugs). Sections E-G should treat the
list below, not the task text above, as the seam's behaviour. Final counts:
`tests` **1405**, `packages/common/tests` **534**, `packages/experts/tests`
**484**, `packages/providers/tests` **196**.

1. **`bp_factor` is 1.0 for EVERY symbol on a 1x account (Task 32).** `initial_rate`
   is now `0.5 if (marginable and multiplier > 1.0) else 1.0`. `Asset.marginable`
   describes the SECURITY; Alpaca reports `multiplier="1"` for cash and
   limited-margin accounts (and drops a margin account to 1 below $2,000 equity),
   where `buying_power == cash` and nothing is lent. The old 0.5 let the engine's
   `sum(notional * bp_factor) <= available_buying_power` test approve TWICE the
   notional such an account can pay for. 2:1 and 4:1 are unchanged.
   *Test rewritten:* `test_marginable_symbol_in_a_cash_account_consumes_half` →
   `..._consumes_the_full_notional`, asserting 1.0. It was asserting the bug.

2. **`maintenance_margin_requirement` FLOORS the initial rate (Task 32).**
   `initial_rate = max(initial_rate, maint / 100.0)` when Alpaca publishes one. A
   100%-maintenance name that is still flagged marginable can no longer be sized as
   if half of it were borrowable.

3. **`_margin_info_cache` entries EXPIRE (Task 32).** Shape is now
   `{symbol: (fetched_at, MarginInfo)}` with `AlpacaAccount._MARGIN_INFO_CACHE_TTL`
   (24h), plus a new `clear_margin_info_cache()` for **Section F to call on an
   explicit Refresh**. The account object is process-wide, not per-request, and
   Alpaca revokes marginability / fractionability on individual names.

4. **`MarginInfo` is `@dataclass(frozen=True)` (Task 27).** Cached instances are
   returned by reference; derive changes with `dataclasses.replace()`.

5. **A dividend's `external_id` is `DIV:<broker activity id>` (Task 33).**
   `get_dividends()` now copies the activity `id` into `div_record`, and
   `get_cash_transfers()` keys on it, falling back to `DIV:<symbol>:<date>` only
   when there is none. The old symbol/date key upserted two DIV activities for one
   payer on one pay date into a single row. **Section F must land after this** —
   changing the key namespace once `portfolio_income_event` has rows would
   duplicate every synced dividend.
   *Tests rewritten:* `test_a_dividend_carries_its_payer_symbol_and_a_stable_external_id`
   → `test_a_dividend_is_keyed_by_the_broker_activity_id_when_there_is_one`;
   `test_resyncing_the_same_window_yields_the_same_external_ids` and
   `test_a_dividend_id_can_never_collide_with_a_cash_transfer_id` re-keyed. A
   dividend with no payer symbol is still dropped (attribution, not identity).

6. **The tolerant base normalises two AccountSnapshot contract rules (Task 28),**
   so **Section E's TastyTrade adapter does not have to**: `equity` /
   `net_liquidation` are mirrored when the broker publishes only one (a
   `portfolio_value`-only broker previously left `net_liquidation` None), and a
   positive `short_market_value` magnitude is negated.

7. **Both share actions FLOOR their quantity (Task 34).**
   `IncreaseInstrumentShareAction` uses `math.floor` and returns `success=False`
   at 0 — `max(1.0, round(...))` defeated the buying-power clamp entirely (0 BP
   emitted a BUY 1; $150 at $100/share emitted a BUY 2).
   `DecreaseInstrumentShareAction` uses
   `min(math.floor(reduction_value / price), abs(current_position_qty))` — `round()`
   plus a min-share clamp gated on `target_percent > 0` let a 2.6-share holding at
   a 0% target emit SELL 3, i.e. an oversell into a short taken off another
   expert's slice. `create_order_record` is annotated `-> Optional[int]`, which is
   what it always returned and what caused both double-save crashes.

8. **The fractional gate covers the wash-trade escape (Task 35).** A MARKET order
   with `use_complex_order` goes out as BRACKET/OTO, which Alpaca will not take
   fractional either, so it now takes the floor branch. The floor warning also
   names the consequence (`"{n} shares of the position are left uncovered"`).
   *Test renamed:* `test_fractional_limit_order_is_retried_once_at_floor_qty` →
   `..._is_floored_before_submission`; there is no retry, the quantity is
   pre-floored and submitted once.

**Left for Section F to decide (not changed here).** `CashTransfer.description`
is populated by the Alpaca adapter (the CSD/CSW activity text) but
`PortfolioIncomeEvent` has no column for it, so it is dropped at persist time.
Section F owns that table; the recommendation is to DROP the field from the
ledger and keep it on the value object — see the review report.

---

## Section E — TastyTrade trading surface

> **Depends on Section D**, which must already have landed: `ba2_common/core/account_types.py`
> (+ the in-tree alias shim) with `AccountSnapshot`, `CashTransfer`, `MarginInfo`, `OrderImpact`,
> `CASH_TRANSFER_*`, `MARGIN_SOURCE_*` (Task 27); the `get_account_snapshot` /
> `get_cash_transfers` / `get_symbol_margin_info` seams (Tasks 28-29); and
> `preview_order_impact` on `AccountInterface` (Task 30). Tasks 51-54 will not import until
> those exist.
>
> **TWO SDK TRAPS — read before writing any code in this section.**
> 1. `tastytrade.account.Account.place_order(self, session, order, dry_run: bool = True)` —
>    **`dry_run` DEFAULTS TO `True`** (`venv/lib/python3.12/site-packages/tastytrade/account.py:877-879`;
>    `place_complex_order` is the same at `:894-896`). A real submission that forgets the kwarg
>    silently places nothing. **Every** call site in this section passes `dry_run=` explicitly,
>    submit and preview alike.
> 2. `tastytrade.order.NewOrder.price_effect` is a **`@computed_field`** derived from the SIGN of
>    `price` (`venv/lib/python3.12/site-packages/tastytrade/order.py:264-276`): negative = debit,
>    with `abs()` applied by the `@field_serializer` on the way out. **Never set `price_effect`
>    by hand.** A BUY limit is a debit, so its `price` must be written NEGATIVE. Symmetrically,
>    `BuyingPowerEffect.change_in_buying_power` comes back SIGNED (negative for a buy, via the
>    `set_sign_for` validator at `order.py:381-393`) — always consume `OrderImpact.bp_cost`,
>    never the raw field.
>
> There is **no TastyTrade account in the live database**, so every test in this section runs
> against a mocked SDK. `tests/test_tastytrade_account.py` does not exist yet — Task 37 creates
> it, including the shared broker-double helpers that every later task reuses.
>
> Bump the version files once at the very end of the plan (Task 79), not per task.

---

### Task 36: Pin the broker SDK versions

`requirements.txt:3-4` lists a bare `alpaca-py` and a bare `tastytrade`. tastytrade 12.x is the
OAuth-only async rewrite; an unpinned upgrade would move the API out from under this whole
section.

**Files:**
- Modify: `requirements.txt:3-4`
- Test: `tests/test_broker_sdk_pins.py`

- [ ] **Step 1: Write the failing test**

```python
"""The two broker SDKs this platform writes directly against must be PINNED.

tastytrade 12.x is the OAuth-only async rewrite: `Account.place_order` became a
coroutine with a `dry_run` parameter that defaults to True, and `Session` moved to
`provider_secret`/`refresh_token`. An unpinned `tastytrade` line lets a routine
`pip install -r requirements.txt` move that API under TastyTradeAccount. alpaca-py
is pinned for the same reason (TradeAccount/Asset field shapes).
"""
from importlib.metadata import version
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def _pinned_versions():
    """Parse `name==version` lines out of requirements.txt, ignoring comments."""
    pins = {}
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, _, pinned = line.partition("==")
        pins[name.strip().lower()] = pinned.strip()
    return pins


def test_tastytrade_is_pinned_to_the_installed_version():
    assert _pinned_versions().get("tastytrade") == version("tastytrade")


def test_alpaca_py_is_pinned_to_the_installed_version():
    assert _pinned_versions().get("alpaca-py") == version("alpaca-py")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_broker_sdk_pins.py -v`

Expected: FAIL — both tests, with `AssertionError: assert None == '<installed version>'` (the
bare lines produce no `==` pin, so `.get()` returns `None`).

- [ ] **Step 3: Write minimal implementation**

First read back the exact installed versions so the pins are right on THIS machine:

```bash
venv/bin/python -c "from importlib.metadata import version; print('alpaca-py', version('alpaca-py')); print('tastytrade', version('tastytrade'))"
```

Then replace `requirements.txt` lines 3-4, which currently read exactly:

```
alpaca-py
tastytrade
```

with (substituting the two versions the command printed — at the time of writing
`0.43.2` and `12.0.2`):

```
alpaca-py==0.43.2
tastytrade==12.0.2  # pin: 12.x is the OAuth-only ASYNC rewrite (Account.place_order is a coroutine whose dry_run defaults to True). Do not float this line.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_broker_sdk_pins.py -v`

Expected: PASS — 2 passed.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/test_broker_sdk_pins.py
git commit -m "build: pin tastytrade and alpaca-py to the installed versions"
```

---

### Task 37: Read `is_test` through the interface default (and create the test module)

`TastyTradeAccount.py:58` reads `bool(self.settings.get("is_test", False))`. The `settings`
property seeds every *declared* key to `None`, so `.get(key, default)` never returns the default
for a never-saved key — and a legacy row holding the literal string `"None"` (the documented
`str(None)` write bug) coerces to `True`, silently pointing a production account at the
**sandbox**.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:53-72`
- Create: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for TastyTradeAccount against a MOCKED tastytrade SDK (12.0.2).

There is no TastyTrade account in the live database, so nothing here talks to a
broker. Broker responses are either REAL SDK pydantic objects (where a validator or
a sign convention is part of what is being tested) or SimpleNamespace stand-ins
(where the real model has 40+ required fields and the code only reads a handful).

Two SDK traps these tests exist to guard:
  * Account.place_order(session, order, dry_run=True) -- dry_run DEFAULTS TO TRUE
    (tastytrade/account.py:877). Real submissions must pass dry_run=False.
  * NewOrder.price_effect is a computed field derived from the SIGN of `price`
    (order.py:264-276): negative = debit. It must never be set by hand.
"""
import asyncio
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tastytrade.order import (
    BuyingPowerEffect,
    FeeCalculation,
    InstrumentType as TTInstrumentType,
    Leg,
    Message,
    OrderAction,
    OrderStatus as TTOrderStatus,
    OrderTimeInForce,
    OrderType as TTOrderType,
    PlacedOrder,
    PlacedOrderResponse,
    FillInfo,
)
from tastytrade.utils import PriceEffect

from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount


# ---------------------------------------------------------------------------
# SHARED BROKER DOUBLES  (used by every task in this module)
# ---------------------------------------------------------------------------

def _sync_run(coro):
    """Drive a coroutine to completion.

    Test stand-in for TastyTradeAccount._run_async, which in production hands the
    coroutine to a persistent background event loop. Tests that exercise _run_async
    ITSELF build a real loop instead (see _looped_account).
    """
    return asyncio.run(coro)


def _bare_account(settings=None):
    """A TastyTradeAccount with no __init__: no network, no DB settings lookup."""
    acct = object.__new__(TastyTradeAccount)
    acct.id = 1
    acct._authentication_error = None
    acct._session = SimpleNamespace(label="tasty-session")
    acct._account = SimpleNamespace(account_number="5WX00000", margin_or_cash="Margin",
                                    account_type_name="Individual")
    acct._loop = None
    acct._loop_thread = None
    acct._settings_cache = dict(settings or {})
    acct._run_async = _sync_run
    with TastyTradeAccount._CACHE_LOCK:
        TastyTradeAccount._GLOBAL_PRICE_CACHE[acct.id] = {}
    return acct


def _balances(**overrides):
    """Stand-in for tastytrade AccountBalance (the real model has 45 required fields)."""
    data = dict(
        cash_balance=Decimal("25000"),
        equity_buying_power=Decimal("50000"),
        derivative_buying_power=Decimal("25000"),
        long_equity_value=Decimal("75000"),
        short_equity_value=Decimal("0"),
        margin_equity=Decimal("100000"),
        maintenance_requirement=Decimal("18750"),
        net_liquidating_value=Decimal("100000"),
        cash_available_to_withdraw=Decimal("25000"),
        pending_cash=Decimal("0"),
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _tt_position(symbol="AAPL", quantity="10", direction="Long", average_open_price="140",
                 close_price="150", mark_price="155", multiplier=1,
                 instrument_type=TTInstrumentType.EQUITY, realized_day_gain="3"):
    """Stand-in for tastytrade CurrentPosition."""
    return SimpleNamespace(
        symbol=symbol,
        quantity=Decimal(quantity),
        quantity_direction=direction,
        average_open_price=Decimal(average_open_price),
        close_price=Decimal(close_price),
        mark_price=Decimal(mark_price),
        multiplier=multiplier,
        instrument_type=instrument_type,
        realized_day_gain=Decimal(realized_day_gain),
    )


def _placed_order(order_id=987654, symbol="AAPL", status=TTOrderStatus.RECEIVED,
                  order_type=TTOrderType.MARKET, action=OrderAction.BUY_TO_OPEN,
                  size="10", external_identifier=None, fills=None, price=None,
                  time_in_force=OrderTimeInForce.DAY):
    """A REAL tastytrade PlacedOrder -- the mapping code must survive its validators."""
    leg = Leg(instrument_type=TTInstrumentType.EQUITY, symbol=symbol,
              action=action, quantity=Decimal(size), fills=fills)
    return PlacedOrder(
        account_number="5WX00000",
        time_in_force=time_in_force,
        order_type=order_type,
        underlying_symbol=symbol,
        underlying_instrument_type=TTInstrumentType.EQUITY,
        status=status,
        cancellable=True,
        editable=False,
        edited=False,
        updated_at=datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc),
        received_at=datetime(2026, 8, 20, 14, 29, tzinfo=timezone.utc),
        legs=[leg],
        id=order_id,
        size=Decimal(size),
        price=price,
        external_identifier=external_identifier,
    )


def _fill(quantity="10", fill_price="150.25"):
    return FillInfo(fill_id="f-1", quantity=Decimal(quantity), fill_price=Decimal(fill_price),
                    filled_at=datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc))


def _placed_order_response(order, change_in_buying_power="-1500",
                           isolated_requirement="1500", total_fees="0.03",
                           warnings=None, errors=None):
    """A REAL PlacedOrderResponse. change_in_buying_power is SIGNED: a buy is negative."""
    bpe = BuyingPowerEffect(
        change_in_margin_requirement=Decimal("1500"),
        change_in_buying_power=Decimal(change_in_buying_power),
        current_buying_power=Decimal("10000"),
        new_buying_power=Decimal("8500"),
        isolated_order_margin_requirement=Decimal(isolated_requirement),
        is_spread=False,
        impact=Decimal("1500"),
        effect=PriceEffect.DEBIT,
    )
    return PlacedOrderResponse(
        buying_power_effect=bpe,
        order=order,
        fee_calculation=FeeCalculation(
            regulatory_fees=Decimal("0.01"), clearing_fees=Decimal("0.02"),
            commission=Decimal("0"), proprietary_index_option_fees=Decimal("0"),
            total_fees=Decimal(total_fees)),
        warnings=[Message(code="w", message=w) for w in (warnings or [])],
        errors=[Message(code="e", message=e) for e in (errors or [])],
    )


class _FakeEquity:
    """Stand-in for tastytrade.instruments.Equity.

    Only the two fields the account code reads, plus build_leg -- which returns a
    REAL SDK Leg so NewOrder validation is genuinely exercised.
    """

    def __init__(self, symbol, is_fractional_quantity_eligible=True):
        self.symbol = symbol
        self.instrument_type = TTInstrumentType.EQUITY
        self.is_fractional_quantity_eligible = is_fractional_quantity_eligible

    def build_leg(self, quantity, action):
        return Leg(instrument_type=TTInstrumentType.EQUITY, symbol=self.symbol,
                   quantity=quantity, action=action)


# ---------------------------------------------------------------------------
# Sandbox flag
# ---------------------------------------------------------------------------

def test_is_sandbox_with_string_none_stored_returns_false():
    """A legacy row holding the literal string "None" must NOT select the sandbox.

    bool("None") is True, so the old `self.settings.get("is_test", False)` would
    point a production account at TastyTrade's certification environment.
    """
    acct = _bare_account(settings={"is_test": "None"})
    assert acct._is_sandbox() is False


def test_is_sandbox_with_unsaved_setting_returns_interface_default():
    """A never-saved key is seeded to None by the settings property; the declared
    default (False) must still apply."""
    acct = _bare_account(settings={"is_test": None})
    assert acct._is_sandbox() is False


def test_is_sandbox_with_saved_true_returns_true():
    acct = _bare_account(settings={"is_test": True})
    assert acct._is_sandbox() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: FAIL — all three sandbox tests error with
`AttributeError: 'TastyTradeAccount' object has no attribute '_is_sandbox'`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, replace line 58, which currently
reads exactly:

```python
        is_test = bool(self.settings.get("is_test", False))
```

with:

```python
        is_test = self._is_sandbox()
```

Then insert this new method immediately **above** `def _connect(self):` (currently line 53):

```python
    def _is_sandbox(self) -> bool:
        """Whether this account targets TastyTrade's sandbox (certification) API.

        Read through ``get_setting_with_interface_default`` rather than
        ``self.settings.get('is_test', False)``. The settings property seeds every
        DECLARED key to ``None``, so ``.get(key, default)`` never returns the default
        for a never-saved key; and a legacy row holding the literal string ``"None"``
        (the str(None) write bug) coerces to ``True`` under ``bool()``, which would
        silently point a PRODUCTION account at the sandbox.
        ``get_setting_with_interface_default`` treats ``"None"`` as unset.
        """
        return bool(self.get_setting_with_interface_default("is_test", log_warning=False))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): read is_test via get_setting_with_interface_default"
```

---

### Task 38: Give `_run_async` a real timeout budget and a named error

`_run_async` (line 48-51) hardcodes `future.result(timeout=30)`. Every caller wraps it in
`except Exception: return []`, so a slow paginated call (a year of transactions, full order
history) surfaces as "no data" rather than as an error, and the log line does not say it timed
out.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:48-51`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# _run_async
# ---------------------------------------------------------------------------

def _looped_account():
    """A bare account with a REAL persistent event loop, for testing _run_async itself."""
    acct = object.__new__(TastyTradeAccount)
    acct.id = 7
    acct._authentication_error = None
    acct._session = SimpleNamespace(label="tasty-session")
    acct._account = SimpleNamespace(account_number="5WX00000")
    acct._settings_cache = {}
    acct._loop = asyncio.new_event_loop()
    acct._loop_thread = threading.Thread(target=acct._loop.run_forever, daemon=True)
    acct._loop_thread.start()
    return acct


def _stop_loop(acct):
    acct._loop.call_soon_threadsafe(acct._loop.stop)
    acct._loop_thread.join(timeout=5)


def test_run_async_returns_the_coroutine_result():
    acct = _looped_account()
    try:
        async def _work():
            return {"items": [1, 2, 3]}

        assert acct._run_async(_work()) == {"items": [1, 2, 3]}
    finally:
        _stop_loop(acct)


def test_run_async_raises_timeout_error_naming_the_budget():
    """A slow SDK call must fail as an ERROR that says it timed out, not silently
    bubble a bare concurrent.futures timeout that every caller turns into `[]`."""
    acct = _looped_account()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            acct._run_async(asyncio.sleep(5), timeout=0.05)
        assert "timed out after 0.05" in str(excinfo.value)
    finally:
        _stop_loop(acct)


def test_run_async_default_budget_exceeds_the_old_thirty_second_limit():
    """Paginated history calls routinely exceed 30s; the default must be generous."""
    assert TastyTradeAccount._ASYNC_TIMEOUT_SECONDS >= 120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k run_async`

Expected: FAIL — `test_run_async_raises_timeout_error_naming_the_budget` fails with
`TypeError: TastyTradeAccount._run_async() got an unexpected keyword argument 'timeout'`, and
`test_run_async_default_budget_exceeds_the_old_thirty_second_limit` fails with
`AttributeError: type object 'TastyTradeAccount' has no attribute '_ASYNC_TIMEOUT_SECONDS'`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the import block at the top
(after line 4, `from datetime import ...`):

```python
from concurrent.futures import TimeoutError as FutureTimeoutError
```

Replace the whole method at lines 48-51, which currently reads exactly:

```python
    def _run_async(self, coro):
        """Run an async coroutine on this account's persistent event loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)
```

with:

```python
    #: Wall-clock budget for ONE SDK call. The old hardcoded 30s was routinely
    #: exceeded by paginated calls (a full order history, a year of transactions),
    #: and because every caller wraps _run_async in `except Exception: return []`,
    #: a timeout surfaced as "the broker has no data" instead of as a failure.
    _ASYNC_TIMEOUT_SECONDS = 180

    def _run_async(self, coro, timeout: Optional[float] = None):
        """Run an async coroutine on this account's persistent event loop.

        Args:
            coro: the coroutine to drive.
            timeout: seconds to wait; ``None`` uses ``_ASYNC_TIMEOUT_SECONDS``.

        Raises:
            TimeoutError: naming the account and the budget, so the caller's
                ``logger.error`` says WHY it produced nothing. The pending future is
                cancelled first so the loop does not keep the request alive.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        limit = self._ASYNC_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            return future.result(timeout=limit)
        except FutureTimeoutError as e:
            future.cancel()
            raise TimeoutError(
                f"[Account {self.id}] TastyTrade call timed out after {limit}s"
            ) from e
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): configurable _run_async timeout with a named TimeoutError"
```

---

### Task 39: `get_account_info` must publish `buying_power`

`MarketExpertInterface._get_actual_available_balance`
(`packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:815`) probes
`buying_power` → `cash` → `cash_balance` → `equity_buying_power`, first hit wins.
`get_account_info` (line 126) publishes `cash_balance` but no `buying_power`, so the probe stops
at cash and the account's margin buying power is ignored.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:126-147`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_account_info
# ---------------------------------------------------------------------------

def test_get_account_info_publishes_buying_power_from_equity_buying_power():
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(return_value=_balances())

    info = acct.get_account_info()

    assert info["buying_power"] == 50000.0


def test_actual_available_balance_uses_margin_buying_power_not_cash():
    """The expert probe stops at the FIRST of buying_power/cash/cash_balance/
    equity_buying_power. Without a buying_power key it fell through to cash_balance
    and a margin account was sized as if it were a cash account."""
    from ba2_trade_platform.core.interfaces.MarketExpertInterface import MarketExpertInterface

    acct = _bare_account()
    acct._account.get_balances = AsyncMock(
        return_value=_balances(cash_balance=Decimal("25000"),
                               equity_buying_power=Decimal("50000")))

    assert MarketExpertInterface._get_actual_available_balance(acct) == 50000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k "account_info or available_balance"`

Expected: FAIL — `test_get_account_info_publishes_buying_power_from_equity_buying_power` fails
with `KeyError: 'buying_power'`, and `test_actual_available_balance_uses_margin_buying_power_not_cash`
fails with `assert 25000.0 == 50000.0`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, inside `get_account_info`, replace
the returned dict literal (currently lines 131-144) which reads exactly:

```python
            return {
                "account_number": self._account.account_number,
                "account_type": self._account.account_type_name,
                "net_liquidating_value": float(balances.net_liquidating_value),
                "cash_balance": float(balances.cash_balance),
                "equity_buying_power": float(balances.equity_buying_power),
                "long_equity_value": float(balances.long_equity_value),
                "short_equity_value": float(balances.short_equity_value),
                "margin_equity": float(balances.margin_equity),
                "maintenance_requirement": float(balances.maintenance_requirement),
                "pending_cash": float(balances.pending_cash),
                "cash_available_to_withdraw": float(balances.cash_available_to_withdraw),
                "supports_trading": False,
            }
```

with:

```python
            return {
                "account_number": self._account.account_number,
                "account_type": self._account.account_type_name,
                # `buying_power` MUST be present and MUST come first among the
                # spendable-balance keys: MarketExpertInterface._get_actual_available_balance
                # probes buying_power -> cash -> cash_balance -> equity_buying_power and
                # takes the FIRST hit. Without it the probe fell through to cash_balance
                # and margin buying power was silently ignored.
                "buying_power": float(balances.equity_buying_power),
                "net_liquidating_value": float(balances.net_liquidating_value),
                "cash_balance": float(balances.cash_balance),
                "equity_buying_power": float(balances.equity_buying_power),
                "derivative_buying_power": float(balances.derivative_buying_power),
                "long_equity_value": float(balances.long_equity_value),
                "short_equity_value": float(balances.short_equity_value),
                "margin_equity": float(balances.margin_equity),
                "maintenance_requirement": float(balances.maintenance_requirement),
                "pending_cash": float(balances.pending_cash),
                "cash_available_to_withdraw": float(balances.cash_available_to_withdraw),
                "supports_trading": False,
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 8 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): publish buying_power in get_account_info"
```

---

### Task 40: `get_positions` — equities only, `None` on fetch failure, real intraday numbers

Three defects in one method (line 149): it returns `EQUITY_OPTION` rows whose `market_value` is
multiplier-scaled (×100), which would fold option notionals into equity allocation weights; it
returns `[]` on a fetch failure, which reconciliation reads as "the broker holds nothing" (the
2026-07-03 incident mass-closed 8 real transactions on exactly that confusion); and it hardcodes
`change_today=0.0` (line 165) while filling `unrealized_intraday_pl` with `realized_day_gain`
(line 186), which is a different quantity entirely.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:149-197`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

def test_get_positions_excludes_equity_option_rows():
    """An option's market_value is multiplier-scaled (x100). Folding it in with
    equities would blow up every allocation weight."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", instrument_type=TTInstrumentType.EQUITY),
        _tt_position(symbol="AAPL  260918C00150000", multiplier=100,
                     instrument_type=TTInstrumentType.EQUITY_OPTION),
    ])

    positions = acct.get_positions()

    assert [p.symbol for p in positions] == ["AAPL"]


def test_get_positions_returns_none_when_the_fetch_fails():
    """None means FETCH FAILED, [] means genuinely flat. Reconciliation mass-closes
    real transactions if a broker outage is allowed to look like an empty book."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(side_effect=RuntimeError("connection reset"))

    assert acct.get_positions() is None


def test_get_positions_returns_empty_list_when_genuinely_flat():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[])

    assert acct.get_positions() == []


def test_get_positions_derives_change_today_from_the_previous_close():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", quantity="10", close_price="150", mark_price="155"),
    ])

    position = acct.get_positions()[0]

    assert position.change_today == pytest.approx((155.0 - 150.0) / 150.0)


def test_get_positions_intraday_pl_is_mark_minus_close_not_realized_day_gain():
    """realized_day_gain is CLOSED-out P&L for the day; unrealized_intraday_pl is
    the OPEN position's move since the previous close. They are different numbers."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", quantity="10", close_price="150", mark_price="155",
                     realized_day_gain="3"),
    ])

    position = acct.get_positions()[0]

    assert position.unrealized_intraday_pl == pytest.approx(50.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k get_positions`

Expected: FAIL — 4 of the 5 fail: the option row is present
(`assert ['AAPL', 'AAPL  260918C00150000'] == ['AAPL']`), the failure case returns `[]` not
`None`, `change_today` is `0.0`, and `unrealized_intraday_pl` is `3.0` not `50.0`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the imports at the top:

```python
from tastytrade.order import InstrumentType as TTInstrumentType
```

Replace the whole method at lines 149-197 (`def get_positions(self) -> List[Position]:` through
its `return []`) with:

```python
    def get_positions(self) -> Optional[List[Position]]:
        """Current EQUITY positions.

        Returns:
            Optional[List[Position]]: the equity book, ``[]`` when the account is
            genuinely flat, and ``None`` when the FETCH ITSELF FAILED. That
            distinction is load-bearing: ``reconcile_externally_closed_transactions``
            and the overview position comparison treat an empty list as "the broker
            confirmed it holds nothing", and a transient outage swallowed to ``[]``
            once mass-closed 8 real open transactions (2026-07-03).

        EQUITY_OPTION rows are excluded: their market value is multiplier-scaled
        (x100), so including them would fold option notionals into equity weights.
        Option exposure is read through OptionsAccountInterface, not here.
        """
        if not self._check_authentication():
            return None
        try:
            tt_positions = self._run_async(
                self._account.get_positions(self._session, include_marks=True))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting positions: {e}", exc_info=True)
            return None

        positions = []
        skipped_non_equity = 0
        for pos in tt_positions:
            if pos.instrument_type != TTInstrumentType.EQUITY:
                skipped_non_equity += 1
                continue

            qty = float(pos.quantity)
            if qty == 0:
                continue

            multiplier = int(pos.multiplier) if pos.multiplier else 1
            avg_price = float(pos.average_open_price)
            close_price = float(pos.close_price) if pos.close_price is not None else None
            # mark_price is per-share (`mark` is the whole position); fall back to the
            # previous close, then to the entry price. Never a fabricated constant.
            if pos.mark_price is not None:
                current = float(pos.mark_price)
            elif close_price is not None:
                current = close_price
            else:
                current = avg_price

            abs_qty = abs(qty)
            cost_basis = avg_price * abs_qty * multiplier
            market_val = current * abs_qty * multiplier
            unrealized_pl = market_val - cost_basis
            unrealized_plpc = (unrealized_pl / cost_basis) if cost_basis else 0.0

            # INTRADAY = move since the previous close, on the position still OPEN.
            # (`realized_day_gain` is CLOSED-out P&L for the day -- a different number,
            # and what this used to report.)
            lastday_price = close_price if close_price is not None else current
            change_today = ((current - lastday_price) / lastday_price) if lastday_price else 0.0
            intraday_pl = (current - lastday_price) * abs_qty * multiplier
            lastday_value = lastday_price * abs_qty * multiplier
            intraday_plpc = (intraday_pl / lastday_value) if lastday_value else 0.0

            side = OrderDirection.BUY if pos.quantity_direction == "Long" else OrderDirection.SELL

            positions.append(Position(
                asset_class="Equity",
                avg_entry_price=avg_price,
                avg_entry_swap_rate=None,
                change_today=change_today,
                cost_basis=cost_basis,
                current_price=current,
                exchange="",
                lastday_price=lastday_price,
                market_value=market_val,
                qty=abs_qty,
                qty_available=abs_qty,
                side=side,
                swap_rate=None,
                symbol=pos.symbol,
                unrealized_intraday_pl=intraday_pl,
                unrealized_intraday_plpc=intraday_plpc,
                unrealized_pl=unrealized_pl,
                unrealized_plpc=unrealized_plpc,
            ))

        logger.debug(
            f"[Account {self.id}] Retrieved {len(positions)} equity positions from "
            f"TastyTrade ({skipped_non_equity} non-equity rows skipped)")
        return positions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 13 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): equity-only positions, None on fetch failure, real intraday P&L"
```

---

### Task 41: Pass the `page_offset=None` all-pages sentinel

`Session._paginate` (`venv/lib/python3.12/site-packages/tastytrade/session.py:389-419`) walks
every page **only** when `params["page-offset"] is None`. Three call sites omit the sentinel and
silently truncate: `get_orders` (line 203) to the first 50 rows, `get_filled_trades` (line 458)
and `symbols_exist` (line 228) to the first 250. `get_dividends` (`:342`) and
`get_balance_history` (`:418`) already pass it correctly.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:203`, `:228`, `:447-450`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# Pagination: the SDK only walks every page when page_offset is None
# (tastytrade/session.py:389-419). Omitting it truncates silently.
# ---------------------------------------------------------------------------

def test_get_orders_requests_all_pages():
    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders()

    assert acct._account.get_order_history.call_args.kwargs["page_offset"] is None


def test_get_filled_trades_requests_all_pages():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[])

    acct.get_filled_trades()

    assert acct._account.get_history.call_args.kwargs["page_offset"] is None


def test_symbols_exist_requests_all_pages():
    acct = _bare_account()
    fake_get = AsyncMock(return_value=[_FakeEquity("AAPL"), _FakeEquity("MSFT")])

    with patch("tastytrade.instruments.Equity.get", new=fake_get):
        result = acct.symbols_exist(["AAPL", "MSFT"])

    assert result == {"AAPL": True, "MSFT": True}
    assert fake_get.call_args.kwargs["page_offset"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k all_pages`

Expected: FAIL — all three fail with `KeyError: 'page_offset'` (the kwarg is never passed, so the
SDK's default of `0` applies and only the first page is fetched).

- [ ] **Step 3: Write minimal implementation**

Three edits in `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`.

(a) In `get_orders`, replace line 203, which reads exactly:

```python
            orders = self._run_async(self._account.get_order_history(self._session))
```

with:

```python
            # page_offset=None is the SDK's "walk every page" sentinel
            # (session.py:389-419). Without it only the first 50 rows come back.
            orders = self._run_async(
                self._account.get_order_history(self._session, page_offset=None))
```

(b) In `symbols_exist`, replace line 228, which reads exactly:

```python
                equities = self._run_async(Equity.get(self._session, symbols))
```

with:

```python
                # page_offset=None -> all pages; the default of 0 caps the lookup at 250.
                equities = self._run_async(
                    Equity.get(self._session, symbols, page_offset=None))
```

(c) In `get_filled_trades`, replace the `params` literal at lines 447-450, which reads exactly:

```python
            params = {
                "types": ["Trade"],
                "sort": "Asc",
            }
```

with:

```python
            params = {
                "types": ["Trade"],
                "sort": "Asc",
                # page_offset=None -> all pages; the default of 0 caps history at 250 rows.
                "page_offset": None,
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 16 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): pass page_offset=None so paginated calls are not truncated"
```

---

### Task 42: `get_orders` must honour its `status` filter

`get_orders(self, status=None)` (line 199) accepts a status and ignores it entirely, returning
every order the account ever placed. The SDK supports it natively via
`get_order_history(..., statuses=[OrderStatus, ...])` (`account.py:813`, `:846`).

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:199-208`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_orders status filter
# ---------------------------------------------------------------------------

def test_get_orders_open_filter_asks_the_broker_for_working_statuses_only():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.OPEN)

    requested = acct._account.get_order_history.call_args.kwargs["statuses"]
    assert set(requested) == {
        TTOrderStatus.RECEIVED, TTOrderStatus.ROUTED, TTOrderStatus.IN_FLIGHT,
        TTOrderStatus.LIVE, TTOrderStatus.CONTINGENT,
    }


def test_get_orders_all_filter_sends_no_status_filter():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.ALL)

    assert "statuses" not in acct._account.get_order_history.call_args.kwargs


def test_get_orders_filled_filter_maps_to_the_tastytrade_filled_status():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.FILLED)

    assert acct._account.get_order_history.call_args.kwargs["statuses"] == [TTOrderStatus.FILLED]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k get_orders`

Expected: FAIL — `test_get_orders_open_filter_...` and `test_get_orders_filled_filter_...` fail
with `KeyError: 'statuses'` (the filter is dropped on the floor).

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the imports at the top:

```python
from tastytrade.order import OrderStatus as TTOrderStatus
from ...core.types import OrderStatus
```

Insert this class-level table and helper immediately **above**
`def get_orders(self, status=None) -> Any:` (currently line 199):

```python
    #: TastyTrade order status -> platform OrderStatus. TastyTrade's enum lives in
    #: tastytrade.order (imported here as TTOrderStatus); the platform's is
    #: ba2_common.core.types.OrderStatus. Keep this the ONE place they meet.
    _TT_STATUS_MAP = {
        TTOrderStatus.RECEIVED: OrderStatus.NEW,
        TTOrderStatus.ROUTED: OrderStatus.NEW,
        TTOrderStatus.IN_FLIGHT: OrderStatus.PENDING_NEW,
        TTOrderStatus.LIVE: OrderStatus.ACCEPTED,
        TTOrderStatus.CONTINGENT: OrderStatus.WAITING_TRIGGER,
        TTOrderStatus.FILLED: OrderStatus.FILLED,
        TTOrderStatus.CANCELLED: OrderStatus.CANCELED,
        TTOrderStatus.CANCEL_REQUESTED: OrderStatus.PENDING_CANCEL,
        TTOrderStatus.REPLACE_REQUESTED: OrderStatus.PENDING_REPLACE,
        TTOrderStatus.EXPIRED: OrderStatus.EXPIRED,
        TTOrderStatus.REJECTED: OrderStatus.REJECTED,
        TTOrderStatus.REMOVED: OrderStatus.CANCELED,
        TTOrderStatus.PARTIALLY_REMOVED: OrderStatus.CANCELED,
    }

    #: TastyTrade statuses that mean "still working at the broker".
    _TT_OPEN_STATUSES = (TTOrderStatus.RECEIVED, TTOrderStatus.ROUTED,
                         TTOrderStatus.IN_FLIGHT, TTOrderStatus.LIVE,
                         TTOrderStatus.CONTINGENT)

    #: TastyTrade statuses that mean "done, one way or another".
    _TT_CLOSED_STATUSES = (TTOrderStatus.FILLED, TTOrderStatus.CANCELLED,
                           TTOrderStatus.EXPIRED, TTOrderStatus.REJECTED,
                           TTOrderStatus.REMOVED)

    @classmethod
    def _tt_statuses_for(cls, status) -> Optional[List["TTOrderStatus"]]:
        """Translate a platform status filter into the SDK's ``statuses=[...]`` list.

        Returns ``None`` for "no filter" -- i.e. ``status`` is ``None`` or
        ``OrderStatus.ALL``. The SDK filters server-side via the ``status[]`` query
        param, so the argument must never be silently dropped (which is what
        ``get_orders`` used to do, returning every order ever placed).
        """
        if status is None or status == OrderStatus.ALL:
            return None
        if status == OrderStatus.OPEN:
            return list(cls._TT_OPEN_STATUSES)
        if status == OrderStatus.CLOSED:
            return list(cls._TT_CLOSED_STATUSES)
        matches = [tt for tt, core in cls._TT_STATUS_MAP.items() if core == status]
        if not matches:
            logger.warning(
                f"No TastyTrade order status maps to {status!r}; fetching unfiltered")
            return None
        return matches
```

Then replace the body of `get_orders` — currently lines 199-208 as amended by Task 41 — with:

```python
    def get_orders(self, status=None) -> Any:
        """All orders for this account, optionally filtered by platform OrderStatus.

        Args:
            status: a ``ba2_common.core.types.OrderStatus``. ``None`` and ``ALL``
                mean unfiltered; ``OPEN``/``CLOSED`` expand to the matching
                TastyTrade statuses.
        """
        if not self._check_authentication():
            return []
        try:
            # page_offset=None is the SDK's "walk every page" sentinel
            # (session.py:389-419). Without it only the first 50 rows come back.
            kwargs = {"page_offset": None}
            statuses = self._tt_statuses_for(status)
            if statuses:
                kwargs["statuses"] = statuses
            orders = self._run_async(
                self._account.get_order_history(self._session, **kwargs))
            logger.debug(f"[Account {self.id}] Retrieved {len(orders)} orders from TastyTrade")
            return orders
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting orders: {e}", exc_info=True)
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 19 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): honour the status filter in get_orders"
```

---

### Task 43: `get_order` must reject a non-numeric broker id as such

`get_order` (line 215) does a bare `int(order_id)` **inside** the try. Python's `int()` does
strip surrounding whitespace, and a raised `ValueError` is caught by the same `except`, so the
*return value* already happens to be `None` — but the log line says
`Error getting order <uuid>: invalid literal for int()`, i.e. it reports a caller mistake as a
broker failure, and the SDK import inside the try is dead weight. The failing assertion below is
therefore on the LOG, which is the only observable difference.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:210-219`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_order id handling
# ---------------------------------------------------------------------------

def test_get_order_with_a_non_numeric_id_says_so_instead_of_blaming_the_broker(caplog):
    """TastyTrade order ids are integers. A UUID left on a migrated row must be
    reported as a bad id, not logged as 'Error getting order ...' as if the broker
    had failed."""
    import logging

    acct = _bare_account()
    acct._account.get_order = AsyncMock()

    with caplog.at_level(logging.ERROR):
        assert acct.get_order("6e2d1f3a-0000-4c11-9c1e-8d2f3a4b5c6d") is None

    assert any("is not a TastyTrade order id" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]
    acct._account.get_order.assert_not_called()


def test_get_order_with_numeric_id_queries_the_broker_with_an_int():
    """Regression guard: a padded numeric id must still resolve."""
    acct = _bare_account()
    acct._account.get_order = AsyncMock(return_value=_placed_order(order_id=987654))

    acct.get_order(" 987654 ")

    assert acct._account.get_order.call_args.args[1] == 987654
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k get_order_with`

Expected: FAIL — `test_get_order_with_a_non_numeric_id_says_so_instead_of_blaming_the_broker`
fails on the `caplog` assertion, printing the actual message
`["[Account 1] Error getting order 6e2d1f3a-...: invalid literal for int() with base 10: '6e2d1f3a-...'"]`.
The second test passes already (`int()` strips whitespace) and is kept as a regression guard.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, replace the whole method at lines
210-219, which reads exactly:

```python
    def get_order(self, order_id: str) -> Any:
        if not self._check_authentication():
            return None
        try:
            from tastytrade.account import Account as TastyAccount
            order = self._run_async(self._account.get_order(self._session, int(order_id)))
            return order
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting order {order_id}: {e}", exc_info=True)
            return None
```

with:

```python
    def get_order(self, order_id: str) -> Any:
        """Fetch one order by its BROKER id.

        TastyTrade order ids are integers. A non-numeric id -- an Alpaca UUID left on
        a migrated row, or a caller handing over something else entirely -- is
        rejected up front and logged as such, instead of raising ValueError out of a
        bare ``int()`` and being reported as a broker failure.
        """
        if not self._check_authentication():
            return None
        try:
            broker_id = int(str(order_id).strip())
        except (TypeError, ValueError):
            logger.error(
                f"[Account {self.id}] '{order_id}' is not a TastyTrade order id "
                f"(broker ids are numeric)")
            return None
        try:
            return self._run_async(self._account.get_order(self._session, broker_id))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting order {order_id}: {e}", exc_info=True)
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 21 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "fix(tastytrade): reject non-numeric broker order ids in get_order"
```

---

### Task 44: Broker order → `TradingOrder` mapping

`get_orders` and `get_order` currently leak raw `PlacedOrder` pydantic objects to callers that
expect `TradingOrder` rows (this is what `AlpacaAccount.alpaca_order_to_tradingorder` @571 and
`_map_order_type` @548 exist for). `PlacedOrder` also has **no top-level side** — it lives on
each leg's `OrderAction` — and its `price` is signed.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (add helpers above `get_orders`; rewrite the tails of `get_orders` and `get_order`)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# PlacedOrder -> TradingOrder mapping
# ---------------------------------------------------------------------------

def test_order_mapping_derives_buy_side_from_the_leg_action():
    """PlacedOrder carries no top-level side -- it is on each leg's OrderAction."""
    from ba2_trade_platform.core.types import OrderDirection

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(action=OrderAction.BUY_TO_OPEN))

    assert mapped.side == OrderDirection.BUY


def test_order_mapping_derives_sell_side_from_a_closing_leg_action():
    from ba2_trade_platform.core.types import OrderDirection

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(action=OrderAction.SELL_TO_CLOSE))

    assert mapped.side == OrderDirection.SELL


def test_order_mapping_makes_a_sell_limit_type_from_side_plus_limit():
    """TastyTrade's order type is non-directional; ours is directional."""
    from ba2_trade_platform.core.types import OrderType

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(order_type=TTOrderType.LIMIT, action=OrderAction.SELL_TO_CLOSE,
                      price=Decimal("161.40")))

    assert mapped.order_type == OrderType.SELL_LIMIT


def test_order_mapping_stores_limit_price_unsigned():
    """PlacedOrder.price is SIGNED (negative = debit). TradingOrder.limit_price is a
    plain price."""
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(order_type=TTOrderType.LIMIT, action=OrderAction.BUY_TO_OPEN,
                      price=Decimal("-142.50")))

    assert mapped.limit_price == 142.50


def test_order_mapping_summarises_fills_into_quantity_and_average_price():
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(status=TTOrderStatus.FILLED, size="10",
                      fills=[_fill(quantity="4", fill_price="150.00"),
                             _fill(quantity="6", fill_price="151.00")]))

    assert mapped.filled_qty == 10.0
    assert mapped.open_price == pytest.approx(150.6)


def test_order_mapping_leaves_open_price_none_when_nothing_filled():
    """No fabricated fill price -- None means 'not filled', never zero."""
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(_placed_order(status=TTOrderStatus.LIVE))

    assert mapped.filled_qty == 0.0
    assert mapped.open_price is None


def test_order_mapping_translates_broker_status_to_platform_status():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(status=TTOrderStatus.CANCEL_REQUESTED))

    assert mapped.status == OrderStatus.PENDING_CANCEL


def test_get_orders_returns_trading_orders_not_raw_broker_objects():
    from ba2_trade_platform.core.models import TradingOrder

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=1), _placed_order(order_id=2)])

    orders = acct.get_orders()

    assert len(orders) == 2
    assert all(isinstance(o, TradingOrder) for o in orders)
    assert [o.broker_order_id for o in orders] == ["1", "2"]


def test_get_order_returns_a_trading_order():
    from ba2_trade_platform.core.models import TradingOrder

    acct = _bare_account()
    acct._account.get_order = AsyncMock(return_value=_placed_order(order_id=987654))

    order = acct.get_order("987654")

    assert isinstance(order, TradingOrder)
    assert order.broker_order_id == "987654"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k "mapping or returns_a_trading_order or not_raw_broker"`

Expected: FAIL — the mapping tests error with
`AttributeError: 'TastyTradeAccount' object has no attribute 'tastytrade_order_to_tradingorder'`;
the two `get_orders`/`get_order` tests fail with `assert False` on the
`isinstance(..., TradingOrder)` check.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the imports at the top:

```python
from ...core.models import TradingOrder
from ...core.types import OrderType as CoreOrderType
from tastytrade.order import OrderType as TTOrderType
```

Insert these five methods immediately **below** the `_tt_statuses_for` classmethod added in
Task 42:

```python
    @classmethod
    def _map_order_status(cls, tt_status) -> OrderStatus:
        """TastyTrade order status -> platform OrderStatus. UNKNOWN for anything unmapped."""
        if tt_status is None:
            return OrderStatus.UNKNOWN
        try:
            return cls._TT_STATUS_MAP[TTOrderStatus(tt_status)]
        except (ValueError, KeyError):
            logger.warning(f"Unmapped TastyTrade order status {tt_status!r}; recording UNKNOWN")
            return OrderStatus.UNKNOWN

    @staticmethod
    def _map_order_type(tt_type, side: OrderDirection) -> CoreOrderType:
        """Map a TastyTrade order type onto our DIRECTIONAL OrderType.

        TastyTrade's ``order_type`` is non-directional (Market / Limit / Stop /
        Stop Limit / Marketable Limit / Notional Market); ours is directional for the
        limit and stop variants, so it must be combined with the side. Unknown types
        fall back to MARKET, exactly as ``AlpacaAccount._map_order_type`` does.
        """
        if tt_type is None:
            return CoreOrderType.MARKET
        value = str(getattr(tt_type, "value", tt_type))
        is_buy = side == OrderDirection.BUY
        if value in ("Market", "Notional Market"):
            return CoreOrderType.MARKET
        if value in ("Limit", "Marketable Limit"):
            return CoreOrderType.BUY_LIMIT if is_buy else CoreOrderType.SELL_LIMIT
        if value == "Stop":
            return CoreOrderType.BUY_STOP if is_buy else CoreOrderType.SELL_STOP
        if value == "Stop Limit":
            return CoreOrderType.BUY_STOP_LIMIT if is_buy else CoreOrderType.SELL_STOP_LIMIT
        logger.warning(f"Unmapped TastyTrade order type {value!r}; recording MARKET")
        return CoreOrderType.MARKET

    @staticmethod
    def _side_from_legs(legs) -> Optional[OrderDirection]:
        """Derive the order side from its legs.

        A ``PlacedOrder`` has NO top-level side: it lives on each leg's
        ``OrderAction`` ('Buy to Open', 'Sell to Close', ...). Equity orders here are
        always single-leg, so the first leg decides. Returns ``None`` when no leg
        yields a side -- the caller must skip the order rather than guess, because a
        fabricated side puts the row on the wrong side of the book.
        """
        for leg in legs or []:
            raw = getattr(leg, "action", None)
            action = str(getattr(raw, "value", raw) or "")
            if action.startswith("Buy"):
                return OrderDirection.BUY
            if action.startswith("Sell"):
                return OrderDirection.SELL
        return None

    @staticmethod
    def _fills_summary(legs):
        """(total filled quantity, quantity-weighted average fill price) across legs.

        Returns ``(0.0, None)`` when nothing has filled -- never a fabricated price.
        """
        total_qty = 0.0
        total_notional = 0.0
        for leg in legs or []:
            for fill in (getattr(leg, "fills", None) or []):
                quantity = float(fill.quantity)
                total_qty += quantity
                total_notional += quantity * float(fill.fill_price)
        if total_qty <= 0:
            return 0.0, None
        return total_qty, total_notional / total_qty

    def tastytrade_order_to_tradingorder(self, order) -> Optional[TradingOrder]:
        """Convert a tastytrade ``PlacedOrder`` into an UNSAVED TradingOrder.

        Returns ``None`` when the side cannot be determined from the legs.
        ``PlacedOrder.price`` is SIGNED (negative = debit) but ``limit_price`` is a
        plain price, so it is stored as an absolute value. A dry-run order has
        ``id == -1``, which is not a broker id and is stored as ``None``.
        """
        side = self._side_from_legs(getattr(order, "legs", None))
        if side is None:
            logger.error(
                f"[Account {self.id}] Cannot determine side for TastyTrade order "
                f"{getattr(order, 'id', None)} -- skipping")
            return None

        filled_qty, avg_fill_price = self._fills_summary(getattr(order, "legs", None))
        raw_id = getattr(order, "id", None)
        size = getattr(order, "size", None)
        price = getattr(order, "price", None)
        stop_trigger = getattr(order, "stop_trigger", None)
        tif = getattr(order, "time_in_force", None)

        return TradingOrder(
            broker_order_id=str(raw_id) if raw_id not in (None, -1) else None,
            symbol=getattr(order, "underlying_symbol", None),
            quantity=float(size) if size is not None else filled_qty,
            side=side,
            order_type=self._map_order_type(getattr(order, "order_type", None), side),
            good_for=(str(getattr(tif, "value", tif)) if tif is not None else None),
            limit_price=abs(float(price)) if price is not None else None,
            stop_price=float(stop_trigger) if stop_trigger is not None else None,
            status=self._map_order_status(getattr(order, "status", None)),
            filled_qty=filled_qty,
            open_price=avg_fill_price,
            comment=None,
            created_at=getattr(order, "received_at", None) or getattr(order, "updated_at", None),
        )
```

Then, in `get_orders`, replace the four lines that read exactly:

```python
            orders = self._run_async(
                self._account.get_order_history(self._session, **kwargs))
            logger.debug(f"[Account {self.id}] Retrieved {len(orders)} orders from TastyTrade")
            return orders
```

with:

```python
            raw_orders = self._run_async(
                self._account.get_order_history(self._session, **kwargs))
            orders = [o for o in (self.tastytrade_order_to_tradingorder(r) for r in raw_orders)
                      if o is not None]
            logger.debug(f"[Account {self.id}] Retrieved {len(orders)} orders from TastyTrade")
            return orders
```

And in `get_order`, replace the line that reads exactly:

```python
            return self._run_async(self._account.get_order(self._session, broker_id))
```

with:

```python
            raw = self._run_async(self._account.get_order(self._session, broker_id))
            return self.tastytrade_order_to_tradingorder(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 30 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): map PlacedOrder to TradingOrder in get_orders/get_order"
```

---

### Task 45: `_build_new_order` and `_submit_order_impl`

**Never override `submit_order`.** The template method on `AccountInterface` owns validation,
transaction creation, the wash-trade gate and the protective-leg bracket. Overriding it with a
different signature is exactly what disabled `IBKRAccount` (see its class docstring,
`IBKRAccount.py:27-40`). Implement `_submit_order_impl` only, shaped after
`AlpacaAccount._submit_order_impl` @832.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (new methods, inserted above `refresh_positions` at line 294)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# Order submission
# ---------------------------------------------------------------------------

def _tt_trading_order(**kwargs):
    """A PERSISTED TradingOrder owned by a persisted TastyTrade AccountDefinition."""
    from tests.factories import create_account_definition, create_trading_order
    from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType

    account_def = create_account_definition(name="TastyTrade Test", provider="TastyTrade")
    defaults = dict(symbol="AAPL", quantity=3.0, side=OrderDirection.BUY,
                    order_type=OrderType.MARKET, status=OrderStatus.PENDING,
                    good_for="day")
    defaults.update(kwargs)
    order = create_trading_order(account_id=account_def.id, **defaults)
    return account_def, order


def test_submit_order_impl_places_a_live_order_and_records_the_broker_id():
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(
            _placed_order(order_id=987654, status=TTOrderStatus.RECEIVED)))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        result = acct._submit_order_impl(order)

    assert result.broker_order_id == "987654"
    assert result.status == OrderStatus.NEW  # TastyTrade "Received"


def test_submit_order_impl_passes_dry_run_false_explicitly():
    """place_order's dry_run parameter DEFAULTS TO True (tastytrade/account.py:877)."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order()))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order)

    assert acct._account.place_order.call_args.kwargs["dry_run"] is False


def test_submit_order_impl_builds_a_buy_to_open_leg_tagged_with_our_row_id():
    account_def, order = _tt_trading_order(quantity=3.0)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order()))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order)

    sent = acct._account.place_order.call_args.args[1]
    assert sent.legs[0].action == OrderAction.BUY_TO_OPEN
    assert sent.legs[0].quantity == Decimal("3")
    assert sent.time_in_force == OrderTimeInForce.DAY
    assert sent.order_type == TTOrderType.MARKET
    # external_identifier is TastyTrade's client_order_id equivalent -- refresh_orders
    # matches on it.
    assert sent.external_identifier == str(order.id)


def test_submit_order_impl_closing_sell_uses_sell_to_close():
    from ba2_trade_platform.core.types import OrderDirection

    account_def, order = _tt_trading_order(side=OrderDirection.SELL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order()))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order, is_closing_order=True)

    assert acct._account.place_order.call_args.args[1].legs[0].action == OrderAction.SELL_TO_CLOSE


def test_build_new_order_prices_a_buy_limit_as_a_negative_debit():
    """NewOrder.price_effect is a COMPUTED field derived from the sign of `price`
    (order.py:264-276): negative = debit. It must never be set by hand."""
    from ba2_trade_platform.core.types import OrderType

    account_def, order = _tt_trading_order(order_type=OrderType.BUY_LIMIT, limit_price=142.5)
    acct = _bare_account()
    acct.id = account_def.id

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        new_order = acct._build_new_order(order)

    assert new_order.price == Decimal("-142.5")
    assert new_order.price_effect == PriceEffect.DEBIT


def test_build_new_order_prices_a_sell_limit_as_a_positive_credit():
    from ba2_trade_platform.core.types import OrderDirection, OrderType

    account_def, order = _tt_trading_order(side=OrderDirection.SELL,
                                           order_type=OrderType.SELL_LIMIT,
                                           limit_price=161.4)
    acct = _bare_account()
    acct.id = account_def.id

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        new_order = acct._build_new_order(order)

    assert new_order.price == Decimal("161.4")
    assert new_order.price_effect == PriceEffect.CREDIT


def test_submit_order_impl_skips_an_order_that_already_has_a_broker_id():
    """Idempotency guard: an order already sent to the broker is never re-sent."""
    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock()

    result = acct._submit_order_impl(order)

    assert result is order
    acct._account.place_order.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k "submit_order_impl or build_new_order"`

Expected: FAIL — all seven error with
`AttributeError: 'TastyTradeAccount' object has no attribute '_submit_order_impl'` /
`'_build_new_order'`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the imports at the top:

```python
from decimal import Decimal
from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce
from ...core.db import add_instance, get_instance, update_instance
```

Insert these methods immediately **above** `def refresh_positions(self) -> bool:` (currently
line 294):

```python
    #: platform TradingOrder.good_for -> TastyTrade TIF. An absent/unknown value falls
    #: back to GTC, matching AlpacaAccount._submit_order_impl's tif_map default.
    _TT_TIF_MAP = {
        "day": OrderTimeInForce.DAY,
        "gtc": OrderTimeInForce.GTC,
        "gtd": OrderTimeInForce.GTD,
        "ext": OrderTimeInForce.EXT,
        "gtc_ext": OrderTimeInForce.GTC_EXT,
        "ioc": OrderTimeInForce.IOC,
    }

    @staticmethod
    def _tt_action(side: OrderDirection, is_closing_order: bool) -> OrderAction:
        """The equity ``OrderAction`` for a side plus an open/close intent."""
        if side == OrderDirection.BUY:
            return OrderAction.BUY_TO_CLOSE if is_closing_order else OrderAction.BUY_TO_OPEN
        return OrderAction.SELL_TO_CLOSE if is_closing_order else OrderAction.SELL_TO_OPEN

    @staticmethod
    def _signed_price(price: float, side: OrderDirection) -> Decimal:
        """TastyTrade encodes the direction of cash flow in the SIGN of ``NewOrder.price``.

        Negative = debit (you pay: a BUY), positive = credit (a SELL). The
        ``price_effect`` field is COMPUTED from this sign (order.py:264-276) with
        ``abs()`` applied on serialisation, so it must never be set by hand.
        """
        magnitude = abs(Decimal(str(price)))
        return -magnitude if side == OrderDirection.BUY else magnitude

    def _build_new_order(self, trading_order: TradingOrder,
                         is_closing_order: bool = False) -> NewOrder:
        """Build the SDK ``NewOrder`` for a TradingOrder.

        Shared by the live submit and by ``preview_order_impact``'s dry run, so a
        preview always prices exactly the order that would be sent.

        ``external_identifier`` carries our own row id -- TastyTrade's equivalent of
        Alpaca's ``client_order_id`` -- which is how ``refresh_orders`` matches broker
        orders back to database rows.

        Raises:
            ValueError: when a required price is missing, or the order type is not one
                TastyTrade equity submission supports here (OCO/OTO are out of scope).
        """
        from tastytrade.instruments import Equity

        equity = self._run_async(Equity.get(self._session, trading_order.symbol))
        action = self._tt_action(trading_order.side, is_closing_order)
        leg = equity.build_leg(Decimal(str(trading_order.quantity)), action)

        kwargs = {
            "time_in_force": self._TT_TIF_MAP.get(
                (trading_order.good_for or "").lower(), OrderTimeInForce.GTC),
            "legs": [leg],
            "external_identifier": str(trading_order.id) if trading_order.id else None,
        }

        core_type = trading_order.order_type
        if core_type == CoreOrderType.MARKET:
            kwargs["order_type"] = TTOrderType.MARKET
        elif core_type in (CoreOrderType.BUY_LIMIT, CoreOrderType.SELL_LIMIT):
            if trading_order.limit_price is None:
                raise ValueError(f"Limit price is required for {core_type.value} orders")
            kwargs["order_type"] = TTOrderType.LIMIT
            kwargs["price"] = self._signed_price(trading_order.limit_price, trading_order.side)
        elif core_type in (CoreOrderType.BUY_STOP, CoreOrderType.SELL_STOP):
            if trading_order.stop_price is None:
                raise ValueError(f"Stop price is required for {core_type.value} orders")
            kwargs["order_type"] = TTOrderType.STOP
            kwargs["stop_trigger"] = Decimal(str(trading_order.stop_price))
        elif core_type in (CoreOrderType.BUY_STOP_LIMIT, CoreOrderType.SELL_STOP_LIMIT):
            if trading_order.stop_price is None or trading_order.limit_price is None:
                raise ValueError("Stop and limit prices are both required for stop-limit orders")
            kwargs["order_type"] = TTOrderType.STOP_LIMIT
            kwargs["stop_trigger"] = Decimal(str(trading_order.stop_price))
            kwargs["price"] = self._signed_price(trading_order.limit_price, trading_order.side)
        else:
            raise ValueError(
                f"TastyTrade equity submission does not support order type {core_type}")

        return NewOrder(**kwargs)

    def _submit_order_impl(self, trading_order: TradingOrder, tp_price: Optional[float] = None,
                           sl_price: Optional[float] = None, is_closing_order: bool = False,
                           use_complex_order: bool = False) -> Optional[TradingOrder]:
        """Send ONE equity order to TastyTrade.

        NEVER override ``submit_order``: the template method on AccountInterface owns
        validation, transaction creation, the wash-trade gate and protective legs.
        Overriding it is exactly what disabled IBKRAccount (IBKRAccount.py:27-40).

        ``tp_price`` / ``sl_price`` / ``use_complex_order`` are accepted for interface
        compatibility and IGNORED: TastyTrade complex orders are out of scope here, so
        a protective leg has to be placed as its own order.

        Returns:
            Optional[TradingOrder]: the refreshed database row on success, ``None`` on
            failure.
        """
        if not self._check_authentication():
            return None

        # Idempotency guard: an order that already carries a broker_order_id was
        # already sent. Never re-submit it.
        if trading_order.broker_order_id:
            logger.warning(
                f"Order {trading_order.id} already has broker_order_id "
                f"{trading_order.broker_order_id} -- skipping re-submission")
            return trading_order

        if tp_price is not None or sl_price is not None:
            logger.warning(
                f"Order {trading_order.id}: TastyTrade does not attach TP/SL legs at "
                f"submission (tp={tp_price}, sl={sl_price} ignored); place them separately")

        try:
            if trading_order.id is None:
                trading_order.status = OrderStatus.PENDING
                trading_order.id = add_instance(trading_order, expunge_after_flush=True)
                logger.info(
                    f"Created new order {trading_order.id} in database with status PENDING")

            new_order = self._build_new_order(trading_order, is_closing_order=is_closing_order)

            # dry_run DEFAULTS TO True in the SDK (tastytrade/account.py:877-879).
            # Pass it explicitly so a signature change can never turn a live order
            # into a silent no-op.
            response = self._run_async(
                self._account.place_order(self._session, new_order, dry_run=False))

            fresh_order = get_instance(TradingOrder, trading_order.id)
            fresh_order.broker_order_id = str(response.order.id)
            fresh_order.status = self._map_order_status(response.order.status)
            update_instance(fresh_order)
            logger.info(
                f"Submitted TastyTrade order {fresh_order.id}: "
                f"broker_order_id={fresh_order.broker_order_id}, status={fresh_order.status}")
            return fresh_order
        except Exception as e:
            logger.error(
                f"Error submitting order {trading_order.id} to TastyTrade: {e}", exc_info=True)
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 37 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement _submit_order_impl with explicit dry_run=False"
```

---

### Task 46: `cancel_order`

`AlpacaAccount.cancel_order` @1315 tells a database id from a broker id by looking for a `-`
(Alpaca ids are UUIDs). TastyTrade broker ids are integers, exactly like our database ids, so
that trick does not transfer — resolution has to be by lookup.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (new method, inserted above `refresh_positions`)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

def test_cancel_order_by_database_id_deletes_the_broker_order():
    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(return_value=None)

    assert acct.cancel_order(order.id) is True
    assert acct._account.delete_order.call_args.args[1] == 987654


def test_cancel_order_by_broker_id_resolves_the_same_row():
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(return_value=None)

    assert acct.cancel_order("987654") is True
    assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL


def test_cancel_order_marks_pending_cancel_not_canceled():
    """The cancel has only been REQUESTED. refresh_orders promotes it once the broker
    confirms -- a dependent replacement must not fire before the qty is released."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(return_value=None)

    acct.cancel_order(order.id)

    assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL


def test_cancel_order_without_a_broker_id_fails_without_calling_the_broker():
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock()

    assert acct.cancel_order(order.id) is False
    acct._account.delete_order.assert_not_called()


def test_cancel_order_for_an_unknown_id_returns_false():
    account_def, _order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock()

    assert acct.cancel_order("999999999") is False
    acct._account.delete_order.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k cancel_order`

Expected: FAIL — all five error with
`AttributeError: 'TastyTradeAccount' object has no attribute 'cancel_order'`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the imports at the top:

```python
from sqlmodel import Session, select
from ...core.db import get_db, InstanceNotFound
```

Insert this method immediately **below** `_submit_order_impl`:

```python
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. ``order_id`` may be our DATABASE id or the BROKER id.

        AlpacaAccount.cancel_order tells the two apart by looking for a '-' (its broker
        ids are UUIDs). TastyTrade broker ids are integers, exactly like our database
        ids, so resolution is by LOOKUP instead: our own row first (scoped to this
        account), then by ``broker_order_id``.

        Returns:
            bool: True when the cancel was accepted by the broker.
        """
        if not self._check_authentication():
            return False

        db_order = None
        try:
            candidate = get_instance(TradingOrder, int(str(order_id).strip()))
            if candidate.account_id == self.id:
                db_order = candidate
        except (InstanceNotFound, TypeError, ValueError):
            db_order = None

        if db_order is None:
            with get_db() as session:
                found = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.broker_order_id == str(order_id),
                        TradingOrder.account_id == self.id,
                    )
                ).first()
                found_id = found.id if found else None
            db_order = get_instance(TradingOrder, found_id) if found_id else None

        if db_order is None:
            logger.error(f"[Account {self.id}] Order {order_id} not found in database")
            return False
        if not db_order.broker_order_id:
            logger.error(
                f"[Account {self.id}] Order {db_order.id} has no broker_order_id "
                f"-- it was never sent to TastyTrade")
            return False

        try:
            self._run_async(
                self._account.delete_order(self._session, int(db_order.broker_order_id)))
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Error cancelling TastyTrade order {order_id}: {e}",
                exc_info=True)
            return False

        # PENDING_CANCEL, not CANCELED: the cancel has only been REQUESTED.
        # refresh_orders promotes it once the broker confirms and the qty is actually
        # released -- same rule as AlpacaAccount.cancel_order.
        fresh_order = get_instance(TradingOrder, db_order.id)
        fresh_order.status = OrderStatus.PENDING_CANCEL
        update_instance(fresh_order)
        logger.info(
            f"[Account {self.id}] Requested cancel of TastyTrade order "
            f"broker_order_id={db_order.broker_order_id} (db id={db_order.id})")
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 42 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement cancel_order with dual db/broker id resolution"
```

---

### Task 47: Re-parent to `AccountInterface` and reconcile `supports_trading`

`supports_trading` is read from the **class** at `ui/pages/settings.py:1435`
(`getattr(provider_cls, 'supports_trading', True)`) and from the **instance** at
`core/TradeManager.py:921` and `:1223` (`getattr(account, 'supports_trading', True)`). All three
must agree. Removing the local `supports_trading = False` pin at `TastyTradeAccount.py:19` lets
the class inherit `True` from `AccountInterface:28`, which is the single source all three reads
then see.

`AccountInterface` declares six abstract methods: `_submit_order_impl` (Task 45) and
`cancel_order` (Task 46) are done; `modify_order`, `adjust_tp`, `adjust_sl` and `adjust_tp_sl`
are **out of scope** per the design and get explicit unsupported implementations here. Until all
six exist, `TastyTradeAccount` cannot be instantiated at all.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:9`, `:12-19` (base class + docstring), `get_account_info`, plus new methods
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# Trading-capable class wiring
# ---------------------------------------------------------------------------

def test_tastytrade_account_is_a_trading_account_interface():
    from ba2_trade_platform.core.interfaces import AccountInterface

    assert issubclass(TastyTradeAccount, AccountInterface)


def test_every_abstract_method_is_implemented_so_the_class_can_be_constructed():
    """object.__new__ on an ABC raises unless every @abstractmethod is implemented."""
    assert object.__new__(TastyTradeAccount) is not None


def test_supports_trading_reads_true_from_the_provider_registry_class():
    """ui/pages/settings.py:1435 reads it from the CLASS via the provider registry."""
    from ba2_trade_platform.modules.accounts import providers

    assert getattr(providers["TastyTrade"], "supports_trading", True) is True


def test_supports_trading_reads_true_from_an_instance():
    """core/TradeManager.py:921 and :1223 read it from the INSTANCE."""
    assert getattr(_bare_account(), "supports_trading", True) is True


def test_supports_trading_is_not_pinned_on_the_class_itself():
    """A local pin is what made the class and instance reads disagree; inherit it."""
    assert "supports_trading" not in TastyTradeAccount.__dict__


def test_get_account_info_reports_trading_support():
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(return_value=_balances())

    assert acct.get_account_info()["supports_trading"] is True


def test_modify_order_is_reported_as_unsupported():
    assert _bare_account().modify_order("987654") is None


def test_tp_sl_adjustment_is_reported_as_unsupported():
    from tests.factories import create_transaction

    acct = _bare_account()
    transaction = create_transaction(symbol="AAPL")

    assert acct.adjust_tp(transaction, 160.0) is False
    assert acct.adjust_sl(transaction, 130.0) is False
    assert acct.adjust_tp_sl(transaction, 160.0, 130.0) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k "trading_account_interface or supports_trading or abstract_method or unsupported"`

Expected: FAIL — `test_tastytrade_account_is_a_trading_account_interface` fails with
`assert False` (still parented to `ReadOnlyAccountInterface`); the three `supports_trading` tests
fail with `assert False is True` / `assert 'supports_trading' not in {...}`; the two
"unsupported" tests error with
`AttributeError: 'TastyTradeAccount' object has no attribute 'modify_order'` / `'adjust_tp'`.

- [ ] **Step 3: Write minimal implementation**

Four edits in `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`.

(a) Replace the import at line 9, which reads exactly:

```python
from ...core.interfaces import ReadOnlyAccountInterface
```

with:

```python
from ...core.interfaces import AccountInterface
from ...core.models import Transaction
```

(b) Replace lines 12-19, which read exactly:

```python
class TastyTradeAccount(ReadOnlyAccountInterface):
    """
    Read-only account interface for TastyTrade brokerage.

    Uses the tastytrade Python SDK (async) with sync wrappers.
    This account does NOT support trading operations.
    """
    supports_trading = False
```

with:

```python
class TastyTradeAccount(AccountInterface):
    """
    Trading account interface for TastyTrade brokerage.

    Uses the tastytrade Python SDK 12.x (async) driven from a persistent
    background event loop, so the httpx client's connections are never invalidated
    by a closed loop.

    ``supports_trading`` is deliberately NOT pinned here -- it is inherited as True
    from AccountInterface. It is read from the CLASS at ui/pages/settings.py:1435 and
    from the INSTANCE at core/TradeManager.py:921 and :1223; a local pin is exactly
    what made those reads disagree.

    Supported: equity market/limit/stop/stop-limit submission, cancellation, order and
    position refresh, order preview (dry run), account snapshot, cash transfers and
    per-symbol margin metadata.

    Out of scope (explicitly unsupported below, never silently half-working):
    ``modify_order``, TP/SL adjustment, complex orders and OptionsAccountInterface.

    TWO SDK TRAPS:
      * ``Account.place_order``'s ``dry_run`` DEFAULTS TO True (account.py:877) --
        every real submission passes ``dry_run=False`` explicitly.
      * ``NewOrder.price_effect`` is a computed field derived from the SIGN of
        ``price`` (order.py:264-276) -- never set it by hand.
    """
```

(c) In `get_account_info`, replace the line that reads exactly:

```python
                "supports_trading": False,
```

with:

```python
                "supports_trading": self.supports_trading,
```

(d) Insert these four unsupported-operation implementations immediately **below** `cancel_order`:

```python
    # ------------------------------------------------------------------
    # Out of scope for TastyTrade (see class docstring). These are declared
    # @abstractmethod on AccountInterface, so they must exist for the class to be
    # instantiable -- but they fail LOUDLY rather than half-working.
    # ------------------------------------------------------------------

    def modify_order(self, order_id: str, trading_order: Optional[TradingOrder] = None):
        """NOT SUPPORTED on TastyTrade.

        The SDK exposes ``Account.replace_order``, but using it would need the whole
        cancel/replace + dependent-order bookkeeping AlpacaAccount carries. Cancel the
        order and submit a new one instead.
        """
        logger.error(
            f"[Account {self.id}] modify_order is not supported for TastyTrade "
            f"(order {order_id}); cancel and resubmit instead")
        return None

    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        logger.error(
            f"[Account {self.id}] adjust_tp is not supported for TastyTrade "
            f"(transaction {transaction.id}, requested {new_tp_price})")
        return False

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        logger.error(
            f"[Account {self.id}] adjust_sl is not supported for TastyTrade "
            f"(transaction {transaction.id}, requested {new_sl_price})")
        return False

    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: Optional[float] = None,
                     new_sl_price: Optional[float] = None, source: str = "") -> bool:
        """NOT SUPPORTED: TastyTrade protective-leg management is out of scope."""
        logger.error(
            f"[Account {self.id}] adjust_tp_sl is not supported for TastyTrade "
            f"(transaction {transaction.id}, tp={new_tp_price}, sl={new_sl_price})")
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 50 passed.

Then confirm nothing that reads the account registry regressed:

Run: `venv/bin/python -m pytest tests/test_accounts/test_account_interface.py -v`
Run: `venv/bin/python -m pytest tests/test_boot_smoke.py -v`

Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): re-parent to AccountInterface, supports_trading=True"
```

---

### Task 48: Route submission failures through `_handle_order_submit_error`

`_submit_order_impl`'s except block currently just logs and returns `None`, so a rejected order
stays `PENDING` forever with no reason recorded. `AccountInterface._handle_order_submit_error`
(now inherited, since Task 47) classifies the error, retries a breached stop as a market order,
and marks the row `ERROR` with the reason in `comment` — which is what the Pending Orders UI
shows.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (`_submit_order_impl` except block)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# Submission failure handling
# ---------------------------------------------------------------------------

def test_submit_order_impl_marks_the_row_error_with_the_broker_message():
    """A rejected order must not sit at PENDING forever with no reason recorded."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        side_effect=RuntimeError("preflight failed: account is restricted"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        result = acct._submit_order_impl(order)

    assert result is None
    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.ERROR
    assert "account is restricted" in stored.comment
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k marks_the_row_error`

Expected: FAIL — `AssertionError: assert <OrderStatus.PENDING: 'pending'> == <OrderStatus.ERROR: 'ERROR'>`
(the row is left untouched).

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, replace the except block at the
end of `_submit_order_impl`, which reads exactly:

```python
        except Exception as e:
            logger.error(
                f"Error submitting order {trading_order.id} to TastyTrade: {e}", exc_info=True)
            return None
```

with:

```python
        except Exception as e:
            logger.error(
                f"Error submitting order {trading_order.id} to TastyTrade: {e}", exc_info=True)
            # Broker-agnostic failure handling: classify the error, retry ONCE as a
            # MARKET order when a stop was already through the market, and otherwise
            # mark the row ERROR with the typed reason + broker message in `comment`
            # (so it is visible in the Pending Orders UI, not just the log). Returns
            # the resubmitted order on a successful retry, else None.
            if trading_order.id:
                return self._handle_order_submit_error(trading_order, e)
            logger.warning("Cannot mark order as ERROR - order has no ID")
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 51 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): route submission failures through _handle_order_submit_error"
```

---

### Task 49: `refresh_orders`

`refresh_orders` is a `return True` stub at line 298. Without it, `refresh_transactions`
(`ReadOnlyAccountInterface.py:411`) derives transaction state from orders that never leave their
submitted status, so every transaction stays `WAITING` forever.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:298-300`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# refresh_orders
# ---------------------------------------------------------------------------

def test_refresh_orders_promotes_a_filled_order_and_records_the_fill():
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(quantity=10.0, broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                      external_identifier=str(order.id),
                      fills=[_fill(quantity="10", fill_price="150.25")]),
    ])

    assert acct.refresh_orders() is True
    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.FILLED
    assert stored.filled_qty == 10.0
    assert stored.open_price == pytest.approx(150.25)


def test_refresh_orders_backfills_broker_order_id_from_external_identifier():
    """external_identifier is our own row id -- TastyTrade's client_order_id."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder

    account_def, order = _tt_trading_order(quantity=10.0)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.LIVE, size="10",
                      external_identifier=str(order.id)),
    ])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).broker_order_id == "987654"


def test_refresh_orders_leaves_an_order_absent_from_the_response_untouched():
    """TastyTrade's order history is paginated and date-windowed, so absence is NOT
    evidence of cancellation. Unlike Alpaca's refresh, nothing is auto-canceled."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="111111",
                                           status=OrderStatus.ACCEPTED)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).status == OrderStatus.ACCEPTED


def test_refresh_orders_returns_false_when_the_fetch_fails():
    account_def, _order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    assert acct.refresh_orders() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k refresh_orders`

Expected: FAIL — `test_refresh_orders_promotes_a_filled_order_and_records_the_fill` fails with
`assert <OrderStatus.PENDING: 'pending'> == <OrderStatus.FILLED: 'filled'>`; the backfill test
fails with `assert None == '987654'`; the failure test fails with `assert True is False` (the
stub always returns True).

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, replace lines 298-300, which read
exactly:

```python
    def refresh_orders(self, **kwargs) -> bool:
        # Orders are always fetched live from API
        return True
```

with:

```python
    def refresh_orders(self, **kwargs) -> bool:
        """Sync our TradingOrder rows with TastyTrade's order book.

        Without this, ``refresh_transactions``
        (ReadOnlyAccountInterface.refresh_transactions) has nothing to derive from and
        every transaction stays WAITING forever.

        Matching is on ``external_identifier`` first (we set it to our own row id at
        submission -- TastyTrade's equivalent of Alpaca's ``client_order_id``), then on
        ``broker_order_id``. Unlike AlpacaAccount.refresh_orders this does NOT cancel
        rows that are missing from the response: TastyTrade's order history is
        paginated and date-windowed, so absence is not evidence of cancellation.

        Args:
            **kwargs: absorbs the Alpaca-specific ``heuristic_mapping`` / ``fetch_all``
                that ui/pages/overview.py and core/TradeManager.py pass by name.

        Returns:
            bool: False only when the broker fetch itself failed.
        """
        if not self._check_authentication():
            return False
        try:
            raw_orders = self._run_async(
                self._account.get_order_history(self._session, page_offset=None))
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Error refreshing orders from TastyTrade: {e}",
                exc_info=True)
            return False

        updated_count = 0
        mapped_count = 0
        for raw in raw_orders:
            raw_id = getattr(raw, "id", None)
            if raw_id in (None, -1):
                continue
            broker_order_id = str(raw_id)

            broker_state = self.tastytrade_order_to_tradingorder(raw)
            if broker_state is None:
                continue

            db_order = None
            external_identifier = getattr(raw, "external_identifier", None)
            if external_identifier:
                try:
                    candidate = get_instance(TradingOrder, int(external_identifier))
                except (InstanceNotFound, TypeError, ValueError):
                    candidate = None
                if candidate is not None and candidate.account_id == self.id:
                    db_order = candidate
                    if not db_order.broker_order_id:
                        mapped_count += 1

            if db_order is None:
                with get_db() as session:
                    found = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.broker_order_id == broker_order_id,
                            TradingOrder.account_id == self.id,
                        )
                    ).first()
                    found_id = found.id if found else None
                db_order = get_instance(TradingOrder, found_id) if found_id else None
            if db_order is None:
                continue

            has_changes = False

            # PENDING_CANCEL only advances once the broker reaches a FINAL state --
            # a dependent replacement must not fire before the qty is released.
            if db_order.status == OrderStatus.PENDING_CANCEL:
                resolved = OrderStatus.resolve_pending_cancel(broker_state.status)
                if resolved is not None and resolved != db_order.status:
                    logger.info(
                        f"Order {db_order.id} PENDING_CANCEL -> {resolved.value} "
                        f"(broker reported {broker_state.status})")
                    db_order.status = resolved
                    has_changes = True
            elif db_order.status != broker_state.status:
                logger.debug(
                    f"Order {db_order.id} status changed: {db_order.status} -> {broker_state.status}")
                db_order.status = broker_state.status
                has_changes = True

            if float(db_order.filled_qty or 0.0) != float(broker_state.filled_qty or 0.0):
                db_order.filled_qty = broker_state.filled_qty
                has_changes = True

            if broker_state.open_price is not None and db_order.open_price != broker_state.open_price:
                db_order.open_price = broker_state.open_price
                has_changes = True

            if not db_order.broker_order_id:
                db_order.broker_order_id = broker_order_id
                has_changes = True

            if has_changes:
                update_instance(db_order)
                updated_count += 1

        logger.info(
            f"[Account {self.id}] Refreshed TastyTrade orders: {updated_count} updated, "
            f"{mapped_count} mapped via external_identifier")
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 55 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement refresh_orders keyed on external_identifier"
```

---

### Task 50: `refresh_positions`

`refresh_positions` is a `return True` stub at line 294, so it reports success even when the
broker is unreachable — the opposite of `AlpacaAccount.refresh_positions` @1685, whose whole job
is to convert a `None` fetch into a `False`.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:294-296`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# refresh_positions
# ---------------------------------------------------------------------------

def test_refresh_positions_returns_false_when_the_fetch_fails():
    """A stub that always returns True tells callers the book was confirmed when it
    was not."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(side_effect=RuntimeError("connection reset"))

    assert acct.refresh_positions() is False


def test_refresh_positions_returns_true_when_the_book_was_read():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[_tt_position(symbol="AAPL")])

    assert acct.refresh_positions() is True


def test_refresh_positions_returns_true_for_a_genuinely_flat_account():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[])

    assert acct.refresh_positions() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k refresh_positions`

Expected: FAIL — `test_refresh_positions_returns_false_when_the_fetch_fails` fails with
`assert True is False`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, replace lines 294-296, which read
exactly:

```python
    def refresh_positions(self) -> bool:
        # Positions are always fetched live from API
        return True
```

with:

```python
    def refresh_positions(self) -> bool:
        """Confirm the broker's equity book is readable.

        TastyTrade positions are always fetched live, so there is no cache to refresh
        -- but the return value is a real signal that the broker answered. ``None``
        from ``get_positions`` means the FETCH FAILED (not a flat account), so it maps
        to False here, exactly as AlpacaAccount.refresh_positions does.
        """
        positions = self.get_positions()
        if positions is None:
            logger.error(f"[Account {self.id}] Error refreshing positions from TastyTrade: fetch failed")
            return False
        logger.info(
            f"[Account {self.id}] Successfully refreshed {len(positions)} positions from TastyTrade")
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 58 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): make refresh_positions report a real broker outcome"
```

---

### Task 51: `preview_order_impact` (broker dry run)

The `preview_order_impact` seam on `AccountInterface` (Task 30) returns `None` by default,
meaning "this broker has no precheck". TastyTrade has one:
`place_order(..., dry_run=True)` returns a `PlacedOrderResponse` whose `buying_power_effect`
gives the exact cost. Its `change_in_buying_power` is **signed** — negative for a buy — which is
why `OrderImpact.bp_cost` exists.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (new method below `cancel_order`)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# preview_order_impact
# ---------------------------------------------------------------------------

def test_preview_order_impact_passes_dry_run_true_explicitly():
    """It must never send a live order -- and must not rely on the SDK default."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1)))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct.preview_order_impact(order)

    assert acct._account.place_order.call_args.kwargs["dry_run"] is True


def test_preview_order_impact_turns_a_signed_debit_into_a_positive_bp_cost():
    """BuyingPowerEffect.change_in_buying_power is NEGATIVE for a buy (order.py:381)."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            change_in_buying_power="-1500",
                                            isolated_requirement="1500",
                                            total_fees="0.03"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.change_in_buying_power == -1500.0
    assert impact.bp_cost == 1500.0
    assert impact.margin_requirement == 1500.0
    assert impact.estimated_fees == pytest.approx(0.03)
    assert impact.accepted is True


def test_preview_order_impact_marks_a_rejected_preview_as_not_accepted():
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            errors=["insufficient buying power"]))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.accepted is False
    assert any("insufficient buying power" in e for e in impact.errors)


def test_preview_order_impact_returns_none_when_the_preview_call_fails():
    """None means 'no precheck available', NOT 'the order is free'. It must never be
    fabricated as a zero impact."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        assert acct.preview_order_impact(order) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k preview_order_impact`

Expected: FAIL — the first three fail because the inherited base returns `None` without calling
the broker: `TypeError: 'NoneType' object is not subscriptable` on `call_args`, and
`AttributeError: 'NoneType' object has no attribute 'change_in_buying_power'` / `'accepted'`.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, add to the imports at the top:

```python
from ...core.account_types import OrderImpact
```

Insert this method immediately **below** `cancel_order`:

```python
    def preview_order_impact(self, trading_order: TradingOrder) -> Optional[OrderImpact]:
        """Broker-side dry run of ONE order: what it would cost in buying power.

        MUST NOT send a live order. ``place_order``'s ``dry_run`` parameter DEFAULTS
        TO True (tastytrade/account.py:877-879) -- it is passed explicitly here anyway,
        and must always be passed explicitly at real submission sites.

        The order is built with the SAME ``_build_new_order`` the live submit uses, so
        a preview prices exactly what would be sent. ``trading_order`` is neither
        mutated nor persisted, and no ``broker_order_id`` is written.

        Returns:
            Optional[OrderImpact]: ``None`` when the preview call failed. ``None``
            means "no precheck", NOT "the order is free" -- a zero impact is never
            fabricated.
        """
        if not self._check_authentication():
            return None
        try:
            new_order = self._build_new_order(trading_order)
            response = self._run_async(
                self._account.place_order(self._session, new_order, dry_run=True))
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Order preview failed for {trading_order.symbol}: {e}",
                exc_info=True)
            return None

        effect = response.buying_power_effect
        fees = getattr(response, "fee_calculation", None)
        warnings = [str(w) for w in (response.warnings or [])]
        errors = [str(err) for err in (response.errors or [])]
        return OrderImpact(
            symbol=trading_order.symbol,
            # SIGNED: negative for a buy. Consume OrderImpact.bp_cost, never this.
            change_in_buying_power=float(effect.change_in_buying_power),
            margin_requirement=float(effect.isolated_order_margin_requirement),
            estimated_fees=float(fees.total_fees) if fees is not None else None,
            accepted=not errors,
            warnings=warnings,
            errors=errors,
            raw={
                "current_buying_power": float(effect.current_buying_power),
                "new_buying_power": float(effect.new_buying_power),
                "change_in_margin_requirement": float(effect.change_in_margin_requirement),
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 62 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement preview_order_impact via place_order dry run"
```

---

### Task 52: `get_account_snapshot`

The base implementation on `ReadOnlyAccountInterface` (Task 28) probes `get_account_info()`
tolerantly. TastyTrade can do much better: `get_balances()` returns a typed `AccountBalance`, and
`Account.margin_or_cash` says whether Reg-T 2:1 applies — which is the `default_bp_factor` the
allocation engine falls back to.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (new methods below `get_account_info`)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_account_snapshot
# ---------------------------------------------------------------------------

def test_account_snapshot_maps_a_margin_account():
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    acct._account.get_balances = AsyncMock(return_value=_balances())

    snapshot = acct.get_account_snapshot()

    assert snapshot.cash == 25000.0
    assert snapshot.buying_power == 50000.0
    assert snapshot.net_liquidation == 100000.0
    assert snapshot.equity == 100000.0
    assert snapshot.long_market_value == 75000.0
    assert snapshot.is_margin_account is True
    assert snapshot.margin_multiplier == 2.0


def test_account_snapshot_of_a_cash_account_has_no_leverage():
    acct = _bare_account()
    acct._account.margin_or_cash = "Cash"
    acct._account.get_balances = AsyncMock(return_value=_balances())

    snapshot = acct.get_account_snapshot()

    assert snapshot.is_margin_account is False
    assert snapshot.margin_multiplier == 1.0


def test_account_snapshot_negates_tastytrades_positive_short_magnitude():
    """AccountSnapshot pins short_market_value as NEGATIVE while shorts are held
    (the Alpaca convention), but TastyTrade reports short-equity-value as a POSITIVE
    magnitude. If the adapter passes it through, gross exposure becomes
    broker-dependent — and every other fixture here uses a zero short, which is
    sign-agnostic and would never catch it."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    balances = _balances()
    balances.short_equity_value = Decimal("12000")
    acct._account.get_balances = AsyncMock(return_value=balances)

    snapshot = acct.get_account_snapshot()

    assert snapshot.short_market_value == -12000.0


def test_account_snapshot_leaves_an_absent_short_value_as_none():
    """None means 'the broker did not say', which must not be negated into -0.0."""
    acct = _bare_account()
    balances = _balances()
    balances.short_equity_value = None
    acct._account.get_balances = AsyncMock(return_value=balances)

    snapshot = acct.get_account_snapshot()

    assert snapshot.short_market_value is None


def test_account_snapshot_on_failure_is_all_none_not_zeros():
    """An all-None snapshot is a legitimate 'the broker told us nothing'. Zeros would
    be a fabricated balance, which the caller cannot distinguish from a real one."""
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    snapshot = acct.get_account_snapshot()

    assert snapshot.cash is None
    assert snapshot.buying_power is None
    assert snapshot.net_liquidation is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k account_snapshot`

Expected: FAIL — the first two fail with `assert None == 25000.0` / `assert False is True` (the
inherited base probe reads `get_account_info()`, which is not what these mocks feed, and never
sets `margin_multiplier`).

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, extend the `account_types` import
added in Task 51 to:

```python
from ...core.account_types import (
    AccountSnapshot, CashTransfer, MarginInfo, OrderImpact,
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL,
    MARGIN_SOURCE_DEFAULT, MARGIN_SOURCE_POSITION,
)
```

Insert these two methods immediately **below** `get_account_info`:

```python
    def _is_margin_account(self) -> bool:
        """Whether this is a Reg-T margin account (``Account.margin_or_cash``)."""
        return str(getattr(self._account, "margin_or_cash", "") or "").strip().lower() == "margin"

    def get_account_snapshot(self) -> AccountSnapshot:
        """Broker-agnostic cash / equity / buying-power view of this account.

        Overrides the base tolerant probe: TastyTrade returns a typed AccountBalance,
        so every field is read directly.

        Never fabricates a number -- a field TastyTrade did not supply stays ``None``,
        and a failed fetch returns an ALL-NONE snapshot, which is a legitimate "the
        broker told us nothing" result the caller must refuse to plan on. Zeros would
        be indistinguishable from a real flat account.

        ``margin_multiplier`` is the Reg-T leverage the allocation engine uses as its
        conservative ``default_bp_factor``: 2.0 for a margin account, 1.0 for cash.
        """
        if not self._check_authentication():
            return AccountSnapshot()
        try:
            balances = self._run_async(self._account.get_balances(self._session))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error getting account snapshot: {e}", exc_info=True)
            return AccountSnapshot()

        def _num(name):
            value = getattr(balances, name, None)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        is_margin = self._is_margin_account()
        net_liquidation = _num("net_liquidating_value")
        return AccountSnapshot(
            cash=_num("cash_balance"),
            equity=net_liquidation,
            net_liquidation=net_liquidation,
            buying_power=_num("equity_buying_power"),
            non_marginable_buying_power=_num("cash_available_to_withdraw"),
            margin_multiplier=2.0 if is_margin else 1.0,
            is_margin_account=is_margin,
            long_market_value=_num("long_equity_value"),
            # NEGATED ON PURPOSE. AccountSnapshot pins short_market_value as NEGATIVE
            # while shorts are held (the Alpaca convention), but TastyTrade's
            # short-equity-value is a POSITIVE MAGNITUDE. Passing it through unchanged
            # makes gross exposure broker-dependent: long + abs(short) and long - short
            # disagree, and no fixture with a zero short can tell the difference.
            short_market_value=(
                -_num("short_equity_value")
                if _num("short_equity_value") is not None
                else None
            ),
            # TastyTrade's pending_cash is SIGNED (positive = incoming); it is reported
            # as-is rather than clamped, so the caller sees what the broker said.
            pending_transfer_in=_num("pending_cash"),
            supports_fractional=True,
            raw={
                "margin_equity": _num("margin_equity"),
                "maintenance_requirement": _num("maintenance_requirement"),
                "derivative_buying_power": _num("derivative_buying_power"),
                "margin_or_cash": getattr(self._account, "margin_or_cash", None),
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 65 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement get_account_snapshot from typed balances"
```

---

### Task 53: `get_cash_transfers`

Feeds the income ledger. TastyTrade posts deposits, withdrawals and dividends as
`Money Movement` transactions. Two shapes matter: a dividend arrives as a POSITIVE gross leg
plus (optionally) a NEGATIVE tax leg sharing the same `(symbol, transaction_date)`; and with DRIP
on there is an extra `Withdrawal` leg described as "Cash dividend reinvested into X", which never
actually left the account.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (new method below `get_dividends`)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_cash_transfers
# ---------------------------------------------------------------------------

def _money_movement(txn_id, sub_type, net_value, transaction_date=date(2026, 8, 3),
                    symbol=None, description="", underlying_symbol=None):
    """Stand-in for a tastytrade `Money Movement` Transaction."""
    return SimpleNamespace(
        id=txn_id,
        transaction_type="Money Movement",
        transaction_sub_type=sub_type,
        net_value=Decimal(net_value),
        transaction_date=transaction_date,
        symbol=symbol,
        underlying_symbol=underlying_symbol,
        description=description,
    )


def test_get_cash_transfers_requests_money_movement_over_all_pages():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[])

    acct.get_cash_transfers(start_date=date(2026, 7, 1), end_date=date(2026, 8, 20))

    kwargs = acct._account.get_history.call_args.kwargs
    assert kwargs["types"] == ["Money Movement"]
    assert kwargs["page_offset"] is None
    assert kwargs["start_date"] == date(2026, 7, 1)
    assert kwargs["end_date"] == date(2026, 8, 20)


def test_get_cash_transfers_reports_a_deposit_as_positive_income():
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_DEPOSIT

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9001, "Deposit", "2500", description="ACH DEPOSIT"),
    ])

    transfers = acct.get_cash_transfers()

    assert len(transfers) == 1
    assert transfers[0].external_id == "9001"
    assert transfers[0].event_type == CASH_TRANSFER_DEPOSIT
    assert transfers[0].amount == 2500.0
    assert transfers[0].is_income is True


def test_get_cash_transfers_nets_dividend_tax_off_the_gross_leg():
    """Gross and tax share (symbol, date). One row is emitted, keeping the GROSS leg's
    own broker id so the (account_id, external_id) idempotency key stays 1:1."""
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_DIVIDEND

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9101, "Dividend", "1.57", underlying_symbol="TIDL",
                        description="TIDAL TRUST II"),
        _money_movement(9102, "Dividend", "-0.24", underlying_symbol="TIDL",
                        description="TIDAL TRUST II"),
    ])

    transfers = acct.get_cash_transfers()

    assert len(transfers) == 1
    assert transfers[0].external_id == "9101"
    assert transfers[0].event_type == CASH_TRANSFER_DIVIDEND
    assert transfers[0].symbol == "TIDL"
    assert transfers[0].amount == pytest.approx(1.33)


def test_get_cash_transfers_reports_a_withdrawal_as_negative_and_not_income():
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_WITHDRAWAL

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9201, "Withdrawal", "-800", description="ACH WITHDRAWAL"),
    ])

    transfers = acct.get_cash_transfers()

    assert transfers[0].event_type == CASH_TRANSFER_WITHDRAWAL
    assert transfers[0].amount == -800.0
    assert transfers[0].is_income is False


def test_get_cash_transfers_ignores_the_drip_reinvestment_leg():
    """A DRIP 'Withdrawal' never left the account -- it bought shares with the dividend
    already recorded. Recording it would double-count the cash going out."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9301, "Withdrawal", "-1.33",
                        description="Cash dividend reinvested into TIDL"),
    ])

    assert acct.get_cash_transfers() == []


def test_get_cash_transfers_returns_empty_list_on_failure():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    assert acct.get_cash_transfers() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k cash_transfers`

Expected: FAIL — the first five fail because the inherited base seam returns `[]` without calling
the broker: `TypeError: 'NoneType' object is not subscriptable` on the pagination test, and
`assert 0 == 1` / `IndexError: list index out of range` on the rest.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, insert this class-level pair and
method immediately **below** `get_dividends`:

```python
    #: Money Movement sub-types that ADD cash to the account.
    _TT_DEPOSIT_SUB_TYPES = ("Deposit", "Transfer")
    #: Money Movement sub-types that REMOVE cash from the account.
    _TT_WITHDRAWAL_SUB_TYPES = ("Withdrawal", "Transfer")

    def get_cash_transfers(self, start_date=None, end_date=None) -> List[CashTransfer]:
        """Deposits, withdrawals and dividends over a date window.

        ``page_offset=None`` is the SDK "all pages" sentinel. ``external_id`` is the
        broker transaction id -- the ``(account_id, external_id)`` idempotency key of
        ``portfolio_income_event`` -- so re-syncing a window upserts instead of
        duplicating.

        A dividend arrives as a POSITIVE gross leg plus (optionally) a NEGATIVE tax leg
        sharing the same ``(symbol, transaction_date)``. ONE CashTransfer is emitted per
        GROSS leg, keeping that leg's own id, with the tax netted off its amount -- so
        the ledger records the income actually KEPT and the id stays 1:1.

        Returns:
            List[CashTransfer]: ``[]`` on failure as well as on genuine emptiness (this
            seam does not distinguish the two); the failure is logged.
        """
        if not self._check_authentication():
            return []

        params = {"types": ["Money Movement"], "sort": "Asc", "page_offset": None}
        if start_date is not None:
            params["start_date"] = start_date.date() if isinstance(start_date, datetime) else start_date
        if end_date is not None:
            params["end_date"] = end_date.date() if isinstance(end_date, datetime) else end_date

        try:
            transactions = self._run_async(self._account.get_history(self._session, **params))
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching cash transfers: {e}", exc_info=True)
            return []

        # Pass 1: collect withholding tax per (symbol, date) so it can be netted off
        # its OWN symbol's gross dividend and never emitted as a phantom negative one.
        tax_by_key = {}
        for txn in transactions:
            if getattr(txn, "transaction_sub_type", None) != "Dividend":
                continue
            net_value = float(getattr(txn, "net_value", 0) or 0)
            if net_value >= 0:
                continue
            key = (getattr(txn, "underlying_symbol", None) or getattr(txn, "symbol", None),
                   getattr(txn, "transaction_date", None))
            tax_by_key[key] = tax_by_key.get(key, 0.0) + abs(net_value)

        transfers = []
        for txn in transactions:
            external_id = str(getattr(txn, "id", "") or "")
            event_date = getattr(txn, "transaction_date", None)
            if not external_id or event_date is None:
                continue
            sub_type = getattr(txn, "transaction_sub_type", None)
            net_value = float(getattr(txn, "net_value", 0) or 0)
            description = getattr(txn, "description", None)

            if sub_type == "Dividend":
                if net_value <= 0:
                    continue  # a tax leg -- already netted onto its gross row
                symbol = getattr(txn, "underlying_symbol", None) or getattr(txn, "symbol", None)
                amount = round(net_value - tax_by_key.get((symbol, event_date), 0.0), 2)
                transfers.append(CashTransfer(
                    external_id=external_id, event_date=event_date,
                    event_type=CASH_TRANSFER_DIVIDEND, amount=amount,
                    symbol=symbol, description=description))
            elif sub_type in self._TT_DEPOSIT_SUB_TYPES and net_value > 0:
                transfers.append(CashTransfer(
                    external_id=external_id, event_date=event_date,
                    event_type=CASH_TRANSFER_DEPOSIT, amount=net_value,
                    description=description))
            elif sub_type in self._TT_WITHDRAWAL_SUB_TYPES and net_value < 0:
                # A DRIP leg is a "Withdrawal" that never left the account: it bought
                # shares with the dividend already recorded above. Emitting it would
                # double-count the cash going out.
                if "reinvest" in (description or "").lower():
                    continue
                transfers.append(CashTransfer(
                    external_id=external_id, event_date=event_date,
                    event_type=CASH_TRANSFER_WITHDRAWAL, amount=net_value,
                    description=description))

        logger.debug(f"[Account {self.id}] Retrieved {len(transfers)} cash transfers")
        return transfers
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 71 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement get_cash_transfers from Money Movement history"
```

---

### Task 54: `get_symbol_margin_info`

Three SDK inputs combine into one `MarginInfo` per symbol: `Account.get_margin_requirements()`
gives a per-underlying `initial_requirement` (the data behind TastyTrade's Cap Req screen), which
divided by the position's own notional yields the REAL initial margin rate for a **held** symbol;
`Equity.is_fractional_quantity_eligible` gives fractionability; `get_quantity_decimal_precisions()`
gives the equity trade increment.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py` (new method below `get_cash_transfers`)
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# get_symbol_margin_info
# ---------------------------------------------------------------------------

def _margin_report(*entries):
    """Stand-in for tastytrade MarginReport. `groups` legitimately contains EmptyDict
    placeholders, which carry no attributes at all."""
    return SimpleNamespace(groups=list(entries))


def _margin_entry(underlying_symbol, initial_requirement):
    return SimpleNamespace(underlying_symbol=underlying_symbol,
                           initial_requirement=Decimal(initial_requirement))


def _precision(minimum_increment_precision=5, symbol=None,
               instrument_type=TTInstrumentType.EQUITY):
    return SimpleNamespace(instrument_type=instrument_type, value=5, symbol=symbol,
                           minimum_increment_precision=minimum_increment_precision)


def _wire_margin_sources(acct, equities, report, precisions, positions):
    acct._account.get_margin_requirements = AsyncMock(return_value=report)
    acct._account.get_positions = AsyncMock(return_value=positions)
    return (
        patch("tastytrade.instruments.Equity.get", new=AsyncMock(return_value=equities)),
        patch("tastytrade.instruments.get_quantity_decimal_precisions",
              new=AsyncMock(return_value=precisions)),
    )


def test_symbol_margin_info_derives_the_real_rate_for_a_held_symbol():
    """initial_requirement / position notional is the actual Reg-T rate charged."""
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_POSITION

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct,
        equities=[_FakeEquity("AAPL")],
        # 10 shares marked at 155 = 1550 notional; 775 required = a 0.5 rate.
        report=_margin_report(_margin_entry("AAPL", "775")),
        precisions=[_precision(minimum_increment_precision=5)],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == pytest.approx(0.5)
    assert info["AAPL"].bp_factor == pytest.approx(1.0)  # 0.5 rate x 2:1 account
    assert info["AAPL"].source == MARGIN_SOURCE_POSITION


def test_symbol_margin_info_falls_back_to_the_account_multiplier_when_unheld():
    """Unheld symbols get bp_factor == the account multiplier -- exactly the caller's
    own conservative fallback, so nothing is over-committed."""
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_DEFAULT

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("MSFT")], report=_margin_report(),
        precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["MSFT"])

    assert info["MSFT"].bp_factor == 2.0
    assert info["MSFT"].initial_margin_rate is None
    assert info["MSFT"].source == MARGIN_SOURCE_DEFAULT


def test_symbol_margin_info_omits_a_symbol_the_broker_cannot_describe():
    """Omission, not a default -- the caller must know it fell back."""
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[], report=_margin_report(), precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["NOSUCH"])

    assert info == {}


def test_symbol_margin_info_reports_fractionability_and_increment():
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct,
        equities=[_FakeEquity("AAPL", is_fractional_quantity_eligible=True),
                  _FakeEquity("BRKA", is_fractional_quantity_eligible=False)],
        report=_margin_report(), precisions=[_precision(minimum_increment_precision=5)],
        positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL", "BRKA"])

    assert info["AAPL"].fractionable is True
    assert info["AAPL"].min_trade_increment == pytest.approx(1e-5)
    assert info["BRKA"].fractionable is False
    assert info["BRKA"].min_trade_increment == 1.0


def test_symbol_margin_info_skips_empty_margin_report_groups():
    """MarginReport.groups is `list[MarginReportEntry | EmptyDict]` -- the EmptyDict
    placeholders have no attributes at all."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")],
        report=_margin_report(SimpleNamespace(), _margin_entry("AAPL", "775")),
        precisions=[_precision()],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == pytest.approx(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k symbol_margin_info`

Expected: FAIL — four of the five fail with `KeyError: 'AAPL'` / `KeyError: 'MSFT'` (the
inherited base seam returns `{}`); `test_symbol_margin_info_omits_a_symbol_the_broker_cannot_describe`
passes vacuously.

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, insert this method immediately
**below** `get_cash_transfers`:

```python
    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        """Per-symbol margin / fractionability metadata, for buying-power sizing.

        Three SDK inputs are combined:
          * ``Account.get_margin_requirements()`` -> per-underlying
            ``initial_requirement``. Divided by the position's own notional (from
            ``get_positions``) that is the REAL initial margin rate for a HELD symbol
            -- the data behind TastyTrade's Cap Req screen.
          * ``Equity.is_fractional_quantity_eligible`` -> ``fractionable``.
          * ``get_quantity_decimal_precisions()`` -> the equity trade increment.

        A symbol with NO Equity record is OMITTED, never defaulted here -- the caller
        must know it fell back. A symbol that has an Equity record but is not held gets
        ``bp_factor = account multiplier``, which is EXACTLY the caller's own
        conservative fallback (assume no leverage), so reporting it over-commits
        nothing while still supplying real fractionability data.

        Args:
            symbols: symbols to describe; normalised here to ``.strip().upper()``.

        Returns:
            Dict[str, MarginInfo]: keyed by the normalised symbol.
        """
        from tastytrade.instruments import Equity, get_quantity_decimal_precisions

        wanted = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
        if not wanted or not self._check_authentication():
            return {}

        is_margin = self._is_margin_account()
        multiplier = 2.0 if is_margin else 1.0

        equities = {}
        try:
            found = self._run_async(Equity.get(self._session, wanted, page_offset=None))
            equities = {e.symbol.strip().upper(): e for e in found}
        except Exception as e:
            logger.warning(f"[Account {self.id}] Equity metadata fetch failed: {e}")

        increment = None
        try:
            for precision in self._run_async(get_quantity_decimal_precisions(self._session)):
                # The generic EQUITY row (symbol is None) is the one that applies to
                # every equity; per-symbol overrides are not needed for sizing.
                if precision.instrument_type == TTInstrumentType.EQUITY and precision.symbol is None:
                    increment = float(10 ** -int(precision.minimum_increment_precision))
                    break
        except Exception as e:
            logger.warning(f"[Account {self.id}] Quantity precision fetch failed: {e}")

        notional = {}
        for position in (self.get_positions() or []):
            if position.market_value:
                notional[position.symbol.strip().upper()] = abs(float(position.market_value))

        requirement = {}
        try:
            report = self._run_async(self._account.get_margin_requirements(self._session))
            for group in (getattr(report, "groups", None) or []):
                # `groups` is list[MarginReportEntry | EmptyDict]; the EmptyDict
                # placeholders carry no attributes, hence getattr with defaults.
                symbol = getattr(group, "underlying_symbol", None)
                initial = getattr(group, "initial_requirement", None)
                if symbol and initial is not None:
                    requirement[symbol.strip().upper()] = abs(float(initial))
        except Exception as e:
            logger.warning(f"[Account {self.id}] Margin requirement fetch failed: {e}")

        result = {}
        for symbol in wanted:
            equity = equities.get(symbol)
            if equity is None:
                continue
            rate = None
            source = MARGIN_SOURCE_DEFAULT
            if symbol in requirement and notional.get(symbol):
                rate = min(1.0, requirement[symbol] / notional[symbol])
                source = MARGIN_SOURCE_POSITION
            fractionable = bool(getattr(equity, "is_fractional_quantity_eligible", False))
            result[symbol] = MarginInfo(
                symbol=symbol,
                bp_factor=(rate * multiplier) if rate is not None else multiplier,
                # TastyTrade publishes no PER-SYMBOL marginability flag, so this
                # reports whether the ACCOUNT is a margin account.
                marginable=is_margin,
                fractionable=fractionable,
                min_order_size=None,
                min_trade_increment=increment if fractionable else 1.0,
                initial_margin_rate=rate,
                maintenance_margin_rate=None,
                source=source,
            )
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 76 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "feat(tastytrade): implement get_symbol_margin_info from margin report + equity metadata"
```

---

### Task 55: Bulk quotes via `get_market_data_by_type`, chunked at 100

`_get_instrument_current_price_impl`'s list branch (line 267) loops `get_market_data` one HTTP
round trip per symbol. `get_market_data_by_type` fetches a batch, but its **combined limit across
all instrument types is 100 per call** (`venv/lib/python3.12/site-packages/tastytrade/market_data.py:132`),
so the list must be chunked.

**Files:**
- Modify: `ba2_trade_platform/modules/accounts/TastyTradeAccount.py:244-292`
- Test: `tests/test_tastytrade_account.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tastytrade_account.py`:

```python
# ---------------------------------------------------------------------------
# Bulk quotes
# ---------------------------------------------------------------------------

def _market_data(symbol, bid="149.90", ask="150.10", mid="150.00", last="150.05",
                 close="148.00"):
    return SimpleNamespace(
        symbol=symbol,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        mid=Decimal(mid) if mid is not None else None,
        last=Decimal(last) if last is not None else None,
        close=Decimal(close) if close is not None else None,
    )


def test_bulk_quotes_chunk_at_the_hundred_symbol_api_limit():
    """get_market_data_by_type's COMBINED limit across all types is 100 per call."""
    acct = _bare_account()
    symbols = [f"SYM{i:03d}" for i in range(150)]
    bulk = AsyncMock(side_effect=lambda session, equities: [_market_data(s) for s in equities])

    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        acct._get_instrument_current_price_impl(symbols, price_type="mid")

    chunk_sizes = [len(call.kwargs["equities"]) for call in bulk.call_args_list]
    assert chunk_sizes == [100, 50]


def test_bulk_quotes_return_the_requested_price_type():
    acct = _bare_account()
    bulk = AsyncMock(return_value=[_market_data("AAPL", bid="149.90", ask="150.10")])

    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        prices = acct._get_instrument_current_price_impl(["AAPL"], price_type="ask")

    assert prices == {"AAPL": 150.10}


def test_bulk_quotes_leave_a_missing_symbol_as_none():
    """No fabricated price for a symbol the broker did not return."""
    acct = _bare_account()
    bulk = AsyncMock(return_value=[_market_data("AAPL")])

    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        prices = acct._get_instrument_current_price_impl(["AAPL", "NOSUCH"], price_type="mid")

    assert prices["AAPL"] == 150.00
    assert prices["NOSUCH"] is None


def test_bulk_quotes_survive_a_failing_chunk():
    acct = _bare_account()
    symbols = [f"SYM{i:03d}" for i in range(150)]

    def _fail_first(session, equities):
        if equities[0] == "SYM000":
            raise RuntimeError("gateway timeout")
        return [_market_data(s) for s in equities]

    bulk = AsyncMock(side_effect=_fail_first)
    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        prices = acct._get_instrument_current_price_impl(symbols, price_type="mid")

    assert prices["SYM000"] is None
    assert prices["SYM100"] == 150.00
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v -k bulk_quotes`

Expected: FAIL — all four fail because the current implementation calls the single-symbol
`get_market_data` instead: `assert [] == [100, 50]` on the chunking test, and
`assert {'AAPL': None} == {'AAPL': 150.1}` on the rest (the patched `get_market_data_by_type` is
never called).

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/modules/accounts/TastyTradeAccount.py`, replace the whole method at lines
244-292 (`def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):`
through its final `return None`) with:

```python
    #: get_market_data_by_type's COMBINED limit across ALL instrument types is 100 per
    #: call (tastytrade/market_data.py:132), so symbol lists are chunked.
    _MARKET_DATA_CHUNK = 100

    @staticmethod
    def _pick_price(data, price_type: str) -> Optional[float]:
        """Resolve one MarketData row to a price, falling back down the ladder.

        Returns ``None`` when the row carries no usable price -- never a fabricated
        number (platform rule: no fallback values for live data).
        """
        if price_type == 'bid' and data.bid:
            return float(data.bid)
        if price_type == 'ask' and data.ask:
            return float(data.ask)
        if price_type == 'mid' and data.mid:
            return float(data.mid)
        if data.last:
            return float(data.last)
        if data.close:
            return float(data.close)
        return None

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        """Fetch a single price or a bulk price map.

        The list branch uses ``get_market_data_by_type``, which returns a whole batch
        in ONE round trip -- chunked at ``_MARKET_DATA_CHUNK`` because the SDK's
        combined limit is 100 symbols per call. A failing chunk leaves its symbols at
        ``None`` rather than aborting the whole fetch.
        """
        if not self._check_authentication():
            if isinstance(symbol_or_symbols, list):
                return {s: None for s in symbol_or_symbols}
            return None

        from tastytrade.market_data import get_market_data, get_market_data_by_type
        from tastytrade.order import InstrumentType

        if isinstance(symbol_or_symbols, str):
            try:
                data = self._run_async(
                    get_market_data(self._session, symbol_or_symbols, InstrumentType.EQUITY))
                return self._pick_price(data, price_type)
            except Exception as e:
                logger.error(
                    f"[Account {self.id}] Error getting price for {symbol_or_symbols}: {e}",
                    exc_info=True)
                return None

        symbols = list(symbol_or_symbols)
        result = {s: None for s in symbols}
        for start in range(0, len(symbols), self._MARKET_DATA_CHUNK):
            chunk = symbols[start:start + self._MARKET_DATA_CHUNK]
            try:
                rows = self._run_async(
                    get_market_data_by_type(self._session, equities=chunk))
            except Exception as e:
                logger.warning(
                    f"[Account {self.id}] Bulk quote fetch failed for {len(chunk)} symbols "
                    f"starting at {chunk[0]}: {e}")
                continue
            for row in rows:
                if row.symbol in result:
                    result[row.symbol] = self._pick_price(row, price_type)
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_tastytrade_account.py -v`

Expected: PASS — 80 passed.

Then re-run the account and boot suites to confirm the re-parenting and the new surface did not
disturb anything:

```bash
venv/bin/python -m pytest tests/test_accounts/test_account_interface.py -v
venv/bin/python -m pytest tests/test_accounts/test_broker_error_handling.py -v
venv/bin/python -m pytest tests/test_boot_smoke.py -v
```

Expected: PASS all three.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/modules/accounts/TastyTradeAccount.py tests/test_tastytrade_account.py
git commit -m "perf(tastytrade): bulk quotes via get_market_data_by_type, chunked at 100"
```

---

## Section F — Page shell, gating and default view

This section builds `ba2_trade_platform/ui/pages/portfolio_allocation.py` and everything it
needs to decide *what* to show. Every decision the page makes is extracted into a pure sibling
module, `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (no NiceGUI, no DB, no
broker), which is where all the tests live. The widget drawing itself is eyeball-only and is
called out as such per task.

**Where things live, and why:**

| Piece | File | Testable? |
|---|---|---|
| `manual_trading_enabled` setting | `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py` | yes — pure, no DB |
| `get_symbols_by_label()` | `packages/common/ba2_common/core/utils.py` | yes — DB test |
| Gate / row-building / label-filter logic | `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` | yes — pure |
| Persistence | `packages/common/ba2_common/core/portfolio_allocation_store.py` | yes — DB test |
| Widgets | `ba2_trade_platform/ui/pages/portfolio_allocation.py` | no — eyeball only |

`ba2_trade_platform/ui/utils/` is chosen over `ui/pages/` on purpose: `ui/pages/__init__.py`
imports `overview`/`settings`, which pull the whole expert + LLM stack (an import that takes
**minutes**). `ui/utils/` holds only `perf_logger.py` today and imports in milliseconds.

**Depends on:** Section B (the store and the five models) and Task 26 (the engine shim, for
`PositionState` and `PositionFetchFailed`).

---

### Task 56: Declare the `manual_trading_enabled` account setting

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py:30-42`
- Test: `packages/common/tests/test_manual_trading_setting.py`

This is the page's gate. Declaring it once in `_ensure_builtin_settings` means every broker
inherits it and the existing generic settings dialog renders and saves it with **zero UI code**.
Pure-testable: no DB, no broker.

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_manual_trading_setting.py`:

```python
"""`manual_trading_enabled` — the Portfolio Allocation page's per-account gate.

Declared once on ReadOnlyAccountInterface so every broker inherits it and the
generic settings dialog renders/saves it with no UI code.

These tests touch no database and no broker: they exercise the settings-definition
merge and the never-saved-key read path only. The load-bearing behaviour is that
`settings.get(key, default)` does NOT work here (the settings property seeds every
declared key to None, so the default never applies) while
`get_setting_with_interface_default` does.
"""
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class StubAccount(ReadOnlyAccountInterface):
    """Concrete ReadOnlyAccountInterface with every abstract method filled in and a
    settings dict supplied by the test, so no DB is needed."""

    def __init__(self, stored_settings):
        self.id = 1
        self._stored = stored_settings

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_balance(self):
        return 0.0

    def get_account_info(self):
        return {}

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        return None

    def refresh_positions(self):
        return True

    def refresh_orders(self):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []


def test_manual_trading_enabled_is_declared_as_bool_defaulting_false():
    defs = StubAccount.get_merged_settings_definitions()
    assert "manual_trading_enabled" in defs, f"declared settings: {sorted(defs)}"
    assert defs["manual_trading_enabled"]["type"] == "bool"
    assert defs["manual_trading_enabled"]["default"] is False


def test_manual_trading_enabled_never_saved_reads_false_not_none():
    """The settings property seeds every DECLARED key to None, so
    `settings.get(key, False)` yields None — the trap. Only
    get_setting_with_interface_default falls back to the declared default."""
    acct = StubAccount({"manual_trading_enabled": None})
    assert acct.settings.get("manual_trading_enabled", False) is None
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is False


def test_manual_trading_enabled_saved_true_is_returned():
    acct = StubAccount({"manual_trading_enabled": True})
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is True


def test_manual_trading_enabled_saved_false_is_returned_not_treated_as_unset():
    """A deliberately-saved False must survive: False is not None."""
    acct = StubAccount({"manual_trading_enabled": False})
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is False


def test_manual_trading_enabled_string_none_is_treated_as_unset():
    """str(None) was historically written to the settings table; the literal
    string 'None' must not read back as a truthy flag."""
    acct = StubAccount({"manual_trading_enabled": "None"})
    assert acct.get_setting_with_interface_default(
        "manual_trading_enabled", log_warning=False) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_manual_trading_setting.py -v`

Expected: FAIL. `test_manual_trading_enabled_is_declared_as_bool_defaulting_false` fails with
`AssertionError: declared settings: ['minimum_equity_threshold_percent']`, and the other four
fail with
`ValueError: Setting 'manual_trading_enabled' not found in StubAccount interface definitions. Available settings: ['minimum_equity_threshold_percent']`

- [ ] **Step 3: Write minimal implementation**

In `packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py`, replace the whole
`_ensure_builtin_settings` method (lines 30-42) with:

```python
    @classmethod
    def _ensure_builtin_settings(cls):
        """Ensure builtin settings are initialized for account classes."""
        if not cls._builtin_settings:
            cls._builtin_settings = {
                "minimum_equity_threshold_percent": {
                    "type": "float",
                    "required": False,
                    "default": 5.0,
                    "description": "Minimum equity threshold (%)",
                    "tooltip": "Minimum percentage of account balance that must remain available across all experts before new positions are blocked. This is an account-wide safety net."
                },
                "manual_trading_enabled": {
                    "type": "bool",
                    "required": False,
                    "default": False,
                    "description": "Manually traded account",
                    "tooltip": "Enable the Portfolio Allocation page for this account. Only for accounts you trade by hand -- the page refuses to run when the account has any enabled expert."
                },
            }
```

The new key MUST go inside this same dict literal: the body is guarded by
`if not cls._builtin_settings:`, so a second `if`-block or a post-hoc `.update()` would never run
once the dict is populated.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_manual_trading_setting.py -v`
Expected: PASS — 5 passed.

Then check nothing else broke:
Run: `venv/bin/python -m pytest tests/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py packages/common/tests/test_manual_trading_setting.py
git commit -m "feat(accounts): declare manual_trading_enabled builtin account setting"
```

---

### Task 57: `get_symbols_by_label()` — the inverse of `get_labels_by_symbol`

**Files:**
- Modify: `packages/common/ba2_common/core/utils.py` (insert after `remove_label_from_instruments` and before `expert_uses_risk_manager`)
- Modify: `packages/common/tests/test_utils_pure.py` (the `expected` list only)
- Test: `tests/test_instrument_labels.py`

The page needs `{label: [symbols]}` for its expansions. A managed label with no instruments must
map to an **empty list, with the key present** — that is how the page tells "managed but empty"
from "not managed".

Two gates in `packages/common/tests/test_utils_pure.py` apply: a subprocess leak check
(`:32-38`) asserting `import ba2_common.core.utils` pulls in no live-tree package, and the
explicit list of pure helpers. So the new helper MUST use the same **lazy**
`from ba2_common.core.models import Instrument` inside the function body that the other four
label helpers use, and MUST be added to that list. **Do not touch that test's docstring again** —
Task 1 already reworded it.

- [ ] **Step 1: Write the failing test**

Extend the import at the top of `tests/test_instrument_labels.py` from:

```python
from ba2_trade_platform.core.utils import (
    add_label_to_instruments, remove_label_from_instruments,
    get_labels_by_symbol, get_all_instrument_labels,
)
```

to:

```python
from ba2_trade_platform.core.utils import (
    add_label_to_instruments, remove_label_from_instruments,
    get_labels_by_symbol, get_all_instrument_labels, get_symbols_by_label,
)
```

Then append these methods to the existing `class TestInstrumentLabels`:

```python
    def test_get_symbols_by_label_returns_sorted_symbols_per_label(self):
        add_label_to_instruments(['MSFT', 'AAPL'], 'ARK26')
        add_label_to_instruments(['NVDA'], 'NASDAQ30')
        out = get_symbols_by_label(['ARK26', 'NASDAQ30'])
        assert out['ARK26'] == ['AAPL', 'MSFT']
        assert out['NASDAQ30'] == ['NVDA']

    def test_get_symbols_by_label_symbol_with_two_labels_appears_under_both(self):
        add_label_to_instruments(['TSLA'], 'ARK26')
        add_label_to_instruments(['TSLA'], 'HighRisk')
        out = get_symbols_by_label(['ARK26', 'HighRisk'])
        assert out['ARK26'] == ['TSLA']
        assert out['HighRisk'] == ['TSLA']

    def test_get_symbols_by_label_label_with_no_instruments_maps_to_empty_list(self):
        add_label_to_instruments(['AMZN'], 'ARK26')
        out = get_symbols_by_label(['ARK26', 'NOBODY_HAS_THIS'])
        assert out['ARK26'] == ['AMZN']
        # The key is PRESENT: "managed but empty" must be distinguishable from
        # "not managed" by the caller.
        assert out['NOBODY_HAS_THIS'] == []

    def test_get_symbols_by_label_blank_and_none_labels_are_ignored(self):
        add_label_to_instruments(['GOOG'], 'ARK26')
        out = get_symbols_by_label(['ARK26', '   ', None])
        assert list(out.keys()) == ['ARK26']

    def test_get_symbols_by_label_normalises_symbols_to_upper(self):
        add_label_to_instruments(['spce'], 'ARK26')
        assert get_symbols_by_label(['ARK26'])['ARK26'] == ['SPCE']

    def test_get_symbols_by_label_is_the_inverse_of_get_labels_by_symbol(self):
        add_label_to_instruments(['IBM'], 'ARK26')
        assert 'ARK26' in get_labels_by_symbol(['IBM'])['IBM']
        assert 'IBM' in get_symbols_by_label(['ARK26'])['ARK26']
```

Also, in `packages/common/tests/test_utils_pure.py`, extend the `expected` list. Replace the two
lines Task 1 left:

```python
        "get_expert_options_for_ui",
        "normalize_symbol", "parse_instrument_symbol_list",
```

with:

```python
        "get_expert_options_for_ui",
        "normalize_symbol", "parse_instrument_symbol_list",
        "get_symbols_by_label",
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_instrument_labels.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'get_symbols_by_label' from 'ba2_trade_platform.core.utils'`

Run: `venv/bin/python -m pytest packages/common/tests/test_utils_pure.py -v`
Expected: FAIL with `AssertionError: missing pure helper: get_symbols_by_label`

- [ ] **Step 3: Write minimal implementation**

Insert into `packages/common/ba2_common/core/utils.py` between the end of
`remove_label_from_instruments` (`    return changed`) and `def expert_uses_risk_manager`:

```python
def get_symbols_by_label(labels) -> Dict[str, List[str]]:
    """Return ``{label: [symbols]}`` for each requested label.

    The inverse of ``get_labels_by_symbol``. A requested label with no instruments
    maps to an EMPTY list -- the key is ALWAYS present, so the caller can tell
    "managed but empty" from "not managed". Symbols are normalised
    (.strip().upper()) and returned sorted; a blank/None label is ignored.

    Labels live in a plain JSON column, so this scans the instrument table exactly
    as ``get_all_instrument_labels`` does. The ``Instrument`` import is LAZY (same
    as the other label helpers) to keep this module free of live-tree imports.
    """
    from ba2_common.core.models import Instrument
    if isinstance(labels, str):
        labels = [labels]
    wanted: List[str] = []
    for lbl in (labels or []):
        text = (lbl or "").strip()
        if text and text not in wanted:
            wanted.append(text)
    out: Dict[str, List[str]] = {lbl: [] for lbl in wanted}
    if not wanted:
        return out
    wanted_set = set(wanted)
    with get_db() as session:
        for inst in session.exec(select(Instrument)).all():
            name = normalize_symbol(inst.name)
            if not name:
                continue
            for lbl in (inst.labels or []):
                if lbl in wanted_set:
                    out[lbl].append(name)
    for lbl in out:
        out[lbl] = sorted(set(out[lbl]))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_instrument_labels.py -v`
Expected: PASS — 19 passed (8 original + 5 from Task 1 + 6 here).

Run: `venv/bin/python -m pytest packages/common/tests/test_utils_pure.py -v`
Expected: PASS — the leak gate still reports CLEAN.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/utils.py packages/common/tests/test_utils_pure.py tests/test_instrument_labels.py
git commit -m "feat(utils): add get_symbols_by_label pure helper"
```

---

### Task 58: Route `/portfolioallocation`, sidebar entry, and the page shell

**Files:**
- Create: `ba2_trade_platform/ui/utils/__init__.py` (empty — the directory currently has no `__init__.py`)
- Create: `ba2_trade_platform/ui/pages/portfolio_allocation.py`
- Modify: `ba2_trade_platform/ui/menus.py` (whole file)
- Modify: `ba2_trade_platform/ui/main.py:2` and `:128`
- Test: `tests/test_portfolio_allocation_route.py`

`menu_items` is currently a local inside `sidemenu()`, so it cannot be asserted on. Hoist it to a
module-level `MENU_ITEMS` (a one-line refactor) so the navigation contract is testable. The route
itself is asserted **structurally, via AST** — importing `ba2_trade_platform.ui.main` pulls every
page module and through them the LLM/expert stack, which does not finish inside five minutes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_route.py`:

```python
"""Portfolio Allocation is reachable: a sidebar entry and a '/portfolioallocation' route.

`ba2_trade_platform.ui.main` and `ba2_trade_platform.ui.pages` are deliberately NOT
imported: both pull every page module (and through them the expert/LLM stack), an
import that does not complete in minutes. `ui.menus` is cheap (nicegui + svg only)
and is imported for real; the route and the page's entry point are asserted
structurally by parsing the source with `ast`.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "ba2_trade_platform" / "ui" / "main.py"
PAGE_PY = REPO_ROOT / "ba2_trade_platform" / "ui" / "pages" / "portfolio_allocation.py"


def _decorated_routes(source: str):
    """[(route_path, function_name)] for every @ui.page('...')-decorated function."""
    routes = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "page"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                routes.append((dec.args[0].value, node.name))
    return routes


def _toplevel_function_names(source: str):
    tree = ast.parse(source)
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_sidebar_menu_has_portfolio_allocation_entry():
    from ba2_trade_platform.ui.menus import MENU_ITEMS
    entry = next((i for i in MENU_ITEMS if i["route"] == "/portfolioallocation"), None)
    assert entry is not None, f"routes present: {[i['route'] for i in MENU_ITEMS]}"
    assert entry["icon"] == "pie_chart"
    assert entry["label"] == "Portfolio Allocation"


def test_sidebar_menu_keeps_every_pre_existing_entry():
    from ba2_trade_platform.ui.menus import MENU_ITEMS
    routes = {i["route"] for i in MENU_ITEMS}
    assert {"/", "/marketanalysis", "/activitymonitor",
            "/livetrades", "/tools", "/settings"} <= routes


def test_main_registers_the_portfolio_allocation_route():
    routes = dict(_decorated_routes(MAIN_PY.read_text(encoding="utf-8")))
    assert "/portfolioallocation" in routes, f"registered routes: {sorted(routes)}"


def test_portfolio_allocation_page_module_exposes_content():
    names = _toplevel_function_names(PAGE_PY.read_text(encoding="utf-8"))
    assert "content" in names, f"top-level functions: {sorted(names)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_route.py -v`
Expected: FAIL — `ImportError: cannot import name 'MENU_ITEMS' from 'ba2_trade_platform.ui.menus'`
for the two menu tests, `AssertionError: registered routes: ['/', '/activitymonitor', ...]` for
the route test, and `FileNotFoundError: ... ui/pages/portfolio_allocation.py` for the last.

- [ ] **Step 3: Write minimal implementation**

Create an empty `ba2_trade_platform/ui/utils/__init__.py` (the directory currently relies on
PEP 420 implicit namespace packaging; making it a regular package is explicit and harmless):

```python
```

Replace the whole of `ba2_trade_platform/ui/menus.py` with:

```python
from nicegui import ui
from . import svg


# Module-level so the navigation contract is unit-testable without rendering
# anything (importing ui.main to inspect routes pulls the whole expert stack).
MENU_ITEMS = [
    {'icon': 'dashboard', 'label': 'Overview', 'route': '/', 'description': 'Dashboard & Stats'},
    {'icon': 'analytics', 'label': 'Market Analysis', 'route': '/marketanalysis', 'description': 'Experts Analysis'},
    {'icon': 'receipt_long', 'label': 'Activity Monitor', 'route': '/activitymonitor', 'description': 'System Logs'},
    {'icon': 'trending_up', 'label': 'Live Trades', 'route': '/livetrades', 'description': 'Active Positions'},
    {'icon': 'pie_chart', 'label': 'Portfolio Allocation', 'route': '/portfolioallocation', 'description': 'Manual Rebalancing'},
    {'icon': 'build', 'label': 'Tools', 'route': '/tools', 'description': 'Utilities'},
    {'icon': 'settings', 'label': 'Settings', 'route': '/settings', 'description': 'Configuration'},
]


def sidemenu() -> None:
    """Modern sidebar navigation menu"""
    with ui.column().classes('w-full gap-1 px-2'):
        for item in MENU_ITEMS:
            with ui.item(on_click=lambda r=item['route']: ui.navigate.to(r)).classes('rounded-lg hover:bg-white/10'):
                with ui.item_section().props('avatar'):
                    ui.icon(item['icon']).classes('text-accent')
                with ui.item_section():
                    ui.item_label(item['label']).classes('text-white font-medium')
                    ui.item_label(item['description']).props('caption').classes('text-secondary-custom text-xs')


def topmenu() -> None:
    """Top bar navigation actions"""
    with ui.row().classes('items-center gap-2'):
        # GitHub link
        with ui.link(target='https://github.com/bmigette/BA2TradePlatform').classes('max-[365px]:hidden').tooltip('GitHub'):
            svg.github().classes('fill-white scale-125 m-1')
```

In `ba2_trade_platform/ui/main.py`, replace line 2:

```python
from .pages import overview, settings, marketanalysis, market_analysis_detail, rulesettest, marketanalysishistory, smart_risk_manager_detail, activity_monitor, live_trades, tools
```

with:

```python
from .pages import overview, settings, marketanalysis, market_analysis_detail, rulesettest, marketanalysishistory, smart_risk_manager_detail, activity_monitor, live_trades, tools, portfolio_allocation
```

and insert this block between line 127 (the blank line after `tools.content()`) and line 129
(`STATICPATH = ...`):

```python
@ui.page('/portfolioallocation')
async def portfolio_allocation_page() -> None:
    logger.debug("[ROUTE] /portfolioallocation - Loading portfolio allocation page")
    with layout_render('Portfolio Allocation'):
        await portfolio_allocation.content()

```

Create `ba2_trade_platform/ui/pages/portfolio_allocation.py`:

```python
"""Portfolio Allocation page — manually traded accounts only.

Shows the account's current allocation, grouped by the instrument labels the user
chose to manage, and lets those labels, symbols and comments be edited. Every
decision this page makes lives in the pure, unit-tested module
``ba2_trade_platform/ui/utils/portfolio_allocation_view.py``; this file only does
IO (broker + DB) and draws widgets.

This repo uses no ``ui.refreshable`` / ``ui.stepper`` / ``ui.aggrid``: refresh is
``container.clear()`` followed by rebuilding inside ``with container:``. Blocking
broker work goes through ``asyncio.to_thread``.
"""
from nicegui import ui

from ...logger import logger
from ..account_filter_context import get_selected_account_id


async def content() -> None:
    """Entry point for the /portfolioallocation route."""
    account_id = get_selected_account_id()
    logger.debug(f"[PAGE] portfolio_allocation.content() account_id={account_id}")

    with ui.column().classes('w-full gap-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('📊 Portfolio Allocation').classes('text-h6')
            ui.label('Manually traded accounts only').classes('text-xs text-secondary-custom')
        ui.label(f'Selected account: {account_id if account_id is not None else "All accounts"}'
                 ).classes('text-secondary-custom')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_route.py -v`
Expected: PASS — 4 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/__init__.py ba2_trade_platform/ui/menus.py ba2_trade_platform/ui/main.py ba2_trade_platform/ui/pages/portfolio_allocation.py tests/test_portfolio_allocation_route.py
git commit -m "feat(ui): add /portfolioallocation route, sidebar entry and page shell"
```

---

### Task 59: The pure gate, and the page's three empty states

**Files:**
- Create: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation.py` (whole file)
- Test: `tests/test_portfolio_allocation_view.py`

`evaluate_gate` is pure — three inputs, one `GateResult` out — so all four states are unit-tested
with no UI. Rendering the result is eyeball-only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_view.py`:

```python
"""Pure view-model logic for the Portfolio Allocation page.

Nothing here imports NiceGUI, opens a database or talks to a broker: the page
hands plain data to these functions and draws whatever comes back.
"""
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK,
    evaluate_gate,
)


def test_gate_no_account_selected_is_blocked_with_no_account_reason():
    """The header selector on 'All accounts' yields account_id None."""
    gate = evaluate_gate(None, True, [])
    assert gate.allowed is False
    assert gate.reason_code == GATE_NO_ACCOUNT
    assert gate.message


def test_gate_manual_flag_off_is_blocked_with_not_manual_reason():
    gate = evaluate_gate(7, False, [])
    assert gate.allowed is False
    assert gate.reason_code == GATE_NOT_MANUAL
    assert gate.expert_names == []


def test_gate_enabled_experts_block_and_are_named_in_the_message():
    gate = evaluate_gate(7, True, ["TradingAgents #3", "PennyMomentum"])
    assert gate.allowed is False
    assert gate.reason_code == GATE_HAS_EXPERTS
    assert gate.expert_names == ["TradingAgents #3", "PennyMomentum"]
    assert "TradingAgents #3" in gate.message
    assert "PennyMomentum" in gate.message


def test_gate_manual_account_with_no_enabled_experts_is_allowed():
    gate = evaluate_gate(7, True, [])
    assert gate.allowed is True
    assert gate.reason_code == GATE_OK
    assert gate.expert_names == []


def test_gate_no_account_takes_precedence_over_every_other_problem():
    """'Pick an account' is the only actionable message when nothing is selected —
    we cannot even know whether the account is manual or has experts."""
    gate = evaluate_gate(None, False, ["SomeExpert"])
    assert gate.reason_code == GATE_NO_ACCOUNT


def test_gate_blank_expert_names_are_dropped_and_do_not_block():
    gate = evaluate_gate(7, True, ["", None])
    assert gate.allowed is True
    assert gate.reason_code == GATE_OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: FAIL at collection with
`ModuleNotFoundError: No module named 'ba2_trade_platform.ui.utils.portfolio_allocation_view'`

- [ ] **Step 3: Write minimal implementation**

Create `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`:

```python
"""Pure view-model helpers for the Portfolio Allocation page.

No NiceGUI, no database, no broker SDK: plain data in, plain data out, so every
decision the page makes is unit-testable without a browser. The page module
(``ui/pages/portfolio_allocation.py``) does the IO and hands the results here.

Lives under ``ui/utils/`` rather than beside the page because
``ui/pages/__init__.py`` imports the whole page set (and through it the LLM/expert
stack); ``ui/utils/`` holds only perf_logger and imports in milliseconds.
"""
from dataclasses import dataclass, field
from typing import List, Optional

# ---- gate reason codes (exact spellings; use these, never bare literals) ----

GATE_OK = "OK"
GATE_NO_ACCOUNT = "NO_ACCOUNT"
GATE_NOT_MANUAL = "NOT_MANUAL"
GATE_HAS_EXPERTS = "HAS_EXPERTS"


@dataclass
class GateResult:
    """Whether the page may run for the current selection, and why not if not.

    ``allowed is False`` means the page renders ``message`` and NOTHING else --
    no broker calls, no plan. ``expert_names`` is populated only for
    ``GATE_HAS_EXPERTS``.
    """
    allowed: bool
    reason_code: str
    message: str
    expert_names: List[str] = field(default_factory=list)


def evaluate_gate(account_id: Optional[int],
                  has_manual_flag: bool,
                  enabled_expert_names) -> GateResult:
    """Decide whether Portfolio Allocation may run. Pure; never raises.

    Precedence is deliberate and tested: with no account selected we cannot even
    know whether the account is manual or expert-driven, so "pick an account" is
    the only actionable message and it wins.

    Args:
        account_id: the global account filter's value; ``None`` == "All accounts".
        has_manual_flag: the account's ``manual_trading_enabled`` setting, read via
            ``get_setting_with_interface_default('manual_trading_enabled',
            log_warning=False)`` -- NOT ``settings.get(...)``, which returns None
            for a never-saved key.
        enabled_expert_names: display names of the account's ENABLED experts.
            Blank/None entries are dropped.

    Returns:
        GateResult: ``allowed=True`` only when an account is selected, it is
        flagged manual, and it has no enabled expert.
    """
    names = [n for n in (enabled_expert_names or []) if n]

    if account_id is None:
        return GateResult(
            allowed=False,
            reason_code=GATE_NO_ACCOUNT,
            message="Pick a single account in the header selector — portfolio "
                    "allocation is computed per account.",
        )

    if not has_manual_flag:
        return GateResult(
            allowed=False,
            reason_code=GATE_NOT_MANUAL,
            message="This account is not flagged as manually traded. Tick "
                    "'Manually traded account' in its Settings to enable this page.",
        )

    if names:
        return GateResult(
            allowed=False,
            reason_code=GATE_HAS_EXPERTS,
            message="This account has enabled experts (" + ", ".join(names) +
                    "). Disable them in Settings before allocating by hand — "
                    "otherwise the experts and this page would fight over the "
                    "same buying power.",
            expert_names=list(names),
        )

    return GateResult(allowed=True, reason_code=GATE_OK, message="")
```

Replace the whole of `ba2_trade_platform/ui/pages/portfolio_allocation.py` with:

```python
"""Portfolio Allocation page — manually traded accounts only.

Shows the account's current allocation, grouped by the instrument labels the user
chose to manage, and lets those labels, symbols and comments be edited. Every
decision this page makes lives in the pure, unit-tested module
``ba2_trade_platform/ui/utils/portfolio_allocation_view.py``; this file only does
IO (broker + DB) and draws widgets.

This repo uses no ``ui.refreshable`` / ``ui.stepper`` / ``ui.aggrid``: refresh is
``container.clear()`` followed by rebuilding inside ``with container:``. Blocking
broker work goes through ``asyncio.to_thread``.
"""
import asyncio
from typing import List, Optional

from nicegui import ui
from sqlmodel import select

from ...core.db import get_db
from ...core.models import ExpertInstance
from ...core.utils import get_account_instance_from_id
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ..utils.portfolio_allocation_view import GATE_NO_ACCOUNT, GateResult, evaluate_gate


def _enabled_expert_names(account_id: int) -> List[str]:
    """Display names of the account's ENABLED experts; empty list when there are none."""
    with get_db() as session:
        rows = session.exec(
            select(ExpertInstance).where(
                ExpertInstance.account_id == account_id,
                ExpertInstance.enabled == True,  # noqa: E712 — SQL boolean, not identity
            )
        ).all()
        return [(r.alias or r.expert) for r in rows]


def _load_gate(account_id: Optional[int]) -> GateResult:
    """Resolve the three gate inputs (blocking; call via asyncio.to_thread).

    An account that cannot be instantiated is reported as "not manual" rather than
    crashing the page — the user's next action (open Settings) is the same either way.
    """
    if account_id is None:
        return evaluate_gate(None, False, [])
    try:
        account = get_account_instance_from_id(account_id)
    except Exception as e:
        logger.error(f"Portfolio allocation: cannot load account {account_id}: {e}", exc_info=True)
        account = None
    if account is None:
        return evaluate_gate(account_id, False, [])
    manual = bool(account.get_setting_with_interface_default(
        'manual_trading_enabled', log_warning=False))
    return evaluate_gate(account_id, manual, _enabled_expert_names(account_id))


def _render_gate_blocked(gate: GateResult) -> None:
    """Draw the empty state for a blocked gate (eyeball-only; logic is in evaluate_gate)."""
    with ui.card().classes('w-full'):
        with ui.row().classes('items-center gap-2'):
            ui.icon('block').classes('text-accent')
            ui.label('Portfolio Allocation is not available for this selection').classes('text-h6')
        ui.label(gate.message).classes('text-secondary-custom')
        if gate.reason_code != GATE_NO_ACCOUNT:
            with ui.row().classes('mt-2'):
                ui.button('Open Settings', icon='settings',
                          on_click=lambda: ui.navigate.to('/settings')).props('outline')


async def content() -> None:
    """Entry point for the /portfolioallocation route."""
    account_id = get_selected_account_id()
    logger.debug(f"[PAGE] portfolio_allocation.content() account_id={account_id}")

    with ui.column().classes('w-full gap-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('📊 Portfolio Allocation').classes('text-h6')
            ui.label('Manually traded accounts only').classes('text-xs text-secondary-custom')

        gate = await asyncio.to_thread(_load_gate, account_id)
        if not gate.allowed:
            _render_gate_blocked(gate)
            return

        with ui.element('div').classes('alert-banner info w-full p-3'):
            ui.label('Gate passed — allocation view lands next.')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 6 passed.

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_route.py -v`
Expected: PASS (the page still exposes `content`).

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py ba2_trade_platform/ui/pages/portfolio_allocation.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): gate the portfolio allocation page on account, manual flag and experts"
```

---

### Task 60: `positions_by_symbol()` — a failed fetch is not a flat account

**Files:**
- Modify: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (append)
- Test: `tests/test_portfolio_allocation_view.py` (append)

`get_positions()` returning `None` means the broker fetch **failed**; `[]` means genuinely flat.
Conflating them once mass-closed 8 real open transactions (2026-07-03). This function makes the
distinction impossible to skip: `None` raises `PositionFetchFailed`, which is defined once in the
pure engine (`ba2_common/core/portfolio_allocation.py`, Task 16) so the UI and the live service
share one class.

- [ ] **Step 1: Write the failing test**

Extend the existing import block in `tests/test_portfolio_allocation_view.py` to:

```python
from ba2_trade_platform.core.portfolio_allocation import PositionFetchFailed
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK,
    evaluate_gate, positions_by_symbol,
)
```

and append:

```python
import pytest
from types import SimpleNamespace


def test_positions_by_symbol_none_raises_position_fetch_failed():
    """None from get_positions() is a FETCH FAILURE, never a flat account."""
    with pytest.raises(PositionFetchFailed):
        positions_by_symbol(None)


def test_positions_by_symbol_empty_list_is_a_genuinely_flat_account():
    assert positions_by_symbol([]) == {}


def test_positions_by_symbol_reads_broker_objects_and_normalises_symbols():
    raw = [SimpleNamespace(symbol=' aapl ', qty=10.0, cost_basis=1500.0, market_value=1800.0)]
    out = positions_by_symbol(raw)
    assert list(out) == ['AAPL']
    assert out['AAPL'].quantity == 10.0
    assert out['AAPL'].cost_basis == 1500.0
    assert out['AAPL'].market_value == 1800.0


def test_positions_by_symbol_reads_dicts_as_well_as_objects():
    out = positions_by_symbol([{'symbol': 'MSFT', 'qty': 3, 'cost_basis': 900,
                                'market_value': 1000}])
    assert out['MSFT'].quantity == 3.0
    assert out['MSFT'].cost_basis == 900.0


def test_positions_by_symbol_sums_duplicate_rows_for_one_symbol():
    out = positions_by_symbol([
        {'symbol': 'NVDA', 'qty': 2, 'cost_basis': 200, 'market_value': 260},
        {'symbol': 'NVDA', 'qty': 3, 'cost_basis': 330, 'market_value': 390},
    ])
    assert out['NVDA'].quantity == 5.0
    assert out['NVDA'].cost_basis == 530.0
    assert out['NVDA'].market_value == 650.0


def test_positions_by_symbol_missing_quantity_raises_rather_than_defaulting():
    """Platform rule: no fallback values for quantities/balances — raise instead."""
    with pytest.raises(ValueError) as exc:
        positions_by_symbol([{'symbol': 'AAPL', 'cost_basis': 100, 'market_value': 120}])
    assert 'AAPL' in str(exc.value)


def test_positions_by_symbol_missing_cost_basis_raises_rather_than_defaulting():
    with pytest.raises(ValueError) as exc:
        positions_by_symbol([{'symbol': 'AAPL', 'qty': 1, 'market_value': 120}])
    assert 'AAPL' in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'positions_by_symbol' from 'ba2_trade_platform.ui.utils.portfolio_allocation_view'`

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`, replace the typing import line

```python
from typing import List, Optional
```

with:

```python
from typing import Any, Dict, List, Optional

from ...core.portfolio_allocation import PositionFetchFailed, PositionState
```

and append to the end of the file:

```python
def _probe(obj: Any, name: str) -> Any:
    """Read ``name`` off a dict OR an object, tolerantly (brokers return both)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def positions_by_symbol(raw_positions) -> Dict[str, PositionState]:
    """Turn a broker position list into ``{SYMBOL: PositionState}``.

    Args:
        raw_positions: whatever ``account.get_positions()`` returned — a list of
            Position rows (objects or dicts), ``[]`` for a genuinely flat account,
            or ``None`` for a FAILED fetch.

    Returns:
        Dict[str, PositionState]: keyed by normalised (.strip().upper()) symbol.
        Duplicate rows for one symbol are summed.

    Raises:
        PositionFetchFailed: when ``raw_positions`` is ``None``. Defined in the
            pure engine so the live service and this module raise the same class.
        ValueError: when a row has no quantity or no cost basis — no fallback
            values for quantities or balances (platform rule).
    """
    if raw_positions is None:
        raise PositionFetchFailed(
            "get_positions() returned None: the broker fetch FAILED. No allocation "
            "may be computed against an unknown book — an empty list would mean "
            "genuinely flat, None does not."
        )

    out: Dict[str, PositionState] = {}
    for row in raw_positions:
        raw_symbol = _probe(row, 'symbol')
        if not raw_symbol:
            continue
        symbol = str(raw_symbol).strip().upper()

        quantity = _probe(row, 'qty')
        if quantity is None:
            quantity = _probe(row, 'quantity')
        if quantity is None:
            raise ValueError(f"Position for {symbol} has no quantity — refusing to "
                             f"substitute a default")

        cost_basis = _probe(row, 'cost_basis')
        if cost_basis is None:
            raise ValueError(f"Position for {symbol} has no cost basis — refusing to "
                             f"substitute a default")

        market_value = _probe(row, 'market_value')

        state = out.get(symbol)
        if state is None:
            state = PositionState(symbol=symbol)
            out[symbol] = state
        state.quantity += float(quantity)
        state.cost_basis += float(cost_basis)
        if market_value is not None:
            state.market_value = (state.market_value or 0.0) + float(market_value)

    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 13 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): positions_by_symbol distinguishes a failed fetch from a flat account"
```

---

### Task 61: `build_label_views()` — the default allocation view

**Files:**
- Modify: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (append)
- Test: `tests/test_portfolio_allocation_view.py` (append)

One row per symbol in a managed label: current value, `% of label`, `% of total`, quantity, live
price, market value. A symbol in two managed labels is flagged and counted **once** in the total.
Task 66 adds the `valuation_mode` keyword; this task establishes the cost-basis behaviour that
becomes `cost` mode. Fully pure-testable; the `ui.expansion` that displays it is eyeball-only
(Task 64).

- [ ] **Step 1: Write the failing test**

Extend the import block in `tests/test_portfolio_allocation_view.py` to:

```python
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK,
    ManagedLabel, build_label_views, evaluate_gate, positions_by_symbol,
)
```

and append:

```python
def _pos(symbol, quantity, cost_basis, market_value=None):
    """A PositionState as positions_by_symbol would have produced it."""
    return positions_by_symbol([{'symbol': symbol, 'qty': quantity,
                                 'cost_basis': cost_basis,
                                 'market_value': market_value}])[symbol]


def test_build_label_views_computes_pct_of_label_and_pct_of_total():
    managed = [ManagedLabel('ARK26', 40.0), ManagedLabel('NASDAQ30', 60.0)]
    symbols_by_label = {'ARK26': ['AAPL', 'MSFT'], 'NASDAQ30': ['NVDA']}
    positions = {'AAPL': _pos('AAPL', 10, 6000.0),
                 'MSFT': _pos('MSFT', 5, 2000.0),
                 'NVDA': _pos('NVDA', 4, 2000.0)}
    views = build_label_views(managed, symbols_by_label, positions, {})

    ark = views[0]
    assert ark.label == 'ARK26'
    assert ark.current_value == 8000.0
    assert ark.pct_of_total == 80.0
    aapl = next(r for r in ark.rows if r.symbol == 'AAPL')
    assert aapl.pct_of_label == 75.0
    assert aapl.pct_of_total == 60.0
    msft = next(r for r in ark.rows if r.symbol == 'MSFT')
    assert msft.pct_of_label == 25.0
    assert msft.pct_of_total == 20.0

    nasdaq = views[1]
    assert nasdaq.rows[0].pct_of_label == 100.0
    assert nasdaq.rows[0].pct_of_total == 20.0


def test_build_label_views_symbol_with_no_position_is_listed_with_zeroes():
    """Symbols with no position must still appear — they are editable targets."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'TSLA']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {})
    tsla = next(r for r in views[0].rows if r.symbol == 'TSLA')
    assert tsla.quantity == 0.0
    assert tsla.cost_basis == 0.0
    assert tsla.pct_of_label == 0.0


def test_build_label_views_symbol_in_two_labels_is_flagged_and_counted_once():
    """Decision 7: targets sum, but the managed total must not double count."""
    views = build_label_views(
        [ManagedLabel('ARK26', 50.0), ManagedLabel('HighRisk', 50.0)],
        {'ARK26': ['TSLA'], 'HighRisk': ['TSLA']},
        {'TSLA': _pos('TSLA', 10, 6000.0)},
        {},
    )
    row = views[0].rows[0]
    assert row.multi_label is True
    assert row.labels == ['ARK26', 'HighRisk']
    # Counted once: TSLA is 100% of total, not 50%.
    assert row.pct_of_total == 100.0


def test_build_label_views_empty_label_has_no_rows_and_zero_current_value():
    views = build_label_views([ManagedLabel('EMPTY', 25.0)], {'EMPTY': []}, {}, {})
    assert views[0].rows == []
    assert views[0].current_value == 0.0
    assert views[0].pct_of_total == 0.0


def test_build_label_views_uses_live_price_for_market_value():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=1100.0)},
                              {'AAPL': 250.0})
    assert views[0].rows[0].price == 250.0
    assert views[0].rows[0].market_value == 2500.0


def test_build_label_views_missing_price_falls_back_to_broker_market_value():
    """No price is NOT a guessed price: the broker's own market value is real data,
    and a symbol with neither reports None."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'TSLA']},
                              {'AAPL': _pos('AAPL', 10, 1000.0, market_value=1100.0)},
                              {'AAPL': None, 'TSLA': None})
    aapl = next(r for r in views[0].rows if r.symbol == 'AAPL')
    tsla = next(r for r in views[0].rows if r.symbol == 'TSLA')
    assert aapl.price is None and aapl.market_value == 1100.0
    assert tsla.price is None and tsla.market_value is None


def test_build_label_views_rows_are_ordered_by_current_value_descending():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL', 'MSFT', 'NVDA']},
                              {'AAPL': _pos('AAPL', 1, 100.0),
                               'MSFT': _pos('MSFT', 1, 900.0),
                               'NVDA': _pos('NVDA', 1, 500.0)},
                              {})
    assert [r.symbol for r in views[0].rows] == ['MSFT', 'NVDA', 'AAPL']


def test_build_label_views_attaches_per_symbol_comments():
    views = build_label_views([ManagedLabel('ARK26', 100.0, comment='core basket')],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 1, 100.0)},
                              {},
                              symbol_comments={('ARK26', 'AAPL'): 'trim on strength'})
    assert views[0].comment == 'core basket'
    assert views[0].rows[0].comment == 'trim on strength'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'ManagedLabel' from 'ba2_trade_platform.ui.utils.portfolio_allocation_view'`

- [ ] **Step 3: Write minimal implementation**

Append to `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`:

```python
@dataclass
class ManagedLabel:
    """One managed label as the page reads it out of ``portfolio_allocation_label``."""
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None


@dataclass
class SymbolRow:
    """One symbol's line in the default view.

    ``current_value`` is what the account holds in this symbol under the active
    valuation mode (cost basis, or ``qty x price`` -- see Task 66);
    ``pct_of_label`` and ``pct_of_total`` are 1-100 of it. ``price`` is ``None``
    when no quote is available; ``market_value`` then falls back to the broker's
    own figure and is ``None`` when there is neither (never a guessed number).
    """
    symbol: str
    labels: List[str] = field(default_factory=list)
    quantity: float = 0.0
    cost_basis: float = 0.0
    current_value: float = 0.0
    price: Optional[float] = None
    market_value: Optional[float] = None
    pct_of_label: float = 0.0
    pct_of_total: float = 0.0
    comment: Optional[str] = None

    @property
    def multi_label(self) -> bool:
        """True when this symbol carries more than one MANAGED label (⚠ in the UI)."""
        return len(self.labels) > 1


@dataclass
class LabelView:
    """A managed label's expansion: its totals and its symbol rows."""
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None
    current_value: float = 0.0
    cost_basis: float = 0.0
    market_value: Optional[float] = None
    pct_of_total: float = 0.0
    rows: List[SymbolRow] = field(default_factory=list)


def build_label_views(managed,
                      symbols_by_label,
                      positions,
                      prices,
                      symbol_comments=None) -> List[LabelView]:
    """Build the default view: one LabelView per managed label. Pure.

    Current value is the COST BASIS here; Task 66 adds a ``valuation_mode``
    keyword so ``market`` mode can measure the same positions at ``qty x price``.

    Args:
        managed: ``List[ManagedLabel]`` in display order.
        symbols_by_label: ``{label: [symbols]}`` from ``get_symbols_by_label`` — a
            managed label with no instruments maps to an empty list and yields a
            LabelView with no rows.
        positions: ``{SYMBOL: PositionState}`` from ``positions_by_symbol``. A
            symbol absent here is flat, NOT unknown (the caller must already have
            refused a ``None`` fetch).
        prices: ``{SYMBOL: price or None}`` from the bulk quote call.
        symbol_comments: ``{(label, symbol): comment}``; optional.

    Returns:
        List[LabelView]: labels in the given order, rows within each ordered by
        current value descending then symbol. ``pct_of_total`` is computed against
        the DISTINCT managed value, so a symbol in two labels is counted once.
    """
    comments = symbol_comments or {}

    def _clean(label: str) -> List[str]:
        seen, out = set(), []
        for sym in (symbols_by_label or {}).get(label, []) or []:
            s = (sym or "").strip().upper()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    # Membership first, so a multi-label symbol knows all of its managed labels.
    membership: Dict[str, List[str]] = {}
    for entry in managed:
        for sym in _clean(entry.label):
            membership.setdefault(sym, [])
            if entry.label not in membership[sym]:
                membership[sym].append(entry.label)

    total_value = 0.0
    for sym in membership:
        state = positions.get(sym)
        if state is not None:
            total_value += state.cost_basis

    views: List[LabelView] = []
    for entry in managed:
        symbols = _clean(entry.label)
        label_value = sum(positions[s].cost_basis for s in symbols if s in positions)
        label_market_value: Optional[float] = None
        rows: List[SymbolRow] = []

        for sym in symbols:
            state = positions.get(sym)
            quantity = state.quantity if state is not None else 0.0
            cost_basis = state.cost_basis if state is not None else 0.0
            price = (prices or {}).get(sym)
            if price is not None:
                market_value = quantity * price
            elif state is not None:
                market_value = state.market_value
            else:
                market_value = None
            if market_value is not None:
                label_market_value = (label_market_value or 0.0) + market_value

            rows.append(SymbolRow(
                symbol=sym,
                labels=list(membership.get(sym, [entry.label])),
                quantity=quantity,
                cost_basis=cost_basis,
                current_value=cost_basis,
                price=price,
                market_value=market_value,
                pct_of_label=(cost_basis / label_value * 100.0) if label_value > 0 else 0.0,
                pct_of_total=(cost_basis / total_value * 100.0) if total_value > 0 else 0.0,
                comment=comments.get((entry.label, sym)),
            ))

        rows.sort(key=lambda r: (-r.current_value, r.symbol))
        views.append(LabelView(
            label=entry.label,
            target_pct=entry.target_pct,
            comment=entry.comment,
            current_value=label_value,
            cost_basis=label_value,
            market_value=label_market_value,
            pct_of_total=(label_value / total_value * 100.0) if total_value > 0 else 0.0,
            rows=rows,
        ))

    return views
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 21 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): build_label_views computes the default allocation rows"
```

---

### Task 62: `filter_selectable_labels()` — hide the machine tags

**Files:**
- Modify: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (append)
- Test: `tests/test_portfolio_allocation_view.py` (append)

`get_all_instrument_labels()` on the live database returns ~31 labels, of which the majority are
machine tags: `auto_added`, `expert_selected`, `ai_selected`, `not_found`, plus the numbered
expert families `penny-17`, `penny-4`, `tradingagents-16`, `fmprating-18` and friends. Only
`ARK26`, `NASDAQ30`, `HighRisk`, `sp500`, `Penny` etc. are user labels. Note `Penny` (no `-N`) is
a *user* label and must survive. Pure, and the test data is taken from the real label
distribution.

- [ ] **Step 1: Write the failing test**

Extend the import block in `tests/test_portfolio_allocation_view.py` to add
`filter_selectable_labels` and `is_machine_label`:

```python
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    GATE_HAS_EXPERTS, GATE_NOT_MANUAL, GATE_NO_ACCOUNT, GATE_OK,
    ManagedLabel, build_label_views, evaluate_gate, filter_selectable_labels,
    is_machine_label, positions_by_symbol,
)
```

and append:

```python
# The label set actually present in the live database on 2026-08-20.
LIVE_LABELS = [
    'auto_added', 'expert_selected', 'Penny', 'penny-17', 'sp500', 'ARK26',
    'ai_selected', 'fmprating-18', 'penny-4', 'NASDAQ30', 'HighRisk', 'not_found',
    'tradingagents-16', 'ai_selector', 'tech', 'megacap',
]


def test_filter_selectable_labels_hides_the_four_machine_tags():
    out = filter_selectable_labels(LIVE_LABELS)
    for tag in ('auto_added', 'expert_selected', 'ai_selected', 'not_found'):
        assert tag not in out


def test_filter_selectable_labels_hides_the_numbered_expert_families():
    out = filter_selectable_labels(LIVE_LABELS)
    for tag in ('penny-17', 'penny-4', 'fmprating-18', 'tradingagents-16'):
        assert tag not in out


def test_filter_selectable_labels_keeps_user_labels_including_bare_penny():
    """'Penny' with no -N index is a user basket, not a machine tag."""
    out = filter_selectable_labels(LIVE_LABELS)
    assert set(out) == {'Penny', 'sp500', 'ARK26', 'NASDAQ30', 'HighRisk',
                        'ai_selector', 'tech', 'megacap'}


def test_filter_selectable_labels_show_all_is_the_escape_hatch():
    out = filter_selectable_labels(LIVE_LABELS, show_all=True)
    assert 'auto_added' in out
    assert 'penny-17' in out
    assert len(out) == len(LIVE_LABELS)


def test_filter_selectable_labels_is_sorted_case_insensitively_and_deduped():
    out = filter_selectable_labels(['zeta', 'ARK26', 'alpha', 'ARK26', '  ', None])
    assert out == ['alpha', 'ARK26', 'zeta']


def test_is_machine_label_is_case_insensitive():
    assert is_machine_label('AUTO_ADDED') is True
    assert is_machine_label('Penny-4') is True
    assert is_machine_label('Penny') is False
    assert is_machine_label('') is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'filter_selectable_labels' from 'ba2_trade_platform.ui.utils.portfolio_allocation_view'`

- [ ] **Step 3: Write minimal implementation**

Add `import re` as the first line of the import block in
`ba2_trade_platform/ui/utils/portfolio_allocation_view.py`, then append:

```python
#: Machine-written instrument tags that must not appear in the managed-label picker.
MACHINE_LABELS = frozenset({'auto_added', 'expert_selected', 'ai_selected', 'not_found'})

#: Per-expert-instance tags written by InstrumentAutoAdder: 'penny-17',
#: 'tradingagents-16', 'fmprating-18'. The bare family name without an index
#: ('Penny') is a USER label and is deliberately NOT matched.
MACHINE_LABEL_FAMILY_RE = re.compile(r'^(?:penny|tradingagents|fmprating)-\d+$',
                                     re.IGNORECASE)


def is_machine_label(label) -> bool:
    """True when ``label`` was written by the platform rather than by the user.

    Case-insensitive on both the exact tags and the numbered families. A blank or
    ``None`` label is not a machine label (it is simply dropped by the caller).
    """
    text = (label or "").strip()
    if not text:
        return False
    if text.lower() in MACHINE_LABELS:
        return True
    return bool(MACHINE_LABEL_FAMILY_RE.match(text))


def filter_selectable_labels(all_labels, show_all: bool = False) -> List[str]:
    """The labels offered in the managed-label picker. Pure.

    Args:
        all_labels: everything ``get_all_instrument_labels()`` returned.
        show_all: the picker's escape hatch — when True nothing is hidden, so a
            user who really does want to manage 'auto_added' still can.

    Returns:
        List[str]: de-duplicated, blank-stripped, sorted case-insensitively.
    """
    seen, kept = set(), []
    for label in (all_labels or []):
        text = (label or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if show_all or not is_machine_label(text):
            kept.append(text)
    return sorted(kept, key=lambda s: s.lower())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 27 passed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): filter machine tags out of the managed-label picker"
```

---

### Task 63: Store — bulk label selection, symbol membership and comments

> **A comment-only write silently zeroes a symbol's weight — this task as written hits it.**
> `set_symbol_weight` creates the row with the model default `weight_pct=0.0`, and
> `get_symbol_weights` treats the EXISTENCE of a row as an explicit weight. This task's UI code does
> `set_symbol_weight(account_id, label, symbol, comment=value or "")`, so a user typing a note on an
> unweighted symbol drops it from its even-split default to a hard 0% — it stops receiving any
> allocation. No test here asserts a weight after a comment-only write, so it would land silently.
>
> Fix by passing the symbol's current EFFECTIVE weight alongside the comment. Do **not** try to treat
> `weight_pct == 0.0` as "unstored": that breaks a legitimate explicit 0% and re-introduces drift
> from the engine's `build_symbol_targets`. Add a test asserting the weight is unchanged after a
> comment-only write.


The page needs four things the store does not have yet: replace the whole managed-label set in
one call (the picker), read back per-symbol comments, and add/remove symbols from a label.

**Two deliberate non-additions.** A label comment is already `set_managed_label(account_id,
label, comment=...)` (Task 9) and a symbol comment is already `set_symbol_weight(account_id,
label, symbol, comment=...)` (Task 10) — there is no separate `set_label_comment` /
`set_symbol_comment`. One writer per row.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation_store.py` (append)
- Test: `tests/test_portfolio_allocation_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_portfolio_allocation_store.py`:

```python


# --- page helpers: bulk label selection and symbol membership --------------

def _instrument_labels(symbol):
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db as _get_db
    from ba2_trade_platform.core.models import Instrument
    with _get_db() as session:
        inst = session.exec(select(Instrument).where(Instrument.name == symbol)).first()
        return list(inst.labels) if inst else None


def test_replace_managed_labels_creates_rows_in_selection_order(account_id):
    store.replace_managed_labels(account_id, ['NASDAQ30', 'ARK26'])
    labels = store.get_managed_labels(account_id)
    assert [r.label for r in labels] == ['NASDAQ30', 'ARK26']
    assert all(r.target_pct == 0.0 for r in labels)


def test_replace_managed_labels_is_idempotent_and_reports_no_change(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    assert store.replace_managed_labels(account_id, ['ARK26']) == {'added': 0, 'removed': 0}
    assert [r.label for r in store.get_managed_labels(account_id)] == ['ARK26']


def test_replace_managed_labels_unmanaging_deletes_the_symbol_rows(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', comment='core holding')
    assert len(store.get_symbol_rows(account_id, 'ARK26')) == 1

    assert store.replace_managed_labels(account_id, []) == {'added': 0, 'removed': 1}
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, 'ARK26') == {}


def test_replace_managed_labels_is_scoped_per_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name='Other Account')
    store.replace_managed_labels(account_id, ['ARK26'])
    store.replace_managed_labels(other.id, ['NASDAQ30'])
    assert [r.label for r in store.get_managed_labels(account_id)] == ['ARK26']
    assert [r.label for r in store.get_managed_labels(other.id)] == ['NASDAQ30']


def test_get_symbol_comments_returns_only_symbols_that_have_one(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    assert store.get_symbol_comments(account_id, 'ARK26') == {}
    store.set_symbol_weight(account_id, 'ARK26', ' tsla ', comment='trim on strength')
    store.set_symbol_weight(account_id, 'ARK26', 'PLTR', weight_pct=25.0)
    assert store.get_symbol_comments(account_id, 'ARK26') == {'TSLA': 'trim on strength'}


def test_add_symbols_to_label_labels_the_instruments_normalised(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    assert store.add_symbols_to_label(account_id, 'ARK26', [' tsla ', 'roku']) == 2
    assert _instrument_labels('TSLA') == ['ARK26']
    assert _instrument_labels('ROKU') == ['ARK26']


def test_remove_symbols_from_label_unlabels_and_deletes_the_symbol_row(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    store.add_symbols_to_label(account_id, 'ARK26', ['TSLA'])
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', comment='core holding')

    assert store.remove_symbols_from_label(account_id, 'ARK26', ['tsla']) == 1
    assert _instrument_labels('TSLA') == []
    assert store.get_symbol_rows(account_id, 'ARK26') == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: FAIL — `AttributeError: module 'ba2_common.core.portfolio_allocation_store' has no attribute 'replace_managed_labels'`

- [ ] **Step 3: Write minimal implementation**

Append to the end of `packages/common/ba2_common/core/portfolio_allocation_store.py`:

```python


# ---------------------------------------------------------------------------
# Page helpers: bulk label selection, symbol membership, comment reads
# ---------------------------------------------------------------------------

def replace_managed_labels(account_id: int, labels) -> Dict[str, int]:
    """Make ``labels`` EXACTLY the account's managed set, in the given order.

    This is the label-picker's writer; ``set_managed_label`` remains the writer
    for ONE label's target/comment. Unmanaging a label also deletes that account's
    lazy symbol rows for it (the live DB runs with ``PRAGMA foreign_keys = 0``, so
    nothing cascades on its own).

    Returns:
        Dict[str, int]: ``{'added': n, 'removed': n}``. Re-saving the same
        selection returns zeroes, which lets an eager on-change handler skip a
        pointless write.
    """
    wanted: List[str] = []
    for label in (labels or []):
        text = (label or "").strip()
        if text and text not in wanted:
            wanted.append(text)

    added = removed = 0
    with get_db() as session:
        existing = session.exec(
            select(PortfolioAllocationLabel)
            .where(PortfolioAllocationLabel.account_id == account_id)
        ).all()
        by_label = {row.label: row for row in existing}

        for label, row in list(by_label.items()):
            if label in wanted:
                continue
            for srow in session.exec(select(PortfolioAllocationSymbol).where(
                    PortfolioAllocationSymbol.account_id == account_id,
                    PortfolioAllocationSymbol.label == label)).all():
                session.delete(srow)
            session.delete(row)
            removed += 1

        for order, label in enumerate(wanted):
            row = by_label.get(label)
            if row is None:
                session.add(PortfolioAllocationLabel(
                    account_id=account_id, label=label, target_pct=0.0, sort_order=order))
                added += 1
            elif row.sort_order != order:
                row.sort_order = order
                session.add(row)

        session.commit()

    logger.info(f"Managed labels for account {account_id}: +{added} / -{removed} -> {wanted}")
    return {'added': added, 'removed': removed}


def get_symbol_comments(account_id: int, label: str) -> Dict[str, str]:
    """``{SYMBOL: comment}`` for one managed label; symbols with no comment are omitted."""
    return {symbol: row.comment
            for symbol, row in get_symbol_rows(account_id, label).items()
            if row.comment}


def add_symbols_to_label(account_id: int, label: str, symbols) -> int:
    """Give ``symbols`` the instrument label ``label``. Returns instruments changed.

    Instrument labels are GLOBAL (they live on the ``instrument`` row), so this
    also affects any other account managing the same label. ``account_id`` is
    accepted and logged for auditability.
    """
    from ba2_common.core.utils import add_label_to_instruments

    lbl = (label or "").strip()
    syms = _normalise_symbols(symbols)
    if not lbl or not syms:
        return 0
    changed = add_label_to_instruments(syms, lbl)
    logger.info(f"Account {account_id}: added label '{lbl}' to "
                f"{changed}/{len(syms)} instrument(s)")
    return changed


def remove_symbols_from_label(account_id: int, label: str, symbols) -> int:
    """Drop the instrument label AND delete this account's lazy symbol rows.

    Returns the number of instruments whose label list changed.
    """
    from ba2_common.core.utils import remove_label_from_instruments

    lbl = (label or "").strip()
    syms = _normalise_symbols(symbols)
    if not lbl or not syms:
        return 0
    changed = remove_label_from_instruments(syms, lbl)
    with get_db() as session:
        rows = session.exec(select(PortfolioAllocationSymbol).where(
            PortfolioAllocationSymbol.account_id == account_id,
            PortfolioAllocationSymbol.label == lbl,
            PortfolioAllocationSymbol.symbol.in_(syms))).all()
        for row in rows:
            session.delete(row)
        if rows:
            session.commit()
    logger.info(f"Account {account_id}: removed label '{lbl}' from "
                f"{changed}/{len(syms)} instrument(s)")
    return changed
```

The two `ba2_common.core.utils` imports are LOCAL to their functions so the store's module-level
import graph stays `db` + `models` + `logger`.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v`
Expected: PASS — 48 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation_store.py tests/test_portfolio_allocation_store.py
git commit -m "feat(allocation): bulk label selection, symbol membership and comment reads"
```

---

### Task 64: `collect_managed_symbols()` and the default view render

**Files:**
- Modify: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (append)
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation.py` (whole file)
- Test: `tests/test_portfolio_allocation_view.py` (append)

`collect_managed_symbols` is the bulk-quote request list — one
`get_instrument_current_price(symbols)` call for the whole page, deduplicated so a symbol in two
labels is quoted once. It is pure and tested here; the `ui.expansion`/`ui.table` rendering that
consumes it is **eyeball-only**.

- [ ] **Step 1: Write the failing test**

Extend the import block in `tests/test_portfolio_allocation_view.py` to add
`collect_managed_symbols`, and append:

```python
def test_collect_managed_symbols_dedupes_across_labels():
    """A symbol in two managed labels must be quoted once, not twice."""
    out = collect_managed_symbols({'ARK26': ['TSLA', 'ROKU'], 'HighRisk': ['TSLA']})
    assert out == ['ROKU', 'TSLA']


def test_collect_managed_symbols_normalises_and_sorts():
    out = collect_managed_symbols({'ARK26': [' tsla ', 'aapl']})
    assert out == ['AAPL', 'TSLA']


def test_collect_managed_symbols_drops_blanks_and_empty_labels():
    out = collect_managed_symbols({'ARK26': ['AAPL', '', None], 'EMPTY': []})
    assert out == ['AAPL']


def test_collect_managed_symbols_of_nothing_is_empty():
    assert collect_managed_symbols({}) == []
    assert collect_managed_symbols(None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'collect_managed_symbols' from 'ba2_trade_platform.ui.utils.portfolio_allocation_view'`

- [ ] **Step 3: Write minimal implementation**

Append to `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`:

```python
def collect_managed_symbols(symbols_by_label) -> List[str]:
    """Every distinct symbol across the managed labels, normalised and sorted.

    This is the bulk-quote request list for
    ``account.get_instrument_current_price(symbols)``: ONE call for the whole page,
    deduplicated so a symbol carrying two managed labels is quoted once.
    """
    out = set()
    for symbols in (symbols_by_label or {}).values():
        for sym in (symbols or []):
            text = (sym or "").strip().upper()
            if text:
                out.add(text)
    return sorted(out)
```

Replace the whole of `ba2_trade_platform/ui/pages/portfolio_allocation.py` with:

```python
"""Portfolio Allocation page — manually traded accounts only.

Shows the account's current allocation, grouped by the instrument labels the user
chose to manage. Every decision this page makes lives in the pure, unit-tested
module ``ba2_trade_platform/ui/utils/portfolio_allocation_view.py``; this file only
does IO (broker + DB) and draws widgets.

Two house rules are load-bearing here:

* ``get_positions()`` returning ``None`` means the broker fetch FAILED, while
  ``[]`` means genuinely flat. ``positions_by_symbol`` raises
  ``PositionFetchFailed`` on ``None`` and this page shows an error banner instead
  of pretending the account is empty.
* Prices come from ``get_instrument_current_price`` in ONE bulk, cached call, and
  work for symbols with no position. Alpaca's default feed is ``delayed_sip``
  (15 minutes delayed), which the page states next to the data.

This repo uses no ``ui.refreshable`` / ``ui.stepper`` / ``ui.aggrid``: refresh is
``container.clear()`` followed by rebuilding inside ``with container:``. Blocking
broker work goes through ``asyncio.to_thread``.
"""
import asyncio
from typing import Any, Dict, List, Optional

from nicegui import ui
from sqlmodel import select

from ...core.db import get_db
from ...core.models import ExpertInstance
from ...core.portfolio_allocation_store import get_managed_labels, get_symbol_comments
from ...core.utils import get_account_instance_from_id, get_symbols_by_label
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ..utils.portfolio_allocation_view import (
    GATE_NO_ACCOUNT, GateResult, ManagedLabel, PositionFetchFailed,
    build_label_views, collect_managed_symbols, evaluate_gate, positions_by_symbol,
)


# ---------------------------------------------------------------------------
# Blocking IO (always called through asyncio.to_thread)
# ---------------------------------------------------------------------------

def _enabled_expert_names(account_id: int) -> List[str]:
    """Display names of the account's ENABLED experts; empty list when there are none."""
    with get_db() as session:
        rows = session.exec(
            select(ExpertInstance).where(
                ExpertInstance.account_id == account_id,
                ExpertInstance.enabled == True,  # noqa: E712 — SQL boolean, not identity
            )
        ).all()
        return [(r.alias or r.expert) for r in rows]


def _load_gate(account_id: Optional[int]) -> GateResult:
    """Resolve the three gate inputs.

    An account that cannot be instantiated is reported as "not manual" rather than
    crashing the page — the user's next action (open Settings) is the same either way.
    """
    if account_id is None:
        return evaluate_gate(None, False, [])
    try:
        account = get_account_instance_from_id(account_id)
    except Exception as e:
        logger.error(f"Portfolio allocation: cannot load account {account_id}: {e}", exc_info=True)
        account = None
    if account is None:
        return evaluate_gate(account_id, False, [])
    manual = bool(account.get_setting_with_interface_default(
        'manual_trading_enabled', log_warning=False))
    return evaluate_gate(account_id, manual, _enabled_expert_names(account_id))


def _load_view_payload(account_id: int) -> Dict[str, Any]:
    """One render's worth of data: managed labels, membership, positions, prices.

    Raises:
        PositionFetchFailed: the broker position fetch failed (NOT a flat account).
        RuntimeError: the account could not be instantiated.
    """
    managed = [ManagedLabel(label=row.label, target_pct=row.target_pct, comment=row.comment)
               for row in get_managed_labels(account_id)]
    symbols_by_label = get_symbols_by_label([m.label for m in managed])
    symbols = collect_managed_symbols(symbols_by_label)

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")

    positions = positions_by_symbol(account.get_positions())

    prices: Dict[str, Optional[float]] = {}
    if symbols:
        fetched = account.get_instrument_current_price(symbols)
        if isinstance(fetched, dict):
            prices = dict(fetched)
        else:
            logger.warning(f"Bulk price fetch returned {type(fetched).__name__}, "
                           f"expected a dict — rendering without prices")

    comments: Dict[tuple, str] = {}
    for entry in managed:
        for symbol, text in get_symbol_comments(account_id, entry.label).items():
            comments[(entry.label, symbol)] = text

    return {
        'views': build_label_views(managed, symbols_by_label, positions, prices, comments),
        'symbols_by_label': symbols_by_label,
    }


# ---------------------------------------------------------------------------
# Rendering (eyeball-only; all decisions already made above)
# ---------------------------------------------------------------------------

def _render_gate_blocked(gate: GateResult) -> None:
    with ui.card().classes('w-full'):
        with ui.row().classes('items-center gap-2'):
            ui.icon('block').classes('text-accent')
            ui.label('Portfolio Allocation is not available for this selection').classes('text-h6')
        ui.label(gate.message).classes('text-secondary-custom')
        if gate.reason_code != GATE_NO_ACCOUNT:
            with ui.row().classes('mt-2'):
                ui.button('Open Settings', icon='settings',
                          on_click=lambda: ui.navigate.to('/settings')).props('outline')


def _render_label_body(view) -> None:
    """One managed label's symbol table."""
    if view.comment:
        ui.label(view.comment).classes('text-xs text-secondary-custom')

    rows = [{
        'flag': '⚠' if r.multi_label else '',
        'symbol': r.symbol,
        'labels': ', '.join(r.labels),
        'current_value': round(r.current_value, 2),
        'pct_of_label': round(r.pct_of_label, 2),
        'pct_of_total': round(r.pct_of_total, 2),
        'quantity': round(r.quantity, 4),
        'cost_basis': round(r.cost_basis, 2),
        'price': None if r.price is None else round(r.price, 4),
        'market_value': None if r.market_value is None else round(r.market_value, 2),
        'comment': r.comment or '',
    } for r in view.rows]

    columns = [
        {'name': 'flag', 'label': '', 'field': 'flag', 'align': 'center'},
        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
        {'name': 'labels', 'label': 'Labels', 'field': 'labels', 'align': 'left'},
        {'name': 'current_value', 'label': 'Current value', 'field': 'current_value', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_label', 'label': '% of label', 'field': 'pct_of_label', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_total', 'label': '% of total', 'field': 'pct_of_total', 'sortable': True, 'align': 'right'},
        {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'sortable': True, 'align': 'right'},
        {'name': 'cost_basis', 'label': 'Cost basis', 'field': 'cost_basis', 'sortable': True, 'align': 'right'},
        {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True, 'align': 'right'},
        {'name': 'market_value', 'label': 'Market value', 'field': 'market_value', 'sortable': True, 'align': 'right'},
        {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'},
    ]

    table = ui.table(columns=columns, rows=rows, row_key='symbol').classes('w-full dark-pagination')
    table.add_slot('body-cell-flag', r'''
        <q-td :props="props">
            <span v-if="props.value" :title="'Also in: ' + props.row.labels"
                  style="color:#f6ad55;font-weight:600">{{ props.value }}</span>
        </q-td>
    ''')


def _render_labels(payload: Dict[str, Any]) -> None:
    views = payload['views']
    if not views:
        with ui.element('div').classes('alert-banner info w-full p-3'):
            ui.label('No labels are managed for this account yet.')
        return

    total = sum(v.current_value for v in views)
    with ui.row().classes('w-full gap-4'):
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed value').classes('text-xs text-secondary-custom')
            ui.label(f'${total:,.2f}').classes('text-lg font-bold')
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed labels').classes('text-xs text-secondary-custom')
            ui.label(str(len(views))).classes('text-lg font-bold')

    ui.label('Prices are the broker feed (Alpaca defaults to delayed_sip — 15 minutes '
             'delayed). Only symbols carrying a managed label are listed.'
             ).classes('text-xs text-secondary-custom')

    for view in views:
        header = (f'{view.label} — ${view.current_value:,.2f} '
                  f'({view.pct_of_total:.1f}% of managed, target {view.target_pct:.1f}%)')
        with ui.expansion(header, icon='label').classes('w-full'):
            _render_label_body(view)


async def content() -> None:
    """Entry point for the /portfolioallocation route."""
    account_id = get_selected_account_id()
    logger.debug(f"[PAGE] portfolio_allocation.content() account_id={account_id}")

    with ui.column().classes('w-full gap-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('📊 Portfolio Allocation').classes('text-h6')
            ui.label('Manually traded accounts only').classes('text-xs text-secondary-custom')

        gate = await asyncio.to_thread(_load_gate, account_id)
        if not gate.allowed:
            _render_gate_blocked(gate)
            return

        toolbar = ui.row().classes('w-full items-center gap-2')
        body = ui.column().classes('w-full gap-3')

        async def _refresh() -> None:
            body.clear()
            with body:
                ui.spinner(size='lg').classes('self-center')
            try:
                payload = await asyncio.to_thread(_load_view_payload, account_id)
            except PositionFetchFailed as e:
                logger.error(f"Portfolio allocation: position fetch failed: {e}")
                body.clear()
                with body:
                    with ui.element('div').classes('alert-banner danger w-full p-3'):
                        ui.label(f'Broker position fetch FAILED: {e}')
                        ui.label('Nothing is shown until the broker answers — a failed '
                                 'fetch and a flat account are not the same thing.'
                                 ).classes('text-xs text-secondary-custom')
                return
            except Exception as e:
                logger.error(f"Portfolio allocation refresh failed: {e}", exc_info=True)
                body.clear()
                with body:
                    with ui.element('div').classes('alert-banner danger w-full p-3'):
                        ui.label(f'Could not load allocation: {e}')
                return
            body.clear()
            with body:
                _render_labels(payload)

        with toolbar:
            ui.button('Refresh', icon='refresh', on_click=_refresh).props('outline')

        await _refresh()
```

`PositionFetchFailed` is re-exported from the view module (it imports it from the engine in
Task 60), so the page has a single import surface.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 31 passed.

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_route.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py ba2_trade_platform/ui/pages/portfolio_allocation.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): render the default allocation view per managed label"
```

---

### Task 65: `diff_managed_labels()`, the label picker, symbol add/remove and comments

**Files:**
- Modify: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (append)
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation.py` (whole file)
- Test: `tests/test_portfolio_allocation_view.py` (append)

`diff_managed_labels` is what the picker's `on_change` needs to persist eagerly and report what
changed — pure and tested. The dialogs and inline comment inputs that call it are
**eyeball-only**.

Everything persists **on change**, not behind a Save button: switching the global account calls
`ui.run_javascript('window.location.reload()')` (`ui/layout.py:124`), so the page never gets a
chance to flush pending edits.

- [ ] **Step 1: Write the failing test**

Extend the import block in `tests/test_portfolio_allocation_view.py` to add
`diff_managed_labels`, and append:

```python
def test_diff_managed_labels_reports_additions_and_removals():
    to_add, to_remove = diff_managed_labels(['ARK26', 'HighRisk'], ['ARK26', 'NASDAQ30'])
    assert to_add == ['NASDAQ30']
    assert to_remove == ['HighRisk']


def test_diff_managed_labels_unchanged_selection_is_two_empty_lists():
    """Eager persistence fires on every change event; an unchanged selection must
    be a no-op rather than a pointless write."""
    assert diff_managed_labels(['ARK26'], ['ARK26']) == ([], [])


def test_diff_managed_labels_is_order_independent():
    assert diff_managed_labels(['A', 'B'], ['B', 'A']) == ([], [])


def test_diff_managed_labels_from_nothing_adds_everything():
    to_add, to_remove = diff_managed_labels([], ['ARK26', 'HighRisk'])
    assert to_add == ['ARK26', 'HighRisk']
    assert to_remove == []


def test_diff_managed_labels_ignores_blank_and_none_entries():
    to_add, to_remove = diff_managed_labels(['ARK26', None], ['ARK26', '  '])
    assert (to_add, to_remove) == ([], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: FAIL at collection with
`ImportError: cannot import name 'diff_managed_labels' from 'ba2_trade_platform.ui.utils.portfolio_allocation_view'`

- [ ] **Step 3: Write minimal implementation**

Append to `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`:

```python
def diff_managed_labels(current, selected):
    """Return ``(to_add, to_remove)`` for a managed-label selection change. Pure.

    Both sides are normalised (stripped, de-duplicated, blank/None dropped) and the
    results are sorted, so persistence is order-independent and idempotent:
    re-saving the same selection returns two empty lists, which lets the eager
    on-change handler skip a pointless write.
    """
    cur = {s.strip() for s in (current or []) if s and s.strip()}
    sel = {s.strip() for s in (selected or []) if s and s.strip()}
    return sorted(sel - cur), sorted(cur - sel)
```

In `ba2_trade_platform/ui/pages/portfolio_allocation.py`, make four changes.

(a) Extend the store import to:

```python
from ...core.portfolio_allocation_store import (
    add_symbols_to_label, get_managed_labels, get_symbol_comments,
    remove_symbols_from_label, replace_managed_labels, set_managed_label,
    set_symbol_weight,
)
```

(b) Extend the utils import to:

```python
from ...core.utils import (
    get_account_instance_from_id, get_all_instrument_labels, get_symbols_by_label,
)
```

(c) Extend the view import to:

```python
from ..utils.portfolio_allocation_view import (
    GATE_NO_ACCOUNT, GateResult, ManagedLabel, PositionFetchFailed,
    build_label_views, collect_managed_symbols, diff_managed_labels, evaluate_gate,
    filter_selectable_labels, positions_by_symbol,
)
```

(d) Insert these blocking loaders and eager-persistence handlers immediately after
`_load_view_payload`, replace `_render_label_body` and `_render_labels`, and rewrite `content`'s
toolbar:

```python
def _load_picker_data(account_id: int) -> Dict[str, List[str]]:
    """Current managed labels plus every label in use, for the picker dialog."""
    return {
        'current': [row.label for row in get_managed_labels(account_id)],
        'all_labels': get_all_instrument_labels(),
    }


# ---------------------------------------------------------------------------
# Eager persistence handlers (no Save button -- switching the global account
# hard-reloads the document, so a pending edit would be lost)
# ---------------------------------------------------------------------------

def _save_label_comment(account_id: int, label: str, value: str) -> None:
    try:
        set_managed_label(account_id, label, comment=value or "")
    except Exception as e:
        logger.error(f"Saving comment for label '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not save comment: {e}', type='negative')


def _save_symbol_comment(account_id: int, label: str, symbol: str, value: str) -> None:
    try:
        set_symbol_weight(account_id, label, symbol, comment=value or "")
    except Exception as e:
        logger.error(f"Saving comment for {label}/{symbol} failed: {e}", exc_info=True)
        ui.notify(f'Could not save comment: {e}', type='negative')


def _open_add_symbol_dialog(account_id: int, label: str, refresh) -> None:
    with ui.dialog() as dialog, ui.card().classes('min-w-[420px]'):
        ui.label(f"Add symbols to '{label}'").classes('text-h6')
        ui.label('Comma-separated. A symbol does not need an open position.'
                 ).classes('text-xs text-secondary-custom')
        entry = ui.input('Symbols', placeholder='AAPL, MSFT').classes('w-full')

        async def _apply() -> None:
            symbols = [s.strip().upper() for s in (entry.value or '').split(',') if s.strip()]
            if not symbols:
                ui.notify('Enter at least one symbol', type='warning')
                return
            try:
                added = await asyncio.to_thread(add_symbols_to_label, account_id, label, symbols)
            except Exception as e:
                logger.error(f"Adding {symbols} to '{label}' failed: {e}", exc_info=True)
                ui.notify(f'Could not add: {e}', type='negative')
                return
            ui.notify(f"Added {added} symbol(s) to '{label}'", type='positive')
            dialog.close()
            await refresh()

        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Add', on_click=_apply).props('color=primary')
    dialog.open()


def _open_label_picker(account_id: int, refresh) -> None:
    """Pick which labels this account manages. Persists on every change."""
    try:
        data = _load_picker_data(account_id)
    except Exception as e:
        logger.error(f"Loading labels for account {account_id} failed: {e}", exc_info=True)
        ui.notify(f'Could not load labels: {e}', type='negative')
        return

    current = list(data['current'])
    all_labels = data['all_labels']

    with ui.dialog() as dialog, ui.card().classes('min-w-[520px]'):
        ui.label('Managed labels').classes('text-h6')
        ui.label('Machine tags (auto_added, expert_selected, ai_selected, not_found and '
                 'the penny-N / tradingagents-N / fmprating-N families) are hidden.'
                 ).classes('text-xs text-secondary-custom')

        async def _persist(e) -> None:
            selected = list(e.value or [])
            to_add, to_remove = diff_managed_labels(current, selected)
            if not to_add and not to_remove:
                return
            try:
                await asyncio.to_thread(replace_managed_labels, account_id, selected)
            except Exception as exc:
                logger.error(f"Saving managed labels for account {account_id} failed: {exc}",
                             exc_info=True)
                ui.notify(f'Could not save: {exc}', type='negative')
                return
            current[:] = selected
            ui.notify(f'Managed labels updated (+{len(to_add)} / -{len(to_remove)})',
                      type='positive')

        picker = ui.select(filter_selectable_labels(all_labels), value=list(current),
                           multiple=True, label='Labels', on_change=_persist
                           ).props('dense outlined use-chips').classes('w-full')

        ui.switch('Show all labels (including machine tags)',
                  on_change=lambda e: picker.set_options(
                      filter_selectable_labels(all_labels, show_all=bool(e.value)),
                      value=picker.value))

        async def _close() -> None:
            dialog.close()
            await refresh()

        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Close', on_click=_close).props('color=primary')
    dialog.open()


def _render_label_body(account_id: int, view, refresh) -> None:
    """One managed label's comment box, symbol table and add/remove controls."""
    with ui.row().classes('w-full items-center gap-2'):
        ui.input('Label comment', value=view.comment or '',
                 on_change=lambda e, lbl=view.label: _save_label_comment(account_id, lbl, e.value)
                 ).props('dense outlined').classes('flex-grow')
        ui.button('Add symbol', icon='add',
                  on_click=lambda lbl=view.label: _open_add_symbol_dialog(account_id, lbl, refresh)
                  ).props('outline dense')

    rows = [{
        'flag': '⚠' if r.multi_label else '',
        'symbol': r.symbol,
        'labels': ', '.join(r.labels),
        'current_value': round(r.current_value, 2),
        'pct_of_label': round(r.pct_of_label, 2),
        'pct_of_total': round(r.pct_of_total, 2),
        'quantity': round(r.quantity, 4),
        'cost_basis': round(r.cost_basis, 2),
        'price': None if r.price is None else round(r.price, 4),
        'market_value': None if r.market_value is None else round(r.market_value, 2),
        'comment': r.comment or '',
    } for r in view.rows]

    columns = [
        {'name': 'flag', 'label': '', 'field': 'flag', 'align': 'center'},
        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
        {'name': 'labels', 'label': 'Labels', 'field': 'labels', 'align': 'left'},
        {'name': 'current_value', 'label': 'Current value', 'field': 'current_value', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_label', 'label': '% of label', 'field': 'pct_of_label', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_total', 'label': '% of total', 'field': 'pct_of_total', 'sortable': True, 'align': 'right'},
        {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'sortable': True, 'align': 'right'},
        {'name': 'cost_basis', 'label': 'Cost basis', 'field': 'cost_basis', 'sortable': True, 'align': 'right'},
        {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True, 'align': 'right'},
        {'name': 'market_value', 'label': 'Market value', 'field': 'market_value', 'sortable': True, 'align': 'right'},
        {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'},
    ]

    table = ui.table(columns=columns, rows=rows, row_key='symbol',
                     selection='multiple').classes('w-full dark-pagination')
    table.add_slot('body-cell-flag', r'''
        <q-td :props="props">
            <span v-if="props.value" :title="'Also in: ' + props.row.labels"
                  style="color:#f6ad55;font-weight:600">{{ props.value }}</span>
        </q-td>
    ''')
    table.add_slot('body-cell-comment', r'''
        <q-td :props="props">
            <q-input :model-value="props.value" dense borderless
                     @update:model-value="(val) => $parent.$emit('commentChange', props.row.symbol, val)" />
        </q-td>
    ''')
    table.on('commentChange',
             lambda e, lbl=view.label: _save_symbol_comment(account_id, lbl, e.args[0], e.args[1]))

    async def _remove_selected() -> None:
        symbols = [r['symbol'] for r in (table.selected or [])]
        if not symbols:
            ui.notify('Tick one or more symbols first', type='warning')
            return
        try:
            removed = await asyncio.to_thread(
                remove_symbols_from_label, account_id, view.label, symbols)
        except Exception as e:
            logger.error(f"Removing {symbols} from '{view.label}' failed: {e}", exc_info=True)
            ui.notify(f'Could not remove: {e}', type='negative')
            return
        ui.notify(f"Removed {removed} symbol(s) from '{view.label}'", type='positive')
        await refresh()

    with ui.row().classes('w-full justify-end'):
        ui.button('Remove selected from label', icon='delete', on_click=_remove_selected
                  ).props('outline color=negative dense')


def _render_labels(account_id: int, payload: Dict[str, Any], refresh) -> None:
    views = payload['views']
    if not views:
        with ui.element('div').classes('alert-banner info w-full p-3'):
            ui.label('No labels are managed for this account yet — click "Manage labels".')
        return

    total = sum(v.current_value for v in views)
    with ui.row().classes('w-full gap-4'):
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed value').classes('text-xs text-secondary-custom')
            ui.label(f'${total:,.2f}').classes('text-lg font-bold')
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed labels').classes('text-xs text-secondary-custom')
            ui.label(str(len(views))).classes('text-lg font-bold')

    ui.label('Prices are the broker feed (Alpaca defaults to delayed_sip — 15 minutes '
             'delayed). Only symbols carrying a managed label are listed.'
             ).classes('text-xs text-secondary-custom')

    for view in views:
        header = (f'{view.label} — ${view.current_value:,.2f} '
                  f'({view.pct_of_total:.1f}% of managed, target {view.target_pct:.1f}%)')
        with ui.expansion(header, icon='label').classes('w-full'):
            _render_label_body(account_id, view, refresh)
```

and in `content`, replace the two lines

```python
            body.clear()
            with body:
                _render_labels(payload)

        with toolbar:
            ui.button('Refresh', icon='refresh', on_click=_refresh).props('outline')
```

with:

```python
            body.clear()
            with body:
                _render_labels(account_id, payload, _refresh)

        with toolbar:
            ui.button('Manage labels', icon='pie_chart',
                      on_click=lambda: _open_label_picker(account_id, _refresh)).props('outline')
            ui.button('Refresh', icon='refresh', on_click=_refresh).props('outline')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 36 passed.

Run these per-file (the full suite is flaky from a pre-existing session leak):
```bash
venv/bin/python -m pytest tests/test_portfolio_allocation_route.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v
venv/bin/python -m pytest tests/test_instrument_labels.py -v
```
Expected: PASS for all three.

Eyeball check (the only way to verify the widgets): start the app with `venv/bin/python main.py`,
open `http://localhost:8080/portfolioallocation`, and confirm in turn — (1) with the header
selector on "All accounts" the "pick a single account" empty state shows and no Settings button
appears; (2) selecting an account without `manual_trading_enabled` shows the "not flagged as
manually traded" state with a working Settings button; (3) after ticking "Manually traded
account" in that account's settings and disabling its experts, "Manage labels" lists user labels
only until "Show all labels" is switched on; (4) picking `ARK26` immediately shows a green
notification and the label's expansion appears after Close; (5) a symbol in two managed labels
shows ⚠ with an "Also in:" tooltip; (6) typing in a label or symbol comment persists across a
full browser reload with no Save button pressed.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py ba2_trade_platform/ui/pages/portfolio_allocation.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): managed-label picker, symbol add/remove and eager comment persistence"
```

---

### Task 66: The valuation-mode toggle (`cost` vs `market`)

Spec decision 5a. The toggle sits on the page, its value lives in
`portfolio_allocation_config.valuation_mode` (Task 11), and it selects the meaning of
"current value" in the base, the percentages and every delta. The page must STATE which mode
produced the numbers on screen, and switching modes re-computes rather than silently
reinterpreting.

**Files:**
- Modify: `ba2_trade_platform/ui/utils/portfolio_allocation_view.py` (`build_label_views`)
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation.py`
- Test: `tests/test_portfolio_allocation_view.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_portfolio_allocation_view.py`:

```python
from ba2_trade_platform.core.portfolio_allocation import (
    VALUATION_MODE_COST, VALUATION_MODE_MARKET,
)


def test_label_views_in_cost_mode_measure_positions_at_their_cost_basis():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_COST)
    assert views[0].current_value == 1000.0
    assert views[0].rows[0].current_value == 1000.0
    assert views[0].rows[0].market_value == 2500.0     # still reported, just not the basis


def test_label_views_in_market_mode_measure_positions_at_quantity_times_price():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].current_value == 2500.0
    assert views[0].rows[0].current_value == 2500.0
    assert views[0].rows[0].cost_basis == 1000.0       # still reported


def test_market_mode_changes_the_percentages_a_doubled_position_reports():
    """AAPL doubled, MSFT flat. In cost mode they are 50/50; in market mode 67/33."""
    positions = {'AAPL': _pos('AAPL', 10, 1000.0), 'MSFT': _pos('MSFT', 10, 1000.0)}
    prices = {'AAPL': 200.0, 'MSFT': 100.0}
    symbols = {'ARK26': ['AAPL', 'MSFT']}

    cost = build_label_views([ManagedLabel('ARK26', 100.0)], symbols, positions, prices,
                             valuation_mode=VALUATION_MODE_COST)
    market = build_label_views([ManagedLabel('ARK26', 100.0)], symbols, positions, prices,
                               valuation_mode=VALUATION_MODE_MARKET)

    cost_by = {r.symbol: r for r in cost[0].rows}
    market_by = {r.symbol: r for r in market[0].rows}
    assert cost_by['AAPL'].pct_of_label == 50.0
    assert market_by['AAPL'].pct_of_label == pytest.approx(66.67, abs=0.01)
    assert market_by['MSFT'].pct_of_label == pytest.approx(33.33, abs=0.01)


def test_market_mode_without_a_price_reports_zero_current_value_not_a_guess():
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': None},
                              valuation_mode=VALUATION_MODE_MARKET)
    assert views[0].rows[0].current_value == 0.0
    assert views[0].rows[0].price is None


def test_build_label_views_defaults_to_cost_mode():
    """The DB default is 'cost' (spec 5a); the helper agrees so an omitted argument
    can never silently reinterpret the page."""
    views = build_label_views([ManagedLabel('ARK26', 100.0)],
                              {'ARK26': ['AAPL']},
                              {'AAPL': _pos('AAPL', 10, 1000.0)},
                              {'AAPL': 250.0})
    assert views[0].current_value == 1000.0


def test_build_label_views_rejects_an_unknown_valuation_mode():
    with pytest.raises(ValueError):
        build_label_views([ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL']},
                          {'AAPL': _pos('AAPL', 10, 1000.0)}, {},
                          valuation_mode='marketish')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v -k "valuation_mode or cost_mode or market_mode or defaults_to_cost"`
Expected: FAIL — `TypeError: build_label_views() got an unexpected keyword argument 'valuation_mode'` on all six.

- [ ] **Step 3: Write minimal implementation**

**3a.** In `ba2_trade_platform/ui/utils/portfolio_allocation_view.py`, extend the engine import
to:

```python
from ...core.portfolio_allocation import (
    VALUATION_MODE_COST, VALUATION_MODE_MARKET, PositionFetchFailed, PositionState,
    current_value,
)
```

Change `build_label_views`'s signature from:

```python
def build_label_views(managed,
                      symbols_by_label,
                      positions,
                      prices,
                      symbol_comments=None) -> List[LabelView]:
```

to:

```python
def build_label_views(managed,
                      symbols_by_label,
                      positions,
                      prices,
                      symbol_comments=None,
                      valuation_mode: str = VALUATION_MODE_COST) -> List[LabelView]:
```

Add this paragraph to its docstring, replacing the sentence
"Current value is the COST BASIS here; Task 66 adds a ``valuation_mode`` keyword...":

```
    ``valuation_mode`` (decision 5a) selects what "current value" means: ``cost``
    (the cost basis) or ``market`` (``qty x price``). It drives BOTH percentage
    columns and both totals, so the page must state which mode produced them.
    Defaults to ``cost``, matching ``portfolio_allocation_config.valuation_mode``.
    A market-mode symbol with no price contributes 0 rather than a guessed value.
```

Add this guard immediately after `comments = symbol_comments or {}`:

```python
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
```

Replace the `total_value` loop:

```python
    total_value = 0.0
    for sym in membership:
        state = positions.get(sym)
        if state is not None:
            total_value += state.cost_basis
```

with a mode-aware version that uses the LIVE price rather than the PositionState's own (the page
fetches prices separately, so `PositionState.price` is not populated here):

```python
    def _value_of(sym: str) -> float:
        """This symbol's current value under the active mode, using the LIVE price.

        ``PositionState.price`` is not populated on this path -- the page fetches
        quotes in one bulk call -- so a shallow copy carrying the live price is fed
        to the engine's ``current_value``, keeping ONE definition of the rule.
        """
        state = positions.get(sym)
        if state is None:
            return 0.0
        if valuation_mode == VALUATION_MODE_COST:
            return current_value(state, VALUATION_MODE_COST)
        priced = PositionState(symbol=state.symbol, quantity=state.quantity,
                               cost_basis=state.cost_basis, price=(prices or {}).get(sym))
        return current_value(priced, VALUATION_MODE_MARKET)

    total_value = sum(_value_of(sym) for sym in membership)
```

Replace the per-label total:

```python
        label_value = sum(positions[s].cost_basis for s in symbols if s in positions)
```

with:

```python
        label_value = sum(_value_of(s) for s in symbols)
```

And in the row loop, replace:

```python
            rows.append(SymbolRow(
                symbol=sym,
                labels=list(membership.get(sym, [entry.label])),
                quantity=quantity,
                cost_basis=cost_basis,
                current_value=cost_basis,
                price=price,
                market_value=market_value,
                pct_of_label=(cost_basis / label_value * 100.0) if label_value > 0 else 0.0,
                pct_of_total=(cost_basis / total_value * 100.0) if total_value > 0 else 0.0,
                comment=comments.get((entry.label, sym)),
            ))
```

with:

```python
            row_value = _value_of(sym)
            rows.append(SymbolRow(
                symbol=sym,
                labels=list(membership.get(sym, [entry.label])),
                quantity=quantity,
                cost_basis=cost_basis,
                current_value=row_value,
                price=price,
                market_value=market_value,
                pct_of_label=(row_value / label_value * 100.0) if label_value > 0 else 0.0,
                pct_of_total=(row_value / total_value * 100.0) if total_value > 0 else 0.0,
                comment=comments.get((entry.label, sym)),
            ))
```

Finally, in the `LabelView(...)` construction, replace `cost_basis=label_value,` with:

```python
            cost_basis=sum(positions[s].cost_basis for s in symbols if s in positions),
```

**3b.** In `ba2_trade_platform/ui/pages/portfolio_allocation.py`:

Extend the store import to add `get_allocation_config` and `set_allocation_config`, and add the
engine import:

```python
from ...core.portfolio_allocation import VALUATION_MODE_COST, VALUATION_MODE_MARKET
```

Change `_load_view_payload`'s signature to
`def _load_view_payload(account_id: int, valuation_mode: str) -> Dict[str, Any]:`, pass the mode
through to `build_label_views(..., comments, valuation_mode=valuation_mode)`, and add
`'valuation_mode': valuation_mode,` to the returned dict.

Add this loader beside `_load_picker_data`:

```python
def _load_valuation_mode(account_id: int) -> str:
    """The account's stored valuation mode, creating the config row on first use."""
    return get_allocation_config(account_id).valuation_mode
```

In `_render_labels`, replace the single stat row with one that names the mode:

```python
    mode = payload['valuation_mode']
    mode_label = ('cost basis (what you paid)' if mode == VALUATION_MODE_COST
                  else 'market value (qty x price)')
    total = sum(v.current_value for v in views)
    with ui.row().classes('w-full gap-4'):
        with ui.column().classes('stat-card p-3'):
            ui.label(f'Managed value — {mode_label}').classes('text-xs text-secondary-custom')
            ui.label(f'${total:,.2f}').classes('text-lg font-bold')
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed labels').classes('text-xs text-secondary-custom')
            ui.label(str(len(views))).classes('text-lg font-bold')
```

And in `content`, add the toggle to the toolbar and thread the mode through `_refresh`:

```python
        toolbar = ui.row().classes('w-full items-center gap-2')
        body = ui.column().classes('w-full gap-3')
        mode_state = {'value': await asyncio.to_thread(_load_valuation_mode, account_id)}

        async def _refresh() -> None:
            body.clear()
            with body:
                ui.spinner(size='lg').classes('self-center')
            try:
                payload = await asyncio.to_thread(
                    _load_view_payload, account_id, mode_state['value'])
            except PositionFetchFailed as e:
                logger.error(f"Portfolio allocation: position fetch failed: {e}")
                body.clear()
                with body:
                    with ui.element('div').classes('alert-banner danger w-full p-3'):
                        ui.label(f'Broker position fetch FAILED: {e}')
                        ui.label('Nothing is shown until the broker answers — a failed '
                                 'fetch and a flat account are not the same thing.'
                                 ).classes('text-xs text-secondary-custom')
                return
            except Exception as e:
                logger.error(f"Portfolio allocation refresh failed: {e}", exc_info=True)
                body.clear()
                with body:
                    with ui.element('div').classes('alert-banner danger w-full p-3'):
                        ui.label(f'Could not load allocation: {e}')
                return
            body.clear()
            with body:
                _render_labels(account_id, payload, _refresh)

        async def _set_mode(event) -> None:
            """Persist the mode EAGERLY and RE-COMPUTE -- never reinterpret silently."""
            chosen = event.value
            if not chosen or chosen == mode_state['value']:
                return
            try:
                await asyncio.to_thread(set_allocation_config, account_id,
                                        valuation_mode=chosen)
            except Exception as e:
                logger.error(f"Saving valuation mode failed: {e}", exc_info=True)
                ui.notify(f'Could not save valuation mode: {e}', type='negative')
                return
            mode_state['value'] = chosen
            ui.notify(f'Valuation mode: {chosen}', type='info')
            await _refresh()

        with toolbar:
            ui.select({VALUATION_MODE_COST: 'Cost basis',
                       VALUATION_MODE_MARKET: 'Market value'},
                      value=mode_state['value'], label='Valuation',
                      on_change=_set_mode).props('dense outlined').classes('w-44')
            ui.button('Manage labels', icon='pie_chart',
                      on_click=lambda: _open_label_picker(account_id, _refresh)).props('outline')
            ui.button('Refresh', icon='refresh', on_click=_refresh).props('outline')

        await _refresh()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v`
Expected: PASS — 42 passed.

Eyeball check: reload `/portfolioallocation`, switch Valuation from "Cost basis" to
"Market value" and confirm the percentages change, the stat card renames itself, and the choice
survives a full browser reload.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/portfolio_allocation_view.py ba2_trade_platform/ui/pages/portfolio_allocation.py tests/test_portfolio_allocation_view.py
git commit -m "feat(ui): per-account cost/market valuation-mode toggle"
```

---

### Task 67: Clear allocation data when an account is deleted

Foreign keys on the five allocation tables are declarative only — the live DB runs with
`PRAGMA foreign_keys = 0`, so `ondelete="CASCADE"` never fires. `delete_account` already has an
explicit `AccountSetting` cleanup loop for exactly this reason; the allocation rows need the same
treatment or the next account to reuse that id inherits them.

**Files:**
- Modify: `ba2_trade_platform/ui/pages/settings.py:1025-1042` (`delete_account`)
- Test: `tests/test_portfolio_allocation_account_deletion.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_account_deletion.py`:

```python
"""Deleting an account must clear its allocation rows.

The live DB runs with PRAGMA foreign_keys = 0, so the declared ondelete="CASCADE"
never fires: an account id that is later reused would otherwise inherit the old
account's managed labels, weights, income ledger and run history.

`AccountsTab.delete_account` is a NiceGUI method, but its body is plain DB work
apart from one ui.notify in the failure branch, so it is driven directly with a
bare instance (object.__new__) and a stubbed _update_table_rows.
"""
from datetime import date

from ba2_trade_platform.core import portfolio_allocation_store as store
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from tests.factories import create_account_definition


def _delete_account(account):
    from ba2_trade_platform.ui.pages.settings import AccountsTab
    tab = object.__new__(AccountsTab)
    tab._update_table_rows = lambda: None
    tab.delete_account(account)


def _seed(account_id):
    store.set_managed_label(account_id, 'ARK26', target_pct=100.0)
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', weight_pct=100.0)
    store.upsert_income_event(account_id, 'csd-1', date(2026, 8, 1), 'DEPOSIT', 100.0)
    store.record_allocation_run(account_id, 'REBALANCE', {})
    store.set_allocation_config(account_id, valuation_mode='market')


def test_deleting_an_account_clears_all_of_its_allocation_rows():
    account = create_account_definition(name='Doomed')
    account_id = account.id
    _seed(account_id)

    _delete_account(account)

    with get_db() as session:
        assert session.get(AccountDefinition, account_id) is None
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, 'ARK26') == {}
    assert store.get_open_income_events(account_id) == []
    assert store.get_recent_runs(account_id) == []


def test_deleting_an_account_leaves_another_accounts_allocation_intact():
    doomed = create_account_definition(name='Doomed')
    keeper = create_account_definition(name='Keeper')
    _seed(doomed.id)
    _seed(keeper.id)

    _delete_account(doomed)

    assert [r.label for r in store.get_managed_labels(keeper.id)] == ['ARK26']
    assert store.get_open_income_total(keeper.id) == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_account_deletion.py -v`
Expected: FAIL — `test_deleting_an_account_clears_all_of_its_allocation_rows` fails with
`AssertionError: assert [<PortfolioAllocationLabel ...>] == []` (the account row is gone but its
allocation rows survive).

- [ ] **Step 3: Write minimal implementation**

In `ba2_trade_platform/ui/pages/settings.py`, replace the body of `delete_account`
(lines 1025-1042), which reads exactly:

```python
    def delete_account(self, account: AccountDefinition) -> None:
        try:
            # First delete related account settings
            with get_db() as session:
                settings = session.exec(
                    select(AccountSetting).where(AccountSetting.account_id == account.id)
                ).all()
                for setting in settings:
                    delete_instance(setting, session)
                logger.info(f"Deleted {len(settings)} settings for account: {account.name}")
            
            # Then delete the account
            delete_instance(account)
            logger.info(f"Deleted account: {account.name}")
            self._update_table_rows()
        except Exception as e:
            logger.error(f"Error deleting account: {str(e)}", exc_info=True)
            ui.notify("Error deleting account", type="error")
```

with:

```python
    def delete_account(self, account: AccountDefinition) -> None:
        try:
            # First delete related account settings
            with get_db() as session:
                settings = session.exec(
                    select(AccountSetting).where(AccountSetting.account_id == account.id)
                ).all()
                for setting in settings:
                    delete_instance(setting, session)
                logger.info(f"Deleted {len(settings)} settings for account: {account.name}")

            # Then the portfolio-allocation rows. The live DB runs with
            # PRAGMA foreign_keys = 0, so the ondelete="CASCADE" declared on those
            # five tables NEVER fires -- an id reused by a future account would
            # otherwise inherit this account's labels, weights, ledger and runs.
            from ...core.portfolio_allocation_store import delete_account_allocation_data
            counts = delete_account_allocation_data(account.id)
            logger.info(f"Deleted allocation data for account {account.name}: {counts}")

            # Then delete the account
            delete_instance(account)
            logger.info(f"Deleted account: {account.name}")
            self._update_table_rows()
        except Exception as e:
            logger.error(f"Error deleting account: {str(e)}", exc_info=True)
            ui.notify("Error deleting account", type="negative")
```

Note the `type=` fix on the last line: `"error"` is NOT a valid `ui.notify` type (only
`'positive' | 'negative' | 'warning' | 'info'` are), so the existing bug is corrected in the one
line this task already touches.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_account_deletion.py -v`
Expected: PASS — 2 passed.

Run: `venv/bin/python -m pytest tests/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/pages/settings.py tests/test_portfolio_allocation_account_deletion.py
git commit -m "fix(ui): clear portfolio allocation rows when an account is deleted"
```

---

## Section G — Wizard, dry-run, submission, income UI

**Prerequisites** (Tasks that must already have landed; every task below fails at import if one
is missing):

- Task 27: `ba2_common/core/account_types.py` + its shim.
- Tasks 16-26: the pure engine `ba2_common/core/portfolio_allocation.py` + its shim, with the
  constants block, the five value objects, `PositionFetchFailed`, `compute_base_notional`,
  `even_split_pct`, `build_symbol_targets`, `validate_label_targets`, `compute_allocation`,
  `compute_label_investment`, `apply_order_impacts`, `consume_income_events`, `current_value`
  and the `VALUATION_MODE_*` constants.
- Tasks 7-15: the five models, in `tests/conftest.py`'s import list, and
  `ba2_common/core/portfolio_allocation_store.py` + its shim.
- Tasks 56-67: the page and its pure view module.

**What is pure-testable vs eyeball-only** is stated per task. Section G owns two new
implementation files — the live service `ba2_trade_platform/core/portfolio_allocation_service.py`
and the NiceGUI module `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py`. It
deliberately does **not** rewrite `ui/pages/portfolio_allocation.py` (owned by Section F), which
imports the three entry points `open_allocation_wizard()`, `open_allocation_steps()` and
`render_income_panel()` from the wizard module.

---

### Task 68: Base snapshot — the allocatable base, frozen at wizard open

Pure-testable: all of it. Eyeball-only: nothing.

`compute_base_notional` already exists (Task 22, mode-aware since Task 25); this task adds the
`BaseSnapshot` wrapper that carries the split, the conservative `default_bp_factor` and the
timestamp the wizard displays.

Note that `compute_base_notional`'s `valuation_mode` is a REQUIRED keyword with no default
(Task 25, amendment 5) — every call below passes one, including in the tests.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py` (append at end of file)
- Test: `packages/common/tests/test_portfolio_allocation_wizard.py` (create)

- [ ] **Step 1: Write the failing test**

Create `packages/common/tests/test_portfolio_allocation_wizard.py`:

```python
"""Pure unit tests for the Portfolio Allocation wizard arithmetic.

Everything here runs with no DB, no broker and no NiceGUI. Invoke by explicit
path -- pytest.ini has `testpaths = tests`, so this directory is not collected
by a bare `pytest`.
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.portfolio_allocation import (
    VALUATION_MODE_COST,
    VALUATION_MODE_MARKET,
    BaseSnapshot,
    PositionState,
    WARNING_NO_MULTIPLIER,
    build_base_snapshot,
    compute_base_notional,
)


def test_base_notional_adds_cost_basis_of_managed_positions_only():
    current = {
        "AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0, price=160.0),
        "MSFT": PositionState(symbol="MSFT", quantity=5, cost_basis=2000.0, price=410.0),
        "TSLA": PositionState(symbol="TSLA", quantity=3, cost_basis=900.0, price=300.0),
    }
    # TSLA is held but NOT managed -> it must not inflate the base.
    # valuation_mode is REQUIRED (no default, see the Task 25 amendment) and COST is
    # what 1500 + 2000 is: at market those two positions are 1600 + 2050.
    base = compute_base_notional(10_000.0, current, ["AAPL", "MSFT"],
                                 valuation_mode=VALUATION_MODE_COST)
    assert base == pytest.approx(13_500.0)


def test_build_base_snapshot_splits_buying_power_from_managed_value():
    snap = AccountSnapshot(cash=2_000.0, buying_power=10_000.0, margin_multiplier=2.0,
                           is_margin_account=True, supports_fractional=True)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0)}

    base = build_base_snapshot(snap, current, ["AAPL"])

    assert isinstance(base, BaseSnapshot)
    assert base.available_buying_power == pytest.approx(10_000.0)
    assert base.managed_value == pytest.approx(1_500.0)
    assert base.base_notional == pytest.approx(11_500.0)
    assert base.default_bp_factor == pytest.approx(2.0)
    assert base.valuation_mode == VALUATION_MODE_COST
    assert base.warnings == []


def test_build_base_snapshot_in_market_mode_values_positions_at_the_live_price():
    snap = AccountSnapshot(buying_power=10_000.0, margin_multiplier=1.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0,
                                     price=250.0)}

    base = build_base_snapshot(snap, current, ["AAPL"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.managed_value == pytest.approx(2_500.0)
    assert base.base_notional == pytest.approx(12_500.0)
    assert base.valuation_mode == VALUATION_MODE_MARKET


def test_build_base_snapshot_without_multiplier_assumes_cash_account():
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=None)
    base = build_base_snapshot(snap, {}, [])
    assert base.default_bp_factor == pytest.approx(1.0)
    assert WARNING_NO_MULTIPLIER in base.warnings


def test_build_base_snapshot_missing_buying_power_raises():
    snap = AccountSnapshot(cash=1_000.0, buying_power=None)
    with pytest.raises(ValueError):
        build_base_snapshot(snap, {}, [])


def test_build_base_snapshot_with_no_snapshot_at_all_raises():
    with pytest.raises(ValueError):
        build_base_snapshot(None, {}, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`

Expected: FAIL at collection with
`ImportError: cannot import name 'BaseSnapshot' from 'ba2_common.core.portfolio_allocation'`

- [ ] **Step 3: Write minimal implementation**

Append to `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
# ---------------------------------------------------------------------------
# Wizard: the allocatable base, snapshotted when the wizard opens.
# ---------------------------------------------------------------------------

#: Added to ``BaseSnapshot.warnings`` when the broker published no multiplier.
WARNING_NO_MULTIPLIER = "broker published no margin multiplier - assuming a cash account (1.0)"


@dataclass
class BaseSnapshot:
    """Frozen-at-wizard-open view of what there is to allocate.

    The wizard reads this ONCE when it opens and re-reads it only when the user
    presses Refresh, so the numbers cannot move underneath an edit.

    ``managed_value`` is the current value of the managed positions under
    ``valuation_mode`` (decision 5a), and ``base_notional`` is that plus buying
    power (decision 1).

    ``default_bp_factor`` is the conservative per-dollar buying-power cost fed to
    the engine for symbols the broker could not describe. It is the account
    margin multiplier when the broker publishes one; when it does not, it is
    1.0 -- "one dollar of notional costs one dollar of buying power", i.e. a cash
    account. Never guess HIGHER leverage than the broker admitted to.
    """
    available_buying_power: float
    managed_value: float
    base_notional: float
    default_bp_factor: float
    valuation_mode: str = VALUATION_MODE_COST
    cash: Optional[float] = None
    is_margin_account: bool = False
    supports_fractional: bool = False
    taken_at: DateTime = field(default_factory=lambda: DateTime.now(timezone.utc))
    warnings: List[str] = field(default_factory=list)


def build_base_snapshot(
    snapshot: "AccountSnapshot",
    current: Dict[str, PositionState],
    managed_symbols: List[str],
    *,
    valuation_mode: str = VALUATION_MODE_COST,
) -> BaseSnapshot:
    """Turn a broker AccountSnapshot into the wizard's frozen base.

    Raises:
        ValueError: when there is no snapshot at all, when the broker published no
        ``buying_power`` (a plan sized against a guessed balance is worse than no
        plan), or when ``valuation_mode`` is unknown.
    """
    if snapshot is None:
        raise ValueError("build_base_snapshot: no AccountSnapshot (the broker call failed).")
    if snapshot.buying_power is None:
        raise ValueError(
            "build_base_snapshot: broker published no buying_power; refusing to plan "
            "against a substituted default."
        )
    buying_power = float(snapshot.buying_power)
    managed_value = compute_base_notional(0.0, current, managed_symbols,
                                          valuation_mode=valuation_mode)

    warnings: List[str] = []
    if snapshot.margin_multiplier is None:
        default_bp_factor = 1.0
        warnings.append(WARNING_NO_MULTIPLIER)
        logger.warning("build_base_snapshot: no margin multiplier; using default_bp_factor=1.0")
    else:
        default_bp_factor = float(snapshot.margin_multiplier)

    return BaseSnapshot(
        available_buying_power=buying_power,
        managed_value=managed_value,
        base_notional=buying_power + managed_value,
        default_bp_factor=default_bp_factor,
        valuation_mode=valuation_mode,
        cash=snapshot.cash,
        is_margin_account=bool(snapshot.is_margin_account),
        supports_fractional=bool(snapshot.supports_fractional),
        warnings=warnings,
    )
```

This needs `AccountSnapshot`, `DateTime` and `timezone` at the top of the module. Extend the
existing import block from:

```python
from ba2_common.core.account_types import MarginInfo, OrderImpact  # noqa: F401 (re-exported)
from ba2_common.core.types import OrderDirection
```

to:

```python
from datetime import datetime as DateTime, timezone

from ba2_common.core.account_types import (  # noqa: F401 (re-exported)
    AccountSnapshot, MarginInfo, OrderImpact,
)
from ba2_common.core.types import OrderDirection
from ba2_common.logger import logger
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`
Expected: PASS — 6 passed

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v`
Expected: PASS — still 67 passed (the new imports must not disturb the engine).

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation_wizard.py
git commit -m "feat(allocation): allocatable-base snapshot for the wizard"
```

---

### Task 69: Wire the engine to real data — positions, prices, margin info, precheck

> **Guard the precheck call site — a bare call raises rather than returning `None`.**
> `preview_order_impact` lives on `AccountInterface` only, not `ReadOnlyAccountInterface`, because
> previewing is a trading capability. So `acct.preview_order_impact(...)` on a read-only account
> raises `AttributeError` instead of yielding the intended "this broker cannot preview". Use
> `getattr(acct, "preview_order_impact", None)`, or gate on `supports_trading` /
> `isinstance(acct, AccountInterface)` — and make that path produce `None`, meaning *not asked*,
> never a zero-valued `OrderImpact`.
>
> **Key "did I get an impact?" off `is None`, never off falsiness.** `OrderImpact.bp_cost` returns
> `0.0` for an order that FREES buying power. That is a real zero and semantically different from no
> impact at all; treating it as falsy silently drops the re-solve for exactly the orders that would
> have given headroom back.
>
> **Keep precheck-derived margin out of `_margin_info_cache`.** Task 32's cache holds only immutable
> Asset facts and re-derives `bp_factor` per call from the live account multiplier — because Alpaca
> moves an account between 1x/2x/4x as it crosses the PDT threshold, and a cached factor would go
> stale for days. That repricing branch treats a cached entry's `initial_margin_rate` as the source
> of truth. A `MARGIN_SOURCE_PRECHECK` entry's `bp_factor` is already ABSOLUTE, not
> multiplier-relative, so caching one there would get it multiplied a second time. Either keep
> precheck results out of that cache entirely, or give them an explicit `initial_margin_rate`.
> (As landed, a cached entry with `initial_margin_rate is None` is skipped by the repricing and
> returned unchanged, so a precheck entry survives a cache hit intact. It would still be dropped
> after 24h by `_MARGIN_INFO_CACHE_TTL`, and the entry is a frozen dataclass.)


Pure-testable: `build_position_states` (fake account + in-memory DB), `fetch_margin_info`,
`precheck_plan` (fake account, no DB). Eyeball-only: nothing.

The precheck flow is exactly: solve once → build candidate orders → `account.preview_order_impact(order)`
per buy → re-solve **only** if at least one impact came back. Alpaca returns `None` (no precheck
endpoint), so its deterministic `get_symbol_margin_info` data stands and there is no second solve.

**Files:**
- Create: `ba2_trade_platform/core/portfolio_allocation_service.py`
- Test: `tests/test_portfolio_allocation_submit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_allocation_submit.py`:

```python
"""Live-side Portfolio Allocation service tests.

Uses tests/conftest.py's in-memory SQLite (autouse `patch_db_engine`) and a
duck-typed FakeAccount -- no broker, no NiceGUI.
"""
import pytest

from ba2_trade_platform.core.account_types import MarginInfo, OrderImpact
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, PositionFetchFailed, PositionState,
)
from ba2_trade_platform.core.types import (
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.core import portfolio_allocation_service as svc


class FakePosition:
    """Minimal stand-in for a broker Position row."""

    def __init__(self, symbol, qty, cost_basis, market_value):
        self.symbol = symbol
        self.qty = qty
        self.cost_basis = cost_basis
        self.market_value = market_value


class FakeAccount:
    """Duck-typed stand-in for AccountInterface. No DB lookups, no broker."""

    supports_trading = True

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.positions = []          # list[FakePosition]; None means FETCH FAILED
        self.prices = {}             # symbol -> float
        self.margin = {}             # symbol -> MarginInfo
        self.impacts = {}            # symbol -> OrderImpact
        self.cash_transfers = []     # list[CashTransfer]
        self.submitted = []          # [(symbol, side, quantity, comment)]
        self.closed = []             # [transaction_id]
        self.reject_quantities = set()
        self.washtrade_symbols = set()

    def get_positions(self):
        return self.positions

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        if isinstance(symbol_or_symbols, str):
            return self.prices.get(symbol_or_symbols)
        return {s: self.prices.get(s) for s in symbol_or_symbols}

    def get_symbol_margin_info(self, symbols):
        return {s: self.margin[s] for s in symbols if s in self.margin}

    def preview_order_impact(self, trading_order):
        return self.impacts.get(trading_order.symbol)

    def get_cash_transfers(self, start_date=None, end_date=None):
        return list(self.cash_transfers)

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        self.submitted.append((trading_order.symbol, trading_order.side,
                               trading_order.quantity, trading_order.comment))
        if trading_order.quantity in self.reject_quantities:
            trading_order.comment = f"{trading_order.comment or ''} | broker rejected"
            return None
        if trading_order.symbol in self.washtrade_symbols:
            trading_order.status = OrderStatus.WASHTRADE_LOCKED
            return trading_order
        trading_order.status = OrderStatus.FILLED
        trading_order.filled_qty = trading_order.quantity
        return trading_order

    def close_transaction(self, transaction_id):
        self.closed.append(transaction_id)
        return {'success': True, 'message': 'closed', 'canceled_count': 0,
                'deleted_count': 0, 'close_order_id': 999}


def make_open_transaction(account_id: int, symbol: str, quantity: float) -> int:
    """Persist an OPENED Transaction linked to `account_id` via a filled order."""
    txn_id = add_instance(Transaction(
        symbol=symbol, quantity=quantity, side=OrderDirection.BUY,
        open_price=100.0, status=TransactionStatus.OPENED,
    ))
    add_instance(TradingOrder(
        account_id=account_id, symbol=symbol, quantity=quantity,
        side=OrderDirection.BUY, order_type=OrderType.MARKET, good_for='day',
        status=OrderStatus.FILLED, open_type=OrderOpenType.MANUAL,
        transaction_id=txn_id,
    ))
    return txn_id


def make_row(symbol, side, delta, value, bp_cost, price=100.0):
    return AllocationRow(
        symbol=symbol, price=price, delta_quantity=delta, side=side,
        estimated_value=value, bp_cost=bp_cost, bp_factor=1.0,
    )


# ---------------------------------------------------------------------------
# build_position_states
# ---------------------------------------------------------------------------

def test_build_position_states_raises_when_get_positions_returns_none():
    account = FakeAccount()
    account.positions = None  # broker fetch FAILED -- not a flat account
    with pytest.raises(PositionFetchFailed):
        svc.build_position_states(account, ["AAPL"])


def test_build_position_states_maps_quantity_cost_basis_and_price():
    account = FakeAccount()
    account.positions = [FakePosition("AAPL", 10.0, 1500.0, 1600.0)]
    account.prices = {"AAPL": 160.0, "NVDA": 900.0}

    states = svc.build_position_states(account, ["aapl", "NVDA"])

    assert set(states) == {"AAPL", "NVDA"}
    assert states["AAPL"].quantity == pytest.approx(10.0)
    assert states["AAPL"].cost_basis == pytest.approx(1500.0)
    assert states["AAPL"].price == pytest.approx(160.0)
    # Managed but not held -> flat, still priced, still plannable.
    assert states["NVDA"].quantity == pytest.approx(0.0)
    assert states["NVDA"].cost_basis == pytest.approx(0.0)
    assert states["NVDA"].price == pytest.approx(900.0)


def test_build_position_states_lists_open_transaction_ids_oldest_first():
    account = FakeAccount(account_id=7)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    account.prices = {"AAPL": 106.0}
    first = make_open_transaction(7, "AAPL", 20.0)
    second = make_open_transaction(7, "AAPL", 10.0)

    states = svc.build_position_states(account, ["AAPL"])

    assert states["AAPL"].transaction_ids == [first, second]


# ---------------------------------------------------------------------------
# fetch_margin_info / precheck_plan
# ---------------------------------------------------------------------------

def test_fetch_margin_info_omits_symbols_the_broker_cannot_describe():
    account = FakeAccount()
    account.margin = {"AAPL": MarginInfo(symbol="AAPL", bp_factor=1.0, marginable=True)}
    info = svc.fetch_margin_info(account, ["AAPL", "NVDA"])
    assert set(info) == {"AAPL"}


def test_precheck_plan_without_broker_support_returns_the_same_plan():
    account = FakeAccount()  # every preview_order_impact() returns None
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0, required_buying_power=1600.0,
        total_buy_value=1600.0,
    )
    assert svc.precheck_plan(account, plan, available_buying_power=10_000.0) is plan


def test_precheck_plan_replaces_the_estimate_with_the_broker_buying_power_cost():
    account = FakeAccount()
    # Broker says this buy really costs 3200 of BP, not the estimated 1600.
    account.impacts = {"AAPL": OrderImpact(symbol="AAPL", change_in_buying_power=-3200.0)}
    plan = AllocationPlan(
        rows=[make_row("AAPL", OrderDirection.BUY, 10, 1600.0, 1600.0)],
        available_buying_power=10_000.0, required_buying_power=1600.0,
        total_buy_value=1600.0,
    )

    result = svc.precheck_plan(account, plan, available_buying_power=10_000.0)

    assert result is not plan
    assert result.rows[0].bp_cost == pytest.approx(3200.0)
    assert result.required_buying_power == pytest.approx(3200.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`

Expected: FAIL at collection with
`ModuleNotFoundError: No module named 'ba2_trade_platform.core.portfolio_allocation_service'`

- [ ] **Step 3: Write minimal implementation**

Create `ba2_trade_platform/core/portfolio_allocation_service.py`:

```python
"""Portfolio Allocation service: the live wiring between the pure engine and reality.

This module is LIVE-ONLY (it touches the DB and a broker), so it belongs in-tree
rather than in ba2_common -- it is NOT a shim, and there is no
``ba2_common.core.portfolio_allocation_service``. Every *decision* it makes is
delegated to a pure function in ``ba2_common.core.portfolio_allocation`` or to the
persistence layer ``ba2_common.core.portfolio_allocation_store``; what lives here
is the IO: reading positions/prices/margin metadata, running the broker precheck,
creating TradingOrder rows, and driving the run audit.

Do not confuse it with ``ba2_trade_platform/core/portfolio_allocation.py``, which
IS a shim (for the pure engine).
"""
from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from ..logger import logger
from .db import add_instance, get_db, get_instance, InstanceNotFound, log_activity, update_instance
from .models import Transaction, TradingOrder
from .portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP,
    AllocationPlan, BaseSnapshot, PositionFetchFailed, PositionState,
    apply_order_impacts, decide_symbol_action, plan_quantity_attempts, split_delta_fifo,
    FRACTIONAL_PATH_WHOLE,
)
from .TransactionHelper import TransactionHelper
from .types import (
    ActivityLogSeverity, ActivityLogType, OrderDirection, OrderOpenType, OrderStatus,
    OrderType, TransactionStatus,
)


def _open_transaction_ids(account_id: int, symbols: List[str]) -> Dict[str, List[int]]:
    """``{symbol: [transaction_id]}`` for OPENED/CLOSING transactions, oldest first.

    Transaction has NO account_id column -- it links to an account only through
    ``TradingOrder.account_id``, hence the join. Ordering is by primary key,
    which is creation order, so submission can consume them FIFO.
    """
    if not symbols:
        return {}
    out: Dict[str, List[int]] = {}
    with get_db() as session:
        statement = select(Transaction).join(TradingOrder).where(
            TradingOrder.account_id == account_id,
            Transaction.symbol.in_(symbols),
            Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSING]),
        ).distinct()
        for txn in session.exec(statement).all():
            out.setdefault(txn.symbol, []).append(txn.id)
    return {symbol: sorted(ids) for symbol, ids in out.items()}


def build_position_states(account, symbols: List[str]) -> Dict[str, PositionState]:
    """Positions + live prices + open transaction ids for the managed symbols.

    A managed symbol with no position is returned FLAT (quantity 0) but priced,
    so the wizard can open a position in it. A symbol with no price keeps
    ``price=None`` and the engine will skip it with a reason.

    Raises:
        PositionFetchFailed: when ``get_positions()`` returned None. The class is
            defined in the pure engine, so the UI's view module raises the same one.
    """
    wanted = []
    for raw in symbols:
        if raw and raw.strip():
            normalised = raw.strip().upper()
            if normalised not in wanted:
                wanted.append(normalised)

    positions = account.get_positions()
    if positions is None:
        raise PositionFetchFailed(
            f"get_positions() returned None for account {account.id}: the broker fetch "
            f"failed. Refusing to treat it as a flat account."
        )

    held: Dict[str, Any] = {}
    for position in positions:
        symbol = (getattr(position, 'symbol', '') or '').strip().upper()
        if symbol in wanted:
            held[symbol] = position

    prices = account.get_instrument_current_price(wanted) if wanted else {}
    if not isinstance(prices, dict):
        prices = {}
    txn_ids = _open_transaction_ids(account.id, wanted)

    states: Dict[str, PositionState] = {}
    for symbol in wanted:
        position = held.get(symbol)
        states[symbol] = PositionState(
            symbol=symbol,
            quantity=float(getattr(position, 'qty', 0.0) or 0.0) if position else 0.0,
            cost_basis=float(getattr(position, 'cost_basis', 0.0) or 0.0) if position else 0.0,
            price=prices.get(symbol),
            market_value=float(getattr(position, 'market_value', 0.0) or 0.0) if position else 0.0,
            transaction_ids=list(txn_ids.get(symbol, [])),
        )
    return states


def fetch_margin_info(account, symbols: List[str]) -> Dict[str, Any]:
    """``{symbol: MarginInfo}`` from the broker, tolerating brokers without the seam.

    A symbol the broker cannot describe is OMITTED; the engine falls back to the
    conservative ``default_bp_factor``, which under-deploys rather than
    over-commits.
    """
    if not symbols:
        return {}
    try:
        info = account.get_symbol_margin_info(list(symbols))
    except Exception as e:
        logger.error(f"get_symbol_margin_info failed for account {account.id}: {e}", exc_info=True)
        return {}
    return info or {}


def precheck_plan(account, plan: AllocationPlan, *, available_buying_power: float) -> AllocationPlan:
    """Re-solve the plan against broker order prechecks, when the broker has them.

    Solve once (the caller has already done that), build the candidate BUY
    orders, dry-run each through ``preview_order_impact``, and re-solve ONLY if
    at least one impact came back. Alpaca has no order-preview endpoint and
    returns None for every row, so its deterministic per-asset margin data
    stands and this returns the SAME plan object -- no second solve.

    The candidate orders are never persisted and never submitted.
    """
    impacts: Dict[str, Any] = {}
    for row in plan.buy_rows:
        candidate = TradingOrder(
            account_id=account.id,
            symbol=row.symbol,
            quantity=abs(row.delta_quantity),
            side=row.side,
            order_type=OrderType.MARKET,
            good_for='day',
            status=OrderStatus.PENDING,
        )
        try:
            impact = account.preview_order_impact(candidate)
        except Exception as e:
            logger.error(f"preview_order_impact failed for {row.symbol}: {e}", exc_info=True)
            impact = None
        if impact is not None:
            impacts[row.symbol] = impact

    if not impacts:
        return plan

    logger.info(f"Allocation precheck returned {len(impacts)} broker impact(s); re-solving")
    return apply_order_impacts(plan, impacts, available_buying_power=available_buying_power)
```

> **OPEN QUESTION — `apply_order_impacts(margin=...)` is omitted here, and that is not free.**
> The engine's own docstring says "Pass the SAME `margin` dict the plan was solved with:
> without it the re-solve rebuilds a bare `MarginInfo` for each fractional row and so rounds
> on the default 4dp grid, losing `min_trade_increment` and `min_order_size` — a broker-side
> rejection waiting to happen." `precheck_plan` as written above does not take a `margin`
> dict, so it cannot pass one; the caller already has it (it fetched it for the solve). Two
> engine tests, `test_apply_order_impacts_keeps_the_min_trade_increment_on_the_re_solve` and
> `..._keeps_the_min_order_size_on_the_re_solve`, exist precisely because this degrades
> silently. Before implementing, decide: thread `margin` through `precheck_plan` and pass it,
> and/or make the engine's `margin` parameter required. Same shape as the `valuation_mode`
> trap closed in the Task 25 amendment — an omission that produces a plausible-looking wrong
> answer instead of an error.

The module-level import of `plan_quantity_attempts` / `FRACTIONAL_PATH_WHOLE` /
`decide_symbol_action` / `split_delta_fifo` / the `ACTION_*` constants anticipates Tasks 72-73;
they already exist in the engine only from Task 72 onward, so **if you are running Task 69 in
isolation, drop those four names from the import and add them back in Task 72.**

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/core/portfolio_allocation_service.py tests/test_portfolio_allocation_submit.py
git commit -m "feat(allocation): wire the engine to positions, prices, margin info and the broker precheck"
```

---

### Task 70: The dry-run table — pure row/total arithmetic, then the NiceGUI wizard

Pure-testable: `dry_run_rows`, `filter_plan_rows`, `summarise_plan` (Steps 1-4). Eyeball-only:
the NiceGUI rendering in `AllocationWizard` (Steps 6-8) — the automated check is only a smoke
test that the module imports and the entry points are callable with the page's signature.

Nothing in this task writes to the database (decision 10: the dry-run is in-memory).

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py` (append at end of file)
- Create: `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py`
- Test: `packages/common/tests/test_portfolio_allocation_wizard.py` (append)
- Test: `tests/test_portfolio_allocation_wizard_ui.py` (create)

- [ ] **Step 1: Write the failing test**

Append to `packages/common/tests/test_portfolio_allocation_wizard.py`:

```python
from ba2_common.core.portfolio_allocation import (
    AllocationPlan,
    AllocationRow,
    dry_run_rows,
    filter_plan_rows,
    summarise_plan,
)
from ba2_common.core.types import OrderDirection


def _plan():
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", price=160.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1600.0,
                          bp_cost=1600.0, bp_factor=1.0),
            AllocationRow(symbol="NVDA", price=900.0, delta_quantity=4.0,
                          side=OrderDirection.BUY, estimated_value=3600.0,
                          bp_cost=7200.0, bp_factor=2.0,
                          reasons=["⚠ not marginable"]),
            AllocationRow(symbol="MSFT", price=400.0, delta_quantity=-5.0,
                          side=OrderDirection.SELL, estimated_value=2000.0,
                          bp_cost=0.0, bp_factor=1.0),
            AllocationRow(symbol="TSLA", price=None, delta_quantity=0.0,
                          side=None, skipped=True, reasons=["no price - skipped"]),
        ],
        base_notional=20_000.0,
        available_buying_power=10_000.0,
        required_buying_power=8_800.0,
        bp_usage_pct=88.0,
        total_buy_value=5_200.0,
        total_sell_value=2_000.0,
    )


def test_dry_run_rows_shows_one_row_per_non_zero_delta():
    rows = dry_run_rows(_plan())
    assert [r["symbol"] for r in rows] == ["AAPL", "NVDA", "MSFT"]
    assert rows[0]["side"] == "BUY"
    assert rows[2]["side"] == "SELL"
    assert rows[2]["quantity"] == pytest.approx(5.0)  # magnitude, never signed


def test_dry_run_rows_carries_bp_usage_pct_and_reason_strings():
    rows = dry_run_rows(_plan())
    nvda = next(r for r in rows if r["symbol"] == "NVDA")
    assert nvda["bp_cost"] == pytest.approx(7200.0)
    assert nvda["bp_usage_pct"] == pytest.approx(72.0)  # 7200 of 10000
    assert "not marginable" in nvda["reasons"]


def test_filter_plan_rows_unticking_a_buy_drops_it_from_the_totals():
    filtered = filter_plan_rows(_plan(), ["AAPL", "MSFT"])
    assert [r.symbol for r in filtered.rows] == ["AAPL", "MSFT"]
    assert filtered.total_buy_value == pytest.approx(1600.0)
    assert filtered.total_sell_value == pytest.approx(2000.0)
    assert filtered.required_buying_power == pytest.approx(1600.0)
    assert filtered.bp_usage_pct == pytest.approx(16.0)


def test_filter_plan_rows_keeps_the_plan_level_context():
    filtered = filter_plan_rows(_plan(), ["AAPL"])
    assert filtered.base_notional == pytest.approx(20_000.0)
    assert filtered.available_buying_power == pytest.approx(10_000.0)


def test_summarise_plan_estimated_cash_after_nets_buys_against_sells():
    totals = summarise_plan(_plan(), cash=7_000.0)
    assert totals["total_buy_value"] == pytest.approx(5_200.0)
    assert totals["total_sell_value"] == pytest.approx(2_000.0)
    assert totals["net_buy_value"] == pytest.approx(3_200.0)
    assert totals["estimated_cash_after"] == pytest.approx(3_800.0)


def test_summarise_plan_with_no_cash_figure_raises():
    with pytest.raises(ValueError):
        summarise_plan(_plan(), cash=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`

Expected: FAIL at collection with
`ImportError: cannot import name 'dry_run_rows' from 'ba2_common.core.portfolio_allocation'`

- [ ] **Step 3: Write minimal implementation**

Append to `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
# ---------------------------------------------------------------------------
# Wizard step 4: the dry-run table. Pure -- the NiceGUI module only draws these.
# ---------------------------------------------------------------------------

def dry_run_rows(plan: "AllocationPlan") -> List[Dict[str, Any]]:
    """One display dict per NON-ZERO delta row, in plan order.

    Zero-delta rows are omitted (nothing to review). Skipped rows with a delta
    are kept and flagged so the UI can grey them out and show the reason.
    ``quantity`` and every money figure is a POSITIVE magnitude; the direction
    is in ``side``.
    """
    available = float(plan.available_buying_power or 0.0)
    out: List[Dict[str, Any]] = []
    for row in plan.rows:
        if row.side is None or row.delta_quantity == 0:
            continue
        out.append({
            "symbol": row.symbol,
            "side": row.side.value,
            "quantity": round(abs(row.delta_quantity), 4),
            "price": row.price,
            "estimated_value": round(row.estimated_value, 2),
            "bp_cost": round(row.bp_cost, 2),
            "bp_usage_pct": round(row.bp_cost / available * 100.0, 2) if available else 0.0,
            "reasons": ", ".join(row.reasons),
            "skipped": bool(row.skipped),
        })
    return out


def filter_plan_rows(plan: "AllocationPlan", selected_symbols: List[str]) -> "AllocationPlan":
    """A NEW plan holding only the ticked symbols, with the totals recomputed.

    Un-ticking a row must change the buy/sell totals and the buying-power
    requirement the user is about to commit to, so this is what Submit consumes
    -- never the unfiltered plan. ``plan`` is not mutated.
    """
    wanted = {s.strip().upper() for s in selected_symbols}
    rows = [r for r in plan.rows if r.symbol.strip().upper() in wanted]

    buy_value = sum(r.estimated_value for r in rows if r.is_buy)
    sell_value = sum(r.estimated_value for r in rows if r.is_sell)
    required = sum(r.bp_cost for r in rows if r.is_buy)
    available = float(plan.available_buying_power or 0.0)

    return AllocationPlan(
        rows=rows,
        base_notional=plan.base_notional,
        available_buying_power=plan.available_buying_power,
        required_buying_power=required,
        bp_usage_pct=(required / available * 100.0) if available else 0.0,
        scale_factor=plan.scale_factor,
        unallocatable_pct=plan.unallocatable_pct,
        total_buy_value=buy_value,
        total_sell_value=sell_value,
        allow_fractional=plan.allow_fractional,
        warnings=list(plan.warnings),
    )


def summarise_plan(plan: "AllocationPlan", *, cash: float) -> Dict[str, float]:
    """Plan-level totals for the dry-run footer.

    ``estimated_cash_after = cash - buys + sells``. It is an ESTIMATE: market
    orders fill at the fill price, not the quoted one, and off-hours orders queue
    until the open.

    Raises:
        ValueError: if ``cash`` is None -- no fallback for a balance.
    """
    if cash is None:
        raise ValueError("summarise_plan: cash is None; the broker published no cash balance.")
    return {
        "total_sell_value": plan.total_sell_value,
        "total_buy_value": plan.total_buy_value,
        "net_buy_value": plan.net_buy_value,
        "required_buying_power": plan.required_buying_power,
        "available_buying_power": plan.available_buying_power,
        "bp_usage_pct": plan.bp_usage_pct,
        "estimated_cash_after": float(cash) - plan.total_buy_value + plan.total_sell_value,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`
Expected: PASS — 12 passed

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation_wizard.py
git commit -m "feat(allocation): pure dry-run row and total arithmetic"
```

- [ ] **Step 6: Write the failing UI smoke test**

Create `tests/test_portfolio_allocation_wizard_ui.py`:

```python
"""Smoke tests for the Portfolio Allocation NiceGUI wizard module.

These do NOT render anything -- NiceGUI needs a client context. They assert the
module imports cleanly (which catches syntax errors, bad relative imports and
names that drifted from the pure engine) and that the entry points exist with
the signature the allocation page calls them with.
"""
import inspect


def test_wizard_module_imports_and_exposes_its_entry_points():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert hasattr(wiz, "AllocationWizard")
    assert callable(wiz.open_allocation_wizard)
    assert callable(wiz.render_income_panel)
    assert callable(wiz.render_outcomes)


def test_open_allocation_wizard_accepts_the_page_call_signature():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    params = inspect.signature(wiz.open_allocation_wizard).parameters
    assert list(params)[:2] == ["base", "plan"]
    assert "on_refresh" in params
    assert "on_submit" in params
```

- [ ] **Step 7: Run the UI smoke test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v`

Expected: FAIL with
`ModuleNotFoundError: No module named 'ba2_trade_platform.ui.pages.portfolio_allocation_wizard'`

- [ ] **Step 8: Write the wizard module**

Create `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py`:

```python
"""Portfolio Allocation wizard: steps, dry-run dialog, income panel, outcome table.

Section G owns this module. The allocation PAGE
(``ui/pages/portfolio_allocation.py``) renders the label/symbol editor and calls
``open_allocation_steps()``, ``open_allocation_wizard()`` and
``render_income_panel()`` from here.

This module only DRAWS. Every decision lives in
``ba2_common.core.portfolio_allocation`` (pure) or in
``core.portfolio_allocation_service`` (live), both of which are unit-tested
without NiceGUI.

Valid ``ui.notify`` types are 'positive' | 'negative' | 'warning' | 'info' --
'error' is not one of them (settings.py gets this wrong; do not copy it).
"""
from typing import Callable, Dict, List, Optional

from nicegui import ui

from ...core.portfolio_allocation import (
    AllocationPlan,
    BaseSnapshot,
    dry_run_rows,
    filter_plan_rows,
    summarise_plan,
)
from ...logger import logger


class AllocationWizard:
    """The dry-run dialog: base panel, fractional toggle, tickable rows, totals.

    Nothing is written to the database until the user presses Submit, which hands
    the FILTERED plan (ticked rows only) to ``on_submit``.
    """

    def __init__(
        self,
        base: BaseSnapshot,
        plan: AllocationPlan,
        *,
        on_refresh: Callable[[bool], AllocationPlan],
        on_submit: Callable[[AllocationPlan], None],
        title: str = 'Portfolio allocation - dry run',
    ):
        self.base = base
        self.plan = plan
        self.on_refresh = on_refresh
        self.on_submit = on_submit
        self.title = title
        self.allow_fractional = bool(plan.allow_fractional)
        self.selected = {r['symbol'] for r in dry_run_rows(plan) if not r['skipped']}
        self.dialog = None
        self._rows_container = None
        self._totals_container = None

    # -- public -----------------------------------------------------------
    def open(self):
        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            self.dialog = dialog
            ui.label(self.title).classes('text-xl font-bold')
            self._render_base_panel()
            ui.switch('Allow fractional shares', value=self.allow_fractional,
                      on_change=lambda e: self._refresh(bool(e.value)))
            ui.label('Market orders placed outside market hours queue until the open '
                     'and may fill away from these prices.').classes('text-xs text-orange-400')
            self._rows_container = ui.column().classes('w-full gap-0')
            self._totals_container = ui.column().classes('w-full')
            self._render_rows()
            self._render_totals()
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Refresh', on_click=lambda: self._refresh(self.allow_fractional)).props('outline')
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Submit', on_click=self._submit).props('color=primary')
        dialog.open()
        return dialog

    # -- internals --------------------------------------------------------
    def _render_base_panel(self):
        with ui.row().classes('w-full gap-6 items-center'):
            ui.label(f'Buying power: {self.base.available_buying_power:,.2f}')
            ui.label(f'Managed value ({self.base.valuation_mode}): '
                     f'{self.base.managed_value:,.2f}')
            ui.label(f'Base notional: {self.base.base_notional:,.2f}').classes('font-bold')
            ui.label(f"as of {self.base.taken_at:%Y-%m-%d %H:%M UTC}").classes('text-xs text-gray-400')
        for warning in self.base.warnings:
            ui.label(warning).classes('text-xs text-orange-400')
        for warning in self.plan.warnings:
            ui.label(warning).classes('text-xs text-orange-400')

    def _render_rows(self):
        self._rows_container.clear()
        rows = dry_run_rows(self.plan)
        with self._rows_container:
            if not rows:
                ui.label('No orders required - the account already matches its targets.') \
                    .classes('text-sm text-gray-400')
                return
            with ui.row().classes('w-full text-xs font-bold border-b py-1'):
                for header, width in (('', 'w-10'), ('Symbol', 'w-24'), ('Side', 'w-16'),
                                      ('Qty', 'w-24'), ('Est. value', 'w-28'),
                                      ('BP cost', 'w-28'), ('BP %', 'w-16'), ('Reasons', 'flex-1')):
                    ui.label(header).classes(width)
            for row in rows:
                with ui.row().classes('w-full text-sm items-center border-b py-1'):
                    ui.checkbox(
                        value=row['symbol'] in self.selected,
                        on_change=lambda e, s=row['symbol']: self._toggle(s, bool(e.value)),
                    ).classes('w-10').set_enabled(not row['skipped'])
                    ui.label(row['symbol']).classes('w-24 font-medium')
                    ui.label(row['side']).classes(
                        'w-16 ' + ('text-green-500' if row['side'] == 'BUY' else 'text-red-500'))
                    ui.label(f"{row['quantity']:,.4f}").classes('w-24')
                    ui.label(f"{row['estimated_value']:,.2f}").classes('w-28')
                    ui.label(f"{row['bp_cost']:,.2f}").classes('w-28')
                    ui.label(f"{row['bp_usage_pct']:.1f}%").classes('w-16')
                    ui.label(row['reasons']).classes('flex-1 text-xs text-gray-400')

    def _render_totals(self):
        self._totals_container.clear()
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        try:
            totals = summarise_plan(selected_plan, cash=self.base.cash)
        except ValueError:
            totals = None
        with self._totals_container:
            with ui.row().classes('w-full gap-6 mt-2 text-sm'):
                ui.label(f"Sell value: {selected_plan.total_sell_value:,.2f}")
                ui.label(f"Buy value: {selected_plan.total_buy_value:,.2f}")
                ui.label(f"Required BP: {selected_plan.required_buying_power:,.2f} "
                         f"/ {selected_plan.available_buying_power:,.2f} "
                         f"({selected_plan.bp_usage_pct:.1f}%)")
                if totals is not None:
                    ui.label(f"Est. cash after: {totals['estimated_cash_after']:,.2f}")
                else:
                    ui.label('Est. cash after: unknown (broker published no cash balance)') \
                        .classes('text-orange-400')
            if selected_plan.required_buying_power > selected_plan.available_buying_power:
                ui.label('Required buying power exceeds available - the smallest buys will be '
                         'truncated as buying power runs out.').classes('text-xs text-orange-400')

    def _toggle(self, symbol: str, checked: bool):
        if checked:
            self.selected.add(symbol)
        else:
            self.selected.discard(symbol)
        self._render_totals()

    def _refresh(self, allow_fractional: bool):
        self.allow_fractional = allow_fractional
        try:
            self.plan = self.on_refresh(allow_fractional)
        except Exception as e:
            logger.error(f"Allocation dry-run refresh failed: {e}", exc_info=True)
            ui.notify(f'Refresh failed: {e}', type='negative')
            return
        self.selected = {r['symbol'] for r in dry_run_rows(self.plan) if not r['skipped']}
        self._render_rows()
        self._render_totals()
        ui.notify('Dry run refreshed', type='info')

    def _submit(self):
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        if not selected_plan.rows:
            ui.notify('Nothing selected to submit', type='warning')
            return
        if self.dialog is not None:
            self.dialog.close()
        self.on_submit(selected_plan)


def open_allocation_wizard(
    base: BaseSnapshot,
    plan: AllocationPlan,
    *,
    on_refresh: Callable[[bool], AllocationPlan],
    on_submit: Callable[[AllocationPlan], None],
    title: str = 'Portfolio allocation - dry run',
) -> AllocationWizard:
    """Open the dry-run dialog. Returns the wizard so the caller can keep a handle."""
    wizard = AllocationWizard(base, plan, on_refresh=on_refresh, on_submit=on_submit, title=title)
    wizard.open()
    return wizard


def render_income_panel(events: List[Dict], open_total: float,
                        *, on_sync: Callable[[], None],
                        on_invest: Callable[[float], None]) -> None:
    """Placeholder replaced in Task 74."""
    ui.label(f'Unallocated income: {open_total:,.2f}')


def render_outcomes(outcomes: List, *, run_id: Optional[int] = None) -> None:
    """Placeholder replaced in Task 75."""
    ui.label(f'{len(outcomes)} row(s) processed')
```

- [ ] **Step 9: Run the UI smoke test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v`
Expected: PASS — 2 passed

- [ ] **Step 10: Commit**

```bash
git add ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py tests/test_portfolio_allocation_wizard_ui.py
git commit -m "feat(allocation): dry-run wizard dialog with tickable rows and plan totals"
```

---

### Task 71: Wizard steps 1-3 and the INVEST_LABEL mode

> **The engine will not validate for you — this step must call it.** Task 22 shipped
> `validate_label_targets(labels, *, tolerance=LABEL_TOTAL_TOLERANCE_PCT)`, which returns a list of
> human-readable error strings (empty means valid) covering: label percentages totalling 100 within
> 0.01pp, negative label percentages, duplicate labels, a non-zero label with no symbols, and — per
> label — symbol weights totalling 100, negative symbol weights, and a symbol duplicated inside one
> label. Nothing calls it yet. `compute_allocation` deliberately multiplies whatever weights it is
> handed, so an unvalidated 150% symbol set silently over-deploys its label.
>
> Wire it as the submit gate: block progression on a non-empty result and show the strings verbatim
> (they already name the offending label and symbol).
>
> **Generate every default percentage through `even_split_pct`, never by hand.** The 0.01pp tolerance
> is deliberately tight enough to reject a naive 2dp split — `3 x 33.33 = 99.99` fails — which is
> what forces the remainder onto the last slot (`[33.33, 33.33, 33.34]`).
>
> **`steps_validation_messages` as written double-reports every symbol-total error.** Its own loop
> re-appends `ERROR_SYMBOL_TOTAL_FMT` after already calling `validate_label_targets`, but that loop
> predates Task 22 folding the symbol checks into the validator. Simulated against the shipped
> engine it emits the identical string twice. Delete the loop — the body is now just
> `return validate_label_targets(labels, tolerance=tolerance)`.
>
> **Gate INVEST_LABEL separately.** `validate_label_targets` cannot be reused for it: a single
> chosen label at 40% would spuriously fail the labels-total-100 check. Task 23 shipped
> `validate_symbol_weights(label: LabelTarget, *, tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]`
> for exactly this — same list-of-strings contract, symbol level only, ignores `target_pct`. Without
> it a 150% weight set turns a $10,000 budget into $15,000 of buys with nothing blocking it
> (verified). `validate_invest_amount` checks only the amount, so both are needed on that path.


Spec "The wizard": **Rebalance** — step 1 sets label percentages, validated to total exactly 100%,
with an "Even split" button; step 2 sets symbol weights within each label, defaulting to even;
step 3 shows the base breakdown, a Refresh button and the fractional toggle; step 4 is the
dry-run (Task 70). **Invest into one label** — pick a label and an amount, pre-filled with
unallocated income; buys only, no sells.

Pure-testable: `steps_validation_messages` and `even_split_targets` (Steps 1-4). Eyeball-only:
the two dialogs (Steps 6-8). This repo uses no `ui.stepper`, so the steps are drawn as three
sections inside one dialog with a validated Continue button.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py` (append at end of file)
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py` (append)
- Test: `packages/common/tests/test_portfolio_allocation_wizard.py` (append)
- Test: `tests/test_portfolio_allocation_wizard_ui.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `packages/common/tests/test_portfolio_allocation_wizard.py`:

```python
from ba2_common.core.portfolio_allocation import (
    ERROR_INVEST_AMOUNT_FMT,
    LabelTarget,
    SymbolTarget,
    even_split_targets,
    steps_validation_messages,
    validate_invest_amount,
)


def test_even_split_targets_gives_each_label_an_equal_share_totalling_100():
    labels = [LabelTarget("A", 0.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 0.0, [SymbolTarget("BBB", 100.0)]),
              LabelTarget("C", 0.0, [SymbolTarget("CCC", 100.0)])]
    out = even_split_targets(labels)
    assert [t.target_pct for t in out] == [33.33, 33.33, 33.34]
    assert sum(t.target_pct for t in out) == pytest.approx(100.0)
    assert [t.label for t in out] == ["A", "B", "C"]
    # The originals are not mutated -- the dialog can still cancel.
    assert [t.target_pct for t in labels] == [0.0, 0.0, 0.0]


def test_even_split_targets_of_nothing_is_empty():
    assert even_split_targets([]) == []


def test_steps_validation_reports_the_label_total_and_the_symbol_totals():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 40.0), SymbolTarget("BBB", 40.0)]),
              LabelTarget("B", 40.0, [SymbolTarget("CCC", 100.0)])]
    messages = steps_validation_messages(labels)
    assert any("A" in m and "80.00" in m for m in messages)


def test_steps_validation_is_empty_for_a_fully_valid_set():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 50.0), SymbolTarget("BBB", 50.0)]),
              LabelTarget("B", 40.0, [SymbolTarget("CCC", 100.0)])]
    assert steps_validation_messages(labels) == []


def test_steps_validation_includes_the_label_target_errors_too():
    """Step 1's rule (labels total 100) and step 2's rule (weights total 100 inside
    each label) are reported together, so Submit is blocked on either."""
    labels = [LabelTarget("A", 55.0, [SymbolTarget("AAA", 100.0)])]
    messages = steps_validation_messages(labels)
    assert any("must total 100%" in m for m in messages)


def test_validate_invest_amount_accepts_a_positive_amount_within_buying_power():
    assert validate_invest_amount(500.0, available_buying_power=10_000.0) == []


def test_validate_invest_amount_rejects_zero_and_negative():
    assert validate_invest_amount(0.0, available_buying_power=10_000.0) == [
        ERROR_INVEST_AMOUNT_FMT.format(amount=0.0)]
    assert validate_invest_amount(-5.0, available_buying_power=10_000.0)


def test_validate_invest_amount_warns_when_it_exceeds_buying_power():
    messages = validate_invest_amount(20_000.0, available_buying_power=10_000.0)
    assert any("buying power" in m for m in messages)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v -k "even_split_targets or steps_validation or invest_amount"`

Expected: FAIL at collection with
`ImportError: cannot import name 'ERROR_INVEST_AMOUNT_FMT' from 'ba2_common.core.portfolio_allocation'`

- [ ] **Step 3: Write minimal implementation**

Append to `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
# ---------------------------------------------------------------------------
# Wizard steps 1-3: label percentages, symbol weights, and the INVEST amount.
# ---------------------------------------------------------------------------

ERROR_SYMBOL_TOTAL_FMT = ("label '{label}' symbol weights total {total:.2f}% "
                          "- they must total 100%")
ERROR_INVEST_AMOUNT_FMT = "amount {amount:,.2f} must be greater than zero"
WARNING_INVEST_EXCEEDS_BP_FMT = ("amount {amount:,.2f} exceeds available buying power "
                                 "{available:,.2f} - the plan will be scaled down")


def even_split_targets(labels: List[LabelTarget]) -> List[LabelTarget]:
    """The "Even split" button: every label gets an equal share of 100%.

    Returns NEW LabelTarget objects (symbols are shared by reference, which is
    fine -- step 2 replaces them wholesale), so the caller can still cancel out of
    the dialog without having mutated its inputs. The remainder lands on the LAST
    label so the set totals exactly 100.
    """
    items = list(labels or [])
    if not items:
        return []
    return [LabelTarget(label=lt.label, target_pct=pct, symbols=list(lt.symbols),
                        comment=lt.comment)
            for lt, pct in zip(items, even_split_pct(len(items)))]


def steps_validation_messages(labels: List[LabelTarget], *,
                              tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Every reason Submit is blocked, from step 1 AND step 2. Pure.

    Step 1's rule is ``validate_label_targets`` (labels total 100, no duplicates,
    no negatives, no empty non-zero label). Step 2 adds: within each label with
    symbols, the weights must total 100 +/- ``tolerance``.

    Returns:
        List[str]: EMPTY means the wizard may proceed to the dry-run.
    """
    messages = list(validate_label_targets(labels, tolerance=tolerance))
    for lt in labels or []:
        if not lt.symbols:
            continue
        total = sum(float(st.weight_pct or 0.0) for st in lt.symbols)
        if abs(total - 100.0) > tolerance:
            messages.append(ERROR_SYMBOL_TOTAL_FMT.format(label=lt.label, total=total))
    return messages


def validate_invest_amount(amount: float, *, available_buying_power: float) -> List[str]:
    """Validate an INVEST_LABEL amount. Pure -- returns problems, never raises.

    A zero or negative amount is an ERROR. An amount above available buying power
    is reported too, but as an explanation rather than a hard block: the engine
    scales the plan down pro-rata and the dry-run shows the result, which is more
    useful than refusing to compute it.
    """
    messages: List[str] = []
    value = float(amount or 0.0)
    if value <= 0:
        messages.append(ERROR_INVEST_AMOUNT_FMT.format(amount=value))
        return messages
    available = float(available_buying_power or 0.0)
    if value > available:
        messages.append(WARNING_INVEST_EXCEEDS_BP_FMT.format(
            amount=value, available=available))
    return messages
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`
Expected: PASS — 20 passed

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation_wizard.py
git commit -m "feat(allocation): wizard step validation, even split and invest-amount rules"
```

- [ ] **Step 6: Write the failing UI smoke test**

Append to `tests/test_portfolio_allocation_wizard_ui.py`:

```python
def test_wizard_module_exposes_the_steps_entry_point():
    from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz

    assert callable(wiz.open_allocation_steps)
    params = inspect.signature(wiz.open_allocation_steps).parameters
    assert list(params)[:2] == ["base", "labels"]
    assert "on_dry_run" in params
    assert "mode" in params
    assert "invest_amount" in params
```

- [ ] **Step 7: Run the UI smoke test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v -k steps_entry_point`

Expected: FAIL —
`AttributeError: module 'ba2_trade_platform.ui.pages.portfolio_allocation_wizard' has no attribute 'open_allocation_steps'`

- [ ] **Step 8: Write the steps dialog**

In `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py`, extend the engine import to:

```python
from ...core.portfolio_allocation import (
    ALLOCATION_MODE_INVEST_LABEL,
    ALLOCATION_MODE_REBALANCE,
    AllocationPlan,
    BaseSnapshot,
    LabelTarget,
    SymbolTarget,
    dry_run_rows,
    even_split_targets,
    filter_plan_rows,
    steps_validation_messages,
    summarise_plan,
    validate_invest_amount,
)
```

and append:

```python
class AllocationSteps:
    """Steps 1-3 of the wizard, drawn as three sections in ONE dialog.

    This repo uses no ``ui.stepper``, so the three steps are stacked sections
    with a single validated Continue button; ``steps_validation_messages`` (pure)
    decides whether Continue is enabled, and its messages are shown verbatim.

    REBALANCE mode edits label percentages (step 1) and symbol weights (step 2).
    INVEST_LABEL mode replaces step 1 with a label picker plus an amount box and
    skips the 100% rule entirely -- the amount is the whole budget (decision:
    buys only, no sells).

    Nothing is written here. Continue hands the edited targets to ``on_dry_run``,
    which solves and opens ``AllocationWizard``.
    """

    def __init__(self, base: BaseSnapshot, labels: List[LabelTarget], *,
                 on_dry_run: Callable[..., None],
                 mode: str = ALLOCATION_MODE_REBALANCE,
                 invest_amount: float = 0.0):
        self.base = base
        self.labels = [LabelTarget(label=lt.label, target_pct=lt.target_pct,
                                   symbols=[SymbolTarget(st.symbol, st.weight_pct, st.comment)
                                            for st in lt.symbols],
                                   comment=lt.comment)
                       for lt in labels]
        self.on_dry_run = on_dry_run
        self.mode = mode
        self.invest_amount = float(invest_amount or 0.0)
        self.allow_fractional = bool(base.supports_fractional and False)
        self.scope_label = self.labels[0].label if self.labels else None
        self.dialog = None
        self._errors_container = None
        self._continue_button = None

    def open(self):
        title = ('Rebalance - set targets' if self.mode == ALLOCATION_MODE_REBALANCE
                 else 'Invest into one label')
        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            self.dialog = dialog
            ui.label(title).classes('text-xl font-bold')

            if self.mode == ALLOCATION_MODE_REBALANCE:
                self._render_step1_label_targets()
                self._render_step2_symbol_weights()
            else:
                self._render_invest_scope()

            self._render_step3_base_panel()
            self._errors_container = ui.column().classes('w-full')
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                self._continue_button = ui.button('Continue to dry run',
                                                  on_click=self._continue).props('color=primary')
            self._revalidate()
        dialog.open()
        return dialog

    # -- steps ------------------------------------------------------------
    def _render_step1_label_targets(self):
        ui.label('1. Label targets (% of the base notional, must total 100%)') \
            .classes('text-lg font-bold mt-2')
        container = ui.column().classes('w-full gap-1')

        def _draw():
            container.clear()
            with container:
                for lt in self.labels:
                    with ui.row().classes('w-full items-center gap-3'):
                        ui.label(lt.label).classes('w-40 font-medium')
                        ui.number(value=lt.target_pct, min=0, max=100, step=0.01, suffix='%',
                                  on_change=lambda e, t=lt: self._set_label_pct(t, e.value)
                                  ).props('dense outlined').classes('w-32')

        def _even():
            for edited, fresh in zip(self.labels, even_split_targets(self.labels)):
                edited.target_pct = fresh.target_pct
            _draw()
            self._revalidate()

        ui.button('Even split', icon='balance', on_click=_even).props('outline dense')
        _draw()

    def _render_step2_symbol_weights(self):
        ui.label('2. Symbol weights within each label (each label must total 100%)') \
            .classes('text-lg font-bold mt-4')
        for lt in self.labels:
            with ui.expansion(f'{lt.label} — {len(lt.symbols)} symbol(s)').classes('w-full'):
                if not lt.symbols:
                    ui.label('No symbols carry this label — it can absorb no allocation.') \
                        .classes('text-xs text-orange-400')
                    continue
                for st in lt.symbols:
                    with ui.row().classes('w-full items-center gap-3'):
                        ui.label(st.symbol).classes('w-32')
                        ui.number(value=st.weight_pct, min=0, max=100, step=0.01, suffix='%',
                                  on_change=lambda e, t=st: self._set_symbol_pct(t, e.value)
                                  ).props('dense outlined').classes('w-32')

    def _render_invest_scope(self):
        ui.label('1. Which label, and how much').classes('text-lg font-bold mt-2')
        with ui.row().classes('w-full items-center gap-3'):
            ui.select([lt.label for lt in self.labels], value=self.scope_label, label='Label',
                      on_change=self._set_scope).props('dense outlined').classes('w-56')
            ui.number(value=self.invest_amount, min=0, step=0.01, label='Amount',
                      on_change=self._set_amount).props('dense outlined').classes('w-40')
        ui.label('Pre-filled with the unallocated income total. Buys only — an '
                 'INVEST run never sells.').classes('text-xs text-secondary-custom')

    def _render_step3_base_panel(self):
        ui.label('3. What there is to allocate').classes('text-lg font-bold mt-4')
        with ui.row().classes('w-full gap-6 items-center'):
            ui.label(f'Buying power: {self.base.available_buying_power:,.2f}')
            ui.label(f'Managed value ({self.base.valuation_mode}): '
                     f'{self.base.managed_value:,.2f}')
            ui.label(f'Base notional: {self.base.base_notional:,.2f}').classes('font-bold')
            ui.label(f"as of {self.base.taken_at:%Y-%m-%d %H:%M UTC}").classes('text-xs text-gray-400')
        for warning in self.base.warnings:
            ui.label(warning).classes('text-xs text-orange-400')
        ui.switch('Allow fractional shares', value=self.allow_fractional,
                  on_change=lambda e: setattr(self, 'allow_fractional', bool(e.value)))

    # -- state + validation ------------------------------------------------
    def _set_label_pct(self, target: LabelTarget, value):
        target.target_pct = float(value or 0.0)
        self._revalidate()

    def _set_symbol_pct(self, target: SymbolTarget, value):
        target.weight_pct = float(value or 0.0)
        self._revalidate()

    def _set_scope(self, event):
        self.scope_label = event.value
        self._revalidate()

    def _set_amount(self, event):
        self.invest_amount = float(event.value or 0.0)
        self._revalidate()

    def _problems(self) -> List[str]:
        if self.mode == ALLOCATION_MODE_REBALANCE:
            return steps_validation_messages(self.labels)
        messages = validate_invest_amount(
            self.invest_amount, available_buying_power=self.base.available_buying_power)
        if not self.scope_label:
            messages.append('pick a label to invest into')
        return messages

    def _blocking(self, messages: List[str]) -> bool:
        """A buying-power warning explains, it does not block; everything else blocks."""
        return any('exceeds available buying power' not in m for m in messages)

    def _revalidate(self):
        if self._errors_container is None:
            return
        messages = self._problems()
        self._errors_container.clear()
        with self._errors_container:
            for message in messages:
                blocking = 'exceeds available buying power' not in message
                ui.label(('✖ ' if blocking else '⚠ ') + message).classes(
                    'text-xs ' + ('text-red-500' if blocking else 'text-orange-400'))
        if self._continue_button is not None:
            self._continue_button.set_enabled(not self._blocking(messages))

    def _continue(self):
        messages = self._problems()
        if self._blocking(messages):
            ui.notify('Fix the highlighted problems first', type='warning')
            return
        if self.dialog is not None:
            self.dialog.close()
        if self.mode == ALLOCATION_MODE_REBALANCE:
            self.on_dry_run(mode=ALLOCATION_MODE_REBALANCE, labels=self.labels,
                            scope_label=None, amount=0.0,
                            allow_fractional=self.allow_fractional)
        else:
            scope = next((lt for lt in self.labels if lt.label == self.scope_label), None)
            self.on_dry_run(mode=ALLOCATION_MODE_INVEST_LABEL,
                            labels=[scope] if scope else [], scope_label=self.scope_label,
                            amount=self.invest_amount,
                            allow_fractional=self.allow_fractional)


def open_allocation_steps(base: BaseSnapshot, labels: List[LabelTarget], *,
                          on_dry_run: Callable[..., None],
                          mode: str = ALLOCATION_MODE_REBALANCE,
                          invest_amount: float = 0.0) -> AllocationSteps:
    """Open steps 1-3. ``on_dry_run`` is called with keyword arguments
    ``mode``, ``labels``, ``scope_label``, ``amount`` and ``allow_fractional``."""
    steps = AllocationSteps(base, labels, on_dry_run=on_dry_run, mode=mode,
                            invest_amount=invest_amount)
    steps.open()
    return steps
```

- [ ] **Step 9: Run the UI smoke test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v`
Expected: PASS — 3 passed

- [ ] **Step 10: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation.py ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py tests/test_portfolio_allocation_wizard_ui.py packages/common/tests/test_portfolio_allocation_wizard.py
git commit -m "feat(allocation): wizard steps 1-3 for rebalance and invest-into-one-label"
```

---

### Task 72: Submission — sells first, buys descending, three per-symbol branches

Pure-testable: `decide_symbol_action` and `split_delta_fifo` (no IO), and `submit_plan` against
the FakeAccount + in-memory DB. Eyeball-only: nothing.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py` (append at end of file)
- Modify: `ba2_trade_platform/core/portfolio_allocation_service.py` (append at end of file)
- Test: `packages/common/tests/test_portfolio_allocation_wizard.py` (append)
- Test: `tests/test_portfolio_allocation_submit.py` (append)

- [ ] **Step 1: Write the failing pure test**

Append to `packages/common/tests/test_portfolio_allocation_wizard.py`:

```python
from ba2_common.core.portfolio_allocation import (
    ACTION_ADJUST,
    ACTION_CLOSE,
    ACTION_NEW,
    ACTION_SKIP,
    decide_symbol_action,
    split_delta_fifo,
)


def test_decide_symbol_action_not_held_with_a_buy_is_a_new_position():
    row = AllocationRow(symbol="NVDA", price=900.0, delta_quantity=4.0,
                        side=OrderDirection.BUY, target_quantity=4.0)
    assert decide_symbol_action(row, None) == ACTION_NEW


def test_decide_symbol_action_held_with_a_non_zero_target_is_an_adjustment():
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-3.0,
                        side=OrderDirection.SELL, target_quantity=7.0)
    state = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_ADJUST


def test_decide_symbol_action_held_with_a_zero_target_is_a_close():
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-10.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    state = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_CLOSE


def test_decide_symbol_action_skipped_row_is_never_traded():
    row = AllocationRow(symbol="TSLA", price=None, delta_quantity=5.0,
                        side=OrderDirection.BUY, skipped=True,
                        reasons=["no price - skipped"])
    assert decide_symbol_action(row, None) == ACTION_SKIP


def test_decide_symbol_action_sell_of_an_unheld_symbol_is_skipped():
    # Long-only: there is nothing to sell, so this must not become a short.
    row = AllocationRow(symbol="NVDA", price=900.0, delta_quantity=-2.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    assert decide_symbol_action(row, None) == ACTION_SKIP


def test_split_delta_fifo_sell_spans_two_transactions_oldest_first():
    assert split_delta_fifo(-30.0, [(11, 20.0), (12, 15.0)]) == [(11, -20.0), (12, -10.0)]


def test_split_delta_fifo_sell_larger_than_held_is_clamped_to_what_exists():
    assert split_delta_fifo(-50.0, [(11, 20.0), (12, 15.0)]) == [(11, -20.0), (12, -15.0)]


def test_split_delta_fifo_buy_lands_entirely_on_the_oldest_transaction():
    assert split_delta_fifo(7.0, [(11, 20.0), (12, 15.0)]) == [(11, 7.0)]


def test_split_delta_fifo_with_no_transactions_returns_empty():
    assert split_delta_fifo(-5.0, []) == []
    assert split_delta_fifo(0.0, [(11, 20.0)]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`

Expected: FAIL at collection with
`ImportError: cannot import name 'ACTION_ADJUST' from 'ba2_common.core.portfolio_allocation'`

- [ ] **Step 3: Write the pure implementation**

Append to `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
# ---------------------------------------------------------------------------
# Submission decisions. Pure -- the live service does the IO around them.
# ---------------------------------------------------------------------------

ACTION_ADJUST = "adjust"   # held, target > 0, delta != 0 -> adjust_quantity_with_tpsl
ACTION_CLOSE = "close"     # held, target == 0            -> close_transaction
ACTION_NEW = "new"         # not held, target > 0         -> new TradingOrder
ACTION_SKIP = "skip"       # nothing to do (or nothing we are willing to do)


def decide_symbol_action(row: "AllocationRow", state: Optional["PositionState"]) -> str:
    """Which of the three submission paths this row takes (decision 14).

    Long-only: a SELL on a symbol we do not hold would open a short, so it is
    skipped rather than submitted. A row that the engine already marked
    ``skipped`` (no price, precheck rejected) is never traded.
    """
    if row.skipped or row.side is None or row.delta_quantity == 0:
        return ACTION_SKIP

    held = state is not None and (state.quantity or 0.0) > 0 and bool(state.transaction_ids)
    if held:
        return ACTION_CLOSE if row.target_quantity <= 0 else ACTION_ADJUST

    return ACTION_NEW if row.side == OrderDirection.BUY else ACTION_SKIP


def split_delta_fifo(
    delta_quantity: float,
    transaction_quantities: List[Tuple[int, float]],
) -> List[Tuple[int, float]]:
    """Spread a signed delta across a symbol's open transactions, oldest first.

    Args:
        delta_quantity: SIGNED. Negative trims, positive adds.
        transaction_quantities: ``[(transaction_id, quantity)]``, ALREADY sorted
            oldest first.

    Returns:
        List[Tuple[int, float]]: ``[(transaction_id, signed_qty_change)]``, only
        for transactions actually touched. A trim consumes them FIFO and is
        CLAMPED to what is actually held (never oversells). An add lands
        entirely on the OLDEST transaction, so the account keeps one transaction
        per symbol (decision 14). Empty when there is nothing to do.
    """
    if not transaction_quantities or delta_quantity == 0:
        return []

    if delta_quantity > 0:
        return [(transaction_quantities[0][0], float(delta_quantity))]

    remaining = abs(float(delta_quantity))
    out: List[Tuple[int, float]] = []
    for txn_id, quantity in transaction_quantities:
        if remaining <= 0:
            break
        available = float(quantity or 0.0)
        if available <= 0:
            continue
        take = min(available, remaining)
        out.append((txn_id, -take))
        remaining -= take
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`
Expected: PASS — 29 passed

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation_wizard.py
git commit -m "feat(allocation): pure per-symbol submission decision and FIFO delta split"
```

- [ ] **Step 6: Write the failing orchestration test**

Append to `tests/test_portfolio_allocation_submit.py`:

```python
from ba2_trade_platform.core.portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP,
)
from ba2_trade_platform.core.TransactionHelper import TransactionHelper


def test_submit_plan_submits_every_sell_before_any_buy():
    account = FakeAccount(account_id=3)
    account.positions = [FakePosition("MSFT", 5.0, 1800.0, 2000.0)]
    txn_id = make_open_transaction(3, "MSFT", 5.0)
    current = {
        "MSFT": PositionState(symbol="MSFT", quantity=5.0, price=400.0,
                              transaction_ids=[txn_id]),
    }
    plan = AllocationPlan(
        rows=[
            make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0),
            make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0),
        ],
        available_buying_power=10_000.0,
    )
    plan.rows[0].target_quantity = 10.0
    plan.rows[1].target_quantity = 0.0

    svc.submit_plan(account, plan, current, run_tag="17", allow_fractional=False)

    # MSFT is a full close (target 0) -> close_transaction, and it happened before
    # the AAPL buy reached submit_order.
    assert account.closed == [txn_id]
    assert [s[0] for s in account.submitted] == ["AAPL"]


def test_submit_plan_orders_buys_by_descending_estimated_value():
    account = FakeAccount(account_id=4)
    account.positions = []
    plan = AllocationPlan(
        rows=[
            make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0),
            make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0),
            make_row("KO", OrderDirection.BUY, 10.0, 600.0, 600.0, price=60.0),
        ],
        available_buying_power=10_000.0,
    )
    for row in plan.rows:
        row.target_quantity = row.delta_quantity

    svc.submit_plan(account, plan, {}, run_tag="18", allow_fractional=False)

    assert [s[0] for s in account.submitted] == ["NVDA", "AAPL", "KO"]


def test_submit_plan_new_order_comment_never_contains_the_word_closing():
    account = FakeAccount(account_id=5)
    account.positions = []
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="19", allow_fractional=False)

    comment = account.submitted[0][3]
    assert "19" in comment
    assert "closing" not in comment.lower()
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].action == ACTION_NEW
    assert outcomes[0].order_ids


def test_submit_plan_reports_washtrade_locked_instead_of_treating_it_as_success():
    account = FakeAccount(account_id=6)
    account.positions = []
    account.washtrade_symbols = {"AAPL"}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="20", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_WASHTRADE_LOCKED


def test_submit_plan_hard_failure_reports_the_reason_left_on_the_order_comment():
    account = FakeAccount(account_id=8)
    account.positions = []
    account.reject_quantities = {10.0}
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="21", allow_fractional=False)

    assert outcomes[0].status == svc.OUTCOME_FAILED
    assert "broker rejected" in outcomes[0].message


def test_submit_plan_trim_on_a_held_symbol_adjusts_the_transaction_fifo(monkeypatch):
    account = FakeAccount(account_id=9)
    account.positions = [FakePosition("AAPL", 30.0, 3000.0, 3200.0)]
    first = make_open_transaction(9, "AAPL", 20.0)
    second = make_open_transaction(9, "AAPL", 10.0)
    calls = []

    def fake_adjust(acct, transaction, qty_change, tp_price=None, sl_price=None, expert_id=None):
        calls.append((transaction.id, qty_change))
        return {'success': True, 'message': 'ok', 'orders_created': [111], 'orders_canceled': []}

    monkeypatch.setattr(TransactionHelper, 'adjust_quantity_with_tpsl', staticmethod(fake_adjust))

    row = make_row("AAPL", OrderDirection.SELL, -25.0, 2650.0, 0.0, price=106.0)
    row.target_quantity = 5.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=30.0, price=106.0,
                                     transaction_ids=[first, second])}

    outcomes = svc.submit_plan(account, plan, current, run_tag="22", allow_fractional=False)

    assert calls == [(first, -20.0), (second, -5.0)]
    assert outcomes[0].action == ACTION_ADJUST
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED


def test_submit_plan_reports_skipped_rows_without_touching_the_broker():
    account = FakeAccount(account_id=10)
    account.positions = []
    skipped = AllocationRow(symbol="TSLA", price=None, delta_quantity=0.0, side=None,
                            skipped=True, reasons=["no price - skipped"])
    plan = AllocationPlan(rows=[skipped], available_buying_power=10_000.0)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="23", allow_fractional=False)

    assert account.submitted == []
    assert outcomes[0].action == ACTION_SKIP
    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert "no price" in outcomes[0].message
```

- [ ] **Step 7: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`

Expected: FAIL with
`AttributeError: module 'ba2_trade_platform.core.portfolio_allocation_service' has no attribute 'submit_plan'`

- [ ] **Step 8: Write the submission implementation**

Append to `ba2_trade_platform/core/portfolio_allocation_service.py`:

```python
# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

OUTCOME_SUBMITTED = "submitted"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_WASHTRADE_LOCKED = "washtrade_locked"

#: Comment stamped on every order an allocation run creates.
#: It MUST NOT contain the substring "closing" in any case: close_transaction
#: (AccountInterface.py:1531-1536) re-detects an existing close order with
#: ``order_type == MARKET and 'closing' in order.comment.lower()``, and allocation
#: orders are MARKET orders. A comment containing it would make every future
#: close on that symbol believe a close order already exists.
RUN_COMMENT_FMT = "Portfolio allocation run {run_tag} - {side} {symbol}"


@dataclass
class RowOutcome:
    """What actually happened to one dry-run row at submission time."""
    symbol: str
    action: str
    status: str
    quantity: float = 0.0
    path: str = ""
    order_ids: List[int] = field(default_factory=list)
    transaction_ids: List[int] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "action": self.action, "status": self.status,
            "quantity": self.quantity, "path": self.path,
            "order_ids": list(self.order_ids),
            "transaction_ids": list(self.transaction_ids),
            "message": self.message,
        }


def submit_plan(account, plan: AllocationPlan, current: Dict[str, PositionState],
                *, run_tag: str, allow_fractional: bool) -> List[RowOutcome]:
    """Submit a plan: every SELL first, then the BUYs by descending value.

    Decision 13 (sells before buys) and the "buying_power shrinks as buys fill"
    risk: descending value means a shortfall truncates the SMALLEST positions.
    Partial failure is normal: each row reports its own outcome and nothing is
    rolled back.
    """
    outcomes: List[RowOutcome] = []

    for row in plan.sell_rows:
        outcomes.append(_submit_row(account, row, current.get(row.symbol),
                                    run_tag=run_tag, allow_fractional=allow_fractional))
    for row in plan.buy_rows:
        outcomes.append(_submit_row(account, row, current.get(row.symbol),
                                    run_tag=run_tag, allow_fractional=allow_fractional))

    traded = {o.symbol for o in outcomes}
    for row in plan.rows:
        if row.symbol not in traded:
            outcomes.append(RowOutcome(
                symbol=row.symbol, action=ACTION_SKIP, status=OUTCOME_SKIPPED,
                message="; ".join(row.reasons) or "no delta",
            ))
    return outcomes


def _submit_row(account, row, state, *, run_tag: str, allow_fractional: bool) -> RowOutcome:
    action = decide_symbol_action(row, state)
    try:
        if action == ACTION_SKIP:
            return RowOutcome(symbol=row.symbol, action=ACTION_SKIP, status=OUTCOME_SKIPPED,
                              message="; ".join(row.reasons) or "nothing to do")
        if action == ACTION_CLOSE:
            return _close_symbol(account, row, state)
        if action == ACTION_ADJUST:
            return _adjust_symbol(account, row, state)
        return _open_symbol(account, row, run_tag=run_tag, allow_fractional=allow_fractional)
    except Exception as e:
        logger.error(f"Allocation submission failed for {row.symbol}: {e}", exc_info=True)
        return RowOutcome(symbol=row.symbol, action=action, status=OUTCOME_FAILED, message=str(e))


def _close_symbol(account, row, state) -> RowOutcome:
    """Target 0 on a held symbol -> close every open transaction for it."""
    messages: List[str] = []
    closed: List[int] = []
    ok = True
    for txn_id in state.transaction_ids:
        result = account.close_transaction(txn_id)
        if result and result.get('success'):
            closed.append(txn_id)
        else:
            ok = False
            messages.append(f"txn {txn_id}: {(result or {}).get('message', 'close failed')}")
    return RowOutcome(
        symbol=row.symbol, action=ACTION_CLOSE,
        status=OUTCOME_SUBMITTED if ok else OUTCOME_FAILED,
        quantity=abs(row.delta_quantity), transaction_ids=closed,
        message="; ".join(messages),
    )


def _adjust_symbol(account, row, state) -> RowOutcome:
    """Held, target > 0 -> resize the existing transaction(s), FIFO."""
    quantities: List[tuple] = []
    for txn_id in state.transaction_ids:
        try:
            txn = get_instance(Transaction, txn_id)
        except InstanceNotFound:
            logger.warning(f"Allocation: transaction {txn_id} vanished before adjustment")
            continue
        quantities.append((txn_id, float(txn.quantity or 0.0)))

    splits = split_delta_fifo(row.delta_quantity, quantities)
    if not splits:
        return RowOutcome(symbol=row.symbol, action=ACTION_ADJUST, status=OUTCOME_SKIPPED,
                          message="no open transaction quantity to adjust")

    order_ids: List[int] = []
    touched: List[int] = []
    messages: List[str] = []
    ok = True
    for txn_id, qty_change in splits:
        try:
            txn = get_instance(Transaction, txn_id)
        except InstanceNotFound:
            ok = False
            messages.append(f"txn {txn_id}: vanished")
            continue
        result = TransactionHelper.adjust_quantity_with_tpsl(account, txn, qty_change)
        touched.append(txn_id)
        order_ids.extend(result.get('orders_created') or [])
        if not result.get('success'):
            ok = False
            messages.append(f"txn {txn_id}: {result.get('message')}")

    return RowOutcome(
        symbol=row.symbol, action=ACTION_ADJUST,
        status=OUTCOME_SUBMITTED if ok else OUTCOME_FAILED,
        quantity=abs(row.delta_quantity), order_ids=order_ids,
        transaction_ids=touched, message="; ".join(messages),
    )


def _open_symbol(account, row, *, run_tag: str, allow_fractional: bool) -> RowOutcome:
    """Not held, target > 0 -> a brand new MARKET order."""
    quantity = abs(row.delta_quantity)
    return _submit_new_order(account, row, quantity, run_tag=run_tag, path="whole")


def _submit_new_order(account, row, quantity: float, *, run_tag: str, path: str) -> RowOutcome:
    order = TradingOrder(
        account_id=account.id,
        symbol=row.symbol,
        quantity=quantity,
        side=row.side,
        order_type=OrderType.MARKET,
        # Alpaca rejects fractional quantities on GTC and on any non-market type,
        # and AlpacaAccount defaults an unset good_for to GTC (:940).
        good_for='day',
        status=OrderStatus.PENDING,
        open_type=OrderOpenType.MANUAL,
        expert_recommendation_id=None,
        comment=RUN_COMMENT_FMT.format(run_tag=run_tag, side=row.side.value, symbol=row.symbol),
    )
    order_id = add_instance(order, expunge_after_flush=True)
    if not order_id:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path,
                          message="could not persist the TradingOrder")

    result = account.submit_order(order)

    # submit_order returns a TRUTHY order with status WASHTRADE_LOCKED when the
    # gate fires, and None on hard failure with the reason on .comment. Inspect
    # .status, never truthiness.
    if result is None:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path, order_ids=[order_id],
                          message=order.comment or "broker rejected the order")
    if getattr(result, 'status', None) == OrderStatus.WASHTRADE_LOCKED:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW,
                          status=OUTCOME_WASHTRADE_LOCKED, quantity=quantity, path=path,
                          order_ids=[order_id], message="wash-trade gate locked this symbol")
    return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_SUBMITTED,
                      quantity=quantity, path=path, order_ids=[order_id])
```

- [ ] **Step 9: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`
Expected: PASS — 13 passed

- [ ] **Step 10: Commit**

```bash
git add ba2_trade_platform/core/portfolio_allocation_service.py tests/test_portfolio_allocation_submit.py
git commit -m "feat(allocation): submit a plan - sells first, buys descending, three per-symbol paths"
```

---

### Task 73: Fractional shares with a one-shot whole-share fallback

Pure-testable: `plan_quantity_attempts` (no IO) and the retry loop against the FakeAccount, whose
first submit is rejected. Eyeball-only: nothing.

**Files:**
- Modify: `packages/common/ba2_common/core/portfolio_allocation.py` (append at end of file)
- Modify: `ba2_trade_platform/core/portfolio_allocation_service.py` (replace `_open_symbol`)
- Test: `packages/common/tests/test_portfolio_allocation_wizard.py` (append)
- Test: `tests/test_portfolio_allocation_submit.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `packages/common/tests/test_portfolio_allocation_wizard.py`:

```python
from ba2_common.core.portfolio_allocation import (
    FRACTIONAL_PATH_FRACTIONAL,
    FRACTIONAL_PATH_WHOLE,
    plan_quantity_attempts,
)


def test_plan_quantity_attempts_fractional_first_then_whole_shares():
    attempts = plan_quantity_attempts(2.5, allow_fractional=True, fractionable=True)
    assert attempts == [(FRACTIONAL_PATH_FRACTIONAL, 2.5), (FRACTIONAL_PATH_WHOLE, 2.0)]


def test_plan_quantity_attempts_sub_one_share_has_no_whole_share_fallback():
    attempts = plan_quantity_attempts(0.4, allow_fractional=True, fractionable=True)
    assert attempts == [(FRACTIONAL_PATH_FRACTIONAL, 0.4)]


def test_plan_quantity_attempts_sub_one_share_without_fractional_is_skipped():
    # floor(0.4) == 0 -> nothing to attempt, which the caller reports as SKIPPED.
    assert plan_quantity_attempts(0.4, allow_fractional=False, fractionable=True) == []


def test_plan_quantity_attempts_already_whole_needs_no_fractional_attempt():
    attempts = plan_quantity_attempts(3.0, allow_fractional=True, fractionable=True)
    assert attempts == [(FRACTIONAL_PATH_WHOLE, 3.0)]


def test_plan_quantity_attempts_non_fractionable_symbol_goes_straight_to_whole():
    attempts = plan_quantity_attempts(2.5, allow_fractional=True, fractionable=False)
    assert attempts == [(FRACTIONAL_PATH_WHOLE, 2.0)]


def test_plan_quantity_attempts_uses_the_magnitude_of_a_signed_delta():
    attempts = plan_quantity_attempts(-4.0, allow_fractional=False, fractionable=False)
    assert attempts == [(FRACTIONAL_PATH_WHOLE, 4.0)]
```

Append to `tests/test_portfolio_allocation_submit.py`:

```python
def test_submit_plan_retries_whole_shares_once_when_the_fractional_order_is_rejected():
    account = FakeAccount(account_id=11)
    account.positions = []
    account.reject_quantities = {2.5}   # broker refuses the fractional quantity
    row = make_row("NVDA", OrderDirection.BUY, 2.5, 2250.0, 2250.0, price=900.0)
    row.target_quantity = 2.5
    row.fractional = True
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0, allow_fractional=True)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="30", allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5, 2.0]
    assert outcomes[0].status == svc.OUTCOME_SUBMITTED
    assert outcomes[0].path == "whole"
    assert outcomes[0].quantity == pytest.approx(2.0)


def test_submit_plan_reports_the_fractional_path_when_it_is_accepted():
    account = FakeAccount(account_id=12)
    account.positions = []
    row = make_row("NVDA", OrderDirection.BUY, 2.5, 2250.0, 2250.0, price=900.0)
    row.target_quantity = 2.5
    row.fractional = True
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0, allow_fractional=True)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="31", allow_fractional=True)

    assert [s[2] for s in account.submitted] == [2.5]
    assert outcomes[0].path == "fractional"


def test_submit_plan_fractional_order_is_sent_good_for_day_as_a_market_order():
    account = FakeAccount(account_id=13)
    account.positions = []
    sent = []
    original = account.submit_order

    def spy(order, tp_price=None, sl_price=None, is_closing_order=False):
        sent.append((order.good_for, order.order_type))
        return original(order, tp_price, sl_price, is_closing_order)

    account.submit_order = spy
    row = make_row("NVDA", OrderDirection.BUY, 2.5, 2250.0, 2250.0, price=900.0)
    row.target_quantity = 2.5
    row.fractional = True
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0, allow_fractional=True)

    svc.submit_plan(account, plan, {}, run_tag="32", allow_fractional=True)

    assert sent == [('day', OrderType.MARKET)]


def test_submit_plan_reports_skipped_not_failed_when_the_whole_share_floor_is_zero():
    account = FakeAccount(account_id=14)
    account.positions = []
    row = make_row("BRK.A", OrderDirection.BUY, 0.4, 260_000.0, 260_000.0, price=650_000.0)
    row.target_quantity = 0.4
    plan = AllocationPlan(rows=[row], available_buying_power=500_000.0, allow_fractional=False)

    outcomes = svc.submit_plan(account, plan, {}, run_tag="33", allow_fractional=False)

    assert account.submitted == []
    assert outcomes[0].status == svc.OUTCOME_SKIPPED
    assert "whole share" in outcomes[0].message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`

Expected: FAIL at collection with
`ImportError: cannot import name 'FRACTIONAL_PATH_FRACTIONAL' from 'ba2_common.core.portfolio_allocation'`

- [ ] **Step 3: Write minimal implementation**

Append to `packages/common/ba2_common/core/portfolio_allocation.py`:

```python
# ---------------------------------------------------------------------------
# Fractional shares: which quantities to attempt, and in what order (decision 12).
# ---------------------------------------------------------------------------

FRACTIONAL_PATH_FRACTIONAL = "fractional"
FRACTIONAL_PATH_WHOLE = "whole"


def plan_quantity_attempts(
    quantity: float,
    *,
    allow_fractional: bool,
    fractionable: bool,
) -> List[Tuple[str, float]]:
    """The ordered submission attempts for one order quantity. Pure.

    Fractional ON, symbol fractionable and the quantity really is fractional ->
    try the fractional quantity first, then ONE retry at ``floor(quantity)``.
    Anything else goes straight to whole shares.

    Returns:
        List[Tuple[str, float]]: ``[(path, quantity)]``, first attempt first.
        EMPTY when ``floor(quantity)`` is 0 and fractional is unavailable -- the
        caller reports that as SKIPPED, not as a failure.
    """
    magnitude = abs(float(quantity))
    whole = float(math.floor(magnitude))

    if allow_fractional and fractionable and magnitude > whole:
        attempts = [(FRACTIONAL_PATH_FRACTIONAL, magnitude)]
        if whole > 0:
            attempts.append((FRACTIONAL_PATH_WHOLE, whole))
        return attempts

    return [(FRACTIONAL_PATH_WHOLE, whole)] if whole > 0 else []
```

In `ba2_trade_platform/core/portfolio_allocation_service.py`, replace the whole `_open_symbol`
function with:

```python
def _open_symbol(account, row, *, run_tag: str, allow_fractional: bool) -> RowOutcome:
    """Not held, target > 0 -> a brand new MARKET order, with the fractional fallback.

    Alpaca rejects a fractional quantity on GTC or on a non-market order type, so
    every attempt goes in as MARKET / good_for='day'. On rejection the order is
    retried ONCE at floor(qty); a floor of 0 is SKIPPED, not a failure. The row
    reports which path succeeded.
    """
    attempts = plan_quantity_attempts(
        row.delta_quantity,
        allow_fractional=allow_fractional,
        fractionable=bool(row.fractional),
    )
    if not attempts:
        return RowOutcome(
            symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_SKIPPED,
            quantity=abs(row.delta_quantity),
            message="below one whole share and fractional is off - nothing submitted",
        )

    last = None
    for path, quantity in attempts:
        last = _submit_new_order(account, row, quantity, run_tag=run_tag, path=path)
        if last.status != OUTCOME_FAILED:
            return last
        logger.warning(
            f"Allocation: {row.symbol} rejected at qty={quantity} ({path}); "
            f"{'retrying at whole shares' if path != FRACTIONAL_PATH_WHOLE else 'no retry left'}"
        )
    return last
```

(The service's module-level import already lists `plan_quantity_attempts` and
`FRACTIONAL_PATH_WHOLE` — see Task 69's note; add them now if you dropped them there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`
Expected: PASS — 35 passed

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`
Expected: PASS — 17 passed

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/portfolio_allocation.py packages/common/tests/test_portfolio_allocation_wizard.py ba2_trade_platform/core/portfolio_allocation_service.py tests/test_portfolio_allocation_submit.py
git commit -m "feat(allocation): fractional submission with a one-shot whole-share fallback"
```

---

### Task 74: Income ledger — sync on load/Refresh only, FIFO consumption

Pure-testable: the broker→ledger sync and the consumption wrappers against the FakeAccount +
in-memory DB. Eyeball-only: `render_income_panel`'s layout — the automated check is the smoke
test that the module still imports.

The pure FIFO rule (`consume_income_events`) and the DB writes (`upsert_income_event`,
`consume_income`, `get_open_income_total`, `get_income_events_since`) already exist — in the
engine (Task 24) and in the store (Tasks 12-13). This task adds ONLY the broker-facing sync and
thin service wrappers, so there is exactly one implementation of each rule.

The ledger syncs on page load and on explicit Refresh **only** — never on a `ui.timer`, so the
page never issues broker calls in the background.

**Files:**
- Modify: `ba2_trade_platform/core/portfolio_allocation_service.py` (append at end of file)
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py` (replace `render_income_panel`)
- Test: `tests/test_portfolio_allocation_submit.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_portfolio_allocation_submit.py`:

```python
from datetime import date

from sqlmodel import select

from ba2_trade_platform.core.account_types import (
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL, CashTransfer,
)
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import PortfolioIncomeEvent


def test_sync_income_events_writes_deposits_and_dividends():
    account = FakeAccount(account_id=21)
    account.cash_transfers = [
        CashTransfer(external_id="csd-1", event_date=date(2026, 8, 1),
                     event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0),
        CashTransfer(external_id="div-1", event_date=date(2026, 8, 5),
                     event_type=CASH_TRANSFER_DIVIDEND, amount=42.5, symbol="AAPL"),
    ]

    written = svc.sync_income_events(account)

    assert written == 2
    with get_db() as session:
        rows = session.exec(select(PortfolioIncomeEvent)).all()
    assert {r.external_id for r in rows} == {"csd-1", "div-1"}


def test_sync_income_events_skips_withdrawals():
    account = FakeAccount(account_id=22)
    account.cash_transfers = [
        CashTransfer(external_id="csw-1", event_date=date(2026, 8, 2),
                     event_type=CASH_TRANSFER_WITHDRAWAL, amount=-1_000.0),
    ]

    assert svc.sync_income_events(account) == 0
    with get_db() as session:
        assert session.exec(select(PortfolioIncomeEvent)).all() == []


def test_sync_income_events_is_idempotent_on_the_broker_activity_id():
    account = FakeAccount(account_id=23)
    account.cash_transfers = [
        CashTransfer(external_id="csd-9", event_date=date(2026, 8, 1),
                     event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0),
    ]

    assert svc.sync_income_events(account) == 1
    assert svc.sync_income_events(account) == 0

    with get_db() as session:
        rows = session.exec(select(PortfolioIncomeEvent)).all()
    assert len(rows) == 1


def test_sync_income_events_returns_zero_when_the_broker_call_fails():
    """A broker outage must not look like "there was no income"; it is logged and
    the existing ledger is left alone."""
    account = FakeAccount(account_id=27)

    def _boom(start_date=None, end_date=None):
        raise RuntimeError("gateway timeout")

    account.get_cash_transfers = _boom
    assert svc.sync_income_events(account) == 0


def test_consume_income_for_run_consumes_oldest_first_and_persists_the_remainder():
    first = add_instance(PortfolioIncomeEvent(
        account_id=25, external_id="a", event_date=date(2026, 8, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=300.0))
    second = add_instance(PortfolioIncomeEvent(
        account_id=25, external_id="b", event_date=date(2026, 8, 5),
        event_type=CASH_TRANSFER_DIVIDEND, amount=500.0))

    taken = svc.consume_income_for_run(25, 450.0)

    assert taken == [(first, pytest.approx(300.0)), (second, pytest.approx(150.0))]
    with get_db() as session:
        rows = {r.id: r for r in session.exec(select(PortfolioIncomeEvent)).all()}
    assert rows[first].open_amount == pytest.approx(0.0)
    assert rows[second].open_amount == pytest.approx(350.0)


def test_consume_income_for_run_with_a_sell_funded_rebalance_consumes_nothing():
    event_id = add_instance(PortfolioIncomeEvent(
        account_id=26, external_id="a", event_date=date(2026, 8, 1),
        event_type=CASH_TRANSFER_DEPOSIT, amount=300.0))

    assert svc.consume_income_for_run(26, 0.0) == []

    with get_db() as session:
        row = session.exec(select(PortfolioIncomeEvent).where(
            PortfolioIncomeEvent.id == event_id)).one()
    assert row.consumed_amount == pytest.approx(0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v -k "income"`

Expected: FAIL — `AttributeError: module 'ba2_trade_platform.core.portfolio_allocation_service' has no attribute 'sync_income_events'`

- [ ] **Step 3: Write minimal implementation**

Append to `ba2_trade_platform/core/portfolio_allocation_service.py`:

```python
# ---------------------------------------------------------------------------
# Income ledger (deposits + dividends), synced on page load and Refresh only.
# ---------------------------------------------------------------------------

#: How far back the page syncs and displays the ledger.
INCOME_WINDOW_DAYS = 30


def sync_income_events(account, *, days: int = INCOME_WINDOW_DAYS) -> int:
    """Upsert the broker's cash movements into ``portfolio_income_event``.

    Only ``CashTransfer.is_income`` rows (positive deposits and dividends) are
    persisted -- a withdrawal is not income. The DB write is
    ``portfolio_allocation_store.upsert_income_event``, keyed on
    ``(account_id, external_id)``, so re-syncing the same window updates rather
    than duplicating.

    Never runs on a timer: the caller invokes it on page load and on explicit
    Refresh, so the page issues no background broker calls.

    Returns:
        int: how many NEW events were inserted (updates are not counted; a broker
        failure is logged and returns 0 rather than looking like "no income").
    """
    from .portfolio_allocation_store import get_open_income_events, upsert_income_event

    end_date = Date.today()
    start_date = end_date - timedelta(days=days)
    try:
        transfers = account.get_cash_transfers(start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.error(f"get_cash_transfers failed for account {account.id}: {e}", exc_info=True)
        return 0

    known = {row.external_id for row in get_open_income_events(account.id)}
    inserted = 0
    for transfer in transfers or []:
        if not transfer.is_income:
            continue
        existed = transfer.external_id in known
        upsert_income_event(account.id, transfer.external_id, transfer.event_date,
                            transfer.event_type, transfer.amount, symbol=transfer.symbol)
        if not existed:
            inserted += 1
            known.add(transfer.external_id)

    logger.info(f"Income sync for account {account.id}: {inserted} new event(s)")
    return inserted


def get_recent_income_events(account_id: int, *,
                             days: int = INCOME_WINDOW_DAYS) -> List[Dict[str, Any]]:
    """The last ``days`` of income events, newest first, as display dicts."""
    from .portfolio_allocation_store import get_income_events_since

    cutoff = Date.today() - timedelta(days=days)
    return [{
        "id": row.id,
        "event_date": row.event_date,
        "event_type": row.event_type,
        "symbol": row.symbol,
        "amount": row.amount,
        "open_amount": row.open_amount,
    } for row in get_income_events_since(account_id, cutoff)]


def get_open_income_total(account_id: int) -> float:
    """Total un-consumed income for this account, across the WHOLE ledger."""
    from .portfolio_allocation_store import get_open_income_total as _store_total
    return _store_total(account_id)


def consume_income_for_run(account_id: int, net_buy_value: float) -> List[Tuple[int, float]]:
    """Consume the ledger oldest-first against a run's NET buy value.

    Thin wrapper over ``portfolio_allocation_store.consume_income`` (which itself
    delegates the FIFO arithmetic to the pure
    ``portfolio_allocation.consume_income_events``), so there is exactly ONE
    implementation of the rule and the service keeps one import surface.

    Returns:
        List[Tuple[int, float]]: ``[(income_event_id, amount_consumed)]``. Empty
        when the run bought nothing net (a rebalance funded by its own sells).
    """
    from .portfolio_allocation_store import consume_income
    return consume_income(account_id, net_buy_value)
```

`get_open_income_events` is imported inside `sync_income_events` only to tell an insert from an
update; the counting could equally be done in the store, but keeping it here leaves the store's
upsert a pure upsert.

Replace `render_income_panel` in `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py`
with:

```python
def render_income_panel(events: List[Dict], open_total: float,
                        *, on_sync: Callable[[], None],
                        on_invest: Callable[[float], None]) -> None:
    """Last 30 days of income, the open total, and the Invest shortcut.

    The panel NEVER polls. ``on_sync`` is wired to the Refresh button and is
    additionally called once by the page on load; there is deliberately no
    ``ui.timer`` here, so the page issues no background broker calls.

    ``on_invest(open_total)`` opens the wizard in INVEST_LABEL mode pre-filled
    with the unallocated amount.
    """
    with ui.card().classes('w-full'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Income (last 30 days)').classes('text-lg font-bold')
            with ui.row().classes('gap-2 items-center'):
                ui.label(f'Unallocated: {open_total:,.2f}').classes('font-bold text-green-500')
                ui.button('Refresh', on_click=on_sync).props('outline dense')
                ui.button('Invest', on_click=lambda: on_invest(open_total)) \
                    .props('color=primary dense').set_enabled(open_total > 0)
        if not events:
            ui.label('No deposits or dividends in the last 30 days.') \
                .classes('text-sm text-gray-400')
            return
        with ui.row().classes('w-full text-xs font-bold border-b py-1'):
            for header, width in (('Date', 'w-28'), ('Type', 'w-24'), ('Symbol', 'w-24'),
                                  ('Amount', 'w-28'), ('Open', 'w-28')):
                ui.label(header).classes(width)
        for event in events:
            with ui.row().classes('w-full text-sm border-b py-1'):
                ui.label(str(event['event_date'])).classes('w-28')
                ui.label(event['event_type']).classes('w-24')
                ui.label(event['symbol'] or '-').classes('w-24')
                ui.label(f"{event['amount']:,.2f}").classes('w-28')
                ui.label(f"{event['open_amount']:,.2f}").classes('w-28')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`
Expected: PASS — 23 passed

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v`
Expected: PASS — 3 passed

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/core/portfolio_allocation_service.py ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py tests/test_portfolio_allocation_submit.py
git commit -m "feat(allocation): income ledger sync, FIFO consumption wrapper and the income panel"
```

---

### Task 75: Record the run, log the activity, show the per-row outcome table

Pure-testable: `summarise_outcomes` (no IO), and `run_allocation` against the FakeAccount +
in-memory DB. Eyeball-only: `render_outcomes`' layout.

Partial failure is normal: the run row records exactly what was submitted, and nothing is
rolled back.

**Files:**
- Modify: `ba2_trade_platform/core/portfolio_allocation_service.py` (append at end of file)
- Modify: `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py` (replace `render_outcomes`)
- Test: `tests/test_portfolio_allocation_submit.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_portfolio_allocation_submit.py`:

```python
from ba2_trade_platform.core.models import PortfolioAllocationRun
from ba2_trade_platform.core.portfolio_allocation import (
    ALLOCATION_MODE_INVEST_LABEL, ALLOCATION_MODE_REBALANCE, BaseSnapshot,
)


def make_base(buying_power=10_000.0, managed_value=5_000.0, cash=4_000.0):
    return BaseSnapshot(
        available_buying_power=buying_power,
        managed_value=managed_value,
        base_notional=buying_power + managed_value,
        default_bp_factor=1.0,
        cash=cash,
    )


def test_summarise_outcomes_counts_only_the_rows_that_were_submitted():
    row_ok = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row_bad = make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0)
    row_sell = make_row("MSFT", OrderDirection.SELL, -5.0, 2000.0, 0.0, price=400.0)
    plan = AllocationPlan(rows=[row_ok, row_bad, row_sell], available_buying_power=10_000.0)

    outcomes = [
        svc.RowOutcome(symbol="AAPL", action="new", status=svc.OUTCOME_SUBMITTED,
                       quantity=10.0, order_ids=[101]),
        svc.RowOutcome(symbol="NVDA", action="new", status=svc.OUTCOME_FAILED,
                       quantity=4.0, order_ids=[102]),
        svc.RowOutcome(symbol="MSFT", action="close", status=svc.OUTCOME_SUBMITTED,
                       quantity=5.0, transaction_ids=[7]),
    ]

    totals = svc.summarise_outcomes(plan, outcomes)

    assert totals["submitted_buy_value"] == pytest.approx(1600.0)
    assert totals["submitted_sell_value"] == pytest.approx(2000.0)
    assert totals["net_buy_value"] == pytest.approx(0.0)
    assert totals["order_ids"] == [101]


def test_summarise_outcomes_uses_the_quantity_that_actually_went_in():
    # Fractional 2.5 was rejected and 2.0 whole shares filled instead.
    row = make_row("NVDA", OrderDirection.BUY, 2.5, 2250.0, 2250.0, price=900.0)
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)
    outcomes = [svc.RowOutcome(symbol="NVDA", action="new", status=svc.OUTCOME_SUBMITTED,
                               quantity=2.0, path="whole", order_ids=[9])]

    totals = svc.summarise_outcomes(plan, outcomes)

    assert totals["submitted_buy_value"] == pytest.approx(1800.0)


def test_run_allocation_persists_a_run_row_carrying_the_plan_and_the_order_ids():
    account = FakeAccount(account_id=31)
    account.positions = []
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], base_notional=15_000.0,
                          available_buying_power=10_000.0, total_buy_value=1600.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    with get_db() as session:
        run = session.exec(select(PortfolioAllocationRun)).one()
    assert run.id == result["run_id"]
    assert run.mode == ALLOCATION_MODE_REBALANCE
    assert run.base_notional == pytest.approx(15_000.0)
    assert run.submitted_buy_value == pytest.approx(1600.0)
    assert run.order_ids == result["outcomes"][0].order_ids
    assert run.plan_json["rows"][0]["symbol"] == "AAPL"


def test_run_allocation_stamps_the_run_id_into_every_order_comment():
    account = FakeAccount(account_id=32)
    account.positions = []
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    comment = account.submitted[0][3]
    assert str(result["run_id"]) in comment
    assert "closing" not in comment.lower()


def test_run_allocation_partial_failure_records_only_what_was_submitted():
    account = FakeAccount(account_id=33)
    account.positions = []
    account.reject_quantities = {4.0}
    ok = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    ok.target_quantity = 10.0
    bad = make_row("NVDA", OrderDirection.BUY, 4.0, 3600.0, 3600.0, price=900.0)
    bad.target_quantity = 4.0
    plan = AllocationPlan(rows=[ok, bad], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)

    with get_db() as session:
        run = session.exec(select(PortfolioAllocationRun)).one()
    assert run.submitted_buy_value == pytest.approx(1600.0)
    statuses = {o.symbol: o.status for o in result["outcomes"]}
    assert statuses["AAPL"] == svc.OUTCOME_SUBMITTED
    assert statuses["NVDA"] == svc.OUTCOME_FAILED


def test_run_allocation_consumes_income_up_to_the_net_buy_value():
    account = FakeAccount(account_id=34)
    account.positions = []
    add_instance(PortfolioIncomeEvent(account_id=34, external_id="dep-1",
                                      event_date=date(2026, 8, 1),
                                      event_type=CASH_TRANSFER_DEPOSIT, amount=5_000.0))
    row = make_row("AAPL", OrderDirection.BUY, 10.0, 1600.0, 1600.0, price=160.0)
    row.target_quantity = 10.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000.0)

    result = svc.run_allocation(account, plan, {}, make_base(),
                                mode=ALLOCATION_MODE_INVEST_LABEL, scope_label="ARK26")

    assert result["income_consumed"] == pytest.approx(1600.0)
    assert svc.get_open_income_total(34) == pytest.approx(3_400.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v -k "summarise_outcomes or run_allocation"`

Expected: FAIL — `AttributeError: module 'ba2_trade_platform.core.portfolio_allocation_service' has no attribute 'summarise_outcomes'`

- [ ] **Step 3: Write minimal implementation**

Append to `ba2_trade_platform/core/portfolio_allocation_service.py`:

```python
# ---------------------------------------------------------------------------
# Run audit: portfolio_allocation_run + log_activity + income consumption.
# ---------------------------------------------------------------------------

def summarise_outcomes(plan: AllocationPlan, outcomes: List[RowOutcome]) -> Dict[str, Any]:
    """What a run ACTUALLY committed, as opposed to what it planned. Pure.

    Only OUTCOME_SUBMITTED rows count. The value uses the quantity that really
    went in (a fractional order that fell back to whole shares is worth less than
    the plan said), falling back to the row's estimate when there is no price.
    """
    by_symbol = {row.symbol: row for row in plan.rows}
    buy_value = 0.0
    sell_value = 0.0
    order_ids: List[int] = []

    for outcome in outcomes:
        if outcome.status != OUTCOME_SUBMITTED:
            continue
        row = by_symbol.get(outcome.symbol)
        if row is None:
            continue
        if row.price and outcome.quantity:
            value = float(row.price) * float(outcome.quantity)
        else:
            value = float(row.estimated_value)
        if row.is_buy:
            buy_value += value
        elif row.is_sell:
            sell_value += value
        order_ids.extend(outcome.order_ids)

    return {
        "submitted_buy_value": buy_value,
        "submitted_sell_value": sell_value,
        "net_buy_value": max(0.0, buy_value - sell_value),
        "order_ids": order_ids,
    }


def run_allocation(account, plan: AllocationPlan, current: Dict[str, PositionState],
                   base: BaseSnapshot, *, mode: str,
                   scope_label: Optional[str] = None) -> Dict[str, Any]:
    """Submit a reviewed plan and record it. The single Submit entry point.

    Order of operations:
      1. INSERT the ``portfolio_allocation_run`` row with the plan snapshot and
         zero submitted values, so its id can be stamped into every order comment.
      2. Submit (sells first, buys descending, per-row outcomes).
      3. UPDATE the run row with what was actually submitted.
      4. Consume the income ledger oldest-first by the NET buy value.
      5. log_activity.

    Partial failure is normal and is reported per row; nothing is rolled back.
    """
    from .portfolio_allocation_store import record_allocation_run, update_allocation_run_totals

    run = record_allocation_run(
        account.id, mode, plan.to_dict(),
        scope_label=scope_label,
        base_notional=base.base_notional,
        available_buying_power=base.available_buying_power,
        allow_fractional=bool(plan.allow_fractional),
    )
    run_id = run.id

    outcomes = submit_plan(account, plan, current, run_tag=str(run_id),
                           allow_fractional=bool(plan.allow_fractional))
    totals = summarise_outcomes(plan, outcomes)

    try:
        update_allocation_run_totals(
            run_id,
            submitted_buy_value=totals["submitted_buy_value"],
            submitted_sell_value=totals["submitted_sell_value"],
            order_ids=totals["order_ids"])
    except InstanceNotFound:
        logger.error(f"Allocation run {run_id} vanished before its totals could be written")

    consumed = consume_income_for_run(account.id, totals["net_buy_value"])
    income_consumed = float(sum(amount for _, amount in consumed))

    submitted = sum(1 for o in outcomes if o.status == OUTCOME_SUBMITTED)
    failed = sum(1 for o in outcomes if o.status == OUTCOME_FAILED)
    log_activity(
        ActivityLogSeverity.SUCCESS if failed == 0 else ActivityLogSeverity.WARNING,
        ActivityLogType.ORDER_SUBMITTED,
        f"Portfolio allocation run {run_id} ({mode}"
        f"{' / ' + scope_label if scope_label else ''}): "
        f"{submitted} submitted, {failed} failed",
        data={
            "run_id": run_id,
            "mode": mode,
            "scope_label": scope_label,
            "submitted_buy_value": totals["submitted_buy_value"],
            "submitted_sell_value": totals["submitted_sell_value"],
            "income_consumed": income_consumed,
            "rows": [o.to_dict() for o in outcomes],
        },
        source_account_id=account.id,
    )

    return {
        "run_id": run_id,
        "outcomes": outcomes,
        "submitted_buy_value": totals["submitted_buy_value"],
        "submitted_sell_value": totals["submitted_sell_value"],
        "order_ids": totals["order_ids"],
        "income_consumed": income_consumed,
    }
```

Replace `render_outcomes` in `ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py` with:

```python
OUTCOME_COLOURS = {
    'submitted': 'text-green-500',
    'skipped': 'text-gray-400',
    'washtrade_locked': 'text-orange-400',
    'failed': 'text-red-500',
}


def render_outcomes(outcomes: List, *, run_id: Optional[int] = None) -> None:
    """Per-row outcome table shown after Submit.

    Partial failure is normal: a failed row sits next to a filled one and nothing
    is rolled back, so every row is listed with its own status and message.
    """
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
        title = f'Allocation run {run_id} - results' if run_id else 'Allocation run - results'
        ui.label(title).classes('text-lg font-bold')
        with ui.row().classes('w-full text-xs font-bold border-b py-1'):
            for header, width in (('Symbol', 'w-24'), ('Action', 'w-24'), ('Status', 'w-36'),
                                  ('Qty', 'w-24'), ('Path', 'w-24'), ('Detail', 'flex-1')):
                ui.label(header).classes(width)
        for outcome in outcomes:
            with ui.row().classes('w-full text-sm border-b py-1'):
                ui.label(outcome.symbol).classes('w-24 font-medium')
                ui.label(outcome.action).classes('w-24')
                ui.label(outcome.status).classes(
                    'w-36 ' + OUTCOME_COLOURS.get(outcome.status, ''))
                ui.label(f'{outcome.quantity:,.4f}').classes('w-24')
                ui.label(outcome.path or '-').classes('w-24')
                ui.label(outcome.message or '').classes('flex-1 text-xs text-gray-400')
        with ui.row().classes('w-full justify-end mt-2'):
            ui.button('Close', on_click=dialog.close).props('flat')
    dialog.open()
    failed = sum(1 for o in outcomes if o.status == 'failed')
    if failed:
        ui.notify(f'{failed} row(s) failed - see the results table', type='warning')
    else:
        ui.notify('Allocation run submitted', type='positive')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v`
Expected: PASS — 29 passed

Run: `venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v`
Expected: PASS — 3 passed

Run: `venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v`
Expected: PASS — 35 passed

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/core/portfolio_allocation_service.py ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py tests/test_portfolio_allocation_submit.py
git commit -m "feat(allocation): persist the run, log the activity, show per-row outcomes"
```

---

## Section H — Adjacent fix: growth-chart label persistence

Spec decision 18 and the "Adjacent fix" section. Unrelated to allocation, but the same label
machinery, and the user asked for it alongside.

`OverviewTab._render_growth_by_label_charts` builds its "Labels shown" selector at
`ui/pages/overview.py:5453-5455` with `default_labels = [l for l in labels if l != 'auto_added']`,
and the selection is lost on every reload. It persists to `app.storage.user` under an
`overview_growth_labels` key, seeded from the existing default when absent and intersected with
the currently available labels so a deleted label cannot break the chart. **Session storage, not
the database.**

`ui/pages/symbol360.py:36` and `:163-181` are the precedent, including the constraint that
matters: `app.storage.user` raises `RuntimeError` outside a UI context, so reads and writes must
be guarded and must not happen from a thread pool. The storage secret is already configured at
`ui/main.py:173`.

---

### Task 76: Persist the Overview growth-chart label selection

> **Also fix the two label-normalisation bypasses in this chart's own code.** Task 1 made the
> shared helpers normalise symbols to `.strip().upper()`, but `overview.py:5775-5796` **and**
> `overview.py:6098-6111` are two independent copies of a pattern that builds
> `symbol_labels[inst.name] = inst.labels` straight from `Instrument` rows and then looks up with
> `pos.symbol`, bypassing `get_labels_by_symbol` and therefore all of that normalisation. Route both
> through `get_labels_by_symbol`, or normalise both the key and the lookup. Fix BOTH sites — fixing
> only the first leaves the same bug in the second chart.


Pure-testable: `resolve_growth_labels` (Steps 1-4), plus the two storage helpers' RuntimeError
guards. Eyeball-only: the two-line wiring in `overview.py`.

**Files:**
- Create: `ba2_trade_platform/ui/utils/growth_label_storage.py`
- Modify: `ba2_trade_platform/ui/pages/overview.py:5453-5462`
- Test: `tests/test_overview_growth_labels.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_overview_growth_labels.py`:

```python
"""The Overview growth-by-label chart remembers which labels you ticked.

`resolve_growth_labels` is pure -- stored selection in, effective selection out --
so every rule is unit-tested with no browser. The two storage helpers are tested
only for their RuntimeError guard: `app.storage.user` raises outside a UI context
(ui/pages/symbol360.py documents the same constraint), and neither helper may let
that escape into the chart.
"""
from ba2_trade_platform.ui.utils.growth_label_storage import (
    GROWTH_LABELS_STORAGE_KEY,
    read_growth_labels,
    resolve_growth_labels,
    write_growth_labels,
)


def test_no_stored_selection_falls_back_to_everything_except_auto_added():
    available = ['auto_added', 'ARK26', 'NASDAQ30']
    assert resolve_growth_labels(None, available) == ['ARK26', 'NASDAQ30']


def test_an_empty_stored_selection_is_respected_not_treated_as_missing():
    """Un-ticking every label is a real choice; it must not silently reset."""
    assert resolve_growth_labels([], ['ARK26', 'NASDAQ30']) == []


def test_a_stored_selection_is_returned_in_the_available_order():
    available = ['ARK26', 'NASDAQ30', 'HighRisk']
    assert resolve_growth_labels(['HighRisk', 'ARK26'], available) == ['ARK26', 'HighRisk']


def test_a_deleted_label_is_dropped_from_the_stored_selection():
    """A label that no longer exists must not break the chart."""
    assert resolve_growth_labels(['ARK26', 'GONE'], ['ARK26', 'NASDAQ30']) == ['ARK26']


def test_a_stored_selection_that_no_longer_matches_anything_falls_back():
    """Every stored label is gone -> show the default rather than an empty chart."""
    assert resolve_growth_labels(['GONE', 'ALSO_GONE'], ['ARK26']) == ['ARK26']


def test_with_only_auto_added_available_the_default_shows_it_rather_than_nothing():
    assert resolve_growth_labels(None, ['auto_added']) == ['auto_added']


def test_no_available_labels_at_all_yields_an_empty_selection():
    assert resolve_growth_labels(None, []) == []
    assert resolve_growth_labels(['ARK26'], []) == []


def test_read_growth_labels_outside_a_ui_context_returns_none_instead_of_raising():
    """app.storage.user raises RuntimeError with no client; the chart must still draw."""
    assert read_growth_labels() is None


def test_write_growth_labels_outside_a_ui_context_does_not_raise():
    write_growth_labels(['ARK26'])   # must be a silent, logged no-op


def test_the_storage_key_is_the_documented_one():
    assert GROWTH_LABELS_STORAGE_KEY == 'overview_growth_labels'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_overview_growth_labels.py -v`
Expected: FAIL at collection with
`ModuleNotFoundError: No module named 'ba2_trade_platform.ui.utils.growth_label_storage'`

- [ ] **Step 3: Write minimal implementation**

Create `ba2_trade_platform/ui/utils/growth_label_storage.py`:

```python
"""Session persistence for the Overview growth-by-label chart's label selection.

Session storage, NOT the database: this is a per-user view preference, not
allocation state that changes money. ``ui/pages/symbol360.py`` is the precedent
for both the ``app.storage.user`` pattern and its one hard constraint --
``app.storage.user`` raises ``RuntimeError`` outside a UI context, so it must be
guarded and must never be touched from a thread pool.

``resolve_growth_labels`` is pure and carries every rule; the two helpers around
it do nothing but read and write, guarded.
"""
from typing import List, Optional

from ...logger import logger

#: app.storage.user key holding the user's growth-chart label selection.
GROWTH_LABELS_STORAGE_KEY = 'overview_growth_labels'

#: Excluded from the DEFAULT selection (it is the machine tag on almost every
#: instrument, so including it would drown the chart). A user who deliberately
#: ticks it still gets it -- this only shapes the fallback.
GROWTH_LABELS_DEFAULT_EXCLUDED = ('auto_added',)


def resolve_growth_labels(stored: Optional[List[str]],
                          available: List[str]) -> List[str]:
    """The labels the chart should show, given what was stored and what exists now.

    Rules, in order:
      * ``stored is None`` (never saved) -> the historical default: everything
        except ``auto_added``, or everything when that would be empty.
      * ``stored == []`` -> respected. Un-ticking every label is a real choice.
      * otherwise -> the stored labels that STILL EXIST, in ``available`` order so
        the chart's series order is stable. A deleted label cannot break it.
      * if that intersection is empty but ``stored`` was not, fall back to the
        default rather than drawing an empty chart.

    Pure: no storage, no NiceGUI.
    """
    options = [l for l in (available or []) if l]
    default = [l for l in options if l not in GROWTH_LABELS_DEFAULT_EXCLUDED] or list(options)

    if stored is None:
        return default
    if not stored:
        return []

    wanted = {s for s in stored if s}
    kept = [l for l in options if l in wanted]
    return kept if kept else default


def read_growth_labels() -> Optional[List[str]]:
    """The stored selection, or ``None`` when nothing is stored / storage is unavailable.

    UI-thread only. ``app.storage.user`` raises ``RuntimeError`` outside a UI
    context (e.g. from an ``asyncio.to_thread`` worker), which is caught here so
    the chart still draws with its default.
    """
    try:
        from nicegui import app
        value = app.storage.user.get(GROWTH_LABELS_STORAGE_KEY)
    except (RuntimeError, AttributeError) as e:
        logger.debug(f"Growth labels: storage unavailable for read: {e}")
        return None
    if value is None:
        return None
    return [str(v) for v in value]


def write_growth_labels(labels: List[str]) -> None:
    """Persist the selection. UI-thread only -- see ``read_growth_labels``.

    A storage failure is logged and swallowed: losing a view preference must never
    break the page.
    """
    try:
        from nicegui import app
        app.storage.user[GROWTH_LABELS_STORAGE_KEY] = [str(l) for l in (labels or [])]
    except (RuntimeError, AttributeError) as e:
        logger.warning(f"Growth labels: could not persist the selection: {e}")
```

In `ba2_trade_platform/ui/pages/overview.py`, add this import to the module's import block:

```python
from ..utils.growth_label_storage import read_growth_labels, resolve_growth_labels, write_growth_labels
```

and inside `_render_growth_by_label_charts`, replace lines 5453-5462, which read exactly:

```python
            default_labels = [l for l in labels if l != 'auto_added'] or list(labels)
            label_select = ui.select(options=list(labels), value=default_labels, multiple=True,
                                     label='Labels shown').classes('w-72 mb-2')
            chart_container = ui.column().classes('w-full')

            def rebuild():
                visible = list(label_select.value) if label_select.value else []
                chart_container.clear()
                with chart_container:
                    ui.echart(build_options(visible, mode.value == '%')).classes('w-full').style('height: 320px')

            with chart_container:
                ui.echart(build_options(default_labels, False)).classes('w-full').style('height: 320px')
```

with:

```python
            # The selection persists in app.storage.user (session, not the DB) and is
            # intersected with the labels that still exist, so a deleted label cannot
            # break the chart. See ui/utils/growth_label_storage.py.
            default_labels = resolve_growth_labels(read_growth_labels(), list(labels))
            label_select = ui.select(options=list(labels), value=default_labels, multiple=True,
                                     label='Labels shown').classes('w-72 mb-2')
            chart_container = ui.column().classes('w-full')

            def rebuild():
                visible = list(label_select.value) if label_select.value else []
                write_growth_labels(visible)
                chart_container.clear()
                with chart_container:
                    ui.echart(build_options(visible, mode.value == '%')).classes('w-full').style('height: 320px')

            with chart_container:
                ui.echart(build_options(default_labels, False)).classes('w-full').style('height: 320px')
```

`rebuild` is already wired to the selector's change handler further down the method, so no other
change is needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_overview_growth_labels.py -v`
Expected: PASS — 10 passed.

Then confirm the growth-chart tests that already exist still pass:
Run: `venv/bin/python -m pytest tests/test_overview.py -v`
Expected: PASS (if `tests/test_overview.py` does not exist in your tree, skip this and rely on
the eyeball check below).

Eyeball check: start the app, open `/`, scroll to "Growth by label", untick `NASDAQ30`, reload
the browser, and confirm the selection came back as you left it. Then delete that label from an
instrument in Settings, reload, and confirm the chart still draws with the remaining labels.

- [ ] **Step 5: Commit**

```bash
git add ba2_trade_platform/ui/utils/growth_label_storage.py ba2_trade_platform/ui/pages/overview.py tests/test_overview_growth_labels.py
git commit -m "feat(ui): persist the Overview growth-chart label selection in session storage"
```

---

## Section I — Split the trade and test-platform versions

The test platform's distributed GA workers decide whether to self-update by comparing a single
string: `worker_client.ensure_synced` (`testplatform/backend/app/services/worker_client.py:337-370`)
keys on `app_version` and *deliberately* not on the git commit, so that "ordinary pushes — docs,
scratch scripts, unrelated fixes — don't force every connected worker to self-update mid-run".

But `self_update._app_version` (`testplatform/backend/app/services/self_update.py:61-70`) reads that
string out of **`ba2_trade_platform/version.py`** — the *trade* app's file — and
`unsyncable_reason` (`:121`) git-statuses that same trade file. So a trade-only version bump is
indistinguishable from a real test-platform change: every worker re-syncs for nothing, and an
uncommitted trade bump trips the retry-and-exclude path that burns 300 s per worker per selection.

Giving the test platform its own version file decouples them.

**The rule this establishes, which Task 79 then follows:**

- Change under `ba2_trade_platform/` only → bump `ba2_trade_platform/version.py`.
- Change under `testplatform/` **or `packages/`** → bump `testplatform/version.py`.

`packages/` counts because both apps import it and sync is keyed on the version string alone: a
shared-code change without a test-version bump leaves workers running different `ba2_common` code
from the master, which silently breaks the determinism guarantee `self_update` exists to provide.
**This branch changes `packages/common` heavily, so it bumps both.**

### Task 77: Give the test platform its own version file

**Files:**
- Create: `testplatform/version.py`
- Modify: `testplatform/backend/app/services/self_update.py:61-70`, `:121`
- Test: `testplatform/backend/tests/test_self_update_version_split.py`

- [ ] **Step 1: Write the failing test**

```python
"""The test platform's reported app_version must come from its OWN version file,
so that bumping the trade app's version does not force every GA worker to re-sync."""
import re
from pathlib import Path

from app.services import self_update


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_app_version_reads_the_testplatform_file_not_the_trade_file(tmp_path):
    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.9999"\n')
    _write(tmp_path, "testplatform/version.py", 'TEST_APP_VERSION = "2026.08.0042"\n')

    assert self_update._app_version(tmp_path) == "2026.08.0042"


def test_app_version_ignores_a_trade_only_bump(tmp_path):
    _write(tmp_path, "testplatform/version.py", 'TEST_APP_VERSION = "2026.08.0042"\n')
    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.1067"\n')
    before = self_update._app_version(tmp_path)

    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.1068"\n')
    after = self_update._app_version(tmp_path)

    assert before == after == "2026.08.0042"


def test_app_version_is_unknown_when_the_testplatform_file_is_missing(tmp_path):
    assert self_update._app_version(tmp_path) == "unknown"


def test_shipped_testplatform_version_file_is_parseable():
    root = self_update.resolve_repo_root()
    body = (root / "testplatform" / "version.py").read_text(encoding="utf-8")
    m = re.search(r"""TEST_APP_VERSION\s*=\s*["']([^"']+)["']""", body)
    assert m, "testplatform/version.py must define TEST_APP_VERSION"
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", m.group(1)), m.group(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd testplatform/backend && ../../venv/bin/python -m pytest tests/test_self_update_version_split.py -v`
Expected: FAIL — the first test asserts `"2026.08.0042"` but gets `"2026.08.9999"` (it is still
reading the trade file), and the last two fail with `FileNotFoundError` / `unknown` because
`testplatform/version.py` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `testplatform/version.py`:

```python
# Test platform version — format: YYYY.MM.NNNNN
# NNNNN is the sequential build number.
#
# Bump this for ANY change under testplatform/ or packages/. The distributed GA
# workers key their self-update decision on this string alone (see
# backend/app/services/worker_client.py:ensure_synced), so a change to the shared
# packages/ code that does NOT bump this leaves workers running different code from
# the master — which silently breaks trial reproducibility.
#
# Changes confined to ba2_trade_platform/ bump ba2_trade_platform/version.py instead.
TEST_APP_VERSION = "2026.08.1067"
```

In `testplatform/backend/app/services/self_update.py`, replace `_app_version` (lines 61-70):

```python
def _app_version(root: Path) -> str:
    """Read ``TEST_APP_VERSION`` from ``testplatform/version.py`` by FILE (not import).

    This is the TEST platform's own version, deliberately independent of the trade app's
    ``ba2_trade_platform/version.py``. Workers key their self-update decision on this string
    (``worker_client.ensure_synced``), so a trade-app-only release must not churn them.

    Reading the file rather than importing it works from either venv and from a plain
    monorepo checkout, where the module may not be importable.
    """
    vf = root / "testplatform" / "version.py"
    try:
        m = re.search(r"""TEST_APP_VERSION\s*=\s*["']([^"']+)["']""", vf.read_text(encoding="utf-8"))
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"
```

Then in `unsyncable_reason`, change the git-status path (line 121) from the trade file to the
test-platform one:

```python
            ["git", "status", "--porcelain", "--", "testplatform/version.py"],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd testplatform/backend && ../../venv/bin/python -m pytest tests/test_self_update_version_split.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add testplatform/version.py testplatform/backend/app/services/self_update.py testplatform/backend/tests/test_self_update_version_split.py
git commit -m "feat(testplatform): own version file so trade-app bumps stop churning GA workers"
```

---

### Task 78: Document the two-version rule and keep the existing sync test green

**Files:**
- Modify: `CLAUDE.md`
- Modify: `testplatform/CLAUDEBT.md`
- Test: `testplatform/backend/tests/test_self_update_unsyncable.py`

- [ ] **Step 1: Run the existing sync test to see whether the previous task broke it**

Run: `cd testplatform/backend && ../../venv/bin/python -m pytest tests/test_self_update_unsyncable.py -v`
Expected: it may FAIL, because `unsyncable_reason` now git-statuses `testplatform/version.py`
while the test still stages `ba2_trade_platform/version.py`. Read the failure before changing
anything.

- [ ] **Step 2: Update the existing test to the new path**

In `testplatform/backend/tests/test_self_update_unsyncable.py`, every place that creates, writes
or `git add`s `ba2_trade_platform/version.py` becomes `testplatform/version.py`, and every
`APP_VERSION = ` literal in a fixture body becomes `TEST_APP_VERSION = `. Do not change the
assertions — the behaviour under test (uncommitted → reason, unpushed → reason, clean → None) is
unchanged; only which file carries the version has moved.

- [ ] **Step 3: Run it to verify it passes**

Run: `cd testplatform/backend && ../../venv/bin/python -m pytest tests/test_self_update_unsyncable.py -v`
Expected: PASS

- [ ] **Step 4: Write the rule into both CLAUDE files**

In `CLAUDE.md`, replace the body of the `## Versioning` section's bump instruction with:

```markdown
**Before every `git push`, increment the build number (NNNNN) by 1** in the file that matches
what you changed:

| What you changed | Bump |
|---|---|
| `ba2_trade_platform/` only | `ba2_trade_platform/version.py` (`APP_VERSION`) |
| `testplatform/` **or `packages/`** | `testplatform/version.py` (`TEST_APP_VERSION`) |
| both | both files |

`packages/` counts as a test-platform change because the distributed GA workers decide whether to
self-update by comparing `TEST_APP_VERSION` alone
(`testplatform/backend/app/services/worker_client.py:ensure_synced`). A shared-package change that
does not bump it leaves workers running different `ba2_common` code from the master, which
silently breaks trial reproducibility.

Update the year/month when they change.
```

Add the same table to `testplatform/CLAUDEBT.md` under its own versioning heading.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md testplatform/CLAUDEBT.md testplatform/backend/tests/test_self_update_unsyncable.py
git commit -m "docs: document the trade/test two-version bump rule"
```

---

## Final

### Task 79: Bump both versions, run the full per-file sweep, and commit

This branch changes `packages/common` heavily — new models, the label-helper normalisation, the
new account seams, the `TradeActions.py` fix — so per the Task 78 rule it bumps **both** version
files, not just the trade one.

**Files:**
- Modify: `ba2_trade_platform/version.py:3`
- Modify: `testplatform/version.py`

- [ ] **Step 1: Bump the version**

In `ba2_trade_platform/version.py`, replace line 3:

```python
APP_VERSION = "2026.08.1067"
```

with:

```python
APP_VERSION = "2026.08.1068"
```

If the branch has moved on and the current value is higher than `2026.08.1067`, bump whatever is
actually there by one instead.

- [ ] **Step 1b: Bump the test-platform version too**

`packages/common` changed, so the GA workers must re-sync. In `testplatform/version.py`, bump
`TEST_APP_VERSION` by one on the same rule:

```python
TEST_APP_VERSION = "2026.08.1068"
```

- [ ] **Step 2: Run the full per-file test sweep**

The full suite fails non-deterministically from a pre-existing session leak, so **run per file**.
Every file this plan created or touched:

```bash
# Section A — instrument uniqueness
venv/bin/python -m pytest tests/test_instrument_labels.py -v
venv/bin/python -m pytest tests/test_instrument_symbol_import.py -v
venv/bin/python -m pytest tests/test_instrument_autoadd_normalisation.py -v
venv/bin/python -m pytest tests/test_instrument_merge.py -v
venv/bin/python -m pytest tests/test_instrument_unique_migration.py -v
venv/bin/python -m pytest tests/test_instrument_unique_constraint.py -v

# Section B — models, migration, store
venv/bin/python -m pytest tests/test_portfolio_allocation_models.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_migration.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_store.py -v

# Section C — pure engine
venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_shim.py -v

# Section D — account seams and Alpaca
venv/bin/python -m pytest packages/common/tests/test_account_types.py -v
venv/bin/python -m pytest tests/test_account_types_shim.py -v
venv/bin/python -m pytest packages/common/tests/test_account_seams.py -v
venv/bin/python -m pytest packages/common/tests/test_preview_order_impact.py -v
venv/bin/python -m pytest tests/test_alpaca_account_snapshot.py -v
venv/bin/python -m pytest tests/test_alpaca_margin_info.py -v
venv/bin/python -m pytest tests/test_alpaca_cash_transfers.py -v
venv/bin/python -m pytest tests/test_alpaca_fractional_submission.py -v
venv/bin/python -m pytest packages/common/tests/test_increase_instrument_share_buying_power.py -v

# Section E — TastyTrade
venv/bin/python -m pytest tests/test_broker_sdk_pins.py -v
venv/bin/python -m pytest tests/test_tastytrade_account.py -v

# Section F — page
venv/bin/python -m pytest packages/common/tests/test_manual_trading_setting.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_route.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_view.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_account_deletion.py -v

# Section G — wizard and submission
venv/bin/python -m pytest packages/common/tests/test_portfolio_allocation_wizard.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_submit.py -v
venv/bin/python -m pytest tests/test_portfolio_allocation_wizard_ui.py -v

# Section H — growth chart
venv/bin/python -m pytest tests/test_overview_growth_labels.py -v

# Section I — trade/test version split (run from testplatform/backend)
cd testplatform/backend && ../../venv/bin/python -m pytest tests/test_self_update_version_split.py -v
cd testplatform/backend && ../../venv/bin/python -m pytest tests/test_self_update_unsyncable.py -v
cd ../..

# Pre-existing suites this plan could have broken
venv/bin/python -m pytest packages/common/tests/test_utils_pure.py -v
venv/bin/python -m pytest packages/common/tests/test_interfaces_import.py -v
venv/bin/python -m pytest packages/common/tests/test_cleanroom_gate.py -v
venv/bin/python -m pytest packages/common/tests/test_trade_actions_account_interface_inmem.py -v
venv/bin/python -m pytest tests/test_alias_shim_race.py -v
venv/bin/python -m pytest tests/test_models.py -v
venv/bin/python -m pytest tests/test_settings.py -v
venv/bin/python -m pytest tests/test_job_scheduling.py -v
venv/bin/python -m pytest tests/test_boot_smoke.py -v
venv/bin/python -m pytest tests/test_accounts/test_account_interface.py -v
venv/bin/python -m pytest tests/test_accounts/test_alpaca_idempotency.py -v
venv/bin/python -m pytest tests/test_accounts/test_broker_error_handling.py -v
venv/bin/python -m pytest tests/test_alpaca_order_type_mapping.py -v
venv/bin/python -m pytest tests/test_alpaca_options.py -v
```

Expected: every file PASSES. Do NOT accept a green from `venv/bin/python -m pytest` with no path
— that run is unreliable here.

Also confirm the migration chain still has exactly one head:

```bash
PYTHONPATH=packages/common:packages/providers:packages/experts venv/bin/python -m alembic heads
```
Expected: `f1c8a24b7e05 (head)` — one line, no branch warning.

- [ ] **Step 3: Commit**

```bash
git add ba2_trade_platform/version.py testplatform/version.py
git commit -m "chore: bump APP_VERSION and TEST_APP_VERSION to 2026.08.1068"
```

- [ ] **Step 4: Applying the destructive migration to a real database (manual, once)**

Not part of the TDD loop. See the note at the end of Section A: dry-run first, back up, then
upgrade. On this Mac the live DB is stamped `d5e1b9a3c842` — two revisions behind head — and
`init_db()`'s `create_all` has already materialised tables Alembic does not know about, so check
`PRAGMA table_info` and consider `alembic stamp` before upgrading it.

---

## ROLLOUT ORDERING — read before the next app start

**`migrate.py upgrade` must run BEFORE the app starts again.** `init_db()` calls
`SQLModel.metadata.create_all()` (`packages/common/ba2_common/core/db.py:368`), and Task 7 registered
the five allocation models — so the next app start creates all five tables *outside Alembic's
knowledge*. Reproduced: starting the app first and then upgrading gives

```
sqlalchemy.exc.OperationalError: table portfolio_allocation_config already exists
```

If the app has already started and `create_all` has made the tables, do **not** upgrade — run
`alembic stamp f1c8a24b7e05` instead. Stamping is safe here specifically because Task 8 proved the
two construction paths produce byte-equivalent schemas (`compare_metadata()` → 0 diffs over the whole
database), and that proof is the only reason the escape hatch is legitimate.

The same hazard already existed for `option_activity`, `option_iv_snapshot` and `provider_cache`,
which `create_all` materialised before Alembic knew about them — which is why the live DB may fail an
upgrade with a duplicate-column error. See Task 5's runbook.

---

## Known limitations (deliberate, from the spec)

1. **Allocation plans are not covered by settings export.** `settings_export_import.py` only
   knows `AppSetting`, `AccountDefinition` + `AccountSetting` and `ExpertInstance` +
   `ExpertSetting`. Adding the five allocation tables is out of scope.
2. **`InstrumentAutoAdder.py:96-101` mutates `existing.labels` in place** on a plain JSON column
   with no `MutableList` wrapper, so the subsequent commit emits no UPDATE and every label the
   auto-adder tries to add to an EXISTING instrument is silently lost. Out of scope: the two-line
   fix would start persisting thousands of expert labels and further pollute the label list,
   which deserves its own decision.
3. **No `get_clock` / `extended_hours` handling** anywhere in `AlpacaAccount`: off-hours market
   orders queue until the open, at prices that may differ from the dry-run. The dry-run states
   this rather than blocking submission.
4. **TastyTrade is unit-tested against a mocked SDK only.** There is no TastyTrade account in the
   live database, so live verification is the user's.
5. **Out of scope for TastyTrade:** `modify_order`, TP/SL adjustment, complex orders and the
   whole of `OptionsAccountInterface`.
