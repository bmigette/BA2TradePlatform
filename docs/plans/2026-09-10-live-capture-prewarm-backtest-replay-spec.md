# Live input capture, targeted prewarm and backtest replay

Date: 2026-09-10  
Status: implementation specification; no code or production configuration changed  
Initial scope: production 8081, its six deployed instances across FMPRating,
FMPEarningsDrift, FMPInsiderClusterBuy and DeterministicScorer

## 1. Outcome and constraints

After a live analysis session, the platform must be able to answer:

1. Given the exact information live consumed, does the shared expert calculation
   produce the same recommendation?
2. Does an ordinary historical backtest obtain equivalent information for that
   evaluation time, even when it uses different endpoints or reconstruction?
3. With the same recommendations, rules, quotes and account state, do the
   decision and sizing paths produce the same intended orders and protection?

Different retrieval methods are acceptable. Equivalence concerns the values,
units, periods, coverage and availability of the information entering the
calculation. This project does not presume the historical FMPRating
reconstruction is wrong. No changed recommendation caused by differing expert
inputs has yet been demonstrated in the September 10 audit. The separately
observed live order/protection issues remain separate findings.

Required invariants:

- Preserve existing expert calculations, rules, live freshness policies,
  historical reconstruction and ordinary backtest fill/account semantics.
- Preserve existing backtest results and frozen golden fingerprints. Do not
  add leverage simulation to the ordinary backtest. Replay may inspect the
  existing live margin calculation using recorded broker inputs.
- Capture information already fetched or read by live, including cache hits.
  Recording must introduce **zero additional external requests**.
- Warm only missing or explicitly stale required data. Background warmup must
  not hold an account submission lock or delay scheduled trading to finish.
- Preserve original observations. A later download must never overwrite the
  evidence of what live saw or be presented as a historical estimate vintage.
- Replay is offline and uses an isolated database/account adapter. It cannot
  instantiate a submitting broker or write to production.
- September 10 can provide a partial bootstrap. Missing original observations
  cannot be recovered merely by fetching data after the fact.

## 2. Three comparison capabilities

| Capability | Input and execution | What success establishes |
|---|---|---|
| Recorded expert replay | Exact normalized bundle captured immediately before `_process`, settings, evaluation clock; execute shared expert calculation again | Same calculation and recorded inputs produce the same recommendation. Does not validate `_gather` or historical reconstruction. |
| Historical expert comparison | Normal `analyze_as_of` path against a pinned, warmed historical cache; capture its normalized bundle and compare to live | Measures input and recommendation differences caused by historical reconstruction, coverage, timing or revisions. Different endpoints alone are not a failure. |
| Decision and execution comparison | Replay shared rules and sizing with captured state and event order; compare generated order intent and protection to live records | Validates deterministic decisions. Broker fills/rejections are observations, not a promise that a bar fill model reproduces the market. |

Also support replaying the **live gather path** from a tape of recorded provider
returns. This validates mapping/shortcut behavior separately from processing a
saved normalized bundle. A missing tape response must stop that comparison,
not trigger a real request or substitute a historical endpoint.

Reports must state which capabilities ran. A successful normalized-bundle replay
must never be labelled a complete live/backtest match.

## 3. Store exact observations beside reusable history

Use the production instance's explicitly resolved cache root. Keep existing
Parquet and `fmp_history` formats readable. Add an independent replay store:

```text
<instance-cache>/replay/v1/
  index.sqlite                       # replay index, independent of trading DB
  objects/<hash-prefix>/<sha256>.*    # immutable JSON/Arrow payloads
  sessions/<session-id>/manifest.json
  sessions/<session-id>/coverage.json
```

The host opens this store through an injected service; shared packages do not
choose a production database or absolute Windows path. Resolve roots before
provider imports. A replay worker receives an explicit cache/store handle;
changing an import-time global in the live process is not an isolation mechanism.

Existing `ProviderCache` has useful public/value dates and content deduplication,
but it does not identify each analysis's exact response, complete request,
cache-hit origin or revision selection. Its event-row reader can return several
revisions. Do not silently reinterpret that table or enable it globally as the
live source. Retain it for compatible historical uses; add the observation index
needed for replay. Reuse proven interval handling and Parquet codecs where safe.

### Stored records

| Record | Required contents |
|---|---|
| Session | Schema version; instance/session IDs; UTC start/end and exchange timezone; deployed app/package versions, source revision and dirty-state identity; resolved configuration hashes; capability/status flags |
| Analysis | Analysis ID, attempt ID, expert/instance, symbol, enter/open-position use case, scheduled/actual times, resolved expert settings and provider identities; normalized input object; recommendation or skip/error; consumed clock values |
| Provider observation | Provider/method and sanitized request identity; ordered invocation ID and analysis links; exact returned payload; response classification; content hash; fetched/observed/consumed times and available source timestamps; cache-hit provenance |
| Selection | Screener settings and schedule; actual candidate inputs already read; filtering/ranking results, reasons and ordering; selected symbols; held symbols added to analysis; full-universe coverage flag |
| Decision event | Recommendation links, ordered rules/settings snapshots, current transaction/order state read, rule results, quote/ATR inputs, sizing operands and result, protection intent, submit attempt/outcome, subsequent broker updates |
| Coverage | Required dependency, actual artifact hash, interval/window, validation results, provenance, freshness, known-empty status, and missing reason; status per comparison capability |

Request identity includes provider, method/endpoint, symbol or batch universe,
range, frequency, interval, pagination, adjustment/session mode and all
result-changing arguments. Credentials are excluded; use an opaque source
profile ID to distinguish feeds/entitlements without persisting secrets.
Associate one shared response with multiple consuming analyses without copying
its bytes. Repeated calls that return different quotes remain distinct events.

Keep both the raw/returned provider data and the exact normalized `_gather`
bundle. Some methods perform direct HTTP calls, some return cached objects,
and some bypass `ProviderBundle`. Capturing only HTTP cache misses or only
`ProviderBundle` methods is insufficient. In the first delivery, normalized
bundles are mandatory; raw capture coverage is explicit rather than assumed.

Serialize enums, UTC/timezone metadata, numbers, nulls, DataFrame index/column
order and dtypes without rounding or silently dropping rows. Store arrays/frames
as typed Arrow/Parquet objects. Copy/freeze the bundle before later mutations.
Use a versioned codec with round-trip tests; reject unsupported types as capture
gaps. Do not coerce a missing price into zero. Expected recommendations and order
outputs are stored separately and must not be fed back as replay inputs.

### Observation time and revision rules

- `value_time`: what a row concerns, e.g. fiscal period, transaction or bar time.
- `published_at`: provider-supplied public availability time, if actually known.
- `fetched_at`: when this process obtained the response from its external source.
- `consumed_at`: when the particular live calculation read it, including a cache hit.
- `first_observed_at`: earliest recorded observation of this exact revision.

Unknown timestamps remain unknown. File mtime is not publication time. Never
assign a fiscal period end as an estimate's publication time.

Exact replay selects artifact hashes referenced by the analysis. A future
point-in-time archive reader selects an explicitly eligible recorded revision;
it does not combine duplicate revisions or use a response first observed later
as proof it was available earlier. Externally fetched historical event data may
support reconstruction when it has valid publication dates, but its later
acquisition is still disclosed and does not prove absence of later corrections.

Legacy history files may be reused as reconstruction inputs with provenance
`legacy_history_unknown_revision`. That designation cannot satisfy exact live
observation coverage.

### Atomicity, concurrency and failure behavior

Write immutable objects to temporary files, verify hashes, atomically publish,
then commit references in the dedicated SQLite index. Use process-safe writes,
unique request IDs and bounded writer queues; thread-only locks are insufficient
for concurrent warmup processes. A crash before index commit leaves an orphan
object, not a valid complete analysis. Clean orphans only after a grace period.

Normal recording is asynchronous after an immutable copy is made. It never
waits on remote I/O. On queue saturation, disk failure or unsupported data,
leave the existing live trading behavior intact and raise a visible capture
health error. Coverage becomes incomplete. Recording is observational; it must
not turn an otherwise valid trade into a skipped trade or silently mark a gap
as complete. Session finalization waits for persistence separately from trading.

Persist a session/analysis-start marker so interrupted captures can be detected
after restart. Reconcile analysis IDs against the live DB through a read-only
host adapter; missing start markers must not make lost analyses disappear from
coverage totals.

## 4. Capture integration and time handling

At the actual live call site, resolve settings, gather inputs, snapshot the
normalized bundle, run the existing calculation and link its actual output.
Capture skip/error paths as well as successful BUY/SELL/HOLD results.

Introduce a narrow injectable evaluation-clock seam only where necessary to
record/replay time-dependent calculations. Production must retain the same
time-read semantics. Replay supplies the recorded reads in order. Do not turn
live calls into `analyze_as_of(now)` merely to get a timestamp: this would switch
FMPRating and EarningsDrift into different gather branches. Do not freeze the
process-wide clock across concurrent live workers.

A replay adapter can call `_process` with the recorded evaluation time only
after verifying that `as_of` affects time alone for that expert. Otherwise use
the scoped clock seam and preserve its live/historical branch selector. The
capture schema keeps branch selection and evaluation time separate.

Record provider responses at the return boundary, including memory/disk cache
hits. Capture FMP direct helpers, live bulk calendar results and broker quote
reads, not merely low-level HTTP. A cache hit refers to its original observation
when known; otherwise record the returned data with unknown fetch provenance.
Never issue a duplicate fetch just to fill metadata.

## 5. Dependencies for the initial experts

Implement one settings-aware dependency resolver consumed by capture coverage,
CLI prewarm, API prewarm and historical replay. Define explicit methods such as
`required_replay_inputs(settings, rules, universe, window)` returning typed
requirements. Runtime capture also records undeclared reads and flags manifest
drift. A declared-but-unused optional dependency is not a proven live input.

| Expert | Exact live capture | Historical warm requirements |
|---|---|---|
| FMPRating | All consumed target fields and rating buckets, target-count/recency inputs when used, quote, normalized bundle | Dated price targets and grades; individual analyst grades when recency is enabled; price history. Record reconstruction window and target selection. |
| EarningsDrift | Actual calendar or detail response and chosen row, report date, actual/estimated EPS, quote, dynamic/model operands | Per-symbol past earnings; estimates and model earnings when model mode is enabled; required prices. |
| Insider | Insider rows after provider filtering and raw rows where available, filing/trade dates, quote, estimator inputs in model mode | Insider history; model-mode past earnings and forward estimate snapshots; required prices. |
| DeterministicScorer | Exact OHLCV frame and benchmark, statements, section inputs, clock, normalized bundle | Long daily history/benchmark; annual statements; analyst and earnings data required by settings; model inputs when enabled. Account for currently executed macro reads even when their score weight is off. |

Add dependencies from the active rules and risk manager: ATR/price intervals,
earnings conditions, cooldown/previous-close state and any additional provider
reads. The expert-class name alone cannot describe everything a trade needs.
Unsupported expert/provider combinations return `unsupported`, never a green
coverage result. Other experts, including ETF/basket strategies, join through
explicit adapters later; they are not silently covered by this first scope.

Initial September 10 inventory provides **95 symbols including SPY** and **224
history keys** for the analyzed configurations. These are seed observations, not
hardcoded future requirements. Counts change with settings, positions and
selection. The four mid-DS held symbols and both DS configurations remain
distinct; an inactive instance is not an analysis failure.

Historical EPS estimates need special status: the current provider filters
fiscal periods, not historical revisions. Save live estimate snapshots going
forward. A warmup cannot repair that limitation retrospectively. An ordinary
historical run may still use its existing estimate behavior, but its comparison
must report the provenance limitation without silently changing old results.

## 6. Warmup lifecycle and bandwidth policy

Keep capture and warmup independent. Do not enable `frozen_ttl_cache()` in normal
live analysis to make it write files: it changes cache/freshness semantics and
can serve old backtest data. Add an observation sink to current return paths.
Only a background warmup worker uses the explicit historical writer.

### Lifecycle

1. **Plan without network.** Resolve active settings, scheduled analysis windows,
   held positions, required benchmarks, rules and existing coverage. Inspect
   production and explicitly configured shared roots read-only. Emit the exact
   missing/stale windows, request classes and estimated bytes.
2. **Before analysis.** Warm missing closed-bar lookbacks for known holdings and
   known candidate sets if budget allows. Universe-wide work is not required
   just to capture a live selection. Do not wait for future bars or substitute
   warmed data into the running expert merely to reduce requests.
3. **During analysis.** Persist actual responses and normalized inputs. As each
   screener finishes, preserve its real selection/order and queue only newly
   required historical dependencies. Shared symbols/requests are deduplicated.
4. **After analysis.** Warm remaining histories required by the historical
   comparison, without blocking trades. Mark later snapshot fetches as later.
5. **After session close/provider settlement.** Extend intraday/daily price tails,
   validate completeness, pin comparison artifacts and finalize coverage.
   Exchanges, holidays and DST determine the close; no fixed Paris-time close.

JobManager supplies scheduled work and analysis-completion hooks. Jobs use an
explicit background worker/process, not the trading worker queue at equal
priority. CLI/API operate the same planner and worker service. This specification
does not create a scheduled task or start a warmup.

### Price and history rules

- Fetch missing prefixes, holes and tails. Never refetch an unchanged complete
  multi-year history solely because another expert asks for it.
- Match canonical interval aliases, provider, raw/adjusted convention, timezone,
  session coverage and corporate-action treatment. Do not splice incompatible
  adjusted prefixes into newer tails. A split/revision triggers a bounded repair
  with recorded provenance or an explicit incomplete result.
- Use daily bars for required indicator lookbacks and intraday bars only over
  the execution-comparison window unless a rule explicitly needs more. Do not
  download multi-year five-minute data by default.
- Preserve the partial daily frame live actually saw in its capture. Later final
  daily bars belong to the historical comparison and cannot replace it.
- Validate complete expected session coverage, not just max date or file mtime.
  Account for IPOs, halts and sparse trading without manufacturing candles.
- An endpoint without a range parameter may require a full payload refresh;
  label and budget that exception. Coalesce repeated requests and never retry
  an unbounded full-history request on every missing bar.
- Historical artifacts are versioned/pinned for a comparison run. A mutable
  latest cache may be materialized for existing readers in an isolated folder;
  it is derived from explicitly selected versions.
- A validated empty response is `checked_empty` for a request/window and time.
  HTTP errors, rate limits, partial pagination and malformed responses are not
  empty data and must not be persisted as proof of no coverage.

### Operational policy

New controls are explicit settings with a migration and validation; consumers
must not invent missing configuration values. Proposed pilot configuration:

| Setting | Pilot value / behavior |
|---|---|
| Capture enabled | Off on installation; enable for selected paper instance, then production after validation |
| Warm enabled | Off independently until its plan is reviewed; no deployment-triggered bulk download |
| Warm workers | 2, low priority; share the existing FMP rate gate |
| Warm daily download allowance | 100 MiB incremental background allowance; configurable, not a prediction of actual usage |
| Retention | 90 days for unpinned complete sessions; pinned investigations retained until explicitly unpinned |
| Store quota | Explicit byte cap selected at rollout from measured capture size and available disk; no unlimited implicit value |

Track requests/bytes by endpoint and purpose: normal live, capture overhead
(must be zero network), and warmup. Repeated warmup with unchanged data must
perform zero downloads until a declared freshness/window condition changes.
Budget is shared across warm workers; reserve estimated bytes before dispatch.
Check actual streamed bytes where available. Unknown-size responses reserve
a conservative amount; an unavoidable in-flight overshoot is bounded by active
responses and reported, not misrepresented as a strict wire-level cap.

Rate-limit responses back off; exhausted budgets pause warmup with an explicit
remaining-gap report. Live requests retain priority. Dev/prod sources sharing
the same provider credentials need coordinated warm budgets without putting
credentials in job keys/logs. Freshness policies are provider/data-type specific;
there is no blanket seven-day "fresh enough" rule for replay eligibility.

Retention garbage collection deletes only unreferenced objects within the
resolved replay root. It must respect pinned/exported sessions, active writers
and cross-session references. Quota exhaustion surfaces incomplete capture;
it does not overwrite older evidence or alter order decisions.

## 7. Account state, rules and execution evidence

Exact expert replay is the first deliverable. Sizing replay additionally needs
state captured at the time each decision actually reads it, not a later DB dump.

Capture all existing operands without extra broker queries: equity/cash/currency,
configured factor, broker multiplier/buying power, allocation percentage,
instrument caps, exposure components, pending reservations, existing orders and
positions (including outside-expert exposure), price/ATR, quantity increments,
minimum sizes and rounding rules. Persist the existing budget resolver's result
as an expected output, not as a replacement for its raw inputs.

Record the timestamp/source of each broker snapshot and the local pending-state
version. These may not be synchronized: capture the disagreement instead of
constructing a fictitious coherent balance. Preserve initial state plus ordered
state changes, with per-account submit-lock acquisition/attempt order and causal
links. Wall-clock timestamps alone cannot order simultaneous attempts reliably.

Replay has two explicit checks:

- Recompute the existing live capital mapping/headroom from raw recorded
  operands, then compare to the recorded budget. This can expose a duplicate
  pending reservation rather than hide it by feeding back the final balance.
- Feed the resulting effective budget through the existing sizing decision
  interface in an isolated adapter and compare quantity, side, rule branch and
  TP/SL intent. This adds no leverage formula to the ordinary backtest account.

Record pre-submit ruleset stop/target, safeguard, reconciled protection,
post-fill rebase and broker acknowledgements separately. Compare intended
quantity and protection exactly at their documented rounding precision. Broker
fills, partial fills and rejections form a separate outcome comparison. Feeding
recorded fills into a deterministic trace is labelled trace replay, not an
independent simulation of those fills.

Existing positions, earlier closes/cooldowns, pending entries, schedule, account
allocation state and symbol gating all participate. If required state is missing,
mark that event unavailable for sizing replay; do not seed a flat $1m account or
default unknown balances to make the check pass. Known live/backtest cash/equity
and cap semantics remain visible differences pending their separate decisions.

## 8. Replay interfaces and reports

Add a host-neutral replay contract in `ba2_common`, provider capture/warm adapters
in `ba2_providers`, expert dependency adapters in `ba2_experts`, and separate
live/backtest host services. The runtime store is accessed through that contract.
No shared package imports a live broker or starts an application on import.

Proposed command interfaces, **not implemented commands**:

```text
ba2-test replay inventory --bundle <session-export>
ba2-test replay warm-plan --bundle <session-export> --cache-root <isolated-root>
ba2-test replay warm --plan <plan-json>
ba2-test replay experts --bundle <session-export>
ba2-test replay gather --bundle <session-export>
ba2-test replay historical --bundle <session-export> --cache-root <pinned-root>
ba2-test replay decisions --bundle <session-export>
```

The live host exposes export by instance/date/session, with schema/version
validation, content hashes and relative object references. Export a consistent
finalized bundle; an unfinished export retains an explicit incomplete status.
Do not copy the production trading DB, API keys or broker credentials. Restrict
captured settings/HTTP metadata to an allowlist; sanitize errors as well as
successful responses. Include necessary account values with local opaque IDs.

Replay runs in an isolated process with network disabled at the transport layer
and a broker adapter that cannot submit/cancel/replace real orders. It cannot
fall through to `_get_current_price`, a provider fetch, or production DB lookup.
Every unexpected dependency is a typed replay miss with analysis/request ID.

Reports contain per-analysis field diffs and a summary by stage:

| Stage | Compared fields |
|---|---|
| Selection | Candidate coverage, filters, ranked order, selected/held symbols |
| Expert inputs | Targets/rating buckets; report/EPS fields; insider rows; estimates and revision provenance; OHLCV/statement periods and values |
| Recommendation | Skip reason, signal, confidence, expected profit, current price, risk, horizon and any decision-bearing extra fields |
| Rules and sizing | Branch, eligibility, operands/budget, intended side/quantity, TP/SL and reconciliation |
| Execution | Submit attempts/rejections, partial/full fills, final active protection |

Use statuses `match`, `difference`, `missing_capture`, `missing_history`,
`revision_unknown`, `unsupported`, and `not_run`. Coverage is independent for
each capability. Include HOLD/skipped/failed analyses in totals; never report
100% by dropping unavailable rows.

For recorded replay, numeric equality uses exact serialized values and the
existing output rounding; a narrowly documented floating-point tolerance may
be used for intermediate calculations only. No broad "close enough" tolerance
may hide a changed signal, threshold crossing, share quantity or stop tick.
Historical comparison reports absolute/relative input differences and resulting
decision differences; it does not assume zero difference is always attainable.

## 9. Concrete implementation sequence

| Step | Work and likely locations | Exit criterion |
|---|---|---|
| 1. Contract/store | New `packages/common/ba2_common/core/replay/`: schemas, typed codecs, context/clock, object store/index, coverage and export. Host-injected paths/config. | Immutable round-trip and crash/concurrency tests pass; no provider/broker imports or production DB effects. |
| 2. Live expert recording | Hooks at `_gather`/`_process` in the four package experts; provider return taps in `fmp_common.py`, fundamentals/insider adapters, `StockScreener.py`, OHLCV and broker quote boundary. Live host session coordinator. | Capture on/off produce identical outputs and provider call counts. Every analysis is represented; raw gaps explicitly marked. |
| 3. Expert replay | New service beside `testplatform/backend/app/services/backtest/parity_harness.py`; isolated capture replay and input-diff reports. Extend tooling rather than rely on the old flat-account harness. | Recorded expert decisions reproduce offline, including skips and time boundaries. No historical cache is needed for complete normalized bundles. |
| 4. Shared warm service | Settings/rules dependency resolver; common CLI/API implementation replacing duplicated fetch lists in `testplatform/ba2test_launcher.py` and `.../services/data_build_handler.py`; isolated workers and verified writes. | Insider model dependencies and DS histories covered; current thread-local prewarm failure regression passes; second unchanged run downloads nothing. |
| 5. Historical comparison | Pinned cache materializer, input capture on historical path, coverage/revision validation and stage report. JobManager schedules only configured bounded jobs. | Offline historical run either completes with honest diffs or names every missing dependency; ordinary backtest outputs remain unchanged. |
| 6. Decision trace | Live host hooks in `TradeManager.py`, `TradeRiskManagement.py`, account snapshot/exposure methods and shared evaluator/actions; read-only replay account adapter. | Quantities/protection checked from actual raw state; concurrent/partial-fill trace tests pass; no submitting broker can be resolved. |
| 7. Pilot rollout | Versioned settings/migrations, health UI/log counters, paper sessions, then configured production observation and bounded warmup. | Paper acceptance suite passes; production capture completeness and request/byte measurements reported before claiming readiness. |

Do not edit re-export shims. Reuse existing host migration conventions for live
settings and backend configuration; the new store owns its own schema version.
App/test version bumps follow repository rules when implementation is pushed.

The current backend prewarm enables a thread-local freeze only in the parent;
its workers do not persist histories. CLI initializes workers correctly but has
an incomplete settings-dependent fetch list. Fix both through the shared warm
service. Explicitly propagate/reset context in threads and subprocesses;
process-global empty-sentinel flags must not leak into concurrent live work.

## 10. Acceptance tests

1. **Behavior preservation:** capture on/off under identical provider responses
   and time reads yields identical expert outputs, rule decisions, quantities,
   stop/target values and external request counts. Existing golden fingerprints
   remain byte-identical; run focused live order tests and the blocking backtest
   parity gate for affected paths.
2. **Actual replay:** fixture of each deployed expert/mode, including shared
   response cache hits, skipped coverage, missing EPS and open-position analysis,
   reproduces every recorded decision field offline. Alter one input and prove
   the corresponding field/decision difference is reported rather than masked.
3. **FMPRating:** current median/other target fields and rating counts versus
   dated reconstruction are compared at the same clock/settings. Equal payloads
   yield equal decisions regardless of endpoint; deliberately different payloads
   produce visible diffs. Disabled target-count gate remains disabled.
4. **Earnings/Insider dates:** calendar/detail equality and missing calendar row;
   report/filing boundaries; identical fiscal periods with different estimate
   revisions. A later revision cannot satisfy exact replay of an earlier run.
5. **Clock/price fidelity:** across-midnight and DST cases, repeated quote reads,
   partial daily bars, next-day final bars, alias conflicts and split-adjustment
   mismatch. No finalized close replaces the quote/frame used by live.
6. **Warm correctness:** workers persist and read back hashes; paginated history
   is complete; checked-empty differs from error; model-mode dependencies are
   present; DS per-symbol history is warmed; legacy unknown-revision data keeps
   that status. Repeated unchanged plans download nothing.
7. **Concurrency/failure:** two experts/processes share one warm fetch; mutable
   response objects cannot corrupt recorded bundles; disk full, queue saturation,
   interrupted writes/restart and pinned retention are handled visibly. Trading
   output is unchanged when capture fails.
8. **Account/event replay:** existing holdings, two competing entries, partially
   filled local pending orders versus newer broker snapshots, reductions and
   protection changes. Recompute budgets from operands and report discrepancies;
   do not infer funded trades from recommendation or RM status alone.
9. **Isolation:** deny all replay network/production DB access; attempted unknown
   provider/quote lookup fails locally. A canary in production-like state remains
   unchanged. Credential/error redaction and portable export checks pass.
10. **Operational cost:** measure p50/p95 added local capture latency, queue depth,
    session disk growth, requests and bytes. Proposed pilot budget: under 10 ms
    p95 additional expert-boundary time after bundle creation; for large frames
    use immutable buffer ownership if verified safe. A missed budget is reported
    and optimized, never concealed by dropping captures. Warmup respects its
    configured allowance and never acquires trading account locks.

Complete exact expert coverage for captured analyses and a successful historical
comparison are separate acceptance results. An input mismatch may be an expected
reconstruction limitation or a defect; the report supplies evidence before a
strategy change is proposed.

## 11. First usable delivery

Ship steps 1–3 first: exact live bundles, an offline expert replay and a coverage
report. This provides useful evidence without requiring additional downloads.
Then add targeted warmup and historical comparison, followed by full sizing and
order trace replay. All steps are part of this spec; the initial expert replay
is not completion of account/execution validation.

Use September 10's saved recommendations, rules and selections to bootstrap
fixtures and known gaps. Label that session partial. Start complete capture on
a new paper session, then production observation after the tests above. Existing
account/protection findings can become replay regressions without changing
historical strategy calculations in this work.

## 12. Status (2026-09-11, after the second delivery)

First delivery: branch `feat/live-capture-replay` (from dev 82baaa10), merged as 3e34440a; task
plan `2026-09-10-live-capture-replay-implementation-plan.md`. Second delivery: branch
`feat/live-capture-replay-2` (from dev 3e34440a); task plan
`2026-09-11-live-capture-replay-second-delivery-plan.md`. Capture is ON in production since
2026-09-11 08:28 (operator decision); the warm service ships with `warm_enabled=false`.

| Step | State | Where | Commits |
|---|---|---|---|
| 1 Contract/store | done | `packages/common/ba2_common/core/replay/` (schemas, codec, store, context, clock, service, observe) | 9e851b6b, 7d408b25 |
| 2 Live expert recording | done for the four deployed expert classes | `MarketExpertInterface._gather_and_process`, taps in FMPRating/FMPEarningsDrift/cached_get/StockScreener/OHLCV/quote (incl. IBKR), `replay_now` clock seam, host `ba2_trade_platform/core/replay_capture.py`, `main.py`, `WorkerQueue` | 7ffe2518, 402d82b5 |
| 3 Expert replay | done: `recorded_expert` and `gather_tape` capabilities, inventory/report, CLI | `testplatform/backend/app/services/replay/`, `ba2test replay inventory\|experts\|gather --bundle <dir>`, `tools/replay_bootstrap_2026_09_10.py` | 1280820e, 9c2c573e |
| 2b Remaining taps (§5) | done: DeterministicScorer statements/macro/index/analyst reads, FRED, ATR, estimator inputs; clock seams; analysis-keyed macro memo; complete indicator identity | `FMPCompanyDetailsProvider`, `fred_series`, `analyst_target_model`, `position_sizing.get_latest_atr`, DS `data.py` | 93fb9725, 8761e861 |
| 4 Warm service | done: dependency resolver + adapters, network-free planner over pinned/shared roots, budgets, low-priority warm queue, host worker with settings `warm_enabled` (false), `warm_workers` (2), `warm_daily_allowance_mib` (100), `warm_settlement_offset_minutes` (90); backend prewarm persists from worker threads, one fetcher table, estimator inputs warmed for Drift and Insider unconditionally | `ba2_common.core.replay.dependencies`, `ba2_common.core.warm`, `ba2_providers.warm` (planner, roots, seams), `ba2_experts.warm_fetchers` / `replay_dependencies`, `ba2_trade_platform/core/warm_service.py`, `testplatform/backend/app/services/prewarm_fetchers.py`, CLI `replay warm-plan` / `replay warm` | fa69a8eb, 0bf432e0, b6b62b39, b38322ce |
| 5 Historical comparison | done: `historical` capability re-runs `analyze_as_of` in a child process pinned to a cache root (CACHE_FOLDER set before import, hermetic FMP, transport closed, offline credentials), records the historical bundle under a derived session in the same store, diffs inputs (with absolute/relative deltas) and recommendation, classifies `match` / `difference` / `missing_history` / `revision_unknown` / `unsupported`; pin manifest verified per run | `testplatform/backend/app/services/replay/historical.py`, CLI `replay historical --bundle <dir> --cache-root <pinned> [--timeout]` | 6cc7e1bb, 5e11ce14, 1bc2fcd8 |
| 6 Decision/sizing trace | DROPPED (operator decision 2026-09-11): the DB already records the classic run (`RiskManagerRun`: received/funded/refused with reasons, balance and cap in force), the orders with final TP/SL and the fills; the extra stream (raw broker operands at read time, submit-lock order, validator outcomes, protection chain, fill links) serves live order-path debugging, not the live-vs-backtest question. The useful remainder — ranking and allocation operands — is delivered as an enrichment of `RiskManagerRun` on branch `feat/classic-rm-run-trace` instead. | | |
| 7 Pilot rollout | reduced: capture ON in prod; warm settings exist (get-or-create, OFF); no settings card, health badge, retention GC, export CLI or paper acceptance script were built (not needed for the operator's goal). Health counters remain readable via `get_capture_health()`. | | |

How to use:

- Enable capture on an instance: set app setting `replay_capture_enabled` to `true` and
  restart. Records land under `<instance cache>/replay/v1/` (index.sqlite + objects). Health
  counters via `ba2_trade_platform.core.replay_capture.get_capture_health()`.
- Export/replay: `ba2-test replay inventory --bundle <session-export>`,
  `ba2-test replay experts --bundle <dir> [--out <dir>]`, `ba2-test replay gather --bundle <dir>`.
  `experts`/`gather` exit 1 on any `difference`; `missing_capture` is coverage, not failure.
- Warm the backtest cache for what a session read: `ba2-test replay warm-plan --bundle <dir>
  --cache-root <writable root> [--cache-root <shared root> ...] [--as-of-now <iso>] --out plan.json`
  (no network), then `ba2-test replay warm --plan plan.json [--workers 2] [--allowance-mib 100]`.
- Compare live decisions with the backtest path on a pinned root: `ba2-test replay historical
  --bundle <dir> --cache-root <pinned root> [--out <dir>] [--timeout <s>]`. Exit 1 on any
  `difference`; `missing_history` / `revision_unknown` are coverage, not failure. The report
  fills the "Expert inputs" and "Recommendation" stages with per-field diffs and deltas.
- Live host warm worker: app settings `warm_enabled` (false), `warm_workers`,
  `warm_daily_allowance_mib`, `warm_settlement_offset_minutes`; enqueue-only at analysis batch
  end, never holds a trading lock.
- September 10 bootstrap: `tools/replay_bootstrap_2026_09_10.py <live_inputs.json> <out>`
  yields a PARTIAL session (no normalized bundles were recorded that day), so every analysis
  reports `missing_capture` by design.

Known limits recorded by the reviews:

- A recorded-bundle `match` is not a live/backtest match; only the two capabilities above run.
- Rule kept from the first delivery: no request-identity key may carry an un-replayed
  wall-clock value (the DeterministicScorer statements/macro/index gap is closed, 93fb9725).
- Historical comparison reads a pinned root in a child process; a session with several hundred
  analyses runs sequentially in that child (timeout scales with the job count).
- `absorb_if_benign` can swallow `ReplayMiss` under non-enforce error modes; a never-absorb
  registry in `failure_modes` is owed.
- `shutdown_replay_capture()` has no app shutdown path; interrupt-and-recover at next
  startup is the live lifecycle.
- Live capture cost is bounded by the writer queue (64 items); p95 latency budget not yet
  measured on a live session (acceptance test 10 pending a paper session).

References:

- [Replay readiness audit](../../reports/trading/live_backtest_replay_readiness_2026-09-10.md)
- [September 10 production trade review](../../reports/trading/prod_post_analysis_review_2026-09-10.md)
- [FMP bandwidth/cache audit](../../reports/fmp_cache/fmp_live_cache_audit_2026-09-08.md)
- [Saved September 10 dependency manifest](../../reports/trading/replay_2026-09-10/history_prewarm_manifest.json)
- Existing integration points: `ba2_common/core/backtest_context.py`,
  `ba2_common/core/native_cache.py`, `ba2_common/core/provider_cache_model.py`,
  `ba2_providers/fmp_common.py`, expert `_gather`/`_process`, live
  `JobManager`/`TradeManager`, and backend `daily_backtest_handler`/`parity_harness`.
