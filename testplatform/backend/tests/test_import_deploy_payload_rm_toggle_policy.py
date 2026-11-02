"""``tools/import_deploy_payload.py`` under the atr_grid_2027 run policy (design §3.2 item 6).

``_apply_rm_toggles`` used to unconditionally OVERWRITE the payload's toggle with the CLI flag.
That is correct for every payload on record (``use_atr_stop`` always False there), and wrong for
a policy row whose backtest genuinely ran with ``use_atr_stop=True``: deploying it without
``--use-atr`` would silently reset it to False on the live instance -- a DIFFERENT strategy from
the one that was scored. This pins the refusal, the reworded banner, and that every existing
(all-False) payload is unaffected.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))            # testplatform/backend/tests
_TOOLS_DIR = os.path.normpath(os.path.join(_ROOT, "..", "..", "..", "tools"))


@pytest.fixture
def tool(tmp_path):
    """The import tool module, safely imported: BA2_LIVE_DB/BA2_REPO point at throwaway paths
    and ba2_common.core.db's process-global engine is snapshotted/restored, so this never
    touches the real live trade DB. Mirrors test_deploy_payload_market_conditions.py's live_db
    fixture."""
    import ba2_common.core.db as ba2db
    from sqlmodel import SQLModel

    saved_file, saved_engine = ba2db._db_file, ba2db._engine
    saved_path = list(sys.path)
    db_path = str(tmp_path / "live.sqlite")
    saved_env = {k: os.environ.get(k) for k in ("BA2_LIVE_DB", "BA2_REPO")}
    os.environ["BA2_LIVE_DB"] = db_path
    os.environ["BA2_REPO"] = os.path.dirname(_TOOLS_DIR)   # never the main checkout
    try:
        spec = importlib.util.spec_from_file_location(
            "import_deploy_payload_rm_toggle_policy_test",
            os.path.join(_TOOLS_DIR, "import_deploy_payload.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        SQLModel.metadata.create_all(ba2db.get_engine())
        yield m
    finally:
        try:
            ba2db.get_engine().dispose()
        except Exception:  # noqa: BLE001 -- teardown must not mask a failure
            pass
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ba2db._db_file, ba2db._engine = saved_file, saved_engine
        sys.path[:] = saved_path


class TestExistingAllFalsePayloadsAreUnaffected:
    def test_off_stays_off_without_any_flag(self, tool):
        expert_params = {"use_atr_stop": False, "regime_overlay_enabled": False}
        tool._apply_rm_toggles(expert_params, {"use_atr_stop": False, "regime_overlay_enabled": False},
                               label="x")
        assert expert_params == {"use_atr_stop": False, "regime_overlay_enabled": False}

    def test_flags_can_still_force_it_on_deliberately(self, tool):
        """The escape hatch (--use-atr on a payload that did NOT exercise it) must keep working
        -- it is how a deliberate ATR/regime experiment starts."""
        expert_params = {"use_atr_stop": False, "regime_overlay_enabled": False}
        tool._apply_rm_toggles(expert_params, {"use_atr_stop": True, "regime_overlay_enabled": False},
                               label="x")
        assert expert_params["use_atr_stop"] is True


class TestAPolicyRowThatExercisedIt:
    def test_true_without_the_flag_is_refused(self, tool):
        """THE POINT: a policy row's payload already carries use_atr_stop=True (forced_expert_
        settings, from the row's own model:* gene -- see app.api.backtests._executed_toggle).
        Deploying it without --use-atr must refuse rather than silently write False."""
        expert_params = {"use_atr_stop": True, "regime_overlay_enabled": False}
        with pytest.raises(ValueError):
            tool._apply_rm_toggles(
                expert_params, {"use_atr_stop": False, "regime_overlay_enabled": False}, label="x")

    def test_true_with_the_flag_deploys_it_as_scored(self, tool):
        expert_params = {"use_atr_stop": True, "regime_overlay_enabled": False}
        tool._apply_rm_toggles(
            expert_params, {"use_atr_stop": True, "regime_overlay_enabled": False}, label="x")
        assert expert_params["use_atr_stop"] is True

    def test_main_names_the_row_and_exits_1(self, tool, capsys, monkeypatch, tmp_path):
        """The main() call site catches the ValueError and prints FATAL: <label>: ... then
        returns 1, matching every other refusal in this tool."""
        import json

        from ba2_common.core.db import add_instance
        from ba2_common.core.models import ExpertInstance

        inst_id = add_instance(ExpertInstance(
            account_id=1, expert="FMPRating", alias="before", enabled=True,
            virtual_equity_pct=10.0))
        payload = [{
            "backtest_id": 1, "target_instance_id": inst_id, "account_id": 1,
            "virtual_equity_pct": 10.0, "expert_name": "FMPRating", "label": "atr27-row",
            "ruleset": {"entry_rules": [], "exit_rules": []},
            "settings": {"settings": {"expert_params": {"use_atr_stop": True,
                                                        "regime_overlay_enabled": False}},
                        "universe": None, "execution": {}},
        }]
        path = str(tmp_path / "payload.json")
        json.dump(payload, open(path, "w"), default=str)
        monkeypatch.setattr(sys, "argv", ["import_deploy_payload.py", path])

        rc = tool.main()

        assert rc == 1
        out = capsys.readouterr().out
        assert "FATAL: atr27-row:" in out
        assert "--use-atr" in out


def test_the_banner_names_a_policy_row_differently_from_a_deliberate_override(tool, capsys):
    """A policy row's ON banner must say the backtest EXERCISED it; the historical (deliberate
    override) banner must say it did NOT -- the two are not the same claim."""
    tool._apply_rm_toggles(
        {"use_atr_stop": True, "regime_overlay_enabled": False},
        {"use_atr_stop": True, "regime_overlay_enabled": False}, label="policy-row")
    out = capsys.readouterr().out
    assert "ACTUALLY EXERCISED" in out

    tool._apply_rm_toggles(
        {"use_atr_stop": False, "regime_overlay_enabled": False},
        {"use_atr_stop": True, "regime_overlay_enabled": False}, label="deliberate-override")
    out2 = capsys.readouterr().out
    assert "did NOT exercise" in out2 or "NOT reproduce" in out2
