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
