# Decision-time gene (`schedule:time`) — design notes

## What it is
A categorical GA gene (existing `choice` type, int index into the job's list) that picks the
exchange-local decision time of a stock backtest on the 5-minute clock. Decoded in ONE place
(`strategy_optimization_handler._schedule_times_from_gene`) into the `times` of BOTH
`run_schedule_override` (enter-market) and `manage_schedule_override` (open-positions); days stay
the run's own. Today both get the same value; splitting into an entry time and a manage time
changes that function and `ba2_common.core.schedule_genes.schedule_override_from_genes` only.

* Shared list: `ba2_common.core.knowability.DEFAULT_DECISION_TIME_CHOICES`
  (09:35, 09:40, 09:45, 10:00, 12:00, 15:30, 15:45). Flag `--decision-times default|fixed|HH:MM,...`.
* Grid DRIVERS (`run_screener_capband_matrix.py`, `run_senate_matrix.py`, the exploration driver)
  default to `default`; plain `ba2-test optimize` / `optimize-batch` default to fixed (an ad-hoc run
  stays a single reproducible time and needs a distinct, explicit `...timegene...` name).
* Validation (`validate_decision_times`): HH:MM, on the bar grid, strictly after the first bar,
  strictly before the last bar (a decision on the last bar would fill at the NEXT session's open).
* Generation 0 is stratified (`GeneticOptimizer._initial_population`, spec key `stratify`): each value
  at least `POP // k` times; warm-start / resumed populations are untouched.
* Decode and encode REFUSE an index / value outside the declared list (no clamp, no index-0 fallback).

## Engine facts
* Decision price = close of the last bar that has ENDED at T; the order fills at the open of the first
  bar strictly after the decision bar. T=15:50 -> fills at the 15:55 open (same session). T=15:55 would
  fill at the next session's 09:30 open (refused by validation).
* A session that has no bar at T (15:30 / 15:45 on a 13:00 half day) gets no decision and no manage
  pass at that time: the engine now COUNTS those sessions (`engine.sessions_without_decision_bar`) and
  logs one WARNING per run. 12:00 exists on half days.
* Live (`JobManager._parse_schedule`) fires a cron at the stored time with no session check: on a half
  day a 15:30 job would run against a closed market. Backtest = no entry that day (conservative); live
  differs. Open finding, behaviour unchanged.

## Pass order at one time (confirmed, not changed)
Backtest `daily_engine._run_expert_bar` (entry) runs before `_manage_open_positions` on a bar where both
are due; live parks OPEN_POSITIONS while an ENTER_MARKET pass is in flight
(`WorkerQueue.defer_open_positions_if_entry_in_flight`, commit e5e37c6a). Nothing keys on a time of day
(batch ids carry HHmm only as a label), so the gene does not change it.

## Option jobs: feasibility of a decision time (owner decision pending)
Option backtests run on a DAILY clock and the drivers refuse `--decision-times`.
* (a) Data: `ThetaDataOptionsProvider` implements only EOD endpoints (`option_history_eod`,
  `option_history_greeks_eod`, open interest); `AlpacaOptionsProvider` requests `TimeFrame.Day` only.
  No intraday option quote/OHLC path exists in the code. The assumed ThetaData tier is "Standard"
  (4 concurrent requests, docs/2026-09-03-thetadata-eod-backfill-assessment.md); whether it includes
  intraday history is not asserted anywhere in the repo. Roadmap: docs/plans/2026-07-25-options-data-
  and-intraday-roadmap.md Phase 3 (never built): the whole chain at 5 min is ~1.07 B rows, a selective
  universe (ATM +-10 strikes, 0-2 DTE) ~13 M rows.
* (b) Needed: an intraday option bar/quote store, a timestamp-aware `_option_fill_price` (today
  `get_bar(contract, fill_day)` by DATE), `option_bar` PK is (occ_symbol, date), and a 5-minute option
  clock in the engine; or model prices.
* (c) Cheap approximation: price the option at the decision from the underlying's 5-minute price + the
  prior day's IV through Black-Scholes. Would NOT be trustworthy for selection by spread / liquidity /
  delta targeting (IV moves intraday, no real quotes, OI dead), so a "better time" it found could be an
  artefact of the model.
* (d) Live meaning of the daily clock: the weekday/time mapping is being established by another agent
  (open question).

## Default list and the live deploy bound (owner decision 2026-10-07)
Default list: 09:35, 09:40, 09:45, 10:00, 12:00, 15:30, **15:45** (15:50 replaced: 15:40 and 15:50 are too
close to 15:30 / the close). `LIVE_PASS_P95_SECONDS = 510` and `LIVE_PASS_MARGIN_SECONDS = 300`
(`schedule_genes.py`): `tools/import_deploy_payload.py` refuses a time where time + 510 s + 300 s ends
after 16:00 on a normal day. 15:45 -> 15:58:30 accepted, 15:50 -> 16:03:30 refused. The measured maximum
pass was 506 s (orders out by about 15:53:30 for a 15:45 decision); September passes at 09:30 on a machine
that was often loaded. Re-measure if the expert universe or the hardware changes. A test fails if a value
is ever added to the default list that the deploy tool would refuse.

## Live session guard (JobManager)
* The fire instant is APScheduler's scheduled run time (`ScheduledExpertExecutor`), in the trigger's own
  timezone: a prod schedule reaches the guard as exactly 09:30:00.000 New York; the open is inclusive.
* ERROR for a skip on a FULL regular session day or any guard fault (calendar unavailable, naive instant,
  crypto instrument); WARNING for a weekend, a holiday or a half day after its early close.
* Kill switch: app setting `scheduled_session_guard_enabled` (bool, declared default ON, Settings page ->
  System Settings). State logged at startup; every skip line names the setting.
* Audit (read-only, 2026-10-07): prod, dev and options DBs hold no enabled schedule outside 09:30-16:00
  market time and none with a local time basis (prod 18 times, dev 56, options 2, all market basis).
* The schedule editor shows a warning for a time outside the session ("this pass will be skipped") and
  for one too close to the close to finish (same bound as the deploy tool); a warning only.
