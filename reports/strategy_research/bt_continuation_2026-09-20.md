# BT session continuation — 2026-09-20

## Request recovered and completed

Claude session **BT** stopped at its usage limit after the user requested a lower
trade minimum and then clarified: **“Or penalize under 30 but not discard.”**
The implementation uses 30 completed structures across the whole backtest window.
It does not impose a 15-trade minimum or a 30-per-year target.

Implemented and pushed **`acd94ddeffe3a89c16743d26a0a7b0bcead91715`** to `dev`.
APP `2026.09.1174`, TEST `2026.09.0061`.

- Explicit metric: `option_car_target_soft30`.
- Count factor: `min(completed structures / 30, 1)` on positive scores.
- Legacy objectives and trade execution remain unchanged.
- Existing concentration, Monte Carlo and spread penalties remain active. A sparse
  book can still score zero because of concentration; it is no longer rejected
  solely by the annual trade-count floor under this new objective.
- Discovery identities differ, and checkpoint metadata prevents mixing soft30 with
  old scores under the same name.

See [scoring and launch specification](../../docs/strategy_research/options/option_stage1_soft30.md).
The focused checks passed: 1,107 tests, including legacy metrics and golden parity.
One Windows shell test first selected unavailable WSL; it passed with Git Bash.

## Remote campaign

Host: `debian@141.94.199.227`, dedicated checkout `/home/debian/ba2-grid/repo`.

The old campaign was active on the initial inspection but had failed by the restart
check. Its watchdog reported **one trial without progress for 5,400 seconds**, then
aborted optimization 6 with `local pool stalled`. The sequencer stopped after job
1/16; it did not launch later jobs. The exact cause of that trial's stall was not
established. This preceded deployment of the new objective.

The process safety check refused the initial restart when the expected sequencer
was absent. A subsequent inventory confirmed no grid Python processes remained.
No process termination was then necessary. The separate fleet worker PIDs 1369
and 653399 retained their original process start times.

Preserved before the new launch:

- Database backup: `/home/debian/ba2-grid/archive/before-soft30-20260920T072952Z/dl_forecasting.db`
  (23,539,712 bytes, SQLite backup API).
- Previous log: `/home/debian/ba2-grid/archive/before-soft30-20260920T072952Z/stage1_2020.log`.
- Dry-run output: same archive directory, `soft30-dry-run.log`.
- Previous optimization rows and checkpoints retained without re-scoring or deleting them.
  Rows 4 and 5 still have their pre-existing stale `running` database labels; they do
  not correspond to active grid processes.

New launch: **2026-09-20 07:29:52 UTC / 09:29:52 Paris**.

| Item | Verified value |
|---|---|
| Optimization ID | 7 |
| First job | `optm-DeterministicScorer-O_LC-st1soft30-dbf86bd781123` |
| Metric | `option_car_target_soft30` |
| Robustness | enabled |
| Campaign | DeterministicScorer only, 16 structures |
| Window | 2020-01-01 through 2025-12-31 |
| Starting equity | $20,000, retained from BT's existing campaign |
| Search | population 200, generations 60, patience 8, parallel 24 |
| Options store | ThetaData |
| New log | `/home/debian/ba2-grid/stage1_soft30_20260920T072952Z.log` |
| Receipt | `/home/debian/ba2-grid/soft30_restart_receipt.json` |

Both existing market-condition manifests verified and prepared successfully, each
covering 7,081 objects and 7,358 raw shards:

- `ohlcv-v1`: `a101c3e1840e550eb1b4e6851e7de6e40eafbbac3b1b4acb0be6e15674ad10cc`
- `ta-structure-v1`: `be3ed624e35fa7b8c028b9f86d9c8e5fa64d669bea5dd7e4c970e2d906983bf9`

The first trial completed and persisted under the new metric; it had zero trades
and correctly retained the zero-trade sentinel. At the verification snapshot there
were zero tracebacks, missing-session messages, no-context messages or pool-stall
messages. This establishes successful startup and scoring dispatch, not campaign
completion or resolution of the prior long-running-trial issue.

## Neutral structure clarification

**Superseded decision:** the user subsequently requested both HOLD and low-confidence
arms. The original finding below explains the old behavior; implementation and
preservation are documented in [the September 20 grid review](option_grid_review_2026-09-20.md).

The user correctly pointed out the existing low-confidence BUY/SELL condition.
Actual engine fixture fills confirm that, with the extra HOLD flag disabled:

| Signal | Confidence | Low-confidence threshold | Entry |
|---|---:|---:|---|
| BUY | 20 | 30 | filled |
| SELL | 20 | 30 | filled |
| BUY | 50 | 30 | rejected |

Enabling the separate HOLD-only flag rejects both BUY and SELL; HOLD is filtered
before entry-rule evaluation. The review recommendation is therefore corrected:
remove the conflicting HOLD flag from newly generated neutral recipes and keep
the low-confidence condition. Broadening live/BT HOLD eligibility is unnecessary
for the user's intended strategy. This recipe correction is documented, not applied
by the scoring commit; old rules and the grid's existing neutral gate remain intact.

The updated [commit review](commit_review_2026-09-19.md) and its engine probe also
retain the separate follow-up-driver preflight finding. Neither finding was silently
bundled into this scoring/restart change.

### Does DeterministicScorer actually emit HOLD?

Yes, in the current implementation. `combine.schmitt_trigger` returns BUY above
`theta_buy`, SELL below `-theta_sell`, and HOLD between them. The default thresholds
are +0.30 and -0.20. `_process` calls that function with `prev_signal=None`, explicitly
maps all three actions to `OrderRecommendation`, and returns the selected signal.
The grid searches positive buy/sell thresholds, so it retains a HOLD region too.
The existing signal-threshold regression test passed on September 20.

For example, a final score of +0.10 produces HOLD; +0.35 produces BUY with 35%
confidence; -0.25 produces SELL with 25% confidence. HOLD is an expert output,
but the entry pipeline filters it before rule evaluation.

The earlier engine probe injected recommendations to test entry plumbing. Its
BUY-at-20% example is not possible under the default buy threshold of 0.30; it
becomes possible when the grid selects a lower threshold such as 0.15. The
low-confidence cutoff must therefore be considered together with the expert's
buy/sell thresholds, rather than treated as an independent supply of signals.
