# Option Selection Modes, Budget-Aware Picking, and Max-Loss Exits — Design

> **Amends** `2026-08-27-option-risk-manager-design.md` (§7 selection policy, §8.2 lane-1
> arithmetic, §9.5 genes). That document is mid-build — P1 shipped, P2 in progress — so this
> is an amendment rather than a rewrite: renumbering a spec that in-flight work is being
> reviewed against costs more than it buys.

**Goal:** four additions to option support, requested 2026-08-29. Two of them turned out to
already exist and are recorded here as verified rather than built.

---

## 1. The four items, and what each turned out to be

| # | asked for | finding |
|---|---|---|
| 1 | two modes for picking the best option — **profit** and **risk/reward** | **new.** Selection scores five features today; neither max profit nor the profit/loss ratio is among them, and `max_profit()` does not exist. |
| 2 | rules issue an intent with a **range**, the manager picks the best inside it | **already exactly that.** `PolicyContext.box_min/box_max` is the range; `SelectionPolicy.pick()` chooses inside. No work. |
| 3 | open-positions **early exit** for contracts that support it, e.g. a credit spread | **new.** Credit structures already exit at a % of *credit collected* (`opt_sl`). A % of *max loss* is a different, scale-free quantity and does not exist. |
| 4 | **total risk finetunable**; the RM must not pick a contract above it | **half true.** Six tunable rails exist and §8.2 already sizes against a budget — but it sizes *down and then refuses*. It never re-picks a cheaper contract that would fit. |

Items 1, 3 and 4 all reduce to one primitive: the structure's **max loss** and **max profit**
as measured from the payoff curve. That is why they are one design and not three.

---

## 2. `max_profit()` — the missing mirror

`option_payoff.max_loss()` returns a tri-state `MaxLossResult`: `MEASURED(amount)`,
`UNBOUNDED`, or `UNMEASURABLE(reason)`. `max_profit()` is its mirror and returns the same
shape:

* the same `critical_points` scan, taking `max` where `max_loss` takes `min`;
* `UNBOUNDED` when `upside_slope(legs) > +_SLOPE_EPSILON` — a long call's profit has no
  ceiling, exactly as a naked short call's loss has no floor;
* `UNMEASURABLE` on the same `validate_legs` failure, for the same reason.

**The guard order is the mirror image of `max_loss`'s and is equally non-negotiable.** In
`max_loss` the slope test must precede the arbitrage test or every naked short call reports
`UNMEASURABLE`. Here the slope test must precede a "structure cannot profit at any price"
test, or an ordinary long call — non-positive payoff across `[0, K_max]`, unbounded above it
— is reported as having no profitable outcome.

---

## 3. Two new selection features (§7 amendment)

`SelectionPolicy` gains two weights, keeping the existing shape:

```python
@dataclass(frozen=True)
class SelectionPolicy:
    w_box_center: float = 1.0     # PINNED reference weight, not a gene
    w_premium:    float = 0.0
    w_iv:         float = 0.0
    w_rvol:       float = 0.0
    w_spread:     float = 0.0
    w_profit:     float = 0.0     # NEW
    w_rr:         float = 0.0     # NEW
```

| feature | definition | higher is |
|---|---|---|
| `profit` | `max_profit` of one structure unit, min-max normalised across the candidate set | more upside |
| `rr` | `max_profit / max_loss`, normalised the same way | better reward per unit of risk |

Both are normalised **within the candidate set**, for the reason the other five are: an
absolute dollar threshold is meaningless across a \$15 stock and a \$900 one, while a rank
among the peers on the chain in front of you is scale-free.

**The default stays a provable no-op.** Both default to `0.0`, so `is_default` and the
recorded-chain no-op test are unchanged.

---

## 4. Inapplicable is not missing

The two new features are only *defined* for structures bounded on the side they read:

| structure | `max_profit` | `max_loss` | `w_profit` | `w_rr` |
|---|---|---|---|---|
| credit spread, iron condor, butterfly | bounded | bounded | applies | applies |
| debit vertical | bounded | bounded (debit) | applies | applies |
| long call, long strangle | **UNBOUNDED** | bounded (debit) | inapplicable | inapplicable |
| naked short put | bounded (credit) | **UNBOUNDED** | applies | inapplicable |

§7 says a feature no candidate publishes is a configuration error naming the weight. **That
rule is right for a missing field and wrong for this.** A long call whose every candidate
reports `UNBOUNDED` profit is not misconfigured; it is a payoff shape the feature cannot
describe. Raising there would make a perfectly valid arm crash on a weight it should simply
ignore.

So the two cases are separated, and the distinction is reported rather than silent:

* **`UNBOUNDED` — inapplicable.** The weight is inert for that structure and the fact is
  recorded, the way `RailVerdict.evaluated` records that `undefined_risk_max_pct` is
  genuinely dead for a debit arm rather than passing it in silence.
* **`MEASURED` but absent on a candidate — missing.** Scores worst, failing closed, exactly
  as §7 and `passes_liquidity` already require.
* **No candidate publishes it, on a structure where it *is* applicable** — still the §7
  configuration error. Unchanged.

Without this split, `w_profit` on every debit directional member is a **dead gene**: GA
budget spent searching a dimension that cannot move fitness. This codebase has paid for that
before — the dead roll gene, and the trial-config whitelist silently dropping new knobs.

---

## 5. The budget ceiling reaches the picker (§8.2 amendment)

§8.2 computes a budget and divides:

```
raw_budget = min(instrument_left, book_left, structure_cap)
contracts  = floor(raw_budget × confidence/100 / max_loss_per_contract)
```

When one contract already exceeds the budget this yields `contracts = 0` and refuses. It
sizes down, then gives up — it never asks whether a *cheaper strike in the same box* would
have fitted.

`_resolve()` therefore gains an optional `max_loss_ceiling`, applied as a candidate filter
**before** scoring, beside `passes_liquidity`:

```
candidates = [c for c in candidates
              if max_loss_per_contract(c) <= max_loss_ceiling]
```

**The ceiling is `min(instrument_left, structure_cap)`, not the full `raw_budget`.** Those two
terms are knowable before triage runs. `book_left` is not — it depends on which other
structures this bar's greedy triage admits first — so it stays where it is, sizing and
refusing as today. Threading a number that does not exist yet into selection would be a
fiction.

Three properties:

* **Per contract, not per structure.** Sizing still decides how many; this only guarantees
  at least one fits.
* **`ceiling = None` is a provable no-op** — no filter, byte-identical selection. This is what
  lets it ship without moving a backtest.
* **Nothing fits is a refusal with a reason, never a silent zero** (§9):
  `BUDGET_CEILING_REFUSAL`, naming the cheapest candidate's max loss and the ceiling it
  exceeded. "The sleeve stopped trading" must be diagnosable.

`UNBOUNDED` max loss needs no new rule: §8.3 already refuses those unless
`allow_undefined_risk_options` is on, in which case they are charged `spot × 100` and filter
against that.

**Consequence, stated because it is the point rather than a side effect:** the same rule on
the same chain can now pick a different strike depending on remaining budget — a cheaper,
further-OTM contract late in a bar when the book is nearly full. It lands in **P3, gated on
`classic_options`**, so the PremiumSeller path and every backtest already run are untouched.

---

## 6. Exit at a % of max loss

§8.2 already has the RM persisting `max_loss_per_contract` onto the parent order's `data` at
submit, mirroring `option_reserve`. The exit evaluator reads it back — **no leg
reconstruction, no OCC parsing.** That is what makes this cheap.

New condition field `loss_pct_of_max_loss` = unrealized loss ÷ persisted max loss × 100.
`_option_exit_rules` gains one rule, emitted **only for defined-risk members**:

```python
{"id": "opt_sl_ml", "action_type": "close_option", "toggle_optimize": True,
 "conditions": {"type": "AND", "conditions": [
     {"id": "sl_ml", "field": "loss_pct_of_max_loss", "op": ">",
      "value": 50, "optimize": True,
      "value_min": 25, "value_max": 75, "value_step": 5}]}}
```

**A separate rule, not a "basis" gene on `opt_sl`.** The basis-gene version was tried on paper
and abandoned: the sensible threshold range differs by basis (−200..−50 % of credit versus
25..75 % of max loss), so one threshold gene would need a domain conditional on another gene.
Two independently toggleable rules avoid that entirely, and the GA still selects the basis —
by toggling which rule is live, which is how `opt_tp`/`opt_time`/`opt_dte`/`opt_sl` already
express on/off.

**Per structure, defined-risk only.** This is "contracts that support it", enforced
structurally rather than by a runtime check: a naked short's `max_loss` is `UNBOUNDED`, so
there is no denominator, so the rule is never emitted for it. §4 of the grid design already
establishes per-structure condition tiers; no new mechanism.

Both stops may be live at once — first match wins, as the OPEN_POSITIONS ruleset already
does. They are correlated but not redundant: 50 % of max loss is scale-free, always half the
defined risk, while −100 % of credit drifts with however much credit that trial collected.

---

## 7. Genes and stage-2 wiring (§9.5 amendment)

**None of the policy weights are GA-wired today.** §9.5 specifies four of them as genes, but
that is P5, unbuilt. `SelectionPolicy` currently sits at its no-op default, searched by
nothing. "GA-tuned in stage 2" therefore applies to all seven weights, not just the new two.

| gene | domain | note |
|---|---|---|
| `w_premium` | **−2.0 – 2.0** | **sign fix, see §8** |
| `w_iv` | −2.0 – 2.0 | unchanged |
| `w_rvol` | 0.0 – 2.0 | unchanged |
| `w_spread` | 0.0 – 2.0 | unchanged |
| `w_profit` | 0.0 – 2.0 | new; unsigned — more upside has no legitimate "less is better" |
| `w_rr` | 0.0 – 2.0 | new; unsigned — same |
| `w_box_center` | pinned 1.0 | not a gene (§7) |

**Sharing tier.** Naive per-structure wiring is 7 × 18 = 126 new genes on a stage-2 job
already carrying ~300. Instead:

* **The five general weights share per debit/credit half** — 10 genes, not 90. This reuses the
  grid design's own logic (share on semantics, not convenience) and the launcher's existing
  `_DEBIT_OPTION_MEMBERS` / `_CREDIT_OPTION_MEMBERS` partition, which is already asserted
  total. That line is precisely where "which contract in the box" flips direction.
* **`w_profit` and `w_rr` are emitted only where the payoff is bounded on the side they read**,
  shared across that set. This deliberately does **not** follow the debit/credit split: a
  debit vertical is bounded both ways, a long call neither, a naked short put on profit only.
  Applicability is a property of the payoff shape, determined per structure from
  `max_profit()`/`max_loss()` returning `MEASURED` — never assumed from the half.

---

## 8. The `w_premium` sign fix

§9.5 gives `w_premium` the domain `0.0 – 2.0`, on the stated grounds that premium richness has
an unambiguous good direction. **It does not.** A premium *seller* wants rich premium; a
*buyer* wants cheap. That is the identical asymmetry that made `w_iv` the one signed weight:

> "selling structures want rich vol and buying structures want cheap vol, and which is right is
> exactly the sort of question the GA should settle rather than inherit."

The same sentence is true with "premium" substituted for "vol". Unsigned, a debit member can
only ever express "prefer richer" — never "prefer cheaper" — so the gene is half dead across
the entire debit half.

The domain becomes **−2.0 – 2.0**. `w_iv` is no longer "the one signed weight"; the signed
weights are those whose good direction depends on whether the structure is long or short
premium, which is `w_premium` and `w_iv`. `w_rvol` and `w_spread` stay unsigned — nobody wants
an illiquid contract with a wide quote.

---

## 9. Testing

Beyond the unit tests each piece carries:

* **`max_profit` mirrors `max_loss` on the shapes that pin the guard order** — a long call
  (`UNBOUNDED`, and *not* "cannot profit"), a naked short call, a 1×2 ratio, a credit spread
  whose credit equals its width.
* **The no-op is still provable.** The recorded-chain test that pins `pick()` to
  `_pick_by`'s selection must pass unchanged with the two new weights present and zero.
* **`ceiling = None` is byte-identical** to today's selection on a recorded chain.
* **The dead-gene guard, per member:** every emitted weight must demonstrably change the
  contract picked on a recorded chain. A weight that cannot move the pick fails the test
  instead of quietly costing GA budget. This is the direct defence against §4's failure mode
  and the reason the applicability rule exists at all.
* **`opt_sl_ml` is never emitted for a member whose `max_loss` is `UNBOUNDED`** — asserted over
  all 18 members, not spot-checked.

---

## 10. Phasing

Slots into the existing P1–P5 without reordering it:

| phase | added here |
|---|---|
| P1 | `max_profit()`; the two new `SelectionPolicy` weights; the applicability split |
| P3 | `max_loss_ceiling` in `_resolve()`; `BUDGET_CEILING_REFUSAL` |
| P5 | all seven weights as genes; the sharing tier; `w_premium` sign fix; `opt_sl_ml` in `_option_exit_rules` |

P1 and P5 additions are behaviour-neutral until the genes are searched. The P3 addition is
gated on `classic_options`, like the rest of P3.

---

## 11. Out of scope

* **Re-picking against `book_left`.** Only the knowable ceiling reaches selection (§5). Making
  the full budget available would require ranking structures before contracts exist — a
  data-flow change disproportionate to the gain.
* **A share-capacity notion for short calls.** `option_book` does not track share inventory;
  unchanged here.
* **Rebalancing an existing structure to a cheaper strike.** This is entry selection only.
