# Option GA Grid — Design

**Date:** 2026-08-27
**Status:** Approved design, not yet implemented
**Deliverable:** a runnable script, not a run. The target machine holds the option data; this
repo holds the definition of what to run.

---

## 1. Goal

One expert that opens the **best** option structure for the market conditions in front of it,
with the conditions per structure discovered by genetic search rather than authored.

Two distinct outputs, and conflating them is the main design risk:

* **Knowledge** — for each structure, the market conditions under which it worked.
* **An artefact** — a single deployable expert that arbitrates between structures.

They need different jobs. The first wants a small genome searched hard, per structure. The
second wants one large genome seeded from the first.

## 2. What already exists

Verified against the code on 2026-08-27, not assumed:

* **15 option strategy keys** in `_OPTION_STRATS`, all buildable as single-strategy jobs.
* **4 family groups** `OS1`–`OS4` with toggleable members (`_OPTION_GROUPS`).
* **Multi-job fan-out** — `optimize-batch` runs one GA job per strategy over a shared universe,
  each tagged with its own `optimization_id`.
* **8 option gene families** — dte, sizing, strike_method, strike_param, strike_delta,
  wing_width, min_arc, delta.
* **An option-specific fitness** — `option_consistent_annual_return`, correctly resolved for
  pure-option kinds by `_resolve_fitness`.
* **Distributed execution** — `worker`, `sync-cache`, `distributed_eval`.
* **Population seeding** — `genetic.py` accepts `initial_population` (built for resume).
* **Screener cap bands** — small \$50M–\$2B, mid \$2B–\$10B, large ≥\$10B.

The machinery is essentially complete. What is missing is decided values, two small wiring
pieces, and a driver.

## 3. The measurement that shapes everything

Genome sizes, measured with `collect_param_space`:

| job shape | genome | option genes | population for 10× | current default |
|---|---|---|---|---|
| OS1 family (5 members) | **120** | 24 (20%) | 1200 | **40** |
| OS2 family (3) | 74 | 12 | 740 | 40 |
| OS3 family (2) | 56 | 12 | 560 | 40 |
| OS4 family (2) | 47 | 5 | 470 | 40 |
| **single structure** | **25–31 (median 28)** | 2–6 | ~300 | 40 |

Two things follow.

**The default population of 40 is smaller than every family genome.** Against OS1's 120 genes
that is closer to random sampling than to a genetic search.

**A family job spends 80% of its genome on entry conditions.** OS1's 120 genes are 53
condition on/off toggles, 43 condition values, and only 24 option genes. A grid meant to find a
good *option* strategy would spend most of its compute deciding which technical conditions gate
the entry.

A single-structure job is four times smaller and answers the knowledge question directly. That
is why discovery is per structure and composition is a separate, seeded stage.

## 4. Condition tiers

The 9 conditions (`gate_confidence`, `iv_rank`, `iv_to_realized_vol`, four `price_*` channel
gates, `rel_volume`, `signal`) are currently replicated per member at ~17 genes each. They split
on semantics, not on convenience:

| condition | scope | why |
|---|---|---|
| `rel_volume` | **shared** | the builder documents it as identical for both halves — *"the searched threshold is the only per-half difference, and there is none"*. Pure duplication today. |
| `gate_confidence` | **shared** | expert conviction in the symbol; structure-independent. |
| `iv_rank` | **per structure** | operator flips: debit members use `<` (buy cheap vol), credit members `>` (sell rich vol). The GA never searches operators, so a shared gate is **not expressible** across a mixed family. |
| `iv_to_realized_vol` | **per structure** | same operator flip. |
| `signal` | **per structure** | bullish vs bearish is the discriminator. |
| `price_*` × 4 | **per structure** | directional by construction. |

**Sharing is implemented by giving the condition the same `id` across member rules.** Verified:
rewriting `{member}-rel_volume` → `shared-rel_volume` and the same for `gate_confidence`
collapses 20 genes to 4 on OS1 (120 → 104) with no GA change, because the gene key derives from
the condition id.

Only those two share. Everything else stays per structure, because the per-structure condition
set **is** the knowledge the grid exists to produce.

## 5. Stages

### Stage 0 · Smoke — prove the pipeline, not the strategy

Two structures (one debit, one credit), population 8, 2 generations, 3 symbols, ~3 months, and
**every optional condition gate disabled**. A few hundred trials; minutes.

Disabling the gates is the whole point. The known failure modes are pipeline-shaped — `iv_rank`
and `iv_to_realized_vol` fail CLOSED when IV is unmeasurable, and an options cache without
greeks makes every such individual trade nothing and score the zero-trade sentinel. With the
gates off, "traded nothing" can only mean data or wiring. This is also the artefact handed to
whoever runs the real grid so they can verify their machine before committing days of compute.

**Pass criteria, stated so the run is falsifiable:** at least one structure opens, at least one
closes at or before expiry, the fitness is a finite number rather than the sentinel, results
persist with an `optimization_id`, and every configured worker answers.

### Stage 1 · Discovery — 15 jobs, one per structure

Genome ~28, population 500 (18×), 30 generations, ≈15,000 trials per job.

Each job answers "under what conditions did *this* structure work", with every per-structure
condition searched independently. It also answers the cheap question first: whether the
structure trades at all on this data.

All 15 are searchable here, including `O_CSP` / `O_JL` / `O_RS`, which the group filter excludes.
That filter is unconditional and its own comment says the three "remain runnable as EXPLICIT
single-strategy jobs" — stage 1 is exactly that.

### Stage 2 · Composition — 1 job, the deployable expert

**Survival criterion, stated so stage 1 is decidable rather than a judgement call.** A structure
advances to stage 2 when its best stage-1 individual: opened at least 20 trades, scored a finite
fitness rather than the zero-trade sentinel, and did not achieve its result on a single symbol
(no more than 50% of its trades on one underlying). The first two separate "this structure does
not work" from "this structure could not run"; the third rejects the single-symbol artefact that
a 15,000-trial search will otherwise find. A structure that fails only the trade-count test is
recorded as UNDETERMINED, not as rejected — the distinction matters when the cause is a data gap.

Every surviving structure as a toggleable member of ONE expert, plus the shared tier-1 gate.
Genome **~300, ESTIMATED** — `OS_ALL` does not exist yet, so unlike every other figure in this
spec that number is arithmetic (15 members x ~14 per-structure condition genes + 4 shared + the
option genes + the entry toggles) rather than a measurement. Measure it with
`collect_param_space` as the first step of building it, and re-derive the population from what
comes back. **Seeded from stage 1's winners** via `initial_population`. Population 2000, 40
generations, ≈80,000 trials.

Seeding is what makes this tractable. A 290-dimensional search from a random start would need a
population in the thousands merely to cover the space once; starting from 15 independently
optimised structures, the GA begins near a solution and mostly learns arbitration — which
members to enable and how they interact.

This stage produces the artefact: one expert that opens the best structure.

### Stage 3 · Generalisation — optional

The stage-2 winner re-run against the `small` and `large` cap bands it has not seen, and against
a holdout window. This is where the band split earns its keep: it answers whether the arbitration
learned on mid caps transfers, rather than paying 3x up front to find out.

## 6. Capital and universe

**Capital: \$20,000 account balance.** `virtual_equity_pct` defaults to 100 in backtests, so
account balance, virtual equity and the expert's sizing base are the same number.

**But the account balance is NOT the sizing budget, and this is where the existing filter is
misleading.** Option sizing is
`budget = equity x min(option_sizing%, max_virtual_equity_per_instrument_percent%)`. Both are
GENES: `option_sizing` searches 5–40% for the full-notional band, and the per-instrument cap
searches 5–30%. So the per-instrument budget is **\$1,000 at the floor of both and \$6,000 at
the ceiling of both** — never \$20,000. The affordability filter's rationale compares
per-contract reserve against the whole account and therefore understates the constraint by up
to 3.3x.

Measured against `option_reserve_required`, the highest spot at which ONE contract fits:

| structure | budget \$6,000 (both genes at ceiling) | \$3,000 (mid) | \$1,000 (both at floor) |
|---|---|---|---|
| cash-secured put | \$60 | \$30 | \$10 |
| jade lizard | \$56 | \$26 | \$6 |
| put ratio spread | \$61 | \$31 | \$11 |
| short strangle | \$300 | \$150 | \$50 |
| iron condor | any spot | any spot | any spot |

Three consequences, and the second was not visible before this was measured:

* **The full-notional three are cheap-underlying-only at \$20k.** Spot ≤ \$60 at best, ≤ \$30
  at typical gene settings. They stay in stage 1 — a measured "this structure is unreachable at
  this account size" is a finding, not a failure — but they run against an explicit
  **spot ≤ \$60 sub-universe**, and their result must be read as conditional on that. Running
  them on the full band would spend 15,000 trials each rediscovering the reserve table.
* **Short straddle and short strangle have a spot ceiling too** (\$300 permissive, \$50
  restrictive). I had treated Reg-T naked structures as affordable everywhere; they are not.
  They run on a **spot ≤ \$300 sub-universe**, which excludes most mega caps.
* **Only defined-risk structures are spot-independent** — iron condor, the credit and debit
  verticals, butterflies. Their reserve is a function of wing width, not of the underlying, so
  they alone can search the full band.

**Universe: the screener cap bands** — but stage 1 runs on **one band only, `mid`
(\$2B–\$10B)**, with `small` and `large` deferred to stage 3.

This is a decision, not an oversight, and it is the single biggest cost lever in the plan.
Running all three bands in stage 1 would make it 45 jobs and ~675,000 trials instead of 15 and
~225,000. `mid` is the right one to start with for two independent reasons: small caps
(\$50M–\$2B) largely have thin or nonexistent option chains, so the liquidity floor and the
10%-of-bar-volume fill cap would dominate the result rather than the strategy; and mega caps
carry share prices that put five of the fifteen structures out of reach. Flip this to all three
bands if the compute is available — it is one argument in the driver.

**Cap band is not share price**, which is why the spot sub-universes above are defined on price
and not on band: a \$10B company can trade at \$30 or \$800, so the band controls neither
affordability nor option-chain depth.

**ETFs need an explicit list**, since they carry no screener market cap. At \$20k they are fine
for defined-risk structures and excluded from the full-notional three and the naked-vol two by
the spot caps above (SPY and QQQ fail every one of them; IWM passes only the \$300 cap).

## 7. What has to be built

1. **Shared condition ids** — emit `shared-rel_volume` and `shared-gate_confidence` in the group
   builder. Verified to collapse 20 genes to 4 on OS1; no GA change.
2. **An `OS_ALL` group** for stage 2, carrying every structure that survived stage 1.
3. **Cross-job seeding** — read stage-1 winners and emit `initial_population` for stage 2. The
   hook exists for resume; this points it at another job's results.
4. **A price-capped universe helper** for the full-notional three.
5. **The driver script** — stages 0 → 1 → 2 as separate, individually runnable commands, so the
   operator can stop after stage 0, or re-run one structure without re-running the grid.
6. **Fix the stale `optimize-batch --fitness` help**, which still says `consistent_annual_return`
   where the code correctly resolves `option_consistent_annual_return`.

## 8. Cost

| stage | jobs | population | generations | trials |
|---|---|---|---|---|
| 0 smoke | 2 | 8 | 2 | ~300 |
| 1 discovery | 15 | 500 | 30 | ~225,000 |
| 2 composition | 1 | 2000 | 40 | ~80,000 |
| **total** | | | | **~305,000** |

Per-trial runtime on the target machine is the number that decides whether this is a weekend or
a fortnight, and it cannot be measured from here. Stage 0 measures it as a side effect, which is
a second reason to run it first: it converts this table from an estimate into a schedule.

## 9. Out of scope, and why

* **The option data pipeline.** The target machine has the data. This repo's own backtest reads
  an Alpaca-only cache with no history before 2024-02-01 and no greeks, so the grid could not
  run here regardless. Wiring the TastyTrade parquet store into `HistoricalOptionsProvider` is
  separate work.
* **The option risk manager** (phases 1–5, in progress). It changes what `option_sizing` MEANS:
  today it is a structure's share of equity, after Phase 3 it is that structure's cap within a
  shared per-instrument budget. **Grids run either side of Phase 3 are not comparable**, and the
  script must record which side it ran on.
* **The new gene set** (`option_term`, the delta box, the selection-policy weights). Those
  arrive with Phase 5. This grid searches the genes that exist today.
