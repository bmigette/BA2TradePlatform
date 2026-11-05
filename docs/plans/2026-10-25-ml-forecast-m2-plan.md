# ML forecast milestone 2: TimesFM 3.0 adapter and store builder

Design: [2026-10-25-ml-forecast-expert-design.md](2026-10-25-ml-forecast-expert-design.md) §1.2, §1.3, §3, §5.
Stacked on [M1](2026-10-25-ml-forecast-m1-plan.md).

## Decisions
- **Import isolation.** `ba2_common/__init__` and `core/__init__` are empty, and `ml_forecast` imports only stdlib,
  numpy, pandas, pyarrow and `ba2_common.{version,core.types,config}` (the last lazily). So the ml venv gets
  `pip install --no-deps -e packages/common`; nothing else is needed. A subprocess test guards this: the modules
  loaded by importing ml_forecast must equal the baseline of `import numpy, pandas, pyarrow` plus `ba2_common.*`.
- **Return definition.** One 20-step model call per context. For horizon h in (5, 10, 20), decile k is
  `quantiles[h-1, k] / last_close - 1` (fractional return of the forecast price path at step h vs the close of S).
  Quantile crossing or a price <= 0 is refused loudly by `ForecastRow`; the builder counts and logs such rows, it
  never repairs them.
- **Adapter** takes an injectable `predict_fn(list[ndarray], horizon) -> list[ndarray(horizon, 9)]`; torch is imported
  only by `load_timesfm3_predict_fn`. Covariate set `()` only; `("volume",)` raises NotImplementedError.
- **Cards.** `timesfm3_card(context_length)` for 512 and 2048; distinct model keys through the card.
- **Builder.** Reads `<cache>/AlpacaHistoryOHLCVProvider/<SYM>_1d.parquet` (`Date`, `Close`). The session calendar
  is the union of symbol dates (weekly = last union date per ISO week). A forecast at S uses the last
  `context_length` bars with `Date <= S` (asserted); fewer bars or no bar on S is a counted skip. Batches cross
  symbols for one session. Rows are buffered and flushed every N sessions through `write_rows`; resume skips
  (symbol, session) pairs already in the parquet.

## Tasks (TDD, one commit each)
1. Import-isolation test + editable install in the ml venv.
2. Adapter + model card (stub-model tests: maths, point in time, short history, covariates refusal).
3. `build_store.py` (resume, skip counts, store round trip through `read_row`).
4. Real smoke run, determinism check, throughput and ETA; version check.
