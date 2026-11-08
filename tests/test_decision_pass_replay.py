"""The stock decision-pass REPLAY: record real passes with the real recorder, export, replay through the CLI.

Every bundle here is produced by the shipped ``TradeManager.process_expert_recommendations_after_analysis`` /
``process_open_positions_recommendations`` running over a seeded world with a real ``ReplayStore`` session
(the world builders and the fake account are those of ``tests/test_decision_pass_capture_roundtrip.py``);
the session is exported with the export service and replayed by ``ba2-test replay decision`` (the CLI
entry point ``ba2test_launcher._cmd_replay``). Nothing in a bundle is hand-written.

This lives in the root suite (not ``testplatform/backend/tests/replay``) because driving the real
TradeManager needs the root harness (``tests/conftest.py`` seams, ``tests.factories``) and the
``langchain`` stack the testplatform test venv does not carry. The replay modules themselves are
``testplatform/backend/app/services/replay/decision_pass.py`` and friends.

Tampering is done on the exported bundle the way a changed code path would show: an object is rewritten
(content-addressed: new bytes, new hash, manifest updated), so the bundle stays loadable and verified.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ba2_common.core.replay import ReplayStatus, ReplayStore, load_bundle, set_replay_store
from ba2_common.core.replay.codec import content_hash, encode
from tests.test_decision_pass_capture_roundtrip import (
    NOW, _enter_world, _exit_world, _reset, _run_exit,
)
from tests.test_funded_entry_loop import _run_enter

from app.services.replay import decision_pass

MATCH = ReplayStatus.COVERAGE_MATCH
DIFFERENCE = ReplayStatus.COVERAGE_DIFFERENCE
MISSING = ReplayStatus.COVERAGE_MISSING_CAPTURE
SESSION_ID = "S-DECISION"


# --------------------------------------------------------------------------- recording
def _running_code_session():
    """A session claiming EXACTLY the code running this test (else every difference is, rightly, drift)."""
    from app.services.replay.provenance import running_repo
    from ba2_common.core.replay import SessionRecord, provenance as prov

    repo = running_repo()
    snap = prov.snapshot_code(repo)
    versions = prov.read_versions(repo)
    return SessionRecord(
        session_id=SESSION_ID, instance_id="decision-replay-test", started_at=NOW,
        exchange_tz="America/New_York", app_version=versions["app_version"],
        package_versions={k: v for k, v in versions.items() if k.startswith("ba2_")},
        source_revision=snap.revision, dirty=snap.dirty,
        provenance={"patch_sha256": snap.patch_sha256,
                    "untracked": [dict(e) for e in snap.untracked], "versions": versions})


def record(tmp_path: Path, test_engine, build, run) -> Path:
    """Seed a world, run ONE real pass over it recorded into a real store, export the session."""
    _reset(test_engine)
    world = build()
    store = ReplayStore(tmp_path / "store", writer="sync")
    store.begin_session(_running_code_session())
    set_replay_store(store)
    try:
        run(world)
        store.finalize_session(SESSION_ID)
        return store.export_session(SESSION_ID, tmp_path / "bundle")
    finally:
        set_replay_store(None)
        store.close(timeout=5.0)


def enter_bundle(tmp_path, test_engine, per_instrument_pct=None):
    return record(tmp_path, test_engine, _enter_world, lambda w: _run_enter(w[1], w[2], w[0].id))


def exit_bundle(tmp_path, test_engine, action=None):
    return record(tmp_path, test_engine, lambda: _exit_world(action),
                  lambda w: _run_exit(w[1], w[2], w[0].id))


# --------------------------------------------------------------------------- the CLI entry point
def replay_cli(bundle_dir: Path, out: Path = None):
    """``ba2-test replay decision --bundle <dir> [--out <dir>]`` through its real entry point."""
    import ba2test_launcher

    args = types.SimpleNamespace(replay_cmd="decision", bundle=str(bundle_dir),
                                 out=None if out is None else str(out))
    return ba2test_launcher._cmd_replay(args)


# --------------------------------------------------------------------------- tampering
def rewrite_payload(bundle_dir: Path, observation_match, mutate):
    """Rewrite the payload of every observation ``observation_match(o)`` selects with ``mutate(payload)``."""
    manifest_path = Path(bundle_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    from ba2_common.core.replay import load_bundle as _load

    bundle = _load(bundle_dir)
    changed = 0
    for entry in manifest["observations"]:
        if entry["payload_object"] is None:
            continue
        observation = next(o for o in bundle.observations if o.observation_id == entry["observation_id"])
        if not observation_match(observation):
            continue
        payload = bundle.decode(observation.payload_object)
        mutate(payload)
        encoded = encode(payload)
        for side in encoded.sides:
            _put(bundle_dir, manifest, side.kind, side.data)
        new_hash = _put(bundle_dir, manifest, encoded.kind, encoded.data)
        entry["payload_object"] = new_hash
        entry["content_hash"] = new_hash
        changed += 1
    assert changed, "no observation selected for rewriting"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return changed


def _put(bundle_dir, manifest, kind, data) -> str:
    object_hash = content_hash(kind, data)
    relative = f"objects/{object_hash[:2]}/{object_hash}.{'json' if kind == 'json' else 'arrow'}"
    path = Path(bundle_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if not any(o["hash"] == object_hash for o in manifest["objects"]):
        manifest["objects"].append({"hash": object_hash, "kind": kind, "size": len(data), "path": relative})
    return object_hash


def drop_observations(bundle_dir: Path, match):
    manifest_path = Path(bundle_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle = load_bundle(bundle_dir)
    keep, dropped = [], 0
    for entry in manifest["observations"]:
        observation = next(o for o in bundle.observations if o.observation_id == entry["observation_id"])
        if match(observation):
            dropped += 1
        else:
            keep.append(entry)
    assert dropped, "no observation selected for removal"
    manifest["observations"] = keep
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- report helpers
def rows(report, kind=None):
    return [r for r in report.results if kind is None or r.expert_class == kind]


def run_replay(bundle_dir, out=None):
    from app.services.replay.isolation import replay_isolation

    bundle = load_bundle(bundle_dir)
    with replay_isolation():
        return decision_pass.run_bundle(bundle, str(bundle_dir))


# --------------------------------------------------------------------------- the enter pass
def _assert_all_match(report, expect_kinds):
    kinds = sorted(r.expert_class for r in report.results)
    assert kinds == sorted(expect_kinds), kinds
    for row in report.results:
        assert row.status == MATCH, f"{row.expert_class} {row.symbol}: {row.detail} {row.field_diffs}"
        assert row.field_diffs == ()


ENTER_KINDS = ["selection"] + ["rules"] * 3 + ["size"] + ["execute"] * 3
NOT_RUN = ReplayStatus.COVERAGE_NOT_RUN


def test_an_enter_pass_recorded_by_the_real_recorder_replays_to_a_match_at_every_stage(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    report = run_replay(bundle_dir)

    _assert_all_match(report, ENTER_KINDS)
    for stage in decision_pass.STAGES:
        assert len(report.stage_results[stage]) == report.total          # every row at every stage
    assert {r.symbol for r in rows(report, "rules")} == {"AAPL", "MSFT", "GOOGL"}
    statuses = {stage: set(report.stage_results[stage].values()) for stage in decision_pass.STAGES}
    for stage in ("Decision rules", "Decision sizing", "Decision TP/SL", "Decision order build",
                  "Decision submit / refusal"):
        assert statuses[stage] == {MATCH, NOT_RUN}, stage


def test_the_cli_entry_point_replays_a_bundle_writes_the_report_and_the_coverage(tmp_path, test_engine, capsys):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    out = tmp_path / "report"

    code = replay_cli(bundle_dir, out)

    assert code == 0
    printed = capsys.readouterr().out
    assert "decision and execution comparison" in printed            # the capability that ran
    assert "smart risk manager" in printed.lower()                   # and what it did not run
    markdown = (out / "decision.md").read_text(encoding="utf-8")
    for stage in ("Selection", "Rules and sizing", "Execution", "Decision rules", "Decision sizing",
                  "Decision TP/SL", "Decision order build", "Decision submit / refusal"):
        assert stage in markdown
    payload = json.loads((out / "decision.json").read_text(encoding="utf-8"))
    assert payload["capability"] == "decision" and payload["total"] == len(ENTER_KINDS)
    stage_status = {row["stage"]: row["status"] for row in payload["stages"]}
    assert stage_status["Rules and sizing"] == "match 4, not_run 4"
    assert stage_status["Execution"] == "match 3, not_run 5"
    assert stage_status["Selection"] == "match 1, not_run 7"
    coverage = json.loads((Path(bundle_dir) / "coverage.json").read_text(encoding="utf-8"))["entries"]
    decision = [e for e in coverage if e["capability"] == ReplayStatus.CAPABILITY_DECISION]
    assert len(decision) == len(ENTER_KINDS) and {e["status"] for e in decision} == {MATCH}


def test_the_cli_exits_non_zero_on_a_difference(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    rewrite_payload(bundle_dir, _pass_state, _raise_the_confidence_bar)
    assert replay_cli(bundle_dir) == 1


# --------------------------------------------------------------------------- tampered recordings
def _pass_state(o):
    return o.provider == "decision_step" and o.method == "pass_state"


def _raise_the_confidence_bar(payload):
    edited = 0
    for entry in payload["db_rows"]:
        if entry["model"] == "EventAction":
            for trigger in entry["row"]["triggers"].values():
                if trigger["event_type"] == "confidence":
                    trigger["value"] = 95.0                      # the recorded confidence is 90
                    edited += 1
    assert edited


def test_an_edited_ruleset_condition_is_a_rules_difference_and_the_steps_after_it_say_so(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    rewrite_payload(bundle_dir, _pass_state, _raise_the_confidence_bar)

    report = run_replay(bundle_dir)

    for row in rows(report, "rules"):
        assert row.status == DIFFERENCE, (row.symbol, row.detail)
        assert row.field_diffs
    assert report.stage_results["Decision rules"][rows(report, "rules")[0].row_id] == DIFFERENCE
    # the recorded size/execute steps have no candidate in the re-run: a difference, named, never skipped
    assert [r.status for r in rows(report, "size")] == [DIFFERENCE]
    assert [r.status for r in rows(report, "execute")] == [DIFFERENCE] * 3
    assert rows(report, "selection")[0].status == MATCH


def test_a_deleted_account_snapshot_read_is_missing_capture_naming_the_step_and_the_read(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    drop_observations(bundle_dir, lambda o: o.method == "get_account_snapshot")

    report = run_replay(bundle_dir)

    rules = rows(report, "rules")
    assert [r.status for r in rules] == [MISSING] * 3
    for row in rules:
        assert "get_account_snapshot" in row.detail and row.symbol in row.detail
        assert "gates" in row.detail
    # every step that depends on them says so: not skipped, not a match
    (size,) = rows(report, "size")
    assert size.status == MISSING and "depends on the rules" in size.detail
    assert [r.status for r in rows(report, "execute")] == [MISSING] * 3
    assert all("depends" in r.detail for r in rows(report, "execute"))


def test_a_changed_per_instrument_cap_is_a_sizing_difference_with_the_delta(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)

    def halve_the_cap(payload):
        edited = 0
        for entry in payload["db_rows"]:
            if (entry["model"] == "ExpertSetting"
                    and entry["row"]["key"] == "max_virtual_equity_per_instrument_percent"):
                entry["row"]["value_float"] = 20.0                # recorded: 40.0
                edited += 1
        assert edited

    rewrite_payload(bundle_dir, _pass_state, halve_the_cap)
    report = run_replay(bundle_dir)

    assert all(r.status == MATCH for r in rows(report, "rules"))   # the rules do not read the cap
    (size,) = rows(report, "size")
    assert size.status == DIFFERENCE
    quantity = [d for d in size.field_diffs if d.field.endswith("['quantity']") and d.abs_delta is not None]
    assert quantity and all(d.abs_delta > 0 for d in quantity)
    assert any(d.rel_delta is not None for d in quantity)
    assert report.stage_results["Decision sizing"][size.row_id] == DIFFERENCE
    executes = rows(report, "execute")
    assert executes and DIFFERENCE in {r.status for r in executes}


def test_an_execute_step_is_missing_capture_when_the_rules_step_it_depends_on_is(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    drop_observations(bundle_dir, lambda o: o.provider == "decision_step" and o.method == "rules"
                      and o.request_identity["symbol"] == "AAPL")

    report = run_replay(bundle_dir)

    by_symbol = {r.symbol: r for r in rows(report, "rules")}
    assert by_symbol["AAPL"].status == MISSING and "no rules step was recorded" in by_symbol["AAPL"].detail
    assert by_symbol["MSFT"].status == MATCH and by_symbol["GOOGL"].status == MATCH
    (size,) = rows(report, "size")
    assert size.status == MISSING and "AAPL" in size.detail
    executes = {r.symbol: r for r in rows(report, "execute")}
    assert set(executes) == {"AAPL", "MSFT", "GOOGL"}
    assert all(r.status == MISSING and "depends" in r.detail for r in executes.values())


def test_a_missing_size_step_is_reported_not_dropped(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)
    drop_observations(bundle_dir, lambda o: o.provider == "decision_step" and o.method == "size")

    report = run_replay(bundle_dir)

    (size,) = rows(report, "size")
    assert size.status == DIFFERENCE and "no size step" in size.detail
    assert len(rows(report, "execute")) == 3


# --------------------------------------------------------------------------- the exit pass
def test_an_exit_pass_adjusting_the_stop_replays_to_a_match(tmp_path, test_engine):
    bundle_dir = exit_bundle(tmp_path, test_engine)
    report = run_replay(bundle_dir)
    _assert_all_match(report, ["rules", "execute"])
    (execute,) = rows(report, "execute")
    assert report.stage_results["Decision TP/SL"][execute.row_id] == MATCH


def test_an_exit_pass_closing_the_position_replays_to_a_match(tmp_path, test_engine):
    from ba2_common.core.types import ExpertActionType

    bundle_dir = exit_bundle(tmp_path, test_engine, {"action_type": ExpertActionType.CLOSE.value})
    report = run_replay(bundle_dir)
    _assert_all_match(report, ["rules", "execute"])


def test_an_exit_whose_stop_price_differs_is_a_tpsl_difference_with_the_delta(tmp_path, test_engine):
    bundle_dir = exit_bundle(tmp_path, test_engine)

    def move_the_stop_rule(payload):
        for entry in payload["db_rows"]:
            if entry["model"] == "EventAction":
                for action in entry["row"]["actions"].values():
                    action["value"] = -10.0                       # recorded -5.0

    rewrite_payload(bundle_dir, _pass_state, move_the_stop_rule)
    report = run_replay(bundle_dir)
    (execute,) = rows(report, "execute")
    assert execute.status == DIFFERENCE
    assert report.stage_results["Decision TP/SL"][execute.row_id] == DIFFERENCE
    assert any(d.abs_delta for d in execute.field_diffs)


# --------------------------------------------------------------------------- inventory and the other capabilities
def test_the_inventory_counts_a_decision_pass_by_record_kind_not_as_an_expert_analysis(tmp_path, test_engine):
    from app.services.replay import inventory

    bundle_dir = enter_bundle(tmp_path, test_engine)
    counted = inventory.run(bundle_dir)

    assert counted["analyses_by_record_kind"] == {"decision_pass": 1}
    assert counted["analyses_by_expert"] == {} and counted["analyses_by_expert_and_outcome"] == {}
    passes = counted["decision_passes"]
    assert passes["total"] == 1 and passes["by_pass"] == {"enter-market": 1}
    assert passes["by_outcome"] == {"decision": 1}
    assert passes["steps_by_kind"] == {"pass_state": 1, "rules": 3, "size": 1, "execute": 3}
    # the expert capabilities owe this analysis no replay; the decision one does, and says not_run
    assert counted["coverage_by_capability"]["recorded_expert"]["not_run"] == 0
    assert counted["coverage_by_capability"]["decision"]["not_run"] == 1
    assert "Decision passes" in inventory.to_markdown(counted)

    assert replay_cli(bundle_dir) == 0
    after = inventory.run(bundle_dir)
    assert after["coverage_by_capability"]["decision"]["match"] == len(ENTER_KINDS)
    assert after["coverage_by_capability"]["decision"]["not_run"] == 0


def test_the_expert_capabilities_skip_a_decision_pass_without_noise(tmp_path, test_engine):
    from app.services.replay import expert_replay, gather_tape

    bundle_dir = enter_bundle(tmp_path, test_engine)
    assert expert_replay.run(bundle_dir).results == []
    assert gather_tape.run(bundle_dir).results == []


def _skip_world():
    world = _enter_world()
    world[1].save_settings({"allow_automated_trade_opening": (False, "bool")})
    return world


def test_a_pass_that_ended_as_a_recorded_skip_is_a_not_run_row(tmp_path, test_engine):
    bundle_dir = record(tmp_path, test_engine, _skip_world, lambda w: _run_enter(w[1], w[2], w[0].id))
    report = run_replay(bundle_dir)
    (row,) = report.results
    assert row.expert_class == "pass" and row.status == ReplayStatus.COVERAGE_NOT_RUN
    assert "decision not to act" in row.detail


# --------------------------------------------------------------------------- the tape account
class _FakeTape:
    def __init__(self, held):
        self.held = held                           # {(provider, method): value}
        self.taken = []

    def identities(self, provider, method):
        return ({"account_class": "X", "account_id": 1},) if (provider, method) in self.held else ()

    def take(self, provider, method, identity):
        self.taken.append((provider, method))
        return self.held[(provider, method)]


def _tape_account(tape):
    cls = decision_pass._account_class("X", {})
    return cls(tape, account_id=1, analysis_id="a", settings={"margin_enabled": False},
               price_type_in_quote=True)


@pytest.mark.parametrize("provider", ["broker", "broker_options"])
def test_the_tape_account_serves_snapshot_and_positions_from_whichever_provider_recorded_them(provider):
    tape = _FakeTape({(provider, "get_account_snapshot"): "SNAP", (provider, "get_positions"): []})
    account = _tape_account(tape)
    assert account.get_account_snapshot() == "SNAP" and account.get_positions() == []
    assert tape.taken == [(provider, "get_account_snapshot"), (provider, "get_positions")]
    assert account.settings == {"margin_enabled": False}              # the recorded account settings


def test_an_unrecorded_read_is_a_typed_miss_and_an_unserved_method_is_refused_loudly():
    from ba2_common.core.replay import ReplayMiss

    class _EmptyTape(_FakeTape):
        def take(self, provider, method, identity):
            raise ReplayMiss("tape", detail="nothing recorded")

    account = _tape_account(_EmptyTape({}))
    with pytest.raises(ReplayMiss):
        account.get_balance()
    with pytest.raises(ReplayMiss):
        account.get_account_snapshot()
    with pytest.raises(ReplayMiss, match="untapped_broker_read|broker_write"):
        account.get_orders()                                          # not a tapped read: refused


def test_an_account_call_the_rerun_makes_that_live_did_not_is_a_difference_naming_it(tmp_path, test_engine):
    bundle_dir = enter_bundle(tmp_path, test_engine)

    def forget_the_first_effect(payload):
        payload["outcome"]["effects"] = payload["outcome"]["effects"][1:]

    rewrite_payload(bundle_dir,
                    lambda o: o.provider == "decision_step" and o.method == "execute"
                    and o.request_identity["symbol"] == "AAPL", forget_the_first_effect)
    report = run_replay(bundle_dir)

    aapl = {r.symbol: r for r in rows(report, "execute")}["AAPL"]
    assert aapl.status == DIFFERENCE
    assert report.stage_results["Decision submit / refusal"][aapl.row_id] == DIFFERENCE
    assert any("effects" in d.field for d in aapl.field_diffs)
    others = [r for r in rows(report, "execute") if r.symbol != "AAPL"]
    assert all(r.status == MATCH for r in others)
