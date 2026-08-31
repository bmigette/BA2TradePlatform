# LEAPS Grid — Design

Date: 2026-08-31. Validated section-by-section with the operator in-session.
Status: DESIGNED — implementation starts after the `option-selection-modes` branch
merges (depends on Task-8 max-loss stamps, delta viability, and the covered-call
stock-leg fix queued on that branch).

Companion: `2026-08-31-convex-harvest-grid-design.md` (the OTM convexity book —
deliberately a SEPARATE grid with a different fitness; see §8 there for why).

## 1. Feasibility — measured, not assumed (2026-08-31, seed 7)

The parquet cache (`TastyTradeOptionsProvider`, 857 underlyings, bars
2023-01..2026-08) DOES carry LEAPS, but only where the market lists them:

- LEAPS live on the **January cycles**. Sampled Jan-2025/Jan-2026 expiries carry
  bars from **745–858 days** before expiry on liquid names (AXP, FUTU, CCJ, APLD),
  with ~180 trading days inside a 270–540-DTE entry band.
- **Split universe**: smaller names (SCI, NWG, HBM in sample) never list LEAPS —
  their January partitions begin 52–246 days pre-expiry. 8/14 sampled January
  expiries had any 1yr+ bars. Weekly expiries list ~6 weeks out and are irrelevant.
- Bar density at LEAPS range ≈ 50–65% of trading days: sparser than near-dated,
  workable for daily marks with a fallback.
- Greeks: per-date bar iv/delta is 88.2% populated (see
  `option_selector._publishes_spread` for the authoritative cache record), so
  **delta-based selection is viable and causal** at LEAPS range.

## 2. Strategies and genes

Three pure-option strategy keys, `_OPTION_STRATS` style:

**`O_LEAPC` — stock-replacement long call.** Selects by **delta** (new strike
method, §5.1), not percent-OTM. Genes: target delta 0.70–0.90 step 0.05; entry
DTE band 365–550; roll/exit DTE floor 90–240 (exit before the decay/gamma zone —
the core LEAPS discipline); `opt_sl_ml` stop (max loss = debit, so it is a stop
on the premium); sizing % of sleeve.

**`O_LEAPP` — bearish twin.** Same builder, `kind=put`, same genes. The grid's
only bearish long-dated arm; cheap to carry, easy to drop from a matrix run.

**`O_PMCC` — poor man's covered call (diagonal, wheel-pattern lifecycle).**
LEAPS leg delta 0.75–0.85, DTE ≥365 at entry. Short-call overlay delta
0.15–0.30, DTE 30–45, rolled at expiry or at a buyback trigger (% of credit
decayed — searched). Shares the LEAPS roll-floor gene with `O_LEAPC`.

~7–9 genes per strategy: delta selection replaces the strike-param + box
machinery.

## 3. PMCC model — the operator's "both" decision

The payoff evaluator prices payoff AT EXPIRY; a PMCC has two expiries. Resolution:

- **Risk math (max loss, rails, stops): intrinsic floor.** At the short's expiry
  the LEAPS is always worth ≥ intrinsic, so valuing the cover at intrinsic gives a
  conservative, MEASURABLE bound: max loss = LEAPS debit − net credits collected.
  Stamped at submit (Task-8 seam), charged to the deployment cap (covered, not
  naked), drives `opt_sl_ml` unchanged. Errs against the strategy, never for it.
- **Daily marks: real bars first** (the cache has the LEAPS' own closes), under
  the F1/F2 clamps.
- **Black-Scholes on the bar's cached iv as the mark FALLBACK only** (~35–50% of
  days at LEAPS range have no bar), clamped to no-arb bounds. **BS never touches
  a risk number** — pinned by a mutation test. Hierarchy: bars → BS(iv) →
  intrinsic/entry.

## 4. PMCC lifecycle mechanics

- **Entry**: one decision opens the structure — buy the LEAPS, immediately sell
  the first short call. Admission requires **short strike > LEAPS strike** (what
  makes the pair bounded).
- **Roll loop** (wheel pattern): at short expiry or buyback trigger — expired
  worthless → sell the next; ITM → buy back at the modelled price (F7 concession)
  and re-sell. Credits accumulate against the LEAPS basis.
- **Structure exit**: LEAPS roll floor hit, `opt_sl_ml` fires, or LEAPS delta
  falls below ~0.50 (stops behaving like stock — searched on/off). Any exit
  closes BOTH legs. **The engine never leaves the short uncovered** — invariant
  pinned by a named test (operator's no-naked rule).

## 5. Build items (dependency order)

1. **`option_strike_method: "delta"`** beside `percent_otm`, reading the bar's
   delta. Small; eligibility already filters on delta.
2. **BS mark fallback**: one pure function (price from iv, no-arb clamped), wired
   only into the mark-fallback chain.
3. **PMCC lifecycle builder**: wheel pattern with a long-call cover — extends the
   covered-call stock-leg fix (same seam, cover kind = long call). Never-uncovered
   invariant + strike-ordering guard tests.
4. **Preflight probe tool** (the §1 measurement productionised) +
   **`tools/run_leaps_matrix.py`** + the three strategy defs/genes + the explicit
   lower trade floor.

## 6. Universe and matrix

- **LEAPS-listed ∩ stage-1 universe**, measured at preflight: keep a symbol iff
  it has a January-cycle expiry with bars at DTE ≥365 inside the window. Preflight
  prints kept/dropped counts — no silent no-contract trials. Expect ~half the
  stage-1 universe, skewed liquid.
- **Jobs**: `run_leaps_matrix.py`, singles only ({O_LEAPC, O_LEAPP, O_PMCC} ×
  stage-1's experts) — no group jobs in round one (a 3-member group only muddies
  attribution). 6–9 jobs total.
- **Fitness `option_car`**, window **2023-01 → 2025-12**, 2026 held out. LEAPS
  trials trade rarely: the LEAPS jobs get an explicit, commented **lower
  trade floor** in config — never a silent bypass of the 12-trade/yr rule.
- **Pop 40 / gen 6, elitism 10%** — deliberately modest; the sample supports
  "does any region work," not fine-tuning.

## 7. Limitations (read results through these)

- **One regime.** 2023–2025 was mostly up; long calls flatter themselves. The
  matrix answers "which configurations worked in this window" only.
- **Spread costs at LEAPS range are a guess** — the percent-of-premium model was
  set for near-dated; real LEAPS spreads are wider. Marginal winners are noise.
- **~2.5 non-overlapping holding periods.** Directional evidence, not statistics.
- Runs AFTER the selection-branch merge; never alongside a live grid in the main
  tree (worktree discipline as usual).
