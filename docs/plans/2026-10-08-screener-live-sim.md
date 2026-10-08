# The backtest's screener gate = a simulation of the LIVE FMP screener (criteria `live-daily-v2`)

Owner's decision (2026-10-08): live keeps using the FMP screener and is the reference; the backtest simulates it, daily, on a finite
universe. Live code is not changed by this work (its post-filters are imported or re-derived and pinned by tests).

## 1. What the simulation is

`packages/providers/ba2_providers/screener/live_sim.py` (pure), `live_sim_build.py` (builder), `live_parity.py` (read-only comparison with
live logs/DB), `testplatform/backend/app/services/backtest/screener_gate.py` (engine side).

Live's order of operations, reproduced by ONE function (`select_from_columns`), post-fix contract (`fix/live-screener-quirks`):

1. vendor stage: cap band (inclusive), price floor/ceiling on the quote, sorted by cap descending; no volume / float parameter;
2. float stage (only if a bound is set): unknown or 0 float passes; `float_min <= f <= float_max` (store column, effective-dated);
3. stage 2, ALWAYS: no finished bars -> `dropped_no_history` (more than 10 % of candidates, or all of them -> `ScreenerDataOutage`, job-fatal);
   `rvol >= rvol_min` (if > 0), `avg_volume >= volume_min`, `avg_volume <= volume_max`; the live price / market-cap refresh;
4. Weinstein stage 2 (optional); 5. rank by market cap (ties: symbol ascending); 6. price drop walk; 7. `max_stocks` cut.

| criterion | definition (identical to live) |
|---|---|
| RVOL | last finished session's volume / `round(mean(last <= 20 bars in [D-35d, D)), 2)` (denominator INCLUDES the numerator), `round(.., 2)` |
| price drop | peak = highest `max(high, low)` over the bars dated in `[D-(n+5)d, D)` PLUS the forming bar, the last `n` of them; `current` = now; `round((peak-cur)/peak*100, 2) >= pct` |
| Weinstein | `classify_weinstein_stage` (imported) on the closes in `[D-255d, D)` PLUS the forming bar's close = now |
| market cap, BAND (vendor stage 1) | the vendor's last-trade price x its share count, **as-traded basis**: the PREVIOUS CLOSE inside the session's first bar (09:30, the names that have not printed), the PRICE AT T for every later decision (`band_at_now`); measured, section 4b |
| market cap, RANK KEY | the refreshed `/quote` cap = price at T x the vendor's shares (`RANK_CAP_BASIS` is gone: one rule); a name without a price ranks on its previous-close cap |
| now | see below |

### "Now" and the forming daily bar, for every decision time T
FMP's daily history carries the session's FORMING bar when live screens. The simulation models it as: close = the price at T, high = the
highest high of the session's intraday bars ENDED <= T (+ the open), and uses it as the last close of the Weinstein input, as one of the
last `n` bars of the drop window (its high joins the peak) and as the drop test's current price.
**The close is a stand-in** (measured 2026-10-08, section 4b): FMP's forming-bar close lags the quote (median 0.26 %, 95th percentile 1.1 %);
the selection function takes the true bar close (`forming_close`) in the replay of recorded inputs, the backtest has no second price and passes
`None`.

| decision time T | "now" | forming-bar high | code |
|---|---|---|---|
| inside the session's first bar (09:30) | the OPENING PRINT (open of the first bar), owner-approved exception, screener only | the same print | `AsOfPriceSource.screener_now_price` / `screener_session_high` |
| T >= 09:35 | the close of the latest 5-minute bar ENDED at or before T (== `decision_price`) | max of the highs of the bars ended <= T | same |
| no bar of the session ended and not in the first bar (thin name) | the `decision_price` rule (last bar of the last finished session) | the same price | same |
| nothing knowable at all | not a candidate (no fallback price), counted in `results["screener_gate"]`; > 10 % of the LOADED candidates -> `ScreenerDataOutage` | | `PanelGate` |
| daily clock (`1d`) | the decision day's close; the gate screens the NEXT session's morning | the close | `screen_days(intraday=False)` |

Tests: `test_forming_bar_through_T_never_sees_a_bar_that_has_not_ended` (09:30, 09:35, 10:00, 12:00, 15:30, 15:45; a 14:00 spike is invisible
at 10:00-14:00 and visible at 14:05), `test_the_day_bounds_are_a_valid_superset_for_every_decision_time` (every T in 09:30..15:55 lies in the
session's [low, high]), `test_gate_t_1000_does_not_leak_a_later_session_high` (end to end through `PanelGate`).

### The superset is a superset for every T
Prune and static universe call the same selection with "now" = `[min(session low, previous close), max(session high, previous close)]` (the
previous close covers a name that has not traded yet) and the forming high bounded by the session high, no `max_stocks` cut. The daily
clock uses the degenerate interval `[previous close, previous close]` (what the gate reads). Property tests: gate output inside the prune
inside the static superset, intraday AND daily clock, with prices supplied ONLY for the pruned symbols, as the engine does.

### Remaining differences from live
* the live quote's exact second (30-90 s after the open), and which print it is: the daily bar's open (the official auction print) is
  identical to the first five-minute bar's open for 43 % of names, median |diff| 0.09 %, p95 1.7 % (430 sampled pairs, June 2026);
* the vendor's share count in the 44 symbols without a vendor history (FMP-implied fallback) and before 2021-05;
* tie order of equal market caps: symbol ascending;
* the forming bar's close (stand-in = the price at T; FMP's differs by a median 0.26 %, p95 1.1 %: MRNA / QCOM at 10:05);
* the band at 09:30 is the previous close for every name, the vendor's is a per-symbol mix (names that have printed show the last trade);
* cache gaps inside a window are skipped; the n-session trim is applied on the common NYSE session grid; delisted names are outside the finite universe.

## 2. Design: a daily panel, not a daily store

`ba2-test prewarm --screener-panel` builds ONE shared panel `<cache>/screener/daily_panel/<fingerprint>/` (sessions x symbols, day-major,
memory-mapped): O/H/L/C/V, vendor shares, free float, the as-traded split factor `fac`, and the genome-independent criteria columns (rvol,
avg20, last volume, previous close, Weinstein stage and the forming-bar Weinstein terms). The price-drop peak depends on the genome (`n`) and
is computed per row on demand (`peak_row(n, p)`, ~0.1 ms, nothing cached, thread-safe).

* **Size**: 990 MB (4,734 symbols x 1,919 sessions 2019-03..2026-10, 15 arrays). **Build**: 11-14 min (bars 1 min, shares ~2 min, derived ~8 min).
* **Per-trial cost** (measured, 2020-2025 window, 1,500 decision days): prune 5-10 s (weekly-store prune 7.7 s); gate with an array "now"
  2.7-4.6 ms/decision; **gate with the real price source** (461 symbols of 5-minute bars, half a year): 27 ms/decision at 10:00 and ~100 ms at
  09:30 (the opening-print path), i.e. 3 - 12 s for a Monday-only schedule (~300 decisions), 40 - 150 s if every one of 1,500 days is an
  entry day; the weekly store gate was 34 ms per scan (11.6 s per run).
* **Static superset** (goal2020 window, intraday): small 3,521, mid 2,317, large 1,381 symbols (weekly-store superset: 3,491 / 2,299 / 1,341).
  FactorRanker (bypass) keeps the weekly store's list: 3,491 / 2,299 / 1,341 symbols = ~11.3 / 12.0 / 8.0 GB of float64 5-minute arrays for the list
  (3.2 - 5.8 MB per symbol); mapped memory, per-trial private memory not measured.

### One code path
The static universe, the per-trial prune and the per-decision gate all call `select_from_columns`. Ties: `np.lexsort((symbol, -cap))`. The
guard counts once per decision and says which set was violated (pruned preload vs static universe).

### Identity and staleness
* Each panel is its own directory named by its fingerprint (inputs: criteria version, panel format, build revision, window, lag, vendor table,
  stat tokens of every OHLCV / market-cap / shares / float / splits file). `cache push` compares (path, size) and the `.npy` files have fixed
  sizes, so a panel rebuilt in place would never reach a worker; a new directory per build does. No pointer file (it would have the same flaw).
* Every file is written as `<name>.tmp` and renamed, the manifest last; no `.building` directory.
* The job stamps `screener_opt.panel` (relative to the cache root, resolved on each machine), `panel_fingerprint`, `behaviour` (`post-fix`) and
  `criteria_version`; the checkpoint fingerprint is `criteria/behaviour/panel fingerprint`; `get_panel` never reloads and refuses a path whose
  fingerprint differs from the job's; results carry `screener_gate`. Shapes are validated against `symbols.json` / `sessions.json` on load.
* Job name: `<base>-timegene-sup1-lds2-d<digest>` (FactorRanker keeps `<base>-sup1`).
* Older panel directories are kept; the build prints their sizes.

### Settings: nothing is left to StockScreener's defaults
`ba2_common.core.deploy_parity.SCREENER_SELECTION_KEYS` is THE list (14 keys; pinned equal to `StockScreener._DEFAULTS` and to the interface
definitions). The simulation refuses a missing key (launch and job start), the launcher writes every key (`price_min = price_max = volume_min =
volume_max = float_min = float_max = 0`, `sort_metric = market_cap`, as the enabled prod instances 7-11 carry; it prints the resolved base), the
deploy (`import_deploy_payload`) writes every key (the "off" value for a bound the payload never stated).

### Out-of-range genomes
`decode_params` / `snap_to_lattice` clamp only with the "min" lattice anchor; the legacy "zero" anchor passes an out-of-range warm-start seed
through unclamped, and stored pins bypass the GA. The launcher stores `declared_ranges`; a genome outside them raises `ScreenerGenomeOutOfRange`
(job-fatal) when its trial config is built, and a whole warm-start population is refused at job setup.

## 3. Market cap on the vendor's basis (point in time) and the SPLIT BASIS
Measured 2026-10-08: the vendor's `marketCap / price` equals `outstandingShares` of the bulk table `api/v4/shares_float/all` for 95.8 % of
names (the cap of the other 4.2 % uses another count, kept as a per-symbol factor). `api/v4/historical/shares_float?symbol=` returns the same
field as a DATED series (from 2021-05-18): the vendor's own history, used from each row's own date (no look-ahead).

**Split basis (verified on known splits).** The vendor's series is the RAW count as of each day; the OHLCV cache is split-ADJUSTED as of its fetch:

| symbol, date | adj close | vendor raw shares | adj x shares | x `fac` (as-traded) |
|---|---|---|---|---|
| NVDA 2024-06-07 (10:1 on 06-10) | 121.00 | 2.46e9 | 2.98e11 | **2.98e12** (fac 10) |
| NVDA 2024-06-10 | 120.89 | 2.439e10 | 2.949e12 | 2.949e12 |
| GOOGL 2022-07-14 (20:1 on 07-18) | 111.35 | 3.008e8 | 3.35e10 | **6.70e11** (fac 20) |
| GOOGL 2022-07-19 | 109.03 | 6.015e9 | 6.558e11 | 6.558e11 |
| TSLA 2022-08-24 (3:1 on 08-25) | 296.45 | 1.044e9 | 3.10e11 | **9.29e11** (fac 3) |
| TSLA 2022-08-26 | 296.07 | 2.089e9 | 6.19e11 | 6.19e11 |

Without the factor a name that splits LATER is mis-capped by the split ratio before the split (NVDA 10x low). The panel therefore carries
`fac(D) = split_basis.as_traded_factor` (product of the ratios of splits dated after D, from the split calendar: the market-condition warm-up's
`fmp_history/mc_stock_split__*` or our own `screener_fundamentals/splits/*`, fetched by the prewarm for the 2,254 symbols that had none);
`cap = previous close x raw shares x fac`, and the price floor / ceiling compare `now x fac`. A symbol without a calendar has `fac = NaN` (not a
candidate) and makes the panel refuse. 741 of the 4,734 universe symbols have a split inside 2019-03..2026-10. The FMP-implied fallback is
converted to the same raw basis (`cap / adjusted close / F`, lagged in the adjusted basis, divided by F(D) afterwards: a split is known on its
ex-date and is not lagged). Tests: `test_live_sim_split_basis.py` (factor matrix == `as_traded_factor`; NVDA-style raw series), `test_live_sim_build.py`
(a 10:1 split name keeps one cap across the split).

Vendor histories were fetched for all 4,734 symbols (4,579 calls + 111 cached): 4,690 have a series, **44 have none** (FMP-implied fallback),
1 listed symbol has no source at all (CICC). Band flips of the 5-10B band between the FMP-implied fallback and the real histories:
2026-09-08 19 of 465 (4 %), 2024-09-03 58 of 432 (13 %), **2023-09-05 88 of 409 (22 %)**, 2022-09-06 95 of 396 (24 %), **2021-09-07 124 of 456 (27 %)**;
names whose cap differs by more than 5 %: 15 % (2026) -> 28 % (2023) -> 34 % (2021). Using the real histories removes these.

## 4. Validation against REAL live output (acceptance), real build
Prod instances' `LIVE SELECTION` log lines vs the simulation on the REAL panel (vendor share histories, as-traded factors), "now" = the daily
bar's open. **The 5-minute cache ends 2026-06-30 for most symbols, so the September-October recording cannot be scored with 5-minute "now"**
(the first run of that configuration found no "now" for most candidates and collapsed to Jaccard 0.0 - 0.5 on three of the four instances); the job configuration reads the first 5-minute bar's open, which differs
from the daily open by the amounts above. Per-day tables: `reports/screener_parity_2026-10-08/`.

| instance | days | share counts: vendor history emulated (current count held) | **real build** (vendor histories + splits) | FMP-implied fallback only |
|---|---|---|---|---|
| 11 (mid, rvol 1.5, drop 13/22d, max 20) | 13 | 0.950, 146/147 | **0.950, 146/147** | 0.874, 141/147 |
| 8 (mid, rvol 0.6, drop 16/18d, max 40) | 4 | 0.854, 137/152 | **0.854, 137/152** | 0.831, 135/152 |
| 10 (cap 2-10B, rvol 1.3, drop 8/10d, max 50) | 9 | 0.845, 362/406 | **0.842, 361/406** | 0.823, 357/406 |
| 7 (>= 20B, rvol 0.1, drop 5/8d, Weinstein, max 30) | 4 | 0.670, 96/120 | **0.670, 96/120** | 0.670, 96/120 |

* The job configuration (`POST_FIX`) and the recorded code (`LEGACY_CURRENT`) select IDENTICALLY on all 30 scored days of the four enabled instances
  (floors at zero, `rvol_min > 0`; same numbers in both rows of the score run), and this is asserted on the 5 fixture days by
  `test_post_fix_and_pre_fix_live_select_identically_for_the_enabled_prod_instances`.
* **Sensitivity to "now"** (post-fix, daily open x factor), mean Jaccard i11 / i8 / i10 / i7: x0.99 0.879 / 0.737 / 0.760 / 0.520; x0.995 0.931 /
  0.840 / 0.854 / 0.637; **x1.0 0.950 / 0.854 / 0.842 / 0.670**; x1.005 0.916 / 0.802 / 0.772 / 0.551; x1.01 0.867 / 0.750 / 0.728 / 0.438.
  A 0.5 % shift costs 0.03 - 0.12: the scores ARE sensitive to the quote, so quote-time noise of ~0.5 % explains the residuals with a drop margin
  below ~0.8 points; it does not explain a margin of 3 - 15 points. (x0.995 is slightly better for i8 / i10: live's quote sits ~0.3 % below the
  daily open on average.) The 09:35 five-minute close configuration cannot be scored (no 5-minute data in September).
* **Classification** (strict: edge only <= 0.3 points of the drop threshold, <= 2 % of the rvol threshold, <= 1 % of the cap band). Unexplained
  residuals (> 0.3 point): i11 4 (ORA +1.18, CGON +0.73, ACT +1.08, M +0.38), i8 13, i10 49, i7 37. Of these, **margins above 2 points** (not
  quote noise): i8: NAVN +14.7, VSXY +4.0 (2026-09-10); i10: LAZ +7.8, SITE +5.3, EROK +5.1, MH +4.1, NXE +3.8, SIM -3.8 (live-only), SGHC +3.7, BLDR +3.0,
  AYA +3.2, TENB +2.7, EMAT (band, 3.7 % below the floor); i7: DASH +8.6, FCX +9.4, WPM +7.3, ABNB +4.0, ASX +4.6, OXY +3.9, NUE +2.3 (sim-only),
  BHP -3.9, PANW -3.8, NBIS -2.5 (live-only), and on 2026-09-14 the six live-only Weinstein names AMAT, ASML, LRCX, SCCO, TXN, WDC (the simulation
  has them below their rising SMA150 by 1 - 8 %). The max_stocks cut binds on 4/4 (i7), 4/9 (i10), 2/4 (i8) days and the sim-only names are
  displaced / displacing at it; re-ranking by `now x shares` instead of the previous-close cap does not remove them. **Unexplained: all of the
  above**; the stage-log request (section 6) is what would settle them. Weinstein counts agree where live logged them (2026-09-28: 230 vs 230;
  2026-10-05: 224 vs 225), so there is no aggregate bias, but 09-14 and 09-21 have no logged counts.
* **Fitted choices, no holdout** (SUPERSEDED by section 4b: both were MEASURED on the live captures of 2026-10-08 and the cap basis changed;
  the tables in this section and in `reports/screener_parity_2026-10-08/` were scored with the OLD rules, previous close x shares as the rank
  key and the band, and are kept as history only): `FORMING_BAR_PRESENT = True` (fitted on instance 7 days 09-14 / 09-21: HOOD, WDC) and
  `prev_close` as the cap basis (fitted on XENE 2026-09-18) were chosen on these scored days. The holdout is the coming live days: `tools/screener_parity_report.py --from
  <day> --to <day>` scores any range read-only (the screen of 2026-10-08 onward is out of sample).
* The recorded-day fixture (`fixtures/screener_recorded_days.json`, 700 KB) was re-extracted from the real build: 5 days of instance 11 (3 exact,
  2 with the pinned residuals ORA, CGON), not selection-biased.

## 4b. Live captures of 2026-10-08 (the verdicts that fixed the rules)
`tools/screener_capture/`: the real post-fix `StockScreener.screen()` for prod instances 7, 8, 10, 11, with a recording wrapper at the vendor
HTTP seam (<= 4 calls/s), at 09:40 ET (label `0940`, instance 7; instance 8 hit the label budget), 09:50 (`0950`: 8, 10, 11, a re-capture) and
10:05 (`1005`: 7, 8, 10, 11). 9 cells. Offline: (a) **replay** through live `screen()` from the recorded bodies; (b) the simulation's selection
function on the **same recorded inputs**; (c) the simulation on its **own inputs** (cache + panel).

| check | result |
|---|---|
| (a) replay through live `screen()` | ordered picks IDENTICAL in 9 of 9 cells; stage stats equal; 0 unmatched requests |
| (b) selection function on recorded inputs | ordered picks IDENTICAL in 9 of 9 cells, 0 term differences |
| (c) simulation on its OWN inputs | ordered equality in 3 of 9 cells; Jaccard 0.917 - 1.0 (1005: 0.935 / 1.0 / 0.961 / 0.917 for instances 7 / 8 / 10 / 11; 0940: 1.0; 0950: 1.0 / 0.961 / 0.923) |

**What (b) proves and does NOT prove.** The candidate list (the vendor stage's output), the "now" price and the share count in (b) come from LIVE'S OWN
staged outputs of that run, and the market-cap band is switched off. So (b) validates stages 2-7 and the ordering (RVOL, floors, Weinstein, rank,
price-drop walk, cut) and the rules found by it (rank key, forming-bar close). It does NOT validate the band, `band_at_now`, the panel's share
counts, the as-traded factors or the cache: those are only exercised by (c).

**(c): the differing names** (honest own-input result): CTVA (cache not adjusted for the 2026-10-01 spin-off: bars differ by up to 85 %),
NEOG and LQDA (band edge / share count: the vendor's cap vs the panel's shares x price differ by 2-3 %), MRNA vs QCOM at 10:05 (the forming-bar
close stand-in, above).

**Verdicts** (rule changes in `live_sim.py`; tests in `test_live_sim_parity.py` and `test_screener_gate_review_fixes.py`):
1. *Forming bar present*: 1,747 of 1,750 symbols have a bar dated today at 10:05; its high equals the quote's `dayHigh` (median ratio 1.0); its close differs
   from the quote price by a median 0.26 % (p95 1.1 %). `FORMING_BAR_PRESENT = True` confirmed; the close is a lagged snapshot (MRNA / QCOM).
2. *Rank key* = quote price x vendor shares (the quote's `marketCap` / that product: median 1.0000; against previous close x shares 0.9996). Rank on
   the cap at the decision price.
3. *Band cap* = the vendor's last-trade price x shares: the vendor cap is closer to quote x shares than to previous close x shares for 89 % of 2,633 names
   at 10:05 (82 % of 617 / 2,018 at 09:41-09:54); the vendor's price equals the quote for 17 %, the previous close for 2 %. The open snapshot (50 names of
   the 5-10 bn band, 44 usable; 09:30:07 / 09:37:49 / 10:00:01): at 09:30:07 the vendor cap equals previous close x shares (median deviation 0.09 %, against
   0.45 % for the quote), and its price is the previous close for 21 of 44 names (the ones that have not printed); at 09:37 and 10:00 it equals the live
   price (0.06 % / 0.11 %) with 0 of 44 at the previous close. So: band on the previous close inside the first bar, on the price at T afterwards
   (`band_at_now`). How many band-edge names the 09:30 approximation (previous close for every name) can flip was NOT quantified.
4. *Exact band bound*: with `band_at_now` the candidates are pre-selected on the session's low / high x shares (cap at the low <= max, cap at the high
   >= min; widened by the previous close), the final test is the exact price at T. The first version used a hard-coded [0.5 x min, 2 x max] window
   on the previous-close cap that silently dropped names moving more than that; it is gone.

Identity: these changed the selection rules, so `CRITERIA_VERSION` is `live-daily-v2` (job-name token `-lds2`): no panel, checkpoint or re-run
crosses a rule set. Old panels are refused (`panel criteria_version ... !=`) and must be rebuilt (same data, ~13 min).

## 5. Prewarm and builder wiring
```
ba2-test prewarm --screener-panel --screener-store <store dir> --start 2020-01-01 --end 2025-12-31   # resumable, prints every step
ba2-test build-screener-metrics ... --daily-panel                                                    # same, after the store build
```
Steps: vendor bulk share table (1 call, refreshed weekly) -> vendor share history per symbol (paced) -> split calendar per symbol (paced) ->
FMP market-cap / float caches where missing -> check of the daily bars -> panel. **Any fetch failure ends the run with an error** (resumable).
API: `data_build_handler.handle_prewarm` payload `screener_panel: {store, start, end}`. `optimize --screener` REFUSES with the list when the panel
is missing, does not cover `[start - 260 d, end]` (bars through the last session before `end`), was built under another `criteria_version` /
format, lists a symbol that is in the vendor's CURRENT listing but has stale bars (the build lists the symbols treated as delisted: 85 now) or has
no share source / no split calendar (acknowledge with `acknowledged_stale` at build time). Today's real build is refused for 8 stale listed
symbols (DBRG, GORO, IPEXR, IPEXU, MIDD, MOD, RBKB, SNFCA: `fetch-cache` them) and CICC (no share source).
The API route `_merge_screener_opt` refuses any screener optimization that is not already stamped with the rule (+ criteria and panel for
classic experts); the web UI's optimization form receives that HTTP 400 text (the request is rejected before a job exists; rendering of the
detail by the front end was not verified in a browser).

`interval_is_intraday` now treats `"1hour"` / `"4hour"` as intraday (it classified them as daily). No driver, launcher choice or doc uses those
spellings (the cache holds `1h`, which was always intraday); the change is kept and is a no-op for every existing run.

## 5b. Launch guards added in review round 2
* **Exclusion list** (`panel_exclusions.json`): an entry whose `reviewed_by` starts with "pending" REFUSES the launch (`panel_problems`), naming the
  entries; accepted entries are printed with their reviewer. The owner confirms an entry or it is removed after the data is repaired; nothing is
  approved by the code.
* **Intraday coverage** (`ba2_providers/screener/intraday_coverage.py`, run by `ba2-test optimize` for every intraday job and by `--print-universe`):
  of the symbol-days with a daily bar, more than 5 % without an intraday bar REFUSES the launch; the report (worst symbols, symbols with no file)
  is printed either way. The intraday-vs-daily PRICE BASIS check is the shared function of `fix/ohlcv-cross-interval-basis`.
* **Job-end alarm**: a candidate that had no price because it was not loaded, although the bounds-based selection (the prune's function) returns it
  for that day, is job-fatal (`PanelGate.assert_complete`, `ScreenerGateRefusal`). The no-price counts are logged at job end and every trial row
  of the persisted `all_results` carries `sg: {no_price, outside, symbols}`.
* **Web UI**: `Backtesting.tsx` sends `screener_opt` without the launcher's stamp (`screener_universe_rule`, `enabled_instruments`, panel); the
  API REFUSES it (400, message names `ba2-test optimize --screener`), classic and FactorRanker alike (the UI never sends
  `apply_to_expert_settings`). Test: `test_strategies_screener_opt_api.py`. A UI screener launch needs an owner decision.
* Cost: `screener_session_high` is a bisect + array read after one vectorised running-high pass per symbol (+8 bytes per bar of a screened
  symbol); the drop-window peak reads the memory-mapped high / low arrays (no 72 MB private copy per process).
* The API key is scrubbed (`fmp_common.redact`) from the screener's request/exception log lines.

## 6. Request to the stage-log developer (`feat/screener-stage-log` @ 81b2bec7)
It logs one `LIVE STAGES` line (counts per stage + FMP failure counters) and the existing `LIVE SELECTION` line. For a replayable parity
test the following is MISSING (additive, no change of selection): (1) the vendor request parameters and the wall-clock time of the call;
(2) the raw vendor response rows per symbol: `symbol, price, marketCap, volume` (one JSONL per distinct request, de-duplicated by request
hash); (3) per candidate after stage 2: the `/quote` price and market cap used, the quote's timestamp, `volume`, `avg_volume`,
`relative_volume`, the history window actually returned (first/last bar date, whether a bar dated today was present and its close);
(4) the symbol list after EACH stage (not only counts) and the final ORDERED list with the rank key; (5) per candidate of the price-drop walk:
symbol, peak, current, drop; (6) per Weinstein candidate: the number of closes, SMA150, SMA150 20 bars earlier, slope, last close; (7) the
resolved settings dict. `tools/screener_parity_report.py` reads `LIVE SELECTION` and `LIVE STAGES` and prints the stage counts next to the
simulation's `diag` (same keys as live's stats).
