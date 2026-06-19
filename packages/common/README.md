# BA2TradeCommon

Shared core library for the BA2 trading/backtesting stack: configuration, the
shared SQLModel/SQLAlchemy DB layer (app-settings + API keys), provider cache
substrate, logging, and common types. Installed **editable** into the other
repos' virtualenvs, so changes here propagate everywhere (notably the cache/data
layout defined in `ba2_common/config.py`).

## Data & cache layout

Nothing is cached inside the code repos. All cache/data lives under a single
root, **`BA2_HOME`** (env-overridable, default `~/Documents/ba2`), split into
three buckets:

```
BA2_HOME  (default ~/Documents/ba2)
├── common/                 # shared across test + live
│   ├── cache/              # raw provider cache: OHLCV parquet, as_of cache, fmp_history   (CACHE_FOLDER)
│   ├── db.sqlite           # shared app-settings / API-keys DB (FMP, Finnhub, ...)         (DB_FILE)
│   └── options/            # options-history cache                                          (OPTIONS_CACHE_DB)
├── test/                   # BA2TestPlatform artifacts (were inside the repo — the bug)
│   ├── datasets/           # generated dataset CSVs
│   ├── trained_models/     # saved model artifacts
│   ├── cache/jobs/         # per-job cache
│   ├── cache/news/         # news content files
│   └── news_exports/       # exported news JSON
└── trade/                  # screener caches + live trade instance DBs
    └── screener/           # metric_store/ (parquet) + screener_history.sqlite
```

These are defined as plain module-level paths in `ba2_common/config.py`:
`BA2_HOME`, `COMMON_DIR`, `TEST_DIR`, `TRADE_DIR`, `CACHE_FOLDER`, `DB_FILE`,
`SCREENER_STORE_DIR`, `SCREENER_HISTORY_DB`, `OPTIONS_CACHE_DB`.

### Overrides (backward-compatible)

- `BA2_HOME` relocates the whole tree.
- Per-path env vars still win when set: `DB_FILE`, `CACHE_FOLDER` (and, in
  BA2TestPlatform, `BA2_DATASETS_DIR` / `BA2_MODELS_DIR` / `BA2_JOBS_CACHE_DIR` /
  `BA2_NEWS_CACHE_DIR` / `BA2_NEWS_EXPORTS_DIR`).

## Install / first run

`ba2_common` is installed **editable** into each consuming venv:

```bash
# from the consuming repo's venv, e.g. BA2TestPlatform/backend/venv or ~/ba2-venvs/test
pip install -e ../BA2TradeCommon        # path relative to the venv's repo
python -c "import ba2_common.config as c; print(c.BA2_HOME, c.CACHE_FOLDER, c.DB_FILE)"
```

The data buckets are created on first use (e.g. on BA2TestPlatform startup).

## Migrating from the old layout

The old layout cached under `~/Documents/ba2_trade_platform` and inside the
repos. A migration script lives in BA2TestPlatform:

```bash
# dry-run (prints planned moves + sizes)
BA2TestPlatform/backend/venv/bin/python BA2TestPlatform/scripts/migrate_cache_layout.py
# perform the moves
BA2TestPlatform/backend/venv/bin/python BA2TestPlatform/scripts/migrate_cache_layout.py --apply
```

Restart any running instances after migrating so they pick up the new locations.
