# Audit Fix Design — 2026-09-06

**Goal:** Land the ~18 remaining findings from the 2026-09-06 GPT-6 audit pass without
invalidating existing backtest results, except where a defect is critical to live parity.

**Source reports:** `audit_gpt6_code.md` (15 findings), `audit_gpt6_options.md` (12),
`audit_gpt6_strategy.md` (truncated), `audit_gpt6_strategy_design.md`. `audit_t2.md` is empty
(HTTP 503) but its three expert files are a strict subset of the code audit's eight, so no
findings were lost.

**Already landed:** Senate disclosure-date lookahead (`6d08a34e`), option half-quote +
bracketed `last` fallback (`6d08a34e`), ThetaData `StatusCode.INTERNAL` classifier
(`b0629626`), sizing caps + `uncovered_short_calls` (`5baa3a69`, unpushed).

---

## Organising principle

Findings are grouped by **what a fix costs**, not by severity. The binding constraint is that
every `packages/` change needs a `TEST_APP_VERSION` bump, and each bump forces every
distributed worker to re-sync. So all buckets land as **one bump, one push**.

| Bucket | Scope | Cost |
|---|---|---|
| A | Options (11) | Free — no option strategy has been optimized yet |
| B | Senate (2) | Free — both are live-only defects |
| D | Measure-then-decide (5) | Free where the path provably never fired |

Bucket C (EarningsDrift `#15`) was **dropped** — see "Rejected" below.

---

## Bucket A — Options (11 findings)

Free rein: nothing has been run, so nothing can be voided. Eight of the eleven are one shape —
*an input we cannot measure is treated as a passing value* — the same idiom as the
`bid = ask = close` defect already fixed.

**A1 — Guards that must refuse instead of pass.**
- opt #2: `option_risk_manager_enabled()` catches a malformed `risk_manager_mode` and returns
  `False`, so a typo (`classic_option` for `classic_options`) silently disables the option
  rails. Must raise, not disable.
- opt #3: `eligible()` treats a missing `structure_fn` like a disabled ceiling, so
  `max_loss_ceiling=0` admits a contract without measuring its loss.
- opt #8: the minimum-premium floor applies only when `mark is not None`, so an unpriced
  contract passes a gate that cannot measure it.
- code #7: the no-arb gate returns `None` on missing contract terms or spot, which the caller
  reads as "no rejection".

Each becomes a refusal naming the missing input.

**A2 — Missing ranked as best.**
- opt #9: `_WORST = 0` multiplied by a NEGATIVE weight is the *best* contribution, not the
  worst, so a missing IV/premium wins the tie-break.
- opt #10: NaN survives the `None` checks; every subsequent comparison is false, so a
  structure with NaN P&L and delta returns a healthy HOLD instead of `LIFECYCLE_UNKNOWN`.

Extends the `_minimise` discipline that already exists in `option_selection_policy`.

**A3 — Arithmetic.**
- opt #11: `structure_metrics()` hardcodes a 100 multiplier despite `OptionStructure.multiplier`
  being an input, so candidate and existing-book measurements can disagree with assignment.
- opt #7: `select_wing()` picks the nearest strike to target without requiring a call wing
  above the centre (or a put wing below), so it can return the centre strike and collapse the
  spread it was meant to build.

**A4 — State and time.**
- opt #6: breaker state keyed `(None, expert_id)` when `inmem_trades_active()` is false, but
  `reset_thread_state()` only removes keys whose first element is the current thread id — so a
  halted breaker survives into the next file-backed run for that expert.
- opt #12: `delta_at_entry()` accepts a datetime, discards the time, and includes that date's
  daily bar — returning a delta derived from the entry day's *closing* prices.

**A5 — code #9.** Unresolvable valuation prices are replaced with entry prices, and
`_liquidate_option_lot()` executes a cash settlement at that fallback. Same family as the
`_quote_side` bracket fix: a price that is not executable must not stand in for one that is.

Each fix gets a regression test in the existing suites.

---

## Bucket B — Senate (2 findings), live-only

Both are live-path defects; the backtest side is already correct. **The running Senate rerun
does not need restarting.**

- **#13** `as_of_bucket = now.strftime("%Y-%m") if is_live else now.date().isoformat()`. Live
  buckets the confidence/skill cache by month while the yearly buy/sell windows slide daily,
  so live can return a focus figure that is weeks stale. Drop the live branch and bucket
  daily everywhere; the `else` arm is untouched.

  Nearly free: the cache exists because a GA job re-walks the same history hundreds of times
  per trial, whereas live runs it once per scan cycle. Daily bucketing keeps all intra-run
  sharing across symbols on the same day and loses only cross-day reuse — exactly the stale
  part.

- **#14** `_PRICE_MAP_MEM` is a module-level LRU keyed by symbol with no expiry, so a
  long-lived live process that cached a symbol on Jan 2 never sees Jan 8's price;
  `_get_price_at_date` returns `None` and the trade is silently dropped. Key the memo on
  `(sym, stamp)` — `stamp` is `None` in backtest (behaviourally identical to today) and
  today's date in live.

---

## Bucket D — Measure, then fix only what is free

Each finding gets a cheap probe first. The fix lands only where the bad path provably never
fired, in which case it cannot change any result.

| # | What fires it | Probe |
|---|---|---|
| #8 | an order reaching fill with no quantity | orders where `status=FILLED AND filled_qty=0` across all persisted backtests |
| #12 | a parquet/path exception during preload | completed grid logs for preload `missing` > 0 |
| #6 | a caller passing a tz-aware, non-UTC `end_date` | static sweep of `_slice`/`get_ohlcv` call sites |
| #11 | `insider_get` returning non-dict, or a sale with `value=None` | run logs for the failure path; cached FMP insider payloads for null `value` |

**#1 is not latent** — it fires on every fill, so no measurement makes it free. It splits:

- **#1a (free, land now):** stamp `_fill_dates` with the bar the fill price actually came
  from, leaving *when* cash and positions are applied unchanged. Trade history becomes
  truthful and live parity improves (live records real fill timestamps; backtest recorded the
  decision bar). The equity curve — and therefore every metric — is bit-identical.
- **#1b (REJECTED):** deferring the whole fill application to the next bar was considered and
  dropped. It would move the cash impact a bar later, voiding every existing backtest, and
  buys almost nothing:
  - The equity snapshot is consumed **only** for reporting (`bt.equity_curve`, and the
    Sharpe/max-DD computed from it). Nothing feeds it back into a decision — sizing reads the
    account balance live, and the sole non-reporting consumer is the
    `net_liquidating_value <= 0` ruin flag, which a rounding-scale blip cannot flip.
  - The error does **not accumulate**: bar N carries `qty x (close_N - open_N+1)`, and at bar
    N+1 the position marks normally and it is gone. So **CAR is entirely unaffected** (final
    equity is identical), and only max-DD and Sharpe see single-bar, random-sign noise
    bounded by position weight x overnight gap — order 0.025% of equity against drawdowns of
    10-16%.

  Voiding every result for a sub-basis-point cosmetic correction to two metrics is a bad
  trade. #1a delivers the part that matters — truthful trade dates and live parity in the
  trade record — for free.

---

## Verified as NOT lookahead (no action)

- **code #1** (next-bar fills booked before the clock reaches the fill bar). Real as
  bookkeeping, but the engine's per-bar order is `set_clock → analysis → sizing →
  submit_order → _fills_and_settlements → snapshot_equity`. Every decision is made *before*
  the fill, so a strategy can never condition on the next bar's open. Handled as #1a/#1b above.
- **code #2** (same-bar retrospective execution). Inapplicable: `fill_model` is absent from
  all 400 most recent persisted run configs, so every run takes the `next_bar_open` default.

---

## Rejected

**code #15 — EarningsDrift live-only calendar shortcut.** The audit overstated this. The bulk
call is `earning_calendar(from_date=now-max_days, to_date=today)`, deduped to the latest row
per symbol and keyed upper-case — the same data as the per-symbol path, cached and accessed by
symbol. Absence is genuinely conclusive because the window *is* the strategy's own lookback.
Failures fall through to the per-symbol fetch, and ~70% of rows fall through anyway (bulk rows
lack the analyst estimate). The only residual is ≤4h staleness on a multi-day drift signal
inside a multi-day window, which the next scan catches.

---

## Out of scope

The design audit's ten per-expert strategy overlays (sections A–F of
`audit_gpt6_strategy_design.md`) get their **own design doc**. They are blocked on
instrumentation we do not currently persist — daily positions, equity, reserved buying power,
and capital-days — which the audit names as the missing input for ranking candidates on
capital efficiency and pairwise overlap.

---

## Sequencing

1. Land Bucket A, Bucket B, #1a, and the proven-free parts of Bucket D locally.
2. Run the Bucket D probes; report blast radius for anything that did fire.
3. **One** `TEST_APP_VERSION` bump, one commit, one push.
4. Leave the Senate rerun running throughout — nothing here disturbs it.
5. Any non-free Bucket D fix waits for a grid boundary. #1b is rejected outright, so with a
   clean Bucket D there is nothing left needing a rerun.
