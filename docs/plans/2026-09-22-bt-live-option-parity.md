# BT/live option parity: one session clock, exact-session volume, comparable trade records

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Make the backtest and the live path read exactly the same session for every
decision input, and make both paths persist the same option trade record, so a live trade can be
diffed field by field against its backtest counterpart.

**Worktree / branch:** `C:\Users\basti\Documents\dev\BA2-optparity`, `fix/bt-live-option-parity` (from dev `9f9c320f`).

**Origin:** `docs/code-review-2026-09-22.md` P1 + §3, and the 2026-09-22 session finding that the
backtest's market-condition gates lag live by one session. User decisions (2026-09-22):
- BT and live must be **exactly** the same.
- Option volume must come from the exact session: a contract that didn't trade that session has volume 0. Prices from a stale bar stay for now (measure how often a stale row is picked).
- The stage-1 option grid on remote227 is **held**. Job 1 (old rules) gets archived, then the whole campaign restarts from scratch on this build with **28 slots**.
- The live path must store enough to compare with the backtest.

---

## 0. The rule (read this first)

**The backtest's clock (verified 2026-09-22):**
- Bar D's decision sees data through D's close: the expert's price clamp includes D, and the option chain reads D.
- Orders fill on D+1: market orders at the next bar's open (`backtest_account.py:28,2240`), and option orders are staged for the next bar (`:3763`).
- So **backtest bar D ≡ live run during session N(D)**, where `N(D)` is the next regular NYSE session after D, and the data it may read is session D.

**Live:** a decision at instant `t` belongs to session label `L = NY date of t`. The data it may read is `prior_regular_session(L)`, as in the existing `prior_session_v1` policy.

**One rule for everything:** `data_session = prior_regular_session(decision_label)`, where:
- live: `decision_label = NY date of the decision instant`;
- backtest: `decision_label = next_regular_session(bar_date)`, so `data_session == bar_date` on every session bar.

Both labels are produced by functions in `ba2_common.core.market_calendar`. No caller computes a session any other way.

| Input | BT bar D today | Live (session N(D)) today | After this plan |
|---|---|---|---|
| Market-condition row | `prior(D)` = D−1 (**lag**) | D | D both |
| Option chain volume | latest bar ≤ D (stale for no-trade contracts) | none (SDK drops `dailyBar`) | exact bar of D, else 0, both |
| Option chain prices | latest bar ≤ D | live quote | unchanged (user decision), stale-pick rate measured |

The timing-policy string stays `prior_session_v1`, because its meaning ("read the session before the decision's session") is unchanged. What changes is the backtest's **decision label**. The manifest window semantics change (§A3), so manifests are rebuilt and their digests change. That also renames every grid job.

---

## Part A — Market-condition session clock

### Task A1: calendar functions
**Files:** modify `packages/common/ba2_common/core/market_calendar.py`; test `packages/common/tests/test_market_calendar_decision_session.py` (new).

Add, using the existing `_session_table` / `searchsorted` pattern (no new schedule calls):
- `next_regular_session(day: date) -> date`: the first regular session strictly after `day`.
- `backtest_decision_label(bar_date: date) -> date`: `next_regular_session(bar_date)`. The docstring states the §0 equivalence and cites `backtest_account.py:2240,3763`.
- `live_decision_label(moment: datetime) -> date`: `_decision_local_date(moment)`. It refuses naive datetimes.
- `decision_data_session(label: date) -> date`: `prior_regular_session(label)`. This is the single name every caller uses.

Tests (write first, see them fail):
- `backtest_decision_label(Fri 2025-11-28 half-day) == Mon 2025-12-01`
- `backtest_decision_label(Wed 2025-12-24) == Fri 2025-12-26`
- For every session D in 2024 through 2025: `decision_data_session(backtest_decision_label(D)) == D`.
- For every session S in 2024 through 2025, at 09:35 ET and at 15:55 ET: `decision_data_session(live_decision_label(S@t)) == prior_regular_session(S)`.
- Live on Saturday: data session = Friday.
- Naive datetime raises `ValueError`.

Run: `cd packages/common && ..\..\.venv\Scripts\python.exe -m pytest tests/test_market_calendar_decision_session.py -v`

### Task A2: backtest resolver uses the label
**Files:** modify `testplatform/backend/app/services/backtest/market_condition_bt.py` (`BacktestMarketConditionResolver.__call__`, `note_entry`, module docstring); tests in `testplatform/backend/tests/backtest/` (find the existing resolver tests with `grep -rln BacktestMarketConditionResolver testplatform/backend/tests`).

- `bar = account._as_of_date()`.
- A bar on a non-session date keeps today's behaviour: `no_context` plus the warning, triggered as now by `regular_session_close_utc(bar)` raising.
- `session_label = backtest_decision_label(bar)`, `prior_session = decision_data_session(session_label)` (== bar).
- `decision_time = regular_session_close_utc(bar)`. This is the instant the backtest decides; keep it and document it.
- Cache the context on the bar date, not on the label.
- `note_entry` / `attach_entry_states`: the recorded `session` is now the label N(D), the session the fill happens in. Check that `ENTRY_STATE_MAX_GAP_DAYS` binding now reports gap 0 for next-bar fills, and update the tests that pinned gap 1.

Test first: a resolver over a fake account at bar Tue 2025-06-03 yields `prior_session == 2025-06-03` and `session_label == 2025-06-04`. It currently yields 2025-06-02, so the test fails.

### Task A3: coverage and warmup windows
**Files:**
- `packages/common/ba2_common/core/market_condition_reader.py` (~1030-1045, the manifest-vs-run window check);
- `packages/providers/ba2_providers/market_conditions/warmup.py` (~615-625, `plan()` row range; `_decision_sessions`);
- `tools/strategy_research/market_conditions.py` (research gates: route through the same resolver or the same functions).

Changes:
- **Reader check:** rows required for a backtest window `[first, last]` = `decision_data_session(backtest_decision_label(d))` for the first and last session bars, i.e. the sessions in `[first, last]`. The "built for decision dates" message must talk about backtest bars.
- **Warmup:** build rows `[prior_regular_session(first_decision), last_decision]`. That is a superset covering both the live rule and the backtest rule, one extra row. Record it in the manifest's window fields. If the manifest schema has a field for the covered row range, use it; otherwise add `rows_start` / `rows_end`, never overloading `window_*`.
- **Consistency:** grep for any other `prior_regular_session(` caller that assumes a backtest bar is the decision label, and route it through the new functions. Known callers: `market_condition_live.py:345` (live, correct as is), `market_condition_reader.py:1038`, `warmup.py:621`, `market_condition_bt.py:118`.

Tests: the existing coverage/warmup tests must be updated deliberately. Every changed expectation is quoted in the commit message with the reason.

### Task A4: BT/live market-condition parity test
**File:** `testplatform/backend/tests/backtest/test_market_condition_bt_live_session_parity.py` (new; add it to the CI PARITY GATE step).

For sessions S across 2024 through 2025, including the day after a holiday, the day after a half-day and a Monday, it builds:
- the live `DecisionState` at S 09:35 ET (resolver with a `DictMarketConditionReader`, capture off);
- the backtest resolver at bar `prior_regular_session(S)`.

It asserts that `context().prior_session` is equal, and that `reader.observe(symbol, prior_session)` returns the same row object. This is the test that would have caught the lag.

---

## Part B — Option volume from the exact session

### Task B1: shared session-volume rule
**Files:** create `packages/common/ba2_common/core/option_session.py`; test `packages/common/tests/test_option_session.py`.

```python
def session_volume(bars: Iterable[Tuple[date, Optional[int]]], data_session: date) -> int:
    """Volume the contract traded IN ``data_session``: the bar dated exactly that session, else 0.

    A bar that exists but carries no volume is refused (ValueError): every source we read
    (ThetaData EOD, Alpaca option bars) publishes volume on every bar, so a missing one is a
    parse bug, not a zero. A bar dated after data_session is ignored (never lookahead).
    """
```
Tests:
- exact match → its volume;
- only an older bar → 0;
- only a newer bar → 0;
- two bars (prev + today) → picks the one equal to `data_session`;
- a matching bar with `None` volume → `ValueError`.

### Task B2: backtest chains use it
**Files:**
- `testplatform/backend/app/services/backtest/parquet_options_provider.py` (`get_chain` ~1159, `_Underlying.contract` ~925);
- `testplatform/backend/app/services/backtest/options_provider.py` (sqlite store `get_chain`, `_bar_volume`);
- `backtest_account.get_option_chain` (~3539) computes `data_session = decision_data_session(backtest_decision_label(as_of))` and passes it down (`get_chain(..., data_session=...)`).

Details:
- **Volume:** `exact_row(ci, data_session.toordinal())` → volume, else 0, via `session_volume` semantics. The parquet row's NaN volume on a present bar stays the documented "known zero" (ThetaData convention). Keep that rule in the provider, and have `session_volume` receive `0` for it rather than `None`.
- **Prices:** still from `latest_row_on_or_before(as_of)` (user decision).
- **Stale-pick counter:** count how often the latest row is older than `data_session` and expose it in the run's option telemetry. That is the measurement the user asked for.
- **Tests (write first):**
  - a contract with bars on D−3 and none on D: volume 0 (today it would be the D−3 volume);
  - a contract with a bar on D: that volume.

### Task B3: live Alpaca chain and quote read the raw snapshot
**File:** `ba2_trade_platform/modules/accounts/AlpacaAccount.py` (`_get_option_data_client` ~5928, `get_option_chain` ~6066, `get_option_quote` ~6193); tests in `tests/test_alpaca_option_snapshot_parse.py` (new).

**Why raw:** alpaca-py 0.43.4 and 0.44.0 `OptionsSnapshot` keep only quote/trade/IV/greeks and **drop `dailyBar` / `prevDailyBar` / `minuteBar`**. Verified by constructing one from a raw dict. Alpaca's REST schema has `dailyBar.v` (required).

Steps:
- Add `_get_option_data_client_raw()`: a second cached client with `raw_data=True`. Keep the typed client for anything else that uses it.
- Add a pure module-level `parse_alpaca_option_snapshot(raw: dict) -> dict` returning:
  - `bid`, `ask`, `bid_size`, `ask_size`, `quote_time`, `last`, `last_time`;
  - `iv`, `delta`, `gamma`, `theta`, `vega`, `rho`;
  - `bars: [(ny_session_date, volume)]` from `dailyBar` and `prevDailyBar`. The bar `t` is an RFC-3339 timestamp; convert to the NY date. Pin it with a fixture where `t` is `...T04:00:00Z` and one where it is `...T05:00:00Z`.
- In `get_option_chain`, call `get_option_chain` on the raw client (with `OptionChainRequest` unchanged) and parse. Set `volume = session_volume(parsed["bars"], decision_data_session(live_decision_label(replay_now())))`, using the replay-aware clock `ba2_common.core.replay.clock.replay_now`, as `market_condition_live._replay_now` does.
- Do the same in `get_option_quote`. `OptionQuote` gets `volume` and `rho` (Task B4).
- Pagination: check that the raw client's `get_option_chain` still pages (`_return_paginated_result`). A test with a two-page fake is required.
- **Tests (write first):**
  - a raw fixture copied from Alpaca's documented example → an `OptionContract` with volume from the matching bar;
  - a morning fixture (dailyBar dated yesterday, prevDailyBar the day before) → yesterday's volume;
  - an after-close fixture (dailyBar dated today) → at a decision instant after close the data session is still yesterday (`prior_session_v1`), so it gets **prevDailyBar's** volume.

  The last case is exactly the policy; call it out in the test name.

### Task B4: rho, and volume on quotes
**Files:** `packages/common/ba2_common/core/option_types.py` (`OptionContract.rho`, `OptionQuote.rho`, `OptionQuote.volume`); the backtest greeks (`option_greeks.py` Black-Scholes, plus `greeks_tuple` in `parquet_options_provider.py`), which must compute rho.

The backtest rho must use the same rate the other BS greeks use, `risk_free_rate`. Test it against a textbook BS value.

### Task B5: liquidity parity test (the P1 regression)
**File:** `testplatform/backend/tests/backtest/test_option_liquidity_bt_live_parity.py` (new; add it to the CI PARITY GATE).
- Build an option action config exactly as the grid does (`ba2test_launcher._apply_option_min_volume`), so `option_min_volume=25`.
- Feed `check_liquidity_data_available` a chain built by `AlpacaAccount.get_option_chain` from a raw fixture (fake raw client). It must **not** raise, and the selector must pick the same contract as it picks on the equivalent backtest parquet chain for the same session.
- Also: the same contract's volume from the backtest chain (bar D) equals the live chain's volume at session N(D) 09:35 ET.

---

## Part C — One option trade record for both paths

The goal is that a live option position and its backtest twin produce records with **identical
keys and identical computation**, differing only in source values. Schema version `option_trade_record_v1`.

### Task C1: shared snapshot builders
**File:** create `packages/common/ba2_common/core/option_trade_record.py`; test `packages/common/tests/test_option_trade_record.py`.

```python
OPTION_TRADE_RECORD_VERSION = "option_trade_record_v1"
LEG_SNAPSHOT_FIELDS = (
    "contract_symbol", "right", "strike", "expiry", "dte", "data_session", "spot",
    "moneyness_pct", "bid", "ask", "mid", "spread_pct", "last", "iv",
    "delta", "gamma", "theta", "vega", "rho", "open_interest", "volume",
    "greeks_source", "quote_time",
)
def leg_snapshot(contract: OptionContract, *, spot: float, data_session: date,
                 decision_date: date, greeks_source: str, quote_time: Optional[datetime]) -> dict
def structure_snapshot(legs: Sequence[dict], *, strategy: str, quantity: int, multiplier: int,
                       net_price: float, max_loss: Optional[float], max_profit: Optional[float],
                       breakevens: Sequence[float]) -> dict
```
Rules:
- **No defaults for live data.** `spot is None` raises. Unknown greeks stay `None` (a vendor gap), never 0.
- `dte` is computed from `decision_date` (NY), `moneyness_pct = (strike/spot − 1)·100` signed by right.
- `greeks_source` is `"broker"` (Alpaca) or `"bs_from_close"` (backtest), so the two are never silently compared as if they came from one source.
- JSON-serialisable (dates → ISO).

Test: every key in `LEG_SNAPSHOT_FIELDS` is present for a full contract and for a sparse one.

### Task C2: entry snapshot written by the shared submit path
**Files:** `packages/common/ba2_common/core/option_types.py` (`OptionLeg.quote: Optional[OptionContract] = field(default=None, compare=False, repr=False)`, not persisted); every builder in `TradeActions.py` that turns a chosen `OptionContract` into an `OptionLeg` (find them with `grep -n "OptionLeg(" packages/common/ba2_common/core/TradeActions.py`) sets `quote=`; `_submit_option_order` (~2834) writes `data["entry_record"] = {"version":..., "legs":[...], "structure":{...}}` and the same object into `entry_facts`.

- **Spot** comes from the account's own price source (`account.get_instrument_current_price(underlying)`, the same call the builders already use for strike selection), so it's the same code on both sides.
- **The data session** comes from the new calendar functions:
  - live: `live_decision_label(replay_now())`;
  - backtest: `backtest_decision_label(account._as_of_date())`.

  Add one method on the account interface, `decision_label()`, implemented by `AlpacaAccount` / `BacktestAccount`, and call only that.
- **Test:** one shared test, parametrised over a fake live account and a `BacktestAccount` with the same fixture chain, asserts the two `entry_record`s are equal except `greeks_source` / `quote_time`.

**Decision (2026-09-23, controller): a record failure never blocks an entry, except a missing spot.**
- A missing or unusable spot refuses the entry, and so does a split-basis refusal.
- Any other failure to build the record is logged at ERROR, the record is stored as `{"version", "error"}`, and the entry proceeds.
- This includes an account that doesn't declare `OPTION_GREEKS_SOURCE`, which deviates from C2's "raises". It's safe because a test walks every concrete production options account and checks that it declares a source.

### Task C3: real exit trigger and exit snapshot
**Files:** `packages/common/ba2_common/core/types.py` (new `OptionCloseReason` enum: `take_profit`, `stop_loss`, `time_exit`, `rule_exit`, `roll`, `expired_otm`, `assigned`, `exercised`, `forced_liquidation`, `manual`); `CloseOptionAction.execute` (`TradeActions.py` ~5484) writes `data["exit_record"] = {"version", "trigger": <OptionCloseReason>, "rule_id", "legs":[leg_snapshot...]}` on the close order; the live expiry/assignment handling in `AlpacaAccount` (~6647-6664) and the backtest expiry close (`backtest_account.py` ~3901, ~4362) write the same `trigger`; `Transaction.close_reason` takes the enum's value in both paths.

- Delete the price-proximity guess `_exit_reason` (`backtest_account.py:3460`) for option rows. Keep equities as they are.
- **Test:**
  - a single-leg stop-loss close is recorded `stop_loss` (today it's `take_profit`);
  - expiry → `expired_otm`, assignment → `assigned`, in both the live fake and the backtest.

**Implementation notes (2026-09-23):**
- **`rule_id` is a per-run DB id in the backtest.** It is the firing `EventAction`'s row id in that run's in-memory store, so the same rule has different ids in different runs (and differs from the live id). `rule_name` is the seeded name (`…-rule-N`), not the launcher id (`opt_tp`). Compare runs, and backtest against live, by **`trigger`**, never by `rule_id`.
- **A split-basis refusal raised after a close was submitted** (the exit record's spot read, `failure_modes.is_never_absorbed`) propagates as on the entry path, so that action's `TradeActionResult` is not persisted even though the close order is. This can only happen in the backtest (live spot has no basis conversion), and the run aborts on it anyway.
- **Enum additions beyond the list above:** `dte_exit`, and `tested` / `circuit_breaker` / `cover_lost` for the live lifecycle pass. An ITM long at backtest expiry is `exercised` (the live OPEXC event), although the backtest settles it at premium/intrinsic rather than delivering shares.
- **A structure's settlement `close_reason`** is the most consequential of its legs' settlements (`assigned` > `exercised` > `expired_otm`, `option_trade_record.settlement_close_reason`), in both the backtest and the live reconciler, so it does not depend on leg or report order.
- **Records only on persisted runs** (C4 follow-up): the run config states `option_trade_records` (required for options runs). A GA fitness trial (`False`) builds no entry record and only the close trigger on exits (`exit_record_lean`); rows keep the pre-record keys. Measured record overhead on a fitness trial: 0.07 % of trial time.

### Task C4: backtest trades JSON carries the record
**Files:** `testplatform/backend/app/services/backtest/backtest_account.py` (`get_round_trip_trades` ~3407-3454, which also removes the duplicate `"multiplier"` key), `results.py` (`_trade_row` ~365-427).

- Each option leg row gets `entry_record`, `exit_record`, `option_strategy`, `recommendation_confidence`.
- **Only once per structure:** the structure-level parts go on the first leg, matching `attach_entry_states` (blob size).
- Measure the blob size change on one stage-1 genome and put the number in the commit message.

### Task C5: live record stored and a diff tool
**Files:** live already persists `TradingOrder.data`. Add nothing new to the DB schema unless the fields don't reach it. Create `tools/compare_option_trade_records.py`:
- **Inputs:** a live DB (`?mode=ro`) and an account id, a backtest id (testplatform DB, read-only), and a window.
- **Matching:** pairs structures by (underlying, strategy, **decision session**). The backtest stamps a next-bar-open fill with the **decision bar D** (`_fill_dates[order.id] = as_of` in `_apply_fill` / `_apply_option_fill`), while live records the real fill time in session N(D). So match live's N(D) against the backtest's D using `backtest_decision_label`. Don't compare raw fill dates.
- **Output:** a field-by-field diff (entry session, contracts, DTE, strikes, spot, moneyness, IV, greeks with their source, volume, fill vs mid, exit trigger, exit session, P&L), plus a summary of unmatched structures on either side.

Test the pairing on synthetic records.

**Implementation notes (2026-09-23):**
- **Pairing key = the entry record's leg `data_session`**, not a label conversion. Both paths write it through `decision_data_session(account.decision_label())`: live N(D) reads D, backtest bar D reads D. So it is equal by construction (pinned by `test_option_entry_record_parity`), and neither side needs calendar arithmetic. A structure with no usable record falls back to the fill: live `prior_regular_session(created_at)`, where a naive `created_at` is read as UTC; backtest `entry_time` date = D. The report tags that fallback as `key_source="fill"`.
- The pure code is in `packages/common/ba2_common/core/option_trade_compare.py`. The CLI is `tools/compare_option_trade_records.py`, which opens both DBs with `mode=ro` and `query_only`. Tests:
  - `packages/common/tests/test_option_trade_compare.py`;
  - `tests/test_compare_option_trade_records_tool.py`, a smoke test on temp DBs;
  - `testplatform/backend/tests/backtest/test_option_trade_compare_reads_engine_rows.py`, which runs on real engine rows and on the real live and backtest submit records.
- **P&L is compared gross**, because live commissions aren't on the order rows. The backtest's commission-inclusive `pnl` stays in the pair metadata.
- **Greeks and IV rows carry both sources and a `source_mismatch` flag.** The summary lists them under their own heading.
- **No DB schema change was needed.** Read-only checks on 2026-09-23 of prod, dev and opt (`tradingorder`, `transaction`, `accountdefinition`) and of the testplatform `backtests` table found every column the tool reads. No option orders or option backtests exist yet, so this validated the schema only.

### Deferred (recorded, not in this plan)
Daily per-leg mark snapshots over each position's life (review §3 item 5). They are needed for path comparison, not for entry/exit parity. Their schema should reuse `LEG_SNAPSHOT_FIELDS`.

---

## Part E — Split basis: option strikes as-traded vs split-adjusted spot (BLOCKS the grid restart)

**Finding (verified 2026-09-22).** The ThetaData option store keeps strikes and premiums **as traded**. The option path's spot is the run's FMP close (`options_store.price_source_spot` → `AsOfPriceSource.close_asof`), which FMP **back-adjusts** for every split up to the cache's fetch date. Nothing converts between the two.
- NFLX on 2024-05-01: the put-call-parity spot is $553, the FMP close is $55.17.
- 31 of the 97 stage-1 symbols are affected, on 16.4% of sampled symbol-days. Per-symbol detail: `scratchpad/spreads/split_mismatch_by_sym_year.csv`.

Live is unaffected, since Alpaca's spot and strikes are both as traded. So this is a BT/live parity break, and it touches:
- strike selection by % OTM or delta;
- Black-Scholes greeks and the arbitrage guard;
- intrinsic marks and expiry settlement (`_leg_intrinsic`);
- assignment, exercise and coverage for O_CC / O_PP / O_WHEEL / O_CSP, where the equity book holds adjusted shares.

**Rule.** The option path always works in the **as-traded** basis, in both live and backtest. The backtest's equity book stays split-adjusted, which is how every equity backtest works today. Conversion happens only at the boundary:
- `as_traded_factor(symbol, day)` = the product of split ratios whose ex-date is after `day` and on or before the FMP cache's adjustment basis date.
- The basis date is the per-symbol full-fetch marker `fetched_on` in `split_basis.py`. Splits after that date aren't in the cache, and adjusting for them would double-count.

### Task E1: shared split-factor function
**File:** `packages/common/ba2_common/core/split_basis.py` (or a new `option_basis.py` next to it). It is a pure function over the split calendar (`CalendarSplit` events) and the basis date.
- A missing calendar or basis marker **refuses** (raises). It never falls back to a factor of 1.
- **Tests:** NFLX 2024-05-01 → 10; NVDA 2020 → 40 (the 2021 4:1 and 2024 10:1); a date after the last split → 1; a split after `fetched_on` is excluded; a reverse split (GE-style ratio < 1).

### Task E2: one account method for the option spot
**Interface:** add `get_option_underlying_price(symbol)` to `OptionsAccountInterface`.
- **Live** (`AlpacaAccount`, TastyTrade): delegates to `get_instrument_current_price` unchanged, since live prices are already as traded.
- **Backtest** (`BacktestAccount`): adjusted close × `as_traded_factor(symbol, as_of)`.

**Callers:**
- every option builder and selector call in `TradeActions.py` that reads spot for strike, delta or moneyness (`grep -n "get_instrument_current_price" packages/common/ba2_common/core/TradeActions.py`, then keep only the option paths);
- the option lifecycle service;
- the parquet provider's `spot_source`: `options_store.price_source_spot` multiplies by the factor;
- the backtest's intrinsic and expiry settlement (`_leg_intrinsic`, the expiry close ~4362, assignment ~3901).

**Test:** an NFLX fixture chain on 2024-05-01 selects the ~5%-OTM strike (~$580), not ~$58.

### Task E3: stock ↔ option boundary in the backtest
**Assignment/exercise:** delivers `100 × contracts` shares at the as-traded strike. It books into the adjusted equity book as `shares × factor` at `strike / factor`, which leaves the dollar value unchanged.

**Coverage checks** (covered call, protective put, wheel):
- contracts coverable = `floor(held_adjusted_shares / factor / 100)`;
- the CSP reserve uses the as-traded strike × 100 (dollars, already correct).

**Test:** a covered call on NFLX in 2024 with 1,000 adjusted shares (= 100 real) allows 1 contract, not 10. Assignment round-trips the dollar value exactly.

### Task E4: guard
A per-bar sanity check in the backtest option chain: compare the as-traded spot with the chain's put-call-parity spot from the nearest expiry (the method used in the measurement). If they diverge by more than 5%, the backtest **refuses loudly** with the symbol, date and ratio. A future basis bug then can't silently mis-price a grid again.

Measure its cost; if it's material, run it only on the first bar of each symbol and at each split ex-date.

## Part G — Repair the basis data before the restart (user decision 2026-09-22: repair, don't exclude)

**Evidence:** a full sweep of all 97 symbols, every session 2020–2025 (138k symbol-sessions). CSVs are in `scratchpad/basis_repair/`: `sessions_ratio_all.csv`, `defect_spans.csv`, `symbol_summary.csv`, `fmp_jumps_and_calendar.csv`, `option_basis_overrides.proposed.json`.

**Defects found:**

| Symbol | Ratio (parity / converted spot) | Root cause | Repair |
|---|---|---|---|
| CRWD | 0.2501 over the whole window | The FMP parquet mixes bases: as traded until 2026-06-15, ÷4 from 2026-06-16. The real ex-date is 07-02. `check_split_basis` looks only at 07-02, so it says "consistent". | Force a full refetch; the 07-24 FMP payload is fully adjusted. Also have the split check detect this case (G1). |
| HON | 1.0615 until 2025-10-29 | The Solstice spin-off on **2025-10-30** is in FMP prices but not in the calendar. (The 2026-06-28 event *is* correctly applied.) | Override: add adjustment 1.0614 on 2025-10-30 |
| NVS | 1.057 until 2023-10-03 | The Sandoz spin-off on 2023-10-04 is in prices, not in the calendar | Override: add 1.0575 on 2023-10-04 |
| SCCO | 1.04, 2020-01 → 2024-07, fading out by 2025-08 | Quarterly stock dividends 2024-08 → 2025-08 are in prices, not in the calendar. It stays under 5%, so the guard never sees it. | Overrides for 5 quarterly adjustments. Pin the exact ex-dates with one FMP dividends call. |
| META | 0.044 until 2022-06-08 | Before the rename, ThetaData's "META" chain is the Roundhill ETF. The partitions for 2022-08-19 and 2022-09-16 expiries **mix ETF and Meta rows**. | Delete the META tree and backfill 2020-01 → 2022-06-08 from root `FB`, stored as META (G3) |
| MUFG, SMFG, SAN, AZN | single-session false refusals | Far-OTM single pairs on low-priced ADRs; dips before ex-dividend dates | Moneyness gate plus 5-session window (G2) |

### Task G1: split-check jump detection and a versioned override module
- **`split_basis.check_split_basis`:** also flag any one-day FMP move beyond ×1.4 (either direction) within ±30 days of a calendar split whose date doesn't match. That verdict requires a refetch; CRWD is the regression test. APP, ARM and NFLX 2022-04-20 have real one-day moves this big, but none sits near a split, and a test confirms they aren't flagged.
- **New `ba2_common/core/split_basis_overrides.py`:**
  - Holds `OVERRIDES_VERSION` and `BASIS_OVERRIDES`.
  - Entry format: `{symbol, event_date, ratio, kind: add_price_adjustment | exclude_calendar_event, anchors: [{date, fmp_close}], evidence}`.
  - Entries: HON, NVS and SCCO as computed in the scratch JSON. Anchors on 2020-01-02: HON 178.71, NVS 89.85, SCCO 40.18.
- **How the resolver uses them:**
  - `resolve_symbol_split_basis` applies the overrides **after** `check_split_basis`.
  - If any anchor's FMP close is off by more than 0.1%, it **refuses**. FMP's adjustment set changes between fetches, and a refetch must force a re-measure, never a silent double count.
  - `OVERRIDES_VERSION` and the entries go into `SymbolSplitBasis.identity()`, so digests and memo keys move.
- **Tests:**
  - HON, NVS and SCCO spans come back to about 1.00.
  - A changed anchor refuses.
  - `exclude_calendar_event` works; no entry uses it today.

### Task G2: guard moneyness gate
In `option_basis_guard.parity_spot`, a session counts as unevaluable (not a pass, not a refusal) when the chosen strike is more than 10% from its own parity spot. This doesn't depend on the basis. The window stays at 5.

**Test:** emulate the guard on the sweep data for the non-defect symbols; there must be zero false refusals. Every CRWD, HON, NVS and META defect session is still refused before its repair.

### Task G3: META ← FB root alias in the ThetaData provider
**Code:** in `packages/providers/ba2_providers/options/thetadata.py`, add `ROOT_HISTORY = {"META": ((date(2022,6,9), "FB"),)}`. Split each request window at 2022-06-09 and request root `FB` before that date. Store the rows under META, with the OCC symbol built through `_occ_symbol("META", …)` so the stored contract symbols stay consistent.

**Tests:** fake client only; no network in tests.

### Task G4: data operations (network; run after G1–G3 land in the worktree)
**Local**, cache `C:\Users\basti\Documents\ba2\common\cache`:
1. Run `FMPOHLCVProvider().force_full_refetch('CRWD','1d')`. Then verify that parity ÷ (FMP × 4) ≈ 1.00.
2. Run one ThetaData probe, `option_list_expirations("FB")`. If FB isn't served, stop and report: the fallback is starting META at 2022-06-09, which needs the user's decision.
3. Delete `ThetaDataOptionsProvider/META/` entirely and run `python tools/warm_options_history.py --provider thetadata --wide --symbols META --start 2020-01-01`. Estimate: about 4.5M rows, 30–45 minutes.
4. Optionally, make one FMP dividends call to pin SCCO's exact ex-dates, and update the overrides.
5. Re-run the sweep. **Pass condition:** 0 defect spans and 0 guard refusals across all 97 symbols, 2020–2025.

**Local result (2026-09-23):**
- **CRWD:** refetched (backup in `scratchpad/g4/backup/`); parity ÷ (FMP × factor) is 1.000–1.003.
- **META:** rebuilt from the `FB` root, 5.19M rows, 2020-01-02 → 2026-09-11. It took 1h42m at concurrency 1. The old ETF-contaminated tree is backed up to `scratchpad/g4/backup/META_20260923-032038`.
- **Guard replay:** 0 refusals over 39,921 checks, CRWD and META included.
- **Sweep:** 27 residual spans, accepted by the controller as **not** basis defects:
  - 16 are put-call parity reading low before ex-dividend dates (BHP, RIO, UBS, NVS);
  - 7 are single cheap strikes (MUFG, SAN, SMFG);
  - 5 are AZN 2022 blips at about 1.03, unexplained, all under tolerance.

  The pass condition is therefore **0 guard refusals**. Refining the span check to be dividend-aware is a follow-up, not a blocker.
- **SCCO:** the overrides were corrected to the measured ex-dates, and a sixth stock dividend (2025-11-12) was added.

**remote227** (during D3, before the manifests are rebuilt):
1. Refetch CRWD with the same call (worker venv, with the `DB_FILE`/`PYTHONPATH` exports from the runbook; `chmod g+ws` on the FMP directory).
2. Copy the new META tree over, or run the backfill there with the key.
3. Check that the HON, NVS and SCCO anchors match that box's FMP files.
4. Re-warm both market-condition profiles, then run `tools/build_shared_arrays.py` twice.

**Why this matters beyond options:** CRWD's mixed-basis file also shows a fake −75% day in **equity** backtests. The refetch removes it. The equity no-impact gate (D1b) compares code, so it is unaffected. Record in the grid guide that equity results on CRWD from before the refetch are affected.

## Part F — Option fill spread calibrated to measured spreads

**Measurement** (2026-09-22): ThetaData end-of-day NBBO, 97 symbols, 2020–2025, 28.7M valid quotes; tables in `scratchpad/spreads/`.
- Today's model is `full = max($0.02, 5% × premium)`, doubled when the fill day's volume is under 100, and applied at the next day's open.
- It is too cheap under $2 (0.3–0.7× the median) and too expensive above $10 (1.4–3×). Overall it charges about 2× reality in dollars.
- Real spreads scale roughly with premium^0.6.
- The thin-contract doubling reads the **fill day's** full-day volume, which is a small lookahead.

### Task F1: charge the as-of real spread (Option A)
**Files:** `parquet_options_provider.py` `bar_dict` (~904-917) carries `bid` / `ask`. In `backtest_account._option_half_spread` (~2511-2528), `half = (ask − bid)/2` comes from the **decision-day (as-of) bar's** closing NBBO when it has a valid quote. This is causal. Validated 2024-25 against the next day's real spread: median ratio 1.00, total ratio 0.99, typical error 0.41 vs 0.83 for today's model.

Live crosses its real quote, so this is also the parity-correct choice.

### Task F2: calibrated fallback model (Option B)
**File:** new `packages/common/ba2_common/core/option_spread_model.py`, a pure function used when the as-of bar has no valid quote:

`full = max(0.01, 0.2301 × premium^0.6035 × max(volume,1)^-0.1357)` using the **as-of** bar's volume.

Fitted on 2020–23, tested on 2024–25: typical error 0.696, median ratio 1.09. It takes no spot input, so it isn't affected by the split basis.

- Retire the fill-day thin doubling.
- Keep `--option-spread-pct` / `--option-spread-min-tick` only as explicit overrides.
- Record the model version in the run config and the job identity (this renames jobs, which is intended).

**Tests:** the as-of quote is used when present; the fallback is used otherwise; the fill-day volume is never read. Fixture numbers are reproduced from `fit_validation.csv`.

**Caveat, to state in the grid guide:** the NBBO is at the close and fills happen at the open, where spreads are typically wider. This remains a mildly optimistic calibration.

### Follow-ups (recorded 2026-09-23, not in this plan)
- **Fill-day volume in the liquidity cap (lookahead).** `BacktestAccount._volume_cap_reject_reason` still reads the fill day's full-session volume (TODO in the code). The causal alternative is the decision bar's volume. Switching changes which fills happen, so it needs its own measured change and golden re-pin.
- **`entry_cross` goes beyond the touch on quoted stores.** The concession was designed for close-proxy chains (`bid == ask == close`). On ThetaData the builders already quote at the real touch (buy at ask, sell at bid), so a positive `entry_cross` concedes a further fraction of the spread past the touch. Measure how often this happens and how much it costs, then decide: gate the concession off for quoted contracts, or measure it from the mid.
- **Close-vs-open spread optimism.** Fills are charged the decision bar's closing NBBO but happen at the next open, where spreads are typically wider. Quantify it (intraday or open quotes on a sample) and decide whether to add a widening factor.

## Part D — Versioning, CI, deploy

### Task D1: CI and versions
- Add the three new parity tests (A4, B5, C2's shared test) to the PARITY GATE step, and add `--junitxml=junit-parity.xml` there, fed to the annotator (review P10).
- Bump `testplatform/version.py` (packages + testplatform changed) **and** `ba2_trade_platform/version.py` (AlpacaAccount changed). Compute the next numbers from `git show origin/dev:<file>`, not the local checkout.

### Task D1b: equity no-impact gate (user requirement, blocking)
**User requirement (2026-09-22):** nothing in this plan may change any existing non-option backtest. Every change must stay on the option path: option chains, quotes, greeks, fills, option trade rows. The only other exception is market-condition runs, which on the grid are options-only.

**Gate:**
- Pick at least 6 finished equity backtests from the testplatform DB, covering FactorRanker, Senate, FMPRating, DeterministicScorer equity and a classic-RM expert, each with a ruleset.
- Re-run each from its stored config on the branch base (dev `9f9c320f`) and on the branch head, with the same seed and the same caches.
- The results must be **identical**: equity curve, trades and metrics, compared with the same comparison `tools/backtest_parity.py` uses.
- Also check that `test_parity_golden.py` and `test_market_condition_all_off_matches_baseline.py` pass unchanged.

Any difference blocks the merge until it is explained and removed.

### Task D1c: performance gate (user requirement 2026-09-23, blocking)
**User requirement:** none of this work may make backtests slower.

**Setup:** measure the branch base (dev `9f9c320f`) against the branch head, on the same machine, with the same warm caches, the same seeds, and nothing else running. Take at least 3 repeats per case and report the median, with the spread.

**Cases:**
- **Equity:**
  - one FactorRanker trial and one Senate trial;
  - one classic-RM expert trial.
- **Options, on the real ThetaData parquet store, 2 years, 10–15 symbols including split symbols (NFLX, NVDA):**
  - DeterministicScorer O_LC (the stage-1 hot case);
  - O_IC (multi-leg);
  - O_CC (the stock/option boundary).

**Measure per case:**
- trial wall time;
- peak RSS of the trial process (size the structures directly where possible: RSS deltas are noisy);
- for options, the cost of each new component on its own: guard, session volume, as-of spread read, entry record, split-basis factor.

**Thresholds:**
- **Equity:** within measurement noise (target 0; flag anything over +1%).
- **Options:** at most +3% wall time and at most +5% peak RSS per trial against the base.
- If a component costs more, optimise it before merging (for example, memoise the entry record's payoff per structure, or sample the guard on the first bar per symbol plus each split ex-date). Don't relax the threshold without the user's decision.

### Task D2: full verification in the worktree
Run each separately and never concurrently (worktree quirks memory):
- `packages/common` tests;
- `packages/providers` tests;
- `testplatform/backend` tests (from its own dir, its `pytest.ini`);
- root `tests/`.

Compare against the dev baseline failure list, and report any new failure explicitly.

### Task D3: remote227 relaunch (only after merge + push, user go-ahead)
1. **Archive job 1:**
   - export its `strategy_optimizations` row and `task_queue` checkpoint `ckpt-ebe7cb6cebea69f2512595f7` to `/home/debian/ba2-grid/prereset_backup/job1_oldclock_<ts>.json`;
   - add label `ArchivedOldClock`;
   - rename it with suffix `-archived-oldclock` through the platform's own update path, not raw SQL on blob columns;
   - keep `stage1_lab2_20260922T060544Z.log`.
2. **Code:** back up local edits, then fetch GitHub dev and `git merge --ff-only`. Confirm the version and that `backtest_decision_label` / `session_volume` / `option_trade_record` exist.
3. **Manifests:** rebuild both profiles with the warmup `build` for the stage-1 window and universe. Record the new digests.
4. **Relaunch** with the new `MARKET_CONDITION_MANIFEST` digests, `--parallel 28`, and the same labels/suffix. Before launching, check the fleet worker (1369/653399) is idle; it holds ~66 GB in 6 idle pool processes.
5. **Verify:**
   - job 1 starts at generation 0 under a **new** name (the digest covers the manifest);
   - the old name is not resumed;
   - the log shows 28 slots and memory stays under ~80%.

### Task D4: live rollout (user decides timing)
APP bump → dev restart first. After a scheduled option analysis, check `entry_record` in a real order row, then prod. Prod restarts are already owed (memory: open tasks 2026-09-18).
