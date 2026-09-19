# Market conditions review: implementation and option stage 1

Reviewed 2026-09-16 at `23e457aad86f8743c8ca955ca65eb7079c58be65`.

**Conclusion:** the condition calculators, shared readers and option stage-1 gene wiring are substantially implemented and tested. Both profiles reach all 16 discovery structures, including their deployed rule representation. However, snapshot preflight can accept the wrong date range, the matrix driver can silently discard a manifest-only argument, and live manifest configuration cannot accommodate experts with different profile sets on the same host. These should be corrected before relying on the corresponding workflows.

This pass changed documentation only. No strategy, calculator, stored backtest, production setting or database was changed; no optimization or provider warmup was launched. Stage 2 was deliberately deferred. The equity follow-up integration is specified in [goal2020_followups.md](../../docs/strategy_research/goal2020_followups.md); it is not implemented by this review.

**Follow-up status (2026-09-17):** F1 (session-window validation), F2 (manifest-only
argument refusal) and F3 (per-expert host-manifest selection) are now fixed in the current
branch (`16eae91e`, `fb7437d7` and `4dd86f94`). The opt-in equity follow-up adapter and its
cache-only preflight/export driver are implemented; the launch contract and
verification plan are recorded in [goal2020_followups.md](../../docs/strategy_research/goal2020_followups.md).
The original findings below remain the historical evidence from the 2026-09-16 review.

## Findings

### F1 — High: snapshot coverage checks do not validate the requested sessions

Sources: [launcher](../../testplatform/ba2test_launcher.py), `_apply_market_conditions`, lines 4923–4932; [backtest seam](../../testplatform/backend/app/services/backtest/seam_wiring.py), `check_market_condition_coverage`, line 484; [reader](../../packages/common/ba2_common/core/market_condition_reader.py), `missing_coverage`, line 850.

The launcher passes only `enabled_instruments` and `_ga_trial` to the coverage check. The check compares symbol names with `mapped.symbols()`. It does not compare the requested dates or required prior-session rows. Manifest integrity and worker preparation prove that the published objects can be read, not that they cover this experiment.

**Reproduced with real temporary feature objects and the actual launcher preflight:**

```text
snapshot window:       2024-03-01 .. 2024-03-29
requested BT window:   2025-01-02 .. 2025-12-31
symbol:                AAA, present in both
coverage result:       []  (accepted)
_apply_market_conditions: accepted and wrote profile/pin onto the run
observe(AAA, 2025-01-02): None
```

The fixture was `packages/common/tests/test_market_condition_reader.py::_fabricate`; the launch used an O_LC template, an FMPRating expert spec and the temporary manifest. No backtest was submitted. `MarketConditionCompare.evaluate` turns an absent row into `missing_session` and returns false. Consequently an active gate can suppress entries because the cache is wrong, and the GA can score that suppression as strategy behavior. Internal date holes have the same unchecked path. An explicit negative row can also pass the symbol-presence check.

**Fix:** validate the actual required `(symbol, prior_regular_session(decision))` rows for each selected profile, once before dispatch; carry start/end and the relevant calendar into this check. Cache the validation by digest, universe and decision window. Distinguish legitimate pre-listing/initial-history and undefined-structure observations from unapproved missing data; do not require every structure field to be numerically valid. Refuse unexplained holes or an out-of-range pin as a job configuration error, not a zero-trade fitness. Add wrong-window, internal-hole and legitimate-negative-row regression cases.

The current published manifests' metadata spans the intended 2020–2025 decision window. This finding demonstrates a missing guard, not evidence that those particular runs used a wrong-window snapshot.

### F2 — Medium: the stage-1 matrix silently drops a manifest when the profile is omitted

Source: [run_options_matrix.py](../../tools/run_options_matrix.py), `_market_condition_passthrough`, line 140.

With `--market-condition-manifest abc123` and the default profile `none`, this helper returns `[]`. The launcher therefore never sees the manifest and cannot apply its explicit manifest-without-profile refusal.

**Reproduced through `resolve_args`, `build_cmd` and `discovery_name`:** no condition arguments reach the child command, and its job/checkpoint name is identical to the ordinary ungated job. It can run ungated or be skipped against an existing ungated completion. Supplying both arguments correctly does work.

**Fix:** reject a nonempty manifest when the profile is off, before command construction and name generation, including dry-run. Apply the same rule to the stage-1 shell wrapper's environment inputs. Add a driver-level test; the existing launcher refusal test does not cover the driver dropping the argument.

### F3 — Medium: host-wide live pins conflict with per-expert profile selection

Source: [market_condition_live.py](../../packages/common/ba2_common/core/market_condition_live.py), `manifest_digests_from_env`, line 188, called from `PerInstanceMarketConditionResolver._build`.

Profiles are correctly selected per expert, but `BA2_MARKET_CONDITION_MANIFEST` is a process-wide map. The parser rejects any map entry not used by the particular expert being resolved.

**Reproduced without touching live services:**

```text
host map: ohlcv-v1=ohlcv-digest,ta-structure-v1=structure-digest
expert A profiles: ohlcv-v1                  -> ValueError (extra structure pin)
expert B profiles: ta-structure-v1           -> ValueError (extra OHLCV pin)
expert C profiles: ohlcv-v1,ta-structure-v1  -> accepted
```

Thus two independently valid, pinned strategies using different profiles cannot currently coexist under that host configuration. Once source certification succeeds, resolver construction raises before the affected entry pass. This does not demonstrate incorrect fills or changed backtest results, and exits are outside that entry scope.

**Fix:** validate the host map against the registered profile names, then select the subset needed by each expert; retain rejection of unknown names, empty or duplicate pins and incompatible manifests. Alternatively make manifest pins explicitly per instance. Test A, B, C and an ungated expert together. Removing pins and accepting live recomputation is not a reproducible substitute.

### F4 — Medium: the runbook described obsolete live activation and overstated coverage

Source: [RUNBOOK-goal2020-grid.md](../../docs/RUNBOOK-goal2020-grid.md), market-condition feature-store section.

Three operational claims had drifted from the implementation:

- It instructed users to set `BA2_MARKET_CONDITION_PROFILE`; `assert_profile_env_retired` explicitly rejects a nonempty value. The active control is the expert's `market_condition_profile` setting.
- It promised an automatic full FMP history repair on first live use. Current `_report_split_basis_drift` reports the issue; replacement is an explicit `force_full_refetch`/warmup action. This reversal was an operator decision, not a missing automatic-repair feature.
- Its snapshot table claimed zero coverage exceptions. The actual manifests contain negative observations, detailed below. Symbol presence is not the same as valid feature coverage.

**Status:** corrected in the runbook during this documentation pass. The live mixed-profile limitation and the session-validation gap remain open code findings.

## What stage 1 does correctly

I constructed every permitted structure with each profile selection: **64 template combinations**. The results were:

| Selection | Entry leaves added per structure | Additional genes | Exit leaves added |
|---|---:|---:|---:|
| `none` | 0 | 0 | 0 |
| `ohlcv-v1` | 3 | 6 | 0 |
| `ta-structure-v1` | 5 | 9 | 0 |
| Both | 8 | 15 | 0 |

The 16 structures are O_LC, O_LP, O_VERT, O_BULLCS, O_BULLPS, O_BEARCS, O_BF, O_IC, O_JL, O_RS, O_CSP, O_STRD, O_STRG, O_CC, O_PP and O_WHEEL.

- Numeric fields have `off/below/above` plus a threshold gene. Swing structure has `off/bull/bear`, without a numeric threshold. Twelve structure fields are calculated, but only five are searched by this profile.
- Pure options gate initial entries. Covered calls/protective puts gate the initial stock entry; subsequent protection and management remain ungated. Wheel IDs are renamed correctly and its initial short-put entry is gated.
- The driver explicitly forwards profile and manifest pairs when configured correctly. Discovery names change with profile/digest, avoiding collision with ungated jobs. `tools/stage1_run.sh` includes plan/build/verify/prepare-host and forwards the resulting pins.
- The launcher persists both profiles, each manifest, calculator/source/timing metadata and the expert setting. Worker preparation, cache sync verification and trial configuration retain the pins. Missing or corrupt snapshots are refused.
- Mode decoding removes inactive leaves, resolves active operators and strips template-only mode metadata before deployment. Inactive numeric thresholds share a memo key without changing the persisted raw genome.
- Tests confirm profile-off/all-off compatibility and that an always-failing gate actually blocks entries. Deployment and UI guards reject unsupported fields and market gates on exit rules.

**Activation remains opt-in.** Stage 1 does not search market conditions unless a profile is selected. This preserves existing experiments. The budget also remains population 200 / 60 generations under the discovery defaults; adding 6, 9 or 15 genes does not increase that budget automatically. This review did not launch the remote shell wrapper or a full 32-job grid.

## Calculator, cache and live/backtest quality

The shared implementation has useful correctness boundaries: deterministic 128-session input windows, explicit invalid observations, prior-completed-session timing, confirmed pivots, and exact batch/reference tests. No calculator defect or current-session lookahead was found in the inspected paths. The backtest's close-time context metadata does not change the feature session: both live and BT read the prior regular session.

Warmup accepts **decision dates** and shifts the required feature dates itself. A 2020-01-02 decision correctly reads the 2019-12-31 feature row. Raw prehistory must cover the 128-session window as well. There is no need to subtract that prefix manually from the CLI's decision start.

The central store publishes immutable, content-addressed objects and manifests, and the host reader uses shared mapped arrays. GA trials read published values rather than recomputing indicators or downloading history. Unpinned live operation is still possible: it computes from the local FMP cache and logs an error per decision pass. That is a distinct operating mode, not proof of identical inputs to a pinned BT. Capture/replay is the appropriate evidence for a particular live decision.

The chart fields also have strategy limitations, rather than implementation bugs:

- They describe the traded underlying, not a broad-market/SPY regime or option IV term structure.
- An absent confirmed resistance/support level is unknown, not infinite room to run. Either comparison direction then rejects entry. A resistance-distance gate can therefore remove some breakouts; inspect the rejected winners.
- A gate only filters recommendations the expert already produced. It cannot turn HOLD/SKIP into an eligible opportunity or create a new range-trading signal.
- A single comparison cannot represent an arbitrary bounded interval. More genes do not establish better out-of-sample performance.

### Published local snapshot observations

Read directly from the two manifests under `C:/Users/basti/Documents/ba2/common/cache/market_conditions`, and checked representative raw-cache dates and a negative SPCX feature row. Object integrity was exercised on fixtures by the tests; I did not re-hash the entire production cache or certify remote hosts during this pass.

| Profile | Manifest | Stored symbol/session rows | Symbols with recorded exceptions |
|---|---|---:|---:|
| `ohlcv-v1` | `c9ba981fbae8726ec749eca4201c98399a4285046733d16cca6b112b8c8371df` | 147,784 | 6 |
| `ta-structure-v1` | `3c3020d05f9e24ded59272050e1f06193abc74e6c00e41e3168a1750bcc44385` | 147,784 | 98 |

Both contain 98 symbols × 1,508 rows, with feature dates 2019-12-31 through 2025-12-30 and decision dates 2020-01-02 through 2025-12-31.

For **each** basic OHLCV field, 5,307 rows are `missing_session` and 635 are `insufficient_history`; do not add the three field counts as if they were separate sessions. APP, ARM, GEV, PLTR and SNDK have data starting part-way through the window, followed by 127 initial-history rows. SPCX has 1,508 missing rows: its current daily cache begins 2026-06-12. This establishes the cache contents, not an independent certification of listing dates or an assertion that historical trades were lost. SPCX cannot supply a valid condition observation anywhere in this research window.

Structure adds legitimate undefined-level/state-history observations. For example, resistance distance has 15,494 `insufficient_history` rows plus the same 5,307 missing rows; support distance has 5,395 plus 5,307. An undefined pivot level is not repaired by repeatedly downloading history. Report usable coverage by field and eligible recommendation, not merely “98/98 warmed.”

## Validation and next steps

Focused verification on the reviewed code:

| Suite | Result |
|---|---:|
| Common calculators, batch/reference, store, readers, conditions and profile settings | 320 passed, 6 skipped |
| Provider warmup and split certification | 29 passed |
| Backend market-condition launcher, matrix, worker, sync, deployment, replay and BT tests | 342 passed |
| Mode decoding and optimizer mode-anchor tests | 39 passed |
| Live context, live coverage and UI guards | 69 passed |
| **Total** | **799 passed, 6 skipped** |

The skips include optional TA-Lib reference comparisons; this is not a claim that every independent-reference test ran. The original combined package invocation encountered same-name `tests.conftest` imports; running common and providers separately resolved collection. The additional template matrix and three bug reproductions above are separate from the pytest count. No full repository suite or full GA performance benchmark was run. Earlier timing evidence remains in [the benchmark report](market_conditions_bench_2026-09-16.md); its original 85-symbol snapshot is historical and superseded.

Recommended order:

1. Keep the F1/F2/F3 launch and resolver regressions in the blocking gate; leave all calculators and existing backtest results unchanged.
2. Use explicit frozen ungated controls and matched seeds. Compare profit, CAR, drawdown, trades, capital usage and top-five winner concentration, plus missing-data versus measured-condition rejections. Do not infer improvement from fewer trades alone.
3. Run the opt-in equity follow-up driver only after each pinned profile passes warmup, verification and host preparation. Stage 2 remains a separate review.
