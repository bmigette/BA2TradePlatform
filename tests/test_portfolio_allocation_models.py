"""The five portfolio-allocation tables: round-trip, idempotency keys, computed properties."""
from datetime import date, datetime as DateTime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import (
    PortfolioAllocationConfig,
    PortfolioAllocationLabel,
    PortfolioAllocationRun,
    PortfolioAllocationSymbol,
    PortfolioIncomeEvent,
)


def test_allocation_label_round_trips_with_its_fields(mock_account_def):
    add_instance(PortfolioAllocationLabel(
        account_id=mock_account_def.id, label="ARK26", target_pct=40.0,
        sort_order=2, comment="growth sleeve"))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationLabel)).one()
        assert row.label == "ARK26"
        assert row.target_pct == 40.0
        assert row.sort_order == 2
        assert row.comment == "growth sleeve"


def test_duplicate_label_on_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioAllocationLabel(
        account_id=mock_account_def.id, label="ARK26", target_pct=40.0))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioAllocationLabel(
            account_id=mock_account_def.id, label="ARK26", target_pct=60.0))


def test_same_symbol_in_two_labels_is_allowed(mock_account_def):
    add_instance(PortfolioAllocationSymbol(
        account_id=mock_account_def.id, label="ARK26", symbol="TSLA", weight_pct=50.0))
    add_instance(PortfolioAllocationSymbol(
        account_id=mock_account_def.id, label="HighRisk", symbol="TSLA", weight_pct=25.0))
    with get_db() as session:
        rows = session.exec(select(PortfolioAllocationSymbol)).all()
        assert sorted(r.label for r in rows) == ["ARK26", "HighRisk"]


def test_duplicate_external_id_on_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioIncomeEvent(
        account_id=mock_account_def.id, external_id="act-1",
        event_date=date(2026, 8, 1), event_type="DEPOSIT", amount=1000.0))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioIncomeEvent(
            account_id=mock_account_def.id, external_id="act-1",
            event_date=date(2026, 8, 1), event_type="DEPOSIT", amount=1000.0))


def test_income_event_open_amount_is_the_unconsumed_remainder():
    event = PortfolioIncomeEvent(
        account_id=1, external_id="act-1", event_date=date(2026, 8, 1),
        event_type="DIVIDEND", amount=250.0, consumed_amount=90.0)
    assert event.open_amount == 160.0


def test_income_event_open_amount_never_goes_negative():
    event = PortfolioIncomeEvent(
        account_id=1, external_id="act-1", event_date=date(2026, 8, 1),
        event_type="DEPOSIT", amount=100.0, consumed_amount=140.0)
    assert event.open_amount == 0.0


def test_run_net_buy_value_is_filled_buys_minus_filled_sells():
    """The columns are FILLED value, not submitted value. A run whose orders never
    filled has zeros here and so consumes nothing from the income ledger."""
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 filled_buy_value=5000.0, filled_sell_value=1200.0)
    assert run.filled_buy_value == 5000.0
    assert run.filled_sell_value == 1200.0
    assert run.net_buy_value == 3800.0


def test_run_net_buy_value_is_zero_when_sells_exceed_buys():
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 filled_buy_value=1000.0, filled_sell_value=4000.0)
    assert run.net_buy_value == 0.0


def test_run_with_nothing_filled_has_no_net_buy_value():
    """The whole point of the rename: submitted-but-unfilled is worth 0 here."""
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE")
    assert run.filled_buy_value == 0.0
    assert run.filled_sell_value == 0.0
    assert run.net_buy_value == 0.0


def test_a_fresh_run_has_not_consumed_income():
    """NULL ``income_consumed_at`` is what "this run has never spent from the
    ledger" is spelled as -- the guard finalise_allocation_run checks."""
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE")
    assert run.income_consumed_at is None
    assert run.is_income_consumed is False
    assert run.income_consumed_amount == 0.0


def test_run_income_consumed_amount_sums_the_breakdown():
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 income_consumed_events=[[7, 100.0], [8, 150.5]])
    assert run.income_consumed_amount == pytest.approx(250.5)


def test_a_run_that_consumed_nothing_is_still_marked_consumed():
    """A rebalance funded by its own sells takes 0.0 from the ledger, which is NOT
    the same state as never having tried -- otherwise a recovery pass would
    re-run it forever."""
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 income_consumed_at=DateTime(2026, 8, 20, 12, 0),
                                 income_consumed_events=[])
    assert run.is_income_consumed is True
    assert run.income_consumed_amount == 0.0


def test_run_income_columns_round_trip(mock_account_def):
    add_instance(PortfolioAllocationRun(
        account_id=mock_account_def.id, mode="REBALANCE",
        income_consumed_at=DateTime(2026, 8, 20, 12, 0),
        income_consumed_events=[[3, 42.5]]))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationRun)).one()
        assert row.is_income_consumed is True
        assert row.income_consumed_events == [[3, 42.5]]
        assert row.income_consumed_amount == pytest.approx(42.5)


def test_run_json_columns_round_trip(mock_account_def):
    add_instance(PortfolioAllocationRun(
        account_id=mock_account_def.id, mode="INVEST_LABEL", scope_label="ARK26",
        plan_json={"rows": [{"symbol": "TSLA", "side": "BUY"}], "scale_factor": 0.61},
        order_ids=[11, 12, 13]))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationRun)).one()
        assert row.plan_json["scale_factor"] == 0.61
        assert row.plan_json["rows"][0]["symbol"] == "TSLA"
        assert row.order_ids == [11, 12, 13]


def test_allocation_config_defaults_to_market_mode_and_fractional_shares(mock_account_def):
    """MARKET is the default: the requirement is "allocate by VALUE", and cost mode
    understates the allocatable base by the whole unrealised P&L, so it buys MORE of
    a winner instead of trimming it.

    Fractional defaults ON: about three quarters of the user's symbols ARE
    fractionable, and the quarter that is not falls back to whole shares per symbol
    inside the engine anyway."""
    add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig)).one()
        assert row.valuation_mode == "market"
        assert row.allow_fractional is True


def test_allocation_config_round_trips_cost_mode(mock_account_def):
    """Cost basis is still a first-class, user-selectable mode -- it is the escape
    hatch a held symbol with a failed quote sends the user to."""
    add_instance(PortfolioAllocationConfig(
        account_id=mock_account_def.id, valuation_mode="cost", allow_fractional=True))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig)).one()
        assert row.valuation_mode == "cost"
        assert row.allow_fractional is True


def test_a_second_config_row_for_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))


# --- W2: one generation of "what did I allocate with last time" -------------

def test_a_label_starts_with_no_previous_target(mock_account_def):
    """NULL, not 0.0. "There is no last" and "last time this got nothing" are
    different answers, and the Load-last button's disabled state is exactly the
    first of them."""
    add_instance(PortfolioAllocationLabel(account_id=mock_account_def.id, label='ARK26'))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationLabel)).one()
        assert row.previous_target_pct is None


def test_a_label_round_trips_a_previous_target_of_zero(mock_account_def):
    """0.0 has to be storable and has to read back as 0.0 rather than as None --
    the whole point of the column being nullable is that the two mean different
    things."""
    add_instance(PortfolioAllocationLabel(account_id=mock_account_def.id, label='ARK26',
                                          target_pct=60.0, previous_target_pct=0.0))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationLabel)).one()
        assert row.previous_target_pct == 0.0
        assert row.previous_target_pct is not None


def test_a_symbol_starts_with_no_previous_weight(mock_account_def):
    add_instance(PortfolioAllocationSymbol(account_id=mock_account_def.id,
                                           label='ARK26', symbol='AAPL'))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationSymbol)).one()
        assert row.previous_weight_pct is None


def test_a_symbol_round_trips_a_previous_weight(mock_account_def):
    add_instance(PortfolioAllocationSymbol(account_id=mock_account_def.id, label='ARK26',
                                           symbol='AAPL', weight_pct=70.0,
                                           previous_weight_pct=30.0))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationSymbol)).one()
        assert row.previous_weight_pct == 30.0
