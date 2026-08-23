"""Repository layer for the portfolio-allocation tables, against the in-memory test DB."""
import threading
from datetime import date

import pytest

from ba2_trade_platform.core import portfolio_allocation_store as store


@pytest.fixture
def account_id(mock_account_def):
    """The id of a persisted AccountDefinition (conftest fixture)."""
    return mock_account_def.id


@pytest.fixture
def file_backed_db(tmp_path):
    """Repoint the store at a REAL, production-configured sqlite FILE.

    The concurrency tests below cannot run on the session-wide in-memory engine
    from conftest: ``create_engine("sqlite:///:memory:")`` gets SQLAlchemy's
    ``SingletonThreadPool``, so a second thread opens a second connection to a
    second, EMPTY in-memory database and the race under test cannot even be
    expressed. Worse, it would pass for the wrong reason.

    So the engine here is built by ``ba2_common.core.db._build_engine`` -- the
    very function the live app uses -- which is what puts the assertions on the
    real pragmas (``journal_mode=WAL``, ``busy_timeout=30000``) rather than on a
    hand-rolled approximation of them. The autouse ``patch_db_engine`` fixture has
    already installed the in-memory engine by the time this runs, so this saves
    and restores whatever it finds.
    """
    import ba2_common.core.db as pkg_db
    from sqlmodel import SQLModel

    engine = pkg_db._build_engine(str(tmp_path / "allocation_race.sqlite"))
    SQLModel.metadata.create_all(engine)
    saved = pkg_db._engine
    pkg_db._engine = engine
    try:
        yield engine
    finally:
        pkg_db._engine = saved
        engine.dispose()


def consume(account_id, net_buy_value, *, sell_value=0.0):
    """Spend the income ledger the ONLY way the store allows: by finalising a run.

    There is deliberately no ``consume_income(account_id, amount)`` any more --
    money is spent on behalf of a run, once, so every consumption test has to go
    through one. A "negative net buy value" is expressed the way the real thing
    produces it: a run whose sells outweigh its buys.

    Returns the ``[(income_event_id, amount)]`` the run actually took.
    """
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=net_buy_value, filled_sell_value=sell_value,
        order_ids=[])
    return [tuple(pair) for pair in finalised.income_consumed_events]


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


@pytest.mark.parametrize("count", [2, 3, 6, 7, 11, 15])
def test_default_symbol_weights_are_bit_for_bit_the_engines_own_split(account_id, count):
    """The defaults the PAGE shows must equal the ones the ENGINE would compute.

    ``_split_evenly`` exists to delegate to ``even_split_pct`` rather than
    re-derive the split, and this is what holds it to that. A hand-rolled
    ``round(100 / n, 2)`` passes at n=2 and n=3 -- where the two happen to agree,
    and where every other test in this file lives -- and then disagrees from n=6
    on: 16.67 against the engine's 16.66. The page would offer weights the engine
    immediately recomputes differently, and the drift is invisible until a plan
    comes back with numbers nobody typed.

    Compared against ``build_symbol_targets`` rather than a hard-coded list on
    purpose: a literal here would just be a second place to get the split wrong.
    """
    from ba2_common.core.portfolio_allocation import build_symbol_targets

    symbols = [f"SYM{i}" for i in range(count)]
    assert store.get_symbol_weights(account_id, "ARK26", symbols) == {
        target.symbol: target.weight_pct for target in build_symbol_targets(symbols)}


def test_set_symbol_weight_stores_a_lowercase_symbol_uppercased(account_id):
    row = store.set_symbol_weight(account_id, "ARK26", " tsla ", weight_pct=60.0, comment="core")
    assert row.symbol == "TSLA"
    assert store.get_symbol_rows(account_id, "ARK26")["TSLA"].comment == "core"


def test_set_symbol_weight_leaves_unpassed_fields_untouched(account_id):
    """A comment-only write must not zero the weight. This bug happened once.

    ``None`` means LEAVE UNCHANGED, and the difference between that and
    ``float(weight_pct or 0.0)`` is silent: the row still exists, the comment
    saves, and the weight is now 0. The engine reads 0 as "hold none of this",
    so the next rebalance SELLS THE POSITION TO ZERO. Commit c63d34c exists
    because it was found by hand; the label-side equivalent is pinned by
    ``test_set_managed_label_leaves_unpassed_fields_untouched`` and the symbol
    side was not.
    """
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=70.0, comment="core")
    store.set_symbol_weight(account_id, "ARK26", "TSLA", comment="trimming into strength")
    row = store.get_symbol_rows(account_id, "ARK26")["TSLA"]
    assert row.weight_pct == 70.0
    assert row.comment == "trimming into strength"


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
    # MARKET, not cost (W1): the requirement is to allocate by VALUE, and cost mode
    # understates the allocatable base by the whole unrealised P&L.
    config = store.get_allocation_config(account_id)
    assert config.valuation_mode == "market"
    assert config.allow_fractional is True
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


def test_set_allocation_config_leaves_the_fractional_choice_untouched(account_id):
    """The mirror of the test above: a mode-only write must not reset the switch
    back to its default. The guard is ``is not None``, not truthiness."""
    store.set_allocation_config(account_id, valuation_mode="market", allow_fractional=False)
    store.set_allocation_config(account_id, valuation_mode="cost")
    config = store.get_allocation_config(account_id)
    assert config.valuation_mode == "cost"
    assert config.allow_fractional is False


def test_set_allocation_config_can_turn_fractional_back_off(account_id):
    """False must be storable even though it is falsy -- the guard is `is not None`."""
    store.set_allocation_config(account_id, allow_fractional=False)
    assert store.get_allocation_config(account_id).allow_fractional is False


def test_set_allocation_config_rejects_an_unknown_valuation_mode(account_id):
    with pytest.raises(ValueError):
        store.set_allocation_config(account_id, valuation_mode="marketish")


def test_valuation_mode_is_scoped_per_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.set_allocation_config(account_id, valuation_mode="cost")
    assert store.get_allocation_config(other.id).valuation_mode == "market"


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
    consume(account_id, 400.0)
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
    assert consume(account_id, 0.0) == []
    # A rebalance whose sells outweigh its buys is funded by itself, not by income.
    assert consume(account_id, 750.0, sell_value=1000.0) == []
    assert store.get_open_income_total(account_id) == pytest.approx(1000.0)


def test_consuming_a_sub_cent_net_buy_value_writes_nothing(account_id):
    """Below the engine's MONEY_EPSILON there is nothing worth a ledger write.
    Inherited from ``consume_income_events`` -- an inline FIFO walk here would
    instead persist a 1e-7 consumption on the oldest event."""
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert consume(account_id, 1e-7) == []
    assert store.get_open_income_events(account_id)[0].consumed_amount == 0.0


def test_consuming_partially_leaves_a_remainder_open(account_id):
    event = store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    assert consume(account_id, 300.0) == [(event.id, 300.0)]
    open_events = store.get_open_income_events(account_id)
    assert len(open_events) == 1
    assert open_events[0].consumed_amount == pytest.approx(300.0)
    assert open_events[0].open_amount == pytest.approx(700.0)


def test_consuming_spends_the_oldest_event_first_then_spills_over(account_id):
    first = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    second = store.upsert_income_event(account_id, "b", date(2026, 8, 5), "DIVIDEND", 500.0)
    assert consume(account_id, 250.0) == [(first.id, 100.0), (second.id, 150.0)]
    assert store.get_open_income_total(account_id) == pytest.approx(350.0)


def test_consuming_broker_cents_leaves_the_right_remainder(account_id):
    """Real amounts are not round, so the remainder is only approximately exact."""
    a = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DIVIDEND", 10.01, symbol="AAPL")
    b = store.upsert_income_event(account_id, "b", date(2026, 8, 2), "DIVIDEND", 20.02, symbol="MSFT")
    c = store.upsert_income_event(account_id, "c", date(2026, 8, 3), "DIVIDEND", 30.03, symbol="KO")
    taken = consume(account_id, 45.0)
    assert [event_id for event_id, _ in taken] == [a.id, b.id, c.id]
    assert sum(amount for _, amount in taken) == pytest.approx(45.0)
    assert store.get_open_income_total(account_id) == pytest.approx(15.06)


def test_consuming_more_than_the_ledger_holds_empties_it_without_error(account_id):
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    consumed = consume(account_id, 9999.0)
    assert sum(amount for _, amount in consumed) == pytest.approx(100.0)
    assert store.get_open_income_total(account_id) == 0.0


def test_consuming_an_empty_ledger_returns_nothing(account_id):
    assert consume(account_id, 500.0) == []


def test_fully_consumed_events_drop_out_of_the_open_list(account_id):
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 100.0)
    consume(account_id, 100.0)
    assert store.get_open_income_events(account_id) == []
    assert store.get_open_income_total(account_id) == 0.0


def test_an_event_restated_below_what_it_already_spent_is_skipped(account_id):
    """``consumed_amount > amount`` is reachable: a DIVNRA tax leg restates a
    dividend downward AFTER a run consumed the gross. ``open_amount`` clamps at 0,
    so the event must simply be skipped -- never contribute a negative take."""
    store.upsert_income_event(account_id, "div-1", date(2026, 8, 1), "DIVIDEND", 100.0, symbol="AAPL")
    consume(account_id, 100.0)
    store.upsert_income_event(account_id, "div-1", date(2026, 8, 1), "DIVIDEND", 60.0, symbol="AAPL")
    later = store.upsert_income_event(account_id, "csd-2", date(2026, 8, 2), "DEPOSIT", 500.0)

    assert consume(account_id, 200.0) == [(later.id, 200.0)]
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
    consume(account_id, 100.0)
    assert store.get_open_income_total(account_id) == 0.0
    assert store.get_open_income_total(other.id) == pytest.approx(100.0)


def test_two_runs_each_consume_their_own_share_oldest_first(account_id):
    """Consecutive runs walk the same FIFO queue; the second picks up the remainder."""
    first = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 300.0)
    second = store.upsert_income_event(account_id, "b", date(2026, 8, 5), "DIVIDEND", 500.0)
    assert consume(account_id, 200.0) == [(first.id, 200.0)]
    assert consume(account_id, 400.0) == [(first.id, 100.0), (second.id, 300.0)]
    assert store.get_open_income_total(account_id) == pytest.approx(200.0)


def test_the_store_exposes_no_account_level_consume_entry_point():
    """Money is spent on behalf of a RUN, so there is nothing to call without one.

    The removed ``consume_income(account_id, net_buy_value)`` was replayable by
    construction: nothing in it recorded which run had already spent. If it comes
    back, this fails.
    """
    assert not hasattr(store, "consume_income")
    assert not hasattr(store, "update_allocation_run_totals")


# --- run audit -------------------------------------------------------------

def test_record_allocation_run_persists_the_plan_and_order_ids(account_id):
    run = store.record_allocation_run(
        account_id, "REBALANCE", {"rows": [{"symbol": "TSLA"}], "scale_factor": 0.61},
        base_notional=50_000.0, available_buying_power=20_000.0, allow_fractional=True,
        filled_buy_value=8000.0, filled_sell_value=3000.0, order_ids=[7, 8])
    assert run.id is not None
    stored = store.get_recent_runs(account_id)[0]
    assert stored.mode == "REBALANCE"
    assert stored.plan_json["scale_factor"] == 0.61
    assert stored.order_ids == [7, 8]
    assert stored.allow_fractional is True
    assert stored.net_buy_value == 5000.0


def test_recent_runs_are_newest_first_and_respect_the_limit(account_id):
    for i in range(3):
        store.record_allocation_run(account_id, "INVEST_LABEL", {}, scope_label=f"L{i}")
    runs = store.get_recent_runs(account_id, limit=2)
    assert [r.scope_label for r in runs] == ["L2", "L1"]


def test_recent_runs_is_empty_for_an_account_that_never_ran(account_id):
    assert store.get_recent_runs(account_id) == []


def test_finalise_allocation_run_writes_back_what_was_actually_submitted(account_id):
    """The run row is created BEFORE submission so its id can be stamped into every
    order comment, then finalised with the real totals afterwards."""
    run = store.record_allocation_run(account_id, "REBALANCE", {"rows": []},
                                      base_notional=10_000.0)
    updated = store.finalise_allocation_run(
        run.id, filled_buy_value=1600.0, filled_sell_value=400.0, order_ids=[101, 102])
    assert updated.filled_buy_value == 1600.0
    assert updated.filled_sell_value == 400.0
    assert updated.order_ids == [101, 102]
    assert updated.net_buy_value == 1200.0
    assert store.get_recent_runs(account_id)[0].order_ids == [101, 102]


def test_a_run_funded_entirely_by_its_own_sells_has_no_net_buy_value(account_id):
    """``net_buy_value`` clamps at 0 so such a rebalance consumes NO income."""
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=4000.0, filled_sell_value=9000.0, order_ids=[])
    assert finalised.net_buy_value == 0.0
    assert finalised.income_consumed_events == []
    assert finalised.income_consumed_amount == 0.0
    assert store.get_open_income_total(account_id) == pytest.approx(1000.0)
    # It still counts as consumed: the income step RAN and correctly took nothing,
    # which is why it must not show up as unfinished work.
    assert finalised.is_income_consumed is True
    assert store.get_unconsumed_runs(account_id) == []


def test_finalise_allocation_run_rejects_a_missing_total(account_id):
    """A None total would silently understate net_buy_value and under-consume the
    ledger, so it must raise HERE -- not deep inside ``float(None)``."""
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    with pytest.raises(ValueError, match="both totals"):
        store.finalise_allocation_run(
            run.id, filled_buy_value=None, filled_sell_value=0.0, order_ids=[])
    # The refusal is total: nothing was stamped, so the run is still recoverable.
    assert [r.id for r in store.get_unconsumed_runs(account_id)] == [run.id]


def test_finalise_allocation_run_raises_when_the_run_is_gone():
    from ba2_common.core.db import InstanceNotFound
    with pytest.raises(InstanceNotFound):
        store.finalise_allocation_run(
            999_999, filled_buy_value=1.0, filled_sell_value=0.0, order_ids=[])


def test_a_run_row_written_by_raw_sql_reads_back_with_null_json(account_id):
    """plan_json/order_ids are nullable JSON with PYTHON-side defaults, so a row
    that did not go through this module lands NULL, not {}/[]. Reads must cope.

    ``income_consumed_at`` is nullable for the same reason it is the guard: a row
    nobody stamped has NOT consumed income, and a NULL JSON breakdown must read as
    0.0 rather than blowing up."""
    from sqlalchemy import text
    from ba2_common.core.db import get_db
    with get_db() as session:
        session.exec(text(
            "INSERT INTO portfolio_allocation_run "
            "(account_id, mode, base_notional, available_buying_power, allow_fractional, "
            " filled_buy_value, filled_sell_value, created_at) "
            "VALUES (:aid, 'REBALANCE', 0, 0, 0, 0, 0, '2026-08-20 00:00:00')"
        ), params={"aid": account_id})
        session.commit()
    row = store.get_recent_runs(account_id)[0]
    assert row.plan_json is None
    assert row.order_ids is None
    assert row.net_buy_value == 0.0
    assert row.income_consumed_events is None
    assert row.is_income_consumed is False
    assert row.income_consumed_amount == 0.0


# --- income consumption is idempotent per run ------------------------------

def test_finalising_the_same_run_twice_consumes_the_ledger_once(account_id):
    """The replay guard. A service-layer retry re-states the totals but must NOT
    spend the ledger a second time -- that is duplicated real money."""
    event = store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "INVEST_LABEL", {}, scope_label="ARK26")

    first = store.finalise_allocation_run(
        run.id, filled_buy_value=1600.0, filled_sell_value=0.0, order_ids=[101])
    second = store.finalise_allocation_run(
        run.id, filled_buy_value=1600.0, filled_sell_value=0.0, order_ids=[101])

    assert first.income_consumed_amount == pytest.approx(1600.0)
    # Same answer both times, and the ledger only moved once.
    assert second.income_consumed_amount == pytest.approx(1600.0)
    assert second.income_consumed_events == [[event.id, 1600.0]]
    assert second.income_consumed_at == first.income_consumed_at
    assert store.get_open_income_total(account_id) == pytest.approx(3400.0)


def test_a_replayed_run_restates_its_totals_without_respending(account_id):
    """A retry that submitted more on the second pass still consumes only once.

    Restating the money that went out is harmless and useful; taking the ledger
    again is the bug. The recorded consumption stays the one that happened.
    """
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    store.finalise_allocation_run(run.id, filled_buy_value=1000.0,
                                  filled_sell_value=0.0, order_ids=[1])

    replayed = store.finalise_allocation_run(
        run.id, filled_buy_value=2500.0, filled_sell_value=0.0, order_ids=[1, 2])

    assert replayed.filled_buy_value == pytest.approx(2500.0)
    assert replayed.order_ids == [1, 2]
    assert replayed.income_consumed_amount == pytest.approx(1000.0)
    assert store.get_open_income_total(account_id) == pytest.approx(4000.0)


def test_a_crashed_run_is_visibly_unconsumed_and_can_be_recovered(account_id):
    """Crash recovery. A submit that died before finalising leaves the ledger
    untouched AND the run un-stamped, so the money it spent at the broker is
    findable instead of being silently re-allocated by the next run."""
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    crashed = store.record_allocation_run(account_id, "REBALANCE", {})
    # ... orders went out here, then the process died before finalise_allocation_run.

    assert [r.id for r in store.get_unconsumed_runs(account_id)] == [crashed.id]
    assert store.get_recent_runs(account_id)[0].is_income_consumed is False
    assert store.get_open_income_total(account_id) == pytest.approx(5000.0)

    recovered = store.finalise_allocation_run(
        crashed.id, filled_buy_value=1600.0, filled_sell_value=0.0, order_ids=[101])

    assert recovered.income_consumed_amount == pytest.approx(1600.0)
    assert store.get_open_income_total(account_id) == pytest.approx(3400.0)
    assert store.get_unconsumed_runs(account_id) == []


def test_a_failed_consumption_takes_the_totals_down_with_it(account_id, monkeypatch):
    """One transaction, or the crash window the design claims to have closed reopens.

    If the totals were committed BEFORE the ledger step, a failure between the
    two would leave a run carrying real submitted values and a NULL stamp --
    which reads as "died mid-submit" and invites a recovery pass to consume
    against totals whose orders may never have gone out. Worse, a run that looks
    finalised is one nobody re-checks.

    So the failure has to be total: no totals, no order ids, no stamp, and the
    run still standing in ``get_unconsumed_runs()`` where a human will find it.
    Inserting a ``session.commit()`` between the totals write and the
    consumption is exactly the mutation this kills.
    """
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    def explode(session, aid, net_buy_value):
        raise RuntimeError("ledger unreachable mid-finalise")

    monkeypatch.setattr(store, "_apply_income_consumption", explode)
    with pytest.raises(RuntimeError, match="ledger unreachable"):
        store.finalise_allocation_run(
            run.id, filled_buy_value=1600.0, filled_sell_value=200.0,
            order_ids=[101, 102])

    stored = store.get_recent_runs(account_id)[0]
    assert stored.id == run.id
    assert stored.filled_buy_value == 0.0
    assert stored.filled_sell_value == 0.0
    assert stored.order_ids == []
    assert stored.is_income_consumed is False
    assert [r.id for r in store.get_unconsumed_runs(account_id)] == [run.id]
    assert store.get_open_income_total(account_id) == pytest.approx(5000.0)


def test_unconsumed_runs_are_scoped_to_one_account_and_newest_first(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    first = store.record_allocation_run(account_id, "REBALANCE", {}, scope_label="A")
    second = store.record_allocation_run(account_id, "REBALANCE", {}, scope_label="B")
    store.record_allocation_run(other.id, "REBALANCE", {}, scope_label="C")
    store.finalise_allocation_run(first.id, filled_buy_value=0.0,
                                  filled_sell_value=0.0, order_ids=[])

    assert [r.id for r in store.get_unconsumed_runs(account_id)] == [second.id]
    assert [r.scope_label for r in store.get_unconsumed_runs(other.id)] == ["C"]


def test_the_consumption_breakdown_says_which_events_a_run_spent(account_id):
    """Per-run attribution: which income paid for this run, and how much of each.

    This is what a run id on a consumption row would have bought, without a
    sixth table -- and consumption is many-to-many (one run spans several events,
    one event is split across runs), so a scalar run id on the event could not
    have expressed it anyway.
    """
    first = store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    second = store.upsert_income_event(account_id, "b", date(2026, 8, 5), "DIVIDEND", 500.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=250.0, filled_sell_value=0.0, order_ids=[])

    assert finalised.income_consumed_events == [[first.id, 100.0], [second.id, 150.0]]
    assert finalised.income_consumed_amount == pytest.approx(250.0)
    assert store.get_recent_runs(account_id)[0].income_consumed_amount == pytest.approx(250.0)


def test_a_run_consuming_more_than_the_ledger_holds_is_still_stamped(account_id):
    """A shortfall is not an error -- buying power is the constraint, not the
    ledger -- but the run must still be marked consumed, or a recovery pass would
    keep trying to spend income that was never there."""
    store.upsert_income_event(account_id, "a", date(2026, 8, 1), "DEPOSIT", 100.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=9999.0, filled_sell_value=0.0, order_ids=[])

    assert finalised.income_consumed_amount == pytest.approx(100.0)
    assert finalised.is_income_consumed is True
    assert store.get_unconsumed_runs(account_id) == []


# --- account deletion cleanup ---------------------------------------------

def test_deleting_account_allocation_data_removes_every_table_row(account_id):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import AccountDefinition
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_symbol_weight(account_id, "ARK26", "TSLA", weight_pct=100.0)
    store.upsert_income_event(account_id, "csd-1", date(2026, 8, 1), "DEPOSIT", 100.0)
    store.record_allocation_run(account_id, "REBALANCE", {})
    store.set_allocation_config(account_id, valuation_mode="market")
    counts = store.delete_account_allocation_data(account_id)
    assert counts == {"config": 1, "labels": 1, "symbols": 1, "income_events": 1, "runs": 1}
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, "ARK26") == {}
    assert store.get_open_income_events(account_id) == []
    assert store.get_recent_runs(account_id) == []
    # The config row is gone, so the next read recreates it with the defaults.
    assert store.get_allocation_config(account_id).valuation_mode == "market"
    # The parent AccountDefinition is still here: no FK cascade can have done ANY
    # of the work above, because a cascade only fires on a parent delete.
    assert get_instance(AccountDefinition, account_id).id == account_id


def test_deleting_allocation_data_leaves_other_accounts_alone(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_managed_label(other.id, "ARK26", target_pct=100.0)
    store.delete_account_allocation_data(account_id)
    assert [r.label for r in store.get_managed_labels(other.id)] == ["ARK26"]


def test_the_declared_cascade_never_fires_so_cleanup_must_be_explicit(account_id):
    """Why this helper has to exist.

    Every allocation table declares ``ondelete="CASCADE"`` on ``account_id``, but
    SQLite enforces foreign keys only under ``PRAGMA foreign_keys = ON`` and
    NOTHING turns it on: ``ba2_common.core.db._build_engine`` sets journal_mode,
    synchronous and busy_timeout only, and the test engine sets no pragmas at all,
    so both run at SQLite's default of OFF. Deleting the parent AccountDefinition
    therefore ORPHANS the allocation rows rather than removing them -- this test
    pins that, so the day someone enables enforcement it fails loudly instead of
    letting the cascade quietly stand in for the explicit delete.
    """
    from ba2_common.core.db import delete_instance, get_instance
    from ba2_common.core.models import AccountDefinition
    store.set_managed_label(account_id, "ARK26", target_pct=100.0)
    store.set_allocation_config(account_id, valuation_mode="market")
    delete_instance(get_instance(AccountDefinition, account_id))
    # The cascade did NOT fire: the rows outlived the account they belong to.
    assert [r.label for r in store.get_managed_labels(account_id)] == ["ARK26"]
    counts = store.delete_account_allocation_data(account_id)
    assert counts == {"config": 1, "labels": 1, "symbols": 0, "income_events": 0, "runs": 0}
    assert store.get_managed_labels(account_id) == []


# --- concurrent finalisation must not double-spend the ledger --------------
#
# These run against ``file_backed_db`` (a real sqlite file built by the live
# ``_build_engine``), NOT the in-memory conftest engine -- see that fixture for
# why the in-memory one cannot express the race at all.

RACE_TRIALS = 25
"""Trials per race test. The window is timing-dependent, so one trial proves
nothing either way; the assertion is that ALL of them come out right. Before the
fix roughly four in five went wrong, which makes 25 trials a certainty rather
than a coin toss, and it costs well under a second."""


def _finalise_concurrently(run_ids, *, buy_value):
    """Finalise each of ``run_ids`` from its OWN thread, released together.

    A ``threading.Barrier`` is what makes this a race rather than two sequential
    calls: every thread parks until the last one arrives, so they all enter
    ``finalise_allocation_run`` within microseconds of each other and interleave
    the guard read with the ledger write.

    Returns ``(results_by_slot, errors)``. Exceptions are collected rather than
    raised in the worker, because a thread that dies takes its traceback with it
    and would otherwise surface only as a mystified assertion on the ledger.
    """
    barrier = threading.Barrier(len(run_ids))
    results = {}
    errors = []

    def worker(slot, run_id):
        try:
            barrier.wait(timeout=30)
            results[slot] = store.finalise_allocation_run(
                run_id, filled_buy_value=buy_value, filled_sell_value=0.0,
                order_ids=[])
        except BaseException as exc:            # noqa: BLE001 -- reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(slot, run_id))
               for slot, run_id in enumerate(run_ids)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(t.is_alive() for t in threads), \
        "a finalise_allocation_run thread never returned -- deadlock, not a race"
    return results, errors


def test_the_race_tests_run_on_a_wal_engine_with_a_busy_timeout(file_backed_db):
    """The fix leans on the live pragmas, so pin that the fixture really has them.

    ``BEGIN IMMEDIATE`` only makes concurrent finalisation SAFE if a blocked
    writer WAITS: with the default zero busy timeout the loser would get an
    instant ``database is locked`` instead of the right answer, and the race
    tests below would be proving something else entirely.
    """
    with file_backed_db.connect() as connection:
        journal = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert journal.lower() == "wal"
    assert busy == 30000


def test_two_threads_finalising_one_run_consume_the_ledger_exactly_once(file_backed_db):
    """C-1. The replay guard is a check-then-act, so it needs a LOCK, not a read.

    Two callers finalising the SAME run -- a retry racing the original, or two
    NiceGUI tabs on one submit -- both read ``income_consumed_at`` as NULL,
    because pysqlite issues no ``BEGIN`` ahead of a ``SELECT`` and so takes no
    snapshot. Both then spend the ledger. Neither raises, and both report the
    same, correct-looking ``income_consumed_amount``; the only visible trace is
    that the deposit has been eaten twice.

    400 consumed against a 1,000 deposit must leave 600 open. Anything else is
    real income that has silently vanished from the figure the page shows and
    pre-fills into the wizard.
    """
    from tests.factories import create_account_definition

    wrong = []
    for trial in range(RACE_TRIALS):
        account = create_account_definition(name=f"Race one-run {trial}").id
        store.upsert_income_event(account, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
        run = store.record_allocation_run(account, "REBALANCE", {})

        results, errors = _finalise_concurrently([run.id, run.id], buy_value=400.0)
        assert not errors, f"trial {trial} raised: {errors!r}"
        assert len(results) == 2

        open_total = round(store.get_open_income_total(account), 2)
        if open_total != 600.0:
            wrong.append((trial, open_total))

    assert wrong == [], (
        f"{len(wrong)}/{RACE_TRIALS} trials double-spent the income ledger "
        f"(expected 600.0 open, got): {wrong}")


LEDGER_READ_HOLD_SECONDS = 1.0
"""How long the first racer holds open the "both have read, neither has written"
window in the lost-update test. A plain barrier will NOT produce that window --
over 125 measured trials the two threads serialised every single time -- so the
gate below forces the interleaving instead of hoping for it. The wait is bounded
because the FIXED code must be allowed to never arrive; see the test."""


def _install_ledger_read_gate(monkeypatch):
    """Make the first racer pause between reading the ledger and writing it.

    Patches the FIFO helper the store calls after its ``SELECT`` of
    ``portfolio_income_event`` and before it writes the takes back, which is
    exactly the read-modify-write that must not interleave. The first caller to
    arrive parks until the second one does; the second releases it and walks on.

    The wait is bounded by ``LEDGER_READ_HOLD_SECONDS`` on purpose, and the bound
    is the whole point rather than a safety net: once the store takes a write
    lock before its first read, the second caller is BLOCKED OUTSIDE this
    function and can never arrive. An unbounded rendezvous would then deadlock
    the fix it is meant to certify. Timing out and carrying on is what "the other
    thread is waiting its turn, as it should be" looks like from in here.
    """
    real_consume = store.consume_income_events
    lock = threading.Lock()
    seen = []
    second_arrived = threading.Event()

    def gated_consume(open_events, budget):
        with lock:
            seen.append(1)
            arrival = len(seen)
        if arrival == 1:
            second_arrived.wait(timeout=LEDGER_READ_HOLD_SECONDS)
        else:
            second_arrived.set()
        return real_consume(open_events, budget)

    monkeypatch.setattr(store, "consume_income_events", gated_consume)


def test_two_threads_finalising_different_runs_do_not_lose_a_consumption(
        file_backed_db, monkeypatch):
    """The narrower window: two DIFFERENT runs racing on the SAME ledger.

    Nothing here is a replay -- both runs are entitled to their 400 -- so the
    per-run ``income_consumed_at`` guard has nothing to say about it, and a
    conditional ``UPDATE ... WHERE income_consumed_at IS NULL`` would not close
    it either. What breaks is plain lost update: both read the deposit as fully
    open, both write ``consumed_amount = 400``, and one run's spend silently
    overwrites the other's. The ledger then shows 600 open where 200 is the
    truth, and the next run cheerfully spends 400 that is already gone.

    THE RUNS ARE RECORDED WITH THE TOTALS THEY ARE FINALISED WITH, and that is
    load-bearing, not incidental. In the ordinary flow the run is recorded with
    zeros, so ``finalise_allocation_run``'s totals assignment leaves the row
    dirty and the very next ``session.exec`` AUTOFLUSHES it -- an ``UPDATE
    portfolio_allocation_run`` that takes SQLite's write lock BEFORE the ledger
    is read, incidentally serialising the two racers. (Verified by dumping the
    blocked thread's stack: it sits in ``cursor.execute`` under
    ``session._autoflush`` inside ``_apply_income_consumption``.) Pre-set totals
    dirty nothing, no autoflush happens, no lock is taken, and the window is
    wide open. Relying on an ORM flush ordering to protect the money ledger is
    not a guarantee, so the store takes the write lock explicitly and this test
    pins the case where the accident does not save it.
    """
    from tests.factories import create_account_definition

    _install_ledger_read_gate(monkeypatch)

    account = create_account_definition(name="Race two-runs").id
    store.upsert_income_event(account, "csd-1", date(2026, 8, 1), "DEPOSIT", 1000.0)
    recorded = dict(filled_buy_value=400.0, filled_sell_value=0.0, order_ids=[])
    first = store.record_allocation_run(account, "REBALANCE", {}, **recorded)
    second = store.record_allocation_run(account, "REBALANCE", {}, **recorded)

    results, errors = _finalise_concurrently([first.id, second.id], buy_value=400.0)
    assert not errors, f"a racer raised: {errors!r}"

    booked = round(sum(r.income_consumed_amount for r in results.values()), 2)
    assert booked == 800.0, "both runs must still record the 400 each of them spent"
    assert round(store.get_open_income_total(account), 2) == 200.0, (
        "the ledger lost one run's consumption: two runs spent 400 each out of a "
        "1,000 deposit, so 200 must be left open")


# --- page helpers: bulk label selection and symbol membership --------------

def _instrument_labels(symbol):
    from sqlmodel import select
    from ba2_trade_platform.core.db import get_db as _get_db
    from ba2_trade_platform.core.models import Instrument
    with _get_db() as session:
        inst = session.exec(select(Instrument).where(Instrument.name == symbol)).first()
        return list(inst.labels) if inst else None


def test_replace_managed_labels_creates_rows_in_selection_order(account_id):
    store.replace_managed_labels(account_id, ['NASDAQ30', 'ARK26'])
    labels = store.get_managed_labels(account_id)
    assert [r.label for r in labels] == ['NASDAQ30', 'ARK26']
    assert all(r.target_pct == 0.0 for r in labels)


def test_replace_managed_labels_is_idempotent_and_reports_no_change(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    assert store.replace_managed_labels(account_id, ['ARK26']) == {'added': 0, 'removed': 0}
    assert [r.label for r in store.get_managed_labels(account_id)] == ['ARK26']


def test_replace_managed_labels_unmanaging_deletes_the_symbol_rows(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', comment='core holding')
    assert len(store.get_symbol_rows(account_id, 'ARK26')) == 1

    assert store.replace_managed_labels(account_id, []) == {'added': 0, 'removed': 1}
    assert store.get_managed_labels(account_id) == []
    assert store.get_symbol_rows(account_id, 'ARK26') == {}


def test_replace_managed_labels_is_scoped_per_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name='Other Account')
    store.replace_managed_labels(account_id, ['ARK26'])
    store.replace_managed_labels(other.id, ['NASDAQ30'])
    assert [r.label for r in store.get_managed_labels(account_id)] == ['ARK26']
    assert [r.label for r in store.get_managed_labels(other.id)] == ['NASDAQ30']


def test_replace_managed_labels_keeps_a_surviving_labels_target_and_comment(account_id):
    """Re-saving the picker must not reset the labels the user KEPT.

    This is the label-level twin of the comment-only-zeroes-the-weight bug
    (``test_set_symbol_weight_leaves_unpassed_fields_untouched``): the picker
    fires on every change event, so a bulk writer that re-created a surviving
    row -- or wrote ``target_pct=0.0`` over it -- would wipe a 40% target every
    time the user ticked an unrelated label, and the next rebalance would sell
    the whole basket down to nothing.
    """
    store.set_managed_label(account_id, 'ARK26', target_pct=40.0, comment='growth')
    store.replace_managed_labels(account_id, ['NASDAQ30', 'ARK26'])
    kept = {r.label: r for r in store.get_managed_labels(account_id)}['ARK26']
    assert kept.target_pct == 40.0
    assert kept.comment == 'growth'


def test_get_symbol_comments_returns_only_symbols_that_have_one(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    assert store.get_symbol_comments(account_id, 'ARK26') == {}
    store.set_symbol_weight(account_id, 'ARK26', ' tsla ', comment='trim on strength')
    store.set_symbol_weight(account_id, 'ARK26', 'PLTR', weight_pct=25.0)
    assert store.get_symbol_comments(account_id, 'ARK26') == {'TSLA': 'trim on strength'}


def test_a_comment_only_write_on_an_unstored_symbol_creates_an_explicit_zero(account_id):
    """Characterisation of the trap the PAGE has to avoid -- deliberately preserved.

    ``set_symbol_weight`` creates the row with the model default
    ``weight_pct=0.0`` and ``get_symbol_weights`` treats a row's EXISTENCE as an
    explicit weight, so a bare comment write moves the symbol off its even-split
    default onto a hard 0% and the next rebalance sells it out.

    The invariant guarded here is the one the plan insists on: ``weight_pct ==
    0.0`` stays a LEGITIMATE explicit zero and is never re-read as "unstored"
    (doing that would re-introduce drift from the engine's
    ``build_symbol_targets``). The fix therefore belongs at the call site --
    see the test below -- and this test exists so that anyone who "fixes" it
    here instead has to justify it.
    """
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', comment='core holding')
    assert store.get_symbol_weights(account_id, 'ARK26', ['TSLA', 'PLTR']) == {
        'TSLA': 0.0, 'PLTR': 100.0}


def test_a_comment_written_with_the_effective_weight_preserves_the_allocation(account_id):
    """The pattern the page uses: read the EFFECTIVE weight, write it with the comment.

    Passing the even-split default back in makes the row explicit at exactly the
    value it already had, so the comment saves and not one symbol's allocation
    moves.
    """
    symbols = ['TSLA', 'PLTR']
    before = store.get_symbol_weights(account_id, 'ARK26', symbols)
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA',
                            weight_pct=before['TSLA'], comment='core holding')
    assert store.get_symbol_weights(account_id, 'ARK26', symbols) == before
    assert store.get_symbol_comments(account_id, 'ARK26') == {'TSLA': 'core holding'}


def test_add_symbols_to_label_labels_the_instruments_normalised(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    assert store.add_symbols_to_label(account_id, 'ARK26', [' tsla ', 'roku']) == 2
    assert _instrument_labels('TSLA') == ['ARK26']
    assert _instrument_labels('ROKU') == ['ARK26']


def test_remove_symbols_from_label_unlabels_and_deletes_the_symbol_row(account_id):
    store.replace_managed_labels(account_id, ['ARK26'])
    store.add_symbols_to_label(account_id, 'ARK26', ['TSLA'])
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', comment='core holding')

    assert store.remove_symbols_from_label(account_id, 'ARK26', ['tsla']) == 1
    assert _instrument_labels('TSLA') == []
    assert store.get_symbol_rows(account_id, 'ARK26') == {}


def test_remove_symbols_from_label_leaves_the_other_labels_rows_alone(account_id):
    """A symbol may sit in two managed labels; removing it from one keeps the other.

    The delete is scoped by ``(account_id, label, symbol)``. Widening it to
    ``(account_id, symbol)`` would silently discard the weight and comment the
    user set under the OTHER basket.
    """
    store.replace_managed_labels(account_id, ['ARK26', 'HighRisk'])
    store.add_symbols_to_label(account_id, 'ARK26', ['TSLA'])
    store.add_symbols_to_label(account_id, 'HighRisk', ['TSLA'])
    store.set_symbol_weight(account_id, 'ARK26', 'TSLA', weight_pct=60.0, comment='ark note')
    store.set_symbol_weight(account_id, 'HighRisk', 'TSLA', weight_pct=30.0, comment='hr note')

    assert store.remove_symbols_from_label(account_id, 'ARK26', ['TSLA']) == 1
    assert store.get_symbol_rows(account_id, 'ARK26') == {}
    survivor = store.get_symbol_rows(account_id, 'HighRisk')['TSLA']
    assert (survivor.weight_pct, survivor.comment) == (30.0, 'hr note')
    assert _instrument_labels('TSLA') == ['HighRisk']


# --- filled_* is the contract, not submitted_* ------------------------------

def test_finalise_allocation_run_takes_filled_values_not_submitted_ones(account_id):
    """The keyword names ARE the contract. ``filled_buy_value`` invited callers to
    pass what they intended to trade; ``filled_buy_value`` asks for what the broker
    actually did, which is the only number the income ledger may be spent against."""
    store.upsert_income_event(account_id, "dep-rename", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=1600.0, filled_sell_value=400.0, order_ids=[101, 102])

    assert finalised.filled_buy_value == 1600.0
    assert finalised.filled_sell_value == 400.0
    assert finalised.net_buy_value == 1200.0
    assert finalised.income_consumed_amount == pytest.approx(1200.0)


def test_record_allocation_run_takes_filled_values_too(account_id):
    """Same spelling on the CREATE path, which normally passes zeros."""
    run = store.record_allocation_run(account_id, "REBALANCE", {},
                                      filled_buy_value=0.0, filled_sell_value=0.0)
    assert run.filled_buy_value == 0.0
    assert run.filled_sell_value == 0.0


# --- deferred finalisation: orders still working at the broker ---------------

def test_finalising_with_unsettled_orders_records_totals_but_spends_nothing(account_id):
    """An order still working is worth 0 right now, and 0 must not be STAMPED as
    the final answer -- the stamp is one-shot, so it would strand the income."""
    store.upsert_income_event(account_id, "dep-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=0.0, filled_sell_value=0.0,
        order_ids=[101, 102], orders_settled=False)

    assert finalised.order_ids == [101, 102]
    assert finalised.income_consumed_at is None
    assert finalised.is_income_consumed is False
    assert finalised.income_consumed_amount == 0.0
    assert store.get_open_income_total(account_id) == pytest.approx(5000.0)


def test_a_deferred_run_records_the_part_that_did_fill(account_id):
    """A partial fill is real money and belongs in the audit row even though the
    ledger is not spent yet."""
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    finalised = store.finalise_allocation_run(
        run.id, filled_buy_value=640.0, filled_sell_value=0.0,
        order_ids=[101], orders_settled=False)

    assert finalised.filled_buy_value == pytest.approx(640.0)
    assert finalised.is_income_consumed is False


def test_a_deferred_run_stays_in_the_recovery_view(account_id):
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    store.finalise_allocation_run(run.id, filled_buy_value=0.0, filled_sell_value=0.0,
                                  order_ids=[101], orders_settled=False)
    assert [r.id for r in store.get_unconsumed_runs(account_id)] == [run.id]


def test_a_deferred_run_consumes_when_it_is_finalised_again_as_settled(account_id):
    """The recovery path. Same run, re-measured once its orders settled."""
    store.upsert_income_event(account_id, "dep-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    store.finalise_allocation_run(run.id, filled_buy_value=0.0, filled_sell_value=0.0,
                                  order_ids=[101], orders_settled=False)

    settled = store.finalise_allocation_run(
        run.id, filled_buy_value=1600.0, filled_sell_value=0.0,
        order_ids=[101], orders_settled=True)

    assert settled.income_consumed_amount == pytest.approx(1600.0)
    assert settled.filled_buy_value == pytest.approx(1600.0)
    assert store.get_open_income_total(account_id) == pytest.approx(3400.0)
    assert store.get_unconsumed_runs(account_id) == []


def test_a_deferred_run_that_ends_up_filling_nothing_consumes_nothing(account_id):
    """Deferred, then every order came back rejected. Zero is now the TRUE answer,
    so it is stamped -- the run leaves the recovery view having spent nothing."""
    store.upsert_income_event(account_id, "dep-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    store.finalise_allocation_run(run.id, filled_buy_value=0.0, filled_sell_value=0.0,
                                  order_ids=[101], orders_settled=False)

    settled = store.finalise_allocation_run(
        run.id, filled_buy_value=0.0, filled_sell_value=0.0,
        order_ids=[101], orders_settled=True)

    assert settled.is_income_consumed is True
    assert settled.income_consumed_amount == 0.0
    assert store.get_open_income_total(account_id) == pytest.approx(5000.0)
    assert store.get_unconsumed_runs(account_id) == []


def test_deferring_never_un_consumes_an_already_consumed_run(account_id):
    """A run that already spent must not be re-opened by a late 'still working'
    report. The replay guard outranks the settled flag."""
    store.upsert_income_event(account_id, "dep-1", date(2026, 8, 1), "DEPOSIT", 5000.0)
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    store.finalise_allocation_run(run.id, filled_buy_value=1600.0, filled_sell_value=0.0,
                                  order_ids=[101])

    again = store.finalise_allocation_run(
        run.id, filled_buy_value=1600.0, filled_sell_value=0.0,
        order_ids=[101], orders_settled=False)

    assert again.is_income_consumed is True
    assert again.income_consumed_amount == pytest.approx(1600.0)
    assert store.get_open_income_total(account_id) == pytest.approx(3400.0)


def test_orders_settled_defaults_to_true(account_id):
    """Every existing caller keeps its meaning: pass nothing, consume as before."""
    import inspect
    signature = inspect.signature(store.finalise_allocation_run)
    assert signature.parameters["orders_settled"].default is True


def test_the_deferred_path_still_takes_the_write_lock_before_reading(account_id, monkeypatch):
    """BEGIN IMMEDIATE is not conditional on consuming. The deferred path still
    does a check-then-act (read the stamp, write the totals), and dropping the lock
    for it would reopen the exact race _begin_write_transaction closes."""
    calls = []
    original = store._begin_write_transaction

    def spy(session):
        calls.append(True)
        return original(session)

    monkeypatch.setattr(store, "_begin_write_transaction", spy)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    store.finalise_allocation_run(run.id, filled_buy_value=0.0, filled_sell_value=0.0,
                                  order_ids=[], orders_settled=False)

    assert calls == [True]


# --- append_run_order_ids: durability at SUBMISSION time --------------------
#
# record_allocation_run writes no order ids and finalise_allocation_run is the
# only other writer, so for the whole of the submission loop the run row claimed
# the run had created nothing. A crash in there stranded orders that had really
# reached the broker in a run the recovery drain then priced at zero.

def test_append_run_order_ids_adds_an_id_to_an_empty_run(account_id):
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    assert run.order_ids == []

    assert store.append_run_order_ids(run.id, [7]) == [7]
    assert store.get_recent_runs(account_id)[0].order_ids == [7]


def test_append_run_order_ids_keeps_what_is_already_there(account_id):
    run = store.record_allocation_run(account_id, "REBALANCE", {}, order_ids=[3])

    store.append_run_order_ids(run.id, [9])

    assert store.get_recent_runs(account_id)[0].order_ids == [3, 9]


def test_append_run_order_ids_never_duplicates_an_id(account_id):
    """A retried whole-share fallback re-reports the id of the order it already
    created; a run listing it twice would be measured twice."""
    run = store.record_allocation_run(account_id, "REBALANCE", {}, order_ids=[3])

    store.append_run_order_ids(run.id, [3, 4, 4])

    assert store.get_recent_runs(account_id)[0].order_ids == [3, 4]


def test_append_run_order_ids_with_nothing_to_add_writes_nothing(account_id):
    run = store.record_allocation_run(account_id, "REBALANCE", {}, order_ids=[3])
    assert store.append_run_order_ids(run.id, []) == [3]
    assert store.get_recent_runs(account_id)[0].order_ids == [3]


def test_append_run_order_ids_takes_the_write_lock_before_reading(account_id, monkeypatch):
    """Read-modify-write on a JSON column, called once per order while another
    caller may be finalising the same run. BEGIN IMMEDIATE or the append is lost."""
    calls = []
    original = store._begin_write_transaction

    def spy(session):
        calls.append(True)
        return original(session)

    monkeypatch.setattr(store, "_begin_write_transaction", spy)
    run = store.record_allocation_run(account_id, "REBALANCE", {})

    store.append_run_order_ids(run.id, [1])

    assert calls == [True]


def test_append_run_order_ids_raises_for_a_run_that_does_not_exist():
    from ba2_trade_platform.core.db import InstanceNotFound

    with pytest.raises(InstanceNotFound):
        store.append_run_order_ids(987654, [1])


def test_appending_to_an_already_consumed_run_still_records_the_order(account_id):
    """The stamp closes the LEDGER, not the audit. An order that turns up after a
    run consumed still belongs to it, and hiding it would leave a broker order no
    run in the system admits to."""
    run = store.record_allocation_run(account_id, "REBALANCE", {})
    store.finalise_allocation_run(run.id, filled_buy_value=0.0, filled_sell_value=0.0,
                                  order_ids=[1])

    store.append_run_order_ids(run.id, [2])

    stored = store.get_recent_runs(account_id)[0]
    assert stored.order_ids == [1, 2]
    assert stored.income_consumed_at is not None


# ---------------------------------------------------------------------------
# W0: persisting the targets the user actually allocated with.
#
# Until this existed the wizard was write-only-to-memory: ``_on_dry_run``
# persisted only the fractional switch, so every ``target_pct`` stayed at the
# 0.0 the label picker created it with and there could never be a "last".
#
# It is a SEPARATE writer on purpose. ``set_managed_label`` and
# ``set_symbol_weight`` stay byte-identical, because the comment-save path
# (``_write_symbol_comment``) deliberately re-writes ``weight_pct`` on every
# debounced keystroke to avoid zeroing the row, and anything this function grows
# later -- the previous-target shift of W2 above all -- must never fire there.
# ---------------------------------------------------------------------------

def _label_target(label, target_pct, symbols=()):
    from ba2_trade_platform.core.portfolio_allocation import LabelTarget, SymbolTarget
    return LabelTarget(label=label, target_pct=target_pct,
                       symbols=[SymbolTarget(symbol=s, weight_pct=w) for s, w in symbols])


def test_save_allocation_targets_writes_the_label_percentages(account_id):
    store.set_managed_label(account_id, "ARK26")
    store.set_managed_label(account_id, "TECH")

    store.save_allocation_targets(account_id, [_label_target("ARK26", 60.0),
                                               _label_target("TECH", 40.0)])

    stored = {row.label: row.target_pct for row in store.get_managed_labels(account_id)}
    assert stored == {"ARK26": 60.0, "TECH": 40.0}


def test_save_allocation_targets_writes_the_symbol_weights(account_id):
    store.set_managed_label(account_id, "ARK26")

    store.save_allocation_targets(
        account_id, [_label_target("ARK26", 100.0, [("AAPL", 70.0), ("MSFT", 30.0)])])

    assert store.get_symbol_weights(account_id, "ARK26", ["AAPL", "MSFT"]) == {
        "AAPL": 70.0, "MSFT": 30.0}


def test_save_allocation_targets_makes_an_even_split_default_explicit(account_id):
    """A symbol with no row was silently taking the even-split default. Once the
    user has ALLOCATED with that number it stops being a default and becomes a
    choice, so the row is created -- which is what "load last" then reads back.

    Documented consequence, the same one the comment path already accepts: adding a
    symbol to the label later re-splits only what is LEFT, not the whole 100%.
    """
    store.set_managed_label(account_id, "ARK26")
    assert store.get_symbol_rows(account_id, "ARK26") == {}

    store.save_allocation_targets(
        account_id, [_label_target("ARK26", 100.0, [("AAPL", 50.0), ("MSFT", 50.0)])])

    assert sorted(store.get_symbol_rows(account_id, "ARK26")) == ["AAPL", "MSFT"]


def test_save_allocation_targets_leaves_comments_and_sort_order_alone(account_id):
    store.set_managed_label(account_id, "ARK26", sort_order=3, comment="core basket")
    store.set_symbol_weight(account_id, "ARK26", "AAPL", weight_pct=10.0,
                            comment="trim on strength")

    store.save_allocation_targets(
        account_id, [_label_target("ARK26", 100.0, [("AAPL", 100.0)])])

    label_row = store.get_managed_labels(account_id)[0]
    assert label_row.comment == "core basket"
    assert label_row.sort_order == 3
    assert store.get_symbol_rows(account_id, "ARK26")["AAPL"].comment == "trim on strength"


def test_save_allocation_targets_never_resurrects_an_unmanaged_label(account_id):
    """``set_managed_label`` CREATES the row it cannot find, at target_pct=0. A
    wizard opened before the label was unmanaged (another tab, the picker) still
    holds it in memory, and re-creating it here would put a label the user deleted
    back into every future rebalance."""
    store.set_managed_label(account_id, "ARK26")

    written = store.save_allocation_targets(
        account_id, [_label_target("ARK26", 50.0, [("AAPL", 100.0)]),
                     _label_target("GONE", 50.0, [("TSLA", 100.0)])])

    assert [row.label for row in store.get_managed_labels(account_id)] == ["ARK26"]
    assert store.get_symbol_rows(account_id, "GONE") == {}
    assert written["skipped_labels"] == 1


def test_save_allocation_targets_reports_what_it_wrote(account_id):
    store.set_managed_label(account_id, "ARK26")

    written = store.save_allocation_targets(
        account_id, [_label_target("ARK26", 100.0, [("AAPL", 60.0), ("MSFT", 40.0)])])

    assert written == {"labels": 1, "symbols": 2, "skipped_labels": 0}


def test_save_allocation_targets_of_nothing_is_a_no_op(account_id):
    assert store.save_allocation_targets(account_id, []) == {
        "labels": 0, "symbols": 0, "skipped_labels": 0}
    assert store.get_managed_labels(account_id) == []


def test_save_allocation_targets_can_skip_the_label_percentages(account_id):
    """An INVEST_LABEL run spends an explicit AMOUNT on one label; that label's
    percentage is meaningless to it and must not be restated as if the user had
    chosen it. Its symbol weights ARE what the money was split by, so those are
    still what "last" should answer with."""
    store.set_managed_label(account_id, "ARK26", target_pct=25.0)

    store.save_allocation_targets(
        account_id, [_label_target("ARK26", 99.0, [("AAPL", 100.0)])],
        save_label_targets=False)

    assert store.get_managed_labels(account_id)[0].target_pct == 25.0
    assert store.get_symbol_weights(account_id, "ARK26", ["AAPL"]) == {"AAPL": 100.0}


def test_save_allocation_targets_rejects_a_blank_symbol_before_writing_anything(account_id):
    """One transaction: a set that cannot be written in full is not written at all,
    so the stored targets can never be half of what the user allocated with."""
    store.set_managed_label(account_id, "ARK26")
    store.set_managed_label(account_id, "TECH")

    with pytest.raises(ValueError):
        store.save_allocation_targets(
            account_id, [_label_target("ARK26", 50.0, [("AAPL", 100.0)]),
                         _label_target("TECH", 50.0, [("  ", 100.0)])])

    stored = {row.label: row.target_pct for row in store.get_managed_labels(account_id)}
    assert stored == {"ARK26": 0.0, "TECH": 0.0}
    assert store.get_symbol_rows(account_id, "ARK26") == {}


def test_save_allocation_targets_normalises_symbols_like_every_other_writer(account_id):
    store.set_managed_label(account_id, "ARK26")

    store.save_allocation_targets(
        account_id, [_label_target("ARK26", 100.0, [(" aapl ", 100.0)])])

    assert list(store.get_symbol_rows(account_id, "ARK26")) == ["AAPL"]


def test_save_allocation_targets_is_scoped_to_one_account(account_id):
    from tests.factories import create_account_definition
    other = create_account_definition(name="Other Account")
    store.set_managed_label(account_id, "ARK26")
    store.set_managed_label(other.id, "ARK26")

    store.save_allocation_targets(account_id,
                                  [_label_target("ARK26", 77.0, [("AAPL", 100.0)])])

    assert store.get_managed_labels(other.id)[0].target_pct == 0.0
    assert store.get_symbol_rows(other.id, "ARK26") == {}
