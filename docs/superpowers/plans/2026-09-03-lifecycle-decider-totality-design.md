# M1 design note — `decide()` totality and the `LIFECYCLE_ROLL_SHORT` discard

Branch `options-grid2`, worktree `C:\Users\basti\Documents\dev\BA2-options-grid2`, on top of
`adad3a93`. Investigation only — no production code changed by this commit.

Mandate: the operator's new rule, *"no silent failure allowed anywhere — a computed decision
must be acted on or refused loudly, never dropped"*, applied to the live option lifecycle pass
finding recorded in `2026-09-02-options-grid2-final-review.md` §1b/A1 and §2b.

---

## (a) Every reason `decide()` can return, and who acts on it

`_decide_one` (`packages/common/ba2_common/core/option_lifecycle.py:1044-1136`) is a single
precedence ladder with exactly nine terminal `return`s. The service
(`ba2_trade_platform/core/option_lifecycle_service.py:394-421`) dispatches on
`LIFECYCLE_UNKNOWN`, `LIFECYCLE_COVER_LOST` and `decision.should_close`
(`reason in LIFECYCLE_CLOSING_REASONS`, `option_lifecycle.py:330`).

| # | reason | emitted at | live: acted on where | backtest counterpart (rule → action) |
|---|---|---|---|---|
| 1 | `circuit_breaker` | `:1058-1062` | CLOSING → `_close` (MARKET) `service:421` | **none** — the latch is shared (`update_sleeve_breaker`), the flatten is not (review C1) |
| 2 | `cover_lost` | `:1064-1069` | named branch `service:405-414` (ERROR + `result.cover_lost`) **and** CLOSING → `_close` | **none** (review C2) |
| 3 | `profit_capture` | `:1071-1079` | CLOSING → `_close` | `opt_tp` → `ProfitLossPercentCondition` → `close_option` (same arithmetic, two implementations — C3) |
| 4 | `credit_stop` | `:1081-1097` | CLOSING → `_close` | `opt_sl` / `opt_sl_ml` → `LossPctOfMaxLossCondition` → `close_option` (different partition — C5) |
| 5 | `tested` | `:1099-1105` | CLOSING → `_close` | **none** — no short-leg delta field exists (review B1) |
| 6 | `roll_dte` | `:1120-1122`, **single-expiry branch only** | CLOSING → `_close` | `opt_dte` → `DaysToExpiryCondition` → `close_option` (long leg on a multi-expiry structure — C6) |
| 7 | `roll_short` | `:1113-1119`, **multi-expiry branch only** | **NOWHERE — silently dropped.** Not in `LIFECYCLE_CLOSING_REASONS` (`:135-137`), no branch in the service | `pmcc_roll_dte` / `pmcc_roll_buyback` → `RollPMCCShortAction` — reached by BOTH runtimes (review §2c) |
| 8 | `unknown` | `:1124-1130` | named branch `service:397-404` — WARNING + `result.unknown`, loud by construction | per-condition `_unevaluable` only, no aggregate (C7) |
| 9 | `hold` | `:1132-1136` | no action, correctly | n/a |

Row 7 is the whole finding. Rows 1, 2, 5 are the *other* asymmetries; they are recorded in the
STATE note's follow-up list and are out of this mandate's scope.

## (b) Which live structures can produce `LIFECYCLE_ROLL_SHORT`

**Only a transaction whose `option_strategy` tag is exactly `"pmcc"`.** The branch is gated on
`is_multi_expiry_strategy(structure.strategy)` (`option_lifecycle.py:1112`), which is
membership in `MULTI_EXPIRY_OPTION_STRATEGIES = frozenset({PMCC_STRATEGY})` —
`option_expiry.py:90-114`, fail-closed: `None`, `""` and every unrecognised tag are `False`.

**So the mandate's premise — "computes `LIFECYCLE_ROLL_SHORT` for covered calls" — does not
hold.** A stock-covered call carries `COVERED_CALL_STRATEGY = "covered_call"`
(`OptionsAccountInterface.py:65`), which is single-expiry, so it takes the `elif` at
`option_lifecycle.py:1120` and produces `LIFECYCLE_ROLL_DTE` — a *closing* reason, in
`LIFECYCLE_CLOSING_REASONS`, acted on by `_close`. Nothing is dropped for a covered call, a
wheel, or any other non-PMCC structure. The discard is PMCC-only.

**What the backtest does for those structures today.**

* `O_CC` (`ba2test_launcher.py:4669-4700`): S2's *equity* exit list plus the
  `cc_guard`/`cc_sell` overlay pair. There is **no `close_option` rule at all** — the written
  call rides to expiry/assignment. It is neither rolled nor closed.
* `O_WHEEL` (`:4702-4780`): `O_CSP`'s `_option_exit_rules` (`opt_tp` / `opt_time` / `opt_dte` /
  `opt_sl`) with the same overlay pair front-spliced. Its option legs *do* have DTE/TP/SL
  closes; **neither key rolls the short.**
* `O_PMCC` (`:3971-4007`, `_OVERLAY_ROLL_KINDS = {"O_PMCC"}`): the only key that emits roll
  rules — `pmcc_roll_dte` (`short_leg_days_to_expiry <=`) and `pmcc_roll_buyback`
  (`credit_decayed_pct >=`), both `roll_pmcc_short`, plus the `pmcc_delta_floor` close.

## (c) Fix shapes considered

### Option A — delete the emission (the reviewer's recommendation, §2b step 2)

Drop the `pmcc_roll_due` call at `:1113-1119`, keeping the `is_multi_expiry_strategy` guard so
a PMCC still does **not** fall into `LIFECYCLE_ROLL_DTE` (that would throw a LEAPS away every
month on schedule). A PMCC in its roll window then reports `hold` (or `unknown` if blind), and
the rule is the one and only roller in both runtimes.

*Cost:* `decide()` loses the ability to say "this overlay is due"; the live `roll_dte` setting
and the `pmcc_roll_dte` gene stop being cross-checkable; a PMCC deployed **without** the roll
rule in its ruleset becomes silently unrolled — which is the same class of failure the
operator's rule forbids, moved one layer out.

### Option A′ — keep the computation, make the dispatch TOTAL (recommended)

The service grows a named `LIFECYCLE_ROLL_SHORT` branch that does not roll:

1. record it (`result.roll_due`, a new list on `LifecyclePassResult`) and log it, naming the
   ruleset action that owns the roll — the decision is observed, never dropped;
2. **refuse loudly** (`logger.error`, and the transaction named in a new
   `result.roll_unowned` bucket) when the sleeve's OPEN_POSITIONS ruleset carries **no**
   `roll_pmcc_short` rule — i.e. the roll is due and *nothing in either runtime will do it*;
3. a terminal `else: raise` over the reason ladder, so a future `LIFECYCLE_*` constant cannot
   be added and silently ignored.

No second roller, no live-only roll branch, no gene-space change, and it converts today's
drop into the loudest available signal. Both runtimes still roll through exactly one path:
`TradeActionEvaluator` → `RollPMCCShortAction`.

### Option B — the mandate's preferred shape (a shared `RollShortCallAction` on `O_CC`/`O_WHEEL`)

Assessed and **not** recommended as a fix for this finding, for three independent reasons:

1. **It does not close the finding.** The discarded decision is PMCC-only (§b). Adding roll
   rules to `O_CC`/`O_WHEEL` changes nothing about `option_lifecycle_service.py:394-421`.
2. **It is a new strategy feature, not a parity fix.** `RollPMCCShortAction` refuses any
   transaction that is not a declared two-expiry structure (`TradeActions.py:4985-4990`) and
   selects the next overlay from `data[ORDER_PMCC_OVERLAY_KEY]`, stamped by `open_pmcc`
   (`:4818`). A stock-covered call has neither. Generalising it needs: an overlay-spec stamp
   written by `SellCoveredCallAction`; a **share**-cover input threaded into the
   `uncovered_short_calls` invariant (`option_lifecycle.py:776-807` sees option legs only, so
   it reports every stock-covered call as uncovered); and a decision about what "the cover is
   never released" means when the cover is equity the equity rules may also sell.
3. **It changes stage-1 gene space.** Two new gene families (`roll_dte`, `roll_buyback` +
   `enabled`) on two shipped stage-1 keys, moving the option golden — which the mandate itself
   names as a STOP condition.

## (d) Keys whose emitted ruleset changes

* **Option A / A′: none.** Zero launcher changes; the gene-to-artefact audit and both goldens
  (equity `test_equity_golden_run.py`, option `9a126d13`) stay byte-identical. No
  results-comparability consequence — no grid number moves.
* **Option B: `O_CC` and `O_WHEEL`**, both stage-1 keys, both goldens move, and every existing
  `O_CC`/`O_WHEEL` result becomes non-comparable with post-change runs.

---

## OUTCOME (2026-09-03) — A' chosen, then extended to the roll-DTE close

The operator chose **A'** and, after the second STOP below, the roll-DTE deletion as well.
What landed is recorded in the final review's §10 addendum. Two things belong here, because
they are the parts of this note that turned out to be wrong or incomplete:

**(c) was incomplete.** A' alone leaves a PMCC deployed without a roll rule silently unrolled.
That is why the shipped version does not merely record `ROLL_SHORT` but RAISES
`UnownedRollError` when nothing owns the roll.

**(d) changed once the roll-DTE close moved.** With `decide()` no longer closing a
single-expiry structure at its expiry, `O_CC` and `O_WHEEL` needed an exit of their own — and
`opt_dte` could not be it. THE STANDING RULE this produced, which is the most reusable thing on
this page:

> An option exit condition anchored on the evaluated TRANSACTION is inert for a stock-anchored
> overlay key. Both runtimes evaluate an OPEN_POSITIONS ruleset once per SYMBOL against the
> OLDEST entry order — the stock on `O_CC`/`O_WHEEL` — while the call is written on its own
> transaction. `days_to_expiry` there never fires in EITHER direction while carrying a searched
> gene. Overlay keys must use REPOSITORY-resolved conditions, the shape `has_covered_call`
> already used.

Measured, not argued: an engine run with a plain `opt_dte` rule on `O_CC` was identical to the
run with the rule deleted. So `O_CC`/`O_WHEEL` emit `cc_dte`
(`covered_call_days_to_expiry <= N` -> `close_option(close_target='covered_call')`), both keys
are a new results baseline, and every live instance of them needs re-export/re-import.

---

## STOPPED — question for the operator (answered: A', then the roll-DTE deletion)

The finding as written cannot be fixed by the shape the mandate prefers, and the shape the
mandate prefers is a separate feature with a stage-1 gene-space cost. Awaiting a decision
between **A′** (recommended: totality pin + a named, loud, non-acting `roll_short` branch +
the unowned-roll alarm; no ruleset change, no golden move) and **A** (the reviewer's delete),
with **B** carried as its own piece of work if a rolling covered call is wanted on its merits.
