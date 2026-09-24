# Test platform tests

The test platform's tests live in three places. Run them with the test venv's Python
(`~/ba2-venvs/test`, see the [Quick Start](../QUICK_START.md)); on Windows the interpreter is
`~/ba2-venvs/test/Scripts/python.exe`, on Linux/macOS `~/ba2-venvs/test/bin/python`.

| Location | What | Runner |
|---|---|---|
| `testplatform/backend/tests/` | the main backend suite: API, GA, fitness, launcher, workers, migrations, DL module; `backtest/` holds the backtest engine and the live/backtest parity tests, `replay/` the live-capture replay tests | pytest, from `testplatform/backend` |
| `testplatform/tests/` (this folder) | screener tests: the `as_of` seam, live-screener equivalence, survivorship-bias removal | pytest, from the repo root |
| `testplatform/frontend/` | React UI tests | vitest (`npm test`) |

## Backend suite

```bash
cd testplatform/backend
python -m pytest tests                    # everything
python -m pytest tests -m "not slow"      # skip tests marked slow
python -m pytest tests/backtest           # backtest engine + shared decision path
```

`backend/pytest.ini` puts `packages/common`, `packages/providers`, `packages/experts` and
`testplatform/` on the path, so the tests exercise the in-repo packages (this matters in a git
worktree, where the venv's editable installs point at the main checkout). `tests/conftest.py`
points `DATABASE_URL` at a throwaway SQLite file so tests never write to the real test DB; set
`BA2_TEST_KEEP_DB=1` to opt out.

CI (`.github/workflows/parity-and-coverage.yml`) runs the live/backtest parity tests and
`tests/backtest` on every push and pull request to `dev` and `main`.

## This folder

```bash
# from the repo root: the root pytest.ini puts packages/* on the path
python -m pytest testplatform/tests
```

These tests monkeypatch every FMP call, except the network-gated golden check in
`test_screener_live_equivalence.py`, which runs only when `FMP_API_KEY` is set.

## Frontend

```bash
cd testplatform/frontend
npm test
```

## Ad-hoc scripts

`testplatform/backend/tests_scripts/` holds debugging and benchmark scripts that are run
directly (`python tests_scripts/<name>.py`), not collected as a suite. When one becomes a
durable regression test, port it into `backend/tests/`.

The shared packages have their own suites under `packages/*/tests`; run them in a separate
invocation.
