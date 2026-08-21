"""The Overview label charts remember which labels you ticked.

`resolve_growth_labels` is pure -- stored selection in, effective selection out --
so every rule is unit-tested with no browser. The two storage helpers are tested
both for a real round-trip (against a stand-in for ``app.storage.user``) and for
their RuntimeError guard: ``app.storage.user`` raises outside a UI context
(``ui/pages/symbol360.py`` documents the same constraint), and neither helper may
let that escape into the chart.
"""
from pathlib import Path

import pytest

from ba2_trade_platform.ui.utils.growth_label_storage import (
    GROWTH_LABELS_STORAGE_KEY,
    MONTHLY_PROFIT_LABELS_STORAGE_KEY,
    read_growth_labels,
    resolve_growth_labels,
    write_growth_labels,
)

OVERVIEW_PY = (Path(__file__).resolve().parents[1]
               / 'ba2_trade_platform' / 'ui' / 'pages' / 'overview.py')


# --------------------------------------------------------------------------
# resolve_growth_labels -- pure
# --------------------------------------------------------------------------

def test_no_stored_selection_falls_back_to_everything_except_auto_added():
    available = ['auto_added', 'ARK26', 'NASDAQ30']
    assert resolve_growth_labels(None, available) == ['ARK26', 'NASDAQ30']


def test_an_empty_stored_selection_is_respected_not_treated_as_missing():
    """Un-ticking every label is a real choice; it must not silently reset."""
    assert resolve_growth_labels([], ['ARK26', 'NASDAQ30']) == []


def test_a_stored_selection_is_returned_in_the_available_order():
    available = ['ARK26', 'NASDAQ30', 'HighRisk']
    assert resolve_growth_labels(['HighRisk', 'ARK26'], available) == ['ARK26', 'HighRisk']


def test_a_deleted_label_is_dropped_from_the_stored_selection():
    """A label that no longer exists must not break the chart."""
    assert resolve_growth_labels(['ARK26', 'GONE'], ['ARK26', 'NASDAQ30']) == ['ARK26']


def test_a_stored_selection_that_no_longer_matches_anything_falls_back():
    """Every stored label is gone -> show the default rather than an empty chart."""
    assert resolve_growth_labels(['GONE', 'ALSO_GONE'], ['ARK26']) == ['ARK26']


def test_with_only_auto_added_available_the_default_shows_it_rather_than_nothing():
    assert resolve_growth_labels(None, ['auto_added']) == ['auto_added']


def test_no_available_labels_at_all_yields_an_empty_selection():
    assert resolve_growth_labels(None, []) == []
    assert resolve_growth_labels(['ARK26'], []) == []


def test_auto_added_is_kept_when_the_user_deliberately_ticked_it():
    """The exclusion only shapes the DEFAULT, never an explicit choice."""
    assert resolve_growth_labels(['auto_added'], ['auto_added', 'ARK26']) == ['auto_added']


def test_blank_labels_are_ignored_on_both_sides():
    assert resolve_growth_labels(None, ['ARK26', '', None]) == ['ARK26']
    assert resolve_growth_labels(['', 'ARK26'], ['ARK26', 'NASDAQ30']) == ['ARK26']


# --------------------------------------------------------------------------
# read/write -- the RuntimeError guard (the failure mode that bites in prod)
# --------------------------------------------------------------------------

def test_read_growth_labels_outside_a_ui_context_returns_none_instead_of_raising():
    """app.storage.user raises RuntimeError with no client; the chart must still draw."""
    assert read_growth_labels() is None


def test_write_growth_labels_outside_a_ui_context_does_not_raise():
    write_growth_labels(['ARK26'])   # must be a silent, logged no-op


def test_read_outside_a_ui_context_is_none_for_the_monthly_key_too():
    assert read_growth_labels(MONTHLY_PROFIT_LABELS_STORAGE_KEY) is None


def test_the_storage_key_is_the_documented_one():
    assert GROWTH_LABELS_STORAGE_KEY == 'overview_growth_labels'


def test_the_two_charts_do_not_share_one_key():
    """Two independent selectors -- ticking a label in one must not move the other."""
    assert MONTHLY_PROFIT_LABELS_STORAGE_KEY != GROWTH_LABELS_STORAGE_KEY


# --------------------------------------------------------------------------
# read/write -- the real round-trip, against a stand-in user store
# --------------------------------------------------------------------------

class _FakeStorage:
    """Stands in for nicegui's Storage: only ``.user`` is ever touched."""

    def __init__(self, user=None):
        self.user = {} if user is None else user


@pytest.fixture
def user_storage(monkeypatch):
    """Give app.storage.user a plain dict so the helpers can actually persist."""
    from nicegui import app
    fake = _FakeStorage()
    monkeypatch.setattr(app, 'storage', fake)
    return fake.user


def test_a_written_selection_reads_back(user_storage):
    write_growth_labels(['ARK26', 'NASDAQ30'])
    assert user_storage[GROWTH_LABELS_STORAGE_KEY] == ['ARK26', 'NASDAQ30']
    assert read_growth_labels() == ['ARK26', 'NASDAQ30']


def test_an_empty_written_selection_reads_back_as_empty_not_missing(user_storage):
    """[] must survive the round-trip, otherwise un-ticking everything resets."""
    write_growth_labels([])
    assert read_growth_labels() == []
    assert resolve_growth_labels(read_growth_labels(), ['ARK26']) == []


def test_nothing_stored_reads_as_none(user_storage):
    assert read_growth_labels() is None


def test_the_two_keys_are_stored_independently(user_storage):
    write_growth_labels(['ARK26'], MONTHLY_PROFIT_LABELS_STORAGE_KEY)
    write_growth_labels(['NASDAQ30'])
    assert read_growth_labels(MONTHLY_PROFIT_LABELS_STORAGE_KEY) == ['ARK26']
    assert read_growth_labels() == ['NASDAQ30']


def test_a_corrupt_stored_value_does_not_raise(user_storage):
    """Hand-edited / stale session data must degrade to the default, not 500."""
    user_storage[GROWTH_LABELS_STORAGE_KEY] = 'ARK26'   # a str, not a list
    assert read_growth_labels() is None


# --------------------------------------------------------------------------
# The two label-normalisation bypasses this task also had to close
# --------------------------------------------------------------------------

def test_neither_label_chart_builds_its_own_unnormalised_symbol_map():
    """Task 1 normalised symbols in get_labels_by_symbol; two charts bypassed it.

    Both built ``symbol_labels[inst.name] = inst.labels`` straight off Instrument
    rows and looked up with the raw ``pos.symbol``, so a lowercase row or a padded
    position symbol silently lost its labels. Guarding the source text because the
    real call needs a DB and a UI context.
    """
    src = OVERVIEW_PY.read_text(encoding='utf-8')
    assert 'symbol_labels[inst.name]' not in src


def test_both_multi_select_label_charts_are_wired_to_the_store():
    """Guards the wiring the plan called eyeball-only.

    A refactor that drops the write() call still renders a perfectly good chart --
    it just silently stops remembering. Both charts must resolve their default
    through the store and write back on rebuild, and neither may keep the old
    hard-coded ``[l for l in ... if l != 'auto_added']`` default.
    """
    src = OVERVIEW_PY.read_text(encoding='utf-8')
    for key in (GROWTH_LABELS_STORAGE_KEY, MONTHLY_PROFIT_LABELS_STORAGE_KEY):
        const = ('GROWTH_LABELS_STORAGE_KEY' if key == GROWTH_LABELS_STORAGE_KEY
                 else 'MONTHLY_PROFIT_LABELS_STORAGE_KEY')
        assert f'read_growth_labels({const})' in src, f'{const} default not restored'
        assert f'write_growth_labels(visible, {const})' in src, f'{const} never persisted'
    assert src.count('resolve_growth_labels(') == 2
    # The single-select "positions within a label" chart keeps its own default and
    # is not part of this task, so only the two MULTI-selects lose the old literal.
    assert "[l for l in labels if l != 'auto_added']" not in src
    assert "[l for l in all_labels if l != 'auto_added']" not in src
