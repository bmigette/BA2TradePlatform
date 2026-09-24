# ba2trade-providers (`ba2_providers`)

Shared market-data, fundamentals, screener, and options providers for the BA2
stack. Provides OHLCV providers (FMP, Alpaca, Polygon, EODHD, AlphaVantage,
yfinance), the as-of provider cache, the FMP-history disk cache, the screener
metric store, and the offline options cache. Consumed by both apps of the BA2TradePlatform
monorepo: the live trade app and the test platform (`testplatform/`).

## Install / first run

The repo-root install script (`install.ps1` / `install.sh`) installs the
`common -> providers -> experts` chain from `packages/` into both app venvs
(`~/ba2-venvs/{trade,test}`). To install by hand into an existing venv, from the repo root, in
this order (with `uv pip`, add `--no-sources` so the `[tool.uv.sources]` git pins are ignored):

```bash
pip install -e packages/common
pip install -e packages/providers
```

API keys (FMP, Finnhub, ...) are read with `ba2_common.config.get_app_setting` from the
`AppSetting` table of the database the host app configured at startup
(`ba2_common.core.db.configure_db`); each app has its own database and its own keys.

## Data & cache layout

Nothing is cached inside the repo. Provider caches live under the shared
**`BA2_HOME`** root (env-overridable, default `~/Documents/ba2`):

```
BA2_HOME  (default ~/Documents/ba2)
└── common/
    └── cache/              # provider cache shared by both apps                (CACHE_FOLDER)
        ├── <provider>/     #   OHLCV parquet, as_of cache, fmp_history, ...
        ├── screener/       #   metric_store/ (parquet) + screener_history.sqlite
        └── options/        #   options_history.sqlite                          (OPTIONS_CACHE_DB)
```

Defined in `ba2_common/config.py`. `BA2_HOME` relocates the whole tree;
`CACHE_FOLDER` still wins when set explicitly (backward-compatible).

A migration script for the old layout (`~/Documents/ba2_trade_platform` and
in-repo caches) lives in the test platform. From the repo root, with the test venv's Python:

```bash
python testplatform/scripts/migrate_cache_layout.py            # dry run
python testplatform/scripts/migrate_cache_layout.py --apply    # move the files
```

Restart any running instances after migrating.
