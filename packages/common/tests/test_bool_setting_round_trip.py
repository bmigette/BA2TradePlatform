"""A boolean setting must survive the round trip whatever spelling it arrives in.

THE DEFECT (parity review 2026-09-07, P1 #3). ``save_settings`` stored a bool-declared setting
as ``json.dumps(value)`` with no coercion. The GA hands its genes over as integers, so a gene
turned ON was written as the JSON string ``"1"`` -- and the reader tested
``value.lower() == 'true'``, which ``"1"`` is not. The setting came back False.

Thirteen live rows were in that state across instances 6-12: ``use_atr_stop``,
``regime_overlay_enabled`` and ``screener_weinstein_stage2_only`` silently OFF on strategies
whose winning genome had them ON. Nothing logged, nothing failed -- the value was simply
present, well-formed, and wrong.

Both ends now go through ``coerce_bool``, so 1 is stored as ``true`` and a legacy ``"1"``
already in a database reads back True. The stored rows were separately canonicalised to the
value they read as BEFORE the fix (tools/migrate_bool_settings.py), so no deployed strategy
silently changed configuration when this landed.
"""
import json

import pytest

from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool


class TestTheSpellingsThatWereBeingLost:
    @pytest.mark.parametrize("raw", [1, "1", '"1"', True, "true", "True", '"true"', "yes", "on"])
    def test_truthy_spellings_all_read_as_true(self, raw):
        """``1`` and ``"1"`` are THE defect: the optimizer's own encoding, read as False."""
        assert coerce_bool(raw) is True

    @pytest.mark.parametrize("raw", [0, "0", '"0"', False, "false", "False", '"false"', "no", "off"])
    def test_falsy_spellings_all_read_as_false(self, raw):
        assert coerce_bool(raw) is False

    def test_a_multiply_escaped_value_is_unwrapped(self):
        """Corrupted rows carry '"\\"true\\""'; the old reader unwrapped these and the new one
        must keep doing so."""
        assert coerce_bool(json.dumps(json.dumps("true"))) is True

    def test_whitespace_and_case_do_not_matter(self):
        assert coerce_bool("  TRUE  ") is True
        assert coerce_bool(" False ") is False


class TestWhatMustNotBeGuessedAt:
    """The old reader's final ``bool(value)`` turned anything non-empty into True and any
    exception into a silent False. Both are how a wrong value passes for a real one."""

    @pytest.mark.parametrize("raw", ["maybe", "", "2", 2, -1, None, [], {}, 1.5])
    def test_an_unreadable_value_raises_rather_than_defaulting(self, raw):
        with pytest.raises(ValueError):
            coerce_bool(raw)

    def test_the_error_names_the_offending_value(self):
        with pytest.raises(ValueError, match="maybe"):
            coerce_bool("maybe")


class TestTheStoredEncodingIsCanonical:
    """What save_settings writes: json.dumps(coerce_bool(value)). Pinning the stored form
    matters because it is what every OTHER reader of the column sees."""

    @pytest.mark.parametrize("raw,expected", [
        (1, "true"), ("1", "true"), (True, "true"), ("true", "true"),
        (0, "false"), ("0", "false"), (False, "false"), ("false", "false"),
    ])
    def test_the_value_written_is_a_json_boolean(self, raw, expected):
        assert json.dumps(coerce_bool(raw)) == expected

    def test_the_round_trip_is_stable(self):
        """Write, read, write again -- a migration must be idempotent."""
        once = json.dumps(coerce_bool(1))
        twice = json.dumps(coerce_bool(once))
        assert once == twice == "true"


class TestTheMigrationPinsBehaviourRatherThanChangingIt:
    """The migration writes each row back as what the OLD reader made of it, so fixing the
    reader cannot flip a deployed strategy into a configuration nothing has tested."""

    def _legacy(self, raw):
        from tools.migrate_bool_settings import _legacy_effective
        return _legacy_effective(raw)

    @pytest.mark.parametrize("raw", ['"1"', '"0"'])
    def test_the_integer_spellings_pin_to_false(self, raw):
        """Both read False under the old reader -- including '"1"', which is the whole bug --
        so both must be written as false, NOT as what the gene intended."""
        assert self._legacy(raw) is False

    @pytest.mark.parametrize("raw,expected", [('"true"', True), ('"false"', False)])
    def test_correctly_stored_values_are_unchanged(self, raw, expected):
        assert self._legacy(raw) is expected

    def test_legacy_and_fixed_readers_now_agree_on_canonical_rows(self):
        """After the migration every row is `true`/`false`, where the two readers agree --
        which is what makes the fix a no-op on existing data."""
        for canonical in (True, False):
            raw = json.dumps(canonical)
            assert self._legacy(raw) is canonical
            assert coerce_bool(raw) is canonical
