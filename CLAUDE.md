# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BA2 Trade Platform is a Python-based algorithmic trading platform featuring AI-driven market analysis, multi-agent trading strategies, and a plugin architecture for accounts and market experts. Built with SQLModel ORM, NiceGUI web interface, and the TradingAgents multi-agent LLM framework.

## Common Commands

### Running the Application
```bash
# Windows
.venv\Scripts\python.exe main.py

# Linux/macOS
.venv/bin/python main.py

# With custom options
python main.py --port 9090 --db-file ./dev.db
```

### Installing Dependencies
```bash
# With uv (recommended - faster)
uv pip install -r requirements.txt

# With pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Database Migrations (Alembic)
```bash
python migrate.py create "Description of changes"  # Create migration
python migrate.py upgrade                          # Apply migrations
python migrate.py downgrade -1                     # Rollback one revision
python migrate.py current                          # Check current revision
```

### Running Tests
```bash
# Unit tests (pytest)
.venv\Scripts\python.exe -m pytest              # Run all tests
.venv\Scripts\python.exe -m pytest -x            # Stop on first failure
.venv\Scripts\python.exe -m pytest -k "test_name" # Run specific test

# Ad-hoc / one-off scripts (NOT collected by pytest)
.venv\Scripts\python.exe test_files/test_name.py
```

**`tests/` vs `test_files/`:** `tests/` is the pytest suite (the only thing
`pytest` collects — see `pytest.ini` `testpaths = tests`). `test_files/` holds
ad-hoc investigation/probe scripts and `__main__`-style harnesses that pytest does
NOT run. When a script under `test_files/` becomes a durable regression test, port it
into `tests/` (as `test_*.py`) so it runs in CI; keep throwaway probes out of `tests/`.

### PyTorch / Transformers
PyTorch is a transitive dependency (via `transformers` used by `langchain_core`). On Windows, use the **CPU-only** build to avoid CUDA DLL issues:
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
```
Do NOT upgrade torch to the latest (e.g. 2.10+) blindly - it causes `OSError: [WinError 1114]` DLL load failures on Windows. Pin to a known working version (e.g. `torch==2.6.0+cpu`).

## Architecture

### Plugin System
- **AccountInterface**: Base class for broker integrations (e.g., `AlpacaAccount`, `TastyTradeAccount`, `IBKRAccount`)
- **MarketExpertInterface**: Base class for AI trading experts (e.g., `TradingAgents`, `FMPRating`)
- **ExtendableSettingsInterface**: Shared settings management via database key-value storage

### Interactive Brokers (IBKR) account
`IBKRAccount` (`modules/accounts/IBKRAccount.py`, + `ibkr_runtime.py`, `ibkr_options.py`) was written **with no live
IBKR account to test against**: everything is exercised against `tests/ibkr_fakes.py`, a behavioural fake of
`ib_async.IB`. Read `docs/plans/2026-10-03-ibkr-support-design.md` before changing it: it lists every UNVERIFIED IBKR
fact and the conservative choice taken for each. Rules specific to this adapter:
- **Never call `ib_async` from a platform thread.** Each account definition has ONE runtime (private asyncio loop
  thread + one TWS session, `ibkr_runtime.get_runtime`) shared by every `IBKRAccount` object; use `self._call(...)`.
  `TradeManager` builds a fresh account object per call, so a connection per object would collide on the client id.
- Pure mapping rules (status table, error codes, OCC symbols, ticks, snapshot maths) live in
  `ba2_common/core/ibkr_mapping.py`; the shared TP/SL exit-order maintenance in `ba2_common/core/protective_legs.py`
  (not yet adopted by Alpaca). `ib_async` is imported only in `modules/accounts/` (CI installs `packages/*` only).
- Operator smoke test against a PAPER Gateway: `tools/ibkr_paper_smoke.py` (read-only by default). Setup:
  `docs/IBKR-SETUP.md`.

### Core Directory Structure
```
ba2_trade_platform/
├── core/                    # Interfaces, models, utilities
│   ├── interfaces/          # Abstract base classes (AccountInterface, MarketExpertInterface)
│   ├── models.py            # SQLModel database models
│   ├── types.py             # Enums (OrderStatus, OrderDirection, RiskLevel, etc.)
│   ├── db.py                # Database helpers (get_instance, add_instance, update_instance)
│   ├── utils.py             # Shared utilities (get_expert_instance_from_id, etc.)
│   ├── TradeManager.py      # Order processing and recommendation handling
│   ├── JobManager.py        # Background job scheduling
│   └── WorkerQueue.py       # Task queue for parallel processing
├── modules/
│   ├── accounts/            # Broker implementations (AlpacaAccount, IBKRAccount, TastyTradeAccount)
│   ├── experts/             # Expert implementations (TradingAgents, FMPRating, etc.)
│   └── dataproviders/       # Market data providers (news, indicators, OHLCV, etc.)
├── ui/                      # NiceGUI web interface
│   ├── main.py              # Route definitions
│   └── pages/               # Page components
├── thirdparties/TradingAgents/  # Multi-agent LLM framework
├── config.py                # Global configuration
└── logger.py                # Centralized logging
```

> **Phase 6 packages (source of truth for shared code).** As of the Phase 6
> migration, the *implementation* of most pure/shared code under
> `ba2_trade_platform/core`, `modules/dataproviders`, and `modules/experts` now
> lives in three installable in-repo packages — `ba2_common`
> (`packages/common`), `ba2_providers` (`packages/providers`), and `ba2_experts`
> (`packages/experts`). The matching in-tree modules are now thin **re-export
> shims** (e.g. `core/types.py`, `core/db.py`, `core/position_sizing.py`,
> `core/TradeConditions.py`, the `core/interfaces/*`, the non-AI data providers,
> and the clean experts), so every existing `from ba2_trade_platform...` import
> keeps working unchanged. **When adding or changing shared code, contribute it
> to the relevant package — not to the in-tree shim.** The shims only re-export;
> edits to them are overwritten by the package source. Live-only pieces stay
> real in-tree: `AlpacaAccount`/`IBKRAccount`/`TastyTradeAccount`, the Smart Risk
> Manager stack, `TradingAgents`/`TradingAgentsUI`, the 3 AI providers
> (`AINewsProvider`, `AICompanyOverviewProvider`, `AISocialMediaSentiment`),
> `ModelFactory`/LLM stack, `JobManager`/`WorkerQueue`/`TradeManager`, the
> instance caches, `InstrumentAutoAdder`, the UI, and `MarketAnalysisPDFExport`.
> The packages are consumed via seams wired at startup by
> `core/seam_wiring.py:wire_all_seams()` (called first in
> `main.initialize_system()`): instance resolver, LLM service, DB config,
> `TradeConditions` provider resolver, the instrument-auto-adder hook, and the
> classic-RM ATR indicator provider.

### Database
- SQLite at `~/Documents/ba2/trade/db.sqlite` (under `BA2_HOME`; override with `--db-file`)
- Key models: `AccountDefinition`, `ExpertInstance`, `ExpertRecommendation`, `MarketAnalysis`, `TradingOrder`, `Transaction`, `Ruleset`, `EventAction`

### TradingAgents Framework
Located in `ba2_trade_platform/thirdparties/TradingAgents/`. Multi-agent system with:
- Analyst team (Fundamentals, Sentiment, News, Technical)
- Researcher team (Bull/Bear debates)
- Trader agent and Risk Management

## Critical Patterns

### Avoid Code Duplication
Check `core/utils.py` for existing helpers before writing new code. Key functions:
- `close_transaction_with_logging()` - Transaction closures with P&L
- `log_close_order_activity()` - Activity logging for orders
- `get_expert_instance_from_id()` - Cached expert instance retrieval (live-only factory)
- `get_account_instance_from_id()` - Cached account instance retrieval (live-only factory)

After Phase 6, `core/utils.py` is a **split shim**: the shared pure helpers are
re-exported from `ba2_common.core.utils` (the source of truth — add new shared
helpers there), while the 3 instance-factory functions
(`get_expert_instance_from_id`, `get_account_instance_from_id`,
`get_account_instance_from_transaction`) stay live in-tree because they depend on
the live registries and instance caches. New *shared* code belongs in the
packages (`ba2_common`/`ba2_providers`/`ba2_experts`); new *live-only* code
(broker/Smart-RM/TradingAgents/UI/LLM) belongs in-tree.

### Database Operations
```python
from ba2_trade_platform.core.db import get_instance, add_instance, update_instance
from ba2_trade_platform.core.models import ExpertInstance

expert = get_instance(ExpertInstance, expert_id)
new_id = add_instance(new_expert)
```

### Configuration Access - No Defaults
Always use explicit dict access, never `.get()` with defaults:
```python
# CORRECT
model = config["quick_think_llm"]

# WRONG - hides missing config
model = config.get("quick_think_llm", "gpt-3.5-turbo")
```

### Logging
```python
from ba2_trade_platform.logger import logger

logger.info("General info")
logger.debug("Debug info")

# ONLY use exc_info=True inside except blocks
try:
    risky_operation()
except Exception as e:
    logger.error(f"Failed: {e}", exc_info=True)
```

### Live Data - No Fallbacks
Never use default values for prices, balances, or quantities:
```python
# WRONG
price = recommendation.current_price or 1.0

# CORRECT
if price is None:
    raise ValueError("Price not available")
```

### Prices in decision code
The current price is the ACCOUNT (`account.get_instrument_current_price`; experts: `MarketExpertInterface._decision_price`),
the same call live and in a backtest, where it is the close of the latest intraday bar that has ENDED at the decision.
History (indicators, ATR, factors) is the clamped OHLCV provider, which on an intraday clock returns finished sessions
only (the unclamped read is the explicit `get_ohlcv_data_unsliced`). Never take "the price" from a bar's last row
(`df["Close"].iloc[-1]`): `testplatform/backend/tests/test_no_bar_price_in_decision_code.py` fails on a new site unless
it is allowlisted with a reason. Date-keyed stores (screener scans, regime flags) go through
`metric_store.visible_scan_date` / `knowability.scan_cutoff_date`.

### Confidence Values
Always stored as 1-100 scale (not 0-1):
```python
confidence = 78.1  # Means 78.1%
print(f"{confidence:.1f}%")  # "78.1%"
```

### Data Provider format_type
Providers with `format_type` parameter must support three formats:
- `"markdown"` (default): Returns markdown string for LLM consumption
- `"dict"`: Returns JSON-serializable Python dict (NO markdown)
- `"both"`: Returns dict with `"text"` and `"data"` keys

### AI-Friendly API Design
Prefer explicit function names over string parameters for AI agents:
```python
# CORRECT
def open_buy_position(self, symbol, quantity, ...): ...
def open_sell_position(self, symbol, quantity, ...): ...

# WRONG - AI can confuse "LONG"/"BUY", "SHORT"/"SELL"
def open_position(self, symbol, direction: str, ...): ...
```

## Settings System

Both accounts and experts use the ExtendableSettingsInterface pattern:
```python
class MyExpert(MarketExpertInterface):
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "api_key": {"type": "str", "required": True, "description": "API Key"},
            "threshold": {"type": "float", "required": True, "default": 0.5}
        }

    def __init__(self, id: int):
        super().__init__(id)
        # Access via self.settings["api_key"]
```

## Web Interface

- Runs on port 8080 by default (NiceGUI)
- Main routes defined in `ba2_trade_platform/ui/main.py`
- Access at http://localhost:8080
- Settings configuration at http://localhost:8080/settings

## Environment Variables

The live app never loads a `.env` file.

- **API keys** (OpenAI, Anthropic, Finnhub, FMP, FRED, Alpha Vantage, Alpaca, ...) are entered on the
  Settings page and stored in the app's DB (`AppSetting` table). TradingAgents copies the OpenAI,
  Finnhub and FRED keys into `os.environ` at runtime.
- **Path overrides** (optional environment variables):
  - `BA2_HOME`: data root, default `~/Documents/ba2` (`packages/common/ba2_common/config.py`).
  - `DB_FILE`, `LOG_FOLDER`, `CACHE_FOLDER`: override one path each (`ba2_trade_platform/config.py`).
    The `--db-file`, `--log-folder` and `--cache-folder` flags of `main.py` override them in turn.
- `PRICE_CACHE_TIME` is a constant in `ba2_trade_platform/config.py` (60 seconds), not an
  environment variable.

The test platform is different: `ba2-test` loads `testplatform/backend/.env` and the repo-root
`.env` when they exist.

## Versioning

There are **FOUR** kinds of static version string, all of the form `YYYY.MM.NNNNN`:

- `ba2_trade_platform/version.py` -> `APP_VERSION` (the trade app; shown in the UI sidebar,
  bottom-left)
- `testplatform/version.py` -> `TEST_APP_VERSION` (the test platform, zero-padded so the two
  sequences can never produce the same string)
- `packages/<dir>/<pkg>/version.py` -> `PACKAGE_VERSION`, one per shared package (`ba2_common`,
  `ba2_providers`, `ba2_experts`); `__version__` and the pyproject `version` are the same string
- `testplatform/required_package_versions.py` -> `REQUIRED_PACKAGE_VERSIONS`, the minimum
  package versions the test platform requires of its workers

**Before every `git push`, increment the build number (NNNNN) by 1** in each file that matches what
you changed:

| What you changed | Bump |
|---|---|
| `ba2_trade_platform/` only | `ba2_trade_platform/version.py` (`APP_VERSION`) |
| `testplatform/` | `testplatform/version.py` (`TEST_APP_VERSION`). EXEMPT: edits to `required_package_versions.py` and `ga_neutral_package_paths.py` alone need no TEST bump |
| anything shipped under `packages/<dir>/<pkg>/` | that package's `PACKAGE_VERSION` (`packages/<dir>/<pkg>/version.py`, plus the same string as `version` in `packages/<dir>/pyproject.toml`) -- ALWAYS |
| ... and the change CAN affect GA / backtest results | also set that package's entry in `testplatform/required_package_versions.py` EQUAL to the new `PACKAGE_VERSION`. No `TEST_APP_VERSION` bump is needed: the raised minimum itself makes older workers sync |
| ... and the change CANNOT (broker-only code, etc.) | leave the minimum alone; add a narrow glob for the path to `testplatform/ga_neutral_package_paths.py` in a reviewed change (CI: the `ga-neutral-reviewed` PR label, or `--allow-neutral-change` by hand); the allowlist is judged from the BASE, so a path added in the same diff does not exempt itself |
| more than one of the above | each matching file |

Why workers compare versions: distributed GA trials must run the IDENTICAL code as the master or a
trial's fitness depends on where it ran. A worker re-syncs (`git pull`, reinstall, restart) when
(a) its `TEST_APP_VERSION` differs from the master's, or (b) a package version it reports is BELOW
the master's `REQUIRED_PACKAGE_VERSIONS` entry (a worker that reports none is "unknown": synced once,
logged loudly). A package version that merely differs from the master's while the worker is at/above
the minimum does NOT force a re-sync -- that is the point of the scheme, and it means such a worker
may run OLDER package code than the master by design. The master logs a `WARNING ... DRIFT` line
(once per job, per worker) listing every package that differs, so drift is observable, never silent.
Raising a minimum is therefore the explicit act "this change can affect GA results".

`tools/check_package_versions.py` (a unit test, and the `package-version-guard` CI job) enforces the bump rules against
`git diff <base>...HEAD`: a shipped `packages/` change without a `PACKAGE_VERSION` bump fails; one that
is not matched by `GA_NEUTRAL_GLOBS` and does not raise the minimum (or bump `TEST_APP_VERSION`) fails.
Run it before pushing: `python tools/check_package_versions.py [--base origin/dev] [--include-worktree]`.
Full rationale: `docs/plans/2026-10-03-package-versioning-design.md`. Commit AND push the bumps --
`unsyncable_reason` WARNS (it does not block the run) when a version file is uncommitted or the
branch is unpushed; a worker's `git pull` could not reach it.

Boundary checklist (shipping a package-versioning or minimum-raising change):

1. Every master that shares the workers moves to the new commit TOGETHER, between jobs: the main
   clone, the remote150 isolated-worktree lanes, remote227 if shared. Pull and RESTART long-lived
   `ba2-test serve` masters (a pulled but un-restarted master still advertises the old payload).
2. Push before the next job and confirm `unsyncable_reason` is silent (it warns on uncommitted
   version files or an unpushed branch).
3. Check EVERY worker's `GET /version`: `package_versions` and `required_package_versions` present
   and what you expect (an old worker without them is synced once, loudly).
4. Roll back by reverting FORWARD (a revert commit with a higher TEST_APP_VERSION), never by
   `git reset`: a worker newer than the master is EXCLUDED, not downgraded.
5. Never push a minimum raise to dev while any master is mid-job: workers below it re-sync.

