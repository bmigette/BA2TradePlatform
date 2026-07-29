# Queued fixes — blocked on opt 233 (2026-07-29)

All of these need a **version bump**, which cannot happen while opt 233 runs: the master
snapshots its version at job start, and a repo that moves ahead makes the worker pull the new
version and then fail the job's version match forever (this cost opt 218). Editing the tree is
also unsafe mid-run — local trials execute from it, so a later-spawned pool worker would run
different code than an earlier one.

Ordered by value.

---

## 1. Log the `fitness=0.0` trapdoor  (`distributed_eval.py:284`)

```python
except Exception as e:
    out = {"ok": False, "fitness": 0.0, "trades": 0, "error": repr(e), "fatal": False}
    self.broker.post_result(job["trial_id"], out)   # <- no log line
```

A trial that RAISES becomes a 0-fitness result with no log. The GA silently discards that genome
and the search is biased against whatever triggers the error — the ATR pattern in new clothing.

Fix: log at ERROR with the traceback before posting. Cheap, and it converts the single remaining
"silent" path in the distributed evaluator into a visible one.

Probe that works TODAY (no code change), over `strategy_optimizations.all_results`:

```python
exception_path = [r for r in res if (r.get('fitness') or 0) == 0 and (r.get('trades') or 0) == 0]
```

**BOTH conditions are required.** `fitness == 0` ALONE is a false-positive detector: a trial can
legitimately score 0.0 with real trades, because CAR is
`base x dd_guard x consistency x trade_gate` and an `annualized_return` of exactly 0 zeroes the
product. Measured 2026-07-29 — opt 232 and opt 233 each contain exactly one such trial, one of
them with **410 trades**. The exception path is distinguishable because it hardcodes
`"trades": 0` alongside `"fitness": 0.0`.

(I initially used the one-condition version, saw `err0=1`, and wrongly announced the trapdoor had
fired. The discriminating field was already in the record I had printed.)

Both sentinels remain separately identifiable: `-1e9` = no trades, `-2e9` = account wiped out.

Current status: **0 exception-path trials across opt 232 (216) and opt 233 (153)** — latent, not
active.

## 2. Buffered worker logging  (agreed with the user 2026-07-29)

Attach `logging.handlers.MemoryHandler(capacity=N, flushLevel=CRITICAL, target=<stderr>)` at level
ERROR in `_worker_init`, keep `logging.disable(logging.INFO)` so DEBUG/INFO records die before
formatting (~17k/trial — the real cost), and flush explicitly in `_trial_worker`'s `finally`.
Visibility without per-bar disk I/O.

**Read this before implementing** — the premise I originally gave for this was WRONG:

* the call is `logging.disable(logging.INFO)`, which already allows WARNING/ERROR/CRITICAL
  through (its own comment says so). I repeatedly mis-cited it as `disable(ERROR)`;
* `_worker_init` does set `BA2_FILE_LOGGING=0` / `BA2_STDOUT_LOGGING=0`, so a worker has no ba2
  handlers — but Python's `logging.lastResort` then writes WARNING+ to **stderr**, which the grid
  captures via `2>&1`. Verified empirically 2026-07-29 (bare, unformatted output = the lastResort
  signature).

So worker errors are NOT plumbed away today. This change improves COST and structure, not
visibility. Do not sell it as a fix for invisible errors.

## 3. Correct the wrong `disable(ERROR)` claim in code

The bad claim leaked into `packages/common/ba2_common/core/failure_modes.py` (module docstring)
and `tools/summarize_would_raise.py`. Both state GA pool workers run at `logging.disable(ERROR)`
and that this is how the ATR bug stayed invisible. Replace with the corrected account above: what
hid the ATR bug was signal QUALITY — a `logger.warning(...)` + `return None` is indistinguishable
from the legitimate "this symbol has no ATR data".

## 4. `fetch-cache` must fail loudly on zero rows

Observed 2026-07-29: a run that fetched NOTHING reported

```json
"5min": {"status": "success", "rows": 0}
```

and the wrapper printed `fetch-cache: 1/1 symbols (0 failed)`. A monitor grepping for "completed"
confirmed the failure. Zero rows for a non-empty symbol list is a failure, not a success.

## 5. `_parse_symbols_arg` should accept a comma-separated `@file`

`@file` splits on WHITESPACE ("one symbol per line"), but `~/Documents/ba2/senate_universe.csv`
is a single comma-separated line — so the whole file became ONE 3,000-character "symbol" that FMP
predictably had no data for. Split on commas AND whitespace; a symbol containing a comma is not a
thing.

## 6. Warmup is measured in BARS but applied as CALENDAR DAYS at the execution interval

`_EXPERT_WARMUP_BARS` is documented in trading BARS and converted by `_BARS_TO_CALDAYS = 1.45`
(252 bars -> 365 days), a conversion that only makes sense for DAILY bars. But
`price_source.preload` does:

```python
fetch_start = start - timedelta(days=warmup_days)
win = (self._interval, ...)          # <- the EXECUTION interval
```

So on a 5min run, FactorRanker's 252-bar momentum pulls **375 calendar days ≈ 29,000 five-minute
bars per symbol** when it needs 252 daily ones. Across ~500 symbols that is likely a large share
of the measured ~12GB/worker RSS.

Investigate whether warmup should be expressed per-interval. Potentially a much bigger memory win
than the scoring-cache work (which saved ~270MB/process against a ~12GB worker). **Measure before
changing** — indicators may legitimately need intraday warmup, and shortening it would silently
cold-start them, which is the failure mode this codebase keeps producing.

---

## Not queued: the ~1513 remaining broad handlers

Do NOT bulk-convert now that `BA2_ERROR_MODE=enforce` is the default — 821 of them are in live
`ba2_trade_platform` (brokers, UI, JobManager), and each conversion changes live behaviour at
that site. Per-module, calibrated in observe mode. The paths that gate money are already done
(`TradeConditions.py` 52/52). See [[reference-error-mode-enforce]].
