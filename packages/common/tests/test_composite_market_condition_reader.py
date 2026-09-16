"""One reader per profile behind one ``MarketConditionReader`` (Task 10: the seam went plural).

The contract this pins is what lets a condition stay ignorant of profiles: it asks the context's
reader for its own FIELD, and the reader that answers has to carry every registered profile's
fields in one row. The interesting cases are the failure ones -- a profile with no row for this
symbol/session must still ANSWER, with ``missing_session``, because an absent field is a
LookupError the condition raises on (a wiring defect) and "no row today" is not one.
"""
from datetime import date

import pytest

from ba2_common.core.market_condition_readers import (
    CompositeMarketConditionReader,
    market_condition_reader_for,
)
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_MISSING_SESSION,
    STATUS_VALID,
    FeatureRow,
    Observation,
)

SESSION = date(2025, 6, 30)


class _Reader:
    """A stand-in for one profile's window reader: the rows it was given, and nothing else."""

    def __init__(self, profile, rows):
        self.profile = profile
        self.calc_version = PROFILES[profile].calc_version
        self._rows = rows
        self.calls = 0

    def observe(self, symbol, session):
        self.calls += 1
        return self._rows.get((symbol, session))

    @property
    def mapped_reader(self):
        return f"mapped:{self.profile}"


def _row(profile, value=1.0):
    fields = [f.name for f in PROFILES[profile].fields]
    return FeatureRow(values={f: Observation(value, STATUS_VALID) for f in fields},
                      calc_versions={f: PROFILES[profile].calc_version for f in fields})


def _readers(ohlcv_rows=True, struct_rows=True):
    a = _Reader("ohlcv-v1", {("AAA", SESSION): _row("ohlcv-v1", 1.0)} if ohlcv_rows else {})
    b = _Reader("ta-structure-v1",
                {("AAA", SESSION): _row("ta-structure-v1", 2.0)} if struct_rows else {})
    return a, b


def test_one_profile_is_not_wrapped_at_all():
    """A single-profile run must be unchanged by the widening: same object, no merge, no memo."""
    a, _ = _readers()
    assert market_condition_reader_for([a]) is a


def test_the_merged_row_carries_every_field_of_every_profile():
    a, b = _readers()
    reader = market_condition_reader_for([a, b])
    assert isinstance(reader, CompositeMarketConditionReader)
    row = reader.observe("AAA", SESSION)
    expected = {f.name for p in ("ohlcv-v1", "ta-structure-v1") for f in PROFILES[p].fields}
    assert set(row.by_field()) == expected
    assert row.by_field()["underlying_adx_14"].value == 1.0
    assert row.by_field()["channel_pos_20"].value == 2.0
    assert row.calc_versions["channel_pos_20"] == PROFILES["ta-structure-v1"].calc_version


def test_a_profile_without_a_row_answers_missing_session_instead_of_vanishing():
    a, b = _readers(struct_rows=False)
    row = market_condition_reader_for([a, b]).observe("AAA", SESSION)
    obs = row.by_field()["structure_state"]
    assert obs.value is None and obs.status == STATUS_MISSING_SESSION
    assert "ta-structure-v1" in obs.reason and "AAA" in obs.reason
    # the profile that DOES have a row is unaffected
    assert row.by_field()["underlying_adx_14"].status == STATUS_VALID


def test_no_profile_with_a_row_is_None_exactly_as_a_single_reader_would_be():
    a, b = _readers(ohlcv_rows=False, struct_rows=False)
    assert market_condition_reader_for([a, b]).observe("AAA", SESSION) is None
    assert market_condition_reader_for([a, b]).observe("ZZZ", SESSION) is None


def test_the_merge_is_memoised_so_repeated_leaves_cost_one_read_each():
    a, b = _readers()
    reader = market_condition_reader_for([a, b])
    first = reader.observe("AAA", SESSION)
    for _ in range(9):
        assert reader.observe("AAA", SESSION) is first
    assert (a.calls, b.calls) == (1, 1)


def test_the_calc_version_names_every_profile_and_the_mapped_readers_stay_separate():
    a, b = _readers()
    reader = CompositeMarketConditionReader([a, b])
    assert reader.profiles == ("ohlcv-v1", "ta-structure-v1")
    assert reader.calc_versions == {"ohlcv-v1": PROFILES["ohlcv-v1"].calc_version,
                                    "ta-structure-v1": PROFILES["ta-structure-v1"].calc_version}
    for profile, version in reader.calc_versions.items():
        assert f"{profile}={version}" in reader.calc_version
    assert reader.mapped_readers == ("mapped:ohlcv-v1", "mapped:ta-structure-v1")
    # A single-profile composite reports that profile's version VERBATIM, so a run that happens
    # to be wrapped records exactly what an unwrapped one does.
    assert CompositeMarketConditionReader([a]).calc_version == PROFILES["ohlcv-v1"].calc_version


def test_a_repeated_profile_and_an_empty_list_are_refused():
    a, _ = _readers()
    with pytest.raises(ValueError, match="at least one reader"):
        CompositeMarketConditionReader([])
    with pytest.raises(ValueError, match="repeat a profile"):
        CompositeMarketConditionReader([a, _Reader("ohlcv-v1", {})])


def test_asking_a_composite_for_ONE_mapped_reader_raises_past_getattr_with_a_default():
    """Every host-side coverage check is ``getattr(reader, "mapped_reader", None)`` followed by
    "None means research mode, nothing to check". An ``AttributeError`` would be swallowed by that
    default and the check would report a clean bill of health for a run whose snapshots it never
    opened -- so this raises ``TypeError``, which ``getattr(..., default)`` does NOT suppress."""
    a, b = _readers()
    reader = CompositeMarketConditionReader([a, b])
    with pytest.raises(TypeError, match="no single mapped reader"):
        reader.mapped_reader
    with pytest.raises(TypeError):
        getattr(reader, "mapped_reader", None)      # the default must not rescue it
    message = ""
    try:
        reader.mapped_reader
    except TypeError as e:
        message = str(e)
    assert "ohlcv-v1" in message and "ta-structure-v1" in message and "mapped_readers" in message
    # the plural accessor is the one that answers
    assert reader.mapped_readers == ("mapped:ohlcv-v1", "mapped:ta-structure-v1")
