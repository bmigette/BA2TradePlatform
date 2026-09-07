"""END TO END through the real save/load path: an optimizer's integer 1 must come back True.

The unit coverage of the coercion itself lives in
``packages/common/tests/test_bool_setting_round_trip.py``. This file exists because the defect
was never in the coercion -- there wasn't one -- it was in the seam between two halves that
disagreed, and only a real DB write followed by a real DB read can show that they now agree:

  * the WRITER resolves a setting's type from ``get_settings_definitions``, not from the Python
    value handed to it -- so a bool-declared key given the GA's integer ``1`` takes the bool
    branch and used to be stored as ``json.dumps(1)`` == the string ``"1"``
  * the READER also resolves from the definitions, and tested ``value.lower() == 'true'``

``"1"`` is not ``"true"``, so the value came back False. Thirteen live rows were in exactly that
state. The reader fix is only half the repair; if the writer still emitted ``"1"`` then every
NEW optimizer run would keep planting rows that only the fixed reader could interpret.

The declared-type precedence is asserted directly, because it is the thing that makes the fix
reach the GA at all: the backtest handler passes ``_setting_type(value)``, which reports "int"
for an integer gene, and that must NOT win over the "bool" declaration.
"""
import json

import pytest
from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import ExpertSetting
from tests.conftest import MockExpert
from tests.factories import create_account_definition, create_expert_instance


BOOL_KEY = "allow_automated_trade_opening"   # a builtin bool every expert declares


@pytest.fixture
def expert():
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="MockExpert")
    return MockExpert(inst.id)


def _stored(expert, key):
    with get_db() as session:
        row = session.exec(
            select(ExpertSetting).where(ExpertSetting.instance_id == expert.id,
                                        ExpertSetting.key == key)
        ).first()
        return None if row is None else row.value_json


class TestTheOptimizersIntegerSurvivesTheRoundTrip:
    def test_an_integer_one_reads_back_as_True(self, expert):
        """THE DEFECT: this returned False, and that is how use_atr_stop,
        regime_overlay_enabled and screener_weinstein_stage2_only came to be off on live
        strategies selected with them on."""
        expert.save_settings({BOOL_KEY: (1, "int")})
        assert expert.settings[BOOL_KEY] is True

    def test_an_integer_zero_reads_back_as_False(self, expert):
        expert.save_settings({BOOL_KEY: (0, "int")})
        assert expert.settings[BOOL_KEY] is False

    def test_a_real_bool_still_round_trips(self, expert):
        """The inverse: the UI path, which was never broken, must not move."""
        expert.save_settings({BOOL_KEY: (True, "bool")})
        assert expert.settings[BOOL_KEY] is True
        expert.save_settings({BOOL_KEY: (False, "bool")})
        assert expert.settings[BOOL_KEY] is False

    def test_a_none_type_hint_resolves_from_the_declaration(self, expert):
        """import_deploy_payload passes (value, None) for everything."""
        expert.save_settings({BOOL_KEY: (1, None)})
        assert expert.settings[BOOL_KEY] is True


class TestWhatIsActuallyStored:
    """The column is read by more than the settings reader (migrations, audits, the parity
    script), so the canonical encoding is part of the contract."""

    def test_an_integer_gene_is_stored_as_a_json_boolean(self, expert):
        expert.save_settings({BOOL_KEY: (1, "int")})
        assert _stored(expert, BOOL_KEY) in (True, "true")

    def test_the_string_one_is_never_written_again(self, expert):
        expert.save_settings({BOOL_KEY: (1, "int")})
        assert _stored(expert, BOOL_KEY) != json.dumps("1")


class TestLegacyRowsAlreadyInTheDatabase:
    def test_a_stored_string_one_now_reads_as_True(self, expert):
        """Rows written before the fix. Production's were migrated to the value they read as
        BEFORE the fix (deliberately -- see tools/migrate_bool_settings.py), but the reader
        must still cope with any that survive elsewhere, e.g. a retained trial DB."""
        expert.save_settings({BOOL_KEY: (True, "bool")})
        with get_db() as session:
            row = session.exec(
                select(ExpertSetting).where(ExpertSetting.instance_id == expert.id,
                                            ExpertSetting.key == BOOL_KEY)
            ).first()
            row.value_json = json.dumps("1")
            session.add(row)
            session.commit()
        assert MockExpert(expert.id).settings[BOOL_KEY] is True

    def test_an_unreadable_row_falls_back_to_False_without_raising(self, expert):
        """One corrupt row must not take an expert down -- but it is logged now, where the old
        handler swallowed every "1" in the database in silence."""
        expert.save_settings({BOOL_KEY: (True, "bool")})
        with get_db() as session:
            row = session.exec(
                select(ExpertSetting).where(ExpertSetting.instance_id == expert.id,
                                            ExpertSetting.key == BOOL_KEY)
            ).first()
            row.value_json = json.dumps("maybe")
            session.add(row)
            session.commit()
        assert MockExpert(expert.id).settings[BOOL_KEY] is False


def test_the_declaration_beats_the_callers_type_hint(expert):
    """The load-bearing precedence. daily_backtest_handler._build_experts passes
    ``_setting_type(value)``, which returns "int" for an integer gene; if that won over the
    "bool" declaration the value would land in value_float and the reader -- which resolves
    from the declaration -- would find nothing in value_json."""
    expert.save_settings({BOOL_KEY: (1, "int")})
    with get_db() as session:
        row = session.exec(
            select(ExpertSetting).where(ExpertSetting.instance_id == expert.id,
                                        ExpertSetting.key == BOOL_KEY)
        ).first()
        assert row.value_json is not None, "must be stored as a bool, not as value_float"
        assert row.value_float is None
