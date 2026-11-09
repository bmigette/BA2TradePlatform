"""Alembic c4e7a9d2f1b8: every EXISTING DeterministicScorer instance keeps yc_scale = 0.005.

The definition default moved 0.005 -> 0.5; an instance with no stored row would silently switch.
Run against a temporary SQLite DB built from the real models (never a live DB).
"""
import importlib.util
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

from ba2_common.core import models as M
from ba2_common.core.interfaces.ExtendableSettingsInterface import decode_setting_rows

ROOT = pathlib.Path(__file__).resolve().parents[1]
REVISION_FILE = ROOT / "alembic/versions/c4e7a9d2f1b8_pin_deterministic_scorer_yc_scale.py"


def _run(engine, fn_name="upgrade"):
    spec = importlib.util.spec_from_file_location("ds_yc_scale_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, fn_name)()


def _rows(engine):
    with engine.connect() as c:
        return {(r.instance_id, r.key): r for r in c.execute(text(
            "SELECT id, instance_id, key, value_str, value_json, value_float FROM expertsetting"))}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'live-shaped.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[
        SQLModel.metadata.tables[t] for t in ("accountdefinition", "ruleset", "expertinstance",
                                              "expertsetting")])
    with Session(engine) as s:
        s.add(M.AccountDefinition(id=1, name="acct", provider="Alpaca", description=None))
        s.add(M.ExpertInstance(id=1, account_id=1, expert="DeterministicScorer"))   # no row
        s.add(M.ExpertInstance(id=2, account_id=1, expert="DeterministicScorer"))   # explicit 0.3
        s.add(M.ExpertInstance(id=3, account_id=1, expert="DeterministicScorer"))   # empty row
        s.add(M.ExpertInstance(id=4, account_id=1, expert="FMPRating"))             # not DS
        s.add(M.ExpertInstance(id=5, account_id=1, expert="DeterministicScorer"))   # other keys only
        s.add(M.ExpertSetting(instance_id=2, key="yc_scale", value_float=0.3))
        s.add(M.ExpertSetting(instance_id=3, key="yc_scale", value_str="None"))
        s.add(M.ExpertSetting(instance_id=5, key="macro_mode", value_str="gate"))
        # A float row written the way save_settings writes one, to compare the shape against.
        s.add(M.ExpertSetting(instance_id=4, key="some_float", value_float=1.25))
        s.commit()
    return engine


def test_instances_without_the_row_are_pinned_to_the_old_default(db):
    _run(db)
    rows = _rows(db)
    for iid in (1, 5):
        r = rows[(iid, "yc_scale")]
        assert r.value_float == 0.005
        assert r.value_str is None
    assert rows[(5, "macro_mode")].value_str == "gate"


def test_the_pinned_row_has_the_shape_save_settings_writes(db):
    _run(db)
    rows = _rows(db)
    pinned, reference = rows[(1, "yc_scale")], rows[(4, "some_float")]
    assert (pinned.value_str, pinned.value_json) == (reference.value_str, reference.value_json)


def test_the_pin_resolves_through_the_real_settings_decoder(db):
    from ba2_experts.DeterministicScorer import DeterministicScorer
    defs = DeterministicScorer.get_settings_definitions()
    assert defs["yc_scale"]["default"] == 0.5      # the new default the pin protects against
    _run(db)
    with Session(db) as s:
        from sqlmodel import select
        for iid, expected in ((1, 0.005), (2, 0.3), (3, 0.005), (5, 0.005)):
            rows = s.exec(select(M.ExpertSetting).where(M.ExpertSetting.instance_id == iid)).all()
            assert decode_setting_rows(rows, defs)["yc_scale"] == expected, iid


def test_an_explicit_value_and_other_experts_are_untouched(db):
    before = _rows(db)
    _run(db)
    after = _rows(db)
    assert tuple(after[(2, "yc_scale")]) == tuple(before[(2, "yc_scale")])
    assert (4, "yc_scale") not in after
    assert tuple(after[(4, "some_float")]) == tuple(before[(4, "some_float")])


def test_an_empty_row_is_filled_not_duplicated(db):
    _run(db)
    rows = _rows(db)
    assert rows[(3, "yc_scale")].value_float == 0.005
    with db.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM expertsetting WHERE instance_id = 3 "
                           "AND key = 'yc_scale'")).scalar()
    assert n == 1


def test_a_second_run_is_a_no_op(db):
    _run(db)
    once = _rows(db)
    _run(db)
    assert {k: tuple(v) for k, v in _rows(db).items()} == {k: tuple(v) for k, v in once.items()}


def test_downgrade_is_a_documented_no_op(db):
    _run(db)
    pinned = _rows(db)
    _run(db, "downgrade")
    assert {k: tuple(v) for k, v in _rows(db).items()} == {k: tuple(v) for k, v in pinned.items()}


def test_a_db_without_the_tables_is_left_alone(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    _run(engine)


def test_the_revision_chains_onto_the_previous_head_and_keeps_a_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["c4e7a9d2f1b8"]
    assert script.get_revision("c4e7a9d2f1b8").down_revision == "b8d1f4a29c63"
