# Production 8081 entry-stop review — September 9, 2026

## Finding and correction

Confirmed: the RAT entry ruleset's tighter stop was overwritten by the risk
manager safeguard during live submission. This is a live/backtest discrepancy,
independent of margin. The backtest already reconciles both stops before submit.

Production `app.log` records the sequence:

| Transaction | Symbol | Ruleset adjustment | Later initial_setup adjustment |
|---|---|---|---|
| 194 | ENOV | 15:32:23.384, SL $19.14 | 15:32:24.553, SL $17.95 |
| 195 | CBIO | 15:32:24.588, SL $20.05 | 15:32:25.319, SL $18.80 |

The same sequence appears on September 7 for RARE (transaction 127, $14.78 to
$13.86) and ANAB (transaction 128, $54.53 to $51.12). Subsequent fill rebasing
preserved the widened approximately 10% distance. The database's current
`open_date` for those older transactions is September 8; the September 7 entry
date above comes from the logged submission sequence.

Live `TradeManager` now calls the existing shared `reconcile_protective_stop`
before funded submissions and wash-trade retries. Database-lock retries reread
the current transaction stop on each attempt. For longs, the higher stop wins;
for shorts, the lower stop wins. A ruleset-only stop keeps its existing leg.
Closing orders and stop-entry triggers retain their existing retry handling.

The helper's executable implementation, RM sizing inputs, share quantities,
strategy rules, fill policy, and backtest engine are unchanged. Only live
submission adopts the backtest's existing selection policy. The obsolete comment
saying this live change awaited approval was updated following the user's
explicit request to correct this discrepancy and preserve backtest behavior.

## Today's analyses and strategy distinction

The read-only production snapshot reports four new expert entries: small ED
opened ANAB (3 shares, transaction 192) and NX (10 shares, transaction 193); RAT
opened ENOV and CBIO (14 shares each, transactions 194/195).

Today's analysis rows in the snapshot comprise:

- Small ED (expert 9): 24 completed entry analyses.
- RAT (expert 12): 4 completed and 15 skipped entry analyses, plus 2 completed
  open-position analyses.
- Mid DS (expert 10): 4 completed open-position analyses.
- Mid ED (expert 11): 1 completed open-position analysis.

There were no failed analysis rows in that snapshot. This is a status count,
not a claim that every skipped symbol should have traded.

RAT has **two entry branches**, not one global 4% stop. Confidence >=80 selects
the 4% ruleset stop; its lower-confidence branch specifies 18%. With the current
10% safeguard, the existing backtest selects 4% and 10%, respectively. The fix
preserves both cases. RAT's open-position rule requests 18%, but the shared
ruleset stop ratchet rejects widening an existing tighter stop. It therefore
will not repair the four already-overwritten stops by itself.

Small ED's entry rule only buys. Its 10% safeguard is intended at entry, and its
open-position rule tightens to 6%. Today's ED ANAB/NX entries are consistent with
that policy. In particular, ANAB transaction **192 belongs to ED**, while the
older ANAB transaction **128 belongs to RAT**; they must not be confused.

The log also contains account-configuration parsing and margin-capacity warnings.
Neither explains the demonstrated stop overwrite. This patch does not alter
broker configuration, capital limits, allocations, or the strategies' settings.

## Existing positions still affected

The following are from the broker-synchronized local database, read without
starting the live app or sending broker requests. They are not a fresh direct
broker verification. The last column is the configured 4% rule applied to the
recorded fill price, rounded to cents for display.

| RAT transaction | Symbol | Shares | Recorded fill | Recorded stop | 4% rule from fill |
|---|---|---:|---:|---:|---:|
| 127 | RARE | 10 | $15.1200 | $13.6080 | $14.52 |
| 128 | ANAB | 2 | $55.4800 | $49.9320 | $53.26 |
| 194 | ENOV | 14 | $19.9753 | $17.9818 | $19.18 |
| 195 | CBIO | 14 | $20.6215 | $18.5584 | $19.80 |

This code correction prevents future submission overwrites. It does **not**
retroactively change these existing broker orders. They still require separate
manual review/correction in the trading platform. No production database, broker
order, or account setting was modified during this investigation.

## Verification and scope

- The new regression file reproduced six failures before the fix, including a
  real Alpaca bracket being overwritten from 96 to 90 in a temporary database.
- After the fix, 93 live tests pass across funded entry processing, DB-lock and
  wash-trade retries, close handling, idempotency, and fill rebasing.
- Eight shared protective-stop/safeguard tests pass.
- The integrated regression uses real Alpaca bracket maintenance and the live
  fill-rebase path with broker I/O replaced: a stop at 96 on a 100 reference
  remains 96 at submission and becomes 96.96 on a 101 fill. TP and quantity are
  preserved. Both long/short and tighter-safeguard cases are separately pinned.
- The shared stop helper's executable AST is identical to pre-change `d6345511`;
  its only package edit is documentation. Backtest calculations were not edited.

Frozen replay results and fingerprints are recorded in
[validation](entry_stop_validation_2026-09-09.txt). No baseline was regenerated.
The previously documented cash/equity and instrument-cap parity exceptions remain
outside this fix; it establishes parity for entry stop selection, not a new claim
that every live/backtest behavior is identical.

Validation uses simulated broker responses, not an actual paper/live broker
trade. The running production process must load the corrected code for future
entries; restarting or verifying that process is outside this code push.

Evidence: [read-only database extract](entry_stop_evidence_2026-09-09.json).
Release versions: APP 2026.09.1147, TEST 2026.09.0027.
