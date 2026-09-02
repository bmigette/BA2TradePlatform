# Options Grid 2 — LEAPS, PMCC, Earnings Vol, Backspreads, Calendars — Design

Date: 2026-08-31. Validated section-by-section with the operator in-session;
scope extended same day: ONE grid, MANY strategy keys, each with its own gene
set — the S1–S7 pattern the equity grids use, applied to option structures
(operator: "1 grid many strat").
Status: DESIGNED — implementation starts after the `option-selection-modes`
branch merges (depends on Task-8 max-loss stamps, delta viability, the
covered-call stock-leg fix).

Companion: `2026-08-31-convex-harvest-grid-design.md` — `O_CONVEX` stays a
SEPARATE grid because it needs a different fitness (`option_convex`) and a
different reading of results. Everything in THIS grid runs under `option_car`.

## 1. Feasibility — measured, not assumed (2026-08-31, seed 7)

The parquet cache (`TastyTradeOptionsProvider`, 857 underlyings, bars
2023-01..2026-08) carries LEAPS where the market lists them:

- LEAPS live on the **January cycles**. Sampled Jan-2025/Jan-2026 expiries carry
  bars from **745–858 days** before expiry on liquid names (AXP, FUTU, CCJ,
  APLD), ~180 trading days inside a 270–540-DTE entry band.
- **Split universe**: smaller names never list LEAPS (partitions begin 52–246
  days pre-expiry). 8/14 sampled January expiries had any 1yr+ bars.
- Bar density at LEAPS range ≈ 50–65% of trading days.
- Per-date bar iv/delta is 88.2% populated (authoritative record:
  `option_selector._publishes_spread`) → **delta selection is viable and
  causal**, including at LEAPS range; iv is measurable for the earnings-crush
  and iv-rank gates.

## 2. Strategy keys and genes (each its own searched space)

### Long-dated family
> **LANDED AS (operator decision, 2026-09-02, superseding the two-key text
> below): `O_LEAPC`/`O_LEAPP` are NOT two launchable keys — they are the two
> toggleable ARMS of ONE signal-driven group key `O_LEAP`** (a bullish
> `buy_call` arm and a bearish `buy_put` arm, one shared entry-gate table per
> arm via `_option_entry_rule(member, toggleable=True)`, one shared exit
> ruleset). The expert's directional signal (bullish/bearish) picks which arm
> can fire — there is no separate `kind` gene; `has_no_position` is
> per-instrument, so both arms can never open on the same underlying at once.
> The per-arm `enabled` gene comes free with the group builder, so the GA can
> drop a direction outright in a one-sided regime. Each arm keeps its own row
> (and so its own gene table) below — a row IS a gene table — but only
> `O_LEAP` is a launchable strategy key.

**`O_LEAPC` — stock-replacement long call arm.** Delta-selected (new strike
method, §6.1). Genes: target delta 0.70–0.90 step 0.05; entry DTE 365–550;
roll/exit DTE floor 90–240 (exit before the decay/gamma zone); `opt_sl_ml`;
sizing.

**`O_LEAPP` — bearish arm.** Same builder, `buy_put`, same genes. The grid's
only bearish long-dated arm.

**`O_PMCC` — poor man's covered call** (diagonal, wheel-pattern lifecycle, §3–4).
LEAPS leg delta 0.75–0.85, DTE ≥365; short-call overlay delta 0.15–0.30, DTE
30–45, rolled at expiry or buyback trigger (% of credit decayed — searched);
shares the LEAPS roll-floor gene.

### Event family
**`O_ERN` — earnings long vol.** Buy a straddle (or strangle — structure
toggle gene) before earnings, exit after the move/crush. A DIFFERENT alpha
source: event-driven, not screener-driven; long-only premium, so the no-naked
rule is untouched. Genes: entry days-before-earnings 1–5; exit days-after 0–2;
structure = straddle | strangle with strangle width delta 0.25–0.45; expiry
selection DTE 7–30 (nearest with runway past the event); iv-rank entry gate
(only when iv-rank ≤ X, X searched 30–70 or off — buying vol only when it is
not already bid); sizing; `opt_sl_ml` searchable, default OFF (the thesis is
binary; a stop mid-event amputates it). Uses the FMP earnings-dates provider
(FMPEarningsDrift already consumes it). Daily-bar caveat: entries/exits pin to
closes; the intraday earnings-day price is not modelled — stated, accepted.

> **LANDED AS (2026-09-02, plan Task 14b item 5):** the delta 0.25–0.45 band
> above was designed from the start but not expressible until this date —
> `OpenStrangleAction` hard-coded `method="percent_otm"` at both leg-selection
> sites until it learned the delta method (now passes `self.strike_method` on
> BOTH legs; the selector ranks on ABSOLUTE delta, so one target on both legs
> is symmetric — 0.35 picks a call above spot and a put below it at matching
> `|delta|`). `option_strike_delta` is a STILL-CONDITIONAL domain, not a dead
> gene: `open_straddle` is ATM by definition (both legs on the strike nearest
> spot) and stays deliberately OFF the strike-method registry (see
> `OpenStraddleAction`'s own docstring) — the gene is inert on the straddle
> arm and live on every strangle genome, the same shape a conditional-domain
> gene takes anywhere else in this design.

### Convexity-financed family
**`O_CBS` / `O_PBS` — call / put ratio backspreads** (sell 1 nearer, buy 2
further out; 1×2 fixed). Convexity financed by the short — the convex-harvest
thesis with the bleed reduced; `O_PBS` doubles as a crash hedge. Loss bounded
(worst case pins between strikes); the payoff machinery already handles ratios
(`O_RS` is the mirror). Genes: short-leg delta 0.35–0.50; long-leg delta
0.15–0.30; DTE 60–180; exit DTE floor 20–45; take-profit multiple 2x–6x | to
expiry; `opt_sl_ml` searchable default off; sizing. NOTE: these sit between
the two fitness worlds — if they score uniformly thin under `option_car`, the
right move is migrating them to the convex matrix, not tuning them here.
Recorded so a future reader does not misread thin scores as "backspreads don't
work."

### Term-structure family
**`O_CAL` — ATM calendar** (sell near-dated, own far-dated, same strike).
Theta-differential harvesting; unlocked by the PMCC machinery (same two-expiry
lifecycle, same intrinsic-floor risk basis and mark hierarchy). Genes: strike
delta ~0.45–0.55; short-leg DTE 20–40; long-leg DTE 60–120; short roll like
PMCC; exit when the long hits its DTE floor. **Phase-gated: runs only after
PMCC's machinery is proven in this same matrix** (thinnest data support of the
family — lives entirely on relative pricing between expiries, where sparsity
and the guessed spread model hurt most).

## 3. PMCC / two-expiry model — the operator's "both" decision

Applies to `O_PMCC` and `O_CAL`:
- **Risk math (max loss, rails, stops): intrinsic floor.** At the short's
  expiry the long is worth ≥ intrinsic → conservative MEASURABLE bound (PMCC:
  LEAPS debit − net credits). Stamped at submit (Task-8 seam), charged to the
  deployment cap (covered), drives `opt_sl_ml`. Errs against the strategy.
- **Daily marks: real bars first** (F1/F2 clamps).
- **BS on the bar's cached iv as mark FALLBACK only**, no-arb clamped. **BS
  never touches a risk number** — pinned by a mutation test.
  Hierarchy: bars → BS(iv) → intrinsic/entry.

## 4. Two-expiry lifecycle mechanics (wheel pattern)

- **Entry**: one decision opens the structure — long leg first, short
  immediately after. PMCC admission requires short strike > LEAPS strike.
- **Roll loop**: at short expiry/buyback trigger — worthless → sell next;
  ITM → buy back at modelled price (F7 concession), re-sell.
- **Structure exit**: long-leg DTE floor, `opt_sl_ml`, or (PMCC) LEAPS delta
  < ~0.50 (searched on/off). Any exit closes BOTH legs. **The engine never
  leaves the short uncovered** — invariant pinned by a named test.

## 5. Universe

**Listed-depth ∩ stage-1 universe, measured at preflight** with a
per-strategy DTE threshold: LEAPS/PMCC keys need January-cycle bars at
DTE ≥365; O_CBS/O_PBS/O_CAL need DTE ≥180; O_ERN needs only earnings dates +
DTE ≥7 chains (nearly the whole stage-1 universe). One probe tool, threshold
parameterised; preflight prints kept/dropped per strategy — no silent
no-contract trials.

## 6. Build items (dependency order)

1. **`option_strike_method: "delta"`** beside `percent_otm` (bar delta).
2. **BS mark fallback** (pure function, mark chain only).
3. **Two-expiry lifecycle builder** (wheel pattern, long-option cover) —
   extends the covered-call stock-leg fix; serves PMCC first, calendar later.
4. **Earnings-event entry gating**: the earnings-dates provider surfaced as an
   entry condition for `O_ERN` (days-to-earnings window), plus the iv-rank
   entry gate reusing the existing `_IV_RANK_GATE` machinery.
5. **Backspread builders** `O_CBS`/`O_PBS` (1×2, both legs delta-selected) —
   near-free given `O_RS`.
6. **Preflight probe tool** (parameterised threshold) +
   **`tools/run_options2_matrix.py`** + strategy defs/genes + the commented
   lower trade floor for the long-dated keys (O_ERN keeps the normal floor —
   earnings events are frequent).
7. Phase 2 (after PMCC proves the machinery in real runs): `O_CAL`.

## 7. Matrix

- Singles only, one job per (strategy × expert) — attribution stays clean, the
  S1–S7 shape. Phase 1: {O_LEAP, O_PMCC, O_ERN, O_CBS, O_PBS} × stage-1's
  experts → 12–18 jobs (O_LEAP LANDED AS the merged O_LEAPC/O_LEAPP group key,
  §2 above; O_PMCC stays phase-gated behind Task 6-PRE/6, not yet launchable
  on this branch). Phase 2 adds O_CAL.
- **Fitness `option_car`** for every key in this grid. Window 2023-01 →
  2025-12, 2026 held out. Long-dated keys get the explicit commented lower
  trade floor; O_ERN does not need it.
- **Pop 40 / gen 6, elitism 10%** — modest by design: the sample supports
  "does any region work," not fine-tuning. Group jobs (an OS5-style umbrella)
  only if stage-2 seeding later wants them.

## 8. Limitations (read results through these)

- **One regime.** 2023–2025 was mostly up; long calls and PMCC flatter
  themselves; O_PBS (crash hedge) will look useless in a window with no crash
  — that is the window talking, not the structure.
- **Spread costs at long-dated/OTM range are a guess** — the
  percent-of-premium model was set near-dated. Marginal winners are noise.
- **~2.5 non-overlapping holding periods** for the long-dated keys.
  O_ERN is the exception: hundreds of independent events in-window, so it is
  the one key whose result deserves statistical weight.
- Runs AFTER the selection-branch merge; worktree discipline as usual.

## 9. The event expert — `FMPEarningsEvent` (operator-requested 2026-08-31)

`O_ERN` chains behind a NEW expert that ranks upcoming earnings events; the
existing pipeline does the chaining for free (expert emits `ExpertRecommendation`
with confidence 1–100 → the strategy's `gate_confidence` gene thresholds it).

**Timing split (design rule):** the EXPERT owns the ranking, the STRATEGY owns
the timing. The expert surfaces every event inside a fixed look-ahead (setting
`earnings_days_look`, default 10 — a plain setting, NOT a gene) and stamps
`days_to_earnings` + its feature values onto the recommendation; `O_ERN`'s
searched entry gene (1–5 days before) reads `days_to_earnings` as an entry
condition. One timing knob, owned by one side.

### Field coverage — MEASURED 2026-08-31 against the backtest's own FMP disk
cache (`fmp_history`, ~5,000 symbols; sample = 23 names across the three bands):

- **`past_earnings_quarterly`** (dates + eps/epsEstimated): 11–12 events per
  symbol in 2023–2025, eps+estimate populated ≥90% on 21/23 names, ALL bands
  (outliers: SBET 1/12 — no coverage; ARM/GOTU minor holes). → event dates,
  historical moves (dates × our OHLCV) and surprise history are SOLID.
- **`earnings_estimates_quarterly`** (dispersion, analyst counts): high/low
  spreads populated where present, BUT only ~3 rows per symbol fall inside the
  whole 3-year window — the endpoint looks forward-biased, so HISTORICAL
  point-in-time dispersion is thin and a lookahead risk. Median analyst counts:
  large 10–26, mid 1–8, small 1–10; several mid/small names sit at exactly 1
  analyst (degenerate high==low).
- **`price_target`**: the known windowed/degenerate trap CONFIRMED in this
  window — mid/small run 0–3 targets/quarter with frequent one-analyst
  quarters (DRS: 5 of them; AMR/SKE: zero targets at all).

### Genes — emission decided BY the measurements (the w_spread discipline)

EMIT:
- `w_hist_move` — avg abs % earnings-day move over past events (dates from
  past_earnings_quarterly × our OHLCV). Solid everywhere.
- `w_surprise_vol` — std of past EPS surprises. Solid everywhere.
- `w_vol_cheapness` — historical move ÷ current implied move (straddle price
  from the options cache; bar iv 88% populated, measured). The only feature
  comparing what you PAY to what you GET — the prior favourite.
- `min_analysts` (searchable gate) and `allow_unconfirmed_dates` (on/off —
  estimated dates slip; a slipped date buys vol for nothing).

> **AMENDMENT (2026-09-01), LANDED (plan Task 11):** `min_analysts`' range is
> **0–5, with 0 = the gate OFF** — measured: the original 1–5 band's default
> of 3 refuses 66% of the universe at a 2023 as-of and 47% at 2025, a
> decaying strictness that tilts the traded universe across the window. The
> expert's DEFAULT (not the gene's) is lowered to **1** — a data-quality
> floor (at least one analyst covers the name), not a selection filter — and
> this measurement is recorded beside the setting in
> `packages/experts/ba2_experts/FMPEarningsEvent.py`.

WITHHELD until verified point-in-time (recorded, not silently dropped):
- `w_dispersion` — ~3 in-window estimate rows/symbol and 1-analyst degeneracy
  in mid/small; emitting it now would be a dead-or-lookahead gene. Emit only
  after a point-in-time replay proves the estimate rows were available BEFORE
  each event.
- `w_revision` (estimate/grade momentum) — same point-in-time caveat.
- Any price-target-based expected move for mid/small — the measured 0–3
  targets/quarter makes it noise below large-cap; if used at all it takes the
  `min_price_targets_per_quarter`-style guard verbatim.

FIXED SETTINGS (not genes): `earnings_days_look` (10), `min_hist_events` (4 —
below it the features are a coin flip; SBET-class names fail here naturally),
provider cache TTLs.

### Universe caveat (standing memory)
FMPEarningsDrift's edge lives small/midcap; large-cap earnings data was its
weak spot. O_ERN × FMPEarningsEvent jobs run on the mid/small bands first; a
large-cap job is read skeptically.
