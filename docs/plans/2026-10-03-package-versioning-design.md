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

`unsyncable_reason` now also WARNS (it never blocks a run) if `required_package_versions.py` or a package
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
`GA_NEUTRAL_GLOBS` in a REVIEWED change (rule 3: `--allow-neutral-change`; in CI the
`ga-neutral-reviewed` PR label or a `GA-Neutral-Reviewed: <reason>` commit trailer in a pushed commit;
the base's list judges the same diff) and justify it in the commit message (the code must be
unreachable from the GA/backtest path). Never list a module the backtest engine imports.

### What to bump when

| Change | Bump |
|---|---|
| `ba2_trade_platform/` only | `APP_VERSION` |
| `testplatform/` | `TEST_APP_VERSION`. EXEMPT: edits to `required_package_versions.py` / `ga_neutral_package_paths.py` alone |
| shipped `packages/<dir>/<pkg>/` code | that package's `PACKAGE_VERSION` (+ pyproject), always |
| ... that can affect GA results | also set its entry in `required_package_versions.py` EQUAL to the package version at the LAST GA-relevant change in the range; no TEST bump needed |
| ... that cannot | add the path to `ga_neutral_package_paths.py` in a reviewed change (see rule 3) |

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

## Review amendments (Opus review of e3876bde)

Final rule, as documented in CLAUDE.md "Versioning":

1. **Worker AHEAD of the master is excluded, not tolerated.** A worker whose reported
   `required_package_versions[pkg]` is above the master's declares GA-relevant code the master lacks;
   `/update` cannot downgrade it, so `ensure_synced` returns False WITHOUT calling `/update`, with a
   `WARNING ... NEWER than the master ...` (once per job via `drift_seen`). The re-admission recheck
   re-evaluates it each cycle, so it returns as soon as the master catches up. A worker that only has
   a higher package VERSION with an equal minimum is allowed drift (neutral changes), as before.
2. **The master refuses distributed mode when it cannot enforce minimums.** Every entry
   (`strategy_optimization_handler`, `ba2test_launcher`, `api/workers.py`, `tools/rerun_dev_deployed_on_worker.py`)
   calls `self_update.master_sync_policy()`, which raises `PackageGatingError` unless every package has
   a parseable own version and minimum (missing, empty, conflict-marked or inconsistent files all fail).
   `ensure_synced` itself raises on an empty minimum mapping instead of treating it as "gating off".
3. **No TEST bump for a minimum raise.** The raised minimum itself makes older workers sync.
   `required_package_versions.py` and `ga_neutral_package_paths.py` edits are exempt from the
   `testplatform/` -> bump-TEST rule. A `TEST_APP_VERSION` bump still satisfies guard rule 2 (it
   re-syncs every worker anyway).
4. Guard rule 2: a raised minimum must EQUAL the new `PACKAGE_VERSION`.
5. An explicitly named base (`--base`, `$BA2_VERSION_CHECK_BASE`) that does not resolve exits 1; only
   automatic candidates are skipped softly.
6. CI: job `package-version-guard` (PRs: opened/synchronize/reopened/labeled/unlabeled; a force-pushed `before` that cannot be fetched falls back to `origin/<ref>`, then the automatic candidates) (fetch-depth 0, base = `origin/<PR base>` or the push's `before`
   sha) runs the guard; the two new test files also run in the `parity` job. Local `pytest` runs them too.
7. Neutrality is judged with the BASE's allowlist. Editing `ga_neutral_package_paths.py` is rule 3 and
   needs `--allow-neutral-change` (CI: PR label `ga-neutral-reviewed`). If the base has no allowlist yet
   (first introduction) the head's list is used.
8. `unsyncable_reason` WARNS; docs say so.
9. Docstring/comment-only edits to a `.py` file (ast equal after stripping docstrings) are neutral for
   rule 2 but still need the package bump (rule 1). Deleted/renamed files are changed files.
10. Version parsing is strict: month 01-12, no surrounding whitespace/newline. Unhashable package
    values from a worker never raise. A worker with no `app_version` is still accepted (pre-existing)
    but now with a WARNING. A test imports the backtest engine in a clean interpreter and asserts no
    GA-neutral module is loaded, keeping the allowlist honest.

### Boundary checklist (when shipping at a grid job boundary)

1. Merge `feat/package-versions` into dev; run `python tools/check_package_versions.py --base origin/dev`.
2. Bump `TEST_APP_VERSION` once (this change touches `testplatform/`): every worker re-syncs once, and
   old workers (no `package_versions`) are synced loudly one time. Do it only between jobs.
3. Push; confirm the `package-version-guard` and `parity` CI jobs are green.
4. After workers report, check `GET /version` on one worker shows `package_versions` equal to the master's.
5. From then on: package change -> bump that `PACKAGE_VERSION`; GA-relevant -> minimum EQUAL to it;
   neutral -> allowlist (reviewed). Never touch `TEST_APP_VERSION` for a package change mid-run.

Additions (second review):

6. Every master that shares the workers moves to the new commit together, between jobs: the main
   clone, the remote150 isolated-worktree lanes, remote227 if shared. Pull and RESTART long-lived
   `ba2-test serve` masters.
7. Push before the next job and confirm `unsyncable_reason` is silent.
8. Check EVERY worker's `GET /version`, including `required_package_versions`.
9. Roll back by reverting FORWARD with a higher `TEST_APP_VERSION`, never `git reset`: a worker newer
   than the master is excluded, not downgraded (the WARN names both commits and the git commands).
10. Never push a minimum raise to dev while any master is mid-job.

Guard note: the minimum must equal the package version at the LAST GA-relevant commit of the range
(evaluated per commit; a later neutral/docstring bump does not invalidate it), and this equality is
required even when `TEST_APP_VERSION` is also bumped.
