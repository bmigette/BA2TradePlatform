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

* **15 pure-option strategy keys** in `_OPTION_STRATS`, all buildable as single-strategy jobs.
* **Two EQUITY-ENTRY overlay keys**, `O_CC` (covered call) and `O_PP` (protective put), absent
  from `_OPTION_STRATS` because they buy stock via the normal equity entry and splice an option
  overlay into the exit rules. They are OPPOSITES: `O_CC` COLLECTS premium and caps upside,
  `O_PP` PAYS premium for downside insurance.
* **The wheel primitive**: a `has_assigned_shares` condition separating assigned stock from stock
  bought outright, tested as a usable rule trigger (`tests/test_wheel_assignment_order.py`, 14
  tests). There is no wheel STRATEGY, but this is what makes one expressible.
* **A complete option backtest engine.** `backtest_account.py` carries ~24 option methods over
  342 option references — chain fetch, quotes, multi-leg combo submit, fills with modelled
  spread/slippage/participation cap, MTM, expiry settlement, assignment, called-away lot
  splitting — plus `options_provider.py`, `fetch_options.py` and a full Black-Scholes
  `option_greeks.py` with IV by inversion. **Early American assignment is NOT modelled**
  (`daily_engine.py:1408`): options resolve at expiry only.
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

**CORRECTION, 2026-08-27, after a 5-agent survey of the actual mechanisms.** An earlier draft of
this section ended "the machinery is essentially complete; what is missing is decided values, two
small wiring pieces, and a driver." That was wrong, and section 7 now carries the real list. The
pieces above genuinely exist, but roughly half of what this grid needs does not — most
importantly cross-job seeding, which the whole stage-1 → stage-2 design rests on. See §7.1.

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

### Stage 0a · Smoke — prove the pipeline, not the strategy

Two structures (one debit, one credit), population 8, 2 generations, 3 symbols, ~3 months, and
**every optional condition gate disabled**. A few hundred trials; minutes.

Disabling the gates is the whole point. The known failure modes are pipeline-shaped — `iv_rank`
and `iv_to_realized_vol` fail CLOSED when IV is unmeasurable, and an options cache without
greeks makes every such individual trade nothing and score the zero-trade sentinel. With the
gates off, "traded nothing" can only mean data or wiring. This is also the artefact handed to
whoever runs the real grid so they can verify their machine before committing days of compute.

**Pass criteria, stated so the run is falsifiable:** at least one structure opens, at least one
closes at or before expiry, the fitness is a finite number rather than the sentinel, results
persist with an `optimization_id`, and every configured worker answers. It must also **report the
sub-universe populations** — how many large-band names sit under \$100 and under \$300 — since
that decides whether the price-constrained structures have anything to trade.

### Stage 0b · Pilot — prove the GRID, on a short window

All 15 structures, gates **on**, real settings, a few months, population 60, 10 generations.
~9,000 trials.

Distinct from 0a and both are worth having, because they fail differently. 0a runs with the gates
OFF, so a failure there can only be data or wiring. 0b runs the real shape, so a failure with 0a
green is configuration or strategy. 0b is also what turns the cost table below from an estimate
into a schedule: it measures per-trial runtime on the machine that will do the work.

### Stage 1 · Discovery — 18 jobs, one per structure

The 15 pure-option keys plus the three wheel-family strategies below.

Genome ~28, **population 200, generations 60 with early-stop patience 8**. ~5,000 trials per job
in practice; 12,000 only if a job never plateaus.

Each job answers "under what conditions did *this* structure work", with every per-structure
condition searched independently. It also answers the cheap question first: whether the
structure trades at all on this data.

All 15 are searchable here, including `O_CSP` / `O_JL` / `O_RS`, which the group filter excludes.
That filter is unconditional and its own comment says the three "remain runnable as EXPLICIT
single-strategy jobs" — stage 1 is exactly that.

#### The wheel family — three more strategies, one of which must be built

| key | entry | overlay | premium | build state |
|---|---|---|---|---|
| `O_CSP` | sell put (pure option) | — | collect | exists |
| `O_CC` | buy stock (equity) | sell call | collect | exists |
| `O_PP` | buy stock (equity) | buy put | **pay** | exists |
| `O_WHEEL` | sell put (pure option) | sell call **on assigned shares** | collect | **to build** |

`O_WHEEL` = `O_CSP`'s entry rule + `O_CC`'s guard/overlay pair, with ONE deliberate change: gate
the overlay on **`has_assigned_shares`**, not `has_position`. `has_position` would write calls
against any stock the expert holds; `has_assigned_shares` writes them only against shares the
wheel actually put you into. That distinction IS the wheel.

**It must use `_insert_option_overlay`, not append.** That helper exists because an appended
overlay sat behind S2's always-matching floor stop and could never fire — and the GA could not
route around it, since that rule declares no toggle gene. The consequence was not subtle: `O_CC`
and `O_PP`, two OPPOSITE strategies, produced byte-identical top-5 results with zero trades
carrying a contract symbol, because both were silently running the same plain long-equity
baseline. Every pre-fix `O_CC`/`O_PP` number is a mislabelled equity run. A wheel that skips the
splice degrades into plain `O_CSP` the same way.

**Read wheel results knowing early assignment is not modelled.** In reality a deep-ITM short put
is often assigned early, especially around dividends, so a backtested wheel turns over more
slowly than a live one and overstates time-in-put. Not a reason to skip it; a reason not to read
its turnover as real.

**Capital binds this family twice.** All four must fund a full strike or 100 shares — spot ≤
\$60 today, ≤ \$100 at the 50% cap. The wheel inherits that at the put AND again at the
assigned lot.

### Stage 2 · Composition — 1 job, the deployable expert

**THERE IS NO SURVIVAL GATE, AND THAT IS DELIBERATE.** An earlier draft advanced only structures
that cleared a trade-count and diversification bar. That was a decomposition bias: a structure
which earns its place only ALONGSIDE another is judged alone in stage 1, dropped, and stage 2
then inherits an exclusion that was never tested in the context where it mattered.

So **all 15 structures enter stage 2 as members regardless of stage-1 performance.** Stage 1's
results are used only as SEEDS — structures that did well start from their stage-1 winner, the
rest start from authored defaults. Stage 1 hands out head starts; it has no power to exclude.

Stage 1's per-structure verdicts are still recorded, because they are the knowledge the grid
exists to produce, and a structure that could not trade at all is a finding worth having. They
just do not gate anything.

All 18 structures as toggleable members of ONE expert, plus the shared tier-1 gate.
Genome **~300, ESTIMATED** — `OS_ALL` does not exist yet, so unlike every other figure in this
spec that number is arithmetic (15 members x ~14 per-structure condition genes + 4 shared + the
option genes + the entry toggles) rather than a measurement. Measure it with
`collect_param_space` as the first step of building it, and re-derive the population from what
comes back. **Seeded from stage 1's winners** via `initial_population`. **Population 300,
generations 80 with early-stop patience 10** — ~8,000 trials in practice.

**Seeding collapses the effective dimensionality, and that is why the population is 300 and not
3,000.** Stage 2 is not a cold search of ~300 genes: roughly 280 of them arrive near-optimal from
stage 1 and need only local refinement. What is genuinely unknown is the ~15 member on/off
toggles and the 4 shared-gate genes — an effective dimensionality nearer 20. Sizing the
population for a cold 300-dimensional search would have been wrong by an order of magnitude.

**Generations are a ceiling, not a cost.** `genetic.py` stops when the best fitness has not
improved for `early_stopping_generations`, so a high generation cap is only paid for while the
search is still improving. Spend on PATIENCE rather than on population: 8–10 rather than the
default 4, because a rugged fitness landscape plateaus and then breaks through, and patience 4
cuts it off mid-plateau.

This stage produces the artefact: one expert that opens the best structure.

### Stage 3 · Generalisation — optional

The stage-2 winner re-run against the `mid` and `small` cap bands it has not seen, and against
a holdout window. This is where the band split earns its keep: it answers whether the
arbitration learned on large caps transfers down the liquidity curve, rather than paying 3x
up front to find out. Expect degradation — thinner chains mean more contracts rejected by
the volume gate — and that degradation is itself the result worth having.

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
| cash-secured put | \$60 | \$30 | \$10 |   ← \$100 once the cap gene reaches 50%
| jade lizard | \$56 | \$26 | \$6 |
| put ratio spread | \$61 | \$31 | \$11 |
| short strangle | \$300 | \$150 | \$50 |
| iron condor | any spot | any spot | any spot |

Three consequences, and the second was not visible before this was measured:

* **The full-notional three need the per-instrument cap raised to 50%.** At today's 30% ceiling
  they top out at spot \$60. A cash-secured put at spot \$100 reserves exactly \$10,000, i.e.
  50% of a \$20k account, so BOTH gene ranges move: `max_virtual_equity_per_instrument_percent`
  5–30% → **5–50%**, and the full-notional `option_sizing` band 5–40% → **5–50%** (the budget is
  the min of the two, so raising one alone does nothing). The consequence is concentration, and
  it is inherent rather than a flaw: at 50% you hold at most TWO such positions, one contract
  each, and an assignment leaves you owning \$10,000 of stock — half the account. **Scope the
  raised range to the option grid**, since the same setting is read by the equity risk manager
  and a global change would move every equity grid. They stay in stage 1 — a measured "this structure is unreachable at
  this account size" is a finding, not a failure — but they run against an explicit
  **spot ≤ \$100 sub-universe**, and their result must be read as conditional on that. Running
  them on the full band would spend 15,000 trials each rediscovering the reserve table.
* **Short straddle and short strangle have a spot ceiling too** (\$300 permissive, \$50
  restrictive). I had treated Reg-T naked structures as affordable everywhere; they are not.
  They run on a **spot ≤ \$300 sub-universe**, which excludes most mega caps.
* **Only defined-risk structures are spot-independent** — iron condor, the credit and debit
  verticals, butterflies. Their reserve is a function of wing width, not of the underlying, so
  they alone can search the full band.

**Universe: the screener cap bands, and stage 1 runs on `large` (≥\$10B) alone.** `small` and
`mid` are deferred to stage 3.

Large is not a compromise, it is the priority, for three reasons:

* **Liquidity is the UNIVERSAL constraint; affordability is a minority one.** Thin option chains
  break all fifteen structures — the `option_min_volume` gate rejects the contract and the fill
  engine's 10%-of-bar-volume participation cap means a too-thin contract yields an order that
  can never fill, only retry. Affordability binds five of fifteen. Choosing the universe to suit
  the minority constraint at the cost of the universal one is backwards.
* **Market cap and share price are independent, and that cuts both ways.** The large band
  contains plenty of cheap-priced names — a \$10B+ company can trade at \$11 or \$800 — so the
  `spot ≤ \$100` sub-universe the full-notional three need is populated INSIDE the large band.
  Dropping to mid caps buys nothing for affordability and costs chain depth.
* **It is the universe every existing option finding was measured against.** The affordability
  filter's own rationale reads "On this large-cap universe at the grid's \$20k capital", and the
  equity experts run the same band. Deviating would make new results incomparable to old ones
  for no gain.

Running all three bands in stage 1 would be 45 jobs and ~675,000 trials instead of 15 and
~225,000; that is the single biggest cost lever in the plan and it is one argument in the driver.

**Stage 0 should report the sub-universe populations** — how many large-band names sit under
\$60 and under \$300 — because that is the number which decides whether the five
price-constrained structures have anything to trade, and it cannot be measured from a machine
without the data.

**Cap band is not share price**, which is why the spot sub-universes above are defined on price
and not on band: the band controls neither affordability nor option-chain depth.

**ETFs need an explicit list**, since they carry no screener market cap. At \$20k they are fine
for defined-risk structures and excluded from the full-notional three and the naked-vol two by
the spot caps above (SPY and QQQ fail every one of them; IWM passes only the \$300 cap).

## 7. What has to be built

1. **`O_WHEEL`** — compose `O_CSP`'s entry with `O_CC`'s overlay pair, gated on `has_assigned_shares` and spliced with `_insert_option_overlay`.
2. **Shared condition ids** — emit `shared-rel_volume` and `shared-gate_confidence` in the group
   builder. Verified to collapse 20 genes to 4 on OS1; no GA change.
2. **An `OS_ALL` group** for stage 2, carrying all 18 structures.
3. **Cross-job seeding** — read stage-1 winners and emit `initial_population` for stage 2. The
   hook exists for resume; this points it at another job's results.
4. **A price-capped universe helper** for the full-notional three.
5. **The driver script** — stages 0 → 1 → 2 as separate, individually runnable commands, so the
   operator can stop after stage 0, or re-run one structure without re-running the grid.
6. **Fix the stale `optimize-batch --fitness` help**, which still says `consistent_annual_return`
   where the code correctly resolves `option_consistent_annual_return`.

## 7.1 What does NOT exist, measured

Everything below was verified against the code, and several items falsify claims made earlier in
this document. They are listed here rather than quietly fixed inline so the size of the work is
visible.

**Cross-job seeding is the biggest build item, and the obvious version is broken.**
`warmStartFromOptimizationId` is a SINGLE int with no plural form and no merge; stage 2 needs 18
winners combined. Worse, `genetic.py:377` fills any gene absent from the seed with
`config['min']`, and an `enabled` gene's range is `(0, 1)` — so encoding a single-structure
winner into the `OS_ALL` space sets **every member toggle to 0** and the seeded individual trades
nothing. Seeding must therefore (a) merge N sources, (b) explicitly set each seeded member's
toggle ON, and (c) leave un-seeded members at their authored defaults rather than at zero.

**The shared-id change collides with seeding.** Renaming `{member}-rel_volume` to
`shared-rel_volume` in the group builder only would make stage-1's `cond:o_lc-rel_volume:*` keys
UNKNOWN in the stage-2 space, and `encode_params` drops unknown keys silently. Either both job
shapes use the shared id, or the seeder translates keys. Decide before implementing either.

**`OS_ALL` would give its debit members CREDIT exit bands.** `_option_exit_rules(kind)` branches
on the GROUP key and `OS_ALL` is not in `_DEBIT_OPTION_KINDS`. Exit condition ids (`tp`, `td`,
`dte`, `sl`) are also unprefixed, so per-member exits are not expressible at all. Registering
`OS_ALL` additionally fails two live tests, one of which asserts that `O_CSP`/`O_JL`/`O_RS` are
in NO group — i.e. it asserts the opposite of the intended change.

**Section 6's universe design is not implementable as written.** `--screener-cap-band` is read
only inside the `if args.screener:` branch, so a pure-option job cannot use a cap band at all.
ETFs are excluded from the screener metric store at BUILD time (`isEtf=false`; SPY, QQQ, IWM,
XLF and TLT are all absent), so they cannot be combined with the gate. No `_OPTION_STRATS` entry
declares a `screener_gate_base`, and `_screener_gate_base_for_strategy` merges member dicts with
`merged.update(...)`, so a single `price_max` collapses to one value for a whole group. No tool
exists to dump a price-capped universe list.

**Stage 0a needs a flag that does not exist.** The condition gates are `toggle_optimize=True`
nodes the GA searches; there is no "all gates off" knob.

**`optimize-batch` cannot drive this grid.** No warm-start argument, no per-job name, and it
never calls `_screener_gate_opt_block`. It also polls forever when `ba2-test serve` is not
running — `queue_task` writes `"queued"` and the break set has no timeout. The driver should loop
`optimize` sequentially instead.

**Elitism is hardcoded at 0.1%** (`elitismPercent` 0.1, `genetic.py:647` takes
`max(1, int(0.001 * len(population)))`), i.e. exactly ONE elite individual at any population
below 1000, with no CLI flag. At the populations in §8 that is effectively no elitism.

**Four existing tests must be rewritten in the same commits as the changes they guard**, two of
which assert the current behaviour as intent: `test_launcher_volume_vol_genes.py` asserts
`rel_volume` is searched PER MEMBER, and `test_option_strategy_builders.py` asserts the
full-notional three belong to no group.

## 8. Cost

| stage | jobs | population | generations (ceiling) | early-stop | trials if full | realistic |
|---|---|---|---|---|---|---|
| 0a smoke | 2 | 8 | 2 | — | ~300 | ~300 |
| 0b pilot | 18 | 60 | 10 | 4 | ~11,000 | ~11,000 |
| 1 discovery | 18 | 200 | 60 | 8 | 216,000 | ~90,000 |
| 2 composition | 1 | 300 | 80 | 10 | 24,000 | ~8,000 |
| **total** | | | | | **~251,000** | **~109,000** |

"Realistic" assumes early-stop fires around generation 25; "if full" is the ceiling where nothing
ever plateaus. The true figure lands between, and stage 0b measures which end.

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
