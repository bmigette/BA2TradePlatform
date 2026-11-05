"""Option selection replay: a recorded option entry re-run on its tape.

The bundle is recorded through the real recorder and exported (``option_support``); these tests
pin the replay service itself: every stage matches on a complete tape, a removed read is a
``missing_capture`` naming the request, a changed selection config is a ``selection``
difference, the tape account refuses writes, and the report states what it did not run.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import ba2_common
import app.services.replay.option_selection as option_selection
from ba2_common.core.replay import ReplayMiss, ReplayStatus, load_bundle
from ba2_common.core.replay import option_action_record as oar

from app.services.replay import report as report_module
from app.services.replay.isolation import replay_isolation
from app.services.replay.option_selection import TapeOptionsAccount, run
from tests.replay import drop_observations
from tests.replay.option_support import (
    BPS, RecordingOptionsAccount, entry_action, record_session,
)

WORKTREE = Path(__file__).resolve().parents[4]
MATCH = ReplayStatus.COVERAGE_MATCH


def test_the_modules_under_test_come_from_this_worktree():
    """The shared venv's editable installs point at the MAIN checkout; pytest.ini's pythonpath
    must win. A replay test that silently ran the main clone's code proves nothing."""
    for module in (option_selection, ba2_common):
        assert Path(module.__file__).resolve().is_relative_to(WORKTREE), module.__file__


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("option-selection")

    def scenario():
        account = RecordingOptionsAccount()
        return entry_action(account).execute(), account

    recorded = record_session(root / "store", root / "export", scenario)
    result, account = recorded["outcome"]
    assert result["success"], result["message"]
    assert account.submitted, "the recorded entry must have submitted"
    return recorded["bundle_dir"]


@pytest.fixture
def bundle_copy(session, tmp_path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(session, target)
    return target


def _only(report):
    (row,) = report.results
    return row


def _stage(report, stage):
    (status,) = report.stage_results[stage].values()
    return status


def test_a_complete_tape_matches_at_every_stage(session):
    report = option_selection.run(session)
    row = _only(report)
    assert row.status == MATCH, row.detail
    for stage in option_selection.STAGES:
        assert _stage(report, stage) == MATCH, stage
    assert row.field_diffs == ()
    assert row.expert_class == "OpenBullPutSpreadAction"
    assert row.symbol == "XYZ"


def test_the_replay_does_not_touch_the_network_the_instance_resolver_or_a_real_database(session):
    bundle = load_bundle(session)
    with replay_isolation() as probe:
        option_selection.run_bundle(bundle)
    assert probe.clean, probe.summary()


def test_a_removed_chain_read_is_missing_capture_naming_the_request(bundle_copy):
    analysis_id = load_bundle(bundle_copy).analyses[0].analysis_id
    assert drop_observations(bundle_copy, analysis_id, "get_option_chain") > 0
    report = option_selection.run(bundle_copy)
    row = _only(report)
    assert row.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "get_option_chain" in row.detail and "XYZ" in row.detail
    for stage in option_selection.STAGES:
        assert _stage(report, stage) == ReplayStatus.COVERAGE_MISSING_CAPTURE


class _EditedBundle:
    """The bundle, with the recorded action's constructor config edited on decode."""

    def __init__(self, bundle, edit):
        self._bundle = bundle
        self._edit = edit

    def __getattr__(self, name):
        return getattr(self._bundle, name)

    def decode(self, *args, **kwargs):
        value = self._bundle.decode(*args, **kwargs)
        if isinstance(value, dict) and "inputs" in value and "outcome" in value:
            self._edit(value)
        return value


@pytest.fixture(scope="module")
def long_call_session(tmp_path_factory) -> Path:
    """A debit entry: its downstream reads do not depend on the contract chosen (no reserve),
    so a changed strike changes the DECISION and not the requests the tape must answer."""
    root = tmp_path_factory.mktemp("option-selection-long")

    def scenario():
        account = RecordingOptionsAccount()
        return entry_action(account, "buy_call", strike_method="percent_otm", strike_param=0.0,
                            dte_min=10, dte_max=40, sizing=5.0).execute()

    recorded = record_session(root / "store", root / "export", scenario)
    assert recorded["outcome"]["success"], recorded["outcome"]["message"]
    return recorded["bundle_dir"]


def test_an_edited_strike_config_is_a_selection_difference(long_call_session):
    matching = option_selection.run(long_call_session)
    assert _only(matching).status == MATCH, _only(matching).detail

    def edit(payload):
        payload["inputs"]["ctor_kwargs"]["strike_param"] = 10.0

    with replay_isolation():
        report = option_selection.run_bundle(_EditedBundle(load_bundle(long_call_session), edit))
    row = _only(report)
    assert row.status == ReplayStatus.COVERAGE_DIFFERENCE, row.detail
    assert _stage(report, option_selection.STAGE_OPTION_SELECTION) == ReplayStatus.COVERAGE_DIFFERENCE
    assert any(diff.field.startswith("selection") and "contract_symbol" in diff.field
               for diff in row.field_diffs)


def test_a_changed_credit_spread_strike_asks_the_tape_for_a_read_it_lacks(session):
    """A credit spread's capacity check is keyed by the strikes chosen, so a different strike
    is a request the tape never recorded: missing_capture naming it, not a guessed answer."""
    def edit(payload):
        payload["inputs"]["ctor_kwargs"]["strike_param"] = [10.0, 15.0]

    with replay_isolation():
        report = option_selection.run_bundle(_EditedBundle(load_bundle(session), edit))
    row = _only(report)
    assert row.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "check_short_put_assignment_capacity" in row.detail


def test_an_edited_recorded_limit_is_a_pricing_difference_only(session):
    def edit(payload):
        payload["outcome"]["submits"][0]["limit_price"] += 0.01

    bundle = _EditedBundle(load_bundle(session), edit)
    with replay_isolation():
        report = option_selection.run_bundle(bundle)
    assert _stage(report, option_selection.STAGE_OPTION_PRICING) == ReplayStatus.COVERAGE_DIFFERENCE
    assert _stage(report, option_selection.STAGE_OPTION_SELECTION) == MATCH
    assert _stage(report, option_selection.STAGE_OPTION_SIZING) == MATCH


def test_an_edited_recorded_quantity_and_record_key_are_reported_by_key(session):
    def edit(payload):
        payload["outcome"]["submits"][0]["quantity"] += 1
        record = payload["outcome"]["entry_record"]
        record["version"] = "tampered"

    bundle = _EditedBundle(load_bundle(session), edit)
    with replay_isolation():
        report = option_selection.run_bundle(bundle)
    row = _only(report)
    assert _stage(report, option_selection.STAGE_OPTION_SIZING) == ReplayStatus.COVERAGE_DIFFERENCE
    assert _stage(report, option_selection.STAGE_OPTION_RECORD) == ReplayStatus.COVERAGE_DIFFERENCE
    assert any("entry_record" in diff.field and "version" in diff.field
               for diff in row.field_diffs)


def test_an_edited_refusal_message_is_a_refusal_difference(tmp_path):
    root = tmp_path

    def scenario():
        account = RecordingOptionsAccount()
        return entry_action(account, **dict(BPS, dte_min=500, dte_max=600)).execute()

    recorded = record_session(root / "store", root / "export", scenario)
    assert not recorded["outcome"]["success"]
    matching = option_selection.run(recorded["bundle_dir"])
    assert _only(matching).status == MATCH

    def edit(payload):
        payload["outcome"]["message"] = "a different refusal"

    with replay_isolation():
        report = option_selection.run_bundle(_EditedBundle(load_bundle(recorded["bundle_dir"]), edit))
    assert _stage(report, option_selection.STAGE_OPTION_REFUSAL) == ReplayStatus.COVERAGE_DIFFERENCE


def test_a_clock_read_that_is_not_in_the_record_is_missing_capture(session):
    def edit(payload):
        from datetime import timedelta
        payload["inputs"]["decision_time"] = payload["inputs"]["decision_time"] + timedelta(seconds=3)

    with replay_isolation():
        report = option_selection.run_bundle(_EditedBundle(load_bundle(session), edit))
    row = _only(report)
    assert row.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "decision clock" in row.detail


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
def test_the_report_names_what_was_not_run_and_carries_the_option_stage_rows(session, tmp_path):
    out = tmp_path / "report"
    report = option_selection.run(session, out)
    markdown = (out / "option_selection.md").read_text(encoding="utf-8")
    payload = json.loads((out / "option_selection.json").read_text(encoding="utf-8"))
    assert "historical_provider" in markdown and "NOT run" in markdown
    assert "historical_provider" in payload["capabilities_not_run"]
    assert payload["counts"][MATCH] == report.total == 1
    stages = {row["stage"]: row["status"] for row in payload["stages"]}
    for stage in option_selection.STAGES:
        assert stages[stage] == "match 1", stages
        line = next(l for l in markdown.splitlines() if l.startswith(f"| {stage} |"))
        assert line.rstrip().endswith("| match 1 |")


def test_running_writes_the_coverage_back_into_the_bundle(bundle_copy):
    option_selection.run(bundle_copy)
    coverage = json.loads((bundle_copy / "coverage.json").read_text(encoding="utf-8"))
    rows = [e for e in coverage["entries"] if e["capability"] == "option_selection"]
    assert [r["status"] for r in rows] == [MATCH]


def test_the_stock_reports_are_unchanged_by_the_new_capability():
    assert report_module.CAPABILITY_OPTION_SELECTION not in report_module.CAPABILITY_TITLES
    assert all(stage not in [row[0] for row in report_module.STAGE_ROWS]
               for stage in option_selection.STAGES)


# --------------------------------------------------------------------------- #
# the tape account
# --------------------------------------------------------------------------- #
class _EmptyTape:
    misses = []

    def take(self, provider, method, identity):
        raise ReplayMiss("tape", request_identity={"method": method})

    def identities(self, provider, method):
        return ()


def _bare_account():
    cls = type("AlpacaAccount", (TapeOptionsAccount,), {})
    return cls(_EmptyTape(), account_id=3, analysis_id="a", price_type_in_quote=True,
               price_type_in_underlying=True)


def test_every_abstract_broker_member_refuses_instead_of_answering():
    account = _bare_account()
    assert type(account).__name__ == "AlpacaAccount" and account.id == 3
    for name in ("cancel_order", "modify_order", "adjust_tp", "adjust_sl", "adjust_tp_sl",
                 "_submit_order_impl", "refresh_orders", "refresh_positions"):
        with pytest.raises(ReplayMiss) as excinfo:
            getattr(account, name)()
        assert excinfo.value.kind == "broker_write", name
    with pytest.raises(ReplayMiss) as excinfo:
        account.get_balance()
    assert excinfo.value.kind == "untapped_broker_read"


def test_a_tapped_read_the_tape_lacks_is_a_typed_miss():
    account = _bare_account()
    with pytest.raises(ReplayMiss, match="get_option_quote"):
        account.get_option_quote("XYZ261127P00095000")


# --------------------------------------------------------------------------- #
# the CLI (out of process, like the other replay commands: main() chdirs and rebinds a DB)
# --------------------------------------------------------------------------- #
def test_the_cli_parses_and_runs_option_selection(bundle_copy, tmp_path):
    import os
    import subprocess
    import sys

    launcher = WORKTREE / "testplatform" / "ba2test_launcher.py"
    out = tmp_path / "cli"
    result = subprocess.run(
        [sys.executable, str(launcher), "replay", "option-selection",
         "--bundle", str(bundle_copy), "--out", str(out)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            str(WORKTREE / "packages" / name) for name in ("common", "providers", "experts"))})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (out / "option_selection.md").is_file() and (out / "option_selection.json").is_file()
    assert "Capability run:" in result.stdout and "historical_provider" in result.stdout


def test_the_tape_account_serves_a_recorded_stock_quote_by_identity_and_in_recorded_order(session):
    """The stock quote tap (provider ``broker``) is served through the same identity function the
    recorder used; a second read of the same request is a miss, not the last value again."""
    bundle = load_bundle(session)
    (analysis,) = bundle.analyses
    observations = [o for o in bundle.observations_for(analysis.analysis_id)
                    if o.provider in ("broker", "broker_options")]
    action = next(o for o in bundle.observations_for(analysis.analysis_id)
                  if o.provider == "option_action")
    inputs = bundle.decode(action.payload_object)["inputs"]
    tape = option_selection.SpanTape(bundle, analysis, observations)
    account = option_selection._tape_account(tape, inputs, analysis.analysis_id)
    assert account.get_instrument_current_price("XYZ", "mid") == 100.0
    with pytest.raises(ReplayMiss, match="tape_exhausted"):
        account.get_instrument_current_price("XYZ", "mid")
    with pytest.raises(ReplayMiss, match="get_instrument_current_price"):
        account.get_instrument_current_price("OTHER", "mid")
    assert [m.kind for m in tape.misses] == ["tape_exhausted", "tape"]
