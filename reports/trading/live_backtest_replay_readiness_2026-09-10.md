# Can September 10 production trading be replayed?

**Partly. We have useful recorded decisions, but not a complete cache of the
inputs needed to rerun all experts and reproduce live trading exactly. A
targeted prewarm is required for a fresh historical backtest. It will not, by
itself, recover the exact inputs that live saw.**

Scope: production 8081, September 10, including its four entry-analysis experts
and the existing positions reviewed by mid DS. Large DS had no scheduled entry
and no positions that day. This is a readiness audit, not an executed backtest
or warmup. No production data, trading code, historical results, or existing
price/provider caches were modified. All provider-fetch probes used fake data.

## Clarification: different retrieval paths versus different inputs

Different endpoints are not, by themselves, a strategy discrepancy. Historical
reconstruction is an intentional way to obtain an equivalent input when a live
snapshot has no historical endpoint. The earlier conversational description of
FMPRating as a "confirmed mismatch" was too strong: this audit has not measured
a material difference between contemporaneous live and reconstructed values.

Production Rating uses `target_price_type=median` and a 180-day reconstruction
window. Its `min_price_targets_per_quarter=0`, so the different target-count
windows do not currently affect that gate. The relevant comparison is the
actual median/other target fields consumed by its calculation and the rating
buckets at the same evaluation time. Endpoint names alone do not settle this.

EarningsDrift's calendar shortcut is likewise a coverage/field-equivalence risk,
not a demonstrated signal discrepancy today. Compare report date, reported EPS,
estimated EPS and missing-report behavior against its per-symbol history.

One concrete temporal-data limitation is the deployed Insider model's forward
EPS estimates. `FMPCompanyDetailsProvider.get_earnings_estimates()` filters rows
by **fiscal period end** and selects the next periods; it does not select the
estimate revision that was available at the historical evaluation time
(`packages/providers/ba2_providers/fundamentals/details/FMPCompanyDetailsProvider.py:855`).
Consequently a later API/cache snapshot cannot certify what an earlier live run
knew. The estimator calculation is shared, but a historical rerun may consume
revised values. This is a point-in-time data limitation, not merely an endpoint
difference. Its effect on the existing model-mode backtests has not been measured.

No different strategy formula has been established for Insider or DS in this
audit. Data equivalence still requires checking normalized input fields, units,
periods and availability at the same timestamp, then comparing recommendations.

## What is available now

I exported a read-only snapshot to
[replay_2026-09-10/live_inputs.json](replay_2026-09-10/live_inputs.json):

- 103 analysis rows: 92 completed and 11 skipped.
- 92 recorded recommendations, including quote, action, confidence, expected
  profit, risk and time horizon.
- 156 analysis outputs, including the live FMPRating consensus and
  upgrade/downgrade responses, plus EarningsDrift/Insider analysis narratives.
- The six enabled expert configurations and ordered entry/open-position rules.
- Relevant account-1 order records, current expert transaction state, and
  today's logged screener selections and sizing inputs.

Account credentials and broker order-ID columns are excluded. This is a snapshot
of what remains in the database, **not** a newly invented opening-bell account
snapshot. Today's recommendations are sufficient to test rules against the
signals actually issued, without asking FMP to regenerate those signals.

The existing `tools/capture_live_parity_fixture.py` and
`testplatform/backend/app/services/backtest/parity_harness.py` are useful
foundations, but not a complete production replay driver. The capture script
has an old hardcoded database path/window. The harness uses a flat $1m account,
a fixed clock and recorded recommendations to test order direction; it does
not establish equality of actual quantities, margin state, brackets or fills.
It also classifies funding from order records rather than replaying the full
sequence of submissions and rejections.

## Cache coverage actually measured

95 distinct symbols were inventoried: today's analyzed symbols, including
skips/open positions, plus SPY. This is a scope for replaying the observed
selections, not the entire market-wide candidate universe.

| Required data | Production cache | Shared backtest cache |
|---|---|---|
| Daily Parquet | 8/95 symbols present | 95/95 present |
| Daily files reaching September 10 | 0/95 | 0/95 |
| Five-minute Parquet | 0/95 present | 95/95 present |
| Five-minute files reaching September 10 | 0/95 | 0/95 |
| Required FMP history payloads for deployed settings | 0/224 present | 216/224 present |
| Required history files refreshed on September 10 | 0 | 0 |
| Generic `provider_cache` rows in prod DB | 0 | Separate host-owned store; not assumed shared |

The cache roots differ:

- Production: `C:/Users/basti/Documents/ba2_trade_platform-prod/cache`.
- Normal backtest/shared cache: `C:/Users/basti/Documents/ba2/common/cache`.

This matches the desktop launcher. Pointing a backtest at the production folder
alone does not expose the much larger shared cache. Pointing it at shared alone
does not pick up the latest production SPY/DS bars.

Of shared daily files, 65 end June 30 and 26 end July 15; the remaining four
end August 10, August 24, August 28 and September 4. Shared five-minute files
end June 29, June 30 or July 14. Example: SANA's daily and five-minute history
both stop June 30; NAVN daily ends July 15. Production SPY reaches September 9.

There are also 338 legacy CSV files in prod and 986 in shared. Their existence
does not satisfy the backtest's native Parquet reader; this inventory does not
count them as verified substitutes. A file reaching a date would still need
bar completeness and finalization checks; this audit found the more basic
missing-tail problem first.

The 216 existing history payloads were last written in June–August, and nine
are empty sentinels. Those may have correctly meant “no data” when fetched,
but they are not evidence that there is still no data in September. Hermetic
backtesting deliberately ignores file age, so a run can reuse stale histories
without raising a cache-miss error. Existence checks alone are insufficient.

Eight required history keys are absent from shared:

- SPTX: `grades_historical`, `price_target`.
- DPC and FPS: `insider_v2`, `past_earnings_quarterly`,
  `earnings_estimates_quarterly` for each symbol.

The shared screener metric-store partitions inspected end at **2026-06**;
there is no September partition. Therefore rerunning the normal historical
screener is another gap, even after warming the analyzed symbols. The live
final selections are saved in the exported logs, which lets the first replay
hold those selections fixed. It does not recreate all rejected candidates or
their exact live quote/volume inputs.

Detailed paths, dates and gaps are in
[cache_inventory.json](replay_2026-09-10/cache_inventory.json).

## Why “live caches everything” does not hold

`fmp_history_disk_cached()` explicitly bypasses disk reads/writes unless the
backtest freeze flag is set
(`packages/providers/ba2_providers/fmp_common.py:308–333`). Live callers may have
short-lived in-process memos, but that does not populate the reusable historical
JSON store. The empty production `provider_cache` table corroborates that the
generic event store is not preserving these inputs here.

The distinctions by expert matter:

| Expert | Live inputs retained / behavior | Historical rerun requirement or difference |
|---|---|---|
| FMPRating | Current consensus and upgrade/downgrade snapshots saved as analysis output; recorded broker quote | Reconstructs target statistics from dated analyst targets and rating buckets from grades. Deployed target choice is **median**, with a **180-day** reconstruction window. Equivalence to the live snapshot must be measured; warming alone does not establish it. |
| EarningsDrift | Analysis narrative and resulting recommendation; live often uses a bulk earnings-calendar shortcut | Backtest calls per-symbol earnings history. The complete raw calendar response is not archived as a replay bundle. |
| Insider | Narrative/cluster summary and recommendation; deployed `expected_profit_mode=model` | Needs insider transactions **plus quarterly earnings and earnings-estimate histories**. Full provider bundles are not persisted. Filing-date filtering is also explicit in the historical path. |
| DeterministicScorer | Score-section outputs and some production daily history | Needs a long daily lookback, statements, analyst/earnings inputs and benchmark history. The actual analyzed mid-DS configuration uses analyst and earnings sections. Its macro mode is off, although the gather stage still reads macro data. |

FRED is separate too: shared VIX/credit observations examined end August 7,
the yield-spread series August 10, and unemployment at July. These were fetched
August 11. Macro mode is off in both deployed DS experts, so refreshing macro
inputs should not be confused with a required change to their decision formula.

Live analysis and RM use broker quotes. Ordinary backtests use bar-based prices
and their configured fill model. Five-minute bars can provide a closer execution
comparison, but cannot recover the exact 09:32 quote or broker acceptance/fill
sequence. Newly fetched/revised estimates also do not recreate an earlier
provider vintage. Use the recorded values where exact decision inputs matter.

## Two prewarm tooling gaps

**1. Backend/API prewarm can report success without creating the history cache.**

`testplatform/backend/app/services/data_build_handler.py:329` enters
`frozen_ttl_cache()` outside `ThreadPoolExecutor`, without enabling the flag in
each worker. The flag is thread-local. Worker fetches therefore follow the
live branch and do not write `fmp_history` files. The CLI implementation already
has the correct worker initializer and empty-result sentinel handling
(`testplatform/ba2test_launcher.py:689`).

I reproduced these two patterns using the real cache helper, one fake payload
and a temporary directory: both returned data, the backend pattern wrote **no
file**, and the CLI pattern wrote the file. There were zero network calls.
Evidence: [prewarm_thread_probe.json](replay_2026-09-10/prewarm_thread_probe.json).

**2. Standard Insider prewarm is insufficient for this deployed model mode.**

Its CLI fetcher warms only insider transactions. The actual model-based target
also calls `fetch_estimator_inputs()` for earnings and estimates
(`packages/experts/ba2_experts/analyst_target_model.py:86`). Those must be warmed
explicitly. The backend DS prewarm also only refreshes FRED, whereas the CLI DS
fetcher warms its per-symbol financial histories. These entry points are not
interchangeable.

## Concrete replay plan

1. **Keep today's recorded evidence fixed.** Use the exported recommendations,
   selected symbols, rules and quotes to test the rules and bracket logic first.
   Seed positions already open before today's analysis, prior closes needed by
   cooldown rules, expert allocations and the captured sizing budgets. Mark
   missing account-state timing explicitly. A new flat account would answer a
   different question, particularly for margin and simultaneous experts.
2. **Create an isolated replay cache**, reusing the shared historical prefixes
   and the newer production files where applicable. Preserve the source files
   and avoid changing caches used by active GA jobs. Resolve aliases and
   overlapping rows explicitly; do not blindly overwrite the longer file.
3. **Refresh the 95-symbol price tails** through the comparison window, using
   daily bars for indicators and five-minute bars for intraday comparison.
   DS requires about 387 calendar days of warmup by the current handler;
   the other three expert classes have a 60-day floor. Existing historical
   prefixes can largely be reused. For full-session results, wait for the
   session and final bars to be complete; partial daily bars are not final data.
4. **Warm the settings-specific history manifest** in that isolated cache,
   with freeze enabled inside every worker and checked-empty sentinels.
   Refresh the old payloads as well as the eight absent keys. The exact manifest
   contains 224 distinct keys: 40 insider histories, 40 estimate histories,
   76 earnings histories, 28 grade histories, 28 price-target histories and
   12 annual statement histories. RAT's analyst-recency filter is off, so its
   optional individual-analyst-grade history is excluded.
5. **Use frozen live selections for the first comparison.** Rebuilding the
   entire historical screener requires a September metric-store update and
   coverage of its full candidate universe, not just the final 95 symbols.
   Test selection differences separately so they do not hide execution bugs.
6. **Run offline and report differences by stage:** selection, recommendation,
   rule/TP/SL decision, sizing, submit outcome and fill. Check zero unexpected
   network calls and zero skipped symbols caused by cache gaps. Preserve the
   existing backtest semantics; do not change formulas or old results to force
   a match. The missing-TP and stale-pending issues found today remain expected
   live discrepancies until fixed.

Ready-to-use symbol lists and
[history_prewarm_manifest.json](replay_2026-09-10/history_prewarm_manifest.json)
are saved beside the snapshot. The collector is read-only and reproducible:
[collect_inputs.py](replay_2026-09-10/collect_inputs.py).

**Recommendation:** begin with the recorded-decision replay, then do the
targeted isolated prewarm for the historical expert rerun. A broad warmup of
the whole market is unnecessary for the first comparison, and a simple
“rerun today's date” against the current caches would not be a trustworthy
live/backtest comparison.

Implementation specification:
[Live capture, prewarm and backtest replay](../../docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md).
It preserves current expert/backtest semantics and distinguishes exact recorded
input replay from comparison with historical reconstruction.
