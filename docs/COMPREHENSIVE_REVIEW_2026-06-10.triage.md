# Triage — reconciling the canonical review with adversarial verification

**Date:** 2026-06-10 · **Branch:** `review-fixes-2026-06-10`
**Inputs:**
- `docs/COMPREHENSIVE_REVIEW_2026-06-10.md` — canonical (from git), IDs `PR-/RM-/EX-/CQ-`. Severity scheme: **P1=bug, P2=prompt/strategy, P3=quality**.
- `docs/COMPREHENSIVE_REVIEW_2026-06-10.claude-local.md` — my 20-agent verification (adversarial verify + discovery + hand/empirical re-checks).

This file records, per canonical ID, the **verification verdict** and the **action taken**. The canonical doc is the spec; the corrections below override it where verification disproved a claim.

## Corrections the verification makes to the canonical doc

| Canonical claim | Correction (from verification) | Effect on implementation |
|---|---|---|
| PR-1 / EX-6: weak signal prompt → `processed_signal` → `ERROR` recommendations | The `else` fallback in `_extract_recommendation_data` (`TradingAgents.py:504‑528`) is **unreachable** — the only caller (`:651`) runs after the guard at `:646‑648` raises when `expert_recommendation` is empty. The live signal is `expert_recommendation['recommended_action']` (`:496`). | Still delete the duplicate prompts (reflection prompt **is** live + weakened) and remove/raise the dead fallback. The "ERROR rows" symptom is latent, not active. |
| RM-2: "remove tuple-unpacking — `session.exec(select(Model))` returns model instances" | **True for `TradeRiskManagement.py`** (imports `sqlmodel.select`, line 16 → models). **False for `core/db.py`** (imports `sqlalchemy.select`, line 5 → `Row`, so `i[0]` is required and correct). | Apply the simplification only in TradeRiskManagement; **do not** touch `db.py:494` (verified working by running `get_all_instances`). |
| RM-4: WAITING counted as \$0 → systematic over-allocation | WAITING txns are normally created **with** `quantity`+`open_price` (`AccountInterface.py:316‑318`), so `abs(qty*open_price)` is used; `=0` is only an edge-case fallback. | Still implement the defensive estimate (qty*open_price/current_price), but it closes an **edge case**, not a systematic leak. |
| CQ-6: "none of the decision math is tested" | `consensus_from_counts` (`tests/test_finnhub_rating.py`, 9 tests) and `FMPRating._calculate_recommendation` (`tests/test_experts/test_fmp_rating.py`) **are** tested. | The real gap is **allocation/position sizing** (`_calculate_order_quantities`, `calculate_position_sizes`, Senate confidence). Focus new tests there + the EX-1 SELL-boost regression. |
| RM-8: "~476 `exc_info=True`, many outside except" | Actual: **791** total, **770 inside except**, **21 outside** (2.7%), all `logger.error` in validation/safety branches. | Fix exactly those 21 (use `stack_info=True` where a stack is useful). |
| RM-7: `RESEARCH_PROMPT` "Direction Policy" section doesn't exist | The `## DIRECTION POLICY` block **is** injected via `{expert_instructions}` (`SmartRiskManagerGraph.py:2894‑2901`) when `enable_buy` or `enable_sell` is False (true under defaults, `enable_sell=False`). Dangles only if both are enabled. | Naming-consistency fix only; always emit a header so refs never dangle. |
| EX-3: missing types "trigger needless fix-prompt round trips" | `validate_condition` checks the **full** registry (`conditions.py:217`), so valid-but-unlisted types **pass** — no retry. | Real issues are the **`time_before` vs `time_after` EOD contradiction** and **under-use** of exit signals. Fix both. |
| (discovery) `get_all_instances` TypeError | **False positive** — correct for `sqlalchemy.select` (returns `Row`). Verified by running it. | No change. |

## Per-ID disposition (all are IN SCOPE — user approved "everything")

**Prompts:** PR-1 ✅confirmed(P3 impact) · PR-2 ✅confirmed · PR-3 ✅ · PR-4 ✅ · PR-5 ✅ · PR-6 ✅ (note: `n_matches` is already ignored in `memory.get_memories`, still de-hardcode) · PR-7 ✅ · PR-8 ✅
**Risk mgrs:** RM-1 ✅ · RM-2 ✅(per-file) · RM-3 ✅ · RM-4 ✅(edge case) · RM-5 ✅ · RM-6 ✅ · RM-7 ✅(split + naming) · RM-8 ✅(21 sites)
**Experts:** EX-1 ✅(top correctness) · EX-2 ✅ · EX-3 ✅(EOD semantics) · EX-4 ✅(split) · EX-5 ✅ · EX-6 ✅(dead-path→FAILED)
**Quality:** CQ-1 ✅ · CQ-2 ✅ · CQ-3 ✅ · CQ-4 ✅ · CQ-5 ✅ · CQ-6 ✅(sizing focus)

**Execution order** follows the canonical doc's session table (tests before EX-1/RM-3/RM-4). Baseline before changes: **723 passed**.
