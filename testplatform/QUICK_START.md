# Quick Start — `ba2-test`

The short path from a fresh clone to a running backtest platform. The
[test platform README](README.md) has the full reference.

## 1. Prerequisites

- Python 3.12 (the backend's `pandas-ta` needs it; the install scripts use it by default)
- Node.js / npm for the React UI
- Git

## 2. Install

From the **monorepo root** (not `testplatform/`), build the test venv:

```bash
./install.sh --test-only --editable          # Linux/macOS
.\install.ps1 -TestOnly -Editable            # Windows (PowerShell)
```

This creates `~/ba2-venvs/test`, installs the in-repo `packages/common -> providers -> experts`
chain and `backend/requirements.txt`, registers the `ba2-test` command, runs `npm install` in
`frontend/`, and migrates the test database if one exists. Useful flags: `--upgrade` / `-Upgrade`
to re-resolve dependencies, `--no-db` / `-NoDb` to skip the database step, `--python` /
`-Python` to pick the interpreter.

`ba2-test` lives in the venv, so activate it first (or call it by its full path):

```bash
source ~/ba2-venvs/test/bin/activate         # Linux/macOS
~\ba2-venvs\test\Scripts\Activate.ps1        # Windows (PowerShell)
```

## 3. Run

```bash
ba2-test serve                   # API on :8000 + Vite UI on :5173
ba2-test serve --mode back       # API only (add --reload while developing)
ba2-test serve --mode front      # UI only
```

- UI: http://localhost:5173 — API: http://localhost:8000 — OpenAPI docs: http://localhost:8000/docs
- On startup the backend creates its tables and runs `backend/scripts/migrate_db.py`.
- Ports: `--port` (API, default 8000) and `--frontend-port` (UI, default 5173). If the API is not
  on `http://localhost:8000/api`, set `VITE_API_BASE` in `frontend/.env.local`.

## 4. Configure

Open **Settings** in the UI and enter provider API keys (FMP, Alpaca, Finnhub, ...), or copy them
from the trade platform's database with the copy action there. The test platform keeps its own
database (`BA2_HOME/test/dl_forecasting.db`, default `BA2_HOME` = `~/Documents/ba2`) and shares
the provider cache with the trade platform.

## 5. First backtest

In the UI: **Backtesting -> New Backtest**, pick an expert, a universe, dates and a strategy, and
run it. From the command line:

```bash
ba2-test backtest -h             # arguments are passed through to run_daily_backtest
ba2-test optimize -h             # one GA optimization job
ba2-test -h                      # every command
```

## Next

- [README.md](README.md) — pages, backtesting and optimization, distributed workers, CLI, API, data layout
- [docs/](docs/README.md) — grid and fitness guide, robustness suite, backtest engine scope
- [tests/README.md](tests/README.md) — running the tests
