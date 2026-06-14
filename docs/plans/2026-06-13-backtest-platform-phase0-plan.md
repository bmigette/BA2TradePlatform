# Backtest Platform — Phase 0 (Package Extraction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the BA2TradePlatform engine into three pip-installable packages — `ba2_common` (interfaces, types, models, DB, ruleset/`TradeConditions`, classic `TradeRiskManagement` + `position_sizing`), `ba2_providers` (data providers), `ba2_experts` (experts) — each installable from git, importable in a clean venv, and free of layering violations, **without touching the running BA2TradePlatform** (its migration onto the packages is Phase 6).

**Architecture:** Strict one-way dependency chain `ba2_common ← ba2_providers ← ba2_experts`. The extraction is a **copy** (not a cut): the new repos become refactored copies; `BA2TradePlatform/ba2_trade_platform/` is left untouched and fully functional. Three architectural **seams** keep `ba2_common` free of provider/LLM/live-runtime coupling: an `InstanceResolver` protocol (replaces `utils.get_*_instance_from_id`), an `LLMServiceInterface` (replaces direct `ModelFactory` imports), and provider/ATR **dependency injection** (replaces `ba2_common → ba2_providers` back-edges). These seams are *defined* in Phase 0 and *wired* by a host app in Phase 1/2/6 — in Phase 0 nothing wires them, so packages import cleanly and the pure decision logic is unit-tested directly.

**Tech Stack:** Python ≥3.11, hatchling build backend, SQLModel/SQLAlchemy, pandas/numpy, pytest, `import-linter` (layering CI gate), `libcst` (import codemod, dev-only). Packages reference each other via `git+ssh://git@github.com/bmigette/<repo>.git`; `install.{sh,ps1}` supports `--editable` for local clones.

---

## Source of truth & repo locations

- Source tree (read-only in Phase 0): `BA2TradePlatform/ba2_trade_platform/` at branch `dev`, commit `72eefee` (already checked out locally). **Re-confirm `git rev-parse --short HEAD` == `72eefee` before starting.**
- Target repos (siblings under `…/dev/BA2/`): `BA2TradeCommon`, `BA2TradeProviders`, `BA2TradeExperts` (all empty scaffolds: README/.gitignore only, branch `main`).
- This plan is derived from `docs/plans/2026-06-13-backtest-platform-design.md` (§1, §6) and `docs/FMP_BACKTEST_FEASIBILITY.md`, plus a full file-by-file recon of the `72eefee` tree.

## Decisions taken (confirm before execution)

These resolve forks the recon surfaced. Override any of them at approval time.

1. **Extract = COPY, leave live untouched (Model A).** Phase 0 populates the three repos and makes them install/import cleanly. `BA2TradePlatform` keeps its in-tree copy and keeps working; switching it to *consume* the packages (deleting the in-tree copy, wiring the injections, the `run_analysis == analyze_as_of(now)` golden test) is **Phase 6**, drafted here as the optional **Task 11**. *Alternative:* move-and-rewire-live now (merges Phase 0+6, bigger blast radius on the live trading platform).
2. **`ba2_experts` is not strictly headless for v1.** Per-expert `ui.py` (FactorRanker, PennyMomentumTrader) travel with their experts; `nicegui` is an **optional extra** (`ba2trade-experts[ui]`) and `ui.py` imports it lazily, so `import ba2_experts` works without nicegui installed.
3. **`models_registry.py` → `ba2_common` as data-only** (it imports only `logger`, no langchain). This lets the LLM seam + the live ModelFactory share `parse_model_selection`/`PROVIDER_CONFIG` without dragging langchain into `ba2_common`.
4. **The whole LLM runtime stays live.** `ModelFactory`, `ChatKimiThinking`, `prompt_caching`, `LLMUsageTracker`, `ModelBillingUsage`, `LLMUsageQueries` stay in `BA2TradePlatform`. `ba2_common` gets a langchain-free `LLMServiceInterface`. The 3 AI providers (`AINewsProvider`, `AICompanyOverviewProvider`, `AISocialMediaSentiment`) and `PennyMomentumTrader`'s 3 mixins import `ModelFactory` at module top → AI providers **stay live & are dropped from the `ba2_providers` registry**; Penny moves to `ba2_experts` with its LLM calls converted to the injected service.
5. **Concrete brokers stay live.** `AlpacaAccount`/`IBKRAccount`/`TastyTradeAccount` + the `modules/accounts` registry stay in `BA2TradePlatform`; only the `AccountInterface`/`OptionsAccountInterface`/`ReadOnlyAccountInterface` *bases* go to `ba2_common`. Experts never import concrete accounts — they resolve via the `InstanceResolver`.
6. **Versioning:** semver `0.1.0` per package (libraries), `requires-python = ">=3.11"`, build backend `hatchling`. (The live platform keeps its CalVer `version.py`.)
7. **DB seam:** `ba2_common.core.db` builds its engine lazily and configurably (default path preserves today's `~/Documents/ba2_trade_platform/db.sqlite`), so a package never hardcodes/binds the live DB at import.

## Amendments (2026-06-13, post-review — these OVERRIDE the body where they conflict)

**A1 — Execution environment (this machine).** PyPI direct TLS fails (`CERTIFICATE_VERIFY_FAILED`); the existing live venv `BA2TradePlatform/venv` (Python 3.12.10) already has every heavy dep (sqlmodel, pandas, numpy, sqlalchemy, pydantic, fmpsdk, requests, …). Therefore:
- Any `pip install` MUST add `--trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org` (verified working).
- Run package **tests** with the live-venv interpreter via `PYTHONPATH` (no install needed): `PYTHONPATH=<common>:<providers>:<experts> /…/BA2TradePlatform/venv/bin/python -m pytest …`. Install dev tools (`import-linter`, `libcst`, `hatchling`, `pytest`) into a dedicated venv (or the live venv) with the trusted-host flags.
- **Leak gates must use `sys.modules`, not "not installed"** (langchain/fmpsdk ARE present in the live venv). The real assertion: importing a package must not PULL a forbidden module. Helper used by the gate tests:
```python
import subprocess, sys, textwrap
def assert_no_leak(import_stmt, forbidden, py):
    code = textwrap.dedent(f"""
        import sys; {import_stmt}
        bad=[m for m in {forbidden!r} if any(k==m or k.startswith(m+'.') for k in sys.modules)]
        print('LEAK:'+','.join(bad) if bad else 'CLEAN')""")
    out = subprocess.run([py,'-c',code], capture_output=True, text=True)
    assert out.stdout.strip()=='CLEAN', f"{import_stmt} pulled {out.stdout.strip()} / {out.stderr}"
```
e.g. `assert_no_leak("import ba2_common", ["ba2_providers","ba2_experts","langchain","langchain_core","fmpsdk","nicegui"], PY)`. Task 7's "clean-room" gate uses this in the live venv instead of a deps-free venv. import-linter (static) remains the second gate.

**A2 — `install.{sh,ps1}` MUST create a venv** (replaces Task 1 Step 9). The scripts create a fresh venv and install the chain into it, with the trusted-host flags:

`BA2TradeCommon/install.sh`:
```bash
#!/usr/bin/env bash
# Create a venv and install the BA2 trade package chain (common -> providers -> experts) into it.
#   ./install.sh [--editable] [--ui] [--venv PATH] [--python PY]
#     (default venv: <dev/BA2>/.venv ; default base interpreter: python3)
set -euo pipefail
EDITABLE=0; UI=0; VENV=""; BASE_PY="${PYTHON:-python3}"
while [ $# -gt 0 ]; do case "$1" in
  --editable|-e) EDITABLE=1 ;; --ui) UI=1 ;;
  --venv) shift; VENV="$1" ;; --python) shift; BASE_PY="$1" ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;; esac; shift; done
OWNER="bmigette"
HERE="$(cd "$(dirname "$0")/.." && pwd)"            # dev/BA2 dir holding the sibling clones
VENV="${VENV:-$HERE/.venv}"
EXTRA=""; [ "$UI" = "1" ] && EXTRA="[ui]"
TRUSTED="--trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org"
echo ">> creating venv at $VENV (base: $BASE_PY)"
"$BASE_PY" -m venv "$VENV"
VPY="$VENV/bin/python"; [ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"
PIP() { "$VPY" -m pip install $TRUSTED "$@"; }
PIP --upgrade pip >/dev/null 2>&1 || true
if [ "$EDITABLE" = "1" ]; then
  echo ">> editable install from $HERE"
  PIP -e "$HERE/BA2TradeCommon"
  PIP -e "$HERE/BA2TradeProviders"
  PIP -e "$HERE/BA2TradeExperts${EXTRA}"
else
  echo ">> git install over SSH"
  PIP "git+ssh://git@github.com/${OWNER}/BA2TradeCommon.git"
  PIP "git+ssh://git@github.com/${OWNER}/BA2TradeProviders.git"
  PIP "git+ssh://git@github.com/${OWNER}/BA2TradeExperts.git${EXTRA}"
fi
echo ">> verifying imports"
"$VPY" -c "import ba2_common, ba2_providers, ba2_experts; print('ok', ba2_common.__version__)"
echo ">> done. activate: source $VENV/bin/activate"
```

`BA2TradeCommon/install.ps1`:
```powershell
param([switch]$Editable, [switch]$Ui, [string]$Venv, [string]$Python)
$ErrorActionPreference = "Stop"
$Owner = "bmigette"
$BasePy = if ($Python) {$Python} elseif ($env:PYTHON) {$env:PYTHON} else {"python"}
$Here = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $Venv) { $Venv = Join-Path $Here ".venv" }
$Extra = if ($Ui) {"[ui]"} else {""}
$Trusted = @("--trusted-host","pypi.org","--trusted-host","files.pythonhosted.org","--trusted-host","pypi.python.org")
Write-Host ">> creating venv at $Venv (base: $BasePy)"
& $BasePy -m venv $Venv
$Vpy = Join-Path $Venv "Scripts/python.exe"; if (-not (Test-Path $Vpy)) { $Vpy = Join-Path $Venv "bin/python" }
function Pip { param([string[]]$a) & $Vpy -m pip install @Trusted @a }
Pip @("--upgrade","pip") | Out-Null
if ($Editable) {
  Write-Host ">> editable install from $Here"
  Pip @("-e", (Join-Path $Here "BA2TradeCommon"))
  Pip @("-e", (Join-Path $Here "BA2TradeProviders"))
  Pip @("-e", ((Join-Path $Here "BA2TradeExperts") + $Extra))
} else {
  Write-Host ">> git install over SSH"
  Pip @("git+ssh://git@github.com/$Owner/BA2TradeCommon.git")
  Pip @("git+ssh://git@github.com/$Owner/BA2TradeProviders.git")
  Pip @(("git+ssh://git@github.com/$Owner/BA2TradeExperts.git") + $Extra)
}
Write-Host ">> verifying imports"
& $Vpy -c "import ba2_common, ba2_providers, ba2_experts; print('ok', ba2_common.__version__)"
```

**A3 — Expert settings import/export lives in `ba2_experts` and is used by BOTH platforms.** Add to Task 9: create `ba2_experts/settings_io.py` by extracting the **expert** portion of `BA2TradePlatform/settings_export_import.py` (`export_experts` @87, `import_experts` @258, plus the shared `_setting_to_dict` @51 and `_upsert_setting` @146 helpers). Expose a clean API — `export_expert_settings(session, expert_ids=None) -> list[dict]` and `import_expert_settings(session, experts_list, dry_run=False) -> dict` — parameterized by a SQLModel `session` and resolving expert classes via `ba2_experts.get_expert_class` (NOT the live registry). Both BA2TradePlatform (Phase 6 wires its UI/CLI to call it) and BA2TestPlatform (to load an exported expert config into a backtest, and to export optimizer-found params back) import it from `ba2_experts.settings_io`. The app-settings/accounts portions of `settings_export_import.py` stay in BA2TradePlatform. Add `tests/test_settings_io.py`: round-trip export→import of a sample expert config against a temp DB equals the original.

**A4 — DB schema changes require migration scripts, using each repo's EXISTING tooling (cross-cutting, all phases).** Any new/changed table or column ships with a migration authored via the repo's own migrator — do NOT introduce a parallel mechanism:
- **BA2TradePlatform** → `python migrate.py create "<message>"` (Alembic autogenerate; `migrate.py:23` create / `:36` upgrade / `:46` downgrade / `alembic.ini` + `alembic/`), then `python migrate.py upgrade`. Used by Phase 6.
- **BA2TestPlatform** → its existing migrator `backend/scripts/migrate_db.py` + the `backend/db_migrate/` migration set (inspect that dir for the established revision pattern and add a new one). Used by Phase 1 (`ProviderCache` table), Phase 2 (`Backtest.model_id` nullable), Phase 4 (RM columns on `Strategy`), Phase 5.
- **`ba2_common` / the provider cache DB**: `ProviderCache` is a `ba2_common` model but the DB is owned by the consuming host, so the table is created/migrated by whichever host owns that DB (the backtest cache DB via BA2TestPlatform's migrator, or `ba2_common.core.db.init_db()` `create_all` for a standalone cache DB). `ba2_common` ships the model + a `create_all`-from-metadata helper; the host owns the migration.
- Each schema-changing task's gate includes: "the migration applies cleanly on a fresh DB **and** upgrades an existing populated DB (no data loss)."

**A5 — Push policy.** Commit and push are allowed for `BA2TradeCommon`, `BA2TradeProviders`, `BA2TradeExperts`, and `BA2TestPlatform` (feature branches). **Never push `BA2TradePlatform` `dev`/`main`** until the whole effort is reviewed; its Phase 6 work stays on a local feature branch.

## Package layout (internal structure)

`ba2_common` **preserves** the `core/` + root `config.py`/`logger.py` structure so intra-package relative imports survive the move unchanged:

```
ba2_common/
  __init__.py            config.py   logger.py
  core/
    __init__.py  types.py  models.py  db.py  option_types.py  option_selector.py
    position_sizing.py  weinstein.py  provider_utils.py  news_enrichment.py
    text_utils.py  date_utils.py  models_registry.py  utils.py            # pure subset only
    TransactionHelper.py  TradeConditions.py  TradeActions.py  TradeActionEvaluator.py
    TradeRiskManagement.py  rules_documentation.py  rules_export_import.py # exporter/importer only
    instance_resolver.py                                                   # NEW seam
    interfaces/
      __init__.py  DataProviderInterface.py  MarketDataProviderInterface.py
      MarketIndicatorsInterface.py  CompanyFundamentalsOverviewInterface.py
      CompanyFundamentalsDetailsInterface.py  CompanyInsiderInterface.py
      MacroEconomicsInterface.py  MarketNewsInterface.py
      SocialMediaDataProviderInterface.py  ScreenerProviderInterface.py
      ReadOnlyAccountInterface.py  AccountInterface.py  OptionsAccountInterface.py
      ExtendableSettingsInterface.py  MarketExpertInterface.py  LiveExpertInterface.py
      SmartRiskExpertInterface.py  LLMServiceInterface.py                  # NEW seam
```

`ba2_providers` and `ba2_experts` **flatten** (drop the `modules/dataproviders` / `modules/experts` prefix):

```
ba2_providers/
  __init__.py            # get_provider() registry, AI providers removed
  fmp_common.py  alpha_vantage_common.py  StockScreener.py
  ohlcv/  news/  indicators/  fundamentals/{overview,details}/  macro/  insider/  socialmedia/  screener/
ba2_experts/
  __init__.py            # get_expert_class() registry, TradingAgents removed
  expert_mixins.py
  FinnHubRating.py  FMPRating.py  FMPSenateTraderCopy.py  FMPSenateTraderWeight.py
  FMPEarningsDrift.py  FMPInsiderClusterBuy.py
  FactorRanker/  PennyMomentumTrader/
```

## Import-rewrite rules (deterministic)

Applied by the libcst codemod in **Task 1** and re-run per package as files land. Two passes:

**Pass A — absolutize:** convert every *relative* import in a moved file to an absolute `ba2_trade_platform.*` import (libcst knows each file's full module name). After this pass there are no relative cross-cutting imports to mis-resolve.

**Pass B — remap roots** (longest-prefix first):

| From (absolute) | To |
|---|---|
| `ba2_trade_platform.core` | `ba2_common.core` |
| `ba2_trade_platform.config` | `ba2_common.config` |
| `ba2_trade_platform.logger` | `ba2_common.logger` |
| `ba2_trade_platform.modules.dataproviders` | `ba2_providers` |
| `ba2_trade_platform.modules.experts` | `ba2_experts` |

`ba2_trade_platform.modules.accounts.*`, `ba2_trade_platform.core.ModelFactory` (and the other live-only modules), `ba2_trade_platform.thirdparties.*`, `ba2_trade_platform.ui.*` have **no mapping** — any surviving reference to them in a package is a layering violation the import-linter gate (Task 7/8/9) must reject; they are removed via the seams.

## The three seams (defined in `ba2_common`, wired by the host later)

- **`InstanceResolver`** (`core/instance_resolver.py`): a `Protocol` with `get_expert_instance(id)` / `get_account_instance(id)` / `get_account_instance_from_transaction(txn)`; a module-level `set_instance_resolver()/get_instance_resolver()` with a default that raises `InstanceResolverNotConfigured`. Replaces `utils.get_expert_instance_from_id` / `get_account_instance_from_id` everywhere in the interfaces.
- **`LLMServiceInterface`** (`core/interfaces/LLMServiceInterface.py`): abstract `create_llm(...)` + `do_llm_call_with_websearch(...)` mirroring `ModelFactory` (returning `Any`, never langchain types); module-level `set_llm_service()/get_llm_service()` default-raises. Replaces direct `ModelFactory` imports in Penny's mixins.
- **Provider/ATR injection:** `position_sizing.get_latest_atr` takes an indicator provider; `TradeConditions` condition classes take an injected `get_provider` resolver. Removes the `ba2_common → ba2_providers` / `fmpsdk` back-edges.

## Acceptance gate for Phase 0

1. In a **fresh venv with only `ba2_common` installed**: `python -c "import ba2_common, ba2_common.core.interfaces, ba2_common.core.TradeConditions, ba2_common.core.TradeRiskManagement, ba2_common.core.utils"` succeeds with **no** `langchain`, `fmpsdk`, `ba2_providers`, or `ba2_experts` installed.
2. Fresh venv `ba2_common+ba2_providers`: `python -c "import ba2_providers"` succeeds with **no** `langchain`/`ModelFactory` importable.
3. Fresh venv `ba2_common+ba2_providers+ba2_experts`: `python -c "import ba2_experts"` succeeds with **no** `langchain` installed.
4. `lint-imports` (import-linter) passes in all three repos.
5. Unit tests pass in all three repos (pure calculators: `weinstein`, `position_sizing`, `evaluate_earnings_drift`, `detect_insider_cluster`, FactorRanker factor math, Penny conditions, pure `utils` helpers, the two seams).
6. `BA2TradePlatform` is byte-for-byte unchanged (`git -C BA2TradePlatform status` clean except this plan doc). Its own test suite still passes.

---

## Task 1: Scaffold the three repos + tooling + install scripts

**Files (per repo, create):** `pyproject.toml`, `<pkg>/__init__.py`, `<pkg>/py.typed`, `.importlinter`, `tests/__init__.py`, `tests/conftest.py`, `tools/codemod_imports.py` (common repo only, shared by copy), and `BA2TradeCommon/install.sh` + `BA2TradeCommon/install.ps1`. `BA2TradeProviders`/`BA2TradeExperts` already have `.gitignore`; add one to `BA2TradeCommon` (copy from providers).

- [ ] **Step 1: Branch each target repo for the extraction**

```bash
cd /Users/bmigette/Documents/dev/BA2
for r in BA2TradeCommon BA2TradeProviders BA2TradeExperts; do
  git -C "$r" checkout -b phase0-extraction
done
git -C BA2TradePlatform rev-parse --short HEAD   # MUST print 72eefee
```

- [ ] **Step 2: Create `ba2_common` package skeleton + pyproject**

Create `BA2TradeCommon/ba2_common/__init__.py`:

```python
"""ba2_common — shared interfaces, types, models, DB, ruleset engine, classic risk/sizing."""
__version__ = "0.1.0"
```

Create `BA2TradeCommon/ba2_common/py.typed` (empty file). Create `BA2TradeCommon/pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ba2trade-common"
version = "0.1.0"
description = "BA2 Trade — shared interfaces, types, models, ruleset engine, classic risk/sizing"
requires-python = ">=3.11"
dependencies = [
    "sqlmodel>=0.0.22",
    "sqlalchemy>=2.0.36",
    "pydantic>=2.10.0",
    "pandas>=2.2.0,<3.0.0",
    "numpy>=2.0.0",
    "python-dateutil>=2.9.0",
    "pytz>=2024.1",
    "tzlocal>=5.0",
    "python-dotenv>=1.0.1",
    "requests>=2.32.0",
    "trafilatura>=1.6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-cov>=5.0", "import-linter>=2.0", "libcst>=1.4"]

[tool.hatch.build.targets.wheel]
packages = ["ba2_common"]
```

- [ ] **Step 3: Create `ba2_providers` skeleton + pyproject**

`BA2TradeProviders/ba2_providers/__init__.py`:

```python
"""ba2_providers — market data providers (OHLCV, fundamentals, indicators, news, macro, insider, screener)."""
__version__ = "0.1.0"
```

`BA2TradeProviders/ba2_providers/py.typed` (empty). `BA2TradeProviders/pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ba2trade-providers"
version = "0.1.0"
description = "BA2 Trade — market data providers"
requires-python = ">=3.11"
dependencies = [
    "ba2trade-common",
    "requests>=2.32.0",
    "aiohttp>=3.11.0",
    "pandas>=2.2.0,<3.0.0",
    "numpy>=2.0.0",
    "beautifulsoup4>=4.12.0",
    "fmpsdk>=20230123",
    "yfinance>=0.2.40",
    "stockstats>=0.6.2",
    "alpaca-py>=0.21.0",
    "tenacity>=8.2.0",
    "curl_cffi>=0.7.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-cov>=5.0", "import-linter>=2.0", "libcst>=1.4"]

[tool.uv.sources]
ba2trade-common = { git = "ssh://git@github.com/bmigette/BA2TradeCommon.git", branch = "main" }

[tool.hatch.build.targets.wheel]
packages = ["ba2_providers"]
```

> Note: `[tool.uv.sources]` lets `uv` resolve the git dep; plain `pip` users get it via `install.{sh,ps1}` (Step 9) which installs `ba2_common` first. Hatchling ignores `[tool.uv.*]`.

- [ ] **Step 4: Create `ba2_experts` skeleton + pyproject**

`BA2TradeExperts/ba2_experts/__init__.py`:

```python
"""ba2_experts — trading expert implementations."""
__version__ = "0.1.0"
```

`BA2TradeExperts/ba2_experts/py.typed` (empty). `BA2TradeExperts/pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "ba2trade-experts"
version = "0.1.0"
description = "BA2 Trade — expert implementations"
requires-python = ">=3.11"
dependencies = [
    "ba2trade-common",
    "ba2trade-providers",
    "pandas>=2.2.0,<3.0.0",
    "numpy>=2.0.0",
    "requests>=2.32.0",
    "pytz>=2024.1",
    "sqlmodel>=0.0.22",
    "fmpsdk>=20230123",
]

[project.optional-dependencies]
ui = ["nicegui>=3.0.0"]
dev = ["pytest>=8.0", "pytest-cov>=5.0", "import-linter>=2.0", "libcst>=1.4"]

[tool.uv.sources]
ba2trade-common = { git = "ssh://git@github.com/bmigette/BA2TradeCommon.git", branch = "main" }
ba2trade-providers = { git = "ssh://git@github.com/bmigette/BA2TradeProviders.git", branch = "main" }

[tool.hatch.build.targets.wheel]
packages = ["ba2_experts"]
```

- [ ] **Step 5: Add the import-linter contracts**

`BA2TradeCommon/.importlinter`:

```ini
[importlinter]
root_packages =
    ba2_common

[importlinter:contract:common-is-self-contained]
name = ba2_common must not import providers, experts, or live-only modules
type = forbidden
source_modules =
    ba2_common
forbidden_modules =
    ba2_providers
    ba2_experts
    ba2_trade_platform
```

`BA2TradeProviders/.importlinter`:

```ini
[importlinter]
root_packages =
    ba2_providers

[importlinter:contract:providers-layering]
name = ba2_providers may use ba2_common but not experts or live modules
type = forbidden
source_modules =
    ba2_providers
forbidden_modules =
    ba2_experts
    ba2_trade_platform
```

`BA2TradeExperts/.importlinter`:

```ini
[importlinter]
root_packages =
    ba2_experts

[importlinter:contract:experts-layering]
name = ba2_experts may use ba2_common and ba2_providers but not the live platform
type = forbidden
source_modules =
    ba2_experts
forbidden_modules =
    ba2_trade_platform
```

> import-linter only sees *statically analyzable* imports; the import-smoke tests (Tasks 7–9) are the backstop for lazy/in-function imports.

- [ ] **Step 6: Write the import codemod tool**

Create `BA2TradeCommon/tools/codemod_imports.py` (copy the same file into each repo's `tools/` when needed):

```python
"""Rewrite imports for the package extraction.

Pass A: absolutize relative imports (using each file's full module name).
Pass B: remap ba2_trade_platform.* roots to the new package roots.

Usage:
    python tools/codemod_imports.py <root_dir> <old_pkg_for_relative_base>
Example (run from inside the repo, files already copied in):
    python tools/codemod_imports.py ba2_common      ba2_trade_platform
    python tools/codemod_imports.py ba2_providers    ba2_trade_platform.modules.dataproviders
    python tools/codemod_imports.py ba2_experts      ba2_trade_platform.modules.experts
The second arg is the ORIGINAL fully-qualified package that the copied files
came from, used to reconstruct each file's original module name so relative
imports resolve to the correct absolute target before remapping.
"""
import sys, pathlib
import libcst as cst

# Pass B mapping, longest prefix first.
REMAP = [
    ("ba2_trade_platform.core", "ba2_common.core"),
    ("ba2_trade_platform.config", "ba2_common.config"),
    ("ba2_trade_platform.logger", "ba2_common.logger"),
    ("ba2_trade_platform.modules.dataproviders", "ba2_providers"),
    ("ba2_trade_platform.modules.experts", "ba2_experts"),
]

def remap(mod: str) -> str:
    for old, new in REMAP:
        if mod == old or mod.startswith(old + "."):
            return new + mod[len(old):]
    return mod

class Rewriter(cst.CSTTransformer):
    def __init__(self, current_module: str):
        # current_module = original absolute module name of this file
        self.pkg = current_module.rsplit(".", 1)[0] if "." in current_module else current_module

    def _abs_from_relative(self, dots: int, tail: str) -> str:
        base = self.pkg.split(".")
        # `from . import x` -> dots=1 keeps current package; each extra dot pops one.
        pops = dots - 1
        if pops > len(base):
            return tail  # cannot resolve; leave as-is (will fail import gate -> visible)
        prefix = base[: len(base) - pops] if pops else base
        parts = prefix + ([tail] if tail else [])
        return ".".join(p for p in parts if p)

    def leave_ImportFrom(self, node, updated):
        dots = len(updated.relative)
        if dots == 0:
            # absolute import: just remap the root
            if updated.module is None:
                return updated
            absmod = cst_module_to_str(updated.module)
            new = remap(absmod)
            return updated.with_changes(module=str_to_cst_attr(new)) if new != absmod else updated
        # relative -> absolutize -> remap
        tail = cst_module_to_str(updated.module) if updated.module else ""
        absmod = self._abs_from_relative(dots, tail)
        absmod = remap(absmod)
        return updated.with_changes(relative=[], module=str_to_cst_attr(absmod))

def cst_module_to_str(mod) -> str:
    if isinstance(mod, cst.Name):
        return mod.value
    if isinstance(mod, cst.Attribute):
        return cst_module_to_str(mod.value) + "." + mod.attr.value
    raise TypeError(type(mod))

def str_to_cst_attr(dotted: str):
    parts = dotted.split(".")
    node = cst.Name(parts[0])
    for p in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(p))
    return node

def main():
    root = pathlib.Path(sys.argv[1])           # e.g. ba2_common
    orig_base = sys.argv[2]                     # e.g. ba2_trade_platform  (or ...modules.dataproviders)
    pkg_root_name = root.name                   # ba2_common / ba2_providers / ba2_experts
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).with_suffix("")
        rel_mod = ".".join(rel.parts)
        rel_mod = rel_mod[: -len(".__init__")] if rel_mod.endswith(".__init__") else rel_mod
        # original module name = orig_base + (path relative to the NEW root, which mirrors the old subtree)
        original = orig_base + ("." + rel_mod if rel_mod and rel_mod != "__init__" else "")
        src = path.read_text(encoding="utf-8")
        tree = cst.parse_module(src)
        new = tree.visit(Rewriter(original))
        if new.code != src:
            path.write_text(new.code, encoding="utf-8")
            print(f"rewrote {path}")

if __name__ == "__main__":
    main()
```

> The codemod is a *starting* rewrite; the import-smoke + import-linter gates in later tasks catch anything it misses, and the seam edits (resolver/LLM/provider injection) are done by hand in the relevant tasks.

- [ ] **Step 7: Add per-repo `tests/conftest.py`** (isolates DB to a temp file so no test touches the live sqlite)

`BA2TradeCommon/tests/conftest.py`:

```python
import os, tempfile, pathlib
import pytest

@pytest.fixture(scope="session", autouse=True)
def _isolated_db():
    """Point ba2_common's DB seam at a throwaway sqlite for the whole test session."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "test.sqlite"
    from ba2_common.core import db
    db.configure_db(str(tmp))   # defined in Task 3
    db.init_db()
    yield
```

`BA2TradeProviders/tests/conftest.py` and `BA2TradeExperts/tests/conftest.py`: identical body (they depend on `ba2_common`). Add empty `tests/__init__.py` to each repo.

- [ ] **Step 8: Verify empty packages build & import**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeCommon && python -m venv /tmp/v_common && \
  /tmp/v_common/bin/pip install -q -e ".[dev]" && \
  /tmp/v_common/bin/python -c "import ba2_common; print(ba2_common.__version__)"
```
Expected: prints `0.1.0`, no errors.

- [ ] **Step 9: Write `install.sh` and `install.ps1`**

`BA2TradeCommon/install.sh`:

```bash
#!/usr/bin/env bash
# Install the BA2 trade package chain (common -> providers -> experts).
#   ./install.sh                 # install from GitHub over SSH (release/main)
#   ./install.sh --editable      # install local sibling clones with -e (dev)
#   ./install.sh --editable --ui # also install the experts [ui] extra (nicegui)
set -euo pipefail

EDITABLE=0; UI=0
for a in "$@"; do
  case "$a" in
    --editable|-e) EDITABLE=1 ;;
    --ui) UI=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

OWNER="bmigette"
PY="${PYTHON:-python}"
EXTRA=""; [ "$UI" = "1" ] && EXTRA="[ui]"

if [ "$EDITABLE" = "1" ]; then
  HERE="$(cd "$(dirname "$0")/.." && pwd)"   # the dev/BA2 dir holding the sibling clones
  echo ">> editable install from $HERE"
  "$PY" -m pip install -e "$HERE/BA2TradeCommon"
  "$PY" -m pip install -e "$HERE/BA2TradeProviders"
  "$PY" -m pip install -e "$HERE/BA2TradeExperts${EXTRA}"
else
  echo ">> git install over SSH"
  "$PY" -m pip install "git+ssh://git@github.com/${OWNER}/BA2TradeCommon.git"
  "$PY" -m pip install "git+ssh://git@github.com/${OWNER}/BA2TradeProviders.git"
  "$PY" -m pip install "git+ssh://git@github.com/${OWNER}/BA2TradeExperts.git${EXTRA}"
fi
echo ">> verifying imports"
"$PY" -c "import ba2_common, ba2_providers, ba2_experts; print('ok', ba2_common.__version__)"
```

`BA2TradeCommon/install.ps1`:

```powershell
# Install the BA2 trade package chain (common -> providers -> experts).
#   ./install.ps1                  # from GitHub over SSH
#   ./install.ps1 -Editable        # local sibling clones with -e
#   ./install.ps1 -Editable -Ui    # also install experts [ui] extra
param([switch]$Editable, [switch]$Ui)
$ErrorActionPreference = "Stop"
$Owner = "bmigette"
$Py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Extra = if ($Ui) { "[ui]" } else { "" }

if ($Editable) {
    $Here = Resolve-Path (Join-Path $PSScriptRoot "..")
    Write-Host ">> editable install from $Here"
    & $Py -m pip install -e (Join-Path $Here "BA2TradeCommon")
    & $Py -m pip install -e (Join-Path $Here "BA2TradeProviders")
    & $Py -m pip install -e ((Join-Path $Here "BA2TradeExperts") + $Extra)
} else {
    Write-Host ">> git install over SSH"
    & $Py -m pip install "git+ssh://git@github.com/$Owner/BA2TradeCommon.git"
    & $Py -m pip install "git+ssh://git@github.com/$Owner/BA2TradeProviders.git"
    & $Py -m pip install ("git+ssh://git@github.com/$Owner/BA2TradeExperts.git" + $Extra)
}
Write-Host ">> verifying imports"
& $Py -c "import ba2_common, ba2_providers, ba2_experts; print('ok', ba2_common.__version__)"
```

```bash
chmod +x BA2TradeCommon/install.sh
```

- [ ] **Step 10: Commit scaffolds**

```bash
for r in BA2TradeCommon BA2TradeProviders BA2TradeExperts; do
  git -C "/Users/bmigette/Documents/dev/BA2/$r" add -A
  git -C "/Users/bmigette/Documents/dev/BA2/$r" commit -m "chore: Phase 0 scaffold (pyproject, package skeleton, import-linter, tooling)"
done
```

---

## Task 2: `ba2_common` foundation leaves (zero/low-dep modules)

**Files — copy from `BA2TradePlatform/ba2_trade_platform/` then codemod:**
- Create `ba2_common/logger.py` ← `ba2_trade_platform/logger.py`
- Create `ba2_common/core/{types,option_types,date_utils,text_utils,provider_utils,weinstein,models_registry}.py` and `ba2_common/core/__init__.py` (empty, like source)
- Test: `BA2TradeCommon/tests/test_weinstein.py`, `tests/test_types_smoke.py`

- [ ] **Step 1: Copy the leaf modules + logger**

```bash
cd /Users/bmigette/Documents/dev/BA2
S=BA2TradePlatform/ba2_trade_platform
D=BA2TradeCommon/ba2_common
cp "$S/logger.py" "$D/logger.py"
cp "$S/core/__init__.py" "$D/core/__init__.py"
for f in types option_types date_utils text_utils provider_utils weinstein models_registry; do
  cp "$S/core/$f.py" "$D/core/$f.py"
done
```

- [ ] **Step 2: Codemod imports for what's copied so far**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeCommon
cp -n ../BA2TradeCommon/tools/codemod_imports.py tools/ 2>/dev/null || true
python tools/codemod_imports.py ba2_common ba2_trade_platform
```
Expected: rewrites any `ba2_trade_platform.*` / relative imports in the copied files (e.g. `provider_utils` `from ..logger` stays valid; `models_registry` `from ..logger import logger` → still `ba2_common.logger`). `weinstein.py`/`types.py`/`option_types.py` are dependency-light and likely unchanged.

- [ ] **Step 3: Write failing test for `weinstein`**

`BA2TradeCommon/tests/test_weinstein.py`:

```python
import pytest

def test_weinstein_module_imports_without_providers():
    import ba2_common.core.weinstein as w
    assert w is not None

def test_weinstein_stage2_uptrend():
    """A clean rising series above a rising 30-period SMA classifies as Stage 2."""
    from ba2_common.core import weinstein
    closes = [10 + i * 0.5 for i in range(60)]   # steady uptrend
    stage = weinstein.classify_stage(closes)     # confirm real fn name in source before running
    assert "2" in str(stage) or "stage2" in str(stage).lower()
```

- [ ] **Step 4: Reconcile test with the real API, then run**

Open `ba2_common/core/weinstein.py`, confirm the public function name/return type, and adjust `test_weinstein_stage2_uptrend` to assert the actual contract (the source is pure SMA/stage math, `typing`-only). Then:

```bash
/tmp/v_common/bin/pip install -q -e . && /tmp/v_common/bin/python -m pytest tests/test_weinstein.py -v
```
Expected: PASS.

- [ ] **Step 5: Write + run the types import-smoke test**

`BA2TradeCommon/tests/test_types_smoke.py`:

```python
def test_core_leaf_modules_import():
    import ba2_common.core.types
    import ba2_common.core.option_types
    import ba2_common.core.date_utils
    import ba2_common.core.text_utils
    import ba2_common.core.provider_utils
    import ba2_common.core.models_registry  # data-only; must NOT pull langchain
```
```bash
/tmp/v_common/bin/python -m pytest tests/test_types_smoke.py -v
```
Expected: PASS, and (crucially) no `langchain` import error from `models_registry`.

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -m "feat(common): foundation leaves (types, utils, weinstein, models_registry, logger)"
```

---

## Task 3: `ba2_common` config + DB seam + models

**Files:**
- Create `ba2_common/config.py` ← `ba2_trade_platform/config.py` (+ DB seam)
- Create `ba2_common/core/db.py` ← `ba2_trade_platform/core/db.py` (+ lazy/configurable engine)
- Create `ba2_common/core/models.py` ← `ba2_trade_platform/core/models.py`
- Test: `BA2TradeCommon/tests/test_db_seam.py`

- [ ] **Step 1: Copy config, db, models + codemod**

```bash
cd /Users/bmigette/Documents/dev/BA2
S=BA2TradePlatform/ba2_trade_platform; D=BA2TradeCommon/ba2_common
cp "$S/config.py" "$D/config.py"
cp "$S/core/db.py" "$D/core/db.py"
cp "$S/core/models.py" "$D/core/models.py"
cd BA2TradeCommon && python tools/codemod_imports.py ba2_common ba2_trade_platform
```

- [ ] **Step 2: Add the DB config seam to `ba2_common/core/db.py`**

Replace the module-level eager engine block (source `db.py:13`/`71-83`, which calls `create_engine(f"sqlite:///{DB_FILE}", …)` at import) with a lazy, configurable engine. Edit the top of `db.py`:

```python
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import select, event
import os, threading, time, atexit
from queue import Queue

from ..config import DB_FILE as _DEFAULT_DB_FILE, DB_PERF_LOG_THRESHOLD_MS
from ..logger import logger

_db_file = _DEFAULT_DB_FILE   # configurable; defaults to live path for backward-compat
_engine = None

def configure_db(db_file: str) -> None:
    """Point ba2_common at a specific sqlite file. Call BEFORE first get_engine().
    Resets any existing engine so a new path takes effect (used by tests/backtest)."""
    global _db_file, _engine
    _db_file = db_file
    _engine = None

def get_engine():
    """Lazily build (and memoize) the SQLModel engine. No DB I/O happens at import."""
    global _engine
    if _engine is None:
        os.makedirs(os.path.dirname(_db_file), exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{_db_file}",
            connect_args={"check_same_thread": False, "timeout": 30.0},
            pool_size=20, max_overflow=40, pool_timeout=10,
            pool_recycle=600, pool_pre_ping=True, echo=False,
        )
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return _engine
```

Then replace every remaining bare reference to the old module global `engine` in `db.py` (in `init_db`, `add_instance`, `update_instance`, `delete_instance`, `get_instance`, `get_all_instances`, `get_setting`, `reorder_ruleset_rules`, `move_rule_up`, `move_rule_down`) with `get_engine()`. Update `get_db()`:

```python
def get_db():
    """Return a new SQLModel session bound to the lazily-built engine."""
    return Session(get_engine())
```

And `init_db()`:

```python
def init_db():
    logger.debug("Importing models for table creation")
    from . import models  # registers all tables
    SQLModel.metadata.create_all(get_engine())
    logger.info("Database initialized with WAL mode enabled")
    _start_activity_log_worker()
```

> The `_db_write_lock`, activity-log queue/worker, and `retry_on_lock` are unchanged. This removes the only import-time DB binding; the live platform behaviour is preserved because the default path is still `config.DB_FILE`.

- [ ] **Step 3: Confirm no top-level `db`↔`models` cycle**

`grep -n "import" ba2_common/core/models.py | grep -i "\.db\b"` — every `db` import inside `models.py` must be **inside a function** (lazy), not module top-level. If a top-level `from .db import …` exists in `models.py`, move it into the methods that use it (`get_db()` is called lazily by model helper methods per the recon). `db.py` must import `models` only lazily (it already does, inside `init_db`/`_activity_log_worker`). Verify:

```bash
/tmp/v_common/bin/python -c "import ba2_common.core.models; import ba2_common.core.db; print('no cycle')"
```
Expected: prints `no cycle`.

- [ ] **Step 4: Write the DB-seam round-trip test**

`BA2TradeCommon/tests/test_db_seam.py`:

```python
def test_configure_db_isolates_to_temp(tmp_path):
    from ba2_common.core import db
    target = tmp_path / "iso.sqlite"
    db.configure_db(str(target))
    db.init_db()
    eng = db.get_engine()
    assert str(target) in str(eng.url)
    assert target.exists()

def test_appsetting_round_trip(tmp_path):
    from ba2_common.core import db
    from ba2_common.core.models import AppSetting
    db.configure_db(str(tmp_path / "rt.sqlite"))
    db.init_db()
    db.add_instance(AppSetting(key="x", value_str="42"))
    assert db.get_setting("x") == "42"
```

- [ ] **Step 5: Run the DB tests**

```bash
/tmp/v_common/bin/pip install -q -e . && /tmp/v_common/bin/python -m pytest tests/test_db_seam.py -v
```
Expected: PASS. (If `AppSetting` field names differ, reconcile from `ba2_common/core/models.py` first.)

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -m "feat(common): config + models + DB with lazy configurable engine seam"
```

---

## Task 4: `ba2_common` seams — `InstanceResolver` + `LLMServiceInterface`

**Files:**
- Create `ba2_common/core/instance_resolver.py`
- Create `ba2_common/core/interfaces/__init__.py` (start it; bases land in Task 6) and `ba2_common/core/interfaces/LLMServiceInterface.py`
- Test: `BA2TradeCommon/tests/test_seams.py`

- [ ] **Step 1: Write `instance_resolver.py`**

`BA2TradeCommon/ba2_common/core/instance_resolver.py`:

```python
"""Instance-resolution seam.

The interface bases need to turn an expert/account *id* into a live instance, but
the registries + instance caches that do that are live-platform runtime
(BA2TradePlatform). ba2_common defines the protocol; the host app injects a
concrete resolver at startup via set_instance_resolver(). Until then, calling a
resolver method raises InstanceResolverNotConfigured (loud, not silent)."""
from __future__ import annotations
from typing import Any, Optional, Protocol, runtime_checkable


class InstanceResolverNotConfigured(RuntimeError):
    """Raised when interface code needs an instance resolver but none is injected."""


@runtime_checkable
class InstanceResolver(Protocol):
    def get_expert_instance(self, expert_id: int) -> Any: ...
    def get_account_instance(self, account_id: int) -> Any: ...
    def get_account_instance_from_transaction(self, transaction: Any) -> Any: ...


class _UnconfiguredResolver:
    def _fail(self, *_a, **_k):
        raise InstanceResolverNotConfigured(
            "No InstanceResolver injected. The host app must call "
            "ba2_common.core.instance_resolver.set_instance_resolver(<resolver>) at startup."
        )
    get_expert_instance = _fail
    get_account_instance = _fail
    get_account_instance_from_transaction = _fail


_resolver: InstanceResolver = _UnconfiguredResolver()  # type: ignore[assignment]


def set_instance_resolver(resolver: InstanceResolver) -> None:
    global _resolver
    _resolver = resolver


def get_instance_resolver() -> InstanceResolver:
    return _resolver
```

- [ ] **Step 2: Write `LLMServiceInterface.py`**

`BA2TradeCommon/ba2_common/core/interfaces/LLMServiceInterface.py`:

```python
"""LLM-service seam — keeps ba2_common free of langchain/openai.

Mirrors the two ModelFactory entry points package code uses
(ba2_trade_platform/core/ModelFactory.py: create_llm @135, do_llm_call_with_websearch @893).
Return types are Any so no langchain type leaks into ba2_common. The live platform
registers a ModelFactory-backed implementation via set_llm_service()."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Optional, List, Dict


class LLMServiceNotConfigured(RuntimeError):
    """Raised when expert code needs an LLM but no service is injected."""


class LLMServiceInterface(ABC):
    @abstractmethod
    def create_llm(
        self,
        model_selection: str,
        temperature: float = 0.0,
        streaming: Optional[bool] = None,
        callbacks: Optional[List[Any]] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        track_usage: bool = True,
        use_case: str = "LangChain LLM Call",
        expert_instance_id: Optional[int] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        smart_risk_manager_job_id: Optional[int] = None,
        **extra_kwargs: Any,
    ) -> Any:
        """Return a chat-model object (langchain BaseChatModel in the live impl)."""

    @abstractmethod
    def do_llm_call_with_websearch(
        self,
        model_selection: str,
        prompt: str,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> str:
        """Return the model's text response with web search enabled."""


class _UnconfiguredLLMService(LLMServiceInterface):
    def create_llm(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured(
            "No LLMServiceInterface injected. The host app must call "
            "ba2_common.core.interfaces.LLMServiceInterface.set_llm_service(<svc>) at startup."
        )
    def do_llm_call_with_websearch(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured("No LLMServiceInterface injected.")


_llm_service: LLMServiceInterface = _UnconfiguredLLMService()


def set_llm_service(service: LLMServiceInterface) -> None:
    global _llm_service
    _llm_service = service


def get_llm_service() -> LLMServiceInterface:
    return _llm_service
```

- [ ] **Step 3: Create a minimal `interfaces/__init__.py`** (extended in Task 6)

`BA2TradeCommon/ba2_common/core/interfaces/__init__.py`:

```python
"""ba2_common interface base classes + seams."""
from .LLMServiceInterface import (
    LLMServiceInterface, LLMServiceNotConfigured, set_llm_service, get_llm_service,
)
```

- [ ] **Step 4: Write failing seam tests**

`BA2TradeCommon/tests/test_seams.py`:

```python
import pytest

def test_unconfigured_instance_resolver_raises():
    from ba2_common.core.instance_resolver import (
        get_instance_resolver, set_instance_resolver, InstanceResolverNotConfigured)
    with pytest.raises(InstanceResolverNotConfigured):
        get_instance_resolver().get_expert_instance(1)

    class Fake:
        def get_expert_instance(self, i): return f"expert-{i}"
        def get_account_instance(self, i): return f"acct-{i}"
        def get_account_instance_from_transaction(self, t): return "acct-from-txn"
    set_instance_resolver(Fake())
    assert get_instance_resolver().get_expert_instance(7) == "expert-7"

def test_unconfigured_llm_service_raises():
    from ba2_common.core.interfaces.LLMServiceInterface import (
        get_llm_service, set_llm_service, LLMServiceInterface, LLMServiceNotConfigured)
    with pytest.raises(LLMServiceNotConfigured):
        get_llm_service().create_llm("openai/gpt5")

    class FakeLLM(LLMServiceInterface):
        def create_llm(self, model_selection, **k): return ("llm", model_selection)
        def do_llm_call_with_websearch(self, model_selection, prompt, **k): return "answer"
    set_llm_service(FakeLLM())
    assert get_llm_service().do_llm_call_with_websearch("openai/gpt5", "hi") == "answer"
```

- [ ] **Step 5: Run seam tests**

```bash
/tmp/v_common/bin/pip install -q -e . && /tmp/v_common/bin/python -m pytest tests/test_seams.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -m "feat(common): InstanceResolver + LLMServiceInterface seams"
```

---

## Task 5: `ba2_common` `utils.py` split (the keystone)

**Files:**
- Create `ba2_common/core/utils.py` — the **pure** subset only
- Test: `BA2TradeCommon/tests/test_utils_pure.py`

The recon's split (source `core/utils.py`): **to `ba2_common`** = `get_labels_by_symbol`, `get_all_instrument_labels`, `add_label_to_instruments`, `remove_label_from_instruments`, `expert_uses_risk_manager`, `expert_schedules_open_positions`, `get_market_analysis_id_from_order_id`, `has_existing_transactions_for_expert_and_symbol`, `get_latest_recommendation_id_for_symbol`, `get_account_id_for_recommendation`, `calculate_transaction_pnl`, `close_transaction_with_logging`, `log_close_order_activity`, `log_transaction_created_activity`, `log_trade_action_activity`, `get_risk_manager_mode`, `get_order_status_color`, `log_analysis_batch_start`, `log_analysis_batch_end`, `log_manual_analysis`, `parse_fmp_amount_range`, `calculate_fmp_trade_metrics`, `get_setting_safe`, `get_expert_options_for_ui`. **Stays live** (NOT copied) = `get_expert_instance_from_id`, `get_account_instance_from_id`, `get_account_instance_from_transaction`, and the two top-level `from ..modules.experts/accounts import …` lines (source `utils.py:11-12`).

- [ ] **Step 1: Copy `utils.py` and strip the back-edges**

```bash
cd /Users/bmigette/Documents/dev/BA2
cp BA2TradePlatform/ba2_trade_platform/core/utils.py BA2TradeCommon/ba2_common/core/utils.py
cd BA2TradeCommon && python tools/codemod_imports.py ba2_common ba2_trade_platform
```
Then **edit `ba2_common/core/utils.py`**: delete the two top-level imports `from ..modules.experts import get_expert_class` and `from ..modules.accounts import get_account_class` (source lines 11-12), and delete the three functions `get_expert_instance_from_id`, `get_account_instance_from_id`, `get_account_instance_from_transaction`.

- [ ] **Step 2: Re-point any internal callers to the resolver**

`grep -n "get_expert_instance_from_id\|get_account_instance_from_id\|get_account_instance_from_transaction\|get_expert_class\|get_account_class" ba2_common/core/utils.py`. For each surviving call inside a *retained* function, replace with the resolver:

```python
from .instance_resolver import get_instance_resolver
# was: inst = get_account_instance_from_id(account_id)
inst = get_instance_resolver().get_account_instance(account_id)
```
If a retained function's *only* purpose was instance resolution, it should not be here — move it to the live registry list instead (note it in the task's commit message). Goal: `utils.py` imports only `ba2_common.*` + stdlib + sqlmodel.

- [ ] **Step 3: Write failing purity test**

`BA2TradeCommon/tests/test_utils_pure.py`:

```python
def test_utils_imports_without_experts_or_accounts():
    """The keystone: importing common utils must NOT require any provider/expert/account pkg."""
    import importlib, ba2_common.core.utils as u
    src = importlib.util.find_spec("ba2_common.core.utils")
    assert src is not None
    assert not hasattr(u, "get_expert_instance_from_id")  # moved to live registry

def test_parse_fmp_amount_range():
    from ba2_common.core.utils import parse_fmp_amount_range
    lo, hi = parse_fmp_amount_range("$1,001 - $15,000")  # confirm return shape vs source
    assert lo == 1001 and hi == 15000

def test_calculate_fmp_trade_metrics_smoke():
    from ba2_common.core.utils import calculate_fmp_trade_metrics
    assert callable(calculate_fmp_trade_metrics)
```

- [ ] **Step 4: Reconcile with real signatures, then run**

Confirm `parse_fmp_amount_range`'s real return type from the source and fix the assertion. Then:

```bash
/tmp/v_common/bin/pip install -q -e . && /tmp/v_common/bin/python -m pytest tests/test_utils_pure.py -v
```
Expected: PASS (and the import works with only `ba2_common` installed).

- [ ] **Step 5: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -m "feat(common): split utils into pure subset (registry funcs deferred to live host)"
```

---

## Task 6: `ba2_common` engine + interfaces (with injection)

**Files — copy then codemod, then apply the seam edits below:**
- `ba2_common/core/`: `TransactionHelper.py`, `position_sizing.py`, `TradeConditions.py`, `TradeActions.py`, `TradeActionEvaluator.py`, `TradeRiskManagement.py`, `rules_documentation.py`, `rules_export_import.py` (exporter/importer only), `news_enrichment.py`, `option_selector.py`
- `ba2_common/core/interfaces/`: all 17 source interface files
- Test: `BA2TradeCommon/tests/test_position_sizing.py`, `tests/test_interfaces_import.py`

- [ ] **Step 1: Copy the engine modules + interfaces + codemod**

```bash
cd /Users/bmigette/Documents/dev/BA2
S=BA2TradePlatform/ba2_trade_platform; D=BA2TradeCommon/ba2_common
for f in TransactionHelper position_sizing TradeConditions TradeActions TradeActionEvaluator \
         TradeRiskManagement rules_documentation rules_export_import news_enrichment option_selector; do
  cp "$S/core/$f.py" "$D/core/$f.py"
done
cp "$S/core/interfaces/"*.py "$D/core/interfaces/"
cd BA2TradeCommon && python tools/codemod_imports.py ba2_common ba2_trade_platform
```

- [ ] **Step 2: Inject the ATR fetch in `position_sizing.get_latest_atr`**

The pure `compute_risk_based_quantity` / `derive_stop_for_quantity` already accept `atr`/`stop_price` — leave them. Only `get_latest_atr` (source line 206) lazily imports a provider (line 213). Replace it with an injected indicator provider:

```python
def get_latest_atr(symbol, indicator_provider, period: int = 14, interval: str = "1d"):
    """Fetch the latest ATR via an injected MarketIndicatorsInterface provider.

    indicator_provider: a ba2_common.core.interfaces.MarketIndicatorsInterface impl
        (the live host / backtest passes a ba2_providers PandasIndicatorCalc, or a
        pre-fetched cache adapter). Returns None on failure. ba2_common never imports
        ba2_providers."""
    from datetime import datetime, timezone
    from ..logger import logger
    try:
        lookback = max(period * 4, 60)
        result = indicator_provider.get_indicator(
            symbol, "atr", end_date=datetime.now(timezone.utc),
            lookback_days=lookback, interval=interval, format_type="dict",
        )
        values = (result or {}).get("values") or []
        for v in reversed(values):
            if v is not None:
                return float(v)
        logger.warning(f"get_latest_atr: no ATR value for {symbol}")
        return None
    except Exception as e:
        logger.warning(f"get_latest_atr failed for {symbol}: {e}")
        return None
```
Then fix the one caller in `ba2_common/core/TradeRiskManagement.py` (source lazy import line ~730): it must obtain an indicator provider and pass it to `get_latest_atr(...)`. Since `ba2_common` cannot import `ba2_providers`, the RM should accept the indicator provider via its constructor/method (inject from the host) OR compute risk only when an `atr`/`stop_price` is already available. Apply the minimal change: thread an optional `indicator_provider=None` parameter through the RM sizing call; when `None`, skip the ATR path (the pure `compute_risk_based_quantity` already handles "no usable ATR" by returning a reasoned zero). Record this as a known Phase-6 wiring point in the commit message.

- [ ] **Step 3: Inject the provider lookup in `TradeConditions`**

Source back-edges: lazy `from ..modules.dataproviders import get_provider` (lines 1576, 1634), `import fmpsdk` (1744), lazy `from .interfaces ... OptionsAccountInterface` (1687, intra-common → fine). For the two `get_provider` sites and the `fmpsdk` site, replace the direct lookup with an injected resolver. Add at the top of the condition class that needs data:

```python
# ba2_common must not import ba2_providers. Data access is injected.
from .interfaces.LLMServiceInterface import get_llm_service  # only if the class needs LLM
# provider access:
_provider_resolver = None
def set_provider_resolver(fn):
    """fn(category:str, name:str, **kw) -> provider instance. Injected by the host."""
    global _provider_resolver
    _provider_resolver = fn
def _get_provider(category, name, **kw):
    if _provider_resolver is None:
        raise RuntimeError("TradeConditions provider resolver not configured (host must call set_provider_resolver)")
    return _provider_resolver(category, name, **kw)
```
Replace the two `get_provider(...)` call sites with `_get_provider(...)`, and the direct `import fmpsdk` + its usage with a call routed through `_get_provider("fundamentals_details"/appropriate, "fmp")` (or the matching provider method). Confirm the exact data each call needs from the source before substituting.

- [ ] **Step 4: Sever the interface back-edges (resolver + caches)**

In `ba2_common/core/interfaces/`:
- `AccountInterface.py` (source lazy `..utils.get_expert_instance_from_id` + activity-log helpers @334,348,768,1155,1180,1192,1344,1391,1437): replace `get_expert_instance_from_id(...)` with `from ..instance_resolver import get_instance_resolver; get_instance_resolver().get_expert_instance(...)`; keep `close_transaction_with_logging`/`log_*` as `from ..utils import …` (now common-pure).
- `MarketExpertInterface.py` (source lazy `..utils.get_account_instance_from_id` @400,571,629,681,858): replace with `get_instance_resolver().get_account_instance(...)`.
- `ExtendableSettingsInterface.py` (source lazy `..AccountInstanceCache` @278 / `..ExpertInstanceCache` @282 — both stay live): remove those lazy imports; route any cached-instance access through `get_instance_resolver()` (the live resolver wraps the caches). If the cache was only an optimization, fall back to a direct resolver call.
- `MarketNewsInterface.py` (source lazy `..news_enrichment` @49): now `from ..news_enrichment import …` (intra-common; news_enrichment copied in Step 1) — verify it resolves.

`grep -rn "modules\.\(experts\|accounts\|dataproviders\)\|ModelFactory\|InstanceCache" ba2_common/core/interfaces/` must return **nothing**.

- [ ] **Step 5: Split `rules_export_import.py`** — keep exporter/importer, drop the UI class

In `ba2_common/core/rules_export_import.py` delete the `RulesExportImportUI` class (source lines ~376-end; the only `from nicegui import ui` user). Keep `RulesExporter`/`RulesImporter` (deps `models`/`db` only). `grep -n "nicegui" ba2_common/core/rules_export_import.py` must return nothing. (The UI class is re-created in `BA2TradePlatform` in Phase 6 — out of scope here.)

- [ ] **Step 6: Finish `interfaces/__init__.py`**

Extend `ba2_common/core/interfaces/__init__.py` to re-export all bases (mirror the source `core/interfaces/__init__.py` re-export list) **plus** the `LLMServiceInterface` exports from Task 4. Confirm names against the source `__init__.py`.

- [ ] **Step 7: Write failing `position_sizing` tests**

`BA2TradeCommon/tests/test_position_sizing.py`:

```python
from ba2_common.core.position_sizing import compute_risk_based_quantity, derive_stop_for_quantity

def test_risk_quantity_by_explicit_stop():
    # equity 100k, risk 1% => $1000 risk; stop $2 below $100 entry => 500 shares.
    out = compute_risk_based_quantity(100_000, 100.0, 1.0, stop_price=98.0)
    assert out["quantity"] == 500
    assert out["risk_per_share"] == 2.0

def test_risk_quantity_floored_by_min_stop_pct():
    # tight $0.50 stop on $100 => 1% stop, but min_stop_pct 7% floors risk/share to $7 => 142.
    out = compute_risk_based_quantity(100_000, 100.0, 1.0, stop_price=99.5, min_stop_pct=7.0)
    assert out["quantity"] == 142
    assert out.get("stop_floored") is True

def test_risk_quantity_capped_by_notional():
    out = compute_risk_based_quantity(1_000_000, 100.0, 1.0, stop_price=98.0,
                                      max_position_value=10_000)
    assert out["quantity"] == 100
    assert out["capped_by"] == "notional"

def test_derive_stop_reduces_qty_to_keep_min_stop():
    # 1000 shares of $100 at 1% of 100k = $1000 budget => $1 stop = 1% < 7% min,
    # so qty reduces to risk_dollars/min_stop_dist = 1000/7 = 142.
    out = derive_stop_for_quantity(100_000, 100.0, 1000, 1.0, is_long=True, min_stop_pct=7.0)
    assert out["quantity"] == 142
    assert out["rejected"] is False
    assert out["sl_price"] < 100.0
```

- [ ] **Step 8: Run position-sizing tests**

```bash
/tmp/v_common/bin/pip install -q -e . && /tmp/v_common/bin/python -m pytest tests/test_position_sizing.py -v
```
Expected: PASS (these encode the documented behaviour from `position_sizing.py`).

- [ ] **Step 9: Write + run the interfaces import-smoke**

`BA2TradeCommon/tests/test_interfaces_import.py`:

```python
def test_all_interfaces_import_clean():
    import ba2_common.core.interfaces as I
    for name in ["AccountInterface", "OptionsAccountInterface", "ReadOnlyAccountInterface",
                 "MarketExpertInterface", "LiveExpertInterface", "ExtendableSettingsInterface",
                 "MarketDataProviderInterface", "MarketIndicatorsInterface",
                 "CompanyFundamentalsOverviewInterface", "CompanyFundamentalsDetailsInterface",
                 "CompanyInsiderInterface", "MacroEconomicsInterface", "MarketNewsInterface",
                 "SocialMediaDataProviderInterface", "ScreenerProviderInterface",
                 "SmartRiskExpertInterface", "LLMServiceInterface"]:
        assert hasattr(I, name), f"missing {name}"

def test_ruleset_engine_imports_without_providers():
    import ba2_common.core.TradeConditions
    import ba2_common.core.TradeActions
    import ba2_common.core.TradeActionEvaluator
    import ba2_common.core.TradeRiskManagement
```
```bash
/tmp/v_common/bin/python -m pytest tests/test_interfaces_import.py -v
```
Expected: PASS with only `ba2_common` installed (no `fmpsdk`, no providers).

- [ ] **Step 10: Commit**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -m "feat(common): interfaces + ruleset/RM engine with resolver/provider/ATR injection"
```

---

## Task 7: `ba2_common` clean-room gate + import-linter

- [ ] **Step 1: Fresh-venv import gate (no provider/LLM deps present)**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeCommon
rm -rf /tmp/v_clean && python -m venv /tmp/v_clean
/tmp/v_clean/bin/pip install -q -e .          # NOTE: not [dev]; only runtime deps
/tmp/v_clean/bin/python - <<'PY'
import importlib
for m in ["ba2_common", "ba2_common.config", "ba2_common.logger",
          "ba2_common.core.types", "ba2_common.core.models", "ba2_common.core.db",
          "ba2_common.core.utils", "ba2_common.core.position_sizing",
          "ba2_common.core.weinstein", "ba2_common.core.interfaces",
          "ba2_common.core.TradeConditions", "ba2_common.core.TradeActions",
          "ba2_common.core.TradeActionEvaluator", "ba2_common.core.TradeRiskManagement",
          "ba2_common.core.rules_export_import"]:
    importlib.import_module(m)
for forbidden in ["langchain", "langchain_core", "fmpsdk", "nicegui", "ba2_providers", "ba2_experts"]:
    try:
        importlib.import_module(forbidden); raise SystemExit(f"FAIL: {forbidden} importable/pulled")
    except ImportError:
        pass
print("CLEAN: ba2_common imports with zero provider/LLM/UI deps")
PY
```
Expected: prints `CLEAN: …`. If a forbidden module is importable it means a dep leaked into `pyproject.toml` or a module still imports it — fix before proceeding.

- [ ] **Step 2: Run import-linter**

```bash
/tmp/v_common/bin/pip install -q -e ".[dev]"
cd /Users/bmigette/Documents/dev/BA2/BA2TradeCommon && /tmp/v_common/bin/lint-imports
```
Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 3: Full `ba2_common` test run + commit**

```bash
/tmp/v_common/bin/python -m pytest -q
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeCommon commit -am "test(common): clean-room import gate + import-linter green" || true
```
Expected: all tests pass.

---

## Task 8: `ba2_providers`

**Files — copy the provider tree (minus the 3 AI providers) + StockScreener, then codemod + edit registries.**

- [ ] **Step 1: Copy providers (excluding AI providers) + StockScreener**

```bash
cd /Users/bmigette/Documents/dev/BA2
S=BA2TradePlatform/ba2_trade_platform; D=BA2TradeProviders/ba2_providers
# whole dataproviders tree -> ba2_providers root (flatten)
cp -R "$S/modules/dataproviders/." "$D/"
cp "$S/core/StockScreener.py" "$D/StockScreener.py"
# drop the 3 LLM-coupled AI providers (they STAY in BA2TradePlatform)
rm "$D/news/AINewsProvider.py" \
   "$D/fundamentals/overview/AICompanyOverviewProvider.py" \
   "$D/socialmedia/AISocialMediaSentiment.py"
cp ../BA2TradeCommon/tools/codemod_imports.py "$D/../tools/" 2>/dev/null || mkdir -p "$D/../tools" && cp ../BA2TradeCommon/tools/codemod_imports.py "$D/../tools/"
cd BA2TradeProviders && python tools/codemod_imports.py ba2_providers ba2_trade_platform.modules.dataproviders
```
> The `__init__.py` of `ba2_providers` was overwritten by the copy — it now holds the source registry. The next step fixes it. `StockScreener.py` came from `core/`; its `from .weinstein` becomes `from ba2_common.core.weinstein` (codemod handles via the `ba2_trade_platform.core` map after absolutize — confirm) and its lazy `get_provider`/`fmp_common` are now intra-`ba2_providers`.

- [ ] **Step 2: Remove AI providers from the registry**

Edit `ba2_providers/__init__.py` (was `modules/dataproviders/__init__.py`):
- Delete the AI imports: from the news import line drop `AINewsProvider`; delete `AICompanyOverviewProvider` from the fundamentals import block; delete `from .socialmedia import AISocialMediaSentiment` (source lines 47, 51, 59).
- Delete the `"ai"` registry entries: `FUNDAMENTALS_OVERVIEW_PROVIDERS["ai"]` (76), `NEWS_PROVIDERS["ai"]` (91), `SOCIALMEDIA_PROVIDERS["ai"]` (109). Leave `SOCIALMEDIA_PROVIDERS` with the StockTwits entries (add `"stocktwits"`/`"stocktwits_trending"` keys if the source only had `"ai"` — confirm the registry has non-AI socialmedia entries; if `SOCIALMEDIA_PROVIDERS` becomes empty, register the StockTwits classes that already live in `ba2_providers/socialmedia/`).
- Remove `AINewsProvider`, `AICompanyOverviewProvider`, `AISocialMediaSentiment` from `__all__`.
- Fix the docstring example imports (`from ba2_trade_platform.modules.dataproviders import …` → `from ba2_providers import …`) and the top imports `from ba2_trade_platform.logger import logger` → `from ba2_common.logger import logger`, `from ba2_trade_platform.core.interfaces import (…)` → `from ba2_common.core.interfaces import (…)`.

- [ ] **Step 3: Drop AI re-exports from sub-package `__init__.py`**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradeProviders/ba2_providers
grep -rn "AINewsProvider\|AICompanyOverviewProvider\|AISocialMediaSentiment" news/__init__.py fundamentals/overview/__init__.py fundamentals/__init__.py socialmedia/__init__.py
```
Edit each listed `__init__.py` to delete the AI-provider import/re-export line (source: news `__init__` line 16, fundamentals/overview `__init__` line 13, socialmedia `__init__` line 13; `fundamentals/__init__` re-exports overview — drop the AI name from its list).

- [ ] **Step 4: Write failing providers import-smoke**

`BA2TradeProviders/tests/test_providers_import.py`:

```python
import importlib, pytest

def test_providers_import_without_llm():
    import ba2_providers
    from ba2_providers import get_provider
    assert callable(get_provider)

def test_no_ai_providers_registered():
    import ba2_providers as p
    # AI providers stay in the live platform for Phase 0
    for cat in ["news", "fundamentals_overview", "socialmedia"]:
        # get_provider must raise for "ai", not import ModelFactory
        with pytest.raises(ValueError):
            get = p.get_provider
            get(cat, "ai")

def test_modelfactory_not_pulled():
    with pytest.raises(ImportError):
        importlib.import_module("langchain_core")
```

- [ ] **Step 5: Write a deterministic-provider behaviour test (no network)**

`BA2TradeProviders/tests/test_fmp_provider_construct.py`:

```python
def test_construct_fmp_ohlcv_provider():
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    prov = FMPOHLCVProvider()              # construction must not hit the network
    assert prov is not None

def test_construct_screener_engine():
    from ba2_providers.StockScreener import StockScreener  # confirm class name in source
    assert StockScreener is not None
```

- [ ] **Step 6: Install chain + run providers tests**

```bash
cd /Users/bmigette/Documents/dev/BA2
rm -rf /tmp/v_prov && python -m venv /tmp/v_prov
/tmp/v_prov/bin/pip install -q -e BA2TradeCommon          # common first (local)
/tmp/v_prov/bin/pip install -q -e "BA2TradeProviders[dev]"
cd BA2TradeProviders && /tmp/v_prov/bin/python -m pytest -q
```
Expected: PASS. (Reconcile class/registry names with source if a test errors on a name.)

- [ ] **Step 7: import-linter + commit**

```bash
/tmp/v_prov/bin/lint-imports
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeProviders commit -m "feat(providers): extract data providers (AI providers + ModelFactory excluded)"
```
Expected: `Contracts: 1 kept, 0 broken.`

---

## Task 9: `ba2_experts`

**Files — copy experts (minus TradingAgents/UI) + expert_mixins + FactorRanker + Penny, then codemod + LLM injection.**

- [ ] **Step 1: Copy experts (excluding TradingAgents) + codemod**

```bash
cd /Users/bmigette/Documents/dev/BA2
S=BA2TradePlatform/ba2_trade_platform; D=BA2TradeExperts/ba2_experts
cp -R "$S/modules/experts/." "$D/"
rm "$D/TradingAgents.py" "$D/TradingAgentsUI.py"        # LLM, out of scope -> stay live
mkdir -p BA2TradeExperts/tools && cp BA2TradeCommon/tools/codemod_imports.py BA2TradeExperts/tools/
cd BA2TradeExperts && python tools/codemod_imports.py ba2_experts ba2_trade_platform.modules.experts
```

- [ ] **Step 2: Fix the experts registry**

Edit `ba2_experts/__init__.py` (source has `from .TradingAgents import TradingAgents` @1 and `TradingAgents` first in the `experts` list @12). Delete the `TradingAgents` import line and remove `TradingAgents` from the `experts` list. Resulting list: `[FinnHubRating, FMPRating, FMPSenateTraderWeight, FMPSenateTraderCopy, FMPInsiderClusterBuy, FMPEarningsDrift, PennyMomentumTrader, FactorRanker]`. Keep `get_expert_class`.

- [ ] **Step 3: Convert Penny's `ModelFactory` imports to the LLM seam**

In each of `ba2_experts/PennyMomentumTrader/{data_gathering.py (line 15), monitoring.py (line 18), screening.py (line 18)}`: delete `from ....core.ModelFactory import ModelFactory` (now `from ba2_common.core.ModelFactory …` after codemod — which does not exist; that's the violation). Replace each `ModelFactory.create_llm(...)` call site:

```python
# was: llm = ModelFactory.create_llm(model_selection, temperature=0.0, ...)
from ba2_common.core.interfaces.LLMServiceInterface import get_llm_service
llm = get_llm_service().create_llm(model_selection, temperature=0.0, ...)  # same kwargs
```
And in `screening.py` line 265, replace `from ...core.InstrumentAutoAdder import get_instrument_auto_adder` (live infra) with an optional injected hook:

```python
# InstrumentAutoAdder is live-platform infra. Make it an optional, host-provided hook.
def _maybe_auto_add_instruments(symbols):
    try:
        from ba2_experts import get_instrument_auto_adder_hook  # set by host, default no-op
    except Exception:
        return
    hook = get_instrument_auto_adder_hook()
    if hook:
        hook(symbols)
```
Add to `ba2_experts/__init__.py`:

```python
_instrument_auto_adder_hook = None
def set_instrument_auto_adder_hook(fn):
    global _instrument_auto_adder_hook
    _instrument_auto_adder_hook = fn
def get_instrument_auto_adder_hook():
    return _instrument_auto_adder_hook
```
`grep -rn "ModelFactory\|InstrumentAutoAdder" ba2_experts/` must return nothing after this step.

- [ ] **Step 4: Make per-expert UI imports lazy/optional**

In `ba2_experts/FactorRanker/ui.py` and `ba2_experts/PennyMomentumTrader/ui.py`, ensure `import nicegui`/`from nicegui import ui` happens **inside** the render functions (lazy), not at module top, so `import ba2_experts` works without the `[ui]` extra. If the source already imports nicegui lazily, leave it; otherwise move the import into the function body. Repoint any `from ...ui.components …` (live platform UI) — the recon found none in these two files, but `grep -rn "ba2_trade_platform.ui\|from ....ui\|ui.components" ba2_experts/` must return nothing (if it does, that render path stays live — guard or drop it).

- [ ] **Step 5: Repoint FactorRanker/Penny absolute imports + resolver**

`grep -rn "ba2_trade_platform" ba2_experts/` — should be empty after codemod. For `FactorRanker/portfolio.py` and `PennyMomentumTrader/trade_manager.py` (source used absolute `ba2_trade_platform.core.*` + `core.utils` registry funcs): confirm codemod mapped `core.*` → `ba2_common.core.*`, and replace any `get_account_instance_from_id`/`get_expert_instance_from_id` calls with `from ba2_common.core.instance_resolver import get_instance_resolver; get_instance_resolver().get_account_instance(...)`. Same for `FMPSenateTraderCopy.py`/`FMPSenateTraderWeight.py` (pure `calculate_fmp_trade_metrics`/`parse_fmp_amount_range` now come from `ba2_common.core.utils`; the registry call uses the resolver).

- [ ] **Step 6: Write the clean-expert calculator tests (the key payoff)**

`BA2TradeExperts/tests/test_clean_expert_calculators.py`:

```python
from datetime import datetime, timezone
from ba2_experts.FMPEarningsDrift import evaluate_earnings_drift
from ba2_experts.FMPInsiderClusterBuy import detect_insider_cluster

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)

def test_earnings_drift_fresh_beat_signals():
    row = {"report_date": "2026-06-10", "reported_eps": 1.20, "estimated_eps": 1.00,
           "surprise_percent": 20.0}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0, max_days_since_report=30)
    assert out["is_signal"] is True
    assert out["surprise_pct"] == 20.0
    assert out["days_since_report"] == 3
    assert 55.0 <= out["confidence"] <= 100.0

def test_earnings_drift_stale_report_no_signal():
    row = {"report_date": "2026-04-01", "reported_eps": 1.2, "estimated_eps": 1.0,
           "surprise_percent": 20.0}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0, max_days_since_report=30)
    assert out["is_signal"] is False
    assert "not fresh" in out["reason"]

def test_earnings_drift_below_threshold_no_signal():
    row = {"report_date": "2026-06-12", "reported_eps": 1.01, "estimated_eps": 1.00,
           "surprise_percent": 1.0}
    out = evaluate_earnings_drift(row, NOW, surprise_min_pct=5.0, max_days_since_report=30)
    assert out["is_signal"] is False

def test_earnings_drift_no_data():
    out = evaluate_earnings_drift(None, NOW, 5.0, 30)
    assert out["is_signal"] is False and out["reason"] == "no earnings data"

def test_insider_cluster_three_buyers_signals():
    txns = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
    ]
    out = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
    assert out["is_cluster"] is True
    assert out["buyer_count"] == 3
    assert out["buy_value"] == 300_000
    assert out["confidence"] > 55.0

def test_insider_cluster_two_buyers_no_signal():
    txns = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
    ]
    out = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
    assert out["is_cluster"] is False and out["buyer_count"] == 2

def test_insider_cluster_sells_reduce_confidence():
    txns = [
        {"insider_name": "A", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "B", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "C", "transaction_type": "P-Purchase", "value": 100_000},
        {"insider_name": "D", "transaction_type": "S-Sale", "value": 150_000},
    ]
    out = detect_insider_cluster(txns, min_insiders=3, min_total_value=200_000)
    assert out["is_cluster"] is True
    assert out["sell_value"] == 150_000
```

- [ ] **Step 7: Write the experts import-smoke (no langchain)**

`BA2TradeExperts/tests/test_experts_import.py`:

```python
import importlib, pytest

def test_experts_package_imports_without_langchain():
    import ba2_experts
    from ba2_experts import get_expert_class
    assert get_expert_class("FMPEarningsDrift") is not None
    assert get_expert_class("TradingAgents") is None   # stays in the live platform

def test_langchain_not_pulled_by_experts():
    with pytest.raises(ImportError):
        importlib.import_module("langchain_core")

def test_penny_modules_import_via_llm_seam():
    import ba2_experts.PennyMomentumTrader.data_gathering
    import ba2_experts.PennyMomentumTrader.monitoring
    import ba2_experts.PennyMomentumTrader.screening
```

- [ ] **Step 8: Install full chain + run experts tests**

```bash
cd /Users/bmigette/Documents/dev/BA2
rm -rf /tmp/v_exp && python -m venv /tmp/v_exp
/tmp/v_exp/bin/pip install -q -e BA2TradeCommon
/tmp/v_exp/bin/pip install -q -e BA2TradeProviders
/tmp/v_exp/bin/pip install -q -e "BA2TradeExperts[dev]"
cd BA2TradeExperts && /tmp/v_exp/bin/python -m pytest -q
```
Expected: PASS — especially the calculator tests (real decision logic preserved) and `langchain` NOT importable.

- [ ] **Step 9: import-linter + commit**

```bash
/tmp/v_exp/bin/lint-imports
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts add -A
git -C /Users/bmigette/Documents/dev/BA2/BA2TradeExperts commit -m "feat(experts): extract experts with LLM-service injection (TradingAgents excluded)"
```
Expected: `Contracts: 1 kept, 0 broken.`

---

## Task 10: End-to-end install + cross-package gate + push

- [ ] **Step 1: Editable chain install via `install.sh`**

```bash
cd /Users/bmigette/Documents/dev/BA2
rm -rf /tmp/v_e2e && python -m venv /tmp/v_e2e
PYTHON=/tmp/v_e2e/bin/python bash BA2TradeCommon/install.sh --editable
```
Expected: ends with `ok 0.1.0` (all three import together).

- [ ] **Step 2: Cross-package smoke (one real chain)**

```bash
/tmp/v_e2e/bin/python - <<'PY'
from ba2_experts.FMPInsiderClusterBuy import detect_insider_cluster
from ba2_providers import get_provider
from ba2_common.core.position_sizing import compute_risk_based_quantity
print("chain ok:",
      detect_insider_cluster([], 3, 1)["is_cluster"],
      callable(get_provider),
      compute_risk_based_quantity(100_000, 100, 1.0, stop_price=98)["quantity"])
PY
```
Expected: `chain ok: False True 500`.

- [ ] **Step 3: Verify BA2TradePlatform is untouched**

```bash
git -C /Users/bmigette/Documents/dev/BA2/BA2TradePlatform status --short
```
Expected: only `docs/plans/2026-06-13-backtest-platform-phase0-plan.md` (this file) + the pre-existing untracked files; **no** changes under `ba2_trade_platform/`.

- [ ] **Step 4: Run the full live test suite to confirm no regression**

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform && python -m pytest -q
```
Expected: same pass/fail baseline as before Phase 0 (we changed none of its code).

- [ ] **Step 5: Push the three package branches (only after approval to publish)**

```bash
for r in BA2TradeCommon BA2TradeProviders BA2TradeExperts; do
  git -C /Users/bmigette/Documents/dev/BA2/$r push -u origin phase0-extraction
done
```
> Pushing is outward-facing — do this only when the user confirms. Then the `install.sh` git mode (non-editable) can be validated against the pushed branches.

---

## Task 11 (OPTIONAL — Phase 6 preview; only if "move + rewire live now" was chosen)

Not part of Model A Phase 0. If selected, after Tasks 1–10:
- In `BA2TradePlatform`, create `ba2_trade_platform/core/instance_registry.py` holding the 3 instance-factory funcs (`get_expert_instance_from_id`, `get_account_instance_from_id`, `get_account_instance_from_transaction`) + the live caches, exposing an `InstanceResolver` impl; call `set_instance_resolver(...)`, `set_llm_service(ModelFactoryLLMService())`, `TradeConditions.set_provider_resolver(get_provider)`, and `set_instrument_auto_adder_hook(...)` at startup (`main.py`).
- Replace the in-tree `ba2_trade_platform/{core,modules/dataproviders,modules/experts}` with thin re-export shims importing from the packages (or delete and import directly), keeping `AlpacaAccount`/Smart RM/TradingAgents/UI/LLM stack/AI providers live.
- **Golden test:** for each clean expert, assert live `run_analysis(...)` == `analyze_as_of(now)` produce identical `Recommendation`s (the design's Phase 1 acceptance gate).

---

## Self-Review

**Spec coverage (design §6 Phase 0 + §1 table):**
- "Create BA2TradeProviders/BA2TradeExperts; pyproject each + BA2TradeCommon/install.{sh,ps1}" → Task 1. ✓
- "Move interfaces/types/TradeConditions/models/classic risk manager/position_sizing → common" → Tasks 2–6. ✓
- "providers → providers" → Task 8. ✓ "experts → experts" → Task 9. ✓
- "install script installs the chain from git; --editable from local" → Task 1 Step 9 + Task 10. ✓
- "Classic RM only; smart RM stays" → SmartRiskManager* mapped to stays; not copied. ✓
- §1 "consumed by both platforms" / divergence kill → packages produced; live consumption is Phase 6 (Task 11). ✓ (intentional per Decision 1)
- Options-ready: `OptionsAccountInterface` + `option_types`/`option_selector` → `ba2_common` (Tasks 4/6). ✓

**Placeholder scan:** new artifacts (pyproject×3, install×2, importlinter×3, codemod, seams, DB seam, position_sizing injection, tests) contain full code. Move steps give exact paths + commands + reconciliation notes (the "confirm name in source" notes are deliberate guards against name drift in 90KB+ files, not placeholders). No "TBD"/"add error handling".

**Type/name consistency:** seam APIs are consistent across tasks — `configure_db`/`get_engine` (Task 3, used in conftest Task 1 & gate Task 7); `set/get_instance_resolver` + `get_expert_instance/get_account_instance` (Tasks 4,5,6,9); `set/get_llm_service` + `create_llm/do_llm_call_with_websearch` (Tasks 4,9); `set_provider_resolver` (Task 6, wired Task 11); `set/get_instrument_auto_adder_hook` (Task 9, wired Task 11). Calculator signatures (`evaluate_earnings_drift`, `detect_insider_cluster`) match the source exactly.

**Known reconciliation points (verify against source during execution, do not assume):** `weinstein` public fn name; `AppSetting` field names; `parse_fmp_amount_range` return shape; `StockScreener` class name; whether `SOCIALMEDIA_PROVIDERS` has non-AI entries; exact `TradeConditions` data needs at the 3 injection sites; exact Penny `create_llm` kwargs at each call site.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-06-13-backtest-platform-phase0-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`).
2. **Inline Execution** — execute tasks in this session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

Both run in branches `phase0-extraction` on the three package repos; `BA2TradePlatform` stays read-only (Model A).
