# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before making any changes.

## Project Overview

**BA2 Trade Platform** is a Python-based algorithmic trading monorepo. It contains two applications and three shared libraries:

- **Live trading app** (`ba2_trade_platform/`, package `ba2trade-app`): AI-driven market analysis, multi-agent trading strategies (LLM-based), and a plugin architecture for broker accounts and market experts. Built with SQLModel ORM (SQLite), NiceGUI web interface, and a vendored copy of the TradingAgents multi-agent LLM framework.
- **Test/backtest app** (`testplatform/`, package `ba2test-app`): "BA2ML" — dataset builder, deep-learning model trainer (genetic-optimization driven), and strategy backtester. FastAPI backend + React/TypeScript frontend.
- **Shared libraries** (`packages/`): `ba2_common` (interfaces, types, models, ruleset engine, risk/sizing), `ba2_providers` (market data providers), `ba2_experts` (the "clean" experts). Both apps consume these.

The project is in **alpha**; APIs and DB schema change without notice. All work happens on the `dev` branch. The 4 former sibling repos (BA2TradeCommon/Providers/Experts, BA2TestPlatform) are frozen archives — do not reference or push to them; the packages now live in-tree under `packages/` (see `MIGRATION.md`).

## Repository Layout

```
ba2_trade_platform/          # Live trade app (package: ba2trade-app, console script `ba2-trade`)
├── core/                    # Interfaces, models, TradeManager, JobManager, WorkerQueue, LLM stack
│   ├── interfaces/          # Abstract base classes (AccountInterface, MarketExpertInterface, ...)
│   ├── models.py            # SQLModel database models
│   ├── types.py             # Enums (OrderStatus, OrderDirection, RiskLevel, ...)
│   ├── db.py                # DB helpers (get_instance, add_instance, update_instance) — re-export shim
│   ├── utils.py             # Shared utilities (split shim; see "Phase 6 packages" below)
│   ├── seam_wiring.py       # wire_all_seams() — connects packages to live implementations at startup
│   ├── TradeManager.py      # Order processing and recommendation handling
│   ├── JobManager.py        # Background job scheduling (APScheduler)
│   └── WorkerQueue.py       # Task queue for parallel processing
├── modules/
│   ├── accounts/            # Broker integrations (AlpacaAccount, IBKRAccount, TastyTradeAccount)
│   ├── experts/             # Expert implementations (TradingAgents, FMPRating, FactorRanker, ...)
│   └── dataproviders/       # Market data providers (news, indicators, OHLCV, screener, ...)
├── ui/                      # NiceGUI web interface (main.py = routes, pages/, components/)
├── thirdparties/TradingAgents/  # Vendored multi-agent LLM framework
├── config.py                # Global configuration (paths, ports, price cache)
├── logger.py                # Centralized logging
└── version.py               # APP_VERSION (see Versioning below)

testplatform/                # Test/backtest app (package: ba2test-app, console script `ba2-test`)
├── backend/                 # FastAPI + uvicorn; app/{api,services,models,schemas,tasks}
│   ├── requirements.txt     # Heavy ML deps (torch, darts, tsai, chronos, deap, TA-Lib, ...)
│   ├── tests/               # pytest suite (incl. tests/backtest/ parity suite)
│   └── db_migrate/          # Its own alembic migrations
├── frontend/                # React 19 + TypeScript + Vite 7 + Tailwind 4 (vitest, eslint)
├── ba2cli.py                # CLI wrapper around the backend HTTP API
└── start.sh / start.bat     # Dev launchers (backend|frontend|all)

packages/                    # Shared libraries (Phase 6 extraction — source of truth)
├── common/                  # ba2trade-common  -> ba2_common
├── providers/               # ba2trade-providers -> ba2_providers
└── experts/                 # ba2trade-experts  -> ba2_experts (extra [ui] pulls nicegui)

tests/                       # pytest suite for the live app (ONLY thing pytest collects; see pytest.ini)
test_files/                  # Ad-hoc probe/investigation scripts — NOT collected by pytest
tools/, test_tools/          # Utility/maintenance scripts
alembic/ + alembic.ini + migrate.py  # Live-app DB migrations
docs/                        # Design docs, plans, memos (large; docs/plans/ has dated plan files)
reports/                     # Audit/review reports
main.py                      # Live app entry point
install.sh / install.ps1     # Builds both venvs (~/ba2-venvs/{trade,test})
Dockerfile / docker-compose.yml      # Container deployment of the live app
```

### Phase 6 packages (critical architectural fact)

The *implementation* of most shared code under `ba2_trade_platform/core`, `modules/dataproviders`, and `modules/experts` lives in the three installable packages under `packages/`. The matching in-tree modules are thin **re-export shims**, so existing `from ba2_trade_platform...` imports keep working.

- **When adding or changing shared code, edit the package (`packages/common|providers|experts`), not the in-tree shim.** Shims only re-export; edits to them are overwritten by the package source.
- Live-only code stays in-tree: broker accounts (Alpaca/IBKR/TastyTrade), the Smart Risk Manager stack, `TradingAgents`/`TradingAgentsUI`, the 3 AI providers, `ModelFactory`/LLM stack, `JobManager`/`WorkerQueue`/`TradeManager`, instance caches, `InstrumentAutoAdder`, the UI, `MarketAnalysisPDFExport`.
- Packages are wired to live implementations at startup by `core/seam_wiring.py:wire_all_seams()` (called first in `main.initialize_system()`): instance resolver, LLM service, DB config, `TradeConditions` provider resolver, instrument-auto-adder hook, classic-RM ATR provider.

## Technology Stack

- **Language**: Python ≥ 3.11 (install.sh prefers Python 3.12 — the test backend's `pandas-ta` needs it).
- **Live app**: NiceGUI ≥ 3.0 (web UI), SQLModel/SQLite, APScheduler, Alembic, langchain stack (OpenAI/Anthropic/Google/xAI/DeepSeek/AWS), Alpaca/IBKR/TastyTrade broker APIs, Plotly, ReportLab.
- **Test app**: FastAPI + uvicorn, SQLAlchemy/Alembic, PyTorch/darts/tsai/chronos (DL forecasting), deap (genetic optimization), TA-Lib/pandas-ta; React 19 + TypeScript + Vite + Tailwind 4 frontend.
- **Key pins** (`requirements.txt`): `pandas<3` (pandas 3 untested on live app), `reportlab<5`, NiceGUI ≥ 3.0.
- **PyTorch**: transitive dep on Windows must be the **CPU-only** build (`pip install torch --index-url https://download.pytorch.org/whl/cpu`, known-good `torch==2.6.0+cpu`). Do NOT blindly upgrade torch — newer versions cause `OSError: [WinError 1114]` DLL load failures on Windows.
- **Package management**: `uv` preferred (`uv pip install -r requirements.txt`); pip works too.

## Setup, Build and Run Commands

### Initial setup

```bash
# Builds ~/ba2-venvs/{trade,test} with the common -> providers -> experts chain
# editable from packages/, plus both apps. Windows: run under Git Bash/WSL.
./install.sh --editable            # add --ui for the experts[ui] extra; --trade-only / --test-only

# Or manually into an existing .venv:
uv pip install -r requirements.txt
```

### Running the live app

```bash
# Windows
.venv\Scripts\python.exe main.py
# Linux/macOS
.venv/bin/python main.py

# Options (same for the `ba2-trade` console script)
python main.py --port 9090 --db-file ./dev.db --cache-folder ./cache --log-folder ./logs
```

Web UI: http://localhost:8080 (settings at `/settings`). Startup order matters: `wire_all_seams()` runs before any DB/provider/expert/LLM imports.

### Running the test app

```bash
cd testplatform
./start.sh all          # or: backend | frontend   (start.bat on Windows)
# Backend alone: cd backend && uvicorn app.main:app --reload   (http://localhost:8000/docs)
# Frontend alone: cd frontend && npm run dev
# CLI against the backend API: python ba2cli.py --help  (or the `ba2-test` console script)
```

### Database migrations (live app, Alembic)

```bash
python migrate.py create "Description of changes"   # autogenerate from core/models.py changes
python migrate.py upgrade                           # apply (target: head)
python migrate.py downgrade -1                      # rollback one revision
python migrate.py current                           # show current revision
```

The test backend has its own migration runner under `testplatform/backend/db_migrate/`. Default live DB location is under the shared data dir (`BA2_HOME`, default `~/Documents/ba2`, `trade/` bucket); per-instance runs relocate logs next to the DB file.

### Versioning

`ba2_trade_platform/version.py` holds `APP_VERSION = "YYYY.MM.NNNNN"`. **Increment the build number (NNNNN) by 1 before every `git push`** (update year/month when they change). The version is shown in the UI sidebar and logged at startup.

## Testing

### Live app (`tests/`)

```bash
.venv\Scripts\python.exe -m pytest                 # all tests (pytest.ini: testpaths = tests)
.venv\Scripts\python.exe -m pytest -x              # stop on first failure
.venv\Scripts\python.exe -m pytest -k "test_name"  # filter
.venv\Scripts\python.exe -m pytest -m "not slow"   # skip slow-marked tests
```

- `tests/` is the pytest suite; `test_files/` holds ad-hoc probes that pytest does NOT run. When a probe becomes a durable regression test, port it into `tests/` as `test_*.py`.
- `tests/conftest.py` provides: session-scoped **in-memory SQLite** engine (dropped/recreated before every test), `MockAccount` (concrete `AccountInterface` + `OptionsAccountInterface` with canned broker responses), `MockExpert`, and factory helpers (`tests/factories.py`). It also wires the Phase 6 package seams at import time — the LLM-service seam is deliberately NOT wired (LLM-touching tests must mock it).
- Use the provided fixtures (`mock_account`, `mock_expert_instance`, `db_session`, ...) rather than hitting real brokers/APIs.

### Test app

```bash
cd testplatform/backend && python -m pytest            # backend suite
cd testplatform/backend && python -m pytest tests/backtest -q   # engine + parity suite
cd testplatform/frontend && npm test                   # vitest
cd testplatform/frontend && npm run lint               # eslint
```

### CI (GitHub Actions)

`.github/workflows/parity-and-coverage.yml` (on push/PR to `dev`/`main`): installs `packages/*` from this monorepo (NOT the stale sibling repos — a guard step asserts `ba2_common` resolves to `packages/common`), then runs the **live↔backtest parity gate** (`testplatform/backend/tests/backtest/test_parity_golden.py`, blocking) and the full backtest suite (blocking), plus a non-blocking branch-coverage artifact for the shared decision path.

## Code Style and Critical Conventions

These are enforced project idioms from `CLAUDE.md` — follow them exactly:

- **No config defaults.** Explicit dict access, never `.get()` with fallbacks: `config["quick_think_llm"]`, not `config.get("quick_think_llm", "gpt-3.5-turbo")` — defaults hide missing config.
- **No live-data fallbacks.** Never default prices/balances/quantities (`price or 1.0` is a bug). If a price is unavailable, raise.
- **Confidence values are 1–100** (e.g. `78.1` means 78.1%), not 0–1.
- **Logging**: `from ba2_trade_platform.logger import logger`. Use `exc_info=True` ONLY inside `except` blocks.
- **DB access**: use the helpers — `get_instance(Model, id)`, `add_instance(obj)`, `update_instance(obj)` from `ba2_trade_platform.core.db` — not raw sessions in app code. Check `core/utils.py` for existing helpers (e.g. `close_transaction_with_logging`, `get_expert_instance_from_id`) before writing new ones.
- **Settings system**: accounts and experts implement `get_settings_definitions()` (from `ExtendableSettingsInterface`) and read values via `self.settings["key"]`.
- **Data provider `format_type`**: providers must support `"markdown"` (default, LLM-ready string), `"dict"` (JSON-serializable, no markdown), and `"both"` (`{"text", "data"}`).
- **AI-friendly APIs**: explicit method names over string discriminator params — `open_buy_position(...)` / `open_sell_position(...)`, not `open_position(direction: str)`.
- **Shared vs live-only code**: new shared code goes into `packages/` (source of truth); new live-only code (brokers, Smart RM, TradingAgents, UI, LLM) goes in-tree. Never edit re-export shims.
- Match the surrounding file's conventions; keep changes minimal and scoped.

## Security Considerations

- **This software trades real money.** It is alpha/experimental; always test against paper-trading accounts first. Never bypass risk limits, position-sizing rules, or approval flows.
- **Secrets**: API keys (OpenAI, Finnhub, Alpha Vantage, Alpaca, FMP, ...) come from `.env` (see `.env.example`) or, mostly, the web UI Settings page (stored in the `AppSetting` DB table). Never hardcode keys, never log them, never commit `.env` or `creds.env`.
- **No silent fallbacks for money-related values** (see conventions) — fail loud instead of trading on fabricated data.
- The Docker image runs as a non-root user (`trader`); keep it that way.
- The NiceGUI `STORAGE_SECRET` in `config.py` is a placeholder default for session storage — override it for any real deployment.
- Broker integrations submit real orders; any change to `TradeManager`, order submission, or position sizing paths needs tests in `tests/` (see the extensive option/order test files for patterns).

## Deployment

- **Docker** (live app): multi-stage `Dockerfile` (builder uses `uv`, final image python:3.11-slim, non-root user). `docker-compose.yml` exposes port 8000 and persists three volumes (db, cache, logs) under `/opt/ba2_trade_platform/`.
  ```bash
  docker compose up --build
  ```
- **Bare metal / dev**: `install.sh` (or `install.ps1` on Windows) builds `~/ba2-venvs/{trade,test}` and can copy/migrate old DBs. On-disk data (DBs, caches, logs) lives outside the repo under `~/Documents/ba2/...`.
- Remember: bump `APP_VERSION` in `ba2_trade_platform/version.py` before every push.

## Reference Documentation

- `README.md` — full feature list, API key setup, screenshots
- `CLAUDE.md` — agent guidance this file is based on (kept in sync)
- `EXPERTS.md` — every trading expert, its settings and configuration; `docs/FACTORRANKER_EXPERT.md` for FactorRanker
- `MIGRATION.md` — monorepo migration notes; `MIGRATIONS.md` — Alembic usage
- `testplatform/README.md`, `testplatform/docs/` — test/backtest app docs
- `docs/plans/` — dated design/implementation plans; `reports/` — audits and reviews
