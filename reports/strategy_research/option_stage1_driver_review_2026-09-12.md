# Option grid stage 1: discovery review

Reviewed 2026-09-12 against the current checkout, the August 27 grid design,
August 30 review/state notes, later unbounded-risk exclusion, and September 3
goal2020 runbook amendment. Code changes are local and uncommitted. No grid,
warmup, remote worker job or production action was launched.

**Verdict:** the decomposition is appropriate: discover conditions per structure
under two independent signal experts, then compose later. The wrapper and driver
had drifted from later decisions. I corrected the launch definition and result
handling. This does not certify data readiness or establish that a winning
strategy is profitable out of sample.

## Findings and adjustments

### 1. The old wrapper requested two explicitly prohibited search arms

`stage1_run.sh` still contained the original 18 structures × 2 experts. The
launcher now refuses `O_SSTG` and `O_SSTD` under the August 31 decision to exclude
truly unbounded payoff structures. Their builders remain available for tests;
successfully building a strategy does not mean `optimize` permits it.

The old matrix driver continued after their nonzero exits and eventually returned
success, so a nominal 36-job discovery could quietly be incomplete.

**Fixed:** the discovery profile contains **16 permitted structures × 2 experts
= 32 jobs**. Explicit requests for either excluded structure fail immediately.
Jade lizard, put ratio spread, cash-secured put, wheel and the stock overlays
remain included. This respects the later risk decision without inventing a
performance survival gate. Every permitted structure remains a potential
composition member regardless of its stage-1 score.

### 2. The search budget was reduced without evidence of equivalent discovery

The wrapper defaulted to population 140 while its own comment said equivalence
to the approved population 200 had not been measured. The lower setting had
become the production launch default before the promised stability pilot.

**Fixed:** restore **200 population / 60 generations / patience 8**. `POP=140`
and explicit CLI overrides still support a separate pilot and print a warning.
Elitism remains the already-fixed **10%**, not the obsolete 0.1% mentioned in old
reviews. The driver forwards it explicitly, along with the random seed.

The real joint genomes are larger than the old design's ~22-strategy-gene figure:

| Expert | Most permitted singles | Wheel | Components outside strategy rules |
|---|---:|---:|---|
| FMPRating | 47–53 genes | 60 | 15 model/RM + 7 schedule genes |
| DeterministicScorer | 50–56 genes | 63 | 18 model/RM + 7 schedule genes |

Measured by building every strategy through the real launcher, merging its
expert/RM parameters, and calling `collect_param_space` with schedule genes.
Detailed counts, resolved fitness, price caps and assignment behavior are in
[the inventory](option_stage1_genome_2026-09-12.json).

Population 200 is the approved baseline, not proof of convergence. Before
spending on the full matrix, compare several fixed seeds on the same pilot
window and inspect top-N/condition stability as well as fitness. The new `--seed`
passthrough and separate job identities make that comparison possible. No such
pilot was run in this review, and no wall-time prediction is claimed.

### 3. Name-only completion/checkpoint identity could confuse experiments

Previously, changing dates, capital, seed or population while retaining `-st1`
could skip an old completed optimization. The backend also uses job name to find
checkpoints, so a stable name is insufficient to identify a changed experiment.

**Fixed for discovery:** append a deterministic configuration digest to each job
name. It includes the actual expert/structure, universe ordering, dates, search
and fitness knobs, capital policy, launcher path and selected store environment.
Changing only worker count or selecting a smaller job subset preserves identity
so the same experiment can resume. Old `-st1` records remain untouched and do
not silently count as the new profile's results.

This is configuration identity, not a hash of every historical bar or source
module. After replacing cache contents or code at unchanged paths, supply a
fresh `--name-suffix`. The generic `matrix` profile retains its historical
name-based behavior; it also needs fresh names for changed experiments.

### 4. Failure status, dry-run and holdout checks needed tightening

**Fixed:** a failed child job stops the driver with a nonzero exit. Dry-run now
prints all resolved commands, not just job names. Reading completion status uses
SQLite read-only mode, never creates a missing DB, and does not treat a corrupt
or unreadable DB as an empty history. The wrapper also stops on setup failures.

The grid driver rejects a search reaching 2026 for **all** its arms, including
`O_CC`/`O_PP` and the optional stock control. Previously the backend's deliberately
pure-option-only holdout rail did not cover those equity-entry shapes. This
change is scoped to the option grid driver; unrelated equity backtests and the
backend holdout contract are unchanged.

Dry-run validates arguments and renders commands. It explicitly does **not**
certify option-chain, OHLCV, FMP-history or metric-store coverage.

### 5. The later goal2020 window is still not supported as an explicit store choice

The runbook says to move options to 2020–2025 on ThetaData after provider parity.
The wrapper still uses 2023–2025 and `BACKTEST_OPTIONS_STORE=parquet`.

The historical-data package has a ThetaData fetch provider. However, the backend
store selector still exposes only `sqlite -> alpaca` and `parquet -> tastytrade`.
It does not identify a ThetaData-backed run as ThetaData for vendor-floor checks.
Changing the start date or renaming the Parquet folder does not implement that
missing serving-vendor distinction or prove the cache spans 2020.

**Reported, not concealed by changing a default:** retain the old window as a
limited-history experiment and print that limitation. Full goal2020 readiness
still requires explicit store/vendor wiring, reader/Greeks/cache parity checks,
and measured window/chain coverage on the serving machine. Do not override a
vendor's history floor merely to get the launcher to accept the date.

### 6. Capital and ranking policy must remain explicit

Kept **$20,000 initial capital**, option CAR, and the existing 2000% per-bet /
25% profit-share adjustments. Overlays already resolve to option CAR; the old
review claiming they use Sharpe is stale. All current permitted single jobs
were checked against the real fitness resolver.

The original stage-1 wrapper did **not** set an equity cap: its account compounds.
Some later option-grid documents discuss capped-equity experiments. I added
`--equity-cap` passthrough so that comparison can be explicit, but did not silently
change the established stage-1 capital policy. A capped and an uncapped run get
different discovery identities.

The current option CAR hard floor is **12 structures/year**, with full frequency
credit at 30/year. That is consistent with the documented recurring-return
objective, but it means stage 1 does not exhaustively discover rare profitable
episodes. A low-frequency sentinel is not proof a structure has no use alongside
another. Preserve the no-survival-gate rule and keep low-trade/zero-trade findings
separate from bad economics. A different discovery fitness would be a separately
versioned experiment; this review does not change fitness or old results.

## What already looks right

- One structure per discovery job avoids family toggles hiding which structure
  actually earned the return. The two signal experts provide a useful check that
  a result is not specific to one signal model.
- The generated rules use the shared expected-profit gate rather than requiring
  FMPRating-only target-range fields. Its thresholds/toggles are searchable.
- Per-structure price caps remain $100 for CSP/JL/RS and the inheriting wheel;
  other permitted singles have no extra spot cap from this gate. The discovery
  profile requires a metric store and disables the blanket $100 cap. Actual
  sizing, assignment-capacity and volume checks still apply.
- Only the wheel enables `hold_assigned_stock`; its rule management retains the
  assigned shares. That flag was checked for every permitted job.
- 2026 remains reserved for validation. Stage 1 supplies observations and seeds;
  it is not itself the final deployable multi-structure expert. The complete
  mixed composition/multi-source seeding design remains separate work.

## Usage and remaining readiness work

On the configured Linux stage-1 host, `tools/stage1_run.sh` now selects discovery.
It retains its host-specific paths and two local consumers. Do not edit/restart
the remote running wrapper or merge into an importing grid mid-job; deploy at
the documented job boundary. No remote running state or target-cache coverage
was established in this local source review.

Portable command inspection from the repository, using the test environment:

```bash
python tools/run_options_matrix.py --profile discovery \
  --launcher testplatform/ba2test_launcher.py \
  --screener-gate-store /path/to/metric_store --max-stock-price 0 \
  --name-suffix=-st1-review20260912 --dry-run
```

Use `--strategies O_LC,O_IC --experts FMPRating --seed 7` for a selected pilot;
explicit shorter dates/budgets produce separate identities. A successful dry-run
is not permission to skip smoke/pilot coverage checks. Do not restart a full
goal2020 matrix until finding 5 is resolved.

Before drawing discovery conclusions, establish option-contract depth across
the window, causal Greeks/IV availability, underlying/FMP/metric-store coverage,
and the affordable sub-universe. A static cached large-cap list does not by
itself certify historical large-cap membership or eliminate survivorship bias.
Separate missing-data/refusal outcomes from genuine no-signal/poor-performance
outcomes. The driver intentionally makes no claim to have solved those data
questions on the remote machine.

## Validation

Focused suites cover driver commands/identity/failures, profit-cap passthrough,
holdout rails, real option builders/genes, and per-strategy fitness resolution.
**360 tests passed** in the final combined run (two dependency deprecation
warnings). Bash syntax (`bash -n tools/stage1_run.sh`) and Git whitespace checks
also passed. No live broker or provider calls were used. The full backend suite
was not run: these changes affect launch orchestration and its tests, not the
backtest engine or fitness implementation.

Changed implementation: `tools/run_options_matrix.py`, `tools/stage1_run.sh`.
Tests: new `test_option_discovery_driver.py` and the existing profit-cap source
inspection updated for the parser/command helper extraction. Engine, expert
math, payoff, reserve/sizing and fitness implementations are unchanged.
