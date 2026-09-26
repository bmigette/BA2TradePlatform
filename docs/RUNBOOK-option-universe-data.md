# Runbook: preparing an option universe's data

This runbook covers the data behind an option grid: FMP daily bars on one split basis, split and
dividend calendars, the ThetaData option tree under the right roots, DeterministicScorer inputs,
the screener gate store, both market-condition manifests, and a preflight that replays the real
option basis guard. It is the stage-2 repair of 2026-09-25/26 (753 names) turned into one
committed command. The measurements behind each step are in the commit messages of branch
`data/stage2-universe-repair` (`data(stage2)`, `fix(split-basis)`, `fix(thetadata)`,
`fix(option-guard)`).

Run from the repo root with the test platform's interpreter, which has `thetadata` and the
backend installed. `BA2_HOME` selects the data tree.

```bash
PY=~/ba2-venvs/test/Scripts/python.exe
$PY tools/prepare_option_universe.py --universe-file my_universe.txt \
    --start 2020-01-01 --end 2025-12-31 --report-dir reports/prep-$(date +%F) \
    [--db <platform sqlite holding the ThetaData key>]
```

- `--end` is the last grid decision session. It must be a completed session, because it is the
  manifests' end.
- `--theta-end` sets the ThetaData window end. It defaults to today.
- `--dry-run` prints every command without running any of them.
- `--steps a,b` runs a subset of the steps.
- `--from-step X` resumes at step X after a stop. Every step is idempotent and resumable.

## The steps

| # | Step | Tool | Automatic | Stops when |
|---|---|---|---|---|
| 1 | `split-check` | `warm_market_conditions.py plan --profile ohlcv-v1` | Fetches the split calendars (disk-cached for a day) and checks each FMP file's split basis | A configuration error (exit 2). A refused name is not a stop here; step 3 handles it. |
| 2 | `dividends` | `warm_dividend_calendars.py` | Fetches the FMP dividend calendars for the guard's carry separation | Any symbol failed |
| 3 | `fmp-repair` | `warm_market_conditions.py repair --plan <step 1 plan>` | Force-refetches the full history of every refused name, tops up missing coverage, then re-checks the split basis. Builds nothing. | A name still refuses, a fetch failed, or a split calendar is unreadable. It also refuses while the US session is open (see below). |
| 4 | `thetadata` | `warm_options_history.py --provider thetadata --wide` | Fetches missing and unfinished partitions, plus every partition that a `ROOT_HISTORY` alias or None root makes stale, logged with the reason. An incomplete tree (the XYZ case) is refetched in one pass. | A unit failed. Re-run; the tool resumes. |
| 5 | `ds-prewarm` | `ba2test_launcher.py prewarm --experts DeterministicScorer` | Warms the six `fmp_history` namespaces DS reads | Non-zero exit |
| 6 | `gate-check` | `option_universe_preflight.py --sections gate` | Reports screener metric-store rows per name. Names that are absent are review findings. | The store is missing |
| 7 | `manifests` | `warm_market_conditions.py plan` + `build --cache-only`, for `ohlcv-v1` and `ta-structure-v1` | Builds from the repaired files and records the digests in the report | A plan is not buildable cache-only, or a build fails |
| 8 | `preflight` | `option_universe_preflight.py` | Checks coverage, split basis, the real guard replay, DS inputs and the gate store, and writes `preflight.json` | Any blocking finding |

Each step's stdout and stderr go to `<report-dir>/NN-<step>.out` and `.err`. The report
directory also holds the plans, `thetadata.log`, `gate.json` and `preflight.json`.
`prepare_report.json` summarizes the run: per step, the command, the exit code, the elapsed time
and the parsed result, plus the manifest digests and, when the run stopped, why.

### What is automatic and must not be bypassed

The following are code. They apply whenever data is read or fetched, so no step repeats them:

- **Split-basis overrides** (`ba2_common.core.split_basis_overrides`). They are used by the
  split-basis check, the backtest's run split basis and the preflight.
- **`ROOT_HISTORY`** (`ba2_providers.options.thetadata`). The provider splits every request at
  each root change. Every partition the ThetaData warm writes records the symbol's root-history
  signature. A partition fetched under another history is stale and gets refetched.
- **The option basis guard and its carry separation.** This covers every backtest, the
  preflight replay and the contract-detail view.

Do not hand-edit cache files, swap partitions or write markers. Each one would skip a check that
the tools above apply.

### The US-session rule (step 3)

A full refetch during the US regular session stores that day's partial bar. The repair
therefore refuses while the session is open. Run it after the close, or pass
`--allow-open-session` and run `--from-step fmp-repair` again after the close. During stage 2
this was the pass A / pass B procedure.

## Decisions that need a human

The pipeline stops at each of these. It never guesses.

1. **A name is still refused after its refetch** (step 3 `still_refused`, or preflight `split`).
   - The vendor's history ends a few bars before the cache (SATS in stage 2: step 3's
     `fetch_errors` says "LESS history"). Re-run step 3 with `--keep-newer-cached-tail`. The
     provider keeps at most 5 cached bars, and only if the split calendar can be read, no split
     event falls among those bars, and the last 60 shared sessions agree within
     `ANCHOR_TOLERANCE`. Otherwise it refuses and leaves the file byte-identical. The keep is
     logged.
   - A real basis step or a corporate action the calendar gets wrong. Add an override in
     `packages/common/ba2_common/core/split_basis_overrides.py` (`add_price_adjustment`,
     `exclude_calendar_event`, `acknowledge_jump`, or a replacement, which is an exclude plus an
     add on the same date). Include its anchors, which are (date, FMP close) pairs, and bump
     `OVERRIDES_VERSION`. Measure add ratios with `tools/measure_basis_plateaus.py SYMBOL
     cut[,cut]`, which uses the plateau convention: medians over at most 60 sessions,
     fixed-ratio actions at their nominal ratio. Pin each new entry with a test next to the
     existing override tests. Then run `--from-step split-check`.
   - Or exclude the name: drop it from the universe file, or pass `--allow-exclusions` to
     publish the manifests with the exclusion recorded.
2. **The preflight's guard refuses a name** (`guard_refused`), or coverage shows the chain
   starting at a rename. Check whether the ticker changed hands or roots. If it did, add a
   `ROOT_HISTORY` entry in `packages/providers/ba2_providers/options/thetadata.py`: a
   `(cutover, old_root)` tuple, or `(listing_date, None)` when the earlier chain belongs to
   another company. Include the measurement in the comment and a test in
   `packages/providers/tests/test_thetadata_root_history_stage2.py`. Import-time validation
   rejects a malformed entry. Nothing else needs editing: `ROOT_HISTORY_LEGACY_FETCHED` is
   frozen, and step 4 refetches what the entry changes. Then run `--from-step thetadata`. If no
   root explains the refusal, exclude the name. In stage 2 these were QS, VFS, BULL, JOBY, BSBR
   and OWL.
3. **Review findings** (in `preflight.json`, severity `review`). These are:
   - A first option bar more than 30 days after both the window start and the stock's own first
     FMP bar. This is either a vendor gap (the accepted early-2020 one for LAMR…VRT, LIN, LLY,
     MA and MCD), a SPAC whose FMP history predates its merger, or a chain that traded under an
     earlier ticker. The last case is a `ROOT_HISTORY` candidate (see 2).
   - No option bar in the window at all.
   - A DS file FMP does not have.
   - A name absent from the gate store.

   Accept the finding or exclude the name. Blocking DS is at or above
   `fmp_common._HERMETIC_MISS_LIMIT` distinct missing files, the point at which a hermetic run
   aborts.

Record every decision where the next person will find it: the override's or entry's comment,
plus the run's report directory.

## Costs measured in stage 2 (753 names, local box)

- **Split calendars and dividend calendars:** one FMP request per symbol each, a few MB in
  total.
- **FMP forced refetch:** one request per refused name, about 1.3 MB each. There were 94
  refetches, about 120 MB.
- **ThetaData, per alias symbol group:** roughly 1.7k to 3.7k requests and 20 to 35 min. XYZ's
  full tree took 1,851 requests and 55 min. Peak RSS stayed under 0.4 GB.
- **Manifests:** about 45 to 70 min for both profiles together on a cold build (cache-only, concurrency 1). Rebuilds reuse published rows.
- **Preflight:** 28 min for 753 names at `--procs 2` (2 starts x 5 phases), with a peak of
  0.56 GB. Memory is bounded by `--procs`, and each worker holds one symbol's option arrays.

## Not part of this pipeline

- Syncing the prepared tree to grid hosts.
- `prepare-host` on each host (`warm_market_conditions.py prepare-host --manifest <digest>`).
- The early-2020 ThetaData vendor gap, which is accepted (see the comment in `thetadata.py`).
  Do not refetch it.
