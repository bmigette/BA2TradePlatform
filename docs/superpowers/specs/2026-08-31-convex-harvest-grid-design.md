# Convex-Harvest Grid — Design

Date: 2026-08-31. Operator-requested ("buy a lot of long calls, medium-long term,
expecting the big winners to beat the premium paid on the rest"). Companion to
`2026-08-31-leaps-grid-design.md`; deliberately a SEPARATE grid because the
strategy needs a different fitness and a different universe filter (§8).
Status: DESIGNED — build after the LEAPS grid items land (shares the delta
strike method and the preflight probe).

## 1. The thesis

A book of CHEAP out-of-the-money medium/long-dated calls across many names.
Most expire worthless (that is expected, not failure); the few large winners
must pay for the graveyard. Convexity harvesting: high loss RATE, small loss
SIZE, unbounded win size. The operator's "exercise the winners to get cheap
stock" framing is economically equivalent at expiry to cash-settling at
intrinsic — the backtest tests the thesis without modelling post-exercise
holdings.

This is the OPPOSITE corner of the space from `O_LEAPC` stock replacement
(deep ITM, pays intrinsic, hates decay). Same builder, different thesis —
kept as a separate key so results never blend.

## 2. Strategy key `O_CONVEX`

> **LANDED AS (operator decision, 2026-09-02, superseding the plan's own
> Task 13 text — "the tail-hedge put arm = a `kind` toggle gene (call|put|
> both) — cheap per design"): `O_CONVEX` is a GROUP key over TWO
> member arms, `O_CONVEXC` (bullish signal → `buy_call`) and `O_CONVEXP`
> (bearish signal → `buy_put`), built with the exact same
> `_build_strategy_option_group` mechanism `O_LEAP` uses — one toggleable
> entry TradeRule per arm sharing ONE exit ruleset, not a categorical
> `kind: call|put|both` gene. Reusing the group builder means the put arm
> gets the group's STANDARD per-rule `enabled` gene for free instead of a new
> toggle being invented, and there is no simultaneous call+put "both" arm on
> offer: `has_no_position` is per-INSTRUMENT (not per-structure), so once
> either arm opens a ticket on an underlying both rules are blocked from a
> second one until it closes — the same 1-ticket-per-underlying guard every
> other option key has, ruling "both" out by construction rather than
> omission.
>
> **Also landed, beyond this section's original gene list (review finding,
> 2026-09-02):** a PER-KEY override of the shared `opt_time` (elapsed-time,
> `days_opened > N`) exit. The platform-shared band (28 default, 10-45) was
> set for the 25-45-DTE structures the first grid searched and is authored ON
> by default everywhere else — for `O_CONVEX`'s ~300-DTE median entry that
> band would close a lottery ticket 28 days into a thesis built to run most
> of a year. Fixed with a wider band (90-360 step 30, 10 levels) AND authored
> OFF by default (the same `opt_sl_ml` removal idiom below, not a flag), so
> an unsearched genome never closes the ticket early; the GA can still
> discover a time stop is worth it and switch the rule on.

Genes:
- target delta **0.10–0.35 step 0.05** (the cheapness/convexity dial)
- entry DTE **180–540** (medium-long: enough runway for a thesis to play out;
  not restricted to January LEAPS cycles)
- per-ticket premium sizing **0.5–2.0% of sleeve** (many small tickets — no
  single ticket may dominate ex-ante)
- max concurrent tickets per underlying: 1 (fixed); portfolio breadth comes
  from the universe, not from pyramiding one name
- take-profit multiple **3x–10x premium, plus hold-to-expiry** (searched — the
  central open question of convexity harvesting is whether to cash winners or
  let them run)
- `opt_sl_ml` stop **searchable with default OFF** — stopping a lottery ticket
  at a % of its premium amputates the convexity the strategy exists to buy.
  The GA may still discover a stop helps; it must not be imposed. **LANDED
  AS**: "default OFF" means the rule is REMOVED from the emitted ruleset for
  an unsearched/default genome (`_OPTION_SL_ML_AUTHORED_OFF`), not stamped
  with a `enabled: False` flag — the rule keeps its `toggle_optimize` gene so
  the GA can still switch it on, but a rule-level `enabled: False` is never
  emitted onto any ruleset (plan Task 14a item 4(e); the same removal, not a
  flag, discipline the `opt_time` override above and O_ERN/O_CBS/O_PBS use).

Max loss per ticket = premium (MEASURED); the rails charge it to the
deployment cap. Nothing here is naked; the operator's no-naked rule is
untouched.

## 3. Fitness — why `option_car` cannot judge this

`consistent_annual_return`-family fitness rewards steady equity and punishes
drawdown. Convex harvesting bleeds premium steadily and wins lumpily: under
CAR, a genuinely profitable convex book scores like a bad strategy, and the
grid would "learn" to gut the convexity (tight stops, near-dated, low delta
spread) to fake smoothness. The metric must match the payoff shape.

**New fitness `option_convex`** (testplatform change, fitness registry +
routing like `_OPTION_CAR_STRATEGIES`):

1. **Wipeout sentinel first** (same literal ordering discipline as F9a): any
   dd ≥ 100 → sentinel, before anything else.
2. **Rank on end-of-window total return** (net of costs), NOT year-by-year
   consistency. One number: did the winners beat the graveyard. **LANDED AS
   NOTE**: this return term is a `total_return`-family value, and 683c7379
   (2026-09, plan Task 14a item 3) fixed `stressed_results` so a run with
   `--stress-spread-bps` set now actually applies the stress to
   `total_return`/`calmar_ratio` instead of silently discarding it — before
   that fix a stressed `option_convex` run was scoring on the UNSTRESSED
   return term despite the flag. A convex run scored WITH `--stress-spread-
   bps` before 683c7379 and one scored WITH it after are different baselines
   for that reason (see the results-comparability note); an unstressed run
   is unaffected either side of that commit.
3. **Drawdown enters only past a threshold**: no penalty below 50% peak-to-
   trough (bleed is the cost of the book); linear penalty 50→90%; sentinel at
   100. The threshold is a config constant, commented, not a gene.
4. **Breadth floor instead of the 12-trades/yr floor**: ≥ 30 tickets/yr and
   ≥ 20 distinct underlyings traded, else LOW_TRADE-style sentinel — a convex
   result built on 5 tickets is a coin flip, not a strategy.
5. **Telemetry recorded, not scored**: hit rate, top-1/top-5 share of net P&L
   (the concentration deploy-check will light up BY DESIGN — per the standing
   2026-08-06 decision, skew is a legitimate profile and stays a deploy-time
   check, not a fitness signal).

## 4. Universe

Broader than the LEAPS filter: keep a stage-1-universe symbol iff the cache
carries expiries with bars at **DTE ≥ 270** in-window (regular monthlies
qualify; January-only names too). The same preflight probe tool as the LEAPS
grid with a different threshold parameter. Preflight prints kept/dropped.

## 5. Matrix

- `tools/run_convex_matrix.py` (or a `--fitness option_convex` mode of the
  LEAPS script if the code falls out that way — operator does not care about
  the script boundary, only that results and fitness never mix).
- Jobs: `O_CONVEX` × stage-1's experts, singles. **LANDED AS 2 jobs**:
  `tools/run_convex_matrix.py`'s `_DEFAULT_EXPERTS` is `["FMPRating",
  "DeterministicScorer"]` — the two stage-1 experts that clear the "measured,
  not just registered" bar (the same reasoning `_DEFAULT_EXPERTS` uses for
  the screener-style grid-2 keys). Pop 40 / gen 6.
- Window 2023-01 → 2025-12, 2026 held out.

## 6. Build items beyond the LEAPS grid's

1. Fitness `option_convex` (registry + per-strategy routing + the frozen-
   baseline test pattern used by the equity fitness).
2. The breadth floor plumbed as config (never silently replacing the trade
   floor for other strategies).
3. Take-profit-multiple exit rule for long options (close at N× entry
   premium) if not already expressible; sl_ml default-off wiring for this key.
4. Probe-tool threshold parameter (270 vs 365).

## 7. Limitations — sharper than the LEAPS grid's

- **A bull window is the friendliest possible test.** 2023–2025 contains
  monster runs; a WIN here is weak evidence, a LOSS here is strong evidence
  against. State this in every results readout.
- Rare-win strategies need many independent draws; ~2.5 years of one regime
  is close to the statistical minimum even with the breadth floor.
- OTM medium-dated spreads are proportionally the widest-quoted contracts;
  the percent-of-premium cost model likely understates them. Marginal winners
  are noise.

## 8. Why a separate grid (operator decision, 2026-08-31)

Different fitness (option_convex vs option_car), different universe threshold
(270 vs 365 DTE), different reading of results (evidence-against is the
strong signal). Sharing a matrix with the LEAPS keys would invite comparing
fitness numbers across metrics — the exact cross-metric comparison the
CAR-scale change of 2026-08-04 taught us never to allow.
