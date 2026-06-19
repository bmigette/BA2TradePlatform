# Backtest Interface Rework — Backend Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the backend for the expert-centric backtest UI: expert-listing + settings-definition endpoints, RM-as-expert-settings optimization (retire the Strategy `rm_*` columns and `rm:*` namespace), JSON v1.1 rule import/export (ruleset ↔ condition tree), expert/universe backtest+optimize payloads, screener-cache universe resolution, and expert/optimization-job list filters. The legacy ML path keeps working.

**Architecture:** All changes are in `BA2TestPlatform/backend`, reusing shared seams in `ba2_common` (`rules_export_import`, models) and `ba2_experts` (the expert classes + `get_settings_definitions()`). RM/sizing is optimized through the existing `expert_cfg`→`model:*` path; the `rm:*` namespace and `Strategy.rm_*` columns are removed. New code is small, focused services + thin API routers.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLAlchemy (sqlite), pytest. Test interpreter: `~/ba2-venvs/test/bin/python` (the editable chain venv). Run tests from `BA2TestPlatform/backend`.

**Spec:** `docs/superpowers/specs/2026-06-15-backtest-interface-rework-design.md`

**Conventions:**
- Run all tests as: `cd BA2TestPlatform/backend && ~/ba2-venvs/test/bin/python -m pytest <path> -q -p no:cacheprovider`
- Fail-early validation (no silent defaults) per `backend/CLAUDE.md`.
- Commit after each task. Branch is `dev`; commit, do not push until the plan is done (or per user).

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `backend/db_migrate/022_drop_strategy_rm_columns.py` | Drop the retired `Strategy.rm_*` columns | Create |
| `backend/app/models/strategy.py` | Strategy ORM — remove `rm_*` columns | Modify |
| `backend/app/services/strategy_param_space.py` | Remove `rm:*` namespace + `rm` decode | Modify |
| `backend/app/services/strategy_optimization_handler.py` | Stop reading `rm_params`; drop `rm` mapping in trial config | Modify |
| `backend/app/services/experts_catalog.py` | List experts + read `get_settings_definitions()` | Create |
| `backend/app/api/experts.py` | `GET /api/experts`, `GET /api/experts/{class}/settings-definitions` | Create |
| `backend/app/services/rules_tree_json.py` | ruleset-JSON ↔ condition-tree (v1.1, optimize ranges) | Create |
| `backend/app/api/rules.py` | `export-rules` / `import-rules` endpoints | Create |
| `backend/app/api/backtests.py` | create accepts `engine`/`expert`/`universe`; list filters | Modify |
| `backend/app/api/strategies.py` | optimize payload: `expert`/`universe`/`expert_params`; drop `rm_params` | Modify |
| `backend/app/services/backtest/universe_resolver.py` | resolve a screener universe from the offline cache | Create |
| `backend/app/main.py` | register the two new routers | Modify |
| `backend/tests/...` | tests per task | Create |

---

## Task 1: Drop the retired `Strategy.rm_*` columns

The 5 RM params each have 5 columns (`rm_<p>`, `rm_<p>_optimize`, `rm_<p>_min/_max/_step`) = 25 columns. RM is now optimized as expert settings, so they're dead. sqlite can't `DROP COLUMN` reliably on old versions, so rebuild the table by copying the kept columns. The ML path never used these columns, so it is unaffected.

**Files:**
- Create: `backend/db_migrate/022_drop_strategy_rm_columns.py`
- Modify: `backend/app/models/strategy.py`
- Test: `backend/tests/test_migration_022.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_migration_022.py
"""Migration 022 drops Strategy.rm_* columns; kept columns + rows survive."""
import importlib.util
import sqlite3
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "db_migrate" / "022_drop_strategy_rm_columns.py"

_RM_COLS = [
    "rm_risk_per_trade_pct", "rm_per_instrument_cap_pct", "rm_min_stop_pct",
    "rm_atr_stop_mult", "rm_max_concurrent_positions",
]


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig022", MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_legacy_db(path):
    con = sqlite3.connect(path)
    # minimal legacy strategies table with a couple of rm_* columns + kept columns
    con.execute(
        "CREATE TABLE strategies (id INTEGER PRIMARY KEY, name TEXT, "
        "initial_tp_percent REAL, initial_sl_percent REAL, "
        "buy_entry_conditions TEXT, exit_conditions TEXT, "
        "rm_risk_per_trade_pct REAL, rm_risk_per_trade_pct_optimize INTEGER, "
        "rm_max_concurrent_positions INTEGER)"
    )
    con.execute(
        "INSERT INTO strategies (id, name, initial_tp_percent, rm_risk_per_trade_pct) "
        "VALUES (1, 'keep-me', 5.0, 1.0)"
    )
    con.commit()
    con.close()


def test_022_drops_rm_columns_keeps_rows(tmp_path):
    db = tmp_path / "t.db"
    _make_legacy_db(db)
    mod = _load_migration()
    con = sqlite3.connect(db)
    mod.upgrade(con)  # migration applies against an open sqlite3 connection
    cols = [r[1] for r in con.execute("PRAGMA table_info(strategies)")]
    assert not any(c.startswith("rm_") for c in cols), f"rm_* still present: {cols}"
    row = con.execute("SELECT name, initial_tp_percent FROM strategies WHERE id=1").fetchone()
    assert row == ("keep-me", 5.0)
    con.close()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_migration_022.py -q -p no:cacheprovider`
Expected: FAIL — `db_migrate/022_drop_strategy_rm_columns.py` does not exist (import error).

- [ ] **Step 3: Inspect the existing migration runner contract**

Run: `sed -n '1,60p' scripts/migrate_db.py` and open one recent migration (e.g. `db_migrate/021_add_backtest_expert_optimization.py`) to copy its exact `upgrade(...)`/`MIGRATION_NAME` shape. Mirror that signature in 022 (the test calls `mod.upgrade(con)`; if the runner passes a cursor instead, adapt the test + migration to match the real contract — keep them identical).

- [ ] **Step 4: Write the migration**

```python
# backend/db_migrate/022_drop_strategy_rm_columns.py
"""Drop the retired Strategy.rm_* columns (RM is now optimized as expert settings).

sqlite-safe table rebuild: create a new table with only the kept columns, copy data,
swap. Idempotent: if no rm_* columns exist, do nothing.
"""
MIGRATION_NAME = "022_drop_strategy_rm_columns"


def upgrade(conn):
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(strategies)")]
    rm_cols = [c for c in cols if c.startswith("rm_")]
    if not rm_cols:
        return  # already migrated / fresh schema
    keep = [c for c in cols if not c.startswith("rm_")]
    keep_csv = ", ".join(f'"{c}"' for c in keep)
    cur.execute("ALTER TABLE strategies RENAME TO strategies__old")
    # Recreate from the old table's definition minus rm_* by selecting kept cols into a new table.
    cur.execute(f"CREATE TABLE strategies AS SELECT {keep_csv} FROM strategies__old")
    cur.execute("DROP TABLE strategies__old")
    conn.commit()
```

Note: `CREATE TABLE AS SELECT` loses PK/constraints; the app uses `Base.metadata.create_all()` on startup which is a no-op for existing tables. If the runner/tests require a precise schema, instead build the `CREATE TABLE` DDL from the ORM. For the test platform's sqlite this CTAS approach is acceptable; confirm `id` remains usable (it does — values are copied).

- [ ] **Step 5: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_migration_022.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Remove `rm_*` from the ORM model**

In `backend/app/models/strategy.py`, delete the 25 `rm_*` column definitions and any `rm_*` keys in `to_dict()`. Keep `name`, `buy_entry_conditions`, `sell_entry_conditions`, `exit_conditions`, `initial_tp_*`, `initial_sl_*`, timestamps.

- [ ] **Step 7: Apply the migration to the dev DB + verify the model imports**

Run: `~/ba2-venvs/test/bin/python scripts/migrate_db.py`
Then: `~/ba2-venvs/test/bin/python -c "from app.models.strategy import Strategy; print([c for c in Strategy.__table__.columns.keys() if c.startswith('rm_')])"`
Expected: `[]` and migration reports 022 applied.

- [ ] **Step 8: Commit**

```bash
git add backend/db_migrate/022_drop_strategy_rm_columns.py backend/app/models/strategy.py backend/tests/test_migration_022.py
git commit -m "feat(model): drop Strategy.rm_* columns (RM now optimized as expert settings)"
```

---

## Task 2: Retire the `rm:*` param-space namespace

RM sizing is now optimized via the existing `expert_cfg`→`model:*` path (keyed by the real setting names, e.g. `risk_per_trade_pct`). Remove `_collect_rm`, `CLASSIC_RM_PARAMS`, `_rm_defaults_from_strategy`, the `rm_cfg` parameter, and the `rm:`/`rm` handling in `decode_params`.

**Files:**
- Modify: `backend/app/services/strategy_param_space.py`
- Modify: `backend/app/services/strategy_optimization_handler.py`
- Test: `backend/tests/test_param_space_no_rm.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_param_space_no_rm.py
"""rm:* namespace is gone; expert settings (incl RM sizing) optimize via model:*."""
import types
from app.services.strategy_param_space import collect_param_space, decode_params


def _strategy():
    return types.SimpleNamespace(
        initial_tp_percent=5.0, initial_tp_optimize=False,
        initial_sl_percent=2.0, initial_sl_optimize=False,
        buy_entry_conditions=None, sell_entry_conditions=None,
        entry_conditions=None, exit_conditions=None,
    )


def test_rm_sizing_optimizes_through_model_namespace():
    expert_cfg = {"risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 3.0,
                                         "step": 0.5, "type": "float"}}
    space = collect_param_space(_strategy(), expert_cfg=expert_cfg)
    assert "model:risk_per_trade_pct" in space
    assert not any(k.startswith("rm:") for k in space)


def test_decode_has_no_rm_key_and_routes_to_expert_overrides():
    decoded = decode_params(_strategy(), {"model:risk_per_trade_pct": 1.5})
    assert decoded["expert_overrides"] == {"risk_per_trade_pct": 1.5}
    assert "rm" not in decoded
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_param_space_no_rm.py -q -p no:cacheprovider`
Expected: FAIL — `collect_param_space` still accepts/needs `rm_cfg` paths and `decode_params` returns a `rm` key.

- [ ] **Step 3: Edit `strategy_param_space.py`**

- Delete `CLASSIC_RM_PARAMS`, `_collect_rm`, `_rm_defaults_from_strategy`.
- Change `collect_param_space(strategy, expert_cfg=None, bypass=False)` (drop `rm_cfg`); remove the `space.update(_collect_rm(rm_cfg))` line.
- In `decode_params`, delete the `rm: Dict = {}`, the `elif key.startswith("rm:")` branch, the `rm_full = _rm_defaults_from_strategy(...)` block, and remove `"rm": rm_full` from the returned dict. Keep `expert_overrides` (which now carries `model:*` incl RM settings).
- Update the module docstring namespacing block (drop `rm:<p>`).

- [ ] **Step 4: Update the optimizer handler to stop passing `rm_cfg`**

In `backend/app/services/strategy_optimization_handler.py`:
- Remove `rm_cfg = ga.get("rm_params")` and pass only `expert_cfg` to `collect_param_space(strategy, expert_cfg=expert_cfg, bypass=bypass_expert)`.
- In `_build_daily_trial_config`, delete the block that maps `decoded["rm"]` via `_RM_SETTING_NAME` (and the `_RM_SETTING_NAME` dict). The expert overrides (`decoded["expert_overrides"]`, real setting names) already merge into each expert spec's settings — that now carries RM sizing. Keep the `tp`/`sl` forwarding.

- [ ] **Step 5: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_param_space_no_rm.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Regression — optimizer + backtest suites still green**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_strategy_optimization_handler.py tests/backtest/ -q -p no:cacheprovider`
Expected: PASS (fix any test that referenced `rm_params`/`rm:*`/`decoded["rm"]` to the new shape — e.g. an ML/daily trial test that asserted an `rm` key).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/strategy_param_space.py backend/app/services/strategy_optimization_handler.py backend/tests/test_param_space_no_rm.py
git commit -m "feat(optimizer): retire rm:* namespace; RM sizing optimizes via expert model:* settings"
```

---

## Task 3: Expert catalog + endpoints

The form needs the list of experts and each expert's settings definitions.

**Files:**
- Create: `backend/app/services/experts_catalog.py`
- Create: `backend/app/api/experts.py`
- Modify: `backend/app/main.py` (register router)
- Test: `backend/tests/test_experts_api.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_experts_api.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_list_experts_includes_known_classes():
    r = client.get("/api/experts")
    assert r.status_code == 200
    classes = {e["class"] for e in r.json()["experts"]}
    assert {"FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FactorRanker"} <= classes
    fr = next(e for e in r.json()["experts"] if e["class"] == "FactorRanker")
    assert fr["bypasses_classic_rm"] is True


def test_settings_definitions_shape():
    r = client.get("/api/experts/FMPRating/settings-definitions")
    assert r.status_code == 200
    defs = r.json()["definitions"]
    assert "sizing_mode" in defs and "risk_per_trade_pct" in defs
    assert defs["risk_per_trade_pct"]["type"] == "float"


def test_unknown_expert_404():
    assert client.get("/api/experts/NotAnExpert/settings-definitions").status_code == 404
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_experts_api.py -q -p no:cacheprovider`
Expected: FAIL — `/api/experts` route does not exist (404).

- [ ] **Step 3: Write the catalog service**

```python
# backend/app/services/experts_catalog.py
"""Expert catalog for the UI: the supported experts + their settings definitions.

Source of truth for the class->module map is daily_backtest_handler._SUPPORTED_EXPERTS.
Experts are imported + instantiated transiently (no DB) to read get_settings_definitions().
"""
import importlib
from typing import Any, Dict, List

from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS


def _load_class(class_name: str):
    mod_path = _SUPPORTED_EXPERTS.get(class_name)
    if not mod_path:
        return None
    return getattr(importlib.import_module(mod_path), class_name)


def list_experts() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name in sorted(_SUPPORTED_EXPERTS):
        cls = _load_class(name)
        if cls is None:
            continue
        out.append({
            "class": name,
            "label": name,
            "bypasses_classic_rm": bool(getattr(cls, "bypasses_classic_rm", False)),
            "uses_risk_manager": bool(getattr(cls, "uses_risk_manager", True)),
        })
    return out


def settings_definitions(class_name: str) -> Dict[str, Any]:
    """Return the expert's get_settings_definitions() map, or raise KeyError if unknown."""
    cls = _load_class(class_name)
    if cls is None:
        raise KeyError(class_name)
    # get_settings_definitions is an instance method on MarketExpertInterface; the builtin
    # settings need no DB. Instantiate with no args if the ctor allows it, else read the
    # classmethod/staticmethod form. Confirm the call form against MarketExpertInterface.
    inst = cls.__new__(cls)
    return inst.get_settings_definitions()
```

Note: confirm `get_settings_definitions()` works on a `__new__`-constructed instance (it reads class-level builtin defs + the subclass's own). If it needs `__init__` state, adjust to call the builtin-defs assembler directly. Verify with: `~/ba2-venvs/test/bin/python -c "from app.services.experts_catalog import settings_definitions; print(list(settings_definitions('FMPRating'))[:5])"` from `backend/`.

- [ ] **Step 4: Write the router**

```python
# backend/app/api/experts.py
from fastapi import APIRouter, HTTPException

from app.services.experts_catalog import list_experts, settings_definitions

router = APIRouter(prefix="/api/experts", tags=["experts"])


@router.get("")
def get_experts():
    return {"experts": list_experts()}


@router.get("/{class_name}/settings-definitions")
def get_settings_definitions(class_name: str):
    try:
        return {"class": class_name, "definitions": settings_definitions(class_name)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown expert {class_name!r}")
```

- [ ] **Step 5: Register the router**

In `backend/app/main.py`, where other routers are included, add:
```python
from app.api import experts as experts_api
app.include_router(experts_api.router)
```
(Match the existing include style in main.py.)

- [ ] **Step 6: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_experts_api.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/experts_catalog.py backend/app/api/experts.py backend/app/main.py backend/tests/test_experts_api.py
git commit -m "feat(api): expert catalog + settings-definitions endpoints"
```

---

## Task 4: Ruleset JSON ↔ condition-tree mapping (v1.1)

The import (inverse of `seed_ruleset_from_tree`) turns a ruleset JSON into a condition tree; the export turns a strategy tree into v1.1 JSON with optimize ranges. Reuse `_FIELD_EVENT` from `default_rulesets` for the field↔event mapping.

**Files:**
- Create: `backend/app/services/rules_tree_json.py`
- Test: `backend/tests/test_rules_tree_json.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_rules_tree_json.py
from app.services.rules_tree_json import ruleset_json_to_tree, tree_to_ruleset_json

# v1.1 ruleset JSON (one EventAction: bullish AND confidence>0.7 with an optimize range)
RULESET_JSON = {
    "export_version": "1.1", "export_type": "ruleset",
    "ruleset": {"name": "enter", "type": "trading_recommendation_rule",
                "subtype": "enter_market", "rules": [{
        "triggers": {
            "bullish": {"event_type": "bullish", "enabled": True},
            "gate_0": {"event_type": "confidence", "operator": ">", "value": 0.7,
                       "enabled": True, "optimize": {"min": 0.5, "max": 0.9, "step": 0.05}},
        },
        "actions": {"buy": {"action_type": "buy"}},
        "continue_processing": False, "order_index": 0}]}}


def test_import_builds_tree_with_value_and_optimize():
    tree = ruleset_json_to_tree(RULESET_JSON, which="enter")
    # OR of rules -> AND of triggers; find the confidence leaf
    leaves = _all_leaves(tree)
    conf = next(l for l in leaves if l.get("field") == "confidence")
    assert conf["op"] == ">" and conf["value"] == 0.7
    assert conf["optimize"] is True
    assert (conf["value_min"], conf["value_max"], conf["value_step"]) == (0.5, 0.9, 0.05)
    # flag trigger preserved as a flag node
    assert any(l.get("field") == "bullish" for l in leaves)


def test_export_roundtrips_back_to_triggers():
    tree = ruleset_json_to_tree(RULESET_JSON, which="enter")
    out = tree_to_ruleset_json(tree, which="enter", name="enter")
    trig = out["ruleset"]["rules"][0]["triggers"]
    gate = next(v for v in trig.values() if v["event_type"] == "confidence")
    assert gate["value"] == 0.7 and gate["optimize"] == {"min": 0.5, "max": 0.9, "step": 0.05}
    assert out["export_version"] == "1.1"


def test_unknown_event_type_is_reported_not_silently_dropped():
    bad = {"export_version": "1.1", "export_type": "ruleset", "ruleset": {"rules": [{
        "triggers": {"x": {"event_type": "totally_unknown", "value": 1}},
        "actions": {"buy": {"action_type": "buy"}}}]}}
    import pytest
    with pytest.raises(ValueError, match="unknown event_type"):
        ruleset_json_to_tree(bad, which="enter")


def _all_leaves(node, acc=None):
    acc = acc if acc is not None else []
    if isinstance(node, dict):
        kids = node.get("conditions")
        if kids:
            for k in kids:
                _all_leaves(k, acc)
        elif node.get("field"):
            acc.append(node)
    return acc
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_rules_tree_json.py -q -p no:cacheprovider`
Expected: FAIL — module/functions do not exist.

- [ ] **Step 3: Write the mapping module**

```python
# backend/app/services/rules_tree_json.py
"""Ruleset JSON (rules_export_import shape, v1.1) <-> Strategy condition tree.

Import = inverse of default_rulesets.seed_ruleset_from_tree: ruleset rules (OR) whose
triggers (AND) become condition leaves; v1.1 carries per-operand optimize {min,max,step}
and an enabled flag. Export = tree -> v1.1 JSON. Flag triggers (bullish/has_no_position)
become flag leaves (no operator/value) and are re-added on seed-back.
"""
import uuid
from typing import Any, Dict, List

from app.services.backtest.default_rulesets import _FIELD_EVENT

# event_type value -> strategy field (reverse of _FIELD_EVENT, by enum .value)
_EVENT_FIELD = {et.value: field for field, et in _FIELD_EVENT.items()}
# flag event_types kept as flag leaves
_FLAG_EVENTS = {"bullish", "bearish", "has_no_position"}


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _trigger_to_leaf(key: str, trig: Dict[str, Any]) -> Dict[str, Any]:
    et = trig.get("event_type")
    if et in _FLAG_EVENTS:
        return {"id": _new_id(), "field": et, "is_flag": True,
                "enabled": trig.get("enabled", True)}
    field = _EVENT_FIELD.get(et)
    if field is None:
        raise ValueError(f"unknown event_type {et!r} (no field mapping)")
    leaf: Dict[str, Any] = {
        "id": _new_id(), "field": field,
        "op": trig.get("operator", ">"), "value": trig.get("value"),
        "enabled": trig.get("enabled", True),
    }
    opt = trig.get("optimize")
    if isinstance(opt, dict):
        leaf.update({"optimize": True, "value_min": opt.get("min"),
                     "value_max": opt.get("max"), "value_step": opt.get("step")})
    else:
        leaf["optimize"] = False
    return leaf


def ruleset_json_to_tree(payload: Dict[str, Any], which: str) -> Dict[str, Any]:
    """Return a ConditionGroup (OR of rules; each rule = AND of trigger leaves)."""
    ruleset = payload.get("ruleset") or {}
    rules: List[Dict[str, Any]] = ruleset.get("rules") or []
    or_children: List[Dict[str, Any]] = []
    for rule in rules:
        and_children = [_trigger_to_leaf(k, t) for k, t in (rule.get("triggers") or {}).items()]
        or_children.append({"id": _new_id(), "operator": "AND", "conditions": and_children})
    return {"id": _new_id(), "operator": "OR", "conditions": or_children}


def _leaf_to_trigger(idx: int, leaf: Dict[str, Any]) -> Dict[str, Any]:
    if leaf.get("is_flag") or leaf.get("field") in _FLAG_EVENTS:
        return {"event_type": leaf["field"], "enabled": leaf.get("enabled", True)}
    et = _FIELD_EVENT[leaf["field"]].value  # KeyError if not mappable (caller guards UI fields)
    trig: Dict[str, Any] = {"event_type": et, "operator": leaf.get("op", ">"),
                            "value": leaf.get("value"), "enabled": leaf.get("enabled", True)}
    if leaf.get("optimize"):
        trig["optimize"] = {"min": leaf.get("value_min"), "max": leaf.get("value_max"),
                            "step": leaf.get("value_step")}
    return trig


def _iter_rules(tree: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """Flatten a (possibly nested) tree into a list of AND-groups (each a list of leaves)."""
    if not isinstance(tree, dict):
        return []
    op = (tree.get("operator") or "AND").upper()
    kids = tree.get("conditions") or []
    if not kids and tree.get("field"):
        return [[tree]]
    if op == "OR":
        groups: List[List[Dict[str, Any]]] = []
        for k in kids:
            groups.extend(_iter_rules(k))
        return groups
    # AND: collect leaf children into one group (nested AND/OR flattened best-effort)
    leaves = [k for k in kids if isinstance(k, dict) and k.get("field")]
    return [leaves] if leaves else []


def tree_to_ruleset_json(tree: Dict[str, Any], which: str, name: str) -> Dict[str, Any]:
    subtype = "enter_market" if which == "enter" else "exit_market"
    rules = []
    for order, group in enumerate(_iter_rules(tree)):
        triggers = {f"t{i}": _leaf_to_trigger(i, leaf) for i, leaf in enumerate(group)}
        rules.append({"name": f"{name}-{order}", "triggers": triggers,
                      "actions": {"buy": {"action_type": "buy"}},
                      "continue_processing": False, "order_index": order})
    return {"export_version": "1.1", "export_type": "ruleset",
            "ruleset": {"name": name, "type": "trading_recommendation_rule",
                        "subtype": subtype, "rules": rules}}
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_rules_tree_json.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/rules_tree_json.py backend/tests/test_rules_tree_json.py
git commit -m "feat(rules): ruleset-JSON <-> condition-tree mapping (v1.1 optimize ranges)"
```

---

## Task 5: Rules import/export endpoints

**Files:**
- Create: `backend/app/api/rules.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_rules_api.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_rules_api.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

RULESET_JSON = {"export_version": "1.1", "export_type": "ruleset", "ruleset": {"rules": [{
    "triggers": {"gate": {"event_type": "confidence", "operator": ">", "value": 0.7}},
    "actions": {"buy": {"action_type": "buy"}}}]}}


def test_import_rules_returns_tree():
    r = client.post("/api/strategies/import-rules", json={"json": RULESET_JSON, "which": "enter"})
    assert r.status_code == 200
    tree = r.json()["tree"]
    assert tree["operator"] == "OR"


def test_import_rules_rejects_bad_event_type():
    bad = {"export_version": "1.1", "export_type": "ruleset", "ruleset": {"rules": [{
        "triggers": {"x": {"event_type": "nope", "value": 1}}, "actions": {}}]}}
    r = client.post("/api/strategies/import-rules", json={"json": bad, "which": "enter"})
    assert r.status_code == 422
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_rules_api.py -q -p no:cacheprovider`
Expected: FAIL — route missing.

- [ ] **Step 3: Write the router**

```python
# backend/app/api/rules.py
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.database import SessionLocal
from app.models.strategy import Strategy
from app.services.rules_tree_json import ruleset_json_to_tree, tree_to_ruleset_json

router = APIRouter(prefix="/api/strategies", tags=["rules"])


class ImportRulesRequest(BaseModel):
    json: Dict[str, Any]
    which: str  # "enter" | "exit"


@router.post("/import-rules")
def import_rules(req: ImportRulesRequest):
    if req.which not in ("enter", "exit"):
        raise HTTPException(422, "which must be 'enter' or 'exit'")
    try:
        tree = ruleset_json_to_tree(req.json, which=req.which)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"tree": tree}


@router.get("/{strategy_id}/export-rules")
def export_rules(strategy_id: int, which: str = "enter"):
    if which not in ("enter", "exit"):
        raise HTTPException(422, "which must be 'enter' or 'exit'")
    db = SessionLocal()
    try:
        s = db.query(Strategy).filter(Strategy.id == strategy_id).first()
        if not s:
            raise HTTPException(404, f"strategy {strategy_id} not found")
        tree = s.buy_entry_conditions if which == "enter" else {"operator": "OR", "conditions": [
            {"operator": "AND", "conditions": (s.exit_conditions or [])}]}
        return tree_to_ruleset_json(tree or {"operator": "OR", "conditions": []},
                                    which=which, name=f"{s.name}-{which}")
    finally:
        db.close()
```

Note: exit_conditions are a list of rules, not a tree — confirm the exit shape and adjust `export_rules` to wrap them the way `tree_to_ruleset_json` expects (the test in Task 4 covers the enter tree; add an exit-shape test here if the exit serialization differs).

- [ ] **Step 4: Register the router in `main.py`**

```python
from app.api import rules as rules_api
app.include_router(rules_api.router)
```

- [ ] **Step 5: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_rules_api.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/rules.py backend/app/main.py backend/tests/test_rules_api.py
git commit -m "feat(api): rule import/export endpoints (JSON v1.1 <-> condition tree)"
```

---

## Task 6: Backtest create accepts expert/universe; list filters

**Files:**
- Modify: `backend/app/api/backtests.py`
- Test: `backend/tests/test_backtests_api_filters.py`

- [ ] **Step 1: Read the current create + list handlers**

Run: `sed -n '1,120p' backend/app/api/backtests.py` — note the `BacktestCreate` model + `list_backtests` query so the additions match the existing style.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_backtests_api_filters.py
from fastapi.testclient import TestClient
from app.main import app
from app.models.database import Base, engine, SessionLocal
from app.models.backtest import Backtest

client = TestClient(app)


def _seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for name, expert, opt in [("a", "FMPRating", None), ("b", "FMPRating", 5),
                                  ("c", "FMPEarningsDrift", 5)]:
            db.add(Backtest(name=name, expert_name=expert, optimization_id=opt,
                            status="completed", initial_capital=1000.0,
                            commission=1.0, slippage=0.0))
        db.commit()
    finally:
        db.close()


def test_list_filters_by_expert_and_optimization():
    _seed()
    assert all(b["expert_name"] == "FMPRating"
               for b in client.get("/api/backtests?expert=FMPRating").json()["backtests"])
    ids = client.get("/api/backtests?optimization_id=5").json()["backtests"]
    assert all(b["optimization_id"] == 5 for b in ids)
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_backtests_api_filters.py -q -p no:cacheprovider`
Expected: FAIL — the list endpoint ignores `expert`/`optimization_id`.

- [ ] **Step 4: Add list filters**

In `list_backtests` add query params `expert: str | None = None`, `optimization_id: int | None = None`, `saved: bool | None = None`, and apply `.filter(Backtest.expert_name == expert)` / `.filter(Backtest.optimization_id == optimization_id)` / `.filter(Backtest.is_saved == saved)` when present. Ensure the response items include `expert_name` + `optimization_id`.

- [ ] **Step 5: Extend `BacktestCreate` for engine/expert/universe**

Add optional fields to `BacktestCreate`: `engine: str = "ml"`, `expert: dict | None = None` (`{class, settings}`), `universe: dict | None = None` (`{mode, symbols, screener_settings}`). When `engine == "daily_expert"`, build the daily payload (expert spec + `enabled_instruments` from `universe`) and route to `handle_daily_backtest`; otherwise keep the existing ML path. Validate fail-early: daily requires `expert.class` in `_SUPPORTED_EXPERTS` and a non-empty universe.

- [ ] **Step 6: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_backtests_api_filters.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/backtests.py backend/tests/test_backtests_api_filters.py
git commit -m "feat(api): backtest create accepts engine/expert/universe; list filters by expert+optimization"
```

---

## Task 7: Optimize payload — expert/universe/expert_params; drop rm_params

**Files:**
- Modify: `backend/app/api/strategies.py`
- Test: `backend/tests/test_optimize_payload.py`

- [ ] **Step 1: Read the current optimize endpoint + OptimizeRequest**

Run: `sed -n '380,433p' backend/app/api/strategies.py`.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_optimize_payload.py
"""The optimize endpoint stores expert/universe + expert_params and no rm_params."""
from fastapi.testclient import TestClient
from app.main import app
from app.models.database import Base, engine, SessionLocal
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization

client = TestClient(app)


def _strategy_id():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        s = Strategy(name="opt", initial_tp_percent=5.0, initial_sl_percent=2.0)
        db.add(s); db.commit(); db.refresh(s); return s.id
    finally:
        db.close()


def test_optimize_stores_expert_params(monkeypatch):
    import app.api.strategies as S
    monkeypatch.setattr(S, "enqueue_task", lambda *a, **k: "task-1", raising=False)
    sid = _strategy_id()
    body = {"name": "o", "fitness_metric": "sharpe", "optimization_type": "genetic",
            "optimization_config": {"populationSize": 4, "generations": 2, "crossoverProb": 0.7,
                "mutationProb": 0.3, "earlyStoppingGenerations": 10, "elitismPercent": 25, "seed": 42,
                "expert_params": {"risk_per_trade_pct": {"optimize": True, "min": 0.5, "max": 3,
                                                         "step": 0.5, "type": "float"}},
                "backtest": {"engine": "daily", "start_date": "2024-01-02", "end_date": "2024-06-28",
                             "enabled_instruments": ["AAPL"], "experts": [{"class": "FMPRating",
                             "settings": {}}], "initial_capital": 100000.0,
                             "account_settings": {"starting_cash": 100000.0, "commission_per_trade": 1.0,
                             "slippage_bps": 0.0, "fill_model": "next_bar_open"}, "warmup_days": 60,
                             "seed": 42, "subtype": "daily_expert"}}}
    r = client.post(f"/api/strategies/{sid}/optimize", json=body)
    assert r.status_code == 200
    db = SessionLocal()
    try:
        oid = r.json()["optimizationId"]
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == oid).first()
        ga = opt.optimization_config
        assert "expert_params" in ga and "rm_params" not in ga
    finally:
        db.close()
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_optimize_payload.py -q -p no:cacheprovider`
Expected: FAIL — depends on whether `OptimizeRequest` passes `expert_params`/strips `rm_params`. (If the handler/test names differ — e.g. `enqueue_task` — read the file in Step 1 and adjust the monkeypatch target.)

- [ ] **Step 4: Update `OptimizeRequest` + the endpoint**

Ensure `optimization_config` passes through `expert_params` and the `backtest` block (with `expert`/`universe`/`experts`). Remove any handling that injects `rm_params`. Keep the existing validation of required GA keys.

- [ ] **Step 5: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/test_optimize_payload.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/strategies.py backend/tests/test_optimize_payload.py
git commit -m "feat(api): optimize payload carries expert_params/expert/universe; drop rm_params"
```

---

## Task 8: Screener-cache universe resolution

When `universe.mode == "screener"`, resolve symbols from the offline screener-history cache; fail fast if the range isn't cached. Reuse `screened_universe_for_bar` (used by `ba2-test fetch-screener`).

**Files:**
- Create: `backend/app/services/backtest/universe_resolver.py`
- Modify: `backend/app/services/backtest/daily_backtest_handler.py` (use resolver when universe is screener-typed)
- Test: `backend/tests/backtest/test_universe_resolver.py`

- [ ] **Step 1: Read the screener-cache API**

Run: `sed -n '1,60p' backend/app/services/screener_history_cache.py` — confirm `ScreenerHistoryCache(cache_db)` + `screened_universe_for_bar(settings, date, group, cache)` signatures and how to query whether a date is cached.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/backtest/test_universe_resolver.py
import pytest
from app.services.backtest.universe_resolver import resolve_screener_universe, ScreenerCacheMiss


def test_missing_cache_fails_fast(tmp_path):
    with pytest.raises(ScreenerCacheMiss):
        resolve_screener_universe(
            screener_settings={"screener_max_stocks": 5},
            start="2024-01-02", end="2024-01-31",
            cache_db=str(tmp_path / "empty.db"), group="g")
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_universe_resolver.py -q -p no:cacheprovider`
Expected: FAIL — module missing.

- [ ] **Step 4: Write the resolver**

```python
# backend/app/services/backtest/universe_resolver.py
"""Resolve a screener-based backtest universe from the OFFLINE screener-history cache.

Live per-bar screening is slow; backtests read the precomputed survivorship-free cache
built by `ba2-test fetch-screener`. If the requested range isn't cached, fail fast with a
clear message telling the user to build it.
"""
from datetime import datetime
from typing import Any, Dict, List

from app.services.screener_history_cache import ScreenerHistoryCache, screened_universe_for_bar


class ScreenerCacheMiss(RuntimeError):
    pass


def resolve_screener_universe(screener_settings: Dict[str, Any], start: str, end: str,
                              cache_db: str, group: str) -> List[str]:
    """Union of cached survivors across the scan dates in [start, end]. Raises
    ScreenerCacheMiss if no scan date in the range is present in the cache."""
    cache = ScreenerHistoryCache(cache_db)
    s = datetime.fromisoformat(str(start)); e = datetime.fromisoformat(str(end))
    symbols: set = set()
    any_hit = False
    # screened_universe_for_bar returns cached survivors for a bar WITHOUT rebuilding when the
    # cache has the date; here we only READ — a miss must raise, not trigger a live screen.
    for d in cache.cached_scan_dates(group, s, e):  # confirm this read-only accessor exists
        any_hit = True
        symbols.update(r["symbol"] for r in cache.get_survivors(group, d))
    if not any_hit:
        raise ScreenerCacheMiss(
            f"No screener-history cache for {group} in [{start}..{end}]. Build it first: "
            f"ba2-test fetch-screener --settings-json <f> --start {start} --end {end} "
            f"--cache-db {cache_db} --group {group}")
    return sorted(symbols)
```

Note: confirm the read-only accessors (`cached_scan_dates`, `get_survivors`) exist on `ScreenerHistoryCache`; if not, add thin read-only methods there (do NOT call `screened_universe_for_bar`, which rebuilds/screens live). Keep this resolver READ-ONLY.

- [ ] **Step 5: Run the test, verify it passes**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_universe_resolver.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Wire into the daily handler**

In `daily_backtest_handler._build_config` (or `run_daily_backtest`), when the payload carries `universe.mode == "screener"`, call `resolve_screener_universe(...)` to populate `enabled_instruments` before preload; let `ScreenerCacheMiss` propagate as a fail-early error.

- [ ] **Step 7: Regression — full backtest suite**

Run: `~/ba2-venvs/test/bin/python -m pytest tests/backtest/ -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/backtest/universe_resolver.py backend/app/services/backtest/daily_backtest_handler.py backend/tests/backtest/test_universe_resolver.py
git commit -m "feat(backtest): resolve screener universe from offline cache (fail-fast on miss)"
```

---

## Final verification

- [ ] Full backend test sweep: `cd backend && ~/ba2-venvs/test/bin/python -m pytest tests/ -q -p no:cacheprovider` (expect green except the pre-existing env-only `test_leak_gate_is_non_vacuous`).
- [ ] Boot smoke: `~/ba2-venvs/test/bin/python -c "from app.main import app; print('routes', len(app.routes))"`.
- [ ] Manual: `GET /api/experts`, `GET /api/experts/FMPRating/settings-definitions`, `POST /api/strategies/import-rules` round-trip.

## Self-review notes (author)
- Spec coverage: experts endpoints (T3), RM-as-expert-settings + rm:* retire (T1,T2), JSON v1.1 import/export (T4,T5), backtest create + filters (T6), optimize payload (T7), screener cache (T8), ML coexistence preserved (T2 keeps shared decode_params; no ML path touched). Tabs UI + expert/universe/strategy forms = Plan 2 (frontend).
- Open confirmations flagged inline (migration runner signature; `get_settings_definitions` on `__new__`; exit_conditions serialization shape; `enqueue_task` name; `ScreenerHistoryCache` read-only accessors) — each task's Step "Read ..." resolves them against the real code before writing.
