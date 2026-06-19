"""Round-trip test for ba2_experts.settings_io (Amendment A3).

Export an expert config from a temp DB, drop the rows, re-import, and assert the
re-exported config equals the original. Uses ba2_experts.get_expert_class to
resolve/validate expert types (a known package expert: FMPEarningsDrift).
"""
import pathlib
import tempfile

import pytest
from sqlmodel import select

from ba2_common.core import db
from ba2_common.core.models import (
    AccountDefinition,
    ExpertInstance,
    ExpertSetting,
)
from ba2_experts.settings_io import (
    export_expert_settings,
    import_expert_settings,
)


@pytest.fixture()
def fresh_db():
    """A throwaway sqlite DB for one test (resets ba2_common's DB seam)."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "settings_io.sqlite"
    db.configure_db(str(tmp))
    db.init_db()
    yield


def _seed_one_expert(session):
    """Create an account + one FMPEarningsDrift expert with two settings.
    Returns the expert id."""
    acct = AccountDefinition(name="paper-1", provider="alpaca",
                             description="test account")
    session.add(acct)
    session.flush()

    expert = ExpertInstance(
        expert="FMPEarningsDrift",
        account_id=acct.id,
        enabled=True,
        alias="drift-main",
        user_description="primary earnings-drift expert",
        virtual_equity_pct=42.5,
    )
    session.add(expert)
    session.flush()

    session.add(ExpertSetting(instance_id=expert.id, key="surprise_min_pct",
                              value_str=None, value_json={}, value_float=7.5))
    session.add(ExpertSetting(instance_id=expert.id, key="max_days_since_report",
                              value_str="14", value_json={}, value_float=None))
    session.commit()
    return expert.id


def test_export_import_round_trip(fresh_db):
    # Seed and export the original config.
    with db.get_db() as session:
        _seed_one_expert(session)
        original = export_expert_settings(session)

    assert len(original) == 1
    exp = original[0]
    assert exp["expert"] == "FMPEarningsDrift"
    assert exp["account_name"] == "paper-1"
    assert exp["alias"] == "drift-main"
    assert exp["virtual_equity_pct"] == 42.5
    assert {s["key"] for s in exp["settings"]} == {"surprise_min_pct", "max_days_since_report"}

    # Wipe the experts + settings (keep the account so import can match by name).
    with db.get_db() as session:
        for s in session.exec(select(ExpertSetting)).all():
            session.delete(s)
        for e in session.exec(select(ExpertInstance)).all():
            session.delete(e)
        session.commit()

    # Confirm the wipe.
    with db.get_db() as session:
        assert export_expert_settings(session) == []

    # Re-import the original config and commit.
    with db.get_db() as session:
        stats = import_expert_settings(session, original, dry_run=False)
        session.commit()

    assert stats["experts_created"] == 1
    assert stats["experts_skipped"] == 0
    assert stats["settings_created"] == 2

    # Re-export and compare to the original (order-independent on settings).
    with db.get_db() as session:
        round_tripped = export_expert_settings(session)

    assert len(round_tripped) == 1
    rt = round_tripped[0]

    def _norm(cfg):
        cfg = dict(cfg)
        cfg["settings"] = sorted(cfg["settings"], key=lambda s: s["key"])
        return cfg

    assert _norm(rt) == _norm(exp)


def test_import_skips_unknown_expert_type(fresh_db):
    """An exported expert whose type this package does not know is skipped, not created."""
    with db.get_db() as session:
        acct = AccountDefinition(name="paper-2", provider="alpaca", description=None)
        session.add(acct)
        session.commit()

    config = [{
        "expert": "SomeLiveOnlyExpert",   # not in ba2_experts.get_expert_class
        "account_name": "paper-2",
        "enabled": True,
        "alias": None,
        "user_description": None,
        "virtual_equity_pct": 100.0,
        "settings": [{"key": "x", "value_str": "1", "value_json": {}, "value_float": None}],
    }]

    with db.get_db() as session:
        stats = import_expert_settings(session, config, dry_run=False)
        session.commit()

    assert stats["experts_skipped"] == 1
    assert stats["experts_created"] == 0

    with db.get_db() as session:
        assert export_expert_settings(session) == []


def test_dry_run_makes_no_changes(fresh_db):
    """A dry-run import reports stats but writes nothing."""
    with db.get_db() as session:
        acct = AccountDefinition(name="paper-3", provider="alpaca", description=None)
        session.add(acct)
        session.commit()

    config = [{
        "expert": "FMPEarningsDrift",
        "account_name": "paper-3",
        "enabled": True,
        "alias": "dry",
        "user_description": None,
        "virtual_equity_pct": 100.0,
        "settings": [{"key": "surprise_min_pct", "value_str": None,
                      "value_json": {}, "value_float": 5.0}],
    }]

    with db.get_db() as session:
        stats = import_expert_settings(session, config, dry_run=True)
        session.rollback()

    assert stats["experts_created"] == 1
    assert stats["settings_created"] == 1

    with db.get_db() as session:
        assert export_expert_settings(session) == []
