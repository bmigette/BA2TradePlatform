# The backtest's screener gate = a simulation of the LIVE FMP screener (criteria `live-daily-v1`)

Owner's decision (2026-10-08): live keeps using the FMP screener and is the reference; the backtest simulates it, daily,
on a finite universe. Live code is not changed by this work (its post-filters are imported or re-derived and pinned by tests).

## 1. What the simulation is

`packages/providers/ba2_providers/screener/live_sim.py` (pure), `live_sim_build.py` (builder), `live_parity.py` (read-only
comparison with live logs/DB), `testplatform/backend/app/services/backtest/screener_gate.py` (engine side).

Live's order of operations, reproduced by ONE function (`select_from_columns`), post-fix contract (`fix/live-screener-quirks`):

1. vendor stage: cap band (inclusive), price floor/ceiling on the quote, sorted by cap descending; no volume / float parameter;
2. float stage (only if a bound is set): unknown or 0 float passes; `float_min <= f <= float_max` (store column, effective-dated);
3. stage 2, ALWAYS: no finished bars -> `dropped_no_history` (more than 10 % of candidates, or all of them -> `ScreenerDataOutage`, job-fatal);
   `rvol >= rvol_min` (if > 0), `avg_volume >= volume_min`, `avg_volume <= volume_max`; the live price / market-cap refresh;
4. Weinstein stage 2 (optional); 5. rank by market cap (ties: symbol ascending); 6. price drop walk; 7. `max_stocks` cut.

Criterion definitions (all from FINISHED daily bars before the morning, plus "now"):

| criterion | definition (identical to live) |
|---|---|
| RVOL | last finished session's volume / `round(mean(last <= 20 bars in [D-35d, D)), 2)` (denominator INCLUDES the numerator), `round(.., 2)` |
| price drop | peak = highest `max(high, low)` over the bars dated in `[D-(n+5)d, D)` PLUS the forming bar, the last `n` of them (so the last n-1 finished sessions); `current` = now; `round((peak-cur)/peak*100, 2) >= pct` |
| Weinstein | `classify_weinstein_stage` (imported) on the closes in `[D-255d, D)` PLUS the forming bar's close = now |
| market cap | previous close x the vendor's share count (section 4) |
| now | the opening print of the first bar when the decision sits on the first bar, else the close of the latest ENDED intraday bar at T; daily clock: the decision day's close, screening the NEXT session |

The forming bar (FMP returns the session's bar at ~09:31 with close = the current price) is part of live's drop window and Weinstein
input. MEASURED, correcting the 2026-10-07 report ("identical with or without"): on prod instance 7 the live-only picks HOOD
(2026-09-14) and WDC (2026-09-21) are stage 2 only with the opening print appended as the last close; on 2026-10-05 the simulation's
Weinstein pass count equals live's logged one (224 of 606/613 candidates). One-line switch: `live_sim.FORMING_BAR_PRESENT`.

### Owner-approved exception (screener only)
`AsOfPriceSource.screener_now_price` returns the session's opening print when no bar of the session has ended (decision inside the
first bar). Its single call site (`screener_gate.PanelGate.symbols`) is allowlisted in
`testplatform/backend/tests/test_no_bar_price_in_decision_code.py` with the reason "owner-approved: screener simulation uses the opening
print as live uses a quote ~30-90 s after the open". Every other decision keeps the ended-bars-only rule.

### Remaining differences from live (and their measured size)
* the live quote's exact second: drop margins within ~1.8 % of price flip (all residuals of section 5 are in this class or at the `max_stocks` cut);
* the vendor's share count per past date (section 4);
* tie order of equal market caps: symbol ascending (the vendor's order is unknown);
* the rank key: live ranks by the refreshed `/quote` cap; the simulation by `previous close x shares` (`RANK_CAP_BASIS`, `BAND_CAP_BASIS`
  are one-line switches to "now x shares" once the 09:30 open-snapshot experiment has a verdict; assumed: previous close);
* a symbol with no bar in its 35-day window is not a candidate (a vendor lists only actively traded names); the simulation therefore never
  produces `dropped_no_history` by itself, the panel refuses an incomplete cache instead (`fresh_fraction < 90 %`);
* missing bars inside a window (cache gaps) are skipped; the n-session trim is applied on the common NYSE session grid;
* delisted/renamed names: finite universe (the store's 4,734 symbols, of which 132 are no longer in the vendor list).

## 2. Design: a daily panel, not a daily store

Per job nothing is recomputed. `ba2-test prewarm --screener-panel` builds ONE shared panel `<cache>/screener/daily_panel`
(sessions x symbols, day-major float64 arrays, memory-mapped, shared by all workers): O/H/L/C/V, shares, float and the genome-independent
criteria columns (rvol, avg20, last volume, previous close, Weinstein stage and the forming-bar Weinstein terms). Only the price-drop peak
depends on the genome (`n`): `peak_by_day(n)` is built on first use and memoised (6 n's per process).

Why not the store: the weekly store has 1.35 M rows x 45 columns (pandas); a daily store would be 5x that and still would not carry the live
definitions of the criteria; the panel is 4,734 x 1,919 numpy rows read as contiguous slices.

* size: 954 MB (4,734 symbols x 1,919 sessions 2019-03..2026-10, 14 arrays); build ~6 min unloaded (bars 45 s, shares ~100 s, derived ~200 s; 13 min while the machine was busy); no-op when up to date (source fingerprint);
* per-trial cost (measured on that panel, unloaded): gate 2.2 ms per decision day (3.7 s if every one of the 1,695 days is evaluated, ~0.7 s for a weekly entry schedule) vs the weekly store gate 34 ms per scan (11.6 s per run);
  per-trial prune 1.5 - 4.5 s (1,695 days) vs 7.7 s; first `peak_by_day(n)` 6.7 s per n per process (cold worker: <= 3 min for all 29 n);
* static superset (goal2020 window, intraday): small 3,521, mid 2,317, large 1,381 symbols (the weekly-store superset: 3,491 / 2,299 / 1,341).

### One code path (reviewer items 1, 3)
The static universe, the per-trial prune and the per-decision gate all call `select_from_columns` (`panel.select`, `select_bounds`):
the superset/prune pass "now" as the session's [low, high] (price-monotone criteria, Weinstein monotone in the forming close) and DO NOT cut
(a cut is not monotone in "now"), the gate passes the real "now" and cuts. Ties: `np.lexsort((symbol, -cap))` everywhere. The run-time guard
counts once per decision (identity of the memoised list) and says which set was violated (pruned preload vs static universe).

### Out-of-range genomes (item 2)
`decode_params`/`snap_to_lattice` clamp only with the "min" lattice anchor; the legacy "zero" anchor passes an out-of-range warm-start seed
through unclamped (and stored pins bypass the GA altogether). Now: the launcher stores `screener_opt.declared_ranges`; a genome outside them
raises `ScreenerGenomeOutOfRange` (job-fatal) when its trial config is built, and `assert_population_screener_genes_in_range` refuses a whole
warm-start population at job setup.

## 3. Job identity
`backtest.screener_opt.criteria_version = "live-daily-v1"`; checkpoint fingerprint gains `screener_criteria` (only when stamped: old jobs keep
theirs); job name `<base>-timegene-sup1-lds1-d<digest>` (`-lds1` right after `-sup1`, digest stays last; FactorRanker keeps `<base>-sup1`: it
builds its universe from the weekly store itself and is not covered). A run without the stamp (a stored pre-simulation row) keeps the legacy
weekly gate (`_screened_symbols_for_bar` branches on `criteria_version`); the two never share a name, a checkpoint or a "completed" check.

## 4. Market cap on the vendor's basis (point in time)
Measured 2026-10-08: the vendor's `marketCap / price` equals `outstandingShares` of the bulk table `api/v4/shares_float/all` (median |diff|
6e-11) for 95.8 % of names; for 4.2 % (class shares, ADR ratios) the cap uses another count, so `k = (cap/price)/outstanding` is kept per
symbol. `api/v4/historical/shares_float?symbol=` returns the same `outstandingShares` as a DATED series (daily-ish rows, from 2021-05-18; the
endpoint keeps ~1,800 rows) = the vendor's own history, look-ahead free (a row is used from its own date).

Panel share count: 1. vendor series as of D x `k`; 2. before the series starts (2020-01..2021-05) and for symbols without one: FMP's implied share
series (cached historical market cap / close) delayed by 45 days (filing lag) and scaled to the vendor's count; every symbol on source 2 is counted
in the manifest. NOTE: the per-symbol vendor histories (4,734 calls) were NOT fetched here (call budget); the validation below uses source 2
for everything, and "vendor history emulated" = the vendor's current count held constant over the six validation weeks.

Error of source 2 (vs the vendor's CURRENT count x that day's previous close; the share of names off by more than 5 %, band 5-10B flips per day):
2026-09-08 14 % / 24 of 466; 2026-03-02 15 % / 47 of 453; 2025-09 32 % / 67 of 436; 2024-09 50 % / 107 of 453; 2022-09 67 % / 155 of 438;
2020-09 76 % / 174 of 382 (older dates include REAL share changes, so these are upper bounds of the artifact). Artifact example: SYRE, the FMP
series is 92 % below the vendor's history for the whole of 2021-2023 (NXST: within 5 %). The old store's FMP cap column vs the vendor's count
at the same close (2026-10-03): 19.2 % off by more than 5 % (median 0.5 %), matching the 2026-10-07 report. Look-ahead of lag 0 vs lag 45:
16 of 464 band flips per day recently (3.4 %), at most 1.5 % for older dates.
A more faithful source exists and is wired: the vendor's own history (`prefetch_shares`, 1 call per symbol, resumable, refreshed weekly); the
bulk table adds a daily vendor snapshot going forward.

## 5. Validation against REAL live output (acceptance), legacy (pre-fix) behaviour
Prod DB instances' `LIVE SELECTION` log lines (the screener's own list) vs the simulation on the panel built in scratch, "now" = the day's
opening print. Per-day tables: `reports/screener_parity_2026-10-08/`. Mean daily Jaccard / live picks reproduced:

| instance | days | share counts = vendor history emulated | share counts = FMP-implied (what a fresh machine without share histories gets) |
|---|---|---|---|
| 11 (mid, rvol 1.5, drop 13/22d, max 20) | 13 | **0.950**, 146 / 147 | 0.874, 141 / 147 |
| 8 (mid, rvol 0.6, drop 16/18d, max 40) | 4 | **0.854**, 137 / 152 | 0.831, 135 / 152 |
| 10 (cap 2-10B, rvol 1.3, drop 8/10d, max 50) | 9 | 0.845, 362 / 406 | 0.823, 357 / 406 |
| 7 (>= 20B, rvol 0.1, drop 5/8d, Weinstein, max 30) | 4 | 0.670, 96 / 120 | 0.670, 96 / 120 |

Instances 11 and 8 reproduce the 2026-10-07 report (0.950 / 0.854) through the production code path. Residuals: every symbol has a class in the
tables. Instance 11: 6 simulation-only names with a drop margin 0.2 - 1.8 points and 1 live-only at 0.2. Instance 8: 13 live-only names with
drop margin 0.03 - 1.6 points, 2 `max_stocks` displacements, the rest simulation-only edges. Instance 10: 37 drop-edge names (0.02 - 1.8), 4 symbols
outside the universe (VCRE, CSQR, JMKE: not in the store), the rest edges/cut. Instance 7 is the weak one: a 5 % threshold on 8 days over 600
large caps puts ~30 names per day within one quote tick of the threshold and the 30-name cut turns each flip into a displacement (not a defect:
the same sensitivity is in live's day-to-day variation). Unknown residuals: none (all classified); 4 `outside_universe` symbols (finite universe).

The recorded days are also a permanent test: `packages/providers/tests/test_live_sim_recorded_days.py` (3 days of instance 11, 14/11/9 picks,
exactly reproduced from a 400 KB fixture of real bars). `tools/screener_parity_report.py` re-checks any range read-only.

## 6. Prewarm and builder wiring
```
ba2-test prewarm --screener-panel --screener-store <store dir> --start 2020-01-01 --end 2025-12-31   # resumable, prints every step
ba2-test build-screener-metrics ... --daily-panel                                                    # same, after the store build
```
Steps: vendor bulk share table (1 call) -> per-symbol vendor share history -> FMP market-cap/float caches where missing -> check of the daily
bars (`fetch-cache` is the tool that fetches them; missing ones are listed) -> panel. API: `data_build_handler.handle_prewarm` payload
`screener_panel: {store, start, end}`. `optimize --screener` REFUSES with the list when the panel is missing, does not cover
`[start - 260 d, end]` (bars through the last session before `end`), was built under another `criteria_version`/format, or the OHLCV cache is
incomplete; the engine/handler re-check at job start (`require_panel`), a worker never fetches. `cache push` carries
`screener/daily_panel` (~0.95 GB) and `screener_fundamentals/shares`; nothing else is new.

## 7. Request to the stage-log developer (`feat/screener-stage-log` @ 81b2bec7)
It logs one `LIVE STAGES` line (counts per stage + FMP failure counters) and the existing `LIVE SELECTION` line. For a replayable parity
test the following is MISSING (additive, no change of selection): (1) the vendor request parameters and the wall-clock time of the call
(the `stage 1 ... with filters` INFO line has the filters, not the time); (2) the raw vendor response rows per symbol: `symbol, price,
marketCap, volume` (one JSONL per distinct request, de-duplicated by request hash across instances, ~200 KB); (3) per candidate after stage
2: the `/quote` price and market cap used, the quote's timestamp, `volume`, `avg_volume`, `relative_volume`, the history window actually
returned (first/last bar date, whether a bar dated today was present and its close); (4) the symbol list after EACH stage (not only the
counts), and the final ORDERED list with the rank key; (5) the drop value and peak per candidate of the price-drop walk (symbol, peak,
current, drop); (6) the resolved settings dict. `tools/screener_parity_report.py` already reads `LIVE SELECTION` and `LIVE STAGES` and prints
the stage counts next to the simulation's `diag` (`select_from_columns(diag=...)` carries the same keys as live's stats).
