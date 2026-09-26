# Option Stage 2 (Style Experts): Build Tasks, Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development) to implement this plan task by task. Nothing here
> launches a grid. The seeding POLICY and the style MEMBERSHIP stay user inputs; this plan builds
> only the mechanism.

**Goal:** Build the CODE for option-grid stage 2. The pieces are: config-driven style groups
(one expert per trading style, each member's entry toggleable, each member's exits kept and
separately expressible); multi-source seeding from stage-1 and LEAPS-grid results, with the
policy passed in; a stage-2 driver entry with a sound job identity; the wheel and overlay checks
for the premium group; and a pilot plus cost hook to size `--parallel` for 820 names. Stage 1 and
the equity paths must not move.

**Architecture.** A new launcher key, `O_STYLE`, builds ONE strategy from a style spec (JSON,
`--style-config F --style NAME`). The builder runs each member's OWN single-job builder, so every
member keeps the exact entry rule, exit bands, overlay rules and genes it had in stage 1 or in the
LEAPS grid. Entry rules become toggleable. Exit rule and condition ids get a member prefix, and
each exit rule gets ANDed with an `entry_tag == <member code>` leaf, which is what lets one
OPEN_POSITIONS ruleset carry per-member exits. The tag is a new, shared (BT and live) entry fact.
The option entry action writes it onto the parent order row, beside `earnings_event_date` and
`max_loss_per_contract`, and a new `EntryTagCondition` reads it back. Seeding works in three
parts:
- a read-only tool resolves the policy's sources (optimizations, distinct TOP-N ranks, persisted
  backtests) into a seed BUNDLE file;
- the launcher, which has the real target gene space, translates each source's gene keys through
  the builder's id map, fills un-seeded members from the template's AUTHORED values (never
  `min`), optionally narrows ranges, and refuses loudly on any unknown or dropped key;
- the optimization handler encodes the seeds STRICTLY.

A new `--profile style` in `tools/run_options_matrix.py` and a new `tools/stage2_run.sh` launch
it. The job-name digest covers the resolved style spec and the seed bundle's content.

**Tech stack:** Python 3.11 (local) / 3.13 (remote227), pytest, SQLModel/SQLite, DEAP (via
`genetic.py`), NiceGUI-free backend services.

**Inputs this plan implements:**
- `docs/plans/2026-09-25-option-stage2-plan.md` (the decisions: 2-3 style experts, the full 820
  universe, market conditions on, every stage-1 flag, the seeding policy decided later);
- `docs/superpowers/specs/2026-08-27-option-ga-grid-design.md` §5 Stage 2, §7, §7.1;
- `docs/superpowers/specs/2026-08-31-leaps-grid-design.md` (O_LEAP / O_PMCC; O_CONVEX is the
  convex-harvest grid).

**Worktree:** `C:\Users\basti\Documents\dev\BA2-stage2`, branch `plan/option-stage2` (based on
dev `3b3465c8`). Do the implementation on a fresh branch cut from this one, e.g.
`feat/option-stage2`.

---

## 0. What the code already has, and what this plan therefore SKIPS

These were checked against the code on 2026-09-25:

| Spec item | State in code | Action |
|---|---|---|
| Shared `shared-rel_volume` / `shared-gate_confidence` ids | Already emitted with the SAME id in single-structure jobs and groups (`_option_entry_rule`, `_relative_volume_gate`). Pinned by `test_option_grid_foundations::test_single_and_group_jobs_use_the_SAME_key_for_a_shared_gate`. | Keep. The seeding translation treats `cond:shared-*` as SHARED keys. No key rename. |
| Selection-weight genes shared per half (`optsel:<half>:<w>`) | Already keyed so a single job and a group emit the same key. | Keep. Treated as SHARED keys. |
| Wheel's entry gene keys equal O_CSP's | Yes (`o_csp-*`; the market leaves are re-prefixed `o_wheel-market-*`). Pinned by `test_wheel_reuses_O_CSPs_entry_gene_keys_verbatim`. | Keep (single builds). **STALE for styles since 2026-09-26:** inside a style the wheel's entry is `o_wheel-o_csp-*`, so a stage-1 O_WHEEL genome translates through the id map, not by identity (see "Tasks 5-6 as built"). |
| `_hold_assigned_stock` for a group key | Already "any member" over `_OPTION_GROUPS`. Pinned by `tests/backtest/test_wheel_assignment.py::test_a_group_containing_the_wheel_still_holds_its_assigned_stock`. | Extend to `O_STYLE` (Task 7). |
| "Two live tests assert the opposite" (§7.1) | They assert things about `_OPTION_GROUPS`: the full-notional trio is in no group, and `rel_volume` is shared. This plan does NOT add a key to `_OPTION_GROUPS`/`_OPTION_GROUPS_ALL`, because style groups are spec-driven. Both tests stay TRUE. | Skip the rewrite. Task 7 adds a test that `O_STYLE` never enters `_OPTION_GROUPS`, so those premises keep holding. |
| `OS_ALL` | Does not exist. It is superseded by the 2-3 style experts. | Not built. |
| Stage-1 flags | `option_min_one_contract` (not for O_CONVEX*), the entry-cross band 0.75-1.0, rel_volume ≤ 1.5 and the debit IV/RV floor 1.0 all live in code on `_option_entry_action_for`/the gate builders. A style member built by its own builder inherits them. `macro_short_side=mirror` applies to any key in `_OPTION_STRATEGY_KEYS`. `--option-size-within-fill-volume` is a run flag. | No code. The driver REQUIRES the run flag (Task 17). Task 7 pins that the fixed settings reach `O_STYLE`. |
| Per-trial wall time + RSS | Already logged per individual (`_log_trial_memory`: `mem gen … \| Ns \| rss NMB \| …`). | Add PRIVATE bytes and a report tool (Task 19). |

---

## Ground rules (read before any task)

- **Python.** `PY=C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe`.
- **Test commands.** Run the three suites as SEPARATE invocations, never concurrently. The memory
  note on worktree test-running quirks has the details.
  - packages: from the worktree root, `%PY% -m pytest packages/common/tests/<file> -q -p no:cacheprovider`
  - backend: `cd testplatform/backend` first (its `pytest.ini` sets `pythonpath`), then
    `%PY% -m pytest tests/<file> -q -p no:cacheprovider`
  - root: `%PY% -m pytest tests -q -p no:cacheprovider`
- **No-impact is a hard gate.**
  - The stage-1 discovery job names and commands are pinned by Task 0.
  - `tests/backtest/test_equity_golden_run.py` and `test_engine_golden_regression.py` must stay
    byte-identical.
  - The option goldens must ALSO stay byte-identical. Nothing here changes a single-structure
    path; a change is a bug.
- **Loud, never silent.** Every unknown key, dropped key, out-of-range seed, mixed-cap group or
  inadmissible member is a refusal that names the key. There is no `.get(k, default)` on config.
  The memory notes "No silent failure anywhere" and the CLAUDE.md config rule both apply.
- **BT/live parity.** The `entry_tag` fact and condition live in `packages/common` and are shared
  by both runtimes. No backtest-only branch.
- **Do not edit `tools/stage1_run.sh`.** Stage 1 is running from a checkout on remote227 (see the
  memory note "NEVER EDIT A RUNNING SHELL SCRIPT"). Stage 2 gets its own script.
- **Do not merge into, or pull in, the remote227 stage-1 checkout mid-job.** Do not bump
  `version.py` while a grid runs (worker version match). Bump `TEST_APP_VERSION` once, in the final
  task, at merge time. Everything is under `testplatform/`, `tools/` or `packages/`, so the
  CLAUDE.md table requires the TEST bump only.
- **Commits.** One commit per task, message `feat(stage2): …` (or `test(stage2): …`), ending with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ur9PyFzwcdHKmnb4byPiKb
  ```
- **Subagents** (if you use subagent-driven development): implementers and quality reviewers on
  opus, spec reviewers on sonnet.

---

## Task overview (size: S ≤ 1 h, M 1-3 h, L 3-6 h)

| # | Task | Size | Depends on |
|---|---|---|---|
| 0 | Baseline suites + pin stage-1 discovery identity | S | – |
| 1 | `entry_tag` action param → parent order row (packages/common) | M | – |
| 2 | `EntryTagCondition` + registries (packages/common) | M | 1 (field name only) |
| 3 | Style spec: loader, validator, member tag table (launcher) | M | – |
| 4 | Candidate style configs as data (4 styles, marked candidate) | S | 3 |
| 5 | Style builder: entry side | M | 3 |
| 6 | Style builder: member-prefixed, tag-scoped exits + id maps | L | 5, 2 |
| 7 | Register `O_STYLE`: CLI, run-config rules, refusals, iterating tests | M | 6 |
| 8 | Engine reach: tag leaf becomes a trigger; per-member isolation | M | 1, 2, 6 |
| 9 | Premium group: wheel / overlay / concentration checks | M | 7 |
| 10 | `encode_params_strict` in `genetic.py` | S | – |
| 11 | Handler: `param_space_for_optimization`, `rangeOverrides`, `seedPopulation` | M | 10 |
| 12 | `authored_genome` (template values for every strategy gene) | M | – |
| 13 | `style_seeding.translate_member_genes` | M | 6 |
| 14 | `style_seeding.assemble_individual` + `narrow_ranges` | M | 12, 13 |
| 15 | `tools/build_style_seed_bundle.py` (read-only DB extraction) | M | – |
| 16 | Launcher: `--seed-population` / `--seed-check-only` | M | 7, 11, 14, 15 |
| 17 | Driver `--profile style` + identity digest + dry-run | M | 7 |
| 18 | `tools/stage2_run.sh` + wiring test | S | 17 |
| 19 | Cost hook: private bytes per trial + `tools/stage2_cost_report.py` | M | – |
| 20 | Pilot command and runbook check (dry-run only here) | S | 18, 19 |
| 21 | No-impact gate, full suites, version bump | S | all |

Tasks 1-2, 3-4, 10, 12, 15 and 19 are independent starting points.

---

### Task 0: Baseline + pin the stage-1 discovery identity

**Files:**
- Create: `testplatform/backend/tests/test_stage1_identity_unchanged.py`

**Step 1: Run the three full suites on the untouched branch.** Record the pass/fail counts and
the failing test ids in the task report. Per memory, the dev baseline is about 25 failures in
7 files, and the known root-suite failures are the two in `test_no_zero_coercion.py`. Every later
task compares against this list, never against "green". Also record
`sha256(tools/stage1_run.sh)`; Task 18 pins it.

**Step 2: Write the identity pin (it must PASS on the baseline).**

```python
"""Stage-2 work must not move a single stage-1 job name or command.

The discovery name is the resume/SKIP key of every stage-1 checkpoint (tools/run_options_matrix.py
discovery_name). This pins the names and commands of the -st1fix-shaped campaign, computed
machine-independently: the launcher is passed as the entry-point name "ba2-test" (so the digest
reads this checkout's _EXPERT_OPT through _CHECKOUT_LAUNCHER), os.path.abspath is the identity, and
the store env keys are cleared.
"""
import importlib.util
import os
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("rom_pin", ROOT / "tools/run_options_matrix.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

_ENV = ("BA2_HOME", "BACKTEST_OPTIONS_STORE", "BACKTEST_OPTIONS_PARQUET_ROOT",
        "BACKTEST_OPTIONS_RISK_FREE_RATE", "TASTYTRADE_OPTIONS_HISTORY_FLOOR")

_ARGV = ["--profile", "discovery", "--screener-gate-store", "STORE", "--max-stock-price", "0",
         "--option-size-within-fill-volume", "--experts", "DeterministicScorer",
         "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
         "--market-condition-manifest", "ohlcv-v1=aaaa,ta-structure-v1=bbbb",
         "--fitness", "option_car_target_soft30", "--name-suffix=-st1fix"]

#: Filled ONCE from the baseline commit (Step 3) and never edited afterwards.
PINNED: dict = {}


def _jobs(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(M.os.path, "abspath", lambda p: p)
    args = M.resolve_args(M.build_parser(), _ARGV)
    uni = "AAPL,MSFT,KO"
    out = {}
    for name, expert, strat, mode in M.planned_jobs(args, "ba2-test", ["DeterministicScorer"],
                                                    args.strategies.split(","), uni):
        out[name] = shlex.join(M.build_cmd(args, "ba2-test", name, expert, strat, uni, mode))
    return out


def test_stage1_discovery_names_and_commands_are_unchanged(monkeypatch):
    got = _jobs(monkeypatch)
    assert got == PINNED, "a stage-1 job name or command moved -- stage 2 must not touch stage 1"
```

**Step 3: Fill `PINNED`.** Temporarily print `_jobs(...)` (for example with
`%PY% -c` plus a monkeypatch shim, or a one-off `print` in the test). Paste the 16-entry dict as a
literal and remove the print. Run it and expect PASS.

**Step 4: Commit.** `test(stage2): pin stage-1 discovery job identity before stage-2 work`

---

### Task 1: `entry_tag`, an action param carried onto the parent order row (packages/common)

**Why.** One OPEN_POSITIONS ruleset serves every member of a style expert, and it is evaluated per
SYMBOL against the oldest entry order. Per-member exits therefore need to know which member opened
the position. `Transaction.option_strategy` cannot tell: O_LC, O_LEAPC and O_CONVEXC are all
`long_call`. So the entry writes an integer tag on its parent order row. That is the same seam, and
the same "read back, never reconstruct" rule, as `earnings_event_date` (design 2026-08-29 S8.2).

**Files:**
- Modify: `packages/common/ba2_common/core/rule_builders.py` (the `_OPTION_ACTION_PARAM_KEYS`
  tuple, ~line 372)
- Modify: `packages/common/ba2_common/core/TradeActionEvaluator.py` (the forwarded option param
  tuple, ~line 55-70)
- Modify: `packages/common/ba2_common/core/TradeActions.py` (`_OptionEntryAction.__init__`
  ~2395/2457, `_submit_option_order` entry facts ~3527-3548)
- Create: `packages/common/ba2_common/core/entry_tag.py`
- Test: `packages/common/tests/test_entry_tag_fact.py`

**Step 1: Write the failing tests.** Reuse the account/action fixture pattern of
`packages/common/tests/test_option_min_one_contract_floor.py`, which already builds a
`_OptionEntryAction` against a fake options account and reads the stored order row.

```python
import pytest
from ba2_common.core.rule_builders import action_from_rule
from ba2_common.core.entry_tag import ORDER_ENTRY_TAG_KEY, order_entry_tag


def test_rule_builder_forwards_option_entry_tag():
    cfg = action_from_rule({"action_type": "buy_call", "option_entry_tag": 7})["act"]
    assert cfg["entry_tag"] == 7


def test_rule_builder_omits_an_absent_tag():
    cfg = action_from_rule({"action_type": "buy_call"})["act"]
    assert "entry_tag" not in cfg


def test_the_tag_rides_the_parent_order_row(buy_call_with):      # fixture adapted from
    order = buy_call_with(entry_tag=14).submitted_order()          # test_option_min_one_contract_floor
    assert order.data[ORDER_ENTRY_TAG_KEY] == 14
    assert order_entry_tag(order) == 14


def test_no_tag_writes_nothing(buy_call_with):
    order = buy_call_with().submitted_order()
    assert ORDER_ENTRY_TAG_KEY not in (order.data or {})
    assert order_entry_tag(order) is None


@pytest.mark.parametrize("bad", [0, -3, 1.5, "x", True])
def test_a_bad_tag_is_refused_at_construction(buy_call_with, bad):
    with pytest.raises(ValueError, match="entry_tag"):
        buy_call_with(entry_tag=bad)


def test_multi_leg_structures_tag_the_PARENT_not_a_leg(vertical_with):
    parent = vertical_with(entry_tag=3).submitted_order()
    assert parent.contract_symbol is None and parent.data[ORDER_ENTRY_TAG_KEY] == 3
```

**Step 2: Run them.** `%PY% -m pytest packages/common/tests/test_entry_tag_fact.py -q -p no:cacheprovider`
Expected: FAIL (`ModuleNotFoundError: ba2_common.core.entry_tag`).

**Step 3: Minimal implementation.**

`entry_tag.py`:

```python
"""The style-group ENTRY TAG: which member of a composed option expert opened a position.

Written by ``_OptionEntryAction._submit_option_order`` onto the PARENT order row's ``data`` (the
same seam as ``earnings_event_date``), read back by ``EntryTagCondition``. Absent on every position
not opened by a style-group entry, and absence is never a value (see the condition).
"""
from typing import Any, Optional

ORDER_ENTRY_TAG_KEY = "entry_tag"


def coerce_entry_tag(raw: Any) -> Optional[int]:
    """None stays None; anything else must be a positive int (bool refused -- True == 1)."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw or raw < 1:
        raise ValueError(f"entry_tag must be a positive integer, got {raw!r}")
    return int(raw)


def order_entry_tag(order) -> Optional[int]:
    """The tag stamped on ``order``'s entry row, or None. Resolve the row EXACTLY as
    ``earnings_stamp.order_event_date`` does (copy its parent-order walk, do not re-derive it)."""
    ...
```

- `rule_builders._OPTION_ACTION_PARAM_KEYS`: add `("entry_tag", ("option_entry_tag",)),` with a
  comment ("style-group member tag; absent stays absent -- byte-identical for every existing
  rule").
- `TradeActionEvaluator`: add `'entry_tag'` to the forwarded option param tuple. If that tuple is
  gated per action through a `types.get_*_action_values()` registry (as `min_one_contract` is,
  `types.py:959`), add `get_entry_tag_action_values()` = every option ENTRY action value (not
  `close_option`, the rolls or the overlays), with a drift test mirroring the min_one_contract one.
- `_OptionEntryAction.__init__`: add `entry_tag: Any = None` and
  `self.entry_tag = coerce_entry_tag(entry_tag)`.
- `_submit_option_order`: after `entry_facts` is built, add
  `if self.entry_tag is not None: entry_facts[ORDER_ENTRY_TAG_KEY] = self.entry_tag`. It must be
  by NAME, not via the whitelist tuple (the trap that comment block describes).

**Step 4: Run the tests again, then the whole packages suite.** Expected: the new file PASSES,
and the packages suite equals the Task 0 baseline.

**Step 5: Commit.** `feat(stage2): entry_tag option action param stamped on the parent order row`

---

### Task 2: `EntryTagCondition` + registries (packages/common)

**Files:**
- Modify: `packages/common/ba2_common/core/types.py` (`ExpertEventType`: `N_ENTRY_TAG = "entry_tag"`; the numeric-event value list)
- Modify: `packages/common/ba2_common/core/TradeConditions.py` (new class after `DaysAfterEventCondition`; `CONDITION_MAP` ~4496)
- Modify: `packages/common/ba2_common/core/rule_builders.py` (`FIELD_EVENT`)
- Modify: `packages/common/ba2_common/core/rules_export_import.py` (`_FIELD_ABBR`: `"entry_tag": "etag"`)
- Modify: any other registry that the drift tests list (for example `rules_documentation.py`). Let the failing drift tests name them.
- Test: `packages/common/tests/test_entry_tag_condition.py`

**Step 1: Write the failing tests.**

```python
from types import SimpleNamespace
import pytest
from ba2_common.core.TradeConditions import EntryTagCondition, CONDITION_MAP
from ba2_common.core.types import ExpertEventType
from ba2_common.core.rule_builders import FIELD_EVENT, triggers_from_condition_tree


def _cond(order, op="==", value=3.0):
    return EntryTagCondition(account=None, instrument_name="AAPL",
                             expert_recommendation=None, operator_str=op, value=value,
                             existing_order=order)


def _order(tag):
    return SimpleNamespace(id=1, data=({"entry_tag": tag} if tag is not None else {}),
                           parent_order_id=None, contract_symbol=None)


def test_matches_its_own_member():
    assert _cond(_order(3)).evaluate() is True


def test_rejects_another_member():
    assert _cond(_order(4)).evaluate() is False


@pytest.mark.parametrize("op", ["==", "!=", ">", "<", ">=", "<="])
def test_an_untagged_position_never_fires_for_any_operator(op):
    c = _cond(_order(None), op=op)
    assert c.evaluate() is False and c.get_calculated_value() is None


def test_no_existing_order_never_fires():
    assert _cond(None).evaluate() is False


def test_registered_everywhere_the_engine_reads_a_field():
    assert CONDITION_MAP[ExpertEventType.N_ENTRY_TAG] is EntryTagCondition
    assert FIELD_EVENT["entry_tag"] == ExpertEventType.N_ENTRY_TAG
    tree = {"type": "AND", "conditions": [
        {"id": "x-tag", "field": "entry_tag", "op": "==", "value": 3.0}]}
    (trig,) = triggers_from_condition_tree(tree).values()
    assert trig["event_type"] == "entry_tag" and float(trig["value"]) == 3.0


def test_the_tag_is_never_a_forced_exit_trigger():
    """A tag leaf ANDed into an exit must not change that exit's forced/discretionary class."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit
    tp = SimpleNamespace(triggers={"cond_0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50},
                                   "cond_1": {"event_type": "entry_tag", "operator": "==", "value": 3}})
    assert forced_option_exit(tp) is False
```

(Adapt the trigger dict keys to what `forced_option_exit` actually reads. Copy them from
`packages/common/tests/test_earnings_event_timing_gates.py`.)

**Step 2: Run.** Expected: FAIL (`ImportError: EntryTagCondition`).

**Step 3: Implement.** Mirror `DaysAfterEventCondition`: read the row first, and unknown never
fires.

```python
class EntryTagCondition(CompareCondition):
    """Which style-group MEMBER opened the evaluated position (``entry_tag`` on its entry order).

    ANDed into every exit rule a style expert emits (ba2test_launcher O_STYLE), so one
    OPEN_POSITIONS ruleset carries each member's OWN exits. UNKNOWN NEVER FIRES: no order, no row
    data or no tag -> calculated_value None and False for every operator. That is safe by
    construction (an untagged position is not a style position) and it is why the wheel's
    stock-phase rules are deliberately left UNSCOPED (the assigned stock lot has no entry tag).
    """

    def evaluate(self) -> bool:
        try:
            if not self.existing_order:
                self.calculated_value = None
                return False
            tag = order_entry_tag(self.existing_order)
            if tag is None:
                self.calculated_value = None
                return False
            self.calculated_value = float(tag)
            return self.operator_func(self.calculated_value, self.value)
        except Exception as e:
            absorb_if_benign(e)
            logger.error(f"Error evaluating entry_tag condition: {e}", exc_info=True)
            self.calculated_value = None
            return False
```

Register it in `CONDITION_MAP`, `FIELD_EVENT`, `_FIELD_ABBR` and whichever registries the drift
tests list.

**Step 4: Run the new file, then the full packages suite.** Expected: PASS; the suite equals the
baseline.

**Step 5: Commit.** `feat(stage2): EntryTagCondition reads the member tag back off the entry order`

---

### Task 3: Style spec: loader, validator, member tag table (launcher)

**Files:**
- Modify: `testplatform/ba2test_launcher.py`. Add a new section, `# --- STAGE-2 STYLE GROUPS ---`,
  directly after `_build_strategy_option_group` (~line 5401).
- Test: `testplatform/backend/tests/test_option_style_spec.py`

**Spec format** (schema 1). Member order = entry precedence; see question Q4.

```json
{
  "schema": 1,
  "styles": {
    "premium": {
      "status": "candidate",
      "note": "free text",
      "price_cap_policy": "none",
      "members": [
        {"key": "O_BULLPS"},
        {"key": "O_IC", "exit_overrides": {"td": {"value": 21, "value_min": 10, "value_max": 35, "value_step": 5}}},
        {"key": "O_WHEEL"}
      ]
    }
  }
}
```

**Step 1: Write the failing tests.**

```python
import json
import pytest
from tests.test_option_grid_foundations import _launcher   # reuse the path loader


def _write(tmp_path, styles):
    p = tmp_path / "styles.json"
    p.write_text(json.dumps({"schema": 1, "styles": styles}))
    return str(p)


def test_member_tags_cover_exactly_the_admissible_members_and_never_collide():
    m = _launcher()
    assert set(m._STYLE_MEMBER_TAGS) == m._style_admissible_members()
    assert len(set(m._STYLE_MEMBER_TAGS.values())) == len(m._STYLE_MEMBER_TAGS)
    assert all(isinstance(v, int) and v >= 1 for v in m._STYLE_MEMBER_TAGS.values())


def test_the_admissible_set_is_what_the_builders_say():
    m = _launcher()
    assert m._style_admissible_members() == {
        "O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BF", "O_BEARCS", "O_BULLPS", "O_IC",
        "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG", "O_WHEEL",
        "O_LEAP", "O_PMCC", "O_ERN", "O_CBS", "O_PBS", "O_CONVEX"}


def test_a_valid_spec_loads_and_resolves_one_style(tmp_path):
    m = _launcher()
    path = _write(tmp_path, {"short": {"status": "candidate",
                                        "members": [{"key": "O_LC"}, {"key": "O_LP"}]}})
    style = m._load_style_spec(path, "short")
    assert style["name"] == "short"
    assert [x["key"] for x in style["members"]] == ["O_LC", "O_LP"]
    assert len(style["spec_sha256"]) == 64


@pytest.mark.parametrize("members, msg", [
    ([{"key": "O_CC"}], "equity-entry"),                    # Q1
    ([{"key": "O_PP"}], "equity-entry"),
    ([{"key": "O_SSTG"}], "unbounded"),
    ([{"key": "O_CAL"}], "phase-gated"),
    ([{"key": "OS1"}], "list the members"),
    ([{"key": "O_LEAPC"}], "O_LEAP"),                        # arms are not members; the group key is
    ([{"key": "O_LC"}, {"key": "O_LC"}], "twice"),
    ([{"key": "O_WHEEL"}, {"key": "O_CSP"}], "hold_assigned_stock"),   # Q2
    ([{"key": "O_WHEEL"}, {"key": "O_JL"}], "hold_assigned_stock"),
    ([], "no members"),
    ([{"key": "O_LC", "action_overrides": {"option_sizing": 9}}], "action_overrides"),
    ([{"key": "O_LC", "exit_overrides": {"tp": {"op": ">"}}}], "exit_overrides"),
    ([{"key": "O_LC", "bogus": 1}], "unknown member key"),
])
def test_invalid_members_are_refused_by_name(tmp_path, members, msg):
    m = _launcher()
    path = _write(tmp_path, {"s": {"status": "candidate", "members": members}})
    with pytest.raises(ValueError, match=msg):
        m._load_style_spec(path, "s")


def test_mixed_price_caps_need_an_explicit_policy(tmp_path):
    m = _launcher()
    mixed = {"status": "candidate", "members": [{"key": "O_BULLPS"}, {"key": "O_WHEEL"}]}
    with pytest.raises(ValueError, match="price_cap_policy"):
        m._load_style_spec(_write(tmp_path, {"p": mixed}), "p")
    ok = dict(mixed, price_cap_policy="none")
    assert m._load_style_spec(_write(tmp_path, {"p": ok}), "p")["price_cap_policy"] == "none"


def test_unknown_style_name_and_schema_are_refused(tmp_path):
    m = _launcher()
    path = _write(tmp_path, {"short": {"status": "candidate", "members": [{"key": "O_LC"}]}})
    with pytest.raises(ValueError, match="no style 'mid'"):
        m._load_style_spec(path, "mid")
```

**Step 2: Run.** Expected: FAIL (`AttributeError: _STYLE_MEMBER_TAGS`).

**Step 3: Implement** (all pure; `ValueError` here, the CLI turns it into `sys.exit` in Task 7):

```python
_STYLE_KEY = "O_STYLE"
#: Strategy keys whose builder needs a run input (a style spec) -- excluded from every test that
#: builds "every registered key" with no spec (Task 7 lists them).
_SPEC_DRIVEN_STRATEGIES = {_STYLE_KEY}
_GRID1_FAMILY_KEYS = {"OS1", "OS2", "OS3", "OS4"}

#: member -> entry_tag. APPEND-ONLY PERSISTED CONTRACT: the tag is written on live and backtest
#: order rows and read back by every deployed style ruleset. Never renumber, never reuse a value.
_STYLE_MEMBER_TAGS = {
    "O_LC": 1, "O_LP": 2, "O_VERT": 3, "O_BULLCS": 4, "O_BF": 5, "O_BEARCS": 6, "O_BULLPS": 7,
    "O_IC": 8, "O_JL": 9, "O_RS": 10, "O_CSP": 11, "O_STRD": 12, "O_STRG": 13, "O_WHEEL": 14,
    "O_LEAP": 15, "O_PMCC": 16, "O_CONVEX": 17, "O_ERN": 18, "O_CBS": 19, "O_PBS": 20,
}
#: Members whose short leg is assigned PER LEG (not unit-settled as a defined-risk combo --
#: BacktestAccount.defined_risk_combo_strategy returns None for them), so with the account-level
#: hold_assigned_stock on (any wheel member) their stock would be held and covered-called too.
_STYLE_ASSIGNS_STOCK = {"O_CSP", "O_JL", "O_RS", "O_WHEEL"}
_STYLE_EQUITY_ENTRY = {"O_CC", "O_PP", "O_STK"}
_STYLE_MEMBER_FIELDS = {"key", "action_overrides", "exit_overrides", "note"}
_STYLE_FIELDS = {"status", "note", "members", "price_cap_policy", "convex_fitness_waiver"}
_STYLE_STATUSES = {"candidate", "approved"}
_STYLE_PRICE_CAP_POLICIES = {"group_min", "none"}
_STYLE_ACTION_OVERRIDE_KEYS = {"option_dte_min", "option_dte_max", "option_dte_min_range",
                               "option_dte_max_range", "option_dte_step"}
_STYLE_EXIT_OVERRIDE_FIELDS = {"value", "value_min", "value_max", "value_step"}


def _style_admissible_members() -> set:
    """Derived from the builders, never restated: every launchable pure-option key whose builder
    is a single, a non-grid-1 group (O_LEAP/O_CONVEX) or the wheel, minus the risk and phase
    exclusions."""
    out = set()
    for k, b in _STRATEGY_BUILDERS.items():
        if (b is _build_strategy_option or b is _build_strategy_wheel
                or (b is _build_strategy_option_group and k not in _GRID1_FAMILY_KEYS)):
            out.add(k)
    return out - _UNDEFINED_RISK_MEMBERS - set(_PHASE_GATED_OPTION_STRATEGIES)


def _validate_style(name: str, style: dict) -> None: ...     # every rule in the tests above
def _load_style_spec(path: str, name: str) -> dict: ...      # json load, schema check, validate
                                                             # EVERY style in the file (a broken
                                                             # sibling is still a broken file), return
                                                             # {"name", **style, "spec_sha256"} where
                                                             # the sha is over the canonical JSON of
                                                             # THIS style (sort_keys=True)
```

Validation messages must name the style, the member and the reason. For the refusals tied to
open questions, the message cites the question. For example, the O_WHEEL with O_CSP/O_JL/O_RS
refusal says: "hold_assigned_stock is account-level: with O_WHEEL in the group, O_CSP's assigned
stock would also be held and covered-called, i.e. it becomes a second wheel. See Q2 in
docs/plans/2026-09-25-option-stage2-build.md."

The price-cap check compares `_screener_gate_base_for_strategy(member)` across members. If they
differ and `price_cap_policy` is absent, refuse. A policy other than `group_min`/`none` is also
refused. If the bases differ on any key other than `price_max`, refuse even with a policy.

**Step 4: Run the new file.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): style spec loader + validator + append-only member tag table`

---

### Task 4: Candidate style configs as data

**Files:**
- Create: `tools/option_styles.candidates.json`
- Test: add to `testplatform/backend/tests/test_option_style_spec.py`

**Step 1: Write the failing test.**

```python
from pathlib import Path
_CANDIDATES = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")


def test_the_candidate_file_has_the_four_styles_all_marked_candidate():
    m = _launcher()
    data = json.loads(Path(_CANDIDATES).read_text())
    assert set(data["styles"]) == {"short", "mid", "leaps_convex", "premium"}
    assert {s["status"] for s in data["styles"].values()} == {"candidate"}
    for name in data["styles"]:
        m._load_style_spec(_CANDIDATES, name)          # every candidate validates
```

**Step 2: Run.** Expected: FAIL (the file does not exist).

**Step 3: Create the file.** Every `note` must say "CANDIDATE: membership decided after stage 1 +
the LEAPS grid".

```json
{
  "schema": 1,
  "styles": {
    "short": {
      "status": "candidate",
      "note": "CANDIDATE: short-term directional DEBIT at the stage-1 25-60 DTE bands; membership decided after stage 1 + the LEAPS grid.",
      "members": [{"key": "O_LC"}, {"key": "O_LP"}, {"key": "O_BULLCS"}, {"key": "O_VERT"}, {"key": "O_BF"}]
    },
    "mid": {
      "status": "candidate",
      "note": "CANDIDATE: the directional families at LONGER DTE plus the long-vol pair. The DTE windows and the td/dte exit bands below are PLACEHOLDERS (question Q7), not decisions.",
      "members": [
        {"key": "O_LC", "action_overrides": {"option_dte_min": 60, "option_dte_max": 90, "option_dte_min_range": 60, "option_dte_max_range": 120, "option_dte_step": 10},
         "exit_overrides": {"td": {"value": 45, "value_min": 20, "value_max": 90, "value_step": 5}, "dte": {"value": 21, "value_min": 7, "value_max": 35, "value_step": 7}}},
        {"key": "O_LP", "action_overrides": {"option_dte_min": 60, "option_dte_max": 90, "option_dte_min_range": 60, "option_dte_max_range": 120, "option_dte_step": 10},
         "exit_overrides": {"td": {"value": 45, "value_min": 20, "value_max": 90, "value_step": 5}, "dte": {"value": 21, "value_min": 7, "value_max": 35, "value_step": 7}}},
        {"key": "O_BULLCS", "action_overrides": {"option_dte_min": 60, "option_dte_max": 90, "option_dte_min_range": 60, "option_dte_max_range": 120, "option_dte_step": 10}},
        {"key": "O_VERT", "action_overrides": {"option_dte_min": 60, "option_dte_max": 90, "option_dte_min_range": 60, "option_dte_max_range": 120, "option_dte_step": 10}},
        {"key": "O_STRD"}, {"key": "O_STRG"}
      ]
    },
    "leaps_convex": {
      "status": "candidate",
      "note": "CANDIDATE: long-dated / convex. REFUSES TO LAUNCH under a CAR-family fitness until question Q5 is answered (no convex_fitness_waiver on purpose).",
      "members": [{"key": "O_LEAP"}, {"key": "O_PMCC"}, {"key": "O_CONVEX"}]
    },
    "premium": {
      "status": "candidate",
      "note": "CANDIDATE: credit/income. O_CSP/O_JL/O_RS excluded while O_WHEEL is a member (Q2); O_CC cannot be a member (Q1). price_cap_policy 'none' pending Q3; the wheel is LAST so the defined-risk members get precedence (Q4).",
      "price_cap_policy": "none",
      "members": [{"key": "O_BULLPS"}, {"key": "O_BEARCS"}, {"key": "O_IC"}, {"key": "O_WHEEL"}]
    }
  }
}
```

**Step 4: Run.** Expected: PASS. (`exit_overrides` ids are checked against the member's real exit
ids in Task 6, which re-runs this file.)

**Step 5: Commit.** `feat(stage2): four candidate style configs (short/mid/leaps_convex/premium) as data`

---

### Task 5: Style builder, entry side

**Files:**
- Modify: `testplatform/ba2test_launcher.py` (style section)
- Test: `testplatform/backend/tests/test_option_style_builder.py`

**Step 1: Write the failing tests.**

```python
import json
import pytest
from tests.test_option_grid_foundations import _launcher

_CAND = "…/tools/option_styles.candidates.json"   # resolve off __file__ as in Task 4


def _style(m, name):
    m._STYLE_ACTIVE = m._load_style_spec(_CAND, name)
    return m._build_strategy_style("O_STYLE")


@pytest.mark.parametrize("name", ["short", "mid", "premium", "leaps_convex"])
def test_entry_rules_are_the_members_own_rules_in_member_order(name):
    m = _launcher()
    s = _style(m, name)
    expected = []
    for mem in m._STYLE_ACTIVE["members"]:
        expected += [r["id"] for r in m._STRATEGY_BUILDERS[mem["key"]](mem["key"]).entry_rules]
    assert [r["id"] for r in s.entry_rules] == expected


def test_every_entry_rule_is_toggleable_and_every_option_action_carries_its_member_tag():
    m = _launcher()
    s = _style(m, "premium")
    by_rule = {}
    for mem in m._STYLE_ACTIVE["members"]:
        for r in m._STRATEGY_BUILDERS[mem["key"]](mem["key"]).entry_rules:
            by_rule[r["id"]] = m._STYLE_MEMBER_TAGS[mem["key"]]
    for r in s.entry_rules:
        assert r.get("toggle_optimize") is True
        tags = {a["option_entry_tag"] for a in r["actions"]}
        assert tags == {by_rule[r["id"]]}


def test_the_wheel_member_keeps_O_CSP_entry_ids_and_its_own_market_ids():
    m = _launcher()
    s = _style(m, "premium")
    assert "o_csp-entry" in [r["id"] for r in s.entry_rules]


def test_action_overrides_move_the_dte_band_and_nothing_else():
    m = _launcher()
    s = _style(m, "mid")
    lc = next(r for r in s.entry_rules if r["id"] == "o_lc-entry")["actions"][0]
    base = m._option_entry_action_for("O_LC")
    assert (lc["option_dte_min_range"], lc["option_dte_max_range"]) == (60, 120)
    for k in ("option_strike_param_min", "option_strike_param_max", "option_sizing"):
        assert lc[k] == base[k]


def test_single_structure_builders_are_untouched_by_building_a_style():
    m = _launcher()
    before = json.dumps(m._build_strategy_option("O_LC").entry_rules, sort_keys=True, default=str)
    _style(m, "short")
    after = json.dumps(m._build_strategy_option("O_LC").entry_rules, sort_keys=True, default=str)
    assert before == after   # deepcopy discipline: no shared mutable state leaks back


def test_no_style_loaded_is_a_loud_refusal():
    m = _launcher()
    m._STYLE_ACTIVE = None
    with pytest.raises(SystemExit, match="--style-config"):
        m._build_strategy_style("O_STYLE")
```

**Step 2: Run.** Expected: FAIL (`_build_strategy_style` missing).

**Step 3: Implement.** Use `deepcopy` of each member's built rules, never shared dicts.

```python
_STYLE_ACTIVE: "dict | None" = None   # set from --style-config/--style at command entry (Task 7)


def _style_entry_rules(member: str, tag: int, action_overrides):
    from copy import deepcopy
    from ba2_common.core.types import is_option_action
    src = _STRATEGY_BUILDERS[member](member)
    rules = deepcopy(list(src.entry_rules or []))
    for r in rules:
        r["toggle_optimize"] = True
        r["toggleOptimize"] = True          # canonical rules may carry the camelCase mirror
        for a in (r.get("actions") or []):
            if is_option_action(str(a.get("action_type") or a.get("action") or "")):
                a["option_entry_tag"] = int(tag)
                a.update(action_overrides or {})
                _assert_style_dte_override_is_decodable(member, a)   # min<=max, hw>=7, centre range sane
    return rules, src


def _build_strategy_style(kind: str):
    from app.models.strategy import Strategy
    from ba2_common.core.rule_models import normalize_trade_rules
    style = _STYLE_ACTIVE
    if style is None:
        sys.exit(f"{kind}: no style loaded -- pass --style-config FILE --style NAME")
    entry, sources = [], {}
    for mem in style["members"]:
        key = mem["key"]
        rules, src = _style_entry_rules(key, _STYLE_MEMBER_TAGS[key], mem.get("action_overrides"))
        entry += rules
        sources[key] = src
    s = Strategy(name=f"{kind}-{style['name']}", entry_rules=normalize_trade_rules(entry),
                 exit_rules=[])          # Task 6 fills the exits; O_STYLE is unregistered until Task 7
    s.entry_action = dict(s.entry_rules[0]["actions"][0])  # type: ignore[attr-defined]
    s._style_sources = sources            # type: ignore[attr-defined]  (Task 6 reads the exits)
    return s
```

`normalize_trade_rules` must keep `option_entry_tag`, because `ActionCfg` keeps extras verbatim.
The tag test above proves that.

**Step 4: Run.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): style builder entry side -- members' own toggleable entry rules, tagged`

---

### Task 6: Style builder: member-prefixed, tag-scoped exits + id maps

**Why.** Today `_option_exit_rules(kind)` keys its bands on the launched kind, and exit ids (`tp`,
`td`, `dte`, `sl`, `sl_ml`, …) are unprefixed. A group with debit and credit members would
therefore give one band set to all of them, and two members' exits would collide on ids (spec
§7.1). Here each member's exit list comes from ITS OWN builder:
- the right debit/credit bands, `opt_sl_ml` authored-off, the grid-2 DTE/TP-multiple/event bands,
  the PMCC roll rules, and the wheel's overlay chain;
- every exit rule and condition id is re-prefixed `<member>-`;
- every exit rule EXCEPT the wheel's stock-phase rules is ANDed with `<member>-tag == tag`.

**Files:**
- Modify: `testplatform/ba2test_launcher.py`
- Test: `testplatform/backend/tests/test_option_style_builder.py` (extend)

**Step 1: Write the failing tests.**

```python
from app.services.strategy_param_space import collect_param_space

_STOCK_PHASE = {"has_covered_call", "has_assigned_shares", "covered_call_days_to_expiry"}


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "field" in node:
            out.append(node)
        for c in node.get("conditions") or []:
            _leaves(c, out)
    return out


@pytest.mark.parametrize("name", ["short", "mid", "premium", "leaps_convex"])
def test_each_members_exit_block_is_its_own_builders_exits_re_ided(name):
    m = _launcher()
    s = _style(m, name)
    ids = [r["id"] for r in s.exit_rules]
    assert len(ids) == len(set(ids))
    for mem in m._STYLE_ACTIVE["members"]:
        p = mem["key"].lower()
        own = [r["id"] for r in m._STRATEGY_BUILDERS[mem["key"]](mem["key"]).exit_rules]
        assert [i for i in ids if i.startswith(p + "-")] == [f"{p}-{i}" for i in own]


@pytest.mark.parametrize("name", ["short", "mid", "premium", "leaps_convex"])
def test_every_non_stock_phase_exit_is_scoped_to_exactly_its_member(name):
    m = _launcher()
    s = _style(m, name)
    for r in s.exit_rules:
        leaves = _leaves(r["conditions"])
        tags = [x for x in leaves if x["field"] == "entry_tag"]
        if any(x["field"] in _STOCK_PHASE for x in leaves):
            assert tags == [], f"{r['id']}: stock-phase rules must stay UNSCOPED (lot has no tag)"
            continue
        member = next(k for k in m._STYLE_MEMBER_TAGS if r["id"].startswith(k.lower() + "-"))
        assert len(tags) == 1 and tags[0]["op"] == "==" \
            and tags[0]["value"] == float(m._STYLE_MEMBER_TAGS[member])


def test_debit_and_credit_members_keep_their_own_tp_bands_and_the_credit_stop():
    m = _launcher()
    s = _style(m, "premium")       # all credit
    tp = next(x for r in s.exit_rules if r["id"] == "o_ic-opt_tp" for x in _leaves(r["conditions"]) if x["id"] == "o_ic-tp")
    assert (tp["value"], tp["value_min"], tp["value_max"]) == (50, 25, 75)
    assert "o_ic-opt_sl" in [r["id"] for r in s.exit_rules]
    s = _style(m, "short")         # all debit
    tp = next(x for r in s.exit_rules if r["id"] == "o_lc-opt_tp" for x in _leaves(r["conditions"]) if x["id"] == "o_lc-tp")
    assert (tp["value"], tp["value_min"], tp["value_max"]) == (100, 25, 200)
    assert "o_lc-opt_sl" not in [r["id"] for r in s.exit_rules]


def test_grid2_members_keep_their_own_bands_and_authored_off_rules():
    m = _launcher()
    s = _style(m, "leaps_convex")
    rid = {r["id"]: r for r in s.exit_rules}
    dte = next(x for x in _leaves(rid["o_leap-opt_dte"]["conditions"]) if x["id"] == "o_leap-dte")
    assert (dte["value"], dte["value_min"], dte["value_max"]) == (150, 90, 240)
    assert rid["o_pmcc-opt_sl_ml"].get("enabled") is False
    assert "o_pmcc-pmcc_roll_dte" in rid and "o_convex-opt_tp_mult" in rid
    # the PMCC roll rules still come FIRST within the PMCC block (builder order preserved)
    pmcc = [i for i in rid if i.startswith("o_pmcc-")]
    assert pmcc[:3] == ["o_pmcc-pmcc_roll_dte", "o_pmcc-pmcc_roll_buyback", "o_pmcc-pmcc_delta_floor"]


def test_exit_overrides_move_only_the_named_band():
    m = _launcher()
    s = _style(m, "mid")
    td = next(x for r in s.exit_rules if r["id"] == "o_lc-opt_time" for x in _leaves(r["conditions"]) if x["id"] == "o_lc-td")
    assert (td["value"], td["value_min"], td["value_max"], td["value_step"]) == (45, 20, 90, 5)


def test_an_exit_override_naming_no_exit_condition_is_refused(tmp_path):
    m = _launcher()
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema": 1, "styles": {"x": {"status": "candidate", "members": [
        {"key": "O_LC", "exit_overrides": {"nope": {"value": 1}}}]}}}))
    m._STYLE_ACTIVE = m._load_style_spec(str(p), "x")
    with pytest.raises(ValueError, match="nope"):
        m._build_strategy_style("O_STYLE")


@pytest.mark.parametrize("name", ["short", "mid", "premium", "leaps_convex"])
def test_the_style_genome_is_the_disjoint_union_of_the_members_genomes(name):
    """No gene key collides and none is silently lost: every member's own strategy gene,
    translated through the id map, is in the style space; the only NEW keys are the entry
    toggles; shared keys (cond:shared-*, optsel:*) appear once."""
    m = _launcher()
    s = _style(m, name)
    style_space = collect_param_space(s)
    seen = set()
    for mem in m._STYLE_ACTIVE["members"]:
        key = mem["key"]
        idm = s.style_id_maps[key]
        own = collect_param_space(m._STRATEGY_BUILDERS[key](key))
        for g in own:
            t = m._style_translate_key(g, idm)      # the SAME function Task 13 wraps
            assert t in style_space, f"{key}: {g} -> {t} missing from the style space"
            seen.add(t)
    extra = set(style_space) - seen
    assert all(g.startswith("entry:") and g.endswith(":enabled") for g in extra), sorted(extra)
    print(f"{name}: {len(style_space)} strategy genes")     # record in the task report
```

(Write the tests with the real TradeRule shape. Before asserting, print one built exit rule to
confirm where `conditions`, `id` and `enabled` live after `trade_rules_from_legacy`, and adjust
the helpers once.)

**Step 2: Run.** Expected: FAIL (`exit_rules == []`, `style_id_maps` missing).

**Step 3: Implement.**

```python
_STYLE_STOCK_PHASE_FIELDS = {"has_covered_call", "has_assigned_shares",
                             "covered_call_days_to_expiry"}


def _style_scope_exits(member: str, tag: int, exit_rules, exit_overrides):
    """(rules, id_map). Re-id every exit rule and condition leaf with the member prefix, apply
    exit_overrides BY ORIGINAL condition id, and AND the tag leaf into every rule that is not a
    wheel STOCK-PHASE rule (those read has_assigned_shares / has_covered_call /
    covered_call_days_to_expiry, which are true only for the wheel's own assigned lot; the lot is
    an assignment-created stock transaction with NO entry tag, so a tag leaf would disarm the
    overlay -- the naked-call defect in a new shape)."""
    from copy import deepcopy
    p = member.lower()
    rules = deepcopy(list(exit_rules or []))
    pending = {k: dict(v) for k, v in (exit_overrides or {}).items()}
    rule_map, cond_map = {}, {}
    for r in rules:
        tree = r.setdefault("conditions", {"type": "AND", "conditions": []})
        leaves = _style_tree_leaves(tree)
        stock_phase = any(x.get("field") in _STYLE_STOCK_PHASE_FIELDS for x in leaves)
        for leaf in leaves:
            cid = leaf.get("id")
            if cid in pending:
                leaf.update(pending.pop(cid))
            if cid:
                cond_map[cid] = leaf["id"] = f"{p}-{cid}"
        rule_map[r["id"]] = r["id"] = f"{p}-{r['id']}"
        if r.get("name"):
            r["name"] = f"{p}-{r['name']}"
        if not stock_phase:
            tree.setdefault("conditions", []).append(
                {"id": f"{p}-tag", "field": "entry_tag", "field_type": "numeric",
                 "op": "==", "value": float(tag)})
    if pending:
        raise ValueError(f"{member}: exit_overrides name no exit condition: {sorted(pending)}")
    return rules, {"exit_rules": rule_map, "exit_conds": cond_map}


def _style_translate_key(gene: str, id_map: dict) -> str:
    """ONE member-level gene key -> the style key (identity for entry/shared keys). Raises
    ValueError on a key the member's id map cannot place. Task 13's translator wraps this."""
    ...
```

Wire it into `_build_strategy_style`:
- for each member, `scoped, idm = _style_scope_exits(key, tag, sources[key].exit_rules, mem.get("exit_overrides"))`;
- add `idm["entry_rules"]` (the member's entry rule ids) and `idm["entry_conds"]` (every entry
  leaf id);
- concatenate the scoped exit blocks in member order.

Set `s.style_id_maps = {member: idm}`. Validate the `exit_overrides` values: `value_min ≤ value ≤ value_max` and step > 0; refuse otherwise.

**Step 4: Run.** Expected: PASS. Record each candidate style's strategy-gene count in the task report
(spec §7.1: "measure each group's real genome with collect_param_space first").

**Step 5: Commit.** `feat(stage2): member-prefixed, entry_tag-scoped exits keep every member's own bands`

---

### Task 7: Register `O_STYLE`: CLI, run-config rules, refusals, iterating tests

**Files:**
- Modify: `testplatform/ba2test_launcher.py`:
  - `_STRATEGY_BUILDERS` (`"O_STYLE": _build_strategy_style`)
  - `_PURE_OPTION_STRATEGIES` (`| {_STYLE_KEY}`)
  - `_screener_gate_base_for_strategy`, `_hold_assigned_stock`, `_apply_option_trade_floor`,
    `_refuse_convex_fitness_mismatch`
  - `_cmd_optimize`: `--style-config`/`--style` parsing, `backtest_block["option_style"]`
  - `_cmd_optimize_batch`: refuse O_STYLE
  - the argparse `optimize` block (~7544)
- Modify (exclude `_SPEC_DRIVEN_STRATEGIES` with a one-line reason, or add a style case):
  - `tests/test_equity_cap_launcher.py:217`
  - `tests/test_gene_to_artefact_audit.py:143` and `:807` (the count of 29 must NOT move)
  - `tests/test_launcher_dte_exit_rule.py:84`
  - `tests/test_launcher_resolve_fitness.py` (check its loops at 49/106/160/169)
  - any other test the full backend run shows iterating every key
- Test: `testplatform/backend/tests/test_option_style_registration.py`

**Step 1: Write the failing tests.**

```python
import pytest
from tests.test_option_grid_foundations import _launcher
from tests.test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize


def _argv(tmp_path, style, extra=()):
    return _BASE_ARGV + ["--strategy", "O_STYLE", "--style-config", CAND, "--style", style, *extra]


def test_o_style_is_a_pure_option_key_but_never_a_grid1_group():
    m = _launcher()
    assert "O_STYLE" in m._STRATEGY_BUILDERS and "O_STYLE" in m._PURE_OPTION_STRATEGIES
    assert "O_STYLE" in m._OPTION_STRATEGY_KEYS and "O_STYLE" in m._MARKET_CONDITION_STRATEGIES
    assert "O_STYLE" not in m._OPTION_GROUPS and "O_STYLE" not in m._OPTION_GROUPS_ALL


def test_premium_with_the_wheel_holds_assigned_stock(monkeypatch, tmp_path):
    cfg = _run_optimize(_parse(_argv(tmp_path, "premium")), monkeypatch)
    assert cfg["backtest"]["account_settings"]["hold_assigned_stock"] is True


def test_a_style_without_the_wheel_does_not(monkeypatch, tmp_path):
    cfg = _run_optimize(_parse(_argv(tmp_path, "short")), monkeypatch)
    assert cfg["backtest"]["account_settings"]["hold_assigned_stock"] is False


def test_the_run_config_records_the_resolved_style(monkeypatch, tmp_path):
    cfg = _run_optimize(_parse(_argv(tmp_path, "short")), monkeypatch)
    rec = cfg["backtest"]["option_style"]
    assert rec["name"] == "short" and [x["key"] for x in rec["members"]][:2] == ["O_LC", "O_LP"]
    assert len(rec["spec_sha256"]) == 64


def test_option_fixed_settings_reach_a_style_job(monkeypatch, tmp_path):
    argv = _BASE_ARGV[:]            # swap the expert to DeterministicScorer as test_ds_macro_short_side_launcher does
    ...
    assert settings["macro_short_side"] == "mirror"


def test_price_cap_policy_none_gives_no_price_gate_and_group_min_the_lowest(monkeypatch):
    m = _launcher()
    m._STYLE_ACTIVE = m._load_style_spec(CAND, "premium")
    assert m._screener_gate_base_for_strategy("O_STYLE") == {}
    m._STYLE_ACTIVE = dict(m._STYLE_ACTIVE, price_cap_policy="group_min")
    assert m._screener_gate_base_for_strategy("O_STYLE") == {"price_max": 100.0}


def test_a_convex_member_under_a_car_fitness_is_refused_without_a_waiver():
    m = _launcher()
    m._STYLE_ACTIVE = m._load_style_spec(CAND, "leaps_convex")
    with pytest.raises(SystemExit, match="Q5"):
        m._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_car_target_soft30")
    with pytest.raises(SystemExit, match="O_CONVEX-only"):
        m._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_convex")
    m._STYLE_ACTIVE = dict(m._STYLE_ACTIVE, convex_fitness_waiver="user: accepted 2026-10-xx")
    m._refuse_convex_fitness_mismatch("optimize", "O_STYLE", "option_car_target_soft30")


def test_the_low_trade_floor_applies_when_any_member_is_long_dated():
    m = _launcher()
    m._STYLE_ACTIVE = m._load_style_spec(CAND, "leaps_convex")
    blk = {}
    m._apply_option_trade_floor("O_STYLE", blk)
    assert blk == m._OPTION_LOW_TRADE_FLOOR


@pytest.mark.parametrize("bad", [["--style", "short"], ["--style-config", "x.json"]])
def test_style_flags_are_refused_without_o_style_and_required_with_it(bad):
    ...   # --strategy S2 --style short -> SystemExit; --strategy O_STYLE without both -> SystemExit


def test_optimize_batch_refuses_o_style():
    ...   # "style jobs run through `optimize`; the driver loops it"
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**
- `_hold_assigned_stock`: `if kind == _STYLE_KEY: return _STYLE_ACTIVE is not None and any(x["key"] in _HOLDS_ASSIGNED_STOCK for x in _STYLE_ACTIVE["members"])`.
- `_apply_option_trade_floor`: the same any-member rule over `_OPTION_LOW_TRADE_FLOOR_STRATEGIES`.
  **Also check** whether `option_car_target_soft30` reads `car_hard_min_trades_per_year` /
  `car_min_trades_per_year` at all (grep `strategy_fitness.py`), and write the answer in the task
  report. Under soft30 the floor may be inert; that goes to the user, it is not decided here.
- `_screener_gate_base_for_strategy`: for `O_STYLE`, collect the per-member bases. If they are
  all equal, return that base. Otherwise `none` returns `{}` and `group_min` returns
  `{"price_max": min(...)}` over the members that carry one.
- `_refuse_convex_fitness_mismatch`: a style branch as in the test. The message cites Q5 in this
  plan.
- `_cmd_optimize` entry: if `args.strategy == "O_STYLE"`, require both flags and set
  `global _STYLE_ACTIVE`. The `_load_style_spec` ValueError becomes `sys.exit`. If either flag is
  given without O_STYLE, `sys.exit`. Record `backtest_block["option_style"] = {"name", "members", "status", "price_cap_policy", "spec_sha256"}`.
  Only style jobs write this key, so every other stored config is unchanged.
- `_build_strategy`: nothing to change. `O_STYLE ∈ _OPTION_STRATEGY_KEYS` dispatches
  `_STRATEGY_BUILDERS[kind](kind)` and then `_clamp_confidence_genes` runs as for any key.
- Iterating tests: exclude `m._SPEC_DRIVEN_STRATEGIES` with the comment "spec-driven: built with a
  style in test_option_style_*". In `test_gene_to_artefact_audit.py`, ADD one audit case that
  builds `O_STYLE` from the `short` candidate. The gene-to-artefact audit is the strongest guard
  that every style gene reaches a real ruleset field.

**Step 4: Run the new file, then the FULL backend suite.** Expected: the new tests PASS. The suite
equals the baseline plus the new tests.

**Step 5: Commit.** `feat(stage2): register O_STYLE (hold/floor/price-cap/convex rules, CLI, batch refusal)`

---

### Task 8: Engine reach: the tag leaf becomes a trigger, and members stay isolated

**Files:**
- Test: `testplatform/backend/tests/test_option_style_engine_reach.py`

**Step 1: Write the tests.** They should pass if Tasks 1, 2 and 6 are right. If one fails, fix the
registry or the builder, not the test.

```python
from ba2_common.core.rule_builders import FIELD_EVENT, FLAG_FIELD_EVENT, tree_leaves, triggers_from_condition_tree


@pytest.mark.parametrize("name", ["short", "mid", "premium", "leaps_convex"])
def test_no_style_leaf_is_dropped_at_seeding(name):
    s = _style(_launcher(), name)
    for rules in (s.entry_rules, s.exit_rules):
        for r in rules:
            leaves = tree_leaves(r["conditions"])
            assert len(triggers_from_condition_tree(r["conditions"])) == len(leaves), r["id"]


def test_the_exit_ruleset_the_backtest_SEEDS_carries_the_tag_trigger():
    """Follow test_option_grid_foundations::test_the_sl_ml_leaf_becomes_a_real_engine_trigger:
    seed the OPEN_POSITIONS ruleset from the style's exit_rules through the same helper the
    backtest uses, then assert o_lc-opt_tp's EventAction has an entry_tag trigger == 1."""


def test_a_position_opened_by_member_A_matches_no_exit_rule_of_member_B():
    """Static isolation over EVERY pair of members of EVERY candidate style: for each non-stock
    rule of B, its tag trigger value != tag(A). With the Task-2 condition that is a False leaf in
    an AND, so B's exits can never close A's position."""


def test_forced_exit_classification_is_unchanged_by_scoping():
    """For each member, forced_option_exit(scoped rule) == forced_option_exit(the member's own
    single-job rule) -- the tag must not turn a DTE/SL exit discretionary or vice versa."""


def test_the_entry_action_tag_reaches_the_seeded_enter_ruleset():
    """Mirror test_launcher_option_entry_gates_reach_engine for entries: the enter ruleset the
    trial config seeds has one rule per member, each with its OWN action carrying entry_tag."""
```

**Step 2: Run.** `cd testplatform/backend && %PY% -m pytest tests/test_option_style_engine_reach.py -q -p no:cacheprovider`
Expected: PASS. A failure here is a real integration gap: fix it and note it in the commit.

**Step 3: Commit.** `test(stage2): style tag leaves reach the engine; members' exits are isolated`

---

### Task 9: Premium group: wheel / overlay / concentration checks

**Files:**
- Test: `testplatform/backend/tests/test_option_style_premium_wheel.py`
- Test: `testplatform/backend/tests/backtest/test_style_wheel_assignment.py` (reuse the harness
  functions of `tests/backtest/test_wheel_assignment.py`: `_build`, `_engine`, `_sell_the_put`,
  `_assign_the_put`, `_shares`)

**Step 1: Write the tests.**

```python
def test_the_wheel_overlay_chain_is_intact_unscoped_and_in_the_wheels_order():
    s = _style(_launcher(), "premium")
    ids = [r["id"] for r in s.exit_rules]
    chain = ["o_wheel-cc_dte", "o_wheel-cc_guard", "o_wheel-cc_sell", "o_wheel-wheel_stock_guard"]
    pos = [ids.index(i) for i in chain]
    assert pos == sorted(pos)
    for i in chain:
        rule = s.exit_rules[ids.index(i)]
        assert not [x for x in _leaves(rule["conditions"]) if x["field"] == "entry_tag"]
    # the CSP-phase closes of the wheel come AFTER its stock guard, and ARE scoped
    assert ids.index("o_wheel-opt_tp") > ids.index("o_wheel-wheel_stock_guard")


def test_the_covered_call_is_written_only_against_ASSIGNED_shares():
    s = _style(_launcher(), "premium")
    cc_sell = next(r for r in s.exit_rules if r["id"] == "o_wheel-cc_sell")
    assert [x["field"] for x in _leaves(cc_sell["conditions"])] == ["has_assigned_shares"]


def test_no_other_premium_member_can_produce_assigned_stock():
    """The reason the stock-phase rules may stay unscoped (and the account-level hold is right):
    every non-wheel premium member settles as a defined-risk combo."""
    m = _launcher()
    style = m._load_style_spec(CAND, "premium")
    others = {x["key"] for x in style["members"]} - {"O_WHEEL"}
    assert not (others & m._STYLE_ASSIGNS_STOCK)
    # and the combo classification really covers them:
    from app.services.backtest.backtest_account import BacktestAccount
    combo = BacktestAccount.DEFINED_RISK_LONG_STRATEGIES | BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES
    for k in others:
        assert m._OPTION_STRATS[k]["action_type"].replace("open_", "") in combo, k


def test_the_style_assigns_stock_set_matches_the_settlement_path():
    """_STYLE_ASSIGNS_STOCK is restated from BacktestAccount's per-leg path: jade_lizard and
    put_ratio_spread are NOT defined-risk combos, cash_secured_put is single-leg."""


# backtest/test_style_wheel_assignment.py
def test_while_assigned_shares_are_held_no_other_member_can_enter(tmp_path):
    """has_no_position is expert-level per instrument: after _assign_the_put, the flat leaf of
    EVERY premium member's entry rule reads False for that symbol (evaluate HasNoPositionCondition
    for the same expert id). So a share lot and another member's structure never overlap on one
    underlying, and the concentration grouping cannot fuse two members into one bet."""


def test_concentration_groups_the_wheel_cycle_as_one_bet_and_other_members_apart():
    """strategy_fitness._option_structure_groups over a synthetic premium trade list:
    CSP (exit 'assigned') -> stock lot at the strike -> covered call overlapping the lot ->
    called away == ONE bet; an O_BULLPS combo on another symbol == its own bet; an O_IC on the
    SAME symbol opened after the lot closed (non-overlapping) == its own bet."""
```

**Step 2: Run both files.** Expected: PASS. If `_STYLE_ASSIGNS_STOCK` disagrees with the
settlement path, fix the set (and Task 3's validator follows).

**Step 3: Commit.** `test(stage2): premium group wheel/overlay/concentration checks`

---

### Task 10: `encode_params_strict` in `genetic.py`

**Why.** `GeneticOptimizer.encode_params` fills an absent gene with `config['min']` (`genetic.py`,
the `expanded_params.get(param_name, config['min'])` line) and an unknown choice with index 0. A
seed missing its `entry:<m>-entry:enabled` gene would therefore seed that member OFF, silently
(spec §7.1). Warm start keeps the lenient path, so stage 1 does not move (question Q11).

**Files:**
- Modify: `testplatform/backend/app/services/genetic.py` (next to `encode_params`)
- Test: `testplatform/backend/tests/test_genetic_strict_encode.py`

**Step 1: Write the failing tests.**

```python
import pytest
from app.services.genetic import GeneticOptimizer

SPACE = {"a": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1},
         "n": {"type": "int", "min": 410, "max": 500, "step": 15},
         "c": {"type": "choice", "choices": ["off", "below", "above"], "min": 0, "max": 2, "step": 1}}


def _opt():
    return GeneticOptimizer(param_ranges=SPACE, population_size=4, n_generations=1)


def test_round_trips_a_complete_genome():
    ind = _opt().encode_params_strict({"a": 0.3, "n": 420, "c": "above"})
    d = _opt().decode_individual(ind)
    assert d["a"] == pytest.approx(0.3) and d["n"] == 420 and d["c"] == "above"


def test_a_value_off_the_decode_lattice_is_refused():
    """425 is inside [410, 500] but decode(425) == 420: a seed claiming 425 could never be
    reproduced by the GA, so it is refused rather than silently moved."""
    with pytest.raises(ValueError, match="'n'"):
        _opt().encode_params_strict({"a": 0.3, "n": 425, "c": "above"})


def test_a_missing_gene_is_refused_not_filled_with_min():
    with pytest.raises(ValueError, match="missing.*'n'"):
        _opt().encode_params_strict({"a": 0.3, "c": "above"})


def test_an_unknown_gene_is_refused():
    with pytest.raises(ValueError, match="unknown.*'zz'"):
        _opt().encode_params_strict({"a": 0.3, "n": 425, "c": "above", "zz": 1})


def test_an_unknown_choice_is_refused_not_mapped_to_index_0():
    with pytest.raises(ValueError, match="'sideways'"):
        _opt().encode_params_strict({"a": 0.3, "n": 425, "c": "sideways"})


def test_a_value_the_decoder_cannot_reproduce_is_refused():
    with pytest.raises(ValueError, match="'a'"):
        _opt().encode_params_strict({"a": 1.7, "n": 425, "c": "above"})


def test_a_decode_image_value_just_outside_min_is_accepted():
    """Decode rounds from ZERO, not from min: 410 on a step-15 lattice decodes to 405. A stage-1
    O_LEAP winner can therefore carry 405 (question Q12). The strict check accepts exactly the
    values decode(clip(v, min, max)) reproduces."""
    ind = _opt().encode_params_strict({"a": 0.3, "n": 405, "c": "above"})
    assert _opt().decode_individual(ind)["n"] == 405


def test_the_lenient_encode_is_unchanged():
    assert list(_opt().encode_params({"a": 0.3})) == [0.3, 410, 0]
```

**Step 2: Run.** Expected: FAIL (`AttributeError: encode_params_strict`).

**Step 3: Implement.**

```python
def encode_params_strict(self, params: Dict) -> List:
    """encode_params for SEEDS: every gene present, no unknown gene, choices valid, and each
    value one the decoder reproduces (decode(clip(v)) == v). Raises ValueError naming the key.
    Used by seedPopulation; warm start keeps the lenient encode_params (stage-1 no-impact)."""
    missing = [k for k in self.param_names if k not in params]
    unknown = sorted(set(params) - set(self.param_names))
    if missing or unknown:
        raise ValueError(f"seed genome: missing {missing!r}, unknown {unknown!r}")
    out = []
    for name in self.param_names:
        cfg, v = self.param_ranges[name], params[name]
        if cfg["type"] == "choice":
            if v not in cfg["choices"]:
                raise ValueError(f"seed genome: {name!r}={v!r} is not one of {cfg['choices']!r}")
            out.append(cfg["choices"].index(v))
            continue
        clipped = min(max(v, cfg["min"]), cfg["max"])
        step = cfg.get("step", 1 if cfg["type"] == "int" else 0.01)
        back = int(round(clipped / step) * step) if cfg["type"] == "int" else round(clipped / step) * step
        if abs(back - v) > 1e-9 * max(1.0, abs(v)):
            raise ValueError(f"seed genome: {name!r}={v!r} is outside [{cfg['min']}, {cfg['max']}] "
                             f"step {step} (decodes to {back!r})")
        out.append(int(clipped) if cfg["type"] == "int" else float(clipped))
    return creator.Individual(out)
```

**Step 4: Run.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): strict seed encoding -- no min-fill, no index-0 choice fallback`

---

### Task 11: Handler: `param_space_for_optimization`, `rangeOverrides`, `seedPopulation`

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py`:
  - extract lines ~1466-1505 (the `expert_params` → model/screener/schedule split plus
    `collect_param_space`) into `param_space_for_optimization(strategy, optimization_config, bypass)`;
  - the warm-start branch (~2035-2065);
  - a new `_build_seed_population`.
- Modify: `testplatform/backend/app/services/strategy_param_space.py` (new
  `apply_range_overrides`)
- Test: `testplatform/backend/tests/test_seed_population_handler.py`

**Step 1: Write the failing tests.**

```python
import pytest
from app.services.strategy_param_space import apply_range_overrides
from app.services.strategy_optimization_handler import (
    param_space_for_optimization, _build_seed_population, checkpoint_fingerprint)

SPACE = {"cond:x:value": {"type": "float", "min": 0.0, "max": 10.0, "step": 0.5},
         "entry:r:enabled": {"type": "int", "min": 0, "max": 1, "step": 1},
         "cond:y:mode": {"type": "choice", "choices": ["off", "below", "above"], "min": 0, "max": 2, "step": 1}}


def test_override_narrows_and_keeps_key_order():
    out = apply_range_overrides(SPACE, {"cond:x:value": {"min": 2.0, "max": 4.0}})
    assert list(out) == list(SPACE) and out["cond:x:value"]["min"] == 2.0
    assert SPACE["cond:x:value"]["min"] == 0.0          # input untouched


@pytest.mark.parametrize("ov, msg", [
    ({"nope": {"min": 1, "max": 2}}, "unknown gene"),
    ({"cond:y:mode": {"min": 0, "max": 1}}, "choice"),
    ({"cond:x:value": {"min": -1.0, "max": 4.0}}, "outside"),
    ({"cond:x:value": {"min": 5.0, "max": 4.0}}, "min > max"),
    ({"cond:x:value": {"min": 2.0}}, "both min and max"),
])
def test_bad_overrides_are_refused(ov, msg):
    with pytest.raises(ValueError, match=msg):
        apply_range_overrides(SPACE, ov)


def test_overrides_change_the_checkpoint_fingerprint():
    ga = {"populationSize": 4, "generations": 1}
    a = checkpoint_fingerprint(SPACE, ga)
    b = checkpoint_fingerprint(apply_range_overrides(SPACE, {"cond:x:value": {"min": 2.0, "max": 4.0}}), ga)
    assert a != b


def test_param_space_for_optimization_equals_the_old_inline_result():
    """Regression for the extraction: a real launcher-built O_LC config gives the same space,
    same ORDER, as collect_param_space with the split the handler used to do inline."""


def test_seed_population_is_strict_and_padded_to_the_population():
    # build a GeneticOptimizer on SPACE, pop 5; two seeds; expect 5 individuals, first two decode back
    ...


def test_more_seeds_than_the_population_is_refused(): ...
def test_seeds_and_warm_start_together_fail_the_job(): ...        # _fail message names both keys
def test_a_checkpoint_still_wins_over_seeds(): ...                 # resume path unchanged
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**
- `apply_range_overrides(space, overrides)` does the following:
  - it applies only to numeric (`int`/`float`) genes;
  - it needs both `min` and `max`, inside the original range, with `min ≤ max`;
  - it preserves `type`/`step`;
  - it returns a NEW dict in the original key order;
  - it refuses unknown keys and choice genes.
  Lattice alignment is NOT required (see Task 10's decode-image rule). The caller checks every
  seed with `encode_params_strict` against the narrowed space.
- `param_space_for_optimization(strategy, optimization_config, bypass)` = the extracted code +
  `apply_range_overrides(space, cfg["rangeOverrides"])` when `"rangeOverrides" in cfg`. Absence
  is the documented "no overrides" state of every existing config, and the explicit membership
  test keeps the no-defaults rule visible. The handler calls it where it called `collect_param_space`, so `opt.parameter_ranges`
  now records the NARROWED space, which is the truth.
- In the non-checkpoint branch:
  ```python
  seeds = ga["seedPopulation"] if "seedPopulation" in ga else None
  if seeds is not None and ga.get("warmStartFromOptimizationId"):
      return _fail(opt_id, db, "seedPopulation and warmStartFromOptimizationId are mutually exclusive")
  if seeds is not None:
      try:
          init_pop = _build_seed_population(seeds, optimizer, int(ga["populationSize"]))
      except ValueError as e:
          return _fail(opt_id, db, f"seedPopulation refused: {e}")
      logger.warning(f"strategy_optimization {opt_id}: seeded {len(seeds)} of "
                     f"{ga['populationSize']} individuals (rest random, same seed)")
  elif warm_start_id: ...   # unchanged
  ```
  `_build_seed_population` = strict encode each, refuse if `len(seeds) > target`, and pad with
  `optimizer.toolbox.individual()`. The RNG was seeded earlier, so this is deterministic.

**Step 4: Run the new file + `test_trial_memory_log.py` + any warm-start test**
(`grep -l warmStart tests/`). Expected: PASS; the warm-start tests are unchanged.

**Step 5: Commit.** `feat(stage2): strict seedPopulation + rangeOverrides in the optimization handler`

---

### Task 12: `authored_genome`: the template's own value for every strategy gene

**Why.** "Leave un-seeded members at their authored defaults (never min)." Nothing in the code
maps a template to a genome today; `_collect_authored_values` covers only
`value_offset_from` bases.

**Files:**
- Modify: `testplatform/backend/app/services/strategy_param_space.py`
- Test: `testplatform/backend/tests/test_authored_genome.py`

**Step 1: Write the failing tests.**

```python
import pytest
from app.services.strategy_param_space import authored_genome, collect_param_space, decode_params
from tests.test_option_grid_foundations import _launcher

KEYS = ["O_LC", "O_LP", "O_IC", "O_BULLPS", "O_BEARCS", "O_STRD", "O_ERN", "O_CBS",
        "O_LEAP", "O_PMCC", "O_CONVEX", "O_WHEEL", "OS1"]


@pytest.mark.parametrize("key", KEYS)
def test_every_strategy_gene_gets_an_authored_value(key):
    m = _launcher()
    s = m._build_strategy(key, f"a-{key}", "DeterministicScorer")
    space = collect_param_space(s)
    g = authored_genome(s, space)
    strategy_genes = {k for k in space if k.split(":")[0] in ("cond", "entry", "exit", "optsel")}
    assert set(g) == strategy_genes


@pytest.mark.parametrize("key", KEYS)
def test_decoding_the_authored_genome_reproduces_the_template(key):
    """Round trip: every leaf keeps its authored op/value (or the nearest lattice point the
    decoder can produce, listed in KNOWN_LATTICE below), every rule the template enables is
    still there, every authored-OFF rule (opt_sl_ml on O_PMCC/O_CONVEX, opt_time on O_CONVEX) is
    still off, and every option action keeps its strike/dte/sizing/cross."""


KNOWN_LATTICE = {("O_LEAP", "option_dte"): (425, 420), ("O_PMCC", "option_dte"): (425, 420)}   # Q12


def test_toggles_follow_the_authored_enabled_flag():
    m = _launcher()
    s = m._build_strategy("O_PMCC", "a", "FMPRating")
    g = authored_genome(s, collect_param_space(s))
    assert g["exit:opt_sl_ml:enabled"] == 0 and g["exit:opt_tp:enabled"] == 1


def test_mode_genes_take_the_authored_direction():
    m = _launcher()
    s = m._build_strategy("O_LP", "a", "FMPRating")
    g = authored_genome(s, collect_param_space(s))
    assert g["cond:o_lp-signal:mode"] == "below"       # bearish member, authored '<'


def test_model_schedule_screener_genes_are_not_authored_here():
    ...  # a space carrying model:/schedule:/screener: keys -> absent from the result, no error


def test_a_gene_the_template_cannot_answer_is_refused():
    ...  # hand-made space with cond:ghost:value -> ValueError naming 'ghost'
```

**Step 2: Run.** Expected: FAIL (`ImportError: authored_genome`).

**Step 3: Implement.** Walk the entry and exit rule lists once (`_walk_condition_nodes`' order),
build `by_id` for leaves and `by_rule` for rules, then answer each gene key:

| gene | authored value |
|---|---|
| `cond:<id>:value` | `leaf["value"]` |
| `cond:<id>:enabled` | `1` (a present leaf is on; the smoke-mode removal is not a template value) |
| `cond:<id>:confirmation_bars` | `leaf["confirmation_bars"]` |
| `cond:<id>:mode` | `leaf["mode"]` if set; numeric leaf: `{">": "above", ">=": "above", "<": "below", "<=": "below"}[op]`; categorical: the choice whose registry code equals `leaf["value"]` (`field_spec`) |
| `<ns>:<rid>:enabled` | `0 if rule.get("enabled") is False else 1` |
| `<ns>:<rid>:a<i>:option_strike_param` / `_delta` / `_delta_long` | the action's own key |
| `…:option_strike_method` / `…:option_structure` | the action's method / `action_type` (must be one of the gene's choices) |
| `…:option_dte` | `(option_dte_min + option_dte_max) // 2` (the inverse of `_apply_option_dte`) |
| `…:option_wing_width` / `option_sizing` / `option_min_arc` / `option_entry_cross` / `action_value` | `option_wing_width_pct` / `option_sizing` / `option_min_arc` / `option_entry_cross` / `action_value` |
| `optsel:<half>:<w>` | `option_<w>` of the first action with that half (all of them are equal; `_collect_action_genes` refuses conflicting domains) |
| `model:` / `schedule:` / `screener:` | not authored (absent) |

Numeric values are returned SNAPPED to the gene's decode image: `decode(clip(v, min, max))`,
the value the GA can actually produce. This makes every authored genome pass
`encode_params_strict` (Task 10). A snap that moves a value (O_LEAP/O_PMCC `option_dte`
425 → 420) is legitimate and listed in `KNOWN_LATTICE`. A template value more than one step
outside `[min, max]` is a template bug and raises. Any gene you cannot answer raises `ValueError`
naming the key.

**Step 4: Run.** Expected: PASS. Record the `KNOWN_LATTICE` entries in the task report.

**Step 5: Commit.** `feat(stage2): authored_genome -- template values for every strategy gene`

---

### Task 13: `style_seeding.translate_member_genes`

**Files:**
- Create: `testplatform/backend/app/services/style_seeding.py`
- Test: `testplatform/backend/tests/test_style_seeding_translate.py`

**Step 1: Write the failing tests.**

```python
import pytest
from app.services.style_seeding import SeedRefused, translate_member_genes, SHARED_KEY_PREFIXES
from app.services.strategy_param_space import collect_param_space
from tests.test_option_grid_foundations import _launcher


def _pair(style, member):
    m = _launcher()
    m._STYLE_ACTIVE = m._load_style_spec(CAND, style)
    s = m._build_strategy_style("O_STYLE")
    src = m._STRATEGY_BUILDERS[member](member)
    return s, src, s.style_id_maps[member]


def test_a_single_members_full_genome_translates_into_the_style_space():
    s, src, idm = _pair("short", "O_LC")
    genes = {k: 1 for k in collect_param_space(src)}       # values irrelevant here
    member, shared = translate_member_genes("O_LC", genes, idm)
    space = collect_param_space(s)
    assert set(member) | set(shared) <= set(space)
    assert "cond:o_lc-tp:value" in member and "cond:tp:value" not in member
    assert "exit:o_lc-opt_tp:enabled" in member
    assert "cond:shared-rel_volume:value" in shared


# STALE (2026-09-26, "Tasks 5-6 as built"): inside a style the wheel's o_csp-* entry ids are
# o_wheel-o_csp-*, so cond:o_csp-iv_rank:value translates to cond:o_wheel-o_csp-iv_rank:value;
# rewrite this test against the id map when Task 13 is built.
def test_the_wheel_translates_its_o_csp_entry_by_identity_and_prefixes_its_exits():
    s, src, idm = _pair("premium", "O_WHEEL")
    genes = {k: 1 for k in collect_param_space(src)}
    member, _ = translate_member_genes("O_WHEEL", genes, idm)
    assert "cond:o_csp-iv_rank:value" in member
    assert "cond:o_wheel-ccdte:value" in member
    assert "exit:o_wheel-cc_sell:a0:option_strike_param" in member


@pytest.mark.parametrize("bad", ["cond:o_lp-iv_rank:value",       # another member's id
                                 "exit:opt_ghost:enabled", "screener:price_max",
                                 "model", "garbage:key"])
def test_an_unplaceable_key_is_refused_by_name(bad):
    s, src, idm = _pair("short", "O_LC")
    with pytest.raises(SeedRefused, match=bad.split(":")[1] if ":" in bad else bad):
        translate_member_genes("O_LC", {bad: 1}, idm)


def test_shared_prefixes_are_exactly_the_cross_member_namespaces():
    assert SHARED_KEY_PREFIXES == ("cond:shared-", "optsel:", "model:", "schedule:")
```

**Step 2: Run.** Expected: FAIL (the module does not exist).

**Step 3: Implement.**

```python
"""Stage-2 seeding: translate stage-1 / LEAPS-grid genomes into a style job's gene space.

Pure functions over dicts. The style builder (ba2test_launcher._build_strategy_style) supplies the
per-member id maps; the launcher supplies the target space and the authored genome. Every key that
cannot be placed is a SeedRefused naming it -- the thing encode_params never did.
"""
SHARED_KEY_PREFIXES = ("cond:shared-", "optsel:", "model:", "schedule:")


class SeedRefused(ValueError):
    pass


def translate_member_genes(member, genes, id_map):
    """({style_key: value} member-specific, {style_key: value} shared). Delegates the per-key
    rule to the launcher's _style_translate_key logic, re-implemented here as a pure function
    over id_map ({exit_rules, exit_conds, entry_rules, entry_conds}):
      cond:<id>:*  -> exit cond (prefixed) | shared-* (shared) | entry cond (identity) | REFUSE
      entry:<rid>:* -> entry rule (identity) | REFUSE
      exit:<rid>:*  -> exit rule (prefixed) | REFUSE
      optsel:/model:/schedule: -> shared
      anything else (screener:, unprefixed) -> REFUSE"""
```

Then make the launcher's `_style_translate_key` (Task 6) CALL this module's per-key function, so
there is one implementation. The Task 6 test keeps passing.

**Step 4: Run this file and Task 6's genome test.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): translate member genomes into style keys, refusing unplaceable keys`

---

### Task 14: `style_seeding.assemble_individual` + `narrow_ranges`

**Files:**
- Modify: `testplatform/backend/app/services/style_seeding.py`
- Test: `testplatform/backend/tests/test_style_seeding_assemble.py`

**Policy fields this consumes** (from the bundle, Task 15). They are decided by the user later;
this code only obeys them:
- per individual: `contributions` [{member, genes}], `shared_from` (index), `other_members`
  (`"authored"` | `"off"`);
- global: `fill_missing_member_genes` (bool, default false = refuse), `out_of_range` (`"refuse"` |
  `"clamp"`), `narrow` (`null` | `{"k": int, "scope": ["member"|"shared"|"model"]}`).

**Step 1: Write the failing tests.** Use the premium style's real space
(`collect_param_space(style) + a fake model:/schedule: block`) and hand-made contribution genomes
from `authored_genome` of each member's own strategy. That gives valid values without a DB.

```python
def test_a_seeded_member_is_ON_with_its_genes_and_the_others_at_AUTHORED_defaults(): ...
    # entry:o_bullps-entry:enabled == 1; o_ic genes == authored; entry:o_ic-entry:enabled == 1

def test_other_members_off_turns_their_entry_toggles_to_0_only(): ...

def test_shared_keys_come_from_shared_from_and_conflicts_are_REPORTED_not_hidden(): ...
    # two contributions disagree on cond:shared-gate_confidence:value -> value from index 0,
    # provenance["conflicts"] == [{"key": ..., "values": [...], "used": 0}]

def test_a_shared_key_no_contribution_has_falls_back_to_authored_or_is_refused(): ...
    # optsel:* -> authored (provenance origin "authored"); model:* missing -> SeedRefused

def test_a_member_gene_missing_from_its_source_is_refused_unless_policy_fills_it(): ...

def test_out_of_range_refuse_and_clamp(): ...
    # a mid-term O_LC seed with option_dte 35 vs the override range 60..120:
    # "refuse" -> SeedRefused naming the key; "clamp" -> 60, provenance records the clamp

def test_the_result_covers_the_target_space_exactly(): ...

def test_narrow_ranges_is_the_union_of_seed_windows_clipped_to_the_original(): ...
    # two seeds 20 and 30 on a step-5 gene [0,100], k=2 -> {min: 10, max: 40}
    # toggles and choice genes are never narrowed; scope ["member"] leaves shared/model alone;
    # every seed then encodes strictly against the narrowed space (Task 10)
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**

```python
def assemble_individual(space, authored, id_maps, contributions, *, shared_from=0,
                        other_members="authored", fill_missing_member_genes=False,
                        out_of_range="refuse"):
    """-> (genes covering EXACTLY `space`, provenance). Order of resolution per key:
    seeded member keys from their contribution (translated); the seeded members' entry toggles
    = 1 unless the source itself carried them (O_LEAP/O_CONVEX arms); shared keys from
    contributions[shared_from], else the first contribution that has them, else authored, else
    refuse; un-seeded members' keys from authored (entry toggles 0 when other_members='off').
    Values are range-checked against `space` (decode-image rule, Task 10) per out_of_range."""


def narrow_ranges(space, individuals, provenances, *, k, scope):
    """{gene: {min, max}} for numeric genes whose values came from a SOURCE in the requested
    scope: [max(orig_min, min(v) - k*step), min(orig_max, max(v) + k*step)] over every
    individual. Never toggles (0/1 int genes named *:enabled) or choice genes."""
```

**Step 4: Run.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): assemble seeded style individuals (authored defaults, reported conflicts, optional narrowing)`

---

### Task 15: `tools/build_style_seed_bundle.py` (read-only DB extraction)

**Files:**
- Create: `tools/build_style_seed_bundle.py`
- Test: `testplatform/backend/tests/test_style_seed_bundle_tool.py` (a temp SQLite DB with
  hand-inserted `strategy_optimizations` and `backtests` rows, as
  `test_persist_distinct_topn_tool.py` does)

**Policy file (input):**

```json
{
  "schema": 1,
  "style": "premium",
  "individuals": [
    {"contributions": [
       {"member": "O_BULLPS", "source": {"opt_name": "optm-DeterministicScorer-O_BULLPS-…-d…", "pick": "best"}},
       {"member": "O_IC",     "source": {"opt_id": 812, "pick": "distinct", "rank": 2}}],
     "shared_from": 0, "other_members": "authored"},
    {"contributions": [{"member": "O_WHEEL", "source": {"backtest_id": 4411}}],
     "shared_from": 0, "other_members": "off"}
  ],
  "narrow": {"k": 2, "scope": ["member"]},
  "out_of_range": "refuse",
  "fill_missing_member_genes": false
}
```

**Bundle file (output):** the policy, plus for every contribution: `genes` (flat gene dict),
`source_space_keys` (sorted keys of the source's `parameter_ranges`), and `resolved` (`opt_id`,
`opt_name`, `status`, `pick`, `rank`, `fitness`, `params_key`, `backtest_id`). Also
`policy_sha256`, `built_at`, `db_path`.

**Step 1: Write the failing tests.**

```python
def test_best_pick_reads_best_params_and_the_source_space(tmp_db): ...
def test_distinct_pick_uses_select_behaviour_distinct_with_its_default_tolerances(tmp_db): ...
def test_backtest_pick_strips_only_the_named_camelcase_extras(tmp_db): ...
    # entryRules / exitRules / expertFixedSettings removed; genes remain
def test_a_row_pin_key_is_refused(tmp_db): ...
    # strategy_params carrying "_atr_swap_migration" or "_inert_toggle_pin" -> exit 2, names it
    # (memory: "Stored row pins beat the genome")
def test_a_genome_whose_keys_differ_from_its_source_space_is_refused(tmp_db): ...
def test_a_running_source_is_refused_without_allow_running(tmp_db): ...
def test_a_missing_rank_or_source_is_refused(tmp_db): ...
def test_the_tool_never_writes_the_db(tmp_db): ...      # open ?mode=ro, compare file bytes
def test_policy_schema_errors_name_the_field(tmp_path): ...
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**
- `argparse`: `--policy`, `--out`, `--db` (default `DB_FILE`), `--allow-running`.
- Open SQLite read-only (`file:…?mode=ro`, as `stage1_gen_report.py` does). No launcher import, no
  backend session: this tool is pure extraction.
- `distinct` picks import `app.services.distinct_topn.select_behaviour_distinct` (add
  `testplatform/backend` to `sys.path` the way `persist_distinct_topn._bootstrap` does, but without
  `_enter_backend`).
- A gene key is recognised as `^(cond|entry|exit|optsel|model|schedule|screener):`. Any other key
  in a backtest's `strategy_params` must be one of `{"entryRules", "exitRules", "expertFixedSettings"}`,
  else exit 2 naming it.
- Exit codes: 0 ok, 2 refused (message on stderr).

**Step 4: Run.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): build_style_seed_bundle -- resolve seed sources read-only into a bundle`

---

### Task 16: Launcher: `--seed-population` / `--seed-check-only`

**Files:**
- Modify: `testplatform/ba2test_launcher.py`:
  - `_cmd_optimize`: extract `_optimize_gene_blocks(spec, args, bypass)` from the `expert_params`
    expression (~6290) and call it before `db.add(strat)`;
  - the seed assembly;
  - the argparse flags.
- Test: `testplatform/backend/tests/test_launcher_seed_population.py`

**Step 1: Write the failing tests.** Use the `_run_optimize` harness plus a bundle built
in-test. Its genes come from `authored_genome` of each member's own strategy, and its fake
`model:`/`schedule:` blocks come from the real spec's gene blocks.

```python
def test_a_bundle_becomes_seedPopulation_rangeOverrides_and_provenance(monkeypatch, tmp_path):
    cfg = _run_optimize(_parse(_argv(tmp_path, "premium", ["--seed-population", str(bundle)])), monkeypatch)
    assert len(cfg["seedPopulation"]) == 2
    assert set(cfg["seedPopulation"][0]) == set(cfg_space)        # full coverage
    assert cfg["backtest"]["seed_bundle_sha256"] == sha256(bundle)
    assert cfg["seedProvenance"][0]["contributions"][0]["member"] == "O_BULLPS"


def test_check_only_prints_the_report_and_writes_nothing(monkeypatch, tmp_path, capsys):
    # no Strategy / StrategyOptimization row created; stdout lists space size, seeds, conflicts,
    # narrowed genes; exit 0


def test_seed_population_requires_o_style(): ...
def test_seed_population_and_warm_start_are_mutually_exclusive(): ...
def test_a_bundle_for_another_style_is_refused(): ...
def test_an_unplaceable_seed_key_is_a_launch_refusal_not_a_trial_failure(): ...
def test_the_gene_blocks_extraction_leaves_every_existing_job_config_identical(monkeypatch):
    """For S2, O_LC, OS1 and O_WHEEL under FMPRating and DeterministicScorer, cfg['expert_params']
    before/after the extraction is identical (compare against a dict captured from the Task-0
    baseline, or against a direct call of the old expression kept in the test)."""
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**
- New flags on `optimize`: `--seed-population PATH` (a bundle) and `--seed-check-only`.
- After the strategy is built and `_optimize_gene_blocks` has run, compute the space with
  `param_space_for_optimization(strat, {"expert_params": blocks}, bypass)`, without overrides.
  Then:
  - `authored = authored_genome(strat, space)`;
  - for each bundle individual, run `assemble_individual(...)` with `strat.style_id_maps`;
  - if `narrow`, `overrides = narrow_ranges(...)`, then re-check every seed with
    `encode_params_strict` against `apply_range_overrides(space, overrides)`. A `GeneticOptimizer`
    built on that space is enough; no DB is needed.
- On success, set `cfg["seedPopulation"]`, `cfg["rangeOverrides"]`, `cfg["seedProvenance"]` and
  `backtest_block["seed_bundle_sha256"]`. Print one summary line: space size, seeds, per-seed
  members, conflicts, narrowed-gene count.
- `SeedRefused`/`ValueError` → `sys.exit("optimize: seed bundle refused: …")`.
- `--seed-check-only` prints and returns 0 BEFORE any DB write.

**Step 4: Run the new file + `test_equity_cap_launcher.py` + `test_gene_to_artefact_audit.py`.**
Expected: PASS.

**Step 5: Commit.** `feat(stage2): optimize --seed-population/--seed-check-only for style jobs`

---

### Task 17: Driver `--profile style` + identity digest + dry-run

**Files:**
- Modify: `tools/run_options_matrix.py` (`build_parser`, `resolve_args`, `build_cmd`,
  `discovery_name`, `planned_jobs`, `main`)
- Test: `testplatform/backend/tests/test_option_style_driver.py`

**Step 1: Write the failing tests.**

```python
def style_args(tmp_path, *extra):
    return M.resolve_args(M.build_parser(), [
        "--profile", "style", "--style-config", CAND, "--styles", "short,premium",
        "--screener-gate-store", "S", "--max-stock-price", "0",
        "--option-size-within-fill-volume",
        "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
        "--market-condition-manifest", "ohlcv-v1=a,ta-structure-v1=b",
        "--allow-unseeded", *extra])


def test_style_defaults_are_the_stage2_decisions(tmp_path):
    a = style_args(tmp_path)
    assert (a.population, a.generations, a.early_stop) == (300, 80, 10)
    assert a.fitness == "option_car_target_soft30" and a.experts == "DeterministicScorer"
    assert a.universe_file.endswith("options_universe_large_cap.txt")


@pytest.mark.parametrize("drop, msg", [
    ("--option-size-within-fill-volume", "fill-volume"),
    ("--market-condition-profile", "market-condition"),
    ("--screener-gate-store", "gate"),
    ("--allow-unseeded", "seed"),
])
def test_every_stage1_flag_is_REQUIRED(tmp_path, drop, msg): ...

def test_one_job_per_expert_and_style_carrying_the_style_flags(tmp_path): ...
    # names optm-DeterministicScorer-O_STYLE-short<suffix>-d<12 hex>; cmd has
    # --strategy O_STYLE --style-config CAND --style short (+ --seed-population when seeded);
    # labels get "O_STYLE" and "style-short"

def test_identity_covers_the_resolved_style_not_the_path(tmp_path): ...
    # copy CAND to another path -> same names; change one member's override -> short changes,
    # premium does not

def test_identity_covers_the_seed_bundle_content(tmp_path): ...
    # --seed-dir with short.json; edit one byte -> the short job's name changes

def test_identity_covers_universe_manifests_and_flags(tmp_path): ...
    # a different universe file / manifest digest / fitness each change the name

def test_a_missing_seed_file_for_a_selected_style_is_refused(tmp_path): ...

def test_dry_run_prints_members_status_seed_and_commands_without_a_db(monkeypatch, tmp_path, capsys): ...
    # "CANDIDATE membership" banner, members in order, seed sha or UNSEEDED, and each command

def test_discovery_and_matrix_are_unchanged():
    """Delegates to Task 0's pin; also assert _jobs/_DISCOVERY_* untouched."""
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**
- `--profile {matrix,discovery,style}`.
- New flags: `--style-config`, `--styles` (default: every style in the file, in file order),
  `--seed-dir` (expects `<dir>/<style>.json`), `--allow-unseeded`.
- `resolve_args` for style:
  - defaults: pop 300, gen 80, early-stop 10, mutation 0.3, fitness `option_car_target_soft30`,
    experts `DeterministicScorer`, `universe_file` = `tools/options_universe_large_cap.txt` (unless
    passed);
  - refuse without `--screener-gate-store` and `--max-stock-price 0`;
  - refuse without `--option-size-within-fill-volume`;
  - refuse with market profile `none` or without a manifest;
  - refuse unless there is exactly one of `--seed-dir` / `--allow-unseeded`;
  - refuse neutral-entry-modes other than legacy;
  - experts must be ⊆ {DeterministicScorer, FMPRating} (Q10);
  - load the style file with the launcher module's `_load_style_spec` (through `_launcher_module`,
    so validation is the launcher's own) and refuse any invalid selected style;
  - refuse a selected style with no seed file under `--seed-dir`;
  - refuse a selected style whose `status` is `candidate` unless the run passes an explicit
    acknowledgement flag (e.g. `--allow-candidate-styles`), and print the acknowledgement in
    the banner (Task 8 review M3). Every style in the candidates file is `candidate` today, so
    the pilot passes the flag knowingly; an `approved` style needs none.
- `planned_jobs` (style): for expert × style, name `optm-{expert}-O_STYLE-{style}{spread_tag}{suffix}`
  → `discovery_name(..., extra_identity={"style_sha256": <the loaded style's `spec_sha256`>, "seed_bundle_sha256": <sha or None>})`.
  The identity digest uses `spec_sha256` (the canonical-JSON hash of the style's CONTENT,
  `_load_style_spec`) and NEVER `spec_path`: the run config's `option_style` block carries
  `spec_path` too, so hashing that block (or the path) would rename every job when the
  file moves and keep the name when the content changes (Task 8 review M2).
- `discovery_name`:
  - new kwarg `extra_identity=None`, merged into `identity` ONLY when given, so discovery digests
    stay byte-identical (Task 0's pin);
  - in the token loop, `--style-config` and `--seed-population` VALUES are excluded (paths are not
    identity; their content is, via `extra_identity`);
  - `--style` stays in `args`.
- `build_cmd`: new kwarg `style=None`. When set, append
  `--style-config <path> --style <name> [--seed-population <dir>/<name>.json]` and the
  `style-<name>` label.
- `main`: print the style banner in the dry-run and in the launch header (status, members, price
  cap policy, seed file + sha, or UNSEEDED).

**Step 4: Run this file + `test_option_discovery_driver.py` + Task 0's pin +
`test_matrix_labels_do_not_move_identity.py` + `test_option_matrix_market_condition_flag.py` +
`test_robust_fitness_default_on.py`.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): run_options_matrix --profile style with a content-hashed job identity`

---

### Task 18: `tools/stage2_run.sh` + wiring test

**Files:**
- Create: `tools/stage2_run.sh` (a NEW file; `stage1_run.sh` stays untouched)
- Test: `testplatform/backend/tests/test_stage2_run_sh.py` (grep-style, like `test_stage1_run_sh.py`)

**Step 1: Write the failing test.**

```python
def test_stage2_script_wiring():
    t = _text("tools/stage2_run.sh")
    assert "--profile style" in t
    assert 'POP="${POP:-300}"' in t and 'GEN="${GEN:-80}"' in t and "--early-stop 10" in t
    assert 'PARALLEL="${PARALLEL:?' in t                 # sized from the pilot, never defaulted
    assert "--option-size-within-fill-volume" in t
    assert "--max-stock-price 0" in t and "--screener-gate-store" in t
    assert "options_universe_large_cap.txt" in t
    assert 'MARKET_CONDITION_PROFILE="${MARKET_CONDITION_PROFILE:-ohlcv-v1,ta-structure-v1}"' in t
    assert "MARKET_CONDITION_MANIFEST" in t and "prepare-host" in t and " build " not in t  # pre-built digests only
    assert "--fitness option_car_target_soft30" in t
    assert 'STAGE2_SUFFIX="${STAGE2_SUFFIX:--st2}"' in t
    assert "STYLE_CONFIG" in t and "SEED_DIR" in t and "STAGE2_ALLOW_UNSEEDED" in t
    assert "build_shared_arrays.py" in t and "0 built / 820 opened" in t
    assert "smaps_rollup" in t


def test_stage1_script_is_byte_identical_to_the_baseline():
    assert _sha("tools/stage1_run.sh") == "<sha256 recorded in Task 0>"
```

**Step 2: Run.** Expected: FAIL (no file).

**Step 3: Write the script.**
- Base it on `stage1_run.sh`'s env block: BA2_HOME / DB_FILE / DATABASE_URL / FMP_KEY /
  PYTHONPATH / `BT_MAX_TASKS_PER_CHILD=32`. Add `STAGE2_REPO`
  (default `/home/debian/ba2-grid/repo-stage2`): a SEPARATE checkout, because the stage-1
  checkout must not change under a running grid.
- Market conditions: REQUIRE `MARKET_CONDITION_MANIFEST` as `profile=digest` pairs (the manifests
  are built locally and synced, per the user decision). Run `verify` + `prepare-host` per profile
  and refuse if the manifest is unset. Never run `plan`/`build` here.
- Seeds: `SEED_DIR` or `STAGE2_ALLOW_UNSEEDED=1` (exactly one).
- Fitness: fixed `--fitness option_car_target_soft30`. `STAGE2_FITNESS` overrides it only with a
  non-default `STAGE2_SUFFIX` (the same refusal as stage 1).
- The header runbook covers:
  - the mandatory prewarm for 820 names
    (`build_shared_arrays.py --universe-file tools/options_universe_large_cap.txt …`, run twice;
    the second run must say `0 built / 820 opened`);
  - PARALLEL sized from `tools/stage2_cost_report.py` (Task 19);
  - measuring PRIVATE bytes via `smaps_rollup`;
  - "never run beside stage 1".
- `exec … tools/run_options_matrix.py --profile style --launcher "$STAGE2_REPO/testplatform/ba2test_launcher.py" --universe-file tools/options_universe_large_cap.txt --style-config "$STYLE_CONFIG" --styles "$STAGE2_STYLES" …`

**Step 4: Run.** Expected: PASS. Then run `bash -n tools/stage2_run.sh` (syntax only).

**Step 5: Commit.** `feat(stage2): tools/stage2_run.sh (separate checkout, pre-built manifests, sized PARALLEL)`

---

### Task 19: Cost hook: private bytes per trial + `tools/stage2_cost_report.py`

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py`
  (`_trial_memory_snapshot` ~547, `_log_trial_memory` ~1026)
- Create: `tools/stage2_cost_report.py`
- Test: `testplatform/backend/tests/test_trial_memory_log.py` (extend),
  `testplatform/backend/tests/test_stage2_cost_report.py`

**Step 1: Write the failing tests.**

```python
# test_trial_memory_log.py
def test_private_mb_is_logged_after_rss_when_known(caplog):
    _log_trial_memory(0, 1, 1, 28, {"rss_mb": 900, "private_mb": 610, "bar_cache": {}, "series_memo": {}}, secs=812.4)
    assert "| rss 900MB | priv 610MB |" in caplog.text


def test_the_line_is_unchanged_when_private_mb_is_absent(caplog):
    _log_trial_memory(0, 1, 1, 28, {"rss_mb": 900, "bar_cache": {}, "series_memo": {}}, secs=812.4)
    assert "priv" not in caplog.text


def test_private_mb_reads_smaps_rollup(tmp_path, monkeypatch):
    f = tmp_path / "smaps_rollup"
    f.write_text("Rss: 1000 kB\nPrivate_Clean: 2048 kB\nPrivate_Dirty: 1024 kB\n")
    assert _private_mb(str(f)) == 3


# test_stage2_cost_report.py
LINES = ["… mem gen 1/1 ind 1/28 | 800.0s | rss 2100MB | priv 1500MB | bars …",
         "… mem gen 1/1 ind 2/28 | 1200.0s | rss 2300MB | priv 1800MB | bars …"]


def test_summary_and_parallel_recommendation():
    r = report(parse(LINES), ram_gb=128, reserve_gb=16, shared_gb=20, population=300, generations=80)
    assert r["n"] == 2 and r["secs_p95"] >= 1180 and r["priv_p95_mb"] >= 1780
    assert r["parallel"] == (128 - 16 - 20) * 1024 // r["priv_p95_mb"]
    assert r["gen_hours"] == pytest.approx(math.ceil(300 / r["parallel"]) * r["secs_p95"] / 3600)


def test_rss_is_used_only_as_a_flagged_fallback():
    r = report(parse([l.replace(" | priv 1500MB", "") for l in LINES[:1]]), ram_gb=64, reserve_gb=8, shared_gb=10, population=28, generations=1)
    assert r["memory_basis"] == "rss (overcounts shared maps)"
```

**Step 2: Run.** Expected: FAIL.

**Step 3: Implement.**
- `_private_mb(path="/proc/self/smaps_rollup")` returns Private_Clean + Private_Dirty in MB, or
  None when the file is absent (Windows). `_trial_memory_snapshot` adds
  `snap["private_mb"] = _private_mb()`.
- `_log_trial_memory` adds `f" | priv {mem['private_mb']}MB"` right after the rss segment, only
  when it is not None. The Windows/local log stays byte-identical.
- `tools/stage2_cost_report.py`:
  - `parse(lines)` → records `{gen, ind, secs, rss_mb, private_mb}`;
  - `report(...)` → n, secs p50/p95/max, memory p50/p95/max (private, else rss flagged),
    `parallel = floor((ram - reserve - shared) GB / p95 MB)`, `gen_hours = ceil(pop/parallel) × p95 secs`,
    and `total_days_ceiling = gen_hours × generations / 24`;
  - CLI: `--log FILE [--log FILE…] --ram-gb --reserve-gb --shared-gb --population --generations`.
  - `--shared-gb` is the measured size of the host's `_derived` shared-array cache for the 820
    names (`du -sh <CACHE_FOLDER>/_derived`). Its help text says so.

**Step 4: Run both files.** Expected: PASS.

**Step 5: Commit.** `feat(stage2): per-trial private bytes + stage2_cost_report sizing tool`

---

### Task 20: Pilot command + runbook check (dry-run only in this task)

**Files:**
- Modify: `tools/stage2_run.sh` header (the pilot block)
- Test: extend `test_stage2_run_sh.py` (the header contains the pilot command verbatim)

**The pilot** (user-approved launch only, at a stage-1 job boundary, on remote227, from the
SEPARATE checkout `/home/debian/ba2-grid/repo-stage2`, never beside a running stage-1 job):

```bash
# 1. prewarm 820 names (twice; the second run must report 0 built / 820 opened)
# 2. choose the style with the LARGEST validated membership (Task 6 genome counts; today "mid")
STYLE_CONFIG=tools/option_styles.candidates.json STAGE2_STYLES=mid \
STAGE2_ALLOW_UNSEEDED=1 POP=28 GEN=1 STAGE2_START=2023-01-01 STAGE2_END=2025-12-31 \
STAGE2_SUFFIX=-st2pilot PARALLEL=28 \
MARKET_CONDITION_MANIFEST=ohlcv-v1=<820-digest>,ta-structure-v1=<820-digest> \
  tools/stage2_run.sh 2>&1 | tee /home/debian/ba2-grid/logs/st2pilot.log
# 3. size --parallel for the real run
python tools/stage2_cost_report.py --log /home/debian/ba2-grid/logs/st2pilot.log \
  --ram-gb <box> --reserve-gb 16 --shared-gb <du _derived> --population 300 --generations 80
```

- Pop 28 at `PARALLEL=28` is one batch. If the per-slot private estimate says 28 slots cannot
  fit, lower PARALLEL for the pilot too. The 2020 tree OOM'd at 20 slots before the shared arrays
  landed.
- The pilot's job name differs from any real job (`-st2pilot`, and the 2023 window is in the
  digest), so it can never be resumed into.

**Step 1: Add the pilot block to the header and pin it in the test.**

**Step 2: Dry-run it locally.** Run with `--dry-run`, a fake manifest (`ohlcv-v1=x,ta-structure-v1=x`)
and `STAGE2_REPO` pointing at this worktree. Expected: the resolved command, the style banner,
UNSEEDED and a digested job name. No job starts. Paste the output into the task report.

**Step 3: Seed-check dry-run.** Build a tiny policy against any completed optimization in the
LOCAL test DB (or skip, and say so, if none matches a candidate member). Run
`build_style_seed_bundle.py`, then `ba2-test optimize … --seed-population B --seed-check-only`.
Expected: the seed report and exit 0 with no DB writes. Paste it.

**Step 4: Commit.** `docs(stage2): pilot command in stage2_run.sh header (dry-run verified)`

---

### Task 21: No-impact gate, full suites, version bump

**Step 1: Run the three full suites.** Compare with Task 0: no new failures. Any baseline
failure now passing is fine; note it.

**Step 2: Run the no-impact set explicitly.** All must PASS byte-identical:
- `tests/test_stage1_identity_unchanged.py`;
- `tests/backtest/test_equity_golden_run.py`, `tests/backtest/test_engine_golden_regression.py`;
- every `tests/backtest/test_option_golden_run*.py` (a single-structure change is a bug here);
- `tests/test_option_grid_foundations.py`, `tests/test_option_strategy_builders.py`,
  `tests/test_launcher_volume_vol_genes.py`, `tests/test_wheel_*`, `tests/backtest/test_wheel_assignment.py`.

**Step 3: Discovery dry-run diff.** Run `run_options_matrix.py --profile discovery --dry-run` with
the stage-1 flags on this branch and on `3b3465c8`, then `diff` the two outputs. Expected: empty.

**Step 4: Version.** Bump `testplatform/version.py` `TEST_APP_VERSION` by one from its value AT
MERGE TIME (0089 when this was written). Do this only at a stage-1 job boundary and never while a
grid runs (the memory note on distributed optimize traps).

**Step 5: Commit.** `chore(stage2): TEST_APP_VERSION <n> -> <n+1>`. Do NOT push without the user.

---

## Open design questions (for the user; the build refuses loudly until each one is answered)

1. **Q1: O_CC (and O_PP) cannot be style members.** The backtest decides ONE entry path per run
   from `config["entry_action"]` (`daily_engine._entry_is_option`). An equity-entry member (a
   covered call's stock buy) and option-entry members cannot share an expert without engine work.
   The premium candidate list names O_CC. Options:
   (a) drop it from the premium style (the build's default refusal);
   (b) keep O_CC as its own expert;
   (c) engine work so mixed equity and option entries can coexist (large: RM sizing path, exits
       for stock positions, the per-member exits).
2. **Q2: `hold_assigned_stock` is account-level.** With O_WHEEL in a group, O_CSP / O_JL / O_RS
   assignments would ALSO be held and covered-called, which makes each of them a second wheel. The
   build refuses O_WHEEL together with any of the three. O_WHEEL's entry IS O_CSP's, so O_CSP adds
   nothing next to it; O_JL/O_RS are the real loss. The alternative is a member-scoped hold in the
   backtest engine (hold only when the assigned leg's order carries the wheel's `entry_tag`). That
   is BT-only because live never liquidates, and it is roughly an L task.
3. **Q3: mixed per-member price caps.** The gate is one screener block per job. The full-notional
   members (O_CSP/O_JL/O_RS, and O_WHEEL through O_CSP) carry $100; the defined-risk ones carry
   none. The build requires `price_cap_policy`:
   - `none` means no price gate, and sizing refuses unaffordable entries. The premium candidate
     uses this.
   - `group_min` caps every member at $100.
   A true per-member cap would need a new shared `underlying_price` entry leaf (BT and live).
   Which one?
4. **Q4: entry precedence.** The rule evaluator stops at the FIRST entry rule whose conditions
   pass (`TradeActionEvaluator`, `continue_processing=False`), even if that member's action then
   refuses (no contract, unaffordable, no LEAPS chain on one of the 820). Member ORDER in the spec
   is therefore precedence, and it is part of the job identity. Confirm the order per style, or ask
   for an evaluator change that falls through to the next rule on refusal. That would be a shared
   change affecting BT and live.
5. **Q5: LEAPS/convex fitness.** O_CONVEX was designed for `option_convex`, and the CAR family
   "teaches the GA to gut the convexity". Style jobs run soft30. The build refuses a style
   containing O_CONVEX under a CAR-family fitness unless the style has a `convex_fitness_waiver`.
   Options: waive it, drop O_CONVEX from the style (running it as its own convex job), or define a
   mixed objective.
6. **Q6: LEAPS chain depth on 820 names.** The LEAPS grid probes a per-strategy sub-universe
   (DTE ≥ 365; ≥ 270 for convex). A style job has one universe, so LEAPS members will refuse on
   names without deep chains, and under Q4 they also consume that symbol's entry slot for the
   bar. Options: accept this, restrict the leaps_convex style's universe to the probed subset
   (a driver input), or order the LEAPS members last.
7. **Q7: what "mid-term" means.** The candidate `mid` DTE windows (60-90, centres 60-120) and its
   td/dte exit bands are PLACEHOLDERS. Stage-1 seeds come from 25-60 DTE jobs, so their DTE genes
   fall outside a mid-term range. The seed policy's `out_of_range` (`refuse`/`clamp`) must be
   chosen.
8. **Q8: shared genes across seeds.** One expert means one set of `model:*` (DS weights),
   `cond:shared-*`, `optsel:*` and `schedule:*` per individual. By default they come from the
   `shared_from` contribution, and conflicts are reported. Confirm that, or ask for another rule
   (for example the best-fitness contribution).
9. **Q9: un-seeded members.** The spec says "authored defaults", which means ON. The policy also
   offers `other_members: "off"`. Pick per individual.
10. **Q10: experts.** DS only, or FMPRating as well? FMPRating has no bearish signal on large caps
    (memory). The driver accepts both and defaults to DS.
11. **Q11: warm start stays lenient.** `--warm-start-from` still encodes absent genes to `min`, and
    this plan leaves it (stage-1 no-impact). Should it become strict too?
12. **Q12: side finding, the gene lattice is anchored at zero.** `decode_individual` rounds
    `value/step` from 0, not from `min`. The O_LEAP/O_PMCC DTE centre gene (410..500 step 15)
    therefore decodes 410 → 405, a window [360, 450] below the 365 floor that
    `test_the_leaps_entry_dte_gene_can_never_decode_below_365` claims to pin, which that test does
    not see. O_ERN (14..23 step 3) can decode 23 → 24, dte_max 31 > 30. This plan does not fix it;
    the strict seed check accepts decode-image values so LEAPS-grid seeds are not refused. Fix it
    separately?
13. **Q13: deploying a style expert.** `rules_export_import` defaults `order_index` to 0 and
    collapses a ruleset on import (memory, unfixed). A style expert depends on rule order: the
    wheel's guards come first, and entry precedence follows member order. That must be fixed before
    any style deploy (out of scope here).
14. **Q14: the soft30 trade floor.** The long-dated floor is applied to a style with any O_LEAP /
    O_PMCC member (the any-member rule). Task 7 records whether `option_car_target_soft30` reads it
    at all. If it does not, the floor is inert for stage 2.

---

## Revisions after the user's answers (2026-09-25)

**Premium style holds stock + options** (user: "options with covered calls / underlying stocks
should be able to purchase it, and backtested on it"). This removes the build's refusals for O_CC,
O_PP and O_WHEEL inside a group and adds two engine tasks. Both are backtest-only; parity with live
is the rule.

- **Task E1: route each entry by its own action type** (in `daily_engine`).
  - Today the engine picks ONE entry path per run from `config["entry_action"]`
    (`_entry_is_option`): either stock entries staged for the equity RM, or self-sizing option
    actions.
  - Route PER ENTRY instead: an entry whose actions are all option actions self-sizes, as in the
    option path today; an equity entry goes through the RM sizing pass; a mix inside ONE rule is
    refused loudly.
  - This mirrors the live TradeManager routing fixed on 2026-09-24 (`f73a33f3`: all-option actions
    self-size, anything else goes to the RM, a mix is refused).
  - Pure-equity and pure-option runs must stay byte-identical: equity goldens, option goldens, and
    the stage-1 discovery names and outputs.
  - Result: O_CC and O_PP (a stock entry plus an option overlay) can be members of a style.
- **Task E2: member-scoped assigned-stock hold.**
  - Today `hold_assigned_stock` is one run-level flag, so in a group every assignment would be held
    and have calls written on it.
  - Decide per assignment from the ENTRY TAG of the option position that was assigned (Tasks 1-2
    add `entry_tag`): hold only when that member declares `hold_assigned_stock` (the wheel), and
    liquidate at the next bar's open otherwise (CSP, JL, RS), as today.
  - The covered-call overlay rules must only write calls on the shares the wheel member holds.
  - Tests: a wheel and a CSP in one style; each assignment follows its own member's policy.
- **The LEAPS/convex style runs on its OWN filtered universe** (user choice): the names whose
  chains reach the LEAPS DTE window, measured by the stage-2 data preflight. No per-member universe
  filter is built.
- **Members without a seed start ON at their authored defaults** (user choice).
- **Fixed now, independently of stage 2:**
  - **(Q12)** The GA rounds integer genes from zero, not from `min`, so the O_LEAP/O_PMCC DTE gene
    can decode below its 365 floor (and O_ERN's `dte_max` can reach 31). Anchor the rounding at
    `min`, or clamp. Check that stage-1 genes are unaffected, or document the effect.
  - **(Q13)** `rules_export_import` resets `order_index` to 0 on import and collapses rule order.
    This blocks any style deploy (memory: order_index IS precedence).
- **Still decided after stage 1:** the mid-term DTE windows, which experts, the price-cap choice
  per style, shared-gene source per seed, convex fitness for the LEAPS/convex style, and whether
  warm start becomes strict.

Execution order: Task 0 → Q12 and Q13 fixes → Tasks 1-2 (`entry_tag`) → E1 → E2 → Tasks 3-21.

### Added after the E1 review (2026-09-25)

- **E1 is done** (`917a1681`): one shared router, `trade_cycle.entry_route`, used by the backtest engine
  AND live TradeManager. A tag on equity entries is supported.
- **E1b: CSP collateral in mixed runs.** Must land before any premium-style result is trusted.
  - **The gap:** the equity RM does not see the collateral a cash-secured put ties up. An open short put
    counts only its premium as used (`MarketExpertInterface.py:~1677`), and the backtest `buying_power`
    is plain cash (`backtest_account.py:~2302`).
  - **Consequence:** in a style run that holds both a CSP member and a stock-buying member (O_CC/O_PP),
    the RM can spend cash the CSP collateral requires. Live Alpaca's Reg-T buying power would refuse that
    trade.
  - **Fix direction:** subtract the reserved option collateral when the RM reads the available budget,
    shared live and backtest, byte-identical when no option collateral is reserved. Do it together with
    E2.
- **Round lot (`lot_size`) now applies.** It is fixed on its own branch, `fix/equity-lot-size-effective`,
  from dev, and fast-forwarded to the stage-1 grid before its O_CC/O_PP jobs. This branch merges dev
  afterwards.
- **E1 follow-ups:**
  - fix the covered-call coexist test so its round-lot assertion no longer passes by coincidence;
  - stub the ATR indicator provider in the real-engine tests (they call FMP with a fake key);
  - refresh the stale `_entry_is_option` docstrings (4 test files);
  - make `test_bull_put_spread_grid` assert through `entry_route`.

### Added after Tasks 3-4 (2026-09-25)

- **E2 changes Task 6's stock-phase exemption.** Task 6 (and Tasks 8-9) assumed the wheel's
  stock-phase rules (`has_assigned_shares` / `has_covered_call` / `covered_call_days_to_expiry`)
  must stay UNSCOPED because the assigned stock lot had no entry tag. Since E2 it has one: the
  delivered stock inherits the assigned put's member tag (`assignment_hold.assigned_stock_entry_data`)
  and a CC/PP overlay inherits the covered position's tag. So Task 6 should AND the member tag into
  the stock-phase rules too (drop the `_STYLE_STOCK_PHASE_FIELDS` exemption and flip the
  "must stay UNSCOPED" assertions in Tasks 6 and 9). That is also what keeps O_CC's
  `cc_guard`/`cc_sell` and O_PP's `pp_guard`/`pp_buy` (they read `has_covered_call` /
  `has_position`) off another member's shares. Update the stale sentence in
  `EntryTagCondition`'s docstring ("the assigned stock lot has no entry tag") with it.
- **Task 3 as built** (`41943d1e`):
  - O_CC / O_PP are admissible (E1) with tags 21 / 22; O_STK stays refused (no option structure).
  - The Q2 refusal (O_WHEEL with O_CSP/O_JL/O_RS) is NOT built: E2 answered it.
  - A generic refusal replaces it: two members whose builders emit the SAME entry rule or
    non-`shared-*` condition ids are refused (today O_WHEEL + O_CSP and O_CC + O_PP), because their
    genes would merge into one. Letting both in needs a re-id of one member's entry (Tasks 5/6).
  - `action_overrides` on an equity-entry member (O_CC/O_PP) are refused: nothing to act on.
  - Review fixes: the loader refuses a duplicate JSON key (naming it and its path) and any
    NaN/Infinity (at parse and in the band checks). A member can be retired without breaking
    append-only: its tag goes in `_STYLE_RETIRED_TAGS` and stays in the table, never reused.
- **Task 4 as built** (`e317a399`): premium = O_BULLPS, O_BEARCS, O_IC, O_JL, O_RS, O_CC, O_WHEEL
  (the stage-2 plan's premium list minus O_CSP), `price_cap_policy: none` (Q3), wheel last (Q4).
  - **O_CSP is excluded for now by a MECHANISM limit, not a strategy decision.** O_WHEEL reuses
    O_CSP's entry ids, so the loader refuses the pair (pinned by
    `test_premium_plus_O_CSP_is_refused_by_the_shared_entry_id_mechanism`). Since E2 a CSP (liquidates
    on assignment) and a wheel (holds and writes calls) are DIFFERENT strategies, so re-identifying
    one member's entry is an open Task 5/6 design question, and whether O_CSP joins premium is a user
    decision after stage 1.
- **Knock-on for later tasks:**
  - Task 5: premium now has an EQUITY entry (O_CC), so "every option action carries its member
    tag" must become "every entry action carries its member tag" (E1 supports a tag on equity
    entries), and `s.entry_action` must not assume the first member is an option.
  - Task 7: for O_STYLE, write `hold_assigned_stock_tags = [tags of members in
    _HOLDS_ASSIGNED_STOCK]` instead of the run-level `hold_assigned_stock` (a run may not name
    both), and expect deploy parity to refuse exporting such a run.
  - Task 9: `test_no_other_premium_member_can_produce_assigned_stock` no longer holds (O_JL/O_RS
    are premium members). Replace it with: their assignments liquidate while the wheel's hold
    (the E2 tests pin the mechanism; Task 9 pins it on the built premium style).

### Tasks 5-6 as built (2026-09-26)

- **Member namespacing inside a style** (user decision 2026-09-26). `_style_member_id(member, id)`:
  `shared-*` stays shared; an id the member's builder already prefixes with its own key (or, for
  O_LEAP / O_CONVEX, an arm's key) is kept; anything else gets `<member>-`. So O_WHEEL's reused
  `o_csp-entry` becomes `o_wheel-o_csp-entry`, O_CC / O_PP's S2 `buy` / `root` / `gate_*` become
  `o_cc-*` / `o_pp-*`, and every exit id is prefixed (`opt_tp` -> `o_lc-opt_tp`). It depends on
  the member only, never on its siblings, and only a style build applies it: all 37 registered
  keys build byte-identically (3 market profiles x 2 experts = 222 digests: 216 real builds plus
  6 identical O_CAL phase-gate refusals), and every option single's entry ids (so its stage-1
  entry genes) are the style's verbatim.
- **Loader:** the Task 3 entry-id clash refusal is relaxed to what still collides after
  namespacing -- two members owning one id prefix -- checked over entry and exit rule ids and
  every condition node id (group ids included). O_WHEEL + O_CSP and O_CC + O_PP load. The
  builder also refuses a `shared-*` condition two members author differently.
- **Premium candidate:** O_CSP is back, next to O_WHEEL (wheel still last). Still `candidate`.
- **Entry side (Task 5):** no `normalize_trade_rules` pass over the members' rules (they are
  already canonical; the builder sets both toggle spellings itself). Option entries carry
  `option_entry_tag`, the O_CC / O_PP equity `buy` carries `entry_tag`. A mixed rule is refused
  through `trade_cycle.entry_route`, an opening `sell` by name. `entry_action` is a copy of the
  first OPTION entry in member order (none if the style has no option entry, like O_CC alone).
  `action_overrides` are also checked merged with the authored band (centre inside the searched
  range; no inert range keys on a non-searched DTE).
- **Exit side (Task 6):** every exit rule is scoped, the wheel's stock-phase rules and the
  CC/PP guard+overlay pairs included (E2). The tag leaf is appended last to the root AND and
  named `<rule>-tag` (unique per rule, not `<member>-tag`); a non-AND root is refused.
  `exit_overrides` move the camelCase mirror too and are refused on an unknown id, a flag leaf,
  a non-searched leaf, or a merged band out of order. `_style_translate_key(gene, id_map,
  member, style_space=None)` takes the member for its refusal messages (see the review fixes
  below for `screener` and `style_space`).
- **Strategy genes** (`collect_param_space`, no expert/schedule genes; profile none /
  `ohlcv-v1,ta-structure-v1`): short 116 / 191, mid 140 / 230, leaps_convex 99 / 174,
  premium (8 members) 216 / 336.

**Review fixes to Tasks 5-6 (2026-09-26)**

- **I1, shared guard.** `rule_builders.rule_triggers_from_tree` refused "leaves but no trigger"
  (ALWAYS TRUE), but a style exit's `entry_tag` leaf always converts, so a rule whose AUTHORED
  leaves all dropped became `{entry_tag == N}`: always true for that member, while its single
  twin was refused. The guard now judges the non-`entry_tag` leaves and triggers; untagged rules
  compare exactly the lists they always did (goldens, stage-1 pin, all 222 build digests
  unchanged). The builder test asserts every leaf converts (`len(triggers) == len(leaves)`).
- **I2, screener genes are refused.** `_style_translate_key` no longer passes `screener:*`
  through: screener genes are run-level, so one member's gate (a stage-1 O_CSP seed's $100
  `price_max`) would become the whole style's and pre-empt Q3. Task 13's list of refused keys
  stands. Identity stays for `optsel:*`, `model:*`, `schedule:*` and `cond:shared-*`.
- **I3, per-member DTE invariants.** `action_overrides` are refused by name on O_ERN (expiry
  must clear the earnings entry window; `_assert_option_expiry_clears_event_window` checks the
  authored table only), O_LEAP and O_PMCC (a LEAPS opens at >= 365 DTE; their own 90-240 DTE
  exit would close a shorter one at once) and O_CONVEX (`_STYLE_DTE_INVARIANT_MEMBERS`). None
  of those invariants can be re-run on a merged action today.
- **M1/M2.** Every builder refusal is prefixed `style '<name>' member <KEY>:` (the exception
  type is kept). A mixed option+equity entry is refused naming member and rule. The `sell`
  refusal is checked BEFORE tagging, so a `[buy, sell]` rule is refused (it used to be shadowed).
- **M3, Task 13 must check against the style space.** `_style_translate_key` now refuses a
  `cond:` field outside `value / confirmation_bars / enabled / mode`, and takes an optional
  `style_space` (the built style's `collect_param_space`): a translated key that is not a gene
  of it is refused. Task 13's translator MUST pass it; a well-formed key naming no gene
  (`cond:tp:enabled` -> `cond:o_lc-tp:enabled`, a leaf with no toggle) is otherwise accepted.
- **M4, for Task 8.** Same-symbol isolation between members rests on two engine facts Task 8
  must pin: the one-position-per-expert-per-symbol entry gate (`daily_engine`
  `_has_open_or_waiting_position`, ~line 1083; live: `TradeManager` dup-position gate), which
  means only ONE member can hold a given symbol at a time in a style expert; and the exit pass
  reading the OLDEST transaction's entry order (`_oldest_entry_order`, ~line 1337), which is
  whose `entry_tag` every member exit compares against.
- **M6.** `deploy_parity` will state the first OPTION member's action as the run's
  `entry_action`. That is informational only: the style's entries are its `entry_rules`, each
  routed per entry (E1).
- **OPEN (Q4, taken to the user): premium member order.** First match wins, and O_CSP now
  precedes O_WHEEL with IDENTICAL entry gates (the wheel's entry is O_CSP's), so the wheel only
  trades once the GA moves its genes away from O_CSP's or toggles O_CSP off. The order is left
  as is (O_CSP, O_WHEEL last) pending the user's decision.

### Task 8 review fixes (2026-09-26)

- **I1, one entry-order read and one dup gate for both runtimes** (`3695e14a`).
  - `ba2_common.core.entry_order.oldest_entry_order(transactions)`: the entry order of the oldest
    OPENED transaction (UTC `open_date`, else `created_at`, then id). WAITING transactions are
    not positions and are never picked.
  - `entry_order_for_transaction(id)`: the first-written ROOT order (no `depends_on_order`, no
    `parent_order_id`), UTC `created_at` then id, any status. That is the row every entry path
    stamps `entry_tag` and its entry facts on (a combo's parent, never a leg child).
  - `trade_cycle.open_or_waiting_position(expert, symbol)`: the one-position gate.
  - The backtest engine, `BacktestAccount._entry_order_for_transaction`, live `TradeManager`
    (open-positions pass, `resolve_entry_order`, the enter-market SAFETY CHECK) and
    `AlpacaAccount._position_entry_tag` all call them. An OPENED position with no entry row
    raises `EntryOrderUnresolved` (live: ERROR + skip the symbol; backtest: the run ends).
  - Goldens and the stage-1 pin are unchanged. Live differs from the old code only where the
    old code was wrong: an older WAITING transaction beside the open one, a partly filled then
    cancelled entry, a same-side combo leg tied on `created_at`, mixed naive/aware timestamps
    (old: TypeError), and a position with no entry row (old: evaluated against None).
- **M1.** A style build refusal now carries `style '<name>' member <KEY>:` in its MESSAGE
  (and still as a PEP-678 note), rebuilt as the same type where the type accepts a message,
  else raised as `StyleBuildError(ValueError)` chained from the original (pydantic
  `ValidationError`).
- **M4.** `_STYLE_ASSIGNS_STOCK` is derived from the legs each member's opening actions actually
  build (captured at `_submit_option_order`), not from source text. The set's comment now says
  that short calls (O_CBS, O_PMCC, O_JL's call) deliver SHORT stock.
- **M2/M3** are Task 17 notes (above): the identity digest uses `spec_sha256`, never
  `spec_path`, and the driver refuses `status: candidate` styles unless acknowledged.
