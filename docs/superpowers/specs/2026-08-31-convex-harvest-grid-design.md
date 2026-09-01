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
  The GA may still discover a stop helps; it must not be imposed.

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
   consistency. One number: did the winners beat the graveyard.
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
- Jobs: `O_CONVEX` × stage-1's experts, singles. 2–3 jobs. Pop 40 / gen 6.
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
