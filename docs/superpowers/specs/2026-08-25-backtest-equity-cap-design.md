# Backtest equity cap — design

**Status:** approved 2026-08-25. Off by default. Backtest only.

## The problem

A backtest compounds. A strategy that happens to do well in its first year deploys larger
positions for every year after, so its later results are carried by its earlier luck — and the
same strategy started twelve months later can score completely differently. That makes it hard to
tell a genuinely stable setting from one that got a good entry point.

**The test we want:** hold the capital still. If a strategy is given the same money every year and
still performs, the result is about the strategy rather than about when it started.

## What it does

An optional fixed dollar ceiling on the equity a backtest may deploy. When set, the risk manager
sizes every trade against that amount rather than against a compounding balance. Profits above the
ceiling are not deployed; losses below it are real, so a drawdown genuinely reduces what can be
put to work.

Off by default. When unset, every code path behaves exactly as it does today.

## Two quantities, deliberately separated

The central risk in this feature is one of these leaking into the other. They are computed in
different places and consumed by different callers.

### Deployed equity — what sizing sees

```
deployed = min(cap, real_equity)
```

`real_equity` is the account's actual value **including unrealised marks**. Two consequences,
both intended:

- Above the cap, the excess is invisible to anything that sizes a position. It is not withdrawn
  or hidden from the ledger — it simply is not offered to the sizer.
- Below the cap, the real figure is used. A drawdown to $15,000 under a $20,000 cap means the
  strategy deploys $15,000, and recovery raises it back toward the cap but never past it.

Unrealised is included because an open position that is down $5,000 genuinely leaves less to
deploy, and one that is up $5,000 is exactly the excess the cap exists to withhold.

### Scoring series — untouched by the cap

Each period's return is measured against the **fixed** capital:

```
period_pnl      = real_equity[t] - real_equity[t-1]      # from the RECORDED series, not the capped one
period_return   = period_pnl / cap                        # constant denominator, never the running equity
synthetic_curve = cap * Π(1 + period_return)
```

**"Period" means one point of the recorded equity curve** — whatever granularity
`snapshot_equity` writes for the run, which is what `build_results` already consumes. No
resampling: introducing a second time base would let the synthetic curve and the trade ledger
disagree about when a return happened.

`period_pnl` is taken from the **real** recorded equity, never from the deployed figure. The
deployed figure is capped by construction, so differencing it would report zero P&L for every
period spent above the cap — which is precisely the bug this separation exists to prevent.

A strategy earning $5,000 a year on a $20,000 cap reads **25% every year** — 20k → 25k → 31.25k →
39.06k → 48.83k, CAGR 25%, total return 144%. A steady strategy therefore reads flat whatever
year it started in, which is the whole point.

Compare with the naive alternative, which we rejected: feeding the raw `cap + cumulative P&L`
curve to the metrics gives 25%, 20%, 16.7%, 14.3% for that identical strategy, so a flat CAGR
would signal *improvement*.

### Drawdown is denominated in the cap too

```
max_drawdown_pct = peak_to_trough(cumulative_pnl) / cap
```

A $2,000 trough is 10% whenever it occurs. On the compounded synthetic curve it would otherwise
read −10% in year one and −5% in year four, leaving risk on a moving denominator while returns sit
on a fixed one. That asymmetry matters to the GA specifically: `dd_guard` is
`min(20/max(dd,1), 2.0)`, so a late-run strategy would earn a better guard multiplier purely from
arithmetic.

## Where it lives

**One seam.** `BacktestAccount.get_balance()` and `get_account_snapshot()` return the deployed
figure when the cap is set. Everything downstream inherits it without knowing the feature exists:

- `TradeRiskManagement.size_candidate_orders` via `expert.get_available_balance()`
- per-instrument caps via `expert.get_virtual_balance()` (account balance × `virtual_equity_pct`,
  so an expert on a 50% sleeve sees $10,000 under a $20,000 cap — correct and automatic)
- the buying-power gate, the margin check, and the option book rails

The alternative — capping inside the risk manager only — was rejected. Buying power and margin
would still see the real balance, so a margin account could deploy **twice the cap** while
appearing capped. This codebase has been bitten repeatedly by rules enforced at a call site
rather than at a seam; the assignment-capacity gate and the silently-dropped `FIELD_EVENT`
conditions were both that shape.

**`snapshot_equity` keeps recording real equity.** The results curve is built from truth and
converted at scoring time. The cap must never reach the recorded series, or the run's own history
becomes unreconstructable.

## Configuration

A single nullable field on the backtest config: `equity_cap`. `None` means off.

- **Single backtest:** a field on the run configuration.
- **GA:** a **run-level parameter, not a gene.** Making it a gene would optimise the capital
  rather than the strategy — the opposite of the intent — and every individual in a population
  would then be scored against a different denominator.

Validation:

| input | behaviour |
|---|---|
| `None` | feature off; all paths byte-identical to today |
| `<= 0` | **refuse** at config validation with a named error |
| above the initial capital | **allowed**, logged at INFO that it cannot bind until equity reaches it. It is not an error: the account may grow into it |
| non-numeric | refuse |

## Live safety

The field and the logic live on `BacktestAccount` only, never on `AccountInterface` or any live
broker class. A live account has no code path that could reach it.

This is asserted by test rather than assumed: a test walks the live account classes and fails if
any exposes an equity-cap attribute or honours the config key.

## No regression

With `equity_cap = None` every path is byte-identical to today.

Proven by a **golden comparison** — a full backtest run captured before the change and re-run
after, asserting identical results — not merely by the suite staying green. The suite passing
proves nothing was broken that a test already covered; the golden run proves nothing moved at all.

## Edge cases

- **Recovery after a drawdown.** `min(cap, real_equity)` handles it: deployed climbs back to the
  cap and stops.
- **Nothing is affordable at the cap.** The run completes and the results carry an explicit note
  ("no position was affordable at the configured cap"). It does not raise — a strategy that cannot
  be run at $20,000 is a finding, not a crash. For the GA this already scores catastrophically via
  the existing `trade_gate` sentinel, which is loud enough.
- **Zero-length or single-period runs.** The synthetic curve needs at least two points to produce
  a return; with fewer, the return is unmeasurable rather than zero.
- **A period P&L of exactly zero** is a measured flat period, not a missing one. This project has
  spent a great deal of effort separating those two, and the conversion must not reintroduce the
  conflation.

## Testing

### End-to-end is mandatory, not optional

Unit tests on the two conversions are necessary and **not sufficient**. The entire feature is a
number flowing correctly through the whole engine — sizer, buying-power gate, margin check, rails,
equity recorder, metric builder — and every previous defect of this shape in this codebase was a
seam that each side tested correctly in isolation. Existing harnesses to extend rather than
replace:

| harness | use for |
|---|---|
| `testplatform/backend/tests/backtest/test_engine_golden_regression.py` | the cap-off byte-identical golden run |
| `testplatform/backend/tests/backtest/test_daily_engine_e2e.py` | a full capped single backtest |
| `testplatform/backend/tests/test_options_optimization_ga_e2e.py` | the GA path, cap as a run-level parameter |

The e2e runs must assert on **real engine output**, not on mocked intermediate values:

- A full capped run where deployed equity is sampled at every bar and **never exceeds the cap**,
  including after a large gain.
- The **same strategy, same data, started in year 3 versus year 1, scores the same.** This is the
  feature's entire reason to exist and it can only be shown end to end.
- A capped run produces **visibly smaller positions** than an uncapped run on identical data —
  proving the cap reached the sizer rather than only the metrics.
- The buying-power gate and margin check refuse a trade the *real* balance would have permitted,
  proving the cap reached them too and did not stop at the risk manager.
- A GA run completes with the cap set, every individual scored against the same denominator, and
  the cap absent from the gene space.

### Unit level, beyond the golden comparison:

- $5,000 a year on a $20,000 cap reads 25% every year and CAGR 25% — the headline case, asserted
  on the actual numbers.
- The same strategy started in year 3 of the data scores the same as one started in year 1. This
  is the feature's entire purpose and deserves a test that says so.
- A $2,000 drawdown reads 10% in year one and 10% in year four.
- Deployed equity never exceeds the cap, including after a large gain.
- Deployed equity falls below the cap on a drawdown and recovers to — not past — it.
- Buying power, margin and the option rails all see the capped figure, not the real one.
- With the cap off, a golden run is byte-identical.
- No live account class exposes the cap.

## Out of scope

- Live trading. This is a backtest analysis tool.
- Periodic contributions or withdrawals — a fixed ceiling only.
- Making the cap a GA gene.
- Comparing a capped run against an uncapped one in the same grid. CAGR under a fixed notional and
  CAGR under compounding are different quantities that share a name; mixing them in one ranking
  would be the same "two things, one label" trap this codebase has hit repeatedly. If cross-mode
  comparison is ever wanted it needs its own decision.
