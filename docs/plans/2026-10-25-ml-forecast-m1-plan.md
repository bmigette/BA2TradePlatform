# ML forecast milestone 1: contract and store (plan)

Scope: pure code under `packages/common/ba2_common/core/ml_forecast/`. No torch, no expert registration, no GA wiring.
Design: [2026-10-25-ml-forecast-expert-design.md](2026-10-25-ml-forecast-expert-design.md) sections 1.1, 1.3, 2, 5.

Each task is TDD: failing test in `packages/common/tests/test_ml_forecast_<x>.py`, then code, then commit.

1. **model_card.py**: frozen `ModelCard`, strict validation, `to_dict`/`from_dict`, `model_key()` (sha256 of canonical JSON, 16 hex).
2. **forecast_row.py**: `ForecastRow` (symbol, session, per-horizon 9 deciles as fractional returns, optional
   P(TP before SL) grid per direction), `TP_GRID_PCT`/`SL_GRID_PCT`, column naming, validation (monotone, in [0,1]).
3. **store.py**: parquet layout + `meta.json`, writer (upsert by session, refuses a different card under one key),
   `read_row(model_key, symbol, cutoff)` (latest session <= cutoff, `ForecastStoreMiss` otherwise, staleness limit),
   per-process frame cache with `reset_cache()`.
4. **mapping.py**: `MappingGenes`, `MappingResult`, `p_up_from_deciles`, `map_forecast`, `map_held_position`.
5. Version bump, `check_package_versions.py`, full `packages/common` suite vs. baseline.
