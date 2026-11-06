# Structure Share Condition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an opt-in rule condition `structure_share` that lets the GA explore UNEVEN capital shares between the members of one option style expert: a member's entry rule passes only while `committed(member) / sleeve_equity * 100 < X`, so a false condition lets the next member's rule be tried. First user: job C, style `long`, `{"min": 20, "max": 60, "step": 10}`. Design: `docs/plans/2026-11-06-structure-share-condition-design.md`.

**Architecture:**
- ONE shared implementation in `packages/common/ba2_common` used by live and backtest (BT == live, no fork): `StructureShareCondition` (`TradeConditions.py`) asks `OptionRiskManagement.member_share(...)`, which keeps a per-cycle `{entry_tag: committed}` cache beside the existing per-sleeve process state (`_PENDING`), filed under the same `_sleeve_key`, cleared by the same resets, incremented by `record_submitted`.
- The condition learns its member from its OWN rule's entry action: `TradeActionEvaluator._evaluate_conditions` calls an optional `bind_event_action(event_action)` hook that only `StructureShareCondition` defines (every other condition: one `getattr` returning None, nothing else happens).
- The member's committed capital = open option structures tagged with it (`option_book._structure_totals`, same measure as `max_deployment_pct`) + assigned-stock cost basis (`Transaction.open_price * quantity` of `meta_data.origin == "csp_assignment"` lots, tag read off the lot's entry order) + this cycle's not-yet-visible submissions (`_PendingCharge.candidate.max_loss`). Covered calls add 0 (the lot's cost basis already is their capital; see Findings).
- Opt-in per style (`structure_share` style field): without it NOTHING changes (no leaf, no genes, no cache, no state) -- proven by golden tests (Task 0 captures them BEFORE any code changes).
- GA-neutral release (owner, 2026-11-06): ba2_common + TEST_APP_VERSION bumps, ba2_common minimum in `required_package_versions.py` NOT raised.

**Tech Stack:** Python 3.11, pytest, SQLModel in-mem trade store (`trade_store.inmem_trades`), NiceGUI untouched. Shared code in `packages/common/ba2_common`; builder in `testplatform/ba2test_launcher.py`.

---

## Findings (verified against the code at commit 2da4be26; these decide the tasks below)

1. **Covered call carries the wheel's tag: YES, no propagation task.** `TradeActionEvaluator._create_action` (`packages/common/ba2_common/core/TradeActionEvaluator.py:1456-1465`): for `_OVERLAY_ACTION_TYPES` (`SELL_COVERED_CALL`, `BUY_PROTECTIVE_PUT`, line 44) with no authored tag it sets `kwargs['entry_tag'] = order_entry_tag(existing_order)` (the stock lot's entry order, which carries the put's tag via `assigned_stock_entry_data`, `assignment_hold.py:123`). `SellCoveredCallAction` is an `_OptionEntryAction` (`TradeActions.py:4683`) so `_submit_option_order` stamps it on the call's parent order row (`TradeActions.py:4056-4057`). A test in Task 3 pins the wheel's share through put -> assignment -> covered call. BUT the call is its own transaction with `option_strategy == "covered_call"` (`TradeActions.py:4811`) and its book committed is the FULL short-call notional (`option_lifecycle.structure_metrics`, naked branch, `option_lifecycle.py:~456-465`), i.e. roughly the stock's cost. Counting both would show a 10% wheel as 20%. So covered calls (book structures AND pending charges with strategy `covered_call`) contribute 0; the lot's cost basis is the capital. This deliberately refines the design table row "covered call -> `_structure_totals`".
2. **O_CONVEX goes through `_option_entry_rule`: YES.** `O_CONVEX` is a GROUP key: `_STRATEGY_BUILDERS["O_CONVEX"]` is `_build_strategy_option_group` (`ba2test_launcher.py:7374-7390`), which builds one `_option_entry_rule(m, toggleable=True)` per arm (`O_CONVEXC`, `O_CONVEXP`, `_OPTION_GROUPS` at ~3908). The style builder deep-copies those rules (`_build_strategy_style` -> `_style_entry_rules`, `ba2test_launcher.py:8295-8305`), both arms carry tag 17. Same for O_LEAP (arms `O_LEAPC`/`O_LEAPP`); O_CBS and O_PMCC are singles (`_build_strategy_option`, line 5342). Decision: append the leaf in `_style_entry_rules` (the ONE place every style member's option-entry rules pass through, after the member's own gates incl. market-condition / SPY-regime gates, which `_option_entry_rule` appends last at ~7336-7340), NOT inside `_option_entry_rule` (which knows nothing of styles and also builds the stage-1 single jobs). One leaf id per MEMBER (`<member>-structure_share`) reused by both arms of a group = one gene `cond:<member>-structure_share:value`. Equity-entry members (O_CC, O_PP: `_STYLE_EQUITY_ENTRY`, ~7430) get no leaf.
3. **Cheap per-transaction entry tag in BOTH runtimes:** `ba2_common.core.entry_order.entry_order_for_transaction(txn_id)` (`entry_order.py:104`; reads through `trade_store.orders_where`, so it is the in-mem store in a backtest and SQLite live) + `entry_tag.order_entry_tag` (`entry_tag.py:86`). New shared helper `entry_order.transaction_entry_tag(txn_id)` (Task 1). The Alpaca-only `_position_entry_tag` (`AlpacaAccount.py:8197`) is the same two calls via `TradeManager.resolve_entry_order` (`TradeManager.py:215`, which delegates to the same `entry_order_for_transaction`). One orders query per OPEN member transaction, once per cycle (cache).
4. **Cache location/key:** a module-level `_SHARE_CACHE: Dict[sleeve_key, _ShareCache]` in `OptionRiskManagement.py` next to `_PENDING` (`:703`), key `_sleeve_key(expert_id)` (`:315`: `(None, expert)` live, `(thread id, expert)` backtest), entry stamped with the DECISION TIME. The condition gets the decision time from `expert_recommendation.created_at` -- the same "sim-aware now" `DaysOpenedCondition` uses (`TradeConditions.py:2213-2260`): BT stamps it with the bar `as_of` (`daily_engine.py:500`, `created_at=as_of`), live it is the recommendation's wall-clock creation. A stamp match = same cycle -> reuse; a mismatch simply REBUILDS from truth (book + pending), so correctness never depends on the stamp (in live, recommendations of one pass may carry different timestamps, so the cache then only saves work some of the time -- stated, accepted). `record_submitted(..., entry_tag=)` (`:718`) adds the admitted candidate's `max_loss` to `cache.committed[tag]` (not for `covered_call`), and bumps `_SHARE_EPOCH[key]` so a build that raced with a submission is not stored (the build loop retries). Both runtimes evaluate+submit option entries symbol by symbol (live `TradeManager.py:3030-3141` -> `evaluator.execute(submit_to_broker=True)`; BT `daily_engine._stage_recommendation_candidate` `:1492` -> `evaluator.execute(submit_to_broker=True)` `:1645`), so the increment is seen by the next symbol. Resets: `reset_state` (`:~865`), `reset_thread_state` (`:~895`), `reset_pending_charges` (`:706`).
5. **Assigned stock cost basis:** `Transaction.open_price` (the assignment STRIKE) x `abs(Transaction.quantity)` (shares). Backtest: `BacktestAccount._open_assigned_stock_transaction` (`backtest_account.py:6846-6900`: `open_price=float(price)`, `quantity=abs(float(qty))`, `meta_data={"origin": "csp_assignment"}`, entry order `data` = `assigned_stock_entry_data(entry_tag)`); live: `AlpacaAccount` writes the same shape (`~8139`, `meta_data.origin == TXN_ORIGIN_CSP_ASSIGNMENT`, `types.py:366`). A lot whose `open_price` is None is UNMEASURABLE (live writes NULL rather than inventing a basis, `AlpacaAccount.py:~8150`) -> condition False + ERROR. Covered calls: see Finding 1.
6. **Facts to respect:** `ExpertRecommendation.instance_id` is the expert id (condition needs it); the condition only makes sense for an expert that runs `risk_manager_mode == "classic_options"` (otherwise `record_submitted` is never called, `TradeActions.py:3985-4014`) -> the condition refuses (False + ERROR) when the RM is not engaged; the style validator refuses `structure_share` on a style without a `risk` block.

## Environment (verified)

No `.venv` exists in the worktree. Use the main checkout's interpreter with the WORKTREE's code on `PYTHONPATH`. Run from the worktree root. PowerShell (once per shell):

```powershell
cd C:\Users\basti\Documents\dev\BA2-structshare
$w = (Get-Location).Path
$env:PYTHONPATH = "$w\packages\common;$w\packages\providers;$w\packages\experts;$w\testplatform\backend;$w\testplatform;$w"
$PY = "C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe"
& $PY -c "import ba2_common, ba2_experts, app; print(ba2_common.__file__, ba2_experts.__file__, app.__file__)"
```

Expected: every path starts with `C:\Users\basti\Documents\dev\BA2-structshare\` (verified 2026-11-06). Without `PYTHONPATH`, `ba2_common` resolves to the MAIN checkout (editable `.pth`). Bash `PYTHONPATH` with `;` does NOT work (git-bash mangles it): use PowerShell. All commands below are `& $PY -m pytest <path> -q -x` from the worktree root (both `packages/common/tests/...` and `testplatform/backend/tests/...` run this way; verified). One pytest process at a time; never the full suite; never kill python broadly. Do NOT touch `C:\Users\basti\Documents\dev\BA2TradePlatform`. Do not push.

Commit trailer for EVERY commit in this plan (two lines, last in the message):

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F8HzfECLzdvfEbz74Aokc7
```

---

## Task 0: GA-neutrality goldens (captured BEFORE any code change)

Why first: the owner's release rule is "no GA change for existing jobs". The goldens are produced from the UNMODIFIED builder and then guard every later task.

**Files:**
- Create: `testplatform/backend/tests/test_option_style_ga_neutral.py`
- Create (generated): `testplatform/backend/tests/data/option_style_goldens.json` (mid, premium, long; ~200 KB)

**Step 1: write the test module (goldens part)**

```python
"""GA-NEUTRALITY of the structure_share feature (owner 2026-11-06).

A style WITHOUT the opt-in ``structure_share`` field must build rules, genes and id maps that are
byte-identical to what the builder produced BEFORE the feature existed. The goldens in
``data/option_style_goldens.json`` were generated from the unmodified builder (plan
2026-11-06-structure-share-condition-plan.md, Task 0). ``long`` is golden WITHOUT the field: the
shipped ``long`` style opts in later (Task 10), so its golden is compared after removing the field.

Regenerate ONLY deliberately (a builder change that is meant to move these rules):
    python testplatform/backend/tests/test_option_style_ga_neutral.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

from tests.test_option_grid_foundations import _launcher  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_STAGE2 = str(_ROOT / "tools/option_styles.stage2.json")
_GOLDEN = Path(__file__).resolve().parent / "data" / "option_style_goldens.json"
_NAMES = ["mid", "premium", "long"]


def snapshot(m, name: str, *, drop_structure_share: bool = False) -> dict:
    """The built style, as plain JSON: entry rules, exit rules, gene space, id maps."""
    from app.services.strategy_param_space import collect_param_space

    style = m._load_style_spec(_STAGE2, name)
    if drop_structure_share:
        style.pop("structure_share", None)
    m._STYLE_ACTIVE = style
    s = m._build_strategy_style("O_STYLE")
    return json.loads(json.dumps(
        {"entry": s.entry_rules, "exit": s.exit_rules, "genes": collect_param_space(s),
         "id_maps": s.style_id_maps}, sort_keys=True, default=str))


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    m = _launcher()
    monkeypatch.setattr(m, "_STYLE_ACTIVE", None)
    yield


@pytest.mark.parametrize("name", _NAMES)
def test_a_style_without_structure_share_builds_byte_identical_rules_and_genes(name):
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    got = snapshot(_launcher(), name, drop_structure_share=True)
    assert got["genes"] == golden[name]["genes"]
    assert got["id_maps"] == golden[name]["id_maps"]
    assert got["entry"] == golden[name]["entry"]
    assert got["exit"] == golden[name]["exit"]
    assert "structure_share" not in json.dumps(got)


def _regen() -> None:
    m = _launcher()
    out = {n: snapshot(m, n, drop_structure_share=True) for n in _NAMES}
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN.write_text(json.dumps(out, sort_keys=True, indent=1), encoding="utf-8")
    print("wrote", _GOLDEN, {n: len(json.dumps(v)) for n, v in out.items()})


if __name__ == "__main__":
    _regen()
```

**Step 2: run it, expect FAIL (no golden file yet)**

`& $PY -m pytest testplatform/backend/tests/test_option_style_ga_neutral.py -q -x`
Expected: FAIL `FileNotFoundError: ...option_style_goldens.json`.

**Step 3: generate the golden from the UNMODIFIED builder** (git must show no change under `packages/` or `testplatform/ba2test_launcher.py` yet)

`& $PY testplatform/backend/tests/test_option_style_ga_neutral.py`
Expected: prints `wrote ...option_style_goldens.json {'mid': ~51000, 'premium': ~93000, 'long': ~50000}` (sizes approximate; 2026-11-06 digests of the same payload: mid 124 genes, premium 222, long 122).

**Step 4: run, expect PASS**

`& $PY -m pytest testplatform/backend/tests/test_option_style_ga_neutral.py -q -x` -> 3 passed.

**Step 5: commit**

```
git add testplatform/backend/tests/test_option_style_ga_neutral.py testplatform/backend/tests/data/option_style_goldens.json
git commit -m "test(style): goldens of the built mid/premium/long styles before structure_share" -m "<trailer>"
```
(Write the real two trailer lines, not `<trailer>`.)

---

## Task 1: shared entry-tag helpers (transaction tag, tag of a rule's actions)

**Files:**
- Modify: `packages/common/ba2_common/core/entry_order.py` (append after `oldest_entry_order`, ends line ~142)
- Modify: `packages/common/ba2_common/core/entry_tag.py` (append at end, after `order_entry_tag`, line ~101)
- Create: `packages/common/tests/test_structure_share_entry_tag.py`

**Step 1: failing test**

```python
"""Shared entry-tag lookups used by the structure_share condition (plan 2026-11-06, Task 1)."""
from types import SimpleNamespace

import pytest

import ba2_common.core.entry_order as eo
from ba2_common.core.entry_tag import entry_tag_of_actions


def test_transaction_entry_tag_reads_the_entry_orders_tag(monkeypatch):
    monkeypatch.setattr(eo, "entry_order_for_transaction",
                        lambda tid, session=None: SimpleNamespace(id=1, data={"entry_tag": 14}))
    assert eo.transaction_entry_tag(5) == 14


@pytest.mark.parametrize("order", [None, SimpleNamespace(id=1, data=None),
                                   SimpleNamespace(id=1, data={})])
def test_transaction_entry_tag_is_none_for_an_untagged_or_missing_entry(monkeypatch, order):
    monkeypatch.setattr(eo, "entry_order_for_transaction", lambda tid, session=None: order)
    assert eo.transaction_entry_tag(5) is None


def test_a_corrupt_stored_tag_propagates(monkeypatch):
    monkeypatch.setattr(eo, "entry_order_for_transaction",
                        lambda tid, session=None: SimpleNamespace(id=1, data={"entry_tag": "x"}))
    with pytest.raises(ValueError, match="entry_tag"):
        eo.transaction_entry_tag(5)


def test_tag_of_actions_reads_the_entry_action_config():
    actions = {"a0": {"action_type": "buy_call", "entry_tag": 19, "option_dte_min": 80}}
    assert entry_tag_of_actions(actions) == 19


@pytest.mark.parametrize("actions", [None, {}, {"a0": {"action_type": "buy_call"}},
                                     {"a0": "not-a-dict"}])
def test_tag_of_actions_is_none_without_a_tag(actions):
    assert entry_tag_of_actions(actions) is None


def test_two_different_tags_on_one_rule_are_refused():
    actions = {"a0": {"action_type": "buy_call", "entry_tag": 19},
               "a1": {"action_type": "buy_put", "entry_tag": 20}}
    with pytest.raises(ValueError, match="disagree"):
        entry_tag_of_actions(actions)


def test_the_same_tag_twice_is_one_tag():
    actions = {"a0": {"action_type": "buy_call", "entry_tag": 19},
               "a1": {"action_type": "buy_put", "entry_tag": 19}}
    assert entry_tag_of_actions(actions) == 19
```

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_entry_tag.py -q -x` -> FAIL `AttributeError: module 'ba2_common.core.entry_order' has no attribute 'transaction_entry_tag'` (and ImportError for `entry_tag_of_actions`).

**Step 3: implement**

Append to `entry_order.py`:

```python
def transaction_entry_tag(transaction_id: Any) -> Optional[int]:
    """The style-member tag of ``transaction_id``'s position (stamped on its entry order), or None.

    ONE definition for both runtimes: ``entry_order_for_transaction`` reads through
    ``trade_store.orders_where`` (the backtest's in-mem store, else SQLite), and the tag is read
    back with ``entry_tag.order_entry_tag`` -- the same two calls ``AlpacaAccount.
    _position_entry_tag`` makes via ``TradeManager.resolve_entry_order``. No entry order or an
    untagged one -> None; a stored value no entry could have written RAISES (``ValueError``).
    """
    from ba2_common.core.entry_tag import order_entry_tag

    return order_entry_tag(entry_order_for_transaction(transaction_id))
```

Append to `entry_tag.py`:

```python
def entry_tag_of_actions(actions: Any) -> Optional[int]:
    """The member tag an EventAction's ``actions`` dict carries, or None.

    Reads the ``entry_tag`` key of each action config (``rules_convert`` / ``action_from_rule``
    write the authored ``option_entry_tag`` there, and the evaluator forwards it to the entry
    action). Used by conditions that must know WHICH member their own rule opens
    (``StructureShareCondition``). Distinct tags on one rule are refused: the condition could
    not tell which member's share to measure.
    """
    if not isinstance(actions, dict):
        return None
    tags = set()
    for cfg in actions.values():
        if isinstance(cfg, dict) and cfg.get(ORDER_ENTRY_TAG_KEY) is not None:
            tags.add(coerce_entry_tag(cfg[ORDER_ENTRY_TAG_KEY]))
    if len(tags) > 1:
        raise ValueError(f"entry actions of one rule disagree on entry_tag: {sorted(tags)}")
    return next(iter(tags), None)
```

**Step 4:** rerun -> all pass. Also `& $PY -m pytest packages/common/tests/test_entry_tag_condition.py -q` still passes.

**Step 5: commit** `feat(entry-tag): transaction_entry_tag and entry_tag_of_actions shared helpers` + trailer.

---

## Task 2: `_PendingCharge.entry_tag`, `record_submitted(entry_tag=)`, tag-aware prune (inert when untagged)

**Files:**
- Modify: `packages/common/ba2_common/core/OptionRiskManagement.py`: `_PendingCharge` (`:703-705` region), `record_submitted` (`:718-726`), `_prune_pending` (`:751-807`)
- Modify: `packages/common/ba2_common/core/TradeActions.py:4014-4015` (the `record_submitted` call) + one tiny helper method on `_OptionEntryAction`
- Create: `packages/common/tests/test_structure_share_pending.py`

**Step 1: failing tests**

```python
"""Pending charges carry the member tag (plan 2026-11-06, Task 2). Inert when untagged."""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.TradeActions as ta
from ba2_common.core.option_book import CandidateStructure
from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
from ba2_common.core.types import OptionRight

EID = 7


def cand(max_loss, strategy="cash_secured_put"):
    return CandidateStructure(underlying="MSFT", strategy=strategy, max_loss=max_loss,
                              notional=max_loss, short_put_assignment=max_loss,
                              is_defined_risk=True)


def visible(txn_id):
    return OptionStructure(
        transaction_id=txn_id, underlying="AAPL", strategy="cash_secured_put",
        legs=(LifecycleLeg(contract_symbol="AAPL270115P100", net_qty=-1.0, strike=100.0,
                           option_type=OptionRight.PUT, expiry=date(2027, 1, 15),
                           underlying="AAPL", entry_premium=2.0),),
        quantity=1.0, multiplier=100, entry_net_premium=-2.0)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rm.reset_state()
    monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: {101, 102})
    yield
    rm.reset_state()


def test_a_charge_carries_the_tag_it_was_given():
    rm.record_submitted(EID, 101, cand(500.0), entry_tag=14)
    (charge,) = rm._PENDING[rm._sleeve_key(EID)]
    assert charge.entry_tag == 14


def test_a_charge_recorded_the_old_way_has_no_tag():
    rm.record_submitted(EID, 101, cand(500.0))
    (charge,) = rm._PENDING[rm._sleeve_key(EID)]
    assert charge.entry_tag is None


def test_prune_pending_still_returns_candidates_and_drops_the_visible_one():
    c1, c2 = cand(500.0), cand(700.0)
    rm.record_submitted(EID, 101, c1, entry_tag=14)
    rm.record_submitted(EID, 102, c2, entry_tag=11)
    kept = rm._prune_pending(EID, [visible(102)])       # 102 is in the book now
    assert kept == [c1]


def test_prune_pending_charges_keeps_the_tags():
    c1, c2 = cand(500.0), cand(700.0)
    rm.record_submitted(EID, 101, c1, entry_tag=14)
    rm.record_submitted(EID, 102, c2, entry_tag=11)
    kept = rm._prune_pending_charges(EID, [visible(102)])
    assert [(c.candidate, c.entry_tag) for c in kept] == [(c1, 14)]


def test_the_action_forwards_the_tag_only_when_it_has_one():
    """An untagged action must call record_submitted EXACTLY as before (existing tests patch
    it with a 3-positional lambda), a tagged one adds the keyword."""
    f = ta._OptionEntryAction._record_submitted_kwargs
    assert f(SimpleNamespace(entry_tag=None)) == {}
    assert f(SimpleNamespace(entry_tag=14)) == {"entry_tag": 14}
```

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_pending.py -q -x` -> FAIL (`TypeError: record_submitted() got an unexpected keyword argument 'entry_tag'`).

**Step 3: implement**

`OptionRiskManagement.py`:

```python
@dataclass
class _PendingCharge:
    transaction_id: Optional[int]
    candidate: CandidateStructure
    #: The style-member tag of the entry this charge stands for (structure_share condition,
    #: plan 2026-11-06). None for every untagged entry: nothing reads it then.
    entry_tag: Optional[int] = None
```

```python
def record_submitted(expert_instance_id: int, transaction_id: Optional[int],
                     candidate: CandidateStructure, entry_tag: Optional[int] = None) -> None:
    """Remember what was just put on the wire so the NEXT candidate is charged for it.

    ``entry_tag`` (tagged style entries only) also feeds the per-member share cache
    (:func:`_note_submission`); an untagged entry never touches it."""
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        charges = _PENDING.setdefault(key, [])
        charges.append(_PendingCharge(transaction_id, candidate, entry_tag))
        if len(charges) > _PENDING_LIMIT:
            del charges[:-_PENDING_LIMIT]
        if entry_tag is not None:
            _note_submission(key, entry_tag, candidate)
```

Add a placeholder that Task 4 fills (so this task is self-contained and green):

```python
def _note_submission(key, entry_tag: int, candidate: CandidateStructure) -> None:
    """Hook for the per-member share cache (filled in by the structure_share cache)."""
```

Rename the body of `_prune_pending` to `_prune_pending_charges` returning `List[_PendingCharge]` (`return kept` instead of `[c.candidate for c in kept]`; early `return []` unchanged) and keep the old name as a wrapper:

```python
def _prune_pending(expert_instance_id: int,
                   visible: Sequence[OptionStructure]) -> List[CandidateStructure]:
    """Drop charges the book can now see ... (docstring unchanged, moved to
    :func:`_prune_pending_charges`)."""
    return [c.candidate for c in _prune_pending_charges(expert_instance_id, visible)]
```
(Move the existing long docstring to `_prune_pending_charges` verbatim; do not change any logic line.)

`TradeActions.py`: add to `_OptionEntryAction` (next to `_option_risk_manager`, ~3727):

```python
    def _record_submitted_kwargs(self) -> Dict[str, Any]:
        """Extra ``record_submitted`` keywords: the member tag only when this entry has one, so
        an untagged entry calls it exactly as before the structure_share feature."""
        return {} if self.entry_tag is None else {"entry_tag": self.entry_tag}
```
and change the call at `:4014`:

```python
            record_submitted(rm_instance_id, getattr(order, "transaction_id", None),
                             rm_candidate, **self._record_submitted_kwargs())
```

**Step 4:** rerun -> pass. Regression: `& $PY -m pytest packages/common/tests/test_option_risk_manager_wiring.py packages/common/tests/test_option_risk_manager_seam.py packages/common/tests/test_option_rm_state_is_thread_safe.py testplatform/backend/tests/backtest/test_option_order_fill_volume_sizing.py testplatform/backend/tests/backtest/test_option_rm_state_is_per_run.py -q` (one process) -> all pass.

**Step 5: commit** `feat(option-rm): pending charges carry the member tag` + trailer.

---

## Task 3: `member_share` -- committed capital per member (book + assigned stock + pending)

**Files:**
- Modify: `packages/common/ba2_common/core/OptionRiskManagement.py`: imports (`:121-140`: add `from ba2_common.core.entry_order import transaction_entry_tag`, and `_structure_totals` to the `option_book` import), new section after `sleeve_equity` (`~:1075`)
- Create: `packages/common/tests/test_structure_share_cache.py`

**Step 1: failing tests (build side; the cache lifecycle tests are Task 4)**

```python
"""Per-member committed capital and share (plan 2026-11-06, Tasks 3-5)."""
from __future__ import annotations

import threading
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.option_book import CandidateStructure
from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
from ba2_common.core.types import OptionRight

EID = 7
CSP, WHEEL, CBS = 11, 14, 19
T1, T2, T3 = (datetime(2026, 11, d, 10, 0) for d in (2, 3, 4))


def _leg(sym, qty, strike, right, premium=2.0):
    return LifecycleLeg(contract_symbol=sym, net_qty=qty, strike=strike, option_type=right,
                        expiry=date(2027, 1, 15), underlying="AAPL", entry_premium=premium)


def short_put(txn_id, strike=100.0, qty=1.0):
    """Cash-secured put: committed = strike * 100 * qty (naked short put)."""
    return OptionStructure(transaction_id=txn_id, underlying="AAPL", strategy="cash_secured_put",
                           legs=(_leg(f"AAPL270115P{strike}", -qty, strike, OptionRight.PUT),),
                           quantity=qty, multiplier=100, entry_net_premium=-2.0)


def long_call(txn_id, premium=2.0):
    """Long call: committed = the premium paid = premium * 100."""
    return OptionStructure(transaction_id=txn_id, underlying="AAPL", strategy="long_call",
                           legs=(_leg("AAPL270115C100", 1.0, 100.0, OptionRight.CALL),),
                           quantity=1.0, multiplier=100, entry_net_premium=premium)


def covered_call(txn_id, strike=110.0):
    return OptionStructure(transaction_id=txn_id, underlying="AAPL", strategy="covered_call",
                           legs=(_leg(f"AAPL270115C{strike}", -1.0, strike, OptionRight.CALL),),
                           quantity=1.0, multiplier=100, entry_net_premium=-1.0)


def lot(txn_id, price=100.0, qty=100.0):
    return SimpleNamespace(id=txn_id, symbol="AAPL", open_price=price, quantity=qty)


def cand(max_loss, strategy="cash_secured_put"):
    return CandidateStructure(underlying="MSFT", strategy=strategy, max_loss=max_loss,
                              notional=max_loss, short_put_assignment=max_loss,
                              is_defined_risk=True)


class World:
    """The data seams of the share build, as a mutable fixture (the real readers are covered by
    the in-mem-store test in Task 5)."""

    def __init__(self, monkeypatch):
        self.structures, self.unbuildable, self.tags, self.lots = [], [], {}, []
        self.equity = 100_000.0
        self.reads = 0
        monkeypatch.setattr(rm, "sleeve_structures", self._structures)
        monkeypatch.setattr(rm, "transaction_entry_tag", lambda tid: self.tags.get(tid))
        monkeypatch.setattr(rm, "assigned_stock_lots", lambda eid: list(self.lots))
        # None = "ledger unreadable": every in-flight charge is KEPT (the conservative reading)
        monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: None)

    def _structures(self, eid):
        self.reads += 1
        return list(self.structures), list(self.unbuildable)

    @property
    def account(self):
        return SimpleNamespace(
            get_account_snapshot=lambda: SimpleNamespace(equity=self.equity))


@pytest.fixture
def world(monkeypatch):
    rm.reset_state()
    yield World(monkeypatch)
    rm.reset_state()


def share(world, tag, at=T1):
    return rm.member_share(world.account, EID, tag, at)


def test_committed_is_the_members_own_open_structures(world):
    world.structures = [short_put(1), short_put(2, strike=50.0), long_call(3)]
    world.tags = {1: CSP, 2: CSP, 3: CBS}
    csp = share(world, CSP)
    assert csp.committed == 15_000.0
    assert csp.share_pct == pytest.approx(15.0)
    assert csp.unmeasurable == ()
    assert share(world, CBS).committed == 200.0
    assert share(world, 99).committed == 0.0           # a member with nothing open: measured 0


def test_an_untagged_structure_belongs_to_no_member(world):
    world.structures = [short_put(1)]
    world.tags = {}
    assert share(world, CSP).committed == 0.0


def test_a_covered_call_adds_nothing_the_lots_cost_basis_is_its_capital(world):
    world.structures = [covered_call(2)]
    world.lots = [lot(900)]
    world.tags = {2: WHEEL, 900: WHEEL}
    assert share(world, WHEEL).committed == 10_000.0     # 100 sh * 100, NOT +11_000


def test_the_wheels_share_holds_through_assignment_and_the_covered_call(world):
    world.tags = {1: WHEEL, 900: WHEEL, 2: WHEEL}
    world.structures = [short_put(1)]                    # phase 1: the short put
    assert share(world, WHEEL, T1).committed == 10_000.0
    world.structures, world.lots = [], [lot(900)]        # phase 2: assigned, put gone
    assert share(world, WHEEL, T2).committed == 10_000.0
    world.structures = [covered_call(2)]                 # phase 3: call written on the lot
    assert share(world, WHEEL, T3).committed == 10_000.0


def test_an_untagged_lot_is_nobodys(world):
    world.lots = [lot(900)]
    assert share(world, WHEEL).committed == 0.0


def test_pending_charges_count_until_the_book_sees_them(world):
    rm.record_submitted(EID, 555, cand(8_000.0), entry_tag=CSP)
    assert share(world, CSP, T1).committed == 8_000.0
    world.structures, world.tags = [short_put(555)], {555: CSP}    # now visible: charge pruned
    r = share(world, CSP, T2)
    assert r.committed == 10_000.0                       # the book's number, not 8_000 + 10_000


def test_a_pending_covered_call_adds_nothing(world):
    rm.record_submitted(EID, 556, cand(11_000.0, "covered_call"), entry_tag=WHEEL)
    assert share(world, WHEEL).committed == 0.0


def test_an_unmeasurable_structure_makes_the_members_share_unknown_not_zero(world):
    bad = OptionStructure(transaction_id=1, underlying="AAPL", strategy="cash_secured_put",
                          legs=(_leg("AAPL270115P100", -1.0, None, OptionRight.PUT),),
                          quantity=1.0, multiplier=100, entry_net_premium=-2.0)
    world.structures, world.tags = [bad, long_call(3)], {1: CSP, 3: CBS}
    r = share(world, CSP)
    assert r.share_pct is None and r.committed is None
    assert any("transaction 1" in why for why in r.unmeasurable)
    assert share(world, CBS).share_pct == pytest.approx(0.2)    # other members unaffected


def test_an_unbuildable_tagged_transaction_blinds_its_member(world):
    world.unbuildable, world.tags = [9], {9: CSP}
    r = share(world, CSP)
    assert r.share_pct is None and any("transaction 9" in why for why in r.unmeasurable)


def test_a_lot_without_a_cost_basis_blinds_its_member(world):
    world.lots, world.tags = [lot(900, price=None)], {900: WHEEL}
    r = share(world, WHEEL)
    assert r.share_pct is None and any("transaction 900" in why for why in r.unmeasurable)


@pytest.mark.parametrize("equity", [None, 0.0, -5.0])
def test_unusable_equity_is_unknown_never_a_share(world, equity):
    world.equity = equity
    world.structures, world.tags = [short_put(1)], {1: CSP}
    r = share(world, CSP)
    assert r.share_pct is None and r.equity is None
    assert any("equity" in why for why in r.unmeasurable)


def test_a_decision_time_is_required(world):
    with pytest.raises(ValueError, match="decision time"):
        rm.member_share(world.account, EID, CSP, None)
```

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_cache.py -q -x` -> FAIL (`AttributeError: ... has no attribute 'transaction_entry_tag'`).

**Step 3: implement** in `OptionRiskManagement.py` (new section, after `sleeve_equity`; imports at top as listed). Note `COVERED_CALL_STRATEGY` is `option_lifecycle.COVERED_CALL_STRATEGY` (`option_lifecycle.py:169`); add it to the existing `option_lifecycle` import.

```python
# ---------------------------------------------------------------------------
# structure share: what each style MEMBER has committed (plan 2026-11-06)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MemberShare:
    """One member's committed capital against the sleeve's equity. ``share_pct`` is None --
    never 0 -- whenever any input is unmeasurable; ``unmeasurable`` says which."""
    entry_tag: int
    committed: Optional[float]
    equity: Optional[float]
    share_pct: Optional[float]
    unmeasurable: Tuple[str, ...] = ()


@dataclass
class _ShareCache:
    stamp: Any
    equity: Optional[float]
    committed: Dict[int, float]
    blind: Dict[int, List[str]]


def assigned_stock_lots(expert_instance_id: int) -> List[Any]:
    """This expert's OPENED equity transactions that an option assignment delivered
    (``meta_data.origin == "csp_assignment"``), oldest first. Dual-path like
    :func:`open_option_transactions`: the in-mem store in a backtest, SQLite live."""
    from ba2_common.core.trade_store import transactions_where
    from ba2_common.core.types import AssetClass, TransactionStatus, TXN_ORIGIN_CSP_ASSIGNMENT

    rows = transactions_where(expert_id=expert_instance_id, status=TransactionStatus.OPENED)
    return sorted((t for t in rows
                   if t.asset_class == AssetClass.EQUITY and isinstance(t.meta_data, dict)
                   and t.meta_data.get("origin") == TXN_ORIGIN_CSP_ASSIGNMENT),
                  key=lambda t: t.id)


def _build_share_cache(account, expert_instance_id: int, stamp: Any) -> _ShareCache:
    """Committed capital per entry tag from the book, the assigned stock and the in-flight
    charges. A covered call contributes 0: its capital is the lot's cost basis."""
    committed: Dict[int, float] = {}
    blind: Dict[int, List[str]] = {}

    def add(tag: int, amount: float) -> None:
        committed[tag] = committed.get(tag, 0.0) + amount

    def lose(tag: int, why: str) -> None:
        blind.setdefault(tag, []).append(why)

    structures, unbuildable = sleeve_structures(expert_instance_id)
    for structure in structures:
        tag = transaction_entry_tag(structure.transaction_id)
        if tag is None or structure.strategy == COVERED_CALL_STRATEGY:
            continue
        amount, *_rest, why = _structure_totals(structure)
        if why or amount is None:
            lose(tag, f"transaction {structure.transaction_id}: {why}")
        else:
            add(tag, amount)
    for transaction_id in unbuildable:
        tag = transaction_entry_tag(transaction_id)
        if tag is not None:
            lose(tag, f"transaction {transaction_id}: OPENED option position with no executed "
                      f"option order row -- its committed capital is unmeasurable")
    for lot_txn in assigned_stock_lots(expert_instance_id):
        tag = transaction_entry_tag(lot_txn.id)
        if tag is None:
            continue
        if lot_txn.open_price is None or lot_txn.quantity is None:
            lose(tag, f"transaction {lot_txn.id}: assigned stock lot has no cost basis "
                      f"(open_price={lot_txn.open_price!r}, quantity={lot_txn.quantity!r})")
        else:
            add(tag, abs(float(lot_txn.open_price)) * abs(float(lot_txn.quantity)))
    for charge in _prune_pending_charges(expert_instance_id, structures):
        if charge.entry_tag is None or charge.candidate.strategy == COVERED_CALL_STRATEGY:
            continue
        if charge.candidate.max_loss is None:
            lose(charge.entry_tag, f"in-flight {charge.candidate.strategy} on "
                                   f"{charge.candidate.underlying}: max loss unmeasurable")
        else:
            add(charge.entry_tag, float(charge.candidate.max_loss))
    equity = sleeve_equity(account, expert_instance_id)
    if equity is not None and equity <= 0:
        equity = None
    return _ShareCache(stamp, equity, committed, blind)


def _share_view(cache: _ShareCache, entry_tag: int) -> MemberShare:
    why = list(cache.blind.get(entry_tag, ()))
    if cache.equity is None:
        why.append("sleeve equity is unavailable or not positive")
    if why:
        return MemberShare(entry_tag, None, cache.equity, None, tuple(why))
    amount = cache.committed.get(entry_tag, 0.0)
    return MemberShare(entry_tag, amount, cache.equity, amount / cache.equity * 100.0)


def member_share(account, expert_instance_id: int, entry_tag: int, as_of: Any) -> MemberShare:
    """``entry_tag``'s committed capital as a percent of the sleeve's equity at ``as_of``.

    SHARED by live and backtest. Task 4 puts the per-cycle cache in front of
    :func:`_build_share_cache`; until then every call rebuilds."""
    if as_of is None:
        raise ValueError("member_share needs the decision time (the recommendation's "
                         "created_at); without it the per-cycle cache has no key")
    return _share_view(_build_share_cache(account, expert_instance_id, as_of), entry_tag)
```

Fix the unpack in `_build_share_cache` if `_structure_totals` returns a 6-tuple `(committed, naked, notional, outlay, assignment, why)`: write it explicitly as `amount, _n, _no, _o, _a, why = _structure_totals(structure)` (clearer than the starred form above; use this).

**Step 4:** rerun -> pass (14 tests).

**Step 5: commit** `feat(option-rm): member_share -- per-member committed capital (book, assigned stock, pending)` + trailer.

---

## Task 4: the per-cycle cache, its increment, epoch guard and resets

**Files:**
- Modify: `packages/common/ba2_common/core/OptionRiskManagement.py`: stores next to `_PENDING` (`:703-715`), `_note_submission` (filled), `member_share`, `reset_state` (`:~865`), `reset_pending_charges` (`:706`), `reset_thread_state` (`:~895`)
- Modify: `packages/common/tests/test_structure_share_cache.py` (append)

**Step 1: failing tests (append)**

```python
# ------------------------------------------------------------------ the cache (Task 4)
def test_one_cycle_reads_the_book_once(world):
    world.structures, world.tags = [short_put(1)], {1: CSP}
    share(world, CSP, T1)
    share(world, CBS, T1)
    share(world, CSP, T1)
    assert world.reads == 1


def test_a_new_decision_time_rebuilds_from_the_book(world):
    world.structures, world.tags = [short_put(1)], {1: CSP}
    assert share(world, CSP, T1).committed == 10_000.0
    world.structures.append(short_put(2, strike=50.0))
    world.tags[2] = CSP
    assert share(world, CSP, T1).committed == 10_000.0     # same cycle: cached
    assert share(world, CSP, T2).committed == 15_000.0     # next cycle: rebuilt, no drift
    assert world.reads == 2


def test_an_admitted_entry_is_seen_by_the_next_symbol_of_the_same_cycle(world):
    assert share(world, CSP, T1).committed == 0.0
    rm.record_submitted(EID, 700, cand(5_000.0), entry_tag=CSP)
    r = share(world, CSP, T1)
    assert r.committed == 5_000.0 and r.share_pct == pytest.approx(5.0)
    assert world.reads == 1                                 # incremented, not rebuilt


def test_the_increment_goes_to_its_own_tag_only(world):
    share(world, CSP, T1)
    rm.record_submitted(EID, 700, cand(5_000.0), entry_tag=CSP)
    assert share(world, CBS, T1).committed == 0.0


def test_a_submitted_covered_call_does_not_move_the_cache(world):
    share(world, WHEEL, T1)
    rm.record_submitted(EID, 701, cand(11_000.0, "covered_call"), entry_tag=WHEEL)
    assert share(world, WHEEL, T1).committed == 0.0


def test_an_untagged_submission_touches_no_share_state(world):
    rm.record_submitted(EID, 702, cand(5_000.0))
    assert rm._SHARE_CACHE == {} and rm._SHARE_EPOCH == {}


def test_a_submission_that_raced_a_build_is_not_lost(world, monkeypatch):
    """A record_submitted landing AFTER the build read its pending snapshot but BEFORE the
    cache was stored must not vanish: the build is not stored and is redone."""
    real = rm._prune_pending_charges
    fired = []

    def racing(eid, structures):
        out = real(eid, structures)
        if not fired:
            fired.append(1)
            rm.record_submitted(EID, 703, cand(4_000.0), entry_tag=CSP)
        return out

    monkeypatch.setattr(rm, "_prune_pending_charges", racing)
    assert share(world, CSP, T1).committed == 4_000.0
    assert share(world, CSP, T1).committed == 4_000.0       # and it stays
    assert world.reads == 2                                  # first build discarded, second kept


@pytest.mark.parametrize("reset", [rm.reset_state, rm.reset_pending_charges,
                                   rm.reset_thread_state])
def test_the_run_boundary_resets_clear_the_cache(world, monkeypatch, reset):
    # backtest-shaped key so reset_thread_state sees this thread's keys
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    share(world, CSP, T1)
    assert rm._SHARE_CACHE
    reset()
    assert rm._SHARE_CACHE == {}


def test_each_backtest_thread_has_its_own_cache(world, monkeypatch):
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    share(world, CSP, T1)
    main_keys = set(rm._SHARE_CACHE)
    seen = {}

    def other():
        share(world, CSP, T1)
        seen["both"] = set(rm._SHARE_CACHE)
        rm.reset_thread_state()                 # only THIS thread's key
        seen["after"] = set(rm._SHARE_CACHE)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert len(seen["both"]) == 2 and seen["after"] == main_keys


def test_live_and_backtest_keys_do_not_share_a_cache(world, monkeypatch):
    share(world, CSP, T1)                                   # live key (None, EID)
    assert set(rm._SHARE_CACHE) == {(None, EID)}
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    share(world, CSP, T1)
    assert len(rm._SHARE_CACHE) == 2
```

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_cache.py -q -x` -> FAIL (`AttributeError: ... '_SHARE_CACHE'` / reads == 3).

**Step 3: implement**

Next to `_PENDING`:

```python
#: Per-sleeve, per-cycle {entry_tag: committed} for the structure_share condition (plan
#: 2026-11-06). Same key and same lock as _PENDING; cleared by the same run-boundary resets.
_SHARE_CACHE: Dict[Tuple[Optional[int], int], "_ShareCache"] = {}
#: Bumped by every TAGGED submission; a build that observed a different value when it finished
#: is discarded (a concurrent record_submitted would otherwise be lost between its pending
#: snapshot and the store). Untagged submissions never touch it.
_SHARE_EPOCH: Dict[Tuple[Optional[int], int], int] = {}
```
(`_ShareCache` is defined later in the module; annotate as string or move the dataclass above. Put `_ShareCache` and `MemberShare` BEFORE these dicts.)

Fill the hook:

```python
def _note_submission(key, entry_tag: int, candidate: CandidateStructure) -> None:
    """Called under _STATE_LOCK by :func:`record_submitted` for a TAGGED entry: bump the epoch
    and add the admitted candidate's max loss to the cached total of its tag, so the next
    symbol of this cycle sees it. A covered call adds nothing (see _build_share_cache)."""
    _SHARE_EPOCH[key] = _SHARE_EPOCH.get(key, 0) + 1
    cache = _SHARE_CACHE.get(key)
    if cache is None or candidate.strategy == COVERED_CALL_STRATEGY:
        return
    if candidate.max_loss is None:
        cache.blind.setdefault(entry_tag, []).append(
            f"in-flight {candidate.strategy} on {candidate.underlying}: max loss unmeasurable")
    else:
        cache.committed[entry_tag] = cache.committed.get(entry_tag, 0.0) + float(candidate.max_loss)
```

Replace the body of `member_share`:

```python
    if as_of is None:
        raise ValueError(...)  # unchanged
    key = _sleeve_key(expert_instance_id)
    for _attempt in range(3):
        with _STATE_LOCK:
            cache = _SHARE_CACHE.get(key)
            if cache is not None and cache.stamp == as_of:
                return _share_view(cache, entry_tag)
            epoch = _SHARE_EPOCH.get(key, 0)
        built = _build_share_cache(account, expert_instance_id, as_of)   # I/O outside the lock
        with _STATE_LOCK:
            if _SHARE_EPOCH.get(key, 0) == epoch:
                _SHARE_CACHE[key] = built
                return _share_view(built, entry_tag)
    # three consecutive races: answer from the last build, uncached (next call rebuilds)
    return _share_view(built, entry_tag)
```

Resets: `reset_pending_charges` also `_SHARE_CACHE.clear(); _SHARE_EPOCH.clear()` (inside the same `with`); `reset_state` calls `reset_pending_charges` already (check: it does) -> covered; `reset_thread_state` adds `_clear_this_threads_keys(_SHARE_CACHE)` and `_clear_this_threads_keys(_SHARE_EPOCH)`. Update the docstring list ("FIVE stores").

**Step 4:** rerun the file -> all pass; plus `packages/common/tests/test_option_rm_state_is_thread_safe.py` and `testplatform/backend/tests/backtest/test_option_rm_state_is_per_run.py` still pass.

**Step 5: commit** `feat(option-rm): per-cycle member share cache, incremented by record_submitted` + trailer.

---

## Task 5: BT/live parity of the share, on the real readers

**Files:**
- Modify: `packages/common/tests/test_structure_share_cache.py` (append)

**Step 1: tests (append).** Two tests: (a) identical inputs give the identical `MemberShare` under the live key shape and the backtest key shape; (b) the REAL readers (`sleeve_structures`, `transaction_entry_tag`, `assigned_stock_lots`) over the in-mem store give the expected number for a put -> assignment -> covered call story.

```python
# ------------------------------------------------------------------ parity (Task 5)
def _scenario(world):
    world.structures = [short_put(1), short_put(2, strike=50.0), long_call(3)]
    world.lots = [lot(900, price=80.0, qty=200.0)]
    world.tags = {1: CSP, 2: CSP, 3: CBS, 900: CSP}
    rm.record_submitted(EID, 555, cand(2_500.0), entry_tag=CSP)


def test_live_and_backtest_key_shapes_give_the_same_share_and_verdict(world, monkeypatch):
    results = {}
    for mode, key in (("live", lambda e: (None, e)),
                      ("bt", lambda e: (threading.get_ident(), e))):
        rm.reset_state()
        monkeypatch.setattr(rm, "_sleeve_key", key)
        _scenario(world)
        first = share(world, CSP, T1)
        rm.record_submitted(EID, 556, cand(1_000.0), entry_tag=CSP)
        results[mode] = (first, share(world, CSP, T1), share(world, CBS, T1))
    assert results["live"] == results["bt"]
    # 10_000 + 5_000 (puts) + 16_000 (200 sh * 80) + 2_500 pending = 33_500, then + 1_000
    assert results["bt"][0].committed == 33_500.0
    assert results["bt"][1].committed == 34_500.0


def test_the_real_readers_over_the_in_mem_store(monkeypatch):
    """put -> assignment -> covered call, through trade_store (the backtest runtime)."""
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.trade_store import inmem_trades, store_add
    from ba2_common.core.types import (AssetClass, OrderDirection, OrderStatus, OrderType,
                                       TransactionStatus, TXN_ORIGIN_CSP_ASSIGNMENT)

    rm.reset_state()
    acct = SimpleNamespace(get_account_snapshot=lambda: SimpleNamespace(equity=100_000.0))
    with inmem_trades():
        tid = store_add(Transaction(
            symbol="AAPL", quantity=100.0, side=OrderDirection.BUY, open_price=100.0,
            open_date=T1, status=TransactionStatus.OPENED, asset_class=AssetClass.EQUITY,
            expert_id=EID, meta_data={"origin": TXN_ORIGIN_CSP_ASSIGNMENT}))
        store_add(TradingOrder(
            account_id=1, symbol="AAPL", quantity=100.0, filled_qty=100.0,
            side=OrderDirection.BUY, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            open_price=100.0, transaction_id=tid, asset_class=AssetClass.EQUITY,
            created_at=T1, data={"entry_tag": WHEEL}))
        cc = store_add(Transaction(
            symbol="AAPL", quantity=1.0, side=OrderDirection.SELL, open_price=1.0, open_date=T2,
            status=TransactionStatus.OPENED, asset_class=AssetClass.OPTION, expert_id=EID,
            option_strategy="covered_call", multiplier=100))
        store_add(TradingOrder(
            account_id=1, symbol="AAPL", quantity=1.0, filled_qty=1.0, side=OrderDirection.SELL,
            order_type=OrderType.LIMIT, status=OrderStatus.FILLED, open_price=1.0,
            transaction_id=cc, asset_class=AssetClass.OPTION, contract_symbol="AAPL270115C110",
            strike=110.0, option_type=OptionRight.CALL, expiry=date(2027, 1, 15),
            underlying_symbol="AAPL", created_at=T2, data={"entry_tag": WHEEL}))
        r = rm.member_share(acct, EID, WHEEL, T3)
    assert r.unmeasurable == ()
    assert r.committed == 10_000.0 and r.share_pct == pytest.approx(10.0)
    rm.reset_state()
```
If a model constructor rejects a field name, align it with `models.py` (`Transaction`, `TradingOrder`); the assertions stay.

**Step 2:** run -> expected FAIL only if something above is wrong (this task is mostly verification of Tasks 3-4); if both pass immediately, note it: "tests written after the implementation by design -- they pin parity". If (b) fails on a field name, fix the TEST, not the code.

**Step 3/4:** fix whatever the real readers expose (e.g. `entry_order_for_transaction` ordering with `created_at`), re-run until green.

**Step 5: commit** `test(option-rm): member share parity across key shapes and on the in-mem store` + trailer.

---

## Task 6: `StructureShareCondition`

**Files:**
- Modify: `packages/common/ba2_common/core/TradeConditions.py`: new class after `EntryTagCondition` (ends `:3473`)
- Create: `packages/common/tests/test_structure_share_condition.py`

(`ExpertEventType.N_STRUCTURE_SHARE` does not exist yet; the class does not need it. Task 7 registers it.)

**Step 1: failing tests**

```python
"""StructureShareCondition: pass while a member's share of the sleeve is below X (plan 2026-11-06)."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.TradeConditions as TC
from ba2_common.core.TradeConditions import StructureShareCondition

EID, CSP = 7, 11
AT = datetime(2026, 11, 2, 10, 0)


class _World:
    def __init__(self, mp):
        self.structures, self.tags, self.equity = [], {}, 100_000.0
        mp.setattr(rm, "sleeve_structures", lambda eid: (list(self.structures), []))
        mp.setattr(rm, "transaction_entry_tag", lambda tid: self.tags.get(tid))
        mp.setattr(rm, "assigned_stock_lots", lambda eid: [])
        mp.setattr(rm, "_live_transaction_ids", lambda eid: None)
        self.account = SimpleNamespace(
            get_account_snapshot=lambda: SimpleNamespace(equity=self.equity))


@pytest.fixture
def rig(monkeypatch):
    rm.reset_state()
    w = _World(monkeypatch)
    w.engaged = True
    expert = SimpleNamespace(settings={"risk_manager_mode": "classic_options"})
    monkeypatch.setattr(
        "ba2_common.core.instance_resolver.get_instance_resolver",
        lambda: SimpleNamespace(get_expert_instance=lambda i: expert if w.engaged else
                                SimpleNamespace(settings={"risk_manager_mode": "classic"})))
    errors = []
    monkeypatch.setattr(TC.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    w.errors = errors
    yield w
    rm.reset_state()


def _put(txn_id, strike):
    from datetime import date
    from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
    from ba2_common.core.types import OptionRight
    return OptionStructure(
        transaction_id=txn_id, underlying="AAPL", strategy="cash_secured_put",
        legs=(LifecycleLeg(contract_symbol=f"AAPL270115P{strike}", net_qty=-1.0, strike=strike,
                           option_type=OptionRight.PUT, expiry=date(2027, 1, 15),
                           underlying="AAPL", entry_premium=2.0),),
        quantity=1.0, multiplier=100, entry_net_premium=-2.0)


def cond(w, value, tag=CSP, op="<", created_at=AT):
    c = StructureShareCondition(
        account=w.account, instrument_name="AAPL",
        expert_recommendation=SimpleNamespace(instance_id=EID, created_at=created_at),
        operator_str=op, value=value)
    c.rule_entry_tag = tag
    return c


def test_passes_while_the_share_is_below_the_limit(rig):
    rig.structures, rig.tags = [_put(1, 200.0)], {1: CSP}       # 20_000 / 100_000 = 20%
    c = cond(rig, 30.0)
    assert c.evaluate() is True
    assert c.get_calculated_value() == pytest.approx(20.0)


def test_fails_at_and_above_the_limit_strictly(rig):
    rig.structures, rig.tags = [_put(1, 200.0)], {1: CSP}
    assert cond(rig, 20.0).evaluate() is False                  # '<' is strict
    assert cond(rig, 10.0).evaluate() is False


def test_an_empty_member_always_passes(rig):
    assert cond(rig, 20.0).evaluate() is True


def test_no_member_tag_on_the_rule_is_false_and_loud(rig):
    assert cond(rig, 30.0, tag=None).evaluate() is False
    assert any("entry action" in e for e in rig.errors)


def test_an_expert_not_on_the_option_risk_manager_is_false_and_loud(rig):
    rig.engaged = False
    assert cond(rig, 30.0).evaluate() is False
    assert any("classic_options" in e for e in rig.errors)


def test_a_missing_decision_time_is_false_and_loud(rig):
    assert cond(rig, 30.0, created_at=None).evaluate() is False
    assert any("created_at" in e for e in rig.errors)


def test_an_unmeasurable_member_is_false_and_the_error_names_the_transaction(rig):
    from ba2_common.core.option_lifecycle import OptionStructure
    bad = OptionStructure(transaction_id=42, underlying="AAPL", strategy="cash_secured_put",
                          legs=(), quantity=1.0, multiplier=100, entry_net_premium=-2.0)
    rig.structures, rig.tags = [bad], {42: CSP}
    c = cond(rig, 30.0)
    assert c.evaluate() is False
    assert c.get_calculated_value() is None
    assert any("transaction 42" in e for e in rig.errors)


def test_bind_event_action_takes_the_tag_from_the_rules_entry_action(rig):
    c = StructureShareCondition(rig.account, "AAPL", None, "<", 30.0)
    c.bind_event_action(SimpleNamespace(actions={"a0": {"action_type": "buy_call",
                                                        "entry_tag": 19}}))
    assert c.rule_entry_tag == 19


def test_description_and_display(rig):
    rig.structures, rig.tags = [_put(1, 200.0)], {1: CSP}
    c = cond(rig, 30.0)
    c.evaluate()
    assert "structure" in c.get_description().lower() and "< 30" in c.get_description()
    assert c.get_actual_value_display() == "member 11: 20.0% of sleeve"
```

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_condition.py -q -x` -> FAIL `ImportError: cannot import name 'StructureShareCondition'`.

**Step 3: implement** (after `EntryTagCondition`):

```python
class StructureShareCondition(CompareCondition):
    """Gate a style member's ENTRY on its own share of the sleeve (plan 2026-11-06).

    ``share = committed(member) / sleeve_equity * 100``; the rule passes while ``share <op>
    value`` (the style builder authors ``<``). ANDed LAST into each member's entry rule, so it
    only runs on candidates that passed every other gate, and a False lets the NEXT member's
    rule be tried for the symbol (a rail refusal would not).

    WHICH member: the entry tag of the rule's OWN entry action, injected by
    ``TradeActionEvaluator._evaluate_conditions`` through :meth:`bind_event_action` (the only
    condition with that hook; every other condition is untouched). The candidate's own size is
    not known yet (strike/qty/max_loss are computed in ``_submit_option_order``): a member may
    overshoot X by one entry, bounded by its sizing gene.

    The arithmetic, the cache and the "unmeasurable" rule live in
    ``OptionRiskManagement.member_share`` -- ONE function for live and backtest. UNKNOWN NEVER
    PASSES: no member tag, no decision time, an expert that does not run the option risk manager
    (its ``record_submitted`` charges would never reach the cache), or any unmeasurable input
    -> False and an ERROR log naming why (and the transaction). A corrupted stored tag
    propagates (``ValueError``, not benign under BA2_ERROR_MODE=enforce).
    """

    def __init__(self, account: AccountInterface, instrument_name: str,
                 expert_recommendation: ExpertRecommendation, operator_str: str, value: float,
                 existing_order: Optional[TradingOrder] = None):
        super().__init__(account, instrument_name, expert_recommendation, operator_str,
                         float(value), existing_order)
        self.rule_entry_tag: Optional[int] = None

    def bind_event_action(self, event_action) -> None:
        """Called by the evaluator with the EventAction this condition belongs to."""
        from ba2_common.core.entry_tag import entry_tag_of_actions

        self.rule_entry_tag = entry_tag_of_actions(getattr(event_action, "actions", None))

    def evaluate(self) -> bool:
        self.calculated_value = None
        try:
            tag = self.rule_entry_tag
            if tag is None:
                logger.error(f"structure_share on {self.instrument_name}: the rule has no entry "
                             f"action carrying an entry_tag, so no member's share can be "
                             f"measured -- the condition is FALSE")
                return False
            rec = self.expert_recommendation
            instance_id = getattr(rec, "instance_id", None)
            as_of = getattr(rec, "created_at", None)
            if instance_id is None or as_of is None:
                logger.error(f"structure_share on {self.instrument_name}: the recommendation "
                             f"carries no instance_id/created_at (instance_id={instance_id!r}, "
                             f"created_at={as_of!r}) -- the condition is FALSE")
                return False
            from ba2_common.core import OptionRiskManagement as orm
            from ba2_common.core.instance_resolver import get_instance_resolver

            expert = get_instance_resolver().get_expert_instance(instance_id)
            if expert is None or not orm.option_risk_manager_enabled(
                    getattr(expert, "settings", None), expert_instance_id=instance_id):
                logger.error(f"structure_share on {self.instrument_name}: expert {instance_id} "
                             f"does not run risk_manager_mode='classic_options', so in-flight "
                             f"entries are never recorded and a share cannot be kept -- the "
                             f"condition is FALSE")
                return False
            reading = orm.member_share(self.account, instance_id, tag, as_of)
            if reading.share_pct is None:
                logger.error(f"structure_share on {self.instrument_name}: member {tag}'s share "
                             f"is UNMEASURABLE ({'; '.join(reading.unmeasurable)}) -- the "
                             f"condition is FALSE, never read as zero")
                return False
            self.calculated_value = reading.share_pct
            return self.operator_func(reading.share_pct, self.value)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating structure_share condition: {e}", exc_info=True)
            self.calculated_value = None
            return False

    def get_description(self) -> str:
        return (f"Check if the member's committed capital as a share of the sleeve for "
                f"{self.instrument_name}'s entry (structure_share) is {self.operator_str} "
                f"{self.value:g}%")

    def get_actual_value_display(self) -> Optional[str]:
        if self.calculated_value is None:
            return None
        return f"member {self.rule_entry_tag}: {self.calculated_value:.1f}% of sleeve"
```

`get_description` test asserts `"< 30"` in the text: `{self.value:g}` gives `30`; the description contains `is < 30%`. OK.

**Step 4:** rerun -> pass.

**Step 5: commit** `feat(conditions): StructureShareCondition` + trailer.

---

## Task 7: registration (enum, CONDITION_MAP, FIELD_EVENT, abbreviation, catalog, docs)

A missing `FIELD_EVENT` entry silently DROPS the leaf (`rule_builders.triggers_from_condition_tree`, `rule_builders.py:~332-345`). The existing closure tests catch every miss.

**Files:**
- Modify: `packages/common/ba2_common/core/types.py`: enum member after `N_ENTRY_TAG` (`:544`); `get_numeric_event_values` (`:819-829`, after `N_ENTRY_TAG`)
- Modify: `packages/common/ba2_common/core/TradeConditions.py`: `CONDITION_MAP` (`:4666`, after `N_ENTRY_TAG`)
- Modify: `packages/common/ba2_common/core/rule_builders.py`: `FIELD_EVENT` (`:99`, after `"entry_tag"`)
- Modify: `packages/common/ba2_common/core/rules_export_import.py`: `_FIELD_ABBR` (`:42`)
- Modify: `packages/common/ba2_common/core/trigger_catalog.py`: `_OPTIONS` tuple (`:138-143`)
- Modify: `packages/common/ba2_common/core/rules_documentation.py` (after the `N_ENTRY_TAG` entry, `:509-523`)
- Modify (counts): `packages/common/tests/test_trigger_catalog.py:105-123`
- Create: `packages/common/tests/test_structure_share_registration.py`

**Step 1: failing tests**

```python
"""structure_share is registered everywhere a condition must be (plan 2026-11-06, Task 7)."""
from ba2_common.core.TradeConditions import CONDITION_MAP, StructureShareCondition
from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree
from ba2_common.core.rules_export_import import _abbr_field
from ba2_common.core.rules_documentation import get_event_type_documentation
from ba2_common.core.trigger_catalog import trigger_catalog, triggers_in_category
from ba2_common.core.types import ExpertEventType, is_numeric_event


def test_the_event_type_is_numeric_and_maps_to_its_class():
    et = ExpertEventType("structure_share")
    assert et is ExpertEventType.N_STRUCTURE_SHARE
    assert is_numeric_event(et.value)
    assert CONDITION_MAP[et] is StructureShareCondition
    assert FIELD_EVENT["structure_share"] is et


def test_a_leaf_becomes_a_trigger_and_is_not_dropped():
    tree = {"type": "AND", "conditions": [
        {"id": "o_cbs-structure_share", "field": "structure_share", "op": "<", "value": 40.0}]}
    (trigger,) = triggers_from_condition_tree(tree).values()
    assert trigger == {"event_type": "structure_share", "operator": "<", "value": 40.0}


def test_it_has_a_curated_abbreviation_a_catalog_row_and_documentation():
    assert _abbr_field("structure_share") == "structShare"
    assert "structure_share" in {e.value for e in trigger_catalog()}
    assert "structure_share" in {e.value for e in triggers_in_category("options")}
    assert "structure_share" in get_event_type_documentation()
```
(Confirm the actual accessor names `is_numeric_event` at `types.py:927` and `get_event_type_documentation` in `rules_documentation.py` when writing; if the doc accessor has another name, `grep -n "N_ENTRY_TAG" rules_documentation.py` shows the enclosing dict/function.)

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_registration.py packages/common/tests/test_condition_registry_coverage.py packages/common/tests/test_trigger_catalog.py -q` -> registration test FAILS (`ValueError: 'structure_share' is not a valid ExpertEventType`).

**Step 3: implement**

- `types.py`: after `N_ENTRY_TAG = "entry_tag"`:
  ```python
      # A style MEMBER's committed capital as a percent of the sleeve's equity (plan
      # 2026-11-06 structure-share-condition). An ENTRY gate: ANDed last into each member's
      # entry rule by a style that opts in (``structure_share``); False lets the next member's
      # rule be tried. UNEVALUABLE (False, ERROR logged) when the share cannot be measured.
      N_STRUCTURE_SHARE = "structure_share"
  ```
  and `ExpertEventType.N_STRUCTURE_SHARE.value,` after the `N_ENTRY_TAG` line in `get_numeric_event_values`.
- `TradeConditions.py` `CONDITION_MAP`: `ExpertEventType.N_STRUCTURE_SHARE: StructureShareCondition,`
- `rule_builders.py` `FIELD_EVENT`: `"structure_share": ExpertEventType.N_STRUCTURE_SHARE,` with a one-line comment.
- `rules_export_import.py` `_FIELD_ABBR`: `"structure_share": "structShare",`
- `trigger_catalog.py` `_OPTIONS`: append `'structure_share'`.
- `rules_documentation.py`: entry modelled on `N_ENTRY_TAG`:
  ```python
          ExpertEventType.N_STRUCTURE_SHARE.value: {
              "name": "Structure Share (style member's share of the sleeve)",
              "description": (
                  "For an option ENTRY rule of a style expert: the committed capital of the "
                  "member this rule opens (open structures, assigned-stock cost basis and "
                  "entries submitted this cycle) as a percent of the sleeve's equity. The rule "
                  "passes while the share is below the threshold, so a member that already "
                  "holds its share steps aside and the next member's rule is tried. Written "
                  "by the style builder (style field structure_share), not meant to be typed "
                  "by hand. FALSE, with an error logged, when the share cannot be measured."
              ),
              "type": "numeric",
              "example": "Cap the call-backspread member at 40% of the sleeve: structure_share < 40",
          },
  ```
- `test_trigger_catalog.py::test_the_counts_the_design_agreed`: `"all": 84`, `"options": 14` (docstring: add `83 -> 84 / options 13 -> 14 (2026-11-06): structure_share`).

**Step 4:** rerun the three files -> pass. Then `& $PY -m pytest tests/test_rule_trigger_picker.py tests/test_rule_editor_preserves_hidden_keys.py -q` (UI picker tests that enumerate triggers); fix any enumeration they pin (they list known triggers; add `structure_share` where a closed list is asserted).

**Step 5: commit** `feat(conditions): register structure_share (enum, map, field, catalog, docs)` + trailer.

---

## Task 8: evaluator injects the rule's tag (and nothing else changes)

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActionEvaluator.py:1105-1111` (after the condition is created, before `get_description()` at `:1121`)
- Create: `packages/common/tests/test_structure_share_evaluator.py`

**Step 1: failing tests**

```python
"""The evaluator hands a structure_share condition its rule's entry tag; for every other
condition it is a no-op (plan 2026-11-06, Task 8)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.entry_tag as entry_tag_mod
from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_common.core.types import ExpertEventType

EID, CSP = 7, 11
AT = datetime(2026, 11, 2, 10, 0)


def _rec():
    return SimpleNamespace(instance_id=EID, created_at=AT)


@pytest.fixture
def share_rig(monkeypatch):
    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    monkeypatch.setattr(rm, "transaction_entry_tag", lambda tid: None)
    monkeypatch.setattr(rm, "assigned_stock_lots", lambda eid: [])
    monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: None)
    expert = SimpleNamespace(settings={"risk_manager_mode": "classic_options"})
    monkeypatch.setattr("ba2_common.core.instance_resolver.get_instance_resolver",
                        lambda: SimpleNamespace(get_expert_instance=lambda i: expert))
    account = SimpleNamespace(get_account_snapshot=lambda: SimpleNamespace(equity=100_000.0))
    yield account
    rm.reset_state()


def _action(trigger, actions):
    return SimpleNamespace(name="r", id=1, triggers={"t0": trigger}, actions=actions)


def test_a_structure_share_leaf_is_bound_to_its_rules_entry_tag_and_evaluates(share_rig):
    ev = TradeActionEvaluator(share_rig, "AAPL")
    action = _action({"event_type": "structure_share", "operator": "<", "value": 30.0},
                     {"a0": {"action_type": "buy_call", "entry_tag": CSP}})
    assert ev._evaluate_conditions(action, "AAPL", _rec(), None) is True
    cond = ev.rule_evaluations[0]["conditions"][0]
    assert cond["event_type"] == "structure_share" and cond["calculated_value"] == 0.0


def test_a_rule_whose_entry_action_has_no_tag_fails_the_leaf(share_rig):
    ev = TradeActionEvaluator(share_rig, "AAPL")
    action = _action({"event_type": "structure_share", "operator": "<", "value": 30.0},
                     {"a0": {"action_type": "buy_call"}})
    assert ev._evaluate_conditions(action, "AAPL", _rec(), None) is False


def test_the_binding_is_a_no_op_when_the_rule_has_no_structure_share_leaf(monkeypatch):
    """GA neutrality (c): no structure_share leaf -> the tag helper is never called."""
    def boom(*a, **k):
        raise AssertionError("entry_tag_of_actions must not run without a structure_share leaf")

    monkeypatch.setattr(entry_tag_mod, "entry_tag_of_actions", boom)
    account = MagicMock()
    account.get_iv_rank.return_value = 25.0
    ev = TradeActionEvaluator(account, "AAPL")
    action = _action({"event_type": ExpertEventType.N_IV_RANK.value, "operator": "<=",
                      "value": 40.0},
                     {"a0": {"action_type": "buy_call", "entry_tag": CSP}})
    before = ev.rule_evaluations[:]
    ev._evaluate_conditions(action, "AAPL", None, None)
    assert [c["event_type"] for c in ev.rule_evaluations[0]["conditions"]] == ["iv_rank"]
    assert before == []


def test_only_structure_share_defines_the_hook():
    from ba2_common.core.TradeConditions import CONDITION_MAP
    owners = {et.value for et, cls in CONDITION_MAP.items() if hasattr(cls, "bind_event_action")}
    assert owners == {"structure_share"}
```
(In the no-op test the iv_rank condition may fail its own evaluation depending on the MagicMock account; the assertions only pin that evaluation ran and the helper was not called.)

**Step 2:** `& $PY -m pytest packages/common/tests/test_structure_share_evaluator.py -q -x` -> first test FAILS (`calculated_value`/False: tag never bound -> condition logs "no entry action carrying an entry_tag" and returns False).

**Step 3: implement.** In `_evaluate_conditions`, immediately after the `if not condition:` block (`:1111-1118`) and before `condition_evaluation["condition_description"] = condition.get_description()`:

```python
                # A condition that must know WHICH member its own rule opens (structure_share)
                # defines ``bind_event_action``; no other condition does, so for every other
                # trigger this is one attribute lookup and nothing happens (GA-neutral).
                bind = getattr(condition, "bind_event_action", None)
                if bind is not None:
                    bind(event_action)
```

**Step 4:** rerun -> pass; plus `packages/common/tests/test_iv_rank_condition_display.py packages/common/tests/test_trigger_unknown_event_type_fails.py -q`.

**Step 5: commit** `feat(evaluator): bind a structure_share condition to its rule's entry tag` + trailer.

---

## Task 9: style field `structure_share` (validation + leaf), opt-in

**Files:**
- Modify: `testplatform/ba2test_launcher.py`: `_STYLE_FIELDS` (`:7448`), new `_validate_style_structure_share` (after `_validate_style_risk`, `:7740-7778`), call it in `_validate_style` (`:7797-7880`, after the members loop / before `_validate_style_price_caps`), `_style_entry_rules` (`:7991-8037`, new parameter + helper), the caller in `_build_strategy_style` (`:8305-8306`)
- Create: `testplatform/backend/tests/test_option_style_structure_share.py`

**Step 1: failing tests**

```python
"""The opt-in ``structure_share`` style field (plan 2026-11-06, Task 9)."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

from tests.test_option_grid_foundations import _launcher  # noqa: E402
from tests.test_option_style_builder import _custom, _leaves, _write  # noqa: E402
from tests.test_option_style_risk import _RISK  # noqa: E402

SHARE = {"min": 20, "max": 60, "step": 10}


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    m = _launcher()
    monkeypatch.setattr(m, "_STYLE_ACTIVE", None)
    monkeypatch.setattr(m, "_MARKET_CONDITION_PROFILES", ())
    monkeypatch.setattr(m, "_BENCHMARK_REGIME_GATES", False)
    yield


def _share_leaves(rule):
    return [x for x in _leaves(rule["conditions"]) if x.get("field") == "structure_share"]


def _opted_in(m, tmp_path, members, share=SHARE, **extra):
    return _custom(m, tmp_path, members, structure_share=share, risk=_RISK, **extra)


def test_every_option_entry_rule_of_an_opted_in_style_ends_with_its_members_leaf(tmp_path):
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}, {"key": "O_CONVEX"}, {"key": "O_PMCC"}])
    assert [r["id"] for r in s.entry_rules] == [
        "o_cbs-entry", "o_convexc-entry", "o_convexp-entry", "o_pmcc-entry"]
    expected = {"o_cbs-entry": "o_cbs-structure_share",
                "o_convexc-entry": "o_convex-structure_share",      # ONE gene for both arms
                "o_convexp-entry": "o_convex-structure_share",
                "o_pmcc-entry": "o_pmcc-structure_share"}
    for rule in s.entry_rules:
        root = rule["conditions"]["conditions"]
        (leaf,) = _share_leaves(rule)
        assert root[-1] is leaf, "the leaf must be LAST in the AND list"
        assert leaf["id"] == expected[rule["id"]]
        assert leaf["comparison"] == "<" and leaf["optimize"] is True
        assert (leaf["value_min"], leaf["value_max"], leaf["value_step"]) == (20.0, 60.0, 10.0)
        assert "toggle_optimize" not in leaf or not leaf["toggle_optimize"]


def test_the_leaf_stays_last_behind_market_and_spy_regime_gates(tmp_path, monkeypatch):
    m = _launcher()
    monkeypatch.setattr(m, "_MARKET_CONDITION_PROFILES", ("ohlcv-v1", "ta-structure-v1"))
    monkeypatch.setattr(m, "_BENCHMARK_REGIME_GATES", True)
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}])
    (rule,) = s.entry_rules
    assert rule["conditions"]["conditions"][-1]["field"] == "structure_share"
    assert len(rule["conditions"]["conditions"]) > 8       # the other gates are still there


def test_the_gene_space_has_exactly_one_value_gene_per_member_and_no_enabled_toggle(tmp_path):
    from app.services.strategy_param_space import _range_entry, collect_param_space
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}, {"key": "O_CONVEX"}])
    space = collect_param_space(s)
    mine = {k: v for k, v in space.items() if "structure_share" in k}
    assert set(mine) == {"cond:o_cbs-structure_share:value",
                         "cond:o_convex-structure_share:value"}
    assert mine["cond:o_cbs-structure_share:value"] == _range_entry(20.0, 60.0, 10.0,
                                                                    is_int=False)


def test_the_leaf_converts_to_a_trigger_beside_the_rules_entry_tag(tmp_path):
    from ba2_common.core.rule_builders import checked_entry_tag, triggers_from_condition_tree
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}])
    (rule,) = s.entry_rules
    triggers = triggers_from_condition_tree(rule["conditions"])
    shares = [t for t in triggers.values() if t["event_type"] == "structure_share"]
    assert shares == [{"event_type": "structure_share", "operator": "<", "value": 60.0}]
    assert checked_entry_tag(rule["actions"][0]) == m._STYLE_MEMBER_TAGS["O_CBS"]


def test_equity_entry_members_get_no_leaf(tmp_path):
    m = _launcher()
    s = _opted_in(m, tmp_path, [{"key": "O_CBS"}, {"key": "O_CC"}])
    by_id = {r["id"]: r for r in s.entry_rules}
    assert _share_leaves(by_id["o_cbs-entry"])
    cc = [r for r in s.entry_rules if r["id"] != "o_cbs-entry"]
    assert cc and all(not _share_leaves(r) for r in cc)


def test_removing_the_leaves_gives_back_the_style_without_the_field(tmp_path):
    m = _launcher()
    members = [{"key": "O_CBS"}, {"key": "O_CONVEX"}]
    plain = _custom(m, tmp_path, members, risk=_RISK)
    opted = _opted_in(m, tmp_path, members)

    def strip(rules):
        out = json.loads(json.dumps(rules, default=str))
        for r in out:
            r["conditions"]["conditions"] = [c for c in r["conditions"]["conditions"]
                                             if c.get("field") != "structure_share"]
        return out

    assert strip(opted.entry_rules) == json.loads(json.dumps(plain.entry_rules, default=str))
    assert opted.exit_rules == plain.exit_rules


@pytest.mark.parametrize("band,msg", [
    ({"min": 0, "max": 60, "step": 10}, "0 <"),
    ({"min": 20, "max": 101, "step": 10}, "<= 100"),
    ({"min": 70, "max": 60, "step": 10}, "min <= max"),
    ({"min": 20, "max": 60, "step": 0}, "step"),
    ({"min": 20, "max": 60}, "exactly"),
    ({"min": 20, "max": 60, "step": 10, "x": 1}, "exactly"),
    ({"min": True, "max": 60, "step": 10}, "number"),
    ({"min": "20", "max": 60, "step": 10}, "number"),
    ([20, 60, 10], "object"),
])
def test_a_bad_band_is_refused_by_name(tmp_path, band, msg):
    m = _launcher()
    with pytest.raises(ValueError, match=msg):
        m._load_style_spec(_write(tmp_path, {"x": {
            "status": "candidate", "price_cap_policy": "none", "risk": _RISK,
            "members": [{"key": "O_CBS"}], "structure_share": band}}), "x")


def test_structure_share_without_a_risk_block_is_refused(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match="risk"):
        m._load_style_spec(_write(tmp_path, {"x": {
            "status": "candidate", "price_cap_policy": "none",
            "members": [{"key": "O_CBS"}], "structure_share": SHARE}}), "x")


def test_structure_share_on_a_style_with_no_option_entry_member_is_refused(tmp_path):
    m = _launcher()
    with pytest.raises(ValueError, match="option entry"):
        m._load_style_spec(_write(tmp_path, {"x": {
            "status": "candidate", "price_cap_policy": "none", "risk": _RISK,
            "members": [{"key": "O_CC"}], "structure_share": SHARE}}), "x")
```

**Step 2:** `& $PY -m pytest testplatform/backend/tests/test_option_style_structure_share.py -q -x` -> FAIL (`ValueError: style 'x': unknown style field(s) ['structure_share']`).

**Step 3: implement** in `ba2test_launcher.py`.

`_STYLE_FIELDS = {"status", "note", "members", "price_cap_policy", "convex_fitness_waiver", "risk", "structure_share"}`

```python
#: ``structure_share`` (plan 2026-11-06): the opt-in per-member share cap. {"min","max","step"} in
#: percent of the sleeve's equity; ONE searched value per member, the cap is always on.
_STYLE_STRUCTURE_SHARE_FIELDS = {"min", "max", "step"}


def _validate_style_structure_share(where: str, style: dict) -> None:
    """The ``structure_share`` band: exactly min/max/step, plain numbers, 0 < min <= max <= 100,
    step > 0; needs the option risk manager (the condition reads the sleeve rails' equity and the
    pending charges only that mode records) and at least one option-entry member (otherwise the
    field would be inert)."""
    where = f"{where} structure_share"
    band = style["structure_share"]
    if not isinstance(band, dict):
        raise ValueError(f"{where}: must be an object, got {band!r}")
    if set(band) != _STYLE_STRUCTURE_SHARE_FIELDS:
        raise ValueError(f"{where}: must be exactly {sorted(_STYLE_STRUCTURE_SHARE_FIELDS)}, "
                         f"got {sorted(band)}")
    for field in sorted(_STYLE_STRUCTURE_SHARE_FIELDS):
        v = band[field]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{where}: {field}={v!r} must be a finite number")
    if not (0 < band["min"] <= band["max"] <= 100):
        raise ValueError(f"{where}: needs 0 < min <= max <= 100, got {band}")
    if band["step"] <= 0:
        raise ValueError(f"{where}: step must be > 0, got {band['step']}")
    if "risk" not in style:
        raise ValueError(f"{where}: needs a risk block (risk_manager_mode classic_options); "
                         f"without the option risk manager the share is never kept")
    if not any(mem["key"] not in _STYLE_EQUITY_ENTRY for mem in style["members"]):
        raise ValueError(f"{where}: the style has no option entry member to apply it to")
```
(`math` is imported function-locally elsewhere in the launcher, e.g. `:7521`, not at module level: add `import math` as the first line INSIDE `_validate_style_structure_share`.)

In `_validate_style`, after the members loop / `keys` are known (before `ids = {k: ...}`):
```python
    if "structure_share" in style:
        _validate_style_structure_share(where, style)
```

`_style_entry_rules(key, tag, src_rules, action_overrides, structure_share=None)`: after the `if not tagged: raise` check inside the `for rule in rules:` loop add

```python
        if structure_share is not None and any(at in option_entries for at in types):
            _style_append_structure_share(key, rule, structure_share)
```
and the helper above `_style_entry_rules`:

```python
def _style_append_structure_share(key: str, rule: dict, band: dict) -> None:
    """Append the member's ``structure_share`` leaf LAST in the rule's root AND (after every
    gate the member's own builder authored, market-condition and SPY-regime gates included, so
    all their leaf indexes are unchanged). One id per MEMBER, reused by every arm of a group
    (O_CONVEX's two arms share one gene). ``optimize`` with the style's band gives the gene
    ``cond:<member>-structure_share:value``; there is deliberately NO ``toggle_optimize``: the
    cap is always on, only its value is searched. The authored value is the LOOSEST (max)."""
    from ba2_common.core.rule_models import normalize_tree

    root = rule.get("conditions")
    if not isinstance(root, dict) or root.get("type") != "AND":
        raise ValueError(f"{key} entry rule {rule['id']!r}: its root is not an AND group, so a "
                         f"structure_share leaf appended to it would not be ANDed")
    root.setdefault("conditions", []).append(normalize_tree({
        "id": f"{key.lower()}-structure_share", "field": "structure_share",
        "field_type": "numeric", "comparison": "<", "value": float(band["max"]),
        "optimize": True, "value_min": float(band["min"]), "value_max": float(band["max"]),
        "value_step": float(band["step"])}))
```
Caller in `_build_strategy_style`:
```python
            e_rules, e_map = _style_entry_rules(
                key, tag, src.entry_rules, mem.get("action_overrides"),
                style["structure_share"] if "structure_share" in style else None)
```

**Step 4:** rerun -> pass. Regression (one process each, in this order): `test_option_style_ga_neutral.py` (goldens: MUST still pass: no style in the goldens carries the field yet), `test_option_style_builder.py`, `test_option_style_spec.py`, `test_option_style_risk.py`.

**Step 5: commit** `feat(style): opt-in structure_share style field (leaf last in each member's entry rule)` + trailer.

---

## Task 10: job C -- `long` opts in; triage dependants

**Files:**
- Modify: `tools/option_styles.stage2.json` (style `long`, after `"price_cap_policy": "none",` at line 54)
- Modify: `testplatform/backend/tests/test_option_style_long_job_c.py` (assert the field; fix any gene-space assumption that now sees +4 genes)
- Modify: `testplatform/backend/tests/test_option_style_ga_neutral.py` (add the long test)

**Step 1: failing tests.** Append to `test_option_style_structure_share.py`:

```python
def test_job_c_long_opts_in_with_20_to_60_and_mid_premium_do_not():
    m = _launcher()
    stage2 = str(__import__("pathlib").Path(__file__).resolve().parents[3]
                 / "tools/option_styles.stage2.json")
    assert m._load_style_spec(stage2, "long")["structure_share"] == {"min": 20, "max": 60, "step": 10}
    for name in ("mid", "premium"):
        assert "structure_share" not in m._load_style_spec(stage2, name)
```
Append to `test_option_style_ga_neutral.py`:

```python
def test_long_with_the_field_differs_from_its_golden_only_by_the_four_leaves_and_genes():
    m = _launcher()
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))["long"]
    got = snapshot(m, "long")
    assert got["exit"] == golden["exit"]
    assert set(got["genes"]) - set(golden["genes"]) == {
        f"cond:{k}-structure_share:value" for k in ("o_cbs", "o_leap", "o_pmcc", "o_convex")}
    assert all(got["genes"][g] == golden["genes"][g] for g in golden["genes"])
    for g_rule, n_rule in zip(golden["entry"], got["entry"]):
        kept = [c for c in n_rule["conditions"]["conditions"] if c.get("field") != "structure_share"]
        assert kept == g_rule["conditions"]["conditions"]
        assert n_rule["conditions"]["conditions"][-1]["field"] == "structure_share"
        assert {k: v for k, v in n_rule.items() if k != "conditions"} == \
               {k: v for k, v in g_rule.items() if k != "conditions"}
```

**Step 2:** run both -> FAIL (`KeyError: 'structure_share'`).

**Step 3: implement.** Insert in `tools/option_styles.stage2.json` after line 54 (`"price_cap_policy": "none",` of `long`):

```json
      "structure_share": {"min": 20, "max": 60, "step": 10},
```
(textual edit, keeping the file's 6-space indent; do not reformat the file.)

**Step 4:** run `test_option_style_structure_share.py`, `test_option_style_ga_neutral.py`, then `test_option_style_long_job_c.py` and `test_option_style_spec.py`. Triage:
- `test_job_c_policy_assembles_offline_against_the_real_space` asserts `all(set(s) == set(space) for s in seeds)`: the seed policy fills genes absent from the source with the `"zero"` policy; if the four new genes break the assembly, give them the same treatment as `entry:*:enabled` genes in `L._assemble_seed_population` / `tools/build_style_seed_bundle.py` (`_style_translate_key` finds no source gene for a `cond:*-structure_share:value` key) -- read the failure first, then fix the narrowest place (default to the leaf's authored value = max); add a test line asserting the seed's structure_share genes equal 60.0.
- Any test pinning the long gene COUNT (122) moves to 126; update the number with a comment.

**Step 5: commit** `feat(job-c): long style opts into structure_share 20..60 step 10` + trailer.

---

## Task 11: GA-neutrality proofs (rail inputs unchanged; no state without the leaf)

(a) is Task 0/9/10's goldens (re-run here). This task adds (b) and the whole-feature inertness check.

**Files:**
- Create: `packages/common/tests/test_structure_share_ga_neutral.py`

**Step 1: tests**

```python
"""GA-NEUTRALITY (owner 2026-11-06): the new tag on _PendingCharge and the record_submitted
bookkeeping never change an admit_option_entry verdict or any rail input."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

RAILS: Dict[str, Any] = {"max_concurrent_structures": 10, "max_deployment_pct": 40.0,
                         "max_notional_leverage": 3.0, "undefined_risk_max_pct": 20.0,
                         "circuit_breaker_pct": 20.0}
EID = 7


class _Expert:
    settings = {"risk_manager_mode": "classic_options"}

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in RAILS:
            raise ValueError(key)
        return RAILS[key]


class _Account:
    id = 1

    def get_account_info(self):
        return {"equity": 100_000.0, "cash": 100_000.0, "balance": 100_000.0}

    get_account_snapshot = ReadOnlyAccountInterface.get_account_snapshot
    true_equity = ReadOnlyAccountInterface.true_equity

    def cash_available_for_delivery(self):
        return 100_000.0


def _legs(symbol: str) -> List[OptionLeg]:
    return [OptionLeg(contract_symbol=f"{symbol}260116P100", side=OrderDirection.SELL,
                      ratio_qty=1, option_type=OptionRight.PUT, strike=100.0,
                      expiry=date(2026, 1, 16), underlying=symbol)]


def _run(monkeypatch, tag: Optional[int]):
    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: {100, 101, 102})
    seen, verdicts = [], []
    real_admit = rm.admit

    def spy(cands, book, equity, rails, breaker, cash):
        seen.append((tuple(cands), book, equity, dict(rails), breaker, cash))
        return real_admit(cands, book, equity, rails, breaker, cash)

    monkeypatch.setattr(rm, "admit", spy)
    for i, (sym, max_loss) in enumerate([("AAPL", 4_000.0), ("MSFT", 9_000.0),
                                         ("NVDA", 30_000.0)]):   # the 3rd breaks the 40% cap
        v = rm.admit_option_entry(
            expert=_Expert(), account=_Account(), expert_instance_id=EID, underlying=sym,
            option_strategy="cash_secured_put", legs=_legs(sym), quantity=1,
            max_loss_per_contract=max_loss)
        verdicts.append((v.allowed, v.phrase, v.reason, v.detail))
        if v.allowed:
            kw = {} if tag is None else {"entry_tag": tag}
            rm.record_submitted(EID, 100 + i, v.candidate, **kw)
    return verdicts, seen


def test_a_tagged_submission_changes_no_verdict_and_no_rail_input(monkeypatch):
    plain = _run(monkeypatch, None)
    tagged = _run(monkeypatch, 14)
    assert plain[0] == tagged[0]
    assert plain[1] == tagged[1]
    assert [v[0] for v in plain[0]] == [True, True, False]       # the scenario exercises a refusal


def test_without_a_structure_share_condition_no_share_cache_is_ever_built(monkeypatch):
    _run(monkeypatch, 14)
    assert rm._SHARE_CACHE == {}


def test_untagged_submissions_touch_no_share_state(monkeypatch):
    _run(monkeypatch, None)
    assert rm._SHARE_CACHE == {} and rm._SHARE_EPOCH == {}
```

**Step 2/3:** these pass if Tasks 2-4 kept the promise; if any assertion fails, FIX THE CODE (a `record_submitted` side effect leaking into rail inputs), not the test. Expected on first run: pass.

**Step 4: the neutrality set, one process each:**
- `& $PY -m pytest packages/common/tests/test_structure_share_ga_neutral.py -q`
- `& $PY -m pytest testplatform/backend/tests/test_option_style_ga_neutral.py -q` (goldens mid/premium/long-minus-field + long-with-field delta)
- `& $PY -m pytest packages/common/tests/test_structure_share_evaluator.py -q` (the no-op binding)
- `& $PY -m pytest packages/common/tests/test_option_risk_manager_wiring.py packages/common/tests/test_option_risk_manager_seam.py -q`
- `& $PY -m pytest testplatform/backend/tests/backtest/test_option_order_fill_volume_sizing.py -q`

**Step 5: commit** `test(structure-share): GA-neutrality -- rail inputs and verdicts unchanged without the leaf` + trailer.

---

## Task 12: release bookkeeping (GA-neutral: package + TEST bump, NO minimum raise)

**Files:**
- Modify: `packages/common/ba2_common/version.py` (`PACKAGE_VERSION`, currently `2026.11.00029`)
- Modify: `packages/common/pyproject.toml:7` (`version`, same string)
- Modify: `testplatform/version.py` (`TEST_APP_VERSION`, currently `2026.11.0171`)
- Do NOT modify: `testplatform/required_package_versions.py` (ba2_common stays `2026.11.00028`), `testplatform/ga_neutral_package_paths.py`, `ba2_trade_platform/version.py` (nothing in `ba2_trade_platform/` changed)
- Already done in this plan's commit: `docs/plans/2026-11-06-structure-share-condition-design.md` "Release" section (GA-neutral wording)

**Step 1:** `git fetch` is NOT needed offline; re-read the three current version strings and `git log origin/dev -1 -- packages/common/ba2_common/version.py testplatform/version.py`. If `origin/dev` advanced past this branch's base, rebase first (only if the user asked; otherwise bump from the values in the tree). Month is the CURRENT month (`2026.11`), the build counter keeps running.

**Step 2: failing check.** `& $PY tools/check_package_versions.py --base origin/dev --include-worktree` -> exit 1: `ba2_common` shipped code changed without a PACKAGE_VERSION bump.

**Step 3: bump** (values assume no concurrent bumps; use current+1 otherwise):
- `version.py`: `PACKAGE_VERSION = "2026.11.00030"`
- `pyproject.toml` line 7: `version = "2026.11.00030"`
- `testplatform/version.py`: `TEST_APP_VERSION = "2026.11.0172"` (testplatform changed in Tasks 0, 9, 10; the TEST bump is what satisfies the GA rule without raising the minimum, and makes workers sync for job C)

**Step 4:** `& $PY tools/check_package_versions.py --base origin/dev --include-worktree` -> exit 0. Then the unit test that wraps it and the version-consistency tests: `& $PY -m pytest testplatform/backend/tests/test_check_package_versions_tool.py -q` (one process).

**Step 5: final targeted run, one pytest process each** (everything this plan touched):
`packages/common/tests/test_structure_share_*.py`, `packages/common/tests/test_condition_registry_coverage.py`, `packages/common/tests/test_trigger_catalog.py`, `packages/common/tests/test_entry_tag_condition.py`, `testplatform/backend/tests/test_option_style_*.py`, `tests/test_rule_trigger_picker.py`.

**Step 6: commit** `chore(release): ba2_common 2026.11.00030, TEST 2026.11.0172 (structure_share; GA-neutral, minimum unchanged)` + trailer. Do not push. At merge time: job boundary only (TEST bump makes every worker re-sync); roll-out notes in the design doc's Release section.

---

## Spec coverage matrix (design -> task)

| Design item | Task |
|---|---|
| `StructureShareCondition`, `<` X, member from the rule's entry action | 6, 8 |
| book + assigned stock + covered call + pending counted; unmeasurable -> False + ERROR | 3, 6 |
| `_PendingCharge` gains the tag | 2 |
| per-cycle cache (sleeve key + decision time), increment on `record_submitted`, resets, per-thread isolation | 4 |
| BT/live parity | 5 |
| wheel share holds through assignment + covered call | 3 |
| opt-in style field, validation, always-on, one gene per member, last in each member's entry rule | 9 |
| job C `long` 20..60 step 10, O_CONVEX covered | 9, 10 |
| registration + registry coverage | 7 |
| GA-neutral release (owner rule): goldens, rail inputs, evaluator no-op, no minimum raise | 0, 8, 11, 12 |
