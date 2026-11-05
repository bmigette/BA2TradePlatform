# Task 11: Per-Account Live HISTORY Source Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Parent spec: `docs/plans/2026-10-20-alpaca-history-source.md` ("Phase 2", "Task 11 (amended)", "Task 11: Live ACCOUNT setting + top-up").
> Branch `feat/history-source-task11` (from origin/dev 46e7aa85). PLAN ONLY: no code was changed while writing it.

**Goal:** Every account carries an `ohlcv_history_source` setting (`fmp` | `alpaca`, declared default `fmp`). The OHLCV
HISTORY that an expert / transaction of that account reads live (indicators, ATR, option spot history, intraday TA) comes
from the selected source through ONE resolution function, and an `alpaca` account is served the Alpaca-derived,
split-verified cache topped up live with RAW finished sessions. Current prices are untouched (account price feed, `fmp`
since APP 1249). Deploying with every account on `fmp` changes nothing.

**Architecture:** Pure naming/validation stays in `ba2_common.core.ohlcv_history` (already: `HISTORY_SOURCES`,
`check_history_source`, `history_registry_key`). Task 11 adds (a) the pure `account_history_source(settings)` there,
(b) ONE host function `core/account_history.py::history_provider_for_account(account_id)` (in-tree: it needs live
registries + account instances) that every account-bound site calls, (c) a live top-up inside
`AlpacaHistoryOHLCVProvider` (package `ba2_providers`) that fetches RAW finished sessions, applies the verified split
table and appends under the same basis rules as the build, and (d) a corporate-actions refresh job that adds new splits
through the SAME `decide_splits` rule and re-bases affected symbols' files. BT/live parity: the top-up and the refresh
reuse `build.adjust` / `decide_splits` (one function, two callers), never a re-implementation.

**Tech Stack:** Python 3.11, pandas/pyarrow, `ba2_common` / `ba2_providers` / `ba2_experts` packages (shared code goes
in packages, host wiring in-tree), APScheduler (`JobManager`), NiceGUI settings dialog, pytest.

## Verified code map (checked against 46e7aa85, 2026-10-24; spec line numbers had drifted)

| Spec site | Now | Account-bound? | Plan |
|---|---|---|---|
| `core/TradeManager.py:279` `get_provider("ohlcv","fmp")` in `_refresh_market_regime` (SPY, 1200 days, once per day per PROCESS) | line 279 | NO: one platform-wide read, no account in scope | stays FMP (Task 9 decision, open question 3) |
| `modules/dataproviders/options/atm_iv_history.py:543-545` `_default_ohlcv` (`FMPOHLCVProvider()`, `_verified_tail_topup`) and `:605-606` `_default_splits` (`FMPOHLCVProvider()._split_calendar`) | 543-545, 605-606 | spot history for ATM IV; the class is built per account/expert call site (verify in Task 8) | Task 8 |
| expert provider bundle: `ba2_common/core/backtest_context.py:29` `LIVE_OHLCV_PROVIDER = "fmp"`, `LiveProviderBundle.ohlcv()` `:85`, `MarketExpertInterface._live_providers` `:1482` | as listed | YES (expert -> `account_id`) | Task 6 |
| default indicator stack (ATR for classic RM / SmartRM): `core/seam_helpers.py:74,92` `DEFAULT_INDICATOR_OHLCV_PROVIDER = "yfinance"`, used by `seam_wiring.py:138`, `SmartRiskManagerToolkit.py:1944-1951`, `warm_service.py:285` | as listed | account-bound only through its caller; today it is **yfinance, not FMP** (a third source nobody mentioned) | Task 7 + open question 2 |
| `ui/pages/symbol360.py:113,122`, `ui/components/option_structure_chart.py:385`, `modules/experts/TradingAgentsUI.py:99-100` | as listed | NO (account-less UI) | unchanged, pinned by a test (Task 9) |
| `FactorRanker/data.py:182` (`LIVE_OHLCV_PROVIDER`), `PennyMomentumTrader/{monitoring.py:233,1282, screening.py:503}` (`ohlcv_vendor` setting) | as listed | experts' own provider choice | FactorRanker follows the bundle (Task 6); Penny keeps its explicit vendor setting (not touched) |
| `AlpacaHistoryOHLCVProvider._get_ohlcv_data_impl` / `_refresh_parquet_if_stale` raise `HistoryTopUpUnavailable` | `packages/providers/ba2_providers/ohlcv/AlpacaHistoryOHLCVProvider.py:50,60` | - | Tasks 2-5 replace them |
| account settings definitions | `AlpacaAccount.get_settings_definitions` `:442`, `TastyTradeAccount:360`, `IBKRAccount:359` (all static per class; base `get_merged_settings_definitions` = `_builtin_settings` + class dict, `ExtendableSettingsInterface.py:160-196`) | - | Task 6 |

Facts the design rests on (all from the code):
- Cache files are on the build's asof basis (`build.adjust`: prices / F, volume * F, F = product of accepted events dated after the bar; `_meta/build_manifest.json` has `store_asof` and per-symbol `events`; `_meta/split_table.json` the accepted events).
- `native_cache.write_timeseries` is the only parquet writer: atomic temp+rename, in-process per-path `threading.Lock`, and it silently drops unfinished bars (`ohlcv_final_bars`, `SETTLE_AFTER_CLOSE = 4h`). There is NO cross-process lock and `filelock` is not a dependency.
- `MarketDataProviderInterface._verified_tail_topup` + `ohlcv_topup_guard.verify_topup` is the existing overlap-verified append machinery (FMP); it calls `self._get_ohlcv_data_impl(symbol, probe_start, fetch_end, interval)` for an overlap probe, so implementing `_get_ohlcv_data_impl` with ADJUSTED output makes the existing guard work unchanged.
- `tools/alpaca_history_download.py` (Transport, `fetch_bars`, `bars_to_frame`) lives in `tools/`, which `packages/*` cannot import (CI installs `packages/*` only). The live fetch needs its own small client in the provider package.
- The live app holds Alpaca keys per account (`api_key` / `api_secret`, `AlpacaAccount.py:450-451`); TastyTrade and IBKR accounts hold NO Alpaca keys.

## Rules that apply to every task
- TDD: failing test first, run it red, minimal code, run green, commit. Each task ends with its own commit.
- Run pytest with the app venv: `.venv\Scripts\python.exe -m pytest <file> -q` from the repo root for `tests/`; package tests from the package dir (conftest clash): `cd packages\providers && ..\..\.venv\Scripts\python.exe -m pytest tests\<file> -q` (same for `common`, `experts`). One pytest at a time.
- Config: explicit dict access, no `.get(k, default)`. Live data: no fallbacks (a failed Alpaca read is a named error, NEVER a silent FMP read). No `except Exception: pass`.
- `logger.error(..., exc_info=True)` only inside `except`.
- Shared code -> packages (`ba2_common`, `ba2_providers`, `ba2_experts`); account-bound resolution / scheduling / UI -> in-tree.

---

## Task 0: Owner decisions (2026-10-24, ANSWERED; they override the drafts below)

1. **Live bars append IN PLACE, UNGATED.** Live-fetched Alpaca bars are appended to `CACHE_FOLDER/AlpacaHistoryOHLCVProvider/`
   files (no overlay folder, no build-gate verification on top-ups). Backtests past the build end (2026-10-12) may read
   live-appended bars; the owner accepts this. Safety kept: finished sessions only (`ohlcv_final_bars`), the split table
   applied (`build.adjust`), never a silent basis mix (per-symbol effective basis in the manifest, see 3), a cross-process
   per-symbol file lock against concurrent writers (readers rely on `os.replace` atomicity).
   **Continuity rule (owner addition, hard):** an append starts at the FIRST regular NYSE session after the file's last bar
   (holidays / half days / weekends are not gaps; `ba2_common.core.market_calendar`) and covers EVERY finished session up
   to the newest one. Alpaca's series missing any session in that range -> append NOTHING for that symbol/interval, log
   ERROR naming symbol, interval and the missing sessions, file untouched, the next refresh retries. No duplicate /
   overlapping rows (an already-present session is not re-appended); timestamps strictly increasing after the append. A file
   of ANY age is caught up in one contiguous append, fetched in requests of at most `CHUNK_SESSIONS = 30` sessions and appended
   only when the whole range is in (owner 2026-10-24: no refusal by age; the build-age startup check is a WARNING only). 5-min files: every appended SESSION must be present; Alpaca emits no
   bar for a slot without trades, so slots are NOT required. Chosen completeness rule = the build's own
   (`tools/alpaca_history_download.py`, `days_without_intraday_bars`: every session that has a daily bar must have at least one
   regular-session 5-min bar), regular session only (09:30..15:55 starts, `build.to_native_5min`); the build's selfcons check
   (`verify.SELFCONS_TOL`, last 5-min close vs daily close) is a report there, not a rejection, so it is not one here either.
2. **ATR follows the account's history source.** `alpaca` accounts read ATR from the Alpaca history; `fmp` accounts keep the
   yfinance stack (no behaviour change on deploy). (Task 7/8.)
3. **Live splits EXTEND `_meta/build_manifest.json` directly** (no `live_state.json`): a re-base appends the accepted event to
   the symbol's `events.splits` (and sets `live.effective`), atomically, under a lock, with a `.bak` backup, idempotently.
   Consequences the implementer MUST handle (found while reading `verify.py` / `option_split_basis.py`): (a) the PASS gate
   certificate pins `sha256(build_manifest.json)`, so every manifest extension re-stamps `gate_certificate.json`
   (`build_manifest_sha256`, plus `live_extended_at`) in the same locked section, backing it up too; (b) the option check
   compares the table's events with the manifest's, so the corporate-actions job (Task 6) must extend `split_table.json` for the
   same event and update `manifest["split_table"]["sha256"]` in the same transaction. Plain top-ups do NOT touch the manifest
   (8 MB; only a re-base changes the basis).
4. **Rollout:** ALL accounts switch to `alpaca` at once after deploy (dev 1-3, prod 1, options 1 Alpaca accounts). The
   operator switches them, not the implementer. Consequence: a symbol absent from the build is refused (loud), see Task 12.

5. **New symbols are SUPPORTED (owner 2026-10-24, overrides "symbols not in the build are refused").** A live provider asked for
   an interval of a symbol with no file BACKFILLS it on first read (`alpaca_history/live_backfill.py`): RAW bars from the build's
   own history start (earliest first bar in the manifest, per interval) or the symbol's first Alpaca bar, whichever is LATER, to
   the newest finished session; the SAME contiguity rule as top-ups (a missing session after the first bar -> nothing written,
   ERROR); the symbol's corporate actions fetched once and judged by `decide_splits` (`corporate_actions.decide_events`); an event
   sent to review, or an unexplained overnight jump, refuses the backfill. Written under the symbol lock; the manifest entry
   (`origin: "live_backfill"`, `backfilled_on`, events, per-interval first/last/bars), `split_table.json` + its sha256 in the
   manifest and the gate certificate are updated in ONE locked section BEFORE the bar file is written (an entry without a file is
   backfilled again; a file without an entry would be refused). A 5-minute backfill of a symbol already in the manifest is adjusted
   with the manifest's events (one basis per symbol). Non-US-ticker / non-NYSE symbols and symbols Alpaca does not have stay refused
   (`SymbolUnavailable`). The daily 5-min history is large (build start to now): it is fetched only when the 5-min interval is read.

6. **Amendment (owner 2026-10-24): download no more than needed; BT fetches "aside".** The first read of a symbol with no file
   fetches ONLY the requested interval (daily OR 5-minute) for ONLY the caller's requested range, `[start_date, newest finished
   session]` (clipped to the build's own start), contiguous within that range, splits judged for that range. A later read needing
   an earlier start extends the file BACKWARDS by just the missing range (contiguous with the existing first bar, the new
   range's splits judged, the seam checked); later sessions arrive through the normal top-up. The manifest marks the symbol
   `origin: live_backfill` and each interval `partial: true` with `covered_from` (the earliest date the file was asked to cover;
   an IPO's first bar may be later). The BACKTEST reader (the keyless `AlpacaHistoryOHLCVProvider`) REFUSES, naming the symbol and
   the rebuild (`PartialLiveBackfill`), a window starting before `covered_from`; a backtest therefore never treats a live-backfilled
   partial file as a built symbol (it is downloaded aside by a rebuild). This changes the keyless read -> `ba2_providers` minimum raised.
7. **Warm-up and short history (owner: "Still need 200 days for EMA. New symbol with not enough history should not trade.").**
   The backfill range is the caller's start, and every account-bound reader already puts its warm-up in that start:
   `PandasIndicatorCalc` (ATR, SMA/EMA, RSI, ...) fetches 365 calendar days before the visible window, DeterministicScorer 600,
   FactorRanker 400, ETFTrend `ceil(max(momentum+1, trend)*7/5)+120`, FMPEarningsEvent 900; pinned by a test (a first 200-SMA
   read fetches >= 300 days before the window). A history shorter than an indicator's window raises `InsufficientHistory`
   (`ba2_common.core.ohlcv_history`) from `PandasIndicatorCalc` (shared by BT and live; the BT metric-store ATR already returns
   no value) and `get_latest_atr` turns it into "no ATR" at WARNING. Per expert, what exists today: DeterministicScorer refuses
   (`min_history_days` 260 -> `skip_reason=insufficient_history`); PullbackReversion raises "Insufficient history" (MIN_BARS);
   ETFTrend raises "Insufficient ETF warmup"; FactorRanker returns no factor for a series shorter than its lookback
   (`factors.py`) and needs `k` measured factors; FMPRating / FMPEarningsDrift / FMPInsiderClusterBuy / FMPSenateTraderWeight /
   FMPSenateTraderCopy compute no OHLCV-window indicator (the price at the decision date only), FMPEarningsEvent measures
   historical event moves from earnings rows: none of these changed.

Defaults kept: global regime-overlay SPY read stays FMP; TastyTrade/IBKR refuse `alpaca` (no Alpaca keys); the
corporate-actions job covers only symbols held/traded by `alpaca` accounts at 08:30 ET; the 5-minute top-up stays.

---

## Task 1: Pure account-source resolution in `ba2_common`

**Files:**
- Modify `packages/common/ba2_common/core/ohlcv_history.py` (append after `other_history_cache_dirnames`, ~line 80)
- Test `packages/common/tests/test_ohlcv_history_account.py` (new)

**Step 1: failing test**
```python
import pytest
from ba2_common.core.ohlcv_history import (
    ACCOUNT_HISTORY_SOURCE_KEY, ACCOUNT_DEFAULT_HISTORY_SOURCE, UnknownHistorySource,
    account_history_source)

def test_key_and_declared_default():
    assert ACCOUNT_HISTORY_SOURCE_KEY == "ohlcv_history_source"
    assert ACCOUNT_DEFAULT_HISTORY_SOURCE == "fmp"          # owner 2026-10-20: live stays FMP until chosen

def test_unset_means_declared_default():
    assert account_history_source({}) == "fmp"
    assert account_history_source({"ohlcv_history_source": None}) == "fmp"

def test_alpaca_round_trip_and_case():
    assert account_history_source({"ohlcv_history_source": "alpaca"}) == "alpaca"
    assert account_history_source({"ohlcv_history_source": " Alpaca "}) == "alpaca"

@pytest.mark.parametrize("bad", ["", "yfinance", "FMP2", 3])
def test_unknown_refuses(bad):
    with pytest.raises(UnknownHistorySource):
        account_history_source({"ohlcv_history_source": bad})
```
(`""` refuses; only an ABSENT / `None` setting is "unset". The settings loader maps a stored empty string to `None`
(`ExtendableSettingsInterface.py:130-133`), so confirm that in the test with a real settings dict in Task 6.)

**Step 2:** `cd packages\common && ..\..\.venv\Scripts\python.exe -m pytest tests\test_ohlcv_history_account.py -q` -> FAIL (ImportError).

**Step 3: implement** `ACCOUNT_HISTORY_SOURCE_KEY`, `ACCOUNT_DEFAULT_HISTORY_SOURCE = "fmp"`, and
`account_history_source(settings: Mapping) -> str`: `raw = settings[KEY] if KEY in settings else None`; `None` ->
default; `str` -> `check_history_source(raw.strip().lower())`; non-str -> `UnknownHistorySource`. This is the only place
that spells the default.

**Step 4:** pytest PASS. **Step 5:** commit `feat(history): pure account_history_source (declared default fmp)`.
(Versions are bumped once, in Task 12.)

---

## Task 2: Live raw-bar client in the provider package (network failures loud)

**Files:**
- Create `packages/providers/ba2_providers/ohlcv/alpaca_history/live_fetch.py`
- Test `packages/providers/tests/test_alpaca_history_live_fetch.py` (new)

Design: `class AlpacaLiveBars` with `__init__(api_key, api_secret, *, transport=None)` and
`daily(symbol, start: date, end_exclusive: date) -> pd.DataFrame` / `intraday_5min(...)`, returning RAW
(`adjustment=raw`), `feed=sip`, columns `t,o,h,l,c,v` converted to the native layout by the existing
`build.to_native_daily` / `to_native_5min` (reuse, do not re-implement the tz/RTH rules). Use the same request shape as
`tools/alpaca_history_download.py::fetch_bars` (params at `:257-261`) and the same refusal rules as `bars_to_frame`
(missing columns / nulls refuse). `transport` is injectable (`requests.Session`-like) so tests make no network call.
Errors: `AlpacaLiveFetchError(RuntimeError)` for HTTP != 200, malformed JSON, pagination loop; 429/5xx retried at most
3 times with backoff (no sleeping in tests: injected `sleep`), then raise. NEVER return an empty frame to hide a failure;
an empty answer for a finished session that Alpaca should have is returned as empty and judged by the caller (Task 3).

**Step 1: failing tests** (fake transport):
```python
def test_daily_raw_request_shape(fake):           # adjustment=raw, feed=sip, timeframe=1Day, symbol translated
    AlpacaLiveBars("k", "s", transport=fake).daily("BRK.B", date(2026,10,19), date(2026,10,24))
    q = fake.requests[0].params
    assert q["adjustment"] == "raw" and q["feed"] == "sip" and q["timeframe"] == "1Day"
    assert q["symbols"] == "BRK.B"                 # Alpaca spelling comes from tools' alpaca_symbol rule: pin it

def test_pages_are_followed_and_loop_detected(fake_paged): ...
def test_http_500_raises_after_three_tries(fake_500, no_sleep):
    with pytest.raises(AlpacaLiveFetchError, match="500"): ...
    assert fake_500.calls == 3
def test_null_bar_values_refuse(fake_null): ...
def test_native_layout_equals_build_layout(fake): # columns Date,Open,High,Low,Close,Volume,effective_date; NY-naive
```
**Step 2:** run -> FAIL. **Step 3:** implement. **Step 4:** PASS. **Step 5:** commit
`feat(alpaca-history): live raw-bar client (sip, raw, loud failures)`.

---

## Task 3: Top-up core: finished sessions only, split table, basis guard

**Files:**
- Create `packages/providers/ba2_providers/ohlcv/alpaca_history/live_topup.py` (pure, injectable fetcher + clock)
- Test `packages/providers/tests/test_alpaca_history_live_topup.py` (new)

Pure function `topup_frame(cached: DataFrame, raw_new: DataFrame, events: Sequence[(date, factor)], *, interval,
now_utc, symbol) -> (merged, report)`:
1. Sessions to fetch: from `cache_end + 1 session` through the newest session for which
   `ohlcv_final_bars.session_is_final(symbol, day, now)` (daily; for 5-min the last bar whose interval ended and is final).
   NEVER a session that is not final (the free key's 15-min SIP delay and settlement then never touch history).
2. `raw_new` rows dated outside that range -> `TopUpRefused` (a forming bar is never written; `native_cache` also drops it).
2b. **Continuity (Task 0 item 1):** `expected_sessions(symbol, after_last_bar, newest_final)` from the NYSE calendar; any expected session missing from `raw_new` (daily), or without >= 1 regular 5-min bar (5min), -> `TopUpGap` (ERROR log naming symbol/interval/sessions), nothing appended. Rows already present are dropped, never re-appended; the result must be strictly increasing. More than `MAX_BACKFILL_SESSIONS` (30) behind -> `TopUpTooFarBehind`.
3. Adjust `raw_new` with `build.adjust(raw_new, [e for e in events if e.date > bar_date])`: for bars after the last
   known event the factor is 1; events dated AFTER the cache end exist only if the refresh job already accepted them
   (Task 5), and then the file was re-based first (Task 4), so the invariant holds: **file and appended bars are on ONE
   basis = "all accepted events with date <= `effective` applied"**.
4. (Owner decision 1: UNGATED. The overlap probe + `verify_topup` below is dropped; the seam/unexplained-jump check in 5 is the only price check.) Draft, superseded: Overlap check, the existing guard: ask the fetcher for the last `ohlcv_topup_guard.TOPUP_OVERLAP_BARS` CACHED
   sessions as well, adjust them the same way, and `verify_topup` against the cache; `basis_change` /
   `vendor_unadjusted` / `unexplained` / `no_overlap` -> `OHLCVTopUpRefused` (nothing written), exactly like FMP.
5. Unexplained raw overnight jump at the seam (the first appended Open vs the last cached Close beyond
   `split_table.JUMP_HI` / `JUMP_LO`) with no accepted event within `NEAR_SESSIONS` -> `PendingCorporateAction` (named):
   a split has happened that the table does not know yet. The caller (Task 5 hook) runs the refresh for that symbol and
   retries ONCE; if still unexplained it raises. Never appends a mixed basis.
6. A symbol absent from the manifest (not in the build) -> `SymbolNotInHistory` (named; no cold backfill in this task,
   see open question 7).

**Step 1: failing tests** (synthetic frames; reuse constants, not copies):
```python
def test_appends_only_final_sessions(...)          # now = Fri 15:00 NY -> Fri bar NOT requested/appended; Thu is
def test_events_after_bar_divide_prices_and_multiply_volume(...)
def test_overlap_disagreement_refuses_and_writes_nothing(...)
def test_unexplained_2to1_seam_raises_pending_corporate_action(...)   # raw price halves, no event
def test_explained_split_seam_is_accepted_when_table_has_it(...)
def test_no_new_session_is_unchanged(...)         # weekend: no fetch at all (fake counts calls == 0)
def test_empty_answer_for_a_final_session_raises(...)  # Alpaca returned nothing for a finished weekday -> loud
def test_5min_rth_only_and_78_bars(...)
```
**Step 2-4:** red, implement, green. **Step 5:** commit `feat(alpaca-history): live top-up core (final sessions, table, guard)`.

---

## Task 4: Per-file basis record, cross-process lock, and re-base

**Files:**
- Create `packages/providers/ba2_providers/ohlcv/alpaca_history/live_state.py`
- Test `packages/providers/tests/test_alpaca_history_live_state.py` (new)

Pieces:
- **Basis record = the build manifest (owner decision 3):** `_meta/build_manifest.json` `symbols[SYM].events.{splits,spinoffs}` is the basis of the files; `symbols[SYM].live = {"effective": date of the last re-base}` is added by a re-base. (Superseded draft: `live_state.json`; not created.)
  `effective` = the date through which accepted events are applied to that symbol's files. Read on every top-up; a file
  whose record is missing is initialised from `build_manifest.json` (`store_asof`, `symbols[S].events`), never guessed.
  (The manifest IS extended by a re-base, with `.bak`, lock, atomic replace, certificate re-stamp; see Task 0 item 3.)
- **Lock**: `locked_symbol(cache_dir, symbol)` context manager, cross-process (`os.open(O_CREAT|O_EXCL)` lock file under
  `_meta/locks/<SYM>.lock` with pid + timestamp, stale-lock takeover after N minutes with a WARNING; no new dependency).
  Held across read-check-fetch-write of ONE symbol (both intervals). Readers (backtests, `native_cache.read_timeseries`)
  take NO lock: they rely on `os.replace` atomicity (a reader sees the old or the new complete file). Test that.
- **Re-base** `rebase_symbol(cache_dir, symbol, new_events)` (also: `file basis != manifest` detection): under the lock, for BOTH files divide pre-event prices by the
  new factor and multiply volume (`build.adjust` with only the new events), write each file atomically via
  `native_cache.write_timeseries` (so the final-bar rule applies), THEN update the basis record. Crash between the file
  writes and the record: the next call detects `file basis != record` by comparing the seam ratio at the event date and
  REFUSES (named `BasisRecordMismatch`), never re-divides blindly. Order: daily file, 5-min file, manifest (+ certificate).
- Idempotence: re-basing for an event already in `record.events` is a no-op returning `False`.

**Step 1: failing tests** (tmp cache dir, real parquet):
```python
def test_rebase_halves_pre_event_prices_and_doubles_volume(tmp_cache)
def test_rebase_twice_is_a_noop(tmp_cache)
def test_rebase_updates_both_intervals_then_record(tmp_cache)
def test_crash_after_files_before_record_refuses_next_call(tmp_cache, monkeypatch)
def test_reader_never_sees_half_a_file(tmp_cache)       # thread: writer loops rebase, reader reads 200x, every frame consistent
def test_two_processes_serialize(tmp_cache)             # multiprocessing: second waits, then sees first's record
def test_stale_lock_taken_over_with_warning(tmp_cache)
def test_basis_record_initialised_from_build_manifest(tmp_cache)
def test_backtest_pinned_read_unchanged_by_topup(tmp_cache) # a read with end_date <= old end returns byte-identical rows after a top-up
```
**Step 2-4:** red/green. **Step 5:** commit `feat(alpaca-history): basis record, cross-process symbol lock, re-base`.

---

## Task 5: Wire the top-up into `AlpacaHistoryOHLCVProvider` (credentials from the account)

**Files:**
- Modify `packages/providers/ba2_providers/ohlcv/AlpacaHistoryOHLCVProvider.py:41-76`
- Modify `packages/providers/ba2_providers/__init__.py:76` area only if the registry needs kwargs support (check `get_provider(**kwargs)` passes them to the constructor; it does, `__init__.py:158-177`)
- Test `packages/providers/tests/test_alpaca_history_provider.py` (extend; existing cache-only tests must stay green)

Behaviour:
- `AlpacaHistoryOHLCVProvider(api_key=None, api_secret=None, *, fetcher=None, clock=None)`. WITHOUT credentials it stays
  exactly today's cache-only provider (backtests, GA workers, `cached_only=True`): `_get_ohlcv_data_impl` still raises
  `HistoryTopUpUnavailable`. WITH credentials the impl fetches via `AlpacaLiveBars`, adjusts with the split table
  (`build.load_split_table`) and the basis record, and returns the ADJUSTED frame so the inherited
  `_verified_tail_topup` (overlap guard, `OHLCVTopUpRefused`, refusal memo) works unchanged for daily; intraday uses
  `topup_frame` directly in `_refresh_parquet_if_stale`.
- `_refresh_parquet_if_stale`: with credentials -> `locked_symbol` -> `topup_frame` -> `native_cache.write_timeseries`
  -> update `last_topup`; `PendingCorporateAction` -> run `corporate_actions.refresh_symbol` (Task 6) once, retry once.
  Without credentials -> current raise.
- A pinned backtest read (`end_date` not latest) never reaches the top-up (the base only tops up a LATEST request,
  `_is_latest_request`), so a backtest of a past window is byte-stable. State this in a test.
- Network failure -> `AlpacaLiveFetchError` propagates (the daily top-up's FMP-style "serve stale cache with a warning"
  is NOT acceptable here: `_verified_tail_topup(raise_fetch_errors=True)` is used, and the live read raises; the CALLER
  decides, see Task 7/8 for what each site does: log ERROR and skip the decision input, never read FMP).

**Step 1: failing tests**
```python
def test_no_credentials_is_still_cache_only(...)                         # existing behaviour pinned
def test_latest_daily_read_with_credentials_appends_final_sessions(...)  # fake fetcher, clock Mon 10:00 NY -> Fri added
def test_pinned_read_does_not_fetch(...)                                 # fetcher.calls == 0
def test_fetch_error_propagates_and_cache_untouched(...)
def test_split_in_new_bars_triggers_one_refresh_then_retry(...)
def test_second_failure_after_refresh_raises(...)
```
**Step 2-4:** red/green. **Step 5:** commit `feat(alpaca-history): live top-up with account keys (final sessions only)`.

---

## Task 6: Corporate-actions refresh (same `decide_splits`) and its daily job

**Files:**
- Create `packages/providers/ba2_providers/ohlcv/alpaca_history/corporate_actions.py`
- Modify `ba2_trade_platform/core/JobManager.py` (`_schedule_system_jobs` ~667-697; new `_schedule_history_corporate_actions_job` + `_execute_history_corporate_actions_refresh`, next to `_schedule_fred_preopen_job` ~797 / `_execute_fred_preopen_refresh` ~882)
- Tests: `packages/providers/tests/test_alpaca_history_corporate_actions.py`, `tests/test_history_corporate_actions_job.py`

Package side `refresh(cache_dir, symbols, *, today, ca_client, raw_fetcher) -> RefreshReport`:
1. Symbols = the union of symbols of accounts set to `alpaca` that are held or traded (host passes the list), never the
   whole 11k store (rate limit; open question 5).
2. For each symbol: fetch Alpaca corporate actions (splits, reverse splits, unit splits, stock dividends, spin-offs) with
   `ex_date > record.effective` through `today`; fetch the symbol's RAW daily bars around each event (+/- 10 sessions).
3. Judge with **the same `split_table.decide_splits(raw_daily, alpaca_events, fmp_events, overrides, ...)`**, the same
   constants and the same `split_overrides.json`. Event not yet provable from the price (ex-date is today/tomorrow, no
   post-event bar) -> stays `pending` (not applied, retried next run, listed). An event `decide_splits` sends to review
   (`split_review`) is NEVER applied; it is written to `_meta/live_split_review.csv`, an ERROR is logged naming symbol and
   event, and top-ups for that symbol REFUSE (`PendingCorporateAction`) until the owner adds an override. No silent skip.
4. Accepted events -> `rebase_symbol` (Task 4). Update `record.events` / `effective`. The run writes
   `_meta/corporate_actions_live.json` `{run_date, symbols_checked, applied, pending, review}`.
5. Idempotent: same inputs twice -> second run applies nothing and reports `applied=[]`.
6. FMP second opinion: `decide_splits` takes `fmp_events`; the live refresh passes FMP's split calendar when the FMP key
   exists (read-only, vendor corroboration as in the build); absent key -> `fmp_events=[]`, said once in the report.

Host side: daily CronTrigger (US/Eastern, `market_time` semantics like the FRED pre-open job; default **08:30 ET**, after
Alpaca's overnight corporate-action posting and before the 09:30 entry window; avoid 09:30 per memory "FMP 09:30
collision") with `replace_existing=True` (the job must survive `refresh_scheduled_jobs`, the IV-snapshot-job-lost bug).
The job runs only when at least one account has `ohlcv_history_source = alpaca`; otherwise it is not scheduled (logged at
INFO). Failure -> ERROR + retry at +15 min, max 3 (the FRED retry pattern `_schedule_fred_preopen_retry` ~906), then a
persistent ERROR; top-ups then refuse via the staleness check below.

**Staleness gate (top-up side):** a top-up requires `corporate_actions_live.json.run_date >=` the previous NYSE session;
older -> `CorporateActionsStale` (named, loud). This is what stops the live reads when the job is dead.

**Step 1: failing tests**
```python
def test_unproven_split_is_pending_not_applied(...)       # no post-event bar yet
def test_proven_2to1_split_rebases_both_files(...)
def test_refresh_idempotent(...)
def test_review_verdict_is_never_applied_and_blocks_topup(...)
def test_uses_decide_splits_not_a_copy(monkeypatch)       # patch split_table.decide_splits with a spy; assert it ran
def test_fmp_disagreement_follows_decide_splits_verdict(...)
def test_stale_run_blocks_topup(...)
# host
def test_job_scheduled_only_if_an_account_is_alpaca(...)
def test_job_survives_refresh_scheduled_jobs(...)         # replace_existing, same id after refresh
def test_failure_retries_three_times_then_errors(...)
```
**Step 2-4:** red/green. **Step 5:** commit `feat(alpaca-history): daily corporate-actions refresh (decide_splits, re-base) + job`.

---

## Task 7: The ACCOUNT setting, validation, settings UI

**Files:**
- Modify `packages/common/ba2_common/core/interfaces/AccountInterface.py` (add the shared definition; see below)
- Modify `ba2_trade_platform/modules/accounts/AlpacaAccount.py:442-467`, `TastyTradeAccount.py:360`, `IBKRAccount.py:359` ONLY if the base cannot carry it (prefer the base)
- Modify `ba2_trade_platform/ui/pages/settings.py` only if the dialog does not render a shared definition (check `valid_values` dropdown handling first; `data_feed` already renders one)
- Tests: `tests/test_account_history_source_setting.py` (new); extend `tests/test_settings_dialog_missing_defaults.py` pattern

Design: ONE definition in `AccountInterface` merged into every account class so the three types cannot drift:
```python
"ohlcv_history_source": {
    "type": "str", "required": False, "default": "fmp",
    "description": "Live OHLCV history source",
    "valid_values": ["fmp", "alpaca"],
    "tooltip": "fmp = FMP daily/5-min history (cache topped up from FMP). alpaca = Alpaca-derived, split-verified "
               "history (the cache backtests use), topped up with raw finished sessions; needs Alpaca market-data "
               "keys (Alpaca accounts only). Current prices are set by the price feed, not this.",
}
```
`default` is taken from `ba2_common.core.ohlcv_history.ACCOUNT_DEFAULT_HISTORY_SOURCE` (one spelling), and
`valid_values` from `sorted(HISTORY_SOURCES)`. First check how the settings page and `get_setting_with_interface_default`
read definitions (`get_settings_definitions` vs `get_merged_settings_definitions`); the test below decides which hook
carries the shared entry. Do not re-use the `_builtin_settings` dict (it is expert-shaped and class-global).

Validation (`validate_account_history_source(account_cls, settings)` in-tree `core/account_history.py`, called by the
settings save path and again at resolution):
- unknown value -> `UnknownHistorySource`, the dialog shows the message, nothing saved;
- `alpaca` on an account without `api_key` + `api_secret` (TastyTrade, IBKR, or an Alpaca account missing keys) ->
  `HistorySourceNeedsAlpacaKeys` (named): refuses to save, and refuses at resolution.

**Step 1: failing tests**
```python
@pytest.mark.parametrize("cls", [AlpacaAccount, TastyTradeAccount, IBKRAccount])
def test_every_account_type_declares_the_setting(cls):
    d = cls.get_merged_settings_definitions()["ohlcv_history_source"]
    assert d["default"] == "fmp" and d["valid_values"] == ["alpaca", "fmp"] or set(d["valid_values"]) == {"fmp","alpaca"}
def test_unset_resolves_to_fmp_for_all_types(...)
def test_saving_unknown_value_is_refused(...)
def test_alpaca_on_tastytrade_refused_with_named_error(...)
def test_alpaca_on_alpaca_without_keys_refused(...)
def test_settings_dialog_renders_dropdown_for_it(...)      # pattern of test_settings_dialog_missing_defaults.py
def test_existing_accounts_without_the_row_get_the_default_not_a_crash(...)
```
**Step 2-4:** red/green. **Step 5:** commit `feat(accounts): ohlcv_history_source setting on every account type (default fmp)`.

---

## Task 8: ONE host resolver + the account-bound call sites

**Files:**
- Create `ba2_trade_platform/core/account_history.py`
- Modify `ba2_trade_platform/core/seam_wiring.py` (live resolver, near `:731` default) and `ba2_common/core/backtest_context.py:29,85` / `MarketExpertInterface._live_providers:1482`
- Modify `ba2_trade_platform/modules/dataproviders/options/atm_iv_history.py:543-545,605-606`
- Modify `ba2_trade_platform/core/seam_helpers.py:74-92`, `SmartRiskManagerToolkit.py:1944-1951`, `warm_service.py:285` (indicator stack)
- Tests: `tests/test_account_history_resolver.py`, `tests/test_history_sites_follow_account.py`

`core/account_history.py` (the single function, nothing else decides a live history source):
```python
def history_source_for_account(account_id: int) -> str          # settings -> ba2_common account_history_source
def history_provider_for_account(account_id: int, *, interval_hint=None)
    # fmp   -> get_provider("ohlcv", "fmp")
    # alpaca-> get_provider("ohlcv", "alpaca_hist", api_key=..., api_secret=...)   (keys from the account's settings)
def history_provider_for_expert(expert_id: int)                  # expert.account_id -> the above
def history_provider_for_transaction(transaction_id: int)        # transaction -> account
```
Unresolvable account/expert/transaction -> raise (no FMP fallback). Instances come from `get_account_instance_from_id`
(cached; `/api/reload` drops it, so a changed setting needs a reload; say so in the setting's tooltip).

Sites:
1. **Expert bundle.** `LiveProviderBundle` gets `ohlcv_name`; the in-tree resolver already wins for backtests. Live:
   `_live_providers()` passes the expert's account provider by overriding `ohlcv()` through an injected
   `ohlcv_factory` (a bundle argument, default = today's `get_provider("ohlcv", LIVE_OHLCV_PROVIDER)`), set by the host
   seam from `history_provider_for_expert(self.id)`. `indicators()` already builds on `self.ohlcv()`. Package change in
   `ba2_common` (bundle) + `ba2_experts` (FactorRanker `data.py:180-182` consumes the bundle's provider instead of
   `LIVE_OHLCV_PROVIDER`); host wiring in-tree. A backtest bundle is unchanged (it already names the run's history).
2. **atm_iv_history** (`_default_ohlcv`, `_default_splits`): the class receives the history source (constructor arg
   `history_source`, default NOT assumed: required keyword, resolved by its builder from the account). `fmp` path =
   today's code byte-for-byte. `alpaca` path reads `AlpacaHistoryOHLCVProvider` (topped up via the provider; no
   `_verified_tail_topup` duplicate) and its split factors come from the basis record (`live_state` events), not the FMP
   calendar. Failure -> `SpotSourceTransient` (existing taxonomy), never an FMP read.
3. **Default indicator stack** (ATR for classic RM / SmartRM, currently `yfinance`): NOT silently changed. With an
   account `alpaca` the ATR for that account's positions must read the same history as its experts (otherwise an Alpaca
   account sizes with yfinance ATR while the expert decided on Alpaca history, a BT/live asymmetry). Plan: build the
   indicator provider per account via `history_provider_for_account` when the account is `alpaca`; `fmp` accounts keep
   `yfinance` ATR EXACTLY as today (so default behaviour does not change). Open question 2 (should `fmp` accounts' ATR
   also move to FMP? it would be a live behaviour change, so NOT in this task).

**Step 1: failing tests**
```python
def test_fmp_account_gets_fmp_provider(...)
def test_alpaca_account_gets_alpaca_hist_with_its_keys(...)
def test_unresolvable_expert_raises_no_fmp_fallback(...)
def test_expert_bundle_ohlcv_follows_account(...)                  # two experts, two accounts, two providers
def test_indicators_built_on_the_account_provider(...)
def test_atm_iv_alpaca_uses_basis_record_not_fmp_calendar(...)     # FMPOHLCVProvider never constructed (monkeypatch raises)
def test_atm_iv_fmp_path_unchanged(...)                            # existing atm_iv tests still green
def test_fmp_accounts_atr_still_yfinance(...)                      # no behaviour change on deploy
def test_alpaca_fetch_error_never_reads_fmp(...)                   # patch FMPOHLCVProvider.__init__ to raise
```
**Step 2-4:** red/green. **Step 5:** commit per site (3 commits): `feat(live): expert bundle follows account history source`,
`feat(options): atm_iv_history follows account history source`, `feat(risk): per-account ATR history provider`.

---

## Task 9: Pin the account-less sites and the SPY gates (no change)

**Files:** Test only: `tests/test_history_account_less_sites_stay_fmp.py`

- `ui/pages/symbol360.py:113,122`, `ui/components/option_structure_chart.py:385`, `modules/experts/TradingAgentsUI.py:99-100`:
  source-level test (AST/grep of the three call expressions) that they still name `"fmp"` / `FMPOHLCVProvider`, so a
  later "resolve everywhere" refactor must decide on purpose.
- `TradeManager._refresh_market_regime` (`:279`): same pin, with the reason in the allowlist comment (global, once per day,
  no account).
- **Interaction with `feat/spy-regime-gate` (unmerged, 3fc1202b):** that branch adds `_fetch_benchmark_daily`
  (`get_provider("ohlcv","fmp")`, owner decision 2026-10-24: "live SPY data uses the same source as the stock price,
  FMP") and `_warn_benchmark_source_mismatch`, which compares each enabled Alpaca account's `data_feed` (the PRICE feed)
  to `fmp`. Task 11 changes neither: the history source is a different setting. Consequences to carry into the merge:
  (a) no code conflict expected (we do not touch `TradeManager.py`; verify with `git merge-tree` before merging whichever
  lands second); (b) the SPY gate stays FMP even for an `alpaca` history account, so the gate's regime and that account's
  indicator history can differ (the branch's own probe measured 0.07% trend / 0.80% vol of days); (c) if the owner later
  wants the gate on the account's history source, it is a one-line change to `history_provider_for_account` inside
  `_fetch_benchmark_daily` PLUS re-checking its BT parity test (`test_benchmark_regime_bt_live_parity.py`), whose backtest
  side reads the job's history source. Not done here. Add a test in this task, skipped unless the branch's symbols exist
  (`pytest.importorskip`-style on `_fetch_benchmark_daily`), asserting it still names `fmp`.

Commit `test(history): pin account-less history sites and the SPY gate on fmp`.

---

## Task 10: BT/live parity tests

**Files:** `tests/test_history_source_bt_live_parity.py`; `packages/providers/tests/test_alpaca_history_parity.py`

1. **Same bars, same function.** For synthetic raw sessions + a split table: (a) `build.adjust` on the whole raw series
   (what the BUILD does) equals (b) cache-up-to-D adjusted by the build + live `topup_frame` for the rest + (c) a later
   `rebase_symbol` for a new split. Frame equality (`pd.testing.assert_frame_equal`) on prices and volume. This is the
   "one shared function, two callers" proof.
2. **Re-base == rebuild.** Build from raw with the new split included versus the incrementally re-based file: identical.
3. **Basis never mixes.** Property test: random event sequences and top-up/refresh interleavings; the invariant "no seam
   ratio inside [0.55, 1.8] violated by an unexplained jump" and "record.effective == the max applied event" holds after
   every step; a refused step leaves files byte-identical.
4. **Decision parity.** A deterministic indicator (ATR 14) computed through the expert bundle with an `alpaca` account
   equals the same indicator computed by the backtest bundle (`history_registry_key("alpaca")`) over the same window.
5. **Known divergences asserted, not hidden:** the last (latest) bar of a live read is a FINISHED session in both
   (`ohlcv_final_bars`), pinned by a test at 09:30 and 10:00 ET.

Commit `test(history): BT/live parity for the Alpaca live top-up and re-base`.

---

## Task 11: Versions, guard, full suites

- Bump `PACKAGE_VERSION` (+ pyproject `version`) of `ba2_common` (2026.10.00014 -> next), `ba2_providers` (2026.10.00015 -> next),
  `ba2_experts` (2026.10.00006 -> next, only if FactorRanker/bundle files changed there). `APP_VERSION`
  (`ba2_trade_platform/version.py`, now 2026.09.1249) +1.
- GA relevance: the live top-up, refresh, lock, setting and live resolvers are live-only; the cache-only provider path
  (no credentials), `build.py` and `ohlcv_history.py` pure additions do not change a backtest. So **do not raise
  `required_package_versions.py`**; add narrow globs to `testplatform/ga_neutral_package_paths.py` in a reviewed change for
  the new modules (`alpaca_history/live_*.py`, `alpaca_history/corporate_actions.py`), CI label `ga-neutral-reviewed`
  (the allowlist is judged from the BASE, not the same diff). If Task 5 touched shared base code (`MarketDataProviderInterface`),
  re-evaluate: that WOULD be GA-relevant and the minimum must be raised EQUAL to the new version (and the boundary
  checklist in CLAUDE.md applies, never mid-job).
- `python tools/check_package_versions.py --base origin/dev --include-worktree` -> OK.
- Suites, one at a time, each from its own package dir: common, providers, experts, then repo-root `tests/`, then
  `testplatform/backend` (parity only; known pre-existing failures in the parent plan Task 10 are ignored, anything new is ours).
- Code-review subagent on the branch diff; fix.
- Commit `chore: versions for task 11 (APP, ba2_common, ba2_providers[, ba2_experts])`.

## Task 12: Deploy steps

> Owner 2026-10-24: after deploy ALL accounts (dev 1-3, prod 1, options 1 Alpaca accounts) are switched to `alpaca` at once by the operator. New symbols are backfilled on first read (Task 0 items 5-7). (owner decides the values; default `fmp` = no behaviour change)

1. Merge only with no masters mid-job (CLAUDE.md boundary checklist item 5); push; confirm `unsyncable_reason` is silent.
2. **Backups first** on each instance: `python tools/backup_dbs.py` (prod + test DBs, daily task writes to
   `G:\Mon Drive\backup\BA2`), plus a copy of `CACHE_FOLDER/AlpacaHistoryOHLCVProvider/_meta` before the first live write.
3. Pull + restart dev (8080), prod (8081), options (8082) via the Desktop `ba2.bat` (never blanket-kill python; stop by
   port/path only). Start state: no account has the row -> declared default `fmp` -> identical behaviour. Verify in the log:
   no `alpaca_hist` provider constructed; the corporate-actions job logs "not scheduled: no alpaca history account".
4. Per account, the OWNER chooses (settings UI, or SQL on `AccountSetting` key `ohlcv_history_source`): suggested rollout
   dev first (one Alpaca paper account `alpaca`), watch one full session + one corporate-actions run
   (`_meta/corporate_actions_live.json`, `live_state.json`), then prod/options if desired. After each change `/api/reload`
   (drops the instance caches; the new source applies on the next read). State the prod effect up front: indicator/ATR
   history for that account's experts switches to the Alpaca-derived series (differences vs FMP exist: the build compared
   96.5% vs 91.9% coverage; adjusted-price level differences are what the SPY probe saw).
5. Verify on the box: last bar of a latest daily read = last FINISHED session; no `CorporateActionsStale` / `Top-up REFUSED`
   in the log; a backtest started on the same box still reads byte-identical bars for a pinned window (hash of two
   symbol files before/after).
6. Rollback: set the account back to `fmp` + `/api/reload`; the live-written rows stay in the Alpaca cache (harmless, FMP
   never reads that folder).

---

## Risks

1. **Live writes into the cache backtests read.** The top-up appends beyond the build's asof, so a later backtest whose window
   passes 2026-10-12 would read live-written bars (non-reproducible vs a worker that was not topped up); the cache push
   (prune keeps workers identical to local) will also replicate them. Mitigation in the plan: pinned reads never top up,
   byte-stability test, live state separate in `_meta/live_state.json`. The cleaner alternative is a separate live overlay
   folder (open question 1).
2. **Re-base rewrites old rows and breaks option split-basis certification.** Task 12 of the parent plan cross-checks
   `build_manifest.json` events against `split_table.json`; a live re-base adds an event to neither. Option BT on a re-based
   symbol would see "table disagreeing" and REFUSE (loud, but blocking), or worse, read a half-updated basis. Needs the
   option basis resolver to read `live_state.json` events too (not in this task) or a rule that re-based symbols are
   excluded until the next full rebuild.
3. **Alpaca free key**: SIP history is allowed for sessions older than 15 min, and the top-up fetches finished sessions only,
   but the rate limit (200/min) applies to the refresh job; the symbol list is bounded to held/traded symbols.
4. **A split on ex-day, before Alpaca posts the action**: the top-up sees an unexplained seam and raises
   `PendingCorporateAction`; the live read for that symbol fails loudly for up to a day. That is intended (never a mixed
   basis) but is an availability cost on split days for `alpaca` accounts.
5. **Staleness gate coupling**: if the 08:30 job fails, every `alpaca` account's history reads refuse. Intended, loud.
6. **Account-less / global sites stay FMP**, so a user with an `alpaca` account still has FMP in the regime overlay and UI;
   this is by owner decision for the SPY gate, an open question for the overlay.
7. **ATR source**: the default indicator stack is yfinance today. Moving `alpaca` accounts' ATR to Alpaca history is a
   change for those accounts only; `fmp` accounts keep yfinance (no change on deploy).
8. **Stale lock files** after a crash: takeover rule + WARNING; tested.

## Open questions for the owner (ANSWERED 2026-10-24, see Task 0)

1. ANSWERED: append in place, ungated, continuity rule.
2. ANSWERED: alpaca accounts' ATR from Alpaca history; fmp accounts keep yfinance.
3. Default kept: SPY regime read stays FMP.
4. Default kept: TastyTrade/IBKR refuse `alpaca`.
5. Default kept: held/traded symbols of alpaca accounts, 08:30 ET.
6. ANSWERED: extend `build_manifest.json` directly (plus certificate re-stamp and split table, Task 0 item 3).
7. Default kept: symbols not in the build are refused.
8. Default kept: 5-min top-up stays (build completeness rule).
9. ANSWERED: all accounts switch to `alpaca` at once after deploy, by the operator (Task 12).
