# Option trade UI: second review

**Superseded status:** [Synced review through `7f2f58a5`](../strategy_research/synced_grid_option_ui_review_2026-09-21.md) confirms the subsequent corrections to N1–N5 and records the remaining sorting and delivery gaps. The findings below describe `80139b93`, not current HEAD.

Date: 2026-09-21. Reviewed corrections through `80139b93`; checkout subsequently advanced to `10136037`. The two intervening commits do not change the UI paths below.

**Verdict: substantially improved, but not yet clear.** The original complete-spread pricing and closing-order payoff bugs are corrected, and the actual browser now keeps the overlay aligned during price-axis dragging. Five remaining defects reproduced with isolated fixtures, including a live Options loader crash. No application code was changed for this review.

## Findings

### N1 — P1: a single-leg option can empty the live Options tab

[`option_trades.py:422`](../../ba2_trade_platform/ui/pages/option_trades.py#L422) marks `_contract_quote(self, account_inst, order)` as `@staticmethod`. The caller at line 359 supplies the two arguments expected of an instance method. On a real `OptionTradesTab` instance this raises `TypeError: OptionTradesTab._contract_quote() missing 1 required positional argument: 'order'`.

The enclosing loader catches the exception and returns `([], 0)`. One open single-leg transaction can therefore make the whole table appear empty. The new loader tests miss this because their `SimpleNamespace` fixture manually binds the function using `MethodType`; lifecycle fixtures also replace the method with a lambda.

**Correction:** make this an ordinary instance method and exercise the actual class through the loader, with single-leg and spread rows together. Assert that one bad row cannot silently erase the entire table.

### N2 — P2: normal backend sessions still cannot resolve the option store

[`backtest_trade_chart.py:146`](../../testplatform/backend/app/services/backtest_trade_chart.py#L146) calls `session.exec(...)` to find the saved optimization. The backtest backend uses a SQLAlchemy `Session`, which has `execute`/`get`, not SQLModel's `exec`.

An actual in-memory SQLAlchemy session with an existing optimization and recorded SQLite store returned unresolved provenance, logging `'Session' object has no attribute 'exec'`. Consequently, the common GA-result path still cannot supply entry/exit contract detail, despite the frontend now rendering those fields correctly. The current unit fixture implements a fake `exec`, masking the mismatch.

**Correction:** use the backend's real ORM API and add a test with its actual session type. Separately, the reader at lines 535–548 still supports only SQLite: a correctly identified parquet/vendor store deliberately returns unavailable. That behavior is honest, but the original multi-store delivery requirement remains incomplete.

### N3 — P2: the totals limit also makes older transactions inaccessible

[`option_trades.py:279`](../../ba2_trade_platform/ui/pages/option_trades.py#L279) counts all matches, fetches only the newest 500, calculates/sorts those rows, then slices the requested page. With 501 matching transactions and 20 per page, the UI advertises 26 pages but page 26 is empty. Computed sorting also excludes every older transaction.

**Correction:** separate bounded totals computation from browse pagination. A totals disclaimer must not silently remove transactions from the table. Verify the last page and sorting beyond the first 500 rows with a query fixture that respects SQL limits.

### N4 — P2: missing fill data silently changes the option structure

[`option_positions.py:217`](../../ba2_trade_platform/core/option_positions.py#L217) drops an executed opening leg if its premium is absent; missing size is handled similarly. Callers build an available payoff from the remaining legs without acting on `excluded`.

Reproduction: a filled 95/105 call spread whose short leg lacks its stored fill premium becomes one long call, with **unlimited maximum profit**. The live pricing dispatcher also sees a single leg. Filtering parents, cancelled orders and closing fills is correct; treating an incompletely recorded executed position the same way is not.

**Correction:** preserve knowledge that the executed structure is incomplete. Mark its payoff and structure valuation unavailable with the missing-field reason instead of pricing a different position. Cover a missing premium and missing executed quantity on either leg.

### N5 — P2: quote reuse is cross-account and does not cover P&L

[`option_trades.py:431`](../../ba2_trade_platform/ui/pages/option_trades.py#L431) keys `_quote_snapshot` only by contract symbol. In the all-accounts view, the second account receives the first account's quote without calling its own quote source. The isolated fixture returned 13.30 for both accounts even though the second account's quote was 20.00. This was tested by bypassing N1's binding error only.

Also, `option_transaction_pnl` at line 353 fetches its own broker quotes, while the cache supplies only the Current column. Thus P&L and Current still do not share one per-refresh observation; spread quotes are not deduplicated through this dictionary either. Worker-thread loading fixes the event-loop block, but not quote consistency.

**Correction:** use a per-refresh, per-account-and-contract quote snapshot for both display and valuation. Keep refresh state isolated if refreshes overlap.

## Status of the original review

| Original item | Recheck |
|---|---|
| R1: spread sent through single-contract pricing | Corrected for complete entry structures; N1/N4 remain. |
| R2: closing/cancelled orders pollute entry payoff | Corrected for complete lifecycle fixtures; N4 remains for incomplete executed fills. |
| R3: historical markers at latest close | Corrected: event-candle anchors and collision offsets. |
| R4: overlay detaches after price-axis drag | **Verified fixed in the real rendered popup.** Both candle canvas and SVG projection change. |
| R5: unordered/overlapping markers | Corrected: chronological order, short labels, separate entry/exit placement. |
| R6: contract provenance and rendering | Rendering and OI mapping corrected; normal ORM lookup broken (N2), non-SQLite reader support unfinished. |
| R7: intraday Greeks use that day's close | Corrected: prior-session observation, explicitly approximate. |
| R8: viewer creates/mutates cache | Corrected: read-only opening and missing-file refusal. |
| R9: totals change by page | Stable totals over the bounded set, but cap now truncates browsing (N3). |
| R10: broker reads block the UI | Reads moved to a worker thread; quote consistency remains incomplete (N5). |

The finishing presentation commit also adds P&L dollar ticks, parent-row chart opening, whole-structure recorded P&L, wider/neutral sign bands, and Escape handling. Full focus trapping/return is still incomplete. Sign bands use the available bars' price range, not every possible manually expanded visible Y range.

The live dialog still lacks the requested ITM/ATM/OTM display, numeric breakeven/max-profit/max-loss summary, and user-facing marker/overlay toggles. Observation timestamp/age and price-adjustment/deliverable compatibility are also still incomplete. These are delivery gaps, separate from the reproducible defects above.

## Validation and evidence

- Live option UI/lifecycle/loader tests: **76 passed**.
- Backend trade-chart context tests: **27 passed**.
- Frontend payoff, overlay, trade and contract-detail tests: **104 passed**.
- Total for this recheck: **207 passed**. These counts are from the corrections at `80139b93`; the subsequent capital-usage and grid-sizing commits were not part of these suites' review scope.
- [Offline probes](../../test_files/option_ui_review_20260920/recheck.py): **all five remaining defects reproduced**. The probe uses the real UI class, mock accounts, and an in-memory SQLAlchemy database. Its assertions document current defects, not desired regression behavior.
- [Browser harness](../../test_files/option_ui_review_20260920/browser.cjs): one intercepted context request, no page errors; sorted markers, price-axis reprojection, Greek/OI values, and P&L tick labels all verified. Earlier harness checks for Greeks and ticks were updated to recognize the actual numeric table and SVG labels.
- [Browser evidence](assets/option-ui-recheck-2026-09-21/evidence.json), [initial popup](assets/option-ui-recheck-2026-09-21/popup-before.png), [after axis drag](assets/option-ui-recheck-2026-09-21/popup-after-axis-drag.png).

This was an isolated implementation review, not a deployed-account trial. No broker calls, orders, production data changes, backtest reruns, or trading/valuation-engine edits were made.

Recommended order: N1 and N2 first, then incomplete-structure handling, pagination and quote consistency; finish the live presentation and multi-store gaps before marking the entire specification delivered.
