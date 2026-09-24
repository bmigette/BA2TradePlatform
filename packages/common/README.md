# ba2trade-common (`ba2_common`)

Shared core library for the BA2 trading/backtesting stack: configuration, the
shared SQLModel/SQLAlchemy DB layer (app-settings + API keys), provider cache
substrate, logging, and common types, plus the interfaces, models, rule engine and position
sizing shared by the live trade app and the test platform. Lives in `packages/common` of the
BA2TradePlatform monorepo and installed into both app venvs (editable with the install
script's `-Editable` / `--editable` flag), so changes here reach both apps (notably the cache/data layout defined in `ba2_common/config.py`).

## Data & cache layout

Nothing is cached inside the repo. All cache/data lives under a single
root, **`BA2_HOME`** (env-overridable, default `~/Documents/ba2`), split into
three buckets:

```
BA2_HOME  (default ~/Documents/ba2)
├── common/
│   └── cache/              # provider cache shared by both apps                             (CACHE_FOLDER)
│       ├── <provider>/     #   OHLCV parquet, fmp_history, as_of cache, ...
│       ├── screener/       #   metric_store/ (parquet) + screener_history.sqlite (SCREENER_STORE_DIR, SCREENER_HISTORY_DB)
│       └── options/        #   options_history.sqlite                                       (OPTIONS_CACHE_DB)
├── test/                   # test platform data: dl_forecasting.db, datasets/, trained_models/,
│                           #   cache/jobs/, cache/news/, news_exports/, logs/
└── trade/                  # live trade platform: db.sqlite + logs/
```

These are defined as plain module-level paths in `ba2_common/config.py`:
`BA2_HOME`, `COMMON_DIR`, `TEST_DIR`, `TRADE_DIR`, `CACHE_FOLDER`, `DB_FILE`,
`SCREENER_STORE_DIR`, `SCREENER_HISTORY_DB`, `OPTIONS_CACHE_DB`. Each app configures its
own database at startup (`trade/db.sqlite` for the live app, `test/dl_forecasting.db` for the
test platform); `DB_FILE` here is only a last-resort fallback.

### Overrides (backward-compatible)

- `BA2_HOME` relocates the whole tree.
- Per-path env vars still win when set: `DB_FILE`, `CACHE_FOLDER` (and, in
  the test platform, `BA2_DATASETS_DIR` / `BA2_MODELS_DIR` / `BA2_JOBS_CACHE_DIR` /
  `BA2_NEWS_CACHE_DIR` / `BA2_NEWS_EXPORTS_DIR`).

## Install / first run

The repo-root install script (`install.ps1` / `install.sh`) installs the
`common -> providers -> experts` chain from `packages/` into both app venvs
(`~/ba2-venvs/{trade,test}`). To install by hand into an existing venv, from the repo root, in
this order (with `uv pip`, add `--no-sources` so the `[tool.uv.sources]` git pins are ignored):

```bash
pip install -e packages/common
python -c "import ba2_common.config as c; print(c.BA2_HOME, c.CACHE_FOLDER)"
```

The data buckets are created on first use.

## Migrating from the old layout

A migration script for the old layout (`~/Documents/ba2_trade_platform` and
in-repo caches) lives in the test platform. From the repo root, with the test venv's Python:

```bash
python testplatform/scripts/migrate_cache_layout.py            # dry run
python testplatform/scripts/migrate_cache_layout.py --apply    # move the files
```

Restart any running instances after migrating.
