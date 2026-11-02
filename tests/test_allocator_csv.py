"""CSV export / import of the portfolio-allocator setup (core.allocator_csv)."""
import pytest

from ba2_trade_platform.core import allocator_csv, portfolio_allocation_store as store
from ba2_common.core.utils import get_symbols_by_label


@pytest.fixture
def account_id(mock_account_def):
    return mock_account_def.id


def _seed(account_id):
    store.replace_managed_labels(account_id, ["ARK", "TECH"])
    store.set_managed_label(account_id, "ARK", target_pct=60, comment="growth", color="#56B4E9")
    store.set_managed_label(account_id, "TECH", target_pct=40)
    store.add_symbols_to_label(account_id, "ARK", ["TSLA", "ROKU"])
    store.add_symbols_to_label(account_id, "TECH", ["AAPL"])
    store.set_symbol_weight(account_id, "ARK", "TSLA", weight_pct=70, comment="core")


def test_export_then_import_round_trips(account_id):
    _seed(account_id)
    text = allocator_csv.export_allocation_csv(account_id)
    assert text.splitlines()[0] == ",".join(allocator_csv.COLUMNS)
    store.replace_managed_labels(account_id, [])            # wipe the managed set
    allocator_csv.import_allocation_csv(account_id, text)
    assert [(r.label, r.target_pct, r.comment, r.color)
            for r in store.get_managed_labels(account_id)] == [
        ("ARK", 60.0, "growth", "#56B4E9"), ("TECH", 40.0, "", None)]
    rows = store.get_symbol_rows(account_id, "ARK")
    assert set(rows) == {"TSLA"} and rows["TSLA"].weight_pct == 70.0
    assert allocator_csv.export_allocation_csv(account_id) == text


def test_import_replaces_labels_and_symbols(account_id):
    _seed(account_id)
    csv_text = ("label,target_pct,label_comment,label_color,symbol,weight_pct,symbol_comment\n"
                "NEW,100,,,MSFT,100,\n")
    summary = allocator_csv.import_allocation_csv(account_id, csv_text)
    assert [r.label for r in store.get_managed_labels(account_id)] == ["NEW"]
    assert summary["labels_removed"] == 2
    assert get_symbols_by_label(["NEW"])["NEW"] == ["MSFT"]
    assert store.get_symbol_rows(account_id, "ARK") == {}


def test_import_drops_symbols_not_in_the_file_from_a_surviving_label(account_id):
    _seed(account_id)
    allocator_csv.import_allocation_csv(
        account_id,
        "label,target_pct,label_comment,label_color,symbol,weight_pct,symbol_comment\n"
        "ARK,50,,,TSLA,,\n")
    assert get_symbols_by_label(["ARK"])["ARK"] == ["TSLA"]
    assert store.get_symbol_rows(account_id, "ARK") == {}    # blank weight = no stored weight


@pytest.mark.parametrize("body,problem", [
    ("ARK,abc,,,TSLA,,\n", "not a number"),
    ("ARK,150,,,TSLA,,\n", "outside 0-100"),
    ("ARK,50,,,TSLA,,\nARK,50,,,tsla,,\n", "appears twice"),
    ("ARK,50,,,TSLA,,\nARK,40,,,AAPL,,\n", "two different target_pct"),
    (",50,,,TSLA,,\n", "empty label"),
])
def test_bad_file_is_rejected_and_changes_nothing(account_id, body, problem):
    _seed(account_id)
    before = allocator_csv.export_allocation_csv(account_id)
    with pytest.raises(ValueError, match=problem):
        allocator_csv.import_allocation_csv(
            account_id, "label,target_pct,label_comment,label_color,symbol,weight_pct,"
                        "symbol_comment\n" + body)
    assert allocator_csv.export_allocation_csv(account_id) == before


def test_missing_column_is_rejected(account_id):
    with pytest.raises(ValueError, match="missing column"):
        allocator_csv.parse_allocation_csv("label,target_pct\nARK,50\n")
