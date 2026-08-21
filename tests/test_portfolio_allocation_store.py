"""Repository layer for the portfolio-allocation tables, against the in-memory test DB."""
from datetime import date

import pytest

from ba2_trade_platform.core import portfolio_allocation_store as store


@pytest.fixture
def account_id(mock_account_def):
    """The id of a persisted AccountDefinition (conftest fixture)."""
    return mock_account_def.id


# --- managed labels --------------------------------------------------------

def test_get_managed_labels_is_empty_for_a_new_account(account_id):
    assert store.get_managed_labels(account_id) == []


def test_set_managed_label_creates_then_updates_one_row(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=40.0, comment="growth")
    store.set_managed_label(account_id, "ARK26", target_pct=55.0)
    rows = store.get_managed_labels(account_id)
    assert len(rows) == 1
    assert rows[0].target_pct == 55.0
    assert rows[0].comment == "growth"


def test_set_managed_label_leaves_unpassed_fields_untouched(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=40.0, comment="growth")
    store.set_managed_label(account_id, "ARK26", comment="renamed sleeve")
    row = store.get_managed_labels(account_id)[0]
    assert row.target_pct == 40.0
    assert row.comment == "renamed sleeve"


def test_managed_labels_come_back_in_sort_order(account_id):
    store.set_managed_label(account_id, "ZULU", target_pct=10.0, sort_order=0)
    store.set_managed_label(account_id, "ARK26", target_pct=90.0, sort_order=1)
    assert [r.label for r in store.get_managed_labels(account_id)] == ["ZULU", "ARK26"]


def test_set_managed_label_rejects_a_blank_label(account_id):
    with pytest.raises(ValueError):
        store.set_managed_label(account_id, "   ", target_pct=10.0)


def test_remove_managed_label_also_removes_its_symbol_weights(account_id):
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=70.0)
    assert store.remove_managed_label(account_id, "ARK26") is True
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, "ARK26") == {}


def test_remove_managed_label_returns_false_when_not_managed(account_id):
    assert store.remove_managed_label(account_id, "NOPE") is False
