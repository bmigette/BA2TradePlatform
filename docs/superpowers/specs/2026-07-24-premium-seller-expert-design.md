# PremiumSeller — dedicated option income expert (design)

Date: 2026-07-24
Status: awaiting user review
Scope: backtest app (`testplatform/`) v1; live enablement is a later, separate step.

## 1. Context and motivation

The OS1–OS4 option strategies run through the classic per-signal pipeline (expert
signal → ruleset → TradeRiskManagement → TradeConditions exits). Two audit cycles on
them (see `docs/2026-07-22-options-audit-and-fixes.md`, B1–B10) concluded:

- **OS1 (long premium) has no edge** on weekly FMPRating signals — 307/307 GA configs ≤ 0.
- **OS2 (credit selling) has the systematic premium** but is hobbled by the architecture:
  per-signal, one-structure-at-a-time deployment (~$335 premium per structure, ~6
  structures/yr on $10k → ~3% capital utilization) and TP/SL/time exits that are the
  wrong shape for theta harvesting. Post-fix validation (run 760, since wiped from the
  DB): +13.1% per-premium, TR 7.42% over 2.2y.
- The classical equity experts' backtest numbers (2,000–17,000% total return with
  `fixed` $1,000 sizing on $10k) **do not reconcile and are not an honest benchmark**.

The user decision: build a **dedicated option income expert** that bypasses the classic
risk manager (the FactorRanker plumbing pattern: `bypasses_classic_rm` +
`uses_risk_manager=False`), owning its whole lifecycle via its own portfolio manager.
FactorRanker is the plumbing example only — the internals are NOT copied.

## 2. External evidence (web research, 2026-07-24)

- **Index put writing is the benchmark.** CBOE PUT Index (ATM cash-secured S&P puts):
  ~9.4–9.7%/yr over 37 years, lower volatility than the S&P 500; beats covered-call
  writing (BXM) by ~1%/yr over the last decade
  ([WisdomTree](https://www.wisdomtree.com/us/insights/blog/why-fear-pays-the-case-for-put-writing-over-call-writing),
  [Cboe/Bondarenko](https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf)).
  Edge = implied vol systematically exceeding realized vol, richest when IV ≫ RV
  ([arXiv survey](https://arxiv.org/pdf/2508.16598)).
- **Mechanical short-premium rules (tastytrade school) — the de-facto practitioner spec:**
  16-delta short strangles, 45 DTE, close at 50% of max profit (25% for straddles),
  exit/roll at 21 DTE (managing at 21 DTE added ~10% higher average daily returns —
  [tastylive](https://www.tastylive.com/news-insights/test-different-duration-volatilities-get-some-surprising-results)),
  IVR ≥ 50 entry gate, deploy ≤40–50% of capital scaled by IVR/VIX, ≤3x notional
  leverage, many uncorrelated underlyings
  ([SteadyOptions](https://steadyoptions.com/articles/selling-short-strangles-and-straddles-does-it-work-r516/)).
- **Honest counter-evidence:** an 11-year backtest of those exact rules on
  undefined-risk strangles LOST 9%
  ([sjoptions](https://www.sjoptions.com/backtesting-options-strategies/)). The tail is
  real → undefined-risk structures get their own stricter rails (§6).
- **The wheel** (cash-secured put → assignment → covered calls; 30–45 DTE, Δ0.30,
  1–2%/month target — [OIC](https://www.optionseducation.org/news/june-webinar-takeaways),
  [JetStream OS](https://www.jets3t.com/lessons)) is excluded from v1: the backtest
  engine force-liquidates assigned stock at the next bar (no-orphaned-stock policy), so
  a true wheel needs engine changes. Possible v2.

**Honest expectation:** sustained ~10–15%/yr on deployed capital is the evidence-based
ceiling for defined-risk selling; 30%/yr requires leverage/tail risk the backtest
rewards and live punishes. The design maximizes deployment within explicit rails; it
does not promise a number.

## 3. Architecture

Two new components, one engine seam generalization, GA rewiring. Every change is
additive; FactorRanker and all stock/equity paths must remain byte-identical in
behavior (standing user constraint).

### 3.1 `PremiumSeller` expert — `packages/experts/ba2_experts/PremiumSeller/`

A `MarketExpertInterface` bypass expert:

- `bypasses_classic_rm = True` class attribute.
- `get_expert_properties()`: `uses_risk_manager: False`,
  `can_recommend_instruments: True`, `should_expand_instrument_jobs: False`,
  `required_instrument_selection_method: "expert"`, `schedules_open_positions: False`
  (mirrors the FactorRanker markers so the live WorkerQueue RM gate and the backtest
  catalog treat it correctly from day one).
- New class attribute `manages_between_entries = True` (see §3.3): held-position exits
  run on the MANAGE cadence, entries on the ENTRY cadence.
- New class attribute naming its portfolio manager (see §3.3).
- `analyze_as_of(as_of, ctx)` returns one Recommendation with
  `raw_outputs["targets"] = {"structures": [StructureSpec, ...]}` (§4). No
  ExpertRecommendation-per-symbol, no ruleset evaluation.
- Registered in `testplatform/backend/app/services/backtest/daily_backtest_handler.py`'s
  expert map and surfaced by `experts_catalog` (class-attribute driven, no catalog
  change expected beyond the import map).
- Settings via `get_settings_definitions()` — every parameter is an explicit setting;
  no config `.get()` defaults, no silent fallbacks (project convention).

### 3.2 `OptionPortfolioManager` — `packages/experts/ba2_experts/PremiumSeller/portfolio.py`

Owns the full lifecycle (this replaces the RM for this expert):

- `reconcile(targets)`: diff desired vs held structures → close/roll held ones that hit
  their management rule (§5), open new ones up to the caps (§6). Opens via the existing
  multi-leg order path (`option_strategy` tagged: `put_credit_spread`, `short_put`,
  `short_strangle`), which carries defined-risk vs undefined-risk margin handling in
  `BacktestAccount`. Closes/rolls via the B10-fixed `TradeActions._close_multi_leg`.
- `manage_open(as_of)`: the manage-cadence pass — evaluate capture %/roll-DTE per held
  structure without opening new ones.
- Expiry settlement stays with the engine's `_apply_option_expiry`; margin-call
  liquidation stays with `maybe_margin_call_liquidation` (both B10-hardened). The
  manager keeps its own margin headroom (§6) so engine liquidation is the last resort.
- Holdings discovery uses the same expert-scoped OPENED-transaction query pattern as
  `FactorPortfolioManager.get_holdings` so the structures are recognized as this
  expert's own.

### 3.3 Engine seam generalizations — `testplatform/backend/app/services/backtest/daily_engine.py`

1. `_bypass_manager(expert_id)` currently hardcodes `FactorPortfolioManager`. Resolve
   the manager class from the expert (class attribute, e.g.
   `portfolio_manager_classpath`), **defaulting to FactorRanker's manager** — existing
   bypass behavior byte-identical.
2. The bypass branch currently runs only on entry bars ("rebalance IS the management").
   When the expert declares `manages_between_entries = True`, route MANAGE-cadence bars
   to `manager.manage_open(as_of)` as well. Default `False` → FactorRanker unchanged.
3. `_apply_bypass_stops` (per-name equity-loss stop) stays equity-only; PremiumSeller's
   downside protection is its per-structure management + rails (§6), not an equity stop.

### 3.4 GA wiring — `strategy_optimization_handler.py`

- Existing bypass handling already strips tp/sl/cond:*/exit:* genes when the expert
  declares `bypasses_classic_rm`; PremiumSeller gets that for free.
- The gene space = PremiumSeller's optimizable settings (§7). Filters are GA-toggleable
  (user decision) so the optimizer can prove which filters earn their keep.
- Fitness: reuse the options-scoped fitness (per commit `358ee91`); the tuning must not
  affect stock experts (standing constraint).

## 4. Structure selection pipeline (per ENTRY bar)

The expert does NOT predict direction. It systematically sells the same shapes of
insurance every cycle on the richest premium available, sized by a risk budget. The
entry gates below and the exit rules of §5 are the tunable core of the expert — every
threshold is a GA gene (§7).

1. **Universe.** Screener metric store (FactorRanker's data source) when the
   `universe_source=screener` filter is enabled, else the static `enabled_instruments`
   list. Large-cap/liquid names only (market-cap + volume thresholds are settings).
2. **Earnings exclusion** (GA-toggleable): skip underlyings with an earnings date inside
   the DTE window (FMP earnings calendar, already in-tree).
3. **FMP-rating floor** (GA-toggleable): skip underlyings whose FMP rating is below a
   threshold — the "only sell puts on names you'd be happy to own" filter. This is a
   risk filter, not a directional signal (the OS1 lesson: directional timing doesn't pay).
4. **IVR gate** (GA-toggleable, threshold GA): IVR = rank of current chain IV within its
   ~1y history of cached OPRA chain snapshots. Skip underlyings below the gate. Ranking
   among survivors: IVR descending.
5. **IV−HV spread gate** (GA-toggleable, threshold GA in vol points): sell only when
   implied vol exceeds realized (historical) vol by at least X pp — the direct measure
   of the premium seller's edge. HV from the underlying's daily closes (setting:
   lookback window); missing HV history → gate fails closed (skip, never fabricate).
6. **Trend filter** (GA-toggleable, SMA period GA): sell puts / put spreads only when
   the underlying closes above its SMA(N) (default 200). A *when-not-to-sell* regime
   rule, not a directional bet — it keeps the sleeve from selling insurance into
   downtrends. Ignored for strangles (delta-neutral by construction).
7. **Structure construction** (per selected underlying, per the GA-tuned structure mix):
   - Expiry nearest to target DTE (GA, 30–45).
   - Short strike(s): delta closest to target (GA, 0.16–0.35). Delta from OPRA greeks;
     Black-Scholes from chain IV as fallback; missing IV → skip underlying (never
     fabricate a delta).
   - `put_credit_spread`: long strike = width W below short strike (GA).
   - `short_put` / `short_strangle`: undefined-risk; subject to §6 sub-rails.
   - Sizing: qty = floor(risk_budget_per_structure / max_loss_per_structure) for
     defined-risk (max loss = (width − credit) × 100); for undefined-risk, qty bounded
     by the margin model and the notional leverage cap. Skip if credit < min-credit
     ratio (GA floor).
8. **Targets emission.** Held structures count against the caps; only the gap between
   current book and target book is emitted.

## 5. Position management (per MANAGE bar, daily)

Per held structure, in priority order:

1. **Profit capture**: close at X% of max credit captured (GA, ~50%; ~25% for
   strangles).
2. **Tested-side management** (GA-toggleable, threshold GA): close or roll when the
   short strike's delta exceeds the threshold (e.g. Δ > 0.30 — the position is being
   tested). The standard practitioner adjustment; uses the same greeks source as entry
   (OPRA greeks, BS-from-IV fallback, missing → no action this bar).
3. **Time stop / roll**: remaining DTE < roll threshold (GA, ~21) → close; if the
   underlying still passes §4 filters and the book has room, re-enter fresh (the "always
   invested" engine). Managed at 21 DTE specifically to avoid end-of-life gamma
   (evidence §2).
4. **Defined-risk stop** (GA-toggleable, multiple GA): exit a spread when its loss
   reaches N× the credit received (symmetric counterpart of the undefined-risk stop).
5. **Undefined-risk stop** (GA-toggleable): close at 200% of credit received.
6. **Circuit breaker**: sleeve drawdown since equity peak > X% (GA) → flatten the whole
   book and stand down until the next entry bar.

What reaches expiry unit-settles via the engine's existing `_apply_option_expiry`.

## 6. Risk rails (mandatory — this IS the risk manager for the sleeve)

All explicit settings with defaults in `get_settings_definitions()`; no silent
fallbacks:

- `max_deployment_pct` (GA, default ≤ 50): max % of sleeve equity in use, scaled by the
  IVR/VIX regime when `ivr_scaling` is enabled (deploy more when premium is rich).
- `undefined_risk_max_pct` (GA, default ≤ 20): separate, stricter sub-cap on naked
  margin usage.
- `max_notional_leverage` (GA, default ≤ 3.0): total short notional ≤ N × sleeve equity.
- `max_structures_per_underlying` (default 1).
- `max_concurrent_structures` (GA).
- `circuit_breaker_drawdown_pct` (GA).
- `min_credit_ratio` (GA floor on credit vs width/risk).

## 7. GA-tunable parameters (gene space)

The entry/exit signal set of §4–§5 IS the tunable core of this expert — every threshold
is a GA gene:

- **Entry signals**: IVR gate on/off + threshold, IV−HV spread gate on/off + threshold
  (+ HV lookback), trend filter on/off + SMA period, earnings filter on/off,
  FMP-rating floor on/off + threshold, universe source (static/screener), target delta,
  target DTE, spread width, min-credit ratio.
- **Exit signals**: profit-capture % (per structure family), tested-side delta
  management on/off + threshold, roll-DTE threshold, defined-risk stop on/off + credit
  multiple, undefined-risk stop on/off (+ multiple), circuit-breaker %.
- **Book/rails**: structure mix enablement (`enable_put_credit_spread` /
  `enable_short_put` / `enable_short_strangle`), max deployment %, IVR scaling on/off,
  undefined-risk sub-cap %, notional leverage, max concurrent structures.

Roughly 20–24 genes — deliberately smaller and more meaningful than the OS1–4
rm:/cond:/exit: gene soup. Every added toggle is overfit surface: the forward
out-of-sample split (§10) is what keeps the tuned result honest.

## 8. Error handling

- Chain/IV-history cache miss → `BacktestCacheMiss` propagates and ABORTS the run
  (existing bypass convention; silently degrading results is worse than crashing).
- Missing greeks → Black-Scholes from chain IV; missing IV → skip that underlying.
- Missing quote at close/roll → skip the action that bar, log, retry next bar; the
  expiry path still resolves the position.
- Per-bar reconcile/manage failure → logged and swallowed (one bad bar must not abort a
  run), matching the engine's existing bypass try/except convention.
- No fabricated money values anywhere: missing prices/margins decline the trade, never
  default.

## 9. Data requirements

- **OPRA chain snapshots**: ~1y of cached chain rows per universe underlying for IVR
  (the `options_cache.write_chain_rows` store). A launcher prewarm step must populate
  this before the first backtest — same discipline as existing cache warming; the run
  aborts loudly on a miss (§8).
- FMP earnings calendar + FMP rating: already cached in-tree (fmp_common history cache).
- Screener metric store: already maintained for FactorRanker.

## 10. Testing plan

- **Unit** (`packages/experts/tests/`, live `tests/` where shared): IVR math, structure
  construction (delta/DTE/width selection incl. BS fallback), sizing math, every rail
  of §6, filter toggles.
- **Engine integration** (`testplatform/backend/tests/backtest/`): manager-class
  resolution (PremiumSeller → OptionPortfolioManager, FactorRanker → unchanged);
  manage-cadence routing; a deterministic seeded-cache run asserting: expected spread
  opened, 50%-capture close, 21-DTE roll, expiry settle, deployment cap respected,
  earnings/rating/IVR filters each block entry when toggled on.
- **Non-regression gates**: full live suite and backend backtest suite green
  (1042 + 440 at design time); FactorRanker bypass runs byte-identical (manager
  resolution defaults + manage-cadence flag defaults off); a FactorRanker golden run
  compares equal before/after the engine seam change.
- **GA smoke**: optimization on PremiumSeller strips classic genes, exposes §7 genes,
  applies options fitness; stock-expert optimizations untouched.
- **Validation**: same window as the OS2 runs + a forward out-of-sample split; judged
  against the honest ~10–15%/yr benchmark (§2), not the classical experts' artifacts.

## 11. Scope fences (v1)

- Backtest app only; live wiring (WorkerQueue already honors `uses_risk_manager=False`)
  is a separate later step.
- No wheel / no holding assigned stock (engine policy; v2 candidate).
- No directional signal input in v1 (OS1 lesson). FMP rating is a quality floor only.
- Stock/equity expert code paths untouched; FactorRanker behavior byte-identical.
- Shared code goes in `packages/experts`; only the engine seams and GA wiring live in
  `testplatform/backend`.
