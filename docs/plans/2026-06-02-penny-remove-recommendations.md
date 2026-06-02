# Remove ExpertRecommendation from PennyMomentumTrader — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop creating `ExpertRecommendation` records in PennyMomentumTrader, for consistency with the self-contained custom-manager pattern (FactorRanker), without breaking order/expert attribution or the UI.

**Architecture:** PennyMomentum currently creates recommendations in two spots and links the entry order to one via `expert_recommendation_id`. Expert→order attribution actually flows through `Transaction.expert_id`, and `TradingOrder.expert_recommendation_id` is nullable, so the recommendations can be removed — *after verifying* nothing downstream depends on them.

**Tech Stack:** Python, SQLModel, pytest.

---

## Background / current usage

- `modules/experts/PennyMomentumTrader/trade_manager.py:161` — `execute_entry` builds `rec = ExpertRecommendation(...)`, `rec_id = add_instance(rec)`, then sets `expert_recommendation_id=rec_id` on the entry order (`:198`).
- `modules/experts/PennyMomentumTrader/__init__.py:1679` — deep-triage builds an `ExpertRecommendation` (`add_instance(rec)` at `:1697`).
- `TradingOrder.expert_recommendation_id` is nullable (`core/models.py`).

---

## Task 1: Investigate downstream dependence (no code change)

**Do this first; record findings in the commit message of Task 2.**
- `grep -rn "expert_recommendation_id" ba2_trade_platform/` — confirm consumers. Key question: does any **PennyMomentum-facing** UI / P&L / analysis path read the recommendation for display, or only use it as an optional link?
- `grep -rn "ExpertRecommendation" ba2_trade_platform/ui/` — check Live Trades / analysis pages. Confirm penny attribution uses `transaction.expert_id` (it does) and that a null `expert_recommendation_id` renders fine.
- Check the deep-triage recommendation (`__init__.py:1679`) — what consumes it? If it only feeds the analysis state/UI, plan to replace that display with the existing `AnalysisOutput`/state the expert already writes.

**Decision gate:** if any consumer *requires* the recommendation for correctness (not just display), STOP and report — do not remove blindly.

---

## Task 2: Remove the entry recommendation (execute_entry)

**Files:** Modify `modules/experts/PennyMomentumTrader/trade_manager.py` (~`:142-198`); Test `tests/test_penny_exit_staging.py` / a new `tests/test_penny_entry.py` if entry isn't covered.

**Step 1 — adjust/confirm a failing test:** add/extend a test asserting that after `execute_entry`, the created entry `TradingOrder` has `expert_recommendation_id is None` and **no** `ExpertRecommendation` row exists for the instance. Run it — it fails against current code.

**Step 2 — implement:** delete the `ExpertRecommendation` construction + `add_instance(rec)`; set the order's `expert_recommendation_id=None` (or drop the kwarg). Keep all order fields otherwise unchanged. Ensure `Transaction.expert_id` is still set on the resulting transaction (attribution path).

**Step 3 — run, expect PASS;** also run `tests/test_penny_exit_staging.py`, `tests/test_penny_fixes.py` — green.

**Step 4 — commit:** `git commit -m "refactor(penny): stop creating entry ExpertRecommendation"` (include Task 1 findings in the body).

---

## Task 3: Remove the deep-triage recommendation

**Files:** Modify `modules/experts/PennyMomentumTrader/__init__.py` (~`:1679-1697`); Tests as needed.

**Step 1 — test:** if the deep-triage rec only fed analysis display, assert the analysis still records the candidate via the existing `state`/`AnalysisOutput` (add/extend a test) and that no `ExpertRecommendation` is created. Run — fails.

**Step 2 — implement:** remove the rec creation; if any display read the rec, point it at the existing analysis state/output instead (from Task 1 findings).

**Step 3 — run penny tests + full suite** `\.venv\Scripts\python.exe -m pytest -q` — no new failures.

**Step 4 — commit:** `git commit -m "refactor(penny): stop creating deep-triage ExpertRecommendation"`

---

## Task 4: Verify end-to-end + cleanup

- Manual (`/run` or `@verify`): run PennyMomentum on a paper account; confirm entries/exits still place, Live Trades still shows the position with correct expert attribution, and no errors from a null `expert_recommendation_id`.
- `grep -rn "ExpertRecommendation" ba2_trade_platform/modules/experts/PennyMomentumTrader/` — expect no remaining creations (imports may remain only if still referenced; otherwise remove the unused import).
- Bump `version.py` before push.
- **Commit:** `git commit -m "chore(penny): remove unused ExpertRecommendation import"` (if applicable)

## Reminders
- TDD; watch each test fail first.
- Do not remove if Task 1 finds a hard dependency — report instead.
- Attribution must remain intact via `transaction.expert_id`.
