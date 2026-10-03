# BA2 Test Platform (`ba2-test`) Backend - Claude Code Instructions

This is the backend of the BA2 Test Platform: the backtesting and genetic-optimization
platform for the BA2 Trade Platform's experts, which also carries a deep-learning forecasting
module (datasets, model training, model library). See `../README.md` for the overview.

## Python Environment

**IMPORTANT**: Always use Python from the test venv built by the repo-root install script
(`install.ps1 -TestOnly` / `install.sh --test-only`), located at `~/ba2-venvs/test`:

```bash
~/ba2-venvs/test/bin/python <script>            # Windows: ~/ba2-venvs/test/Scripts/python.exe
# or
~/ba2-venvs/test/bin/python -m pip install <package>
```

Do NOT use system Python or `python` directly. Always use the test venv's Python.

## Running Tests

```bash
~/ba2-venvs/test/bin/python -m pytest tests                  # from testplatform/backend
~/ba2-venvs/test/bin/python tests_scripts/test_dataset_generation.py   # ad-hoc script
```

See `../tests/README.md` for the full test layout.

## Running the API Server

```bash
ba2-test serve --mode back --reload
# or, from testplatform/backend
~/ba2-venvs/test/bin/python -m uvicorn app.main:app --reload
```

## Versioning -- `testplatform/version.py` for the test platform, per-package versions for `packages/`

Changes under `testplatform/` increment `TEST_APP_VERSION` (NNNNN + 1) in `testplatform/version.py`
before the push. Changes under `packages/` increment that package's `PACKAGE_VERSION`; they raise
`testplatform/required_package_versions.py` ONLY when the change can affect GA results (otherwise the
path goes in `testplatform/ga_neutral_package_paths.py`). Changes confined to `ba2_trade_platform/`
bump `ba2_trade_platform/version.py`.

This is not cosmetic: `worker_client.ensure_synced` re-syncs a distributed GA worker when its
`TEST_APP_VERSION` differs from the master's OR a package it reports is below the declared minimum
(deliberately not the git commit, so ordinary pushes don't churn every worker mid-run). A worker
at/above the minimums may run older package code than the master by design; the master WARNs
(`DRIFT`) once per job. Ship a GA-relevant `packages/` fix without raising the minimum and workers
keep running the old code while reporting "synced" -- `tools/check_package_versions.py` fails that.
Commit **and push** the bumps -- `unsyncable_reason` refuses the run otherwise.

## Key Directories

- `app/` - FastAPI application and services
- `scripts/` - Utility scripts (DB migrations, backtest runners, data processing)
- `tests/` - pytest suite; `tests_scripts/` - ad-hoc scripts, not collected
- Data providers come from the shared `ba2_providers` package (`packages/providers`), not from this folder
- Generated datasets, trained models and caches live under `BA2_HOME/test/` (see `app/paths.py`), not in the repo

## CRITICAL: No Default Values in Job Configuration

**NEVER use default values for job configuration parameters!**

Prefer early failure over random/unexpected settings. All required job configuration values should be explicitly provided by the frontend and validated:

- All `genetic_config` parameters (populationSize, generations, crossoverProb, mutationProb, earlyStoppingGenerations, elitismPercent, trainingEpochs)
- All `metrics_config` parameters (optimizeMetric, classificationMetric, lossFunction)
- All `parameter_ranges` values (layersMin/Max, layerSizeMin/Max, learningRateMin/Max, dropoutMin/Max, seqLen for classification)
- Core job settings (job_type, selected_models, train_test_split, prediction_horizon, prediction_modes)
- Target configuration (must have explicit type and config values)

**Pattern to follow:**
```python
# GOOD - fail early
value = config.get('parameterName')
if value is None:
    return {'status': 'failed', 'error': 'config.parameterName is required'}

# BAD - hidden defaults cause confusion
value = config.get('parameterName', 20)  # DO NOT DO THIS
```

## Non-Configurable Parameters

The following are NOT configurable via job settings:
- **activationFunction**: Fixed per model architecture, not exposed to users
