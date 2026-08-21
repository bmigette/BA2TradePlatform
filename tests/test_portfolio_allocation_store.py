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


# --- symbol weights (lazy rows, even-split defaults) -----------------------

def test_symbol_weights_default_to_an_even_split_when_no_rows_exist(account_id):
    weights = store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR", "COIN"])
    assert weights == {"TSLA": 33.33, "PLTR": 33.33, "COIN": 33.34}
    assert sum(weights.values()) == 100.0


def test_symbol_weights_split_the_remainder_among_unstored_symbols(account_id):
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=50.0)
    weights = store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR", "COIN"])
    assert weights == {"TSLA": 50.0, "PLTR": 25.0, "COIN": 25.0}


def test_symbol_weights_give_unstored_symbols_zero_when_stored_already_total_100(account_id):
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=100.0)
    weights = store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR"])
    assert weights == {"TSLA": 100.0, "PLTR": 0.0}


def test_symbol_weights_normalise_and_deduplicate_symbols(account_id):
    weights = store.get_symbol_weights(account_id, "ARK26", [" tsla ", "TSLA", "pltr"])
    assert list(weights.keys()) == ["TSLA", "PLTR"]
    assert weights == {"TSLA": 50.0, "PLTR": 50.0}


def test_symbol_weights_of_an_empty_label_are_empty(account_id):
    assert store.get_symbol_weights(account_id, "ARK26", []) == {}


def test_set_symbol_weight_stores_a_lowercase_symbol_uppercased(account_id):
    row = store.set_symbol_weight(account_id, "ARK26", " tsla ", weight_pct=60.0, comment="core")
    assert row.symbol == "TSLA"
    assert store.get_symbol_rows(account_id, "ARK26")["TSLA"].comment == "core"


def test_set_symbol_weight_rejects_a_blank_symbol(account_id):
    with pytest.raises(ValueError):
        store.set_symbol_weight(account_id, "ARK26", "", weight_pct=10.0)


def test_remove_symbol_weight_restores_the_even_split_default(account_id):
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=90.0)
    assert store.remove_symbol_weight(account_id, "ARK26", "TSLA") is True
    assert store.get_symbol_weights(account_id, "ARK26", ["TSLA", "PLTR"]) == {
        "TSLA": 50.0, "PLTR": 50.0}


def test_remove_symbol_weight_returns_false_when_no_row_exists(account_id):
    assert store.remove_symbol_weight(account_id, "ARK26", "TSLA") is False


# --- per-account allocation config (valuation mode) ------------------------

def test_get_allocation_config_creates_the_row_with_spec_defaults(account_id):
    config = store.get_allocation_config(account_id)
    assert config.valuation_mode == "cost"
    assert config.allow_fractional is False
    # Reading twice must not create a second row (account_id is unique).
    assert store.get_allocation_config(account_id).id == config.id


def test_set_valuation_mode_persists_market(account_id):
    store.set_allocation_config(account_id, valuation_mode="market")
    assert store.get_allocation_config(account_id).valuation_mode == "market"


def test_set_allocation_config_leaves_unpassed_fields_untouched(account_id):
    store.set_allocation_config(account_id, valuation_mode="market", allow_fractional=True)
    store.set_allocation_config(account_id, allow_fractional=False)
    config = store.get_allocation_config(account_id)
    assert config.valuation_mode == "market"
    assert config.allow_fractional is False


def test_set_allocation_config_rejects_an_unknown_valuation_mode(account_id):
    with pytest.raises(ValueError):
        store.set_allocation_config(account_id, valuation_mode="marketish")


def test_valuation_mode_is_scoped_per_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.set_allocation_config(account_id, valuation_mode="market")
    assert store.get_allocation_config(other.id).valuation_mode == "cost"


def test_set_allocation_config_bumps_updated_at(account_id):
    first = store.get_allocation_config(account_id).updated_at
    store.set_allocation_config(account_id, valuation_mode="market")
    assert store.get_allocation_config(account_id).updated_at >= first


# --- income ledger ---------------------------------------------------------

def test_upsert_income_event_inserts_a_new_event(account_id):
    row = store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert row.id is not None
    assert store.get_open_income_total(account_id) == 1000.0


def test_reupserting_the_same_external_id_updates_instead_of_duplicating(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1250.0)
    events = store.get_open_income_events(account_id)
    assert len(events) == 1
    assert events[0].amount == 1250.0


def test_reupserting_an_event_does_not_reset_what_was_already_consumed(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    store.consume_income(account_id, 400.0)
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.get_open_income_total(account_id) == 600.0


def test_upsert_income_event_rejects_a_blank_external_id(account_id):
    with pytest.raises(ValueError):
        store.upsert_income_event(account_id, "  ", date(2026, 8, 1), "DEPOSIT", 100.0)


def test_open_income_events_are_ordered_oldest_first(account_id):
    store.upsert_income_event(account_id, "b", date(2026, 8, 10), "DIVIDEND", 50.0, symbol="AAPL")
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 500.0)
    assert [e.external_id for e in store.get_open_income_events(account_id)] == ["a", "b"]


def test_income_events_since_excludes_older_events(account_id):
    store.upsert_income_event(account_id, "old", date(2026, 6, 1), "DEPOSIT", 100.0)
    store.upsert_income_event(account_id, "new", date(2026, 8, 15), "DEPOSIT", 200.0)
    recent = store.get_income_events_since(account_id, date(2026, 8, 1))
    assert [e.external_id for e in recent] == ["new"]


# --- FIFO consumption ------------------------------------------------------

def test_consuming_with_a_zero_or_negative_net_buy_value_consumes_nothing(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.consume_income(account_id, 0.0) == []
    # A rebalance whose sells outweigh its buys is funded by itself, not by income.
    assert store.consume_income(account_id, -250.0) == []
    assert store.get_open_income_total(account_id) == pytest.approx(1000.0)


def test_consuming_a_sub_cent_net_buy_value_writes_nothing(account_id):
    """Below the engine's MONEY_EPSILON there is nothing worth a ledger write.
    Inherited from ``consume_income_events`` -- an inline FIFO walk here would
    instead persist a 1e-7 consumption on the oldest event."""
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.consume_income(account_id, 1e-7) == []
    assert store.get_open_income_events(account_id)[0].consumed_amount == 0.0


def test_consuming_partially_leaves_a_remainder_open(account_id):
    event = store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert store.consume_income(account_id, 300.0) == [(event.id, 300.0)]
    open_events = store.get_open_income_events(account_id)
    assert len(open_events) == 1
    assert open_events[0].consumed_amount == pytest.approx(300.0)
    assert open_events[0].open_amount == pytest.approx(700.0)


def test_consuming_spends_the_oldest_event_first_then_spills_over(account_id):
    first = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    second = store.upsert_income_event(account_id, "b", date(2026, 8, 5), "DIVIDEND", 500.0)
    assert store.consume_income(account_id, 250.0) == [(first.id, 100.0), (second.id, 150.0)]
    assert store.get_open_income_total(account_id) == pytest.approx(350.0)


def test_consuming_broker_cents_leaves_the_right_remainder(account_id):
    """Real amounts are not round, so the remainder is only approximately exact."""
    a = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DIVIDEND", 10.01, symbol="AAPL")
    b = store.upsert_income_event(account_id, "b", date(2026, 8, 2), "DIVIDEND", 20.02, symbol="MSFT")
    c = store.upsert_income_event(account_id, "c", date(2026, 8, 3), "DIVIDEND", 30.03, symbol="KO")
    taken = store.consume_income(account_id, 45.0)
    assert [event_id for event_id, _ in taken] == [a.id, b.id, c.id]
    assert sum(amount for _, amount in taken) == pytest.approx(45.0)
    assert store.get_open_income_total(account_id) == pytest.approx(15.06)


def test_consuming_more_than_the_ledger_holds_empties_it_without_error(account_id):
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    consumed = store.consume_income(account_id, 9999.0)
    assert sum(amount for _, amount in consumed) == pytest.approx(100.0)
    assert store.get_open_income_total(account_id) == 0.0


def test_consuming_an_empty_ledger_returns_nothing(account_id):
    assert store.consume_income(account_id, 500.0) == []


def test_fully_consumed_events_drop_out_of_the_open_list(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.consume_income(account_id, 100.0)
    assert store.get_open_income_events(account_id) == []
    assert store.get_open_income_total(account_id) == 0.0


def test_an_event_restated_below_what_it_already_spent_is_skipped(account_id):
    """``consumed_amount > amount`` is reachable: a DIVNRA tax leg restates a
    dividend downward AFTER a run consumed the gross. ``open_amount`` clamps at 0,
    so the event must simply be skipped -- never contribute a negative take."""
    store.upsert_income_event(account_id, "div-1", date(2026, 8, 1), "DIVIDEND", 100.0, symbol="AAPL")
    store.consume_income(account_id, 100.0)
    store.upsert_income_event(account_id, "div-1", date(2026, 8, 1), "DIVIDEND", 60.0, symbol="AAPL")
    later = store.upsert_income_event(account_id, "csd-2", date(2026, 8, 2), "DEPOSIT", 500.0)

    assert store.consume_income(account_id, 200.0) == [(later.id, 200.0)]
    assert store.get_open_income_total(account_id) == pytest.approx(300.0)
    over_consumed = {e.external_id: e for e in
                     store.get_income_events_since(account_id, date(2026, 8, 1))}["div-1"]
    assert over_consumed.consumed_amount == pytest.approx(100.0)   # the TRUE spend, not clamped
    assert over_consumed.open_amount == 0.0


def test_consumption_is_scoped_to_one_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.upsert_income_event(other.id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.consume_income(account_id, 100.0)
    assert store.get_open_income_total(account_id) == 0.0
    assert store.get_open_income_total(other.id) == pytest.approx(100.0)
