# BA2TradeProviders (`ba2_providers`)

Shared market-data, fundamentals, screener, and options providers for the BA2
stack. Provides OHLCV providers (FMP, Alpaca, Polygon, EODHD, AlphaVantage,
yfinance), the as-of provider cache, the FMP-history disk cache, the screener
metric store, and the offline options cache. Consumed by BA2TestPlatform and
BA2TradePlatform.

## Install / first run

Installed (editable) into each consuming venv alongside `ba2_common`:

```bash
# from the consuming repo's venv (BA2TestPlatform/backend/venv or ~/ba2-venvs/test)
pip install -e ../BA2TradeCommon
pip install -e ../BA2TradeProviders
```

API keys (FMP, Finnhub, ...) are read from the shared app-settings DB
(`ba2_common.config.DB_FILE`, default `~/Documents/ba2/common/db.sqlite`) or the
corresponding env vars (e.g. `FMP_API_KEY`).

## Data & cache layout

Nothing is cached inside the code repos. Provider caches live under the shared
**`BA2_HOME`** root (env-overridable, default `~/Documents/ba2`):

```
BA2_HOME  (default ~/Documents/ba2)
├── common/
│   ├── cache/              # raw provider cache: OHLCV parquet, as_of cache, fmp_history   (CACHE_FOLDER)
│   ├── db.sqlite           # shared app-settings / API-keys DB                              (DB_FILE)
│   └── options/            # options-history cache                                          (OPTIONS_CACHE_DB)
└── trade/
    └── screener/           # metric_store/ (parquet) + screener_history.sqlite
```

Defined in `ba2_common/config.py`. `BA2_HOME` relocates the whole tree;
`CACHE_FOLDER` / `DB_FILE` still win when set explicitly (backward-compatible).

A migration script for the old layout (`~/Documents/ba2_trade_platform` and
in-repo caches) lives in BA2TestPlatform:

```bash
BA2TestPlatform/backend/venv/bin/python BA2TestPlatform/scripts/migrate_cache_layout.py [--apply]
```

Restart any running instances after migrating.
