# BA2TradeExperts (`ba2_experts`)

Shared "expert" signal modules for the BA2 stack (e.g. FMPRating,
FMPEarningsDrift, FMPInsiderClusterBuy and friends). Experts consume the shared
providers (`ba2_providers`) and the shared config/DB (`ba2_common`); they are
used by both the backtester (BA2TestPlatform) and the live trader
(BA2TradePlatform).

## Install / first run

Installed (editable) into each consuming venv alongside `ba2_common` and
`ba2_providers`:

```bash
# from the consuming repo's venv (BA2TestPlatform/backend/venv or ~/ba2-venvs/test)
pip install -e ../BA2TradeCommon
pip install -e ../BA2TradeProviders
pip install -e ../BA2TradeExperts
```

API keys are read from the shared app-settings DB
(`ba2_common.config.DB_FILE`, default `~/Documents/ba2/common/db.sqlite`) or env.

## Data & cache layout

Nothing is cached inside the code repos. Experts that cache history (e.g. the
FMP-history disk cache used by the disk-cached experts) write under the shared
**`BA2_HOME`** root (env-overridable, default `~/Documents/ba2`):

```
BA2_HOME  (default ~/Documents/ba2)
└── common/
    ├── cache/              # raw provider cache incl. fmp_history (used by disk-cached experts)  (CACHE_FOLDER)
    └── db.sqlite           # shared app-settings / API-keys DB                                    (DB_FILE)
```

Defined in `ba2_common/config.py`. `BA2_HOME` relocates the whole tree;
`CACHE_FOLDER` / `DB_FILE` still win when set explicitly (backward-compatible).

A migration script for the old layout lives in BA2TestPlatform:

```bash
BA2TestPlatform/backend/venv/bin/python BA2TestPlatform/scripts/migrate_cache_layout.py [--apply]
```

Restart any running instances after migrating.
