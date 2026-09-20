# Commit review — 2026-09-19

## Scope and delivery

Committed and pushed the follow-up driver as **`4ce1e6e8e4c4711a4c367b85e091bb2c2569f4a0`**
to `origin/dev`, on top of **`f359ab9afb6ce5fddd17345ecb897c076cb6f97c`** (option entry
direction). APP version is `2026.09.1173`; TEST version is `2026.09.0060`.
Unrelated working files were left untouched.

Reviewed both commits because the latest pre-existing commit was the option-direction feature.
The earlier neutral-entry change (`d66a5a75`) is identified separately below. This review did
not change trading behavior or submit research jobs. Its probe uses isolated temporary
databases, fixture option prices and stub experts.

**Verdict:** the new option direction selector works through an actual option fill, including
the contrarian SELL-to-long-call case. The follow-up driver's default campaign remains
unchanged. Two actionable problems remain: an inherited neutral-entry path that cannot reach
its rules, and a readiness mismatch introduced by the new driver. The green tests do not
establish that every newly advertised strategy can trade.

## Findings

### R1 — High: neutral-signal option entries are unreachable

**Origin:** `d66a5a75`, retained by `f359ab9a`; this is an adjacent existing issue, not a new
defect in the signed direction condition.

- [Launcher](../../testplatform/ba2test_launcher.py), `_NEUTRAL_ENTRY_MEMBERS` near line 2822
  and `_option_signal_gate` near line 5110, makes O_STRD, O_STRG and O_IC require
  `current_rating_neutral` when their signal gate is enabled.
- [Backtest entry loop](../../testplatform/backend/app/services/backtest/daily_engine.py),
  lines 308 and 967, discards HOLD before the entry evaluator runs. Entry staging never
  passes `allow_hold=True`.
- [Live entry loop](../../ba2_trade_platform/core/TradeManager.py), line 2153, filters HOLD
  out of its recommendation query before applying the entry rules.

An enabled neutral gate therefore cannot open a new position: HOLD never reaches the gate,
and BUY/SELL fails it. The GA can trade with the neutral gate switched off, but it cannot
actually explore the advertised HOLD-only strategy. This affects interpretation of the new
neutral search space, not merely a display or logging message.

**Reproduction:** the real `DailyBacktestEngine.run()` fixture, with an always-HOLD expert
and the launcher's neutral leaf, opened zero option positions. The same fixture opened one
long call under BUY/above and one under SELL/below. For the neutral probe the action remains
a fixture-backed buy call to isolate entry eligibility; this is not a full straddle pricing
test. A direct call to the actual entry-staging method also returned False for HOLD before
any evaluator, account or ruleset could be used. Removing the direction leaf (`off`) still
does not admit HOLD at this earlier filter.

The new contrarian tests reach persisted triggers and condition evaluation, but do not run
the full entry loop. In particular, their assertion that mode `off` accepts HOLD describes
the condition tree alone. It does not prove a trade can be opened from that recommendation.

**User clarification, September 20:** the intended neutral approach is low-confidence
BUY/SELL, not HOLD. The launcher already supplies an optimisable `confidence <= threshold`
leaf (10–70). It is ANDed with the separate HOLD-only signal flag. Disabling that flag
allows the intended low-confidence BUY/SELL path; the low-confidence leaf itself works.
Thus this finding does **not** mean neutral structures cannot trade. It means enabling
the extra HOLD flag creates an unreachable subset of the search space.

**Revised recommended fix:** remove the conflicting HOLD requirement from newly generated
neutral recipes, retain the existing low-confidence gate, and give affected experiments
new identities. Do not broaden live/backtest entry eligibility merely to accommodate this
recipe. Saved rules remain unchanged. Add full-engine checks that low-confidence BUY and
SELL can enter, high-confidence signals fail an enabled low-confidence gate, and HOLD
remains ineligible. The review probe now covers these combinations with fixture fills.

### R2 — Medium: driver preflight can pass a pin that its GA trial rejects

**Origin:** `4ce1e6e8`.

[Driver preflight](../../tools/strategy_research/market_conditions.py), lines 130–178, verifies
objects and required feature rows but does not invoke the shared decision-window validation
used by [the engine seam](../../testplatform/backend/app/services/backtest/seam_wiring.py),
`check_market_condition_window`. Thus the two readiness checks can disagree.

**Reproduction using the committed driver's fixture:**

| Check | Result |
|---|---|
| Manifest's declared decision window | 2024-03-25 through 2024-03-27 |
| Stored feature sessions | March 25, 26 and 27 |
| Requested decision window | 2024-03-26 through 2024-03-28 |
| Driver `MC.preflight` | Passes all three required prior-session rows |
| Actual GA trial `check_market_condition_window` | Raises: declared decision window ends too early |

The fixture itself labels feature dates as decision dates at
[test_research10_market_conditions.py](../../testplatform/backend/tests/test_research10_market_conditions.py),
line 147. Its readiness test checks trial construction, which does not execute the engine's
guard. An operator can therefore receive a preflight success, create a research job, and
then have its trials fail on a condition that could have been detected before dispatch.
The runtime guard still refuses the mismatch; this is not evidence of an incorrect fill.

**Recommended fix:** run the shared `window_coverage_problems` check in driver preflight before
dispatch, retain the detailed row/status audit, and correct the fixture's decision bounds.
Add a test that the same accepted manifest passes both driver preflight and the actual
GA trial guard, plus a declared-window mismatch that both refuse.

## Confirmed behavior and operational limitations

The full-engine fixture produced:

| Long-call direction mode | Expert signal | Open option positions | Final cash |
|---|---|---:|---:|
| above | BUY | 1 | $95,600 |
| above | SELL | 0 | $100,000 |
| below | SELL | 1 | $95,600 |
| below | BUY | 0 | $100,000 |
| off | HOLD | 0 | $100,000 |

These fixture amounts establish execution, not performance. The shared direction condition
also rejects ERROR/unknown grades for every comparison operator. Its five-grade behavior is
deliberately broader than the old flags: OVERWEIGHT/UNDERWEIGHT participate on the appropriate
side; BUY/HOLD/SELL defaults match their prior directional meaning.

`--market-condition-mode all-off` disables the new condition genes while preserving the
recipe's existing search space. For example, quality_momentum still searches momentum
lookback, buy threshold, stop offset and holding time. It is suitable for a matched search
comparison; a comparison of one frozen recipe needs those concrete parameters frozen too.
The current CLI does not import a saved winner as a fixed recipe.

The option direction commit deliberately changes the gene-space fingerprint while retaining
job names. In-flight incompatible checkpoints restart, and completed jobs remain skipped by
name. Use a fresh suffix to explore the new direction space in already-completed cells.

## Verification

| Verification on the reviewed code | Result |
|---|---:|
| Research driver suites + market-condition launcher suite | 193 passed |
| Option entry/contrarian, gene-to-artifact, mode-anchor, builders, convex/bull-put and golden parity suites | 196 passed |
| Shared signed-direction condition suite | 27 passed |
| **Total focused pytest checks** | **416 passed** |

Additional full-engine and preflight probes are retained in
[review_followups_20260919.py](../../test_files/strategy_research/review_followups_20260919.py),
with their output in [commit_review_2026-09-19_evidence.json](commit_review_2026-09-19_evidence.json).
Run the probe with the installed test venv from the repository root. No full repository suite,
remote worker dispatch, production account operation or full GA campaign was run for this review.

No fixes for R1/R2 have been applied by this review. These review artifacts were created after
the requested push and are not part of `4ce1e6e8`.
