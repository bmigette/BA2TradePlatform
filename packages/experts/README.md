# ba2trade-experts (`ba2_experts`)

Shared "expert" signal modules for the BA2 stack (e.g. FMPRating,
FMPEarningsDrift, FMPInsiderClusterBuy and friends). Experts consume the shared
providers (`ba2_providers`) and the shared config/DB (`ba2_common`); they are
used by both the backtester (`testplatform/`) and the live trade app of the
BA2TradePlatform monorepo.

## Install / first run

The repo-root install script (`install.ps1` / `install.sh`) installs the
`common -> providers -> experts` chain from `packages/` into both app venvs
(`~/ba2-venvs/{trade,test}`). To install by hand into an existing venv, from the repo root, in
this order (with `uv pip`, add `--no-sources` so the `[tool.uv.sources]` git pins are ignored):

```bash
pip install -e packages/common
pip install -e packages/providers
pip install -e packages/experts
```

API keys (FMP, Finnhub, ...) are read with `ba2_common.config.get_app_setting` from the
`AppSetting` table of the database the host app configured at startup
(`ba2_common.core.db.configure_db`); each app has its own database and its own keys.

## Data & cache layout

Nothing is cached inside the repo. Experts that cache history (e.g. the
FMP-history disk cache used by the disk-cached experts) write under the shared
**`BA2_HOME`** root (env-overridable, default `~/Documents/ba2`):

```
BA2_HOME  (default ~/Documents/ba2)
└── common/
    └── cache/              # provider cache incl. fmp_history (used by disk-cached experts)  (CACHE_FOLDER)
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
