# Per-package versioning with a test-platform minimum (2026-10-03)

Status: implemented on `feat/package-versions`. Operator decision (2026-10-03): each shared
package gets its own version, and the test platform enforces a minimum package version only when
it needs one.

## Problem

CLAUDE.md required any change under `packages/` to bump `TEST_APP_VERSION`, because GA workers
decide whether to self-update by comparing that one string
(`worker_client.ensure_synced`). A broker-only module such as the in-flight IBKR modules
(`ba2_common/core/ibkr_mapping.py`) could not affect a GA result, yet forced every worker to
`git pull`, reinstall and restart, which is costly mid-grid.

## Design

### Files

| File | Content |
|---|---|
| `packages/{common,providers,experts}/<pkg>/version.py` | `PACKAGE_VERSION = "YYYY.MM.NNNNN"` (5-digit build). Initial `2026.10.00001`. |
| `packages/<dir>/<pkg>/__init__.py` | `from .version import PACKAGE_VERSION as __version__` (single source). |
| `packages/<dir>/pyproject.toml` | `version` is the same string, kept static; a test and `check_package_versions.py` assert equality. |
| `testplatform/required_package_versions.py` | `REQUIRED_PACKAGE_VERSIONS`, equal to the initial versions so the first sync is a no-op. |
| `testplatform/ga_neutral_package_paths.py` | `GA_NEUTRAL_GLOBS`, the reviewed allowlist. |
| `testplatform/backend/app/services/package_versions.py` | Stdlib-only parse/compare/read helpers, shared by the sync path and the guard tool. |
| `tools/check_package_versions.py` | The CI/by-hand guard. |

**Deviation from the brief (pyproject).** The brief allowed "pyproject reads it dynamically".
Hatch dynamic versioning needs `hatchling` at install time; it is not installed in the shared
venv, and workers reinstall the package chain on every pull, so a dynamic-version mistake could
not be verified here without touching an environment. pyproject therefore stays static and the
agreement is enforced by tests and the guard (rule 0). The cost is editing two lines per bump.

Versions are compared numerically as `(year, month, build)`, never as strings
(`"2026.10.99" > "2026.10.100"` as strings). TEST/APP versions are zero-padded to 4 digits and
packages to 5; the parser accepts any build width, packages are additionally required to be exactly
5 digits.

### Worker sync rule

`ensure_synced(worker, master_version, ..., required_packages=None, master_packages=None,
drift_seen=None)`. A worker is re-synced when ANY of:

1. its payload lacks `version_scheme` (pre-split code; unchanged),
2. its `app_version` differs from the master's `TEST_APP_VERSION` (unchanged),
3. `required_packages` is given and a package it reports is below the minimum, or it reports no
   usable version for a required package ("unknown").

A worker whose `TEST_APP_VERSION` matches and whose packages are at/above the minimums is NOT
re-synced merely because a package version differs from the master's.
`required_packages=None` (the default) is exactly the previous behaviour; this keeps every
pre-existing test and any caller that does not opt in unchanged. The production callers
(`DistributedEvaluator` via `strategy_optimization_handler`, `api/workers.py` manual update, and
`ba2test_launcher` backtest fan-out) pass the master's minimums from `get_version_info()`.

**Operator-chosen semantics (reproducibility).** A worker at/above the minimums but BELOW the
master's package version runs OLDER package code than the master, by design: the operator declared
the intervening changes GA-neutral by not raising the minimum. Reproducibility of GA results is then
guaranteed only to the extent that declaration is right. That is why the allowlist is an explicit,
reviewed diff (below) and why drift is never silent.

**Drift visibility.** After a successful check the master compares each worker's package versions
with its own and emits one `WARNING ... package version DRIFT within the declared minimum ...
ba2_common master=... worker=...` line per worker per distinct drift, deduplicated per job (the
`DistributedEvaluator` owns a `_drift_seen` set; a new job warns again). It goes to the job log and
to the module logger.

**Old workers.** The version payload (`GET /version`, also nested under `/health["version"]`) gains
two additive keys: `package_versions` (read from the checkout by text) and
`required_package_versions`. A worker built before this change omits `package_versions`; the new
master treats every required package as "unknown": it logs `WARNING worker X reports NO version for
package(s) ...`, triggers `/update` once, and converges when the pulled worker reports versions. If it
never does, it is excluded with `still failing package minimums`, never a crash. An OLD master with a
NEW worker simply ignores the extra keys (and never passes minimums).

`unsyncable_reason` now also refuses a distributed run if `required_package_versions.py` or a package
`version.py` is uncommitted (a worker's `git pull` could never reach it).

### Guard (the risk this scheme creates)

`python tools/check_package_versions.py [--base REF] [--include-worktree] [--root DIR]`

* Rule 0, consistency (always): versions well formed and equal to pyproject; every package has a
  minimum, well formed and not above the package's own version.
* Rule 1: any change to shipped package code must bump `PACKAGE_VERSION`. Shipped means
  `packages/<dir>/<pkg>/**` excluding `tests/`, `test_files/`, `docs/`, `*.md`, `test_*.py`,
  `conftest.py`, plus `packages/<dir>/pyproject.toml` unless only its `version =` line changed.
* Rule 2: if any shipped changed file is not matched by `GA_NEUTRAL_GLOBS`, the package's minimum
  must also be raised in the same diff, or `TEST_APP_VERSION` bumped.

Base: `--base`, else `$BA2_VERSION_CHECK_BASE`, else the first resolvable of `origin/dev`,
`origin/main`, `dev`, `main` that shares history with HEAD (the diff is `merge-base...HEAD`). If none
resolves (shallow CI checkout, not a git repo) only rule 0 runs, the tool prints
`base ref unavailable ... Rules 1-2 ... SKIPPED`, and the exit status is 0. `--include-worktree`
compares the working tree (tracked, uncommitted changes) instead of HEAD; untracked new files need
`git add` first.

Failure messages name the rule, the offending files and the exact fix (which file to edit, or which
glob list to extend).

**How to declare a path GA-neutral:** append a narrow `fnmatch` glob (`*` crosses `/`) to
`GA_NEUTRAL_GLOBS` in the same commit and justify it in the commit message (the code must be
unreachable from the GA/backtest path). Never list a module the backtest engine imports.

### What to bump when

| Change | Bump |
|---|---|
| `ba2_trade_platform/` only | `APP_VERSION` |
| `testplatform/` | `TEST_APP_VERSION` |
| shipped `packages/<dir>/<pkg>/` code | that package's `PACKAGE_VERSION` (+ pyproject), always |
| ... that can affect GA results | also raise its entry in `required_package_versions.py` |
| ... that cannot | add the path to `ga_neutral_package_paths.py` |

## Limits and open points

* `package_versions` are read from files in the worker's checkout, like `app_version`; they are
  what the last `git pull` brought, not what the running interpreter imported. The existing
  update path restarts after pulling, so the two converge; a non-editable stale site-packages copy
  is reinstalled by `self_update` on pull, as before.
* With the minimum enforced only at pre-flight, a worker that drifts mid-job is not re-checked
  (same as `TEST_APP_VERSION` today).
* Nothing here was exercised against a real worker or the running grid; behaviour of the live
  `/update` -> `/version` loop is covered only through fakes.
* A bump to `TEST_APP_VERSION`, the first set of values, and the first real minimum raise are left
  to the controller at a grid job boundary.
