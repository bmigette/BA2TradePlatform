# BA2MLTestPlatform Backend - Claude Code Instructions

## Python Environment

**IMPORTANT**: Always use Python from the virtual environment located in the backend folder:

```bash
./venv/bin/python <script>
# or
./venv/bin/pip install <package>
```

Do NOT use system Python or `python` directly. Always use `./venv/bin/python`.

## Running Tests

```bash
./venv/bin/python scripts/test_dataset_generation.py
```

## Running the API Server

```bash
./venv/bin/python -m uvicorn app.main:app --reload
```

## Key Directories

- `app/` - FastAPI application and services
- `dataproviders/` - Data provider implementations (yfinance, FMP, FRED, etc.)
- `scripts/` - Utility scripts for testing and data processing
- `datasets/` - Generated datasets and cache

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
