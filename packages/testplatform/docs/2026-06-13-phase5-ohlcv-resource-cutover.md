# Phase 5 — OHLCV Re-source Cutover Note (`OHLCV_SOURCE`)

> Status: **gated, default `legacy`** (flag OFF). Nothing changes for existing
> training until the flag is explicitly flipped. This note records the flag, the
> verified equivalence, the cache topology, the one-time cache-invalidation
> procedure, and the rollback path.

## 1. What changed

Phase 5 added a single, reversible **seam** so the ML dataset builder can source
OHLCV through `ba2_providers` (the same provider layer the as_of experts use)
instead of instantiating the legacy `YFinance`/`FMP` providers directly:

- `app/api/datasets.py::get_ohlcv_provider(provider_name)` now consults the
  `OHLCV_SOURCE` env flag. **Default `legacy`** keeps the historical direct
  `YFinanceDataProvider` / `FMPOHLCVProvider` path. `OHLCV_SOURCE=ba2_providers`
  routes the fetch through `dataproviders/ba2providers_adapter.py::BA2ProvidersOHLCVAdapter`.
- `BA2ProvidersOHLCVAdapter.get_data(symbol, start_date, end_date, interval, use_cache)`
  returns the **same** `List[MarketDataPoint]` contract the builder already
  consumes, so `_build_dataset_in_background` / `_regenerate_dataset_in_background`
  and every other call site (all routed through the single `get_ohlcv_provider`
  factory) are **untouched**. `use_cache`, `interval`, `calculate_warmup_period`
  + warmup-row filtering, `PredictionTargetService` targets, and
  `DataPreparationService` normalization are all unchanged.
- `as_of` maps to `end_date` (inclusive close): the reproducible / leak-free view
  known at the dataset's end.

The `backtesting.py` `MLStrategy` engine (`app/services/backtest_handler.py:33`)
is **unchanged** — it remains the "ML expert" engine. Nothing in the training /
backtest path was modified beyond the source seam.

A parallel, secondary seam for sentiment / fundamentals / macro features
(`FEATURES_SOURCE`, `app/services/features_source.py`) was wired in shape only and
**also stays default `legacy`** — see §6.

## 2. The flag

| Flag | Values | Default | Effect |
|------|--------|---------|--------|
| `OHLCV_SOURCE` | `legacy` \| `ba2_providers` | `legacy` | `ba2_providers` routes OHLCV through `ba2_providers.get_provider('ohlcv', <name>).get_ohlcv_data(...)`; `legacy` uses the direct `YFinance`/`FMP` providers. |
| `FEATURES_SOURCE` | `legacy` \| `ba2_providers` | `legacy` | Secondary seam for sentiment/fundamentals/macro; deferred — keep `legacy`. |

Set per-process, e.g.:

```bash
OHLCV_SOURCE=ba2_providers venv/bin/python -m uvicorn app.main:app
```

The adapter is constructed eagerly so that if `ba2_providers` is missing, or a
provider constructor raises (e.g. FMP requiring `FMP_API_KEY`), `get_ohlcv_provider`
logs the error and **falls back to legacy** before any build is attempted.

## 3. Decision: cutover stays gated (default `legacy`)

The Phase-5 plan offered, on a green byte-equality gate, to flip the runtime
default to `OHLCV_SOURCE=ba2_providers` for new builds. **We deliberately keep the
default `legacy`** (flag OFF) for this phase:

- The two consumers (experts via as_of slices, ML via the materialized matrix)
  can already share one cache *when the flag is set*, but flipping the default
  would silently change the cache topology for all existing training (see §5) and
  would change which provider library is exercised on every build. Keeping the
  default `legacy` means existing training is bit-for-bit unaffected until an
  operator opts in explicitly.
- Rollback is then trivial and operator-controlled (unset the flag), with no code
  change required.

To opt a build/run into the shared cache, set `OHLCV_SOURCE=ba2_providers` for that
process. To revert: unset it (or set `legacy`).

## 4. Equivalence verification (GATE item 1)

Verified by `tests/test_resource_ml_datasets.py`:

- `test_byte_equal_csv` — both seam paths are fed the **same** canonical OHLCV at
  the `get_data()` boundary (the only thing Phase 5 changed); the real builder runs
  unchanged under each flag and the two output CSVs are compared **byte-for-byte
  (SHA-256)**. They are identical.
- `test_byte_equal_csv_with_indicators` — same, with technical indicators applied.
- `test_equivalence_delta_is_only_volume_rendering` — bounds the equivalence:

### Documented equivalence delta (justified, not ignored)

The legacy path returns `dataproviders.interfaces.types.MarketDataPoint` and the
`ba2_providers` path returns `dataproviders.base.MarketDataPoint`. These are
**different classes** but both expose the exact six attributes the builder reads
(`.timestamp/.open/.high/.low/.close/.volume`). The **only** rendering difference
between them is the `Volume` column dtype: `interfaces.types` stores the value
verbatim (`int -> "1028"`) while `dataproviders.base` casts `volume=float(...)`
(`"1028.0"`).

**This delta does NOT appear in production**, because the legacy production path
(`MarketDataProviderInterface._dataframe_to_datapoints`, `.py:327`) ALSO
float-casts volume — so both paths emit float volume given a float-volume
DataFrame, and the CSVs are byte-identical. The dedicated test asserts the delta
is exactly `Volume`-rendering and nothing in `Date/Open/High/Low/Close`, so the
equivalence claim is **verified, not assumed**.

## 5. Cache topology (important for cutover planning)

`ba2_providers`' `get_ohlcv_data` uses its **own legacy-style per-class CSV cache**
under `ba2_common.config.CACHE_FOLDER`
(`~/Documents/ba2_trade_platform/cache/<ClassName>/<SYMBOL>_<interval>.csv`). It
does **not** (yet) use the native parquet / `provider_cache` SQLite as_of store —
`read_timeseries`/`write_timeseries` have **no callers** in `ba2_providers` (grep
confirmed). So re-sourcing through `ba2_providers` gives a **shared legacy-style CSV
cache** (one cache for experts + ML), not a parquet as_of store, unless additional
wiring lands later (out of Phase-5 scope).

Two distinct cache roots therefore exist:

| Root | Used by | Path |
|------|---------|------|
| Backend `CACHE_FOLDER` | legacy `YFinance`/`FMP` providers (default OHLCV path) | `<backend>/cache` (env-overridable) |
| `ba2_common.config.CACHE_FOLDER` | `ba2_providers` providers (when `OHLCV_SOURCE=ba2_providers`) | `~/Documents/ba2_trade_platform/cache` (expanduser, NOT env-driven) |

The cache-management UI's `asof` type points at `ba2_common.config.CACHE_FOLDER`
(and its `datasets/cache` spill), per the Task-1 scanner.

## 6. Sentiment / fundamentals / macro (deferred)

`FEATURES_SOURCE` (`app/services/features_source.py`) wires the parallel
`ba2_providers` route for the three feature services, but stays **default
`legacy`**. Per-block byte-equivalence for these multi-field feature matrices is
materially harder than for OHLCV and was not part of the Phase-5 gate. When
flagged on, the fundamentals service in particular *probes* `ba2_providers` (to
surface misconfiguration) and then uses the legacy orchestrator for the actual
fetch this phase, because the legacy orchestrator surface differs from a single
`ba2_providers fundamentals_details` provider. Do **not** flip `FEATURES_SOURCE`
to `ba2_providers` until per-block equivalence is documented in a follow-up.

## 7. One-time cache invalidation / rebuild

Per the migration-safety contract: a one-time cache invalidation is expected when
moving from the legacy `datasets/cache` to the shared `ba2_providers` cache, so
that subsequent builds populate the shared cache cleanly. Because the default
stays `legacy`, this is an **operator action taken only when opting a deployment
into `OHLCV_SOURCE=ba2_providers`**, not something forced on existing installs.

Use the new cache-management UI / API to purge the legacy OHLCV cache once:

```bash
# With the backend running (default port 8000):
curl -s -X DELETE http://localhost:8000/api/cache/ohlcv | venv/bin/python -m json.tool
```

This clears the OHLCV (`<backend>/cache`) CSV cache (24h-TTL, regenerable). It does
NOT touch dataset CSVs or `trained_models` (those are destructive types, excluded
from generic clears — delete them only by explicit type). After the purge, the next
`OHLCV_SOURCE=ba2_providers` build repopulates the shared
`ba2_common.config.CACHE_FOLDER` cache.

> Note: the OHLCV cache is point-in-time-regenerable (24h TTL), so this is a
> convenience step, not a correctness requirement — a stale legacy CSV simply gets
> ignored once the flag selects the `ba2_providers` provider, which reads/writes its
> own root.

## 8. Rollback

1. Unset `OHLCV_SOURCE` (or set `OHLCV_SOURCE=legacy`) — the next build uses the
   legacy direct provider path. No code change, no data migration.
2. The legacy provider path was never removed; it is the default and the
   automatic fallback when the `ba2_providers` adapter cannot be constructed.

## 9. Phase-5 gate sign-off

Run from `BA2TestPlatform/backend` with `venv/bin/python`:

```bash
venv/bin/python -m pytest tests/test_cache_api.py tests/test_resource_ml_datasets.py -v
```

- **GATE 1 — dataset build via providers reproduces the prior matrix:** green —
  `test_byte_equal_csv` (+ indicators variant) byte-equal; `Volume`-rendering delta
  documented and proven not to apply in production (§4).
- **GATE 2 — cache-UI operations on a seeded cache:** green —
  `tests/test_cache_api.py` (usage report, by-type / by-date deletion, clean-all
  leaves `datasets` + `trained_models` intact).
- **GATE 3 — ML training still runs through the provider path:** green —
  `test_ml_training_smoke_through_provider_path` builds a tiny dataset and runs the
  **unchanged** `backtesting.py MLStrategy` engine end-to-end with synthetic
  predictions (torch-free substitute; `torch`/`tsai`/`darts` are not installed, and
  the `torch` import lives only inside `run_backtest()`, not in `MLStrategy`).
- **GATE 4 — no regression on the live platform:** Phase 5 confined every edit to
  `BA2TestPlatform`. (At the time of this sign-off the `BA2TradePlatform/` working
  tree carries unrelated **Phase 6** migration shims under `ba2_trade_platform/core/`
  — those are NOT Phase-5 changes; Phase 5 added/modified zero files there.)
