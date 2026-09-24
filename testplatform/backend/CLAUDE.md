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

## Versioning — bump `testplatform/version.py`, NOT the trade app's

Any change under `testplatform/` **or `packages/`** must increment `TEST_APP_VERSION` (NNNNN + 1)
in `testplatform/version.py` before the push. Only changes confined to `ba2_trade_platform/` bump
`ba2_trade_platform/version.py`.

This is not cosmetic: `worker_client.ensure_synced` decides whether a distributed GA worker
self-updates by comparing `TEST_APP_VERSION` alone (deliberately not the git commit, so ordinary
pushes don't churn every worker mid-run). Ship a `packages/` fix without bumping it and the
workers keep running the old code while reporting "synced". Commit **and push** the bump —
`unsyncable_reason` refuses the run otherwise, because a worker's `git pull` could never
converge on an uncommitted or unpushed version.

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
