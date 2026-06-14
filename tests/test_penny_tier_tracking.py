"""
Behavioral tests for PennyMomentumTrader identity-based take-profit tier tracking.

The helpers under test live in a dependency-free leaf module
(PennyMomentumTrader/tier_tracking.py) so they can be loaded and exercised
directly without triggering the heavy package __init__ chain.

These cover the fix for the "tier re-fire" bug: a take-profit tier must fire
exactly once, even when the LLM rewrites the tier list on every exit-condition
refresh. Tiers are tracked by a stable id instead of by list index.
"""
import importlib.util
import os

# Phase 6: the leaf module now lives in the ba2_experts package (the in-tree
# PennyMomentumTrader/tier_tracking.py is an alias shim). Load from the package.
# Use import_module (not ``import ba2_experts.PennyMomentumTrader``) because the
# package __init__ binds the same-named *class* onto its namespace, shadowing the
# subpackage module on a plain import.
import importlib as _il
_penny_pkg = _il.import_module("ba2_experts.PennyMomentumTrader")
_BASE = os.path.dirname(_penny_pkg.__file__)


def _load(name, filepath):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_tt = _load("penny_tier_tracking", os.path.join(_BASE, "tier_tracking.py"))
ensure_tier_ids = _tt.ensure_tier_ids
merge_tier_update = _tt.merge_tier_update
migrate_triggered_state = _tt.migrate_triggered_state


# ---------------------------------------------------------------------------
# ensure_tier_ids
# ---------------------------------------------------------------------------

class TestEnsureTierIds:
    def test_assigns_ids_to_idless_tiers(self):
        tiers = [{"condition": {}, "exit_pct": 33}, {"condition": {}, "exit_pct": 50}]
        out, next_id = ensure_tier_ids(tiers, 0)
        ids = [t["id"] for t in out]
        assert len(set(ids)) == 2  # unique
        assert all(ids)            # non-empty
        assert next_id == 2

    def test_preserves_existing_ids(self):
        tiers = [{"id": "t7", "condition": {}, "exit_pct": 33}, {"condition": {}, "exit_pct": 50}]
        out, next_id = ensure_tier_ids(tiers, 5)
        assert out[0]["id"] == "t7"      # untouched
        assert out[1]["id"]              # new one assigned
        assert out[1]["id"] != "t7"
        assert next_id == 6              # only advanced once

    def test_non_dict_entries_ignored(self):
        tiers = [{"condition": {}, "exit_pct": 33}, "garbage"]
        out, next_id = ensure_tier_ids(tiers, 0)
        assert out[0]["id"]
        assert out[1] == "garbage"
        assert next_id == 1


# ---------------------------------------------------------------------------
# merge_tier_update
# ---------------------------------------------------------------------------

class TestMergeTierUpdate:
    def test_surviving_position_keeps_id(self):
        """A tier that already exists at position i keeps its id even when the
        LLM rewrites its condition — so a fired tier never re-arms."""
        old = [
            {"id": "t0", "condition": {"percent": 5}, "exit_pct": 50},
            {"id": "t1", "condition": {"percent": 10}, "exit_pct": 50},
        ]
        new = [
            {"condition": {"percent": 7}, "exit_pct": 50},   # tier 0 trailed up
            {"condition": {"percent": 12}, "exit_pct": 50},
        ]
        merged, next_id = merge_tier_update(old, new, 2)
        assert merged[0]["id"] == "t0"
        assert merged[1]["id"] == "t1"
        # New content is applied
        assert merged[0]["condition"]["percent"] == 7
        assert next_id == 2  # no new ids needed

    def test_added_tier_gets_fresh_id(self):
        old = [{"id": "t0", "condition": {"percent": 5}, "exit_pct": 50}]
        new = [
            {"condition": {"percent": 5}, "exit_pct": 50},
            {"condition": {"percent": 20}, "exit_pct": 100},  # brand new tier
        ]
        merged, next_id = merge_tier_update(old, new, 1)
        assert merged[0]["id"] == "t0"
        assert merged[1]["id"] == "t1"
        assert next_id == 2

    def test_removed_tier_drops_out(self):
        old = [
            {"id": "t0", "condition": {"percent": 5}, "exit_pct": 50},
            {"id": "t1", "condition": {"percent": 10}, "exit_pct": 50},
        ]
        new = [{"condition": {"percent": 5}, "exit_pct": 50}]
        merged, next_id = merge_tier_update(old, new, 2)
        assert len(merged) == 1
        assert merged[0]["id"] == "t0"

    def test_does_not_mutate_old_tiers(self):
        old = [{"id": "t0", "condition": {"percent": 5}, "exit_pct": 50}]
        new = [{"condition": {"percent": 9}, "exit_pct": 50}]
        merge_tier_update(old, new, 1)
        assert old[0]["condition"]["percent"] == 5  # untouched


# ---------------------------------------------------------------------------
# migrate_triggered_state
# ---------------------------------------------------------------------------

class TestMigrateTriggeredState:
    def test_translates_legacy_indices_to_ids(self):
        info = {
            "exit_conditions": {
                "take_profit": [
                    {"condition": {"percent": 5}, "exit_pct": 33},
                    {"condition": {"percent": 10}, "exit_pct": 50},
                    {"condition": {"percent": 20}, "exit_pct": 100},
                ]
            },
            "triggered_tp_tiers": [0, 1],  # legacy index-based fired set
        }
        migrate_triggered_state(info)
        tiers = info["exit_conditions"]["take_profit"]
        # All tiers now have ids
        assert all(t.get("id") for t in tiers)
        # Fired ids correspond to old indices 0 and 1
        assert info["triggered_tp_tier_ids"] == [tiers[0]["id"], tiers[1]["id"]]

    def test_idempotent(self):
        info = {
            "exit_conditions": {"take_profit": [{"condition": {}, "exit_pct": 100}]},
            "triggered_tp_tiers": [0],
        }
        migrate_triggered_state(info)
        first_ids = list(info["triggered_tp_tier_ids"])
        first_tier_id = info["exit_conditions"]["take_profit"][0]["id"]
        migrate_triggered_state(info)
        assert info["triggered_tp_tier_ids"] == first_ids
        assert info["exit_conditions"]["take_profit"][0]["id"] == first_tier_id

    def test_no_legacy_state_starts_empty(self):
        info = {"exit_conditions": {"take_profit": [{"condition": {}, "exit_pct": 100}]}}
        migrate_triggered_state(info)
        assert info["triggered_tp_tier_ids"] == []
        assert info["exit_conditions"]["take_profit"][0].get("id")

    def test_handles_missing_exit_conditions(self):
        info = {}
        migrate_triggered_state(info)  # must not raise
        assert info["triggered_tp_tier_ids"] == []
