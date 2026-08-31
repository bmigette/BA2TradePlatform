# BA2 Options Backtest — Findings Log (babatest grid)

Server: `debian@141.94.199.227` · Worktree: `/tmp/ba2-gridtest-wt` · Isolated `BA2_HOME=/tmp/ba2-gridtest-home`
No cron — manual resume via skill `ba2-options-backtest`.

---

## 2026-08-27 (evening) — wheel-engine merge received, probe resumed

### Commits received on origin/dev (merge `00a108d2`, 21:07 UTC)
- `00a108d2` Merge branch 'wheel-engine' into dev
- `1036fdef` fix(backtest): the wheel is representable — hold assigned stock, opt-in (plan Task 10 + 14 review findings)
- `73456343` feat(grid): O_WHEEL builds and runs — wire hold_assigned_stock per strategy
- `d2e6db19` feat(backtest): hold_assigned_stock — the wheel can hold its assigned shares
- `f57cac36` docs+test(wheel): pin the expert_id link, record Task 10's answer in the plan
- `3654c9a7` docs: ETF option universe investigation + the 1DTE variant that depends on it

Closes **H2 from reports/option-grid-prep-review-2026-08-27.md**: engine used to liquidate assigned
shares at next-bar open while the manage pass (same bar) wrote a covered call against them → every
O_WHEEL position was secretly a naked short call. Fixed via `hold_assigned_stock` run setting,
DEFAULT OFF (historical runs proven bit-identical).

### Verified ✅
- Worktree at merge head `00a108d2`; isolated BA2_HOME resolves full cache chain
  (`BA2_HOME → COMMON_DIR → CACHE_FOLDER → OPTIONS_CACHE_DB`), 19,484,995 option bars via `?mode=ro`.
- `option_chain`: underlying, as_of, occ_symbol, option_type, strike, expiry, bid, ask, last, iv,
  delta, gamma, theta, vega, open_interest, volume. `option_bar` has greeks too. NO bid/ask in option_bar.
- `testplatform/backend/tests/backtest/test_wheel_assignment.py`: **19/19 passed** on merged code.
  (Top-level `tests/` conftest needs langchain_core — not in test venv; use `--confcutdir=testplatform/backend/tests`.)
- `_hold_assigned_stock()` handles GROUP keys (OS1-OS4/OS_ALL expand); `_HOLDS_ASSIGNED_STOCK = {"O_WHEEL"}`
  wired at `optimize` (:3700) and `optimize-batch` (:4018).
- Probe script patched: was missing `hold_assigned_stock` in its hand-built account_settings (would
  have tested the naked-call defect). Now calls `m._hold_assigned_stock(kind)` like `_cmd_optimize`.
- Scratch DB seeding: probe needs `FMP_API_KEY` — key lives in shared grid DB
  `/opt/ba2worker/Documents/ba2/test/dl_forecasting.db` table `appsetting`; seed our scratch DB from
  it (read-only) + pass FMP_API_KEY in env.

---

## 2026-08-28 (night) — why single probes had 0 trades: three findings

Probe: O_CSP/O_WHEEL, AAPL, FMPRating expert, gates-off, seed 42, 2024-06-01 → 2024-12-31.

### F1 — HIGH: expired option DAY orders lock the symbol for the rest of the run
Reproducible at $100k: ONE order placed 2024-06-03 (`SELL AAPL240628P00175000 limit 0.40`) expires
unfilled on 2024-06-04 (correct — DAY TIF). But its WAITING transaction is never released during the
run: the engine's live-parity dup-gate (`_has_open_or_waiting_position`, daily_engine.py:1025) then
skips EVERY subsequent entry for that (expert, symbol) — **146 consecutive skips, June → December**.
The never-opened cleanup in `refresh_transactions` (ReadOnlyAccountInterface.py:1114) only appears to
fire at the very end (post-run in-mem dump is empty). Net effect: one missed fill = strategy dead
for the whole window. Compounds with F3 into permanent lockout.
Fix direction (dev): the DAY-expiry sweep (`_expire_stale_option_limits`, backtest_account.py:1558)
or the next `refresh_transactions` must promptly delete/close the WAITING transaction whose only
entry order is EXPIRED.

### F2 — probe sizing artifact (not a bug, but blocks naive probes)
$20k capital × 20% option sizing = $4,000 budget < $17,500 collateral for an AAPL 10%-OTM CSP
(strike 175) → `Insufficient budget to size cash_secured_put` every bar, 0 orders. Sizing is
`floor(virtual_equity × sizing% ÷ cost_per_contract)` (TradeActions.py `_size_by_cost`).
**Probe rule: use ≥ $100k capital for single-name CSP/wheel probes.**

### F3 — DESIGN QUESTION: next_bar_open + limit-quoted-at-analysis-close ≈ structural no-fill for premium sellers
With the default `next_bar_open` fill model, the entry limit is the analysis day's close premium
(0.40) but the fill attempt is the NEXT bar's open (0.33). For decaying OTM puts, next open < prior
close on most days, so a SELL_LIMIT almost never crosses; DAY TIF then kills the order. Same run
with `fill_model=same_bar_close`: **17 trades, +62.0% total return** over the same window.
Question for Bastien: intended? Options maybe need the limit re-quoted against the FILL bar, or a
marketable-quote convention — otherwise option selling strategies under next_bar_open + DAY TIF
produce near-zero fills regardless of capital.

### Status
- O_STK (equity) baseline works: 4 trades, +2.78% (Jun–Dec 2024) — pipeline/data/expert all fine.
- O_CSP at $100k same_bar_close: 17 trades, +62% — the option fill/close/mark pipeline DOES trade.
- O_WHEEL single probe still blocked by F1+F3 (needs fills to reach assignment); wheel unit tests pass.
- Poller alive; no new commits since `00a108d2`.

### Next
1. Report F1 to Bastien (likely grid-blocking for any option strategy with DAY-TIF misses).
2. If F1 fixed upstream: re-run O_CSP/O_WHEEL probes at $100k next_bar_open, count fills.
3. Perf parity task (preload/index options path) still pending.
4. Read `3654c9a7` ETF option universe doc.

---

## 2026-08-28 (night) — F1 fixed + pushed, F2 verified, F3 researched

### F1 — FIXED, shipped as `6369f8ee` (TEST_APP_VERSION 2026.08.0037)
Root cause confirmed: `_expire_stale_option_limits` (OPT-B4) terminalises an unfilled option
DAY-limit with NO fill, but `daily_engine.py:731` gated `refresh_transactions()` on
"something filled" — so the WAITING->CLOSED arm (ReadOnlyAccountInterface:1361,
`entry_orders_terminal_no_execution`) never ran and the dup gate held the symbol locked.
The teardown already existed; it just never got called.

Fix: `_expire_stale_option_limits` returns whether it terminalised anything; `refresh_orders`
folds that into its book-changed signal; engine gate unchanged (roll on any change). The gate's
old comment premise ("a transaction only changes state when one of its orders fills") predates
OPT-B4's own sweep.

Test: `test_refresh_orders_signals_the_roll_on_an_expiry_with_no_fill` mimics the engine gate
verbatim (roll iff signal). Verified RED pre-fix / GREEN post-fix. Full backtest suite 779
passed; launcher/option 1072 passed. NOTE: the pre-existing account-level test called
refresh_transactions() unconditionally — exactly why it couldn't catch this.

Push timing: the grid master had STOPPED POLLING at ~03:16 UTC (worker swept all 24 jobs at
03:21), so the version bump went out between runs — the documented safe window.

### F2 — VERIFIED: full wheel cycle runs end-to-end (INTC, $20k, gates-off)
With F1 + `hold_assigned_stock`:
```
short-put assignment of 100 x INTC at strike 35 — HOLDING the assigned stock (cash $16,562)
... covered-call overlay sell attempts (INTC241004C00020000 ...) ...
short-call assignment of 100 x INTC at strike 21 — HOLDING   <- called away
O_WHEEL trades=7 ret=-5.03%
```
Control run O_CSP (hold OFF, same window): same put assignment → `assignment_liquidation:
sold 100 x INTC @ 31.12 next-bar open`. Perfect A/B: wheel holds + recycles, CSP dumps.
(Used probe's new `--ride-to-expiry` mode to force natural expiry; normal runs exit early.)

### F3 — RESEARCHED: fill convention for premium sellers
Empirical head-to-head (INTC, Feb–Dec 2024, gates-off, authored genome):

| structure | next_bar_open | same_bar_close |
|---|---|---|
| O_CSP | 6 trades, −0.63% | 9 trades, −1.09% |
| O_LC | 5 trades, −6.02% | 8 trades, −16.65% |
| O_SSTG / O_BULLPS / O_IC | 0 | 0 (multi-leg fill-starved on thin chains) |

Mechanics: entry limits are quoted at the ANALYSIS close; next_bar_open then demands the NEXT
day's open cross the stale quote. For decaying OTM premium (puts especially) the premium keeps
falling away from the quote, the DAY order expires, and the entry never happens. same_bar_close
fills at the quoted bar's close — executable, but it IS the mild look-ahead the convention
exists to prevent (deciding and filling on the same bar's close price).

Recommendation (recorded, not applied):
1. KEEP `next_bar_open` as the grid default — conservative, no look-ahead, and it is what the
   existing equity grids used, so numbers stay comparable.
2. The real fix is QUOTE SIDE, not fill bar: premium sellers should quote a touch above/below
   the close (or use market-with-slippage) — i.e. an `option_entry_quote` gene (close vs
   close+offset) for the option grid, so the GA pays for realistic fill probability instead of
   quoting at the close and praying. Roadmap already anticipates intraday fills (5min clock
   gives the limit multiple crossing chances per day — the long-run answer once intraday
   option bars land).
3. Do NOT flip the default to same_bar_close silently — it would retroactively change every
   historical option number and introduce a look-ahead the platform's whole hermetic contract
   exists to prevent. If compared, run it as an explicit stress dimension (like spread_bps).

### Outstanding
- Grid master was DOWN at 03:21 UTC (24 jobs swept). Needs a look from the master box.
- Probe script gained `--ride-to-expiry` (untracked in repo, lives in worktree).

---

## 2026-08-28 (day, laptop) — parquet store wired + GA validated e2e on Q1-2023

Local box, `~/Documents/ba2/common/cache/TastyTradeOptionsProvider` (the Q1-2023 export:
686 underlyings, 9,587 partitions, 205 MB). `BACKTEST_OPTIONS_STORE=parquet`.

### Wired: the backtest can now read the parquet store
`ParquetOptionsProvider` + `options_store.resolve_options_store()`; sqlite stays the DEFAULT and
was proven bit-identical (full-engine digest vs a pristine `dev` worktree, with a 1e-9 fill-price
mutation shown to break the digest, so the identity means something). As-of clamping is TESTED,
not asserted — future bars are refused, a not-yet-trading contract is absent from the chain, and
delta differs across as-of dates.

Measured: cold chain load 56-273 ms/underlying, warm 0.5-1.5 ms (~60-100x). GOOG @2023-01-17
returns 180 contracts over 6 expiries, monotone delta, put/call parity on iv, and REAL
open_interest — the field the sqlite store has NULL on all 6,757,055 of its chain rows.
<sup>[superseded 2026-08-31: the conclusion holds — `open_interest` is NULL on **every** sqlite
chain row, and it is the *only* genuinely dead field there — but the row count is wrong. The
store has 1,440,782 chain rows, not 6,757,055. See
`ba2_common.core.option_selector._publishes_spread` for the re-verified record.]</sup>

### Structure sweep — 19 kinds, 9 symbols, 2023-01-10..2023-03-28, $100k, gates-off
**0 errors, 16 of 19 traded.** Wall 0.3-1.6 s per backtest.
O_WHEEL traded 28 times, so the wheel runs end-to-end on real data through the new store.
Zero-trade: O_BULLPS, O_IC, O_SSTG — all multi-leg credit, all fill-starved on thin chains,
consistent with the 10%-of-bar-volume participation cap. Same cause class as the babatest probe.

### GA e2e — it works, and it is fast
pop=40 x gen=5 = 200 trials, 9 symbols:
| parallel | wall | per trial |
|---|---|---|
| 8 | 30.4 s | **152 ms** |
| 4 | 40.8 s | 204 ms |
| 1 | 60.2 s | 301 ms |

The winning genome confirms this session's work is live end-to-end:
`option_entry_cross` (the F3 gene), `cond:shared-gate_confidence` / `cond:shared-rel_volume`
(shared ids), `cond:o_lc-exp_profit` (the gate that replaced the four FMPRating-only price gates).

### NEW FINDING — F4 (HIGH for the grid): results are not reproducible across `--parallel`
Same seed, same everything else, ONLY `--parallel` differing:

    pop=40 gen=5 : parallel=8 -> 791.25   parallel=4 -> 791.25   parallel=1 -> 871.27
    pop=16 gen=3 : parallel=4 -> -6.04    parallel=1 -> -43.66

Each configuration IS reproducible with itself (ran twice, identical both times), so this is not
flakiness — `--parallel` is part of the experiment, not a speed knob. Consequences:
 * a grid result cannot be reproduced on a box with a different core count;
 * distributed workers with differing `--parallel` are not running the same experiment;
 * comparing two jobs is only valid if their parallelism matched.
Likely cause: evaluation order changes how the GA consumes the RNG (selection/mutation draws),
rather than any per-trial nondeterminism — the per-trial backtests themselves are deterministic.
Fix direction: seed each individual's evaluation from (seed, generation, individual index) so the
draw sequence is independent of completion order. Until then, PIN `--parallel` in the grid config
and record it alongside the seed.

### Also fixed here
`entry_limit_with_concession` clamped a too-large concession to `0.0`. A SELL_LIMIT of 0.0 ALWAYS
clears, so the clamp wrote a short option for ZERO premium while carrying the full assignment
liability. Now DECLINED (returns the original limit) — an unfillable order is the honest outcome.
Two tests that had pinned the clamp as intent were re-pointed at the corrected semantics.

---

## 2026-08-28 (evening, laptop) — F4 fixed; a 40% perf win; and the fill clock is not a speed knob

### F4 — FIXED, shipped as `655c0ddf` + `e15b9af4` (TEST_APP_VERSION 2026.08.0046)

**The cause was NOT the one guessed above.** It is not evaluation order permuting the RNG draws:
`batch_fitness` returns fitnesses index-aligned and nothing in the master's batch loop draws
randomness — which is exactly why `parallel=4` and `parallel=8` agreed with each other.

The real mechanism: the GA drew all of its own randomness from the process-global `random`
module, and `DailyBacktestEngine.run()` opens with `random.seed(self.seed)`
(`backtest/daily_engine.py:481`). At `--parallel <= 1`,
`_dispatch_engages()` returns False, so `genetic.py` evaluates IN-PROCESS
(`list(map(self.toolbox.evaluate, ...))`) and every trial **reset the GA's stream mid-search**. At
`--parallel > 1` the trial runs in a spawned worker and the master's RNG is untouched. So the
stream was reset, not merely permuted — a stronger failure than the one hypothesised.

The fix gives the optimizer a private `random.Random`, cloned from the global state at
construction (so the draw sequence stays byte-identical to the old one) and checkpointed as
`ga_random_state`. `tools.cxTwoPoint` / `tools.selTournament` are replaced by draw-for-draw
identical local re-implementations, because DEAP offers no seam to hand those a generator.

Verified: small case (5 symbols, pop=16 gen=3) `parallel=1/2/4/8` all → **-6.04**, identical
`best_params` and the identical 29-genome evaluated set. Large case (9 symbols, pop=40 gen=5)
`parallel=1` and `parallel=8` both → **335.3088156886005**, same 138 genomes. Both large runs were
bracketed by a source-tree checksum to prove no concurrent edit landed mid-experiment.
**`--parallel > 1` results did not move at all** — only `parallel=1` changed, onto the answer the
other settings always gave. So no previously-recorded grid result at `--parallel > 1` is invalidated.

A second, independent parallelism dependence found and fixed in the same area (`e15b9af4`):
`genetic.py`'s in-process `evaluate()` scored a RAISING fitness function at `0.0`, while the batch
path scores `ZERO_TRADE_SENTINEL`. Strategy fitnesses are routinely negative (-6.04, -43.66), so
`0.0` meant "better than every genome that actually traded" and the GA would breed toward whatever
crashes the backtest. Now a named `FITNESS_EVALUATION_FAILED`, pinned by test to equal
`ZERO_TRADE_SENTINEL`.

### PERF — the NYSE schedule was 40% of every option trial (`b602aac8`)

`_nyse_calendar()` memoised the calendar OBJECT, but `.schedule()` — where
`pandas_market_calendars` actually works, ~10 ms a call — re-ran for every range.
`BacktestAccount._iv_rank_sample_dates` requests one trailing window per iv_rank evaluation, so a
run walks a sliding range and re-derives windows overlapping the previous by all but a day.

Profiled on the GA trial path (5 symbols, O_LC, 5min clock, weekly analysis):

| window | before | after | `nyse_regular_sessions` share of wall |
|---|---|---|---|
| 3 months | 0.52 s | **0.25 s** | 37% → 0% |
| 1 year | 0.86 s | **0.56 s** | 41% → 0% |

`get_iv_rank` fell from ~40% of wall to ~2%. Fitness is byte-identical (-6.04 before and after,
same seed) — this changes no number, only how often it is recomputed. The memo is bounded (512
ranges) and returns a copy, so no caller's mutation can delete a trading session for everyone else.

Scaling, measured rather than extrapolated: 4× the window costs 2.2× the time (fixed
preload/warmup/chain-load do not scale), so the earlier linear ~1.5 s/trial estimate for a
2–3 year stage-1 window is pessimistic. Still measured on 5 symbols only; a larger universe moves it.

### F5 — HIGH: on a 5-min clock, `next_bar_open` silently becomes SAME-DAY-OPEN for options

**Option bars are DAILY ONLY.** Every file in the parquet store is `<SYM>_<expiry>_1d.parquet`
keyed by a `bar_date` (9,587 files checked). There is no intraday option data anywhere, so a 5-min
fill clock adds **zero** price resolution for an option leg — the premium is constant across all 78
intraday steps of a session.

Worse, it changes the fill model. `_option_fill_price` picks the fill day via
`self._price.next_bar_date(underlying, as_of)` — the next bar of the UNDERLYING's series — then
takes `.date()`. On a 5-min clock the next bar is 5 minutes later, whose date is the SAME DAY:

| clock | order placed | resolved option fill day |
|---|---|---|
| 1d | 2023-02-01 | 2023-02-02 — next trading day ✅ |
| 5min | 09:30 | **2023-02-01 — same day** ❌ |
| 5min | 12:00 | **2023-02-01 — same day** ❌ |
| 5min | 15:55 | 2023-02-02 — next trading day |

So for 77 of 78 bars a session, an option order quoted on the analysis bar fills against **that
same day's daily bar OPEN**. Three consequences:

1. **The overnight gap risk `next_bar_open` exists to impose is gone** for option legs, while it
   still applies to equity legs — one run, two fill models.
2. **Look-ahead for any order not decided at the open.** An entry decided at 12:00, after observing
   the underlying's intraday move, fills at the option's 09:30 price. A GA is precisely the machine
   that finds and exploits that. The current grid analyses only at `times: ["09:30"]`, which limits
   the exposure — but pending orders retry on later bars, and the management schedule does not.
3. **The F3 `option_entry_cross` gene measures something else.** It was designed against the
   overnight barrier; at 5min that barrier does not exist for options.

Measured effect on trade counts (5 symbols, Q1-2023, gates-off, 1d vs 5min): O_STK 9/9 (equity,
unaffected), O_LC 5→6, O_LP 2→4, O_STRD 8/8, O_STRG 6→4, O_CC 12→13. Pure-option structures move,
so this is not academic.

**Recommendation: run the option grid at `--interval 1d`** until this is resolved. 5min costs ~50%
more wall clock to buy a fill model nobody chose. The principled fix is for the option fill-day
lookup to use the underlying's DAILY calendar regardless of the run's fill clock, so
`next_bar_open` means "next trading day" for an option at any interval — that would make 5min safe
and preserve intraday TP/SL precision for the equity control arm (O_STK) and stock legs
(covered call, assigned wheel stock), which DO have 5-min bars and are the only reason to want the
finer clock at all. Not yet implemented; it changes results for any option run made at 5min.

**Superseded in part by F6 below:** the covered-call/assigned-stock half of that argument is void.
A broker LOCKS those shares, so there is no intraday equity exit on them to gain resolution for.
Only the O_STK equity control arm remains a reason to want 5min, which is not enough to buy a
broken option fill model. The `--interval 1d` recommendation stands, more strongly.

---

## 2026-08-28 (evening) — F6: the backtest let a covered call go NAKED

### The defect

A broker LOCKS the shares collateralising a short call — while the covered call is open you cannot
sell the stock, because that would leave a naked call. The simulator modelled no such lock, so
O_CC's equity exits (it carries a staged trailing stop + time exit) sold the shares out from under
the written call.

Measured on O_CC / GOOG,BAC,INTC,F,T / 2023-01-10..2023-03-28 / $100k / gates-off / `--interval 1d`
by instrumenting `_covered_short_call_contracts`, classified into three buckets so a dead lot is
never filed as a live naked call:

```
PRE-LOCK (9096c27e)   O_CC: trades=19   uncovered short calls: 6
  contract              held  needed  bars  alive  nakedbars  verdict
  INTC230303C00030000      2     200    27     10         10  GENUINELY NAKED
  BAC230414C00034000       4     200    15     15         15  GENUINELY NAKED
  BAC230303C00037000       4     200    40     23          0  greedy-allocation artifact
  BAC230210C00036000       4     200    15      0          0  stale EXPIRED ledger lot
  BAC230303C00038000     297     200    19     19          0  greedy-allocation artifact
  GOOG230414C00105000    107     100     9      9          0  greedy-allocation artifact
```

Two genuinely naked short calls — unbounded upside risk — held for 10 and 15 bars. The
greedy-allocation rows are NOT this bug (`_covered_short_call_contracts` allocates
largest-lot-first, so a second lot on the same underlying is legitimately reported uncovered while
the shares are in fact there). The *stale expired* row was a second defect this work uncovered
(D1 below).

The codebase already knew this state was dangerous: `option_lifecycle.py` ranks
`LIFECYCLE_COVER_LOST` second in precedence, above profit capture — *"A covered call that has lost
its shares is a NAKED short call ... The reason is the alarm."* But `run_option_lifecycle_pass` is
called only from `JobManager.py`, the LIVE path. There were **zero** references to the lifecycle or
cover-lost machinery anywhere under `testplatform/backend/app/services/backtest/`. The backtest had
neither the lock nor the alarm.

### The fix — model the lock (`a6aca81e`, `9179aabf`, TEST_APP_VERSION 2026.08.0049)

Chosen over the alternative ("allow the sale, close the call next bar") because that leaves real
overnight naked exposure. An equity sell may not reduce the share count below what is pledged to
open short calls, and it **clamps** rather than blanket-refuses — a broker lets you sell the
unpledged excess (297 held, 200 pledged → sell 97, land on exactly 200; zero excess → cancel the
order). Only short CALLs pledge shares (short puts pledge cash; long options pledge nothing).
Assignment delivery is never blocked — a called-away covered call removes the shares and the call
together, and guarding that path would deadlock the wheel. An unmeasurable pledge REFUSES and names
the lot, rather than reading as "nothing pledged" and failing open into the very state being
prevented.

**Three further defects were found by adversarial review and fixed in `9179aabf`** — all reproduced
before being fixed, none theoretical:

* **D1 — the pledge counted EXPIRED contracts.** No expiry test, and the lot ledger is never
  reconciled, so at bar 2023-03-08 BAC asked to sell 293 and was allowed 0: a ledger pledge of 600
  against a true book pledge of 200, from two contracts that had expired on 02-10 and 03-03. A lot
  whose expiry has passed now pledges nothing (strictly `<`, since expiry settles at `<=` and the
  fill pass runs earlier in the same bar), and the stale lot is named at ERROR once per contract
  rather than silently obeyed or silently repaired.
* **D2 — a clamped partial fill CLOSED its transaction and stranded the residue.** `oco_leg_filled`
  was a membership test with no quantity in it, so a clamped stop still wearing its `OCO-SL-`
  comment marked the whole transaction CLOSED with a fabricated P&L on shares that never sold.
  Those shares then had no OPENED transaction — no TP/SL, invisible to every exit pass, absent from
  the round-trip trade list while the equity curve still marked them — so trade-level metrics
  stopped reconciling with the curve the GA scores.
* **D3 — a refused close stranded the row in CLOSING.** The pre-existing refusal was asked only at
  the bottom seam, by which point `close_transaction` had already flipped the row to CLOSING and
  cancelled its working orders — and CLOSING is a dead end, since the retry passes scan OPENED only.
  Measured on O_CC/BAC: refused 2023-03-13, cover released 2023-05-10, still CLOSING at run end. The
  refusal is self-clearing by nature, so it must leave the row exactly as it found it.

D2 and D3 are in `packages/common` (`ReadOnlyAccountInterface`, `AccountInterface`) and therefore
change LIVE behaviour too. Both reuse `cover_refusal_for_equity_sale` /
`shares_pledged_to_short_calls`, the pre-existing tri-state helpers from `828e65a9` — no second,
divergent notion of cover was introduced.

### Result

```
POST-LOCK (9179aabf)  O_CC: trades=15   GENUINELY NAKED: 0
                      final_equity 97741.34   total_return -2.26%   (was -1.42%)
```

**O_CC's historical numbers move and are no longer comparable to prior runs** — it had been booking
premium against risk a broker would not have permitted. A 19-arm sweep at full precision confirms
only O_CC moves: 18/19 arms byte-identical, including O_WHEEL, O_PP and O_CSP, so the shared-code
repairs do not leak outside covered structures.

Suites: backend `1 failed, 3520 passed, 158 skipped` (the 1 is the known Windows-only
`test_logs_rejects_path_traversal`, reproduced in isolation and untouched by these commits);
`tests/` 4411 passed; `packages/common` 2446 passed — all three re-run independently of the agents
that wrote the code. Test sensitivity was verified by neutering each of the four fixes one at a time
in a scratch worktree: with `_pledged_share_lock` stubbed to a passthrough, 12 of the 24 new cases
fail and the 12 ALLOW-path cases stay green.

### Still open

`SellAction` (`ba2_common/core/TradeActions.py`) is an unguarded equity-sell path in LIVE: it calls
`create_order_record(side="sell", ...)` with no pledge check, unlike `CloseAction`, which delegates
to `close_transaction`. In the BACKTEST it funnels through
`submit_order → refresh_orders → _apply_fill`, so the new fill-time clamp covers it. The live gap is
real and not yet closed.
