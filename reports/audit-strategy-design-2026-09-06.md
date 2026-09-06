# Strategy design audit — independently reviewed 2026-09-06

## Scope correction: optimizable rules first

The user's intended deliverable is strategies expressible through existing optimizable rules. This section supersedes the implementation priorities below: new account allocators, cross-expert score feeds and partial-sizing actions are optional future extensions, not prerequisites for designing rule variants. The implementation defects remain separate maintenance findings.

The existing mappings in `packages/common/ba2_common/core/rule_builders.py` support confidence, expected profit, position P&L, days held, re-entry cooldowns, rating transitions, account-position flags and days to earnings. Exit actions include closing and adjusting TP/SL. The launcher already demonstrates numeric ranges, action-value ranges and optional-rule toggles. New configurations/templates may need edits, but these primitives do not require new trading-engine behavior.

| Expert | Rule-based candidate using existing primitives | Parameters to optimize |
|---|---|---|
| Insider Cluster Buy | Preserve entry; close if `days_opened > D AND profit_loss_percent < P`; move SL to an entry-relative offset after profit exceeds L; apply a losing-close cooldown. | D, P, L, SL offset, cooldown and optional-rule toggles. |
| Earnings Drift | Close a stalled position with the same age-and-P&L rule; separately test a maximum holding period and re-entry cooldown. | Stalling age/profit threshold, maximum age, cooldown. Absolute P&L replaces the original sector-relative hypothesis; it is a different, simpler experiment. |
| FMP Rating | Test rating downgrade/negative-rating exits, target-lowered exits, an age-and-P&L exit, and cooldowns after closing. | Supported numeric thresholds and enable/disable flags; no analyst-publication-age primitive is assumed. |
| DeterministicScorer | Preserve score-driven entries; test age-and-P&L exits, profit-lock stops and losing-close cooldowns. Optionally require `has_no_position_account` at entry. | Age, P&L threshold, lock threshold/offset, cooldown and gate toggles. Compare with existing S2/S6 rules to avoid duplicate variants. |
| FactorRanker | Optimize its native construction/expert settings separately. | `top_n`, weighting, gross exposure and per-name cap where exposed in its parameter space. It bypasses classic RM, so do not assume the generic rule proposals control its portfolio. |
| Senate Trader Weight | A generic age-and-P&L/cooldown variant is expressible, but remains lower research priority without verified parent results. | Age, profit threshold and cooldown. |

`days_to_earnings` also already exists, so an earnings-entry exclusion or full pre-earnings close does not inherently need a new condition class. However its historical schedule provenance and fallback behavior must be checked before trusting the backtest (`TradeConditions.py:2860`). Its units are calendar days; do not describe them as sessions. Verify day-count semantics for every age condition used.

The original 50% trims, sector-relative returns, cross-expert score percentiles and atomic residual issuer allocation are not established primitives in the inspected generic rule path. Set them aside for this scope. `has_no_position_account` can block an entry while another expert holds the symbol; it is not an atomic cap including simultaneous pending entries.

Start with three small rule families: **stalled-position exit**, **profit-lock stop**, and **post-loss cooldown**. Optimize thresholds and optional toggles against unchanged parents. Preserve protective stops and order rules so an always-matching stop adjustment cannot shadow exit rules in the first-match evaluator. No new fixed thresholds are endorsed by this correction.

**Updated assessment: retain the shortlist as research candidates, but reproduce the parents under verified sizing and execution before testing overlays.** The previous draft's capital-efficiency emphasis is sound. Its numerical anchors are inherited from `audit_gpt6_strategy.md` and the original draft; I have not independently recomputed them from trade ledgers. Family labels, job IDs and TOP ranks are not sufficient to reconstruct a strategy.

This update adds source-based analysis of strategy builders, expert signals and portfolio construction. It does not introduce new performance measurements or approve an allocation. Percentages below remain proposed experiment settings. See section 3 for the independent analysis and revised priorities, and [the code audit](reports/code_and_strategy_audit_2026-09-06.md) for reproduced implementation defects. Only this document was changed in this review; concurrent source edits were left untouched.

## 1. Expert-specific strategy proposals

**Design verdict: preserve the accepted entries and test capital-release, position-risk, and duplicate-exposure overlays first.** Do not launch another broad search for higher CAR. The first candidates I would develop are small Insider S1, mid EarningsDrift S1, and an exposure-controlled DeterministicScorer variant. Large Rating S1 and FactorRanker are useful additional candidates, but their shared large-cap exposure needs explicit management.

The numbers below motivate experiments; **they do not establish that the proposed overlays work.** I retain the previous pass's family classifications provisionally, pending verification of the persisted configurations, execution assumptions and result lineage.

### Common design requirements

Three distinctions are essential:

- **Capital utilization is missing.** Trade counts, `risk_atr` labels, and drawdowns do not reveal average or peak capital deployed. We need daily positions, market values, equity, reserved buying power, holding periods, and rejected orders.
- **“10% annually using 20% of capital” must mean account-level return if that is the objective.** A sleeve earning 10% on an allocation equal to 20% of the account contributes roughly 2% to account return—not 10%. Nor can we assume that shrinking a reported 24% CAR strategy preserves 10% CAR.
- **Annualized return does not establish steadiness.** Annual/monthly returns and underwater durations are absent. Rating comparisons also need a common calendar denominator because its feed starts in 2022.

For the experiments below, I would use these **proposed, fixed design settings—not inferred existing genes or optimized values**:

1. `family_gross_cap = 25% of account equity` is an experimental ceiling for each family, not an account-wide budget. Set a separate aggregate gross cap and reserve before combining families; five 25% ceilings permit 125% in aggregate. Measure average utilization rather than targeting 15–25% by forcing deployment when signals are scarce.
2. `account_issuer_cap = 3%`: multiple experts cannot each independently allocate 3% to the same company.
3. Evaluate net P&L against **capital-days**—the integral of gross deployed capital through time—alongside account CAR, drawdown, and monthly downside.
4. Compare every overlay with both the unchanged parent and a **uniformly downsized parent with matched average exposure**. Otherwise ordinary deleveraging can masquerade as better strategy design.

For an initial promotion hurdle, require either approximately **20% lower drawdown at comparable exposure**, or **20% fewer capital-days while retaining at least 80% of net P&L**, after costs. These are engineering acceptance criteria, not predictions. Reject an overlay that cannot outperform simple scaling on the intended dimension.

The actual persisted S1/S2/S6 genomes for the cited jobs have not been verified. Current templates ARE available in `testplatform/ba2test_launcher.py`: `_build_strategy_S1` at line 2060, S2's `_build_strategy_row` alias at line 1695, and `_build_strategy_S6` at line 1833. S1 was rewritten on 2026-08-17; current templates cannot substitute for historical job payloads. Retrieve each saved decoded genome, rule toggles, fixed settings, code revision and data/cost configuration before implementation.

---

### A. FMPInsiderClusterBuy: protect the existing S1 edge without demanding more winners

**Numerical anchor.** Small jobs **414/447 TOP5** are particularly attractive design parents for this mandate: **16.79% CAR, −4.59% DD, 206 trades, PF 2.85**, with the best five trades contributing **16.7%** of net P&L. TOP4 offers **23.44% CAR and −5.69% DD**. TOP1 has **23.77% CAR and −9.74% DD**, despite its **86.48% win rate**; its worst trade is **−3.87**, versus average trade **+0.54**.

For mid-cap, **430 TOP1** has **15.79% CAR, −9.38% DD, 269 trades**, and **26.6%** top-five concentration. **430 TOP5** offers **13.14% CAR and −7.72% DD**.

Those are reasons to prioritize loss severity and capital efficiency—not to maximize win rate.

#### A1. S1 plus a gap-aware notional ceiling

**Rule.** Preserve each parent’s entry and exit logic. Set:

`position_notional = min(parent_notional, 3% × account_equity, risk_budget / stop_fraction)`

Use `risk_budget = 0.15% of account equity`, where `stop_fraction` comes from the parent’s actual stop distance. If the parent has no defined stop, test the notional ceiling alone rather than inventing an implicit guaranteed loss limit.

As a separate ablation, add:

`pre_earnings_trim = 50%`, executed two sessions before the next earnings release **as scheduled and known then**.

**Added edge hypothesized.** Prevent low measured ATR or a narrow stop from producing excessive dollar exposure, and reduce identifiable overnight event risk. This is particularly relevant to TOP1’s occasional large loss, but the data do **not** identify that loss as an earnings gap.

**Data/feed feasibility.** Position sizes, stop distances, OHLC/ATR, and historical earnings schedules. Prices and earnings dates are plausibly available; point-in-time schedule revisions and announcement times must be verified. Actual gap-loss attribution requires the trade ledger.

**Capital and overlap.** Position caps and trims reduce intended exposure. Earnings trims should shorten overlap with EarningsDrift around subsequent announcements. They do not eliminate shared small-cap risk with **360** or shared mid-cap risk with **389/430**.

**Falsification.** Reject if capped or pre-earnings exposure has no worse downside per dollar than retained exposure, or if the overlay loses more profitable recovery exposure than it saves in tail losses. Compare with simple downsizing of **414 TOP5**, not just the riskier TOP1. A stop-based budget must also survive gap-aware execution; stops do not cap overnight losses.

#### A2. S1 plus a soft DeterministicScorer distress filter

**Rule.** At an otherwise valid S1 entry:

`size_multiplier = 0.5 if DS_percentile_in_band < 20 else 1.0`

Do not require a top-decile DS score and do not add positions because both experts agree.

**Added edge hypothesized.** Distinguish insider buying into deteriorating businesses from buying into temporarily mispriced businesses. A soft reduction preserves opportunities for contrarian recoveries and avoids turning S1 into a narrow intersection strategy.

**Data/feed feasibility.** Historical DS scores and their constituent inputs at the insider signal’s **public availability time**. Internal DS outputs plausibly exist, but persisted historical snapshots are not shown. Check whether DS already includes insider information; otherwise “confirmation” could simply count the same signal twice.

**Capital and overlap.** Reduces exposure to low-DS insider trades. However, the retained portfolio may become **more similar** to DS **360** in small-cap and **389** in mid-cap. This is a quality experiment, not a diversification claim. Enforce the shared issuer cap.

**Falsification.** Within band, sector, and entry month, the bottom-20% DS subset must exhibit worse subsequent downside or lower net P&L per capital-day. Reject if it does not, or if omitted contrarian rebounds account for the parent’s advantage. Test small and mid separately rather than assuming transfer.

---

### B. FMPEarningsDrift: release capital when drift fails to materialize

**Numerical anchor.** **370 TOP1** produces **28.38% CAR, −16.28% DD, 562 trades**, with **14.5%** top-five concentration. **424 TOP1** produces **24.87% CAR, −16.39% DD, 472 trades**, with **15.6%** concentration.

There is substantial headline return to trade away for lower risk. But choosing a lower-DD persisted genome is not automatically the best solution: **370 TOP3** has **18.62% CAR and −9.22% DD**, while its best five trades supply **44.7%** of net P&L.

#### B1. S1 plus a “failed drift” capital-release exit

**Rule.**

- At session 10 after entry, halve the position if its cumulative return minus its sector benchmark return is nonpositive.
- At session 20, close the remainder if that relative return remains nonpositive.
- Otherwise retain the parent’s exit.
- Never extend a parent holding period; never immediately re-enter the same earnings event.

Proposed genes: `drift_review_days = 10`, `failed_drift_exit_days = 20`, `relative_progress_floor = 0`.

**Added edge hypothesized.** Stop financing positions that have not demonstrated the expected post-announcement continuation. This targets holding time and failed-event exposure rather than new entry selection.

**Data/feed feasibility.** Entry/event identifiers, daily adjusted prices, sector classification and benchmarks. These are plausibly available. Position-level return paths and existing holding periods are missing; if the parent normally exits before day 10, this overlay is irrelevant.

**Capital and overlap.** Reduces the original position’s capital-days. Account-level utilization falls only if released cash is not immediately refilled. Expected to reduce persistent overlap with mid DS **389** and Insider **430**, although common earnings-season exposure remains.

**Falsification.** The flagged positions’ returns **after** day 10/day 20 must be weak enough to justify exiting. Reject if delayed drift is important and the overlay systematically sells before recoveries, or if freed capital produces no account-level efficiency improvement after realistic redeployment and costs.

#### B2. S1 plus publicly disclosed insider confirmation for sizing

**Rule.**

`size_multiplier = 1.0 if qualifying_insider_cluster_public_within_30_calendar_days else 0.5`

Keep the EarningsDrift entry unchanged. Confirmation means information available by the entry decision, not insider transactions filed afterward.

**Added edge hypothesized.** Test whether earnings continuation is more dependable when management has recently committed its own capital. Use the accepted **430 S1 signal**, not fitted Insider S2/S3 rules.

**Data/feed feasibility.** Earnings event timestamps, insider identities, open-market purchase classifications, and filing/publication timestamps. FMP plausibly carries the underlying records; reliable historical availability timestamps and deduplication need verification.

**Capital and overlap.** Reduces unconfirmed exposure relative to **370/424 TOP1**. It will likely **increase conditional overlap with 430**, especially when those clusters already generated positions. Treat a shared company as one account exposure; do not fund two full positions.

**Falsification.** Confirmed events must improve downside or capital-day productivity over unconfirmed events after controlling for sector, size, and announcement month. Reject if confirmation merely identifies the same few profitable companies or if the shared-account result is inferior to operating the parents separately under the issuer cap.

**Priority:** test B1 first. It does not deliberately concentrate the strategy into another expert’s holdings.

---

### C. FMPRating: avoid stale exposure and use independent confirmation conservatively

**Numerical anchor.** Large **390 TOP1** reports **15.99% CAR, −9.03% DD, 257 trades, PF 2.74**, and **32.6%** top-five concentration. **358 TOP2** reports **11.33% CAR, −9.11% DD, 224 trades**. **390 TOP5** has **12.09% CAR, −8.66% DD**, but only **123 trades**; fewer trades do not establish less capital use.

#### C1. S1 plus freshness sizing and duplicate-event suppression

**Rule.**

- `duplicate_issuer_window = 5 trading sessions`: repeated rating/target records within that window cannot add exposure to an already-open issuer position.
- For an otherwise valid entry, `stale_move_size = 0.5` if price has already advanced more than **one pre-announcement ATR** from the last tradable price before publication.
- Preserve the existing exit; do not add analyst-specific “skill” weights.

**Added edge hypothesized.** Avoid paying full size for information already incorporated into price, and avoid treating a burst of related analyst updates as independent conviction.

**Data/feed feasibility.** Original publication timestamps, analyst/broker identifiers, revisions, prices and pre-event ATR. FMP plausibly carries rating/target records, but timestamp precision and revision history are unproven here. Daily-only records require conservative next-session execution.

**Capital and overlap.** Limits clustered deployment and repeated issuer exposure. May reduce synchronized entries with large DS **375** and FactorRanker **362**, but the effect is unmeasured.

**Falsification.** Reject the stale-move rule if already-advanced entries have stronger subsequent continuation and equal or better downside-adjusted returns. Reject duplicate suppression if the parent never pyramids or if independent follow-on updates materially improve exposure productivity. Evaluate only where Rating coverage exists—2022 onward.

#### C2. S1 plus a FactorRanker downside veto on size

**Rule.**

`size_multiplier = 0.5 if current_large_cap_factor_percentile < 20 else 1.0`

Apply to **390 TOP1** without changing the rating-triggered entry. Test separately from C1.

**Added edge hypothesized.** Avoid full-size analyst enthusiasm in companies whose broader cross-sectional characteristics remain poor.

**Data/feed feasibility.** Point-in-time **362** factor ranks and constituent data. Internal rankings are plausible; historical availability of fundamentals is essential. If Rating information is already embedded in FactorRanker, strip out that component for this test or acknowledge the duplicated information.

**Capital and overlap.** Lowers gross deployment but potentially **increases resemblance to 362** among retained holdings. It is acceptable only if the improved downside compensates for reduced diversification.

**Falsification.** Bottom-quintile factor names must underperform the retained rating trades on the intended downside measure. Reject if the combination merely loads more heavily on the same sector/style winners without improving matched-exposure account outcomes.

---

### D. FactorRanker: add portfolio constraints where the classic risk manager is bypassed

**Numerical anchor.** **362 TOP1** has **12.67% CAR, −12.04% DD, 1,279 trades, PF 1.60**, and average trade **0.06** in the supplied units. TOP1–TOP5 span **11.33–12.67% CAR** and approximately **−11.67% to −12.04% DD**.

The design challenge is not extracting another percentage point of CAR. It is controlling portfolio exposure and preserving a relatively small per-trade margin after trading costs.

#### D1. Existing ranking plus a native constrained-allocation overlay

**Rule.** Preserve the factor ranking and existing eligible basket, but apply:

- `factor_family_gross_cap = 25%`
- `sector_cap = 25% of the factor sleeve`
- `account_issuer_cap = 3%`
- `cross_expert_duplicate_policy = residual_capacity_only`

If Rating or DS already occupies an issuer’s capacity, FactorRanker may use only the remaining capacity. Allocate released budget across other **already eligible** basket constituents within caps; otherwise hold cash.

These controls must operate in FactorRanker’s actual allocation path. Its `riskatr` name is not evidence that classic risk-manager limits apply.

**Added edge hypothesized.** Reduce concentration and duplicated exposure while preserving cross-sectional breadth. This adds portfolio construction discipline rather than an unproven new predictor.

**Data/feed feasibility.** Daily desired and actual weights, sector classifications, and consolidated positions across experts. These are mostly internal account data; historical portfolio snapshots are missing.

**Capital and overlap.** Explicitly bounds intended utilization and same-name overlap with **375/390**. Sector limits may reduce common shocks, but neither the limits nor different expert labels establish low return correlation.

**Falsification.** Replay the combined account. Reject the claimed efficiency improvement if constraints remove profitable breadth, induce costly rebalancing, or fail to improve drawdown relative to matched-exposure scaling. Given **1,279 trades** and average trade **0.06**, assess costs in actual dollars and traded notional; the supplied trade metric’s units are insufficient for a basis-point break-even calculation.

---

### E. DeterministicScorer: use it as a controlled, non-duplicative portfolio component

**Numerical anchor.**

- Large **375 TOP2:** **20.51% CAR, −13.90% DD, 514 trades**, top-five share **13.6%**.
- Mid **389 TOP1:** **20.13% CAR, −11.75% DD, 227 trades**.
- Mid **389 TOP4:** **13.95% CAR, −9.03% DD, 236 trades**.
- Small **360 TOP1:** **12.87% CAR, −9.19% DD, 335 trades**, top-five share **21.8%**.

#### E1. S6/S2 plus an event-risk sizing overlay

**Rule.** For existing DS entries:

`size_multiplier = 0.5 if next_known_earnings_release_within_5_sessions else 1.0`

For existing holdings, separately test the pre-earnings half-trim used in A1. Leave DS exits and score thresholds unchanged.

**Added edge hypothesized.** A broad composite score may remain favorable even when a near-term binary event dominates risk. Reduce that event exposure without requiring DS to predict earnings surprises.

**Data/feed feasibility.** DS entry timestamps, historical earnings schedules, position histories and announcement-gap returns. Feed feasibility is plausible; point-in-time schedules remain a prerequisite.

**Capital and overlap.** Releases pre-event capital for post-event EarningsDrift, creating a possible temporal complement between **389** and **370/424**. It can similarly reduce shared exposure between **360** and small Insider S1. This is a hypothesis, not observed diversification.

**Falsification.** Reject if DS’s event-window returns are disproportionately profitable without disproportionate downside, or if the parent rarely holds through earnings. Require actual combined-account capital availability improvement, not just less exposure in the DS ledger.

#### E2. S6/S2 plus “event expert owns the duplicate” allocation

**Rule.**

`DS_new_order = min(parent_order, remaining_account_issuer_capacity)`

When a qualifying Insider, EarningsDrift, or Rating position already occupies issuer capacity, DS adds no duplicate exposure. Do not force an existing DS position to churn merely because a second expert subsequently agrees.

**Added edge hypothesized.** DS contributes opportunities outside the event specialists rather than spending scarce capital expressing the same idea twice.

**Data/feed feasibility.** Consolidated position and signal timestamps; no new external feed required. Exact overlaps are absent.

**Capital and overlap.** Directly limits additional same-name exposure: **360 versus 414/447**, **389 versus 430 and 370/424**, and **375 versus 390/362**. It does not remove sector, beta, or style overlap.

**Falsification.** Reject event-expert priority if displaced DS exposure has better incremental net P&L per capital-day than the exposure receiving priority. Compare against a neutral proportional-sharing policy; do not assume event experts deserve priority simply because their narratives are more specific.

---

### F. FMPSenateTraderWeight: no funded proposal from this output

**There are no SenateTraderWeight jobs or ranks supplied, and it is not among the accepted families.** There is therefore no numerical basis for assigning it a strategy sleeve.

At most, reserve an unfunded future ablation: a publicly disclosed congressional purchase could act as a sizing annotation on an existing DS or FactorRanker entry, never as evidence of a timely original trade. Required data include transaction date, public disclosure timestamp, amendments, purchase/sale classification, and amount range. Existing feed coverage is not established.

Measure the actual reporting lag from each record's transaction and public-availability timestamps; do not assume a universal 30–45 day delay. Falsification must use returns after public availability. Any apparent benefit existing only from the politician's transaction date is unusable. Do not consume the untouched holdout testing this before establishing a credible design-sample case. This is a dataset-timing requirement, not a statement of reporting law.

### Development order and portfolio decision

After parent reproduction and a shared-account baseline, I would proceed in this order:

1. **Insider S1 position-risk cap**, using **414 TOP5** as the conservative parent.
2. **EarningsDrift failed-drift exit**, using **370 TOP1**.
3. **Consolidated issuer allocation**, including native FactorRanker constraints.
4. **DS event-risk sizing**.
5. Only then test the cross-expert confirmation variants.

The confirmation variants are lower priority because agreement often **increases concentration**, whereas the operator wants several strategies sharing capital efficiently.

Before choosing the final combination, measure every pair’s dollar-weighted same-name overlap, sector overlap, daily return correlation, and joint losses on the account’s worst days. Report average, 95th-percentile, and peak aggregate capital use. **This table cannot tell us whether several attractive standalone candidates actually fit into one low-drawdown account.**

## 2. What would falsify this?

1. **One preregistered evaluation on demonstrably unused data:** `tools/grid_goal2020.sh:10–14` designates 2026-H1 as a holdout and configures training through 2025-12-31. That establishes intent, not proof it remains untouched on 2026-09-06. Check experiment and human-selection history before using it. If it has influenced design, label it validation data and reserve a genuinely unused interval or forward paper period. Freeze the shortlist, parent comparators, costs, allocation rules, and failure criteria before scoring. Evaluate paired overlay-minus-parent outcomes and the consolidated account. Six months cannot establish steady annual performance; sparse-event results may remain inconclusive.

2. **Public-availability and execution replay:** enforce actual filing/publication timestamps, conservative next-session execution where timing is ambiguous, realistic spreads/slippage, and gap fills. Disappearance of profitability under information that was actually tradable falsifies deployability.

3. **Ablation against matched-exposure scaling:** remove each overlay individually and perturb its few thresholds modestly. If gains are explained by lower exposure alone, depend on one precise threshold, or vanish after shared-account constraints, reject the claimed incremental design edge.

## 3. Independent analysis: what I would change before implementation

### 3.1 Verify the engine before interpreting strategy economics

The current classic RM reads `atr_risk_budget_pct` inside `_ensure_safeguard_stop`, while `_risk_atr_quantity` reads `risk_per_trade_pct` as the sizing budget (`packages/common/ba2_common/core/TradeRiskManagement.py:1202`, `:1255`). These are reversed relative to the dedicated budget's documented purpose. My earlier isolated source probe produced $2,100 stop exposure on a $1,000 configured dedicated budget, even with a notional cap. The relevant source was rechecked for this update and still has that wiring.

This matters directly to A1 and to comparisons labelled ATR versus notional. Do not conclude that a sizing mode lacks value from matching result rows until the saved setting values, cap binding and code version are known. Nor can we conclude that the cited historical jobs were affected: their runtime revisions and use of the dedicated setting were not verified. Reproduce the original parent first, then rerun it on the corrected engine; distinguish the engine correction from the overlay's incremental contribution.

Backtests also reconcile conflicting ruleset and safeguard stops through `reconcile_protective_stop`, whereas the inspected live submission does not use that same helper. Verify the resulting protective legs in both execution paths before comparing stop-based overlays. Shared signal calculations alone do not establish deployability.

The ML leakage findings in the separate audit apply to the inspected ML preparation paths. They are **not evidence that these deterministic-expert grid jobs consumed leaked ML targets**. Keep the two result populations separate unless job provenance proves a connection.

### 3.2 Make the common account the unit of design

The draft's family and issuer caps are useful experiments, but lack an aggregate admission rule. Before a new entry, compute remaining capacity from consolidated marked holdings **plus pending opening-order reservations**. Normalize share classes and aliases to an issuer where the data supports it. Define the policy for opposing positions explicitly; net exposure alone can conceal gross risk.

For a long-only equity experiment, admission should be bounded by the minimum of parent demand, remaining family capacity, remaining account gross capacity, remaining issuer capacity and available buying power after reserves. State all capacities in dollars of current account equity. Missing holdings, prices or reservations must block a new allocation rather than create apparent free capacity.

If multiple experts decide on the same timestamp, evaluate the batch together with deterministic proportional allocation as the neutral comparator. A sequential `remaining_capacity` rule otherwise rewards whichever expert runs first. Reservations must be created atomically with admission, released on cancellation/rejection and reconciled after partial fills. Price appreciation above a cap needs an explicit policy—block further buys or rebalance—rather than accidental churn.

Use virtual sleeves to attribute P&L, but one real cash/buying-power ledger for fills. Existing helpers in `packages/common/ba2_common/core/portfolio_allocation.py` handle allocation arithmetic and order impacts; their presence does not prove an atomic cross-expert issuer limit exists in both the classic and FactorRanker paths.

**Revised priority:** establish the common-account baseline before ranking A1, B1 or D1 as portfolio improvements. Then test A1 and B1 individually. Event-expert priority in E2 is a later hypothesis; proportional sharing is the starting comparator.

### 3.3 Several overlays need more precise definitions

| Proposal | Independent assessment | Required refinement |
|---|---|---|
| A1 Insider risk cap | Useful first sizing experiment; the stop-based formula is not itself gap-aware. | Call it a notional and stop-risk cap. Separately stress adverse opening gaps. Apply the account-dollar budget at the actual protective stop, after quantity rounding; verify that the cap binds often enough to test. |
| A2 Insider + DS | Plausible distress ablation, not independent confirmation by default. | Rank against the contemporaneous eligible universe in the same band. Specify ties, minimum coverage and missing scores; never turn missing into bottom-quintile automatically. Record constituent coverage and retain an unchanged-parent comparator. |
| B1 Failed drift | The clearest capital-release hypothesis. | Define session counting, sector total-return benchmark, partial-exit quantity rounding and next-tradable-bar execution. A decision using session-10 close cannot fill earlier that session. Persist issuer + report/event identity to prevent immediate re-entry. |
| B2 Earnings + insider | High overlap risk; keep lower priority. | Use clusters public by entry, distinguish new information from reused cluster records, and evaluate incremental account P&L after shared-cap allocation. |
| C1 Rating freshness | Mechanistically plausible but the proposed event anchor may not exist in this expert's signal. | Establish which dated rating/target record caused the recommendation. If the score combines several records, define age of the underlying evidence and component weights rather than inventing one announcement timestamp. |
| C2 Rating + FactorRanker | Cross-sectional veto can change style exposure more than improve selection. | Log which factor caused the veto and compare sector/size-matched trades; isolate active factor components before claiming independent evidence. |
| D1 Factor constraints | Necessary path-specific integration, with some constraints already present. | Reuse native gross/name limits; add sector and consolidated residual capacity with an explicit account-to-sleeve conversion. |
| E1 DS earnings trim | Sensible event-exposure experiment only when historical schedules are known. | Measure actual event-window holdings first. Fixed-fraction trims and re-entry rules must avoid repeated halving on consecutive daily runs. |
| E2 Duplicate ownership | Allocation experiment, not a new predictive signal. | Compare proportional sharing and event priority using the same timestamped candidate batches. Do not give credit for avoided exposure without measuring the displaced opportunity. |

For B1, preserve two distinct views: the original entry cohort's subsequent price path (including the path after an overlay exits) and an executable account replay with redeployment. The former tests whether failed drift predicts weak continuation; the latter tests whether freed capital actually helps. An exit alone cannot establish both.

### 3.4 Existing strategy behavior changes what counts as a new overlay

**S6 already targets capital release.** Its current builder contains an always-on `days_opened` exit with a searched threshold of 10–30, plus toggleable tight entry TP/SL (`testplatform/ba2test_launcher.py:1833–1876`). A generic new time stop may duplicate that mechanism. Inspect the saved rule and the engine's actual day-count convention before comparing it with the draft's trading-session thresholds. S1 instead uses conviction tiers and a target-anchored bracket in the current builder; neither name defines one immutable strategy across revisions.

**FactorRanker already caps gross and per-name weights.** `construction.long_only_top_n` redistributes excess only among selected names and holds residual cash when all hit their caps (`construction.py:11–63`). D1 is therefore an extension of native construction, not the first concentration control. `portfolio.py:239–246` uses expert virtual equity when no equity argument is supplied. A 25% account ceiling must be converted to a sleeve weight using the actual denominator: with virtual equity already equal to 25% of account equity, setting native gross exposure to 0.25 deploys only 6.25% of the account. Do not scale twice.

**Verify that FactorRanker's ranking selects anything.** `FactorRanker/__init__.py:283–318` explicitly warns when `top_n >= screener_max_stocks`. If every eligible name is held with equal weights, changing factor weights cannot alter selection. Score weighting may still change sizes, so this is conditional, not universal inertness. Before using job 362 as a ranking parent or a veto, record eligible count, selected count, weighting mode and actual weight changes after factor perturbations.

**The experts share information.** FactorRanker combines momentum, value, quality and PEAD (`FactorRanker/__init__.py:39–43`). DeterministicScorer has a dated analyst section (`DeterministicScorer/analyst.py:178`), although the module documents its default weight as zero. The active genome decides whether that overlap matters. FactorRanker plus EarningsDrift may reuse earnings surprise; DS plus Rating may reuse analyst evidence. Separate signal overlap, same-name holdings overlap and correlated losses: none is a substitute for the others.

**EarningsDrift scores surprise and freshness, not a calibrated success probability.** `FMPEarningsDrift.py:162–168` builds confidence from fixed surprise and recency terms. Equal confidence thresholds across experts do not represent equal win probabilities. For S1 conviction tiers, plot acceptance and outcomes against each expert's native components before interpreting a high-confidence tier as more reliable.

### 3.5 Tighten the measurement and promotion criteria

Report capital-days in dollar-days as `sum(daily gross marked exposure × elapsed days)`, with net P&L and P&L per dollar-year of exposure. Report pending buying-power reservations separately and include them in admission constraints. Use one declared convention for weekends and non-trading days. Capital-day productivity is a diagnostic ratio, not an account return and not a tail-risk measure.

Exposure matching should use a scale chosen on development data and frozen before validation. Choosing the scale using the final holdout's average exposure is acceptable only as a retrospective attribution diagnostic, not as a deployable comparator. Report changes in turnover, event exposure, issuer breadth and rejected opportunities alongside average exposure; average matching alone does not equalize risk.

The proposed 20% improvement / 80% P&L retention hurdles are screening preferences, not statistical evidence. Report paired monthly differences and uncertainty using time blocks that preserve overlapping holdings; resample the experts together for account comparisons. Trade counts overstate independent evidence when several entries share one issuer or event. Add leave-one-issuer/event-cluster-out diagnostics and report concentration against both gross winning P&L and net P&L, since a small net denominator can inflate top-five shares.

Do not run every A–E variation and present the best one as a preregistered success. Count all tested variants, choose a small initial set, separate development from selection, and use the unused evaluation interval only after freezing the design. Inconclusive evidence is a valid outcome; sparse six-month event samples need not produce a promoted strategy.

### 3.6 Concrete next research deliverable

Produce one reproducible parent-and-overlay comparison bundle before new broad optimization:

1. **Manifest:** job ID + saved rank, complete decoded rules and settings, code/data version, universe membership by date, execution costs, calendar and evaluation-history record.
2. **Baseline ledger:** daily common-account equity, gross/net holdings, pending reservations, issuer/sector exposure, fills, rejected candidates and event identifiers. Reconcile it to parent trades and net P&L.
3. **Small experiment set:** unchanged parent, exposure-scaled parent, A1 alone or B1 alone, then the combined account with neutral proportional issuer allocation. Cross-expert confirmation comes later.
4. **Decision sheet:** net account return, drawdown, underwater duration, capital-days, turnover, event/issuer concentration, incremental P&L and uncertainty. State pass, fail or inconclusive against the frozen objective.

My preferred research direction remains **smaller well-defined positions, timely release of unproductive capital, and consolidated duplicate-exposure control**. The independent correction is that these must first be made executable and measurable on a shared account. The inherited standalone rankings do not yet select the best portfolio.

### Review scope

Reviewed the original design document, the companion strategy summary, current grid driver defaults, S1/S2/S6 builder definitions, classic sizing settings, FactorRanker construction/execution and selected EarningsDrift/DS signal code. Numerical job results, historical genomes, live account allocations and point-in-time schedule datasets were not loaded or recomputed. No strategy backtest or full test suite was run for this document-only update. Source references are repository-relative and line numbers reflect the reviewed working tree.
